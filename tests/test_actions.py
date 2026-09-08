"""Action dispatch, validation, and batch semantics."""

from __future__ import annotations

import time

import pytest

from cufast.actions import NOT_EXECUTED, execute, run_batch
from cufast.config import Config
from cufast.session import ActionError


class TestSingleActions:
    def test_click_moves_then_clicks(self, session, fake_input):
        execute(session, "left_click", {"coordinate": [512, 288]})
        assert fake_input.events == [
            ("mouse_move", 960, 540),
            ("mouse_click", "left", 1, ""),
        ]

    def test_click_without_coordinate_does_not_move(self, session, fake_input):
        execute(session, "left_click", {})
        assert fake_input.names() == ["mouse_click"]

    def test_click_counts(self, session, fake_input):
        for name, expected in (("double_click", 2), ("triple_click", 3)):
            fake_input.events.clear()
            execute(session, name, {})
            assert fake_input.events[0][2] == expected

    def test_click_passes_modifiers(self, session, fake_input):
        execute(session, "left_click", {"coordinate": [10, 10], "text": "ctrl+shift"})
        assert fake_input.events[-1] == ("mouse_click", "left", 1, "ctrl+shift")

    def test_right_and_middle_click(self, session, fake_input):
        execute(session, "right_click", {})
        execute(session, "middle_click", {})
        assert [e[1] for e in fake_input.events] == ["right", "middle"]

    def test_drag_maps_both_endpoints(self, session, fake_input):
        execute(session, "left_click_drag", {"start_coordinate": [0, 0], "coordinate": [512, 288]})
        assert fake_input.events == [("mouse_drag", 0, 0, 960, 540, "")]

    def test_scroll(self, session, fake_input):
        execute(session, "scroll", {"scroll_direction": "down", "scroll_amount": 3})
        assert fake_input.events == [("mouse_scroll", "down", 3, "")]

    def test_scroll_with_coordinate_moves_first(self, session, fake_input):
        execute(
            session,
            "scroll",
            {"coordinate": [512, 288], "scroll_direction": "up", "scroll_amount": 1},
        )
        assert fake_input.names() == ["mouse_move", "mouse_scroll"]

    def test_type_and_key(self, session, fake_input):
        execute(session, "type", {"text": "hello"})
        execute(session, "key", {"text": "ctrl+s"})
        execute(session, "key", {"text": "Tab", "repeat": 4})
        assert fake_input.events == [
            ("type_text", "hello"),
            ("press_key", "ctrl+s", 1),
            ("press_key", "Tab", 4),
        ]

    def test_mouse_down_up(self, session, fake_input):
        execute(session, "left_mouse_down", {})
        execute(session, "left_mouse_up", {})
        assert fake_input.names() == ["mouse_down", "mouse_up"]

    def test_cursor_position_reports_screenshot_space(self, session, fake_input):
        fake_input.cursor = (960, 540)
        result = execute(session, "cursor_position", {})
        assert result.text == "X=512, Y=288"

    def test_screenshot_returns_an_image(self, session):
        result = execute(session, "screenshot", {})
        assert result.image is not None
        assert result.image.media_type == "image/jpeg"

    def test_wait_actually_waits(self, session):
        start = time.perf_counter()
        execute(session, "wait", {"duration": 0.05})
        assert time.perf_counter() - start >= 0.04


class TestValidation:
    def test_unknown_action_lists_valid_ones(self, session):
        with pytest.raises(ActionError, match="unknown action") as excinfo:
            execute(session, "hover", {})
        assert "left_click" in str(excinfo.value)

    def test_unknown_parameter_is_rejected(self, session):
        with pytest.raises(ActionError, match="does not take"):
            execute(session, "left_click", {"coordinates": [1, 2]})

    def test_unknown_parameter_names_the_valid_ones(self, session):
        with pytest.raises(ActionError) as excinfo:
            execute(session, "type", {"txt": "oops"})
        assert "text" in str(excinfo.value)

    def test_bad_coordinate_shape(self, session):
        with pytest.raises(ActionError, match=r"\[x, y\]"):
            execute(session, "mouse_move", {"coordinate": [1, 2, 3]})

    def test_non_numeric_coordinate(self, session):
        with pytest.raises(ActionError):
            execute(session, "mouse_move", {"coordinate": ["a", "b"]})

    def test_missing_required_parameters(self, session):
        with pytest.raises(ActionError):
            execute(session, "mouse_move", {})
        with pytest.raises(ActionError):
            execute(session, "type", {})
        with pytest.raises(ActionError):
            execute(session, "scroll", {"scroll_direction": "down"})
        with pytest.raises(ActionError):
            execute(session, "left_click_drag", {"coordinate": [1, 1]})

    def test_repeat_bounds(self, session):
        with pytest.raises(ActionError, match="between 1 and 100"):
            execute(session, "key", {"text": "a", "repeat": 0})
        with pytest.raises(ActionError, match="between 1 and 100"):
            execute(session, "key", {"text": "a", "repeat": 101})

    def test_duration_bounds(self, session):
        with pytest.raises(ActionError, match="between 0 and 300"):
            execute(session, "wait", {"duration": 301})
        with pytest.raises(ActionError, match="between 0 and 300"):
            execute(session, "wait", {"duration": -1})

    def test_negative_scroll_amount(self, session):
        with pytest.raises(ActionError, match="negative"):
            execute(session, "scroll", {"scroll_direction": "up", "scroll_amount": -2})


class TestBatch:
    def test_runs_in_order(self, session, fake_input):
        run_batch(
            session,
            [
                {"action": "left_click", "coordinate": [512, 288]},
                {"action": "type", "text": "hi"},
            ],
            auto_screenshot=False,
        )
        assert fake_input.names() == ["mouse_move", "mouse_click", "type_text"]

    def test_appends_screenshot_by_default(self, session):
        results = run_batch(session, [{"action": "left_click"}])
        assert results[-1].label == "screenshot"
        assert results[-1].image is not None

    def test_does_not_double_up_a_trailing_screenshot(self, session):
        results = run_batch(session, [{"action": "left_click"}, {"action": "screenshot"}])
        assert [r.label for r in results] == ["left_click", "screenshot"]

    def test_trailing_zoom_also_counts_as_a_capture(self, session):
        results = run_batch(
            session, [{"action": "left_click"}, {"action": "zoom", "region": [0, 0, 100, 100]}]
        )
        assert len(results) == 2

    def test_auto_screenshot_can_be_disabled(self, session):
        results = run_batch(session, [{"action": "left_click"}], auto_screenshot=False)
        assert len(results) == 1

    def test_stops_at_first_failure(self, session, fake_input):
        results = run_batch(
            session,
            [
                {"action": "left_click", "coordinate": [10, 10]},
                {"action": "key", "text": "ctrl+s", "repeat": 999},
                {"action": "type", "text": "never runs"},
            ],
            auto_screenshot=False,
        )
        assert results[0].is_error is False
        assert results[1].is_error is True
        assert results[2].is_error is True
        # The spec fixes this wording exactly.
        assert results[2].text == NOT_EXECUTED
        assert "type_text" not in fake_input.names()

    def test_no_screenshot_is_appended_after_a_failure(self, session):
        results = run_batch(session, [{"action": "nonsense"}])
        assert len(results) == 1
        assert results[0].is_error

    def test_out_of_range_click_fails_the_batch(self, session, fake_input):
        results = run_batch(
            session,
            [{"action": "left_click", "coordinate": [1900, 100]}, {"action": "type", "text": "x"}],
            auto_screenshot=False,
        )
        assert results[0].is_error
        assert "outside the screenshot" in results[0].text
        assert results[1].text == NOT_EXECUTED

    def test_rejects_empty_batch(self, session):
        with pytest.raises(ActionError):
            run_batch(session, [])

    def test_rejects_malformed_entries(self, session):
        with pytest.raises(ActionError, match="must be an object"):
            run_batch(session, ["left_click"])
        with pytest.raises(ActionError, match="missing the required"):
            run_batch(session, [{"coordinate": [1, 2]}])

    def test_settle_delay_applies_between_mutating_actions(self, monkeypatch, fake_screen,
                                                           fake_input):
        from cufast import _native
        import cufast.session as session_mod

        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        slow = session_mod.Session(Config(max_width=1024, max_height=768, settle_ms=30))

        slept: list[float] = []
        monkeypatch.setattr("cufast.actions.time.sleep", slept.append)
        run_batch(
            slow,
            [{"action": "left_click"}, {"action": "type", "text": "x"}],
            auto_screenshot=True,
        )
        # One delay between the two actions, one before the trailing screenshot.
        assert slept == [0.03, 0.03]

    def test_no_settle_delay_after_a_read_only_action(self, monkeypatch, fake_screen, fake_input):
        from cufast import _native
        import cufast.session as session_mod

        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        slow = session_mod.Session(Config(max_width=1024, max_height=768, settle_ms=30))
        slept: list[float] = []
        monkeypatch.setattr("cufast.actions.time.sleep", slept.append)
        run_batch(slow, [{"action": "cursor_position"}], auto_screenshot=True)
        assert slept == []
