"""What happens when the monitor being controlled is unplugged.

This shipped broken and the failure was silent, which is the worst shape a bug in
this project can take. ensure_geometry re-resolved the display by device name, and
when no attached monitor matched it returned having changed nothing: duplication
stayed "ready" against an output that no longer existed, every acquire timed out,
and a timeout means "the screen is idle" -- so the cached frame from before the
unplug was served for the life of the process.

Nothing reported it. The content hash matched every time, so the harness said
"screen unchanged", and the model was handed an hour-old desktop as current. It
then measured coordinates off a display that was gone.

Observed, not theorised: a monitor was disconnected mid-session and the server kept
returning the same frame for an hour while a fresh process captured the real screen.

Marked desktop because it needs a real Screen. It unplugs nothing -- the seam only
makes one Capture object stop recognising its own monitor.
"""

from __future__ import annotations

import pytest

from cufast import _native
from cufast.config import Config
from cufast.session import Session

BOX = {"max_w": 200, "max_h": 200, "quality": 0.5, "draw_cursor": False, "timeout_ms": 0}


@pytest.mark.desktop
class TestALostDisplayIsReportedNotFaked:
    def test_grabbing_fails_instead_of_serving_the_last_frame(self):
        screen = _native.Screen(0)
        before = screen.grab(**BOX)
        assert before.width > 0

        screen._forget_display()
        with pytest.raises(RuntimeError, match="no longer attached"):
            screen.grab(**BOX)

    def test_the_message_says_how_to_recover(self):
        """The model has to be able to act on this without guessing.

        A bare failure reads as transient, and the response to a transient failure
        is to retry it forever against a display that is never coming back.
        """
        screen = _native.Screen(0)
        screen._forget_display()
        with pytest.raises(RuntimeError) as caught:
            screen.grab(**BOX)
        message = str(caught.value)
        assert "screen_info" in message
        assert "display" in message

    def test_it_keeps_failing_rather_than_recovering_on_its_own(self):
        # The guard in ensure_geometry short-circuits when the desktop layout looks
        # unchanged, so a one-shot failure that silently resumed serving the stale
        # frame on the next call would be the original bug with an extra step.
        screen = _native.Screen(0)
        screen._forget_display()
        for _ in range(3):
            with pytest.raises(RuntimeError, match="no longer attached"):
                screen.grab(**BOX)

    def test_the_session_surfaces_it_as_an_action_failure(self):
        """It has to reach the model as a readable error, not a crash.

        Native failures arrive through nanobind as RuntimeError, which the action
        layer already treats as a failed action rather than a bug.
        """
        from cufast.actions.limits import ACTION_FAILURES

        session = Session(Config(display_index=0, max_width=200, max_height=200,
                                 settle_ms=0))
        session.screenshot()
        session.screen._forget_display()
        with pytest.raises(ACTION_FAILURES):
            session.screenshot()
