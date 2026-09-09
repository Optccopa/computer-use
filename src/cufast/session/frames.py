"""One captured image, and the span arithmetic that maps pixels back to it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Screenshot:
    data: bytes
    width: int
    height: int
    media_type: str
    # Hash of the decoded pixels, computed natively during the capture and free to
    # carry. Two captures of the same region with the same hash are the same image,
    # which is what lets a static screen cost text instead of ~777 visual tokens.
    content_hash: int = 0
    # Which part of the display this is, monitor-local. Part of the identity: a zoom
    # and a full screenshot are different pictures even in the vanishing case where
    # their hashes collide.
    region: tuple[int, int, int, int] = (0, 0, 0, 0)


def _span(index: int, ref: int, native: int) -> tuple[int, int]:
    """The half-open native range that produced screenshot pixel `index`.

    Mirrors the downscaler exactly: destination pixel x averages source columns
    [x*native/ref, (x+1)*native/ref).
    """
    start = (index * native) // ref
    end = ((index + 1) * native) // ref
    if end <= start:
        end = start + 1
    return start, min(end, native)
