#pragma once

// One monitor's whole pipeline: capture, downscale, encode, and the blocking
// wait for something to change. Deliberately free of any binding layer, so the
// Python module and the cufast CLI drive the same object rather than two
// almost-identical ones -- and so the capture path can be exercised with
// nanobind out of the picture.

#include <cstdint>
#include <mutex>
#include <vector>

#include "capture/capture.hpp"
#include "image/image.hpp"

namespace cufast {

// One encoded frame plus everything the Python layer needs to map the model's
// coordinates back onto the desktop.
struct Shot {
    std::vector<uint8_t> data;
    int width = 0, height = 0;   // encoded image size, i.e. what the model sees
    int src_x = 0, src_y = 0;    // captured region, monitor-local
    int src_w = 0, src_h = 0;
    uint64_t frame_id = 0;
    uint64_t content_hash = 0;
    bool dxgi = false;
};

// Owns a monitor's capture pipeline and its scratch buffers, so a steady stream of
// screenshots performs no allocation after the first.

class Screen {
public:
    Screen(int display_index, bool prefer_dxgi = true);

    Shot grab(int max_w, int max_h, float quality, bool draw_cursor, int timeout_ms,
              int rx = 0, int ry = 0, int rw = -1, int rh = -1, bool png = false,
              bool allow_upscale = false);

    std::vector<int32_t> profile(int max_w, int max_h, int timeout_ms);
    std::vector<int32_t> profile_rows(int max_w, int max_h, int timeout_ms);
    double wait_for_change(double timeout_seconds, int grid_w, int grid_h);
    uint64_t sample_hash(int grid_w, int grid_h, int timeout_ms);

    // Locked like everything else that touches the capture: grab() mutates the
    // monitor record from inside ensure_geometry when the display mode changes.
    int width() const;
    int height() const;
    int origin_x() const;
    int origin_y() const;
    int index() const;
    bool using_dxgi() const;

private:
    mutable std::mutex mutex_;
    Capture capture_;
    std::vector<uint8_t> pixels_;
    ScaleScratch scratch_;
};

}  // namespace cufast
