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

# What the model is told when the user has hit the stop button. Worded as an
# instruction rather than a status because a bare "input is blocked" reads to a
# model like a transient fault, and the response to a transient fault is to retry.
STOPPED_MESSAGE = (
    "STOPPED BY THE USER. They pressed the kill switch (Ctrl+Esc), which blocks all "
    "mouse and keyboard input. Stop what you were doing, do not retry, and tell them "
    "you have stopped. They release it by pressing Ctrl+Esc again."
)

MAX_DURATION_SECONDS = 300.0

# How long a wait sleeps before looking at the kill switch again. Short enough that
# stopping feels immediate, long enough that a five minute wait is not a busy loop.
_SLEEP_SLICE_SECONDS = 0.05


def _sleep_interruptibly(seconds: float) -> None:
    """Sleeps, but gives up the moment the kill switch is engaged.

    A plain sleep here was a real hole in the stop button: `wait` accepts up to five
    minutes, and the observed sessions used it constantly, so pressing Ctrl+Esc
    during one left the harness sleeping for the rest of it before anything noticed.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if _native.input_blocked():
            raise ActionError(STOPPED_MESSAGE)
        time.sleep(min(remaining, _SLEEP_SLICE_SECONDS))
MAX_SCROLL_AMOUNT = 1000

# Exceptions that represent a failed action rather than a bug in this code. Native
# failures arrive as RuntimeError through nanobind. TypeError and AttributeError are
# deliberately NOT caught: those are programming errors, and dressing them up as
# action results would have the model retry them forever.
# OverflowError joins them because it is produced by arithmetic on model-supplied
# numbers (dx=1e308), not by a bug here, and letting it escape destroyed the entire
# batch result including screenshots that had already succeeded.
ACTION_FAILURES = (ActionError, RuntimeError, OSError, ValueError, OverflowError)

# One action is capped at MAX_DURATION_SECONDS, but the batch length was not, so
# 200 waits of 300s could occupy the single native worker for sixteen hours with no
# way to report it -- screen_info is dispatched onto the same worker.
MAX_BATCH_DURATION_SECONDS = 600.0

# Bounds on one call. None of these is a safety gate on what the model may click --
# driving the desktop is the entire point -- they stop a single batch from consuming
# the harness itself. The one worker thread runs batches one at a time, so an
# unbounded batch is an unbounded outage, and the reply has to fit in a response.
MAX_ACTIONS_PER_BATCH = 64
MAX_TYPE_CHARS = 8000
MAX_IMAGES_PER_BATCH = 10

# The same cap applied across the whole batch, not just one action. The per-action
# limit is justified by "typing occupies the harness for the whole time", but the
# batch budget below only ever counted declared `duration`, so 64 actions of 8000
# characters each was accepted: 512,000 keystrokes, 1,024,000 injected events, in one
# call that no cap objected to. A cap that the batch multiplies by 64 is not a cap.
MAX_TYPE_CHARS_PER_BATCH = MAX_TYPE_CHARS

# What one step of a split relative move costs, matching the sleep in
# mouse_move_relative. steps is capped at 1000 per action, so a single action can
# occupy the harness for two seconds and a full batch for over two minutes -- none of
# which the duration budget saw, because no `duration` was ever declared.
_SECONDS_PER_RELATIVE_STEP = 0.002

# Rough cost of injecting one character. type_text batches 512 events per SendInput,
# so this is dominated by the receiving application rather than by us; it only has to
# be the right order of magnitude to keep a batch from monopolising the one worker.
_SECONDS_PER_TYPED_CHAR = 0.0005

_STEPPED = frozenset({"mouse_move_rel", "aim", "look"})

# Every parameter any action takes, as siblings of a top-level `action`. This is the
# flat shape the standard computer tool uses, and it is the shape the model has
# actually been trained on -- so accepting it means a model's existing priors drive
# this harness with no translation step at all. `actions` stays the fast path, and is
# what makes more than one action cost one round trip instead of several.
FLAT_PARAMS = (
    "coordinate", "text", "start_coordinate", "scroll_direction", "scroll_amount",
    "duration", "repeat", "region", "dx", "dy", "steps", "yaw", "pitch",
    "aim_ratio", "look_degrees_per_pixel",
)


def batch_from_call(
    action: Any, actions: list[dict[str, Any]] | None, flat: dict[str, Any]
) -> list[dict[str, Any]]:
    """Accepts either call shape and returns the batch to run.

    Both at once is refused rather than guessed at: silently preferring one would
    run something the caller did not ask for, and a call carrying both is a mistake
    worth reporting while it is still cheap.
    """
    given = {k: v for k, v in flat.items() if v is not None}
    if actions is not None and action is not None:
        raise ActionError(
            "pass either `action` (one action, with its parameters alongside it) or "
            "`actions` (an ordered list), not both."
        )
    if actions is not None:
        if given:
            raise ActionError(
                f"{', '.join(sorted(given))} belongs inside the `actions` list, not "
                "beside it. Each item in `actions` carries its own parameters."
            )
        return actions
    if action is not None:
        return [{"action": action, **given}]
    raise ActionError(
        "give either `action` with its parameters, or `actions` as an ordered list. "
        f"Valid actions: {', '.join(ACTION_NAMES)}"
    )


# Sent in place of an image the model already has. Worded as "you already have it"
# rather than "no image": the point is to stop the next call being another look, and
# a bare "unchanged" reads like a failed capture worth retrying.
SCREEN_UNCHANGED = (
    "Screen unchanged -- pixel for pixel identical to the last image you were given, "
    "so no new one was sent. You already have the current state of the display; act "
    "on it rather than looking again."
)

# Added to a capture the caller did not need to ask for. Every call returns a
# screenshot on its own, so an explicit one buys nothing but the round trip it took
# to request it.
CAPTURE_WAS_FREE = (
    " (you did not need to ask for this -- every call returns a screenshot of the "
    "display when it is done, so put the actions you want in the call instead)"
)


@dataclass
class ActionResult:
    label: str
    text: str | None = None
    image: Screenshot | None = None
    is_error: bool = False


def capture_result(session: Session, label: str, shot: Screenshot,
                   note: str = "") -> ActionResult:
    """One capture, as either an image or the news that it is the same image.

    A desktop is static for most of the time an agent spends looking at it, and an
    identical frame costs a full image of context to say nothing. Suppressing it is
    worth about 777 visual tokens a call, and it is also the only thing that teaches
    the model that looking again was not worth a round trip.
    """
    if not session.mark_delivered(shot):
        return ActionResult(label, text=SCREEN_UNCHANGED + note)
    return ActionResult(label, image=shot, text=note or None)


# Actions after which the UI needs a moment before the next action is meaningful.
# mouse_move is included because hovering is its whole purpose: without a settle the
# screenshot that follows is the pre-hover frame, and the model concludes the tooltip
# never opened.
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
    "left_click": frozenset({"coordinate", "text"}),
    "right_click": frozenset({"coordinate", "text"}),
    "middle_click": frozenset({"coordinate", "text"}),
    "double_click": frozenset({"coordinate", "text"}),
    "triple_click": frozenset({"coordinate", "text"}),
    "left_click_drag": frozenset({"start_coordinate", "coordinate", "text"}),
    "mouse_move": frozenset({"coordinate"}),
    "mouse_move_rel": frozenset({"dx", "dy", "steps"}),
    "aim": frozenset({"coordinate", "steps"}),
    "look": frozenset({"yaw", "pitch", "steps"}),
    "calibrate": frozenset({"aim_ratio", "look_degrees_per_pixel"}),
    "left_mouse_down": frozenset(),
    "left_mouse_up": frozenset(),
    "cursor_position": frozenset(),
    "scroll": frozenset({"scroll_direction", "scroll_amount", "coordinate", "text"}),
    "type": frozenset({"text"}),
    "key": frozenset({"text", "repeat"}),
    "hold_key": frozenset({"text", "duration"}),
    "key_down": frozenset({"text"}),
    "key_up": frozenset({"text"}),
    "wait": frozenset({"duration"}),
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
}


def canonical(name: Any) -> Any:
    """Maps a known misspelling onto the real action name."""
    return _ALIASES.get(name, name) if isinstance(name, str) else name

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


def execute(session: Session, name: str, params: dict[str, Any]) -> ActionResult:
    """Runs one action. Raises ActionError with a message meant for the model.

    Every parameter is validated before the first native call, so an action can never
    half-apply -- moving the cursor and then rejecting its own arguments.
    """
    validate(name, params)
    name = canonical(name)

    if name == "screenshot":
        # Free either way; say so, because an explicit screenshot is a whole
        # round trip spent asking for something that arrives on its own.
        return capture_result(session, name, session.screenshot(), CAPTURE_WAS_FREE)

    if name == "zoom":
        shot = session.zoom([float(v) for v in params["region"]])
        return capture_result(session, name, shot)

    if name in _CLICK_BUTTONS:
        button, clicks = _CLICK_BUTTONS[name]
        modifiers = _text(params, required=False)
        # Resolved before the move. "ctrl+zzz" used to move the cursor and only then
        # fail, leaving a hover applied and the batch halted with no screenshot.
        _native.validate_chord(modifiers)
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

    if name == "mouse_move_rel":
        dx, dy = session.scale_delta(float(params.get("dx", 0)), float(params.get("dy", 0)))
        was_on = session.cursor_is_on_display()
        _native.mouse_move_relative(dx, dy, int(params.get("steps", 1)))
        session.confine_cursor(was_on)
        return ActionResult(name, text=f"OK (moved {dx:+d}, {dy:+d} native pixels)")

    if name == "aim":
        x, y = _coordinate(params["coordinate"], "coordinate")
        # Before the probe, which moves the view: rejecting the coordinate afterwards
        # left the camera rotated and then blamed the coordinate.
        session.check_in_frame(x, y)
        # Calibrating here rather than making the model do it first is the whole
        # point: a primitive with a setup step does not get used.
        note = ""
        already_turned = 0
        if session.aim_ratio is None:
            already_turned = session.autocalibrate_aim()
            note = f" [calibrated itself: aim_ratio={session.aim_ratio:.3g}]"
        dx, dy = session.aim_delta(x, y)
        # The probe turned the view as a side effect, and the coordinate was given
        # against the frame from before it, so that much of the turn is already done.
        dx -= already_turned
        was_on = session.cursor_is_on_display()
        _native.mouse_move_relative(dx, dy, int(params.get("steps", 1)))
        session.confine_cursor(was_on)
        return ActionResult(
            name,
            text=f"OK (turned {dx:+d}, {dy:+d} native pixels to bring ({x:g}, {y:g}) "
            f"onto the crosshair){note}",
        )

    if name == "look":
        yaw = float(params.get("yaw", 0))
        pitch = float(params.get("pitch", 0))
        dx, dy = session.look_delta(yaw, pitch)
        was_on = session.cursor_is_on_display()
        _native.mouse_move_relative(dx, dy, int(params.get("steps", 1)))
        session.confine_cursor(was_on)
        return ActionResult(name, text=f"OK (yaw {yaw:+g}, pitch {pitch:+g} degrees "
                                       f"= {dx:+d}, {dy:+d} native pixels)")

    if name == "calibrate":
        if params.get("aim_ratio") is not None:
            session.set_aim_ratio(float(params["aim_ratio"]))
        if params.get("look_degrees_per_pixel") is not None:
            dpp = float(params["look_degrees_per_pixel"])
            session.set_look_scale(dpp, dpp)
        aim = "unset" if session.aim_ratio is None else f"{session.aim_ratio:g}"
        look = "unset" if session.look_scale is None else f"{session.look_scale[0]:g}"
        return ActionResult(
            name, text=f"OK (aim_ratio={aim}, look_degrees_per_pixel={look})"
        )

    if name == "key_down":
        _native.key_down(_text(params))
        held = _native.held_keys()
        listed = ", ".join(held) if held else "nothing"
        return ActionResult(name, text=f"OK (now held: {listed})")

    if name == "key_up":
        _native.key_up(_text(params))
        held = _native.held_keys()
        listed = ", ".join(held) if held else "nothing"
        return ActionResult(name, text=f"OK (still held: {listed})")

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
        _native.validate_chord(modifiers)
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
        _sleep_interruptibly(_duration(params))
        return ActionResult(name, text="OK")

    if name == "wait_for_change":
        limit = _duration(params)
        waited = session.wait_for_change(limit)
        if waited is None:
            return ActionResult(
                name,
                text=f"nothing changed within {limit:g}s. The screen is still. Either "
                "the action you were waiting on has already finished, or it never "
                "started -- check the screenshot rather than waiting again.",
            )
        return ActionResult(name, text=f"OK (the screen changed after {waited:.0f} ms)")

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
    if len(actions) > MAX_ACTIONS_PER_BATCH:
        raise ActionError(
            f"{len(actions)} actions in one call, over the {MAX_ACTIONS_PER_BATCH} "
            "limit. Batching is worth doing, but a batch this long cannot be reasoned "
            "about from a single screenshot at the end of it. Split it."
        )

    # Checked for the whole call, not just the injecting actions. A batch of pure
    # screenshots would otherwise succeed while the switch is engaged, and the model
    # would carry on looking around instead of stopping.
    if _native.input_blocked():
        raise ActionError(STOPPED_MESSAGE)

    # Validate everything up front. Doing it inside the loop would let a malformed
    # action at index 1 execute index 0 first and then raise, applying half the batch
    # while reporting none of it.
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise ActionError(f"action {index} must be an object, got {type(raw).__name__}")
        name = canonical(raw.get("action"))
        if not isinstance(name, str):
            raise ActionError(f"action {index} is missing the required 'action' field")
        try:
            validate(name, {k: v for k, v in raw.items() if k != "action"})
        except ActionError as exc:
            raise ActionError(f"action {index} ({name}): {exc}") from None

    # Every way a batch can occupy the worker, not just the ways that announce
    # themselves with a `duration`. Waits were the only thing counted, so a batch
    # could hold the single worker for minutes through `steps` and `type` alone
    # while passing a budget that believed it was instantaneous.
    total_wait = 0.0
    typed_chars = 0
    for raw in actions:
        name = canonical(raw.get("action"))
        if name in ("wait", "wait_for_change", "hold_key"):
            total_wait += float(raw.get("duration", 0) or 0)
        elif name in _STEPPED:
            steps = raw.get("steps", 1)
            if isinstance(steps, int) and not isinstance(steps, bool):
                total_wait += max(0, steps - 1) * _SECONDS_PER_RELATIVE_STEP
        elif name == "type":
            text = raw.get("text")
            if isinstance(text, str):
                typed_chars += len(text)
                total_wait += len(text) * _SECONDS_PER_TYPED_CHAR

    if typed_chars > MAX_TYPE_CHARS_PER_BATCH:
        raise ActionError(
            f"this batch types {typed_chars} characters in total, over the "
            f"{MAX_TYPE_CHARS_PER_BATCH} limit for one call. The per-action limit is "
            "the same number: it bounds the batch, not just each `type` in it, "
            "because every character is a separate injected keystroke and the "
            "harness runs one batch at a time. Split it, or use a file."
        )

    if total_wait > MAX_BATCH_DURATION_SECONDS:
        raise ActionError(
            f"this batch would occupy the harness for about {total_wait:g}s, over the "
            f"{MAX_BATCH_DURATION_SECONDS:g}s limit for one call. Waits, split "
            "relative moves (`steps`) and typing all count toward this. The harness "
            "runs one batch at a time, so nothing else -- including screen_info -- "
            "can run while it does. Split it up."
        )

    images = sum(1 for raw in actions if canonical(raw.get("action")) in _CAPTURING)
    # The trailing automatic screenshot is an image the caller receives and pays for,
    # so it belongs in the count. Leaving it out let ten explicit captures come back
    # as eleven images, one over a cap whose whole purpose is bounding context cost.
    if auto_screenshot and canonical(actions[-1].get("action")) not in _CAPTURING:
        images += 1
    if images > MAX_IMAGES_PER_BATCH:
        raise ActionError(
            f"{images} captures in one call, over the {MAX_IMAGES_PER_BATCH} limit. "
            "Every image is about 777 visual tokens, so a batch of them costs more "
            "context than the actions are worth."
        )

    results: list[ActionResult] = []
    failed = False

    for index, raw in enumerate(actions):
        name = canonical(raw["action"])
        if failed:
            results.append(ActionResult(name, text=NOT_EXECUTED, is_error=True))
            continue

        # Re-checked per action, not just once at the top. Most injecting actions
        # consult the switch inside the native layer, but key_up and left_mouse_up
        # deliberately do not -- releasing what is held must never be the thing that
        # is blocked, or engaging the switch mid-drag strands the desktop. That
        # bypass exists for the harness's own recovery path, and the model reaching
        # it through an action is not the same thing: a batch of key_up actions ran
        # to completion after the user pressed stop, and key_up takes any chord, not
        # only one this process pressed.
        if _native.input_blocked():
            results.append(ActionResult(name, text=f"Error: {STOPPED_MESSAGE}", is_error=True))
            failed = True
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

    last = canonical(actions[-1]["action"])
    if auto_screenshot and not failed and last not in _CAPTURING:
        # Saves an entire model round trip versus asking for the screenshot in the
        # next turn, which is the single most common two-call pattern.
        #
        # Canonicalised, because an alias here would miss _MUTATING and skip the
        # settle, capturing the frame from before the action it is meant to confirm.
        if session.config.settle_ms and last in _MUTATING:
            time.sleep(session.config.settle_ms / 1000.0)
        try:
            results.append(capture_result(session, "screenshot", session.screenshot()))
        except ACTION_FAILURES as exc:
            # Must not escape: the batch already ran, and losing every result because
            # the trailing capture failed would leave the model unable to tell what
            # actually happened. A DXGI device loss here is routine.
            results.append(
                ActionResult("screenshot", text=f"Error: {exc}", is_error=True)
            )

    return results
