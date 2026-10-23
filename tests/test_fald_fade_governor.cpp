// The flank fade's safety governor (shared/fald_fade_governor.h): the budget, the p90 / suspend / retry rules with
// injected timings (pure logic, no device), and on WARP: the timing read never waits, a suspended fade leaves the layer
// exactly C (the layer without the fade), byte for byte, in the overlay and the hook.
#include "doctest.h"
#include "../shared/fald_fade_governor.h"
#include "../shared/fald_fade.h"
#include "../shared/fald_shader.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include <d3dcompiler.h>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {

// n content frames `intervalMs` apart from t, each with a measured fade time `us` (us < 0: no sample)
double Feed(FaldFadeGovernor& g, double t, int n, double intervalMs, double us) {
    for (int i = 0; i < n; i++) {
        g.OnContentFrame(t);
        if (us >= 0.0) g.OnSample(us);
        t += intervalMs;
    }
    return t;
}

FaldFadeGovernor Running(double refreshMs) {
    FaldFadeGovernor g;
    g.Configure(false, false);
    g.SetRefreshMs(refreshMs);
    g.SetMode(1);
    g.SetEnabled(true);
    return g;
}

}  // namespace

TEST_CASE("FALD fade governor: the budget is 15 % of the content interval, never more than the refresh interval") {
    FaldFadeGovernor g = Running(20.854);           // 47.952 Hz
    CHECK(g.State() == FaldFadeGovState::Running);
    CHECK(g.BudgetUs() == 0.0);                       // no interval yet: no verdict either
    Feed(g, 0.0, 40, 41.708, -1.0);                   // 23.976 fps video
    CHECK(g.MedianIntervalMs() == doctest::Approx(41.708));
    CHECK(g.BudgetUs() == doctest::Approx(0.15 * 41.708 * 1000.0));   // 6.26 ms
    FaldFadeGovernor h = Running(6.944);               // 144 Hz desktop, content every refresh
    Feed(h, 0.0, 40, 6.944, -1.0);
    CHECK(h.BudgetUs() == doctest::Approx(0.15 * 6.944 * 1000.0));    // 1.04 ms
    FaldFadeGovernor s = Running(16.667);              // a static desktop: content once a second
    Feed(s, 0.0, 40, 1000.0, -1.0);
    CHECK(s.BudgetUs() == doctest::Approx(16.667 * 1000.0));          // capped at the refresh interval
    FaldFadeGovernor u = Running(0.0);                 // unknown refresh: 60 Hz assumed
    Feed(u, 0.0, 40, 5000.0, -1.0);                    // gaps above 1 s count as 1 s
    CHECK(u.MedianIntervalMs() == doctest::Approx(1000.0));
    CHECK(u.BudgetUs() == doctest::Approx(1000.0 / 60.0 * 1000.0));
}

TEST_CASE("FALD fade governor: the p90 over the window decides, after at least 30 samples") {
    {   // under budget: runs on
        FaldFadeGovernor g = Running(20.854);
        Feed(g, 0.0, 300, 41.708, 5000.0);
        CHECK(g.State() == FaldFadeGovState::Running);
        CHECK(g.Status().p90Us == doctest::Approx(5000.0));
        CHECK(g.Status().samples == 120u);
    }
    {   // over budget: suspended at the 30th sample, not before
        FaldFadeGovernor g = Running(20.854);
        double t = Feed(g, 0.0, 29, 41.708, 7000.0);
        CHECK(g.State() == FaldFadeGovState::Running);
        Feed(g, t, 1, 41.708, 7000.0);
        CHECK(g.State() == FaldFadeGovState::SuspendedBudget);
        const FaldFadeGovernorStatus st = g.Status();
        CHECK(st.p90Us == doctest::Approx(7000.0));
        CHECK(st.budgetUs == doctest::Approx(0.15 * 41.708 * 1000.0));
        CHECK(st.contentMs == doctest::Approx(41.708));
        CHECK_FALSE(g.Allowed());
    }
    {   // the p90, not a spike: 12 of 120 at 50 ms keep it at the rest's 3 ms; a 13th tips it
        FaldFadeGovernor g = Running(20.854);
        double t = 0.0;
        for (int i = 0; i < 120; i++) { g.OnContentFrame(t); g.OnSample(i % 10 == 9 ? 50000.0 : 3000.0); t += 41.708; }
        CHECK(g.State() == FaldFadeGovState::Running);
        CHECK(g.Status().p90Us == doctest::Approx(3000.0));
        g.OnContentFrame(t); g.OnSample(3000.0); t += 41.708;    // pushes out a 3 ms sample (the oldest)
        for (int i = 0; i < 1; i++) { g.OnContentFrame(t); g.OnSample(50000.0); t += 41.708; }
        CHECK(g.State() == FaldFadeGovState::SuspendedBudget);
    }
}

TEST_CASE("FALD fade governor: three content frames in a row over a whole refresh interval suspend at once") {
    FaldFadeGovernor g = Running(16.667);
    double t = Feed(g, 0.0, 2, 41.708, 400000.0);       // a GTX 1060 at 4K: 0.4 s per content frame
    CHECK(g.State() == FaldFadeGovState::Running);       // two: not yet (one could be another app's GPU work)
    Feed(g, t, 1, 41.708, 3000.0);                       // broken run
    t = Feed(g, t + 41.708, 2, 41.708, 400000.0);
    CHECK(g.State() == FaldFadeGovState::Running);
    Feed(g, t, 1, 41.708, 400000.0);
    CHECK(g.State() == FaldFadeGovState::SuspendedBudget);
    CHECK(g.Status().p90Us >= 400000.0 * 0.5);
}

TEST_CASE("FALD fade governor: retries only after a mode change, a re-toggle or a content cadence that makes room") {
    // suspended on a 144 Hz desktop (budget 1.04 ms) with a 3 ms fade
    FaldFadeGovernor g = Running(6.944);
    double t = Feed(g, 0.0, 60, 6.944, 3000.0);
    REQUIRE(g.State() == FaldFadeGovState::SuspendedBudget);
    const uint32_t gen0 = g.Status().generation;
    // the same cadence for a long time: no retry (and samples while suspended are ignored)
    t = Feed(g, t, 2000, 6.944, 100.0);
    CHECK(g.State() == FaldFadeGovState::SuspendedBudget);
    CHECK(g.Status().generation == gen0);
    // 24 fps video: the interval grows x6 and its budget (6.26 ms) >= 1.25 x 3 ms -> a retry once the median of the
    // cadence window (120 content frames) is the video's: after ~61 of its frames
    int videoFrames = 0;
    while (g.State() == FaldFadeGovState::SuspendedBudget && videoFrames < 200) { t = Feed(g, t, 1, 41.708, -1.0); videoFrames++; }
    CHECK(g.State() == FaldFadeGovState::Running);
    CHECK(videoFrames >= 55);
    CHECK(videoFrames <= 65);
    CHECK(g.Status().generation == gen0 + 1);
    {   // ... and never sooner than 120 content frames after the suspension
        FaldFadeGovernor h = Running(6.944);
        double u = Feed(h, 0.0, 30, 6.944, 3000.0);
        REQUIRE(h.State() == FaldFadeGovState::SuspendedBudget);
        u = Feed(h, u, 119, 41.708, -1.0);
        CHECK(h.State() == FaldFadeGovState::SuspendedBudget);
        Feed(h, u, 1, 41.708, -1.0);
        CHECK(h.State() == FaldFadeGovState::Running);
    }
    t = Feed(g, t, 200, 41.708, 3000.0);
    CHECK(g.State() == FaldFadeGovState::Running);
    // back to the desktop: suspended again (a changed cadence), then video again: one retry per change, no ping-pong
    t = Feed(g, t, 200, 6.944, 3000.0);
    CHECK(g.State() == FaldFadeGovState::SuspendedBudget);
    t = Feed(g, t, 300, 6.944, -1.0);
    CHECK(g.State() == FaldFadeGovState::SuspendedBudget);
    {   // a cadence that grows but leaves no room (the fade 6 ms: 6.26 < 1.25 x 6): stays suspended
        FaldFadeGovernor h = Running(6.944);
        double u = Feed(h, 0.0, 60, 6.944, 6000.0);
        REQUIRE(h.State() == FaldFadeGovState::SuspendedBudget);
        Feed(h, u, 400, 41.708, -1.0);
        CHECK(h.State() == FaldFadeGovState::SuspendedBudget);
        // ... a mode change retries (the cost is measured again), the same mode does not
        h.SetMode(1);
        CHECK(h.State() == FaldFadeGovState::SuspendedBudget);
        h.SetMode(2);
        CHECK(h.State() == FaldFadeGovState::Running);
        CHECK(h.Status().samples == 0u);
    }
    {   // re-toggle: off, then on
        FaldFadeGovernor h = Running(6.944);
        Feed(h, 0.0, 60, 6.944, 6000.0);
        REQUIRE(h.State() == FaldFadeGovState::SuspendedBudget);
        h.SetEnabled(true);                                 // no change: nothing
        CHECK(h.State() == FaldFadeGovState::SuspendedBudget);
        h.SetEnabled(false);
        CHECK(h.State() == FaldFadeGovState::Off);
        h.SetEnabled(true);
        CHECK(h.State() == FaldFadeGovState::Running);
    }
    {   // a retry that does not fit after all is suspended again, and the next retry needs ANOTHER growth of the cadence
        FaldFadeGovernor h = Running(20.854);
        double u = Feed(h, 0.0, 60, 6.944, 3000.0);        // suspended at 6.94 ms (p90 3 ms)
        REQUIRE(h.State() == FaldFadeGovState::SuspendedBudget);
        u = Feed(h, u, 130, 41.708, -1.0);                  // video: retry
        REQUIRE(h.State() == FaldFadeGovState::Running);
        u = Feed(h, u, 40, 41.708, 7000.0);                 // the cost was more: suspended again at 41.7 ms
        REQUIRE(h.State() == FaldFadeGovState::SuspendedBudget);
        u = Feed(h, u, 1000, 41.708, -1.0);                 // the same cadence: no retry
        CHECK(h.State() == FaldFadeGovState::SuspendedBudget);
    }
}

TEST_CASE("FALD fade governor: a weak adapter starts suspended and stays; forced never suspends") {
    FaldFadeGovernor w;
    w.Configure(true, false);
    w.SetMode(1);
    w.SetEnabled(true);
    CHECK(w.State() == FaldFadeGovState::SuspendedWeakGpu);
    CHECK_FALSE(w.Allowed());
    w.SetMode(2);
    CHECK(w.State() == FaldFadeGovState::SuspendedWeakGpu);   // a mode change is no reason
    w.SetEnabled(false); w.SetEnabled(true);
    CHECK(w.State() == FaldFadeGovState::SuspendedWeakGpu);   // nor a re-toggle
    Feed(w, 0.0, 300, 1000.0, -1.0);
    CHECK(w.State() == FaldFadeGovState::SuspendedWeakGpu);   // nor any cadence
    w.Configure(true, true);                                   // the INI force key
    CHECK(w.State() == FaldFadeGovState::Forced);
    CHECK(w.Allowed());
    Feed(w, 0.0, 300, 6.944, 900000.0);
    CHECK(w.State() == FaldFadeGovState::Forced);
    CHECK(w.Status().p90Us == doctest::Approx(900000.0));      // still measured
    w.Configure(false, false);
    CHECK(w.State() == FaldFadeGovState::Running);
}

TEST_CASE("FALD fade governor: every state change bumps the generation once") {
    FaldFadeGovernor g;
    const uint32_t g0 = g.Status().generation;
    g.Configure(false, false);
    g.SetEnabled(true);
    CHECK(g.Status().generation == g0 + 1);                     // Off -> Running
    g.SetMode(5);
    CHECK(g.Status().generation == g0 + 1);                     // Running -> Running: no news
    Feed(g, 0.0, 40, 6.944, 3000.0);
    CHECK(g.Status().generation == g0 + 2);                     // -> suspended
    g.SetEnabled(false);
    CHECK(g.Status().generation == g0 + 3);                     // -> Off
    CHECK(std::string(FaldFadeGovStateName(FaldFadeGovState::SuspendedBudget)) == "suspended_budget");
    CHECK(std::string(FaldFadeGovStateName(FaldFadeGovState::SuspendedWeakGpu)) == "suspended_weak_gpu");
}

// ---------------------------------------------------------------------------------------------------------------------
// WARP: the timing read never waits; a suspended fade is C, byte for byte (the overlay; the hook: test_fald_hook.cpp)
// ---------------------------------------------------------------------------------------------------------------------
namespace {

std::string GovEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

std::wstring GovOwnerPanel(const wchar_t* rel) {
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir = exe;
    for (int up = 0; up < 7; up++) {
        const size_t slash = dir.find_last_of(L"\\/");
        if (slash == std::wstring::npos) break;
        dir.resize(slash);
        const std::wstring p = dir + L"\\" + rel;
        if (GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES) return p;
    }
    const std::wstring p = std::wstring(L"H:\\Projects\\DesktopLUT\\") + rel;
    return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES ? p : std::wstring();
}

struct GovWarp {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11VertexShader* vs = nullptr;
    bool ok = false;
    GovWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main",
                              "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~GovWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

std::vector<uint8_t> GovReadBack(ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return out;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.resize((size_t)d.Width * d.Height * 8);
        for (UINT y = 0; y < d.Height; y++)
            std::memcpy(out.data() + (size_t)y * d.Width * 8, (const uint8_t*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * 8);
        g_context->Unmap(st, 0);
    }
    st->Release();
    return out;
}

// The odd-sized cell on its own panel file (DLC tests/fald_fade_cells.py): small enough for WARP in a unit test.
struct OddCell { std::string dir; std::vector<uint16_t> frame; bool ok = false; };
OddCell LoadOdd() {
    OddCell c;
    c.dir = GovEnv("FALD_TEST_FADE_DIR");
    if (c.dir.empty()) return c;
    c.frame.assign((size_t)1366 * 768 * 4, 0);
    std::ifstream f(c.dir + "/x_odd1366.rgba16f", std::ios::binary);
    if (!f) return c;
    f.read((char*)c.frame.data(), (std::streamsize)(c.frame.size() * 2));
    c.ok = (size_t)f.gcount() == c.frame.size() * 2;
    return c;
}

// Runs `presents` presents of the odd cell (the first `content` of them with new content) on a fresh context; returns
// the output of the last one.
std::vector<uint8_t> RunOdd(GovWarp& w, const OddCell& c, bool fade, bool force, int presents, MonitorContext** keep = nullptr,
                            double* lastCollectMs = nullptr) {
    g_faldFadeForce.store(force);
    MonitorContext* ctx = new MonitorContext();
    ctx->width = 1366; ctx->height = 768; ctx->isHDREnabled = true; ctx->isFP16SDR = false;
    ctx->swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx->frameTimeExactMs = 16.667f;
    FaldSettings& fs = ctx->hdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = std::wstring(c.dir.begin(), c.dir.end()) + L"\\odd_panel.bin"; fs.star.enabled = true;
    fs.coreMode = FALD_CORE_MODE_C;   // the always-on core off: this case holds the layer to its C reference
    fs.flankFade = fade;
    std::vector<uint8_t> out;
    if (!FaldEnsureResources(ctx, fs)) { delete ctx; return out; }
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = 1366; td.Height = 768; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    w.dev->CreateTexture2D(&td, nullptr, &target);
    w.dev->CreateRenderTargetView(target, nullptr, &rtv);
    for (int i = 0; i < presents; i++) {
        D3D11_VIEWPORT vp = { 0, 0, 1366.0f, 768.0f, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx->fald->inter, 0, nullptr, c.frame.data(), 1366u * 8u, 0);
        FaldRunPasses(ctx, rtv, true, 1000u + (unsigned)i);
        if (lastCollectMs && ctx->fald->fade && ctx->fald->fadeOn) {
            // straight after the submission, before any flush: the GPU (WARP) cannot be done; the read must not wait
            double us[4];
            const auto t0 = std::chrono::steady_clock::now();
            FaldFadeCollectTiming(ctx->fald->fade, w.ic, us, 4);
            *lastCollectMs = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
        }
        w.ic->Flush();
    }
    out = GovReadBack(target);
    rtv->Release(); target->Release();
    if (keep) *keep = ctx;
    else { FaldReleaseResources(ctx); delete ctx; }
    return out;
}

}  // namespace

TEST_CASE("FALD fade governor on WARP: a suspended fade is C byte for byte; the timing read never waits (FALD_TEST_FADE_DIR)") {
    const OddCell c = LoadOdd();
    if (!c.ok) return;
    GovWarp w;
    REQUIRE(w.ok);
    // WARP is a software adapter: weak for the fade (no dedicated VRAM) — the governor never starts it
    std::string desc;
    CHECK(FaldFadeWeakAdapter(w.dev, &desc));
    MESSAGE("WARP: " << desc);
    const std::vector<uint8_t> C = RunOdd(w, c, false, false, 3);
    MonitorContext* ctx = nullptr;
    const std::vector<uint8_t> suspended = RunOdd(w, c, true, false, 3, &ctx);
    REQUIRE(ctx);
    CHECK(ctx->fald->fadeGov.State() == FaldFadeGovState::SuspendedWeakGpu);
    CHECK_FALSE(ctx->fald->fadeOn);
    CHECK(ctx->fald->fade == nullptr);                   // nothing built, no VRAM
    FaldReleaseResources(ctx); delete ctx;
    REQUIRE(!C.empty());
    CHECK(suspended == C);                               // the layer = C, every byte
    // forced on (the INI key): the fade runs; the timing read right after a submission returns at once
    double collectMs = -1.0;
    ctx = nullptr;
    const std::vector<uint8_t> forced = RunOdd(w, c, true, true, 30, &ctx, &collectMs);
    REQUIRE(ctx);
    CHECK(ctx->fald->fadeGov.State() == FaldFadeGovState::Forced);
    CHECK(ctx->fald->fadeOn);
    CHECK(forced != C);                                  // the fade does something on this frame
    MESSAGE("FaldFadeCollectTiming straight after a submission: " << collectMs << " ms");
    CHECK(collectMs >= 0.0);
    CHECK(collectMs < 5.0);
    // ... and once the GPU is done, the content frames' times arrive (WARP: seconds per frame at most)
    g_context->Flush();
    unsigned int got = 0;
    const auto t0 = std::chrono::steady_clock::now();
    while (got == 0 && std::chrono::steady_clock::now() - t0 < std::chrono::seconds(60)) {
        double us[4];
        got = FaldFadeCollectTiming(ctx->fald->fade, g_context, us, 4);
        if (got) { MESSAGE("a content frame of the fade on WARP (1366 x 768): " << us[0] / 1000.0 << " ms"); CHECK(us[0] > 0.0); }
        else Sleep(10);
    }
    CHECK(got > 0u);
    FaldReleaseResources(ctx); delete ctx;
    g_faldFadeForce.store(false);
}
