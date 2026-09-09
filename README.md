# cufast

Fast, Vibecoded, Windows computer use for Claude Code. Screen capture and input injection in C++, behind an MCP server and a plugin that puts the screen in front of the model before every turn.

![Claude Code](https://img.shields.io/badge/Claude_Code-D97757?logo=claude&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-server_+_plugin-000000?logo=modelcontextprotocol&logoColor=white)
![Windows](https://img.shields.io/badge/Windows_only-0078D4?logo=windows&logoColor=white)

[![CI](https://github.com/Optccopa/computer-use/actions/workflows/ci.yml/badge.svg)](https://github.com/Optccopa/computer-use/actions/workflows/ci.yml)
[![Linting](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/charliermarsh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![tests](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Optccopa/computer-use/main/.github/badges/tests.json)](https://github.com/Optccopa/computer-use/actions/workflows/ci.yml)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Optccopa/computer-use/main/.github/badges/coverage.json)](https://github.com/Optccopa/computer-use/actions/workflows/ci.yml)
[![mutations](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Optccopa/computer-use/main/.github/badges/mutations.json)](scripts/mutate.py)
[![C++20](https://img.shields.io/badge/C%2B%2B-20-00599C?logo=cplusplus&logoColor=white)](CMakeLists.txt)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](pyproject.toml)

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

There is no sandbox. Clicks and keystrokes go to your real machine, so the model can do anything you could, and the kill switch is the boundary rather than a permission list. The harness caps how long one call may occupy it and keeps the cursor on the display it was given, but it does not decide which buttons are safe to press.

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
