"""Runtime configuration, overridable from the environment.

Every value that trades image fidelity against token cost lives here, because that
is the only knob in this project worth tuning per machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Anthropic's computer-use docs recommend 1024x768 (XGA) or 1280x720 for desktop
# work and warn against going above 1920x1080. On a 16:9 display the XGA box fits
# to 1024x576 (~777 visual tokens); 1280x720 keeps text noticeably more readable
# for ~1196. Current models accept up to a 2576px long edge and 4784 visual tokens,
# so neither is near the ceiling -- this is a cost choice, not a limit.
DEFAULT_MAX_WIDTH = 1024
DEFAULT_MAX_HEIGHT = 768


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    # Every other CUFAST_* variable reports a typo; silently reading "tru" as false
    # would quietly disable the cursor overlay with no way to notice.
    raise ValueError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Config:
    display_index: int = 0
    max_width: int = DEFAULT_MAX_WIDTH
    max_height: int = DEFAULT_MAX_HEIGHT
    jpeg_quality: float = 0.75
    draw_cursor: bool = True

    # Installs the Ctrl+Esc kill switch. While it runs, Ctrl+Esc no longer opens the
    # Start menu: the low-level hook sees the chord first and swallows it, which is
    # the point -- the stop button must not also be a shortcut the focused
    # application can act on.
    kill_switch: bool = True

    # Makes display_index a boundary instead of a default. Off by default because
    # driving several monitors is a documented feature of the tool; an operator who
    # wants the agent confined to one display has no other way to say so, since the
    # `display` parameter otherwise walks straight past the containment that
    # confine_cursor enforces for the cursor.
    lock_display: bool = False

    # How long a screenshot waits for the compositor to present a new frame. On a
    # timeout the previous frame is reused, which is correct: nothing changed.
    # One 60Hz frame is the useful ceiling.
    capture_timeout_ms: int = 16

    # Pause between an input action and the screenshot that follows it in the same
    # batch, giving the UI a chance to repaint. Overridden per call by wait actions.
    settle_ms: int = 40

    def __post_init__(self) -> None:
        # Validating here rather than only in from_env() matters: build_server()
        # accepts a caller-supplied Config, and an unvalidated one reaches the native
        # layer and desynchronises coordinates with no error anywhere.
        self.validate()

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            display_index=_env_int("CUFAST_DISPLAY", 0),
            max_width=_env_int("CUFAST_MAX_WIDTH", DEFAULT_MAX_WIDTH),
            max_height=_env_int("CUFAST_MAX_HEIGHT", DEFAULT_MAX_HEIGHT),
            jpeg_quality=_env_float("CUFAST_JPEG_QUALITY", 0.75),
            draw_cursor=_env_bool("CUFAST_DRAW_CURSOR", True),
            kill_switch=_env_bool("CUFAST_KILL_SWITCH", True),
            lock_display=_env_bool("CUFAST_LOCK_DISPLAY", False),
            capture_timeout_ms=_env_int("CUFAST_CAPTURE_TIMEOUT_MS", 16),
            settle_ms=_env_int("CUFAST_SETTLE_MS", 40),
        )
        return cfg

    def validate(self) -> None:
        if self.max_width < 1 or self.max_height < 1:
            raise ValueError("CUFAST_MAX_WIDTH and CUFAST_MAX_HEIGHT must be positive")
        # Beyond a 2576px long edge the API resizes the image itself, which would
        # silently desynchronise the coordinates we hand back from the ones the
        # model sees.
        if max(self.max_width, self.max_height) > 2576:
            raise ValueError(
                "screenshot long edge must stay at or below 2576px; above that the "
                "API rescales the image and coordinates no longer line up"
            )
        if not 0.01 <= self.jpeg_quality <= 1.0:
            raise ValueError("CUFAST_JPEG_QUALITY must be between 0.01 and 1.0")
        if self.display_index < 0:
            raise ValueError("CUFAST_DISPLAY must not be negative")
        if self.capture_timeout_ms < 0:
            raise ValueError("CUFAST_CAPTURE_TIMEOUT_MS must not be negative")
        if self.settle_ms < 0:
            raise ValueError("CUFAST_SETTLE_MS must not be negative")
