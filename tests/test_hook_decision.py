"""The decision the keyboard hook makes about one Ctrl+Esc.

Untestable through SendInput on purpose: the hook ignores injected keystrokes, which
is what stops the agent from pressing its own stop button. So the decision is split
out and driven directly.

Mutation testing is what said these were needed -- putting the auto-repeat bug back
left the whole suite green.
"""

from __future__ import annotations

import pytest

from cufast import _native

# Captured at import, before conftest's autouse fixture swaps them for stubs. The
# decision under test lives in C++ and toggles the real flag, so reading the stub
# would compare the test against itself.
BLOCKED = _native.input_blocked
SET_BLOCKED = _native.set_input_blocked
TRIPS = _native.kill_switch_trips


@pytest.fixture(autouse=True)
def clean_switch():
    """Leaves the real switch off however the test ends."""
    SET_BLOCKED(False)
    # Clear any half-finished press latched by a previous test.
    _native._hook_key_event(False, False, False)
    yield
    SET_BLOCKED(False)
    _native._hook_key_event(False, False, False)


def press(ctrl_down: bool = True) -> bool:
    return _native._hook_key_event(True, False, ctrl_down)


def release() -> bool:
    return _native._hook_key_event(False, False, False)


class TestAutoRepeat:
    """A held Ctrl+Esc must toggle exactly once, not once per repeat.

    Hardware auto-repeat arrives as ordinary key-downs at about 31 a second and
    KBDLLHOOKSTRUCT carries no repeat count, so a one-second hold delivered roughly
    sixteen of them. Toggling on each made whether the agent ended up stopped the
    parity of how long the user leaned on the key.
    """

    def test_one_hold_toggles_once(self):
        before = TRIPS()
        press()
        for _ in range(30):  # a second of typematic repeat
            press()
        assert TRIPS() == before + 1
        assert BLOCKED()

    def test_the_release_rearms_it(self):
        before = TRIPS()
        press()
        press()
        release()
        press()  # a genuine second press
        assert TRIPS() == before + 1  # second press RELEASED it
        assert not BLOCKED()

    def test_a_long_hold_leaves_it_engaged_not_a_coin_flip(self):
        for repeats in (1, 2, 5, 16, 31, 60):
            SET_BLOCKED(False)
            release()  # clear the latch
            for _ in range(repeats):
                press()
            assert BLOCKED(), (
                f"{repeats} repeats left the switch off -- the outcome depends on how "
                "long the key was held"
            )
            release()


class TestWhatIsSwallowed:
    def test_the_chord_is_swallowed_so_the_start_menu_stays_shut(self):
        assert press() is True
        assert release() is True

    def test_the_release_is_swallowed_only_once(self):
        press()
        assert release() is True
        assert release() is False

    def test_escape_without_ctrl_passes_through(self):
        assert press(ctrl_down=False) is False
        assert not BLOCKED()

    def test_an_injected_chord_is_ignored_entirely(self):
        # The agent must not be able to press its own stop button by being asked to
        # type ctrl+esc. This is the property that makes the switch meaningful.
        before = TRIPS()
        assert _native._hook_key_event(True, True, True) is False
        assert _native._hook_key_event(False, True, True) is False
        assert TRIPS() == before
        assert not BLOCKED()

    def test_injected_repeats_are_ignored_too(self):
        before = TRIPS()
        for _ in range(20):
            _native._hook_key_event(True, True, True)
        assert TRIPS() == before
