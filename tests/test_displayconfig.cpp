#include "doctest.h"
#include "displayconfig.h"
#include "capture.h"
#include "display_power.h"
#include "windows_tonemap.h"
#include "fixtures/edid_samples.h"
#include <cmath>
#include <string>

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
    CHECK((reinterpret_cast<uintptr_t>(&cfg.beaconColorId) - base) == 44);
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
    CHECK((reinterpret_cast<uintptr_t>(&cfg.beaconActive) - base) == 16 + 48 * MAX_DWM_HOOK_MONITORS);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.beaconGeneration) - base) == 16 + 48 * MAX_DWM_HOOK_MONITORS + 4);
    CHECK((reinterpret_cast<uintptr_t>(&cfg.beaconSize) - base) == 16 + 48 * MAX_DWM_HOOK_MONITORS + 8);
}

TEST_CASE("DwmHookSharedConfigEx: the FALD tuning tail sits after the frozen 464-byte head") {
    // An older DwmHook.dll maps exactly the head; everything it reads must stay where it was.
    DwmHookSharedConfigEx ex = {};
    auto base = reinterpret_cast<uintptr_t>(&ex);
    CHECK((reinterpret_cast<uintptr_t>(&ex.head.version) - base) == 0);
    CHECK((reinterpret_cast<uintptr_t>(&ex.head.faldFlags[0]) - base) == 16 + 48 * MAX_DWM_HOOK_MONITORS + 12);
    CHECK((reinterpret_cast<uintptr_t>(&ex.tail) - base) == 464);
    // the HDR dither kill switch came out of the head's _reserved: first of the five words (0 = dither on)
    CHECK(offsetof(DwmHookSharedConfig, hdrDitherOff) == 464 - 5 * 4);
    // the 3D LUT stage bypass mask came out of _reserved next (0 = every cube applied)
    CHECK(offsetof(DwmHookSharedConfig, lutOffMask) == 464 - 4 * 4);
    // the live edit preview generation came out of _reserved next (0 = no preview)
    CHECK(offsetof(DwmHookSharedConfig, mhcPreviewGen) == 464 - 3 * 4);
    CHECK(offsetof(DwmHookSharedConfig, _reserved) == 464 - 2 * 4);
    CHECK((reinterpret_cast<uintptr_t>(&ex.tail.fald[0]) - base) == 464 + 16);
    CHECK((reinterpret_cast<uintptr_t>(&ex.tail.fald[1]) - base) == 464 + 16 + 128);
    CHECK(sizeof(DwmHookFaldTuning) == 128);
    // LED-lag fields came out of _reserved: their offsets are wire contract too
    CHECK(offsetof(DwmHookFaldTuning, _retiredGlow) == 56);   // the glow fill's three words (removed 2026-10-07): kept
    CHECK(offsetof(DwmHookFaldTuning, tempMode) == 68);
    CHECK(offsetof(DwmHookFaldTuning, tempClockParity) == 88);
    CHECK(offsetof(DwmHookFaldTuning, refreshMs) == 92);
    CHECK(offsetof(DwmHookFaldTuning, _reserved) == 96);
    CHECK(sizeof(DwmHookSharedConfigEx) == 464 + 16 + 128 * MAX_DWM_HOOK_MONITORS);
}

TEST_CASE("DwmHookFaldPack: the starfield bit") {
    // The pre-merge words are unchanged (a DLL that predates bit 6 ignores it).
    const uint32_t base = DwmHookFaldPack(1, 4, 1);
    CHECK(DwmHookFaldEnabled(base) == 1);
    CHECK(DwmHookFaldDebugMode(base) == 4u);
    CHECK(DwmHookFaldPedMode(base) == 1);
    CHECK(DwmHookFaldStar(base) == 0);
    const uint32_t star = DwmHookFaldPack(1, 0, 0, 1);
    CHECK(DwmHookFaldStar(star) == 1);
    // Bit 7 (the glow fill's, removed 2026-10-07) is never packed: an already-resident older DLL keeps the fill off.
    CHECK((star & 0x00000080u) == 0u);
    // The starfield bit stays clear of the enable / debug / pedestal fields.
    CHECK((DWM_HOOK_FALD_STAR_BIT & (DWM_HOOK_FALD_ENABLED_BIT | DWM_HOOK_FALD_DEBUG_MASK | DWM_HOOK_FALD_PEDMODE_BIT)) == 0u);
}

TEST_CASE("DwmHookBeacon: palette and classifier agree across encodings") {
    // Monitor index -> colour id wraps over the six-colour palette, never 0.
    CHECK(DwmHookBeaconColorIdForMonitor(0) == 1);
    CHECK(DwmHookBeaconColorIdForMonitor(5) == 6);
    CHECK(DwmHookBeaconColorIdForMonitor(6) == 1);
    // Every palette colour round-trips through the classifier at any positive scale
    // (8-bit normalised, 10-bit normalised, scRGB FP16 with SDR white-level gain).
    for (uint32_t id = 1; id <= DWM_HOOK_BEACON_COLORS; id++) {
        int r, g, b;
        DwmHookBeaconRGB(id, &r, &g, &b);
        CHECK((r | g | b) != 0);
        for (float scale : { 1.0f, 0.5f, 3.75f, 12.5f }) {
            CHECK(DwmHookBeaconClassify(r * scale, g * scale, b * scale, 0.15f) == id);
            // A little leakage into the "off" channels (composition rounding) is tolerated.
            CHECK(DwmHookBeaconClassify(r * scale + 0.1f * scale, g * scale + 0.1f * scale, b * scale + 0.1f * scale, 0.15f) == id);
        }
    }
    // Non-beacon content is rejected: black, white, grey, a pastel, a dim sample, NaN.
    CHECK(DwmHookBeaconClassify(0.0f, 0.0f, 0.0f, 0.15f) == 0);
    CHECK(DwmHookBeaconClassify(1.0f, 1.0f, 1.0f, 0.15f) == 0);
    CHECK(DwmHookBeaconClassify(0.5f, 0.5f, 0.5f, 0.15f) == 0);
    CHECK(DwmHookBeaconClassify(1.0f, 0.5f, 0.5f, 0.15f) == 0);
    CHECK(DwmHookBeaconClassify(0.1f, 0.0f, 0.0f, 0.15f) == 0);
    CHECK(DwmHookBeaconClassify(std::nanf(""), 0.0f, 0.0f, 0.15f) == 0);
    // Id 0 / out-of-range ids paint nothing.
    int r = 1, g = 1, b = 1;
    DwmHookBeaconRGB(0, &r, &g, &b);
    CHECK((r | g | b) == 0);
    DwmHookBeaconRGB(99, &r, &g, &b);
    CHECK((r | g | b) == 0);
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
// Display colour mode (ACM detection, DLC work guide C8)
// ============================================================================

TEST_CASE("DisplayColorMode: DXGI HDR verdict always wins") {
    auto r = ClassifyDisplayColorMode(true, false, true, 1u, true, true);   // DisplayConfig says WCG, DXGI says HDR
    CHECK(r.mode == DisplayColorMode::HDR);
    CHECK(std::string(r.source) == "dxgi");
    CHECK(std::string(DisplayColorModeName(r.mode)) == "HDR");
}

TEST_CASE("DisplayColorMode: 24H2 activeColorMode classifies SDR vs ACM (the DXGI colour space cannot)") {
    // ACM on (owner's machine 2026-09-14): DXGI still says G22_P709, DisplayConfig 2 says WCG
    auto acm = ClassifyDisplayColorMode(false, false, true, 1u, true, true);
    CHECK(acm.mode == DisplayColorMode::AcmSdr);
    CHECK(std::string(acm.source) == "displayconfig2");
    CHECK(std::string(DisplayColorModeName(acm.mode)) == "ACM_SDR");
    // ACM off: plain SDR
    auto sdr = ClassifyDisplayColorMode(false, false, true, 0u, true, false);
    CHECK(sdr.mode == DisplayColorMode::SDR);
    CHECK(std::string(DisplayColorModeName(sdr.mode)) == "SDR");
    // DisplayConfig 2 reports HDR while the DXGI query missed it: trust DisplayConfig
    auto hdr = ClassifyDisplayColorMode(false, false, true, 2u, false, false);
    CHECK(hdr.mode == DisplayColorMode::HDR);
}

TEST_CASE("DisplayLinkFormat: encoding + connector names (query_monitors link_* fields)") {
    CHECK(std::string(DisplayColorEncodingName(0)) == "RGB");
    CHECK(std::string(DisplayColorEncodingName(3)) == "YCBCR420");
    CHECK(std::string(DisplayColorEncodingName(99)) == "UNKNOWN");
    CHECK(std::string(DisplayOutputTechnologyName(5)) == "HDMI");
    CHECK(std::string(DisplayOutputTechnologyName(10)) == "DISPLAYPORT_EXTERNAL");
    CHECK(std::string(DisplayOutputTechnologyName(0x80000000u)) == "INTERNAL");
    CHECK(std::string(DisplayOutputTechnologyName(7)) == "UNKNOWN");
}

TEST_CASE("DisplayColorMode: an activeColorMode value this build does not know falls back to the legacy query") {
    auto r = ClassifyDisplayColorMode(false, false, true, 7u, true, true);
    CHECK(r.mode == DisplayColorMode::AcmSdr);
    CHECK(std::string(r.source) == "displayconfig");
    auto d = ClassifyDisplayColorMode(false, false, true, 7u, false, false);
    CHECK(std::string(d.source) == "dxgi");
}

TEST_CASE("Swapchain rebuild follows SDR <-> ACM SDR as well as SDR <-> HDR") {
    CHECK_FALSE(SwapchainModeChanged(false, false, false, false));   // plain SDR, unchanged
    CHECK(SwapchainModeChanged(false, false, false, true));          // ACM switched on at runtime (the 2026-09-14 audit)
    CHECK(SwapchainModeChanged(false, true, false, false));          // ACM switched off
    CHECK(SwapchainModeChanged(false, true, true, false));           // ACM SDR -> HDR
    CHECK(SwapchainModeChanged(true, false, false, false));          // HDR -> plain SDR
    CHECK_FALSE(SwapchainModeChanged(true, false, true, false));      // HDR, unchanged
}

TEST_CASE("DisplayColorMode: pre-24H2 fallback = advancedColorEnabled && !HDR") {
    auto acm = ClassifyDisplayColorMode(false, false, false, 0u, true, true);
    CHECK(acm.mode == DisplayColorMode::AcmSdr);
    CHECK(std::string(acm.source) == "displayconfig");
    auto sdr = ClassifyDisplayColorMode(false, false, false, 0u, true, false);
    CHECK(sdr.mode == DisplayColorMode::SDR);
}

TEST_CASE("DisplayColorMode: no DisplayConfig answer at all falls back to the DXGI colour space") {
    auto sdr = ClassifyDisplayColorMode(false, false, false, 0u, false, false);
    CHECK(sdr.mode == DisplayColorMode::SDR);
    CHECK(std::string(sdr.source) == "dxgi");
    auto fp16 = ClassifyDisplayColorMode(false, true, false, 0u, false, false);   // a driver that does report G10_P709
    CHECK(fp16.mode == DisplayColorMode::AcmSdr);
    CHECK(std::string(DisplayColorModeName(DisplayColorMode::Unknown)) == "UNKNOWN");
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

// ============================================================================
// Display identity: EDID serial + device path parsing
// ============================================================================

namespace {
// A valid-header EDID with an optional ID serial (bytes 12-15) and an optional
// ASCII serial descriptor (tag 0xFF) in the first descriptor block at byte 54.
struct EdidBuilder {
    uint8_t bytes[128] = {};
    EdidBuilder() {
        const uint8_t hdr[8] = { 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00 };
        memcpy(bytes, hdr, 8);
    }
    EdidBuilder& idSerial(uint32_t s) {
        bytes[12] = (uint8_t)(s & 0xFF);
        bytes[13] = (uint8_t)((s >> 8) & 0xFF);
        bytes[14] = (uint8_t)((s >> 16) & 0xFF);
        bytes[15] = (uint8_t)((s >> 24) & 0xFF);
        return *this;
    }
    EdidBuilder& asciiSerial(const char* s, int block = 0) {
        uint8_t* d = bytes + 54 + 18 * block;
        d[0] = d[1] = d[2] = 0; d[3] = 0xFF; d[4] = 0;
        int k = 5;
        for (; k < 18 && *s; k++, s++) d[k] = (uint8_t)*s;
        if (k < 18) d[k++] = 0x0A;
        for (; k < 18; k++) d[k] = 0x20;
        return *this;
    }
};
}  // namespace

TEST_CASE("EDID serial: ASCII serial descriptor wins over the ID serial") {
    EdidBuilder e; e.idSerial(16843009).asciiSerial("S4LMSB007317");
    std::wstring s;
    REQUIRE(ParseEDIDSerial(e.bytes, 128, s));
    CHECK(s == L"S4LMSB007317");
}

TEST_CASE("EDID serial: descriptor may sit in any of the four blocks") {
    EdidBuilder e; e.asciiSerial("ET99K01234", 2);
    std::wstring s;
    REQUIRE(ParseEDIDSerial(e.bytes, 128, s));
    CHECK(s == L"ET99K01234");
}

TEST_CASE("EDID serial: 13-char serial with no terminator, padding trimmed") {
    EdidBuilder e; e.asciiSerial("ABCDEFGHIJKLM");   // fills all 13 payload bytes
    std::wstring s;
    REQUIRE(ParseEDIDSerial(e.bytes, 128, s));
    CHECK(s == L"ABCDEFGHIJKLM");

    EdidBuilder p; p.asciiSerial("AB  ");
    REQUIRE(ParseEDIDSerial(p.bytes, 128, s));
    CHECK(s == L"AB");
}

TEST_CASE("EDID serial: falls back to the numeric ID serial") {
    EdidBuilder e; e.idSerial(16843009);   // 0x01010101 — LG's well-known placeholder
    std::wstring s;
    REQUIRE(ParseEDIDSerial(e.bytes, 128, s));
    CHECK(s == L"16843009");
}

TEST_CASE("EDID serial: none present, bad header, too short") {
    EdidBuilder e;
    std::wstring s = L"stale";
    CHECK_FALSE(ParseEDIDSerial(e.bytes, 128, s));
    CHECK(s.empty());

    EdidBuilder bad; bad.idSerial(7); bad.bytes[1] = 0x00;
    CHECK_FALSE(ParseEDIDSerial(bad.bytes, 128, s));

    EdidBuilder shortE; shortE.idSerial(7);
    CHECK_FALSE(ParseEDIDSerial(shortE.bytes, 100, s));
    CHECK_FALSE(ParseEDIDSerial(nullptr, 128, s));
}

TEST_CASE("EDID serial: control and non-ASCII bytes are never emitted") {
    EdidBuilder e; e.asciiSerial("AB");
    e.bytes[54 + 5 + 2] = 0x01;   // control byte inside the payload, before the terminator
    e.bytes[54 + 5 + 3] = 0xC3;   // high byte
    e.bytes[54 + 5 + 4] = 'C';
    e.bytes[54 + 5 + 5] = 0x0A;
    std::wstring s;
    REQUIRE(ParseEDIDSerial(e.bytes, 128, s));
    CHECK(s == L"ABC");
}

TEST_CASE("Device instance id from a monitor device path") {
    CHECK(DeviceInstanceIdFromPath(L"\\\\?\\DISPLAY#GSM84CD#5&14ca04b&2&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}")
          == L"DISPLAY\\GSM84CD\\5&14ca04b&2&UID4352");
    CHECK(DeviceInstanceIdFromPath(L"\\\\?\\DISPLAY#AUS322A#5&14ca04b&2&UID4353") == L"DISPLAY\\AUS322A\\5&14ca04b&2&UID4353");
    CHECK(DeviceInstanceIdFromPath(L"\\\\?\\DISPLAY#AUS322A#").empty());
    CHECK(DeviceInstanceIdFromPath(L"\\\\?\\DISPLAY##abc#{g}").empty());
    CHECK(DeviceInstanceIdFromPath(L"\\\\?\\PCI#VEN_10DE").empty());
    CHECK(DeviceInstanceIdFromPath(L"").empty());
}

TEST_CASE("Night Light: the CloudStore state blob reads on / off / unknown") {
    // Captured on Windows 11 25H2 with Night Light off (2026-10-13): byte 18 = 0x13.
    const unsigned char off[] = { 0x43, 0x42, 0x01, 0x00, 0x0A, 0x02, 0x01, 0x00, 0x2A, 0x06, 0xF8, 0xF3, 0x8F, 0xD5,
                                  0x06, 0x2A, 0x2B, 0x0E, 0x13, 0x43, 0x42, 0x01, 0x00, 0xD0, 0x0A, 0x02 };
    CHECK(ParseNightLightState(off, sizeof(off)) == 0);
    unsigned char on[sizeof(off)];
    memcpy(on, off, sizeof(off));
    on[18] = 0x15;
    CHECK(ParseNightLightState(on, sizeof(on)) == 1);
    on[18] = 0x99;
    CHECK(ParseNightLightState(on, sizeof(on)) == -1);       // a format we do not know: no claim
    CHECK(ParseNightLightState(off, 18) == -1);               // too short
    CHECK(ParseNightLightState(nullptr, 0) == -1);
    unsigned char bad[sizeof(off)];
    memcpy(bad, off, sizeof(off));
    bad[0] = 0;
    CHECK(ParseNightLightState(bad, sizeof(bad)) == -1);      // not the CloudStore header
}

// The state Windows sends right after the registration is not a wake (2026-10-14: every startup logged
// "Display waking from sleep", ran an MHC burst and latched a forced reinit + MHC reapply).
TEST_CASE("DisplayPowerTracker: only on-after-off is a wake") {
    SUBCASE("startup: the initial 'on' and repeats are nothing") {
        DisplayPowerTracker t;
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
    }
    SUBCASE("off then on is a wake, every off is reported") {
        DisplayPowerTracker t;
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
        CHECK(t.OnState(0) == DisplayPowerEvent::Off);
        CHECK(t.OnState(0) == DisplayPowerEvent::Off);
        CHECK(t.OnState(1) == DisplayPowerEvent::Wake);
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
    }
    SUBCASE("registered while the display is off: its first 'on' is a wake") {
        DisplayPowerTracker t;
        CHECK(t.OnState(0) == DisplayPowerEvent::Off);
        CHECK(t.OnState(1) == DisplayPowerEvent::Wake);
    }
    SUBCASE("dimmed and back is not a wake; dimmed, off, on is") {
        DisplayPowerTracker t;
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
        CHECK(t.OnState(2) == DisplayPowerEvent::None);
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
        CHECK(t.OnState(2) == DisplayPowerEvent::None);
        CHECK(t.OnState(0) == DisplayPowerEvent::Off);
        CHECK(t.OnState(1) == DisplayPowerEvent::Wake);
    }
    SUBCASE("off, then dimmed, then on is still a wake") {
        DisplayPowerTracker t;
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
        CHECK(t.OnState(0) == DisplayPowerEvent::Off);
        CHECK(t.OnState(2) == DisplayPowerEvent::None);
        CHECK(t.OnState(1) == DisplayPowerEvent::Wake);
        CHECK(t.OnState(1) == DisplayPowerEvent::None);
    }
    SUBCASE("registered while dimmed, or off twice") {
        DisplayPowerTracker a;
        CHECK(a.OnState(2) == DisplayPowerEvent::None);
        CHECK(a.OnState(1) == DisplayPowerEvent::None);
        DisplayPowerTracker b;
        CHECK(b.OnState(0) == DisplayPowerEvent::Off);
        CHECK(b.OnState(0) == DisplayPowerEvent::Off);
        CHECK(b.OnState(1) == DisplayPowerEvent::Wake);
    }
}

// ============================================================================
// Windows tone map (windows_tonemap.h): what Windows tells apps an HDR display peaks at
// ============================================================================

TEST_CASE("Windows tone map: Auto reports 10000 nits exactly while DesktopLUT's tone map is meant to run") {
    // mode, tone map on, suspended, exiting
    CHECK(WindowsToneMapReportsMax(WindowsToneMap::Auto, true, false, false));
    CHECK_FALSE(WindowsToneMapReportsMax(WindowsToneMap::Auto, false, false, false));   // the tone map off
    CHECK_FALSE(WindowsToneMapReportsMax(WindowsToneMap::Auto, true, true, false));     // suspended
    CHECK_FALSE(WindowsToneMapReportsMax(WindowsToneMap::Auto, true, false, true));     // DesktopLUT exits
    for (bool on : { false, true })
        for (bool suspended : { false, true })
            for (bool exiting : { false, true }) {
                CHECK(WindowsToneMapReportsMax(WindowsToneMap::Bypass, on, suspended, exiting));
                CHECK_FALSE(WindowsToneMapReportsMax(WindowsToneMap::Panel, on, suspended, exiting));
            }
}

TEST_CASE("Windows tone map: Auto's 10000 needs a tone map that takes the full range") {
    TonemapSettings tm;
    CHECK_FALSE(ToneMapTakesFullRange(tm));               // off
    tm.enabled = true;
    tm.dynamicPeak = true;
    CHECK(ToneMapTakesFullRange(tm));                     // dynamic: the detected peak lands on the target
    tm.dynamicPeak = false;
    tm.sourcePeakNits = 1000.0f;                          // the INI default: hard-clips above 1000
    CHECK_FALSE(ToneMapTakesFullRange(tm));
    tm.sourcePeakNits = 4000.0f;                          // flat above 4000
    CHECK_FALSE(ToneMapTakesFullRange(tm));
    tm.sourcePeakNits = 10000.0f;
    CHECK(ToneMapTakesFullRange(tm));
    tm.enabled = false;
    CHECK_FALSE(ToneMapTakesFullRange(tm));
}

TEST_CASE("Windows tone map: the INI spelling round-trips, anything else is Auto") {
    for (WindowsToneMap m : { WindowsToneMap::Auto, WindowsToneMap::Bypass, WindowsToneMap::Panel })
        CHECK(ParseWindowsToneMap(WindowsToneMapKey(m)) == m);
    CHECK(std::wstring(WindowsToneMapKey(WindowsToneMap::Auto)) == L"auto");
    CHECK(std::wstring(WindowsToneMapKey(WindowsToneMap::Bypass)) == L"bypass");
    CHECK(std::wstring(WindowsToneMapKey(WindowsToneMap::Panel)) == L"panel");
    CHECK(ParseWindowsToneMap(L"Panel") == WindowsToneMap::Panel);
    CHECK(ParseWindowsToneMap(L"") == WindowsToneMap::Auto);
    CHECK(ParseWindowsToneMap(nullptr) == WindowsToneMap::Auto);
    CHECK(ParseWindowsToneMap(L"10000") == WindowsToneMap::Auto);
}

namespace {

// A 256-byte EDID: a base block and one CTA-861 extension whose data block collection is `blocks`.
std::vector<BYTE> EdidWithCta(const std::vector<BYTE>& blocks, BYTE extensionTag = 0x02) {
    std::vector<BYTE> e(256, 0);
    const BYTE header[] = { 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00 };
    memcpy(e.data(), header, 8);
    e[126] = 1;                                  // one extension
    BYTE* x = e.data() + 128;
    x[0] = extensionTag;
    x[1] = 3;                                    // CTA revision 3
    x[2] = (BYTE)(4 + blocks.size());            // the timings start right after the data blocks
    memcpy(x + 4, blocks.data(), blocks.size());
    return e;
}

float CtaLuminance(BYTE cv) { return 50.0f * std::pow(2.0f, cv / 32.0f); }

}  // namespace

TEST_CASE("EDID: the CTA-861 HDR static metadata block gives the panel's own HDR luminance") {
    // A video data block first (tag 2, three SVDs), then the HDR static metadata block (tag 7, extended tag 6):
    // EOTFs, descriptors, max 138 (~994 nits), max frame-average 115 (~604 nits), min 50.
    const std::vector<BYTE> blocks = { (2 << 5) | 3, 0x10, 0x04, 0x03,
                                       (7 << 5) | 6, 0x06, 0x0D, 0x01, 138, 115, 50 };
    std::vector<BYTE> edid = EdidWithCta(blocks);
    EdidHdrLuminance lum;
    REQUIRE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    CHECK(lum.maxNits == doctest::Approx(CtaLuminance(138)));
    CHECK(lum.maxNits == doctest::Approx(993.6f).epsilon(0.001));
    CHECK(lum.maxFrameAvgNits == doctest::Approx(CtaLuminance(115)));
    CHECK(lum.minNits == doctest::Approx(CtaLuminance(138) * (50.0f / 255.0f) * (50.0f / 255.0f) / 100.0f));

    // Only the optional max byte: no frame average, no min.
    edid = EdidWithCta({ (7 << 5) | 4, 0x06, 0x0D, 0x01, 160 });
    REQUIRE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    CHECK(lum.maxNits == doctest::Approx(1600.0f));
    CHECK(lum.maxFrameAvgNits == 0.0f);
    CHECK(lum.minNits == 0.0f);
}

TEST_CASE("EDID: no HDR peak declared, or nothing to read it from") {
    EdidHdrLuminance lum;
    // The block without its luminance bytes (EOTFs and descriptors only), or a max of 0 ("not indicated").
    std::vector<BYTE> edid = EdidWithCta({ (7 << 5) | 3, 0x06, 0x0D, 0x01 });
    CHECK_FALSE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    edid = EdidWithCta({ (7 << 5) | 6, 0x06, 0x0D, 0x01, 0, 115, 50 });
    CHECK_FALSE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    CHECK(lum.maxNits == 0.0f);
    // Another extended tag (colorimetry, 5) is not it.
    edid = EdidWithCta({ (7 << 5) | 3, 0x05, 0xC0, 0x00 });
    CHECK_FALSE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    // A block length running past the data block collection: nothing is read past it.
    edid = EdidWithCta({ (7 << 5) | 20, 0x06, 0x0D, 0x01, 138 });
    CHECK_FALSE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    // Not a CTA extension (DisplayID, 0x70), a base block alone, no EDID header.
    edid = EdidWithCta({ (7 << 5) | 6, 0x06, 0x0D, 0x01, 138, 115, 50 }, 0x70);
    CHECK_FALSE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    CHECK_FALSE(ParseEDIDHdrLuminance(kEDID_sRGB, sizeof(kEDID_sRGB), lum));
    edid = EdidWithCta({ (7 << 5) | 6, 0x06, 0x0D, 0x01, 138, 115, 50 });
    edid[0] = 0x01;
    CHECK_FALSE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    CHECK_FALSE(ParseEDIDHdrLuminance(nullptr, 256, lum));
}

TEST_CASE("EDID: a real HDR monitor's CTA-861 extension (captured, not built by the parser's own reading)") {
    // A mini-LED HDR monitor's extension block as its EDID carries it: video, audio, speaker, vendor and colorimetry
    // blocks before the HDR static metadata block (E6 06 0F 01 AA A0 04), then detailed timings. An independent
    // CTA-861.3 decode of the same bytes gives 1987 / 1600 / 0.0049 nits.
    static const BYTE kCta[128] = {
        0x02, 0x03, 0x36, 0xC0, 0x52, 0x61, 0x10, 0x04, 0x03, 0x02, 0x60, 0x1F, 0x13, 0x12, 0x11, 0x05,
        0x14, 0x07, 0x16, 0x0F, 0x0E, 0x1E, 0x1D, 0x23, 0x09, 0x17, 0x07, 0x83, 0x01, 0x00, 0x00, 0xE2,
        0x00, 0xEA, 0xE3, 0x05, 0xE3, 0x01, 0xE6, 0x06, 0x0F, 0x01, 0xAA, 0xA0, 0x04, 0x68, 0x1A, 0x00,
        0x00, 0x01, 0x01, 0x28, 0x3C, 0x00, 0x40, 0xAD, 0x00, 0xA4, 0xF0, 0x70, 0x96, 0x80, 0x34, 0x20,
        0x35, 0x00, 0xC0, 0x1C, 0x32, 0x00, 0x00, 0x1E, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xA2,
    };
    std::vector<BYTE> edid(256, 0);
    const BYTE header[] = { 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00 };
    memcpy(edid.data(), header, 8);
    edid[126] = 1;
    memcpy(edid.data() + 128, kCta, 128);
    EdidHdrLuminance lum;
    REQUIRE(ParseEDIDHdrLuminance(edid.data(), edid.size(), lum));
    CHECK(lum.maxNits == doctest::Approx(1987.0f).epsilon(0.001));
    CHECK(lum.maxFrameAvgNits == doctest::Approx(1600.0f).epsilon(0.001));
    CHECK(lum.minNits == doctest::Approx(0.0049f).epsilon(0.01));
}
