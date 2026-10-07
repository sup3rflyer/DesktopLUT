// Analysis overlay compute shader (src/shader.h g_analysisCSSource) on WARP: the output slot layout the
// C++ readback relies on (analysis_slot), and T4.21 — MaxCLL / MaxFALL are defined on max(R,G,B)
// (CTA-861.3), not on luminance Y: a saturated 1000-nit red has Y 213.

#include "doctest.h"
#include "shader.h"
#include <DirectXPackedVector.h>

#include <d3d11.h>
#include <d3dcompiler.h>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace {

struct Px { unsigned x, y; float r, g, b; };

uint16_t HalfFromFloat(float f) { return DirectX::PackedVector::XMConvertFloatToHalf(f); }

struct AnalysisRun {
    std::vector<uint32_t> out;
    std::string error;
    bool deviceUnavailable = false;
};

template <class T> void Rel(T*& p) { if (p) { p->Release(); p = nullptr; } }

// One dispatch of the production shader over an 80x45 FP16 frame (the shader's sample grid is 80x45,
// so every sample hits exactly one texel) that is black except for `pixels`.
AnalysisRun RunAnalysis(const std::vector<Px>& pixels, bool isHDR) {
    AnalysisRun run;
    const unsigned W = 80, H = 45;
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    ID3D11ComputeShader* cs = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11Buffer* buf = nullptr;
    ID3D11Buffer* staging = nullptr;
    ID3D11UnorderedAccessView* uav = nullptr;
    ID3D11Texture2D* tex = nullptr;
    ID3D11ShaderResourceView* srv = nullptr;
    auto cleanup = [&] { Rel(srv); Rel(tex); Rel(uav); Rel(staging); Rel(buf); Rel(cb); Rel(cs); Rel(dc); Rel(dev); };

    D3D_FEATURE_LEVEL fl;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &dev, &fl, &dc);
    if (FAILED(hr))
        hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION, &dev, &fl, &dc);
    if (FAILED(hr) || fl < D3D_FEATURE_LEVEL_11_0) { run.deviceUnavailable = true; run.error = "no D3D11 device"; cleanup(); return run; }

    ID3DBlob* blob = nullptr;
    ID3DBlob* err = nullptr;
    hr = D3DCompile(g_analysisCSSource, strlen(g_analysisCSSource), "AnalysisCS", nullptr, nullptr, "main", "cs_5_0", 0, 0, &blob, &err);
    if (FAILED(hr)) {
        run.error = std::string("compile failed: ") + (err ? (const char*)err->GetBufferPointer() : "?");
        Rel(err); cleanup(); return run;
    }
    Rel(err);
    hr = dev->CreateComputeShader(blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, &cs);
    Rel(blob);
    if (FAILED(hr)) { run.error = "CreateComputeShader failed"; cleanup(); return run; }

    uint32_t params[4] = { W, H, isHDR ? 1u : 0u, 0u };
    D3D11_BUFFER_DESC cbd = {};
    cbd.ByteWidth = sizeof(params);
    cbd.Usage = D3D11_USAGE_DEFAULT;
    cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    D3D11_SUBRESOURCE_DATA cbInit = { params, 0, 0 };
    if (FAILED(dev->CreateBuffer(&cbd, &cbInit, &cb))) { run.error = "CB failed"; cleanup(); return run; }

    // The same buffers CreateAnalysisResources makes
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = kAnalysisOutputUints * sizeof(uint32_t);
    bd.Usage = D3D11_USAGE_DEFAULT;
    bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    bd.StructureByteStride = sizeof(uint32_t);
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_UNKNOWN;
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    ud.Buffer.NumElements = kAnalysisOutputUints;
    D3D11_BUFFER_DESC sd = {};
    sd.ByteWidth = bd.ByteWidth;
    sd.Usage = D3D11_USAGE_STAGING;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (FAILED(dev->CreateBuffer(&bd, nullptr, &buf)) || FAILED(dev->CreateUnorderedAccessView(buf, &ud, &uav)) ||
        FAILED(dev->CreateBuffer(&sd, nullptr, &staging))) {
        run.error = "output buffers failed"; cleanup(); return run;
    }

    std::vector<uint16_t> texels((size_t)W * H * 4, 0);
    for (size_t i = 0; i < (size_t)W * H; i++) texels[i * 4 + 3] = HalfFromFloat(1.0f);
    for (const Px& p : pixels) {
        const size_t i = ((size_t)p.y * W + p.x) * 4;
        texels[i + 0] = HalfFromFloat(p.r);
        texels[i + 1] = HalfFromFloat(p.g);
        texels[i + 2] = HalfFromFloat(p.b);
    }
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = W; td.Height = H; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1;
    td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA texInit = { texels.data(), W * 4 * (UINT)sizeof(uint16_t), 0 };
    if (FAILED(dev->CreateTexture2D(&td, &texInit, &tex)) || FAILED(dev->CreateShaderResourceView(tex, nullptr, &srv))) {
        run.error = "texture failed"; cleanup(); return run;
    }

    // As DispatchAnalysisCompute: clear, dispatch one group, copy out
    UINT zero[4] = { 0, 0, 0, 0 };
    dc->ClearUnorderedAccessViewUint(uav, zero);
    dc->CSSetShader(cs, nullptr, 0);
    dc->CSSetConstantBuffers(0, 1, &cb);
    dc->CSSetShaderResources(0, 1, &srv);
    dc->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
    dc->Dispatch(1, 1, 1);
    ID3D11UnorderedAccessView* nullUav = nullptr;
    dc->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
    dc->CopyResource(staging, buf);
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m))) { run.error = "Map failed"; cleanup(); return run; }
    run.out.assign((const uint32_t*)m.pData, (const uint32_t*)m.pData + kAnalysisOutputUints);
    dc->Unmap(staging, 0);
    cleanup();
    return run;
}

float F(const std::vector<uint32_t>& out, unsigned slot) {
    float f;
    std::memcpy(&f, &out[slot], sizeof(f));
    return f;
}

}  // namespace

TEST_CASE("Analysis CS: MaxCLL basis is max(R,G,B), Peak Y stays luminance (CTA-861.3)") {
    // A 1000-nit pure red (scRGB 12.5) and a 200-nit pure blue (2.5); everything else black.
    const AnalysisRun run = RunAnalysis({ { 10, 10, 12.5f, 0.0f, 0.0f }, { 50, 30, 0.0f, 0.0f, 2.5f } }, true);
    if (run.deviceUnavailable) { MESSAGE("skipped: " << run.error); return; }
    REQUIRE_MESSAGE(run.error.empty(), run.error);
    REQUIRE(run.out.size() == kAnalysisOutputUints);

    CHECK(run.out[analysis_slot::TotalPixels] == 80u * 45u);
    // Peak Y: the red's luminance, 12.5 * 0.2126 * 80 — what the tonemapper compares
    CHECK(F(run.out, analysis_slot::PeakY) == doctest::Approx(212.6).epsilon(0.002));
    // Peak RGB: the red channel itself, 1000 nits (FP16 holds 12.5 exactly)
    CHECK(F(run.out, analysis_slot::PeakRgb) == doctest::Approx(1000.0).epsilon(0.001));
    // Sum of max(R,G,B): 1000 + 200 (FALL = that / total samples on the CPU)
    CHECK(F(run.out, analysis_slot::SumRgb) == doctest::Approx(1200.0).epsilon(0.001));
    // Luminance sum for comparison: 212.6 + 2.5 * 0.0722 * 80
    CHECK(F(run.out, analysis_slot::SumY) == doctest::Approx(212.6 + 14.44).epsilon(0.002));
    // The unused tail of the buffer stays cleared
    for (unsigned i = analysis_slot::SumRgb + 1; i < kAnalysisOutputUints; i++) CHECK(run.out[i] == 0u);
}

TEST_CASE("Analysis CS: negative (out-of-709) components do not lower or raise the max(R,G,B) basis") {
    // A wide-gamut green: negative red and blue in scRGB. max(R,G,B) = the green channel.
    const AnalysisRun run = RunAnalysis({ { 0, 0, -0.25f, 5.0f, -0.1f } }, true);
    if (run.deviceUnavailable) { MESSAGE("skipped: " << run.error); return; }
    REQUIRE_MESSAGE(run.error.empty(), run.error);
    CHECK(F(run.out, analysis_slot::PeakRgb) == doctest::Approx(400.0).epsilon(0.001));
    CHECK(F(run.out, analysis_slot::SumRgb) == doctest::Approx(400.0).epsilon(0.001));
    CHECK(run.out[analysis_slot::Rec709] + run.out[analysis_slot::P3Only] + run.out[analysis_slot::Rec2020Only] +
              run.out[analysis_slot::OutOfGamut] == 80u * 45u);
}
