// The convolution pass's variant auto-tune (shared/fald_conv_tune.h): the tuner's schedule and decision with injected
// timings, the cache file's round trip, the key; and on WARP: a layer tunes itself within a few dozen full runs, caches
// the decision, starts from the cache next time, and its output is the same bytes whichever variant it lands on.
#include "doctest.h"
#include "../shared/fald_conv_tune.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_panel.h"
#include "../shared/fald_core_governor.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include <d3dcompiler.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

TEST_CASE("FALD conv tune: the schedule is round-robin, the decision the fastest median after a warm-up each") {
    FaldConvTuner t;
    CHECK(t.GetState() == FaldConvTuner::State::Policy);
    t.Start(1);
    CHECK(t.Active());
    CHECK(t.Variant() == 1u);                                   // the policy's until decided
    CHECK(t.NextVariant() == 0u);
    t.OnSample(0, 100.0);
    CHECK(t.NextVariant() == 1u);                               // the fewest samples next
    t.OnSample(1, 100.0); t.OnSample(2, 100.0);
    CHECK(t.NextVariant() == 3u);
    t.OnSample(3, 100.0);
    CHECK(t.NextVariant() == 0u);
    CHECK_FALSE(t.Decide());
    // the warm-up sample (the first of each) is discarded: variant 2's slow first dispatch does not count
    const double us[4] = { 120.0, 95.0, 90.0, 140.0 };
    for (unsigned int k = 0; k < FALD_CONV_TUNE_SAMPLES; k++)
        for (unsigned int v = 0; v < 4; v++) t.OnSample(v, us[v] + (k == 2 ? 1000.0 : 0.0));   // one spike each: the median ignores it
    CHECK(t.Decide());
    CHECK(t.GetState() == FaldConvTuner::State::Tuned);
    CHECK(t.Variant() == 2u);
    CHECK(t.Medians()[0] == doctest::Approx(120.0));
    CHECK(t.Medians()[2] == doctest::Approx(90.0));
    CHECK(t.Decide());                                          // stays decided
    t.OnSample(0, 1.0);                                         // ignored once decided
    CHECK(t.Variant() == 2u);
    // a sample above the cap is ignored; a negative one too
    FaldConvTuner u;
    u.Start(0);
    for (int i = 0; i < 40; i++) for (unsigned int v = 0; v < 4; v++) u.OnSample(v, v == 3 ? 10.0 : 50.0);
    u.OnSample(3, -5.0);
    CHECK(u.Decide());
    CHECK(u.Variant() == 3u);
    // the cache's record: used as it is
    FaldConvTuneRecord rec; rec.key = "k"; rec.variant = 2; rec.us[0] = 1; rec.us[1] = 2; rec.us[2] = 3; rec.us[3] = 4;
    FaldConvTuner c;
    c.UseCached(rec);
    CHECK(c.GetState() == FaldConvTuner::State::Cached);
    CHECK(c.Variant() == 2u);
    CHECK(c.Medians()[3] == doctest::Approx(4.0));
    CHECK_FALSE(c.Active());
    CHECK(std::string(FaldConvTuner::StateName(FaldConvTuner::State::Tuning)) == "tuning");
}

TEST_CASE("FALD conv tune: the cache file round-trips, replaces a key's line, keeps at most 64 lines") {
    char tmp[MAX_PATH] = {};
    GetTempPathA(MAX_PATH, tmp);
    const std::string path = std::string(tmp) + "DesktopLUT_fald_conv_tune_test_" + std::to_string(GetCurrentProcessId()) + ".txt";
    DeleteFileA(path.c_str());
    FaldConvTuneRecord r;
    CHECK_FALSE(FaldConvTuneCacheLookup(path, "a", r));         // no file: no cache
    FaldConvTuneRecord a; a.key = "NVIDIA GeForce RTX 5090|10de:2b85|32.0.16.1742|48x48|sub8|kt27x25|ke17x17"; a.variant = 1;
    a.us[0] = 123.4; a.us[1] = 98.7; a.us[2] = 140.0; a.us[3] = 210.5;
    REQUIRE(FaldConvTuneCacheStore(path, a));
    REQUIRE(FaldConvTuneCacheLookup(path, a.key, r));
    CHECK(r.variant == 1u);
    CHECK(r.us[1] == doctest::Approx(98.7));
    CHECK(r.us[3] == doctest::Approx(210.5));
    CHECK_FALSE(FaldConvTuneCacheLookup(path, "other", r));
    a.variant = 2;
    REQUIRE(FaldConvTuneCacheStore(path, a));                   // replaced, not duplicated
    REQUIRE(FaldConvTuneCacheLookup(path, a.key, r));
    CHECK(r.variant == 2u);
    {
        std::ifstream f(path); std::string line; int n = 0;
        while (std::getline(f, line)) if (!line.empty()) n++;
        CHECK(n == 1);
    }
    for (int i = 0; i < 70; i++) { FaldConvTuneRecord b; b.key = "k" + std::to_string(i); b.variant = 0; REQUIRE(FaldConvTuneCacheStore(path, b)); }
    CHECK_FALSE(FaldConvTuneCacheLookup(path, a.key, r));       // the oldest dropped past 64 lines
    CHECK(FaldConvTuneCacheLookup(path, "k69", r));
    CHECK_FALSE(FaldConvTuneCacheLookup(path, "k5", r));
    CHECK(FaldConvTuneCacheLookup(path, "k6", r));
    // a damaged line is skipped, the rest read
    { std::ofstream f(path, std::ios::app); f << "v1\tbroken\t9\t1\t2\t3\t4\nnonsense\n"; }
    CHECK(FaldConvTuneCacheLookup(path, "k69", r));
    FaldConvTuneRecord bad; bad.key = "x"; bad.variant = 7;
    CHECK_FALSE(FaldConvTuneCacheStore(path, bad));
    CHECK_FALSE(FaldConvTuneCacheStore("-", a));                // "-": no cache
    CHECK_FALSE(FaldConvTuneCacheLookup("-", a.key, r));
    DeleteFileA(path.c_str());
    // the key: the device part + the geometry
    CHECK(FaldConvTuneKey("dev|1:2|3.4.5.6", 48, 24, 8, 27, 25, 17, 17) == "dev|1:2|3.4.5.6|48x24|sub8|kt27x25|ke17x17");
}

// ---------------------------------------------------------------------------------------------------------------------
// WARP: a layer tunes itself, caches it, starts from the cache, and the output is the same whichever variant it lands on
// ---------------------------------------------------------------------------------------------------------------------
namespace {

std::string CtEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

struct CtWarp {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr; ID3D11VertexShader* vs = nullptr; bool ok = false;
    CtWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~CtWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

std::vector<uint8_t> CtReadBack(ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    if (!tex) return out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    const UINT bpp = 8;   // R16G16B16A16_FLOAT
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

struct CtLayer {
    MonitorContext ctx;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    bool ok = false;
    CtLayer(CtWarp& w, const std::wstring& panel, int fw, int fh) {
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
    ~CtLayer() { if (rtv) rtv->Release(); if (target) target->Release(); FaldReleaseResources(&ctx); }
    // a full run on the frame (new content, an unknown frame): the output's bytes
    std::vector<uint8_t> Run(CtWarp& w, const std::vector<uint16_t>& frame, unsigned long long key, bool wait) {
        D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        FaldRunPasses(&ctx, rtv, true, key);
        if (wait) return CtReadBack(target);   // (the read-back waits for the GPU: every query of the run is then finished)
        w.ic->Flush();
        return std::vector<uint8_t>();
    }
};

}  // namespace

TEST_CASE("FALD conv tune: a layer tunes itself on WARP, caches it, starts from the cache; the output is the same bytes (FALD_TEST_FADE_DIR)") {
    const std::string cells = CtEnv("FALD_TEST_FADE_DIR");
    if (cells.empty()) return;
    const std::wstring panel = std::wstring(cells.begin(), cells.end()) + L"\\odd_panel.bin";
    if (GetFileAttributesW(panel.c_str()) == INVALID_FILE_ATTRIBUTES) return;
    const int W = 1366, H = 768;
    std::vector<uint16_t> A((size_t)W * H * 4), B;
    {
        std::ifstream f(cells + "/x_odd1366.rgba16f", std::ios::binary);
        REQUIRE(f.good());
        f.read((char*)A.data(), (std::streamsize)(A.size() * 2));
        REQUIRE((size_t)f.gcount() == A.size() * 2);
    }
    B = A;
    for (int y = 100; y < 300; y++) for (int x = 100; x < 300; x++) for (int c = 0; c < 3; c++) B[((size_t)y * W + x) * 4 + c] = 0x3C00;
    char tmp[MAX_PATH] = {};
    GetTempPathA(MAX_PATH, tmp);
    const std::string cache = std::string(tmp) + "DesktopLUT_fald_conv_tune_warp_" + std::to_string(GetCurrentProcessId()) + ".txt";
    DeleteFileA(cache.c_str());
    const int savedTune = g_faldConvTuneOverride, savedVar = g_faldConvVariantOverride;
    const std::string savedPath = g_faldConvTuneCachePath;
    g_faldConvTuneCachePath = cache;
    CtWarp w;
    REQUIRE(w.ok);
    unsigned int chosen = 99;
    std::vector<uint8_t> tunedOut;
    {
        g_faldConvTuneOverride = 1;                                 // tune (nothing cached anyway)
        g_faldConvVariantOverride = -1;
        CtLayer lay(w, panel, W, H);
        REQUIRE(lay.ok);
        CHECK(lay.ctx.fald->convTune.GetState() == FaldConvTuner::State::Tuning);
        int runs = 0;
        for (; runs < 80 && lay.ctx.fald->convTune.GetState() == FaldConvTuner::State::Tuning; runs++)
            lay.Run(w, (runs & 1) ? B : A, 1000 + runs, (runs % 8) == 7);   // a read-back now and then lets the queries finish
        MESSAGE("tuned after " << runs << " full runs");
        REQUIRE(lay.ctx.fald->convTune.GetState() == FaldConvTuner::State::Tuned);
        chosen = lay.ctx.fald->convTune.Variant();
        CHECK(chosen < 4u);
        CHECK(lay.ctx.fald->convVariant == chosen);
        const double* med = lay.ctx.fald->convTune.Medians();
        for (int v = 0; v < 4; v++) { CHECK(med[v] > 0.0); MESSAGE("S=" << (1 << v) << ": " << med[v] << " us"); }
        tunedOut = lay.Run(w, B, 5000, true);                       // the output after the switch
        REQUIRE(!tunedOut.empty());
        FaldConvTuneRecord rec;
        REQUIRE(FaldConvTuneCacheLookup(cache, lay.ctx.fald->convTuneKey, rec));   // cached
        CHECK(rec.variant == chosen);
    }
    {   // the next start uses the cache, no tuning
        g_faldConvTuneOverride = -1;
        CtLayer lay(w, panel, W, H);
        REQUIRE(lay.ok);
        CHECK(lay.ctx.fald->convTune.GetState() == FaldConvTuner::State::Cached);
        CHECK(lay.ctx.fald->convVariant == chosen);
        CHECK(lay.Run(w, B, 1, true) == tunedOut);
    }
    {   // the same bytes from every variant, the policy included (the gates' claim, checked here on the tuned frame)
        g_faldConvTuneOverride = 0;
        for (int v = 0; v < 4; v++) {
            g_faldConvVariantOverride = v;
            CtLayer lay(w, panel, W, H);
            REQUIRE(lay.ok);
            CHECK(lay.ctx.fald->convTune.GetState() == FaldConvTuner::State::Policy);
            CHECK(lay.ctx.fald->convVariant == (unsigned int)v);
            CHECK_MESSAGE(lay.Run(w, B, 1, true) == tunedOut, "variant S=" << (1 << v));
        }
    }
    g_faldConvTuneOverride = savedTune; g_faldConvVariantOverride = savedVar; g_faldConvTuneCachePath = savedPath;
    DeleteFileA(cache.c_str());
}
