"""A zoom you can click: `in_zoom` maps a zoom pixel to the exact native pixel.

The full screenshot is fitted into a box, so on a 1920-wide display one of its
pixels covers nearly two real ones. That is fine for a button and useless for a
one-pixel border or a caret between two characters. A zoom is captured at native
resolution, so with `in_zoom` set its coordinates address individual real pixels.

The tests that matter here are the ones that check a coordinate lands where it was
aimed, because the failure mode is silent: a wrong mapping clicks something
plausible and adjacent rather than raising.
"""

from __future__ import annotations

import pytest

from cufast.actions import run_batch
from cufast.session import ActionError


def action_of(result) -> str:
    return result.label.split(" (")[0]


def last_move(fake_input) -> tuple[int, int]:
    """Where the cursor was last sent, in absolute desktop pixels."""
    moves = [e for e in fake_input.events if e[0] == "mouse_move"]
    return (moves[-1][1], moves[-1][2])


def last_drag(fake_input) -> tuple[int, int, int, int]:
    drags = [e for e in fake_input.events if e[0] == "mouse_drag"]
    return drags[-1][1:5]


class TestAZoomPixelIsANativePixel:
    def test_the_origin_of_the_zoom_is_the_origin_of_the_region(
        self, session, fake_screen, fake_input
    ):
        session.zoom([100, 50, 200, 100])
        rx, ry, _, _ = session.last_zoom.region
        x, y = session.zoom_to_screen(0, 0)
        assert (x, y) == (rx + session.screen.origin_x, ry + session.screen.origin_y)

    def test_walking_one_pixel_in_the_zoom_walks_one_native_pixel(
        self, session, fake_screen, fake_input
    ):
        # The whole claim of the feature. If this ever moves by two, the zoom is
        # being treated as scaled and every precise click is off by half a pixel and
        # growing across the image.
        session.zoom([100, 50, 300, 150])
        shot = session.last_zoom
        if shot.width != shot.region[2]:
            pytest.skip("zoom was scaled to fit, so its pixels are not 1:1 here")
        first = session.zoom_to_screen(10, 10)
        second = session.zoom_to_screen(11, 10)
        assert second[0] - first[0] == 1
        assert second[1] == first[1]

    def test_it_lands_inside_the_region_at_the_far_corner(
        self, session, fake_screen, fake_input
    ):
        session.zoom([100, 50, 200, 100])
        shot = session.last_zoom
        rx, ry, rw, rh = shot.region
        x, y = session.zoom_to_screen(shot.width - 1, shot.height - 1)
        lx = x - session.screen.origin_x
        ly = y - session.screen.origin_y
        assert rx <= lx < rx + rw
        assert ry <= ly < ry + rh

    def test_the_zoom_is_more_precise_than_the_full_screenshot(
        self, session, fake_screen, fake_input
    ):
        """Two adjacent zoom pixels must be reachable separately. Through the full
        screenshot on a scaled display they are not, which is the reason this exists.
        """
        session.zoom([100, 50, 300, 150])
        shot = session.last_zoom
        if shot.width != shot.region[2]:
            pytest.skip("zoom was scaled to fit, so its pixels are not 1:1 here")
        points = {session.zoom_to_screen(i, 5) for i in range(4)}
        assert len(points) == 4


class TestItRefusesRatherThanGuessing:
    def test_in_zoom_before_any_zoom_is_an_error(self, session, fake_input):
        # Silently falling back to full-screenshot coordinates would put the click
        # somewhere real and wrong, which is worse than refusing.
        with pytest.raises(ActionError, match="nothing has been zoomed yet"):
            session.zoom_to_screen(5, 5)

    def test_a_coordinate_outside_the_zoom_is_refused(
        self, session, fake_screen, fake_input
    ):
        session.zoom([100, 50, 200, 100])
        with pytest.raises(ActionError, match="outside the zoom image"):
            session.zoom_to_screen(session.last_zoom.width + 50, 5)

    def test_the_error_says_which_space_the_numbers_are_in(
        self, session, fake_screen, fake_input
    ):
        session.zoom([100, 50, 200, 100])
        with pytest.raises(ActionError, match="zoom's own pixel space"):
            session.zoom_to_screen(-99, 5)

    def test_in_zoom_must_be_a_boolean(self, session, fake_input):
        # "false" is a non-empty string. Read as truthiness it would switch
        # coordinate space while saying the opposite.
        # Rejected before anything runs, like every other malformed parameter: a
        # batch must never half-apply because its last action was wrong.
        with pytest.raises(ActionError, match="in_zoom must be true or false"):
            run_batch(
                session,
                [{"action": "left_click", "coordinate": [10, 10], "in_zoom": "false"}],
                False,
            )


class TestItReachesTheActions:
    def test_a_click_can_be_given_in_zoom_coordinates(
        self, session, fake_screen, fake_input
    ):
        results = run_batch(
            session,
            [
                {"action": "zoom", "region": [100, 50, 200, 100]},
                {"action": "left_click", "coordinate": [5, 5], "in_zoom": True},
            ],
            False,
        )
        assert not any(r.is_error for r in results)
        assert action_of(results[0]) == "zoom"

    def test_the_click_lands_where_the_zoom_says(self, session, fake_screen, fake_input):
        run_batch(session, [{"action": "zoom", "region": [100, 50, 200, 100]}], False)
        expected = session.zoom_to_screen(7, 3)
        fake_input.events.clear()
        run_batch(
            session,
            [{"action": "left_click", "coordinate": [7, 3], "in_zoom": True}],
            False,
        )
        assert last_move(fake_input) == expected

    def test_without_the_flag_nothing_changes(self, session, fake_screen, fake_input):
        run_batch(session, [{"action": "zoom", "region": [100, 50, 200, 100]}], False)
        expected = session.to_screen(7, 3)
        fake_input.events.clear()
        run_batch(session, [{"action": "left_click", "coordinate": [7, 3]}], False)
        assert last_move(fake_input) == expected

    def test_a_drag_uses_the_same_space_for_both_ends(
        self, session, fake_screen, fake_input
    ):
        # One end in zoom space and the other in screenshot space would be a drag
        # across the whole display rather than across a control.
        run_batch(session, [{"action": "zoom", "region": [100, 50, 300, 150]}], False)
        start = session.zoom_to_screen(2, 2)
        end = session.zoom_to_screen(20, 8)
        fake_input.events.clear()
        results = run_batch(
            session,
            [{"action": "left_click_drag", "start_coordinate": [2, 2],
              "coordinate": [20, 8], "in_zoom": True}],
            False,
        )
        assert not any(r.is_error for r in results)
        assert last_drag(fake_input) == (*start, *end)


class TestTheModelIsTold:
    def test_the_zoom_result_reports_what_it_gained(
        self, session, fake_screen, fake_input
    ):
        results = run_batch(
            session, [{"action": "zoom", "region": [100, 50, 200, 100]}], False
        )
        assert "native area" in results[0].text
        assert "in_zoom=true" in results[0].text

    def test_the_description_explains_pixel_perfect_targeting(self):
        from cufast.server import TOOL_DESCRIPTION

        assert "PIXEL-PERFECT TARGETING" in TOOL_DESCRIPTION
        assert "in_zoom" in TOOL_DESCRIPTION
