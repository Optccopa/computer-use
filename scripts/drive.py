"""Run an action batch against a display without going through MCP.

Same code path the MCP server uses, minus the transport -- handy for manual testing
and for driving the harness from a shell.

    python scripts/drive.py --display 1 '[{"action": "screenshot"}]'
    python scripts/drive.py --display 1 --out-dir shots '[
        {"action": "left_click", "coordinate": [200, 400]},
        {"action": "type", "text": "hello"}
    ]'

Images are written to --out-dir (default: a temp folder) and their paths printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from cufast.actions import run_batch
from cufast.config import Config
from cufast.session import ActionError, Session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("actions", help="JSON list of actions, or '-' to read stdin")
    parser.add_argument("--display", type=int, default=None, help="display index")
    parser.add_argument("--max-width", type=int, default=None)
    parser.add_argument("--max-height", type=int, default=None)
    parser.add_argument("--out-dir", default=None, help="where to write screenshots")
    parser.add_argument(
        "--no-auto-screenshot",
        action="store_true",
        help="do not append a screenshot after the last action",
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.actions == "-" else args.actions
    try:
        actions = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"actions is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(actions, list):
        actions = [actions]

    base = Config.from_env()
    config = Config(
        display_index=args.display if args.display is not None else base.display_index,
        max_width=args.max_width or base.max_width,
        max_height=args.max_height or base.max_height,
        jpeg_quality=base.jpeg_quality,
        draw_cursor=base.draw_cursor,
        capture_timeout_ms=base.capture_timeout_ms,
        settle_ms=base.settle_ms,
    )
    config.validate()

    out_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.gettempdir()) / "cufast"
    out_dir.mkdir(parents=True, exist_ok=True)

    session = Session(config)
    print(session.describe(), file=sys.stderr)

    started = time.perf_counter()
    try:
        results = run_batch(session, actions, auto_screenshot=not args.no_auto_screenshot)
    except ActionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    elapsed = (time.perf_counter() - started) * 1000

    stamp = time.strftime("%H%M%S")
    failed = False
    for index, result in enumerate(results):
        if result.image is not None:
            path = out_dir / f"{stamp}_{index}_{result.label}.jpg"
            path.write_bytes(result.image.data)
            print(
                f"[{index}] {result.label}: {result.image.width}x{result.image.height}, "
                f"{len(result.image.data) / 1024:.1f} KiB -> {path}"
            )
        else:
            print(f"[{index}] {result.label}: {result.text}")
            failed = failed or result.is_error

    print(f"batch took {elapsed:.1f} ms", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
