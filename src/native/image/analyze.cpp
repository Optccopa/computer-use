// Measuring what moved between two frames, and cheap frame identity.
#include "image/image.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace cufast {

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
