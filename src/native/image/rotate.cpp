// Cache-blocked quarter turns, so a rotated panel stays on the fast path.
#include "image/image.hpp"

#include <algorithm>
#include <cstring>

namespace cufast {
namespace {

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

}  // namespace

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

}  // namespace cufast
