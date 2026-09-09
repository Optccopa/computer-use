"""Screenshot pixels to desktop pixels, and keeping the cursor on the display.

The single most correctness-critical piece of the project: a wrong scale clicks
the wrong thing silently rather than raising. A mixin because it is one coherent
responsibility of Session, not a separate object -- it needs the same live screen
and the same reference size as everything else.
"""

from __future__ import annotations

import math

from cufast import _native
from cufast.session.deltas import _finite_delta
from cufast.session.errors import ActionError
from cufast.session.frames import _span


class CoordinateMixin:
    """Everything about where a coordinate actually lands."""

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
            out = _finite_delta(value * native / ref, "the movement")
            if out == 0 and value != 0:
                out = 1 if value > 0 else -1
            return out

        return (scaled(dx, self.screen.width, ref_w),
                scaled(dy, self.screen.height, ref_h))

    def cursor_is_on_display(self) -> bool:
        x, y = _native.cursor_position()
        lx, ly = x - self.screen.origin_x, y - self.screen.origin_y
        return 0 <= lx < self.screen.width and 0 <= ly < self.screen.height

    def confine_cursor(self, was_on_display: bool) -> None:
        """Puts the cursor back if a relative move walked it off the display.

        Absolute movement is bounds-checked against the display being controlled;
        relative movement went straight around that check. Six mouse_move_rel of
        -400 walked the cursor from the primary onto a second monitor, and a click
        with no coordinate then lands there -- on a display the model was never
        given. That is the one thing the coordinate system exists to prevent.

        A pointer-locked game warps the cursor to its own centre every frame, so it
        never trips this; the check costs one GetCursorPos.
        """
        x, y = _native.cursor_position()
        lx, ly = x - self.screen.origin_x, y - self.screen.origin_y
        if 0 <= lx < self.screen.width and 0 <= ly < self.screen.height:
            return

        clamped_x = min(max(lx, 0), self.screen.width - 1)
        clamped_y = min(max(ly, 0), self.screen.height - 1)
        _native.mouse_move_relative(clamped_x - lx, clamped_y - ly, 1)

        if not was_on_display:
            # It started off-display -- someone else moved it there. Bringing it back
            # is right, but this move is not what took it away, so it is not an error.
            return
        raise ActionError(
            f"that movement took the cursor off display {self.screen.index} to "
            f"({lx}, {ly}), outside its {self.screen.width}x{self.screen.height} "
            "bounds, so it has been moved back to the edge. Relative movement is for "
            "pointer-locked applications, where the cursor never actually travels. "
            "To reach a position, use a coordinate; to control another monitor, pass "
            "`display`."
        )

    def zoom_to_screen(self, x: float, y: float) -> tuple[int, int]:
        """A pixel in the last zoom image -> an absolute desktop pixel.

        This is what makes a zoom actionable rather than merely readable. A zoom is
        captured at native resolution, so one of its pixels is one real pixel of the
        display; a full screenshot of a 1920-wide monitor fitted into a 1024-wide box
        is not, and no coordinate expressed in it can address a specific native pixel.
        Anything that has to be exact -- a one-pixel border, a caret between two
        characters, the gap between adjacent toolbar buttons -- is only reachable
        this way.

        The alternative was making the model convert zoom pixels back to screenshot
        pixels itself, which is arithmetic over four numbers it would have to be told
        and would get wrong silently, landing a confident click somewhere plausible.
        """
        shot = self.last_zoom
        if shot is None:
            raise ActionError(
                "in_zoom was set, but nothing has been zoomed yet in this session. "
                "Run a `zoom` action first; its coordinates are the ones in_zoom "
                "refers to."
            )
        rx, ry, rw, rh = shot.region
        for axis, value, limit in (("x", x, shot.width), ("y", y, shot.height)):
            if not math.isfinite(value):
                raise ActionError(f"{axis} must be a finite number, got {value!r}")
            if value < -1.0 or value >= limit + 1.0:
                raise ActionError(
                    f"{axis}={value:g} is outside the zoom image, which is "
                    f"{shot.width}x{shot.height}. With in_zoom set, coordinates are "
                    f"in the zoom's own pixel space, not the full screenshot's."
                )

        # Centre of the source box, then confined to it -- the same rule to_local
        # uses, and for the same reason: at a scale factor below 2 the centre can
        # round past the end of its own span and land on the neighbouring pixel.
        # Usually the zoom is 1:1 native and both reduce to the identity.
        xi = min(max(int(x), 0), shot.width - 1)
        yi = min(max(int(y), 0), shot.height - 1)
        sx0, sx1 = _span(xi, shot.width, rw)
        sy0, sy1 = _span(yi, shot.height, rh)
        lx = min(max(int((x + 0.5) * rw / shot.width), sx0), sx1 - 1)
        ly = min(max(int((y + 0.5) * rh / shot.height), sy0), sy1 - 1)

        # Region offsets are monitor-local, so this lands on the display the zoom
        # came from even if the harness has since been pointed at another one.
        return (rx + lx + self.screen.origin_x, ry + ly + self.screen.origin_y)

    def resolve_point(self, x: float, y: float, in_zoom: bool) -> tuple[int, int]:
        """One entry point for both coordinate spaces, so no caller can forget."""
        return self.zoom_to_screen(x, y) if in_zoom else self.to_screen(x, y)

    def check_in_frame(self, x: float, y: float) -> None:
        """Rejects a coordinate that is not on the screenshot, moving nothing.

        Separate from aim_delta because aim calibrates before it maps, and
        calibrating turns the view: a bad coordinate used to leave the camera
        rotated by the probe and then blame the coordinate, with nothing undoing it.
        """
        ref_w, ref_h = self._refresh_reference()
        self._check_axis(x, ref_w, "x")
        self._check_axis(y, ref_h, "y")


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

        # The exact inverse of _span, not a rescale. Screenshot pixel x covers native
        # columns [x*W//ref, (x+1)*W//ref), so the pixel containing column lx is the
        # largest x with x*W//ref <= lx. A plain rescale is off by one on roughly half
        # of all columns, always low, and to_local goes to real trouble to keep a
        # coordinate inside its own span -- this has to agree with it.
        x = ((lx + 1) * ref_w + self.screen.width - 1) // self.screen.width - 1
        y = ((ly + 1) * ref_h + self.screen.height - 1) // self.screen.height - 1
        return (
            min(max(x, 0), ref_w - 1),
            min(max(y, 0), ref_h - 1),
            on_display,
        )

