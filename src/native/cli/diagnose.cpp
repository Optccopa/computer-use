// displays and probe: the two commands that answer "what is actually going on".
//
// Both exist because of bugs that were invisible until something printed the real
// state. The rotation was wrong by a quarter turn for days; "display 2" in Windows
// is index 1 here; and per-monitor DPI awareness failing turns every coordinate
// silently wrong rather than visibly broken.

#include <cstdio>
#include <string>

#include "capture/capture.hpp"
#include "capture/screen.hpp"
#include "cli/commands.hpp"
#include "image/image.hpp"
#include "input/input.hpp"

namespace cufast::cli {
namespace {

std::string narrow(const std::wstring& wide) {
    // Device names are ASCII (L"\\\\.\\DISPLAY1"), so this narrowing is safe.
    return std::string(wide.begin(), wide.end());
}

}  // namespace

int cmd_displays(const Args&) {
    const auto monitors = enumerate_monitors();
    std::printf("%-6s %-14s %-12s %-16s %s\n",
                "index", "device", "size", "origin", "notes");
    for (const auto& m : monitors) {
        char size[32];
        std::snprintf(size, sizeof(size), "%dx%d", m.width(), m.height());
        char origin[32];
        std::snprintf(origin, sizeof(origin), "(%ld,%ld)", m.rect.left, m.rect.top);
        std::printf("%-6d %-14s %-12s %-16s %s\n", m.index, narrow(m.device_name).c_str(),
                    size, origin, m.is_primary ? "primary" : "");
    }
    // The off-by-one that keeps costing people a wrong click.
    std::printf("\nWindows numbers displays from 1; these indices start at 0, so "
                "Windows DISPLAY2 is index 1.\n");
    return 0;
}

int cmd_probe(const Args& args) {
    const int index = option_int(args, "--display", 0);

    std::printf("AVX2                 %s\n", cpu_supports_avx2() ? "yes" : "NO");
    std::printf("per-monitor DPI      %s\n", dpi_per_monitor_aware() ? "yes" : "NO");
    if (!dpi_per_monitor_aware()) {
        std::printf("  WARNING: coordinates are virtualized on a scaled display, so every\n"
                    "  number below is in the wrong units.\n");
    }

    Screen screen(index);
    std::printf("display %d            %dx%d at (%d,%d)\n", screen.index(),
                screen.width(), screen.height(), screen.origin_x(), screen.origin_y());
    std::printf("capture path         %s\n",
                screen.using_dxgi() ? "DXGI Desktop Duplication" : "GDI BitBlt (fallback)");

    // Duplication hands back the physical panel, so on a rotated monitor the copy
    // out has to apply a quarter turn. Getting that backwards produced an upside
    // down image that still looked like a plausible screenshot, which is exactly
    // the kind of thing only a direct comparison catches.
    const Shot fast = screen.grab(320, 320, 0.8f, false, 40);
    Screen reference(index, /*prefer_dxgi=*/false);
    const Shot slow = reference.grab(320, 320, 0.8f, false, 40);
    std::printf("orientation          ");
    if (!screen.using_dxgi()) {
        std::printf("both paths are GDI here, nothing to compare\n");
    } else if (fast.width != slow.width || fast.height != slow.height) {
        std::printf("MISMATCH: duplication is %dx%d, GDI is %dx%d\n",
                    fast.width, fast.height, slow.width, slow.height);
    } else {
        std::printf("duplication and GDI agree on %dx%d\n", fast.width, fast.height);
    }
    std::printf("screenshot size      %dx%d\n", fast.width, fast.height);
    return 0;
}

}  // namespace cufast::cli
