# What earns its keep in C++

The C++ is not a performance ornament around image encoding. Three different kinds
of work live there, and only one of them is about throughput.

## The measurement that sets the bar

From `scripts/profile_split.py`, 1920x1080 producing 1024x576 JPEGs:

```
NATIVE
  capture + downscale + encode                 3.82 ms
  capture + downscale only (no encode)         2.26 ms
  change-poll hash (160x90)                    1.48 ms

PYTHON GLUE (per action)
  coordinate map (to_screen)                   0.001 ms
  full batch dispatch, no capture              0.005 ms

TRANSPORT (per screenshot)
  base64 encode                                0.218 ms
  json.dumps of the image payload              0.156 ms
```

The entire Python layer costs **0.01 ms per action — 0.2% of the native pipeline it
wraps**, against a model round trip of 1000–3000 ms. So "rewrite it in C++ because
C++ is faster" is not an argument that survives contact with a stopwatch. Something
has to earn its place on the native side for a specific reason.

There are four such reasons.

---

## 1. Throughput: work that touches every pixel

Capture, downscale, and encode. This is the obvious one and the only one that is
purely about speed.

The downscale is where the real work is: at 1920x1080 it moves 8 MB per frame, and
the naive version cost 11 ms — slower than calling out to PIL, which would have made
the whole native layer pointless. Two fixes made it 3.97 ms: turning off the `/Os`
that nanobind applies by default, and recognising that for any downscale between
0.5x and 1.0x a box average over two pixels *is* `_mm256_avg_epu8`, one instruction.

JPEG encode is only ~2 ms of the pipeline. If this layer were only about encoding it
would barely justify itself.

## 2. Atomicity and unwind safety: the input state machine

**This one is about correctness, not speed, and it is the strongest argument in the
project for a native layer.**

Injected input is a *state machine on the user's real desktop*. Keys and buttons go
down and must come back up. Two guarantees are only available if the entire down/up
lifecycle lives on one side of the language boundary:

**SendInput is atomic per call.** Windows guarantees that events within a single
`SendInput` call are not interleaved with the user's real keyboard and mouse. One
call per action means a click with modifiers, or a whole typed string, cannot have
the user's own keystrokes spliced into the middle of it. If Python issued key-downs
and key-ups as separate calls, that guarantee would be gone.

**Releases must be unwind-safe and must not be blockable.** The harness has a kill
switch that makes every injection routine throw. Before `PressGuard` existed, that
switch caused the exact catastrophe it was built to prevent: engage it mid-drag, the
next `mouse_move` throws, the stack unwinds past the button release, and the user is
left with the left mouse button physically held down — rubber-banding a selection
across every window they touch — with no recovery path, because every call that could
release it was also blocked.

`PressGuard` records every key and button pressed and releases them from its
destructor, deliberately bypassing the kill switch. One extra injected event beats a
latched Ctrl on a real desktop. That invariant cannot be enforced from Python: an
exception, a `KeyboardInterrupt`, or a GC pause between a Python-issued key-down and
key-up would strand the keyboard.

The input layer also has to answer questions only the OS can answer — which keyboard
layout the *foreground window* uses, whether a character needs Shift or AltGr on that
layout, which virtual-key a character maps to. Getting that wrong types `q` when the
model asked for `@`, and `Ctrl+Q` — quit — when it asked for `Ctrl+@`.

## 3. Blocking on OS primitives

`AcquireNextFrame` blocks *in the driver* until the compositor presents a frame. In
C++ with the GIL released that is one blocking call: near-zero CPU, microsecond wake
latency. Driven from Python it degenerates into a poll loop, burning CPU and adding
latency to the feature whose entire purpose is removing it. This is why
`wait_for_change` belongs on the native side.

The kill-switch hotkey is the same shape from the other direction: `WH_KEYBOARD_LL`
needs a dedicated thread running a message pump, and the callback must return within
a system timeout. A Python callback there would take the GIL on every keystroke the
user types — a latency hazard and a re-entrancy hazard. In C++ it is an atomic store.

## 4. Repeated crossings of a process boundary

UI Automation, when it lands. Every `IUIAutomationElement` property read is a
cross-process COM call into the target application, on the order of 0.1–1 ms. A
window with 200 elements read 5 ways is 1000 crossings — hundreds of milliseconds,
which is exactly why Python UIA wrappers feel so slow.
`IUIAutomationCacheRequest` with `FindAllBuildCache` fetches a whole subtree in
**one** crossing. The language is not the point; the batching API is, and no common
Python wrapper uses it well.

---

## The rule, and what stays in Python

| Signal | Side |
|---|---|
| Touches every pixel | C++ |
| Holds OS state that must be released even while unwinding | C++ |
| Needs an atomicity guarantee the OS only gives per-call | C++ |
| Blocks on an OS primitive, or needs its own thread and message pump | C++ |
| Crosses a process boundary repeatedly | C++ |
| Runs once per action | Python |
| Is policy, validation, schema, or transport | Python |

So Python keeps: config, coordinate mapping, action validation and dispatch, batch
semantics, the MCP server. All of it measured at 0.01 ms per action, all of it the
part most likely to change, and none of it holding OS state that can strand the
machine.

Concretely, for what is left to build:

- **`wait_for_change` → C++** (reason 3)
- **UI Automation → C++** (reason 4)
- **Kill-switch hotkey → C++** (reason 3)
- **Local OCR, if it happens → C++** (reason 1)
- **Shell execution → Python.** One `subprocess` call; the cost is the child process.
- **Clipboard → Python.** Two Win32 calls, once per action.
- **Display selection → Python.** Policy. The `Screen` objects are C++ and cached,
  so switching costs a dictionary lookup after first use.

## Why the 28 ms MCP round trip is not a C++ problem

Native is 3.8 ms and serialisation is 0.37 ms, so ~24 ms is subprocess pipe latency
and asyncio scheduling — context switches, not computation. Rewriting the server in
C++ would move the same bytes through the same pipe. The only real lever is **sending
fewer screenshots**, which is what batching and `wait_for_change` are for.

## Measured, so it does not get re-litigated

From `scripts/bench.py` on a 1920x1080 primary, and from a real hour-long Minecraft
session driven through the MCP server (233 calls):

| | |
|---|---|
| full screenshot into the 1024x768 box | 2.62 ms |
| JPEG encode alone | 1.24 ms |
| zoom of a 480x270 region | 0.34 ms |
| downscale alone | 1.53 ms |
| native-resolution PNG, no downscale | 49.6 ms |
| MCP round trip, end to end | 10-19 ms |
| **model round trip** | **9-18 s** |
| harness share of wall clock | **0.3%** |

The conclusion that shapes everything else: **the harness is not the bottleneck and
cannot become one.** Optimising capture further buys nothing. The only lever that
moves elapsed time is making the model issue fewer calls, which is why `aim`,
`wait_for_change` and batching exist, and why `aim` calibrates itself rather than
asking the model to do it in a separate call.

Two second-order effects from the same session, both worth remembering:

- Context grew 53k -> 310k tokens over an hour, and call time went 8.5 s -> 18.1 s
  with it. Roughly 55% of that context was accumulated screenshots.
- Actions per call sat at 2.8, and 51 of 53 calls ended in a screenshot. A tool that
  needs setup before it can be used does not get used: `aim` sat at zero uses in 233
  calls while the model narrated "aim at the closest trunk" and hand-rolled pixel
  deltas instead.
