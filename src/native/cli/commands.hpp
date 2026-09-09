#pragma once

// The CLI's subcommands. Each takes the arguments after the subcommand name and
// returns a process exit code: 0 for success, 1 for a failure the user should read,
// 2 for a usage mistake.
//
// The CLI exists for two reasons, and only one of them is speed. Every native test
// in this project runs through nanobind, so a bug in the binding and a bug in the
// capture look identical from the test's side -- the same problem the DXGI rotation
// bug hid behind for days. A binary that calls Capture and SendInput directly is a
// second, independent path to the same answers. The other reason is that a broken
// Python environment is exactly when you want diagnostics to still work.

#include <string>
#include <vector>

namespace cufast::cli {

using Args = std::vector<std::string>;

int cmd_displays(const Args& args);
int cmd_probe(const Args& args);
int cmd_shot(const Args& args);
int cmd_watch(const Args& args);
int cmd_bench(const Args& args);
int cmd_input(const Args& args);
int cmd_hook(const Args& args);

// Shared argument helpers. Deliberately tiny: this parses a handful of flags, and
// pulling in a parser library for that would be more dependency than program.
bool flag(const Args& args, const std::string& name);
std::string option(const Args& args, const std::string& name, const std::string& fallback);
int option_int(const Args& args, const std::string& name, int fallback);

}  // namespace cufast::cli
