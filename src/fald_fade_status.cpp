// The flank fade governor's status for the interface, Events and the pipe — see fald_fade_status.h.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_fade_status.h"
#include "fald.h"
#include "dwm_inject.h"
#include "globals.h"
#include "types.h"
#include "actions/notify.h"

#include <windows.h>
#include <cstdio>
#include <map>
#include <mutex>
#include <tuple>

namespace {

std::mutex g_overlayMutex;
std::map<std::pair<int, bool>, FaldFadeGovReport> g_overlay;   // (monitor index, hdr) -> the render thread's last

FaldFadeGovReport FromStatus(const FaldFadeGovernorStatus& st) {
    FaldFadeGovReport r;
    r.known = true;
    r.state = st.state;
    r.p90Ms = st.p90Us / 1000.0; r.budgetMs = st.budgetUs / 1000.0;
    r.contentMs = st.contentMs; r.refreshMs = st.refreshMs;
    r.generation = st.generation;
    return r;
}

bool MonitorPosition(HMONITOR h, int& left, int& top) {
    MONITORINFO mi = { sizeof(mi) };
    if (!h || !GetMonitorInfo(h, &mi)) return false;
    left = mi.rcMonitor.left; top = mi.rcMonitor.top;
    return true;
}

bool FromHookBlock(const DwmHookFaldStatus& b, int left, int top, bool hdr, FaldFadeGovReport& out) {
    for (const DwmHookFaldFadeStatusEntry& e : b.entry) {
        if (!e.used || e.left != left || e.top != top || e.isHdr != (hdr ? 1u : 0u)) continue;
        out = FaldFadeGovReport();
        out.known = true;
        out.state = (FaldFadeGovState)(e.state <= 4u ? e.state : 0u);
        out.p90Ms = e.p90Us / 1000.0; out.budgetMs = e.budgetUs / 1000.0;
        out.contentMs = e.contentMs; out.refreshMs = e.refreshMs;
        out.generation = e.generation;
        return true;
    }
    return false;
}

// The Events line's mode (its DISPLAY column names the monitor; -1 = a hook entry no live monitor matches)
std::wstring ModeName(int monitorIndex, bool hdr) {
    wchar_t b[48];
    if (monitorIndex < 0) swprintf_s(b, L"%s (unmatched display)", hdr ? L"HDR" : L"SDR");
    else swprintf_s(b, L"%s", hdr ? L"HDR" : L"SDR");
    return b;
}

}  // namespace

void FaldFadeGovPublish(int monitorIndex, bool hdr, const FaldFadeGovernorStatus& st) {
    std::lock_guard<std::mutex> lk(g_overlayMutex);
    g_overlay[{ monitorIndex, hdr }] = FromStatus(st);
}

bool FaldFadeGovReportFor(int monitorIndex, HMONITOR hmon, bool hdr, FaldFadeGovReport& out) {
    out = FaldFadeGovReport();
    if (g_dwmHookMode.load()) {
        int left = 0, top = 0;   // the DLL keys its entries by the monitor's desktop position
        if (!IsDwmHookActive() || !MonitorPosition(hmon, left, top)) return false;
        DwmHookFaldStatus b;
        if (!ReadDwmHookFaldStatus(b)) return false;
        return FromHookBlock(b, left, top, hdr, out);
    }
    std::lock_guard<std::mutex> lk(g_overlayMutex);
    auto it = g_overlay.find({ monitorIndex, hdr });
    if (it == g_overlay.end()) return false;
    out = it->second;
    return true;
}

std::wstring FaldFadeGovExplain(const FaldFadeGovReport& r) {
    wchar_t b[400] = {};
    switch (r.state) {
    case FaldFadeGovState::SuspendedBudget:
        swprintf_s(b, L"Paused: %.1f ms of GPU time per content frame (p90) is over its %.1f ms budget (15 %% of %.1f ms).\n"
                      L"FALD runs without the fade. It retries after a mode change, a slower content rate, or OFF and ON.",
                   r.p90Ms, r.budgetMs, r.contentMs);
        break;
    case FaldFadeGovState::SuspendedWeakGpu:
        swprintf_s(b, L"Paused: an integrated GPU or one with under 1 GB of VRAM cannot run it in time.\n"
                      L"FALD runs without the fade. FaldFlankFadeForce=1 in [General] runs it anyway.");
        break;
    case FaldFadeGovState::Forced:
        swprintf_s(b, L"Forced on (FaldFlankFadeForce=1): no GPU budget. %.1f ms per content frame (p90).", r.p90Ms);
        break;
    case FaldFadeGovState::Running:
        if (r.budgetMs > 0.0) swprintf_s(b, L"%.1f ms of GPU time per content frame (p90), budget %.1f ms.", r.p90Ms, r.budgetMs);
        break;
    default:
        break;
    }
    return b;
}

void FaldFadeGovPostEvent(int monitorIndex, bool hdr, const FaldFadeGovReport& r) {
    const std::wstring who = ModeName(monitorIndex, hdr);
    wchar_t num[96] = {};
    switch (r.state) {
    case FaldFadeGovState::SuspendedBudget:   // terse: the numbers (the FALD panel's tooltip explains)
        swprintf_s(num, L": %.1f ms p90 / %.1f ms budget", r.p90Ms, r.budgetMs);
        notify::Post(notify::Sev::Warning, monitorIndex, L"fald", L"Flank fade paused: GPU over budget", who + num);
        break;
    case FaldFadeGovState::SuspendedWeakGpu:
        notify::Post(notify::Sev::Warning, monitorIndex, L"fald", L"Flank fade paused: weak GPU", who);
        break;
    case FaldFadeGovState::Forced:
        notify::Post(notify::Sev::Info, monitorIndex, L"fald", L"Flank fade forced on", who);
        break;
    case FaldFadeGovState::Running:
        notify::Post(notify::Sev::Info, monitorIndex, L"fald", L"Flank fade resumed", who);
        break;
    default:
        break;
    }
}

void FaldFadeGovPollHookEvents() {
    static std::map<std::tuple<int, int, bool>, uint32_t> seen;   // (left, top, hdr) -> the generation last posted
    DwmHookFaldStatus b;
    if (!IsDwmHookActive() || !ReadDwmHookFaldStatus(b)) return;
    std::vector<std::pair<int, HMONITOR>> mons;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        for (size_t i = 0; i < g_gui.monitors.size(); i++) mons.push_back({ (int)i, g_gui.monitors[i] });
    }
    for (const DwmHookFaldFadeStatusEntry& e : b.entry) {
        if (!e.used) continue;
        const auto key = std::make_tuple((int)e.left, (int)e.top, e.isHdr != 0u);
        auto it = seen.find(key);
        const bool first = it == seen.end();
        if (!first && it->second == e.generation) continue;
        seen[key] = e.generation;
        FaldFadeGovReport r;
        if (!FromHookBlock(b, e.left, e.top, e.isHdr != 0u, r)) continue;
        if (first && r.state != FaldFadeGovState::SuspendedBudget && r.state != FaldFadeGovState::SuspendedWeakGpu) continue;
        int index = -1;
        for (const auto& m : mons) {
            int l = 0, t = 0;
            if (MonitorPosition(m.second, l, t) && l == e.left && t == e.top) { index = m.first; break; }
        }
        FaldFadeGovPostEvent(index, e.isHdr != 0u, r);
    }
}
