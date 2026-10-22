// DesktopLUT - shared/fald_fade_governor.cpp — see fald_fade_governor.h. Compiled into DesktopLUT.exe, DwmHook.dll and the
// tests. No allocation, no exception: it runs in DWM's present.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_fade_governor.h"

#include <d3d11.h>
#include <d3d11_3.h>
#include <dxgi.h>

#include <algorithm>
#include <cstdio>

const char* FaldFadeGovStateName(FaldFadeGovState s) {
    switch (s) {
    case FaldFadeGovState::Off: return "off";
    case FaldFadeGovState::Running: return "running";
    case FaldFadeGovState::SuspendedBudget: return "suspended_budget";
    case FaldFadeGovState::SuspendedWeakGpu: return "suspended_weak_gpu";
    case FaldFadeGovState::Forced: return "forced";
    }
    return "off";
}

void FaldFadeGovernor::Configure(bool weakGpu, bool forced) {
    if (configured_ && weak_ == weakGpu && forced_ == forced) return;
    configured_ = true;
    weak_ = weakGpu;
    forced_ = forced;
    if (enabled_) Restart();
}

void FaldFadeGovernor::SetEnabled(bool on) {
    if (on == enabled_) return;
    enabled_ = on;
    Restart();   // on: a re-toggle retries; off: Off
}

void FaldFadeGovernor::SetMode(uint64_t modeKey) {
    if (haveMode_ && modeKey == modeKey_) return;
    haveMode_ = true;
    modeKey_ = modeKey;
    if (enabled_) Restart();
}

void FaldFadeGovernor::SetRefreshMs(double ms) {
    refreshMs_ = (ms > 0.0 && ms < 1000.0) ? ms : kDefaultRefreshMs;
}

void FaldFadeGovernor::Restart() {
    nSamples_ = headSamples_ = 0;
    nIntervals_ = headIntervals_ = 0;
    haveLastFrame_ = false;
    hardRun_ = 0;
    framesSinceSuspend_ = 0;
    suspP90Us_ = suspBudgetUs_ = suspIntervalMs_ = 0.0;
    const FaldFadeGovState s = !enabled_ ? FaldFadeGovState::Off
                             : forced_ ? FaldFadeGovState::Forced
                             : weak_ ? FaldFadeGovState::SuspendedWeakGpu
                                     : FaldFadeGovState::Running;
    if (s != state_) { state_ = s; Bump(); }
}

double FaldFadeGovernor::Budget(double intervalMs) const {
    if (!(intervalMs > 0.0)) return 0.0;
    return std::min(kBudgetShare * intervalMs, refreshMs_) * 1000.0;
}

double FaldFadeGovernor::MedianIntervalMs() const {
    if (nIntervals_ == 0) return 0.0;
    double v[kWindow];
    for (unsigned int i = 0; i < nIntervals_; i++) v[i] = intervals_[i];
    std::nth_element(v, v + nIntervals_ / 2, v + nIntervals_);
    return v[nIntervals_ / 2];
}

double FaldFadeGovernor::BudgetUs() const { return Budget(MedianIntervalMs()); }

// rank ceil(0.9 n) (1-based) of the window
double FaldFadeGovernor::P90() const {
    if (nSamples_ == 0) return 0.0;
    double v[kWindow];
    for (unsigned int i = 0; i < nSamples_; i++) v[i] = samples_[i];
    const unsigned int k = (9u * nSamples_ + 9u) / 10u - 1u;
    std::nth_element(v, v + k, v + nSamples_);
    return v[k];
}

void FaldFadeGovernor::Suspend() {
    suspP90Us_ = P90();
    suspIntervalMs_ = MedianIntervalMs();
    suspBudgetUs_ = Budget(suspIntervalMs_);
    if (!(suspBudgetUs_ > 0.0)) suspBudgetUs_ = refreshMs_ * 1000.0;   // the hard rule before any interval
    nSamples_ = headSamples_ = 0;
    nIntervals_ = headIntervals_ = 0;   // the retry looks at the cadence SINCE the suspension
    hardRun_ = 0;
    framesSinceSuspend_ = 0;
    state_ = FaldFadeGovState::SuspendedBudget;
    Bump();
}

void FaldFadeGovernor::OnContentFrame(double nowMs) {
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
    if (state_ != FaldFadeGovState::SuspendedBudget) return;
    if (framesSinceSuspend_ < 0xFFFFFFFFu) framesSinceSuspend_++;
    if (framesSinceSuspend_ < kRetryHold || nIntervals_ < kMinSamples) return;
    const double med = MedianIntervalMs();
    if (med >= kRetryGrowth * suspIntervalMs_ && Budget(med) >= kRetryMargin * suspP90Us_) {
        // the content cadence changed and leaves room: a retry, measured afresh (the cadence window kept)
        nSamples_ = headSamples_ = 0;
        hardRun_ = 0;
        state_ = FaldFadeGovState::Running;
        Bump();
    }
}

void FaldFadeGovernor::OnSample(double fadeUs) {
    if (state_ != FaldFadeGovState::Running && state_ != FaldFadeGovState::Forced) return;
    if (!(fadeUs >= 0.0)) return;
    samples_[headSamples_] = fadeUs;
    headSamples_ = (headSamples_ + 1u) % kWindow;
    if (nSamples_ < kWindow) nSamples_++;
    if (state_ == FaldFadeGovState::Forced) return;
    hardRun_ = (fadeUs > refreshMs_ * 1000.0) ? hardRun_ + 1u : 0u;
    if (hardRun_ >= kHardRun) { Suspend(); return; }
    if (nSamples_ < kMinSamples) return;
    const double b = BudgetUs();
    if (b > 0.0 && P90() > b) Suspend();
}

FaldFadeGovernorStatus FaldFadeGovernor::Status() const {
    FaldFadeGovernorStatus s;
    s.state = state_;
    s.refreshMs = refreshMs_;
    s.samples = nSamples_;
    s.generation = generation_;
    if (state_ == FaldFadeGovState::SuspendedBudget) {
        s.p90Us = suspP90Us_; s.budgetUs = suspBudgetUs_; s.contentMs = suspIntervalMs_;
    } else {
        s.p90Us = P90(); s.contentMs = MedianIntervalMs(); s.budgetUs = Budget(s.contentMs);
    }
    return s;
}

bool FaldFadeWeakAdapter(ID3D11Device* dev, std::string* desc) {
    if (!dev) return false;
    bool uma = false;
    D3D11_FEATURE_DATA_D3D11_OPTIONS2 o2 = {};
    if (SUCCEEDED(dev->CheckFeatureSupport(D3D11_FEATURE_D3D11_OPTIONS2, &o2, sizeof(o2)))) uma = o2.UnifiedMemoryArchitecture != FALSE;
    unsigned long long vram = 0;
    bool known = false;
    char name[128] = {};
    IDXGIDevice* dx = nullptr;
    if (SUCCEEDED(dev->QueryInterface(__uuidof(IDXGIDevice), (void**)&dx)) && dx) {
        IDXGIAdapter* ad = nullptr;
        if (SUCCEEDED(dx->GetAdapter(&ad)) && ad) {
            DXGI_ADAPTER_DESC d = {};
            if (SUCCEEDED(ad->GetDesc(&d))) {
                vram = (unsigned long long)d.DedicatedVideoMemory;
                known = true;
                WideCharToMultiByte(CP_UTF8, 0, d.Description, -1, name, (int)sizeof(name) - 1, nullptr, nullptr);
            }
            ad->Release();
        }
        dx->Release();
    }
    const bool weak = uma || (known && vram < (1ull << 30));
    if (desc) {
        char line[256];
        snprintf(line, sizeof(line), "%s, %llu MB dedicated VRAM%s%s", name[0] ? name : "unknown adapter", vram >> 20,
                 uma ? ", unified memory" : "", weak ? " (weak for the flank fade)" : "");
        *desc = line;
    }
    return weak;
}
