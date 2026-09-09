# cufast

Fast Windows computer use for Claude Code. Screen capture and input injection in C++, behind an MCP server and a plugin that puts the screen in front of the model before every turn.

![Claude Code](https://img.shields.io/badge/Claude_Code-D97757?logo=claude&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-server_+_plugin-000000?logo=modelcontextprotocol&logoColor=white)
![Windows](https://img.shields.io/badge/Windows_only-0078D4?logo=windows&logoColor=white)

[![CI](https://github.com/Optccopa/computer-use/actions/workflows/ci.yml/badge.svg)](https://github.com/Optccopa/computer-use/actions/workflows/ci.yml)
[![Linting](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/charliermarsh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)
[![Mutation tested](https://img.shields.io/badge/mutation_tested-brightgreen)](scripts/mutate.py)
![C++20](https://img.shields.io/badge/C%2B%2B-20-00599C?logo=cplusplus&logoColor=white)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)

## Speed

| Stage | Time |
|---|---|
| Capture 1920x1080 | ~1-3 ms |
| Downscale to 1024x576 | ~4 ms |
| JPEG encode q75 | ~2 ms |
| **Full screenshot** | **~9 ms** |
| MCP round trip, over stdio | ~15 ms |

A model round trip takes about nine seconds, so the harness is under 2% of a session and the only lever that matters is making fewer calls. Hence batching.

## Install

Requires Visual Studio with the C++ workload (MSVC 14.5+), CMake 3.26+, Python 3.14, and AVX2.

```bash
uv venv --python 3.14
uv pip install -e .
.venv/Scripts/python.exe -m pytest -q
```

Then pick **one** of the two below, not both. Two servers means two harnesses, each with its own kill switch, and only one stops the one that is running.

**Ctrl+Esc stops everything.** Press it again to release.

### MCP

```bash
claude mcp add cufast -- /full/path/to/.venv/Scripts/python.exe -m cufast.server
```

Gives you `computer` and `screen_info`.

### Plugin

The same server, plus a hook that captures the screen before every turn and a skill covering batching, zoom and monitor switching.

```bash
cmake --build build/cli --config Release
claude plugin marketplace add /full/path/to/computer-use
claude plugin install cufast@cufast
```

Build first: the hook runs `plugin/bin/cufast.exe`, which CMake copies there and which is gitignored, so a stale copy can never answer with older capture code than the source claims.

For one session without installing:

```bash
claude --plugin-dir /full/path/to/computer-use/plugin
```

Remember to uninstall the plugin or scope it per session so that your not wasting context or exposing sensitive info during other sessions, it will continue running.

## Usage

Batch actions into one call. A click, the text after it and the confirming screenshot cost one round trip instead of three.

```jsonc
{
  "actions": [
    {"action": "left_click", "coordinate": [512, 288]},
    {"action": "type", "text": "hello"},
    {"action": "key", "text": "Return"}
  ]
}
```

A screenshot is appended automatically unless the batch already ends with one. Every coordinate is in screenshot space, never native pixels: a 1920x1080 display gives a 1024x576 frame by default.

## Actions

| Name | Args | Notes |
|---|---|---|
| `screenshot` | | Free; one arrives with every call anyway |
| `zoom` | `region: [x0, y0, x1, y1]` | Re-captures at native resolution |
| `left_click` | `coordinate`, `text`, `in_zoom` | `text` holds modifiers, e.g. `ctrl+shift` |
| `right_click` `middle_click` `double_click` `triple_click` | same as `left_click` | Omit `coordinate` to act where the cursor is |
| `left_click_drag` | `start_coordinate`, `coordinate`, `text`, `in_zoom` | |
| `mouse_move` | `coordinate`, `in_zoom` | |
| `mouse_move_rel` | `dx`, `dy`, `steps` | Last resort; prefer `aim` |
| `left_mouse_down` `left_mouse_up` | | Acts at the cursor |
| `cursor_position` | | In screenshot space |
| `scroll` | `scroll_direction`, `scroll_amount`, `coordinate`, `text` | |
| `type` | `text` | Newlines and tabs become real keystrokes |
| `key` | `text`, `repeat` | X11 keysym names: `Return`, `ctrl+s`, `alt+Tab` |
| `key_down` `key_up` | `text` | Stays held across calls |
| `hold_key` | `text`, `duration` | Blocks; use `key_down` to hold across actions |
| `wait` | `duration` | When the duration is the point, e.g. holding to mine |
| `wait_for_change` | `duration` | When you do not know how long it takes |
| `aim` | `coordinate`, `steps` | Turns the view to put that pixel on the crosshair. Self-calibrating |
| `look` | `yaw`, `pitch`, `steps` | Turn by an angle. Needs `calibrate` |
| `calibrate` | `aim_ratio`, `look_degrees_per_pixel` | Optional; `aim` measures itself |

`in_zoom` reads the coordinate against the last zoom image instead of the full screenshot, which is the only way to hit an exact pixel: four adjacent screenshot coordinates reach native columns 938, 940, 942, 944, while four adjacent zoom coordinates reach 937, 938, 939, 940.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `CUFAST_DISPLAY` | `0` | `0` is always primary; see `screen_info` |
| `CUFAST_MAX_WIDTH` | `1024` | Screenshot box, aspect preserved |
| `CUFAST_MAX_HEIGHT` | `768` | |
| `CUFAST_JPEG_QUALITY` | `0.75` | |
| `CUFAST_DRAW_CURSOR` | `true` | Composites the real cursor into the frame |
| `CUFAST_KILL_SWITCH` | `true` | The Ctrl+Esc stop button |
| `CUFAST_LOCK_DISPLAY` | `false` | Makes the display a boundary, not a default |
| `CUFAST_CAPTURE_TIMEOUT_MS` | `16` | One frame at 60Hz |
| `CUFAST_SETTLE_MS` | `40` | Pause after a UI-mutating action |

## License

Released under the MIT License. See [LICENSE](LICENSE).
