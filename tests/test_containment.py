"""Limits on what the model can reach and how much it can ask for.

Not safety gates on what it may click -- driving the desktop is the whole point of
the tool. These are the boundaries of the tool itself: the display it was given, and
a batch that cannot consume the harness.
"""

from __future__ import annotations

import pytest

from cufast.actions import (
    MAX_ACTIONS_PER_BATCH,
    MAX_IMAGES_PER_BATCH,
    MAX_TYPE_CHARS,
    execute,
    run_batch,
    validate,
)
from cufast.session import ActionError


class TestRelativeMovementCannotLeaveTheDisplay:
    """The escape that absolute coordinates are checked for and relative was not.

    Demonstrated on the real machine before the fix: six mouse_move_rel of -400
    walked the cursor from (1188, 721) on the primary to (-1078, 725), which is on
    the second monitor. A click with no coordinate then lands there -- on a display
    the model was never given.
    """

    def test_walking_off_the_display_is_refused(self, session, fake_input):
        fake_input.cursor = (100, 540)
        with pytest.raises(ActionError, match="off display"):
            execute(session, "mouse_move_rel", {"dx": -400})

    def test_the_cursor_is_put_back_on_the_display(self, session, fake_input):
        fake_input.cursor = (100, 540)
        with pytest.raises(ActionError):
            execute(session, "mouse_move_rel", {"dx": -400})
        x, y = fake_input.cursor
        assert 0 <= x < 1920 and 0 <= y < 1080, "left off-display after refusing"

    def test_repeated_small_moves_cannot_creep_off(self, session, fake_input):
        # The real exploit was incremental: no single move looked unreasonable.
        fake_input.cursor = (300, 540)
        with pytest.raises(ActionError):
            for _ in range(10):
                execute(session, "mouse_move_rel", {"dx": -50})
        assert fake_input.cursor[0] >= 0

    def test_movement_inside_the_display_is_untouched(self, session, fake_input):
        fake_input.cursor = (960, 540)
        execute(session, "mouse_move_rel", {"dx": 100})
        assert fake_input.events[-1][0] == "mouse_move_relative"
        assert 0 <= fake_input.cursor[0] < 1920

    def test_a_pointer_locked_game_is_unaffected(self, session, fake_input):
        # The cursor is warped back to centre every frame, so it never travels and
        # the check never fires -- however far the view turns.
        fake_input.pointer_locked = True
        fake_input.cursor = (960, 540)
        for _ in range(20):
            execute(session, "mouse_move_rel", {"dx": -400})
        assert len(fake_input.events) == 20

    def test_aim_is_confined_too(self, session, fake_input):
        # Ratio kept well under the delta cap, so this exercises the display bound
        # rather than the "that number is not a movement" guard.
        session.set_aim_ratio(5.0)
        fake_input.cursor = (10, 540)
        with pytest.raises(ActionError, match="off display"):
            execute(session, "aim", {"coordinate": [0, 288]})

    def test_look_is_confined_too(self, session, fake_input):
        session.set_look_scale(0.05, 0.05)
        fake_input.cursor = (10, 540)
        with pytest.raises(ActionError, match="off display"):
            execute(session, "look", {"yaw": -30})

    def test_a_cursor_already_off_display_is_recovered_not_blamed(self, session,
                                                                 fake_input):
        # Someone else moved it there. Bringing it back is right; calling it the
        # model's fault and halting the batch is not.
        fake_input.cursor = (-500, 540)
        execute(session, "mouse_move_rel", {"dx": 10})
        assert 0 <= fake_input.cursor[0] < 1920


class TestBatchSizeIsBounded:
    def test_too_many_actions_is_refused(self, session):
        with pytest.raises(ActionError, match="over the"):
            run_batch(session, [{"action": "cursor_position"}] * (MAX_ACTIONS_PER_BATCH + 1))

    def test_the_limit_itself_is_allowed(self, session, fake_input):
        results = run_batch(session, [{"action": "cursor_position"}] * MAX_ACTIONS_PER_BATCH,
                            auto_screenshot=False)
        assert len(results) == MAX_ACTIONS_PER_BATCH

    def test_too_many_captures_is_refused(self, session):
        with pytest.raises(ActionError, match="captures"):
            run_batch(session, [{"action": "screenshot"}] * (MAX_IMAGES_PER_BATCH + 1),
                      auto_screenshot=False)

    def test_zoom_counts_as_a_capture(self, session):
        with pytest.raises(ActionError, match="captures"):
            run_batch(
                session,
                [{"action": "zoom", "region": [0, 0, 100, 100]}] * (MAX_IMAGES_PER_BATCH + 1),
                auto_screenshot=False,
            )

    def test_an_enormous_type_is_refused(self, session):
        with pytest.raises(ActionError, match="over the"):
            validate("type", {"text": "x" * (MAX_TYPE_CHARS + 1)})

    def test_a_normal_type_is_fine(self, session, fake_input):
        execute(session, "type", {"text": "hello world"})
        assert fake_input.events[-1] == ("type_text", "hello world")

    def test_the_limits_are_refusals_not_truncations(self, session, fake_input):
        # Silently doing less than asked would have the model believe it succeeded.
        with pytest.raises(ActionError):
            run_batch(session, [{"action": "cursor_position"}] * 500)
        assert fake_input.events == []


class TestTheModelCannotDisarmItsOwnStopButton:
    def test_there_is_no_action_that_unblocks_input(self):
        from cufast.actions import ACTION_NAMES

        for name in ACTION_NAMES:
            assert "block" not in name
            assert "kill" not in name

    def test_pressing_the_chord_itself_does_nothing(self, session, fake_input):
        # The hook ignores injected keystrokes, which is what stops the agent from
        # pressing its own stop button. Here the action simply goes through as a
        # normal keypress with no effect on the switch.
        execute(session, "key", {"text": "ctrl+Escape"})
        assert fake_input.events[-1][0] == "press_key"
