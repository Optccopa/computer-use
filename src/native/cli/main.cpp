// cufast: drives capture and input with no Python interpreter in the picture.

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "cli/commands.hpp"
#include "core/common.hpp"
#include "image/image.hpp"
#include "input/input.hpp"

namespace {

const char* kUsage =
    "cufast -- Windows capture and input, without the Python layer\n"
    "\n"
    "  cufast displays                     list monitors and their indices\n"
    "  cufast probe                        DPI awareness, capture path, rotation, AVX2\n"
    "  cufast shot [--out FILE]            capture a display\n"
    "      --display N   which monitor (default 0)\n"
    "      --out FILE    write here (default cufast.jpg); - writes to stdout\n"
    "      --png         PNG instead of JPEG\n"
    "      --max W,H     fit into this box (default the native size)\n"
    "      --region X,Y,W,H   capture only this rectangle, monitor-local\n"
    "      --no-cursor   leave the mouse pointer out of the image\n"
    "  cufast watch [--timeout S]          block until the screen changes\n"
    "  cufast bench [--frames N]           time the capture pipeline\n"
    "  cufast input ...                    move, click, type, key -- see: cufast input\n"
    "\n"
    "Exit codes: 0 ok, 1 failed, 2 bad usage.\n";

}  // namespace

int main(int argc, char** argv) {
    // Must happen before anything measures or clicks a pixel, exactly as in the
    // Python module: without it Windows reports virtualized coordinates on a scaled
    // display and every number this prints is quietly wrong.
    cufast::enable_dpi_awareness();

    if (argc < 2) {
        std::fputs(kUsage, stderr);
        return 2;
    }

    const std::string command = argv[1];
    if (command == "-h" || command == "--help" || command == "help") {
        std::fputs(kUsage, stdout);
        return 0;
    }

    cufast::cli::Args args(argv + 2, argv + argc);

    // The whole binary is compiled with /arch:AVX2 for the downscale kernel, so a
    // CPU without it faults with an illegal instruction rather than anything
    // catchable. Checked here for the same reason the module checks it at import.
    if (!cufast::cpu_supports_avx2()) {
        std::fputs("cufast requires a CPU with AVX2 (2013 onward)\n", stderr);
        return 1;
    }

    try {
        if (command == "displays") return cufast::cli::cmd_displays(args);
        if (command == "probe") return cufast::cli::cmd_probe(args);
        if (command == "shot") return cufast::cli::cmd_shot(args);
        if (command == "watch") return cufast::cli::cmd_watch(args);
        if (command == "bench") return cufast::cli::cmd_bench(args);
        if (command == "input") return cufast::cli::cmd_input(args);
    } catch (const std::exception& e) {
        // Every failure in the core arrives as an exception carrying a message meant
        // to be read. Printing it beats a stack trace nobody can act on.
        std::fprintf(stderr, "cufast %s: %s\n", command.c_str(), e.what());
        return 1;
    }

    std::fprintf(stderr, "unknown command '%s'\n\n", command.c_str());
    std::fputs(kUsage, stderr);
    return 2;
}
