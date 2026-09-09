// Enumerating displays, in an order that means the same thing across runs.
#include "capture/capture.hpp"

#include <algorithm>

namespace cufast {
namespace {

struct EnumCtx {
    std::vector<MonitorInfo>* out;
    bool failed = false;
};

BOOL CALLBACK monitor_enum_proc(HMONITOR hmon, HDC, LPRECT, LPARAM lparam) {
    auto* ctx = reinterpret_cast<EnumCtx*>(lparam);
    // Never let an exception unwind through user32's enumeration frame: foreign
    // frames are not guaranteed unwindable and its internal state would be left
    // inconsistent.
    try {
        MONITORINFOEXW mi{};
        mi.cbSize = sizeof(mi);
        if (GetMonitorInfoW(hmon, &mi)) {
            MonitorInfo info;
            info.device_name = mi.szDevice;
            info.rect = mi.rcMonitor;
            info.is_primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
            ctx->out->push_back(std::move(info));
        } else {
            ctx->failed = true;
        }
    } catch (...) {
        ctx->failed = true;
        return FALSE;
    }
    return TRUE;
}

}  // namespace

std::vector<MonitorInfo> enumerate_monitors() {
    std::vector<MonitorInfo> monitors;
    monitors.reserve(8);  // so the callback does not allocate mid-enumeration
    EnumCtx ctx{&monitors};
    EnumDisplayMonitors(nullptr, nullptr, monitor_enum_proc, reinterpret_cast<LPARAM>(&ctx));
    if (monitors.empty()) throw Error("no displays found");

    // Stable ordering so display_index means the same thing across runs:
    // primary first, then left-to-right, top-to-bottom.
    std::sort(monitors.begin(), monitors.end(), [](const MonitorInfo& a, const MonitorInfo& b) {
        if (a.is_primary != b.is_primary) return a.is_primary;
        if (a.rect.left != b.rect.left) return a.rect.left < b.rect.left;
        return a.rect.top < b.rect.top;
    });
    for (size_t i = 0; i < monitors.size(); ++i) monitors[i].index = static_cast<int>(i);
    return monitors;
}

}  // namespace cufast
