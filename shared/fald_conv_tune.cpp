// DesktopLUT - shared/fald_conv_tune.cpp — see fald_conv_tune.h. Compiled into DesktopLUT.exe, DwmHook.dll and the tests.
// The cache file is touched at a build and at a decision only, never per present.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "fald_conv_tune.h"

#include <d3d11.h>
#include <dxgi.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <vector>

int g_faldConvTuneOverride = -1;
std::string g_faldConvTuneCachePath;

const char* FaldConvTuner::StateName(State s) {
    switch (s) {
    case State::Policy: return "policy";
    case State::Cached: return "cached";
    case State::Tuning: return "tuning";
    case State::Tuned: return "tuned";
    }
    return "policy";
}

std::string FaldConvTuneDeviceKey(ID3D11Device* dev) {
    if (!dev) return "no device";
    char name[160] = {};
    unsigned int vendor = 0, device = 0;
    unsigned long long umd = 0;
    IDXGIDevice* dx = nullptr;
    if (SUCCEEDED(dev->QueryInterface(__uuidof(IDXGIDevice), (void**)&dx)) && dx) {
        IDXGIAdapter* ad = nullptr;
        if (SUCCEEDED(dx->GetAdapter(&ad)) && ad) {
            DXGI_ADAPTER_DESC d = {};
            if (SUCCEEDED(ad->GetDesc(&d))) {
                WideCharToMultiByte(CP_UTF8, 0, d.Description, -1, name, (int)sizeof(name) - 1, nullptr, nullptr);
                vendor = d.VendorId; device = d.DeviceId;
            }
            LARGE_INTEGER v = {};
            if (SUCCEEDED(ad->CheckInterfaceSupport(__uuidof(IDXGIDevice), &v))) umd = (unsigned long long)v.QuadPart;
            ad->Release();
        }
        dx->Release();
    }
    for (char* c = name; *c; c++) if (*c == '|' || *c == '\t' || *c == '\n' || *c == '\r') *c = ' ';
    char buf[320];
    snprintf(buf, sizeof(buf), "%s|%04x:%04x|%u.%u.%u.%u", name[0] ? name : "unknown adapter", vendor, device,
             (unsigned int)(umd >> 48), (unsigned int)((umd >> 32) & 0xFFFF), (unsigned int)((umd >> 16) & 0xFFFF), (unsigned int)(umd & 0xFFFF));
    return buf;
}

std::string FaldConvTuneKey(const std::string& deviceKey, unsigned int cols, unsigned int rows, unsigned int sub,
                            unsigned int Wt, unsigned int Ht, unsigned int We, unsigned int He) {
    char buf[96];
    snprintf(buf, sizeof(buf), "|%ux%u|sub%u|kt%ux%u|ke%ux%u", cols, rows, sub, Wt, Ht, We, He);
    return deviceKey + buf;
}

namespace {

bool ParseLine(const std::string& line, FaldConvTuneRecord& r) {
    std::vector<std::string> f;
    std::string cur;
    for (char c : line) { if (c == '\t') { f.push_back(cur); cur.clear(); } else if (c != '\r' && c != '\n') cur.push_back(c); }
    f.push_back(cur);
    if (f.size() != 3u + FALD_CONV_TUNE_VARIANTS || f[0] != "v1" || f[1].empty()) return false;
    char* end = nullptr;
    const unsigned long v = strtoul(f[2].c_str(), &end, 10);
    if (!end || *end || v >= FALD_CONV_TUNE_VARIANTS) return false;
    r.key = f[1];
    r.variant = (unsigned int)v;
    for (unsigned int i = 0; i < FALD_CONV_TUNE_VARIANTS; i++) {
        const double u = strtod(f[3 + i].c_str(), &end);
        if (!end || *end || !(u >= 0.0)) return false;
        r.us[i] = u;
    }
    return true;
}

std::string FormatLine(const FaldConvTuneRecord& r) {
    std::string s = "v1\t" + r.key + "\t" + std::to_string(r.variant);
    for (unsigned int i = 0; i < FALD_CONV_TUNE_VARIANTS; i++) {
        char b[32]; snprintf(b, sizeof(b), "\t%.1f", r.us[i]); s += b;
    }
    return s;
}

std::vector<FaldConvTuneRecord> ReadAll(const std::string& path) {
    std::vector<FaldConvTuneRecord> out;
    std::ifstream f(path);
    if (!f.good()) return out;
    std::string line;
    while (std::getline(f, line)) {
        FaldConvTuneRecord r;
        if (ParseLine(line, r)) out.push_back(r);
    }
    return out;
}

}  // namespace

bool FaldConvTuneCacheLookup(const std::string& path, const std::string& key, FaldConvTuneRecord& out) {
    if (path.empty() || path == "-") return false;
    for (const FaldConvTuneRecord& r : ReadAll(path))
        if (r.key == key) { out = r; return true; }
    return false;
}

bool FaldConvTuneCacheStore(const std::string& path, const FaldConvTuneRecord& rec) {
    if (path.empty() || path == "-" || rec.key.empty() || rec.variant >= FALD_CONV_TUNE_VARIANTS) return false;
    std::vector<FaldConvTuneRecord> all = ReadAll(path);
    all.erase(std::remove_if(all.begin(), all.end(), [&](const FaldConvTuneRecord& r) { return r.key == rec.key; }), all.end());
    all.push_back(rec);
    while (all.size() > FALD_CONV_TUNE_CACHE_LINES) all.erase(all.begin());
    std::ofstream f(path, std::ios::trunc);
    if (!f.good()) return false;
    for (const FaldConvTuneRecord& r : all) f << FormatLine(r) << "\n";
    return f.good();
}

void FaldConvTuner::UsePolicy(unsigned int policyVariant) {
    state_ = State::Policy;
    variant_ = policyVariant < FALD_CONV_TUNE_VARIANTS ? policyVariant : 0u;
    for (unsigned int i = 0; i < FALD_CONV_TUNE_VARIANTS; i++) { n_[i] = 0; med_[i] = 0.0; }
}

void FaldConvTuner::UseCached(const FaldConvTuneRecord& rec) {
    UsePolicy(rec.variant);
    state_ = State::Cached;
    for (unsigned int i = 0; i < FALD_CONV_TUNE_VARIANTS; i++) med_[i] = rec.us[i];
}

void FaldConvTuner::Start(unsigned int policyVariant) {
    UsePolicy(policyVariant);
    state_ = State::Tuning;
}

unsigned int FaldConvTuner::NextVariant() const {
    unsigned int best = 0;
    for (unsigned int i = 1; i < FALD_CONV_TUNE_VARIANTS; i++) if (n_[i] < n_[best]) best = i;
    return best;
}

void FaldConvTuner::OnSample(unsigned int variant, double us) {
    if (state_ != State::Tuning || variant >= FALD_CONV_TUNE_VARIANTS || !(us >= 0.0)) return;
    const unsigned int cap = FALD_CONV_TUNE_WARMUP + FALD_CONV_TUNE_SAMPLES;
    if (n_[variant] >= cap) return;
    samples_[variant][n_[variant]++] = us;
}

bool FaldConvTuner::Decide() {
    if (state_ != State::Tuning) return state_ == State::Tuned;
    const unsigned int cap = FALD_CONV_TUNE_WARMUP + FALD_CONV_TUNE_SAMPLES;
    for (unsigned int i = 0; i < FALD_CONV_TUNE_VARIANTS; i++) if (n_[i] < cap) return false;
    unsigned int best = 0;
    for (unsigned int i = 0; i < FALD_CONV_TUNE_VARIANTS; i++) {
        double v[FALD_CONV_TUNE_SAMPLES];
        for (unsigned int k = 0; k < FALD_CONV_TUNE_SAMPLES; k++) v[k] = samples_[i][FALD_CONV_TUNE_WARMUP + k];
        std::nth_element(v, v + FALD_CONV_TUNE_SAMPLES / 2, v + FALD_CONV_TUNE_SAMPLES);
        med_[i] = v[FALD_CONV_TUNE_SAMPLES / 2];
        if (med_[i] < med_[best]) best = i;
    }
    variant_ = best;
    state_ = State::Tuned;
    return true;
}
