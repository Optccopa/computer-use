"""Regression tests for coordinate mapping.

Each class here pins a bug that shipped: a wrong mapping does not raise, it clicks
the wrong thing, so these are the tests that matter most in the project.
"""

from __future__ import annotations

import pytest

from cufast import _native
from cufast.config import Config
from cufast.session import ActionError, Session
from tests.conftest import FakeScreen


def make_session(monkeypatch, screen, **cfg):
    import cufast.session as session_mod

    monkeypatch.setattr(_native, "Screen", lambda index: screen, raising=True)
    defaults = dict(max_width=1024, max_height=768, settle_ms=0)
    defaults.update(cfg)
    return session_mod.Session(Config(**defaults))


# Sizes chosen to include scale factors below 2, where naive pixel-centre mapping
# escapes the source span. 1920x1080 at the default box is 1.875x and happens to be
# clean, which is why the original bug went unnoticed on the development machine.
DISPLAYS = [
    (1920, 1080), (1366, 768), (1600, 900), (1440, 900),
    (1280, 800), (1152, 864), (2560, 1440), (1080, 1920), (3840, 2160),
]
BOXES = [(1024, 768), (1280, 720), (1366, 768)]


class TestClickLandsInsideItsOwnPixel:
    """The property that makes a click correct.

    The downscaler defines screenshot pixel x as the average of native columns
    [x*W//R, (x+1)*W//R). A click derived from that pixel must land inside that same
    span; one pixel past it is a click on a neighbour's territory.
    """

    @pytest.mark.parametrize("native", DISPLAYS)
    @pytest.mark.parametrize("box", BOXES)
    def test_every_column_and_row(self, monkeypatch, native, box):
        screen = FakeScreen(width=native[0], height=native[1])
        session = make_session(monkeypatch, screen, max_width=box[0], max_height=box[1])
        ref_w, ref_h = session.ref_width, session.ref_height
        W, H = native

        for x in range(ref_w):
            lx, _ = session.to_local(x, 0)
            start, end = (x * W) // ref_w, ((x + 1) * W) // ref_w
            assert start <= lx < max(end, start + 1), (
                f"{native} -> {ref_w}x{ref_h}: column {x} mapped to {lx}, "
                f"outside its source span [{start}, {end})"
            )

        for y in range(ref_h):
            _, ly = session.to_local(0, y)
            start, end = (y * H) // ref_h, ((y + 1) * H) // ref_h
            assert start <= ly < max(end, start + 1)

    @pytest.mark.parametrize("native", DISPLAYS)
    def test_mapping_is_monotonic_and_covers_the_display(self, monkeypatch, native):
        screen = FakeScreen(width=native[0], height=native[1])
        session = make_session(monkeypatch, screen)
        previous = -1
        for x in range(session.ref_width):
            lx, _ = session.to_local(x, 0)
            assert 0 <= lx < native[0]
            assert lx >= previous
            previous = lx


class TestReferenceFrameTracksDisplayChanges:
    """The native layer follows resolution changes, so the mapping must too.

    Caching the screenshot size at construction leaves a live numerator against a
    stale denominator: clicks land hundreds of pixels off, and coordinates that are
    legal in the delivered image get rejected against a resolution never shown.
    """

    def test_reference_follows_a_mode_change(self, monkeypatch):
        screen = FakeScreen(width=1920, height=1080)
        session = make_session(monkeypatch, screen)
        assert (session.ref_width, session.ref_height) == (1024, 576)

        screen.resize(1080, 1920)
        assert (session.ref_width, session.ref_height) == (432, 768)

    def test_screenshot_size_always_matches_the_reference(self, monkeypatch):
        screen = FakeScreen(width=1920, height=1080)
        session = make_session(monkeypatch, screen)
        for size in ((1920, 1080), (1080, 1920), (1366, 768), (2560, 1440)):
            screen.resize(*size)
            shot = session.screenshot()
            assert (shot.width, shot.height) == (session.ref_width, session.ref_height)

    def test_coordinates_legal_after_a_rotation(self, monkeypatch):
        screen = FakeScreen(width=1920, height=1080)
        session = make_session(monkeypatch, screen)
        screen.resize(1080, 1920)
        # Previously this raised "y=700 is outside the screenshot, which is 1024x576"
        # even though the delivered image was 432x768.
        x, y = session.to_local(216, 700)
        assert 0 <= x < 1080
        assert 0 <= y < 1920

    def test_centre_maps_to_centre_after_a_change(self, monkeypatch):
        screen = FakeScreen(width=1920, height=1080)
        session = make_session(monkeypatch, screen)
        screen.resize(1080, 1920)
        x, y = session.to_local(session.ref_width // 2, session.ref_height // 2)
        assert abs(x - 540) <= 3
        assert abs(y - 960) <= 3


class TestAxisGuardBoundary:
    def test_accepts_rounding_slack_and_clamps(self, session):
        assert session.to_local(1024.0, 0)[0] == 1919
        assert session.to_local(-0.9, 0)[0] == 0

    def test_rejects_just_beyond_the_slack(self, session):
        with pytest.raises(ActionError, match="outside the screenshot"):
            session.to_local(1025.5, 0)
        with pytest.raises(ActionError):
            session.to_local(-1.5, 0)

    def test_rejects_a_coordinate_wrong_on_both_axes(self, session):
        # Previously the 2% tolerance made this clamp silently to the far corner,
        # which at 1.875x is 38 native pixels of drift -- wider than a scrollbar.
        with pytest.raises(ActionError):
            session.to_local(1030, 580)


class TestCursorReporting:
    def test_reports_off_display_rather_than_a_bogus_coordinate(self, monkeypatch, fake_input):
        screen = FakeScreen(width=1080, height=1920, origin_x=-1080, origin_y=-696)
        session = make_session(monkeypatch, screen)
        fake_input.cursor = (0, 0)  # on the primary, not this display
        x, y, on_display = session.cursor_in_screenshot_space()
        assert not on_display
        # Whatever it reports must be a coordinate to_screen will accept.
        assert 0 <= x < session.ref_width
        assert 0 <= y < session.ref_height
        session.to_screen(x, y)

    def test_never_reports_a_negative(self, monkeypatch, fake_input):
        screen = FakeScreen(width=1080, height=1920, origin_x=-1080, origin_y=-696)
        session = make_session(monkeypatch, screen)
        fake_input.cursor = (-5000, -5000)
        x, y, on_display = session.cursor_in_screenshot_space()
        assert not on_display
        assert x >= 0 and y >= 0

    def test_reported_position_round_trips(self, monkeypatch, fake_input):
        screen = FakeScreen(width=1920, height=1080)
        session = make_session(monkeypatch, screen)
        for cursor in ((0, 0), (960, 540), (1919, 1079), (1, 1078)):
            fake_input.cursor = cursor
            x, y, on_display = session.cursor_in_screenshot_space()
            assert on_display
            lx, ly = session.to_local(x, y)
            # Must land in the same screenshot pixel it was reported from. Asserted
            # as the span property rather than by rescaling: a rescale is not the
            # inverse of _span, and asserting it with the same wrong formula the code
            # used is exactly how the off-by-one in cursor_position went unnoticed.
            assert (x * 1920) // session.ref_width <= lx
            assert lx < max(((x + 1) * 1920) // session.ref_width,
                            (x * 1920) // session.ref_width + 1)
            assert (y * 1080) // session.ref_height <= ly
            assert ly < max(((y + 1) * 1080) // session.ref_height,
                            (y * 1080) // session.ref_height + 1)


class TestZoomRegion:
    def test_exact_rectangle(self, session, fake_screen):
        session.zoom([100, 50, 300, 200])
        call = fake_screen.calls[-1]
        # 1024x576 -> 1920x1080. Both corners floor through the downscaler's spans.
        assert call["rx"] == (100 * 1920) // 1024
        assert call["ry"] == (50 * 1080) // 576
        assert call["rx"] + call["rw"] == (300 * 1920) // 1024
        assert call["ry"] + call["rh"] == (200 * 1080) // 576

    def test_rejects_a_far_corner_outside_the_frame(self, session):
        # This path used to skip validation entirely, silently returning most of the
        # screen when the model reasoned in native pixels.
        with pytest.raises(ActionError, match="outside the screenshot"):
            session.zoom([100, 50, 1900, 1000])
        with pytest.raises(ActionError):
            session.zoom([0, 0, 99999, 99999])

    def test_accepts_the_exclusive_far_edge(self, session, fake_screen):
        session.zoom([0, 0, session.ref_width, session.ref_height])
        call = fake_screen.calls[-1]
        assert call["rx"] == 0 and call["ry"] == 0
        assert call["rx"] + call["rw"] == 1920
        assert call["ry"] + call["rh"] == 1080

    def test_tiny_region_stays_valid(self, session, fake_screen):
        session.zoom([500, 300, 501, 301])
        call = fake_screen.calls[-1]
        assert call["rw"] >= 1 and call["rh"] >= 1
        assert call["rx"] + call["rw"] <= 1920
        assert call["ry"] + call["rh"] <= 1080

    def test_sub_pixel_region_does_not_invert(self, session, fake_screen):
        session.zoom([100.4, 100.4, 100.5, 100.5])
        call = fake_screen.calls[-1]
        assert call["rw"] >= 1 and call["rh"] >= 1

    def test_rejects_non_finite(self, session):
        with pytest.raises(ActionError, match="finite"):
            session.zoom([0, 0, float("inf"), 100])

    @pytest.mark.parametrize("region", [[0, 0, 10, 10], [500, 200, 900, 500], [1, 1, 1023, 575]])
    def test_region_always_inside_the_display(self, session, fake_screen, region):
        session.zoom(region)
        call = fake_screen.calls[-1]
        assert 0 <= call["rx"] and call["rx"] + call["rw"] <= 1920
        assert 0 <= call["ry"] and call["ry"] + call["rh"] <= 1080
