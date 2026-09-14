// DesktopLUT - monitor_identity.cpp
// Identity-keyed re-attachment of per-monitor settings. See monitor_identity.h.

#include "monitor_identity.h"
#include "displayconfig.h"
#include "globals.h"
#include <algorithm>
#include <cwctype>
#include <iostream>
#include <mutex>

namespace {

bool EqualsNoCase(const std::wstring& a, const std::wstring& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++) {
        if (towlower(a[i]) != towlower(b[i])) return false;
    }
    return true;
}

struct PoolEntry {
    MonitorSettings ms;
    int prevIndex;   // position in previousLive, -1 for parked
    bool taken = false;
};

std::wstring Describe(const DisplayIdentity& id) {
    std::wstring s = id.friendlyName.empty() ? L"(unnamed)" : id.friendlyName;
    if (!id.edidId.empty()) s += L" [" + id.edidId + L"]";
    return s;
}

}  // namespace

MonitorMatchResult MatchMonitorSettings(const std::vector<LiveDisplay>& live,
                                        const std::vector<MonitorSettings>& previousLive,
                                        const std::vector<MonitorSettings>& parked) {
    MonitorMatchResult result;

    std::vector<PoolEntry> pool;
    pool.reserve(previousLive.size() + parked.size());
    for (size_t i = 0; i < previousLive.size(); i++) pool.push_back({ previousLive[i], (int)i });
    for (const auto& p : parked) pool.push_back({ p, -1 });

    // Storage slots are never reused: a new display always gets max+1.
    int nextSlot = 0;
    for (const auto& e : pool) nextSlot = (std::max)(nextSlot, e.ms.slot + 1);

    auto findFirst = [&](auto pred) -> int {
        for (size_t k = 0; k < pool.size(); k++) {
            if (!pool[k].taken && pred(pool[k])) return (int)k;
        }
        return -1;
    };

    for (size_t i = 0; i < live.size(); i++) {
        const LiveDisplay& d = live[i];
        int pick = -1;
        std::wstring how;

        if (d.identified) {
            if (!d.identity.devicePath.empty()) {
                pick = findFirst([&](const PoolEntry& e) {
                    return EqualsNoCase(e.ms.identity.devicePath, d.identity.devicePath);
                });
                if (pick >= 0) how = L"device path";
            }
            if (pick < 0 && !d.identity.edidId.empty()) {
                // Prefer the twin that already sat at this index; otherwise the first twin.
                pick = findFirst([&](const PoolEntry& e) {
                    return e.prevIndex == (int)i && EqualsNoCase(e.ms.identity.edidId, d.identity.edidId);
                });
                if (pick < 0) {
                    pick = findFirst([&](const PoolEntry& e) {
                        return EqualsNoCase(e.ms.identity.edidId, d.identity.edidId);
                    });
                }
                if (pick >= 0) how = L"EDID id (connector changed)";
            }
            if (pick < 0) {
                pick = findFirst([&](const PoolEntry& e) {
                    return e.prevIndex == (int)i && e.ms.identity.empty() && e.ms.legacyIndex < 0;
                });
                if (pick >= 0) how = L"same index, previously unidentified";
            }
            if (pick < 0) {
                pick = findFirst([&](const PoolEntry& e) {
                    return e.ms.legacyIndex == (int)i && e.ms.identity.empty();
                });
                if (pick >= 0) how = L"migrated from legacy [Monitor" + std::to_wstring(i) + L"]";
            }
        } else {
            result.allIdentified = false;
            pick = findFirst([&](const PoolEntry& e) { return e.prevIndex == (int)i; });
            if (pick >= 0) how = L"same index (identity unavailable)";
            if (pick < 0) {
                pick = findFirst([&](const PoolEntry& e) {
                    return e.ms.legacyIndex == (int)i && e.ms.identity.empty();
                });
                if (pick >= 0) how = L"legacy [Monitor" + std::to_wstring(i) + L"] by index (identity unavailable)";
            }
        }

        MonitorSettings out;
        if (pick >= 0) {
            pool[pick].taken = true;
            out = pool[pick].ms;
        } else {
            how = d.identified ? L"new display, default settings"
                               : L"unknown display, default settings (identity unavailable, not persisted)";
        }

        if (d.identified) {
            out.identity = d.identity;          // refreshes the device path after a connector move
            if (out.slot < 0) out.slot = nextSlot++;
        }

        std::wstring line = L"Monitor " + std::to_wstring(i) + L": ";
        line += d.identified ? Describe(d.identity) : L"(unidentified)";
        line += L" <- " + how;
        if (out.slot >= 0) line += L" [Display" + std::to_wstring(out.slot) + L"]";
        result.log.push_back(line);
        result.live.push_back(std::move(out));
    }

    for (auto& e : pool) {
        if (e.taken) continue;
        if (!e.ms.identity.empty() || e.ms.legacyIndex >= 0) {
            if (!e.ms.identity.empty() && e.ms.slot < 0) e.ms.slot = nextSlot++;
            result.parked.push_back(std::move(e.ms));
        } else {
            result.log.push_back(L"Dropping an anonymous settings entry (never identified, no legacy origin)");
        }
    }

    return result;
}

std::vector<LiveDisplay> QueryLiveDisplays(const std::vector<HMONITOR>& monitors) {
    std::vector<LiveDisplay> live;
    live.reserve(monitors.size());
    for (HMONITOR h : monitors) {
        LiveDisplay d;
        d.hmon = h;
        d.identified = QueryDisplayIdentity(h, d.identity);
        live.push_back(std::move(d));
    }
    return live;
}

bool ResolveMonitorSettings(const std::vector<HMONITOR>& monitors) {
    std::vector<LiveDisplay> live = QueryLiveDisplays(monitors);

    std::vector<MonitorSettings> previousLive, parked;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        previousLive = g_gui.monitorSettings;
        parked = g_gui.parkedSettings;
    }

    MonitorMatchResult r = MatchMonitorSettings(live, previousLive, parked);
    for (const auto& line : r.log) std::wcout << L"[Monitor identity] " << line << std::endl;

    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        // Element-wise, inside the reserved capacity: never swap the buffer out from
        // under a reference some caller may still hold (see reserve(64) in gui_layout).
        g_gui.monitorSettings.resize(r.live.size());
        for (size_t i = 0; i < r.live.size(); i++) {
            g_gui.monitorSettings[i] = std::move(r.live[i]);
        }
        g_gui.parkedSettings = std::move(r.parked);
    }
    return r.allIdentified;
}
