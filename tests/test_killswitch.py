"""The stop button.

This is the one feature whose failure mode is "the user cannot get their machine
back", so it is tested at both layers: the native hook lifecycle, and the refusal
the model actually sees.
"""

from __future__ import annotations

import ctypes

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cufast import _native
from cufast.actions import STOPPED_MESSAGE, run_batch
from cufast.config import Config
from cufast.server import Harness, build_server
from cufast.session import ActionError
from tests.conftest import FakeScreen
from tests.test_server import run

# Captured at import, before the autouse fixture swaps them for stubs. The stubs are
# what keeps a real system-wide hook out of the test process; these are the handful
# of tests that genuinely need the real thing.
NATIVE = {
    name: getattr(_native, name)
    for name in ("start_kill_switch", "stop_kill_switch", "kill_switch_running",
                 "kill_switch_trips", "input_blocked", "set_input_blocked",
                 "_trip_kill_switch")
}

_MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C, 0x01, 0x02, 0x04)


def nothing_is_physically_held() -> bool:
    """True when no modifier or mouse button is down on the real desktop.

    Engaging the switch releases whatever is held, which is the only path in the
    whole suite that could inject input. It injects nothing when nothing is held, so
    the tests that take that path check first rather than trusting it.
    """
    get_state = ctypes.windll.user32.GetAsyncKeyState
    return not any(get_state(vk) & 0x8000 for vk in _MODIFIER_VKS)


class TestNativeHook:
    def test_lifecycle_is_idempotent(self):
        assert not NATIVE["kill_switch_running"]()
        try:
            NATIVE["start_kill_switch"]()
            assert NATIVE["kill_switch_running"]()
            NATIVE["start_kill_switch"]()  # second call must not install a second hook
            assert NATIVE["kill_switch_running"]()
        finally:
            NATIVE["stop_kill_switch"]()
        assert not NATIVE["kill_switch_running"]()
        NATIVE["stop_kill_switch"]()  # and stopping twice must not hang or throw

    def test_trip_engages_then_releases(self):
        if not nothing_is_physically_held():
            pytest.skip("a key or button is physically held; skipping to avoid injecting")
        before = NATIVE["kill_switch_trips"]()
        try:
            NATIVE["_trip_kill_switch"]()
            assert NATIVE["input_blocked"]()
            assert NATIVE["kill_switch_trips"]() == before + 1

            # The same chord releases it, and releasing is not counted as a trip.
            NATIVE["_trip_kill_switch"]()
            assert not NATIVE["input_blocked"]()
            assert NATIVE["kill_switch_trips"]() == before + 1
        finally:
            NATIVE["set_input_blocked"](False)

    def test_injection_is_refused_while_engaged(self):
        if not nothing_is_physically_held():
            pytest.skip("a key or button is physically held; skipping to avoid injecting")
        try:
            NATIVE["set_input_blocked"](True)
            # press_key is the cheapest gated entry point. It must throw before it
            # reaches SendInput, so nothing lands on the desktop.
            with pytest.raises(RuntimeError, match="STOPPED BY THE USER"):
                _native.press_key("a", 1)
        finally:
            NATIVE["set_input_blocked"](False)


class TestBatchRefusal:
    def test_every_batch_is_refused_while_engaged(self, session, no_real_kill_switch):
        no_real_kill_switch["blocked"] = True
        with pytest.raises(ActionError, match="STOPPED BY THE USER"):
            run_batch(session, [{"action": "left_click", "coordinate": [10, 10]}])

    def test_a_screenshot_only_batch_is_refused_too(self, session, no_real_kill_switch):
        # Looking is harmless in itself, but a model that can still see keeps working
        # the problem. Stopping means stopping.
        no_real_kill_switch["blocked"] = True
        with pytest.raises(ActionError, match="STOPPED BY THE USER"):
            run_batch(session, [{"action": "screenshot"}])

    def test_the_message_tells_the_model_not_to_retry(self):
        assert "do not retry" in STOPPED_MESSAGE
        assert "Ctrl+Esc" in STOPPED_MESSAGE

    def test_normal_batches_run_once_released(self, session, no_real_kill_switch,
                                              fake_input):
        no_real_kill_switch["blocked"] = True
        with pytest.raises(ActionError):
            run_batch(session, [{"action": "screenshot"}])
        no_real_kill_switch["blocked"] = False
        results = run_batch(session, [{"action": "screenshot"}])
        assert not any(r.is_error for r in results)


class TestServerWiring:
    def test_harness_arms_it_on_construction(self, no_real_kill_switch, fake_screen,
                                             monkeypatch, fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        harness = Harness(Config(settle_ms=0))
        assert no_real_kill_switch["running"]
        harness.shutdown()
        assert not no_real_kill_switch["running"]

    def test_it_can_be_turned_off(self, no_real_kill_switch, fake_screen, monkeypatch,
                                  fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        Harness(Config(settle_ms=0, kill_switch=False))
        assert not no_real_kill_switch["running"]

    def test_tool_call_reports_the_stop(self, no_real_kill_switch, fake_screen,
                                        monkeypatch, fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(settle_ms=0))
        no_real_kill_switch["blocked"] = True
        with pytest.raises(ToolError, match="STOPPED BY THE USER"):
            run(server.call_tool("computer", {"actions": [{"action": "screenshot"}]}))

    @pytest.mark.parametrize(
        "state, expected",
        [
            ({"running": True, "blocked": False}, "Kill switch armed"),
            ({"running": True, "blocked": True}, "KILL SWITCH ENGAGED"),
            ({"running": False, "blocked": False}, "NOT running"),
        ],
    )
    def test_screen_info_reports_the_state(self, no_real_kill_switch, monkeypatch,
                                           fake_input, state, expected):
        monkeypatch.setattr(_native, "Screen", lambda index: FakeScreen(index=index),
                            raising=True)
        server = build_server(Config(settle_ms=0))
        no_real_kill_switch.update(state)
        text = run(server.call_tool("screen_info", {})).content[0].text
        assert expected in text
