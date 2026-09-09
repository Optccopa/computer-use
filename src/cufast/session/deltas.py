"""Turning a computed movement into something the native layer will accept."""

from __future__ import annotations

import math

from cufast.session.errors import ActionError

# The native side rejects anything past this, and beyond it the value stops being a
# mouse movement and starts being a way to crash the binding: a delta over 2^31 came
# back as a nanobind TypeError that nothing caught, which destroyed the whole batch
# result including any screenshot already in it.
MAX_NATIVE_DELTA = 100_000


def _finite_delta(value: float, what: str) -> int:
    """Rounds to an int the native layer will accept, or says why it will not.

    Rounds half away from zero rather than to even: a calibrated turn is issued over
    and over, and banker's rounding would send exact halves alternately up and down.
    """
    if not math.isfinite(value):
        raise ActionError(f"{what} came out as {value}, which is not a movement")
    if abs(value) > MAX_NATIVE_DELTA:
        raise ActionError(
            f"{what} came out as {value:.0f} native pixels, past the {MAX_NATIVE_DELTA} "
            "limit. Either the value asked for is far too large, or the calibration "
            "is wrong -- recalibrate rather than repeating this."
        )
    return int(math.copysign(math.floor(abs(value) + 0.5), value))
