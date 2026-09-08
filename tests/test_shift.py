"""Measuring how far the view panned between two frames.

This is what makes `aim` calibrate itself. The failure that matters is not a wrong
answer reported as wrong -- that just retries -- but a wrong answer reported
confidently, because the ratio it produces is then baked into every later aim.
"""

from __future__ import annotations

import math
import random

import pytest

from cufast import _native


def signal(n: int = 512, seed: int = 7) -> list[int]:
    """A repeating-but-not-periodic profile, like a view with scenery in it."""
    rng = random.Random(seed)
    return [int(400 + 200 * math.sin(i / 9.0) + rng.randint(-30, 30)) for i in range(n)]


def shifted(values: list[int], by: int) -> list[int]:
    """The same signal displaced by `by` samples, with the edges filled in."""
    n = len(values)
    return [values[min(max(i - by, 0), n - 1)] for i in range(n)]


class TestFindsTheShift:
    @pytest.mark.parametrize("amount", [-120, -37, -8, -1, 0, 1, 8, 37, 120])
    def test_recovers_a_known_displacement(self, amount):
        base = signal()
        got, confidence = _native.best_shift(base, shifted(base, amount), 200)
        assert got == amount
        assert confidence > 0.1

    def test_is_confident_about_a_clear_match(self):
        base = signal()
        _, confidence = _native.best_shift(base, shifted(base, 25), 200)
        assert confidence > 0.3

    def test_survives_a_brightness_change(self):
        # A cloud, a torch, or the sun moving raises the whole profile. Matching on
        # first differences is what makes that cancel instead of dominating.
        base = signal()
        brighter = [v + 90 for v in shifted(base, 30)]
        got, confidence = _native.best_shift(base, brighter, 200)
        assert got == 30
        assert confidence > 0.3

    def test_survives_noise(self):
        rng = random.Random(3)
        base = signal()
        noisy = [v + rng.randint(-12, 12) for v in shifted(base, -44)]
        got, _ = _native.best_shift(base, noisy, 200)
        assert abs(got - (-44)) <= 2


class TestRefusesToGuess:
    def test_a_flat_view_has_no_confidence(self):
        # Facing a wall or the sky. Every candidate shift scores identically, so
        # there is nothing to learn and saying so is the only correct answer.
        flat = [128] * 512
        _, confidence = _native.best_shift(flat, flat, 200)
        assert confidence < 0.05

    def test_unrelated_views_are_not_confident(self):
        a, b = signal(seed=1), signal(seed=99)
        _, confidence = _native.best_shift(a, b, 200)
        assert confidence < 0.25

    def test_the_calibration_threshold_rejects_a_flat_view(self):
        from cufast.session import AIM_MIN_CONFIDENCE

        flat = [200] * 512
        _, confidence = _native.best_shift(flat, flat, 200)
        assert confidence < AIM_MIN_CONFIDENCE


class TestDegenerateInput:
    @pytest.mark.parametrize("values", [[], [1], [1, 2, 3]])
    def test_too_short_is_zero_not_a_crash(self, values):
        assert _native.best_shift(values, values, 10) == (0, 0.0)

    def test_zero_search_range_is_zero(self):
        base = signal()
        assert _native.best_shift(base, base, 0) == (0, 0.0)

    def test_mismatched_lengths_do_not_read_past_the_end(self):
        base = signal(512)
        short = signal(64)
        shift, _ = _native.best_shift(base, short, 200)
        assert isinstance(shift, int)

    def test_a_shift_beyond_the_search_range_is_clamped_not_wrong(self):
        base = signal()
        shift, _ = _native.best_shift(base, shifted(base, 150), 40)
        assert abs(shift) <= 40


@pytest.mark.desktop
class TestLiveProfile:
    """The profile actually produced by a real capture of this machine."""

    def test_a_real_display_produces_a_usable_profile(self):
        screen = _native.Screen(0)
        prof = screen.profile(1024, 768, 200)
        assert len(prof) > 0
        assert all(v >= 0 for v in prof)
        # A real desktop is not uniform, so it must carry some signal; a profile of
        # all one value would silently defeat calibration.
        assert len(set(prof)) > 1

    def test_a_screen_that_is_not_panning_matches_at_zero(self):
        # Retried, because "still" is not something a test can insist on: this runs
        # against whatever the developer happens to be doing. Content changing is
        # fine -- the profile only has to agree that nothing moved SIDEWAYS, which
        # is what a rotation or a pan would show up as.
        screen = _native.Screen(0)
        for _ in range(5):
            a = screen.profile(1024, 768, 200)
            b = screen.profile(1024, 768, 200)
            shift, _ = _native.best_shift(a, b, 100)
            if abs(shift) <= 1:
                return
        pytest.fail(f"the view appears to have panned by {shift} between captures")
