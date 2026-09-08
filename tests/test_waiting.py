"""Waiting: the fixed sleep, and the one that ends when something happens.

Waiting is where a real session spent most of the time it was not spending on
round trips, and where the kill switch had its one remaining hole.
"""

from __future__ import annotations

import threading
import time

import pytest

from cufast import _native
from cufast.actions import STOPPED_MESSAGE, execute, run_batch, validate
from cufast.session import ActionError


class TestWaitIsInterruptible:
    """The stop button has to stop a wait that is already running.

    `wait` accepts up to five minutes and real sessions used it constantly, so a
    plain sleep meant pressing Ctrl+Esc could leave the harness asleep for the rest
    of it before anything noticed.
    """

    def test_a_long_wait_stops_when_the_switch_engages(self, session, no_real_kill_switch):
        def engage():
            time.sleep(0.15)
            no_real_kill_switch["blocked"] = True

        timer = threading.Thread(target=engage)
        started = time.monotonic()
        timer.start()
        try:
            with pytest.raises(ActionError, match="STOPPED BY THE USER"):
                execute(session, "wait", {"duration": 30})
        finally:
            timer.join()
        assert time.monotonic() - started < 5  # not the full thirty seconds

    def test_a_wait_that_is_not_interrupted_still_waits(self, session):
        started = time.monotonic()
        execute(session, "wait", {"duration": 0.2})
        assert 0.15 <= time.monotonic() - started < 2.0

    def test_zero_is_allowed_and_immediate(self, session):
        started = time.monotonic()
        execute(session, "wait", {"duration": 0})
        assert time.monotonic() - started < 0.5

    def test_it_refuses_to_start_while_engaged(self, session, no_real_kill_switch):
        no_real_kill_switch["blocked"] = True
        with pytest.raises(ActionError, match="STOPPED BY THE USER"):
            run_batch(session, [{"action": "wait", "duration": 1}])


class TestWaitForChange:
    def test_reports_how_long_it_took(self, session, fake_screen):
        fake_screen.change_after_ms = 340.0
        result = execute(session, "wait_for_change", {"duration": 5})
        assert "340" in result.text

    def test_a_timeout_says_nothing_happened(self, session, fake_screen):
        fake_screen.change_after_ms = -1.0
        result = execute(session, "wait_for_change", {"duration": 2})
        assert "nothing changed" in result.text
        # It must not be an error: "nothing happened" is a real answer, and failing
        # would discard the screenshot that shows why.
        assert not result.is_error

    def test_the_kill_switch_stops_it(self, session, fake_screen):
        fake_screen.change_after_ms = -2.0
        with pytest.raises(ActionError, match="STOPPED BY THE USER"):
            execute(session, "wait_for_change", {"duration": 30})

    def test_the_timeout_is_passed_through(self, session, fake_screen):
        execute(session, "wait_for_change", {"duration": 12.5})
        assert fake_screen.calls[-1] == {"wait_for_change": 12.5}

    def test_it_validates_like_wait(self):
        with pytest.raises(ActionError):
            validate("wait_for_change", {"duration": 10000})
        with pytest.raises(ActionError):
            validate("wait_for_change", {"duration": "a while"})

    @pytest.mark.parametrize("alias", ["wait_for_screen_change", "await_change"])
    def test_spellings(self, session, fake_screen, alias):
        assert execute(session, alias, {"duration": 1}).label == "wait_for_change"


class TestNativeWaitForChange:
    """Against the real display, which is where the blocking actually happens."""

    def test_a_still_screen_times_out_rather_than_returning_early(self):
        screen = _native.Screen(0)
        started = time.monotonic()
        waited = screen.wait_for_change(0.4)
        elapsed = time.monotonic() - started
        # Either it timed out (-1) having genuinely waited, or the desktop really did
        # change -- a clock ticking is enough. Both are correct; returning instantly
        # with -1 is not.
        if waited < 0:
            assert elapsed >= 0.35
        else:
            assert waited >= 0

    def test_it_returns_promptly_and_does_not_overrun(self):
        screen = _native.Screen(0)
        started = time.monotonic()
        screen.wait_for_change(0.3)
        assert time.monotonic() - started < 2.0

    def test_zero_timeout_returns_at_once(self):
        screen = _native.Screen(0)
        started = time.monotonic()
        assert screen.wait_for_change(0.0) == -1.0
        assert time.monotonic() - started < 1.0
