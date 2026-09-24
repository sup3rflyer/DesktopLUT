#include "doctest.h"
#include "settings.h"
#include "globals.h"
#include "monitor_identity.h"
#include "fald.h"    // FALD_TAU_MAX_MS
#include <cstdio>
#include <cmath>
#include <string>

// Helper to create a temp INI file and clean up
struct TempIni {
    std::wstring path;
    TempIni() {
        wchar_t tmp[MAX_PATH];
        GetTempPathW(MAX_PATH, tmp);
        path = std::wstring(tmp) + L"desktoplut_test.ini";
    }
    ~TempIni() {
        _wremove(path.c_str());
    }
    const wchar_t* c_str() const { return path.c_str(); }
};

// ============================================================================
// Boolean Parsing
// ============================================================================

TEST_CASE("Bool: true values") {
    TempIni ini;

    WritePrivateProfileStringW(L"Test", L"val_true", L"true", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"val_1", L"1", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"val_yes", L"yes", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"val_TRUE", L"TRUE", ini.c_str());

    CHECK(GetPrivateProfileBool(L"Test", L"val_true", false, ini.c_str()) == true);
    CHECK(GetPrivateProfileBool(L"Test", L"val_1", false, ini.c_str()) == true);
    CHECK(GetPrivateProfileBool(L"Test", L"val_yes", false, ini.c_str()) == true);
    CHECK(GetPrivateProfileBool(L"Test", L"val_TRUE", false, ini.c_str()) == true);
}

TEST_CASE("Bool: false values") {
    TempIni ini;

    WritePrivateProfileStringW(L"Test", L"val_false", L"false", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"val_0", L"0", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"val_no", L"no", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"val_False", L"False", ini.c_str());

    CHECK(GetPrivateProfileBool(L"Test", L"val_false", true, ini.c_str()) == false);
    CHECK(GetPrivateProfileBool(L"Test", L"val_0", true, ini.c_str()) == false);
    CHECK(GetPrivateProfileBool(L"Test", L"val_no", true, ini.c_str()) == false);
    CHECK(GetPrivateProfileBool(L"Test", L"val_False", true, ini.c_str()) == false);
}

TEST_CASE("Bool: missing key returns default") {
    TempIni ini;
    CHECK(GetPrivateProfileBool(L"Test", L"missing", true, ini.c_str()) == true);
    CHECK(GetPrivateProfileBool(L"Test", L"missing", false, ini.c_str()) == false);
}

TEST_CASE("Bool: empty value returns default") {
    TempIni ini;
    WritePrivateProfileStringW(L"Test", L"empty", L"", ini.c_str());
    CHECK(GetPrivateProfileBool(L"Test", L"empty", true, ini.c_str()) == true);
}

// ============================================================================
// Whitelist Parsing
// ============================================================================

TEST_CASE("Whitelist: basic CSV") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"mpv, chrome, firefox", out);
    REQUIRE(out.size() == 3);
    CHECK(out[0] == L"mpv");
    CHECK(out[1] == L"chrome");
    CHECK(out[2] == L"firefox");
}

TEST_CASE("Whitelist: semicolon separator") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"mpv; chrome; firefox", out);
    REQUIRE(out.size() == 3);
    CHECK(out[0] == L"mpv");
    CHECK(out[1] == L"chrome");
    CHECK(out[2] == L"firefox");
}

TEST_CASE("Whitelist: mixed separators") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"mpv, chrome; firefox", out);
    REQUIRE(out.size() == 3);
}

TEST_CASE("Whitelist: uppercase and .exe stripped") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"MPV.exe; Chrome.EXE", out);
    REQUIRE(out.size() == 2);
    CHECK(out[0] == L"mpv");
    CHECK(out[1] == L"chrome");
}

TEST_CASE("Whitelist: whitespace trimming") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"  mpv  ,  chrome  ", out);
    REQUIRE(out.size() == 2);
    CHECK(out[0] == L"mpv");
    CHECK(out[1] == L"chrome");
}

TEST_CASE("Whitelist: empty string") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"", out);
    CHECK(out.empty());
}

TEST_CASE("Whitelist: trailing separator") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"mpv,", out);
    REQUIRE(out.size() == 1);
    CHECK(out[0] == L"mpv");
}

TEST_CASE("Whitelist: multiple separators") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"a,,b", out);
    REQUIRE(out.size() == 2);
    CHECK(out[0] == L"a");
    CHECK(out[1] == L"b");
}

TEST_CASE("Whitelist: only whitespace") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"  ,  ,  ", out);
    CHECK(out.empty());
}

TEST_CASE("Whitelist: single item") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"notepad", out);
    REQUIRE(out.size() == 1);
    CHECK(out[0] == L"notepad");
}

TEST_CASE("Whitelist: .exe only not stripped (4 chars, needs >4)") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L".exe", out);
    // ".exe" is exactly 4 chars, stripping requires size() > 4
    // So ".exe" stays as ".exe" (lowercased)
    REQUIRE(out.size() == 1);
    CHECK(out[0] == L".exe");
}

TEST_CASE("Whitelist: a.exe stripped to 'a'") {
    std::vector<std::wstring> out;
    ParseWhitelistString(L"a.exe", out);
    REQUIRE(out.size() == 1);
    CHECK(out[0] == L"a");
}

// ============================================================================
// Tonemap Curve Enum
// ============================================================================

TEST_CASE("Tonemap: enum to string") {
    CHECK(std::wstring(TonemapCurveToString(TonemapCurve::BT2390)) == L"BT2390");
    CHECK(std::wstring(TonemapCurveToString(TonemapCurve::SoftClip)) == L"SoftClip");
    CHECK(std::wstring(TonemapCurveToString(TonemapCurve::Reinhard)) == L"Reinhard");
    CHECK(std::wstring(TonemapCurveToString(TonemapCurve::BT2446A)) == L"BT2446A");
    CHECK(std::wstring(TonemapCurveToString(TonemapCurve::HardClip)) == L"HardClip");
}

TEST_CASE("Tonemap: string to enum") {
    CHECK(StringToTonemapCurve(L"BT2390") == TonemapCurve::BT2390);
    CHECK(StringToTonemapCurve(L"SoftClip") == TonemapCurve::SoftClip);
    CHECK(StringToTonemapCurve(L"Reinhard") == TonemapCurve::Reinhard);
    CHECK(StringToTonemapCurve(L"BT2446A") == TonemapCurve::BT2446A);
    CHECK(StringToTonemapCurve(L"HardClip") == TonemapCurve::HardClip);
}

TEST_CASE("Tonemap: unknown string defaults to BT2390") {
    CHECK(StringToTonemapCurve(L"InvalidCurve") == TonemapCurve::BT2390);
}

TEST_CASE("Tonemap: all 5 curves round-trip") {
    TonemapCurve curves[] = {
        TonemapCurve::BT2390, TonemapCurve::SoftClip, TonemapCurve::Reinhard,
        TonemapCurve::BT2446A, TonemapCurve::HardClip
    };
    for (auto c : curves) {
        CHECK(StringToTonemapCurve(TonemapCurveToString(c)) == c);
    }
}

// ============================================================================
// Color Correction Settings Round-Trip
// Primaries, grayscale, WB, and desktop gamma moved to MHC settings on this branch.
// CC only persists tonemapping (HDR) — everything else loads as defaults.
// ============================================================================

TEST_CASE("CC settings: SDR saves and loads no-ops (tonemapping only branch)") {
    TempIni ini;
    ColorCorrectionSettings original;
    original.primariesEnabled = true;       // should NOT persist
    original.grayscale.enabled = true;      // should NOT persist
    original.grayscale.use24Gamma = true;   // should NOT persist

    SaveColorCorrectionSettings(L"TestMon", L"SDR_", original, ini.c_str());

    ColorCorrectionSettings loaded;
    LoadColorCorrectionSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    // Primaries and grayscale are in MHC now — defaults come back
    CHECK(loaded.primariesEnabled == false);
    CHECK(loaded.grayscale.enabled == false);
    CHECK(loaded.grayscale.use24Gamma == false);
}

TEST_CASE("CC settings: HDR tonemap round-trip") {
    TempIni ini;
    ColorCorrectionSettings original;
    original.grayscale.pointCount = 20;
    original.grayscale.initLinearPQ();
    original.tonemap.enabled = true;
    original.tonemap.curve = TonemapCurve::SoftClip;
    original.tonemap.sourcePeakNits = 4000.0f;
    original.tonemap.targetPeakNits = 1000.0f;
    original.tonemap.dynamicPeak = true;

    SaveColorCorrectionSettings(L"TestMon", L"HDR_", original, ini.c_str());

    ColorCorrectionSettings loaded;
    LoadColorCorrectionSettings(L"TestMon", L"HDR_", loaded, ini.c_str());

    CHECK(loaded.tonemap.enabled == true);
    CHECK(loaded.tonemap.curve == TonemapCurve::SoftClip);
    CHECK(loaded.tonemap.sourcePeakNits == doctest::Approx(4000.0f).epsilon(0.001));
    CHECK(loaded.tonemap.targetPeakNits == doctest::Approx(1000.0f).epsilon(0.001));
    CHECK(loaded.tonemap.dynamicPeak == true);
}

TEST_CASE("CC settings: FALD layer round-trips per mode (SDR under ACM and HDR)") {
    for (const wchar_t* prefix : { L"SDR_", L"HDR_" }) {
        TempIni ini;
        ColorCorrectionSettings original;
        original.fald.enabled = true;
        original.fald.paramsPath = L"C:\\panels\\pa32ucxr_sdr_fald_panel.bin";
        original.fald.pedMode = 1;
        original.fald.debugMode = 4;    // runtime only: must NOT persist
        original.fald.temporalMode = 2; // temporal drive state (2026-09-17): persisted
        original.fald.tauRiseMs = 40.5f;
        original.fald.tauFallMs = 120.0f;
        original.fald.delayFrames = 2;
        original.fald.clockClosure = 0.65f;  // panel clock (temporal mode 3, 2026-09-20, work guide C13): persisted
        original.fald.clockParity = 1;
        original.fald.star.enabled = true;   // starfield balancing (2026-09-19, work guide S1): persisted
        original.fald.star.even = 0.75f; original.fald.star.lift = 0.25f; original.fald.star.targetGain = 0.8f;
        original.fald.star.targetSigma = 1.75f; original.fald.star.keepNits = 60.0f;
        original.fald.star.evenReach = 5; original.fald.star.capNits = 400.0f; original.fald.star.strength = 0.5f;
        original.fald.star.areaLo = 30.0f; original.fald.star.areaHi = 200.0f; original.fald.star.peakHi = 900.0f;
        original.fald.star.reach = 3; original.fald.star.nbLo = 0.1f; original.fald.star.nbHi = 0.4f;
        original.fald.glow.enabled = true;   // glow fill (2026-09-20, work guide S2): persisted
        original.fald.glow.strength = 0.6f; original.fald.glow.reach = 3; original.fald.glow.capNits = 0.045f;
        const bool hdrSlot = std::wstring(prefix) == L"HDR_";    // HDR only: the SDR slot's switch never loads as on
        SaveColorCorrectionSettings(L"TestMon", prefix, original, ini.c_str());

        ColorCorrectionSettings loaded;
        LoadColorCorrectionSettings(L"TestMon", prefix, loaded, ini.c_str());
        CHECK(loaded.fald.enabled == true);
        CHECK(loaded.fald.paramsPath == L"C:\\panels\\pa32ucxr_sdr_fald_panel.bin");
        CHECK(loaded.fald.pedMode == 1u);
        CHECK(loaded.fald.debugMode == 0u);
        CHECK(loaded.fald.temporalMode == 2u);
        CHECK(loaded.fald.tauRiseMs == doctest::Approx(40.5f).epsilon(1e-4));
        CHECK(loaded.fald.tauFallMs == doctest::Approx(120.0f).epsilon(1e-4));
        CHECK(loaded.fald.delayFrames == 2u);
        CHECK(loaded.fald.clockClosure == doctest::Approx(0.65f).epsilon(1e-4));
        CHECK(loaded.fald.clockParity == 1);
        CHECK(loaded.fald.star.enabled);
        CHECK(loaded.fald.star.even == doctest::Approx(0.75f).epsilon(1e-4));
        CHECK(loaded.fald.star.lift == doctest::Approx(0.25f).epsilon(1e-4));
        CHECK(loaded.fald.star.targetGain == doctest::Approx(0.8f).epsilon(1e-4));
        CHECK(loaded.fald.star.targetSigma == doctest::Approx(1.75f).epsilon(1e-4));
        CHECK(loaded.fald.star.keepNits == doctest::Approx(60.0f).epsilon(1e-4));
        CHECK(loaded.fald.star.evenReach == 5u);
        CHECK(loaded.fald.star.capNits == doctest::Approx(400.0f).epsilon(1e-4));
        CHECK(loaded.fald.star.strength == doctest::Approx(0.5f).epsilon(1e-4));
        CHECK(loaded.fald.star.areaLo == doctest::Approx(30.0f).epsilon(1e-4));
        CHECK(loaded.fald.star.areaHi == doctest::Approx(200.0f).epsilon(1e-4));
        CHECK(loaded.fald.star.peakHi == doctest::Approx(900.0f).epsilon(1e-4));
        CHECK(loaded.fald.star.reach == 3u);
        CHECK(loaded.fald.star.nbLo == doctest::Approx(0.1f).epsilon(1e-4));
        CHECK(loaded.fald.star.nbHi == doctest::Approx(0.4f).epsilon(1e-4));
        CHECK(loaded.fald.glow.enabled == hdrSlot);
        CHECK(loaded.fald.glow.strength == doctest::Approx(0.6f).epsilon(1e-4));
        CHECK(loaded.fald.glow.reach == 3u);
        CHECK(loaded.fald.glow.capNits == doctest::Approx(0.045f).epsilon(1e-4));
    }
    // a fresh INI (no keys) = the filter OFF; out-of-range values are clamped, an unknown mode is OFF
    {
        TempIni ini;
        ColorCorrectionSettings fresh;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", fresh, ini.c_str());
        CHECK(fresh.fald.temporalMode == 0u);
        CHECK(fresh.fald.tauRiseMs == 0.0f);
        CHECK(fresh.fald.tauFallMs == 0.0f);
        CHECK(fresh.fald.clockClosure == FALD_CLOCK_CLOSURE_DEFAULT);   // panel clock: 0.72, parity unknown
        CHECK(fresh.fald.clockParity == -1);
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalClosure", L"3.5", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalParity", L"4", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalMode", L"7", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTauRiseMs", L"9000", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTauFallMs", L"-3", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldDelayFrames", L"9", ini.c_str());
        ColorCorrectionSettings clamped;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", clamped, ini.c_str());
        CHECK(clamped.fald.temporalMode == 0u);
        CHECK(clamped.fald.tauRiseMs == FALD_TAU_MAX_MS);
        CHECK(clamped.fald.tauFallMs == 0.0f);
        CHECK(clamped.fald.delayFrames == FALD_DELAY_MAX);
        CHECK(clamped.fald.clockClosure == FALD_CLOCK_CLOSURE_MAX);
        CHECK(clamped.fald.clockParity == -1);                          // an unknown parity value = unknown
        // mode 3 (panel clock) is a valid persisted mode; closure below the range is clamped up, parity -1 / 0 survive
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalMode", L"3", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalClosure", L"0.01", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalParity", L"0", ini.c_str());
        ColorCorrectionSettings panelClock;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", panelClock, ini.c_str());
        CHECK(panelClock.fald.temporalMode == FALD_TEMPORAL_PANEL);
        CHECK(panelClock.fald.clockClosure == FALD_CLOCK_CLOSURE_MIN);
        CHECK(panelClock.fald.clockParity == 0);
        panelClock.fald.clockParity = -1;                               // the default parity round-trips as "-1"
        SaveColorCorrectionSettings(L"TestMon", L"SDR_", panelClock, ini.c_str());
        ColorCorrectionSettings panelClock2;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", panelClock2, ini.c_str());
        CHECK(panelClock2.fald.temporalMode == FALD_TEMPORAL_PANEL);
        CHECK(panelClock2.fald.clockParity == -1);
        // an empty or garbage parity is UNKNOWN (-1), never 0 (a known parity)
        for (const wchar_t* bad : { L"", L"abc", L"1x", L"2", L"0.5" }) {
            WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalParity", bad, ini.c_str());
            ColorCorrectionSettings garbage;
            LoadColorCorrectionSettings(L"TestMon", L"SDR_", garbage, ini.c_str());
            CHECK(garbage.fald.clockParity == -1);
        }
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldTemporalParity", L" 1", ini.c_str());
        ColorCorrectionSettings spaced;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", spaced, ini.c_str());
        CHECK(spaced.fald.clockParity == 1);
        CHECK(fresh.fald.delayFrames == 0u);
        // starfield balancing: a fresh INI = OFF with the reference defaults; out-of-range values are clamped
        CHECK_FALSE(fresh.fald.star.enabled);
        CHECK(fresh.fald.star.even == doctest::Approx(0.8f));
        CHECK(fresh.fald.star.targetSigma == 0.0f);
        CHECK(fresh.fald.star.keepNits == 100.0f);
        CHECK(fresh.fald.star.lift == 0.0f);
        CHECK(fresh.fald.star.evenReach == 8u);
        CHECK(fresh.fald.star.reach == 2u);
        CHECK(fresh.fald.star.areaLo == 40.0f);
        CHECK(fresh.fald.star.areaHi == 160.0f);
        CHECK(fresh.fald.star.nbLo == doctest::Approx(0.15f));
        CHECK(fresh.fald.star.nbHi == doctest::Approx(0.30f));
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarfield", L"1", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarEven", L"4", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarTargetGain", L"0.001", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarTargetSigma", L"7.5", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarEvenReach", L"50", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarReach", L"-2", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarAreaLo", L"300", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldStarAreaHi", L"100", ini.c_str());
        ColorCorrectionSettings starClamped;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", starClamped, ini.c_str());
        CHECK(starClamped.fald.star.enabled);
        CHECK(starClamped.fald.star.even == 1.0f);
        CHECK(starClamped.fald.star.targetGain == doctest::Approx(0.05f));
        CHECK(starClamped.fald.star.targetSigma == 4.0f);
        CHECK(starClamped.fald.star.evenReach == FALD_STAR_EVEN_REACH_MAX);
        CHECK(starClamped.fald.star.reach == 0u);
        CHECK(starClamped.fald.star.areaLo == 300.0f);
        CHECK(starClamped.fald.star.areaHi == 300.0f);
        // glow fill: a fresh INI = OFF with the reference defaults; out-of-range values are clamped
        CHECK_FALSE(fresh.fald.glow.enabled);
        CHECK(fresh.fald.glow.strength == 1.0f);
        CHECK(fresh.fald.glow.reach == 2u);
        CHECK(fresh.fald.glow.capNits == doctest::Approx(0.05f));
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldGlowFill", L"1", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldGlowStrength", L"2.5", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldGlowReach", L"-3", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldGlowCapNits", L"9", ini.c_str());
        ColorCorrectionSettings glowClamped;
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", glowClamped, ini.c_str());
        CHECK_FALSE(glowClamped.fald.glow.enabled);                     // HDR only: an SDR_FaldGlowFill=1 in the INI stays off
        WritePrivateProfileStringW(L"TestMon", L"HDR_FaldGlowFill", L"1", ini.c_str());
        ColorCorrectionSettings glowHdr;
        LoadColorCorrectionSettings(L"TestMon", L"HDR_", glowHdr, ini.c_str());
        CHECK(glowHdr.fald.glow.enabled);
        CHECK(glowClamped.fald.glow.strength == 1.0f);
        CHECK(glowClamped.fald.glow.reach == FALD_GLOW_REACH_MIN);
        CHECK(glowClamped.fald.glow.capNits == FALD_GLOW_CAP_MAX);
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldGlowReach", L"40", ini.c_str());
        WritePrivateProfileStringW(L"TestMon", L"SDR_FaldGlowCapNits", L"0.0001", ini.c_str());
        LoadColorCorrectionSettings(L"TestMon", L"SDR_", glowClamped, ini.c_str());
        CHECK(glowClamped.fald.glow.reach == FALD_GLOW_REACH_MAX);
        CHECK(glowClamped.fald.glow.capNits == FALD_GLOW_CAP_MIN);
    }
    // the two modes are independent keys: an SDR save never touches the HDR slot
    TempIni ini;
    ColorCorrectionSettings sdr, hdr;
    sdr.fald.enabled = true; sdr.fald.paramsPath = L"sdr.bin";
    SaveColorCorrectionSettings(L"TestMon", L"SDR_", sdr, ini.c_str());
    SaveColorCorrectionSettings(L"TestMon", L"HDR_", hdr, ini.c_str());
    ColorCorrectionSettings loadedSdr, loadedHdr;
    LoadColorCorrectionSettings(L"TestMon", L"SDR_", loadedSdr, ini.c_str());
    LoadColorCorrectionSettings(L"TestMon", L"HDR_", loadedHdr, ini.c_str());
    CHECK(loadedSdr.fald.enabled == true);
    CHECK(loadedHdr.fald.enabled == false);
    CHECK(loadedHdr.fald.paramsPath.empty());
}

TEST_CASE("CC settings: 24Gamma not persisted (moved to MHC)") {
    TempIni ini;
    ColorCorrectionSettings original;
    original.grayscale.use24Gamma = true;  // moved to MHC — should NOT persist

    SaveColorCorrectionSettings(L"TestMon", L"SDR_", original, ini.c_str());

    ColorCorrectionSettings loaded;
    LoadColorCorrectionSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.grayscale.use24Gamma == false);
}

// ============================================================================
// MHC Settings Round-Trip
// ============================================================================

TEST_CASE("MHC settings: default round-trip") {
    TempIni ini;
    MHCSettings original;
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();

    SaveMHCSettings(L"TestMon", L"SDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.enabled == original.enabled);
    CHECK(loaded.primariesEnabled == original.primariesEnabled);
}

TEST_CASE("MHC settings: with profile path") {
    TempIni ini;
    MHCSettings original;
    original.enabled = true;
    original.profilePath = L"C:\\test\\profile.icm";
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();

    SaveMHCSettings(L"TestMon", L"SDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.enabled == true);
    CHECK(loaded.profilePath == L"C:\\test\\profile.icm");
}

TEST_CASE("MHC settings: with source file") {
    TempIni ini;
    MHCSettings original;
    original.sourceFilePath = L"D:\\calibration\\test.cube";
    original.sourceIs1DCube = true;
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();

    SaveMHCSettings(L"TestMon", L"SDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.sourceFilePath == L"D:\\calibration\\test.cube");
    CHECK(loaded.sourceIs1DCube == true);
}

TEST_CASE("MHC settings: with metadata") {
    TempIni ini;
    MHCSettings original;
    original.metaPrimaries = L"P3-D65";
    original.metaGamma = L"2.2";
    original.metaPeakNits = 1000.0f;
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();

    SaveMHCSettings(L"TestMon", L"HDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"HDR_", loaded, ini.c_str());

    CHECK(loaded.metaPrimaries == L"P3-D65");
    CHECK(loaded.metaGamma == L"2.2");
    CHECK(loaded.metaPeakNits == doctest::Approx(1000.0f).epsilon(0.001));
}

TEST_CASE("MHC settings: with primaries") {
    TempIni ini;
    MHCSettings original;
    original.primariesEnabled = true;
    original.customPrimaries = {0.6800f, 0.3200f, 0.2650f, 0.6900f, 0.1500f, 0.0600f, 0.3127f, 0.3290f, L"Custom"};
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();

    SaveMHCSettings(L"TestMon", L"SDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.primariesEnabled == true);
    CHECK(loaded.customPrimaries.Rx == doctest::Approx(0.6800f).epsilon(0.0001));
    CHECK(loaded.customPrimaries.Gy == doctest::Approx(0.6900f).epsilon(0.0001));
}

// ============================================================================
// Bool Edge Cases
// ============================================================================

TEST_CASE("Bool: unrecognized value returns default") {
    TempIni ini;
    WritePrivateProfileStringW(L"Test", L"maybe", L"maybe", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"two", L"2", ini.c_str());
    WritePrivateProfileStringW(L"Test", L"on", L"on", ini.c_str());

    CHECK(GetPrivateProfileBool(L"Test", L"maybe", false, ini.c_str()) == false);
    CHECK(GetPrivateProfileBool(L"Test", L"maybe", true, ini.c_str()) == true);
    CHECK(GetPrivateProfileBool(L"Test", L"two", false, ini.c_str()) == false);
    CHECK(GetPrivateProfileBool(L"Test", L"on", false, ini.c_str()) == false);
}

// ============================================================================
// Float I/O Locale Safety
// ============================================================================

TEST_CASE("Float: round-trip preserves decimal point") {
    TempIni ini;
    // Values that would break under comma-decimal locales (e.g. 0.6400 → "0,6400")
    float testValues[] = { 0.6400f, 0.3300f, 0.0001f, 1.0f, 0.0f, -0.5f, 999.123f };
    for (int i = 0; i < 7; i++) {
        wchar_t key[32];
        swprintf(key, 32, L"val%d", i);
        WritePrivateProfileFloat(L"FloatTest", key, testValues[i], ini.c_str());
    }
    for (int i = 0; i < 7; i++) {
        wchar_t key[32];
        swprintf(key, 32, L"val%d", i);
        float loaded = GetPrivateProfileFloat(L"FloatTest", key, -999.0f, ini.c_str());
        CHECK(loaded == doctest::Approx(testValues[i]).epsilon(0.0001));
    }
}

TEST_CASE("Float: default returned for missing key") {
    TempIni ini;
    float result = GetPrivateProfileFloat(L"NonExistent", L"missing", 42.0f, ini.c_str());
    CHECK(result == doctest::Approx(42.0f).epsilon(0.001));
}

// ============================================================================
// Tonemap Case-Insensitive Parsing
// ============================================================================

TEST_CASE("Tonemap: string to enum case-insensitive") {
    CHECK(StringToTonemapCurve(L"bt2390") == TonemapCurve::BT2390);
    CHECK(StringToTonemapCurve(L"softclip") == TonemapCurve::SoftClip);
    CHECK(StringToTonemapCurve(L"reinhard") == TonemapCurve::Reinhard);
    CHECK(StringToTonemapCurve(L"bt2446a") == TonemapCurve::BT2446A);
    CHECK(StringToTonemapCurve(L"hardclip") == TonemapCurve::HardClip);
}

// ============================================================================
// C Locale (locale-independent parsing)
// ============================================================================

TEST_CASE("GetCLocale: returns valid locale handle") {
    _locale_t loc = GetCLocale();
    CHECK(loc != nullptr);
    CHECK(GetCLocale() == loc);  // Same cached handle
}

TEST_CASE("GetCLocale: float parsing with C locale") {
    _locale_t loc = GetCLocale();
    wchar_t* end = nullptr;
    double result = _wcstod_l(L"0.6400", &end, loc);
    CHECK(result == doctest::Approx(0.64).epsilon(0.0001));
    CHECK(*end == L'\0');
}

TEST_CASE("GetCLocale: handles negative and zero values") {
    _locale_t loc = GetCLocale();
    wchar_t* end = nullptr;
    CHECK(_wcstod_l(L"0.0", &end, loc) == doctest::Approx(0.0).epsilon(0.0001));
    CHECK(_wcstod_l(L"-0.5", &end, loc) == doctest::Approx(-0.5).epsilon(0.0001));
    CHECK(_wcstod_l(L"999.123", &end, loc) == doctest::Approx(999.123).epsilon(0.001));
}

// ============================================================================
// Float Written Format
// ============================================================================

TEST_CASE("Float: written value uses decimal point (not comma)") {
    TempIni ini;
    WritePrivateProfileFloat(L"Test", L"val", 0.6400f, ini.c_str());
    wchar_t buf[64] = {};
    GetPrivateProfileStringW(L"Test", L"val", L"", buf, 64, ini.c_str());
    std::wstring raw(buf);
    CHECK(raw.find(L'.') != std::wstring::npos);
    CHECK(raw.find(L',') == std::wstring::npos);
}

// ============================================================================
// HDR CC Persistence
// ============================================================================

TEST_CASE("CC settings: HDR primaries/grayscale not persisted") {
    TempIni ini;
    ColorCorrectionSettings original;
    original.primariesEnabled = true;
    original.grayscale.enabled = true;
    original.grayscale.pointCount = 20;
    original.grayscale.initLinearPQ();
    original.tonemap.enabled = true;
    original.tonemap.curve = TonemapCurve::BT2390;

    SaveColorCorrectionSettings(L"TestMon", L"HDR_", original, ini.c_str());

    ColorCorrectionSettings loaded;
    LoadColorCorrectionSettings(L"TestMon", L"HDR_", loaded, ini.c_str());

    CHECK(loaded.tonemap.enabled == true);
    CHECK(loaded.tonemap.curve == TonemapCurve::BT2390);
    CHECK(loaded.primariesEnabled == false);
    CHECK(loaded.grayscale.enabled == false);
}

// ============================================================================
// MHC Settings Extended Round-Trips
// ============================================================================

TEST_CASE("MHC settings: grayscale points round-trip") {
    TempIni ini;
    MHCSettings original;
    original.enabled = true;
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();
    original.baseGrayscale.points[5] = 0.123f;
    original.baseGrayscale.points[10] = 0.456f;
    original.baseGrayscale.enabled = true;

    SaveMHCSettings(L"TestMon", L"SDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.baseGrayscale.enabled == true);
    CHECK(loaded.baseGrayscale.pointCount == 20);
    CHECK(loaded.baseGrayscale.points[5] == doctest::Approx(0.123f).epsilon(0.001));
    CHECK(loaded.baseGrayscale.points[10] == doctest::Approx(0.456f).epsilon(0.001));
}

TEST_CASE("MHC settings: white balance round-trip") {
    TempIni ini;
    MHCSettings original;
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinear();
    original.whiteBalanceEnabled = true;
    original.whiteBalanceWx = 0.3100f;
    original.whiteBalanceWy = 0.3200f;

    SaveMHCSettings(L"TestMon", L"SDR_", original, ini.c_str());

    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    CHECK(loaded.whiteBalanceEnabled == true);
    CHECK(loaded.whiteBalanceWx == doctest::Approx(0.3100f).epsilon(0.0001));
    CHECK(loaded.whiteBalanceWy == doctest::Approx(0.3200f).epsilon(0.0001));
}

// ============================================================================
// Per-channel RGB Deviations
// ============================================================================

TEST_CASE("CC settings: grayscale deviations not persisted (moved to MHC)") {
    TempIni ini;
    ColorCorrectionSettings original;
    original.grayscale.enabled = true;
    original.grayscale.pointCount = 20;
    original.grayscale.initLinear();
    original.grayscale.rgbDeviations[0][5] = 0.95f;

    SaveColorCorrectionSettings(L"TestMon", L"SDR_", original, ini.c_str());

    ColorCorrectionSettings loaded;
    LoadColorCorrectionSettings(L"TestMon", L"SDR_", loaded, ini.c_str());

    // Grayscale moved to MHC — deviations should be empty (not loaded)
    CHECK(loaded.grayscale.enabled == false);
    CHECK(loaded.grayscale.rgbDeviations[0].empty());
}

// ============================================================================
// Per-display sections: identity-keyed [Display<slot>] + legacy [Monitor<N>]
// ============================================================================

TEST_CASE("Sections: none saved yields an empty index list") {
    TempIni ini;
    WritePrivateProfileStringW(L"General", L"StartMinimized", L"true", ini.c_str());
    CHECK(EnumerateSavedSectionIndices(kDisplaySectionPrefix, ini.c_str()).empty());
    CHECK(EnumerateSavedSectionIndices(kLegacySectionPrefix, ini.c_str()).empty());
}

TEST_CASE("Sections: indices come back sorted, unique, per prefix") {
    TempIni ini;
    WritePrivateProfileStringW(L"General", L"StartMinimized", L"true", ini.c_str());
    WritePrivateProfileStringW(L"Monitor1", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Monitor0", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Display3", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Display0", L"LUT_SDR", L"", ini.c_str());

    auto legacy = EnumerateSavedSectionIndices(kLegacySectionPrefix, ini.c_str());
    REQUIRE(legacy.size() == 2);
    CHECK(legacy[0] == 0);
    CHECK(legacy[1] == 1);

    auto display = EnumerateSavedSectionIndices(kDisplaySectionPrefix, ini.c_str());
    REQUIRE(display.size() == 2);
    CHECK(display[0] == 0);
    CHECK(display[1] == 3);
}

TEST_CASE("Sections: only exact <prefix><digits> names count") {
    TempIni ini;
    WritePrivateProfileStringW(L"Monitor0", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Monitors", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"MonitorX", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Monitor 7", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Monitor-1", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Monitor2b", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"monitor5", L"LUT_SDR", L"", ini.c_str());  // case-sensitive
    WritePrivateProfileStringW(L"Display1", L"LUT_SDR", L"", ini.c_str());  // other prefix
    auto legacy = EnumerateSavedSectionIndices(kLegacySectionPrefix, ini.c_str());
    REQUIRE(legacy.size() == 1);
    CHECK(legacy[0] == 0);
}

TEST_CASE("Sections: out-of-range index is ignored, not clamped") {
    TempIni ini;
    WritePrivateProfileStringW(L"Display1", L"LUT_SDR", L"", ini.c_str());
    WritePrivateProfileStringW(L"Display99999", L"LUT_SDR", L"", ini.c_str());
    auto display = EnumerateSavedSectionIndices(kDisplaySectionPrefix, ini.c_str());
    REQUIRE(display.size() == 1);
    CHECK(display[0] == 1);
}

TEST_CASE("Monitor settings: section round-trip keeps LUTs, MaxTML, DG/WB/MHC") {
    TempIni ini;
    MonitorSettings original;
    original.hdrPath = L"C:\\luts\\mon1_hdr.cube";
    original.maxTml.enabled = true;
    original.maxTml.peakNits = 800.0f;
    original.hdrMHC.enabled = true;
    original.hdrMHC.profilePath = L"C:\\WINDOWS\\system32\\spool\\drivers\\color\\DesktopLUT_Mon1_HDR_1.icm";
    original.hdrMHC.desktopGammaEnabled = true;
    original.hdrMHC.whiteBalanceEnabled = true;
    original.hdrMHC.whiteBalanceWx = 0.3100f;
    original.hdrMHC.whiteBalanceWy = 0.3200f;
    original.hdrMHC.activePerm = MHCSettings::PERM_WB | MHCSettings::PERM_DG;
    original.hdrMHC.permPaths[original.hdrMHC.activePerm] = original.hdrMHC.profilePath;
    original.hdrMHC.baseGrayscale.pointCount = 20;
    original.hdrMHC.baseGrayscale.initLinear();
    original.sdrMHC.baseGrayscale.pointCount = 20;
    original.sdrMHC.baseGrayscale.initLinear();

    SaveMonitorSettings(L"Display7", original, ini.c_str());

    MonitorSettings ms;
    LoadMonitorSettings(L"Display7", ms, ini.c_str());
    CHECK(ms.sdrPath.empty());
    CHECK(ms.hdrPath == L"C:\\luts\\mon1_hdr.cube");
    CHECK(ms.maxTml.enabled == true);
    CHECK(ms.maxTml.peakNits == doctest::Approx(800.0f).epsilon(0.001));
    CHECK(ms.hdrMHC.enabled == true);
    CHECK(ms.hdrMHC.profileName == L"DesktopLUT_Mon1_HDR_1.icm");
    CHECK(ms.hdrMHC.desktopGammaEnabled == true);
    CHECK(ms.hdrMHC.whiteBalanceEnabled == true);
    CHECK(ms.hdrMHC.whiteBalanceWx == doctest::Approx(0.3100f).epsilon(0.001));
    CHECK(ms.hdrMHC.whiteBalanceWy == doctest::Approx(0.3200f).epsilon(0.001));
    CHECK(ms.hdrMHC.activePerm == (MHCSettings::PERM_WB | MHCSettings::PERM_DG));
    CHECK(ms.hdrMHC.permNames[ms.hdrMHC.activePerm] == L"DesktopLUT_Mon1_HDR_1.icm");
    CHECK(ms.sdrMHC.enabled == false);
    // Bookkeeping is not part of the per-monitor keys.
    CHECK(ms.slot == -1);
    CHECK(ms.legacyIndex == -1);
    CHECK(ms.identity.empty());

    // A section that does not exist is a clean default, not a partial copy of a neighbor.
    MonitorSettings none;
    LoadMonitorSettings(L"Display8", none, ini.c_str());
    CHECK(none.hdrPath.empty());
    CHECK(none.hdrMHC.enabled == false);
    CHECK(none.hdrMHC.desktopGammaEnabled == false);
}

TEST_CASE("Pool: loads identity-keyed displays and unclaimed legacy sections") {
    TempIni ini;
    WritePrivateProfileStringW(L"Display3", L"DevicePath", L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{guid}", ini.c_str());
    WritePrivateProfileStringW(L"Display3", L"EdidId", L"GSM84CD-16843009", ini.c_str());
    WritePrivateProfileStringW(L"Display3", L"DisplayName", L"LG TV SSCR2", ini.c_str());
    WritePrivateProfileStringW(L"Display3", L"LUT_SDR", L"C:\\luts\\lg.cube", ini.c_str());
    WritePrivateProfileStringW(L"Monitor0", L"LUT_SDR", L"C:\\luts\\mon0.cube", ini.c_str());
    WritePrivateProfileStringW(L"Monitor1", L"LUT_SDR", L"C:\\luts\\mon1.cube", ini.c_str());

    std::vector<MonitorSettings> pool;
    LoadMonitorSettingsPool(pool, ini.c_str());
    REQUIRE(pool.size() == 3);

    CHECK(pool[0].slot == 3);
    CHECK(pool[0].legacyIndex == -1);
    CHECK(pool[0].identity.devicePath == L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{guid}");
    CHECK(pool[0].identity.edidId == L"GSM84CD-16843009");
    CHECK(pool[0].identity.friendlyName == L"LG TV SSCR2");
    CHECK(pool[0].sdrPath == L"C:\\luts\\lg.cube");

    CHECK(pool[1].slot == -1);
    CHECK(pool[1].legacyIndex == 0);
    CHECK(pool[1].identity.empty());
    CHECK(pool[1].sdrPath == L"C:\\luts\\mon0.cube");

    CHECK(pool[2].legacyIndex == 1);
    CHECK(pool[2].sdrPath == L"C:\\luts\\mon1.cube");
}

TEST_CASE("Pool: a [Display] section without identity is kept, not discarded") {
    TempIni ini;
    WritePrivateProfileStringW(L"Display0", L"LUT_SDR", L"C:\\luts\\x.cube", ini.c_str());
    std::vector<MonitorSettings> pool;
    LoadMonitorSettingsPool(pool, ini.c_str());
    REQUIRE(pool.size() == 1);
    CHECK(pool[0].slot == 0);
    CHECK(pool[0].identity.empty());
    CHECK(pool[0].sdrPath == L"C:\\luts\\x.cube");
}

// Integration through the real INI path (next to the test executable). Only runs
// when no real INI is there, so a developer's config is never clobbered.
namespace {
struct AppIniGuard {
    std::wstring path = GetIniPath();
    bool usable = GetFileAttributesW(path.c_str()) == INVALID_FILE_ATTRIBUTES;
    ~AppIniGuard() { if (usable) _wremove(path.c_str()); }
};
DisplayIdentity AsusId() {
    DisplayIdentity d;
    d.devicePath = L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{guid}";
    d.edidId = L"AUS322A-S4LMSB007317";
    d.friendlyName = L"PA32UCXR";
    return d;
}
}  // namespace

TEST_CASE("LoadSettings/SaveSettings: [General] HdrDither defaults on and round-trips") {
    AppIniGuard guard;
    if (!guard.usable) { MESSAGE("skipped: an INI already exists next to the test executable"); return; }
    const wchar_t* ini = guard.path.c_str();
    g_gui.monitors.clear();
    g_gui.monitorSettings.clear();
    g_gui.parkedSettings.clear();
    WritePrivateProfileStringW(L"General", L"StartMinimized", L"false", ini);   // an INI without the key
    g_hdrDither = false;
    LoadSettings();
    CHECK(g_hdrDither.load() == true);                  // absent -> on (the new dither is the default)
    g_hdrDither = false;
    SaveSettings();
    wchar_t buf[16] = {};
    GetPrivateProfileStringW(L"General", L"HdrDither", L"", buf, 16, ini);
    CHECK(std::wstring(buf) == L"false");
    g_hdrDither = true;
    LoadSettings();
    CHECK(g_hdrDither.load() == false);                 // the kill switch survives a restart
    g_hdrDither = true;
}

TEST_CASE("LoadSettings/SaveSettings: legacy sections migrate to identity sections once matched") {
    AppIniGuard guard;
    if (!guard.usable) { MESSAGE("skipped: an INI already exists next to the test executable"); return; }
    const wchar_t* ini = guard.path.c_str();

    WritePrivateProfileStringW(L"Monitor0", L"LUT_SDR", L"C:\\luts\\asus.cube", ini);
    {
        MHCSettings hdr;
        hdr.enabled = true;
        hdr.profilePath = L"C:\\color\\DesktopLUT_Mon1_HDR_2.icm";
        hdr.desktopGammaEnabled = true;
        hdr.whiteBalanceEnabled = true;
        hdr.baseGrayscale.pointCount = 20;
        hdr.baseGrayscale.initLinear();
        SaveMHCSettings(L"Monitor1", L"HDR_", hdr, ini);
    }

    // No live monitors in the test process: everything loads parked, nothing attaches.
    g_gui.monitors.clear();
    g_gui.monitorSettings.clear();
    g_gui.parkedSettings.clear();
    LoadSettings();
    CHECK(g_gui.monitorSettings.empty());
    REQUIRE(g_gui.parkedSettings.size() == 2);
    CHECK(g_gui.parkedSettings[0].legacyIndex == 0);
    CHECK(g_gui.parkedSettings[1].legacyIndex == 1);
    CHECK(g_gui.parkedSettings[1].hdrMHC.profileName == L"DesktopLUT_Mon1_HDR_2.icm");
    // Nothing is live, so a parked display's DG must not switch the global mode on.
    CHECK(g_userDesktopGammaMode.load() == false);

    // Saving with nothing claimed leaves the legacy sections untouched and writes no Display section.
    SaveSettings();
    CHECK(EnumerateSavedSectionIndices(kDisplaySectionPrefix, ini).empty());
    REQUIRE(EnumerateSavedSectionIndices(kLegacySectionPrefix, ini).size() == 2);

    // The ASUS shows up at index 0: it adopts [Monitor0]; the LG's [Monitor1] stays parked.
    LiveDisplay asus;
    asus.hmon = (HMONITOR)1;
    asus.identity = AsusId();
    asus.identified = true;
    MonitorMatchResult r = MatchMonitorSettings({ asus }, g_gui.monitorSettings, g_gui.parkedSettings);
    g_gui.monitorSettings = r.live;
    g_gui.parkedSettings = r.parked;
    REQUIRE(g_gui.monitorSettings.size() == 1);
    CHECK(g_gui.monitorSettings[0].sdrPath == L"C:\\luts\\asus.cube");
    CHECK(g_gui.monitorSettings[0].legacyIndex == 0);
    CHECK(g_gui.monitorSettings[0].slot == 0);

    SaveSettings();
    auto display = EnumerateSavedSectionIndices(kDisplaySectionPrefix, ini);
    REQUIRE(display.size() == 1);
    CHECK(display[0] == 0);
    auto legacy = EnumerateSavedSectionIndices(kLegacySectionPrefix, ini);
    REQUIRE(legacy.size() == 1);
    CHECK(legacy[0] == 1);   // [Monitor0] retired, [Monitor1] still waiting for the LG
    CHECK(g_gui.monitorSettings[0].legacyIndex == -1);

    wchar_t buf[512] = {};
    GetPrivateProfileStringW(L"Display0", L"EdidId", L"", buf, 512, ini);
    CHECK(std::wstring(buf) == L"AUS322A-S4LMSB007317");
    GetPrivateProfileStringW(L"Display0", L"DevicePath", L"", buf, 512, ini);
    CHECK(std::wstring(buf) == L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353#{guid}");
    GetPrivateProfileStringW(L"Display0", L"LUT_SDR", L"", buf, 512, ini);
    CHECK(std::wstring(buf) == L"C:\\luts\\asus.cube");

    // Reload from disk: the identity section and the unclaimed legacy one both come back.
    g_gui.monitors.clear();
    g_gui.monitorSettings.clear();
    g_gui.parkedSettings.clear();
    LoadSettings();
    REQUIRE(g_gui.parkedSettings.size() == 2);
    CHECK(g_gui.parkedSettings[0].slot == 0);
    CHECK(g_gui.parkedSettings[0].identity.edidId == L"AUS322A-S4LMSB007317");
    CHECK(g_gui.parkedSettings[0].sdrPath == L"C:\\luts\\asus.cube");
    CHECK(g_gui.parkedSettings[1].legacyIndex == 1);
    CHECK(g_gui.parkedSettings[1].hdrMHC.desktopGammaEnabled == true);

    g_gui.monitorSettings.clear();
    g_gui.parkedSettings.clear();
}

TEST_CASE("SaveSettings: parked displays are written, anonymous entries are not") {
    AppIniGuard guard;
    if (!guard.usable) { MESSAGE("skipped: an INI already exists next to the test executable"); return; }
    const wchar_t* ini = guard.path.c_str();

    MonitorSettings parkedLg;
    parkedLg.identity.devicePath = L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{guid}";
    parkedLg.identity.edidId = L"GSM84CD-16843009";
    parkedLg.identity.friendlyName = L"LG TV SSCR2";
    parkedLg.slot = 4;
    parkedLg.hdrMHC.whiteBalanceEnabled = true;
    parkedLg.hdrMHC.baseGrayscale.pointCount = 20;
    parkedLg.hdrMHC.baseGrayscale.initLinear();
    parkedLg.sdrMHC.baseGrayscale.pointCount = 20;
    parkedLg.sdrMHC.baseGrayscale.initLinear();

    MonitorSettings anonymous;   // identity query never succeeded for this live display
    anonymous.sdrPath = L"C:\\luts\\unknown.cube";

    g_gui.monitors.clear();
    g_gui.monitorSettings = { anonymous };
    g_gui.parkedSettings = { parkedLg };
    SaveSettings();

    auto display = EnumerateSavedSectionIndices(kDisplaySectionPrefix, ini);
    REQUIRE(display.size() == 1);
    CHECK(display[0] == 4);
    wchar_t buf[64] = {};
    GetPrivateProfileStringW(L"Display4", L"HDR_MHCWhiteBalanceEnabled", L"", buf, 64, ini);
    CHECK(std::wstring(buf) == L"true");

    g_gui.monitorSettings.clear();
    g_gui.parkedSettings.clear();
}

