// The always-on core round 2 — PROTOTYPE (shared/fald_core2.h; DLC results/fald_principled_2026-10-22/PREREG_CORE2.md).
//   - the shaders compile (every pass with the common source's core seam; the core's five kernels) on WARP;
//   - opt-in, FALD_TEST_CORE2_DIR: the overlay's layer with the core ON runs on WARP over the frames <dir>/fNN.rgba16f
//     (panel.bin, frames written by DLC tests/test_fald_core2_warp.py), one fald_dump per run in <dir>/dNN (zone fields,
//     the output), replayed by that test against the GPU-order twin dlc.fald.core2emu; then the setting goes off and the
//     resources must be gone. The refresh period is set long (1 s) so every run advances the zone IIR by exactly k = 1
//     refresh (FaldCore2Refreshes: the run interval over the period, at least 1): a = 1 - 0.65^0.5, deterministic.
// The OFF identity (the core off = main bit for bit) is the flank fade's OFF manifest (tests/test_fald_fade.cpp,
// FALD_TEST_FADE_REF with tests/fixtures/fald_fade_off_main.hashes).
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_core2.h"
#include <d3dcompiler.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <vector>

namespace {
int CoreEnvMode() {                      // FALD_TEST_CORE2_MODE: 1 = KC2c (default), 2 = KC3, 4 = KC5
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, "FALD_TEST_CORE2_MODE") != 0 || !v) return 1;
    const int m = atoi(v); free(v);
    return (m == 2 || m == 4) ? m : 1;
}
std::wstring CoreEnvDir(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::wstring();
    std::string s(v); free(v);
    return std::wstring(s.begin(), s.end());   // ASCII paths only (a test convenience)
}
}  // namespace

TEST_CASE("FALD core round 2: the core's kernels and the layer's passes compile with the common source's core seam") {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    REQUIRE(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic)));
    std::string err;
    CHECK_MESSAGE(FaldCore2InitShaders(dev, g_faldCommonSource, &err), err);
    CHECK(FaldCore2ShadersReady());
    FaldCore2ReleaseShaders();
    CHECK_FALSE(FaldCore2ShadersReady());
    CHECK(FaldCore2IirFactor(0) == 0.0f);
    CHECK(std::fabs(FaldCore2IirFactor(2) - 0.35f) < 1e-6f);                  // 0.35 per 2-refresh content frame
    CHECK(std::fabs(FaldCore2IirFactor(1) - (1.0f - std::sqrt(0.65f))) < 1e-6f);
    CHECK(FaldCore2Refreshes(true, 3, 50.0f, 16.667f) == 3ull);              // mode 3: the panel clock's k
    CHECK(FaldCore2Refreshes(false, 0, 33.4f, 16.667f) == 2ull);             // else the run interval over the period
    CHECK(FaldCore2Refreshes(false, 0, 2.0f, 16.667f) == 1ull);              // at least one
    CHECK(g_faldCore2TestRefreshes == 0ull);                                  // the seam is off unless a test sets it
    ic->Release(); dev->Release();
}

TEST_CASE("FALD core round 2 on WARP: dumps for the DLC twin (FALD_TEST_CORE2_DIR)") {
    const std::wstring dir = CoreEnvDir("FALD_TEST_CORE2_DIR");
    if (dir.empty()) return;
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    REQUIRE(SUCCEEDED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic)));
    g_device = dev; g_context = ic;
    REQUIRE(InitFaldShaders());
    REQUIRE(FaldCore2ShadersReady());

    FaldPanelParams pp; std::string err;
    REQUIRE_MESSAGE(LoadFaldPanelParams(dir + L"/panel.bin", pp, err), err);
    MonitorContext ctx;
    ctx.width = (int)(pp.originX + pp.latW); ctx.height = (int)(pp.originY + pp.latH);
    ctx.isHDREnabled = true;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    ctx.frameTimeExactMs = 1000.0f;                                 // k = 1 refresh per run (see the header)
    FaldSettings& fs = ctx.hdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = dir + L"/panel.bin";
    fs.temporalMode = FALD_TEMPORAL_OFF;
    fs.coreMode = (uint32_t)CoreEnvMode();
    REQUIRE(FaldEnsureResources(&ctx, fs));
    g_faldCore2TestRefreshes = 1;                                   // k = 1 per run, whatever WARP's run time

    static const char* vsSrc = "struct O { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };"
                               "O main(uint id : SV_VertexID) { O o; o.uv = float2((id << 1) & 2, id & 2);"
                               "o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1); return o; }";
    ID3DBlob* vsBlob = nullptr;
    REQUIRE(SUCCEEDED(D3DCompile(vsSrc, strlen(vsSrc), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &vsBlob, nullptr)));
    ID3D11VertexShader* vs = nullptr;
    REQUIRE(SUCCEEDED(dev->CreateVertexShader(vsBlob->GetBufferPointer(), vsBlob->GetBufferSize(), nullptr, &vs)));
    vsBlob->Release();
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = (UINT)ctx.width; td.Height = (UINT)ctx.height; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    REQUIRE(SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &target)));
    REQUIRE(SUCCEEDED(dev->CreateRenderTargetView(target, nullptr, &rtv)));
    D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };

    std::vector<uint16_t> frame((size_t)ctx.width * ctx.height * 4);
    int runs = 0;
    for (int run = 0; run < 100; run++) {
        wchar_t name[32];
        swprintf_s(name, L"/f%02d.rgba16f", run);
        std::ifstream ff(dir + name, std::ios::binary);
        if (!ff) break;
        ff.read((char*)frame.data(), (std::streamsize)(frame.size() * sizeof(uint16_t)));
        REQUIRE((size_t)ff.gcount() == frame.size() * sizeof(uint16_t));
        ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        ic->RSSetViewports(1, &vp);
        ic->VSSetShader(vs, nullptr, 0);
        ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        wchar_t dname[32];
        swprintf_s(dname, L"/d%02d", run);
        CreateDirectoryW((dir + dname).c_str(), nullptr);
        {
            std::lock_guard<std::mutex> lk(g_monitorsMutex);
            ctx.faldDumpDir = dir + dname;
        }
        ctx.faldDumpRequested.store(true);
        FaldRunPasses(&ctx, rtv, true);
        ic->Flush();
        CHECK_FALSE(ctx.faldDumpRequested.load());
        CHECK(ctx.fald->core2On);
        runs++;
        Sleep(12);
    }
    CHECK(runs > 0);
    g_faldCore2TestRefreshes = 0;
    fs.coreMode = 0;                                                // off (C): the core's resources are released
    ctx.faldDumpRequested.store(false);
    FaldRunPasses(&ctx, rtv, true);
    CHECK_FALSE(ctx.fald->core2On);
    CHECK(ctx.fald->core2 == nullptr);
    rtv->Release(); target->Release(); vs->Release();
    FaldReleaseResources(&ctx);
    ReleaseFaldShaders();
    g_context = nullptr; g_device = nullptr;
    ic->ClearState(); ic->Flush();
    ic->Release(); dev->Release();
}
