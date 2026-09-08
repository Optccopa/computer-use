# TODO

Standing instructions and outstanding work, so nothing gets lost across a long run.

## Standing instructions (apply to everything)

- **Optimise for speed.** Batching and not re-sending unchanged frames beat
  micro-optimisation; a model round trip is 1000–3000 ms against milliseconds of work.
- **Push work into C++ when it actually earns its keep** — not just image encoding.
  The criteria and the measurements are in `ARCHITECTURE.md`; re-read it before
  deciding which side new work belongs on.
- **Read the Anthropic computer-use docs** before changing the tool surface. They
  move: the tool is now the `computer_toolset_20260801` *toolset*, and
  `display_width_px` / `display_height_px` / `display_number` are rejected.
- **Review with subagents, repeatedly, until the issues are dust.** Fix, then review
  again — the fixes themselves introduce bugs (round 1's capture fix created the
  stale-`ref_width` bug in `session.py`).
- Keep the private repo up to date: https://github.com/Optccopa/computer-use
- Never commit screenshots — `.gitignore` blocks `*.png` / `*.jpg` because captures
  are pictures of the real desktop.

## Decided

- **Target display: 2** (Windows `\\.\DISPLAY2`, index **1** here — 1080x1920 portrait
  at −1080,−696). Registered via `claude mcp add cufast -e CUFAST_DISPLAY=1`.
- **Claude picks the display at runtime** — `display` parameter on the `computer` tool,
  persists until changed. `screen_info` lists both the index and the Windows name.
- **Separate cursor: dropped** ("nvm"). Windows has one cursor per session and Home
  edition has no RDP host / Sandbox / Hyper-V, so it was not buildable as asked.
- **Target apps: Discord, YouTube, general desktop** — no games, so raw-input
  compatibility is not a constraint.
- Screenshot box stays 1024x768 (→1024x576 on 16:9), JPEG q75, with `zoom` as the
  escape hatch for small text.

## Outstanding

### 1. Verify it end to end on display 2
The MCP server is registered and connected, and capture on display 2 is confirmed,
but the tools are not reachable from *this* session (the tool list is fixed at
startup). Needs a session restart, then an actual drive of Discord/YouTube.
`scripts/drive.py` runs the same code path without MCP in the meantime.

### 2. Second review round
Round 1 found 4 critical bugs across capture, input, and the Python layer, all fixed.
Re-run the same four reviewers over the rewritten files — capture.cpp, input.cpp,
image.cpp and the Python layer all changed substantially since they were read.

### 3. The tool set that was deferred
Original instruction was "write a simple shell, get it working, then add all of these":

- **`wait_for_change`** — C++. `AcquireNextFrame` blocks in the driver, so this is one
  blocking call rather than a poll loop. The DXGI dirty rects are already tracked.
  Biggest remaining latency win: it removes the screenshot/sleep/screenshot pattern.
- **`powershell` / shell exec** — Python. Often 100x faster than driving a GUI.
- **`find_element` / `list_windows`** — C++, using `IUIAutomationCacheRequest` +
  `FindAllBuildCache` so a whole subtree costs one cross-process call instead of one
  per property.
- **`clipboard` get/set** — Python. Makes bulk text entry instant.

### 4. Kill-switch hotkey
The gate exists and every injection path checks it (`set_input_blocked`), and
`PressGuard` now guarantees releases bypass it. Nothing *sets* it yet — needs a
`WH_KEYBOARD_LL` hook on its own thread with a message pump, in C++.

## Known gaps not yet addressed

- `hold_key` holds the single native worker thread for up to 300 s, stalling every
  other tool call on that server.
- No test asserts `_MUTATING` membership, so a new action can be added without a
  settle-delay decision.
- Rotated panels always fall back to GDI (~15–30 ms vs ~1–3 ms). Display 2 is
  portrait, so it is on the slow path — worth revisiting if it matters in practice.
