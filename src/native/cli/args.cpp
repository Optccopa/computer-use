// Flag parsing, kept deliberately small.

#include <cstdlib>
#include <string>

#include "cli/commands.hpp"
#include "core/common.hpp"

namespace cufast::cli {

bool flag(const Args& args, const std::string& name) {
    for (const auto& a : args) {
        if (a == name) return true;
    }
    return false;
}

std::string option(const Args& args, const std::string& name, const std::string& fallback) {
    for (size_t i = 0; i + 1 < args.size(); ++i) {
        if (args[i] == name) return args[i + 1];
    }
    // Also accept --name=value, because that is what half of everyone types.
    const std::string prefix = name + "=";
    for (const auto& a : args) {
        if (a.rfind(prefix, 0) == 0) return a.substr(prefix.size());
    }
    return fallback;
}

int option_int(const Args& args, const std::string& name, int fallback) {
    const std::string raw = option(args, name, "");
    if (raw.empty()) return fallback;
    // Rejected rather than silently read as 0. A mistyped --display would otherwise
    // quietly capture the primary monitor and look like the flag being ignored.
    char* end = nullptr;
    const long value = std::strtol(raw.c_str(), &end, 10);
    if (end == raw.c_str() || *end != '\0') {
        throw Error(name + " expects a number, got '" + raw + "'");
    }
    return static_cast<int>(value);
}

}  // namespace cufast::cli
