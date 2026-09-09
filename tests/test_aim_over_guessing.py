"""Naming the overshoot-and-correct loop at the moment it happens.

Taken from a real 25-minute Minecraft session: 25 `mouse_move_rel` calls against 4
`aim` calls, and the relative ones came in hunting runs -- turn 150 right, 180 back,
30 back again, 15 the other way -- each correction a separate round trip. The model
even wrote "Aim at nearest one and mine" and then issued a hand-guessed delta.

The cause was our own tool description, which told it in a Minecraft-titled section
to "switch to mouse_move_rel and stop reasoning about position" -- and `aim` is the
one action whose entire interface is a position. That prose is fixed. This covers
the second half: prose alone was already there and was not enough, so the result of
a correcting move now says so.
"""

from __future__ import annotations

from cufast.actions import run_batch


def rel(dx, dy):
    return {"action": "mouse_move_rel", "dx": dx, "dy": dy}


def text_of(session, actions):
    return " ".join(r.text or "" for r in run_batch(session, actions, False))


class TestACorrectionIsNamed:
    def test_reversing_the_last_move_is_pointed_out(self, session, fake_input):
        run_batch(session, [rel(150, -15)], False)
        out = text_of(session, [rel(-180, 25)])
        assert "overshot" in out
        assert "aim" in out

    def test_the_hint_quotes_the_move_it_reversed(self, session, fake_input):
        # Naming the actual numbers is the difference between a lecture and evidence.
        run_batch(session, [rel(100, 5)], False)
        out = text_of(session, [rel(-140, -5)])
        assert "(+100, +5)" in out

    def test_continuing_in_the_same_direction_is_not_a_correction(
        self, session, fake_input
    ):
        # Walking the view steadily one way is normal; only a reversal means the
        # previous delta was wrong.
        run_batch(session, [rel(-50, 0)], False)
        out = text_of(session, [rel(-30, 0)])
        assert "overshot" not in out

    def test_the_first_move_is_never_a_correction(self, session, fake_input):
        out = text_of(session, [rel(300, 0)])
        assert "overshot" not in out

    def test_only_the_dominant_axis_counts(self, session, fake_input):
        # (60, -10) then (40, 30): dx is dominant and kept its sign, so the flip on
        # the incidental y is noise, not a correction.
        run_batch(session, [rel(60, -10)], False)
        out = text_of(session, [rel(40, 30)])
        assert "overshot" not in out

    def test_a_vertical_reversal_is_caught_too(self, session, fake_input):
        run_batch(session, [rel(0, 100)], False)
        out = text_of(session, [rel(0, -200)])
        assert "overshot" in out


class TestAimEndsTheHunt:
    def test_aiming_clears_the_recorded_delta(self, session, fake_screen, fake_input):
        """`aim` is a measured turn, so a relative move after it is not correcting
        a guess and must not be accused of one."""
        run_batch(session, [rel(150, 0)], False)
        run_batch(session, [{"action": "aim", "coordinate": [300, 200]}], False)
        out = text_of(session, [rel(-40, 0)])
        assert "overshot" not in out


class TestTheGuidanceRoutesToAim:
    def test_the_contradictory_instruction_is_gone(self):
        # This exact line is what the session followed into 25 hand-guessed deltas.
        from cufast.server import TOOL_DESCRIPTION

        assert "stop reasoning about position" not in TOOL_DESCRIPTION

    def test_it_asks_whether_the_target_is_visible(self):
        from cufast.server import TOOL_DESCRIPTION

        assert "CAN YOU SEE THE THING YOU WANT TO LOOK AT?" in TOOL_DESCRIPTION

    def test_relative_movement_is_named_the_last_resort(self):
        from cufast.server import TOOL_DESCRIPTION

        assert "THE LAST RESORT, not the default" in TOOL_DESCRIPTION
