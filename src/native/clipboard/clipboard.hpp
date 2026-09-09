#pragma once

// The clipboard, as a way to move exact text in and out of applications.
//
// This exists because reading the screen is the harness's weakest sense. A model
// driving a desktop can only recover text by looking at pixels, so a file name, a
// URL or an error message costs a zoom and a guess, and the guess is silently wrong
// for anything ambiguous -- rn against m, l against 1, a truncated path. Select all
// and copy is a mechanism every Windows application already implements, and reading
// what lands on the clipboard turns that guess into the actual characters.
//
// The same in reverse: writing the clipboard and pasting is how long or awkward text
// gets into an application. type_text injects it keystroke by keystroke, which
// occupies the harness for the whole string and goes through the active layout;
// a paste is one keystroke and is byte-exact.

#include <string>

namespace cufast {

// The clipboard's text, as UTF-8. Empty when it holds no text at all -- an image,
// a file list, or nothing. Never throws for an empty clipboard, because "there is
// no text here" is an answer rather than a failure.
//
// Throws when the clipboard cannot be opened. That is a real and routine condition:
// only one process may hold it open at a time, so a clipboard manager or the
// application being driven can be mid-update. The open is retried briefly first.
std::string clipboard_read();

// Replaces the clipboard's contents with this text. Gated on the kill switch like
// every other action that changes the machine's state: the clipboard is the user's,
// and a stopped agent must not still be overwriting it.
void clipboard_write(const std::string& utf8);

}  // namespace cufast
