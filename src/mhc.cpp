// DesktopLUT - mhc.cpp
// Transfer functions, grayscale evaluation, and 1D LUT generation
//
// This is the math kernel used by mhc_icc.cpp (profile generation) and tested directly.

#include "mhc.h"
#include <cmath>
#include <algorithm>

// ============================================================================
// SECTION: Transfer Functions
// ============================================================================

// sRGB EOTF: decode sRGB-encoded value to linear light
float SrgbEOTF(float v) {
    if (v <= 0.04045f)
        return v / 12.92f;
    return powf((v + 0.055f) / 1.055f, 2.4f);
}

// sRGB OETF: encode linear light to sRGB signal
float SrgbOETF(float v) {
    if (v <= 0.0031308f)
        return v * 12.92f;
    return 1.055f * powf(v, 1.0f / 2.4f) - 0.055f;
}

// PQ OETF constants
static const float PQ_m1 = 0.1593017578125f;
static const float PQ_m2 = 78.84375f;
static const float PQ_c1 = 0.8359375f;
static const float PQ_c2 = 18.8515625f;
static const float PQ_c3 = 18.6875f;

// PQ EOTF: PQ signal (0-1) -> linear light (0-1 normalized to 10000 nits)
float PqEOTF(float pq) {
    float Vm = powf((std::max)(pq, 1e-10f), 1.0f / PQ_m2);
    float t = (std::max)(Vm - PQ_c1, 0.0f) / (std::max)(PQ_c2 - PQ_c3 * Vm, 1e-10f);
    return powf(t, 1.0f / PQ_m1);
}

// PQ OETF: linear light (0-1 normalized to 10000 nits) -> PQ signal (0-1)
float PqOETF(float L) {
    float Y = (std::max)(L, 1e-10f);
    float Ym = powf(Y, PQ_m1);
    return powf((PQ_c1 + PQ_c2 * Ym) / (1.0f + PQ_c3 * Ym), PQ_m2);
}

// ============================================================================
// SECTION: Grayscale Evaluation
// ============================================================================

// Evaluate SDR grayscale correction (matches shader's ApplyGrayscaleCorrectionLinear)
// Input/output are linear light values (0-1)
float EvalGrayscaleSDR(float Y_linear, const GrayscaleData& gs) {
    if (Y_linear <= 0.0f) return 0.0f;

    float idx = sqrtf(std::clamp(Y_linear, 0.0f, 1.0f)) * (float)(gs.pointCount - 1);
    int i0 = (int)floorf(idx);
    int i1 = (std::min)(i0 + 1, gs.pointCount - 1);
    float t = idx - floorf(idx);

    float v0 = (i0 < 32) ? gs.points[i0] : 0.0f;
    float v1 = (i1 < 32) ? gs.points[i1] : 0.0f;

    // Interpolate in sqrt domain (matches shader)
    float s0 = sqrtf((std::max)(v0, 0.0f));
    float s1 = sqrtf((std::max)(v1, 0.0f));
    float correctedS = s0 + (s1 - s0) * t;
    return correctedS * correctedS;
}

// Evaluate HDR grayscale correction (matches shader's ApplyGrayscaleICtCp on I channel)
// For achromatic content (R=G=B), I channel maps to PQ value directly
float EvalGrayscaleHDR(float pqValue, const GrayscaleData& gs, float pqPeak) {
    if (pqValue <= 0.0f || pqPeak <= 0.0f) return pqValue;

    float scaledI = pqValue / pqPeak;
    if (scaledI <= 1.0f) {
        float idx = scaledI * (float)(gs.pointCount - 1);
        int i0 = (int)floorf(idx);
        int i1 = (std::min)(i0 + 1, gs.pointCount - 1);
        float t = idx - floorf(idx);

        float v0 = (i0 < 32) ? gs.points[i0] : 0.0f;
        float v1 = (i1 < 32) ? gs.points[i1] : 0.0f;

        float corrected = v0 + (v1 - v0) * t;
        return corrected * pqPeak;
    } else {
        // Above peak: apply last point's correction factor
        int lastIdx = gs.pointCount - 1;
        float lastVal = (lastIdx < 32) ? gs.points[lastIdx] : 1.0f;
        return lastVal * pqValue;
    }
}

// Per-channel variants: read from pointsR/G/B instead of points
float EvalGrayscaleSDR_Channel(float Y_linear, const GrayscaleData& gs, int channel) {
    if (Y_linear <= 0.0f) return 0.0f;

    const float* pts = (channel == 0) ? gs.pointsR : (channel == 1) ? gs.pointsG : gs.pointsB;
    float idx = sqrtf(std::clamp(Y_linear, 0.0f, 1.0f)) * (float)(gs.pointCount - 1);
    int i0 = (int)floorf(idx);
    int i1 = (std::min)(i0 + 1, gs.pointCount - 1);
    float t = idx - floorf(idx);

    float v0 = (i0 < 32) ? pts[i0] : 0.0f;
    float v1 = (i1 < 32) ? pts[i1] : 0.0f;

    float s0 = sqrtf((std::max)(v0, 0.0f));
    float s1 = sqrtf((std::max)(v1, 0.0f));
    float correctedS = s0 + (s1 - s0) * t;
    return correctedS * correctedS;
}

float EvalGrayscaleHDR_Channel(float pqValue, const GrayscaleData& gs, float pqPeak, int channel) {
    if (pqValue <= 0.0f || pqPeak <= 0.0f) return pqValue;

    const float* pts = (channel == 0) ? gs.pointsR : (channel == 1) ? gs.pointsG : gs.pointsB;
    float scaledI = pqValue / pqPeak;
    if (scaledI <= 1.0f) {
        float idx = scaledI * (float)(gs.pointCount - 1);
        int i0 = (int)floorf(idx);
        int i1 = (std::min)(i0 + 1, gs.pointCount - 1);
        float t = idx - floorf(idx);

        float v0 = (i0 < 32) ? pts[i0] : 0.0f;
        float v1 = (i1 < 32) ? pts[i1] : 0.0f;

        float corrected = v0 + (v1 - v0) * t;
        return corrected * pqPeak;
    } else {
        int lastIdx = gs.pointCount - 1;
        float lastVal = (lastIdx < 32) ? pts[lastIdx] : 1.0f;
        return lastVal * pqValue;
    }
}

// ============================================================================
// SECTION: 1D LUT Generation
// ============================================================================

void GenerateMHC2LUT_SDR(const GrayscaleData& gs, float* outLUT, int lutSize) {
    for (int j = 0; j < lutSize; j++) {
        float t = (float)j / (float)(lutSize - 1);  // sRGB-encoded input position

        if (!gs.enabled) {
            // Identity LUT - linear ramp
            outLUT[j] = t;
            continue;
        }

        // Decode to linear light
        float Y_linear = SrgbEOTF(t);

        // Apply grayscale correction (sqrt-domain interpolation, matches shader)
        float Y_corrected = EvalGrayscaleSDR(Y_linear, gs);

        // 2.2->2.4 gamma transform (BT.1886): pow(L, 2.4/2.2) in linear, darker midtones
        if (gs.use24Gamma) {
            Y_corrected = powf((std::max)(Y_corrected, 0.0f), 2.4f / 2.2f);
        }

        // Encode back to sRGB signal
        outLUT[j] = std::clamp(SrgbOETF((std::max)(Y_corrected, 0.0f)), 0.0f, 1.0f);
    }
}

void GenerateMHC2LUT_HDR(const GrayscaleData& gs, float peakNits, float* outLUT, int lutSize) {
    float pqPeak = PqOETF(peakNits / 10000.0f);

    for (int j = 0; j < lutSize; j++) {
        float pqIn = (float)j / (float)(lutSize - 1);  // PQ-encoded input position

        if (!gs.enabled) {
            // Identity LUT
            outLUT[j] = pqIn;
            continue;
        }

        // Apply grayscale correction in PQ domain
        float pqOut = EvalGrayscaleHDR(pqIn, gs, pqPeak);

        outLUT[j] = std::clamp(pqOut, 0.0f, 1.0f);
    }
}

// Per-channel LUT generators: uses pointsR/G/B for per-channel corrections
void GenerateMHC2LUT_SDR_Channel(const GrayscaleData& gs, float* outLUT, int lutSize, int channel) {
    for (int j = 0; j < lutSize; j++) {
        float t = (float)j / (float)(lutSize - 1);
        if (!gs.enabled) { outLUT[j] = t; continue; }
        float Y_linear = SrgbEOTF(t);
        float Y_corrected = EvalGrayscaleSDR_Channel(Y_linear, gs, channel);
        if (gs.use24Gamma) {
            Y_corrected = powf((std::max)(Y_corrected, 0.0f), 2.4f / 2.2f);
        }
        outLUT[j] = std::clamp(SrgbOETF((std::max)(Y_corrected, 0.0f)), 0.0f, 1.0f);
    }
}

void GenerateMHC2LUT_HDR_Channel(const GrayscaleData& gs, float peakNits, float* outLUT, int lutSize, int channel) {
    float pqPeak = PqOETF(peakNits / 10000.0f);
    for (int j = 0; j < lutSize; j++) {
        float pqIn = (float)j / (float)(lutSize - 1);
        if (!gs.enabled) { outLUT[j] = pqIn; continue; }
        float pqOut = EvalGrayscaleHDR_Channel(pqIn, gs, pqPeak, channel);
        outLUT[j] = std::clamp(pqOut, 0.0f, 1.0f);
    }
}

// ============================================================================
// SECTION: Per-channel TRC → MHC2 1D LUT (ICC file import path)
// ============================================================================

// Invert a tabulated TRC: given target linear value, find the signal that produces it.
// TRC is monotonically increasing: signal(0-1) → linear(0-1), tabulated at even intervals.
// Uses binary search for robustness.
float InvertTRC(const std::vector<float>& trc, float targetLinear) {
    if (trc.size() < 2) return targetLinear;
    int n = (int)trc.size();

    // Clamp to range
    if (targetLinear <= trc[0]) return 0.0f;
    if (targetLinear >= trc[n - 1]) return 1.0f;

    // Binary search for the interval containing targetLinear
    int lo = 0, hi = n - 1;
    while (hi - lo > 1) {
        int mid = (lo + hi) / 2;
        if (trc[mid] <= targetLinear) lo = mid;
        else hi = mid;
    }

    // Linear interpolation within the interval
    float t = (trc[hi] > trc[lo]) ? (targetLinear - trc[lo]) / (trc[hi] - trc[lo]) : 0.0f;
    return ((float)lo + t) / (float)(n - 1);
}

// Generate MHC2 1D LUT from a measured per-channel TRC curve (SDR).
// The LUT corrects the display's non-ideal gamma to match the target power law gamma.
// Pipeline: signal_in → pow(signal, targetGamma) → linear_target → TRC_inverse → signal_out
// Default target is pure 2.2 power law (industry standard for display calibration).
// Note: sRGB EOTF (linear toe + 2.4 power) is NOT used because it produces variable
// effective gamma (~1.0 near black to ~2.27 near white), not flat 2.2 as expected by
// calibration workflows (DisplayCal, ColourSpace, CalMAN).
void GenerateMHC2LUT_FromTRC_SDR(const std::vector<float>& trc, float* outLUT, int lutSize, float targetGamma) {
    for (int j = 0; j < lutSize; j++) {
        float signalIn = (float)j / (float)(lutSize - 1);
        float linearTarget = powf(signalIn, targetGamma);
        float signalOut = InvertTRC(trc, linearTarget);
        outLUT[j] = std::clamp(signalOut, 0.0f, 1.0f);
    }
}

// Generate MHC2 1D LUT from a measured per-channel TRC curve (HDR / PQ).
// ICC TRC maps PQ signal (0-1) → linear light (0-1, normalized to display peak).
// Pipeline: pqIn → PqEOTF (target linear) → InvertTRC → pqOut
// For a perfect PQ display: TRC(s) = PqEOTF(s)*10000/peak, so InvertTRC(target) = pqIn → identity.
void GenerateMHC2LUT_FromTRC_HDR(const std::vector<float>& trc, float* outLUT, int lutSize, float peakNits) {
    for (int j = 0; j < lutSize; j++) {
        float pqIn = (float)j / (float)(lutSize - 1);
        float targetLinear = PqEOTF(pqIn) * 10000.0f / peakNits;
        targetLinear = (std::min)(targetLinear, 1.0f);
        float pqOut = InvertTRC(trc, targetLinear);
        outLUT[j] = std::clamp(pqOut, 0.0f, 1.0f);
    }
}
