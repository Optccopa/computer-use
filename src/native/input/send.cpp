// The gate every injection passes through, and the only calls to SendInput.
#include "input/input.hpp"

#include "input/detail.hpp"

#include <atomic>
#include <vector>

namespace cufast {
using namespace detail;  // file-local before the split
namespace {

std::atomic<bool> g_blocked{false};

}  // namespace

void check_input_allowed() {
    if (g_blocked.load(std::memory_order_relaxed)) {
        throw Error("STOPPED BY THE USER. They pressed the kill switch (Ctrl+Esc), which "
                    "blocks all mouse and keyboard input. Stop what you were doing, do not "
                    "retry, and tell them you have stopped. They release it by pressing "
                    "Ctrl+Esc again.");
    }
}

namespace detail {

void check_allowed() { check_input_allowed(); }

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

void push_mouse(std::vector<INPUT>& out, DWORD flags, int data) {
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

}  // namespace detail

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

void get_cursor_pos(int* x, int* y) {
    POINT p{};
    win_check(GetCursorPos(&p) != 0, "GetCursorPos");
    *x = p.x;
    *y = p.y;
}

}  // namespace cufast
