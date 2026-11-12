// Performance instruments for the DWM hook's pass, the overlay's pass and their shaders. All three are OPT-IN (an
// environment variable each); without one, every case here returns at once and the normal suite stays as it was.
//
//   1. DLUT_PERF_STATIC=<dir>      Static cost table, no GPU: every shipped shader compiled exactly as the product
//                                  compiles it (same text, entry point, target, flags), then reflected and disassembled:
//                                  instructions, texture samples / loads, log / exp / sqrt / div, branches, loops, temp
//                                  registers, constant-buffer bytes -> <dir>/static_costs.tsv + one .asm per shader.
//                                  The hook's and the overlay's pixel shaders are ONE shader each whose mode is chosen
//                                  by constant-buffer switches at run time, so the table also has the "executed path"
//                                  of each user mode: the same text with that mode's switches made compile-time
//                                  constants (rows "<shader>@<mode>"), and the experiment variants ("...+sl" etc).
//                                  DLUT_PERF_STATIC_REF=<tsv> prints every row that differs from a recorded table
//                                  (tests/fixtures/perf_static_baseline.tsv is the integration state of 2026-11-12).
//   2. DLUT_PERF_GPU=<dir>         GPU time, offscreen, in this process (no DWM, no DesktopLUT): the hook's REAL pass
//                                  (dwm_hook/hook_render.cpp compiled into this exe: InitializeStuff + RenderLUT, the
//                                  cube parsed by the hook's own parser, the FALD layer's own files) over a back buffer
//                                  standing in for DWM's, per configuration x resolution x dirty-rect scenario; the
//                                  overlay's pass with the product's shader text and the same resources src/gpu.cpp
//                                  makes; the bare copy / peak-detection passes; CPU time of the calls themselves.
//                                  -> <dir>/hook_times.tsv, overlay_times.tsv, primitives.tsv.
//                                  DLUT_PERF_ADAPTER = a substring of the adapter's name ("" = the first hardware one;
//                                  "WARP" = the software rasteriser), DLUT_PERF_N runs per case (40; WARP 5),
//                                  DLUT_PERF_HEAT = "12,0" (ms of GPU warm-up before each timed run; each listed value
//                                  is its own set of rows), DLUT_PERF_RES = "1080,1440,2160", DLUT_PERF_ONLY = substrings
//                                  of "<cfg>/<variant>/<scenario>", DLUT_PERF_LUT = the cube's size (65),
//                                  DLUT_PERF_VARIANTS=0 skips the experiment variants, DLUT_PERF_BATCH = K repetitions
//                                  per timed submission (default 1; 8 = the steady-state cost, see the code).
//                                  Etiquette: one run per submission (a few ms), below-normal priority, a 2 ms pause
//                                  between runs, a case stops at the first run over 45 ms.
//   3. DLUT_PERF_HASH_WRITE=<file> / DLUT_PERF_HASH_REF=<file>   The bit-identity gate: every measured configuration
//                                  rendered on WARP (DLUT_PERF_HASH_ADAPTER to use another adapter) over fixed synthetic
//                                  frames, each present's back buffer hashed (FNV-1a 64) into a manifest; REF checks a
//                                  build against a recorded manifest and FAILS on any difference. With
//                                  DLUT_PERF_REF_DIR=<dir> WRITE also stores the frames and REF reports how far a
//                                  differing frame is from its reference (in the target's own units), so a change is
//                                  classified as bit-identical / within rounding (how much) / a different picture.
//                                  tests/fixtures/perf_ref_warp.hashes is the integration state of 2026-11-12.
//
// The hook's sources in this exe are built with DLUT_HOOK_TESTING (a %TEMP% log, no host events, no host sections); this
// file never calls ApplyLUT / ApplyLUTDirect (they persist routing state under %SYSTEMROOT%\Temp) and the pass's dump
// trigger probe (same folder) is compiled out of hook_render.cpp for this exe (DesktopLUT.Tests.vcxproj).
#include "perf_common.h"
#include "doctest.h"
#include <memory>
#include <tlhelp32.h>
#include <mmsystem.h>   // timeBeginPeriod (winmm.lib is among the test exe's libraries)

#include "../dwm_hook/hook_render.h"
#include "../dwm_hook/hook_lut.h"
#include "../dwm_hook/hook_fald.h"
#include "../dwm_hook/hook_shader.h"     // g_shaders (the hook's VS + PS), noiseBytes
#include "../dwm_hook/hook_shader_bytecode.h"   // what the DLL creates its shaders from
#include "../src/shader.h"               // g_vsSource, g_psSource, g_analysisCSSource, BuildDesktopGammaLut
#include "../src/types.h"                // g_blueNoiseData
#include "../shared/fald_shader.h"
#include "../shared/fald_core_mode.h"
#include "../shared/peak_detect.h"
#include "../shared/tonemap_curves.h"
#include "../shared/hdr_dither.h"

#pragma comment(lib, "version.lib")  // the compiler DLL's version (the bytecode test)
#pragma comment(lib, "dxguid.lib")   // IID_ID3D11Texture2D / IID_ID3D11Device for hook_render.cpp in this exe (the DLL links it)

using perf::Dom;
using perf::Kind;

// ---------------------------------------------------------------------------------------------------------------
// What dwm_hook/dllmain.cpp gives hook_render.cpp in the DLL (the test exe has no dllmain.cpp)
// ---------------------------------------------------------------------------------------------------------------
bool isWindows11 = false;
bool isWindows11_24h2 = false;
extern bool isWindows11_25h2;                                   // tests/test_lut_canonical.cpp
extern const int COverlayContext_DeviceClipBox_offset = 0;
int COverlayContext_DeviceClipBox_offset_w11 = 0;
int COverlayContext_DeviceClipBox_offset_w11_24h2 = 0;
LocalFaldParams g_localFald[MAX_DWM_HOOK_MONITORS] = {};
int g_numLocalFald = 0;
static LocalTonemapParams s_perfTonemap = {};
LocalTonemapParams* FindTonemapForMonitor(int left, int top) { return (left == 0 && top == 0) ? &s_perfTonemap : nullptr; }
LocalFaldParams* FindFaldForMonitor(int left, int top) {
    for (int i = 0; i < g_numLocalFald; i++)
        if (g_localFald[i].left == left && g_localFald[i].top == top) return &g_localFald[i];
    return nullptr;
}
void PerfStub_FaldPollDumpRequest() {}                          // hook_render.cpp's FaldPollDumpRequest in this exe
extern D3D11_TEXTURE2D_DESC textureDesc[2];                     // hook_render.cpp: the shared staging textures' sizes
extern ID3D11PixelShader* pixelShader;                          // hook_render.cpp: the pass's pixel shader

namespace {

// ---------------------------------------------------------------------------------------------------------------
// The cube: a smooth non-identity transform inside [0, 1] (so every interpolation path matters to the output)
// ---------------------------------------------------------------------------------------------------------------
void CubeFn(float r, float g, float b, float o[3]) {
    o[0] = std::clamp(r + 0.020f * std::sin(3.1f * g) * r * (1.0f - r) * 4.0f, 0.0f, 1.0f);
    o[1] = std::clamp(g + 0.015f * std::sin(2.7f * b + 0.5f) * g * (1.0f - g) * 4.0f, 0.0f, 1.0f);
    o[2] = std::clamp(b * (0.985f + 0.015f * r) + 0.010f * std::sin(4.0f * g) * b * (1.0f - b) * 4.0f, 0.0f, 1.0f);
}
bool WriteCube(const std::string& path, int n) {
    FILE* f = fopen(path.c_str(), "w");
    if (!f) return false;
    fprintf(f, "LUT_3D_SIZE %d\n", n);
    for (int b = 0; b < n; b++)
        for (int g = 0; g < n; g++)
            for (int r = 0; r < n; r++) {
                float o[3];
                CubeFn((float)r / (float)(n - 1), (float)g / (float)(n - 1), (float)b / (float)(n - 1), o);
                fprintf(f, "%.7f %.7f %.7f\n", o[0], o[1], o[2]);
            }
    fclose(f);
    return true;
}
// The same cube as a texture of another size / format (the experiments; the hook's own are R32G32B32A32_FLOAT).
ID3D11ShaderResourceView* MakeCubeSrv(ID3D11Device* dev, int n, DXGI_FORMAT fmt) {
    std::vector<float> f((size_t)n * n * n * 4);
    for (int b = 0; b < n; b++)
        for (int g = 0; g < n; g++)
            for (int r = 0; r < n; r++) {
                float* o = &f[(((size_t)b * n + g) * n + r) * 4];
                CubeFn((float)r / (float)(n - 1), (float)g / (float)(n - 1), (float)b / (float)(n - 1), o);
                o[3] = 1.0f;
            }
    std::vector<uint16_t> h;
    const void* data = f.data();
    UINT texel = 16;
    if (fmt == DXGI_FORMAT_R16G16B16A16_FLOAT || fmt == DXGI_FORMAT_R16G16B16A16_UNORM) {
        h.resize(f.size());
        for (size_t i = 0; i < f.size(); i++)
            h[i] = fmt == DXGI_FORMAT_R16G16B16A16_FLOAT ? perf::FloatToHalf(f[i]) : (uint16_t)std::lround(std::clamp(f[i], 0.0f, 1.0f) * 65535.0f);
        data = h.data(); texel = 8;
    }
    D3D11_TEXTURE3D_DESC d = {};
    d.Width = d.Height = d.Depth = (UINT)n; d.MipLevels = 1; d.Format = fmt; d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA init = { data, (UINT)n * texel, (UINT)n * (UINT)n * texel };
    ID3D11Texture3D* t = nullptr;
    if (FAILED(dev->CreateTexture3D(&d, &init, &t))) return nullptr;
    ID3D11ShaderResourceView* v = nullptr;
    dev->CreateShaderResourceView(t, nullptr, &v);
    t->Release();
    return v;
}

std::string OwnerPanel() {
    const std::string env = perf::Env("DLUT_PERF_FALD_PANEL");
    if (!env.empty()) return GetFileAttributesA(env.c_str()) != INVALID_FILE_ATTRIBUTES ? env : std::string();
    const char* p = "H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";   // the PA32UCXR's (3840 x 2160, HDR)
    return GetFileAttributesA(p) != INVALID_FILE_ATTRIBUTES ? std::string(p) : std::string();
}

// ---------------------------------------------------------------------------------------------------------------
// The hook's pass, as the DLL runs it: one monitor at (0, 0)
// ---------------------------------------------------------------------------------------------------------------
struct HookCfg {
    std::string id;
    Dom dom;
    bool lut, tm; int curve; bool dyn, fald, ditherOff;
};
std::vector<HookCfg> HookConfigs() {
    return {
        { "sdr8_lut",             Dom::Sdr8,  true,  false, 0, false, false, false },   // legacy SDR, 8-bit back buffer
        { "sdr10_lut",            Dom::Sdr10, true,  false, 0, false, false, false },   // legacy SDR, 10-bit back buffer
        { "acm_lut",              Dom::Acm,   true,  false, 0, false, false, false },   // SDR under Auto Color Management (FP16)
        { "hdr_lut",              Dom::Hdr,   true,  false, 0, false, false, false },   // HDR, cube only (+ output dither)
        { "hdr_lut_nodither",     Dom::Hdr,   true,  false, 0, false, false, true  },
        { "hdr_tm_bt2390",        Dom::Hdr,   false, true,  0, false, false, false },   // HDR, static tone map only
        { "hdr_lut_tm_bt2390",    Dom::Hdr,   true,  true,  0, false, false, false },
        { "hdr_lut_tm_softclip",  Dom::Hdr,   true,  true,  1, false, false, false },
        { "hdr_lut_tm_reinhard",  Dom::Hdr,   true,  true,  2, false, false, false },
        { "hdr_lut_tm_bt2446a",   Dom::Hdr,   true,  true,  3, false, false, false },
        { "hdr_lut_tm_hardclip",  Dom::Hdr,   true,  true,  4, false, false, false },
        { "hdr_tm_dyn",           Dom::Hdr,   false, true,  0, true,  false, false },   // dynamic peak (+ the detector's two dispatches)
        { "hdr_lut_tm_dyn",       Dom::Hdr,   true,  true,  0, true,  false, false },
        { "hdr_fald",             Dom::Hdr,   false, false, 0, false, true,  false },   // the FALD core alone (KC5, starfield off = the code's defaults)
        { "hdr_lut_tm_dyn_fald",  Dom::Hdr,   true,  true,  0, true,  true,  false },   // everything
    };
}

struct HookRig {
    perf::Dev& d;
    std::string stage;
    int lutN = 0;
    bool ok = false, panel = false, prev25h2 = false;
    double initUs = 0.0;                       // InitializeStuff: in the DLL it runs inside the first hooked present
    double faldInitUs = 0.0;                   // FaldInitShaders: inside the first present of a monitor that needs the layer
    int fakeCtx[4] = {};                       // the pass only uses the context as a key

    HookRig(perf::Dev& dev, int lutSize) : d(dev), lutN(lutSize) {
        char tmp[MAX_PATH] = {};
        GetTempPathA(MAX_PATH, tmp);
        stage = std::string(tmp) + "dlut_perf_" + std::to_string(GetCurrentProcessId());
        CreateDirectoryA(stage.c_str(), nullptr);
        CreateDirectoryA((stage + "\\fald").c_str(), nullptr);
        if (!WriteCube(stage + "\\0_0.cube", lutN) || !WriteCube(stage + "\\0_0_hdr.cube", lutN)) return;
        const std::string src = OwnerPanel();
        if (!src.empty()) panel = CopyFileA(src.c_str(), (stage + "\\fald\\0_0_hdr.bin").c_str(), FALSE) != FALSE;
        UninitializeStuff();                   // whatever an earlier case left (no-op on a clean process)
        std::memset(textureDesc, 0, sizeof(D3D11_TEXTURE2D_DESC) * 2);
        ResetContextRouting();
        if (!AddLUTs((char*)stage.c_str()) || numLuts != 2) return;
        if (panel) panel = FaldLoadPanelFiles(stage.c_str()) == 1;
        prev25h2 = isWindows11_25h2;
        isWindows11_25h2 = true;               // the context's monitor comes from the routing cache, as on 25H2
        g_numMonitorHdrStates = 1;
        g_monitorHdrStates[0] = { 0, 0, false, 10u, 1920u, 1080u };
        const double t0 = perf::QpcUs();
        InitializeStuff(d.dev);
        initUs = perf::QpcUs() - t0;
        if (!device) return;                   // hook_render.cpp's: null = the init failed (its log says why)
        CacheContextPositionEx(fakeCtx, 0, 0, CTXPOS_UNIQUE);
        ok = true;
    }
    ~HookRig() {
        UninitializeStuff();
        std::memset(textureDesc, 0, sizeof(D3D11_TEXTURE2D_DESC) * 2);
        ResetContextRouting();
        g_numMonitorHdrStates = 0;
        g_numLocalFald = 0;
        g_hdrDitherOff = 0;
        isWindows11_25h2 = prev25h2;
        for (const char* f : { "\\0_0.cube", "\\0_0_hdr.cube", "\\fald\\0_0_hdr.bin" }) DeleteFileA((stage + f).c_str());
        RemoveDirectoryA((stage + "\\fald").c_str());
        RemoveDirectoryA(stage.c_str());
    }
    void Apply(const HookCfg& c, int W, int H) {
        const bool hdr = c.dom == Dom::Hdr;
        g_monitorHdrStates[0] = { 0, 0, hdr, c.dom == Dom::Sdr8 ? 8u : 10u, (unsigned)W, (unsigned)H };
        g_numMonitorHdrStates = 1;
        LocalTonemapParams& t = s_perfTonemap;
        t.left = 0; t.top = 0;
        t.enabled = c.tm;
        t.curve = (DwmHookTonemapCurve)c.curve;
        t.sourcePeakNits = 4000.0f; t.targetPeakNits = 1000.0f;
        t.dynamicPeak = c.dyn;
        t.pqSourcePeak = LinearToPQ(t.sourcePeakNits / 10000.0f);
        t.pqTargetPeak = LinearToPQ(t.targetPeakNits / 10000.0f);
        t.lutOff = !c.lut;
        g_localFald[0] = {};
        g_localFald[0].flags = DwmHookFaldPackCore2(DwmHookFaldPack(c.fald ? 1 : 0, 0, 0), FALD_CORE_MODE_DEFAULT);
        g_localFald[0].hasTuning = false;      // the layer's own defaults
        g_numLocalFald = 1;
        g_hdrDitherOff = c.ditherOff ? 1u : 0u;
    }
    // Would the FALD layer run for this frame (a panel file whose lattice fits it)?
    bool FaldAvailable(int W, int H) {
        if (!panel) return false;
        if (!FaldShadersReady()) {             // as RenderLUT asks for them on that monitor's first present
            const double t0 = perf::QpcUs();
            const bool ok = FaldInitShaders(device, deviceContext);
            faldInitUs = perf::QpcUs() - t0;
            if (!ok) return false;
        }
        FaldMonitor* m = FaldAcquire(0, 0, true, (unsigned)W, (unsigned)H, DXGI_FORMAT_R16G16B16A16_FLOAT);
        if (!m) return false;
        FaldReturn(m);
        return true;
    }
    bool Present(ID3D11Texture2D* back, std::vector<RECT>& rects) {
        try { return RenderLUT(fakeCtx, back, rects.data(), (int)rects.size()); }
        catch (...) { return false; }
    }
};

struct Scenario { std::string id; Kind kind; std::vector<RECT> rects; };
std::vector<Scenario> Scenarios(int W, int H) {
    const int s = std::max(1, H / 1080);
    std::vector<Scenario> v;
    v.push_back({ "full", Kind::Video, { RECT{ 0, 0, W, H } } });                            // fullscreen video / game: everything, every present
    v.push_back({ "video_window", Kind::Mix, { perf::VideoWindow(W, H) } });                 // a video in a window (44 % of the frame)
    v.push_back({ "scroll", Kind::Mix, { RECT{ W * 80 / 1920, H * 60 / 1080, W * 1000 / 1920, H * 900 / 1080 } } });   // a scrolling document (37 %)
    v.push_back({ "small", Kind::Mix, { RECT{ W / 5, H * 3 / 10, W / 5 + 2 * s, H * 3 / 10 + 18 * s },                // a caret and the clock
                                        RECT{ W - 90 * s, H - 40 * s, W - 10 * s, H - 8 * s } } });
    Scenario m{ "multi8", Kind::Mix, {} };                                                   // eight 128 px tiles (hover effects, a spinner, ...)
    for (int i = 0; i < 8; i++) {
        const int x = (W - 128 * s) * (i * 37 % 100) / 100, y = (H - 128 * s - 48 * s) * (i * 61 % 100) / 100;
        m.rects.push_back(RECT{ x, y, x + 128 * s, y + 128 * s });
    }
    v.push_back(m);
    return v;
}
double DirtyPx(const std::vector<RECT>& r) {
    double a = 0;
    for (const RECT& x : r) a += (double)(x.right - x.left) * (double)(x.bottom - x.top);
    return a;
}

// "DWM composes": the present's rects of the back buffer get new content (iter odd: the content shifted, so the pixels
// really change; even: as it is). Outside the rects the back buffer keeps the previous present's finished output.
void Compose(ID3D11DeviceContext* ic, ID3D11Texture2D* back, ID3D11Texture2D* content, const std::vector<RECT>& rects, int W, int H, int iter) {
    for (const RECT& r : rects) {
        if (r.left == 0 && r.top == 0 && r.right == W && r.bottom == H) {
            if (iter & 1) {
                D3D11_BOX a = { 0, 0, 0, (UINT)(W / 2), (UINT)H, 1 }, b = { (UINT)(W / 2), 0, 0, (UINT)W, (UINT)H, 1 };
                ic->CopySubresourceRegion(back, 0, (UINT)(W - W / 2), 0, 0, content, 0, &a);
                ic->CopySubresourceRegion(back, 0, 0, 0, 0, content, 0, &b);
            } else ic->CopyResource(back, content);
            continue;
        }
        int dx = (iter & 1) ? 61 : 0;
        if (r.right + dx > W) dx = -dx;
        if (r.left + dx < 0) dx = 0;
        D3D11_BOX box = { (UINT)(r.left + dx), (UINT)r.top, 0, (UINT)(r.right + dx), (UINT)r.bottom, 1 };
        ic->CopySubresourceRegion(back, 0, (UINT)r.left, (UINT)r.top, 0, content, 0, &box);
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Variants of the hook's pixel shader (made from its own text)
// ---------------------------------------------------------------------------------------------------------------
struct HookSwitches { int colorMode, tonemapEnabled, tonemapCurve, tonemapDynamic, hasLut; };
HookSwitches SwitchesOf(const HookCfg& c) {
    return { c.dom == Dom::Hdr ? 1 : c.dom == Dom::Acm ? 2 : 0, c.tm ? 1 : 0, c.tm ? c.curve : 0, (c.tm && c.dyn) ? 1 : 0, c.lut ? 1 : 0 };
}
struct PsVariant { std::string id; bool fold, sampleLevel, pqLut; bool lutLoad = false; };
const PsVariant kPsVariants[] = {
    { "spec",           true,  false, false },   // this mode's switches as compile-time constants (what a per-mode shader would execute)
    { "sl",             false, true,  false },   // every implicit-gradient Sample as SampleLevel(0)
    { "spec+sl",        true,  true,  false },
    { "spec+sl+pqlut",  true,  true,  true  },   // + the cube path's analytic PQ through the 4096-entry tables the tone map already uses
    { "load",           false, false, false, true },   // the cube's four texels by Load (integer texel, clamped) instead of Sample(point) + a divide each
    { "spec+sl+load",   true,  true,  false, true },
};
std::string HookPsText(const PsVariant& v, const HookSwitches& sw, std::string* err) {
    std::string s(g_shaders, sizeof g_shaders - 1);
    if (v.fold) {
        perf::FoldField(s, "int colorMode;", "int", "colorMode", std::to_string(sw.colorMode), err);
        perf::FoldField(s, "int tonemapEnabled;", "int", "tonemapEnabled", std::to_string(sw.tonemapEnabled), err);
        perf::FoldField(s, "int tonemapCurve;", "int", "tonemapCurve", std::to_string(sw.tonemapCurve), err);
        perf::FoldField(s, "int tonemapDynamic;", "int", "tonemapDynamic", std::to_string(sw.tonemapDynamic), err);
        perf::FoldField(s, "int hasLut;", "int", "hasLut", std::to_string(sw.hasLut), err);
        perf::FoldField(s, "float4 mhcRow0;", "float4", "mhcRow0", "float4(0, 0, 0, 0)", err);   // the live edit preview off
    }
    if (v.lutLoad) {
        perf::ReplaceOnce(s, "float3 tex = (index + 0.5) / lutSize;", "int3 texi = min((int3)index, lutSize - 1);", err);
        perf::ReplaceOnce(s, "return lutTex.Sample(smp, tex).rgb;", "return lutTex.Load(int4(texi, 0)).rgb;", err);
    }
    if (v.sampleLevel && perf::SampleToSampleLevel(s) == 0 && err) *err += "no Sample call found ";
    if (v.pqLut) {
        perf::ReplaceOnce(s, "return pow(max((pow(e, 1 / m2) - c1), 0) / (c2 - c3 * pow(e, 1 / m2)), 1 / m1);", "return PQ_to_Linear(e);", err);
        perf::ReplaceOnce(s, "return pow((c1 + c2 * pow(y, m1)) / (1 + c3 * pow(y, m1)), m2);", "return Linear_to_PQ(y);", err);
    }
    return s;
}
std::string SwitchTag(const HookSwitches& sw) {
    char b[64];
    snprintf(b, sizeof(b), "m%d_tm%d_c%d_d%d_l%d", sw.colorMode, sw.tonemapEnabled, sw.tonemapCurve, sw.tonemapDynamic, sw.hasLut);
    return b;
}

// ---------------------------------------------------------------------------------------------------------------
// The overlay's pass: the product's shader text over the resources src/gpu.cpp InitD3D creates, drawn as
// src/render.cpp RenderMonitor draws it (clear, bind, one fullscreen triangle)
// ---------------------------------------------------------------------------------------------------------------
struct OvCfg {
    std::string id;
    Dom dom;                 // the captured desktop's format (Sdr8 -> R10G10B10A2 target, else FP16)
    bool lut, tetra, dg, prim, gs, tm; int curve; bool dyn;
};
std::vector<OvCfg> OverlayConfigs() {
    return {
        { "sdr8_lut",            Dom::Sdr8, true,  true,  false, false, false, false, 0, false },
        { "sdr8_lut_trilinear",  Dom::Sdr8, true,  false, false, false, false, false, 0, false },
        { "sdr8_full",           Dom::Sdr8, true,  true,  false, true,  true,  false, 0, false },   // primaries + grayscale + cube
        { "acm_lut",             Dom::Acm,  true,  true,  false, false, false, false, 0, false },
        { "hdr_lut",             Dom::Hdr,  true,  true,  false, false, false, false, 0, false },
        { "hdr_lut_trilinear",   Dom::Hdr,  true,  false, false, false, false, false, 0, false },
        { "hdr_dg_lut",          Dom::Hdr,  true,  true,  true,  false, false, false, 0, false },   // + the desktop-gamma shader stage
        { "hdr_lut_tm_bt2390",   Dom::Hdr,  true,  true,  false, false, false, true,  0, false },
        { "hdr_lut_tm_dyn",      Dom::Hdr,  true,  true,  false, false, false, true,  0, true  },
        { "hdr_full",            Dom::Hdr,  true,  true,  true,  true,  true,  true,  0, true  },   // REFERENCE.md's benchmark configuration
        { "hdr_preview_idle",    Dom::Hdr,  false, true,  false, true,  true,  false, 0, false },   // hook mode's editor preview: corrections, no cube, no tone map
    };
}
void OvFillCb(float cb[148], const OvCfg& c, int lutN) {
    std::memset(cb, 0, sizeof(float) * 148);
    const bool hdr = c.dom == Dom::Hdr;
    cb[0] = hdr ? 1.0f : 0.0f; cb[1] = 200.0f; cb[2] = 1000.0f; cb[3] = (float)lutN;
    cb[4] = (hdr && c.dg) ? 1.0f : 0.0f; cb[5] = c.tetra ? 1.0f : 0.0f; cb[6] = c.lut ? 0.0f : 1.0f;
    cb[7] = (c.prim || c.gs) ? 1.0f : 0.0f;
    cb[8] = 20.0f; cb[9] = c.gs ? 1.0f : 0.0f; cb[10] = (hdr && c.tm) ? 1.0f : 0.0f; cb[11] = (float)c.curve;
    static const float M[9] = { 1.020f, -0.015f, -0.005f, -0.010f, 1.015f, -0.005f, 0.000f, -0.020f, 1.020f };
    static const float I[9] = { 1, 0, 0, 0, 1, 0, 0, 0, 1 };
    static const float WB[3] = { 0.98f, 1.0f, 1.03f };
    for (int r = 0; r < 3; r++) {
        for (int k = 0; k < 3; k++) cb[12 + r * 4 + k] = c.prim ? M[r * 3 + k] : I[r * 3 + k];
        cb[12 + r * 4 + 3] = c.prim ? WB[r] : 1.0f;
    }
    if (c.dyn) cb[24] = LinearToPQ(DlutDynamicPeakFloorNits(1000.0f, c.curve == 0 || c.curve == 3) / 10000.0f);
    else cb[24] = LinearToPQ(4000.0f / 10000.0f);
    cb[25] = 1000.0f; cb[26] = c.dyn ? 1.0f : 0.0f; cb[27] = 0.0f;
    cb[28] = 1000.0f; cb[29] = c.dom == Dom::Acm ? 1.0f : 0.0f; cb[30] = LinearToPQ(1000.0f / 10000.0f); cb[31] = LinearToPQ(1000.0f / 10000.0f);
    const bool ictcp = hdr && c.gs && c.tm;
    for (int i = 0; i < 32; i++) {
        const float t = i < 20 ? (float)i / 19.0f : 1.0f;
        if (ictcp) {                       // additive ICtCp offsets
            cb[32 + i] = i < 20 ? 0.0020f * std::sin((float)i) : 0.0f; cb[64 + i] = i < 20 ? 0.0010f * t : 0.0f; cb[96 + i] = i < 20 ? -0.0010f * t : 0.0f;
        } else if (hdr) {                  // normalised PQ points
            cb[32 + i] = t * (1.0f + 0.010f * (1.0f - t)); cb[64 + i] = t; cb[96 + i] = t * (1.0f - 0.012f * (1.0f - t));
        } else {                           // SDR: signal-domain points, slot i = t^2
            const float b = t * t;
            cb[32 + i] = std::min(1.0f, b * 1.01f); cb[64 + i] = b; cb[96 + i] = b * 0.985f;
        }
    }
    cb[130] = ictcp ? 1.0f : 0.0f;
    cb[131] = DlutHdrDitherLsb(hdr, c.lut || c.tm || c.prim || c.gs || c.dg, true);
    cb[144] = 200.0f / 80.0f; cb[145] = 80.0f / 200.0f;
}
std::string OvPsText(const OvCfg& c, bool fold, std::string* err) {
    std::string s(g_psSource);
    if (!fold) return s;
    float cb[148];
    OvFillCb(cb, c, 33);
    auto f = [&](const char* name, int idx) {
        char v[32]; snprintf(v, sizeof(v), "%.1ff", cb[idx]);
        perf::FoldField(s, std::string("float ") + name + ";", "float", name, v, err);
    };
    f("isHDR", 0); f("desktopGamma", 4); f("tetrahedralInterp", 5); f("usePassthrough", 6); f("useManualCorrection", 7);
    f("grayscaleEnabled", 9); f("tonemapEnabled", 10); f("tonemapCurve", 11); f("tonemapDynamic", 26); f("grayscale24", 27);
    f("isFP16SDR", 29); f("motionBarEnabled", 128); f("grayscaleICtCp", 130);
    perf::FoldField(s, "float4 mhcPreviewRow0;", "float4", "mhcPreviewRow0", "float4(0, 0, 0, 0)", err);
    return s;
}

template <typename T> void Rel(T*& p) { if (p) { p->Release(); p = nullptr; } }

struct OverlayRig {
    perf::Dev& d;
    ID3D11VertexShader* vs = nullptr; ID3D11PixelShader* ps = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11SamplerState* smp[3] = {};
    ID3D11ShaderResourceView* noise = nullptr; ID3D11ShaderResourceView* dg = nullptr; ID3D11ShaderResourceView* pqO = nullptr;
    ID3D11ShaderResourceView* pqE = nullptr; ID3D11ShaderResourceView* sO = nullptr; ID3D11ShaderResourceView* sE = nullptr;
    ID3D11ShaderResourceView* wb = nullptr; ID3D11ShaderResourceView* prev = nullptr; ID3D11ShaderResourceView* cube = nullptr;
    ID3D11ComputeShader* reduceCS = nullptr; ID3D11ComputeShader* smoothCS = nullptr; ID3D11Buffer* peakCB = nullptr;
    ID3D11Texture2D* peakTex = nullptr; ID3D11UnorderedAccessView* peakUAV = nullptr; ID3D11ShaderResourceView* peakSRV = nullptr;
    ID3D11Texture2D* rawTex = nullptr; ID3D11UnorderedAccessView* rawUAV = nullptr;
    int lutN = 0;
    bool ok = false;
    std::string error;

    ID3D11ShaderResourceView* Row(const float* data, int n, int rows = 1) {
        ID3D11Texture2D* t = nullptr;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)n; td.Height = (UINT)rows; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R32_FLOAT; td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = { data, (UINT)n * 4u, 0 };
        if (FAILED(d.dev->CreateTexture2D(&td, &init, &t))) return nullptr;
        ID3D11ShaderResourceView* v = nullptr;
        d.dev->CreateShaderResourceView(t, nullptr, &v);
        t->Release();
        return v;
    }
    ID3D11PixelShader* CompilePs(const std::string& text) {
        ID3DBlob* b = nullptr; ID3DBlob* e = nullptr;
        if (FAILED(D3DCompile(text.c_str(), text.size(), "PS", nullptr, nullptr, "main", "ps_5_0", 0, 0, &b, &e))) {
            error = e ? (const char*)e->GetBufferPointer() : "ps compile";
            if (e) e->Release();
            return nullptr;
        }
        if (e) e->Release();
        ID3D11PixelShader* p = nullptr;
        d.dev->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &p);
        b->Release();
        return p;
    }
    OverlayRig(perf::Dev& dev, int lutSize) : d(dev), lutN(lutSize) {
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_vsSource, strlen(g_vsSource), "VS", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) return;
        d.dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ps = CompilePs(g_psSource);
        D3D11_SAMPLER_DESC sd = {};
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;  d.dev->CreateSamplerState(&sd, &smp[0]);
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR; d.dev->CreateSamplerState(&sd, &smp[1]);
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT; sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
        d.dev->CreateSamplerState(&sd, &smp[2]);
        D3D11_BUFFER_DESC cd = {};
        cd.ByteWidth = 592; cd.Usage = D3D11_USAGE_DYNAMIC; cd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        d.dev->CreateBuffer(&cd, nullptr, &cb);
        {
            D3D11_TEXTURE2D_DESC nd = {};
            nd.Width = nd.Height = 64; nd.MipLevels = 1; nd.ArraySize = 1; nd.Format = DXGI_FORMAT_R8_UNORM; nd.SampleDesc.Count = 1;
            nd.Usage = D3D11_USAGE_IMMUTABLE; nd.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            D3D11_SUBRESOURCE_DATA init = { g_blueNoiseData, 64, 0 };
            ID3D11Texture2D* t = nullptr;
            if (SUCCEEDED(d.dev->CreateTexture2D(&nd, &init, &t))) { d.dev->CreateShaderResourceView(t, nullptr, &noise); t->Release(); }
        }
        {
            std::vector<float> g(DLUT_DESKTOP_GAMMA_LUT_SIZE);
            BuildDesktopGammaLut(g.data());
            dg = Row(g.data(), DLUT_DESKTOP_GAMMA_LUT_SIZE);
        }
        {   // exactly src/gpu.cpp's tables
            const int N = 4096;
            const float m1 = 0.1593017578125f, m2 = 78.84375f, c1 = 0.8359375f, c2 = 18.8515625f, c3 = 18.6875f;
            std::vector<float> o(N), e(N);
            for (int i = 0; i < N; i++) {
                const float t = (float)i / (float)(N - 1);
                const float Ym = powf(std::max(t * t, 1e-12f), m1);
                o[i] = powf((c1 + c2 * Ym) / (1.0f + c3 * Ym), m2);
                const float Vm = powf(std::max(t, 1e-12f), 1.0f / m2);
                e[i] = powf(std::max(Vm - c1, 0.0f) / std::max(c2 - c3 * Vm, 1e-12f), 1.0f / m1);
            }
            pqO = Row(o.data(), N); pqE = Row(e.data(), N);
            const int S = 1024;
            std::vector<float> so(S), se(S), w(512);
            for (int i = 0; i < S; i++) {
                const float t = (float)i / (float)(S - 1);
                so[i] = t <= 0.0031308f ? 12.92f * t : 1.055f * powf(t, 1.0f / 2.4f) - 0.055f;
                se[i] = t <= 0.04045f ? t / 12.92f : powf((t + 0.055f) / 1.055f, 2.4f);
            }
            for (int i = 0; i < 512; i++) w[i] = powf(std::max((float)i / 511.0f * 2.0f, 0.001f), 1.0f / 2.2f);
            sO = Row(so.data(), S); sE = Row(se.data(), S); wb = Row(w.data(), 512);
            std::vector<float> z((size_t)4096 * 3, 0.0f);
            prev = Row(z.data(), 4096, 3);
        }
        cube = MakeCubeSrv(d.dev, lutN, DXGI_FORMAT_R32G32B32A32_FLOAT);   // src/lut.cpp CreateLUTTexture's format
        auto cs = [&](const char* src, ID3D11ComputeShader** out) {
            ID3DBlob* cb2 = nullptr;
            if (FAILED(D3DCompile(src, strlen(src), "cs", nullptr, nullptr, "main", "cs_5_0", 0, 0, &cb2, nullptr))) return;
            d.dev->CreateComputeShader(cb2->GetBufferPointer(), cb2->GetBufferSize(), nullptr, out);
            cb2->Release();
        };
        cs(g_peakReduceCSSource, &reduceCS); cs(g_peakSmoothCSSource, &smoothCS);
        D3D11_BUFFER_DESC pd = {};
        pd.ByteWidth = 32; pd.Usage = D3D11_USAGE_DYNAMIC; pd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; pd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        d.dev->CreateBuffer(&pd, nullptr, &peakCB);
        {
            D3D11_TEXTURE2D_DESC td = {};
            td.Width = td.Height = 1; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R32_FLOAT; td.SampleDesc.Count = 1;
            td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
            const float zero = 0.0f;
            D3D11_SUBRESOURCE_DATA init = { &zero, 4, 0 };
            if (SUCCEEDED(d.dev->CreateTexture2D(&td, &init, &peakTex))) {
                d.dev->CreateUnorderedAccessView(peakTex, nullptr, &peakUAV);
                d.dev->CreateShaderResourceView(peakTex, nullptr, &peakSRV);
            }
            CreatePeakRawTexture(d.dev, d.ic, &rawTex, &rawUAV);
        }
        ok = vs && ps && cb && smp[0] && smp[1] && smp[2] && noise && dg && pqO && pqE && sO && sE && wb && prev && cube &&
             reduceCS && smoothCS && peakCB && peakUAV && peakSRV && rawUAV;
    }
    ~OverlayRig() {
        Rel(vs); Rel(ps); Rel(cb); for (auto& s : smp) Rel(s);
        Rel(noise); Rel(dg); Rel(pqO); Rel(pqE); Rel(sO); Rel(sE); Rel(wb); Rel(prev); Rel(cube);
        Rel(reduceCS); Rel(smoothCS); Rel(peakCB); Rel(peakUAV); Rel(peakSRV); Rel(peakTex); Rel(rawUAV); Rel(rawTex);
    }
    void ResetPeak() { ResetPeakState(d.ic, peakUAV, rawUAV); }
    void Peak(ID3D11ShaderResourceView* frame, int W, int H) {
        DispatchPeakDetection(d.ic, reduceCS, smoothCS, peakCB, frame, peakUAV, rawUAV, (unsigned)W, (unsigned)H);
    }
    // One frame as RenderMonitor draws it. usePs: a variant's shader (null = the product's).
    void Draw(const OvCfg& c, ID3D11ShaderResourceView* capture, ID3D11RenderTargetView* target, int W, int H, ID3D11PixelShader* usePs = nullptr,
              bool clear = true) {
        float data[148];
        OvFillCb(data, c, lutN);
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(d.ic->Map(cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) { std::memcpy(m.pData, data, sizeof(data)); d.ic->Unmap(cb, 0); }
        if (c.dom == Dom::Hdr && c.tm && c.dyn) Peak(capture, W, H);
        D3D11_VIEWPORT vp = { 0, 0, (float)W, (float)H, 0, 1 };
        d.ic->RSSetViewports(1, &vp);
        d.ic->VSSetShader(vs, nullptr, 0);
        d.ic->IASetInputLayout(nullptr);
        d.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        const float zero[4] = { 0, 0, 0, 0 };
        if (clear) d.ic->ClearRenderTargetView(target, zero);
        d.ic->OMSetRenderTargets(1, &target, nullptr);
        d.ic->PSSetShader(usePs ? usePs : ps, nullptr, 0);
        d.ic->PSSetConstantBuffers(0, 1, &cb);
        ID3D11ShaderResourceView* srv[12] = { capture, c.lut ? cube : nullptr, noise, peakSRV, dg, pqO, pqE, sO, sE, nullptr, wb, prev };
        d.ic->PSSetShaderResources(0, 12, srv);
        d.ic->PSSetSamplers(0, 3, smp);
        d.ic->Draw(3, 0);
        ID3D11ShaderResourceView* none[12] = {};
        d.ic->PSSetShaderResources(0, 12, none);
        d.ic->OMSetRenderTargets(0, nullptr, nullptr);
    }
};

std::vector<int> ResList(const std::string& env, const char* dflt) {
    std::vector<int> v;
    std::stringstream ss(env.empty() ? dflt : env);
    std::string tok;
    while (std::getline(ss, tok, ',')) { const int h = atoi(tok.c_str()); if (h >= 120) v.push_back(h); }
    return v;
}
int WidthOf(int h) { return h == 1440 ? 2560 : h == 2160 ? 3840 : h == 1080 ? 1920 : (h * 16 / 9) & ~1; }

std::string StatCols(const perf::Stats& s) {
    char b[160];
    snprintf(b, sizeof(b), "%.1f\t%.1f\t%.1f\t%.1f", s.med, s.p10, s.p90, s.mn);
    return b;
}

}  // namespace

// =================================================================================================================
// 1. The static cost table
// =================================================================================================================
TEST_CASE("perf: static shader cost table (DLUT_PERF_STATIC)") {
    const std::string out = perf::Env("DLUT_PERF_STATIC");
    if (out.empty()) return;
    CreateDirectoryA(out.c_str(), nullptr);
    std::vector<std::string> rows;
    int failed = 0;
    auto add = [&](const std::string& name, const char* target, const perf::ShaderCost& c) {
        if (!c.ok) { failed++; MESSAGE(name << ": " << c.error); return; }
        rows.push_back(perf::CostRow(name, target, c));
        std::string file = name;
        for (char& ch : file) if (ch == '@' || ch == '+' || ch == '/') ch = '_';
        perf::SaveBytes(out + "\\" + file + ".asm", c.asmText.data(), c.asmText.size());
    };

    // --- the DWM hook's pass (hook_render.cpp InitializeStuff) ---
    // D3DCompile time by group (wall, this thread; includes the reflection + disassembly, a few percent): what the hook's
    // InitializeStuff spends inside the first hooked present and the overlay's InitD3D at start.
    double tHook = perf::QpcUs();
    add("hook.vs", "vs_5_0", perf::CostOf(g_shaders, sizeof g_shaders, "VS", "vs_5_0"));
    add("hook.ps", "ps_5_0", perf::CostOf(g_shaders, sizeof g_shaders, "PS", "ps_5_0"));
    tHook = perf::QpcUs() - tHook;
    {
        std::map<std::string, bool> seen;
        for (const HookCfg& c : HookConfigs()) {
            const HookSwitches sw = SwitchesOf(c);
            const std::string tag = SwitchTag(sw);
            if (seen[tag]) continue;
            seen[tag] = true;
            for (const PsVariant& v : kPsVariants) {
                if (!v.fold) continue;
                std::string err;
                const std::string text = HookPsText(v, sw, &err);
                CHECK_MESSAGE(err.empty(), "hook variant " << v.id << ": " << err);
                if (v.lutLoad && !sw.hasLut) continue;
                add("hook.ps@" + tag + (v.id == "spec" ? "" : "+" + v.id.substr(5)), "ps_5_0", perf::CostOf(text.c_str(), text.size(), "PS", "ps_5_0"));
            }
        }
        std::string err;
        const std::string sl = HookPsText(kPsVariants[1], HookSwitches{}, &err);
        CHECK_MESSAGE(err.empty(), "hook variant sl: " << err);
        add("hook.ps+sl", "ps_5_0", perf::CostOf(sl.c_str(), sl.size(), "PS", "ps_5_0"));
    }
    // --- dynamic peak detection (both paths) and the overlay (src/gpu.cpp InitD3D) ---
    double tPeak = perf::QpcUs();
    add("peak.reduce.cs", "cs_5_0", perf::CostOf(g_peakReduceCSSource, strlen(g_peakReduceCSSource), "main", "cs_5_0"));
    add("peak.smooth.cs", "cs_5_0", perf::CostOf(g_peakSmoothCSSource, strlen(g_peakSmoothCSSource), "main", "cs_5_0"));
    tPeak = perf::QpcUs() - tPeak;
    double tOverlay = perf::QpcUs();
    add("overlay.vs", "vs_5_0", perf::CostOf(g_vsSource, strlen(g_vsSource), "main", "vs_5_0"));
    add("overlay.ps", "ps_5_0", perf::CostOf(g_psSource, strlen(g_psSource), "main", "ps_5_0"));
    add("overlay.analysis.cs", "cs_5_0", perf::CostOf(g_analysisCSSource, strlen(g_analysisCSSource), "main", "cs_5_0"));
    tOverlay = perf::QpcUs() - tOverlay;
    for (const OvCfg& c : OverlayConfigs()) {
        std::string err;
        const std::string text = OvPsText(c, true, &err);
        CHECK_MESSAGE(err.empty(), "overlay variant " << c.id << ": " << err);
        add("overlay.ps@" + c.id, "ps_5_0", perf::CostOf(text.c_str(), text.size(), "main", "ps_5_0"));
    }
    // --- the FALD layer's passes that shared/fald_shader.h exposes, as hook_fald.cpp FaldInitShaders compiles them (the
    //     always-on core's own lattice kernels are file-static in shared/fald_core2.cpp and not in this table) ---
    {
        const std::string common = g_faldCommonSource, combine = std::string(g_faldZoneCombineDefine) + common;
        struct E { const char* name; std::string text; const char* target; };
        const std::vector<E> fald = {
            { "fald.stat.cs", common + g_faldStatSource, "cs_5_0" },
            { "fald.stat_combine.cs", combine + g_faldStatSource, "cs_5_0" },
            { "fald.conv_so1.cs", std::string(g_faldConvSoDefine[0]) + common + g_faldConvSource, "cs_5_0" },
            { "fald.conv_so2.cs", std::string(g_faldConvSoDefine[1]) + common + g_faldConvSource, "cs_5_0" },
            { "fald.conv_so4.cs", std::string(g_faldConvSoDefine[2]) + common + g_faldConvSource, "cs_5_0" },
            { "fald.conv_so8.cs", std::string(g_faldConvSoDefine[3]) + common + g_faldConvSource, "cs_5_0" },
            { "fald.gain.cs", common + g_faldGainSource, "cs_5_0" },
            { "fald.blur.cs", common + g_faldBlurSource, "cs_5_0" },
            { "fald.gain_blur.cs", common + g_faldGainBlurSource, "cs_5_0" },
            { "fald.decide.cs", common + g_faldDecideSource, "cs_5_0" },
            { "fald.pixel.ps", common + g_faldPixelSource, "ps_5_0" },
            { "fald.fullscreen.vs", std::string(g_faldFullscreenVsSource), "vs_5_0" },
            { "fald.star_stat.cs", common + g_faldStarStatSource, "cs_5_0" },
            { "fald.star_weight.cs", common + g_faldStarWeightSource, "cs_5_0" },
            { "fald.star_plan.cs", common + g_faldStarPlanSource, "cs_5_0" },
            { "fald.panel_clock.cs", common + g_faldPanelClockSource, "cs_5_0" },
        };
        double tFald = perf::QpcUs();
        for (const E& e : fald) add(e.name, e.target, perf::CostOf(e.text.c_str(), e.text.size(), "main", e.target));
        tFald = perf::QpcUs() - tFald;
        MESSAGE("D3DCompile time: hook VS + PS " << tHook / 1000.0 << " ms, peak detection (2) " << tPeak / 1000.0 << " ms, overlay VS + PS + analysis "
                << tOverlay / 1000.0 << " ms, the FALD layer's " << fald.size() << " exposed shaders " << tFald / 1000.0 << " ms");
    }
    CHECK(failed == 0);

    std::string table = std::string(perf::CostHeader()) + "\n";
    for (const std::string& r : rows) table += r + "\n";
    CHECK(perf::SaveBytes(out + "\\static_costs.tsv", table.data(), table.size()));
    MESSAGE(rows.size() << " shaders -> " << out << "\\static_costs.tsv");

    const std::string ref = perf::Env("DLUT_PERF_STATIC_REF");
    if (!ref.empty()) {
        std::ifstream f(ref);
        REQUIRE_MESSAGE(f.good(), "no reference table " << ref);
        std::map<std::string, std::string> want;
        std::string line;
        while (std::getline(f, line)) { if (!line.empty() && line.back() == '\r') line.pop_back(); const size_t tab = line.find('\t'); if (tab != std::string::npos) want[line.substr(0, tab)] = line; }
        int same = 0, differ = 0, added = 0;
        for (const std::string& r : rows) {
            const std::string key = r.substr(0, r.find('\t'));
            auto w = want.find(key);
            if (w == want.end()) { added++; MESSAGE("NEW  " << r); }
            else if (w->second == r) same++;
            else { differ++; MESSAGE("WAS  " << w->second); MESSAGE("NOW  " << r); }
        }
        MESSAGE("static table vs " << ref << ": " << same << " unchanged, " << differ << " changed, " << added << " new (columns: " << std::string(perf::CostHeader()) << ")");
    }
}

// =================================================================================================================
// 2. GPU time
// =================================================================================================================
TEST_CASE("perf: GPU time of the hook's pass (the real RenderLUT) and the overlay's (DLUT_PERF_GPU)") {
    const std::string out = perf::Env("DLUT_PERF_GPU");
    if (out.empty()) return;
    CreateDirectoryA(out.c_str(), nullptr);
    const std::string adapter = perf::Env("DLUT_PERF_ADAPTER");
    const bool wantWarp = adapter == "WARP" || adapter == "warp";
    perf::BePolite(wantWarp ? 0x0FF00000ull : 0x0FFFFFFFull);      // WARP: eight CPUs
    perf::Dev dev(adapter);
    REQUIRE_MESSAGE(dev.ok, "no adapter matching '" << adapter << "'");
    MESSAGE("adapter: " << dev.name);
    const int N = std::max(3, perf::EnvInt("DLUT_PERF_N", dev.warp ? 5 : 40));
    const std::string only = perf::Env("DLUT_PERF_ONLY");
    const std::vector<int> resList = ResList(perf::Env("DLUT_PERF_RES"), dev.warp ? "1080" : "1080,1440,2160");
    std::vector<double> heats;
    {
        std::stringstream ss(perf::Env("DLUT_PERF_HEAT").empty() ? (dev.warp ? "0" : "12,0") : perf::Env("DLUT_PERF_HEAT"));
        std::string tok;
        while (std::getline(ss, tok, ',')) heats.push_back(atof(tok.c_str()));
    }
    const bool variants = perf::EnvInt("DLUT_PERF_VARIANTS", 1) != 0;
    const int lutN = std::clamp(perf::EnvInt("DLUT_PERF_LUT", 65), 2, 128);
    // DLUT_PERF_BATCH = K: every timed submission runs its pass K times back to back and reports the time per repetition.
    // K = 1 times one pass between two timestamps: on a pipelined GPU that is when its commands were ISSUED, and the tail of
    // the work can retire after the second timestamp. K = 8 or so keeps the pipeline full, so (last - first) / K is the
    // steady-state cost per pass: the number to quote as a share of a frame. (The repetitions re-process their own output:
    // the passes' cost does not depend on the pixel values.)
    const int batch = std::clamp(perf::EnvInt("DLUT_PERF_BATCH", 1), 1, 64);
    perf::Heater heater(dev);
    const double stopUs = dev.warp ? 30e6 : 45000.0;
    bool stop = false;
    struct TimerRes { TimerRes() { timeBeginPeriod(1); } ~TimerRes() { timeEndPeriod(1); } } timerRes;   // Sleep(2) = 2 ms

    // one timed series: `setup(i)` (not timed, flushed) then `run()` (timed)
    struct Series { perf::Stats gpu, cpu, wall; bool aborted = false; };
    auto series = [&](double heatMs, const std::function<void(int)>& setup, const std::function<void()>& run) {
        Series s;
        std::vector<double> g, c, w;
        for (int i = 0; i < N && !stop; i++) {
            setup(i);
            dev.ic->Flush();
            if (heatMs > 0.0 && !dev.warp) heater.Heat(dev, heatMs);
            double gu, cu, wu;
            dev.Time([&] { for (int k = 0; k < batch; k++) run(); }, gu, cu, wu);
            if ((gu > 0 ? gu : wu) > stopUs) { s.aborted = true; break; }
            g.push_back(gu > 0 ? gu / batch : gu); c.push_back(cu / batch); w.push_back(wu > 0 ? wu / batch : wu);
            if (!dev.warp) Sleep(2);                                // the desktop gets the GPU between runs
        }
        s.gpu = perf::MakeStats(g); s.cpu = perf::MakeStats(c); s.wall = perf::MakeStats(w);
        return s;
    };

    std::ofstream hookTsv(out + "\\hook_times.tsv"), ovTsv(out + "\\overlay_times.tsv"), primTsv(out + "\\primitives.tsv");
    hookTsv << "adapter\theat_ms\tcfg\tvariant\tres\tscenario\tdirty_px\tn\tgpu_us_med\tgpu_us_p10\tgpu_us_p90\tgpu_us_min\tcpu_us_med\tcpu_us_p10\tcpu_us_p90\tcpu_us_min\twall_us_med\tdiff_vs_base\tnote\n";
    ovTsv << "adapter\theat_ms\tcfg\tvariant\tres\tn\tgpu_us_med\tgpu_us_p10\tgpu_us_p90\tgpu_us_min\tcpu_us_med\tcpu_us_p10\tcpu_us_p90\tcpu_us_min\twall_us_med\tdiff_vs_base\n";
    primTsv << "adapter\theat_ms\tprimitive\tformat\tres\tn\tgpu_us_med\tgpu_us_p10\tgpu_us_p90\tgpu_us_min\tcpu_us_med\tcpu_us_p10\tcpu_us_p90\tcpu_us_min\n";

    // CPU only: the diagnostic string the Present hook builds on EVERY present before its log-once test
    // (dwm_hook/dllmain.cpp COverlayContext_Present_hook_24h2: a std::stringstream, nine insertions, destroyed unread).
    if (perf::Selected(only, "prim")) {
        std::vector<double> v;
        volatile long long sink = 0;
        for (int k = 0; k < 40; k++) {
            const double t0 = perf::QpcUs();
            for (int i = 0; i < 1000; i++) {
                std::stringstream m;
                m << "OverlaySwapChain address: 0x" << std::hex << (void*)&v << " -- windows 11 25h2: " << isWindows11_25h2
                  << " -- windows 11 24h2: " << isWindows11_24h2 << " -- " << "windows 11: " << isWindows11;
                sink = sink + (long long)m.tellp();
            }
            v.push_back(perf::QpcUs() - t0);
        }
        const perf::Stats s = perf::MakeStats(v);
        primTsv << dev.name << "\t0\tpresent_hook_stringstream_x1000\t-\t0\t" << s.n << "\t-1\t-1\t-1\t-1\t" << StatCols(s) << "\n";
        // The host's periodic work, one call each (read-only): the whitelist thread's process snapshot (src/whitelist.cpp,
        // every 500 ms in any running mode) and one DisplayConfig topology query (the 15 s tick and the 5 s health check
        // make several per monitor).
        std::vector<double> snap, qdc;
        for (int k = 0; k < 20; k++) {
            double t0 = perf::QpcUs();
            HANDLE h = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
            snap.push_back(perf::QpcUs() - t0);
            t0 = perf::QpcUs();
            UINT32 np = 0, nm = 0;
            if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &np, &nm) == ERROR_SUCCESS && np && nm) {
                std::vector<DISPLAYCONFIG_PATH_INFO> paths(np);
                std::vector<DISPLAYCONFIG_MODE_INFO> modes(nm);
                QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &np, paths.data(), &nm, modes.data(), nullptr);
            }
            qdc.push_back(perf::QpcUs() - t0);
            Sleep(20);
        }
        const perf::Stats ss = perf::MakeStats(snap), sq = perf::MakeStats(qdc);
        primTsv << dev.name << "\t0\thost_process_snapshot\t-\t0\t" << ss.n << "\t-1\t-1\t-1\t-1\t" << StatCols(ss) << "\n";
        primTsv << dev.name << "\t0\thost_query_display_config\t-\t0\t" << sq.n << "\t-1\t-1\t-1\t-1\t" << StatCols(sq) << "\n";
    }

    // ---------------- the hook ----------------
    if (perf::EnvInt("DLUT_PERF_HOOK", 1)) {
        HookRig rig(dev, lutN);
        REQUIRE_MESSAGE(rig.ok, "the hook's InitializeStuff failed (see %TEMP%\\DesktopLUT_dwmhook_test.log)");
        MESSAGE("hook rig: cube " << lutN << "^3, FALD panel file " << std::string(rig.panel ? "loaded" : "NOT available (FALD rows skipped)")
                << "; InitializeStuff (inside the first hooked present in the DLL) took " << rig.initUs / 1000.0 << " ms");
        primTsv << dev.name << "\t0\thook_initialize_stuff\t-\t0\t1\t-1\t-1\t-1\t-1\t" << rig.initUs << "\t" << rig.initUs << "\t" << rig.initUs << "\t" << rig.initUs << "\n";
        ID3D11PixelShader* const productPs = pixelShader;
        std::map<std::string, ID3D11PixelShader*> psCache;
        auto variantPs = [&](const PsVariant& v, const HookCfg& c) -> ID3D11PixelShader* {
            const std::string key = v.id + "/" + SwitchTag(SwitchesOf(c));
            auto f = psCache.find(key);
            if (f != psCache.end()) return f->second;
            std::string err;
            const std::string text = HookPsText(v, SwitchesOf(c), &err);
            ID3D11PixelShader* p = nullptr;
            ID3DBlob* b = nullptr;
            if (err.empty() && SUCCEEDED(D3DCompile(text.c_str(), text.size(), "PS", nullptr, nullptr, "PS", "ps_5_0", 0, 0, &b, nullptr))) {
                dev.dev->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &p);
                b->Release();
            }
            psCache[key] = p;
            return p;
        };
        struct LutVariant { const char* id; int n; DXGI_FORMAT fmt; };
        const LutVariant lutVariants[] = { { "lut17", 17, DXGI_FORMAT_R32G32B32A32_FLOAT }, { "lut33", 33, DXGI_FORMAT_R32G32B32A32_FLOAT },
                                           { "lut65", 65, DXGI_FORMAT_R32G32B32A32_FLOAT }, { "lut_fp16", lutN, DXGI_FORMAT_R16G16B16A16_FLOAT },
                                           { "lut_unorm16", lutN, DXGI_FORMAT_R16G16B16A16_UNORM } };

        for (int H : resList) {
            const int W = WidthOf(H);
            for (Dom dom : { Dom::Sdr8, Dom::Sdr10, Dom::Acm, Dom::Hdr }) {
                if (stop) break;
                bool any = false;
                for (const HookCfg& c : HookConfigs()) if (c.dom == dom && perf::Selected(only, c.id)) any = true;
                if (!any) continue;
                const DXGI_FORMAT fmt = perf::DomFormat(dom);
                const unsigned bpp = perf::DomBpp(dom);
                ID3D11Texture2D* back = perf::MakeTex(dev.dev, W, H, fmt, D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
                ID3D11Texture2D* content = perf::MakeTex(dev.dev, W, H, fmt, D3D11_BIND_SHADER_RESOURCE);
                REQUIRE((back && content));
                Kind loaded = Kind::Torture;
                std::map<int, std::vector<unsigned char>> frames;   // generated once per kind
                auto frameOf = [&](Kind k) -> const std::vector<unsigned char>& {
                    auto f = frames.find((int)k);
                    if (f == frames.end()) f = frames.emplace((int)k, perf::MakeFrame(k, dom, W, H)).first;
                    return f->second;
                };
                std::vector<Scenario> scen = Scenarios(W, H);
                std::vector<RECT> full = { RECT{ 0, 0, W, H } };
                std::map<std::string, std::vector<unsigned char>> baseOut;

                // primitives of this format: the bare full-frame copy, the peak detector, an RTV create + release
                if (perf::Selected(only, "prim")) {
                    dev.ic->UpdateSubresource(content, 0, nullptr, frameOf(Kind::Video).data(), (UINT)W * bpp, 0);
                    loaded = Kind::Video;
                    ID3D11Texture2D* scratch = perf::MakeTex(dev.dev, W, H, fmt, D3D11_BIND_SHADER_RESOURCE);
                    for (double heat : heats) {
                        Series s = series(heat, [](int) {}, [&] { dev.ic->CopyResource(scratch, content); });
                        primTsv << dev.name << "\t" << heat << "\tcopy_full\t" << perf::DomName(dom) << "\t" << H << "\t" << s.gpu.n << "\t" << StatCols(s.gpu) << "\t" << StatCols(s.cpu) << "\n";
                        s = series(heat, [](int) {}, [&] {
                            for (int i = 0; i < 50; i++) { ID3D11RenderTargetView* v = nullptr; dev.dev->CreateRenderTargetView(back, nullptr, &v); if (v) v->Release(); }
                        });
                        primTsv << dev.name << "\t" << heat << "\trtv_create_release_x50\t" << perf::DomName(dom) << "\t" << H << "\t" << s.cpu.n << "\t-1\t-1\t-1\t-1\t" << StatCols(s.cpu) << "\n";
                    }
                    Rel(scratch);
                    primTsv.flush();
                }

                for (const HookCfg& c : HookConfigs()) {
                    if (c.dom != dom || stop) continue;
                    if (c.fald && !rig.FaldAvailable(W, H)) continue;
                    if (c.fald && rig.faldInitUs > 0.0) {
                        MESSAGE("FaldInitShaders (inside the first present of a monitor that needs the layer) took " << rig.faldInitUs / 1000.0 << " ms");
                        primTsv << dev.name << "\t0\thook_fald_init_shaders\t-\t0\t1\t-1\t-1\t-1\t-1\t" << rig.faldInitUs << "\t" << rig.faldInitUs << "\t"
                                << rig.faldInitUs << "\t" << rig.faldInitUs << "\n";
                        rig.faldInitUs = 0.0;
                    }
                    rig.Apply(c, W, H);
                    struct Run { std::string variant; ID3D11PixelShader* ps; const LutVariant* lv; };
                    std::vector<Run> runs = { { "base", nullptr, nullptr } };
                    if (variants && !c.fald) {
                        for (const PsVariant& v : kPsVariants) {
                            if (v.pqLut && !(c.dom == Dom::Hdr)) continue;
                            if (v.lutLoad && !c.lut) continue;
                            ID3D11PixelShader* p = variantPs(v, c);
                            CHECK_MESSAGE(p != nullptr, "variant " << v.id << " of " << c.id << " did not compile");
                            if (p) runs.push_back({ v.id, p, nullptr });
                        }
                        if (c.id == "hdr_lut" || c.id == "sdr8_lut")
                            for (const LutVariant& lv : lutVariants) runs.push_back({ lv.id, nullptr, &lv });
                    }
                    for (const Run& run : runs) {
                        lutData* L = nullptr; ID3D11ShaderResourceView* savedView = nullptr; int savedSize = 0; ID3D11ShaderResourceView* mine = nullptr;
                        if (run.lv) {
                            for (int k = 0; k < numLuts; k++) if (luts[k].isHdr == (c.dom == Dom::Hdr)) L = &luts[k];
                            mine = MakeCubeSrv(dev.dev, run.lv->n, run.lv->fmt);
                            if (!L || !mine) { Rel(mine); continue; }
                            savedView = L->textureView; savedSize = L->size;
                            L->textureView = mine; L->size = run.lv->n;
                        }
                        pixelShader = run.ps ? run.ps : productPs;
                        for (const Scenario& sc : scen) {
                            if (stop) break;
                            if (run.variant != "base" && sc.id != "full") continue;
                            const std::string id = c.id + "/" + run.variant + "/" + sc.id;
                            if (!perf::Selected(only, id)) continue;
                            if (loaded != sc.kind) {
                                dev.ic->UpdateSubresource(content, 0, nullptr, frameOf(sc.kind).data(), (UINT)W * bpp, 0);
                                loaded = sc.kind;
                            }
                            std::vector<RECT> rects = sc.rects;
                            // prime: two full-frame presents (the peak source copy, the FALD layer's clean copy), then the scenario's
                            bool ran = true;
                            for (int i = 0; i < 2; i++) { Compose(dev.ic, back, content, full, W, H, 0); ran = rig.Present(back, full) && ran; dev.ic->Flush(); }
                            for (int i = 0; i < 4; i++) { Compose(dev.ic, back, content, rects, W, H, i); ran = rig.Present(back, rects) && ran; dev.ic->Flush(); }
                            for (double heat : heats) {
                                if (run.variant != "base" && heat != heats.front()) continue;
                                bool ok = true;
                                Series s = series(heat, [&](int i) { Compose(dev.ic, back, content, rects, W, H, i); },
                                                  [&] { ok = rig.Present(back, rects) && ok; });
                                std::string diff = "-";
                                if (sc.id == "full" && !c.dyn && !c.fald && heat == heats.front()) {
                                    Compose(dev.ic, back, content, full, W, H, 0);
                                    rig.Present(back, full);
                                    std::vector<unsigned char> px = perf::ReadBack(dev.dev, dev.ic, back);
                                    if (run.variant == "base") baseOut[c.id] = std::move(px);
                                    else if (baseOut.count(c.id)) {
                                        const perf::Diff d = perf::Compare(baseOut[c.id], px, fmt);
                                        char b[128];
                                        snprintf(b, sizeof(b), "%zu/%zu max %.3g rel %.3g", d.differing, d.total, d.maxAbs, d.maxRel);
                                        diff = d.differing ? b : "identical";
                                    }
                                }
                                hookTsv << dev.name << "\t" << heat << "\t" << c.id << "\t" << run.variant << "\t" << H << "\t" << sc.id << "\t" << (long long)DirtyPx(rects)
                                        << "\t" << s.gpu.n << "\t" << StatCols(s.gpu) << "\t" << StatCols(s.cpu) << "\t" << s.wall.med << "\t" << diff << "\t"
                                        << (ok && ran ? "" : "PASS_DID_NOT_RUN ") << (s.aborted ? "ABORTED_OVER_LIMIT" : "") << "\n";
                                hookTsv.flush();
                                if (s.aborted && !dev.warp) { MESSAGE(id << ": a run over 45 ms - stopping"); stop = true; }
                            }
                        }
                        pixelShader = productPs;
                        if (run.lv && L) { L->textureView = savedView; L->size = savedSize; Rel(mine); }
                    }
                }
                Rel(content); Rel(back);
                dev.ic->ClearState(); dev.ic->Flush();
            }
        }
        pixelShader = productPs;
        for (auto& kv : psCache) Rel(kv.second);
    }

    // ---------------- the overlay ----------------
    if (perf::EnvInt("DLUT_PERF_OVERLAY", 1) && !stop) {
        OverlayRig ov(dev, std::min(lutN, 65));
        REQUIRE_MESSAGE(ov.ok, "overlay rig: " << ov.error);
        for (int H : resList) {
            const int W = WidthOf(H);
            for (Dom dom : { Dom::Sdr8, Dom::Acm, Dom::Hdr }) {
                if (stop) break;
                bool any = false;
                for (const OvCfg& c : OverlayConfigs()) if (c.dom == dom && perf::Selected(only, "overlay/" + c.id)) any = true;
                if (!any) continue;
                const DXGI_FORMAT capFmt = perf::DomFormat(dom), tgtFmt = dom == Dom::Sdr8 ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R16G16B16A16_FLOAT;
                const std::vector<unsigned char> px = perf::MakeFrame(Kind::Mix, dom, W, H);
                ID3D11Texture2D* cap = perf::MakeTex(dev.dev, W, H, capFmt, D3D11_BIND_SHADER_RESOURCE, px.data(), (UINT)W * perf::DomBpp(dom));
                ID3D11Texture2D* tgt = perf::MakeTex(dev.dev, W, H, tgtFmt, D3D11_BIND_RENDER_TARGET);
                REQUIRE((cap && tgt));
                ID3D11ShaderResourceView* capSrv = nullptr; ID3D11RenderTargetView* rtv = nullptr;
                dev.dev->CreateShaderResourceView(cap, nullptr, &capSrv);
                dev.dev->CreateRenderTargetView(tgt, nullptr, &rtv);
                if (dom == Dom::Hdr && perf::Selected(only, "prim"))
                    for (double heat : heats) {
                        Series s = series(heat, [](int) {}, [&] { ov.Peak(capSrv, W, H); });
                        primTsv << dev.name << "\t" << heat << "\tpeak_detect\thdr\t" << H << "\t" << s.gpu.n << "\t" << StatCols(s.gpu) << "\t" << StatCols(s.cpu) << "\n";
                        primTsv.flush();
                    }
                for (const OvCfg& c : OverlayConfigs()) {
                    if (c.dom != dom || stop || !perf::Selected(only, "overlay/" + c.id)) continue;
                    std::string err;
                    ID3D11PixelShader* spec = variants ? ov.CompilePs(OvPsText(c, true, &err)) : nullptr;
                    CHECK_MESSAGE((!variants || (spec && err.empty())), "overlay spec variant of " << c.id << ": " << err << ov.error);
                    struct Run { const char* variant; ID3D11PixelShader* ps; bool clear; };
                    std::vector<Run> runs = { { "base", nullptr, true } };
                    if (variants) { runs.push_back({ "noclear", nullptr, false }); if (spec) runs.push_back({ "spec", spec, true }); }
                    std::vector<unsigned char> base;
                    for (const Run& run : runs) {
                        ov.ResetPeak();
                        for (int i = 0; i < 4; i++) { ov.Draw(c, capSrv, rtv, W, H, run.ps, run.clear); dev.ic->Flush(); }
                        for (double heat : heats) {
                            if (std::string(run.variant) != "base" && heat != heats.front()) continue;
                            Series s = series(heat, [](int) {}, [&] { ov.Draw(c, capSrv, rtv, W, H, run.ps, run.clear); });
                            std::string diff = "-";
                            if (!c.dyn && heat == heats.front()) {
                                std::vector<unsigned char> o = perf::ReadBack(dev.dev, dev.ic, tgt);
                                if (std::string(run.variant) == "base") base = std::move(o);
                                else {
                                    const perf::Diff d = perf::Compare(base, o, tgtFmt);
                                    char b[128];
                                    snprintf(b, sizeof(b), "%zu/%zu max %.3g rel %.3g", d.differing, d.total, d.maxAbs, d.maxRel);
                                    diff = d.differing ? b : "identical";
                                }
                            }
                            ovTsv << dev.name << "\t" << heat << "\t" << c.id << "\t" << run.variant << "\t" << H << "\t" << s.gpu.n << "\t" << StatCols(s.gpu) << "\t"
                                  << StatCols(s.cpu) << "\t" << s.wall.med << "\t" << diff << "\n";
                            ovTsv.flush();
                            if (s.aborted && !dev.warp) { MESSAGE("overlay/" << c.id << ": a run over 45 ms - stopping"); stop = true; }
                        }
                    }
                    Rel(spec);
                }
                Rel(capSrv); Rel(rtv); Rel(cap); Rel(tgt);
                dev.ic->ClearState(); dev.ic->Flush();
            }
        }
    }
    CHECK_FALSE(stop);
}

// =================================================================================================================
// 3. The bit-identity gate
// =================================================================================================================
TEST_CASE("perf: output reference manifest (DLUT_PERF_HASH_WRITE / DLUT_PERF_HASH_REF)") {
    const std::string writePath = perf::Env("DLUT_PERF_HASH_WRITE"), refPath = perf::Env("DLUT_PERF_HASH_REF");
    if (writePath.empty() && refPath.empty()) return;
    const std::string refDir = perf::Env("DLUT_PERF_REF_DIR"), only = perf::Env("DLUT_PERF_ONLY");
    if (!refDir.empty()) CreateDirectoryA(refDir.c_str(), nullptr);
    std::string adapter = perf::Env("DLUT_PERF_HASH_ADAPTER");
    if (adapter.empty()) adapter = "WARP";
    const bool wantWarp = adapter == "WARP" || adapter == "warp";
    perf::BePolite(wantWarp ? 0x0FF00000ull : 0x0FFFFFFFull);
    perf::Dev dev(adapter);
    REQUIRE_MESSAGE(dev.ok, "no adapter matching '" << adapter << "'");
    const int lutN = std::clamp(perf::EnvInt("DLUT_PERF_LUT", 33), 2, 128);
    const int H = std::max(120, perf::EnvInt("DLUT_PERF_HASH_RES", 540)), W = WidthOf(H);

    std::map<std::string, std::string> want;
    if (!refPath.empty()) {
        std::ifstream f(refPath);
        REQUIRE_MESSAGE(f.good(), "no manifest " << refPath);
        std::string line;
        while (std::getline(f, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            const size_t sp = line.find(' ');
            if (sp != std::string::npos && line[0] != '#') want[line.substr(0, sp)] = line.substr(sp + 1);
        }
    }
    std::vector<std::string> lines;
    int same = 0, differ = 0, missing = 0;
    auto record = [&](const std::string& key, const std::vector<unsigned char>& px, DXGI_FORMAT fmt) {
        char b[64];
        snprintf(b, sizeof(b), "%zu %016llx", px.size(), (unsigned long long)perf::Fnv(px.data(), px.size()));
        lines.push_back(key + " " + b);
        std::string file = key;
        for (char& ch : file) if (ch == '/') ch = '_';
        if (!writePath.empty() && !refDir.empty()) perf::SaveBytes(refDir + "\\" + file + ".bin", px.data(), px.size());
        if (refPath.empty()) return;
        auto w = want.find(key);
        if (w == want.end()) { missing++; MESSAGE("not in the manifest: " << key); return; }
        if (w->second == b) { same++; return; }
        differ++;
        std::vector<unsigned char> ref;
        if (!refDir.empty() && perf::LoadBytes(refDir + "\\" + file + ".bin", ref)) {
            const perf::Diff d = perf::Compare(ref, px, fmt);
            if (d.sizeMismatch) { FAIL_CHECK(key << ": DIFFERS (size)"); return; }
            FAIL_CHECK(key << ": DIFFERS in " << d.differing << " of " << d.total << " components, max abs " << d.maxAbs
                           << (fmt == DXGI_FORMAT_R16G16B16A16_FLOAT ? " (scRGB units; max rel " : " codes (") << d.maxRel << ")");
        } else FAIL_CHECK(key << ": DIFFERS (no stored frame to measure against; set DLUT_PERF_REF_DIR)");
    };

    // the frames themselves (a manifest only means something against the frames it was recorded with)
    {
        uint64_t h = 1469598103934665603ull;
        for (Dom dom : { Dom::Sdr8, Dom::Sdr10, Dom::Acm, Dom::Hdr }) {
            const std::vector<unsigned char> px = perf::MakeFrame(Kind::Torture, dom, W, H);
            h = perf::Fnv(px.data(), px.size(), h);
        }
        char b[64];
        snprintf(b, sizeof(b), "%d %016llx", W * H, (unsigned long long)h);
        lines.push_back(std::string("frames/") + std::to_string(H) + " " + b);
        if (!refPath.empty()) {
            auto w = want.find(std::string("frames/") + std::to_string(H));
            REQUIRE_MESSAGE((w != want.end() && w->second == b), "the synthetic frames differ from the manifest's (another toolset / CRT?): re-record it");
        }
    }

    // ---- the hook ----
    std::unique_ptr<HookRig> rigPtr;
    bool rigStateful = false;
    for (const HookCfg& c : HookConfigs()) {
        if (!perf::Selected(only, "hook/" + c.id)) continue;
        // a rig of its own for every stateful configuration (and after one): the peak detector's history and the FALD
        // layer's state start fresh, so a line depends on its own configuration only
        const bool stateful = c.dyn || c.fald;
        if (!rigPtr || stateful || rigStateful) { rigPtr.reset(); rigPtr = std::make_unique<HookRig>(dev, lutN); }
        rigStateful = stateful;
        HookRig& rig = *rigPtr;
        REQUIRE_MESSAGE(rig.ok, "the hook's InitializeStuff failed");
        int w = W, h = H;
        if (c.fald) {
            w = 3840; h = 2160;                                  // the panel file's lattice
            if (!rig.panel) { MESSAGE("hook/" << c.id << ": no FALD panel file - skipped"); continue; }   // (the shaders: RenderLUT asks for them, as in the DLL)
        }
        rig.Apply(c, w, h);
        const DXGI_FORMAT fmt = perf::DomFormat(c.dom);
        ID3D11Texture2D* back = perf::MakeTex(dev.dev, w, h, fmt, D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
        const std::vector<unsigned char> px = perf::MakeFrame(Kind::Torture, c.dom, w, h);
        ID3D11Texture2D* content = perf::MakeTex(dev.dev, w, h, fmt, D3D11_BIND_SHADER_RESOURCE, px.data(), (UINT)w * perf::DomBpp(c.dom));
        REQUIRE((back && content));
        std::vector<RECT> full = { RECT{ 0, 0, w, h } };
        std::vector<RECT> part = Scenarios(w, h)[4].rects;       // the eight tiles
        part.push_back(RECT{ w / 4, h / 4, w / 4 + w / 3, h / 4 + h / 5 });
        // full, tiles (shifted content), full shifted, then two more for the stateful ones (the peak's smoothing, FALD)
        const int presents = (c.dyn || c.fald) ? 5 : 3;
        for (int p = 0; p < presents; p++) {
            std::vector<RECT>& rects = (p == 1) ? part : full;
            Compose(dev.ic, back, content, rects, w, h, p == 0 ? 0 : (p == 3 ? 0 : 1));
            const bool ran = rig.Present(back, rects);
            CHECK_MESSAGE(ran, "hook/" << c.id << " present " << p << ": the pass did not run");
            dev.ic->Flush();
            record("hook/" + c.id + "/" + std::to_string(h) + "/p" + std::to_string(p), perf::ReadBack(dev.dev, dev.ic, back), fmt);
        }
        Rel(content); Rel(back);
        dev.ic->ClearState(); dev.ic->Flush();
    }
    rigPtr.reset();

    // ---- the overlay ----
    {
        OverlayRig ov(dev, lutN);
        REQUIRE_MESSAGE(ov.ok, "overlay rig: " << ov.error);
        for (const OvCfg& c : OverlayConfigs()) {
            if (!perf::Selected(only, "overlay/" + c.id)) continue;
            const DXGI_FORMAT capFmt = perf::DomFormat(c.dom), tgtFmt = c.dom == Dom::Sdr8 ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R16G16B16A16_FLOAT;
            const std::vector<unsigned char> px = perf::MakeFrame(Kind::Torture, c.dom, W, H);
            ID3D11Texture2D* cap = perf::MakeTex(dev.dev, W, H, capFmt, D3D11_BIND_SHADER_RESOURCE, px.data(), (UINT)W * perf::DomBpp(c.dom));
            ID3D11Texture2D* tgt = perf::MakeTex(dev.dev, W, H, tgtFmt, D3D11_BIND_RENDER_TARGET);
            REQUIRE((cap && tgt));
            ID3D11ShaderResourceView* capSrv = nullptr; ID3D11RenderTargetView* rtv = nullptr;
            dev.dev->CreateShaderResourceView(cap, nullptr, &capSrv);
            dev.dev->CreateRenderTargetView(tgt, nullptr, &rtv);
            ov.ResetPeak();
            for (int p = 0; p < (c.dyn ? 3 : 1); p++) {
                ov.Draw(c, capSrv, rtv, W, H);
                dev.ic->Flush();
                record("overlay/" + c.id + "/" + std::to_string(H) + "/p" + std::to_string(p), perf::ReadBack(dev.dev, dev.ic, tgt), tgtFmt);
            }
            Rel(capSrv); Rel(rtv); Rel(cap); Rel(tgt);
        }
    }

    if (!writePath.empty()) {
        std::string text = "# DesktopLUT output reference manifest: adapter " + dev.name + ", cube " + std::to_string(lutN) + "^3 (tests/test_perf.cpp)\n";
        for (const std::string& l : lines) text += l + "\n";
        CHECK(perf::SaveBytes(writePath, text.data(), text.size()));
        MESSAGE(lines.size() << " lines -> " << writePath);
    }
    if (!refPath.empty()) {
        MESSAGE("against " << refPath << ": " << same << " identical, " << differ << " differ, " << missing << " not in the manifest");
        CHECK(missing == 0);
    }
}

// =================================================================================================================
// Always-on cases (WARP): the hook pass's safety and correctness rules that the instruments' rig can drive
// =================================================================================================================

// A device on which the pass's resources cannot be created (feature level 10: no vs_5_0 / ps_5_0). The old code retried
// InitializeStuff on every present and, with a tone map configured, went on into RenderLUT with a null device (an access
// violation inside dwm.exe: the DLL is not built with /EHa).
TEST_CASE("hook: a failed InitializeStuff is final for the injection and every apply path is a clean no-op (WARP, feature level 10)") {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_10_0;
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) {
        MESSAGE("skipped: no WARP device");
        return;
    }
    UninitializeStuff();
    std::memset(textureDesc, 0, sizeof(D3D11_TEXTURE2D_DESC) * 2);
    ResetContextRouting();
    HookTestResetInitState();
    const bool prev25h2 = isWindows11_25h2;
    isWindows11_25h2 = true;
    g_numMonitorHdrStates = 1;
    g_monitorHdrStates[0] = { 0, 0, true, 10u, 64u, 64u };
    s_perfTonemap = {};
    s_perfTonemap.enabled = true;                       // a tone map: the pass has work, so the old code reached the device
    s_perfTonemap.sourcePeakNits = 4000.0f; s_perfTonemap.targetPeakNits = 1000.0f;
    s_perfTonemap.pqSourcePeak = LinearToPQ(0.4f); s_perfTonemap.pqTargetPeak = LinearToPQ(0.1f);
    g_numLocalFald = 0;
    ID3D11Texture2D* back = perf::MakeTex(dev, 64, 64, DXGI_FORMAT_R16G16B16A16_FLOAT, D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
    REQUIRE(back != nullptr);
    RECT full = { 0, 0, 64, 64 };
    int ctx[4] = {};
    for (int i = 0; i < 5; i++) CHECK_FALSE(ApplyLUTDirect(ctx, back, &full, 1));
    CHECK(device == nullptr);
    CHECK(HookRenderInitFailed());
    CHECK(HookTestInitAttempts() == 1);                 // one attempt, not one per present
    CHECK_FALSE(RenderLUT(ctx, back, &full, 1));        // and the pass itself never touches a null device
    HookTestResetInitState();
    UninitializeStuff();
    ResetContextRouting();
    g_numMonitorHdrStates = 0;
    s_perfTonemap = {};
    isWindows11_25h2 = prev25h2;
    back->Release();
    ic->ClearState(); ic->Flush(); ic->Release(); dev->Release();
}

// The dynamic tone map is a function of the whole frame's (smoothed) peak, and the smoothing advances on EVERY present. A
// scene cut followed by presents that only carry small dirty rects (a caret, a clock) must leave the frame as if every one
// of those presents had redrawn all of it: the part outside the rects cannot keep the tone map of an older peak.
TEST_CASE("hook: with a dynamic peak the whole frame follows the peak, not only the present's dirty rects (WARP)") {
    perf::Dev dev("WARP");
    if (!dev.ok) { MESSAGE("skipped: no WARP device"); return; }
    const int W = 480, H = 270;
    const std::vector<unsigned char> bright = perf::MakeFrame(Kind::Video, Dom::Hdr, W, H);
    std::vector<unsigned char> dark = bright;           // the same picture at 2 % (peak ~80 nits)
    for (size_t i = 0; i + 7 < dark.size(); i += 8)
        for (int c = 0; c < 3; c++) {
            uint16_t h; std::memcpy(&h, &dark[i + (size_t)c * 2], 2);
            h = perf::FloatToHalf(perf::HalfToFloat(h) * 0.02f);
            std::memcpy(&dark[i + (size_t)c * 2], &h, 2);
        }
    auto sequence = [&](bool everyPresentFull) {
        std::vector<unsigned char> out;
        HookRig rig(dev, 17);
        REQUIRE(rig.ok);
        const HookCfg c{ "hdr_tm_dyn", Dom::Hdr, false, true, 0, true, false, false };
        rig.Apply(c, W, H);
        const DXGI_FORMAT fmt = DXGI_FORMAT_R16G16B16A16_FLOAT;
        ID3D11Texture2D* back = perf::MakeTex(dev.dev, W, H, fmt, D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE);
        ID3D11Texture2D* tb = perf::MakeTex(dev.dev, W, H, fmt, D3D11_BIND_SHADER_RESOURCE, bright.data(), (UINT)W * 8u);
        ID3D11Texture2D* td = perf::MakeTex(dev.dev, W, H, fmt, D3D11_BIND_SHADER_RESOURCE, dark.data(), (UINT)W * 8u);
        REQUIRE((back && tb && td));
        std::vector<RECT> full = { RECT{ 0, 0, W, H } };
        std::vector<RECT> caret = { RECT{ W / 5, H * 3 / 10, W / 5 + 2, H * 3 / 10 + 18 }, RECT{ W - 90, H - 40, W - 10, H - 8 } };
        bool ran = true;
        for (int i = 0; i < 3; i++) { Compose(dev.ic, back, td, full, W, H, 0); ran = rig.Present(back, full) && ran; }   // a dark scene, settled
        Compose(dev.ic, back, tb, full, W, H, 0); ran = rig.Present(back, full) && ran;                                    // the cut
        for (int i = 0; i < 45; i++) {                                                                                     // the peak climbs
            std::vector<RECT>& rects = everyPresentFull ? full : caret;
            Compose(dev.ic, back, tb, rects, W, H, 0);
            ran = rig.Present(back, rects) && ran;
        }
        CHECK(ran);
        dev.ic->Flush();
        out = perf::ReadBack(dev.dev, dev.ic, back);
        Rel(td); Rel(tb); Rel(back);
        dev.ic->ClearState(); dev.ic->Flush();
        return out;
    };
    const std::vector<unsigned char> partial = sequence(false), reference = sequence(true);
    const perf::Diff d = perf::Compare(reference, partial, DXGI_FORMAT_R16G16B16A16_FLOAT);
    MESSAGE("small-rect presents vs full redraws after a cut: " << d.differing << " of " << d.total << " components differ, max "
            << d.maxAbs << " scRGB (" << d.maxAbs * 80.0 << " nits), max relative " << d.maxRel);
    CHECK(d.differing == 0);
}

// The hook creates its own shaders from dwm_hook/hook_shader_bytecode.h (nothing is compiled inside DWM's present). That
// header must be exactly what D3DCompile makes of the current shader text with the calls the DLL used to make - a source
// edit without regenerating it fails here.
// Regenerate (then rebuild):  DLUT_HOOK_BYTECODE_WRITE=1 bin/Test/DesktopLUT.Tests.exe -tc="hook: the shipped shader bytecode*"
namespace {
std::string CompilerVersion() {
    char path[MAX_PATH] = {};
    HMODULE m = GetModuleHandleA("D3DCompiler_47.dll");
    if (!m || !GetModuleFileNameA(m, path, MAX_PATH)) return "unknown";
    DWORD dummy = 0;
    const DWORD n = GetFileVersionInfoSizeA(path, &dummy);
    if (!n) return "unknown";
    std::vector<unsigned char> buf(n);
    VS_FIXEDFILEINFO* fi = nullptr; UINT len = 0;
    if (!GetFileVersionInfoA(path, 0, n, buf.data()) || !VerQueryValueA(buf.data(), "\\", (void**)&fi, &len) || !fi) return "unknown";
    char v[64];
    snprintf(v, sizeof(v), "%u.%u.%u.%u", HIWORD(fi->dwFileVersionMS), LOWORD(fi->dwFileVersionMS), HIWORD(fi->dwFileVersionLS), LOWORD(fi->dwFileVersionLS));
    return v;
}
}  // namespace

TEST_CASE("hook: the shipped shader bytecode is D3DCompile of the current source") {
    struct Item { const char* array; const char* src; size_t len; const char* name; const char* entry; const char* target; const unsigned char* have; size_t haveLen; };
    const Item items[] = {   // the calls hook_render.cpp InitializeStuff made
        { "g_hookVsBytecode", g_shaders, sizeof g_shaders, nullptr, "VS", "vs_5_0", g_hookVsBytecode, sizeof(g_hookVsBytecode) },
        { "g_hookPsBytecode", g_shaders, sizeof g_shaders, nullptr, "PS", "ps_5_0", g_hookPsBytecode, sizeof(g_hookPsBytecode) },
        { "g_hookPeakReduceCsBytecode", g_peakReduceCSSource, strlen(g_peakReduceCSSource), "PeakReduceCS", "main", "cs_5_0", g_hookPeakReduceCsBytecode, sizeof(g_hookPeakReduceCsBytecode) },
        { "g_hookPeakSmoothCsBytecode", g_peakSmoothCSSource, strlen(g_peakSmoothCSSource), "PeakSmoothCS", "main", "cs_5_0", g_hookPeakSmoothCsBytecode, sizeof(g_hookPeakSmoothCsBytecode) },
    };
    std::vector<std::vector<unsigned char>> made;
    for (const Item& it : items) {
        ID3DBlob* blob = nullptr; ID3DBlob* msgs = nullptr;
        const HRESULT hr = D3DCompile(it.src, it.len, it.name, nullptr, nullptr, it.entry, it.target, 0, 0, &blob, &msgs);
        if (msgs) msgs->Release();
        REQUIRE_MESSAGE((SUCCEEDED(hr) && blob), it.array << " does not compile");
        const unsigned char* b = (const unsigned char*)blob->GetBufferPointer();
        made.emplace_back(b, b + blob->GetBufferSize());
        blob->Release();
    }
    const std::string compiler = CompilerVersion();

    if (!perf::Env("DLUT_HOOK_BYTECODE_WRITE").empty()) {
        char exe[MAX_PATH] = {};
        GetModuleFileNameA(nullptr, exe, MAX_PATH);
        std::string dir(exe), file;
        for (int up = 0; up < 8 && file.empty(); up++) {
            const size_t slash = dir.find_last_of("\\/");
            if (slash == std::string::npos) break;
            dir.resize(slash);
            if (GetFileAttributesA((dir + "\\dwm_hook\\hook_shader.h").c_str()) != INVALID_FILE_ATTRIBUTES) file = dir + "\\dwm_hook\\hook_shader_bytecode.h";
        }
        REQUIRE_MESSAGE(!file.empty(), "dwm_hook/hook_shader.h not found above " << exe);
        std::string text =
            "// GENERATED - do not edit. The DWM hook's own shaders as bytecode: D3DCompile of dwm_hook/hook_shader.h (VS, PS) and\n"
            "// shared/peak_detect.h (the two peak-detection passes), entry points / targets / flags as hook_render.cpp compiled them\n"
            "// at run time until 2026-11. InitializeStuff creates the shaders from these arrays, so nothing is compiled inside DWM's\n"
            "// present. tests/test_perf.cpp (\"hook: the shipped shader bytecode is D3DCompile of the current source\") fails when this\n"
            "// file and the shader text disagree. After ANY edit of that text regenerate and rebuild:\n"
            "//   DLUT_HOOK_BYTECODE_WRITE=1 bin/Test/DesktopLUT.Tests.exe -tc=\"hook: the shipped shader bytecode*\"\n"
            "#pragma once\n\n"
            "#define HOOK_BYTECODE_COMPILER \"" + compiler + "\"   // the d3dcompiler_47.dll that made it (file version)\n";
        for (size_t k = 0; k < made.size(); k++) {
            text += std::string("\nstatic const unsigned char ") + items[k].array + "[" + std::to_string(made[k].size()) + "] = {";
            char b[8];
            for (size_t i = 0; i < made[k].size(); i++) {
                if (i % 24 == 0) text += "\n\t";
                snprintf(b, sizeof(b), "0x%02X,", made[k][i]);
                text += b;
            }
            text += "\n};\n";
        }
        REQUIRE(perf::SaveBytes(file, text.data(), text.size()));
        MESSAGE("wrote " << file << " (compiler " << compiler << "): rebuild the DLL and this exe");
        return;
    }

    bool allEqual = true;
    for (size_t k = 0; k < made.size(); k++)
        if (made[k].size() != items[k].haveLen || std::memcmp(made[k].data(), items[k].have, made[k].size()) != 0) {
            allEqual = false;
            MESSAGE(std::string(items[k].array) << ": the header has " << items[k].haveLen << " bytes, the current source compiles to " << made[k].size()
                    << (made[k].size() == items[k].haveLen ? " (different bytes)" : ""));
        }
    if (!allEqual && compiler != HOOK_BYTECODE_COMPILER) {
        // another d3dcompiler_47.dll than the one that made the header: its output may differ for the same text, so this
        // machine cannot tell an edited shader from a different compiler. The machine that regenerates the header can.
        MESSAGE("the header was made by d3dcompiler " << std::string(HOOK_BYTECODE_COMPILER) << ", this machine has " << compiler << ": not judged here");
        return;
    }
    CHECK_MESSAGE(allEqual, "dwm_hook/hook_shader_bytecode.h is not the current shader source: regenerate it (the command is above this test) and rebuild");
}

