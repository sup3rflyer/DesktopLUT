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
//
// Protocol: stdin lines, replies on stdout (one line each, flushed):
//   load <path>        -> "ok load <name> shapes=<n> frames=<n>"   | "err ..."
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

struct Shape {             // scene units = full-resolution panel pixels relative to the monitor's top-left
    int kind = 0;          // 0 rect, 1 disc
    double cx = 0, cy = 0, a = 0, b = 0;    // rect: w, h | disc: r, 0
    double vx = 0, vy = 0;
    double r = 0, g = 0, bl = 0;            // linear nits per channel
    int blink = 0, blinkPhase = 0;          // > 0: shown for `blink` content frames, hidden for `blink`, … (motion.MovingShape)
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
    int frames() const { return pre + move + post; }
    double motionTime(int i) const { return (double)std::clamp(i - pre, 0, move); }
};

struct alignas(16) CB {
    float bg[4];
    float origin[2]; float pxscale; float outscale;
    unsigned nshapes; unsigned sdr; float sdrWhite; float sdrInvGamma;
    float shp[MAX_SHAPES][8];   // (kind, cx, cy, a) (b, R, G, B)
};

static const char* kHLSL = R"(
#define MAX_SHAPES 128
cbuffer CB : register(b0) {
    float4 bg;
    float2 origin; float pxscale; float outscale;
    uint nshapes; uint sdr; float sdrWhite; float sdrInvGamma;
    float4 shp[MAX_SHAPES * 2];
};
float4 VS(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}
float cov1(float lo, float hi, float p0, float p1) { return saturate((min(p1, hi) - max(p0, lo)) / (p1 - p0)); }
float4 PS(float4 pos : SV_Position) : SV_Target {
    float2 p0 = origin + floor(pos.xy) * pxscale;          // this pixel = [p0, p0 + pxscale) in scene px
    float2 p1 = p0 + pxscale;
    float3 c = bg.rgb;
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
        else { err = "line " + std::to_string(ln) + ": unknown key " + k; return false; }
        if (!ok) { err = "line " + std::to_string(ln) + ": bad values for " + k; return false; }
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

// Fill the constant buffer for content frame i (or park when sc == nullptr). presentIdx = the Gray code's value.
// lockRefresh >= 0: blinking shapes follow the REFRESH the frame is aimed at (play ... lock) instead of the content index
static void fillCB(CB& cb, const Args& a, const Scene* sc, int i, unsigned long long presentIdx, double parkNits,
                   long long lockRefresh = -1) {
    memset(&cb, 0, sizeof cb);
    double pxs = a.sceneW / a.w;
    cb.origin[0] = 0; cb.origin[1] = 0; cb.pxscale = (float)pxs; cb.outscale = 1.0f / 80.0f;
    cb.sdr = a.hdr ? 0u : 1u; cb.sdrWhite = (float)a.sdrWhite; cb.sdrInvGamma = (float)(1.0 / a.sdrGamma);
    if (!sc) {
        for (int c = 0; c < 3; ++c) cb.bg[c] = (float)parkNits;
        return;
    }
    for (int c = 0; c < 3; ++c) cb.bg[c] = (float)sc->bg[c];
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
    std::string line;
    char buf[4096];
    while (fgets(buf, sizeof buf, stdin)) {
        line = buf;
        while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) line.pop_back();
        std::istringstream is(line); std::string k; is >> k;
        if (k == "quit") { reply("ok quit"); break; }
        if (k == "load") {
            std::string path; std::getline(is, path); path.erase(0, path.find_first_not_of(' '));
            Scene s; if (parseScene(path, s, err)) { scene = std::move(s); have = true; reply("ok load " + scene.name); } else reply("err load: " + err);
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
    std::string statsPath = a.log + ".stats.csv";
    FILE* statf = nullptr;
    if (fopen_s(&statf, statsPath.c_str(), "w") != 0 || !statf) { reply("fatal cannot open the stats log"); return 2; }
    fprintf(statf, "qpc,st_present_count,st_present_refresh,st_sync_refresh,st_sync_qpc\n");
    UINT lastSeenCount = 0xFFFFFFFFu;
    struct StatRow { long long qpc; UINT pc, pr, sr; long long sq; };
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
    };
    auto pollStats = [&]() {
        DXGI_FRAME_STATISTICS st{};
        if (FAILED(sc->GetFrameStatistics(&st)) || st.PresentCount == 0 || st.PresentCount == lastSeenCount) return;
        lastSeenCount = st.PresentCount;
        const Sub& sb = submitN[st.PresentCount % submitN.size()];
        if (sb.pc == st.PresentCount) {
            offHist.push_back((long long)st.PresentRefreshCount - sb.n);
            if (offHist.size() > 7) offHist.erase(offHist.begin());
            std::vector<long long> c(offHist); std::nth_element(c.begin(), c.begin() + c.size() / 2, c.end());
            offsetMed = c[c.size() / 2]; offsetValid = offHist.size() >= 5;
        }
        LARGE_INTEGER t; QueryPerformanceCounter(&t);
        stats.push_back({t.QuadPart, st.PresentCount, st.PresentRefreshCount, st.SyncRefreshCount, st.SyncQPCTime.QuadPart});
    };
    auto flushStats = [&]() {
        for (const StatRow& r : stats) fprintf(statf, "%lld,%u,%u,%u,%lld\n", r.qpc, r.pc, r.pr, r.sr, r.sq);
        fflush(statf);
        stats.clear();
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
                std::string err; Scene s;
                if (state == PLAY) reply("err load: playing");
                else if (parseScene(path, s, err)) {
                    scene = std::move(s); haveScene = true; state = PARK;
                    reply("ok load " + scene.name + " shapes=" + std::to_string(scene.shapes.size()) + " frames=" + std::to_string(scene.frames()));
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
    if (mm) AvRevertMmThreadCharacteristics(mm);
    timeEndPeriod(1);
    DestroyWindow(g_hwnd);
    return 0;
}
