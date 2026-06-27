// DesktopLUT - mhc_icc.cpp
// ICC binary format helpers, tag writers, matrix math, and MHC2 profile generation

#include "mhc.h"
#include "mhc_internal.h"
#include <iostream>
#include <fstream>
#include <cstring>
#include <cmath>
#include <algorithm>
#include <wincrypt.h>

#pragma comment(lib, "Advapi32.lib")

// ============================================================================
// SECTION: ICC Binary Helpers
// ============================================================================

// Convert float to ICC s15Fixed16Number (multiply by 65536, round)
int32_t FloatToS15Fixed16(float f) {
    // s15Fixed16 represents [-32768, +32767.99998]. Guard NaN (-> 0) and out-of-range
    // magnitudes so the float->int conversion is never UB and the tag is never silently
    // corrupted. Current callers are bounded, so this is defensive.
    if (!(f == f)) return 0;                       // NaN
    if (f >= 32767.99998f)  return 0x7FFFFFFF;     // saturate +max
    if (f <= -32768.0f)     return (int32_t)0x80000000;  // saturate -min
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

// Write para (parametricCurveType) tag encoding the sRGB piecewise EOTF (ICC type 4):
//   X >= d:  Y = (a*X + b)^g + e
//   X <  d:  Y = c*X + f
// sRGB params (e=f=0). Used for the HDR rTRC/gTRC/bTRC. In Advanced-Color/HDR mode Windows
// IGNORES these TRC tags for a valid MHC profile (only lumi/MHC2/primaries drive the PQ
// scanout) — so this curve does NOT touch PQ content. It exists for CMM-aware apps that
// color-manage directly against the display profile: sRGB is the correct neutral tag (a
// gamma-2.2 tag makes such apps apply an unwanted sRGB->2.2 conversion), and it matches the
// canonical MHC2 reference (dantmnf/MHC2Gen always tags sRGB, never gamma 2.2). Real PQ-crush
// would come from the MHC2 regamma LUT (desktop-gamma), not from this tag.
static void WriteParaTagSrgb(std::vector<uint8_t>& buf) {
    AppendBE32(buf, MakeSig("para"));   // type signature
    AppendBE32(buf, 0);                 // reserved
    AppendBE16(buf, 4);                 // function type 4 (full sRGB piecewise)
    AppendBE16(buf, 0);                 // reserved
    const float p[7] = {
        2.4f,            // g
        1.0f / 1.055f,   // a
        0.055f / 1.055f, // b
        1.0f / 12.92f,   // c
        0.04045f,        // d
        0.0f,            // e
        0.0f,            // f
    };
    for (int i = 0; i < 7; i++) {
        AppendBE32(buf, (uint32_t)FloatToS15Fixed16(p[i]));  // s15Fixed16
    }
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
                       bool isHDR, float outMHC[12],
                       const float* whiteBalanceGains,
                       float* outAsAppliedRGB) {
    // The MHC2 driver pipeline:
    //   wire → DeGamma → RGBtoXYZ → [MHC2 matrix] → XYZtoRGB → ReGamma → LUT → display
    //
    // The driver wraps with implicit sRGB (SDR) or BT.2020 (HDR) RGB↔XYZ conversions.
    // The full chain from content to display output:
    //   displayed_XYZ = displayRGBtoXYZ * XYZtoWire * MHC2 * WireToXYZ * input_linear
    //
    // For correct colors: displayed_XYZ = srcRGBtoXYZ * input_linear
    // Therefore: displayRGBtoXYZ * MHC2 = srcRGBtoXYZ
    //   MHC2 = inv(displayRGBtoXYZ) * srcRGBtoXYZ
    // (For SDR and the legacy BT.2020-src HDR path the wire == src so the wire factors cancel; for
    // the native-src HDR path wire (BT.2020) != src (native) — but since Windows applies the matrix
    // directly to linear RGB with no XYZ wrap, "src" just SELECTS how that linear RGB is interpreted.
    // Native src ⇒ the matrix is a diagonal white-only move; the 3D LUT owns the BT.2020→native gamut.
    // See GenerateMHC2Profile's source-primaries note.)
    //
    // As-applied basis (corrected 2026-06-26, SDR_MHC2_XYZ_BASIS_BUG.md): Windows consumes the MHC2
    // matrix as a CIEXYZ "3x4 XYZ→XYZ adjustment", composing SrcRGBtoXYZ * Adjust * XYZtoTgtRGB. In
    // the source/target RGB basis the as-applied transform is therefore inv(B) * M * B (B = that
    // basis's RGB→XYZ NPM, M = the emitted tag matrix) — NOT a direct, wrap-free RGB→RGB apply.
    // We still COMPUTE `result` as the intended RGB→RGB forward transform inv(displayRGBtoXYZ) *
    // srcRGBtoXYZ (= display_XYZtoRGB ∘ source_RGBtoXYZ, matching dantmnf/MHC2Gen), then emit it so
    // the wrap reproduces it:
    //   * SDR: B = sRGB@D65 and the wrap is NOT harmless (it over-desaturates every primary ~3 dE),
    //     so we conjugate below — emit S*result*inv(S) so the as-applied inv(S)*emit*S == result.
    //     HW-confirmed on a PA32UCXR (R/G land on sRGB, white on D65 to meter noise).
    //   * HDR: B = BT.2020 and `result` is a near-diagonal white-only move (gamut identity), which is
    //     ~invariant under the BT.2020 wrap (~0.016 white error) — so DIRECT emission has worked and
    //     is kept. Do NOT conjugate HDR without its own offline replay + HW probe.
    // The earlier "no XYZ wrap" conclusion from the HDR white-correction work distinguished matrix
    // ORDER, not wrap PRESENCE — the wrap was simply harmless for HDR's near-diagonal matrix. That
    // order finding still stands: prior code used the REVERSED order srcRGBtoXYZ * inv(displayRGBtoXYZ)
    // on a false "operates in XYZ space" assumption; verified 2026-06-21 to leave the panel's native
    // white uncorrected — HDR white stayed ~native 0.323 vs D65 0.313 because the warm-channel
    // reduction came out as an (impossible, no-headroom) cool-channel boost at peak. SDR masked it via
    // the post-install refine loop; HDR has none.
    //
    // No Bradford adaptation - the matrix maps source↔native RGB linearly. The native→D65 white
    // move is encoded in displayRGBtoXYZ, which MUST be built with the panel's MEASURED native
    // white (so set_white carries the native display white, NOT the D65 target).
    //
    // White balance gains (optional): diagonal RGB scaling in wire space.
    // Baked into the matrix by scaling each column of srcToXYZ before the multiply:
    //   srcToXYZ_scaled[col_i] = srcToXYZ[col_i] * gains[i]
    // This shifts the white point from D65 to the target in a single matrix pass.

    float srcToXYZ[9], displayToXYZ[9], displayFromXYZ[9];
    if (!BuildRGBtoXYZ(srcPrimaries, srcToXYZ) || !BuildRGBtoXYZ(displayPrimaries, displayToXYZ)
        || !MatInv3(displayToXYZ, displayFromXYZ)) {
        std::cerr << "MHC2 matrix: degenerate primaries, using identity" << std::endl;
        memset(outMHC, 0, sizeof(float) * 12);
        outMHC[0] = outMHC[5] = outMHC[10] = 1.0f;  // 3x4 identity (row-major)
        return;
    }

    // Apply white balance gains to srcToXYZ columns (von Kries in wire RGB space)
    if (whiteBalanceGains) {
        // Column 0 (R): rows 0,3,6 of row-major matrix
        srcToXYZ[0] *= whiteBalanceGains[0]; srcToXYZ[3] *= whiteBalanceGains[0]; srcToXYZ[6] *= whiteBalanceGains[0];
        // Column 1 (G): rows 1,4,7
        srcToXYZ[1] *= whiteBalanceGains[1]; srcToXYZ[4] *= whiteBalanceGains[1]; srcToXYZ[7] *= whiteBalanceGains[1];
        // Column 2 (B): rows 2,5,8
        srcToXYZ[2] *= whiteBalanceGains[2]; srcToXYZ[5] *= whiteBalanceGains[2]; srcToXYZ[8] *= whiteBalanceGains[2];
    }

    // MHC2 = inv(displayRGBtoXYZ) * srcRGBtoXYZ_scaled  (RGB->RGB: source/wire -> display-native)
    float result[9];
    MatMul3(displayFromXYZ, srcToXYZ, result);

    std::cout << "MHC2 matrix (RGB-to-RGB, source->display):" << std::endl;
    std::cout << "  [" << result[0] << ", " << result[1] << ", " << result[2] << "]" << std::endl;
    std::cout << "  [" << result[3] << ", " << result[4] << ", " << result[5] << "]" << std::endl;
    std::cout << "  [" << result[6] << ", " << result[7] << ", " << result[8] << "]" << std::endl;

    // Capture the NET as-applied RGB->RGB transform (WB baked in) BEFORE the SDR basis
    // conjugation below overwrites `result` with the emitted tag. This pre-conjugation
    // `result` is what the panel sees (Windows applies inv(S)*emit*S == result for SDR),
    // and what the live grayscale full-preview shader reproduces. (mhc_icc.cpp:306 pipeline.)
    if (outAsAppliedRGB) memcpy(outAsAppliedRGB, result, sizeof(float) * 9);

    // SDR-only basis conjugation (SDR_MHC2_XYZ_BASIS_BUG.md). Windows consumes the SDR MHC2 matrix as
    // a CIEXYZ "3x4 XYZ→XYZ adjustment", composing SrcRGBtoXYZ * Adjust * XYZtoTgtRGB; for src=tgt=sRGB
    // the as-applied transform is inv(S)*result*S (see basis note above). `result` is a direct RGB→RGB
    // gamut matrix, so emit it conjugated into that basis (store S*result*inv(S)) and the as-applied
    // result is exactly `result`. S*result*inv(S) is the spec-correct XYZ→XYZ adjustment, and because
    // it conjugates `result` it carries the white-balance gains already baked into srcToXYZ above (a
    // simplified S*inv(native) form would drop them). HDR's near-diagonal native-src matrix is
    // ~invariant under this conjugation (which is why direct emission works there) — the !isHDR guard
    // is required; an unguarded conjugation would inject ~0.016 white error on HDR.
    if (!isHDR) {
        float srgbToXYZ[9], srgbFromXYZ[9], tmp[9], emit[9];
        if (BuildRGBtoXYZ(g_srgbPrimaries, srgbToXYZ) && MatInv3(srgbToXYZ, srgbFromXYZ)) {
            MatMul3(srgbToXYZ, result, tmp);   // S * result
            MatMul3(tmp, srgbFromXYZ, emit);   // (S * result) * inv(S)
            memcpy(result, emit, sizeof(float) * 9);
            std::cout << "MHC2 matrix (SDR XYZ-basis emit, S*M*inv(S)):" << std::endl;
            std::cout << "  [" << result[0] << ", " << result[1] << ", " << result[2] << "]" << std::endl;
            std::cout << "  [" << result[3] << ", " << result[4] << ", " << result[5] << "]" << std::endl;
            std::cout << "  [" << result[6] << ", " << result[7] << ", " << result[8] << "]" << std::endl;
        }
    }

    // Pack into 3x4 row-major (4th column = 0)
    outMHC[0]  = result[0]; outMHC[1]  = result[1]; outMHC[2]  = result[2]; outMHC[3]  = 0.0f;
    outMHC[4]  = result[3]; outMHC[5]  = result[4]; outMHC[6]  = result[5]; outMHC[7]  = 0.0f;
    outMHC[8]  = result[6]; outMHC[9]  = result[7]; outMHC[10] = result[8]; outMHC[11] = 0.0f;
}

// Compute the SDR scanout (net as-applied matrix + per-channel base 1D LUT) the live grayscale
// full-preview shader reproduces. Mirrors the SDR matrix + base-LUT generation in
// GenerateMHC2Profile (srcPrim=sRGB; displayPrim=params.displayPrimaries when primariesEnabled;
// LUT source = 1D-cube corr / per-channel TRC / base grayscale, in that priority). corrGS is NOT
// composed here — the shader applies it live (params should be built for the PERM_GS-stripped perm).
bool ComputeSdrScanoutForShader(const MHC2ProfileParams& params,
                                float outResult9[9],
                                std::vector<float>& outBaseLutR,
                                std::vector<float>& outBaseLutG,
                                std::vector<float>& outBaseLutB) {
    if (params.isHDR) return false;
    const int lutSize = 1024;

    // SDR source/display primaries — identical derivation to GenerateMHC2Profile (SDR branch).
    const DisplayPrimariesData& srcPrim = g_srgbPrimaries;
    const DisplayPrimariesData& displayPrim = params.primariesEnabled
        ? params.displayPrimaries : g_srgbPrimaries;
    bool hasWB = (params.whiteBalanceGains[0] != 1.0f || params.whiteBalanceGains[1] != 1.0f
                  || params.whiteBalanceGains[2] != 1.0f);
    const float* wbGains = hasWB ? params.whiteBalanceGains : nullptr;

    float mhcMatrix[12];
    ComputeMHC2Matrix(srcPrim, displayPrim, false, mhcMatrix, wbGains, outResult9);

    outBaseLutR.assign(lutSize, 0.0f);
    outBaseLutG.assign(lutSize, 0.0f);
    outBaseLutB.assign(lutSize, 0.0f);

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
        resampleCurve(params.corrR, outBaseLutR.data(), lutSize);
        resampleCurve(params.corrG, outBaseLutG.data(), lutSize);
        resampleCurve(params.corrB, outBaseLutB.data(), lutSize);
    } else if (params.hasPerChannelTRC && !params.trcR.empty() && !params.trcG.empty() && !params.trcB.empty()) {
        float targetGamma = params.grayscale.use24Gamma ? 2.4f : 2.2f;
        GenerateMHC2LUT_FromTRC_SDR(params.trcR, outBaseLutR.data(), lutSize, targetGamma);
        GenerateMHC2LUT_FromTRC_SDR(params.trcG, outBaseLutG.data(), lutSize, targetGamma);
        GenerateMHC2LUT_FromTRC_SDR(params.trcB, outBaseLutB.data(), lutSize, targetGamma);
    } else {
        GenerateMHC2LUT_SDR_Channel(params.grayscale, outBaseLutR.data(), lutSize, 0);
        GenerateMHC2LUT_SDR_Channel(params.grayscale, outBaseLutG.data(), lutSize, 1);
        GenerateMHC2LUT_SDR_Channel(params.grayscale, outBaseLutB.data(), lutSize, 2);
    }
    return true;
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
    //
    // SOURCE (target) primaries:
    //   SDR  → sRGB. sRGB is INSIDE the panel's gamut, so it's reachable: blue maps near-identity,
    //          desktop content clamps correctly, no near-zero-red routing.
    //   HDR  → the panel's MEASURED NATIVE primaries (NOT BT.2020). Targeting BT.2020 asks the
    //          MHC2 matrix to map the sub-Rec.2020 panel UP to BT.2020; that routes in-gamut /
    //          desaturated blue (e.g. [0.54,0.54,0.729]) through a near-ZERO native-red drive, and
    //          the panel is sub-additive at near-zero red, so it renders ~0 light there →
    //          RED-CHANNEL COLLAPSE (in-gamut blues rendered WORSE than uncorrected; HW-measured
    //          47-53 dE_ITP vs native ~2-9). With native src, srcRGBtoXYZ shares the panel's
    //          chromaticities with displayRGBtoXYZ, so MHC2 = inv(displayToXYZ)·srcToXYZ degenerates
    //          to a pure DIAGONAL white-only move (native white → D65) with GAMUT IDENTITY — no
    //          remap, no collapse. The panel renders its native gamut at D65 grayscale; the 3D-LUT
    //          layer owns in-gamut color volume (1+1+1 design). Validated 2026-06-23 on PA32UCXR
    //          (via DLC_SRC_NATIVE=1): the four collapsed in-gamut blues dropped 49→13 dE_ITP,
    //          grayscale held D65. See the mhc-blue-red-channel-collapse memo.
    //
    // The native HDR src carries the D65 TARGET white (like the sRGB/BT.2020 src whites); displayPrim
    // carries the panel's MEASURED native white (via set_white, populated when primariesEnabled). The
    // native→D65 move is exactly that src↔display white delta — building src from the native white too
    // would make S_src == S_disp ⇒ ZERO white move (the panel's native white would pass straight
    // through). Wire interpretation: Windows wraps the matrix in the source/target RGB↔XYZ basis
    // (as-applied inv(B)*M*B, see ComputeMHC2Matrix), but HDR's near-diagonal white-only matrix is
    // ~invariant under that BT.2020 wrap, so it behaves as a direct linear-RGB apply — choosing
    // native src simply REINTERPRETS the incoming BT.2020-container linear RGB as native-gamut+D65
    // content. The old "wire cancels since wire = src" identity no longer holds for HDR, and that is
    // intentional (the 3D LUT, not the MHC, owns BT.2020→native).
    DisplayPrimariesData hdrNativeSrc{};
    const DisplayPrimariesData* srcPrimPtr;
    if (!params.isHDR) {
        srcPrimPtr = &g_srgbPrimaries;
    } else if (params.primariesEnabled) {
        hdrNativeSrc = params.displayPrimaries;  // native R/G/B chromaticities + measured native white
        hdrNativeSrc.Wx = 0.3127f;               // ...but src TARGETS D65 (white move = src↔display delta)
        hdrNativeSrc.Wy = 0.3290f;
        srcPrimPtr = &hdrNativeSrc;
    } else {
        srcPrimPtr = &g_bt2020Primaries;  // no measured primaries → display==src → identity (old no-op)
    }
    const DisplayPrimariesData& srcPrim = *srcPrimPtr;
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

    // Check if white balance gains are non-identity
    bool hasWB = (params.whiteBalanceGains[0] != 1.0f || params.whiteBalanceGains[1] != 1.0f || params.whiteBalanceGains[2] != 1.0f);
    const float* wbGains = hasWB ? params.whiteBalanceGains : nullptr;

    float mhcMatrix[12];
    ComputeMHC2Matrix(srcPrim, displayPrim, params.isHDR, mhcMatrix, wbGains);

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
        // Note: TRC may not reach 1.0 on all channels (e.g. Curves+Matrix HDR profiles where
        // per-channel peaks differ). This is valid data — InvertTRC handles it by clamping.
        // LUT-based profiles with garbage fallback TRC are rejected at import time in gui_mhc.cpp.
        if (params.isHDR) {
            // ICC TRC from HDR-mode profiling (e.g. DisplayCal with PQ pattern generator like
            // Dogegen/Resolve): TRC maps PQ signal → measured linear light (normalized to peak).
            // Use per-channel PQ correction to fix the display's PQ tracking errors.
            std::cout << "MHC2: Using per-channel TRC for HDR PQ correction (" << params.trcR.size() << " points, peak " << params.peakNits << " nits)" << std::endl;
            GenerateMHC2LUT_FromTRC_HDR(params.trcR, lutR.data(), lutSize, params.peakNits);
            GenerateMHC2LUT_FromTRC_HDR(params.trcG, lutG.data(), lutSize, params.peakNits);
            GenerateMHC2LUT_FromTRC_HDR(params.trcB, lutB.data(), lutSize, params.peakNits);
        } else {
            float targetGamma = params.grayscale.use24Gamma ? 2.4f : 2.2f;
            std::cout << "MHC2: Using per-channel TRC (" << params.trcR.size() << " points, target gamma " << targetGamma << ")" << std::endl;
            GenerateMHC2LUT_FromTRC_SDR(params.trcR, lutR.data(), lutSize, targetGamma);
            GenerateMHC2LUT_FromTRC_SDR(params.trcG, lutG.data(), lutSize, targetGamma);
            GenerateMHC2LUT_FromTRC_SDR(params.trcB, lutB.data(), lutSize, targetGamma);
        }
    } else if (params.isHDR) {
        GenerateMHC2LUT_HDR_Channel(params.grayscale, params.peakNits, lutR.data(), lutSize, 0);
        GenerateMHC2LUT_HDR_Channel(params.grayscale, params.peakNits, lutG.data(), lutSize, 1);
        GenerateMHC2LUT_HDR_Channel(params.grayscale, params.peakNits, lutB.data(), lutSize, 2);
    } else {
        GenerateMHC2LUT_SDR_Channel(params.grayscale, lutR.data(), lutSize, 0);
        GenerateMHC2LUT_SDR_Channel(params.grayscale, lutG.data(), lutSize, 1);
        GenerateMHC2LUT_SDR_Channel(params.grayscale, lutB.data(), lutSize, 2);
    }

    // ========================================================================
    // Compose additional corrections on top of base LUT
    // Order: correction grayscale first (calibration), then desktop gamma (look).
    // DG must be last so correction GS sees un-DG'd values matching calibration.
    // ========================================================================

    bool hasDG = params.isHDR && params.desktopGammaEnabled;
    bool hasCorrGS = params.correctionGrayscaleEnabled && params.correctionGrayscale.enabled;

    if (hasDG || hasCorrGS) {
        float pqPeak = params.isHDR ? PqOETF(params.peakNits / 10000.0f) : 0.0f;

        float* luts[3] = { lutR.data(), lutG.data(), lutB.data() };
        for (int ch = 0; ch < 3; ch++) {
            for (int j = 0; j < lutSize; j++) {
                float v = luts[ch][j];  // Base LUT output (signal domain)

                if (params.isHDR) {
                    // HDR: LUT output is PQ signal

                    // Correction grayscale first (calibration refinement)
                    // Must precede DG so the correction curve sees un-DG'd values
                    // matching its calibration conditions.
                    if (hasCorrGS) {
                        v = EvalGrayscaleHDR_Channel(v, params.correctionGrayscale, pqPeak, ch);
                    }

                    // Desktop gamma last: sRGB→2.2 for SDR luminance range
                    if (hasDG) {
                        float linearNits = PqEOTF(v) * 10000.0f;
                        if (linearNits <= 80.0f && linearNits > 0.0f) {
                            float sdrLinear = linearNits / 80.0f;
                            float srgbEncoded = SrgbOETF(sdrLinear);
                            float gamma22 = powf(srgbEncoded, 2.2f);
                            v = PqOETF(gamma22 * 80.0f / 10000.0f);
                        }
                    }
                } else {
                    // SDR: LUT output is sRGB signal

                    // Correction grayscale (fine-tuning on top of base). SIGNAL-domain
                    // (mirrors GenerateMHC2LUT_SDR_Channel / the shader): feed the signal `v`
                    // straight into the eval — NO sRGB decode/encode around it, or the slot
                    // index would land on the wrong slider and disagree with the live preview.
                    if (hasCorrGS) {
                        float corrected = EvalGrayscaleSDR_Channel(v, params.correctionGrayscale, ch);
                        // Optional 2.2→2.4 gamma is a linear-light transform: decode, pow, re-encode.
                        if (params.correctionGrayscale.use24Gamma) {
                            float lin = SrgbEOTF((std::max)(corrected, 0.0f));
                            lin = powf((std::max)(lin, 0.0f), 2.4f / 2.2f);
                            corrected = SrgbOETF((std::max)(lin, 0.0f));
                        }
                        v = corrected;
                    }
                }

                luts[ch][j] = std::clamp(v, 0.0f, 1.0f);
            }
        }

        if (hasDG) std::cout << "MHC2: Desktop gamma (sRGB→2.2) composed into HDR LUT" << std::endl;
        if (hasCorrGS) std::cout << "MHC2: Correction grayscale composed on top of base LUT" << std::endl;
    }

    // Build ICC colorants (D50-adapted)
    // Always use wire-format primaries (sRGB for SDR, BT.2020 for HDR) so the profile's
    // ICC tags match what Windows assumes for the wire format. The MHC2 matrix handles
    // the actual gamut correction independently, just like SDR.
    const DisplayPrimariesData& iccPrimaries = params.isHDR
        ? g_bt2020Primaries
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

    // rTRC/gTRC/bTRC - display EOTF tag for CMM-aware apps (shared tag, same offset for all
    // three). SDR: a pure power-law curv (2.2 default, 2.4 for BT.1886) — the owner's "2.2,
    // never piecewise sRGB" rule. HDR: the sRGB piecewise curve — the standards-correct neutral
    // tag matching the MHC2 reference (dantmnf/MHC2Gen). NOTE: in HDR/Advanced-Color Windows
    // ignores this tag for the PQ path; it only affects apps that color-manage against the
    // profile directly. (See WriteParaTagSrgb.)
    if (params.isHDR) {
        WriteParaTagSrgb(tagTRC);
    } else {
        float profileGamma = params.grayscale.use24Gamma ? 2.4f : 2.2f;
        WriteCurvTagGamma(tagTRC, profileGamma);
    }

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
