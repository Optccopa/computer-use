#include "hotkey.hpp"

#include <atomic>
#include <condition_variable>
#include <future>
#include <mutex>
#include <string>
#include <thread>

#include "input.hpp"

namespace cufast {
namespace {

std::mutex g_lifecycle;
// Heap-allocated and never destroyed. A namespace-scope std::thread that is still
// joinable at static destruction calls std::terminate, so an embedder that armed the
// switch and never stopped it would abort on the way out. Joining from a static
// destructor is not an alternative: that runs under the loader lock.
std::thread* g_thread = nullptr;
std::atomic<DWORD> g_thread_id{0};

// Releasing held input means calling SendInput, and every event it injects has to be
// dispatched through our own low-level hook -- which only happens while the hook's
// thread is pumping. Doing it on that thread blocks it inside the call that generates
// the events it must handle, the raw input thread waits out LowLevelHooksTimeout, and
// Windows silently uninstalls the hook. So it gets its own thread.
std::thread* g_releaser = nullptr;
std::mutex g_release_mutex;
std::condition_variable g_release_cv;
bool g_release_wanted = false;
bool g_release_quit = false;
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
    // A failed post -- thread queue full, or the pump already exiting -- would engage
    // the switch without unlatching anything, so fall back rather than drop it.
    if (tid == 0 || !PostThreadMessageW(tid, WM_CUFAST_ENGAGED, 0, 0)) {
        std::lock_guard<std::mutex> lock(g_release_mutex);
        g_release_wanted = true;
        g_release_cv.notify_one();
    }
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
            // Hardware auto-repeat arrives as ordinary key-downs and KBDLLHOOKSTRUCT
            // carries no repeat count, so a held chord is indistinguishable from a
            // burst of fresh presses. Leaning on it for a second delivers about
            // sixteen, and toggling on each made whether the agent ended up stopped
            // the parity of how long the user held the key -- a coin flip on the one
            // control that exists to stop it. The first down of a press wins; the
            // rest are swallowed until the matching up clears the latch.
            if (!g_swallow_next_up.exchange(true, std::memory_order_relaxed)) {
                toggle_and_notify();
            }
            return 1;
        }
        if (!injected && !down && g_swallow_next_up.exchange(false, std::memory_order_relaxed)) {
            return 1;
        }
    }
    return CallNextHookEx(nullptr, code, wparam, lparam);
}

void releaser_main() {
    while (true) {
        bool work = false;
        {
            std::unique_lock<std::mutex> lock(g_release_mutex);
            g_release_cv.wait(lock, [] { return g_release_wanted || g_release_quit; });
            if (g_release_quit && !g_release_wanted) return;
            work = g_release_wanted;
            g_release_wanted = false;
        }
        if (work) release_held_input();
    }
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
            // Handed to the releaser thread rather than done here -- see above.
            {
                std::lock_guard<std::mutex> lock(g_release_mutex);
                g_release_wanted = true;
            }
            g_release_cv.notify_one();
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
    if (g_thread != nullptr) return;

    {
        std::lock_guard<std::mutex> rlock(g_release_mutex);
        g_release_quit = false;
        g_release_wanted = false;
    }
    g_releaser = new std::thread(releaser_main);

    std::promise<std::string> ready;
    auto future = ready.get_future();
    g_thread = new std::thread(hook_thread_main, std::move(ready));

    const std::string error = future.get();
    if (!error.empty()) {
        g_thread->join();
        delete g_thread;
        g_thread = nullptr;
        {
            std::lock_guard<std::mutex> rlock(g_release_mutex);
            g_release_quit = true;
        }
        g_release_cv.notify_one();
        g_releaser->join();
        delete g_releaser;
        g_releaser = nullptr;
        throw Error("kill switch could not be installed: " + error);
    }
}

void stop_kill_switch() {
    std::lock_guard<std::mutex> lock(g_lifecycle);
    if (g_thread == nullptr) return;
    const DWORD tid = g_thread_id.load(std::memory_order_relaxed);
    if (tid != 0) PostThreadMessageW(tid, WM_QUIT, 0, 0);
    g_thread->join();
    delete g_thread;
    g_thread = nullptr;

    {
        std::lock_guard<std::mutex> rlock(g_release_mutex);
        g_release_quit = true;
    }
    g_release_cv.notify_one();
    g_releaser->join();
    delete g_releaser;
    g_releaser = nullptr;
}

bool kill_switch_running() { return g_thread_id.load(std::memory_order_relaxed) != 0; }

uint64_t kill_switch_trips() { return g_trips.load(std::memory_order_relaxed); }

}  // namespace cufast
