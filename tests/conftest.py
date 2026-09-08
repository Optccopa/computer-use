"""Shared fixtures.

Input injection is stubbed by default. These tests must never move the real mouse or
type into whatever window happens to have focus on the developer's machine.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cufast import _native
from cufast.config import Config

INPUT_FUNCTIONS = (
    "mouse_move",
    "mouse_click",
    "mouse_down",
    "mouse_up",
    "mouse_drag",
    "mouse_scroll",
    "type_text",
    "press_key",
    "hold_key",
    "cursor_position",
    "mouse_move_relative",
    "key_down",
    "key_up",
    "held_keys",
)


class FakeScreen:
    """Stands in for _native.Screen with a fixed geometry and no real capture."""

    def __init__(self, width=1920, height=1080, origin_x=0, origin_y=0, index=0):
        self.width = width
        self.height = height
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.index = index
        self.using_dxgi = True
        self.calls: list[dict] = []
        self.raise_on_grab: Exception | None = None
        # Pretend view: how far the camera has been turned, and how much mouse it
        # takes to move the image one profile sample.
        self._panned = 0
        self.pan_ratio = 2.0
        # What wait_for_change reports: milliseconds, -1 for timeout, -2 for the
        # kill switch.
        self.change_after_ms = 120.0

    def resize(self, width, height):
        """Simulates a display mode change, which the native layer follows."""
        self.width = width
        self.height = height

    def wait_for_change(self, timeout_seconds, grid_w=160, grid_h=90):
        self.calls.append({"wait_for_change": timeout_seconds})
        return self.change_after_ms

    def profile(self, max_w, max_h, timeout_ms=16):
        """A 1-D luma profile that pans with the fake cursor.

        Modelled so that a mouse delta shifts it, which is what calibration
        measures. pan_ratio is native mouse pixels per profile sample, i.e. exactly
        the aim_ratio a correct calibration should recover.
        """
        dst_w, _ = _native.plan_fit(self.width, self.height, max_w, max_h, False)
        offset = round(self._panned / self.pan_ratio)
        # A repeating-but-not-periodic pattern, so a shift is unambiguous.
        return [((x + offset) * 37) % 251 + ((x + offset) * 7) % 13 for x in range(dst_w)]

    def grab(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_grab is not None:
            raise self.raise_on_grab
        rw = kwargs.get("rw", -1)
        rh = kwargs.get("rh", -1)
        src_w = self.width if rw < 0 else rw
        src_h = self.height if rh < 0 else rh
        dst_w, dst_h = _native.plan_fit(src_w, src_h, kwargs["max_w"], kwargs["max_h"], False)
        return SimpleNamespace(
            data=b"\xff\xd8\xff\xd9",  # the smallest thing shaped like a JPEG
            width=dst_w,
            height=dst_h,
            src_x=kwargs.get("rx", 0),
            src_y=kwargs.get("ry", 0),
            src_w=src_w,
            src_h=src_h,
            frame_id=1,
            content_hash=0,
            dxgi=True,
        )


class RecordingInput:
    """Captures input calls instead of performing them."""

    def __init__(self):
        self.events: list[tuple] = []
        self.cursor = (960, 540)
        self.held: list[str] = []
        # Optional FakeScreen to pan when a relative move happens, so aim
        # calibration has a view that actually responds to the mouse.
        self.screen = None
        # A pointer-locked application (any 3D game) hides the cursor and warps it
        # back to the window centre every frame, so relative movement turns the view
        # without the cursor ever travelling. An ordinary desktop does move it.
        self.pointer_locked = False

    def mouse_move(self, x, y):
        self.events.append(("mouse_move", x, y))
        self.cursor = (x, y)

    def mouse_click(self, button="left", clicks=1, modifiers=""):
        self.events.append(("mouse_click", button, clicks, modifiers))

    def mouse_down(self, button="left"):
        self.events.append(("mouse_down", button))

    def mouse_up(self, button="left"):
        self.events.append(("mouse_up", button))

    def mouse_drag(self, x0, y0, x1, y1, modifiers=""):
        self.events.append(("mouse_drag", x0, y0, x1, y1, modifiers))
        self.cursor = (x1, y1)

    def mouse_scroll(self, direction, amount, modifiers=""):
        self.events.append(("mouse_scroll", direction, amount, modifiers))

    def type_text(self, text):
        self.events.append(("type_text", text))

    def press_key(self, chord, repeat=1):
        self.events.append(("press_key", chord, repeat))

    def hold_key(self, chord, seconds):
        self.events.append(("hold_key", chord, seconds))

    def mouse_move_relative(self, dx, dy, steps=1):
        self.events.append(("mouse_move_relative", dx, dy, steps))
        if not self.pointer_locked:
            self.cursor = (self.cursor[0] + dx, self.cursor[1] + dy)
        if self.screen is not None:
            self.screen._panned += dx

    def key_down(self, chord):
        self.events.append(("key_down", chord))
        if chord not in self.held:
            self.held.append(chord)

    def key_up(self, chord):
        self.events.append(("key_up", chord))
        if chord in self.held:
            self.held.remove(chord)

    def held_keys(self):
        return list(self.held)

    def cursor_position(self):
        return self.cursor

    def names(self) -> list[str]:
        return [event[0] for event in self.events]


@pytest.fixture(autouse=True)
def no_real_kill_switch(monkeypatch):
    """Keeps the low-level keyboard hook out of the test process.

    Autouse rather than opt-in: build_server() arms the kill switch by default, so
    without this every test that builds a server would install a real system-wide
    hook and silently break Ctrl+Esc for whoever is running the suite.
    """
    state = {"running": False, "blocked": False, "releases": 0}
    monkeypatch.setattr(_native, "start_kill_switch",
                        lambda: state.__setitem__("running", True), raising=True)
    monkeypatch.setattr(_native, "stop_kill_switch",
                        lambda: state.__setitem__("running", False), raising=True)
    monkeypatch.setattr(_native, "kill_switch_running", lambda: state["running"], raising=True)
    monkeypatch.setattr(_native, "input_blocked", lambda: state["blocked"], raising=True)
    monkeypatch.setattr(_native, "set_input_blocked",
                        lambda blocked: state.__setitem__("blocked", blocked), raising=True)
    # Stubbed for the same reason as the rest: the real one calls SendInput, and a
    # test run must never inject anything onto the developer's desktop.
    monkeypatch.setattr(_native, "release_held_input",
                        lambda: state.__setitem__("releases", state["releases"] + 1),
                        raising=True)
    return state


@pytest.fixture
def config():
    # settle_ms=0 keeps tests from sleeping; the delay has its own dedicated test.
    return Config(display_index=0, max_width=1024, max_height=768, settle_ms=0)


@pytest.fixture
def fake_input(monkeypatch):
    """Redirects every input call to a recorder.

    cufast.actions and cufast.session both hold the same `_native` module object, so
    patching its attributes once covers every caller.
    """
    recorder = RecordingInput()
    for name in INPUT_FUNCTIONS:
        monkeypatch.setattr(_native, name, getattr(recorder, name), raising=True)
    return recorder


@pytest.fixture
def fake_screen():
    return FakeScreen()


@pytest.fixture
def session(config, fake_screen, fake_input, monkeypatch):
    """A Session backed by FakeScreen, so no capture or input touches the machine.

    The recorder is wired to the screen so a relative move pans the fake view, which
    is what aim calibration measures.
    """
    import cufast.session as session_mod

    fake_input.screen = fake_screen
    monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
    return session_mod.Session(config)
