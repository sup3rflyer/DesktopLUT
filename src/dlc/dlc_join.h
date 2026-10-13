#pragma once
// Which DLC display and which DLC record describe what a live display runs. Pure (tested): the inputs
// are one scan (Data) and what DesktopLUT has configured on the display (LiveDisplay).
//
// Display match, strongest evidence first, never a guess:
//   1. calibration_profile.yaml quirks.hardware_id == the display's EDID hardware id (desktoplut_monitor
//      only breaks a tie between identical twins);
//   2. stack_registry evidence: a recorded mhc.base_lut equals the display's MHC source file, or a
//      recorded cube.cube_path equals its 3D LUT;
//   3. a verify-only report whose identity hardware_id equals the display's;
//   otherwise unmatched.

#include "dlc/dlc_data.h"

#include <string>
#include <vector>

namespace dlc {

// One scan of the DLC folder (immutable once published).
struct Data {
    std::wstring folder;                    // empty: no DLC folder configured or found
    bool profileFound = false;
    Profile profile;
    std::vector<StackRecord> stacks;
    std::vector<CorrectionRecord> corrections;
    std::vector<Report> reports;            // newest first
    std::string today;                      // the scan's date, YYYY-MM-DD
};

// What DesktopLUT runs on one display. Index 0 = SDR, 1 = HDR.
struct LiveDisplay {
    std::string hardwareId;                 // EDID vendor + product ("ABC1234"); empty when unknown
    int monitor = -1;                       // live index (tie-break only)
    int sameModelCount = 1;                 // live displays with this hardware id (identical twins > 1)
    std::wstring mhcSource[2];              // <MODE>_MHCSourceFile of an enabled MHC profile, else empty
    std::wstring lut[2];                    // LUT_SDR / LUT_HDR
};

// "ABC1234" from an EDID id ("ABC1234-16843009") or, failing that, a device path (\\?\DISPLAY#ABC1234#...).
std::string HardwareIdOf(const std::wstring& edidId, const std::wstring& devicePath);

enum class MatchBy { None, HardwareId, StackEvidence, ReportIdentity };
struct DisplayMatch {
    std::string name;                       // the DLC display name; empty = unmatched
    MatchBy by = MatchBy::None;
    bool ambiguous = false;                 // evidence pointed at more than one DLC display
};
DisplayMatch MatchDisplay(const Data& d, const LiveDisplay& live);

// A pipeline layer against DLC's record of what it applied.
enum class LayerState {
    Off,          // DesktopLUT has nothing here
    Matches,      // DesktopLUT runs exactly what DLC recorded
    Differs,      // DesktopLUT runs something else (changed since DLC applied, or another file)
    Unrecorded,   // DesktopLUT runs something, DLC recorded nothing for this layer
};

struct ModeRecord {
    int stack = -1;                         // index into Data::stacks ("<name>:<MODE>"), -1 none
    LayerState mhc = LayerState::Off, cube = LayerState::Off;
    int current = -1;                       // index into Data::reports: newest report that verified what runs now
    int latest = -1;                        // newest report for this display and mode at all
    int verifiedDaysAgo = -1;               // age of `current` (-1: none or undated)
    std::string correctionMade;             // meter correction date (store, else the profile's)
    int correctionDaysAgo = -1;
    bool correctionStale = false;           // older than meter.correction.max_age_days
    bool reminderDue = false;               // verification older than `reminderDays`, or a stale correction
};
ModeRecord RecordFor(const Data& d, const std::string& name, int modeIndex, const LiveDisplay& live, int reminderDays);

// Does this report describe what `live` runs in that mode? (The cube it verified is the loaded one; with
// no cube loaded, it comes from the run that installed the MHC profile in use.)
bool ReportVerifiesLive(const Report& r, const StackRecord* stack, int modeIndex, const LiveDisplay& live);

}  // namespace dlc
