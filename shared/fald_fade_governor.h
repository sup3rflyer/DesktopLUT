// DesktopLUT - shared/fald_fade_governor.h
// The flank fade's safety governor: the fade (shared/fald_fade.h) costs GPU time once per CONTENT frame (~2-14 ms at 4K
// on an RTX 5090, seconds on an iGPU); inside the DWM hook an overrun is a frozen desktop. The governor measures what the
// fade's passes take (FaldFadeCollectTiming: timestamp queries read without a flush or a wait) and suspends the fade when
// it does not fit: the layer then runs as C, today's layer without the fade, bit for bit. The user's setting stays on.
//
// The rule (2026-10-22, the owner):
//   * budget = 15 % of the content-frame interval (the median over the last kWindow content frames), never more than the
//     refresh interval;
//   * the p90 of the fade's GPU time over the last kWindow content frames (at least kMinSamples) above the budget ->
//     suspended (SuspendedBudget); kHardRun content frames in a row each longer than a whole refresh interval suspend at
//     once (an overrun that drops frames on every content frame: a weak GPU at 4K);
//   * a suspension is retried only after a mode change (SetMode), the user turning the fade on again (SetEnabled), or a
//     content-interval change that makes room: at least kRetryHold content frames later, the median interval since the
//     suspension >= kRetryGrowth x the one at the suspension AND its budget >= kRetryMargin x the p90 that suspended it.
//     A retry that suspends again needs the next such change: no oscillation within one content cadence;
//   * an iGPU / UMA adapter, or one with less than 1 GB of dedicated VRAM (FaldFadeWeakAdapter), starts suspended
//     (SuspendedWeakGpu) and stays so;
//   * forced (INI [General] FaldFlankFadeForce=1, REFERENCE.md): never suspended, the numbers still measured.
// Pure logic (no D3D, no clock of its own: the callers pass times), ONE implementation for the overlay (src/fald.cpp) and
// the hook (dwm_hook/hook_fald.cpp); unit-tested with injected timings (tests/test_fald_fade_governor.cpp).
#pragma once

#include <stdint.h>
#include <string>

struct ID3D11Device;

// Wire values (dwm_hook_config.h DwmHookFaldFadeStatusEntry::state, IPC state.get fald_flank_fade_governor.state)
enum class FaldFadeGovState : uint32_t {
    Off = 0,               // the setting is off
    Running = 1,           // the fade runs (measured)
    SuspendedBudget = 2,   // over budget: the layer runs without the fade
    SuspendedWeakGpu = 3,  // an iGPU / UMA / < 1 GB VRAM adapter: never started
    Forced = 4,            // the INI force key: runs whatever it costs (measured)
};
const char* FaldFadeGovStateName(FaldFadeGovState s);   // "off", "running", "suspended_budget", "suspended_weak_gpu", "forced"

struct FaldFadeGovernorStatus {
    FaldFadeGovState state = FaldFadeGovState::Off;
    double p90Us = 0.0;       // running: the p90 of the window; suspended (budget): the p90 that suspended it
    double budgetUs = 0.0;    // the budget that p90 is held against
    double contentMs = 0.0;   // the content-frame interval the budget comes from (median)
    double refreshMs = 0.0;   // the refresh interval cap
    unsigned int samples = 0; // GPU samples in the window
    uint32_t generation = 0;  // bumps on every state change
};

class FaldFadeGovernor {
public:
    static constexpr double kBudgetShare = 0.15;
    static constexpr unsigned int kWindow = 120;
    static constexpr unsigned int kMinSamples = 30;
    static constexpr unsigned int kHardRun = 3;
    static constexpr double kRetryMargin = 1.25;
    static constexpr double kRetryGrowth = 1.25;
    static constexpr unsigned int kRetryHold = 120;
    static constexpr double kMaxIntervalMs = 1000.0;     // a longer gap counts as this (a static desktop)
    static constexpr double kDefaultRefreshMs = 1000.0 / 60.0;   // an unknown refresh period

    // The adapter (weak: FaldFadeWeakAdapter) and the force key. A change re-evaluates the state.
    void Configure(bool weakGpu, bool forced);
    // The user's setting; off -> on is a re-toggle (a retry).
    void SetEnabled(bool on);
    // The mode the fade runs in (hdr, size, refresh — any key): a change is a retry.
    void SetMode(uint64_t modeKey);
    // The refresh interval cap (ms; <= 0 = unknown -> kDefaultRefreshMs).
    void SetRefreshMs(double ms);
    // The fade may run now (Running or Forced).
    bool Allowed() const { return state_ == FaldFadeGovState::Running || state_ == FaldFadeGovState::Forced; }
    // A content frame at `nowMs` (the caller's clock), while running or suspended: the content-interval clock.
    void OnContentFrame(double nowMs);
    // One content frame's measured GPU time of the fade's passes (only while running / forced).
    void OnSample(double fadeUs);
    FaldFadeGovernorStatus Status() const;
    FaldFadeGovState State() const { return state_; }
    double BudgetUs() const;          // the budget at the current content interval (0 = no interval known yet)
    double MedianIntervalMs() const;  // over the window (0 = none)

private:
    void Restart();                   // samples and the interval window cleared; Running unless weak / forced / off
    void Suspend();
    void Bump() { generation_++; }
    double P90() const;
    double Budget(double intervalMs) const;

    FaldFadeGovState state_ = FaldFadeGovState::Off;
    bool enabled_ = false, weak_ = false, forced_ = false, configured_ = false;
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
    // the suspension being held
    double suspP90Us_ = 0.0, suspBudgetUs_ = 0.0, suspIntervalMs_ = 0.0;
    unsigned int framesSinceSuspend_ = 0;
    uint32_t generation_ = 0;
};

// The adapter the device runs on is weak for the fade: integrated / UMA (D3D11_FEATURE_D3D11_OPTIONS2
// UnifiedMemoryArchitecture) or less than 1 GB of dedicated VRAM. `desc` gets a one-line description (log). D3D11 + DXGI
// calls only (no pointer reads into anything not ours): safe on DWM's device, once at init.
bool FaldFadeWeakAdapter(ID3D11Device* dev, std::string* desc);
