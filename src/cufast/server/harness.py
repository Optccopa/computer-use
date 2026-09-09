"""Owns the sessions, the single native worker, and the kill switch."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from cufast import _native
from cufast.actions import run_batch
from cufast.config import Config
from cufast.session import ActionError, Session


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

    def _session_for(self, display: int | None, commit: bool = True) -> Session:
        index = self._current if display is None else display
        if display is not None and display != self._current and self.config.lock_display:
            raise ActionError(
                f"this harness is pinned to display {self._current} and will not "
                f"control display {display}. The operator set CUFAST_LOCK_DISPLAY, "
                "which makes the assigned display a boundary rather than a default. "
                "Work with what is on this display, or ask the user to move the "
                "window onto it."
            )
        session = self._sessions.get(index)
        if session is None:
            # Cached because constructing a Session builds a D3D11 device and a
            # duplication object; switching displays should not pay that twice.
            session = Session(replace(self.config, display_index=index))
            self._sessions[index] = session
        # Only commit the switch once the session actually exists, so a bad index
        # does not leave the harness pointing at a display it could not open.
        #
        # commit=False is for describe(): reporting on a display must not silently
        # become controlling it. It used to, which made screen_info a way to retarget
        # the harness -- and it worked while the kill switch was engaged, so a stopped
        # agent could still choose which monitor it would act on the moment the user
        # released the switch.
        if commit:
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
            session = self._session_for(display, commit=False)
            lines = [session.describe(), "", "attached displays:"]
            for entry in _native.list_displays():
                marker = " (primary)" if entry["primary"] else ""
                # Against the display actually being controlled, not the one being
                # asked about. Those are no longer the same thing: describing a
                # display does not switch to it.
                active = " <- controlled" if entry["index"] == self._current else ""
                if entry["index"] == session.screen.index and entry["index"] != self._current:
                    active = " <- described above (not controlled)"
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
            if self.config.lock_display:
                lines.append(
                    f"This harness is PINNED to display {self._current}: the operator "
                    "set CUFAST_LOCK_DISPLAY, so `display` is refused rather than "
                    "honoured. The index here is zero-based, so Windows DISPLAY2 is "
                    "index 1."
                )
            else:
                lines.append(
                    "Pass `display` to the computer tool to control a different one. "
                    "Passing it here only describes that display; it does not switch. "
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

