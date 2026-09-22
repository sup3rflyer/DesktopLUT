// DesktopLUT DWM Hook - hook_fald.cpp
// The FALD correction inside dwm.exe: the whole layer the overlay path runs — the core, starfield with its glow-fill
// part, and LED lag (the temporal drive state, bookkeeping shared with the overlay in shared/fald_temporal.cpp).
//
// This file deliberately mirrors src/fald.cpp pass for pass and constant-buffer word for word, running the same HLSL
// (shared/fald_shader.h), so for the same input frame the two paths must produce the same output — the acceptance
// gate, checked with the one-shot field dump (FaldPollDumpRequest) against the overlay and dlc/fald/gpuemu.py.
#include "pch.h"
#include "hook_fald.h"
#include "hook_log.h"
#include "fald_shader.h"
#include "fald_temporal.h"

#include <cstdarg>
#include <cstring>
#include <string>
#include <algorithm>
#include <vector>

// ---------------------------------------------------------------------------------------------
// Process-global shaders (compiled once per attach on DWM's device)
// ---------------------------------------------------------------------------------------------
static ID3D11Device* g_dev = nullptr;
static ID3D11DeviceContext* g_ctx = nullptr;

static ID3D11ComputeShader* g_statCS = nullptr;
static ID3D11ComputeShader* g_convCS = nullptr;
static ID3D11ComputeShader* g_gainCS = nullptr;
static ID3D11ComputeShader* g_blurCS = nullptr;
static ID3D11ComputeShader* g_boostCS = nullptr;
static ID3D11PixelShader* g_faldPS = nullptr;
static ID3D11VertexShader* g_faldVS = nullptr;
static ID3D11SamplerState* g_faldSampler = nullptr;

// Starfield feature (starfield balancing S0-S2 + its glow-fill part G0-G4). Compiled beside the core
// but NOT part of FaldShadersReady: a compile failure here costs the feature, never the correction.
static ID3D11ComputeShader* g_starStatCS = nullptr;     // S0: star statistic of the source frame
static ID3D11ComputeShader* g_starWeightCS = nullptr;   // S1: tapered protection field + zone weights
static ID3D11ComputeShader* g_starPlanCS = nullptr;     // S2: target + the plan the pixels sample
static ID3D11ComputeShader* g_glowZoneCS = nullptr;     // G0: zone pedestal
static ID3D11ComputeShader* g_glowDilateCS = nullptr;   // G1: box maximum
static ID3D11ComputeShader* g_glowErodeCS = nullptr;    // G2: box minimum (the closing)
static ID3D11ComputeShader* g_glowEnvCS = nullptr;      // G3: blur + deficit
static ID3D11ComputeShader* g_glowBandCS = nullptr;     // G4: count-threshold band (boost + mean rule only)
static ID3D11ComputeShader* g_temporalCS = nullptr;     // LED lag pass 1b: first-order drive state (modes 1 / 2)
static ID3D11ComputeShader* g_clockCS = nullptr;        // LED lag pass 1c: the two parity clocks (mode 3)
static bool StarShadersReady() { return g_starStatCS && g_starWeightCS && g_starPlanCS; }
// (the settle / content events are per monitor: FaldMonitor::settleEvt / contentEvt)
static bool GlowShadersReady() { return g_glowZoneCS && g_glowDilateCS && g_glowErodeCS && g_glowEnvCS && g_glowBandCS; }
static void CompileFeatureShaders();   // below FaldReleaseShaders
static void FaldUnbindAll();          // with FaldRun
static void CoverReset(FaldMonitor* m);   // with FaldUpdateClean

// GPU timing (timestamp queries, read back without flushing a few frames later — never a stall in
// the present path). Three stamps per run: start, after the starfield passes, end of the pixel pass.
static ID3D11Query* g_tsDisjoint[4] = {};
static ID3D11Query* g_tsStamp[4][3] = {};
static bool g_tsInFlight[4] = {};
static FaldMonitor* g_tsOwner[4] = {};   // the monitor a slot's result is credited to
static unsigned int g_tsHead = 0;

// src/fald.h FALD_GLOW_REACH_MAX, which is ALSO the HLSL's constant of that name (shared/fald_shader.h):
// it sizes the glow dilation texture's margin, so the two must agree.
static const unsigned int HOOK_GLOW_REACH_MAX = 4u;

static const unsigned int HOOK_FALD_COVER_TILE = 32u;       // priming coverage granularity (px)
static const unsigned int HOOK_FALD_RELEASE_AFTER = 600u;   // presents off before an entry frees its GPU memory
static HANDLE g_primeEvent = NULL;                          // DWM_HOOK_FALD_PRIME_EVENT (the host creates it)
static long long g_primeLastQpc = 0;                        // the last prime request (throttle)

// t0..t24, as the HLSL declares them (HOOK_FALD_SRV_SLOTS). The pixel pass and every compute pass
// bind the whole range so a slot left over from a previous pass cannot be read by accident.
static const UINT FALD_SRV_SLOTS = HOOK_FALD_SRV_SLOTS;
// The deepest UAV range any core pass binds (stat binds u0/u1).
static const UINT FALD_UAV_SLOTS = HOOK_FALD_UAV_SLOTS;

static void LogF(const char* fmt, ...) {
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    log_to_file(msg);
}

static bool CompileOne(const std::string& src, const char* name, const char* target, ID3DBlob** blob) {
    ID3DBlob* err = nullptr;
    HRESULT hr = D3DCompile(src.c_str(), src.size(), name, nullptr, nullptr, "main", target, 0, 0, blob, &err);
    if (FAILED(hr)) {
        LogF("FALD: %s compile error: %s", name, err ? (const char*)err->GetBufferPointer() : "?");
        if (err) err->Release();
        return false;
    }
    if (err) err->Release();
    return true;
}

bool FaldShadersReady() {
    return g_statCS && g_convCS && g_gainCS && g_blurCS && g_boostCS && g_faldPS && g_faldVS && g_faldSampler;
}

bool FaldInitShaders(ID3D11Device* dev, ID3D11DeviceContext* ctx) {
    if (!dev || !ctx) return false;
    g_dev = dev;
    g_ctx = ctx;
    if (FaldShadersReady()) return true;

    const std::string common = g_faldCommonSource;
    ID3DBlob* b = nullptr;
    HRESULT hr = S_OK;

    struct { const char* src; const char* name; ID3D11ComputeShader** out; } cs[] = {
        { g_faldStatSource,  "FaldStatCS",  &g_statCS  },
        { g_faldConvSource,  "FaldConvCS",  &g_convCS  },
        { g_faldGainSource,  "FaldGainCS",  &g_gainCS  },
        { g_faldBlurSource,  "FaldBlurCS",  &g_blurCS  },
        { g_faldBoostSource, "FaldBoostCS", &g_boostCS },
    };
    for (const auto& s : cs) {
        if (!CompileOne(common + s.src, s.name, "cs_5_0", &b)) { FaldReleaseShaders(); return false; }
        hr = g_dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, s.out);
        b->Release(); b = nullptr;
        if (FAILED(hr)) { LogF("FALD: CreateComputeShader(%s) failed hr=0x%08X", s.name, (unsigned)hr); FaldReleaseShaders(); return false; }
    }

    if (!CompileOne(common + g_faldPixelSource, "FaldPS", "ps_5_0", &b)) { FaldReleaseShaders(); return false; }
    hr = g_dev->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldPS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { LogF("FALD: CreatePixelShader failed hr=0x%08X", (unsigned)hr); FaldReleaseShaders(); return false; }

    // The pixel pass is a fullscreen triangle with no vertex buffer and no input layout, so the hook
    // cannot reuse its own LUT vertex shader (that one reads a POSITION/TEXCOORD vertex buffer).
    if (!CompileOne(g_faldFullscreenVsSource, "FaldVS", "vs_5_0", &b)) { FaldReleaseShaders(); return false; }
    hr = g_dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &g_faldVS);
    b->Release(); b = nullptr;
    if (FAILED(hr)) { LogF("FALD: CreateVertexShader failed hr=0x%08X", (unsigned)hr); FaldReleaseShaders(); return false; }

    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    if (FAILED(g_dev->CreateSamplerState(&sd, &g_faldSampler))) {
        log_to_file("FALD: sampler creation failed");
        FaldReleaseShaders();
        return false;
    }
    log_to_file("FALD: correction shaders compiled (stateless core)");
    CompileFeatureShaders();
    return true;
}

template <typename T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

static void ReleaseFeatureShaders() {
    SafeRelease(g_clockCS); SafeRelease(g_temporalCS);
    if (g_primeEvent) { CloseHandle(g_primeEvent); g_primeEvent = NULL; }
    SafeRelease(g_glowBandCS); SafeRelease(g_glowEnvCS); SafeRelease(g_glowErodeCS);
    SafeRelease(g_glowDilateCS); SafeRelease(g_glowZoneCS);
    SafeRelease(g_starPlanCS); SafeRelease(g_starWeightCS); SafeRelease(g_starStatCS);
    for (int i = 0; i < 4; i++) {
        SafeRelease(g_tsDisjoint[i]);
        for (int k = 0; k < 3; k++) SafeRelease(g_tsStamp[i][k]);
        g_tsInFlight[i] = false;
    }
}

static void CompileFeatureShaders() {
    const std::string common = g_faldCommonSource;
    struct { const char* src; const char* name; ID3D11ComputeShader** out; } cs[] = {
        { g_faldStarStatSource,   "FaldStarStatCS",   &g_starStatCS   },
        { g_faldStarWeightSource, "FaldStarWeightCS", &g_starWeightCS },
        { g_faldStarPlanSource,   "FaldStarPlanCS",   &g_starPlanCS   },
        { g_faldGlowZoneSource,   "FaldGlowZoneCS",   &g_glowZoneCS   },
        { g_faldGlowDilateSource, "FaldGlowDilateCS", &g_glowDilateCS },
        { g_faldGlowErodeSource,  "FaldGlowErodeCS",  &g_glowErodeCS  },
        { g_faldGlowEnvSource,    "FaldGlowEnvCS",    &g_glowEnvCS    },
        { g_faldGlowBandSource,   "FaldGlowBandCS",   &g_glowBandCS   },
        { g_faldTemporalSource,   "FaldTemporalCS",   &g_temporalCS   },
        { g_faldPanelClockSource, "FaldPanelClockCS", &g_clockCS      },
    };
    for (const auto& s : cs) {
        ID3DBlob* b = nullptr;
        if (!CompileOne(common + s.src, s.name, "cs_5_0", &b)) continue;   // logged; that part stays off
        if (FAILED(g_dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, s.out)))
            LogF("FALD: CreateComputeShader(%s) failed", s.name);
        b->Release();
    }
    // GPU timestamp queries (optional: without them the cost log shows CPU time only)
    D3D11_QUERY_DESC qd = {};
    for (int i = 0; i < 4; i++) {
        qd.Query = D3D11_QUERY_TIMESTAMP_DISJOINT;
        if (FAILED(g_dev->CreateQuery(&qd, &g_tsDisjoint[i]))) { g_tsDisjoint[i] = nullptr; continue; }
        qd.Query = D3D11_QUERY_TIMESTAMP;
        for (int k = 0; k < 3; k++)
            if (FAILED(g_dev->CreateQuery(&qd, &g_tsStamp[i][k]))) g_tsStamp[i][k] = nullptr;
    }
    LogF("FALD: starfield shaders %s, glow fill shaders %s, LED lag shaders %s",
         StarShadersReady() ? "compiled" : "UNAVAILABLE", GlowShadersReady() ? "compiled" : "UNAVAILABLE",
         (g_temporalCS && g_clockCS) ? "compiled" : "UNAVAILABLE");
}

void FaldReleaseShaders() {
    ReleaseFeatureShaders();
    SafeRelease(g_faldSampler);
    SafeRelease(g_faldVS);
    SafeRelease(g_faldPS);
    SafeRelease(g_boostCS);
    SafeRelease(g_blurCS);
    SafeRelease(g_gainCS);
    SafeRelease(g_convCS);
    SafeRelease(g_statCS);
}

// ---------------------------------------------------------------------------------------------
// Staged panel files (read once at attach)
// ---------------------------------------------------------------------------------------------
struct FaldPanelFile {
    int left = 0, top = 0;
    bool isHdr = false;
    FaldPanelParams params;
};
// Fixed-size, like lutTargets in hook_lut.h and for the same reason: DWM may call the Present hooks
// on more than one thread, and a container that reallocates would hand a concurrent reader a freed
// pointer. Both arrays are filled once (panel files at attach) or appended to only from a Present
// (monitors), and never shrink.
static const int FALD_MAX_PANEL_FILES = 2 * MAX_DWM_HOOK_MONITORS;   // one SDR + one HDR per monitor
static FaldPanelFile g_panelFiles[FALD_MAX_PANEL_FILES];
static int g_numPanelFiles = 0;

static const FaldPanelFile* FindPanelFile(int left, int top, bool isHdr) {
    for (int i = 0; i < g_numPanelFiles; i++)
        if (g_panelFiles[i].left == left && g_panelFiles[i].top == top && g_panelFiles[i].isHdr == isHdr)
            return &g_panelFiles[i];
    return nullptr;
}

bool FaldHasPanelFile(int left, int top, bool isHdr) { return FindPanelFile(left, top, isHdr) != nullptr; }
bool FaldHasAnyPanelFile() { return g_numPanelFiles > 0; }

int FaldLoadPanelFiles(const char* lutFolder) {
    g_numPanelFiles = 0;
    if (!lutFolder) return 0;

    char dirA[MAX_PATH];
    snprintf(dirA, sizeof(dirA), "%s\\%s", lutFolder, DWM_HOOK_FALD_SUBDIR_A);
    char patternA[MAX_PATH];
    snprintf(patternA, sizeof(patternA), "%s\\*.bin", dirA);

    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(patternA, &fd);
    if (h == INVALID_HANDLE_VALUE) return 0;
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        if (g_numPanelFiles >= FALD_MAX_PANEL_FILES) { log_to_file("FALD: too many panel files staged — ignoring the rest"); break; }
        FaldPanelFile pf;
        if (sscanf(fd.cFileName, "%d_%d", &pf.left, &pf.top) != 2) continue;
        pf.isHdr = strstr(fd.cFileName, "_hdr") != nullptr;

        char fullA[MAX_PATH];
        snprintf(fullA, sizeof(fullA), "%s\\%s", dirA, fd.cFileName);
        wchar_t fullW[MAX_PATH] = {};
        if (MultiByteToWideChar(CP_ACP, 0, fullA, -1, fullW, MAX_PATH) == 0) continue;

        std::string err;
        if (!LoadFaldPanelParams(fullW, pf.params, err)) {
            LogF("FALD: skipping unparseable panel file %s: %s", fd.cFileName, err.c_str());
            continue;
        }
        // The fit's code domain is the panel's: a PQ (HDR) file cannot serve an ACM SDR desktop and
        // vice versa. Caught here so a mismatched file costs one log line at attach, not a wrong
        // correction on screen.
        if (!FaldTransferMatchesMode(pf.params.transfer, pf.isHdr)) {
            LogF("FALD: panel file %s is a %s fit but is staged for %s — ignored", fd.cFileName,
                 pf.params.transfer == FALD_TRANSFER_GAMMA ? "gamma (SDR)" : "PQ (HDR)",
                 pf.isHdr ? "HDR" : "SDR (ACM)");
            continue;
        }
        LogF("FALD: panel file %s loaded: pos(%d,%d) %s, %ux%u cells of %ux%u px, sub %u, white %.1f nits, boost %s",
             fd.cFileName, pf.left, pf.top, pf.isHdr ? "HDR" : "SDR(ACM)",
             pf.params.cols, pf.params.rows, pf.params.cellW, pf.params.cellH, pf.params.sub,
             pf.params.white, pf.params.hasBoost ? "yes" : "none");
        g_panelFiles[g_numPanelFiles++] = std::move(pf);
    } while (FindNextFileA(h, &fd) != 0);
    FindClose(h);

    LogF("FALD: %d panel file(s) loaded", g_numPanelFiles);
    return g_numPanelFiles;
}

// ---------------------------------------------------------------------------------------------
// Per-monitor resources
// ---------------------------------------------------------------------------------------------
struct FaldMonitor : FaldTemporalState {   // LED-lag bookkeeping: shared/fald_temporal.h (the overlay's FaldResources too)
    int left = 0, top = 0;
    bool isHdr = false;
    unsigned int width = 0, height = 0;
    DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    FaldPanelParams params;
    bool valid = false;
    bool failed = false;          // latched: logged once, never retried for this (pos, mode, size)

    uint32_t debugMode = 0;
    uint32_t pedMode = 0;
    unsigned long long framesRun = 0;
    double lastRunUs = 0.0;

    // Full-size intermediate: the hook's LUT/tonemap pass renders the whole frame here, the pixel
    // pass below reads it and writes the back buffer. Same role as FaldResources::inter in the
    // overlay path, and the reason the layer costs one extra full-size FP16 target per monitor.
    ID3D11Texture2D* interTex = nullptr;
    ID3D11RenderTargetView* interRTV = nullptr;
    ID3D11ShaderResourceView* interSRV = nullptr;

    // Clean source: the composed frame as DWM drew it, BEFORE any of our passes. DWM only re-composes
    // its dirty rects into the back buffer — everywhere else the back buffer still holds the previous
    // frame's FINISHED output (LUT + FALD). The layer needs the whole frame, so it cannot read the
    // back buffer wholesale (that re-corrects corrected pixels every present: stacking); it keeps
    // this copy up to date from the dirty rects alone and reads the full frame from here.
    // `primed` = every pixel has been written from a composed rect since the copy last went stale
    // (creation, or a present of this monitor the layer did not see) — until then the layer is off.
    ID3D11Texture2D* cleanTex = nullptr;
    ID3D11ShaderResourceView* cleanSRV = nullptr;
    bool primed = false;
    // Priming by COVERAGE, not by one full-frame rect: the copy is valid once every pixel has been refreshed from a
    // composed rect since it went stale, whichever presents did it. Tracked on HOOK_FALD_COVER_TILE-px tiles (a tile
    // counts only when one rect covers it completely — conservative). Allocated at build, never per frame.
    std::vector<uint8_t> cover;
    unsigned int coverCols = 0, coverRows = 0, coverLeft = 0;
    unsigned int offPresents = 0;                         // consecutive presents this entry did not run (VRAM release)
    HANDLE settleEvt = NULL, contentEvt = NULL;           // DWM_HOOK_FALD_SETTLE / CONTENT events of this position
    unsigned int evtOpenTick = 0;                         // open retried every ~120 runs while the host has none

    // panel tables
    ID3D11Texture2D* curveTex = nullptr;  ID3D11ShaderResourceView* curveSRV = nullptr;
    ID3D11Buffer* kTrueBuf = nullptr;     ID3D11ShaderResourceView* kTrueSRV = nullptr;
    ID3D11Buffer* kEstBuf = nullptr;      ID3D11ShaderResourceView* kEstSRV = nullptr;
    // per-frame fields
    ID3D11Texture2D* driveTex = nullptr;  ID3D11UnorderedAccessView* driveUAV = nullptr; ID3D11ShaderResourceView* driveSRV = nullptr;
    ID3D11Texture2D* bTrueTex = nullptr;  ID3D11UnorderedAccessView* bTrueUAV = nullptr; ID3D11ShaderResourceView* bTrueSRV = nullptr;
    ID3D11Texture2D* bEstTex = nullptr;   ID3D11UnorderedAccessView* bEstUAV = nullptr;  ID3D11ShaderResourceView* bEstSRV = nullptr;
    ID3D11Texture2D* gainATex = nullptr;  ID3D11UnorderedAccessView* gainAUAV = nullptr; ID3D11ShaderResourceView* gainASRV = nullptr;
    ID3D11Texture2D* gainBTex = nullptr;  ID3D11UnorderedAccessView* gainBUAV = nullptr; ID3D11ShaderResourceView* gainBSRV = nullptr;
    // flat-lattice response of both kernels (computed once at build): a flat field must give gain 1
    ID3D11Texture2D* flatTrueTex = nullptr; ID3D11UnorderedAccessView* flatTrueUAV = nullptr; ID3D11ShaderResourceView* flatTrueSRV = nullptr;
    ID3D11Texture2D* flatEstTex = nullptr;  ID3D11UnorderedAccessView* flatEstUAV = nullptr;  ID3D11ShaderResourceView* flatEstSRV = nullptr;
    // black-frame LED boost, one set per statistic round
    ID3D11Texture2D* activeTex[2] = {}; ID3D11UnorderedAccessView* activeUAV[2] = {}; ID3D11ShaderResourceView* activeSRV[2] = {};
    ID3D11Texture2D* boostTex[2] = {};  ID3D11UnorderedAccessView* boostUAV[2] = {};  ID3D11ShaderResourceView* boostSRV[2] = {};
    ID3D11Buffer* boostLutBuf = nullptr; ID3D11ShaderResourceView* boostLutSRV = nullptr;

    ID3D11Buffer* cb = nullptr;

    // Starfield feature (created on the first frame it is on, released when it goes off).
    // Requests from the live settings; *On = requested AND the textures exist (CB words 35 / 75 / 80).
    bool starWanted = false, glowWanted = false;
    bool starOn = false, glowOn = false, glowBand = false;
    bool starFailed = false, glowFailed = false;          // latched: logged once, no per-frame retry
    DwmHookFaldTuning tuning = {};                        // clamped; defaults until the host sends some
    ID3D11Texture2D* starStatTex = nullptr;  ID3D11UnorderedAccessView* starStatUAV = nullptr;  ID3D11ShaderResourceView* starStatSRV = nullptr;
    ID3D11Texture2D* starWTex = nullptr;     ID3D11UnorderedAccessView* starWUAV = nullptr;     ID3D11ShaderResourceView* starWSRV = nullptr;
    ID3D11Texture2D* starPlanTex = nullptr;  ID3D11UnorderedAccessView* starPlanUAV = nullptr;  ID3D11ShaderResourceView* starPlanSRV = nullptr;
    ID3D11Texture2D* starBgTex = nullptr;    ID3D11UnorderedAccessView* starBgUAV = nullptr;    ID3D11ShaderResourceView* starBgSRV = nullptr;
    ID3D11Texture2D* starPlan2Tex = nullptr; ID3D11UnorderedAccessView* starPlan2UAV = nullptr; ID3D11ShaderResourceView* starPlan2SRV = nullptr;
    ID3D11Texture2D* glowVTex = nullptr;     ID3D11UnorderedAccessView* glowVUAV = nullptr;     ID3D11ShaderResourceView* glowVSRV = nullptr;
    ID3D11Texture2D* glowDilTex = nullptr;   ID3D11UnorderedAccessView* glowDilUAV = nullptr;   ID3D11ShaderResourceView* glowDilSRV = nullptr;
    ID3D11Texture2D* glowCTex = nullptr;     ID3D11UnorderedAccessView* glowCUAV = nullptr;     ID3D11ShaderResourceView* glowCSRV = nullptr;
    ID3D11Texture2D* glowEnvTex = nullptr;   ID3D11UnorderedAccessView* glowEnvUAV = nullptr;   ID3D11ShaderResourceView* glowEnvSRV = nullptr;
    ID3D11Texture2D* glowKTex = nullptr;     ID3D11UnorderedAccessView* glowKUAV = nullptr;     ID3D11ShaderResourceView* glowKSRV = nullptr;

    // LED lag (temporal drive state). driveFilt / driveState / the delay ring are built with the monitor (cols x rows R32F,
    // as the overlay's Build); the four panel-clock textures only while mode 3 is on (EnsureClock).
    FaldTemporalSettings tempSettings;                    // from the tuning tail (mode 0 until the host sends one)
    float refreshMs = 0.0f;                               // this monitor's nominal refresh period (mode 3's grid)
    bool clockFailed = false;                             // latched until the next rebuild
    ID3D11Texture2D* driveFiltTex = nullptr;  ID3D11UnorderedAccessView* driveFiltUAV = nullptr;  ID3D11ShaderResourceView* driveFiltSRV = nullptr;
    ID3D11Texture2D* driveStateTex = nullptr; ID3D11UnorderedAccessView* driveStateUAV = nullptr; ID3D11ShaderResourceView* driveStateSRV = nullptr;
    ID3D11Texture2D* delayTex[FALD_DELAY_MAX] = {}; ID3D11UnorderedAccessView* delayUAV[FALD_DELAY_MAX] = {}; ID3D11ShaderResourceView* delaySRV[FALD_DELAY_MAX] = {};
    ID3D11Texture2D* clkStateTex[2] = {}; ID3D11UnorderedAccessView* clkStateUAV[2] = {}; ID3D11ShaderResourceView* clkStateSRV[2] = {};
    ID3D11Texture2D* clkPrevTex = nullptr; ID3D11UnorderedAccessView* clkPrevUAV = nullptr; ID3D11ShaderResourceView* clkPrevSRV = nullptr;
    ID3D11Texture2D* clkEstTex = nullptr;  ID3D11UnorderedAccessView* clkEstUAV = nullptr;  ID3D11ShaderResourceView* clkEstSRV = nullptr;

    // GPU cost accumulated between log lines (microseconds; from the timestamp queries)
    double gpuSumUs = 0.0, gpuStarSumUs = 0.0, gpuMaxUs = 0.0;
    unsigned int gpuSamples = 0;
};

// The defaults src/fald.h gives FaldStarfieldSettings / FaldGlowSettings (used until the host sends a
// tuning tail — an older host — and for the inert CB words while the feature is off).
static DwmHookFaldTuning DefaultTuning() {
    DwmHookFaldTuning t = {};
    t.starEven = 0.8f; t.starLift = 0.0f; t.starTargetGain = 1.0f; t.starTargetSigma = 0.0f;
    t.starKeepNits = 100.0f; t.starCapNits = 0.0f; t.starStrength = 1.0f; t.starAreaLo = 40.0f;
    t.starAreaHi = 160.0f; t.starPeakHi = 0.0f; t.starNbLo = 0.15f; t.starNbHi = 0.30f;
    t.starReach = 2u; t.starEvenReach = 8u;
    t.glowStrength = 1.0f; t.glowCapNits = 0.05f; t.glowReach = 2u;
    return t;
}

// Shared memory is the host's word, but the integers here size shader loops: bound them whatever
// arrives (the host already clamped with src FaldStarfieldClamp / FaldGlowClamp), and replace NaNs.
static DwmHookFaldTuning SanitizeTuning(const DwmHookFaldTuning& in) {
    const DwmHookFaldTuning d = DefaultTuning();
    DwmHookFaldTuning t = in;
    auto fix = [](float v, float lo, float hi, float dflt) { return (v != v) ? dflt : (v < lo ? lo : (v > hi ? hi : v)); };
    t.starEven = fix(t.starEven, 0.0f, 1.0f, d.starEven);
    t.starLift = fix(t.starLift, 0.0f, 1.0f, d.starLift);
    t.starTargetGain = fix(t.starTargetGain, 0.05f, 2.0f, d.starTargetGain);
    t.starTargetSigma = fix(t.starTargetSigma, 0.0f, 4.0f, d.starTargetSigma);
    t.starKeepNits = fix(t.starKeepNits, 0.0f, 10000.0f, d.starKeepNits);
    t.starCapNits = fix(t.starCapNits, 0.0f, 10000.0f, d.starCapNits);
    t.starStrength = fix(t.starStrength, 0.0f, 1.0f, d.starStrength);
    t.starAreaLo = fix(t.starAreaLo, 0.0f, 1.0e6f, d.starAreaLo);
    t.starAreaHi = fix(t.starAreaHi, t.starAreaLo, 1.0e6f, d.starAreaHi);
    t.starPeakHi = fix(t.starPeakHi, 0.0f, 10000.0f, d.starPeakHi);
    t.starNbLo = fix(t.starNbLo, 0.0f, 1.0f, d.starNbLo);
    t.starNbHi = fix(t.starNbHi, t.starNbLo, 1.0f, d.starNbHi);
    if (t.starReach > 4u) t.starReach = 4u;              // FALD_STAR_REACH_MAX
    if (t.starEvenReach > 12u) t.starEvenReach = 12u;    // FALD_STAR_EVEN_REACH_MAX
    t.glowStrength = fix(t.glowStrength, 0.0f, 1.0f, d.glowStrength);
    t.glowCapNits = fix(t.glowCapNits, 0.005f, 0.5f, d.glowCapNits);   // FALD_GLOW_CAP_MIN / MAX
    if (t.glowReach < 1u) t.glowReach = 1u;              // FALD_GLOW_REACH_MIN
    if (t.glowReach > HOOK_GLOW_REACH_MAX) t.glowReach = HOOK_GLOW_REACH_MAX;
    return t;
}

static void ReleaseClock(FaldMonitor* m) {
    for (unsigned int i = 0; i < 2; i++) { SafeRelease(m->clkStateSRV[i]); SafeRelease(m->clkStateUAV[i]); SafeRelease(m->clkStateTex[i]); }
    SafeRelease(m->clkPrevSRV); SafeRelease(m->clkPrevUAV); SafeRelease(m->clkPrevTex);
    SafeRelease(m->clkEstSRV); SafeRelease(m->clkEstUAV); SafeRelease(m->clkEstTex);
    m->clkElapsed = 0; m->clkIndex = 0; m->clkSeeded = false;   // as src/fald.cpp ReleaseClock
}

static void ReleaseTemporal(FaldMonitor* m) {
    ReleaseClock(m);
    SafeRelease(m->driveFiltSRV); SafeRelease(m->driveFiltUAV); SafeRelease(m->driveFiltTex);
    SafeRelease(m->driveStateSRV); SafeRelease(m->driveStateUAV); SafeRelease(m->driveStateTex);
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++) { SafeRelease(m->delaySRV[i]); SafeRelease(m->delayUAV[i]); SafeRelease(m->delayTex[i]); }
    // the textures are gone: so is the state (src/fald.cpp ReleaseResources does the same on a rebuild)
    m->stateValid = false; m->settleLeft = 0; m->temporalMode = FALD_TEMPORAL_OFF; m->delayCount = 0;
    m->delayHead = 0; m->delayFrames = 0;   // as the overlay's rebuild (dump slots line up)
}

static void ReleaseStar(FaldMonitor* m) {
    SafeRelease(m->starPlan2SRV); SafeRelease(m->starPlan2UAV); SafeRelease(m->starPlan2Tex);
    SafeRelease(m->starBgSRV);    SafeRelease(m->starBgUAV);    SafeRelease(m->starBgTex);
    SafeRelease(m->starPlanSRV);  SafeRelease(m->starPlanUAV);  SafeRelease(m->starPlanTex);
    SafeRelease(m->starWSRV);     SafeRelease(m->starWUAV);     SafeRelease(m->starWTex);
    SafeRelease(m->starStatSRV);  SafeRelease(m->starStatUAV);  SafeRelease(m->starStatTex);
    m->starOn = false;
}

static void ReleaseGlow(FaldMonitor* m) {
    SafeRelease(m->glowKSRV);   SafeRelease(m->glowKUAV);   SafeRelease(m->glowKTex);
    SafeRelease(m->glowEnvSRV); SafeRelease(m->glowEnvUAV); SafeRelease(m->glowEnvTex);
    SafeRelease(m->glowCSRV);   SafeRelease(m->glowCUAV);   SafeRelease(m->glowCTex);
    SafeRelease(m->glowDilSRV); SafeRelease(m->glowDilUAV); SafeRelease(m->glowDilTex);
    SafeRelease(m->glowVSRV);   SafeRelease(m->glowVUAV);   SafeRelease(m->glowVTex);
    m->glowOn = false;
    m->glowBand = false;
}

// One entry per (position, mode) actually seen presenting; fixed-size for the same reason as above.
static FaldMonitor* g_monitors[FALD_MAX_PANEL_FILES] = {};
static int g_numMonitors = 0;

static void ReleaseMonitor(FaldMonitor* m) {
    SafeRelease(m->cb);
    SafeRelease(m->boostLutSRV); SafeRelease(m->boostLutBuf);
    for (int i = 0; i < 2; i++) {
        SafeRelease(m->boostSRV[i]); SafeRelease(m->boostUAV[i]); SafeRelease(m->boostTex[i]);
        SafeRelease(m->activeSRV[i]); SafeRelease(m->activeUAV[i]); SafeRelease(m->activeTex[i]);
    }
    SafeRelease(m->flatEstSRV); SafeRelease(m->flatEstUAV); SafeRelease(m->flatEstTex);
    SafeRelease(m->flatTrueSRV); SafeRelease(m->flatTrueUAV); SafeRelease(m->flatTrueTex);
    SafeRelease(m->gainBSRV); SafeRelease(m->gainBUAV); SafeRelease(m->gainBTex);
    SafeRelease(m->gainASRV); SafeRelease(m->gainAUAV); SafeRelease(m->gainATex);
    SafeRelease(m->bEstSRV); SafeRelease(m->bEstUAV); SafeRelease(m->bEstTex);
    SafeRelease(m->bTrueSRV); SafeRelease(m->bTrueUAV); SafeRelease(m->bTrueTex);
    SafeRelease(m->driveSRV); SafeRelease(m->driveUAV); SafeRelease(m->driveTex);
    SafeRelease(m->kEstSRV); SafeRelease(m->kEstBuf);
    SafeRelease(m->kTrueSRV); SafeRelease(m->kTrueBuf);
    SafeRelease(m->curveSRV); SafeRelease(m->curveTex);
    SafeRelease(m->interSRV); SafeRelease(m->interRTV); SafeRelease(m->interTex);
    SafeRelease(m->cleanSRV); SafeRelease(m->cleanTex);
    ReleaseTemporal(m);
    ReleaseStar(m);
    ReleaseGlow(m);
    m->primed = false;
    m->valid = false;
}

void FaldReleaseAll() {
    for (int i = 0; i < 4; i++) { g_tsInFlight[i] = false; g_tsOwner[i] = nullptr; }   // owners are about to go
    for (int i = 0; i < g_numMonitors; i++) {
        if (!g_monitors[i]) continue;
        ReleaseMonitor(g_monitors[i]);
        if (g_monitors[i]->settleEvt) CloseHandle(g_monitors[i]->settleEvt);
        if (g_monitors[i]->contentEvt) CloseHandle(g_monitors[i]->contentEvt);
        delete g_monitors[i];
        g_monitors[i] = nullptr;
    }
    g_numMonitors = 0;
    g_numPanelFiles = 0;
}

static bool MakeRWTexture(UINT w, UINT h, ID3D11Texture2D** tex, ID3D11UnorderedAccessView** uav,
                          ID3D11ShaderResourceView** srv, DXGI_FORMAT format = DXGI_FORMAT_R32_FLOAT) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = format;
    d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_dev->CreateTexture2D(&d, nullptr, tex))) return false;
    if (FAILED(g_dev->CreateUnorderedAccessView(*tex, nullptr, uav))) return false;
    if (FAILED(g_dev->CreateShaderResourceView(*tex, nullptr, srv))) return false;
    return true;
}

static bool MakeFloatBuffer(const std::vector<float>& data, ID3D11Buffer** buf, ID3D11ShaderResourceView** srv) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = (UINT)(data.size() * sizeof(float));
    bd.Usage = D3D11_USAGE_IMMUTABLE;
    bd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA init = {};
    init.pSysMem = data.data();
    if (FAILED(g_dev->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32_FLOAT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = (UINT)data.size();
    return SUCCEEDED(g_dev->CreateShaderResourceView(*buf, &sd, srv));
}

// The five cols x rows RGBA32F starfield textures (src/fald.cpp EnsureStar). A failure is latched per
// build: logged once, the feature stays off for this monitor until the next rebuild (resize / mode).
static bool EnsureStar(FaldMonitor* m) {
    if (m->starStatTex && m->starWTex && m->starPlanTex && m->starBgTex && m->starPlan2Tex) return true;
    if (m->starFailed || !StarShadersReady()) return false;
    ReleaseStar(m);
    const FaldPanelParams& p = m->params;
    const DXGI_FORMAT f = DXGI_FORMAT_R32G32B32A32_FLOAT;
    if (MakeRWTexture(p.cols, p.rows, &m->starStatTex, &m->starStatUAV, &m->starStatSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &m->starWTex, &m->starWUAV, &m->starWSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &m->starPlanTex, &m->starPlanUAV, &m->starPlanSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &m->starBgTex, &m->starBgUAV, &m->starBgSRV, f) &&
        MakeRWTexture(p.cols, p.rows, &m->starPlan2Tex, &m->starPlan2UAV, &m->starPlan2SRV, f))
        return true;
    ReleaseStar(m);
    m->starFailed = true;
    LogF("FALD: pos(%d,%d) starfield textures could not be created - starfield stays off", m->left, m->top);
    return false;
}

// The five glow-fill zone textures (src/fald.cpp EnsureGlow): Vz, the dilation on the lattice extended
// by the reach maximum on every side, the closing, (Ez, Dz, Cz, Vz), and the band's scale k.
static bool EnsureGlow(FaldMonitor* m) {
    if (m->glowVTex && m->glowDilTex && m->glowCTex && m->glowEnvTex && m->glowKTex) return true;
    if (m->glowFailed || !GlowShadersReady()) return false;
    ReleaseGlow(m);
    const FaldPanelParams& p = m->params;
    const UINT margin = 2u * HOOK_GLOW_REACH_MAX;
    if (MakeRWTexture(p.cols, p.rows, &m->glowVTex, &m->glowVUAV, &m->glowVSRV) &&
        MakeRWTexture(p.cols + margin, p.rows + margin, &m->glowDilTex, &m->glowDilUAV, &m->glowDilSRV) &&
        MakeRWTexture(p.cols, p.rows, &m->glowCTex, &m->glowCUAV, &m->glowCSRV) &&
        MakeRWTexture(p.cols, p.rows, &m->glowEnvTex, &m->glowEnvUAV, &m->glowEnvSRV, DXGI_FORMAT_R32G32B32A32_FLOAT) &&
        MakeRWTexture(p.cols, p.rows, &m->glowKTex, &m->glowKUAV, &m->glowKSRV))
        return true;
    ReleaseGlow(m);
    m->glowFailed = true;
    LogF("FALD: pos(%d,%d) glow fill textures could not be created - glow fill stays off", m->left, m->top);
    return false;
}

// The four cols x rows R32F panel-clock textures (LED lag mode 3; src/fald.cpp EnsureClock). Latched on failure: the
// mode then runs as off until the next rebuild.
static bool EnsureClock(FaldMonitor* m) {
    if (m->clkStateTex[0] && m->clkStateTex[1] && m->clkPrevTex && m->clkEstTex) return true;
    if (m->clockFailed || !g_clockCS) return false;
    ReleaseClock(m);
    const FaldPanelParams& p = m->params;
    if (MakeRWTexture(p.cols, p.rows, &m->clkStateTex[0], &m->clkStateUAV[0], &m->clkStateSRV[0]) &&
        MakeRWTexture(p.cols, p.rows, &m->clkStateTex[1], &m->clkStateUAV[1], &m->clkStateSRV[1]) &&
        MakeRWTexture(p.cols, p.rows, &m->clkPrevTex, &m->clkPrevUAV, &m->clkPrevSRV) &&
        MakeRWTexture(p.cols, p.rows, &m->clkEstTex, &m->clkEstUAV, &m->clkEstSRV))
        return true;
    ReleaseClock(m);
    m->clockFailed = true;
    LogF("FALD: pos(%d,%d) panel clock textures could not be created - LED lag mode 3 runs as off", m->left, m->top);
    return false;
}

// Resolve this frame's starfield / glow state from the live request (FaldSetLiveSettings). Off =
// textures released and nothing downstream knows the feature exists (CB words 35 / 75 / 80 = 0,
// t15 / t18 / t23 / t24 unbound) — the bit-identical-when-off rule of src/fald.cpp.
static void ResolveFeatures(FaldMonitor* m) {
    const bool wasStar = m->starOn, wasGlow = m->glowOn;
    if (m->starWanted) m->starOn = EnsureStar(m);
    else { if (m->starStatTex || m->starOn) ReleaseStar(m); m->starFailed = false; }   // off: the next enable retries
    // glow: only with starfield (the merged feature), only on PQ panel files (HDR measurements)
    const bool glowWanted = m->glowWanted && m->starOn && m->params.transfer == FALD_TRANSFER_PQ;
    if (glowWanted) m->glowOn = EnsureGlow(m);
    else { if (m->glowVTex || m->glowOn) ReleaseGlow(m); m->glowFailed = false; }
    m->glowBand = m->glowOn && m->params.hasBoost && m->params.boostRule == FALD_BOOST_RULE_MEAN;
    if (m->starOn != wasStar || m->glowOn != wasGlow)
        LogF("FALD: pos(%d,%d) %s starfield %s, glow fill %s%s", m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)",
             m->starOn ? "ON" : "off", m->glowOn ? "ON" : "off", m->glowBand ? " (count-threshold band)" : "");
}

static void ComputeFlatResponse(FaldMonitor* m);

static bool BuildMonitor(FaldMonitor* m, const FaldPanelParams& params) {
    ReleaseMonitor(m);
    m->params = params;
    m->starFailed = false;
    m->glowFailed = false;
    const FaldPanelParams& p = m->params;

    if (!FaldLatticeFits(p, (int)m->width, (int)m->height)) {
        LogF("FALD: panel lattice (%ux%u) does not fit monitor %ux%u at pos(%d,%d) — layer off",
             p.cols * p.cellW, p.rows * p.cellH, m->width, m->height, m->left, m->top);
        return false;
    }
    // intermediate (back-buffer format, so the LUT/tonemap pass writes it unchanged)
    {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = m->width; d.Height = m->height; d.MipLevels = 1; d.ArraySize = 1;
        d.Format = m->format; d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
        d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        if (FAILED(g_dev->CreateTexture2D(&d, nullptr, &m->interTex)) ||
            FAILED(g_dev->CreateRenderTargetView(m->interTex, nullptr, &m->interRTV)) ||
            FAILED(g_dev->CreateShaderResourceView(m->interTex, nullptr, &m->interSRV))) {
            log_to_file("FALD: intermediate render target creation failed"); return false;
        }
        // clean source: same format + size as the back buffer, the copy target of its dirty rects
        d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        if (FAILED(g_dev->CreateTexture2D(&d, nullptr, &m->cleanTex)) ||
            FAILED(g_dev->CreateShaderResourceView(m->cleanTex, nullptr, &m->cleanSRV))) {
            log_to_file("FALD: clean source texture creation failed"); return false;
        }
        m->coverCols = (m->width + HOOK_FALD_COVER_TILE - 1) / HOOK_FALD_COVER_TILE;
        m->coverRows = (m->height + HOOK_FALD_COVER_TILE - 1) / HOOK_FALD_COVER_TILE;
        m->cover.assign((size_t)m->coverCols * m->coverRows, (uint8_t)0);
        CoverReset(m);
    }
    // curve LUT (curveN x 1, R32F)
    {
        D3D11_TEXTURE2D_DESC c = {};
        c.Width = p.curveN; c.Height = 1; c.MipLevels = 1; c.ArraySize = 1; c.Format = DXGI_FORMAT_R32_FLOAT;
        c.SampleDesc.Count = 1; c.Usage = D3D11_USAGE_IMMUTABLE; c.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = {};
        init.pSysMem = p.curve.data(); init.SysMemPitch = p.curveN * sizeof(float);
        if (FAILED(g_dev->CreateTexture2D(&c, &init, &m->curveTex)) ||
            FAILED(g_dev->CreateShaderResourceView(m->curveTex, nullptr, &m->curveSRV))) {
            log_to_file("FALD: curve texture creation failed"); return false;
        }
    }
    if (!MakeFloatBuffer(p.kTrue, &m->kTrueBuf, &m->kTrueSRV)) { log_to_file("FALD: kTrue buffer failed"); return false; }
    if (!MakeFloatBuffer(p.kEst, &m->kEstBuf, &m->kEstSRV)) { log_to_file("FALD: kEst buffer failed"); return false; }
    if (!MakeRWTexture(p.cols, p.rows, &m->driveTex, &m->driveUAV, &m->driveSRV)) { log_to_file("FALD: drive texture failed"); return false; }
    // LED lag (modes 1 / 2): the filtered drive, the committed state and the delay ring (as src/fald.cpp Build)
    if (!MakeRWTexture(p.cols, p.rows, &m->driveFiltTex, &m->driveFiltUAV, &m->driveFiltSRV)) { log_to_file("FALD: filtered drive texture failed"); return false; }
    if (!MakeRWTexture(p.cols, p.rows, &m->driveStateTex, &m->driveStateUAV, &m->driveStateSRV)) { log_to_file("FALD: drive state texture failed"); return false; }
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++)
        if (!MakeRWTexture(p.cols, p.rows, &m->delayTex[i], &m->delayUAV[i], &m->delaySRV[i])) { log_to_file("FALD: delay ring texture failed"); return false; }
    m->clockFailed = false;
    const UINT fw = p.cols * p.sub, fh = p.rows * p.sub;
    if (!MakeRWTexture(fw, fh, &m->bTrueTex, &m->bTrueUAV, &m->bTrueSRV)) { log_to_file("FALD: B_true texture failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->bEstTex, &m->bEstUAV, &m->bEstSRV)) { log_to_file("FALD: B_est texture failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->gainATex, &m->gainAUAV, &m->gainASRV)) { log_to_file("FALD: gain texture A failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->gainBTex, &m->gainBUAV, &m->gainBSRV)) { log_to_file("FALD: gain texture B failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->flatTrueTex, &m->flatTrueUAV, &m->flatTrueSRV)) { log_to_file("FALD: flat B_true texture failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->flatEstTex, &m->flatEstUAV, &m->flatEstSRV)) { log_to_file("FALD: flat B_est texture failed"); return false; }
    if (p.hasBoost) {
        for (unsigned int i = 0; i < 2; i++) {
            if (!MakeRWTexture(p.cols, p.rows, &m->activeTex[i], &m->activeUAV[i], &m->activeSRV[i])) { log_to_file("FALD: active-zone texture failed"); return false; }
            if (!MakeRWTexture(2, 1, &m->boostTex[i], &m->boostUAV[i], &m->boostSRV[i])) { log_to_file("FALD: boost texture failed"); return false; }
        }
        std::vector<float> lut;
        for (uint32_t i = 0; i < p.boostN; i++) {
            lut.push_back((float)FaldBoostZoneThreshold(p.boostLo[i], p.cols * p.rows));
            lut.push_back(p.boostVal[i]);
        }
        if (!MakeFloatBuffer(lut, &m->boostLutBuf, &m->boostLutSRV)) { log_to_file("FALD: boost LUT buffer failed"); return false; }
    }
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = FALD_CB_BYTES;
    cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_dev->CreateBuffer(&cbd, nullptr, &m->cb))) { log_to_file("FALD: constant buffer failed"); return false; }

    m->valid = true;
    ComputeFlatResponse(m);
    FaldUnbindAll();   // the flat pass bound CS b0 / s0; a build can end the present without FaldRun (unprimed)
    LogF("FALD: monitor pos(%d,%d) %s ready: %ux%u cells of %ux%u px, sub %u, white %.1f nits, transfer %s, boost %s",
         m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", p.cols, p.rows, p.cellW, p.cellH, p.sub, p.white,
         p.transfer == FALD_TRANSFER_GAMMA ? "gamma" : "PQ", p.hasBoost ? "yes" : "none");
    return true;
}

FaldMonitor* FaldAcquire(int left, int top, bool isHdr, unsigned int width, unsigned int height,
                         DXGI_FORMAT format) {
    if (!FaldShadersReady() || width == 0 || height == 0) return nullptr;

    FaldMonitor* m = nullptr;
    for (int i = 0; i < g_numMonitors; i++) {
        FaldMonitor* e = g_monitors[i];
        if (e && e->left == left && e->top == top && e->isHdr == isHdr) { m = e; break; }
    }
    // The same position's entry for the OTHER mode never sees this mode's presents: its copy and LED-lag state are
    // void, and holding its two full-size frame textures across an HDR <-> SDR switch only costs VRAM. Free it; a
    // switch back rebuilds it fresh (the overlay resets its state on a mode switch too).
    for (int i = 0; i < g_numMonitors; i++) {
        FaldMonitor* e = g_monitors[i];
        if (e && e != m && e->left == left && e->top == top && e->isHdr != isHdr && e->valid) {
            ReleaseMonitor(e);
            CoverReset(e);
        }
    }
    if (m && m->width == width && m->height == height && m->format == format) {
        if (m->failed) return nullptr;
        if (m->valid) return m;
        // released (layer off for a while, or a mode switch): rebuild below
    }

    const FaldPanelFile* pf = FindPanelFile(left, top, isHdr);
    if (!pf) return nullptr;

    if (!m) {
        if (g_numMonitors >= FALD_MAX_PANEL_FILES) return nullptr;
        m = new FaldMonitor();
        m->left = left; m->top = top; m->isHdr = isHdr;
        m->tuning = DefaultTuning();
        // Published last: a concurrent reader either does not see the slot or sees a complete entry.
        g_monitors[g_numMonitors] = m;
        g_numMonitors++;
    }
    // A resize (or the first sight of this monitor) rebuilds and clears the latch: the lattice may
    // fit the new frame even though it did not fit the old one.
    m->width = width; m->height = height; m->format = format;
    m->failed = false;
    if (!BuildMonitor(m, pf->params)) {
        ReleaseMonitor(m);
        m->failed = true;     // logged once inside BuildMonitor; no per-frame retry inside DWM
        return nullptr;
    }
    return m;
}

ID3D11RenderTargetView* FaldIntermediateRTV(FaldMonitor* m) { return m ? m->interRTV : nullptr; }
ID3D11Texture2D* FaldIntermediateTexture(FaldMonitor* m) { return m ? m->interTex : nullptr; }
ID3D11Texture2D* FaldCleanTexture(FaldMonitor* m) { return m ? m->cleanTex : nullptr; }
ID3D11ShaderResourceView* FaldCleanSRV(FaldMonitor* m) { return m ? m->cleanSRV : nullptr; }

// The clean copy went stale (created, or a present of this monitor the layer did not see): every tile owes a refresh.
static void CoverReset(FaldMonitor* m) {
    m->primed = false;
    if (!m->cover.empty()) std::fill(m->cover.begin(), m->cover.end(), (uint8_t)0);
    m->coverLeft = m->coverCols * m->coverRows;
}

// Ask the host for one full-screen recomposition (DWM_HOOK_FALD_PRIME_EVENT): a monitor is waiting for a clean copy
// (this layer's, or the dynamic-peak source in hook_render.cpp). At most once a second across all callers; the event is
// opened lazily. False = not signalled (throttled, or the host has no event yet): ask again on a later present.
bool HookRequestFullRecompose() {
    LARGE_INTEGER now, f;
    QueryPerformanceCounter(&now); QueryPerformanceFrequency(&f);
    if (g_primeLastQpc != 0 && f.QuadPart > 0 && (now.QuadPart - g_primeLastQpc) < f.QuadPart) return false;
    g_primeLastQpc = now.QuadPart;
    if (!g_primeEvent) g_primeEvent = OpenEventW(EVENT_MODIFY_STATE, FALSE, DWM_HOOK_FALD_PRIME_EVENT);
    return g_primeEvent && SetEvent(g_primeEvent);
}

static void RequestPrime() { HookRequestFullRecompose(); }

bool FaldUpdateClean(FaldMonitor* m, ID3D11Texture2D* backBuffer, const RECT* rects, int numRects) {
    if (!m || !m->valid || !m->cleanTex || !backBuffer || !g_ctx) return false;
    m->offPresents = 0;
    const LONG w = (LONG)m->width, h = (LONG)m->height;
    const LONG T = (LONG)HOOK_FALD_COVER_TILE;
    for (int i = 0; i < numRects; i++) {
        const LONG l = rects[i].left > 0 ? rects[i].left : 0;
        const LONG t = rects[i].top > 0 ? rects[i].top : 0;
        const LONG r = rects[i].right < w ? rects[i].right : w;
        const LONG b = rects[i].bottom < h ? rects[i].bottom : h;
        if (r <= l || b <= t) continue;
        D3D11_BOX box = { (UINT)l, (UINT)t, 0, (UINT)r, (UINT)b, 1 };
        g_ctx->CopySubresourceRegion(m->cleanTex, 0, (UINT)l, (UINT)t, 0, backBuffer, 0, &box);
        if (m->primed || m->coverLeft == 0) continue;
        // tiles this rect covers COMPLETELY (a tile clipped by the frame edge counts up to the edge)
        const LONG tx0 = (l + T - 1) / T, ty0 = (t + T - 1) / T;
        const LONG tx1 = (r == w) ? (LONG)m->coverCols : r / T;
        const LONG ty1 = (b == h) ? (LONG)m->coverRows : b / T;
        for (LONG ty = ty0; ty < ty1; ty++)
            for (LONG tx = tx0; tx < tx1; tx++) {
                uint8_t& c = m->cover[(size_t)ty * m->coverCols + (size_t)tx];
                if (!c) { c = 1; m->coverLeft--; }
            }
    }
    if (!m->primed && m->coverLeft == 0) {
        m->primed = true;
        LogF("FALD: pos(%d,%d) %s clean source primed (every pixel refreshed since it went stale)",
             m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)");
    }
    if (!m->primed) {
        FaldTemporalIdle(m);   // the layer does not run this present (src FaldLayerIdle)
        RequestPrime();        // the host recomposes the screen; nothing else would on a static desktop
    }
    return m->primed;
}

// The layer did not run for this present of the monitor: its copy misses this present's rects, the LED-lag state is
// void (src FaldLayerIdle). After HOOK_FALD_RELEASE_AFTER such presents in a row the entry gives its GPU memory back
// (two full-size frame textures + fields); the next FaldAcquire rebuilds it.
static void MarkEntryStale(FaldMonitor* e) {
    CoverReset(e);
    FaldTemporalIdle(e);
    if (e->valid && ++e->offPresents > HOOK_FALD_RELEASE_AFTER) {
        ReleaseMonitor(e);   // valid = false, failed stays false: rebuilt on demand
        e->offPresents = 0;
        LogF("FALD: pos(%d,%d) %s layer off for a while - GPU memory released", e->left, e->top, e->isHdr ? "HDR" : "SDR(ACM)");
    }
}

void FaldMarkStale(int left, int top) {
    for (int i = 0; i < g_numMonitors; i++) {
        FaldMonitor* e = g_monitors[i];
        if (e && e->left == left && e->top == top) MarkEntryStale(e);
    }
}

void FaldMarkAllStale() {
    for (int i = 0; i < g_numMonitors; i++)
        if (g_monitors[i]) MarkEntryStale(g_monitors[i]);
}

void FaldSetLiveSettings(FaldMonitor* m, unsigned int debugMode, int pedMode, bool star, bool glow,
                         const DwmHookFaldTuning* tuning) {
    if (!m) return;
    m->debugMode = debugMode;
    m->pedMode = pedMode ? 1u : 0u;
    m->starWanted = star;
    m->glowWanted = star && glow;
    m->tuning = tuning ? SanitizeTuning(*tuning) : DefaultTuning();
    // LED lag: from the tail (none = off). Bounded here too — the delay indexes a texture array.
    FaldTemporalSettings ts;
    if (tuning) {
        const DwmHookFaldTuning& t = *tuning;
        ts.mode = t.tempMode <= FALD_TEMPORAL_PANEL ? t.tempMode : FALD_TEMPORAL_OFF;
        auto tau = [](float v) { return (v == v && v > 0.0f) ? (v > FALD_TAU_MAX_MS ? FALD_TAU_MAX_MS : v) : 0.0f; };
        ts.tauRiseMs = tau(t.tempTauRiseMs); ts.tauFallMs = tau(t.tempTauFallMs);
        ts.delayFrames = t.tempDelayFrames > FALD_DELAY_MAX ? FALD_DELAY_MAX : t.tempDelayFrames;
        ts.clockClosure = FaldPanelClockClosure(t.tempClockClosure);
        ts.clockParity = FaldPanelClockParity(t.tempClockParity);
        m->refreshMs = (t.refreshMs == t.refreshMs && t.refreshMs > 1.0f && t.refreshMs < 1000.0f) ? t.refreshMs : 0.0f;
    }
    if (!g_temporalCS && (ts.mode == FALD_TEMPORAL_BOTH || ts.mode == FALD_TEMPORAL_TRUE_ONLY)) ts.mode = FALD_TEMPORAL_OFF;
    m->tempSettings = ts;
}

double FaldLastRunMicros(const FaldMonitor* m) { return m ? m->lastRunUs : 0.0; }

// ---------------------------------------------------------------------------------------------
// Passes — mirror of src/fald.cpp with the stateless core only
// ---------------------------------------------------------------------------------------------
// boostOn = false: the flat-lattice normalisation pass (a boost-free conv whatever the file says).
static void FillCB(FaldMonitor* m, uint32_t roundIdx, uint32_t blurDir = 0, bool boostOn = true) {
    const FaldPanelParams& p = m->params;
    D3D11_MAPPED_SUBRESOURCE map;
    if (FAILED(g_ctx->Map(m->cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &map))) return;
    uint32_t* u = (uint32_t*)map.pData; float* f = (float*)map.pData;
    memset(map.pData, 0, FALD_CB_BYTES);
    u[0] = m->width; u[1] = m->height; u[2] = p.cols; u[3] = p.rows;
    u[4] = p.sub; u[5] = p.cellW; u[6] = p.cellH; u[7] = roundIdx;
    u[8] = p.reachTrueC; u[9] = p.reachTrueR; u[10] = p.reachEstC; u[11] = p.reachEstR;
    u[12] = p.curveN; f[13] = p.white; f[14] = p.tmin; f[15] = p.area0;
    f[16] = p.w[0]; f[17] = p.w[1]; f[18] = p.w[2]; f[19] = p.gainMin;
    f[20] = p.gainMax; f[21] = p.driveFloor; f[22] = p.curveLogMin; f[23] = p.curveLogMax;
    u[24] = m->debugMode; u[25] = p.originX; u[26] = p.originY; u[27] = blurDir;
    f[28] = p.fadeLo; f[29] = p.fadeHi; f[30] = p.gainSmoothCells * (float)p.sub;   // sigma in fine samples
    u[31] = p.transfer;                                                             // 0 = PQ (HDR), 1 = gamma (ACM SDR)
    f[32] = p.lumFadeLo; f[33] = p.lumFadeHi;                                       // pixel-luminance fade (nits)
    u[34] = (boostOn && p.hasBoost) ? p.boostN : 0u;                                // black-frame LED boost steps (0 = no term)
    u[35] = m->starOn ? 1u : 0u;                                                    // starfield balancing (fields in t15 / t18)
    const bool perChannel = (m->pedMode == 1) && p.hasPedColour;
    f[36] = p.tmin * p.pedRGB[0]; f[37] = p.tmin * p.pedRGB[1]; f[38] = p.tmin * p.pedRGB[2];
    u[39] = perChannel ? 1u : 0u;
    f[40] = p.chromaGain;
    f[41] = (p.chromaLo < 0.0f) ? p.lumFadeLo : p.chromaLo;
    f[42] = (p.chromaHi < 0.0f) ? p.lumFadeHi : p.chromaHi;
    f[43] = p.sdrGamma;                                                             // panel EOTF exponent (transfer 1)
    // LED lag = the temporal drive state (words 44-47): per-frame blend factors, mode, "no valid state yet" (copy the drive)
    f[44] = m->tempAlphaRise; f[45] = m->tempAlphaFall;
    u[46] = m->temporalMode; u[47] = m->stateValid ? 0u : 1u;
    // black-frame LED boost: the zone activation rule (words 48-51; read only when word 34 != 0)
    f[48] = p.boostLitNits; f[49] = p.boostLitFrac; f[50] = p.boostDimNits; f[51] = p.boostDimFrac;
    // Starfield words 52-65: the clamped settings, written even while the feature is off (the shader
    // reads them only when word 35 is set) — as the overlay does, so the two paths' CBs match.
    const DwmHookFaldTuning& t = m->tuning;
    f[52] = t.starEven; f[53] = t.starLift; f[54] = t.starTargetGain; f[55] = t.starCapNits;
    f[56] = t.starStrength; f[57] = t.starAreaLo; f[58] = t.starAreaHi; f[59] = t.starPeakHi;
    f[60] = t.starNbLo; f[61] = t.starNbHi; u[62] = t.starReach; u[63] = t.starEvenReach;
    f[64] = t.starTargetSigma; f[65] = t.starKeepNits;
    // panel clock (LED lag mode 3; read by pass 1c only): the clocks' weights, then per clock the blends (words 66-71)
    f[66] = m->clkW[0]; f[67] = m->clkW[1];
    f[68] = m->clkFactor[0]; f[69] = m->clkFactor[1]; f[70] = m->clkFactor[2]; f[71] = m->clkFactor[3];
    // black-frame LED boost: the zone rule (words 72-74; read only when word 34 != 0)
    u[72] = p.boostRule; f[73] = p.boostMeanGamma; f[74] = p.boostMeanThresh;
    // glow fill (word 75 = on; words 76-79 read only when it is set / by the glow passes)
    u[75] = m->glowOn ? 1u : 0u;
    f[76] = t.glowStrength; f[77] = t.glowCapNits; u[78] = t.glowReach; f[79] = FaldGlowReqCeil(p);
    u[80] = m->glowBand ? 1u : 0u;                                                  // the count-threshold band (k in t24)
    u[81] = 0u; u[82] = 0u; u[83] = 0u;
    g_ctx->Unmap(m->cb, 0);
}

// t0 is the monitor's intermediate: the composed, LUT/tonemapped frame the caller left there.
// Slots belonging to passes this path does not run stay null — binding the full range every time is
// what keeps a previous pass's view out.
static void BindCommon(FaldMonitor* m, bool compute) {
    ID3D11ShaderResourceView* srvs[FALD_SRV_SLOTS] = {
        m->interSRV, m->curveSRV, m->kTrueSRV, m->kEstSRV, nullptr, nullptr, nullptr,
        m->flatTrueSRV, m->flatEstSRV, nullptr, nullptr, nullptr,
        m->boostLutSRV, nullptr, nullptr,   // t12: null without a boost LUT
        m->starOn ? m->starPlanSRV : nullptr,          // t15: the starfield plan (Balance)
        nullptr, nullptr,                              // t16/t17: star passes only (RunStar)
        m->starOn ? m->starPlan2SRV : nullptr,         // t18: ln background, near, spk (Balance)
        nullptr,                                       // t19: star pass S1 only (RunStar)
        nullptr, nullptr, nullptr,                     // t20-t22: glow passes only (RunGlow)
        m->glowOn ? m->glowEnvSRV : nullptr,           // t23: the glow deficit (GlowAdd)
        m->glowBand ? m->glowKSRV : nullptr            // t24: the band's zone scale (GlowAdd)
    };
    if (compute) {
        g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
        g_ctx->CSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_ctx->CSSetSamplers(0, 1, &g_faldSampler);
    } else {
        g_ctx->PSSetConstantBuffers(0, 1, &m->cb);
        g_ctx->PSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_ctx->PSSetSamplers(0, 1, &g_faldSampler);
    }
}

static void UnbindCompute() {
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    ID3D11UnorderedAccessView* nullUav[FALD_UAV_SLOTS] = {};
    g_ctx->CSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_ctx->CSSetUnorderedAccessViews(0, FALD_UAV_SLOTS, nullUav, nullptr);
    g_ctx->CSSetShader(nullptr, nullptr, 0);
}

static void RunStat(FaldMonitor* m, uint32_t roundIdx) {
    const FaldPanelParams& p = m->params;
    FillCB(m, roundIdx);
    g_ctx->CSSetShader(g_statCS, nullptr, 0);
    BindCommon(m, true);
    if (roundIdx == 1) {
        ID3D11ShaderResourceView* fields[2] = { m->bTrueSRV, m->bEstSRV };
        g_ctx->CSSetShaderResources(5, 2, fields);
        g_ctx->CSSetShaderResources(9, 1, &m->gainBSRV);      // smoothed gain of the previous round
    }
    ID3D11UnorderedAccessView* uavs[2] = { m->driveUAV, m->activeUAV[roundIdx & 1u] };   // u1: null without a boost LUT
    g_ctx->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);                               // (the shader then never writes it)
    g_ctx->Dispatch(p.cols, p.rows, 1);
    UnbindCompute();
}

static void RunBoost(FaldMonitor* m, uint32_t roundIdx) {
    if (!m->params.hasBoost) return;
    const unsigned int k = roundIdx & 1u;
    g_ctx->CSSetShader(g_boostCS, nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(13, 1, &m->activeSRV[k]);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->boostUAV[k], nullptr);
    g_ctx->Dispatch(1, 1, 1);
    UnbindCompute();
}

// boost: this round's 2x1 boost texture (B_true only; null = none — no LUT in the file, or the
// flat-lattice pass, where the CB's boostN is 0 and the shader never reads t14).
// trueDrive / estDrive: the drive maps the real-spread and the estimate kernels see (both the instantaneous drive
// unless LED lag routes a filtered / clock map: FaldTemporalRun::trueMap / estMap).
static void RunConv(FaldMonitor* m, ID3D11ShaderResourceView* trueDrive, ID3D11ShaderResourceView* estDrive,
                    ID3D11ShaderResourceView* boost) {
    const FaldPanelParams& p = m->params;
    g_ctx->CSSetShader(g_convCS, nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(4, 1, &trueDrive);
    g_ctx->CSSetShaderResources(10, 1, &estDrive);
    g_ctx->CSSetShaderResources(14, 1, &boost);
    ID3D11UnorderedAccessView* uavs[2] = { m->bTrueUAV, m->bEstUAV };
    g_ctx->CSSetUnorderedAccessViews(0, 2, uavs, nullptr);
    g_ctx->Dispatch(FaldConvGroupsX(p.cols, p.rows), p.sub * p.sub, 1);   // cells x sub-offsets (fald_shader.h)
    UnbindCompute();
}

// Pass 2b/2c: gain on the fine grid, then a separable Gaussian low-pass (A -> B -> A; final in gainB).
static void RunGain(FaldMonitor* m) {
    const FaldPanelParams& p = m->params;
    const UINT gx = (p.cols * p.sub + 15) / 16, gy = (p.rows * p.sub + 15) / 16;
    g_ctx->CSSetShader(g_gainCS, nullptr, 0);
    BindCommon(m, true);
    ID3D11ShaderResourceView* fields[2] = { m->bTrueSRV, m->bEstSRV };
    g_ctx->CSSetShaderResources(5, 2, fields);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->gainAUAV, nullptr);
    g_ctx->Dispatch(gx, gy, 1);
    UnbindCompute();
    // horizontal: A -> B
    FillCB(m, 1, 0);
    g_ctx->CSSetShader(g_blurCS, nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(9, 1, &m->gainASRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->gainBUAV, nullptr);
    g_ctx->Dispatch(gx, gy, 1);
    UnbindCompute();
    // vertical: B -> A
    FillCB(m, 1, 1);
    g_ctx->CSSetShader(g_blurCS, nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(9, 1, &m->gainBSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->gainAUAV, nullptr);
    g_ctx->Dispatch(gx, gy, 1);
    UnbindCompute();
    // final smoothed gain lives in A; copy to B so consumers always read gainB
    g_ctx->CopyResource(m->gainBTex, m->gainATex);
}

// LED lag pass 1b (modes 1 / 2), mirror of src/fald.cpp RunTemporal: filtered drive = state + a * (drive - state) per
// cell, from the map the panel's pipeline is fed (t4: this round's instantaneous drive, or a delay-ring entry) and the
// state committed after the previous frame (t11); with no valid state the drive is copied.
static void RunTemporal(FaldMonitor* m, ID3D11ShaderResourceView* inDrive) {
    const FaldPanelParams& p = m->params;
    g_ctx->CSSetShader(g_temporalCS, nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(4, 1, &inDrive);
    g_ctx->CSSetShaderResources(11, 1, &m->driveStateSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->driveFiltUAV, nullptr);
    g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

// LED lag pass 1c (mode 3, valid state only), mirror of src/fald.cpp RunPanelClock: the two parity clocks advance in
// place toward the previous frame's round-1 drives (t4 = clkPrev) by the CB's blend factors; u0 / u1 receive the maps
// the kernels see in both rounds. Binds only what it reads; all four UAV slots are cleared here.
static void RunPanelClock(FaldMonitor* m) {
    const FaldPanelParams& p = m->params;
    FillCB(m, 0);
    g_ctx->CSSetShader(g_clockCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(4, 1, &m->clkPrevSRV);
    ID3D11UnorderedAccessView* uavs[4] = { m->driveFiltUAV, m->clkEstUAV, m->clkStateUAV[0], m->clkStateUAV[1] };
    g_ctx->CSSetUnorderedAccessViews(0, 4, uavs, nullptr);
    g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();   // clears all FALD_UAV_SLOTS (4)
}

// Starfield balancing, mirror of src/fald.cpp RunStar: S0 star statistic of the SOURCE frame -> S1
// tapered protection + zone weights -> S2 target + plan; every later pass samples plan (t15) and plan2
// (t18). A pure function of the source frame in the intermediate, recomputed every run. The passes
// bind only what they read (never BindCommon: that would bind plan / plan2 as SRVs while S1 / S2
// write them).
static void RunStar(FaldMonitor* m) {
    const FaldPanelParams& p = m->params;
    FillCB(m, 0);
    ID3D11ShaderResourceView* in2[2] = { m->interSRV, m->curveSRV };
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetSamplers(0, 1, &g_faldSampler);
    // S0: per zone stat = (peak, speck-zone flag, sparse, solid) + bg = (ln b, brightest pixel's index, lit sum, a_eff)
    ID3D11UnorderedAccessView* out0[2] = { m->starStatUAV, m->starBgUAV };
    g_ctx->CSSetShader(g_starStatCS, nullptr, 0);
    g_ctx->CSSetShaderResources(0, 2, in2);
    g_ctx->CSSetUnorderedAccessViews(0, 2, out0, nullptr);
    g_ctx->Dispatch(p.cols, p.rows, 1);
    UnbindCompute();
    // S1: tapered protection field + flank test + zone weights
    ID3D11UnorderedAccessView* out1[2] = { m->starWUAV, m->starPlan2UAV };
    g_ctx->CSSetShader(g_starWeightCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(16, 1, &m->starStatSRV);
    g_ctx->CSSetShaderResources(19, 1, &m->starBgSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 2, out1, nullptr);
    g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
    // S2: (w0_field, ln target, ln lift, ln peak)
    ID3D11ShaderResourceView* in3[2] = { m->starStatSRV, m->starWSRV };
    g_ctx->CSSetShader(g_starPlanCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(16, 2, in3);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->starPlanUAV, nullptr);
    g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

// Glow fill, mirror of src/fald.cpp RunGlow, after EACH round's gain: G0 zone pedestal of this round's
// B_true -> G1 box maximum -> G2 box minimum (the closing) -> G3 blur + deficit; G4 (band only) the
// zone scale k. The statistic round 1 / the pixel pass then sample glowEnv (t23) and k (t24).
static void RunGlow(FaldMonitor* m) {
    const FaldPanelParams& p = m->params;
    FillCB(m, 0);
    const UINT gz = (p.cols + 15) / 16, gzy = (p.rows + 15) / 16;
    const UINT ge = (p.cols + 2 * HOOK_GLOW_REACH_MAX + 15) / 16, gey = (p.rows + 2 * HOOK_GLOW_REACH_MAX + 15) / 16;
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    // G0
    g_ctx->CSSetShader(g_glowZoneCS, nullptr, 0);
    g_ctx->CSSetShaderResources(5, 1, &m->bTrueSRV);
    g_ctx->CSSetShaderResources(7, 1, &m->flatTrueSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->glowVUAV, nullptr);
    g_ctx->Dispatch(gz, gzy, 1);
    UnbindCompute();
    // G1
    g_ctx->CSSetShader(g_glowDilateCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(20, 1, &m->glowVSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->glowDilUAV, nullptr);
    g_ctx->Dispatch(ge, gey, 1);
    UnbindCompute();
    // G2
    g_ctx->CSSetShader(g_glowErodeCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(21, 1, &m->glowDilSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->glowCUAV, nullptr);
    g_ctx->Dispatch(gz, gzy, 1);
    UnbindCompute();
    // G3
    g_ctx->CSSetShader(g_glowEnvCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(20, 1, &m->glowVSRV);
    g_ctx->CSSetShaderResources(22, 1, &m->glowCSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->glowEnvUAV, nullptr);
    g_ctx->Dispatch(gz, gzy, 1);
    UnbindCompute();
    // G4 (band only, every round): k is unbound as an SRV while it is written
    if (m->glowBand) {
        g_ctx->CSSetShader(g_glowBandCS, nullptr, 0);
        BindCommon(m, true);
        ID3D11ShaderResourceView* fields[2] = { m->bTrueSRV, m->bEstSRV };
        g_ctx->CSSetShaderResources(5, 2, fields);
        g_ctx->CSSetShaderResources(9, 1, &m->gainBSRV);
        ID3D11ShaderResourceView* none = nullptr;
        g_ctx->CSSetShaderResources(24, 1, &none);
        g_ctx->CSSetUnorderedAccessViews(0, 1, &m->glowKUAV, nullptr);
        g_ctx->Dispatch(p.cols, p.rows, 1);
        UnbindCompute();
    }
}

// Flat-lattice response: run the convolution once on a drive map of ones and keep the two fields.
// Must run after the fine textures exist; the flat textures are bound as SRVs t7/t8 from then on
// (they are null during this call, which the conv pass does not read).
static void ComputeFlatResponse(FaldMonitor* m) {
    const float one[4] = { 1.0f, 1.0f, 1.0f, 1.0f };
    g_ctx->ClearUnorderedAccessViewFloat(m->driveUAV, one);
    ID3D11ShaderResourceView* saveT = m->flatTrueSRV; ID3D11ShaderResourceView* saveE = m->flatEstSRV;
    m->flatTrueSRV = nullptr; m->flatEstSRV = nullptr;          // not inputs of this pass
    FillCB(m, 0, 0, false);                                     // boost 1: the normalisation is the un-boosted lattice
    RunConv(m, m->driveSRV, m->driveSRV, nullptr);
    m->flatTrueSRV = saveT; m->flatEstSRV = saveE;
    g_ctx->CopyResource(m->flatTrueTex, m->bTrueTex);
    g_ctx->CopyResource(m->flatEstTex, m->bEstTex);
}

// ---------------------------------------------------------------------------------------------
// One-shot dump (development instrument for the acceptance gate)
// ---------------------------------------------------------------------------------------------
// A dump costs a staging copy, a Map (which stalls the GPU) and a file write. Per frame in DWM's
// present path that is the stall recorded in HANDOFF_HAGS_FLIPQUEUE_2026-09-06.md, so it is armed
// once by a trigger file, consumed by the next run, and disarmed immediately.
static std::wstring g_dumpDir;        // non-empty = the next run dumps, then clears it

static const char* FaldDumpTriggerPath() {
    static char path[MAX_PATH] = {};
    if (path[0] == '\0')
        ExpandEnvironmentStringsA("%SYSTEMROOT%\\Temp\\DesktopLUT_hook_fald_dump.txt", path, sizeof(path));
    return path;
}

void FaldPollDumpRequest() {
    if (!g_dumpDir.empty()) return;                 // one already armed
    // Throttled hard: this is a file-system probe, and doing one per Present is precisely the
    // synchronous-IO-in-the-present-path mistake recorded in HANDOFF_HAGS_FLIPQUEUE_2026-09-06.md.
    // ~5 s between probes at 60 Hz, which is fast enough for a human arming a dump by hand.
    static unsigned int tick = 0;
    static bool disabled = false;                   // a trigger DWM cannot consume: stop probing for this attach
    if (disabled || (tick++ % 300u) != 0u) return;
    const char* trigger = FaldDumpTriggerPath();
    if (GetFileAttributesA(trigger) == INVALID_FILE_ATTRIBUTES) return;
    char line[MAX_PATH] = {};
    FILE* f = fopen(trigger, "r");
    if (f) {
        if (!fgets(line, sizeof(line), f)) line[0] = '\0';
        fclose(f);
    }
    if (!DeleteFileA(trigger)) {                    // consume it whatever happens next — or never look again: an
        disabled = true;                            // unreadable / undeletable file would be re-probed forever
        LogF("FALD: dump trigger %s cannot be consumed by DWM (grant it Everyone:(F)) - dump polling off", trigger);
        return;
    }
    for (char* q = line; *q; q++) if (*q == '\r' || *q == '\n') { *q = '\0'; break; }
    if (line[0] == '\0') return;
    wchar_t dirW[MAX_PATH] = {};
    if (MultiByteToWideChar(CP_ACP, 0, line, -1, dirW, MAX_PATH) == 0) return;
    g_dumpDir = dirW;
    if (!g_dumpDir.empty() && g_dumpDir.back() != L'\\' && g_dumpDir.back() != L'/') g_dumpDir += L'\\';
    LogF("FALD: field dump armed for the next frame -> %s", line);
}

static void DumpTexture(ID3D11Texture2D* tex, const std::wstring& file, UINT w, UINT h, UINT bytesPerPx) {
    if (!tex) return;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_dev->CreateTexture2D(&d, nullptr, &st))) return;
    g_ctx->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE map;
    if (SUCCEEDED(g_ctx->Map(st, 0, D3D11_MAP_READ, 0, &map))) {
        FILE* f = _wfopen(file.c_str(), L"wb");
        if (f) {
            for (UINT y = 0; y < h; y++)
                fwrite((const char*)map.pData + (size_t)y * map.RowPitch, 1, (size_t)w * bytesPerPx, f);
            fclose(f);
        }
        g_ctx->Unmap(st, 0);
    }
    st->Release();
}

// The overlay's runtime.fald_dump field files, same names and layouts so the two are diffed directly (and both
// against dlc/fald/gpuemu.py). The run's LED-lag bookkeeping goes to fald_hook_temporal.txt with the overlay's key
// names (the hook writes no fald_dump.txt: the rest of that text is host-side state).
static void DumpFields(FaldMonitor* m, const std::wstring& dir) {
    const FaldPanelParams& p = m->params;
    DumpTexture(m->driveTex, dir + L"fald_drive.f32", p.cols, p.rows, 4);
    DumpTexture(m->bTrueTex, dir + L"fald_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(m->bEstTex, dir + L"fald_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(m->flatTrueTex, dir + L"fald_flat_btrue.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(m->flatEstTex, dir + L"fald_flat_best.f32", p.cols * p.sub, p.rows * p.sub, 4);
    DumpTexture(m->gainBTex, dir + L"fald_gain_fine.f32", p.cols * p.sub, p.rows * p.sub, 4);
    if (p.hasBoost) {
        DumpTexture(m->activeTex[0], dir + L"fald_active_r0.f32", p.cols, p.rows, 4);
        DumpTexture(m->activeTex[1], dir + L"fald_active.f32", p.cols, p.rows, 4);
        DumpTexture(m->boostTex[0], dir + L"fald_boost_r0.f32", 2, 1, 4);
        DumpTexture(m->boostTex[1], dir + L"fald_boost_r1.f32", 2, 1, 4);
    }
    // LED lag: the same files as the overlay's dump (src/fald.cpp DumpFields), dumped before the commit
    if (m->temporalMode == FALD_TEMPORAL_PANEL) {
        if (!m->clkSeeded) {
            DumpTexture(m->driveFiltTex, dir + L"fald_drive_filt.f32", p.cols, p.rows, 4);
            DumpTexture(m->clkEstTex, dir + L"fald_clock_est.f32", p.cols, p.rows, 4);
            DumpTexture(m->clkStateTex[0], dir + L"fald_clock_s0.f32", p.cols, p.rows, 4);
            DumpTexture(m->clkStateTex[1], dir + L"fald_clock_s1.f32", p.cols, p.rows, 4);
            DumpTexture(m->clkPrevTex, dir + L"fald_clock_dprev.f32", p.cols, p.rows, 4);
        }
    } else if (m->temporalMode != FALD_TEMPORAL_OFF) {
        DumpTexture(m->driveFiltTex, dir + L"fald_drive_filt.f32", p.cols, p.rows, 4);
        DumpTexture(m->driveStateTex, dir + L"fald_drive_state.f32", p.cols, p.rows, 4);
    }
    // the run's LED-lag bookkeeping, for replaying it offline (the overlay writes these in fald_dump.txt)
    {
        FILE* f = _wfopen((dir + L"fald_hook_temporal.txt").c_str(), L"w");
        if (f) {
            // key names = the overlay's fald_dump.txt temporal / clock lines (DLC test_fald_paneltime_warp.py _meta)
            fprintf(f, "temporal_mode %u\n"
                       "state_valid %d\n"
                       "tau_rise_ms %.9g\n"
                       "tau_fall_ms %.9g\n"
                       "delay_frames %u (ring %u)\n"
                       "temp_alpha_rise %.9g\n"
                       "temp_alpha_fall %.9g\n"
                       "dt_ms %.9g\n"
                       "settle_left %u (before this run's accounting)\n"
                       "clock_seed %d\n"
                       "clock_time_ms %.4f\n"
                       "clock_grid_ms %.6f\n"
                       "clock_lock_residual %.6f\n"
                       "clock_lock_gain %.6f\n"
                       "clock_refresh_ms %.6f\n"
                       "clock_refresh_index %llu\n"
                       "clock_elapsed_refreshes %llu\n"
                       "clock_factors %.9g %.9g %.9g %.9g\n"
                       "clock_weights %.9g %.9g\n"
                       "clock_closure %.9g\n"
                       "clock_parity %d\n",
                    m->temporalMode, m->stateValid ? 1 : 0, m->tempSettings.tauRiseMs, m->tempSettings.tauFallMs,
                    m->delayFrames, m->delayCount, m->tempAlphaRise, m->tempAlphaFall, m->dtMs, m->settleLeft,
                    m->clkSeeded ? 1 : 0, m->clkTimeMs, m->clkGridMs, m->clkResidual, m->clkGain, m->refreshMs,
                    m->clkIndex, m->clkElapsed, m->clkFactor[0], m->clkFactor[1], m->clkFactor[2], m->clkFactor[3],
                    m->clkW[0], m->clkW[1], m->clkClosure, m->clkParity);
            fclose(f);
        }
    }
    if (m->starOn) {   // same files as the overlay's dump (src/fald.cpp DumpFields)
        DumpTexture(m->starStatTex, dir + L"fald_star_stat.f32", p.cols, p.rows, 16);
        DumpTexture(m->starWTex, dir + L"fald_star_w.f32", p.cols, p.rows, 16);
        DumpTexture(m->starPlanTex, dir + L"fald_star_plan.f32", p.cols, p.rows, 16);
        DumpTexture(m->starPlan2Tex, dir + L"fald_star_plan2.f32", p.cols, p.rows, 16);
        DumpTexture(m->starBgTex, dir + L"fald_star_bg.f32", p.cols, p.rows, 16);
    }
    if (m->glowOn) {
        DumpTexture(m->glowVTex, dir + L"fald_glow_vz.f32", p.cols, p.rows, 4);
        DumpTexture(m->glowEnvTex, dir + L"fald_glow_env.f32", p.cols, p.rows, 16);
        if (m->glowBand) DumpTexture(m->glowKTex, dir + L"fald_glow_k.f32", p.cols, p.rows, 4);
    }
}

static void DumpFrame(ID3D11ShaderResourceView* srv, const std::wstring& file, UINT w, UINT h) {
    if (!srv) return;
    ID3D11Resource* res = nullptr;
    srv->GetResource(&res);
    if (!res) return;
    ID3D11Texture2D* tex = nullptr;
    if (SUCCEEDED(res->QueryInterface(IID_PPV_ARGS(&tex))) && tex) {
        DumpTexture(tex, file, w, h, 8);   // FP16 scRGB only: the layer never runs on another format
        tex->Release();
    }
    res->Release();
}

static void DumpOutput(ID3D11RenderTargetView* rtv, const std::wstring& file, UINT w, UINT h) {
    if (!rtv) return;
    ID3D11Resource* res = nullptr;
    rtv->GetResource(&res);
    if (!res) return;
    ID3D11Texture2D* tex = nullptr;
    if (SUCCEEDED(res->QueryInterface(IID_PPV_ARGS(&tex))) && tex) {
        DumpTexture(tex, file, w, h, 8);
        tex->Release();
    }
    res->Release();
}

// ---------------------------------------------------------------------------------------------
// The run
// ---------------------------------------------------------------------------------------------
// Everything this binds is cleared before returning: a stale SRV hands DWM's own shaders a wrong
// texture and a stale UAV hands them a GPU fault, and DWM reuses this immediate context for its
// own rendering the moment we return.
static void FaldUnbindAll() {
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    ID3D11UnorderedAccessView* nullUav[FALD_UAV_SLOTS] = {};
    ID3D11SamplerState* nullSamp[1] = {};
    ID3D11Buffer* nullCB = nullptr;
    g_ctx->CSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_ctx->CSSetUnorderedAccessViews(0, FALD_UAV_SLOTS, nullUav, nullptr);
    g_ctx->CSSetSamplers(0, 1, nullSamp);
    g_ctx->CSSetConstantBuffers(0, 1, &nullCB);
    g_ctx->CSSetShader(nullptr, nullptr, 0);
    g_ctx->PSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_ctx->PSSetSamplers(0, 1, nullSamp);
    g_ctx->PSSetConstantBuffers(0, 1, &nullCB);
    g_ctx->PSSetShader(nullptr, nullptr, 0);
    g_ctx->VSSetShader(nullptr, nullptr, 0);
    g_ctx->OMSetRenderTargets(0, nullptr, nullptr);
}

// ---------------------------------------------------------------------------------------------
// GPU timing — a ring of 4 query sets, read back with DONOTFLUSH only once the GPU is done with them,
// so measuring never waits inside DWM's present path. A slot still in flight when its turn comes
// round simply skips timing that frame.
// ---------------------------------------------------------------------------------------------
static void GpuTimingCollect(FaldMonitor*) {
    for (int i = 0; i < 4; i++) {
        if (!g_tsInFlight[i]) continue;
        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT dj = {};
        if (g_ctx->GetData(g_tsDisjoint[i], &dj, sizeof(dj), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) continue;
        UINT64 s[3] = {};
        bool ok = true;
        for (int k = 0; k < 3 && ok; k++)
            ok = (g_ctx->GetData(g_tsStamp[i][k], &s[k], sizeof(UINT64), D3D11_ASYNC_GETDATA_DONOTFLUSH) == S_OK);
        if (!ok) continue;
        g_tsInFlight[i] = false;
        FaldMonitor* o = g_tsOwner[i];
        g_tsOwner[i] = nullptr;
        if (!o || dj.Disjoint || dj.Frequency == 0 || s[2] < s[0] || s[1] < s[0]) continue;
        const double total = (double)(s[2] - s[0]) * 1e6 / (double)dj.Frequency;
        const double star = (double)(s[1] - s[0]) * 1e6 / (double)dj.Frequency;
        o->gpuSumUs += total;
        o->gpuStarSumUs += star;
        if (total > o->gpuMaxUs) o->gpuMaxUs = total;
        o->gpuSamples++;
    }
}

// Returns the slot started for this run, or -1 (no queries, or the slot is still in flight).
static int GpuTimingBegin() {
    const int i = (int)(g_tsHead % 4u);
    if (!g_tsDisjoint[i] || !g_tsStamp[i][0] || !g_tsStamp[i][1] || !g_tsStamp[i][2] || g_tsInFlight[i]) return -1;
    g_tsHead++;
    g_ctx->Begin(g_tsDisjoint[i]);
    g_ctx->End(g_tsStamp[i][0]);
    return i;
}

static void GpuTimingStamp(int slot, int k) {
    if (slot >= 0) g_ctx->End(g_tsStamp[slot][k]);
}

static void GpuTimingEnd(int slot, FaldMonitor* m) {
    if (slot < 0) return;
    g_ctx->End(g_tsStamp[slot][2]);
    g_ctx->End(g_tsDisjoint[slot]);
    g_tsInFlight[slot] = true;
    g_tsOwner[slot] = m;
}

bool FaldRun(FaldMonitor* m, ID3D11RenderTargetView* dstRTV, bool newContent) {
    if (!m || !m->valid || !m->interSRV || !dstRTV) return false;

    LARGE_INTEGER t0, t1, freq;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);

    const std::wstring dumpDir = g_dumpDir;
    g_dumpDir.clear();                       // one frame only, whatever happens below

    ResolveFeatures(m);                      // starfield / glow fill on or off for THIS frame

    // GPU timing: collect finished slots (no flush, no wait), then start this run's slot if it is free.
    GpuTimingCollect(m);
    const int ts = GpuTimingBegin();

    // LED lag (temporal drive state): the bookkeeping is shared/fald_temporal.cpp, the SAME code the overlay runs
    // (FaldTemporalBeginRun / EndRun); only the D3D side is here. The panel clock's textures exist only in mode 3.
    bool clockOk = false;
    if (m->tempSettings.mode == FALD_TEMPORAL_PANEL) clockOk = EnsureClock(m);
    else if (m->clkStateTex[0] || m->clkStateTex[1] || m->clkPrevTex || m->clkEstTex) ReleaseClock(m);
    LARGE_INTEGER qpcNow;
    QueryPerformanceCounter(&qpcNow);
    const FaldTemporalRun trun = FaldTemporalBeginRun(m, m->tempSettings, clockOk, qpcNow.QuadPart, freq.QuadPart, m->refreshMs);
    auto mapSrv = [m](FaldDriveMap k) {
        return k == FALD_MAP_FILTERED ? m->driveFiltSRV : (k == FALD_MAP_CLOCK_EST ? m->clkEstSRV : m->driveSRV);
    };
    ID3D11ShaderResourceView* inDrive = trun.delayedSlot >= 0 ? m->delaySRV[trun.delayedSlot] : m->driveSRV;
    ID3D11ShaderResourceView* trueDrive = mapSrv(trun.trueMap);
    ID3D11ShaderResourceView* estDrive = mapSrv(trun.estMap);

    // No render target may be bound while the passes write their UAVs.
    g_ctx->OMSetRenderTargets(0, nullptr, nullptr);

    // Starfield first: every pass below reads Balance(source) from its plan.
    if (m->starOn) RunStar(m);
    GpuTimingStamp(ts, 1);
    if (trun.clock.runPass) RunPanelClock(m);   // mode 3: this frame's LED state comes from PAST frames only

    // Two inverse rounds. Round 0's output is what round 1's statistic and boost count see, so the
    // panel's own response is accounted for in the frame it actually receives. Glow fill runs after
    // each round's gain: round 0's fill is part of what round 1 sees, round 1's is in the output.
    // LED lag modes 1 / 2 filter each round's drive (both rounds read the SAME committed state).
    RunStat(m, 0);
    RunBoost(m, 0);
    if (trun.temporal) RunTemporal(m, inDrive);
    RunConv(m, trueDrive, estDrive, m->boostSRV[0]);
    RunGain(m);
    if (m->glowOn) RunGlow(m);
    RunStat(m, 1);
    RunBoost(m, 1);
    if (trun.temporal) RunTemporal(m, inDrive);
    RunConv(m, trueDrive, estDrive, m->boostSRV[1]);
    RunGain(m);
    if (m->glowOn) RunGlow(m);
    m->framesRun++;

    if (!dumpDir.empty()) {                   // before the commit: the state files are the maps the passes read
        DumpFields(m, dumpDir);
        DumpFrame(m->interSRV, dumpDir + L"fald_frame.rgba16f", m->width, m->height);
    }

    // LED lag commit (the copies; FaldTemporalEndRun below commits the indices and does the settle accounting)
    if (trun.temporal) {                      // round 1's filtered map becomes the state; the ring takes round 1's
        g_ctx->CopyResource(m->driveStateTex, m->driveFiltTex);   // INSTANTANEOUS map
        if (trun.delay > 0) g_ctx->CopyResource(m->delayTex[m->delayHead], m->driveTex);
    }
    if (trun.panel) {                         // round 1's instantaneous map is the next frame's target; a seeding run
        if (trun.clock.seedStates) {          // takes the panel as settled on this frame (both clocks)
            g_ctx->CopyResource(m->clkStateTex[0], m->driveTex);
            g_ctx->CopyResource(m->clkStateTex[1], m->driveTex);
        }
        if (trun.clock.commitPrev) g_ctx->CopyResource(m->clkPrevTex, m->driveTex);
    }
    FaldTemporalEndRun(m, trun, m->tempSettings, newContent);

    // pixel pass: source + fields -> the back buffer (fullscreen triangle, no vertex buffer)
    FillCB(m, 1);
    const D3D11_VIEWPORT vp = { 0.0f, 0.0f, (float)m->width, (float)m->height, 0.0f, 1.0f };
    g_ctx->RSSetViewports(1, &vp);
    g_ctx->OMSetRenderTargets(1, &dstRTV, nullptr);
    g_ctx->IASetInputLayout(nullptr);
    g_ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    g_ctx->VSSetShader(g_faldVS, nullptr, 0);
    g_ctx->PSSetShader(g_faldPS, nullptr, 0);
    BindCommon(m, false);
    ID3D11ShaderResourceView* fields[2] = { m->bTrueSRV, m->bEstSRV };
    g_ctx->PSSetShaderResources(5, 2, fields);
    g_ctx->PSSetShaderResources(9, 1, &m->gainBSRV);
    // t4 / t10 = debug view 7: instantaneous vs filtered (mode 3: vs the clocks' mean LED state of this frame)
    ID3D11ShaderResourceView* filt = mapSrv(trun.debugFiltMap);
    g_ctx->PSSetShaderResources(4, 1, &m->driveSRV);
    g_ctx->PSSetShaderResources(10, 1, &filt);
    g_ctx->PSSetShaderResources(13, 1, &m->activeSRV[1]);   // debug view 8 (null without a boost LUT)
    g_ctx->Draw(3, 0);
    GpuTimingEnd(ts, m);

    if (!dumpDir.empty()) {
        DumpOutput(dstRTV, dumpDir + L"fald_out.rgba16f", m->width, m->height);
        LogF("FALD: field dump written (pos %d,%d, %s)", m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)");
    }

    FaldUnbindAll();

    // LED-lag settle hold: DWM presents nothing on a static desktop, so while this monitor still owes settle frames ask
    // the host to keep DWM composing it (dwm_hook_config.h DWM_HOOK_FALD_SETTLE_EVENT). One SetEvent per run while
    // pending — no wait, no I/O. The host creates the event; opening it is retried rarely, never per frame.
    // LED-lag settle hold (dwm_hook_config.h DWM_HOOK_FALD_SETTLE_EVENT_FMT): while this monitor owes settle frames
    // ask the host to keep DWM composing it; CONTENT tells it content is flowing anyway (no kick needed then).
    // SetEvent only — no wait, no I/O. Events are opened rarely (the host creates them), never per frame.
    const bool pending = FaldTemporalSettlePending(m);
    if (pending || newContent) {
        if ((!m->settleEvt || !m->contentEvt) && (m->evtOpenTick++ % 120u) == 0u) {
            wchar_t name[96];
            if (!m->settleEvt) {
                swprintf_s(name, DWM_HOOK_FALD_SETTLE_EVENT_FMT, m->left, m->top);
                m->settleEvt = OpenEventW(EVENT_MODIFY_STATE, FALSE, name);
            }
            if (!m->contentEvt) {
                swprintf_s(name, DWM_HOOK_FALD_CONTENT_EVENT_FMT, m->left, m->top);
                m->contentEvt = OpenEventW(EVENT_MODIFY_STATE, FALSE, name);
            }
        }
        if (newContent && m->contentEvt) SetEvent(m->contentEvt);
        if (pending && m->settleEvt) SetEvent(m->settleEvt);
    }

    QueryPerformanceCounter(&t1);
    if (freq.QuadPart > 0)
        m->lastRunUs = (double)(t1.QuadPart - t0.QuadPart) * 1e6 / (double)freq.QuadPart;
    // The cost of the whole layer, logged rarely: overrunning DWM's present budget stutters the whole
    // desktop, and a number beats a guess. ~every 10 s at 60 Hz. CPU = submission time in the present
    // path; GPU = the timestamp queries' average / max over the interval, and the starfield share.
    if ((m->framesRun % 600ull) == 1ull) {
        if (m->gpuSamples > 0)
            LogF("FALD: pos(%d,%d) %s frame %llu, CPU %.0f us | GPU avg %.0f us, max %.0f us, starfield %.0f us (%u samples) [star %s, glow %s]",
                 m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->framesRun, m->lastRunUs,
                 m->gpuSumUs / m->gpuSamples, m->gpuMaxUs, m->gpuStarSumUs / m->gpuSamples, m->gpuSamples,
                 m->starOn ? "on" : "off", m->glowOn ? (m->glowBand ? "on+band" : "on") : "off");
        else
            LogF("FALD: pos(%d,%d) %s frame %llu, CPU %.0f us/frame in the present path (no GPU timing yet) [star %s, glow %s]",
                 m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->framesRun, m->lastRunUs,
                 m->starOn ? "on" : "off", m->glowOn ? "on" : "off");
        m->gpuSumUs = m->gpuStarSumUs = m->gpuMaxUs = 0.0;
        m->gpuSamples = 0;
    }
    return true;
}
