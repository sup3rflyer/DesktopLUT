// DesktopLUT - monitor_identity.cpp
// Identity-keyed re-attachment of per-monitor settings. See monitor_identity.h.

#include "monitor_identity.h"
#include "displayconfig.h"
#include "globals.h"
#include "settings.h"
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

bool IsHardwareOnly(const std::wstring& edidId) { return edidId.find(L'-') == std::wstring::npos; }

}  // namespace

std::wstring EdidHardwarePart(const std::wstring& edidId) {
    size_t dash = edidId.find(L'-');
    return dash == std::wstring::npos ? edidId : edidId.substr(0, dash);
}

bool EdidIdsCompatible(const std::wstring& a, const std::wstring& b) {
    if (a.empty() || b.empty()) return true;
    if (EqualsNoCase(a, b)) return true;
    // A serial-less read ("AUS322A") is the same model as "AUS322A-<serial>": compatible.
    // Two different serials, or two different models, are not.
    if (IsHardwareOnly(a) != IsHardwareOnly(b)) return EqualsNoCase(EdidHardwarePart(a), EdidHardwarePart(b));
    return false;
}

MonitorMatchResult MatchMonitorSettings(const std::vector<LiveDisplay>& live,
                                        const std::vector<MonitorSettings>& previousLive,
                                        const std::vector<MonitorSettings>& parked,
                                        const std::wstring& today) {
    MonitorMatchResult result;

    std::vector<PoolEntry> pool;
    pool.reserve(previousLive.size() + parked.size());
    for (size_t i = 0; i < previousLive.size(); i++) pool.push_back({ previousLive[i], (int)i });
    for (const auto& p : parked) pool.push_back({ p, -1 });

    // Storage slots: every slot any pool entry holds is in use (a parked display keeps its
    // slot forever, an identity-less [Display] section on disk keeps its number), and a new
    // display gets the lowest free slot below the cap. Two entries claiming one slot can only
    // come from a bug or a hand edit; the later one is moved to a free slot rather than letting
    // the two silently overwrite each other's section.
    std::vector<bool> slotUsed(kMaxDisplaySlots, false);
    for (auto& e : pool) {
        if (e.ms.slot < 0) continue;
        if (e.ms.slot >= kMaxDisplaySlots) {
            result.log.push_back(L"Settings slot " + std::to_wstring(e.ms.slot) + L" is out of range; reassigning");
            e.ms.slot = -1;
            continue;
        }
        if (slotUsed[e.ms.slot]) {
            result.log.push_back(L"Duplicate settings slot [Display" + std::to_wstring(e.ms.slot) +
                                 L"] for " + Describe(e.ms.identity) + L"; moving it to a free slot");
            e.ms.slot = -1;
            continue;
        }
        slotUsed[e.ms.slot] = true;
    }
    auto allocSlot = [&]() -> int {
        for (int s = 0; s < kMaxDisplaySlots; s++) {
            if (!slotUsed[s]) { slotUsed[s] = true; return s; }
        }
        return -1;
    };
    // Entries that lost a duplicate/out-of-range slot above and carry an identity get a new one.
    for (auto& e : pool) {
        if (e.ms.slot < 0 && !e.ms.identity.empty()) {
            e.ms.slot = allocSlot();
        }
    }

    auto findFirst = [&](auto pred) -> int {
        for (size_t k = 0; k < pool.size(); k++) {
            if (!pool[k].taken && pred(pool[k])) return (int)k;
        }
        return -1;
    };
    // Same-index candidate first (breaks ties between twins), then any.
    auto findPreferIndex = [&](int i, auto pred) -> int {
        int k = findFirst([&](const PoolEntry& e) { return e.prevIndex == i && pred(e); });
        return k >= 0 ? k : findFirst(pred);
    };

    for (size_t i = 0; i < live.size(); i++) {
        const LiveDisplay& d = live[i];
        int pick = -1;
        std::wstring how;

        if (d.identified) {
            if (!d.identity.devicePath.empty()) {
                // The device path's instance segment is connector-scoped, not unit-scoped: a
                // same-model replacement on the same connector has the same path. The EDID id
                // (with its serial) must agree too, or the replacement inherits the old unit's
                // per-unit calibration.
                pick = findFirst([&](const PoolEntry& e) {
                    return EqualsNoCase(e.ms.identity.devicePath, d.identity.devicePath) &&
                           EdidIdsCompatible(e.ms.identity.edidId, d.identity.edidId);
                });
                if (pick >= 0) how = L"device path";
            }
            if (pick < 0 && !d.identity.edidId.empty()) {
                pick = findPreferIndex((int)i, [&](const PoolEntry& e) {
                    return EqualsNoCase(e.ms.identity.edidId, d.identity.edidId);
                });
                if (pick < 0) {
                    // One side read without a serial (a transient EDID/SetupAPI failure).
                    pick = findPreferIndex((int)i, [&](const PoolEntry& e) {
                        return !e.ms.identity.edidId.empty() &&
                               EdidIdsCompatible(e.ms.identity.edidId, d.identity.edidId);
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
            // Refresh the device path (connector move) and name. Never downgrade the stored EDID
            // id: a serial-less read of the same model keeps the serial-bearing id on record.
            std::wstring keepEdid = out.identity.edidId;
            out.identity = d.identity;
            if (!keepEdid.empty() && !IsHardwareOnly(keepEdid) &&
                (d.identity.edidId.empty() ||
                 (IsHardwareOnly(d.identity.edidId) &&
                  EqualsNoCase(EdidHardwarePart(keepEdid), d.identity.edidId)))) {
                out.identity.edidId = keepEdid;
            }
            if (out.slot < 0) {
                out.slot = allocSlot();
                if (out.slot < 0) how += L" (no free settings slot: not persisted)";
            }
            if (!today.empty()) {
                if (out.firstSeen.empty()) out.firstSeen = today;
                out.lastSeen = today;
            }
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
        // Kept: anything with an identity, a legacy origin, or a storage slot (an identity-less
        // [Display<slot>] section from disk: nothing can match it, but it is the user's data and
        // its slot number must never be handed to another display).
        if (!e.ms.identity.empty() || e.ms.legacyIndex >= 0 || e.ms.slot >= 0) {
            result.parked.push_back(std::move(e.ms));
        } else {
            result.log.push_back(L"Dropping an anonymous settings entry (never identified, no legacy origin)");
        }
    }

    return result;
}

bool LiveSettingsAttachmentChanged(const std::vector<MonitorSettings>& before,
                                   const std::vector<MonitorSettings>& after) {
    if (before.size() != after.size()) return true;
    for (size_t i = 0; i < before.size(); i++) {
        if (before[i].slot != after[i].slot) return true;
        if (before[i].legacyIndex != after[i].legacyIndex) return true;
        if (!EqualsNoCase(before[i].identity.devicePath, after[i].identity.devicePath)) return true;
        if (!EqualsNoCase(before[i].identity.edidId, after[i].identity.edidId)) return true;
    }
    return false;
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

MonitorResolveOutcome ResolveMonitorSettings(const std::vector<HMONITOR>& monitors) {
    // Windows queries (DisplayConfig + EDID registry reads) outside the lock.
    std::vector<LiveDisplay> live = QueryLiveDisplays(monitors);
    const std::wstring today = TodayIsoDate();

    MonitorResolveOutcome outcome;
    MonitorMatchResult r;
    {
        // One hold across snapshot + match + write-back: the matcher is pure and O(n²) on a
        // handful of entries, and a writer that slipped in between two lock scopes (the
        // whitelist thread's permutation swap writes profileName/activePerm) would be lost.
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        r = MatchMonitorSettings(live, g_gui.monitorSettings, g_gui.parkedSettings, today);
        outcome.liveChanged = LiveSettingsAttachmentChanged(g_gui.monitorSettings, r.live);
        // Element-wise, inside the reserved capacity: never swap the buffer out from
        // under a reference some caller may still hold (see reserve(64) in gui_layout).
        g_gui.monitorSettings.resize(r.live.size());
        for (size_t i = 0; i < r.live.size(); i++) {
            g_gui.monitorSettings[i] = std::move(r.live[i]);
        }
        g_gui.parkedSettings = std::move(r.parked);
    }
    for (const auto& line : r.log) std::wcout << L"[Monitor identity] " << line << std::endl;
    outcome.allIdentified = r.allIdentified;
    return outcome;
}
