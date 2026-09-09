#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/vector.h>

#include <chrono>
#include <mutex>
#include <thread>

#include "capture/capture.hpp"
#include "hotkey/hotkey.hpp"
#include "image/image.hpp"
#include "input/input.hpp"

namespace nb = nanobind;
using namespace cufast;

namespace {

// One encoded frame plus everything the Python layer needs to map the model's
// coordinates back onto the desktop.
struct Shot {
    std::vector<uint8_t> data;
    int width = 0, height = 0;   // encoded image size, i.e. what the model sees
    int src_x = 0, src_y = 0;    // captured region, monitor-local
    int src_w = 0, src_h = 0;
    uint64_t frame_id = 0;
    uint64_t content_hash = 0;
    bool dxgi = false;
};

// Owns a monitor's capture pipeline and its scratch buffers, so a steady stream of
// screenshots performs no allocation after the first.
class Screen {
public:
    Screen(int display_index, bool prefer_dxgi) : capture_(display_index, prefer_dxgi) {}

    Shot grab(int max_w, int max_h, float quality, bool draw_cursor, int timeout_ms,
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
    std::vector<int32_t> profile(int max_w, int max_h, int timeout_ms) {
        std::lock_guard<std::mutex> lock(mutex_);
        // No cursor: it does not move with the camera, so compositing it in would
        // plant a fixed feature in a signal that is entirely about movement.
        FrameView frame = capture_.grab(false, timeout_ms);
        ScalePlan plan = plan_fit(frame.width, frame.height, max_w, max_h, false);
        downscale_bgra_to_bgr(frame, plan, pixels_, scratch_);
        return column_profile(pixels_.data(), plan.dst_w, plan.dst_h);
    }

    // The vertical counterpart, for measuring how far a pitch change moved the view.
    std::vector<int32_t> profile_rows(int max_w, int max_h, int timeout_ms) {
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
    double wait_for_change(double timeout_seconds, int grid_w, int grid_h) {
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
    uint64_t sample_hash(int grid_w, int grid_h, int timeout_ms) {
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
    int width() const { std::lock_guard<std::mutex> lock(mutex_); return capture_.monitor().width(); }
    int height() const { std::lock_guard<std::mutex> lock(mutex_); return capture_.monitor().height(); }
    int origin_x() const { std::lock_guard<std::mutex> lock(mutex_); return capture_.monitor().rect.left; }
    int origin_y() const { std::lock_guard<std::mutex> lock(mutex_); return capture_.monitor().rect.top; }
    int index() const { std::lock_guard<std::mutex> lock(mutex_); return capture_.monitor().index; }
    bool using_dxgi() const { std::lock_guard<std::mutex> lock(mutex_); return capture_.using_dxgi(); }

private:
    mutable std::mutex mutex_;
    Capture capture_;
    std::vector<uint8_t> pixels_;
    ScaleScratch scratch_;
};

MouseButton parse_button(const std::string& name) {
    if (name == "left") return MouseButton::Left;
    if (name == "right") return MouseButton::Right;
    if (name == "middle") return MouseButton::Middle;
    throw Error("button must be left, right, or middle");
}

}  // namespace

NB_MODULE(_native, m) {
    m.doc() = "Native Windows capture and input for the cufast computer-use harness.";

    // The whole module is compiled with /arch:AVX2, so a CPU without it would fault
    // with an illegal instruction rather than raising anything Python could catch.
    if (!cpu_supports_avx2()) {
        throw std::runtime_error(
            "cufast requires a CPU with AVX2 (Intel Haswell / AMD Excavator, 2013 onward)");
    }

    // Must happen before anything measures or clicks a pixel.
    enable_dpi_awareness();

    nb::class_<Shot>(m, "Shot")
        .def_prop_ro("data",
                     [](const Shot& s) {
                         return nb::bytes(reinterpret_cast<const char*>(s.data.data()),
                                          s.data.size());
                     })
        .def_ro("width", &Shot::width)
        .def_ro("height", &Shot::height)
        .def_ro("src_x", &Shot::src_x)
        .def_ro("src_y", &Shot::src_y)
        .def_ro("src_w", &Shot::src_w)
        .def_ro("src_h", &Shot::src_h)
        .def_ro("frame_id", &Shot::frame_id)
        .def_ro("content_hash", &Shot::content_hash)
        .def_ro("dxgi", &Shot::dxgi);

    nb::class_<Screen>(m, "Screen")
        .def(nb::init<int, bool>(), nb::arg("display_index") = 0,
             nb::arg("prefer_dxgi") = true)
        .def(
            "grab",
            [](Screen& self, int max_w, int max_h, float quality, bool draw_cursor, int timeout_ms,
               int rx, int ry, int rw, int rh, bool png, bool allow_upscale) {
                nb::gil_scoped_release release;
                return self.grab(max_w, max_h, quality, draw_cursor, timeout_ms, rx, ry, rw, rh,
                                 png, allow_upscale);
            },
            nb::arg("max_w"), nb::arg("max_h"), nb::arg("quality") = 0.75f,
            nb::arg("draw_cursor") = true, nb::arg("timeout_ms") = 16, nb::arg("rx") = 0,
            nb::arg("ry") = 0, nb::arg("rw") = -1, nb::arg("rh") = -1, nb::arg("png") = false,
            nb::arg("allow_upscale") = false)
        .def(
            "sample_hash",
            [](Screen& self, int grid_w, int grid_h, int timeout_ms) {
                nb::gil_scoped_release release;
                return self.sample_hash(grid_w, grid_h, timeout_ms);
            },
            nb::arg("grid_w") = 160, nb::arg("grid_h") = 90, nb::arg("timeout_ms") = 16)
        .def(
            "profile",
            [](Screen& self, int max_w, int max_h, int timeout_ms) {
                nb::gil_scoped_release release;
                return self.profile(max_w, max_h, timeout_ms);
            },
            nb::arg("max_w"), nb::arg("max_h"), nb::arg("timeout_ms") = 16)
        .def(
            "profile_rows",
            [](Screen& self, int max_w, int max_h, int timeout_ms) {
                nb::gil_scoped_release release;
                return self.profile_rows(max_w, max_h, timeout_ms);
            },
            nb::arg("max_w"), nb::arg("max_h"), nb::arg("timeout_ms") = 16)
        .def(
            "wait_for_change",
            [](Screen& self, double timeout_seconds, int grid_w, int grid_h) {
                nb::gil_scoped_release release;
                return self.wait_for_change(timeout_seconds, grid_w, grid_h);
            },
            nb::arg("timeout_seconds"), nb::arg("grid_w") = 160, nb::arg("grid_h") = 90)
        // The GIL is released around these for the same reason it is released around
        // grab: they now take the capture's lock, and wait_for_change can hold that
        // for its whole timeout. Blocking on it while holding the GIL would stall
        // every other Python thread in the process for up to five minutes -- a worse
        // failure than the unsynchronised read this locking replaced.
        .def_prop_ro("width", [](Screen& s) { nb::gil_scoped_release r; return s.width(); })
        .def_prop_ro("height", [](Screen& s) { nb::gil_scoped_release r; return s.height(); })
        .def_prop_ro("origin_x", [](Screen& s) { nb::gil_scoped_release r; return s.origin_x(); })
        .def_prop_ro("origin_y", [](Screen& s) { nb::gil_scoped_release r; return s.origin_y(); })
        .def_prop_ro("index", [](Screen& s) { nb::gil_scoped_release r; return s.index(); })
        .def_prop_ro("using_dxgi",
                     [](Screen& s) { nb::gil_scoped_release r; return s.using_dxgi(); });

    // Exposed so the Python coordinate mapping uses the exact same fit the encoder
    // used, rather than a second copy of the rounding rules that could drift.
    m.def(
        "plan_fit",
        [](int src_w, int src_h, int max_w, int max_h, bool allow_upscale) {
            const ScalePlan p = plan_fit(src_w, src_h, max_w, max_h, allow_upscale);
            return nb::make_tuple(p.dst_w, p.dst_h);
        },
        nb::arg("src_w"), nb::arg("src_h"), nb::arg("max_w"), nb::arg("max_h"),
        nb::arg("allow_upscale") = false);

    // Test seam: runs the downscale over a caller-supplied BGRA buffer instead of a
    // live capture, so the SIMD and scalar paths can be compared on identical input.
    // A screen changes between grabs; a synthetic buffer does not.
    m.def(
        "_downscale_raw",
        [](nb::bytes bgra, int src_w, int src_h, int dst_w, int dst_h, bool force_general) {
            const size_t need = static_cast<size_t>(src_w) * src_h * 4;
            if (src_w <= 0 || src_h <= 0) throw Error("_downscale_raw: empty source");
            if (bgra.size() < need) throw Error("_downscale_raw: buffer shorter than src_w*src_h*4");

            FrameView frame;
            frame.pixels = const_cast<uint8_t*>(reinterpret_cast<const uint8_t*>(bgra.c_str()));
            frame.width = src_w;
            frame.height = src_h;
            frame.stride = src_w * 4;

            ScalePlan plan;
            plan.src_x = 0;
            plan.src_y = 0;
            plan.src_w = src_w;
            plan.src_h = src_h;
            plan.dst_w = dst_w;
            plan.dst_h = dst_h;

            std::vector<uint8_t> out;
            ScaleScratch scratch;
            downscale_bgra_to_bgr(frame, plan, out, scratch, force_general);
            return nb::bytes(reinterpret_cast<const char*>(out.data()), out.size());
        },
        nb::arg("bgra"), nb::arg("src_w"), nb::arg("src_h"), nb::arg("dst_w"), nb::arg("dst_h"),
        nb::arg("force_general") = false);

    m.def(
        "_rotate_raw",
        [](nb::bytes bgra, int src_w, int src_h, int turns) {
            if (src_w <= 0 || src_h <= 0) throw Error("_rotate_raw: empty source");
            const size_t need = static_cast<size_t>(src_w) * src_h * 4;
            if (bgra.size() < need) throw Error("_rotate_raw: buffer shorter than src_w*src_h*4");
            const int dst_w = (turns & 1) ? src_h : src_w;
            const int dst_h = (turns & 1) ? src_w : src_h;
            std::vector<uint8_t> out(static_cast<size_t>(dst_w) * dst_h * 4);
            rotate_bgra(reinterpret_cast<const uint8_t*>(bgra.c_str()), src_w * 4, src_w, src_h,
                        out.data(), dst_w * 4, turns);
            return nb::bytes(reinterpret_cast<const char*>(out.data()), out.size());
        },
        nb::arg("bgra"), nb::arg("src_w"), nb::arg("src_h"), nb::arg("turns"));

    m.def("list_displays", []() {
        nb::list out;
        for (const auto& mon : enumerate_monitors()) {
            nb::dict d;
            d["index"] = mon.index;
            d["primary"] = mon.is_primary;
            // Device names are ASCII ("\\\\.\\DISPLAY1"), so a narrowing copy is
            // safe. Reported because "display 2" in Windows is index 1 here, and
            // that ambiguity is worth removing.
            const std::string device(mon.device_name.begin(), mon.device_name.end());
            d["device"] = nb::str(device.c_str());
            d["x"] = static_cast<int>(mon.rect.left);
            d["y"] = static_cast<int>(mon.rect.top);
            d["width"] = mon.width();
            d["height"] = mon.height();
            out.append(d);
        }
        return out;
    });

    // --- input -------------------------------------------------------------
    // Every one of these takes absolute virtual-desktop coordinates.

    m.def(
        "mouse_move",
        [](int x, int y) {
            nb::gil_scoped_release release;
            mouse_move(x, y);
        },
        nb::arg("x"), nb::arg("y"));

    m.def(
        "mouse_click",
        [](const std::string& button, int clicks, const std::string& modifiers) {
            nb::gil_scoped_release release;
            mouse_click(parse_button(button), clicks, modifiers);
        },
        nb::arg("button") = "left", nb::arg("clicks") = 1, nb::arg("modifiers") = "");

    m.def(
        "mouse_down",
        [](const std::string& button) {
            nb::gil_scoped_release release;
            mouse_down(parse_button(button));
        },
        nb::arg("button") = "left");

    m.def(
        "mouse_up",
        [](const std::string& button) {
            nb::gil_scoped_release release;
            mouse_up(parse_button(button));
        },
        nb::arg("button") = "left");

    m.def(
        "mouse_drag",
        [](int x0, int y0, int x1, int y1, const std::string& modifiers) {
            nb::gil_scoped_release release;
            mouse_drag(x0, y0, x1, y1, modifiers);
        },
        nb::arg("x0"), nb::arg("y0"), nb::arg("x1"), nb::arg("y1"), nb::arg("modifiers") = "");

    m.def(
        "mouse_scroll",
        [](const std::string& direction, int amount, const std::string& modifiers) {
            nb::gil_scoped_release release;
            mouse_scroll(direction, amount, modifiers);
        },
        nb::arg("direction"), nb::arg("amount"), nb::arg("modifiers") = "");

    m.def(
        "mouse_move_relative",
        [](int dx, int dy, int steps) {
            nb::gil_scoped_release release;
            mouse_move_relative(dx, dy, steps);
        },
        nb::arg("dx"), nb::arg("dy"), nb::arg("steps") = 1);

    m.def(
        "key_down",
        [](const std::string& chord) {
            nb::gil_scoped_release release;
            key_down(chord);
        },
        nb::arg("chord"));

    m.def(
        "key_up",
        [](const std::string& chord) {
            nb::gil_scoped_release release;
            key_up(chord);
        },
        nb::arg("chord"));

    m.def("held_keys", &held_keys);
    m.def("_held_registry", &held_registry_for_test, nb::arg("down"), nb::arg("up") = "");
    m.def("validate_chord", &validate_chord, nb::arg("chord"));

    m.def(
        "best_shift",
        [](const std::vector<int32_t>& a, const std::vector<int32_t>& b, int max_shift) {
            ShiftEstimate est;
            {
                nb::gil_scoped_release release;
                est = best_shift(a, b, max_shift);
            }
            return nb::make_tuple(est.shift, est.confidence);
        },
        nb::arg("a"), nb::arg("b"), nb::arg("max_shift"));
    m.def("_relative_step_plan", &relative_step_plan,
          nb::arg("dx"), nb::arg("dy"), nb::arg("steps"));

    m.def("cursor_position", []() {
        int x = 0, y = 0;
        {
            nb::gil_scoped_release release;
            get_cursor_pos(&x, &y);
        }
        return nb::make_tuple(x, y);
    });

    m.def(
        "type_text",
        [](const std::string& text) {
            nb::gil_scoped_release release;
            type_text(text);
        },
        nb::arg("text"));

    m.def(
        "press_key",
        [](const std::string& chord, int repeat) {
            nb::gil_scoped_release release;
            press_key(chord, repeat);
        },
        nb::arg("chord"), nb::arg("repeat") = 1);

    m.def(
        "hold_key",
        [](const std::string& chord, double seconds) {
            nb::gil_scoped_release release;
            hold_key(chord, seconds);
        },
        nb::arg("chord"), nb::arg("seconds"));

    m.def("set_input_blocked", &set_input_blocked, nb::arg("blocked"));
    m.def("input_blocked", &input_blocked);
    m.def("release_held_input", []() {
        // SendInput can block for the low-level hook timeout window, and holding the
        // GIL across that stalls every other Python thread in the process.
        nb::gil_scoped_release release;
        release_held_input();
    });

    // The hook thread must be able to run while Python is busy, and the pump calls
    // back into SendInput, so none of these may hold the GIL.
    m.def("start_kill_switch", []() {
        nb::gil_scoped_release release;
        start_kill_switch();
    });
    m.def("stop_kill_switch", []() {
        nb::gil_scoped_release release;
        stop_kill_switch();
    });
    m.def("_trip_kill_switch", []() {
        nb::gil_scoped_release release;
        trip_kill_switch_for_test();
    });
    m.def("_hook_key_event", &hook_key_event_for_test,
          nb::arg("down"), nb::arg("injected"), nb::arg("ctrl_down"));
    m.def("kill_switch_running", &kill_switch_running);
    m.def("kill_switch_trips", &kill_switch_trips);
    m.def("dpi_per_monitor_aware", &dpi_per_monitor_aware);
}
