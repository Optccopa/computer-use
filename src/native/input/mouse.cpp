// Pointer movement, buttons, wheel, and the relative path a locked game reads.
#include "input/input.hpp"

#include "input/detail.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <thread>
#include <utility>
#include <vector>

namespace cufast {
using namespace detail;  // file-local before the split
namespace {

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

// Every virtual key a chord presses, in the order they must go down: named
// modifiers first, then any the layout needs to reach the character, then the key.
// Splits a delta into per-step deltas. Pure, so the property that matters -- that
// the steps sum to exactly the requested delta, however the division rounds -- can
// be tested without moving a real mouse.
std::vector<std::pair<int, int>> plan_relative_steps(int dx, int dy, int steps) {
    std::vector<std::pair<int, int>> out;
    int sent_x = 0, sent_y = 0;
    for (int i = 1; i <= steps; ++i) {
        // Derived from the running total rather than a per-step quotient, so
        // rounding error cannot accumulate across steps.
        const int want_x = static_cast<int>(std::llround(static_cast<double>(dx) * i / steps));
        const int want_y = static_cast<int>(std::llround(static_cast<double>(dy) * i / steps));
        const int step_x = want_x - sent_x;
        const int step_y = want_y - sent_y;
        sent_x = want_x;
        sent_y = want_y;
        if (step_x != 0 || step_y != 0) out.emplace_back(step_x, step_y);
    }
    return out;
}

}  // namespace

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
    guard.disarm();
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
    guard.disarm();
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
    guard.disarm();
}

void mouse_move_relative(int dx, int dy, int steps) {
    check_allowed();
    // A bound rather than a validation: the point is that no single call can send
    // the pointer somewhere unrecoverable, not that the number is meaningful.
    constexpr int kMaxDelta = 100000;
    if (dx < -kMaxDelta || dx > kMaxDelta || dy < -kMaxDelta || dy > kMaxDelta) {
        throw Error("relative move is larger than 100000 pixels on an axis");
    }
    if (steps < 1) steps = 1;
    if (steps > 1000) throw Error("steps must be between 1 and 1000");

    // Each step is its own SendInput call. Batching them into one call would have
    // the target coalesce the whole thing into a single frame's delta, which is
    // exactly what splitting was meant to avoid.
    const auto plan = plan_relative_steps(dx, dy, steps);
    for (size_t i = 0; i < plan.size(); ++i) {
        std::vector<INPUT> batch;
        INPUT in{};
        in.type = INPUT_MOUSE;
        in.mi.dx = plan[i].first;
        in.mi.dy = plan[i].second;
        // No MOUSEEVENTF_ABSOLUTE: that is the whole point. This reaches a
        // raw-input client as MOUSE_MOVE_RELATIVE, which is the only form a
        // pointer-locked game reads.
        in.mi.dwFlags = MOUSEEVENTF_MOVE;
        batch.push_back(in);
        send(batch);

        if (i + 1 < plan.size()) {
            // Roughly one frame at 240Hz. Long enough that a game polling per frame
            // sees separate deltas, short enough that a 20-step sweep still fits in
            // a fraction of the round trip that delivered it.
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            check_allowed();
        }
    }
}

std::vector<std::pair<int, int>> relative_step_plan(int dx, int dy, int steps) {
    if (steps < 1) steps = 1;
    return plan_relative_steps(dx, dy, steps);
}

}  // namespace cufast
