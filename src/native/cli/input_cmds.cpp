// input: driving the real mouse and keyboard from the command line.
//
// This injects into whatever window has focus, which for a command line tool means
// the terminal you typed it into unless you switch away first. That is stated in the
// help rather than guarded against: the whole point is to exercise the real input
// path, and a version that refused to type into the focused window would be
// exercising something else.

#include <cstdio>
#include <string>

#include "cli/commands.hpp"
#include "core/common.hpp"
#include "input/input.hpp"

namespace cufast::cli {
namespace {

const char* kInputUsage =
    "cufast input SUBCOMMAND\n"
    "\n"
    "  move X Y              move the pointer to an absolute desktop position\n"
    "  rel DX DY [--steps N] move BY a delta -- the only form a pointer-locked\n"
    "                        game reads; see the note in input.hpp\n"
    "  click [--button left|right|middle] [--clicks N] [--mods CHORD]\n"
    "  down / up             press or release the left button\n"
    "  type TEXT             type literal text\n"
    "  key CHORD [--repeat N]  e.g. Return, ctrl+s, alt+Tab\n"
    "  cursor                print the pointer position\n"
    "  held                  list keys this process is holding down\n"
    "\n"
    "Input goes to the FOCUSED window, which is this terminal unless you switch\n"
    "away first. Windows also silently refuses injection into a window running at\n"
    "higher integrity than this process, and reports success either way.\n";

int need(const Args& args, size_t index, const char* what) {
    if (args.size() <= index) throw Error(std::string("missing ") + what);
    try {
        return std::stoi(args[index]);
    } catch (...) {
        throw Error(std::string(what) + " must be a number, got '" + args[index] + "'");
    }
}

}  // namespace

int cmd_input(const Args& args) {
    if (args.empty()) {
        std::fputs(kInputUsage, stdout);
        return 2;
    }
    const std::string sub = args[0];
    const Args rest(args.begin() + 1, args.end());

    if (sub == "cursor") {
        int x = 0, y = 0;
        get_cursor_pos(&x, &y);
        std::printf("%d %d\n", x, y);
        return 0;
    }
    if (sub == "held") {
        const auto keys = held_keys();
        if (keys.empty()) {
            std::printf("nothing held\n");
            return 0;
        }
        for (const auto& k : keys) std::printf("%s\n", k.c_str());
        return 0;
    }
    if (sub == "move") {
        mouse_move(need(rest, 0, "X"), need(rest, 1, "Y"));
        return 0;
    }
    if (sub == "rel") {
        mouse_move_relative(need(rest, 0, "DX"), need(rest, 1, "DY"),
                            option_int(rest, "--steps", 1));
        return 0;
    }
    if (sub == "click") {
        const std::string button = option(rest, "--button", "left");
        MouseButton which = MouseButton::Left;
        if (button == "right") which = MouseButton::Right;
        else if (button == "middle") which = MouseButton::Middle;
        else if (button != "left") throw Error("--button must be left, right or middle");
        mouse_click(which, option_int(rest, "--clicks", 1), option(rest, "--mods", ""));
        return 0;
    }
    if (sub == "down") {
        mouse_down(MouseButton::Left);
        return 0;
    }
    if (sub == "up") {
        mouse_up(MouseButton::Left);
        return 0;
    }
    if (sub == "type") {
        if (rest.empty()) throw Error("missing TEXT");
        type_text(rest[0]);
        return 0;
    }
    if (sub == "key") {
        if (rest.empty()) throw Error("missing CHORD");
        press_key(rest[0], option_int(rest, "--repeat", 1));
        return 0;
    }

    std::fprintf(stderr, "unknown input subcommand '%s'\n\n", sub.c_str());
    std::fputs(kInputUsage, stderr);
    return 2;
}

}  // namespace cufast::cli
