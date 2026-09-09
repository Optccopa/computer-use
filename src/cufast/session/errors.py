"""Failures a model should see and react to, rather than crashes."""

from __future__ import annotations

# Native failures arrive as RuntimeError through nanobind. Kept local to avoid
# importing cufast.actions, which imports this module.
ACTION_FAILURES = (RuntimeError, OSError, ValueError, OverflowError)

# Kept here rather than imported from cufast.actions, which imports this module.
STOPPED_BY_KILL_SWITCH = (
    "STOPPED BY THE USER while waiting. They pressed the kill switch (Ctrl+Esc). "
    "Stop what you were doing, do not retry, and tell them you have stopped."
)


class ActionError(Exception):
    """A computer action that failed for a reason the model should see and react to."""
