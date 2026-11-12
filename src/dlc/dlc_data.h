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
// the section's reference rate (the rate the calibration was made at, by DLC's contract; 60 Hz on every section so far,
// but recorded, never assumed) for full-field greys keyed by their reference-rate light. DesktopLUT derives its
// per-rate MHC trim from it (refresh_trim.h).
constexpr int kDipRefreshSchema = 1;            // the newest section schema this build reads
// The number as DLC measured it, to `sig` significant digits, never shortened to "0" (or "-0") when it is not zero:
// 1835.1 -> "1835", 121.72 -> "122", 0.0021 -> "0.0021", 0.05376 -> "0.0538"; trailing zeros dropped (0.0210 ->
// "0.021"); whole digits are never rounded away (sig counts from the first digit only below 10^sig). `sign`: a "+"
// on positive values. Exactly 0 prints "0"; not finite prints "" (the caller shows its dash).
std::string FormatSig(double v, int sig, bool sign = false);
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
    // How the rate was produced (additive, 2026-11-12): "mode" (a fixed display mode; also when absent) or "vrr" (a
    // VRR cadence on the reference mode, listed here only when DLC's mode-vs-VRR cross-check passed). Informational:
    // the trim treats both alike (DLC put it in `rates` as a fixed-mode trim input).
    std::string source = "mode";
    int proofOk = -1;                      // proof.ok: 1 / 0, -1 not given
    std::string proofKind;                 // proof.kind ("displayconfig", "vrr:refresh_hz", ...)
};
// The VRR sweep (top-level `vrr`, additive): its range, how many points, the cross-check, and whether VRR points
// became fixed-mode rates. Only what the card shows; the points themselves are DLC's evidence.
struct DipRefreshVrr {
    bool present = false;
    double rangeLoHz = kNaN, rangeHiHz = kNaN;
    int points = 0;
    int crossCheckOk = -1;                 // 1 / 0, -1 not measured
    std::string crossCheckRate;            // the rate measured both ways
    bool fixedModeTrimDerived = false;
};
struct DipRefreshSection {
    std::string recordKey;                 // the DIP record ("Asus ProArt PA32UCXR:HDR")
    std::string display, mode;             // the record's display name, mode ("HDR"; from the key when absent)
    int schema = 0;
    std::string measured, runId;           // YYYY-MM-DD, the run
    // The reference rate: the rate whose light every ratio is against, and at which no trim applies. Read from the
    // section's `reference` {key, num, den}; a reference block without a positive rational makes the section unusable
    // (error). With no reference block at all (DLC always writes one) the fields keep dip.py's own default, 60 / 1,
    // and `referenceRecorded` is false: refresh_trim.cpp does not apply such a section (the calibration's rate is
    // not on record).
    std::string referenceKey = "60";
    uint32_t referenceNum = 60, referenceDen = 1;
    bool referenceRecorded = false;
    double ReferenceHz() const { return referenceDen ? (double)referenceNum / (double)referenceDen : kNaN; }
    DipRefreshVrr vrr;
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

// ---- peak_by_window (HDR, the "ABL graph", DLC dip.py PeakByWindow / peak_window.py, schema 1) -----------------------
// The light against the size of a centred white window on black: per window size (% of the screen area) the EARLY
// value (the first read after the window appeared), the SUSTAINED value (where the limiter / backlight settled), the
// minimum and a fitted decay time, from an up pass and a mirrored down pass. Where the passes disagree DLC does not
// average them: the combined values are null there (`agree` = 0) and each pass's own values remain. `valid` = every
// size agreed and the run was complete. Read-only, like the rest of the DIP. Also the shape of the live probe chart
// (charts.json probes.peak_by_window, ParsePeakWindowLive in dlc_live.cpp): there the passes come as series, the
// verdict only once the run completes.
constexpr int kDipPeakWindowSchema = 1;
struct DipPeakPass {
    bool present = false;
    double early = kNaN, sustained = kNaN, min = kNaN, tau = kNaN;
    int settled = -1;                      // 1 / 0 (the hold was cut at its max before settling: an upper bound), -1 unknown
};
struct DipPeakSize {
    double areaPct = kNaN;                 // (0, 100]
    double early = kNaN, sustained = kNaN, min = kNaN, tau = kNaN;   // combined; NaN where the passes disagree
    DipPeakPass up, down;
    int agree = -1;                        // 1 the passes agree, 0 they disagree, -1 one pass only (the turn) / not judged (live)
    double relDiff = kNaN, tol = kNaN;     // |up - down| / mean, and the tolerance it was judged against
    int n = 0;                             // reads behind it
};
struct DipPeakByWindow {
    bool present = false;                  // the record carries the section (non-null)
    std::string measured;                  // YYYY-MM-DD ("" when it has none)
    int schema = 0;
    bool newer = false;                    // a schema newer than this build reads: shown as such, not parsed
    std::string error;                     // non-empty: present but unreadable (DLC drops it too), and why
    std::string runId;
    bool valid = false;
    bool complete = true;                  // live: the run has finished (the DIP's section always has)
    std::vector<DipPeakSize> curve;        // ascending area
    double plateauNits = kNaN;             // the small-window level (the curve's maximum sustained value)
    double runWindowAreaPct = kNaN;        // the largest window still on that plateau (evidence for a run window)
    double runWindowSustainedNits = kNaN;
    double fullFieldSustainedNits = kNaN;
    std::vector<double> areasFailed;       // sizes whose passes disagree
    bool systematic = false;               // one sign across the sizes: the panel drifted over the run
    double maxRelDiff = kNaN;
    int steps = 0;
    std::string stack;                     // conditions.stack: "native" | "installed"
    int emissive = -1;                     // conditions.emissive
    double peakWhiteS = kNaN, longestHoldS = kNaN;   // wear
    int stepsDone = -1, stepsPlanned = -1; // live only
    bool Usable() const { return present && error.empty() && !newer && !curve.empty(); }
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
// One record's peak_by_window value as DLC's loader would take it (dip.py PeakByWindow.from_dict): not an object,
// an entry that is not an object, an area outside (0, 100], a negative or non-numeric value, no curve or no measured
// date = `error` (the record keeps its other fields); a newer schema = `newer`, nothing else read.
DipPeakByWindow ParsePeakByWindowJson(const std::string& json);

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
