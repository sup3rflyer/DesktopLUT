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

// Pure matcher — no Windows calls, fully testable.
//
// Pool = previousLive (indexed by their previous enumeration position) + parked.
// For each live display, in order, the first rule that finds an untaken pool entry wins:
//   identified display:
//     1. same device path (case-insensitive)           — exact panel on the exact connector
//     2. same EDID id (case-insensitive)               — the panel moved to another connector;
//        a candidate that sat at this same index is preferred over other twins
//     3. entry at this same index that has NO identity — a display we could not identify earlier
//     4. unclaimed legacy [Monitor<i>] entry           — pre-identity INI, adopted by index once
//     5. a fresh default entry
//   unidentified display (identity query failed):
//     1. whatever sat at this index before (identified or not) — never blank a display out
//     2. unclaimed legacy [Monitor<i>] entry
//     3. a fresh default entry (no identity, not persisted until identified)
// A matched identified display has the live identity stamped on it (device path refreshes
// when a panel moves connector) and gets a storage slot if it had none.
// Untaken entries with an identity or a legacy origin are parked; anonymous leftovers drop.
MonitorMatchResult MatchMonitorSettings(const std::vector<LiveDisplay>& live,
                                        const std::vector<MonitorSettings>& previousLive,
                                        const std::vector<MonitorSettings>& parked);

// Query the identity of each HMONITOR (Windows).
std::vector<LiveDisplay> QueryLiveDisplays(const std::vector<HMONITOR>& monitors);

// Rebuild g_gui.monitorSettings (in place, within its reserved capacity) and
// g_gui.parkedSettings for `monitors`. GUI thread only — must not run while an
// editor dialog holds a reference into g_gui.monitorSettings (g_mhcEditDialogOpen).
// Returns false when some live display could not be identified (caller retries).
bool ResolveMonitorSettings(const std::vector<HMONITOR>& monitors);
