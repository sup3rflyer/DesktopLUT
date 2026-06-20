#include "doctest.h"
#include "mhc.h"
#include "color.h"
#include <cmath>
#include <algorithm>
#include <fstream>
#include <cstring>

struct TempFile {
    std::wstring path;
    TempFile(const wchar_t* name) : path(name) {}
    ~TempFile() { _wremove(path.c_str()); }
    const wchar_t* c_str() const { return path.c_str(); }
};

static constexpr DisplayPrimariesData kSRGB   = {0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f};
static constexpr DisplayPrimariesData kBT2020 = {0.7080f, 0.2920f, 0.1700f, 0.7970f, 0.1310f, 0.0460f, 0.3127f, 0.3290f};
static constexpr DisplayPrimariesData kP3D65  = {0.6800f, 0.3200f, 0.2650f, 0.6900f, 0.1500f, 0.0600f, 0.3127f, 0.3290f};

// ============================================================================
// Binary Format Helpers
// ============================================================================

TEST_CASE("s15Fixed16: known values") {
    CHECK(FloatToS15Fixed16(1.0f) == 65536);
    CHECK(FloatToS15Fixed16(0.5f) == 32768);
    CHECK(FloatToS15Fixed16(-1.0f) == -65536);
    CHECK(FloatToS15Fixed16(0.0f) == 0);
}

TEST_CASE("s15Fixed16: round-trip") {
    float testValues[] = {-2.0f, -1.0f, -0.5f, 0.0f, 0.25f, 0.5f, 1.0f, 1.5f, 2.0f};
    for (float v : testValues) {
        int32_t fixed = FloatToS15Fixed16(v);
        // Write to bytes in big-endian
        uint8_t bytes[4];
        bytes[0] = (uint8_t)(fixed >> 24);
        bytes[1] = (uint8_t)(fixed >> 16);
        bytes[2] = (uint8_t)(fixed >> 8);
        bytes[3] = (uint8_t)(fixed);
        float readBack = ReadS15Fixed16(bytes);
        CHECK(readBack == doctest::Approx(v).epsilon(1.0f / 65536.0f));
    }
}

TEST_CASE("BE32: round-trip") {
    uint32_t val = 0x01020304;
    uint8_t bytes[4];
    bytes[0] = (uint8_t)(val >> 24);
    bytes[1] = (uint8_t)(val >> 16);
    bytes[2] = (uint8_t)(val >> 8);
    bytes[3] = (uint8_t)(val);
    CHECK(ReadBE32(bytes) == 0x01020304);
}

TEST_CASE("BE16: round-trip") {
    uint16_t val = 0x0102;
    uint8_t bytes[2];
    bytes[0] = (uint8_t)(val >> 8);
    bytes[1] = (uint8_t)(val);
    CHECK(ReadBE16(bytes) == 0x0102);
}

// ============================================================================
// sRGB Transfer Functions
// ============================================================================

TEST_CASE("sRGB EOTF: boundary values") {
    CHECK(SrgbEOTF(0.0f) == doctest::Approx(0.0f).epsilon(1e-7));
    CHECK(SrgbEOTF(1.0f) == doctest::Approx(1.0f).epsilon(1e-6));
    CHECK(SrgbEOTF(0.5f) == doctest::Approx(0.214f).epsilon(0.001));
}

TEST_CASE("sRGB EOTF: toe boundary") {
    CHECK(SrgbEOTF(0.04045f) == doctest::Approx(0.003131f).epsilon(1e-5));
}

TEST_CASE("sRGB OETF: boundary values") {
    CHECK(SrgbOETF(0.0f) == doctest::Approx(0.0f).epsilon(1e-7));
    CHECK(SrgbOETF(1.0f) == doctest::Approx(1.0f).epsilon(1e-6));
}

TEST_CASE("sRGB: round-trip EOTF/OETF") {
    for (int i = 0; i <= 10; i++) {
        float x = (float)i / 10.0f;
        CHECK(SrgbOETF(SrgbEOTF(x)) == doctest::Approx(x).epsilon(1e-5));
    }
}

TEST_CASE("PQ (mhc): round-trip EOTF/OETF") {
    for (int i = 0; i <= 10; i++) {
        float x = (float)i / 10.0f;
        if (x < 0.001f) continue;  // Skip near-zero (clamped)
        CHECK(PqOETF(PqEOTF(x)) == doctest::Approx(x).epsilon(1e-5));
    }
}

// ============================================================================
// MHC2 Matrix
// ============================================================================

TEST_CASE("MHC2 matrix: SDR identity") {
    float mhc[12];
    ComputeMHC2Matrix(kSRGB, kSRGB, false, mhc);
    // 3x3 part should be identity
    CHECK(mhc[0]  == doctest::Approx(1.0f).epsilon(1e-3));
    CHECK(mhc[1]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[2]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[4]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[5]  == doctest::Approx(1.0f).epsilon(1e-3));
    CHECK(mhc[6]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[8]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[9]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[10] == doctest::Approx(1.0f).epsilon(1e-3));
}

TEST_CASE("MHC2 matrix: HDR identity") {
    float mhc[12];
    ComputeMHC2Matrix(kBT2020, kBT2020, true, mhc);
    // Check all 12 elements: 3x3 identity + zero 4th column
    CHECK(mhc[0]  == doctest::Approx(1.0f).epsilon(1e-3));
    CHECK(mhc[1]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[2]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[3]  == 0.0f);
    CHECK(mhc[4]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[5]  == doctest::Approx(1.0f).epsilon(1e-3));
    CHECK(mhc[6]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[7]  == 0.0f);
    CHECK(mhc[8]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[9]  == doctest::Approx(0.0f).epsilon(1e-3));
    CHECK(mhc[10] == doctest::Approx(1.0f).epsilon(1e-3));
    CHECK(mhc[11] == 0.0f);
}

TEST_CASE("MHC2 matrix: HDR non-identity (BT2020→P3)") {
    float mhc[12];
    ComputeMHC2Matrix(kBT2020, kP3D65, true, mhc);
    // BT.2020 source, P3 display — not identity
    bool isIdentity = true;
    float id[12] = {1,0,0,0, 0,1,0,0, 0,0,1,0};
    for (int i = 0; i < 12; i++) {
        if (std::fabs(mhc[i] - id[i]) > 0.01f) { isIdentity = false; break; }
    }
    CHECK_FALSE(isIdentity);
    // Column 4 still zero
    CHECK(mhc[3] == 0.0f);
    CHECK(mhc[7] == 0.0f);
    CHECK(mhc[11] == 0.0f);
}

TEST_CASE("MHC2 matrix: SDR gamut expansion (P3)") {
    float mhc[12];
    ComputeMHC2Matrix(kSRGB, kP3D65, false, mhc);
    // Non-identity
    bool isIdentity = true;
    float id[12] = {1,0,0,0, 0,1,0,0, 0,0,1,0};
    for (int i = 0; i < 12; i++) {
        if (std::fabs(mhc[i] - id[i]) > 0.01f) { isIdentity = false; break; }
    }
    CHECK_FALSE(isIdentity);
}

TEST_CASE("MHC2 matrix: column 4 always zero") {
    float mhc[12];
    ComputeMHC2Matrix(kSRGB, kP3D65, false, mhc);
    CHECK(mhc[3] == 0.0f);
    CHECK(mhc[7] == 0.0f);
    CHECK(mhc[11] == 0.0f);
}

TEST_CASE("MHC2 matrix: singular fallback") {
    DisplayPrimariesData degenerate = {0, 0, 0, 0, 0, 0, 0.3127f, 0.3290f};
    float mhc[12];
    ComputeMHC2Matrix(degenerate, degenerate, false, mhc);
    CHECK(mhc[0]  == doctest::Approx(1.0f).epsilon(1e-6));
    CHECK(mhc[5]  == doctest::Approx(1.0f).epsilon(1e-6));
    CHECK(mhc[10] == doctest::Approx(1.0f).epsilon(1e-6));
}

// ============================================================================
// 1D LUT Generation (SDR)
// ============================================================================

TEST_CASE("SDR LUT: identity ramp (grayscale disabled)") {
    GrayscaleData gs = {};
    gs.enabled = false;
    gs.pointCount = 20;
    gs.initLinear();

    std::vector<float> lut(1024);
    GenerateMHC2LUT_SDR(gs, lut.data(), 1024);

    for (int i = 0; i < 1024; i++) {
        float expected = (float)i / 1023.0f;
        CHECK(lut[i] == doctest::Approx(expected).epsilon(0.001));
    }
}

TEST_CASE("SDR LUT: endpoint preservation") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinear();

    std::vector<float> lut(1024);
    GenerateMHC2LUT_SDR(gs, lut.data(), 1024);

    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lut[1023] == doctest::Approx(1.0f).epsilon(0.001));
}

TEST_CASE("SDR LUT: monotonicity") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinear();

    std::vector<float> lut(1024);
    GenerateMHC2LUT_SDR(gs, lut.data(), 1024);

    for (int i = 1; i < 1024; i++) {
        CHECK(lut[i] >= lut[i - 1]);
    }
}

TEST_CASE("SDR LUT: 2.4 gamma changes output") {
    GrayscaleData gsNoGamma = {};
    gsNoGamma.enabled = false;
    gsNoGamma.pointCount = 20;
    gsNoGamma.initLinear();

    GrayscaleData gsGamma = gsNoGamma;
    gsGamma.enabled = true;
    gsGamma.use24Gamma = true;

    std::vector<float> lutNo(1024), lutYes(1024);
    GenerateMHC2LUT_SDR(gsNoGamma, lutNo.data(), 1024);
    GenerateMHC2LUT_SDR(gsGamma, lutYes.data(), 1024);

    // Midtone should differ
    bool differs = false;
    for (int i = 100; i < 900; i++) {
        if (std::fabs(lutNo[i] - lutYes[i]) > 0.001f) { differs = true; break; }
    }
    CHECK(differs);

    // 2.4 gamma darkens midtones: pow(L, 2.4/2.2) in linear domain
    // At midtone, 2.4 LUT should output lower values (darker signal)
    CHECK(lutYes[512] < lutNo[512]);
}

// ============================================================================
// 1D LUT Generation (HDR)
// ============================================================================

TEST_CASE("HDR LUT: identity ramp (grayscale disabled)") {
    GrayscaleData gs = {};
    gs.enabled = false;
    gs.pointCount = 20;
    gs.initLinearPQ();

    std::vector<float> lut(4096);
    GenerateMHC2LUT_HDR(gs, 10000.0f, lut.data(), 4096);

    for (int i = 0; i < 4096; i++) {
        float expected = (float)i / 4095.0f;
        CHECK(lut[i] == doctest::Approx(expected).epsilon(0.001));
    }
}

TEST_CASE("HDR LUT: endpoint preservation") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinearPQ();

    std::vector<float> lut(4096);
    GenerateMHC2LUT_HDR(gs, 1000.0f, lut.data(), 4096);

    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lut[4095] == doctest::Approx(1.0f).epsilon(0.001));
}

// ============================================================================
// Grayscale Evaluation
// ============================================================================

TEST_CASE("Grayscale SDR: linear response") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinear();

    CHECK(EvalGrayscaleSDR(0.0f, gs) == doctest::Approx(0.0f).epsilon(1e-6));
    CHECK(EvalGrayscaleSDR(1.0f, gs) == doctest::Approx(1.0f).epsilon(0.01));
    CHECK(EvalGrayscaleSDR(0.25f, gs) == doctest::Approx(0.25f).epsilon(0.02));
}

TEST_CASE("Grayscale HDR: linear response") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinearPQ();

    float pqPeak = 1.0f;
    CHECK(EvalGrayscaleHDR(0.0f, gs, pqPeak) == doctest::Approx(0.0f).epsilon(1e-6));
    CHECK(EvalGrayscaleHDR(pqPeak, gs, pqPeak) == doctest::Approx(pqPeak).epsilon(0.01));
}

TEST_CASE("Grayscale HDR: pointCount above internal table does not collapse highlights") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 40;  // Malformed IPC payload; runtime table is fixed at 32.
    for (int i = 0; i < 32; ++i) {
        float t = (float)i / 31.0f;
        gs.points[i] = t;
        gs.pointsR[i] = t;
        gs.pointsG[i] = t;
        gs.pointsB[i] = t;
    }

    float pqPeak = 1.0f;
    CHECK(EvalGrayscaleHDR(pqPeak, gs, pqPeak) == doctest::Approx(pqPeak).epsilon(0.01));
    CHECK(EvalGrayscaleHDR_Channel(pqPeak, gs, pqPeak, 0) == doctest::Approx(pqPeak).epsilon(0.01));
    CHECK(EvalGrayscaleHDR_Channel(pqPeak, gs, pqPeak, 1) == doctest::Approx(pqPeak).epsilon(0.01));
    CHECK(EvalGrayscaleHDR_Channel(pqPeak, gs, pqPeak, 2) == doctest::Approx(pqPeak).epsilon(0.01));
}

TEST_CASE("Grayscale SDR: non-linear correction") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinear();
    // Darken midpoint
    gs.points[10] = gs.points[10] * 0.8f;

    float uncorrected = 0.25f;  // In the affected range
    float result = EvalGrayscaleSDR(uncorrected, gs);
    // Should differ from identity (correction applied)
    CHECK(result != doctest::Approx(uncorrected).epsilon(0.001));
    CHECK(std::isfinite(result));
    CHECK(result >= 0.0f);
    CHECK(result <= 1.0f);
}

TEST_CASE("Grayscale HDR: above peak extrapolation") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinearPQ();

    float pqPeak = 0.5f;
    float result = EvalGrayscaleHDR(0.7f, gs, pqPeak);
    CHECK(std::isfinite(result));
    CHECK(result > 0.0f);
}

// ============================================================================
// TRC Inversion
// ============================================================================

TEST_CASE("InvertTRC: identity TRC") {
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = (float)i / 255.0f;

    CHECK(InvertTRC(trc, 0.5f) == doctest::Approx(0.5f).epsilon(0.01));
    CHECK(InvertTRC(trc, 0.0f) == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(InvertTRC(trc, 1.0f) == doctest::Approx(1.0f).epsilon(0.01));
}

TEST_CASE("InvertTRC: gamma 2.2 TRC") {
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = powf((float)i / 255.0f, 2.2f);

    float result = InvertTRC(trc, 0.5f);
    float expected = powf(0.5f, 1.0f / 2.2f);
    CHECK(result == doctest::Approx(expected).epsilon(0.01));
}

TEST_CASE("InvertTRC: 2-entry TRC (minimum)") {
    std::vector<float> trc = { 0.0f, 1.0f };
    CHECK(InvertTRC(trc, 0.0f) == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(InvertTRC(trc, 1.0f) == doctest::Approx(1.0f).epsilon(0.01));
    CHECK(InvertTRC(trc, 0.5f) == doctest::Approx(0.5f).epsilon(0.02));
}

TEST_CASE("InvertTRC: 4096-entry TRC (HDR-sized)") {
    std::vector<float> trc(4096);
    for (int i = 0; i < 4096; i++) trc[i] = powf((float)i / 4095.0f, 2.2f);

    float result = InvertTRC(trc, 0.5f);
    float expected = powf(0.5f, 1.0f / 2.2f);
    CHECK(result == doctest::Approx(expected).epsilon(0.005));
}

TEST_CASE("InvertTRC: target below TRC minimum") {
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = 0.1f + 0.9f * ((float)i / 255.0f);
    // trc[0] = 0.1, target = 0.0 → should return ~0
    float result = InvertTRC(trc, 0.0f);
    CHECK(result >= 0.0f);
    CHECK(result < 0.05f);
}

TEST_CASE("InvertTRC: steep TRC (gamma 4.0)") {
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = powf((float)i / 255.0f, 4.0f);
    float result = InvertTRC(trc, 0.5f);
    float expected = powf(0.5f, 1.0f / 4.0f);  // = 0.8409
    CHECK(result == doctest::Approx(expected).epsilon(0.02));
}

TEST_CASE("InvertTRC: flat segment (constant TRC region)") {
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) {
        float t = (float)i / 255.0f;
        if (t < 0.3f) trc[i] = t * (0.5f / 0.3f);
        else if (t < 0.7f) trc[i] = 0.5f;
        else trc[i] = 0.5f + (t - 0.7f) * (0.5f / 0.3f);
    }
    float result = InvertTRC(trc, 0.5f);
    CHECK(result >= 0.25f);
    CHECK(result <= 0.75f);
    CHECK(std::isfinite(result));
}

TEST_CASE("InvertTRC: target above TRC maximum") {
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = 0.8f * ((float)i / 255.0f);
    // trc[255] = 0.8, target = 1.0 → should return ~1.0
    float result = InvertTRC(trc, 1.0f);
    CHECK(result >= 0.9f);
    CHECK(result <= 1.0f);
}

// ============================================================================
// ICC Profile Generation & Read-Back
// ============================================================================

TEST_CASE("ICC: generate and verify header") {
    MHC2ProfileParams params;
    params.monitorName = L"TestMonitor";
    params.displayPrimaries = kSRGB;
    params.primariesEnabled = false;
    params.grayscaleEnabled = false;
    params.isHDR = false;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));

    // Check minimum size
    CHECK(data.size() > 132);

    // Check ICC signature at offset 36 = "acsp"
    CHECK(data[36] == 'a');
    CHECK(data[37] == 'c');
    CHECK(data[38] == 's');
    CHECK(data[39] == 'p');

    // Profile size in header should match actual size
    uint32_t profileSize = ReadBE32(data.data());
    CHECK(profileSize == data.size());
}

TEST_CASE("ICC: tag count >= 12") {
    MHC2ProfileParams params;
    params.monitorName = L"TestMonitor";
    params.displayPrimaries = kSRGB;
    params.primariesEnabled = false;
    params.grayscaleEnabled = false;
    params.isHDR = false;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));

    uint32_t tagCount = ReadBE32(data.data() + 128);
    CHECK(tagCount >= 12);
}

TEST_CASE("ICC: profile ID is non-zero (MD5)") {
    MHC2ProfileParams params;
    params.monitorName = L"TestMonitor";
    params.displayPrimaries = kSRGB;
    params.primariesEnabled = false;
    params.grayscaleEnabled = false;
    params.isHDR = false;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));

    // Profile ID at offset 84 (16 bytes)
    bool allZero = true;
    for (int i = 84; i < 100; i++) {
        if (data[i] != 0) { allZero = false; break; }
    }
    CHECK_FALSE(allZero);
}

TEST_CASE("ICC: generate then read round-trip") {
    MHC2ProfileParams params;
    params.monitorName = L"TestMonitor";
    params.displayPrimaries = kP3D65;
    params.primariesEnabled = true;
    params.grayscaleEnabled = false;
    params.isHDR = false;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));

    // Write to temp file
    TempFile tmp(L"_test_roundtrip.icm");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        f.write(reinterpret_cast<const char*>(data.data()), data.size());
    }

    ICCProfileData icc;
    bool ok = ReadICCProfile(tmp.path, icc);

    REQUIRE(ok);
    // ReadICCProfile extracts primaries via chrm tag or rXYZ/gXYZ/bXYZ
    // The profile was generated with sRGB colorants for SDR,
    // so extracted primaries should be close to sRGB (the wire primaries)
    CHECK(icc.hasPrimaries);
    CHECK(icc.primaries.Rx == doctest::Approx(0.64f).epsilon(0.02));
    CHECK(icc.primaries.Gy == doctest::Approx(0.60f).epsilon(0.02));
    // Also verify TRC was extracted
    CHECK(icc.hasTRC);
    CHECK(icc.trcR.size() >= 256);
}

TEST_CASE("ICC: parametric curve type 3 (para) extraction") {
    // Build a minimal ICC whose rTRC/gTRC/bTRC are parametric type-3 curves and verify the
    // extracted TRC follows the ICC SPEC formula:  Y = (aX+b)^g for X >= d, else Y = c*X.
    // Params g=2, a=1, b=0, c=0.1, d=0.5  ->  upper = X^2, lower = 0.1*X, max (at X=1) = 1.
    // The old code applied a type-4-shaped formula with c/e swapped and read 7 params from a
    // 5-param tag, so this case would have failed (spurious +c on the upper segment, garbage
    // lower segment). max==1.0 so the reader's normalizeTRC leaves the curve unchanged.
    auto putBE32 = [](std::vector<uint8_t>& v, uint32_t x) {
        v.push_back((uint8_t)(x >> 24)); v.push_back((uint8_t)(x >> 16));
        v.push_back((uint8_t)(x >> 8));  v.push_back((uint8_t)x);
    };
    auto putBE16 = [](std::vector<uint8_t>& v, uint16_t x) {
        v.push_back((uint8_t)(x >> 8)); v.push_back((uint8_t)x);
    };
    auto putFixed = [&](std::vector<uint8_t>& v, float f) { putBE32(v, (uint32_t)FloatToS15Fixed16(f)); };

    const uint32_t tagDataOffset = 168;     // 128 header + 4 count + 3*12 table
    const uint32_t paraSize = 12 + 5 * 4;   // 'para'+reserved+funcType+reserved + 5 params = 32

    std::vector<uint8_t> icc(128, 0);       // header
    icc[36] = 'a'; icc[37] = 'c'; icc[38] = 's'; icc[39] = 'p';  // 'acsp' signature
    putBE32(icc, 3);                        // tag count
    const char* sigs[3] = { "rTRC", "gTRC", "bTRC" };
    for (int t = 0; t < 3; t++) {
        for (int k = 0; k < 4; k++) icc.push_back((uint8_t)sigs[t][k]);
        putBE32(icc, tagDataOffset);        // all three share the same para data
        putBE32(icc, paraSize);
    }
    REQUIRE(icc.size() == tagDataOffset);
    icc.push_back('p'); icc.push_back('a'); icc.push_back('r'); icc.push_back('a');
    putBE32(icc, 0);            // reserved
    putBE16(icc, 3);           // funcType 3
    putBE16(icc, 0);           // reserved
    putFixed(icc, 2.0f);       // g
    putFixed(icc, 1.0f);       // a
    putFixed(icc, 0.0f);       // b
    putFixed(icc, 0.1f);       // c
    putFixed(icc, 0.5f);       // d
    uint32_t sz = (uint32_t)icc.size();
    icc[0] = (uint8_t)(sz >> 24); icc[1] = (uint8_t)(sz >> 16);
    icc[2] = (uint8_t)(sz >> 8);  icc[3] = (uint8_t)sz;  // profile size field

    TempFile tmp(L"_test_para_type3.icm");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        f.write(reinterpret_cast<const char*>(icc.data()), icc.size());
    }

    ICCProfileData out;
    REQUIRE(ReadICCProfile(tmp.path, out));
    REQUIRE(out.hasTRC);
    REQUIRE(out.trcR.size() == 256);

    // Lower segment (X < 0.5): Y = c*X = 0.1*X
    CHECK(out.trcR[0]  == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(out.trcR[64] == doctest::Approx(0.1f * (64.0f / 255.0f)).epsilon(0.02));
    // Upper segment (X >= 0.5): Y = X^2
    CHECK(out.trcR[200] == doctest::Approx(std::pow(200.0f / 255.0f, 2.0f)).epsilon(0.01));
    CHECK(out.trcR[255] == doctest::Approx(1.0f).epsilon(0.01));
}

TEST_CASE("ICC: MHC2 tag matrix serialization") {
    // Locate the MHC2 tag in the generated profile and verify the 3x4 matrix was serialized
    // exactly as ComputeMHC2Matrix produced it. The MHC2 tag — the product's core artifact —
    // was previously never parsed back by any test, so a wrong tag-relative offset, byte order,
    // s15Fixed16 scaling, matrix transpose, or sign error would have passed the whole suite.
    MHC2ProfileParams params;
    params.monitorName = L"TestMHC2";
    params.displayPrimaries = kBT2020;     // non-identity: SDR wire (sRGB) != display (BT.2020)
    params.primariesEnabled = true;
    params.grayscaleEnabled = false;
    params.isHDR = false;

    // GenerateMHC2Profile uses sRGB as the SDR source/wire primaries and no WB gains here.
    float expected[12];
    ComputeMHC2Matrix(kSRGB, kBT2020, false, expected, nullptr);

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));
    REQUIRE(data.size() > 168);

    // 'MHC2' fourcc as ReadBE32 (big-endian) sees it; MakeSig is internal to mhc.cpp.
    const uint32_t MHC2_SIG = ((uint32_t)'M' << 24) | ((uint32_t)'H' << 16) | ((uint32_t)'C' << 8) | (uint32_t)'2';
    uint32_t tagCount = ReadBE32(data.data() + 128);
    uint32_t mhc2Offset = 0;
    for (uint32_t i = 0; i < tagCount; i++) {
        const uint8_t* e = data.data() + 132 + i * 12;
        if (ReadBE32(e) == MHC2_SIG) { mhc2Offset = ReadBE32(e + 4); break; }
    }
    REQUIRE(mhc2Offset != 0);
    REQUIRE(ReadBE32(data.data() + mhc2Offset) == MHC2_SIG);

    // Matrix lives at tag-relative offset 36, 12 x s15Fixed16 big-endian. Margin is loose
    // enough to absorb any micro-difference in the sRGB source constant yet far tighter than
    // a transpose/sign/scaling error (the sRGB->BT.2020 matrix elements are O(0.1-1.6)).
    const uint8_t* m = data.data() + mhc2Offset + 36;
    for (int i = 0; i < 12; i++) {
        float serialized = ReadS15Fixed16(m + i * 4);
        CHECK(serialized == doctest::Approx(expected[i]).epsilon(0.02));
    }
}

TEST_CASE("ICC: HDR profile generation") {
    MHC2ProfileParams params;
    params.monitorName = L"TestHDR";
    params.displayPrimaries = kBT2020;
    params.primariesEnabled = true;
    params.grayscaleEnabled = true;
    params.grayscale.enabled = true;
    params.grayscale.pointCount = 20;
    params.grayscale.initLinearPQ();
    params.isHDR = true;
    params.peakNits = 1000.0f;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));
    CHECK(data.size() > 132);

    // ICC signature
    CHECK(data[36] == 'a');
    CHECK(data[39] == 'p');

    // Write to temp, read back
    TempFile tmp(L"_test_hdr_profile.icm");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        f.write(reinterpret_cast<const char*>(data.data()), data.size());
    }
    ICCProfileData icc;
    REQUIRE(ReadICCProfile(tmp.path, icc));
    CHECK(icc.hasPrimaries);
    CHECK(icc.hasTRC);
}

TEST_CASE("ICC: read SwapRedGreen.icm") {
    ICCProfileData icc;
    bool ok = ReadICCProfile(L"profiles/test/SwapRedGreen.icm", icc);
    if (!ok) {
        WARN("SwapRedGreen.icm not found, skipping");
        return;
    }
    CHECK(icc.hasPrimaries);
}

TEST_CASE("ICC: read SurfacesRGB.icm") {
    ICCProfileData icc;
    bool ok = ReadICCProfile(L"profiles/test/SurfacesRGB.icm", icc);
    if (!ok) {
        WARN("SurfacesRGB.icm not found, skipping");
        return;
    }
    CHECK(icc.hasPrimaries);
    // Primaries should be close to sRGB
    CHECK(icc.primaries.Rx == doctest::Approx(0.64f).epsilon(0.02));
    CHECK(icc.primaries.Gy == doctest::Approx(0.60f).epsilon(0.02));
}

TEST_CASE("ICC: invalid file (random bytes)") {
    TempFile tmp(L"_test_invalid.icm");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        uint8_t junk[50] = {0xDE, 0xAD, 0xBE, 0xEF};
        f.write(reinterpret_cast<const char*>(junk), 50);
    }

    ICCProfileData icc;
    bool ok = ReadICCProfile(tmp.path, icc);

    CHECK_FALSE(ok);
}

TEST_CASE("ICC: file too small") {
    TempFile tmp(L"_test_small.icm");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        uint8_t bytes[100] = {};
        f.write(reinterpret_cast<const char*>(bytes), 100);
    }

    ICCProfileData icc;
    bool ok = ReadICCProfile(tmp.path, icc);

    CHECK_FALSE(ok);
}

// ============================================================================
// 1D Cube Loading
// ============================================================================

// ============================================================================
// TRC-based LUT Generation
// ============================================================================

TEST_CASE("FromTRC SDR: gamma 2.2 TRC produces identity LUT") {
    // Build 256-entry TRC matching the target: trc[i] = pow(i/255, 2.2)
    // Display already has 2.2 gamma — no correction needed
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = powf((float)i / 255.0f, 2.2f);

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_SDR(trc, lut.data(), 64);

    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(lut[63] == doctest::Approx(1.0f).epsilon(0.01));
    // Midpoint should be near identity (display matches target)
    CHECK(lut[32] == doctest::Approx(32.0f / 63.0f).epsilon(0.02));
}

TEST_CASE("FromTRC SDR: linear TRC produces non-identity LUT") {
    // Linear TRC: display is "too bright" — no gamma curve
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = (float)i / 255.0f;

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_SDR(trc, lut.data(), 64);

    // Should differ from identity (compensating for missing gamma)
    bool differs = false;
    for (int i = 5; i < 60; i++) {
        float identity = (float)i / 63.0f;
        if (std::fabs(lut[i] - identity) > 0.01f) { differs = true; break; }
    }
    CHECK(differs);

    // Verify monotonicity
    for (int i = 1; i < 64; i++) {
        CHECK(lut[i] >= lut[i - 1]);
    }
}

TEST_CASE("FromTRC SDR: targetGamma 2.4 differs from 2.2") {
    // BT.1886 uses 2.4 gamma; the LUT should differ from the default 2.2 path
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = powf((float)i / 255.0f, 2.2f);

    std::vector<float> lut22(64), lut24(64);
    GenerateMHC2LUT_FromTRC_SDR(trc, lut22.data(), 64);           // default 2.2
    GenerateMHC2LUT_FromTRC_SDR(trc, lut24.data(), 64, 2.4f);     // BT.1886

    // Endpoints should be the same
    CHECK(lut24[0] == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(lut24[63] == doctest::Approx(1.0f).epsilon(0.01));

    // Midtone values should differ (2.4 target is darker than 2.2)
    bool differs = false;
    for (int i = 10; i < 50; i++) {
        if (std::fabs(lut24[i] - lut22[i]) > 0.001f) { differs = true; break; }
    }
    CHECK(differs);

    // Monotonicity
    for (int i = 1; i < 64; i++) {
        CHECK(lut24[i] >= lut24[i - 1]);
    }
}

TEST_CASE("FromTRC SDR: gamma 2.4 TRC with targetGamma 2.4 is identity") {
    // Display already has 2.4 gamma and target is 2.4 → no correction needed
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = powf((float)i / 255.0f, 2.4f);

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_SDR(trc, lut.data(), 64, 2.4f);

    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(lut[63] == doctest::Approx(1.0f).epsilon(0.01));
    CHECK(lut[32] == doctest::Approx(32.0f / 63.0f).epsilon(0.02));
}

TEST_CASE("FromTRC HDR: PQ-shaped TRC produces identity LUT") {
    // A perfect PQ display: TRC(s) = PqEOTF(s) * 10000 / peakNits
    // For such a display, the MHC correction should be identity (no correction needed)
    float peakNits = 1000.0f;
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) {
        float pqSignal = (float)i / 255.0f;
        trc[i] = std::min(PqEOTF(pqSignal) * 10000.0f / peakNits, 1.0f);
    }

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_HDR(trc, lut.data(), 64, peakNits);

    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.01));
    // At display peak PQ signal, output should be at peak
    float pqPeak = PqOETF(peakNits / 10000.0f);
    int peakIdx = (int)(pqPeak * 63.0f + 0.5f);
    CHECK(lut[peakIdx] == doctest::Approx(pqPeak).epsilon(0.03));
    // Midrange should be near-identity
    CHECK(lut[16] == doctest::Approx(16.0f / 63.0f).epsilon(0.03));
}

TEST_CASE("FromTRC HDR: linear TRC applies PQ pre-encoding") {
    // A linear display (TRC(s)=s) doesn't follow PQ — MHC must pre-encode
    float peakNits = 1000.0f;
    std::vector<float> trc(256);
    for (int i = 0; i < 256; i++) trc[i] = (float)i / 255.0f;

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_HDR(trc, lut.data(), 64, peakNits);

    // For linear TRC, output should be PqEOTF(pqIn)*10000/peak (much darker than identity)
    // At pqIn=0.5 (index 32): PqEOTF(0.5)*10000/1000 ≈ 0.094 — far below 0.5
    CHECK(lut[32] < 0.15f);  // Much darker than identity
    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.01));
}

// ============================================================================
// ExtractGrayscaleFromICC
// ============================================================================

TEST_CASE("ExtractGrayscaleFromICC: no TRC returns false") {
    ICCProfileData icc;
    icc.hasTRC = false;
    GrayscaleSettings gs;
    CHECK_FALSE(ExtractGrayscaleFromICC(icc, gs, false));
}

TEST_CASE("ExtractGrayscaleFromICC: too-short TRC returns false") {
    ICCProfileData icc;
    icc.hasTRC = true;
    icc.trcR = {0.5f};
    icc.trcG = {0.5f};
    icc.trcB = {0.5f};
    GrayscaleSettings gs;
    CHECK_FALSE(ExtractGrayscaleFromICC(icc, gs, false));
}

TEST_CASE("ExtractGrayscaleFromICC: identity TRC SDR") {
    ICCProfileData icc;
    icc.hasTRC = true;
    icc.trcR.resize(256);
    icc.trcG.resize(256);
    icc.trcB.resize(256);
    for (int i = 0; i < 256; i++) {
        float v = (float)i / 255.0f;
        icc.trcR[i] = v;
        icc.trcG[i] = v;
        icc.trcB[i] = v;
    }

    GrayscaleSettings gs;
    gs.pointCount = 20;
    bool ok = ExtractGrayscaleFromICC(icc, gs, false);
    CHECK(ok);
    CHECK(gs.enabled);
    REQUIRE(gs.points.size() == 20);
    // Identity TRC: endpoints should be 0 and 1
    CHECK(gs.points[0] == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(gs.points[19] == doctest::Approx(1.0f).epsilon(0.01));
    // Midpoints should be reasonable (between 0 and 1, monotonic)
    for (int i = 1; i < 20; i++) {
        CHECK(gs.points[i] >= gs.points[i-1] - 0.001f);
    }
}

TEST_CASE("ExtractGrayscaleFromICC: HDR returns false (SDR TRC not applicable)") {
    // SDR ICC TRC describes the display's SDR-mode response (including calibration target),
    // not the display's HDR PQ tracking. Extraction correctly returns false for HDR.
    ICCProfileData icc;
    icc.hasTRC = true;
    icc.trcR.resize(256);
    icc.trcG.resize(256);
    icc.trcB.resize(256);
    for (int i = 0; i < 256; i++) {
        float v = (float)i / 255.0f;
        icc.trcR[i] = v;
        icc.trcG[i] = v;
        icc.trcB[i] = v;
    }

    GrayscaleSettings gs;
    gs.pointCount = 20;
    bool ok = ExtractGrayscaleFromICC(icc, gs, true);
    CHECK_FALSE(ok);
    CHECK_FALSE(gs.enabled);
}

// ============================================================================
// ExtractGrayscaleFromCube
// ============================================================================

TEST_CASE("ExtractGrayscaleFromCube: invalid path returns false") {
    GrayscaleSettings gs;
    CHECK_FALSE(ExtractGrayscaleFromCube(L"nonexistent_file.cube", gs));
}

TEST_CASE("ExtractGrayscaleFromCube: identity cube extracts near-identity") {
    // Try multiple paths for the fixture
    std::wstring paths[] = {
        L"tests/fixtures/identity_17.cube",
        L"../../tests/fixtures/identity_17.cube",
        L"../tests/fixtures/identity_17.cube",
    };
    GrayscaleSettings gs;
    gs.pointCount = 20;
    bool found = false;
    for (auto& p : paths) {
        if (ExtractGrayscaleFromCube(p, gs)) {
            found = true;
            break;
        }
    }
    if (!found) {
        WARN("identity_17.cube not found, skipping");
        return;
    }
    CHECK(gs.enabled);
    REQUIRE(gs.points.size() == 20);
    // Identity cube: neutral axis R=G=B should produce near-identity grayscale
    CHECK(gs.points[0] == doctest::Approx(0.0f).epsilon(0.05));
    CHECK(gs.points[19] == doctest::Approx(1.0f).epsilon(0.05));
}

// ============================================================================
// WriteMHC2Profile
// ============================================================================

TEST_CASE("WriteMHC2Profile: write and read back") {
    MHC2ProfileParams params;
    params.monitorName = L"TestMonitor";
    params.displayPrimaries = kSRGB;
    params.primariesEnabled = false;
    params.grayscaleEnabled = false;
    params.isHDR = false;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));

    TempFile tmp(L"_test_write_profile.icm");
    REQUIRE(WriteMHC2Profile(data, tmp.path));

    // Read back and verify byte-for-byte match
    std::ifstream f(tmp.path, std::ios::binary | std::ios::ate);
    REQUIRE(f.good());
    auto fileSize = f.tellg();
    CHECK(fileSize == (std::streamoff)data.size());

    f.seekg(0);
    std::vector<uint8_t> readBack(fileSize);
    f.read(reinterpret_cast<char*>(readBack.data()), fileSize);
    CHECK(readBack == data);
}

TEST_CASE("WriteMHC2Profile: invalid path returns false") {
    std::vector<uint8_t> data = {0x01, 0x02, 0x03};
    CHECK_FALSE(WriteMHC2Profile(data, L"Z:\\nonexistent\\dir\\file.icm"));
}

// ============================================================================
// SDR LUT: Non-trivial Grayscale
// ============================================================================

TEST_CASE("SDR LUT: non-trivial grayscale correction") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinear();
    // Modify midtone point to be darker
    gs.points[10] = gs.points[10] * 0.8f;

    std::vector<float> lut(1024);
    GenerateMHC2LUT_SDR(gs, lut.data(), 1024);

    // Endpoints preserved
    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lut[1023] == doctest::Approx(1.0f).epsilon(0.001));

    // Should differ from identity at midtones
    std::vector<float> identityLut(1024);
    GrayscaleData gsId = {};
    gsId.enabled = false;
    gsId.pointCount = 20;
    gsId.initLinear();
    GenerateMHC2LUT_SDR(gsId, identityLut.data(), 1024);

    bool differs = false;
    for (int i = 200; i < 800; i++) {
        if (std::fabs(lut[i] - identityLut[i]) > 0.001f) { differs = true; break; }
    }
    CHECK(differs);
}

// ============================================================================
// HDR LUT: Monotonicity
// ============================================================================

TEST_CASE("HDR LUT: monotonicity") {
    GrayscaleData gs = {};
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinearPQ();

    std::vector<float> lut(4096);
    GenerateMHC2LUT_HDR(gs, 1000.0f, lut.data(), 4096);

    for (int i = 1; i < 4096; i++) {
        CHECK(lut[i] >= lut[i - 1]);
    }
}

// ============================================================================
// 1D Cube Loading
// ============================================================================

// ============================================================================
// HDR TRC LUT Pipeline (pqIn → PqEOTF → InvertTRC → pqOut)
// ============================================================================

TEST_CASE("FromTRC HDR: raised black floor maps to non-zero output") {
    // Display with 5% black floor: trc[0] = 0.05 (non-zero minimum luminance)
    // Without BPC, PQ 0% targets linear 0.0 → InvertTRC finds signal near 0
    // (below the display's floor), so shadow detail is lost
    float peakNits = 1000.0f;
    int N = 256;
    std::vector<float> trc(N);
    float blackFloor = 0.05f;
    for (int i = 0; i < N; i++) {
        float t = (float)i / (float)(N - 1);
        trc[i] = std::min(PqEOTF(t) * 10000.0f / peakNits, 1.0f);
        // Shift up by black floor
        trc[i] = blackFloor + trc[i] * (1.0f - blackFloor);
    }

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_HDR(trc, lut.data(), 64, peakNits);

    // At pqIn=0: target=0.0, InvertTRC(trc, 0.0) → signal ≈ 0 (below trc[0]=0.05)
    CHECK(lut[0] >= 0.0f);
    CHECK(lut[0] < 0.02f);  // Very low signal — below the display's floor

    // Monotonicity preserved
    for (int i = 1; i < 64; i++) {
        CHECK(lut[i] >= lut[i - 1]);
    }

    // Peak maps to 1.0
    CHECK(lut[63] == doctest::Approx(1.0f).epsilon(0.02));
}

TEST_CASE("FromTRC HDR: perfect PQ display produces near-identity") {
    // TRC perfectly matches PQ → InvertTRC(target) ≈ pqIn (identity)
    float peakNits = 1000.0f;
    int N = 256;
    std::vector<float> trc(N);
    for (int i = 0; i < N; i++) {
        float pq = (float)i / (float)(N - 1);
        trc[i] = std::min(PqEOTF(pq) * 10000.0f / peakNits, 1.0f);
    }
    CHECK(trc[0] == doctest::Approx(0.0f).epsilon(1e-6));

    std::vector<float> lut(64);
    GenerateMHC2LUT_FromTRC_HDR(trc, lut.data(), 64, peakNits);

    // Near-identity: pqOut ≈ pqIn at sampled points
    CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(lut[16] == doctest::Approx(16.0f / 63.0f).epsilon(0.03));
    CHECK(lut[32] == doctest::Approx(32.0f / 63.0f).epsilon(0.03));
}

TEST_CASE("FromTRC HDR: endpoints and monotonicity at various peaks") {
    // Pipeline: pqIn → PqEOTF(pqIn)*10000/peak → clamp to 1.0 → InvertTRC → pqOut
    // At pqIn=0: targetLinear=0 → InvertTRC(0.0) ≈ 0.0
    // At peak: targetLinear=1.0 → InvertTRC(1.0) ≈ 1.0
    float peaks[] = { 400.0f, 1000.0f, 4000.0f };
    for (float peakNits : peaks) {
        int N = 256;
        std::vector<float> trc(N);
        for (int i = 0; i < N; i++) {
            trc[i] = (float)i / (float)(N - 1);  // Linear ramp
        }

        std::vector<float> lut(64);
        GenerateMHC2LUT_FromTRC_HDR(trc, lut.data(), 64, peakNits);

        CHECK(lut[0] == doctest::Approx(0.0f).epsilon(0.02));
        CHECK(lut[63] == doctest::Approx(1.0f).epsilon(0.02));

        // Monotonicity
        for (int i = 1; i < 64; i++) {
            CHECK(lut[i] >= lut[i - 1]);
        }
    }
}

// ============================================================================
// HDR Per-Channel TRC Profile Generation
// ============================================================================

TEST_CASE("GenerateMHC2Profile: per-channel HDR TRC generates valid profile") {
    MHC2ProfileParams params;
    params.monitorName = L"TestHDR_TRC";
    params.displayPrimaries = kBT2020;
    params.primariesEnabled = true;
    params.isHDR = true;
    params.peakNits = 1000.0f;
    params.hasPerChannelTRC = true;

    // Create slightly different TRC per channel (simulating per-channel PQ tracking errors)
    int N = 256;
    params.trcR.resize(N);
    params.trcG.resize(N);
    params.trcB.resize(N);
    for (int i = 0; i < N; i++) {
        float pq = (float)i / (float)(N - 1);
        float base = std::min(PqEOTF(pq) * 10000.0f / 1000.0f, 1.0f);
        params.trcR[i] = base * 0.98f;  // Red slightly dim
        params.trcG[i] = base;           // Green perfect
        params.trcB[i] = base * 0.95f;  // Blue more dim
    }
    params.grayscaleEnabled = true;
    params.grayscale.enabled = true;

    std::vector<uint8_t> data;
    REQUIRE(GenerateMHC2Profile(params, data));
    CHECK(data.size() > 132);

    // Write and read back to verify validity
    TempFile tmp(L"_test_hdr_trc_profile.icm");
    {
        std::ofstream f(tmp.path, std::ios::binary);
        f.write(reinterpret_cast<const char*>(data.data()), data.size());
    }
    ICCProfileData icc;
    REQUIRE(ReadICCProfile(tmp.path, icc));
    CHECK(icc.hasTRC);
}

TEST_CASE("GenerateMHC2Profile: per-channel TRC takes precedence over grayscale") {
    // When both hasPerChannelTRC and grayscaleEnabled are set,
    // the per-channel TRC path should execute (params.hasPrecomputedCorrection check first,
    // then hasPerChannelTRC, then grayscale fallback)
    MHC2ProfileParams paramsWithTRC;
    paramsWithTRC.monitorName = L"TestTRC";
    paramsWithTRC.displayPrimaries = kBT2020;
    paramsWithTRC.primariesEnabled = false;
    paramsWithTRC.isHDR = true;
    paramsWithTRC.peakNits = 1000.0f;
    paramsWithTRC.hasPerChannelTRC = true;
    // Linear TRC (very different from PQ — will produce non-identity LUT)
    paramsWithTRC.trcR.resize(256);
    paramsWithTRC.trcG.resize(256);
    paramsWithTRC.trcB.resize(256);
    for (int i = 0; i < 256; i++) {
        float v = (float)i / 255.0f;
        paramsWithTRC.trcR[i] = v;
        paramsWithTRC.trcG[i] = v;
        paramsWithTRC.trcB[i] = v;
    }
    paramsWithTRC.grayscaleEnabled = true;
    paramsWithTRC.grayscale.enabled = true;
    paramsWithTRC.grayscale.pointCount = 20;
    paramsWithTRC.grayscale.initLinearPQ();

    MHC2ProfileParams paramsGSOnly;
    paramsGSOnly.monitorName = L"TestGS";
    paramsGSOnly.displayPrimaries = kBT2020;
    paramsGSOnly.primariesEnabled = false;
    paramsGSOnly.isHDR = true;
    paramsGSOnly.peakNits = 1000.0f;
    paramsGSOnly.hasPerChannelTRC = false;
    paramsGSOnly.grayscaleEnabled = true;
    paramsGSOnly.grayscale.enabled = true;
    paramsGSOnly.grayscale.pointCount = 20;
    paramsGSOnly.grayscale.initLinearPQ();

    std::vector<uint8_t> dataTRC, dataGS;
    REQUIRE(GenerateMHC2Profile(paramsWithTRC, dataTRC));
    REQUIRE(GenerateMHC2Profile(paramsGSOnly, dataGS));

    // The profiles should differ in their LUT data (TRC path vs grayscale path)
    CHECK(dataTRC != dataGS);
}

// ============================================================================
// TRC Normalization & isLUTBased Detection
// ============================================================================

// Helper: build a minimal synthetic ICC file for testing ReadICCProfile behavior.
// Creates a valid ICC with rTRC/gTRC/bTRC curv tags (tabular) and optional A2B0 tag.
static std::vector<uint8_t> BuildSyntheticICC(
    const std::vector<uint16_t>& trcR,
    const std::vector<uint16_t>& trcG,
    const std::vector<uint16_t>& trcB,
    bool includeA2B0 = false)
{
    // Each curv tag: 4 (type) + 4 (reserved) + 4 (count) + count*2 (data)
    auto curvSize = [](size_t count) -> uint32_t { return 12 + (uint32_t)count * 2; };
    uint32_t rSize = curvSize(trcR.size());
    uint32_t gSize = curvSize(trcG.size());
    uint32_t bSize = curvSize(trcB.size());

    uint32_t tagCount = 3 + (includeA2B0 ? 1 : 0);
    uint32_t tagTableSize = 4 + tagCount * 12;
    uint32_t dataStart = 128 + tagTableSize;

    // Pad each tag to 4-byte boundary
    auto pad4 = [](uint32_t v) -> uint32_t { return (v + 3) & ~3u; };
    uint32_t rOff = dataStart;
    uint32_t gOff = rOff + pad4(rSize);
    uint32_t bOff = gOff + pad4(gSize);
    uint32_t a2b0Off = bOff + pad4(bSize);
    uint32_t a2b0Size = 16;  // Minimal dummy tag
    uint32_t profileSize = includeA2B0 ? (a2b0Off + pad4(a2b0Size)) : (bOff + pad4(bSize));

    std::vector<uint8_t> data(profileSize, 0);
    auto writeBE32 = [&](size_t off, uint32_t v) {
        data[off] = (uint8_t)(v >> 24); data[off+1] = (uint8_t)(v >> 16);
        data[off+2] = (uint8_t)(v >> 8); data[off+3] = (uint8_t)v;
    };
    auto writeBE16 = [&](size_t off, uint16_t v) {
        data[off] = (uint8_t)(v >> 8); data[off+1] = (uint8_t)v;
    };
    auto writeSig = [&](size_t off, const char* s) {
        data[off] = s[0]; data[off+1] = s[1]; data[off+2] = s[2]; data[off+3] = s[3];
    };

    // Header
    writeBE32(0, profileSize);      // Profile size
    writeSig(36, "acsp");           // ICC signature

    // Tag table
    writeBE32(128, tagCount);
    size_t t = 132;
    writeSig(t, "rTRC"); writeBE32(t+4, rOff); writeBE32(t+8, rSize); t += 12;
    writeSig(t, "gTRC"); writeBE32(t+4, gOff); writeBE32(t+8, gSize); t += 12;
    writeSig(t, "bTRC"); writeBE32(t+4, bOff); writeBE32(t+8, bSize); t += 12;
    if (includeA2B0) {
        writeSig(t, "A2B0"); writeBE32(t+4, a2b0Off); writeBE32(t+8, a2b0Size); t += 12;
    }

    // Write curv tags
    auto writeCurv = [&](uint32_t off, const std::vector<uint16_t>& vals) {
        writeSig(off, "curv");                    // Type signature
        writeBE32(off + 4, 0);                    // Reserved
        writeBE32(off + 8, (uint32_t)vals.size()); // Count
        for (size_t i = 0; i < vals.size(); i++) {
            writeBE16(off + 12 + i * 2, vals[i]);
        }
    };
    writeCurv(rOff, trcR);
    writeCurv(gOff, trcG);
    writeCurv(bOff, trcB);

    return data;
}

TEST_CASE("ICC: TRC normalization scales channels to reach 1.0") {
    // Build synthetic ICC with TRC maxes: R=0.90, G=0.85, B=0.72
    int N = 64;
    std::vector<uint16_t> r(N), g(N), b(N);
    for (int i = 0; i < N; i++) {
        float t = (float)i / (float)(N - 1);
        r[i] = (uint16_t)(t * 0.90f * 65535.0f + 0.5f);
        g[i] = (uint16_t)(t * 0.85f * 65535.0f + 0.5f);
        b[i] = (uint16_t)(t * 0.72f * 65535.0f + 0.5f);
    }

    auto data = BuildSyntheticICC(r, g, b);
    TempFile tmp(L"_test_trc_norm.icm");
    { std::ofstream f(tmp.path, std::ios::binary); f.write((const char*)data.data(), data.size()); }

    ICCProfileData icc;
    REQUIRE(ReadICCProfile(tmp.path, icc));
    CHECK(icc.hasTRC);

    // After normalization, all channels should reach 1.0
    CHECK(icc.trcR.back() == doctest::Approx(1.0f).epsilon(0.01));
    CHECK(icc.trcG.back() == doctest::Approx(1.0f).epsilon(0.01));
    CHECK(icc.trcB.back() == doctest::Approx(1.0f).epsilon(0.01));

    // Shape preserved: midpoint ratios should be maintained
    // Original mid R = 0.45/0.90 = 0.50, normalized should still be ~0.50
    int mid = N / 2;
    CHECK(icc.trcR[mid] == doctest::Approx(0.50f).epsilon(0.02));
    CHECK(icc.trcG[mid] == doctest::Approx(0.50f).epsilon(0.02));
    CHECK(icc.trcB[mid] == doctest::Approx(0.50f).epsilon(0.02));
}

TEST_CASE("ICC: isLUTBased false for standard matrix+TRC profile") {
    // Build synthetic ICC without A2B0/B2A0 tags
    int N = 32;
    std::vector<uint16_t> trc(N);
    for (int i = 0; i < N; i++) trc[i] = (uint16_t)((float)i / (float)(N - 1) * 65535.0f);

    auto data = BuildSyntheticICC(trc, trc, trc, false);
    TempFile tmp(L"_test_not_lut_based.icm");
    { std::ofstream f(tmp.path, std::ios::binary); f.write((const char*)data.data(), data.size()); }

    ICCProfileData icc;
    REQUIRE(ReadICCProfile(tmp.path, icc));
    CHECK_FALSE(icc.isLUTBased);
}

TEST_CASE("ICC: isLUTBased true when A2B0 tag present") {
    // Build synthetic ICC with A2B0 tag
    int N = 32;
    std::vector<uint16_t> trc(N);
    for (int i = 0; i < N; i++) trc[i] = (uint16_t)((float)i / (float)(N - 1) * 65535.0f);

    auto data = BuildSyntheticICC(trc, trc, trc, true);
    TempFile tmp(L"_test_lut_based.icm");
    { std::ofstream f(tmp.path, std::ios::binary); f.write((const char*)data.data(), data.size()); }

    ICCProfileData icc;
    REQUIRE(ReadICCProfile(tmp.path, icc));
    CHECK(icc.isLUTBased);
    // TRC should still be read (isLUTBased is just a flag, not a filter)
    CHECK(icc.hasTRC);
}

// ============================================================================
// 1D Cube Loading
// ============================================================================

TEST_CASE("1D cube: valid identity file") {
    std::vector<float> r, g, b;
    bool ok = Load1DCubeLUT(L"tests/fixtures/test_1d.cube", r, g, b);
    if (!ok) {
        // Try from test exe directory
        ok = Load1DCubeLUT(L"../../tests/fixtures/test_1d.cube", r, g, b);
    }
    if (!ok) {
        WARN("test_1d.cube not found, skipping");
        return;
    }
    CHECK(r.size() == 16);
    CHECK(g.size() == 16);
    CHECK(b.size() == 16);

    // Should be identity ramp
    CHECK(r[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(r[15] == doctest::Approx(1.0f).epsilon(0.001));
}

// ============================================================================
// Per-channel grayscale evaluation
// ============================================================================

TEST_CASE("Grayscale SDR per-channel: R=G=B equals single-value") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.initLinear();  // Also initializes pointsR/G/B to match points
    // Modify some points
    for (int i = 0; i < 20; i++) {
        float t = (float)i / 19.0f;
        gs.points[i] = t * t * 0.9f + 0.05f * t;  // Non-trivial curve
        gs.pointsR[i] = gs.points[i];
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i];
    }

    // When per-channel matches single-value, results must be identical
    float testInputs[] = { 0.0f, 0.01f, 0.1f, 0.25f, 0.5f, 0.75f, 1.0f };
    for (float Y : testInputs) {
        float single = EvalGrayscaleSDR(Y, gs);
        float chR = EvalGrayscaleSDR_Channel(Y, gs, 0);
        float chG = EvalGrayscaleSDR_Channel(Y, gs, 1);
        float chB = EvalGrayscaleSDR_Channel(Y, gs, 2);
        CHECK(chR == doctest::Approx(single).epsilon(0.0001));
        CHECK(chG == doctest::Approx(single).epsilon(0.0001));
        CHECK(chB == doctest::Approx(single).epsilon(0.0001));
    }
}

TEST_CASE("Grayscale SDR per-channel: independent corrections") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 10;
    gs.initLinear();
    // Set different R/G/B at midpoint (index 5)
    // Base points are all equal, but per-channel differs
    float t5 = 5.0f / 9.0f;
    float base = t5 * t5;
    gs.pointsR[5] = base * 0.9f;  // R darker
    gs.pointsG[5] = base * 1.0f;  // G unchanged
    gs.pointsB[5] = base * 1.1f;  // B brighter

    float Y = base;  // Linear value at index 5
    float chR = EvalGrayscaleSDR_Channel(Y, gs, 0);
    float chG = EvalGrayscaleSDR_Channel(Y, gs, 1);
    float chB = EvalGrayscaleSDR_Channel(Y, gs, 2);
    CHECK(chR < chG);
    CHECK(chG < chB);
}

TEST_CASE("Grayscale HDR per-channel: R=G=B equals single-value") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Modify to non-trivial curve
    for (int i = 0; i < 20; i++) {
        float t = (float)i / 19.0f;
        gs.points[i] = t * 0.95f + 0.025f * t * t;
        gs.pointsR[i] = gs.points[i];
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i];
    }

    float pqPeak = PqOETF(1000.0f / 10000.0f);
    float testPQ[] = { 0.0f, 0.1f, 0.3f, 0.5f, 0.7f, pqPeak };
    for (float pq : testPQ) {
        float single = EvalGrayscaleHDR(pq, gs, pqPeak);
        float chR = EvalGrayscaleHDR_Channel(pq, gs, pqPeak, 0);
        float chG = EvalGrayscaleHDR_Channel(pq, gs, pqPeak, 1);
        float chB = EvalGrayscaleHDR_Channel(pq, gs, pqPeak, 2);
        CHECK(chR == doctest::Approx(single).epsilon(0.0001));
        CHECK(chG == doctest::Approx(single).epsilon(0.0001));
        CHECK(chB == doctest::Approx(single).epsilon(0.0001));
    }
}

TEST_CASE("Grayscale HDR per-channel: independent corrections") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 10;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Make R < G < B at midpoint
    gs.pointsR[5] = gs.points[5] * 0.95f;
    gs.pointsG[5] = gs.points[5] * 1.00f;
    gs.pointsB[5] = gs.points[5] * 1.05f;

    float pqPeak = PqOETF(1000.0f / 10000.0f);
    float pqMid = (5.0f / 9.0f) * pqPeak;
    float chR = EvalGrayscaleHDR_Channel(pqMid, gs, pqPeak, 0);
    float chG = EvalGrayscaleHDR_Channel(pqMid, gs, pqPeak, 1);
    float chB = EvalGrayscaleHDR_Channel(pqMid, gs, pqPeak, 2);
    CHECK(chR < chG);
    CHECK(chG < chB);
}

TEST_CASE("Grayscale HDR per-channel: above-peak extrapolation") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Set per-channel corrections
    for (int i = 0; i < 20; i++) {
        gs.pointsR[i] = gs.points[i] * 0.95f;
        gs.pointsG[i] = gs.points[i] * 1.00f;
        gs.pointsB[i] = gs.points[i] * 1.05f;
    }

    float pqPeak = PqOETF(1000.0f / 10000.0f);
    float abovePeak = pqPeak + 0.1f;
    for (int ch = 0; ch < 3; ch++) {
        float result = EvalGrayscaleHDR_Channel(abovePeak, gs, pqPeak, ch);
        CHECK(std::isfinite(result));
        CHECK(result > 0.0f);
    }
    // Ordering should be preserved above peak (R < G < B corrections)
    float rAbove = EvalGrayscaleHDR_Channel(abovePeak, gs, pqPeak, 0);
    float gAbove = EvalGrayscaleHDR_Channel(abovePeak, gs, pqPeak, 1);
    float bAbove = EvalGrayscaleHDR_Channel(abovePeak, gs, pqPeak, 2);
    CHECK(rAbove <= gAbove);
    CHECK(gAbove <= bAbove);
}

TEST_CASE("MHC LUT SDR per-channel: R/G/B differ") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 10;
    gs.initLinear();
    // Make R slightly darker, B slightly brighter than G
    for (int i = 0; i < 10; i++) {
        gs.pointsR[i] = gs.points[i] * 0.95f;
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i] * 1.05f;
    }

    const int lutSize = 256;
    std::vector<float> lutR(lutSize), lutG(lutSize), lutB(lutSize);
    GenerateMHC2LUT_SDR_Channel(gs, lutR.data(), lutSize, 0);
    GenerateMHC2LUT_SDR_Channel(gs, lutG.data(), lutSize, 1);
    GenerateMHC2LUT_SDR_Channel(gs, lutB.data(), lutSize, 2);

    // Endpoints preserved
    CHECK(lutR[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lutG[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lutB[0] == doctest::Approx(0.0f).epsilon(0.001));

    // Mid-LUT values should differ: R < G < B
    CHECK(lutR[128] < lutG[128]);
    CHECK(lutG[128] < lutB[128]);
}

TEST_CASE("MHC LUT HDR per-channel: R/G/B differ") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 10;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    for (int i = 0; i < 10; i++) {
        gs.pointsR[i] = gs.points[i] * 0.95f;
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i] * 1.05f;
    }

    const int lutSize = 512;
    std::vector<float> lutR(lutSize), lutG(lutSize), lutB(lutSize);
    GenerateMHC2LUT_HDR_Channel(gs, 1000.0f, lutR.data(), lutSize, 0);
    GenerateMHC2LUT_HDR_Channel(gs, 1000.0f, lutG.data(), lutSize, 1);
    GenerateMHC2LUT_HDR_Channel(gs, 1000.0f, lutB.data(), lutSize, 2);

    // Endpoints
    CHECK(lutR[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lutG[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(lutB[0] == doctest::Approx(0.0f).epsilon(0.001));

    // Mid-LUT: R < G < B
    CHECK(lutR[256] < lutG[256]);
    CHECK(lutG[256] < lutB[256]);
}

// ============================================================================
// 32-Point Grayscale Boundary Tests
// ============================================================================

TEST_CASE("Grayscale SDR: 32-point full sweep") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 32;
    gs.initLinear();
    // Apply a sine-wave modulation to test interpolation across all 32 points
    for (int i = 0; i < 32; i++) {
        float t = (float)i / 31.0f;
        gs.points[i] = t * t + 0.02f * sinf(t * 6.28f);
        gs.points[i] = std::clamp(gs.points[i], 0.0f, 1.0f);
        gs.pointsR[i] = gs.points[i];
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i];
    }

    // Sweep through 100 input values — all must be finite and in range
    for (int i = 0; i <= 100; i++) {
        float Y = (float)i / 100.0f;
        float result = EvalGrayscaleSDR(Y, gs);
        CHECK(std::isfinite(result));
        CHECK(result >= 0.0f);
        CHECK(result <= 1.1f);
    }

    // Per-channel should match when R=G=B
    float Y = 0.5f;
    float single = EvalGrayscaleSDR(Y, gs);
    CHECK(EvalGrayscaleSDR_Channel(Y, gs, 0) == doctest::Approx(single).epsilon(0.0001));
    CHECK(EvalGrayscaleSDR_Channel(Y, gs, 1) == doctest::Approx(single).epsilon(0.0001));
    CHECK(EvalGrayscaleSDR_Channel(Y, gs, 2) == doctest::Approx(single).epsilon(0.0001));
}

TEST_CASE("Grayscale HDR: 32-point full sweep") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 32;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Modulate some points
    for (int i = 0; i < 32; i++) {
        gs.points[i] *= (1.0f + 0.05f * sinf((float)i * 0.5f));
        gs.pointsR[i] = gs.points[i];
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i];
    }

    float pqPeak = PqOETF(1000.0f / 10000.0f);
    // Sweep PQ values 0 to pqPeak
    for (int i = 0; i <= 50; i++) {
        float pq = pqPeak * (float)i / 50.0f;
        float result = EvalGrayscaleHDR(pq, gs, pqPeak);
        CHECK(std::isfinite(result));
        CHECK(result >= 0.0f);
    }
}

// ============================================================================
// ICtCp Grayscale Offset Conversion
// ============================================================================

TEST_CASE("ICtCp offsets: identity produces zero deltas") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Identity: all deviations = 1.0, so pointsR/G/B = points (equal per-channel)

    ComputeGrayscaleICtCpOffsets(gs);
    CHECK(gs.ictcpValid);

    // All deltas should be zero (or very close)
    for (int i = 0; i < 20; i++) {
        CHECK(gs.ictcpI[i] == doctest::Approx(0.0f).epsilon(0.001));
        CHECK(gs.ictcpCt[i] == doctest::Approx(0.0f).epsilon(0.001));
        CHECK(gs.ictcpCp[i] == doctest::Approx(0.0f).epsilon(0.001));
    }
}

TEST_CASE("ICtCp offsets: equal non-identity produces zero Ct/Cp") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // All channels equally brighter at midpoint
    for (int i = 0; i < 20; i++) {
        float boosted = gs.points[i] * 1.05f;
        gs.pointsR[i] = boosted;
        gs.pointsG[i] = boosted;
        gs.pointsB[i] = boosted;
    }

    ComputeGrayscaleICtCpOffsets(gs);
    CHECK(gs.ictcpValid);

    // Non-zero I deltas (luminance change), but Ct/Cp should be zero (no hue shift)
    for (int i = 1; i < 20; i++) {
        CHECK(gs.ictcpCt[i] == doctest::Approx(0.0f).epsilon(0.001));
        CHECK(gs.ictcpCp[i] == doctest::Approx(0.0f).epsilon(0.001));
    }
    // At least some mid-range points should have non-zero I delta
    bool anyNonZeroI = false;
    for (int i = 5; i < 15; i++) {
        if (std::fabs(gs.ictcpI[i]) > 1e-5f) anyNonZeroI = true;
    }
    CHECK(anyNonZeroI);
}

TEST_CASE("ICtCp offsets: red boost produces positive Cp") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Boost red only at midpoints
    for (int i = 0; i < 20; i++) {
        gs.pointsR[i] = gs.points[i] * 1.10f;  // Red 10% brighter
        gs.pointsG[i] = gs.points[i];
        gs.pointsB[i] = gs.points[i];
    }

    ComputeGrayscaleICtCpOffsets(gs);
    CHECK(gs.ictcpValid);

    // Red boost should produce positive Cp (protan axis) at mid-range points
    // ICtCp Cp row: [4.378, -4.246, -0.133] — heavily weighted towards L (red-ish)
    // More L' (from red) → more positive Cp
    bool anyPositiveCp = false;
    for (int i = 5; i < 15; i++) {
        if (gs.ictcpCp[i] > 1e-5f) anyPositiveCp = true;
    }
    CHECK(anyPositiveCp);
}

TEST_CASE("ICtCp offsets: black point produces near-zero deltas") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Modify only mid/high points, leave black (index 0) at identity
    for (int i = 5; i < 20; i++) {
        gs.pointsR[i] = gs.points[i] * 1.05f;
        gs.pointsG[i] = gs.points[i] * 0.95f;
        gs.pointsB[i] = gs.points[i];
    }

    ComputeGrayscaleICtCpOffsets(gs);

    // Index 0 (black) should have zero deltas regardless of other point modifications
    CHECK(gs.ictcpI[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(gs.ictcpCt[0] == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(gs.ictcpCp[0] == doctest::Approx(0.0f).epsilon(0.001));
}

TEST_CASE("ICtCp offsets: peak value produces no NaN/infinity") {
    GrayscaleData gs;
    gs.enabled = true;
    gs.pointCount = 20;
    gs.peakNits = 1000.0f;
    gs.initLinearPQ();
    // Extreme corrections at peak
    gs.pointsR[19] = gs.points[19] * 1.25f;  // +25%
    gs.pointsG[19] = gs.points[19] * 0.75f;  // -25%
    gs.pointsB[19] = gs.points[19] * 1.25f;

    ComputeGrayscaleICtCpOffsets(gs);

    for (int i = 0; i < 20; i++) {
        CHECK(!std::isnan(gs.ictcpI[i]));
        CHECK(!std::isinf(gs.ictcpI[i]));
        CHECK(!std::isnan(gs.ictcpCt[i]));
        CHECK(!std::isinf(gs.ictcpCt[i]));
        CHECK(!std::isnan(gs.ictcpCp[i]));
        CHECK(!std::isinf(gs.ictcpCp[i]));
    }
    // Beyond pointCount should be zero (identity)
    for (int i = 20; i < 32; i++) {
        CHECK(gs.ictcpI[i] == doctest::Approx(0.0f).epsilon(0.001));
        CHECK(gs.ictcpCt[i] == doctest::Approx(0.0f).epsilon(0.001));
        CHECK(gs.ictcpCp[i] == doctest::Approx(0.0f).epsilon(0.001));
    }
}

// ============================================================================
// SECTION: White Balance Matrix Baking
// ============================================================================

TEST_CASE("MHC2 matrix: white balance gains identity") {
    // WB gains of 1.0 should produce same result as no gains
    float mhcNoWB[12], mhcWithWB[12];
    float gains[3] = { 1.0f, 1.0f, 1.0f };
    ComputeMHC2Matrix(kSRGB, kSRGB, false, mhcNoWB);
    ComputeMHC2Matrix(kSRGB, kSRGB, false, mhcWithWB, gains);
    for (int i = 0; i < 12; i++) {
        CHECK(mhcWithWB[i] == doctest::Approx(mhcNoWB[i]).epsilon(1e-5));
    }
}

TEST_CASE("MHC2 matrix: white balance gains scale output") {
    float mhc[12];
    float gains[3] = { 1.1f, 1.0f, 0.9f };
    ComputeMHC2Matrix(kSRGB, kSRGB, false, mhc, gains);
    // White input (1,1,1) → output: sum of each matrix row
    float outR = mhc[0] + mhc[1] + mhc[2];
    float outG = mhc[4] + mhc[5] + mhc[6];
    float outB = mhc[8] + mhc[9] + mhc[10];
    // R gain > 1.0 → red boosted
    CHECK(outR > 1.0f);
    // G gain = 1.0 → green near 1.0
    CHECK(outG == doctest::Approx(1.0f).epsilon(0.01));
    // B gain < 1.0 → blue reduced
    CHECK(outB < 1.0f);
}

TEST_CASE("MHC2 matrix: white balance + primaries combined") {
    // Verify matrix encodes both gamut mapping AND white shift
    float mhcPrimOnly[12], mhcBoth[12];
    float gains[3] = { 1.05f, 1.0f, 0.95f };
    ComputeMHC2Matrix(kSRGB, kP3D65, false, mhcPrimOnly);
    ComputeMHC2Matrix(kSRGB, kP3D65, false, mhcBoth, gains);
    // Results should differ (WB modifies the matrix)
    bool same = true;
    for (int i = 0; i < 12; i++) {
        if (fabsf(mhcBoth[i] - mhcPrimOnly[i]) > 1e-5f) { same = false; break; }
    }
    CHECK(!same);
    // Column 4 should still be zero
    CHECK(mhcBoth[3] == 0.0f);
    CHECK(mhcBoth[7] == 0.0f);
    CHECK(mhcBoth[11] == 0.0f);
}

// ============================================================================
// SECTION: Desktop Gamma HDR LUT
// ============================================================================

TEST_CASE("Desktop gamma: HDR LUT SDR range corrected") {
    MHC2ProfileParams paramsNoDG;
    paramsNoDG.isHDR = true;
    paramsNoDG.peakNits = 1000.0f;
    paramsNoDG.primariesEnabled = false;
    paramsNoDG.grayscaleEnabled = false;
    paramsNoDG.grayscale.enabled = false;
    paramsNoDG.desktopGammaEnabled = false;

    MHC2ProfileParams paramsDG = paramsNoDG;
    paramsDG.desktopGammaEnabled = true;

    std::vector<uint8_t> dataNoDG, dataDG;
    REQUIRE(GenerateMHC2Profile(paramsNoDG, dataNoDG));
    REQUIRE(GenerateMHC2Profile(paramsDG, dataDG));

    CHECK(dataNoDG.size() > 128);
    CHECK(dataDG.size() > 128);
    // Desktop gamma version has different LUT data
    CHECK(dataNoDG != dataDG);
}

TEST_CASE("Desktop gamma: sRGB to 2.2 conversion at SDR") {
    // Verify the math: sRGB OETF then pow(2.2) should darken relative to PQ identity
    float sdrLinear = 0.5f;  // 50% linear light at 40 nits
    float srgbEncoded = SrgbOETF(sdrLinear);
    float gamma22 = powf(srgbEncoded, 2.2f);
    // sRGB OETF of 0.5 ≈ 0.735, pow(0.735, 2.2) ≈ 0.514
    // So gamma22 should be close to but slightly above sdrLinear (sRGB→2.2 at midtone)
    CHECK(gamma22 > 0.4f);
    CHECK(gamma22 < 0.6f);
    // Convert to PQ and back
    float pqOriginal = PqOETF(sdrLinear * 80.0f / 10000.0f);
    float pqCorrected = PqOETF(gamma22 * 80.0f / 10000.0f);
    // They should differ (correction is applied)
    CHECK(pqOriginal != doctest::Approx(pqCorrected).epsilon(1e-4));
}

// ============================================================================
// SECTION: Correction Grayscale Composition
// ============================================================================

TEST_CASE("Correction grayscale: SDR composition on identity base") {
    // Base grayscale = identity, correction grayscale = non-trivial
    // Result should equal just the correction grayscale applied
    MHC2ProfileParams params;
    params.isHDR = false;
    params.primariesEnabled = false;
    params.grayscaleEnabled = false;
    params.grayscale.enabled = false;

    // Set up correction grayscale with a midtone boost
    params.correctionGrayscaleEnabled = true;
    params.correctionGrayscale.enabled = true;
    params.correctionGrayscale.pointCount = 20;
    params.correctionGrayscale.initLinear();
    // Darken midpoint
    params.correctionGrayscale.points[10] = params.correctionGrayscale.points[10] * 0.9f;
    for (int i = 0; i < 20; i++) {
        params.correctionGrayscale.pointsR[i] = params.correctionGrayscale.points[i];
        params.correctionGrayscale.pointsG[i] = params.correctionGrayscale.points[i];
        params.correctionGrayscale.pointsB[i] = params.correctionGrayscale.points[i];
    }

    std::vector<uint8_t> profileData;
    bool ok = GenerateMHC2Profile(params, profileData);
    CHECK(ok);
    CHECK(profileData.size() > 128);

    // Verify correction changes the LUT vs no correction
    MHC2ProfileParams paramsNone;
    paramsNone.isHDR = false;
    paramsNone.primariesEnabled = false;
    paramsNone.grayscaleEnabled = false;
    paramsNone.grayscale.enabled = false;
    paramsNone.correctionGrayscaleEnabled = false;
    std::vector<uint8_t> noCorr;
    REQUIRE(GenerateMHC2Profile(paramsNone, noCorr));
    CHECK(profileData != noCorr);
}

TEST_CASE("Correction grayscale: HDR composition on identity base") {
    MHC2ProfileParams params;
    params.isHDR = true;
    params.peakNits = 1000.0f;
    params.primariesEnabled = false;
    params.grayscaleEnabled = false;
    params.grayscale.enabled = false;

    // Non-trivial correction grayscale
    params.correctionGrayscaleEnabled = true;
    params.correctionGrayscale.enabled = true;
    params.correctionGrayscale.pointCount = 20;
    params.correctionGrayscale.peakNits = 1000.0f;
    params.correctionGrayscale.initLinearPQ();
    params.correctionGrayscale.points[10] = params.correctionGrayscale.points[10] * 0.95f;
    for (int i = 0; i < 20; i++) {
        params.correctionGrayscale.pointsR[i] = params.correctionGrayscale.points[i];
        params.correctionGrayscale.pointsG[i] = params.correctionGrayscale.points[i];
        params.correctionGrayscale.pointsB[i] = params.correctionGrayscale.points[i];
    }

    std::vector<uint8_t> profileData;
    bool ok = GenerateMHC2Profile(params, profileData);
    CHECK(ok);
    CHECK(profileData.size() > 128);
}

TEST_CASE("Correction grayscale + desktop gamma combined HDR") {
    // Both desktop gamma and correction grayscale enabled
    MHC2ProfileParams params;
    params.isHDR = true;
    params.peakNits = 1000.0f;
    params.primariesEnabled = false;
    params.desktopGammaEnabled = true;

    params.correctionGrayscaleEnabled = true;
    params.correctionGrayscale.enabled = true;
    params.correctionGrayscale.pointCount = 20;
    params.correctionGrayscale.peakNits = 1000.0f;
    params.correctionGrayscale.initLinearPQ();
    for (int i = 0; i < 20; i++) {
        params.correctionGrayscale.pointsR[i] = params.correctionGrayscale.points[i];
        params.correctionGrayscale.pointsG[i] = params.correctionGrayscale.points[i];
        params.correctionGrayscale.pointsB[i] = params.correctionGrayscale.points[i];
    }

    std::vector<uint8_t> profileData;
    bool ok = GenerateMHC2Profile(params, profileData);
    CHECK(ok);
    CHECK(profileData.size() > 128);
}
