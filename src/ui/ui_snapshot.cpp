#include "ui_snapshot.h"

#include "globals.h"
#include "displayconfig.h"
#include "dwm_inject.h"
#include "actions/notify.h"
#include "actions/reconcile.h"
#include "dlc/dlc_scan.h"
#include "desktoplut_ipc_server.h"
#include "processing.h"   // IsProcessingTransitionActive
#include "windows_tonemap.h"   // EdidHdrPeakNits
#include "fald_fade_status.h"  // FaldFadeGovReportFor
#include <dxgi1_6.h>
#include <mutex>

namespace ui {

namespace {

struct ModeEntry {
    HMONITOR hmon = nullptr;
    Mode mode = Mode::Unknown;
    std::wstring name;
    float maxLuminance = 0;   // DXGI_OUTPUT_DESC1::MaxLuminance (what apps are told)
    float edidPeakNits = 0;   // the HDR peak its EDID declares (EdidHdrPeakNits, cached per device path)
};
std::vector<ModeEntry> g_modeCache;   // GUI thread only

Mode FromDisplayColorMode(DisplayColorMode m) {
    switch (m) {
    case DisplayColorMode::HDR:    return Mode::HDR;
    case DisplayColorMode::AcmSdr: return Mode::AcmSdr;
    case DisplayColorMode::SDR:    return Mode::SDR;
    default:                       return Mode::Unknown;
    }
}

const ModeEntry* FindMode(HMONITOR h) {
    for (const ModeEntry& e : g_modeCache) if (e.hmon == h) return &e;
    return nullptr;
}

}  // namespace

bool RefreshReportedPeaks() {
    if (g_modeCache.empty()) return false;
    IDXGIFactory1* factory = nullptr;   // fresh: a cached factory keeps the luminance it saw first
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory)))) return false;
    bool changed = false;
    IDXGIAdapter1* adapter = nullptr;
    for (UINT a = 0; factory->EnumAdapters1(a, &adapter) != DXGI_ERROR_NOT_FOUND; a++) {
        IDXGIOutput* output = nullptr;
        for (UINT o = 0; adapter->EnumOutputs(o, &output) != DXGI_ERROR_NOT_FOUND; o++) {
            IDXGIOutput6* output6 = nullptr;
            if (SUCCEEDED(output->QueryInterface(__uuidof(IDXGIOutput6), reinterpret_cast<void**>(&output6)))) {
                DXGI_OUTPUT_DESC1 desc = {};
                if (SUCCEEDED(output6->GetDesc1(&desc)))
                    for (ModeEntry& e : g_modeCache)
                        if (e.hmon == desc.Monitor && e.maxLuminance != desc.MaxLuminance) {
                            e.maxLuminance = desc.MaxLuminance;
                            changed = true;
                        }
                output6->Release();
            }
            output->Release();
        }
        adapter->Release();
    }
    factory->Release();
    return changed;
}

void RefreshModeCache() {
    std::vector<HMONITOR> mons;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        mons = g_gui.monitors;
    }
    std::vector<ModeEntry> fresh;
    fresh.reserve(mons.size());
    for (HMONITOR h : mons) {
        ModeEntry e;
        e.hmon = h;
        DisplayInfo di;
        const bool haveDi = GetDisplayInfoForHMonitor(h, di);
        if (haveDi) e.name = di.name;
        if (haveDi) e.edidPeakNits = EdidHdrPeakNits(di);
        DXGI_OUTPUT_DESC1 desc = {};
        if (QueryFreshOutputDesc(h, desc)) {
            e.maxLuminance = desc.MaxLuminance;
            // Same verdict as windows.query_monitors: DXGI sees HDR / FP16 SDR but not ACM, which comes
            // from DisplayConfig (displayconfig.h DisplayColorMode).
            const bool hdrActive = desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
            const bool fp16Sdr = desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709;
            DisplayColorModeResult cm = haveDi ? QueryDisplayColorMode(di, hdrActive, fp16Sdr)
                                               : ClassifyDisplayColorMode(hdrActive, fp16Sdr, false, 0, false, false);
            e.mode = FromDisplayColorMode(cm.mode);
        }
        fresh.push_back(std::move(e));
    }
    g_modeCache = std::move(fresh);
}

void TakeSnapshot(Snapshot& out) {
    out.live.clear();
    out.parked.clear();
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        out.live.reserve(g_gui.monitorSettings.size());
        for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
            DisplayRow r;
            r.liveIndex = (int)i;
            r.hmon = i < g_gui.monitors.size() ? g_gui.monitors[i] : nullptr;
            r.settings = g_gui.monitorSettings[i];
            out.live.push_back(std::move(r));
        }
        out.parked.reserve(g_gui.parkedSettings.size());
        for (const MonitorSettings& ms : g_gui.parkedSettings) {
            DisplayRow r;
            r.settings = ms;
            r.name = !ms.label.empty() ? ms.label : DisplayLabel(ms.identity, std::wstring());
            out.parked.push_back(std::move(r));
        }
    }
    for (DisplayRow& r : out.live) {
        const ModeEntry* m = FindMode(r.hmon);
        r.mode = m ? m->mode : Mode::Unknown;
        r.reportedPeakNits = m && r.mode == Mode::HDR ? m->maxLuminance : 0.0f;
        r.edidPeakNits = m ? m->edidPeakNits : 0.0f;
        r.name = !r.settings.label.empty() ? r.settings.label : DisplayLabel(r.settings.identity, m ? m->name : std::wstring());
        MONITORINFO mi = { sizeof(mi) };
        if (r.hmon && GetMonitorInfo(r.hmon, &mi)) {
            r.primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
            r.widthPx = mi.rcMonitor.right - mi.rcMonitor.left;
            r.heightPx = mi.rcMonitor.bottom - mi.rcMonitor.top;
        }
        // the flank fade governor's status per mode (the overlay's table, or the hook's section by position)
        for (int h = 0; h < 2; h++) FaldFadeGovReportFor(r.liveIndex, r.hmon, h == 1, r.fadeGov[h]);
        for (int h = 0; h < 2; h++) FaldCoreGovReportFor(r.liveIndex, r.hmon, h == 1, r.coreGov[h]);
    }

    // DLC records for every display, live and remembered.
    out.dlc = dlc::Current();
    // Identical twins: how many displays (live or remembered) share each hardware id.
    std::vector<std::string> ids;
    for (const DisplayRow& r : out.live) ids.push_back(dlc::HardwareIdOf(r.settings.identity.edidId, r.settings.identity.devicePath));
    for (const DisplayRow& r : out.parked) ids.push_back(dlc::HardwareIdOf(r.settings.identity.edidId, r.settings.identity.devicePath));
    auto twins = [&](const DisplayRow& r) {
        const std::string id = dlc::HardwareIdOf(r.settings.identity.edidId, r.settings.identity.devicePath);
        if (id.empty()) return 1;
        int n = 0;
        for (const std::string& x : ids) if (_stricmp(x.c_str(), id.c_str()) == 0) n++;
        return n > 0 ? n : 1;
    };
    // The engine state first: the DLC records read against what reaches the screen. A Start / Stop in progress
    // (a cube change or a pipe call restarts the engine, pumping frames while it joins) keeps the last state: the
    // lights and MEASURED would otherwise flash a fault for a restart nobody needs to see.
    if (!IsProcessingTransitionActive()) {
        out.running = g_gui.isRunning.load();
        out.hookActive = g_dwmHookMode.load() && IsDwmHookActive();
    }
    out.hookMode = g_dwmHookMode.load();
    out.hookActive = out.hookActive && out.hookMode;
    out.suspended = act::LiveLayersSuspended();
    out.calibrating = IsCalibrationOrLiveEditActive();   // locks of its own: outside the settings lock above
    auto attach = [&](DisplayRow& r) {
        const MonitorSettings& ms = r.settings;
        dlc::LiveDisplay& live = r.dlcLive;
        live = dlc::LiveDisplay();
        live.hardwareId = dlc::HardwareIdOf(ms.identity.edidId, ms.identity.devicePath);
        live.monitor = r.liveIndex;
        live.sameModelCount = twins(r);
        // What RUNS: a bypassed stage is not DLC's record any more (its verification no longer describes it).
        const MHCSettings* mhc[2] = { &ms.sdrMHC, &ms.hdrMHC };
        for (int k = 0; k < 2; k++)
            if (mhc[k]->enabled && !mhc[k]->profileName.empty() && !mhc[k]->bypassPrimaries && !mhc[k]->bypassGrayscale)
                live.mhcSource[k] = mhc[k]->sourceFilePath;
        live.lut[0] = LiveLutPath(ms, false);
        live.lut[1] = LiveLutPath(ms, true);
        r.dlcMatch = out.dlc->folder.empty() ? dlc::DisplayMatch() : dlc::MatchDisplay(*out.dlc, live);
        // Suspended live layers, or the engine down on the mode the display is in, leave a connected display's cube
        // off the screen (MHC stays): the panel's record is read against what runs, as for a bypassed cube
        // (CubeOffScreen). The display is still matched by what it is configured with, and the sidebar's reminder is
        // the configured record's — how old the calibration is does not change with a suspend or a restart.
        dlc::LiveDisplay running = live;
        bool cubeOff[2] = {};
        for (int k = 0; k < 2; k++) {
            cubeOff[k] = !running.lut[k].empty() && CubeOffScreen(out, r, k == 1);
            if (cubeOff[k]) running.lut[k].clear();
        }
        r.reminderDue = false;
        for (int k = 0; k < 2; k++) {
            if (r.dlcMatch.name.empty()) {
                r.dlcRecord[k] = dlc::ModeRecord();
                continue;
            }
            r.dlcRecord[k] = dlc::RecordFor(*out.dlc, r.dlcMatch.name, k, running, g_recalReminderDays);
        }
        if (!r.dlcMatch.name.empty()) {
            const int k = PipelineIsHdr(r.mode) ? 1 : 0;
            r.reminderDue = cubeOff[k] ? dlc::RecordFor(*out.dlc, r.dlcMatch.name, k, live, g_recalReminderDays).reminderDue
                                       : r.dlcRecord[k].reminderDue;
        }
    };
    for (DisplayRow& r : out.live) attach(r);
    for (DisplayRow& r : out.parked) attach(r);

    // The display the DLC run measures: the DLC display it names (its profile name), else its hardware id, else
    // the DesktopLUT index it was started on. One display or none, never a guess between two.
    out.run = dlc::CurrentLive();
    if (out.run->phase != dlc::RunPhase::None) {
        const dlc::Live& run = *out.run;
        const std::string name = run.state && !run.state->display.empty() ? run.state->display : run.active.display;
        const std::string& hw = run.active.hardwareId;
        const int mon = run.state && run.state->monitor >= 0 ? run.state->monitor : run.active.monitor;
        auto unique = [&](auto pred) {
            int found = -1;
            for (int i = 0; i < (int)out.live.size(); i++) {
                if (!pred(out.live[i])) continue;
                if (found >= 0) return -2;   // more than one
                found = i;
            }
            return found;
        };
        int at = name.empty() ? -1 : unique([&](const DisplayRow& r) { return _stricmp(r.dlcMatch.name.c_str(), name.c_str()) == 0; });
        if (at == -1 && !hw.empty()) at = unique([&](const DisplayRow& r) { return _stricmp(r.dlcLive.hardwareId.c_str(), hw.c_str()) == 0; });
        if (at == -1 && mon >= 0 && mon < (int)out.live.size()) at = mon;
        if (at >= 0) out.live[(size_t)at].dlcRun = true;
    }

    out.lastStatus.clear();
    out.lastStatusSev = notify::Sev::Info;
    for (auto it = notify::Snapshot(32); !it.empty(); it.pop_back()) {
        const notify::Event& e = it.back();
        if (e.source == L"status" || e.sev == notify::Sev::Warning || e.sev == notify::Sev::Error) {
            out.lastStatus = e.title;
            out.lastStatusSev = e.sev;
            break;
        }
    }
}

}  // namespace ui
