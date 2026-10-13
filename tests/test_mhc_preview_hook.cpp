// The live edit preview through the DWM hook (mhc_preview.h, DwmHookMhcPreview): the REAL hook pixel shader on WARP
// with the preview on applies the profile the bake installs — exactly what EvalMhcScanout gives for what the hook
// outputs with the preview off — in all three colour modes, with and without a 3D LUT in front of it.

#define _CRT_SECURE_NO_WARNINGS  // dwm_hook/hook_log.h (via hook_shader.h) calls fopen; /sdl makes C4996 an error
#define NOMINMAX
#include <windows.h>             // hook_log.h needs MAX_PATH / ExpandEnvironmentStringsA

#include "doctest.h"
#include "mhc.h"
#include "../dwm_hook/hook_shader.h"    // g_shaders (DWM hook), noiseBytes (via noise.h)
#include "../shared/dwm_hook_config.h"  // DwmHookMhcPreview

#include <d3d11.h>
#include <d3dcompiler.h>
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <random>
#include <string>
#include <vector>

namespace {

struct HookCb {
    int lutSize, colorMode, ditherLevels, tonemapEnabled;
    int tonemapCurve; float tonemapTargetNits, pqSourcePeak, pqTargetPeak;
    int tonemapDynamic, hasLut; float hdrDitherLsb, pad2;
};
static_assert(sizeof(HookCb) == 48, "hook cbuffer layout (hook_render.cpp)");

struct HookGpu {
    static constexpr int W = 1024;
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    std::vector<IUnknown*> keep;
    ID3D11VertexShader* vs = nullptr;
    ID3D11PixelShader* ps = nullptr;
    ID3D11InputLayout* layout = nullptr;
    ID3D11Buffer* vb = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11Buffer* mhcCb = nullptr;
    ID3D11ShaderResourceView* srv[7] = {};
    ID3D11SamplerState* smp[3] = {};
    ID3D11Texture2D* rt = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11Texture2D* staging = nullptr;
    std::string error;
    bool unavailable = false;

    ~HookGpu() {
        if (dc) dc->ClearState();
        for (IUnknown* u : keep) if (u) u->Release();
        if (dc) dc->Release();
        if (dev) dev->Release();
    }
    template <typename T> T* Keep(T* p) { keep.push_back(p); return p; }

    ID3D11ShaderResourceView* Tex2D(int w, int h, DXGI_FORMAT fmt, const void* data, UINT pitch) {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = fmt; d.SampleDesc.Count = 1;
        d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = { data, pitch, 0 };
        ID3D11Texture2D* t = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&d, &init, &t)));
        Keep(t);
        ID3D11ShaderResourceView* v = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateShaderResourceView(t, nullptr, &v)));
        return Keep(v);
    }

    ID3DBlob* Compile(const char* entry, const char* target) {
        ID3DBlob* blob = nullptr;
        ID3DBlob* msgs = nullptr;
        HRESULT hr = D3DCompile(g_shaders, sizeof g_shaders, nullptr, nullptr, nullptr, entry, target, 0, 0, &blob, &msgs);
        if (FAILED(hr)) error = msgs ? (const char*)msgs->GetBufferPointer() : "compile failed";
        if (msgs) msgs->Release();
        return SUCCEEDED(hr) ? blob : nullptr;   // as hook_render.cpp compiles it
    }

    bool Init() {
        D3D_FEATURE_LEVEL fl;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION,
                                     &dev, &fl, &dc))) {
            unavailable = true;
            error = "no WARP device";
            return false;
        }
        ID3DBlob* vsb = Compile("VS", "vs_5_0");
        if (!vsb) return false;
        dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs);
        Keep(vs);
        D3D11_INPUT_ELEMENT_DESC ied[] = {   // exactly the hook layout (hook_render.cpp)
            {"POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
            {"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, D3D11_APPEND_ALIGNED_ELEMENT, D3D11_INPUT_PER_VERTEX_DATA, 0}};
        dev->CreateInputLayout(ied, 2, vsb->GetBufferPointer(), vsb->GetBufferSize(), &layout);
        Keep(layout);
        vsb->Release();
        ID3DBlob* psb = Compile("PS", "ps_5_0");
        if (!psb) return false;
        dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &ps);
        Keep(ps);
        psb->Release();
        const float quad[] = { -1, -1, 0, 1,  -1, 1, 0, 0,  1, 1, 1, 0,   -1, -1, 0, 1,  1, 1, 1, 0,  1, -1, 1, 1 };
        D3D11_BUFFER_DESC bd = { sizeof quad, D3D11_USAGE_IMMUTABLE, D3D11_BIND_VERTEX_BUFFER, 0, 0, 0 };
        D3D11_SUBRESOURCE_DATA vinit = { quad, 0, 0 };
        REQUIRE(SUCCEEDED(dev->CreateBuffer(&bd, &vinit, &vb)));
        Keep(vb);
        D3D11_BUFFER_DESC cbd = { sizeof(HookCb), D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0 };
        REQUIRE(SUCCEEDED(dev->CreateBuffer(&cbd, nullptr, &cb)));
        Keep(cb);
        cbd.ByteWidth = 48;   // b1, as hook_render.cpp MhcPreviewSync creates it
        REQUIRE(SUCCEEDED(dev->CreateBuffer(&cbd, nullptr, &mhcCb)));
        Keep(mhcCb);

        std::vector<float> noise(NOISE_SIZE * NOISE_SIZE);   // exactly as hook_render.cpp builds it
        for (int i = 0; i < NOISE_SIZE; i++)
            for (int j = 0; j < NOISE_SIZE; j++) noise[i * NOISE_SIZE + j] = (noiseBytes[i][j] + 0.5f) / 256;
        srv[2] = Tex2D(NOISE_SIZE, NOISE_SIZE, DXGI_FORMAT_R32_FLOAT, noise.data(), NOISE_SIZE * 4);

        D3D11_SAMPLER_DESC sd = {};
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.ComparisonFunc = D3D11_COMPARISON_NEVER;
        sd.MaxLOD = D3D11_FLOAT32_MAX;
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &smp[0])));
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &smp[1])));
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &smp[2])));
        for (auto* s : smp) Keep(s);

        D3D11_TEXTURE2D_DESC rd = {};
        rd.Width = W; rd.Height = 1; rd.MipLevels = 1; rd.ArraySize = 1;
        rd.Format = DXGI_FORMAT_R32G32B32A32_FLOAT; rd.SampleDesc.Count = 1;
        rd.Usage = D3D11_USAGE_DEFAULT; rd.BindFlags = D3D11_BIND_RENDER_TARGET;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&rd, nullptr, &rt)));
        Keep(rt);
        REQUIRE(SUCCEEDED(dev->CreateRenderTargetView(rt, nullptr, &rtv)));
        Keep(rtv);
        rd.Usage = D3D11_USAGE_STAGING; rd.BindFlags = 0; rd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&rd, nullptr, &staging)));
        Keep(staging);
        return true;
    }

    void SetLut3D(int n, const std::vector<float>& rgba) {
        D3D11_TEXTURE3D_DESC d = {};
        d.Width = d.Height = d.Depth = n; d.MipLevels = 1; d.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = { rgba.data(), (UINT)(n * 16), (UINT)(n * n * 16) };
        ID3D11Texture3D* t = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateTexture3D(&d, &init, &t)));
        Keep(t);
        ID3D11ShaderResourceView* v = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateShaderResourceView(t, nullptr, &v)));
        srv[1] = Keep(v);
    }

    // t6 + b1 exactly as MhcPreviewSync writes them.
    void SetPreview(const MhcScanout& s) {
        std::vector<float> rows((size_t)DWM_HOOK_MHC_PREVIEW_LUT_MAX * 3, 0.0f);
        for (int ch = 0; ch < 3; ch++)
            std::copy(s.lut[ch].begin(), s.lut[ch].end(), rows.begin() + (size_t)ch * DWM_HOOK_MHC_PREVIEW_LUT_MAX);
        srv[6] = Tex2D(DWM_HOOK_MHC_PREVIEW_LUT_MAX, 3, DXGI_FORMAT_R32_FLOAT, rows.data(),
                       DWM_HOOK_MHC_PREVIEW_LUT_MAX * 4);
        float c[12] = {};
        for (int r = 0; r < 3; r++)
            for (int col = 0; col < 3; col++) c[r * 4 + col] = s.applied[r * 3 + col];
        c[3] = 1.0f;
        c[7] = (float)s.lutSize;
        dc->UpdateSubresource(mhcCb, 0, nullptr, c, 0, 0);
    }

    std::vector<float> Run(const std::vector<float>& inputRgba, const HookCb& c, bool preview) {
        srv[0] = Tex2D(W, 1, DXGI_FORMAT_R32G32B32A32_FLOAT, inputRgba.data(), W * 16);
        dc->UpdateSubresource(cb, 0, nullptr, &c, 0, 0);
        UINT stride = 16, offset = 0;
        dc->IASetInputLayout(layout);
        dc->IASetVertexBuffers(0, 1, &vb, &stride, &offset);
        dc->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        dc->VSSetShader(vs, nullptr, 0);
        dc->PSSetShader(ps, nullptr, 0);
        ID3D11Buffer* cbs[2] = { cb, preview ? mhcCb : nullptr };   // b1 unbound = off, as hook_render.cpp binds it
        dc->PSSetConstantBuffers(0, 2, cbs);
        ID3D11ShaderResourceView* views[7] = { srv[0], srv[1], srv[2], nullptr, nullptr, nullptr,
                                               preview ? srv[6] : nullptr };
        dc->PSSetShaderResources(0, 7, views);
        dc->PSSetSamplers(0, 3, smp);
        D3D11_VIEWPORT vp = { 0, 0, (float)W, 1, 0, 1 };
        dc->RSSetViewports(1, &vp);
        dc->OMSetRenderTargets(1, &rtv, nullptr);
        dc->Draw(6, 0);
        dc->CopyResource(staging, rt);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));
        std::vector<float> out((size_t)W * 4);
        std::memcpy(out.data(), m.pData, (size_t)W * 16);
        dc->Unmap(staging, 0);
        return out;
    }
};

// The hook's scRGB -> BT.2100 matrix (hook_shader.h: 10000 nits = 1, the 80/10000 scale included), in double.
void HookTo2100(const float* s, double out[3]) {
    static const double M[3][3] = {
        {2939026994.0 / 585553224375.0, 9255011753.0 / 3513319346250.0, 173911579.0 / 501902763750.0},
        {76515593.0 / 138420033750.0, 6109575001.0 / 830520202500.0, 75493061.0 / 830520202500.0},
        {12225392.0 / 93230009375.0, 1772384008.0 / 2517210253125.0, 18035212433.0 / 2517210253125.0}};
    for (int r = 0; r < 3; r++) out[r] = M[r][0] * s[0] + M[r][1] * s[1] + M[r][2] * s[2];
}
double PqOetfD(double l) {
    const double ym = std::pow((std::max)(l, 0.0), 0.1593017578125);
    return std::pow((0.8359375 + 18.8515625 * ym) / (1.0 + 18.6875 * ym), 78.84375);
}
double PqEotfD(double pq) {
    const double vm = std::pow((std::max)(pq, 0.0), 1.0 / 78.84375);
    return std::pow((std::max)(vm - 0.8359375, 0.0) / (18.8515625 - 18.6875 * vm), 1.0 / 0.1593017578125);
}
double SrgbOetfD(double l) { return l <= 0.0031308 ? l * 12.92 : 1.055 * std::pow(l, 1.0 / 2.4) - 0.055; }
double SrgbEotfD(double v) { return v <= 0.04045 ? v / 12.92 : std::pow((v + 0.055) / 1.055, 2.4); }

MHC2ProfileParams HookParams(bool hdr) {
    MHC2ProfileParams p;
    p.monitorName = L"HookPreviewTest";
    p.isHDR = hdr;
    p.peakNits = hdr ? 1450.0f : 80.0f;
    p.primariesEnabled = true;
    p.displayPrimaries = { 0.6923f, 0.3033f, 0.1783f, 0.7519f, 0.1513f, 0.0646f, 0.3155f, 0.3267f };
    p.whiteBalanceGains[0] = 0.97f; p.whiteBalanceGains[1] = 1.0f; p.whiteBalanceGains[2] = 1.04f;
    p.grayscaleEnabled = true;
    p.grayscale.enabled = true;
    p.grayscale.pointCount = 20;
    for (int i = 0; i < 20; i++) {
        const float t = (float)i / 19.0f, b = hdr ? t : t * t;
        p.grayscale.points[i] = b;
        p.grayscale.pointsR[i] = (std::min)(1.0f, b * 1.02f);
        p.grayscale.pointsG[i] = b;
        p.grayscale.pointsB[i] = b * 0.97f;
    }
    p.correctionGrayscaleEnabled = true;
    p.correctionGrayscale.enabled = true;
    p.correctionGrayscale.pointCount = 10;
    for (int i = 0; i < 10; i++) {
        const float t = (float)i / 9.0f, b = hdr ? t : t * t;
        const float bump = (i == 3 ? 1.06f : (i == 6 ? 0.95f : 1.0f));
        p.correctionGrayscale.points[i] = (std::min)(1.0f, b * bump);
        p.correctionGrayscale.pointsR[i] = (std::min)(1.0f, b * bump * 0.99f);
        p.correctionGrayscale.pointsG[i] = (std::min)(1.0f, b * bump);
        p.correctionGrayscale.pointsB[i] = (std::min)(1.0f, b * bump * 1.015f);
    }
    if (hdr) { p.desktopGammaEnabled = true; p.sdrWhiteNits = 116.0f; }
    return p;
}

std::vector<float> SwirlCube(int n) {
    std::vector<float> c((size_t)n * n * n * 4);
    for (int b = 0; b < n; b++)
        for (int g = 0; g < n; g++)
            for (int r = 0; r < n; r++) {
                const float R = (float)r / (n - 1), G = (float)g / (n - 1), B = (float)b / (n - 1);
                float* o = &c[(((size_t)b * n + g) * n + r) * 4];
                o[0] = powf(G, 1.1f);
                o[1] = 0.8f * R + 0.2f * B;
                o[2] = B * 0.9f;
                o[3] = 1.0f;
            }
    return c;
}

}  // namespace

TEST_CASE("MHC preview (hook): the real hook pixel shader applies the bake, exactly, last") {
    HookGpu gpu;
    if (!gpu.Init()) {
        if (gpu.unavailable) { MESSAGE("skipped: " << gpu.error); return; }
        FAIL("hook shader setup: " << gpu.error);
    }
    std::mt19937 rng(20261013);
    std::uniform_real_distribution<float> u(0.0f, 1.0f);
    const int N = HookGpu::W;
    const std::vector<float> cube = SwirlCube(17);
    std::vector<float> id2(8 * 4);
    for (int i = 0; i < 8; i++) { id2[i * 4] = (float)(i & 1); id2[i * 4 + 1] = (float)((i >> 1) & 1); id2[i * 4 + 2] = (float)((i >> 2) & 1); id2[i * 4 + 3] = 1; }

    for (int colorMode : { 0, 1, 2 }) {   // legacy SDR, HDR, ACM SDR (hook_render.cpp)
        const bool hdr = colorMode == 1;
        CAPTURE(colorMode);
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(HookParams(hdr), s));
        gpu.SetPreview(s);

        std::vector<float> in((size_t)N * 4);
        for (int i = 0; i < N; i++) {
            for (int ch = 0; ch < 3; ch++) {
                float v;
                if (i < 256) v = (float)i / 255.0f;   // a gray ramp, then random colours
                else v = u(rng);
                if (colorMode == 0) v = std::round(v * 255.0f) / 255.0f;                 // 8-bit code
                else if (colorMode == 2) v = SrgbEOTF(v) * (i >= 256 ? 1.1f : 1.0f) - (i >= 256 ? 0.03f : 0.0f);
                else v = PqEOTF(v) * 125.0f;                                             // scRGB, 80 nits = 1
                in[i * 4 + ch] = v;
            }
            in[i * 4 + 3] = 1.0f;
        }

        for (bool withCube : { false, true }) {
            CAPTURE(withCube);
            if (withCube) gpu.SetLut3D(17, cube); else gpu.SetLut3D(2, id2);
            HookCb c = {};
            c.lutSize = withCube ? 17 : 2;
            c.colorMode = colorMode;
            c.ditherLevels = 1 << 20;   // the legacy ordered dither below 1e-6, so the outputs compare exactly
            c.hasLut = withCube ? 1 : 0;
            c.hdrDitherLsb = 0.0f;
            const std::vector<float> normal = gpu.Run(in, c, false);   // what the pass outputs without the preview
            const std::vector<float> got = gpu.Run(in, c, true);

            double worst = 0.0, worstBlackNits = 0.0;
            for (int i = 0; i < N; i++) {
                const float* src = &normal[i * 4];
                float lin[3], want[3];
                double have[3];
                if (colorMode == 0) {
                    const float* sig = withCube ? src : &in[i * 4];
                    for (int ch = 0; ch < 3; ch++) lin[ch] = (float)SrgbEotfD(std::clamp(sig[ch], 0.0f, 1.0f));
                } else if (colorMode == 2) {
                    for (int ch = 0; ch < 3; ch++) lin[ch] = withCube ? src[ch] : std::clamp(in[i * 4 + ch], 0.0f, 1.0f);
                } else {
                    double l[3];
                    HookTo2100(withCube ? src : &in[i * 4], l);
                    for (int ch = 0; ch < 3; ch++) lin[ch] = (float)l[ch];
                }
                EvalMhcScanout(s, lin, want);
                const float* o = &got[i * 4];
                if (colorMode == 0) {
                    for (int ch = 0; ch < 3; ch++) have[ch] = o[ch];
                } else if (colorMode == 2) {
                    for (int ch = 0; ch < 3; ch++) have[ch] = SrgbOetfD(std::clamp((double)o[ch], 0.0, 1.0));
                } else {
                    double l[3];
                    HookTo2100(o, l);
                    for (int ch = 0; ch < 3; ch++) {
                        if (want[ch] < 2e-4f) {   // PQ too steep in fp32 light near black: compare in light
                            worstBlackNits = (std::max)(worstBlackNits, std::fabs(l[ch] - PqEotfD(want[ch])) * 1e4);
                            have[ch] = want[ch];
                        } else {
                            have[ch] = PqOetfD(l[ch]);
                        }
                    }
                }
                for (int ch = 0; ch < 3; ch++) worst = (std::max)(worst, std::fabs(have[ch] - want[ch]));
            }
            const std::string label = std::string("hook colorMode ") + std::to_string(colorMode) + (withCube ? " +cube" : "");
            MESSAGE(label << ": worst |shader - bake| = " << worst << " (" << worst * 1023.0 << " 10-bit codes)"
                    << "; deepest blacks within " << worstBlackNits << " nits");
            CHECK(worst < (hdr ? 1e-5 : 3e-6));
            CHECK(worstBlackNits < 1e-6);
        }
    }
}

TEST_CASE("MHC preview (hook): the shader splices the shared stage once and binds it at b1 / t6") {
    const std::string text(g_shaders, sizeof g_shaders);
    const size_t at = text.find(DLUT_MHC_SCANOUT_HLSL);
    CHECK(at != std::string::npos);
    CHECK(text.find(DLUT_MHC_SCANOUT_HLSL, at + 1) == std::string::npos);
    ID3DBlob* blob = nullptr;
    ID3DBlob* msgs = nullptr;
    HRESULT hr = D3DCompile(g_shaders, sizeof g_shaders, nullptr, nullptr, nullptr, "PS", "ps_5_0", 0, 0, &blob, &msgs);
    if (msgs) msgs->Release();
    REQUIRE(SUCCEEDED(hr));
    ID3D11ShaderReflection* refl = nullptr;
    REQUIRE(SUCCEEDED(D3DReflect(blob->GetBufferPointer(), blob->GetBufferSize(),
                                 __uuidof(ID3D11ShaderReflection), (void**)&refl)));
    D3D11_SHADER_INPUT_BIND_DESC bd = {};
    REQUIRE(SUCCEEDED(refl->GetResourceBindingDescByName("mhcLutTex", &bd)));
    CHECK(bd.BindPoint == 6u);
    REQUIRE(SUCCEEDED(refl->GetResourceBindingDescByName("MhcPreview", &bd)));
    CHECK(bd.BindPoint == 1u);
    ID3D11ShaderReflectionConstantBuffer* c = refl->GetConstantBufferByName("MhcPreview");
    D3D11_SHADER_BUFFER_DESC cd = {};
    REQUIRE(SUCCEEDED(c->GetDesc(&cd)));
    CHECK(cd.Size == 48u);   // = the ByteWidth MhcPreviewSync creates
    refl->Release();
    blob->Release();
}

TEST_CASE("MHC preview (hook): the shared block keeps its layout and the config's generation word") {
    CHECK(offsetof(DwmHookSharedConfig, mhcPreviewGen) == 452u);
    CHECK(offsetof(DwmHookSharedConfig, lutOffMask) == 448u);
    CHECK(sizeof(DwmHookSharedConfig) == 464u);
    CHECK(offsetof(DwmHookMhcPreview, gen) == 8u);
    CHECK(offsetof(DwmHookMhcPreview, matrix) == 32u);
    CHECK(offsetof(DwmHookMhcPreview, lut) == 80u);
    CHECK(offsetof(DwmHookMhcPreview, ackGen) == 80u + 3u * 4u * DWM_HOOK_MHC_PREVIEW_LUT_MAX);
}
