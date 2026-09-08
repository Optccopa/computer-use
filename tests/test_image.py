"""Downscale and scaling-plan tests.

The SIMD narrow path and the scalar general path must agree bit for bit, because
which one runs depends only on the scale factor -- a silent divergence would show up
as screenshots that look subtly different at some display sizes and not others.
"""

from __future__ import annotations

import random

import pytest

from cufast import _native


def make_bgra(width: int, height: int, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(width * height * 4))


def solid_bgra(width: int, height: int, b: int, g: int, r: int) -> bytes:
    return bytes([b, g, r, 255]) * (width * height)


class TestPlanFit:
    def test_preserves_aspect_ratio(self):
        # 16:9 into the 4:3 XGA box is height-limited by width: 1024x576.
        assert _native.plan_fit(1920, 1080, 1024, 768) == (1024, 576)

    def test_never_upscales_by_default(self):
        assert _native.plan_fit(320, 240, 1024, 768) == (320, 240)

    def test_upscales_when_asked(self):
        w, h = _native.plan_fit(320, 240, 1024, 768, True)
        assert (w, h) == (1024, 768)

    def test_result_never_exceeds_box(self):
        for src_w, src_h in ((1919, 1081), (2560, 1440), (1080, 1920), (3840, 2160)):
            w, h = _native.plan_fit(src_w, src_h, 1024, 768)
            assert w <= 1024 and h <= 768

    def test_portrait_display(self):
        # The second attached display in development is 1080x1920.
        w, h = _native.plan_fit(1080, 1920, 1024, 768)
        assert h == 768
        assert w == pytest.approx(432, abs=1)

    def test_rejects_empty(self):
        # RuntimeError specifically: nanobind maps the native Error to it, and a
        # blind Exception would also pass if the call itself were misspelled.
        with pytest.raises(RuntimeError):
            _native.plan_fit(0, 100, 1024, 768)
        with pytest.raises(RuntimeError):
            _native.plan_fit(100, 100, 0, 768)


class TestDownscaleEquivalence:
    @pytest.mark.parametrize(
        "src_w,src_h,dst_w,dst_h",
        [
            (1920, 1080, 1024, 576),   # the default screenshot path, 0.53x
            (1920, 1080, 1280, 720),   # the other recommended size, 0.67x
            (1920, 1080, 1920, 1080),  # 1.0x, every span is a single pixel
            (640, 480, 400, 300),      # 0.625x
            (640, 480, 321, 241),      # awkward ratio, not a clean fraction
            (63, 47, 40, 30),          # smaller than one SIMD block plus a tail
            (17, 9, 9, 5),             # tail-only, no full 8-wide iteration
            (1920, 1080, 480, 270),    # 0.25x -- spans of 4, general path both ways
            (1920, 1080, 160, 90),     # the change-poll grid, spans of 12
        ],
    )
    def test_simd_matches_scalar(self, src_w, src_h, dst_w, dst_h):
        src = make_bgra(src_w, src_h, seed=src_w * 31 + dst_w)
        fast = _native._downscale_raw(src, src_w, src_h, dst_w, dst_h, False)
        slow = _native._downscale_raw(src, src_w, src_h, dst_w, dst_h, True)
        assert len(fast) == dst_w * dst_h * 3
        assert fast == slow

    def test_identity_scale_returns_source_pixels(self):
        # At 1.0x every span is one pixel, so the output must be the input with the
        # alpha byte dropped -- this is the case that runs a pixel through
        # _mm256_avg_epu8 against itself.
        src = make_bgra(64, 8, seed=7)
        out = _native._downscale_raw(src, 64, 8, 64, 8, False)
        expected = bytes(
            src[i * 4 + c] for i in range(64 * 8) for c in range(3)
        )
        assert out == expected

    def test_preserves_channel_order(self):
        # A distinctive BGR triple must survive as BGR, not get swapped to RGB.
        src = solid_bgra(32, 32, b=10, g=120, r=240)
        out = _native._downscale_raw(src, 32, 32, 16, 16, False)
        assert set(out[0::3]) == {10}
        assert set(out[1::3]) == {120}
        assert set(out[2::3]) == {240}

    def test_solid_colour_survives_any_scale(self):
        src = solid_bgra(200, 150, b=33, g=77, r=199)
        for dst in ((100, 75), (199, 149), (50, 37), (13, 9)):
            out = _native._downscale_raw(src, 200, 150, dst[0], dst[1], False)
            assert set(out[0::3]) == {33}, dst
            assert set(out[1::3]) == {77}, dst
            assert set(out[2::3]) == {199}, dst

    def test_two_pixel_average_is_exact(self):
        # Two source columns, one destination column: the average must round half up,
        # matching (a + b + 1) >> 1.
        src = bytes([0, 0, 0, 255]) + bytes([255, 255, 255, 255])
        out = _native._downscale_raw(src, 2, 1, 1, 1, False)
        assert out == bytes([128, 128, 128])

    def test_rejects_short_buffer(self):
        with pytest.raises(RuntimeError):
            _native._downscale_raw(b"\x00" * 16, 100, 100, 10, 10, False)


class TestHashStability:
    def test_same_pixels_same_hash(self):
        src = solid_bgra(64, 64, 1, 2, 3)
        a = _native._downscale_raw(src, 64, 64, 32, 32, False)
        b = _native._downscale_raw(src, 64, 64, 32, 32, False)
        assert a == b
