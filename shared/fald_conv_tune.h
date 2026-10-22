// DesktopLUT - shared/fald_conv_tune.h
// The convolution pass's variant auto-tune (2026-10-22, the owner's decision after the core-speed work): the conv pass
// (fald_shader.h g_faldConvSource) exists in four byte-identical variants, S = 1 / 2 / 4 / 8 sub-offsets per thread
// (FALD_CONV_SO_VARIANTS). Which is fastest depends on the device and the lattice (an RTX 5090 wants S = 1 on a small
// lattice and S = 2 on a 48 x 48 one; a device with far fewer thread slots wants the loads S = 4 / 8 save). The hosts
// (src/fald.cpp, dwm_hook/hook_fald.cpp) keep every variant's kernel tables resident and, once per full run while
// tuning, time ONE extra dispatch of the variant under test into scratch outputs (timestamp queries read a few runs
// later, no flush, no wait: at most one convolution's cost on top of a present, never a stall); after one warm-up and
// kSamples timed dispatches per variant the fastest median wins, the switch is exact (the gates prove every variant
// equal), and the result is cached per device + driver + geometry (a text file the host names) so later starts begin on
// it. Without timing (no queries) the measured policy of fald_panel.h FaldConvVariant stands. Pure logic here (the
// tuner and the cache); the D3D side is the hosts'. Tested in tests/test_fald_conv_tune.cpp.
#pragma once

#include <stdint.h>
#include <string>

struct ID3D11Device;

static const unsigned int FALD_CONV_TUNE_VARIANTS = 4u;   // = FALD_CONV_SO_VARIANTS (fald_shader.h)
static const unsigned int FALD_CONV_TUNE_WARMUP = 1u;     // the first timed dispatch of a variant is discarded (its first-use cost)
static const unsigned int FALD_CONV_TUNE_SAMPLES = 5u;    // timed dispatches per variant that count

// The device part of a measurement's key: the adapter's description, vendor:device id and the driver's UMD version
// ("NVIDIA GeForce RTX 5090|10de:2b85|32.0.16.1742"). D3D11 / DXGI calls on the device only (safe on DWM's device).
std::string FaldConvTuneDeviceKey(ID3D11Device* dev);
// The whole key: the device part + the lattice, the sub-cell grid and the two kernel tables' sizes.
std::string FaldConvTuneKey(const std::string& deviceKey, unsigned int cols, unsigned int rows, unsigned int sub,
                            unsigned int Wt, unsigned int Ht, unsigned int We, unsigned int He);

struct FaldConvTuneRecord {
    std::string key;
    unsigned int variant = 0;
    double us[FALD_CONV_TUNE_VARIANTS] = {};   // the medians measured (0 = not measured)
};
// The cache file: one record per line, "v1<TAB>key<TAB>variant<TAB>us0<TAB>us1<TAB>us2<TAB>us3". Lookup by key; store
// adds or replaces the key's line (the newest last; at most kMaxLines kept, the oldest dropped). An unreadable or
// unwritable file is simply no cache (false).
static const unsigned int FALD_CONV_TUNE_CACHE_LINES = 64u;
bool FaldConvTuneCacheLookup(const std::string& path, const std::string& key, FaldConvTuneRecord& out);
bool FaldConvTuneCacheStore(const std::string& path, const FaldConvTuneRecord& rec);

class FaldConvTuner {
public:
    enum class State : uint32_t {
        Policy = 0,        // the measured policy (no timing available, or tuning never started)
        Cached = 1,        // a cached measurement of this key
        Tuning = 2,        // timing the variants, one extra dispatch per full run
        Tuned = 3,         // decided this process
    };
    static const char* StateName(State s);   // "policy", "cached", "tuning", "tuned"

    // The variant in use now (the policy's, the cache's, or the tuned one).
    unsigned int Variant() const { return variant_; }
    State GetState() const { return state_; }
    const double* Medians() const { return med_; }   // Tuned: the medians; Cached: the record's; else zeros

    void UsePolicy(unsigned int policyVariant);
    void UseCached(const FaldConvTuneRecord& rec);
    // Tuning starts from the policy's variant (used until decided).
    void Start(unsigned int policyVariant);
    bool Active() const { return state_ == State::Tuning; }
    // The variant to time on this full run (the one with the fewest samples; the lowest index on ties).
    unsigned int NextVariant() const;
    // A finished timed dispatch of `variant` (any order: the queries finish a few runs later).
    void OnSample(unsigned int variant, double us);
    // Every variant has its warm-up + samples: the fastest median wins (-> Tuned); false while samples are missing.
    bool Decide();

private:
    State state_ = State::Policy;
    unsigned int variant_ = 0;
    double samples_[FALD_CONV_TUNE_VARIANTS][FALD_CONV_TUNE_WARMUP + FALD_CONV_TUNE_SAMPLES] = {};
    unsigned int n_[FALD_CONV_TUNE_VARIANTS] = {};
    double med_[FALD_CONV_TUNE_VARIANTS] = {};
};

// Test seams (both hosts; tests only): g_faldConvTuneOverride -1 = tune when not cached and timing exists, 0 = never tune
// (the policy / the cache only), 1 = tune even when cached; g_faldConvTuneCachePath = the cache file the hosts use in
// place of their own (empty = their own; "-" = no cache at all).
extern int g_faldConvTuneOverride;
extern std::string g_faldConvTuneCachePath;
