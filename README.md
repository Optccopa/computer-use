# cufast

A fast Windows computer-use harness. The hot path — screen capture, downscale, JPEG
encode, and input injection — is C++ behind [nanobind](https://nanobind.readthedocs.io);
Python handles coordinates, action dispatch, and an MCP server you can point Claude
Code at.

Windows only, and deliberately so: it uses DXGI Desktop Duplication, WIC, and
`SendInput` directly rather than going through a portable abstraction.

## Why it is fast

| Stage | Time | How |
|---|---|---|
| Capture 1920x1080 | ~1–3 ms | DXGI Desktop Duplication, device kept warm across calls |
| Downscale to 1024x576 | ~4 ms | Two-pass separable box filter, AVX2 |
| JPEG encode q75 | ~2 ms | WIC, straight from 24bpp BGR — no channel swap |
| **Full screenshot** | **~9 ms** | 70 KiB, ~777 visual tokens |
| MCP round trip | ~28 ms | includes stdio framing and base64 |

Two things mattered more than the rest:

**Batching.** The `computer` tool takes an ordered *list* of actions. A click, the
text after it, and the confirming screenshot cost one round trip instead of three.
The actions take milliseconds; the round trips take seconds. Nothing else in the
project comes close to this as a speed lever.

**Not paying for what does not change.** A timed-out `AcquireNextFrame` means the
screen is idle, so the cached frame is reused. Only the pixels under the previous
cursor are saved and restored, rather than re-copying an 8 MB surface.

## Install

Requires Visual Studio with the C++ workload (MSVC 14.5+), CMake 3.26+, and Python
3.14. The build needs AVX2 — anything from 2013 onward.

```bash
uv venv --python 3.14
uv pip install -e .
uv pip install pytest && .venv/Scripts/python.exe -m pytest -q
```

## Use it from Claude Code

```bash
claude mcp add cufast -- /full/path/to/.venv/Scripts/python.exe -m cufast.server
```

Two tools appear: `computer` and `screen_info`.

```jsonc
{
  "actions": [
    {"action": "left_click", "coordinate": [512, 288]},
    {"action": "type", "text": "hello"},
    {"action": "key", "text": "Return"}
  ]
}
```

A screenshot is appended automatically unless the batch already ends with one.

## Coordinates

Screenshots are scaled down, so **every coordinate is in screenshot space**, never
native display pixels. On a 1920x1080 display with the default 1024x768 box, that
means a 1024x576 frame.

A coordinate well outside the frame is rejected with a message naming both
resolutions rather than being clamped. Clamping would click the right-hand edge and
look like it worked — this is the failure mode worth being loud about.

`zoom` re-captures a region at full resolution for reading small text, and
deliberately does **not** get its own coordinate frame: per the computer-use spec,
zoom images do not change the coordinate space, so clicks after a zoom still use
full-screenshot coordinates.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `CUFAST_DISPLAY` | `0` | `0` is always the primary; see `screen_info` |
| `CUFAST_MAX_WIDTH` | `1024` | Screenshot box, aspect preserved |
| `CUFAST_MAX_HEIGHT` | `768` | |
| `CUFAST_JPEG_QUALITY` | `0.75` | |
| `CUFAST_DRAW_CURSOR` | `true` | Composites the real cursor into the frame |
| `CUFAST_CAPTURE_TIMEOUT_MS` | `16` | One frame at 60Hz |
| `CUFAST_SETTLE_MS` | `40` | Pause after a UI-mutating action |

### Picking a screenshot size

Anthropic's docs recommend 1024x768 (XGA) or 1280x720 for desktop work and warn
against exceeding 1920x1080. On a 16:9 display:

| Box | Actual | Size | Visual tokens |
|---|---|---|---|
| 1024x768 (default) | 1024x576 | 70 KiB | ~777 |
| 1280x720 | 1280x720 | 97 KiB | ~1196 |
| 1920x1080 | 1920x1080 | 248 KiB | ~2691 |

Current models accept a 2576px long edge and 4784 visual tokens, so none of these is
near the ceiling — this is a cost choice. The default trades readability for tokens
on the assumption that `zoom` covers anything too small to read. If you find yourself
zooming constantly, set `CUFAST_MAX_WIDTH=1280 CUFAST_MAX_HEIGHT=720`.

## Layout

```
src/native/           C++ -- becomes cufast._native
  capture.cpp   DXGI Desktop Duplication, GDI fallback, cursor compositing
  image.cpp     box-filter downscale (AVX2), quarter-turn, WIC encode, hash
  input.cpp     SendInput: mouse, keyboard, X11 keysym names, held-key registry
  hotkey.cpp    Ctrl+Esc kill switch (low-level keyboard hook, own thread)
  module.cpp    nanobind bindings
src/cufast/           Python
  session.py    display geometry and coordinate mapping
  actions.py    action dispatch and batch semantics
  server.py     MCP server
  config.py     environment-backed settings
tests/                pytest; input is always stubbed
scripts/              benchmarks and manual drivers
```

## Notes on the tricky parts

**GDI is not just a fallback for old hardware.** Duplication is unavailable on the
secure desktop (UAC prompts, lock screen) and during a session switch, so the GDI
path is what keeps a screenshot working rather than throwing.

**Rotated panels stay on the fast path.** Duplication returns the *unrotated* panel
surface, so a portrait monitor arrives as a landscape image on its side. Rather than
falling back to GDI, the copy out applies a cache-blocked quarter-turn: same image,
a fraction of the cost.

**The first duplication frame is blank.** The first `AcquireNextFrame` after
`DuplicateOutput` reports `LastPresentTime == 0` and hands back a surface the
compositor has not presented into. That case seeds from GDI instead.

**nanobind defaults to `/Os`.** It optimizes binding glue for size, which also
suppressed vectorization across the downscale kernel. `NOMINSIZE` in `CMakeLists.txt`
is load-bearing — without it the change-poll path is 4.6x slower.

**`_mm256_avg_epu8` is exactly a two-pixel box average.** For any downscale between
0.5x and 1.0x every destination pixel spans at most two source pixels, and using the
same index twice makes the one-pixel case fall out of the same instruction, since
`(p + p + 1) >> 1 == p`. The SIMD and scalar paths are asserted bit-identical across
nine scale ratios in `tests/test_image.py`.

**UIPI.** Windows refuses injected input into windows running at higher integrity
than this process. If clicks silently do nothing against an elevated app, that is
why; `SendInput` returning short is surfaced as an error rather than ignored.

## Containment

What the model may do here is deliberately broad: clicking destructive buttons,
typing into terminals and reading whatever is on screen are all normal computer use,
and none of it is gated. The boundaries are about the harness, not about the desktop:

- **Kill switch.** A `WH_KEYBOARD_LL` hook on its own thread watches for Ctrl+Esc and
  sets the `set_input_blocked` gate that every injection path checks. It ignores
  injected keystrokes, so the agent cannot press its own stop button, and engaging it
  releases whatever was held. Every batch is refused while it is engaged — screenshots
  included — and it is re-checked between actions, because `key_up` and
  `left_mouse_up` deliberately bypass the gate so recovery is never the thing blocked.
- **Display.** Coordinates are bounds-checked against the controlled display, and
  relative moves that would walk the cursor off it are undone and refused. `display`
  selects a monitor per call; set `CUFAST_LOCK_DISPLAY=true` to make the configured
  display a boundary rather than a default. `screen_info` describes another display
  without switching to it.
- **One call.** At most 64 actions, 10 returned images, 8000 typed characters, and
  ~600s of estimated occupancy — waits, `steps` and typing all count, because the
  harness runs one batch at a time and an unbounded batch is an unbounded outage.
- **Screen content is untrusted.** The tool description says so explicitly: a
  screenshot is data the model is looking at, not instructions it has received.

## Not yet built

A shell tool, UI Automation element lookup, and clipboard access.
