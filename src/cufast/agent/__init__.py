"""An agent loop that feeds the screen in, so the model never has to ask for it.

The MCP server answers requests: the model decides it wants to look, spends a call
saying so, and gets an image back. This drives the whole conversation instead. It
captures the display, hands it over with the task, runs whatever actions come back,
and attaches the next frame to the result -- so a screenshot is never something the
model plans, requests, or spends a round trip on. It is simply what it is looking at.

That matters because of where the time goes. Measured on a real session: the actions
take about 0.03s and the model round trip that delivered them takes nine to eighteen
seconds, so the harness is 0.3% of the wall clock. Every call the model spends
finding out what is on screen is a call it did not spend doing anything, and in the
transcripts about 55% of them were exactly that.

Two things follow, and both are implemented here rather than merely requested:
  - Every tool result carries the current screen, so asking is never necessary.
  - An identical frame is replaced by one line of text, so a static desktop costs
    nothing to keep looking at, and looking again visibly buys nothing.

Requires the `anthropic` package: pip install "cufast[agent]".
"""

from __future__ import annotations

from cufast.agent.loop import Agent, Result, Turn
from cufast.agent.prompt import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, SYSTEM_PROMPT
from cufast.agent.schema import computer_tool_schema

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    "Agent",
    "Result",
    "Turn",
    "computer_tool_schema",
]
