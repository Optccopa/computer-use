"""MCP server behaviour, exercised in-process against a fake display."""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from tests.conftest import FakeScreen

from cufast import _native
from cufast.actions import ACTION_NAMES, FLAT_PARAMS
from cufast.config import Config
from cufast.server import Harness, build_server


@pytest.fixture
def harness(monkeypatch, fake_screen, fake_input):
    monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
    return Harness(Config(max_width=1024, max_height=768, settle_ms=0))


def run(coro):
    return asyncio.run(coro)


class TestToolSurface:
    @pytest.mark.anyio
    def test_exposes_exactly_two_tools(self, monkeypatch, fake_screen):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(settle_ms=0))
        tools = run(server.list_tools())
        assert {t.name for t in tools} == {"computer", "screen_info"}

    def test_computer_schema_has_the_documented_parameters(self, monkeypatch, fake_screen):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(settle_ms=0))
        tool = next(t for t in run(server.list_tools()) if t.name == "computer")
        # `action` plus its parameters is the standard computer tool's shape, kept so
        # a model's existing priors work here with no translation; `actions` is the
        # batch form. Tied to FLAT_PARAMS so the two cannot drift: a parameter in one
        # and not the other would be sent by the model and silently dropped.
        assert set(tool.input_schema["properties"]) == {
            "action", "actions", "auto_screenshot", "display", *FLAT_PARAMS,
        }

    def test_description_documents_every_action(self, monkeypatch, fake_screen):
        # Guards against an action being added to the dispatcher but never described,
        # which the model would then never know to use.
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(settle_ms=0))
        tool = next(t for t in run(server.list_tools()) if t.name == "computer")
        for name in ACTION_NAMES:
            assert name in tool.description, f"{name} is not in the tool description"

    def test_rejects_an_invalid_config(self, monkeypatch, fake_screen):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        with pytest.raises(ValueError):
            build_server(Config(max_width=9000))


class TestHarness:
    def test_all_native_work_happens_on_one_thread(self, harness, fake_screen):
        seen: set[int] = set()
        original = fake_screen.grab

        def record(**kwargs):
            seen.add(threading.get_ident())
            return original(**kwargs)

        fake_screen.grab = record
        run(harness.run([{"action": "screenshot"}], False, None))
        run(harness.run([{"action": "screenshot"}], False, None))
        run(harness.describe(None))
        assert len(seen) == 1
        assert seen != {threading.get_ident()}  # and it is not the calling thread

    def test_sessions_are_cached_per_display(self, monkeypatch, fake_input):
        built: list[int] = []

        def make(index):
            built.append(index)
            return FakeScreen(index=index)

        monkeypatch.setattr(_native, "Screen", make, raising=True)
        harness = Harness(Config(settle_ms=0))
        run(harness.session(0))
        run(harness.session(0))
        run(harness.session(1))
        run(harness.session(0))
        assert built == [0, 1]

    def test_display_selection_persists(self, monkeypatch, fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: FakeScreen(index=index),
                            raising=True)
        harness = Harness(Config(settle_ms=0))
        assert run(harness.session(1)).screen.index == 1
        # Later calls without an explicit display stay on the chosen one.
        assert run(harness.session(None)).screen.index == 1
        assert run(harness.session(0)).screen.index == 0


class TestComputerTool:
    @pytest.fixture
    def tool(self, monkeypatch, fake_screen, fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(max_width=1024, max_height=768, settle_ms=0))
        return server, fake_screen

    def call(self, server, args):
        return run(server.call_tool("computer", args))

    def test_screenshot_returns_a_decodable_image(self, tool):
        server, _ = tool
        result = self.call(server, {"actions": [{"action": "screenshot"}]})
        images = [b for b in result.content if b.type == "image"]
        assert len(images) == 1
        assert images[0].mime_type == "image/jpeg"
        assert base64.b64decode(images[0].data)

    def test_image_is_preceded_by_a_label(self, tool):
        server, _ = tool
        result = self.call(server, {"actions": [{"action": "screenshot"}]})
        assert result.content[0].type == "text"
        assert "screenshot" in result.content[0].text
        assert result.content[1].type == "image"

    def test_batch_appends_a_screenshot(self, tool):
        server, _ = tool
        result = self.call(server, {"actions": [{"action": "cursor_position"}]})
        assert any(b.type == "image" for b in result.content)

    def test_malformed_batch_is_a_protocol_error(self, tool):
        server, _ = tool
        # call_tool raises; the kernel turns this into CallToolResult(is_error=True).
        with pytest.raises(ToolError, match="unknown action"):
            self.call(server, {"actions": [{"action": "nonsense"}]})

    def test_failure_without_a_capture_is_a_protocol_error(self, tool):
        server, _ = tool
        with pytest.raises(ToolError, match="outside the screenshot"):
            self.call(
                server,
                {
                    "actions": [{"action": "left_click", "coordinate": [5000, 5000]}],
                    "auto_screenshot": False,
                },
            )

    def test_failure_with_a_capture_keeps_the_image(self, tool):
        server, _ = tool
        result = self.call(
            server,
            {
                "actions": [
                    {"action": "screenshot"},
                    {"action": "left_click", "coordinate": [5000, 5000]},
                ]
            },
        )
        # The screenshot is worth more to the model than the protocol flag, so it is
        # kept and the failure is reported as a leading text block.
        assert any(b.type == "image" for b in result.content)
        assert "BATCH FAILED" in result.content[0].text

    def test_a_failing_capture_does_not_discard_the_batch(self, tool):
        server, screen = tool
        screen.raise_on_grab = RuntimeError("DXGI: device removed")
        result = self.call(server, {"actions": [{"action": "cursor_position"}]})
        text = " ".join(b.text for b in result.content if b.type == "text")
        # The cursor_position result must survive, and the real cause must be visible
        # rather than collapsed into "Error executing tool computer".
        assert "cursor_position" in text
        assert "device removed" in text

    def test_empty_batch_is_rejected(self, tool):
        server, _ = tool
        with pytest.raises(ToolError):
            self.call(server, {"actions": []})

    def test_display_argument_switches_display(self, monkeypatch, fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: FakeScreen(index=index),
                            raising=True)
        server = build_server(Config(settle_ms=0))
        result = run(server.call_tool("screen_info", {"display": 1}))
        assert "display 1" in result.content[0].text


class TestScreenInfo:
    def test_reports_both_sizes_and_the_capture_path(self, monkeypatch, fake_screen,
                                                     fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(max_width=1024, max_height=768, settle_ms=0))
        text = run(server.call_tool("screen_info", {})).content[0].text
        assert "1920x1080" in text
        assert "1024x576" in text
        assert "DXGI" in text

    def test_lists_windows_device_names(self, monkeypatch, fake_screen, fake_input):
        monkeypatch.setattr(_native, "Screen", lambda index: fake_screen, raising=True)
        server = build_server(Config(settle_ms=0))
        text = run(server.call_tool("screen_info", {})).content[0].text
        # The index here is zero-based while Windows names displays from 1, and that
        # ambiguity is worth stating explicitly.
        assert "DISPLAY" in text
        assert "zero-based" in text

    def test_bad_display_is_reported_not_crashed(self, monkeypatch, fake_input):
        def explode(index):
            raise RuntimeError(f"display_index {index} out of range (2 display(s) attached)")

        monkeypatch.setattr(_native, "Screen", explode, raising=True)
        server = build_server(Config(settle_ms=0))
        with pytest.raises(ToolError, match="out of range"):
            run(server.call_tool("screen_info", {"display": 9}))
