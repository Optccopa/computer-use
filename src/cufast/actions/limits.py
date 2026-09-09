"""Bounds on one call, and the messages that explain them.

None of these gates what the model may click -- driving the desktop is the whole
point. They stop one batch from consuming the harness, which runs batches one at
a time, so an unbounded batch is an unbounded outage.
"""

from __future__ import annotations

import time

from cufast import _native
from cufast.session import ActionError

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
