"""The standard computer tool's call shape works here without translation.

The project's stated aim is that a model's existing priors about `left_click`,
`scroll_direction` and friends transfer with no adaptation. Names alone were not
enough: the tool still demanded `actions: [...]` for a single click, which is a
translation step the model has to learn before anything works at all. Both shapes
are accepted now -- the familiar one so the first call succeeds, the batch one
because that is where the latency actually goes.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cufast.actions import FLAT_PARAMS, batch_from_call
from cufast.session import ActionError


class TestOneActionWithItsParametersAlongside:
    def test_a_click_is_the_shape_the_model_already_knows(self):
        assert batch_from_call("left_click", None, {"coordinate": [4, 5]}) == [
            {"action": "left_click", "coordinate": [4, 5]}
        ]

    def test_omitted_parameters_are_not_passed_through_as_none(self):
        # A None sibling is "not supplied", not "supplied as null". Passing it on
        # would trip the unknown-parameter check on every single-action call.
        assert batch_from_call("screenshot", None, {"coordinate": None, "text": None}) == [
            {"action": "screenshot"}
        ]

    def test_every_standard_parameter_survives_the_trip(self):
        flat = {
            "start_coordinate": [1, 2],
            "coordinate": [3, 4],
            "text": "shift",
            "scroll_direction": "down",
            "scroll_amount": 3,
            "duration": 1.5,
            "repeat": 2,
        }
        got = batch_from_call("left_click_drag", None, dict(flat))[0]
        assert {k: v for k, v in got.items() if k != "action"} == flat


class TestTheBatchShapeStillWorks:
    def test_a_list_is_passed_through_untouched(self):
        actions = [{"action": "type", "text": "hi"}, {"action": "key", "text": "Return"}]
        assert batch_from_call(None, actions, {}) == actions

    def test_an_empty_list_is_left_for_run_batch_to_reject(self):
        # run_batch owns that message; duplicating it here would let the two drift.
        assert batch_from_call(None, [], {}) == []


class TestTheTwoShapesAreNotMixed:
    def test_both_at_once_is_refused(self):
        with pytest.raises(ActionError, match="not both"):
            batch_from_call("screenshot", [{"action": "screenshot"}], {})

    def test_parameters_beside_a_list_are_refused(self):
        # Silently dropping them would run a click at the wrong place, or a `type`
        # with no text, and look like the harness ignoring the request.
        with pytest.raises(ActionError, match="belongs inside the `actions` list"):
            batch_from_call(None, [{"action": "left_click"}], {"coordinate": [1, 2]})

    def test_neither_is_refused_with_the_valid_actions_listed(self):
        with pytest.raises(ActionError, match="either `action`"):
            batch_from_call(None, None, {})


class TestTheToolSignatureMatchesTheParameterList:
    """FLAT_PARAMS and the tool's own signature have to stay in step.

    A parameter added to one and not the other is invisible: the model would send it
    and the harness would drop it on the floor, which reads as the action silently
    not working rather than as an error.
    """

    def test_every_flat_param_is_accepted_and_forwarded(self):
        import cufast.server as server_mod

        source = inspect.getsource(server_mod.build_server)
        for param in FLAT_PARAMS:
            # Declared in the signature...
            assert f"{param}:" in source, f"{param} is in FLAT_PARAMS but not the signature"
            # ...and actually passed on. Accepting a parameter and then dropping it
            # is worse than rejecting it: the model sees the action succeed and the
            # parameter do nothing.
            assert f'"{param}": {param},' in source, f"{param} is accepted but not forwarded"


class TestTheFlatShapeWorksThroughTheRealToolCall:
    """The helper being right is not the claim. The claim is that a model sending
    the shape it already knows gets a working call, so this goes through the MCP
    surface rather than through batch_from_call."""

    @pytest.fixture
    def server(self, monkeypatch, fake_screen, fake_input):
        from cufast import _native
        from cufast.config import Config
        from cufast.server import build_server

        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        return build_server(Config(max_width=1024, max_height=768, settle_ms=0))

    def call(self, server, args):
        return asyncio.run(server.call_tool("computer", args))

    def test_a_bare_screenshot(self, server):
        result = self.call(server, {"action": "screenshot"})
        assert any(b.type == "image" for b in result.content)

    def test_a_click_with_its_coordinate_alongside(self, server, fake_input):
        self.call(server, {"action": "left_click", "coordinate": [400, 300]})
        assert "mouse_click" in fake_input.names()

    def test_type_with_text_alongside(self, server, fake_input):
        self.call(server, {"action": "type", "text": "hello"})
        assert ("type_text", "hello") in fake_input.events

    def test_scroll_with_its_own_parameters(self, server, fake_input):
        self.call(
            server,
            {"action": "scroll", "scroll_direction": "down", "scroll_amount": 3},
        )
        assert "mouse_scroll" in fake_input.names()

    def test_a_bad_action_still_reports_properly(self, server):
        with pytest.raises(ToolError, match="unknown action"):
            self.call(server, {"action": "nonsense", "auto_screenshot": False})

    def test_the_batch_shape_still_works_through_the_tool(self, server, fake_input):
        self.call(
            server,
            {"actions": [{"action": "type", "text": "hi"},
                         {"action": "key", "text": "Return"}]},
        )
        assert ("type_text", "hi") in fake_input.events
        assert "press_key" in fake_input.names()

    def test_mixing_the_shapes_is_refused_at_the_tool(self, server):
        with pytest.raises(ToolError, match="not both"):
            self.call(
                server,
                {"action": "screenshot", "actions": [{"action": "screenshot"}]},
            )

    def test_neither_shape_is_refused_at_the_tool(self, server):
        with pytest.raises(ToolError, match="either `action`"):
            self.call(server, {"auto_screenshot": False})


class TestTheDescriptionLeadsWithTheFamiliarShape:
    def test_it_says_it_works_like_the_tool_the_model_knows(self):
        from cufast.server import TOOL_DESCRIPTION

        first = TOOL_DESCRIPTION.split("\n\n")[1]
        assert "computer tool you already know" in first

    def test_it_shows_both_shapes(self):
        from cufast.server import TOOL_DESCRIPTION

        assert '{"action": "type", "text": "hello"}' in TOOL_DESCRIPTION
        assert '"actions": [' in TOOL_DESCRIPTION
