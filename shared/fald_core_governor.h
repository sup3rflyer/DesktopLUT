// DesktopLUT - shared/fald_core_governor.h
// The core FALD layer's rate-cap governor (2026-10-22, the owner's decision after the core-speed work): the layer's FULL
// path (the zone statistics, the two convolutions, the gains, the pixel pass — everything but the flank fade, which has
// its own governor, fald_fade_governor.h) costs GPU time on every present that changes enough (the change-driven path,
// fald_change.h, takes the small changes). On a device where that does not fit the refresh interval the layer is never
// paused: its full path is run at a CAPPED RATE, and the presents in between keep the standing fields and run only the
// pixel pass (a "held" run: the new content corrected with the previous full run's fields — the dimming lags by at most
// the cap's spacing, the picture never does).
//
// The rule:
//   * budget = kBudgetShare (15 %) of the REFRESH interval;
//   * the p90 of the full path's GPU time over the last kWindow full runs (at least kMinSamples) above the budget ->
//     Capped; kHardRun full runs in a row each longer than a whole refresh interval cap at once (an overrun that drops
//     frames on every content frame: a weak GPU at 4K);
//   * capped: full runs at most every `spacing` = p90 / kBudgetShare of wall time (the average GPU share stays at the
//     budget: p90 every spacing), never less than the refresh interval, never more than kMaxSpacingMs; the full runs
//     keep being measured and the spacing follows the p90;
//   * released (Running again) when the p90 of the capped full runs is at or below budget / kReleaseMargin (hysteresis
//     against flapping), on a mode change (SetMode: measured afresh), or the layer switched off and on;
//   * forced (INI [General] FaldCoreForce=1, REFERENCE.md): never capped, the numbers still measured.
// Pure logic (no D3D, no clock of its own: the callers pass times), ONE implementation for the overlay (src/fald.cpp)
// and the hook (dwm_hook/hook_fald.cpp); unit-tested with injected timings (tests/test_fald_core_governor.cpp). The GPU
// timing both hosts feed it is FaldCoreTiming below (timestamp queries read without a flush or a wait).
#pragma once

#include <stdint.h>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Query;

// Wire values (dwm_hook_config.h DwmHookFaldCoreStatusEntry::state, IPC state.get fald_core_governor.state)
enum class FaldCoreGovState : uint32_t {
    Off = 0,       // the layer is off
    Running = 1,   // every full run runs (measured)
    Capped = 2,    // over budget: the full path at most every `spacing`, held runs in between
    Forced = 3,    // the INI force key: every full run runs whatever it costs (measured)
};
const char* FaldCoreGovStateName(FaldCoreGovState s);   // "off", "running", "capped", "forced"

struct FaldCoreGovernorStatus {
    FaldCoreGovState state = FaldCoreGovState::Off;
    double p90Us = 0.0;       // the p90 of the window (capped: of the capped full runs since the cap, or the one that capped)
    double budgetUs = 0.0;    // kBudgetShare x the refresh interval
    double refreshMs = 0.0;   // the refresh interval the budget comes from
    double spacingMs = 0.0;   // capped: the wall time between full runs (0 otherwise)
    double contentMs = 0.0;   // the content-frame interval (median over the window; information)
    unsigned int samples = 0; // GPU samples in the window
    uint32_t generation = 0;  // bumps on every state change
};

class FaldCoreGovernor {
public:
    static constexpr double kBudgetShare = 0.15;
    static constexpr unsigned int kWindow = 120;
    static constexpr unsigned int kMinSamples = 30;
    static constexpr unsigned int kHardRun = 3;
    static constexpr double kReleaseMargin = 1.25;
    static constexpr double kMaxSpacingMs = 1000.0;     // a cap never holds the fields longer than this
    static constexpr double kMaxIntervalMs = 1000.0;    // a longer content gap counts as this (a static desktop)
    static constexpr double kDefaultRefreshMs = 1000.0 / 60.0;   // an unknown refresh period

    // The force key. A change re-evaluates the state.
    void Configure(bool forced);
    // The layer runs (on) or not; off -> on measures afresh.
    void SetEnabled(bool on);
    // The mode the layer runs in (hdr, size, refresh — any key): a change measures afresh.
    void SetMode(uint64_t modeKey);
    // The refresh interval (ms; <= 0 = unknown -> kDefaultRefreshMs): the budget's base.
    void SetRefreshMs(double ms);
    // May the full path run NOW (the caller's clock, ms)? Running / Forced: always; Capped: when `spacing` has passed
    // since the last full run (less half a refresh interval, so a present cadence never aliases to one more present).
    bool FullAllowed(double nowMs) const;
    // The caller runs the full path now (its start time on the caller's clock).
    void OnFullRun(double nowMs);
    // A content frame at `nowMs`: the content-interval clock (information for the status).
    void OnContentFrame(double nowMs);
    // One full run's measured GPU time of the core passes (any state but Off).
    void OnSample(double coreUs);
    FaldCoreGovernorStatus Status() const;
    FaldCoreGovState State() const { return state_; }
    double BudgetUs() const { return kBudgetShare * refreshMs_ * 1000.0; }
    double SpacingMs() const { return state_ == FaldCoreGovState::Capped ? spacingMs_ : 0.0; }
    double MedianIntervalMs() const;  // over the window (0 = none)
    double P90() const;               // over the sample window (0 = none)

private:
    void Restart();                   // samples and the interval window cleared; Running unless forced / off
    void Cap(double p90Us);
    void Bump() { generation_++; }
    double SpacingFor(double p90Us) const;

    FaldCoreGovState state_ = FaldCoreGovState::Off;
    bool enabled_ = false, forced_ = false, configured_ = false;
    uint64_t modeKey_ = 0;
    bool haveMode_ = false;
    double refreshMs_ = kDefaultRefreshMs;
    double samples_[kWindow] = {};
    unsigned int nSamples_ = 0, headSamples_ = 0;
    double intervals_[kWindow] = {};
    unsigned int nIntervals_ = 0, headIntervals_ = 0;
    double lastFrameMs_ = 0.0;
    bool haveLastFrame_ = false;
    unsigned int hardRun_ = 0;
    double spacingMs_ = 0.0, capP90Us_ = 0.0;
    double lastFullMs_ = 0.0;
    bool haveLastFull_ = false;
    uint32_t generation_ = 0;
};

// Test seam (both hosts; tests only): -1 = the governor decides; 1 = every run whose fields stand is a held run; 0 = never
// held (the governor still measures and reports).
extern int g_faldCoreHoldOverride;

// The core passes' GPU time per run for the governor, in both hosts: a ring of kSlots timestamp-query sets, kStamps
// stamps each, read back with DONOTFLUSH only once the GPU is done (a slot still in flight when its turn comes round
// skips timing that run). The stamps of a run, in order:
//   0 the run's start (before the starfield)   1 after the starfield (before the fade's frame passes)
//   2 after the fade's frame passes             3 before the fade's reach of round 0
//   4 after it                                  5 before the fade's reach of round 1
//   6 after it                                  7 the run's end (after the pixel pass)
// core = (1 - 0) + (3 - 2) + (5 - 4) + (7 - 6); total = 7 - 0; star = 1 - 0; fade = total - core. A host that does not run a
// segment stamps its two ends back to back (zero). A held run is not timed (it is not a full run).
struct FaldCoreTimingSample { double coreUs, totalUs, starUs; void* owner; };
struct FaldCoreTiming {
    static const int kSlots = 4;
    static const int kStamps = 8;
    ID3D11Query* dj[kSlots] = {};
    ID3D11Query* ts[kSlots][kStamps] = {};
    bool inFlight[kSlots] = {};
    void* owner[kSlots] = {};        // what a slot's result is credited to (the hook: the monitor)
    unsigned int head = 0;
    bool ok = false;
};
bool FaldCoreTimingCreate(FaldCoreTiming* t, ID3D11Device* dev);
void FaldCoreTimingRelease(FaldCoreTiming* t);
// Finished slots, oldest first (no flush, no wait): n <= maxOut samples.
unsigned int FaldCoreTimingCollect(FaldCoreTiming* t, ID3D11DeviceContext* ctx, FaldCoreTimingSample* out, unsigned int maxOut);
// The slot started for this run (stamp 0 issued), or -1 (no queries, or the slot is still in flight).
int FaldCoreTimingBegin(FaldCoreTiming* t, ID3D11DeviceContext* ctx, void* owner);
// Stamp k (1 .. kStamps - 2) of the slot; the last stamp + the disjoint query's end: End.
void FaldCoreTimingStamp(FaldCoreTiming* t, ID3D11DeviceContext* ctx, int slot, int k);
void FaldCoreTimingEnd(FaldCoreTiming* t, ID3D11DeviceContext* ctx, int slot);
