# TODO

Standing instructions and outstanding work, so nothing gets lost across a long run.

## Standing instructions (apply to everything)

- **Optimise for speed, but measure first.** The harness is 0.3% of wall clock. A
  screenshot is 2.6 ms; a model round trip is 9–18 s. The only lever that moves the
  number is *fewer tool calls*. Micro-optimising capture is wasted work.
- **Push work into C++ when it actually earns its keep** — pixels, unwind-safe OS
  state, blocking OS primitives, repeated cross-process boundaries. Criteria and
  measurements are in `ARCHITECTURE.md`; re-read it before deciding which side new
  work belongs on.
- **Read the Anthropic computer-use docs** before changing the tool surface. The tool
  is now the `computer_toolset_20260801` *toolset*, and `display_width_px` /
  `display_height_px` / `display_number` are rejected.
- **Review with subagents, repeatedly, until the issues are dust.** Fix, then review
  again — the fixes themselves introduce bugs. Round 1's capture fix created the
  stale-`ref_width` bug; the alias work created the settle bug in `run_batch`.
- **A primitive with a setup ritual does not get used.** `aim` sat at zero uses in
  233 calls because it demanded a calibration first. Anything new must work on the
  first call with no preparation.
- Keep the private repo up to date: https://github.com/Optccopa/computer-use
- Never commit screenshots — `.gitignore` blocks `*.png` / `*.jpg` because captures
  are pictures of the real desktop.
- Tests must never inject real input or install a real keyboard hook. The `fake_input`
  and autouse `no_real_kill_switch` fixtures enforce this.

## Decided

- **Claude picks the display at runtime** — `display` parameter, persists until
  changed. `screen_info` lists index and Windows device name.
- **Kill switch is Ctrl+Esc**, latching, released by pressing it again. Engaging it
  releases every held key and mouse button. While the hook is installed Ctrl+Esc no
  longer opens the Start menu — that is the point.
- **No shell tool.** Claude Code already has one; a second inside the harness is
  redundant.
- **Separate cursor: dropped.** Windows has one cursor per session and Home edition
  has no RDP host / Sandbox / Hyper-V.
- **Screenshot box stays 1024x768** (→1024x576 on 16:9), JPEG q75. It is Anthropic's
  documented recommendation, and `zoom` covers small text. Tunable per run with
  `CUFAST_MAX_WIDTH` / `CUFAST_MAX_HEIGHT` — that is the right place for the trade.
- **Games are in scope.** Pointer-locked applications need relative movement; absolute
  positioning cannot express what they read.

## Measured, so it does not get re-litigated

From a real hour-long Minecraft session (233 calls) and `scripts/bench.py`:

| | |
|---|---|
| full screenshot → 1024x768 box | 2.62 ms |
| JPEG encode alone | 1.24 ms |
| zoom of a 480x270 region | 0.34 ms |
| MCP round trip | 10–18 ms |
| **model round trip** | **9–18 s** |
| harness share of wall clock | **0.3%** |
| context growth over an hour | 53k → 310k tokens (~55% screenshots) |
| call time, first third → last third | 7.5 s → 18.1 s |

## Outstanding

### 1. Second review round
In progress. Round 1 found 4 critical bugs; everything has been rewritten since.

### 2. UIA `find_element` / `list_windows`
C++, using `IUIAutomationCacheRequest` + `FindAllBuildCache` so a whole subtree costs
one cross-process call instead of one per property. For desktop work this replaces a
screenshot-and-hunt cycle with one query — the same win `aim` gave games.

### 3. `clipboard` get/set
Python. Makes bulk text entry instant instead of a keystroke at a time.

### 4. Drive display 2 end to end
Registered, capture-verified, rotation now on the DXGI path. Never actually driven.

## Known gaps not yet addressed

- `hold_key` occupies the single native worker for up to 300 s, stalling every other
  tool call on that server. `wait` no longer does — it slices and checks the kill
  switch — but `hold_key`'s slicing is in C++ and does not release the worker.
- The `Session` cache in `Harness` never evicts, so every display ever selected keeps
  a D3D11 device alive.
- A pointer-locked game stomps absolute positioning process-wide: while Minecraft has
  focus, `mouse_move` to another display fails with "cursor would not move".

## Side effects on the developer's machine (from agent runs, not from the code)

- Minecraft options were changed by an earlier session: **Raw Input OFF, sensitivity
  100% → 27%, Auto-Jump ON**. Raw Input should go back ON now that `mouse_move_rel`
  exists — raw input bypasses Windows pointer ballistics, so deltas are exactly 1:1.
- One existing Creative world has a `/give`n diamond and the "Diamonds!" advancement.
