#include "input.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <thread>
#include <unordered_map>

namespace cufast {
namespace {

std::atomic<bool> g_blocked{false};

void check_allowed() {
    if (g_blocked.load(std::memory_order_relaxed)) {
        throw Error("input is blocked: the kill switch is engaged");
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
            {"caps_lock", VK_CAPITAL}, {"capslock", VK_CAPITAL},
            {"num_lock", VK_NUMLOCK}, {"scroll_lock", VK_SCROLL},
            {"print", VK_SNAPSHOT}, {"print_screen", VK_SNAPSHOT}, {"printscreen", VK_SNAPSHOT},
            {"pause", VK_PAUSE}, {"menu", VK_APPS}, {"apps", VK_APPS},
            // Punctuation by keysym name; these are the US-layout OEM positions.
            {"minus", VK_OEM_MINUS}, {"equal", VK_OEM_PLUS}, {"plus", VK_OEM_PLUS},
            {"bracketleft", VK_OEM_4}, {"bracketright", VK_OEM_6},
            {"semicolon", VK_OEM_1}, {"apostrophe", VK_OEM_7}, {"grave", VK_OEM_3},
            {"backslash", VK_OEM_5}, {"comma", VK_OEM_COMMA}, {"period", VK_OEM_PERIOD},
            {"slash", VK_OEM_2},
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

struct KeyStroke {
    WORD vk = 0;
    bool needs_shift = false;
};

// Resolves one key name. Single printable characters go through VkKeyScanW so the
// user's actual keyboard layout decides the physical key, rather than assuming US.
KeyStroke resolve_key(const std::string& raw) {
    if (raw.empty()) return {VK_OEM_PLUS, true};  // a lone "+" inside a chord

    const auto& map = keymap();
    auto it = map.find(lower(raw));
    if (it != map.end()) return {it->second, false};

    const std::wstring wide = utf8_to_utf16(raw);
    if (wide.size() == 1) {
        const SHORT scan = VkKeyScanW(wide[0]);
        if (scan != -1) {
            KeyStroke ks;
            ks.vk = static_cast<WORD>(scan & 0xFF);
            ks.needs_shift = (scan & 0x100) != 0;
            return ks;
        }
    }
    throw Error("unknown key name: " + raw);
}

std::vector<std::string> split_chord(const std::string& chord) {
    std::vector<std::string> parts;
    std::string current;
    for (char c : chord) {
        if (c == '+') {
            parts.push_back(current);
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    parts.push_back(current);
    // "ctrl++" splits to {"ctrl", "", ""}: the trailing empty pair is a literal plus.
    if (parts.size() >= 2 && parts.back().empty() && parts[parts.size() - 2].empty()) {
        parts.pop_back();
    }
    if (parts.size() > 1) {
        // Drop empties introduced by stray separators, but keep a lone final empty
        // (the literal "+" case resolve_key() understands).
        std::vector<std::string> cleaned;
        for (size_t i = 0; i < parts.size(); ++i) {
            if (!parts[i].empty() || i + 1 == parts.size()) cleaned.push_back(parts[i]);
        }
        parts.swap(cleaned);
    }
    if (parts.empty()) throw Error("empty key chord");
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

void send(std::vector<INPUT>& batch) {
    if (batch.empty()) return;
    // SendInput takes the whole batch atomically with respect to other input, so
    // one call per action keeps interleaving from the real user impossible.
    const UINT sent = SendInput(static_cast<UINT>(batch.size()), batch.data(), sizeof(INPUT));
    if (sent != batch.size()) {
        // UIPI blocks injection into windows running at a higher integrity level.
        throw Error("SendInput was blocked (elevated window has focus?)");
    }
    batch.clear();
}

// Parses a modifier list like "ctrl+shift" into virtual-key codes.
std::vector<WORD> parse_modifiers(const std::string& modifiers) {
    std::vector<WORD> out;
    if (modifiers.empty()) return out;
    for (const auto& part : split_chord(modifiers)) {
        if (part.empty()) continue;
        out.push_back(resolve_key(part).vk);
    }
    return out;
}

void mouse_button_flags(MouseButton button, DWORD* down, DWORD* up) {
    switch (button) {
        case MouseButton::Left:   *down = MOUSEEVENTF_LEFTDOWN;   *up = MOUSEEVENTF_LEFTUP;   break;
        case MouseButton::Right:  *down = MOUSEEVENTF_RIGHTDOWN;  *up = MOUSEEVENTF_RIGHTUP;  break;
        case MouseButton::Middle: *down = MOUSEEVENTF_MIDDLEDOWN; *up = MOUSEEVENTF_MIDDLEUP; break;
    }
}

void push_mouse(std::vector<INPUT>& out, DWORD flags, int data = 0) {
    INPUT in{};
    in.type = INPUT_MOUSE;
    in.mi.dwFlags = flags;
    in.mi.mouseData = static_cast<DWORD>(data);
    out.push_back(in);
}

}  // namespace

void enable_dpi_awareness() {
    // Fails harmlessly if a manifest already set awareness for the process.
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
}

void set_input_blocked(bool blocked) { g_blocked.store(blocked, std::memory_order_relaxed); }
bool input_blocked() { return g_blocked.load(std::memory_order_relaxed); }

void get_cursor_pos(int* x, int* y) {
    POINT p{};
    win_check(GetCursorPos(&p) != 0, "GetCursorPos");
    *x = p.x;
    *y = p.y;
}

void mouse_move(int x, int y) {
    check_allowed();

    const int vx = GetSystemMetrics(SM_XVIRTUALSCREEN);
    const int vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
    const int vw = GetSystemMetrics(SM_CXVIRTUALSCREEN);
    const int vh = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    if (vw <= 1 || vh <= 1) throw Error("virtual desktop has no area");

    std::vector<INPUT> batch;
    INPUT in{};
    in.type = INPUT_MOUSE;
    in.mi.dx = static_cast<LONG>((static_cast<double>(x - vx) * 65535.0) / (vw - 1) + 0.5);
    in.mi.dy = static_cast<LONG>((static_cast<double>(y - vy) * 65535.0) / (vh - 1) + 0.5);
    in.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK;
    batch.push_back(in);
    send(batch);

    // The absolute range is 65536 steps across the whole virtual desktop, so on wide
    // desktops one step spans more than a pixel and the move can land one pixel off.
    // Snap exactly; the synthetic move above already delivered the WM_MOUSEMOVE that
    // hover-sensitive UI is watching for.
    POINT actual{};
    if (GetCursorPos(&actual) && (actual.x != x || actual.y != y)) {
        SetCursorPos(x, y);
    }
}

void mouse_click(MouseButton button, int clicks, const std::string& modifiers) {
    check_allowed();
    if (clicks < 1) clicks = 1;
    if (clicks > 3) throw Error("clicks must be 1, 2, or 3");

    DWORD down = 0, up = 0;
    mouse_button_flags(button, &down, &up);
    const auto mods = parse_modifiers(modifiers);

    std::vector<INPUT> batch;
    for (WORD vk : mods) push_key(batch, vk, false);
    for (int i = 0; i < clicks; ++i) {
        push_mouse(batch, down);
        push_mouse(batch, up);
    }
    for (auto it = mods.rbegin(); it != mods.rend(); ++it) push_key(batch, *it, true);
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
    check_allowed();
    DWORD down = 0, up = 0;
    mouse_button_flags(button, &down, &up);
    std::vector<INPUT> batch;
    push_mouse(batch, up);
    send(batch);
}

void mouse_drag(int x0, int y0, int x1, int y1, const std::string& modifiers) {
    check_allowed();
    const auto mods = parse_modifiers(modifiers);

    mouse_move(x0, y0);

    std::vector<INPUT> batch;
    for (WORD vk : mods) push_key(batch, vk, false);
    push_mouse(batch, MOUSEEVENTF_LEFTDOWN);
    send(batch);

    // Drag targets track WM_MOUSEMOVE to decide a drag started at all, so step the
    // cursor rather than teleporting it. Ten steps is enough for every drag handler
    // tested and costs ~20ms.
    constexpr int kSteps = 10;
    for (int i = 1; i <= kSteps; ++i) {
        const int x = x0 + (x1 - x0) * i / kSteps;
        const int y = y0 + (y1 - y0) * i / kSteps;
        mouse_move(x, y);
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }

    push_mouse(batch, MOUSEEVENTF_LEFTUP);
    for (auto it = mods.rbegin(); it != mods.rend(); ++it) push_key(batch, *it, true);
    send(batch);
}

void mouse_scroll(const std::string& direction, int amount, const std::string& modifiers) {
    check_allowed();
    const std::string dir = lower(direction);
    if (amount < 0) throw Error("scroll_amount must not be negative");

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
        // Windows horizontal wheel is positive-right, the opposite of the X11 sense
        // the computer-use action names come from.
        flags = MOUSEEVENTF_HWHEEL;
        delta = -WHEEL_DELTA * amount;
    } else {
        throw Error("scroll_direction must be up, down, left, or right");
    }

    const auto mods = parse_modifiers(modifiers);
    std::vector<INPUT> batch;
    for (WORD vk : mods) push_key(batch, vk, false);
    push_mouse(batch, flags, delta);
    for (auto it = mods.rbegin(); it != mods.rend(); ++it) push_key(batch, *it, true);
    send(batch);
}

void type_text(const std::string& utf8) {
    check_allowed();
    const std::wstring wide = utf8_to_utf16(utf8);

    std::vector<INPUT> batch;
    batch.reserve(256);
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
            // Surrogate pairs are sent as two units; Windows recombines them.
            push_unicode(batch, c, false);
            push_unicode(batch, c, true);
        }
        // Chunk so one enormous string cannot exceed what SendInput will take.
        if (batch.size() >= 512) send(batch);
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

    std::vector<INPUT> batch;
    for (WORD vk : mods) push_key(batch, vk, false);
    // A character that needs Shift on this layout (e.g. "?") gets it implicitly.
    const bool add_shift =
        key.needs_shift && std::find(mods.begin(), mods.end(), WORD{VK_SHIFT}) == mods.end();
    if (add_shift) push_key(batch, VK_SHIFT, false);

    for (int i = 0; i < repeat; ++i) {
        push_key(batch, key.vk, false);
        push_key(batch, key.vk, true);
        if (batch.size() >= 512) send(batch);
    }

    if (add_shift) push_key(batch, VK_SHIFT, true);
    for (auto it = mods.rbegin(); it != mods.rend(); ++it) push_key(batch, *it, true);
    send(batch);
}

void hold_key(const std::string& chord, double seconds) {
    check_allowed();
    if (seconds < 0.0 || seconds > 300.0) throw Error("duration must be between 0 and 300 seconds");

    const auto parts = split_chord(chord);
    std::vector<WORD> keys;
    for (const auto& part : parts) keys.push_back(resolve_key(part).vk);

    std::vector<INPUT> batch;
    for (WORD vk : keys) push_key(batch, vk, false);
    send(batch);

    std::this_thread::sleep_for(std::chrono::duration<double>(seconds));

    for (auto it = keys.rbegin(); it != keys.rend(); ++it) push_key(batch, *it, true);
    // Release even if the kill switch flipped mid-hold: leaving a modifier latched
    // down would be worse than one more injected event.
    if (!batch.empty()) {
        SendInput(static_cast<UINT>(batch.size()), batch.data(), sizeof(INPUT));
        batch.clear();
    }
}

}  // namespace cufast
