#pragma once

#include <string>
#include <vector>

#include "common.h"

namespace cufast {

// Must run before any capture or coordinate work, or Windows reports virtualized
// (scaled) pixels on high-DPI displays and every coordinate is silently wrong.
// Safe to call repeatedly; a no-op once awareness is set.
void enable_dpi_awareness();

// Whether per-monitor awareness actually took effect. It does not if a manifest or
// an earlier call already set a different mode, and in that case every coordinate is
// virtualized on a scaled display -- silently wrong rather than visibly broken.
bool dpi_per_monitor_aware();

// The kill switch. Every injection routine checks this first and throws when set,
// so one flag disarms the whole harness.
void set_input_blocked(bool blocked);
bool input_blocked();

// Releases every mouse button and modifier key that is currently down, bypassing
// the kill switch. Called when the stop button is pressed: whatever the agent was
// mid-way through, the desktop it hands back has to be usable. Never throws.
void release_held_input() noexcept;

enum class MouseButton { Left, Right, Middle };

// All coordinates are absolute virtual-desktop pixels and may be negative when a
// display sits left of or above the primary.
void mouse_move(int x, int y);
void mouse_click(MouseButton button, int clicks, const std::string& modifiers);
void mouse_down(MouseButton button);
void mouse_up(MouseButton button);
void mouse_drag(int x0, int y0, int x1, int y1, const std::string& modifiers);
void mouse_scroll(const std::string& direction, int amount, const std::string& modifiers);

// Moves by a delta instead of to a position, which is the only thing that works in
// a pointer-locked application: a 3D game hides the cursor and warps it back to the
// window centre every frame, so it has no position to move to and reads deltas
// instead. Absolute moves reach such a game as MOUSE_MOVE_ABSOLUTE raw input, which
// it either ignores or unpacks wrongly -- in Minecraft with raw input on, horizontal
// moves did nothing and vertical moves changed the yaw.
//
// Relative moves also bypass Windows pointer ballistics on the raw-input path, so
// the delta the game sees is exactly the delta requested.
//
// steps splits the delta into that many separate SendInput calls a couple of
// milliseconds apart. The default of 1 is right for a game that accumulates deltas
// per frame; more steps are for games that clamp how far the view can turn in one
// frame, and for a visually smooth sweep.
void mouse_move_relative(int dx, int dy, int steps);

// The per-step deltas mouse_move_relative would send. Pure, and exposed so the
// property that matters can be tested without moving a real mouse: the steps must
// sum to exactly the requested delta, or a calibrated turn drifts off target by a
// little more every time it is issued.
std::vector<std::pair<int, int>> relative_step_plan(int dx, int dy, int steps);

// Press and release as separate calls, so a key stays down across tool calls while
// other actions run. hold_key cannot do this: it occupies the calling thread for
// the whole duration, which makes "walk forward while turning" impossible.
//
// Everything held this way is tracked, so the kill switch and shutdown can release
// it. A latched W key is the game equivalent of a latched mouse button.
void key_down(const std::string& chord);
void key_up(const std::string& chord);
std::vector<std::string> held_keys();
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
