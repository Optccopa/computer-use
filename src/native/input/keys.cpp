// Resolving key names and chords against the layout of the window that will
// actually receive the input.
#include "input/input.hpp"

#include "input/detail.hpp"

#include <algorithm>
#include <cctype>
#include <string>
#include <unordered_map>
#include <vector>

namespace cufast {
using namespace detail;  // file-local before the split
namespace {

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

}  // namespace

namespace detail {

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

std::vector<WORD> chord_vks(const std::string& chord) {
    const auto parts = split_chord(chord);
    std::vector<WORD> vks;
    for (size_t i = 0; i + 1 < parts.size(); ++i) vks.push_back(resolve_key(parts[i]).vk);
    const KeyStroke key = resolve_key(parts.back());

    auto already = [&vks](WORD vk) {
        return std::find(vks.begin(), vks.end(), vk) != vks.end();
    };
    if (key.shift && !already(VK_SHIFT)) vks.push_back(VK_SHIFT);
    if (key.ctrl && !already(VK_CONTROL)) vks.push_back(VK_CONTROL);
    if (key.alt && !already(VK_MENU)) vks.push_back(VK_MENU);
    vks.push_back(key.vk);
    return vks;
}

std::vector<WORD> parse_modifiers(const std::string& modifiers) {
    std::vector<WORD> out;
    if (modifiers.empty()) return out;
    for (const auto& part : split_chord(modifiers)) out.push_back(resolve_key(part).vk);
    return out;
}


}  // namespace detail

void validate_chord(const std::string& chord) {
    if (chord.empty()) return;
    (void)chord_vks(chord);
}

}  // namespace cufast
