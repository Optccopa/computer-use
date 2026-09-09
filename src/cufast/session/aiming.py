"""Turning the view in a pointer-locked application, and measuring how to.

`aim` is the reason this exists: an earlier version made the model calibrate by
hand first and was used zero times in 233 calls, because a primitive with a setup
ritual loses to one that works immediately. So the measurement happens inside the
first aim and is folded into the turn it was asked for.
"""

from __future__ import annotations

import contextlib
import math
import time

from cufast import _native
from cufast.session.deltas import _finite_delta
from cufast.session.errors import ACTION_FAILURES, ActionError

# How far to turn when measuring the view's response to the mouse, tried in order
# until one measures. The first is big enough to clear the noise and small enough
# not to fling the camera somewhere unrecoverable.
#
# The small rung comes second on purpose. A failed measurement has two opposite
# causes and they look identical from here: the view barely moved (sensitivity too
# low to see) or it moved so far that nothing in the new frame correlates with the
# old one (sensitivity far too high). Only ever escalating assumes the first, and on
# a very sensitive setup turns a bad probe into a worse one -- an observed session
# escalated 120 -> 600 -> 600 and reported "turning the camera 1320 pixels did not
# move the image measurably" while facing a detailed forest, which is exactly what
# over-rotation looks like through a correlation window.
AIM_PROBE_LADDER = (120, 24, 600)
AIM_PROBE_NATIVE_PX = AIM_PROBE_LADDER[0]
# Below this the match is not distinguishable from the average candidate, which is
# what a featureless or repeating view produces. Accepting it would bake a wrong
# ratio into every later aim.
AIM_MIN_CONFIDENCE = 0.12
AIM_MIN_SHIFT_PX = 3
# A ratio outside this says the measurement is wrong rather than the sensitivity
# unusual: one native pixel of mouse cannot pan the view by twenty.
AIM_RATIO_BOUNDS = (0.02, 200.0)


class AimingMixin:
    """Calibration and the turns that use it."""

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

    def _measure(self, dx: int, dy: int, rows: bool, cap: int) -> tuple[int, float, int]:
        """Turn by a known amount and measure how far the image moved.

        Uses profiles rather than screenshots: the JPEG encode is most of the cost of
        a capture and produces nothing this needs. `rows` picks the axis -- a pitch
        change slides the image vertically, which a column profile cannot see at all
        because it sums over exactly the axis that moved.
        """
        ref_w, ref_h = self._refresh_reference()
        cfg = self.config
        profile = self.screen.profile_rows if rows else self.screen.profile
        before = profile(cfg.max_width, cfg.max_height, cfg.capture_timeout_ms)
        _native.mouse_move_relative(dx, dy, 1)
        # The frame that shows the turn has to have been drawn before it can be
        # measured, and settle_ms is allowed to be 0 in tests.
        time.sleep(max(cfg.settle_ms, 50) / 1000.0)
        after = profile(cfg.max_width, cfg.max_height, cfg.capture_timeout_ms)
        window = max(min((ref_h if rows else ref_w) // 2, cap), 1)
        shift, confidence = _native.best_shift(before, after, window)
        return shift, confidence, window

    def _measure_pan(self, probe_native: int) -> tuple[int, float, int]:
        return self._measure(probe_native, 0, rows=False, cap=400)

    def _measure_tilt(self, probe_native: int) -> tuple[int, float, int]:
        return self._measure(0, probe_native, rows=True, cap=300)

    def _calibrate_pitch(self) -> None:
        """Measures the vertical ratio, and leaves it unset if it cannot.

        Deliberately best-effort. Pitch clamps at plus or minus ninety degrees in
        most games, so a probe near the limit moves nothing however hard it is
        pushed, and a view with strong horizontal banding gives a poor vertical
        match. Falling back to the horizontal ratio is what the code did
        unconditionally before, so an unmeasurable axis is no worse than it was --
        and a measurable one is now right.
        """
        probe = AIM_PROBE_NATIVE_PX
        try:
            shift, confidence, window = self._measure_tilt(probe)
        except ACTION_FAILURES:
            self._undo(0, probe)
            return
        try:
            if (confidence >= AIM_MIN_CONFIDENCE and AIM_MIN_SHIFT_PX <= abs(shift)
                    < window - 1):
                ratio = -probe / shift
                if AIM_RATIO_BOUNDS[0] <= abs(ratio) <= AIM_RATIO_BOUNDS[1]:
                    self.aim_ratio_y = float(ratio)
        finally:
            self._undo(0, probe)

    def _undo(self, dx: int, dy: int) -> None:
        """Puts a probe back, without letting its own failure mask the real one."""
        if not dx and not dy:
            return
        # Suppressed on purpose: this runs while a real failure is already
        # propagating, and letting the undo's own error replace it would hide the
        # thing that actually went wrong.
        with contextlib.suppress(Exception):
            _native.mouse_move_relative(-dx, -dy, 1)

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
        probe = AIM_PROBE_LADDER[0]
        committed = False
        try:
            for attempt in range(len(AIM_PROBE_LADDER)):
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
                        # Measured rather than assumed equal, and undone, so the
                        # horizontal accounting the caller relies on still holds.
                        self._calibrate_pitch()
                        return turned
                # Nothing measurable. Take the next rung rather than assuming which
                # direction was wrong; the ladder covers both.
                probe = AIM_PROBE_LADDER[min(attempt + 1, len(AIM_PROBE_LADDER) - 1)]
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
            "the view is featureless (facing a wall or the sky), when the "
            "application does not respond to relative mouse movement at all, or "
            "when the view is so sensitive that the probe span it past anything "
            "recognisable. Face something with visible detail and try again. To "
            "set it by hand instead, use calibrate with AIM_RATIO -- that is the "
            "one `aim` needs, and look_degrees_per_pixel does not substitute for "
            "it. aim_ratio is native mouse pixels per screenshot pixel of "
            "on-screen movement: turn by a known amount, see how far the image "
            "shifted, and divide."
        )


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

        vertical = self.aim_ratio if self.aim_ratio_y is None else self.aim_ratio_y
        return (_finite_delta(offset_x * self.aim_ratio, "the horizontal turn"),
                _finite_delta(offset_y * vertical, "the vertical turn"))

