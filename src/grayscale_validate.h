// DesktopLUT - grayscale_validate.h
// The one set of rules a grayscale slot (MHC base or correction grayscale) must satisfy, shared by
// the INI loader (settings.cpp) and the calibration pipe (ipc_grayscale.h): what the pipe accepts is
// exactly what a restart will load back, never silently reset.

#pragma once

#include "types.h"   // GrayscaleSettings

#include <cmath>
#include <vector>

// The editor's point layouts: 10, 20 or 32 points (anything else has no editor and no stable grid).
inline bool IsValidGrayscalePointCount(int n) { return n == 10 || n == 20 || n == 32; }

// Highest output level a point may carry. Levels above 1 are legitimate: a client composing
// points x luminance (DLC's touch-up solver) can lift the top point slightly past full scale, which
// the bake saturates (the top segment reaches 1 a little earlier). 2 is a sanity bound against
// garbage, not a colour limit; anything at or below it round-trips through the INI unchanged.
constexpr float kMaxGrayscalePointLevel = 2.0f;

// Output levels: exactly n finite values in [0, kMaxGrayscalePointLevel].
inline bool GrayscalePointsValid(const std::vector<float>& points, int n) {
    if ((int)points.size() != n) return false;
    for (float v : points)
        if (!std::isfinite(v) || v < 0.0f || v > kMaxGrayscalePointLevel) return false;
    return true;
}

// Per-channel gains (rgbDeviations, centred at 1): exactly n finite values in [0, 8].
inline bool GrayscaleGainsValid(const std::vector<float>& gains, int n) {
    if ((int)gains.size() != n) return false;
    for (float v : gains)
        if (!std::isfinite(v) || v < 0.0f || v > 8.0f) return false;
    return true;
}

// The identity curve of a slot: SDR points sit on the dense-low signal grid (point i = (i/(n-1))^2),
// HDR points are uniform in PQ. A uniform ramp on the SDR grid is NOT identity — it is a square root.
inline void InitIdentityGrayscale(GrayscaleSettings& gs, bool isHDR) {
    if (isHDR) gs.initLinearPQ();
    else gs.initLinear();
}
