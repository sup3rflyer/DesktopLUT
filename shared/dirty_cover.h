// Dirty-rect coverage of a clean copy of a composed frame (the DWM hook).
//
// DWM re-composes only its dirty rects into a back buffer; outside them the back buffer keeps the
// previous frame's FINISHED output (our LUT/tonemap already applied). Anything in the hook that
// needs the whole composed frame — the dynamic-tonemap peak detector — therefore keeps its own
// copy, fed only from dirty rects. That copy is trustworthy once every pixel has been refreshed
// from a composed rect since it went stale (created, or a present of its monitor that did not feed
// it). This tracks that on square tiles: a tile counts only when ONE rect covers it completely
// (conservative — two rects that together cover a tile do not count). A tile clipped by the frame
// edge counts up to the edge.
//
// Pure bookkeeping, no D3D — unit-tested (tests/test_peak_detect.cpp). Init allocates; Reset and
// Mark never do, so the present path stays allocation-free.
#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

struct DirtyCover {
    std::vector<uint8_t> tiles;
    unsigned width = 0, height = 0, tile = 0;
    unsigned cols = 0, rows = 0;
    size_t left = 0;           // tiles not yet refreshed since the last Reset

    void Init(unsigned w, unsigned h, unsigned tilePx) {
        width = w;
        height = h;
        tile = tilePx ? tilePx : 1;
        cols = (w + tile - 1) / tile;
        rows = (h + tile - 1) / tile;
        tiles.assign((size_t)cols * rows, 0);
        left = tiles.size();
    }

    void Reset() {
        for (uint8_t& t : tiles) t = 0;
        left = tiles.size();
    }

    bool Primed() const { return !tiles.empty() && left == 0; }

    // Clip a dirty rect to the frame. False = nothing of it lies inside.
    bool Clip(long l, long t, long r, long b, unsigned& cl, unsigned& ct, unsigned& cr, unsigned& cb) const {
        if (l < 0) l = 0;
        if (t < 0) t = 0;
        if (r > (long)width) r = (long)width;
        if (b > (long)height) b = (long)height;
        if (r <= l || b <= t) return false;
        cl = (unsigned)l; ct = (unsigned)t; cr = (unsigned)r; cb = (unsigned)b;
        return true;
    }

    // Record an already-clipped rect [l,r) x [t,b). Returns how many tiles it newly covered.
    size_t Mark(unsigned l, unsigned t, unsigned r, unsigned b) {
        if (left == 0 || tiles.empty()) return 0;
        const unsigned tx0 = (l + tile - 1) / tile, ty0 = (t + tile - 1) / tile;
        const unsigned tx1 = (r >= width) ? cols : r / tile;
        const unsigned ty1 = (b >= height) ? rows : b / tile;
        size_t newly = 0;
        for (unsigned ty = ty0; ty < ty1; ty++)
            for (unsigned tx = tx0; tx < tx1; tx++) {
                uint8_t& c = tiles[(size_t)ty * cols + tx];
                if (!c) { c = 1; newly++; }
            }
        left -= newly;
        return newly;
    }
};
