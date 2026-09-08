"""End-to-end MCP check: spawns the server over stdio and drives it as a client.

Only read-only actions are used (screenshot, zoom, cursor_position), so running this
never moves the mouse or types anything.

Run: .venv/Scripts/python.exe scripts/smoke_mcp.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cufast.server"],
        cwd=str(REPO),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            for tool in tools.tools:
                schema = tool.input_schema.get("properties", {})
                print(f"  {tool.name}({', '.join(schema)})")

            print("\n--- screen_info ---")
            info = await session.call_tool("screen_info", {})
            for block in info.content:
                if block.type == "text":
                    print(block.text)

            print("\n--- computer: screenshot ---")
            start = time.perf_counter()
            result = await session.call_tool(
                "computer", {"actions": [{"action": "screenshot"}]}
            )
            elapsed = (time.perf_counter() - start) * 1000
            images = [b for b in result.content if b.type == "image"]
            texts = [b for b in result.content if b.type == "text"]
            print(f"round trip {elapsed:.1f} ms, {len(images)} image(s), {len(texts)} text(s)")
            for block in texts:
                print("  ", block.text)
            for block in images:
                # base64 inflates by 4/3; report the wire size the model actually pays for.
                print(f"   image {block.mime_type}, {len(block.data) / 1024:.1f} KiB base64")

            print("\n--- computer: batch of read-only actions ---")
            start = time.perf_counter()
            result = await session.call_tool(
                "computer",
                {
                    "actions": [
                        {"action": "cursor_position"},
                        {"action": "zoom", "region": [0, 0, 300, 200]},
                    ]
                },
            )
            elapsed = (time.perf_counter() - start) * 1000
            print(f"round trip {elapsed:.1f} ms")
            for block in result.content:
                if block.type == "text":
                    print("  ", block.text)
                else:
                    print(f"   image {block.mime_type}, {len(block.data) / 1024:.1f} KiB base64")

            print("\n--- error handling: a bad action must not kill the server ---")
            result = await session.call_tool(
                "computer", {"actions": [{"action": "left_click", "coordinate": [9999, 9999]}]}
            )
            for block in result.content:
                if block.type == "text":
                    print("  ", block.text[:160])

            print("\n--- server still alive ---")
            result = await session.call_tool(
                "computer", {"actions": [{"action": "cursor_position"}], "auto_screenshot": False}
            )
            for block in result.content:
                if block.type == "text":
                    print("  ", block.text)

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
