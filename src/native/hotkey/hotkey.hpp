#pragma once

#include <cstdint>

#include "core/common.hpp"

namespace cufast {

// The physical stop button: a low-level keyboard hook watching for Ctrl+Esc.
//
// This lives in C++ rather than Python for two reasons that are not about speed.
// A WH_KEYBOARD_LL hook only delivers events to the thread that installed it, and
// only while that thread pumps messages, so it needs a dedicated OS thread that is
// never the one running Python; and the hook procedure runs on the input path of
// every keystroke on the desktop, where exceeding LowLevelHooksTimeout gets the
// hook silently uninstalled by Windows. A callback that has to acquire the GIL is
// exactly the thing that blows that budget.
//
// Idempotent: calling start twice leaves one hook installed.
void start_kill_switch();
void stop_kill_switch();
bool kill_switch_running();

// Counts how many times the user has engaged it. The caller compares this against
// the value it saw last to notice a trip that has since been released, which a
// plain "is it blocked now" flag cannot express.
uint64_t kill_switch_trips();

// Runs exactly what a real Ctrl+Esc runs, minus the hook. Exists because the hook
// ignores injected keystrokes on purpose -- that is what stops the agent pressing
// its own stop button -- which also means no test can drive it through SendInput.
void trip_kill_switch_for_test();

// Runs the hook's decision for one Escape event without a hook installed, so the
// auto-repeat guard can be tested. Returns true when the event is swallowed.
bool hook_key_event_for_test(bool down, bool injected, bool ctrl_down);

}  // namespace cufast
