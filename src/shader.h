// DesktopLUT - shader.h
// HLSL shader source strings

#pragma once

// Vertex shader: fullscreen triangle (no vertex buffer)
inline const char* g_vsSource = R"(
struct VS_OUTPUT {
    float4 pos : SV_POSITION;
    float2 uv : TEXCOORD0;
};

VS_OUTPUT main(uint id : SV_VertexID) {
    VS_OUTPUT o;
    o.uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}
)";

// Pixel shader: LUT application with HDR/SDR support
// Split into multiple parts to avoid MSVC string literal length limit
inline const char* g_psSource =
// Part 1: Constant buffer and textures
R"(
cbuffer LUTParams : register(b0) {
    float isHDR;
    float sdrWhiteNits;
    float maxNits;
    float lutSize;
    float desktopGamma;
    float tetrahedralInterp;
    float usePassthrough;
    float useManualCorrection;
    float grayscalePoints;
    float grayscaleEnabled;
    float tonemapEnabled;
    float tonemapCurve;
    float4 primariesRow0;
    float4 primariesRow1;
    float4 primariesRow2;
    float tonemapSourcePeak;
    float tonemapTargetPeak;
    float tonemapDynamic;
    float grayscale24;     // SDR: apply 2.2->2.4 gamma transform (0 or 1)
    float grayscalePeakNits;   // HDR grayscale peak - must match ColourSpace target peak
    float _padding;            // Padding for alignment (was whiteBalanceGains, now in primaries matrix via Bradford)
    float _padding2;
    float _padding3;
    float4 grayscale[8];
};

Texture2D<float4> captureTexture : register(t0);
Texture3D<float4> lutTexture : register(t1);
Texture2D<float> blueNoiseTexture : register(t2);
Texture2D<float> peakTexture : register(t3);  // Dynamic peak detection result
SamplerState pointSampler : register(s0);
SamplerState linearSampler : register(s1);
SamplerState wrapSampler : register(s2);

float3 ApplyPrimariesMatrix(float3 rgb) {
    if (useManualCorrection < 0.5) return rgb;
    float3x3 mat = float3x3(primariesRow0.xyz, primariesRow1.xyz, primariesRow2.xyz);
    return mul(mat, rgb);
}
)"
// Part 2: SDR Grayscale correction functions
R"(
// SDR 2.2->2.4 gamma transform (independent of grayscale correction)
// For BT.1886 displays that use 2.4 gamma instead of 2.2
float3 Apply24Gamma(float3 rgb) {
    if (grayscale24 < 0.5f) return rgb;
    float Y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    if (Y < 1e-6f) return rgb;
    // Apply 2.2->2.4 gamma transform: pow(x, 2.4/2.2)
    float correctedY = pow(max(Y, 0.0f), 1.090909f);  // 2.4/2.2 = 12/11
    return rgb * (correctedY / Y);
}

// SDR grayscale: sqrt distribution in linear space
float3 ApplyGrayscaleCorrection(float3 rgb) {
    if (grayscaleEnabled < 0.5) return rgb;
    float Y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    // sqrt distribution: index = sqrt(Y) * (N-1), curve stores Y values
    float idx = sqrt(saturate(Y)) * (grayscalePoints - 1.0f);
    int i0 = (int)floor(idx);
    int i1 = min(i0 + 1, (int)grayscalePoints - 1);
    float v0 = grayscale[i0 / 4][i0 % 4];
    float v1 = grayscale[i1 / 4][i1 % 4];
    float t = idx - floor(idx);
    // Linear interpolation - no undulations, kinks imperceptible in practice
    // Interpolate in sqrt domain for correct curve reconstruction
    float s0 = sqrt(max(v0, 0.0f));
    float s1 = sqrt(max(v1, 0.0f));
    float correctedS = lerp(s0, s1, t);
    float correctedY = correctedS * correctedS;
    // Scale all channels proportionally to preserve chromaticity
    if (Y < 1e-6f) return rgb;
    return rgb * (correctedY / Y);
}
)"
// Part 2b: ICTCP color space infrastructure (Dolby ICtCp for HDR)
// Based on Dolby white paper "What is ICtCp?" v7.1
// Provides perceptually uniform processing for tonemapping and grayscale
R"(
// PQ (ST.2084) constants
static const float PQ_m1 = 0.1593017578125f;   // 2610/16384
static const float PQ_m2 = 78.84375f;          // 2523/4096 * 128
static const float PQ_c1 = 0.8359375f;         // 3424/4096
static const float PQ_c2 = 18.8515625f;        // 2413/4096 * 32
static const float PQ_c3 = 18.6875f;           // 2392/4096 * 32

// Rec.2020 RGB to LMS matrix (Dolby paper page 4)
// Includes Hunt-Pointer-Estevez transform with 4% crosstalk
static const float3x3 Rec2020_to_LMS = {
    0.41210938f, 0.52392578f, 0.06396484f,   // 1688/4096, 2146/4096, 262/4096
    0.16674805f, 0.72045898f, 0.11279297f,   // 683/4096, 2951/4096, 462/4096
    0.02416992f, 0.07543945f, 0.90039063f    // 99/4096, 309/4096, 3688/4096
};

// LMS to Rec.2020 RGB matrix (inverse of above)
static const float3x3 LMS_to_Rec2020 = {
    3.43661000f, -2.50645000f,  0.06984000f,
   -0.79133000f,  1.98360000f, -0.19227000f,
   -0.02595000f, -0.09891000f,  1.12486000f
};

// L'M'S' to ICtCp matrix (Dolby paper page 6)
// I = intensity (luminance), CT = tritan (yellow-blue), CP = protan (red-green)
static const float3x3 LMSprime_to_ICtCp = {
    0.50000000f,  0.50000000f,  0.00000000f,  // 2048/4096, 2048/4096, 0
    1.61376953f, -3.32348633f,  1.70971680f,  // 6610/4096, -13613/4096, 7003/4096
    4.37817383f, -4.24560547f, -0.13256836f   // 17933/4096, -17390/4096, -543/4096
};

// ICtCp to L'M'S' matrix (Dolby paper page 13)
static const float3x3 ICtCp_to_LMSprime = {
    1.0f,  0.00860904f,  0.11102963f,
    1.0f, -0.00860904f, -0.11102963f,
    1.0f,  0.56003134f, -0.32062717f
};

// PQ OETF: Linear light (0-1 normalized to 10000 nits) -> PQ signal (0-1)
float3 Linear_to_PQ(float3 L) {
    float3 Y = max(L, 1e-10f);
    float3 Ym = pow(Y, PQ_m1);
    return pow((PQ_c1 + PQ_c2 * Ym) / (1.0f + PQ_c3 * Ym), PQ_m2);
}

// PQ EOTF: PQ signal (0-1) -> Linear light (0-1 normalized to 10000 nits)
float3 PQ_to_Linear(float3 pq) {
    float3 Vm = pow(max(pq, 1e-10f), 1.0f / PQ_m2);
    float3 t = max(Vm - PQ_c1, 0.0f) / max(PQ_c2 - PQ_c3 * Vm, 1e-10f);
    return pow(t, 1.0f / PQ_m1);
}

// Single-channel PQ for efficiency when only I channel needed
float Linear_to_PQ_scalar(float L) {
    float Y = max(L, 1e-10f);
    float Ym = pow(Y, PQ_m1);
    return pow((PQ_c1 + PQ_c2 * Ym) / (1.0f + PQ_c3 * Ym), PQ_m2);
}

float PQ_to_Linear_scalar(float pq) {
    float Vm = pow(max(pq, 1e-10f), 1.0f / PQ_m2);
    float t = max(Vm - PQ_c1, 0.0f) / max(PQ_c2 - PQ_c3 * Vm, 1e-10f);
    return pow(t, 1.0f / PQ_m1);
}

)"
// Part 3: HDR Tonemapping functions
R"(
float TonemapBT2390(float E, float Lw, float Lmax) {
    float KS = 1.5f * Lmax - 0.5f;
    KS = max(KS, 0.0f);
    if (E <= KS) return E;
    float t = saturate((E - KS) / (Lw - KS));
    float P0 = KS, P1 = Lmax, M0 = (Lw - KS), M1 = 0.0f;
    float t2 = t * t, t3 = t2 * t;
    float h00 = 2.0f * t3 - 3.0f * t2 + 1.0f;
    float h10 = t3 - 2.0f * t2 + t;
    float h01 = -2.0f * t3 + 3.0f * t2;
    float h11 = t3 - t2;
    return saturate(h00 * P0 + h10 * M0 + h01 * P1 + h11 * M1);
}

float TonemapSoftClip(float x, float targetPeak, float targetNits) {
    // Full range for SDR targets (no knee), shoulder-only for HDR
    float knee = (targetNits <= 203.0f) ? 0.0f : targetPeak * 0.8f;
    if (x <= knee) return x;
    float overshoot = x - knee;
    float headroom = targetPeak - knee;
    return knee + headroom * (1.0f - exp(-overshoot / headroom));
}

float TonemapReinhard(float x, float targetPeak, float targetNits) {
    // Full range for SDR targets (no knee), shoulder-only for HDR
    float knee = (targetNits <= 203.0f) ? 0.0f : targetPeak * 0.8f;
    if (x <= knee) return x;
    float overshoot = x - knee;
    float headroom = targetPeak - knee;
    // Reinhard-style hyperbolic compression of overshoot
    return knee + headroom * overshoot / (overshoot + headroom);
}

// Hard clip - simple clamp at target (for colorists to see clipping)
float TonemapHardClip(float x, float targetPeak, float targetNits) {
    return min(x, targetPeak);  // targetNits unused, signature matches other curves
}

// ═══════════════════════════════════════════════════════════════════════════════
// PQ-NATIVE TONEMAPPERS (spec-compliant, no linear conversion needed)
// These operate directly on PQ-encoded I channel values (0-1 range)
// ═══════════════════════════════════════════════════════════════════════════════

// BT.2390 EETF - PQ native (ITU-R spec defines this in PQ domain)
// Reference: ITU-R BT.2390-11, hdr-toys implementation
float TonemapBT2390_PQ(float I, float pqSourcePeak, float pqTargetPeak) {
    // Parameters in PQ domain (ib/ob = 0 for black)
    float iw = pqSourcePeak;
    float ow = pqTargetPeak;

    // Normalize to 0-1 relative to source peak
    float E = I / iw;

    // Calculate knee point and max luminance in normalized space
    float maxLum = ow / iw;
    float KS = 1.5f * maxLum - 0.5f;
    KS = max(KS, 0.0f);

    // Below knee: linear passthrough
    if (E <= KS) {
        return E * iw;  // Denormalize and return
    }

    // Above knee: Hermite spline rolloff (E2 segment)
    float t = (E - KS) / (1.0f - KS);
    float t2 = t * t;
    float t3 = t2 * t;

    // Hermite basis functions
    float h00 = 2.0f * t3 - 3.0f * t2 + 1.0f;
    float h10 = t3 - 2.0f * t2 + t;
    float h01 = -2.0f * t3 + 3.0f * t2;
    // h11 not needed (M1 = 0)

    // Spline evaluation: P0=KS, P1=maxLum, M0=(1-KS), M1=0
    float E_mapped = h00 * KS + h10 * (1.0f - KS) + h01 * maxLum;

    // Denormalize back to PQ range
    return clamp(E_mapped * iw, 0.0f, ow);
}

// Soft clip - PQ native (exponential rolloff)
float TonemapSoftClip_PQ(float I, float pqSourcePeak, float pqTargetPeak, float targetNits) {
    // For SDR targets, apply to full range; for HDR, 80% knee
    float pqKnee = (targetNits <= 203.0f) ? 0.0f : pqTargetPeak * 0.8f;

    if (I <= pqKnee) return I;

    float overshoot = I - pqKnee;
    float headroom = pqTargetPeak - pqKnee;

    // Exponential compression in PQ space
    return pqKnee + headroom * (1.0f - exp(-overshoot / headroom));
}

// Reinhard - PQ native (hyperbolic compression)
float TonemapReinhard_PQ(float I, float pqSourcePeak, float pqTargetPeak, float targetNits) {
    // For SDR targets, apply to full range; for HDR, 80% knee
    float pqKnee = (targetNits <= 203.0f) ? 0.0f : pqTargetPeak * 0.8f;

    if (I <= pqKnee) return I;

    float overshoot = I - pqKnee;
    float headroom = pqTargetPeak - pqKnee;

    // Reinhard hyperbolic compression in PQ space
    return pqKnee + headroom * overshoot / (overshoot + headroom);
}

// Hard clip - PQ native (trivial)
float TonemapHardClip_PQ(float I, float pqTargetPeak) {
    return min(I, pqTargetPeak);
}

// ITU-R BT.2446 Method A - logarithmic compression with piecewise curve
// Full range for SDR targets, shoulder-only for HDR
float TonemapBT2446A(float Y, float targetPeak, float targetNits) {
    // Full range for SDR targets (no knee), shoulder-only for HDR
    float knee = (targetNits <= 203.0f) ? 0.0f : targetPeak * 0.8f;
    if (Y <= knee) return Y;

    // For content above knee, apply BT.2446A-style compression
    // Remap input so knee=0, and compress the overshoot
    float overshoot = Y - knee;
    float maxOvershoot = 1.0f - knee;  // Maximum possible overshoot (up to 10000 nits)
    float headroom = targetPeak - knee;  // Available headroom to target

    // Normalize overshoot to 0-1 range
    float normalizedOvershoot = overshoot / maxOvershoot;

    // Apply BT.2446A-style compression to the overshoot
    // Gamma expansion
    float Yg = pow(normalizedOvershoot, 1.0f / 2.4f);

    // Perceptual parameters based on compression ratio
    float compressionRatio = maxOvershoot / headroom;
    float pHDR = 1.0f + 32.0f * pow(compressionRatio, 1.0f / 2.4f);
    float pSDR = 1.0f + 32.0f;  // Reference (1:1 mapping)

    // Logarithmic compression
    float Yp = log(1.0f + (pHDR - 1.0f) * Yg) / log(pHDR);

    // Piecewise curve (three regions)
    float Yc;
    if (Yp <= 0.7399f)
        Yc = Yp * 1.0770f;
    else if (Yp < 0.9909f)
        Yc = Yp * (-1.1510f * Yp + 2.7811f) - 0.6302f;
    else
        Yc = Yp * 0.5000f + 0.5000f;

    // Inverse log
    float Ysdr = (pow(pSDR, Yc) - 1.0f) / (pSDR - 1.0f);

    // Gamma compression
    float compressed = pow(max(Ysdr, 0.0f), 2.4f);

    // Scale compressed overshoot to headroom and add to knee
    return knee + compressed * headroom;
}

// ICTCP Tonemapping: operates on I (intensity) channel only
// CT and CP (chroma) are preserved, ensuring hue/saturation stability
// This is superior to RGB luminance-based tonemapping which can cause hue shifts
//
// Uses PQ-native curves (spec-compliant BT.2390, minimal pow() overhead)
// Only BT.2446A uses linear-space due to complex gamma operations
float3 ApplyTonemappingICtCp(float3 ictcp) {
    if (tonemapEnabled < 0.5f) return ictcp;

    float I = ictcp.x;
    if (I <= 0.0f) return ictcp;

    // Get source peak: dynamic (from peak texture) or user-specified
    // Dynamic tonemapping disabled at ≤203 nits (SDR reference white) - use static instead
    float sourcePeakNits;
    if (tonemapDynamic > 0.5f && tonemapTargetPeak > 203.0f) {
        float detectedPeak = max(peakTexture.Load(int3(0, 0, 0)), 203.0f);
        float minSourcePeak = tonemapTargetPeak * 1.25f;
        sourcePeakNits = max(detectedPeak, minSourcePeak);
    } else {
        // Static mode: use user-specified peak, fallback to 1000 if not set
        // 1000 nits covers 99.9% of HDR content (typical HDR10 mastering peak)
        sourcePeakNits = (tonemapSourcePeak > 0.0f) ? tonemapSourcePeak : 1000.0f;
    }

    // If content already fits display, no tonemapping needed
    if (sourcePeakNits <= tonemapTargetPeak) return ictcp;

    // Convert peaks to PQ domain (2 pow() ops total - much better than 4)
    float pqSourcePeak = Linear_to_PQ_scalar(sourcePeakNits / 10000.0f);
    float pqTargetPeak = Linear_to_PQ_scalar(tonemapTargetPeak / 10000.0f);

    // Apply PQ-native curve directly on I channel
    float I_mapped;
    if (tonemapCurve < 0.5f) {
        // BT.2390 EETF - spec-native PQ implementation (ITU-R BT.2390)
        I_mapped = TonemapBT2390_PQ(I, pqSourcePeak, pqTargetPeak);
    }
    else if (tonemapCurve < 1.5f) {
        // Soft clip - PQ native
        I_mapped = TonemapSoftClip_PQ(I, pqSourcePeak, pqTargetPeak, tonemapTargetPeak);
    }
    else if (tonemapCurve < 2.5f) {
        // Reinhard - PQ native
        I_mapped = TonemapReinhard_PQ(I, pqSourcePeak, pqTargetPeak, tonemapTargetPeak);
    }
    else if (tonemapCurve < 3.5f) {
        // BT.2446A - linear-space (complex gamma operations don't translate to PQ)
        float nits = PQ_to_Linear_scalar(I) * 10000.0f;
        float normalized = nits / sourcePeakNits;
        float targetNormalized = tonemapTargetPeak / sourcePeakNits;
        float mapped = TonemapBT2446A(normalized, targetNormalized, tonemapTargetPeak);
        I_mapped = Linear_to_PQ_scalar(mapped * sourcePeakNits / 10000.0f);
    }
    else {
        // Hard clip - trivial PQ native
        I_mapped = TonemapHardClip_PQ(I, pqTargetPeak);
    }

    // Return with CT/CP unchanged - hue and saturation preserved!
    return float3(I_mapped, ictcp.y, ictcp.z);
}

// ICTCP Grayscale: operates on I (intensity) channel only
// I channel is true perceptual luminance (r=0.998 correlation per Dolby paper)
// Much more accurate than the old max-channel approximation
float3 ApplyGrayscaleICtCp(float3 ictcp) {
    if (grayscaleEnabled < 0.5f) return ictcp;

    float I = ictcp.x;
    if (I < 1e-6f) return ictcp;

    // Convert grayscalePeakNits to PQ value for scaling
    // Guard against zero/negative peak (would cause division by zero)
    float pqPeak = Linear_to_PQ_scalar(max(grayscalePeakNits, 1.0f) / 10000.0f);

    float scaledI = I / pqPeak;
    float correctedI;

    if (scaledI <= 1.0f) {
        // Within calibration range - linear interpolation
        // No undulations, kinks imperceptible in practice
        float idx = scaledI * (grayscalePoints - 1.0f);
        int i0 = (int)floor(idx);
        int i1 = min(i0 + 1, (int)grayscalePoints - 1);
        float t = idx - floor(idx);
        float v0 = grayscale[i0 / 4][i0 % 4];
        float v1 = grayscale[i1 / 4][i1 % 4];
        correctedI = lerp(v0, v1, t) * pqPeak;
    } else {
        // Above peak - apply same correction factor as last point
        int lastIdx = (int)grayscalePoints - 1;
        float lastCurveValue = grayscale[lastIdx / 4][lastIdx % 4];
        correctedI = lastCurveValue * I;
    }

    // Return with CT/CP unchanged
    return float3(correctedI, ictcp.y, ictcp.z);
}

// ICTCP Dithering: adds perceptually uniform blue noise
// Dithering in ICtCp space ensures noise is distributed according to human perception
// I channel gets more dither (luminance banding is most visible in dark areas)
// CT/CP get less dither (chroma is less sensitive)
float3 ApplyDitherICtCp(float3 ictcp, float2 pos) {
    float2 noiseUV = pos / 64.0f;

    // Sample blue noise texture at different offsets for decorrelated I/CT/CP noise
    float noiseI  = blueNoiseTexture.Sample(wrapSampler, noiseUV);
    float noiseCT = blueNoiseTexture.Sample(wrapSampler, noiseUV + float2(0.5f, 0.0f));
    float noiseCP = blueNoiseTexture.Sample(wrapSampler, noiseUV + float2(0.0f, 0.5f));

    // Dither amplitude: ~1 LSB in 10-bit PQ = 1/1023 ≈ 0.001
    // I channel: full amplitude (luminance banding most visible)
    // CT/CP: half amplitude (chroma less sensitive, avoid color noise)
    float ditherI  = (noiseI  - 0.5f) / 1023.0f;
    float ditherCT = (noiseCT - 0.5f) / 2046.0f;
    float ditherCP = (noiseCP - 0.5f) / 2046.0f;

    return ictcp + float3(ditherI, ditherCT, ditherCP);
}
)"
// Part 4: LUT sampling functions
R"(
float3 SampleLUTTetrahedral(float3 rgb) {
    float3 scaled = saturate(rgb) * (lutSize - 1.0f);
    float3 base = floor(scaled);
    float3 frac = scaled - base;
    float3 texelSize = 1.0f / lutSize;
    float3 baseUV = (base + 0.5f) * texelSize;
    float3 c000 = lutTexture.SampleLevel(pointSampler, baseUV, 0).rgb;
    float3 c111 = lutTexture.SampleLevel(pointSampler, baseUV + texelSize, 0).rgb;
    float3 result;
    if (frac.r >= frac.g) {
        if (frac.g >= frac.b) {
            float3 c100 = lutTexture.SampleLevel(pointSampler, baseUV + float3(texelSize.x, 0, 0), 0).rgb;
            float3 c110 = lutTexture.SampleLevel(pointSampler, baseUV + float3(texelSize.x, texelSize.y, 0), 0).rgb;
            result = c000 + (c100 - c000) * frac.r + (c110 - c100) * frac.g + (c111 - c110) * frac.b;
        } else if (frac.r >= frac.b) {
            float3 c100 = lutTexture.SampleLevel(pointSampler, baseUV + float3(texelSize.x, 0, 0), 0).rgb;
            float3 c101 = lutTexture.SampleLevel(pointSampler, baseUV + float3(texelSize.x, 0, texelSize.z), 0).rgb;
            result = c000 + (c100 - c000) * frac.r + (c101 - c100) * frac.b + (c111 - c101) * frac.g;
        } else {
            float3 c001 = lutTexture.SampleLevel(pointSampler, baseUV + float3(0, 0, texelSize.z), 0).rgb;
            float3 c101 = lutTexture.SampleLevel(pointSampler, baseUV + float3(texelSize.x, 0, texelSize.z), 0).rgb;
            result = c000 + (c001 - c000) * frac.b + (c101 - c001) * frac.r + (c111 - c101) * frac.g;
        }
    } else {
        if (frac.b >= frac.g) {
            float3 c001 = lutTexture.SampleLevel(pointSampler, baseUV + float3(0, 0, texelSize.z), 0).rgb;
            float3 c011 = lutTexture.SampleLevel(pointSampler, baseUV + float3(0, texelSize.y, texelSize.z), 0).rgb;
            result = c000 + (c001 - c000) * frac.b + (c011 - c001) * frac.g + (c111 - c011) * frac.r;
        } else if (frac.r >= frac.b) {
            float3 c010 = lutTexture.SampleLevel(pointSampler, baseUV + float3(0, texelSize.y, 0), 0).rgb;
            float3 c110 = lutTexture.SampleLevel(pointSampler, baseUV + float3(texelSize.x, texelSize.y, 0), 0).rgb;
            result = c000 + (c010 - c000) * frac.g + (c110 - c010) * frac.r + (c111 - c110) * frac.b;
        } else {
            float3 c010 = lutTexture.SampleLevel(pointSampler, baseUV + float3(0, texelSize.y, 0), 0).rgb;
            float3 c011 = lutTexture.SampleLevel(pointSampler, baseUV + float3(0, texelSize.y, texelSize.z), 0).rgb;
            result = c000 + (c010 - c000) * frac.g + (c011 - c010) * frac.b + (c111 - c011) * frac.r;
        }
    }
    return result;
}

float3 SampleLUTTrilinear(float3 rgb) {
    float3 lutUV = (saturate(rgb) * (lutSize - 1.0f) + 0.5f) / lutSize;
    return lutTexture.Sample(linearSampler, lutUV).rgb;
}

float3 SampleLUT(float3 rgb) {
    if (tetrahedralInterp > 0.5f) return SampleLUTTetrahedral(rgb);
    else return SampleLUTTrilinear(rgb);
}
)"
// Part 5: Main function - HDR path (ICTCP pipeline)
// Pipeline: scRGB -> Rec.2020 -> LMS -> PQ -> ICtCp -> [process] -> PQ RGB -> LUT -> scRGB
R"(
float4 main(float4 pos : SV_POSITION, float2 uv : TEXCOORD0) : SV_TARGET {
    float4 color = captureTexture.Sample(pointSampler, uv);
    if (isHDR > 0.5) {
        float3 input = color.rgb;

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 1: Input conditioning (Linear BT.709 / scRGB)
        // ═══════════════════════════════════════════════════════════════════════

        // Desktop gamma correction (sRGB EOTF -> 2.2 power law)
        // Fixes Windows using sRGB EOTF - applies to ALL SDR-range content (sub 80 nits)
        // Sign preserved for wide-gamut; HDR highlights (>80 nits) pass through unchanged
        if (desktopGamma > 0.5) {
            float3 absInput = abs(input);
            float3 signInput = sign(input);
            float3 sdrPart = min(absInput, 1.0);
            float3 hdrPart = max(absInput - 1.0, 0.0);
            float3 srgbEncoded = lerp(12.92 * sdrPart,
                1.055 * pow(max(sdrPart, 0.0001), 1.0 / 2.4) - 0.055,
                step(0.0031308, sdrPart));
            float3 corrected = pow(max(srgbEncoded, 0.0001), 2.2);
            input = (corrected + hdrPart) * signInput;
        }

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 2: Color space conversion to Rec.2020 (Linear)
        // ═══════════════════════════════════════════════════════════════════════

        // BT.709 -> Rec.2020 matrix
        float3 rec2020 = float3(
            dot(input, float3(0.6274039f, 0.3292830f, 0.0433131f)),
            dot(input, float3(0.0690973f, 0.9195404f, 0.0113623f)),
            dot(input, float3(0.0163914f, 0.0880133f, 0.8955953f)));

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 3: Display calibration (Linear Rec.2020)
        // ═══════════════════════════════════════════════════════════════════════

        // Primaries correction: adjusts for display's actual vs ideal primaries
        // Includes Bradford chromatic adaptation for white point correction
        rec2020 = ApplyPrimariesMatrix(rec2020);

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 4: Convert to ICTCP (perceptually uniform space)
        // ═══════════════════════════════════════════════════════════════════════

        // Rec.2020 -> LMS (Hunt-Pointer-Estevez with crosstalk)
        // Note: Wide-gamut Rec.2020 can produce negative LMS for out-of-gamut colors.
        // These are clipped to near-zero during PQ encoding (intentional - no valid PQ for negative light).
        float3 lms = mul(Rec2020_to_LMS, rec2020);

        // LMS -> L'M'S' (PQ encode, normalized to 10000 nits)
        // Input is scRGB-like where 1.0 = 80 nits
        float3 lmsPQ = Linear_to_PQ(lms * (80.0f / 10000.0f));

        // L'M'S' -> ICtCp
        float3 ictcp = mul(LMSprime_to_ICtCp, lmsPQ);

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 5: ICTCP processing (perceptually uniform operations)
        // All operations on I channel preserve hue/saturation (CT/CP unchanged)
        // ═══════════════════════════════════════════════════════════════════════

        // Grayscale correction on I channel (display calibration - constant)
        // Applied first: calibrate display response before content-dependent processing
        ictcp = ApplyGrayscaleICtCp(ictcp);

        // Tonemapping on I channel (content preference - dynamic)
        // Applied after grayscale: compress dynamic range on calibrated display
        ictcp = ApplyTonemappingICtCp(ictcp);

        // Dithering in ICtCp space (perceptually uniform noise distribution)
        ictcp = ApplyDitherICtCp(ictcp, pos.xy);

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 6: Convert to PQ Rec.2020 RGB for LUT
        // ═══════════════════════════════════════════════════════════════════════

        // ICtCp -> L'M'S'
        float3 lmsPQ2 = mul(ICtCp_to_LMSprime, ictcp);

        // L'M'S' -> LMS (PQ decode)
        float3 lms2 = PQ_to_Linear(lmsPQ2);

        // LMS -> Rec.2020 linear
        float3 rec2020_linear = mul(LMS_to_Rec2020, lms2);

        // Rec.2020 linear -> PQ Rec.2020 (for LUT)
        float3 pqRGB = Linear_to_PQ(rec2020_linear);

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 7: LUT application (PQ Rec.2020)
        // ═══════════════════════════════════════════════════════════════════════

        float3 lutResult;
        if (usePassthrough > 0.5) {
            lutResult = pqRGB;
        } else {
            lutResult = SampleLUT(pqRGB);
        }

        // ═══════════════════════════════════════════════════════════════════════
        // STAGE 8: Output conversion (PQ -> Linear -> scRGB)
        // ═══════════════════════════════════════════════════════════════════════

        // PQ -> Linear Rec.2020
        float3 linearRec2020 = PQ_to_Linear(lutResult) * (10000.0f / 80.0f);

        // Rec.2020 -> BT.709 (scRGB output)
        float3 result = float3(
            dot(linearRec2020, float3(1.6604910f, -0.5876411f, -0.0728499f)),
            dot(linearRec2020, float3(-0.1245505f, 1.1328999f, -0.0083494f)),
            dot(linearRec2020, float3(-0.0181508f, -0.1005789f, 1.1187297f)));

        return float4(result, 1.0);
    }
)"
// Part 6: Main function - SDR path
R"(
    else {
        float3 input = color.rgb;
        // Primaries matrix operates in linear space
        // Decode/encode both use 2.2 to match display reality (no gamma curve change)
        if (useManualCorrection > 0.5) {
            // Decode with gamma 2.2 (matching display's actual EOTF)
            float3 lin = pow(max(input, 0.0), 2.2);
            // Apply primaries matrix in linear space
            // Includes Bradford chromatic adaptation for white point correction
            float3x3 mat = float3x3(primariesRow0.xyz, primariesRow1.xyz, primariesRow2.xyz);
            lin = mul(mat, lin);
            // Clamp to avoid negative values from out-of-gamut colors
            lin = max(lin, 0.0);
            // Encode with gamma 2.2 (same as decode = no gamma change)
            input = pow(lin, 1.0 / 2.2);
            // Clamp to valid range after matrix
            input = saturate(input);
        }
        input = ApplyGrayscaleCorrection(input);
        input = Apply24Gamma(input);
        float3 corrected;
        if (usePassthrough > 0.5) corrected = input;
        else corrected = SampleLUT(input);
        // Dithering
        float2 noiseUV = pos.xy / 64.0;
        float noise = blueNoiseTexture.Sample(wrapSampler, noiseUV);
        float dither = (noise - 0.5) / 1024.0;
        float3 dithered = corrected.rgb + dither;
        return float4(dithered, 1.0);
    }
}
)";

// Compute shader for dynamic peak detection
// Single-pass sparse sampling + reduction + temporal smoothing
inline const char* g_csSource = R"(
Texture2D<float4> inputTexture : register(t0);
RWTexture2D<float> peakOutput : register(u0);

cbuffer PeakParams : register(b0) {
    uint frameWidth;
    uint frameHeight;
    float riseRate;    // Exponential rise rate (e.g., 0.3)
    float fallRate;    // Exponential fall rate (e.g., 0.05)
    float maxRisePerFrame;  // Slew limit for rise (nits/frame, e.g., 100)
    float maxFallPerFrame;  // Slew limit for fall (nits/frame, e.g., 50)
    float2 _padding;
};

groupshared float sharedMax[256];

// Single pass: sparse sample + reduction + temporal smoothing
// Dispatch with (1, 1, 1) groups, 256 threads
[numthreads(256, 1, 1)]
void main(uint3 GTid : SV_GroupThreadID) {
    // Each thread samples a grid of pixels across the image
    // 256 threads, each samples ~16 points = 4096 sample points total
    float localMax = 0.0f;

    uint samplesPerThread = 16;
    uint totalSamples = 256 * samplesPerThread;

    for (uint i = 0; i < samplesPerThread; i++) {
        uint sampleIdx = GTid.x * samplesPerThread + i;

        // Distribute samples across the image in a grid pattern
        uint gridSize = 64;  // 64x64 grid = 4096 points
        uint gx = sampleIdx % gridSize;
        uint gy = sampleIdx / gridSize;

        uint px = (gx * frameWidth) / gridSize;
        uint py = (gy * frameHeight) / gridSize;

        if (px < frameWidth && py < frameHeight) {
            float4 pixel = inputTexture.Load(int3(px, py, 0));
            float Y = dot(pixel.rgb, float3(0.2126f, 0.7152f, 0.0722f));
            float nits = Y * 80.0f;
            localMax = max(localMax, nits);
        }
    }

    sharedMax[GTid.x] = localMax;
    GroupMemoryBarrierWithGroupSync();

    // Parallel reduction
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (GTid.x < stride) {
            sharedMax[GTid.x] = max(sharedMax[GTid.x], sharedMax[GTid.x + stride]);
        }
        GroupMemoryBarrierWithGroupSync();
    }

    // Thread 0 applies temporal smoothing with slew limiting
    if (GTid.x == 0) {
        float framePeak = sharedMax[0];
        float prevPeak = peakOutput[uint2(0, 0)];

        // Initialize on first frame
        if (prevPeak <= 0.0f) prevPeak = framePeak;

        // Hybrid: exponential smoothing + slew rate limiting
        // Exponential gives smooth small changes, slew limit prevents jarring jumps
        float target;
        float maxDelta;
        if (framePeak > prevPeak) {
            target = lerp(prevPeak, framePeak, riseRate);
            maxDelta = maxRisePerFrame;
        } else {
            target = lerp(prevPeak, framePeak, fallRate);
            maxDelta = maxFallPerFrame;
        }

        // Apply slew limit - smooth transitions even for large peak changes
        float smoothedPeak = clamp(target, prevPeak - maxDelta, prevPeak + maxDelta);

        // Upper clamp prevents overflow from corrupted frames
        smoothedPeak = clamp(smoothedPeak, 0.0f, 10000.0f);
        peakOutput[uint2(0, 0)] = smoothedPeak;
    }
}
)";

// Compute shader for frame analysis
// Samples a 64x64 grid across the frame to gather statistics
inline const char* g_analysisCSSource = R"(
Texture2D<float4> inputTexture : register(t0);
RWStructuredBuffer<uint> output : register(u0);

cbuffer AnalysisParams : register(b0) {
    uint frameWidth;
    uint frameHeight;
    uint isHDR;
    uint pad;
};

// Output buffer layout (16 uint values = 64 bytes):
// [0] peakNits (as float bits, luminance-based)
// [1] minNits (as float bits, luminance-based)
// [2] sumNits (as float bits, divided by totalPixels later)
// [3] totalPixels
// [4] pixelsRec709
// [5] pixelsP3Only
// [6] pixelsRec2020Only
// [7] pixelsOutOfGamut
// [8] pixelsClipBlack
// [9] pixelsClipWhite
// [10-14] histogram (0-203, 203-1k, 1k-2k, 2k-4k, 4k+)
// [15] minNonZeroNits (as float bits, min excluding <0.1 nit)

groupshared float sharedPeak[256];
groupshared float sharedMin[256];
groupshared float sharedMinNonZero[256];
groupshared float sharedSum[256];
groupshared uint sharedRec709[256];
groupshared uint sharedP3Only[256];
groupshared uint sharedRec2020Only[256];
groupshared uint sharedOutOfGamut[256];
groupshared uint sharedClipBlack[256];
groupshared uint sharedClipWhite[256];
groupshared uint sharedHist0[256];
groupshared uint sharedHist1[256];
groupshared uint sharedHist2[256];
groupshared uint sharedHist3[256];
groupshared uint sharedHist4[256];

// BT.709 to Display P3 matrix (for gamut checking)
static const float3x3 BT709_to_P3 = {
    0.8225, 0.1774, 0.0000,
    0.0332, 0.9669, 0.0000,
    0.0171, 0.0724, 0.9108
};

// BT.709 to Rec.2020 matrix (for gamut checking)
static const float3x3 BT709_to_2020 = {
    0.6274, 0.3293, 0.0433,
    0.0691, 0.9195, 0.0114,
    0.0164, 0.0880, 0.8956
};

bool IsInGamut(float3 rgb) {
    // Gamut check: can this color be represented with positive primaries?
    // Only check for negative values - values > 1 are HDR brightness, not gamut
    // Small tolerance for numerical precision
    return all(rgb >= -0.005f);
}

[numthreads(256, 1, 1)]
void main(uint3 GTid : SV_GroupThreadID) {
    // Initialize local accumulators
    float localPeak = 0.0f;
    float localMin = 100000.0f;
    float localMinNonZero = 100000.0f;  // Min excluding near-black pixels
    float localSum = 0.0f;
    uint localRec709 = 0, localP3Only = 0, localRec2020Only = 0, localOutOfGamut = 0;
    uint localClipBlack = 0, localClipWhite = 0;
    uint localHist0 = 0, localHist1 = 0, localHist2 = 0, localHist3 = 0, localHist4 = 0;

    // Aspect-ratio-aware grid: ~4096 samples distributed proportionally
    // gridX = sqrt(4096 * aspectRatio), gridY = sqrt(4096 / aspectRatio)
    float aspectRatio = (float)frameWidth / (float)frameHeight;
    uint gridX = (uint)sqrt(4096.0f * aspectRatio);
    uint gridY = (uint)sqrt(4096.0f / aspectRatio);
    if (gridX < 1) gridX = 1;
    if (gridY < 1) gridY = 1;
    uint totalSamples = gridX * gridY;

    // Each thread samples totalSamples/256 points (rounded up)
    uint samplesPerThread = (totalSamples + 255) / 256;
    for (uint i = 0; i < samplesPerThread; i++) {
        uint sampleIdx = GTid.x * samplesPerThread + i;
        if (sampleIdx >= totalSamples) break;

        uint gx = sampleIdx % gridX;
        uint gy = sampleIdx / gridX;
        uint px = (gx * frameWidth) / gridX;
        uint py = (gy * frameHeight) / gridY;

        if (px < frameWidth && py < frameHeight) {
            float4 pixel = inputTexture.Load(int3(px, py, 0));
            float3 rgb = pixel.rgb;

            // Calculate luminance (Y) for all metrics
            float Y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
            float nitsY = Y * 80.0f;  // scRGB: 1.0 = 80 nits

            localPeak = max(localPeak, nitsY);     // Peak uses luminance (white level)
            localMin = min(localMin, nitsY);       // Min uses luminance
            if (nitsY > 0.1f) {                    // Min>0 excludes near-black (<0.1 nit)
                localMinNonZero = min(localMinNonZero, nitsY);
            }
            localSum += nitsY;                      // FALL/APL uses luminance average

            // Gamut classification
            // SDR mode: everything is Rec.709 by definition (8-bit sRGB capture)
            // HDR mode: check for wide gamut content (scRGB can represent P3/Rec.2020)
            if (!isHDR) {
                // SDR - all content is Rec.709
                localRec709++;
            } else {
                // HDR - classify by gamut
                // Skip very dark pixels (< 0.1 nit) - no meaningful color info
                float luminanceFloor = 0.00125f;  // ~0.1 nit

                if (Y < luminanceFloor) {
                    localRec709++;
                } else if (IsInGamut(rgb)) {
                    // All components >= 0: fits in Rec.709
                    localRec709++;
                } else {
                    // Has negative values - check wider gamuts
                    float3 p3 = mul(BT709_to_P3, rgb);
                    if (IsInGamut(p3)) {
                        localP3Only++;
                    } else {
                        float3 r2020 = mul(BT709_to_2020, rgb);
                        if (IsInGamut(r2020)) {
                            localRec2020Only++;
                        } else {
                            localOutOfGamut++;
                        }
                    }
                }
            }

            // SDR clipping detection
            if (!isHDR) {
                if (all(rgb < 1.0f/255.0f)) localClipBlack++;
                if (all(rgb > 254.0f/255.0f)) localClipWhite++;
            }

            // HDR histogram (luminance distribution)
            if (isHDR) {
                if (nitsY < 203.0f) localHist0++;        // SDR range
                else if (nitsY < 1000.0f) localHist1++;  // 203-1000
                else if (nitsY < 2000.0f) localHist2++;  // 1000-2000
                else if (nitsY < 4000.0f) localHist3++;  // 2000-4000
                else localHist4++;                        // 4000+
            }
        }
    }

    // Store to shared memory
    sharedPeak[GTid.x] = localPeak;
    sharedMin[GTid.x] = localMin;
    sharedMinNonZero[GTid.x] = localMinNonZero;
    sharedSum[GTid.x] = localSum;
    sharedRec709[GTid.x] = localRec709;
    sharedP3Only[GTid.x] = localP3Only;
    sharedRec2020Only[GTid.x] = localRec2020Only;
    sharedOutOfGamut[GTid.x] = localOutOfGamut;
    sharedClipBlack[GTid.x] = localClipBlack;
    sharedClipWhite[GTid.x] = localClipWhite;
    sharedHist0[GTid.x] = localHist0;
    sharedHist1[GTid.x] = localHist1;
    sharedHist2[GTid.x] = localHist2;
    sharedHist3[GTid.x] = localHist3;
    sharedHist4[GTid.x] = localHist4;
    GroupMemoryBarrierWithGroupSync();

    // Parallel reduction
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (GTid.x < stride) {
            sharedPeak[GTid.x] = max(sharedPeak[GTid.x], sharedPeak[GTid.x + stride]);
            sharedMin[GTid.x] = min(sharedMin[GTid.x], sharedMin[GTid.x + stride]);
            sharedMinNonZero[GTid.x] = min(sharedMinNonZero[GTid.x], sharedMinNonZero[GTid.x + stride]);
            sharedSum[GTid.x] += sharedSum[GTid.x + stride];
            sharedRec709[GTid.x] += sharedRec709[GTid.x + stride];
            sharedP3Only[GTid.x] += sharedP3Only[GTid.x + stride];
            sharedRec2020Only[GTid.x] += sharedRec2020Only[GTid.x + stride];
            sharedOutOfGamut[GTid.x] += sharedOutOfGamut[GTid.x + stride];
            sharedClipBlack[GTid.x] += sharedClipBlack[GTid.x + stride];
            sharedClipWhite[GTid.x] += sharedClipWhite[GTid.x + stride];
            sharedHist0[GTid.x] += sharedHist0[GTid.x + stride];
            sharedHist1[GTid.x] += sharedHist1[GTid.x + stride];
            sharedHist2[GTid.x] += sharedHist2[GTid.x + stride];
            sharedHist3[GTid.x] += sharedHist3[GTid.x + stride];
            sharedHist4[GTid.x] += sharedHist4[GTid.x + stride];
        }
        GroupMemoryBarrierWithGroupSync();
    }

    // Thread 0 writes output
    if (GTid.x == 0) {
        // Recalculate grid size for totalPixels (same as above)
        float ar = (float)frameWidth / (float)frameHeight;
        uint gX = (uint)sqrt(4096.0f * ar);
        uint gY = (uint)sqrt(4096.0f / ar);
        if (gX < 1) gX = 1;
        if (gY < 1) gY = 1;

        output[0] = asuint(sharedPeak[0]);
        output[1] = asuint(sharedMin[0]);
        output[2] = asuint(sharedSum[0]);  // Will divide by totalPixels in CPU code
        output[3] = gX * gY;  // totalPixels (aspect-ratio-aware grid)
        output[4] = sharedRec709[0];
        output[5] = sharedP3Only[0];
        output[6] = sharedRec2020Only[0];
        output[7] = sharedOutOfGamut[0];
        output[8] = sharedClipBlack[0];
        output[9] = sharedClipWhite[0];
        output[10] = sharedHist0[0];
        output[11] = sharedHist1[0];
        output[12] = sharedHist2[0];
        output[13] = sharedHist3[0];
        output[14] = sharedHist4[0];
        output[15] = asuint(sharedMinNonZero[0]);  // Min excluding near-black
    }
}
)";
