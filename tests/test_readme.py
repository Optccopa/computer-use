"""The README's tables have to match the code.

The previous README claimed session.py, actions.py and server.py were files. They
had been directories for a while, and nothing said so -- documentation goes stale
silently, which is the same failure as a test that cannot fail. The tool
description is already checked against the dispatcher this way; this does the same
for the two tables a reader is most likely to act on.
"""

from __future__ import annotations

import pathlib
import re

from cufast.actions import ACTION_NAMES

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def section(title: str) -> str:
    """The body of one `## title` section."""
    body = README.read_text(encoding="utf-8").split(f"## {title}")[1]
    return body.split("\n## ")[0]


class TestEveryActionNameMentionedIsReal:
    def test_no_readme_action_has_been_renamed_away(self):
        """The README no longer tabulates the actions, but it names several in
        prose and in the example. A name that no longer exists reads as a working
        action and fails only when someone types it."""
        prose = README.read_text(encoding="utf-8")
        quoted = set(re.findall(r'"action": "([a-z_]+)"', prose))
        assert quoted <= set(ACTION_NAMES), quoted - set(ACTION_NAMES)


class TestTheConfigurationTable:
    def test_it_matches_the_environment_variables_config_reads(self):
        source = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src" / "cufast" / "config.py"
        ).read_text(encoding="utf-8")
        in_code = set(re.findall(r'"(CUFAST_[A-Z_]+)"', source))
        in_readme = set(re.findall(r"`(CUFAST_[A-Z_]+)`", section("Configuration")))
        assert in_code == in_readme


class TestTheInstallSectionStaysHonest:
    def test_it_warns_against_running_both_at_once(self):
        # Two servers means two harnesses and two kill switches, and only one of
        # them stops the one that is actually running.
        assert "not both" in section("Install")

    def test_it_names_the_stop_button(self):
        assert "Ctrl+Esc" in section("Install")
