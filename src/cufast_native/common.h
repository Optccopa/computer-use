#pragma once

#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace cufast {

// All failures surface to Python as RuntimeError via nanobind.
struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

inline void hr_check(HRESULT hr, const char* what) {
    if (FAILED(hr)) {
        char buf[192];
        std::snprintf(buf, sizeof(buf), "%s failed (HRESULT 0x%08lX)", what,
                      static_cast<unsigned long>(hr));
        throw Error(buf);
    }
}

inline void win_check(bool ok, const char* what) {
    if (!ok) {
        char buf[192];
        std::snprintf(buf, sizeof(buf), "%s failed (GetLastError %lu)", what, GetLastError());
        throw Error(buf);
    }
}

// Minimal COM smart pointer. Avoids pulling in ATL/WRL.
template <typename T>
class ComPtr {
public:
    ComPtr() = default;
    ComPtr(const ComPtr& o) : p_(o.p_) { if (p_) p_->AddRef(); }
    ComPtr(ComPtr&& o) noexcept : p_(o.p_) { o.p_ = nullptr; }
    ~ComPtr() { reset(); }

    ComPtr& operator=(const ComPtr& o) {
        if (this != &o) {
            if (o.p_) o.p_->AddRef();
            reset();
            p_ = o.p_;
        }
        return *this;
    }
    ComPtr& operator=(ComPtr&& o) noexcept {
        if (this != &o) {
            reset();
            p_ = o.p_;
            o.p_ = nullptr;
        }
        return *this;
    }

    void reset() {
        if (p_) {
            p_->Release();
            p_ = nullptr;
        }
    }

    // For out-params: releases any existing pointer first.
    T** put() {
        reset();
        return &p_;
    }
    void** put_void() { return reinterpret_cast<void**>(put()); }

    T* get() const { return p_; }
    T* operator->() const { return p_; }
    explicit operator bool() const { return p_ != nullptr; }

private:
    T* p_ = nullptr;
};

// A captured desktop frame: 32bpp BGRA, top-down, owned by a GDI DIB section so
// that the cursor can be composited with DrawIconEx on either capture path.
struct FrameView {
    uint8_t* pixels = nullptr;  // BGRA, top-down
    int width = 0;
    int height = 0;
    int stride = 0;  // bytes per row
};

}  // namespace cufast
