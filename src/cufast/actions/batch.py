"""Runs actions in order, and decides what one call is allowed to cost."""

from __future__ import annotations

import time
from typing import Any

from cufast import _native
from cufast.actions.execute import ActionResult, capture_result, execute
from cufast.actions.limits import (
    _SECONDS_PER_RELATIVE_STEP,
    _SECONDS_PER_TYPED_CHAR,
    _STEPPED,
    ACTION_FAILURES,
    FLAT_PARAMS,
    MAX_ACTIONS_PER_BATCH,
    MAX_BATCH_DURATION_SECONDS,
    MAX_CLIPBOARD_READS_PER_BATCH,
    MAX_IMAGES_PER_BATCH,
    MAX_TYPE_CHARS_PER_BATCH,
    NOT_EXECUTED,
    STOPPED_MESSAGE,
)
from cufast.actions.names import _CAPTURING, _MUTATING, ACTION_NAMES, canonical
from cufast.actions.validate import validate
from cufast.session import ActionError, Session


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

    # A read is a clipboard action with no `text`, and each one can return twenty
    # thousand characters the model never sent. Bounded here for the same reason
    # images are: it is reply size, and nothing downstream can give it back.
    reads = sum(
        1 for raw in actions
        if canonical(raw.get("action")) == "clipboard" and raw.get("text") is None
    )
    if reads > MAX_CLIPBOARD_READS_PER_BATCH:
        raise ActionError(
            f"{reads} clipboard reads in one call, over the "
            f"{MAX_CLIPBOARD_READS_PER_BATCH} limit. The clipboard only changes when "
            "something copies to it, so reading it repeatedly in one batch returns "
            "the same text at full cost each time."
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
