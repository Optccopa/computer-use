// Typing, chords, and the registry of keys held across calls.
#include "input/input.hpp"

#include "input/detail.hpp"

#include <algorithm>
#include <chrono>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

namespace cufast {
using namespace detail;  // file-local before the split
namespace {

using HeldEntry = std::pair<std::string, std::vector<WORD>>;

// Records a press. Matched on the RESOLVED keys, not on the chord text: the keymap
// has aliases, so "esc" and "escape" -- or "ctrl" and "control" -- are one physical
// key under two spellings. Keyed by text they became two registry entries, and
// releasing either left the other listed as held forever, which matters because
// held_keys() is what the model is told it is holding.
void registry_press(std::vector<HeldEntry>& held, const std::string& chord,
                    const std::vector<WORD>& vks) {
    auto it = std::find_if(held.begin(), held.end(),
                           [&vks](const HeldEntry& e) { return e.second == vks; });
    if (it == held.end()) held.emplace_back(chord, vks);
}

// Removes the entry holding these keys and reports what to release, or an empty
// vector when this process was not holding them. One erase rather than a sweep,
// because registry_press already refuses to add a second entry for the same keys --
// a sweep here was defensive code no test could distinguish from this, which is the
// same thing as untested code.
std::vector<WORD> registry_release(std::vector<HeldEntry>& held,
                                   const std::vector<WORD>& resolved) {
    auto it = std::find_if(held.begin(), held.end(),
                           [&resolved](const HeldEntry& e) { return e.second == resolved; });
    if (it == held.end()) return {};
    std::vector<WORD> vks = it->second;
    held.erase(it);
    return vks;
}

}  // namespace

// Keys held by key_down and not yet released, in press order, labelled by the
// chord that pressed them. Tracked because these outlive the call that pressed
// them: with nothing recording them, a crashed or stopped agent leaves W held
// and the user walks into a wall until they think to tap it themselves.
static std::mutex g_held_mutex;
static std::vector<HeldEntry> g_held;

void release_held_input() noexcept {
    // Left and right control (etc.) rather than the generic VK_CONTROL: a generic
    // up does not clear a specifically-held right modifier, and the modifier that
    // stays latched is the one that makes every later keystroke a shortcut.
    static constexpr struct {
        int vk;
        WORD release;
    } kKeys[] = {
        {VK_LCONTROL, VK_LCONTROL}, {VK_RCONTROL, VK_RCONTROL},
        {VK_LSHIFT, VK_LSHIFT},     {VK_RSHIFT, VK_RSHIFT},
        {VK_LMENU, VK_LMENU},       {VK_RMENU, VK_RMENU},
        {VK_LWIN, VK_LWIN},         {VK_RWIN, VK_RWIN},
    };
    static constexpr struct {
        int vk;
        DWORD up;
    } kButtons[] = {
        {VK_LBUTTON, MOUSEEVENTF_LEFTUP},
        {VK_RBUTTON, MOUSEEVENTF_RIGHTUP},
        {VK_MBUTTON, MOUSEEVENTF_MIDDLEUP},
    };

    // Held for the whole call, the send included, so a key_down cannot slip its
    // injection in after the registry was drained.
    std::lock_guard<std::mutex> lock(g_held_mutex);

    std::vector<INPUT> batch;
    // Buttons first: a drag that ends with the modifier already gone is a plain
    // drag, whereas releasing the modifier last can turn it into a shift-drag.
    for (const auto& b : kButtons) {
        if (GetAsyncKeyState(b.vk) & 0x8000) push_mouse(batch, b.up);
    }

    // Then everything key_down is holding. These are not modifiers and so are not
    // in the table above -- a latched W is what walks the player into a wall.
    for (auto entry = g_held.rbegin(); entry != g_held.rend(); ++entry) {
        for (auto vk = entry->second.rbegin(); vk != entry->second.rend(); ++vk) {
            push_key(batch, *vk, true);
        }
    }
    g_held.clear();

    for (const auto& k : kKeys) {
        if (GetAsyncKeyState(k.vk) & 0x8000) push_key(batch, k.release, true);
    }
    send_release(batch);
}

void type_text(const std::string& utf8) {
    check_allowed();
    const std::wstring wide = utf8_to_utf16(utf8);

    std::vector<INPUT> batch;
    batch.reserve(1024);
    for (size_t i = 0; i < wide.size(); ++i) {
        const wchar_t c = wide[i];
        if (c == L'\r') {
            // Collapse CRLF so a pasted block does not produce doubled newlines.
            if (i + 1 < wide.size() && wide[i + 1] == L'\n') continue;
            push_key(batch, VK_RETURN, false);
            push_key(batch, VK_RETURN, true);
        } else if (c == L'\n') {
            // A literal U+000A does nothing in most edit controls; Return is meant.
            push_key(batch, VK_RETURN, false);
            push_key(batch, VK_RETURN, true);
        } else if (c == L'\t') {
            push_key(batch, VK_TAB, false);
            push_key(batch, VK_TAB, true);
        } else {
            push_unicode(batch, c, false);
            push_unicode(batch, c, true);
        }

        // Flush in chunks, but never between the halves of a surrogate pair: the two
        // units must reach the target in one call or it receives a lone surrogate.
        const bool next_is_low_surrogate =
            i + 1 < wide.size() && wide[i + 1] >= 0xDC00 && wide[i + 1] <= 0xDFFF;
        if (batch.size() >= 512 && !next_is_low_surrogate) {
            // Re-check between chunks so the kill switch can stop a long string
            // partway instead of being consulted once and then ignored for minutes.
            check_allowed();
            send(batch);
        }
    }
    send(batch);
}

void press_key(const std::string& chord, int repeat) {
    check_allowed();
    if (repeat < 1) repeat = 1;
    if (repeat > 100) throw Error("repeat must be between 1 and 100");

    const auto parts = split_chord(chord);
    std::vector<WORD> mods;
    for (size_t i = 0; i + 1 < parts.size(); ++i) mods.push_back(resolve_key(parts[i]).vk);
    const KeyStroke key = resolve_key(parts.back());

    // A character the layout reaches through Shift or AltGr needs those held too.
    auto already = [&mods](WORD vk) {
        return std::find(mods.begin(), mods.end(), vk) != mods.end();
    };
    std::vector<WORD> implicit;
    if (key.shift && !already(VK_SHIFT)) implicit.push_back(VK_SHIFT);
    if (key.ctrl && !already(VK_CONTROL)) implicit.push_back(VK_CONTROL);
    if (key.alt && !already(VK_MENU)) implicit.push_back(VK_MENU);

    PressGuard guard;
    std::vector<INPUT> batch;
    for (WORD vk : mods) guard.key_down(batch, vk);
    for (WORD vk : implicit) guard.key_down(batch, vk);

    for (int i = 0; i < repeat; ++i) {
        push_key(batch, key.vk, false);
        push_key(batch, key.vk, true);
    }

    guard.release_into(batch);
    send(batch);
    guard.disarm();
}

void key_down(const std::string& chord) {
    check_allowed();
    const auto vks = chord_vks(chord);

    // Recorded before the send, not after. If SendInput accepts a prefix and then
    // fails, those keys are down; a registry written afterwards would not know
    // about them and nothing would ever release them. Recording a key that never
    // went down is harmless -- releasing an unpressed key is a no-op.
    // Registry write and injection under one lock, and release_held_input takes the
    // same lock across its own send. Otherwise the kill switch could fire between
    // the two: the releaser drains the registry and injects W-up, then this call
    // injects W-down into a desktop whose stop button has already fired, and nothing
    // is left that would ever release it.
    std::lock_guard<std::mutex> lock(g_held_mutex);
    registry_press(g_held, chord, vks);

    std::vector<INPUT> batch;
    for (WORD vk : vks) push_key(batch, vk, false);
    send(batch);
}

void key_up(const std::string& chord) {
    // Deliberately not gated on the kill switch, for the same reason mouse_up is
    // not: the recovery path must never be the thing that is blocked.
    // Resolved before the lock: chord_vks calls into the keyboard layout, which is
    // not something to do while holding a mutex the hook path also wants.
    const std::vector<WORD> resolved = chord_vks(chord);

    std::lock_guard<std::mutex> lock(g_held_mutex);
    std::vector<WORD> vks = registry_release(g_held, resolved);
    // Falling back to the resolved chord covers a release for something this process
    // did not press -- a key left down by a previous run, say.
    if (vks.empty()) vks = resolved;

    std::vector<INPUT> batch;
    for (auto vk = vks.rbegin(); vk != vks.rend(); ++vk) push_key(batch, *vk, true);
    send_release(batch);
}

std::vector<std::string> held_registry_for_test(const std::vector<std::string>& down,
                                                const std::string& up) {
    std::vector<HeldEntry> held;
    for (const auto& chord : down) registry_press(held, chord, chord_vks(chord));
    if (!up.empty()) registry_release(held, chord_vks(up));
    std::vector<std::string> out;
    out.reserve(held.size());
    for (const auto& entry : held) out.push_back(entry.first);
    return out;
}

std::vector<std::string> held_keys() {
    std::lock_guard<std::mutex> lock(g_held_mutex);
    std::vector<std::string> out;
    out.reserve(g_held.size());
    for (const auto& entry : g_held) out.push_back(entry.first);
    return out;
}

void hold_key(const std::string& chord, double seconds) {
    check_allowed();
    if (seconds < 0.0 || seconds > 300.0) throw Error("duration must be between 0 and 300 seconds");

    const auto parts = split_chord(chord);
    std::vector<WORD> keys;
    for (const auto& part : parts) {
        const KeyStroke stroke = resolve_key(part);
        // Match press_key: a character reached through Shift or AltGr needs them held.
        if (stroke.shift) keys.push_back(VK_SHIFT);
        if (stroke.ctrl) keys.push_back(VK_CONTROL);
        if (stroke.alt) keys.push_back(VK_MENU);
        keys.push_back(stroke.vk);
    }

    PressGuard guard;
    std::vector<INPUT> batch;
    for (WORD vk : keys) guard.key_down(batch, vk);
    send(batch);

    // Sleep in slices so the kill switch can cut a long hold short. A five minute
    // hold that ignores the switch would be the worst possible thing to be holding.
    using Clock = std::chrono::steady_clock;
    const auto total = std::chrono::duration_cast<Clock::duration>(
        std::chrono::duration<double>(seconds));
    const Clock::time_point deadline = Clock::now() + total;
    const Clock::duration slice =
        std::chrono::duration_cast<Clock::duration>(std::chrono::milliseconds(20));

    while (true) {
        const Clock::time_point now = Clock::now();
        if (now >= deadline || input_blocked()) break;
        const Clock::duration remaining = deadline - now;
        std::this_thread::sleep_for(remaining < slice ? remaining : slice);
    }

    // Released by the guard's destructor, which bypasses the kill switch.
}

}  // namespace cufast
