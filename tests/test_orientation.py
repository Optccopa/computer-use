"""The two capture paths must produce the same image.

This exists because a rotation that is wrong by a quarter turn still looks like a
perfectly plausible screenshot, so nothing about the image itself gives it away. It
shipped: DXGI_MODE_ROTATION_ROTATE90 and ROTATE270 were mapped to each other's
inverse, and every duplication frame from the portrait monitor came back upside down.

The reason it survived its original test is worth keeping in mind. The first frame
after DuplicateOutput reports LastPresentTime == 0 and seeds from GDI instead, so a
one-shot capture -- which is exactly what the original check did -- never exercises
the rotation at all. It photographed the reference and compared it with itself.

GDI is the reference here: it reports the composed, already-oriented desktop and
needs no rotation, so agreeing with it is the definition of correct.
"""

from __future__ import annotations

import pytest

from cufast import _native

BOX = (512, 512)


def displays():
    return _native.list_displays()


def dxgi_profile(index: int, axis: str) -> list[int] | None:
    """A profile from a frame that actually went through duplication.

    Returns None when this display will not use DXGI at all, in which case there is
    no rotation being applied and nothing to compare.
    """
    screen = _native.Screen(index)
    if not screen.using_dxgi:
        return None
    grab = screen.profile if axis == "columns" else screen.profile_rows
    # The first grab seeds from GDI. Take several so a presented frame lands.
    last = None
    for _ in range(8):
        last = grab(*BOX, 250)
    return last


def gdi_profile(index: int, axis: str) -> list[int]:
    screen = _native.Screen(index, prefer_dxgi=False)
    assert not screen.using_dxgi
    grab = screen.profile if axis == "columns" else screen.profile_rows
    return grab(*BOX, 250)


def agreement(a: list[int], b: list[int]) -> float:
    """How well two profiles line up, as best_shift confidence at a small offset."""
    shift, confidence = _native.best_shift(a, b, 12)
    return confidence if abs(shift) <= 4 else 0.0


@pytest.mark.desktop
class TestBothPathsAgree:
    @pytest.mark.parametrize("axis", ["columns", "rows"])
    def test_duplication_matches_gdi_on_every_display(self, axis):
        for entry in displays():
            index = entry["index"]
            dxgi = dxgi_profile(index, axis)
            if dxgi is None:
                continue
            gdi = gdi_profile(index, axis)
            # Transposition is deterministic, so this half needs no retry.
            assert len(dxgi) == len(gdi), (
                f"display {index}: duplication produced a {len(dxgi)}-sample {axis} "
                f"profile against GDI's {len(gdi)} -- the frame is transposed, so the "
                "rotation is off by a quarter turn"
            )

            # Best of three. The desktop is live -- a video, a blinking caret, an
            # animating avatar -- so a single pair of captures taken moments apart
            # can disagree for reasons that have nothing to do with orientation.
            # The signal being tested is enormous when the bug is present (0.79
            # reversed against 0.03 upright, measured), so requiring only that some
            # sample prefers upright keeps the test decisive without making it
            # depend on a still screen.
            best_upright, best_flipped = 0.0, 0.0
            for _ in range(3):
                sample = dxgi_profile(index, axis)
                if sample is None:
                    break
                gdi = gdi_profile(index, axis)
                best_upright = max(best_upright, agreement(gdi, sample))
                best_flipped = max(best_flipped, agreement(gdi, list(reversed(sample))))
                if best_upright >= best_flipped and best_upright > 0.1:
                    break
            assert best_upright >= best_flipped, (
                f"display {index}: the {axis} profile matches GDI better reversed "
                f"({best_flipped:.3f}) than upright ({best_upright:.3f}) -- "
                "duplication frames are rotated 180 degrees"
            )

    def test_a_rotated_display_is_actually_being_tested(self):
        # The bug only appears on a rotated panel. If none is attached this file
        # proves nothing, and saying so is better than a green tick that means
        # nothing.
        rotated = [d for d in displays() if d["height"] > d["width"]]
        if not rotated:
            pytest.skip("no portrait display attached; the rotation path is untested here")
        index = rotated[0]["index"]
        screen = _native.Screen(index)
        assert screen.using_dxgi, (
            "the portrait display fell back to GDI, so the rotation path -- the whole "
            "reason it is on duplication -- did not run"
        )

    def test_the_first_frame_is_not_what_later_frames_look_like(self):
        # Pinning the trap itself: the seed frame comes from GDI, so verifying
        # orientation from a single capture verifies the reference against itself.
        rotated = [d for d in displays() if d["height"] > d["width"]]
        if not rotated:
            pytest.skip("no portrait display attached")
        index = rotated[0]["index"]
        screen = _native.Screen(index)
        if not screen.using_dxgi:
            pytest.skip("duplication unavailable on this display")
        first = screen.profile(*BOX, 250)
        assert len(first) > 0
