"""Executes computer actions, individually and in ordered batches.

Action names and parameters mirror the members of Anthropic's computer_toolset,
so the model's existing priors about `left_click`, `scroll_direction`, `repeat`
and friends transfer without translation.
"""

from __future__ import annotations

from cufast.actions.batch import batch_from_call, run_batch
from cufast.actions.execute import ActionResult, capture_result, execute

# _MUTATING and _SECONDS_PER_RELATIVE_STEP are private to the package but re-
# exported because the tests assert against them directly: the settle list and
# the per-step cost are both things a test has to name to check them at all.
from cufast.actions.limits import (
    _SECONDS_PER_RELATIVE_STEP,
    ACTION_FAILURES,
    CAPTURE_WAS_FREE,
    FLAT_PARAMS,
    MAX_ACTIONS_PER_BATCH,
    MAX_BATCH_DURATION_SECONDS,
    MAX_DURATION_SECONDS,
    MAX_IMAGES_PER_BATCH,
    MAX_SCROLL_AMOUNT,
    MAX_TYPE_CHARS,
    MAX_TYPE_CHARS_PER_BATCH,
    NOT_EXECUTED,
    SCREEN_UNCHANGED,
    STOPPED_MESSAGE,
)
from cufast.actions.names import _MUTATING, ACTION_NAMES, canonical
from cufast.actions.validate import validate

__all__ = [
    "ACTION_FAILURES",
    "ACTION_NAMES",
    "CAPTURE_WAS_FREE",
    "FLAT_PARAMS",
    "MAX_ACTIONS_PER_BATCH",
    "MAX_BATCH_DURATION_SECONDS",
    "MAX_DURATION_SECONDS",
    "MAX_IMAGES_PER_BATCH",
    "MAX_SCROLL_AMOUNT",
    "MAX_TYPE_CHARS",
    "MAX_TYPE_CHARS_PER_BATCH",
    "NOT_EXECUTED",
    "SCREEN_UNCHANGED",
    "STOPPED_MESSAGE",
    "_MUTATING",
    "_SECONDS_PER_RELATIVE_STEP",
    "ActionResult",
    "batch_from_call",
    "canonical",
    "capture_result",
    "execute",
    "run_batch",
    "validate",
]
