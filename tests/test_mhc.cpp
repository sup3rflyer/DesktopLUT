#include "doctest.h"
#include "mhc.h"
#include "color.h"
#include <cmath>
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

TEST_CASE("ExtractGrayscaleFromICC: identity TRC HDR") {
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
    CHECK(ok);
    CHECK(gs.enabled);
    REQUIRE(gs.points.size() == 20);
    // Identity TRC: endpoints should be 0 and 1
    CHECK(gs.points[0] == doctest::Approx(0.0f).epsilon(0.01));
    CHECK(gs.points[19] == doctest::Approx(1.0f).epsilon(0.01));
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
