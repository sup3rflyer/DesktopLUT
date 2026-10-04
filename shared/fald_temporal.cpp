// DesktopLUT - shared/fald_temporal.cpp — see fald_temporal.h. Compiled into DesktopLUT.exe, DwmHook.dll and the tests.
#include "fald_temporal.h"

#include <cmath>
#include <cwchar>

// Temporal drive state helpers (DLC dlc/fald/temporal.py alpha_from_tau / settle_frames; tests/test_fald.cpp).
float FaldTemporalAlpha(float tauMs, float dtMs) {
    if (!(tauMs > 0.0f) || !(dtMs > 0.0f)) return 1.0f;
    return 1.0f - std::exp(-dtMs / tauMs);
}
unsigned int FaldSettleFrames(float tauRiseMs, float tauFallMs, float dtMs, unsigned int delayFrames) {
    const unsigned int delay = delayFrames > FALD_DELAY_MAX ? FALD_DELAY_MAX : delayFrames;
    float tau = tauRiseMs > tauFallMs ? tauRiseMs : tauFallMs;
    if (!(tau > 0.0f) || !(dtMs > 0.0f)) return delay;
    double n = std::ceil(5.0 * (double)tau / (double)dtMs - 1e-4);   // 5 tau; the tolerance keeps exact multiples exact (float32 dt)
    return (n < 1.0 ? 1u : (n > 100000.0 ? 100000u : (unsigned int)n)) + delay;
}

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
// ---------------------------------------------------------------------------------------------------------------------
// Stage 3: the absolute refresh index (rules: fald_temporal.h above FALD_VBLANK_LATENCY_REFRESHES; DLC twin
// dlc/fald/paneltime.py abs_refresh_index / clock_source_step)
// ---------------------------------------------------------------------------------------------------------------------
bool FaldAbsRefreshIndex(const FaldVBlankMap& map, long long tQpc, long long qpcFreq, float nominalRefreshMs, int latency,
                         unsigned long long* n, double* ageMs) {
    if (!n || qpcFreq <= 0) return false;
    const double period = map.periodQpc;
    if (!(period > 0.0) || !std::isfinite(period)) return false;
    const double periodMs = period * 1000.0 / (double)qpcFreq;
    if (!(periodMs >= 1.0 && periodMs <= 1000.0)) return false;
    // shared-memory words: never let a garbage anchor overflow the arithmetic below
    if (map.qpcAnchor <= 0 || map.countAnchor < 0 || map.countAnchor > (1ll << 62)) return false;
    const long long d = (long long)((unsigned long long)tQpc - (unsigned long long)map.qpcAnchor);   // wraps defined
    const double age = (double)d * 1000.0 / (double)qpcFreq;
    if (ageMs) *ageMs = age;
    if (nominalRefreshMs > 0.0f && std::fabs(periodMs / (double)nominalRefreshMs - 1.0) > FALD_VBLANK_PERIOD_TOLERANCE)
        return false;                                              // another mode's map (a refresh change in flight)
    if (!(age <= FALD_VBLANK_MAP_MAX_AGE_MS) || age < -2.0 * periodMs) return false;   // stale / from the future
    const long long idx = map.countAnchor + (long long)std::floor((double)d / period) + (long long)latency;
    if (idx < 0) return false;
    *n = (unsigned long long)idx;
    return true;
}
FaldClockSource FaldClockSourceStep(unsigned long long prevIndex, bool prevAbs, unsigned long long kGrid, bool absOk,
                                    unsigned long long absN, unsigned long long& k, unsigned long long& n) {
    FaldClockSource src;
    if (!absOk) {
        k = kGrid; n = prevIndex + kGrid;
        src = prevAbs ? FALD_CLOCK_SRC_SWITCH_GRID : FALD_CLOCK_SRC_GRID;
    } else if (!prevAbs) {
        k = kGrid; n = absN;
        src = FALD_CLOCK_SRC_SWITCH_ABS;
    } else {
        const long long d = (long long)(absN - prevIndex);         // signed refreshes since the previous run (wraps defined)
        const long long kg = kGrid > (1ull << 62) ? (1ll << 62) : (long long)kGrid;
        if (d >= 0 && d <= kg + 2 && d + 2 >= kg) { k = (unsigned long long)d; n = absN; src = FALD_CLOCK_SRC_ABS; }
        else if (d == -1 && kGrid <= 1ull) { k = 0; n = prevIndex; src = FALD_CLOCK_SRC_HOLD; }
        else { k = kGrid; n = absN; src = FALD_CLOCK_SRC_REBASE; }
    }
    if (k > n) k = n;
    return src;
}
const char* FaldClockSourceName(unsigned int source) {
    switch (source) {
        case FALD_CLOCK_SRC_GRID: return "grid";
        case FALD_CLOCK_SRC_ABS: return "abs";
        case FALD_CLOCK_SRC_SWITCH_ABS: return "switch_abs";
        case FALD_CLOCK_SRC_HOLD: return "hold";
        case FALD_CLOCK_SRC_REBASE: return "rebase";
        case FALD_CLOCK_SRC_SWITCH_GRID: return "switch_grid";
        default: return "?";
    }
}

// The publisher's tracker (rules: fald_temporal.h above FALD_VBLANK_WAKE_RING)
static void VBlankRestartModel(FaldVBlankTracker* t) {
    // everything that depends on the local numbering goes; the nominal period, DWM's period and the statistics stay
    FaldVBlankTracker fresh;
    fresh.nominalPeriodQpc = t->nominalPeriodQpc; fresh.periodQpc = t->nominalPeriodQpc; fresh.dwmPeriodQpc = t->dwmPeriodQpc;
    fresh.wakes = t->wakes; fresh.missed = t->missed; fresh.spurious = t->spurious; fresh.lateWakes = t->lateWakes;
    fresh.restarts = t->restarts; fresh.samples = t->samples; fresh.rejectedEdge = t->rejectedEdge;
    fresh.rejectedSlow = t->rejectedSlow; fresh.conflicts = t->conflicts; fresh.reanchors = t->reanchors;
    fresh.anchorJumps = t->anchorJumps;
    *t = fresh;
}
void FaldVBlankTrackerReset(FaldVBlankTracker* t, double nominalPeriodQpc) {
    *t = FaldVBlankTracker();
    t->nominalPeriodQpc = (nominalPeriodQpc > 0.0 && std::isfinite(nominalPeriodQpc)) ? nominalPeriodQpc : 0.0;
    t->periodQpc = t->nominalPeriodQpc;
}
static void VBlankPushWake(FaldVBlankTracker* t, long long qpc) {
    t->wakeQpc[t->wakeHead] = qpc; t->wakeV[t->wakeHead] = t->v;
    t->wakeHead = (t->wakeHead + 1) % FALD_VBLANK_WAKE_RING;
    if (t->wakeN < FALD_VBLANK_WAKE_RING) t->wakeN++;
}
// The period in use: the least-squares fit over the phase points when it exists and agrees with the median of the
// single-step gaps within 1 % (a sanity bound only: with a few hundred us of wake jitter the median of 64 gaps scatters
// by ~0.1 %; a fit outside it means something moved — the median then), the median once there are enough gaps, the
// nominal period before that. Only a fitted period is published (FaldVBlankTrackerMap): the median's few-us-per-refresh
// error, projected over the wake ring and extrapolated by the hook, would cost a large part of a millisecond.
static void VBlankUpdatePeriod(FaldVBlankTracker* t, double fitPeriodQpc) {
    double p = t->nominalPeriodQpc;
    if (t->medianPeriodQpc > 0.0) p = t->medianPeriodQpc;
    t->periodFit = fitPeriodQpc > 0.0 && t->medianPeriodQpc > 0.0 && std::fabs(fitPeriodQpc / t->medianPeriodQpc - 1.0) < 0.01;
    if (t->periodFit) p = fitPeriodQpc;
    t->periodQpc = p;
}
static double VBlankFitPeriod(const FaldVBlankTracker* t) {
    const unsigned int n = t->phaseN;
    if (n < 3u) return 0.0;
    unsigned long long vMin = t->phaseV[0], vMax = t->phaseV[0];
    for (unsigned int i = 1; i < n; i++) { vMin = t->phaseV[i] < vMin ? t->phaseV[i] : vMin; vMax = t->phaseV[i] > vMax ? t->phaseV[i] : vMax; }
    if (vMax - vMin < (unsigned long long)FALD_VBLANK_FIT_MIN) return 0.0;
    const long long q0 = t->phaseQpc[0];
    double mx = 0.0, my = 0.0;
    for (unsigned int i = 0; i < n; i++) { mx += (double)(t->phaseV[i] - vMin); my += (double)(t->phaseQpc[i] - q0); }
    mx /= (double)n; my /= (double)n;
    double sxy = 0.0, sxx = 0.0;
    for (unsigned int i = 0; i < n; i++) {
        const double dx = (double)(t->phaseV[i] - vMin) - mx, dy = (double)(t->phaseQpc[i] - q0) - my;
        sxy += dx * dy; sxx += dx * dx;
    }
    return sxx > 0.0 ? sxy / sxx : 0.0;
}
unsigned int FaldVBlankTrackerWake(FaldVBlankTracker* t, long long qpc) {
    t->wakes++;
    if (!t->haveWake) {
        t->haveWake = true; t->v = 0; t->lastWakeQpc = qpc;
        VBlankPushWake(t, qpc);
        return 0;
    }
    const double P = t->periodQpc;
    if (!(P > 0.0)) { t->lastWakeQpc = qpc; return 0; }        // no period at all: nothing can be numbered
    // Number the wake against the model's vblank instant of the previous index (the latency is one-sided: floor(x + 1/4)
    // takes a wake up to 3/4 of a period late); until the model exists, against the previous wake (symmetric: round).
    long long ref = t->lastWakeQpc;
    const bool model = FaldVBlankTrackerVBlankQpc(t, t->v, &ref);
    const double x = (double)(qpc - ref) / P;
    const double s = std::floor(x + (model ? 0.25 : 0.5));
    if (s < 1.0) { t->spurious++; return 0; }                  // a second wake inside the same refresh
    if (s > (double)FALD_VBLANK_MAX_GAP) {                     // the thread was gone for seconds: start over
        t->restarts++;
        VBlankRestartModel(t);
        t->haveWake = true; t->v = 0; t->lastWakeQpc = qpc;
        VBlankPushWake(t, qpc);
        return 0;
    }
    const unsigned int steps = (unsigned int)s;
    // a numbering anomaly (repaired misses, a wake more than half a period late) could have moved the local index by
    // one: no map until a mid-frame read confirms the offset again (the publisher keeps its last map meanwhile)
    if (model && x - s > 0.5) { t->lateWakes++; t->needConfirm = true; }
    if (steps > 1u) t->needConfirm = true;
    if (steps == 1u) {
        t->gap[t->gapHead] = (double)(qpc - t->lastWakeQpc);
        t->gapHead = (t->gapHead + 1) % FALD_VBLANK_GAP_RING;
        if (t->gapN < FALD_VBLANK_GAP_RING) t->gapN++;
        if (t->gapN >= FALD_VBLANK_MIN_GAPS) {
            double g[FALD_VBLANK_GAP_RING];
            for (unsigned int i = 0; i < t->gapN; i++) g[i] = t->gap[i];
            for (unsigned int i = 1; i < t->gapN; i++) {       // insertion sort (<= 64 values)
                const double key = g[i]; unsigned int j = i;
                while (j > 0 && g[j - 1] > key) { g[j] = g[j - 1]; j--; }
                g[j] = key;
            }
            t->medianPeriodQpc = (t->gapN % 2u) ? g[t->gapN / 2] : 0.5 * (g[t->gapN / 2 - 1] + g[t->gapN / 2]);
            VBlankUpdatePeriod(t, VBlankFitPeriod(t));
        }
    } else {
        t->missed += steps - 1u;
    }
    t->v += steps; t->lastWakeQpc = qpc;
    VBlankPushWake(t, qpc);
    return steps;
}
bool FaldVBlankTrackerVBlankQpc(const FaldVBlankTracker* t, unsigned long long v, long long* qpc) {
    if (!t->haveWake || t->wakeN < FALD_VBLANK_MIN_GAPS || !(t->periodQpc > 0.0) || !qpc) return false;
    // every recent wake projected onto index v (relative to the last wake: small numbers); a low quantile = the wakes
    // with the least latency (index n / 16: the 3rd smallest of a full ring — a stray early value or two cannot pull it)
    double d[FALD_VBLANK_WAKE_RING];
    const unsigned int n = t->wakeN;
    for (unsigned int i = 0; i < n; i++)
        d[i] = (double)(t->wakeQpc[i] - t->lastWakeQpc) + (double)(long long)(v - t->wakeV[i]) * t->periodQpc;
    for (unsigned int i = 1; i < n; i++) {
        const double key = d[i]; unsigned int j = i;
        while (j > 0 && d[j - 1] > key) { d[j] = d[j - 1]; j--; }
        d[j] = key;
    }
    *qpc = t->lastWakeQpc + (long long)std::llround(d[n / 16u]);
    return true;
}
FaldVBlankSampleResult FaldVBlankTrackerSample(FaldVBlankTracker* t, long long qpcBefore, long long qpcAfter,
                                               unsigned long long count, double dwmPeriodQpc) {
    t->samples++;
    if (dwmPeriodQpc > 0.0 && std::isfinite(dwmPeriodQpc)) t->dwmPeriodQpc = dwmPeriodQpc;
    const double P = t->periodQpc;
    long long a = 0;
    if (t->wakeN < FALD_VBLANK_MIN_WAKES || !FaldVBlankTrackerVBlankQpc(t, t->v, &a)) return FALD_VSAMPLE_NO_PHASE;
    if (qpcAfter < qpcBefore || (double)(qpcAfter - qpcBefore) > 0.1 * P) { t->rejectedSlow++; return FALD_VSAMPLE_SLOW; }
    // the read happened somewhere between before and after: its middle, in periods after the model's vblank of index v
    const double x = 0.5 * ((double)(qpcBefore - a) + (double)(qpcAfter - a)) / P;
    const double f = std::floor(x);
    const double phase = x - f;
    if (phase < FALD_VBLANK_SAMPLE_PHASE_LO || phase > FALD_VBLANK_SAMPLE_PHASE_HI) { t->rejectedEdge++; return FALD_VSAMPLE_NEAR_EDGE; }
    const long long vs = (long long)t->v + (long long)f;           // the local index of the refresh the read was made in
    const long long c = (long long)count - vs;
    auto vote = [t, c]() {
        if (t->pendingVotes > 0u && t->pending == c) t->pendingVotes++;
        else { t->pending = c; t->pendingVotes = 1u; }
    };
    // a read that agrees with the offset (or establishes / re-anchors it) confirms the CURRENT numbering: it clears a
    // pending anomaly and restarts the anchor-jump reference (FaldVBlankTrackerMap)
    auto confirm = [t, vs]() {
        t->confirmV = (unsigned long long)vs; t->withheld = false; t->needConfirm = false; t->pendingVotes = 0;
        t->haveRef = false;
    };
    if (!t->offsetValid) {                                      // two agreeing reads establish the offset
        vote();
        if (t->pendingVotes >= 2u) { t->offset = c; t->offsetValid = true; confirm(); }
        return FALD_VSAMPLE_ACCEPTED;
    }
    if (c == t->offset) {                                       // confirmed: a pending disagreement was a glitch
        confirm();
        return FALD_VSAMPLE_ACCEPTED;
    }
    t->conflicts++;
    t->withheld = true;                                         // never publish a count a read just contradicted
    vote();
    if (t->pendingVotes >= FALD_VBLANK_REANCHOR_VOTES) {
        t->offset = c; confirm();
        t->reanchors++;
        return FALD_VSAMPLE_REANCHORED;
    }
    return FALD_VSAMPLE_CONFLICT;
}
bool FaldVBlankTrackerMap(FaldVBlankTracker* t, FaldVBlankMap* out) {
    long long a = 0;
    if (t->wakeN < FALD_VBLANK_MIN_WAKES || !FaldVBlankTrackerVBlankQpc(t, t->v, &a)) return false;
    // a phase point for the period fit (one per index)
    const unsigned int last = (t->phaseHead + FALD_VBLANK_PHASE_RING - 1u) % FALD_VBLANK_PHASE_RING;
    if (t->phaseN == 0u || t->phaseV[last] != t->v) {
        t->phaseQpc[t->phaseHead] = a; t->phaseV[t->phaseHead] = t->v;
        t->phaseHead = (t->phaseHead + 1u) % FALD_VBLANK_PHASE_RING;
        if (t->phaseN < FALD_VBLANK_PHASE_RING) t->phaseN++;
        VBlankUpdatePeriod(t, VBlankFitPeriod(t));
        FaldVBlankTrackerVBlankQpc(t, t->v, &a);                // (the anchor under the updated period)
    }
    // Anchor-jump guard: since the last confirming read, the vblank instant of the current index must lie where the
    // previous map's numbering puts it (within a quarter period). A jump means the local numbering moved (a slip a read
    // has not seen yet — e.g. this cycle's read was rejected): no map until a read confirms.
    const double P = t->periodQpc;
    if (t->haveRef && std::fabs((double)(a - t->refQpc) - (double)(long long)(t->v - t->refV) * P) > 0.25 * P) {
        if (!t->needConfirm) t->anchorJumps++;
        t->needConfirm = true;
    }
    if (!t->periodFit || !t->offsetValid || t->withheld || t->needConfirm || !out) return false;
    if ((long long)(t->v - t->confirmV) > (long long)FALD_VBLANK_CONFIRM_MAX) return false;   // not re-confirmed lately
    if (!(t->nominalPeriodQpc > 0.0) || std::fabs(P / t->nominalPeriodQpc - 1.0) > FALD_VBLANK_RATE_TOLERANCE) return false;
    if (t->dwmPeriodQpc > 0.0 && std::fabs(t->dwmPeriodQpc / P - 1.0) > FALD_VBLANK_RATE_TOLERANCE) return false;
    const long long count = t->offset + (long long)t->v;
    if (count < 0) return false;
    out->countAnchor = count; out->qpcAnchor = a; out->periodQpc = P;
    t->refQpc = a; t->refV = t->v; t->haveRef = true;
    return true;
}

bool FaldPanelClockStep(FaldTemporalState* r, long long nowQpc, long long qpcFreq, float closure, int parity, float refreshMs,
                        const FaldVBlankMap* vblankMap) {
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
    // stage 3: the absolute index of this run's refresh, when a usable vblank map came with it (recorded for the dump
    // whatever happens below)
    unsigned long long absN = 0;
    double mapAge = 0.0;
    const bool absOk = vblankMap && FaldAbsRefreshIndex(*vblankMap, nowQpc, qpcFreq, refreshMs, FALD_VBLANK_LATENCY_REFRESHES,
                                                        &absN, &mapAge);
    r->clkMapOk = absOk; r->clkAbsIndex = absOk ? absN : 0ull;
    r->clkMap = vblankMap ? *vblankMap : FaldVBlankMap();
    r->clkMapAgeMs = vblankMap ? mapAge : 0.0;
    r->clkRunQpc = nowQpc; r->clkQpcFreq = qpcFreq;
    double x = 0.0;
    if (r->stateValid) {
        r->clkTimeMs = (double)(nowQpc - r->clkOriginQpc) * 1000.0 / (double)qpcFreq;
        x = (r->clkTimeMs - r->clkGridMs) / period;
        if (x < -2.0) r->stateValid = false;                       // time ran backwards: start over
    }
    if (!r->stateValid) {
        r->clkOriginQpc = nowQpc; r->clkTimeMs = 0.0; r->clkGridMs = 0.0; r->clkResidual = 0.0; r->clkGain = 0.0;
        r->clkLockRuns = 0; r->clkElapsed = 0; r->clkSeeded = true;
        r->clkIndex = absOk ? absN : 0ull;                         // the seed's refresh: absolute when the map is usable
        r->clkAbs = absOk; r->clkSource = absOk ? FALD_CLOCK_SRC_ABS : FALD_CLOCK_SRC_GRID; r->clkGridK = 0;
        if (absOk) r->clkRunsAbs++; else r->clkRunsGrid++;
        r->stateValid = true;
        FaldPanelClockFactors(0, 1, closure, parity, r->clkFactor, r->clkW);   // (weights for the dump; the pass does not run)
        return true;
    }
    // the phase-locked grid (rules: fald_temporal.h above FALD_TEMPORAL_PANEL; DLC twin: dlc/fald/paneltime.py RefreshGrid)
    double kf = std::floor(x + 0.5);
    if (kf < 0.0) kf = 0.0;
    const unsigned long long kGrid = kf >= 9.0e18 ? 9000000000000000000ull : (unsigned long long)kf;
    double res = x - kf;
    res = res < -0.5 ? -0.5 : (res > 0.5 ? 0.5 : res);
    if (r->clkLockRuns < 1000000u) r->clkLockRuns++;
    double gain = 1.0 / ((double)r->clkLockRuns + 1.0);
    if (gain < FALD_CLOCK_LOCK_GAIN) gain = FALD_CLOCK_LOCK_GAIN;
    if (kGrid > FALD_CLOCK_MAX_REFRESHES) r->clkLockRuns = 0;      // a long pause: the phase is stale, acquire it again
    r->clkGridMs += (kf + gain * res) * period;
    r->clkResidual = res; r->clkGain = gain;
    // which index drives the clock (stage 3): the grid's numbering as before, or the map's absolute one
    const bool prevAbs = r->clkAbs;
    unsigned long long k = 0, n = 0;
    const FaldClockSource src = FaldClockSourceStep(r->clkIndex, prevAbs, kGrid, absOk, absN, k, n);
    r->clkAbs = absOk; r->clkSource = src; r->clkGridK = kGrid;
    if (absOk) r->clkRunsAbs++; else r->clkRunsGrid++;
    if (src == FALD_CLOCK_SRC_SWITCH_ABS || src == FALD_CLOCK_SRC_SWITCH_GRID) r->clkSwitches++;
    else if (src == FALD_CLOCK_SRC_REBASE) r->clkRebases++;
    else if (src == FALD_CLOCK_SRC_HOLD) r->clkHolds++;
    if ((src == FALD_CLOCK_SRC_ABS || src == FALD_CLOCK_SRC_HOLD) && k != kGrid) r->clkKDiffs++;
    // still inside the seeding refresh: the later frame replaces the seed (the grid keeps locking meanwhile) = "the
    // previous run seeded and no refresh elapsed". On a grid-only history this is exactly the pre-stage-3 test n == 0:
    // a grid seed is refresh 0, every non-seeding run has n >= 1 and clkSeeded false (ReleaseClock's clkIndex = 0 is
    // always followed by a re-seed: the state is void then). It must NOT be n == 0 once an absolute seed happened — a
    // seed at n = N followed by map-less k = 0 runs would otherwise bind clock maps no pass has written yet.
    (void)prevAbs;
    const bool seedRefresh = r->clkSeeded && k == 0ull;
    if (seedRefresh) {
        r->clkIndex = n; r->clkElapsed = 0; r->clkSeeded = true;
        return true;
    }
    if (k > 0ull) FaldPanelClockFactors(n - k, k, closure, parity, r->clkFactor, r->clkW);   // k = 0: the previous
    r->clkElapsed = k; r->clkIndex = n; r->clkSeeded = false;                                // run's words stand
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
void FaldSettleAccount(FaldTemporalState* r, bool rearm, unsigned int settle, bool perRefresh) {
    if (rearm) r->settleLeft = settle;
    else if (perRefresh) r->settleLeft -= (r->clkElapsed < (unsigned long long)r->settleLeft) ? (unsigned int)r->clkElapsed : r->settleLeft;
    else if (r->settleLeft > 0) r->settleLeft--;
    if (r->settleLeft > settle) r->settleLeft = settle;
}

// ---------------------------------------------------------------------------------------------------------------------
// One run, start to end (moved out of src/fald.cpp FaldRunPasses unchanged in behaviour)
// ---------------------------------------------------------------------------------------------------------------------
FaldTemporalRun FaldTemporalBeginRun(FaldTemporalState* r, const FaldTemporalSettings& s, bool clockTexturesOk,
                                     long long nowQpc, long long qpcFreq, float refreshMs,
                                     const FaldVBlankMap* vblankMap) {
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
    unsigned int mode = (s.mode <= FALD_TEMPORAL_PANEL) ? s.mode : FALD_TEMPORAL_OFF;
    if (mode == FALD_TEMPORAL_PANEL && !clockTexturesOk) mode = FALD_TEMPORAL_OFF;   // a failed creation runs as off
    // No usable clock (refresh period unknown, no QPC frequency): every run would re-seed with k = 0, and the settle
    // hold — paid in elapsed refreshes — would never end (a host kicking DWM forever). The mode runs as off instead.
    // (The overlay always has a period: capture.cpp falls back to 16.667 ms.)
    if (mode == FALD_TEMPORAL_PANEL && (!(refreshMs > 0.0f) || qpcFreq <= 0)) mode = FALD_TEMPORAL_OFF;
    run.stateReset = false;
    if (mode != r->temporalMode) {
        r->temporalMode = mode; r->stateValid = false; r->settleLeft = 0; r->delayCount = 0;
        run.stateReset = true;
    }
    const unsigned int delay = s.delayFrames > FALD_DELAY_MAX ? FALD_DELAY_MAX : s.delayFrames;
    if (delay != r->delayFrames) { r->delayFrames = delay; r->delayCount = 0; }   // a changed depth restarts the ring
    run.mode = mode;
    run.delay = delay;
    run.panel = (mode == FALD_TEMPORAL_PANEL);
    run.temporal = (mode != FALD_TEMPORAL_OFF) && !run.panel;   // the first-order filter (pass 1b, modes 1 / 2)
    // the map the panel's pipeline is fed this frame: the ring entry `delay` frames back once the ring holds that many
    // (DriveState.delayed), else the instantaneous drive. Both rounds are fed the same map.
    run.delayedSlot = -1;
    if (run.temporal && delay > 0 && r->delayCount >= delay)
        run.delayedSlot = (int)((r->delayHead + FALD_DELAY_MAX - delay) % FALD_DELAY_MAX);
    r->tempAlphaRise = FaldTemporalAlpha(s.tauRiseMs, r->dtMs);
    r->tempAlphaFall = FaldTemporalAlpha(s.tauFallMs, r->dtMs);
    run.trueMap = FALD_MAP_DRIVE;   // what the real-spread kernel sees
    run.estMap = FALD_MAP_DRIVE;    // what the estimate kernel sees
    if (mode == FALD_TEMPORAL_BOTH) { run.trueMap = FALD_MAP_FILTERED; run.estMap = FALD_MAP_FILTERED; }
    else if (mode == FALD_TEMPORAL_TRUE_ONLY) { run.trueMap = FALD_MAP_FILTERED; }
    run.clock = FaldClockPlan{ false, false, false, false };
    if (run.panel) {
        const bool seeded = FaldPanelClockStep(r, nowQpc, qpcFreq, s.clockClosure, s.clockParity, refreshMs, vblankMap);
        run.clock = FaldPanelClockPlan(seeded, r->clkElapsed);
        if (run.clock.bindMaps) { run.trueMap = FALD_MAP_FILTERED; run.estMap = FALD_MAP_CLOCK_EST; }
    }
    // debug view 7: instantaneous vs filtered (mode 3: vs the clocks' mean LED state of this frame)
    run.debugFiltMap = (run.temporal || run.clock.bindMaps) ? FALD_MAP_FILTERED : FALD_MAP_DRIVE;
    return run;
}

void FaldTemporalEndRun(FaldTemporalState* r, const FaldTemporalRun& run, const FaldTemporalSettings& s, bool newContent) {
    if (run.temporal) {                    // DriveState.commit (the caller copied the filtered map into the state texture
        r->stateValid = true;              // and, delay > 0, round 1's instantaneous map into ring slot delayHead)
        if (run.delay > 0) {
            r->delayHead = (r->delayHead + 1) % FALD_DELAY_MAX;
            if (r->delayCount < FALD_DELAY_MAX) r->delayCount++;
        }
    }
    // Settle hold: the capture (Desktop Duplication) / DWM delivers no frames on a static desktop, so a state still
    // settling after the last content frame would freeze mid-transition. Owe 5 tau of settle frames after new content. A
    // resume after a gap (> FALD_RESUME_GAP_MS since the last run: the desktop stood still past the hold, or the layer
    // was asleep) counts as new content too — the first frame after it may carry a change the pass blends from the
    // settled state. Mode 3 owes FaldPanelClockSettleFrames instead, counted in ELAPSED REFRESHES of this monitor (k),
    // not in runs: a run loop may run faster than the panel refreshes (k = 0 runs pay nothing) or slower (one run pays k).
    if (run.temporal || run.panel) {
        const unsigned int settle = run.panel ? FaldPanelClockSettleFrames(r->clkClosure)
                                              : FaldSettleFrames(s.tauRiseMs, s.tauFallMs, r->dtMs, run.delay);
        FaldSettleAccount(r, newContent || run.resumed, settle, run.panel);   // (also: tau lowered mid-hold)
    } else {
        r->settleLeft = 0;
    }
}

bool FaldTemporalSettlePending(const FaldTemporalState* r) {
    return r && r->temporalMode != FALD_TEMPORAL_OFF && r->settleLeft > 0;
}

void FaldTemporalIdle(FaldTemporalState* r) {
    if (!r) return;
    r->stateValid = false;
    r->settleLeft = 0;
    r->delayCount = 0;
    r->lastRunQpc = 0;
}
