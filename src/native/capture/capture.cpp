// Lifetime, display-geometry tracking, and choosing between the two paths.
#include "capture/capture.hpp"

#include <algorithm>

namespace cufast {
namespace {

RECT virtual_screen_rect() {
    const int x = GetSystemMetrics(SM_XVIRTUALSCREEN);
    const int y = GetSystemMetrics(SM_YVIRTUALSCREEN);
    return RECT{x, y, x + GetSystemMetrics(SM_CXVIRTUALSCREEN),
                y + GetSystemMetrics(SM_CYVIRTUALSCREEN)};
}

bool same_rect(const RECT& a, const RECT& b) {
    return a.left == b.left && a.top == b.top && a.right == b.right && a.bottom == b.bottom;
}

}  // namespace

Capture::Capture(int monitor_index, bool allow_dxgi) : allow_dxgi_(allow_dxgi) {
    auto monitors = enumerate_monitors();
    if (monitor_index < 0 || monitor_index >= static_cast<int>(monitors.size())) {
        char buf[128];
        std::snprintf(buf, sizeof(buf), "display_index %d out of range (%zu display(s) attached)",
                      monitor_index, monitors.size());
        throw Error(buf);
    }
    monitor_ = monitors[monitor_index];
    seen_monitor_count_ = static_cast<int>(monitors.size());
    seen_virtual_rect_ = virtual_screen_rect();

    // A throw here escapes the constructor, so the destructor never runs and any
    // GDI handle already created would leak. Clean up explicitly.
    try {
        init_dib(monitor_.width(), monitor_.height());
    } catch (...) {
        release_gdi();
        throw;
    }
    dxgi_ready_ = init_dxgi();  // GDI fallback is used when this returns false
    next_dxgi_retry_ = std::chrono::steady_clock::now() + kDxgiRetryInterval;
}

Capture::~Capture() {
    teardown_dxgi();
    release_gdi();
}

void Capture::ensure_geometry(bool force) {
    // GetSystemMetrics is a handful of nanoseconds, so this guard is effectively
    // free next to the ~4 ms pipeline it protects; the full re-enumeration only
    // runs when the desktop layout actually moved.
    const RECT current = virtual_screen_rect();
    const int count = GetSystemMetrics(SM_CMONITORS);
    if (!force && count == seen_monitor_count_ && same_rect(current, seen_virtual_rect_)) {
        return;
    }

    seen_monitor_count_ = count;
    seen_virtual_rect_ = current;

    // Re-resolve by device name rather than by index: a monitor being unplugged
    // renumbers the list, and following the index would silently start capturing a
    // different physical display.
    for (const auto& candidate : enumerate_monitors()) {
        if (candidate.device_name != monitor_.device_name) continue;
        const bool resized =
            candidate.width() != width_ || candidate.height() != height_;
        const bool moved = !same_rect(candidate.rect, monitor_.rect);
        monitor_ = candidate;
        display_gone_ = false;
        if (resized) {
            init_dib(monitor_.width(), monitor_.height());
            // The duplication surface is tied to the old mode.
            teardown_dxgi();
            dxgi_ready_ = init_dxgi();
            needs_reseed_ = true;
        } else if (moved) {
            // The GDI path blits from absolute desktop coordinates, so a moved
            // monitor invalidates the cached frame even at the same size.
            needs_reseed_ = true;
        }
        return;
    }
    // The display this object was built for is gone.
    //
    // This used to return here and change nothing, which was the worst of the
    // options available. dxgi_ready_ stayed true, so every later grab kept asking a
    // duplication object for a monitor that no longer exists; it timed out, which
    // reads as "the screen is idle", and the cached frame from before the unplug was
    // served instead -- for as long as the process lived. Nothing said so. The
    // content hash matched every time, so the harness reported "screen unchanged"
    // and the model was told an hour-old desktop was current. Coordinates measured
    // off it were coordinates on a display that was gone.
    //
    // Failing is better than that. A screenshot that throws is a problem the caller
    // can see and act on; a screenshot that quietly shows last hour is not.
    teardown_dxgi();
    display_gone_ = true;
}

FrameView Capture::grab(bool draw_cursor, int timeout_ms) {
    ensure_geometry();
    if (display_gone_) {
        throw Error("the display this harness was controlling is no longer attached, "
                    "so there is nothing to capture. Call screen_info to see which "
                    "displays exist now, then pass `display` to move to one of them.");
    }
    maybe_retry_dxgi();

    // CreateDIBSection requires a flush before the bitmap bits are touched through
    // the pointer, and everything below reads or writes pixels_ directly.
    GdiFlush();

    bool fresh = false;
    if (dxgi_ready_) {
        fresh = grab_dxgi(timeout_ms);
        if (!dxgi_ready_) {  // duplication died and could not be re-established
            grab_gdi();
            fresh = true;
        }
    } else {
        grab_gdi();
        fresh = true;
    }

    if (!fresh && needs_reseed_) {
        // Duplication was re-established but has not presented a frame yet. Without
        // this the cached pre-disruption image would be served indefinitely on a
        // static desktop, with frame_id and content_hash both claiming nothing moved.
        grab_gdi();
        fresh = true;
    }

    if (fresh) {
        // A fresh frame overwrote everything, including the last cursor composite.
        cursor_drawn_ = false;
        have_frame_ = true;
        needs_reseed_ = false;
        ++frame_id_;
    } else if (!have_frame_) {
        // Duplication timed out before ever handing us an image.
        grab_gdi();
        have_frame_ = true;
        cursor_drawn_ = false;
        ++frame_id_;
    } else {
        restore_under_cursor();
    }

    if (draw_cursor) draw_cursor_into_dib();

    return FrameView{pixels_, width_, height_, stride_};
}

}  // namespace cufast
