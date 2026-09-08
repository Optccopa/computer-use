"""Relative mouse movement and held keys -- what 3D games need.

A pointer-locked application hides the cursor and warps it back to the window
centre every frame, so it has no position to be moved to and reads deltas instead.
Everything here exists because absolute positioning cannot express that.
"""

from __future__ import annotations

import pytest

from cufast import _native
from cufast.actions import _MUTATING, execute, run_batch, validate
from cufast.session import ActionError


class TestStepPlanExactness:
    """The steps must sum to exactly the requested delta.

    A calibrated turn is issued over and over. Losing a pixel per call to rounding
    is not a rounding error in practice, it is an aim that drifts a little further
    off target every time.
    """

    @pytest.mark.parametrize("delta", [1, 2, 3, 7, 10, 33, 100, 359, 1000, -1, -7, -333])
    @pytest.mark.parametrize("steps", [1, 2, 3, 5, 8, 16, 60])
    def test_sums_to_the_requested_delta(self, delta, steps):
        plan = _native._relative_step_plan(delta, -delta, steps)
        assert sum(x for x, _ in plan) == delta
        assert sum(y for _, y in plan) == -delta

    def test_zero_delta_sends_nothing(self):
        assert _native._relative_step_plan(0, 0, 10) == []

    def test_a_single_step_is_one_event(self):
        assert _native._relative_step_plan(120, -40, 1) == [(120, -40)]

    def test_steps_are_evenly_sized(self):
        # 100 over 4 steps should be four 25s, not three 33s and a 1.
        assert _native._relative_step_plan(100, 0, 4) == [(25, 0)] * 4

    def test_more_steps_than_pixels_does_not_produce_empty_events(self):
        plan = _native._relative_step_plan(3, 0, 60)
        assert sum(x for x, _ in plan) == 3
        assert all(step != (0, 0) for step in plan)


class TestDeltaScaling:
    def test_scales_screenshot_pixels_to_native(self, session):
        # 1920x1080 behind a 1024x576 screenshot is exactly 1.875x.
        assert session.scale_delta(100, 0) == (188, 0)
        assert session.scale_delta(0, -80) == (0, -150)

    def test_a_nudge_never_rounds_to_nothing(self, session):
        # A model asking to turn by one pixel and getting zero movement has no way
        # to tell that apart from the action not working at all.
        for value in (1, -1, 0.4, -0.4):
            dx, _ = session.scale_delta(value, 0)
            assert dx != 0
            assert (dx > 0) == (value > 0)

    def test_zero_stays_zero(self, session):
        assert session.scale_delta(0, 0) == (0, 0)

    def test_rounds_to_nearest_rather_than_outward(self, session):
        # 53 * 1.875 = 99.375. Rounding outward would make every calibrated turn
        # slightly long, and a turn gets issued over and over.
        assert session.scale_delta(53, 0) == (99, 0)
        assert session.scale_delta(-53, 0) == (-99, 0)
        # Exact halves must go the same way every time, so this cannot use
        # banker's rounding: 60 * 1.875 = 112.5.
        assert session.scale_delta(60, 0) == (113, 0)
        assert session.scale_delta(-60, 0) == (-113, 0)

    def test_rejects_non_finite(self, session):
        with pytest.raises(ActionError, match="finite"):
            session.scale_delta(float("inf"), 0)

    def test_follows_a_mode_change(self, session, fake_screen):
        before = session.scale_delta(100, 0)
        fake_screen.resize(1080, 1920)  # portrait: 432x768, so 2.5x
        assert session.scale_delta(100, 0) != before
        assert session.scale_delta(100, 0) == (250, 0)


class TestRelativeAction:
    def test_sends_a_relative_move(self, session, fake_input):
        execute(session, "mouse_move_rel", {"dx": 100, "dy": -40})
        assert fake_input.events == [("mouse_move_relative", 188, -75, 1)]

    def test_steps_pass_through(self, session, fake_input):
        execute(session, "mouse_move_rel", {"dx": 10, "dy": 0, "steps": 5})
        assert fake_input.events[-1][3] == 5

    def test_reports_the_native_delta(self, session, fake_input):
        # The model calibrates against what actually moved, so the reply has to say
        # what that was rather than echoing the request back.
        result = execute(session, "mouse_move_rel", {"dx": 100, "dy": 0})
        assert "+188" in result.text

    def test_one_axis_is_enough(self, session, fake_input):
        execute(session, "mouse_move_rel", {"dx": 50})
        assert fake_input.events[-1][2] == 0

    def test_requires_at_least_one_axis(self):
        with pytest.raises(ActionError, match="requires dx"):
            validate("mouse_move_rel", {})

    @pytest.mark.parametrize("params", [
        {"dx": "left"},
        {"dx": 10, "steps": 0},
        {"dx": 10, "steps": 1001},
        {"dx": 10, "steps": 2.5},
        {"dx": True},
    ])
    def test_rejects_bad_arguments(self, params):
        with pytest.raises(ActionError):
            validate("mouse_move_rel", params)

    def test_it_settles_before_a_screenshot(self):
        # Turning the camera and screenshotting the pre-turn frame would have the
        # model conclude the move did nothing and issue it again.
        assert "mouse_move_rel" in _MUTATING


class TestHeldKeys:
    def test_key_down_holds_and_key_up_releases(self, session, fake_input):
        execute(session, "key_down", {"text": "w"})
        assert fake_input.held == ["w"]
        execute(session, "key_up", {"text": "w"})
        assert fake_input.held == []

    def test_the_result_says_what_is_held(self, session, fake_input):
        down = execute(session, "key_down", {"text": "w"})
        assert "w" in down.text
        up = execute(session, "key_up", {"text": "w"})
        assert "nothing" in up.text

    def test_a_key_survives_across_batches(self, session, fake_input):
        # The whole point: walk forward in one call, turn in the next.
        run_batch(session, [{"action": "key_down", "text": "w"}], auto_screenshot=False)
        run_batch(session, [{"action": "mouse_move_rel", "dx": 60}], auto_screenshot=False)
        assert fake_input.held == ["w"]
        assert ("mouse_move_relative", 113, 0, 1) in fake_input.events

    def test_several_keys_at_once(self, session, fake_input):
        for chord in ("w", "shift", "space"):
            execute(session, "key_down", {"text": chord})
        assert fake_input.held == ["w", "shift", "space"]

    def test_both_require_a_key(self):
        for name in ("key_down", "key_up"):
            with pytest.raises(ActionError):
                validate(name, {})


class TestNativeRegistry:
    """The registry the kill switch and shutdown use to unstick the desktop.

    Exercised through the real native module rather than the recorder, because the
    thing being tested is that the C++ side remembers what it pressed.
    """

    def test_held_keys_starts_empty(self):
        # Nothing in the suite may leave a key down; the recorder is what the other
        # tests use precisely so this stays true.
        assert _native.held_keys() == []
