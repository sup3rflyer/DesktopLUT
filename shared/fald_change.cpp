// DesktopLUT - shared/fald_change.cpp (see fald_change.h)
#include "fald_change.h"

#include <algorithm>

// The shaders' ZoneOfPixel for one axis: the zone a lattice-local pixel's CENTRE lies in (fald_shader.h; fald_panel.h
// FaldZoneOfPixel is its C++ twin, kept inline here so this file stays standalone).
static unsigned int ZoneOf(unsigned int local, unsigned int count, unsigned int extent) {
    return (unsigned int)(((2ull * local + 1ull) * count) / (2ull * extent));
}

bool FaldTouchedZones(const FaldChangeRect* rects, int numRects, unsigned int originX, unsigned int originY, unsigned int latW,
                      unsigned int latH, unsigned int cols, unsigned int rows, unsigned int maxZones, std::vector<uint32_t>& out) {
    out.clear();
    if (cols == 0 || rows == 0 || latW == 0 || latH == 0) return false;
    std::vector<uint8_t> seen((size_t)cols * rows, (uint8_t)0);
    unsigned int n = 0;
    for (int i = 0; i < numRects; i++) {
        // clip to the lattice (half-open), in lattice-local pixels
        const long long l = std::max<long long>(rects[i].left, (long long)originX) - (long long)originX;
        const long long t = std::max<long long>(rects[i].top, (long long)originY) - (long long)originY;
        const long long r = std::min<long long>(rects[i].right, (long long)originX + latW) - (long long)originX;
        const long long b = std::min<long long>(rects[i].bottom, (long long)originY + latH) - (long long)originY;
        if (r <= l || b <= t) continue;
        const unsigned int c0 = ZoneOf((unsigned int)l, cols, latW), c1 = ZoneOf((unsigned int)(r - 1), cols, latW);
        const unsigned int r0 = ZoneOf((unsigned int)t, rows, latH), r1 = ZoneOf((unsigned int)(b - 1), rows, latH);
        for (unsigned int zy = r0; zy <= r1 && zy < rows; zy++)
            for (unsigned int zx = c0; zx <= c1 && zx < cols; zx++) {
                uint8_t& s = seen[(size_t)zy * cols + zx];
                if (s) continue;
                s = 1;
                if (++n > maxZones) { out.clear(); return false; }
            }
    }
    out.reserve(n);
    for (size_t k = 0; k < seen.size(); k++)
        if (seen[k]) out.push_back((uint32_t)k);
    return true;
}

unsigned int FaldChangeMaxZones(unsigned int totalZones) {
    return totalZones / 2u > 0u ? totalZones / 2u : 1u;
}

FaldChangePath FaldChangeDecide(const FaldChangeInputs& in, unsigned int maxZones) {
    if (!in.stateComplete || !in.settingsSame || !in.frameKnown || !in.gainFused || in.fadeOn) return FALD_CHANGE_FULL;
    if (in.temporalMode > 3u) return FALD_CHANGE_FULL;
    if (in.touchedZones > maxZones || in.totalZones == 0u) return FALD_CHANGE_FULL;
    return FALD_CHANGE_PARTIAL;
}
