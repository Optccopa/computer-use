"""Command line for the agent loop.

Deliberately Python and not part of the C++ CLI: this one talks to the Claude API
and spends money, and it is not a diagnostic. The native `cufast` binary answers
"what is the machine doing"; this answers "go and do something".
"""

from __future__ import annotations

import argparse
import sys

from cufast.agent.loop import Agent
from cufast.agent.prompt import DEFAULT_MODEL
from cufast.config import Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cufast-agent",
        description="Run a task on this desktop, feeding the screen in every turn.",
    )
    parser.add_argument("task", help="what to do, in plain language")
    parser.add_argument("--display", type=int, default=None,
                        help="which monitor to control (default: CUFAST_DISPLAY)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-seconds", type=float, default=900.0)
    # Off by default because this drives the real mouse and keyboard on a real
    # desktop and every turn costs money. Making someone type it is the point.
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = Config.from_env()
    if args.display is not None:
        from dataclasses import replace

        config = replace(config, display_index=args.display)

    if not args.yes:
        print(f"About to drive display {config.display_index} of this machine, with "
              f"{args.model}, for up to {args.max_turns} turns.")
        print("The mouse and keyboard will move. Ctrl+Esc stops it at any time.")
        print(f"Task: {args.task}")
        if input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    agent = Agent(
        config=config,
        model=args.model,
        max_turns=args.max_turns,
        max_seconds=args.max_seconds,
    )
    result = agent.run(args.task)

    print()
    if result.final_text:
        print(result.final_text)
    print()
    actions = sum(t.actions for t in result.turns)
    seconds = sum(t.seconds for t in result.turns)
    print(f"{len(result.turns)} turns, {actions} actions, {seconds:.0f}s of model time.")
    # The number this whole design exists to move: frames not sent because the model
    # already had them.
    print(f"{result.images_sent} screenshots sent, {result.images_saved} suppressed as "
          "unchanged.")
    if result.stopped_by_user:
        print("Stopped by the kill switch.")
        return 1
    if result.hit_limit:
        print(f"Stopped early: {result.hit_limit}.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
