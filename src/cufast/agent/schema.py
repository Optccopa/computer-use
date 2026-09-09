"""The computer tool as the API sees it."""

from __future__ import annotations

from typing import Any

from cufast.actions import FLAT_PARAMS, MAX_ACTIONS_PER_BATCH
from cufast.server import TOOL_DESCRIPTION


def computer_tool_schema() -> dict[str, Any]:
    """The tool as the model sees it, built from the same constants the server uses.

    Derived rather than written out again: a parameter added to FLAT_PARAMS and not
    here would be one the model could never send, and the failure would look like
    the action quietly not working.
    """
    properties: dict[str, Any] = {
        "action": {
            "type": "string",
            "description": "One action, with its parameters as siblings of this field.",
        },
        "actions": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                f"An ordered list of up to {MAX_ACTIONS_PER_BATCH} actions, run in one "
                "call. Prefer this: it is where the latency saving is."
            ),
        },
        "auto_screenshot": {
            "type": "boolean",
            "description": (
                "Defaults to true. Leave it alone unless you truly do not need to see "
                "the result -- the screenshot is free and arrives with the result."
            ),
        },
        "display": {"type": "integer", "description": "Which monitor to control."},
    }
    for name in FLAT_PARAMS:
        properties[name] = {"description": f"Parameter for the chosen action: {name}."}
    return {
        "name": "computer",
        "description": TOOL_DESCRIPTION,
        "input_schema": {"type": "object", "properties": properties},
    }

