#pragma once

// Internals shared between the input translation units. Not part of the public
// surface in input.hpp: these exist because splitting one 850-line file into four
// meant the pieces that were in an anonymous namespace now have to be visible to
// each other. Nothing outside src/native/input should include this.

#include <string>
#include <utility>
#include <vector>

#include "core/common.hpp"

namespace cufast::detail {

// One key name resolved against the active layout, with whatever modifiers the
// layout needs to reach it. A character reached through AltGr reports Ctrl and Alt
// together, which is why this carries three flags and not just shift.
struct KeyStroke {
    WORD vk = 0;
    bool shift = false;
    bool ctrl = false;
    bool alt = false;
};

std::string lower(std::string s);
std::wstring utf8_to_utf16(const std::string& utf8);
bool is_extended(WORD vk);

KeyStroke resolve_key(const std::string& raw);
std::vector<std::string> split_chord(const std::string& chord);
std::vector<WORD> chord_vks(const std::string& chord);
std::vector<WORD> parse_modifiers(const std::string& modifiers);

// Throws when the kill switch is engaged. Every injecting entry point calls this
// first; the release paths deliberately do not.
void check_allowed();

void push_key(std::vector<INPUT>& out, WORD vk, bool key_up);
void push_unicode(std::vector<INPUT>& out, wchar_t unit, bool key_up);
void push_mouse(std::vector<INPUT>& out, DWORD flags, int data = 0);

// Sends and throws on a short write. SendInput is atomic with respect to other
// input within one call, so one call per action means real user input can never
// interleave inside an action.
void send(std::vector<INPUT>& batch);

// Sends without consulting the kill switch and without throwing. Only ever used to
// release things that are already held down.
void send_release(std::vector<INPUT>& batch) noexcept;

// Records everything a call presses down and guarantees the matching releases,
// including when an exception unwinds partway through.
//
// This exists because the kill switch used to cause the exact failure it is meant to
// prevent: engaging it mid-drag made the next mouse_move throw, the stack unwound
// past the button release, and the user was left with the left button physically
// held down and no way to release it -- every recovery path was itself blocked.
// Releases therefore bypass the kill switch. One extra injected event is strictly
// better than a latched modifier on a real desktop.
class PressGuard {
public:
    PressGuard() = default;
    PressGuard(const PressGuard&) = delete;
    PressGuard& operator=(const PressGuard&) = delete;

    void key_down(std::vector<INPUT>& batch, WORD vk) {
        push_key(batch, vk, false);
        keys_.push_back(vk);
    }

    void mouse_down(std::vector<INPUT>& batch, DWORD down_flag, DWORD up_flag) {
        push_mouse(batch, down_flag);
        buttons_.push_back(up_flag);
    }

    // Queues the releases in reverse order. Deliberately keeps tracking them:
    // send() clears the batch and throws when SendInput accepts only a prefix, and
    // at that point some of these releases were delivered and some were not.
    // Clearing here disarmed the destructor exactly when it was needed -- a drag
    // whose release was partially accepted left the left button physically down,
    // which is the failure this class exists to prevent. Callers disarm() only once
    // send() has returned.
    void release_into(std::vector<INPUT>& batch) {
        for (auto it = buttons_.rbegin(); it != buttons_.rend(); ++it) push_mouse(batch, *it);
        for (auto it = keys_.rbegin(); it != keys_.rend(); ++it) push_key(batch, *it, true);
    }

    // The releases are known delivered, so the destructor has nothing left to do.
    void disarm() {
        buttons_.clear();
        keys_.clear();
    }

    ~PressGuard() {
        if (keys_.empty() && buttons_.empty()) return;
        std::vector<INPUT> batch;
        release_into(batch);
        send_release(batch);
    }

private:
    std::vector<WORD> keys_;
    std::vector<DWORD> buttons_;
};

}  // namespace cufast::detail
