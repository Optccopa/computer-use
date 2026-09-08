"""The quarter-turn used to keep rotated panels on the DXGI path.

Duplication hands back the physical panel surface, so a portrait monitor arrives as
a landscape image lying on its side. The whole justification for rotating rather
than falling back to GDI is that the result is identical, so these tests compare
against a naive reference rather than against themselves.
"""

from __future__ import annotations

import pytest

from cufast import _native


def make_surface(w: int, h: int) -> bytes:
    """A surface where every pixel encodes its own coordinates.

    Any transposition, mirroring or off-by-one shows up as a specific wrong
    coordinate rather than as a plausible-looking image.
    """
    out = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 4
            out[i] = x & 0xFF
            out[i + 1] = y & 0xFF
            out[i + 2] = (x >> 8) & 0xFF
            out[i + 3] = (y >> 8) & 0xFF
    return bytes(out)


def pixel(buf: bytes, w: int, x: int, y: int) -> tuple[int, ...]:
    i = (y * w + x) * 4
    return tuple(buf[i:i + 4])


def reference(src: bytes, sw: int, sh: int, turns: int) -> bytes:
    """The obvious, slow implementation. The kernel is blocked; this one is not."""
    dw, dh = (sh, sw) if turns & 1 else (sw, sh)
    out = bytearray(dw * dh * 4)
    for y in range(dh):
        for x in range(dw):
            if turns == 0:
                sx, sy = x, y
            elif turns == 1:
                sx, sy = y, sh - 1 - x
            elif turns == 2:
                sx, sy = sw - 1 - x, sh - 1 - y
            else:
                sx, sy = sw - 1 - y, x
            i, j = (y * dw + x) * 4, (sy * sw + sx) * 4
            out[i:i + 4] = src[j:j + 4]
    return bytes(out)


# Deliberately not multiples of the 16-pixel tile: the ragged edge tiles are where a
# blocked implementation goes wrong.
SIZES = [(1, 1), (1, 7), (7, 1), (16, 16), (17, 15), (33, 31), (64, 48), (100, 37)]


class TestMatchesReference:
    @pytest.mark.parametrize("size", SIZES)
    @pytest.mark.parametrize("turns", [0, 1, 2, 3])
    def test_every_turn_and_size(self, size, turns):
        sw, sh = size
        src = make_surface(sw, sh)
        assert _native._rotate_raw(src, sw, sh, turns) == reference(src, sw, sh, turns)

    @pytest.mark.parametrize("turns", [0, 1, 2, 3])
    def test_a_realistic_panel(self, turns):
        # 1920x1080 is the panel behind the developer's portrait display.
        sw, sh = 1920, 1080
        src = make_surface(sw, sh)
        got = _native._rotate_raw(src, sw, sh, turns)
        dw, dh = (sh, sw) if turns & 1 else (sw, sh)
        assert len(got) == dw * dh * 4
        # Spot-checked rather than compared against the reference: at two million
        # pixels the pure-Python version takes minutes. The exhaustive comparison
        # above already covers the ragged edge tiles, which is where blocking breaks.
        corners = [(0, 0), (dw - 1, 0), (0, dh - 1), (dw - 1, dh - 1), (dw // 2, dh // 2)]
        for x, y in corners:
            if turns == 0:
                sx, sy = x, y
            elif turns == 1:
                sx, sy = y, sh - 1 - x
            elif turns == 2:
                sx, sy = sw - 1 - x, sh - 1 - y
            else:
                sx, sy = sw - 1 - y, x
            assert pixel(got, dw, x, y) == pixel(src, sw, sx, sy)


class TestRoundTrips:
    @pytest.mark.parametrize("turns", [1, 2, 3])
    def test_four_quarter_turns_are_the_identity(self, turns):
        sw, sh = 23, 14
        src = make_surface(sw, sh)
        buf, w, h = src, sw, sh
        for _ in range(4):
            buf = _native._rotate_raw(buf, w, h, turns)
            w, h = (h, w) if turns & 1 else (w, h)
        assert (w, h) == (sw, sh)
        assert buf == src

    def test_clockwise_and_counter_clockwise_cancel(self):
        sw, sh = 19, 11
        src = make_surface(sw, sh)
        once = _native._rotate_raw(src, sw, sh, 1)
        assert _native._rotate_raw(once, sh, sw, 3) == src

    def test_zero_turns_is_a_plain_copy(self):
        src = make_surface(12, 9)
        assert _native._rotate_raw(src, 12, 9, 0) == src
