"""Capture session: owns one display and the coordinate frame the model works in."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cufast import _native
from cufast.config import Config


class ActionError(Exception):
    """A computer action that failed for a reason the model should see and react to."""


@dataclass(frozen=True)
class Screenshot:
    data: bytes
    width: int
    height: int
    media_type: str


def _span(index: int, ref: int, native: int) -> tuple[int, int]:
    """The half-open native range that produced screenshot pixel `index`.

    Mirrors the downscaler exactly: destination pixel x averages source columns
    [x*native/ref, (x+1)*native/ref).
    """
    start = (index * native) // ref
    end = ((index + 1) * native) // ref
    if end <= start:
        end = start + 1
    return start, min(end, native)


class Session:
    """One display, one coordinate frame.

    The model only ever sees screenshots scaled into the configured box, so every
    coordinate it produces is in that scaled space. Mapping back is the single most
    correctness-critical piece of this project: a wrong scale silently clicks the
    wrong thing rather than raising.

    Zoom deliberately does not get its own coordinate frame. The computer-use spec
    is explicit that zoom images do not change the coordinate space, so clicks after
    a zoom are still expressed against the full screenshot and map through the same
    reference size as everything else.
    """

    def __init__(self, config: Config) -> None:
        config.validate()
        self.config = config
        self.screen = _native.Screen(config.display_index)
        self._native_size = (0, 0)
        self._ref = (0, 0)
        self._refresh_reference()

    def _refresh_reference(self) -> tuple[int, int]:
        """Recomputes the screenshot size whenever the display mode changes.

        The native layer follows resolution changes, so caching this once at
        construction would leave the mapping using a live numerator against a stale
        denominator -- clicks land hundreds of pixels away, and coordinates that are
        legal in the delivered image get rejected against a resolution the model was
        never shown.
        """
        native = (self.screen.width, self.screen.height)
        if native != self._native_size:
            self._native_size = native
            # plan_fit comes from the C++ side so this matches the encoder exactly
            # rather than re-deriving the rounding rules in Python.
            self._ref = _native.plan_fit(
                native[0], native[1], self.config.max_width, self.config.max_height, False
            )
        return self._ref

    @property
    def ref_width(self) -> int:
        return self._refresh_reference()[0]

    @property
    def ref_height(self) -> int:
        return self._refresh_reference()[1]

    @property
    def using_dxgi(self) -> bool:
        return self.screen.using_dxgi

    def describe(self) -> str:
        ref_w, ref_h = self._refresh_reference()
        return (
            f"display {self.screen.index}: {self.screen.width}x{self.screen.height} native "
            f"at ({self.screen.origin_x},{self.screen.origin_y}), "
            f"screenshots are {ref_w}x{ref_h} "
            f"via {'DXGI Desktop Duplication' if self.using_dxgi else 'GDI BitBlt'}"
        )

    # -- coordinates ----------------------------------------------------------

    def _out_of_frame(self, axis: str, value: float) -> ActionError:
        ref_w, ref_h = self._refresh_reference()
        return ActionError(
            f"{axis}={value:g} is outside the screenshot, which is {ref_w}x{ref_h}. "
            f"Coordinates must be in the pixel space of the screenshot you were given, "
            f"not the native display resolution "
            f"({self.screen.width}x{self.screen.height})."
        )

    def _check_axis(self, value: float, limit: int, axis: str) -> float:
        # Only rounding-width slack is tolerated. A wider window would silently clamp
        # a genuinely wrong coordinate onto the far edge, and at typical scale factors
        # a few screenshot pixels is tens of native pixels -- wider than a scrollbar
        # or a close button, so it would click something plausible but wrong.
        if value < -1.0 or value >= limit + 1.0:
            raise self._out_of_frame(axis, value)
        return min(max(value, 0.0), float(limit - 1))

    def to_local(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot pixel -> monitor-local pixel."""
        ref_w, ref_h = self._refresh_reference()
        x = self._check_axis(x, ref_w, "x")
        y = self._check_axis(y, ref_h, "y")

        # Aim at the centre of the source box, then confine the result to that box.
        # Without the clamp the centre can round past the span end whenever the scale
        # factor is below 2 -- at 1366x768 into a 1024-wide frame that is a third of
        # all columns landing on a pixel belonging to the neighbouring one.
        xi, yi = int(x), int(y)
        sx0, sx1 = _span(xi, ref_w, self.screen.width)
        sy0, sy1 = _span(yi, ref_h, self.screen.height)
        lx = min(max(int((x + 0.5) * self.screen.width / ref_w), sx0), sx1 - 1)
        ly = min(max(int((y + 0.5) * self.screen.height / ref_h), sy0), sy1 - 1)
        return lx, ly

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot pixel -> absolute virtual-desktop pixel, ready for SendInput."""
        lx, ly = self.to_local(x, y)
        return lx + self.screen.origin_x, ly + self.screen.origin_y

    def scale_delta(self, dx: float, dy: float) -> tuple[int, int]:
        """Screenshot-space delta -> native delta.

        Relative movement is expressed in screenshot pixels so it matches every
        other coordinate the model works in; a delta needs no origin, only the
        scale.

        Rounded to nearest rather than outward: a calibrated turn gets issued over
        and over, and always rounding away from zero biases every one of them in the
        direction of travel. The one exception is a delta that would round to zero,
        which becomes the smallest move in the requested direction -- a model asking
        to turn by a pixel and getting nothing back cannot tell that apart from the
        action not working.
        """
        ref_w, ref_h = self._refresh_reference()
        for name, value in (("dx", dx), ("dy", dy)):
            if not math.isfinite(value):
                raise ActionError(f"{name} must be a finite number, got {value!r}")

        def scaled(value: float, native: int, ref: int) -> int:
            exact = value * native / ref
            # floor(|x| + 0.5), not Python's round(): banker's rounding would send
            # exactly-half deltas alternately up and down, which is the opposite of
            # the reproducibility a calibration needs.
            out = int(math.copysign(math.floor(abs(exact) + 0.5), exact))
            if out == 0 and value != 0:
                out = 1 if value > 0 else -1
            return out

        return (scaled(dx, self.screen.width, ref_w),
                scaled(dy, self.screen.height, ref_h))

    def cursor_in_screenshot_space(self) -> tuple[int, int, bool]:
        """Cursor position in the frame the model reasons about.

        Returns (x, y, on_this_display). The cursor is a desktop-global position and
        may be on another monitor, so this must not hand back a coordinate that
        to_screen would immediately reject.
        """
        ref_w, ref_h = self._refresh_reference()
        sx, sy = _native.cursor_position()
        lx = sx - self.screen.origin_x
        ly = sy - self.screen.origin_y
        on_display = 0 <= lx < self.screen.width and 0 <= ly < self.screen.height

        x = math.floor(lx * ref_w / self.screen.width)
        y = math.floor(ly * ref_h / self.screen.height)
        return (
            min(max(x, 0), ref_w - 1),
            min(max(y, 0), ref_h - 1),
            on_display,
        )

    # -- capture --------------------------------------------------------------

    def screenshot(self) -> Screenshot:
        cfg = self.config
        shot = self.screen.grab(
            max_w=cfg.max_width,
            max_h=cfg.max_height,
            quality=cfg.jpeg_quality,
            draw_cursor=cfg.draw_cursor,
            timeout_ms=cfg.capture_timeout_ms,
        )
        # The delivered image defines the coordinate space, so keep the reference in
        # step with what the model actually received.
        self._native_size = (self.screen.width, self.screen.height)
        self._ref = (shot.width, shot.height)
        return Screenshot(shot.data, shot.width, shot.height, "image/jpeg")

    def zoom(self, region: list[float]) -> Screenshot:
        """Re-capture one region of the screen at full resolution.

        The region arrives in screenshot coordinates as [x0, y0, x1, y1], with the
        far corner exclusive. Output is capped at the normal screenshot size and is
        never upscaled, matching the spec's "full resolution, scaled to fit".
        """
        if len(region) != 4:
            raise ActionError("region must be [x0, y0, x1, y1]")
        x0, y0, x1, y1 = region
        if not all(math.isfinite(v) for v in region):
            raise ActionError("region values must be finite numbers")

        ref_w, ref_h = self._refresh_reference()
        # The far corner is exclusive, so it may equal the frame size but no more.
        # Validating it matters: it is the one coordinate path that would otherwise
        # bypass the check that catches native-resolution coordinates.
        self._check_axis(x0, ref_w, "x0")
        self._check_axis(y0, ref_h, "y0")
        if not -1.0 <= x1 <= ref_w + 1.0:
            raise self._out_of_frame("x1", x1)
        if not -1.0 <= y1 <= ref_h + 1.0:
            raise self._out_of_frame("y1", y1)
        if x1 <= x0 or y1 <= y0:
            raise ActionError(
                f"region must have x1 > x0 and y1 > y0, got [{x0:g}, {y0:g}, {x1:g}, {y1:g}]"
            )

        # Both corners floor through the same span arithmetic the downscaler uses, so
        # the captured rectangle is exactly the native area behind those pixels.
        native_w, native_h = self.screen.width, self.screen.height
        lx0 = min(max(int(math.floor(x0)), 0), ref_w - 1) * native_w // ref_w
        ly0 = min(max(int(math.floor(y0)), 0), ref_h - 1) * native_h // ref_h
        lx1 = min(max(int(math.ceil(x1)), 1), ref_w) * native_w // ref_w
        ly1 = min(max(int(math.ceil(y1)), 1), ref_h) * native_h // ref_h

        w = max(1, min(lx1, native_w) - lx0)
        h = max(1, min(ly1, native_h) - ly0)

        cfg = self.config
        shot = self.screen.grab(
            max_w=cfg.max_width,
            max_h=cfg.max_height,
            quality=cfg.jpeg_quality,
            draw_cursor=cfg.draw_cursor,
            timeout_ms=cfg.capture_timeout_ms,
            rx=lx0,
            ry=ly0,
            rw=w,
            rh=h,
        )
        return Screenshot(shot.data, shot.width, shot.height, "image/jpeg")
