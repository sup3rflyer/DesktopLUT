#include "ui_snapshot.h"

#include "globals.h"
#include "displayconfig.h"
#include "dwm_inject.h"
#include "actions/notify.h"
#include <dxgi1_6.h>
#include <mutex>

namespace ui {

namespace {

struct ModeEntry {
    HMONITOR hmon = nullptr;
    Mode mode = Mode::Unknown;
    std::wstring name;
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
        DXGI_OUTPUT_DESC1 desc = {};
        if (QueryFreshOutputDesc(h, desc)) {
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
        r.name = !r.settings.label.empty() ? r.settings.label : DisplayLabel(r.settings.identity, m ? m->name : std::wstring());
        MONITORINFO mi = { sizeof(mi) };
        if (r.hmon && GetMonitorInfo(r.hmon, &mi)) {
            r.primary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
            r.widthPx = mi.rcMonitor.right - mi.rcMonitor.left;
            r.heightPx = mi.rcMonitor.bottom - mi.rcMonitor.top;
        }
    }

    out.running = g_gui.isRunning.load();
    out.hookMode = g_dwmHookMode.load();
    out.hookActive = out.hookMode && IsDwmHookActive();

    out.lastStatus.clear();
    for (auto it = notify::Snapshot(32); !it.empty(); it.pop_back()) {
        if (it.back().source == L"status") { out.lastStatus = it.back().title; break; }
    }
}

}  // namespace ui
