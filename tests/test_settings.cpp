#include "doctest.h"
#include "settings.h"
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
    original.grayscale.pointCount = 20;
    original.grayscale.initLinear();

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
    original.grayscale.pointCount = 20;
    original.grayscale.initLinear();

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
    original.grayscale.pointCount = 20;
    original.grayscale.initLinear();

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
    original.grayscale.pointCount = 20;
    original.grayscale.initLinear();

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
    original.grayscale.pointCount = 20;
    original.grayscale.initLinear();

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
