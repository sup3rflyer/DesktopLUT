#include "actions/actions.h"
#include "actions/reconcile.h"

#include "gui.h"
#include "gui_mhc.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "dwm_inject.h"
#include "displayconfig.h"
#include "fald.h"
#include "mhc.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "windows_tonemap.h"
#include <cmath>
#include <cstdio>
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
        }
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
    // Hook-only mode registers hotkeys on the hub window, overlay mode on the render thread's window.
    HWND target = g_hookOnlyHotkeys ? g_gui.hwndMain : (HWND)g_mainHwnd.load();
    if (target) PostMessage(target, WM_HOTKEY_REGISTER, hotkeyId, on ? 1 : 0);
    SaveSettings();
    SyncClassicGlobalControls();
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
