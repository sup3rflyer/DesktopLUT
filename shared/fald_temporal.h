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
// re-rolls on display mode sets, so it needs the presented refresh index + a one-bit calibration; not built); at a tick every zone's LED drive closes `closure` of the remaining
// gap to the drive demanded by the frame that was on the panel ONE refresh earlier (same up and down); the panel's own
// LCD compensation uses the LED state of ONE refresh earlier. The first-order filter of modes 1 / 2 cannot express
// this. Per zone and parity clock p the layer keeps the LED state S_p; with k = the panel refreshes elapsed since the
// previous processed frame was first shown (Desktop Duplication delivers frames on change only), every tick in between
// targeted that frame's round-1 drives d_prev, so t ticks are ONE blend a = 1 - (1 - closure)^t:
//   B_true of this frame from S_p + aTrue_p (d_prev - S_p)   (ticks up to this refresh; committed as the new S_p)
//   B_est  of this frame from S_p + aEst_p  (d_prev - S_p)   (ticks up to the refresh before it)
// parity -1 (unknown, the default) = the mean of the two clocks (the fields are linear in the drives), 0 / 1 = that
// clock only (EXPERIMENTAL: the refresh index comes from run timing, not from DXGI frame statistics). The state of a
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
};

// Per-run bookkeeping of the panel clock (the caller passes the run's QPC time): applies the reset rules and the time
// law above to stateValid / clkOriginQpc / clkGridMs / clkLockRuns / clkIndex / clkElapsed / clkSeeded / clkFactor /
// clkW. Returns true when this run (re-)SEEDS the clocks: the frame is the stateless layer and its round-1 drives become
// both clocks' state. Otherwise clkElapsed = k (0 = hold the previous run's maps, >= 1 = run the clock pass). Time
// running backwards by more than two periods, an unusable period or QPC frequency: re-seed. The stored times stay small
// (the origin is rebased every hour of grid time); the index is 64-bit.
bool FaldPanelClockStep(FaldTemporalState* r, long long nowQpc, long long qpcFreq, float closure, int parity, float refreshMs);
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
// textures (mode 3 runs as off otherwise). refreshMs: the monitor's nominal refresh period (mode 3's grid).
FaldTemporalRun FaldTemporalBeginRun(FaldTemporalState* r, const FaldTemporalSettings& s, bool clockTexturesOk,
                                     long long nowQpc, long long qpcFreq, float refreshMs);
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
