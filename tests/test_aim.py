"""Aiming and turning by angle, and the calibration both need.

These exist because of a measured failure. In a real Minecraft session the model
issued mouse_move_rel +113, then +188, then -281: it turned, overshot, and came
back. Three round trips at about nine seconds each for one decision, because a
pixel delta cannot express "put that on the crosshair" and so has to be guessed.
"""

from __future__ import annotations

import pytest

from cufast.actions import execute, validate
from cufast.session import ActionError


@pytest.fixture
def aimed(session):
    session.set_aim_ratio(2.0)
    return session


class TestAutoCalibration:
    """aim must work on first use, with no setup.

    The first version required the model to calibrate by hand first. In an hour of
    real play it was used zero times in 233 calls: the model kept narrating "aim at
    the closest trunk" and then issuing a guessed pixel delta. A primitive with a
    setup ritual loses to one that works immediately.
    """

    def test_aim_works_with_no_calibration_at_all(self, session, fake_input):
        assert session.aim_ratio is None
        result = execute(session, "aim", {"coordinate": [700, 288]})
        assert session.aim_ratio is not None
        assert "calibrated itself" in result.text

    def test_it_recovers_the_true_ratio(self, session, fake_screen):
        fake_screen.pan_ratio = 3.5
        session.autocalibrate_aim()
        assert session.aim_ratio == pytest.approx(3.5, rel=0.05)

    def test_it_returns_how_far_it_turned(self, session):
        from cufast.session import AIM_PROBE_NATIVE_PX

        assert session.autocalibrate_aim() == AIM_PROBE_NATIVE_PX

    def test_the_probe_is_subtracted_from_the_aim(self, session, fake_screen, fake_input):
        # The probe turns the view, and the coordinate was given against the frame
        # before it, so that much of the turn is already done. Aiming the full amount
        # on top would overshoot by exactly the probe.
        from cufast.session import AIM_PROBE_NATIVE_PX

        fake_screen.pan_ratio = 2.0
        execute(session, "aim", {"coordinate": [712, 288]})
        moves = [e for e in fake_input.events if e[0] == "mouse_move_relative"]
        assert sum(m[1] for m in moves) == 400  # (712-512) * 2.0, probe included

    def test_it_only_calibrates_once(self, session, fake_input):
        execute(session, "aim", {"coordinate": [600, 288]})
        first = session.aim_ratio
        before = len(fake_input.events)
        result = execute(session, "aim", {"coordinate": [600, 288]})
        assert session.aim_ratio == first
        assert "calibrated itself" not in result.text
        assert len(fake_input.events) - before == 1  # just the aim, no probe

    def test_an_explicit_calibration_skips_the_probe(self, session, fake_input):
        session.set_aim_ratio(2.0)
        execute(session, "aim", {"coordinate": [712, 288]})
        assert fake_input.events == [("mouse_move_relative", 400, 0, 1)]

    def test_a_featureless_view_is_reported_not_guessed(self, session, fake_screen,
                                                        monkeypatch):
        # A flat profile matches equally well at every shift. Believing it would bake
        # a wrong ratio into every later aim, so it must fail loudly instead.
        monkeypatch.setattr(fake_screen, "profile",
                            lambda max_w, max_h, timeout_ms=16: [128] * 1024)
        with pytest.raises(ActionError, match="featureless|did not move"):
            session.autocalibrate_aim()
        assert session.aim_ratio is None

    def test_a_failed_calibration_puts_the_view_back(self, session, fake_screen,
                                                     fake_input, monkeypatch):
        monkeypatch.setattr(fake_screen, "profile",
                            lambda max_w, max_h, timeout_ms=16: [128] * 1024)
        with pytest.raises(ActionError):
            session.autocalibrate_aim()
        moves = [e for e in fake_input.events if e[0] == "mouse_move_relative"]
        assert sum(m[1] for m in moves) == 0  # every probe undone


class TestCalibrationIsRequired:
    def test_look_without_calibration_explains_how(self, session):
        with pytest.raises(ActionError, match="calibration"):
            session.look_delta(30, 0)

    def test_the_message_names_the_action_that_fixes_it(self, session):
        with pytest.raises(ActionError, match="calibrate"):
            session.aim_delta(100, 100)

    @pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
    def test_rejects_a_nonsense_ratio(self, session, bad):
        with pytest.raises(ActionError):
            session.set_aim_ratio(bad)

    def test_catches_a_reciprocal(self, session):
        # Degrees per pixel and pixels per degree are easy to swap, and swapping
        # them turns a 30 degree turn into several full revolutions.
        with pytest.raises(ActionError, match="reciprocal"):
            session.set_look_scale(200.0, 200.0)


class TestAimGeometry:
    def test_the_centre_needs_no_movement(self, aimed):
        # 1024x576 screenshot, so the crosshair is at (512, 288).
        assert aimed.aim_delta(512, 288) == (0, 0)

    def test_left_of_centre_turns_left(self, aimed):
        dx, dy = aimed.aim_delta(312, 288)
        assert dx < 0 and dy == 0

    def test_below_centre_turns_down(self, aimed):
        dx, dy = aimed.aim_delta(512, 388)
        assert dx == 0 and dy > 0

    def test_the_delta_is_the_offset_times_the_ratio(self, aimed):
        # 200 screenshot pixels right of centre, ratio 2.0.
        assert aimed.aim_delta(712, 288) == (400, 0)

    def test_it_is_symmetric(self, aimed):
        left = aimed.aim_delta(312, 188)
        right = aimed.aim_delta(712, 388)
        assert left == (-right[0], -right[1])

    def test_it_rejects_a_coordinate_off_the_screenshot(self, aimed):
        with pytest.raises(ActionError, match="outside the screenshot"):
            aimed.aim_delta(5000, 288)

    def test_it_follows_a_mode_change(self, aimed, fake_screen):
        # The crosshair is the centre of whatever image was last delivered, so a
        # rotation must move it rather than leaving every aim biased.
        fake_screen.resize(1080, 1920)  # 432x768, centre (216, 384)
        assert aimed.aim_delta(216, 384) == (0, 0)


class TestLookGeometry:
    @pytest.fixture
    def looking(self, session):
        session.set_look_scale(0.05, 0.05)  # the value measured in Minecraft
        return session

    def test_positive_yaw_turns_right(self, looking):
        dx, dy = looking.look_delta(30, 0)
        assert dx > 0 and dy == 0

    def test_positive_pitch_turns_down(self, looking):
        dx, dy = looking.look_delta(0, 15)
        assert dx == 0 and dy > 0

    def test_degrees_convert_through_the_calibration(self, looking):
        # 30 degrees at 0.05 deg per screenshot pixel is 600 screenshot pixels,
        # and 1920/1024 makes that 1125 native.
        assert looking.look_delta(30, 0) == (1125, 0)

    def test_it_is_linear(self, looking):
        one = looking.look_delta(10, 0)[0]
        assert looking.look_delta(20, 0)[0] == pytest.approx(2 * one, abs=1)

    def test_a_turn_and_its_opposite_cancel(self, looking):
        assert looking.look_delta(-45, -20) == tuple(-v for v in looking.look_delta(45, 20))

    def test_a_tiny_angle_still_moves(self, looking):
        # Rounding a requested turn to zero is indistinguishable from the action
        # not working, and the model's next move is to issue it again, larger.
        dx, _ = looking.look_delta(0.0001, 0)
        assert dx == 1

    def test_rejects_non_finite(self, looking):
        with pytest.raises(ActionError, match="finite"):
            looking.look_delta(float("nan"), 0)


class TestActions:
    def test_aim_sends_the_computed_delta(self, aimed, fake_input):
        execute(aimed, "aim", {"coordinate": [712, 288]})
        assert fake_input.events == [("mouse_move_relative", 400, 0, 1)]

    def test_look_sends_the_computed_delta(self, session, fake_input):
        session.set_look_scale(0.05, 0.05)
        execute(session, "look", {"yaw": 30})
        assert fake_input.events == [("mouse_move_relative", 1125, 0, 1)]

    def test_calibrate_sets_both_and_reports_them(self, session):
        result = execute(
            session, "calibrate",
            {"aim_ratio": 1.75, "look_degrees_per_pixel": 0.05},
        )
        assert session.aim_ratio == 1.75
        assert session.look_scale == (0.05, 0.05)
        assert "1.75" in result.text and "0.05" in result.text

    def test_calibrate_can_set_just_one(self, session):
        execute(session, "calibrate", {"aim_ratio": 2.0})
        assert session.aim_ratio == 2.0
        assert session.look_scale is None

    def test_calibration_persists_across_actions(self, aimed, fake_input):
        execute(aimed, "aim", {"coordinate": [612, 288]})
        execute(aimed, "aim", {"coordinate": [612, 288]})
        assert len(fake_input.events) == 2
        assert fake_input.events[0] == fake_input.events[1]

    def test_aim_reports_what_it_did(self, aimed, fake_input):
        result = execute(aimed, "aim", {"coordinate": [712, 288]})
        assert "+400" in result.text

    @pytest.mark.parametrize("params", [{}, {"coordinate": [1, 2, 3]}, {"coordinate": "middle"}])
    def test_aim_rejects_bad_arguments(self, params):
        with pytest.raises(ActionError):
            validate("aim", params)

    @pytest.mark.parametrize("params", [{}, {"yaw": "left"}, {"yaw": 10, "steps": 0}])
    def test_look_rejects_bad_arguments(self, params):
        with pytest.raises(ActionError):
            validate("look", params)

    def test_calibrate_requires_something_to_set(self):
        with pytest.raises(ActionError, match="requires"):
            validate("calibrate", {})


class TestAliases:
    """Misspellings that cost a whole round trip each when rejected.

    Both of these were actually issued in the observed session.
    """

    def test_keydown_is_accepted(self, session, fake_input):
        execute(session, "keydown", {"text": "w"})
        assert fake_input.held == ["w"]

    def test_keyup_is_accepted(self, session, fake_input):
        execute(session, "key_down", {"text": "w"})
        execute(session, "keyup", {"text": "w"})
        assert fake_input.held == []

    @pytest.mark.parametrize("alias", ["mouse_move_relative", "move_rel", "rel_move"])
    def test_relative_move_spellings(self, session, fake_input, alias):
        execute(session, alias, {"dx": 100})
        assert fake_input.events[-1][0] == "mouse_move_relative"

    @pytest.mark.parametrize("alias, real", [
        ("mouse_down", "mouse_down"), ("mouse_up", "mouse_up"), ("click", "mouse_click"),
    ])
    def test_click_spellings(self, session, fake_input, alias, real):
        # mouse_down was issued for real and rejected. The click actions carry no
        # "left_" prefix while the button ones do, so dropping it is a fair mistake.
        execute(session, alias, {})
        assert fake_input.events[-1][0] == real

    def test_an_unknown_action_still_fails(self):
        with pytest.raises(ActionError, match="unknown action"):
            validate("teleport", {})
