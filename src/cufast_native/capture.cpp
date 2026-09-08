#include "capture.h"

#include <algorithm>
#include <cstring>

namespace cufast {
namespace {

struct EnumCtx {
    std::vector<MonitorInfo>* out;
};

BOOL CALLBACK monitor_enum_proc(HMONITOR hmon, HDC, LPRECT, LPARAM lparam) {
    auto* ctx = reinterpret_cast<EnumCtx*>(lparam);
    MONITORINFOEXW mi{};
    mi.cbSize = sizeof(mi);
    if (GetMonitorInfoW(hmon, &mi)) {
        MonitorInfo info;
        info.device_name = mi.szDevice;
        info.rect = mi.rcMonitor;
        info.is_primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
        ctx->out->push_back(std::move(info));
    }
    return TRUE;
}

}  // namespace

std::vector<MonitorInfo> enumerate_monitors() {
    std::vector<MonitorInfo> monitors;
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
    init_dib(monitor_.width(), monitor_.height());
    dxgi_ready_ = init_dxgi();  // GDI fallback is used when this returns false
}

Capture::~Capture() {
    teardown_dxgi();
    if (mem_dc_) {
        if (old_bitmap_) SelectObject(mem_dc_, old_bitmap_);
        DeleteDC(mem_dc_);
    }
    if (dib_) DeleteObject(dib_);
}

void Capture::init_dib(int width, int height) {
    if (width <= 0 || height <= 0) throw Error("display has zero area");
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

bool Capture::init_dxgi() {
    teardown_dxgi();
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(factory.put())))) return false;

    ComPtr<IDXGIAdapter1> found_adapter;
    ComPtr<IDXGIOutput> found_output;
    for (UINT ai = 0; !found_output; ++ai) {
        ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(ai, adapter.put()) == DXGI_ERROR_NOT_FOUND) break;
        for (UINT oi = 0;; ++oi) {
            ComPtr<IDXGIOutput> output;
            if (adapter->EnumOutputs(oi, output.put()) == DXGI_ERROR_NOT_FOUND) break;
            DXGI_OUTPUT_DESC desc{};
            if (FAILED(output->GetDesc(&desc))) continue;
            if (monitor_.device_name == desc.DeviceName) {
                // Duplication hands back the unrotated panel surface. Rotating it
                // correctly costs more than it saves, so let GDI -- which always
                // reports the composed, oriented desktop -- handle rotated panels.
                if (desc.Rotation != DXGI_MODE_ROTATION_IDENTITY &&
                    desc.Rotation != DXGI_MODE_ROTATION_UNSPECIFIED) {
                    return false;
                }
                found_adapter = adapter;
                found_output = output;
                break;
            }
        }
    }
    if (!found_output) return false;

    D3D_FEATURE_LEVEL got{};
    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0,
                                        D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
    // D3D_DRIVER_TYPE_UNKNOWN is required when an explicit adapter is supplied.
    HRESULT hr = D3D11CreateDevice(found_adapter.get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                                   D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, ARRAYSIZE(levels),
                                   D3D11_SDK_VERSION, device_.put(), &got, context_.put());
    if (FAILED(hr)) return false;

    ComPtr<IDXGIOutput1> output1;
    if (FAILED(found_output->QueryInterface(IID_PPV_ARGS(output1.put())))) return false;

    // E_ACCESSDENIED here means a secure desktop (UAC prompt, lock screen) owns the
    // session; DXGI_ERROR_UNSUPPORTED means the driver refuses duplication.
    if (FAILED(output1->DuplicateOutput(device_.get(), dupl_.put()))) return false;
    return true;
}

void Capture::teardown_dxgi() {
    if (dupl_ && holding_frame_) dupl_->ReleaseFrame();
    holding_frame_ = false;
    staging_.reset();
    dupl_.reset();
    context_.reset();
    device_.reset();
    dxgi_ready_ = false;
}

bool Capture::grab_dxgi(int timeout_ms) {
    for (int attempt = 0; attempt < 2; ++attempt) {
        if (!dupl_) return false;

        if (holding_frame_) {
            dupl_->ReleaseFrame();
            holding_frame_ = false;
        }

        DXGI_OUTDUPL_FRAME_INFO info{};
        ComPtr<IDXGIResource> resource;
        HRESULT hr = dupl_->AcquireNextFrame(static_cast<UINT>(timeout_ms), &info, resource.put());

        if (hr == DXGI_ERROR_WAIT_TIMEOUT) return false;  // screen idle; cached frame stands
        if (hr == DXGI_ERROR_ACCESS_LOST || hr == DXGI_ERROR_INVALID_CALL) {
            // Mode change, desktop switch, or another client took over duplication.
            dxgi_ready_ = init_dxgi();
            if (!dxgi_ready_) return false;
            continue;
        }
        if (FAILED(hr)) return false;

        holding_frame_ = true;

        // LastPresentTime == 0 means only the pointer moved, so the desktop image is
        // unchanged -- except on the very first grab, where we have nothing cached.
        if (info.LastPresentTime.QuadPart == 0 && have_frame_) {
            dupl_->ReleaseFrame();
            holding_frame_ = false;
            return false;
        }

        bool copied = false;
        ComPtr<ID3D11Texture2D> texture;
        if (SUCCEEDED(resource->QueryInterface(IID_PPV_ARGS(texture.put())))) {
            D3D11_TEXTURE2D_DESC td{};
            texture->GetDesc(&td);

            D3D11_TEXTURE2D_DESC sd{};
            if (staging_) staging_->GetDesc(&sd);
            if (!staging_ || sd.Width != td.Width || sd.Height != td.Height ||
                sd.Format != td.Format) {
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
                if (FAILED(device_->CreateTexture2D(&want, nullptr, staging_.put()))) {
                    dupl_->ReleaseFrame();
                    holding_frame_ = false;
                    return false;
                }
            }

            context_->CopyResource(staging_.get(), texture.get());

            D3D11_MAPPED_SUBRESOURCE mapped{};
            if (SUCCEEDED(context_->Map(staging_.get(), 0, D3D11_MAP_READ, 0, &mapped))) {
                // Duplication yields B8G8R8A8_UNORM, matching the DIB byte order.
                const int rows = (std::min)(height_, static_cast<int>(td.Height));
                const int row_bytes = (std::min)(stride_, static_cast<int>(td.Width) * 4);
                const auto* src = static_cast<const uint8_t*>(mapped.pData);
                for (int y = 0; y < rows; ++y) {
                    std::memcpy(pixels_ + static_cast<size_t>(y) * stride_,
                                src + static_cast<size_t>(y) * mapped.RowPitch, row_bytes);
                }
                context_->Unmap(staging_.get(), 0);
                copied = true;
            }
        }

        dupl_->ReleaseFrame();
        holding_frame_ = false;
        return copied;
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
    GdiFlush();
}

void Capture::restore_under_cursor() {
    if (!cursor_drawn_) return;
    const int w = cursor_rect_.right - cursor_rect_.left;
    const int h = cursor_rect_.bottom - cursor_rect_.top;
    if (w > 0 && h > 0 && cursor_backup_.size() >= static_cast<size_t>(w) * h * 4) {
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
        GdiFlush();  // required before reading DIB bits back through the pointer
        cursor_drawn_ = true;
    }
}

FrameView Capture::grab(bool draw_cursor, int timeout_ms) {
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

    if (fresh) {
        // A fresh frame overwrote everything, including the last cursor composite.
        cursor_drawn_ = false;
        have_frame_ = true;
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
