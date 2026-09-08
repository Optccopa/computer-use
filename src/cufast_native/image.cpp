#include "image.h"

#include <immintrin.h>
#include <objbase.h>
#include <wincodec.h>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace cufast {
namespace {

// WIC needs COM on the calling thread. We deliberately never CoUninitialize:
// the factory is cached thread_local and would otherwise be released against a
// torn-down apartment. RPC_E_CHANGED_MODE means COM is already up as an STA,
// which WIC is equally happy with.
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

// Horizontal box average for the case where every destination pixel spans at most
// two source pixels -- that is, any downscale between 0.5x and 1.0x, which covers
// both recommended screenshot sizes on a 1080p display.
//
// For a two-pixel box the box average is (a + b + 1) >> 1 per channel, which is
// precisely _mm256_avg_epu8. For a one-pixel box the second index is made equal to
// the first, and (p + p + 1) >> 1 == p, so both spans run through the same
// instruction with no branch. The scalar path computes bit-identical results.
//
// Writes up to 4 bytes past the last destination pixel, so dst must carry 16 bytes
// of slack. Rows are filled in increasing order, so the spill from one row is
// overwritten by the next; only the final row needs real padding.
void horiz_avg2_avx2(const uint8_t* src_row, uint8_t* dst_row, int dw, const int32_t* idx_lo,
                     const int32_t* idx_hi) {
    // Packs 4 BGRA dwords per 128-bit lane down to 12 bytes of BGR.
    const __m256i pack = _mm256_setr_epi8(0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, -1, -1, -1, -1,
                                          0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, -1, -1, -1, -1);
    const auto* base = reinterpret_cast<const int*>(src_row);

    int x = 0;
    for (; x + 8 <= dw; x += 8) {
        const __m256i i0 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(idx_lo + x));
        const __m256i i1 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(idx_hi + x));
        const __m256i p0 = _mm256_i32gather_epi32(base, i0, 4);
        const __m256i p1 = _mm256_i32gather_epi32(base, i1, 4);
        const __m256i packed = _mm256_shuffle_epi8(_mm256_avg_epu8(p0, p1), pack);

        uint8_t* out = dst_row + static_cast<size_t>(x) * 3;
        _mm_storeu_si128(reinterpret_cast<__m128i*>(out), _mm256_castsi256_si128(packed));
        _mm_storeu_si128(reinterpret_cast<__m128i*>(out + 12),
                         _mm256_extracti128_si256(packed, 1));
    }
    for (; x < dw; ++x) {
        const uint8_t* a = src_row + static_cast<size_t>(idx_lo[x]) * 4;
        const uint8_t* b = src_row + static_cast<size_t>(idx_hi[x]) * 4;
        dst_row[x * 3 + 0] = static_cast<uint8_t>((a[0] + b[0] + 1) >> 1);
        dst_row[x * 3 + 1] = static_cast<uint8_t>((a[1] + b[1] + 1) >> 1);
        dst_row[x * 3 + 2] = static_cast<uint8_t>((a[2] + b[2] + 1) >> 1);
    }
}

// Vertical average of exactly two rows, the counterpart of the above for the same
// scale range. Contiguous, so no gathers are involved.
void vert_avg2_avx2(const uint8_t* row_a, const uint8_t* row_b, uint8_t* dst, size_t bytes) {
    size_t i = 0;
    for (; i + 32 <= bytes; i += 32) {
        const __m256i a = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(row_a + i));
        const __m256i b = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(row_b + i));
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(dst + i), _mm256_avg_epu8(a, b));
    }
    for (; i < bytes; ++i) dst[i] = static_cast<uint8_t>((row_a[i] + row_b[i] + 1) >> 1);
}

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

    if (quality && props) {
        // PROPBAG2::pstrName is LPOLESTR (non-const), so the name must be writable.
        wchar_t name[] = L"ImageQuality";
        PROPBAG2 option{};
        option.pstrName = name;
        VARIANT value{};
        value.vt = VT_R4;
        value.fltVal = *quality;
        props->Write(1, &option, &value);
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
    hr_check(frame->WritePixels(static_cast<UINT>(h), stride, total,
                                const_cast<BYTE*>(bgr)),
             "WritePixels");
    hr_check(frame->Commit(), "frame Commit");
    hr_check(encoder->Commit(), "encoder Commit");

    // GlobalSize reports the allocation, which overshoots; Stat reports what was
    // actually written.
    STATSTG stat{};
    hr_check(stream->Stat(&stat, STATFLAG_NONAME), "stream Stat");
    const size_t size = static_cast<size_t>(stat.cbSize.QuadPart);

    HGLOBAL handle = nullptr;
    hr_check(GetHGlobalFromStream(stream.get(), &handle), "GetHGlobalFromStream");
    void* mapped = GlobalLock(handle);
    if (!mapped) throw Error("GlobalLock failed");
    std::vector<uint8_t> out(static_cast<const uint8_t*>(mapped),
                             static_cast<const uint8_t*>(mapped) + size);
    GlobalUnlock(handle);
    return out;
}

}  // namespace

ScalePlan plan_fit(int src_w, int src_h, int max_w, int max_h, bool allow_upscale) {
    if (src_w <= 0 || src_h <= 0) throw Error("plan_fit: empty source region");
    if (max_w <= 0 || max_h <= 0) throw Error("plan_fit: empty target box");

    double scale = (std::min)(static_cast<double>(max_w) / src_w,
                              static_cast<double>(max_h) / src_h);
    if (!allow_upscale) scale = (std::min)(scale, 1.0);

    ScalePlan plan;
    plan.src_w = src_w;
    plan.src_h = src_h;
    plan.dst_w = (std::max)(1, static_cast<int>(std::lround(src_w * scale)));
    plan.dst_h = (std::max)(1, static_cast<int>(std::lround(src_h * scale)));
    // Rounding both axes independently can overshoot the box by a pixel.
    plan.dst_w = (std::min)(plan.dst_w, max_w);
    plan.dst_h = (std::min)(plan.dst_h, max_h);
    return plan;
}

void downscale_bgra_to_bgr(const FrameView& frame, const ScalePlan& plan,
                           std::vector<uint8_t>& out, std::vector<uint8_t>& scratch) {
    if (!frame.pixels) throw Error("downscale: no frame");
    const int sx = plan.src_x, sy = plan.src_y;
    const int sw = plan.src_w, sh = plan.src_h;
    const int dw = plan.dst_w, dh = plan.dst_h;

    if (sw <= 0 || sh <= 0 || dw <= 0 || dh <= 0) throw Error("downscale: empty region");
    if (sx < 0 || sy < 0 || sx + sw > frame.width || sy + sh > frame.height) {
        throw Error("downscale: region outside frame");
    }

    // Per-destination-column source spans, plus a fixed-point reciprocal of each
    // span so the inner loops multiply-and-shift instead of dividing.
    std::vector<int32_t> col_start(dw), col_last(dw), col_count(dw);
    std::vector<uint32_t> col_recip(dw);
    int max_count = 1;
    for (int x = 0; x < dw; ++x) {
        int c0 = static_cast<int>(static_cast<int64_t>(x) * sw / dw);
        int c1 = static_cast<int>(static_cast<int64_t>(x + 1) * sw / dw);
        if (c1 <= c0) c1 = c0 + 1;  // upscaling: nearest source pixel
        if (c1 > sw) c1 = sw;
        col_start[x] = sx + c0;
        col_last[x] = sx + c1 - 1;
        col_count[x] = c1 - c0;
        col_recip[x] = 65536u / static_cast<uint32_t>(c1 - c0);
        max_count = (std::max)(max_count, col_count[x]);
    }

    // Pass 1: horizontal average, BGRA -> BGR, into a dw x sh scratch buffer.
    // The SIMD kernel spills up to 4 bytes past each row, so carry 16 bytes of slack.
    const size_t scratch_row = static_cast<size_t>(dw) * 3;
    scratch.resize(scratch_row * sh + 16);
    const bool narrow = max_count <= 2;
    for (int y = 0; y < sh; ++y) {
        const uint8_t* src_row = frame.pixels + static_cast<size_t>(sy + y) * frame.stride;
        uint8_t* dst_row = scratch.data() + static_cast<size_t>(y) * scratch_row;

        if (narrow) {
            horiz_avg2_avx2(src_row, dst_row, dw, col_start.data(), col_last.data());
            continue;
        }
        // General path: reductions of 3 or more source pixels. The inner loop is
        // long enough here that the compiler vectorizes it on its own.
        for (int x = 0; x < dw; ++x) {
            const uint8_t* p = src_row + static_cast<size_t>(col_start[x]) * 4;
            const int n = col_count[x];
            uint32_t b = 0, g = 0, r = 0;
            for (int i = 0; i < n; ++i, p += 4) {
                b += p[0];
                g += p[1];
                r += p[2];  // p[3] is alpha, discarded
            }
            const uint32_t recip = col_recip[x];
            dst_row[x * 3 + 0] = static_cast<uint8_t>((b * recip + 32768u) >> 16);
            dst_row[x * 3 + 1] = static_cast<uint8_t>((g * recip + 32768u) >> 16);
            dst_row[x * 3 + 2] = static_cast<uint8_t>((r * recip + 32768u) >> 16);
        }
    }

    // Pass 2: vertical average over whole rows. The accumulate loop is contiguous
    // uint8 -> uint32 over dw*3 bytes, which MSVC vectorizes under /arch:AVX2.
    const size_t row_bytes = static_cast<size_t>(dw) * 3;
    out.resize(row_bytes * dh);
    std::vector<uint32_t> acc(row_bytes);

    for (int y = 0; y < dh; ++y) {
        int r0 = static_cast<int>(static_cast<int64_t>(y) * sh / dh);
        int r1 = static_cast<int>(static_cast<int64_t>(y + 1) * sh / dh);
        if (r1 <= r0) r1 = r0 + 1;
        if (r1 > sh) r1 = sh;
        const int n = r1 - r0;
        uint8_t* dst_row = out.data() + static_cast<size_t>(y) * row_bytes;

        if (n == 1) {
            std::memcpy(dst_row, scratch.data() + static_cast<size_t>(r0) * row_bytes, row_bytes);
            continue;
        }
        if (n == 2) {  // the 0.5x-1.0x range again, contiguous this time
            vert_avg2_avx2(scratch.data() + static_cast<size_t>(r0) * row_bytes,
                           scratch.data() + static_cast<size_t>(r0 + 1) * row_bytes, dst_row,
                           row_bytes);
            continue;
        }
        std::fill(acc.begin(), acc.end(), 0u);
        for (int ry = r0; ry < r1; ++ry) {
            const uint8_t* s = scratch.data() + static_cast<size_t>(ry) * row_bytes;
            for (size_t i = 0; i < row_bytes; ++i) acc[i] += s[i];
        }
        const uint32_t recip = 65536u / static_cast<uint32_t>(n);
        for (size_t i = 0; i < row_bytes; ++i) {
            dst_row[i] = static_cast<uint8_t>((acc[i] * recip + 32768u) >> 16);
        }
    }
}

std::vector<uint8_t> encode_jpeg(const uint8_t* bgr, int w, int h, float quality) {
    const float q = (std::min)(1.0f, (std::max)(0.01f, quality));
    return encode_wic(bgr, w, h, GUID_ContainerFormatJpeg, &q);
}

std::vector<uint8_t> encode_png(const uint8_t* bgr, int w, int h) {
    return encode_wic(bgr, w, h, GUID_ContainerFormatPng, nullptr);
}

uint64_t hash_bgr(const uint8_t* data, size_t len) {
    // FNV-1a over 8-byte words, tail handled bytewise. Only ever compared against
    // another hash from this same build, so portability of the constant is moot.
    uint64_t h = 1469598103934665603ull;
    const uint64_t prime = 1099511628211ull;
    size_t i = 0;
    for (; i + 8 <= len; i += 8) {
        uint64_t word;
        std::memcpy(&word, data + i, 8);
        h = (h ^ word) * prime;
    }
    for (; i < len; ++i) h = (h ^ data[i]) * prime;
    return h;
}

}  // namespace cufast
