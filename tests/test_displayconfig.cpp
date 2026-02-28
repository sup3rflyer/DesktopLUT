#include "doctest.h"
#include "displayconfig.h"
#include "fixtures/edid_samples.h"
#include <cmath>

// ============================================================================
// EDID Chromaticity Parsing
// ============================================================================

TEST_CASE("EDID: sRGB monitor primaries") {
    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(kEDID_sRGB, sizeof(kEDID_sRGB), p);
    REQUIRE(ok);
    CHECK(p.valid);
    // Exact 10-bit EDID decoded values (see edid_samples.h for bit packing)
    CHECK(p.Rx == doctest::Approx(655.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Ry == doctest::Approx(338.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Gx == doctest::Approx(307.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Gy == doctest::Approx(614.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Bx == doctest::Approx(154.0f / 1024.0f).epsilon(0.001));
    CHECK(p.By == doctest::Approx(61.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Wx == doctest::Approx(320.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Wy == doctest::Approx(337.0f / 1024.0f).epsilon(0.001));
}

TEST_CASE("EDID: wide-gamut P3 monitor") {
    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(kEDID_P3, sizeof(kEDID_P3), p);
    REQUIRE(ok);
    CHECK(p.valid);
    // 10-bit EDID values (see edid_samples.h for bit packing)
    CHECK(p.Rx == doctest::Approx(696.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Ry == doctest::Approx(328.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Gx == doctest::Approx(271.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Gy == doctest::Approx(707.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Bx == doctest::Approx(154.0f / 1024.0f).epsilon(0.001));
    CHECK(p.By == doctest::Approx(61.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Wx == doctest::Approx(320.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Wy == doctest::Approx(337.0f / 1024.0f).epsilon(0.001));
}

TEST_CASE("EDID: too short") {
    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(kEDID_TooShort, sizeof(kEDID_TooShort), p);
    CHECK_FALSE(ok);
}

TEST_CASE("EDID: bad header") {
    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(kEDID_BadHeader, sizeof(kEDID_BadHeader), p);
    CHECK_FALSE(ok);
}

TEST_CASE("EDID: exact minimum size (35 bytes)") {
    // 35 bytes is the minimum for chromaticity data
    uint8_t edid[35];
    memcpy(edid, kEDID_sRGB, 35);
    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(edid, 35, p);
    CHECK(ok);
    CHECK(p.valid);
}

TEST_CASE("EDID: zero chromaticity values") {
    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(kEDID_ZeroChromaticity, sizeof(kEDID_ZeroChromaticity), p);
    CHECK(ok);
    CHECK(p.Rx == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.Ry == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.Gx == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.Gy == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.Bx == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.By == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.Wx == doctest::Approx(0.0f).epsilon(0.001));
    CHECK(p.Wy == doctest::Approx(0.0f).epsilon(0.001));
}

// ============================================================================
// Hardware ID Extraction
// ============================================================================

TEST_CASE("HardwareID: standard format") {
    std::wstring path = L"\\\\?\\DISPLAY#DELA1EE#5&2a3b4c5d&0&UID12345#{guid}";
    CHECK(ExtractHardwareIdFromPath(path) == L"DELA1EE");
}

TEST_CASE("HardwareID: no DISPLAY#") {
    std::wstring path = L"\\\\?\\MONITOR#ABC#foo";
    CHECK(ExtractHardwareIdFromPath(path) == L"");
}

TEST_CASE("HardwareID: no second #") {
    std::wstring path = L"\\\\?\\DISPLAY#DELA1EE";
    CHECK(ExtractHardwareIdFromPath(path) == L"");
}

TEST_CASE("HardwareID: empty string") {
    CHECK(ExtractHardwareIdFromPath(L"") == L"");
}
