// The panel clock (LED lag mode 3) on SETTLED content must be neutral: a still full-field grey shows what the stateless
// layer (LED lag off) shows, whatever the refresh period (2026-11-14: the owner's meter found a full-field grey 0.2-0.75 %
// brighter at 47.952 / 50 Hz with the clock on than with it off; at 60 Hz neutral).
//
// EXPERIMENT (opt-in, FALD_TEST_NEUTRAL_OUT=<file>): both hosts, the owner's HDR panel file, the owner's starfield / core
// settings, static full-field greys at PQ codes 193 .. 769; per refresh period the present sequence a still patch gets
// (the previous patch settled, the content present, the settle kicks at a cadence, the kicker's linger, then one sparse
// present after a long pause), the time SET (g_faldTestNowQpc). Writes the screen centre and the frame mean of every
// present, clock on and LED lag off. FALD_TEST_NEUTRAL_HW=1 runs on the hardware device instead of WARP.
#include "doctest.h"
#include "../dwm_hook/hook_fald.h"
#include "../shared/fald_core_mode.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_temporal.h"

#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <DirectXPackedVector.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#ifdef FALD_RUN_HAS_RECTS
namespace {

std::string NtEnv(const char* name) {
    char buf[1024] = {};
    const DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? std::string(buf, n) : std::string();
}
long long NtQpcFreq() { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f.QuadPart; }
long long NtQpc() { LARGE_INTEGER t; QueryPerformanceCounter(&t); return t.QuadPart; }

double NtHalf(uint16_t h) {
    return (double)DirectX::PackedVector::XMConvertHalfToFloat(h);
}
uint16_t NtToHalf(float f) { return DirectX::PackedVector::XMConvertFloatToHalf(f); }

double NtPqEotfNits(double e) {
    const double m1 = 0.1593017578125, m2 = 78.84375, c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
    const double p = std::pow(e, 1.0 / m2);
    return 10000.0 * std::pow((std::max)(p - c1, 0.0) / (c2 - c3 * p), 1.0 / m1);
}

struct NtDevice {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr; ID3D11VertexShader* vs = nullptr;
    bool ok = false;
    explicit NtDevice(bool hw) {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, hw ? D3D_DRIVER_TYPE_HARDWARE : D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1,
                                     D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        static const char* vsSrc = "struct O { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };"
                                   "O main(uint id : SV_VertexID) { O o; o.uv = float2((id << 1) & 2, id & 2);"
                                   "o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1); return o; }";
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(vsSrc, std::strlen(vsSrc), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr;
    }
    ~NtDevice() {
        if (vs) vs->Release();
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

// What a present left on the screen: the centre pixel (r, g, b) and, when asked, the frame mean, in scRGB (1 = 80 nits).
struct NtShot { double c[3] = { 0, 0, 0 }; double mean[3] = { -1, -1, -1 }; unsigned long long k = 0; bool seeded = false;
                unsigned int settle = 0; };

NtShot NtRead(ID3D11Device* dev, ID3D11DeviceContext* ic, ID3D11Texture2D* t, bool full) {
    NtShot s;
    D3D11_TEXTURE2D_DESC d; t->GetDesc(&d);
    D3D11_TEXTURE2D_DESC sd = d;
    sd.Usage = D3D11_USAGE_STAGING; sd.BindFlags = 0; sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ; sd.MiscFlags = 0;
    if (!full) { sd.Width = 1; sd.Height = 1; }
    ID3D11Texture2D* st = nullptr;
    if (FAILED(dev->CreateTexture2D(&sd, nullptr, &st))) return s;
    if (full) ic->CopyResource(st, t);
    else {
        D3D11_BOX box = { d.Width / 2, d.Height / 2, 0, d.Width / 2 + 1, d.Height / 2 + 1, 1 };
        ic->CopySubresourceRegion(st, 0, 0, 0, 0, t, 0, &box);
    }
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(ic->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        const uint8_t* p = (const uint8_t*)m.pData;
        if (full) {
            double acc[3] = { 0, 0, 0 };
            for (UINT y = 0; y < d.Height; y++) {
                const uint16_t* row = (const uint16_t*)(p + (size_t)y * m.RowPitch);
                for (UINT x = 0; x < d.Width; x++) for (int c = 0; c < 3; c++) acc[c] += NtHalf(row[x * 4 + c]);
            }
            for (int c = 0; c < 3; c++) s.mean[c] = acc[c] / ((double)d.Width * d.Height);
            const uint16_t* cp = (const uint16_t*)(p + (size_t)(d.Height / 2) * m.RowPitch) + (d.Width / 2) * 4;
            for (int c = 0; c < 3; c++) s.c[c] = NtHalf(cp[c]);
        } else {
            const uint16_t* cp = (const uint16_t*)p;
            for (int c = 0; c < 3; c++) s.c[c] = NtHalf(cp[c]);
        }
        ic->Unmap(st, 0);
    }
    st->Release();
    return s;
}

DwmHookFaldTuning NtOwnerTuning(unsigned int tempMode, float periodMs) {
    DwmHookFaldTuning t = {};
    t.starEven = 0.8f; t.starTargetGain = 1.0f; t.starKeepNits = 100.0f; t.starStrength = 1.0f;
    t.starAreaLo = 40.0f; t.starAreaHi = 160.0f; t.starNbLo = 0.15f; t.starNbHi = 0.30f;
    t.starReach = 2u; t.starEvenReach = 8u;
    t.tempMode = tempMode; t.tempClockClosure = FALD_CLOCK_CLOSURE_DEFAULT; t.tempClockParity = -1;
    t.refreshMs = periodMs;
    return t;
}

void NtFill(std::vector<uint16_t>& f, int W, int H, int code) {
    const float v = (float)(NtPqEotfNits(code / 1023.0) / 80.0);
    const uint16_t h = NtToHalf(v), one = NtToHalf(1.0f);
    f.assign((size_t)W * H * 4, 0);
    for (size_t i = 0; i < (size_t)W * H; i++) { f[i * 4] = h; f[i * 4 + 1] = h; f[i * 4 + 2] = h; f[i * 4 + 3] = one; }
}

// One present of a host. kind: 0 = a settle kick (nothing new), 1 = new content (the whole frame).
struct NtHost {
    virtual ~NtHost() {}
    virtual bool Present(const std::vector<uint16_t>& frame, bool newContent, bool readFull, NtShot& out) = 0;
};

struct NtHook : NtHost {
    NtDevice& d; int W, H; DwmHookFaldTuning tuning; unsigned int coreMode;
    ID3D11Texture2D* back = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    std::string root; bool ok = false;
    RECT contentRect = { 0, 0, 0, 0 };   // non-empty: a content present's dirty rect (else the whole frame)
    bool contentWhole = false;           // this content present carries the whole frame anyway
    NtHook(NtDevice& dev, int w, int h, const std::wstring& panel, const DwmHookFaldTuning& t, unsigned int core)
        : d(dev), W(w), H(h), tuning(t), coreMode(core) {
        if (!FaldInitShaders(d.dev, d.ic)) return;
        char tmp[MAX_PATH] = {};
        GetTempPathA(MAX_PATH, tmp);
        root = std::string(tmp) + "dlut_clock_neutral_" + std::to_string(GetCurrentProcessId());
        CreateDirectoryA(root.c_str(), nullptr);
        CreateDirectoryA((root + "\\fald").c_str(), nullptr);
        const std::string dst = root + "\\fald\\0_0_hdr.bin";
        if (!CopyFileW(panel.c_str(), std::wstring(dst.begin(), dst.end()).c_str(), FALSE)) return;
        if (FaldLoadPanelFiles(root.c_str()) != 1) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)W; td.Height = (UINT)H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        ok = SUCCEEDED(d.dev->CreateTexture2D(&td, nullptr, &back)) && SUCCEEDED(d.dev->CreateRenderTargetView(back, nullptr, &rtv));
    }
    ~NtHook() {
        FaldReleaseAll();
        FaldReleaseShaders();
        if (rtv) rtv->Release();
        if (back) back->Release();
        DeleteFileA((root + "\\fald\\0_0_hdr.bin").c_str());
        RemoveDirectoryA((root + "\\fald").c_str());
        RemoveDirectoryA(root.c_str());
    }
    bool Present(const std::vector<uint16_t>& frame, bool newContent, bool readFull, NtShot& out) override {
        // a settle kick: DWM composes the kick pixel's corner (it is not new content to the DLL); the frame is unchanged
        RECT full = { 0, 0, (LONG)W, (LONG)H }, kick = { 0, 0, 1, 1 };
        const bool inRect = newContent && !contentWhole && contentRect.right > contentRect.left;   // content in a small rect
        const RECT* rc = inRect ? &contentRect : (newContent ? &full : &kick);
        if (newContent && !inRect) d.ic->UpdateSubresource(back, 0, nullptr, frame.data(), (UINT)W * 8u, 0);
        else {
            D3D11_BOX b = { (UINT)rc->left, (UINT)rc->top, 0, (UINT)rc->right, (UINT)rc->bottom, 1 };
            d.ic->UpdateSubresource(back, 0, &b, frame.data() + ((size_t)rc->top * W + (size_t)rc->left) * 4, (UINT)W * 8u, 0);
        }
        FaldMonitor* m = FaldAcquire(0, 0, true, (unsigned)W, (unsigned)H, DXGI_FORMAT_R16G16B16A16_FLOAT);
        if (!m) return false;
        FaldSetLiveSettings(m, 0, 0, true, &tuning, 0, 0, false, coreMode);
        if (!FaldUpdateClean(m, back, rc, 1)) { FaldReturn(m); FaldMarkStale(0, 0); return false; }
        const bool partial = FaldPlanPartial(m, 0x9E3779B97F4A7C15ull);
        if (partial) {
            D3D11_BOX box = { (UINT)rc->left, (UINT)rc->top, 0, (UINT)rc->right, (UINT)rc->bottom, 1 };
            d.ic->CopySubresourceRegion(FaldIntermediateTexture(m), 0, (UINT)rc->left, (UINT)rc->top, 0, FaldCleanTexture(m), 0, &box);
        } else d.ic->CopyResource(FaldIntermediateTexture(m), FaldCleanTexture(m));
        const bool ran = FaldRun(m, rtv, newContent, 0x9E3779B97F4A7C15ull, nullptr, 4u);
        out.k = FaldTestClockElapsed(m); out.seeded = FaldTestClockSeeded(m); out.settle = FaldTestSettleLeft(m);
        if (!ran) d.ic->CopyResource(back, FaldIntermediateTexture(m));
        FaldReturn(m);
        const NtShot s = NtRead(d.dev, d.ic, back, readFull);
        for (int c = 0; c < 3; c++) { out.c[c] = s.c[c]; out.mean[c] = s.mean[c]; }
        return ran;
    }
};

struct NtOverlay : NtHost {
    NtDevice& d; MonitorContext ctx; ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr; bool ok = false;
    NtOverlay(NtDevice& dev, int W, int H, const std::wstring& panel, unsigned int tempMode, float periodMs, unsigned int core) : d(dev) {
        g_device = d.dev; g_context = d.ic;
        if (!InitFaldShaders()) return;
        ctx.width = W; ctx.height = H; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = periodMs;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = true;
        fs.star.even = 0.8f; fs.star.targetGain = 1.0f; fs.star.keepNits = 100.0f; fs.star.strength = 1.0f;
        fs.star.areaLo = 40.0f; fs.star.areaHi = 160.0f; fs.star.nbLo = 0.15f; fs.star.nbHi = 0.30f;
        fs.star.reach = 2u; fs.star.evenReach = 8u;
        fs.coreMode = core;
        fs.temporalMode = tempMode; fs.clockParity = -1; fs.clockClosure = FALD_CLOCK_CLOSURE_DEFAULT;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)W; td.Height = (UINT)H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(d.dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(d.dev->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~NtOverlay() {
        if (rtv) rtv->Release();
        if (target) target->Release();
        FaldReleaseResources(&ctx);
        ReleaseFaldShaders();
        g_context = nullptr; g_device = nullptr;
    }
    bool Present(const std::vector<uint16_t>& frame, bool newContent, bool readFull, NtShot& out) override {
        D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
        d.ic->RSSetViewports(1, &vp);
        d.ic->VSSetShader(d.vs, nullptr, 0);
        d.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        if (newContent) d.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        FaldChangeRect full = { 0, 0, ctx.width, ctx.height };
        FaldRunPasses(&ctx, rtv, newContent, 0x9E3779B97F4A7C15ull, newContent ? &full : nullptr, newContent ? 1 : 0);
        out.k = ctx.fald->clkElapsed; out.seeded = ctx.fald->clkSeeded; out.settle = ctx.fald->settleLeft;
        const NtShot s = NtRead(d.dev, d.ic, target, readFull);
        for (int c = 0; c < 3; c++) { out.c[c] = s.c[c]; out.mean[c] = s.mean[c]; }
        return true;
    }
};

}  // namespace

TEST_CASE("FALD panel clock neutrality EXPERIMENT: still full-field greys, clock on vs LED lag off, per refresh period (FALD_TEST_NEUTRAL_OUT)") {
    const std::string outPath = NtEnv("FALD_TEST_NEUTRAL_OUT");
    if (outPath.empty()) return;
    std::wstring panel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";
    REQUIRE(GetFileAttributesW(panel.c_str()) != INVALID_FILE_ATTRIBUTES);
    const bool hw = NtEnv("FALD_TEST_NEUTRAL_HW") == "1";
    const int W = 3840, H = 2160;
    std::ofstream o(outPath);
    REQUIRE(o.good());
    o << "host period cadence code mode step k seeded settle cR cG cB mR mG mB\n";
    const std::string hostsEnv = NtEnv("FALD_TEST_NEUTRAL_HOSTS");   // "hook", "overlay" or both (default)
    const std::string codesEnv = NtEnv("FALD_TEST_NEUTRAL_CODES");
    std::vector<int> codes = { 193, 307, 450, 520, 592, 692, 769 };
    if (!codesEnv.empty()) { codes.clear(); codes.push_back(std::atoi(codesEnv.c_str())); }
    std::vector<float> periods = { 1000.0f / 60.0f, 20.0f, 1000.0f * 1001.0f / 48000.0f };
    if (NtEnv("FALD_TEST_NEUTRAL_FAST") == "1") periods = { 1000.0f / 60.0f, 1000.0f / 144.0f };
    const int prevEnv = NtEnv("FALD_TEST_NEUTRAL_PREV").empty() ? -1 : std::atoi(NtEnv("FALD_TEST_NEUTRAL_PREV").c_str());
    const long long f = NtQpcFreq();
    struct Restore { ~Restore() { g_faldTestNowQpc = 0; g_faldPartialOverride = -1; g_faldClockRestOverride = -1; } } restore;
    if (NtEnv("FALD_TEST_NEUTRAL_REST") == "0") g_faldClockRestOverride = 0;
    // A STREAM: something on the monitor presents content every refresh (the 2026-11-14 runs' core-governor status said
    // content_ms 16.666 at 60 Hz: a content present per refresh while the meter read). Per grey: the content change,
    // then 400 content presents of the same pixels (a small dirty rect away from the corners), at the monitor's refresh
    // or at 60 Hz (a source paced elsewhere); one line per grey with the centre AVERAGED over the last 300 presents
    // (what the meter integrates) in the c columns and the last present's in the m columns.
    if (NtEnv("FALD_TEST_NEUTRAL_SEQ") == "stream") {
        for (int host = 0; host < 2; host++) {
            if (!hostsEnv.empty() && hostsEnv != (host == 0 ? "hook" : "overlay")) continue;
            for (float P : periods) {
                const float cadences[] = { P, 1000.0f / 60.0f };
                for (int ci = 0; ci < 2; ci++) {
                    const float cad = cadences[ci];
                    if (ci > 0 && std::fabs(cad - P) < 0.01f) continue;
                    for (unsigned int mode : { (unsigned)FALD_TEMPORAL_PANEL, (unsigned)FALD_TEMPORAL_OFF }) {
                        NtDevice dev(hw);
                        REQUIRE(dev.ok);
                        NtHook* hk = nullptr; NtOverlay* ov = nullptr; NtHost* h = nullptr;
                        if (host == 0) { hk = new NtHook(dev, W, H, panel, NtOwnerTuning(mode, P), FALD_CORE_MODE_KC5); REQUIRE(hk->ok); h = hk; hk->contentRect = { 1000, 1800, 1064, 1864 }; }
                        else { ov = new NtOverlay(dev, W, H, panel, mode, P, FALD_CORE_MODE_KC5); REQUIRE(ov->ok); h = ov; }
                        const int seq[] = { 120, 193, 307, 450, 520, 592, 692, 769 };
                        std::vector<uint16_t> fr;
                        const long long t0 = NtQpc();
                        double tMs = 0.0;
                        auto at = [&](double ms) { g_faldTestNowQpc = t0 + (long long)(ms * (double)f / 1000.0 + 0.5); };
                        NtShot s;
                        NtFill(fr, W, H, 769);
                        at(tMs);
                        for (int a = 0; a < 3; a++) if (h->Present(fr, true, false, s)) break;
                        for (int code : seq) {
                            tMs += cad; at(tMs);
                            NtFill(fr, W, H, code);
                            if (hk) hk->contentWhole = true;
                            h->Present(fr, true, false, s);
                            if (hk) hk->contentWhole = false;
                            double acc[3] = { 0, 0, 0 }; int n = 0;
                            for (int i = 0; i < 400; i++) {
                                tMs += cad; at(tMs);
                                h->Present(fr, true, false, s);
                                if (i >= 100) { for (int c = 0; c < 3; c++) acc[c] += s.c[c]; n++; }
                            }
                            char b[512];
                            snprintf(b, sizeof(b), "%s %.4f %.4f %d %u %d %d 0 %d %.9g %.9g %.9g %.9g %.9g %.9g\n",
                                     host == 0 ? "hook" : "overlay", P, cad, code, mode, 1000, n, s.settle,
                                     acc[0] / n, acc[1] / n, acc[2] / n, s.c[0], s.c[1], s.c[2]);
                            o << b;
                        }
                        o.flush();
                        g_faldTestNowQpc = 0;
                        delete hk; delete ov;
                    }
                }
            }
        }
        return;
    }
    // The verify's own order: per leg the greys ascending, ~1.8 s each, the previous leg ending on the brightest; the
    // kicker paced by this monitor, a 60-Hz or a 144-Hz monitor elsewhere. One line per grey: what stands when it is read.
    if (NtEnv("FALD_TEST_NEUTRAL_SEQ") == "verify") {
        for (int host = 0; host < 2; host++) {
            if (!hostsEnv.empty() && hostsEnv != (host == 0 ? "hook" : "overlay")) continue;
            for (float P : periods) {
                const float cadences[] = { P, 1000.0f / 60.0f, 1000.0f / 144.0f };
                for (int ci = 0; ci < 3; ci++) {
                    const float cad = cadences[ci];
                    if (ci > 0 && std::fabs(cad - P) < 0.01f) continue;
                    for (unsigned int mode : { (unsigned)FALD_TEMPORAL_PANEL, (unsigned)FALD_TEMPORAL_OFF }) {
                        NtDevice dev(hw);
                        REQUIRE(dev.ok);
                        NtHook* hk = nullptr; NtOverlay* ov = nullptr; NtHost* h = nullptr;
                        if (host == 0) { hk = new NtHook(dev, W, H, panel, NtOwnerTuning(mode, P), FALD_CORE_MODE_KC5); REQUIRE(hk->ok); h = hk; }
                        else { ov = new NtOverlay(dev, W, H, panel, mode, P, FALD_CORE_MODE_KC5); REQUIRE(ov->ok); h = ov; }
                        const int seq[] = { 120, 193, 307, 450, 520, 592, 692, 769 };
                        std::vector<uint16_t> fr;
                        const long long t0 = NtQpc();
                        double tMs = 0.0;
                        auto at = [&](double ms) { g_faldTestNowQpc = t0 + (long long)(ms * (double)f / 1000.0 + 0.5); };
                        NtShot s;
                        NtFill(fr, W, H, 769);
                        at(tMs);
                        for (int a = 0; a < 3; a++) if (h->Present(fr, true, false, s)) break;
                        for (int leg = 0; leg < 2; leg++) {
                            for (int code : seq) {
                                tMs += 1800.0; at(tMs);
                                NtFill(fr, W, H, code);
                                h->Present(fr, true, true, s);
                                {   // the content present's own output (leg 2000+)
                                    char b[512];
                                    snprintf(b, sizeof(b), "%s %.4f %.4f %d %u %d %d 0 %d %.9g %.9g %.9g %.9g %.9g %.9g\n",
                                             host == 0 ? "hook" : "overlay", P, cad, code, mode, 2000 + leg, 1, s.settle,
                                             s.c[0], s.c[1], s.c[2], s.mean[0], s.mean[1], s.mean[2]);
                                    o << b;
                                }
                                double lastSettle = tMs; int presents = 1;
                                for (int i = 0; i < 2000; i++) {
                                    tMs += cad; at(tMs); presents++;
                                    h->Present(fr, false, false, s);
                                    if (s.settle > 0) lastSettle = tMs;
                                    if (tMs - lastSettle >= 100.0) break;
                                }
                                const NtShot r = NtRead(dev.dev, dev.ic, host == 0 ? hk->back : ov->target, true);
                                char b[512];
                                snprintf(b, sizeof(b), "%s %.4f %.4f %d %u %d %d 0 %d %.9g %.9g %.9g %.9g %.9g %.9g\n",
                                         host == 0 ? "hook" : "overlay", P, cad, code, mode, 1000 + leg, presents, s.settle,
                                         r.c[0], r.c[1], r.c[2], r.mean[0], r.mean[1], r.mean[2]);
                                o << b;
                            }
                        }
                        o.flush();
                        g_faldTestNowQpc = 0;
                        delete hk; delete ov;
                    }
                }
            }
        }
        return;
    }
    for (int host = 0; host < 2; host++) {
        if (!hostsEnv.empty() && hostsEnv != (host == 0 ? "hook" : "overlay")) continue;
        for (float P : periods) {
            // the kicker's cadence: this monitor's own refresh, or a 60-Hz monitor elsewhere (the fastest one paces it)
            const float cadences[] = { P, 1000.0f / 60.0f };
            for (int ci = 0; ci < 2; ci++) {
                const float cad = cadences[ci];
                if (ci == 1 && std::fabs(cad - P) < 0.01f) continue;
                for (int code : codes) {
                    for (unsigned int mode : { (unsigned)FALD_TEMPORAL_PANEL, (unsigned)FALD_TEMPORAL_OFF }) {
                        NtDevice dev(hw);
                        REQUIRE(dev.ok);
                        NtHost* h = nullptr;
                        NtHook* hk = nullptr; NtOverlay* ov = nullptr;
                        if (host == 0) { hk = new NtHook(dev, W, H, panel, NtOwnerTuning(mode, P), FALD_CORE_MODE_KC5); REQUIRE(hk->ok); h = hk; }
                        else { ov = new NtOverlay(dev, W, H, panel, mode, P, FALD_CORE_MODE_KC5); REQUIRE(ov->ok); h = ov; }
                        std::vector<uint16_t> prev, cur;
                        NtFill(prev, W, H, prevEnv >= 0 ? prevEnv : (code - 60 > 0 ? code - 60 : 0));
                        NtFill(cur, W, H, code);
                        const long long t0 = NtQpc();
                        double tMs = 0.0;
                        int step = 0;
                        auto at = [&](double ms) { g_faldTestNowQpc = t0 + (long long)(ms * (double)f / 1000.0 + 0.5); };
                        auto log = [&](const NtShot& s) {
                            char b[512];
                            snprintf(b, sizeof(b), "%s %.4f %.4f %d %u %d %llu %d %u %.9g %.9g %.9g %.9g %.9g %.9g\n",
                                     host == 0 ? "hook" : "overlay", P, cad, code, mode, step, s.k, s.seeded ? 1 : 0, s.settle,
                                     s.c[0], s.c[1], s.c[2], s.mean[0], s.mean[1], s.mean[2]);
                            o << b;
                        };
                        // the previous patch, settled (seed + presents a refresh apart for a second)
                        NtShot s;
                        at(tMs);
                        for (int a = 0; a < 3; a++) if (h->Present(prev, true, false, s)) break;
                        for (int i = 0; i < 60; i++) { tMs += P; at(tMs); h->Present(prev, false, false, s); }
                        // a pause, then the new patch: a content present
                        tMs += 2000.0; at(tMs);
                        step = 1; h->Present(cur, true, false, s); log(s);
                        // the settle kicks at the cadence while the hold runs, then the kicker's 100 ms linger
                        double lastSettle = tMs;
                        for (int i = 0; i < 400; i++) {
                            tMs += cad; at(tMs);
                            step++;
                            h->Present(cur, false, false, s); log(s);
                            if (s.settle > 0) lastSettle = tMs;
                            if (tMs - lastSettle >= 100.0) break;
                        }
                        // what stands on the screen after the kicks stop (full frame)
                        step = 900; at(tMs);
                        {
                            NtShot fin = s;
                            const NtShot r = NtRead(dev.dev, dev.ic, host == 0 ? hk->back : ov->target, true);
                            for (int c = 0; c < 3; c++) { fin.c[c] = r.c[c]; fin.mean[c] = r.mean[c]; }
                            log(fin);
                        }
                        // one sparse present after a long pause (another window's), then what stands
                        tMs += 3000.0; at(tMs); step = 901;
                        h->Present(cur, false, true, s); log(s);
                        for (int i = 0; i < 40; i++) {
                            tMs += cad; at(tMs); step++;
                            h->Present(cur, false, false, s);
                            if (s.settle == 0 && i > 2) break;
                        }
                        step = 999; { const NtShot r = NtRead(dev.dev, dev.ic, host == 0 ? hk->back : ov->target, true); NtShot fin = s;
                                      for (int c = 0; c < 3; c++) { fin.c[c] = r.c[c]; fin.mean[c] = r.mean[c]; } log(fin); }
                        o.flush();
                        g_faldTestNowQpc = 0;
                        delete hk; delete ov;
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// THE PROPERTY (2026-11-14): what a still full-field grey settles to with the panel clock does not depend on the refresh
// period a verify compares (60 / 59.94 / 50 / 47.952: one output to 2e-5), the host, or the clock at rest; it is the
// stateless layer's output to within the loop's own slow drift (the target is the clock's own corrected round-1 drives:
// the output stands on a stair 0.03 % in light / 0.1 % in one channel from the stateless one when the kicks stop, and a
// kicker paced by a much faster monitor runs long enough to move one stair further); and one present after a long pause
// (k > FALD_CLOCK_MAX_REFRESHES: blends exactly 1) shows the stateless output to 2e-5. The time is SET (g_faldTestNowQpc),
// so the k sequences are the periods' own. (The owner's meter found 0.2-0.75 % at 50 / 47.952 Hz against 60 Hz with the
// clock on and nothing with LED lag off: this case pins that the layer's arithmetic does not produce it.)
// Opt-in like the other FALD WARP cases (FALD_TEST_FADE_DIR); the owner's panel file (DLC results, local) or skipped;
// FALD_TEST_NEUTRAL_HW=1 runs the whole matrix on the hardware device (seconds: 60 / 50 / 47.952 / 120 / 144 / 59.94 /
// 72 Hz, a 144-Hz kicker too, rest on and forced off); on WARP (the default) the rates a verify compares, as shipped.
// ---------------------------------------------------------------------------------------------------------------------
TEST_CASE("FALD panel clock: a still full-field grey settles to the same output at every refresh period, both hosts, rest on and off (FALD_TEST_FADE_DIR)") {
    if (NtEnv("FALD_TEST_FADE_DIR").empty()) return;
    const std::wstring panel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";
    if (GetFileAttributesW(panel.c_str()) == INVALID_FILE_ATTRIBUTES) { MESSAGE("the owner's panel file is not here: skipped"); return; }
    const bool hw = NtEnv("FALD_TEST_NEUTRAL_HW") == "1";
    const int W = 3840, H = 2160;
    const long long f = NtQpcFreq();
    struct Restore { ~Restore() { g_faldTestNowQpc = 0; g_faldClockRestOverride = -1; } } restore;
    // 60, 50, 47.952, 120, 144, 59.94 (non-integer ratio to 60), 72 Hz
    const float periods[] = { 1000.0f / 60.0f, 20.0f, 1000.0f * 1001.0f / 48000.0f, 1000.0f / 120.0f, 1000.0f / 144.0f,
                              1000.0f * 1001.0f / 60000.0f, 1000.0f / 72.0f };
    const int codes[] = { 307, 520, 769 };   // ascending, each from the one before (the verify's order), ~1.8 s apart
    const int nCodes = (int)(sizeof(codes) / sizeof(codes[0]));
    // one still-patch sequence: the settled centre per code, plus the centre after a sparse present (k > 64)
    auto run = [&](int host, float P, float cad, unsigned int mode, std::vector<NtShot>& settled, NtShot& sparse) {
        NtDevice dev(hw);
        REQUIRE(dev.ok);
        NtHook* hk = nullptr; NtOverlay* ov = nullptr; NtHost* h = nullptr;
        if (host == 0) { hk = new NtHook(dev, W, H, panel, NtOwnerTuning(mode, P), FALD_CORE_MODE_KC5); REQUIRE(hk->ok); h = hk; }
        else { ov = new NtOverlay(dev, W, H, panel, mode, P, FALD_CORE_MODE_KC5); REQUIRE(ov->ok); h = ov; }
        std::vector<uint16_t> fr;
        const long long t0 = NtQpc();
        double tMs = 0.0;
        auto at = [&](double ms) { g_faldTestNowQpc = t0 + (long long)(ms * (double)f / 1000.0 + 0.5); };
        NtShot s;
        NtFill(fr, W, H, 193);
        at(tMs);
        for (int a = 0; a < 3; a++) if (h->Present(fr, true, false, s)) break;
        settled.clear();
        for (int i = 0; i < nCodes; i++) {
            tMs += 1800.0; at(tMs);
            NtFill(fr, W, H, codes[i]);
            h->Present(fr, true, false, s);
            double lastSettle = tMs;
            for (int j = 0; j < 4000; j++) {           // the settle kicks, then the kicker's 100 ms linger
                tMs += cad; at(tMs);
                h->Present(fr, false, false, s);
                if (s.settle > 0) lastSettle = tMs;
                if (tMs - lastSettle >= 100.0) break;
            }
            CHECK_MESSAGE(s.settle == 0u, "the settle hold did not end (period " << P << ", cadence " << cad << ")");
            settled.push_back(s);
        }
        tMs += 3000.0; at(tMs);                         // another window's present, long after
        h->Present(fr, false, false, sparse);
        g_faldTestNowQpc = 0;
        delete hk; delete ov;
    };
    auto rel = [](const NtShot& a, const NtShot& b) {   // worst channel, relative
        double w = 0.0;
        for (int c = 0; c < 3; c++) w = (std::max)(w, std::fabs(a.c[c] / b.c[c] - 1.0));
        return w;
    };
    auto same = [](const NtShot& a, const NtShot& b) { return a.c[0] == b.c[0] && a.c[1] == b.c[1] && a.c[2] == b.c[2]; };
    std::vector<NtShot> ref, off, cur;
    NtShot refSparse, offSparse, sp;
    double worstOffset = 0.0;
    for (int host = 0; host < 2; host++) {
        // the stateless layer (LED lag off) at 60 Hz: the reference of the light
        run(host, periods[0], periods[0], FALD_TEMPORAL_OFF, off, offSparse);
        for (int rest = 0; rest < 2; rest++) {
            g_faldClockRestOverride = rest ? -1 : 0;
            if (!hw && rest == 0) continue;                     // WARP: the layer as shipped only
            for (float P : periods) {
                if (!hw && P < 16.0f) continue;                 // WARP: 60 / 50 / 47.952 / 59.94 only
                const float cads[] = { P, 1000.0f / 144.0f };   // this monitor's refresh, and a faster one elsewhere
                for (float cad : cads) {
                    if (cad > P + 0.01f) continue;
                    if (!hw && cad != P) continue;              // WARP: this monitor's cadence only
                    run(host, P, cad, FALD_TEMPORAL_PANEL, cur, sp);
                    const std::string id = std::string(host ? "overlay" : "hook") + (rest ? " rest" : " unheld") +
                                           " period " + std::to_string(P) + " cadence " + std::to_string(cad);
                    if (ref.empty()) { ref = cur; refSparse = sp; }
                    // The rates a verify compares (60 / 59.94 / 50 / 47.952) settle to the same output within 2e-5; a much
                    // faster monitor's longer kick linger (100 ms = 14 refreshes at 144 Hz) lets the loop drift a stair
                    // further toward its fixed point (measured: -0.031 % -> -0.006 % in light, 0.097 % in one channel):
                    // 1.5e-3 there, and from the stateless layer.
                    const bool verifyRate = P > 16.0f && cad == P;
                    for (int i = 0; i < nCodes; i++) {
                        const double d = rel(cur[i], ref[i]);
                        CHECK_MESSAGE(d <= (verifyRate ? 2e-5 : 1.5e-3), id << " code " << codes[i] << ": the settled output differs from the hook's at 60 Hz by "
                                                                            << d * 100.0 << " %" << (same(cur[i], ref[i]) ? "" : " (not bit-identical)"));
                        const double o = rel(cur[i], off[i]);
                        worstOffset = (std::max)(worstOffset, o);
                        CHECK_MESSAGE(o <= 1.5e-3, id << " code " << codes[i] << ": settled " << o * 100.0 << " % from the stateless layer");
                    }
                    CHECK_MESSAGE(rel(sp, offSparse) <= 2e-5, id << ": one present after a long pause is " << rel(sp, offSparse) * 100.0 << " % from the stateless layer");
                }
            }
        }
        g_faldClockRestOverride = -1;
    }
    MESSAGE("panel clock on settled full-field greys: one output for the verify's rates, both hosts and rest modes; "
            << worstOffset * 100.0 << " % at most from the stateless layer (the loop's fixed point)");
}
#endif
