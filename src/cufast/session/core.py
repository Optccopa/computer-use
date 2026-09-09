"""One display, one coordinate frame, one record of what the model has seen."""

from __future__ import annotations

import math

from cufast import _native
from cufast.config import Config
from cufast.session.aiming import AimingMixin
from cufast.session.errors import STOPPED_BY_KILL_SWITCH, ActionError
from cufast.session.frames import Screenshot
from cufast.session.geometry import CoordinateMixin


class Session(CoordinateMixin, AimingMixin):
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
        # Degrees of view rotation per screenshot pixel of mouse travel. Unknown
        # until measured, because it depends on the game and on the user's
        # sensitivity slider -- there is no default worth guessing.
        self.look_scale: tuple[float, float] | None = None
        # Native mouse pixels per screenshot pixel of on-screen displacement. This
        # is what `aim` needs, and it is not the same constant as look_scale: one is
        # about how far the view turns per unit of mouse travel, the other folds in
        # the field of view as well, because it answers "how much mouse moves a thing
        # I can see onto the crosshair".
        self.aim_ratio: float | None = None
        # The vertical ratio, measured separately. It is usually the same number as
        # the horizontal one, but not always: a game with invert-Y has the opposite
        # sign, and some have independent per-axis sensitivity. Assuming they match
        # made every pitch wrong on those, from the very first aim, with nothing
        # raising. None means "not measured, use the horizontal one".
        self.aim_ratio_y: float | None = None
        # Identity of the last image actually handed to the model. A desktop is
        # static most of the time an agent is looking at it, and re-sending a frame
        # it already has costs a full image of context to say nothing -- which is
        # also what teaches it that asking again is worth doing.
        self._delivered: tuple[tuple[int, int, int, int], int] | None = None
        self._refresh_reference()

    def mark_delivered(self, shot: Screenshot) -> bool:
        """True when this image is new to the model, and records it as delivered.

        The first capture is always new: a model that has been told "unchanged"
        before it has ever seen the screen has been told nothing at all.
        """
        identity = (shot.region, shot.content_hash)
        if self._delivered == identity:
            return False
        self._delivered = identity
        return True

    def forget_delivered(self) -> None:
        """Drops the record, so the next capture is sent whatever it looks like.

        Used when the model's view of the screen is no longer trustworthy -- a
        display switch, say -- where "unchanged" would be true of the pixels and a
        lie about what it is looking at.
        """
        self._delivered = None

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
            first = self._native_size == (0, 0)
            self._native_size = native
            # plan_fit comes from the C++ side so this matches the encoder exactly
            # rather than re-deriving the rounding rules in Python.
            self._ref = _native.plan_fit(
                native[0], native[1], self.config.max_width, self.config.max_height, False
            )
            if not first:
                self._invalidate_calibration()
        return self._ref

    def _invalidate_calibration(self) -> None:
        """Both calibrations are measured against a specific screenshot size.

        aim_ratio is mouse pixels per SCREENSHOT pixel, so a rotation that takes the
        image from 1024 wide to 432 leaves every aim covering about 42% of the turn
        it should. look_scale is worse: it is applied through the live
        native/reference factor, but the physical unit is resolution-independent, so
        any mode change silently rescales every turn -- a calibrated 30 degrees came
        out as 20 after 1920x1080 -> 1280x720.

        Neither failure raises. Discarding them costs one probe on the next aim,
        which is the cheapest correct answer available.
        """
        self.aim_ratio = None
        self.aim_ratio_y = None
        self.look_scale = None

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
        return Screenshot(
            shot.data, shot.width, shot.height, "image/jpeg",
            content_hash=shot.content_hash,
            region=(shot.src_x, shot.src_y, shot.src_w, shot.src_h),
        )

    def wait_for_change(self, timeout_seconds: float) -> float | None:
        """Blocks until the screen changes. Returns milliseconds, or None on timeout.

        The change is judged on a small fixed grid rather than the full frame, which
        is what keeps a blinking caret or a moving cursor from reading as activity.
        """
        waited = self.screen.wait_for_change(timeout_seconds)
        if waited == -2.0:
            raise ActionError(STOPPED_BY_KILL_SWITCH)
        return None if waited < 0 else waited

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
        lx0 = min(max(math.floor(x0), 0), ref_w - 1) * native_w // ref_w
        ly0 = min(max(math.floor(y0), 0), ref_h - 1) * native_h // ref_h
        lx1 = min(max(math.ceil(x1), 1), ref_w) * native_w // ref_w
        ly1 = min(max(math.ceil(y1), 1), ref_h) * native_h // ref_h

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
        return Screenshot(
            shot.data, shot.width, shot.height, "image/jpeg",
            content_hash=shot.content_hash,
            region=(shot.src_x, shot.src_y, shot.src_w, shot.src_h),
        )
