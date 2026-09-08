"""Executes computer actions, individually and in ordered batches.

Action names and parameters mirror the members of Anthropic's computer_toolset,
so the model's existing priors about `left_click`, `scroll_direction`, `repeat`
and friends transfer without translation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from cufast import _native
from cufast.session import ActionError, Screenshot, Session

# The exact wording the computer-use spec requires for actions skipped because an
# earlier action in the same turn failed.
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

MAX_DURATION_SECONDS = 300.0
MAX_SCROLL_AMOUNT = 1000

# Exceptions that represent a failed action rather than a bug in this code. Native
# failures arrive as RuntimeError through nanobind. TypeError and AttributeError are
# deliberately NOT caught: those are programming errors, and dressing them up as
# action results would have the model retry them forever.
ACTION_FAILURES = (ActionError, RuntimeError, OSError, ValueError)


@dataclass
class ActionResult:
    label: str
    text: str | None = None
    image: Screenshot | None = None
    is_error: bool = False


# Actions after which the UI needs a moment before the next action is meaningful.
# mouse_move is included because hovering is its whole purpose: without a settle the
# screenshot that follows is the pre-hover frame, and the model concludes the tooltip
# never opened.
_MUTATING = frozenset(
    {
        "left_click", "right_click", "middle_click", "double_click", "triple_click",
        "left_click_drag", "left_mouse_down", "left_mouse_up", "mouse_move", "scroll",
        "type", "key", "hold_key",
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
    "left_click": frozenset({"coordinate", "text"}),
    "right_click": frozenset({"coordinate", "text"}),
    "middle_click": frozenset({"coordinate", "text"}),
    "double_click": frozenset({"coordinate", "text"}),
    "triple_click": frozenset({"coordinate", "text"}),
    "left_click_drag": frozenset({"start_coordinate", "coordinate", "text"}),
    "mouse_move": frozenset({"coordinate"}),
    "left_mouse_down": frozenset(),
    "left_mouse_up": frozenset(),
    "cursor_position": frozenset(),
    "scroll": frozenset({"scroll_direction", "scroll_amount", "coordinate", "text"}),
    "type": frozenset({"text"}),
    "key": frozenset({"text", "repeat"}),
    "hold_key": frozenset({"text", "duration"}),
    "wait": frozenset({"duration"}),
}

ACTION_NAMES = tuple(sorted(_ALLOWED_PARAMS))

_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


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

    elif name == "type":
        _text(params)

    elif name == "key":
        _text(params)
        repeat = _integer(params.get("repeat", 1), "repeat")
        if not 1 <= repeat <= 100:
            raise ActionError("repeat must be between 1 and 100")

    elif name == "hold_key":
        _text(params)
        _duration(params)

    elif name == "wait":
        _duration(params)


def execute(session: Session, name: str, params: dict[str, Any]) -> ActionResult:
    """Runs one action. Raises ActionError with a message meant for the model.

    Every parameter is validated before the first native call, so an action can never
    half-apply -- moving the cursor and then rejecting its own arguments.
    """
    validate(name, params)

    if name == "screenshot":
        return ActionResult(name, image=session.screenshot())

    if name == "zoom":
        return ActionResult(name, image=session.zoom([float(v) for v in params["region"]]))

    if name in _CLICK_BUTTONS:
        button, clicks = _CLICK_BUTTONS[name]
        modifiers = _text(params, required=False)
        target = None
        if params.get("coordinate") is not None:
            x, y = _coordinate(params["coordinate"], "coordinate")
            target = session.to_screen(x, y)  # may raise; nothing has moved yet
        if target is not None:
            _native.mouse_move(*target)
        _native.mouse_click(button, clicks, modifiers)
        return ActionResult(name, text="OK")

    if name == "left_click_drag":
        x0, y0 = _coordinate(params["start_coordinate"], "start_coordinate")
        x1, y1 = _coordinate(params["coordinate"], "coordinate")
        start = session.to_screen(x0, y0)
        end = session.to_screen(x1, y1)
        _native.mouse_drag(start[0], start[1], end[0], end[1], _text(params, required=False))
        return ActionResult(name, text="OK")

    if name == "mouse_move":
        x, y = _coordinate(params["coordinate"], "coordinate")
        _native.mouse_move(*session.to_screen(x, y))
        return ActionResult(name, text="OK")

    if name == "left_mouse_down":
        _native.mouse_down("left")
        return ActionResult(name, text="OK")

    if name == "left_mouse_up":
        _native.mouse_up("left")
        return ActionResult(name, text="OK")

    if name == "cursor_position":
        x, y, on_display = session.cursor_in_screenshot_space()
        if not on_display:
            return ActionResult(
                name,
                text=f"X={x}, Y={y} (the cursor is on another display; this is the "
                f"nearest point on the one being controlled)",
            )
        return ActionResult(name, text=f"X={x}, Y={y}")

    if name == "scroll":
        direction = params["scroll_direction"]
        clicks = int(params["scroll_amount"])
        modifiers = _text(params, required=False)
        target = None
        if params.get("coordinate") is not None:
            x, y = _coordinate(params["coordinate"], "coordinate")
            target = session.to_screen(x, y)
        if target is not None:
            _native.mouse_move(*target)
        _native.mouse_scroll(direction, clicks, modifiers)
        return ActionResult(name, text="OK")

    if name == "type":
        _native.type_text(_text(params))
        return ActionResult(name, text="OK")

    if name == "key":
        _native.press_key(_text(params), int(params.get("repeat", 1)))
        return ActionResult(name, text="OK")

    if name == "hold_key":
        _native.hold_key(_text(params), _duration(params))
        return ActionResult(name, text="OK")

    if name == "wait":
        time.sleep(_duration(params))
        return ActionResult(name, text="OK")

    raise ActionError(f"unhandled action {name!r}")  # pragma: no cover - guarded above


def run_batch(
    session: Session,
    actions: list[dict[str, Any]],
    auto_screenshot: bool = True,
) -> list[ActionResult]:
    """Runs actions in order, stopping at the first failure.

    Batching is the whole latency story here. Every action in one batch costs a
    single round trip, where the same actions issued one per turn cost one model
    round trip each -- seconds apiece against milliseconds of actual work.

    On failure the remaining actions are reported with the exact wording the
    computer-use spec defines, rather than being silently dropped.
    """
    if not actions:
        raise ActionError("actions must contain at least one action")

    # Validate everything up front. Doing it inside the loop would let a malformed
    # action at index 1 execute index 0 first and then raise, applying half the batch
    # while reporting none of it.
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise ActionError(f"action {index} must be an object, got {type(raw).__name__}")
        name = raw.get("action")
        if not isinstance(name, str):
            raise ActionError(f"action {index} is missing the required 'action' field")
        try:
            validate(name, {k: v for k, v in raw.items() if k != "action"})
        except ActionError as exc:
            raise ActionError(f"action {index} ({name}): {exc}") from None

    results: list[ActionResult] = []
    failed = False

    for index, raw in enumerate(actions):
        name = raw["action"]
        if failed:
            results.append(ActionResult(name, text=NOT_EXECUTED, is_error=True))
            continue

        params = {k: v for k, v in raw.items() if k != "action"}
        try:
            results.append(execute(session, name, params))
        except ACTION_FAILURES as exc:
            results.append(ActionResult(name, text=f"Error: {exc}", is_error=True))
            failed = True
            continue

        if session.config.settle_ms and name in _MUTATING and index + 1 < len(actions):
            time.sleep(session.config.settle_ms / 1000.0)

    if auto_screenshot and not failed and actions[-1]["action"] not in _CAPTURING:
        # Saves an entire model round trip versus asking for the screenshot in the
        # next turn, which is the single most common two-call pattern.
        if session.config.settle_ms and actions[-1]["action"] in _MUTATING:
            time.sleep(session.config.settle_ms / 1000.0)
        try:
            results.append(ActionResult("screenshot", image=session.screenshot()))
        except ACTION_FAILURES as exc:
            # Must not escape: the batch already ran, and losing every result because
            # the trailing capture failed would leave the model unable to tell what
            # actually happened. A DXGI device loss here is routine.
            results.append(
                ActionResult("screenshot", text=f"Error: {exc}", is_error=True)
            )

    return results
