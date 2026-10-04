// Desktop gamma referenced to the Windows SDR white level (2026-10-03).
//
// In HDR, Windows composites SDR content as W * sRGB_EOTF(code), W = the display's SDR white level ("SDR content
// brightness", DisplayConfig GET_SDR_WHITE_LEVEL). Desktop gamma re-decodes that range as W * code^2.2. It used to
// assume W = 80 nits; on a PA32UCXR at W = 116 that left code 20/255 +19.7 % over gamma 2.2 (measured +19.6 %,
// DLC run 20261003_212836) and a kink where Windows' output crossed 80 nits.
//
//   1. The MHC bake (DesktopGammaPQ / GenerateMHC2Profile): W = 80 reproduces the fixed-80-nit bake bit for bit
//      (function and serialized per-channel LUTs vs a replica of the pre-change code); W = 116 is pure 2.2
//      relative to W; HDR highlights above W are untouched; DG stays per channel on the base LUT's output.
//   2. Wiring: BuildMHC2Params carries hdrMHC.dgSdrWhiteNits; RebakeHdrMhcForSdrWhite records a new level and
//      drops cached DG variants; the INI round trip (absent key = a pre-tracking profile = 80).
//   3. The DisplayConfig level -> nits conversion.
//   4. The REAL overlay HLSL (DLUT_DESKTOP_GAMMA_HLSL) on WARP: white = 1.0 is bit-identical to the pre-change
//      shader text; white = 1.45 is the same LUT referenced to W, i.e. pure 2.2 relative to W; the production
//      pixel shader splices it, compiles, and its cbuffer puts the new fields where render.cpp writes them.

#define NOMINMAX
#include <windows.h>

#include "doctest.h"
#include "mhc.h"
#include "gui_mhc.h"
#include "settings.h"
#include "displayconfig.h"
#include "globals.h"
#include "shader.h"

#include <d3d11.h>
#include <d3d11shader.h>
#include <d3dcompiler.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>
#include <vector>

namespace {

// The HDR desktop-gamma bake exactly as mhc_icc.cpp composed it before 2026-10-03 (fixed 80-nit white).
float PreChangeDesktopGammaPQ(float v) {
    float linearNits = PqEOTF(v) * 10000.0f;
    if (linearNits <= 80.0f && linearNits > 0.0f) {
        float sdrLinear = linearNits / 80.0f;
        float srgbEncoded = SrgbOETF(sdrLinear);
        float gamma22 = powf(srgbEncoded, 2.2f);
        v = PqOETF(gamma22 * 80.0f / 10000.0f);
    }
    return v;
}

bool SameBits(float a, float b) { return std::memcmp(&a, &b, sizeof(float)) == 0; }

double SrgbEotfD(double s) { return s <= 0.04045 ? s / 12.92 : std::pow((s + 0.055) / 1.055, 2.4); }

// Windows' HDR composition of an 8-bit SDR code at SDR white W, through desktop gamma referenced to dgWhite (nits).
double ComposedThroughDg(int code, double W, float dgWhite) {
    float pq = PqOETF((float)(W * SrgbEotfD(code / 255.0) / 10000.0));
    return (double)PqEOTF(DesktopGammaPQ(pq, dgWhite)) * 10000.0;
}

// Locate an ICC tag (offset/size) by its 4-char signature.
bool FindTag(const std::vector<uint8_t>& data, const char sig[4], uint32_t& off, uint32_t& size) {
    if (data.size() < 132) return false;
    const uint32_t want = ((uint32_t)(uint8_t)sig[0] << 24) | ((uint32_t)(uint8_t)sig[1] << 16) |
                          ((uint32_t)(uint8_t)sig[2] << 8) | (uint32_t)(uint8_t)sig[3];
    uint32_t tagCount = ReadBE32(data.data() + 128);
    for (uint32_t i = 0; i < tagCount; i++) {
        const uint8_t* e = data.data() + 132 + i * 12;
        if (e + 12 > data.data() + data.size()) return false;
        if (ReadBE32(e) == want) { off = ReadBE32(e + 4); size = ReadBE32(e + 8); return true; }
    }
    return false;
}

// The serialized s15Fixed16 words of one channel's 1D LUT in a generated profile's MHC2 tag.
std::vector<uint32_t> Mhc2LutWords(const std::vector<uint8_t>& profile, int ch) {
    uint32_t off = 0, size = 0;
    REQUIRE(FindTag(profile, "MHC2", off, size));
    const uint8_t* t = profile.data() + off;
    const uint32_t lutSize = ReadBE32(t + 8);
    const uint8_t* lut = t + ReadBE32(t + 24 + ch * 4) + 8;   // skip 'sf32' + reserved
    std::vector<uint32_t> words(lutSize);
    for (uint32_t j = 0; j < lutSize; j++) words[j] = ReadBE32(lut + j * 4);
    return words;
}

// HDR params with a per-channel base grayscale (each channel's base LUT differs) and desktop gamma on.
MHC2ProfileParams PerChannelHdrParams() {
    MHC2ProfileParams p;
    p.monitorName = L"DgTest";
    p.isHDR = true;
    p.peakNits = 1450.0f;
    p.desktopGammaEnabled = true;
    p.grayscaleEnabled = true;
    p.grayscale.enabled = true;
    p.grayscale.pointCount = 20;
    p.grayscale.peakNits = 1450.0f;
    p.grayscale.initLinearPQ();
    for (int i = 0; i < 20; i++) {
        float b = p.grayscale.points[i];
        p.grayscale.pointsR[i] = (std::min)(1.0f, b * 1.02f);
        p.grayscale.pointsG[i] = b;
        p.grayscale.pointsB[i] = b * 0.97f;
    }
    return p;
}

// The serialized LUT words GenerateMHC2Profile must emit for PerChannelHdrParams(): base -> dg -> clamp.
template <typename Dg>
std::vector<uint32_t> ExpectedWords(const MHC2ProfileParams& p, int ch, Dg dg) {
    const int n = 4096;
    std::vector<float> base(n);
    GenerateMHC2LUT_HDR_Channel(p.grayscale, p.peakNits, base.data(), n, ch);
    std::vector<uint32_t> words(n);
    for (int j = 0; j < n; j++) words[j] = (uint32_t)FloatToS15Fixed16(std::clamp(dg(base[j]), 0.0f, 1.0f));
    return words;
}

} // namespace

// =============================================================================================
// 1. The MHC bake
// =============================================================================================

TEST_CASE("Desktop gamma: W = 80 is the fixed-80-nit bake bit for bit (every PQ code and a dense sweep)") {
    for (int j = 0; j < 4096; j++) {
        float pq = (float)j / 4095.0f;
        CAPTURE(j);
        CHECK(SameBits(DesktopGammaPQ(pq, 80.0f), PreChangeDesktopGammaPQ(pq)));
    }
    int mismatches = 0;
    for (int k = 0; k <= 1 << 18; k++) {
        float pq = (float)k / (float)(1 << 18);
        if (!SameBits(DesktopGammaPQ(pq, 80.0f), PreChangeDesktopGammaPQ(pq))) mismatches++;
    }
    CHECK(mismatches == 0);
}

TEST_CASE("Desktop gamma: W = 80 profiles serialize the pre-change per-channel LUTs exactly") {
    MHC2ProfileParams p = PerChannelHdrParams();   // sdrWhiteNits left at its default
    CHECK(p.sdrWhiteNits == 80.0f);
    std::vector<uint8_t> def, explicit80;
    REQUIRE(GenerateMHC2Profile(p, def));
    p.sdrWhiteNits = 80.0f;
    REQUIRE(GenerateMHC2Profile(p, explicit80));
    for (int ch = 0; ch < 3; ch++) {
        CAPTURE(ch);
        auto want = ExpectedWords(p, ch, PreChangeDesktopGammaPQ);
        CHECK(Mhc2LutWords(def, ch) == want);
        CHECK(Mhc2LutWords(explicit80, ch) == want);
    }
    // An unusable white keeps the 80-nit reference instead of baking garbage.
    for (float bad : { 0.0f, -116.0f, NAN, INFINITY }) {
        MHC2ProfileParams q = PerChannelHdrParams();
        q.sdrWhiteNits = bad;
        std::vector<uint8_t> data;
        REQUIRE(GenerateMHC2Profile(q, data));
        for (int ch = 0; ch < 3; ch++) CHECK(Mhc2LutWords(data, ch) == Mhc2LutWords(def, ch));
    }
}

TEST_CASE("Desktop gamma: W = 116 is pure 2.2 relative to the SDR white (code 20 -> 116 * (20/255)^2.2)") {
    const double W = 116.0;
    double worst = 0.0;
    for (int c = 1; c <= 254; c++) {
        double got = ComposedThroughDg(c, W, 116.0f);
        double want = W * std::pow(c / 255.0, 2.2);
        worst = (std::max)(worst, std::fabs(got / want - 1.0));
    }
    INFO("worst relative deviation from W * code^2.2 over codes 1..254: " << worst);
    CHECK(worst < 2e-4);   // float PQ round trip

    const double g22 = W * std::pow(20.0 / 255.0, 2.2);
    CHECK(ComposedThroughDg(20, W, 116.0f) == doctest::Approx(g22).epsilon(1e-4));
    // The measured defect this removes: the 80-nit bake on a 116-nit white leaves code 20 +19.7 % over 2.2
    // (forecast +19.7 %, measured +19.6 % on the PA32UCXR, 2026-10-03).
    CHECK(ComposedThroughDg(20, W, 80.0f) / g22 == doctest::Approx(1.197).epsilon(0.002));
}

TEST_CASE("Desktop gamma: black and everything above the SDR white pass through untouched; continuous at W") {
    for (float W : { 80.0f, 116.0f, 203.0f, 480.0f }) {
        CAPTURE(W);
        CHECK(SameBits(DesktopGammaPQ(0.0f, W), 0.0f));
        for (int j = 0; j < 4096; j++) {
            float pq = (float)j / 4095.0f;
            if (PqEOTF(pq) * 10000.0f > W) CHECK(SameBits(DesktopGammaPQ(pq, W), pq));
        }
        float pqW = PqOETF(W / 10000.0f);
        CHECK(DesktopGammaPQ(pqW, W) == doctest::Approx(pqW).epsilon(1e-5));
    }
    // The kink moves with W: between 80 and 116 nits the 80-nit bake leaves the signal alone, a 116-nit one doesn't.
    float pq100 = PqOETF(100.0f / 10000.0f);
    CHECK(SameBits(DesktopGammaPQ(pq100, 80.0f), pq100));
    CHECK_FALSE(SameBits(DesktopGammaPQ(pq100, 116.0f), pq100));
}

TEST_CASE("Desktop gamma: W = 116 profiles apply DG per channel to each channel's base LUT output") {
    MHC2ProfileParams p = PerChannelHdrParams();
    p.sdrWhiteNits = 116.0f;
    std::vector<uint8_t> at116, at80;
    REQUIRE(GenerateMHC2Profile(p, at116));
    MHC2ProfileParams p80 = PerChannelHdrParams();
    REQUIRE(GenerateMHC2Profile(p80, at80));
    for (int ch = 0; ch < 3; ch++) {
        CAPTURE(ch);
        auto want = ExpectedWords(p, ch, [](float v) { return DesktopGammaPQ(v, 116.0f); });
        CHECK(Mhc2LutWords(at116, ch) == want);
        CHECK(Mhc2LutWords(at116, ch) != Mhc2LutWords(at80, ch));
    }
}

// =============================================================================================
// 2. Wiring: settings -> params, the re-bake bookkeeping, persistence
// =============================================================================================

TEST_CASE("Desktop gamma: BuildMHC2Params bakes hdrMHC.dgSdrWhiteNits (HDR, DG on only)") {
    MHCSettings m;
    m.desktopGammaEnabled = true;
    m.dgSdrWhiteNits = 116.0f;
    MHC2ProfileParams p;
    BuildMHC2Params(m, /*isHDR=*/true, 0, p);
    CHECK(p.desktopGammaEnabled);
    CHECK(p.sdrWhiteNits == 116.0f);

    m.dgSdrWhiteNits = NAN;   // never reaches the bake
    MHC2ProfileParams bad;
    BuildMHC2Params(m, true, 0, bad);
    CHECK(bad.sdrWhiteNits == 80.0f);

    MHC2ProfileParams sdr;   // DG is HDR-only
    m.dgSdrWhiteNits = 116.0f;
    BuildMHC2Params(m, /*isHDR=*/false, 0, sdr);
    CHECK_FALSE(sdr.desktopGammaEnabled);
}

TEST_CASE("Desktop gamma: RebakeHdrMhcForSdrWhite records the level and drops only stale cached DG variants") {
    auto savedLive = g_gui.monitorSettings;
    g_gui.monitorSettings.assign(1, MonitorSettings{});
    {
        // Nothing installed for the active permutation's DG (WB-only active), cached variants of every kind.
        // Names that cannot exist: the bookkeeping deletes the files of the variants it drops.
        MHCSettings& m = g_gui.monitorSettings[0].hdrMHC;
        m.enabled = true;
        m.desktopGammaEnabled = true;
        m.profileName = L"DesktopLUT_UnitTest_NoSuchProfile_P1.icm";
        m.activePerm = MHCSettings::PERM_WB;
        for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
            m.permNames[k] = L"DesktopLUT_UnitTest_NoSuchProfile_P" + std::to_wstring(k) + L".icm";
            m.permPaths[k] = L"C:\\nonexistent\\" + m.permNames[k];
        }
    }
    CHECK(RebakeHdrMhcForSdrWhite(0, 116.0f));
    {
        const MHCSettings& m = g_gui.monitorSettings[0].hdrMHC;
        CHECK(m.dgSdrWhiteNits == 116.0f);
        for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
            CAPTURE(k);
            bool stale = (k & MHCSettings::PERM_DG) != 0;
            CHECK(m.permNames[k].empty() == stale);   // DG variants dropped; WB/GS-only and the active kept
            CHECK(m.permPaths[k].empty() == stale);
        }
        CHECK(m.profileName == L"DesktopLUT_UnitTest_NoSuchProfile_P1.icm");
        CHECK(m.activePerm == MHCSettings::PERM_WB);
    }
    // Same level (Windows reports 0.08-nit steps; the INI round trip may perturb the last float bit): no-op.
    CHECK_FALSE(RebakeHdrMhcForSdrWhite(0, 116.0f + 0.004f));
    CHECK_FALSE(RebakeHdrMhcForSdrWhite(0, NAN));
    CHECK_FALSE(RebakeHdrMhcForSdrWhite(0, 0.0f));
    CHECK_FALSE(RebakeHdrMhcForSdrWhite(5, 116.0f));   // no such monitor
    CHECK(g_gui.monitorSettings[0].hdrMHC.dgSdrWhiteNits == 116.0f);

    // A disabled (named, identity-swapped) profile is never reinstalled; only the level is recorded.
    g_gui.monitorSettings[0].hdrMHC.enabled = false;
    g_gui.monitorSettings[0].hdrMHC.activePerm = MHCSettings::PERM_DG;
    CHECK(RebakeHdrMhcForSdrWhite(0, 203.0f));
    CHECK(g_gui.monitorSettings[0].hdrMHC.dgSdrWhiteNits == 203.0f);
    CHECK(g_gui.monitorSettings[0].hdrMHC.profileName == L"DesktopLUT_UnitTest_NoSuchProfile_P1.icm");
    g_gui.monitorSettings = savedLive;

    CHECK(SameSdrWhiteNits(116.0f, 116.0f));
    CHECK(SameSdrWhiteNits(116.0f, 116.009f));
    CHECK_FALSE(SameSdrWhiteNits(116.0f, 116.08f));   // one Windows step apart
    CHECK_FALSE(SameSdrWhiteNits(80.0f, 116.0f));
}

TEST_CASE("Desktop gamma: the baked SDR white round-trips through the INI; absent = a pre-tracking 80-nit bake") {
    wchar_t tmp[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    std::wstring ini = std::wstring(tmp) + L"desktoplut_test_dgwhite.ini";
    _wremove(ini.c_str());

    MHCSettings original;
    original.desktopGammaEnabled = true;
    original.dgSdrWhiteNits = SdrWhiteLevelToNits(1453);   // 116.24 — not a whole number of nits
    original.baseGrayscale.pointCount = 20;
    original.baseGrayscale.initLinearPQ();
    SaveMHCSettings(L"TestMon", L"HDR_", original, ini.c_str());
    MHCSettings loaded;
    LoadMHCSettings(L"TestMon", L"HDR_", loaded, ini.c_str());
    CHECK(loaded.desktopGammaEnabled);
    CHECK(SameSdrWhiteNits(loaded.dgSdrWhiteNits, original.dgSdrWhiteNits));
    CHECK(loaded.dgSdrWhiteNits == original.dgSdrWhiteNits);

    // A profile saved before the level was tracked was baked at the fixed 80 nits.
    WritePrivateProfileStringW(L"TestMon", L"HDR_MHCDgSdrWhiteNits", nullptr, ini.c_str());
    MHCSettings legacy;
    legacy.dgSdrWhiteNits = 999.0f;
    LoadMHCSettings(L"TestMon", L"HDR_", legacy, ini.c_str());
    CHECK(legacy.dgSdrWhiteNits == 80.0f);

    const wchar_t* garbage[] = { L"abc", L"0", L"-116", L"1e9", L"nan" };
    for (int gi = 0; gi < 5; gi++) {
        CAPTURE(gi);
        WritePrivateProfileStringW(L"TestMon", L"HDR_MHCDgSdrWhiteNits", garbage[gi], ini.c_str());
        MHCSettings g;
        LoadMHCSettings(L"TestMon", L"HDR_", g, ini.c_str());
        CHECK(g.dgSdrWhiteNits == 80.0f);
    }
    _wremove(ini.c_str());
}

// =============================================================================================
// 3. DisplayConfig SDR white level
// =============================================================================================

TEST_CASE("Desktop gamma: DisplayConfig SDRWhiteLevel -> nits (level / 1000 * 80)") {
    CHECK(SdrWhiteLevelToNits(1000) == 80.0f);
    CHECK(SdrWhiteLevelToNits(1450) == 116.0f);    // the PA32UCXR, 2026-10-03
    CHECK(SdrWhiteLevelToNits(6000) == 480.0f);    // top of Windows' slider
    CHECK(SdrWhiteLevelToNits(1453) == doctest::Approx(116.24f).epsilon(1e-6));
    CHECK(IsValidSdrWhiteNits(80.0f));
    CHECK(IsValidSdrWhiteNits(480.0f));
    CHECK_FALSE(IsValidSdrWhiteNits(0.0f));
    CHECK_FALSE(IsValidSdrWhiteNits(-80.0f));
    CHECK_FALSE(IsValidSdrWhiteNits(NAN));
    CHECK_FALSE(IsValidSdrWhiteNits(INFINITY));
    CHECK_FALSE(IsValidSdrWhiteNits(20000.0f));
    // Read-only smoke on this machine's primary display (skips when the query is unavailable).
    float nits = 0.0f;
    if (QuerySdrWhiteNitsForHMonitor(MonitorFromPoint(POINT{ 0, 0 }, MONITOR_DEFAULTTOPRIMARY), nits))
        CHECK(IsValidSdrWhiteNits(nits));
}

// =============================================================================================
// 4. The overlay HLSL on WARP
// =============================================================================================

namespace {

// The shared function vs src/shader.h's desktop gamma before 2026-10-03 (verbatim), selected by DG_OLD.
const char* const kDgTestCS =
    "RWStructuredBuffer<float4> io : register(u0);\n"
    "Texture2D<float> desktopGammaLUT : register(t0);\n"
    "SamplerState linearSampler : register(s0);\n"
    "cbuffer P : register(b0) { float white; float whiteRcp; float2 pad; };\n"
    DLUT_DESKTOP_GAMMA_HLSL
    R"(
float3 PreChangeDesktopGamma(float3 input) {
    float3 absInput = abs(input);
    float3 signInput = sign(input);
    float3 sdrPart = min(absInput, 1.0);
    float3 hdrPart = max(absInput - 1.0, 0.0);
    // UV mapping: texel centers at (i+0.5)/1024, map [0,1] linear -> texel space
    float dgScale = 1023.0f / 1024.0f;
    float dgBias = 0.5f / 1024.0f;
    float3 corrected = float3(
        desktopGammaLUT.SampleLevel(linearSampler, float2(sdrPart.r * dgScale + dgBias, 0.5), 0),
        desktopGammaLUT.SampleLevel(linearSampler, float2(sdrPart.g * dgScale + dgBias, 0.5), 0),
        desktopGammaLUT.SampleLevel(linearSampler, float2(sdrPart.b * dgScale + dgBias, 0.5), 0));
    return (corrected + hdrPart) * signInput;
}

[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    float3 v = io[id.x].xyz;
#if DG_OLD
    io[id.x] = float4(PreChangeDesktopGamma(v), 0.0);
#else
    io[id.x] = float4(DlutDesktopGamma(v, white, whiteRcp, desktopGammaLUT, linearSampler), 0.0);
#endif
}
)";

struct DgGpu {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    ID3D11ComputeShader* csNew = nullptr;
    ID3D11ComputeShader* csOld = nullptr;
    ID3D11ShaderResourceView* lutSrv = nullptr;
    ID3D11SamplerState* sampler = nullptr;
    ID3D11Buffer* cb = nullptr;
    std::string error;
    bool deviceUnavailable = false;

    ~DgGpu() {
        for (IUnknown* p : std::initializer_list<IUnknown*>{ csNew, csOld, lutSrv, sampler, cb, dc, device })
            if (p) p->Release();
    }

    bool Compile(bool old, ID3D11ComputeShader** out) {
        D3D_SHADER_MACRO defs[] = { { "DG_OLD", old ? "1" : "0" }, { nullptr, nullptr } };
        ID3DBlob* blob = nullptr;
        ID3DBlob* err = nullptr;
        HRESULT hr = D3DCompile(kDgTestCS, strlen(kDgTestCS), "DesktopGammaCS", defs, nullptr, "main", "cs_5_0",
                                0, 0, &blob, &err);   // production compile flags (0)
        if (FAILED(hr)) {
            error = std::string("compile failed: ") + (err ? (const char*)err->GetBufferPointer() : "?");
            if (err) err->Release();
            return false;
        }
        if (err) err->Release();
        hr = device->CreateComputeShader(blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, out);
        blob->Release();
        if (FAILED(hr)) { error = "CreateComputeShader failed"; return false; }
        return true;
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
        if (!Compile(false, &csNew) || !Compile(true, &csOld)) return false;

        // The production table and sampler (gpu.cpp: 1024 x R32_FLOAT, MIN_MAG_MIP_LINEAR, clamp).
        std::vector<float> lut(DLUT_DESKTOP_GAMMA_LUT_SIZE);
        BuildDesktopGammaLut(lut.data(), DLUT_DESKTOP_GAMMA_LUT_SIZE);
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = DLUT_DESKTOP_GAMMA_LUT_SIZE;
        td.Height = 1;
        td.MipLevels = 1;
        td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R32_FLOAT;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_IMMUTABLE;
        td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = { lut.data(), (UINT)(lut.size() * sizeof(float)), 0 };
        ID3D11Texture2D* tex = nullptr;
        if (FAILED(device->CreateTexture2D(&td, &init, &tex))) { error = "CreateTexture2D failed"; return false; }
        hr = device->CreateShaderResourceView(tex, nullptr, &lutSrv);
        tex->Release();
        if (FAILED(hr)) { error = "CreateShaderResourceView failed"; return false; }
        D3D11_SAMPLER_DESC sd = {};
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        if (FAILED(device->CreateSamplerState(&sd, &sampler))) { error = "CreateSamplerState failed"; return false; }
        D3D11_BUFFER_DESC cbd = {};
        cbd.ByteWidth = 16;
        cbd.Usage = D3D11_USAGE_DYNAMIC;
        cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        if (FAILED(device->CreateBuffer(&cbd, nullptr, &cb))) { error = "CreateBuffer (cb) failed"; return false; }
        return true;
    }

    // Run one shader over scRGB triples; white / whiteRcp as render.cpp computes them from the SDR white (nits).
    std::vector<float> Run(bool old, float sdrWhiteNits, std::vector<float> rgb) {
        const float white = sdrWhiteNits / 80.0f;
        const float params[4] = { white, 1.0f / white, 0.0f, 0.0f };
        D3D11_MAPPED_SUBRESOURCE mcb = {};
        REQUIRE(SUCCEEDED(dc->Map(cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mcb)));
        std::memcpy(mcb.pData, params, sizeof params);
        dc->Unmap(cb, 0);

        size_t n = rgb.size() / 3;
        size_t padded = (n + 63) / 64 * 64;
        std::vector<float> packed(padded * 4, 0.0f);
        for (size_t i = 0; i < n; i++)
            for (int c = 0; c < 3; c++) packed[i * 4 + c] = rgb[i * 3 + c];
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

        dc->CSSetShader(old ? csOld : csNew, nullptr, 0);
        dc->CSSetConstantBuffers(0, 1, &cb);
        dc->CSSetShaderResources(0, 1, &lutSrv);
        dc->CSSetSamplers(0, 1, &sampler);
        dc->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
        dc->Dispatch((UINT)(padded / 64), 1, 1);
        ID3D11UnorderedAccessView* nullUav = nullptr;
        dc->CSSetUnorderedAccessViews(0, 1, &nullUav, nullptr);
        dc->CSSetShader(nullptr, nullptr, 0);
        dc->CopyResource(staging, buf);

        std::vector<float> out(n * 3);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));
        const float* p = (const float*)m.pData;
        for (size_t i = 0; i < n; i++)
            for (int c = 0; c < 3; c++) out[i * 3 + c] = p[i * 4 + c];
        dc->Unmap(staging, 0);
        staging->Release();
        uav->Release();
        buf->Release();
        return out;
    }
};

#define DG_GPU_OR_SKIP(gpu)                                                       \
    DgGpu gpu;                                                                     \
    if (!gpu.Init()) {                                                             \
        if (gpu.deviceUnavailable) { MESSAGE("skipped: " << gpu.error); return; }  \
        FAIL(gpu.error);                                                           \
    }

} // namespace

TEST_CASE("Desktop gamma: the shader table is the pre-change gpu.cpp table bit for bit") {
    std::vector<float> lut(DLUT_DESKTOP_GAMMA_LUT_SIZE);
    BuildDesktopGammaLut(lut.data(), DLUT_DESKTOP_GAMMA_LUT_SIZE);
    for (int i = 0; i < DLUT_DESKTOP_GAMMA_LUT_SIZE; i++) {
        float L = static_cast<float>(i) / static_cast<float>(DLUT_DESKTOP_GAMMA_LUT_SIZE - 1);
        float srgb = (L <= 0.0031308f) ? 12.92f * L : 1.055f * powf(L, 1.0f / 2.4f) - 0.055f;
        CHECK(SameBits(lut[i], powf((std::max)(srgb, 0.0f), 2.2f)));
    }
}

TEST_CASE("Desktop gamma HLSL on WARP: an 80-nit SDR white is the pre-change shader bit for bit") {
    DG_GPU_OR_SKIP(gpu);
    // scRGB values across the whole range, per-channel distinct, both signs (wide gamut), HDR highlights.
    std::vector<float> rgb;
    for (int i = 0; i <= 4000; i++) {
        float t = i / 4000.0f;
        rgb.push_back(t * t);                          // dense near black
        rgb.push_back(-t * 1.3f);                      // negative (out of sRGB gamut)
        rgb.push_back(t * 12.5f);                      // up to 1000 nits
    }
    for (float v : { 0.0f, -0.0f, 1.0f, -1.0f, 1.0000001f, 0.9999999f, 125.0f, 1e-8f })
        rgb.insert(rgb.end(), { v, v, v });
    auto now = gpu.Run(false, 80.0f, rgb);
    auto before = gpu.Run(true, 80.0f, rgb);
    REQUIRE(now.size() == before.size());
    int mismatches = 0;
    for (size_t i = 0; i < now.size(); i++)
        if (!SameBits(now[i], before[i])) mismatches++;
    CHECK(mismatches == 0);
}

TEST_CASE("Desktop gamma HLSL on WARP: a 116-nit SDR white decodes Windows' sRGB composite as pure 2.2") {
    DG_GPU_OR_SKIP(gpu);
    const float W = 116.0f, w = W / 80.0f;
    // (a) The same table referenced to W: DG_W(w * t) = w * DG_80(t), and above w the signal passes through.
    std::vector<float> rgb, scaled;
    for (int i = 0; i <= 2000; i++) {
        float t = i / 2000.0f;
        rgb.insert(rgb.end(), { t, t * 0.5f, t * t });
        scaled.insert(scaled.end(), { w * t, w * t * 0.5f, w * t * t });
    }
    auto ref = gpu.Run(true, 80.0f, rgb);
    auto at116 = gpu.Run(false, W, scaled);
    double worst = 0.0;
    for (size_t i = 0; i < ref.size(); i++) worst = (std::max)(worst, (double)std::fabs(at116[i] - w * ref[i]));
    INFO("worst |DG_116(w t) - w DG_80(t)| (scRGB): " << worst);
    CHECK(worst < 2e-5);
    auto above = gpu.Run(false, W, { w + 0.5f, w + 10.0f, -(w + 2.0f) });
    CHECK(above[0] == doctest::Approx(w + 0.5f).epsilon(1e-5));
    CHECK(above[1] == doctest::Approx(w + 10.0f).epsilon(1e-5));
    CHECK(above[2] == doctest::Approx(-(w + 2.0f)).epsilon(1e-5));

    // (b) Windows' composite of 8-bit SDR codes at W, through the shader: W * code^2.2 (relative to W). Codes below
    // 32 are left out: the linear-light table is coarse there, the same at any W (a property of the table).
    std::vector<float> codes;
    for (int c = 32; c <= 254; c++) {
        float x = (float)(w * SrgbEotfD(c / 255.0));
        codes.insert(codes.end(), { x, x, x });
    }
    auto out = gpu.Run(false, W, codes);
    double worstRel = 0.0;
    for (int c = 32; c <= 254; c++) {
        double want = w * std::pow(c / 255.0, 2.2);
        worstRel = (std::max)(worstRel, std::fabs(out[(c - 32) * 3] / want - 1.0));
    }
    INFO("worst relative deviation from W * code^2.2 over codes 32..254: " << worstRel);
    CHECK(worstRel < 1e-3);
}

TEST_CASE("Desktop gamma: the overlay pixel shader splices the shared function, compiles, and lays out the cbuffer") {
    const std::string overlay = g_psSource;
    CHECK(overlay.find(DLUT_DESKTOP_GAMMA_HLSL) != std::string::npos);
    size_t first = overlay.find("float3 DlutDesktopGamma(");
    CHECK(first != std::string::npos);
    CHECK(overlay.find("float3 DlutDesktopGamma(", first + 1) == std::string::npos);

    ID3DBlob* blob = nullptr;
    ID3DBlob* msgs = nullptr;
    HRESULT hr = D3DCompile(g_psSource, strlen(g_psSource), nullptr, nullptr, nullptr, "main", "ps_5_0", 0, 0,
                            &blob, &msgs);   // as gpu.cpp compiles it
    std::string output = msgs ? (const char*)msgs->GetBufferPointer() : "";
    if (msgs) msgs->Release();
    INFO("compiler output: " << output);
    REQUIRE(SUCCEEDED(hr));

    // render.cpp writes cbData[1] = SDR white (nits), cbData[144] = white / 80, cbData[145] = its reciprocal,
    // into a 592-byte buffer (gpu.cpp).
    ID3D11ShaderReflection* refl = nullptr;
    REQUIRE(SUCCEEDED(D3DReflect(blob->GetBufferPointer(), blob->GetBufferSize(),
                                 __uuidof(ID3D11ShaderReflection), (void**)&refl)));
    ID3D11ShaderReflectionConstantBuffer* cbuf = refl->GetConstantBufferByName("LUTParams");
    D3D11_SHADER_BUFFER_DESC cbDesc = {};
    REQUIRE(SUCCEEDED(cbuf->GetDesc(&cbDesc)));
    CHECK(cbDesc.Size <= 592u);
    auto offsetOf = [&](const char* name) -> int {
        D3D11_SHADER_VARIABLE_DESC vd = {};
        if (FAILED(cbuf->GetVariableByName(name)->GetDesc(&vd))) return -1;
        return (int)vd.StartOffset;
    };
    CHECK(offsetOf("sdrWhiteNits") == 1 * 4);
    CHECK(offsetOf("desktopGamma") == 4 * 4);
    CHECK(offsetOf("corrPreviewMatRow2") == 140 * 4);
    CHECK(offsetOf("sdrWhiteScRGB") == 144 * 4);
    CHECK(offsetOf("sdrWhiteScRGBRcp") == 145 * 4);
    refl->Release();
    blob->Release();
}
