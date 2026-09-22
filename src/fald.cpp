// FALD context-dependence correction — resources, passes, debug dump. See fald.h / shared/fald_shader.h.
#include "fald.h"
#include "../shared/fald_shader.h"
#include "types.h"
#include "globals.h"
#include <d3dcompiler.h>
#include <fstream>
#include <iostream>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <mutex>

static ID3D11ComputeShader* g_faldStatCS = nullptr;
static ID3D11ComputeShader* g_faldConvCS = nullptr;
static ID3D11ComputeShader* g_faldGainCS = nullptr;
static ID3D11ComputeShader* g_faldBlurCS = nullptr;
static ID3D11ComputeShader* g_faldTemporalCS = nullptr;   // pass 1b: per-cell drive state (temporal modes 1 / 2 only)
static ID3D11ComputeShader* g_faldPanelClockCS = nullptr; // pass 1c: the two parity clocks' LED states (temporal mode 3 only)
static ID3D11ComputeShader* g_faldBoostCS = nullptr;      // pass 1a: non-black zone count -> LED boost (boost LUT only)
static ID3D11ComputeShader* g_faldStarStatCS = nullptr;   // starfield balancing S0: star statistic of the source frame
static ID3D11ComputeShader* g_faldStarWeightCS = nullptr; // S1: tapered protection field + zone weights
static ID3D11ComputeShader* g_faldStarPlanCS = nullptr;   // S2: target + the plan the pixels sample
static ID3D11ComputeShader* g_faldGlowZoneCS = nullptr;   // glow fill G0: zone pedestal of this round's B_true
static ID3D11ComputeShader* g_faldGlowDilateCS = nullptr; // G1: box maximum on the extended lattice
static ID3D11ComputeShader* g_faldGlowErodeCS = nullptr;  // G2: box minimum = the closing
static ID3D11ComputeShader* g_faldGlowEnvCS = nullptr;    // G3: blur under the closing + the zone deficit
static ID3D11ComputeShader* g_faldGlowBandCS = nullptr;   // G4: the count-threshold band's per-zone record (mean-rule files)
static ID3D11ComputeShader* g_faldGlowGuardCS = nullptr;  // G5: the band's neighbour guard -> the final zone scale k (C16)
// The zone sweeps' combine variants (work guide C14; fald_shader.h above ZoneSlices): run only for a lattice whose zones
// hold more than FALD_ZONE_SLICE_PX pixels, after the sliced pass, to fold its partials and finish each zone. Optional:
// a compile failure costs only the lattices that need them (Build / EnsureStar / EnsureGlow refuse those).
static ID3D11ComputeShader* g_faldStatCombineCS = nullptr;
static ID3D11ComputeShader* g_faldStarStatCombineCS = nullptr;
static ID3D11ComputeShader* g_faldGlowBandCombineCS = nullptr;
static ID3D11PixelShader* g_faldPS = nullptr;
static ID3D11SamplerState* g_faldSampler = nullptr;

static const unsigned int FALD_FILE_POLL_FRAMES = 120;   // ~2 s at 60 Hz between params-file stamp checks
// (FALD_RESUME_GAP_MS and the temporal helpers — FaldTemporalAlpha, FaldSettleFrames, the panel clock, the settle
// accounting and the per-run orchestration — live in shared/fald_temporal.cpp, shared with the DWM hook.)

// Starfield balancing settings: every field into its documented range (DLC StarfieldParams; the mock's validation
// uses the same limits). NaN -> the default. The smoothstep pairs stay ordered (hi >= lo).
void FaldStarfieldClamp(FaldStarfieldSettings& s) {
    auto clampF = [](float v, float lo, float hi, float dflt) { return (v != v) ? dflt : (v < lo ? lo : (v > hi ? hi : v)); };
    s.even = clampF(s.even, 0.0f, 1.0f, 0.8f);
    s.lift = clampF(s.lift, 0.0f, 1.0f, 0.0f);
    s.targetGain = clampF(s.targetGain, 0.05f, 2.0f, 1.0f);
    s.targetSigma = clampF(s.targetSigma, 0.0f, 4.0f, 0.0f);
    s.keepNits = clampF(s.keepNits, 0.0f, 10000.0f, 100.0f);
    if (s.evenReach > FALD_STAR_EVEN_REACH_MAX) s.evenReach = FALD_STAR_EVEN_REACH_MAX;
    s.capNits = clampF(s.capNits, 0.0f, 10000.0f, 0.0f);
    s.strength = clampF(s.strength, 0.0f, 1.0f, 1.0f);
    s.areaLo = clampF(s.areaLo, 0.0f, 1.0e6f, 40.0f);
    s.areaHi = clampF(s.areaHi, 0.0f, 1.0e6f, 160.0f);
    if (s.areaHi < s.areaLo) s.areaHi = s.areaLo;
    s.peakHi = clampF(s.peakHi, 0.0f, 10000.0f, 0.0f);
    if (s.reach > FALD_STAR_REACH_MAX) s.reach = FALD_STAR_REACH_MAX;
    s.nbLo = clampF(s.nbLo, 0.0f, 1.0f, 0.15f);
    s.nbHi = clampF(s.nbHi, 0.0f, 1.0f, 0.30f);
    if (s.nbHi < s.nbLo) s.nbHi = s.nbLo;
}

// Glow fill settings: every field into its documented range (DLC GlowFillParams / glowfill.clamp_params; the mock's
// validation uses the same limits). NaN -> the default.
void FaldGlowClamp(FaldGlowSettings& s) {
    auto clampF = [](float v, float lo, float hi, float dflt) { return (v != v) ? dflt : (v < lo ? lo : (v > hi ? hi : v)); };
    s.strength = clampF(s.strength, 0.0f, 1.0f, 1.0f);
    if (s.reach < FALD_GLOW_REACH_MIN) s.reach = FALD_GLOW_REACH_MIN;
    if (s.reach > FALD_GLOW_REACH_MAX) s.reach = FALD_GLOW_REACH_MAX;
    s.capNits = clampF(s.capNits, FALD_GLOW_CAP_MIN, FALD_GLOW_CAP_MAX, 0.05f);
}

// DLC glowfill.req_ceiling: the fill never lights a LED (drive floor) and never makes a zone LIT for the boost count.
const char* const FALD_GLOW_SDR_NOTE = "glow fill is HDR only: the levels behind its request ceiling (drive floor, LIT level, count threshold) are HDR measurements";

const char* const FALD_GLOW_NEEDS_STAR_NOTE = "glow fill is part of the starfield feature: the switch is stored, but the fill runs only while starfield balancing is on";

bool FaldGlowSupported(const FaldPanelParams& p) { return p.transfer == FALD_TRANSFER_PQ; }

bool FaldGlowBandActive(const FaldPanelParams& p) { return p.hasBoost && p.boostRule == FALD_BOOST_RULE_MEAN; }

static void ComputeFlatResponse(FaldResources* r);   // defined with the passes below

void FaldTrace(const char* msg) {
    static std::mutex m;
    static std::wstring path;
    std::lock_guard<std::mutex> lk(m);
    if (path.empty()) {
        wchar_t exe[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, exe, MAX_PATH);
        std::wstring s(exe); size_t k = s.find_last_of(L"\\/");
        path = (k == std::wstring::npos ? L"" : s.substr(0, k + 1)) + L"fald_trace.log";
    }
    std::ofstream f(path, std::ios::app);
    f << GetTickCount64() << " [" << GetCurrentThreadId() << "] " << msg << "\n";
}

// ---------------------------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------------------------
static bool CompileOne(const std::string& src, const char* name, const char* target, ID3DBlob** blob) {
    ID3DBlob* err = nullptr;
    HRESULT hr = D3DCompile(src.c_str(), src.size(), name, nullptr, nullptr, "main", target, 0, 0, blob, &err);
    if (FAILED(hr)) {
        std::cerr << "[FALD] " << name << " compile error: " << (err ? (const char*)err->GetBufferPointer() : "?") << std::endl;
        if (err) err->Release();
        return false;
    }
    if (err) err->Release();
    return true;
}

bool InitFaldShaders() {
    if (!g_device) return false;
    ID3DBlob* b = nullptr;
    std::string common = g_faldCommonSource;
    if (!CompileOne(common + g_faldStatSource, "FaldStatCS", "cs_5_0", &b)) return false;
    HRESULT hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStatCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(stat) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldConvSource, "FaldConvCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldConvCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(conv) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldGainSource, "FaldGainCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldGainCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(gain) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldBlurSource, "FaldBlurCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldBlurCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(blur) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldTemporalSource, "FaldTemporalCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldTemporalCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(temporal) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldPanelClockSource, "FaldPanelClockCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldPanelClockCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(panel clock) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldBoostSource, "FaldBoostCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldBoostCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(boost) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldStarStatSource, "FaldStarStatCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStarStatCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(star stat) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldStarWeightSource, "FaldStarWeightCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStarWeightCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(star weight) failed" << std::endl; return false; }
    if (!CompileOne(common + g_faldStarPlanSource, "FaldStarPlanCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStarPlanCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(star plan) failed" << std::endl; return false; }
    {
        struct { const char* src; const char* name; ID3D11ComputeShader** cs; } glow[6] = {
            { g_faldGlowZoneSource, "FaldGlowZoneCS", &g_faldGlowZoneCS }, { g_faldGlowDilateSource, "FaldGlowDilateCS", &g_faldGlowDilateCS },
            { g_faldGlowErodeSource, "FaldGlowErodeCS", &g_faldGlowErodeCS }, { g_faldGlowEnvSource, "FaldGlowEnvCS", &g_faldGlowEnvCS },
            { g_faldGlowBandSource, "FaldGlowBandCS", &g_faldGlowBandCS }, { g_faldGlowGuardSource, "FaldGlowGuardCS", &g_faldGlowGuardCS } };
        for (auto& g : glow) {
            if (!CompileOne(common + g.src, g.name, "cs_5_0", &b)) return false;
            hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, g.cs);
            b->Release(); b = nullptr;
            if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(" << g.name << ") failed" << std::endl; return false; }
        }
    }
    {
        const std::string combine = std::string(g_faldZoneCombineDefine) + common;   // the same sources, FALD_ZONE_COMBINE
        struct { const char* src; const char* name; ID3D11ComputeShader** cs; } zc[3] = {
            { g_faldStatSource, "FaldStatCombineCS", &g_faldStatCombineCS },
            { g_faldStarStatSource, "FaldStarStatCombineCS", &g_faldStarStatCombineCS },
            { g_faldGlowBandSource, "FaldGlowBandCombineCS", &g_faldGlowBandCombineCS } };
        for (auto& z : zc) {
            if (!CompileOne(combine + z.src, z.name, "cs_5_0", &b)) continue;   // logged; lattices of one-slice zones don't need it
            hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, z.cs);
            b->Release(); b = nullptr;
            if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(" << z.name << ") failed" << std::endl; *z.cs = nullptr; }
        }
    }
    if (!CompileOne(common + g_faldPixelSource, "FaldPS", "ps_5_0", &b)) return false;
    hr = g_device->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldPS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreatePixelShader failed" << std::endl; return false; }
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    if (FAILED(g_device->CreateSamplerState(&sd, &g_faldSampler))) { std::cerr << "[FALD] sampler failed" << std::endl; return false; }
    std::cout << "FALD correction shaders: compiled" << std::endl;
    return true;
}

void ReleaseFaldShaders() {
    if (g_faldSampler) { g_faldSampler->Release(); g_faldSampler = nullptr; }
    if (g_faldPS) { g_faldPS->Release(); g_faldPS = nullptr; }
    if (g_faldGlowBandCombineCS) { g_faldGlowBandCombineCS->Release(); g_faldGlowBandCombineCS = nullptr; }
    if (g_faldStarStatCombineCS) { g_faldStarStatCombineCS->Release(); g_faldStarStatCombineCS = nullptr; }
    if (g_faldStatCombineCS) { g_faldStatCombineCS->Release(); g_faldStatCombineCS = nullptr; }
    if (g_faldGlowGuardCS) { g_faldGlowGuardCS->Release(); g_faldGlowGuardCS = nullptr; }
    if (g_faldGlowBandCS) { g_faldGlowBandCS->Release(); g_faldGlowBandCS = nullptr; }
    if (g_faldGlowEnvCS) { g_faldGlowEnvCS->Release(); g_faldGlowEnvCS = nullptr; }
    if (g_faldGlowErodeCS) { g_faldGlowErodeCS->Release(); g_faldGlowErodeCS = nullptr; }
    if (g_faldGlowDilateCS) { g_faldGlowDilateCS->Release(); g_faldGlowDilateCS = nullptr; }
    if (g_faldGlowZoneCS) { g_faldGlowZoneCS->Release(); g_faldGlowZoneCS = nullptr; }
    if (g_faldStarPlanCS) { g_faldStarPlanCS->Release(); g_faldStarPlanCS = nullptr; }
    if (g_faldStarWeightCS) { g_faldStarWeightCS->Release(); g_faldStarWeightCS = nullptr; }
    if (g_faldStarStatCS) { g_faldStarStatCS->Release(); g_faldStarStatCS = nullptr; }
    if (g_faldBoostCS) { g_faldBoostCS->Release(); g_faldBoostCS = nullptr; }
    if (g_faldPanelClockCS) { g_faldPanelClockCS->Release(); g_faldPanelClockCS = nullptr; }
    if (g_faldTemporalCS) { g_faldTemporalCS->Release(); g_faldTemporalCS = nullptr; }
    if (g_faldBlurCS) { g_faldBlurCS->Release(); g_faldBlurCS = nullptr; }
    if (g_faldGainCS) { g_faldGainCS->Release(); g_faldGainCS = nullptr; }
    if (g_faldConvCS) { g_faldConvCS->Release(); g_faldConvCS = nullptr; }
    if (g_faldStatCS) { g_faldStatCS->Release(); g_faldStatCS = nullptr; }
}

bool FaldShadersReady() {
    return g_faldStatCS && g_faldConvCS && g_faldGainCS && g_faldBlurCS && g_faldTemporalCS && g_faldPanelClockCS && g_faldBoostCS &&
           g_faldStarStatCS && g_faldStarWeightCS && g_faldStarPlanCS &&
           g_faldGlowZoneCS && g_faldGlowDilateCS && g_faldGlowErodeCS && g_faldGlowEnvCS && g_faldGlowBandCS && g_faldGlowGuardCS &&
           g_faldPS && g_faldSampler;
}

// ---------------------------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------------------------
template <typename T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

// Starfield balancing textures: they exist only while the option is on (EnsureStar / FaldRunPasses).
static void ReleaseStar(FaldResources* r) {
    SafeRelease(r->starStatSRV); SafeRelease(r->starStatUAV); SafeRelease(r->starStatTex);
    SafeRelease(r->starWSRV); SafeRelease(r->starWUAV); SafeRelease(r->starWTex);
    SafeRelease(r->starPlanSRV); SafeRelease(r->starPlanUAV); SafeRelease(r->starPlanTex);
    SafeRelease(r->starBgSRV); SafeRelease(r->starBgUAV); SafeRelease(r->starBgTex);
    SafeRelease(r->starPlan2SRV); SafeRelease(r->starPlan2UAV); SafeRelease(r->starPlan2Tex);
    r->starOn = false;
    r->starRetryCounter = 0;                 // option off / resources rebuilt: the next enable tries at once
}

// Glow fill textures: they exist only while the option is on (EnsureGlow / FaldRunPasses).
static void ReleaseGlow(FaldResources* r) {
    SafeRelease(r->glowVSRV); SafeRelease(r->glowVUAV); SafeRelease(r->glowVTex);
    SafeRelease(r->glowDilSRV); SafeRelease(r->glowDilUAV); SafeRelease(r->glowDilTex);
    SafeRelease(r->glowCSRV); SafeRelease(r->glowCUAV); SafeRelease(r->glowCTex);
    SafeRelease(r->glowEnvSRV); SafeRelease(r->glowEnvUAV); SafeRelease(r->glowEnvTex);
    SafeRelease(r->glowKSRV); SafeRelease(r->glowKUAV); SafeRelease(r->glowKTex);
    SafeRelease(r->glowBandSRV); SafeRelease(r->glowBandUAV); SafeRelease(r->glowBandTex);
    SafeRelease(r->glowASRV); SafeRelease(r->glowAUAV); SafeRelease(r->glowATex);
    SafeRelease(r->glowKTmpSRV); SafeRelease(r->glowKTmpUAV); SafeRelease(r->glowKTmpTex);
    SafeRelease(r->glowBandPartUAV); SafeRelease(r->glowBandPartBuf);
    r->glowOn = false;
    r->glowBand = false;
    r->glowRetryCounter = 0;                 // option off / resources rebuilt: the next enable tries at once
}

// Panel clock textures (temporal mode 3): they exist only while the mode is on (EnsureClock / FaldRunPasses).
static void ReleaseClock(FaldResources* r) {
    for (unsigned int i = 0; i < 2; i++) { SafeRelease(r->clkStateSRV[i]); SafeRelease(r->clkStateUAV[i]); SafeRelease(r->clkStateTex[i]); }
    SafeRelease(r->clkPrevSRV); SafeRelease(r->clkPrevUAV); SafeRelease(r->clkPrevTex);
    SafeRelease(r->clkEstSRV); SafeRelease(r->clkEstUAV); SafeRelease(r->clkEstTex);
    r->clkRetryCounter = 0;                  // mode off / resources rebuilt: the next enable tries at once
    r->clkElapsed = 0; r->clkIndex = 0; r->clkSeeded = false;
}

static void ReleaseAll(FaldResources* r) {
    SafeRelease(r->interSRV); SafeRelease(r->interRTV); SafeRelease(r->inter);
    SafeRelease(r->curveSRV); SafeRelease(r->curveTex);
    SafeRelease(r->kTrueSRV); SafeRelease(r->kTrueBuf);
    SafeRelease(r->kEstSRV); SafeRelease(r->kEstBuf);
    SafeRelease(r->driveSRV); SafeRelease(r->driveUAV); SafeRelease(r->driveTex);
    SafeRelease(r->driveFiltSRV); SafeRelease(r->driveFiltUAV); SafeRelease(r->driveFiltTex);
    SafeRelease(r->driveStateSRV); SafeRelease(r->driveStateUAV); SafeRelease(r->driveStateTex);
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++) { SafeRelease(r->delaySRV[i]); SafeRelease(r->delayUAV[i]); SafeRelease(r->delayTex[i]); }
    r->delayHead = 0; r->delayCount = 0;
    r->stateValid = false; r->settleLeft = 0;
    SafeRelease(r->bTrueSRV); SafeRelease(r->bTrueUAV); SafeRelease(r->bTrueTex);
    SafeRelease(r->bEstSRV); SafeRelease(r->bEstUAV); SafeRelease(r->bEstTex);
    SafeRelease(r->gainASRV); SafeRelease(r->gainAUAV); SafeRelease(r->gainATex);
    SafeRelease(r->gainBSRV); SafeRelease(r->gainBUAV); SafeRelease(r->gainBTex);
    SafeRelease(r->flatTrueSRV); SafeRelease(r->flatTrueUAV); SafeRelease(r->flatTrueTex);
    SafeRelease(r->flatEstSRV); SafeRelease(r->flatEstUAV); SafeRelease(r->flatEstTex);
    for (unsigned int i = 0; i < 2; i++) {
        SafeRelease(r->activeSRV[i]); SafeRelease(r->activeUAV[i]); SafeRelease(r->activeTex[i]);
        SafeRelease(r->boostSRV[i]); SafeRelease(r->boostUAV[i]); SafeRelease(r->boostTex[i]);
    }
    SafeRelease(r->boostLutSRV); SafeRelease(r->boostLutBuf);
    SafeRelease(r->zonePartUAV); SafeRelease(r->zonePartBuf); r->zoneSlices = 1;
    ReleaseStar(r);
    ReleaseGlow(r);
    ReleaseClock(r);
    SafeRelease(r->cb);
    r->valid = false;
}

void FaldReleaseResources(MonitorContext* ctx) {
    if (!ctx || !ctx->fald) return;
    FaldTrace("ReleaseResources");
    ReleaseAll(ctx->fald);
    delete ctx->fald;
    ctx->fald = nullptr;
}

static bool MakeRWTexture(UINT w, UINT h, ID3D11Texture2D** tex, ID3D11UnorderedAccessView** uav, ID3D11ShaderResourceView** srv,
                          DXGI_FORMAT format = DXGI_FORMAT_R32_FLOAT) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = format;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, tex))) return false;
    if (FAILED(g_device->CreateUnorderedAccessView(*tex, nullptr, uav))) return false;
    if (FAILED(g_device->CreateShaderResourceView(*tex, nullptr, srv))) return false;
    return true;
}

// The zone sweeps' slice partials (fald_shader.h ZonePart; G4's GlowBandPart): cols * rows * slices records, UAV only.
static bool MakeZonePartBuffer(UINT count, ID3D11Buffer** buf, ID3D11UnorderedAccessView** uav, UINT stride = FALD_ZONE_PART_BYTES) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = count * stride;
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    bd.StructureByteStride = stride;
    if (FAILED(g_device->CreateBuffer(&bd, nullptr, buf))) return false;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_UNKNOWN;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = count;
    return SUCCEEDED(g_device->CreateUnorderedAccessView(*buf, &ud, uav));
}

// The five cols x rows RGBA32F textures of the starfield balancing (created on the first frame the option is on).
static bool EnsureStar(FaldResources* r) {
    if (r->zoneSlices > 1 && !g_faldStarStatCombineCS) return false;   // zones of several slices: S0 needs its combine
    if (r->starStatTex && r->starWTex && r->starPlanTex && r->starBgTex && r->starPlan2Tex) return true;
    // a failed creation is retried on the cadence the Build retry uses (every 300 frames), not every frame
    if (r->starRetryCounter != 0 && (r->starRetryCounter++ % 300) != 0) return false;
    ReleaseStar(r);
    const FaldPanelParams& p = r->params;
    const DXGI_FORMAT f = DXGI_FORMAT_R32G32B32A32_FLOAT;
    if (MakeRWTexture(p.cols, p.rows, &r->starStatTex, &r->starStatUAV, &r->starStatSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &r->starWTex, &r->starWUAV, &r->starWSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &r->starPlanTex, &r->starPlanUAV, &r->starPlanSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &r->starBgTex, &r->starBgUAV, &r->starBgSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &r->starPlan2Tex, &r->starPlan2UAV, &r->starPlan2SRV, f)) {
        r->starFailLogged = false;
        return true;
    }
    ReleaseStar(r);
    r->starRetryCounter = 1;
    if (!r->starFailLogged) {
        std::cerr << "[FALD] starfield balancing textures could not be created: the option stays off" << std::endl;
        r->starFailLogged = true;
    }
    return false;
}

// The zone textures of the glow fill (created on the first frame the option is on): Vz, the dilation on the lattice
// extended by FALD_GLOW_REACH_MAX on every side, the closing, (Ez, Dz, Cz, Vz) for the pixels, and the count-threshold
// band's scale k; with the band (FaldGlowBandActive, C16) also G4's record + neighbour bound, G5's scratch k and — zones
// of more than one slice — G4's partials of the bound.
static bool EnsureGlow(FaldResources* r) {
    const bool band = FaldGlowBandActive(r->params);
    if (r->zoneSlices > 1 && band && !g_faldGlowBandCombineCS) return false;   // G4 needs its combine
    if (r->glowVTex && r->glowDilTex && r->glowCTex && r->glowEnvTex && r->glowKTex &&
        (!band || (r->glowBandTex && r->glowATex && r->glowKTmpTex && (r->zoneSlices == 1 || r->glowBandPartBuf)))) return true;
    // a failed creation is retried on the cadence the Build retry uses (every 300 frames), not every frame
    if (r->glowRetryCounter != 0 && (r->glowRetryCounter++ % 300) != 0) return false;
    ReleaseGlow(r);
    const FaldPanelParams& p = r->params;
    const DXGI_FORMAT f4 = DXGI_FORMAT_R32G32B32A32_FLOAT;
    if (MakeRWTexture(p.cols, p.rows, &r->glowVTex, &r->glowVUAV, &r->glowVSRV) &&
        MakeRWTexture(p.cols + 2 * FALD_GLOW_REACH_MAX, p.rows + 2 * FALD_GLOW_REACH_MAX, &r->glowDilTex, &r->glowDilUAV, &r->glowDilSRV) &&
        MakeRWTexture(p.cols, p.rows, &r->glowCTex, &r->glowCUAV, &r->glowCSRV) &&
        MakeRWTexture(p.cols, p.rows, &r->glowEnvTex, &r->glowEnvUAV, &r->glowEnvSRV, f4) &&
        MakeRWTexture(p.cols, p.rows, &r->glowKTex, &r->glowKUAV, &r->glowKSRV) &&
        (!band || (MakeRWTexture(p.cols, p.rows, &r->glowBandTex, &r->glowBandUAV, &r->glowBandSRV, f4) &&
                   MakeRWTexture(2 * p.cols, p.rows, &r->glowATex, &r->glowAUAV, &r->glowASRV, f4) &&
                   MakeRWTexture(p.cols, p.rows, &r->glowKTmpTex, &r->glowKTmpUAV, &r->glowKTmpSRV) &&
                   (r->zoneSlices == 1 || MakeZonePartBuffer(p.cols * p.rows * r->zoneSlices, &r->glowBandPartBuf,
                                                             &r->glowBandPartUAV, FALD_GLOW_BAND_PART_BYTES))))) {
        r->glowFailLogged = false;
        return true;
    }
    ReleaseGlow(r);
    r->glowRetryCounter = 1;
    if (!r->glowFailLogged) {
        std::cerr << "[FALD] glow fill textures could not be created: the option stays off" << std::endl;
        r->glowFailLogged = true;
    }
    return false;
}

// The four cols x rows R32F textures of the panel clock (created on the first frame temporal mode 3 is on).
static bool EnsureClock(FaldResources* r) {
    if (r->clkStateTex[0] && r->clkStateTex[1] && r->clkPrevTex && r->clkEstTex) return true;
    // a failed creation is retried on the cadence the Build retry uses (every 300 frames), not every frame
    if (r->clkRetryCounter != 0 && (r->clkRetryCounter++ % 300) != 0) return false;
    ReleaseClock(r);
    const FaldPanelParams& p = r->params;
    if (MakeRWTexture(p.cols, p.rows, &r->clkStateTex[0], &r->clkStateUAV[0], &r->clkStateSRV[0]) &&
        MakeRWTexture(p.cols, p.rows, &r->clkStateTex[1], &r->clkStateUAV[1], &r->clkStateSRV[1]) &&
        MakeRWTexture(p.cols, p.rows, &r->clkPrevTex, &r->clkPrevUAV, &r->clkPrevSRV) &&
        MakeRWTexture(p.cols, p.rows, &r->clkEstTex, &r->clkEstUAV, &r->clkEstSRV)) {
        r->clkFailLogged = false;
        return true;
    }
    ReleaseClock(r);
    r->clkRetryCounter = 1;
    if (!r->clkFailLogged) {
        std::cerr << "[FALD] panel clock textures could not be created: temporal mode 3 runs as off" << std::endl;
        r->clkFailLogged = true;
    }
    return false;
}

static bool MakeFloatBuffer(const std::vector<float>& data, ID3D11Buffer** buf, ID3D11ShaderResourceView** srv) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = (UINT)(data.size() * sizeof(float));
    bd.Usage = D3D11_USAGE_IMMUTABLE;
    bd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = data.data();
    if (FAILED(g_device->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32_FLOAT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = (UINT)data.size();
    return SUCCEEDED(g_device->CreateShaderResourceView(*buf, &sd, srv));
}

static bool Build(MonitorContext* ctx, FaldResources* r, const std::wstring& path) {
    ReleaseAll(r);
    r->paramsPath = path;
    r->width = ctx->width; r->height = ctx->height;
    r->builtForHdr = ctx->isHDREnabled;
    r->refusedByFile = false;
    r->fileSize = r->fileMtime = 0;
    FaldPanelFileStamp(path, r->fileSize, r->fileMtime);   // taken before the read: a write racing the load re-triggers a rebuild
    r->fileCheckCounter = 0;
    std::string err;
    if (!LoadFaldPanelParams(path, r->params, err)) { r->lastError = "params: " + err; r->refusedByFile = true; return false; }
    const FaldPanelParams& p = r->params;
    if (!FaldTransferMatchesMode(p.transfer, ctx->isHDREnabled)) {
        // The fit's code domain is the panel's: a PQ (HDR) file cannot serve an ACM SDR desktop and vice versa.
        r->lastError = std::string("panel file transfer is ") + (p.transfer == FALD_TRANSFER_GAMMA ? "gamma (SDR fit)" : "PQ (HDR fit)") +
                       " but the monitor is in " + (ctx->isHDREnabled ? "HDR" : "SDR (ACM)") + " - use a file profiled in this mode";
        r->refusedByFile = true;
        return false;
    }
    if (!FaldLatticeFits(p, ctx->width, ctx->height)) {
        r->lastError = "panel lattice (" + std::to_string(p.cols * p.cellW) + "x" + std::to_string(p.rows * p.cellH) +
                       ") does not fit the monitor (" + std::to_string(ctx->width) + "x" + std::to_string(ctx->height) + ")";
        r->refusedByFile = true;
        return false;
    }
    // intermediate (swapchain format so the main shader writes it unchanged)
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = ctx->width; d.Height = ctx->height; d.MipLevels = 1; d.ArraySize = 1;
    d.Format = ctx->swapchainFormat; d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &r->inter)) ||
        FAILED(g_device->CreateRenderTargetView(r->inter, nullptr, &r->interRTV)) ||
        FAILED(g_device->CreateShaderResourceView(r->inter, nullptr, &r->interSRV))) {
        r->lastError = "intermediate texture"; return false;
    }
    // curve LUT (curveN x 1, R32F)
    {
        D3D11_TEXTURE2D_DESC c = {};
        c.Width = p.curveN; c.Height = 1; c.MipLevels = 1; c.ArraySize = 1; c.Format = DXGI_FORMAT_R32_FLOAT;
        c.SampleDesc.Count = 1; c.Usage = D3D11_USAGE_IMMUTABLE; c.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = {};
        init.pSysMem = p.curve.data(); init.SysMemPitch = p.curveN * sizeof(float);
        if (FAILED(g_device->CreateTexture2D(&c, &init, &r->curveTex)) ||
            FAILED(g_device->CreateShaderResourceView(r->curveTex, nullptr, &r->curveSRV))) {
            r->lastError = "curve texture"; return false;
        }
    }
    if (!MakeFloatBuffer(p.kTrue, &r->kTrueBuf, &r->kTrueSRV)) { r->lastError = "kTrue buffer"; return false; }
    if (!MakeFloatBuffer(p.kEst, &r->kEstBuf, &r->kEstSRV)) { r->lastError = "kEst buffer"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveTex, &r->driveUAV, &r->driveSRV)) { r->lastError = "drive texture"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveFiltTex, &r->driveFiltUAV, &r->driveFiltSRV)) { r->lastError = "filtered drive texture"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveStateTex, &r->driveStateUAV, &r->driveStateSRV)) { r->lastError = "drive state texture"; return false; }
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++)
        if (!MakeRWTexture(p.cols, p.rows, &r->delayTex[i], &r->delayUAV[i], &r->delaySRV[i])) { r->lastError = "delay ring texture"; return false; }
    r->delayHead = 0; r->delayCount = 0; r->delayFrames = 0;
    r->stateValid = false; r->settleLeft = 0; r->temporalMode = FALD_TEMPORAL_OFF;
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bTrueTex, &r->bTrueUAV, &r->bTrueSRV)) { r->lastError = "B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bEstTex, &r->bEstUAV, &r->bEstSRV)) { r->lastError = "B_est texture"; return false; }
    // R32G32F: (gain, the flat-normalised B_est of the soft knee's ceiling), low-passed together (C15)
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainATex, &r->gainAUAV, &r->gainASRV, DXGI_FORMAT_R32G32_FLOAT)) { r->lastError = "gain texture A"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainBTex, &r->gainBUAV, &r->gainBSRV, DXGI_FORMAT_R32G32_FLOAT)) { r->lastError = "gain texture B"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatTrueTex, &r->flatTrueUAV, &r->flatTrueSRV)) { r->lastError = "flat B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatEstTex, &r->flatEstUAV, &r->flatEstSRV)) { r->lastError = "flat B_est texture"; return false; }
    r->zoneSlices = FaldZoneSlices(p.cellW, p.cellH);            // zones larger than one slice: the sweeps' partials
    if (r->zoneSlices > FALD_ZONE_SLICES_MAX) { r->lastError = "zones too large to sweep (slices > 65535)"; r->refusedByFile = true; return false; }
    if (r->zoneSlices > 1 && !g_faldStatCombineCS) { r->lastError = "zone combine shader unavailable"; return false; }
    if (r->zoneSlices > 1 && !MakeZonePartBuffer(p.cols * p.rows * r->zoneSlices, &r->zonePartBuf, &r->zonePartUAV)) {
        r->lastError = "zone partials buffer"; return false;
    }
    if (p.hasBoost) {
        // black-frame LED boost: per-round zone flags + 2x1 result, and the LUT as (first zone COUNT, boost) pairs
        for (unsigned int i = 0; i < 2; i++) {
            if (!MakeRWTexture(p.cols, p.rows, &r->activeTex[i], &r->activeUAV[i], &r->activeSRV[i])) { r->lastError = "active-zone texture"; return false; }
            if (!MakeRWTexture(2, 1, &r->boostTex[i], &r->boostUAV[i], &r->boostSRV[i])) { r->lastError = "boost texture"; return false; }
        }
        std::vector<float> lut;
        for (uint32_t i = 0; i < p.boostN; i++) {
            lut.push_back((float)FaldBoostZoneThreshold(p.boostLo[i], p.cols * p.rows));
            lut.push_back(p.boostVal[i]);
        }
        if (!MakeFloatBuffer(lut, &r->boostLutBuf, &r->boostLutSRV)) { r->lastError = "boost LUT buffer"; return false; }
    }
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = FALD_CB_BYTES;   // 76 words, see FaldCB
    cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&cbd, nullptr, &r->cb))) { r->lastError = "constant buffer"; return false; }
    r->valid = true;
    r->lastError.clear();
    ComputeFlatResponse(r);
    std::cout << "[FALD] Monitor " << ctx->index << " resources ready: " << p.cols << "x" << p.rows << " cells of "
              << p.cellW << "x" << p.cellH << " px, sub " << p.sub << ", white " << p.white << " nits, transfer "
              << (p.transfer == FALD_TRANSFER_GAMMA ? "gamma " + std::to_string(p.sdrGamma) : std::string("PQ"))
              << " (" << (ctx->isHDREnabled ? "HDR" : "ACM SDR") << "), kernels "
              << (2 * p.reachTrueC + 1) << "x" << (2 * p.reachTrueR + 1) << " / " << (2 * p.reachEstC + 1) << "x" << (2 * p.reachEstR + 1)
              << ", black-frame boost " << (p.hasBoost ? std::to_string(p.boostN) + " steps" +
                     (p.boostRule == FALD_BOOST_RULE_MEAN ? " (zone rule LIT-or-MEAN)" : " (zone rule LIT-or-DIM)") : std::string("none")) << std::endl;
    return true;
}

bool FaldEnsureResources(MonitorContext* ctx, const FaldSettings& settings) {
    const std::wstring& paramsPath = settings.paramsPath;
    if (!ctx || !FaldShadersReady() || paramsPath.empty()) return false;
    if (!ctx->fald) { ctx->fald = new FaldResources(); FaldTrace("EnsureResources: new FaldResources"); }
    FaldResources* r = ctx->fald;
    bool stale = r->paramsPath != paramsPath || r->width != ctx->width || r->height != ctx->height ||
                 r->reloadSeq != settings.reloadSeq || r->builtForHdr != ctx->isHDREnabled;
    if (r->valid && !stale && ++r->fileCheckCounter >= FALD_FILE_POLL_FRAMES) {
        // A panel file re-exported IN PLACE (same path) must not keep the old tables on the GPU
        // (HW 2026-09-13: neither a same-path set_fald_params nor an off/on toggle rebuilt).
        r->fileCheckCounter = 0;
        unsigned long long size = 0, mtime = 0;
        if (FaldPanelFileStamp(paramsPath, size, mtime) && (size != r->fileSize || mtime != r->fileMtime)) {
            FaldTrace("EnsureResources: params file changed on disk -> rebuild");
            stale = true;
        }
    }
    if (r->valid && !stale) return true;
    r->reloadSeq = settings.reloadSeq;
    // A failed build (bad/partially written params file, transient resource failure) is retried
    // every ~300 frames so a re-exported file or a recovered device picks the layer up again;
    // each distinct error is logged once. A refusal caused by the FILE is only retried when the
    // file's size/mtime changed (no periodic re-read of a file known to be wrong for this mode).
    if (!stale && !r->lastError.empty()) {
        if ((++r->retryCounter % 300) != 0) return false;
        if (r->refusedByFile) {
            unsigned long long size = 0, mtime = 0;
            bool readable = FaldPanelFileStamp(paramsPath, size, mtime);
            if (readable == (r->fileSize != 0 || r->fileMtime != 0) && size == r->fileSize && mtime == r->fileMtime) return false;
        }
    }
    FaldTrace("EnsureResources: Build begin");
    if (!Build(ctx, r, paramsPath)) {
        FaldTrace("EnsureResources: Build FAILED");
        if (r->lastError.empty()) r->lastError = "unknown";
        if (r->lastError != r->lastLoggedError) {
            std::cerr << "[FALD] Monitor " << ctx->index << " disabled: " << r->lastError << " (retrying periodically)" << std::endl;
            r->lastLoggedError = r->lastError;
        }
        return false;
    }
    r->lastLoggedError.clear();
    FaldTrace("EnsureResources: Build ok");
    return true;
}

bool FaldLayerRefused(const MonitorContext* ctx, const FaldSettings& settings) {
    const FaldResources* r = ctx ? ctx->fald : nullptr;
    if (!r || r->valid || !r->refusedByFile) return false;
    if (r->paramsPath != settings.paramsPath || r->reloadSeq != settings.reloadSeq || r->builtForHdr != ctx->isHDREnabled ||
        r->width != ctx->width || r->height != ctx->height) return false;   // something changed: let the next frame retry
    return true;
}

// ---------------------------------------------------------------------------------------------
// Passes
// ---------------------------------------------------------------------------------------------
// boostOn = false: the flat-lattice normalisation pass (a boost-free conv whatever the file says).
static void FillCB(FaldResources* r, uint32_t roundIdx, uint32_t blurDir = 0, bool boostOn = true) {
    const FaldPanelParams& p = r->params;
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(g_context->Map(r->cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) return;
    uint32_t* u = (uint32_t*)m.pData; float* f = (float*)m.pData;
    memset(m.pData, 0, FALD_CB_BYTES);
    u[0] = (uint32_t)r->width; u[1] = (uint32_t)r->height; u[2] = p.cols; u[3] = p.rows;
    u[4] = p.sub; u[5] = p.cellW; u[6] = p.cellH; u[7] = roundIdx;
    u[8] = p.reachTrueC; u[9] = p.reachTrueR; u[10] = p.reachEstC; u[11] = p.reachEstR;
    u[12] = p.curveN; f[13] = p.white; f[14] = p.tmin; f[15] = p.area0;
    f[16] = p.w[0]; f[17] = p.w[1]; f[18] = p.w[2]; f[19] = p.gainMin;
    f[20] = p.gainMax; f[21] = p.driveFloor; f[22] = p.curveLogMin; f[23] = p.curveLogMax;
    u[24] = r->debugMode; u[25] = p.originX; u[26] = p.originY; u[27] = blurDir;
    f[28] = p.fadeLo; f[29] = p.fadeHi; f[30] = p.gainSmoothCells * (float)p.sub;   // sigma in fine samples
    u[31] = p.transfer;                                                             // 0 = PQ (HDR), 1 = gamma (ACM SDR)
    f[32] = p.lumFadeLo; f[33] = p.lumFadeHi;                                       // pixel-luminance fade (nits)
    u[34] = (boostOn && p.hasBoost) ? p.boostN : 0u;                                // black-frame LED boost steps (0 = no term)
    u[35] = r->starOn ? 1u : 0u;                                                    // starfield balancing (fields in t15 / t18)
    // the panel file's leak colour (tmin * m_c; = tmin for FLD1) is always in the CB so the debug views can show the
    // toggle's influence; pedMode selects it in Correct() (1 only when the file has a colour, else it is a no-op)
    const bool perChannel = (r->pedMode == 1) && p.hasPedColour;
    f[36] = p.tmin * p.pedRGB[0]; f[37] = p.tmin * p.pedRGB[1]; f[38] = p.tmin * p.pedRGB[2];
    u[39] = perChannel ? 1u : 0u;
    // colour-part strength + its own pixel-luminance fade (-1 = follow lumFade): words 40-42
    f[40] = p.chromaGain;
    f[41] = (p.chromaLo < 0.0f) ? p.lumFadeLo : p.chromaLo;
    f[42] = (p.chromaHi < 0.0f) ? p.lumFadeHi : p.chromaHi;
    f[43] = p.sdrGamma;                                                             // panel EOTF exponent (transfer 1)
    // temporal drive state (words 44-47): per-frame blend factors, mode, "no valid state yet" (copy the drive)
    f[44] = r->tempAlphaRise; f[45] = r->tempAlphaFall;
    u[46] = r->temporalMode; u[47] = r->stateValid ? 0u : 1u;
    // black-frame LED boost: the zone activation rule (words 48-51; read only when word 34 != 0)
    f[48] = p.boostLitNits; f[49] = p.boostLitFrac; f[50] = p.boostDimNits; f[51] = p.boostDimFrac;
    // starfield balancing (words 52-65; read only when word 35 != 0)
    const FaldResources::StarCB& sc = r->star;
    f[52] = sc.even; f[53] = sc.lift; f[54] = sc.targetGain; f[55] = sc.capNits;
    f[56] = sc.strength; f[57] = sc.areaLo; f[58] = sc.areaHi; f[59] = sc.peakHi;
    f[60] = sc.nbLo; f[61] = sc.nbHi; u[62] = sc.reach; u[63] = sc.evenReach;
    f[64] = sc.targetSigma; f[65] = sc.keepNits;                                    // words 64-65
    // panel clock (temporal mode 3; read by pass 1c only): the clocks' weights, then per clock the blends (words 66-71)
    f[66] = r->clkW[0]; f[67] = r->clkW[1];
    f[68] = r->clkFactor[0]; f[69] = r->clkFactor[1]; f[70] = r->clkFactor[2]; f[71] = r->clkFactor[3];
    // black-frame LED boost: the zone rule (words 72-74; read only when word 34 != 0)
    u[72] = p.boostRule; f[73] = p.boostMeanGamma; f[74] = p.boostMeanThresh;
    // glow fill (word 75 = on; words 76-79 read only when it is set / by the glow passes)
    u[75] = r->glowOn ? 1u : 0u;
    f[76] = r->glow.strength; f[77] = r->glow.capNits; u[78] = r->glow.reach; f[79] = FaldGlowReqCeil(p);
    u[80] = r->glowBand ? 1u : 0u;                                                  // the count-threshold band (k in t24)
    u[81] = 0u; u[82] = 0u; u[83] = 0u;
    g_context->Unmap(r->cb, 0);
}

static const UINT FALD_SRV_SLOTS = 27;   // t0..t26 (fald_shader.h)

static void BindCommon(FaldResources* r, bool compute) {
    ID3D11ShaderResourceView* srvs[FALD_SRV_SLOTS] = { r->interSRV, r->curveSRV, r->kTrueSRV, r->kEstSRV, nullptr, nullptr, nullptr,
                                                       r->flatTrueSRV, r->flatEstSRV, nullptr, nullptr, nullptr,
                                                       r->boostLutSRV, nullptr, nullptr,     // t12: nullptr without a boost LUT
                                                       r->starOn ? r->starPlanSRV : nullptr, // t15: the starfield plan (Balance)
                                                       nullptr, nullptr,                     // t16/t17: star passes only (RunStar)
                                                       r->starOn ? r->starPlan2SRV : nullptr, // t18: ln background, near, spk (Balance)
                                                       nullptr,                              // t19: star pass S1 only (RunStar)
                                                       nullptr, nullptr, nullptr,            // t20-t22: glow passes only (RunGlow)
                                                       r->glowOn ? r->glowEnvSRV : nullptr,  // t23: the glow deficit (GlowAdd)
                                                       r->glowBand ? r->glowKSRV : nullptr,  // t24: the band's zone scale (GlowAdd)
                                                       nullptr, nullptr };                   // t25 / t26: glow pass G5 only (RunGlow)
    if (compute) {
        g_context->CSSetConstantBuffers(0, 1, &r->cb);
        g_context->CSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_context->CSSetSamplers(0, 1, &g_faldSampler);
    } else {
        g_context->PSSetConstantBuffers(0, 1, &r->cb);
        g_context->PSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_context->PSSetSamplers(0, 1, &g_faldSampler);
    }
}

static void UnbindCompute() {
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    ID3D11UnorderedAccessView* nullUav[4] = {};             // u0 / u1 + u2, the zone sweeps' partials (+ u3: G4's bound partials)
    g_context->CSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_context->CSSetUnorderedAccessViews(0, 4, nullUav, nullptr);
    g_context->CSSetShader(nullptr, nullptr, 0);
}

static void RunStat(FaldResources* r, uint32_t roundIdx) {
    const FaldPanelParams& p = r->params;
    FillCB(r, roundIdx);
    g_context->CSSetShader(g_faldStatCS, nullptr, 0);
    BindCommon(r, true);
    if (roundIdx == 1) {
        ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
        g_context->CSSetShaderResources(5, 2, fields);
        g_context->CSSetShaderResources(9, 1, &r->gainBSRV);      // smoothed (gain, ceiling B_est) of the previous round (C15)
    }
    ID3D11UnorderedAccessView* uavs[3] = { r->driveUAV, r->activeUAV[roundIdx & 1u],    // u1: nullptr without a boost LUT
                                           r->zonePartUAV };                              // (the shader then never writes it)
    g_context->CSSetUnorderedAccessViews(0, 3, uavs, nullptr);                           // u2: partials, zones > one slice
    g_context->Dispatch(p.cols, p.rows, r->zoneSlices);
    if (r->zoneSlices > 1) {                                                             // fold the slices, finish the zones
        g_context->CSSetShader(g_faldStatCombineCS, nullptr, 0);
        g_context->Dispatch(p.cols, p.rows, 1);
    }
    UnbindCompute();
}

// Starfield balancing (option on only): S0 star statistic of the SOURCE frame -> S1 tapered protection + zone weights
// -> S2 target + plan; every later pass samples plan (t15) and plan2 (t18). Recomputed on EVERY run of the layer — new
// frame, C2 re-process of the cached frame and temporal settle frame alike: the fields are a pure function of the
// source frame in r->inter, which all three paths leave valid before FaldRunPasses, so there is no state to keep in
// step with the content. The passes bind only what they read (never BindCommon: that would bind plan / plan2 as SRVs
// while S1 / S2 write them).
static void RunStar(FaldResources* r) {
    const FaldPanelParams& p = r->params;
    FillCB(r, 0);
    ID3D11ShaderResourceView* in2[2] = { r->interSRV, r->curveSRV };
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetSamplers(0, 1, &g_faldSampler);
    // S0: per zone stat = (peak, speck-zone flag, sparse, solid) + bg = (ln b, brightest pixel's index, lit sum, a_eff)
    ID3D11UnorderedAccessView* out0[3] = { r->starStatUAV, r->starBgUAV, r->zonePartUAV };   // u2: zones > one slice
    g_context->CSSetShader(g_faldStarStatCS, nullptr, 0);
    g_context->CSSetShaderResources(0, 2, in2);
    g_context->CSSetUnorderedAccessViews(0, 3, out0, nullptr);
    g_context->Dispatch(p.cols, p.rows, r->zoneSlices);
    if (r->zoneSlices > 1) {
        g_context->CSSetShader(g_faldStarStatCombineCS, nullptr, 0);
        g_context->Dispatch(p.cols, p.rows, 1);
    }
    UnbindCompute();
    // S1: the tapered protection field + flank test + zone weights -> (wt, wt ln peak, flank, spk) and (ln background, near, spk, w)
    ID3D11UnorderedAccessView* out1[2] = { r->starWUAV, r->starPlan2UAV };
    g_context->CSSetShader(g_faldStarWeightCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(16, 1, &r->starStatSRV);
    g_context->CSSetShaderResources(19, 1, &r->starBgSRV);
    g_context->CSSetUnorderedAccessViews(0, 2, out1, nullptr);
    g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
    // S2: (w0_field, ln target, ln lift, ln peak)
    ID3D11ShaderResourceView* in3[2] = { r->starStatSRV, r->starWSRV };
    g_context->CSSetShader(g_faldStarPlanCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(16, 2, in3);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->starPlanUAV, nullptr);
    g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

// Glow fill (option on only), after EACH round's conv pass: G0 zone pedestal of this round's B_true (t5, boost
// included; t7 = the flat-lattice field) -> G1 box maximum on the extended lattice -> G2 box minimum (the closing) -> G3
// blur + deficit. The statistic round 1 / the pixel pass then sample glowEnv (t23). Stateless: a pure function of the
// round's fields. The passes bind only what they read (never BindCommon: that would bind glowEnv as an SRV while G3
// writes it).
static void RunGlow(FaldResources* r, uint32_t roundIdx) {
    const FaldPanelParams& p = r->params;
    FillCB(r, 0);
    const UINT gz = (p.cols + 15) / 16, gzy = (p.rows + 15) / 16;
    const UINT ge = (p.cols + 2 * FALD_GLOW_REACH_MAX + 15) / 16, gey = (p.rows + 2 * FALD_GLOW_REACH_MAX + 15) / 16;
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    // G0
    g_context->CSSetShader(g_faldGlowZoneCS, nullptr, 0);
    g_context->CSSetShaderResources(5, 1, &r->bTrueSRV);
    g_context->CSSetShaderResources(7, 1, &r->flatTrueSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->glowVUAV, nullptr);
    g_context->Dispatch(gz, gzy, 1);
    UnbindCompute();
    // G1
    g_context->CSSetShader(g_faldGlowDilateCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(20, 1, &r->glowVSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->glowDilUAV, nullptr);
    g_context->Dispatch(ge, gey, 1);
    UnbindCompute();
    // G2
    g_context->CSSetShader(g_faldGlowErodeCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(21, 1, &r->glowDilSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->glowCUAV, nullptr);
    g_context->Dispatch(gz, gzy, 1);
    UnbindCompute();
    // G3
    g_context->CSSetShader(g_faldGlowEnvCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(20, 1, &r->glowVSRV);
    g_context->CSSetShaderResources(22, 1, &r->glowCSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->glowEnvUAV, nullptr);
    g_context->Dispatch(gz, gzy, 1);
    UnbindCompute();
    // G4 (band only, EVERY round): the zone record that keeps the fill off the firmware's count threshold — a
    // full-resolution sweep like the statistic pass, on THIS round's corrected request (its fields + gain are what is
    // bound): (Pc, Pf, LIT flag, k0) and the neighbour bound A_d (C16). Round 1's k is exact for the frame that is sent;
    // round 0's is not when the trust factor moves between the rounds (DLC glowfill.py item 7). G5 then runs the
    // neighbour guard (one thread group) and writes the final k; k (t24) is not bound while the two run.
    (void)roundIdx;
    if (r->glowBand) {
        g_context->CSSetShader(g_faldGlowBandCS, nullptr, 0);
        BindCommon(r, true);
        ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
        g_context->CSSetShaderResources(5, 2, fields);
        g_context->CSSetShaderResources(9, 1, &r->gainBSRV);
        ID3D11ShaderResourceView* none = nullptr;
        g_context->CSSetShaderResources(24, 1, &none);
        ID3D11UnorderedAccessView* ub[4] = { r->glowBandUAV, r->glowAUAV, r->zonePartUAV,   // u2 / u3: the slice partials
                                             r->glowBandPartUAV };                            // (zones > one slice only)
        g_context->CSSetUnorderedAccessViews(0, 4, ub, nullptr);
        g_context->Dispatch(p.cols, p.rows, r->zoneSlices);
        if (r->zoneSlices > 1) {
            g_context->CSSetShader(g_faldGlowBandCombineCS, nullptr, 0);
            g_context->Dispatch(p.cols, p.rows, 1);
        }
        UnbindCompute();
        // G5: the neighbour guard -> the final k (glowK = t24 of GlowAdd); its Jacobi state in u0, the scratch in u1
        g_context->CSSetShader(g_faldGlowGuardCS, nullptr, 0);
        g_context->CSSetConstantBuffers(0, 1, &r->cb);
        ID3D11ShaderResourceView* in5[2] = { r->glowBandSRV, r->glowASRV };
        g_context->CSSetShaderResources(25, 2, in5);
        ID3D11UnorderedAccessView* u5[2] = { r->glowKUAV, r->glowKTmpUAV };
        g_context->CSSetUnorderedAccessViews(0, 2, u5, nullptr);
        g_context->Dispatch(1, 1, 1);
        UnbindCompute();
    }
}

// Pass 1a (panel files with a boost LUT only): this round's zone flags -> count -> staircase -> boostTex[round].
// Relies on the CB the statistic pass of the same round filled. Not run (and nothing bound) without a LUT: the
// layer is then the boost-less one, dispatch for dispatch.
static void RunBoost(FaldResources* r, uint32_t roundIdx) {
    if (!r->params.hasBoost) return;
    const unsigned int k = roundIdx & 1u;
    g_context->CSSetShader(g_faldBoostCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(13, 1, &r->activeSRV[k]);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->boostUAV[k], nullptr);
    g_context->Dispatch(1, 1, 1);
    UnbindCompute();
}

// Pass 1b (temporal mode): filtered drive = state + a * (drive - state) per cell, from the drive map the panel's
// pipeline is fed (t4: this round's instantaneous drive, or the ring entry delayFrames frames back) and the state
// committed after the previous frame (t11); with no valid state the drive is copied.
static void RunTemporal(FaldResources* r, ID3D11ShaderResourceView* inDrive) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldTemporalCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &inDrive);
    g_context->CSSetShaderResources(11, 1, &r->driveStateSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->driveFiltUAV, nullptr);
    g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

// Pass 1c (temporal mode 3, valid state only): the two parity clocks advance in place toward the previous frame's
// round-1 drives (t4 = clkPrev) by the CB's blend factors; u0 / u1 receive the maps the kernels see in both rounds.
// Binds only what it reads (like RunStar); the states' UAV slots are cleared here (UnbindCompute clears three).
static void RunPanelClock(FaldResources* r) {
    const FaldPanelParams& p = r->params;
    FillCB(r, 0);
    g_context->CSSetShader(g_faldPanelClockCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(4, 1, &r->clkPrevSRV);
    ID3D11UnorderedAccessView* uavs[4] = { r->driveFiltUAV, r->clkEstUAV, r->clkStateUAV[0], r->clkStateUAV[1] };
    g_context->CSSetUnorderedAccessViews(0, 4, uavs, nullptr);
    g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    ID3D11UnorderedAccessView* nullUav[4] = {};
    g_context->CSSetUnorderedAccessViews(0, 4, nullUav, nullptr);
    UnbindCompute();
}

// trueDrive / estDrive: the drive maps the real-spread and the estimate kernels see (both the instantaneous drive
// unless a temporal mode routes the filtered one). boost: this round's 2x1 boost texture (B_true only; nullptr =
// none — no LUT in the file, or the flat-lattice pass, where the CB's boostN is 0 and the shader never reads t14).
static void RunConv(FaldResources* r, ID3D11ShaderResourceView* trueDrive, ID3D11ShaderResourceView* estDrive,
                    ID3D11ShaderResourceView* boost) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldConvCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &trueDrive);
    g_context->CSSetShaderResources(10, 1, &estDrive);
    g_context->CSSetShaderResources(14, 1, &boost);
    ID3D11UnorderedAccessView* uavs[2] = { r->bTrueUAV, r->bEstUAV };
    g_context->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);
    g_context->Dispatch(FaldConvGroupsX(p.cols, p.rows), p.sub * p.sub, 1);   // cells x sub-offsets (fald_shader.h)
    UnbindCompute();
}

// Pass 2b/2c: (gain, the knee's ceiling B_est) on the fine grid, then a separable Gaussian low-pass of both (A -> B -> A ... final in gainB).
static void RunGain(FaldResources* r) {
    const FaldPanelParams& p = r->params;
    UINT gx = (p.cols * p.sub + 15) / 16, gy = (p.rows * p.sub + 15) / 16;
    g_context->CSSetShader(g_faldGainCS, nullptr, 0);
    BindCommon(r, true);
    ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
    g_context->CSSetShaderResources(5, 2, fields);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainAUAV, nullptr);
    g_context->Dispatch(gx, gy, 1);
    UnbindCompute();
    // horizontal: A -> B
    FillCB(r, 1, 0);
    g_context->CSSetShader(g_faldBlurCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(9, 1, &r->gainASRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainBUAV, nullptr);
    g_context->Dispatch(gx, gy, 1);
    UnbindCompute();
    // vertical: B -> A
    FillCB(r, 1, 1);
    g_context->CSSetShader(g_faldBlurCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(9, 1, &r->gainBSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainAUAV, nullptr);
    g_context->Dispatch(gx, gy, 1);
    UnbindCompute();
    // final smoothed (gain, ceiling B_est) lives in A; copy to B so consumers always read gainB
    g_context->CopyResource(r->gainBTex, r->gainATex);
}

static std::string NarrowUtf8(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &out[0], n, nullptr, nullptr);
    return out;
}

// Flat-lattice response: run the convolution once on a drive map of ones and keep the two fields.
// Must run after the fine textures exist; the flat textures are bound as SRVs t7/t8 from then on
// (they are nullptr during this call, which the conv pass does not read).
static void ComputeFlatResponse(FaldResources* r) {
    const float one[4] = { 1.0f, 1.0f, 1.0f, 1.0f };
    g_context->ClearUnorderedAccessViewFloat(r->driveUAV, one);
    ID3D11ShaderResourceView* saveT = r->flatTrueSRV; ID3D11ShaderResourceView* saveE = r->flatEstSRV;
    r->flatTrueSRV = nullptr; r->flatEstSRV = nullptr;          // not inputs of this pass
    FillCB(r, 0, 0, false);                                     // boost 1: the normalisation is the un-boosted lattice
    RunConv(r, r->driveSRV, r->driveSRV, nullptr);
    r->flatTrueSRV = saveT; r->flatEstSRV = saveE;
    g_context->CopyResource(r->flatTrueTex, r->bTrueTex);
    g_context->CopyResource(r->flatEstTex, r->bEstTex);
}

static void DumpTexture(ID3D11Texture2D* tex, const std::wstring& file, UINT w, UINT h, UINT bytesPerPx) {
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        std::ofstream f(file, std::ios::binary);
        for (UINT y = 0; y < h; y++) f.write((const char*)m.pData + (size_t)y * m.RowPitch, (std::streamsize)w * bytesPerPx);
        g_context->Unmap(st, 0);
    }
    st->Release();
}

// Read back the first n floats of a small R32F texture's top row (dump only; stalls the GPU).
static bool ReadBackFloats(ID3D11Texture2D* tex, float* out, UINT n) {
    if (!tex) return false;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    if (d.Width < n) return false;
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return false;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    bool ok = false;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        memcpy(out, m.pData, n * sizeof(float));
        g_context->Unmap(st, 0);
        ok = true;
    }
    st->Release();
    return ok;
}

// Consume a pending dump request: the directory (with a trailing separator) or "" when none.
static std::wstring TakeDumpRequest(MonitorContext* ctx) {
    if (!ctx->faldDumpRequested.load(std::memory_order_acquire)) return L"";
    std::wstring dir;
    {
        // the IPC handler writes faldDumpDir under g_monitorsMutex before publishing the flag
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        dir = ctx->faldDumpDir;
    }
    ctx->faldDumpRequested.store(false, std::memory_order_release);
    if (!dir.empty() && dir.back() != L'\\' && dir.back() != L'/') dir += L'\\';
    return dir;
}

// Fields + the INPUT frame (the main pass output the layer reads), after the compute passes.
static void DumpFields(MonitorContext* ctx, FaldResources* r, const std::wstring& dir) {
    const FaldPanelParams& p = r->params;
    const FaldSettings& fs = ctx->isHDREnabled ? ctx->hdrColorCorrection.fald : ctx->sdrColorCorrection.fald;
    DumpTexture(r->driveTex, dir + L"fald_drive.f32", p.cols, p.rows, 4);
    if (r->temporalMode == FALD_TEMPORAL_PANEL) {
        // panel clock: not on a seeding run (that frame is the stateless layer and the textures hold nothing yet).
        // filt / est = the maps both rounds' kernels saw; s0 / s1 = the parity clocks' LED states OF THIS FRAME (already
        // advanced; k = 0: the previous run's, untouched); dprev = the target the pass read (the previous frame's round-1
        // drives; dumped before the commit): s_p(before) is not kept, but filt = w0 s0 + w1 s1 checks offline, and
        // consecutive dumps check a step (DLC tests/test_fald_paneltime_warp.py replays them against the reference)
        if (!r->clkSeeded) {
            DumpTexture(r->driveFiltTex, dir + L"fald_drive_filt.f32", p.cols, p.rows, 4);
            DumpTexture(r->clkEstTex, dir + L"fald_clock_est.f32", p.cols, p.rows, 4);
            DumpTexture(r->clkStateTex[0], dir + L"fald_clock_s0.f32", p.cols, p.rows, 4);
            DumpTexture(r->clkStateTex[1], dir + L"fald_clock_s1.f32", p.cols, p.rows, 4);
            DumpTexture(r->clkPrevTex, dir + L"fald_clock_dprev.f32", p.cols, p.rows, 4);
        }
    } else if (r->temporalMode != FALD_TEMPORAL_OFF) {
        DumpTexture(r->driveFiltTex, dir + L"fald_drive_filt.f32", p.cols, p.rows, 4);    // the drive the kernels saw (round 1)
        DumpTexture(r->driveStateTex, dir + L"fald_drive_state.f32", p.cols, p.rows, 4);  // the state the pass READ (dumped before
    }                                                                                    // the commit: filt = s + a (d - s) checks offline)
    DumpTexture(r->bTrueTex, dir + L"fald_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->bEstTex, dir + L"fald_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->flatTrueTex, dir + L"fald_flat_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->flatEstTex, dir + L"fald_flat_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(r->gainBTex, dir + L"fald_gain_fine.rg32f", p.cols * p.sub, p.rows * p.sub, 8);   // (gain, ceiling B_est) pairs, C15
    // black-frame LED boost: the zone flags of both rounds (cols x rows float32, 1 = non-black; fald_active.f32 = round 1,
    // the corrected frame the panel receives) and the reduce pass's results. -1 / 1 when the file has no LUT.
    float boostR[2][2] = { { 1.0f, -1.0f }, { 1.0f, -1.0f } };   // [round][0 boost, 1 zone count]
    if (p.hasBoost) {
        DumpTexture(r->activeTex[0], dir + L"fald_active_r0.f32", p.cols, p.rows, 4);
        DumpTexture(r->activeTex[1], dir + L"fald_active.f32", p.cols, p.rows, 4);
        ReadBackFloats(r->boostTex[0], boostR[0], 2);
        ReadBackFloats(r->boostTex[1], boostR[1], 2);
    }
    // starfield balancing: the five zone textures (cols x rows x 4 float32 each). fald_frame.* stays the SOURCE frame;
    // the balanced frame is not dumped (it exists only inside the passes) — fald_out.* with the option on is
    // Correct(Balance(source)), and dlc/fald/gpuemu.py reproduces Balance from fald_frame + these fields.
    if (r->starOn) {
        DumpTexture(r->starStatTex, dir + L"fald_star_stat.f32", p.cols, p.rows, 16);   // peak, speck-zone flag, sparse, solid
        DumpTexture(r->starWTex, dir + L"fald_star_w.f32", p.cols, p.rows, 16);         // target weight wt, wt ln peak, flank flag, speck-zone flag
        DumpTexture(r->starPlanTex, dir + L"fald_star_plan.f32", p.cols, p.rows, 16);   // w0_field, ln target, ln lift, ln peak
        DumpTexture(r->starPlan2Tex, dir + L"fald_star_plan2.f32", p.cols, p.rows, 16); // ln background, near, speck-zone flag, w
        DumpTexture(r->starBgTex, dir + L"fald_star_bg.f32", p.cols, p.rows, 16);       // ln background, brightest pixel's index ly * cellW + lx, lit sum, a_eff
    }
    // glow fill: round 1's zone fields (the ones the output used). vz = cols x rows float32; env = cols x rows x 4
    // float32 [Ez, Dz, Cz, Vz]. dlc/fald/gpuemu.py Emu.glow_zones reproduces them from fald_btrue.f32.
    if (r->glowOn) {
        DumpTexture(r->glowVTex, dir + L"fald_glow_vz.f32", p.cols, p.rows, 4);
        DumpTexture(r->glowEnvTex, dir + L"fald_glow_env.f32", p.cols, p.rows, 16);
        if (r->glowBand) {                                                                         // round 1's band (C16):
            DumpTexture(r->glowKTex, dir + L"fald_glow_k.f32", p.cols, p.rows, 4);                 // the FINAL k (G5)
            DumpTexture(r->glowBandTex, dir + L"fald_glow_band.f32", p.cols, p.rows, 16);          // G4: Pc, Pf, LIT flag, k0
            DumpTexture(r->glowATex, dir + L"fald_glow_bandA.f32", 2 * p.cols, p.rows, 16);        // G4: A_0..A_7 per zone
        }
    }
    UINT bpp = (ctx->swapchainFormat == DXGI_FORMAT_R16G16B16A16_FLOAT) ? 8 : 4;
    DumpTexture(r->inter, dir + (bpp == 8 ? L"fald_frame.rgba16f" : L"fald_frame.rgb10a2"), r->width, r->height, bpp);
    char clockText[3][192];                  // full-precision clock numbers (the stream's 6 digits cannot check k offline)
    snprintf(clockText[0], sizeof(clockText[0]), "%.4f\nclock_grid_ms %.6f\nclock_lock_residual %.6f\nclock_lock_gain %.6f",
             r->clkTimeMs, r->clkGridMs, r->clkResidual, r->clkGain);
    snprintf(clockText[1], sizeof(clockText[1]), "%.9g", (double)r->clkRefreshMs);
    snprintf(clockText[2], sizeof(clockText[2]), "%.9g %.9g %.9g %.9g", (double)r->clkFactor[0], (double)r->clkFactor[1],
             (double)r->clkFactor[2], (double)r->clkFactor[3]);
    std::ofstream meta(dir + L"fald_dump.txt");
    meta << "width " << r->width << "\nheight " << r->height << "\ncols " << p.cols << "\nrows " << p.rows
         << "\nsub " << p.sub << "\nframe_format " << (bpp == 8 ? "R16G16B16A16_FLOAT scRGB linear (1.0 = 80 nits)" : "R10G10B10A2_UNORM")
         << "\nmode " << (r->builtForHdr ? "HDR" : "SDR_ACM")
         << "\ntransfer " << (p.transfer == FALD_TRANSFER_GAMMA ? "gamma" : "pq") << "\nsdr_gamma " << p.sdrGamma << "\nwhite_nits " << p.white
         << "\nout_file " << (bpp == 8 ? "fald_out.rgba16f" : "fald_out.rgb10a2") << " (same format; the layer's OUTPUT, debug mode " << r->debugMode << ")"
         << "\nped_mode " << (((r->pedMode == 1) && p.hasPedColour) ? "channel" : "white")
         << "\nped_rgb " << p.pedRGB[0] << " " << p.pedRGB[1] << " " << p.pedRGB[2] << (p.hasPedColour ? " (FLD2)" : " (FLD1, white)")
         << "\nped_chroma_gain " << p.chromaGain << " fade " << ((p.chromaLo < 0.0f) ? p.lumFadeLo : p.chromaLo) << " " << ((p.chromaHi < 0.0f) ? p.lumFadeHi : p.chromaHi)
         << "\ntemporal_mode " << r->temporalMode << " (0 off, 1 both fields, 2 B_true only, 3 panel clock)"
         << "\ntau_rise_ms " << fs.tauRiseMs << "\ntau_fall_ms " << fs.tauFallMs << "\ndelay_frames " << r->delayFrames << " (ring " << r->delayCount << ")"
         << "\ntemp_alpha_rise " << r->tempAlphaRise << "\ntemp_alpha_fall " << r->tempAlphaFall << "\ndt_ms " << r->dtMs
         << "\nstate_valid " << (r->stateValid ? 1 : 0)
         << "\nclock_closure " << r->clkClosure << "\nclock_parity " << r->clkParity << " (-1 unknown: the mean of both clocks)"
         << "\nclock_seed " << (r->clkSeeded ? 1 : 0) << " (1 = this run (re-)seeded the clocks: the stateless layer, no clock files)"
         << "\nclock_time_ms " << clockText[0] << "\nclock_refresh_ms " << clockText[1]
         << " (time / grid: ms since the seeding run; grid = the centre of this run's refresh on the phase-locked grid AFTER the"
         << " update, residual in periods)\nclock_refresh_index " << r->clkIndex << "\nclock_elapsed_refreshes " << r->clkElapsed
         << " (k = round((time - the previous run's grid) / period); 0 = the same refresh: the previous run's maps were held; > " << FALD_CLOCK_MAX_REFRESHES
         << ": factors exactly 1)\nclock_factors " << clockText[2] << " (true0 est0 true1 est1)\nclock_weights " << r->clkW[0] << " " << r->clkW[1]
         << " (mode 3 files: fald_drive_filt.f32 / fald_clock_est.f32 = the maps the kernels saw, fald_clock_s0.f32 / _s1.f32 ="
         << " the clocks' LED states of this frame, fald_clock_dprev.f32 = the previous frame's round-1 drives)"
         << "\nboost_in_file " << (p.hasBoost ? 1 : 0) << "\nboost_steps " << p.boostN << "\nzones_total " << (p.cols * p.rows)
         << "\nboost_lit_nits " << p.boostLitNits << "\nboost_lit_frac " << p.boostLitFrac
         << "\nboost_dim_nits " << p.boostDimNits << "\nboost_dim_frac " << p.boostDimFrac
         << "\nboost_rule " << p.boostRule << " (0 = LIT-or-DIM, 1 = LIT-or-MEAN)\nboost_mean_gamma " << p.boostMeanGamma
         << "\nboost_mean_thresh " << p.boostMeanThresh
         << "\nactive_zones_r0 " << (int)boostR[0][1] << "\nboost_r0 " << boostR[0][0]
         << " (round 0: the source frame)\nactive_zones_r1 " << (int)boostR[1][1] << "\nboost_r1 " << boostR[1][0]
         << " (round 1: the corrected frame; this boost is in fald_btrue.f32; files fald_active_r0.f32 / fald_active.f32)"
         << "\nstarfield " << (r->starOn ? 1 : 0) << " (setting " << (fs.star.enabled ? 1 : 0)
         << "; 1 = every pass read Balance(fald_frame); files fald_star_stat.f32 [peak, speck-zone flag, sparse, solid], fald_star_w.f32"
         << " [target weight wt, wt ln peak, flank flag, speck-zone flag], fald_star_plan.f32 [w0_field, ln target, ln lift, ln peak],"
         << " fald_star_plan2.f32 [ln background, near, speck-zone flag, w], fald_star_bg.f32 [ln background, brightest pixel's"
         << " index ly * cellW + lx, lit sum, a_eff]:"
         << " cols x rows x 4 float32)"
         << "\nstarfield even " << r->star.even << " lift " << r->star.lift << " target_gain " << r->star.targetGain
         << " target_sigma " << r->star.targetSigma << " keep_nits " << r->star.keepNits
         << " even_reach " << r->star.evenReach << " cap_nits " << r->star.capNits << " strength " << r->star.strength
         << "\nstarfield area_lo " << r->star.areaLo << " area_hi " << r->star.areaHi << " peak_hi " << r->star.peakHi
         << " reach " << r->star.reach << " nb_lo " << r->star.nbLo << " nb_hi " << r->star.nbHi
         << "\nglowfill " << (r->glowOn ? 1 : 0) << " (setting " << (fs.glow.enabled ? 1 : 0)
         << "; 1 = GlowAdd after Correct in the round-1 statistic and the pixel pass; files fald_glow_vz.f32 [Vz], fald_glow_env.f32"
         << " [Ez, Dz, Cz, Vz]: cols x rows float32, round 1)"
         << "\nglowfill strength " << r->glow.strength << " reach " << r->glow.reach << " cap_nits " << r->glow.capNits
         << " req_ceil " << FaldGlowReqCeil(p) << " band " << (r->glowBand ? 1 : 0)
         << " (band 1 = the count-threshold band, round 1: fald_glow_k.f32 = the zones' final scale k [G5], fald_glow_band.f32 ="
         << " [Pc, Pf, LIT flag, k0] x 4 float32, fald_glow_bandA.f32 = the neighbour bound A_0..A_7 x 8 float32 [G4]; needs a boost"
         << " LUT + the mean zone rule)"
         << ((fs.glow.enabled && !FaldGlowSupported(p)) ? "\nglowfill refused: " : "") << ((fs.glow.enabled && !FaldGlowSupported(p)) ? FALD_GLOW_SDR_NOTE : "")
         << "\nparams " << NarrowUtf8(r->paramsPath) << "\nframes_run " << r->framesRun << "\n";
    std::cout << "[FALD] Monitor " << ctx->index << " dump written to " << NarrowUtf8(dir) << std::endl;
}

// The OUTPUT frame (what the pixel pass wrote into the real target), after the Draw. With debug
// mode 4 (identity) it must equal fald_frame.* bit for bit — the H4 check of the work guide.
static void DumpOutput(MonitorContext* ctx, FaldResources* r, ID3D11RenderTargetView* finalRT, const std::wstring& dir) {
    ID3D11Resource* res = nullptr;
    finalRT->GetResource(&res);
    if (!res) return;
    ID3D11Texture2D* tex = nullptr;
    if (SUCCEEDED(res->QueryInterface(IID_PPV_ARGS(&tex))) && tex) {
        UINT bpp = (ctx->swapchainFormat == DXGI_FORMAT_R16G16B16A16_FLOAT) ? 8 : 4;
        DumpTexture(tex, dir + (bpp == 8 ? L"fald_out.rgba16f" : L"fald_out.rgb10a2"), r->width, r->height, bpp);
        tex->Release();
    }
    res->Release();
}

void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT, bool newContent) {
    FaldResources* r = ctx ? ctx->fald : nullptr;
    if (!r || !r->valid || !finalRT) return;
    const FaldSettings& fs = ctx->isHDREnabled ? ctx->hdrColorCorrection.fald : ctx->sdrColorCorrection.fald;
    r->debugMode = fs.debugMode;
    r->pedMode = fs.pedMode;
    // Starfield balancing (work guide S1): on = the five zone textures exist; off = they are released and nothing
    // below knows the option exists (CB word 35 = 0, t15 / t18 unbound). The clamped settings always go into the CB words
    // 52-63 (read by the shaders only when word 35 is set) so a dump reports them either way.
    {
        FaldStarfieldSettings st = fs.star;
        FaldStarfieldClamp(st);
        r->star.even = st.even; r->star.lift = st.lift; r->star.targetGain = st.targetGain; r->star.capNits = st.capNits;
        r->star.strength = st.strength; r->star.areaLo = st.areaLo; r->star.areaHi = st.areaHi; r->star.peakHi = st.peakHi;
        r->star.nbLo = st.nbLo; r->star.nbHi = st.nbHi; r->star.reach = st.reach; r->star.evenReach = st.evenReach;
        r->star.targetSigma = st.targetSigma; r->star.keepNits = st.keepNits;
        if (st.enabled) r->starOn = EnsureStar(r);
        else if (r->starStatTex || r->starWTex || r->starPlanTex || r->starBgTex || r->starPlan2Tex || r->starOn) ReleaseStar(r);
    }
    // Glow fill (work guide S2): on = the four zone textures exist; off = they are released and nothing below knows the
    // option exists (CB word 75 = 0, t23 unbound). The clamped settings always go into the CB (a dump reports them).
    {
        FaldGlowSettings gs = fs.glow;
        FaldGlowClamp(gs);
        r->glow.strength = gs.strength; r->glow.capNits = gs.capNits; r->glow.reach = gs.reach;
        // HDR (PQ panel files) only — FaldGlowSupported: a gamma-transfer file never runs the fill, whatever the switch says.
        // Part of the starfield feature: it never runs without starfield balancing (same rule as the DWM hook path).
        if (gs.enabled && r->starOn && FaldGlowSupported(r->params)) r->glowOn = EnsureGlow(r);
        else if (r->glowVTex || r->glowDilTex || r->glowCTex || r->glowEnvTex || r->glowKTex || r->glowOn) ReleaseGlow(r);
        r->glowBand = r->glowOn && FaldGlowBandActive(r->params);
    }

    // Temporal drive state ("LED lag", passes 1b / 1c): the bookkeeping — dt, the mode / ring / reset rules, the panel
    // clock's time law, which map each kernel reads — is shared/fald_temporal.cpp FaldTemporalBeginRun (the DWM hook runs
    // the same). Only the D3D side is here. The panel clock's period is the monitor's CURRENT nominal refresh
    // (capture.cpp re-reads it when the duplication is re-created, i.e. on every mode change).
    FaldTemporalSettings ts;
    ts.mode = fs.temporalMode; ts.tauRiseMs = fs.tauRiseMs; ts.tauFallMs = fs.tauFallMs; ts.delayFrames = fs.delayFrames;
    ts.clockClosure = fs.clockClosure; ts.clockParity = fs.clockParity;
    // panel clock (mode 3, work guide C13): its textures exist only while the mode is on; a failed creation runs as off
    bool clockOk = false;
    if (ts.mode == FALD_TEMPORAL_PANEL) clockOk = EnsureClock(r);
    else if (r->clkStateTex[0] || r->clkStateTex[1] || r->clkPrevTex || r->clkEstTex) ReleaseClock(r);
    LARGE_INTEGER qpcNow, qpcFreq;
    QueryPerformanceCounter(&qpcNow); QueryPerformanceFrequency(&qpcFreq);
    const FaldTemporalRun trun = FaldTemporalBeginRun(r, ts, clockOk, qpcNow.QuadPart, qpcFreq.QuadPart, ctx->frameTimeExactMs);
    const bool temporal = trun.temporal, panel = trun.panel;
    const FaldClockPlan plan = trun.clock;
    auto mapSrv = [r](FaldDriveMap m) {
        return m == FALD_MAP_FILTERED ? r->driveFiltSRV : (m == FALD_MAP_CLOCK_EST ? r->clkEstSRV : r->driveSRV);
    };
    // the map the panel's pipeline is fed this frame (modes 1 / 2): a delay-ring entry or the instantaneous drive
    ID3D11ShaderResourceView* inDrive = trun.delayedSlot >= 0 ? r->delaySRV[trun.delayedSlot] : r->driveSRV;
    ID3D11ShaderResourceView* trueDrive = mapSrv(trun.trueMap);   // what the real-spread kernel sees
    ID3D11ShaderResourceView* estDrive = mapSrv(trun.estMap);     // what the estimate kernel sees

    // the main pass rendered into r->inter with finalRT unbound; make sure the RTV is off before
    // the intermediate is read as an SRV
    ID3D11RenderTargetView* nullRT = nullptr;
    g_context->OMSetRenderTargets(1, &nullRT, nullptr);

    // black-frame LED boost (panel files with a LUT): each round's boost comes from the zone flags of the frame the
    // panel receives in that round, and is NOT filtered by the temporal state (instant on the panel)
    if (r->starOn) RunStar(r);             // the plan of THIS source frame; every pass below reads Balance(source)
    if (plan.runPass) RunPanelClock(r);    // mode 3: the state of this frame comes from PAST frames only (PanelDriveState.fields)
    RunStat(r, 0);
    RunBoost(r, 0);
    if (temporal) RunTemporal(r, inDrive); // both rounds read the SAME committed state (DriveState.peek)
    RunConv(r, trueDrive, estDrive, r->boostSRV[0]);
    RunGain(r);
    if (r->glowOn) RunGlow(r, 0);          // round 0's fill: part of the frame the round-1 statistic / boost count see
    RunStat(r, 1);
    RunBoost(r, 1);
    if (temporal) RunTemporal(r, inDrive);
    RunConv(r, trueDrive, estDrive, r->boostSRV[1]);
    RunGain(r);
    if (r->glowOn) RunGlow(r, 1);          // round 1's fill: part of the output
    r->framesRun++;
    const std::wstring dumpDir = TakeDumpRequest(ctx);
    if (!dumpDir.empty()) DumpFields(ctx, r, dumpDir);   // before the commit: the state file is the map the pass read
    if (temporal) {                        // DriveState.commit: round 1's filtered map becomes the state; the ring
        g_context->CopyResource(r->driveStateTex, r->driveFiltTex);   // takes round 1's INSTANTANEOUS map (the indices:
        if (trun.delay > 0)                                           // FaldTemporalEndRun below)
            g_context->CopyResource(r->delayTex[r->delayHead], r->driveTex);
    }
    if (panel) {                           // PanelDriveState.commit: round 1's INSTANTANEOUS map is the next frame's target
        if (plan.seedStates) {             // (k = 0: it replaces the previous frame's — the later frame is the one shown);
            g_context->CopyResource(r->clkStateTex[0], r->driveTex);   // a seeding run takes the panel as settled on this
            g_context->CopyResource(r->clkStateTex[1], r->driveTex);   // frame (both clocks)
        }
        if (plan.commitPrev) g_context->CopyResource(r->clkPrevTex, r->driveTex);
    }

    // pixel pass: inter + fields -> finalRT (fullscreen triangle; g_vs already bound by the caller)
    FillCB(r, 1);
    g_context->OMSetRenderTargets(1, &finalRT, nullptr);
    g_context->PSSetShader(g_faldPS, nullptr, 0);
    BindCommon(r, false);
    ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
    g_context->PSSetShaderResources(5, 2, fields);
    g_context->PSSetShaderResources(9, 1, &r->gainBSRV);
    // debug view 7: instantaneous vs filtered (mode 3: vs the clocks' mean LED state of this frame)
    ID3D11ShaderResourceView* filt = mapSrv(trun.debugFiltMap);
    g_context->PSSetShaderResources(4, 1, &r->driveSRV);
    g_context->PSSetShaderResources(10, 1, &filt);
    g_context->PSSetShaderResources(13, 1, &r->activeSRV[1]);                     // debug view 8 (nullptr without a boost LUT)
    g_context->Draw(3, 0);
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    g_context->PSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    if (!dumpDir.empty()) DumpOutput(ctx, r, finalRT, dumpDir);

    // The commit's indices and the settle hold (render.cpp asks FaldSettlePending on an acquire timeout and re-runs the
    // layer on its own intermediate): shared/fald_temporal.cpp FaldTemporalEndRun.
    FaldTemporalEndRun(r, trun, ts, newContent);
}

bool FaldSettlePending(const MonitorContext* ctx) {
    const FaldResources* r = ctx ? ctx->fald : nullptr;
    return r && r->valid && FaldTemporalSettlePending(r);
}

void FaldLayerIdle(MonitorContext* ctx) {
    FaldResources* r = ctx ? ctx->fald : nullptr;
    FaldTemporalIdle(r);
}
