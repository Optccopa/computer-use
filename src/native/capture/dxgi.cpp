// The fast path: DXGI Desktop Duplication, kept warm across calls.
#include "capture/capture.hpp"

#include <algorithm>

#include "image/image.hpp"

namespace cufast {

bool Capture::init_dxgi() {
    if (!allow_dxgi_) return false;
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
                // Determined by experiment, not from the documentation, because
                // reasoning about which direction DXGI_MODE_ROTATION describes got
                // it exactly 180 degrees wrong: ROTATE90 and ROTATE270 were swapped.
                // The first frame after DuplicateOutput seeds from GDI, so a
                // one-shot capture looked correct while every later frame was
                // upside down -- see the DXGI-vs-GDI orientation test.
                switch (desc.Rotation) {
                    case DXGI_MODE_ROTATION_ROTATE90:  turns = 1; break;
                    case DXGI_MODE_ROTATION_ROTATE180: turns = 2; break;
                    case DXGI_MODE_ROTATION_ROTATE270: turns = 3; break;
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

        // Balances the Map even if the copy out throws. A staging texture left mapped
        // fails every later Map, so one escaping exception would break capture for the
        // life of the process rather than for one frame.
        struct MapGuard {
            ID3D11DeviceContext* ctx;
            ID3D11Texture2D* tex;
            ~MapGuard() { ctx->Unmap(tex, 0); }
        } unmap{context_.get(), staging_.get()};

        // IDXGIOutput1::DuplicateOutput always yields a 32-bit BGRA surface, which
        // matches the DIB byte for byte. rotate_bgra is a straight row copy when
        // there is no turn to apply, so the common case pays nothing for this.
        rotate_bgra(static_cast<const uint8_t*>(mapped.pData),
                    static_cast<int>(mapped.RowPitch), panel_w, panel_h,
                    pixels_, stride_, dxgi_turns_);

        // Deliberately not released here: the docs recommend holding until just
        // before the next acquire, because while the client does not own the frame
        // the OS copies every desktop update into the surface.
        return true;
    }
    return false;
}

}  // namespace cufast
