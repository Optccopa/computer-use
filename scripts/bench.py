"""Times the capture pipeline end to end and per stage.

Run: .venv/Scripts/python.exe scripts/bench.py
"""

import statistics
import sys
import time

from cufast import _native


def timed(label, fn, runs=40, warmup=5):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    print(
        f"  {label:<34} med {statistics.median(samples):6.2f} ms   "
        f"p05 {samples[len(samples) // 20]:6.2f}   p95 {samples[len(samples) * 19 // 20]:6.2f}"
    )
    return statistics.median(samples)


def main():
    print("displays:")
    for d in _native.list_displays():
        print(f"  {d}")

    screen = _native.Screen(0)
    print(
        f"\nScreen 0: {screen.width}x{screen.height} "
        f"at ({screen.origin_x},{screen.origin_y})  "
        f"path={'DXGI' if screen.using_dxgi else 'GDI'}"
    )

    shot = screen.grab(max_w=1024, max_h=768)
    print(
        f"\nXGA fit -> {shot.width}x{shot.height}, {len(shot.data) / 1024:.1f} KiB JPEG q75, "
        f"visual tokens ~{-(-shot.width // 28) * -(-shot.height // 28)}"
    )

    print("\ntimings:")
    # timeout_ms=0 returns instantly whether or not a new frame is ready, which is
    # what the agent loop wants: never block waiting on an idle compositor.
    timed("full screenshot -> 1024x768 box", lambda: screen.grab(1024, 768, timeout_ms=0))
    timed("full screenshot -> 1280x720 box", lambda: screen.grab(1280, 720, timeout_ms=0))
    timed("full screenshot, no cursor",
          lambda: screen.grab(1024, 768, draw_cursor=False, timeout_ms=0))
    timed("native-res PNG (no downscale)",
          lambda: screen.grab(9999, 9999, png=True, timeout_ms=0), runs=10)
    timed("zoom 480x270 region",
          lambda: screen.grab(1024, 768, rx=200, ry=200, rw=480, rh=270, timeout_ms=0))
    timed("sample_hash 160x90 (change poll)", lambda: screen.sample_hash(160, 90, 0))

    # sample_hash runs capture + downscale but no encode, so the gap against the
    # matching grab() isolates what the JPEG encoder costs.
    d = timed("downscale only -> 1024x576", lambda: screen.sample_hash(1024, 576, 0))
    g = timed("downscale + JPEG -> 1024x576", lambda: screen.grab(1024, 768, timeout_ms=0))
    print(f"  {'=> JPEG encode':<34} ~{g - d:6.2f} ms")

    print("\nsize / token cost by target box:")
    for box in ((1024, 768), (1280, 720), (1366, 768), (1920, 1080)):
        s = screen.grab(box[0], box[1], timeout_ms=0)
        tokens = -(-s.width // 28) * -(-s.height // 28)
        print(
            f"  box {box[0]}x{box[1]:<5} -> {s.width}x{s.height:<5} "
            f"{len(s.data) / 1024:6.1f} KiB   ~{tokens} visual tokens"
        )

    out = sys.argv[1] if len(sys.argv) > 1 else None
    if out:
        with open(out, "wb") as fh:
            fh.write(screen.grab(1024, 768).data)
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
