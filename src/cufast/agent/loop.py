"""The loop: capture, ask, act, attach the next frame, repeat."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

from cufast import _native
from cufast.actions import FLAT_PARAMS, run_batch
from cufast.agent.prompt import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, SYSTEM_PROMPT
from cufast.agent.schema import computer_tool_schema
from cufast.config import Config
from cufast.session import ActionError, Screenshot, Session


@dataclass
class Turn:
    """What one exchange cost and what it produced, for reporting afterwards."""

    text: str = ""
    actions: int = 0
    images: int = 0
    unchanged: int = 0
    seconds: float = 0.0


@dataclass
class Result:
    """The whole run. `stopped_by_user` is separated from `finished` on purpose:
    a run the user halted did not fail and did not succeed."""

    turns: list[Turn] = field(default_factory=list)
    final_text: str = ""
    finished: bool = False
    stopped_by_user: bool = False
    hit_limit: str = ""

    # The frame handed over with the task itself, before the first turn. Counted
    # separately because it does not belong to any turn -- leaving it out had a run
    # against a static desktop report "0 screenshots sent" when it had sent one, and
    # the whole point of this loop is that the number is honest.
    opening_images: int = 0

    @property
    def images_sent(self) -> int:
        return self.opening_images + sum(t.images for t in self.turns)

    @property
    def images_saved(self) -> int:
        """Frames not sent because the screen had not changed."""
        return sum(t.unchanged for t in self.turns)


def _image_block(shot: Screenshot) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": shot.media_type,
            "data": base64.b64encode(shot.data).decode("ascii"),
        },
    }


class Agent:
    """Runs a task on the real desktop, feeding the screen in on every turn."""

    def __init__(
        self,
        config: Config | None = None,
        client: Any = None,
        model: str = DEFAULT_MODEL,
        max_turns: int = 40,
        max_seconds: float = 900.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.config = config or Config.from_env()
        self.session = Session(self.config)
        self.model = model
        self.max_turns = max_turns
        self.max_seconds = max_seconds
        self.max_tokens = max_tokens
        self._client = client
        self._owns_kill_switch = False

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Imported here so the MCP server, which is the common case, never needs
            # the API SDK installed at all.
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise RuntimeError(
                    "the agent loop needs the anthropic package: "
                    'pip install "cufast[agent]"'
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def _arm(self) -> None:
        if self.config.kill_switch and not _native.kill_switch_running():
            _native.start_kill_switch()
            self._owns_kill_switch = True

    def _disarm(self) -> None:
        # Mirrors Harness.shutdown: block first so anything mid-flight stops, then
        # release what is held, or a key_down outlives the process and the user is
        # left walking into a wall.
        _native.set_input_blocked(True)
        if self._owns_kill_switch:
            _native.stop_kill_switch()
            self._owns_kill_switch = False
        _native.release_held_input()
        _native.set_input_blocked(False)

    def _current_screen(self) -> list[dict[str, Any]]:
        """The opening view. Only ever called once, before the first turn: after
        that the screen arrives attached to every tool result."""
        shot = self.session.screenshot()
        self.session.mark_delivered(shot)
        return [_image_block(shot)]

    def _run_tool(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], Turn]:
        """Executes one tool call and renders its results as content blocks."""
        turn = Turn()
        from cufast.actions import SCREEN_UNCHANGED, batch_from_call

        flat = {name: params.get(name) for name in FLAT_PARAMS}
        blocks: list[dict[str, Any]] = []
        try:
            batch = batch_from_call(params.get("action"), params.get("actions"), flat)
            results = run_batch(
                self.session, batch, params.get("auto_screenshot", True)
            )
        except ActionError as exc:
            return [{"type": "text", "text": f"Error: {exc}"}], turn

        turn.actions = len(batch)
        for result in results:
            if result.image is not None:
                turn.images += 1
                label = f"{result.label}:"
                if result.text:
                    label = f"{label} {result.text}"
                blocks.append({"type": "text", "text": label})
                blocks.append(_image_block(result.image))
            else:
                if result.text and result.text.startswith(SCREEN_UNCHANGED[:20]):
                    turn.unchanged += 1
                blocks.append({"type": "text", "text": f"{result.label}: {result.text}"})
        return blocks, turn

    def run(self, task: str) -> Result:
        """Drives the task to completion, a stop, or a limit.

        Every exit path goes through _disarm, including an exception: leaving the
        desktop with a button down because the loop raised is the one outcome that
        makes the machine unusable rather than merely unfinished.
        """
        result = Result()
        started = time.monotonic()
        client = self._ensure_client()
        tools = [computer_tool_schema()]

        opening = self._current_screen()
        result.opening_images = len(opening)
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    *opening,
                    {"type": "text", "text": task},
                ],
            }
        ]

        self._arm()
        try:
            for _ in range(self.max_turns):
                if _native.input_blocked():
                    result.stopped_by_user = True
                    break
                if time.monotonic() - started > self.max_seconds:
                    result.hit_limit = f"ran past the {self.max_seconds:g}s budget"
                    break

                turn_started = time.monotonic()
                # Streamed because a long plan plus a full batch can take a while to
                # generate, and a non-streaming request that size risks the request
                # timeout rather than merely being slow.
                with client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    thinking={"type": "adaptive"},
                    messages=messages,
                ) as stream:
                    reply = stream.get_final_message()

                turn = Turn(seconds=time.monotonic() - turn_started)
                messages.append({"role": "assistant", "content": reply.content})

                calls = [b for b in reply.content if getattr(b, "type", "") == "tool_use"]
                said = " ".join(
                    b.text for b in reply.content if getattr(b, "type", "") == "text"
                )
                turn.text = said

                if not calls:
                    # No tool call means it is finished, blocked, or asking. Either
                    # way the loop has nothing left to drive.
                    result.final_text = said
                    result.finished = True
                    result.turns.append(turn)
                    break

                tool_results: list[dict[str, Any]] = []
                for call in calls:
                    blocks, sub = self._run_tool(dict(call.input))
                    turn.actions += sub.actions
                    turn.images += sub.images
                    turn.unchanged += sub.unchanged
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": blocks,
                        }
                    )
                result.turns.append(turn)
                messages.append({"role": "user", "content": tool_results})
            else:
                result.hit_limit = f"used all {self.max_turns} turns"

            if _native.input_blocked():
                result.stopped_by_user = True
        finally:
            self._disarm()
        return result
