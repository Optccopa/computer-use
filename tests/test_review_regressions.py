"""One test per defect found in the second review round.

Each of these failed before the fix. They are collected here rather than scattered
so the next round can see at a glance what was already paid for.
"""

from __future__ import annotations

import math

import pytest

from cufast import _native
from cufast.actions import MAX_BATCH_DURATION_SECONDS, execute, run_batch
from cufast.config import Config
from cufast.server import Harness
from cufast.session import MAX_NATIVE_DELTA, ActionError


class TestAimDoesNotHalfApply:
    """aim probed the view before checking the coordinate it was given.

    A coordinate off the screenshot left the camera rotated by the probe and then
    raised an error blaming the coordinate, with nothing undoing the turn.
    """

    def test_an_off_frame_coordinate_moves_nothing(self, session, fake_input):
        with pytest.raises(ActionError, match="outside the screenshot"):
            execute(session, "aim", {"coordinate": [5000, 288]})
        assert fake_input.events == []
        assert session.aim_ratio is None

    def test_a_capture_failure_mid_probe_restores_the_view(self, session, fake_screen,
                                                           fake_input, monkeypatch):
        calls = {"n": 0}
        real = fake_screen.profile

        def flaky(max_w, max_h, timeout_ms=16):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("DXGI: device lost")
            return real(max_w, max_h, timeout_ms)

        monkeypatch.setattr(fake_screen, "profile", flaky)
        with pytest.raises(RuntimeError, match="device lost"):
            session.autocalibrate_aim()
        moves = [e for e in fake_input.events if e[0] == "mouse_move_relative"]
        assert sum(m[1] for m in moves) == 0, "the probe was left applied"

    def test_a_rejected_measurement_restores_the_view(self, session, fake_screen,
                                                      fake_input, monkeypatch):
        monkeypatch.setattr(fake_screen, "profile",
                            lambda max_w, max_h, timeout_ms=16: [77] * 1024)
        with pytest.raises(ActionError):
            session.autocalibrate_aim()
        moves = [e for e in fake_input.events if e[0] == "mouse_move_relative"]
        assert sum(m[1] for m in moves) == 0


class TestOversizedNumbersAreActionErrors:
    """Arithmetic on model-supplied numbers escaped every handler.

    dx=1e308 overflowed to inf and raised OverflowError; a delta past 2^31 came back
    as a nanobind TypeError. Neither was in ACTION_FAILURES, so the whole batch
    result was destroyed -- including screenshots that had already succeeded.
    """

    @pytest.mark.parametrize("dx", [1e308, 2e9, -2e9, 1e15])
    def test_a_huge_delta_is_reported_not_raised(self, session, dx):
        with pytest.raises(ActionError):
            execute(session, "mouse_move_rel", {"dx": dx})

    def test_the_batch_survives_and_keeps_its_screenshot(self, session, fake_input):
        results = run_batch(
            session,
            [{"action": "screenshot"}, {"action": "mouse_move_rel", "dx": 1e308}],
            auto_screenshot=False,
        )
        assert results[0].image is not None
        assert results[1].is_error
        # The cause has to survive; "Error executing tool computer" tells the model
        # nothing and it will simply try again.
        assert "limit" in results[1].text or "not a movement" in results[1].text

    def test_a_huge_calibration_cannot_be_committed(self, session):
        with pytest.raises(ActionError):
            execute(session, "calibrate", {"aim_ratio": 1e300})
        assert session.aim_ratio is None

    def test_the_bound_matches_what_the_native_side_accepts(self, session):
        session.set_aim_ratio(200.0)
        with pytest.raises(ActionError, match="limit"):
            session.aim_delta(1023, 288)
        assert MAX_NATIVE_DELTA == 100_000


class TestCursorReportsItsOwnPixel:
    """cursor_position used a rescale, not the inverse of the downscaler's spans.

    Roughly half of all native columns reported the neighbouring screenshot pixel,
    always biased low.
    """

    @pytest.mark.parametrize("native_x", [0, 1, 2, 3, 4, 5, 959, 960, 1918, 1919])
    def test_the_reported_pixel_contains_the_cursor(self, session, fake_input, native_x):
        fake_input.cursor = (native_x, 540)
        x, _, on_display = session.cursor_in_screenshot_space()
        assert on_display
        start = (x * 1920) // session.ref_width
        end = ((x + 1) * 1920) // session.ref_width
        assert start <= native_x < max(end, start + 1)

    def test_every_column_of_a_4k_display_lands_in_its_own_pixel(self, monkeypatch,
                                                                 fake_input):
        from tests.conftest import FakeScreen

        import cufast.session as session_mod

        screen = FakeScreen(width=3840, height=2160)
        monkeypatch.setattr(_native, "Screen", lambda index: screen, raising=True)
        s = session_mod.Session(Config(max_width=1024, max_height=768, settle_ms=0))
        for native_x in range(0, 3840, 7):
            fake_input.cursor = (native_x, 100)
            x, _, _ = s.cursor_in_screenshot_space()
            start = (x * 3840) // s.ref_width
            end = ((x + 1) * 3840) // s.ref_width
            assert start <= native_x < max(end, start + 1), f"column {native_x} -> {x}"


class TestModifiersAreCheckedBeforeMoving:
    """The chord was parsed inside the click, after the move had already fired."""

    def test_a_bad_chord_moves_nothing(self, session, fake_input):
        with pytest.raises(RuntimeError, match="unknown key"):
            execute(session, "left_click",
                    {"coordinate": [600, 400], "text": "ctrl+zzz"})
        assert fake_input.events == []

    def test_a_bad_scroll_chord_moves_nothing(self, session, fake_input):
        with pytest.raises(RuntimeError, match="unknown key"):
            execute(session, "scroll", {
                "coordinate": [600, 400], "scroll_direction": "down",
                "scroll_amount": 3, "text": "nonsensekey",
            })
        assert fake_input.events == []

    def test_a_good_chord_still_works(self, session, fake_input):
        execute(session, "left_click", {"coordinate": [600, 400], "text": "ctrl+shift"})
        assert [e[0] for e in fake_input.events] == ["mouse_move", "mouse_click"]


class TestBatchWallTimeIsBounded:
    """One action was capped; the batch was not.

    200 waits of 300s would hold the single native worker for sixteen hours, and
    screen_info runs on that same worker, so nothing could even report it.
    """

    def test_a_batch_that_waits_too_long_is_refused(self, session):
        with pytest.raises(ActionError, match="occupy the harness"):
            run_batch(session, [{"action": "wait", "duration": 300}] * 3)

    def test_it_counts_holds_and_change_waits_too(self, session):
        with pytest.raises(ActionError, match="occupy the harness"):
            run_batch(session, [
                {"action": "hold_key", "text": "w", "duration": 300},
                {"action": "wait_for_change", "duration": 300},
                {"action": "wait", "duration": 100},
            ])

    def test_a_reasonable_batch_is_allowed(self, session, fake_input):
        results = run_batch(session, [{"action": "wait", "duration": 0}] * 4,
                            auto_screenshot=False)
        assert not any(r.is_error for r in results)
        assert MAX_BATCH_DURATION_SECONDS == 600.0


class TestShutdownStopsTheBatchFirst:
    """shutdown() disarmed the switch while the worker was still injecting.

    A 300s wait could then never be interrupted, ran to completion, and pressed its
    key AFTER the release had already happened.
    """

    def test_input_is_blocked_before_the_worker_is_joined(self, no_real_kill_switch,
                                                          fake_screen, monkeypatch,
                                                          fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        order: list[str] = []
        monkeypatch.setattr(_native, "set_input_blocked",
                            lambda b: order.append(f"block={b}"), raising=True)
        monkeypatch.setattr(_native, "stop_kill_switch",
                            lambda: order.append("stop_hook"), raising=True)
        monkeypatch.setattr(_native, "release_held_input",
                            lambda: order.append("release"), raising=True)
        Harness(Config(settle_ms=0)).shutdown()
        assert order.index("block=True") < order.index("stop_hook")
        assert order.index("stop_hook") < order.index("release")
        # And it must not leave the switch latched for the next process to inherit.
        assert order[-1] == "block=False"


class TestCalibrationRefusesALowerBound:
    """A shift pinned at the edge of the search window is not a measurement.

    Accepting it baked a wrong ratio in permanently, and the old retry probed
    FURTHER, pushing a high-sensitivity peak even further outside the window.
    """

    def test_a_shift_at_the_window_edge_is_rejected_and_reprobed(self, session,
                                                                  monkeypatch):
        # The old code accepted it as a measurement and baked the wrong ratio in for
        # good; worse, its retry probed FURTHER, pushing a high-sensitivity peak even
        # further outside the window, so there was no path that recovered.
        probes: list[int] = []

        def fake_measure(probe):
            probes.append(probe)
            window = 400
            if len(probes) == 1:
                return -window, 0.9, window       # pinned: a lower bound, not a value
            return -80, 0.9, window

        monkeypatch.setattr(session, "_measure_pan", fake_measure)
        session.autocalibrate_aim()
        assert probes[1] < probes[0], "the retry must probe less, not more"
        assert session.aim_ratio == pytest.approx(probes[1] / 80, rel=0.01)

    def test_a_normal_measurement_gives_a_positive_ratio(self, session, monkeypatch):
        # b[x] = a[x+k] makes best_shift return -k, and a camera turning right moves
        # the image left, so a conventional setup measures a negative shift.
        monkeypatch.setattr(session, "_measure_pan", lambda probe: (-60, 0.9, 400))
        session.autocalibrate_aim()
        assert session.aim_ratio > 0

    def test_an_inverted_axis_gives_a_negative_ratio(self, session, monkeypatch):
        monkeypatch.setattr(session, "_measure_pan", lambda probe: (60, 0.9, 400))
        session.autocalibrate_aim()
        assert session.aim_ratio < 0
        assert math.isfinite(session.aim_ratio)


class TestCalibrationDoesNotSurviveAModeChange:
    """Both calibrations are measured against a specific screenshot size.

    Neither failure raised; both just turned the wrong amount, forever.
    """

    def test_look_scale_is_discarded(self, session, fake_screen):
        session.set_look_scale(0.05, 0.05)
        fake_screen.resize(1280, 720)
        session._refresh_reference()
        assert session.look_scale is None

    def test_aim_ratio_is_discarded(self, session, fake_screen):
        session.set_aim_ratio(2.0)
        fake_screen.resize(1080, 1920)
        session._refresh_reference()
        assert session.aim_ratio is None

    def test_the_first_reference_is_not_treated_as_a_change(self, session):
        # Construction computes the reference for the first time; wiping a
        # calibration set immediately afterwards would make aim uncalibratable.
        session.set_aim_ratio(3.0)
        session._refresh_reference()
        assert session.aim_ratio == 3.0

    def test_aim_recalibrates_itself_after_a_change(self, session, fake_screen,
                                                    fake_input):
        execute(session, "aim", {"coordinate": [600, 288]})
        assert session.aim_ratio is not None
        fake_screen.resize(1080, 1920)
        result = execute(session, "aim", {"coordinate": [200, 384]})
        # It must measure again rather than reusing a ratio for the old geometry.
        assert "calibrated itself" in result.text


class TestPitchIsMeasuredNotAssumed:
    """aim applied the horizontally-measured ratio to dy.

    A game with invert-Y is then wrong on pitch from the very first aim, and one
    with separate per-axis sensitivity is wrong by a constant factor. Neither
    raises; both just aim at the wrong height.
    """

    def test_the_vertical_ratio_is_measured_separately(self, session, fake_screen):
        fake_screen.pan_ratio = 2.0
        fake_screen.tilt_ratio = 4.0
        session.autocalibrate_aim()
        assert session.aim_ratio == pytest.approx(2.0, rel=0.1)
        assert session.aim_ratio_y == pytest.approx(4.0, rel=0.1)

    def test_invert_y_gets_the_opposite_sign(self, session, fake_screen):
        fake_screen.pan_ratio = 2.0
        fake_screen.tilt_ratio = -2.0
        session.autocalibrate_aim()
        assert session.aim_ratio > 0
        assert session.aim_ratio_y < 0

    def test_aim_uses_the_vertical_ratio_for_dy(self, session):
        session.set_aim_ratio(2.0)
        session.aim_ratio_y = -3.0
        dx, dy = session.aim_delta(612, 388)  # +100 x, +100 y from centre
        assert dx == 200
        assert dy == -300

    def test_it_falls_back_when_the_axis_cannot_be_measured(self, session, fake_screen,
                                                            monkeypatch):
        # Pitch clamps at +/-90 in most games, so a probe near the limit moves
        # nothing. Falling back to the horizontal ratio is what the code did
        # unconditionally before, so this is no worse -- it must not fail the aim.
        monkeypatch.setattr(session, "_measure_tilt", lambda probe: (0, 0.0, 300))
        session.autocalibrate_aim()
        assert session.aim_ratio is not None
        assert session.aim_ratio_y is None
        dx, dy = session.aim_delta(612, 388)
        assert dx > 0 and dy > 0

    def test_the_pitch_probe_is_undone(self, session, fake_screen, fake_input):
        session.autocalibrate_aim()
        vertical = sum(e[2] for e in fake_input.events if e[0] == "mouse_move_relative")
        assert vertical == 0, "the vertical probe was left applied"

    def test_a_mode_change_discards_it_too(self, session, fake_screen):
        session.autocalibrate_aim()
        fake_screen.resize(1280, 720)
        session._refresh_reference()
        assert session.aim_ratio_y is None
