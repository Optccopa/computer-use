// shot, watch and bench: the commands that actually pull frames.

#include <chrono>
#include <cstdio>
#include <string>
#include <vector>

#include <fcntl.h>
#include <io.h>

#include "capture/screen.hpp"
#include "cli/commands.hpp"
#include "core/common.hpp"

namespace cufast::cli {
namespace {

// Parses "W,H" or "X,Y,W,H". Returns false when the shape is wrong, so the caller
// can say which flag was malformed rather than proceeding with zeros.
bool parse_ints(const std::string& text, std::vector<int>& out, size_t want) {
    out.clear();
    size_t start = 0;
    while (start <= text.size()) {
        const size_t comma = text.find(',', start);
        const std::string piece = text.substr(start, comma - start);
        if (piece.empty()) return false;
        try {
            out.push_back(std::stoi(piece));
        } catch (...) {
            return false;
        }
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    return out.size() == want;
}

void write_file(const std::string& path, const std::vector<uint8_t>& bytes) {
    if (path == "-") {
        // Binary, or Windows turns every 0x0A in the JPEG into 0x0D 0x0A and the
        // file that comes out the other end is corrupt in a way that looks like an
        // encoder bug.
        _setmode(_fileno(stdout), _O_BINARY);
        std::fwrite(bytes.data(), 1, bytes.size(), stdout);
        std::fflush(stdout);
        return;
    }
    FILE* f = nullptr;
    if (fopen_s(&f, path.c_str(), "wb") != 0 || f == nullptr) {
        throw Error("could not open " + path + " for writing");
    }
    const size_t wrote = std::fwrite(bytes.data(), 1, bytes.size(), f);
    std::fclose(f);
    if (wrote != bytes.size()) throw Error("short write to " + path);
}

}  // namespace

int cmd_shot(const Args& args) {
    const int index = option_int(args, "--display", 0);
    const bool png = flag(args, "--png");
    const bool cursor = !flag(args, "--no-cursor");
    const std::string out = option(args, "--out", png ? "cufast.png" : "cufast.jpg");

    Screen screen(index);

    int max_w = screen.width();
    int max_h = screen.height();
    const std::string box = option(args, "--max", "");
    if (!box.empty()) {
        std::vector<int> v;
        if (!parse_ints(box, v, 2)) throw Error("--max expects W,H");
        max_w = v[0];
        max_h = v[1];
    }

    int rx = 0, ry = 0, rw = -1, rh = -1;
    const std::string region = option(args, "--region", "");
    if (!region.empty()) {
        std::vector<int> v;
        if (!parse_ints(region, v, 4)) throw Error("--region expects X,Y,W,H");
        rx = v[0];
        ry = v[1];
        rw = v[2];
        rh = v[3];
    }

    const Shot shot = screen.grab(max_w, max_h, 0.75f, cursor, 40, rx, ry, rw, rh, png);
    write_file(out, shot.data);
    if (out != "-") {
        std::fprintf(stderr, "%s  %dx%d  %zu bytes  via %s\n", out.c_str(), shot.width,
                     shot.height, shot.data.size(), shot.dxgi ? "DXGI" : "GDI");
    }
    return 0;
}

int cmd_watch(const Args& args) {
    const int index = option_int(args, "--display", 0);
    const int seconds = option_int(args, "--timeout", 10);

    Screen screen(index);
    // Blocks in the driver until the compositor presents, so this costs nothing
    // while nothing is happening and returns the instant something does.
    const double ms = screen.wait_for_change(static_cast<double>(seconds), 160, 90);
    if (ms < 0) {
        std::printf("no change within %ds\n", seconds);
        return 1;
    }
    std::printf("changed after %.0f ms\n", ms);
    return 0;
}

int cmd_bench(const Args& args) {
    const int index = option_int(args, "--display", 0);
    const int frames = option_int(args, "--frames", 60);
    if (frames < 1) throw Error("--frames must be at least 1");

    Screen screen(index);
    // One untimed frame first: the first grab builds the D3D11 device and the
    // duplication object, and timing that would report setup as if it were steady
    // state.
    screen.grab(1024, 768, 0.75f, false, 0);

    using Clock = std::chrono::steady_clock;
    double total = 0.0, worst = 0.0;
    double best = 1e9;
    for (int i = 0; i < frames; ++i) {
        const auto t0 = Clock::now();
        screen.grab(1024, 768, 0.75f, false, 0);
        const double ms = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        total += ms;
        best = (std::min)(best, ms);
        worst = (std::max)(worst, ms);
    }
    std::printf("%d frames at 1024x768 via %s\n", frames, screen.using_dxgi() ? "DXGI" : "GDI");
    std::printf("  mean %.2f ms   best %.2f ms   worst %.2f ms\n", total / frames, best, worst);
    // timeout_ms is 0 on purpose. With a timeout, AcquireNextFrame parks in the
    // driver waiting for the compositor to present, and on an idle desktop that wait
    // IS the measurement: the first version of this passed 40 and reported 20 ms,
    // which reads as a slow pipeline rather than a fast one waiting for something to
    // happen. At 0 it takes whatever is already there and times the real work.
    std::printf("  (timeout 0, so this is copy + downscale + JPEG encode, not the\n"
                "   wait for a new frame. An idle screen reuses the cached frame;\n"
                "   move something on the display to time the full path.)\n");
    return 0;
}

}  // namespace cufast::cli
