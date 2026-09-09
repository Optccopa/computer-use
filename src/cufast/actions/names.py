"""Action names, their accepted parameters, and the spellings that map onto them."""

from __future__ import annotations

from typing import Any

_MUTATING = frozenset(
    {
        "left_click", "right_click", "middle_click", "double_click", "triple_click",
        "left_click_drag", "left_mouse_down", "left_mouse_up", "mouse_move", "scroll",
        "type", "key", "hold_key", "mouse_move_rel", "key_down", "key_up", "aim", "look",
    }
)

_CAPTURING = frozenset({"screenshot", "zoom"})

_CLICK_BUTTONS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}

_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "screenshot": frozenset(),
    "zoom": frozenset({"region"}),
    "left_click": frozenset({"coordinate", "text", "in_zoom"}),
    "right_click": frozenset({"coordinate", "text", "in_zoom"}),
    "middle_click": frozenset({"coordinate", "text", "in_zoom"}),
    "double_click": frozenset({"coordinate", "text", "in_zoom"}),
    "triple_click": frozenset({"coordinate", "text", "in_zoom"}),
    "left_click_drag": frozenset({"start_coordinate", "coordinate", "text", "in_zoom"}),
    "mouse_move": frozenset({"coordinate", "in_zoom"}),
    "mouse_move_rel": frozenset({"dx", "dy", "steps"}),
    "aim": frozenset({"coordinate", "steps"}),
    "look": frozenset({"yaw", "pitch", "steps"}),
    "calibrate": frozenset({"aim_ratio", "look_degrees_per_pixel"}),
    "left_mouse_down": frozenset(),
    "left_mouse_up": frozenset(),
    "cursor_position": frozenset(),
    "scroll": frozenset(
        {"scroll_direction", "scroll_amount", "coordinate", "text", "in_zoom"}
    ),
    "type": frozenset({"text"}),
    "key": frozenset({"text", "repeat"}),
    "hold_key": frozenset({"text", "duration"}),
    "key_down": frozenset({"text"}),
    "key_up": frozenset({"text"}),
    "wait": frozenset({"duration"}),
    "clipboard": frozenset({"text"}),
    "wait_for_change": frozenset({"duration"}),
}

ACTION_NAMES = tuple(sorted(_ALLOWED_PARAMS))

# Spellings a model reaches for that are not the canonical name. Accepting them
# costs nothing and saves a whole round trip each: in an observed session two of
# fifty-three calls were spent purely on rediscovering the right spelling, and at
# nine seconds a call that is not a rounding error.
_ALIASES = {
    "keydown": "key_down",
    "keyup": "key_up",
    "key_press": "key",
    "press": "key",
    "mouse_move_relative": "mouse_move_rel",
    "move_rel": "mouse_move_rel",
    "rel_move": "mouse_move_rel",
    "wait_for_screen_change": "wait_for_change",
    "await_change": "wait_for_change",
    "screen_shot": "screenshot",
    "capture": "screenshot",
    # Observed in a real session: the model dropped the "left_" prefix, which the
    # click actions do not have either, so the asymmetry is a fair thing to trip on.
    "mouse_down": "left_mouse_down",
    "mouse_up": "left_mouse_up",
    "click": "left_click",
    "mouse_click": "left_click",
    # One action reads or writes depending on whether `text` is present, so the
    # separate names a model is likely to reach for both land on it correctly.
    "clipboard_read": "clipboard",
    "clipboard_write": "clipboard",
    "get_clipboard": "clipboard",
    "set_clipboard": "clipboard",
}


def canonical(name: Any) -> Any:
    """Maps a known misspelling onto the real action name."""
    return _ALIASES.get(name, name) if isinstance(name, str) else name

_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
