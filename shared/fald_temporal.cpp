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
static void VBlankClearDwm(FaldVBlankTracker* t) {
    t->dwmHave = false; t->dwmN = 0; t->dwmHead = 0; t->dwmState = -1; t->dwmPpm = 0.0; t->dwmUnwrapped = 0.0;
}
// Everything that depends on the current phase model goes; the nominal period, the count (v), the epoch and the
// statistics stay.
static void VBlankRestartModel(FaldVBlankTracker* t) {
    FaldVBlankTracker fresh;
    fresh.nominalPeriodQpc = t->nominalPeriodQpc; fresh.periodQpc = t->nominalPeriodQpc;
    fresh.v = t->v; fresh.epoch = t->epoch;
    fresh.wakes = t->wakes; fresh.missed = t->missed; fresh.spurious = t->spurious; fresh.lateWakes = t->lateWakes;
    fresh.restarts = t->restarts; fresh.published = t->published; fresh.slipsRepaired = t->slipsRepaired;
    fresh.phaseJumps = t->phaseJumps; fresh.rebases = t->rebases; fresh.recentLate = t->recentLate;
    fresh.dwmSamples = t->dwmSamples;
    fresh.dwmLastCount = t->dwmLastCount; fresh.dwmLastPeriodQpc = t->dwmLastPeriodQpc;
    *t = fresh;
}
void FaldVBlankTrackerReset(FaldVBlankTracker* t, double nominalPeriodQpc, unsigned long long firstIndex) {
    *t = FaldVBlankTracker();
    t->nominalPeriodQpc = (nominalPeriodQpc > 0.0 && std::isfinite(nominalPeriodQpc)) ? nominalPeriodQpc : 0.0;
    t->periodQpc = t->nominalPeriodQpc;
    t->v = firstIndex;
}
static void VBlankPushWake(FaldVBlankTracker* t, long long qpc) {
    t->wakeQpc[t->wakeHead] = qpc; t->wakeV[t->wakeHead] = t->v;
    t->wakeHead = (t->wakeHead + 1) % FALD_VBLANK_WAKE_RING;
    if (t->wakeN < FALD_VBLANK_WAKE_RING) t->wakeN++;
}
// The period in use: the least-squares fit over the phase points when it exists and is sane — within
// FALD_VBLANK_NOMINAL_TOLERANCE of the nominal period (DisplayConfig's exact rational; without one, within
// FALD_VBLANK_FIT_MEDIAN_TOLERANCE of the median) — else the NOMINAL period, the median of the single-step gaps only when
// there is no nominal one. (Under heavy wake jitter the median of 64 gaps is several % off: projecting the wake ring with
// it would scramble the numbering; the nominal rational is within ~1e-3 of any real display.) Only a fitted period is
// published (FaldVBlankTrackerMap): the nominal one's error, extrapolated by the hook, could cost a millisecond.
static void VBlankUpdatePeriod(FaldVBlankTracker* t, double fitPeriodQpc) {
    double p = t->nominalPeriodQpc;
    if (!(p > 0.0) && t->medianPeriodQpc > 0.0) p = t->medianPeriodQpc;
    if (!(fitPeriodQpc > 0.0)) t->periodFit = false;
    else if (t->nominalPeriodQpc > 0.0)
        t->periodFit = std::fabs(fitPeriodQpc / t->nominalPeriodQpc - 1.0) <= FALD_VBLANK_NOMINAL_TOLERANCE;
    else
        t->periodFit = t->medianPeriodQpc > 0.0 &&
                       std::fabs(fitPeriodQpc / t->medianPeriodQpc - 1.0) < FALD_VBLANK_FIT_MEDIAN_TOLERANCE;
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
// The chain's reference line at index v: the least-squares line through the phase points (as VBlankFitPeriod), its
// value relative to t->lineBase (set here: the first phase point's time) in QPC ticks. False without >= 3 points.
static bool VBlankLineAt(FaldVBlankTracker* t, unsigned long long v, double* rel) {
    const unsigned int n = t->phaseN;
    if (n < 3u) return false;
    unsigned long long vMin = t->phaseV[0];
    for (unsigned int i = 1; i < n; i++) vMin = t->phaseV[i] < vMin ? t->phaseV[i] : vMin;
    const long long q0 = t->phaseQpc[0];
    double mx = 0.0, my = 0.0;
    for (unsigned int i = 0; i < n; i++) { mx += (double)(t->phaseV[i] - vMin); my += (double)(t->phaseQpc[i] - q0); }
    mx /= (double)n; my /= (double)n;
    double sxy = 0.0, sxx = 0.0;
    for (unsigned int i = 0; i < n; i++) {
        const double dx = (double)(t->phaseV[i] - vMin) - mx, dy = (double)(t->phaseQpc[i] - q0) - my;
        sxy += dx * dy; sxx += dx * dx;
    }
    if (!(sxx > 0.0)) return false;
    t->lineBase = q0;
    *rel = my + sxy / sxx * ((double)(long long)(v - vMin) - mx);
    return true;
}
static void VBlankPushPhase(FaldVBlankTracker* t, long long a) {
    const unsigned int last = (t->phaseHead + FALD_VBLANK_PHASE_RING - 1u) % FALD_VBLANK_PHASE_RING;
    if (t->phaseN > 0u && t->phaseV[last] == t->v) return;   // one point per index
    t->phaseQpc[t->phaseHead] = a; t->phaseV[t->phaseHead] = t->v;
    t->phaseHead = (t->phaseHead + 1u) % FALD_VBLANK_PHASE_RING;
    if (t->phaseN < FALD_VBLANK_PHASE_RING) t->phaseN++;
    VBlankUpdatePeriod(t, VBlankFitPeriod(t));
}
// Give index newV to the vblank at time a and renumber every ring entry FROM ITS TIME (wakes: their refresh, the latency
// one-sided below 3/4 of a period; phase points: the nearest index). The count then follows time again.
static void VBlankRenumber(FaldVBlankTracker* t, unsigned long long newV, long long a) {
    const double P = t->periodQpc;
    for (unsigned int i = 0; i < t->wakeN; i++)
        t->wakeV[i] = newV + (unsigned long long)(long long)std::floor((double)(t->wakeQpc[i] - a) / P + 0.25);
    for (unsigned int i = 0; i < t->phaseN; i++)
        t->phaseV[i] = newV + (unsigned long long)(long long)std::llround((double)(t->phaseQpc[i] - a) / P);
    t->v = newV;
}
unsigned int FaldVBlankTrackerWake(FaldVBlankTracker* t, long long qpc) {
    t->wakes++;
    if (!t->haveWake) {
        t->haveWake = true; t->lastWakeQpc = qpc;               // (v = Reset's first index)
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
    const unsigned int maxGap = (t->periodFit && t->haveRef) ? FALD_VBLANK_MAX_GAP_FIT : FALD_VBLANK_MAX_GAP;
    if (s > (double)maxGap) {
        // The thread was gone too long to count the refreshes reliably: start the model over, the count continued by the
        // elapsed time (monotonic) in a new epoch — its parity relative to the panel is no longer guaranteed.
        const double el = (double)(qpc - t->lastWakeQpc) / P;
        const unsigned long long next = t->v + (el >= 1.0 ? (unsigned long long)std::llround(el) : 1ull);
        t->restarts++; t->epoch++;
        VBlankRestartModel(t);
        t->v = next; t->haveWake = true; t->lastWakeQpc = qpc;
        VBlankPushWake(t, qpc);
        return 0;
    }
    const unsigned int steps = (unsigned int)s;
    if (model && x - s > 0.5) t->lateWakes++;
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
    if (!t->haveWake || t->wakeN < FALD_VBLANK_MODEL_MIN_WAKES || !(t->periodQpc > 0.0) || !qpc) return false;
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
bool FaldVBlankTrackerMap(FaldVBlankTracker* t, FaldVBlankMap* out) {
    long long a = 0;
    if (t->wakeN < FALD_VBLANK_MIN_WAKES || !FaldVBlankTrackerVBlankQpc(t, t->v, &a)) return false;
    // the continuity chain: where does this vblank sit on the published numbering? The reference is the least-squares
    // line through the chain's phase points (each one accepted onto the chain; noise averaged over up to 64 of them) once
    // there is a fit, else the last published anchor
    if (t->haveRef && t->periodQpc > 0.0) {
        const double P = t->periodQpc;
        double r = ((double)(a - t->refQpc) - (double)(long long)(t->v - t->refV) * P) / P;
        double lineQpc = 0.0;
        if (t->periodFit && VBlankLineAt(t, t->v, &lineQpc)) r = ((double)(a - t->lineBase) - lineQpc) / P;
        const double m = std::floor(r + 0.5);
        if (std::fabs(r - m) > FALD_VBLANK_PHASE_TOLERANCE) {
            // The phase moved off the chain: withheld, no phase point (it would bend the fit). Adopted as a new chain only
            // once the moved phase is itself stable over FALD_VBLANK_REBASE_AFTER attempts.
            if (t->held == 0u) t->phaseJumps++;
            t->held++;
            if (t->haveMoved) {
                const double rm = ((double)(a - t->movedQpc) - (double)(long long)(t->v - t->movedV) * P) / P;
                t->movedStable = (std::fabs(rm - std::floor(rm + 0.5)) <= FALD_VBLANK_PHASE_TOLERANCE) ? t->movedStable + 1u : 0u;
            }
            t->haveMoved = true; t->movedQpc = a; t->movedV = t->v;
            if (t->movedStable + 1u >= FALD_VBLANK_REBASE_AFTER) {
                // re-base: the nearest whole count on the old chain (monotonic), a new epoch, a fresh fit for the new phase
                t->rebases++; t->epoch++;
                VBlankRenumber(t, t->v + (unsigned long long)(long long)m, a);
                t->phaseN = 0; t->phaseHead = 0;
                VBlankUpdatePeriod(t, 0.0);
                t->haveRef = false; t->held = 0; t->haveMoved = false; t->movedStable = 0;
                VBlankClearDwm(t);
            }
            return false;
        }
        t->held = 0; t->haveMoved = false; t->movedStable = 0;
        if (m != 0.0) {                                          // a slip of the local numbering: back onto the chain
            t->slipsRepaired++;
            VBlankRenumber(t, t->v + (unsigned long long)(long long)m, a);
        }
    }
    VBlankPushPhase(t, a);
    FaldVBlankTrackerVBlankQpc(t, t->v, &a);                    // (the anchor under the updated period)
    if (!t->periodFit || !out) return false;
    // Recent-wake guard: the last FALD_VBLANK_RECENT_WAKES wakes must include one within FALD_VBLANK_RECENT_LATE of the model's
    // vblank. All of them later = the vblanks moved (a re-timed display) and the low quantile still sees the older wakes:
    // withheld now, not only once the ring has turned over (the chain check then decides).
    {
        const unsigned int nRecent = t->wakeN < FALD_VBLANK_RECENT_WAKES ? t->wakeN : FALD_VBLANK_RECENT_WAKES;
        double minLate = 1.0e300;
        for (unsigned int k = 0; k < nRecent; k++) {
            const unsigned int i = (t->wakeHead + FALD_VBLANK_WAKE_RING - 1u - k) % FALD_VBLANK_WAKE_RING;
            const double late = ((double)(t->wakeQpc[i] - a) - (double)(long long)(t->wakeV[i] - t->v) * t->periodQpc) / t->periodQpc;
            if (late < minLate) minLate = late;
        }
        if (minLate > FALD_VBLANK_RECENT_LATE) { t->recentLate++; return false; }
    }
    const double P = t->periodQpc;
    if (!(t->nominalPeriodQpc > 0.0) || std::fabs(P / t->nominalPeriodQpc - 1.0) > FALD_VBLANK_NOMINAL_TOLERANCE) return false;
    out->countAnchor = (long long)t->v; out->qpcAnchor = a; out->periodQpc = P;
    t->refQpc = a; t->refV = t->v; t->haveRef = true;
    t->published++;
    return true;
}
void FaldVBlankTrackerDwmSample(FaldVBlankTracker* t, long long nowQpc, long long qpcVBlank, unsigned long long cRefresh,
                                double qpcRefreshPeriod) {
    t->dwmSamples++;
    t->dwmLastCount = cRefresh; t->dwmLastPeriodQpc = qpcRefreshPeriod;
    long long a = 0;
    if (!t->periodFit || qpcVBlank <= 0 || !FaldVBlankTrackerVBlankQpc(t, t->v, &a)) return;
    const double P = t->periodQpc;
    // the phase of DWM's vblank on this output's grid (a whole-period slip of qpcVBlank does not move it), unwrapped
    const double x = (double)(qpcVBlank - a) / P;
    const double frac = x - std::floor(x);
    if (!t->dwmHave) { t->dwmHave = true; t->dwmT0 = nowQpc; t->dwmUnwrapped = frac; }
    else { double d = frac - t->dwmLastFrac; d -= std::floor(d + 0.5); t->dwmUnwrapped += d; }
    t->dwmLastFrac = frac;
    t->dwmX[t->dwmHead] = (double)(nowQpc - t->dwmT0) / P;   // this output's refreshes since the first sample
    t->dwmY[t->dwmHead] = t->dwmUnwrapped;
    t->dwmHead = (t->dwmHead + 1u) % FALD_VBLANK_DWM_RING;
    if (t->dwmN < FALD_VBLANK_DWM_RING) t->dwmN++;
    double xMin = t->dwmX[0], xMax = t->dwmX[0], mx = 0.0, my = 0.0;
    for (unsigned int i = 0; i < t->dwmN; i++) {
        xMin = t->dwmX[i] < xMin ? t->dwmX[i] : xMin; xMax = t->dwmX[i] > xMax ? t->dwmX[i] : xMax;
        mx += t->dwmX[i]; my += t->dwmY[i];
    }
    if (t->dwmN < 3u || xMax - xMin < (double)FALD_VBLANK_DWM_MIN_SPAN) return;
    mx /= (double)t->dwmN; my /= (double)t->dwmN;
    double sxy = 0.0, sxx = 0.0;
    for (unsigned int i = 0; i < t->dwmN; i++) { sxy += (t->dwmX[i] - mx) * (t->dwmY[i] - my); sxx += (t->dwmX[i] - mx) * (t->dwmX[i] - mx); }
    if (!(sxx > 0.0)) return;
    // phase gained per refresh of this output = DWM's period relative to this output's, minus one
    t->dwmPpm = sxy / sxx * 1.0e6;
    const bool rateOff = qpcRefreshPeriod > 0.0 && std::fabs(qpcRefreshPeriod / P - 1.0) > 0.005;   // another rate class
    t->dwmState = (!rateOff && std::fabs(t->dwmPpm) < FALD_VBLANK_SAME_CLOCK_PPM) ? 1 : 0;
}
bool FaldVBlankWaitTooSoon(long long prevReturnQpc, long long returnQpc, double periodQpc) {
    if (prevReturnQpc == 0 || !(periodQpc > 0.0)) return false;
    return (double)(returnQpc - prevReturnQpc) < 0.25 * periodQpc;
}
bool FaldVBlankNeedsRebind(bool factoryCurrent, bool descOk, bool attached, int outLeft, int outTop, int left, int top,
                           float nominalMs, float nominalNowMs) {
    if (!factoryCurrent || !descOk || !attached) return true;
    if (outLeft != left || outTop != top) return true;           // another output sits there now: the posTag would lie
    if (!(nominalNowMs > 1.0f)) return true;
    return !(std::fabs(nominalNowMs - nominalMs) <= 1.0e-4f * nominalMs);
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

void FaldTemporalClockReleased(FaldTemporalState* r) {
    if (!r) return;
    r->clkElapsed = 0; r->clkIndex = 0; r->clkSeeded = false;
    r->stateValid = false;
}

void FaldTemporalIdle(FaldTemporalState* r) {
    if (!r) return;
    r->stateValid = false;
    r->settleLeft = 0;
    r->delayCount = 0;
    r->lastRunQpc = 0;
}
