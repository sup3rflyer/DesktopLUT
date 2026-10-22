// FALD context-dependence correction — resources, passes, debug dump. See fald.h / shared/fald_shader.h.
#include "fald.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_fade.h"
#include "fald_fade_status.h"
#include "fald_core_status.h"
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
static ID3D11ComputeShader* g_faldConvCS[FALD_CONV_SO_VARIANTS] = {};   // pass 2, one per sub-offsets-per-thread variant
static ID3D11ComputeShader* g_faldGainCS = nullptr;
static ID3D11ComputeShader* g_faldBlurCS = nullptr;
static ID3D11ComputeShader* g_faldGainBlurCS = nullptr;   // passes 2b + 2c fused (fald_shader.h g_faldGainBlurSource)
static ID3D11ComputeShader* g_faldDecideCS = nullptr;     // the change-driven path's decide pass (g_faldDecideSource)
static ID3D11ComputeShader* g_faldTemporalCS = nullptr;   // pass 1b: per-cell drive state (temporal modes 1 / 2 only)
static ID3D11ComputeShader* g_faldPanelClockCS = nullptr; // pass 1c: the two parity clocks' LED states (temporal mode 3 only)
static ID3D11ComputeShader* g_faldStarStatCS = nullptr;   // starfield balancing S0: star statistic of the source frame
static ID3D11ComputeShader* g_faldStarWeightCS = nullptr; // S1: tapered protection field + zone weights
static ID3D11ComputeShader* g_faldStarPlanCS = nullptr;   // S2: target + the plan the pixels sample
// The zone sweeps' combine variants (work guide C14; fald_shader.h above ZoneSlices): run only for a lattice whose zones
// hold more than FALD_ZONE_SLICE_PX pixels, after the sliced pass, to fold its partials and finish each zone. Optional:
// a compile failure costs only the lattices that need them (Build / EnsureStar refuse those).
static ID3D11ComputeShader* g_faldStatCombineCS = nullptr;
static ID3D11ComputeShader* g_faldStarStatCombineCS = nullptr;
static ID3D11PixelShader* g_faldPS = nullptr;
static ID3D11SamplerState* g_faldSampler = nullptr;
// DesktopLUT's own windows while a view is on (shared/fald_exempt.h): the pixel pass again, scissored, the view off.
// Optional: without it a view covers the whole screen, as before.
static ID3D11RasterizerState* g_faldExemptRS = nullptr;
static std::mutex g_faldExemptMutex;
static std::vector<FaldExemptRect> g_faldExemptDesktop;   // the host's list, desktop pixels (empty = no view on)
// The fade stack's passes (shared/fald_fade.cpp) are created from their shipped bytecode the first time a monitor turns the
// flank fade on — no shader compiler on the render thread, FALD_FADE_SHADERS_PER_RUN per run of the layer — and its
// resources the same way (FALD_FADE_RESOURCES_PER_RUN / FALD_FADE_BYTES_PER_RUN per run). Until both are complete the
// layer runs without the fade (today's layer, bit for bit). A session that never uses it pays nothing. A shader failure
// is latched (logged once) until the shaders are released (device loss).
static bool g_faldFadeShadersFailed = false;
std::atomic<bool> g_faldFadeForce{ false };   // INI [General] FaldFlankFadeForce (fald.h)
std::atomic<bool> g_faldCoreForce{ false };   // INI [General] FaldCoreForce (fald.h)

// The core passes' test seam (fald.h FaldCorePassHook): null in production.
FaldCorePassHook* g_faldCorePassHook = nullptr;
static const char* const kFaldCorePassNames[FALD_PASS_COUNT] = {
    "star_s0", "star_s1", "star_s2", "fade_frame", "clock",
    "stat0", "temporal0", "conv0", "gain0", "blur_h0", "blur_v0", "copy0", "fade_reach0",
    "stat1", "temporal1", "conv1", "gain1", "blur_h1", "blur_v1", "copy1", "fade_reach1",
    "commit", "pixel", "decide_s", "decide0", "decide1" };
const char* FaldCorePassName(unsigned int pass) { return pass < FALD_PASS_COUNT ? kFaldCorePassNames[pass] : "?"; }
struct FaldPassScope {
    unsigned int p;
    explicit FaldPassScope(unsigned int pass) : p(pass) { if (g_faldCorePassHook) g_faldCorePassHook->Before(g_context, p); }
    ~FaldPassScope() { if (g_faldCorePassHook) g_faldCorePassHook->After(g_context, p); }
};

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

FaldShaderSources FaldDefaultShaderSources() {
    FaldShaderSources s;
    s.common = g_faldCommonSource; s.stat = g_faldStatSource; s.conv = g_faldConvSource; s.gain = g_faldGainSource;
    s.blur = g_faldBlurSource; s.gainBlur = g_faldGainBlurSource; s.temporal = g_faldTemporalSource; s.panelClock = g_faldPanelClockSource;
    s.starStat = g_faldStarStatSource; s.starWeight = g_faldStarWeightSource;
    s.starPlan = g_faldStarPlanSource; s.zoneCombineDefine = g_faldZoneCombineDefine; s.pixel = g_faldPixelSource;
    s.decide = g_faldDecideSource;
    return s;
}

bool InitFaldShaders() { return InitFaldShadersFrom(FaldDefaultShaderSources()); }

bool InitFaldShadersFrom(const FaldShaderSources& src) {
    if (!g_device) return false;
    ID3DBlob* b = nullptr;
    std::string common = src.common;
    if (!CompileOne(common + src.stat, "FaldStatCS", "cs_5_0", &b)) return false;
    HRESULT hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStatCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(stat) failed" << std::endl; return false; }
    for (unsigned int v = 0; v < FALD_CONV_SO_VARIANTS; v++) {   // the conv pass per variant (fald_shader.h FALD_CONV_SO)
        if (!CompileOne(std::string(g_faldConvSoDefine[v]) + common + src.conv, "FaldConvCS", "cs_5_0", &b)) return false;
        hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldConvCS[v]);
        b->Release(); b = nullptr;
        if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(conv " << FaldConvSoOf(v) << ") failed" << std::endl; return false; }
    }
    if (!CompileOne(common + src.gain, "FaldGainCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldGainCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(gain) failed" << std::endl; return false; }
    if (!CompileOne(common + src.blur, "FaldBlurCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldBlurCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(blur) failed" << std::endl; return false; }
    if (!CompileOne(common + src.gainBlur, "FaldGainBlurCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldGainBlurCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(gain+blur) failed" << std::endl; return false; }
    if (!CompileOne(common + src.decide, "FaldDecideCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldDecideCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(decide) failed" << std::endl; return false; }
    if (!CompileOne(common + src.temporal, "FaldTemporalCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldTemporalCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(temporal) failed" << std::endl; return false; }
    if (!CompileOne(common + src.panelClock, "FaldPanelClockCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldPanelClockCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(panel clock) failed" << std::endl; return false; }
    if (!CompileOne(common + src.starStat, "FaldStarStatCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStarStatCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(star stat) failed" << std::endl; return false; }
    if (!CompileOne(common + src.starWeight, "FaldStarWeightCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStarWeightCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(star weight) failed" << std::endl; return false; }
    if (!CompileOne(common + src.starPlan, "FaldStarPlanCS", "cs_5_0", &b)) return false;
    hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldStarPlanCS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(star plan) failed" << std::endl; return false; }
    {
        const std::string combine = std::string(src.zoneCombineDefine) + common;   // the same sources, FALD_ZONE_COMBINE
        struct { const char* src; const char* name; ID3D11ComputeShader** cs; } zc[2] = {
            { src.stat, "FaldStatCombineCS", &g_faldStatCombineCS },
            { src.starStat, "FaldStarStatCombineCS", &g_faldStarStatCombineCS } };
        for (auto& z : zc) {
            if (!CompileOne(combine + z.src, z.name, "cs_5_0", &b)) continue;   // logged; lattices of one-slice zones don't need it
            hr = g_device->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, z.cs);
            b->Release(); b = nullptr;
            if (FAILED(hr)) { std::cerr << "[FALD] CreateComputeShader(" << z.name << ") failed" << std::endl; *z.cs = nullptr; }
        }
    }
    if (!CompileOne(common + src.pixel, "FaldPS", "ps_5_0", &b)) return false;
    hr = g_device->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldPS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { std::cerr << "[FALD] CreatePixelShader failed" << std::endl; return false; }
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    if (FAILED(g_device->CreateSamplerState(&sd, &g_faldSampler))) { std::cerr << "[FALD] sampler failed" << std::endl; return false; }
    D3D11_RASTERIZER_DESC rd = {};
    rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE; rd.ScissorEnable = TRUE;
    if (FAILED(g_device->CreateRasterizerState(&rd, &g_faldExemptRS))) {
        g_faldExemptRS = nullptr;
        std::cerr << "[FALD] rasterizer state for the views' window exemption failed: a view covers DesktopLUT's windows too" << std::endl;
    }
    std::cout << "FALD correction shaders: compiled" << std::endl;
    return true;
}

void ReleaseFaldShaders() {
    FaldFadeReleaseShaders();
    g_faldFadeShadersFailed = false;
    if (g_faldExemptRS) { g_faldExemptRS->Release(); g_faldExemptRS = nullptr; }
    if (g_faldSampler) { g_faldSampler->Release(); g_faldSampler = nullptr; }
    if (g_faldPS) { g_faldPS->Release(); g_faldPS = nullptr; }
    if (g_faldStarStatCombineCS) { g_faldStarStatCombineCS->Release(); g_faldStarStatCombineCS = nullptr; }
    if (g_faldStatCombineCS) { g_faldStatCombineCS->Release(); g_faldStatCombineCS = nullptr; }
    if (g_faldStarPlanCS) { g_faldStarPlanCS->Release(); g_faldStarPlanCS = nullptr; }
    if (g_faldStarWeightCS) { g_faldStarWeightCS->Release(); g_faldStarWeightCS = nullptr; }
    if (g_faldStarStatCS) { g_faldStarStatCS->Release(); g_faldStarStatCS = nullptr; }
    if (g_faldPanelClockCS) { g_faldPanelClockCS->Release(); g_faldPanelClockCS = nullptr; }
    if (g_faldTemporalCS) { g_faldTemporalCS->Release(); g_faldTemporalCS = nullptr; }
    if (g_faldDecideCS) { g_faldDecideCS->Release(); g_faldDecideCS = nullptr; }
    if (g_faldGainBlurCS) { g_faldGainBlurCS->Release(); g_faldGainBlurCS = nullptr; }
    if (g_faldBlurCS) { g_faldBlurCS->Release(); g_faldBlurCS = nullptr; }
    if (g_faldGainCS) { g_faldGainCS->Release(); g_faldGainCS = nullptr; }
    for (unsigned int v = 0; v < FALD_CONV_SO_VARIANTS; v++) if (g_faldConvCS[v]) { g_faldConvCS[v]->Release(); g_faldConvCS[v] = nullptr; }
    if (g_faldStatCS) { g_faldStatCS->Release(); g_faldStatCS = nullptr; }
}

bool FaldShadersReady() {
    return g_faldStatCS && g_faldConvCS[0] && g_faldConvCS[1] && g_faldConvCS[2] && g_faldConvCS[3] && g_faldGainCS && g_faldBlurCS && g_faldGainBlurCS && g_faldDecideCS && g_faldTemporalCS && g_faldPanelClockCS &&
           g_faldStarStatCS && g_faldStarWeightCS && g_faldStarPlanCS &&
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
    r->stateComplete = false; r->fieldsStand = false;                // the star textures are gone: the next run is a full one
    r->starRetryCounter = 0;                 // option off / resources rebuilt: the next enable tries at once
}

// Panel clock textures (temporal mode 3): they exist only while the mode is on (EnsureClock / FaldRunPasses).
static void ReleaseClock(FaldResources* r) {
    for (unsigned int i = 0; i < 2; i++) { SafeRelease(r->clkStateSRV[i]); SafeRelease(r->clkStateUAV[i]); SafeRelease(r->clkStateTex[i]); }
    SafeRelease(r->clkPrevSRV); SafeRelease(r->clkPrevUAV); SafeRelease(r->clkPrevTex);
    SafeRelease(r->clkEstSRV); SafeRelease(r->clkEstUAV); SafeRelease(r->clkEstTex);
    r->stateComplete = false; r->fieldsStand = false;                // the clock textures are gone: the next run is a full one
    r->clkRetryCounter = 0;                  // mode off / resources rebuilt: the next enable tries at once
    r->clkElapsed = 0; r->clkIndex = 0; r->clkSeeded = false;
}

static void ReleaseAll(FaldResources* r) {
    SafeRelease(r->interSRV); SafeRelease(r->interRTV); SafeRelease(r->inter);
    SafeRelease(r->curveSRV); SafeRelease(r->curveTex);
    SafeRelease(r->kTrue4SRV); SafeRelease(r->kTrueSRV); SafeRelease(r->kTrueBuf);
    SafeRelease(r->kEst4SRV); SafeRelease(r->kEstSRV); SafeRelease(r->kEstBuf);
    SafeRelease(r->driveSRV); SafeRelease(r->driveUAV); SafeRelease(r->driveTex);
    SafeRelease(r->driveFiltSRV); SafeRelease(r->driveFiltUAV); SafeRelease(r->driveFiltTex);
    SafeRelease(r->driveStateSRV); SafeRelease(r->driveStateUAV); SafeRelease(r->driveStateTex);
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++) { SafeRelease(r->delaySRV[i]); SafeRelease(r->delayUAV[i]); SafeRelease(r->delayTex[i]); }
    r->delayHead = 0; r->delayCount = 0;
    r->stateValid = false; r->settleLeft = 0;
    SafeRelease(r->bTrueSRV); SafeRelease(r->bTrueUAV); SafeRelease(r->bTrueTex);
    SafeRelease(r->bEstSRV); SafeRelease(r->bEstUAV); SafeRelease(r->bEstTex);
    SafeRelease(r->fieldsSRV); SafeRelease(r->fieldsUAV); SafeRelease(r->fieldsTex);
    SafeRelease(r->gainASRV); SafeRelease(r->gainAUAV); SafeRelease(r->gainATex);
    SafeRelease(r->gainBSRV); SafeRelease(r->gainBUAV); SafeRelease(r->gainBTex);
    SafeRelease(r->flatTrueSRV); SafeRelease(r->flatTrueUAV); SafeRelease(r->flatTrueTex);
    SafeRelease(r->flatEstSRV); SafeRelease(r->flatEstUAV); SafeRelease(r->flatEstTex);
    for (unsigned int i = 0; i < 2; i++) {
        SafeRelease(r->activeSRV[i]); SafeRelease(r->activeUAV[i]); SafeRelease(r->activeTex[i]);
        SafeRelease(r->boostSRV[i]); SafeRelease(r->boostUAV[i]); SafeRelease(r->boostTex[i]);
    }
    SafeRelease(r->boostLutSRV); SafeRelease(r->boostLutBuf);
    SafeRelease(r->boostCountUAV); SafeRelease(r->boostCountBuf);
    SafeRelease(r->drive0SRV); SafeRelease(r->drive0UAV); SafeRelease(r->drive0Tex);
    SafeRelease(r->driveFilt0SRV); SafeRelease(r->driveFilt0UAV); SafeRelease(r->driveFilt0Tex);
    SafeRelease(r->bTrue0SRV); SafeRelease(r->bTrue0UAV); SafeRelease(r->bTrue0Tex);
    SafeRelease(r->bEst0SRV); SafeRelease(r->bEst0UAV); SafeRelease(r->bEst0Tex);
    SafeRelease(r->fields0SRV); SafeRelease(r->fields0UAV); SafeRelease(r->fields0Tex);
    SafeRelease(r->gain0SRV); SafeRelease(r->gain0UAV); SafeRelease(r->gain0Tex);
    SafeRelease(r->zoneListSRV); SafeRelease(r->zoneListBuf);
    SafeRelease(r->changeFlagsUAV); SafeRelease(r->changeFlagsBuf);
    SafeRelease(r->argsUAV); SafeRelease(r->argsBuf);
    r->stateComplete = false; r->fieldsStand = false; r->lastSettingsKey = 0; r->lastFrameKey = 0; r->lastPartial = false; r->runMode = 0;
    FaldCoreTimingRelease(&r->coreTiming);
    SafeRelease(r->zonePartUAV); SafeRelease(r->zonePartBuf); r->zoneSlices = 1;
    ReleaseStar(r);
    ReleaseClock(r);
    FaldFadeDestroy(r->fade); r->fade = nullptr; r->fadeOn = false; r->fadeKey = 0; r->fadeRetryCounter = 0;
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

// The zone sweeps' slice partials (fald_shader.h ZonePart): cols * rows * slices records, UAV only.
static bool MakeZonePartBuffer(UINT count, ID3D11Buffer** buf, ID3D11UnorderedAccessView** uav) {
    const UINT stride = FALD_ZONE_PART_BYTES;
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
        r->stateComplete = false; r->fieldsStand = false;            // fresh (uninitialised) star textures: the next run is a full one
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
        r->stateComplete = false; r->fieldsStand = false;            // fresh clock textures: the next run is a full one
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

// The statistic pass's counters (fald_shader.h boostCount): `count` uints, zeroed at creation, UAV only.
static bool MakeCounterBuffer(UINT count, ID3D11Buffer** buf, ID3D11UnorderedAccessView** uav) {
    std::vector<uint32_t> zero(count, 0u);
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = count * 4u;
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    bd.StructureByteStride = 4u;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = zero.data();
    if (FAILED(g_device->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_UNKNOWN;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = count;
    return SUCCEEDED(g_device->CreateUnorderedAccessView(*buf, &ud, uav));
}

// The touched-zone list (fald_shader.h zoneList, t38): a dynamic buffer of `count` uints the host maps per run.
static bool MakeZoneList(UINT count, ID3D11Buffer** buf, ID3D11ShaderResourceView** srv) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = count * 4u;
    bd.Usage = D3D11_USAGE_DYNAMIC;
    bd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&bd, nullptr, buf))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32_UINT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = count;
    return SUCCEEDED(g_device->CreateShaderResourceView(*buf, &sd, srv));
}

// The decide pass's indirect arguments (fald_change.h FALD_ARGS_*): a raw buffer the GPU writes and DispatchIndirect /
// DrawInstancedIndirect read.
static bool MakeArgsBuffer(ID3D11Buffer** buf, ID3D11UnorderedAccessView** uav) {
    std::vector<uint32_t> zero(FALD_ARGS_BYTES / 4u, 0u);
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = FALD_ARGS_BYTES;
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_DRAWINDIRECT_ARGS | D3D11_RESOURCE_MISC_BUFFER_ALLOW_RAW_VIEWS;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = zero.data();
    if (FAILED(g_device->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R32_TYPELESS;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = FALD_ARGS_BYTES / 4u;
    ud.Buffer.Flags = D3D11_BUFFER_UAV_FLAG_RAW;
    return SUCCEEDED(g_device->CreateUnorderedAccessView(*buf, &ud, uav));
}

// A float4 view of a float buffer whose size is a multiple of 4 floats (the conv pass's kernel reads, t36 / t37).
static bool MakeFloat4View(ID3D11Buffer* buf, size_t floats, ID3D11ShaderResourceView** srv) {
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = (UINT)(floats / 4);
    return SUCCEEDED(g_device->CreateShaderResourceView(buf, &sd, srv));
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
        r->lastError = "panel lattice (" + std::to_string(p.latW) + "x" + std::to_string(p.latH) +
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
    // the conv pass's kernel tables, laid out for its variant (shared/fald_panel.h FaldConvTableLayout)
    r->convVariant = FaldConvVariant(p.cols, p.rows, p.sub);
    {
        const unsigned int S = FaldConvSoOf(r->convVariant);
        const std::vector<float> kT = FaldConvTableLayout(p.kTrue, p.sub, 2 * p.reachTrueC + 1, 2 * p.reachTrueR + 1, S);
        const std::vector<float> kE = FaldConvTableLayout(p.kEst, p.sub, 2 * p.reachEstC + 1, 2 * p.reachEstR + 1, S);
        if (!MakeFloatBuffer(kT, &r->kTrueBuf, &r->kTrueSRV) || !MakeFloat4View(r->kTrueBuf, kT.size(), &r->kTrue4SRV)) { r->lastError = "kTrue buffer"; return false; }
        if (!MakeFloatBuffer(kE, &r->kEstBuf, &r->kEstSRV) || !MakeFloat4View(r->kEstBuf, kE.size(), &r->kEst4SRV)) { r->lastError = "kEst buffer"; return false; }
    }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveTex, &r->driveUAV, &r->driveSRV)) { r->lastError = "drive texture"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveFiltTex, &r->driveFiltUAV, &r->driveFiltSRV)) { r->lastError = "filtered drive texture"; return false; }
    // round 0's own maps (the change-driven path compares them run to run; round 1's are the textures above)
    if (!MakeRWTexture(p.cols, p.rows, &r->drive0Tex, &r->drive0UAV, &r->drive0SRV)) { r->lastError = "drive texture (round 0)"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveFilt0Tex, &r->driveFilt0UAV, &r->driveFilt0SRV)) { r->lastError = "filtered drive texture (round 0)"; return false; }
    if (!MakeRWTexture(p.cols, p.rows, &r->driveStateTex, &r->driveStateUAV, &r->driveStateSRV)) { r->lastError = "drive state texture"; return false; }
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++)
        if (!MakeRWTexture(p.cols, p.rows, &r->delayTex[i], &r->delayUAV[i], &r->delaySRV[i])) { r->lastError = "delay ring texture"; return false; }
    r->delayHead = 0; r->delayCount = 0; r->delayFrames = 0;
    r->stateValid = false; r->settleLeft = 0; r->temporalMode = FALD_TEMPORAL_OFF;
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bTrue0Tex, &r->bTrue0UAV, &r->bTrue0SRV)) { r->lastError = "B_true texture (round 0)"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bEst0Tex, &r->bEst0UAV, &r->bEst0SRV)) { r->lastError = "B_est texture (round 0)"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->fields0Tex, &r->fields0UAV, &r->fields0SRV, DXGI_FORMAT_R32G32B32A32_FLOAT)) { r->lastError = "packed fields texture (round 0)"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gain0Tex, &r->gain0UAV, &r->gain0SRV, DXGI_FORMAT_R32G32_FLOAT)) { r->lastError = "gain texture (round 0)"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bTrueTex, &r->bTrueUAV, &r->bTrueSRV)) { r->lastError = "B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->bEstTex, &r->bEstUAV, &r->bEstSRV)) { r->lastError = "B_est texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->fieldsTex, &r->fieldsUAV, &r->fieldsSRV, DXGI_FORMAT_R32G32B32A32_FLOAT)) { r->lastError = "packed fields texture"; return false; }
    // R32G32F: (gain, the flat-normalised B_est of the soft knee's ceiling), low-passed together (C15)
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainATex, &r->gainAUAV, &r->gainASRV, DXGI_FORMAT_R32G32_FLOAT)) { r->lastError = "gain texture A"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->gainBTex, &r->gainBUAV, &r->gainBSRV, DXGI_FORMAT_R32G32_FLOAT)) { r->lastError = "gain texture B"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatTrueTex, &r->flatTrueUAV, &r->flatTrueSRV)) { r->lastError = "flat B_true texture"; return false; }
    if (!MakeRWTexture(p.cols * p.sub, p.rows * p.sub, &r->flatEstTex, &r->flatEstUAV, &r->flatEstSRV)) { r->lastError = "flat B_est texture"; return false; }
    r->gainFused = FaldGainBlurFused(p.gainSmoothCells, p.sub) && g_faldGainFusedOverride != 0;   // passes 2b + 2c in one dispatch
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
        if (!MakeCounterBuffer(2, &r->boostCountBuf, &r->boostCountUAV)) { r->lastError = "boost counters"; return false; }
        const float zero[4] = { 0.0f, 0.0f, 0.0f, 0.0f };   // the flag maps start all zero: the counters follow them by deltas
        g_context->ClearUnorderedAccessViewFloat(r->activeUAV[0], zero);
        g_context->ClearUnorderedAccessViewFloat(r->activeUAV[1], zero);
    }
    // the change-driven path: the touched-zone list, the change flags, the decide pass's indirect arguments
    if (!MakeZoneList(p.cols * p.rows, &r->zoneListBuf, &r->zoneListSRV)) { r->lastError = "zone list"; return false; }
    if (!MakeCounterBuffer(3, &r->changeFlagsBuf, &r->changeFlagsUAV)) { r->lastError = "change flags"; return false; }
    if (!MakeArgsBuffer(&r->argsBuf, &r->argsUAV)) { r->lastError = "indirect arguments"; return false; }
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = FALD_CB_BYTES;   // 80 words, see FaldCB
    cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&cbd, nullptr, &r->cb))) { r->lastError = "constant buffer"; return false; }
    r->valid = true;
    r->lastError.clear();
    ComputeFlatResponse(r);
    FaldCoreTimingCreate(&r->coreTiming, g_device);   // the rate-cap governor's GPU timing (without it the layer is never capped)
    std::cout << "[FALD] Monitor " << ctx->index << " resources ready: " << p.cols << "x" << p.rows << " cells on a "
              << p.latW << "x" << p.latH << "-px lattice (" << (FaldLatticeFractional(p) ? "fractional pitch, " : "")
              << "zones up to " << p.cellW << "x" << p.cellH << " px), sub " << p.sub << ", white " << p.white << " nits, transfer "
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
// boostOn = false: the flat-lattice normalisation pass (a boost-free conv whatever the file says). view >= 0 replaces
// the debug view (word 24): the exempt draws (DrawExempt) write 0 there.
static void FillCB(FaldResources* r, uint32_t roundIdx, uint32_t blurDir = 0, bool boostOn = true, int view = -1) {
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
    u[24] = view < 0 ? r->debugMode : (uint32_t)view; u[25] = p.originX; u[26] = p.originY; u[27] = blurDir;
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
    u[75] = r->fadeOn ? FALD_FADE_FLAG_FLANK : 0u;                                  // fadeFlags: the fade stack (bit 1 = L, reserved)
    // the zone lattice's extent (words 76/77; cols * cellW / rows * cellH for an integer pitch): the zones' own spans
    u[76] = p.latW; u[77] = p.latH; u[78] = 0u; u[79] = r->runMode;                 // (docs/fald-fractional-pitch.md; word 79:
                                                                                    // the run's path, fald_change.h FaldRunModeWord)
    g_context->Unmap(r->cb, 0);
}

static const UINT FALD_SRV_SLOTS = 24;   // t0..t23 (fald_shader.h; t20..t23 = the fade stack's views: bound by RunStat(1) and
                                         // the pixel pass while it is on, null elsewhere)
static const UINT FALD_SRV_SLOTS_ALL = 40;   // ... + the fade passes' t24..t35, the conv pass's float4 kernel views t36 / t37,
                                             // the touched-zone list t38, the packed fields t39

static void BindCommon(FaldResources* r, bool compute) {
    ID3D11ShaderResourceView* srvs[FALD_SRV_SLOTS] = { r->interSRV, r->curveSRV, r->kTrueSRV, r->kEstSRV, nullptr, nullptr, nullptr,
                                                       r->flatTrueSRV, r->flatEstSRV, nullptr, nullptr, nullptr,
                                                       r->boostLutSRV, nullptr, nullptr,     // t12: nullptr without a boost LUT
                                                       r->starOn ? r->starPlanSRV : nullptr, // t15: the starfield plan (Balance)
                                                       nullptr, nullptr,                     // t16/t17: star passes only (RunStar)
                                                       r->starOn ? r->starPlan2SRV : nullptr, // t18: ln background, near, spk (Balance)
                                                       nullptr };                            // t19: star pass S1 only (RunStar)
    if (compute) {
        g_context->CSSetConstantBuffers(0, 1, &r->cb);
        g_context->CSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_context->CSSetShaderResources(38, 1, &r->zoneListSRV);   // t38: the touched-zone list (the partial path's sweeps)
        g_context->CSSetSamplers(0, 1, &g_faldSampler);
    } else {
        g_context->PSSetConstantBuffers(0, 1, &r->cb);
        g_context->PSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_context->PSSetSamplers(0, 1, &g_faldSampler);
    }
}

static void UnbindCompute() {
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS_ALL] = {};
    ID3D11UnorderedAccessView* nullUav[7] = {};             // u0 / u1 + u2, the zone sweeps' partials, u3 / u4 the boost's
                                                            // counters + result, u5 the change flags, u6 the indirect arguments
    g_context->CSSetShaderResources(0, FALD_SRV_SLOTS_ALL, nullSrv);
    g_context->CSSetUnorderedAccessViews(0, 7, nullUav, nullptr);
    g_context->CSSetShader(nullptr, nullptr, 0);
}

// How a statistic sweep is dispatched (the change-driven path, shared/fald_change.h): the full lattice by the host, the
// touched zones by the host, or either by the decide pass's indirect arguments (slot = the sweep's, combine = its
// combine's for lattices of more than one slice).
struct StatDispatch { bool partial; bool indirect; unsigned int slot, combine; };
static const StatDispatch kStatFull = { false, false, 0, 0 };
static const StatDispatch kStatPartial = { true, false, 0, 0 };

static void RunStat(FaldResources* r, uint32_t roundIdx, StatDispatch d = kStatFull) {
    const FaldPanelParams& p = r->params;
    r->runMode = (r->runMode & ~0xFFu) | (d.partial ? 1u : 0u);   // the sweep's zones: the group's or the list's
    FillCB(r, roundIdx);
    g_context->CSSetShader(g_faldStatCS, nullptr, 0);
    BindCommon(r, true);
    if (roundIdx == 1) {                                          // round 0's fields and smoothed (gain, ceiling B_est) (C15)
        ID3D11ShaderResourceView* fields[2] = { r->bTrue0SRV, r->bEst0SRV };
        g_context->CSSetShaderResources(5, 2, fields);
        g_context->CSSetShaderResources(FALD_FIELDS_SLOT, 1, &r->fields0SRV);   // t39: the same packed (SampleFields)
        ID3D11ShaderResourceView* gain0 = r->gainFused ? r->gain0SRV : r->gainBSRV;   // (the three-pass path leaves it in gainB)
        g_context->CSSetShaderResources(9, 1, &gain0);
        if (r->fadeOn) {                                          // FadePost on round 0's request: fade, F's opening, reach 0, s - f_q
            ID3D11ShaderResourceView* fv[FALD_FADE_POST_VIEWS];
            FaldFadePostViews(r->fade, 0, fv);
            g_context->CSSetShaderResources(FALD_FADE_POST_SLOT, FALD_FADE_POST_VIEWS, fv);
        }
    }
    ID3D11UnorderedAccessView* uavs[6] = { roundIdx ? r->driveUAV : r->drive0UAV, r->activeUAV[roundIdx & 1u],   // u1: nullptr without a boost LUT
                                           r->zonePartUAV,                                // (the shader then never writes it)
                                           r->boostCountUAV, nullptr,                     // u3: the boost's counters (LUT only); u2: partials
                                           r->changeFlagsUAV };                           // u5: the partial path's flags
    g_context->CSSetUnorderedAccessViews(0, 6, uavs, nullptr);
    {
        FaldPassScope ps(roundIdx ? FALD_PASS_STAT1 : FALD_PASS_STAT0);
        if (d.indirect) g_context->DispatchIndirect(r->argsBuf, d.slot);
        else if (d.partial) g_context->Dispatch((UINT)r->touched.size(), 1, r->zoneSlices);
        else g_context->Dispatch(p.cols, p.rows, r->zoneSlices);
        if (r->zoneSlices > 1) {                                                         // fold the slices, finish the zones
            g_context->CSSetShader(g_faldStatCombineCS, nullptr, 0);
            if (d.indirect) g_context->DispatchIndirect(r->argsBuf, d.combine);
            else if (d.partial) g_context->Dispatch((UINT)r->touched.size(), 1, 1);
            else g_context->Dispatch(p.cols, p.rows, 1);
        }
    }
    UnbindCompute();
}

// The decide pass (fald_shader.h g_faldDecideSource): after S0 (which = 2), after round 0's sweep (0) and after round 1's
// (1). It finishes the round's boost from the persistent count into boostTex[round] on every path; on the partial path
// it also writes the indirect arguments of what follows from the change flags.
static void RunDecide(FaldResources* r, uint32_t which) {
    FillCB(r, which);
    g_context->CSSetShader(g_faldDecideCS, nullptr, 0);
    BindCommon(r, true);
    ID3D11UnorderedAccessView* uavs[7] = { nullptr, nullptr, nullptr, r->boostCountUAV, r->boostUAV[which & 1u], r->changeFlagsUAV, r->argsUAV };
    g_context->CSSetUnorderedAccessViews(0, 7, uavs, nullptr);
    { FaldPassScope ps(which == 2 ? FALD_PASS_DECIDE_S : (which ? FALD_PASS_DECIDE1 : FALD_PASS_DECIDE0)); g_context->Dispatch(1, 1, 1); }
    UnbindCompute();
}

// Starfield balancing (option on only): S0 star statistic of the SOURCE frame -> S1 tapered protection + zone weights
// -> S2 target + plan; every later pass samples plan (t15) and plan2 (t18). Recomputed on EVERY run of the layer — new
// frame, C2 re-process of the cached frame and temporal settle frame alike: the fields are a pure function of the
// source frame in r->inter, which all three paths leave valid before FaldRunPasses, so there is no state to keep in
// step with the content. The passes bind only what they read (never BindCommon: that would bind plan / plan2 as SRVs
// while S1 / S2 write them).
// S0 over the lattice (full) or the touched zones (partial: compared, a moved zone flags the starfield).
static void RunStarS0(FaldResources* r, bool partial) {
    const FaldPanelParams& p = r->params;
    r->runMode = (r->runMode & ~0xFFu) | (partial ? 1u : 0u);
    FillCB(r, 0);
    ID3D11ShaderResourceView* in2[2] = { r->interSRV, r->curveSRV };
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetSamplers(0, 1, &g_faldSampler);
    g_context->CSSetShaderResources(38, 1, &r->zoneListSRV);
    // S0: per zone stat = (peak, speck-zone flag, sparse, solid) + bg = (ln b, brightest pixel's index, lit sum, a_eff)
    ID3D11UnorderedAccessView* out0[6] = { r->starStatUAV, r->starBgUAV, r->zonePartUAV, nullptr, nullptr, r->changeFlagsUAV };   // u2: zones > one slice
    g_context->CSSetShader(g_faldStarStatCS, nullptr, 0);
    g_context->CSSetShaderResources(0, 2, in2);
    g_context->CSSetUnorderedAccessViews(0, 6, out0, nullptr);
    {
        FaldPassScope ps(FALD_PASS_STAR_S0);
        if (partial) g_context->Dispatch((UINT)r->touched.size(), 1, r->zoneSlices);
        else g_context->Dispatch(p.cols, p.rows, r->zoneSlices);
        if (r->zoneSlices > 1) {
            g_context->CSSetShader(g_faldStarStatCombineCS, nullptr, 0);
            if (partial) g_context->Dispatch((UINT)r->touched.size(), 1, 1);
            else g_context->Dispatch(p.cols, p.rows, 1);
        }
    }
    UnbindCompute();
}

// S1 + S2, the plan passes: by the host, or by the decide pass's arguments (the partial path: only when S0 moved).
static void RunStarPlan(FaldResources* r, bool indirect) {
    const FaldPanelParams& p = r->params;
    FillCB(r, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetSamplers(0, 1, &g_faldSampler);
    // S1: the tapered protection field + flank test + zone weights -> (wt, wt ln peak, flank, spk) and (ln background, near, spk, w)
    ID3D11UnorderedAccessView* out1[2] = { r->starWUAV, r->starPlan2UAV };
    g_context->CSSetShader(g_faldStarWeightCS, nullptr, 0);
    g_context->CSSetShaderResources(16, 1, &r->starStatSRV);
    g_context->CSSetShaderResources(19, 1, &r->starBgSRV);
    g_context->CSSetUnorderedAccessViews(0, 2, out1, nullptr);
    {
        FaldPassScope ps(FALD_PASS_STAR_S1);
        if (indirect) g_context->DispatchIndirect(r->argsBuf, FALD_ARGS_STAR_S1);
        else g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    }
    UnbindCompute();
    // S2: (w0_field, ln target, ln lift, ln peak)
    ID3D11ShaderResourceView* in3[2] = { r->starStatSRV, r->starWSRV };
    g_context->CSSetShader(g_faldStarPlanCS, nullptr, 0);
    g_context->CSSetConstantBuffers(0, 1, &r->cb);
    g_context->CSSetShaderResources(16, 2, in3);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->starPlanUAV, nullptr);
    {
        FaldPassScope ps(FALD_PASS_STAR_S2);
        if (indirect) g_context->DispatchIndirect(r->argsBuf, FALD_ARGS_STAR_S2);
        else g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    }
    UnbindCompute();
}

static void RunStar(FaldResources* r) { RunStarS0(r, false); RunStarPlan(r, false); }

// Pass 1b (temporal mode): filtered drive = state + a * (drive - state) per cell, from the drive map the panel's
// pipeline is fed (t4: this round's instantaneous drive, or the ring entry delayFrames frames back) and the state
// committed after the previous frame (t11); with no valid state the drive is copied.
static void RunTemporal(FaldResources* r, ID3D11ShaderResourceView* inDrive, uint32_t roundIdx) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldTemporalCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &inDrive);
    g_context->CSSetShaderResources(11, 1, &r->driveStateSRV);
    ID3D11UnorderedAccessView* uavs[6] = { roundIdx ? r->driveFiltUAV : r->driveFilt0UAV, nullptr, nullptr, nullptr, nullptr, r->changeFlagsUAV };
    g_context->CSSetUnorderedAccessViews(0, 6, uavs, nullptr);
    { FaldPassScope ps(roundIdx ? FALD_PASS_TEMPORAL1 : FALD_PASS_TEMPORAL0); g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1); }
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
    // u0 = the map both rounds' real-spread kernel sees (round 0's filtered slot; round 1 reads the same view)
    ID3D11UnorderedAccessView* uavs[6] = { r->driveFilt0UAV, r->clkEstUAV, r->clkStateUAV[0], r->clkStateUAV[1], nullptr, r->changeFlagsUAV };
    g_context->CSSetUnorderedAccessViews(0, 6, uavs, nullptr);
    { FaldPassScope ps(FALD_PASS_CLOCK); g_context->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1); }
    UnbindCompute();
}

// trueDrive / estDrive: the drive maps the real-spread and the estimate kernels see (both the instantaneous drive
// unless a temporal mode routes the filtered one). boost: this round's 2x1 boost texture (B_true only; nullptr =
// none — no LUT in the file, or the flat-lattice pass, where the CB's boostN is 0 and the shader never reads t14).
// indirect: the decide pass's arguments (the partial path) instead of the host's dispatch size.
static void RunConv(FaldResources* r, ID3D11ShaderResourceView* trueDrive, ID3D11ShaderResourceView* estDrive,
                    ID3D11ShaderResourceView* boost, int roundIdx = -1, bool indirect = false) {
    const FaldPanelParams& p = r->params;
    g_context->CSSetShader(g_faldConvCS[r->convVariant], nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(4, 1, &trueDrive);
    g_context->CSSetShaderResources(10, 1, &estDrive);
    g_context->CSSetShaderResources(14, 1, &boost);
    ID3D11ShaderResourceView* k4[2] = { r->kTrue4SRV, r->kEst4SRV };   // the tables four floats wide (t36 / t37)
    g_context->CSSetShaderResources(36, 2, k4);
    // round 0 writes its own fields (the round-1 sweep and the change-driven path compare / read them later)
    ID3D11UnorderedAccessView* uavs[3] = { roundIdx == 0 ? r->bTrue0UAV : r->bTrueUAV, roundIdx == 0 ? r->bEst0UAV : r->bEstUAV,
                                           roundIdx == 0 ? r->fields0UAV : r->fieldsUAV };   // u2: the two packed with their flat response
    g_context->CSSetUnorderedAccessViews(0, 3, uavs, nullptr);
    {
        // roundIdx < 0 = the flat response at build (no pass of a run: the hook sees FALD_PASS_COUNT and ignores it)
        FaldPassScope ps(roundIdx < 0 ? FALD_PASS_COUNT : (roundIdx ? FALD_PASS_CONV1 : FALD_PASS_CONV0));
        // cells x sub-offset groups (fald_shader.h: FALD_CONV_SO sub-offsets per thread)
        if (indirect) g_context->DispatchIndirect(r->argsBuf, roundIdx ? FALD_ARGS_CONV1 : FALD_ARGS_CONV0);
        else g_context->Dispatch(FaldConvGroupsX(p.cols, p.rows), FaldConvGroups(p.sub, FaldConvSoOf(r->convVariant)), 1);
    }
    UnbindCompute();
}

// Pass 2b/2c: (gain, the knee's ceiling B_est) on the fine grid, then a separable Gaussian low-pass of both (A -> B -> A ... final in gainB).
static void RunGain(FaldResources* r, uint32_t roundIdx, bool indirect = false) {
    const FaldPanelParams& p = r->params;
    UINT gx = (p.cols * p.sub + 15) / 16, gy = (p.rows * p.sub + 15) / 16;
    ID3D11ShaderResourceView* fields[2] = { roundIdx ? r->bTrueSRV : r->bTrue0SRV, roundIdx ? r->bEstSRV : r->bEst0SRV };   // the round's own
    ID3D11ShaderResourceView* packed = roundIdx ? r->fieldsSRV : r->fields0SRV;                                             // t39: the same packed
    if (r->gainFused) {                    // one dispatch: gain + both blurs straight into the round's map (fald_shader.h)
        g_context->CSSetShader(g_faldGainBlurCS, nullptr, 0);
        BindCommon(r, true);
        g_context->CSSetShaderResources(5, 2, fields);
        g_context->CSSetShaderResources(FALD_FIELDS_SLOT, 1, &packed);
        ID3D11UnorderedAccessView* out = roundIdx ? r->gainBUAV : r->gain0UAV;
        g_context->CSSetUnorderedAccessViews(0, 1, &out, nullptr);
        {
            FaldPassScope ps(roundIdx ? FALD_PASS_GAIN1 : FALD_PASS_GAIN0);
            if (indirect) g_context->DispatchIndirect(r->argsBuf, roundIdx ? FALD_ARGS_GAIN1 : FALD_ARGS_GAIN0);
            else g_context->Dispatch(gx, gy, 1);
        }
        UnbindCompute();
        return;
    }
    g_context->CSSetShader(g_faldGainCS, nullptr, 0);   // (the three-pass path is never indirect: the partial path needs the fused one)
    BindCommon(r, true);
    g_context->CSSetShaderResources(5, 2, fields);
    g_context->CSSetShaderResources(FALD_FIELDS_SLOT, 1, &packed);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainAUAV, nullptr);
    { FaldPassScope ps(roundIdx ? FALD_PASS_GAIN1 : FALD_PASS_GAIN0); g_context->Dispatch(gx, gy, 1); }
    UnbindCompute();
    // horizontal: A -> B
    FillCB(r, 1, 0);
    g_context->CSSetShader(g_faldBlurCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(9, 1, &r->gainASRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainBUAV, nullptr);
    { FaldPassScope ps(roundIdx ? FALD_PASS_BLURH1 : FALD_PASS_BLURH0); g_context->Dispatch(gx, gy, 1); }
    UnbindCompute();
    // vertical: B -> A
    FillCB(r, 1, 1);
    g_context->CSSetShader(g_faldBlurCS, nullptr, 0);
    BindCommon(r, true);
    g_context->CSSetShaderResources(9, 1, &r->gainBSRV);
    g_context->CSSetUnorderedAccessViews(0, 1, &r->gainAUAV, nullptr);
    { FaldPassScope ps(roundIdx ? FALD_PASS_BLURV1 : FALD_PASS_BLURV0); g_context->Dispatch(gx, gy, 1); }
    UnbindCompute();
    // final smoothed (gain, ceiling B_est) lives in A; copy to B so consumers always read gainB
    { FaldPassScope ps(roundIdx ? FALD_PASS_COPY1 : FALD_PASS_COPY0); g_context->CopyResource(r->gainBTex, r->gainATex); }
}

// ---------------------------------------------------------------------------------------------
// The frozen fade stack (FaldSettings::flankFade; shared/fald_fade.h — the same module the DWM hook runs)
// ---------------------------------------------------------------------------------------------
// The common views the fade passes that read the frame / a round's fields need (BindCommon's t0..t19; for a round's
// reach also t5 / t6 = its fields, t9 = its smoothed gain).
static FaldFadeCommon FadeCommon(FaldResources* r, bool roundFields, uint32_t round = 1) {
    FaldFadeCommon c;
    c.faldCB = r->cb;
    c.sampler = g_faldSampler;
    ID3D11ShaderResourceView* v[20] = { r->interSRV, r->curveSRV, r->kTrueSRV, r->kEstSRV, nullptr, nullptr, nullptr,
                                        r->flatTrueSRV, r->flatEstSRV, nullptr, nullptr, nullptr, r->boostLutSRV, nullptr, nullptr,
                                        r->starOn ? r->starPlanSRV : nullptr, nullptr, nullptr,
                                        r->starOn ? r->starPlan2SRV : nullptr, nullptr };
    for (int i = 0; i < 20; i++) c.srv[i] = v[i];
    if (roundFields) {                     // the round's own fields and smoothed gain
        c.srv[5] = round ? r->bTrueSRV : r->bTrue0SRV; c.srv[6] = round ? r->bEstSRV : r->bEst0SRV;
        c.fields = round ? r->fieldsSRV : r->fields0SRV;                          // t39: the same packed (SampleFields)
        c.srv[9] = (round || !r->gainFused) ? r->gainBSRV : r->gain0SRV;
    }
    return c;
}

// The fade's resources for this frame size: created on the first run the setting is on (the shaders too), released when
// it goes off. A failed creation runs the layer without it and is retried every ~300 runs (as EnsureStar).
static bool EnsureFade(FaldResources* r) {
    if (!FaldFadeShadersReady()) {
        if (g_faldFadeShadersFailed) return false;
        std::string err;
        const FaldFadeStatus st = FaldFadeShadersStep(g_device, g_context, FALD_FADE_SHADERS_PER_RUN, &err);
        if (st == FaldFadeStatus::Failed) {
            g_faldFadeShadersFailed = true;
            std::cerr << "[FALD] fade stack shaders could not be created: " << err << " - the flank fade stays off" << std::endl;
            return false;
        }
        if (st == FaldFadeStatus::Pending) return false;   // the rest on the next runs; the layer runs without the fade
        std::cout << "FALD fade stack shaders: created" << std::endl;
    }
    if (!r->fade) r->fade = FaldFadeCreate();
    if (!r->fade) return false;
    if (r->fadeRetryCounter != 0 && (r->fadeRetryCounter++ % 300) != 0) return false;   // after a failure: every ~300 runs
    const bool wasBuilt = FaldFadeBuilt(r->fade);
    const FaldFadeStatus st = FaldFadeEnsureStep(r->fade, g_device, (unsigned int)r->width, (unsigned int)r->height,
                                                 FALD_FADE_RESOURCES_PER_RUN, FALD_FADE_BYTES_PER_RUN, g_context);
    if (st == FaldFadeStatus::Ready) {
        r->fadeRetryCounter = 0;
        r->fadeFailLogged = false;
        if (!wasBuilt) std::cout << "[FALD] fade stack resources: " << (FaldFadeVramBytes(r->fade) >> 20) << " MB" << std::endl;
        return true;
    }
    if (st == FaldFadeStatus::Pending) return false;      // paced build: the layer runs without the fade meanwhile
    r->fadeRetryCounter = 1;
    if (!r->fadeFailLogged) {
        std::cerr << "[FALD] fade stack textures could not be created: the flank fade stays off" << std::endl;
        r->fadeFailLogged = true;
    }
    return false;
}

// The settings the cached frame part depends on (the frame the layer works on): the starfield and its parameters.
static unsigned long long FadeInputKey(const FaldResources* r) {
    unsigned long long h = 1469598103934665603ull;
    auto mix = [&h](const void* p, size_t n) { const unsigned char* b = (const unsigned char*)p; for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; } };
    const unsigned int on = r->starOn ? 1u : 0u;
    mix(&on, sizeof(on));
    if (r->starOn) mix(&r->star, sizeof(r->star));
    return h;
}

// The identity of what both rounds' reach are formed from on a run of the layer (fald_fade.h FaldFadeReachCurrent): the
// input frame (the caller's key; 0 = unknown) and the run-time settings the fields depend on besides the panel file
// (whose change rebuilds the layer, the fade with it): the frame part's settings, the pedestal mode, the view. 0 = never
// reused: an unknown frame, or LED lag (the fields move with the drive state while the frame stands still).
static unsigned long long ReachKey(const FaldResources* r, unsigned long long frameKey, bool temporal, bool panel) {
    if (frameKey == 0 || temporal || panel) return 0;
    unsigned long long h = FadeInputKey(r);
    auto mix = [&h](unsigned long long v) { for (int i = 0; i < 8; i++) { h ^= (v >> (8 * i)) & 0xFFu; h *= 1099511628211ull; } };
    mix(frameKey); mix(r->pedMode); mix(r->debugMode);
    return h ? h : 1ull;
}

// Once per content frame (fald_fade.h): newContent steps T; a run without new content re-forms the frame part only when
// it is stale (built / reset / a starfield change) and otherwise reuses it (settle frames, a C2 re-process).
static void RunFadeFrame(FaldResources* r, bool newContent) {
    const unsigned long long key = FadeInputKey(r);
    const bool stale = !FaldFadeFrameValid(r->fade) || key != r->fadeKey;
    if (!newContent && !stale) return;
    r->fadeKey = key;
    FillCB(r, 0);
    FaldPassScope ps(FALD_PASS_FADE_FRAME);
    FaldFadeRunFrame(r->fade, g_context, FadeCommon(r, false), newContent);
}

static void RunFadeReach(FaldResources* r, uint32_t round, unsigned long long key) {
    FillCB(r, round);
    FaldPassScope ps(round ? FALD_PASS_FADE_REACH1 : FALD_PASS_FADE_REACH0);
    FaldFadeRunReach(r->fade, g_context, FadeCommon(r, true, round), round, key);
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
// (they are nullptr during this call: the conv pass's packed output (u2) gets zeros for them here, and every conv run of a
// round rewrites its packed texture before the round's readers run).
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
        DumpTexture(r->starBgTex, dir + L"fald_star_bg.f32", p.cols, p.rows, 16);       // ln background, brightest pixel's index ly * zw + lx, lit sum, a_eff
    }
    if (r->fadeOn) {   // the fade stack: full res float32 (fade, fade_T, the lit floor F, reach of rounds 0 / 1), s at quarter res
        FaldFadeDebugMaterialize(r->fade, g_context);   // fade_T and F as full-res maps (FadePost forms them per pixel)
        const FaldFadeDebug d = FaldFadeDebugViews(r->fade);
        DumpTexture(d.fade, dir + L"fald_fade.f32", d.width, d.height, 4);
        DumpTexture(d.fadeT, dir + L"fald_fade_t.f32", d.width, d.height, 4);
        DumpTexture(d.floorF, dir + L"fald_fade_floor.f32", d.width, d.height, 4);
        DumpTexture(d.reach[0], dir + L"fald_fade_reach0.f32", d.width, d.height, 4);
        DumpTexture(d.reach[1], dir + L"fald_fade_reach1.f32", d.width, d.height, 4);
        DumpTexture(d.s, dir + L"fald_fade_s.f32", d.wq, d.hq, 4);
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
         << "\nlattice_px " << p.latW << " " << p.latH << " (origin " << p.originX << " " << p.originY << "; pitch latW / cols x"
         << " latH / rows px" << (FaldLatticeFractional(p) ? ", FRACTIONAL" : "") << ", zones up to " << p.cellW << "x" << p.cellH
         << " px: docs/fald-fractional-pitch.md)"
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
         << " index ly * zw + lx (zw = the zone's own width), lit sum, a_eff]:"
         << " cols x rows x 4 float32)"
         << "\nstarfield even " << r->star.even << " lift " << r->star.lift << " target_gain " << r->star.targetGain
         << " target_sigma " << r->star.targetSigma << " keep_nits " << r->star.keepNits
         << " even_reach " << r->star.evenReach << " cap_nits " << r->star.capNits << " strength " << r->star.strength
         << "\nstarfield area_lo " << r->star.areaLo << " area_hi " << r->star.areaHi << " peak_hi " << r->star.peakHi
         << " reach " << r->star.reach << " nb_lo " << r->star.nbLo << " nb_hi " << r->star.nbHi
         << "\nflank_fade " << (r->fadeOn ? 1 : 0) << " (setting " << (fs.flankFade ? 1 : 0)
         << "; the frozen fade stack ASF10 + T11_TMTC: files fald_fade.f32 / fald_fade_t.f32 / fald_fade_floor.f32 /"
         << " fald_fade_reach0.f32 / fald_fade_reach1.f32 width x height float32, fald_fade_s.f32 quarter res; "
         << (r->fade ? FaldFadeVramBytes(r->fade) >> 20 : 0ull) << " MB)"
         << "\nrun_path " << (r->lastPartial ? "partial" : "full") << " (shared/fald_change.h: partial = the touched zones' statistics only,"
         << " the rest by the decide pass; the fields are the same bytes either way)"
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

bool FaldSetExemptRects(const std::vector<FaldExemptRect>& desktop) {
    std::lock_guard<std::mutex> lock(g_faldExemptMutex);
    const bool same = desktop.size() == g_faldExemptDesktop.size() &&
                      (desktop.empty() || memcmp(desktop.data(), g_faldExemptDesktop.data(), desktop.size() * sizeof(FaldExemptRect)) == 0);
    if (same) return false;
    g_faldExemptDesktop = desktop;
    return true;
}

std::vector<FaldExemptRect> FaldExemptRects() {
    std::lock_guard<std::mutex> lock(g_faldExemptMutex);
    return g_faldExemptDesktop;
}

// A debug view is on: the pixel pass again, the view off, inside DesktopLUT's own windows on this monitor
// (shared/fald_exempt.h), so the interface stays readable. The main draw's bindings are still in place; only the CB
// (word 24 = 0), the rasterizer state and the scissor change, and both are put back as they were.
static void DrawExempt(const MonitorContext* ctx, FaldResources* r) {
    if (!g_faldExemptRS) return;
    FaldExemptRect desk[FALD_EXEMPT_HOST_RECTS];
    int n = 0;
    {
        std::lock_guard<std::mutex> lock(g_faldExemptMutex);
        for (const FaldExemptRect& d : g_faldExemptDesktop) {
            if (n == FALD_EXEMPT_HOST_RECTS) { desk[n - 1] = FaldExemptUnion(desk[n - 1], d); continue; }
            desk[n++] = d;
        }
    }
    if (n == 0) return;
    FaldExemptRect mon[FALD_EXEMPT_WIRE_RECTS], fr[FALD_EXEMPT_FRAME_RECTS];
    const int m = FaldExemptForMonitor(desk, n, ctx->x, ctx->y, ctx->width, ctx->height, false, mon, FALD_EXEMPT_WIRE_RECTS);
    const int k = FaldExemptToFrame(mon, m, ctx->width, ctx->height, r->width, r->height, fr);
    if (k == 0) return;
    FillCB(r, 1, 0, true, 0);
    ID3D11RasterizerState* prevRS = nullptr;
    g_context->RSGetState(&prevRS);
    D3D11_RECT prevScissor[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};
    UINT prevCount = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    g_context->RSGetScissorRects(&prevCount, prevScissor);
    g_context->RSSetState(g_faldExemptRS);
    for (int i = 0; i < k; i++) {
        const D3D11_RECT rc = { fr[i].x0, fr[i].y0, fr[i].x1, fr[i].y1 };
        g_context->RSSetScissorRects(1, &rc);
        g_context->Draw(3, 0);
    }
    g_context->RSSetState(prevRS);
    if (prevRS) prevRS->Release();
    g_context->RSSetScissorRects(prevCount, prevCount ? prevScissor : nullptr);
}

// The flank fade's governor (shared/fald_fade_governor.h), once per run before the fade is set up: the adapter + the
// force key, the mode, the refresh cap, the setting; the GPU time of finished content frames; this run's content-frame
// clock. Publishes the status for the interface / the pipe and posts an Events line on a state change.
static void UpdateFadeGovernor(MonitorContext* ctx, FaldResources* r, const FaldSettings& fs, bool newContent) {
    FaldFadeGovernor& gov = r->fadeGov;
    if (!r->fadeGovConfigured) {
        std::string desc;
        r->fadeGovWeak = FaldFadeWeakAdapter(g_device, &desc);
        r->fadeGovConfigured = true;
        std::cout << "[FALD] flank fade governor: " << desc << std::endl;
    }
    gov.Configure(r->fadeGovWeak, g_faldFadeForce.load());
    gov.SetRefreshMs(ctx->frameTimeExactMs);
    const unsigned long long refreshUs = (unsigned long long)(ctx->frameTimeExactMs * 1000.0f + 0.5f);
    gov.SetMode(((unsigned long long)(ctx->isHDREnabled ? 1u : 0u) << 63) ^ ((unsigned long long)r->width << 40) ^
                ((unsigned long long)r->height << 20) ^ refreshUs);
    gov.SetEnabled(fs.flankFade);
    if (r->fade) {
        double us[4];
        const unsigned int n = FaldFadeCollectTiming(r->fade, g_context, us, 4);
        for (unsigned int i = 0; i < n; i++) gov.OnSample(us[i]);
    }
    if (newContent) {
        LARGE_INTEGER now, freq;
        QueryPerformanceCounter(&now); QueryPerformanceFrequency(&freq);
        gov.OnContentFrame((double)now.QuadPart * 1000.0 / (double)freq.QuadPart);
    }
    const FaldFadeGovernorStatus st = gov.Status();
    const bool changed = st.generation != r->fadeGovPublishedGen;
    if (changed || (r->framesRun % 120ull) == 0ull) FaldFadeGovPublish(ctx->index, ctx->isHDREnabled, st);
    if (changed) {
        const bool first = r->fadeGovPublishedGen == 0xFFFFFFFFu;
        r->fadeGovPublishedGen = st.generation;
        FaldFadeGovReport rep;
        if (!first || st.state == FaldFadeGovState::SuspendedWeakGpu) {   // the first Running is no news
            FaldFadeGovReportFor(ctx->index, nullptr, ctx->isHDREnabled, rep);   // the overlay's own (no position needed)
            FaldFadeGovPostEvent(ctx->index, ctx->isHDREnabled, rep);
        }
        std::cout << "[FALD] Monitor " << ctx->index << " flank fade governor: " << FaldFadeGovStateName(st.state)
                  << " (p90 " << st.p90Us / 1000.0 << " ms, budget " << st.budgetUs / 1000.0 << " ms, content "
                  << st.contentMs << " ms)" << std::endl;
    }
}

// The settings a run's textures depend on besides the panel file and the frame: a change means the previous run's
// textures do not stand for this run (the change-driven path's settingsSame).
static unsigned long long SettingsKey(const FaldResources* r, const FaldTemporalSettings& ts) {
    unsigned long long h = 1469598103934665603ull;
    auto mix = [&h](const void* p, size_t n) { const unsigned char* b = (const unsigned char*)p; for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; } };
    const uint32_t words[6] = { r->debugMode, r->pedMode, r->starOn ? 1u : 0u, r->fadeOn ? 1u : 0u, r->gainFused ? 1u : 0u, r->convVariant };
    mix(words, sizeof(words));
    mix(&r->star, sizeof(r->star));
    mix(&ts, sizeof(ts));
    return h;
}

// The core layer's rate-cap governor (shared/fald_core_governor.h), once per run: the force key, the mode, the refresh
// interval; the GPU time of finished full runs (the timing ring); this run's content-frame clock. Publishes the status for
// the interface / the pipe and posts an Events line on a state change (as the fade's).
static void UpdateCoreGovernor(MonitorContext* ctx, FaldResources* r, bool newContent, double nowMs) {
    FaldCoreGovernor& gov = r->coreGov;
    gov.Configure(g_faldCoreForce.load());
    gov.SetRefreshMs(ctx->frameTimeExactMs);
    const unsigned long long refreshUs = (unsigned long long)(ctx->frameTimeExactMs * 1000.0f + 0.5f);
    gov.SetMode(((unsigned long long)(ctx->isHDREnabled ? 1u : 0u) << 63) ^ ((unsigned long long)r->width << 40) ^
                ((unsigned long long)r->height << 20) ^ refreshUs);
    gov.SetEnabled(true);
    FaldCoreTimingSample smp[FaldCoreTiming::kSlots];
    const unsigned int n = FaldCoreTimingCollect(&r->coreTiming, g_context, smp, FaldCoreTiming::kSlots);
    for (unsigned int i = 0; i < n; i++) gov.OnSample(smp[i].coreUs);
    if (newContent) gov.OnContentFrame(nowMs);
    const FaldCoreGovernorStatus st = gov.Status();
    const bool changed = st.generation != r->coreGovPublishedGen;
    if (changed || (r->framesRun % 120ull) == 0ull) FaldCoreGovPublish(ctx->index, ctx->isHDREnabled, st);
    if (changed) {
        const bool first = r->coreGovPublishedGen == 0xFFFFFFFFu;
        r->coreGovPublishedGen = st.generation;
        if (!first || st.state == FaldCoreGovState::Capped) {   // the first Running is no news
            FaldCoreGovReport rep;
            FaldCoreGovReportFor(ctx->index, nullptr, ctx->isHDREnabled, rep);   // the overlay's own (no position needed)
            FaldCoreGovPostEvent(ctx->index, ctx->isHDREnabled, rep);
        }
        std::cout << "[FALD] Monitor " << ctx->index << " core governor: " << FaldCoreGovStateName(st.state)
                  << " (p90 " << st.p90Us / 1000.0 << " ms, budget " << st.budgetUs / 1000.0 << " ms, spacing "
                  << st.spacingMs << " ms)" << std::endl;
    }
}

// The pixel pass: the intermediate + the standing fields -> finalRT (the fullscreen triangle; g_vs bound by the caller).
// filt = debug view 7's "filtered" map (t10). Leaves the pixel stage's views null.
static void RunPixel(MonitorContext* ctx, FaldResources* r, ID3D11RenderTargetView* finalRT, ID3D11ShaderResourceView* filt) {
    FillCB(r, 1);
    g_context->OMSetRenderTargets(1, &finalRT, nullptr);
    g_context->PSSetShader(g_faldPS, nullptr, 0);
    BindCommon(r, false);
    ID3D11ShaderResourceView* fields[2] = { r->bTrueSRV, r->bEstSRV };
    g_context->PSSetShaderResources(5, 2, fields);
    g_context->PSSetShaderResources(FALD_FIELDS_SLOT, 1, &r->fieldsSRV);          // t39: the same packed (SampleFields)
    g_context->PSSetShaderResources(9, 1, &r->gainBSRV);
    // debug view 7: instantaneous vs filtered (mode 3: vs the clocks' mean LED state of this frame)
    g_context->PSSetShaderResources(4, 1, &r->driveSRV);
    g_context->PSSetShaderResources(10, 1, &filt);
    g_context->PSSetShaderResources(13, 1, &r->activeSRV[1]);                     // debug view 8 (nullptr without a boost LUT)
    if (r->fadeOn) {                                              // FadePost: fade, F's opening, reach of round 1, s - f_q
        ID3D11ShaderResourceView* fv[FALD_FADE_POST_VIEWS];
        FaldFadePostViews(r->fade, 1, fv);
        g_context->PSSetShaderResources(FALD_FADE_POST_SLOT, FALD_FADE_POST_VIEWS, fv);
    }
    { FaldPassScope ps(FALD_PASS_PIXEL); g_context->Draw(3, 0); }
    if (r->debugMode != 0) DrawExempt(ctx, r);   // a view: DesktopLUT's windows keep the corrected picture
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    g_context->PSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_context->PSSetShaderResources(FALD_FIELDS_SLOT, 1, nullSrv);                // t39 (the conv pass's u2 next run)
}

void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT, bool newContent, unsigned long long frameKey,
                   const FaldChangeRect* rects, int numRects) {
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
    // The frozen fade stack (shared/fald_fade.h): on = its resources exist; off = released, and nothing below knows it
    // exists (CB word 75 = 0, t20-t23 null, no dispatch) — today's layer bit for bit. Its governor suspended = off too
    // (the setting stays on; the resources go back: a retry rebuilds them, paced).
    UpdateFadeGovernor(ctx, r, fs, newContent);
    if (fs.flankFade && r->fadeGov.Allowed()) r->fadeOn = EnsureFade(r);
    else if (r->fade || r->fadeOn) {
        FaldFadeDestroy(r->fade); r->fade = nullptr; r->fadeOn = false; r->fadeKey = 0; r->fadeRetryCounter = 0; r->fadeFailLogged = false;
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
    const double nowMs = (double)qpcNow.QuadPart * 1000.0 / (double)qpcFreq.QuadPart;
    UpdateCoreGovernor(ctx, r, newContent, nowMs);
    // the drive maps the kernels and the LED-lag pass of a round read (round 0 has its own instantaneous / filtered maps)
    auto mapSrv = [r](FaldDriveMap m, uint32_t round) {
        return m == FALD_MAP_FILTERED ? (round ? r->driveFiltSRV : r->driveFilt0SRV)
             : (m == FALD_MAP_CLOCK_EST ? r->clkEstSRV : (round ? r->driveSRV : r->drive0SRV));
    };

    // ---- the path of this run (shared/fald_change.h): the zones the rects touched, the preconditions ----
    const FaldPanelParams& p = r->params;
    const unsigned int totalZones = p.cols * p.rows, maxZones = FaldChangeMaxZones(totalZones);
    r->touched.clear();
    bool listOk = false;
    if (numRects >= 0) listOk = FaldTouchedZones(rects, numRects, p.originX, p.originY, p.latW, p.latH, p.cols, p.rows, maxZones, r->touched);
    else if (!newContent && frameKey != 0 && frameKey == r->lastFrameKey) listOk = true;   // the previous run's frame again
    const unsigned long long settingsKey = SettingsKey(r, ts);
    FaldChangeInputs in = {};
    in.stateComplete = r->stateComplete; in.settingsSame = settingsKey == r->lastSettingsKey;
    in.frameKnown = listOk && frameKey != 0; in.newContent = newContent; in.gainFused = r->gainFused;
    in.temporalMode = clockOk ? ts.mode : (ts.mode == FALD_TEMPORAL_PANEL ? 0u : ts.mode);   // the mode that runs (FaldTemporalBeginRun)
    in.fadeOn = r->fadeOn;
    in.touchedZones = (unsigned int)r->touched.size(); in.totalZones = totalZones;
    const bool partial = g_faldPartialOverride != 0 && FaldChangeDecide(in, maxZones) == FALD_CHANGE_PARTIAL;

    // ---- the rate cap (shared/fald_core_governor.h): a full run the governor holds back keeps the standing fields and runs
    // only the pixel pass on the new frame — before the LED-lag bookkeeping, which a held run does not advance (the
    // layer's time stands still between full runs)
    bool held = false;
    if (!partial && r->fieldsStand && in.settingsSame)
        held = g_faldCoreHoldOverride >= 0 ? g_faldCoreHoldOverride == 1 : !r->coreGov.FullAllowed(nowMs);
    if (held) {
        r->runMode = FaldRunModeWord(false, FaldConvSoOf(r->convVariant), 0);
        ID3D11RenderTargetView* nullRT0 = nullptr;
        g_context->OMSetRenderTargets(1, &nullRT0, nullptr);
        // debug view 7's "filtered" map as the last full run bound it (mode 3 without clock textures runs as off)
        const FaldDriveMap filtMap = ts.mode == FALD_TEMPORAL_PANEL ? (clockOk ? FALD_MAP_CLOCK_EST : FALD_MAP_DRIVE)
                                   : (ts.mode != FALD_TEMPORAL_OFF ? FALD_MAP_FILTERED : FALD_MAP_DRIVE);
        RunPixel(ctx, r, finalRT, mapSrv(filtMap, 1));
        r->framesRun++; r->heldRuns++;
        r->lastPartial = false;
        r->stateComplete = false;              // the standing textures are not this frame's: the next run that is not held is full
        return;
    }
    if (!partial) r->coreGov.OnFullRun(nowMs);
    if (partial && !r->touched.empty()) {                             // the list for the sweeps (t38)
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(g_context->Map(r->zoneListBuf, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
            memcpy(m.pData, r->touched.data(), r->touched.size() * sizeof(uint32_t));
            g_context->Unmap(r->zoneListBuf, 0);
        }
    }
    r->runMode = FaldRunModeWord(partial, FaldConvSoOf(r->convVariant), (unsigned int)r->touched.size());

    const FaldTemporalRun trun = FaldTemporalBeginRun(r, ts, clockOk, qpcNow.QuadPart, qpcFreq.QuadPart, ctx->frameTimeExactMs);
    const bool temporal = trun.temporal, panel = trun.panel;
    const FaldClockPlan plan = trun.clock;
    // the map the panel's pipeline is fed this frame (modes 1 / 2): a delay-ring entry or the round's instantaneous drive
    auto inDriveOf = [r, &trun](uint32_t round) { return trun.delayedSlot >= 0 ? r->delaySRV[trun.delayedSlot] : (round ? r->driveSRV : r->drive0SRV); };

    // the main pass rendered into r->inter with finalRT unbound; make sure the RTV is off before
    // the intermediate is read as an SRV
    ID3D11RenderTargetView* nullRT = nullptr;
    g_context->OMSetRenderTargets(1, &nullRT, nullptr);
    const int tslot = FaldCoreTimingBegin(&r->coreTiming, g_context, r);   // the governor's timing of this full run

    // black-frame LED boost (panel files with a LUT): each round's boost comes from the zone flags of the frame the
    // panel receives in that round (the decide pass after its sweep finishes it from the persistent count), and is NOT
    // filtered by the temporal state (instant on the panel).
    // The starfield plan of THIS source frame; every pass below reads Balance(source). Partial: S0 on the touched
    // zones, then the plan and a full round-0 sweep only if a zone's star statistic moved (the decide pass's arguments).
    const StatDispatch stat0Full = { false, true, FALD_ARGS_STAT0_FULL, FALD_ARGS_STAT0_FULL_COMBINE };
    const StatDispatch stat0Part = { true, true, FALD_ARGS_STAT0_PART, FALD_ARGS_STAT0_PART_COMBINE };
    const StatDispatch stat1Full = { false, true, FALD_ARGS_STAT1_FULL, FALD_ARGS_STAT1_FULL_COMBINE };
    const StatDispatch stat1Part = { true, true, FALD_ARGS_STAT1_PART, FALD_ARGS_STAT1_PART_COMBINE };
    if (r->starOn) {
        if (!partial) RunStar(r);
        else { RunStarS0(r, true); RunDecide(r, 2); RunStarPlan(r, true); }
    }
    FaldCoreTimingStamp(&r->coreTiming, g_context, tslot, 1);
    if (r->fadeOn) RunFadeFrame(r, newContent);   // fade stack: the fade, F, T (once per content frame; fald_fade.h)
    FaldCoreTimingStamp(&r->coreTiming, g_context, tslot, 2);   // (the fade's passes are its own governor's)
    // Both rounds' reach of the frame they were formed from (no new content, LED lag off, the same settings): reused
    const unsigned long long reachKey = r->fadeOn ? ReachKey(r, frameKey, temporal, panel) : 0ull;
    const bool reachReuse = r->fadeOn && !newContent && FaldFadeReachCurrent(r->fade, reachKey);
    if (plan.runPass) RunPanelClock(r);    // mode 3: the state of this frame comes from PAST frames only (PanelDriveState.fields)
    // round 0: the sweep (full; partial: the touched zones — or the whole lattice when the starfield moved)
    if (!partial) RunStat(r, 0);
    else if (!r->starOn) RunStat(r, 0, kStatPartial);
    else { RunStat(r, 0, stat0Full); RunStat(r, 0, stat0Part); }
    if (temporal) RunTemporal(r, inDriveOf(0), 0); // both rounds read the SAME committed state (DriveState.peek)
    RunDecide(r, 0);                       // round 0's boost; partial: what follows from the flags
    RunConv(r, mapSrv(trun.trueMap, 0), mapSrv(trun.estMap, 0), r->boostSRV[0], 0, partial);
    RunGain(r, 0, partial);
    FaldCoreTimingStamp(&r->coreTiming, g_context, tslot, 3);
    if (r->fadeOn && !reachReuse) RunFadeReach(r, 0, reachKey);   // reach of round 0's knee: FadePost in the round-1 statistic
    FaldCoreTimingStamp(&r->coreTiming, g_context, tslot, 4);
    // round 1: the sweep of the corrected frame (partial: the whole lattice if round 0 moved, else the touched zones)
    if (!partial) RunStat(r, 1);
    else { RunStat(r, 1, stat1Full); RunStat(r, 1, stat1Part); }
    if (temporal) RunTemporal(r, inDriveOf(1), 1);
    RunDecide(r, 1);                       // round 1's boost; partial: the convolution, the gain and the pixel pass from the flags
    RunConv(r, mapSrv(trun.trueMap, 1), mapSrv(trun.estMap, 1), r->boostSRV[1], 1, partial);
    RunGain(r, 1, partial);
    FaldCoreTimingStamp(&r->coreTiming, g_context, tslot, 5);
    if (r->fadeOn && !reachReuse) RunFadeReach(r, 1, reachKey);   // reach of round 1's knee: FadePost in the pixel pass
    FaldCoreTimingStamp(&r->coreTiming, g_context, tslot, 6);
    r->framesRun++;
    r->lastPartial = partial;
    const std::wstring dumpDir = TakeDumpRequest(ctx);
    if (!dumpDir.empty()) DumpFields(ctx, r, dumpDir);   // before the commit: the state file is the map the pass read
    if (temporal || panel) {
        FaldPassScope ps(FALD_PASS_COMMIT);
        if (temporal) {                    // DriveState.commit: round 1's filtered map becomes the state; the ring
            g_context->CopyResource(r->driveStateTex, r->driveFiltTex);   // takes round 1's INSTANTANEOUS map (the indices:
            if (trun.delay > 0)                                           // FaldTemporalEndRun below)
                g_context->CopyResource(r->delayTex[r->delayHead], r->driveTex);
        }
        if (panel) {                       // PanelDriveState.commit: round 1's INSTANTANEOUS map is the next frame's target
            if (plan.seedStates) {         // (k = 0: it replaces the previous frame's — the later frame is the one shown);
                g_context->CopyResource(r->clkStateTex[0], r->driveTex);   // a seeding run takes the panel as settled on this
                g_context->CopyResource(r->clkStateTex[1], r->driveTex);   // frame (both clocks)
            }
            if (plan.commitPrev) g_context->CopyResource(r->clkPrevTex, r->driveTex);
        }
    }

    // pixel pass: inter + fields -> finalRT (fullscreen triangle; g_vs already bound by the caller)
    RunPixel(ctx, r, finalRT, mapSrv(trun.debugFiltMap, 1));
    FaldCoreTimingEnd(&r->coreTiming, g_context, tslot);
    if (!dumpDir.empty()) DumpOutput(ctx, r, finalRT, dumpDir);

    // The commit's indices and the settle hold (render.cpp asks FaldSettlePending on an acquire timeout and re-runs the
    // layer on its own intermediate): shared/fald_temporal.cpp FaldTemporalEndRun.
    FaldTemporalEndRun(r, trun, ts, newContent);
    // what this run leaves behind for the next one's path decision (shared/fald_change.h)
    r->stateComplete = true;
    r->fieldsStand = true;
    r->lastSettingsKey = settingsKey;
    r->lastFrameKey = frameKey;
}

bool FaldSettlePending(const MonitorContext* ctx) {
    const FaldResources* r = ctx ? ctx->fald : nullptr;
    return r && r->valid && FaldTemporalSettlePending(r);
}

void FaldLayerIdle(MonitorContext* ctx) {
    FaldResources* r = ctx ? ctx->fald : nullptr;
    FaldTemporalIdle(r);
    if (r) {
        FaldFadeReset(r->fade);            // the fade stack's T state is void too (the panel did not show our frames)
        r->stateComplete = false; r->fieldsStand = false;          // and the previous run's textures stand for no frame the next run will see
    }
}
