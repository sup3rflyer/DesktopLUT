// DesktopLUT - shared/fald_core_governor.cpp — see fald_core_governor.h. Compiled into DesktopLUT.exe, DwmHook.dll and
// the tests. No allocation, no exception: it runs in DWM's present.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_core_governor.h"

#include <d3d11.h>

#include <algorithm>

int g_faldCoreHoldOverride = -1;

const char* FaldCoreGovStateName(FaldCoreGovState s) {
    switch (s) {
    case FaldCoreGovState::Off: return "off";
    case FaldCoreGovState::Running: return "running";
    case FaldCoreGovState::Capped: return "capped";
    case FaldCoreGovState::Forced: return "forced";
    }
    return "off";
}

void FaldCoreGovernor::Configure(bool forced) {
    if (configured_ && forced_ == forced) return;
    configured_ = true;
    forced_ = forced;
    if (enabled_) Restart();
}

void FaldCoreGovernor::SetEnabled(bool on) {
    if (on == enabled_) return;
    enabled_ = on;
    Restart();
}

void FaldCoreGovernor::SetMode(uint64_t modeKey) {
    if (haveMode_ && modeKey == modeKey_) return;
    haveMode_ = true;
    modeKey_ = modeKey;
    if (enabled_) Restart();
}

void FaldCoreGovernor::SetRefreshMs(double ms) {
    refreshMs_ = (ms > 0.0 && ms < 1000.0) ? ms : kDefaultRefreshMs;
}

void FaldCoreGovernor::Restart() {
    nSamples_ = headSamples_ = 0;
    nIntervals_ = headIntervals_ = 0;
    haveLastFrame_ = false;
    haveLastFull_ = false;
    hardRun_ = 0;
    spacingMs_ = capP90Us_ = 0.0;
    const FaldCoreGovState s = !enabled_ ? FaldCoreGovState::Off
                             : forced_ ? FaldCoreGovState::Forced
                                       : FaldCoreGovState::Running;
    if (s != state_) { state_ = s; Bump(); }
}

double FaldCoreGovernor::MedianIntervalMs() const {
    if (nIntervals_ == 0) return 0.0;
    double v[kWindow];
    for (unsigned int i = 0; i < nIntervals_; i++) v[i] = intervals_[i];
    std::nth_element(v, v + nIntervals_ / 2, v + nIntervals_);
    return v[nIntervals_ / 2];
}

// rank ceil(0.9 n) (1-based) of the window
double FaldCoreGovernor::P90() const {
    if (nSamples_ == 0) return 0.0;
    double v[kWindow];
    for (unsigned int i = 0; i < nSamples_; i++) v[i] = samples_[i];
    const unsigned int k = (9u * nSamples_ + 9u) / 10u - 1u;
    std::nth_element(v, v + k, v + nSamples_);
    return v[k];
}

// the wall time between full runs that keeps the average GPU share at the budget: p90 every p90 / share
double FaldCoreGovernor::SpacingFor(double p90Us) const {
    double s = (p90Us / 1000.0) / kBudgetShare;
    if (s < refreshMs_) s = refreshMs_;
    if (s > kMaxSpacingMs) s = kMaxSpacingMs;
    return s;
}

void FaldCoreGovernor::Cap(double p90Us) {
    capP90Us_ = p90Us;
    spacingMs_ = SpacingFor(p90Us);
    nSamples_ = headSamples_ = 0;   // the release looks at the capped full runs
    hardRun_ = 0;
    if (state_ != FaldCoreGovState::Capped) { state_ = FaldCoreGovState::Capped; Bump(); }
}

bool FaldCoreGovernor::FullAllowed(double nowMs) const {
    if (state_ == FaldCoreGovState::Running || state_ == FaldCoreGovState::Forced) return true;
    if (state_ != FaldCoreGovState::Capped) return true;   // off: the caller does not run the layer anyway
    if (!haveLastFull_) return true;
    return nowMs - lastFullMs_ >= spacingMs_ - 0.5 * refreshMs_;
}

void FaldCoreGovernor::OnFullRun(double nowMs) {
    lastFullMs_ = nowMs;
    haveLastFull_ = true;
}

void FaldCoreGovernor::OnContentFrame(double nowMs) {
    if (!enabled_) return;
    if (haveLastFrame_) {
        double d = nowMs - lastFrameMs_;
        if (!(d >= 0.0)) d = 0.0;
        if (d > kMaxIntervalMs) d = kMaxIntervalMs;
        intervals_[headIntervals_] = d;
        headIntervals_ = (headIntervals_ + 1u) % kWindow;
        if (nIntervals_ < kWindow) nIntervals_++;
    }
    lastFrameMs_ = nowMs;
    haveLastFrame_ = true;
}

void FaldCoreGovernor::OnSample(double coreUs) {
    if (state_ == FaldCoreGovState::Off) return;
    if (!(coreUs >= 0.0)) return;
    samples_[headSamples_] = coreUs;
    headSamples_ = (headSamples_ + 1u) % kWindow;
    if (nSamples_ < kWindow) nSamples_++;
    if (state_ == FaldCoreGovState::Forced) return;
    const double budget = BudgetUs();
    if (state_ == FaldCoreGovState::Running) {
        hardRun_ = (coreUs > refreshMs_ * 1000.0) ? hardRun_ + 1u : 0u;
        if (hardRun_ >= kHardRun) { Cap(std::max(P90(), coreUs)); return; }
        if (nSamples_ < kMinSamples) return;
        const double p = P90();
        if (p > budget) Cap(p);
        return;
    }
    // capped: the spacing follows the measured full runs; released with headroom
    if (nSamples_ < kMinSamples) return;
    const double p = P90();
    if (p * kReleaseMargin <= budget) {
        nSamples_ = headSamples_ = 0;
        hardRun_ = 0;
        spacingMs_ = capP90Us_ = 0.0;
        state_ = FaldCoreGovState::Running;
        Bump();
        return;
    }
    capP90Us_ = p;
    spacingMs_ = SpacingFor(p);
}

FaldCoreGovernorStatus FaldCoreGovernor::Status() const {
    FaldCoreGovernorStatus s;
    s.state = state_;
    s.refreshMs = refreshMs_;
    s.budgetUs = BudgetUs();
    s.samples = nSamples_;
    s.generation = generation_;
    s.contentMs = MedianIntervalMs();
    if (state_ == FaldCoreGovState::Capped) {
        s.p90Us = nSamples_ >= kMinSamples ? P90() : capP90Us_;
        s.spacingMs = spacingMs_;
    } else {
        s.p90Us = P90();
    }
    return s;
}

// ---------------------------------------------------------------------------------------------------------------------
// The timing ring
// ---------------------------------------------------------------------------------------------------------------------
bool FaldCoreTimingCreate(FaldCoreTiming* t, ID3D11Device* dev) {
    if (!t || !dev) return false;
    FaldCoreTimingRelease(t);
    D3D11_QUERY_DESC qd = {};
    bool all = true;
    for (int i = 0; i < FaldCoreTiming::kSlots; i++) {
        qd.Query = D3D11_QUERY_TIMESTAMP_DISJOINT;
        if (FAILED(dev->CreateQuery(&qd, &t->dj[i]))) { t->dj[i] = nullptr; all = false; continue; }
        qd.Query = D3D11_QUERY_TIMESTAMP;
        for (int k = 0; k < FaldCoreTiming::kStamps; k++)
            if (FAILED(dev->CreateQuery(&qd, &t->ts[i][k]))) { t->ts[i][k] = nullptr; all = false; }
    }
    t->ok = all;
    if (!all) FaldCoreTimingRelease(t);
    return t->ok;
}

void FaldCoreTimingRelease(FaldCoreTiming* t) {
    if (!t) return;
    for (int i = 0; i < FaldCoreTiming::kSlots; i++) {
        if (t->dj[i]) { t->dj[i]->Release(); t->dj[i] = nullptr; }
        for (int k = 0; k < FaldCoreTiming::kStamps; k++)
            if (t->ts[i][k]) { t->ts[i][k]->Release(); t->ts[i][k] = nullptr; }
        t->inFlight[i] = false;
        t->owner[i] = nullptr;
    }
    t->head = 0;
    t->ok = false;
}

unsigned int FaldCoreTimingCollect(FaldCoreTiming* t, ID3D11DeviceContext* ctx, FaldCoreTimingSample* out, unsigned int maxOut) {
    if (!t || !ctx || !t->ok) return 0;
    unsigned int n = 0;
    for (unsigned int s = 0; s < (unsigned int)FaldCoreTiming::kSlots; s++) {   // oldest first
        const int i = (int)((t->head + s) % (unsigned int)FaldCoreTiming::kSlots);
        if (!t->inFlight[i]) continue;
        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT dj = {};
        if (ctx->GetData(t->dj[i], &dj, sizeof(dj), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) continue;
        UINT64 v[FaldCoreTiming::kStamps] = {};
        bool ok = true;
        for (int k = 0; k < FaldCoreTiming::kStamps && ok; k++)
            ok = ctx->GetData(t->ts[i][k], &v[k], sizeof(UINT64), D3D11_ASYNC_GETDATA_DONOTFLUSH) == S_OK;
        if (!ok) continue;
        t->inFlight[i] = false;
        void* owner = t->owner[i];
        t->owner[i] = nullptr;
        if (dj.Disjoint || dj.Frequency == 0) continue;
        bool ordered = true;
        for (int k = 1; k < FaldCoreTiming::kStamps; k++) if (v[k] < v[k - 1]) ordered = false;
        if (!ordered) continue;
        const double toUs = 1e6 / (double)dj.Frequency;
        FaldCoreTimingSample smp;
        smp.coreUs = (double)((v[1] - v[0]) + (v[3] - v[2]) + (v[5] - v[4]) + (v[7] - v[6])) * toUs;
        smp.totalUs = (double)(v[7] - v[0]) * toUs;
        smp.starUs = (double)(v[1] - v[0]) * toUs;
        smp.owner = owner;
        if (n < maxOut && out) out[n] = smp;
        if (n < maxOut) n++;
    }
    return n;
}

int FaldCoreTimingBegin(FaldCoreTiming* t, ID3D11DeviceContext* ctx, void* owner) {
    if (!t || !ctx || !t->ok) return -1;
    const int i = (int)(t->head % (unsigned int)FaldCoreTiming::kSlots);
    if (t->inFlight[i]) return -1;
    t->head++;
    t->owner[i] = owner;
    ctx->Begin(t->dj[i]);
    ctx->End(t->ts[i][0]);
    return i;
}

void FaldCoreTimingStamp(FaldCoreTiming* t, ID3D11DeviceContext* ctx, int slot, int k) {
    if (!t || !ctx || slot < 0 || k <= 0 || k >= FaldCoreTiming::kStamps - 1) return;
    ctx->End(t->ts[slot][k]);
}

void FaldCoreTimingEnd(FaldCoreTiming* t, ID3D11DeviceContext* ctx, int slot) {
    if (!t || !ctx || slot < 0) return;
    ctx->End(t->ts[slot][FaldCoreTiming::kStamps - 1]);
    ctx->End(t->dj[slot]);
    t->inFlight[slot] = true;
}
