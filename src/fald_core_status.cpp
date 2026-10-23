// The core layer's rate-cap governor status for the interface, Events and the pipe — see fald_core_status.h.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_core_status.h"
#include "../shared/fald_core_mode.h"   // FaldCoreModeName (the tooltip)
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
std::map<std::pair<int, bool>, FaldCoreGovReport> g_overlay;   // (monitor index, hdr) -> the render thread's last

void Extra(FaldCoreGovReport& r, uint32_t extra) {   // dwm_hook_config.h DwmHookFaldCoreExtra
    r.convSo = DwmHookFaldCoreExtraSo(extra); r.convTune = DwmHookFaldCoreExtraTune(extra);
    r.coreMode = DwmHookFaldCoreExtraCoreMode(extra); r.coreOn = DwmHookFaldCoreExtraCoreOn(extra) != 0;
}

FaldCoreGovReport FromStatus(const FaldCoreGovernorStatus& st, uint32_t extra) {
    FaldCoreGovReport r;
    r.known = true;
    r.state = st.state;
    r.p90Ms = st.p90Us / 1000.0; r.budgetMs = st.budgetUs / 1000.0;
    r.refreshMs = st.refreshMs; r.spacingMs = st.spacingMs; r.contentMs = st.contentMs;
    r.generation = st.generation;
    Extra(r, extra);
    return r;
}

bool MonitorPosition(HMONITOR h, int& left, int& top) {
    MONITORINFO mi = { sizeof(mi) };
    if (!h || !GetMonitorInfo(h, &mi)) return false;
    left = mi.rcMonitor.left; top = mi.rcMonitor.top;
    return true;
}

bool FromHookBlock(const DwmHookFaldStatus& b, int left, int top, bool hdr, FaldCoreGovReport& out) {
    for (const DwmHookFaldCoreStatusEntry& e : b.core) {
        if (!e.used || e.left != left || e.top != top || e.isHdr != (hdr ? 1u : 0u)) continue;
        out = FaldCoreGovReport();
        out.known = true;
        out.state = (FaldCoreGovState)(e.state <= 3u ? e.state : 0u);
        out.p90Ms = e.p90Us / 1000.0; out.budgetMs = e.budgetUs / 1000.0;
        out.refreshMs = e.refreshMs; out.spacingMs = e.spacingMs; out.contentMs = e.contentMs;
        out.generation = e.generation;
        Extra(out, e.extra);
        return true;
    }
    return false;
}

std::wstring ModeName(int monitorIndex, bool hdr) {
    wchar_t b[48];
    if (monitorIndex < 0) swprintf_s(b, L"%s (unmatched display)", hdr ? L"HDR" : L"SDR");
    else swprintf_s(b, L"%s", hdr ? L"HDR" : L"SDR");
    return b;
}

}  // namespace

void FaldCoreGovPublish(int monitorIndex, bool hdr, const FaldCoreGovernorStatus& st, uint32_t extra) {
    std::lock_guard<std::mutex> lk(g_overlayMutex);
    g_overlay[{ monitorIndex, hdr }] = FromStatus(st, extra);
}

bool FaldCoreGovReportFor(int monitorIndex, HMONITOR hmon, bool hdr, FaldCoreGovReport& out) {
    out = FaldCoreGovReport();
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

std::wstring FaldCoreGovExplain(const FaldCoreGovReport& r) {
    wchar_t b[420] = {};
    switch (r.state) {
    case FaldCoreGovState::Capped:
        swprintf_s(b, L"Rate-capped: %.2f ms of GPU time per full run (p90) is over its %.2f ms budget (15 %% of the %.2f ms refresh).\n"
                      L"The full run happens every %.0f ms; the presents between keep the last fields and run only the pixel pass\n"
                      L"(the picture stays corrected, the dimming follows with that delay). Released when the cost fits with 25 %% headroom.",
                   r.p90Ms, r.budgetMs, r.refreshMs, r.spacingMs);
        break;
    case FaldCoreGovState::Forced:
        swprintf_s(b, L"Forced (FaldCoreForce=1): no GPU budget. %.2f ms per full run (p90).", r.p90Ms);
        break;
    case FaldCoreGovState::Running:
        if (r.budgetMs > 0.0) swprintf_s(b, L"%.2f ms of GPU time per full run (p90), budget %.2f ms (15 %% of the refresh interval).", r.p90Ms, r.budgetMs);
        break;
    default:
        break;
    }
    std::wstring s = b;
    if (r.convSo != 0u) {                      // the layer's variant / rule (dwm_hook_config.h DwmHookFaldCoreExtra)
        static const wchar_t* kTune[4] = { L"the measured policy", L"cached", L"tuning", L"tuned" };
        wchar_t v[200] = {};
        swprintf_s(v, L"%sConvolution: %u sub-offset%s per thread (%s). Core rule: %S%s.", s.empty() ? L"" : L"\n", r.convSo,
                   r.convSo == 1u ? L"" : L"s", kTune[r.convTune < 4u ? r.convTune : 0u],
                   r.coreOn ? FaldCoreModeName(r.coreMode) : "C", r.coreOn ? L"" : L" (the core off)");
        s += v;
    }
    return s;
}

void FaldCoreGovPostEvent(int monitorIndex, bool hdr, const FaldCoreGovReport& r) {
    const std::wstring who = ModeName(monitorIndex, hdr);
    wchar_t num[128] = {};
    switch (r.state) {
    case FaldCoreGovState::Capped:   // terse: the numbers (the FALD panel's tooltip explains)
        swprintf_s(num, L": %.2f ms p90 / %.2f ms budget, a full run every %.0f ms", r.p90Ms, r.budgetMs, r.spacingMs);
        notify::Post(notify::Sev::Warning, monitorIndex, L"fald", L"FALD rate-capped: GPU over budget", who + num);
        break;
    case FaldCoreGovState::Forced:
        notify::Post(notify::Sev::Info, monitorIndex, L"fald", L"FALD GPU budget forced off", who);
        break;
    case FaldCoreGovState::Running:
        notify::Post(notify::Sev::Info, monitorIndex, L"fald", L"FALD full rate resumed", who);
        break;
    default:
        break;
    }
}

void FaldCoreGovPollHookEvents() {
    static std::map<std::tuple<int, int, bool>, uint32_t> seen;   // (left, top, hdr) -> the generation last posted
    DwmHookFaldStatus b;
    if (!IsDwmHookActive() || !ReadDwmHookFaldStatus(b)) return;
    std::vector<std::pair<int, HMONITOR>> mons;
    {
        std::lock_guard<std::mutex> lk(g_monitorSettingsMutex);
        for (size_t i = 0; i < g_gui.monitors.size(); i++) mons.push_back({ (int)i, g_gui.monitors[i] });
    }
    for (const DwmHookFaldCoreStatusEntry& e : b.core) {
        if (!e.used) continue;
        const auto key = std::make_tuple((int)e.left, (int)e.top, e.isHdr != 0u);
        auto it = seen.find(key);
        const bool first = it == seen.end();
        if (!first && it->second == e.generation) continue;
        seen[key] = e.generation;
        FaldCoreGovReport r;
        if (!FromHookBlock(b, e.left, e.top, e.isHdr != 0u, r)) continue;
        if (first && r.state != FaldCoreGovState::Capped) continue;   // the first Running is no news
        int index = -1;
        for (const auto& m : mons) {
            int l = 0, t = 0;
            if (MonitorPosition(m.second, l, t) && l == e.left && t == e.top) { index = m.first; break; }
        }
        FaldCoreGovPostEvent(index, e.isHdr != 0u, r);
    }
}
