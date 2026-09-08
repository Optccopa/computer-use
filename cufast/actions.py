"""Executes computer actions, individually and in ordered batches.

Action names and parameters mirror the members of Anthropic's computer_toolset,
so the model's existing priors about `left_click`, `scroll_direction`, `repeat`
and friends transfer without translation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from cufast import _native
from cufast.session import ActionError, Screenshot, Session

# The exact wording the computer-use spec requires for actions skipped because an
# earlier action in the same turn failed.
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

MAX_DURATION_SECONDS = 300.0


@dataclass
class ActionResult:
    label: str
    text: str | None = None
    image: Screenshot | None = None
    is_error: bool = False


# Actions after which the UI needs a moment before the next action is meaningful.
_MUTATING = frozenset(
    {
        "left_click", "right_click", "middle_click", "double_click", "triple_click",
        "left_click_drag", "left_mouse_down", "left_mouse_up", "scroll", "type", "key",
    }
)

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


def _coordinate(value: Any, field: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ActionError(f"{field} must be [x, y]")
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        raise ActionError(f"{field} must be two numbers, got {value!r}") from None


def _text(params: dict[str, Any], required: bool = True) -> str:
    value = params.get("text")
    if value is None:
        if required:
            raise ActionError("text is required")
        return ""
    if not isinstance(value, str):
        raise ActionError(f"text must be a string, got {type(value).__name__}")
    return value


def _duration(params: dict[str, Any]) -> float:
    value = params.get("duration")
    if value is None:
        raise ActionError("duration is required (seconds)")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ActionError(f"duration must be a number, got {value!r}") from None
    if seconds < 0 or seconds > MAX_DURATION_SECONDS:
        raise ActionError(f"duration must be between 0 and {MAX_DURATION_SECONDS:g} seconds")
    return seconds


def _validate(name: str, params: dict[str, Any]) -> None:
    if name not in _ALLOWED_PARAMS:
        raise ActionError(
            f"unknown action {name!r}. Valid actions: {', '.join(ACTION_NAMES)}"
        )
    unknown = set(params) - _ALLOWED_PARAMS[name] - {"action"}
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_PARAMS[name])) or "(none)"
        raise ActionError(
            f"{name} does not take {', '.join(sorted(unknown))}; it takes: {allowed}"
        )


def execute(session: Session, name: str, params: dict[str, Any]) -> ActionResult:
    """Runs one action. Raises ActionError with a message meant for the model."""
    _validate(name, params)

    if name == "screenshot":
        return ActionResult(name, image=session.screenshot())

    if name == "zoom":
        region = params.get("region")
        if region is None:
            raise ActionError("zoom requires region as [x0, y0, x1, y1]")
        if not isinstance(region, (list, tuple)):
            raise ActionError("region must be [x0, y0, x1, y1]")
        try:
            bounds = [float(v) for v in region]
        except (TypeError, ValueError):
            raise ActionError(f"region must be four numbers, got {region!r}") from None
        return ActionResult(name, image=session.zoom(bounds))

    if name in _CLICK_BUTTONS:
        button, clicks = _CLICK_BUTTONS[name]
        if "coordinate" in params and params["coordinate"] is not None:
            x, y = _coordinate(params["coordinate"], "coordinate")
            _native.mouse_move(*session.to_screen(x, y))
        _native.mouse_click(button, clicks, _text(params, required=False))
        return ActionResult(name, text="OK")

    if name == "left_click_drag":
        start = params.get("start_coordinate")
        end = params.get("coordinate")
        if start is None or end is None:
            raise ActionError("left_click_drag requires start_coordinate and coordinate")
        x0, y0 = _coordinate(start, "start_coordinate")
        x1, y1 = _coordinate(end, "coordinate")
        sx0, sy0 = session.to_screen(x0, y0)
        sx1, sy1 = session.to_screen(x1, y1)
        _native.mouse_drag(sx0, sy0, sx1, sy1, _text(params, required=False))
        return ActionResult(name, text="OK")

    if name == "mouse_move":
        if params.get("coordinate") is None:
            raise ActionError("mouse_move requires coordinate")
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
        x, y = session.cursor_in_screenshot_space()
        return ActionResult(name, text=f"X={x}, Y={y}")

    if name == "scroll":
        direction = params.get("scroll_direction")
        if not isinstance(direction, str):
            raise ActionError("scroll requires scroll_direction: up, down, left, or right")
        amount = params.get("scroll_amount")
        if amount is None:
            raise ActionError("scroll requires scroll_amount (wheel clicks)")
        try:
            clicks = int(amount)
        except (TypeError, ValueError):
            raise ActionError(f"scroll_amount must be an integer, got {amount!r}") from None
        if clicks < 0:
            raise ActionError("scroll_amount must not be negative")
        if params.get("coordinate") is not None:
            x, y = _coordinate(params["coordinate"], "coordinate")
            _native.mouse_move(*session.to_screen(x, y))
        _native.mouse_scroll(direction, clicks, _text(params, required=False))
        return ActionResult(name, text="OK")

    if name == "type":
        _native.type_text(_text(params))
        return ActionResult(name, text="OK")

    if name == "key":
        repeat = params.get("repeat", 1)
        try:
            times = int(repeat)
        except (TypeError, ValueError):
            raise ActionError(f"repeat must be an integer, got {repeat!r}") from None
        if not 1 <= times <= 100:
            raise ActionError("repeat must be between 1 and 100")
        _native.press_key(_text(params), times)
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

    results: list[ActionResult] = []
    failed_at: int | None = None

    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise ActionError(f"action {index} must be an object, got {type(raw).__name__}")
        name = raw.get("action")
        if not isinstance(name, str):
            raise ActionError(f"action {index} is missing the required 'action' field")

        if failed_at is not None:
            results.append(ActionResult(name, text=NOT_EXECUTED, is_error=True))
            continue

        params = {k: v for k, v in raw.items() if k != "action"}
        try:
            result = execute(session, name, params)
        except ActionError as exc:
            results.append(ActionResult(name, text=f"Error: {exc}", is_error=True))
            failed_at = index
            continue
        except Exception as exc:  # native layer failures reach the model as text
            results.append(ActionResult(name, text=f"Error: {exc}", is_error=True))
            failed_at = index
            continue

        results.append(result)

        # Let the UI repaint before whatever comes next observes it.
        if (
            session.config.settle_ms
            and name in _MUTATING
            and index + 1 < len(actions)
        ):
            time.sleep(session.config.settle_ms / 1000.0)

    if auto_screenshot and failed_at is None:
        last = actions[-1].get("action") if isinstance(actions[-1], dict) else None
        if last not in ("screenshot", "zoom"):
            # Saves an entire model round trip versus asking for the screenshot in
            # the next turn, which is the single most common two-call pattern.
            if session.config.settle_ms and last in _MUTATING:
                time.sleep(session.config.settle_ms / 1000.0)
            results.append(ActionResult("screenshot", image=session.screenshot()))

    return results
