#pragma once

#include <d3d11.h>
#include <dxgi1_2.h>

#include <string>
#include <vector>

#include "common.h"

namespace cufast {

struct MonitorInfo {
    int index = 0;              // stable: primary is always 0
    std::wstring device_name;   // e.g. L"\\.\DISPLAY1"
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
    // True if the last grab() came from the DXGI path.
    bool using_dxgi() const { return dxgi_ready_; }
    // Monotonically increasing; unchanged when grab() reused a cached frame.
    uint64_t frame_id() const { return frame_id_; }

private:
    void init_dib(int width, int height);
    bool init_dxgi();          // false if duplication is unavailable
    void teardown_dxgi();
    bool grab_dxgi(int timeout_ms);  // true if a new frame landed in the DIB
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
    bool holding_frame_ = false;  // AcquireNextFrame outstanding

    bool have_frame_ = false;
    uint64_t frame_id_ = 0;
};

}  // namespace cufast
