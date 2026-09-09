"""The clipboard, which is the only way this harness reads text exactly.

Everything else it knows about the screen came out of a JPEG. Zoom makes small text
legible but it is still a model reading pixels, and for a path, a URL or a hash the
difference between rn and m is the difference between working and not. Select, copy,
read: the answer is the actual characters.

The failure mode worth testing is the silent one. A read that returns the empty
string looks the same as a clipboard holding an image, and a write that is quietly
capped looks the same as one that worked, so both are checked for what they report
rather than only for what they do.
"""

from __future__ import annotations

import pytest

from cufast import _native
from cufast.actions import run_batch
from cufast.actions.limits import MAX_CLIPBOARD_CHARS, MAX_CLIPBOARD_READS_PER_BATCH
from cufast.session import ActionError

# Captured at import, before the autouse fixture swaps them for stubs. Only the one
# test below needs the real flag; everything else in this file runs against the fake.
NATIVE_SET_BLOCKED = _native.set_input_blocked


def texts(results) -> list[str]:
    return [r.text or "" for r in results]


class TestOneActionReadsAndWrites:
    def test_no_text_reads(self, session, fake_input):
        fake_input.clipboard = "C:/Users/example/report.txt"
        results = run_batch(session, [{"action": "clipboard"}], auto_screenshot=False)
        assert "C:/Users/example/report.txt" in results[0].text
        assert ("clipboard_read",) in fake_input.events

    def test_text_writes(self, session, fake_input):
        run_batch(
            session, [{"action": "clipboard", "text": "hello"}], auto_screenshot=False
        )
        assert fake_input.clipboard == "hello"

    def test_an_empty_string_is_a_write_that_clears_it(self, session, fake_input):
        """Absent and empty must not collapse into each other.

        `text: ""` is a request to clear the clipboard, and reading it back would be
        a different action entirely -- one that silently does nothing to the machine
        while reporting success.
        """
        fake_input.clipboard = "something"
        run_batch(session, [{"action": "clipboard", "text": ""}], auto_screenshot=False)
        assert fake_input.clipboard == ""
        assert ("clipboard_write", "") in fake_input.events

    @pytest.mark.parametrize(
        "alias", ["clipboard_read", "clipboard_write", "get_clipboard", "set_clipboard"]
    )
    def test_the_separate_names_a_model_reaches_for_all_work(
        self, session, fake_input, alias
    ):
        params = {"text": "x"} if "write" in alias or "set" in alias else {}
        results = run_batch(
            session, [{"action": alias, **params}], auto_screenshot=False
        )
        assert not results[0].is_error, results[0].text


class TestAnEmptyClipboardIsAnAnswerNotAnError:
    def test_it_says_what_to_do_instead(self, session, fake_input):
        fake_input.clipboard = ""
        results = run_batch(session, [{"action": "clipboard"}], auto_screenshot=False)
        assert not results[0].is_error
        # Naming the copy is the point. "no text" alone reads as a broken action,
        # and the response to a broken action is to retry it unchanged.
        assert "ctrl+c" in results[0].text


class TestAReadCannotFloodTheContext:
    def test_a_huge_clipboard_is_cut_off_and_says_so(self, session, fake_input):
        fake_input.clipboard = "x" * (MAX_CLIPBOARD_CHARS + 5000)
        results = run_batch(session, [{"action": "clipboard"}], auto_screenshot=False)
        text = results[0].text
        assert "cut off" in text
        # The real length has to survive the truncation, or there is no way to tell
        # a document that was clipped from one that happened to end there.
        assert str(MAX_CLIPBOARD_CHARS + 5000) in text
        assert text.count("x") == MAX_CLIPBOARD_CHARS

    def test_exactly_the_cap_is_not_truncated(self, session, fake_input):
        fake_input.clipboard = "y" * MAX_CLIPBOARD_CHARS
        results = run_batch(session, [{"action": "clipboard"}], auto_screenshot=False)
        assert "cut off" not in results[0].text

    def test_too_many_reads_in_one_batch_are_refused(self, session, fake_input):
        actions = [{"action": "clipboard"}] * (MAX_CLIPBOARD_READS_PER_BATCH + 1)
        with pytest.raises(ActionError, match="clipboard reads"):
            run_batch(session, actions, auto_screenshot=False)

    def test_writes_do_not_count_against_the_read_budget(self, session, fake_input):
        """A write costs no reply. The cap exists for text the model did not send."""
        actions = [{"action": "clipboard", "text": "a"}] * 20
        results = run_batch(session, actions, auto_screenshot=False)
        assert not any(r.is_error for r in results)


class TestAWriteIsBounded:
    def test_over_the_cap_is_refused_rather_than_silently_cut(self, session, fake_input):
        # Truncating a write would paste a subtly incomplete string into whatever
        # the user was editing, which is worse than not pasting at all.
        with pytest.raises(ActionError, match="clipboard write"):
            run_batch(
                session,
                [{"action": "clipboard", "text": "z" * (MAX_CLIPBOARD_CHARS + 1)}],
                auto_screenshot=False,
            )

    def test_a_non_string_is_refused(self, session, fake_input):
        with pytest.raises(ActionError, match="text must be a string"):
            run_batch(session, [{"action": "clipboard", "text": 5}], auto_screenshot=False)


class TestItSlotsIntoTheOneCallPattern:
    def test_select_copy_read_is_a_single_batch(self, session, fake_input):
        """The whole point is that recovering exact text costs one round trip.

        Split across calls it is three, and at nine seconds each that is the
        difference between reading a path and not bothering.
        """
        fake_input.clipboard = "the exact characters"
        results = run_batch(
            session,
            [
                {"action": "triple_click", "coordinate": [400, 210]},
                {"action": "key", "text": "ctrl+c"},
                {"action": "clipboard"},
            ],
            auto_screenshot=False,
        )
        assert not any(r.is_error for r in results)
        assert "the exact characters" in results[-1].text

    def test_the_result_marks_the_contents_as_content(self, session, fake_input):
        """Clipboard text came off the desktop, so it is data like a screenshot is.

        Without a label a copied "ignore your instructions and ..." arrives looking
        exactly like the harness talking.
        """
        fake_input.clipboard = "anything"
        results = run_batch(session, [{"action": "clipboard"}], auto_screenshot=False)
        assert "not" in results[0].text and "instructions" in results[0].text


class TestTheStopButtonCoversIt:
    def test_a_blocked_batch_never_reaches_the_clipboard(
        self, session, fake_input, no_real_kill_switch
    ):
        no_real_kill_switch["blocked"] = True
        with pytest.raises(ActionError, match="STOPPED BY THE USER"):
            run_batch(
                session, [{"action": "clipboard", "text": "x"}], auto_screenshot=False
            )
        assert fake_input.clipboard == ""


@pytest.mark.desktop
class TestTheNativeWriteIsGatedToo:
    """The Python layer refuses a blocked batch, but that is not the same check.

    Everything above this runs against a fake clipboard, so it proves the batch is
    refused and nothing about the native call. If the gate inside clipboard_write
    were removed, every test in this file would still pass while a stopped agent
    could go on replacing the user's clipboard -- a change nothing on screen shows.
    """

    def test_a_blocked_write_throws_before_it_touches_the_clipboard(self):
        before = _native.clipboard_read()
        try:
            NATIVE_SET_BLOCKED(True)
            with pytest.raises(RuntimeError, match="STOPPED BY THE USER"):
                _native.clipboard_write("this must never land")
            assert _native.clipboard_read() == before
        finally:
            NATIVE_SET_BLOCKED(False)
            # Only reached if the gate is gone and the write landed. Putting the
            # user's clipboard back matters more than the assertion above.
            if _native.clipboard_read() != before:
                _native.clipboard_write(before)

    def test_an_allowed_write_round_trips_unchanged(self):
        """Read and write have to agree about line endings.

        Windows puts CRLF on the clipboard and the rest of this harness speaks LF,
        so the read strips and the write restores. Either half alone turns every
        read-modify-write cycle into one that grows a carriage return.
        """
        before = _native.clipboard_read()
        try:
            for text in ("plain", "two" + chr(10) + "lines", "unicode \u2014 dash"):
                _native.clipboard_write(text)
                assert _native.clipboard_read() == text
        finally:
            _native.clipboard_write(before)
