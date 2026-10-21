// DesktopLUT - shared/fald_exempt.h
// DesktopLUT's own windows are exempt from the FALD views.
//
// A view (FaldSettings::debugMode 1..9) replaces the picture the layer outputs with a diagnostic, DesktopLUT's own
// interface included: a user who picked one could not read the VIEW row to end it. So while a view is on, both paths
// draw the pixel pass a SECOND time with the view off, scissored to each of these rects: DesktopLUT's windows show the
// corrected picture, the rest of the screen shows the view. With the view off nothing here runs (no rect is sent, no
// draw is added), so the layer's output is the same bytes as without this file — the OFF manifest
// (tests/test_fald_fade.cpp) and tests/test_fald_exempt.cpp hold both paths to that.
//
// The host (src/gui.cpp FaldExemptRefresh) lists its own visible top-level windows — not minimised, not cloaked, not a
// click-through overlay — in desktop pixels, while a view is on. Per monitor they are clipped and merged into at most
// FALD_EXEMPT_WIRE_RECTS rects in monitor pixels (FaldExemptForMonitor). The overlay path takes them from the host's
// list (src/fald.cpp), the DWM hook through DwmHookFaldTuning::exempt (FaldExemptPack / FaldExemptUnpack). Each path
// maps them onto its frame (FaldExemptToFrame): a frame the size of the monitor takes them as they are; one with the
// monitor's width and height swapped (a turned monitor whose back buffer is not turned) takes both quarter turns,
// because the turn's sense is not known there; any other size is scaled. A monitor turned upside down (180 degrees:
// the size cannot tell) gets the mirrored rects too, from the host. Rounded outward everywhere: a window is never
// exempt over less than its own area. Pure (no Windows, no D3D): tested in tests/test_fald_exempt.cpp.
#pragma once
#include <stdint.h>

struct FaldExemptRect { int32_t x0, y0, x1, y1; };   // half-open, pixels; empty when x1 <= x0 or y1 <= y0

constexpr int FALD_EXEMPT_WIRE_RECTS = 3;                              // per monitor on the wire: 2 words each
constexpr int FALD_EXEMPT_WIRE_WORDS = 2 * FALD_EXEMPT_WIRE_RECTS;
constexpr int FALD_EXEMPT_FRAME_RECTS = 2 * FALD_EXEMPT_WIRE_RECTS;    // FaldExemptToFrame's most: both quarter turns
constexpr int FALD_EXEMPT_HOST_RECTS = 32;                             // the host's list: windows beyond merge into the last

inline bool FaldExemptEmpty(const FaldExemptRect& r) { return r.x1 <= r.x0 || r.y1 <= r.y0; }

inline FaldExemptRect FaldExemptUnion(const FaldExemptRect& a, const FaldExemptRect& b) {
    return { a.x0 < b.x0 ? a.x0 : b.x0, a.y0 < b.y0 ? a.y0 : b.y0, a.x1 > b.x1 ? a.x1 : b.x1, a.y1 > b.y1 ? a.y1 : b.y1 };
}

inline int64_t FaldExemptArea(const FaldExemptRect& r) {
    return FaldExemptEmpty(r) ? 0 : (int64_t)(r.x1 - r.x0) * (int64_t)(r.y1 - r.y0);
}

// Merges rects in place until at most maxOut remain: each step joins the pair whose union adds the least area (a rect
// inside another goes first). Deterministic (the first such pair in index order). Returns the count.
inline int FaldExemptMerge(FaldExemptRect* r, int n, int maxOut) {
    if (maxOut < 1) return 0;
    while (n > maxOut) {
        int bi = 0, bj = 1;
        int64_t best = INT64_MAX;
        for (int i = 0; i < n; i++)
            for (int j = i + 1; j < n; j++) {
                const int64_t cost = FaldExemptArea(FaldExemptUnion(r[i], r[j])) - FaldExemptArea(r[i]) - FaldExemptArea(r[j]);
                if (cost < best) { best = cost; bi = i; bj = j; }
            }
        r[bi] = FaldExemptUnion(r[bi], r[bj]);
        for (int k = bj; k + 1 < n; k++) r[k] = r[k + 1];
        n--;
    }
    return n;
}

// The desktop rects (`n`, desktop pixels) that lie on one monitor (desktop rect monLeft, monTop, monW x monH), in
// monitor pixels, clipped to it, empties dropped; `mirror180` adds each one turned upside down; merged to maxOut.
inline int FaldExemptForMonitor(const FaldExemptRect* desktop, int n, int monLeft, int monTop, int monW, int monH,
                                bool mirror180, FaldExemptRect* out, int maxOut) {
    if (!desktop || !out || maxOut < 1 || monW <= 0 || monH <= 0) return 0;
    FaldExemptRect tmp[2 * FALD_EXEMPT_HOST_RECTS];
    int k = 0;
    for (int i = 0; i < n; i++) {
        FaldExemptRect r = { desktop[i].x0 - monLeft, desktop[i].y0 - monTop, desktop[i].x1 - monLeft, desktop[i].y1 - monTop };
        if (r.x0 < 0) r.x0 = 0;
        if (r.y0 < 0) r.y0 = 0;
        if (r.x1 > monW) r.x1 = monW;
        if (r.y1 > monH) r.y1 = monH;
        if (FaldExemptEmpty(r)) continue;
        if (k == FALD_EXEMPT_HOST_RECTS) { tmp[k - 1] = FaldExemptUnion(tmp[k - 1], r); continue; }   // never dropped
        tmp[k++] = r;
    }
    if (mirror180) {
        const int m = k;
        for (int i = 0; i < m; i++) tmp[k++] = { monW - tmp[i].x1, monH - tmp[i].y1, monW - tmp[i].x0, monH - tmp[i].y0 };
    }
    k = FaldExemptMerge(tmp, k, maxOut);
    for (int i = 0; i < k; i++) out[i] = tmp[i];
    return k;
}

// The wire: per rect two words, (x0 | y0 << 16) and (x1 | y1 << 16), monitor pixels clamped to 0..65535; unused = 0.
inline void FaldExemptPack(const FaldExemptRect* r, int n, uint32_t words[FALD_EXEMPT_WIRE_WORDS]) {
    auto c = [](int32_t v) { return (uint32_t)(v < 0 ? 0 : (v > 65535 ? 65535 : v)); };
    for (int i = 0; i < FALD_EXEMPT_WIRE_WORDS; i++) words[i] = 0;
    for (int i = 0, k = 0; i < n && k < FALD_EXEMPT_WIRE_RECTS; i++) {
        if (FaldExemptEmpty(r[i])) continue;
        words[2 * k] = c(r[i].x0) | (c(r[i].y0) << 16);
        words[2 * k + 1] = c(r[i].x1) | (c(r[i].y1) << 16);
        k++;
    }
}

inline int FaldExemptUnpack(const uint32_t words[FALD_EXEMPT_WIRE_WORDS], FaldExemptRect out[FALD_EXEMPT_WIRE_RECTS]) {
    int k = 0;
    for (int i = 0; i < FALD_EXEMPT_WIRE_RECTS; i++) {
        const FaldExemptRect r = { (int32_t)(words[2 * i] & 0xFFFFu), (int32_t)(words[2 * i] >> 16),
                                   (int32_t)(words[2 * i + 1] & 0xFFFFu), (int32_t)(words[2 * i + 1] >> 16) };
        if (!FaldExemptEmpty(r)) out[k++] = r;
    }
    return k;
}

// Monitor rects (`n`, on a monW x monH monitor) onto a frameW x frameH frame, clipped; empties dropped. Writes at most
// 2 n rects (FALD_EXEMPT_FRAME_RECTS for a full wire list) and returns the count.
inline int FaldExemptToFrame(const FaldExemptRect* r, int n, int monW, int monH, int frameW, int frameH, FaldExemptRect* out) {
    if (!r || !out || frameW <= 0 || frameH <= 0) return 0;
    if (monW <= 0 || monH <= 0) { monW = frameW; monH = frameH; }   // unknown: the frame is the monitor
    int k = 0;
    auto put = [&](FaldExemptRect q) {
        if (q.x0 < 0) q.x0 = 0;
        if (q.y0 < 0) q.y0 = 0;
        if (q.x1 > frameW) q.x1 = frameW;
        if (q.y1 > frameH) q.y1 = frameH;
        if (!FaldExemptEmpty(q)) out[k++] = q;
    };
    const bool same = frameW == monW && frameH == monH;
    const bool turned = !same && frameW == monH && frameH == monW;
    for (int i = 0; i < n; i++) {
        const FaldExemptRect& a = r[i];
        if (FaldExemptEmpty(a)) continue;
        if (same) {
            put(a);
        } else if (turned) {
            // (x, y) on the monitor -> (y, monW - x) or (monH - y, x) in the frame: one of the two quarter turns
            put({ a.y0, monW - a.x1, a.y1, monW - a.x0 });
            put({ monH - a.y1, a.x0, monH - a.y0, a.x1 });
        } else {
            // floor the start, ceil the end: outward
            put({ (int32_t)((int64_t)a.x0 * frameW / monW), (int32_t)((int64_t)a.y0 * frameH / monH),
                  (int32_t)(((int64_t)a.x1 * frameW + monW - 1) / monW), (int32_t)(((int64_t)a.y1 * frameH + monH - 1) / monH) });
        }
    }
    return k;
}
