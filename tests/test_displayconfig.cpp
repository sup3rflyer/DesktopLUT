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

// ============================================================================
// EDID Edge Cases
// ============================================================================

TEST_CASE("EDID: maximum chromaticity values (all bits set)") {
    uint8_t edid[128] = {};
    edid[0] = 0x00; edid[1] = 0xFF; edid[2] = 0xFF; edid[3] = 0xFF;
    edid[4] = 0xFF; edid[5] = 0xFF; edid[6] = 0xFF; edid[7] = 0x00;
    edid[25] = 0xFF;
    edid[26] = 0xFF;
    for (int i = 27; i <= 34; i++) edid[i] = 0xFF;

    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(edid, 128, p);
    CHECK(ok);
    CHECK(p.valid);
    // 10-bit value = (255 << 2) | 3 = 1023
    CHECK(p.Rx == doctest::Approx(1023.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Ry == doctest::Approx(1023.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Wx == doctest::Approx(1023.0f / 1024.0f).epsilon(0.001));
}

TEST_CASE("EDID: LSBs only (MSBs zero)") {
    uint8_t edid[128] = {};
    edid[0] = 0x00; edid[1] = 0xFF; edid[2] = 0xFF; edid[3] = 0xFF;
    edid[4] = 0xFF; edid[5] = 0xFF; edid[6] = 0xFF; edid[7] = 0x00;
    // Byte 25: Rx[1:0]=11 Ry[1:0]=10 Gx[1:0]=01 Gy[1:0]=00 = 0xE4
    edid[25] = 0xE4;
    edid[26] = 0x00;
    for (int i = 27; i <= 34; i++) edid[i] = 0x00;

    MonitorPrimaries p = {};
    bool ok = ParseEDIDChromaticity(edid, 128, p);
    CHECK(ok);
    CHECK(p.Rx == doctest::Approx(3.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Ry == doctest::Approx(2.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Gx == doctest::Approx(1.0f / 1024.0f).epsilon(0.001));
    CHECK(p.Gy == doctest::Approx(0.0f).epsilon(0.001));
}

// ============================================================================
// DWM Hook IPC Struct Layout
// ============================================================================

#include "../shared/dwm_hook_config.h"

TEST_CASE("DwmHookMonitorConfig: size and alignment") {
    CHECK(sizeof(DwmHookMonitorConfig) == 48);
    DwmHookMonitorConfig cfg = {};
    auto base = reinterpret_cast<uintptr_t>(&cfg);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.left) - base) == 0);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.top) - base) == 4);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.width) - base) == 8);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.height) - base) == 12);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.bpc) - base) == 16);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.isHdr) - base) == 20);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.tonemapEnabled) - base) == 24);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.tonemapCurve) - base) == 28);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.sourcePeakNits) - base) == 32);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.targetPeakNits) - base) == 36);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.dynamicPeak) - base) == 40);
}

TEST_CASE("DwmHookSharedConfig: size and layout") {
    CHECK(sizeof(DwmHookSharedConfig) == 464);
    DwmHookSharedConfig cfg = {};
    auto base = reinterpret_cast<uintptr_t>(&cfg);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.version) - base) == 0);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.numMonitors) - base) == 4);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.hostPid) - base) == 8);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.lutReloadFlag) - base) == 12);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.monitors[0]) - base) == 16);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.monitors[1]) - base) == 16 + 48);
}

TEST_CASE("DwmHookTonemapCurve: enum values match host app") {
    CHECK(DWMHOOK_TONEMAP_BT2390 == 0);
    CHECK(DWMHOOK_TONEMAP_SOFTCLIP == 1);
    CHECK(DWMHOOK_TONEMAP_REINHARD == 2);
    CHECK(DWMHOOK_TONEMAP_BT2446A == 3);
    CHECK(DWMHOOK_TONEMAP_HARDCLIP == 4);
}

TEST_CASE("DwmHookSharedConfig: zero-initialized is safe default") {
    DwmHookSharedConfig cfg = {};
    CHECK(cfg.version == 0);
    CHECK(cfg.numMonitors == 0);
    CHECK(cfg.hostPid == 0);
    CHECK(cfg.lutReloadFlag == 0);
    for (int i = 0; i < MAX_DWM_HOOK_MONITORS; i++) {
        CHECK(cfg.monitors[i].tonemapEnabled == 0);
        CHECK(cfg.monitors[i].isHdr == 0);
        CHECK(cfg.monitors[i].sourcePeakNits == 0.0f);
    }
}

// ============================================================================
// Live-preview mode gate (EnsureProcessingForPreview decision seam)
// ============================================================================
// Regression: after Windows toggled HDR off while DesktopLUT ran with the
// overlay auto-slept (DWM hook mode), MonitorContext::isHDREnabled stayed at the
// pre-toggle mode — the slept render loop never pumps AcquireNextFrame, so it
// never sees the mode change. The gate then rejected grayscale_live_begin for
// the monitor's ACTUAL mode (SDR) and wrongly accepted the STALE mode (HDR),
// while windows.query_monitors (fresh DXGI factory query) reported the truth.
// The gate now consults the same fresh query; this truth table pins it.
#include "gui_mhc.h"

TEST_CASE("PreviewModeGate: fresh query agrees with context — Ready") {
    // SDR request on an SDR monitor with an SDR context
    CHECK(EvaluatePreviewModeGate(false, false, true, false) == PreviewModeGate::Ready);
    // HDR request on an HDR monitor with an HDR context
    CHECK(EvaluatePreviewModeGate(true, true, true, true) == PreviewModeGate::Ready);
}

TEST_CASE("PreviewModeGate: monitor genuinely in the other mode — Mismatch") {
    // HDR request but monitor actually SDR (context agrees with OS)
    CHECK(EvaluatePreviewModeGate(true, false, true, false) == PreviewModeGate::Mismatch);
    // SDR request but monitor actually HDR (context agrees with OS)
    CHECK(EvaluatePreviewModeGate(false, true, true, true) == PreviewModeGate::Mismatch);
}

TEST_CASE("PreviewModeGate: context slept through an HDR toggle — StaleCtx") {
    // The 2026-08-14 repro: HDR toggled off, OS says SDR, context still says HDR.
    // SDR request must NOT be rejected — it needs a resync, not a failure.
    CHECK(EvaluatePreviewModeGate(false, true, true, false) == PreviewModeGate::StaleCtx);
    // Inverse flip (HDR toggled on while asleep): HDR request resyncs too.
    CHECK(EvaluatePreviewModeGate(true, false, true, true) == PreviewModeGate::StaleCtx);
}

TEST_CASE("PreviewModeGate: stale context must not false-pass the old mode") {
    // Same repro, other direction: an HDR begin against the stale HDR context
    // used to SUCCEED even though the monitor was already SDR. The fresh query
    // must veto it.
    CHECK(EvaluatePreviewModeGate(true, true, true, false) == PreviewModeGate::Mismatch);
    CHECK(EvaluatePreviewModeGate(false, false, true, true) == PreviewModeGate::Mismatch);
}

TEST_CASE("PreviewModeGate: fresh query unavailable — fall back to cached mode") {
    // freshHDR is meaningless when freshQueryOk=false; both values must not matter.
    for (bool junk : {false, true}) {
        CHECK(EvaluatePreviewModeGate(false, false, false, junk) == PreviewModeGate::Ready);
        CHECK(EvaluatePreviewModeGate(true, true, false, junk) == PreviewModeGate::Ready);
        CHECK(EvaluatePreviewModeGate(true, false, false, junk) == PreviewModeGate::Mismatch);
        CHECK(EvaluatePreviewModeGate(false, true, false, junk) == PreviewModeGate::Mismatch);
    }
}
