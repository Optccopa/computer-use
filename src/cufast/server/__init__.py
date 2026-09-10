"""MCP server exposing the harness over stdio.

Run directly, or wire into Claude Code with:

    claude mcp add cufast -- <repo>/.venv/Scripts/python.exe -m cufast.server

Split into three: the tool description is prose and changes for its own reasons,
the harness owns the sessions and the one native worker, and the app is the MCP
surface over both.
"""

from __future__ import annotations

from cufast.server.app import build_server, main
from cufast.server.description import TOOL_DESCRIPTION, tool_description
from cufast.server.harness import Harness

__all__ = ["TOOL_DESCRIPTION", "Harness", "build_server", "main", "tool_description"]


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
