"""Where does the time actually go?

Answers one question: which layers deserve to be C++ and which do not. Anything the
agent loop hits once per action is not worth moving; anything in a tight loop or
crossing a process boundary repeatedly is.

Run: .venv/Scripts/python.exe scripts/profile_split.py
"""

from __future__ import annotations

import base64
import json
import statistics
import time

from cufast import _native
from cufast.actions import run_batch
from cufast.config import Config
from cufast.session import Session


def bench(label, fn, runs=50, warmup=5):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    median = statistics.median(samples)
    print(f"  {label:<44} {median:8.3f} ms")
    return median


def main():
    config = Config.from_env()
    session = Session(config)
    print(session.describe())
    print()

    shot = session.screenshot()
    raw = shot.data
    print(f"screenshot: {shot.width}x{shot.height}, {len(raw) / 1024:.1f} KiB JPEG")
    print()

    print("NATIVE (already C++)")
    native = bench("capture + downscale + encode", lambda: session.screen.grab(
        config.max_width, config.max_height, config.jpeg_quality, True, 0))
    bench("capture + downscale only (no encode)", lambda: session.screen.sample_hash(
        shot.width, shot.height, 0))
    bench("change-poll hash (160x90)", lambda: session.screen.sample_hash(160, 90, 0))

    print()
    print("PYTHON GLUE (per action)")
    coords = bench("coordinate map (to_screen)", lambda: session.to_screen(512, 288),
                   runs=2000, warmup=100)
    dispatch = bench(
        "full batch dispatch, no capture",
        lambda: run_batch(session, [{"action": "cursor_position"}], auto_screenshot=False),
        runs=200,
    )

    print()
    print("TRANSPORT (per screenshot)")
    b64 = bench("base64 encode", lambda: base64.b64encode(raw).decode("ascii"))
    encoded = base64.b64encode(raw).decode("ascii")
    payload = {"content": [{"type": "image", "data": encoded, "mime_type": "image/jpeg"}]}
    dumps = bench("json.dumps of the image payload", lambda: json.dumps(payload))
    blob = json.dumps(payload)
    bench("json.loads back", lambda: json.loads(blob))
    print(f"  {'wire size':<44} {len(blob) / 1024:8.1f} KiB")

    print()
    print("VERDICT")
    total_native = native
    total_glue = coords + dispatch
    total_transport = b64 + dumps
    print(f"  native pipeline      {total_native:7.2f} ms")
    print(f"  python glue          {total_glue:7.2f} ms   "
          f"({total_glue / total_native * 100:.1f}% of native)")
    print(f"  serialise + base64   {total_transport:7.2f} ms")
    print()
    print("  A model round trip is 1000-3000 ms. Anything below ~1 ms per action is")
    print("  noise; moving it to C++ buys nothing and costs readability.")


if __name__ == "__main__":
    main()
