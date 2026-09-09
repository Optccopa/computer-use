"""Capture session: owns one display and the coordinate frame the model works in.

Split by responsibility: what a failure is, what a frame is, how a movement is
rounded, how coordinates map, how aiming calibrates itself, and the Session that
ties them together.
"""

from __future__ import annotations

from cufast.session.aiming import (
    AIM_MIN_CONFIDENCE,
    AIM_MIN_SHIFT_PX,
    AIM_PROBE_NATIVE_PX,
    AIM_PROBE_RETRY_PX,
    AIM_RATIO_BOUNDS,
    AimingMixin,
)
from cufast.session.core import Session
from cufast.session.deltas import MAX_NATIVE_DELTA, _finite_delta
from cufast.session.errors import (
    ACTION_FAILURES,
    STOPPED_BY_KILL_SWITCH,
    ActionError,
)
from cufast.session.frames import Screenshot, _span
from cufast.session.geometry import CoordinateMixin

__all__ = [
    "ACTION_FAILURES",
    "AIM_MIN_CONFIDENCE",
    "AIM_MIN_SHIFT_PX",
    "AIM_PROBE_NATIVE_PX",
    "AIM_PROBE_RETRY_PX",
    "AIM_RATIO_BOUNDS",
    "MAX_NATIVE_DELTA",
    "STOPPED_BY_KILL_SWITCH",
    "ActionError",
    "AimingMixin",
    "CoordinateMixin",
    "Screenshot",
    "Session",
]
