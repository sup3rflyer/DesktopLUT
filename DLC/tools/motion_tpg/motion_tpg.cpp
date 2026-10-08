// motion_tpg — a frame-exact MOTION test-pattern generator for the FALD temporal measurements (DLC, 2026-10-04).
//
// Why not dogegen: dogegen has no motion command, acks before the present (timing truth only from the camera), slips
// +-1 frame at 48 Hz, and exposes no presented-refresh index — the one number stage 3 (tick parity) needs.
//
// What this does:
//   * one borderless top-most window over a monitor rect (physical pixels, per-monitor DPI aware), flip-model
//     FLIP_DISCARD swapchain, FP16 R16G16B16A16 + scRGB (G10_NONE_P709, 1.0 = 80 nits) for HDR — the DWM composes
//     HDR / ACM-SDR desktops in FP16, so the FALD layer in the DWM hook sees exactly these linear values;
//     --sdr: R10G10B10A2 + G22_NONE_P709 with a configurable power-law encode (UNVERIFIED transfer — HDR is the target);
//   * the scene (src/dlc/fald/motion.py Scene, written by motion_tpg.py as a text file) is rendered ANALYTICALLY per
//     pixel with the simulator's coverage rules: rect = exact box-filter area overlap, disc = SDF clamp
//     clamp(r - |pixel centre - centre| + 0.5, 0, 1); shapes painted in order, blended in LINEAR nits;
//   * timing: content frame i is held cadence[i % n] refreshes. The loop waits for the output's vblank
//     (IDXGIOutput::WaitForVBlank), then presents --phase-ms (5) later with SyncInterval 1 — mid-frame, far from the DWM's
//     latch point (this build's DWM latches late, ~3 ms before vblank: a waitable-object loop woke right at it and the
//     2026-10-04 self-test dropped / doubled 14 % of the frames). Default = ONE present per content frame, then
//     k vblanks until the next (the desktop changes only when the content does, like video); --repeat-presents
//     re-presents every refresh (the layer re-runs every refresh). MMCSS thread;
//   * every Present is logged with DXGI's frame statistics (PresentCount -> PresentRefreshCount = the vblank the frame
//     reached the screen on) so the analysis knows the PRESENTED refresh index of every content frame (slips included);
//   * optional camera aids in the scene: a SYNC patch that toggles lo/hi when the motion starts and ends, and a
//     Gray-coded present counter (one cell changes per new frame) for frame identity in the video.
//   * IMAGE scenes (2026-10-08, src/dlc/fald/motion_image.py): up to 4 linear-nits image LAYERS (raw little-endian
//     float16 / float32 files, 1 / 2 / 3 / 4 channels = grey / grey+alpha / RGB / RGBA, premultiplied) on a GRID of S cells per screen pixel
//     (S = 2 = the slow-pan study's half-pixel canvas). Moving layers are translated by an integer number of CELLS per
//     content frame ("disp" list, one (dx, dy) per content frame). A screen pixel is the exact box average of its S x S
//     cells, each cell composited bottom to top (c = c * (1 - a) + rgb) — read with Load(), no sampler, so the pixels
//     equal results/fald_slowpan_2026-10-06/pan_scenes.py (2x canvas shift + 2x2 mean) and its full-frame ImageScene
//     (integer shift, first column repeated = "clamp" addressing) exactly. Shapes / aids are drawn on top as before.
//
// Protocol: stdin lines, replies on stdout (one line each, flushed):
//   load <path>        -> "ok load <name> shapes=<n> frames=<n>[ layers=<n> grid=<S>]"   | "err ..."
//                         (an IMAGE scene's layer files are read and uploaded here: load while parked, not mid-play)
//   play [cycles] [lock] -> "ok play" now (lock: blinking shapes follow the TARGET REFRESH count, so a late frame
//                         cannot shift a toggle's phase for the rest of the play), "done play <cycles> presents=<a>..<b>" when the last present was SUBMITTED (then
//                         holds the last frame; the log says when each present reached the screen)
//   park [nits]        -> "ok park <nits>"  (uniform grey; also the idle state at start)
//   status             -> "ok status ..."
//   quit               -> "ok quit" and exit.  EOF on stdin = quit (no orphaned full-screen window).
// Startup prints "ready output=<name> hdr=<0|1> maxnits=<x> refresh=<hz> rect=<x,y,w,h> qpcfreq=<f>".
//
// Build: tools/motion_tpg/build.cmd (cl, VS2022). Runtime HLSL compile via d3dcompiler_47.dll.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <d3dcompiler.h>
#include <avrt.h>
#include <timeapi.h>
#include <dwmapi.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <deque>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;

static const int MAX_SHAPES = 128;  // scene shapes + sync patch + code cells + readable counter
static const int MAX_LAYERS = 4;    // image layers of an IMAGE scene
static const int MAX_GRID = 8;      // cells per screen pixel
static const int MAX_TEX = 16384;   // D3D11 texture dimension limit

struct Shape {             // scene units = full-resolution panel pixels relative to the monitor's top-left
    int kind = 0;          // 0 rect, 1 disc
    double cx = 0, cy = 0, a = 0, b = 0;    // rect: w, h | disc: r, 0
    double vx = 0, vy = 0;
    double r = 0, g = 0, bl = 0;            // linear nits per channel
    int blink = 0, blinkPhase = 0;          // > 0: shown for `blink` content frames, hidden for `blink`, … (motion.MovingShape)
};

struct Layer {              // one image layer of an IMAGE scene (grid cells; see the header)
    int w = 0, h = 0, ch = 0; bool f16 = false;
    int x = 0, y = 0;           // screen cell of texel (0, 0) at zero displacement
    bool moving = false, clamp = false;
    int clip[4] = {0, 0, 0, 0}; // screen-fixed cell rect [x0, x1) x [y0, y1); outside = transparent
    std::vector<uint16_t> h16;  // expanded RGBA texels until the upload
    std::vector<float> f32;
};

struct Scene {
    std::string name = "(none)";
    double bg[3] = {2, 2, 2};
    int pre = 0, move = 0, post = 0;
    std::vector<int> cadence{1};
    std::vector<Shape> shapes;
    bool hasSync = false; double sync[4] = {0, 0, 0, 0}; double syncLo = 0, syncHi = 0;
    bool hasCode = false; double codeX = 0, codeY = 0, codeCell = 0; int codeBits = 0; double codeLo = 0, codeHi = 0;
    bool hasDigits = false; double digX = 0, digY = 0, digH = 0; int digN = 0; double digLo = 0, digHi = 0;
    int grid = 1;
    std::vector<Layer> layers;
    std::vector<int> dispX, dispY;   // per content frame, cells
    int frames() const { return pre + move + post; }
    double motionTime(int i) const { return (double)std::clamp(i - pre, 0, move); }
};

struct alignas(16) CB {
    float bg[4];
    float origin[2]; float pxscale; float outscale;
    unsigned nshapes; unsigned sdr; float sdrWhite; float sdrInvGamma;
    int nlayers; int grid; int disp[2];
    int lsize[MAX_LAYERS][4];   // (w, h, moving, clamp)
    int lorg[MAX_LAYERS][4];    // (x, y, -, -) cells
    int lclip[MAX_LAYERS][4];   // (x0, y0, x1, y1) cells
    float shp[MAX_SHAPES][8];   // (kind, cx, cy, a) (b, R, G, B)
};

static const char* kHLSL = R"(
#define MAX_SHAPES 128
#define MAX_LAYERS 4
#define MAX_CELLS 64
Texture2D<float4> lay[MAX_LAYERS] : register(t0);
cbuffer CB : register(b0) {
    float4 bg;
    float2 origin; float pxscale; float outscale;
    uint nshapes; uint sdr; float sdrWhite; float sdrInvGamma;
    int nlayers; int grid; int2 disp;
    int4 lsize[MAX_LAYERS];
    int4 lorg[MAX_LAYERS];
    int4 lclip[MAX_LAYERS];
    float4 shp[MAX_SHAPES * 2];
};
float4 VS(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}
float cov1(float lo, float hi, float p0, float p1) { return saturate((min(p1, hi) - max(p0, lo)) / (p1 - p0)); }
// one grid cell: the surround, then every layer bottom to top (premultiplied over)
float3 cellValue(int2 cell, float3 c) {
    [unroll] for (int L = 0; L < MAX_LAYERS; ++L) {
        if (L < nlayers) {
            int4 cl = lclip[L];
            if (all(cell >= cl.xy) && all(cell < cl.zw)) {
                int4 sz = lsize[L];
                int2 q = cell - lorg[L].xy - (sz.z != 0 ? disp : int2(0, 0));
                bool inside = all(q >= 0) && all(q < sz.xy);
                if (sz.w != 0) { q = clamp(q, int2(0, 0), sz.xy - 1); inside = true; }
                if (inside) { float4 v = lay[L].Load(int3(q, 0)); c = c * (1.0 - v.a) + v.rgb; }
            }
        }
    }
    return c;
}
// the pixel [p0, p1) (scene px) = the exact area average of the composited cells it overlaps
float3 imageValue(float2 p0, float2 p1, float3 c0) {
    float S = (float)grid;
    float2 g0 = p0 * S, g1 = p1 * S;
    int2 j0 = (int2)floor(g0);
    int2 j1 = min((int2)ceil(g1), j0 + MAX_CELLS);
    float3 acc = 0; float wsum = 0;
    [loop] for (int y = j0.y; y < j1.y; ++y) {
        float wy = min((float)(y + 1), g1.y) - max((float)y, g0.y);
        [loop] for (int x = j0.x; x < j1.x; ++x) {
            float w = wy * (min((float)(x + 1), g1.x) - max((float)x, g0.x));
            acc += w * cellValue(int2(x, y), c0);
            wsum += w;
        }
    }
    return acc / max(wsum, 1e-20);
}
float4 PS(float4 pos : SV_Position) : SV_Target {
    float2 p0 = origin + floor(pos.xy) * pxscale;          // this pixel = [p0, p0 + pxscale) in scene px
    float2 p1 = p0 + pxscale;
    float3 c = bg.rgb;
    if (nlayers > 0) c = imageValue(p0, p1, c);
    [loop] for (uint i = 0; i < nshapes; ++i) {
        float4 s0 = shp[2 * i], s1 = shp[2 * i + 1];
        float cov;
        if (s0.x < 0.5) {
            float hw = 0.5 * s0.w, hh = 0.5 * s1.x;
            cov = cov1(s0.y - hw, s0.y + hw, p0.x, p1.x) * cov1(s0.z - hh, s0.z + hh, p0.y, p1.y);
        } else {
            float2 m = 0.5 * (p0 + p1);
            cov = saturate((s0.w - distance(m, s0.yz)) / pxscale + 0.5);
        }
        c = lerp(c, s1.yzw, cov);
    }
    if (sdr != 0) {
        float3 n = saturate(c / sdrWhite);
        return float4(pow(n, sdrInvGamma), 1);
    }
    return float4(c * outscale, 1);                         // scRGB: 1.0 = 80 nits
}
)";

// ------------------------------------------------------------------------------------------------ state
struct Args {
    int x = 0, y = 0, w = 0, h = 0;
    bool hdr = true;
    double sdrWhite = 120.0, sdrGamma = 2.2;
    double park = 2.0;
    bool repeatPresents = false;
    double phaseMs = 5.0;     // present this long after the vblank: far from the DWM's latch (late latching ~3 ms before vblank)
    double sceneW = 3840.0;   // scene width the window shows (window smaller than this = a miniature test view)
    double originX = 0, originY = 0;   // scene px at the window's top-left (--origin; offscreen crops)
    std::string log;
};

static std::mutex g_qm;
static std::deque<std::string> g_queue;
static std::atomic<bool> g_eof{false};
static HWND g_hwnd = nullptr;

static void reply(const std::string& s) {
    fputs(s.c_str(), stdout); fputc('\n', stdout); fflush(stdout);
}

static void stdinThread() {
    char buf[4096];
    while (fgets(buf, sizeof buf, stdin)) {
        std::string s(buf);
        while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
        std::lock_guard<std::mutex> lk(g_qm);
        g_queue.push_back(s);
    }
    g_eof = true;
}

static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_SETCURSOR: SetCursor(nullptr); return TRUE;
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case WM_CLOSE: g_eof = true; return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static bool parseScene(const std::string& path, Scene& sc, std::string& err) {
    std::ifstream f(path);
    if (!f) { err = "cannot open " + path; return false; }
    Scene s;
    std::string line;
    int ln = 0;
    while (std::getline(f, line)) {
        ++ln;
        auto hash = line.find('#');
        if (hash != std::string::npos) line.resize(hash);
        std::istringstream is(line);
        std::string k;
        if (!(is >> k)) continue;
        bool ok = true;
        if (k == "name") { is >> s.name; }
        else if (k == "bg") { ok = bool(is >> s.bg[0] >> s.bg[1] >> s.bg[2]); }
        else if (k == "pre") { ok = bool(is >> s.pre); }
        else if (k == "move") { ok = bool(is >> s.move); }
        else if (k == "post") { ok = bool(is >> s.post); }
        else if (k == "cadence") { s.cadence.clear(); int c; while (is >> c) s.cadence.push_back(c); ok = !s.cadence.empty(); }
        else if (k == "rect" || k == "disc") {
            Shape sh; sh.kind = (k == "disc");
            if (sh.kind == 0) ok = bool(is >> sh.cx >> sh.cy >> sh.a >> sh.b >> sh.vx >> sh.vy >> sh.r >> sh.g >> sh.bl);
            else ok = bool(is >> sh.cx >> sh.cy >> sh.a >> sh.vx >> sh.vy >> sh.r >> sh.g >> sh.bl);
            std::string opt;
            if (ok && (is >> opt)) {
                if (opt == "blink") { ok = bool(is >> sh.blink); if (!(is >> sh.blinkPhase)) sh.blinkPhase = 0; ok = ok && sh.blink >= 0 && sh.blinkPhase >= 0; }
                else ok = false;
            }
            s.shapes.push_back(sh);
        }
        else if (k == "sync") { s.hasSync = true; ok = bool(is >> s.sync[0] >> s.sync[1] >> s.sync[2] >> s.sync[3] >> s.syncLo >> s.syncHi); }
        else if (k == "code") { s.hasCode = true; ok = bool(is >> s.codeX >> s.codeY >> s.codeCell >> s.codeBits >> s.codeLo >> s.codeHi); }
        else if (k == "digits") { s.hasDigits = true; ok = bool(is >> s.digX >> s.digY >> s.digH >> s.digN >> s.digLo >> s.digHi); }
        else if (k == "grid") { ok = bool(is >> s.grid); }
        else if (k == "disp") {   // (dx, dy) cell pairs, appended in content-frame order
            int dx, dy; int n = 0;
            while (is >> dx) { if (!(is >> dy)) { ok = false; break; } s.dispX.push_back(dx); s.dispY.push_back(dy); ++n; }
            ok = ok && n > 0;
        }
        else if (k == "layer") {  // layer <w> <h> <ch> <f16|f32> <x> <y> <moving 0|1> <clamp|border> <cx0> <cy0> <cx1> <cy1> <path>
            Layer L; std::string dt, addr; int mv = 0;
            ok = bool(is >> L.w >> L.h >> L.ch >> dt >> L.x >> L.y >> mv >> addr >> L.clip[0] >> L.clip[1] >> L.clip[2] >> L.clip[3]);
            std::string lpath; std::getline(is, lpath);
            lpath.erase(0, std::min(lpath.find_first_not_of(" \t"), lpath.size()));
            while (!lpath.empty() && (lpath.back() == ' ' || lpath.back() == '\t')) lpath.pop_back();
            ok = ok && (dt == "f16" || dt == "f32") && (addr == "clamp" || addr == "border") && !lpath.empty()
                    && L.w >= 1 && L.h >= 1 && L.w <= MAX_TEX && L.h <= MAX_TEX && (L.ch >= 1 && L.ch <= 4);
            if (ok) {
                L.f16 = dt == "f16"; L.moving = mv != 0; L.clamp = addr == "clamp";
                const size_t n = (size_t)L.w * L.h, bytes = L.f16 ? 2 : 4;
                std::ifstream bf(lpath, std::ios::binary | std::ios::ate);
                if (!bf) { err = "line " + std::to_string(ln) + ": cannot open layer file " + lpath; return false; }
                const size_t have = (size_t)bf.tellg();
                if (have != n * L.ch * bytes) {
                    err = "line " + std::to_string(ln) + ": layer file " + lpath + " has " + std::to_string(have) + " bytes, want "
                          + std::to_string(n * L.ch * bytes);
                    return false;
                }
                bf.seekg(0);
                std::vector<char> raw(have);
                if (!bf.read(raw.data(), (std::streamsize)have)) { err = "line " + std::to_string(ln) + ": read " + lpath; return false; }
                // expand to RGBA (grey -> R=G=B, grey+alpha, RGB -> alpha 1, RGBA premultiplied)
                if (L.f16) {
                    const uint16_t* src = (const uint16_t*)raw.data(); L.h16.resize(n * 4);
                    for (size_t p = 0; p < n; ++p) {
                        uint16_t* d = &L.h16[4 * p];
                        if (L.ch == 1) { d[0] = d[1] = d[2] = src[p]; d[3] = 0x3C00; }
                        else if (L.ch == 2) { d[0] = d[1] = d[2] = src[2 * p]; d[3] = src[2 * p + 1]; }
                        else if (L.ch == 3) { d[0] = src[3 * p]; d[1] = src[3 * p + 1]; d[2] = src[3 * p + 2]; d[3] = 0x3C00; }
                        else memcpy(d, &src[4 * p], 8);
                    }
                } else {
                    const float* src = (const float*)raw.data(); L.f32.resize(n * 4);
                    for (size_t p = 0; p < n; ++p) {
                        float* d = &L.f32[4 * p];
                        if (L.ch == 1) { d[0] = d[1] = d[2] = src[p]; d[3] = 1.0f; }
                        else if (L.ch == 2) { d[0] = d[1] = d[2] = src[2 * p]; d[3] = src[2 * p + 1]; }
                        else if (L.ch == 3) { d[0] = src[3 * p]; d[1] = src[3 * p + 1]; d[2] = src[3 * p + 2]; d[3] = 1.0f; }
                        else memcpy(d, &src[4 * p], 16);
                    }
                }
                s.layers.push_back(std::move(L));
            }
        }
        else { err = "line " + std::to_string(ln) + ": unknown key " + k; return false; }
        if (!ok) { err = "line " + std::to_string(ln) + ": bad values for " + k; return false; }
    }
    if (!s.layers.empty() || !s.dispX.empty()) {
        if (s.layers.empty() || (int)s.layers.size() > MAX_LAYERS) { err = "image scene: 1.." + std::to_string(MAX_LAYERS) + " layers"; return false; }
        if (s.grid < 1 || s.grid > MAX_GRID) { err = "grid 1.." + std::to_string(MAX_GRID); return false; }
        if ((int)s.dispX.size() != s.frames()) {
            err = "image scene: disp has " + std::to_string(s.dispX.size()) + " frames, pre+move+post = " + std::to_string(s.frames());
            return false;
        }
    }
    for (int c : s.cadence) if (c < 1 || c > 4) { err = "cadence entries must be 1..4 (DXGI sync interval)"; return false; }
    if (s.pre < 0 || s.move < 0 || s.post < 0 || s.frames() < 1) { err = "pre/move/post"; return false; }
    if (s.codeBits < 0 || s.codeBits > 16) { err = "code bits 0..16"; return false; }
    if (s.hasDigits && (s.digN < 1 || s.digN > 8 || !(s.digH >= 8))) { err = "digits: 1..8 digits, height >= 8 px"; return false; }
    int n = (int)s.shapes.size() + (s.hasSync ? 1 : 0) + (s.hasCode ? s.codeBits + 2 : 0) + (s.hasDigits ? 1 + 7 * s.digN : 0);
    if (n > MAX_SHAPES) { err = "too many shapes (" + std::to_string(n) + " > " + std::to_string(MAX_SHAPES) + ")"; return false; }
    sc = std::move(s);
    return true;
}

// Upload an IMAGE scene's layers (immutable textures, read with Load()); frees the CPU copies. On failure `out` is empty.
static bool uploadLayers(ID3D11Device* dev, Scene& s, std::vector<ComPtr<ID3D11ShaderResourceView>>& out, std::string& err) {
    out.clear();
    for (Layer& L : s.layers) {
        D3D11_TEXTURE2D_DESC td{};
        td.Width = L.w; td.Height = L.h; td.MipLevels = 1; td.ArraySize = 1; td.SampleDesc.Count = 1;
        td.Format = L.f16 ? DXGI_FORMAT_R16G16B16A16_FLOAT : DXGI_FORMAT_R32G32B32A32_FLOAT;
        td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA sd{};
        sd.pSysMem = L.f16 ? (const void*)L.h16.data() : (const void*)L.f32.data();
        sd.SysMemPitch = (UINT)L.w * (L.f16 ? 8u : 16u);
        ComPtr<ID3D11Texture2D> t;
        HRESULT hr = dev->CreateTexture2D(&td, &sd, &t);
        ComPtr<ID3D11ShaderResourceView> v;
        if (SUCCEEDED(hr)) hr = dev->CreateShaderResourceView(t.Get(), nullptr, &v);
        if (FAILED(hr)) {
            char b[96]; snprintf(b, sizeof b, "layer texture %dx%d hr=0x%08lx", L.w, L.h, (unsigned long)hr);
            err = b; out.clear(); return false;
        }
        out.push_back(v);
        std::vector<uint16_t>().swap(L.h16); std::vector<float>().swap(L.f32);
    }
    return true;
}

static void bindLayers(ID3D11DeviceContext* ctx, const std::vector<ComPtr<ID3D11ShaderResourceView>>& srvs) {
    ID3D11ShaderResourceView* v[MAX_LAYERS] = {};
    for (size_t k = 0; k < srvs.size() && k < (size_t)MAX_LAYERS; ++k) v[k] = srvs[k].Get();
    ctx->PSSetShaderResources(0, MAX_LAYERS, v);
}

// Fill the constant buffer for content frame i (or park when sc == nullptr). presentIdx = the Gray code's value.
// lockRefresh >= 0: blinking shapes follow the REFRESH the frame is aimed at (play ... lock) instead of the content index
static void fillCB(CB& cb, const Args& a, const Scene* sc, int i, unsigned long long presentIdx, double parkNits,
                   long long lockRefresh = -1) {
    memset(&cb, 0, sizeof cb);
    double pxs = a.sceneW / a.w;
    cb.origin[0] = (float)a.originX; cb.origin[1] = (float)a.originY; cb.pxscale = (float)pxs; cb.outscale = 1.0f / 80.0f;
    cb.sdr = a.hdr ? 0u : 1u; cb.sdrWhite = (float)a.sdrWhite; cb.sdrInvGamma = (float)(1.0 / a.sdrGamma);
    if (!sc) {
        for (int c = 0; c < 3; ++c) cb.bg[c] = (float)parkNits;
        return;
    }
    for (int c = 0; c < 3; ++c) cb.bg[c] = (float)sc->bg[c];
    if (!sc->layers.empty()) {   // IMAGE scene: this content frame's displacement + the layer descriptors
        const int k = std::clamp(i, 0, (int)sc->dispX.size() - 1);
        cb.nlayers = (int)sc->layers.size(); cb.grid = sc->grid; cb.disp[0] = sc->dispX[k]; cb.disp[1] = sc->dispY[k];
        for (int L = 0; L < cb.nlayers; ++L) {
            const Layer& ly = sc->layers[L];
            cb.lsize[L][0] = ly.w; cb.lsize[L][1] = ly.h; cb.lsize[L][2] = ly.moving ? 1 : 0; cb.lsize[L][3] = ly.clamp ? 1 : 0;
            cb.lorg[L][0] = ly.x; cb.lorg[L][1] = ly.y;
            for (int q = 0; q < 4; ++q) cb.lclip[L][q] = ly.clip[q];
        }
    }
    double t = sc->motionTime(i);
    unsigned n = 0;
    auto put = [&](int kind, double cx, double cy, double aa, double bb, double r, double g, double b) {
        float* p = cb.shp[n++];
        p[0] = (float)kind; p[1] = (float)cx; p[2] = (float)cy; p[3] = (float)aa;
        p[4] = (float)bb; p[5] = (float)r; p[6] = (float)g; p[7] = (float)b;
    };
    for (const Shape& s : sc->shapes) {
        if (s.blink > 0) {
            const long long b2 = 2LL * s.blink;
            const long long pos = lockRefresh >= 0 ? ((lockRefresh % b2) + s.blinkPhase) % b2 : (long long)(i + s.blinkPhase) % b2;
            if (pos >= s.blink) continue;   // hidden this frame / refresh
        }
        put(s.kind, s.cx + s.vx * t, s.cy + s.vy * t, s.a, s.b, s.r, s.g, s.bl);
    }
    if (sc->hasSync) {
        int events = (i >= sc->pre ? 1 : 0) + (i >= sc->pre + sc->move ? 1 : 0);
        double v = (events % 2) ? sc->syncHi : sc->syncLo;
        put(0, sc->sync[0] + 0.5 * sc->sync[2], sc->sync[1] + 0.5 * sc->sync[3], sc->sync[2], sc->sync[3], v, v, v);
    }
    if (sc->hasCode) {
        unsigned long long gray = presentIdx ^ (presentIdx >> 1);
        double cs = sc->codeCell;
        // two reference cells (lo, hi), then bit 0 .. bits-1
        put(0, sc->codeX + 0.5 * cs, sc->codeY + 0.5 * cs, cs, cs, sc->codeLo, sc->codeLo, sc->codeLo);
        put(0, sc->codeX + 1.5 * cs, sc->codeY + 0.5 * cs, cs, cs, sc->codeHi, sc->codeHi, sc->codeHi);
        for (int k = 0; k < sc->codeBits; ++k) {
            double v = ((gray >> k) & 1ull) ? sc->codeHi : sc->codeLo;
            put(0, sc->codeX + (2.5 + k) * cs, sc->codeY + 0.5 * cs, cs, cs, v, v, v);
        }
    }
    if (sc->hasDigits) {
        // readable 7-segment counter of the present number (mod 10^n): a background plate at lo, the lit segments at hi.
        // Geometry mirrors dlc.fald.motion.Scene.aid_rects (digit width 0.6 h, stroke h / 8, gap 0.25 h, plate pad = stroke).
        static const unsigned char SEG[10] = {0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F};   // bits a..g
        const double h = sc->digH, W = 0.6 * h, tk = h / 8.0, gap = 0.25 * h, x0 = sc->digX, y0 = sc->digY;
        const int nd = sc->digN;
        auto rect = [&](double x, double y, double w, double hh, double v) { put(0, x + 0.5 * w, y + 0.5 * hh, w, hh, v, v, v); };
        rect(x0 - tk, y0 - tk, nd * W + (nd - 1) * gap + 2 * tk, h + 2 * tk, sc->digLo);
        unsigned long long v = presentIdx;
        for (int d = nd - 1; d >= 0; --d) {
            const unsigned seg = SEG[v % 10]; v /= 10;
            const double x = x0 + d * (W + gap), y = y0, hv = 0.5 * h - 1.5 * tk;
            const double r[7][4] = {{x + tk, y, W - 2 * tk, tk}, {x + W - tk, y + tk, tk, hv}, {x + W - tk, y + 0.5 * h + 0.5 * tk, tk, hv},
                                    {x + tk, y + h - tk, W - 2 * tk, tk}, {x, y + 0.5 * h + 0.5 * tk, tk, hv}, {x, y + tk, tk, hv},
                                    {x + tk, y + 0.5 * h - 0.5 * tk, W - 2 * tk, tk}};
            for (int k = 0; k < 7; ++k) if (seg & (1u << k)) rect(r[k][0], r[k][1], r[k][2], r[k][3], sc->digHi);
        }
    }
    cb.nshapes = n;
}

struct LogRow {
    unsigned long long present; long long qpc; long long vbq; long long vbN; long long rTarget; int play; int cycle; int content; int sub; int interval;
    UINT lastPresentCount; UINT stPresentCount; UINT stPresentRefresh; UINT stSyncRefresh; long long stSyncQpc; int statsOk;
};

static int fail(const char* what, HRESULT hr) {
    char b[256]; snprintf(b, sizeof b, "fatal %s hr=0x%08lx", what, (unsigned long)hr); reply(b); return 2;
}

static HRESULT compileShaders(ID3D11Device* dev, ComPtr<ID3D11VertexShader>& vs, ComPtr<ID3D11PixelShader>& ps, std::string& err) {
    ComPtr<ID3DBlob> vsb, psb, eb;
    HRESULT hr = D3DCompile(kHLSL, strlen(kHLSL), "motion_tpg", nullptr, nullptr, "VS", "vs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &vsb, &eb);
    if (FAILED(hr)) { err = eb ? (const char*)eb->GetBufferPointer() : "VS"; return hr; }
    hr = D3DCompile(kHLSL, strlen(kHLSL), "motion_tpg", nullptr, nullptr, "PS", "ps_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &psb, &eb);
    if (FAILED(hr)) { err = eb ? (const char*)eb->GetBufferPointer() : "PS"; return hr; }
    hr = dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs);
    if (SUCCEEDED(hr)) hr = dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &ps);
    return hr;
}

// --offscreen: no window, no swapchain — render chosen frames of a scene into a texture of the --rect size with the SAME
// shader / constant-buffer path and write them out, so a test can compare the TPG's pixels with the simulator's coverage
// (tests/test_fald_motion_tpg.py). --warp uses the WARP software rasteriser (deterministic, no GPU needed).
// Commands: load <path> | dump <content_index> <present_number> <out.f16 path>  (RGBA float16 rows, top to bottom) | quit
// --origin x,y = the scene px at the texture's top-left (a crop of a full-screen scene; also honoured on screen).
static int offscreenMain(const Args& a, bool warp) {
    ComPtr<ID3D11Device> dev; ComPtr<ID3D11DeviceContext> ctx;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDevice(nullptr, warp ? D3D_DRIVER_TYPE_WARP : D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, &fl, 1,
                                   D3D11_SDK_VERSION, &dev, nullptr, &ctx);
    if (FAILED(hr)) return fail("D3D11CreateDevice", hr);
    ComPtr<ID3D11VertexShader> vs; ComPtr<ID3D11PixelShader> ps; std::string err;
    if (FAILED(hr = compileShaders(dev.Get(), vs, ps, err))) { reply("fatal shader " + err); return 2; }
    D3D11_TEXTURE2D_DESC td{};
    td.Width = a.w; td.Height = a.h; td.MipLevels = 1; td.ArraySize = 1; td.SampleDesc.Count = 1;
    td.Format = a.hdr ? DXGI_FORMAT_R16G16B16A16_FLOAT : DXGI_FORMAT_R10G10B10A2_UNORM;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ComPtr<ID3D11Texture2D> rt, staging;
    if (FAILED(hr = dev->CreateTexture2D(&td, nullptr, &rt))) return fail("CreateTexture2D", hr);
    td.BindFlags = 0; td.Usage = D3D11_USAGE_STAGING; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (FAILED(hr = dev->CreateTexture2D(&td, nullptr, &staging))) return fail("CreateTexture2D staging", hr);
    ComPtr<ID3D11RenderTargetView> rtv; dev->CreateRenderTargetView(rt.Get(), nullptr, &rtv);
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = sizeof(CB); bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    ComPtr<ID3D11Buffer> cbuf; if (FAILED(hr = dev->CreateBuffer(&bd, nullptr, &cbuf))) return fail("CreateBuffer", hr);
    reply(std::string("ready offscreen ") + (warp ? "warp" : "hardware"));
    Scene scene; bool have = false;
    std::vector<ComPtr<ID3D11ShaderResourceView>> layerSrv;
    std::string line;
    char buf[4096];
    while (fgets(buf, sizeof buf, stdin)) {
        line = buf;
        while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) line.pop_back();
        std::istringstream is(line); std::string k; is >> k;
        if (k == "quit") { reply("ok quit"); break; }
        if (k == "load") {
            std::string path; std::getline(is, path); path.erase(0, path.find_first_not_of(' '));
            Scene s; std::vector<ComPtr<ID3D11ShaderResourceView>> srv;
            if (parseScene(path, s, err) && uploadLayers(dev.Get(), s, srv, err)) {
                scene = std::move(s); layerSrv = std::move(srv); have = true; reply("ok load " + scene.name);
            } else reply("err load: " + err);
        } else if (k == "dump") {
            int i = 0; unsigned long long pn = 0; std::string out;
            if (!(is >> i >> pn) || !have) { reply("err dump: dump <content_index> <present> <path> after load"); continue; }
            std::getline(is, out); out.erase(0, out.find_first_not_of(' '));
            CB cb{}; fillCB(cb, a, &scene, std::clamp(i, 0, scene.frames() - 1), pn, a.park);
            ctx->UpdateSubresource(cbuf.Get(), 0, nullptr, &cb, 0, 0);
            D3D11_VIEWPORT vp{0, 0, (float)a.w, (float)a.h, 0, 1};
            ctx->RSSetViewports(1, &vp); ctx->OMSetRenderTargets(1, rtv.GetAddressOf(), nullptr);
            ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            ctx->VSSetShader(vs.Get(), nullptr, 0); ctx->PSSetShader(ps.Get(), nullptr, 0);
            ctx->PSSetConstantBuffers(0, 1, cbuf.GetAddressOf());
            bindLayers(ctx.Get(), layerSrv);
            ctx->Draw(3, 0);
            ctx->CopyResource(staging.Get(), rt.Get());
            D3D11_MAPPED_SUBRESOURCE m{};
            if (FAILED(hr = ctx->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &m))) { reply("err dump: map"); continue; }
            FILE* f = nullptr;
            if (fopen_s(&f, out.c_str(), "wb") != 0 || !f) { ctx->Unmap(staging.Get(), 0); reply("err dump: open " + out); continue; }
            const size_t rowBytes = (size_t)a.w * (a.hdr ? 8 : 4);
            for (int y = 0; y < a.h; ++y) fwrite((const char*)m.pData + (size_t)y * m.RowPitch, 1, rowBytes, f);
            fclose(f); ctx->Unmap(staging.Get(), 0);
            reply("ok dump " + out);
        } else if (!k.empty()) reply("err unknown command " + k);
    }
    return 0;
}

int main(int argc, char** argv) {
    Args a;
    bool offscreen = false, warp = false;
    for (int k = 1; k < argc; ++k) {
        std::string s = argv[k];
        auto next = [&]() -> std::string { return (k + 1 < argc) ? std::string(argv[++k]) : std::string(); };
        if (s == "--rect") { std::string v = next(); if (sscanf_s(v.c_str(), "%d,%d,%d,%d", &a.x, &a.y, &a.w, &a.h) != 4) { reply("fatal --rect x,y,w,h"); return 2; } }
        else if (s == "--sdr") a.hdr = false;
        else if (s == "--hdr") a.hdr = true;
        else if (s == "--sdr-white") a.sdrWhite = atof(next().c_str());
        else if (s == "--sdr-gamma") a.sdrGamma = atof(next().c_str());
        else if (s == "--park") a.park = atof(next().c_str());
        else if (s == "--repeat-presents") a.repeatPresents = true;
        else if (s == "--phase-ms") a.phaseMs = atof(next().c_str());
        else if (s == "--scene-width") a.sceneW = atof(next().c_str());
        else if (s == "--origin") { std::string v = next(); if (sscanf_s(v.c_str(), "%lf,%lf", &a.originX, &a.originY) != 2) { reply("fatal --origin x,y"); return 2; } }
        else if (s == "--log") a.log = next();
        else if (s == "--offscreen") offscreen = true;
        else if (s == "--warp") warp = true;
        else { reply("fatal unknown argument " + s); return 2; }
    }
    if (a.w <= 0 || a.h <= 0) { reply("fatal --rect x,y,w,h is required"); return 2; }
    if (offscreen) return offscreenMain(a, warp);
    if (a.log.empty()) { reply("fatal --log <presents.csv> is required"); return 2; }

    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    timeBeginPeriod(1);

    WNDCLASSEXW wc{sizeof wc};
    wc.lpfnWndProc = WndProc; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = L"DLCMotionTPG";
    wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    RegisterClassExW(&wc);
    g_hwnd = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, wc.lpszClassName, L"DLC motion TPG",
                             WS_POPUP, a.x, a.y, a.w, a.h, nullptr, nullptr, wc.hInstance, nullptr);
    if (!g_hwnd) return fail("CreateWindowEx", HRESULT_FROM_WIN32(GetLastError()));
    ShowWindow(g_hwnd, SW_SHOWNOACTIVATE);

    ComPtr<IDXGIFactory2> factory;
    HRESULT hr = CreateDXGIFactory2(0, IID_PPV_ARGS(&factory));
    if (FAILED(hr)) return fail("CreateDXGIFactory2", hr);

    // the output whose desktop rect contains the window centre
    ComPtr<IDXGIAdapter1> adapter; ComPtr<IDXGIOutput6> output; DXGI_OUTPUT_DESC1 od{};
    {
        ComPtr<IDXGIFactory1> f1; factory.As(&f1);
        POINT c{a.x + a.w / 2, a.y + a.h / 2};
        for (UINT ai = 0; !output && f1->EnumAdapters1(ai, &adapter) != DXGI_ERROR_NOT_FOUND; ++ai) {
            ComPtr<IDXGIOutput> o;
            for (UINT oi = 0; adapter->EnumOutputs(oi, &o) != DXGI_ERROR_NOT_FOUND; ++oi) {
                ComPtr<IDXGIOutput6> o6;
                if (SUCCEEDED(o.As(&o6)) && SUCCEEDED(o6->GetDesc1(&od)) && PtInRect(&od.DesktopCoordinates, c)) { output = o6; break; }
            }
            if (!output) adapter.Reset();
        }
    }
    if (!output) { reply("fatal no DXGI output contains the rect centre"); return 2; }
    bool outHdr = od.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
    double refreshHz = 0;
    {
        MONITORINFOEXW mi{}; mi.cbSize = sizeof mi;
        DEVMODEW dm{}; dm.dmSize = sizeof dm;
        if (GetMonitorInfoW(od.Monitor, &mi) && EnumDisplaySettingsW(mi.szDevice, ENUM_CURRENT_SETTINGS, &dm)) refreshHz = dm.dmDisplayFrequency;
    }

    ComPtr<ID3D11Device> dev; ComPtr<ID3D11DeviceContext> ctx;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    hr = D3D11CreateDevice(adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ctx);
    if (FAILED(hr)) return fail("D3D11CreateDevice", hr);

    DXGI_SWAP_CHAIN_DESC1 sd{};
    sd.Width = a.w; sd.Height = a.h;
    sd.Format = a.hdr ? DXGI_FORMAT_R16G16B16A16_FLOAT : DXGI_FORMAT_R10G10B10A2_UNORM;
    sd.SampleDesc.Count = 1; sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; sd.BufferCount = 3;
    sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD; sd.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
    ComPtr<IDXGISwapChain1> sc1;
    hr = factory->CreateSwapChainForHwnd(dev.Get(), g_hwnd, &sd, nullptr, nullptr, &sc1);
    if (FAILED(hr)) return fail("CreateSwapChainForHwnd", hr);
    factory->MakeWindowAssociation(g_hwnd, DXGI_MWA_NO_ALT_ENTER);
    ComPtr<IDXGISwapChain3> sc; sc1.As(&sc);
    ComPtr<IDXGISwapChainMedia> media; sc1.As(&media);   // CompositionMode per frame (composed / overlay / independent flip)
    hr = sc->SetColorSpace1(a.hdr ? DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709 : DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709);
    if (FAILED(hr)) return fail("SetColorSpace1", hr);
    {   // at most one frame queued (the vblank wait paces; no waitable object whose count could drift on a timeout)
        ComPtr<IDXGIDevice1> dxdev;
        if (SUCCEEDED(dev.As(&dxdev))) dxdev->SetMaximumFrameLatency(1);
    }

    ComPtr<ID3D11VertexShader> vs; ComPtr<ID3D11PixelShader> ps;
    {
        std::string err;
        if (FAILED(hr = compileShaders(dev.Get(), vs, ps, err))) { reply("fatal shader " + err); return 2; }
    }
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = sizeof(CB); bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    ComPtr<ID3D11Buffer> cbuf; hr = dev->CreateBuffer(&bd, nullptr, &cbuf);
    if (FAILED(hr)) return fail("CreateBuffer", hr);

    // MMCSS + high priority for the present thread
    DWORD task = 0; HANDLE mm = AvSetMmThreadCharacteristicsW(L"Pro Audio", &task);
    if (mm) AvSetMmThreadPriority(mm, AVRT_PRIORITY_CRITICAL);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);

    LARGE_INTEGER qf; QueryPerformanceFrequency(&qf);
    {
        char b[512];
        char name[64]{}; WideCharToMultiByte(CP_UTF8, 0, od.DeviceName, -1, name, sizeof name, nullptr, nullptr);
        snprintf(b, sizeof b, "ready output=%s hdr=%d maxnits=%.0f refresh=%.3f rect=%d,%d,%d,%d qpcfreq=%lld mode=%s presents=%s phase_ms=%.2f",
                 name, outHdr ? 1 : 0, od.MaxLuminance, refreshHz, a.x, a.y, a.w, a.h, qf.QuadPart, a.hdr ? "hdr" : "sdr",
                 a.repeatPresents ? "every-refresh" : "per-content-frame", a.phaseMs);
        reply(b);
    }
    std::thread(stdinThread).detach();

    FILE* logf = nullptr;
    if (fopen_s(&logf, a.log.c_str(), "w") != 0 || !logf) { reply("fatal cannot open --log file"); return 2; }
    fprintf(logf, "present,qpc,vblank_qpc,vblank_n,r_target,play,cycle,content,sub,interval,last_present_count,st_present_count,st_present_refresh,st_sync_refresh,st_sync_qpc,st_ok\n");
    fprintf(logf, "# qpcfreq=%lld refresh=%.3f output_hdr=%d\n", qf.QuadPart, refreshHz, outHdr ? 1 : 0);

    Scene scene; bool haveScene = false;
    std::vector<ComPtr<ID3D11ShaderResourceView>> layerSrv;   // the loaded IMAGE scene's layers
    double parkNits = a.park;
    // PARK = uniform grey (start, after load, after park); PLAY = the scene runs; HOLD = a finished play keeps its last frame
    enum { PARK, PLAY, HOLD } state = PARK;
    int playId = 0, cycles = 0, cycle = 0, content = 0, sub = 0;
    unsigned long long firstPresent = 0;
    unsigned long long presentIdx = 0;
    std::vector<LogRow> rows; rows.reserve(1 << 16);
    CB cb{};

    auto flushRows = [&]() {
        for (const LogRow& r : rows)
            fprintf(logf, "%llu,%lld,%lld,%lld,%lld,%d,%d,%d,%d,%d,%u,%u,%u,%u,%lld,%d\n", r.present, r.qpc, r.vbq, r.vbN, r.rTarget, r.play, r.cycle, r.content, r.sub,
                    r.interval, r.lastPresentCount, r.stPresentCount, r.stPresentRefresh, r.stSyncRefresh, r.stSyncQpc, r.statsOk);
        fflush(logf);
        rows.clear();
    };

    // frame-statistics samples (PresentCount -> PresentRefreshCount), polled at every vblank and after every present;
    // written to <log>.stats.csv when the present rows are flushed
    // DWM composition timing at every vblank wake (stage-3 counter bench: is DWM_TIMING_INFO.cRefresh the same numbering as
    // DXGI PresentRefreshCount, and are qpcVBlank the real vblank instants?) -> <log>.dwm.csv
    FILE* dwmf = nullptr;
    if (fopen_s(&dwmf, (a.log + ".dwm.csv").c_str(), "w") != 0 || !dwmf) { reply("fatal cannot open the dwm log"); return 2; }
    fprintf(dwmf, "qpc,vbn,ok,c_refresh,qpc_vblank,c_frame,qpc_compose,c_frames_displayed,qpc_refresh_period\n");
    struct DwmRow { long long qpc, vbn; int ok; unsigned long long cRefresh, qpcVBlank, cFrame, qpcCompose, cDisplayed, period; };
    std::vector<DwmRow> dwmRows; dwmRows.reserve(1 << 16);
    std::string statsPath = a.log + ".stats.csv";
    FILE* statf = nullptr;
    if (fopen_s(&statf, statsPath.c_str(), "w") != 0 || !statf) { reply("fatal cannot open the stats log"); return 2; }
    fprintf(statf, "qpc,st_present_count,st_present_refresh,st_sync_refresh,st_sync_qpc,st_comp_mode\n");
    UINT lastSeenCount = 0xFFFFFFFFu;
    // st_comp_mode: DXGI_FRAME_PRESENTATION_MODE of the frame (0 composed by DWM, 1 overlay plane, 2 none / independent
    // flip, 3 composition failure; -1 = not reported): whether DWM (and so a DWM hook) ever sees the frames
    struct StatRow { long long qpc; UINT pc, pr, sr; long long sq; int mode; };
    std::vector<StatRow> stats; stats.reserve(1 << 16);
    // own vblank index (arbitrary origin): +1 per WaitForVBlank return, corrected by the QPC gap when a wake came late
    // (period = median of single-step gaps); and its offset to DXGI PresentRefreshCount, learned from every present that
    // reaches the screen: offset = PresentRefreshCount - vbN at its submit (median of the last 7: the DWM's latency shifts
    // between regimes and the own count can slip by one on a late wake, so the offset must re-learn within a few frames). Target refresh of the
    // frame being built = vbN + offset (play ... lock).
    long long vbN = 0, lastVbq = 0;
    double periodQpc = (double)qf.QuadPart / (refreshHz > 1.0 ? refreshHz : 60.0);
    std::vector<double> stepHist;
    struct Sub { UINT pc = 0; long long n = 0; };
    std::vector<Sub> submitN(4096);
    std::vector<long long> offHist;
    long long offsetMed = 0; bool offsetValid = false;
    auto onVblank = [&]() {
        LARGE_INTEGER t; QueryPerformanceCounter(&t);
        if (lastVbq) {
            const double dt = (double)(t.QuadPart - lastVbq);
            long long steps = std::max(1LL, std::llround(dt / periodQpc));
            if (steps == 1) {
                stepHist.push_back(dt);
                if (stepHist.size() >= 31) {
                    std::vector<double> c(stepHist); std::nth_element(c.begin(), c.begin() + c.size() / 2, c.end());
                    periodQpc = c[c.size() / 2]; stepHist.erase(stepHist.begin(), stepHist.begin() + 16);
                }
            }
            vbN += steps;
        }
        lastVbq = t.QuadPart;
        DWM_TIMING_INFO ti{}; ti.cbSize = sizeof ti;
        const int ok = SUCCEEDED(DwmGetCompositionTimingInfo(nullptr, &ti)) ? 1 : 0;
        dwmRows.push_back({t.QuadPart, vbN, ok, ti.cRefresh, ti.qpcVBlank, ti.cFrame, ti.qpcCompose, ti.cFramesDisplayed,
                           ti.qpcRefreshPeriod});
    };
    auto pollStats = [&]() {
        DXGI_FRAME_STATISTICS st{};
        int compMode = -1;
        DXGI_FRAME_STATISTICS_MEDIA sm{};
        if (media && SUCCEEDED(media->GetFrameStatisticsMedia(&sm))) {
            st.PresentCount = sm.PresentCount; st.PresentRefreshCount = sm.PresentRefreshCount;
            st.SyncRefreshCount = sm.SyncRefreshCount; st.SyncQPCTime = sm.SyncQPCTime; st.SyncGPUTime = sm.SyncGPUTime;
            compMode = (int)sm.CompositionMode;
        } else if (FAILED(sc->GetFrameStatistics(&st))) {
            return;
        }
        if (st.PresentCount == 0 || st.PresentCount == lastSeenCount) return;
        lastSeenCount = st.PresentCount;
        const Sub& sb = submitN[st.PresentCount % submitN.size()];
        if (sb.pc == st.PresentCount) {
            offHist.push_back((long long)st.PresentRefreshCount - sb.n);
            if (offHist.size() > 7) offHist.erase(offHist.begin());
            std::vector<long long> c(offHist); std::nth_element(c.begin(), c.begin() + c.size() / 2, c.end());
            offsetMed = c[c.size() / 2]; offsetValid = offHist.size() >= 5;
        }
        LARGE_INTEGER t; QueryPerformanceCounter(&t);
        stats.push_back({t.QuadPart, st.PresentCount, st.PresentRefreshCount, st.SyncRefreshCount, st.SyncQPCTime.QuadPart, compMode});
    };
    auto flushStats = [&]() {
        for (const StatRow& r : stats) fprintf(statf, "%lld,%u,%u,%u,%lld,%d\n", r.qpc, r.pc, r.pr, r.sr, r.sq, r.mode);
        fflush(statf);
        stats.clear();
        for (const DwmRow& r : dwmRows)
            fprintf(dwmf, "%lld,%lld,%d,%llu,%llu,%llu,%llu,%llu,%llu\n", r.qpc, r.vbn, r.ok, r.cRefresh, r.qpcVBlank, r.cFrame,
                    r.qpcCompose, r.cDisplayed, r.period);
        fflush(dwmf);
        dwmRows.clear();
    };

    bool running = true;
    int pendingVblanks = 1;
    bool playLock = false;
    while (running) {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
        // commands — right after the previous present, BEFORE the vblank wait: slow work (a scene parse, a log flush on
        // park) lands in the slack of the frame, never between the phase point and the Present (review 2026-10-04:
        // handled after the phase wait, park / load pushed presents 14-21 ms past the vblank and one refresh late)
        std::vector<std::string> cmds;
        const bool eofNow = g_eof;   // read BEFORE draining: the reader sets it after its last push, so nothing is lost
        { std::lock_guard<std::mutex> lk(g_qm); while (!g_queue.empty()) { cmds.push_back(g_queue.front()); g_queue.pop_front(); } }
        if (eofNow && cmds.empty()) { cmds.push_back("quit"); }
        for (const std::string& line : cmds) {
            std::istringstream is(line);
            std::string k; is >> k;
            if (k.empty()) continue;
            if (k == "load") {
                std::string path; std::getline(is, path);
                path.erase(0, path.find_first_not_of(' '));
                std::string err; Scene s; std::vector<ComPtr<ID3D11ShaderResourceView>> srv;
                if (state == PLAY) reply("err load: playing");
                else if (parseScene(path, s, err) && uploadLayers(dev.Get(), s, srv, err)) {
                    scene = std::move(s); layerSrv = std::move(srv); haveScene = true; state = PARK;
                    std::string r = "ok load " + scene.name + " shapes=" + std::to_string(scene.shapes.size()) + " frames=" + std::to_string(scene.frames());
                    if (!scene.layers.empty()) r += " layers=" + std::to_string(scene.layers.size()) + " grid=" + std::to_string(scene.grid);
                    reply(r);
                } else reply("err load: " + err);
            } else if (k == "play") {
                int n = 1; is >> n;
                std::string opt; playLock = bool(is >> opt) && opt == "lock";
                if (!haveScene) reply("err play: no scene");
                else if (state == PLAY) reply("err play: already playing");
                else { state = PLAY; ++playId; cycles = std::max(n, 1); cycle = 0; content = 0; sub = 0; firstPresent = presentIdx; reply("ok play"); }
            } else if (k == "park") {
                double v = parkNits; is >> v; parkNits = v;
                if (state == PLAY) { char b[96]; snprintf(b, sizeof b, "abort play presents=%llu..%llu", firstPresent + 1, presentIdx); reply(b); }
                state = PARK;
                flushRows(); flushStats();
                char b[64]; snprintf(b, sizeof b, "ok park %.4f", parkNits); reply(b);
            } else if (k == "status") {
                char b[256]; snprintf(b, sizeof b, "ok status presents=%llu state=%s scene=%s cycle=%d content=%d",
                                      presentIdx, state == PLAY ? "play" : state == HOLD ? "hold" : "park", scene.name.c_str(), cycle, content);
                reply(b);
            } else if (k == "quit") { running = false; reply("ok quit"); break; }
            else reply("err unknown command " + k);
        }
        if (!running) break;
        // pace: the previous present's interval in vblanks, then the phase offset
        LARGE_INTEGER vbq{};
        for (int v = 0; v < std::max(pendingVblanks, 1); ++v) {
            output->WaitForVBlank();
            onVblank();
            pollStats();   // every vblank: a frame held k refreshes still has its arrival sampled
        }
        QueryPerformanceCounter(&vbq);
        {
            const long long until = vbq.QuadPart + (long long)(a.phaseMs * 1e-3 * (double)qf.QuadPart);
            LARGE_INTEGER now; QueryPerformanceCounter(&now);
            const double leftMs = (until - now.QuadPart) * 1e3 / (double)qf.QuadPart;
            if (leftMs > 2.0) Sleep((DWORD)(leftMs - 1.5));
            do { QueryPerformanceCounter(&now); } while (now.QuadPart < until);
        }


        // what to show
        int interval = 1;
        const Scene* show = nullptr;
        int ci = 0;
        if (state == PLAY) {
            show = &scene; ci = content;
            int k = scene.cadence[content % scene.cadence.size()];
            interval = a.repeatPresents ? 1 : k;
        } else if (state == HOLD) {
            show = &scene; ci = scene.frames() - 1;   // a finished play holds its last frame until park / play
        }
        const long long rTarget = offsetValid ? vbN + offsetMed : -1;   // the DXGI refresh this frame is aimed at
        fillCB(cb, a, show, ci, presentIdx + 1, parkNits,           // the Gray code carries this present's logged number
               (state == PLAY && playLock) ? rTarget : -1);
        ctx->UpdateSubresource(cbuf.Get(), 0, nullptr, &cb, 0, 0);

        ComPtr<ID3D11Texture2D> bb; sc->GetBuffer(0, IID_PPV_ARGS(&bb));
        ComPtr<ID3D11RenderTargetView> rtv; dev->CreateRenderTargetView(bb.Get(), nullptr, &rtv);
        D3D11_VIEWPORT vp{0, 0, (float)a.w, (float)a.h, 0, 1};
        ctx->RSSetViewports(1, &vp);
        ctx->OMSetRenderTargets(1, rtv.GetAddressOf(), nullptr);
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->VSSetShader(vs.Get(), nullptr, 0);
        ctx->PSSetShader(ps.Get(), nullptr, 0);
        ctx->PSSetConstantBuffers(0, 1, cbuf.GetAddressOf());
        bindLayers(ctx.Get(), layerSrv);
        ctx->Draw(3, 0);

        LARGE_INTEGER q; QueryPerformanceCounter(&q);
        hr = sc->Present(1, 0);
        pendingVblanks = interval;
        if (FAILED(hr)) { flushRows(); flushStats(); fclose(logf); fclose(statf); return fail("Present", hr); }
        ++presentIdx;

        const bool playing = state == PLAY;
        LogRow r{};
        r.present = presentIdx; r.qpc = q.QuadPart; r.vbq = vbq.QuadPart; r.play = playing ? playId : 0; r.cycle = playing ? cycle : -1;
        r.content = playing ? content : -1; r.sub = playing ? sub : -1; r.interval = interval;
        sc->GetLastPresentCount(&r.lastPresentCount);
        r.vbN = vbN; r.rTarget = rTarget;
        submitN[r.lastPresentCount % submitN.size()] = {r.lastPresentCount, vbN};
        DXGI_FRAME_STATISTICS st{};
        if (SUCCEEDED(sc->GetFrameStatistics(&st))) {
            r.statsOk = 1; r.stPresentCount = st.PresentCount; r.stPresentRefresh = st.PresentRefreshCount;
            r.stSyncRefresh = st.SyncRefreshCount; r.stSyncQpc = st.SyncQPCTime.QuadPart;
        }
        rows.push_back(r);
        pollStats();

        if (playing) {
            int k = scene.cadence[content % scene.cadence.size()];
            sub += a.repeatPresents ? 1 : k;
            if (sub >= k) {
                sub = 0; ++content;
                if (content >= scene.frames()) {
                    content = 0; ++cycle;
                    if (cycle >= cycles) {
                        state = HOLD; content = scene.frames() - 1;
                        char b[160]; snprintf(b, sizeof b, "done play %d presents=%llu..%llu", cycles, firstPresent + 1, presentIdx);
                        flushRows(); flushStats();
                        reply(b);
                    }
                }
            }
        } else if (rows.size() > 4096) {
            flushRows(); flushStats();
        }
    }
    flushRows(); flushStats();
    fclose(logf);
    fclose(statf);
    fclose(dwmf);
    if (mm) AvRevertMmThreadCharacteristics(mm);
    timeEndPeriod(1);
    DestroyWindow(g_hwnd);
    return 0;
}
