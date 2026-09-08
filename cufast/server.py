"""MCP server exposing the harness over stdio.

Run directly, or wire into Claude Code with:

    claude mcp add cufast -- <repo>/.venv/Scripts/python.exe -m cufast.server
"""

from __future__ import annotations

import asyncio
import base64
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from cufast import _native
from cufast.actions import run_batch
from cufast.config import Config
from cufast.session import ActionError, Session

TOOL_DESCRIPTION = """\
Control this Windows desktop: take screenshots and drive the real mouse and keyboard.

Pass an ORDERED LIST of actions in `actions`. They run sequentially in one call, so
a click, the text that follows it, and the screenshot that confirms the result cost
one round trip instead of three. Prefer one batch over several calls -- the actions
themselves take single-digit milliseconds, so nearly all elapsed time is round trips.
If an action fails, the ones after it are not run and are reported as such.

A screenshot is appended automatically after the last action unless the batch already
ends with `screenshot` or `zoom`, or you pass auto_screenshot=false.

Set `display` to control a different monitor (see screen_info for what is attached).
It persists for later calls until you change it again.

COORDINATES. Every coordinate is in the pixel space of the screenshot you were given,
origin top-left. That is a scaled-down view of the display, so never use the native
display resolution. `zoom` does NOT change this: after zooming, still click using
full-screenshot coordinates.

ACTIONS (each item is an object with "action" plus that action's parameters):
  screenshot        -- {}. Capture the display.
  zoom              -- {"region": [x0, y0, x1, y1]}. Re-capture that rectangle at full
                       resolution. Use it whenever text is too small to read reliably:
                       file names, tab titles, status bars, button labels, line numbers.
  left_click        -- {"coordinate": [x, y] (optional), "text": modifiers (optional)}
  right_click, middle_click, double_click, triple_click -- same shape as left_click.
                       Omit coordinate to act at the current cursor position. `text`
                       holds modifier keys, e.g. "shift" or "ctrl+shift".
  left_click_drag   -- {"start_coordinate": [x, y], "coordinate": [x, y], "text": mods}
  mouse_move        -- {"coordinate": [x, y]}. Move without clicking, e.g. to hover.
  left_mouse_down / left_mouse_up -- {}. Act at the current cursor position.
  cursor_position   -- {}. Reports the cursor as "X=..., Y=..." in screenshot space.
  scroll            -- {"scroll_direction": "up"|"down"|"left"|"right",
                        "scroll_amount": wheel clicks (0-1000),
                        "coordinate": [x, y] (optional), "text": modifiers (optional)}
  type              -- {"text": "..."}. Types literal text. Newlines and tabs are sent
                       as real Return and Tab keystrokes.
  key               -- {"text": "Return" | "ctrl+s" | "alt+Tab", "repeat": 1-100}.
                       X11 keysym names: Return, Tab, Escape, BackSpace, Delete, Home,
                       End, Page_Up, Page_Down, Up, Down, Left, Right, F1-F24, space.
  hold_key          -- {"text": chord, "duration": seconds up to 300}
  wait              -- {"duration": seconds up to 300}. For a slow app to finish loading.

Dropdowns and scrollbars are often easier to drive with keyboard shortcuts than with
the mouse. If an action does not appear to have worked, take a screenshot and check
before continuing rather than assuming.
"""


class Harness:
    """Holds the sessions and funnels every native call onto one thread.

    Direct3D's immediate context and the duplication object want a single owner, so
    rather than synchronising them across whatever thread the event loop offers, all
    native work is queued onto one dedicated worker. That includes display
    enumeration, which is a real Win32 call and does not belong on the event loop.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cufast")
        self._sessions: dict[int, Session] = {}
        self._current = config.display_index

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _session_for(self, display: int | None) -> Session:
        index = self._current if display is None else display
        session = self._sessions.get(index)
        if session is None:
            # Cached because constructing a Session builds a D3D11 device and a
            # duplication object; switching displays should not pay that twice.
            session = Session(replace(self.config, display_index=index))
            self._sessions[index] = session
        # Only commit the switch once the session actually exists, so a bad index
        # does not leave the harness pointing at a display it could not open.
        self._current = index
        return session

    async def session(self, display: int | None = None) -> Session:
        return await self._call(self._session_for, display)

    async def run(self, actions: list[dict[str, Any]], auto_screenshot: bool,
                  display: int | None):
        def work():
            return run_batch(self._session_for(display), actions, auto_screenshot)

        return await self._call(work)

    async def describe(self, display: int | None) -> str:
        def work():
            session = self._session_for(display)
            lines = [session.describe(), "", "attached displays:"]
            for entry in _native.list_displays():
                marker = " (primary)" if entry["primary"] else ""
                active = " <- controlled" if entry["index"] == session.screen.index else ""
                lines.append(
                    f"  index {entry['index']} = Windows {entry['device']}: "
                    f"{entry['width']}x{entry['height']} "
                    f"at ({entry['x']},{entry['y']}){marker}{active}"
                )
            lines.append("")
            lines.append(
                "Pass `display` to the computer tool to control a different one. "
                "The index here is zero-based, so Windows DISPLAY2 is index 1."
            )
            if not _native.dpi_per_monitor_aware():
                lines.append("")
                lines.append(
                    "WARNING: per-monitor DPI awareness is not active, so coordinates "
                    "may be virtualized on a scaled display."
                )
            return "\n".join(lines)

        return await self._call(work)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


def build_server(config: Config | None = None) -> MCPServer:
    cfg = config or Config.from_env()
    cfg.validate()
    harness = Harness(cfg)

    server = MCPServer(
        name="cufast",
        instructions=(
            "Fast Windows computer-use harness. Batch actions into a single `computer` "
            "call whenever possible; each call is a network round trip while the actions "
            "themselves take milliseconds."
        ),
    )

    @server.tool(name="computer", description=TOOL_DESCRIPTION)
    async def computer(
        actions: list[dict[str, Any]],
        auto_screenshot: bool = True,
        display: int | None = None,
    ) -> list[TextContent | ImageContent]:
        try:
            results = await harness.run(actions, auto_screenshot, display)
        except ActionError as exc:
            raise ToolError(str(exc)) from None
        except (RuntimeError, ValueError) as exc:
            raise ToolError(f"computer failed: {exc}") from None

        blocks: list[TextContent | ImageContent] = []
        for index, result in enumerate(results):
            if result.image is not None:
                blocks.append(TextContent(type="text", text=f"[{index}] {result.label}:"))
                blocks.append(
                    ImageContent(
                        type="image",
                        data=base64.b64encode(result.image.data).decode("ascii"),
                        mime_type=result.image.media_type,
                    )
                )
            else:
                blocks.append(
                    TextContent(type="text", text=f"[{index}] {result.label}: {result.text}")
                )

        failures = [f"[{i}] {r.label}: {r.text}" for i, r in enumerate(results) if r.is_error]
        if failures:
            # ToolError sets the protocol-level error flag but discards all content,
            # so it is only used when nothing was captured. When an image exists it is
            # worth more to the model than the flag -- it shows what the screen
            # actually looks like after the failure -- so the failure is reported as a
            # leading text block instead.
            if all(r.is_error for r in results):
                raise ToolError("\n".join(failures))
            blocks.insert(
                0,
                TextContent(
                    type="text",
                    text="BATCH FAILED -- later actions were not run:\n" + "\n".join(failures),
                ),
            )
        return blocks

    @server.tool(
        name="screen_info",
        description=(
            "Reports the display being controlled: its native resolution, the size of "
            "the screenshots you receive (the coordinate space you must use), and which "
            "capture path is active. Also lists the attached displays with both their "
            "index here and their Windows device name."
        ),
    )
    async def screen_info(display: int | None = None) -> str:
        try:
            return await harness.describe(display)
        except (ActionError, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from None

    return server


def main() -> None:
    try:
        server = build_server()
    except ValueError as exc:
        print(f"cufast: bad configuration: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    server.run()


if __name__ == "__main__":
    main()
