// DesktopLUT DWM Hook - hook_fald.cpp
// The FALD correction inside dwm.exe: the whole layer the overlay path runs — the core, starfield, and LED lag (the
// temporal drive state, bookkeeping shared with the overlay in shared/fald_temporal.cpp).
//
// This file deliberately mirrors src/fald.cpp pass for pass and constant-buffer word for word, running the same HLSL
// (shared/fald_shader.h), so for the same input frame the two paths must produce the same output — the acceptance
// gate, checked with the one-shot field dump (FaldPollDumpRequest) against the overlay and dlc/fald/gpuemu.py.
#include "pch.h"
#include "hook_fald.h"
#include "../shared/fald_change.h"
#include "hook_log.h"
#include "fald_shader.h"
#include "fald_temporal.h"
#include "fald_panel_live.h"
#include "fald_fade.h"
#include "fald_fade_governor.h"
#include "fald_core_governor.h"
#include "fald_exempt.h"

#include <cmath>
#include <cstdarg>
#include <cstring>
#include <string>
#include <algorithm>
#include <atomic>
#include <new>
#include <vector>

// ---------------------------------------------------------------------------------------------
// Process-global shaders (compiled once per attach on DWM's device)
// ---------------------------------------------------------------------------------------------
static ID3D11Device* g_dev = nullptr;
static ID3D11DeviceContext* g_ctx = nullptr;

static ID3D11ComputeShader* g_statCS = nullptr;
static ID3D11ComputeShader* g_convCS[FALD_CONV_SO_VARIANTS] = {};   // pass 2, one per sub-offsets-per-thread variant
static ID3D11ComputeShader* g_gainCS = nullptr;
static ID3D11ComputeShader* g_blurCS = nullptr;
static ID3D11ComputeShader* g_gainBlurCS = nullptr;   // passes 2b + 2c fused (fald_shader.h g_faldGainBlurSource)
static ID3D11ComputeShader* g_decideCS = nullptr;     // the change-driven path's decide pass (g_faldDecideSource)
// The statistic pass's combine variant (work guide C14; fald_shader.h above ZoneSlices): folds the slice partials of a
// lattice whose zones hold more than FALD_ZONE_SLICE_PX pixels. Needed by such lattices only (BuildMonitor refuses one
// without it); a lattice of one-slice zones never dispatches it, so a compile failure here costs nothing there.
static ID3D11ComputeShader* g_statCombineCS = nullptr;
static ID3D11PixelShader* g_faldPS = nullptr;
static ID3D11VertexShader* g_faldVS = nullptr;
static ID3D11SamplerState* g_faldSampler = nullptr;
// DesktopLUT's own windows while a debug view is on (shared/fald_exempt.h): the pixel pass again, scissored, the view off.
// NOT part of FaldShadersReady: without it a view covers the whole screen, as before.
static ID3D11RasterizerState* g_exemptRS = nullptr;

// Starfield feature (starfield balancing S0-S2). Compiled beside the core
// but NOT part of FaldShadersReady: a compile failure here costs the feature, never the correction.
static ID3D11ComputeShader* g_starStatCS = nullptr;     // S0: star statistic of the source frame
static ID3D11ComputeShader* g_starWeightCS = nullptr;   // S1: tapered protection field + zone weights
static ID3D11ComputeShader* g_starPlanCS = nullptr;     // S2: target + the plan the pixels sample
static ID3D11ComputeShader* g_starStatCombineCS = nullptr;   // S0 combine variant (zones > one slice, C14)
static ID3D11ComputeShader* g_temporalCS = nullptr;     // LED lag pass 1b: first-order drive state (modes 1 / 2)
static ID3D11ComputeShader* g_clockCS = nullptr;        // LED lag pass 1c: the two parity clocks (mode 3)
static bool StarShadersReady() { return g_starStatCS && g_starWeightCS && g_starPlanCS; }
// (the settle / content events are per monitor: FaldMonitor::settleEvt / contentEvt)
static void CompileFeatureShaders();   // below FaldReleaseShaders
static void FaldUnbindAll();          // with FaldRun
#include "../shared/fald_core2.h"
static void CoverReset(FaldMonitor* m);   // with FaldUpdateClean

// GPU timing (timestamp queries, read back without flushing a few frames later — never a stall in
// the present path). Three stamps per run: start, after the starfield passes, end of the pixel pass.
static FaldCoreTiming g_coreTiming;   // the layer's GPU time per full run (shared/fald_core_governor.h): the rate-cap
                                      // governor's input and the cost log; a slot's result is credited to its owner monitor

static const unsigned int HOOK_FALD_COVER_TILE = 32u;       // priming coverage granularity (px)
static const unsigned int HOOK_FALD_RELEASE_AFTER = 600u;   // presents off before an entry frees its GPU memory
static HANDLE g_primeEvent = NULL;                          // DWM_HOOK_FALD_PRIME_EVENT (the host creates it)
// The host's named events (prime, settle, content). The hook's sources compiled into the test exe (DLUT_HOOK_TESTING) open
// none: a test must never signal a DesktopLUT host running on the same machine.
#ifdef DLUT_HOOK_TESTING
static HANDLE OpenHostEvent(const wchar_t*) { return NULL; }
#else
static HANDLE OpenHostEvent(const wchar_t* name) { return OpenEventW(EVENT_MODIFY_STATE, FALSE, name); }
#endif
static long long g_primeLastQpc = 0;                        // the last prime request (throttle)

// t0..t35 (HOOK_FALD_SRV_SLOTS): the layer's t0..t22 as the HLSL declares them (t20..t22 = the fade stack's views) and
// the fade passes' t24..t35. The pixel pass and every compute pass bind the whole range so a slot left over from a
// previous pass cannot be read by accident.
static const UINT FALD_SRV_SLOTS = HOOK_FALD_SRV_SLOTS;
// The deepest UAV range any pass binds: u0..u3 (the panel clock; the zone sweeps bind u0..u2).
static const UINT FALD_UAV_SLOTS = HOOK_FALD_UAV_SLOTS;
static const UINT FALD_CORE_SRV_SLOTS = 13;   // t40..t52: the always-on core's (shared/fald_core2.h; nullSrv has FALD_SRV_SLOTS >= 13 entries)

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
    return g_statCS && g_convCS[0] && g_convCS[1] && g_convCS[2] && g_convCS[3] && g_gainCS && g_blurCS && g_gainBlurCS && g_decideCS &&
           g_faldPS && g_faldVS && g_faldSampler;
}

bool FaldInitShaders(ID3D11Device* dev, ID3D11DeviceContext* ctx) {
    if (!dev || !ctx) return false;
    g_dev = dev;
    g_ctx = ctx;
    if (FaldShadersReady()) return true;

    const std::string common = g_faldCommonSource;
    ID3DBlob* b = nullptr;
    HRESULT hr = S_OK;

    const std::string combine = std::string(g_faldZoneCombineDefine) + common;   // the zone sweeps' combine variants
    struct { const char* src; const char* name; ID3D11ComputeShader** out; const char* prefix; } cs[] = {
        { g_faldStatSource,  "FaldStatCS",  &g_statCS,  nullptr },
        { g_faldStatSource,  "FaldStatCombineCS", &g_statCombineCS, g_faldZoneCombineDefine },
        { g_faldConvSource,  "FaldConvCS1", &g_convCS[0], g_faldConvSoDefine[0] },   // pass 2 per variant (fald_shader.h FALD_CONV_SO)
        { g_faldConvSource,  "FaldConvCS2", &g_convCS[1], g_faldConvSoDefine[1] },
        { g_faldConvSource,  "FaldConvCS4", &g_convCS[2], g_faldConvSoDefine[2] },
        { g_faldConvSource,  "FaldConvCS8", &g_convCS[3], g_faldConvSoDefine[3] },
        { g_faldGainSource,  "FaldGainCS",  &g_gainCS,  nullptr },
        { g_faldBlurSource,  "FaldBlurCS",  &g_blurCS,  nullptr },
        { g_faldGainBlurSource, "FaldGainBlurCS", &g_gainBlurCS, nullptr },
        { g_faldDecideSource, "FaldDecideCS", &g_decideCS, nullptr },
    };
    for (const auto& s : cs) {
        const std::string pre = s.prefix ? (s.prefix == g_faldZoneCombineDefine ? combine : std::string(s.prefix) + common) : common;
        if (!CompileOne(pre + s.src, s.name, "cs_5_0", &b)) { FaldReleaseShaders(); return false; }
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
    D3D11_RASTERIZER_DESC rd = {};
    rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE; rd.ScissorEnable = TRUE;
    if (FAILED(g_dev->CreateRasterizerState(&rd, &g_exemptRS))) {
        g_exemptRS = nullptr;   // checked before every use: the views then cover DesktopLUT's windows too
        log_to_file("FALD: rasterizer state for the views' window exemption failed (views cover the whole screen)");
    }
    log_to_file("FALD: correction shaders compiled (stateless core)");
    CompileFeatureShaders();
    return true;
}

template <typename T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

static void ReleaseFeatureShaders() {
    SafeRelease(g_clockCS); SafeRelease(g_temporalCS);
    if (g_primeEvent) { CloseHandle(g_primeEvent); g_primeEvent = NULL; }
    SafeRelease(g_starStatCombineCS);
    SafeRelease(g_starPlanCS); SafeRelease(g_starWeightCS); SafeRelease(g_starStatCS);
    FaldCoreTimingRelease(&g_coreTiming);
}

static void CompileFeatureShaders() {
    const std::string common = g_faldCommonSource;
    const std::string combine = std::string(g_faldZoneCombineDefine) + common;
    struct { const char* src; const char* name; ID3D11ComputeShader** out; bool comb; } cs[] = {
        { g_faldStarStatSource,   "FaldStarStatCS",   &g_starStatCS,   false },
        { g_faldStarStatSource,   "FaldStarStatCombineCS", &g_starStatCombineCS, true },
        { g_faldStarWeightSource, "FaldStarWeightCS", &g_starWeightCS, false },
        { g_faldStarPlanSource,   "FaldStarPlanCS",   &g_starPlanCS,   false },
        { g_faldTemporalSource,   "FaldTemporalCS",   &g_temporalCS,   false },
        { g_faldPanelClockSource, "FaldPanelClockCS", &g_clockCS,      false },
    };
    for (const auto& s : cs) {
        ID3DBlob* b = nullptr;
        if (!CompileOne((s.comb ? combine : common) + s.src, s.name, "cs_5_0", &b)) continue;   // logged; that part stays off
        if (FAILED(g_dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, s.out)))
            LogF("FALD: CreateComputeShader(%s) failed", s.name);
        b->Release();
    }
    // GPU timestamp queries (optional: without them the cost log shows CPU time only and the layer is never rate-capped)
    FaldCoreTimingCreate(&g_coreTiming, g_dev);
    LogF("FALD: starfield shaders %s, LED lag shaders %s",
         StarShadersReady() ? "compiled" : "UNAVAILABLE", (g_temporalCS && g_clockCS) ? "compiled" : "UNAVAILABLE");
    std::string core2Err;                                          // the always-on core round 2 (prototype): optional
    if (!FaldCore2InitShaders(g_dev, g_faldCommonSource, &core2Err)) LogF("FALD: core round 2 shaders UNAVAILABLE: %s", core2Err.c_str());
}

static bool g_fadeShadersFailed = false;   // the fade stack's passes could not be created (latched until released)

// The flank fade's safety governor (shared/fald_fade_governor.h): DWM's adapter, once per attach (D3D11 / DXGI calls on
// the device only — no pointer read into DWM), and the status section the host reads (dwm_hook_config.h DwmHookFaldStatus:
// host-created; opened from the present path rarely, never per present; written only when an entry changes, under the
// block's compare-exchange seqlock — a busy block skips the write to a later present, never a wait).
static int g_fadeWeakAdapter = -1;                  // -1 unknown, 0 / 1
static HANDLE g_faldStatusMap = NULL;
static DwmHookFaldStatus* g_faldStatusView = nullptr;
static unsigned int g_faldStatusOpenTick = 0;

static bool FadeWeakAdapter() {
    if (g_fadeWeakAdapter < 0 && g_dev) {
        std::string desc;
        g_fadeWeakAdapter = FaldFadeWeakAdapter(g_dev, &desc) ? 1 : 0;
        LogF("FALD: flank fade governor: %s", desc.c_str());
    }
    return g_fadeWeakAdapter == 1;
}

#ifdef DLUT_HOOK_TESTING
static HANDLE OpenFaldStatusMap() { return NULL; }   // a test must never write a DesktopLUT host's section
#else
static HANDLE OpenFaldStatusMap() { return OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, DWM_HOOK_FALD_STATUS_NAME); }
#endif

static void CloseFaldStatus() {
    if (g_faldStatusView) { UnmapViewOfFile(g_faldStatusView); g_faldStatusView = nullptr; }
    if (g_faldStatusMap) { CloseHandle(g_faldStatusMap); g_faldStatusMap = NULL; }
    g_faldStatusOpenTick = 0;
}

// One entry of the status block (true = written; false = no section yet, or another writer holds the block: the caller
// tries again on a later present).
static bool WriteFadeStatus(int left, int top, bool isHdr, const FaldFadeGovernorStatus& st) {
    if (!g_faldStatusView) {
        if ((g_faldStatusOpenTick++ % 240u) != 0u) return false;   // the host creates it; asked rarely
        g_faldStatusMap = OpenFaldStatusMap();
        if (!g_faldStatusMap) return false;
        g_faldStatusView = (DwmHookFaldStatus*)MapViewOfFile(g_faldStatusMap, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0,
                                                            sizeof(DwmHookFaldStatus));
        if (!g_faldStatusView) { CloseHandle(g_faldStatusMap); g_faldStatusMap = NULL; return false; }
    }
    DwmHookFaldStatus* b = g_faldStatusView;
    const LONG v = (LONG)b->version;
    if (v & 1) return false;   // a write in progress (another present thread, another session's DWM)
    if (InterlockedCompareExchange((volatile LONG*)&b->version, v + 1, v) != v) return false;
    int slot = -1;
    for (int i = 0; i < DWM_HOOK_FALD_STATUS_SLOTS && slot < 0; i++) {
        const DwmHookFaldFadeStatusEntry& e = b->entry[i];
        if (e.used && e.left == left && e.top == top && e.isHdr == (isHdr ? 1u : 0u)) slot = i;
    }
    for (int i = 0; i < DWM_HOOK_FALD_STATUS_SLOTS && slot < 0; i++)
        if (!b->entry[i].used) slot = i;
    if (slot >= 0) {
        DwmHookFaldFadeStatusEntry& e = b->entry[slot];
        e.left = left; e.top = top; e.isHdr = isHdr ? 1u : 0u;
        e.state = (uint32_t)st.state;
        e.p90Us = (float)st.p90Us; e.budgetUs = (float)st.budgetUs;
        e.contentMs = (float)st.contentMs; e.refreshMs = (float)st.refreshMs;
        e.generation = st.generation;
        e.used = 1u;
        b->magic = DWM_HOOK_FALD_STATUS_MAGIC;
        b->dwmPid = GetCurrentProcessId();
    }
    InterlockedExchange((volatile LONG*)&b->version, v + 2);
    return true;
}

// The core governor's entry of the status block (dwm_hook_config.h DwmHookFaldStatus::core), the fade's rules.
static bool WriteCoreStatus(int left, int top, bool isHdr, const FaldCoreGovernorStatus& st) {
    if (!g_faldStatusView) {
        if ((g_faldStatusOpenTick++ % 240u) != 0u) return false;   // the host creates it; asked rarely
        g_faldStatusMap = OpenFaldStatusMap();
        if (!g_faldStatusMap) return false;
        g_faldStatusView = (DwmHookFaldStatus*)MapViewOfFile(g_faldStatusMap, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0,
                                                            sizeof(DwmHookFaldStatus));
        if (!g_faldStatusView) { CloseHandle(g_faldStatusMap); g_faldStatusMap = NULL; return false; }
    }
    DwmHookFaldStatus* b = g_faldStatusView;
    const LONG v = (LONG)b->version;
    if (v & 1) return false;
    if (InterlockedCompareExchange((volatile LONG*)&b->version, v + 1, v) != v) return false;
    int slot = -1;
    for (int i = 0; i < DWM_HOOK_FALD_STATUS_SLOTS && slot < 0; i++) {
        const DwmHookFaldCoreStatusEntry& e = b->core[i];
        if (e.used && e.left == left && e.top == top && e.isHdr == (isHdr ? 1u : 0u)) slot = i;
    }
    for (int i = 0; i < DWM_HOOK_FALD_STATUS_SLOTS && slot < 0; i++)
        if (!b->core[i].used) slot = i;
    if (slot >= 0) {
        DwmHookFaldCoreStatusEntry& e = b->core[slot];
        e.left = left; e.top = top; e.isHdr = isHdr ? 1u : 0u;
        e.state = (uint32_t)st.state;
        e.p90Us = (float)st.p90Us; e.budgetUs = (float)st.budgetUs;
        e.refreshMs = (float)st.refreshMs; e.spacingMs = (float)st.spacingMs; e.contentMs = (float)st.contentMs;
        e.generation = st.generation;
        e.used = 1u;
        b->magic = DWM_HOOK_FALD_STATUS_MAGIC;
        b->dwmPid = GetCurrentProcessId();
    }
    InterlockedExchange((volatile LONG*)&b->version, v + 2);
    return true;
}

void FaldReleaseShaders() {
    FaldFadeReleaseShaders();
    FaldCore2ReleaseShaders();
    g_fadeShadersFailed = false;
    g_fadeWeakAdapter = -1;   // the next device is asked again
    ReleaseFeatureShaders();
    SafeRelease(g_exemptRS);
    SafeRelease(g_faldSampler);
    SafeRelease(g_faldVS);
    SafeRelease(g_faldPS);
    SafeRelease(g_statCombineCS);
    SafeRelease(g_decideCS);
    SafeRelease(g_gainBlurCS);
    SafeRelease(g_blurCS);
    SafeRelease(g_gainCS);
    for (unsigned int v = 0; v < FALD_CONV_SO_VARIANTS; v++) SafeRelease(g_convCS[v]);
    SafeRelease(g_statCS);
}

// ---------------------------------------------------------------------------------------------
// Panel files: staged at attach, replaced by live deliveries (dwm_hook_config.h DwmHookFaldPanelBlob)
// ---------------------------------------------------------------------------------------------
// One slot per (position, mode): shared/fald_panel_live.h, the same table the unit tests drive. Fixed-size, like
// lutTargets in hook_lut.h and for the same reason: DWM may call the Present hooks on more than one thread, and a
// container that reallocates would hand a concurrent reader a freed pointer. Slot files are heap-owning and a delivery
// or a build changes them, so every use of the table — and of g_monitors below, which FaldAcquire appends to — is
// under g_panelsLock: TRIED on the present path (busy = skip this present's table work, never a wait inside DWM's
// present), waited for only at attach and in FaldReleaseAll.
static const int FALD_MAX_PANEL_FILES = FaldPanelTable::kCapacity;   // one SDR + one HDR per monitor
static FaldPanelTable g_panels;
static FaldPanelLock g_panelsLock;
// g_panels.AnyPresent() as of the last change under the lock, for readers that must not take it (the direct-flip hooks)
static std::atomic<bool> g_anyPanelPresent{ false };
static void RefreshAnyPanelPresent() { g_anyPanelPresent.store(g_panels.AnyPresent()); }   // g_panelsLock held
static void NoteSlotChanged(int left, int top, bool isHdr);   // with FaldAcquire: that entry owes a swap

bool FaldHasAnyPanelFile() { return g_anyPanelPresent.load(); }

int FaldLoadPanelFiles(const char* lutFolder) {
    FaldPanelLockGuard lock(g_panelsLock, true);   // attach: no present runs yet
    g_panels.Clear();
    g_anyPanelPresent.store(false);
    if (!lutFolder) return 0;

    char dirA[MAX_PATH];
    snprintf(dirA, sizeof(dirA), "%s\\%s", lutFolder, DWM_HOOK_FALD_SUBDIR_A);
    char patternA[MAX_PATH];
    snprintf(patternA, sizeof(patternA), "%s\\*.bin", dirA);

    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(patternA, &fd);
    if (h == INVALID_HANDLE_VALUE) return 0;
    int loaded = 0;
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        if (g_panels.Count() >= FALD_MAX_PANEL_FILES) { log_to_file("FALD: too many panel files staged — ignoring the rest"); break; }
        int left = 0, top = 0;
        if (sscanf(fd.cFileName, "%d_%d", &left, &top) != 2) continue;
        const bool isHdr = strstr(fd.cFileName, "_hdr") != nullptr;

        char fullA[MAX_PATH];
        snprintf(fullA, sizeof(fullA), "%s\\%s", dirA, fd.cFileName);
        wchar_t fullW[MAX_PATH] = {};
        if (MultiByteToWideChar(CP_ACP, 0, fullA, -1, fullW, MAX_PATH) == 0) continue;

        FaldPanelParams params;
        std::string err;
        if (!LoadFaldPanelParams(fullW, params, err)) {
            LogF("FALD: skipping unparseable panel file %s: %s", fd.cFileName, err.c_str());
            continue;
        }
        // The fit's code domain is the panel's: a PQ (HDR) file cannot serve an ACM SDR desktop and
        // vice versa. Caught here so a mismatched file costs one log line at attach, not a wrong
        // correction on screen.
        if (!FaldTransferMatchesMode(params.transfer, isHdr)) {
            LogF("FALD: panel file %s is a %s fit but is staged for %s — ignored", fd.cFileName,
                 params.transfer == FALD_TRANSFER_GAMMA ? "gamma (SDR)" : "PQ (HDR)",
                 isHdr ? "HDR" : "SDR (ACM)");
            continue;
        }
        LogF("FALD: panel file %s loaded: pos(%d,%d) %s, %ux%u cells on a %ux%u-px lattice, sub %u, white %.1f nits, boost %s",
             fd.cFileName, left, top, isHdr ? "HDR" : "SDR(ACM)",
             params.cols, params.rows, params.latW, params.latH, params.sub,
             params.white, params.hasBoost ? "yes" : "none");
        if (g_panels.AddStaged(left, top, isHdr, std::move(params))) loaded++;
    } while (FindNextFileA(h, &fd) != 0);
    FindClose(h);
    RefreshAnyPanelPresent();

    LogF("FALD: %d panel file(s) loaded", loaded);
    return loaded;
}

// Live delivery (dwm_hook_config.h, "FALD panel file live delivery"). Per slot, the section of the last delivery taken
// into it stays mapped until the next one replaces it: the DLL writes its later verdicts (APPLIED / BUILD_FAILED) there.
struct FaldDeliveryView {
    HANDLE map = NULL;
    DwmHookFaldPanelBlob* view = nullptr;
    uint32_t gen = 0;
};
static FaldDeliveryView g_deliveryViews[FALD_MAX_PANEL_FILES];   // parallel to g_panels' slots

static void ReleaseDeliveryView(FaldDeliveryView& d) {
    if (d.view) { UnmapViewOfFile(d.view); d.view = nullptr; }
    if (d.map) { CloseHandle(d.map); d.map = NULL; }
    d.gen = 0;
}

// The section is another process's memory: every read and write of it is SEH-guarded (no C++ objects in these
// functions, as MSVC's __try requires). The header is copied out and checked before anything else is read. The filter
// takes only what reading mapped memory can raise — an access violation or a paging error — and lets everything else
// (a C++ exception, a stack overflow) go on to its own handler.
static int SectionFaultFilter(unsigned long code) {
    return (code == EXCEPTION_ACCESS_VIOLATION || code == EXCEPTION_IN_PAGE_ERROR) ? EXCEPTION_EXECUTE_HANDLER
                                                                                   : EXCEPTION_CONTINUE_SEARCH;
}

static bool SehCopy(void* dst, const void* src, size_t n) {
    __try { memcpy(dst, src, n); return true; }
    __except (SectionFaultFilter(GetExceptionCode())) { return false; }
}

// Deliver straight from the view — no copy of the file in DWM's present. Safe on memory another process could still
// write: the parser reads the header words once into its own array and bounds every table copy by `bytes`, which the
// header check proved lies inside the view (values could change under it, never what is read). A section fault
// (SehDeliver) or a C++ exception — bad_alloc (DeliverCaught, its own function: MSVC allows one form of exception
// handling per function) — refuses the delivery; neither ever leaves the present.
static const uint32_t FALD_DELIVERY_FAULT = 0xFFFFFFFFu;
static uint32_t DeliverCaught(int left, int top, bool isHdr, uint32_t gen, const void* payload, size_t bytes,
                              std::string* err) {
    try {
        return g_panels.Deliver(left, top, isHdr, gen, payload, bytes, *err);
    } catch (...) {
        return FALD_DELIVERY_FAULT;
    }
}
static uint32_t SehDeliver(int left, int top, bool isHdr, uint32_t gen, const void* payload, size_t bytes, std::string* err) {
    __try { return DeliverCaught(left, top, isHdr, gen, payload, bytes, err); }
    __except (SectionFaultFilter(GetExceptionCode())) { return FALD_DELIVERY_FAULT; }
}

static void SehWriteAck(DwmHookFaldPanelBlob* v, uint32_t gen, uint32_t status) {
    if (!v) return;
    __try { v->ackStatus = status; v->ackGen = gen; }
    __except (SectionFaultFilter(GetExceptionCode())) {}
}

// The verdict on the file in slot `slot` (the delivered one only: a staged file has no section to answer in).
static void AckSlot(int slot, uint32_t status) {   // g_panelsLock held
    if (slot < 0 || slot >= g_panels.Count()) return;
    const FaldPanelSlot& s = g_panels.At(slot);
    const FaldDeliveryView& d = g_deliveryViews[slot];
    if (!FaldPanelAckApplies(s.gen, d.gen)) return;
    SehWriteAck(d.view, d.gen, status);
}

static void ReleaseDeliveryViews() {
    for (int i = 0; i < FALD_MAX_PANEL_FILES; i++) ReleaseDeliveryView(g_deliveryViews[i]);
}

// Take delivery `gen` for (left, top, isHdr) into the panel table. Runs in a present of that monitor, once per generation
// (FaldSyncPanelDeliveries). Cost: one section map + the staged file's parser reading the tables straight out of the
// view into memory reused from the previous delivery (< 1 MB) — once per delivery, never per frame. No wait, no file
// I/O; the log line (with the measured time) is the only write. g_panelsLock held.
static void IngestDelivery(int left, int top, bool isHdr, uint32_t gen, uint32_t hostPid) {
    LARGE_INTEGER t0, t1, f;
    QueryPerformanceCounter(&t0);
    const char* modeName = isHdr ? "HDR" : "SDR(ACM)";
    wchar_t name[96];
    swprintf_s(name, DWM_HOOK_FALD_PANEL_NAME_FMT, (unsigned long)hostPid, (unsigned long)gen);
    HANDLE map = OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, name);
    DwmHookFaldPanelBlob* view = map ? (DwmHookFaldPanelBlob*)MapViewOfFile(map, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, 0)
                                     : nullptr;
    if (!view) {
        const DWORD error = GetLastError();   // before CloseHandle overwrites it
        if (map) CloseHandle(map);
        // Superseded before it was opened (the config then carries a newer generation), or the host is gone.
        g_panels.MarkSeen(left, top, isHdr, gen, DWM_HOOK_FALD_PANEL_ACK_NONE);
        LogF("FALD: pos(%d,%d) %s panel delivery %u cannot be opened (error %lu) - the layer keeps its current file",
             left, top, modeName, gen, error);
        return;
    }
    MEMORY_BASIC_INFORMATION mbi = {};
    const size_t viewBytes = VirtualQuery(view, &mbi, sizeof(mbi)) ? (size_t)mbi.RegionSize : 0;

    // Nothing below allocates outside SehDeliver (which turns any exception into a refusal): an out-of-memory DWM
    // costs this delivery, never a throw through the present and never a retry per present.
    uint32_t ack = DWM_HOOK_FALD_PANEL_ACK_REFUSED;
    std::string parseWhy;                  // Deliver's reason (only ever written inside SehDeliver)
    const char* why = "header";
    DwmHookFaldPanelBlob h = {};
    if (viewBytes < sizeof(h) || !SehCopy(&h, view, sizeof(h))) {
        why = "header unreadable";
    } else if (!FaldPanelBlobCheck(h, viewBytes, gen, left, top, isHdr, &why)) {
        if (!why) why = "header";
    } else {
        ack = SehDeliver(left, top, isHdr, gen, (const unsigned char*)view + h.headerBytes, h.payloadBytes, &parseWhy);
        if (ack == FALD_DELIVERY_FAULT) {   // a fault, or bad_alloc: refused, never taken into DWM
            why = "section unreadable or out of memory";
            ack = DWM_HOOK_FALD_PANEL_ACK_REFUSED;
        } else {
            why = parseWhy.c_str();
        }
    }
    if (ack != DWM_HOOK_FALD_PANEL_ACK_INGESTED) g_panels.MarkSeen(left, top, isHdr, gen, ack);   // (Deliver marked it too)
    else NoteSlotChanged(left, top, isHdr);   // its entry owes a swap (asked for if it cannot be done this present)
    RefreshAnyPanelPresent();

    // An accepted delivery's section is kept for its later verdicts (applied / build failed) and replaces the slot's
    // previous one. A refused one is answered here and dropped: the slot still holds the file of the section it
    // already keeps, which must stay answerable (AckSlot).
    const int slot = g_panels.IndexOf(left, top, isHdr);
    SehWriteAck(view, gen, ack);
    if (slot >= 0 && FaldPanelKeepsView(ack)) {
        ReleaseDeliveryView(g_deliveryViews[slot]);
        g_deliveryViews[slot].map = map;
        g_deliveryViews[slot].view = view;
        g_deliveryViews[slot].gen = gen;
    } else {
        UnmapViewOfFile(view);
        CloseHandle(map);
    }

    QueryPerformanceCounter(&t1); QueryPerformanceFrequency(&f);
    const double us = f.QuadPart > 0 ? (double)(t1.QuadPart - t0.QuadPart) * 1e6 / (double)f.QuadPart : 0.0;
    if (ack == DWM_HOOK_FALD_PANEL_ACK_INGESTED) {
        const FaldPanelParams& p = g_panels.At(slot).params;
        LogF("FALD: pos(%d,%d) %s panel file delivered live (gen %u, %u bytes): %ux%u cells on a %ux%u-px lattice, sub %u, "
             "white %.1f nits, boost %s - taken in %.0f us; the layer switches at its next run",
             left, top, modeName, gen, h.payloadBytes, p.cols, p.rows, p.latW, p.latH, p.sub, p.white,
             p.hasBoost ? "yes" : "none", us);
    } else {
        LogF("FALD: pos(%d,%d) %s panel delivery %u REFUSED (%s) - the layer keeps its current file",
             left, top, modeName, gen, why ? why : "?");
    }
}

void FaldSyncPanelDeliveries(int left, int top, unsigned int hostPid, const unsigned int panelGen[2]) {
    if (!panelGen || (panelGen[0] == 0 && panelGen[1] == 0)) return;   // nothing delivered: no lock, no table walk
    FaldPanelLockGuard lock(g_panelsLock);
    if (!lock.owned()) return;   // another present thread is in the table: taken on a later present, never waited for
    for (int h = 0; h < 2; h++) {
        const bool isHdr = (h == 1);
        if (!g_panels.Wants(left, top, isHdr, panelGen[h])) continue;
        IngestDelivery(left, top, isHdr, panelGen[h], hostPid);
        return;   // at most one per present
    }
}

// ---------------------------------------------------------------------------------------------
// Per-monitor resources
// ---------------------------------------------------------------------------------------------
static const size_t FALD_MONITOR_AUDITED_BYTES = 6688;   // sizeof(FaldMonitor) the live-swap audit below covers (x64)
                                                         // (2026-10-19: + FaldPanelParams::latW / latH in params and
                                                         // spareParams — panel-file state, BUILT FRESH / KEPT as before;
                                                         // 2026-10-20: + the fade stack's five fields; 2026-10-22: + the
                                                         // fade governor's five and cleanGen; 2026-10-23: + the always-on
                                                         // core's four: core2, coreModeWanted, core2Flags, core2On)
struct FaldMonitor : FaldTemporalState {   // LED-lag bookkeeping: shared/fald_temporal.h (the overlay's FaldResources too)
    int left = 0, top = 0;
    bool isHdr = false;
    unsigned int width = 0, height = 0;
    DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    FaldPanelParams params;
    bool valid = false;
    bool failed = false;          // latched: logged once, never retried for this (pos, mode, size) and this file
    uint32_t panelSeq = 0;        // the panel slot's seq `params` were built from (FaldPanelSlot::seq)
    uint32_t failedSeq = 0;       // the slot seq a latched failure was for: a newly delivered file retries
    uint32_t refusedSeq = 0;      // a live swap to this slot seq failed: the entry runs on, on its previous file

    uint32_t debugMode = 0;
    uint32_t pedMode = 0;
    unsigned long long framesRun = 0;
    double lastRunUs = 0.0;
    // The monitor's desktop size (the host's monitors[] entry; 0 = unknown = the frame's): DesktopLUT's own windows,
    // exempt from a debug view (tuning.exempt, shared/fald_exempt.h), are in monitor pixels and mapped onto the frame.
    unsigned int exemptMonW = 0, exemptMonH = 0;

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

    // panel tables (the kernels re-laid out for the conv pass's variant, shared/fald_panel.h FaldConvTableLayout: a float
    // view at t2 / t3 and a float4 view of the same buffer at t36 / t37)
    ID3D11Texture2D* curveTex = nullptr;  ID3D11ShaderResourceView* curveSRV = nullptr;
    ID3D11Buffer* kTrueBuf = nullptr;     ID3D11ShaderResourceView* kTrueSRV = nullptr; ID3D11ShaderResourceView* kTrue4SRV = nullptr;
    ID3D11Buffer* kEstBuf = nullptr;      ID3D11ShaderResourceView* kEstSRV = nullptr;  ID3D11ShaderResourceView* kEst4SRV = nullptr;
    unsigned int convVariant = 0;         // FaldConvVariant at build: 0..3 <-> 1 / 2 / 4 / 8 sub-offsets per thread
    bool gainFused = false;               // FaldGainBlurFused at build: passes 2b + 2c in one dispatch into gainB (gainA unused)
    // per-frame fields
    ID3D11Texture2D* driveTex = nullptr;  ID3D11UnorderedAccessView* driveUAV = nullptr; ID3D11ShaderResourceView* driveSRV = nullptr;
    ID3D11Texture2D* bTrueTex = nullptr;  ID3D11UnorderedAccessView* bTrueUAV = nullptr; ID3D11ShaderResourceView* bTrueSRV = nullptr;
    ID3D11Texture2D* bEstTex = nullptr;   ID3D11UnorderedAccessView* bEstUAV = nullptr;  ID3D11ShaderResourceView* bEstSRV = nullptr;
    // the same two PACKED with their flat response, (B_true, flat, B_est, flat) RGBA32F (fald_shader.h t39): what SampleFields
    // and the gain passes read, one sample / load for the four; the conv pass writes it next to the two above
    ID3D11Texture2D* fieldsTex = nullptr; ID3D11UnorderedAccessView* fieldsUAV = nullptr; ID3D11ShaderResourceView* fieldsSRV = nullptr;
    ID3D11Texture2D* gainATex = nullptr;  ID3D11UnorderedAccessView* gainAUAV = nullptr; ID3D11ShaderResourceView* gainASRV = nullptr;
    ID3D11Texture2D* gainBTex = nullptr;  ID3D11UnorderedAccessView* gainBUAV = nullptr; ID3D11ShaderResourceView* gainBSRV = nullptr;
    // flat-lattice response of both kernels (computed once at build): a flat field must give gain 1
    ID3D11Texture2D* flatTrueTex = nullptr; ID3D11UnorderedAccessView* flatTrueUAV = nullptr; ID3D11ShaderResourceView* flatTrueSRV = nullptr;
    ID3D11Texture2D* flatEstTex = nullptr;  ID3D11UnorderedAccessView* flatEstUAV = nullptr;  ID3D11ShaderResourceView* flatEstSRV = nullptr;
    // black-frame LED boost, one set per statistic round
    ID3D11Texture2D* activeTex[2] = {}; ID3D11UnorderedAccessView* activeUAV[2] = {}; ID3D11ShaderResourceView* activeSRV[2] = {};
    ID3D11Texture2D* boostTex[2] = {};  ID3D11UnorderedAccessView* boostUAV[2] = {};  ID3D11ShaderResourceView* boostSRV[2] = {};
    ID3D11Buffer* boostLutBuf = nullptr; ID3D11ShaderResourceView* boostLutSRV = nullptr;
    // the statistic pass's non-black zone counters, one per round (zeroed at creation; + (new - old) per finished zone)
    ID3D11Buffer* boostCountBuf = nullptr; ID3D11UnorderedAccessView* boostCountUAV = nullptr;
    // The change-driven path (shared/fald_change.h; src/fald.h has the same): round 0's own drive / filtered drive /
    // fields, the touched-zone list, the change flags, the decide pass's indirect arguments, what the previous run left.
    ID3D11Texture2D* drive0Tex = nullptr; ID3D11UnorderedAccessView* drive0UAV = nullptr; ID3D11ShaderResourceView* drive0SRV = nullptr;
    ID3D11Texture2D* driveFilt0Tex = nullptr; ID3D11UnorderedAccessView* driveFilt0UAV = nullptr; ID3D11ShaderResourceView* driveFilt0SRV = nullptr;
    ID3D11Texture2D* bTrue0Tex = nullptr; ID3D11UnorderedAccessView* bTrue0UAV = nullptr; ID3D11ShaderResourceView* bTrue0SRV = nullptr;
    ID3D11Texture2D* bEst0Tex = nullptr;  ID3D11UnorderedAccessView* bEst0UAV = nullptr;  ID3D11ShaderResourceView* bEst0SRV = nullptr;
    ID3D11Texture2D* fields0Tex = nullptr; ID3D11UnorderedAccessView* fields0UAV = nullptr; ID3D11ShaderResourceView* fields0SRV = nullptr;
    ID3D11Texture2D* gain0Tex = nullptr;  ID3D11UnorderedAccessView* gain0UAV = nullptr;  ID3D11ShaderResourceView* gain0SRV = nullptr;
    ID3D11Buffer* zoneListBuf = nullptr; ID3D11ShaderResourceView* zoneListSRV = nullptr;
    ID3D11Buffer* changeFlagsBuf = nullptr; ID3D11UnorderedAccessView* changeFlagsUAV = nullptr;
    ID3D11Buffer* argsBuf = nullptr; ID3D11UnorderedAccessView* argsUAV = nullptr;
    uint32_t runMode = 0;                  // CB word 79 of the next FillCB
    bool stateComplete = false;            // the previous run left every texture of a complete run (and the intermediate whole)
    bool planPartial = false;              // this present's plan (FaldPlanPartial), consumed by FaldRun
    bool lastPartial = false;              // the previous run took the partial path (the dump)
    unsigned long long lastSettingsKey = 0, lastLutPassKey = 0, planSettingsKey = 0;
    uint32_t lastTrueMap = 0xFFFFFFFFu, lastEstMap = 0xFFFFFFFFu;   // the maps (FaldDriveMap) the previous run's kernels
                                           // read: a run whose routing differs (a panel-clock seed, the clock run after it) is full
    std::vector<uint32_t> touched;         // this present's touched zones (FaldUpdateClean)
    bool touchedOk = false;                // ... within the cap
    std::vector<RECT> rects;               // this present's rects, clipped to the frame (the pixel pass over them)
    unsigned long long partialRuns = 0;    // runs that took the partial path (the cost log)
    // zone sweeps (work guide C14; fald_shader.h above ZoneSlices): slices per zone, and for a lattice of more than one
    // the slice partials the statistic / S0 passes write and their combine variants fold (u2); null for one slice
    unsigned int zoneSlices = 1;
    ID3D11Buffer* zonePartBuf = nullptr; ID3D11UnorderedAccessView* zonePartUAV = nullptr;

    ID3D11Buffer* cb = nullptr;

    // Starfield feature (created on the first frame it is on, released when it goes off).
    // Request from the live settings; starOn = requested AND the textures exist (CB word 35).
    bool starWanted = false;
    bool starOn = false;
    bool starFailed = false;                              // latched: logged once, no per-frame retry
    DwmHookFaldTuning tuning = {};                        // clamped; defaults until the host sends some
    ID3D11Texture2D* starStatTex = nullptr;  ID3D11UnorderedAccessView* starStatUAV = nullptr;  ID3D11ShaderResourceView* starStatSRV = nullptr;
    ID3D11Texture2D* starWTex = nullptr;     ID3D11UnorderedAccessView* starWUAV = nullptr;     ID3D11ShaderResourceView* starWSRV = nullptr;
    ID3D11Texture2D* starPlanTex = nullptr;  ID3D11UnorderedAccessView* starPlanUAV = nullptr;  ID3D11ShaderResourceView* starPlanSRV = nullptr;
    ID3D11Texture2D* starBgTex = nullptr;    ID3D11UnorderedAccessView* starBgUAV = nullptr;    ID3D11ShaderResourceView* starBgSRV = nullptr;
    ID3D11Texture2D* starPlan2Tex = nullptr; ID3D11UnorderedAccessView* starPlan2UAV = nullptr; ID3D11ShaderResourceView* starPlan2SRV = nullptr;

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
    double gpuSumUs = 0.0, gpuStarSumUs = 0.0, gpuMaxUs = 0.0, gpuCoreSumUs = 0.0;
    unsigned int gpuSamples = 0;

    // The frozen fade stack ASF10 + T11_TMTC (shared/fald_fade.h; the setting = faldFlags bit 8). `fade` = its resources and
    // T state (sized by the FRAME only: moved with the frame part by a live panel swap, which then resets its T state —
    // a panel-file switch is a cut); fadeWanted = the live setting; fadeOn = this run applies it (CB word 75, t20-t22);
    // fadeFailed = its creation failed for this build (latched, logged once); fadeKey = the settings its cached frame part
    // was formed under (the starfield): a change re-forms it without a T step.
    FaldFade* fade = nullptr;
    bool fadeWanted = false;
    bool fadeOn = false;
    // The always-on core round 2 (PROTOTYPE, shared/fald_core2.h; faldFlags bit 16): its zone textures + IIR state (panel
    // part: released with it, a swap rebuilds them); core2Wanted = the live setting; core2On = this run applies it.
    FaldCore2* core2 = nullptr;
    uint32_t coreModeWanted = 0;             // the test-only core switch (0 C, 1 KC2c, 2 KC3)
    uint32_t core2Flags = 0;
    bool core2On = false;
    bool fadeFailed = false;
    unsigned long long fadeKey = 0;
    // The fade's safety governor (shared/fald_fade_governor.h): fadeGov = its state (moved with the frame part: a panel
    // swap keeps a suspension); fadeForce = the live setting (faldFlags bit 10); fadeGovWrittenGen / fadeGovWriteRuns =
    // what the status section last got and the runs since. cleanGen counts the clean copy's changes (FaldUpdateClean):
    // with the LUT pass's key it identifies the intermediate (FaldRun's reach reuse).
    FaldFadeGovernor fadeGov;
    bool fadeForce = false;
    uint32_t fadeGovWrittenGen = 0xFFFFFFFFu;
    uint32_t fadeGovWriteRuns = 0;
    unsigned long long cleanGen = 0;
    // The core layer's rate-cap governor (shared/fald_core_governor.h): coreGov = its state (moved with the frame part as
    // the fade's); coreForce = the live setting (faldFlags bit 11); coreGovWrittenGen / coreGovWriteRuns / coreGovLoggedGen
    // = what the status section and the log last got; fieldsStand = a full or partial run's textures stand for this entry
    // (a held run corrects the new frame with them); heldRuns = the runs that kept them.
    FaldCoreGovernor coreGov;
    bool coreForce = false;
    uint32_t coreGovWrittenGen = 0xFFFFFFFFu;
    uint32_t coreGovWriteRuns = 0;
    uint32_t coreGovLoggedGen = 0xFFFFFFFFu;
    bool fieldsStand = false;
    unsigned long long heldRuns = 0;

    // Memory of the params a live swap of THIS entry replaced: its next swap copies the new file into it (SwapPanel),
    // so an A/B allocates nothing in DWM's present after the first switches. Per entry, never shared: two monitors may
    // swap on two present threads. Freed with the entry's panel part (ReleaseMonitor).
    FaldPanelParams spareParams;
};

// Live-swap audit (SwapPanel / MoveFramePart). Every field of FaldMonitor is exactly one of:
//   COPIED into the scratch entry before the build (identity + live settings): left, top, isHdr, width, height, format,
//     debugMode, pedMode, starWanted, tuning, tempSettings, refreshMs, fadeWanted, fadeForce, exemptMonW, exemptMonH,
//     coreModeWanted (the core prototype's test switch);
//   MOVED with the frame part (MoveFramePart): interTex / interRTV / interSRV, cleanTex / cleanSRV, primed, cover,
//     coverCols, coverRows, coverLeft, offPresents, settleEvt, contentEvt, evtOpenTick, framesRun, lastRunUs, gpuSumUs,
//     gpuStarSumUs, gpuMaxUs, gpuSamples, fade (its T state is then reset by SwapPanel: the switch acts as a cut),
//     fadeGov, fadeGovWrittenGen, fadeGovWriteRuns, cleanGen;
//   KEPT on the entry by SwapPanel itself: spareParams;
//   BUILT FRESH by the swap (BuildTables, defaults, or on demand later): params, valid, failed, panelSeq, failedSeq,
//     refusedSeq, every panel-table / per-frame field / flat / boost / zone-partials resource (kTrue4SRV / kEst4SRV and
//     convVariant, gainFused, the boost counters and the change-driven path's maps / list / flags / arguments and its
//     runMode, stateComplete, planPartial, lastPartial, lastSettingsKey, lastLutPassKey, planSettingsKey, lastTrueMap,
//     lastEstMap, touched, touchedOk, rects among the panel tables), zoneSlices, cb, starOn,
//     starFailed, the five starfield textures, clockFailed, the LED-lag and panel-clock textures, fadeOn, fadeFailed,
//     fadeKey, core2 / core2On / core2Flags (the core prototype: its zone textures + IIR state, released with the old panel
//     part by ReleaseMonitor, rebuilt by ResolveFeatures — the swap acts as a cut), and the whole FaldTemporalState base
//     (the LED-lag bookkeeping).
// A field added to FaldMonitor changes its size and stops this assert: put it in one of the lists above (and in
// MoveFramePart / SwapPanel unless it is panel-file state the swap must rebuild), then update the number.
static_assert(sizeof(FaldMonitor) == FALD_MONITOR_AUDITED_BYTES,
              "FaldMonitor changed: classify the new field in the live-swap audit above, then update the size");

// The defaults src/types.h gives FaldStarfieldSettings (used until the host sends a
// tuning tail — an older host — and for the inert CB words while the feature is off).
static DwmHookFaldTuning DefaultTuning() {
    DwmHookFaldTuning t = {};
    t.starEven = 0.8f; t.starLift = 0.0f; t.starTargetGain = 1.0f; t.starTargetSigma = 0.0f;
    t.starKeepNits = 100.0f; t.starCapNits = 0.0f; t.starStrength = 1.0f; t.starAreaLo = 40.0f;
    t.starAreaHi = 160.0f; t.starPeakHi = 0.0f; t.starNbLo = 0.15f; t.starNbHi = 0.30f;
    t.starReach = 2u; t.starEvenReach = 8u;
    return t;
}

// Shared memory is the host's word, but the integers here size shader loops: bound them whatever
// arrives (the host already clamped with src FaldStarfieldClamp), and replace NaNs.
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
    return t;
}

static void ReleaseClock(FaldMonitor* m) {
    m->stateComplete = false; m->fieldsStand = false;              // (the change-driven path: the next run is a full one)
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
    m->stateComplete = false; m->fieldsStand = false;              // (the change-driven path: the next run is a full one)
    SafeRelease(m->starPlan2SRV); SafeRelease(m->starPlan2UAV); SafeRelease(m->starPlan2Tex);
    SafeRelease(m->starBgSRV);    SafeRelease(m->starBgUAV);    SafeRelease(m->starBgTex);
    SafeRelease(m->starPlanSRV);  SafeRelease(m->starPlanUAV);  SafeRelease(m->starPlanTex);
    SafeRelease(m->starWSRV);     SafeRelease(m->starWUAV);     SafeRelease(m->starWTex);
    SafeRelease(m->starStatSRV);  SafeRelease(m->starStatUAV);  SafeRelease(m->starStatTex);
    m->starOn = false;
}

// One entry per (position, mode) actually seen presenting; fixed-size for the same reason as above. Appended to only
// under g_panelsLock, but read lock-free by a present that finds the lock busy: the pointer is published (release)
// before the count, and both are read with acquire (MonitorAt / MonitorCount).
static std::atomic<FaldMonitor*> g_monitors[FALD_MAX_PANEL_FILES] = {};
static std::atomic<int> g_numMonitors{ 0 };
static int MonitorCount() { return g_numMonitors.load(std::memory_order_acquire); }
static FaldMonitor* MonitorAt(int i) { return g_monitors[i].load(std::memory_order_acquire); }
// Entry use vs entry rebuild — the protocol (one gate per g_monitors index: shared/fald_panel_live.h FaldEntryGate,
// whose state machine the unit tests drive):
//   * A present that gets an entry from FaldAcquire has BORROWED it, for the rest of that present: RenderLUT returns it
//     with FaldReturn on every path (FaldBorrowScope). Several presents may borrow one entry (25H2 twin contexts).
//   * Everything that changes an entry's resources or state outside its own use — a live swap, a build or resize, a
//     stale mark (and the release after 600 of them), the other-mode release, FaldReleaseAll — runs under g_panelsLock
//     AND claims the entry's gate first. A claim that finds a borrow is refused: the work stays pending (the slot's
//     newer file, the stale mark) and a later present redoes it; a deferred swap asks for that present (bounded).
//   * Nothing waits: a busy lock, a borrowed entry or a claimed one each mean "not this present".
static std::atomic<bool> g_stalePending[FALD_MAX_PANEL_FILES];   // a stale mark still to apply (by index)
static FaldEntryGate g_entryGate[FALD_MAX_PANEL_FILES];
// A panel file delivered for this entry's (position, mode) is not built yet: a present that cannot do the swap now asks
// for a later present, at most kFaldSwapKicksMax times per delivered file (HookRequestFullRecompose: once a second).
static std::atomic<bool> g_entryNeedsSwap[FALD_MAX_PANEL_FILES];
static std::atomic<unsigned> g_entryKicks[FALD_MAX_PANEL_FILES];
static const unsigned kFaldSwapKicksMax = 5;
// FaldReleaseAll is running: an append refuses (its timeout path does not hold the lock).
static std::atomic<bool> g_faldReleasing{ false };
static void MarkEntryStale(FaldMonitor* e);   // with FaldMarkStale
static void ApplyPendingStale(int left, int top);   // with FaldMarkStale

static void RequestDeferredSwap(int i) {
    if (i < 0 || !g_entryNeedsSwap[i].load() || g_entryKicks[i].load() >= kFaldSwapKicksMax) return;
    if (HookRequestFullRecompose()) g_entryKicks[i].fetch_add(1);   // throttled inside; counted only when signalled
}

static void ReleaseMonitor(FaldMonitor* m) {
    SafeRelease(m->cb);
    SafeRelease(m->boostLutSRV); SafeRelease(m->boostLutBuf);
    SafeRelease(m->boostCountUAV); SafeRelease(m->boostCountBuf);
    SafeRelease(m->drive0SRV); SafeRelease(m->drive0UAV); SafeRelease(m->drive0Tex);
    SafeRelease(m->driveFilt0SRV); SafeRelease(m->driveFilt0UAV); SafeRelease(m->driveFilt0Tex);
    SafeRelease(m->bTrue0SRV); SafeRelease(m->bTrue0UAV); SafeRelease(m->bTrue0Tex);
    SafeRelease(m->bEst0SRV); SafeRelease(m->bEst0UAV); SafeRelease(m->bEst0Tex);
    SafeRelease(m->fields0SRV); SafeRelease(m->fields0UAV); SafeRelease(m->fields0Tex);
    SafeRelease(m->gain0SRV); SafeRelease(m->gain0UAV); SafeRelease(m->gain0Tex);
    SafeRelease(m->zoneListSRV); SafeRelease(m->zoneListBuf);
    SafeRelease(m->changeFlagsUAV); SafeRelease(m->changeFlagsBuf);
    SafeRelease(m->argsUAV); SafeRelease(m->argsBuf);
    m->stateComplete = false; m->fieldsStand = false; m->planPartial = false; m->lastPartial = false; m->runMode = 0;
    m->lastSettingsKey = 0; m->lastLutPassKey = 0; m->planSettingsKey = 0; m->touched.clear(); m->touchedOk = false; m->rects.clear();
    m->lastTrueMap = 0xFFFFFFFFu; m->lastEstMap = 0xFFFFFFFFu;
    SafeRelease(m->zonePartUAV); SafeRelease(m->zonePartBuf); m->zoneSlices = 1;
    for (int i = 0; i < 2; i++) {
        SafeRelease(m->boostSRV[i]); SafeRelease(m->boostUAV[i]); SafeRelease(m->boostTex[i]);
        SafeRelease(m->activeSRV[i]); SafeRelease(m->activeUAV[i]); SafeRelease(m->activeTex[i]);
    }
    SafeRelease(m->flatEstSRV); SafeRelease(m->flatEstUAV); SafeRelease(m->flatEstTex);
    SafeRelease(m->flatTrueSRV); SafeRelease(m->flatTrueUAV); SafeRelease(m->flatTrueTex);
    SafeRelease(m->gainBSRV); SafeRelease(m->gainBUAV); SafeRelease(m->gainBTex);
    SafeRelease(m->gainASRV); SafeRelease(m->gainAUAV); SafeRelease(m->gainATex);
    SafeRelease(m->fieldsSRV); SafeRelease(m->fieldsUAV); SafeRelease(m->fieldsTex);
    SafeRelease(m->bEstSRV); SafeRelease(m->bEstUAV); SafeRelease(m->bEstTex);
    SafeRelease(m->bTrueSRV); SafeRelease(m->bTrueUAV); SafeRelease(m->bTrueTex);
    SafeRelease(m->driveSRV); SafeRelease(m->driveUAV); SafeRelease(m->driveTex);
    SafeRelease(m->kEst4SRV); SafeRelease(m->kEstSRV); SafeRelease(m->kEstBuf);
    SafeRelease(m->kTrue4SRV); SafeRelease(m->kTrueSRV); SafeRelease(m->kTrueBuf);
    SafeRelease(m->curveSRV); SafeRelease(m->curveTex);
    SafeRelease(m->interSRV); SafeRelease(m->interRTV); SafeRelease(m->interTex);
    SafeRelease(m->cleanSRV); SafeRelease(m->cleanTex);
    ReleaseTemporal(m);
    ReleaseStar(m);
    FaldFadeDestroy(m->fade); m->fade = nullptr; m->fadeOn = false; m->fadeKey = 0; m->fadeFailed = false;
    FaldCore2Destroy(m->core2); m->core2 = nullptr; m->core2On = false;
    m->spareParams = FaldPanelParams{};
    m->primed = false;
    m->valid = false;
}

void FaldReleaseAll() {
    // Detach (under the loader lock), or a failed InitializeStuff: give a present thread that is in the table or
    // building a bounded chance to leave (one ingest / one build; yields, no Sleep), and never wait unboundedly — a
    // thread still inside after that is the pre-existing in-flight-present hazard of any detach, not a reason to hang
    // dwm.exe.
    bool locked = g_panelsLock.TryLock();
    if (!locked) {
        LARGE_INTEGER now, freq, until;
        QueryPerformanceFrequency(&freq);
        QueryPerformanceCounter(&now);
        until.QuadPart = now.QuadPart + freq.QuadPart / 20;   // 50 ms at most
        while (!(locked = g_panelsLock.TryLock())) {
            QueryPerformanceCounter(&now);
            if (now.QuadPart >= until.QuadPart) break;
            SwitchToThread();
        }
    }
    if (!locked) log_to_file("FALD: releasing without the panel table lock (a present thread kept it)");
    struct Unlock { bool on; ~Unlock() { if (on) g_panelsLock.Unlock(); } } unlock{ locked };
    g_faldReleasing.store(true);                          // an append refuses from here on
    for (int i = 0; i < FaldCoreTiming::kSlots; i++) g_coreTiming.owner[i] = nullptr;   // owners are about to go
    const int count = g_numMonitors.exchange(0);          // a lookup finds no entry from here on (an append's CAS fails)
    int leaked = 0;
    for (int i = 0; i < count; i++) {
        // Claimed first, like any rebuild: an entry a present still uses (or a lock holder is rebuilding, on the timeout
        // path) is LEFT ALIVE — taken out of the list and leaked — rather than freed under it. The claim is kept, so a
        // borrower that found it before it left the list backs off; a new entry at this index resets the gate.
        if (!g_entryGate[i].TryBeginRebuild()) { g_monitors[i].store(nullptr); leaked++; continue; }
        FaldMonitor* e = g_monitors[i].exchange(nullptr);
        if (!e) continue;
        ReleaseMonitor(e);
        if (e->settleEvt) CloseHandle(e->settleEvt);
        if (e->contentEvt) CloseHandle(e->contentEvt);
        delete e;
    }
    if (leaked) LogF("FALD: %d layer entr%s still in use at release - left allocated", leaked, leaked == 1 ? "y" : "ies");
    for (int i = 0; i < FALD_MAX_PANEL_FILES; i++) {
        g_stalePending[i].store(false); g_entryNeedsSwap[i].store(false); g_entryKicks[i].store(0);
    }
    // The panel files go with them, as before (staged ones are not re-read; a delivered one is taken again from its
    // section, which the host keeps open, at the next present: its generation is no longer marked processed). Not on
    // the timeout path: a lock holder may be inside the table — its memory is left as it is.
    if (locked) {
        g_panels.Clear();
        ReleaseDeliveryViews();
        g_anyPanelPresent.store(false);
    }
    // The fade governor's status section: unmapped only when no entry is still in use (a present inside FaldRun may be
    // writing it); otherwise left mapped, like a leaked entry.
    if (locked && leaked == 0) CloseFaldStatus();
    g_faldReleasing.store(false);
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

// The zone sweeps' slice partials (fald_shader.h ZonePart): cols * rows * slices records, UAV only.
static bool MakeZonePartBuffer(UINT count, ID3D11Buffer** buf, ID3D11UnorderedAccessView** uav) {
    const UINT stride = FALD_ZONE_PART_BYTES;
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = count * stride;
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    bd.StructureByteStride = stride;
    if (FAILED(g_dev->CreateBuffer(&bd, nullptr, buf))) return false;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_UNKNOWN;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = count;
    return SUCCEEDED(g_dev->CreateUnorderedAccessView(*buf, &ud, uav));
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
    if (FAILED(g_dev->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_UNKNOWN;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = count;
    return SUCCEEDED(g_dev->CreateUnorderedAccessView(*buf, &ud, uav));
}

// The touched-zone list (fald_shader.h zoneList, t38): a dynamic buffer of `count` uints the host maps per present.
static bool MakeZoneList(UINT count, ID3D11Buffer** buf, ID3D11ShaderResourceView** srv) {
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = count * 4u;
    bd.Usage = D3D11_USAGE_DYNAMIC;
    bd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_dev->CreateBuffer(&bd, nullptr, buf))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32_UINT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = count;
    return SUCCEEDED(g_dev->CreateShaderResourceView(*buf, &sd, srv));
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
    if (FAILED(g_dev->CreateBuffer(&bd, &init, buf))) return false;
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R32_TYPELESS;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = FALD_ARGS_BYTES / 4u;
    ud.Buffer.Flags = D3D11_BUFFER_UAV_FLAG_RAW;
    return SUCCEEDED(g_dev->CreateUnorderedAccessView(*buf, &ud, uav));
}

// A float4 view of a float buffer whose size is a multiple of 4 floats (the conv pass's kernel reads, t36 / t37).
static bool MakeFloat4View(ID3D11Buffer* buf, size_t floats, ID3D11ShaderResourceView** srv) {
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    sd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sd.Buffer.FirstElement = 0;
    sd.Buffer.NumElements = (UINT)(floats / 4);
    return SUCCEEDED(g_dev->CreateShaderResourceView(buf, &sd, srv));
}

// The five cols x rows RGBA32F starfield textures (src/fald.cpp EnsureStar). A failure is latched per
// build: logged once, the feature stays off for this monitor until the next rebuild (resize / mode).
static bool EnsureStar(FaldMonitor* m) {
    if (m->starStatTex && m->starWTex && m->starPlanTex && m->starBgTex && m->starPlan2Tex) return true;
    if (m->starFailed || !StarShadersReady()) return false;
    if (m->zoneSlices > 1 && !g_starStatCombineCS) {                  // zones of several slices: S0 needs its combine
        m->starFailed = true;
        LogF("FALD: pos(%d,%d) starfield combine shader unavailable - starfield stays off", m->left, m->top);
        return false;
    }
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

// The fade stack for this frame size (shared/fald_fade.h). Its passes ship as bytecode — no shader compiler in DWM's
// present — and are created FALD_FADE_SHADERS_PER_RUN per present the first time any monitor wants them; its resources
// (~120, ~550 MB at 4K) FALD_FADE_RESOURCES_PER_RUN / FALD_FADE_BYTES_PER_RUN per present. Until both are complete the
// layer runs without the fade (CB word 75 = 0: today's layer). A failure is latched (shaders: until released; resources:
// fadeFailed, until the next rebuild) and logged once.
static bool EnsureFade(FaldMonitor* m) {
    if (m->fadeFailed || g_fadeShadersFailed) return false;
    if (!FaldFadeShadersReady()) {
        std::string err;
        const FaldFadeStatus st = FaldFadeShadersStep(g_dev, g_ctx, FALD_FADE_SHADERS_PER_RUN, &err);
        if (st == FaldFadeStatus::Failed) {
            g_fadeShadersFailed = true;
            LogF("FALD: fade stack shaders could not be created (%s) - the flank fade stays off", err.c_str());
            return false;
        }
        if (st == FaldFadeStatus::Pending) return false;
        log_to_file("FALD: fade stack shaders created from their bytecode and warmed");
    }
    if (!m->fade) m->fade = FaldFadeCreate();
    if (!m->fade) { m->fadeFailed = true; return false; }
    const FaldFadeStatus st = FaldFadeEnsureStep(m->fade, g_dev, m->width, m->height, FALD_FADE_RESOURCES_PER_RUN,
                                                 FALD_FADE_BYTES_PER_RUN, g_ctx);
    if (st == FaldFadeStatus::Ready) return true;
    if (st == FaldFadeStatus::Pending) return false;
    m->fadeFailed = true;
    LogF("FALD: pos(%d,%d) fade stack textures could not be created - the flank fade stays off", m->left, m->top);
    return false;
}

// Resolve this frame's starfield state from the live request (FaldSetLiveSettings). Off =
// textures released and nothing downstream knows the feature exists (CB word 35 = 0,
// t15 / t18 unbound) — the bit-identical-when-off rule of src/fald.cpp. The flank fade likewise (CB word 75 = 0, t20-t22 unbound).
static void ResolveFeatures(FaldMonitor* m) {
    const bool wasStar = m->starOn;
    if (m->starWanted) m->starOn = EnsureStar(m);
    else { if (m->starStatTex || m->starOn) ReleaseStar(m); m->starFailed = false; }   // off: the next enable retries
    if (m->starOn != wasStar)
        LogF("FALD: pos(%d,%d) %s starfield %s", m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->starOn ? "ON" : "off");
    const bool wasFade = m->fadeOn;
    // The fade's governor (shared/fald_fade_governor.h): the adapter + the force key, the mode (size + refresh; the mode
    // itself is this entry's), the setting. Suspended = off below (the setting stays on; the resources go back, a retry
    // rebuilds them, paced).
    m->fadeGov.Configure(FadeWeakAdapter(), m->fadeForce);
    m->fadeGov.SetRefreshMs(m->refreshMs);
    m->fadeGov.SetMode(((unsigned long long)m->width << 40) ^ ((unsigned long long)m->height << 20) ^
                       (unsigned long long)(m->refreshMs * 1000.0f + 0.5f));
    m->fadeGov.SetEnabled(m->fadeWanted);
    // The core layer's rate-cap governor (shared/fald_core_governor.h): the force key, the refresh interval, the mode
    m->coreGov.Configure(m->coreForce);
    m->coreGov.SetRefreshMs(m->refreshMs);
    m->coreGov.SetMode(((unsigned long long)m->width << 40) ^ ((unsigned long long)m->height << 20) ^
                       (unsigned long long)(m->refreshMs * 1000.0f + 0.5f));
    m->coreGov.SetEnabled(true);
    if (m->fadeWanted && m->fadeGov.Allowed()) m->fadeOn = EnsureFade(m);
    else {                                                        // off: released, nothing below knows it exists
        if (m->fade) { FaldFadeDestroy(m->fade); m->fade = nullptr; }
        m->fadeOn = false; m->fadeFailed = false; m->fadeKey = 0;
    }
    const bool wasCore2 = m->core2On;                             // the always-on core (shared/fald_core2.h)
    const uint32_t wasCoreFlags = m->core2Flags;
    m->core2Flags = FaldCore2FlagsOfMode(m->coreModeWanted);
    if (m->core2Flags && FaldCore2ShadersReady()) {
        if (!m->core2) m->core2 = FaldCore2Create();
        m->core2On = FaldCore2Ensure(m->core2, g_dev, m->params.cols, m->params.rows, m->params.sub, m->zoneSlices);
    } else if (m->core2 || m->core2On) {
        FaldCore2Destroy(m->core2); m->core2 = nullptr; m->core2On = false;
    }
    if (m->core2On != wasCore2 || (m->core2On && m->core2Flags != wasCoreFlags))
        LogF("FALD: pos(%d,%d) %s always-on core %s (mode %u = %s%s)", m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->core2On ? "ON" : "off",
             m->coreModeWanted, FaldCoreModeName(m->coreModeWanted),
             (m->core2On && FaldCore2Stateful(m->core2Flags)) ? "; temporal state: the full path every present" : "");
    if (m->fadeOn != wasFade) {
        const FaldFadeGovernorStatus gs = m->fadeGov.Status();
        LogF("FALD: pos(%d,%d) %s flank fade %s (%llu MB; governor %s, p90 %.2f ms, budget %.2f ms, content %.1f ms)", m->left,
             m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->fadeOn ? "ON" : "off", m->fadeOn ? FaldFadeVramBytes(m->fade) >> 20 : 0ull,
             FaldFadeGovStateName(gs.state), gs.p90Us / 1000.0, gs.budgetUs / 1000.0, gs.contentMs);
    }
}

static void ComputeFlatResponse(FaldMonitor* m);

// The panel file's lattice must lie inside the frame (checked before anything is created; logged).
static bool PanelFits(FaldMonitor* m) {
    const FaldPanelParams& p = m->params;
    if (FaldLatticeFits(p, (int)m->width, (int)m->height)) return true;
    LogF("FALD: panel lattice (%ux%u) does not fit monitor %ux%u at pos(%d,%d) — layer off",
         p.latW, p.latH, m->width, m->height, m->left, m->top);
    return false;
}

// The FRAME part of an entry: what depends on the back buffer (size, format) and on the composed frames seen, never on
// the panel file — the full-size intermediate, the clean source with its priming coverage. A live panel-file swap keeps
// it (MoveFramePart), so the layer switches files without a re-prime and without a gap.
static bool BuildFrame(FaldMonitor* m) {
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
    return true;
}

// The PANEL part: everything built from m->params (tables, per-frame fields, flat response, CB). Sets valid.
static bool BuildTables(FaldMonitor* m) {
    const FaldPanelParams& p = m->params;
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
    // the conv pass's kernel tables, laid out for its variant (shared/fald_panel.h FaldConvTableLayout)
    m->convVariant = FaldConvVariant(p.cols, p.rows, p.sub);
    {
        const unsigned int S = FaldConvSoOf(m->convVariant);
        const std::vector<float> kT = FaldConvTableLayout(p.kTrue, p.sub, 2 * p.reachTrueC + 1, 2 * p.reachTrueR + 1, S);
        const std::vector<float> kE = FaldConvTableLayout(p.kEst, p.sub, 2 * p.reachEstC + 1, 2 * p.reachEstR + 1, S);
        if (!MakeFloatBuffer(kT, &m->kTrueBuf, &m->kTrueSRV) || !MakeFloat4View(m->kTrueBuf, kT.size(), &m->kTrue4SRV)) { log_to_file("FALD: kTrue buffer failed"); return false; }
        if (!MakeFloatBuffer(kE, &m->kEstBuf, &m->kEstSRV) || !MakeFloat4View(m->kEstBuf, kE.size(), &m->kEst4SRV)) { log_to_file("FALD: kEst buffer failed"); return false; }
    }
    if (!MakeRWTexture(p.cols, p.rows, &m->driveTex, &m->driveUAV, &m->driveSRV)) { log_to_file("FALD: drive texture failed"); return false; }
    // round 0's own maps (the change-driven path compares them run to run; round 1's are the textures named as before)
    if (!MakeRWTexture(p.cols, p.rows, &m->drive0Tex, &m->drive0UAV, &m->drive0SRV)) { log_to_file("FALD: drive texture (round 0) failed"); return false; }
    if (!MakeRWTexture(p.cols, p.rows, &m->driveFilt0Tex, &m->driveFilt0UAV, &m->driveFilt0SRV)) { log_to_file("FALD: filtered drive texture (round 0) failed"); return false; }
    // LED lag (modes 1 / 2): the filtered drive, the committed state and the delay ring (as src/fald.cpp Build)
    if (!MakeRWTexture(p.cols, p.rows, &m->driveFiltTex, &m->driveFiltUAV, &m->driveFiltSRV)) { log_to_file("FALD: filtered drive texture failed"); return false; }
    if (!MakeRWTexture(p.cols, p.rows, &m->driveStateTex, &m->driveStateUAV, &m->driveStateSRV)) { log_to_file("FALD: drive state texture failed"); return false; }
    for (unsigned int i = 0; i < FALD_DELAY_MAX; i++)
        if (!MakeRWTexture(p.cols, p.rows, &m->delayTex[i], &m->delayUAV[i], &m->delaySRV[i])) { log_to_file("FALD: delay ring texture failed"); return false; }
    m->clockFailed = false;
    const UINT fw = p.cols * p.sub, fh = p.rows * p.sub;
    if (!MakeRWTexture(fw, fh, &m->bTrue0Tex, &m->bTrue0UAV, &m->bTrue0SRV)) { log_to_file("FALD: B_true texture (round 0) failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->bEst0Tex, &m->bEst0UAV, &m->bEst0SRV)) { log_to_file("FALD: B_est texture (round 0) failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->fields0Tex, &m->fields0UAV, &m->fields0SRV, DXGI_FORMAT_R32G32B32A32_FLOAT)) { log_to_file("FALD: packed fields texture (round 0) failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->gain0Tex, &m->gain0UAV, &m->gain0SRV, DXGI_FORMAT_R32G32_FLOAT)) { log_to_file("FALD: gain texture (round 0) failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->bTrueTex, &m->bTrueUAV, &m->bTrueSRV)) { log_to_file("FALD: B_true texture failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->bEstTex, &m->bEstUAV, &m->bEstSRV)) { log_to_file("FALD: B_est texture failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->fieldsTex, &m->fieldsUAV, &m->fieldsSRV, DXGI_FORMAT_R32G32B32A32_FLOAT)) { log_to_file("FALD: packed fields texture failed"); return false; }
    // R32G32F: (gain, the flat-normalised B_est of the soft knee's ceiling), low-passed together (C15)
    if (!MakeRWTexture(fw, fh, &m->gainATex, &m->gainAUAV, &m->gainASRV, DXGI_FORMAT_R32G32_FLOAT)) { log_to_file("FALD: gain texture A failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->gainBTex, &m->gainBUAV, &m->gainBSRV, DXGI_FORMAT_R32G32_FLOAT)) { log_to_file("FALD: gain texture B failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->flatTrueTex, &m->flatTrueUAV, &m->flatTrueSRV)) { log_to_file("FALD: flat B_true texture failed"); return false; }
    if (!MakeRWTexture(fw, fh, &m->flatEstTex, &m->flatEstUAV, &m->flatEstSRV)) { log_to_file("FALD: flat B_est texture failed"); return false; }
    m->gainFused = FaldGainBlurFused(p.gainSmoothCells, p.sub) && g_faldGainFusedOverride != 0;   // passes 2b + 2c in one dispatch
    m->zoneSlices = FaldZoneSlices(p.cellW, p.cellH);            // zones larger than one slice: the sweeps' partials
    if (m->zoneSlices > FALD_ZONE_SLICES_MAX) {
        LogF("FALD: zones of %ux%u px need %u sweep slices (max %u) - layer off", p.cellW, p.cellH, m->zoneSlices, FALD_ZONE_SLICES_MAX);
        return false;
    }
    if (m->zoneSlices > 1 && !g_statCombineCS) { log_to_file("FALD: zone combine shader unavailable - layer off for this lattice"); return false; }
    if (m->zoneSlices > 1 && !MakeZonePartBuffer(p.cols * p.rows * m->zoneSlices, &m->zonePartBuf, &m->zonePartUAV)) {
        log_to_file("FALD: zone partials buffer failed"); return false;
    }
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
        if (!MakeCounterBuffer(2, &m->boostCountBuf, &m->boostCountUAV)) { log_to_file("FALD: boost counters failed"); return false; }
        const float zero[4] = { 0.0f, 0.0f, 0.0f, 0.0f };   // the flag maps start all zero: the counters follow them by deltas
        g_ctx->ClearUnorderedAccessViewFloat(m->activeUAV[0], zero);
        g_ctx->ClearUnorderedAccessViewFloat(m->activeUAV[1], zero);
    }
    // the change-driven path: the touched-zone list, the change flags, the decide pass's indirect arguments
    if (!MakeZoneList(p.cols * p.rows, &m->zoneListBuf, &m->zoneListSRV)) { log_to_file("FALD: zone list failed"); return false; }
    if (!MakeCounterBuffer(FALD_CHANGE_FLAGS, &m->changeFlagsBuf, &m->changeFlagsUAV)) { log_to_file("FALD: change flags failed"); return false; }
    if (!MakeArgsBuffer(&m->argsBuf, &m->argsUAV)) { log_to_file("FALD: indirect arguments failed"); return false; }
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = FALD_CB_BYTES;
    cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_dev->CreateBuffer(&cbd, nullptr, &m->cb))) { log_to_file("FALD: constant buffer failed"); return false; }

    m->valid = true;
    ComputeFlatResponse(m);
    FaldUnbindAll();   // the flat pass bound CS b0 / s0; a build can end the present without FaldRun (unprimed)
    LogF("FALD: monitor pos(%d,%d) %s ready: %ux%u cells on a %ux%u-px lattice%s, sub %u, white %.1f nits, transfer %s, boost %s",
         m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", p.cols, p.rows, p.latW, p.latH,
         FaldLatticeFractional(p) ? " (fractional pitch)" : "", p.sub, p.white,
         p.transfer == FALD_TRANSFER_GAMMA ? "gamma" : "PQ", p.hasBoost ? "yes" : "none");
    return true;
}

static bool BuildMonitor(FaldMonitor* m, const FaldPanelParams& params) {
    ReleaseMonitor(m);
    m->params = params;
    m->starFailed = false;
    return PanelFits(m) && BuildFrame(m) && BuildTables(m);
}

// Hand the frame part (BuildFrame) and what belongs to the monitor rather than to a panel file — its settle / content
// events, its cost counters — from `from` to `to`, leaving `from` without them. Everything NOT moved here is the panel
// part, and a live swap builds it fresh: a field added to FaldMonitor is reset by a swap unless it is listed here.
static void MoveFramePart(FaldMonitor* to, FaldMonitor* from) {
    to->interTex = from->interTex; to->interRTV = from->interRTV; to->interSRV = from->interSRV;
    to->cleanTex = from->cleanTex; to->cleanSRV = from->cleanSRV;
    from->interTex = nullptr; from->interRTV = nullptr; from->interSRV = nullptr;
    from->cleanTex = nullptr; from->cleanSRV = nullptr;
    to->primed = from->primed;
    to->cover = std::move(from->cover); from->cover.clear();
    to->coverCols = from->coverCols; to->coverRows = from->coverRows; to->coverLeft = from->coverLeft;
    to->offPresents = from->offPresents;
    to->settleEvt = from->settleEvt; to->contentEvt = from->contentEvt; to->evtOpenTick = from->evtOpenTick;
    from->settleEvt = NULL; from->contentEvt = NULL;
    to->framesRun = from->framesRun; to->lastRunUs = from->lastRunUs;
    to->gpuSumUs = from->gpuSumUs; to->gpuStarSumUs = from->gpuStarSumUs; to->gpuMaxUs = from->gpuMaxUs;
    to->gpuCoreSumUs = from->gpuCoreSumUs;
    to->gpuSamples = from->gpuSamples;
    to->coreGov = from->coreGov;                          // the core governor too (its cost is the frame's, not the file's)
    to->coreGovWrittenGen = from->coreGovWrittenGen; to->coreGovWriteRuns = from->coreGovWriteRuns;
    to->coreGovLoggedGen = from->coreGovLoggedGen; to->heldRuns = from->heldRuns;
    to->fade = from->fade; from->fade = nullptr;          // the fade stack's textures depend on the frame size only
    to->fadeGov = from->fadeGov;                          // its governor too (its cost is the frame's, not the file's)
    to->fadeGovWrittenGen = from->fadeGovWrittenGen; to->fadeGovWriteRuns = from->fadeGovWriteRuns;
    to->cleanGen = from->cleanGen;
}

// Live panel-file swap of a RUNNING entry (a delivery replaced its slot's file). The new panel part is built into a
// scratch entry that borrows m's frame part, so the flat-response pass binds exactly what a fresh build binds; m keeps
// its old panel part meanwhile. Built: the old panel part is released and the scratch's contents become m (same object:
// nothing that points at m — the GPU timing ring — goes stale). Not built (logged inside): the frame part goes back
// and m runs on as it was, on the previous file. Either way this happens between two presents of the monitor: the
// present that calls it runs on one file from its first pass to its last.
// What a swap resets: the panel tables + fields + flat response (new kernels), the LED-lag state (the drive maps and
// clocks it filters were computed under the old file's model: the first run after a swap starts from this frame's
// drive, as a first run after a build does), the starfield textures (rebuilt on demand for the new lattice). What it
// keeps: the clean source and its priming (they hold the composed frame, which no panel file touches), the events,
// the live settings, the cost counters.
static bool SwapPanel(FaldMonitor* m, const FaldPanelParams& params, uint32_t seq) {
    FaldMonitor* n = new (std::nothrow) FaldMonitor();
    if (!n) return false;
    n->left = m->left; n->top = m->top; n->isHdr = m->isHdr;
    n->width = m->width; n->height = m->height; n->format = m->format;
    n->debugMode = m->debugMode; n->pedMode = m->pedMode; n->starWanted = m->starWanted;
    n->tuning = m->tuning; n->tempSettings = m->tempSettings; n->refreshMs = m->refreshMs;
    n->fadeWanted = m->fadeWanted; n->fadeForce = m->fadeForce; n->coreModeWanted = m->coreModeWanted;
    n->exemptMonW = m->exemptMonW; n->exemptMonH = m->exemptMonH;
    bool built = false, moved = false;
    try {
        n->params = std::move(m->spareParams);      // this entry's spare memory ...
        n->params = params;                         // ... reused by the copy when large enough
        n->starFailed = false;
        MoveFramePart(n, m);
        moved = true;
        built = PanelFits(n) && BuildTables(n);   // (binds nothing unless it succeeds: the flat pass is its last step)
    } catch (...) {   // bad_alloc copying the tables: nothing of this file may escape into DWM's present
        log_to_file("FALD: live panel swap ran out of memory");
        built = false;
    }
    if (!built) {
        if (moved) MoveFramePart(m, n);
        ReleaseMonitor(n);     // the half-built panel part (no frame part, no events)
        m->spareParams = std::move(n->params);
        delete n;
        return false;
    }
    n->panelSeq = seq;
    FaldPanelParams old = std::move(m->params);
    ReleaseMonitor(m);         // the old panel part (m holds no frame part now)
    *m = std::move(*n);        // m: the new panel part + its own frame part, events and counters back
    delete n;                  // a moved-from shell: its COM pointers are m's now (FaldMonitor releases nothing itself)
    m->spareParams = std::move(old);
    FaldFadeReset(m->fade);    // the fade stack's T state: a panel-file switch is a cut (the next frame has no history)
    return true;
}

static FaldMonitor* FindMonitorEntry(int left, int top, bool isHdr, int* index) {
    const int n = MonitorCount();
    for (int i = 0; i < n; i++) {
        FaldMonitor* e = MonitorAt(i);
        if (e && e->left == left && e->top == top && e->isHdr == isHdr) { if (index) *index = i; return e; }
    }
    if (index) *index = -1;
    return nullptr;
}

// A claim on entry `index` for the scope (g_panelsLock held): refused (owned() false) while a present borrows it.
struct EntryClaim {
    int index;
    bool ok;
    explicit EntryClaim(int i) : index(i), ok(i >= 0 && g_entryGate[i].TryBeginRebuild()) {}
    ~EntryClaim() { if (ok) g_entryGate[index].EndRebuild(); }
    bool owned() const { return ok; }
};

// Borrow entry `i` (found as `e`) for this present: false when it is claimed, or no longer the entry at `i`.
static FaldMonitor* BorrowEntry(int i, FaldMonitor* e) {
    if (i < 0 || !e || !g_entryGate[i].TryBorrow()) return nullptr;
    if (MonitorAt(i) != e) { g_entryGate[i].Return(); return nullptr; }   // released between the lookup and the borrow
    return e;
}

void FaldReturn(FaldMonitor* m) {
    if (!m) return;
    for (int i = 0, n = MonitorCount(); i < n; i++)
        if (MonitorAt(i) == m) { g_entryGate[i].Return(); return; }
    // not in the list any more: FaldReleaseAll left it allocated (it was borrowed) and nothing will count it again
}

// A panel file for (left, top, isHdr) changed in the table (g_panelsLock held): its entry, if any, owes a swap.
static void NoteSlotChanged(int left, int top, bool isHdr) {
    int i = -1;
    if (FindMonitorEntry(left, top, isHdr, &i)) { g_entryKicks[i].store(0); g_entryNeedsSwap[i].store(true); }
}

FaldMonitor* FaldAcquire(int left, int top, bool isHdr, unsigned int width, unsigned int height,
                         DXGI_FORMAT format) {
    if (!FaldShadersReady() || width == 0 || height == 0) return nullptr;

    FaldPanelLockGuard lock(g_panelsLock);
    if (!lock.owned()) {
        // Another present thread is in the table (a delivery, a build): this present BORROWS the entry as it is, if it
        // can — a swap it owes waits for a later present (asked for). It stays off once more (the caller marks it stale
        // again; nothing of the layer was on screen for it) when it is claimed by a rebuild, not running, or has a
        // stale mark still to apply.
        int i = -1;
        FaldMonitor* found = FindMonitorEntry(left, top, isHdr, &i);   // (sets i: a statement of its own)
        FaldMonitor* e = BorrowEntry(i, found);
        if (!e) return nullptr;
        if (g_stalePending[i].load() || !e->valid || e->width != width || e->height != height || e->format != format) {
            g_entryGate[i].Return();
            return nullptr;
        }
        RequestDeferredSwap(i);
        return e;
    }
    ApplyPendingStale(left, top);
    int mIndex = -1;
    FaldMonitor* m = FindMonitorEntry(left, top, isHdr, &mIndex);
    // The same position's entry for the OTHER mode never sees this mode's presents: its copy and LED-lag state are
    // void, and holding its two full-size frame textures across an HDR <-> SDR switch only costs VRAM. Free it; a
    // switch back rebuilds it fresh (the overlay resets its state on a mode switch too).
    for (int i = 0, n = MonitorCount(); i < n; i++) {
        FaldMonitor* e = MonitorAt(i);
        if (e && e != m && e->left == left && e->top == top && e->isHdr != isHdr && e->valid) {
            EntryClaim claim(i);
            if (!claim.owned()) continue;   // in use (a context of the other mode still presenting): a later present
            ReleaseMonitor(e);
            CoverReset(e);
        }
    }
    const int slot = g_panels.IndexOf(left, top, isHdr);
    const FaldPanelSlot* pf = slot >= 0 ? &g_panels.At(slot) : nullptr;
    FaldLayerBuildState state;   // shared/fald_panel_live.h FaldLayerDecide: the latch rules, unit-tested
    if (m) {
        state.valid = m->valid; state.failed = m->failed;
        state.panelSeq = m->panelSeq; state.failedSeq = m->failedSeq; state.refusedSeq = m->refusedSeq;
    }
    const bool sameFrame = m && m->width == width && m->height == height && m->format == format;
    switch (FaldLayerDecide(state, sameFrame, pf)) {
    case FaldLayerAction::Run:
        g_entryNeedsSwap[mIndex].store(false);   // built from the slot's file (or a failed swap of it: not retried)
        return BorrowEntry(mIndex, m);
    case FaldLayerAction::Off: return nullptr;
    case FaldLayerAction::Build: break;   // below
    case FaldLayerAction::Swap: {
            // A file delivered live since this entry was built (dwm_hook_config.h DwmHookFaldPanelBlob): swap its panel
            // part now, before any pass of this present. A failed swap is not retried for the same file. Another
            // present still using the entry (a twin context on another thread) defers it: this one runs the entry as it
            // is, and a later present — asked for — swaps.
            g_entryNeedsSwap[mIndex].store(true);
            EntryClaim claim(mIndex);
            if (!claim.owned()) {
                RequestDeferredSwap(mIndex);
                return BorrowEntry(mIndex, m);
            }
            LARGE_INTEGER t0, t1, f;
            QueryPerformanceCounter(&t0);
            if (SwapPanel(m, pf->params, pf->seq)) {
                QueryPerformanceCounter(&t1); QueryPerformanceFrequency(&f);
                LogF("FALD: pos(%d,%d) %s switched to the panel file delivered live (gen %u) between two presents "
                     "(clean source kept %s, LED-lag state reset), build %.0f us",
                     left, top, isHdr ? "HDR" : "SDR(ACM)", pf->gen, m->primed ? "primed" : "priming",
                     f.QuadPart > 0 ? (double)(t1.QuadPart - t0.QuadPart) * 1e6 / (double)f.QuadPart : 0.0);
                AckSlot(slot, DWM_HOOK_FALD_PANEL_ACK_APPLIED);
                g_panels.Commit(left, top, isHdr);
            } else {
                const uint32_t failedGen = pf->gen;
                m->refusedSeq = pf->seq;
                AckSlot(slot, DWM_HOOK_FALD_PANEL_ACK_BUILD_FAILED);   // (before the revert: the slot still names it)
                // The slot goes back to the file this entry runs (the last one built), so nothing later — a resize, a
                // mode switch, a release after 600 presents off — rebuilds the file that just failed.
                uint32_t seq = 0;
                if (g_panels.RevertBuild(left, top, isHdr, &seq)) m->panelSeq = seq;
                LogF("FALD: pos(%d,%d) %s could not build the panel file delivered live (gen %u) - the layer stays on "
                     "the previous file", left, top, isHdr ? "HDR" : "SDR(ACM)", failedGen);
            }
            g_entryNeedsSwap[mIndex].store(false);
            RefreshAnyPanelPresent();
        }
        return BorrowEntry(mIndex, m);   // (the claim has ended: the destructor ran with the block)
    }
    // Build: first sight, a resize, released (layer off for a while, or a mode switch), or failed on a file since
    // replaced. FaldLayerDecide returns Build only with a file in the slot. The entry is claimed for the build (a new one
    // before anyone can see it); the claim ends with the block, and only then is the entry borrowed for this present.
    {
        bool claimedNew = false;
        if (!m) {
            const int n = MonitorCount();
            if (n >= FALD_MAX_PANEL_FILES || g_faldReleasing.load()) return nullptr;
            m = new (std::nothrow) FaldMonitor();
            if (!m) return nullptr;
            m->left = left; m->top = top; m->isHdr = isHdr;
            m->tuning = DefaultTuning();
            // Claimed, then published (pointer first, count last): a concurrent reader either does not see the slot or
            // sees a complete, claimed entry. The count moves only from what it was: a FaldReleaseAll on its timeout
            // path that zeroed it meanwhile makes this append undo itself (nothing leaks, the count stays 0).
            g_entryGate[n].ClaimNew();
            g_stalePending[n].store(false); g_entryNeedsSwap[n].store(false); g_entryKicks[n].store(0);
            g_monitors[n].store(m);
            int expected = n;
            if (!g_numMonitors.compare_exchange_strong(expected, n + 1)) {
                g_monitors[n].store(nullptr);
                g_entryGate[n].EndRebuild();
                delete m;
                return nullptr;
            }
            mIndex = n;
            claimedNew = true;
        }
        // A build of an existing entry claims it like a swap: one still in use (a twin context on another thread, at its
        // old size) is rebuilt by a later present — asked for.
        EntryClaim claim(claimedNew ? -1 : mIndex);
        struct NewClaim { int i; ~NewClaim() { if (i >= 0) g_entryGate[i].EndRebuild(); } } newClaim{ claimedNew ? mIndex : -1 };
        if (!claimedNew && !claim.owned()) {
            g_entryNeedsSwap[mIndex].store(true);
            RequestDeferredSwap(mIndex);
            return nullptr;
        }
        // A resize (or the first sight of this monitor) rebuilds and clears the latch: the lattice may
        // fit the new frame even though it did not fit the old one.
        m->width = width; m->height = height; m->format = format;
        m->failed = false;
        m->refusedSeq = 0;
        if (!BuildMonitor(m, pf->params)) {
            ReleaseMonitor(m);
            m->failed = true;     // logged once inside BuildMonitor; no per-frame retry inside DWM
            m->failedSeq = pf->seq;
            AckSlot(slot, DWM_HOOK_FALD_PANEL_ACK_BUILD_FAILED);
            // A delivered file goes back to the one it replaced (its seq moves on, so the next present builds that one,
            // once); a staged file has nothing to go back to and stays latched off, as before.
            g_panels.RevertBuild(left, top, isHdr, nullptr);
            g_entryNeedsSwap[mIndex].store(false);
            RefreshAnyPanelPresent();
            return nullptr;
        }
        m->panelSeq = pf->seq;
        AckSlot(slot, DWM_HOOK_FALD_PANEL_ACK_APPLIED);
        g_panels.Commit(left, top, isHdr);
        g_entryNeedsSwap[mIndex].store(false);
    }
    return BorrowEntry(mIndex, m);
}

ID3D11RenderTargetView* FaldIntermediateRTV(FaldMonitor* m) { return m ? m->interRTV : nullptr; }
ID3D11Texture2D* FaldIntermediateTexture(FaldMonitor* m) { return m ? m->interTex : nullptr; }
ID3D11Texture2D* FaldCleanTexture(FaldMonitor* m) { return m ? m->cleanTex : nullptr; }
ID3D11ShaderResourceView* FaldCleanSRV(FaldMonitor* m) { return m ? m->cleanSRV : nullptr; }

// The clean copy went stale (created, or a present of this monitor the layer did not see): every tile owes a refresh.
static void CoverReset(FaldMonitor* m) {
    m->primed = false;
    m->stateComplete = false; m->fieldsStand = false;   // (the change-driven path: the intermediate / the maps stand for no frame the next run sees)
    m->cleanGen++;   // the copy is stale: whatever it holds is no frame a reach was formed from
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
    if (!g_primeEvent) g_primeEvent = OpenHostEvent(DWM_HOOK_FALD_PRIME_EVENT);
    return g_primeEvent && SetEvent(g_primeEvent);
}

static void RequestPrime() { HookRequestFullRecompose(); }

bool FaldUpdateClean(FaldMonitor* m, ID3D11Texture2D* backBuffer, const RECT* rects, int numRects) {
    if (!m || !m->valid || !m->cleanTex || !backBuffer || !g_ctx) return false;
    m->offPresents = 0;
    const LONG w = (LONG)m->width, h = (LONG)m->height;
    const LONG T = (LONG)HOOK_FALD_COVER_TILE;
    m->rects.clear();
    m->planPartial = false;
    for (int i = 0; i < numRects; i++) {
        const LONG l = rects[i].left > 0 ? rects[i].left : 0;
        const LONG t = rects[i].top > 0 ? rects[i].top : 0;
        const LONG r = rects[i].right < w ? rects[i].right : w;
        const LONG b = rects[i].bottom < h ? rects[i].bottom : h;
        if (r <= l || b <= t) continue;
        { RECT c = { l, t, r, b }; m->rects.push_back(c); }   // (the change-driven path: the zones + the pixel pass over them)
        D3D11_BOX box = { (UINT)l, (UINT)t, 0, (UINT)r, (UINT)b, 1 };
        g_ctx->CopySubresourceRegion(m->cleanTex, 0, (UINT)l, (UINT)t, 0, backBuffer, 0, &box);
        m->cleanGen++;   // the copy changed (FaldRun's reach reuse: a new intermediate)
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
    // the zones this present's rects touch (shared/fald_change.h; a long list = the full path)
    {
        const FaldPanelParams& p = m->params;
        std::vector<FaldChangeRect> cr;
        cr.reserve(m->rects.size());
        for (const RECT& rc : m->rects) { FaldChangeRect x = { (int)rc.left, (int)rc.top, (int)rc.right, (int)rc.bottom }; cr.push_back(x); }
        m->touchedOk = FaldTouchedZones(cr.data(), (int)cr.size(), p.originX, p.originY, p.latW, p.latH, p.cols, p.rows,
                                        FaldChangeMaxZones(p.cols * p.rows), m->touched);
    }
    if (!m->primed) {
        FaldTemporalIdle(m);   // the layer does not run this present (src FaldLayerIdle)
        FaldFadeReset(m->fade);   // ... and the fade stack's T state with it
        FaldCore2Invalidate(m->core2);   // ... and the core's zone IIR
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
    FaldFadeReset(e->fade);
    FaldCore2Invalidate(e->core2);
    if (e->valid && ++e->offPresents > HOOK_FALD_RELEASE_AFTER) {
        ReleaseMonitor(e);   // valid = false, failed stays false: rebuilt on demand
        e->offPresents = 0;
        LogF("FALD: pos(%d,%d) %s layer off for a while - GPU memory released", e->left, e->top, e->isHdr ? "HDR" : "SDR(ACM)");
    }
}

// Stale marks kept per entry (g_monitors index): from FaldMarkAllStale (always), and from FaldMarkStale while another
// present thread held g_panelsLock. Applied only by a present of the entry's OWN position — FaldAcquire / FaldMarkStale,
// on the thread that uses the entry — so no thread ever changes (or, after 600 marks, releases) another monitor's entry
// while that monitor's thread is using it outside the lock (FaldUpdateClean / FaldRun).
// Each mark claims the entry first: one a present is using (a twin context of the same position on another thread,
// or a provisional 25H2 context resolved onto the other twin's position) keeps its mark pending for a later present.
static void MarkEntryStaleClaimed(int i, FaldMonitor* e) {   // g_panelsLock held
    EntryClaim claim(i);
    if (!claim.owned()) { g_stalePending[i].store(true); return; }
    MarkEntryStale(e);
}

static void ApplyPendingStale(int left, int top) {   // g_panelsLock held
    for (int i = 0, n = MonitorCount(); i < n; i++) {
        FaldMonitor* e = MonitorAt(i);
        if (!e || e->left != left || e->top != top) continue;
        if (g_stalePending[i].exchange(false)) MarkEntryStaleClaimed(i, e);
    }
}

void FaldMarkStale(int left, int top) {
    FaldPanelLockGuard lock(g_panelsLock);
    if (lock.owned()) ApplyPendingStale(left, top);
    for (int i = 0, n = MonitorCount(); i < n; i++) {
        FaldMonitor* e = MonitorAt(i);
        if (!e || e->left != left || e->top != top) continue;
        if (lock.owned()) MarkEntryStaleClaimed(i, e);
        else g_stalePending[i].store(true);   // never wait in the present: applied before the entry is used again
    }
}

// A present whose monitor could not be resolved (the conservative fallback): every entry's copy may have missed its
// rects. Only MARKED here — this thread is not the entries' own, and an entry is used lock-free by its own thread —
// and applied by each position's own next present (ApplyPendingStale). An entry whose monitor never presents again
// keeps its GPU memory from these marks (as an entry of a resolved context that stops presenting always did).
void FaldMarkAllStale() {
    for (int i = 0, n = MonitorCount(); i < n; i++)
        if (MonitorAt(i)) g_stalePending[i].store(true);
}

void FaldSetLiveSettings(FaldMonitor* m, unsigned int debugMode, int pedMode, bool star,
                         const DwmHookFaldTuning* tuning, bool fade, unsigned int monW, unsigned int monH, bool fadeForce,
                         bool coreForce, unsigned int coreMode) {
    if (!m) return;
    m->debugMode = debugMode;
    m->exemptMonW = monW;
    m->exemptMonH = monH;
    m->pedMode = pedMode ? 1u : 0u;
    m->starWanted = star;
    m->fadeWanted = fade;
    m->fadeForce = fadeForce;
    m->coreForce = coreForce;
    m->coreModeWanted = coreMode <= FALD_CORE_MODE_MAX ? coreMode : 0u;   // the always-on core's switch (0 = C)
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
// boostOn = false: the flat-lattice normalisation pass (a boost-free conv whatever the file says). view >= 0 replaces
// the debug view (word 24): the exempt draws (DrawExempt) write 0 there.
static void FillCB(FaldMonitor* m, uint32_t roundIdx, uint32_t blurDir = 0, bool boostOn = true, int view = -1) {
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
    u[24] = view < 0 ? m->debugMode : (uint32_t)view; u[25] = p.originX; u[26] = p.originY; u[27] = blurDir;
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
    u[75] = m->fadeOn ? FALD_FADE_FLAG_FLANK : 0u;                                  // fadeFlags: the fade stack (bit 1 = L, reserved)
    // the zone lattice's extent (words 76/77; cols * cellW / rows * cellH for an integer pitch): the zones' own spans
    u[76] = p.latW; u[77] = p.latH; u[78] = 0u; u[79] = m->runMode;                 // (docs/fald-fractional-pitch.md; word 79:
                                                                                    // the run's path, fald_change.h FaldRunModeWord)
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
        nullptr                                        // t19: star pass S1 only (RunStar)
    };
    // The always-on core (shared/fald_core2.h): b3, its zone field (t41) and the deficit of the ROUND the consumer reads —
    // CoreGain in the compute passes is the round-1 statistic's, which reads round 0's (t42 / t48); the pixel pass reads
    // round 1's. Off = b3 unbound (reads 0: CoreGain = identity). src/fald.cpp has the same.
    const unsigned int coreRound = compute ? 0u : 1u;
    ID3D11Buffer* coreCB = m->core2On ? FaldCore2CB(m->core2) : nullptr;
    ID3D11ShaderResourceView* coreZF[2] = { m->core2On ? FaldCore2ZoneSRV(m->core2) : nullptr,
                                            m->core2On ? FaldCore2FieldSRV(m->core2, coreRound) : nullptr };
    ID3D11ShaderResourceView* coreND = m->core2On ? FaldCore2NodeDSRV(m->core2, coreRound) : nullptr;
    if (compute) {
        g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
        g_ctx->CSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_ctx->CSSetShaderResources(38, 1, &m->zoneListSRV);   // t38: the touched-zone list (the partial path's sweeps)
        g_ctx->CSSetSamplers(0, 1, &g_faldSampler);
        g_ctx->CSSetConstantBuffers(3, 1, &coreCB);
        g_ctx->CSSetShaderResources(41, 2, coreZF);
        g_ctx->CSSetShaderResources(48, 1, &coreND);
    } else {
        g_ctx->PSSetConstantBuffers(0, 1, &m->cb);
        g_ctx->PSSetShaderResources(0, FALD_SRV_SLOTS, srvs);
        g_ctx->PSSetSamplers(0, 1, &g_faldSampler);
        g_ctx->PSSetConstantBuffers(3, 1, &coreCB);
        g_ctx->PSSetShaderResources(41, 2, coreZF);
        g_ctx->PSSetShaderResources(48, 1, &coreND);
    }
}

static void UnbindCompute() {
    ID3D11ShaderResourceView* nullSrv[FALD_SRV_SLOTS] = {};
    ID3D11UnorderedAccessView* nullUav[FALD_UAV_SLOTS] = {};
    g_ctx->CSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_ctx->CSSetShaderResources(40, FALD_CORE_SRV_SLOTS, nullSrv);   // the core's t40..t52 (BindCommon's t41 / t42 / t48, its kernels')
    g_ctx->CSSetUnorderedAccessViews(0, FALD_UAV_SLOTS, nullUav, nullptr);
    ID3D11Buffer* nullCB = nullptr;
    g_ctx->CSSetConstantBuffers(3, 1, &nullCB);
    g_ctx->CSSetShader(nullptr, nullptr, 0);
}

// How a statistic sweep is dispatched (the change-driven path, shared/fald_change.h; src/fald.cpp has the same): the
// full lattice by the host, the touched zones by the host, or either by the decide pass's indirect arguments.
struct StatDispatch { bool partial; bool indirect; unsigned int slot, combine; };
static const StatDispatch kStatFull = { false, false, 0, 0 };
static const StatDispatch kStatPartial = { true, false, 0, 0 };

static void RunStat(FaldMonitor* m, uint32_t roundIdx, StatDispatch d = kStatFull) {
    const FaldPanelParams& p = m->params;
    if (d.partial && !d.indirect && m->touched.empty()) return;   // no touched zone: nothing to sweep (no zero-group dispatch)
    m->runMode = (m->runMode & ~0xFFu) | (d.partial ? 1u : 0u);   // the sweep's zones: the group's or the list's
    FillCB(m, roundIdx);
    g_ctx->CSSetShader(g_statCS, nullptr, 0);
    BindCommon(m, true);
    if (roundIdx == 1) {                                          // round 0's fields and smoothed (gain, ceiling B_est) (C15)
        ID3D11ShaderResourceView* fields[2] = { m->bTrue0SRV, m->bEst0SRV };
        g_ctx->CSSetShaderResources(5, 2, fields);
        g_ctx->CSSetShaderResources(FALD_FIELDS_SLOT, 1, &m->fields0SRV);   // t39: the same packed (SampleFields)
        ID3D11ShaderResourceView* gain0 = m->gainFused ? m->gain0SRV : m->gainBSRV;   // (the three-pass path leaves it in gainB)
        g_ctx->CSSetShaderResources(9, 1, &gain0);
        if (m->fadeOn) {                                          // FadePost on round 0's request: fade, F's opening, reach 0, s - f_q
            ID3D11ShaderResourceView* fv[FALD_FADE_POST_VIEWS];
            FaldFadePostViews(m->fade, 0, fv);
            g_ctx->CSSetShaderResources(FALD_FADE_POST_SLOT, FALD_FADE_POST_VIEWS, fv);
        }
    }
    ID3D11UnorderedAccessView* uavs[8] = { roundIdx ? m->driveUAV : m->drive0UAV, m->activeUAV[roundIdx & 1u],   // u1: null without a boost LUT
                                           m->zonePartUAV,                                // (the shader then never writes it)
                                           m->boostCountUAV, nullptr,                     // u3: the boost's counters (LUT only); u2: partials
                                           m->changeFlagsUAV, nullptr, nullptr };         // u5: the partial path's flags
    if (roundIdx == 0 && m->core2On)                 // the always-on core (fald_core2.h), round 0: the zones' P, M, lnG (u4) and,
        FaldCore2StatViews(m->core2, &uavs[4], &uavs[6], &uavs[7]);   // round 3, the node peaks (u6; slice partials u7) — compared on the partial path
    g_ctx->CSSetUnorderedAccessViews(0, 8, uavs, nullptr);
    if (d.indirect) g_ctx->DispatchIndirect(m->argsBuf, d.slot);
    else if (d.partial) g_ctx->Dispatch((UINT)m->touched.size(), 1, m->zoneSlices);
    else g_ctx->Dispatch(p.cols, p.rows, m->zoneSlices);
    if (m->zoneSlices > 1) {                                                             // fold the slices, finish the zones
        g_ctx->CSSetShader(g_statCombineCS, nullptr, 0);
        if (d.indirect) g_ctx->DispatchIndirect(m->argsBuf, d.combine);
        else if (d.partial) g_ctx->Dispatch((UINT)m->touched.size(), 1, 1);
        else g_ctx->Dispatch(p.cols, p.rows, 1);
    }
    UnbindCompute();
}

// The decide pass (fald_shader.h g_faldDecideSource), mirror of src/fald.cpp RunDecide: after S0 (which = 2), after round
// 0's sweep (0) and after round 1's (1). It finishes the round's boost from the persistent count on every path; on the
// partial path it also writes the indirect arguments of what follows from the change flags.
// partial: the run's path (the CB's runMode bit: the sweeps set it per dispatch — a full indirect sweep clears it — and
// the decide pass writes the arguments only on the partial path).
static void RunDecide(FaldMonitor* m, uint32_t which, bool partial) {
    m->runMode = (m->runMode & ~0xFFu) | (partial ? 1u : 0u);
    FillCB(m, which);
    g_ctx->CSSetShader(g_decideCS, nullptr, 0);
    BindCommon(m, true);
    ID3D11UnorderedAccessView* uavs[7] = { nullptr, nullptr, nullptr, m->boostCountUAV, m->boostUAV[which & 1u], m->changeFlagsUAV, m->argsUAV };
    g_ctx->CSSetUnorderedAccessViews(0, 7, uavs, nullptr);
    g_ctx->Dispatch(1, 1, 1);
    UnbindCompute();
}

// boost: this round's 2x1 boost texture (B_true only; null = none — no LUT in the file, or the
// flat-lattice pass, where the CB's boostN is 0 and the shader never reads t14).
// trueDrive / estDrive: the drive maps the real-spread and the estimate kernels see (both the instantaneous drive
// unless LED lag routes a filtered / clock map: FaldTemporalRun::trueMap / estMap).
static void RunConv(FaldMonitor* m, ID3D11ShaderResourceView* trueDrive, ID3D11ShaderResourceView* estDrive,
                    ID3D11ShaderResourceView* boost, int roundIdx = -1, bool indirect = false) {
    const FaldPanelParams& p = m->params;
    g_ctx->CSSetShader(g_convCS[m->convVariant], nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(4, 1, &trueDrive);
    g_ctx->CSSetShaderResources(10, 1, &estDrive);
    g_ctx->CSSetShaderResources(14, 1, &boost);
    ID3D11ShaderResourceView* k4[2] = { m->kTrue4SRV, m->kEst4SRV };   // the tables four floats wide (t36 / t37)
    g_ctx->CSSetShaderResources(36, 2, k4);
    // round 0 writes its own fields (the round-1 sweep and the change-driven path compare / read them later)
    ID3D11UnorderedAccessView* uavs[3] = { roundIdx == 0 ? m->bTrue0UAV : m->bTrueUAV, roundIdx == 0 ? m->bEst0UAV : m->bEstUAV,
                                           roundIdx == 0 ? m->fields0UAV : m->fieldsUAV };   // u2: the two packed with their flat response
    g_ctx->CSSetUnorderedAccessViews(0, 3, uavs, nullptr);
    // cells x sub-offset groups (fald_shader.h: FALD_CONV_SO sub-offsets per thread); indirect: the decide pass's arguments
    if (indirect) g_ctx->DispatchIndirect(m->argsBuf, roundIdx ? FALD_ARGS_CONV1 : FALD_ARGS_CONV0);
    else g_ctx->Dispatch(FaldConvGroupsX(p.cols, p.rows), FaldConvGroups(p.sub, FaldConvSoOf(m->convVariant)), 1);
    UnbindCompute();
}

// Pass 2b/2c: (gain, the knee's ceiling B_est) on the fine grid, then a separable Gaussian low-pass of both (A -> B -> A; final in gainB).
static void RunGain(FaldMonitor* m, uint32_t roundIdx, bool indirect = false) {
    const FaldPanelParams& p = m->params;
    const UINT gx = (p.cols * p.sub + 15) / 16, gy = (p.rows * p.sub + 15) / 16;
    ID3D11ShaderResourceView* fields[2] = { roundIdx ? m->bTrueSRV : m->bTrue0SRV, roundIdx ? m->bEstSRV : m->bEst0SRV };   // the round's own
    ID3D11ShaderResourceView* packed = roundIdx ? m->fieldsSRV : m->fields0SRV;                                             // t39: the same packed
    if (m->gainFused) {                    // one dispatch: gain + both blurs straight into the round's map (fald_shader.h)
        g_ctx->CSSetShader(g_gainBlurCS, nullptr, 0);
        BindCommon(m, true);
        g_ctx->CSSetShaderResources(5, 2, fields);
        g_ctx->CSSetShaderResources(FALD_FIELDS_SLOT, 1, &packed);
        ID3D11UnorderedAccessView* out = roundIdx ? m->gainBUAV : m->gain0UAV;
        g_ctx->CSSetUnorderedAccessViews(0, 1, &out, nullptr);
        if (indirect) g_ctx->DispatchIndirect(m->argsBuf, roundIdx ? FALD_ARGS_GAIN1 : FALD_ARGS_GAIN0);
        else g_ctx->Dispatch(gx, gy, 1);
        UnbindCompute();
        return;
    }
    g_ctx->CSSetShader(g_gainCS, nullptr, 0);   // (the three-pass path is never indirect: the partial path needs the fused one)
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(5, 2, fields);
    g_ctx->CSSetShaderResources(FALD_FIELDS_SLOT, 1, &packed);
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
    // final smoothed (gain, ceiling B_est) lives in A; copy to B so consumers always read gainB
    g_ctx->CopyResource(m->gainBTex, m->gainATex);
}

// LED lag pass 1b (modes 1 / 2), mirror of src/fald.cpp RunTemporal: filtered drive = state + a * (drive - state) per
// cell, from the map the panel's pipeline is fed (t4: this round's instantaneous drive, or a delay-ring entry) and the
// state committed after the previous frame (t11); with no valid state the drive is copied.
// partial: the run's path — the pass compares its output with the previous run's and flags changeFlags[roundIdx] on the
// partial path, so it fills the CB itself (roundIdx, the runMode bit): it must not inherit the previous pass's (a full
// indirect sweep clears the bit; an empty touched-zone sweep is not dispatched at all). src/fald.cpp has the same.
static void RunTemporal(FaldMonitor* m, ID3D11ShaderResourceView* inDrive, uint32_t roundIdx, bool partial) {
    const FaldPanelParams& p = m->params;
    m->runMode = (m->runMode & ~0xFFu) | (partial ? 1u : 0u);
    FillCB(m, roundIdx);
    g_ctx->CSSetShader(g_temporalCS, nullptr, 0);
    BindCommon(m, true);
    g_ctx->CSSetShaderResources(4, 1, &inDrive);
    g_ctx->CSSetShaderResources(11, 1, &m->driveStateSRV);
    ID3D11UnorderedAccessView* uavs[6] = { roundIdx ? m->driveFiltUAV : m->driveFilt0UAV, nullptr, nullptr, nullptr, nullptr, m->changeFlagsUAV };
    g_ctx->CSSetUnorderedAccessViews(0, 6, uavs, nullptr);
    g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

// LED lag pass 1c (mode 3, valid state only), mirror of src/fald.cpp RunPanelClock: the two parity clocks advance in
// place toward the previous frame's round-1 drives (t4 = clkPrev) by the CB's blend factors; u0 / u1 receive the maps
// the kernels see in both rounds. Binds only what it reads; all four UAV slots are cleared here.
static void RunPanelClock(FaldMonitor* m, bool partial) {
    const FaldPanelParams& p = m->params;
    m->runMode = (m->runMode & ~0xFFu) | (partial ? 1u : 0u);   // (the pass compares on the partial path: its own bit)
    FillCB(m, 0);
    g_ctx->CSSetShader(g_clockCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(4, 1, &m->clkPrevSRV);
    // u0 = the ONE B_true map both rounds' real-spread kernel reads this run (mapSrv's clockMaps): the round-1 filtered
    // slot, as on main and as src/fald.cpp — the dump's fald_drive_filt.f32. Round 0's filtered slot belongs to the
    // modes 1 / 2 temporal pass, which runs per round; the clock pass runs ONCE per run. (2026-10-22: it wrote round 0's
    // slot while round 1's convolution read round 1's — a map nothing writes in mode 3.)
    ID3D11UnorderedAccessView* uavs[6] = { m->driveFiltUAV, m->clkEstUAV, m->clkStateUAV[0], m->clkStateUAV[1], nullptr, m->changeFlagsUAV };
    g_ctx->CSSetUnorderedAccessViews(0, 6, uavs, nullptr);
    g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();   // clears all FALD_UAV_SLOTS
}

// Starfield balancing, mirror of src/fald.cpp RunStar: S0 star statistic of the SOURCE frame -> S1
// tapered protection + zone weights -> S2 target + plan; every later pass samples plan (t15) and plan2
// (t18). A pure function of the source frame in the intermediate, recomputed every run. The passes
// bind only what they read (never BindCommon: that would bind plan / plan2 as SRVs while S1 / S2
// write them).
// S0 over the lattice (full) or the touched zones (partial: compared, a moved zone flags the starfield).
// indirectFull = the whole lattice by the decide pass's arguments (FALD_ARGS_STAR_S0_FULL: the frame changed everywhere).
static void RunStarS0(FaldMonitor* m, bool partial, bool indirectFull = false) {
    const FaldPanelParams& p = m->params;
    if (partial && !indirectFull && m->touched.empty()) return;   // no touched zone: nothing to sweep (no zero-group dispatch)
    m->runMode = (m->runMode & ~0xFFu) | (partial ? 1u : 0u);
    FillCB(m, 0);
    ID3D11ShaderResourceView* in2[2] = { m->interSRV, m->curveSRV };
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetSamplers(0, 1, &g_faldSampler);
    g_ctx->CSSetShaderResources(38, 1, &m->zoneListSRV);
    // S0: per zone stat = (peak, speck-zone flag, sparse, solid) + bg = (ln b, brightest pixel's index, lit sum, a_eff)
    ID3D11UnorderedAccessView* out0[6] = { m->starStatUAV, m->starBgUAV, m->zonePartUAV, nullptr, nullptr, m->changeFlagsUAV };   // u2: zones > one slice
    g_ctx->CSSetShader(g_starStatCS, nullptr, 0);
    g_ctx->CSSetShaderResources(0, 2, in2);
    g_ctx->CSSetUnorderedAccessViews(0, 6, out0, nullptr);
    if (indirectFull) g_ctx->DispatchIndirect(m->argsBuf, FALD_ARGS_STAR_S0_FULL);
    else if (partial) g_ctx->Dispatch((UINT)m->touched.size(), 1, m->zoneSlices);
    else g_ctx->Dispatch(p.cols, p.rows, m->zoneSlices);
    if (m->zoneSlices > 1) {
        g_ctx->CSSetShader(g_starStatCombineCS, nullptr, 0);
        if (indirectFull) g_ctx->DispatchIndirect(m->argsBuf, FALD_ARGS_STAR_S0_FULL_COMBINE);
        else if (partial) g_ctx->Dispatch((UINT)m->touched.size(), 1, 1);
        else g_ctx->Dispatch(p.cols, p.rows, 1);
    }
    UnbindCompute();
}

// S1 + S2, the plan passes: by the host, or by the decide pass's arguments (the partial path: only when S0 moved).
static void RunStarPlan(FaldMonitor* m, bool indirect) {
    const FaldPanelParams& p = m->params;
    FillCB(m, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetSamplers(0, 1, &g_faldSampler);
    // S1: tapered protection field + flank test + zone weights
    ID3D11UnorderedAccessView* out1[2] = { m->starWUAV, m->starPlan2UAV };
    g_ctx->CSSetShader(g_starWeightCS, nullptr, 0);
    g_ctx->CSSetShaderResources(16, 1, &m->starStatSRV);
    g_ctx->CSSetShaderResources(19, 1, &m->starBgSRV);
    g_ctx->CSSetUnorderedAccessViews(0, 2, out1, nullptr);
    if (indirect) g_ctx->DispatchIndirect(m->argsBuf, FALD_ARGS_STAR_S1);
    else g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
    // S2: (w0_field, ln target, ln lift, ln peak)
    ID3D11ShaderResourceView* in3[2] = { m->starStatSRV, m->starWSRV };
    g_ctx->CSSetShader(g_starPlanCS, nullptr, 0);
    g_ctx->CSSetConstantBuffers(0, 1, &m->cb);
    g_ctx->CSSetShaderResources(16, 2, in3);
    g_ctx->CSSetUnorderedAccessViews(0, 1, &m->starPlanUAV, nullptr);
    if (indirect) g_ctx->DispatchIndirect(m->argsBuf, FALD_ARGS_STAR_S2);
    else g_ctx->Dispatch((p.cols + 15) / 16, (p.rows + 15) / 16, 1);
    UnbindCompute();
}

static void RunStar(FaldMonitor* m) { RunStarS0(m, false); RunStarPlan(m, false); }

// The fade stack (shared/fald_fade.h — the same module the overlay runs), mirror of src/fald.cpp RunFadeFrame /
// RunFadeReach: the common views its frame / knee passes read (BindCommon's t0..t19; a round's reach also t5 / t6 = its
// fields, t9 = its smoothed gain).
static FaldFadeCommon FadeCommon(FaldMonitor* m, bool roundFields, uint32_t round = 1) {
    FaldFadeCommon c;
    c.faldCB = m->cb;
    c.sampler = g_faldSampler;
    ID3D11ShaderResourceView* v[20] = { m->interSRV, m->curveSRV, m->kTrueSRV, m->kEstSRV, nullptr, nullptr, nullptr,
                                        m->flatTrueSRV, m->flatEstSRV, nullptr, nullptr, nullptr, m->boostLutSRV, nullptr, nullptr,
                                        m->starOn ? m->starPlanSRV : nullptr, nullptr, nullptr,
                                        m->starOn ? m->starPlan2SRV : nullptr, nullptr };
    for (int i = 0; i < 20; i++) c.srv[i] = v[i];
    if (roundFields) {                     // the round's own fields and smoothed gain
        c.srv[5] = round ? m->bTrueSRV : m->bTrue0SRV; c.srv[6] = round ? m->bEstSRV : m->bEst0SRV;
        c.fields = round ? m->fieldsSRV : m->fields0SRV;                          // t39: the same packed (SampleFields)
        c.srv[9] = (round || !m->gainFused) ? m->gainBSRV : m->gain0SRV;
    }
    return c;
}

// The settings the cached frame part depends on (the frame the layer works on): the starfield and its parameters.
static unsigned long long FadeInputKey(const FaldMonitor* m) {
    unsigned long long h = 1469598103934665603ull;
    auto mix = [&h](const void* p, size_t n) { const unsigned char* b = (const unsigned char*)p; for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; } };
    const unsigned int on = m->starOn ? 1u : 0u;
    mix(&on, sizeof(on));
    if (m->starOn) mix(&m->tuning, sizeof(m->tuning));
    return h;
}

// Once per CONTENT frame: DWM composes per present, so a present without new content (the host's settle kick, a
// recomposition of the same frame) reuses fade_T / F / the reach step of the last content frame and steps nothing
// (r11/T/T_hookclock_note.md level 1); with new content the GPU's advance test decides (level 2: an identical 1/8-res map
// steps nothing). A stale frame part (built / reset / a starfield change) is re-formed without a T step.
static void RunFadeFrame(FaldMonitor* m, bool newContent) {
    const unsigned long long key = FadeInputKey(m);
    const bool stale = !FaldFadeFrameValid(m->fade) || key != m->fadeKey;
    if (!newContent && !stale) return;
    m->fadeKey = key;
    FillCB(m, 0);
    FaldFadeRunFrame(m->fade, g_ctx, FadeCommon(m, false), newContent);
}

// src/fald.cpp ReachKey: the identity of what both rounds' reach are formed from (the input frame + the run-time settings
// the fields depend on besides the panel file); 0 = never reused (an unknown frame, or LED lag).
static unsigned long long ReachKey(const FaldMonitor* m, unsigned long long frameKey, bool temporal, bool panel) {
    if (frameKey == 0 || temporal || panel) return 0;
    unsigned long long h = FadeInputKey(m);
    auto mix = [&h](unsigned long long v) { for (int i = 0; i < 8; i++) { h ^= (v >> (8 * i)) & 0xFFu; h *= 1099511628211ull; } };
    mix(frameKey); mix(m->pedMode); mix(m->debugMode);
    return h ? h : 1ull;
}

static void RunFadeReach(FaldMonitor* m, uint32_t round, unsigned long long key) {
    FillCB(m, round);
    FaldFadeRunReach(m->fade, g_ctx, FadeCommon(m, true, round), round, key);
}

// Flat-lattice response: run the convolution once on a drive map of ones and keep the two fields.
// Must run after the fine textures exist; the flat textures are bound as SRVs t7/t8 from then on
// (they are null during this call: the conv pass's packed output (u2) gets zeros for them here, and every conv run of a
// round rewrites its packed texture before the round's readers run).
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
    DumpTexture(m->gainBTex, dir + L"fald_gain_fine.rg32f", p.cols * p.sub, p.rows * p.sub, 8);   // (gain, ceiling B_est) pairs, C15
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
    if (m->fadeOn) {   // the fade stack (same files as the overlay's dump): full res float32, s at quarter res
        FaldFadeDebugMaterialize(m->fade, g_ctx);   // fade_T and F as full-res maps (FadePost forms them per pixel)
        const FaldFadeDebug d = FaldFadeDebugViews(m->fade);
        DumpTexture(d.fade, dir + L"fald_fade.f32", d.width, d.height, 4);
        DumpTexture(d.fadeT, dir + L"fald_fade_t.f32", d.width, d.height, 4);
        DumpTexture(d.floorF, dir + L"fald_fade_floor.f32", d.width, d.height, 4);
        DumpTexture(d.reach[0], dir + L"fald_fade_reach0.f32", d.width, d.height, 4);
        DumpTexture(d.reach[1], dir + L"fald_fade_reach1.f32", d.width, d.height, 4);
        DumpTexture(d.s, dir + L"fald_fade_s.f32", d.wq, d.hq, 4);
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
    ID3D11Buffer* nullCBs[3] = {};                                // b0 FaldCB, b1 / b2 the fade passes' CBs
    g_ctx->CSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_ctx->CSSetUnorderedAccessViews(0, FALD_UAV_SLOTS, nullUav, nullptr);
    g_ctx->CSSetSamplers(0, 1, nullSamp);
    g_ctx->CSSetConstantBuffers(0, 3, nullCBs);
    g_ctx->CSSetConstantBuffers(3, 1, &nullCB);                   // b3 / t40..t52: the always-on core (shared/fald_core2.h)
    g_ctx->CSSetShaderResources(40, FALD_CORE_SRV_SLOTS, nullSrv);
    g_ctx->CSSetShader(nullptr, nullptr, 0);
    g_ctx->PSSetShaderResources(0, FALD_SRV_SLOTS, nullSrv);
    g_ctx->PSSetShaderResources(40, FALD_CORE_SRV_SLOTS, nullSrv);
    g_ctx->PSSetSamplers(0, 1, nullSamp);
    g_ctx->PSSetConstantBuffers(0, 1, &nullCB);
    g_ctx->PSSetConstantBuffers(3, 1, &nullCB);
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
    FaldCoreTimingSample smp[FaldCoreTiming::kSlots];
    const unsigned int n = FaldCoreTimingCollect(&g_coreTiming, g_ctx, smp, FaldCoreTiming::kSlots);
    for (unsigned int i = 0; i < n; i++) {
        FaldMonitor* o = (FaldMonitor*)smp[i].owner;
        if (!o) continue;
        o->gpuSumUs += smp[i].totalUs;
        o->gpuCoreSumUs += smp[i].coreUs;
        o->gpuStarSumUs += smp[i].starUs;
        if (smp[i].totalUs > o->gpuMaxUs) o->gpuMaxUs = smp[i].totalUs;
        o->gpuSamples++;
        o->coreGov.OnSample(smp[i].coreUs);   // the rate-cap governor: the core's time of a finished full run
    }
}

// Returns the slot started for this run, or -1 (no queries, or the slot is still in flight).
static int GpuTimingBegin(FaldMonitor* m) { return FaldCoreTimingBegin(&g_coreTiming, g_ctx, m); }
static void GpuTimingStamp(int slot, int k) { FaldCoreTimingStamp(&g_coreTiming, g_ctx, slot, k); }
static void GpuTimingEnd(int slot, FaldMonitor*) { FaldCoreTimingEnd(&g_coreTiming, g_ctx, slot); }

// A debug view is on: the pixel pass again, the view off, inside DesktopLUT's own windows on this monitor (tuning.exempt,
// monitor pixels; shared/fald_exempt.h), so the interface stays readable. The main draw's bindings are still in place;
// only the CB (word 24 = 0), the rasterizer state and the scissor change, and DWM's own rasterizer state and scissor
// rects are put back exactly as they were. No pointer read, no wait: D3D calls on the context the main draw used.
static void DrawExempt(FaldMonitor* m) {
    if (!g_exemptRS) return;
    FaldExemptRect wire[FALD_EXEMPT_WIRE_RECTS], fr[FALD_EXEMPT_FRAME_RECTS];
    const int n = FaldExemptUnpack(m->tuning.exempt, wire);
    if (n == 0) return;
    const int k = FaldExemptToFrame(wire, n, (int)m->exemptMonW, (int)m->exemptMonH, (int)m->width, (int)m->height, fr);
    if (k == 0) return;
    FillCB(m, 1, 0, true, 0);
    ID3D11RasterizerState* prevRS = nullptr;
    g_ctx->RSGetState(&prevRS);
    D3D11_RECT prevScissor[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};
    UINT prevCount = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    g_ctx->RSGetScissorRects(&prevCount, prevScissor);
    g_ctx->RSSetState(g_exemptRS);
    for (int i = 0; i < k; i++) {
        const D3D11_RECT rc = { fr[i].x0, fr[i].y0, fr[i].x1, fr[i].y1 };
        g_ctx->RSSetScissorRects(1, &rc);
        g_ctx->Draw(3, 0);
    }
    g_ctx->RSSetState(prevRS);
    if (prevRS) prevRS->Release();
    g_ctx->RSSetScissorRects(prevCount, prevCount ? prevScissor : nullptr);
}

// The settings a run's textures depend on besides the panel file and the frame (the change-driven path's settingsSame).
static unsigned long long SettingsKey(const FaldMonitor* m) {
    unsigned long long h = 1469598103934665603ull;
    auto mix = [&h](const void* p, size_t n) { const unsigned char* b = (const unsigned char*)p; for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; } };
    const uint32_t words[7] = { m->debugMode, m->pedMode, m->starOn ? 1u : 0u, m->fadeOn ? 1u : 0u, m->gainFused ? 1u : 0u, m->convVariant,
                                m->core2On ? m->core2Flags : 0u };   // the always-on core's mode (its fields are part of the state)
    mix(words, sizeof(words));
    mix(&m->tuning, sizeof(m->tuning));
    mix(&m->tempSettings, sizeof(m->tempSettings));
    return h;
}

// The pixel pass: the intermediate + the standing fields -> the back buffer (the fullscreen triangle, no vertex buffer).
// filt = debug view 7's "filtered" map (t10); partial = the change-driven path's draws (the decide pass's arguments and
// the rects) instead of the whole frame.
static void PixelPass(FaldMonitor* m, ID3D11RenderTargetView* dstRTV, ID3D11ShaderResourceView* filt, bool partial) {
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
    g_ctx->PSSetShaderResources(FALD_FIELDS_SLOT, 1, &m->fieldsSRV);   // t39: the same packed (SampleFields)
    g_ctx->PSSetShaderResources(9, 1, &m->gainBSRV);
    // t4 / t10 = debug view 7: instantaneous vs filtered (mode 3: vs the clocks' mean LED state of this frame)
    g_ctx->PSSetShaderResources(4, 1, &m->driveSRV);
    g_ctx->PSSetShaderResources(10, 1, &filt);
    g_ctx->PSSetShaderResources(13, 1, &m->activeSRV[1]);   // debug view 8 (null without a boost LUT)
    if (m->fadeOn) {                                        // FadePost: fade, F's opening, reach of round 1, s - f_q
        ID3D11ShaderResourceView* fv[FALD_FADE_POST_VIEWS];
        FaldFadePostViews(m->fade, 1, fv);
        g_ctx->PSSetShaderResources(FALD_FADE_POST_SLOT, FALD_FADE_POST_VIEWS, fv);
    }
    if (!partial) g_ctx->Draw(3, 0);
    else {
        // the whole frame only if anything moved (the decide pass's draw arguments), and always the rects DWM re-composed
        // (the back buffer holds the previous output everywhere else); scissored draws of the same triangle
        g_ctx->DrawInstancedIndirect(m->argsBuf, FALD_ARGS_PIXEL);
        if (g_exemptRS && !m->rects.empty()) {
            ID3D11RasterizerState* prevRS = nullptr;
            g_ctx->RSGetState(&prevRS);
            D3D11_RECT prevScissor[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};
            UINT prevCount = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
            g_ctx->RSGetScissorRects(&prevCount, prevScissor);
            g_ctx->RSSetState(g_exemptRS);
            for (const RECT& rc : m->rects) {
                const D3D11_RECT sc = { rc.left, rc.top, rc.right, rc.bottom };
                g_ctx->RSSetScissorRects(1, &sc);
                g_ctx->Draw(3, 0);
            }
            g_ctx->RSSetState(prevRS);
            if (prevRS) prevRS->Release();
            g_ctx->RSSetScissorRects(prevCount, prevCount ? prevScissor : nullptr);
        }
    }
    if (m->debugMode != 0) DrawExempt(m);   // a view: DesktopLUT's windows keep the corrected picture
}

// The governors' status for the host (its FALD panel, Events, state.get): on a state change, and every ~240 runs (the
// numbers). A busy or missing section leaves it for a later present. The core governor's state changes are logged.
static void WriteGovernorStatus(FaldMonitor* m) {
    {
        const FaldFadeGovernorStatus gs = m->fadeGov.Status();
        const bool due = gs.generation != m->fadeGovWrittenGen || (m->fadeWanted && ++m->fadeGovWriteRuns >= 240u);
        if (due && WriteFadeStatus(m->left, m->top, m->isHdr, gs)) { m->fadeGovWrittenGen = gs.generation; m->fadeGovWriteRuns = 0; }
    }
    {
        const FaldCoreGovernorStatus cs = m->coreGov.Status();
        if (cs.generation != m->coreGovLoggedGen) {
            m->coreGovLoggedGen = cs.generation;
            LogF("FALD: pos(%d,%d) %s core governor %s (p90 %.2f ms, budget %.2f ms, a full run every %.0f ms)", m->left, m->top,
                 m->isHdr ? "HDR" : "SDR(ACM)", FaldCoreGovStateName(cs.state), cs.p90Us / 1000.0, cs.budgetUs / 1000.0, cs.spacingMs);
        }
        const bool due = cs.generation != m->coreGovWrittenGen || ++m->coreGovWriteRuns >= 240u;
        if (due && WriteCoreStatus(m->left, m->top, m->isHdr, cs)) { m->coreGovWrittenGen = cs.generation; m->coreGovWriteRuns = 0; }
    }
}

bool FaldPlanPartial(FaldMonitor* m, unsigned long long lutPassKey) {
    if (!m || !m->valid || !m->primed) return false;
    ResolveFeatures(m);                      // the starfield / the fade of THIS present (FaldRun resolves again: idempotent)
    const FaldPanelParams& p = m->params;
    m->planSettingsKey = SettingsKey(m);
    FaldChangeInputs in = {};
    in.stateComplete = m->stateComplete; in.settingsSame = m->planSettingsKey == m->lastSettingsKey;
    in.frameKnown = m->touchedOk && lutPassKey != 0 && lutPassKey == m->lastLutPassKey;
    in.newContent = true; in.gainFused = m->gainFused; in.temporalMode = m->tempSettings.mode; in.fadeOn = m->fadeOn;
    in.coreStateful = m->core2On && FaldCore2Stateful(m->core2Flags);   // KC2c's zone IIRs: the full path (KC3 is stateless)
    in.touchedZones = (unsigned int)m->touched.size(); in.totalZones = p.cols * p.rows;
    m->planPartial = g_faldPartialOverride != 0 && FaldChangeDecide(in, FaldChangeMaxZones(in.totalZones)) == FALD_CHANGE_PARTIAL;
    return m->planPartial;
}

bool FaldRun(FaldMonitor* m, ID3D11RenderTargetView* dstRTV, bool newContent, unsigned long long lutPassKey,
             ID3D11Buffer* frameChanged, unsigned int frameChangedOffset) {
    if (!m || !m->valid || !m->interSRV || !dstRTV) return false;

    LARGE_INTEGER t0, t1, freq;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);

    const std::wstring dumpDir = g_dumpDir;
    g_dumpDir.clear();                       // one frame only, whatever happens below

    // The fade governor (shared/fald_fade_governor.h): the GPU time of finished content frames (timestamp queries read
    // without a flush or a wait), this run's content-frame clock — before ResolveFeatures, which acts on its verdict.
    if (m->fade) {
        double us[4];
        const unsigned int n = FaldFadeCollectTiming(m->fade, g_ctx, us, 4);
        for (unsigned int i = 0; i < n; i++) m->fadeGov.OnSample(us[i]);
    }
    if (newContent && freq.QuadPart > 0) m->fadeGov.OnContentFrame((double)t0.QuadPart * 1000.0 / (double)freq.QuadPart);

    ResolveFeatures(m);                      // starfield on or off for THIS frame (and the fade, by its governor)
    // The intermediate's identity: the clean copy's generation + what the LUT / tone map pass made of it (0 = unknown)
    unsigned long long frameKey = 0;
    if (lutPassKey != 0) {
        frameKey = 1469598103934665603ull;
        for (unsigned long long v : { m->cleanGen, lutPassKey })
            for (int i = 0; i < 8; i++) { frameKey ^= (v >> (8 * i)) & 0xFFu; frameKey *= 1099511628211ull; }
        if (frameKey == 0) frameKey = 1;
    }

    // GPU timing: collect finished slots (no flush, no wait; the rate-cap governor gets each full run's core time).
    GpuTimingCollect(m);
    const double nowMs = freq.QuadPart > 0 ? (double)t0.QuadPart * 1000.0 / (double)freq.QuadPart : 0.0;
    if (newContent) m->coreGov.OnContentFrame(nowMs);
    // the drive maps the kernels and the LED-lag pass of a round read: round 0 has its own instantaneous / filtered maps
    // (modes 1 / 2 run the temporal pass per round); the panel clock's B_true map — mode 3, ONE pass per run, read by both
    // rounds (clockMaps) — is the round-1 filtered slot, as on main (RunPanelClock; src/fald.cpp has the same)
    auto mapSrv = [m](FaldDriveMap k, uint32_t round, bool clockMaps = false) {
        return k == FALD_MAP_FILTERED ? ((round || clockMaps) ? m->driveFiltSRV : m->driveFilt0SRV)
             : (k == FALD_MAP_CLOCK_EST ? m->clkEstSRV : (round ? m->driveSRV : m->drive0SRV));
    };

    // The path of this present (FaldPlanPartial, decided before the LUT pass drew the rects or the whole frame into the
    // intermediate; shared/fald_change.h). The settings the plan was made with must still hold (ResolveFeatures above
    // is idempotent between the two); otherwise the full path — the intermediate is whole either way.
    bool partial = m->planPartial && SettingsKey(m) == m->planSettingsKey && m->touchedOk;   // + the routing rule below
    m->planPartial = false;
    const unsigned long long settingsKey = SettingsKey(m);

    // The rate cap (shared/fald_core_governor.h): a full run the governor holds back keeps the standing fields and runs only
    // the pixel pass on the new frame (the intermediate is whole: the plan was not partial) — before the LED-lag
    // bookkeeping, which a held run does not advance (the layer's time stands still between full runs).
    bool held = false;
    if (!partial && m->fieldsStand && settingsKey == m->lastSettingsKey)
        held = g_faldCoreHoldOverride >= 0 ? g_faldCoreHoldOverride == 1 : !m->coreGov.FullAllowed(nowMs);
    if (held) {
        m->runMode = FaldRunModeWord(false, FaldConvSoOf(m->convVariant), 0);
        g_ctx->OMSetRenderTargets(0, nullptr, nullptr);
        // debug view 7's "filtered" map as the last full run bound it (mode 3 without clock textures runs as off)
        const FaldDriveMap filtMap = m->tempSettings.mode == FALD_TEMPORAL_PANEL ? (m->clkEstTex ? FALD_MAP_CLOCK_EST : FALD_MAP_DRIVE)
                                   : (m->tempSettings.mode != FALD_TEMPORAL_OFF ? FALD_MAP_FILTERED : FALD_MAP_DRIVE);
        PixelPass(m, dstRTV, mapSrv(filtMap, 1), false);
        m->framesRun++; m->heldRuns++;
        m->lastPartial = false;
        m->stateComplete = false;             // the standing textures are not this frame's: the next present that is not held plans full
        FaldUnbindAll();
        WriteGovernorStatus(m);
        QueryPerformanceCounter(&t1);
        if (freq.QuadPart > 0) m->lastRunUs = (double)(t1.QuadPart - t0.QuadPart) * 1e6 / (double)freq.QuadPart;
        return true;
    }
    const int ts = GpuTimingBegin(m);

    // LED lag (temporal drive state): the bookkeeping is shared/fald_temporal.cpp, the SAME code the overlay runs
    // (FaldTemporalBeginRun / EndRun); only the D3D side is here. The panel clock's textures exist only in mode 3.
    bool clockOk = false;
    if (m->tempSettings.mode == FALD_TEMPORAL_PANEL) clockOk = EnsureClock(m);
    else if (m->clkStateTex[0] || m->clkStateTex[1] || m->clkPrevTex || m->clkEstTex) ReleaseClock(m);
    LARGE_INTEGER qpcNow;
    QueryPerformanceCounter(&qpcNow);
    const FaldTemporalRun trun = FaldTemporalBeginRun(m, m->tempSettings, clockOk, qpcNow.QuadPart, freq.QuadPart, m->refreshMs);
    auto inDriveOf = [m, &trun](uint32_t round) { return trun.delayedSlot >= 0 ? m->delaySRV[trun.delayedSlot] : (round ? m->driveSRV : m->drive0SRV); };
    // The routing rule (src/fald.cpp has the same): the kernels must read the maps the previous run's fields were made
    // from. A run that (re-)seeds the panel clock reads the instantaneous drives where the clock runs before and after it
    // read the clock's maps; the GPU compares each map with its own previous value, not one map with another, so a
    // changed routing takes the full path (the intermediate is whole either way: the rects were drawn into the previous
    // frame through the same pass).
    if (partial && (trun.trueMap != m->lastTrueMap || trun.estMap != m->lastEstMap)) partial = false;
    if (!partial) m->coreGov.OnFullRun(nowMs);

    if (partial && !m->touched.empty()) {                             // the list for the sweeps (t38)
        D3D11_MAPPED_SUBRESOURCE map;
        if (SUCCEEDED(g_ctx->Map(m->zoneListBuf, 0, D3D11_MAP_WRITE_DISCARD, 0, &map))) {
            memcpy(map.pData, m->touched.data(), m->touched.size() * sizeof(uint32_t));
            g_ctx->Unmap(m->zoneListBuf, 0);
        }
    }
    m->runMode = FaldRunModeWord(partial, FaldConvSoOf(m->convVariant), (unsigned int)m->touched.size());

    // No render target may be bound while the passes write their UAVs.
    g_ctx->OMSetRenderTargets(0, nullptr, nullptr);

    // Starfield first: every pass below reads Balance(source) from its plan. Partial: S0 on the touched zones, then the
    // plan and a full round-0 sweep only if a zone's star statistic moved (the decide pass's arguments).
    const StatDispatch stat0Full = { false, true, FALD_ARGS_STAT0_FULL, FALD_ARGS_STAT0_FULL_COMBINE };
    const StatDispatch stat0Part = { true, true, FALD_ARGS_STAT0_PART, FALD_ARGS_STAT0_PART_COMBINE };
    const StatDispatch stat1Full = { false, true, FALD_ARGS_STAT1_FULL, FALD_ARGS_STAT1_FULL_COMBINE };
    const StatDispatch stat1Part = { true, true, FALD_ARGS_STAT1_PART, FALD_ARGS_STAT1_PART_COMBINE };
    // The caller's "the frame changed everywhere" (hook_fald.h frameChanged: the dynamic tone map's peak moved, decided on
    // the GPU): copied into changeFlags[3], GPU to GPU, and the decide pass before any sweep orders S0 and round 0's sweep
    // in full when it is set; every later decide takes it as "moved" (fald_change.h). Without such a caller the flag is 0.
    const bool frameMayChange = partial && frameChanged != nullptr;
    if (frameMayChange) {
        const D3D11_BOX box = { frameChangedOffset, 0, 0, frameChangedOffset + 4u, 1, 1 };
        g_ctx->CopySubresourceRegion(m->changeFlagsBuf, 0, FALD_CHANGE_FLAG_FRAME_BYTES, 0, 0, frameChanged, 0, &box);
        RunDecide(m, 3, true);
    }
    if (m->starOn) {
        if (!partial) RunStar(m);
        else { RunStarS0(m, true); if (frameMayChange) RunStarS0(m, false, true); RunDecide(m, 2, true); RunStarPlan(m, true); }
    }
    GpuTimingStamp(ts, 1);                       // the starfield's share
    if (m->fadeOn) RunFadeFrame(m, newContent);  // fade stack: the fade, F, T — once per content frame (fald_fade.h)
    GpuTimingStamp(ts, 2);                       // (the fade's passes are its own governor's)
    // Both rounds' reach of the intermediate they were formed from (no new content, LED lag off, the same settings): reused
    const unsigned long long reachKey = m->fadeOn ? ReachKey(m, frameKey, trun.temporal, trun.panel) : 0ull;
    const bool reachReuse = m->fadeOn && !newContent && FaldFadeReachCurrent(m->fade, reachKey);
    // The always-on core (shared/fald_core2.h): round 2's zone IIR advances by the refreshes since the previous run (mode
    // 3's k; else the run interval over the refresh period); a resume / mode change resets it (round 3 resets every run).
    // The test seam (every run = k refreshes, whatever the timing) also stands in for a timing-induced resume.
    if (m->core2On) {
        const unsigned long long k = FaldCore2Refreshes(trun.panel, m->clkElapsed, m->dtMs, m->refreshMs);
        if ((trun.resumed || trun.stateReset) && g_faldCore2TestRefreshes == 0) FaldCore2Invalidate(m->core2);
        FaldCore2BeginRun(m->core2, g_ctx, m->core2Flags, FaldCore2IirFactor(k));
    }
    if (trun.clock.runPass) RunPanelClock(m, partial);   // mode 3: this frame's LED state comes from PAST frames only

    // Two inverse rounds. Round 0's output is what round 1's statistic and boost count see, so the
    // panel's own response is accounted for in the frame it actually receives.
    // LED lag modes 1 / 2 filter each round's drive (both rounds read the SAME committed state).
    // Partial: the sweeps on the touched zones (the whole lattice when the starfield / round 0 moved), the rest by the
    // decide pass's arguments — the core's passes included (src/fald.cpp has the same order).
    if (!partial) RunStat(m, 0);
    else if (!m->starOn && !frameMayChange) RunStat(m, 0, kStatPartial);   // (else the pre-decide's / decide(2)'s arguments)
    else { RunStat(m, 0, stat0Full); RunStat(m, 0, stat0Part); }
    if (trun.temporal) RunTemporal(m, inDriveOf(0), 0, partial);
    RunDecide(m, 0, partial);                 // round 0's boost (boostTex[0]); partial: what follows from the flags
    if (m->core2On) FaldCore2RunZone(m->core2, g_ctx, m->cb, partial ? m->argsBuf : nullptr, FALD_ARGS_CORE_LATTICE);   // the cut, alpha, the gate
    RunConv(m, mapSrv(trun.trueMap, 0, trun.panel), mapSrv(trun.estMap, 0, trun.panel), m->boostSRV[0], 0, partial);
    RunGain(m, 0, partial);
    if (m->core2On)                           // round 0's deficit from round 0's gain (the round-1 statistic reads it)
        FaldCore2RunDeficit(m->core2, g_ctx, m->cb, m->gainFused ? m->gain0SRV : m->gainBSRV, 0, partial ? m->argsBuf : nullptr, FALD_ARGS_CORE_FINE0);
    GpuTimingStamp(ts, 3);
    if (m->fadeOn && !reachReuse) RunFadeReach(m, 0, reachKey);   // reach of round 0's knee: FadePost in the round-1 statistic
    GpuTimingStamp(ts, 4);
    if (!partial) RunStat(m, 1);
    else { RunStat(m, 1, stat1Full); RunStat(m, 1, stat1Part); }
    if (trun.temporal) RunTemporal(m, inDriveOf(1), 1, partial);
    RunDecide(m, 1, partial);                 // round 1's boost (boostTex[1]); partial: the convolution, the gain, the pixel pass
    RunConv(m, mapSrv(trun.trueMap, 1, trun.panel), mapSrv(trun.estMap, 1, trun.panel), m->boostSRV[1], 1, partial);
    RunGain(m, 1, partial);
    if (m->core2On)                           // round 1's deficit from round 1's gain (the pixel pass reads it)
        FaldCore2RunDeficit(m->core2, g_ctx, m->cb, m->gainBSRV, 1, partial ? m->argsBuf : nullptr, FALD_ARGS_CORE_FINE1);
    GpuTimingStamp(ts, 5);
    if (m->fadeOn && !reachReuse) RunFadeReach(m, 1, reachKey);   // reach of round 1's knee: FadePost in the pixel pass
    GpuTimingStamp(ts, 6);
    m->framesRun++;
    m->lastPartial = partial;

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
    PixelPass(m, dstRTV, mapSrv(trun.debugFiltMap, 1), partial);
    if (m->core2On) FaldCore2EndRun(m->core2);
    GpuTimingEnd(ts, m);
    // what this run leaves behind for the next present's plan (shared/fald_change.h)
    m->stateComplete = true;
    m->fieldsStand = true;
    m->lastSettingsKey = settingsKey;
    m->lastLutPassKey = lutPassKey;
    m->lastTrueMap = trun.trueMap; m->lastEstMap = trun.estMap;

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
                m->settleEvt = OpenHostEvent(name);
            }
            if (!m->contentEvt) {
                swprintf_s(name, DWM_HOOK_FALD_CONTENT_EVENT_FMT, m->left, m->top);
                m->contentEvt = OpenHostEvent(name);
            }
        }
        if (newContent && m->contentEvt) SetEvent(m->contentEvt);
        if (pending && m->settleEvt) SetEvent(m->settleEvt);
    }

    // The governors' status for the host (its FALD panel, Events, state.get)
    WriteGovernorStatus(m);

    QueryPerformanceCounter(&t1);
    if (freq.QuadPart > 0)
        m->lastRunUs = (double)(t1.QuadPart - t0.QuadPart) * 1e6 / (double)freq.QuadPart;
    // The cost of the whole layer, logged rarely: overrunning DWM's present budget stutters the whole
    // desktop, and a number beats a guess. ~every 10 s at 60 Hz. CPU = submission time in the present
    // path; GPU = the timestamp queries' average / max over the interval, and the starfield share.
    if (partial) m->partialRuns++;
    if ((m->framesRun % 600ull) == 1ull) {
        if (m->gpuSamples > 0)
            LogF("FALD: pos(%d,%d) %s frame %llu, CPU %.0f us | GPU avg %.0f us (core %.0f), max %.0f us, starfield %.0f us (%u samples) [star %s, fade %s, core mode %u %s, partial runs %llu, held runs %llu, governor %s]",
                 m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->framesRun, m->lastRunUs,
                 m->gpuSumUs / m->gpuSamples, m->gpuCoreSumUs / m->gpuSamples, m->gpuMaxUs, m->gpuStarSumUs / m->gpuSamples, m->gpuSamples,
                 m->starOn ? "on" : "off", m->fadeOn ? "on" : "off", m->coreModeWanted, m->core2On ? FaldCoreModeName(m->coreModeWanted) : "C",
                 m->partialRuns, m->heldRuns, FaldCoreGovStateName(m->coreGov.State()));
        else
            LogF("FALD: pos(%d,%d) %s frame %llu, CPU %.0f us/frame in the present path (no GPU timing yet) [star %s, fade %s]",
                 m->left, m->top, m->isHdr ? "HDR" : "SDR(ACM)", m->framesRun, m->lastRunUs,
                 m->starOn ? "on" : "off", m->fadeOn ? "on" : "off");
        m->gpuSumUs = m->gpuStarSumUs = m->gpuMaxUs = m->gpuCoreSumUs = 0.0;
        m->gpuSamples = 0;
    }
    return true;
}

#ifdef DLUT_HOOK_TESTING
// ---------------------------------------------------------------------------------------------
// Test seams (tests/test_fald_hook.cpp): compiled into the test exe only, never into the DLL
// ---------------------------------------------------------------------------------------------
// A live panel delivery without the named section (the section path is IngestDelivery's; this is the rest of it).
bool FaldTestDeliver(int left, int top, bool isHdr, unsigned int gen, const void* payload, size_t bytes) {
    FaldPanelLockGuard lock(g_panelsLock, true);
    if (!g_panels.Wants(left, top, isHdr, gen)) return false;
    std::string err;
    const uint32_t ack = g_panels.Deliver(left, top, isHdr, gen, payload, bytes, err);
    if (ack == DWM_HOOK_FALD_PANEL_ACK_INGESTED) NoteSlotChanged(left, top, isHdr);
    RefreshAnyPanelPresent();
    return ack == DWM_HOOK_FALD_PANEL_ACK_INGESTED;
}
FaldFade* FaldTestFade(FaldMonitor* m) { return m ? m->fade : nullptr; }
bool FaldTestFadeOn(const FaldMonitor* m) { return m && m->fadeOn; }
unsigned int FaldTestFadeGovState(const FaldMonitor* m) { return m ? (unsigned int)m->fadeGov.State() : 0u; }
unsigned int FaldTestCoreGovState(const FaldMonitor* m) { return m ? (unsigned int)m->coreGov.State() : 0u; }
unsigned long long FaldTestHeldRuns(const FaldMonitor* m) { return m ? m->heldRuns : 0ull; }
ID3D11Buffer* FaldTestCB(FaldMonitor* m) { return m ? m->cb : nullptr; }
unsigned long long FaldTestClockElapsed(const FaldMonitor* m) { return m ? m->clkElapsed : 0ull; }
bool FaldTestClockSeeded(const FaldMonitor* m) { return m && m->clkSeeded; }
bool FaldTestLastPartial(const FaldMonitor* m) { return m && m->lastPartial; }
#endif
