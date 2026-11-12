// DesktopLUT - shared/fald_temporal.cpp — see fald_temporal.h. Compiled into DesktopLUT.exe, DwmHook.dll and the tests.
#include "fald_temporal.h"

#include <cmath>
#include <cwchar>

// Panel clock (temporal mode 3) helpers: DLC dlc/fald/paneltime.py clock_ticks / blend_factors / settle_refreshes and
// dlc/fald/gpuemu.py clock_factors32 (the float32 twin); tests/test_fald.cpp.
float FaldPanelClockClosure(float closure) {
    if (closure != closure) return FALD_CLOCK_CLOSURE_DEFAULT;
    return closure < FALD_CLOCK_CLOSURE_MIN ? FALD_CLOCK_CLOSURE_MIN : (closure > FALD_CLOCK_CLOSURE_MAX ? FALD_CLOCK_CLOSURE_MAX : closure);
}
int FaldPanelClockParity(int parity) { return (parity == 0 || parity == 1) ? parity : -1; }
int FaldPanelClockParityFromText(const wchar_t* text) {
    if (!text) return -1;
    wchar_t* end = nullptr;
    const long v = std::wcstol(text, &end, 10);
    if (end == text) return -1;                                    // empty / no number: unknown, never "0"
    while (*end == L' ' || *end == L'\t') end++;
    if (*end != L'\0' || v < -1 || v > 1) return -1;               // trailing garbage / out of range
    return (int)v;
}
void FaldPanelClockTicks(unsigned long long nA, unsigned int k, unsigned int parity, unsigned int& tTrue, unsigned int& tEst) {
    if (k < 1u) k = 1u;
    const unsigned long long p = parity & 1u;
    const unsigned long long upTo = (nA + p) / 2ull;               // ticks at refreshes <= nA (up to a constant)
    tTrue = (unsigned int)((nA + k + p) / 2ull - upTo);
    tEst = (unsigned int)((nA + k - 1ull + p) / 2ull - upTo);
}
void FaldPanelClockFactors(unsigned long long nA, unsigned long long k, float closure, int parity, float factor[4], float weight[2]) {
    const float q = 1.0f - FaldPanelClockClosure(closure);
    auto blend = [q](unsigned int ticks) { float r = 1.0f; for (unsigned int i = 0; i < ticks; i++) r *= q; return 1.0f - r; };
    for (unsigned int p = 0; p < 2u; p++) {
        if (k > FALD_CLOCK_MAX_REFRESHES) { factor[2 * p] = 1.0f; factor[2 * p + 1] = 1.0f; continue; }   // a long pause: settled
        unsigned int tTrue = 0, tEst = 0;
        FaldPanelClockTicks(nA, (unsigned int)k, p, tTrue, tEst);
        factor[2 * p] = blend(tTrue); factor[2 * p + 1] = blend(tEst);
    }
    const int par = FaldPanelClockParity(parity);
    weight[0] = (par < 0) ? 0.5f : (par == 0 ? 1.0f : 0.0f);
    weight[1] = (par < 0) ? 0.5f : (par == 1 ? 1.0f : 0.0f);
}
unsigned int FaldPanelClockSettleFrames(float closure) {
    const double c = (double)FaldPanelClockClosure(closure);
    double m = (c >= 1.0) ? 1.0 : std::ceil(std::log(0.0005) / std::log(1.0 - c) - 1e-9);
    if (m < 1.0) m = 1.0;
    const double n = 2.0 * m + 2.0;
    return n > (double)FALD_CLOCK_SETTLE_MAX ? FALD_CLOCK_SETTLE_MAX : (unsigned int)n;
}
bool FaldPanelClockStep(FaldTemporalState* r, long long nowQpc, long long qpcFreq, float closure, int parity, float refreshMs) {
    closure = FaldPanelClockClosure(closure); parity = FaldPanelClockParity(parity);
    if (closure != r->clkClosure || parity != r->clkParity || refreshMs != r->clkRefreshMs) r->stateValid = false;
    r->clkClosure = closure; r->clkParity = parity; r->clkRefreshMs = refreshMs;
    const double period = (double)refreshMs;
    if (!(period > 0.0) || qpcFreq <= 0) r->stateValid = false;    // no usable clock: every run is a seeding run
    if (r->stateValid && r->clkGridMs > 3.6e6) {
        // keep the stored times small: move the origin forward by whole QPC ticks (nothing else changes)
        const long long ticks = (long long)(std::floor(r->clkGridMs) * (double)qpcFreq / 1000.0);
        r->clkOriginQpc += ticks;
        r->clkGridMs -= (double)ticks * 1000.0 / (double)qpcFreq;
    }
    double x = 0.0;
    if (r->stateValid) {
        r->clkTimeMs = (double)(nowQpc - r->clkOriginQpc) * 1000.0 / (double)qpcFreq;
        x = (r->clkTimeMs - r->clkGridMs) / period;
        if (x < -2.0) r->stateValid = false;                       // time ran backwards: start over
    }
    if (!r->stateValid) {
        r->clkOriginQpc = nowQpc; r->clkTimeMs = 0.0; r->clkGridMs = 0.0; r->clkResidual = 0.0; r->clkGain = 0.0;
        r->clkLockRuns = 0; r->clkIndex = 0; r->clkElapsed = 0; r->clkSeeded = true;
        r->stateValid = true;
        FaldPanelClockFactors(0, 1, closure, parity, r->clkFactor, r->clkW);   // (weights for the dump; the pass does not run)
        return true;
    }
    // the phase-locked grid (rules: fald_temporal.h above FALD_TEMPORAL_PANEL; DLC twin: dlc/fald/paneltime.py RefreshGrid)
    double kf = std::floor(x + 0.5);
    if (kf < 0.0) kf = 0.0;
    const unsigned long long k = kf >= 9.0e18 ? 9000000000000000000ull : (unsigned long long)kf;
    double res = x - kf;
    res = res < -0.5 ? -0.5 : (res > 0.5 ? 0.5 : res);
    if (r->clkLockRuns < 1000000u) r->clkLockRuns++;
    double gain = 1.0 / ((double)r->clkLockRuns + 1.0);
    if (gain < FALD_CLOCK_LOCK_GAIN) gain = FALD_CLOCK_LOCK_GAIN;
    if (k > FALD_CLOCK_MAX_REFRESHES) r->clkLockRuns = 0;          // a long pause: the phase is stale, acquire it again
    r->clkGridMs += (kf + gain * res) * period;
    r->clkResidual = res; r->clkGain = gain;
    const unsigned long long n = r->clkIndex + k;
    if (n == 0ull) {
        // still inside the seeding refresh: the later frame replaces the seed (the grid keeps locking meanwhile)
        r->clkElapsed = 0; r->clkSeeded = true;
        return true;
    }
    if (k > 0ull) FaldPanelClockFactors(r->clkIndex, k, closure, parity, r->clkFactor, r->clkW);   // k = 0: the previous
    r->clkElapsed = k; r->clkIndex = n; r->clkSeeded = false;                                      // run's words stand
    return false;
}
FaldClockPlan FaldPanelClockPlan(bool seeded, unsigned long long elapsed) {
    FaldClockPlan plan;
    plan.runPass = !seeded && elapsed >= 1ull;
    plan.bindMaps = !seeded;
    plan.seedStates = seeded;
    plan.commitPrev = true;
    return plan;
}
void FaldSettleAccount(FaldTemporalState* r, bool rearm, unsigned int settle) {
    if (rearm) r->settleLeft = settle;
    else r->settleLeft -= (r->clkElapsed < (unsigned long long)r->settleLeft) ? (unsigned int)r->clkElapsed : r->settleLeft;
    if (r->settleLeft > settle) r->settleLeft = settle;
}

// ---------------------------------------------------------------------------------------------------------------------
// One run, start to end (moved out of src/fald.cpp FaldRunPasses unchanged in behaviour)
// ---------------------------------------------------------------------------------------------------------------------
FaldTemporalRun FaldTemporalBeginRun(FaldTemporalState* r, const FaldTemporalSettings& s, bool clockTexturesOk,
                                     long long nowQpc, long long qpcFreq, float refreshMs) {
    FaldTemporalRun run = {};
    // dt = the interval between consecutive runs while rendering continuously (EMA over 2..100 ms intervals; a long
    // static gap keeps the last estimate — the response starts at the new frame, however long the desktop stood still)
    run.resumed = false;
    if (r->lastRunQpc != 0 && qpcFreq > 0) {
        float iv = (float)((double)(nowQpc - r->lastRunQpc) * 1000.0 / (double)qpcFreq);
        if (iv >= 2.0f && iv <= 100.0f) r->dtMs = 0.9f * r->dtMs + 0.1f * iv;
        else if (iv > FALD_RESUME_GAP_MS) run.resumed = true;
    }
    r->lastRunQpc = nowQpc;
    unsigned int mode = FaldTemporalModeOf(s.mode);
    if (mode == FALD_TEMPORAL_PANEL && !clockTexturesOk) mode = FALD_TEMPORAL_OFF;   // a failed creation runs as off
    // No usable clock (refresh period unknown, no QPC frequency): every run would re-seed with k = 0, and the settle
    // hold — paid in elapsed refreshes — would never end (a host kicking DWM forever). The mode runs as off instead.
    // (The overlay always has a period: capture.cpp falls back to 16.667 ms.)
    if (mode == FALD_TEMPORAL_PANEL && (!(refreshMs > 0.0f) || qpcFreq <= 0)) mode = FALD_TEMPORAL_OFF;
    run.stateReset = false;
    if (mode != r->temporalMode) {
        r->temporalMode = mode; r->stateValid = false; r->settleLeft = 0;
        run.stateReset = true;
    }
    run.mode = mode;
    run.panel = (mode == FALD_TEMPORAL_PANEL);
    run.trueMap = FALD_MAP_DRIVE;   // what the real-spread kernel sees
    run.estMap = FALD_MAP_DRIVE;    // what the estimate kernel sees
    run.clock = FaldClockPlan{ false, false, false, false };
    if (run.panel) {
        const bool seeded = FaldPanelClockStep(r, nowQpc, qpcFreq, s.clockClosure, s.clockParity, refreshMs);
        run.clock = FaldPanelClockPlan(seeded, r->clkElapsed);
        if (run.clock.bindMaps) { run.trueMap = FALD_MAP_FILTERED; run.estMap = FALD_MAP_CLOCK_EST; }
    }
    // debug view 7: instantaneous vs the clocks' mean LED state of this frame
    run.debugFiltMap = run.clock.bindMaps ? FALD_MAP_FILTERED : FALD_MAP_DRIVE;
    return run;
}

void FaldTemporalEndRun(FaldTemporalState* r, const FaldTemporalRun& run, bool newContent) {
    // Settle hold: the capture (Desktop Duplication) / DWM delivers no frames on a static desktop, so a state still
    // settling after the last content frame would freeze mid-transition. The panel clock owes
    // FaldPanelClockSettleFrames after new content, counted in ELAPSED REFRESHES of this monitor (k), not in runs: a run
    // loop may run faster than the panel refreshes (k = 0 runs pay nothing) or slower (one run pays k). A resume after a
    // gap (> FALD_RESUME_GAP_MS since the last run: the desktop stood still past the hold, or the layer was asleep) counts
    // as new content too — the first frame after it may carry a change the pass blends from the settled state.
    if (run.panel) FaldSettleAccount(r, newContent || run.resumed, FaldPanelClockSettleFrames(r->clkClosure));
    else r->settleLeft = 0;
}

bool FaldTemporalSettlePending(const FaldTemporalState* r) {
    return r && r->temporalMode != FALD_TEMPORAL_OFF && r->settleLeft > 0;
}

void FaldTemporalIdle(FaldTemporalState* r) {
    if (!r) return;
    r->stateValid = false;
    r->settleLeft = 0;
    r->lastRunQpc = 0;
}
