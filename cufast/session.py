"""Capture session: owns one display and the coordinate frame the model works in."""

from __future__ import annotations

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
        self.config = config
        self.screen = _native.Screen(config.display_index)
        # plan_fit comes from the C++ side so this matches the encoder exactly
        # rather than re-deriving the rounding rules in Python.
        self.ref_width, self.ref_height = _native.plan_fit(
            self.screen.width, self.screen.height, config.max_width, config.max_height, False
        )

    @property
    def using_dxgi(self) -> bool:
        return self.screen.using_dxgi

    def describe(self) -> str:
        return (
            f"display {self.screen.index}: {self.screen.width}x{self.screen.height} native "
            f"at ({self.screen.origin_x},{self.screen.origin_y}), "
            f"screenshots are {self.ref_width}x{self.ref_height} "
            f"via {'DXGI Desktop Duplication' if self.using_dxgi else 'GDI BitBlt'}"
        )

    # -- coordinates ----------------------------------------------------------

    def _check_axis(self, value: float, limit: int, axis: str) -> float:
        if value < 0 or value >= limit:
            # A coordinate well outside the frame almost always means the model
            # worked from native display pixels instead of the screenshot it was
            # given. Saying so is far better than clamping and clicking somewhere
            # plausible but wrong.
            slack = max(2.0, limit * 0.02)
            if value < -slack or value >= limit + slack:
                raise ActionError(
                    f"{axis}={value:g} is outside the screenshot, which is "
                    f"{self.ref_width}x{self.ref_height}. Coordinates must be in the "
                    f"pixel space of the screenshot you were given, not the native "
                    f"display resolution ({self.screen.width}x{self.screen.height})."
                )
        return min(max(value, 0.0), float(limit - 1))

    def to_local(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot pixel -> monitor-local pixel."""
        x = self._check_axis(x, self.ref_width, "x")
        y = self._check_axis(y, self.ref_height, "y")
        # Map the centre of the destination pixel to the centre of the source box,
        # so a click lands mid-target instead of biased to its top-left corner.
        lx = (x + 0.5) * self.screen.width / self.ref_width
        ly = (y + 0.5) * self.screen.height / self.ref_height
        return (
            min(int(lx), self.screen.width - 1),
            min(int(ly), self.screen.height - 1),
        )

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot pixel -> absolute virtual-desktop pixel, ready for SendInput."""
        lx, ly = self.to_local(x, y)
        return lx + self.screen.origin_x, ly + self.screen.origin_y

    def cursor_in_screenshot_space(self) -> tuple[int, int]:
        """Absolute cursor position expressed in the frame the model reasons about."""
        sx, sy = _native.cursor_position()
        lx = sx - self.screen.origin_x
        ly = sy - self.screen.origin_y
        x = int(lx * self.ref_width / self.screen.width)
        y = int(ly * self.ref_height / self.screen.height)
        return x, y

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
        return Screenshot(shot.data, shot.width, shot.height, "image/jpeg")

    def zoom(self, region: list[float]) -> Screenshot:
        """Re-capture one region of the screen at full resolution.

        The region arrives in screenshot coordinates as [x0, y0, x1, y1]. Output is
        capped at the normal screenshot size and is never upscaled, matching the
        spec's "full resolution, scaled to fit".
        """
        if len(region) != 4:
            raise ActionError("region must be [x0, y0, x1, y1]")
        x0, y0, x1, y1 = region
        if x1 <= x0 or y1 <= y0:
            raise ActionError(
                f"region must have x1 > x0 and y1 > y0, got [{x0:g}, {y0:g}, {x1:g}, {y1:g}]"
            )

        lx0, ly0 = self.to_local(x0, y0)
        # The far corner is exclusive, so clamp it against the frame rather than
        # running it through the pixel-centre mapping used for click targets.
        lx1 = min(int(round(x1 * self.screen.width / self.ref_width)), self.screen.width)
        ly1 = min(int(round(y1 * self.screen.height / self.ref_height)), self.screen.height)
        w = max(1, lx1 - lx0)
        h = max(1, ly1 - ly0)

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
