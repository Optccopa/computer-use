"""MCP server exposing the harness over stdio.

Run directly, or wire into Claude Code with:

    claude mcp add cufast -- <repo>/.venv/Scripts/python.exe -m cufast.server
"""

from __future__ import annotations

import asyncio
import atexit
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
  mouse_move_rel    -- {"dx": px, "dy": px, "steps": 1}. Move BY a delta rather than
                       to a position, in screenshot pixels. Positive dx is right,
                       positive dy is down. See POINTER-LOCKED APPS below.
  aim               -- {"coordinate": [x, y]}. Turn the view so that whatever is at
                       that screenshot pixel ends up on the crosshair. THIS IS THE
                       ONE TO USE IN A 3D GAME: say where the thing is, not how far
                       to turn. No setup -- the first call measures the mouse
                       sensitivity itself, on both axes separately, and remembers
                       it. Re-measures by itself if the display mode changes.
  look              -- {"yaw": deg, "pitch": deg}. Turn by an angle, for turning to
                       something you cannot currently see ("turn around" is yaw 180).
                       Positive yaw is right, positive pitch is down. Needs
                       `calibrate` first; `aim` does not.
  calibrate         -- {"aim_ratio": n, "look_degrees_per_pixel": n}. Optional. Only
                       needed for `look`, or to override what `aim` measured.
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
  hold_key          -- {"text": chord, "duration": seconds up to 300}. Blocks for the
                       whole duration; use key_down/key_up to hold across actions.
  key_down          -- {"text": chord}. Presses and does NOT release. The key stays
                       down across later calls until key_up, so you can walk forward
                       while turning the camera. Always release what you press.
  key_up            -- {"text": chord}. Releases a key_down.
  wait              -- {"duration": seconds up to 300}. A fixed sleep. Prefer
                       wait_for_change whenever you are waiting for something to
                       happen rather than for a known amount of time.
  wait_for_change   -- {"duration": seconds}. Blocks until the screen actually
                       changes, and reports how long that took. Returns the moment
                       it happens, so it is both faster than a guessed sleep and
                       tells you when nothing happened at all -- which a sleep
                       cannot. Use it after anything whose duration you do not
                       know: a page loading, a block breaking, a menu opening.

POINTER-LOCKED APPS AND 3D GAMES (Minecraft and similar).
Such an app hides the cursor and warps it back to the window centre every frame. It
therefore has no cursor position to move to, and reads mouse *deltas* instead. The
symptoms of using the wrong action are specific and worth recognising:
  - `cursor_position` keeps reporting the exact centre of the screenshot no matter
    what you do. That is how you know the pointer is locked.
  - `mouse_move` to a coordinate turns the view, but repeating the identical
    `mouse_move` turns it again by the same amount instead of doing nothing.
Once you see either, switch to `mouse_move_rel` and stop reasoning about position.

AIMING. Use `aim` and do not compute mouse deltas by hand. The first `aim` turns
the view slightly, measures how far the image actually moved, works out the
sensitivity from that, and folds the measurement into the turn it was asked for --
so it costs nothing extra and you never see it. It reports the ratio it found. If
the view is featureless at that moment (facing a wall, or straight at the sky) it
says so rather than guessing; face something with detail and repeat.

`look` still needs `calibrate` with look_degrees_per_pixel: turn by a known amount,
read the angle off an in-game readout (Minecraft: F3 shows Yaw and Pitch), and
divide. `aim` needs none of this.

`aim` is near-exact for anything within the middle of the view and lands slightly
short for something at the very edge, because a perspective view is a tangent rather
than a scale. If you miss, aim again -- by then it is near the centre, where it is
exact. Do not go back to guessing pixel deltas.

To move and look at once: `key_down` w, then `aim` or `look` in later actions or
calls, then `key_up` w. Do not use `hold_key` for this -- it blocks until it ends.
Every key you press with key_down stays down until you release it; the result of
each action tells you what is currently held.

Relative movement stays on the display you are controlling. In a pointer-locked
game that costs nothing, because the cursor is warped back to centre every frame and
never travels. Outside one, a delta that would take the cursor onto another monitor
is refused and the cursor put back -- to reach a position use a coordinate, and to
control another monitor pass `display`.

LIMITS ON ONE CALL. At most 64 actions, at most 10 captures, and at most 8000
characters in one `type`. These are not restrictions on what you may click; they
stop a single batch from occupying the harness, which runs one batch at a time. Going
over is refused rather than truncated, so split the work instead.

`steps` splits one delta into several sends a couple of milliseconds apart. Leave it
at 1 for a game that accumulates deltas per frame, which is most of them. Raise it if
a large turn comes out smaller than the calibration predicts, which means the game
is clamping how far the view can move in a single frame.

WORKING FAST. Measured on a real session: the actions take about 0.03 s per call and
the round trip that delivered them takes about 9 s. Nothing you can do to the actions
matters; the only thing that matters is making fewer calls. So:
  - Put the whole plan in one call. "Aim at the tree, hold left mouse, wait 3s,
    release, screenshot" is one call, not five.
  - Do not take a screenshot to confirm something you can already predict. Screenshot
    when you genuinely do not know what happened.
  - Pass auto_screenshot=false on a call whose result you do not need to see.
  - Never issue a turn, look, correct, look again. That is three round trips for one
    decision, and it is what `aim` exists to prevent. If you catch yourself sending
    mouse_move_rel at a target you can see, use `aim` instead.

A good game batch looks like this -- one call, six actions:
  [{"action": "aim", "coordinate": [430, 250]},
   {"action": "left_mouse_down"},
   {"action": "wait", "duration": 3},
   {"action": "left_mouse_up"},
   {"action": "key_down", "text": "w"},
   {"action": "wait", "duration": 1.5},
   {"action": "key_up", "text": "w"}]

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
        # Started here rather than lazily on first use: the stop button has to exist
        # before the first action can run, not after it. A failure to install is
        # fatal on purpose -- running an input-injecting server with no way to stop
        # it is worse than not starting.
        if config.kill_switch:
            _native.start_kill_switch()

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
            if _native.input_blocked():
                lines.append("KILL SWITCH ENGAGED -- input is blocked until the user "
                             "presses Ctrl+Esc again. Do not attempt to act.")
            elif _native.kill_switch_running():
                lines.append("Kill switch armed: the user can press Ctrl+Esc to stop you.")
            else:
                lines.append("Kill switch is NOT running; the user has no stop button.")
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
        # Order matters. Blocking input first makes any batch still running abort at
        # its next action or wait slice -- waits are sliced precisely so this works.
        # Tearing the hook down first instead meant a 300s wait could never be
        # interrupted, ran to completion, and then pressed a key AFTER the release
        # had already happened, leaving it held with no stop button left.
        _native.set_input_blocked(True)
        # wait=True so the worker is finished before its keys are released; it does
        # not add delay, because blocking input is what ends the batch.
        self._executor.shutdown(wait=True)
        _native.stop_kill_switch()
        # Anything key_down left holding outlives this process otherwise: the OS has
        # no idea the key belonged to us, so it stays down until someone taps it.
        _native.release_held_input()
        _native.set_input_blocked(False)


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

    # Exposed so main() can shut the harness down on the way out. Deliberately not
    # an atexit hook registered here: build_server is what the tests call, and an
    # atexit hook would have the real native release run at the end of a test
    # session, which is the one thing the suite must never do.
    server.cufast_harness = harness
    return server


def main() -> None:
    try:
        server = build_server()
    except ValueError as exc:
        print(f"cufast: bad configuration: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # A stdio server exits when its client closes the pipe, which for a game is
    # whenever the session ends -- possibly with W still held from a key_down. The
    # OS does not know that key belonged to this process, so without this it stays
    # down and the user walks into a wall.
    atexit.register(server.cufast_harness.shutdown)
    try:
        server.run()
    finally:
        server.cufast_harness.shutdown()


if __name__ == "__main__":
    main()
