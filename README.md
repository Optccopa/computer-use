# cufast

[![C++20](https://img.shields.io/badge/C%2B%2B-20-00599C?logo=cplusplus&logoColor=white)](CMakeLists.txt)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![tests 621 passing](https://img.shields.io/badge/tests-621%20passing-brightgreen)](tests/)
[![coverage 92%](https://img.shields.io/badge/coverage-92%25-brightgreen)](tests/)
[![mutations 53/53 caught](https://img.shields.io/badge/mutations-53%2F53%20caught-brightgreen)](scripts/mutate.py)
[![lint ruff](https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&logoColor=black)](pyproject.toml)

A fast Windows computer-use harness. Screen capture, downscale, JPEG encode and
input injection are C++ behind [nanobind](https://nanobind.readthedocs.io); Python
handles coordinates, action dispatch and an MCP server you point Claude Code at.

Windows only, deliberately: it uses DXGI Desktop Duplication, WIC and `SendInput`
directly rather than a portable abstraction.

## Speed

| Stage | Time |
|---|---|
| Capture 1920x1080 | ~1-3 ms |
| Downscale to 1024x576 | ~4 ms |
| JPEG encode q75 | ~2 ms |
| **Full screenshot** | **~9 ms** |
| MCP round trip, measured over stdio | ~15 ms |

None of that is the point. A round trip to the model takes about nine seconds, so
the harness is under 2% of a session and the only real lever is making fewer calls.
That is why the `computer` tool takes an ordered *list* of actions: a click, the
text after it and the confirming screenshot cost one round trip instead of three.

An idle screen costs nothing to look at. A timed-out `AcquireNextFrame` means
nothing was presented, so the cached frame is reused, and an identical frame is
replaced by one line of text instead of a second copy of the same image.

## Install

Needs Visual Studio with the C++ workload (MSVC 14.5+), CMake 3.26+, Python 3.14,
and a CPU with AVX2.

```bash
uv venv --python 3.14
uv pip install -e .
.venv/Scripts/python.exe -m pytest -q
```

Then pick **one** of the two below. Not both: two servers means two harnesses, each
with its own kill switch, and only one of them stops the one that is running.

**Ctrl+Esc stops everything, at any time.** Press it again to release.

### MCP

```bash
claude mcp add cufast -- /full/path/to/.venv/Scripts/python.exe -m cufast.server
```

Two tools appear, `computer` and `screen_info`. Batch actions into one call:

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
Every coordinate is in screenshot space, never native display pixels: on a
1920x1080 display the default box gives a 1024x576 frame.

### Plugin

The same server, plus the parts that make it usable without being asked for.

```bash
cmake --build build/cli --config Release
claude plugin marketplace add /full/path/to/computer-use
claude plugin install cufast@cufast
```

Restart Claude Code. Beyond the MCP server it adds a **hook** that captures the
screen before every turn and says which display it is and how many are attached,
and a **skill** covering batching, zoom, monitor switching and the stop button.

Build first: the hook runs `plugin/bin/cufast.exe`, which CMake copies there and
which is gitignored rather than committed, so a stale copy can never answer with
older capture code than the source claims.

To try it for one session without installing:

```bash
claude --plugin-dir /full/path/to/computer-use/plugin
```
