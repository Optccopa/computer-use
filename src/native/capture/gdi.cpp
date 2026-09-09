// The fallback path, and the cursor composite both paths share.
//
// GDI reports the composed, already-oriented desktop, which makes it the
// reference a duplication frame can be checked against -- the only way to catch
// a rotation that is wrong by a whole quarter turn, since both look plausible.
#include "capture/capture.hpp"

#include <algorithm>
#include <cstring>

namespace cufast {

void Capture::release_gdi() {
    if (mem_dc_) {
        if (old_bitmap_) SelectObject(mem_dc_, old_bitmap_);
        DeleteDC(mem_dc_);
        mem_dc_ = nullptr;
        old_bitmap_ = nullptr;
    }
    if (dib_) {
        DeleteObject(dib_);
        dib_ = nullptr;
    }
    pixels_ = nullptr;
}

void Capture::init_dib(int width, int height) {
    if (width <= 0 || height <= 0) throw Error("display has zero area");
    release_gdi();

    HDC screen = GetDC(nullptr);
    if (!screen) throw Error("GetDC(NULL) failed");
    mem_dc_ = CreateCompatibleDC(screen);
    ReleaseDC(nullptr, screen);
    if (!mem_dc_) throw Error("CreateCompatibleDC failed");

    BITMAPINFO bi{};
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = width;
    bi.bmiHeader.biHeight = -height;  // negative => top-down rows
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;

    void* bits = nullptr;
    dib_ = CreateDIBSection(mem_dc_, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (!dib_ || !bits) throw Error("CreateDIBSection failed");

    old_bitmap_ = SelectObject(mem_dc_, dib_);
    pixels_ = static_cast<uint8_t*>(bits);
    width_ = width;
    height_ = height;
    stride_ = width * 4;  // 32bpp rows are inherently DWORD-aligned
    have_frame_ = false;
    cursor_drawn_ = false;
}

void Capture::grab_gdi() {
    HDC screen = GetDC(nullptr);
    if (!screen) throw Error("GetDC(NULL) failed");
    // CAPTUREBLT includes layered windows: tooltips, menus, drop shadows.
    BOOL ok = BitBlt(mem_dc_, 0, 0, width_, height_, screen, monitor_.rect.left,
                     monitor_.rect.top, SRCCOPY | CAPTUREBLT);
    ReleaseDC(nullptr, screen);
    if (!ok) throw Error("BitBlt failed");
    // CreateDIBSection requires this before the bits are read through the pointer.
    // The flush at the top of grab() covers the PREVIOUS call's GDI work, not this
    // BitBlt, so without it the cursor backup -- and, when draw_cursor is off, which
    // is what profile/sample_hash/wait_for_change all use, the downscale itself --
    // can read the previous frame.
    GdiFlush();
}

void Capture::restore_under_cursor() {
    if (!cursor_drawn_) return;
    const int w = cursor_rect_.right - cursor_rect_.left;
    const int h = cursor_rect_.bottom - cursor_rect_.top;
    if (w > 0 && h > 0 && cursor_rect_.right <= width_ && cursor_rect_.bottom <= height_ &&
        cursor_backup_.size() >= static_cast<size_t>(w) * h * 4) {
        for (int y = 0; y < h; ++y) {
            std::memcpy(pixels_ + static_cast<size_t>(cursor_rect_.top + y) * stride_ +
                            static_cast<size_t>(cursor_rect_.left) * 4,
                        cursor_backup_.data() + static_cast<size_t>(y) * w * 4,
                        static_cast<size_t>(w) * 4);
        }
    }
    cursor_drawn_ = false;
}

void Capture::draw_cursor_into_dib() {
    CURSORINFO ci{};
    ci.cbSize = sizeof(ci);
    if (!GetCursorInfo(&ci) || !(ci.flags & CURSOR_SHOWING) || !ci.hCursor) return;

    ICONINFO ii{};
    if (!GetIconInfo(ci.hCursor, &ii)) return;

    int cw = 0, ch = 0;
    BITMAP bm{};
    if (ii.hbmColor && GetObject(ii.hbmColor, sizeof(bm), &bm)) {
        cw = bm.bmWidth;
        ch = bm.bmHeight;
    } else if (ii.hbmMask && GetObject(ii.hbmMask, sizeof(bm), &bm)) {
        // Monochrome cursors pack AND and XOR masks into one double-height bitmap.
        cw = bm.bmWidth;
        ch = bm.bmHeight / 2;
    }
    const int x = ci.ptScreenPos.x - monitor_.rect.left - static_cast<int>(ii.xHotspot);
    const int y = ci.ptScreenPos.y - monitor_.rect.top - static_cast<int>(ii.yHotspot);
    if (ii.hbmColor) DeleteObject(ii.hbmColor);
    if (ii.hbmMask) DeleteObject(ii.hbmMask);
    if (cw <= 0 || ch <= 0) return;

    // Clip to the surface, then stash exactly the pixels DrawIconEx will touch so a
    // later reused frame can be un-drawn without re-copying the whole surface.
    RECT r{(std::max)(0, x), (std::max)(0, y), (std::min)(width_, x + cw),
           (std::min)(height_, y + ch)};
    if (r.right <= r.left || r.bottom <= r.top) return;

    const int w = r.right - r.left;
    const int h = r.bottom - r.top;
    cursor_backup_.resize(static_cast<size_t>(w) * h * 4);
    for (int row = 0; row < h; ++row) {
        std::memcpy(cursor_backup_.data() + static_cast<size_t>(row) * w * 4,
                    pixels_ + static_cast<size_t>(r.top + row) * stride_ +
                        static_cast<size_t>(r.left) * 4,
                    static_cast<size_t>(w) * 4);
    }
    cursor_rect_ = r;

    if (DrawIconEx(mem_dc_, x, y, ci.hCursor, 0, 0, 0, nullptr, DI_NORMAL)) {
        cursor_drawn_ = true;
    }
    // Flushed by the next grab() before it touches pixels_, so a failed DrawIconEx
    // that still dirtied the surface cannot race a later pointer read.
    GdiFlush();
}

}  // namespace cufast
