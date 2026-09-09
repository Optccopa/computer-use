"""The model has to be able to find a thing that is on the other monitor.

Switching displays already worked. What did not was knowing to. A model hunting for
a window searches the screen it can see, does not find it, and concludes it is not
there -- it has no reason to suspect a second display exists unless something tells
it. So every screenshot says which display it is and how many are attached, and the
guidance says to look before reporting anything missing.
"""

from __future__ import annotations

import pytest

from cufast.actions import run_batch


@pytest.fixture
def two_displays(monkeypatch):
    from cufast import _native

    monkeypatch.setattr(
        _native,
        "list_displays",
        lambda: [
            {"index": 0, "primary": True, "device": r"\\.\DISPLAY1",
             "width": 1920, "height": 1080, "x": 0, "y": 0},
            {"index": 1, "primary": False, "device": r"\\.\DISPLAY2",
             "width": 1080, "height": 1920, "x": -1080, "y": 0},
        ],
        raising=True,
    )


@pytest.fixture
def one_display(monkeypatch):
    from cufast import _native

    monkeypatch.setattr(
        _native,
        "list_displays",
        lambda: [{"index": 0, "primary": True, "device": r"\\.\DISPLAY1",
                  "width": 1920, "height": 1080, "x": 0, "y": 0}],
        raising=True,
    )


class TestEveryScreenshotSaysWhichScreenItIs:
    def test_the_label_names_the_display_and_the_count(self, session, fake_input,
                                                       two_displays):
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert "display 0 of 2 attached" in results[0].label

    def test_the_automatic_screenshot_is_labelled_too(self, session, fake_input,
                                                      two_displays):
        results = run_batch(session, [{"action": "cursor_position"}], True)
        assert "display 0 of 2 attached" in results[-1].label

    def test_a_zoom_says_it_as_well(self, session, fake_input, two_displays):
        results = run_batch(session, [{"action": "zoom", "region": [0, 0, 100, 100]}], False)
        assert "display 0 of 2 attached" in results[0].label

    def test_an_unchanged_frame_still_says_it(self, session, fake_screen, fake_input,
                                              two_displays):
        # The suppressed frame is exactly when the model is stuck and most likely to
        # start wondering where the thing went.
        fake_screen.static = True
        run_batch(session, [{"action": "screenshot"}], False)
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert results[0].image is None
        assert "display 0 of 2 attached" in results[0].label

    def test_it_follows_the_display_being_controlled(self, session, fake_screen,
                                                     fake_input, two_displays):
        fake_screen.index = 1
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert "display 1 of 2 attached" in results[0].label


class TestOneMonitorSaysNothing:
    def test_the_label_is_left_alone(self, session, fake_input, one_display):
        # On a single-monitor machine this is noise on every image and there is no
        # other screen to go and look at.
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert results[0].label == "screenshot"

    def test_the_count_is_read_live(self, session, fake_input, one_display):
        assert session.display_count == 1


class TestTheGuidanceExists:
    def test_the_tool_description_says_to_check_the_other_screen(self):
        from cufast.server import TOOL_DESCRIPTION

        assert "CANNOT FIND SOMETHING? CHECK THE OTHER SCREEN." in TOOL_DESCRIPTION
        assert "before telling the user something is missing" in TOOL_DESCRIPTION

    def test_it_explains_that_screen_info_does_not_switch(self):
        from cufast.server import TOOL_DESCRIPTION

        assert "WITHOUT switching to it" in TOOL_DESCRIPTION

    def test_it_warns_that_coordinates_do_not_carry_across(self):
        # A coordinate from the previous display maps somewhere real on the new one,
        # so reusing it clicks a wrong thing rather than failing.
        from cufast.server import TOOL_DESCRIPTION

        assert "mean different things on different displays" in TOOL_DESCRIPTION

    def test_the_agent_prompt_says_it_too(self):
        from cufast.agent.prompt import SYSTEM_PROMPT

        assert "IF YOU CANNOT FIND SOMETHING, LOOK AT THE OTHER SCREEN." in SYSTEM_PROMPT
        assert "before reporting anything as missing" in SYSTEM_PROMPT
