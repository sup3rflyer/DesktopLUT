// The live edit preview is exact to the bake (mhc_preview.h): the profile file carries exactly the numbers the
// preview uses, the preview composes exactly what the bake installs, and the production overlay shader applies
// them exactly like the CPU twin — to exactly what the overlay would otherwise hand to scanout.

#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include "doctest.h"
#include "mhc.h"
#include "mhc_preview.h"
#include "gui_mhc.h"
#include "globals.h"
#include "shader.h"
#include "../shared/mhc_scanout_hlsl.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "d3dcompiler.lib")

namespace {

bool SameBits(float a, float b) { return std::memcmp(&a, &b, sizeof(float)) == 0; }

bool FindTagP(const std::vector<uint8_t>& data, const char sig[4], uint32_t& off, uint32_t& size) {
    if (data.size() < 132) return false;
    const uint32_t want = ((uint32_t)(uint8_t)sig[0] << 24) | ((uint32_t)(uint8_t)sig[1] << 16) |
                          ((uint32_t)(uint8_t)sig[2] << 8) | (uint32_t)(uint8_t)sig[3];
    const uint32_t tagCount = ReadBE32(data.data() + 128);
    for (uint32_t i = 0; i < tagCount; i++) {
        const uint8_t* e = data.data() + 132 + i * 12;
        if (e + 12 > data.data() + data.size()) return false;
        if (ReadBE32(e) == want) { off = ReadBE32(e + 4); size = ReadBE32(e + 8); return true; }
    }
    return false;
}

// The MHC2 tag of a generated profile, as Windows reads it.
struct Mhc2Tag {
    uint32_t lutSize = 0;
    int32_t matrix[12] = {};
    std::vector<int32_t> lut[3];
};
Mhc2Tag ReadMhc2(const std::vector<uint8_t>& profile) {
    Mhc2Tag t;
    uint32_t off = 0, size = 0;
    REQUIRE(FindTagP(profile, "MHC2", off, size));
    const uint8_t* p = profile.data() + off;
    t.lutSize = ReadBE32(p + 8);
    const uint8_t* m = p + ReadBE32(p + 20);
    for (int i = 0; i < 12; i++) t.matrix[i] = (int32_t)ReadBE32(m + i * 4);
    for (int ch = 0; ch < 3; ch++) {
        const uint8_t* l = p + ReadBE32(p + 24 + ch * 4) + 8;   // skip 'sf32' + reserved
        t.lut[ch].resize(t.lutSize);
        for (uint32_t j = 0; j < t.lutSize; j++) t.lut[ch][j] = (int32_t)ReadBE32(l + j * 4);
    }
    return t;
}

// Every correction a profile can carry, per channel: a native-gamut panel with a measured white, a white-balance
// move, a per-channel base grayscale, a per-channel correction (SDR: with the 2.4 gamma; HDR: desktop gamma on).
MHC2ProfileParams FullParams(bool hdr) {
    MHC2ProfileParams p;
    p.monitorName = L"PreviewTest";
    p.isHDR = hdr;
    p.peakNits = hdr ? 1450.0f : 80.0f;
    p.primariesEnabled = true;
    p.displayPrimaries = { 0.6923f, 0.3033f, 0.1783f, 0.7519f, 0.1513f, 0.0646f, 0.3155f, 0.3267f };
    p.whiteBalanceGains[0] = 0.97f; p.whiteBalanceGains[1] = 1.0f; p.whiteBalanceGains[2] = 1.04f;
    p.grayscaleEnabled = true;
    p.grayscale.enabled = true;
    p.grayscale.pointCount = 20;
    p.grayscale.peakNits = 1450.0f;
    for (int i = 0; i < 20; i++) {
        const float t = (float)i / 19.0f;
        const float b = hdr ? t : t * t;
        p.grayscale.points[i] = b;
        p.grayscale.pointsR[i] = (std::min)(1.0f, b * 1.02f);
        p.grayscale.pointsG[i] = b;
        p.grayscale.pointsB[i] = b * 0.97f;
    }
    p.correctionGrayscaleEnabled = true;
    p.correctionGrayscale.enabled = true;
    p.correctionGrayscale.pointCount = 10;
    p.correctionGrayscale.use24Gamma = !hdr;
    for (int i = 0; i < 10; i++) {
        const float t = (float)i / 9.0f;
        const float b = hdr ? t : t * t;
        const float bump = (i == 3 ? 1.06f : (i == 6 ? 0.95f : 1.0f));
        p.correctionGrayscale.points[i] = (std::min)(1.0f, b * bump);
        p.correctionGrayscale.pointsR[i] = (std::min)(1.0f, b * bump * 0.99f);
        p.correctionGrayscale.pointsG[i] = (std::min)(1.0f, b * bump);
        p.correctionGrayscale.pointsB[i] = (std::min)(1.0f, b * bump * 1.015f);
    }
    if (hdr) {
        p.desktopGammaEnabled = true;
        p.sdrWhiteNits = 116.0f;
    }
    return p;
}

// The same, as settings (what the editors and the bake start from).
MHCSettings FullSettings() {
    MHCSettings m;
    m.enabled = true;
    m.profileName = L"DesktopLUT_Test.icm";
    m.primariesEnabled = true;
    m.primariesPreset = g_numPresetPrimaries - 1;   // custom
    m.customPrimaries = { 0.6923f, 0.3033f, 0.1783f, 0.7519f, 0.1513f, 0.0646f, 0.3155f, 0.3267f, L"Custom" };
    m.whiteBalanceEnabled = true;
    m.whiteBalanceWx = 0.3100f;
    m.whiteBalanceWy = 0.3300f;
    m.baseGrayscale.enabled = true;
    m.baseGrayscale.pointCount = 20;
    m.baseGrayscale.peakNits = 1450.0f;
    m.baseGrayscale.initLinear();
    for (int i = 0; i < 20; i++) {
        m.baseGrayscale.rgbDeviations[0][i] = 1.02f;
        m.baseGrayscale.rgbDeviations[2][i] = 0.97f;
    }
    m.correctionGrayscale.enabled = true;
    m.correctionGrayscale.pointCount = 10;
    m.correctionGrayscale.initLinear();
    m.correctionGrayscale.points[3] *= 1.06f;
    m.correctionGrayscale.rgbDeviations[0][3] = 0.99f;
    m.correctionGrayscale.rgbDeviations[2][6] = 1.015f;
    m.desktopGammaEnabled = true;
    m.dgSdrWhiteNits = 116.0f;
    return m;
}

void CheckScanoutsIdentical(const MhcScanout& a, const MhcScanout& b) {
    REQUIRE(a.lutSize == b.lutSize);
    for (int i = 0; i < 12; i++) CHECK(SameBits(a.emit[i], b.emit[i]));
    for (int i = 0; i < 9; i++) CHECK(SameBits(a.applied[i], b.applied[i]));
    int mismatches = 0;
    for (int ch = 0; ch < 3; ch++)
        for (int j = 0; j < a.lutSize; j++)
            if (!SameBits(a.lut[ch][j], b.lut[ch][j])) mismatches++;
    CHECK(mismatches == 0);
}

// The written tag carries exactly these numbers.
void CheckFileCarries(const std::vector<uint8_t>& profile, const MhcScanout& s) {
    const Mhc2Tag t = ReadMhc2(profile);
    REQUIRE((int)t.lutSize == s.lutSize);
    for (int i = 0; i < 12; i++) CHECK(t.matrix[i] == FloatToS15Fixed16(s.emit[i]));
    int mismatches = 0;
    for (int ch = 0; ch < 3; ch++)
        for (int j = 0; j < s.lutSize; j++)
            if (!SameBits((float)t.lut[ch][j] / 65536.0f, s.lut[ch][j])) mismatches++;
    CHECK(mismatches == 0);
}

}  // namespace

TEST_CASE("MHC preview: the profile file carries exactly the scanout the preview applies") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        const MHC2ProfileParams p = FullParams(hdr);
        std::vector<uint8_t> profile;
        REQUIRE(GenerateMHC2Profile(p, profile));
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(p, s));
        CHECK(s.lutSize == (hdr ? 4096 : 1024));
        CheckFileCarries(profile, s);
    }
}

TEST_CASE("MHC preview: base computed once, then composed per edit, is the whole bake") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MHC2ProfileParams noCorr = FullParams(hdr);
        noCorr.correctionGrayscaleEnabled = false;
        noCorr.correctionGrayscale.enabled = false;
        MhcScanout base;
        REQUIRE(ComputeMhcScanoutBase(noCorr, base));
        const MHC2ProfileParams full = FullParams(hdr);
        MhcScanout composed, whole;
        ComposeMhcScanout(full, base, composed);
        REQUIRE(ComputeMhcScanout(full, whole));
        CheckScanoutsIdentical(composed, whole);
    }
}

TEST_CASE("MHC preview: from the settings, the preview composes exactly what the bake installs") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MHCSettings m = FullSettings();
        if (!hdr) m.correctionGrayscale.use24Gamma = true;
        // The session is prepared once, with the curve the edit starts from.
        MhcPreviewSession s;
        REQUIRE(PrepareMhcPreview(m, hdr, 0, s));
        CHECK(!(s.perm & MHCSettings::PERM_GS));

        auto bakeOf = [&](const MHCSettings& now, MhcScanout& out, std::vector<uint8_t>& file) {
            MHC2ProfileParams bp;
            // The edit holds desktop gamma out: the bake it previews is the variant without it.
            const uint8_t perm = (uint8_t)(ComputeMhcPermutation(now, hdr) & ~MHCSettings::PERM_DG);
            REQUIRE(BuildMHC2ParamsForPerm(now, hdr, 0, perm, bp));
            REQUIRE(ComputeMhcScanout(bp, out));
            REQUIRE(GenerateMHC2Profile(bp, file));
        };
        auto check = [&](const MHCSettings& now) {
            MhcScanout bake, preview;
            std::vector<uint8_t> file;
            bakeOf(now, bake, file);
            ComposeMhcPreview(s, now.correctionGrayscale, preview);
            CheckScanoutsIdentical(preview, bake);
            CheckFileCarries(file, preview);
        };

        SUBCASE("the curve as the edit starts") { check(m); }
        SUBCASE("edited points and channels (the session's base never sees the curve)") {
            m.correctionGrayscale.points[2] *= 0.93f;
            m.correctionGrayscale.points[8] = (std::min)(1.0f, m.correctionGrayscale.points[8] * 1.03f);
            m.correctionGrayscale.rgbDeviations[1][5] = 1.02f;
            check(m);
        }
        SUBCASE("a point count change") {
            m.correctionGrayscale.pointCount = 20;
            m.correctionGrayscale.initLinear();
            m.correctionGrayscale.points[11] *= 1.04f;
            check(m);
        }
        SUBCASE("the curve switched off") {
            m.correctionGrayscale.enabled = false;
            check(m);
        }
    }
}

TEST_CASE("MHC preview: an edited BASE curve previews exactly what the bake installs") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MHCSettings m = FullSettings();   // a base from points (no imported file), a fine-tune, WB, DG
        m.baseGrayscale.peakNits = 1450.0f;
        auto check = [&](const MHCSettings& prepared, const MHCSettings& edited) {
            MhcPreviewSession s;
            REQUIRE(PrepareMhcPreview(prepared, hdr, 0, s));
            MhcScanout preview, bake;
            ComposeMhcPreviewBase(s, edited.baseGrayscale, edited.correctionGrayscale, preview);
            MHC2ProfileParams bp;
            REQUIRE(BuildMHC2ParamsForPerm(edited, hdr, 0,
                                           (uint8_t)(ComputeMhcPermutation(edited, hdr) & ~MHCSettings::PERM_DG), bp));
            REQUIRE(ComputeMhcScanout(bp, bake));
            CheckScanoutsIdentical(preview, bake);
            std::vector<uint8_t> file;
            REQUIRE(GenerateMHC2Profile(bp, file));
            CheckFileCarries(file, preview);
        };
        SUBCASE("points and channels edited") {
            MHCSettings e = m;
            e.baseGrayscale.points[4] *= 1.05f;
            e.baseGrayscale.points[13] *= 0.96f;
            e.baseGrayscale.rgbDeviations[1][9] = 1.03f;
            check(m, e);
        }
        SUBCASE("a base switched on by the edit (the HDR peak follows it)") {
            MHCSettings off = m;
            off.baseGrayscale.enabled = false;
            MHCSettings e = m;
            e.baseGrayscale.points[7] *= 1.04f;
            check(off, e);
        }
        SUBCASE("a new point count") {
            MHCSettings e = m;
            e.baseGrayscale.pointCount = 32;
            e.baseGrayscale.initLinear();
            e.baseGrayscale.points[20] *= 0.97f;
            check(m, e);
        }
    }
}

TEST_CASE("MHC preview: desktop gamma is held out of the preview; the DONE bake has it again") {
    MHCSettings m = FullSettings();   // HDR desktop gamma on at 116 nits
    MhcPreviewSession s;
    REQUIRE(PrepareMhcPreview(m, true, 0, s));
    CHECK(!(s.perm & MHCSettings::PERM_DG));
    CHECK_FALSE(s.params.desktopGammaEnabled);
    MhcScanout preview, withDg;
    ComposeMhcPreview(s, m.correctionGrayscale, preview);
    MHC2ProfileParams bp;
    REQUIRE(BuildMHC2ParamsForPerm(m, true, 0, ComputeMhcPermutation(m, true), bp));
    CHECK(bp.desktopGammaEnabled);   // what DONE installs
    REQUIRE(ComputeMhcScanout(bp, withDg));
    // They differ exactly in the SDR range (below the SDR white the 2.2 remap applies), and agree above it.
    const int n = preview.lutSize;
    int differBelow = 0, differAbove = 0;
    for (int j = 1; j < n; j++) {
        const float v = preview.lut[1][j];
        const bool below = PqEOTF(v) * 10000.0f < 100.0f;
        const bool above = PqEOTF(v) * 10000.0f > 130.0f;
        const bool same = preview.lut[1][j] == withDg.lut[1][j];
        if (below && !same) differBelow++;
        if (above && !same) differAbove++;
    }
    CHECK(differBelow > 0);
    CHECK(differAbove == 0);
    // SDR has no desktop gamma: nothing to hold.
    MhcPreviewSession sdr;
    REQUIRE(PrepareMhcPreview(m, false, 0, sdr));
    CHECK_FALSE(sdr.params.desktopGammaEnabled);
}

TEST_CASE("MHC preview: with the grayscale stage switched off, neither the preview nor the bake has the curve") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MHCSettings m = FullSettings();
        m.bypassGrayscale = true;
        MhcPreviewSession s;
        REQUIRE(PrepareMhcPreview(m, hdr, 0, s));
        MHC2ProfileParams bp;
        REQUIRE(BuildMHC2ParamsForPerm(m, hdr, 0, (uint8_t)(ComputeMhcPermutation(m, hdr) & ~MHCSettings::PERM_DG), bp));
        MhcScanout bake, preview;
        REQUIRE(ComputeMhcScanout(bp, bake));
        ComposeMhcPreview(s, m.correctionGrayscale, preview);
        CheckScanoutsIdentical(preview, bake);
        // ...and it is the identity LUT (both grayscale stages, desktop gamma included, are off).
        for (int j = 0; j < bake.lutSize; j += 97)
            CHECK(preview.lut[1][j] == doctest::Approx((float)j / (float)(bake.lutSize - 1)).epsilon(2e-5));
    }
}

TEST_CASE("MHC preview: the as-applied matrix is the written tag's, within s15Fixed16 of the intended one") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        const MHC2ProfileParams p = FullParams(hdr);
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(p, s));
        float emit[12], intended[9];
        ComputeMHC2Matrix(MhcSourcePrimaries(p), p.displayPrimaries, hdr, emit, p.whiteBalanceGains, intended);
        for (int i = 0; i < 9; i++) CHECK(std::fabs(s.applied[i] - intended[i]) < 1e-4f);
    }
}

TEST_CASE("MHC preview: an identity profile passes the wire through") {
    for (bool hdr : { false, true }) {
        CAPTURE(hdr);
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(BuildIdentityMHC2Params(hdr, 1000.0f, L"id"), s));
        double worst = 0.0;
        for (int k = 0; k <= 1023; k++) {
            const float sig = (float)k / 1023.0f;
            const float l = hdr ? PqEOTF(sig) : SrgbEOTF(sig);
            const float lin[3] = { l, l, l };
            float out[3];
            EvalMhcScanout(s, lin, out);
            worst = (std::max)(worst, (double)std::fabs(out[1] - sig));
        }
        CHECK(worst < 4e-5);
    }
}

// =============================================================================================
// WARP: the production overlay pixel shader with the preview on
// =============================================================================================
namespace {

// Builds the overlay's LUT textures like gpu.cpp and runs g_psSource on a W x 1 strip.
struct OverlayGpu {
    static constexpr int W = 1024;
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    std::vector<IUnknown*> keep;
    ID3D11VertexShader* vs = nullptr;
    ID3D11PixelShader* ps = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11ShaderResourceView* srv[12] = {};
    ID3D11SamplerState* smp[3] = {};
    ID3D11Texture2D* rt = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11Texture2D* staging = nullptr;
    ID3D11Texture2D* capture = nullptr;
    std::string error;
    bool unavailable = false;

    ~OverlayGpu() {
        if (dc) dc->ClearState();
        for (IUnknown* u : keep) if (u) u->Release();
        if (dc) dc->Release();
        if (dev) dev->Release();
    }
    template <typename T> T* Keep(T* p) { keep.push_back(p); return p; }

    ID3D11ShaderResourceView* Tex2D(int w, int h, DXGI_FORMAT fmt, const void* data, UINT pitch,
                                    D3D11_USAGE usage = D3D11_USAGE_IMMUTABLE, ID3D11Texture2D** outTex = nullptr) {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = w; d.Height = h; d.MipLevels = 1; d.ArraySize = 1; d.Format = fmt; d.SampleDesc.Count = 1;
        d.Usage = usage; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = { data, pitch, 0 };
        ID3D11Texture2D* t = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateTexture2D(&d, data ? &init : nullptr, &t)));
        Keep(t);
        if (outTex) *outTex = t;
        ID3D11ShaderResourceView* v = nullptr;
        REQUIRE(SUCCEEDED(dev->CreateShaderResourceView(t, nullptr, &v)));
        return Keep(v);
    }

    bool Init() {
        D3D_FEATURE_LEVEL fl;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0, D3D11_SDK_VERSION,
                                     &dev, &fl, &dc))) {
            unavailable = true;
            error = "no WARP device";
            return false;
        }
        auto compile = [&](const char* src, const char* target) -> ID3DBlob* {
            ID3DBlob* blob = nullptr;
            ID3DBlob* msgs = nullptr;
            HRESULT hr = D3DCompile(src, strlen(src), nullptr, nullptr, nullptr, "main", target, 0, 0, &blob, &msgs);
            if (FAILED(hr)) error = msgs ? (const char*)msgs->GetBufferPointer() : "compile failed";
            if (msgs) msgs->Release();
            return SUCCEEDED(hr) ? blob : nullptr;
        };
        ID3DBlob* vb = compile(g_vsSource, "vs_5_0");
        if (!vb) return false;
        dev->CreateVertexShader(vb->GetBufferPointer(), vb->GetBufferSize(), nullptr, &vs);
        vb->Release();
        Keep(vs);
        ID3DBlob* pb = compile(g_psSource, "ps_5_0");   // as gpu.cpp compiles it
        if (!pb) return false;
        dev->CreatePixelShader(pb->GetBufferPointer(), pb->GetBufferSize(), nullptr, &ps);
        pb->Release();
        Keep(ps);
        D3D11_BUFFER_DESC cbd = { 592, D3D11_USAGE_DEFAULT, D3D11_BIND_CONSTANT_BUFFER, 0, 0, 0 };
        REQUIRE(SUCCEEDED(dev->CreateBuffer(&cbd, nullptr, &cb)));
        Keep(cb);

        // t2: a flat 0.5 tile = no SDR dither ((noise - 0.5) / 1024); the HDR dither is off in the cbuffer.
        std::vector<float> half(64 * 64, 0.5f);
        srv[2] = Tex2D(64, 64, DXGI_FORMAT_R32_FLOAT, half.data(), 64 * 4);
        // t5 / t6 PQ (sqrt-domain OETF, uniform EOTF) and t7 / t8 sRGB, exactly as gpu.cpp builds them.
        std::vector<float> pqO(4096), pqE(4096), sO(1024), sE(1024);
        for (int i = 0; i < 4096; i++) {
            const float t = (float)i / 4095.0f;
            pqO[i] = PqOETF(t * t);
            pqE[i] = PqEOTF(t);
        }
        for (int i = 0; i < 1024; i++) {
            const float t = (float)i / 1023.0f;
            sO[i] = t <= 0.0031308f ? 12.92f * t : 1.055f * powf(t, 1.0f / 2.4f) - 0.055f;
            sE[i] = t <= 0.04045f ? t / 12.92f : powf((t + 0.055f) / 1.055f, 2.4f);
        }
        srv[5] = Tex2D(4096, 1, DXGI_FORMAT_R32_FLOAT, pqO.data(), 4096 * 4);
        srv[6] = Tex2D(4096, 1, DXGI_FORMAT_R32_FLOAT, pqE.data(), 4096 * 4);
        srv[7] = Tex2D(1024, 1, DXGI_FORMAT_R32_FLOAT, sO.data(), 1024 * 4);
        srv[8] = Tex2D(1024, 1, DXGI_FORMAT_R32_FLOAT, sE.data(), 1024 * 4);

        D3D11_SAMPLER_DESC sd = {};
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.ComparisonFunc = D3D11_COMPARISON_NEVER;
        sd.MaxLOD = D3D11_FLOAT32_MAX;
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &smp[0])));
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        REQUIRE(SUCCEEDED(dev->CreateSamplerState(&sd, &smp[1])));
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
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

    // t1: a 3D LUT (n^3, RGBA float, r fastest) or a 2^3 identity when none.
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

    // t11: the preview's LUTs, exactly as render.cpp SyncMhcPreview writes them (rows R/G/B, 4096 wide).
    void SetPreview(const MhcScanout& s) {
        std::vector<float> rows((size_t)kMhcPreviewLutMax * 3, 0.0f);
        for (int ch = 0; ch < 3; ch++)
            std::copy(s.lut[ch].begin(), s.lut[ch].end(), rows.begin() + (size_t)ch * kMhcPreviewLutMax);
        srv[11] = Tex2D(kMhcPreviewLutMax, 3, DXGI_FORMAT_R32_FLOAT, rows.data(), kMhcPreviewLutMax * 4);
    }

    // One strip of W pixels: input RGB (RGBA float), cbuffer as render.cpp writes it.
    std::vector<float> Run(const std::vector<float>& inputRgba, const float cbData[148]) {
        srv[0] = Tex2D(W, 1, DXGI_FORMAT_R32G32B32A32_FLOAT, inputRgba.data(), W * 16);
        dc->UpdateSubresource(cb, 0, nullptr, cbData, 0, 0);
        dc->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        dc->VSSetShader(vs, nullptr, 0);
        dc->PSSetShader(ps, nullptr, 0);
        dc->PSSetConstantBuffers(0, 1, &cb);
        dc->PSSetShaderResources(0, 12, srv);
        dc->PSSetSamplers(0, 3, smp);
        D3D11_VIEWPORT vp = { 0, 0, (float)W, 1, 0, 1 };
        dc->RSSetViewports(1, &vp);
        dc->OMSetRenderTargets(1, &rtv, nullptr);
        dc->Draw(3, 0);
        dc->CopyResource(staging, rt);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));
        std::vector<float> out((size_t)W * 4);
        std::memcpy(out.data(), m.pData, (size_t)W * 16);
        dc->Unmap(staging, 0);
        return out;
    }
};

// The cbuffer render.cpp writes for a monitor with no corrections (usePassthrough unless a cube is loaded).
void BaseCb(float cb[148], bool hdr, bool acm, bool cube, int cubeSize) {
    std::fill(cb, cb + 148, 0.0f);
    cb[0] = hdr ? 1.0f : 0.0f;
    cb[1] = 80.0f;
    cb[2] = 1000.0f;
    cb[3] = (float)cubeSize;
    cb[5] = 1.0f;                       // tetrahedral
    cb[6] = cube ? 0.0f : 1.0f;         // usePassthrough
    cb[8] = 20.0f;
    cb[12] = cb[17] = cb[22] = 1.0f;    // primaries identity
    cb[15] = cb[19] = cb[23] = 1.0f;    // WB gains 1
    cb[29] = acm ? 1.0f : 0.0f;
    cb[144] = cb[145] = 1.0f;
}
void PreviewCb(float cb[148], const MhcScanout& s) {
    for (int r = 0; r < 3; r++)
        for (int c = 0; c < 3; c++) cb[132 + r * 4 + c] = s.applied[r * 3 + c];
    cb[135] = 1.0f;
    cb[139] = (float)s.lutSize;
}

// The overlay's BT.709 <-> BT.2020 constants (shader.h), for reading HDR results back in the wire's terms.
void To2020(const float* scrgb, float out[3]) {
    out[0] = 0.6274039f * scrgb[0] + 0.3292830f * scrgb[1] + 0.0433131f * scrgb[2];
    out[1] = 0.0690973f * scrgb[0] + 0.9195404f * scrgb[1] + 0.0113623f * scrgb[2];
    out[2] = 0.0163914f * scrgb[0] + 0.0880133f * scrgb[1] + 0.8955953f * scrgb[2];
}
void To2020D(const float* scrgb, double out[3]) {
    out[0] = 0.6274039f * (double)scrgb[0] + 0.3292830f * (double)scrgb[1] + 0.0433131f * (double)scrgb[2];
    out[1] = 0.0690973f * (double)scrgb[0] + 0.9195404f * (double)scrgb[1] + 0.0113623f * (double)scrgb[2];
    out[2] = 0.0163914f * (double)scrgb[0] + 0.0880133f * (double)scrgb[1] + 0.8955953f * (double)scrgb[2];
}
// Exact curves for reading results back (what an exact scanout encoder makes of the shader's output).
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

// A 17^3 cube that is plainly not channel-symmetric: applying the profile before it instead of after shows.
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

std::vector<float> RandomRgba(std::mt19937& rng, int count, float lo, float hi, bool logScale) {
    std::uniform_real_distribution<float> u(0.0f, 1.0f);
    std::vector<float> v((size_t)count * 4);
    for (int i = 0; i < count; i++) {
        for (int ch = 0; ch < 3; ch++) {
            float x = u(rng);
            v[i * 4 + ch] = logScale ? lo * powf(hi / lo, x) : lo + (hi - lo) * x;
        }
        v[i * 4 + 3] = 1.0f;
    }
    return v;
}

}  // namespace

TEST_CASE("MHC preview: the production overlay shader applies the bake, exactly, last") {
    OverlayGpu gpu;
    if (!gpu.Init()) {
        if (gpu.unavailable) { MESSAGE("skipped: " << gpu.error); return; }
        FAIL("overlay shader setup: " << gpu.error);
    }
    std::mt19937 rng(20261013);
    const int N = OverlayGpu::W;
    const std::vector<float> cube = SwirlCube(17);
    std::vector<float> id2(8 * 4);
    for (int i = 0; i < 8; i++) { id2[i * 4] = (float)(i & 1); id2[i * 4 + 1] = (float)((i >> 1) & 1); id2[i * 4 + 2] = (float)((i >> 2) & 1); id2[i * 4 + 3] = 1; }

    for (int mode = 0; mode < 3; mode++) {          // 0 legacy SDR, 1 ACM SDR, 2 HDR
        const bool hdr = mode == 2, acm = mode == 1;
        CAPTURE(mode);
        MhcScanout s;
        REQUIRE(ComputeMhcScanout(FullParams(hdr), s));
        gpu.SetPreview(s);

        // Inputs: a gray ramp over the whole code range, then random colours (some outside [0, 1] in scRGB).
        std::vector<float> in((size_t)N * 4);
        for (int i = 0; i < 256; i++) {
            float v;
            if (mode == 0) v = (float)i / 255.0f;                               // 8-bit code
            else if (mode == 1) v = SrgbEOTF((float)i / 255.0f);                // linear scRGB
            else v = PqEOTF((float)i / 255.0f) * 125.0f;                        // scRGB, 80 nits = 1
            in[i * 4] = in[i * 4 + 1] = in[i * 4 + 2] = v;
            in[i * 4 + 3] = 1.0f;
        }
        const std::vector<float> rnd = mode == 0 ? RandomRgba(rng, N - 256, 0.0f, 1.0f, false)
                                     : mode == 1 ? RandomRgba(rng, N - 256, -0.05f, 1.1f, false)
                                                 : RandomRgba(rng, N - 256, 1e-4f, 125.0f, true);
        std::copy(rnd.begin(), rnd.end(), in.begin() + 256 * 4);
        if (mode == 0)   // legacy input is an 8-bit code
            for (size_t k = 0; k < in.size(); k++) if (k % 4 != 3) in[k] = std::round(in[k] * 255.0f) / 255.0f;

        for (bool withCube : { false, true }) {
            CAPTURE(withCube);
            if (withCube) gpu.SetLut3D(17, cube); else gpu.SetLut3D(2, id2);
            float cb[148];
            BaseCb(cb, hdr, acm, withCube, withCube ? 17 : 2);
            // What the overlay hands to scanout without the preview (no LUT: nothing — the desktop itself).
            const std::vector<float> normal = gpu.Run(in, cb);
            PreviewCb(cb, s);
            const std::vector<float> got = gpu.Run(in, cb);

            double worst = 0.0, worstBlackNits = 0.0;
            int worstAt = -1;
            for (int i = 0; i < N; i++) {
                // The wire signal scanout would hand the profile, from what the overlay outputs (or the desktop).
                float lin[3], want[3];
                double have[3];
                const float* src = withCube ? &normal[i * 4] : &in[i * 4];
                if (mode == 0) {
                    for (int ch = 0; ch < 3; ch++) lin[ch] = (float)SrgbEotfD(std::clamp(src[ch], 0.0f, 1.0f));
                } else if (mode == 1) {
                    for (int ch = 0; ch < 3; ch++) lin[ch] = src[ch];
                } else {
                    To2020(src, lin);
                    for (float& l : lin) l *= 80.0f / 10000.0f;
                }
                EvalMhcScanout(s, lin, want);   // the bake, on the CPU
                // ...and what the shader's output makes scanout (an identity profile now) hand the panel.
                const float* o = &got[i * 4];
                if (mode == 0) {
                    for (int ch = 0; ch < 3; ch++) have[ch] = o[ch];
                } else if (mode == 1) {
                    for (int ch = 0; ch < 3; ch++) have[ch] = SrgbOetfD(std::clamp((double)o[ch], 0.0, 1.0));
                } else {
                    double l2020[3];
                    To2020D(o, l2020);
                    for (int ch = 0; ch < 3; ch++) {
                        const double l = l2020[ch] * 80.0 / 10000.0;
                        // Near black, PQ is so steep in light that fp32 light cannot carry a PQ value to 1e-6
                        // (PQ 1e-4 is 1e-7 nits): the deepest blacks are compared in light instead.
                        if (want[ch] < 2e-4f) {
                            worstBlackNits = (std::max)(worstBlackNits, std::fabs(l - PqEotfD(want[ch])) * 1e4);
                            have[ch] = want[ch];
                        } else {
                            have[ch] = PqOetfD(l);
                        }
                    }
                }
                for (int ch = 0; ch < 3; ch++) {
                    const double d = std::fabs(have[ch] - want[ch]);
                    if (d > worst) { worst = d; worstAt = i; }
                }
            }
            const std::string label = std::string("mode ") + std::to_string(mode) + (withCube ? " +cube" : "");
            MESSAGE(label << ": worst |shader - bake| = " << worst << " (" << worst * 1023.0
                    << " 10-bit codes) at pixel " << worstAt << "; deepest blacks within " << worstBlackNits << " nits");
            // fp32 on both sides (pow on the GPU vs the CRT); HDR also reads back through the 709/2020 constants.
            CHECK(worst < (hdr ? 1e-5 : 2e-6));
            CHECK(worstBlackNits < 1e-6);
        }
    }
}

TEST_CASE("MHC preview: the overlay shader binds the preview LUTs at t11") {
    ID3DBlob* blob = nullptr;
    ID3DBlob* msgs = nullptr;
    HRESULT hr = D3DCompile(g_psSource, strlen(g_psSource), nullptr, nullptr, nullptr, "main", "ps_5_0", 0, 0,
                            &blob, &msgs);
    if (msgs) msgs->Release();
    REQUIRE(SUCCEEDED(hr));
    ID3D11ShaderReflection* refl = nullptr;
    REQUIRE(SUCCEEDED(D3DReflect(blob->GetBufferPointer(), blob->GetBufferSize(),
                                 __uuidof(ID3D11ShaderReflection), (void**)&refl)));
    D3D11_SHADER_INPUT_BIND_DESC bd = {};
    REQUIRE(SUCCEEDED(refl->GetResourceBindingDescByName("mhcPreviewLut", &bd)));
    CHECK(bd.BindPoint == 11u);
    refl->Release();
    blob->Release();
    CHECK(std::string(g_psSource).find(DLUT_MHC_SCANOUT_HLSL) != std::string::npos);
}

TEST_CASE("MHC preview: the self-heal paths expect the edit's identity profile while it runs") {
    MHCSettings m;
    m.profileName = L"DesktopLUT_Mon0_SDR_1.icm";
    CHECK(MhcScanoutName(m) == m.profileName);
    m.previewScanoutName = L"DesktopLUT_Mon0_SDR_Passthru_2.icm";
    CHECK(MhcScanoutName(m) == m.previewScanoutName);
    m.previewScanoutName.clear();
    CHECK(MhcScanoutName(m) == m.profileName);
}
