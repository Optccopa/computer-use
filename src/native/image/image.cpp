#include "image/image.hpp"

#include <immintrin.h>
#include <intrin.h>
#include <objbase.h>
#include <wincodec.h>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace cufast {
namespace {

// Above this span the 16-bit reciprocal cannot round-trip 255 exactly (the error
// term grows with the span), so those columns divide exactly instead. Realistic
// screenshot spans are 1-4, so the division path is effectively never taken.
constexpr int kMaxReciprocalSpan = 256;

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

// Packs 4 BGRA dwords per 128-bit lane down to 12 bytes of BGR. VPSHUFB is strictly
// per-lane, so both halves carry the same mask.
inline __m256i bgr_pack_mask() {
    return _mm256_setr_epi8(0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, -1, -1, -1, -1,
                            0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, -1, -1, -1, -1);
}

// Stores 8 packed BGR pixels (24 bytes) from a shuffled register. Writes 4 bytes
// past the 24, which is why the destination carries 16 bytes of slack; rows are
// filled in increasing order so each row's spill is overwritten by the next.
inline void store_8_bgr(uint8_t* out, __m256i packed) {
    _mm_storeu_si128(reinterpret_cast<__m128i*>(out), _mm256_castsi256_si128(packed));
    _mm_storeu_si128(reinterpret_cast<__m128i*>(out + 12), _mm256_extracti128_si256(packed, 1));
}

// Identity scale: drop alpha, no averaging. Used for zoom, which captures at native
// resolution. Worth its own path because the general narrow kernel would issue two
// gathers over identical indices here, and gathers are ~24 cycles each.
void horiz_pack_avx2(const uint8_t* src_row, uint8_t* dst_row, int dw) {
    const __m256i pack = bgr_pack_mask();
    int x = 0;
    for (; x + 8 <= dw; x += 8) {
        const __m256i pixels =
            _mm256_loadu_si256(reinterpret_cast<const __m256i*>(src_row + static_cast<size_t>(x) * 4));
        store_8_bgr(dst_row + static_cast<size_t>(x) * 3, _mm256_shuffle_epi8(pixels, pack));
    }
    for (; x < dw; ++x) {
        const uint8_t* p = src_row + static_cast<size_t>(x) * 4;
        uint8_t* d = dst_row + static_cast<size_t>(x) * 3;
        d[0] = p[0];
        d[1] = p[1];
        d[2] = p[2];
    }
}

// Horizontal box average where every destination pixel spans at most two source
// pixels -- any downscale between 0.5x and 1.0x, which covers both recommended
// screenshot sizes on a 1080p display.
//
// A two-pixel box average is (a + b + 1) >> 1 per channel, which is precisely
// _mm256_avg_epu8. A one-pixel span points both indices at the same pixel, and
// (p + p + 1) >> 1 == p, so both spans run through one instruction with no branch.
// The scalar general path computes bit-identical results.
void horiz_avg2_avx2(const uint8_t* src_row, uint8_t* dst_row, int dw, const int32_t* idx_lo,
                     const int32_t* idx_hi) {
    const __m256i pack = bgr_pack_mask();
    const auto* base = reinterpret_cast<const int*>(src_row);

    int x = 0;
    for (; x + 8 <= dw; x += 8) {
        const __m256i i0 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(idx_lo + x));
        const __m256i i1 = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(idx_hi + x));
        const __m256i p0 = _mm256_i32gather_epi32(base, i0, 4);
        const __m256i p1 = _mm256_i32gather_epi32(base, i1, 4);
        store_8_bgr(dst_row + static_cast<size_t>(x) * 3,
                    _mm256_shuffle_epi8(_mm256_avg_epu8(p0, p1), pack));
    }
    for (; x < dw; ++x) {
        const uint8_t* a = src_row + static_cast<size_t>(idx_lo[x]) * 4;
        const uint8_t* b = src_row + static_cast<size_t>(idx_hi[x]) * 4;
        uint8_t* d = dst_row + static_cast<size_t>(x) * 3;
        d[0] = static_cast<uint8_t>((a[0] + b[0] + 1) >> 1);
        d[1] = static_cast<uint8_t>((a[1] + b[1] + 1) >> 1);
        d[2] = static_cast<uint8_t>((a[2] + b[2] + 1) >> 1);
    }
}

// Vertical average of exactly two rows, the counterpart of the above for the same
// scale range. Contiguous, so no gathers are involved and no spill is written.
void vert_avg2_avx2(const uint8_t* row_a, const uint8_t* row_b, uint8_t* dst, size_t bytes) {
    size_t i = 0;
    for (; i + 32 <= bytes; i += 32) {
        const __m256i a = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(row_a + i));
        const __m256i b = _mm256_loadu_si256(reinterpret_cast<const __m256i*>(row_b + i));
        _mm256_storeu_si256(reinterpret_cast<__m256i*>(dst + i), _mm256_avg_epu8(a, b));
    }
    for (; i < bytes; ++i) dst[i] = static_cast<uint8_t>((row_a[i] + row_b[i] + 1) >> 1);
}

// Rounds 65536/n to nearest rather than flooring it. Flooring biases every average
// dark, and past a span of ~145 a pure white region stops rounding back to 255.
// Returns 0 for spans too large for 16 bits of reciprocal to stay exact.
uint32_t reciprocal_for(int n) {
    if (n > kMaxReciprocalSpan) return 0;
    return (65536u + static_cast<uint32_t>(n) / 2u) / static_cast<uint32_t>(n);
}

inline uint8_t average(uint32_t sum, uint32_t recip, int n) {
    if (recip) return static_cast<uint8_t>((sum * recip + 32768u) >> 16);
    return static_cast<uint8_t>((sum + static_cast<uint32_t>(n) / 2u) / static_cast<uint32_t>(n));
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

bool cpu_supports_avx2() {
    int info[4] = {0, 0, 0, 0};
    __cpuid(info, 0);
    if (info[0] < 7) return false;
    __cpuidex(info, 7, 0);
    return (info[1] & (1 << 5)) != 0;  // EBX bit 5 = AVX2
}

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
                           std::vector<uint8_t>& out, ScaleScratch& scratch,
                           bool force_general) {
    if (!frame.pixels) throw Error("downscale: no frame");
    const int sx = plan.src_x, sy = plan.src_y;
    const int sw = plan.src_w, sh = plan.src_h;
    const int dw = plan.dst_w, dh = plan.dst_h;

    if (sw <= 0 || sh <= 0 || dw <= 0 || dh <= 0) throw Error("downscale: empty region");
    // Widened so the bounds check cannot be defeated by overflowing its own arithmetic.
    if (sx < 0 || sy < 0 || static_cast<int64_t>(sx) + sw > frame.width ||
        static_cast<int64_t>(sy) + sh > frame.height) {
        throw Error("downscale: region outside frame");
    }
    if (frame.stride < static_cast<int64_t>(frame.width) * 4) {
        throw Error("downscale: frame stride is narrower than its width");
    }

    // Column plan, rebuilt only when the geometry actually changes. A steady stream
    // of same-sized screenshots therefore allocates nothing and performs no divisions.
    if (scratch.planned_w != dw || scratch.planned_src_w != sw || scratch.planned_src_x != sx) {
        scratch.col_start.resize(dw);
        scratch.col_last.resize(dw);
        scratch.col_count.resize(dw);
        scratch.col_recip.resize(dw);
        int max_count = 1;
        for (int x = 0; x < dw; ++x) {
            int c0 = static_cast<int>(static_cast<int64_t>(x) * sw / dw);
            int c1 = static_cast<int>(static_cast<int64_t>(x + 1) * sw / dw);
            if (c1 <= c0) c1 = c0 + 1;  // upscaling: nearest source pixel
            const int count = c1 - c0;
            scratch.col_start[x] = sx + c0;
            scratch.col_last[x] = sx + c1 - 1;
            scratch.col_count[x] = count;
            scratch.col_recip[x] = reciprocal_for(count);
            max_count = (std::max)(max_count, count);
        }
        scratch.max_count = max_count;
        scratch.planned_w = dw;
        scratch.planned_src_w = sw;
        scratch.planned_src_x = sx;
    }

    // Pass 1: horizontal average, BGRA -> BGR, into a dw x sh scratch buffer.
    // The SIMD kernels spill up to 4 bytes past each row, so carry 16 bytes of slack.
    const size_t row_bytes = static_cast<size_t>(dw) * 3;
    scratch.rows.resize(row_bytes * sh + 16);
    const bool narrow = scratch.max_count <= 2 && !force_general;
    const bool identity = narrow && dw == sw;

    for (int y = 0; y < sh; ++y) {
        const uint8_t* src_row = frame.pixels + static_cast<size_t>(sy + y) * frame.stride;
        uint8_t* dst_row = scratch.rows.data() + static_cast<size_t>(y) * row_bytes;

        if (identity) {
            horiz_pack_avx2(src_row + static_cast<size_t>(sx) * 4, dst_row, dw);
            continue;
        }
        if (narrow) {
            horiz_avg2_avx2(src_row, dst_row, dw, scratch.col_start.data(),
                            scratch.col_last.data());
            continue;
        }
        // General path: spans of 3 or more. The inner loop is long enough here that
        // the compiler vectorizes it on its own.
        for (int x = 0; x < dw; ++x) {
            const uint8_t* p = src_row + static_cast<size_t>(scratch.col_start[x]) * 4;
            const int n = scratch.col_count[x];
            uint32_t b = 0, g = 0, r = 0;
            for (int i = 0; i < n; ++i, p += 4) {
                b += p[0];
                g += p[1];
                r += p[2];  // p[3] is alpha, discarded
            }
            const uint32_t recip = scratch.col_recip[x];
            uint8_t* d = dst_row + static_cast<size_t>(x) * 3;
            d[0] = average(b, recip, n);
            d[1] = average(g, recip, n);
            d[2] = average(r, recip, n);
        }
    }

    // Pass 2: vertical average over whole rows. Contiguous, so it vectorizes cleanly.
    out.resize(row_bytes * dh);
    for (int y = 0; y < dh; ++y) {
        int r0 = static_cast<int>(static_cast<int64_t>(y) * sh / dh);
        int r1 = static_cast<int>(static_cast<int64_t>(y + 1) * sh / dh);
        if (r1 <= r0) r1 = r0 + 1;
        if (r1 > sh) r1 = sh;
        const int n = r1 - r0;
        uint8_t* dst_row = out.data() + static_cast<size_t>(y) * row_bytes;

        if (n == 1) {
            std::memcpy(dst_row, scratch.rows.data() + static_cast<size_t>(r0) * row_bytes,
                        row_bytes);
            continue;
        }
        if (n == 2 && !force_general) {  // the 0.5x-1.0x range again, contiguous
            vert_avg2_avx2(scratch.rows.data() + static_cast<size_t>(r0) * row_bytes,
                           scratch.rows.data() + static_cast<size_t>(r0 + 1) * row_bytes,
                           dst_row, row_bytes);
            continue;
        }
        scratch.acc.assign(row_bytes, 0u);
        for (int ry = r0; ry < r1; ++ry) {
            const uint8_t* s = scratch.rows.data() + static_cast<size_t>(ry) * row_bytes;
            for (size_t i = 0; i < row_bytes; ++i) scratch.acc[i] += s[i];
        }
        const uint32_t recip = reciprocal_for(n);
        for (size_t i = 0; i < row_bytes; ++i) dst_row[i] = average(scratch.acc[i], recip, n);
    }
}

// 16 destination pixels is 64 bytes, exactly one cache line, so a tile row on the
// contiguous side is a single line and the strided side holds 16 lines -- 1 KiB of
// working set, comfortably resident while the tile is processed.
constexpr int kRotateTile = 16;

inline const uint32_t* row32(const uint8_t* base, int stride, int row) {
    return reinterpret_cast<const uint32_t*>(base + static_cast<size_t>(row) * stride);
}
inline uint32_t* row32(uint8_t* base, int stride, int row) {
    return reinterpret_cast<uint32_t*>(base + static_cast<size_t>(row) * stride);
}

void rotate_bgra(const uint8_t* src, int src_stride, int src_w, int src_h,
                 uint8_t* dst, int dst_stride, int quarter_turns) {
    const int turns = ((quarter_turns % 4) + 4) % 4;
    const int dst_w = (turns & 1) ? src_h : src_w;
    const int dst_h = (turns & 1) ? src_w : src_h;

    if (turns == 0) {
        const size_t bytes = static_cast<size_t>(src_w) * 4;
        for (int y = 0; y < src_h; ++y) {
            std::memcpy(dst + static_cast<size_t>(y) * dst_stride,
                        src + static_cast<size_t>(y) * src_stride, bytes);
        }
        return;
    }

    if (turns == 2) {
        // A half-turn keeps rows contiguous, so it needs no tiling: read one source
        // row backwards into one destination row.
        for (int y = 0; y < dst_h; ++y) {
            const uint32_t* in = row32(src, src_stride, src_h - 1 - y);
            uint32_t* out = row32(dst, dst_stride, y);
            for (int x = 0; x < dst_w; ++x) out[x] = in[src_w - 1 - x];
        }
        return;
    }

    // Quarter turns. Writes stay contiguous along the destination row and the reads
    // walk one source column, which is what the tiling is there to keep in cache.
    //   1 (clockwise):        dst[y][x] = src[src_h - 1 - x][y]
    //   3 (counter-clockwise): dst[y][x] = src[x][src_w - 1 - y]
    for (int y0 = 0; y0 < dst_h; y0 += kRotateTile) {
        const int y1 = std::min(y0 + kRotateTile, dst_h);
        for (int x0 = 0; x0 < dst_w; x0 += kRotateTile) {
            const int x1 = std::min(x0 + kRotateTile, dst_w);
            if (turns == 1) {
                for (int y = y0; y < y1; ++y) {
                    uint32_t* out = row32(dst, dst_stride, y) + x0;
                    for (int x = x0; x < x1; ++x) {
                        *out++ = row32(src, src_stride, src_h - 1 - x)[y];
                    }
                }
            } else {
                for (int y = y0; y < y1; ++y) {
                    uint32_t* out = row32(dst, dst_stride, y) + x0;
                    const int col = src_w - 1 - y;
                    for (int x = x0; x < x1; ++x) {
                        *out++ = row32(src, src_stride, x)[col];
                    }
                }
            }
        }
    }
}

std::vector<int32_t> column_profile(const uint8_t* bgr, int w, int h) {
    std::vector<int32_t> out(static_cast<size_t>(std::max(w, 0)), 0);
    if (w <= 0 || h <= 0) return out;

    // Middle half of the rows. On a 3D view that is the horizon band, which is where
    // the geometry that actually moves with a pan lives.
    const int y0 = h / 4;
    const int y1 = std::max(y0 + 1, h - h / 4);

    for (int y = y0; y < y1; ++y) {
        const uint8_t* row = bgr + static_cast<size_t>(y) * w * 3;
        for (int x = 0; x < w; ++x) {
            // Rec.601 luma in fixed point. The exact weights do not matter here --
            // this only has to be a stable scalar per pixel -- but green dominating
            // matches how the eye and the JPEG encoder both see the frame.
            const int b = row[x * 3 + 0];
            const int g = row[x * 3 + 1];
            const int r = row[x * 3 + 2];
            out[static_cast<size_t>(x)] += (r * 77 + g * 150 + b * 29) >> 8;
        }
    }
    return out;
}

std::vector<int32_t> row_profile(const uint8_t* bgr, int w, int h) {
    std::vector<int32_t> out(static_cast<size_t>(std::max(h, 0)), 0);
    if (w <= 0 || h <= 0) return out;

    // Middle half of the columns, mirroring column_profile's middle band of rows:
    // the edges of a 3D view are the most distorted by perspective and the least
    // representative of how far the whole image moved.
    const int x0 = w / 4;
    const int x1 = std::max(x0 + 1, w - w / 4);

    for (int y = 0; y < h; ++y) {
        const uint8_t* row = bgr + static_cast<size_t>(y) * w * 3;
        int32_t sum = 0;
        for (int x = x0; x < x1; ++x) {
            const int b = row[x * 3 + 0];
            const int g = row[x * 3 + 1];
            const int r = row[x * 3 + 2];
            sum += (r * 77 + g * 150 + b * 29) >> 8;
        }
        out[static_cast<size_t>(y)] = sum;
    }
    return out;
}

ShiftEstimate best_shift(const std::vector<int32_t>& a, const std::vector<int32_t>& b,
                         int max_shift) {
    ShiftEstimate result;
    const int n = static_cast<int>(std::min(a.size(), b.size()));
    if (n < 8 || max_shift < 1) return result;
    max_shift = std::min(max_shift, n - 4);
    if (max_shift < 1) return result;

    // First differences: a uniform brightness change adds a constant to every
    // sample, which differencing removes entirely.
    std::vector<double> da(static_cast<size_t>(n - 1)), db(static_cast<size_t>(n - 1));
    for (int i = 0; i + 1 < n; ++i) {
        da[static_cast<size_t>(i)] = static_cast<double>(a[i + 1] - a[i]);
        db[static_cast<size_t>(i)] = static_cast<double>(b[i + 1] - b[i]);
    }
    const int m = n - 1;

    double best = 0.0, total = 0.0;
    int best_shift_value = 0;
    int considered = 0;
    for (int shift = -max_shift; shift <= max_shift; ++shift) {
        // Only the overlapping span, and the score is per sample, so a large shift
        // is not rewarded for having fewer samples to disagree about.
        const int lo = std::max(0, -shift);
        const int hi = std::min(m, m - shift);
        const int count = hi - lo;
        if (count < m / 4) continue;  // too little overlap to mean anything

        double sad = 0.0;
        for (int i = lo; i < hi; ++i) {
            sad += std::fabs(da[static_cast<size_t>(i)] - db[static_cast<size_t>(i + shift)]);
        }
        sad /= count;

        if (considered == 0 || sad < best) {
            best = sad;
            best_shift_value = shift;
        }
        total += sad;
        ++considered;
    }
    if (considered == 0) return result;

    const double mean = total / considered;
    result.shift = best_shift_value;
    // How far the winner stands below the average candidate. A flat view scores the
    // same everywhere, mean and best coincide, and this lands at 0.
    result.confidence = mean > 0.0 ? std::clamp(1.0 - best / mean, 0.0, 1.0) : 0.0;
    return result;
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
