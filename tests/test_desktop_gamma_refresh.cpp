// Desktop gamma following the Windows SDR white level at runtime (2026-10-04 review fixes): the per-monitor
// follow-up plan (HDR gating, calibration / dialog / backoff deferral), the retry backoff after a failed
// re-bake, the per-permutation DG stamps that keep a variant baked under another level recognisably stale,
// their INI round trip, and the cheap DisplayConfig pre-filter in front of the fresh-DXGI HDR test.
// The re-bake that installs a profile is not run here (it would change this machine's display state).

#include "doctest.h"
#include "gui_mhc.h"
#include "settings.h"
#include "displayconfig.h"
#include "globals.h"
#include "types.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>

namespace {

using Clock = SdrWhiteRebakeBackoff::Clock;

bool Reason(const SdrWhiteFollowPlan& p, const char* want) {
    return p.pendingReason && std::strcmp(p.pendingReason, want) == 0;
}

// An HDR MHC with DG, active on permutation DG (+ WB), every variant cached and stamped at `stamp`.
MHCSettings DgMhc(float recorded, float stamp) {
    MHCSettings m;
    m.enabled = true;
    m.desktopGammaEnabled = true;
    m.dgSdrWhiteNits = recorded;
    m.activePerm = MHCSettings::PERM_DG | MHCSettings::PERM_WB;
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
        m.permNames[k] = L"DesktopLUT_UnitTest_NoSuchProfile_P" + std::to_wstring(k) + L".icm";
        m.permPaths[k] = L"C:\\nonexistent\\" + m.permNames[k];
        m.permDgWhiteNits[k] = stamp;
    }
    m.profileName = m.permNames[m.activePerm];
    m.profilePath = m.permPaths[m.activePerm];
    return m;
}

} // namespace

// =============================================================================================
// The follow-up plan: gating and deferral
// =============================================================================================

TEST_CASE("SDR white follow: nothing stale, or not in HDR -> nothing happens and nothing waits") {
    for (bool hold : { false, true }) {
        auto p = PlanSdrWhiteFollow(false, false, true, hold, false, true);
        CHECK_FALSE(p.updateShader);
        CHECK_FALSE(p.rebakeMhc);
        CHECK(p.pendingReason == nullptr);
    }
    // Outside HDR the level is not desktop gamma's reference (Windows may report 80 in SDR): keep the last
    // HDR-mode value, report nothing.
    auto sdr = PlanSdrWhiteFollow(true, true, /*inHdr=*/false, false, false, true);
    CHECK_FALSE(sdr.updateShader);
    CHECK_FALSE(sdr.rebakeMhc);
    CHECK(sdr.pendingReason == nullptr);
}

TEST_CASE("SDR white follow: a calibration session or live edit holds BOTH paths and says so") {
    auto p = PlanSdrWhiteFollow(true, true, true, /*sessionHold=*/true, false, true);
    CHECK_FALSE(p.updateShader);   // the measured output must not move mid-run, shader path included
    CHECK_FALSE(p.rebakeMhc);
    CHECK(Reason(p, "calibration"));
    auto shaderOnly = PlanSdrWhiteFollow(false, true, true, true, false, true);
    CHECK_FALSE(shaderOnly.updateShader);
    CHECK(Reason(shaderOnly, "calibration"));
}

TEST_CASE("SDR white follow: in HDR, unheld -> shader now; MHC re-bake unless a dialog or the backoff holds it") {
    auto all = PlanSdrWhiteFollow(true, true, true, false, false, true);
    CHECK(all.updateShader);
    CHECK(all.rebakeMhc);
    CHECK(all.pendingReason == nullptr);

    auto shaderOnly = PlanSdrWhiteFollow(false, true, true, false, false, true);
    CHECK(shaderOnly.updateShader);
    CHECK_FALSE(shaderOnly.rebakeMhc);
    CHECK(shaderOnly.pendingReason == nullptr);

    auto dialog = PlanSdrWhiteFollow(true, true, true, false, /*dialogOpen=*/true, true);
    CHECK(dialog.updateShader);        // the shader reference does not touch the dialog's settings
    CHECK_FALSE(dialog.rebakeMhc);
    CHECK(Reason(dialog, "mhc_dialog_open"));

    auto backoff = PlanSdrWhiteFollow(true, false, true, false, false, /*backoffAllows=*/false);
    CHECK_FALSE(backoff.updateShader);
    CHECK_FALSE(backoff.rebakeMhc);
    CHECK(Reason(backoff, "retry_backoff"));
}

// =============================================================================================
// Retry backoff after a failed re-bake
// =============================================================================================

TEST_CASE("SDR white re-bake backoff: same level waits 1, 2, 4 .. 32 min; a new level retries at once") {
    SdrWhiteRebakeBackoff b;
    const auto t0 = Clock::time_point{} + std::chrono::hours(1);
    CHECK(b.Allowed(0, 116.0f, t0));

    b.Failed(0, 116.0f, t0);
    CHECK_FALSE(b.Allowed(0, 116.0f, t0));
    CHECK_FALSE(b.Allowed(0, 116.004f, t0 + std::chrono::seconds(59)));   // same level (rounding)
    CHECK(b.Allowed(0, 116.0f, t0 + std::chrono::seconds(60)));
    CHECK(b.Allowed(0, 120.0f, t0));            // the level moved again: try at once
    CHECK(b.Allowed(1, 116.0f, t0));            // per monitor

    // Consecutive failures at one level double the wait, capped at 32 minutes.
    auto t = t0;
    for (int n = 2; n <= 8; n++) {
        b.Failed(0, 116.0f, t);
        const int minutes = 1 << (std::min)(n - 1, 5);
        CAPTURE(n);
        CHECK_FALSE(b.Allowed(0, 116.0f, t + std::chrono::minutes(minutes) - std::chrono::seconds(1)));
        CHECK(b.Allowed(0, 116.0f, t + std::chrono::minutes(minutes)));
        t += std::chrono::minutes(minutes);
    }
    // A failure at a NEW level starts over at one minute.
    b.Failed(0, 203.0f, t);
    CHECK(b.Allowed(0, 203.0f, t + std::chrono::minutes(1)));

    b.Succeeded(0);
    CHECK(b.Allowed(0, 203.0f, t));
    b.Failed(0, 203.0f, t);
    b.Failed(1, 203.0f, t);
    b.Clear();                                  // a display transition
    CHECK(b.Allowed(0, 203.0f, t));
    CHECK(b.Allowed(1, 203.0f, t));
}

// =============================================================================================
// Per-permutation DG stamps
// =============================================================================================

TEST_CASE("SDR white stamps: a DG variant is current only at its stamp; non-DG and stamp-0 bakes always are") {
    MHCSettings m = DgMhc(116.0f, 116.0f);
    CHECK(MhcPermDgBakedAt(m, MHCSettings::PERM_DG, 116.0f));
    CHECK_FALSE(MhcPermDgBakedAt(m, MHCSettings::PERM_DG, 80.0f));
    CHECK(MhcPermDgBakedAt(m, MHCSettings::PERM_WB, 80.0f));          // no DG bit: W-independent
    m.permDgWhiteNits[MHCSettings::PERM_DG] = 0.0f;                    // DG bit, but the bake carried no DG
    CHECK(MhcPermDgBakedAt(m, MHCSettings::PERM_DG, 80.0f));
    m.permDgWhiteNits[MHCSettings::PERM_DG] = -1.0f;                   // unreadable stamp: stale
    CHECK_FALSE(MhcPermDgBakedAt(m, MHCSettings::PERM_DG, 116.0f));
    CHECK(MhcPermDgBakedAt(m, -1, 116.0f));                            // out of range: never stale
    CHECK(MhcPermDgBakedAt(m, MHCSettings::PERM_COUNT, 116.0f));
}

TEST_CASE("SDR white stamps: the HDR MHC is stale when the record OR the active profile's DG stamp lags") {
    CHECK_FALSE(HdrMhcSdrWhiteStale(DgMhc(116.0f, 116.0f), 116.0f));
    CHECK(HdrMhcSdrWhiteStale(DgMhc(80.0f, 80.0f), 116.0f));          // level moved
    // Recorded matches but the active profile was baked under the old level (a swap raced the level change,
    // or a re-bake failed): still stale, so it gets re-baked.
    CHECK(HdrMhcSdrWhiteStale(DgMhc(116.0f, 80.0f), 116.0f));
    MHCSettings off = DgMhc(116.0f, 80.0f);
    off.enabled = false;                                               // nothing installed: only the record counts
    CHECK_FALSE(HdrMhcSdrWhiteStale(off, 116.0f));
    MHCSettings noDg = DgMhc(116.0f, 80.0f);
    noDg.activePerm = MHCSettings::PERM_WB;                            // DG swapped out (whitelist)
    CHECK_FALSE(HdrMhcSdrWhiteStale(noDg, 116.0f));
}

TEST_CASE("SDR white stamps: recording a level forgets only the cached variants baked under another one") {
    auto savedLive = g_gui.monitorSettings;
    g_gui.monitorSettings.assign(1, MonitorSettings{});
    MHCSettings& m = g_gui.monitorSettings[0].hdrMHC;
    m = DgMhc(80.0f, 80.0f);
    m.activePerm = MHCSettings::PERM_WB;                               // DG not on screen: no install
    m.profileName = m.permNames[MHCSettings::PERM_WB];
    m.permDgWhiteNits[MHCSettings::PERM_DG | MHCSettings::PERM_GS] = 116.0f;                       // already at 116
    m.permDgWhiteNits[MHCSettings::PERM_DG | MHCSettings::PERM_WB | MHCSettings::PERM_GS] = 0.0f;  // no DG baked

    bool failed = true;
    CHECK(RebakeHdrMhcForSdrWhite(0, 116.0f, &failed));
    CHECK_FALSE(failed);
    CHECK(m.dgSdrWhiteNits == 116.0f);
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
        CAPTURE(k);
        const bool dropped = (k == MHCSettings::PERM_DG) || (k == (MHCSettings::PERM_DG | MHCSettings::PERM_WB));
        CHECK(m.permNames[k].empty() == dropped);
    }
    // Nothing stale any more: a repeat is a no-op.
    CHECK_FALSE(RebakeHdrMhcForSdrWhite(0, 116.0f, &failed));
    CHECK_FALSE(failed);
    g_gui.monitorSettings = savedLive;
}

TEST_CASE("SDR white stamps: INI round trip; absent = baked at the recorded level; unreadable = stale") {
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    std::wstring ini = std::wstring(tmp) + L"desktoplut_test_dgstamps.ini";
    _wremove(ini.c_str());

    MHCSettings original = DgMhc(116.0f, 116.0f);
    original.permDgWhiteNits[MHCSettings::PERM_DG] = 80.0f;            // a stale variant survives the round trip
    original.permDgWhiteNits[MHCSettings::PERM_WB] = 0.0f;
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinearPQ();
    SaveMHCSettings(L"TestMon", L"HDR_", original, ini.c_str());
    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"HDR_", loaded, ini.c_str());
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
        CAPTURE(k);
        CHECK(loaded.permDgWhiteNits[k] == original.permDgWhiteNits[k]);
    }

    // A cache saved before stamps existed: every entry counts as baked at the recorded level.
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++)
        WritePrivateProfileStringW(L"TestMon", (L"HDR_MHCPermDgWhite" + std::to_wstring(k)).c_str(), nullptr,
                                   ini.c_str());
    MHCSettings legacy;
    LoadMHCSettings(L"TestMon", L"HDR_", legacy, ini.c_str());
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) CHECK(legacy.permDgWhiteNits[k] == 116.0f);

    // Unreadable / out-of-range stamps mark the entry stale (regenerated), never "no DG".
    const wchar_t* garbage[] = { L"abc", L"-5", L"1e9", L"nan" };
    for (int gi = 0; gi < 4; gi++) {
        CAPTURE(gi);
        WritePrivateProfileStringW(L"TestMon", L"HDR_MHCPermDgWhite2", garbage[gi], ini.c_str());
        MHCSettings g;
        LoadMHCSettings(L"TestMon", L"HDR_", g, ini.c_str());
        CHECK(g.permDgWhiteNits[2] == -1.0f);
        CHECK_FALSE(MhcPermDgBakedAt(g, 2, 116.0f));
    }
    _wremove(ini.c_str());
}

// =============================================================================================
// The cheap HDR pre-filter
// =============================================================================================

TEST_CASE("SDR white: DisplayConfig rules HDR out cheaply; DXGI decides whatever it cannot") {
    auto mode = [](DisplayColorMode m, const char* src) { DisplayColorModeResult r; r.mode = m; r.source = src; return r; };
    // 24H2+ query: SDR and WCG/ACM are definitive.
    CHECK_FALSE(DisplayModeAllowsHdr(mode(DisplayColorMode::SDR, "displayconfig2")));
    CHECK_FALSE(DisplayModeAllowsHdr(mode(DisplayColorMode::AcmSdr, "displayconfig2")));
    CHECK(DisplayModeAllowsHdr(mode(DisplayColorMode::HDR, "displayconfig2")));
    // Legacy query: advanced colour off is definitive; "on" is HDR or ACM -> DXGI decides.
    CHECK_FALSE(DisplayModeAllowsHdr(mode(DisplayColorMode::SDR, "displayconfig")));
    CHECK(DisplayModeAllowsHdr(mode(DisplayColorMode::AcmSdr, "displayconfig")));
    // No DisplayConfig answer at all (the classifier falls back to "dxgi" with SDR): never trust it to exclude.
    CHECK(DisplayModeAllowsHdr(mode(DisplayColorMode::SDR, "dxgi")));
    CHECK(DisplayModeAllowsHdr(mode(DisplayColorMode::Unknown, "none")));
}
