"""The screen is fed to the model, so it never has to ask for it.

Two measured facts drive this. Actions take about 0.03s and the round trip that
delivers them takes nine to eighteen, so a call spent finding out what is on screen
is a call spent doing nothing -- and in real transcripts roughly 55% of them were
exactly that. And a desktop is static for most of the time an agent spends looking
at it, so most of those images were bytes the model already had.

So: every call returns the screen without being asked, and an image identical to the
last one is replaced by a line of text.
"""

from __future__ import annotations

from cufast.actions import CAPTURE_WAS_FREE, SCREEN_UNCHANGED, run_batch


class TestAnIdenticalFrameIsNotSentTwice:
    def test_the_second_look_at_a_frozen_screen_is_text(self, session, fake_screen,
                                                        fake_input):
        fake_screen.static = True
        first = run_batch(session, [{"action": "screenshot"}], False)
        second = run_batch(session, [{"action": "screenshot"}], False)
        assert first[0].image is not None
        assert second[0].image is None
        assert SCREEN_UNCHANGED in second[0].text

    def test_the_first_frame_is_always_sent(self, session, fake_screen, fake_input):
        # A model told "unchanged" before it has seen anything has been told nothing.
        fake_screen.static = True
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert results[0].image is not None

    def test_a_screen_that_moves_keeps_sending_images(self, session, fake_screen,
                                                     fake_input):
        fake_screen.static = False
        for _ in range(3):
            results = run_batch(session, [{"action": "screenshot"}], False)
            assert results[0].image is not None

    def test_it_is_not_an_error(self, session, fake_screen, fake_input):
        # "Unchanged" is information. Flagged as an error it would read as a failed
        # capture, and the response to a failed capture is to try again.
        fake_screen.static = True
        run_batch(session, [{"action": "screenshot"}], False)
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert not results[0].is_error

    def test_the_message_tells_it_not_to_look_again(self, session, fake_screen,
                                                   fake_input):
        fake_screen.static = True
        run_batch(session, [{"action": "screenshot"}], False)
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert "already have the current state" in results[0].text
        assert "rather than looking again" in results[0].text

    def test_a_zoom_is_judged_against_its_own_region(self, session, fake_screen,
                                                    fake_input):
        # A zoom and a full screenshot are different pictures. Judging them by hash
        # alone would let one suppress the other on a frozen screen.
        fake_screen.static = True
        full = run_batch(session, [{"action": "screenshot"}], False)
        zoom = run_batch(session, [{"action": "zoom", "region": [0, 0, 100, 100]}], False)
        assert full[0].image is not None
        assert zoom[0].image is not None

class TestTheAutomaticScreenshotIsTheFeed:
    def test_every_batch_ends_with_the_screen(self, session, fake_input):
        results = run_batch(session, [{"action": "cursor_position"}], True)
        assert results[-1].image is not None

    def test_a_frozen_screen_still_reports_at_the_end(self, session, fake_screen,
                                                     fake_input):
        # The feed does not go silent when nothing moves: the model is told the
        # screen is the one it already has, which is a fact it needs.
        fake_screen.static = True
        run_batch(session, [{"action": "cursor_position"}], True)
        results = run_batch(session, [{"action": "cursor_position"}], True)
        assert results[-1].image is None
        assert SCREEN_UNCHANGED in results[-1].text


class TestAskingForAScreenshotIsReportedAsFree:
    def test_an_explicit_screenshot_says_it_was_not_needed(self, session, fake_input):
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert results[0].image is not None
        assert CAPTURE_WAS_FREE.strip() in results[0].text

    def test_the_note_points_at_the_thing_to_do_instead(self, session, fake_input):
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert "put the actions you want in the call instead" in results[0].text

    def test_the_automatic_one_carries_no_such_note(self, session, fake_input):
        # It was not asked for, so there is nothing to point out.
        results = run_batch(session, [{"action": "cursor_position"}], True)
        assert results[-1].text is None

    def test_it_still_runs(self, session, fake_input):
        # Refusing would break the genuine case where the model has not seen the
        # screen yet, and would read as the tool being broken.
        results = run_batch(session, [{"action": "screenshot"}], False)
        assert not results[0].is_error
