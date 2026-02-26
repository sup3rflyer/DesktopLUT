// DesktopLUT - mhc.cpp
// MHC2 ICC profile generation, installation, and management
//
// Generates ICC v4 profiles with MHC2 tag for GPU scanout-level color correction.
// The MHC2 tag contains a 3x4 XYZ matrix and three 1D LUTs (R, G, B) that the
// Windows display driver applies at the GPU scanout stage (after all rendering).
//
// References:
// - ICC.1:2022 (ICC v4.4 specification)
// - MHC2Gen (open source MHC2 profile generator)
// - Windows Color Management API documentation

#include "mhc.h"
#include "color.h"
#include "globals.h"
#include "displayconfig.h"
#include <iostream>
#include <fstream>
#include <cstring>
#include <cmath>
#include <algorithm>
#include <set>
#include <wincrypt.h>

#pragma comment(lib, "Advapi32.lib")

// ============================================================================
// SECTION: ICC Binary Helpers
// ============================================================================

// Convert float to ICC s15Fixed16Number (multiply by 65536, round)
static int32_t FloatToS15Fixed16(float f) {
    return (int32_t)(f * 65536.0f + (f >= 0 ? 0.5f : -0.5f));
}

// Write 32-bit big-endian value
static void WriteBE32(std::vector<uint8_t>& buf, size_t offset, uint32_t val) {
    buf[offset + 0] = (uint8_t)(val >> 24);
    buf[offset + 1] = (uint8_t)(val >> 16);
    buf[offset + 2] = (uint8_t)(val >> 8);
    buf[offset + 3] = (uint8_t)(val);
}

// Append 32-bit big-endian value
static void AppendBE32(std::vector<uint8_t>& buf, uint32_t val) {
    buf.push_back((uint8_t)(val >> 24));
    buf.push_back((uint8_t)(val >> 16));
    buf.push_back((uint8_t)(val >> 8));
    buf.push_back((uint8_t)(val));
}

// Write 16-bit big-endian value
static void WriteBE16(std::vector<uint8_t>& buf, size_t offset, uint16_t val) {
    buf[offset + 0] = (uint8_t)(val >> 8);
    buf[offset + 1] = (uint8_t)(val);
}

// Append 16-bit big-endian value
static void AppendBE16(std::vector<uint8_t>& buf, uint16_t val) {
    buf.push_back((uint8_t)(val >> 8));
    buf.push_back((uint8_t)(val));
}

// Make a 4-byte ICC signature from string
static uint32_t MakeSig(const char s[4]) {
    return ((uint32_t)(uint8_t)s[0] << 24) |
           ((uint32_t)(uint8_t)s[1] << 16) |
           ((uint32_t)(uint8_t)s[2] << 8) |
           ((uint32_t)(uint8_t)s[3]);
}

// Pad buffer to 4-byte alignment
static void PadTo4(std::vector<uint8_t>& buf) {
    while (buf.size() % 4 != 0) {
        buf.push_back(0);
    }
}

// ============================================================================
// SECTION: ICC Tag Writers
// ============================================================================

// Write XYZ tag (20 bytes: 'XYZ ' + reserved + 3x s15Fixed16)
static void WriteXYZTag(std::vector<uint8_t>& buf, float X, float Y, float Z) {
    AppendBE32(buf, MakeSig("XYZ "));  // type signature
    AppendBE32(buf, 0);                 // reserved
    AppendBE32(buf, (uint32_t)FloatToS15Fixed16(X));
    AppendBE32(buf, (uint32_t)FloatToS15Fixed16(Y));
    AppendBE32(buf, (uint32_t)FloatToS15Fixed16(Z));
}

// Write curv tag with gamma value (12 bytes: 'curv' + reserved + count=1 + gamma as u8Fixed8)
static void WriteCurvTagGamma(std::vector<uint8_t>& buf, float gamma) {
    AppendBE32(buf, MakeSig("curv"));   // type signature
    AppendBE32(buf, 0);                 // reserved
    AppendBE32(buf, 1);                 // count = 1 (parametric gamma)
    uint16_t g = (uint16_t)(gamma * 256.0f + 0.5f);  // u8Fixed8
    AppendBE16(buf, g);
    PadTo4(buf);
}

// Write mluc (multi-localized Unicode) tag
static void WriteMlucTag(std::vector<uint8_t>& buf, const std::wstring& text) {
    AppendBE32(buf, MakeSig("mluc"));   // type signature
    AppendBE32(buf, 0);                 // reserved
    AppendBE32(buf, 1);                 // number of records
    AppendBE32(buf, 12);               // record size
    // Record: language 'en' (ISO 639-1), country 'US' (ISO 3166-1)
    AppendBE16(buf, ('e' << 8) | 'n');   // 0x656E
    AppendBE16(buf, ('U' << 8) | 'S');   // 0x5553
    uint32_t strLen = (uint32_t)(text.size() * 2);
    AppendBE32(buf, strLen);            // string length in bytes
    AppendBE32(buf, 28);               // offset from start of tag to string data
    // String data (UTF-16BE)
    for (wchar_t ch : text) {
        AppendBE16(buf, (uint16_t)ch);
    }
    PadTo4(buf);
}

// Write sf32 tag (chromatic adaptation matrix - 9 s15Fixed16 values)
static void WriteSf32Tag(std::vector<uint8_t>& buf, const float mat[9]) {
    AppendBE32(buf, MakeSig("sf32"));   // type signature
    AppendBE32(buf, 0);                 // reserved
    for (int i = 0; i < 9; i++) {
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(mat[i]));
    }
}

// Write text tag (for MSCA validation metadata)
static void WriteTextTag(std::vector<uint8_t>& buf, const char* text) {
    AppendBE32(buf, MakeSig("text"));   // type signature
    AppendBE32(buf, 0);                 // reserved
    size_t len = strlen(text);
    for (size_t i = 0; i < len; i++) {
        buf.push_back((uint8_t)text[i]);
    }
    buf.push_back(0);  // null terminator
    PadTo4(buf);
}

// ============================================================================
// SECTION: MHC2 Tag Writer
// ============================================================================

// Write MHC2 tag
// Header (36 bytes) + 3x4 matrix (48 bytes) + 3x LUT (3 * (8 + lutSize*4))
// Format matches dantmnf/MHC2Gen (reference implementation)
static void WriteMHC2Tag(std::vector<uint8_t>& buf, const float matrix[12],
                          const float* lutR, const float* lutG, const float* lutB,
                          int lutSize, bool isHDR, float peakNits = 80.0f) {
    size_t tagStart = buf.size();

    // MHC2 header (36 bytes = 9 x uint32)
    AppendBE32(buf, MakeSig("MHC2"));   // type signature
    AppendBE32(buf, 0);                 // reserved
    AppendBE32(buf, (uint32_t)lutSize); // LUT entries per channel
    // MinCLL/MaxCLL in nits (s15Fixed16)
    // SDR: 0.5 / 80.0 (standard SDR luminance range)
    // HDR: 0.005 / peak nits (PQ range)
    if (isHDR) {
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(0.005f));    // MinCLL
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(peakNits)); // MaxCLL
    } else {
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(0.5f));     // MinCLL
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(80.0f));    // MaxCLL
    }

    // Calculate offsets from tag start
    // Matrix starts at offset 36 (after 9-field header)
    uint32_t matrixOffset = 36;
    // Each LUT channel: 8-byte header ('sf32' + reserved) + lutSize * 4 bytes
    uint32_t lutChannelSize = 8 + lutSize * 4;
    uint32_t lut0Offset = matrixOffset + 48;  // 3x4 matrix = 48 bytes
    uint32_t lut1Offset = lut0Offset + lutChannelSize;
    uint32_t lut2Offset = lut1Offset + lutChannelSize;

    // Offsets from tag start
    AppendBE32(buf, matrixOffset);    // offset to matrix
    AppendBE32(buf, lut0Offset);      // offset to LUT 0 (Red)
    AppendBE32(buf, lut1Offset);      // offset to LUT 1 (Green)
    AppendBE32(buf, lut2Offset);      // offset to LUT 2 (Blue)

    // 3x4 matrix as s15Fixed16 (12 values, 48 bytes)
    for (int i = 0; i < 12; i++) {
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(matrix[i]));
    }

    // LUT data helper - each channel: 'sf32' + reserved + entries
    auto writeLUT = [&](const float* lut) {
        AppendBE32(buf, MakeSig("sf32"));   // type signature
        AppendBE32(buf, 0);                 // reserved
        // LUT entries as s15Fixed16 values
        for (int i = 0; i < lutSize; i++) {
            AppendBE32(buf, (uint32_t)FloatToS15Fixed16(lut[i]));
        }
    };

    writeLUT(lutR);
    writeLUT(lutG);
    writeLUT(lutB);

    PadTo4(buf);
}

// ============================================================================
// SECTION: Matrix Math
// ============================================================================

static void MatMul3(const float a[9], const float b[9], float out[9]) {
    for (int i = 0; i < 3; i++) {
        for (int j = 0; j < 3; j++) {
            out[i * 3 + j] = a[i * 3 + 0] * b[0 * 3 + j] +
                              a[i * 3 + 1] * b[1 * 3 + j] +
                              a[i * 3 + 2] * b[2 * 3 + j];
        }
    }
}

static bool MatInv3(const float m[9], float out[9]) {
    float det = m[0] * (m[4] * m[8] - m[5] * m[7]) -
                m[1] * (m[3] * m[8] - m[5] * m[6]) +
                m[2] * (m[3] * m[7] - m[4] * m[6]);
    if (fabs(det) < 1e-10f) return false;
    float invDet = 1.0f / det;
    out[0] = (m[4] * m[8] - m[5] * m[7]) * invDet;
    out[1] = (m[2] * m[7] - m[1] * m[8]) * invDet;
    out[2] = (m[1] * m[5] - m[2] * m[4]) * invDet;
    out[3] = (m[5] * m[6] - m[3] * m[8]) * invDet;
    out[4] = (m[0] * m[8] - m[2] * m[6]) * invDet;
    out[5] = (m[2] * m[3] - m[0] * m[5]) * invDet;
    out[6] = (m[3] * m[7] - m[4] * m[6]) * invDet;
    out[7] = (m[1] * m[6] - m[0] * m[7]) * invDet;
    out[8] = (m[0] * m[4] - m[1] * m[3]) * invDet;
    return true;
}

static void MatVecMul3(const float m[9], const float v[3], float out[3]) {
    out[0] = m[0] * v[0] + m[1] * v[1] + m[2] * v[2];
    out[1] = m[3] * v[0] + m[4] * v[1] + m[5] * v[2];
    out[2] = m[6] * v[0] + m[7] * v[1] + m[8] * v[2];
}

// CIE xy to XYZ (Y=1)
static void XyToXYZ(float x, float y, float XYZ[3]) {
    if (y < 1e-6f) y = 1e-6f;
    XYZ[0] = x / y;
    XYZ[1] = 1.0f;
    XYZ[2] = (1.0f - x - y) / y;
}

// Build RGB-to-XYZ matrix from primaries (CIE xy) and white point
static bool BuildRGBtoXYZ(const DisplayPrimariesData& p, float outMatrix[9]) {
    float rXYZ[3], gXYZ[3], bXYZ[3], wXYZ[3];
    XyToXYZ(p.Rx, p.Ry, rXYZ);
    XyToXYZ(p.Gx, p.Gy, gXYZ);
    XyToXYZ(p.Bx, p.By, bXYZ);
    XyToXYZ(p.Wx, p.Wy, wXYZ);

    // Primaries matrix (columns are R, G, B XYZ)
    float prim[9] = {
        rXYZ[0], gXYZ[0], bXYZ[0],
        rXYZ[1], gXYZ[1], bXYZ[1],
        rXYZ[2], gXYZ[2], bXYZ[2]
    };
    float primInv[9];
    if (!MatInv3(prim, primInv)) return false;

    float S[3];
    MatVecMul3(primInv, wXYZ, S);

    outMatrix[0] = rXYZ[0] * S[0]; outMatrix[1] = gXYZ[0] * S[1]; outMatrix[2] = bXYZ[0] * S[2];
    outMatrix[3] = rXYZ[1] * S[0]; outMatrix[4] = gXYZ[1] * S[1]; outMatrix[5] = bXYZ[1] * S[2];
    outMatrix[6] = rXYZ[2] * S[0]; outMatrix[7] = gXYZ[2] * S[1]; outMatrix[8] = bXYZ[2] * S[2];
    return true;
}

// ============================================================================
// SECTION: Color Space Constants
// ============================================================================

// sRGB / BT.709 primaries (D65)
static const DisplayPrimariesData g_srgbPrimaries = {
    0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f
};

// BT.2020 primaries (D65)
static const DisplayPrimariesData g_bt2020Primaries = {
    0.7080f, 0.2920f, 0.1700f, 0.7970f, 0.1310f, 0.0460f, 0.3127f, 0.3290f
};

// D50 illuminant (ICC PCS white point)
static const float g_d50XYZ[3] = { 0.9642f, 1.0000f, 0.8249f };

// D65 illuminant
static const float g_d65XYZ[3] = { 0.9505f, 1.0000f, 1.0890f };

// Bradford matrix for D65 -> D50 chromatic adaptation (ICC spec / Bruce Lindbloom)
static const float g_bradfordD65toD50[9] = {
     1.0478112f,  0.0228866f, -0.0501270f,
     0.0295424f,  0.9904844f, -0.0170491f,
    -0.0092345f,  0.0150436f,  0.7521316f
};

// ============================================================================
// SECTION: MHC2 Matrix Computation
// ============================================================================

void ComputeMHC2Matrix(const DisplayPrimariesData& srcPrimaries,
                       const DisplayPrimariesData& displayPrimaries,
                       bool isHDR, float outMHC[12]) {
    // The MHC2 driver pipeline:
    //   wire → DeGamma → RGBtoXYZ → [MHC2 matrix] → XYZtoRGB → ReGamma → LUT → display
    //
    // The driver wraps with implicit sRGB (SDR) or BT.2020 (HDR) RGB↔XYZ conversions.
    // The full chain from content to display output:
    //   displayed_XYZ = displayRGBtoXYZ * XYZtoWire * MHC2 * WireToXYZ * input_linear
    //
    // For correct colors: displayed_XYZ = srcRGBtoXYZ * input_linear
    // Therefore: MHC2 = wireToXYZ * inv(displayRGBtoXYZ) * srcRGBtoXYZ * XYZtoWire
    //
    // Since wire = src (sRGB for SDR, BT.2020 for HDR):
    //   MHC2 = srcRGBtoXYZ * inv(displayRGBtoXYZ)
    //
    // No Bradford adaptation - the matrix directly maps XYZ coordinates.
    // White point changes are encoded in the displayRGBtoXYZ matrix itself
    // (via its white point scaling), so they're naturally included.

    float srcToXYZ[9], displayToXYZ[9], displayFromXYZ[9];
    if (!BuildRGBtoXYZ(srcPrimaries, srcToXYZ) || !BuildRGBtoXYZ(displayPrimaries, displayToXYZ)
        || !MatInv3(displayToXYZ, displayFromXYZ)) {
        std::cerr << "MHC2 matrix: degenerate primaries, using identity" << std::endl;
        memset(outMHC, 0, sizeof(float) * 12);
        outMHC[0] = outMHC[5] = outMHC[10] = 1.0f;  // 3x4 identity (row-major)
        return;
    }

    // MHC2 = srcRGBtoXYZ * inv(displayRGBtoXYZ)
    float result[9];
    MatMul3(srcToXYZ, displayFromXYZ, result);

    std::cout << "MHC2 matrix (XYZ-to-XYZ):" << std::endl;
    std::cout << "  [" << result[0] << ", " << result[1] << ", " << result[2] << "]" << std::endl;
    std::cout << "  [" << result[3] << ", " << result[4] << ", " << result[5] << "]" << std::endl;
    std::cout << "  [" << result[6] << ", " << result[7] << ", " << result[8] << "]" << std::endl;

    // Pack into 3x4 row-major (4th column = 0)
    outMHC[0]  = result[0]; outMHC[1]  = result[1]; outMHC[2]  = result[2]; outMHC[3]  = 0.0f;
    outMHC[4]  = result[3]; outMHC[5]  = result[4]; outMHC[6]  = result[5]; outMHC[7]  = 0.0f;
    outMHC[8]  = result[6]; outMHC[9]  = result[7]; outMHC[10] = result[8]; outMHC[11] = 0.0f;
}

// ============================================================================
// SECTION: Transfer Functions
// ============================================================================

// sRGB EOTF: decode sRGB-encoded value to linear light
static float SrgbEOTF(float v) {
    if (v <= 0.04045f)
        return v / 12.92f;
    return powf((v + 0.055f) / 1.055f, 2.4f);
}

// sRGB OETF: encode linear light to sRGB signal
static float SrgbOETF(float v) {
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
static float PqEOTF(float pq) {
    float Vm = powf((std::max)(pq, 1e-10f), 1.0f / PQ_m2);
    float t = (std::max)(Vm - PQ_c1, 0.0f) / (std::max)(PQ_c2 - PQ_c3 * Vm, 1e-10f);
    return powf(t, 1.0f / PQ_m1);
}

// PQ OETF: linear light (0-1 normalized to 10000 nits) -> PQ signal (0-1)
static float PqOETF(float L) {
    float Y = (std::max)(L, 1e-10f);
    float Ym = powf(Y, PQ_m1);
    return powf((PQ_c1 + PQ_c2 * Ym) / (1.0f + PQ_c3 * Ym), PQ_m2);
}

// ============================================================================
// SECTION: Grayscale Evaluation
// ============================================================================

// Evaluate SDR grayscale correction (matches shader's ApplyGrayscaleCorrection)
// Input/output are linear light values (0-1)
static float EvalGrayscaleSDR(float Y_linear, const GrayscaleData& gs) {
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
static float EvalGrayscaleHDR(float pqValue, const GrayscaleData& gs, float pqPeak) {
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

// ============================================================================
// SECTION: Per-channel TRC → MHC2 1D LUT (ICC file import path)
// ============================================================================

// Invert a tabulated TRC: given target linear value, find the signal that produces it.
// TRC is monotonically increasing: signal(0-1) → linear(0-1), tabulated at even intervals.
// Uses binary search for robustness.
static float InvertTRC(const std::vector<float>& trc, float targetLinear) {
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
// The LUT corrects the display's non-ideal gamma to match sRGB.
// Pipeline: signal_in (sRGB) → sRGB_EOTF → linear_target → TRC_inverse → signal_out
void GenerateMHC2LUT_FromTRC_SDR(const std::vector<float>& trc, float* outLUT, int lutSize) {
    for (int j = 0; j < lutSize; j++) {
        float signalIn = (float)j / (float)(lutSize - 1);
        float linearTarget = SrgbEOTF(signalIn);
        float signalOut = InvertTRC(trc, linearTarget);
        outLUT[j] = std::clamp(signalOut, 0.0f, 1.0f);
    }
}

// Generate MHC2 1D LUT from a measured per-channel TRC curve (HDR / PQ).
// For HDR, the TRC operates in PQ signal domain.
void GenerateMHC2LUT_FromTRC_HDR(const std::vector<float>& trc, float* outLUT, int lutSize) {
    for (int j = 0; j < lutSize; j++) {
        float pqIn = (float)j / (float)(lutSize - 1);
        // For HDR, assume TRC maps PQ signal → linear (PQ-normalized)
        // Identity display: TRC(s) = s, so InvertTRC gives s back
        float pqOut = InvertTRC(trc, pqIn);
        outLUT[j] = std::clamp(pqOut, 0.0f, 1.0f);
    }
}

// ============================================================================
// SECTION: MD5 Hashing
// ============================================================================

static bool ComputeMD5(const uint8_t* data, size_t size, uint8_t outHash[16]) {
    HCRYPTPROV hProv = 0;
    HCRYPTHASH hHash = 0;

    if (!CryptAcquireContext(&hProv, nullptr, nullptr, PROV_RSA_FULL, CRYPT_VERIFYCONTEXT)) {
        return false;
    }

    if (!CryptCreateHash(hProv, CALG_MD5, 0, 0, &hHash)) {
        CryptReleaseContext(hProv, 0);
        return false;
    }

    if (!CryptHashData(hHash, data, (DWORD)size, 0)) {
        CryptDestroyHash(hHash);
        CryptReleaseContext(hProv, 0);
        return false;
    }

    DWORD hashLen = 16;
    bool ok = CryptGetHashParam(hHash, HP_HASHVAL, outHash, &hashLen, 0) != FALSE;

    CryptDestroyHash(hHash);
    CryptReleaseContext(hProv, 0);
    return ok;
}

// ============================================================================
// SECTION: ICC Profile Generation
// ============================================================================

bool GenerateMHC2Profile(const MHC2ProfileParams& params, std::vector<uint8_t>& outData) {
    outData.clear();

    // Compute MHC2 3x4 matrix directly from primaries (no Bradford adaptation)
    const DisplayPrimariesData& srcPrim = params.isHDR ? g_bt2020Primaries : g_srgbPrimaries;
    const DisplayPrimariesData& displayPrim = params.primariesEnabled
        ? params.displayPrimaries : srcPrim;

    if (params.primariesEnabled) {
        std::cout << "MHC2: primariesEnabled=true, isHDR=" << params.isHDR << std::endl;
        std::cout << "MHC2: display primaries: R(" << displayPrim.Rx << "," << displayPrim.Ry
                  << ") G(" << displayPrim.Gx << "," << displayPrim.Gy
                  << ") B(" << displayPrim.Bx << "," << displayPrim.By
                  << ") W(" << displayPrim.Wx << "," << displayPrim.Wy << ")" << std::endl;
    } else {
        std::cout << "MHC2: primariesEnabled=false, identity matrix" << std::endl;
    }

    float mhcMatrix[12];
    ComputeMHC2Matrix(srcPrim, displayPrim, params.isHDR, mhcMatrix);

    // Generate 1D LUTs
    // SDR: 1024 entries (sRGB signal, sufficient for 8-bit output)
    // HDR: 4096 entries (PQ signal needs more precision at low luminance due to steep slope)
    const int lutSize = params.isHDR ? 4096 : 1024;
    std::vector<float> lutR(lutSize), lutG(lutSize), lutB(lutSize);

    // Resample a correction curve to target size via linear interpolation
    auto resampleCurve = [](const std::vector<float>& src, float* dst, int dstSize) {
        for (int j = 0; j < dstSize; j++) {
            float t = (float)j / (float)(dstSize - 1);
            float srcIdx = t * (float)(src.size() - 1);
            int i0 = (int)srcIdx;
            int i1 = (std::min)(i0 + 1, (int)src.size() - 1);
            float frac = srcIdx - floorf(srcIdx);
            dst[j] = std::clamp(src[i0] + (src[i1] - src[i0]) * frac, 0.0f, 1.0f);
        }
    };

    if (params.hasPrecomputedCorrection && !params.corrR.empty() && !params.corrG.empty() && !params.corrB.empty()) {
        // Pre-computed correction curves from 1D .cube: use directly, just resample
        std::cout << "MHC2: Using pre-computed 1D LUT correction (" << params.corrR.size() << " entries)" << std::endl;
        resampleCurve(params.corrR, lutR.data(), lutSize);
        resampleCurve(params.corrG, lutG.data(), lutSize);
        resampleCurve(params.corrB, lutB.data(), lutSize);
    } else if (params.hasPerChannelTRC && !params.trcR.empty() && !params.trcG.empty() && !params.trcB.empty()) {
        // Per-channel TRC from ICC file: generate independent R/G/B correction LUTs
        std::cout << "MHC2: Using per-channel TRC (" << params.trcR.size() << " points)" << std::endl;
        if (params.isHDR) {
            GenerateMHC2LUT_FromTRC_HDR(params.trcR, lutR.data(), lutSize);
            GenerateMHC2LUT_FromTRC_HDR(params.trcG, lutG.data(), lutSize);
            GenerateMHC2LUT_FromTRC_HDR(params.trcB, lutB.data(), lutSize);
        } else {
            GenerateMHC2LUT_FromTRC_SDR(params.trcR, lutR.data(), lutSize);
            GenerateMHC2LUT_FromTRC_SDR(params.trcG, lutG.data(), lutSize);
            GenerateMHC2LUT_FromTRC_SDR(params.trcB, lutB.data(), lutSize);
        }
    } else if (params.isHDR) {
        GenerateMHC2LUT_HDR(params.grayscale, params.peakNits, lutR.data(), lutSize);
        lutG = lutR; lutB = lutR;
    } else {
        GenerateMHC2LUT_SDR(params.grayscale, lutR.data(), lutSize);
        lutG = lutR; lutB = lutR;
    }

    // Build ICC colorants (D50-adapted)
    // SDR: Always use sRGB colorants so Windows classifies as "SDR Profile".
    //      The MHC2 matrix handles the actual correction independently.
    // HDR: Use display native primaries (or BT.2020) for proper HDR classification.
    const DisplayPrimariesData& iccPrimaries = params.isHDR
        ? (params.primariesEnabled ? params.displayPrimaries : g_bt2020Primaries)
        : g_srgbPrimaries;
    float displayRGBtoXYZ[9];
    if (!BuildRGBtoXYZ(iccPrimaries, displayRGBtoXYZ)) {
        std::cerr << "MHC2: Failed to build display RGB-to-XYZ matrix" << std::endl;
        return false;
    }

    // Apply D65->D50 Bradford adaptation to get D50-adapted colorants
    float adaptedRGBtoXYZ[9];
    MatMul3(g_bradfordD65toD50, displayRGBtoXYZ, adaptedRGBtoXYZ);

    // Profile description
    std::wstring desc = L"DesktopLUT - " + params.monitorName;

    // ========================================================================
    // Build tag data blobs
    // ========================================================================

    // Tag list: desc, cprt, wtpt, rXYZ, gXYZ, bXYZ, rTRC, gTRC, bTRC, chad, lumi, MHC2
    const int numTags = 12;

    // Pre-build all tag data into individual buffers
    std::vector<uint8_t> tagDesc, tagCprt, tagWtpt;
    std::vector<uint8_t> tagRXYZ, tagGXYZ, tagBXYZ;
    std::vector<uint8_t> tagTRC;  // Shared for r/g/bTRC
    std::vector<uint8_t> tagChad, tagLumi, tagMHC2;

    // desc - profile description
    WriteMlucTag(tagDesc, desc);

    // cprt - copyright
    WriteMlucTag(tagCprt, L"DesktopLUT");

    // wtpt - D50-adapted white point (should be D50 for ICC v4)
    WriteXYZTag(tagWtpt, g_d50XYZ[0], g_d50XYZ[1], g_d50XYZ[2]);

    // rXYZ/gXYZ/bXYZ - D50-adapted colorants (columns of adapted matrix)
    WriteXYZTag(tagRXYZ, adaptedRGBtoXYZ[0], adaptedRGBtoXYZ[3], adaptedRGBtoXYZ[6]);
    WriteXYZTag(tagGXYZ, adaptedRGBtoXYZ[1], adaptedRGBtoXYZ[4], adaptedRGBtoXYZ[7]);
    WriteXYZTag(tagBXYZ, adaptedRGBtoXYZ[2], adaptedRGBtoXYZ[5], adaptedRGBtoXYZ[8]);

    // rTRC/gTRC/bTRC - gamma 2.2 (shared tag, same offset for all three)
    WriteCurvTagGamma(tagTRC, 2.2f);

    // chad - chromatic adaptation matrix (D65 -> D50 Bradford)
    WriteSf32Tag(tagChad, g_bradfordD65toD50);

    // lumi - peak luminance
    float peakLum = params.isHDR ? params.peakNits : 80.0f;
    WriteXYZTag(tagLumi, 0.0f, peakLum, 0.0f);  // Only Y component used for luminance

    // MHC2 - the correction data
    float mhcPeakNits = params.isHDR ? params.peakNits : 80.0f;
    WriteMHC2Tag(tagMHC2, mhcMatrix, lutR.data(), lutG.data(), lutB.data(), lutSize, params.isHDR, mhcPeakNits);

    // ========================================================================
    // Calculate layout
    // ========================================================================

    uint32_t headerSize = 128;
    uint32_t tagTableSize = 4 + numTags * 12;  // tag count + entries

    // Tag data starts after header + tag table
    uint32_t dataStart = headerSize + tagTableSize;
    // Align to 4 bytes
    while (dataStart % 4 != 0) dataStart++;

    // Calculate offsets for each tag
    struct TagEntry {
        uint32_t sig;
        uint32_t offset;
        uint32_t size;
    };
    std::vector<TagEntry> tags;

    uint32_t currentOffset = dataStart;

    auto addTag = [&](uint32_t sig, const std::vector<uint8_t>& data) {
        tags.push_back({ sig, currentOffset, (uint32_t)data.size() });
        currentOffset += (uint32_t)data.size();
        while (currentOffset % 4 != 0) currentOffset++;  // Pad
    };

    auto addSharedTag = [&](uint32_t sig, uint32_t sharedOffset, uint32_t sharedSize) {
        tags.push_back({ sig, sharedOffset, sharedSize });
    };

    addTag(MakeSig("desc"), tagDesc);
    addTag(MakeSig("cprt"), tagCprt);
    addTag(MakeSig("wtpt"), tagWtpt);
    addTag(MakeSig("rXYZ"), tagRXYZ);
    addTag(MakeSig("gXYZ"), tagGXYZ);
    addTag(MakeSig("bXYZ"), tagBXYZ);

    // rTRC gets its own offset
    uint32_t trcOffset = currentOffset;
    uint32_t trcSize = (uint32_t)tagTRC.size();
    addTag(MakeSig("rTRC"), tagTRC);

    // gTRC and bTRC share the same data as rTRC
    addSharedTag(MakeSig("gTRC"), trcOffset, trcSize);
    addSharedTag(MakeSig("bTRC"), trcOffset, trcSize);

    addTag(MakeSig("chad"), tagChad);
    addTag(MakeSig("lumi"), tagLumi);
    addTag(MakeSig("MHC2"), tagMHC2);

    uint32_t profileSize = currentOffset;

    // ========================================================================
    // Write the profile
    // ========================================================================

    outData.resize(profileSize, 0);

    // --- ICC Header (128 bytes) ---
    WriteBE32(outData, 0, profileSize);         // Profile size
    WriteBE32(outData, 4, MakeSig("MSFT"));     // Preferred CMM type
    WriteBE32(outData, 8, 0x04400000);          // Profile version 4.4.0.0
    WriteBE32(outData, 12, MakeSig("mntr"));    // Device class: monitor
    WriteBE32(outData, 16, MakeSig("RGB "));    // Color space: RGB
    WriteBE32(outData, 20, MakeSig("XYZ "));    // PCS: XYZ

    // Date/time: current time
    SYSTEMTIME st;
    GetSystemTime(&st);
    WriteBE16(outData, 24, st.wYear);
    WriteBE16(outData, 26, st.wMonth);
    WriteBE16(outData, 28, st.wDay);
    WriteBE16(outData, 30, st.wHour);
    WriteBE16(outData, 32, st.wMinute);
    WriteBE16(outData, 34, st.wSecond);

    WriteBE32(outData, 36, MakeSig("acsp"));    // File signature (always 'acsp')
    WriteBE32(outData, 40, MakeSig("MSFT"));    // Primary platform: Microsoft
    WriteBE32(outData, 44, 0);                  // Profile flags
    WriteBE32(outData, 48, 0);                  // Device manufacturer
    WriteBE32(outData, 52, 0);                  // Device model
    // Device attributes (8 bytes)
    WriteBE32(outData, 56, 0);
    WriteBE32(outData, 60, 0);
    WriteBE32(outData, 64, 3);                  // Rendering intent: absolute colorimetric (matches MHC2Gen)

    // PCS illuminant (D50 as s15Fixed16)
    WriteBE32(outData, 68, (uint32_t)FloatToS15Fixed16(g_d50XYZ[0]));
    WriteBE32(outData, 72, (uint32_t)FloatToS15Fixed16(g_d50XYZ[1]));
    WriteBE32(outData, 76, (uint32_t)FloatToS15Fixed16(g_d50XYZ[2]));

    WriteBE32(outData, 80, MakeSig("MSFT"));    // Profile creator

    // Profile ID (bytes 84-99) - computed after full profile is built
    // (set to zero for now, computed below)

    // --- Tag Table ---
    size_t tableOffset = headerSize;
    WriteBE32(outData, tableOffset, numTags);
    for (int i = 0; i < numTags; i++) {
        size_t entryOffset = tableOffset + 4 + i * 12;
        WriteBE32(outData, entryOffset, tags[i].sig);
        WriteBE32(outData, entryOffset + 4, tags[i].offset);
        WriteBE32(outData, entryOffset + 8, tags[i].size);
    }

    // --- Tag Data ---
    auto writeTagData = [&](const TagEntry& entry, const std::vector<uint8_t>& data) {
        if (entry.offset + data.size() <= outData.size()) {
            memcpy(outData.data() + entry.offset, data.data(), data.size());
        }
    };

    // Write each unique tag's data (skip shared tags which point to same offset)
    writeTagData(tags[0], tagDesc);
    writeTagData(tags[1], tagCprt);
    writeTagData(tags[2], tagWtpt);
    writeTagData(tags[3], tagRXYZ);
    writeTagData(tags[4], tagGXYZ);
    writeTagData(tags[5], tagBXYZ);
    writeTagData(tags[6], tagTRC);
    // tags[7] (gTRC) and tags[8] (bTRC) share data with tags[6] (rTRC) - already written
    writeTagData(tags[9], tagChad);
    writeTagData(tags[10], tagLumi);
    writeTagData(tags[11], tagMHC2);

    // --- Compute Profile ID (MD5 hash) ---
    // Per ICC spec: zero out bytes 44-47 (flags), 48-51 (manufacturer), 52-55 (model),
    // 56-63 (attributes), 64-67 (rendering intent), and 84-99 (profile ID) before hashing
    std::vector<uint8_t> hashInput = outData;
    memset(hashInput.data() + 44, 0, 4);   // flags
    memset(hashInput.data() + 48, 0, 4);   // manufacturer
    memset(hashInput.data() + 52, 0, 4);   // model
    memset(hashInput.data() + 56, 0, 8);   // attributes
    memset(hashInput.data() + 64, 0, 4);   // rendering intent
    memset(hashInput.data() + 84, 0, 16);  // profile ID

    uint8_t hash[16];
    if (ComputeMD5(hashInput.data(), hashInput.size(), hash)) {
        memcpy(outData.data() + 84, hash, 16);
    }

    std::cout << "MHC2 profile generated: " << profileSize << " bytes, "
              << numTags << " tags, LUT " << lutSize << " entries" << std::endl;

    return true;
}

bool WriteMHC2Profile(const std::vector<uint8_t>& data, const std::wstring& filePath) {
    std::ofstream file(filePath, std::ios::binary);
    if (!file.is_open()) {
        std::wcerr << L"MHC2: Failed to open file for writing: " << filePath << std::endl;
        return false;
    }

    file.write(reinterpret_cast<const char*>(data.data()), data.size());
    file.close();

    if (!file.good()) {
        std::wcerr << L"MHC2: Write error: " << filePath << std::endl;
        return false;
    }

    std::wcout << L"MHC2 profile written: " << filePath << std::endl;
    return true;
}

// ============================================================================
// SECTION: MSCMS API Loading
// ============================================================================

// Function pointer types for dynamically loaded ICM APIs
typedef BOOL(WINAPI* PFN_InstallColorProfileW)(PCWSTR, PCWSTR);
typedef HRESULT(WINAPI* PFN_ColorProfileAddDisplayAssociation)(
    int scope, PCWSTR profileName, LUID adapterId, UINT32 sourceId,
    BOOL setAsDefault, BOOL associateAsAdvancedColor);
typedef HRESULT(WINAPI* PFN_ColorProfileRemoveDisplayAssociation)(
    int scope, PCWSTR profileName, LUID adapterId, UINT32 sourceId,
    BOOL dissociateAdvancedColor);
typedef HRESULT(WINAPI* PFN_ColorProfileGetDisplayDefault)(
    int scope, LUID targetAdapterID, UINT32 sourceID,
    int profileType, int profileSubType, LPWSTR* profileName);

static HMODULE g_hMscms = nullptr;
static PFN_InstallColorProfileW g_pfnInstallColorProfile = nullptr;
static PFN_ColorProfileAddDisplayAssociation g_pfnAddAssociation = nullptr;
static PFN_ColorProfileRemoveDisplayAssociation g_pfnRemoveAssociation = nullptr;
static PFN_ColorProfileGetDisplayDefault g_pfnGetDisplayDefault = nullptr;
static bool g_mscmsChecked = false;

static void EnsureMscmsLoaded() {
    if (g_mscmsChecked) return;
    g_mscmsChecked = true;

    g_hMscms = LoadLibraryW(L"Mscms.dll");
    if (!g_hMscms) {
        std::cerr << "MHC2: Failed to load Mscms.dll" << std::endl;
        return;
    }

    g_pfnInstallColorProfile = (PFN_InstallColorProfileW)
        GetProcAddress(g_hMscms, "InstallColorProfileW");
    g_pfnAddAssociation = (PFN_ColorProfileAddDisplayAssociation)
        GetProcAddress(g_hMscms, "ColorProfileAddDisplayAssociation");
    g_pfnRemoveAssociation = (PFN_ColorProfileRemoveDisplayAssociation)
        GetProcAddress(g_hMscms, "ColorProfileRemoveDisplayAssociation");
    g_pfnGetDisplayDefault = (PFN_ColorProfileGetDisplayDefault)
        GetProcAddress(g_hMscms, "ColorProfileGetDisplayDefault");

    if (g_pfnAddAssociation) {
        std::cout << "MHC2: Color management APIs available" << std::endl;
    } else {
        std::cout << "MHC2: ColorProfileAddDisplayAssociation not found (requires Windows 10 21H2+)" << std::endl;
    }
}

bool IsMHC2ApiAvailable() {
    EnsureMscmsLoaded();
    return g_pfnInstallColorProfile && g_pfnAddAssociation && g_pfnRemoveAssociation;
}

// ============================================================================
// SECTION: ICC Profile I/O (Install, Remove, Reassociate)
// ============================================================================

bool InstallMHC2Profile(const std::wstring& profilePath, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnInstallColorProfile || !g_pfnAddAssociation) {
        std::cerr << "MHC2: Install API not available" << std::endl;
        return false;
    }

    // Extract filename from full path
    std::wstring profileName = profilePath;
    size_t lastSlash = profileName.find_last_of(L"\\/");
    if (lastSlash != std::wstring::npos) {
        profileName = profileName.substr(lastSlash + 1);
    }

    // Build the system color directory path for this profile
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring sysColorPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    // Delete existing file from system color directory so InstallColorProfileW
    // actually copies the new file (it won't overwrite existing files)
    DeleteFileW(sysColorPath.c_str());

    // Install profile to system color directory (copies from profilePath)
    if (!g_pfnInstallColorProfile(nullptr, profilePath.c_str())) {
        DWORD err = GetLastError();
        if (err != 183) {  // ERROR_ALREADY_EXISTS
            std::cerr << "MHC2: InstallColorProfileW failed: " << err << std::endl;
            return false;
        }
    }

    // SDR profiles: associateAsAdvancedColor=FALSE → classified as "SDR Profile"
    // HDR profiles: associateAsAdvancedColor=TRUE → classified as "HDR Profile"
    HRESULT hr = g_pfnAddAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        TRUE,               // setAsDefault
        isHDR ? TRUE : FALSE  // associateAsAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ColorProfileAddDisplayAssociation failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile installed and associated: " << profileName << std::endl;
    return true;
}

bool RemoveMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnRemoveAssociation) {
        std::cerr << "MHC2: Remove API not available" << std::endl;
        return false;
    }

    // dissociateAdvancedColor must match how the profile was installed
    HRESULT hr = g_pfnRemoveAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        isHDR ? TRUE : FALSE  // dissociateAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ColorProfileRemoveDisplayAssociation failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile removed: " << profileName << std::endl;
    return true;
}

bool ReassociateMHC2Profile(const std::wstring& profileName, LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();

    if (!g_pfnAddAssociation) {
        std::cerr << "MHC2: Reassociate API not available" << std::endl;
        return false;
    }

    HRESULT hr = g_pfnAddAssociation(
        1,                  // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        profileName.c_str(),
        adapterLuid,
        sourceId,
        TRUE,               // setAsDefault
        isHDR ? TRUE : FALSE  // associateAsAdvancedColor
    );

    if (FAILED(hr)) {
        std::cerr << "MHC2: ReassociateMHC2Profile failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    std::wcout << L"MHC2: Profile reassociated: " << profileName << std::endl;
    return true;
}

// ============================================================================
// SECTION: ICC Profile Reading
// ============================================================================

// Read big-endian 32-bit from buffer
static uint32_t ReadBE32(const uint8_t* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

// Read big-endian 16-bit from buffer
static uint16_t ReadBE16(const uint8_t* p) {
    return ((uint16_t)p[0] << 8) | p[1];
}

// Read s15Fixed16Number
static float ReadS15Fixed16(const uint8_t* p) {
    int32_t val = (int32_t)ReadBE32(p);
    return (float)val / 65536.0f;
}

// Bradford D50->D65 matrix (inverse of D65->D50, ICC spec / Bruce Lindbloom)
static const float g_bradfordD50toD65[9] = {
     0.9555766f, -0.0230393f,  0.0631636f,
    -0.0282895f,  1.0099416f,  0.0210077f,
     0.0122982f, -0.0204830f,  1.3299098f
};

bool ReadICCProfile(const std::wstring& path, ICCProfileData& outData) {
    outData = {};

    // Read file
    std::ifstream file(path, std::ios::binary);
    if (!file.is_open()) return false;
    file.seekg(0, std::ios::end);
    size_t fileSize = (size_t)file.tellg();
    if (fileSize < 132) return false;  // Minimum: 128 header + 4 tag count
    file.seekg(0);
    std::vector<uint8_t> data(fileSize);
    file.read(reinterpret_cast<char*>(data.data()), fileSize);
    file.close();

    const uint8_t* d = data.data();

    // Verify ICC signature at offset 36
    if (ReadBE32(d + 36) != MakeSig("acsp")) {
        std::cerr << "ICC: Invalid signature (not 'acsp')" << std::endl;
        return false;
    }

    // Read tag table
    uint32_t tagCount = ReadBE32(d + 128);
    if (tagCount > 200) return false;  // Sanity: no real ICC has >~30 tags
    if (128 + 4 + (uint64_t)tagCount * 12 > fileSize) return false;

    struct TagInfo { uint32_t sig, offset, size; };
    std::vector<TagInfo> tags(tagCount);
    for (uint32_t i = 0; i < tagCount; i++) {
        size_t base = 132 + i * 12;
        tags[i].sig = ReadBE32(d + base);
        tags[i].offset = ReadBE32(d + base + 4);
        tags[i].size = ReadBE32(d + base + 8);
    }

    auto findTag = [&](uint32_t sig) -> const TagInfo* {
        for (auto& t : tags) if (t.sig == sig) return &t;
        return nullptr;
    };

    // Read chromatic adaptation matrix (chad tag) for un-adapting primaries
    float chadInv[9] = { 1,0,0, 0,1,0, 0,0,1 };
    bool hasChadInv = false;
    if (auto* tag = findTag(MakeSig("chad"))) {
        if (tag->offset + 44 <= fileSize) {
            const uint8_t* p = d + tag->offset;
            if (ReadBE32(p) == MakeSig("sf32")) {
                float chad[9];
                for (int i = 0; i < 9; i++)
                    chad[i] = ReadS15Fixed16(p + 8 + i * 4);
                hasChadInv = MatInv3(chad, chadInv);
            }
        }
    }
    if (!hasChadInv) {
        // Use standard D50->D65 Bradford as fallback
        memcpy(chadInv, g_bradfordD50toD65, sizeof(chadInv));
    }

    // Read rXYZ/gXYZ/bXYZ tags -> extract primaries
    auto readXYZTag = [&](uint32_t sig, float outXYZ[3]) -> bool {
        auto* tag = findTag(sig);
        if (!tag || tag->offset + 20 > fileSize) return false;
        const uint8_t* p = d + tag->offset;
        if (ReadBE32(p) != MakeSig("XYZ ")) return false;
        outXYZ[0] = ReadS15Fixed16(p + 8);
        outXYZ[1] = ReadS15Fixed16(p + 12);
        outXYZ[2] = ReadS15Fixed16(p + 16);
        return true;
    };

    float rXYZ[3], gXYZ[3], bXYZ[3];
    if (readXYZTag(MakeSig("rXYZ"), rXYZ) &&
        readXYZTag(MakeSig("gXYZ"), gXYZ) &&
        readXYZTag(MakeSig("bXYZ"), bXYZ)) {
        // Un-adapt from D50 PCS to D65
        float rD65[3], gD65[3], bD65[3];
        MatVecMul3(chadInv, rXYZ, rD65);
        MatVecMul3(chadInv, gXYZ, gD65);
        MatVecMul3(chadInv, bXYZ, bD65);

        // Convert XYZ to CIE xy
        auto xyzToXy = [](const float xyz[3], float& x, float& y) {
            float sum = xyz[0] + xyz[1] + xyz[2];
            if (sum < 1e-6f) { x = 0.3127f; y = 0.3290f; return; }
            x = xyz[0] / sum;
            y = xyz[1] / sum;
        };
        xyzToXy(rD65, outData.primaries.Rx, outData.primaries.Ry);
        xyzToXy(gD65, outData.primaries.Gx, outData.primaries.Gy);
        xyzToXy(bD65, outData.primaries.Bx, outData.primaries.By);
        // White point from sum of un-adapted primaries (native display white)
        float wXYZ[3] = { rD65[0]+gD65[0]+bD65[0], rD65[1]+gD65[1]+bD65[1], rD65[2]+gD65[2]+bD65[2] };
        xyzToXy(wXYZ, outData.primaries.Wx, outData.primaries.Wy);
        outData.hasPrimaries = true;
    }

    // Read rTRC/gTRC/bTRC tags -> extract transfer curves
    auto readCurvTag = [&](uint32_t sig, std::vector<float>& outCurve, float& outGamma) -> bool {
        auto* tag = findTag(sig);
        if (!tag || tag->offset + 12 > fileSize) return false;
        const uint8_t* p = d + tag->offset;
        uint32_t typeSig = ReadBE32(p);

        if (typeSig == MakeSig("curv")) {
            uint32_t count = ReadBE32(p + 8);
            if (count == 0) {
                // Identity (gamma 1.0)
                outGamma = 1.0f;
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) outCurve[i] = (float)i / 255.0f;
                return true;
            } else if (count == 1) {
                // Parametric gamma (u8Fixed8)
                if (tag->offset + 14 > fileSize) return false;
                outGamma = (float)ReadBE16(p + 12) / 256.0f;
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) {
                    float t = (float)i / 255.0f;
                    outCurve[i] = powf(t, outGamma);
                }
                return true;
            } else {
                // Tabular data
                if (count > 65536) return false;  // Sanity: reasonable max for tabular TRC
                if ((uint64_t)tag->offset + 12 + (uint64_t)count * 2 > fileSize) return false;
                outCurve.resize(count);
                for (uint32_t i = 0; i < count; i++) {
                    outCurve[i] = (float)ReadBE16(p + 12 + i * 2) / 65535.0f;
                }
                return true;
            }
        } else if (typeSig == MakeSig("para")) {
            // Parametric curve type
            if (tag->offset + 16 > fileSize) return false;
            uint16_t funcType = ReadBE16(p + 8);
            if (funcType == 0) {
                // Type 0: Y = X^g
                outGamma = ReadS15Fixed16(p + 12);
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) {
                    float t = (float)i / 255.0f;
                    outCurve[i] = powf(t, outGamma);
                }
                return true;
            }
            // Other para types (1-4) are more complex; generate curve from the function
            // For simplicity, handle type 3 (sRGB-like: Y = (aX+b)^g + c for X>=d, else eX+f)
            if (funcType == 3 && tag->offset + 12 + 7 * 4 <= fileSize) {
                float g = ReadS15Fixed16(p + 12);
                float a = ReadS15Fixed16(p + 16);
                float b = ReadS15Fixed16(p + 20);
                float c = ReadS15Fixed16(p + 24);
                float d_param = ReadS15Fixed16(p + 28);
                float e = ReadS15Fixed16(p + 32);
                float f = ReadS15Fixed16(p + 36);
                outCurve.resize(256);
                for (int i = 0; i < 256; i++) {
                    float x = (float)i / 255.0f;
                    if (x >= d_param) {
                        outCurve[i] = powf(a * x + b, g) + c;
                    } else {
                        outCurve[i] = e * x + f;
                    }
                    outCurve[i] = std::clamp(outCurve[i], 0.0f, 1.0f);
                }
                return true;
            }
            // Fallback: assume gamma 2.2
            outGamma = 2.2f;
            outCurve.resize(256);
            for (int i = 0; i < 256; i++) {
                float t = (float)i / 255.0f;
                outCurve[i] = powf(t, 2.2f);
            }
            return true;
        }
        return false;
    };

    float gammaR = 0, gammaG = 0, gammaB = 0;
    bool hasR = readCurvTag(MakeSig("rTRC"), outData.trcR, gammaR);
    bool hasG = readCurvTag(MakeSig("gTRC"), outData.trcG, gammaG);
    bool hasB = readCurvTag(MakeSig("bTRC"), outData.trcB, gammaB);
    if (hasR && hasG && hasB) {
        outData.hasTRC = true;
        // If all are single gamma, record average
        if (gammaR > 0 && gammaG > 0 && gammaB > 0) {
            outData.gamma = (gammaR + gammaG + gammaB) / 3.0f;
            outData.hasGamma = true;
        }
    }

    // Read description tag
    if (auto* tag = findTag(MakeSig("desc"))) {
        if (tag->offset + 12 <= fileSize) {
            const uint8_t* p = d + tag->offset;
            uint32_t typeSig = ReadBE32(p);
            if (typeSig == MakeSig("mluc") && tag->offset + 28 <= fileSize) {
                uint32_t strLen = ReadBE32(p + 20);
                uint32_t strOff = ReadBE32(p + 24);
                if (tag->offset + strOff + strLen <= fileSize) {
                    for (uint32_t i = 0; i < strLen / 2; i++) {
                        outData.description += (wchar_t)ReadBE16(p + strOff + i * 2);
                    }
                }
            }
        }
    }

    // Read lumi tag (peak luminance in cd/m²)
    if (auto* tag = findTag(MakeSig("lumi"))) {
        if (tag->offset + 20 <= fileSize) {
            const uint8_t* p = d + tag->offset;
            if (ReadBE32(p) == MakeSig("XYZ ")) {
                // Y component = luminance in cd/m²
                float Y = ReadS15Fixed16(p + 12);
                if (Y > 0.0f) {
                    outData.luminance = Y;
                    outData.hasLuminance = true;
                }
            }
        }
    }

    std::cout << "ICC: Read profile, primaries=" << outData.hasPrimaries
              << " trc=" << outData.hasTRC << std::endl;
    return outData.hasPrimaries || outData.hasTRC;
}

// ============================================================================
// SECTION: Grayscale Extraction
// ============================================================================

bool ExtractGrayscaleFromICC(const ICCProfileData& icc, GrayscaleSettings& outGrayscale, bool isHDR) {
    if (!icc.hasTRC) return false;

    // Average R/G/B curves
    size_t curveLen = (std::min)({icc.trcR.size(), icc.trcG.size(), icc.trcB.size()});
    if (curveLen < 2) return false;

    std::vector<float> avgCurve(curveLen);
    for (size_t i = 0; i < curveLen; i++) {
        avgCurve[i] = (icc.trcR[i] + icc.trcG[i] + icc.trcB[i]) / 3.0f;
    }

    int N = outGrayscale.pointCount;
    outGrayscale.points.resize(N);

    if (isHDR) {
        // HDR: evenly-spaced PQ points
        // ICC TRC is input-to-output in linear space; map to PQ deviation
        // For simplicity, treat ICC TRC as gamma correction and convert to PQ deviation
        for (int i = 0; i < N; i++) {
            float t = (float)i / (float)(N - 1);  // Input PQ value
            // Sample ICC curve at this position (linear interpolation)
            float idx = t * (float)(curveLen - 1);
            int i0 = (int)idx;
            int i1 = (std::min)(i0 + 1, (int)curveLen - 1);
            float frac = idx - floorf(idx);
            float iccVal = avgCurve[i0] + (avgCurve[i1] - avgCurve[i0]) * frac;
            // In PQ space, the output should equal the input for identity
            // ICC curve maps signal -> linear, so deviation = iccVal / expected
            outGrayscale.points[i] = iccVal;
        }
        outGrayscale.enabled = true;
    } else {
        // SDR: sqrt-distribution points
        // ICC TRC: input signal -> linear light output
        // GrayscaleSettings: points[i] = output linear value for input (i/(N-1))^2
        for (int i = 0; i < N; i++) {
            float t = (float)i / (float)(N - 1);
            float inputLinear = t * t;  // Input level (sqrt distribution)

            // Find where in the ICC curve this input linear level maps from
            // ICC: input_signal -> output_linear
            // We need: what is the output linear for input signal = sRGB_encode(inputLinear)?
            // Approximate: sample ICC curve at position = inputLinear (normalized index)
            float idx = inputLinear * (float)(curveLen - 1);
            int i0 = (int)idx;
            int i1 = (std::min)(i0 + 1, (int)curveLen - 1);
            float frac = idx - floorf(idx);
            float iccVal = avgCurve[i0] + (avgCurve[i1] - avgCurve[i0]) * frac;
            outGrayscale.points[i] = iccVal;
        }
        outGrayscale.enabled = true;
    }

    return true;
}

bool ExtractGrayscaleFromCube(const std::wstring& path, GrayscaleSettings& outGrayscale) {
    // Load cube file
    std::vector<float> lutData;
    int lutSize = 0;

    // Forward declare - defined in lut.cpp
    extern bool LoadLUT(const std::wstring& path, std::vector<float>& data, int& lutSize);

    if (!LoadLUT(path, lutData, lutSize) || lutSize < 2) return false;

    int N = outGrayscale.pointCount;
    outGrayscale.points.resize(N);

    // Sample neutral axis (R=G=B diagonal) at sqrt-spaced input levels.
    // The shader's ApplyGrayscaleCorrection uses sqrt indexing: idx = sqrt(Y) * (N-1),
    // so point i corresponds to input Y = (i/(N-1))^2.
    for (int i = 0; i < N; i++) {
        float sqrtT = (float)i / (float)(N - 1);  // sqrt-space index 0-1
        float t = sqrtT * sqrtT;  // Input level in gamma space

        // Trilinear interpolation at (t, t, t) in the LUT
        float pos = t * (float)(lutSize - 1);
        int i0 = (int)floorf(pos);
        int i1 = (std::min)(i0 + 1, lutSize - 1);
        float frac = pos - floorf(pos);

        // 8 corners of the cube cell (all on diagonal, so r=g=b indices are same)
        auto sampleLUT = [&](int r, int g, int b) -> float {
            int idx = (r + g * lutSize + b * lutSize * lutSize) * 4;  // RGBA
            if (idx + 2 >= (int)lutData.size()) return t;
            return (lutData[idx] + lutData[idx + 1] + lutData[idx + 2]) / 3.0f;
        };

        // Trilinear interpolate along diagonal
        float v000 = sampleLUT(i0, i0, i0);
        float v100 = sampleLUT(i1, i0, i0);
        float v010 = sampleLUT(i0, i1, i0);
        float v001 = sampleLUT(i0, i0, i1);
        float v110 = sampleLUT(i1, i1, i0);
        float v101 = sampleLUT(i1, i0, i1);
        float v011 = sampleLUT(i0, i1, i1);
        float v111 = sampleLUT(i1, i1, i1);

        float c00 = v000 + (v100 - v000) * frac;
        float c01 = v001 + (v101 - v001) * frac;
        float c10 = v010 + (v110 - v010) * frac;
        float c11 = v011 + (v111 - v011) * frac;
        float c0 = c00 + (c10 - c00) * frac;
        float c1 = c01 + (c11 - c01) * frac;
        float output = c0 + (c1 - c0) * frac;

        // For SDR sqrt-distribution: points[i] should be the output value
        // The expected identity output = t, so this captures any LUT deviation
        outGrayscale.points[i] = std::clamp(output, 0.0f, 1.0f);
    }

    outGrayscale.enabled = true;
    return true;
}

// ============================================================================
// SECTION: 1D Cube Loading
// ============================================================================

bool Load1DCubeLUT(const std::wstring& path, std::vector<float>& outR, std::vector<float>& outG, std::vector<float>& outB) {
    std::ifstream file(path);
    if (!file.is_open()) {
        std::wcerr << L"Failed to open 1D LUT file: " << path << std::endl;
        return false;
    }

    int lutSize = 0;
    std::vector<float> tempR, tempG, tempB;
    std::string line;

    while (std::getline(file, line)) {
        if (line.empty() || line[0] == '#') continue;
        if (line.find("TITLE") == 0) continue;
        if (line.find("DOMAIN_MIN") == 0) continue;
        if (line.find("DOMAIN_MAX") == 0) continue;

        // Must be a 1D LUT
        if (line.find("LUT_3D_SIZE") == 0) {
            std::cerr << "Error: File contains a 3D LUT, expected 1D" << std::endl;
            return false;
        }

        if (line.find("LUT_1D_SIZE") == 0) {
            std::istringstream iss(line.substr(11));
            iss >> lutSize;
            if (lutSize < 2 || lutSize > 65536) {
                std::cerr << "Invalid 1D LUT size: " << lutSize << std::endl;
                return false;
            }
            tempR.reserve(lutSize);
            tempG.reserve(lutSize);
            tempB.reserve(lutSize);
            continue;
        }

        if (line.find("LUT_1D_INPUT_RANGE") == 0) continue;

        // Parse R G B triplets
        std::istringstream iss(line);
        float r, g, b;
        if (iss >> r >> g >> b) {
            tempR.push_back(r);
            tempG.push_back(g);
            tempB.push_back(b);
        }
    }

    if (tempR.empty()) {
        std::cerr << "No 1D LUT data found in file" << std::endl;
        return false;
    }

    // If no LUT_1D_SIZE header, infer from data count
    if (lutSize == 0) lutSize = (int)tempR.size();

    if ((int)tempR.size() != lutSize) {
        std::cerr << "1D LUT: expected " << lutSize << " entries, got " << tempR.size() << std::endl;
        return false;
    }

    outR = std::move(tempR);
    outG = std::move(tempG);
    outB = std::move(tempB);
    std::cout << "Loaded 1D cube LUT: " << lutSize << " entries per channel" << std::endl;
    return true;
}

// ============================================================================
// SECTION: Profile Query & Cleanup
// ============================================================================

std::wstring QueryDisplayDefaultProfile(LUID adapterLuid, UINT32 sourceId, bool isHDR) {
    EnsureMscmsLoaded();
    if (!g_pfnGetDisplayDefault) return L"";

    // profileSubType: 7 = CPST_EXTENDED_DISPLAY_IDENTIFICATION_DATA (SDR default)
    //                 8 = CPST_ADVANCED_COLOR (HDR default)
    int subType = isHDR ? 8 : 7;

    LPWSTR profileName = nullptr;
    HRESULT hr = g_pfnGetDisplayDefault(
        1,              // WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
        adapterLuid,
        sourceId,
        0,              // CPT_ICC
        subType,
        &profileName
    );

    if (FAILED(hr) || !profileName) {
        return L"";
    }

    std::wstring result(profileName);
    LocalFree(profileName);
    return result;
}

void CleanupOrphanedMhcProfiles() {
    // Build set of profile names currently referenced by settings
    std::set<std::wstring> activeProfiles;
    for (const auto& ms : g_gui.monitorSettings) {
        if (!ms.sdrMHC.profileName.empty())
            activeProfiles.insert(ms.sdrMHC.profileName);
        if (!ms.hdrMHC.profileName.empty())
            activeProfiles.insert(ms.hdrMHC.profileName);
    }

    // Scan system color directory for DesktopLUT_*.icm files
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring colorDir = std::wstring(sysDir) + L"\\spool\\drivers\\color\\";
    std::wstring searchPattern = colorDir + L"DesktopLUT_*.icm";

    WIN32_FIND_DATAW fd;
    HANDLE hFind = FindFirstFileW(searchPattern.c_str(), &fd);
    if (hFind == INVALID_HANDLE_VALUE) return;

    int deleted = 0;
    do {
        std::wstring fileName = fd.cFileName;
        if (activeProfiles.find(fileName) == activeProfiles.end()) {
            std::wstring fullPath = colorDir + fileName;
            if (DeleteFileW(fullPath.c_str())) {
                std::wcout << L"MHC cleanup: deleted orphaned " << fileName << std::endl;
                deleted++;
            }
        }
    } while (FindNextFileW(hFind, &fd));

    FindClose(hFind);
    if (deleted > 0) {
        std::cout << "MHC cleanup: removed " << deleted << " orphaned profile(s)" << std::endl;
    }
}

void ReapplyAllMhcProfiles() {
    if (!IsMHC2ApiAvailable()) return;

    for (int i = 0; i < (int)g_gui.monitorSettings.size(); i++) {
        const auto& ms = g_gui.monitorSettings[i];

        DisplayInfo displayInfo;
        if (!GetDisplayInfoForMonitor(i, displayInfo)) continue;

        // Reapply SDR profile
        if (ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()) {
            RemoveMHC2Profile(ms.sdrMHC.profileName, displayInfo.adapterId, displayInfo.sourceId, false);
            ReassociateMHC2Profile(ms.sdrMHC.profileName, displayInfo.adapterId, displayInfo.sourceId, false);
            std::wcout << L"MHC reapply: SDR profile '" << ms.sdrMHC.profileName
                       << L"' for monitor " << i << std::endl;
        }

        // Reapply HDR profile
        if (ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty()) {
            RemoveMHC2Profile(ms.hdrMHC.profileName, displayInfo.adapterId, displayInfo.sourceId, true);
            ReassociateMHC2Profile(ms.hdrMHC.profileName, displayInfo.adapterId, displayInfo.sourceId, true);
            std::wcout << L"MHC reapply: HDR profile '" << ms.hdrMHC.profileName
                       << L"' for monitor " << i << std::endl;
        }
    }
}
