// JPEG and PNG encoding through Windows Imaging Component.
#include "image/image.hpp"

#include <objbase.h>
#include <wincodec.h>

namespace cufast {
namespace {

// WIC needs COM on the calling thread. CoUninitialize is deliberately never called:
// the RPC_E_CHANGED_MODE branch means COM was already initialised by someone else as
// an STA, and this guard does not record whether it was the one that initialised it,
// so it is not entitled to tear it down. The factory is released at thread exit by
// its own ComPtr, which is declared after the guard and so destroyed before it.
struct ComGuard {
    ComGuard() {
        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) hr_check(hr, "CoInitializeEx");
    }
};

IWICImagingFactory* wic_factory() {
    thread_local ComGuard guard;
    thread_local ComPtr<IWICImagingFactory> factory;
    if (!factory) {
        hr_check(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_PPV_ARGS(factory.put())),
                 "CoCreateInstance(WICImagingFactory)");
    }
    return factory.get();
}

// Balances GlobalLock even if the copy out of the mapped block throws.
class GlobalLockGuard {
public:
    explicit GlobalLockGuard(HGLOBAL handle) : handle_(handle), data_(GlobalLock(handle)) {
        if (!data_) throw Error("GlobalLock failed");
    }
    ~GlobalLockGuard() { GlobalUnlock(handle_); }
    GlobalLockGuard(const GlobalLockGuard&) = delete;
    GlobalLockGuard& operator=(const GlobalLockGuard&) = delete;
    const uint8_t* data() const { return static_cast<const uint8_t*>(data_); }

private:
    HGLOBAL handle_;
    void* data_;
};

std::vector<uint8_t> encode_wic(const uint8_t* bgr, int w, int h, const GUID& container,
                                const float* quality) {
    if (!bgr || w <= 0 || h <= 0) throw Error("encode: empty image");

    IWICImagingFactory* factory = wic_factory();

    ComPtr<IStream> stream;
    hr_check(CreateStreamOnHGlobal(nullptr, TRUE, stream.put()), "CreateStreamOnHGlobal");

    ComPtr<IWICBitmapEncoder> encoder;
    hr_check(factory->CreateEncoder(container, nullptr, encoder.put()), "CreateEncoder");
    hr_check(encoder->Initialize(stream.get(), WICBitmapEncoderNoCache), "encoder Initialize");

    ComPtr<IWICBitmapFrameEncode> frame;
    ComPtr<IPropertyBag2> props;
    hr_check(encoder->CreateNewFrame(frame.put(), props.put()), "CreateNewFrame");

    if (quality) {
        if (!props) throw Error("JPEG encoder exposed no property bag for ImageQuality");
        // PROPBAG2::pstrName is LPOLESTR (non-const), so the name must be writable.
        wchar_t name[] = L"ImageQuality";
        PROPBAG2 option{};
        option.pstrName = name;
        VARIANT value{};
        value.vt = VT_R4;
        value.fltVal = *quality;
        // Ignoring this would silently fall back to WIC's default quality of 0.9 and
        // make every screenshot materially larger than configured, with no error.
        hr_check(props->Write(1, &option, &value), "set ImageQuality");
    }
    hr_check(frame->Initialize(props.get()), "frame Initialize");
    hr_check(frame->SetSize(static_cast<UINT>(w), static_cast<UINT>(h)), "SetSize");

    WICPixelFormatGUID format = GUID_WICPixelFormat24bppBGR;
    hr_check(frame->SetPixelFormat(&format), "SetPixelFormat");
    if (!IsEqualGUID(format, GUID_WICPixelFormat24bppBGR)) {
        // Both the JPEG and PNG encoders accept 24bppBGR, so this should not happen;
        // failing loudly beats silently writing channel-swapped pixels.
        throw Error("WIC encoder refused 24bppBGR");
    }

    const UINT stride = static_cast<UINT>(w) * 3;
    const UINT total = stride * static_cast<UINT>(h);
    hr_check(frame->WritePixels(static_cast<UINT>(h), stride, total, const_cast<BYTE*>(bgr)),
             "WritePixels");
    hr_check(frame->Commit(), "frame Commit");
    hr_check(encoder->Commit(), "encoder Commit");

    // GlobalSize reports the page-rounded allocation; Stat reports bytes written.
    STATSTG stat{};
    hr_check(stream->Stat(&stat, STATFLAG_NONAME), "stream Stat");
    const size_t size = static_cast<size_t>(stat.cbSize.QuadPart);

    HGLOBAL handle = nullptr;
    hr_check(GetHGlobalFromStream(stream.get(), &handle), "GetHGlobalFromStream");
    GlobalLockGuard locked(handle);
    return std::vector<uint8_t>(locked.data(), locked.data() + size);
}

}  // namespace

std::vector<uint8_t> encode_jpeg(const uint8_t* bgr, int w, int h, float quality) {
    const float q = (std::min)(1.0f, (std::max)(0.01f, quality));
    return encode_wic(bgr, w, h, GUID_ContainerFormatJpeg, &q);
}

std::vector<uint8_t> encode_png(const uint8_t* bgr, int w, int h) {
    return encode_wic(bgr, w, h, GUID_ContainerFormatPng, nullptr);
}

}  // namespace cufast
