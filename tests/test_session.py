"""Coordinate mapping.

This is the correctness-critical part of the harness: a wrong mapping does not raise,
it clicks the wrong thing.
"""

from __future__ import annotations

import pytest
from tests.conftest import FakeScreen

from cufast.config import Config
from cufast.session import ActionError, Session


def make_session(monkeypatch, screen, **cfg_kwargs):
    import cufast.session as session_mod
    from cufast import _native

    monkeypatch.setattr(_native, "Screen", lambda index: screen, raising=True)
    defaults = {"max_width": 1024, "max_height": 768, "settle_ms": 0}
    defaults.update(cfg_kwargs)
    return session_mod.Session(Config(**defaults))


class TestReferenceFrame:
    def test_landscape_fits_to_width(self, session):
        assert (session.ref_width, session.ref_height) == (1024, 576)

    def test_describe_mentions_both_sizes(self, session):
        text = session.describe()
        assert "1920x1080" in text and "1024x576" in text


class TestCoordinateMapping:
    def test_corners(self, session):
        assert session.to_screen(0, 0) == (0, 0)
        assert session.to_screen(1023, 575) == (1919, 1079)

    def test_centre(self, session):
        assert session.to_screen(512, 288) == (960, 540)

    def test_maps_to_pixel_centre_not_corner(self, session):
        # 1024 -> 1920 is 1.875 source pixels per destination pixel. Destination
        # pixel 1 covers source 1.875..3.75, so its centre lands on 2, not 1.
        assert session.to_screen(1, 0)[0] == 2

    def test_monotonic_and_in_range(self, session):
        previous = -1
        for x in range(0, 1024):
            sx, _ = session.to_screen(x, 0)
            assert 0 <= sx <= 1919
            assert sx >= previous
            previous = sx

    def test_applies_monitor_origin(self, monkeypatch):
        # The development machine's second display sits at a negative origin, which
        # is exactly the case a naive implementation gets wrong.
        screen = FakeScreen(width=1080, height=1920, origin_x=-1080, origin_y=-696, index=1)
        session = make_session(monkeypatch, screen)

        # Pixel-centre mapping, so the first screenshot pixel lands just inside the
        # display rather than exactly on its corner: at 2.5x, pixel 0 spans source
        # 0..2.5 and its centre is 1.25.
        x, y = session.to_screen(0, 0)
        assert x == -1080 + 1
        assert y == -696 + 1

        # The whole frame must stay inside this display and never reach the primary,
        # which starts at x = 0.
        for sx, sy in (
            session.to_screen(0, 0),
            session.to_screen(session.ref_width - 1, session.ref_height - 1),
            session.to_screen(session.ref_width // 2, session.ref_height // 2),
        ):
            assert -1080 <= sx <= -1
            assert -696 <= sy <= -696 + 1919

    def test_portrait_reference_frame(self, monkeypatch):
        screen = FakeScreen(width=1080, height=1920, origin_x=0, origin_y=0)
        session = make_session(monkeypatch, screen)
        assert session.ref_height == 768
        assert session.ref_width <= 1024


class TestCoordinateGuards:
    def test_rejects_native_resolution_coordinates(self, session):
        # The classic failure: the model reads 1920x1080 off the display and clicks
        # there instead of in screenshot space. Silently clamping would click the
        # right edge and look like it worked.
        with pytest.raises(ActionError, match="outside the screenshot"):
            session.to_screen(1500, 300)
        with pytest.raises(ActionError, match="native display resolution"):
            session.to_screen(300, 900)

    def test_rejects_negative(self, session):
        with pytest.raises(ActionError):
            session.to_screen(-50, 10)

    def test_tolerates_edge_rounding(self, session):
        # One past the edge is rounding, not confusion, so it clamps rather than raising.
        x, y = session.to_screen(1024, 576)
        assert x <= 1919 and y <= 1079

    def test_error_names_both_resolutions(self, session):
        with pytest.raises(ActionError) as excinfo:
            session.to_screen(1900, 100)
        message = str(excinfo.value)
        assert "1024x576" in message
        assert "1920x1080" in message


class TestCursorReporting:
    def test_reports_in_screenshot_space(self, session, fake_input):
        fake_input.cursor = (960, 540)
        assert session.cursor_in_screenshot_space() == (512, 288, True)

    def test_subtracts_monitor_origin(self, monkeypatch, fake_input):
        screen = FakeScreen(width=1920, height=1080, origin_x=-1920, origin_y=0)
        session = make_session(monkeypatch, screen)
        fake_input.cursor = (-960, 540)
        x, y, on_display = session.cursor_in_screenshot_space()
        assert (x, y) == (512, 288)
        assert on_display


class TestZoom:
    def test_requests_the_right_source_rectangle(self, session, fake_screen):
        session.zoom([100, 50, 300, 200])
        call = fake_screen.calls[-1]
        # Screenshot space is half the native width, so the region doubles.
        assert call["rx"] == pytest.approx(187, abs=3)
        assert call["ry"] == pytest.approx(93, abs=3)
        assert call["rw"] == pytest.approx(375, abs=4)
        assert call["rh"] == pytest.approx(281, abs=4)

    def test_rejects_inverted_region(self, session):
        with pytest.raises(ActionError, match="x1 > x0"):
            session.zoom([300, 50, 100, 200])
        with pytest.raises(ActionError, match="y1 > y0"):
            session.zoom([10, 200, 300, 50])

    def test_rejects_wrong_length(self, session):
        with pytest.raises(ActionError):
            session.zoom([1, 2, 3])

    def test_region_is_clamped_to_the_display(self, session, fake_screen):
        session.zoom([0, 0, session.ref_width, session.ref_height])
        call = fake_screen.calls[-1]
        assert call["rx"] + call["rw"] <= 1920
        assert call["ry"] + call["rh"] <= 1080

    def test_zoom_does_not_change_the_click_frame(self, session):
        # The spec is explicit that zoom images do not change the coordinate space.
        before = session.to_screen(400, 300)
        session.zoom([100, 50, 300, 200])
        assert session.to_screen(400, 300) == before
