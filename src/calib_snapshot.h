// DesktopLUT - calib_snapshot.h
// What a calibration session (calibration.enter .. calibration.exit over the DLC pipe)
// captured so `calibration.exit(restore_snapshot=true)` can put the user's own setup back.
//
// WHY THIS IS ITS OWN TYPE (fable audit Phase 9, ticket T2 — DLC/docs/audits/fable/phase-9.md).
// The store used to be a single slot in CalibState, overwritten on EVERY enter. A run that
// crashed leaves the session ACTIVE with the monitor already cleared to the neutral slate, so
// the next enter snapshotted that CLEARED state and a later restore handed the user back the
// slate instead of their own MHC profile, white balance, correction grayscale and 3D LUT.
//
// The rule this type enforces: within one session the FIRST capture of a monitor wins, and a
// session's captures are dropped only when the session ends. Captures are per monitor because a
// session may enter more than one — the old single slot silently dropped all but the last.

#pragma once

#include <map>

#include "types.h"

struct CalibSnapshotStore {
    struct Entry {
        MonitorSettings settings;  // the monitor's state BEFORE the session cleared it
        bool wasHdr = false;       // the mode being calibrated when it was captured
    };

    // Capture `settings` for `monitor` unless this session already holds a capture for it.
    // Returns true when it captured, false when an earlier (original) capture was kept —
    // which is what `calibration.enter` reports back as `snapshot_retained`.
    bool CaptureIfAbsent(int monitor, const MonitorSettings& settings, bool isHdr) {
        if (monitor < 0) return false;
        if (entries.find(monitor) != entries.end()) return false;
        Entry e;
        e.settings = settings;
        e.wasHdr = isHdr;
        entries.emplace(monitor, e);
        return true;
    }

    bool Has(int monitor) const { return entries.find(monitor) != entries.end(); }
    bool Empty() const { return entries.empty(); }
    void Clear() { entries.clear(); }

    // Keyed by monitor index, so a restore can walk every monitor the session touched.
    std::map<int, Entry> entries;
};
