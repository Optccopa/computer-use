"""The Claude Code plugin: its manifests, its shim, and the hook binary.

None of this had a test, and it is the part that runs before every single turn.
When it breaks it breaks quietly by design -- the hook prints an empty object and
exits 0 rather than failing the turn -- so a regression here looks like the model
simply not knowing what is on screen any more.

The manifest checks need nothing but the files. The hook checks need the built
binary and a real display, so they are marked desktop and skip without it.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"
HOOK_EXE = PLUGIN / "bin" / "cufast.exe"
SHIM = PLUGIN / "bin" / "cufast-mcp.cmd"


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestTheManifestsAgree:
    def test_the_marketplace_points_at_the_plugin_by_the_name_it_uses(self):
        # Installing is `claude plugin install <name>@<marketplace>`, so a rename on
        # one side leaves an install command that names nothing.
        market = load(REPO / ".claude-plugin" / "marketplace.json")
        manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
        entry = next(p for p in market["plugins"] if p["source"] == "./plugin")
        assert entry["name"] == manifest["name"]

    def test_every_file_the_manifests_run_is_one_the_build_produces(self):
        """`${CLAUDE_PLUGIN_ROOT}/bin/x` has to be a real bin/x.

        Both the hook command and the MCP server are strings, so a moved or renamed
        file is not a broken reference anyone sees -- Claude Code just runs
        something that is not there.
        """
        manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
        commands = [
            hook["command"]
            for group in manifest["hooks"].values()
            for entry in group
            for hook in entry["hooks"]
        ]
        commands.append(load(PLUGIN / ".mcp.json")["mcpServers"]["cufast"]["command"])

        for command in commands:
            named = command.split()[0].replace("${CLAUDE_PLUGIN_ROOT}/", "")
            target = PLUGIN / named
            # cufast.exe is gitignored and built, so a clean clone legitimately has
            # no copy of it. The directory it belongs in still has to exist.
            assert target.parent.is_dir(), command
            if target.name != "cufast.exe":
                assert target.is_file(), command

    def test_the_hook_runs_on_both_events_the_context_depends_on(self):
        # SessionStart alone would leave the picture frozen at whatever the desktop
        # showed when Claude Code started.
        manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
        assert set(manifest["hooks"]) == {"SessionStart", "UserPromptSubmit"}

    def test_the_skill_declares_a_name_and_a_description(self):
        # No description means nothing tells the model when the skill applies, and a
        # skill that never loads is indistinguishable from one that was never added.
        text = (PLUGIN / "skills" / "driving-the-desktop" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        front = text.split("---")[1]
        assert "name: driving-the-desktop" in front
        assert "description:" in front


class TestTheShimCanFindAnInterpreter:
    def test_it_reads_the_file_the_build_writes(self):
        """The stamp filename lives in two places and they must not drift.

        CMake writes plugin/bin/interpreter.txt and the shim reads
        %~dp0interpreter.txt. A rename on either side is silent: the shim simply
        falls through to the next candidate, which on an installed plugin is
        whatever python PATH happens to name.
        """
        assert "interpreter.txt" in SHIM.read_text(encoding="utf-8")
        assert "plugin/bin/interpreter.txt" in (REPO / "CMakeLists.txt").read_text(
            encoding="utf-8"
        )

    def test_the_stamp_is_never_committed(self):
        # It is an absolute path on one machine, so a committed copy sends everyone
        # else's plugin at an interpreter that does not exist.
        ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
        assert "plugin/bin/interpreter.txt" in ignored

    def test_it_refuses_rather_than_starting_an_interpreter_that_cannot_import(self):
        """The failure this shim exists to prevent has to be loud.

        Running `python -m cufast.server` blind against whatever PATH names exits
        with ModuleNotFoundError on stderr that nobody reads, and the tool just
        never appears.
        """
        text = SHIM.read_text(encoding="utf-8")
        assert 'python -c "import cufast"' in text
        assert "exit /b 1" in text


@pytest.mark.desktop
class TestTheHookBinary:
    """What Claude Code actually runs before a turn.

    Marked desktop because it captures the real screen. It writes only to a path
    given on the command line, so it touches nothing of the developer's.
    """

    @pytest.fixture(autouse=True)
    def built(self):
        if not HOOK_EXE.is_file():
            pytest.skip("plugin/bin/cufast.exe is not built")

    def run(self, tmp_path, event="UserPromptSubmit", env=None, name="shot.jpg"):
        out = tmp_path / name
        proc = subprocess.run(
            [str(HOOK_EXE), "hook", event, "--out", str(out)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        return proc, out

    def test_it_prints_one_json_object_naming_the_event(self, tmp_path):
        proc, out = self.run(tmp_path)
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)["hookSpecificOutput"]
        assert payload["hookEventName"] == "UserPromptSubmit"
        assert str(out) in payload["additionalContext"]

    def test_it_writes_a_jpeg_where_it_says_it_did(self, tmp_path):
        _, out = self.run(tmp_path)
        assert out.read_bytes()[:2] == b"\xff\xd8"

    def test_the_written_size_is_the_size_it_reports(self, tmp_path):
        """The context states the image dimensions and the model measures pixels
        against them. A number that does not match the file is a coordinate space
        the model cannot see is wrong."""
        proc, out = self.run(tmp_path)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        blob = out.read_bytes()
        # SOF0/SOF2 marker: height then width, big endian, after the length and
        # sample precision. Read from the file rather than trusted from the text.
        index = blob.find(b"\xff\xc0")
        if index < 0:
            index = blob.find(b"\xff\xc2")
        height = int.from_bytes(blob[index + 5:index + 7], "big")
        width = int.from_bytes(blob[index + 7:index + 9], "big")
        assert f"{width}x{height}" in context

    def test_quality_follows_the_same_variable_the_server_reads(self, tmp_path, monkeypatch):
        """It was hardcoded at 0.75 while cufast.config read the environment.

        The file header promises the hook's picture matches what the tool returns.
        Two settings did not, and the cursor one is visible: CUFAST_DRAW_CURSOR=false
        hid the pointer from every tool result while leaving it burnt into the image
        handed over before the turn.
        """
        import os

        low = dict(os.environ, CUFAST_JPEG_QUALITY="0.2")
        high = dict(os.environ, CUFAST_JPEG_QUALITY="0.95")
        _, small = self.run(tmp_path, env=low, name="low.jpg")
        _, large = self.run(tmp_path, env=high, name="high.jpg")
        assert small.stat().st_size < large.stat().st_size

    def test_a_bad_setting_falls_back_instead_of_losing_the_turn(self, tmp_path):
        # The server refuses to start on a bad value, which is right for a server.
        # A hook that did the same would drop the whole turn's context over a typo.
        import os

        proc, out = self.run(tmp_path, env=dict(os.environ, CUFAST_JPEG_QUALITY="high"))
        assert proc.returncode == 0
        assert out.read_bytes()[:2] == b"\xff\xd8"

    def test_it_leaves_no_partial_file_behind(self, tmp_path):
        # The image is written beside the target and moved into place, so a failure
        # cannot replace a good picture with a truncated one.
        self.run(tmp_path)
        assert list(tmp_path.glob("*.part")) == []

    def test_an_unknown_event_is_refused(self, tmp_path):
        proc, _ = self.run(tmp_path, event="Whatever")
        assert proc.returncode == 2
        assert "unknown event" in proc.stderr
