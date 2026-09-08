#pragma once

#include <cstdint>
#include <vector>

#include "common.h"

namespace cufast {

// How a source region maps onto the image the model actually sees. The Python
// layer keeps this to translate model coordinates back to desktop pixels.
struct ScalePlan {
    int src_x = 0, src_y = 0, src_w = 0, src_h = 0;
    int dst_w = 0, dst_h = 0;
};

// Fits a src_w x src_h region into a max_w x max_h box, preserving aspect ratio.
// Never upscales unless allow_upscale: the computer-use zoom action is defined as
// "full resolution, scaled to fit", so a region smaller than the box stays native.
ScalePlan plan_fit(int src_w, int src_h, int max_w, int max_h, bool allow_upscale);

// Box-filter downscale of a BGRA region straight into packed 24bpp BGR.
//
// Separable and two-pass: horizontal averages into a dst_w x src_h uint8 scratch
// (which also drops alpha, so the vertical pass moves 25% less memory), then the
// vertical pass accumulates whole rows, which vectorizes cleanly. Output is BGR
// because that is what the WIC JPEG encoder consumes natively -- no channel swap.
// force_general disables the SIMD narrow path. It exists so tests can prove the two
// paths agree bit for bit; nothing in the capture path sets it.
void downscale_bgra_to_bgr(const FrameView& frame, const ScalePlan& plan,
                           std::vector<uint8_t>& out, std::vector<uint8_t>& scratch,
                           bool force_general = false);

// quality is 0.0-1.0. Encodes packed 24bpp BGR, stride = w * 3.
std::vector<uint8_t> encode_jpeg(const uint8_t* bgr, int w, int h, float quality);
std::vector<uint8_t> encode_png(const uint8_t* bgr, int w, int h);

// 64-bit content hash of a BGR buffer, for cheap "did the screen change" checks.
uint64_t hash_bgr(const uint8_t* bgr, size_t len);

}  // namespace cufast
