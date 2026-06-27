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

static int EffectiveGrayscalePointCount(const GrayscaleData& gs) {
    return std::clamp(gs.pointCount, 2, 32);
}

// Evaluate SDR grayscale correction (matches the shader's ApplyGrayscaleCorrection).
// Domain-agnostic sqrt-index/sqrt-interp: the caller now passes the SIGNAL (the bake
// corrects in signal domain, so slider i sits at signal t² = code cap·t²). The param
// keeps the name Y_linear for diff hygiene but carries a 0-1 signal value here.
float EvalGrayscaleSDR(float Y_linear, const GrayscaleData& gs) {
    if (Y_linear <= 0.0f) return 0.0f;

    int pc = EffectiveGrayscalePointCount(gs);
    float idx = sqrtf(std::clamp(Y_linear, 0.0f, 1.0f)) * (float)(pc - 1);
    int i0 = (int)floorf(idx);
    int i1 = (std::min)(i0 + 1, pc - 1);
    float t = idx - floorf(idx);

    float v0 = gs.points[i0];
    float v1 = gs.points[i1];

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

    int pc = EffectiveGrayscalePointCount(gs);
    float scaledI = pqValue / pqPeak;
    if (scaledI <= 1.0f) {
        float idx = scaledI * (float)(pc - 1);
        int i0 = (int)floorf(idx);
        int i1 = (std::min)(i0 + 1, pc - 1);
        float t = idx - floorf(idx);

        float v0 = gs.points[i0];
        float v1 = gs.points[i1];

        float corrected = v0 + (v1 - v0) * t;
        return corrected * pqPeak;
    } else {
        // Above peak: apply last point's correction factor
        int lastIdx = pc - 1;
        float lastVal = gs.points[lastIdx];
        return lastVal * pqValue;
    }
}

// Per-channel variants: read from pointsR/G/B instead of points
float EvalGrayscaleSDR_Channel(float Y_linear, const GrayscaleData& gs, int channel) {
    if (Y_linear <= 0.0f) return 0.0f;

    const float* pts = (channel == 0) ? gs.pointsR : (channel == 1) ? gs.pointsG : gs.pointsB;
    int pc = EffectiveGrayscalePointCount(gs);
    float idx = sqrtf(std::clamp(Y_linear, 0.0f, 1.0f)) * (float)(pc - 1);
    int i0 = (int)floorf(idx);
    int i1 = (std::min)(i0 + 1, pc - 1);
    float t = idx - floorf(idx);

    float v0 = pts[i0];
    float v1 = pts[i1];

    float s0 = sqrtf((std::max)(v0, 0.0f));
    float s1 = sqrtf((std::max)(v1, 0.0f));
    float correctedS = s0 + (s1 - s0) * t;
    return correctedS * correctedS;
}

float EvalGrayscaleHDR_Channel(float pqValue, const GrayscaleData& gs, float pqPeak, int channel) {
    if (pqValue <= 0.0f || pqPeak <= 0.0f) return pqValue;

    const float* pts = (channel == 0) ? gs.pointsR : (channel == 1) ? gs.pointsG : gs.pointsB;
    int pc = EffectiveGrayscalePointCount(gs);
    float scaledI = pqValue / pqPeak;
    if (scaledI <= 1.0f) {
        float idx = scaledI * (float)(pc - 1);
        int i0 = (int)floorf(idx);
        int i1 = (std::min)(i0 + 1, pc - 1);
        float t = idx - floorf(idx);

        float v0 = pts[i0];
        float v1 = pts[i1];

        float corrected = v0 + (v1 - v0) * t;
        return corrected * pqPeak;
    } else {
        int lastIdx = pc - 1;
        float lastVal = pts[lastIdx];
        return lastVal * pqValue;
    }
}

// ============================================================================
// SECTION: 1D LUT Generation
// ============================================================================

void GenerateMHC2LUT_SDR(const GrayscaleData& gs, float* outLUT, int lutSize) {
    for (int j = 0; j < lutSize; j++) {
        float t = (float)j / (float)(lutSize - 1);  // scanout signal (sRGB-encoded)

        if (!gs.enabled) {
            // Identity LUT - linear ramp
            outLUT[j] = t;
            continue;
        }

        // SIGNAL-domain grayscale: index the slots by sqrt(signal) and correct in signal
        // (mirrors the shader's ApplyGrayscaleCorrection). The editor's points are signal-
        // domain (identity t²), so slider i sits at signal t² i.e. code cap·t² — dense in the
        // shadows — and the slider's value IS the patch code that drives it. EvalGrayscaleSDR's
        // sqrt-index/sqrt-interp math is domain-agnostic; we just feed it the signal, not the
        // sRGB-decoded linear light.
        float corrected = EvalGrayscaleSDR(t, gs);

        // 2.2->2.4 gamma (BT.1886) is a linear-light transform: decode, pow, re-encode.
        if (gs.use24Gamma) {
            float lin = SrgbEOTF((std::max)(corrected, 0.0f));
            lin = powf((std::max)(lin, 0.0f), 2.4f / 2.2f);
            corrected = SrgbOETF((std::max)(lin, 0.0f));
        }

        outLUT[j] = std::clamp(corrected, 0.0f, 1.0f);
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
        float t = (float)j / (float)(lutSize - 1);  // scanout signal
        if (!gs.enabled) { outLUT[j] = t; continue; }
        // Signal-domain (see GenerateMHC2LUT_SDR): index by sqrt(signal), correct in signal.
        float corrected = EvalGrayscaleSDR_Channel(t, gs, channel);
        if (gs.use24Gamma) {
            float lin = SrgbEOTF((std::max)(corrected, 0.0f));
            lin = powf((std::max)(lin, 0.0f), 2.4f / 2.2f);
            corrected = SrgbOETF((std::max)(lin, 0.0f));
        }
        outLUT[j] = std::clamp(corrected, 0.0f, 1.0f);
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
