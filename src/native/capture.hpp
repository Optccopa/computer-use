#pragma once

#include <d3d11.h>
#include <dxgi1_2.h>

#include <chrono>
#include <string>
#include <vector>

#include "common.hpp"

namespace cufast {

struct MonitorInfo {
    int index = 0;              // stable: primary is always 0
    std::wstring device_name;   // e.g. L"\\\\.\\DISPLAY1"
    RECT rect{};                // virtual-desktop coords; may be negative
    bool is_primary = false;

    int width() const { return rect.right - rect.left; }
    int height() const { return rect.bottom - rect.top; }
};

std::vector<MonitorInfo> enumerate_monitors();

// Captures one monitor. Prefers DXGI Desktop Duplication (~1-3 ms, keeps the
// D3D11 device and duplication object warm across calls) and falls back to a
// GDI BitBlt (~15-30 ms) when duplication is unavailable: secure desktop, an
// active session switch, or a driver that refuses DuplicateOutput.
//
// Rotated panels stay on the fast path. Duplication hands back the physical panel
// surface -- on a portrait monitor, the landscape image lying on its side -- so the
// copy out applies the quarter-turn that makes it the desktop image GDI would have
// reported. The result is identical either way; only the cost differs.
//
// Both paths write into one top-down 32bpp BGRA DIB section so the mouse cursor
// can be composited with DrawIconEx regardless of which path produced the frame.
class Capture {
public:
    explicit Capture(int monitor_index);
    ~Capture();

    Capture(const Capture&) = delete;
    Capture& operator=(const Capture&) = delete;

    // Grabs a frame. timeout_ms is how long to wait for the compositor to
    // present a *new* frame; on timeout the previously captured frame is
    // reused, which is correct because nothing on screen changed.
    // Returns a view into internal storage, valid until the next grab().
    FrameView grab(bool draw_cursor, int timeout_ms);

    const MonitorInfo& monitor() const { return monitor_; }
    bool using_dxgi() const { return dxgi_ready_; }
    // Monotonically increasing; unchanged when grab() reused a cached frame.
    uint64_t frame_id() const { return frame_id_; }

private:
    void release_gdi();
    void init_dib(int width, int height);
    // Re-resolves this monitor and resizes the surface if the display mode changed.
    // Without it a resolution change silently produces a cropped frame that still
    // reports the old dimensions, which maps every later click to the wrong place.
    // force skips the cheap guard. The guard only notices a change that alters the
    // monitor count or the virtual-desktop bounding box, and a monitor that is not on
    // the right or bottom edge can change mode without altering either -- so a caller
    // that already knows the geometry moved has to be able to say so.
    void ensure_geometry(bool force = false);

    bool init_dxgi();
    void teardown_dxgi();
    void maybe_retry_dxgi();
    bool grab_dxgi(int timeout_ms);  // true if a new frame landed in the DIB
    void release_held_frame();
    void grab_gdi();
    void draw_cursor_into_dib();
    void restore_under_cursor();

    MonitorInfo monitor_;

    // Destination DIB (owned).
    HDC mem_dc_ = nullptr;
    HBITMAP dib_ = nullptr;
    HGDIOBJ old_bitmap_ = nullptr;
    uint8_t* pixels_ = nullptr;  // BGRA top-down, into dib_
    int width_ = 0;
    int height_ = 0;
    int stride_ = 0;

    // Cheap guard against display reconfiguration, checked per grab.
    int seen_monitor_count_ = 0;
    RECT seen_virtual_rect_{};

    // Saved pixels under the last composited cursor, so a reused frame can be
    // un-drawn without re-copying the whole surface.
    std::vector<uint8_t> cursor_backup_;
    RECT cursor_rect_{};
    bool cursor_drawn_ = false;

    // DXGI duplication state.
    bool dxgi_ready_ = false;
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    ComPtr<IDXGIOutputDuplication> dupl_;
    ComPtr<ID3D11Texture2D> staging_;
    // Clockwise quarter-turns needed to take the duplication surface to the desktop
    // orientation. 0 on a normal landscape display.
    int dxgi_turns_ = 0;
    // The docs recommend holding the frame until just before the next acquire:
    // while the client does not own it, the OS copies every desktop update into
    // the surface, which is wasted GPU work across the seconds an agent spends
    // thinking between screenshots.
    bool holding_frame_ = false;
    std::chrono::steady_clock::time_point next_dxgi_retry_{};

    bool have_frame_ = false;
    // Set when duplication was re-established, so the next grab reseeds from GDI
    // rather than handing back the frame from before the disruption.
    bool needs_reseed_ = false;
    uint64_t frame_id_ = 0;
};

}  // namespace cufast
