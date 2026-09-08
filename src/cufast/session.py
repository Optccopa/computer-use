"""Capture session: owns one display and the coordinate frame the model works in."""

from __future__ import annotations

import contextlib
import math
import time
from dataclasses import dataclass

from cufast import _native
from cufast.config import Config

# How far to turn when measuring the view's response to the mouse. Big enough to
# shift the image well clear of the noise, small enough not to fling the camera
# somewhere unrecoverable if the sensitivity turns out to be very high.
AIM_PROBE_NATIVE_PX = 120
# Tried when the first probe moved too little to measure, which is what a very low
# sensitivity looks like.
AIM_PROBE_RETRY_PX = 600
# Below this the match is not distinguishable from the average candidate, which is
# what a featureless or repeating view produces. Accepting it would bake a wrong
# ratio into every later aim.
AIM_MIN_CONFIDENCE = 0.12
AIM_MIN_SHIFT_PX = 3
# A ratio outside this says the measurement is wrong rather than the sensitivity
# unusual: one native pixel of mouse cannot pan the view by twenty.
AIM_RATIO_BOUNDS = (0.02, 200.0)
# The native side rejects anything past this, and beyond it the value stops being a
# mouse movement and starts being a way to crash the binding: a delta over 2^31 came
# back as a nanobind TypeError that nothing caught, which destroyed the whole batch
# result including any screenshot already in it.
MAX_NATIVE_DELTA = 100_000


def _finite_delta(value: float, what: str) -> int:
    """Rounds to an int the native layer will accept, or says why it will not.

    Rounds half away from zero rather than to even: a calibrated turn is issued over
    and over, and banker's rounding would send exact halves alternately up and down.
    """
    if not math.isfinite(value):
        raise ActionError(f"{what} came out as {value}, which is not a movement")
    if abs(value) > MAX_NATIVE_DELTA:
        raise ActionError(
            f"{what} came out as {value:.0f} native pixels, past the {MAX_NATIVE_DELTA} "
            "limit. Either the value asked for is far too large, or the calibration "
            "is wrong -- recalibrate rather than repeating this."
        )
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


# Kept here rather than imported from cufast.actions, which imports this module.
STOPPED_BY_KILL_SWITCH = (
    "STOPPED BY THE USER while waiting. They pressed the kill switch (Ctrl+Esc). "
    "Stop what you were doing, do not retry, and tell them you have stopped."
)


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
            out = _finite_delta(value * native / ref, "the movement")
            if out == 0 and value != 0:
                out = 1 if value > 0 else -1
            return out

        return (scaled(dx, self.screen.width, ref_w),
                scaled(dy, self.screen.height, ref_h))

    def set_look_scale(self, yaw_dpp: float, pitch_dpp: float) -> None:
        for name, value in (("yaw", yaw_dpp), ("pitch", pitch_dpp)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ActionError(f"{name} degrees-per-pixel must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ActionError(f"{name} degrees-per-pixel must be positive and finite")
            if value > 90:
                raise ActionError(
                    f"{name} degrees-per-pixel of {value} would sweep the whole view in "
                    "one pixel; the value is probably pixels per degree, which is its "
                    "reciprocal"
                )
        self.look_scale = (float(yaw_dpp), float(pitch_dpp))

    def look_delta(self, yaw_deg: float, pitch_deg: float) -> tuple[int, int]:
        """Degrees of view rotation -> native mouse delta.

        This is what lets a model plan a sequence of turns without looking between
        them. Working in pixels, it cannot predict where a turn ends up, so every
        single turn costs a screenshot and a round trip to find out.
        """
        if self.look_scale is None:
            raise ActionError(
                "look needs a calibration first. Issue a known mouse_move_rel (say "
                "dx=100), read how far the view actually turned off the game's own "
                "readout (in Minecraft, F3 shows Yaw and Pitch), divide degrees by "
                "pixels, and pass the result to set_look_scale."
            )
        for name, value in (("yaw", yaw_deg), ("pitch", pitch_deg)):
            if not math.isfinite(value):
                raise ActionError(f"{name} must be a finite number of degrees")

        yaw_dpp, pitch_dpp = self.look_scale
        ref_w, ref_h = self._refresh_reference()
        # Straight to native pixels in one rounding step. Going via screenshot pixels
        # would round twice, and the second rounding is applied to a number the first
        # one already moved.
        dx = (yaw_deg / yaw_dpp) * self.screen.width / ref_w
        dy = (pitch_deg / pitch_dpp) * self.screen.height / ref_h

        def to_int(value: float, degrees: float, axis: str) -> int:
            out = _finite_delta(value, f"the {axis} turn")
            if out == 0 and degrees != 0:
                out = 1 if degrees > 0 else -1
            return out

        return to_int(dx, yaw_deg, "yaw"), to_int(dy, pitch_deg, "pitch")

    def _measure_pan(self, probe_native: int) -> tuple[int, float, int]:
        """Turn by a known amount and measure how far the image moved.

        Uses profiles rather than screenshots: the JPEG encode is most of the cost of
        a capture and produces nothing this needs.
        """
        ref_w, _ = self._refresh_reference()
        cfg = self.config
        before = self.screen.profile(cfg.max_width, cfg.max_height, cfg.capture_timeout_ms)
        _native.mouse_move_relative(probe_native, 0, 1)
        # The frame that shows the turn has to have been drawn before it can be
        # measured, and settle_ms is allowed to be 0 in tests.
        time.sleep(max(cfg.settle_ms, 50) / 1000.0)
        after = self.screen.profile(cfg.max_width, cfg.max_height, cfg.capture_timeout_ms)
        window = max(min(ref_w // 2, 400), 1)
        shift, confidence = _native.best_shift(before, after, window)
        return shift, confidence, window

    def autocalibrate_aim(self) -> int:
        """Works out the aim ratio by experiment. Returns native pixels already turned.

        This exists because the first version of `aim` required the model to run a
        calibration by hand first, and in an hour of real play it was used zero times
        out of two hundred and thirty-three calls -- the model kept saying "aim at
        the closest trunk" and then issuing a guessed pixel delta. A primitive with a
        setup ritual loses to one that works immediately, so this removes the ritual.

        The probe turns the view, which the caller must subtract from the turn it
        then makes; that is why the amount turned is returned rather than hidden.
        """
        turned = 0
        probe = AIM_PROBE_NATIVE_PX
        committed = False
        try:
            for _ in range(3):
                # Counted BEFORE the measurement, not after. _measure_pan moves the
                # mouse and then captures, so a capture that throws -- a DXGI device
                # loss is routine -- left the probe applied and unrecorded, and the
                # restore below had nothing to undo.
                turned += probe
                shift, confidence, window = self._measure_pan(probe)
                if confidence >= AIM_MIN_CONFIDENCE and abs(shift) >= AIM_MIN_SHIFT_PX:
                    if abs(shift) >= window - 1:
                        # Pinned at the edge of the search range, so the true peak is
                        # outside it. That is a lower bound on the movement, not a
                        # measurement of it, and accepting it would bake a wrong ratio
                        # in permanently. A large shift means a HIGH sensitivity, so
                        # the retry probes less, not more.
                        probe = max(AIM_MIN_SHIFT_PX, probe // 5)
                        continue
                    # Negative because the image moves opposite to the camera. Keeping
                    # the sign rather than the magnitude is what lets an inverted-axis
                    # setup calibrate to a negative ratio and aim the right way,
                    # instead of confidently aiming away from the target every time.
                    ratio = -probe / shift
                    if AIM_RATIO_BOUNDS[0] <= abs(ratio) <= AIM_RATIO_BOUNDS[1]:
                        self.set_aim_ratio(ratio)
                        committed = True
                        return turned
                # Too little movement means a low sensitivity, so probe further.
                probe = AIM_PROBE_RETRY_PX
        finally:
            # Whatever happened -- a capture failure mid-probe, the kill switch, a
            # measurement that could not be trusted -- the view must not be left
            # rotated by a probe the caller never asked for. On success the caller
            # subtracts `turned` from its own turn instead, so it is kept.
            if not committed and turned:
                with contextlib.suppress(Exception):
                    _native.mouse_move_relative(-turned, 0, 1)
        raise ActionError(
            "could not work out how the mouse maps to the view: turning the camera "
            f"{turned} pixels did not move the image measurably. This happens when "
            "the view is featureless (facing a wall or the sky) or when the "
            "application does not respond to relative mouse movement at all. Face "
            "something with visible detail and try again, or set the ratio yourself "
            "with calibrate."
        )

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

    def check_in_frame(self, x: float, y: float) -> None:
        """Rejects a coordinate that is not on the screenshot, moving nothing.

        Separate from aim_delta because aim calibrates before it maps, and
        calibrating turns the view: a bad coordinate used to leave the camera
        rotated by the probe and then blame the coordinate, with nothing undoing it.
        """
        ref_w, ref_h = self._refresh_reference()
        self._check_axis(x, ref_w, "x")
        self._check_axis(y, ref_h, "y")

    def set_aim_ratio(self, ratio: float) -> None:
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise ActionError("aim_ratio must be a number")
        if not math.isfinite(ratio) or ratio == 0:
            raise ActionError("aim_ratio must be a non-zero finite number")
        if not AIM_RATIO_BOUNDS[0] <= abs(ratio) <= AIM_RATIO_BOUNDS[1]:
            raise ActionError(
                f"aim_ratio {ratio} is outside the plausible range "
                f"{AIM_RATIO_BOUNDS[0]}..{AIM_RATIO_BOUNDS[1]}; one mouse pixel cannot "
                "pan the view by that much"
            )
        self.aim_ratio = float(ratio)

    def aim_delta(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot pixel -> native mouse delta that brings it to the crosshair.

        A pointer-locked game aims wherever the centre of the view points, so
        "click that" means "turn until that is in the middle, then click". Expressed
        in the same coordinates as every other action, so the model does no
        arithmetic and, more importantly, does not have to guess: guessing is what
        produces a turn, an overshoot, and a correction where one call would do.

        Only exactly linear near the centre -- a perspective projection is a tangent,
        not a scale -- so a target at the very edge lands slightly short. A second
        aim converges, and by then the target is near the centre where it is linear.
        """
        if self.aim_ratio is None:
            # Reachable again now that a mode change discards the calibration: the
            # aim action re-probes, but a direct caller has to be told rather than
            # multiplying by None.
            raise ActionError(
                "aim is not calibrated for the current display mode. The `aim` action "
                "measures this itself; call it rather than aim_delta."
            )
        ref_w, ref_h = self._refresh_reference()
        x = self._check_axis(x, ref_w, "x")
        y = self._check_axis(y, ref_h, "y")

        # The crosshair is the centre of the view, which is the centre of the image.
        offset_x = x - ref_w / 2.0
        offset_y = y - ref_h / 2.0

        return (_finite_delta(offset_x * self.aim_ratio, "the horizontal turn"),
                _finite_delta(offset_y * self.aim_ratio, "the vertical turn"))

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
        return Screenshot(shot.data, shot.width, shot.height, "image/jpeg")
