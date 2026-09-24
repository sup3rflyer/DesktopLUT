// HDR output dither (shared/hdr_dither.h) — the one HLSL copy the overlay (src/shader.h) and the DWM hook
// (dwm_hook/hook_shader.h) splice into their pixel shaders: TPDF, +-1 LSB of 10-bit PQ, per channel, AFTER the
// 3D LUT, amplitude guarded so nothing leaves [0, 1].
//
// Layers:
//   1. CPU port of the exact HLSL: the guard (0 and 1 stay put, the range holds, outside [0, 1] untouched), the
//      TPDF shape, lsb = 0 bit-identical.
//   2. The REAL HLSL text on WARP vs the CPU port (skips with a message only if no D3D11 device exists).
//   3. The real 64x64 blue-noise tile (hook and overlay copies identical): flat histogram, TPDF variance 1/6,
//      decorrelated R/G/B samples, and the quantizer property the design rests on — after rounding to 10 bits the
//      error's mean and spread do not depend on where the value sits between two codes.
//   4. Both production pixel shaders splice the text exactly once, carry no pre-LUT ICtCp dither, and compile.
//
// Keep DitherCpu:: in lockstep with shared/hdr_dither.h — layer 2 fails if the HLSL drifts from this port.

#define _CRT_SECURE_NO_WARNINGS  // dwm_hook/hook_log.h (via hook_shader.h) calls fopen; /sdl makes C4996 an error
#define NOMINMAX
#include <windows.h>             // hook_log.h needs MAX_PATH / ExpandEnvironmentStringsA

#include "doctest.h"
#include "../shared/hdr_dither.h"
#include "shader.h"                     // g_psSource (overlay)
#include "../dwm_hook/hook_shader.h"    // g_shaders (DWM hook), noiseBytes (via noise.h)
#include "types.h"                      // g_blueNoiseData (overlay's copy of the tile)

#include <d3d11.h>
#include <d3dcompiler.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr float kLsb = 1.0f / 1023.0f;

// ---------------------------------------------------------------------------------------------
// CPU port of shared/hdr_dither.h — line for line
// ---------------------------------------------------------------------------------------------
namespace DitherCpu {
float Sign(float x) { return x > 0.0f ? 1.0f : (x < 0.0f ? -1.0f : 0.0f); }
float Tpdf(float u) {
    float x = 2.0f * u - 1.0f;
    return Sign(x) * (1.0f - std::sqrt(1.0f - std::fabs(x)));
}
float DitherPQ(float pq, float u, float lsb) {
    float amp = std::clamp((std::min)(pq, 1.0f - pq), 0.0f, lsb);
    return pq + Tpdf(u) * amp;
}
} // namespace DitherCpu

float TileU(int b) { return (b + 0.5f) / 256.0f; }   // the hook's texel value; the overlay remaps to the same

// ---------------------------------------------------------------------------------------------
// WARP harness: DLUT_HDR_DITHER_HLSL in a compute shader, float4 (pq, u, lsb, -) in, dithered pq out
// ---------------------------------------------------------------------------------------------
const char* const kDitherTestCS =
    "RWStructuredBuffer<float4> io : register(u0);\n"
    DLUT_HDR_DITHER_HLSL
    R"(
[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    float4 v = io[id.x];
    io[id.x] = float4(DlutDitherPQ(float3(v.x, v.x, v.x), float3(v.y, v.y, v.y), v.z).x, 0.0, 0.0, 0.0);
}
)";

struct DitherGpu {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    ID3D11ComputeShader* cs = nullptr;
    std::string error;
    bool deviceUnavailable = false;

    ~DitherGpu() {
        if (cs) cs->Release();
        if (dc) dc->Release();
        if (device) device->Release();
    }

    bool Init() {
        D3D_FEATURE_LEVEL fl;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
                                       D3D11_SDK_VERSION, &device, &fl, &dc);
        if (FAILED(hr))
            hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
                                   D3D11_SDK_VERSION, &device, &fl, &dc);
        if (FAILED(hr)) { deviceUnavailable = true; error = "no D3D11 device (WARP or hardware)"; return false; }
        if (fl < D3D_FEATURE_LEVEL_11_0) { deviceUnavailable = true; error = "feature level < 11_0"; return false; }
        ID3DBlob* blob = nullptr;
        ID3DBlob* err = nullptr;
        hr = D3DCompile(kDitherTestCS, strlen(kDitherTestCS), "HdrDitherCS", nullptr, nullptr, "main", "cs_5_0",
                        0, 0, &blob, &err);   // production compile flags (0)
        if (FAILED(hr)) {
            error = std::string("compile failed: ") + (err ? (const char*)err->GetBufferPointer() : "?");
            if (err) err->Release();
            return false;
        }
        if (err) err->Release();
        hr = device->CreateComputeShader(blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, &cs);
        blob->Release();
        if (FAILED(hr)) { error = "CreateComputeShader failed"; return false; }
        return true;
    }

    std::vector<float> Run(std::vector<float> packed) {
        size_t n = packed.size() / 4;
        size_t padded = (n + 63) / 64 * 64;
        packed.resize(padded * 4, 0.0f);
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = (UINT)(padded * 16);
        bd.Usage = D3D11_USAGE_DEFAULT;
        bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        bd.StructureByteStride = 16;
        D3D11_SUBRESOURCE_DATA init = { packed.data(), 0, 0 };
        ID3D11Buffer* buf = nullptr;
        REQUIRE(SUCCEEDED(device->CreateBuffer(&bd, &init, &buf)));
        D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
        ud.Format = DXGI_FORMAT_UNKNOWN;
        ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
        ud.Buffer.NumElements = (UINT)padded;
        ID3D11UnorderedAccessView* uav = nullptr;
        REQUIRE(SUCCEEDED(device->CreateUnorderedAccessView(buf, &ud, &uav)));
        D3D11_BUFFER_DESC sd = bd;
        sd.Usage = D3D11_USAGE_STAGING;
        sd.BindFlags = 0;
        sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        sd.MiscFlags = 0;
        ID3D11Buffer* staging = nullptr;
        REQUIRE(SUCCEEDED(device->CreateBuffer(&sd, nullptr, &staging)));
        dc->CSSetShader(cs, nullptr, 0);
        dc->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
        dc->Dispatch((UINT)(padded / 64), 1, 1);
        ID3D11UnorderedAccessView* nullUav = nullptr;
        dc->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
        dc->CSSetShader(nullptr, nullptr, 0);
        dc->CopyResource(staging, buf);
        std::vector<float> out(n);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));
        const float* p = (const float*)m.pData;
        for (size_t i = 0; i < n; i++) out[i] = p[i * 4];
        dc->Unmap(staging, 0);
        staging->Release();
        uav->Release();
        buf->Release();
        return out;
    }
};

// The tile's three per-pixel samples, exactly as HdrDitherNoise addresses them (half-tile offsets)
struct TileSamples { std::vector<float> r, g, b; };
TileSamples SampleTile() {
    TileSamples t;
    for (int y = 0; y < NOISE_SIZE; y++)
        for (int x = 0; x < NOISE_SIZE; x++) {
            t.r.push_back(TileU(noiseBytes[y][x]));
            t.g.push_back(TileU(noiseBytes[y][(x + NOISE_SIZE / 2) % NOISE_SIZE]));
            t.b.push_back(TileU(noiseBytes[(y + NOISE_SIZE / 2) % NOISE_SIZE][x]));
        }
    return t;
}

double Corr(const std::vector<float>& a, const std::vector<float>& b) {
    double ma = 0, mb = 0;
    for (size_t i = 0; i < a.size(); i++) { ma += a[i]; mb += b[i]; }
    ma /= a.size(); mb /= b.size();
    double sab = 0, saa = 0, sbb = 0;
    for (size_t i = 0; i < a.size(); i++) {
        sab += (a[i] - ma) * (b[i] - mb); saa += (a[i] - ma) * (a[i] - ma); sbb += (b[i] - mb) * (b[i] - mb);
    }
    return sab / std::sqrt(saa * sbb);
}

} // namespace

// =============================================================================================
// CPU port: the guard and the shape
// =============================================================================================

TEST_CASE("HDR dither: exact black and full scale stay put; nothing leaves [0, 1]; lsb 0 is identity") {
    for (int b = 0; b < 256; b++) {
        float u = TileU(b);
        CHECK(DitherCpu::DitherPQ(0.0f, u, kLsb) == 0.0f);
        CHECK(DitherCpu::DitherPQ(1.0f, u, kLsb) == 1.0f);
        for (int i = 0; i <= 2000; i++) {
            float pq = i / 2000.0f;
            float d = DitherCpu::DitherPQ(pq, u, kLsb);
            CHECK(d >= 0.0f);
            CHECK(d <= 1.0f);
            CHECK(std::fabs(d - pq) <= kLsb * (1.0f + 1e-5f));
            CHECK(DitherCpu::DitherPQ(pq, u, 0.0f) == pq);   // bit-identical off switch
        }
        // outside [0, 1] (can't come from a clamped LUT, but the tonemap-only path passes it) — untouched
        CHECK(DitherCpu::DitherPQ(-0.01f, u, kLsb) == -0.01f);
        CHECK(DitherCpu::DitherPQ(1.01f, u, kLsb) == 1.01f);
    }
}

TEST_CASE("HDR dither: TPDF maps a uniform u onto [-1, 1], symmetric and monotone") {
    CHECK(DitherCpu::Tpdf(0.5f) == 0.0f);
    float prev = -2.0f;
    for (int b = 0; b < 256; b++) {
        float t = DitherCpu::Tpdf(TileU(b));
        CHECK(t > prev);                                      // monotone: the blue-noise spectrum survives
        CHECK(std::fabs(t + DitherCpu::Tpdf(TileU(255 - b))) < 1e-6f);   // symmetric
        CHECK(std::fabs(t) < 1.0f);
        prev = t;
    }
}

// =============================================================================================
// The real HLSL on WARP
// =============================================================================================

TEST_CASE("HDR dither: the shared HLSL on WARP matches the CPU port") {
    DitherGpu gpu;
    if (!gpu.Init()) {
        if (gpu.deviceUnavailable) { MESSAGE("skipping GPU dither test: " << gpu.error); return; }
        FAIL("dither GPU harness init failed: " << gpu.error);
    }
    std::vector<float> packed;
    std::vector<float> pqs = { 0.0f, 1e-6f, kLsb * 0.25f, kLsb, 1.0f - kLsb, 1.0f - 1e-6f, 1.0f, -0.01f, 1.01f };
    for (int i = 0; i <= 256; i++) pqs.push_back(i / 256.0f);
    for (float lsb : { 0.0f, kLsb })
        for (float pq : pqs)
            for (int b = 0; b < 256; b++) packed.insert(packed.end(), { pq, TileU(b), lsb, 0.0f });
    auto out = gpu.Run(packed);
    REQUIRE(out.size() == packed.size() / 4);
    double worst = 0.0;
    for (size_t i = 0; i < out.size(); i++) {
        float pq = packed[i * 4], u = packed[i * 4 + 1], lsb = packed[i * 4 + 2];
        if (lsb == 0.0f) CHECK(out[i] == pq);                // off = bit-identical on the GPU too
        worst = (std::max)(worst, (double)std::fabs(out[i] - DitherCpu::DitherPQ(pq, u, lsb)));
    }
    CHECK(worst < 3e-7);
}

// =============================================================================================
// The real tile and the quantizer property
// =============================================================================================

TEST_CASE("HDR dither: hook and overlay carry the same flat blue-noise tile; R/G/B samples decorrelated") {
    CHECK(std::memcmp(noiseBytes, g_blueNoiseData, NOISE_SIZE * NOISE_SIZE) == 0);
    int hist[256] = {};
    for (int y = 0; y < NOISE_SIZE; y++)
        for (int x = 0; x < NOISE_SIZE; x++) hist[noiseBytes[y][x]]++;
    for (int v = 0; v < 256; v++) CHECK(hist[v] == NOISE_SIZE * NOISE_SIZE / 256);

    TileSamples t = SampleTile();
    double var = 0.0;
    for (float u : t.r) { double d = DitherCpu::Tpdf(u); var += d * d; }
    var /= t.r.size();
    CHECK(var > 0.160);                                       // TPDF on [-1, 1]: variance 1/6
    CHECK(var < 0.172);
    CHECK(std::fabs(Corr(t.r, t.g)) < 0.05);
    CHECK(std::fabs(Corr(t.r, t.b)) < 0.05);
    CHECK(std::fabs(Corr(t.g, t.b)) < 0.05);
}

TEST_CASE("HDR dither: after 10-bit rounding the error is signal-independent (no modulating grain)") {
    // For 41 positions between two codes, dither a flat field over the whole tile, round to 10 bits and look at
    // the error: TPDF +-1 LSB makes its mean ~0 and its spread ~0.5 LSB wherever the value sits (RPDF +-0.5 LSB
    // would swing the spread from 0 to 0.5 — grain appearing and vanishing along a gradient).
    TileSamples t = SampleTile();
    for (int base : { 64, 512, 900 }) {
        for (int j = 0; j <= 40; j++) {
            double v = (base + j / 40.0) / 1023.0;
            double mean = 0.0, m2 = 0.0;
            for (float u : t.r) {
                double d = DitherCpu::DitherPQ((float)v, u, kLsb);
                double e = std::round(d * 1023.0) - v * 1023.0;   // error in LSB
                mean += e; m2 += e * e;
            }
            mean /= t.r.size();
            double sd = std::sqrt((std::max)(m2 / t.r.size() - mean * mean, 0.0));
            INFO("code " << base << " + " << j << "/40: mean " << mean << " sd " << sd);
            CHECK(std::fabs(mean) < 0.02);
            CHECK(sd > 0.45);
            CHECK(sd < 0.55);
        }
    }
}

// =============================================================================================
// Host gating: the dither rides only on a pass that processes the HDR image
// =============================================================================================

TEST_CASE("HDR dither: host amplitude only for HDR + actual processing + switch on") {
    CHECK(DlutHdrDitherLsb(true, true, true) == kLsb);
    CHECK(DlutHdrDitherLsb(false, true, true) == 0.0f);   // SDR / ACM: not this dither
    CHECK(DlutHdrDitherLsb(true, false, true) == 0.0f);   // pure passthrough stays bit-exact
    CHECK(DlutHdrDitherLsb(true, true, false) == 0.0f);   // kill switch
}

// =============================================================================================
// The REAL hook pixel shader on WARP: FP32 target, identity / broken LUTs, vs the pre-change shader text
// =============================================================================================
namespace {

struct HookCB {
    int lutSize, colorMode, ditherLevels, tonemapEnabled;
    int tonemapCurve; float tonemapTargetNits, pqSourcePeak, pqTargetPeak;
    int tonemapDynamic, hasLut; float hdrDitherLsb, pad2;
};
static_assert(sizeof(HookCB) == 48, "hook cbuffer layout");

// The hook shader text as it was before the post-LUT dither: the two HDR returns without it.
std::string PreChangeHookShader() {
    std::string s(g_shaders, sizeof g_shaders);
    auto swap = [&](const std::string& from, const std::string& to) {
        size_t at = s.find(from);
        REQUIRE(at != std::string::npos);
        s.replace(at, from.size(), to);
    };
    swap("pq_eotf(saturate(DlutDitherPQ(lut_out, u, hdrDitherLsb)))", "pq_eotf(lut_out)");
    swap("float3 delta = pq_eotf(DlutDitherPQ(pq, u, hdrDitherLsb)) - pq_eotf(pq);", "float3 delta = 0;");
    return s;
}

struct HookPsGpu {
    static constexpr int W = 256, H = 64;
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    std::vector<IUnknown*> keep;
    ID3D11VertexShader* vs = nullptr;
    ID3D11InputLayout* layout = nullptr;
    ID3D11Buffer* vb = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11ShaderResourceView* inputSRV = nullptr;
    ID3D11ShaderResourceView* noiseSRV = nullptr;
    ID3D11SamplerState* pointClamp = nullptr;
    ID3D11SamplerState* pointWrap = nullptr;
    ID3D11Texture2D* rt = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11Texture2D* staging = nullptr;
    std::string error;
    bool unavailable = false;

    ~HookPsGpu() {
        if (dc) dc->ClearState();
        for (IUnknown* u : keep) if (u) u->Release();
        if (dc) dc->Release();
        if (dev) dev->Release();
    }
    template <typename T> T* Keep(T* p) { keep.push_back(p); return p; }

    ID3DBlob* Compile(const std::string& text, const char* entry, const char* target) {
        ID3DBlob* blob = nullptr;
        ID3DBlob* msgs = nullptr;
        HRESULT hr = D3DCompile(text.data(), text.size(), nullptr, nullptr, nullptr, entry, target, 0, 0, &blob, &msgs);
        if (FAILED(hr)) error = msgs ? (const char*)msgs->GetBufferPointer() : "compile failed";
        if (msgs) msgs->Release();
        return SUCCEEDED(hr) ? blob : nullptr;
    }

    ID3D11PixelShader* Pixel(const std::string& text) {
        ID3DBlob* b = Compile(text, "PS", "ps_5_0");
        if (!b) return nullptr;
        ID3D11PixelShader* ps = nullptr;
        dev->CreatePixelShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &ps);
        b->Release();
        return Keep(ps);
    }

    ID3D11ShaderResourceView* Lut3D(int n, const std::vector<float>& rgba) {
        D3D11_TEXTURE3D_DESC d = {};
        d.Width = d.Height = d.Depth = n;
        d.MipLevels = 1;
        d.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        d.Usage = D3D11_USAGE_IMMUTABLE;
        d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = { rgba.data(), (UINT)(n * 16), (UINT)(n * n * 16) };
        ID3D11Texture3D* t = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateTexture3D(&d, &init, &t)));
        Keep(t);
        ID3D11ShaderResourceView* v = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateShaderResourceView(t, nullptr, &v)));
        return Keep(v);
    }

    bool Init(const std::vector<float>& inputRGBA) {
        D3D_FEATURE_LEVEL fl;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
                                       D3D11_SDK_VERSION, &dev, &fl, &dc);
        if (FAILED(hr)) { unavailable = true; error = "no WARP device"; return false; }
        ID3DBlob* vsb = Compile(std::string(g_shaders, sizeof g_shaders), "VS", "vs_5_0");
        if (!vsb) return false;
        dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs);
        Keep(vs);
        D3D11_INPUT_ELEMENT_DESC ied[] = {   // exactly the hook layout (hook_render.cpp)
            {"POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
            {"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, D3D11_APPEND_ALIGNED_ELEMENT, D3D11_INPUT_PER_VERTEX_DATA, 0}};
        dev->CreateInputLayout(ied, 2, vsb->GetBufferPointer(), vsb->GetBufferSize(), &layout);
        Keep(layout);
        vsb->Release();
        const float quad[] = { -1, -1, 0, 1,  -1, 1, 0, 0,  1, 1, 1, 0,   -1, -1, 0, 1,  1, 1, 1, 0,  1, -1, 1, 1 };
        D3D11_BUFFER_DESC bd = { sizeof quad, D3D11_USAGE_IMMUTABLE, D3D11_BIND_VERTEX_BUFFER, 0, 0, 0 };
        D3D11_SUBRESOURCE_DATA vinit = { quad, 0, 0 };
        REQUIRE(SUCCEEDED(dev->CreateBuffer(&bd, &vinit, &vb)));
        Keep(vb);
        D3D11_BUFFER_DESC cbd = { sizeof(HookCB), D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0 };
        REQUIRE(SUCCEEDED(dev->CreateBuffer(&cbd, nullptr, &cb)));
        Keep(cb);

        D3D11_TEXTURE2D_DESC td = {};
        td.Width = W; td.Height = H; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R32G32B32A32_FLOAT; td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA iinit = { inputRGBA.data(), W * 16, 0 };
        ID3D11Texture2D* in = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&td, &iinit, &in)));
        Keep(in);
        REQUIRE(SUCCEEDED(dev->CreateShaderResourceView(in, nullptr, &inputSRV)));
        Keep(inputSRV);

        std::vector<float> noise(NOISE_SIZE * NOISE_SIZE);   // exactly as hook_render.cpp builds it
        for (int i = 0; i < NOISE_SIZE; i++)
            for (int j = 0; j < NOISE_SIZE; j++) noise[i * NOISE_SIZE + j] = (noiseBytes[i][j] + 0.5f) / 256;
        D3D11_TEXTURE2D_DESC nd = td;
        nd.Width = nd.Height = NOISE_SIZE; nd.Format = DXGI_FORMAT_R32_FLOAT;
        D3D11_SUBRESOURCE_DATA ninit = { noise.data(), NOISE_SIZE * 4, 0 };
        ID3D11Texture2D* nt = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&nd, &ninit, &nt)));
        Keep(nt);
        REQUIRE(SUCCEEDED(dev->CreateShaderResourceView(nt, nullptr, &noiseSRV)));
        Keep(noiseSRV);

        D3D11_SAMPLER_DESC sd = {};
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.ComparisonFunc = D3D11_COMPARISON_NEVER;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &pointClamp)));
        Keep(pointClamp);
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &pointWrap)));
        Keep(pointWrap);

        D3D11_TEXTURE2D_DESC rd = td;
        rd.Usage = D3D11_USAGE_DEFAULT; rd.BindFlags = D3D11_BIND_RENDER_TARGET;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&rd, nullptr, &rt)));
        Keep(rt);
        REQUIRE(SUCCEEDED(dev->CreateRenderTargetView(rt, nullptr, &rtv)));
        Keep(rtv);
        D3D11_TEXTURE2D_DESC st = rd;
        st.Usage = D3D11_USAGE_STAGING; st.BindFlags = 0; st.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&st, nullptr, &staging)));
        Keep(staging);
        return true;
    }

    std::vector<float> Render(ID3D11PixelShader* ps, ID3D11ShaderResourceView* lut, const HookCB& c) {
        dc->UpdateSubresource(cb, 0, nullptr, &c, 0, 0);
        UINT stride = 16, offset = 0;
        dc->IASetInputLayout(layout);
        dc->IASetVertexBuffers(0, 1, &vb, &stride, &offset);
        dc->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        dc->VSSetShader(vs, nullptr, 0);
        dc->PSSetShader(ps, nullptr, 0);
        dc->PSSetConstantBuffers(0, 1, &cb);
        ID3D11ShaderResourceView* srvs[3] = { inputSRV, lut, noiseSRV };
        dc->PSSetShaderResources(0, 3, srvs);
        ID3D11SamplerState* smps[2] = { pointClamp, pointWrap };
        dc->PSSetSamplers(0, 2, smps);
        D3D11_VIEWPORT vp = { 0, 0, (float)W, (float)H, 0, 1 };
        dc->RSSetViewports(1, &vp);
        dc->OMSetRenderTargets(1, &rtv, nullptr);
        dc->Draw(6, 0);
        dc->CopyResource(staging, rt);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));
        std::vector<float> out((size_t)W * H * 4);
        for (int y = 0; y < H; y++)
            std::memcpy(&out[(size_t)y * W * 4], (const char*)m.pData + (size_t)y * m.RowPitch, W * 16);
        dc->Unmap(staging, 0);
        return out;
    }
};

// scRGB -> BT.2100 linear (10000 nits = 1) with the hook matrix, then PQ: reads results back in the PQ domain
double ToPQ2100(const float* scrgb, int ch) {
    static const double M[3][3] = {
        {2939026994.0 / 585553224375.0, 9255011753.0 / 3513319346250.0, 173911579.0 / 501902763750.0},
        {76515593.0 / 138420033750.0, 6109575001.0 / 830520202500.0, 75493061.0 / 830520202500.0},
        {12225392.0 / 93230009375.0, 1772384008.0 / 2517210253125.0, 18035212433.0 / 2517210253125.0}};
    double lin = M[ch][0] * scrgb[0] + M[ch][1] * scrgb[1] + M[ch][2] * scrgb[2];
    constexpr double m1 = 2610.0 / 16384.0, m2 = 2523.0 / 4096.0 * 128.0;
    constexpr double c1 = 3424.0 / 4096.0, c2 = 2413.0 / 4096.0 * 32.0, c3 = 2392.0 / 4096.0 * 32.0;
    double y = std::pow((std::max)(lin, 0.0), m1);
    return std::pow((c1 + c2 * y) / (1.0 + c3 * y), m2);
}

}  // namespace

TEST_CASE("HDR dither: the real hook PS on WARP - off is the pre-change shader bit for bit; on is zero-mean") {
    // Four 64x64 blocks (one noise tile each): black, 100-nit grey, a bright blue, a dim red (scRGB, 80 nits = 1).
    const float blocks[4][3] = { {0, 0, 0}, {1.25f, 1.25f, 1.25f}, {0.10f, 0.10f, 2.0f}, {0.05f, 0.005f, 0.005f} };
    std::vector<float> input((size_t)HookPsGpu::W * HookPsGpu::H * 4);
    for (int y = 0; y < HookPsGpu::H; y++)
        for (int x = 0; x < HookPsGpu::W; x++)
            for (int c = 0; c < 4; c++)
                input[((size_t)y * HookPsGpu::W + x) * 4 + c] = c < 3 ? blocks[x / 64][c] : 1.0f;
    HookPsGpu gpu;
    if (!gpu.Init(input)) {
        if (gpu.unavailable) { MESSAGE("skipping hook PS render test: " << gpu.error); return; }
        FAIL("hook PS harness init failed: " << gpu.error);
    }
    ID3D11PixelShader* psNew = gpu.Pixel(std::string(g_shaders, sizeof g_shaders));
    ID3D11PixelShader* psOld = gpu.Pixel(PreChangeHookShader());
    INFO("compiler: " << gpu.error);
    REQUIRE(psNew);
    REQUIRE(psOld);

    const int n = 17;
    std::vector<float> ident((size_t)n * n * n * 4), broken(ident.size(), -0.25f);
    for (int b = 0; b < n; b++)
        for (int g = 0; g < n; g++)
            for (int r = 0; r < n; r++) {
                float* v = &ident[(((size_t)b * n + g) * n + r) * 4];
                v[0] = r / float(n - 1); v[1] = g / float(n - 1); v[2] = b / float(n - 1); v[3] = 1.0f;
            }
    ID3D11ShaderResourceView* lutId = gpu.Lut3D(n, ident);
    ID3D11ShaderResourceView* lutBad = gpu.Lut3D(n, broken);

    HookCB c = {};
    c.lutSize = n; c.colorMode = 1; c.ditherLevels = 1023; c.tonemapTargetNits = 1000.0f;
    // LUT path and no-LUT path, dither off: bit-identical to the shader before this change
    for (int hasLut : { 1, 0 }) {
        c.hasLut = hasLut; c.hdrDitherLsb = 0.0f;
        auto a = gpu.Render(psNew, lutId, c);
        auto b = gpu.Render(psOld, lutId, c);
        INFO("hasLut " << hasLut);
        CHECK(std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0);
    }

    // Dither on (LUT path): every pixel within 1 LSB of the undithered PQ value, each block's mean dither ~0 (a
    // 64x64 block covers the tile once per channel), and black stays black: the analytic PQ encode of 0 is c1^m2 =
    // 7.3e-7 (not 0), so the guard leaves a sliver of dither there — its light is < 1e-20, which DWM's FP16 buffer
    // stores as exactly 0 (smallest FP16 subnormal 6e-8) and the link sends as code 0.
    c.hasLut = 1;
    c.hdrDitherLsb = 0.0f;
    auto ref = gpu.Render(psNew, lutId, c);
    c.hdrDitherLsb = kLsb;
    auto dit = gpu.Render(psNew, lutId, c);
    for (int blk = 0; blk < 4; blk++) {
        for (int ch = 0; ch < 3; ch++) {
            double sum = 0.0, worst = 0.0;
            bool blackExact = true;
            for (int y = 0; y < HookPsGpu::H; y++)
                for (int x = blk * 64; x < blk * 64 + 64; x++) {
                    size_t i = ((size_t)y * HookPsGpu::W + x) * 4;
                    double e = (ToPQ2100(&dit[i], ch) - ToPQ2100(&ref[i], ch)) * 1023.0;   // in LSB
                    sum += e;
                    worst = (std::max)(worst, std::fabs(e));
                    if (blk == 0 && !(std::fabs(dit[i + ch]) < 3e-8f)) blackExact = false;   // rounds to FP16 0
                }
            INFO("block " << blk << " channel " << ch << " mean " << sum / 4096.0 << " worst " << worst);
            CHECK(std::fabs(sum / 4096.0) < 0.02);
            CHECK(worst <= 1.0 + 1e-3);
            CHECK(blackExact);
        }
    }

    // A user cube with out-of-range entries: the saturate before the EOTF keeps NaN out (dither on and off)
    for (float lsb : { 0.0f, kLsb }) {
        c.hdrDitherLsb = lsb;
        auto bad = gpu.Render(psNew, lutBad, c);
        bool finite = std::all_of(bad.begin(), bad.end(), [](float v) { return std::isfinite(v); });
        CHECK(finite);
    }
}

// =============================================================================================
// Both production pixel shaders
// =============================================================================================

TEST_CASE("HDR dither: both production pixel shaders splice the shared text once and dither after the LUT") {
    const std::string shared = DLUT_HDR_DITHER_HLSL;
    const std::string overlay = g_psSource;
    const std::string hook(g_shaders);
    for (const std::string* src : { &overlay, &hook }) {
        size_t at = src->find(shared);
        CHECK(at != std::string::npos);
        CHECK(src->find(shared, at + 1) == std::string::npos);
        CHECK(src->find("ApplyDitherICtCp") == std::string::npos);   // the old pre-LUT dither is gone
        CHECK(src->find("hdrDitherLsb") != std::string::npos);
        // the dither is applied to the LUT output, not before the LUT
        size_t lut = src->find(src == &hook ? "LutTransformTetrahedral(pq)" : "SampleLUT(pqRGB)");
        size_t dither = src->find("DlutDitherPQ(lut", lut);
        CHECK(lut != std::string::npos);
        CHECK(dither != std::string::npos);
    }
    auto compile = [](const char* text, size_t len, const char* entry, std::string& output) {
        ID3DBlob* blob = nullptr;
        ID3DBlob* msgs = nullptr;
        HRESULT hr = D3DCompile(text, len, nullptr, nullptr, nullptr, entry, "ps_5_0", 0, 0, &blob, &msgs);
        if (msgs) { output = (const char*)msgs->GetBufferPointer(); msgs->Release(); }
        if (blob) blob->Release();
        return SUCCEEDED(hr);
    };
    std::string overlayMsgs, hookMsgs;
    bool overlayOk = compile(g_psSource, strlen(g_psSource), "main", overlayMsgs);
    bool hookOk = compile(g_shaders, sizeof g_shaders, "PS", hookMsgs);
    INFO("overlay compiler output: " << overlayMsgs);
    INFO("hook compiler output: " << hookMsgs);
    CHECK(overlayOk);
    CHECK(hookOk);
}
