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

    def resize(self, width, height):
        """Simulates a display mode change, which the native layer follows."""
        self.width = width
        self.height = height

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

    def cursor_position(self):
        return self.cursor

    def names(self) -> list[str]:
        return [event[0] for event in self.events]


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
def session(config, fake_screen, monkeypatch):
    """A Session backed by FakeScreen, so no capture or input touches the machine."""
    import cufast.session as session_mod

    monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
    return session_mod.Session(config)
