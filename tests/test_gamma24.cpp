// DesktopLUT Tests - SDR GAMMA 2.4 (MhcGamma24Warp, MHC2ProfileParams::sdrGamma24, MhcSdrGamma24)
//
// The switch reads the calibrated curve (base + fine-tune) at t^(2.4/2.2), so a display calibrated to pure 2.2 shows
// pure 2.4 whatever the panel's own response. It used to bake sRGB_OETF(sRGB_EOTF(v)^(2.4/2.2)) AFTER the calibration,
// in the panel's own signal domain: exact only on a panel whose native response is the piecewise sRGB curve (on a
// near-2.2 panel ~40% too little light at the bottom codes), and only while the fine-tune curve was on.

#include <windows.h>
#include "doctest.h"
#include "mhc.h"
#include "gui_mhc.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <string>
#include <vector>

namespace {

// A synthetic panel: the light (relative to white) its native response makes from a drive signal.
struct Panel {
    const char* name;
    std::function<double(double)> light;
};

double SrgbPiecewise(double v) { return v <= 0.04045 ? v / 12.92 : std::pow((v + 0.055) / 1.055, 2.4); }

std::vector<Panel> Panels() {
    return {
        { "pure 2.2 (the PA32UCXR's own response is close to it)", [](double s) { return std::pow(s, 2.2); } },
        { "piecewise sRGB", SrgbPiecewise },
        { "2.5, crushed shadows", [](double s) { return std::pow(s, 2.5); } },
        { "2.0 with a soft top", [](double s) { return std::pow(s, 2.0) * (0.85 + 0.15 * s); } },
    };
}

// The drive signal the panel needs for `y` (its response is monotone): bisection to double precision.
double DriveFor(const Panel& p, double y) {
    double lo = 0.0, hi = 1.0;
    for (int i = 0; i < 100; i++) {
        const double mid = 0.5 * (lo + hi);
        (p.light(mid) < y ? lo : hi) = mid;
    }
    return 0.5 * (lo + hi);
}

// The base a calibration builds for this panel (DLC's base cube): corr(x) = N^-1(x^2.2), so panel(corr(x)) = x^2.2.
std::vector<float> CalibrationCube(const Panel& p, int n) {
    std::vector<float> c(n);
    for (int i = 0; i < n; i++) c[i] = (float)DriveFor(p, std::pow((double)i / (n - 1), 2.2));
    return c;
}

MHC2ProfileParams CubeParams(const std::vector<float>& cube, bool gamma24) {
    MHC2ProfileParams p;
    p.isHDR = false;
    p.hasPrecomputedCorrection = true;
    p.corrR = p.corrG = p.corrB = cube;
    p.grayscaleEnabled = true;
    p.grayscale.enabled = true;
    p.sdrGamma24 = gamma24;
    return p;
}

// A base from points: a per-channel curve as a user (or DLC's GS+WB run) leaves it.
MHCSettings PointsSettings() {
    MHCSettings m;
    m.enabled = true;
    m.profileName = L"DesktopLUT_Gamma24Test.icm";
    m.baseGrayscale.enabled = true;
    m.baseGrayscale.pointCount = 20;
    m.baseGrayscale.initLinear();
    for (int i = 1; i < 19; i++) {
        m.baseGrayscale.points[i] *= 1.0f + 0.03f * std::sin((float)i);
        m.baseGrayscale.rgbDeviations[0][i] = 1.01f;
        m.baseGrayscale.rgbDeviations[2][i] = 0.985f;
    }
    m.correctionGrayscale.pointCount = 10;
    m.correctionGrayscale.initLinear();
    return m;
}

bool LutsEqual(const MhcScanout& a, const MhcScanout& b) {
    for (int ch = 0; ch < 3; ch++)
        if (a.lut[ch] != b.lut[ch]) return false;
    return true;
}

}  // namespace

TEST_CASE("GAMMA 2.4: the warp fed to a pure 2.2 response is pure 2.4") {
    CHECK(MhcGamma24Warp(0.0f) == 0.0f);
    CHECK(MhcGamma24Warp(1.0f) == 1.0f);
    float prev = 0.0f;
    for (int i = 1; i <= 1023; i++) {
        const float t = (float)i / 1023.0f;
        const float w = MhcGamma24Warp(t);
        CHECK(w > prev);
        CHECK(w <= t);   // 2.4 is the darker curve
        CHECK(std::pow((double)w, 2.2) == doctest::Approx(std::pow((double)t, 2.4)).epsilon(2e-6));
        prev = w;
    }
}

TEST_CASE("GAMMA 2.4: a 2.2 calibration shows pure 2.4 end to end, whatever the panel's own response") {
    // Every entry must drive the panel with the exact signal that shows t^2.4 — to 2e-4 of full scale (a fifth of a
    // 10-bit code): the cube's own linear interpolation where the panel's toe is steepest, and the file's s15Fixed16
    // rounding. The previous formula, sRGB_OETF(sRGB_EOTF(v)^(2.4/2.2)) after the calibration, kept as the record of
    // why it was replaced: exact only on a panel whose own response is the piecewise sRGB curve.
    auto old = [](float v) { return SrgbOETF(std::pow((std::max)(SrgbEOTF(v), 0.0f), 2.4f / 2.2f)); };
    const double tol = 2e-4;
    for (const Panel& panel : Panels()) {
        INFO(std::string(panel.name));
        const bool srgbPanel = std::string(panel.name) == "piecewise sRGB";
        const std::vector<float> cube = CalibrationCube(panel, 1024);
        MhcScanout on, off;
        REQUIRE(ComputeMhcScanout(CubeParams(cube, true), on));
        REQUIRE(ComputeMhcScanout(CubeParams(cube, false), off));
        double worstOn = 0.0, worstOff = 0.0, worstOld = 0.0;
        for (int ch = 0; ch < 3; ch++) {
            for (int j = 0; j < on.lutSize; j++) {
                const double t = (double)j / (on.lutSize - 1);
                const double drive24 = DriveFor(panel, std::pow(t, 2.4));
                worstOn = (std::max)(worstOn, std::fabs(on.lut[ch][j] - drive24));
                worstOff = (std::max)(worstOff, std::fabs(off.lut[ch][j] - DriveFor(panel, std::pow(t, 2.2))));
                worstOld = (std::max)(worstOld, std::fabs(QuantizeS15Fixed16(old(off.lut[ch][j])) - drive24));
            }
        }
        CHECK(worstOff < tol);   // the model: the calibration alone is pure 2.2
        CHECK(worstOn < tol);
        if (srgbPanel) CHECK(worstOld < tol);
        else CHECK(worstOld > 10 * tol);
    }
    // On a pure 2.2 panel (the PA32UCXR's own response is close to it) the previous formula left 8-bit code 10 at
    // ~60% of the light 2.4 asks for.
    const Panel pure22 = Panels()[0];
    MhcScanout off;
    REQUIRE(ComputeMhcScanout(CubeParams(CalibrationCube(pure22, 1024), false), off));
    const int code10 = (int)std::lround(10.0 / 255.0 * 1023.0);
    CHECK(pure22.light(old(off.lut[1][code10])) / std::pow(code10 / 1023.0, 2.4) < 0.65);
}

TEST_CASE("GAMMA 2.4: every base source is read at the warp, and the fine-tune after it") {
    SUBCASE("base points + a fine-tune curve: LUT = fine-tune(base(t^(2.4/2.2)))") {
        MHCSettings m = PointsSettings();
        m.correctionGrayscale.enabled = true;
        m.correctionGrayscale.points[3] *= 1.05f;
        m.correctionGrayscale.rgbDeviations[1][6] = 0.98f;
        m.correctionGrayscale.use24Gamma = true;
        MHC2ProfileParams p;
        REQUIRE(BuildMHC2Params(m, false, 0, p));
        REQUIRE(p.sdrGamma24);
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(p, s));
        for (int ch = 0; ch < 3; ch++) {
            for (int j = 0; j < s.lutSize; j++) {
                const float w = MhcGamma24Warp((float)j / (float)(s.lutSize - 1));
                const float base = std::clamp(EvalGrayscaleSDR_Channel(w, p.grayscale, ch), 0.0f, 1.0f);
                const float want = QuantizeS15Fixed16(
                    std::clamp(EvalGrayscaleSDR_Channel(base, p.correctionGrayscale, ch), 0.0f, 1.0f));
                CHECK(std::fabs(s.lut[ch][j] - want) < 2e-5f);   // within one s15Fixed16 step
            }
        }
    }
    SUBCASE("an imported ICC's curves: inverted toward t^2.4 = the 2.2 aim read at the warp") {
        MHC2ProfileParams p;
        p.isHDR = false;
        p.hasPerChannelTRC = true;
        p.grayscaleEnabled = true;
        p.grayscale.enabled = true;
        const Panel panel = Panels()[3];
        for (int i = 0; i < 256; i++) {
            const float v = (float)panel.light(i / 255.0);
            p.trcR.push_back(v); p.trcG.push_back(v); p.trcB.push_back(v);
        }
        p.sdrGamma24 = true;
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(p, s));
        std::vector<float> aim24(s.lutSize);
        GenerateMHC2LUT_FromTRC_SDR(p.trcG, aim24.data(), s.lutSize, 2.4f);
        for (int j = 0; j < s.lutSize; j++) CHECK(std::fabs(s.lut[1][j] - QuantizeS15Fixed16(aim24[j])) < 2e-5f);
    }
    SUBCASE("no base curve at all: the LUT is the warp itself") {
        MHC2ProfileParams p;
        p.isHDR = false;
        p.sdrGamma24 = true;
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(p, s));
        for (int j = 0; j < s.lutSize; j++)
            CHECK(s.lut[0][j] == QuantizeS15Fixed16(MhcGamma24Warp((float)j / (float)(s.lutSize - 1))));
    }
}

TEST_CASE("GAMMA 2.4: the switch works with the fine-tune curve off") {
    MHCSettings m = PointsSettings();
    m.correctionGrayscale.enabled = false;
    m.correctionGrayscale.use24Gamma = true;
    CHECK(MhcSdrGamma24(m));
    CHECK((ComputeMhcPermutation(m, false) & MHCSettings::PERM_GS) == 0);
    MHC2ProfileParams p;
    REQUIRE(BuildMHC2Params(m, false, 0, p));
    CHECK(p.sdrGamma24);
    CHECK_FALSE(p.correctionGrayscaleEnabled);
    MhcScanout on;
    REQUIRE(ComputeMhcScanout(p, on));

    MHCSettings plain = m;
    plain.correctionGrayscale.use24Gamma = false;
    MHC2ProfileParams pp;
    REQUIRE(BuildMHC2Params(plain, false, 0, pp));
    CHECK_FALSE(pp.sdrGamma24);
    MhcScanout off;
    REQUIRE(ComputeMhcScanout(pp, off));
    CHECK_FALSE(LutsEqual(on, off));

    // A neutral fine-tune curve switched on changes nothing: the switch does not depend on it.
    MHCSettings withCurve = m;
    withCurve.correctionGrayscale.enabled = true;
    MHC2ProfileParams pc;
    REQUIRE(BuildMHC2Params(withCurve, false, 0, pc));
    MhcScanout curve;
    REQUIRE(ComputeMhcScanout(pc, curve));
    for (int ch = 0; ch < 3; ch++)
        for (int j = 0; j < on.lutSize; j++) CHECK(std::fabs(curve.lut[ch][j] - on.lut[ch][j]) < 2e-5f);
}

TEST_CASE("GAMMA 2.4: SDR only, part of the grayscale stage, and the base curve's older flag does the same") {
    MHCSettings m = PointsSettings();
    m.correctionGrayscale.use24Gamma = true;
    MHC2ProfileParams hdr;
    REQUIRE(BuildMHC2Params(m, true, 0, hdr));
    CHECK_FALSE(hdr.sdrGamma24);

    MHCSettings bypassed = m;
    bypassed.bypassGrayscale = true;
    MHC2ProfileParams nogray;
    REQUIRE(BuildMHC2ParamsForPerm(bypassed, false, 0, ComputeMhcPermutation(bypassed, false), nogray));
    CHECK_FALSE(nogray.sdrGamma24);

    MHCSettings legacy = PointsSettings();
    legacy.baseGrayscale.use24Gamma = true;
    CHECK(MhcSdrGamma24(legacy));
    MHC2ProfileParams lp, sp;
    REQUIRE(BuildMHC2Params(legacy, false, 0, lp));
    REQUIRE(BuildMHC2Params(m, false, 0, sp));
    CHECK(lp.sdrGamma24);
    MhcScanout a, b;
    REQUIRE(ComputeMhcScanout(lp, a));
    REQUIRE(ComputeMhcScanout(sp, b));
    CHECK(LutsEqual(a, b));   // both flags: one warp, never two
    MHCSettings both = legacy;
    both.correctionGrayscale.use24Gamma = true;
    MHC2ProfileParams bp;
    REQUIRE(BuildMHC2Params(both, false, 0, bp));
    MhcScanout c;
    REQUIRE(ComputeMhcScanout(bp, c));
    CHECK(LutsEqual(a, c));
}

TEST_CASE("GAMMA 2.4: the profile's TRC tag stays the base curve's (the switch leaves it at 2.2)") {
    auto tagGamma = [](const MHCSettings& m) {
        MHC2ProfileParams p;
        REQUIRE(BuildMHC2Params(m, false, 0, p));
        std::vector<uint8_t> data;
        REQUIRE(GenerateMHC2Profile(p, data));
        wchar_t dir[MAX_PATH];
        GetTempPathW(MAX_PATH, dir);
        const std::wstring path = std::wstring(dir) + L"_dlut_gamma24_tag.icm";
        REQUIRE(WriteMHC2Profile(data, path));
        ICCProfileData icc;
        const bool read = ReadICCProfile(path, icc);
        DeleteFileW(path.c_str());
        REQUIRE(read);
        REQUIRE(icc.hasGamma);
        return icc.gamma;
    };
    MHCSettings m = PointsSettings();
    m.correctionGrayscale.use24Gamma = true;
    CHECK(tagGamma(m) == doctest::Approx(2.2f).epsilon(0.005));
    MHCSettings legacy = PointsSettings();
    legacy.baseGrayscale.use24Gamma = true;
    CHECK(tagGamma(legacy) == doctest::Approx(2.4f).epsilon(0.005));
}

TEST_CASE("GAMMA 2.4: the profile label names it") {
    MHCSettings m = PointsSettings();
    m.baseGrayscale.initLinear();   // unadjusted: the label names the gamma
    ComputeMhcMetadata(m, false);
    CHECK(m.metaGamma == L"2.2");
    m.correctionGrayscale.use24Gamma = true;
    ComputeMhcMetadata(m, false);
    CHECK(m.metaGamma == L"2.2\u21922.4");
}
