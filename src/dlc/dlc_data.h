#pragma once
// What DLC (DesktopLUT Calibrator) leaves on disk, read tolerantly. Pure parsers over file contents (the
// scanner, dlc_scan, does the file I/O): a missing key reads as absent, never as an error, and nothing
// here assumes a particular display, folder or monitor index.
//
// Files (all optional): calibration_profile.yaml, stack_registry.json, correction_store.json next to it;
// <paths.output>\<folder>\report.json per run; runs\<id>\reports\verification_iterNN_patch_metrics.json.
// Text from DLC stays UTF-8 (std::string); filesystem paths are std::wstring.

#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace dlc {

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

std::wstring Utf8ToWide(const std::string& s);
std::string WideToUtf8(const std::wstring& w);

// Paths compare normalised: '/' -> '\', lower-case, repeated separators and "." segments collapsed, ".."
// resolved, no trailing separator (DLC writes str(Path), which drops "." itself).
std::wstring NormPath(const std::wstring& p);
bool SamePath(const std::wstring& a, const std::wstring& b);   // false when either is empty
// "...\runs\<id>\..." -> "<id>": the last runs\<segment> whose segment is a run folder name, else the last
// runs\<segment> (a DLC folder may itself sit under another "runs" folder). Empty when there is none.
std::string RunIdFromPath(const std::wstring& p);
// "SDR" / "HDR" for any casing; "" otherwise.
std::string NormMode(const std::string& m);
// DLC's infer_mode_from_filename: the file NAME's delimited HDR / SDR tag (start/end or one of _ - . space
// around it) when it carries exactly one kind; "" when untagged or contradictory.
std::string InferModeFromFilename(const std::wstring& path);
// Days from an ISO date ("YYYY-MM-DD", anything after it ignored) to `today` (same form); -1 if unreadable.
int DaysBetween(const std::string& isoDate, const std::string& today);

// ---- calibration_profile.yaml ------------------------------------------------------------------------
struct ProfileDisplay {
    std::string name;
    std::string hardwareId;   // quirks.hardware_id: EDID vendor + product ("ABC1234"); often absent
    std::string shortName;    // quirks.short_name
    int monitor = -1;         // desktoplut_monitor: the DesktopLUT index when the profile was written
};
struct Profile {
    bool readable = false;
    std::vector<ProfileDisplay> displays;
    std::wstring outputDir;            // paths.output resolved against the profile's folder ("results")
    std::string correctionFile;        // meter.correction.file (empty: no correction)
    std::string correctionMade;        // meter.correction.made
    int correctionMaxAgeDays = 180;    // meter.correction.max_age_days
};
Profile ParseProfile(const std::string& yamlText, const std::wstring& profileDir);

// ---- stack_registry.json: what DLC last applied, per "<display>:<MODE>" -----------------------------
struct StackRecord {
    std::string display, mode;
    int monitor = -1;
    std::string runId, appliedAt;
    std::wstring baseLut;          // mhc.base_lut: equals DesktopLUT's <MODE>_MHCSourceFile when it is installed
    std::wstring cubePath;         // cube.cube_path: equals DesktopLUT's LUT_<MODE> when it is loaded
    std::string cubeRunId, cubeAppliedAt;
};
std::vector<StackRecord> ParseStackRegistry(const std::string& json);

// ---- correction_store.json: the meter correction per display and mode -------------------------------
struct CorrectionRecord {
    std::string display, mode;
    std::string made;              // correction_made, the staleness clock
    std::wstring file;             // relative to the DLC folder
};
std::vector<CorrectionRecord> ParseCorrectionStore(const std::string& json);

// ---- dip_store.json: the refresh response per display and mode (DLC dip.py RefreshResponse, schema 1) -----------
// A measured response, not a correction: per measured refresh rate, the panel's light at that rate over its light at
// the reference rate (60 Hz) for full-field greys keyed by their 60-Hz light. DesktopLUT derives its per-rate MHC
// trim from it (refresh_trim.h).
constexpr int kDipRefreshSchema = 1;            // the newest section schema this build reads
struct DipRefreshLevel {
    double nits = kNaN;     // the reference-rate light (cd/m2), > 0
    double ratio = kNaN;    // light at the rate / at the reference rate, > 0
    double uLn = kNaN;      // 1-sigma of ln(ratio); NaN = not given
    int n = 0;              // repeats combined
};
struct DipRefreshRate {
    std::string key;                       // the catalogue name ("47.952")
    uint32_t num = 0, den = 0;             // the exact rational (48000 / 1001)
    double hz = kNaN;                      // num / den
    std::vector<DipRefreshLevel> levels;   // ascending nits
    std::string sameAs;                    // DLC's verdict: this rate's response is that rate's ("" none; the
                                           // reference's key = no effect)
};
struct DipRefreshSection {
    std::string recordKey;                 // the DIP record ("Asus ProArt PA32UCXR:HDR")
    std::string display, mode;             // the record's display name, mode ("HDR"; from the key when absent)
    int schema = 0;
    std::string measured, runId;           // YYYY-MM-DD, the run
    std::string referenceKey = "60";
    uint32_t referenceNum = 60, referenceDen = 1;
    std::string hardwareId;                // identity.hardware_id, upper case: the EDID vendor + product it was measured on
    std::wstring devicePath;               // identity.device_path (the connector; twin panels)
    std::string edidId;                    // identity.edid_id (informational)
    double peakNits = kNaN;
    bool interpolable = false;             // unmeasured rates may be interpolated (DesktopLUT never does: measured only)
    int maxAgeDays = -1;                   // the record's max_age_days (-1: not set)
    std::vector<DipRefreshRate> rates;
    std::string error;                     // non-empty: present but unusable, and why
};
// ---- dip_store.json: every record, summarised (the DLC tab's PANEL card) --------------------------------------------
// What DLC has measured about one display in one mode (dip.py DisplayInstrumentProfile). Read-only: DesktopLUT never
// writes the DIP. A number DLC has not measured (absent or null) stays NaN, a word or a date stays empty.
constexpr int kDipDefaultMaxAgeDays = 180;      // dip.py is_stale's default_max_age_days (a record may set its own)

// peak_by_window (HDR, the "ABL graph": the light the panel holds per window size). Its JSON is not final, so only
// its presence and its measured date are read. Extension point: the steps ({area_pct, early_nits, sustained_nits,
// min_nits, tau_s, n}), identity, run id and conditions belong in this struct, read by ParsePeakByWindow in
// dlc_data.cpp; their chart belongs to the PANEL card's PEAK BY WINDOW row (PanelCard in ui_dlc.cpp).
struct DipPeakByWindow {
    bool present = false;                  // the record carries the section (non-null)
    std::string measured;                  // YYYY-MM-DD ("" when it has none)
};

struct DipRecord {
    std::string key;                       // the store key: "<display>:<MODE>", or a bare "<display>" (mode-less, old)
    std::string display;                   // the record's display name (the key's when absent)
    std::string mode;                      // "SDR" / "HDR"; "" for a mode-less record
    std::string made, updated;             // YYYY-MM-DD; `made` is DLC's staleness clock
    int maxAgeDays = -1;                   // the record's max_age_days (-1: DLC's default, kDipDefaultMaxAgeDays)
    std::string instrument;                // the meter ("X-Rite i1 DisplayPro")
    std::string correctionFile;            // the meter correction in force while characterizing ("" none)
    // Noise (the instrument axis).
    int noiseBands = 0;                    // noise_model entries
    double noiseFloorNits = kNaN, readOverheadS = kNaN;
    // Timing.
    double settleS = kNaN, settleBrightS = kNaN, settleDarkS = kNaN;
    // The native panel.
    double whiteNits = kNaN, whiteX = kNaN, whiteY = kNaN, blackNits = kNaN;
    bool hasPrimaries = false;             // native_primaries R, G, B
    double sustainedPeakNits = kNaN;       // declared by DLC, no writer yet (null everywhere today)
    // Tone (HDR mostly).
    double eotfUndershoot = kNaN;          // full-field measured vs requested light, a fraction (+0.0145 = 1.45 % over)
    int whiteVsLuminance = 0;              // white_vs_luminance rows
    // Thermal.
    std::string thermalRegime;             // "convergent" | "fluctuating" | "warming"
    double warmupMinutes = kNaN;
    std::string coldChannel;               // "R" / "G" / "B"
    double creepDePerMin = kNaN;
    // Optional sections.
    int refresh = -1;                      // index into DipStoreData::refresh (its refresh_response), -1 none
    DipPeakByWindow peakByWindow;
};
// The record's native contrast (white / black); NaN when either is unmeasured or black reads 0 (below the meter).
double DipContrast(const DipRecord& r);
// Days since `made` (-1 when it is missing or unreadable) and whether DLC calls the record stale (dip.py is_stale:
// older than its max_age_days, else kDipDefaultMaxAgeDays; no readable `made` = stale).
int DipAgeDays(const DipRecord& r, const std::string& today);
bool DipStale(const DipRecord& r, const std::string& today);

struct DipStoreData {
    bool readable = false;                 // the file parsed as a DIP store
    std::vector<DipRefreshSection> refresh;   // one per record that carries a refresh_response
    std::vector<DipRecord> records;        // one per readable record, in the file's order
    std::vector<std::string> dropped;      // keys of records DLC itself would drop (dip.py DipStore.load): not shown
};
DipStoreData ParseDipStore(const std::string& json);

// ---- report.json ------------------------------------------------------------------------------------
struct Verification {
    bool present = false;
    std::string metric;            // "CIEDE2000" / "dE_ITP" (absent in the oldest reports = CIEDE2000)
    double avg = kNaN, p95 = kNaN, max = kNaN, white = kNaN, grayAvg = kNaN;
    int patchCount = 0;
    bool withinQuality = false;
};
struct WorstPatch { float rgb[3] = {}; double de = kNaN; };
struct Report {
    std::wstring path;             // the report.json
    std::wstring folder;           // its folder (the results folder)
    std::string display, mode, contentMode, flow, date, target;
    int monitor = -1;
    Verification verification;
    std::vector<WorstPatch> worst;
    std::wstring cubePath;         // lut3d.cube_path  (runs\<id>\generated\final_<mode>.cube)
    std::wstring deliveredCube;    // deliverables.cube (the copy in the results folder)
    std::wstring runtimeCube;      // verify_only.measured_stack.runtime_cube (the cube that was verified)
    std::wstring verifiedMhcSource;// verify_only.measured_stack.installed_stack.pipe_source_file (its MHC)
    std::string hardwareId;        // verify_only.verify_source.identity.hardware_id
    std::wstring runDir;           // resolved by the scanner (run_done event, cube path, folder stamp)
    uint64_t mtime = 0;            // file time, orders same-day reports
};
bool ParseReport(const std::string& json, Report& out);

// The metric as a short label: "\xCE\x94E2000" / "\xCE\x94E ITP" (UTF-8).
const char* MetricLabel(const std::string& metric);

// ---- verification_iterNN_patch_metrics.json ---------------------------------------------------------
struct Patch {
    float rgb[3] = {};             // normalised signal (HDR: PQ / Rec.2020)
    double measured[3] = { kNaN, kNaN, kNaN };   // XYZ, cd/m2 (absent components NaN)
    double target[3] = { kNaN, kNaN, kNaN };
    double de = kNaN;              // in the report's metric (the field is named de2000 either way)
    bool grayscale = false, clamped = false;
};
std::vector<Patch> ParsePatchMetrics(const std::string& json);

// The run_done events of an events.jsonl tail: results_dir values, last one last.
std::vector<std::wstring> RunDoneResultsDirs(const std::string& eventsTail);

// Run folder names DLC creates: YYYYMMDD_HHMMSS[_ffffff]_<sdr|hdr>_<slug>.
bool IsRunFolderName(const std::wstring& name);

}  // namespace dlc
