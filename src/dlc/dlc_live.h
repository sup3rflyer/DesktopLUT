#pragma once
// The DLC run in progress (or the last one), for the DLC page. DLC's live publisher folds the run's
// spine into runs\<id>\live\state.json + charts.json (dlc/dashboard/publish.py); runs\active.json points
// at the run. This side only reads and renders: every number, word and verdict comes from DLC.
//
// Pure parsers (tests feed them text) + a watcher thread that polls while the interface is visible and
// publishes an immutable snapshot (CurrentLive) — the same pattern as the folder scanner (dlc_scan).

#include "dlc/dlc_data.h"

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace dlc {

constexpr int kLiveSchema = 1;   // the live_schema this reader understands fully

// ---- runs\active.json ----------------------------------------------------------------------------
struct ActivePointer {
    bool present = false;
    std::wstring run;               // the run folder
    std::string flow, updated;
    // Newer DLC adds these (absent: unknown).
    std::string state;              // running / paused_seam / done / failed / cancelled
    std::string display, hardwareId, mode, driver, started;
    int monitor = -1;
    uint32_t pid = 0;
};
bool ParseActivePointer(const std::string& json, ActivePointer& out);

// ---- live\state.json -----------------------------------------------------------------------------
enum class StepStatus { Upcoming, Current, Done };
struct LiveStep { std::string key, label; StepStatus status = StepStatus::Upcoming; };

struct FeedRow {
    std::string time;               // ISO, local
    std::string level, stage, event, title, text;
};

struct SeamPacket {
    std::string key, stage, status; // status: paused / decided / auto_accepted / invalid_decision / resumed
    std::string question, choice, note, recommendation;
    std::vector<std::string> options, checklist;
    bool Pending() const { return status == "paused"; }
};

struct LiveRead {                   // the last meter read
    bool present = false, ok = false, neutral = false;
    std::string label, role;
    double signal[3] = { kNaN, kNaN, kNaN };
    double x = kNaN, y = kNaN, Y = kNaN;          // measured
    double tx = kNaN, ty = kNaN, tY = kNaN;       // target
    double de = kNaN, cct = kNaN, duv = kNaN;
    uint32_t swatch = 0;            // 0xRRGGBB of the intended colour (DLC's sc); 0 = unknown
};

struct LiveDe {                     // rolling per-patch ΔE of the current stage (live_de)
    std::string metric;             // "itp" / "de2000"
    double avg = kNaN, max = kNaN, last = kNaN;
    int n = 0;
};

struct ScoredDe {                   // the scoring stage's verdict (de)
    bool present = false;
    std::string metric, phase;
    double avg = kNaN, p95 = kNaN, max = kNaN, white = kNaN, gray = kNaN;
};

struct LiveState {
    bool present = false;
    int schema = 0;
    uint64_t seq = 0;
    uint32_t pid = 0;
    std::wstring runDir;
    std::string runId, publishedIso, nowIso;
    // header
    std::string display, mode, flow, target;
    int monitor = -1;
    bool hdr = false;
    double luminance = kNaN, gamma = kNaN;
    // progress
    std::string phase, stage, runStatus;     // run_status: idle / running / stalled / completed / ...
    std::vector<LiveStep> steps;
    int stepIndex = 0, stepTotal = 0;        // 1-based current; 0 = none
    int patchesDone = 0, patchesTotal = 0, reads = 0, readsOk = 0, readsFailed = 0;
    double runElapsedS = kNaN, stageElapsedS = kNaN, etaS = kNaN, etaHiS = kNaN, sPerPatch = kNaN;
    // liveness: live / slow / stalled / paused / done / unknown
    std::string light;
    double ageS = kNaN, progressAgeS = kNaN;
    LiveRead lastRead;
    LiveDe liveDe;
    ScoredDe scored;
    SeamPacket seam;
    std::vector<FeedRow> feed;               // oldest first
    // DLC's warm-up settling verdict: settled (-1 unknown, 0 still moving, 1 settled), the fastest channel, its slope
    int settled = -1;
    std::string driftChannel;
    double driftSlopePct10 = kNaN;           // % per 10 min
};
bool ParseLiveState(const std::string& json, LiveState& out);

// ---- live\charts.json ----------------------------------------------------------------------------
// Where a patch sits for the practical view (DLC's dashboard/practical.py): a weightless content anchor, an HDR grey
// at the panel's top (a sustain limit), a target outside the panel's gamut, a reachable target outside the content
// core (HDR > 203 nit / outside Rec.709), or the core.
enum class Zone : uint8_t { Core, Limits, Clamped, Peak, Anchor };
struct CiePoint {
    float x, y, tx, ty, de;
    bool carried, neutral;
    std::string label;
    // practical (NaN when the file predates them)
    float Y = (float)kNaN, tY = (float)kNaN;        // measured / target nits
    float L = (float)kNaN, C = (float)kNaN, H = (float)kNaN;   // the metric's signed lightness / chroma / hue split
    float tw = (float)kNaN, tm = (float)kNaN;       // tint: + warmer / + more magenta (metric units)
    float dY = (float)kNaN, jnd = (float)kNaN;      // luminance error %, and one JND in %
    float w = (float)kNaN;                          // content weight (content-sampled sets)
    Zone zone = Zone::Core;
    uint32_t liftIntended = 0, liftMeasured = 0;    // lightness-lifted swatches (hue visible on a dark theme)
    float jncd = (float)kNaN;                       // the chromaticity miss in just-noticeable differences (u'v' / 0.004)
};
struct GrayRow {
    float signal, Y, cct, duv, de, r, g, b;
    bool dim;
    float tY = (float)kNaN, dY = (float)kNaN, jnd = (float)kNaN, tw = (float)kNaN, tm = (float)kNaN;
    bool carried = false;
};
struct CurvePoint { float x, y; };
struct ConvergencePoint { float elapsedS, avg, max; bool scored; };
struct Offender { std::string label; float de; uint32_t intended, measured; bool oog; };
struct DriftPoint { float elapsedS, r, g, b; int seg; };

// The practical view of the charted stage.
struct BandRow { std::string label; float lo, hi, share, mean, p95, L, C, H; int n; float Lr = (float)kNaN; };   // L signed mean; Lr, C, H RMS
struct HueRow { int sector; std::string name; int n; float w, de, C, H; };
struct WorstRow {
    std::string desc, label;
    float de, impact, w, dY;
    char comp;                              // the largest component: L / C / H
    Zone zone;
    uint32_t intended, measured;
    float L = (float)kNaN, C = (float)kNaN, H = (float)kNaN;   // signed: + brighter / + more saturated / + toward hueTo
    std::string hueTo;                      // the neighbouring hue the colour turns toward ("" none)
};
// A hue family's error along brightness (colour by level): metric units, + brighter / + more saturated.
struct FamilyPoint { float Y, L, C, H, dY, de; int n; };
struct FamilyRow {
    char key = ' ';                        // R Y G C B M
    std::string name;
    int n = 0;
    float w = (float)kNaN;                 // share of the content
    bool thin = false;                     // too few patches to show a trend
    std::vector<FamilyPoint> points;       // brightness order
};
struct PanelLimits {
    float peak = (float)kNaN, targetPeak = (float)kNaN, neutralPeak = (float)kNaN, black = (float)kNaN;
    bool hasWhite = false;
    float whiteY = (float)kNaN, whiteTargetY = (float)kNaN, whiteDe = (float)kNaN, whiteCct = (float)kNaN,
          whiteDuv = (float)kNaN, whiteTw = (float)kNaN, whiteTm = (float)kNaN, whiteDY = (float)kNaN;
};
struct Practical {
    bool present = false;
    bool contentWeighted = false;          // weights from the patch set (else the survey's film shares)
    std::vector<BandRow> bands;
    float score = (float)kNaN, coverage = (float)kNaN, offsetL = (float)kNaN;
    int n = 0;
    std::vector<HueRow> hues;
    std::vector<FamilyRow> families;       // R Y G C B M
    std::vector<WorstRow> worst, apart;    // the misses that matter; anchors / peak / limits / clamped apart
    float impactTotal = (float)kNaN;       // the sum of content x error over every core patch (worst rows' share of it)
    PanelLimits limits;
    std::string contentClass;              // e.g. seq_hdr_balanced
    float toneDark = (float)kNaN, toneLight = (float)kNaN;   // mean tone error %, signal < 0.5 / >= 0.5
    int nDark = 0, nLight = 0;
    float tintSpread = (float)kNaN;        // the largest tint difference between two greys >= 1 nit (metric units)
    float contentScore = (float)kNaN;      // DLC's scored content-weighted ΔE
};
struct StagePractical { std::string stage; float score; int n; std::vector<float> bands; };
struct TonePoint { float signal, Y, tY, dY; };
struct StageTone { std::string stage; std::vector<TonePoint> points; };   // a stage's fresh greys
struct DriftView {
    float noise = (float)kNaN;             // the meter's noise on the drift reference, % (robust sigma)
    float settledAtS = (float)kNaN;        // when the panel first counted as settled (run seconds)
    int settled = -1;                      // the final verdict: -1 unknown, 0 still moving, 1 settled
    std::string channel;
    float slope = (float)kNaN;             // % per 10 min, the fastest channel
};

struct LiveCharts {
    bool present = false;
    uint64_t seq = 0;
    std::string stage;
    bool hdr = false;
    // CIE
    std::vector<CiePoint> cie;
    float primaries[3][2] = {};              // target gamut r, g, b
    bool hasMeasuredPrimaries = false;
    float measuredPrimaries[3][2] = {};
    float white[2] = { 0.3127f, 0.3290f };
    std::string gamutLabel;
    // grayscale (signal order)
    std::vector<GrayRow> gray;
    double targetDuv = kNaN;
    // tone response
    std::string eotfKind;                    // "pq" / "power"
    double gamma = kNaN, luminance = kNaN;
    std::vector<CurvePoint> eotfReference;   // (signal, Y relative to `luminance`, 0..1) — DLC's form
    std::vector<CurvePoint> eotfPoints;      // (signal, Y in cd/m2)
    std::vector<ConvergencePoint> convergence;
    std::vector<Offender> offenders;
    std::vector<DriftPoint> drift;           // channel drift (% from each segment's first checkpoint)
    Practical practical;
    std::vector<StagePractical> stages;      // each measurement stage's band means (a build's before / after)
    std::vector<StageTone> stagesTone;       // the first and the last stage's greys (one entry: one stage)
    float coverageTarget = (float)kNaN, coverageP3 = (float)kNaN;   // % of the target / P3 gamut covered, u'v'
    DriftView driftView;
};
bool ParseLiveCharts(const std::string& json, LiveCharts& out);

// ---- the snapshot the page reads -----------------------------------------------------------------
enum class RunPhase {
    None,       // no run on record
    Running,    // live / slow
    Stalled,    // DLC's own verdict (no progress past its threshold)
    Waiting,    // paused at a seam: the process exited, a judge decides
    Paused,     // a requested pause (control.json), the process is holding
    Done,       // finished (completed / aborted / reverted)
    Failed,     // the process is gone without finishing
    NoData,     // the process runs but the live files are stale or missing
};

struct Live {
    std::wstring dlcFolder;
    ActivePointer active;
    std::shared_ptr<const LiveState> state;    // null: none / unreadable
    std::shared_ptr<const LiveCharts> charts;  // null: none / unreadable
    RunPhase phase = RunPhase::None;
    bool processAlive = false;                 // the run's pid is running (false: unknown or gone)
    double stateAgeS = kNaN;                   // since state.json was last written
    bool pauseRequested = false;               // control.json says pause (until DLC consumes it)
    bool cancelRequested = false;
    std::string pauseRequestedIso;
};

// The phase from the parsed state + what the watcher saw (pure; tested).
RunPhase DeriveRunPhase(const ActivePointer& active, const LiveState* state, bool processAlive,
                        double stateAgeS, bool pauseRequested);

// ---- the watcher -------------------------------------------------------------------------------
void SetLiveListener(std::function<void()> onChange);   // called on the watcher after each change
// Start or move the watch to a DLC folder ("" stops watching). Any thread.
void WatchLive(const std::wstring& dlcFolder);
// Poll only while the interface is visible (hidden: the thread sleeps). Any thread.
void SetLiveWatchActive(bool active);
void StopLiveWatch();                                     // GUI thread, at shutdown
std::shared_ptr<const Live> CurrentLive();                // any thread; never null

// A finished run's final charts (runs\<id>\live\charts.json, written when the run ended or paused): the cached
// charts; a LiveCharts with present == false when the run has none (an older DLC); null while it loads (the
// listener fires when it is in). Any thread.
std::shared_ptr<const LiveCharts> RunCharts(const std::wstring& runDir);
void SeedRunChartsForTest(const std::wstring& runDir, std::shared_ptr<const LiveCharts> charts);

// Charts from a report's patch metrics, for runs without a final snapshot: the CIE scatter (measured and target
// xy, ΔE), the grey steps' luminance against their targets, the worst patches. No balance or convergence.
std::shared_ptr<const LiveCharts> ChartsFromPatches(const std::vector<Patch>& patches, bool hdr);

// ---- control (cooperative, DLC polls it) --------------------------------------------------------
enum class RunControl { Pause, Resume, Cancel };
// Writes runs\<id>\control.json atomically. Pause holds the run for up to 180 s, then DLC rolls it back.
bool WriteRunControl(const std::wstring& runDir, RunControl action, std::string* error = nullptr);

}  // namespace dlc
