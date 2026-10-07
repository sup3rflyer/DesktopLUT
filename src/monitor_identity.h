// DesktopLUT - monitor_identity.h
// Re-attaches per-monitor settings to the live display enumeration by physical
// display identity (device path / EDID), so settings follow the panel rather than
// the index Windows happens to enumerate it at.

#pragma once

#include "types.h"
#include <string>
#include <vector>

// One entry per live HMONITOR, in EnumDisplayMonitors order.
struct LiveDisplay {
    HMONITOR hmon = nullptr;
    DisplayIdentity identity;
    bool identified = false;   // false: identity query failed (mid-transition) — match positionally
};

struct MonitorMatchResult {
    std::vector<MonitorSettings> live;    // one per LiveDisplay, same order — becomes g_gui.monitorSettings
    std::vector<MonitorSettings> parked;  // every other known display — becomes g_gui.parkedSettings
    std::vector<std::wstring> log;        // one human-readable line per live display (+ drops)
    bool allIdentified = true;            // false when any live display had to be matched positionally
};

// EDID ids are "<hardware id>" or "<hardware id>-<serial>" (QueryDisplayIdentity).
std::wstring EdidHardwarePart(const std::wstring& edidId);
// True when the two ids can name the same physical panel: either is empty, they are equal
// (case-insensitive), or exactly one of them lacks the serial and the hardware ids agree.
bool EdidIdsCompatible(const std::wstring& a, const std::wstring& b);

// Pure matcher — no Windows calls, fully testable.
//
// Pool = previousLive (indexed by their previous enumeration position) + parked.
// For each live display, in order, the first rule that finds an untaken pool entry wins:
//   identified display:
//     1. same device path (case-insensitive) AND compatible EDID id — exact panel on the
//        exact connector (a same-model replacement on that connector has the same path but
//        another serial, so it does not inherit the old unit's calibration)
//     2. same EDID id (case-insensitive), then a compatible one (one side read without its
//        serial) — the panel moved to another connector; a candidate that sat at this same
//        index is preferred over other twins
//     3. entry at this same index that has NO identity — a display we could not identify earlier
//     4. unclaimed legacy [Monitor<i>] entry           — pre-identity INI, adopted by index once
//     5. a fresh default entry
//   unidentified display (identity query failed):
//     1. whatever sat at this index before (identified or not) — never blank a display out
//     2. unclaimed legacy [Monitor<i>] entry
//     3. a fresh default entry (no identity, not persisted until identified)
// A matched identified display has the live identity stamped on it (device path refreshes
// when a panel moves connector; a serial-bearing EDID id is never replaced by a serial-less
// read of the same model), gets the lowest free storage slot if it had none, and — when
// `today` is given — FirstSeen (once) / LastSeen stamps.
// Untaken entries with an identity, a legacy origin or a storage slot are parked;
// anonymous leftovers drop. Duplicate slots are repaired (the later entry moves).
MonitorMatchResult MatchMonitorSettings(const std::vector<LiveDisplay>& live,
                                        const std::vector<MonitorSettings>& previousLive,
                                        const std::vector<MonitorSettings>& parked,
                                        const std::wstring& today = std::wstring());

// True when the live vector now attaches a different display (slot / identity / legacy
// claim) at any index, or its length changed — i.e. the pipeline's per-index view is stale.
bool LiveSettingsAttachmentChanged(const std::vector<MonitorSettings>& before,
                                   const std::vector<MonitorSettings>& after);

// Query the identity of each HMONITOR (Windows).
std::vector<LiveDisplay> QueryLiveDisplays(const std::vector<HMONITOR>& monitors);

struct MonitorResolveOutcome {
    bool allIdentified = true;   // false: some live display could not be identified (caller retries)
    bool liveChanged = false;    // LiveSettingsAttachmentChanged(before, after)
};

// Rebuild g_gui.monitorSettings (in place, within its reserved capacity) and
// g_gui.parkedSettings for `monitors`. GUI thread only — must not run while an
// editor dialog holds a reference into g_gui.monitorSettings (the GUI's settings pin /
// g_mhcEditDialogOpen). Snapshot, match and write-back happen under one hold of
// g_monitorSettingsMutex, so a concurrent writer (whitelist-thread permutation swap)
// cannot be lost between them; the identity queries run before the lock.
MonitorResolveOutcome ResolveMonitorSettings(const std::vector<HMONITOR>& monitors);
