#include "hotkey.h"

#include <atomic>
#include <future>
#include <mutex>
#include <string>
#include <thread>

#include "input.h"

namespace cufast {
namespace {

std::mutex g_lifecycle;
std::thread g_thread;
std::atomic<DWORD> g_thread_id{0};
std::atomic<uint64_t> g_trips{0};

// Set when a Ctrl+Esc key-down is swallowed, so the matching key-up is swallowed
// too. Without it an application sees a release for a press it never saw, which
// some dialogs treat as a dismiss.
std::atomic<bool> g_swallow_next_up{false};

// Asks the hook thread to run the release outside the hook procedure.
constexpr UINT WM_CUFAST_ENGAGED = WM_APP + 1;

// Flips the switch and, on engage, asks the pump to unstick the desktop. Kept out
// of the hook procedure so a test can reach it: the hook deliberately ignores
// injected keystrokes, so SendInput cannot drive it, and the only other way to
// exercise it would be a real Ctrl+Esc on the developer's desktop.
void toggle_and_notify() {
    const bool engaging = !input_blocked();
    set_input_blocked(engaging);
    if (!engaging) return;
    g_trips.fetch_add(1, std::memory_order_relaxed);
    const DWORD tid = g_thread_id.load(std::memory_order_relaxed);
    // Not released here: this runs on the input path of every keystroke on the
    // desktop, and Windows silently uninstalls a hook that overruns
    // LowLevelHooksTimeout (250 ms by default).
    if (tid != 0) PostThreadMessageW(tid, WM_CUFAST_ENGAGED, 0, 0);
    else release_held_input();  // no pump running, so do it inline
}

LRESULT CALLBACK ll_keyboard(int code, WPARAM wparam, LPARAM lparam) {
    if (code != HC_ACTION) return CallNextHookEx(nullptr, code, wparam, lparam);

    const auto* ev = reinterpret_cast<const KBDLLHOOKSTRUCT*>(lparam);
    const bool down = (wparam == WM_KEYDOWN || wparam == WM_SYSKEYDOWN);

    if (ev->vkCode == VK_ESCAPE) {
        // Injected events are the harness's own keystrokes coming back around the
        // input path. Acting on them would let the agent trip its own kill switch
        // simply by being asked to press ctrl+esc, and would make the stop button
        // something the thing being stopped can press.
        const bool injected = (ev->flags & LLKHF_INJECTED) != 0;
        if (!injected && down && (GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0) {
            toggle_and_notify();
            g_swallow_next_up.store(true, std::memory_order_relaxed);
            return 1;
        }
        if (!injected && !down && g_swallow_next_up.exchange(false, std::memory_order_relaxed)) {
            return 1;
        }
    }
    return CallNextHookEx(nullptr, code, wparam, lparam);
}

void hook_thread_main(std::promise<std::string> ready) {
    // Force the message queue into existence before anyone is told the thread is
    // up, or a PostThreadMessage racing this startup is dropped on the floor.
    MSG msg;
    PeekMessageW(&msg, nullptr, WM_USER, WM_USER, PM_NOREMOVE);
    g_thread_id.store(GetCurrentThreadId(), std::memory_order_relaxed);

    const HHOOK hook = SetWindowsHookExW(WH_KEYBOARD_LL, ll_keyboard, GetModuleHandleW(nullptr), 0);
    if (hook == nullptr) {
        ready.set_value("SetWindowsHookExW(WH_KEYBOARD_LL) failed (GetLastError " +
                        std::to_string(GetLastError()) + ")");
        g_thread_id.store(0, std::memory_order_relaxed);
        return;
    }
    ready.set_value({});

    while (true) {
        const BOOL got = GetMessageW(&msg, nullptr, 0, 0);
        if (got == 0 || got == -1) break;  // WM_QUIT, or the queue died with us
        if (msg.message == WM_CUFAST_ENGAGED) {
            // The point of the stop button is that the desktop is usable afterwards.
            // If the agent was mid-drag or holding a modifier when it was pressed,
            // leaving those latched hands back a machine that cannot be driven.
            release_held_input();
            continue;
        }
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    UnhookWindowsHookEx(hook);
    g_thread_id.store(0, std::memory_order_relaxed);
}

}  // namespace

void trip_kill_switch_for_test() { toggle_and_notify(); }

void start_kill_switch() {
    std::lock_guard<std::mutex> lock(g_lifecycle);
    if (g_thread.joinable()) return;

    std::promise<std::string> ready;
    auto future = ready.get_future();
    g_thread = std::thread(hook_thread_main, std::move(ready));

    const std::string error = future.get();
    if (!error.empty()) {
        g_thread.join();
        g_thread = {};
        throw Error("kill switch could not be installed: " + error);
    }
}

void stop_kill_switch() {
    std::lock_guard<std::mutex> lock(g_lifecycle);
    if (!g_thread.joinable()) return;
    const DWORD tid = g_thread_id.load(std::memory_order_relaxed);
    if (tid != 0) PostThreadMessageW(tid, WM_QUIT, 0, 0);
    g_thread.join();
    g_thread = {};
}

bool kill_switch_running() { return g_thread_id.load(std::memory_order_relaxed) != 0; }

uint64_t kill_switch_trips() { return g_trips.load(std::memory_order_relaxed); }

}  // namespace cufast
