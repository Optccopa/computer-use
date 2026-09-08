"""MCP server exposing the harness over stdio.

Run directly, or wire into Claude Code with:

    claude mcp add cufast -- <repo>/.venv/Scripts/python.exe -m cufast.server
"""

from __future__ import annotations

import asyncio
import base64
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

from cufast import _native
from cufast.actions import ACTION_NAMES, run_batch
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
                        "scroll_amount": wheel clicks,
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
    """Holds the session and funnels every native call onto one thread.

    Direct3D's immediate context and the duplication object want a single owner, so
    rather than synchronising them across whatever thread the event loop offers, all
    native work is queued onto one dedicated worker.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cufast")
        self._session: Session | None = None

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _ensure_session(self) -> Session:
        if self._session is None:
            self._session = Session(self.config)
        return self._session

    async def session(self) -> Session:
        return await self._call(self._ensure_session)

    async def run(self, actions: list[dict[str, Any]], auto_screenshot: bool):
        def work():
            return run_batch(self._ensure_session(), actions, auto_screenshot)

        return await self._call(work)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


def build_server(config: Config | None = None) -> MCPServer:
    cfg = config or Config.from_env()
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
    ) -> list[TextContent | ImageContent]:
        try:
            results = await harness.run(actions, auto_screenshot)
        except ActionError as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        blocks: list[TextContent | ImageContent] = []
        for index, result in enumerate(results):
            if result.image is not None:
                blocks.append(
                    TextContent(type="text", text=f"[{index}] {result.label}:")
                )
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
        return blocks

    @server.tool(
        name="screen_info",
        description=(
            "Reports the display being controlled: its native resolution, the size of "
            "the screenshots you receive (the coordinate space you must use), and which "
            "capture path is active. Also lists the other attached displays."
        ),
    )
    async def screen_info() -> str:
        session = await harness.session()
        lines = [session.describe(), "", "attached displays:"]
        for display in _native.list_displays():
            marker = " (primary)" if display["primary"] else ""
            active = " <- controlled" if display["index"] == session.screen.index else ""
            lines.append(
                f"  [{display['index']}] {display['width']}x{display['height']} "
                f"at ({display['x']},{display['y']}){marker}{active}"
            )
        lines.append("")
        lines.append(
            "Set CUFAST_DISPLAY to control a different one; "
            "CUFAST_MAX_WIDTH / CUFAST_MAX_HEIGHT change the screenshot size."
        )
        return "\n".join(lines)

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
