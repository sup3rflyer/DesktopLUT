// DesktopLUT - shared/fald_temporal.h
// FALD temporal drive state ("LED lag"): the D3D-free bookkeeping shared by the overlay path (src/fald.cpp) and the
// DWM hook (dwm_hook/hook_fald.cpp). ONE implementation of the rules — the time law, the resets, the delay ring's
// indices, which drive map each kernel reads, the settle hold — so the two paths cannot drift. The GPU work (the
// passes, the texture copies) stays with each caller; these functions only say what to bind, copy and dispatch.
// DLC references: dlc/fald/temporal.py (modes 1 / 2), dlc/fald/paneltime.py (mode 3), dlc/fald/gpuemu.py (GPU order).
// Standalone: no Windows, no D3D, no project headers.
#pragma once

#include <stdint.h>

// Temporal drive state (FaldSettings::temporalMode):
// 0 = off (stateless layer, byte for byte), 1 = both fields from the filtered drive (LEDs AND the panel's estimate
// lag), 2 = B_true from the filtered drive, B_est from the instantaneous one (LEDs lag, the LCD compensation follows
// the command). Default OFF. Modes 1 / 2 = a first-order filter, written before the PA32UCXR's LED law was measured
// and REJECTED as that law by the 2026-09-19/20 camera measurement (work guide H5 / IDEA BOARD findings); kept as the
// owner's A/B controls. 3 = the MEASURED law ("panel
// clock", FALD_TEMPORAL_PANEL below) — its own pass and state; modes 0-2 are untouched by it.
constexpr unsigned int FALD_TEMPORAL_OFF = 0;
constexpr unsigned int FALD_TEMPORAL_BOTH = 1;
constexpr unsigned int FALD_TEMPORAL_TRUE_ONLY = 2;
constexpr float FALD_TAU_MAX_MS = 5000.0f;
constexpr unsigned int FALD_DELAY_MAX = 3;    // pipeline delay ring depth (FaldSettings::delayFrames 0..3)
constexpr float FALD_RESUME_GAP_MS = 250.0f;  // a run this long after the previous one re-arms the settle hold
// Per-frame blend factor 1 - exp(-dt/tau) of a first-order response; tau <= 0 (or dt <= 0) = instant (1).
float FaldTemporalAlpha(float tauMs, float dtMs);
// Frames the layer keeps re-rendering after the last content change so the state settles (5 tau_max, ceil, plus the
// pipeline delay; 0 when neither edge has a time constant and there is no delay): Desktop Duplication delivers no
// frames on a static desktop (and DWM presents none either — the hook asks the host to keep it composing).
unsigned int FaldSettleFrames(float tauRiseMs, float tauFallMs, float dtMs, unsigned int delayFrames = 0);

// Temporal mode 3 = "panel clock" (EXPERIMENT, default off; work guide C13; DLC dlc/fald/paneltime.py is the reference,
// dlc/fald/gpuemu.py GpuPanelDriveState the GPU-order twin). The PA32UCXR's law as the phone camera measured it
// (2026-09-19/20): the dimming engine is a sample-and-hold at HALF the refresh rate (30 Hz at 60 Hz, 24 Hz at 48 Hz:
// measured at both — not a fixed 30 Hz). It ticks every 2nd panel refresh
// (which one — the PARITY — software does not know TODAY: it is locked to the actually presented frame index and
// re-rolls on display mode sets, so it needs the presented refresh index + a one-bit calibration: stage 3 below); at a tick every zone's LED drive closes `closure` of the remaining
// gap to the drive demanded by the frame that was on the panel ONE refresh earlier (same up and down); the panel's own
// LCD compensation uses the LED state of ONE refresh earlier. The first-order filter of modes 1 / 2 cannot express
// this. Per zone and parity clock p the layer keeps the LED state S_p; with k = the panel refreshes elapsed since the
// previous processed frame was first shown (Desktop Duplication delivers frames on change only), every tick in between
// targeted that frame's round-1 drives d_prev, so t ticks are ONE blend a = 1 - (1 - closure)^t:
//   B_true of this frame from S_p + aTrue_p (d_prev - S_p)   (ticks up to this refresh; committed as the new S_p)
//   B_est  of this frame from S_p + aEst_p  (d_prev - S_p)   (ticks up to the refresh before it)
// parity -1 (unknown, the default) = the mean of the two clocks (the fields are linear in the drives), 0 / 1 = that
// clock only (meaningful with stage 3's absolute index — below FaldPanelClockSettleFrames; on the run-time grid alone
// the index re-rolls with DWM's composition timing, so there they stay EXPERIMENTAL). The state of a
// frame depends on past frames only, so both inverse rounds read the same two maps. First frame after a reset: both
// clocks = this frame's drives (the panel is taken as settled), the fields are the stateless layer's.
// TIME: a refresh GRID LOCKED TO THE RUNS (first-order phase lock). The grid has the NOMINAL period (the mode
// description's; the real one is 20-1000 ppm off) and a centre time g of the last run's refresh: a run at time t gives
// x = (t - g) / period, k = round(x) (never negative), residual r = x - k, and the grid follows the runs,
// g += (k + a r) period, with the gain a = 1 / (runs since the seed + 1), not below FALD_CLOCK_LOCK_GAIN — a running mean
// while it acquires, then a slow tracker. The dominant run phase therefore sits at the grid CENTRE, half a period from
// the rounding boundary: a period error only leaves a static lag of ppm / a (1000 ppm -> 1 % of a period), a seeding run
// that came at another point of its cycle than the content runs is pulled in within a few runs, one late run (a render
// stall) moves the grid by a r only. (An unlocked grid — the absolute index of round 2 — drifts onto the boundary and
// then dithers k = 0 / 2 for tens of seconds. The overlay's vblank clock is the COMPOSITOR's, i.e. the primary
// display's, not this monitor's: no per-monitor source to use instead; the DWM hook runs per monitor present.) k = 0 is
// real on the overlay: its render loop ticks on the
// system-wide compositor clock, and a faster display elsewhere on the desktop can make it run more than once per
// refresh of this monitor; two such run populations half a period apart lock to -1/4 and +1/4 of the grid: a stable
// 1 / 0 alternation. A k = 0 run is still inside the previous run's refresh: the clocks do not advance, both rounds read
// the previous run's maps again, and this frame's round-1 drives REPLACE the previous frame's as the next target (the later frame is the one the panel shows in that refresh); inside
// the seeding refresh (n = 0) it simply re-seeds. More than FALD_CLOCK_MAX_REFRESHES elapsed (a static desktop; > 1.07 s
// at 60 Hz) is NOT a reset: the panel has settled on the previous frame, the blends are exactly 1 (S = est = d_prev) —
// the first change after a pause gets the time law like any other. Reset (= re-seed on the next run) only on: a
// layer-off / idle period (FaldTemporalIdle), a mode / closure / parity / refresh-period change, a resource rebuild
// (device, resize, panel file).
constexpr unsigned int FALD_TEMPORAL_PANEL = 3;
constexpr float FALD_CLOCK_CLOSURE_DEFAULT = 0.72f;   // measured 0.63-0.78 per tick
constexpr float FALD_CLOCK_CLOSURE_MIN = 0.05f;
constexpr float FALD_CLOCK_CLOSURE_MAX = 1.0f;
constexpr unsigned int FALD_CLOCK_MAX_REFRESHES = 64;        // more elapsed refreshes than this: blends exactly 1
constexpr unsigned int FALD_CLOCK_SETTLE_MAX = 120;          // cap of the settle hold, in refreshes
constexpr double FALD_CLOCK_LOCK_GAIN = 0.08;                // steady gain of the refresh grid's phase lock (per run)
// Closure into FALD_CLOCK_CLOSURE_MIN..MAX (NaN -> the default); parity into -1 / 0 / 1 (anything else -> -1).
float FaldPanelClockClosure(float closure);
int FaldPanelClockParity(int parity);
// Parity from an INI value: exactly "-1", "0" or "1" (surrounding blanks allowed); empty / garbage / anything else -> -1.
int FaldPanelClockParityFromText(const wchar_t* text);
// Ticks of the clock with this parity (it ticks at refresh n when (n + parity) is even) between a frame first shown at
// refresh nA and the next one at nA + k (k >= 1): tTrue = ticks in nA+1 .. nA+k, tEst = ticks in nA+1 .. nA+k-1.
void FaldPanelClockTicks(unsigned long long nA, unsigned int k, unsigned int parity, unsigned int& tTrue, unsigned int& tEst);
// The six CB words of the clock pass: factor = { aTrue_0, aEst_0, aTrue_1, aEst_1 }, weight = the clocks' shares
// (parity -1: 1/2 each; 0 / 1: that clock alone). float32, the power by repeated multiplication — bit for bit the DLC
// twin's gpuemu.clock_factors32. k > FALD_CLOCK_MAX_REFRESHES: all four exactly 1 (settled on the previous frame).
void FaldPanelClockFactors(unsigned long long nA, unsigned long long k, float closure, int parity, float factor[4], float weight[2]);
// Settle hold of mode 3, in ELAPSED REFRESHES (not runs): m = ceil(ln 0.0005 / ln(1 - closure)) ticks (at least 1), the
// m-th tick is at most 2 m refreshes away and the compensation follows one later: 2 m + 2 (closure 0.72 -> 14), capped at
// FALD_CLOCK_SETTLE_MAX. The residual is 0.05 % of a drive step, not 0.5 %: the round-1 drives feed back through the
// correction, and with 0.5 % the output still moved ~1.4 % per frame at the end of the hold after a 5 -> 1842-nit step.
unsigned int FaldPanelClockSettleFrames(float closure);

// ---------------------------------------------------------------------------------------------------------------------
// Stage 3: an ABSOLUTE refresh index for the panel clock (DLC docs/fald-stage3-absolute-refresh-spec.md; DLC twins
// dlc/fald/paneltime.py abs_refresh_index / clock_source_step; tests/test_fald.cpp).
// ---------------------------------------------------------------------------------------------------------------------
// MEASURED 2026-10-04 (PA32UCXR, meter): the dimming engine ticks every 2nd refresh with its parity locked to the VBLANK
// COUNT; the run-time grid above re-rolls against it every few seconds (DWM's composition timing shifts between latency
// regimes, which moves Σk by one), so parity 0 / 1 could not hold. DesktopLUT.exe now publishes a "vblank map" of the
// monitor (src/dwm_inject.cpp; the shared words: dwm_hook_config.h DwmHookVBlankMap). Phase, period AND COUNT all come
// from a vblank-wait thread on the TARGET OUTPUT itself (FaldVBlankTracker below): the count is that thread's own wake
// numbering. DWM's composition counter (DWM_TIMING_INFO::cRefresh / qpcVBlank) is NOT an input — on the owner's rig
// (2026-10-04) DWM's clock follows the OTHER monitor (BenQ 59.9406 Hz vs the ProArt's 60.0001 Hz: ~1000 ppm, one count
// per ~17 s), and two displays a few ppm apart could not be told apart by any rate check; it is logged as a diagnostic
// only (the drift of DWM's clock against the target output). The hook numbers a composition run at QPC time t with the
// count of the vblank its frame is scanned out on:
//     n = countAnchor + floor((t - qpcAnchor) / period) + L
// L = FALD_VBLANK_LATENCY_REFRESHES: a frame composed in refresh interval j shows from vblank j + 1 (DWM flips at the
// vblank after its composition) — an ASSUMPTION the hardware bench confirms. qpcAnchor sits at the wakes' latency floor
// AFTER the true vblank (a low quantile of the wake times: tens of us), so a run inside that first sliver of a refresh is
// numbered as the previous one (n - 1); DWM's own composition wakes on the same vblank with a latency of the same kind,
// so its runs land after it — the bench's check of L covers this window too.
// The count's ORIGIN is arbitrary: 0 when the publisher starts, monotonic over the publisher's life (re-binding the output,
// a restart of the model or a re-based phase continue it from the elapsed time — each such event starts a new numbering
// EPOCH, logged by the host). The panel's parity bit relative to n is therefore NOT known from n alone: it must be
// calibrated per publisher start / display event / epoch (the meter probe, agent_motion_parity_layer_probe.py); parity
// -1 (the mean of both clocks) stays the default, and 0 / 1 mean "the clock ticks on even / odd n" — stable within an
// epoch across composition regimes. A map that is missing, older than FALD_VBLANK_MAP_MAX_AGE_MS or of another refresh
// period (a mode change) is not used: the run-time grid drives the clock exactly as before (it keeps locking either way).
// PARITY FAIL-SAFE (2026-10-05): the panel's parity bit relative to n is only known for the epoch it was calibrated in,
// and a WRONG parity is worse than none (the 10-05 meter control: mismatched over-darkened 4.51 -> 3.42 nit where
// unknown averages the clocks). So a parity setting carries the epoch it belongs to, and the clock applies it only while
// the run's map is usable AND carries that epoch; otherwise the weights are the unknown parity's (1/2, 1/2) —
// FaldPanelClockEffectiveParity. Switching between the two changes only the clock pass's weights (both clocks' states
// are always kept), never re-seeds. FALD_PARITY_EPOCH_ANY applies the parity whatever the numbering (the overlay, which
// has no map; tests; a deliberate manual A/B); FALD_PARITY_EPOCH_NONE never applies it with a stage-3 map.
constexpr unsigned long long FALD_PARITY_EPOCH_NONE = 0ull;
constexpr unsigned long long FALD_PARITY_EPOCH_ANY = ~0ull;
constexpr int FALD_VBLANK_LATENCY_REFRESHES = 1;
constexpr double FALD_VBLANK_MAP_MAX_AGE_MS = 500.0;
constexpr double FALD_VBLANK_PERIOD_TOLERANCE = 0.01;   // map period vs the hook's nominal refresh period (relative)
struct FaldVBlankMap {
    long long countAnchor = 0;   // the publisher's vblank index of the vblank at qpcAnchor (origin arbitrary, see above)
    long long qpcAnchor = 0;     // QPC time of that vblank
    double periodQpc = 0.0;      // refresh period in QPC ticks (<= 0 or not finite: no map)
    unsigned long long epochId = 0;   // the numbering epoch (random per epoch; 0 = none)
};
// The parity the clock applies this run (rules above FALD_PARITY_EPOCH_NONE): `parity` (-1 / 0 / 1) when parityEpoch is
// FALD_PARITY_EPOCH_ANY, or when the run's map is usable (absOk) and carries parityEpoch (never NONE); else -1.
int FaldPanelClockEffectiveParity(int parity, unsigned long long parityEpoch, bool absOk, unsigned long long mapEpoch);
// n for a run at tQpc (formula above, `latency` = L). False (n untouched) = no usable map: the period not finite, outside
// 1..1000 ms or off nominalRefreshMs by more than FALD_VBLANK_PERIOD_TOLERANCE (nominalRefreshMs <= 0 skips that check);
// the run more than FALD_VBLANK_MAP_MAX_AGE_MS after the anchor or more than two periods before it; no QPC frequency;
// n < 0. ageMs (optional) = (t - qpcAnchor) in ms, written whenever the period and frequency are usable.
bool FaldAbsRefreshIndex(const FaldVBlankMap& map, long long tQpc, long long qpcFreq, float nominalRefreshMs, int latency,
                         unsigned long long* n, double* ageMs = nullptr);
// Which index drives the clock this run (pure). kGrid = the grid's k of this run, absN = the map's n (absOk = usable),
// prevIndex / prevAbs = the last run's index and whether it was absolute.
//   abs after abs:  d = absN - prevIndex. 0 <= d and |d - kGrid| <= 2 -> k = d, n = absN (ABS: the absolute index wins —
//                   the grid re-rolls at a regime shift, the vblank count does not); d = -1 -> k = 0, n = prevIndex (HOLD:
//                   a run just behind the previous one's boundary after a re-published phase; never backwards); else
//                   REBASE: k = kGrid (continuity), n = absN (the numbering: e.g. a new epoch).
//   abs after grid: SWITCH_ABS: k = kGrid, n = absN (the elapsed refreshes are the grid's, the numbering the map's).
//   no abs:         k = kGrid, n = prevIndex + kGrid (GRID — bit for bit the pre-stage-3 clock — or SWITCH_GRID after
//                   an abs stretch: the numbering continues from the last absolute index).
// k never exceeds n (the tick count starts at n - k >= 0).
enum FaldClockSource : unsigned int {
    FALD_CLOCK_SRC_GRID = 0,
    FALD_CLOCK_SRC_ABS = 1,
    FALD_CLOCK_SRC_SWITCH_ABS = 2,
    FALD_CLOCK_SRC_HOLD = 3,
    FALD_CLOCK_SRC_REBASE = 4,
    FALD_CLOCK_SRC_SWITCH_GRID = 5,
};
FaldClockSource FaldClockSourceStep(unsigned long long prevIndex, bool prevAbs, unsigned long long kGrid, bool absOk,
                                    unsigned long long absN, unsigned long long& k, unsigned long long& n);
const char* FaldClockSourceName(unsigned int source);   // "grid" / "abs" / "switch_abs" / "hold" / "rebase" / "switch_grid"

// The host side of the map (DesktopLUT.exe's publisher thread, src/dwm_inject.cpp) — pure bookkeeping on QPC times, no
// Windows calls, so every rule is unit-tested:
//  * Wake(qpc) at every return of the TARGET output's vblank wait: phase, period and the count. A wake is numbered by the
//    phase model, steps = floor((t - vblank instant of the previous index) / period + e). The wake latency is one-sided,
//    so the split between "late" and "early" belongs just below the FASTEST wakes: e = the model vblank's height above
//    the latency floor — (k + 1) order-statistic spacings for the k-th smallest, the spacing measured ABOVE it (so an early
//    stray cannot inflate it) — plus FALD_VBLANK_EARLY_MARGIN, clamped to [FALD_VBLANK_EARLY_TOLERANCE,
//    FALD_VBLANK_EARLY_TOLERANCE_MAX]. (Too small an e drops the fastest genuine wakes as "spurious"; the ring then loses
//    its fastest samples and the model vblank drifts LATE — a positive feedback.) Tight latency
//    (the rig: max 0.011 period, 2026-10-04) gives 0.05 — a wake keeps its refresh up to 0.95 of a period late (review
//    2026-10-04: a fixed + 0.25 numbered wakes 0.75 - 1.0 late as the NEXT vblank, projected up to 1/4 period early, and
//    three of them in the ring became the anchor); latency spread over 0.6 period gives ~0.07 (a fixed 0.05 dropped the
//    fastest genuine wakes as spurious there). KNOWN LIMIT (review 2026-10-05): with a BIMODAL latency whose slow cluster
//    is the majority (deep C-states: most wakes ~1-2 ms late, a few at the floor) the model sits in the slow cluster and
//    the fast genuine wakes are dropped as spurious: the anchor runs 0.09-0.23 period LATE (runs in that first sliver of
//    a refresh are numbered one low) but the count never slips. Letting the fast wakes in was tried and REFUTED in the
//    review harness: a ring holding both clusters flips its low quantile between them, the chain stays withheld, and a
//    wrong slip repair lands (184 silent slips at 240 Hz, 21 under wide bursts; the pre-v3 0.25 tolerance mixed them too:
//    170). The robust fix is a wake-latency-free phase (D3DKMTGetScanLine at the wake) — a bench item. The rig measured
//    unimodal (p99 95 us). A missed wake (a late thread) is
//    repaired, a second wake inside one refresh is ignored. A GAP is counted through only as far as the period is known:
//    s x u <= FALD_VBLANK_GAP_BUDGET, u = the chain line's period uncertainty once the period is fitted on a chain
//    (lineBound below), else FALD_VBLANK_NOMINAL_TOLERANCE — FaldVBlankTrackerMaxGap; a longer gap restarts the model,
//    the numbering continued by the elapsed time in a new epoch (review 2026-10-04: a +258 ppm fit bias from a 1.5-ms
//    step of the wake-latency floor, accepted by the 1000-ppm check, counted a 60-s gap one short and the chain could not
//    see it: its line extrapolated with the same slope). The vblank instant of
//    an index = a low quantile of the recent wakes projected onto it (the latency is one-sided). Period: the median of the
//    recent single-step gaps (robust; the gap repair uses it), refined once phase points span FALD_VBLANK_FIT_MIN
//    refreshes by a least-squares fit over them (the median is only good to a few us per refresh; the fit to well below
//    0.1 us). Only a fitted period within FALD_VBLANK_NOMINAL_TOLERANCE of the nominal one is published.
//  * Map(out): the CONTINUITY CHAIN. Every published map must sit on the published numbering: the current vblank
//    instant must land on the chain's reference — the least-squares line through its phase points (each one accepted
//    onto the chain), or the last published anchor before there is a fit — within FALD_VBLANK_PHASE_TOLERANCE of a
//    whole number m of periods. m != 0 = a slip of the local numbering (wakes miscounted since): repaired — the rings
//    are renumbered from their times — and the map is published on the chain. A non-integer residual = the phase itself
//    moved (a re-timed display, a burst of late wakes): the map is WITHHELD; only once the moved phase has been stable for
//    FALD_VBLANK_REBASE_AFTER publish attempts is it adopted as a new chain (re-based to the nearest whole count, a new
//    epoch; the phase points are dropped and the period re-fitted first). A slip is never published unseen. Before
//    the ring has turned over, a RECENT-WAKE guard catches a moved phase early: if none of the last
//    FALD_VBLANK_RECENT_WAKES wakes lies within FALD_VBLANK_RECENT_LATE periods of the model's vblank, the map is
//    withheld (a display re-timed by more than that is published wrongly for at most ~FALD_VBLANK_RECENT_WAKES refreshes;
//    a smaller re-timing until the wake ring turns over, ~0.5 s, and then only for runs in the last sliver of a refresh).
//  * DwmSample(...): DWM's composition clock against the target output — DIAGNOSTICS ONLY, never an input: the phase of
//    qpcVBlank on the target's vblank grid over time gives the relative period difference in ppm (the owner's BenQ-clocked
//    DWM: about +990 ppm) and whether DWM's clock display is this output (|ppm| < FALD_VBLANK_SAME_CLOCK_PPM).
constexpr unsigned int FALD_VBLANK_WAKE_RING = 32;       // wakes the vblank instant is estimated from
constexpr unsigned int FALD_VBLANK_GAP_RING = 64;        // single-step gaps the median period is taken over
constexpr unsigned int FALD_VBLANK_PHASE_RING = 64;      // phase points the period is fitted on (one per publish attempt)
constexpr unsigned int FALD_VBLANK_MIN_WAKES = 16;
constexpr unsigned int FALD_VBLANK_MIN_GAPS = 8;
constexpr unsigned int FALD_VBLANK_MODEL_MIN_WAKES = 4;   // wakes before the phase model numbers wakes (nominal period)
constexpr unsigned int FALD_VBLANK_FIT_MIN = 120;        // refreshes the phase points must span before the fit is used
constexpr unsigned int FALD_VBLANK_MAX_GAP = 240;        // refreshes ever counted through without a fitted chain ...
constexpr unsigned int FALD_VBLANK_MAX_GAP_FIT = 7200;   // ... and with one (both then capped by the gap budget)
constexpr double FALD_VBLANK_EARLY_TOLERANCE = 0.05;     // the numbering's early tolerance e: at least this ...
constexpr double FALD_VBLANK_EARLY_TOLERANCE_MAX = 0.25; // ... at most this (periods) ...
constexpr double FALD_VBLANK_EARLY_MARGIN = 0.05;        // ... = (model vblank's height above the floor) + this
constexpr double FALD_VBLANK_GAP_BUDGET = 0.05;          // phase error a counted-through gap may accumulate (periods)
constexpr double FALD_VBLANK_LINE_BOUND_K = 4.0;         // line slope uncertainty = K max|residual| / span (worst case: 3)
constexpr double FALD_VBLANK_DRIFT_FLOOR = 2.0e-5;       // ... at least this (unmodelled latency-floor drift: 20 ppm)
constexpr double FALD_VBLANK_NOMINAL_TOLERANCE = 0.001;  // fitted period vs the nominal one (relative)
constexpr double FALD_VBLANK_FIT_MEDIAN_TOLERANCE = 0.01;   // fit vs the median of the gaps (a sanity bound)
constexpr double FALD_VBLANK_PHASE_TOLERANCE = 0.1;      // chain residual off a whole number of periods (periods)
constexpr unsigned int FALD_VBLANK_RECENT_WAKES = 8;     // the recent-wake guard (one of them near the model's vblank)
constexpr double FALD_VBLANK_RECENT_LATE = 0.15;         // ... within this many periods of it
constexpr unsigned int FALD_VBLANK_REBASE_AFTER = 16;    // stable publish attempts before a moved phase is adopted
constexpr unsigned int FALD_VBLANK_DWM_RING = 64;        // DWM-clock diagnostic samples
constexpr unsigned int FALD_VBLANK_DWM_MIN_SPAN = 240;   // refreshes they must span before a drift is reported
constexpr double FALD_VBLANK_SAME_CLOCK_PPM = 20.0;      // |drift| below this: DWM's clock display is the target output
struct FaldVBlankTracker {
    double nominalPeriodQpc = 0.0;            // from the mode description (DisplayConfig); 0 = unknown (no map)
    double medianPeriodQpc = 0.0;             // median of the single-step gaps (0 until FALD_VBLANK_MIN_GAPS)
    double periodQpc = 0.0;                   // the period in use: fit / median / nominal
    bool periodFit = false;                   // periodQpc is the phase-point fit (required for a map)
    double fitPeriodQpc = 0.0;                // the last fit candidate, accepted or not (log)
    double lineBound = 0.0;                   // the chain line's period uncertainty, periods per refresh (0 = no fit):
                                              // max(K max|residual| / span, FALD_VBLANK_DRIFT_FLOOR) — the gap gate
    bool haveWake = false;
    long long lastWakeQpc = 0;
    unsigned long long v = 0;                 // vblank index of the last accepted wake = THE COUNT (origin: Reset's)
    long long wakeQpc[FALD_VBLANK_WAKE_RING] = {};
    unsigned long long wakeV[FALD_VBLANK_WAKE_RING] = {};
    unsigned int wakeN = 0, wakeHead = 0;
    double gap[FALD_VBLANK_GAP_RING] = {};
    unsigned int gapN = 0, gapHead = 0;
    long long phaseQpc[FALD_VBLANK_PHASE_RING] = {};
    unsigned long long phaseV[FALD_VBLANK_PHASE_RING] = {};
    unsigned int phaseN = 0, phaseHead = 0;
    // the continuity chain: the last published map's anchor and index
    bool haveRef = false;
    long long refQpc = 0;
    unsigned long long refV = 0;
    long long lineBase = 0;                   // (the reference line's time origin: VBlankLineAt)
    unsigned int held = 0;                    // consecutive publish attempts withheld by a moved phase
    unsigned int gapHold = 0;                 // fed wakes still owed after a counted-through gap before a publish: the
                                              // recent-wake guard must see post-gap wakes only (review 2026-10-05)
    bool haveMoved = false;                   // the last withheld attempt (the moved phase's own stability)
    long long movedQpc = 0;
    unsigned long long movedV = 0;
    unsigned int movedStable = 0;
    unsigned long long epoch = 0;             // numbering epochs inside this tracker (restart / re-base)
    // DWM's composition clock vs this output (diagnostics only)
    bool dwmHave = false;
    long long dwmT0 = 0;
    double dwmLastFrac = 0.0, dwmUnwrapped = 0.0;
    double dwmX[FALD_VBLANK_DWM_RING] = {}, dwmY[FALD_VBLANK_DWM_RING] = {};
    unsigned int dwmN = 0, dwmHead = 0;
    double dwmPpm = 0.0;                      // DWM's period minus this output's, relative, in ppm (when dwmState >= 0)
    int dwmState = -1;                        // -1 unknown, 0 another display's clock, 1 this output's clock
    unsigned long long dwmLastCount = 0;      // the last cRefresh / qpcRefreshPeriod seen (log)
    double dwmLastPeriodQpc = 0.0;
    // statistics (host log; the bench reads them)
    unsigned long long wakes = 0, missed = 0, spurious = 0, lateWakes = 0, restarts = 0;   // (restarts: gaps)
    unsigned long long published = 0, slipsRepaired = 0, phaseJumps = 0, rebases = 0, recentLate = 0, dwmSamples = 0;
    unsigned long long gapHolds = 0;
};
// A fresh tracker whose first wake gets index firstIndex (the publisher continues its numbering across re-binds).
void FaldVBlankTrackerReset(FaldVBlankTracker* t, double nominalPeriodQpc, unsigned long long firstIndex = 0);
// Returns the refreshes since the previous accepted wake: 0 = ignored (the first wake / a restart also return 0 after
// seeding the model), 1 = the next vblank, > 1 = missed wakes repaired.
unsigned int FaldVBlankTrackerWake(FaldVBlankTracker* t, long long qpc);
// The vblank instant of index v from the phase model; false before FALD_VBLANK_MODEL_MIN_WAKES wakes / without a period.
bool FaldVBlankTrackerVBlankQpc(const FaldVBlankTracker* t, unsigned long long v, long long* qpc);
// The numbering's early tolerance now (periods; rules above). FALD_VBLANK_EARLY_TOLERANCE without a model.
double FaldVBlankTrackerEarlyTolerance(const FaldVBlankTracker* t);
// The longest gap (refreshes between two accepted wakes) counted through now: min(cap, FALD_VBLANK_GAP_BUDGET / u) with
// u = lineBound and cap FALD_VBLANK_MAX_GAP_FIT once the period is fitted on a chain, else u =
// FALD_VBLANK_NOMINAL_TOLERANCE and cap FALD_VBLANK_MAX_GAP. A longer gap restarts the model in a new epoch.
unsigned int FaldVBlankTrackerMaxGap(const FaldVBlankTracker* t);
// One publish attempt (the chain above). True = *out is a map to publish; false = withhold / withdraw.
bool FaldVBlankTrackerMap(FaldVBlankTracker* t, FaldVBlankMap* out);
// DWM's composition timing at nowQpc (qpcVBlank, cRefresh, qpcRefreshPeriod of DWM_TIMING_INFO): diagnostics only.
void FaldVBlankTrackerDwmSample(FaldVBlankTracker* t, long long nowQpc, long long qpcVBlank, unsigned long long cRefresh,
                                double qpcRefreshPeriod);
// The publisher's spin guard, judging every vblank wait by ITS OWN duration (call start -> return; as Chromium's
// VSyncThreadWin): under min(FALD_VBLANK_MIN_WAIT_MS, FALD_VBLANK_MIN_WAIT_PERIODS x the period) it did not wait for a
// vblank (monitor asleep, output gone; the period term keeps 480 Hz+ displays — whose every wait is < 1 ms — out of it) — not fed
// to the tracker, the thread sleeps a period; FALD_VBLANK_SPIN_LIMIT of them in a row = ASLEEP (withdraw the map,
// re-bind). A legitimately short wait (the loop came back less than 1 ms before a vblank: it stalled ~a period) is only
// a missed wake, which the next wake's numbering repairs. (Review 2026-10-04: the previous rule compared consecutive
// RETURNS; the guard's own one-period sleep reset it, so the escalation never fired and ~56 fake wakes a second reached
// the tracker while a display slept.)
constexpr double FALD_VBLANK_MIN_WAIT_MS = 1.0;
constexpr double FALD_VBLANK_MIN_WAIT_PERIODS = 0.25;
constexpr unsigned int FALD_VBLANK_SPIN_LIMIT = 30;
enum FaldVBlankWaitVerdict : unsigned int {
    FALD_VBLANK_WAIT_WAKE = 0,     // a real wait: feed the return to the tracker
    FALD_VBLANK_WAIT_EARLY = 1,    // returned without waiting: skip it, sleep a period
    FALD_VBLANK_WAIT_ASLEEP = 2,   // FALD_VBLANK_SPIN_LIMIT early returns in a row: withdraw, re-bind, back off
};
struct FaldVBlankSpinGuard {
    unsigned int spins = 0;                   // early returns in a row
    unsigned long long early = 0, asleep = 0; // statistics
};
// periodQpc = the output's nominal refresh period (<= 0: the 1-ms rule alone).
FaldVBlankWaitVerdict FaldVBlankSpinGuardStep(FaldVBlankSpinGuard* g, long long waitStartQpc, long long returnQpc,
                                              long long qpcFreq, double periodQpc = 0.0);
// The publisher's numbering-epoch bookkeeping (pure; src/dwm_inject.cpp VBlankPublisherThread draws the random ids).
// A NEW epoch id — so a calibrated parity stops applying at once (fald_temporal.h fail-safe) — on: the first id, a
// (re-)binding, a tracker epoch change (restart / re-base), a FAILED vblank wait or a run of
// FALD_VBLANK_EPOCH_EARLY_RUN early returns (the output went away or did not wait, however briefly: its timing may restart
// anywhere, and a restarted timing that lands within the chain's tolerance would otherwise be absorbed silently — review
// 2026-10-05). Feed every wait outcome (FAILED / EARLY) and every point where the numbering may have moved (WAKE after
// the tracker's Wake, CHECK after its Map / a binding); a nonzero return = draw a new id NOW, before anything is
// published, and says why.
constexpr unsigned int FALD_VBLANK_EPOCH_EARLY_RUN = 2;
enum FaldVBlankEpochEvent : unsigned int {
    FALD_VBLANK_EV_WAKE = 0,       // a wait returned a vblank and the tracker took it
    FALD_VBLANK_EV_EARLY = 1,      // a wait returned without waiting (spin guard EARLY)
    FALD_VBLANK_EV_FAILED = 2,     // a wait failed (output off / gone)
    FALD_VBLANK_EV_CHECK = 3,      // after a binding / a publish attempt (no wait outcome)
};
enum FaldVBlankEpochReason : unsigned int {
    FALD_VBLANK_EPOCH_SAME = 0,
    FALD_VBLANK_EPOCH_BIND = 1,    // the first id / a (re-)binding
    FALD_VBLANK_EPOCH_TRACKER = 2, // the tracker restarted or re-based its numbering
    FALD_VBLANK_EPOCH_FAILED = 3,  // a failed wait since the last id
    FALD_VBLANK_EPOCH_EARLY = 4,   // an early-return run since the last id
};
struct FaldVBlankEpochKeeper {
    bool haveId = false;
    unsigned long long bind = 0, trkEpoch = 0;   // what the current id was drawn for
    unsigned int earlyRun = 0;
    unsigned int suspect = FALD_VBLANK_EPOCH_SAME;   // a failed wait / an early run since: the next check draws an id
};
FaldVBlankEpochReason FaldVBlankEpochKeeperStep(FaldVBlankEpochKeeper* k, unsigned long long bind, unsigned long long trkEpoch,
                                                FaldVBlankEpochEvent ev);
const char* FaldVBlankEpochReasonText(unsigned int reason);

// The publisher's periodic re-check of its output (every few seconds of TIME):
//  * REBIND (withdraw the map, find the output at the monitor's position again, a new epoch) when its description is
//    unreadable, it left the desktop or no longer starts at the monitor's position (a swap / layout change: the posTag
//    would name the wrong output), or the nominal refresh period changed (a mode set);
//  * REFRESH when only the DXGI factory went stale: re-acquire the factory + output objects and KEEP the tracker and the
//    epoch. Windows marks factories stale on display-stack changes that do not touch the timing — the owner's DesktopLUT
//    re-applies its MHC2 profiles every 5 min (Calibration Loader) and every kick staled the factory while the vblank
//    waits ran on untouched (2026-10-05 probe: 6061 waits, 0 failed / early / gapped) — re-binding there started a new
//    numbering epoch every 5 min. A REAL mode set re-binds through the modeset signal (DwmInjectDisplayChanged on
//    WM_DISPLAYCHANGE) and the checks above; a moved phase is the tracker's chain's to catch.
enum FaldVBlankRecheckResult : unsigned int {
    FALD_VBLANK_RECHECK_OK = 0,
    FALD_VBLANK_RECHECK_REFRESH = 1,
    FALD_VBLANK_RECHECK_REBIND = 2,
};
FaldVBlankRecheckResult FaldVBlankRecheck(bool factoryCurrent, bool descOk, bool attached, int outLeft, int outTop, int left,
                                          int top, float nominalMs, float nominalNowMs);

// The per-monitor temporal bookkeeping (no D3D). The overlay's FaldResources and the hook's FaldMonitor both DERIVE
// from it, so every field is reached the same way (r->clkIndex) and the functions below take either.
struct FaldTemporalState {
    // delay ring (modes 1 / 2): indices only — the textures are the caller's
    unsigned int delayHead = 0, delayCount = 0;
    unsigned int delayFrames = 0;            // FaldSettings::delayFrames at the last run
    bool stateValid = false;                 // the state texture holds a committed map (else the next pass copies the drive)
    unsigned int temporalMode = 0;           // FaldSettings::temporalMode at the last run (a change resets the state)
    float tempAlphaRise = 1.0f, tempAlphaFall = 1.0f;   // the CB words of the last frame (dump)
    unsigned int settleLeft = 0;             // frames of redraw hold still owed after the last content change
    float dtMs = 16.667f;                    // EMA of the interval between consecutive runs while rendering continuously
    long long lastRunQpc = 0;
    // panel clock (mode 3)
    float clkClosure = FALD_CLOCK_CLOSURE_DEFAULT;   // the clamped settings + the refresh period of the last run: a change
    int clkParity = -1;                              // of any of them resets the clocks
    int clkParityEff = -1;                           // the parity the last run applied (fail-safe; dump)
    float clkRefreshMs = 0.0f;
    long long clkOriginQpc = 0;              // QPC origin of the two times below (the seeding run; rebased hourly)
    double clkTimeMs = 0.0;                  // the last run's time since the origin (dump: k can be replayed offline)
    double clkGridMs = 0.0;                  // centre of the last run's refresh on the phase-locked grid, since the origin
    double clkResidual = 0.0;                // the last run's phase residual r, in periods (-0.5 .. 0.5; dump)
    double clkGain = 0.0;                    // the lock gain a the last run used (dump)
    unsigned int clkLockRuns = 0;            // runs since the seed / the last long pause (the acquisition gain 1 / (runs + 1))
    unsigned long long clkIndex = 0;         // panel-refresh index n of the last run (0 = the seeding refresh)
    unsigned long long clkElapsed = 0;       // k of the last run: refreshes since the previous run (0 = the same refresh)
    bool clkSeeded = false;                  // the last run (re-)seeded the clocks: the stateless layer, no clock pass
    float clkFactor[4] = { 0.0f, 0.0f, 0.0f, 0.0f };   // CB words 68-71 of the last run: aTrue_0, aEst_0, aTrue_1, aEst_1
    float clkW[2] = { 0.5f, 0.5f };                     // CB words 66-67: the clocks' weights
    // stage 3 (the absolute refresh index; FaldPanelClockStep with a vblank map). clkAbs drives the next run's source
    // step; the rest is dump / log only (the overlay never has a map: it stays on the grid).
    bool clkAbs = false;                     // the last run's index came from the vblank map (else the grid's numbering)
    unsigned int clkSource = FALD_CLOCK_SRC_GRID;   // FaldClockSource of the last run
    bool clkMapOk = false;                   // the last run was offered a USABLE map
    long long clkRunQpc = 0, clkQpcFreq = 0; // the last run's QPC time + frequency (n can be recomputed offline)
    unsigned long long clkAbsIndex = 0;      // the map's n at the last run (when clkMapOk)
    unsigned long long clkGridK = 0;         // the grid's k at the last run, whichever source drove the clock
    FaldVBlankMap clkMap;                    // the map the last run was offered (zeros = none)
    double clkMapAgeMs = 0.0;                // the last run's time after the map's anchor
    // counters since the monitor's state was created (log / dump): runs per source, events, and the runs whose
    // absolute k differed from the grid's (each one a re-roll the grid would have made)
    unsigned long long clkRunsAbs = 0, clkRunsGrid = 0, clkSwitches = 0, clkRebases = 0, clkHolds = 0, clkKDiffs = 0;
};

// Per-run bookkeeping of the panel clock (the caller passes the run's QPC time): applies the reset rules and the time
// law above to stateValid / clkOriginQpc / clkGridMs / clkLockRuns / clkIndex / clkElapsed / clkSeeded / clkFactor /
// clkW. Returns true when this run (re-)SEEDS the clocks: the frame is the stateless layer and its round-1 drives become
// both clocks' state. Otherwise clkElapsed = k (0 = hold the previous run's maps, >= 1 = run the clock pass). Time
// running backwards by more than two periods, an unusable period or QPC frequency: re-seed. The stored times stay small
// (the origin is rebased every hour of grid time); the index is 64-bit.
// vblankMap (stage 3, optional): when FaldAbsRefreshIndex accepts it, the clock is numbered with the ABSOLUTE index
// (FaldClockSourceStep; a seed takes n as its index) and parity 0 / 1 mean even / odd vblank counts. Null / unusable:
// bit for bit the grid clock above.
// parityEpoch: the epoch `parity` belongs to (fail-safe above; default ANY = the setting applies as such — direct
// callers / tests; the run orchestration passes FaldTemporalSettings::clockParityEpoch, default NONE). Only the SETTING
// parity's changes re-seed; the effective parity (clkParityEff) only sets the weights.
bool FaldPanelClockStep(FaldTemporalState* r, long long nowQpc, long long qpcFreq, float closure, int parity, float refreshMs,
                        const FaldVBlankMap* vblankMap = nullptr, unsigned long long parityEpoch = FALD_PARITY_EPOCH_ANY);
// What a run does with the step's result (pure, so the rules are testable without D3D): runPass = dispatch the
// clock pass (k >= 1), bindMaps = both rounds' kernels read the clock's true / est maps (every non-seeding run, k = 0
// INCLUDED: the previous run's maps), seedStates = copy this frame's round-1 drives into both clocks, commitPrev = this
// frame's round-1 drives become the next target (ALWAYS: on k = 0 the later frame replaces the earlier one on the panel).
struct FaldClockPlan { bool runPass; bool bindMaps; bool seedStates; bool commitPrev; };
FaldClockPlan FaldPanelClockPlan(bool seeded, unsigned long long elapsed);
// Settle-hold accounting after a run (pure): rearm (new content / a resume) = the full hold; otherwise the first-order
// modes pay ONE per run, the panel clock pays the ELAPSED REFRESHES (perRefresh: a k = 0 run pays nothing, one slow run
// pays k); never above `settle` (a time constant lowered mid-hold).
void FaldSettleAccount(FaldTemporalState* r, bool rearm, unsigned int settle, bool perRefresh);

// ---------------------------------------------------------------------------------------------------------------------
// One run of the layer, start to end — the orchestration both paths share.
// ---------------------------------------------------------------------------------------------------------------------
// The temporal settings of the mode the monitor is in (FaldSettings fields; the hook receives them in the tuning tail).
struct FaldTemporalSettings {
    unsigned int mode = FALD_TEMPORAL_OFF;   // 0..3; anything else = off
    float tauRiseMs = 0.0f, tauFallMs = 0.0f;
    unsigned int delayFrames = 0;
    float clockClosure = FALD_CLOCK_CLOSURE_DEFAULT;
    int clockParity = -1;
    unsigned long long clockParityEpoch = FALD_PARITY_EPOCH_NONE;   // the epoch clockParity belongs to (fail-safe)
};
// Which drive map a kernel / pass reads this run (the caller maps these onto its own textures).
enum FaldDriveMap : unsigned int {
    FALD_MAP_DRIVE = 0,        // this round's instantaneous drive (driveTex)
    FALD_MAP_FILTERED = 1,     // the temporal pass's output (driveFiltTex; mode 3: the clock pass's B_true map)
    FALD_MAP_CLOCK_EST = 2,    // mode 3: the clock pass's B_est map (clkEstTex)
};
struct FaldTemporalRun {
    unsigned int mode;         // the mode that RUNS (3 falls back to 0 when the caller has no clock textures)
    bool temporal;             // modes 1 / 2: run the temporal pass after each statistic round
    bool panel;                // mode 3
    unsigned int delay;        // clamped delayFrames of this run
    int delayedSlot;           // modes 1 / 2: the ring slot the temporal pass is fed (-1 = the instantaneous drive)
    FaldDriveMap trueMap;      // what the real-spread kernel (B_true) reads
    FaldDriveMap estMap;       // what the estimate kernel (B_est) reads
    FaldDriveMap debugFiltMap; // what debug view 7's "filtered" slot shows (t10 of the pixel pass)
    FaldClockPlan clock;       // mode 3 plan (all false otherwise)
    bool resumed;              // first run after a gap > FALD_RESUME_GAP_MS
    bool stateReset;           // the mode changed: the caller's ring / state textures are void (indices already reset)
};
// Start of a run: the dt estimate (EMA over 2..100 ms intervals; a longer gap = `resumed`), the mode (a change forgets
// the state, the hold and the ring), the delay depth (a change restarts the ring), the blend factors (tempAlphaRise /
// Fall), the panel clock's step, and which map each kernel reads. clockTexturesOk: the caller could create its clock
// textures (mode 3 runs as off otherwise). refreshMs: the monitor's nominal refresh period (mode 3's grid). vblankMap:
// stage 3's absolute refresh index (the DWM hook passes the host's published map; null = the grid, as before).
FaldTemporalRun FaldTemporalBeginRun(FaldTemporalState* r, const FaldTemporalSettings& s, bool clockTexturesOk,
                                     long long nowQpc, long long qpcFreq, float refreshMs,
                                     const FaldVBlankMap* vblankMap = nullptr);
// End of a run, AFTER the caller has done the copies the plan implies, in this order: modes 1 / 2 — copy the filtered
// map into the state texture, and (delay > 0) the round-1 instantaneous drive into ring slot r->delayHead; mode 3 —
// clock.seedStates: the round-1 drive into both clock states, clock.commitPrev: the round-1 drive into clkPrev.
// This commits the indices (stateValid, the ring head / count) and does the settle accounting; newContent = the frame
// is new content (not a re-run for the settle hold).
void FaldTemporalEndRun(FaldTemporalState* r, const FaldTemporalRun& run, const FaldTemporalSettings& s, bool newContent);
// True while the state still owes settle frames after the last content change.
bool FaldTemporalSettlePending(const FaldTemporalState* r);
// The layer did not run (disabled, refused, wrong format, hook-mode overlay): forget the state, the hold and the ring —
// a state kept across a layer-OFF period would blend the new content with the OLD content's drives when the layer comes
// back on a static desktop, and nothing would settle it (design review 2026-09-17).
void FaldTemporalIdle(FaldTemporalState* r);
// The panel clock's textures were released (src/fald.cpp / dwm_hook ReleaseClock): the clocks' state is void, so the next
// run RE-SEEDS (stateValid = false) — the seed rule "clkSeeded && k == 0" must never see a k = 0 run bind maps that no
// pass has written since. (Modes 0-2 are unaffected: ReleaseClock only runs on a rebuild, while mode 3 is (re)starting,
// or when leaving mode 3 — a mode change that resets the state anyway.)
void FaldTemporalClockReleased(FaldTemporalState* r);
