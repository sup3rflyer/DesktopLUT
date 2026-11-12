#include "actions/actions.h"
#include "actions/reconcile.h"

#include "analysis.h"
#include "osd.h"
#include "gui.h"
#include "gui_mhc.h"
#include "refresh_trim.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "dwm_inject.h"
#include "displayconfig.h"
#include "fald.h"
#include "mhc.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "windows_tonemap.h"
#include "ui/ui_model.h"   // HdrMhcOnlyDesktopGamma (pure)
#include <cmath>
#include <cstdio>
#include <cwctype>
#include <iostream>
#include <mutex>

namespace act {

namespace {

Result Done(bool changed = true) { Result r; r.changed = changed; return r; }

Result Fail(notify::Sev sev, std::wstring text) {
    Result r;
    r.sev = sev;
    r.text = std::move(text);
    return r;
}

// Report an action's message once, in the notification log (the classic status line shows it too).
Result Told(int mon, const wchar_t* source, Result r) {
    if (!r.text.empty()) notify::Post(r.sev, mon, source, r.text);
    return r;
}

float Clamp(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

FaldSettings& FaldOf(MonitorSettings& ms, bool hdr) { return hdr ? ms.hdrColorCorrection.fald : ms.sdrColorCorrection.fald; }
MHCSettings& MhcOf(MonitorSettings& ms, bool hdr) { return hdr ? ms.hdrMHC : ms.sdrMHC; }

// Push a live colour-correction change for one mode to whichever engine runs it.
void PushCorrection(int mon, bool hdr, bool reevaluateOverlay) {
    if (!g_gui.isRunning) return;
    UpdateColorCorrectionLive(mon, hdr);
    if (g_dwmHookMode.load()) UpdateDwmHookSharedConfig();
    else if (reevaluateOverlay) DwmHookReevaluateOverlay();
}

// A FALD layer setting of one mode changed (GUI-thread port of the classic ApplyFaldSettingChange).
// Returns what the reconcile did (a Start / Restart already loaded the panel files).
ReconcileDecision PushFald(int mon, bool hdr, bool enabledNow) {
    if (g_gui.isRunning) {
        UpdateColorCorrectionLive(mon, hdr);
        if (g_dwmHookMode.load()) {
            UpdateDwmHookSharedConfig();
            RequestFaldFullRecompose();
        } else {
            DwmHookReevaluateOverlay();
        }
    }
    SaveSettings();
    const ReconcileDecision d = ReconcileLiveLayers("FALD");
    if (enabledNow && g_dwmHookMode.load())
        notify::Post(notify::Sev::Info, mon, L"fald", L"FALD in DWM hook mode: correction, Starfield and LED lag");
    return d;
}

void RegenerateMhc(int mon, bool hdr) {
    if (RegenerateMhcIfActive(mon, hdr) == MhcRegenResult::Failed)
        SetStatus(L"MHC profile not updated (source file unreadable?) - the previous profile stays active");
}

void Finish(int mon) {
    GuiNotifySettingsChanged(mon);
}

// A stage switch waits while DLC calibrates or a live grayscale edit runs: the session measures exactly
// what it set up.
bool RefusedDuringCalibration(int mon) {
    if (!IsCalibrationOrLiveEditActive()) return false;
    notify::Post(notify::Sev::Warning, mon, L"stage", L"Calibration in progress: stage switches stay as they are");
    return true;
}

}  // namespace

bool ValidMonitor(int mon) { return mon >= 0 && mon < (int)g_gui.monitorSettings.size(); }

// ---------------------------------------------------------------------------------------------------
// 3D LUT / engine

Result SetLutPath(int mon, bool hdr, const std::wstring& path) {
    if (!ValidMonitor(mon)) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        (hdr ? ms.hdrPath : ms.sdrPath) = path;
        if (!path.empty()) (hdr ? ms.hdrLutBypass : ms.sdrLutBypass) = false;   // a cube just picked is meant to be seen
    }
    SaveSettings();
    // The hook reads cube files only when it is injected; re-setting the same path is a reload.
    if (g_gui.isRunning) RestartLiveLayers();
    else ReconcileLiveLayers("3D LUT");
    Finish(mon);
    return Done();
}

Result SetLutBypass(int mon, bool hdr, bool bypass) {
    if (!ValidMonitor(mon) || RefusedDuringCalibration(mon)) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        bool& flag = hdr ? ms.hdrLutBypass : ms.sdrLutBypass;
        if (flag == bypass) return Done(false);
        flag = bypass;
    }
    SaveSettings();
    // The hook keeps the cube staged and skips it (shared config: instant, no re-inject); the overlay reads
    // the cubes it applies when it starts.
    if (g_gui.isRunning) {
        if (g_dwmHookMode.load()) UpdateDwmHookSharedConfig();
        else RestartLiveLayers();
    }
    ReconcileLiveLayers("3D LUT bypass");   // the cube may have been the only live-layer need
    Finish(mon);
    return Done();
}

void SetTetrahedral(bool on) {
    g_tetrahedralInterp = on;
    SaveSettings();
    SyncClassicGlobalControls();
}

void SetHdrDither(bool on) {
    // Live: the overlay re-reads it at its next constant-buffer write; the hook via the shared config.
    g_hdrDither = on;
    SaveSettings();
    UpdateDwmHookSharedConfig();
    SyncClassicGlobalControls();
}

void SetDwmHookMode(bool on) {
    // Switching it on is the user's deliberate retry (after a fall-back, a Windows update): every give-up count of
    // the hook starts again (dwm_hook_stability.h). The fall-back itself only ever switches it off.
    if (on) g_dwmHookStability.OnUserReEnable();
    g_dwmHookMode.store(on);
    SaveSettings();
    if (g_gui.isRunning) RestartLiveLayers();
    else ReconcileLiveLayers("engine");   // the FALD layer's need differs per engine (the hook's needs a panel file)
    if (ValidMonitor(g_gui.currentMonitor)) Finish(g_gui.currentMonitor);
    SyncClassicGlobalControls();
}

// ---------------------------------------------------------------------------------------------------
// Tone mapping

Result SetTonemap(int mon, const TonemapSettings& t) {
    if (!ValidMonitor(mon)) return Done(false);
    TonemapSettings v = t;
    v.targetPeakNits = Clamp(v.targetPeakNits, 10.0f, 10000.0f);
    v.sourcePeakNits = Clamp(v.sourcePeakNits, 10.0f, 10000.0f);
    bool enableChanged = false, changed = false, rangeChanged = false;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        TonemapSettings& tm = g_gui.monitorSettings[mon].hdrColorCorrection.tonemap;
        enableChanged = tm.enabled != v.enabled;
        rangeChanged = ToneMapTakesFullRange(tm) != ToneMapTakesFullRange(v);
        changed = enableChanged || tm.curve != v.curve || tm.dynamicPeak != v.dynamicPeak ||
                  tm.targetPeakNits != v.targetPeakNits || tm.sourcePeakNits != v.sourcePeakNits;
        tm = v;
    }
    if (!changed) return Done(false);
    // In hook mode the DLL tone maps from the shared config; the overlay context is still updated so
    // the analysis overlay's TM indicator follows. Only an enable change can change whether the
    // overlay is needed at all.
    if (g_gui.isRunning) PushCorrection(mon, true, enableChanged);
    SaveSettings();
    // An enable change can start or stop the live layers, or change the hook's display list.
    if (enableChanged) ReconcileLiveLayers("tone map");
    // Auto follows whether DesktopLUT tone maps the full range (on, and dynamic or a static content peak of 10000).
    if (rangeChanged) ReconcileWindowsToneMap("tone map", false);
    Finish(mon);
    return Done();
}

// ---------------------------------------------------------------------------------------------------
// Windows tone map

Result SetWindowsToneMap(int mon, WindowsToneMap mode) {
    if (!ValidMonitor(mon)) return Done(false);
    bool changed;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        WindowsToneMap& stored = g_gui.monitorSettings[mon].windowsToneMap;
        changed = stored != mode;
        stored = mode;
    }
    if (!changed) return Done(false);
    SaveSettings();
    ReconcileWindowsToneMap("setting", false);
    Finish(mon);
    return Done();
}

// ---------------------------------------------------------------------------------------------------
// FALD

std::wstring FaldTransferMismatch(const std::wstring& path, bool hdr) {
    uint32_t transfer = 0;
    if (path.empty() || !FaldPanelFileTransfer(path, transfer) || FaldTransferMatchesMode(transfer, hdr)) return {};
    return hdr ? L"FALD: this panel file is an SDR (gamma) fit - the HDR row needs a PQ fit profiled in HDR"
               : L"FALD: this panel file is an HDR (PQ) fit - the SDR (ACM) row needs a gamma fit profiled in SDR";
}

Result SetFaldEnabled(int mon, bool hdr, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    std::wstring path;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        path = FaldOf(g_gui.monitorSettings[mon], hdr).paramsPath;
    }
    if (on && path.empty()) {
        Result r = Fail(notify::Sev::Warning, hdr ? L"FALD compensation (HDR) needs a panel parameter file (…)"
                                                  : L"FALD compensation (SDR) needs a panel parameter file profiled in SDR under ACM (…)");
        SetStatus(r.text.c_str());
        Finish(mon);
        return r;
    }
    if (on) {
        std::wstring mismatch = FaldTransferMismatch(path, hdr);
        if (!mismatch.empty()) {
            SetStatus(mismatch.c_str());
            Finish(mon);
            return Fail(notify::Sev::Warning, mismatch);
        }
    }
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);   // state.get reads it
        FaldOf(g_gui.monitorSettings[mon], hdr).enabled = on;
    }
    PushFald(mon, hdr, on);
    Finish(mon);
    return Done();
}

Result SetFaldPanelFile(int mon, bool hdr, const std::wstring& path, bool reload) {
    if (!ValidMonitor(mon)) return Done(false);
    std::wstring mismatch = FaldTransferMismatch(path, hdr);
    if (!mismatch.empty()) {
        SetStatus(mismatch.c_str());
        Finish(mon);   // the classic row shows the file it keeps
        return Fail(notify::Sev::Warning, mismatch);
    }
    bool changed = false, enabledNow = false;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);   // the pipe thread reads this wstring
        FaldSettings& fald = FaldOf(g_gui.monitorSettings[mon], hdr);
        if (fald.paramsPath != path) { fald.paramsPath = path; changed = true; }
        if (reload) { fald.reloadSeq++; changed = true; }   // re-browsing the same (re-exported) file = reload
        enabledNow = fald.enabled;
    }
    if (!changed) return Done(false);
    // Hook mode: a resident hook takes the file live — no re-injection, the layer switches between two presents.
    // Before PushFald, whose reconcile then sees the hook holding the new file (no restart for the path change).
    const bool live = FaldPanelFileChangedLive(mon, hdr);
    const ReconcileDecision d = PushFald(mon, hdr, enabledNow);
    // Not delivered: the DLL reads the file at injection (a new path already changed the display list
    // and restarted it; a reload of the same path did not).
    if (!live && d != ReconcileDecision::Start && d != ReconcileDecision::Restart) FaldPanelFileChangedReinject();
    Finish(mon);
    return Done();
}

Result ClearFaldPanelFile(int mon, bool hdr) {
    if (!ValidMonitor(mon)) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);   // the pipe thread reads this wstring
        FaldSettings& fald = FaldOf(g_gui.monitorSettings[mon], hdr);
        if (fald.paramsPath.empty() && !fald.enabled) return Done(false);
        fald.paramsPath.clear();
        fald.enabled = false;   // a layer switched on without a file would come back on with the next one
    }
    // Hook mode: the resident hook forgets the file it holds. Nothing is re-injected for it: with the layer off a
    // held file is inert (act::DecideHookStale), and the next start stages none.
    FaldPanelFileChangedLive(mon, hdr);
    PushFald(mon, hdr, false);
    Finish(mon);
    return Done();
}

bool SetFaldDebugView(FaldSettings& f, unsigned int view) {
    if (view >= kFaldDebugViews) view = 0;
    const bool changed = f.debugMode != view;
    f.debugMode = view;
    return changed;
}

bool FaldViewOn() {
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    for (const MonitorSettings& ms : g_gui.monitorSettings)
        if (ms.hdrColorCorrection.fald.debugMode != 0 || ms.sdrColorCorrection.fald.debugMode != 0) return true;
    return false;
}

bool ClearFaldViews() {
    bool any = false;
    for (int mon = 0; ValidMonitor(mon); mon++) {
        bool on;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            const MonitorSettings& ms = g_gui.monitorSettings[mon];
            on = ms.hdrColorCorrection.fald.debugMode != 0 || ms.sdrColorCorrection.fald.debugMode != 0;
        }
        if (on && EditFaldShared(mon, [](FaldSettings& s) { return SetFaldDebugView(s, 0); }).changed) any = true;
    }
    if (any) notify::Post(notify::Sev::Info, -1, L"fald", L"FALD view ended: corrected picture");
    return any;
}

bool SetFaldStarField(FaldStarfieldSettings& s, FaldStarField field, float v) {
    if (!(v >= 0.0f)) v = 0.0f;   // NaN too
    switch (field) {
    case FaldStarField::Reach: {
        const float r = std::floor(v + 0.5f);
        const unsigned int n = r >= (float)FALD_STAR_EVEN_REACH_MAX ? FALD_STAR_EVEN_REACH_MAX : (unsigned int)r;
        const bool changed = s.evenReach != n;
        s.evenReach = n;
        return changed;
    }
    case FaldStarField::Even:     v = Clamp(v, 0.0f, 1.0f); break;
    case FaldStarField::Strength: v = Clamp(v, 0.0f, 1.0f); break;
    case FaldStarField::Spread:   v = Clamp(v, 0.0f, 4.0f); break;
    case FaldStarField::Keep:     v = Clamp(v, 0.0f, 10000.0f); break;
    }
    float& t = field == FaldStarField::Even ? s.even : field == FaldStarField::Strength ? s.strength
             : field == FaldStarField::Spread ? s.targetSigma : s.keepNits;
    const bool changed = t != v;
    t = v;
    return changed;
}

Result EditFaldShared(int mon, const std::function<bool(FaldSettings&)>& edit) {
    if (!ValidMonitor(mon)) return Done(false);
    bool changed = false;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        for (bool hdr : { true, false }) changed = edit(FaldOf(ms, hdr)) || changed;
    }
    if (!changed) return Done(false);
    // A view on or off: DesktopLUT's own windows are listed for the layer (or the list emptied) before the push below
    // carries it (shared/fald_exempt.h).
    FaldExemptRefresh(false);
    if (g_gui.isRunning) {
        UpdateColorCorrectionLive(mon, true);
        UpdateColorCorrectionLive(mon, false);
        if (g_dwmHookMode.load()) {
            UpdateDwmHookSharedConfig();
            RequestFaldFullRecompose();
        } else {
            DwmHookReevaluateOverlay();
        }
    }
    SaveSettings();
    ReconcileLiveLayers("FALD");
    Finish(mon);
    return Done();
}

// ---------------------------------------------------------------------------------------------------
// MHC

Result MhcInstall(int mon, bool hdr) {
    if (!ValidMonitor(mon)) return Done(false);
    if (!IsMHC2ApiAvailable())
        return Told(mon, L"mhc", Fail(notify::Sev::Error, L"MHC2 color management APIs not available. Requires Windows 10 21H2 or later."));
    if (!GenerateAndInstallMhcProfile(mon, hdr))
        return Told(mon, L"mhc", Fail(notify::Sev::Error, L"Failed to install MHC2 profile. Check that the GPU supports MHC2."));
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        ComputeMhcMetadata(MhcOf(g_gui.monitorSettings[mon], hdr), hdr);
    }
    SaveSettings();
    Finish(mon);
    Result r;
    r.sev = notify::Sev::Success;
    r.text = hdr ? L"HDR calibration profile installed" : L"SDR calibration profile installed";
    r.changed = true;
    return Told(mon, L"mhc", r);
}

Result MhcRemove(int mon, bool hdr) {
    if (!ValidMonitor(mon)) return Done(false);
    std::wstring profileName;
    float identityPeak = 0.0f;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MonitorSettings& ms = g_gui.monitorSettings[mon];
        MHCSettings& mhc = MhcOf(ms, hdr);
        profileName = mhc.profileName;
        if (profileName.empty())
            return Fail(notify::Sev::Info, L"No MHC2 profile is installed for this monitor.");
        identityPeak = MhcIdentityPeakNits(ms, hdr);
        // Disabled BEFORE the swap: the whitelist thread's CheckMhcProfiles would otherwise see enabled +
        // a non-matching default (the identity) and re-assert the old profile mid-swap.
        mhc.enabled = false;
    }
    // Identity FIRST, then disassociate the real one: Windows keeps applying the last associated MHC2
    // transform after a removal (HW 2026-09-03 / 2026-09-23), so a removal must hand the display a
    // neutral profile, never "nothing associated".
    ReplaceMhcProfileWithIdentity(mon, hdr, identityPeak, profileName);

    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    const std::wstring colorDir = std::wstring(sysDir) + L"\\spool\\drivers\\color\\";
    if (DeleteFileW((colorDir + profileName).c_str()))
        std::wcout << L"MHC: Deleted profile file " << profileName << std::endl;
    {
        // DeleteFileW takes no locks, so holding the settings mutex across it is safe.
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MHCSettings& mhc = MhcOf(g_gui.monitorSettings[mon], hdr);
        for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
            if (!mhc.permNames[k].empty()) {
                DeleteFileW((colorDir + mhc.permNames[k]).c_str());
                mhc.permNames[k].clear();
                mhc.permPaths[k].clear();
            }
            mhc.permTrimStamp[k].clear();
        }
        for (const auto& e : mhc.trimStash) DeleteFileW((colorDir + e.name).c_str());   // the other rates' variants
        mhc.trimStash.clear();
        mhc.enabled = false;
        mhc.profilePath.clear();
        mhc.profileName.clear();
        mhc.activePerm = 0;
        mhc.hasPerChannelTRC = false;
        mhc.metaPrimaries.clear();
        mhc.metaGamma.clear();
        mhc.metaWhiteBalance.clear();
        mhc.metaPeakNits = 0.0f;
    }
    UpdateMhcFlagsLive(mon);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSetWhiteBalanceEnabled(int mon, bool hdr, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MhcOf(g_gui.monitorSettings[mon], hdr).whiteBalanceEnabled = on;
    }
    RegenerateMhc(mon, hdr);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSetWhiteBalanceXY(int mon, bool hdr, float wx, float wy) {
    if (!ValidMonitor(mon)) return Done(false);
    // A white point, not any chromaticity: the bounds hold every Planckian / daylight white with room
    // (CCT -> infinity is x 0.240, 2000 K is x 0.527 y 0.413).
    wx = Clamp(wx, 0.20f, 0.60f);
    wy = Clamp(wy, 0.20f, 0.50f);
    bool changed;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MHCSettings& mhc = MhcOf(g_gui.monitorSettings[mon], hdr);
        changed = mhc.whiteBalanceWx != wx || mhc.whiteBalanceWy != wy;
        mhc.whiteBalanceWx = wx;
        mhc.whiteBalanceWy = wy;
    }
    if (!changed) { Finish(mon); return Done(false); }
    RegenerateMhc(mon, hdr);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSetCorrectionGsEnabled(int mon, bool hdr, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    {
        // initLinear*/points are a std::vector read by the generation snapshot.
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        GrayscaleSettings& gs = MhcOf(g_gui.monitorSettings[mon], hdr).correctionGrayscale;
        gs.enabled = on;
        if (gs.enabled && gs.points.empty()) {
            if (hdr) gs.initLinearPQ();
            else gs.initLinear();
        }
    }
    RegenerateMhc(mon, hdr);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSetCorrectionGsPoints(int mon, bool hdr, int count) {
    if (!ValidMonitor(mon)) return Done(false);
    if (count != 10 && count != 20 && count != 32) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        GrayscaleSettings& gs = MhcOf(g_gui.monitorSettings[mon], hdr).correctionGrayscale;
        if (gs.pointCount == count) return Done(false);
        gs.pointCount = count;
        if (hdr) gs.initLinearPQ();
        else gs.initLinear();
    }
    RegenerateMhc(mon, hdr);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcResetCorrectionGs(int mon, bool hdr) {
    if (!ValidMonitor(mon)) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        GrayscaleSettings& gs = MhcOf(g_gui.monitorSettings[mon], hdr).correctionGrayscale;
        if (hdr) gs.initLinearPQ();
        else gs.initLinear();
    }
    RegenerateMhc(mon, hdr);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSet24Gamma(int mon, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        g_gui.monitorSettings[mon].sdrMHC.correctionGrayscale.use24Gamma = on;
    }
    RegenerateMhc(mon, false);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSetCorrectionGsPeak(int mon, float nits) {
    if (!ValidMonitor(mon)) return Done(false);
    nits = Clamp(nits, 10.0f, 10000.0f);
    bool changed;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        float& peak = g_gui.monitorSettings[mon].hdrMHC.correctionGrayscale.peakNits;
        changed = peak != nits;
        peak = nits;
    }
    if (!changed) { Finish(mon); return Done(false); }
    RegenerateMhc(mon, true);
    SaveSettings();
    Finish(mon);
    return Done();
}

Result MhcSetRefreshTrim(int mon, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    bool was;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        was = g_gui.monitorSettings[mon].hdrMHC.refreshTrimEnabled;
    }
    SetRefreshTrimEnabled(mon, on);   // persists, reads the DIP again, swaps the variant (or waits out a hold)
    return Done(was != on);
}

void ReloadRefreshTrim() {
    RequestRefreshTrimDipScan();      // a DIP that changed resolves every display again (dlc::SetDipListener)
    ReconcileRefreshTrim("reload");
}

Result MhcSetDesktopGamma(int mon, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    bool mhcEnabled, wasOn;
    const bool userModeBefore = g_userDesktopGammaMode.load(), modeBefore = g_desktopGammaMode.load();
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MHCSettings& mhc = g_gui.monitorSettings[mon].hdrMHC;
        wasOn = mhc.desktopGammaEnabled;
        mhc.desktopGammaEnabled = on;
        mhcEnabled = mhc.enabled;
    }
    // No MHC profile yet: generate an identity one to carry the desktop gamma.
    if (on && !mhcEnabled) GenerateAndInstallMhcProfile(mon, true);
    bool haveProfile;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        const MHCSettings& mhc = g_gui.monitorSettings[mon].hdrMHC;
        mhcEnabled = mhc.enabled;
        haveProfile = !mhc.profileName.empty();
    }
    const bool dgActive = on && mhcEnabled;
    g_userDesktopGammaMode.store(dgActive);
    if (!g_gammaWhitelistActive.load()) g_desktopGammaMode.store(dgActive);
    // Swap to the cached permutation with / without DG (instant, no regenerate). If the variant cannot be built or
    // associated the previous profile stays, and so does the setting: it must never claim a gamma the screen lacks.
    if (haveProfile) {
        if (!SwapMhcPermBits(mon, true, on ? MHCSettings::PERM_DG : 0, on ? 0 : MHCSettings::PERM_DG)) {
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (ValidMonitor(mon)) g_gui.monitorSettings[mon].hdrMHC.desktopGammaEnabled = wasOn;
            }
            g_userDesktopGammaMode.store(userModeBefore);
            if (!g_gammaWhitelistActive.load()) g_desktopGammaMode.store(modeBefore);
            SaveSettings();   // an identity profile generated above to carry the gamma stays installed
            Finish(mon);
            return Told(mon, L"mhc", Fail(notify::Sev::Warning,
                                          L"Desktop gamma switch failed: the profile variant could not be swapped in"));
        }
    }
    SaveSettings();
    ReconcileLiveLayers("desktop gamma");   // desktop gamma is a live-layer need (LiveLayersNeeded)
    Finish(mon);
    return Done();
}

bool IsDesktopLutProfileName(const std::wstring& name) { return name.rfind(L"DesktopLUT_", 0) == 0; }

NeutralPeak DecideNeutralProfilePeak(float storedNits, bool inHdr, float reportedNits, bool defaultIsOurs, float edidNits) {
    auto plausible = [](float v) { return std::isfinite(v) && v > 0.0f && v < kWindowsToneMapBypassNits - 1.0f; };
    NeutralPeak out;
    if (inHdr && !defaultIsOurs && plausible(reportedNits)) {
        out.nits = reportedNits;
        out.source = NeutralPeakSource::Reported;
    } else if (plausible(storedNits)) {
        out.nits = storedNits;
        out.source = NeutralPeakSource::Kept;
    } else if (plausible(edidNits)) {
        out.nits = edidNits;
        out.source = NeutralPeakSource::Edid;
    }
    return out;
}

bool ShouldRestorePriorDefault(const std::wstring& prior, bool fileExists) {
    return !prior.empty() && !IsDesktopLutProfileName(prior) && fileExists;
}

namespace {

std::wstring ColorDirPath(const std::wstring& name) {
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    return std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + name;
}

// Before the one step puts the first HDR profile of ours on the display: what Windows reports for its peak now and which
// profile is its HDR default, recorded in the settings (panelPeakNits / priorDefaultProfile) for the bake to carry and
// for the switch-off to give back.
void RecordStateBeforeOneStep(int mon) {
    DisplayInfo di;
    if (!GetDisplayInfoForMonitor(mon, di)) return;
    HMONITOR h = mon < (int)g_gui.monitors.size() ? g_gui.monitors[mon] : nullptr;
    DXGI_OUTPUT_DESC1 desc = {};
    const bool inHdr = h && QueryFreshOutputDesc(h, desc) && desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
    const std::wstring def = QueryDisplayDefaultProfile(di.adapterId, di.sourceId, true);
    const bool defOurs = IsDesktopLutProfileName(def);
    const float edid = EdidHdrPeakNits(di);
    float stored;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        stored = g_gui.monitorSettings[mon].hdrMHC.panelPeakNits;
    }
    const NeutralPeak peak = DecideNeutralProfilePeak(stored, inHdr, desc.MaxLuminance, defOurs, edid);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (!ValidMonitor(mon)) return;
        MHCSettings& m = g_gui.monitorSettings[mon].hdrMHC;
        m.panelPeakNits = peak.nits;
        m.priorDefaultProfile = (!def.empty() && !defOurs) ? def : std::wstring();
    }
    static const char* const kSource[] = { "reported by Windows", "kept", "the EDID's", "unknown: the 1000-nit default" };
    std::wcout << L"[SDR gamma 2.2] monitor " << mon << L": profile peak " << peak.nits << L" nits ("
               << kSource[(int)peak.source] << L"; Windows reported " << desc.MaxLuminance << L", EDID " << edid
               << L"), HDR default before: " << (def.empty() ? L"none" : def.c_str()) << std::endl;
}

}  // namespace

Result SetSdrGamma22(int mon, bool on) {
    if (!ValidMonitor(mon)) return Done(false);
    if (on) {
        bool ours;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            const MHCSettings& m = g_gui.monitorSettings[mon].hdrMHC;
            ours = !m.profileName.empty();
        }
        // The first profile of ours on the display: it reports the peak HDR apps were told before it, and the default it
        // replaces is remembered for the switch-off.
        if (!ours) RecordStateBeforeOneStep(mon);
        Result r = MhcSetDesktopGamma(mon, true);   // the existing switch's path, its neutral profile included
        if (!r.ok()) return r;
        bool installed;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            const MHCSettings& m = g_gui.monitorSettings[mon].hdrMHC;
            installed = m.enabled && !m.profileName.empty();
        }
        // The setting stays on (the next desktop gamma swap tries the profile again, as for the existing switch).
        if (!installed)
            return Told(mon, L"mhc", Fail(notify::Sev::Warning, L"SDR gamma 2.2: the HDR profile could not be installed"));
        notify::Post(notify::Sev::Info, mon, L"mhc", L"SDR gamma 2.2 on");
        return r;
    }
    bool onlyDg, installed;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        const MHCSettings& m = g_gui.monitorSettings[mon].hdrMHC;
        onlyDg = ui::HdrMhcOnlyDesktopGamma(m);
        installed = !m.profileName.empty();
    }
    // Something else lives in the profile (set since): only the gamma goes, as the GRAYSCALE inspector's switch does.
    if (!onlyDg || !installed) return MhcSetDesktopGamma(mon, false);
    if (RefusedDuringCalibration(mon)) return Done(false);   // the session measures the profile it set up
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        g_gui.monitorSettings[mon].hdrMHC.desktopGammaEnabled = false;
    }
    // The global flag follows what is still configured (another display may keep its desktop gamma).
    const bool anyDg = AnyDesktopGammaConfigured();
    g_userDesktopGammaMode.store(anyDg);
    if (!g_gammaWhitelistActive.load()) g_desktopGammaMode.store(anyDg);
    // The profile existed only for the gamma: the Remove path takes it away (the identity profile is associated first,
    // because Windows keeps applying the last MHC2 transform after a plain disassociation), with its variants.
    std::wstring prior;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        prior = g_gui.monitorSettings[mon].hdrMHC.priorDefaultProfile;
    }
    MhcRemove(mon, true);   // saves, refreshes
    // The display ends where it started: the user's own previous default (the Windows HDR Calibration app's profile, a
    // vendor's) is the default again. It stayed in the list all along. Associated AFTER the identity profile, so an MHC2
    // one is the last transform Windows applies; then the identity association goes (Windows keeps applying the last
    // MHC2 transform after a disassociation, so a non-MHC2 previous default leaves the identity's neutral one).
    if (!prior.empty()) {
        DisplayInfo di;
        const bool exists = GetFileAttributesW(ColorDirPath(prior).c_str()) != INVALID_FILE_ATTRIBUTES;
        if (ShouldRestorePriorDefault(prior, exists) && GetDisplayInfoForMonitor(mon, di) &&
            ReassociateMHC2Profile(prior, di.adapterId, di.sourceId, true)) {
            DisengageIdentityMhcProfile(mon, true);
            std::wcout << L"[SDR gamma 2.2] monitor " << mon << L": " << prior << L" is the HDR default again" << std::endl;
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (ValidMonitor(mon)) g_gui.monitorSettings[mon].hdrMHC.priorDefaultProfile.clear();
            }
            SaveSettings();
        } else {
            notify::Post(notify::Sev::Warning, mon, L"mhc", L"Previous HDR profile not restored",
                         L"It is no longer installed, or Windows refused it: " + prior +
                             L"\nThe display runs a neutral profile instead.");
        }
    }
    ReconcileLiveLayers("desktop gamma");
    notify::Post(notify::Sev::Info, mon, L"mhc", L"SDR gamma 2.2 off");
    Finish(mon);
    return Done();
}

Result MhcSetStageBypass(int mon, bool hdr, MhcStage stage, bool bypass) {
    if (!ValidMonitor(mon) || RefusedDuringCalibration(mon)) return Done(false);
    const bool primaries = stage == MhcStage::Primaries;
    uint8_t perm = 0;
    bool installed = false;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MHCSettings& m = MhcOf(g_gui.monitorSettings[mon], hdr);
        bool& flag = primaries ? m.bypassPrimaries : m.bypassGrayscale;
        if (flag == bypass) return Done(false);
        flag = bypass;
        installed = m.enabled && !m.profileName.empty();
        perm = ComputeMhcPermutation(m, hdr);
    }
    // The variant without the stage (built on first use, cached after) replaces the installed one. If it
    // cannot be built the switch goes back: the setting must never disagree with what is on screen.
    if (installed && !SwapMhcToPermutation(mon, hdr, perm)) {
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (ValidMonitor(mon)) {
                MHCSettings& m = MhcOf(g_gui.monitorSettings[mon], hdr);
                (primaries ? m.bypassPrimaries : m.bypassGrayscale) = !bypass;
            }
        }
        Finish(mon);
        return Told(mon, L"mhc", Fail(notify::Sev::Warning, L"Stage switch failed: the profile variant could not be built"));
    }
    SaveSettings();
    Finish(mon);
    return Done();
}

// ---------------------------------------------------------------------------------------------------
// Display name

Result SetDisplayLabel(int slot, int liveIndex, const std::wstring& label) {
    // Trim, and keep it a name (one line, bounded).
    size_t a = label.find_first_not_of(L" \t\r\n");
    size_t b = label.find_last_not_of(L" \t\r\n");
    std::wstring clean = (a == std::wstring::npos) ? std::wstring() : label.substr(a, b - a + 1);
    for (wchar_t& c : clean) if (c < 32) c = L' ';
    if (clean.size() > 64) clean.resize(64);

    bool found = false, changed = false;
    int liveFound = -1;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        auto apply = [&](MonitorSettings& ms) { found = true; changed = ms.label != clean; ms.label = clean; };
        if (slot >= 0) {
            for (size_t i = 0; i < g_gui.monitorSettings.size() && !found; i++)
                if (g_gui.monitorSettings[i].slot == slot) { apply(g_gui.monitorSettings[i]); liveFound = (int)i; }
            for (MonitorSettings& ms : g_gui.parkedSettings)
                if (!found && ms.slot == slot) apply(ms);
        } else if (liveIndex >= 0 && liveIndex < (int)g_gui.monitorSettings.size()) {
            apply(g_gui.monitorSettings[liveIndex]);
            liveFound = liveIndex;
        }
    }
    if (!found) return Done(false);
    if (changed) SaveSettings();
    GuiNotifySettingsChanged(liveFound);
    return Done(changed);
}

// ---------------------------------------------------------------------------------------------------
// Settings

void SetHotkeyEnabled(int hotkeyId, bool on) {
    if (hotkeyId == HOTKEY_GAMMA) g_hotkeyGammaEnabled.store(on);
    else if (hotkeyId == HOTKEY_HDR_TOGGLE) g_hotkeyHdrEnabled.store(on);
    else if (hotkeyId == HOTKEY_ANALYSIS) g_hotkeyAnalysisEnabled.store(on);
    else return;
    // The hub window holds every hotkey for the session (RegisterHotkeys); its thread registers it.
    if (g_gui.hwndMain) PostMessage(g_gui.hwndMain, WM_HOTKEY_REGISTER, hotkeyId, on ? 1 : 0);
    SaveSettings();
    SyncClassicGlobalControls();
}

// ---------------------------------------------------------------------------------------------------
// Global hotkeys

namespace {

struct HotkeyDef { int id; const std::atomic<bool>* enabled; const std::atomic<char>* key; const wchar_t* name; };
const HotkeyDef kHotkeys[] = {
    { HOTKEY_GAMMA, &g_hotkeyGammaEnabled, &g_hotkeyGammaKey, L"Desktop gamma" },
    { HOTKEY_HDR_TOGGLE, &g_hotkeyHdrEnabled, &g_hotkeyHdrKey, L"HDR" },
    { HOTKEY_ANALYSIS, &g_hotkeyAnalysisEnabled, &g_hotkeyAnalysisKey, L"Analysis" },
};

const HotkeyDef* FindHotkey(int id) {
    for (const HotkeyDef& h : kHotkeys) if (h.id == id) return &h;
    return nullptr;
}

void RegisterOne(const HotkeyDef& h) {
    HWND hub = g_gui.hwndMain;
    const UINT vk = (UINT)(unsigned char)h.key->load();
    if (!hub || !vk) return;
    UnregisterHotKey(hub, h.id);   // a re-registration replaces (a second RegisterHotKey of the same id would add one)
    if (RegisterHotKey(hub, h.id, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, vk)) return;
    // Another program holds the combination: this hotkey stays unregistered, deliberately and visibly.
    wchar_t text[96];
    swprintf_s(text, L"%s hotkey Win+Shift+%c is taken by another program", h.name, (wchar_t)vk);
    std::wcerr << L"[Hotkeys] " << text << L" (error " << GetLastError() << L")" << std::endl;
    notify::Post(notify::Sev::Warning, -1, L"hotkey", text);
}

// Some display with desktop gamma configured is in HDR now (a fresh DisplayConfig + DXGI query: a key press only).
bool DesktopGammaDisplayInHdr() {
    std::vector<int> configured;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (int i = 0; i < (int)g_gui.monitorSettings.size(); i++)
            if (g_gui.monitorSettings[i].hdrMHC.desktopGammaEnabled) configured.push_back(i);
    }
    for (int i : configured) {
        if (i >= (int)g_gui.monitors.size()) continue;
        DisplayInfo di;
        if (GetDisplayInfoForMonitor(i, di) && IsDisplayInHdrMode(di, g_gui.monitors[i])) return true;
    }
    return false;
}

void ToggleDesktopGammaHotkey() {
    switch (DecideGammaHotkey(AnyDesktopGammaConfigured(), DesktopGammaDisplayInHdr())) {
    case GammaHotkey::NotSetUp:
        ShowOSD(L"Desktop gamma: not set up");
        return;
    case GammaHotkey::NoHdrDisplay:
        return;   // nothing visible to change (as before): SDR shows no desktop gamma
    case GammaHotkey::Toggle:
        break;
    }
    const bool newMode = !g_desktopGammaMode.load();
    g_desktopGammaMode.store(newMode);
    // The user's choice: the whitelist respects it.
    g_userDesktopGammaMode.store(newMode);
    // Toggled while a whitelisted app runs: the override lasts until that app exits.
    if (g_gammaWhitelistActive.load()) {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        g_gammaWhitelistOverrideProcess = g_gammaWhitelistMatch;
        for (wchar_t& c : g_gammaWhitelistOverrideProcess) c = towlower(c);
        g_gammaWhitelistMatch.clear();
        g_gammaWhitelistUserOverride.store(true);
        g_gammaWhitelistActive.store(false);
        std::wcout << L"Gamma whitelist: user override active until " << g_gammaWhitelistOverrideProcess << L" exits"
                   << std::endl;
    }
    SwapDgForAllMonitors(newMode);   // the HDR MHC profiles' variants with / without it
    std::cout << "Gamma mode: " << (newMode ? "Desktop (2.2)" : "Content (sRGB)") << std::endl;
    ShowOSD(newMode ? L"Gamma: 2.2" : L"Gamma: sRGB");
    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);   // an overlay carrying desktop gamma (no profile) redraws
}

}  // namespace

void RegisterHotkeys() {
    for (const HotkeyDef& h : kHotkeys)
        if (h.enabled->load()) RegisterOne(h);
}

void UnregisterHotkeys() {
    if (!g_gui.hwndMain) return;
    for (const HotkeyDef& h : kHotkeys) UnregisterHotKey(g_gui.hwndMain, h.id);
}

void ApplyHotkeyRegistration(int hotkeyId, bool on) {
    const HotkeyDef* h = FindHotkey(hotkeyId);
    if (!h || !g_gui.hwndMain) return;
    if (on) RegisterOne(*h);
    else UnregisterHotKey(g_gui.hwndMain, hotkeyId);
}

void OnHotkey(int hotkeyId) {
    if (g_appShuttingDown.load()) return;
    if (hotkeyId == HOTKEY_GAMMA) {
        ToggleDesktopGammaHotkey();
    } else if (hotkeyId == HOTKEY_ANALYSIS) {
        SetAnalysis(!g_analysisEnabled.load());
    } else if (hotkeyId == HOTKEY_HDR_TOGGLE) {
        // DisplayConfig alone: nothing of DesktopLUT's needs to run. A running overlay re-detects the mode after a
        // delay (ACM HDR->SDR keeps the FP16 format, so its format check does not see it): its own thread's timer.
        if (ToggleHdrOnFocusedMonitor()) {
            HWND overlay = g_mainHwnd.load();
            if (overlay) PostMessage(overlay, WM_OVERLAY_HDR_TOGGLED, 0, 0);
        }
    }
}

// ---------------------------------------------------------------------------------------------------
// Analysis overlay

bool AnalysisOn() { return g_analysisEnabled.load(); }

Result SetAnalysis(bool on) {
    const AnalysisSwitch step = DecideAnalysisSwitch(on, g_analysisEnabled.load(), g_userStopped.load(),
                                                     g_appShuttingDown.load());
    if (step == AnalysisSwitch::Unchanged) return Done(false);
    if (step == AnalysisSwitch::RefusedSuspended) {
        ShowOSD(L"Analysis: live layers suspended");
        return Told(-1, L"analysis", Fail(notify::Sev::Warning, L"Analysis needs the live layers: resume them first"));
    }
    if (step == AnalysisSwitch::Show) {
        CreateAnalysisOverlay(GetModuleHandle(nullptr));   // the GUI thread's (made at startup; again if it is gone)
        ShowAnalysisOverlay();
    } else {
        HideAnalysisOverlay();
    }
    // What measures for it follows: in hook mode the analysis-only thread (or a running full overlay) via the
    // re-evaluation; in overlay mode the overlay, whose displays are built at a start (display 0 joins or leaves).
    if (g_gui.isRunning) {
        if (g_dwmHookMode.load()) DwmHookReevaluateOverlay();
        else if (AnalysisNeedsOverlayRestart(false, true, OverlayPipelineMonitorsDiffer(), LiveLayersNeeded()))
            RestartLiveLayers();
    }
    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);   // an auto-sleeping overlay picks the change up
    // Nothing running: the live layers start for analysis alone. Analysis off: whatever ran only for it stops.
    ReconcileLiveLayers("analysis");
    GuiNotifySettingsChanged(-1);
    Result r;
    r.changed = true;
    return r;
}

void SetStartMinimized(bool on) {
    g_startMinimized.store(on);
    SaveSettings();
    SyncClassicGlobalControls();
}

void SetRunAtStartup(bool on) {
    SetStartupEnabled(on);
    RefreshStartupEnabledCache();
    SyncClassicGlobalControls();
}

void SetConsoleLog(bool on) {
    g_consoleEnabled.store(on);
    if (on) {
        if (GetConsoleWindow() == nullptr && AllocConsole()) {
            FILE* fp;
            freopen_s(&fp, "CONOUT$", "w", stdout);
            freopen_s(&fp, "CONOUT$", "w", stderr);
            SetConsoleOutputCP(CP_UTF8);   // the narrow log lines are UTF-8 ("→" read as CP-1252 is "â†'")
            std::cout.clear();
            std::cerr.clear();
            std::wcout.clear();
            std::wcerr.clear();
            std::cout << "Console enabled" << std::endl;
        }
    } else if (GetConsoleWindow() != nullptr) {
        FreeConsole();
    }
    SaveSettings();
    SyncClassicGlobalControls();
}

void SetDlcFolder(const std::wstring& folder) {
    g_dlcFolder = folder;
    if (folder.empty()) g_dlcFolderLast.clear();
    SaveSettings();
}

void SetDlcFolderLastUsed(const std::wstring& folder) {
    if (g_dlcFolderLast == folder) return;
    g_dlcFolderLast = folder;
    SaveSettings();
}

void SetUiTheme(int theme) {
    g_uiTheme = theme >= 0 && theme <= 2 ? theme : 0;
    SaveSettings();
}

void SetUiDlcCharts(const std::wstring& encoded) {
    g_uiDlcCharts = encoded;
    SaveSettings();
}

void SetRecalReminderDays(int days) {
    g_recalReminderDays = days < 0 ? 0 : (days > 3650 ? 3650 : days);
    SaveSettings();
}

void SetVrrWhitelistEnabled(bool on) {
    g_vrrWhitelistEnabled.store(on);
    SaveSettings();
    SyncClassicGlobalControls();
}

void SetGammaWhitelist(const std::wstring& raw) {
    g_gammaWhitelistRaw = NormalizeWhitelistRaw(raw);
    ParseGammaWhitelist();
    SaveSettings();
}

void SetVrrWhitelist(const std::wstring& raw) {
    g_vrrWhitelistRaw = NormalizeWhitelistRaw(raw);
    ParseVrrWhitelist();
    SaveSettings();
}

// ---------------------------------------------------------------------------------------------------
// Live layers

void ReloadLiveLayers() {
    if (g_userStopped) {
        g_userStopped = false;
        notify::Post(notify::Sev::Info, -1, L"live", L"Live layers resumed");
    }
    SaveSettings();
    if (g_gui.isRunning) StopProcessing();
    StartProcessing();   // not running and nothing needed: still the MHC-only hygiene + status
    GuiNotifySettingsChanged(-1);
}

}  // namespace act
