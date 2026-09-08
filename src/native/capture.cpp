#include "capture.hpp"

#include <algorithm>
#include <cstring>

#include "image.hpp"

namespace cufast {
namespace {

// How long to wait before retrying duplication after it was refused. Refusals are
// usually transient -- a UAC prompt owning the secure desktop, or the documented
// four-concurrent-duplicator limit while a screen-share app is running -- so never
// retrying would pin the process to the 15-30 ms GDI path for its whole lifetime.
constexpr auto kDxgiRetryInterval = std::chrono::seconds(2);

struct EnumCtx {
    std::vector<MonitorInfo>* out;
    bool failed = false;
};

BOOL CALLBACK monitor_enum_proc(HMONITOR hmon, HDC, LPRECT, LPARAM lparam) {
    auto* ctx = reinterpret_cast<EnumCtx*>(lparam);
    // Never let an exception unwind through user32's enumeration frame: foreign
    // frames are not guaranteed unwindable and its internal state would be left
    // inconsistent.
    try {
        MONITORINFOEXW mi{};
        mi.cbSize = sizeof(mi);
        if (GetMonitorInfoW(hmon, &mi)) {
            MonitorInfo info;
            info.device_name = mi.szDevice;
            info.rect = mi.rcMonitor;
            info.is_primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
            ctx->out->push_back(std::move(info));
        } else {
            ctx->failed = true;
        }
    } catch (...) {
        ctx->failed = true;
        return FALSE;
    }
    return TRUE;
}

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

std::vector<MonitorInfo> enumerate_monitors() {
    std::vector<MonitorInfo> monitors;
    monitors.reserve(8);  // so the callback does not allocate mid-enumeration
    EnumCtx ctx{&monitors};
    EnumDisplayMonitors(nullptr, nullptr, monitor_enum_proc, reinterpret_cast<LPARAM>(&ctx));
    if (monitors.empty()) throw Error("no displays found");

    // Stable ordering so display_index means the same thing across runs:
    // primary first, then left-to-right, top-to-bottom.
    std::sort(monitors.begin(), monitors.end(), [](const MonitorInfo& a, const MonitorInfo& b) {
        if (a.is_primary != b.is_primary) return a.is_primary;
        if (a.rect.left != b.rect.left) return a.rect.left < b.rect.left;
        return a.rect.top < b.rect.top;
    });
    for (size_t i = 0; i < monitors.size(); ++i) monitors[i].index = static_cast<int>(i);
    return monitors;
}

Capture::Capture(int monitor_index) {
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

void Capture::release_gdi() {
    if (mem_dc_) {
        if (old_bitmap_) SelectObject(mem_dc_, old_bitmap_);
        DeleteDC(mem_dc_);
        mem_dc_ = nullptr;
        old_bitmap_ = nullptr;
    }
    if (dib_) {
        DeleteObject(dib_);
        dib_ = nullptr;
    }
    pixels_ = nullptr;
}

void Capture::init_dib(int width, int height) {
    if (width <= 0 || height <= 0) throw Error("display has zero area");
    release_gdi();

    HDC screen = GetDC(nullptr);
    if (!screen) throw Error("GetDC(NULL) failed");
    mem_dc_ = CreateCompatibleDC(screen);
    ReleaseDC(nullptr, screen);
    if (!mem_dc_) throw Error("CreateCompatibleDC failed");

    BITMAPINFO bi{};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = width;
    bi.bmiHeader.biHeight = -height;  // negative => top-down rows
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;

    void* bits = nullptr;
    dib_ = CreateDIBSection(mem_dc_, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (!dib_ || !bits) throw Error("CreateDIBSection failed");

    old_bitmap_ = SelectObject(mem_dc_, dib_);
    pixels_ = static_cast<uint8_t*>(bits);
    width_ = width;
    height_ = height;
    stride_ = width * 4;  // 32bpp rows are inherently DWORD-aligned
    have_frame_ = false;
    cursor_drawn_ = false;
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
    // The display this object was built for is gone. Keep serving the last frame
    // rather than throwing from what the caller thinks is a screenshot.
}

bool Capture::init_dxgi() {
    teardown_dxgi();
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(factory.put())))) return false;

    ComPtr<IDXGIAdapter1> found_adapter;
    ComPtr<IDXGIOutput> found_output;
    int turns = 0;
    for (UINT ai = 0; !found_output; ++ai) {
        ComPtr<IDXGIAdapter1> adapter;
        // Break on any failure, not just NOT_FOUND: anything else leaves the
        // out-param null and the next call would dereference it.
        if (FAILED(factory->EnumAdapters1(ai, adapter.put())) || !adapter) break;
        for (UINT oi = 0;; ++oi) {
            ComPtr<IDXGIOutput> output;
            if (FAILED(adapter->EnumOutputs(oi, output.put())) || !output) break;
            DXGI_OUTPUT_DESC desc{};
            if (FAILED(output->GetDesc(&desc))) continue;
            if (monitor_.device_name == desc.DeviceName) {
                // DXGI_MODE_ROTATION says how the panel is turned relative to the
                // desktop image, so undoing it is the turn in the other direction:
                // ROTATE90 means the desktop was rotated 90 degrees clockwise onto
                // the panel, and three more clockwise turns put it back.
                switch (desc.Rotation) {
                    case DXGI_MODE_ROTATION_ROTATE90:  turns = 3; break;
                    case DXGI_MODE_ROTATION_ROTATE180: turns = 2; break;
                    case DXGI_MODE_ROTATION_ROTATE270: turns = 1; break;
                    default:                           turns = 0; break;
                }
                found_adapter = adapter;
                found_output = output;
                break;
            }
        }
    }
    if (!found_output) return false;
    dxgi_turns_ = turns;

    D3D_FEATURE_LEVEL got{};
    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                        D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
    // D3D_DRIVER_TYPE_UNKNOWN is required when an explicit adapter is supplied.
    HRESULT hr = D3D11CreateDevice(found_adapter.get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                                   D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, ARRAYSIZE(levels),
                                   D3D11_SDK_VERSION, device_.put(), &got, context_.put());
    if (FAILED(hr)) {
        teardown_dxgi();
        return false;
    }

    ComPtr<IDXGIOutput1> output1;
    if (FAILED(found_output->QueryInterface(IID_PPV_ARGS(output1.put())))) {
        // Do not leave a live D3D11 device pinned for a path that will never run.
        teardown_dxgi();
        return false;
    }

    // E_ACCESSDENIED here means a secure desktop (UAC prompt, lock screen) owns the
    // session; DXGI_ERROR_NOT_CURRENTLY_AVAILABLE means the four-duplicator limit is
    // already taken. Both are transient, hence maybe_retry_dxgi().
    if (FAILED(output1->DuplicateOutput(device_.get(), dupl_.put()))) {
        teardown_dxgi();
        return false;
    }
    return true;
}

void Capture::teardown_dxgi() {
    release_held_frame();
    dxgi_turns_ = 0;
    staging_.reset();
    dupl_.reset();
    context_.reset();
    device_.reset();
    dxgi_ready_ = false;
}

void Capture::maybe_retry_dxgi() {
    if (dxgi_ready_) return;
    const auto now = std::chrono::steady_clock::now();
    if (now < next_dxgi_retry_) return;
    next_dxgi_retry_ = now + kDxgiRetryInterval;
    if (init_dxgi()) {
        dxgi_ready_ = true;
        needs_reseed_ = true;
    }
}

void Capture::release_held_frame() {
    if (!dupl_ || !holding_frame_) {
        holding_frame_ = false;
        return;
    }
    // ReleaseFrame reports ACCESS_LOST on desktop switch, mode change and DWM
    // transitions. Swallowing it costs a stale frame on the next grab.
    const HRESULT hr = dupl_->ReleaseFrame();
    holding_frame_ = false;
    if (hr == DXGI_ERROR_ACCESS_LOST || hr == DXGI_ERROR_INVALID_CALL) {
        dxgi_ready_ = false;
        needs_reseed_ = true;
    }
}

bool Capture::grab_dxgi(int timeout_ms) {
    for (int attempt = 0; attempt < 2; ++attempt) {
        if (!dupl_) return false;

        release_held_frame();
        if (!dxgi_ready_) return false;  // ReleaseFrame reported the duplication died

        DXGI_OUTDUPL_FRAME_INFO info{};
        ComPtr<IDXGIResource> resource;
        HRESULT hr = dupl_->AcquireNextFrame(static_cast<UINT>(timeout_ms), &info, resource.put());

        if (hr == DXGI_ERROR_WAIT_TIMEOUT) return false;  // screen idle; cached frame stands
        if (hr == DXGI_ERROR_ACCESS_LOST || hr == DXGI_ERROR_INVALID_CALL) {
            // Mode change, desktop switch, or another client took over duplication.
            dxgi_ready_ = init_dxgi();
            // The cached frame predates the disruption, so it must not be reused.
            needs_reseed_ = true;
            if (!dxgi_ready_) return false;
            continue;
        }
        if (FAILED(hr)) return false;

        holding_frame_ = true;

        // LastPresentTime == 0 means the compositor has presented nothing since
        // duplication started, so the desktop image is unchanged and the texture is
        // not guaranteed to hold anything. Usually that is just a pointer move. On
        // the first acquire it means the surface is still blank, which is why this
        // must not special-case the uncached path: grab() seeds from GDI instead,
        // rather than handing back a black screenshot.
        if (info.LastPresentTime.QuadPart == 0) return false;

        ComPtr<ID3D11Texture2D> texture;
        if (FAILED(resource->QueryInterface(IID_PPV_ARGS(texture.put())))) return false;

        D3D11_TEXTURE2D_DESC td{};
        texture->GetDesc(&td);

        // A texture that does not match the surface means the mode changed under us.
        // Copying the overlap would leave stale bands and a burnt-in cursor while
        // still reporting the old size, so resize and reseed instead. The panel is
        // the transpose of the desktop on a quarter-turned display, so that is what
        // the texture is compared against -- not the DIB.
        const int panel_w = (dxgi_turns_ & 1) ? height_ : width_;
        const int panel_h = (dxgi_turns_ & 1) ? width_ : height_;
        if (static_cast<int>(td.Width) != panel_w || static_cast<int>(td.Height) != panel_h) {
            // Forced: the texture size disagreeing with ours IS the evidence that
            // the mode changed, and on a monitor that is not on the right or bottom
            // edge of the desktop the cheap guard sees nothing. Without this the
            // capture stuck on GDI forever, still reporting the old resolution, and
            // every click mapped through it landed on the wrong display.
            ensure_geometry(true);
            needs_reseed_ = true;
            return false;
        }

        D3D11_TEXTURE2D_DESC sd{};
        if (staging_) staging_->GetDesc(&sd);
        if (!staging_ || sd.Width != td.Width || sd.Height != td.Height || sd.Format != td.Format) {
            staging_.reset();
            D3D11_TEXTURE2D_DESC want = td;
            want.Usage = D3D11_USAGE_STAGING;
            want.BindFlags = 0;
            want.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            want.MiscFlags = 0;
            want.MipLevels = 1;
            want.ArraySize = 1;
            want.SampleDesc.Count = 1;
            want.SampleDesc.Quality = 0;
            if (FAILED(device_->CreateTexture2D(&want, nullptr, staging_.put()))) return false;
        }

        context_->CopyResource(staging_.get(), texture.get());

        D3D11_MAPPED_SUBRESOURCE mapped{};
        if (FAILED(context_->Map(staging_.get(), 0, D3D11_MAP_READ, 0, &mapped))) return false;

        // IDXGIOutput1::DuplicateOutput always yields a 32-bit BGRA surface, which
        // matches the DIB byte for byte. rotate_bgra is a straight row copy when
        // there is no turn to apply, so the common case pays nothing for this.
        rotate_bgra(static_cast<const uint8_t*>(mapped.pData),
                    static_cast<int>(mapped.RowPitch), panel_w, panel_h,
                    pixels_, stride_, dxgi_turns_);
        context_->Unmap(staging_.get(), 0);

        // Deliberately not released here: the docs recommend holding until just
        // before the next acquire, because while the client does not own the frame
        // the OS copies every desktop update into the surface.
        return true;
    }
    return false;
}

void Capture::grab_gdi() {
    HDC screen = GetDC(nullptr);
    if (!screen) throw Error("GetDC(NULL) failed");
    // CAPTUREBLT includes layered windows: tooltips, menus, drop shadows.
    BOOL ok = BitBlt(mem_dc_, 0, 0, width_, height_, screen, monitor_.rect.left,
                     monitor_.rect.top, SRCCOPY | CAPTUREBLT);
    ReleaseDC(nullptr, screen);
    if (!ok) throw Error("BitBlt failed");
    // CreateDIBSection requires this before the bits are read through the pointer.
    // The flush at the top of grab() covers the PREVIOUS call's GDI work, not this
    // BitBlt, so without it the cursor backup -- and, when draw_cursor is off, which
    // is what profile/sample_hash/wait_for_change all use, the downscale itself --
    // can read the previous frame.
    GdiFlush();
}

void Capture::restore_under_cursor() {
    if (!cursor_drawn_) return;
    const int w = cursor_rect_.right - cursor_rect_.left;
    const int h = cursor_rect_.bottom - cursor_rect_.top;
    if (w > 0 && h > 0 && cursor_rect_.right <= width_ && cursor_rect_.bottom <= height_ &&
        cursor_backup_.size() >= static_cast<size_t>(w) * h * 4) {
        for (int y = 0; y < h; ++y) {
            std::memcpy(pixels_ + static_cast<size_t>(cursor_rect_.top + y) * stride_ +
                            static_cast<size_t>(cursor_rect_.left) * 4,
                        cursor_backup_.data() + static_cast<size_t>(y) * w * 4,
                        static_cast<size_t>(w) * 4);
        }
    }
    cursor_drawn_ = false;
}

void Capture::draw_cursor_into_dib() {
    CURSORINFO ci{};
    ci.cbSize = sizeof(ci);
    if (!GetCursorInfo(&ci) || !(ci.flags & CURSOR_SHOWING) || !ci.hCursor) return;

    ICONINFO ii{};
    if (!GetIconInfo(ci.hCursor, &ii)) return;

    int cw = 0, ch = 0;
    BITMAP bm{};
    if (ii.hbmColor && GetObject(ii.hbmColor, sizeof(bm), &bm)) {
        cw = bm.bmWidth;
        ch = bm.bmHeight;
    } else if (ii.hbmMask && GetObject(ii.hbmMask, sizeof(bm), &bm)) {
        // Monochrome cursors pack AND and XOR masks into one double-height bitmap.
        cw = bm.bmWidth;
        ch = bm.bmHeight / 2;
    }
    const int x = ci.ptScreenPos.x - monitor_.rect.left - static_cast<int>(ii.xHotspot);
    const int y = ci.ptScreenPos.y - monitor_.rect.top - static_cast<int>(ii.yHotspot);
    if (ii.hbmColor) DeleteObject(ii.hbmColor);
    if (ii.hbmMask) DeleteObject(ii.hbmMask);
    if (cw <= 0 || ch <= 0) return;

    // Clip to the surface, then stash exactly the pixels DrawIconEx will touch so a
    // later reused frame can be un-drawn without re-copying the whole surface.
    RECT r{(std::max)(0, x), (std::max)(0, y), (std::min)(width_, x + cw),
           (std::min)(height_, y + ch)};
    if (r.right <= r.left || r.bottom <= r.top) return;

    const int w = r.right - r.left;
    const int h = r.bottom - r.top;
    cursor_backup_.resize(static_cast<size_t>(w) * h * 4);
    for (int row = 0; row < h; ++row) {
        std::memcpy(cursor_backup_.data() + static_cast<size_t>(row) * w * 4,
                    pixels_ + static_cast<size_t>(r.top + row) * stride_ +
                        static_cast<size_t>(r.left) * 4,
                    static_cast<size_t>(w) * 4);
    }
    cursor_rect_ = r;

    if (DrawIconEx(mem_dc_, x, y, ci.hCursor, 0, 0, 0, nullptr, DI_NORMAL)) {
        cursor_drawn_ = true;
    }
    // Flushed by the next grab() before it touches pixels_, so a failed DrawIconEx
    // that still dirtied the surface cannot race a later pointer read.
    GdiFlush();
}

FrameView Capture::grab(bool draw_cursor, int timeout_ms) {
    ensure_geometry();
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
