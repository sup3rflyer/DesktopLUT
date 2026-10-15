// DesktopLUT - windows_tonemap.cpp
// The Windows tone map (windows_tonemap.h): resolve what each HDR display should tell Windows its peak is, and make
// Windows report it — the HDR MHC profile's luminance-tag variant, else the DisplayConfig override.

#include "windows_tonemap.h"

#include "actions/notify.h"
#include "actions/reconcile.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "displayconfig.h"
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "dwm_inject.h"   // IsDwmHookActive
#include "settings.h"
#include <dxgi1_6.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <atomic>
#include <iostream>
#include <map>
#include <mutex>

bool WindowsToneMapReportsMax(WindowsToneMap mode, bool toneMapOn, bool liveLayersSuspended, bool appExiting) {
    switch (mode) {
    case WindowsToneMap::Bypass: return true;
    case WindowsToneMap::Panel:  return false;
    default:                     return toneMapOn && !liveLayersSuspended && !appExiting;
    }
}

bool ToneMapTakesFullRange(const TonemapSettings& tm) {
    return tm.enabled && (tm.dynamicPeak || tm.sourcePeakNits >= kWindowsToneMapBypassNits - 0.5f);
}

const wchar_t* WindowsToneMapKey(WindowsToneMap mode) {
    switch (mode) {
    case WindowsToneMap::Bypass: return L"bypass";
    case WindowsToneMap::Panel:  return L"panel";
    default:                     return L"auto";
    }
}

WindowsToneMap ParseWindowsToneMap(const wchar_t* text) {
    if (text && _wcsicmp(text, L"bypass") == 0) return WindowsToneMap::Bypass;
    if (text && _wcsicmp(text, L"panel") == 0) return WindowsToneMap::Panel;
    return WindowsToneMap::Auto;
}

namespace {

// GUI thread only (the reconcile, the exit release, the interface's mode cache).
struct EdidValues {
    MonitorPrimaries primaries;   // valid=false: unreadable
    EdidHdrLuminance luminance;   // maxNits 0: not declared
};
std::map<std::wstring, EdidValues> g_edidCache;          // by device path (an EDID does not change under one)

struct OverrideState {
    float sentNits = 0.0f;   // the peak this process last told Windows for the display (0 = none since a reapply)
    bool sent = false;       // this process overrode it at some point: the panel's values go back on release
    ULONGLONG lastMismatchResend = 0;   // Windows reported something else (another HDR profile associated): resent
};
std::map<std::wstring, OverrideState> g_overrides;       // by device path
bool g_anySent = false;                                  // any display: nothing to go back to before the first send
constexpr ULONGLONG kMismatchResendMs = 60000;           // at most once a minute, should Windows not keep the value

bool g_exiting = false;
int g_failures = 0;                  // consecutive reconciles with a failed swap / override
ULONGLONG g_retryNotBefore = 0;      // a periodic reconcile leaves failed work to the backoff timer until then
bool g_warned = false;               // the user was told the Windows tone map could not be applied

std::atomic<bool> g_requestPending{ false };
std::atomic<bool> g_reapplyPending{ false };

constexpr UINT kWaitRetryMs = 3000;                       // a hold (calibration, live edit, dialog, maintenance)
constexpr UINT kFailRetryMs[] = { 2000, 5000, 15000, 60000, 300000 };
constexpr int kWarnAfterFailures = 3;

// An EDID that was read is kept (it does not change under a device path); one that could not be read (a display
// still coming up) is read again next time — cached, a failure would send sRGB primaries for the rest of the run.
EdidValues EdidFor(const DisplayInfo& di) {
    auto it = g_edidCache.find(di.devicePath);
    if (it != g_edidCache.end()) return it->second;
    EdidValues v;
    std::vector<BYTE> edid;
    if (di.devicePath.empty() || !ReadEDIDForDisplay(di, edid)) return v;
    ParseEDIDChromaticity(edid.data(), edid.size(), v.primaries);
    ParseEDIDHdrLuminance(edid.data(), edid.size(), v.luminance);
    g_edidCache.emplace(di.devicePath, v);
    return v;
}

// In HDR (the override takes only then), with what Windows reports now.
bool InHdr(HMONITOR h, DXGI_OUTPUT_DESC1& desc) {
    desc = {};
    return h && QueryFreshOutputDesc(h, desc) && desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
}

// A gamut a display could have: every coordinate inside the chromaticity diagram's box, a triangle of real area, the
// white inside it. Some EDIDs (TVs, capture devices, virtual displays) carry zeros or garbage there.
bool PlausiblePrimaries(const MonitorPrimaries& p) {
    if (!p.valid) return false;
    const float c[8] = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
    for (float v : c) if (!(v > 0.0f && v < 0.9f)) return false;
    auto cross = [](float ax, float ay, float bx, float by, float px, float py) {
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax);
    };
    const float area = cross(p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By);
    if (std::fabs(area) < 0.02f) return false;   // sRGB's is ~0.22 (doubled), the narrowest real panels well above
    const float s1 = cross(p.Rx, p.Ry, p.Gx, p.Gy, p.Wx, p.Wy), s2 = cross(p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy),
                s3 = cross(p.Bx, p.By, p.Rx, p.Ry, p.Wx, p.Wy);
    return (s1 > 0 && s2 > 0 && s3 > 0) || (s1 < 0 && s2 < 0 && s3 < 0);
}

// The primaries the override sends (it replaces what Windows reports for them too): the EDID's when they make sense,
// else what Windows reports now, else none (the override's sRGB / D65).
MonitorPrimaries OverridePrimaries(const EdidValues& edid, const DXGI_OUTPUT_DESC1& desc) {
    if (PlausiblePrimaries(edid.primaries)) return edid.primaries;
    MonitorPrimaries p;
    p.Rx = desc.RedPrimary[0];   p.Ry = desc.RedPrimary[1];
    p.Gx = desc.GreenPrimary[0]; p.Gy = desc.GreenPrimary[1];
    p.Bx = desc.BluePrimary[0];  p.By = desc.BluePrimary[1];
    p.Wx = desc.WhitePoint[0];   p.Wy = desc.WhitePoint[1];
    p.valid = true;
    if (PlausiblePrimaries(p)) return p;
    return MonitorPrimaries();
}

// Nothing tone maps, whatever the intent says: the hook gave up (the watchdog's flap cap, until a start re-arms it),
// or, in overlay mode, the VRR passthrough hides the overlay for a whitelisted game (it would get no tone map at all).
bool ToneMapEngineOff() {
    if (g_dwmHookMode.load())
        return g_dwmHookReinjectCount >= DWM_HOOK_WATCHDOG_MAX_REINJECTS && !IsDwmHookActive();
    return g_vrrWhitelistActive.load();
}

void ArmRetry(UINT ms) {
    if (g_gui.hwndMain && !g_appShuttingDown.load()) SetTimer(g_gui.hwndMain, WINDOWS_TONEMAP_TIMER_ID, ms, nullptr);
}

struct Entry {
    int index = -1;
    bool want = false;
    bool installed = false;      // an HDR MHC profile of ours is installed (it carries the peak)
    bool preview = false;        // a live edit's identity scanout runs instead (the swap waits for it)
    uint8_t perm = 0;
    HMONITOR hmon = nullptr;
};

}  // namespace

void RequestWindowsToneMap(bool reapply) {
    if (reapply) g_reapplyPending.store(true);
    if (g_requestPending.exchange(true)) return;   // one message carries every request made before it runs
    if (!g_gui.hwndMain || !PostMessage(g_gui.hwndMain, WM_WINDOWS_TONEMAP, 0, 0)) g_requestPending.store(false);
}

void ReconcileWindowsToneMap(const char* why, bool reapply) {
    // This run serves every request made so far (a queued message runs one more, cheap: nothing differs).
    g_requestPending.store(false);
    if (g_reapplyPending.exchange(false)) reapply = true;
    const char* reason = why ? why : "";
    const bool periodic = strcmp(reason, "periodic") == 0;
    const bool suspended = act::LiveLayersSuspended();
    const bool engineOff = ToneMapEngineOff();
    // A calibration or live edit owns the profiles (and what Windows reports while it measures), an open MHC dialog
    // or a pinned settings vector holds references into it: the flags follow, the swaps and overrides wait.
    const bool hold = !g_exiting && (IsCalibrationOrLiveEditActive() || g_mhcEditDialogOpen.load() ||
                                     g_monitorSettingsPins > 0);

    std::vector<Entry> entries;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        entries.reserve(g_gui.monitorSettings.size());
        for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
            MonitorSettings& ms = g_gui.monitorSettings[i];
            Entry e;
            e.index = (int)i;
            e.want = WindowsToneMapReportsMax(ms.windowsToneMap,
                                              ToneMapTakesFullRange(ms.hdrColorCorrection.tonemap) && !engineOff,
                                              suspended, g_exiting);
            ms.hdrMHC.reportPeakMax = e.want;   // a re-bake installs the variant that says so (ComputeMhcPermutation)
            e.installed = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty();
            e.preview = !ms.hdrMHC.previewScanoutName.empty();
            e.perm = ms.hdrMHC.activePerm;
            e.hmon = i < g_gui.monitors.size() ? g_gui.monitors[i] : nullptr;
            entries.push_back(e);
        }
    }

    bool wait = false, failed = false, changed = false, swapped = false;
    const bool inBackoff = periodic && GetTickCount64() < g_retryNotBefore;
    for (const Entry& e : entries) {
        if (e.installed) {
            // The HDR profile carries it: the variant whose luminance tags say so (the same curves).
            if (((e.perm & MHCSettings::PERM_PEAK_MAX) != 0) == e.want) continue;
            if (hold || e.preview) { wait = true; continue; }
            if (inBackoff) continue;
            // The processing thread's start pass re-associates the active profile under this lock: a swap beside it
            // could leave the other variant associated.
            std::unique_lock<std::mutex> maintenance(g_mhcMaintenanceMutex, std::try_to_lock);
            if (!maintenance.owns_lock()) { wait = true; continue; }
            std::cout << "[Windows tone map] monitor " << e.index << ": the HDR profile reports "
                      << (e.want ? "10000 nits" : "the panel's peak") << " (" << reason << ")" << std::endl;
            if (SwapMhcPermBits(e.index, true, e.want ? MHCSettings::PERM_PEAK_MAX : 0,
                                e.want ? 0 : MHCSettings::PERM_PEAK_MAX)) changed = swapped = true;
            else failed = true;
            continue;
        }

        // No HDR profile of ours: the DisplayConfig override, which Windows takes only while the display is in HDR
        // (the switch into HDR asks for a reapply).
        if (!e.want && !g_anySent) continue;   // nothing told, nothing to take back: no query at all
        if (hold) { wait = true; continue; }
        if (inBackoff) continue;
        DisplayInfo di;
        if (!e.hmon || !GetDisplayInfoForHMonitor(e.hmon, di) || di.devicePath.empty()) continue;
        OverrideState& st = g_overrides[di.devicePath];
        if (!e.want && !st.sent) continue;      // this display was never told anything (or was given back already)
        DXGI_OUTPUT_DESC1 desc;
        if (!InHdr(e.hmon, desc)) {
            st.sentNits = 0.0f;   // leaving HDR drops it: the next HDR session gets it again
            continue;
        }
        const EdidValues edid = EdidFor(di);
        float peak = 0.0f, frameAvg = 0.0f;
        if (e.want) {
            peak = frameAvg = kWindowsToneMapBypassNits;
        } else if (edid.luminance.maxNits > 0.0f) {
            // Back to what Windows reports for the display on its own. Only where this process told it otherwise:
            // a display it never touched may carry someone else's values (the Windows HDR Calibration app's profile).
            peak = edid.luminance.maxNits;
            frameAvg = edid.luminance.maxFrameAvgNits > 0.0f ? edid.luminance.maxFrameAvgNits : peak;
        } else {
            std::cout << "[Windows tone map] monitor " << e.index << ": no HDR peak in the EDID to go back to; "
                      << "Windows keeps 10000 nits until the next mode change" << std::endl;
            st = OverrideState();
            continue;
        }
        // Sent already and still what Windows reports: nothing to do. While 10000 is wanted and Windows reports otherwise
        // (another HDR profile was associated since — the Windows HDR Calibration app's), it is sent again, at most
        // once a minute. Giving it back is sent once: after that the display is left alone.
        bool resend = reapply || st.sentNits != peak;
        if (!resend && e.want && std::fabs(desc.MaxLuminance - peak) > 1.0f &&
            GetTickCount64() - st.lastMismatchResend >= kMismatchResendMs) {
            st.lastMismatchResend = GetTickCount64();
            resend = true;
        }
        if (!resend) continue;
        if (SetDisplayPeakOverride(di, OverridePrimaries(edid, desc), edid.luminance.minNits, peak, frameAvg)) {
            changed = true;
            if (e.want) {
                st.sentNits = peak;
                st.sent = true;
                g_anySent = true;
            } else {
                st = OverrideState();   // given back: nothing of this process's left on the display
            }
        } else {
            failed = true;
        }
    }

    if (failed) {
        const int step = (std::min)(g_failures, (int)(sizeof(kFailRetryMs) / sizeof(kFailRetryMs[0])) - 1);
        g_failures++;
        g_retryNotBefore = GetTickCount64() + kFailRetryMs[step];
        ArmRetry(kFailRetryMs[step]);
        if (g_failures >= kWarnAfterFailures && !g_warned) {
            g_warned = true;
            notify::Post(notify::Sev::Warning, -1, L"mhc", L"Windows tone map could not be applied",
                         L"Windows may still report the panel's peak; retrying");
        }
    } else if (!wait) {
        if (g_failures > 0 && changed) std::cout << "[Windows tone map] applied after a retry" << std::endl;
        g_failures = 0;
        g_retryNotBefore = 0;
        g_warned = false;
    }
    if (wait && !failed) ArmRetry(kWaitRetryMs);
    if (swapped) SaveSettings();                 // the active variant: the next start re-associates this one
    if (changed) GuiNotifySettingsChanged(-1);   // the interface's reported peak
}

void ReleaseWindowsToneMapForExit(bool sessionEnding) {
    if (sessionEnding) return;
    g_exiting = true;
    ReconcileWindowsToneMap("exit", false);
}

float EdidHdrPeakNits(const DisplayInfo& display) {
    return display.devicePath.empty() ? 0.0f : EdidFor(display).luminance.maxNits;
}
