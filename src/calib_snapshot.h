// DesktopLUT - calib_snapshot.h
// What a calibration session (calibration.enter .. calibration.exit over the DLC pipe) captured,
// so calibration.exit(restore_snapshot=true) can put the user's own setup back — plus the PURE
// planning that decides what an enter captures and what an exit restores. Nothing here touches
// Win32, files, profiles or locks: desktoplut_ipc_server.cpp owns all of that and calls these
// with the data it read under its locks. tests/test_calib_snapshot.cpp drives them directly.
//
// WHY THIS IS ITS OWN TYPE (fable audit Phase 9 ticket T2; bug filed 2026-09-27):
//  * The snapshot used to be ONE slot, overwritten on every enter. A run that died without
//    calling calibration.exit leaves the session ACTIVE with the monitor already cleared, so the
//    next enter captured the CLEARED state and a restore handed the user back the neutral slate
//    instead of their MHC profile / white balance / grayscale / runtime cube / FALD settings.
//  * `hasSnapshot` was never cleared, so an exit(restore) with no session behind it (DLC's
//    `3dlut-only --abort`) restored a PREVIOUS run's pre-run snapshot over the current setup.
//  * The slot was keyed by monitor INDEX, which Windows re-enumerates on any display change.
//
// THE RULES:
//  1. Within a session the FIRST capture of a display wins; a re-enter (crashed run) keeps it.
//  2. Captures are keyed by the display's identity (device path / EDID id / settings slot — the
//     same identity monitor_identity re-attaches settings by); the live index is resolved at
//     restore time, and a capture that cannot be resolved is REPORTED, never guessed.
//  3. A restore copies the captured SETTINGS only — never identity / slot / legacyIndex, which
//     belong to the live enumeration, not to the capture.
//  4. The set of MODES entered per display is tracked, and every entered mode gets its MHC put
//     back (reinstalled, or swapped for the identity profile) on restore.
//  5. Captures survive a failed/thrown enter (the next enter keeps them) and are dropped ONLY by
//     calibration.exit — which always drops them, restore or not.
// Disk persistence of the captures (surviving a DesktopLUT restart mid-run) is NOT here; after a
// restart the store is empty and exit reports restored=false — DLC's settings backup covers it.

#pragma once

#include <cstddef>
#include <cstdint>
#include <cwctype>
#include <string>
#include <utility>
#include <vector>

#include "types.h"

struct CalibCaptureKey {
    DisplayIdentity identity;  // the display the capture belongs to (empty = identity was unavailable)
    int slot = -1;             // its [Display<slot>] at capture — stable per display (disambiguates EDID twins)
    int indexAtCapture = -1;   // live enumeration index at capture: reporting, and the ONLY key for a
                               // display captured without an identity
};

struct CalibCapture {
    CalibCaptureKey key;
    MonitorSettings settings;   // the display's settings BEFORE the session cleared anything
    bool sdrEntered = false;    // the modes this session entered on the display
    bool hdrEntered = false;
    uint64_t capturedAtMs = 0;  // the caller's clock (GetTickCount64 in the server)

    bool Entered(bool isHdr) const { return isHdr ? hdrEntered : sdrEntered; }
    void MarkEntered(bool isHdr) { (isHdr ? hdrEntered : sdrEntered) = true; }
};

// What a reader of the store (calibration.status) needs, without the settings payload.
struct CalibCaptureInfo {
    CalibCaptureKey key;
    bool sdrEntered = false;
    bool hdrEntered = false;
    uint64_t capturedAtMs = 0;
};

// The identity half of one live display (g_gui.monitorSettings[i]).
struct CalibLiveMonitor {
    DisplayIdentity identity;
    int slot = -1;
};

inline std::vector<CalibLiveMonitor> CalibLiveMonitorsFrom(const std::vector<MonitorSettings>& live) {
    std::vector<CalibLiveMonitor> out;
    out.reserve(live.size());
    for (const MonitorSettings& ms : live) out.push_back(CalibLiveMonitor{ ms.identity, ms.slot });
    return out;
}

inline bool CalibEqualsNoCase(const std::wstring& a, const std::wstring& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++)
        if (towlower(a[i]) != towlower(b[i])) return false;
    return true;
}

// Where a capture lives in the CURRENT enumeration. liveIndex -1 = unresolved, and `how` then says
// why; otherwise `how` names the rule that matched.
struct CalibResolution {
    int liveIndex = -1;
    const char* how = "";
};

// Resolve each capture key to a live index. Each live display is claimed at most once, and the
// rules run as global passes (strongest first) so a weak rule for one capture can never steal a
// display another capture matches strongly:
//   1. same device path (case-insensitive)           — the exact panel on the exact connector
//   2. same EDID id AND same settings slot           — the panel moved connector (the slot travels
//                                                      with it: MatchMonitorSettings keeps it)
//   3. same EDID id, exactly one candidate, and a slot missing on one side (a slot MISMATCH means
//      a different known display, e.g. an uncaptured EDID twin — never adopted)
//   4. captured WITHOUT an identity: the display now at the captured index — but only while that
//      display is STILL unidentified. Once it is identified nothing proves it is the same panel (an
//      index shift puts another display there), so the capture is reported, never guessed.
inline std::vector<CalibResolution> ResolveCalibCaptures(const std::vector<CalibCaptureKey>& caps,
                                                         const std::vector<CalibLiveMonitor>& live) {
    std::vector<CalibResolution> out(caps.size());
    std::vector<bool> taken(live.size(), false);
    auto claim = [&](size_t c, size_t i, const char* how) {
        out[c].liveIndex = (int)i;
        out[c].how = how;
        taken[i] = true;
    };

    for (size_t c = 0; c < caps.size(); c++) {
        const std::wstring& path = caps[c].identity.devicePath;
        if (path.empty()) continue;
        for (size_t i = 0; i < live.size(); i++) {
            if (!taken[i] && CalibEqualsNoCase(live[i].identity.devicePath, path)) {
                claim(c, i, "device path");
                break;
            }
        }
    }
    for (size_t c = 0; c < caps.size(); c++) {
        if (out[c].liveIndex >= 0 || caps[c].identity.edidId.empty() || caps[c].slot < 0) continue;
        for (size_t i = 0; i < live.size(); i++) {
            if (!taken[i] && live[i].slot == caps[c].slot &&
                CalibEqualsNoCase(live[i].identity.edidId, caps[c].identity.edidId)) {
                claim(c, i, "EDID id + settings slot (connector changed)");
                break;
            }
        }
    }
    for (size_t c = 0; c < caps.size(); c++) {
        if (out[c].liveIndex >= 0 || caps[c].identity.edidId.empty()) continue;
        int only = -1, count = 0;
        for (size_t i = 0; i < live.size(); i++) {
            if (taken[i] || !CalibEqualsNoCase(live[i].identity.edidId, caps[c].identity.edidId)) continue;
            if (caps[c].slot >= 0 && live[i].slot >= 0) continue;   // a different known display
            only = (int)i;
            count++;
        }
        if (count == 1) claim(c, (size_t)only, "EDID id (connector changed)");
        else if (count > 1) out[c].how = "ambiguous: more than one connected display has this EDID id";
    }
    for (size_t c = 0; c < caps.size(); c++) {
        if (out[c].liveIndex >= 0 || !caps[c].identity.empty()) continue;
        const int idx = caps[c].indexAtCapture;
        if (idx < 0 || idx >= (int)live.size() || taken[(size_t)idx]) continue;
        if (live[(size_t)idx].identity.empty())
            claim(c, (size_t)idx, "same index, still unidentified");
        else
            out[c].how = "captured without a display identity; the display now at that index is "
                         "identified, so it cannot be proven to be the same one";
    }
    for (size_t c = 0; c < caps.size(); c++) {
        if (out[c].liveIndex >= 0 || out[c].how[0] != '\0') continue;
        out[c].how = caps[c].identity.empty()
            ? "captured without a display identity and that index is gone or taken"
            : "display not connected";
    }
    return out;
}

// ---------------------------------------------------------------------------------------------
// Restore planning
// ---------------------------------------------------------------------------------------------
enum class CalibMhcRestore {
    None,          // nothing to do for this mode's MHC
    Reinstall,     // the captured MHC was enabled: GenerateAndInstallMhcProfile
    IdentitySwap,  // no captured MHC, but the session left one associated: ReplaceMhcProfileWithIdentity
};

struct CalibModeRestore {
    bool isHdr = false;
    CalibMhcRestore action = CalibMhcRestore::None;
    std::wstring liveProfileName;  // IdentitySwap: the profile the session left associated
};

// One entered mode's MHC: reinstall the captured profile if it was enabled; otherwise, if the
// session left a profile live (DLC's identity or an interim build), swap it for the identity
// profile rather than leaving it associated — after the settings restore nothing references it,
// the stale-association sweep would drop it, and Windows keeps applying the LAST associated MHC2
// transform (HW-proven 2026-09-03 / 2026-09-23; main's ca43c39 behaviour, per mode).
// `liveNow` MUST be the live settings BEFORE the captured settings are copied over them.
inline CalibModeRestore PlanCalibModeRestore(const MonitorSettings& liveNow, const MonitorSettings& captured,
                                             bool isHdr) {
    CalibModeRestore r;
    r.isHdr = isHdr;
    const MHCSettings& snap = isHdr ? captured.hdrMHC : captured.sdrMHC;
    const MHCSettings& cur = isHdr ? liveNow.hdrMHC : liveNow.sdrMHC;
    if (snap.enabled) {
        r.action = CalibMhcRestore::Reinstall;
    } else if (cur.enabled && !cur.profileName.empty()) {
        r.action = CalibMhcRestore::IdentitySwap;
        r.liveProfileName = cur.profileName;
    }
    return r;
}

struct CalibRestoreStep {
    size_t capture = 0;   // index into CalibSnapshotStore::captures
    int liveIndex = -1;   // where it goes back
    const char* how = "";
    std::vector<CalibModeRestore> modes;   // one per ENTERED mode, SDR first
};

struct CalibUnrestored {
    size_t capture = 0;
    const char* reason = "";
};

struct CalibRestorePlan {
    std::vector<CalibRestoreStep> steps;
    std::vector<CalibUnrestored> unrestored;
};

// Copy a capture back over a live display: every setting EXCEPT the identity fields, which belong
// to the live enumeration (a capture taken before an index shift / connector move carries stale
// ones, and copying them would re-key the display's persisted settings).
inline void RestoreCapturedSettings(MonitorSettings& live, const MonitorSettings& captured) {
    DisplayIdentity identity = live.identity;
    const int slot = live.slot;
    const int legacyIndex = live.legacyIndex;
    live = captured;
    live.identity = std::move(identity);
    live.slot = slot;
    live.legacyIndex = legacyIndex;
}

struct CalibSnapshotStore {
    std::vector<CalibCapture> captures;   // in capture order

    bool Empty() const { return captures.empty(); }
    void Clear() { captures.clear(); }

    std::vector<CalibCaptureKey> Keys() const {
        std::vector<CalibCaptureKey> keys;
        keys.reserve(captures.size());
        for (const CalibCapture& c : captures) keys.push_back(c.key);
        return keys;
    }

    std::vector<CalibCaptureInfo> Infos() const {
        std::vector<CalibCaptureInfo> infos;
        infos.reserve(captures.size());
        for (const CalibCapture& c : captures)
            infos.push_back(CalibCaptureInfo{ c.key, c.sdrEntered, c.hdrEntered, c.capturedAtMs });
        return infos;
    }

    // calibration.enter on live[mon] in `isHdr`: capture the display's settings unless this session
    // already holds a capture of it, in which case the ORIGINAL is kept and only the mode is added.
    // Returns true when an earlier capture was kept — what calibration.enter reports back as
    // `snapshot_retained`. Must run BEFORE the enter clears anything.
    bool Enter(const std::vector<MonitorSettings>& live, int mon, bool isHdr, uint64_t nowMs) {
        if (mon < 0 || mon >= (int)live.size()) return false;
        const std::vector<CalibResolution> res = ResolveCalibCaptures(Keys(), CalibLiveMonitorsFrom(live));
        for (size_t c = 0; c < captures.size(); c++) {
            if (res[c].liveIndex == mon) {
                captures[c].MarkEntered(isHdr);
                return true;
            }
        }
        CalibCapture cap;
        cap.key.identity = live[(size_t)mon].identity;
        cap.key.slot = live[(size_t)mon].slot;
        cap.key.indexAtCapture = mon;
        cap.settings = live[(size_t)mon];
        cap.MarkEntered(isHdr);
        cap.capturedAtMs = nowMs;
        captures.push_back(std::move(cap));
        return false;
    }
};

// What calibration.exit(restore_snapshot=true) must do, against the live settings as they are NOW
// (before anything is copied back).
inline CalibRestorePlan PlanCalibRestore(const CalibSnapshotStore& store, const std::vector<MonitorSettings>& live) {
    CalibRestorePlan plan;
    const std::vector<CalibResolution> res = ResolveCalibCaptures(store.Keys(), CalibLiveMonitorsFrom(live));
    for (size_t c = 0; c < store.captures.size(); c++) {
        if (res[c].liveIndex < 0) {
            plan.unrestored.push_back(CalibUnrestored{ c, res[c].how });
            continue;
        }
        const CalibCapture& cap = store.captures[c];
        CalibRestoreStep step;
        step.capture = c;
        step.liveIndex = res[c].liveIndex;
        step.how = res[c].how;
        const MonitorSettings& liveNow = live[(size_t)step.liveIndex];
        for (int mode = 0; mode < 2; mode++) {
            const bool isHdr = (mode == 1);
            if (cap.Entered(isHdr)) step.modes.push_back(PlanCalibModeRestore(liveNow, cap.settings, isHdr));
        }
        plan.steps.push_back(std::move(step));
    }
    return plan;
}
