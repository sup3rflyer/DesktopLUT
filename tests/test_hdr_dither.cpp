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
