// The clipboard, through the Win32 clipboard API.

#include "clipboard/clipboard.hpp"

#include <windows.h>

#include <cstring>

#include "core/common.hpp"
#include "input/input.hpp"

namespace cufast {
namespace {

// Only one process may hold the clipboard open at a time, so a collision is routine
// rather than exceptional: a clipboard manager, or the very application being
// driven, is often mid-update in the milliseconds after a Ctrl+C. Retrying briefly
// turns that into a short wait instead of an error the model has to reason about.
constexpr int kOpenAttempts = 12;
constexpr DWORD kRetryMs = 10;

// Opens for the scope, and closes even when the body throws. Leaving the clipboard
// open would block every other process on the machine from using it.
class Lock {
public:
    Lock() {
        for (int i = 0; i < kOpenAttempts; ++i) {
            if (OpenClipboard(nullptr)) return;
            Sleep(kRetryMs);
        }
        throw Error("could not open the clipboard: another process has it open. That is "
                    "usually a clipboard manager and usually passes -- try once more.");
    }
    Lock(const Lock&) = delete;
    Lock& operator=(const Lock&) = delete;
    ~Lock() { CloseClipboard(); }
};

// Releases a global handle's lock for the scope. GlobalLock nests a count, so an
// early return that skipped the unlock would pin the block for the process lifetime.
class GlobalLock_ {
public:
    explicit GlobalLock_(HGLOBAL h) : h_(h), p_(GlobalLock(h)) {}
    GlobalLock_(const GlobalLock_&) = delete;
    GlobalLock_& operator=(const GlobalLock_&) = delete;
    ~GlobalLock_() { if (p_) GlobalUnlock(h_); }
    void* get() const { return p_; }

private:
    HGLOBAL h_;
    void* p_;
};

std::string utf16_to_utf8(const wchar_t* text, int units) {
    if (units <= 0) return {};
    const int need = WideCharToMultiByte(CP_UTF8, 0, text, units, nullptr, 0, nullptr, nullptr);
    if (need <= 0) throw Error("the clipboard's text could not be decoded");
    std::string out(static_cast<size_t>(need), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text, units, out.data(), need, nullptr, nullptr);
    return out;
}

}  // namespace

std::string clipboard_read() {
    Lock lock;
    // Not an error. A clipboard holding an image, a file list or nothing at all is a
    // fact about the machine, and reporting it as a failure would have the model
    // retry a copy that already worked.
    if (!IsClipboardFormatAvailable(CF_UNICODETEXT)) return {};

    HANDLE handle = GetClipboardData(CF_UNICODETEXT);
    if (!handle) return {};

    GlobalLock_ locked(handle);
    const auto* text = static_cast<const wchar_t*>(locked.get());
    if (!text) return {};

    // The block is sized in bytes and the string is terminated, so the length has to
    // come from the string rather than from GlobalSize: a producer is allowed to
    // over-allocate, and trusting the block size appends whatever padding follows.
    size_t units = 0;
    const size_t cap = GlobalSize(handle) / sizeof(wchar_t);
    while (units < cap && text[units] != L'\0') ++units;

    std::string utf8 = utf16_to_utf8(text, static_cast<int>(units));

    // Windows puts CRLF on the clipboard; every other part of this harness speaks LF.
    // Normalising here is what makes read-then-write a round trip rather than a
    // string that grows a carriage return every cycle.
    std::string out;
    out.reserve(utf8.size());
    for (size_t i = 0; i < utf8.size(); ++i) {
        if (utf8[i] == '\r' && i + 1 < utf8.size() && utf8[i + 1] == '\n') continue;
        out.push_back(utf8[i]);
    }
    return out;
}

void clipboard_write(const std::string& utf8) {
    // The clipboard is the user's, and a stopped agent must not still be overwriting
    // it. Checked before the conversion so a stop costs nothing.
    check_input_allowed();

    // Back to CRLF, because a bare LF pasted into a Win32 edit control renders as a
    // box or as one long line. The pair with clipboard_read's stripping means text
    // survives a round trip unchanged.
    std::string crlf;
    crlf.reserve(utf8.size());
    for (size_t i = 0; i < utf8.size(); ++i) {
        if (utf8[i] == '\n' && (i == 0 || utf8[i - 1] != '\r')) crlf.push_back('\r');
        crlf.push_back(utf8[i]);
    }

    std::wstring wide;
    if (!crlf.empty()) {
        const int need = MultiByteToWideChar(CP_UTF8, 0, crlf.data(),
                                             static_cast<int>(crlf.size()), nullptr, 0);
        if (need <= 0) throw Error("text is not valid UTF-8");
        wide.resize(static_cast<size_t>(need));
        MultiByteToWideChar(CP_UTF8, 0, crlf.data(), static_cast<int>(crlf.size()),
                            wide.data(), need);
    }

    const size_t bytes = (wide.size() + 1) * sizeof(wchar_t);
    HGLOBAL block = GlobalAlloc(GMEM_MOVEABLE, bytes);
    win_check(block != nullptr, "GlobalAlloc for the clipboard");

    {
        GlobalLock_ locked(block);
        if (!locked.get()) {
            GlobalFree(block);
            throw Error("GlobalLock for the clipboard failed");
        }
        auto* dest = static_cast<wchar_t*>(locked.get());
        if (!wide.empty()) memcpy(dest, wide.data(), wide.size() * sizeof(wchar_t));
        dest[wide.size()] = L'\0';
    }

    Lock lock;
    if (!EmptyClipboard()) {
        GlobalFree(block);
        win_check(false, "EmptyClipboard");
    }
    // On success the clipboard owns the block; freeing it here would be a
    // double free the next time anything pastes.
    if (!SetClipboardData(CF_UNICODETEXT, block)) {
        GlobalFree(block);
        win_check(false, "SetClipboardData");
    }
}

}  // namespace cufast
