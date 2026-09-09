// The capture pipeline for one monitor. See screen.hpp for why it has no
// binding layer in it.

#include "capture/screen.hpp"

#include <algorithm>
#include <chrono>
#include <thread>

#include "input/input.hpp"

namespace cufast {

Screen::Screen(int display_index, bool prefer_dxgi)
    : capture_(display_index, prefer_dxgi) {}

Shot Screen::grab(int max_w, int max_h, float quality, bool draw_cursor, int timeout_ms,
                  int rx, int ry, int rw, int rh, bool png, bool allow_upscale) {
        std::lock_guard<std::mutex> lock(mutex_);

        FrameView frame = capture_.grab(draw_cursor, timeout_ms);

        // A negative width means "the whole monitor".
        if (rw < 0 || rh < 0) {
            rx = 0;
            ry = 0;
            rw = frame.width;
            rh = frame.height;
        }
        // Clamp rather than reject: the model works in the downscaled space, so a
        // region derived from those coordinates can round a pixel past the edge.
        rx = (std::max)(0, (std::min)(rx, frame.width - 1));
        ry = (std::max)(0, (std::min)(ry, frame.height - 1));
        rw = (std::max)(1, (std::min)(rw, frame.width - rx));
        rh = (std::max)(1, (std::min)(rh, frame.height - ry));

        ScalePlan plan = plan_fit(rw, rh, max_w, max_h, allow_upscale);
        plan.src_x = rx;
        plan.src_y = ry;

        downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);

        Shot shot;
        shot.width = plan.dst_w;
        shot.height = plan.dst_h;
        shot.src_x = rx;
        shot.src_y = ry;
        shot.src_w = rw;
        shot.src_h = rh;
        shot.frame_id = capture_.frame_id();
        shot.dxgi = capture_.using_dxgi();
        shot.content_hash = hash_bgr(pixels_.data(), pixels_.size());
        shot.data = png ? encode_png(pixels_.data(), plan.dst_w, plan.dst_h)
                        : encode_jpeg(pixels_.data(), plan.dst_w, plan.dst_h, quality);
        return shot;
    }

    // The 1-D luma profile of the current frame, for measuring how far the view
    // panned between two captures. Skips the JPEG encode, which is most of the cost
    // of a screenshot and produces nothing this needs.
std::vector<int32_t> Screen::profile(int max_w, int max_h, int timeout_ms) {
        std::lock_guard<std::mutex> lock(mutex_);
        // No cursor: it does not move with the camera, so compositing it in would
        // plant a fixed feature in a signal that is entirely about movement.
        FrameView frame = capture_.grab(false, timeout_ms);
        ScalePlan plan = plan_fit(frame.width, frame.height, max_w, max_h, false);
        downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);
        return column_profile(pixels_.data(), plan.dst_w, plan.dst_h);
    }

    // The vertical counterpart, for measuring how far a pitch change moved the view.
std::vector<int32_t> Screen::profile_rows(int max_w, int max_h, int timeout_ms) {
        std::lock_guard<std::mutex> lock(mutex_);
        FrameView frame = capture_.grab(false, timeout_ms);
        ScalePlan plan = plan_fit(frame.width, frame.height, max_w, max_h, false);
        downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);
        return row_profile(pixels_.data(), plan.dst_w, plan.dst_h);
    }

    // Blocks until the screen changes, or the deadline passes.
    //
    // Worth doing in C++ because the wait itself is an OS primitive: DXGI's
    // AcquireNextFrame parks the thread in the driver until the compositor presents
    // a new frame, so this costs nothing while nothing is happening and returns the
    // instant something does. The alternative the model reaches for otherwise is a
    // guessed sleep followed by a screenshot to find out whether the guess was long
    // enough -- and a second round trip when it was not.
    //
    // Returns milliseconds waited, -1 on timeout, -2 if the kill switch engaged.
double Screen::wait_for_change(double timeout_seconds, int grid_w, int grid_h) {
        std::lock_guard<std::mutex> lock(mutex_);
        using Clock = std::chrono::steady_clock;

        auto sample = [&]() {
            FrameView frame = capture_.grab(false, 0);
            ScalePlan plan = plan_fit(frame.width, frame.height, grid_w, grid_h, false);
            downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);
            return hash_bgr(pixels_.data(), pixels_.size());
        };

        const auto start = Clock::now();
        const auto deadline = start + std::chrono::duration_cast<Clock::duration>(
                                          std::chrono::duration<double>(timeout_seconds));
        const uint64_t baseline = sample();

        while (true) {
            if (input_blocked()) return -2.0;
            const auto now = Clock::now();
            if (now >= deadline) return -1.0;

            const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                                       deadline - now).count();
            // Capped so the kill switch is consulted regularly however long the
            // caller asked to wait.
            const int slice = static_cast<int>((std::min)(remaining, static_cast<long long>(100)));

            const auto before = Clock::now();
            FrameView frame = capture_.grab(false, slice);
            ScalePlan plan = plan_fit(frame.width, frame.height, grid_w, grid_h, false);
            downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);
            if (hash_bgr(pixels_.data(), pixels_.size()) != baseline) {
                return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
            }
            // Only on the GDI path, which has no blocking acquire and returns at
            // once, so without this the loop would spin a core flat. Sleeping on the
            // DXGI path as well turned an event wait into a 100 ms poll: a present
            // that does not alter the hash -- a blinking caret, a clock digit --
            // makes the acquire return early, and anything appearing during the
            // sleep then went unreported for the rest of the slice.
            if (!capture_.using_dxgi()) {
                const auto spent =
                    std::chrono::duration<double, std::milli>(Clock::now() - before).count();
                if (spent < slice) {
                    std::this_thread::sleep_for(
                        std::chrono::duration<double, std::milli>(slice - spent));
                }
            }
        }
    }

    // Hash of the monitor downscaled to a small fixed grid. Cheap enough to poll,
    // and coarse enough that cursor blink or a caret does not read as a change.
uint64_t Screen::sample_hash(int grid_w, int grid_h, int timeout_ms) {
        std::lock_guard<std::mutex> lock(mutex_);
        FrameView frame = capture_.grab(false, timeout_ms);
        ScalePlan plan = plan_fit(frame.width, frame.height, grid_w, grid_h, false);
        downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);
        return hash_bgr(pixels_.data(), pixels_.size());
    }

    // Locked like everything else that touches the capture. grab() mutates monitor_
    // -- including its std::wstring -- from inside ensure_geometry when the display
    // mode changes, so reading it unlocked is a data race. Unreachable today only
    // because the Python side funnels every native call onto one worker thread,
    // which is a property of the caller rather than of this class.

void Screen::forget_display_for_test() {
    std::lock_guard<std::mutex> lock(mutex_);
    capture_.forget_display_for_test();
}

int Screen::width() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return capture_.monitor().width();
}

int Screen::height() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return capture_.monitor().height();
}

int Screen::origin_x() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return capture_.monitor().rect.left;
}

int Screen::origin_y() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return capture_.monitor().rect.top;
}

int Screen::index() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return capture_.monitor().index;
}

bool Screen::using_dxgi() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return capture_.using_dxgi();
}


}  // namespace cufast
