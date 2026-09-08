#include "input.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <mutex>
#include <thread>
#include <unordered_map>

namespace cufast {
namespace {

std::atomic<bool> g_blocked{false};

// Keys held by key_down and not yet released, in press order, labelled by the chord
// that pressed them. Tracked because these outlive the call that pressed them: with
// nothing recording them, a crashed or stopped agent leaves W held and the user
// walks into a wall until they think to tap it themselves.
std::mutex g_held_mutex;
std::vector<std::pair<std::string, std::vector<WORD>>> g_held;

void check_allowed() {
    if (g_blocked.load(std::memory_order_relaxed)) {
        throw Error("STOPPED BY THE USER. They pressed the kill switch (Ctrl+Esc), which "
                    "blocks all mouse and keyboard input. Stop what you were doing, do not "
                    "retry, and tell them you have stopped. They release it by pressing "
                    "Ctrl+Esc again.");
    }
}

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::wstring utf8_to_utf16(const std::string& utf8) {
    if (utf8.empty()) return {};
    const int need = MultiByteToWideChar(CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()),
                                         nullptr, 0);
    if (need <= 0) throw Error("text is not valid UTF-8");
    std::wstring out(static_cast<size_t>(need), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), out.data(), need);
    return out;
}

// Keys whose scancode must carry KEYEVENTF_EXTENDEDKEY, or applications that read
// raw scancodes confuse them with their numpad twins.
bool is_extended(WORD vk) {
    switch (vk) {
        case VK_UP: case VK_DOWN: case VK_LEFT: case VK_RIGHT:
        case VK_HOME: case VK_END: case VK_PRIOR: case VK_NEXT:
        case VK_INSERT: case VK_DELETE:
        case VK_NUMLOCK: case VK_SNAPSHOT: case VK_DIVIDE:
        case VK_RCONTROL: case VK_RMENU:
        case VK_LWIN: case VK_RWIN: case VK_APPS:
            return true;
        default:
            return false;
    }
}

const std::unordered_map<std::string, WORD>& keymap() {
    static const std::unordered_map<std::string, WORD> map = [] {
        std::unordered_map<std::string, WORD> m{
            // X11 keysym spellings the computer-use tool emits, plus common aliases.
            {"return", VK_RETURN}, {"enter", VK_RETURN}, {"kp_enter", VK_RETURN},
            {"tab", VK_TAB}, {"escape", VK_ESCAPE}, {"esc", VK_ESCAPE},
            {"backspace", VK_BACK}, {"delete", VK_DELETE}, {"del", VK_DELETE},
            {"insert", VK_INSERT}, {"ins", VK_INSERT},
            {"home", VK_HOME}, {"end", VK_END},
            {"page_up", VK_PRIOR}, {"pageup", VK_PRIOR}, {"prior", VK_PRIOR},
            {"page_down", VK_NEXT}, {"pagedown", VK_NEXT}, {"next", VK_NEXT},
            {"up", VK_UP}, {"down", VK_DOWN}, {"left", VK_LEFT}, {"right", VK_RIGHT},
            {"space", VK_SPACE},
            {"ctrl", VK_CONTROL}, {"control", VK_CONTROL},
            {"alt", VK_MENU}, {"meta", VK_MENU},
            {"shift", VK_SHIFT},
            {"super", VK_LWIN}, {"win", VK_LWIN}, {"cmd", VK_LWIN}, {"windows", VK_LWIN},
            {"altgr", VK_RMENU}, {"alt_gr", VK_RMENU},
            {"caps_lock", VK_CAPITAL}, {"capslock", VK_CAPITAL},
            {"num_lock", VK_NUMLOCK}, {"scroll_lock", VK_SCROLL},
            {"print", VK_SNAPSHOT}, {"print_screen", VK_SNAPSHOT}, {"printscreen", VK_SNAPSHOT},
            {"pause", VK_PAUSE}, {"menu", VK_APPS}, {"apps", VK_APPS},
            {"kp_add", VK_ADD}, {"kp_subtract", VK_SUBTRACT}, {"kp_multiply", VK_MULTIPLY},
            {"kp_divide", VK_DIVIDE}, {"kp_decimal", VK_DECIMAL},
        };
        for (int i = 1; i <= 24; ++i) m["f" + std::to_string(i)] = static_cast<WORD>(VK_F1 + i - 1);
        for (int i = 0; i <= 9; ++i) {
            m["kp_" + std::to_string(i)] = static_cast<WORD>(VK_NUMPAD0 + i);
        }
        return m;
    }();
    return map;
}

// Punctuation keysym names resolve through the active layout rather than a table of
// US OEM codes, so "slash" is whatever key actually produces "/" for this user.
const std::unordered_map<std::string, wchar_t>& char_names() {
    static const std::unordered_map<std::string, wchar_t> m{
        {"minus", L'-'},        {"equal", L'='},       {"plus", L'+'},
        {"bracketleft", L'['},  {"bracketright", L']'}, {"semicolon", L';'},
        {"apostrophe", L'\''},  {"grave", L'`'},       {"backslash", L'\\'},
        {"comma", L','},        {"period", L'.'},      {"slash", L'/'},
        {"asterisk", L'*'},     {"percent", L'%'},     {"exclam", L'!'},
        {"question", L'?'},     {"at", L'@'},          {"numbersign", L'#'},
        {"dollar", L'$'},       {"ampersand", L'&'},   {"parenleft", L'('},
        {"parenright", L')'},   {"underscore", L'_'},  {"colon", L':'},
        {"less", L'<'},         {"greater", L'>'},     {"bar", L'|'},
        {"asciitilde", L'~'},   {"quotedbl", L'"'},    {"asciicircum", L'^'},
    };
    return m;
}

// The layout of the window that will actually receive the input. The injecting
// thread's own layout is irrelevant and is frequently different -- a user with US
// and German installed gets US here while typing into a German-language window.
HKL foreground_layout() {
    HWND foreground = GetForegroundWindow();
    const DWORD thread = foreground ? GetWindowThreadProcessId(foreground, nullptr) : 0;
    return GetKeyboardLayout(thread);  // thread 0 means the calling thread
}

struct KeyStroke {
    WORD vk = 0;
    bool shift = false;
    bool ctrl = false;
    bool alt = false;
};

// Resolves one key name. Anything that is not a named key is resolved as a character
// through the active layout, so the physical key depends on the user's keyboard
// rather than on a US-layout assumption.
KeyStroke resolve_key(const std::string& raw) {
    if (raw.empty()) throw Error("empty key name in chord");

    const std::string key = lower(raw);
    const auto& named = keymap();
    auto it = named.find(key);
    if (it != named.end()) return KeyStroke{it->second, false, false, false};

    std::wstring wide;
    auto named_char = char_names().find(key);
    if (named_char != char_names().end()) {
        wide.assign(1, named_char->second);
    } else {
        wide = utf8_to_utf16(raw);
    }

    if (wide.size() == 1) {
        const SHORT scan = VkKeyScanExW(wide[0], foreground_layout());
        if (scan != -1) {
            // High byte: bit 0 Shift, bit 1 Ctrl, bit 2 Alt. A layout that reaches a
            // character through AltGr reports 6 (Ctrl+Alt), which is why testing only
            // the Shift bit silently mistypes on German, French, Polish and Nordic
            // layouts -- "@" there would come out as a bare "q".
            const int state = (scan >> 8) & 0xFF;
            KeyStroke stroke;
            stroke.vk = static_cast<WORD>(scan & 0xFF);
            stroke.shift = (state & 1) != 0;
            stroke.ctrl = (state & 2) != 0;
            stroke.alt = (state & 4) != 0;
            return stroke;
        }
    }
    throw Error("unknown key name: " + raw);
}

std::vector<std::string> split_chord(const std::string& chord) {
    if (chord.empty()) throw Error("empty key chord");
    if (chord == "+") return {"+"};

    std::vector<std::string> parts;
    std::string current;
    for (size_t i = 0; i < chord.size(); ++i) {
        const char c = chord[i];
        if (c != '+') {
            current.push_back(c);
            continue;
        }
        // A "+" that is itself the key is written as the final segment, so "ctrl++"
        // is Ctrl plus the plus key. Any other empty segment is a malformed chord
        // rather than something to silently drop.
        if (i + 1 == chord.size()) {
            if (current.empty()) throw Error("malformed key chord: " + chord);
            parts.push_back(current);
            parts.push_back("+");
            return parts;
        }
        if (chord[i + 1] == '+' && i + 2 == chord.size()) {
            if (current.empty()) throw Error("malformed key chord: " + chord);
            parts.push_back(current);
            parts.push_back("+");
            return parts;
        }
        if (current.empty()) throw Error("malformed key chord: " + chord);
        parts.push_back(current);
        current.clear();
    }
    if (current.empty()) throw Error("malformed key chord: " + chord);
    parts.push_back(current);
    return parts;
}

void push_key(std::vector<INPUT>& out, WORD vk, bool key_up) {
    INPUT in{};
    in.type = INPUT_KEYBOARD;
    in.ki.wVk = vk;
    in.ki.wScan = static_cast<WORD>(MapVirtualKeyW(vk, MAPVK_VK_TO_VSC));
    in.ki.dwFlags = (key_up ? KEYEVENTF_KEYUP : 0) | (is_extended(vk) ? KEYEVENTF_EXTENDEDKEY : 0);
    out.push_back(in);
}

void push_unicode(std::vector<INPUT>& out, wchar_t unit, bool key_up) {
    INPUT in{};
    in.type = INPUT_KEYBOARD;
    in.ki.wScan = unit;
    in.ki.dwFlags = KEYEVENTF_UNICODE | (key_up ? KEYEVENTF_KEYUP : 0);
    out.push_back(in);
}

void push_mouse(std::vector<INPUT>& out, DWORD flags, int data = 0) {
    INPUT in{};
    in.type = INPUT_MOUSE;
    in.mi.dwFlags = flags;
    in.mi.mouseData = static_cast<DWORD>(data);
    out.push_back(in);
}

// Sends without consulting the kill switch and without throwing. Used only to
// release things that are already held down.
void send_release(std::vector<INPUT>& batch) noexcept {
    if (!batch.empty()) {
        SendInput(static_cast<UINT>(batch.size()), batch.data(), sizeof(INPUT));
        batch.clear();
    }
}

void send(std::vector<INPUT>& batch) {
    if (batch.empty()) return;
    // SendInput is atomic with respect to other input within a single call, so one
    // call per action means real user input can never interleave inside an action.
    const UINT sent = SendInput(static_cast<UINT>(batch.size()), batch.data(), sizeof(INPUT));
    const bool short_write = sent != batch.size();
    batch.clear();
    if (short_write) {
        // A short return means a prefix WAS delivered, so the caller's PressGuard has
        // to run. This is not the UIPI case: when UIPI blocks injection into a
        // higher-integrity window, SendInput reports full success and neither the
        // return value nor GetLastError indicates it, so that failure is invisible
        // here by design of the API.
        throw Error("SendInput was only partially accepted (input blocked by another "
                    "application, or the input desktop changed)");
    }
}

// Records everything this call presses down and guarantees the matching releases,
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

    // Queues the releases in reverse order and stops tracking them, for the path
    // where everything succeeded and the releases travel in the normal batch.
    void release_into(std::vector<INPUT>& batch) {
        for (auto it = buttons_.rbegin(); it != buttons_.rend(); ++it) push_mouse(batch, *it);
        for (auto it = keys_.rbegin(); it != keys_.rend(); ++it) push_key(batch, *it, true);
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

std::vector<WORD> parse_modifiers(const std::string& modifiers) {
    std::vector<WORD> out;
    if (modifiers.empty()) return out;
    for (const auto& part : split_chord(modifiers)) out.push_back(resolve_key(part).vk);
    return out;
}

void mouse_button_flags(MouseButton button, DWORD* down, DWORD* up) {
    switch (button) {
        case MouseButton::Left:   *down = MOUSEEVENTF_LEFTDOWN;   *up = MOUSEEVENTF_LEFTUP;   break;
        case MouseButton::Right:  *down = MOUSEEVENTF_RIGHTDOWN;  *up = MOUSEEVENTF_RIGHTUP;  break;
        case MouseButton::Middle: *down = MOUSEEVENTF_MIDDLEDOWN; *up = MOUSEEVENTF_MIDDLEUP; break;
    }
}

struct VirtualDesktop {
    int x, y, width, height;
};

VirtualDesktop virtual_desktop() {
    VirtualDesktop vd{
        GetSystemMetrics(SM_XVIRTUALSCREEN),
        GetSystemMetrics(SM_YVIRTUALSCREEN),
        GetSystemMetrics(SM_CXVIRTUALSCREEN),
        GetSystemMetrics(SM_CYVIRTUALSCREEN),
    };
    if (vd.width <= 1 || vd.height <= 1) throw Error("virtual desktop has no area");
    return vd;
}

}  // namespace

void enable_dpi_awareness() {
    // Fails harmlessly when a manifest or an earlier call already set awareness. It
    // matters which one won, though: if something set SYSTEM_AWARE before us, every
    // coordinate is virtualized on a scaled display, so report the outcome.
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
}

bool dpi_per_monitor_aware() {
    return AreDpiAwarenessContextsEqual(GetThreadDpiAwarenessContext(),
                                        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) != FALSE;
}

void set_input_blocked(bool blocked) { g_blocked.store(blocked, std::memory_order_relaxed); }
bool input_blocked() { return g_blocked.load(std::memory_order_relaxed); }

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

    std::vector<INPUT> batch;
    // Buttons first: a drag that ends with the modifier already gone is a plain
    // drag, whereas releasing the modifier last can turn it into a shift-drag.
    for (const auto& b : kButtons) {
        if (GetAsyncKeyState(b.vk) & 0x8000) push_mouse(batch, b.up);
    }
    for (const auto& k : kKeys) {
        if (GetAsyncKeyState(k.vk) & 0x8000) push_key(batch, k.release, true);
    }
    send_release(batch);
}

void get_cursor_pos(int* x, int* y) {
    POINT p{};
    win_check(GetCursorPos(&p) != 0, "GetCursorPos");
    *x = p.x;
    *y = p.y;
}

void mouse_move(int x, int y) {
    check_allowed();
    const VirtualDesktop vd = virtual_desktop();

    // Off-desktop coordinates would be silently clamped to an edge by Windows and
    // reported as success, so the click would land somewhere plausible but wrong.
    if (x < vd.x || x >= vd.x + vd.width || y < vd.y || y >= vd.y + vd.height) {
        char buf[192];
        std::snprintf(buf, sizeof(buf),
                      "(%d, %d) is outside the virtual desktop, which spans "
                      "(%d, %d) to (%d, %d)",
                      x, y, vd.x, vd.y, vd.x + vd.width - 1, vd.y + vd.height - 1);
        throw Error(buf);
    }

    std::vector<INPUT> batch;
    INPUT in{};
    in.type = INPUT_MOUSE;
    in.mi.dx = static_cast<LONG>(
        std::lround((static_cast<double>(x - vd.x) * 65535.0) / (vd.width - 1)));
    in.mi.dy = static_cast<LONG>(
        std::lround((static_cast<double>(y - vd.y) * 65535.0) / (vd.height - 1)));
    in.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK;
    batch.push_back(in);
    send(batch);

    // The absolute range is 65536 steps across the whole virtual desktop, so on a
    // wide desktop one step spans more than a pixel and the move lands up to a pixel
    // off. SendInput is processed asynchronously, so settle first, then correct.
    POINT actual{};
    for (int attempt = 0; attempt < 3; ++attempt) {
        if (!GetCursorPos(&actual)) break;
        if (actual.x == x && actual.y == y) return;
        if (!SetCursorPos(x, y)) {
            throw Error("SetCursorPos was refused (a higher-integrity window may have "
                        "captured the cursor)");
        }
    }
    if (GetCursorPos(&actual) && (actual.x != x || actual.y != y)) {
        char buf[160];
        std::snprintf(buf, sizeof(buf),
                      "cursor would not move to (%d, %d); it is at (%ld, %ld)", x, y,
                      actual.x, actual.y);
        throw Error(buf);
    }
}

void mouse_click(MouseButton button, int clicks, const std::string& modifiers) {
    check_allowed();
    if (clicks < 1) clicks = 1;
    if (clicks > 3) throw Error("clicks must be 1, 2, or 3");

    DWORD down = 0, up = 0;
    mouse_button_flags(button, &down, &up);
    const auto mods = parse_modifiers(modifiers);

    PressGuard guard;
    std::vector<INPUT> batch;
    for (WORD vk : mods) guard.key_down(batch, vk);
    for (int i = 0; i < clicks; ++i) {
        push_mouse(batch, down);
        push_mouse(batch, up);
    }
    guard.release_into(batch);
    send(batch);
}

void mouse_down(MouseButton button) {
    check_allowed();
    DWORD down = 0, up = 0;
    mouse_button_flags(button, &down, &up);
    std::vector<INPUT> batch;
    push_mouse(batch, down);
    send(batch);
}

void mouse_up(MouseButton button) {
    // Deliberately not gated on the kill switch: releasing a held button must always
    // be possible, or the switch can strand the desktop in a dragging state.
    DWORD down = 0, up = 0;
    mouse_button_flags(button, &down, &up);
    std::vector<INPUT> batch;
    push_mouse(batch, up);
    send_release(batch);
}

void mouse_drag(int x0, int y0, int x1, int y1, const std::string& modifiers) {
    check_allowed();
    const auto mods = parse_modifiers(modifiers);

    mouse_move(x0, y0);

    // From here the button is down, so every exit path must release it. PressGuard
    // does that even when an interpolated move throws partway through the drag.
    PressGuard guard;
    std::vector<INPUT> batch;
    for (WORD vk : mods) guard.key_down(batch, vk);
    guard.mouse_down(batch, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP);
    send(batch);

    // Drag targets watch WM_MOUSEMOVE to decide a drag began at all, so step the
    // cursor rather than teleporting it.
    constexpr int kSteps = 10;
    for (int i = 1; i <= kSteps; ++i) {
        const int x = x0 + (x1 - x0) * i / kSteps;
        const int y = y0 + (y1 - y0) * i / kSteps;
        mouse_move(x, y);
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }

    guard.release_into(batch);
    send(batch);
}

void mouse_scroll(const std::string& direction, int amount, const std::string& modifiers) {
    check_allowed();
    const std::string dir = lower(direction);
    if (amount < 0) throw Error("scroll_amount must not be negative");
    // WHEEL_DELTA * amount must stay inside int, and a wheel click count beyond this
    // is a mistake rather than an intent.
    if (amount > 1000) throw Error("scroll_amount must be at most 1000");

    DWORD flags;
    int delta;
    if (dir == "up") {
        flags = MOUSEEVENTF_WHEEL;
        delta = WHEEL_DELTA * amount;
    } else if (dir == "down") {
        flags = MOUSEEVENTF_WHEEL;
        delta = -WHEEL_DELTA * amount;
    } else if (dir == "right") {
        flags = MOUSEEVENTF_HWHEEL;
        delta = WHEEL_DELTA * amount;
    } else if (dir == "left") {
        flags = MOUSEEVENTF_HWHEEL;
        delta = -WHEEL_DELTA * amount;
    } else {
        throw Error("scroll_direction must be up, down, left, or right");
    }

    const auto mods = parse_modifiers(modifiers);
    PressGuard guard;
    std::vector<INPUT> batch;
    for (WORD vk : mods) guard.key_down(batch, vk);
    push_mouse(batch, flags, delta);
    guard.release_into(batch);
    send(batch);
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
