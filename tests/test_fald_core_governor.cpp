// The core layer's rate-cap governor (shared/fald_core_governor.h): the budget, the p90 / cap / spacing / release rules
// with injected timings (pure logic, no device), and on WARP: a held run (the cap's run between full runs) keeps every
// field texture of the previous run, reproduces the full run's output on the same frame, and leaves no residue (the full
// run after it equals a fresh full run).
#include "doctest.h"
#include "../shared/fald_core_governor.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_change.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include <d3dcompiler.h>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {

// n full runs `intervalMs` apart from t, each a content frame with a measured core time `us` (us < 0: no sample)
double Feed(FaldCoreGovernor& g, double t, int n, double intervalMs, double us) {
    for (int i = 0; i < n; i++) {
        g.OnContentFrame(t);
        if (g.FullAllowed(t)) { g.OnFullRun(t); if (us >= 0.0) g.OnSample(us); }
        t += intervalMs;
    }
    return t;
}

FaldCoreGovernor Running(double refreshMs) {
    FaldCoreGovernor g;
    g.Configure(false);
    g.SetRefreshMs(refreshMs);
    g.SetMode(1);
    g.SetEnabled(true);
    return g;
}

}  // namespace

TEST_CASE("FALD core governor: the budget is 15 % of the refresh interval") {
    FaldCoreGovernor g = Running(20.854);                            // 47.952 Hz
    CHECK(g.State() == FaldCoreGovState::Running);
    CHECK(g.BudgetUs() == doctest::Approx(0.15 * 20.854 * 1000.0));  // 3.13 ms
    FaldCoreGovernor h = Running(6.944);                             // 144 Hz
    CHECK(h.BudgetUs() == doctest::Approx(0.15 * 6.944 * 1000.0));   // 1.04 ms
    FaldCoreGovernor u = Running(0.0);                               // unknown refresh: 60 Hz assumed
    CHECK(u.BudgetUs() == doctest::Approx(0.15 * 1000.0 / 60.0 * 1000.0));
    CHECK(u.Status().refreshMs == doctest::Approx(1000.0 / 60.0));
    Feed(u, 0.0, 40, 41.708, -1.0);                                  // the content interval is information only
    CHECK(u.MedianIntervalMs() == doctest::Approx(41.708));
    CHECK(u.BudgetUs() == doctest::Approx(0.15 * 1000.0 / 60.0 * 1000.0));
}

TEST_CASE("FALD core governor: the p90 over the window decides, after at least 30 samples; capped = full runs every p90 / 15 %") {
    {   // the RTX 5090 at 4K: 0.5 ms per full run, budget 3.1 ms at 47.952 Hz: never capped
        FaldCoreGovernor g = Running(20.854);
        Feed(g, 0.0, 300, 20.854, 500.0);
        CHECK(g.State() == FaldCoreGovState::Running);
        CHECK(g.Status().p90Us == doctest::Approx(500.0));
        CHECK(g.Status().samples == 120u);
        CHECK(g.Status().spacingMs == 0.0);
    }
    {   // a GTX 1060 at 4K: 13 ms per full run, budget 2.5 ms at 60 Hz: capped at the 30th sample, not before
        FaldCoreGovernor g = Running(16.667);
        double t = Feed(g, 0.0, 29, 16.667, 13000.0);
        CHECK(g.State() == FaldCoreGovState::Running);
        Feed(g, t, 1, 16.667, 13000.0);
        CHECK(g.State() == FaldCoreGovState::Capped);
        const FaldCoreGovernorStatus st = g.Status();
        CHECK(st.p90Us == doctest::Approx(13000.0));
        CHECK(st.budgetUs == doctest::Approx(0.15 * 16.667 * 1000.0));
        CHECK(st.spacingMs == doctest::Approx(13.0 / 0.15));        // 86.7 ms: the average share stays at 15 %
        // the cap: a full run every 5 presents (86.7 / 16.667 = 5.2 -> the 6th present is >= spacing - half a refresh)
        t += 16.667;
        int fulls = 0, presents = 0;
        for (; presents < 120; presents++, t += 16.667) {
            g.OnContentFrame(t);
            if (g.FullAllowed(t)) { g.OnFullRun(t); g.OnSample(13000.0); fulls++; }
        }
        CHECK(fulls >= 20); CHECK(fulls <= 24);                       // 120 / 5.2 = 23
        CHECK(g.State() == FaldCoreGovState::Capped);
    }
    {   // the p90, not a spike: 12 of 120 at 50 ms keep it at the rest's 2 ms; a 13th tips it
        FaldCoreGovernor g = Running(20.854);
        double t = 0.0;
        for (int i = 0; i < 120; i++) { g.OnContentFrame(t); g.OnFullRun(t); g.OnSample(i % 10 == 9 ? 50000.0 : 2000.0); t += 41.708; }
        CHECK(g.State() == FaldCoreGovState::Running);
        CHECK(g.Status().p90Us == doctest::Approx(2000.0));
        g.OnContentFrame(t); g.OnFullRun(t); g.OnSample(2000.0); t += 41.708;    // pushes out a 2 ms sample (the oldest)
        g.OnContentFrame(t); g.OnFullRun(t); g.OnSample(50000.0);
        CHECK(g.State() == FaldCoreGovState::Capped);
    }
}

TEST_CASE("FALD core governor: three full runs in a row over a whole refresh interval cap at once") {
    FaldCoreGovernor g = Running(16.667);
    double t = Feed(g, 0.0, 2, 16.667, 400000.0);        // an iGPU at 4K: 0.4 s per full run
    CHECK(g.State() == FaldCoreGovState::Running);        // two: not yet (one could be another app's GPU work)
    Feed(g, t, 1, 16.667, 3000.0);                        // broken run
    t = Feed(g, t + 16.667, 2, 16.667, 400000.0);
    CHECK(g.State() == FaldCoreGovState::Running);
    Feed(g, t, 1, 16.667, 400000.0);
    CHECK(g.State() == FaldCoreGovState::Capped);
    CHECK(g.Status().p90Us >= 400000.0 * 0.5);
    CHECK(g.Status().spacingMs == doctest::Approx(1000.0));   // never held longer than a second (0.4 s / 0.15 = 2.7 s)
}

TEST_CASE("FALD core governor: capped runs keep being measured; released with 25 % headroom, re-tuned otherwise") {
    FaldCoreGovernor g = Running(16.667);                 // budget 2.5 ms
    double t = Feed(g, 0.0, 60, 16.667, 5000.0);          // capped at 5 ms: a full run every 33 ms
    REQUIRE(g.State() == FaldCoreGovState::Capped);
    CHECK(g.Status().spacingMs == doctest::Approx(5.0 / 0.15));
    const uint32_t gen0 = g.Status().generation;
    // the content got dearer: the spacing follows the p90 of the capped full runs (no state change)
    t = Feed(g, t, 2000, 16.667, 10000.0);
    CHECK(g.State() == FaldCoreGovState::Capped);
    CHECK(g.Status().spacingMs == doctest::Approx(10.0 / 0.15));
    CHECK(g.Status().generation == gen0);
    // cheaper, but not with headroom (2.2 ms x 1.25 > 2.5): stays capped
    t = Feed(g, t, 2000, 16.667, 2200.0);
    CHECK(g.State() == FaldCoreGovState::Capped);
    // with headroom (1.9 ms x 1.25 <= 2.5): released once 30 capped full runs say so
    t = Feed(g, t, 2000, 16.667, 1900.0);
    CHECK(g.State() == FaldCoreGovState::Running);
    CHECK(g.Status().generation == gen0 + 1);
    CHECK(g.Status().spacingMs == 0.0);
    // ... and every present runs the full path again
    int fulls = 0;
    for (int i = 0; i < 50; i++, t += 16.667) { g.OnContentFrame(t); if (g.FullAllowed(t)) { g.OnFullRun(t); g.OnSample(1900.0); fulls++; } }
    CHECK(fulls == 50);
}

TEST_CASE("FALD core governor: a mode change or a re-enable measures afresh; forced never caps; off ignores samples") {
    FaldCoreGovernor g = Running(16.667);
    double t = Feed(g, 0.0, 60, 16.667, 5000.0);
    REQUIRE(g.State() == FaldCoreGovState::Capped);
    g.SetMode(1);                                          // the same mode: nothing
    CHECK(g.State() == FaldCoreGovState::Capped);
    g.SetMode(2);
    CHECK(g.State() == FaldCoreGovState::Running);
    CHECK(g.Status().samples == 0u);
    t = Feed(g, t, 60, 16.667, 5000.0);
    REQUIRE(g.State() == FaldCoreGovState::Capped);
    g.SetEnabled(true);                                    // no change: nothing
    CHECK(g.State() == FaldCoreGovState::Capped);
    g.SetEnabled(false);
    CHECK(g.State() == FaldCoreGovState::Off);
    CHECK(g.FullAllowed(t));                               // off: the caller does not run the layer anyway
    g.OnSample(5000.0);
    CHECK(g.Status().samples == 0u);
    g.SetEnabled(true);
    CHECK(g.State() == FaldCoreGovState::Running);
    // forced: measured, never capped
    FaldCoreGovernor f;
    f.Configure(true);
    f.SetRefreshMs(6.944);
    f.SetMode(1);
    f.SetEnabled(true);
    CHECK(f.State() == FaldCoreGovState::Forced);
    Feed(f, 0.0, 300, 6.944, 900000.0);
    CHECK(f.State() == FaldCoreGovState::Forced);
    CHECK(f.Status().p90Us == doctest::Approx(900000.0));
    CHECK(f.FullAllowed(1e9));
    f.Configure(false);                                    // the key withdrawn: measured afresh
    CHECK(f.State() == FaldCoreGovState::Running);
    CHECK(f.Status().samples == 0u);
}

TEST_CASE("FALD core governor: every state change bumps the generation once; the names") {
    FaldCoreGovernor g;
    const uint32_t g0 = g.Status().generation;
    g.Configure(false);
    g.SetEnabled(true);
    CHECK(g.Status().generation == g0 + 1);                // Off -> Running
    g.SetMode(5);
    CHECK(g.Status().generation == g0 + 1);                // Running -> Running: no news
    Feed(g, 0.0, 40, 6.944, 3000.0);
    CHECK(g.Status().generation == g0 + 2);                // -> capped
    g.SetEnabled(false);
    CHECK(g.Status().generation == g0 + 3);                // -> Off
    CHECK(std::string(FaldCoreGovStateName(FaldCoreGovState::Off)) == "off");
    CHECK(std::string(FaldCoreGovStateName(FaldCoreGovState::Running)) == "running");
    CHECK(std::string(FaldCoreGovStateName(FaldCoreGovState::Capped)) == "capped");
    CHECK(std::string(FaldCoreGovStateName(FaldCoreGovState::Forced)) == "forced");
}

// ---------------------------------------------------------------------------------------------------------------------
// WARP: a held run keeps the fields, reproduces the output on the same frame, leaves no residue (the overlay; the hook:
// test_fald_hook.cpp)
// ---------------------------------------------------------------------------------------------------------------------
namespace {

std::string CgEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

struct CgWarp {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr; ID3D11VertexShader* vs = nullptr; bool ok = false;
    CgWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~CgWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

std::vector<uint8_t> CgReadBack(ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    if (!tex) return out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    UINT bpp = 0;
    switch (d.Format) {
    case DXGI_FORMAT_R32_FLOAT: bpp = 4; break;
    case DXGI_FORMAT_R32G32_FLOAT: case DXGI_FORMAT_R16G16B16A16_FLOAT: bpp = 8; break;
    case DXGI_FORMAT_R32G32B32A32_FLOAT: bpp = 16; break;
    default: return out;
    }
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return out;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.resize((size_t)d.Width * d.Height * bpp);
        for (UINT y = 0; y < d.Height; y++)
            std::memcpy(out.data() + (size_t)y * d.Width * bpp, (const uint8_t*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * bpp);
        g_context->Unmap(st, 0);
    }
    st->Release();
    return out;
}
uint64_t CgFnv(const std::vector<uint8_t>& b) { uint64_t h = 1469598103934665603ull; for (uint8_t c : b) { h ^= c; h *= 1099511628211ull; } return h; }

struct CgLayer {
    MonitorContext ctx;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    bool ok = false;
    CgLayer(CgWarp& w, const std::wstring& panel, int fw, int fh) {
        ctx.width = fw; ctx.height = fh; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = 16.667f;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)fw; td.Height = (UINT)fh; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(w.dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(w.dev->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~CgLayer() { if (rtv) rtv->Release(); if (target) target->Release(); FaldReleaseResources(&ctx); }
    // upload the whole frame, run (new content, an unknown frame: no partial path), the hashes of every field + the output
    std::vector<std::pair<std::string, uint64_t>> Run(CgWarp& w, const std::vector<uint16_t>& frame, unsigned long long key) {
        D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        FaldRunPasses(&ctx, rtv, true, key);
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        const std::pair<const char*, ID3D11Texture2D*> tex[] = {
            { "drive0", r->drive0Tex }, { "drive", r->driveTex }, { "drive_filt0", r->driveFilt0Tex }, { "drive_filt", r->driveFiltTex },
            { "drive_state", r->driveStateTex }, { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] },
            { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] }, { "btrue0", r->bTrue0Tex }, { "best0", r->bEst0Tex },
            { "gain0", r->gain0Tex }, { "btrue", r->bTrueTex }, { "best", r->bEstTex }, { "gainB", r->gainBTex },
            { "fields0", r->fields0Tex }, { "fields", r->fieldsTex }, { "OUTPUT", target } };
        std::vector<std::pair<std::string, uint64_t>> out;
        for (const auto& t : tex) if (t.second) out.emplace_back(t.first, CgFnv(CgReadBack(t.second)));
        return out;
    }
};
void CgPatch(std::vector<uint16_t>& f, int W, int l, int t, int r, int b, uint16_t value) {
    for (int y = t; y < b; y++)
        for (int x = l; x < r; x++)
            for (int c = 0; c < 3; c++) f[((size_t)y * W + x) * 4 + c] = value;
}
bool CgSameFields(const std::vector<std::pair<std::string, uint64_t>>& a, const std::vector<std::pair<std::string, uint64_t>>& b) {
    if (a.size() != b.size()) return false;
    for (size_t i = 0; i < a.size(); i++) if (a[i].first != "OUTPUT" && a[i] != b[i]) return false;
    return true;
}
uint64_t CgOutput(const std::vector<std::pair<std::string, uint64_t>>& a) { for (const auto& p : a) if (p.first == "OUTPUT") return p.second; return 0; }

}  // namespace

TEST_CASE("FALD core governor: a held run keeps the fields, reproduces the output on the same frame, leaves no residue (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = CgEnv("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::wstring C = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";
    if (GetFileAttributesW(C.c_str()) == INVALID_FILE_ATTRIBUTES) return;
    const int W = 3840, H = 2160;
    std::vector<uint16_t> A((size_t)W * H * 4);
    {
        std::ifstream f(cellDir + "/n7_lamp.rgba16f", std::ios::binary);
        REQUIRE(f.good());
        f.read((char*)A.data(), (std::streamsize)(A.size() * 2));
        REQUIRE((size_t)f.gcount() == A.size() * 2);
    }
    std::vector<uint16_t> B = A;
    CgPatch(B, W, 300, 300, 500, 500, 0x3C00);                         // a 200 x 200 white patch: the drives move
    CgWarp w;
    REQUIRE(w.ok);
    const int saved = g_faldCoreHoldOverride;
    {
        g_faldCoreHoldOverride = 0;
        CgLayer lay(w, C, W, H);
        REQUIRE(lay.ok);
        auto fullA = lay.Run(w, A, 1);                                 // a full run (no state to hold yet)
        CHECK(lay.ctx.fald->heldRuns == 0ull);
        CHECK(lay.ctx.fald->fieldsStand);
        g_faldCoreHoldOverride = 1;
        auto heldA = lay.Run(w, A, 2);                                 // held on the same frame: the same output, the same fields
        CHECK(lay.ctx.fald->heldRuns == 1ull);
        CHECK(CgSameFields(fullA, heldA));
        CHECK(CgOutput(heldA) == CgOutput(fullA));
        CHECK_FALSE(lay.ctx.fald->stateComplete);                      // the standing textures are not this frame's
        CHECK(lay.ctx.fald->fieldsStand);
        auto heldB = lay.Run(w, B, 3);                                 // held on a new frame: A's fields, B's picture
        CHECK(lay.ctx.fald->heldRuns == 2ull);
        CHECK(CgSameFields(fullA, heldB));
        CHECK(CgOutput(heldB) != CgOutput(fullA));
        g_faldCoreHoldOverride = 0;
        auto fullB = lay.Run(w, B, 4);                                 // the full run after: B's fields
        CHECK(lay.ctx.fald->heldRuns == 2ull);
        CHECK_FALSE(CgSameFields(fullA, fullB));
        CHECK(CgOutput(fullB) != CgOutput(heldB));
        // no residue: a fresh context's full run on B is the same, field for field and in the output
        CgLayer fresh(w, C, W, H);
        REQUIRE(fresh.ok);
        auto freshB = fresh.Run(w, B, 1);
        CHECK(freshB == fullB);
    }
    {   // the governor itself: the first run (nothing stands) is always full, whatever it says; off the seam it runs
        g_faldCoreHoldOverride = -1;
        CgLayer lay(w, C, W, H);
        REQUIRE(lay.ok);
        lay.Run(w, A, 1);
        CHECK(lay.ctx.fald->heldRuns == 0ull);
        CHECK(lay.ctx.fald->coreGov.State() == FaldCoreGovState::Running);
    }
    g_faldCoreHoldOverride = saved;
}
