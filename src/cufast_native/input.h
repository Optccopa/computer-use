#pragma once

#include <string>
#include <vector>

#include "common.h"

namespace cufast {

// Must run before any capture or coordinate work, or Windows reports virtualized
// (scaled) pixels on high-DPI displays and every coordinate is silently wrong.
// Safe to call repeatedly; a no-op once awareness is set.
void enable_dpi_awareness();

// The kill switch. Every injection routine checks this first and throws when set,
// so one flag disarms the whole harness.
void set_input_blocked(bool blocked);
bool input_blocked();

enum class MouseButton { Left, Right, Middle };

// All coordinates are absolute virtual-desktop pixels and may be negative when a
// display sits left of or above the primary.
void mouse_move(int x, int y);
void mouse_click(MouseButton button, int clicks, const std::string& modifiers);
void mouse_down(MouseButton button);
void mouse_up(MouseButton button);
void mouse_drag(int x0, int y0, int x1, int y1, const std::string& modifiers);
void mouse_scroll(const std::string& direction, int amount, const std::string& modifiers);
void get_cursor_pos(int* x, int* y);

// Types literal text. Uses KEYEVENTF_UNICODE so layout never matters, batching the
// whole string into as few SendInput calls as possible. Newline and tab are sent as
// real Return/Tab keystrokes, since the raw control characters do nothing in most
// applications.
void type_text(const std::string& utf8);

// Presses a key or "+"-joined chord, e.g. "Return", "ctrl+s", "alt+Tab".
// Key names follow the X11 keysym spelling the computer-use tool emits.
void press_key(const std::string& chord, int repeat);

// Holds a chord down for a duration, then releases it.
void hold_key(const std::string& chord, double seconds);

}  // namespace cufast
