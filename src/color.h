// DesktopLUT - color.h
// Color space mathematics and primaries calculations

#pragma once

#include "types.h"

// Calculate 3x3 color space conversion matrix using Bradford chromatic adaptation
// Converts from source primaries (content) to target primaries (display)
void CalculatePrimariesMatrix(const DisplayPrimariesData& src, const DisplayPrimariesData& tgt, float* outMatrix);

// CPU-side PQ (ST.2084) OETF: linear light (0-1 normalized to 10000 nits) -> PQ signal (0-1)
// Used to precompute constant PQ values for shader (avoids per-pixel pow() calls)
inline float LinearToPQScalar(float L) {
    static const float m1 = 0.1593017578125f;   // 2610/16384
    static const float m2 = 78.84375f;          // 2523/4096 * 128
    static const float c1 = 0.8359375f;         // 3424/4096
    static const float c2 = 18.8515625f;        // 2413/4096 * 32
    static const float c3 = 18.6875f;           // 2392/4096 * 32
    if (L < 1e-10f) L = 1e-10f;
    float Ym = powf(L, m1);
    return powf((c1 + c2 * Ym) / (1.0f + c3 * Ym), m2);
}
