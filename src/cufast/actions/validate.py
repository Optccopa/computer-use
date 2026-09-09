"""Checks an action without performing any of it.

Run over the whole batch before anything executes, so a malformed action at the
end cannot leave the first half applied with nothing reported.
"""

from __future__ import annotations

from typing import Any

from cufast.actions.limits import (
    MAX_DURATION_SECONDS,
    MAX_SCROLL_AMOUNT,
    MAX_TYPE_CHARS,
)
from cufast.actions.names import (
    _ALLOWED_PARAMS,
    _CLICK_BUTTONS,
    _SCROLL_DIRECTIONS,
    ACTION_NAMES,
    canonical,
)
from cufast.session import ActionError


def _coordinate(value: Any, field: str) -> tuple[float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ActionError(f"{field} must be [x, y]")
    if len(value) != 2:
        raise ActionError(f"{field} must be [x, y]")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ActionError(f"{field} must be two numbers, got {value!r}")
        out.append(float(item))
    return out[0], out[1]


def _text(params: dict[str, Any], required: bool = True) -> str:
    value = params.get("text")
    if value is None:
        if required:
            raise ActionError("text is required")
        return ""
    if not isinstance(value, str):
        raise ActionError(f"text must be a string, got {type(value).__name__}")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionError(f"{field} must be an integer, got {value!r}")
    return value


def _duration(params: dict[str, Any]) -> float:
    value = params.get("duration")
    if value is None:
        raise ActionError("duration is required (seconds)")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionError(f"duration must be a number, got {value!r}")
    seconds = float(value)
    if not 0.0 <= seconds <= MAX_DURATION_SECONDS:
        raise ActionError(f"duration must be between 0 and {MAX_DURATION_SECONDS:g} seconds")
    return seconds


def validate(name: Any, params: dict[str, Any]) -> None:
    """Checks an action without performing any of it.

    Run over the whole batch before anything executes, so a malformed action at the
    end cannot leave the first half applied with nothing reported.
    """
    if not isinstance(name, str):
        raise ActionError("each action needs an 'action' field naming the action")
    name = canonical(name)
    if name not in _ALLOWED_PARAMS:
        raise ActionError(f"unknown action {name!r}. Valid actions: {', '.join(ACTION_NAMES)}")

    unknown = set(params) - _ALLOWED_PARAMS[name]
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_PARAMS[name])) or "(none)"
        raise ActionError(
            f"{name} does not take {', '.join(sorted(unknown))}; it takes: {allowed}"
        )

    if name == "zoom":
        region = params.get("region")
        if region is None:
            raise ActionError("zoom requires region as [x0, y0, x1, y1]")
        if isinstance(region, (str, bytes)) or not isinstance(region, (list, tuple)):
            raise ActionError("region must be [x0, y0, x1, y1]")
        if len(region) != 4:
            raise ActionError("region must be [x0, y0, x1, y1]")
        for item in region:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ActionError(f"region must be four numbers, got {region!r}")

    elif name in _CLICK_BUTTONS or name == "mouse_move":
        if name == "mouse_move" and params.get("coordinate") is None:
            raise ActionError("mouse_move requires coordinate")
        if params.get("coordinate") is not None:
            _coordinate(params["coordinate"], "coordinate")
        _text(params, required=False)

    elif name == "left_click_drag":
        if params.get("start_coordinate") is None or params.get("coordinate") is None:
            raise ActionError("left_click_drag requires start_coordinate and coordinate")
        _coordinate(params["start_coordinate"], "start_coordinate")
        _coordinate(params["coordinate"], "coordinate")
        _text(params, required=False)

    elif name == "scroll":
        direction = params.get("scroll_direction")
        if not isinstance(direction, str) or direction.lower() not in _SCROLL_DIRECTIONS:
            raise ActionError("scroll requires scroll_direction: up, down, left, or right")
        if params.get("scroll_amount") is None:
            raise ActionError("scroll requires scroll_amount (wheel clicks)")
        clicks = _integer(params["scroll_amount"], "scroll_amount")
        if clicks < 0:
            raise ActionError("scroll_amount must not be negative")
        if clicks > MAX_SCROLL_AMOUNT:
            raise ActionError(f"scroll_amount must be at most {MAX_SCROLL_AMOUNT}")
        if params.get("coordinate") is not None:
            _coordinate(params["coordinate"], "coordinate")
        _text(params, required=False)

    elif name == "mouse_move_rel":
        if params.get("dx") is None and params.get("dy") is None:
            raise ActionError("mouse_move_rel requires dx and/or dy (screenshot pixels)")
        for field in ("dx", "dy"):
            value = params.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ActionError(f"{field} must be a number, got {value!r}")
        steps = _integer(params.get("steps", 1), "steps")
        if not 1 <= steps <= 1000:
            raise ActionError("steps must be between 1 and 1000")

    elif name == "aim":
        if params.get("coordinate") is None:
            raise ActionError("aim requires coordinate as [x, y]")
        _coordinate(params["coordinate"], "coordinate")
        steps = _integer(params.get("steps", 1), "steps")
        if not 1 <= steps <= 1000:
            raise ActionError("steps must be between 1 and 1000")

    elif name == "look":
        if params.get("yaw") is None and params.get("pitch") is None:
            raise ActionError("look requires yaw and/or pitch, in degrees")
        for field in ("yaw", "pitch"):
            value = params.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ActionError(f"{field} must be a number of degrees, got {value!r}")
        steps = _integer(params.get("steps", 1), "steps")
        if not 1 <= steps <= 1000:
            raise ActionError("steps must be between 1 and 1000")

    elif name == "calibrate":
        if params.get("aim_ratio") is None and params.get("look_degrees_per_pixel") is None:
            raise ActionError(
                "calibrate requires aim_ratio and/or look_degrees_per_pixel"
            )
        for field in ("aim_ratio", "look_degrees_per_pixel"):
            value = params.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ActionError(f"{field} must be a number, got {value!r}")

    elif name in ("key_down", "key_up"):
        _text(params)

    elif name == "type":
        text = _text(params)
        if len(text) > MAX_TYPE_CHARS:
            raise ActionError(
                f"text is {len(text)} characters, over the {MAX_TYPE_CHARS} limit for "
                "one action. Typing is injected keystroke by keystroke and occupies "
                "the harness for the whole time; split it, or use a file."
            )

    elif name == "key":
        _text(params)
        repeat = _integer(params.get("repeat", 1), "repeat")
        if not 1 <= repeat <= 100:
            raise ActionError("repeat must be between 1 and 100")

    elif name == "hold_key":
        _text(params)
        _duration(params)

    elif name in ("wait", "wait_for_change"):
        _duration(params)

