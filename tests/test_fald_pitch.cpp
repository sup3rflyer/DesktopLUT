// FALD fractional zone pitch (docs/fald-fractional-pitch.md). A lattice is cols x rows zones over an integer pixel extent
// latW x latH: a RATIONAL pitch, so the most common mini-LED monitor (27" 2560 x 1440, 1152 zones, 48 x 24 = 53.33 x 60 px)
// is profiled at its native resolution. Covered here:
//   - the panel file's lattice-extent word (31): the loader takes it for the market's grids, an integer lattice written
//     either way loads to the same params, malformed lattice words are refused;
//   - the CPU reference of the shaders' zone mapping (FaldZoneEdge / FaldZoneOfPixel): an exact partition by pixel centre;
//   - on the WARP software device (a CPU rasterizer — no GPU):
//       * integer lattices render BIT FOR BIT as before: the overlay's FaldRunPasses with the frozen pre-change HLSL
//         (tests/fald_shader_legacy.h) and with the current HLSL, every field and the output frame compared — synthetic
//         panel files (both transfers, one- and two-slice zones, a lattice origin, starfield, the zone debug views) and
//         the owner's real PA32UCXR files when they are on this machine (FALD_TEST_PANEL_FILES or beside the checkout);
//       * fractional lattices: the device's zone sweeps (statistic + starfield S0) and its per-pixel zone lookup (debug
//         view 8) are the CPU reference's, pixel for pixel, on 48 x 24 and 36 x 32 at 2560 x 1440 and on odd lattices.
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "fald_shader_legacy.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3dcompiler.h>
#include <DirectXPackedVector.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace {

struct PitchTempFile {
    std::wstring path;
    explicit PitchTempFile(std::wstring p) : path(std::move(p)) {}
    ~PitchTempFile() { _wremove(path.c_str()); }
};

void WritePanel(const std::wstring& path, const std::vector<char>& b) {
    std::ofstream f(path, std::ios::binary);
    f.write(b.data(), (std::streamsize)b.size());
}

uint32_t ExtentWord(uint32_t w, uint32_t h) { return w | (h << 16); }

// A complete FLD4 panel file. Words 4/5 = cellW / cellH (0 / 0 with an extent), word 31 = the lattice extent (0 = none).
// The tables are plausible, not fitted: a monotone drive curve over ln(nits), separable-ish exponential kernels in CELL
// units per sub-offset (the reference's index convention, kern index = sample cell - source cell), normalised so a full
// field gives B = 1 on average — enough for every pass to do something non-trivial.
struct PanelSpec {
    uint32_t cols = 8, rows = 6, sub = 4;
    uint32_t cellW = 80, cellH = 45;
    uint32_t extentW = 0, extentH = 0;
    uint32_t originX = 0, originY = 0;
    bool gamma = false;      // transfer 1 (SDR under ACM) instead of PQ
    bool boost = true;       // a black-frame boost LUT with the LIT-or-MEAN zone rule (debug view 8 needs one)
    std::vector<std::pair<float, float>> lut = { { 0.0f, 1.17f }, { 0.30f, 1.10f }, { 0.60f, 1.0f } };   // (zone fraction, step)
};

std::vector<char> MakePanel(const PanelSpec& s) {
    const uint32_t rT = 3, rE = 2, curveN = 256;
    std::vector<uint32_t> h(104, 0);
    auto U = [&](size_t i, uint32_t v) { h[i] = v; };
    auto F = [&](size_t i, float v) { std::memcpy(&h[i], &v, 4); };
    U(0, 0x464C4434u);                                                       // 'FLD4'
    U(1, s.cols); U(2, s.rows); U(3, s.sub); U(4, s.cellW); U(5, s.cellH); U(6, s.originX); U(7, s.originY);
    U(8, rT); U(9, rT); U(10, rE); U(11, rE); U(12, curveN);
    const float white = s.gamma ? 400.0f : 1000.0f;
    const float lnMin = std::log(1e-2f), lnMax = std::log(10000.0f);
    F(13, white); F(14, 1.0e-3f); F(15, 1150.0f);
    F(16, 0.2627f); F(17, 0.678f); F(18, 0.0593f);
    F(19, 0.25f); F(20, 4.0f); F(21, 0.5f);
    F(22, lnMin); F(23, lnMax); F(24, -10.0f); F(25, -12.0f);
    F(26, 0.004f); F(27, 0.03f); F(28, 0.7f); F(29, 0.5f); F(30, 5.0f);
    U(31, ExtentWord(s.extentW, s.extentH));
    U(40, s.gamma ? 1u : 0u); F(41, s.gamma ? 2.2f : 0.0f);
    if (s.boost) {
        U(48, (uint32_t)s.lut.size()); F(49, 0.35f); F(50, 0.0f); F(51, 0.011f); F(52, 0.19f);
        U(53, 1); F(54, 0.62f); F(55, 0.0693f);
        for (size_t i = 0; i < s.lut.size(); i++) { F(56 + 2 * i, s.lut[i].first); F(57 + 2 * i, s.lut[i].second); }
    }
    std::vector<float> t;
    for (uint32_t i = 0; i < curveN; i++) {
        const float ln = lnMin + (lnMax - lnMin) * (float)i / (float)(curveN - 1);
        float d = (ln - std::log(0.3f)) / (std::log(white) - std::log(0.3f));
        d = d < 0.0f ? 0.0f : (d > 1.0f ? 1.0f : d);
        t.push_back(std::pow(d, 1.3f));
    }
    auto kernels = [&](uint32_t reach, double scale, double phx, double phy) {
        std::vector<double> k;
        double sum = 0.0;
        const uint32_t W = 2 * reach + 1;
        for (uint32_t oy = 0; oy < s.sub; oy++)
            for (uint32_t ox = 0; ox < s.sub; ox++)
                for (uint32_t j = 0; j < W; j++)
                    for (uint32_t i = 0; i < W; i++) {
                        const double dx = ((int)i - (int)reach) + (ox + 0.5) / s.sub - 0.5 + phx;
                        const double dy = ((int)j - (int)reach) + (oy + 0.5) / s.sub - 0.5 + phy;
                        const double v = std::exp(-std::sqrt(dx * dx + 1.4 * dy * dy) / scale);
                        k.push_back(v); sum += v;
                    }
        const double norm = sum / (double)(s.sub * s.sub);
        for (double v : k) t.push_back((float)(v / norm));
    };
    kernels(rT, 0.9, 0.0, 0.0);
    kernels(rE, 0.6, -0.2, 0.1);
    std::vector<char> b(h.size() * 4 + t.size() * 4);
    std::memcpy(b.data(), h.data(), h.size() * 4);
    std::memcpy(b.data() + h.size() * 4, t.data(), t.size() * 4);
    return b;
}

uint16_t Half(float v) { return DirectX::PackedVector::XMConvertFloatToHalf(v); }
float FromHalf(uint16_t v) { return DirectX::PackedVector::XMConvertHalfToFloat(v); }

void PutPixel(std::vector<uint16_t>& f, int w, int x, int y, float r, float g, float b) {
    const size_t o = ((size_t)y * w + x) * 4;
    f[o] = Half(r); f[o + 1] = Half(g); f[o + 2] = Half(b); f[o + 3] = Half(1.0f);
}

// A deterministic test frame (FP16 scRGB): a dim gradient, bright windows straddling zone borders, small specks (the
// starfield's star-like zones), a coloured patch, a deep-black corner. `level` = scRGB of the windows (PQ: x 80 nits).
std::vector<uint16_t> SyntheticFrame(int w, int h, bool gamma) {
    std::vector<uint16_t> f((size_t)w * h * 4);
    const float bright = gamma ? 0.9f : 10.0f, speck = gamma ? 0.6f : 6.0f;
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++) {
            const float g = (gamma ? 0.002f : 0.0025f) + (gamma ? 0.05f : 0.035f) * (float)(x + 2 * y) / (float)(w + 2 * h);
            PutPixel(f, w, x, y, g, g, g);
        }
    auto rect = [&](int x0, int y0, int rw, int rh, float r, float g, float b) {
        for (int y = y0; y < y0 + rh && y < h; y++)
            for (int x = x0; x < x0 + rw && x < w; x++) if (x >= 0 && y >= 0) PutPixel(f, w, x, y, r, g, b);
    };
    rect(w / 3 - 17, h / 3 - 11, w / 5, h / 6, bright, bright, bright);
    rect(2 * w / 3 - 5, h / 2 + 3, w / 8, h / 4, bright * 0.5f, bright * 0.5f, bright * 0.5f);
    rect(w / 8, 2 * h / 3, w / 10, h / 7, bright * 0.6f, bright * 0.15f, bright * 0.05f);
    rect(0, 0, w / 9, h / 9, 0.0f, 0.0f, 0.0f);
    uint32_t lcg = 12345u;
    auto next = [&]() { lcg = lcg * 1664525u + 1013904223u; return lcg >> 8; };
    for (int i = 0; i < 60; i++) {
        const int x = (int)(next() % (uint32_t)w), y = (int)(next() % (uint32_t)h), sz = 1 + (int)(next() % 3u);
        const float v = speck * (0.4f + 0.6f * (float)(next() % 1000u) / 1000.0f);
        rect(x, y, sz, sz, v, v, v);
    }
    return f;
}

// The WARP device as the process device (g_device / g_context, as the overlay's InitD3D sets them) + the fullscreen VS.
struct Warp {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11VertexShader* vs = nullptr;
    bool ok = false;
    Warp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main",
                              "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr;
    }
    ~Warp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

FaldShaderSources LegacySources() {
    namespace L = fald_legacy_hlsl;
    FaldShaderSources s;
    // The conv pass's kernel-table layout changed on 2026-10-22 (fald/core-speed: sub-offset groups, float4 rows with a
    // range header), which the frozen conv cannot read, and the frozen set has no fused gain + blur pass: both come from
    // the current header here; so does the statistic pass, which finishes the black-frame boost itself since the same day
    // (the frozen set expects the boost pass that no longer exists). Their identity with the earlier passes is held by the
    // fade-OFF manifest recorded on main (tests/fixtures/fald_fade_off_main.hashes) and the fused-vs-three-pass case in
    // test_fald_fade.cpp.
    // ... and the current statistic pass needs the current common source (the zone list, the run mode, the change flags);
    // the frozen passes below compile against it too (they use nothing it lost). What this case still holds bit for bit:
    // the frozen gain / blur (the three passes, forced by g_faldGainFusedOverride below), temporal, panel clock, starfield
    // S0 / S1 / S2 and pixel passes against today's — i.e. the 2026-10-22 rewrites of S2, the fused gain + blur and the
    // compared LED-lag passes. The pre-fractional-pitch zone math was held to the fractional one when that landed (429cffa).
    s.common = g_faldCommonSource; s.stat = g_faldStatSource; s.conv = g_faldConvSource; s.gain = L::g_faldGainSource;
    s.blur = L::g_faldBlurSource; s.gainBlur = g_faldGainBlurSource; s.temporal = L::g_faldTemporalSource; s.panelClock = L::g_faldPanelClockSource;
    s.starStat = L::g_faldStarStatSource; s.starWeight = L::g_faldStarWeightSource;
    // The starfield's lift was retired 2026-10-26: CB word 53 is now starRetired53, always 0. The frozen plan pass still
    // computes its lift from that word, so at 0 it holds today's lift-free S2 + Balance to the pre-retirement maths.
    static const std::string legacyPlan = std::string("#define starLift starRetired53\n") + L::g_faldStarPlanSource;
    s.starPlan = legacyPlan.c_str(); s.zoneCombineDefine = L::g_faldZoneCombineDefine; s.pixel = L::g_faldPixelSource;
    s.decide = g_faldDecideSource;   // (the decide pass, 2026-10-22: the frozen set has none)
    return s;
}

std::vector<uint8_t> ReadBack(ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    if (!tex) return out;
    D3D11_TEXTURE2D_DESC d;
    tex->GetDesc(&d);
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

uint64_t Fnv(const std::vector<uint8_t>& b) {
    uint64_t h = 1469598103934665603ull;
    for (uint8_t c : b) { h ^= c; h *= 1099511628211ull; }
    return h;
}

// One run of the overlay's layer: every field it leaves behind + the output frame.
struct LayerRun {
    std::vector<std::pair<std::string, std::vector<uint8_t>>> fields;   // name -> bytes (empty when the texture does not exist)
    std::vector<uint8_t> out;
    std::string error;
    const std::vector<uint8_t>* Field(const char* name) const {
        for (const auto& f : fields) if (f.first == name) return &f.second;
        return nullptr;
    }
};

bool RunLayer(Warp& w, const std::wstring& panel, bool hdr, int width, int height, const std::vector<uint16_t>& frame,
              unsigned int debugMode, bool star, LayerRun& lr) {
    MonitorContext ctx;
    ctx.width = width; ctx.height = height;
    ctx.isHDREnabled = hdr; ctx.isFP16SDR = !hdr;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    FaldSettings& fs = hdr ? ctx.hdrColorCorrection.fald : ctx.sdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = panel; fs.debugMode = debugMode; fs.star.enabled = star;
    fs.coreMode = FALD_CORE_MODE_C;   // the always-on core off: this case holds the layer to its C reference
    if (!FaldEnsureResources(&ctx, fs)) {
        lr.error = ctx.fald ? ctx.fald->lastError : std::string("no resources");
        FaldReleaseResources(&ctx);
        return false;
    }
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = (UINT)width; td.Height = (UINT)height; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    if (FAILED(w.dev->CreateTexture2D(&td, nullptr, &target)) || FAILED(w.dev->CreateRenderTargetView(target, nullptr, &rtv))) {
        lr.error = "target";
        if (target) target->Release();
        FaldReleaseResources(&ctx);
        return false;
    }
    D3D11_VIEWPORT vp = { 0, 0, (float)width, (float)height, 0, 1 };
    w.ic->RSSetViewports(1, &vp);
    w.ic->VSSetShader(w.vs, nullptr, 0);
    w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)width * 8u, 0);
    FaldRunPasses(&ctx, rtv, true);
    w.ic->Flush();
    const FaldResources* r = ctx.fald;
    const std::pair<const char*, ID3D11Texture2D*> tex[] = {
        { "drive", r->driveTex }, { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] },
        { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] }, { "btrue", r->bTrueTex }, { "best", r->bEstTex },
        { "flat_btrue", r->flatTrueTex }, { "flat_best", r->flatEstTex },
        // (the fused gain + blur pass leaves the final map in gainB alone; the three-pass path leaves it in both)
        { "gainA", r->gainFused ? r->gainBTex : r->gainATex }, { "gainB", r->gainBTex },
        { "star_stat", r->starStatTex }, { "star_w", r->starWTex }, { "star_plan", r->starPlanTex },
        { "star_plan2", r->starPlan2Tex }, { "star_bg", r->starBgTex } };
    for (const auto& t : tex) lr.fields.emplace_back(t.first, ReadBack(t.second));
    lr.out = ReadBack(target);
    rtv->Release(); target->Release();
    FaldReleaseResources(&ctx);
    return true;
}

// The owner's real panel files when on this machine (the list tests/test_fald_live.cpp RealPanelFiles reads): the
// FALD_TEST_PANEL_FILES variable (';'-separated), else the DLC results beside this checkout or a sibling "DesktopLUT".
std::vector<std::wstring> OwnerPanelFiles() {
    std::vector<std::wstring> out;
    auto add = [&](const std::wstring& p) {
        if (p.empty() || GetFileAttributesW(p.c_str()) == INVALID_FILE_ATTRIBUTES) return;
        for (const auto& q : out) if (_wcsicmp(q.c_str(), p.c_str()) == 0) return;
        out.push_back(p);
    };
    wchar_t env[4096] = {};
    const DWORD n = GetEnvironmentVariableW(L"FALD_TEST_PANEL_FILES", env, (DWORD)(sizeof(env) / sizeof(env[0])));
    if (n > 0 && n < sizeof(env) / sizeof(env[0])) {
        std::wstring s = env;
        size_t a = 0;
        while (a <= s.size()) {
            size_t b = s.find(L';', a);
            if (b == std::wstring::npos) b = s.size();
            add(s.substr(a, b - a));
            a = b + 1;
        }
    }
    const wchar_t* rel[] = {
        L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin",
        L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin",
        L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin",
    };
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir = exe;
    for (int up = 0; up < 5; up++) {
        const size_t slash = dir.find_last_of(L"\\/");
        if (slash == std::wstring::npos) break;
        dir.resize(slash);
        for (const wchar_t* r : rel) {
            add(dir + L"\\" + r);
            const size_t parent = dir.find_last_of(L"\\/");
            if (parent != std::wstring::npos) add(dir.substr(0, parent) + L"\\DesktopLUT\\" + r);
        }
    }
    return out;
}

// As-if-white nits of a pixel's brightest channel the PQ path reads (HLSL PanelNits, transfer 0) from an FP16 grey.
double GreyNitsPq(uint16_t half) {
    const double v = FromHalf(half);
    const double rows[3] = { 0.6274040 + 0.3292820 + 0.0433136, 0.0690970 + 0.9195400 + 0.0113612, 0.0163916 + 0.0880132 + 0.8955950 };
    double m = 0.0;
    for (double r : rows) m = std::max(m, std::max(v * r, 0.0));
    return m * 80.0;
}

}  // namespace

// ---------------------------------------------------------------------------------------------------------------------
// The panel file
// ---------------------------------------------------------------------------------------------------------------------
TEST_CASE("FALD fractional pitch: the loader takes a lattice extent (word 31) for the market's grids") {
    struct Grid { uint32_t cols, rows, w, h; const char* what; };
    const Grid grids[] = {
        { 48, 24, 2560, 1440, "27-inch 1440p 1152 zones, the likely grid (53.33 x 60 px)" },
        { 36, 32, 2560, 1440, "27-inch 1440p 1152 zones, the alternative (71.11 x 45 px)" },
        { 48, 24, 3440, 1440, "34-inch ultrawide 1152 zones (71.67 x 60 px)" },
        { 31, 23, 1920, 1080, "an odd TV grid (61.94 x 46.96 px)" },
        { 46, 26, 2560, 1440, "the 1196-zone 46 x 26 (55.65 x 55.38 px)" },
        { 72, 32, 3440, 1440, "34-inch ultrawide 2304 zones (47.78 x 45 px)" },
        { 36, 32, 3840, 2160, "32-inch 4K 1152 zones (106.67 x 67.5 px)" },
    };
    for (const Grid& g : grids) {
        CAPTURE(g.what);
        PanelSpec s; s.cols = g.cols; s.rows = g.rows; s.cellW = 0; s.cellH = 0; s.extentW = g.w; s.extentH = g.h;
        PitchTempFile tf(L"test_fald_pitch_grid.bin");
        WritePanel(tf.path, MakePanel(s));
        FaldPanelParams p; std::string err;
        REQUIRE_MESSAGE(LoadFaldPanelParams(tf.path, p, err), err);
        CHECK(p.latW == g.w); CHECK(p.latH == g.h);
        CHECK(p.cellW == (g.w + g.cols - 1) / g.cols); CHECK(p.cellH == (g.h + g.rows - 1) / g.rows);   // the widest zone
        CHECK(FaldLatticeFractional(p));
        CHECK(FaldLatticeFits(p, (int)g.w, (int)g.h));              // the native frame: exact, nothing uncovered
        CHECK(FaldLatticeFits(p, (int)g.w + 7, (int)g.h + 3));
        CHECK_FALSE(FaldLatticeFits(p, (int)g.w - 1, (int)g.h));
        CHECK_FALSE(FaldLatticeFits(p, (int)g.w, (int)g.h - 1));
        // the zones tile the extent, each floor or ceil of the pitch, the widest = cellW
        uint32_t sum = 0, widest = 0;
        for (uint32_t c = 0; c < p.cols; c++) {
            const uint32_t zw = FaldZoneEdge(c + 1, p.cols, p.latW) - FaldZoneEdge(c, p.cols, p.latW);
            CHECK((zw == g.w / g.cols || zw == (g.w + g.cols - 1) / g.cols));
            sum += zw; widest = zw > widest ? zw : widest;
        }
        CHECK(sum == g.w); CHECK(widest == p.cellW);
        CHECK(FaldZoneSlices(p.cellW, p.cellH) <= FALD_ZONE_SLICES_MAX);
    }
}

TEST_CASE("FALD fractional pitch: an integer lattice written as an extent loads to the whole-pixel file's params") {
    PanelSpec cells; cells.cols = 48; cells.rows = 48; cells.cellW = 80; cells.cellH = 45;          // the PA32UCXR grid
    PanelSpec ext = cells; ext.cellW = 0; ext.cellH = 0; ext.extentW = 3840; ext.extentH = 2160;
    PitchTempFile a(L"test_fald_pitch_cells.bin"), b(L"test_fald_pitch_extent.bin");
    WritePanel(a.path, MakePanel(cells));
    WritePanel(b.path, MakePanel(ext));
    FaldPanelParams pa, pb; std::string err;
    REQUIRE(LoadFaldPanelParams(a.path, pa, err));
    REQUIRE(LoadFaldPanelParams(b.path, pb, err));
    CHECK(pa.latW == 3840); CHECK(pa.latH == 2160);                  // a whole-pixel file: cols * cellW x rows * cellH
    CHECK_FALSE(FaldLatticeFractional(pa));
    CHECK(pb.cellW == 80); CHECK(pb.cellH == 45); CHECK(pb.latW == 3840); CHECK(pb.latH == 2160);
    CHECK_FALSE(FaldLatticeFractional(pb));
    CHECK(pa.cols == pb.cols); CHECK(pa.rows == pb.rows); CHECK(pa.originX == pb.originX); CHECK(pa.originY == pb.originY);
    CHECK(pa.curve == pb.curve); CHECK(pa.kTrue == pb.kTrue); CHECK(pa.kEst == pb.kEst);
    CHECK(pa.white == pb.white); CHECK(pa.boostN == pb.boostN);
}

TEST_CASE("FALD fractional pitch: malformed lattice words are refused") {
    struct Bad { uint32_t cellW, cellH, extW, extH; uint32_t cols; const char* expect; const char* what; };
    const Bad bad[] = {
        { 0, 0, 0, 0, 48, "implausible header", "no cells and no extent (as before: a zero cell word)" },
        { 0, 45, 0, 0, 48, "implausible header", "one zero cell word, no extent (as before)" },
        { 80, 45, 3840, 2160, 48, "implausible lattice words", "cells AND an extent" },
        { 0, 45, 2560, 1440, 48, "implausible lattice words", "one cell word with an extent" },
        { 0, 0, 40, 1440, 48, "implausible lattice words", "an extent narrower than the zone count (a zone of 0 px)" },
        { 0, 0, 2560, 0, 48, "implausible lattice words", "an extent of zero height (the extent word's high half)" },
        { 200, 45, 0, 0, 400, "implausible lattice words", "a whole-pixel lattice of more than 65535 px" },
    };
    for (const Bad& x : bad) {
        CAPTURE(x.what);
        PanelSpec s; s.cols = x.cols; s.rows = 24; s.cellW = x.cellW; s.cellH = x.cellH; s.extentW = x.extW; s.extentH = x.extH;
        PitchTempFile tf(L"test_fald_pitch_bad.bin");
        WritePanel(tf.path, MakePanel(s));
        FaldPanelParams p; std::string err;
        CHECK_FALSE(LoadFaldPanelParams(tf.path, p, err));
        CHECK_MESSAGE(err.find(x.expect) != std::string::npos, err);
    }
    {   // an extent larger than the frame loads, and the layer refuses it for that frame (no wrap, no crop)
        PanelSpec s; s.cols = 48; s.rows = 24; s.cellW = 0; s.cellH = 0; s.extentW = 2560; s.extentH = 1440;
        PitchTempFile tf(L"test_fald_pitch_big.bin");
        WritePanel(tf.path, MakePanel(s));
        FaldPanelParams p; std::string err;
        REQUIRE(LoadFaldPanelParams(tf.path, p, err));
        CHECK_FALSE(FaldLatticeFits(p, 1920, 1080));
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// The CPU reference of the shaders' zone mapping
// ---------------------------------------------------------------------------------------------------------------------
TEST_CASE("FALD fractional pitch: the zone mapping partitions the lattice by pixel centre (CPU reference)") {
    const uint32_t counts[] = { 1, 2, 3, 7, 16, 23, 24, 26, 31, 32, 36, 46, 48, 64, 72, 97, 512 };
    const uint32_t extents[] = { 1, 2, 7, 45, 80, 129, 160, 221, 373, 1080, 1440, 1920, 2160, 2560, 3440, 3840, 7680, 65535 };
    size_t bad = 0, checked = 0;
    for (uint32_t n : counts)
        for (uint32_t e : extents) {
            if (e < n) continue;
            const uint32_t lo = e / n, hi = (e + n - 1) / n;
            if (FaldZoneEdge(0, n, e) != 0 || FaldZoneEdge(n, n, e) != e) bad++;
            for (uint32_t c = 0; c < n; c++) {
                const uint32_t e0 = FaldZoneEdge(c, n, e), e1 = FaldZoneEdge(c + 1, n, e);
                const uint32_t zw = e1 - e0;
                if (!(zw == lo || zw == hi)) bad++;                                     // floor or ceil of the pitch
                // c * pitch rounded to the nearest pixel edge, ties to the lower one: 2 n e0 in [2 c e - n, 2 c e + n)
                const long long t = 2ll * c * e;
                if (!(2ll * n * e0 >= t - (long long)n && 2ll * n * e0 < t + (long long)n)) bad++;
                if (e % n == 0 && e0 != c * (e / n)) bad++;                              // integer pitch: c * P
            }
            // every pixel (all of them up to 4096 px, else the 3 px around every edge + a stride): its zone holds it AND
            // its centre, and an integer pitch gives local / P
            auto check = [&](uint32_t px) {
                const uint32_t z = FaldZoneOfPixel(px, n, e);
                checked++;
                if (z >= n) { bad++; return; }
                if (!(FaldZoneEdge(z, n, e) <= px && px < FaldZoneEdge(z + 1, n, e))) bad++;
                const long long cpx = (2ll * px + 1) * n;                                // (px + 1/2) * n / e in [z, z + 1)
                if (!(cpx >= 2ll * z * e && cpx < 2ll * (z + 1) * e)) bad++;
                if (e % n == 0 && z != px / (e / n)) bad++;
            };
            if (e <= 4096) {
                for (uint32_t px = 0; px < e; px++) check(px);
            } else {
                for (uint32_t c = 0; c <= n; c++) {
                    const uint32_t edge = FaldZoneEdge(c, n, e);
                    for (int d = -3; d <= 3; d++) {
                        const long long px = (long long)edge + d;
                        if (px >= 0 && px < (long long)e) check((uint32_t)px);
                    }
                }
                for (uint32_t px = 0; px < e; px += 97) check(px);
            }
        }
    CHECK(checked > 100000);
    CHECK(bad == 0);
    // ties go to the lower edge: 24 zones over 2556 px = 106.5-px pitch, edge 1 = 106 (106.5 rounded down) and pixel 106
    // (centre 106.5 = exactly on the boundary) belongs to zone 1
    CHECK(FaldZoneEdge(1, 24, 2556) == 106);
    CHECK(FaldZoneOfPixel(105, 24, 2556) == 0); CHECK(FaldZoneOfPixel(106, 24, 2556) == 1);
    // the owner's case: 48 zones over 2560 px, edges at 0, 53, 107, 160, ... (53.33-px pitch)
    CHECK(FaldZoneEdge(1, 48, 2560) == 53); CHECK(FaldZoneEdge(2, 48, 2560) == 107); CHECK(FaldZoneEdge(3, 48, 2560) == 160);
}

// ---------------------------------------------------------------------------------------------------------------------
// WARP: integer lattices bit for bit as before
// ---------------------------------------------------------------------------------------------------------------------
TEST_CASE("FALD fractional pitch: integer lattices render bit for bit as before (pre-change HLSL on WARP)") {
    Warp w;
    REQUIRE(w.ok);
    struct Case {
        std::wstring panel; bool hdr; int width, height; const std::vector<uint16_t>* frame; unsigned int debug; bool star;
        std::string name;
    };
    std::vector<std::unique_ptr<PitchTempFile>> temps;
    std::vector<std::unique_ptr<std::vector<uint16_t>>> frames;
    std::vector<Case> cases;
    struct Synth { PanelSpec s; int width, height; const char* name; };
    std::vector<Synth> synth;
    { PanelSpec s; s.cols = 12; s.rows = 8; s.cellW = 40; s.cellH = 45; synth.push_back({ s, 480, 360, "PQ 12x8 of 40x45" }); }
    { PanelSpec s; s.cols = 6; s.rows = 4; s.cellW = 80; s.cellH = 90; s.originX = 8; s.originY = 6;   // 7200-px zones: 2 slices
      synth.push_back({ s, 500, 380, "PQ 6x4 of 80x90 at (8,6), two sweep slices" }); }
    { PanelSpec s; s.cols = 10; s.rows = 6; s.cellW = 48; s.cellH = 40; s.gamma = true; synth.push_back({ s, 480, 240, "SDR 10x6 of 48x40" }); }
    { PanelSpec s; s.cols = 12; s.rows = 8; s.cellW = 40; s.cellH = 45; s.boost = false; synth.push_back({ s, 480, 360, "PQ 12x8 no boost" }); }
    const std::pair<unsigned int, bool> configs[] = { { 0, false }, { 0, true }, { 1, false }, { 2, false }, { 3, false },
                                                      { 5, false }, { 7, false }, { 8, false }, { 9, true } };
    for (size_t i = 0; i < synth.size(); i++) {
        temps.push_back(std::make_unique<PitchTempFile>(L"test_fald_pitch_bitid_" + std::to_wstring(i) + L".bin"));
        WritePanel(temps.back()->path, MakePanel(synth[i].s));
        frames.push_back(std::make_unique<std::vector<uint16_t>>(SyntheticFrame(synth[i].width, synth[i].height, synth[i].s.gamma)));
        for (const auto& c : configs)
            cases.push_back({ temps.back()->path, !synth[i].s.gamma, synth[i].width, synth[i].height, frames.back().get(), c.first,
                              c.second, std::string(synth[i].name) + " debug " + std::to_string(c.first) + (c.second ? " star" : "") });
    }
    const std::vector<std::wstring> owner = OwnerPanelFiles();
    for (const std::wstring& path : owner) {
        FaldPanelParams p; std::string err;
        if (!LoadFaldPanelParams(path, p, err) || p.originX != 0 || p.originY != 0) continue;
        frames.push_back(std::make_unique<std::vector<uint16_t>>(SyntheticFrame((int)p.latW, (int)p.latH, p.transfer == FALD_TRANSFER_GAMMA)));
        char nameBuf[MAX_PATH * 2] = {};
        WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, nameBuf, (int)sizeof(nameBuf), nullptr, nullptr);
        for (bool star : { false, true })
            cases.push_back({ path, p.transfer != FALD_TRANSFER_GAMMA, (int)p.latW, (int)p.latH, frames.back().get(), 0u, star,
                              std::string(nameBuf) + (star ? " star" : "") });
    }
    MESSAGE("bit-identity cases: " << cases.size() << " (" << owner.size() << " owner panel file(s) found)");

    // the frozen pre-change HLSL (its three-pass gain + blur: the fused pass is not in the frozen set) ...
    g_faldGainFusedOverride = 0;
    REQUIRE(InitFaldShadersFrom(LegacySources()));
    std::vector<std::vector<std::pair<std::string, std::pair<size_t, uint64_t>>>> ref(cases.size());
    for (size_t i = 0; i < cases.size(); i++) {
        const Case& c = cases[i];
        CAPTURE(c.name);
        LayerRun lr;
        REQUIRE_MESSAGE(RunLayer(w, c.panel, c.hdr, c.width, c.height, *c.frame, c.debug, c.star, lr), lr.error);
        for (const auto& f : lr.fields) ref[i].push_back({ f.first, { f.second.size(), Fnv(f.second) } });
        ref[i].push_back({ "OUTPUT", { lr.out.size(), Fnv(lr.out) } });
    }
    ReleaseFaldShaders();
    g_faldGainFusedOverride = -1;
    // ... and the current HLSL: every field and the output, bit for bit
    REQUIRE(InitFaldShaders());
    size_t mismatches = 0;
    for (size_t i = 0; i < cases.size(); i++) {
        const Case& c = cases[i];
        CAPTURE(c.name);
        LayerRun lr;
        REQUIRE_MESSAGE(RunLayer(w, c.panel, c.hdr, c.width, c.height, *c.frame, c.debug, c.star, lr), lr.error);
        std::vector<std::pair<std::string, std::pair<size_t, uint64_t>>> now;
        for (const auto& f : lr.fields) now.push_back({ f.first, { f.second.size(), Fnv(f.second) } });
        now.push_back({ "OUTPUT", { lr.out.size(), Fnv(lr.out) } });
        REQUIRE(now.size() == ref[i].size());
        for (size_t k = 0; k < now.size(); k++) {
            CAPTURE(now[k].first);
            CHECK(now[k].second.first == ref[i][k].second.first);
            CHECK(now[k].second.second == ref[i][k].second.second);
            if (now[k].second != ref[i][k].second) mismatches++;
        }
        // the comparison is not vacuous: the layer changed the frame (view 0), the starfield found star-like zones
        if (c.debug == 0) {
            const std::vector<uint8_t> in((const uint8_t*)c.frame->data(), (const uint8_t*)c.frame->data() + c.frame->size() * 2);
            CHECK(lr.out.size() == in.size());
            CHECK(lr.out != in);
        }
        if (c.star) {
            const std::vector<uint8_t>* st = lr.Field("star_stat");
            REQUIRE((st && !st->empty()));
            const float* v = (const float*)st->data();
            size_t speck = 0;
            for (size_t z = 0; z < st->size() / 16; z++) speck += v[4 * z + 1] > 0.5f ? 1 : 0;
            CHECK(speck > 0);
        }
    }
    CHECK(mismatches == 0);
    // the device is deterministic (else equal hashes would say little): the first case once more
    {
        LayerRun a;
        REQUIRE(RunLayer(w, cases[0].panel, cases[0].hdr, cases[0].width, cases[0].height, *cases[0].frame, cases[0].debug,
                         cases[0].star, a));
        CHECK(Fnv(a.out) == ref[0].back().second.second);
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// WARP: fractional lattices follow the CPU reference
// ---------------------------------------------------------------------------------------------------------------------
TEST_CASE("FALD fractional pitch: on WARP the zone sweeps and the per-pixel zone lookup follow the CPU reference") {
    Warp w;
    REQUIRE(w.ok);
    REQUIRE(InitFaldShaders());
    struct Lat { uint32_t cols, rows, extW, extH, ox, oy; int frameW, frameH; const char* what; };
    const Lat lats[] = {
        { 48, 24, 2560, 1440, 0, 0, 2560, 1440, "27-inch 1440p 1152 zones, 48 x 24 (53.33 x 60 px)" },
        { 36, 32, 2560, 1440, 0, 0, 2560, 1440, "27-inch 1440p 1152 zones, 36 x 32 (71.11 x 45 px)" },
        { 7, 5, 373, 221, 5, 3, 380, 226, "odd pitch on both axes, an origin, a frame larger than the lattice" },
        { 2, 2, 129, 128, 0, 0, 129, 128, "zones of 4096 / 4160 px: two sweep slices, the narrow zone's second one empty" },
    };
    for (const Lat& L : lats) {
        CAPTURE(L.what);
        PanelSpec s; s.cols = L.cols; s.rows = L.rows; s.cellW = 0; s.cellH = 0; s.extentW = L.extW; s.extentH = L.extH;
        s.originX = L.ox; s.originY = L.oy;
        PitchTempFile tf(L"test_fald_pitch_frac.bin");
        WritePanel(tf.path, MakePanel(s));
        FaldPanelParams p; std::string err;
        REQUIRE_MESSAGE(LoadFaldPanelParams(tf.path, p, err), err);
        REQUIRE(FaldLatticeFractional(p));
        const int W = L.frameW, H = L.frameH;
        auto zoneOf = [&](int x, int y, uint32_t& cx, uint32_t& cy) {
            if (x < (int)p.originX || y < (int)p.originY || x >= (int)(p.originX + p.latW) || y >= (int)(p.originY + p.latH)) return false;
            cx = FaldZoneOfPixel((uint32_t)x - p.originX, p.cols, p.latW);
            cy = FaldZoneOfPixel((uint32_t)y - p.originY, p.rows, p.latH);
            return true;
        };
        auto edgeX = [&](uint32_t c) { return FaldZoneEdge(c, p.cols, p.latW); };
        auto edgeY = [&](uint32_t r) { return FaldZoneEdge(r, p.rows, p.latH); };

        // (1) the zone sweeps: per zone a uniform base (neighbours always differ: 1 + (cx + 2 cy) mod 5 nits) + ONE bright
        // pixel at a zone-local position; the starfield's S0 reports per zone the peak, the darkest pixel, the lit sum and
        // the brightest pixel's index ly * zw + lx — a pixel swept into the wrong zone moves at least one of them
        {
            std::vector<uint16_t> f((size_t)W * H * 4, 0);
            for (int i = 0; i < W * H; i++) f[(size_t)i * 4 + 3] = Half(1.0f);
            for (int y = 0; y < H; y++)
                for (int x = 0; x < W; x++) {
                    uint32_t cx, cy;
                    if (!zoneOf(x, y, cx, cy)) continue;
                    const float g = (1.0f + (float)((cx + 2 * cy) % 5)) / 80.0f;
                    PutPixel(f, W, x, y, g, g, g);
                }
            for (uint32_t cy = 0; cy < p.rows; cy++)
                for (uint32_t cx = 0; cx < p.cols; cx++) {
                    const uint32_t zw = edgeX(cx + 1) - edgeX(cx), zh = edgeY(cy + 1) - edgeY(cy);
                    const uint32_t lx = (cx * 7 + 3) % zw, ly = (cy * 5 + 2) % zh;
                    const float g = (200.0f + 7.0f * (float)((cx + cy) % 50)) / 80.0f;
                    PutPixel(f, W, (int)(p.originX + edgeX(cx) + lx), (int)(p.originY + edgeY(cy) + ly), g, g, g);
                }
            LayerRun lr;
            REQUIRE_MESSAGE(RunLayer(w, tf.path, true, W, H, f, 0, true, lr), lr.error);
            const std::vector<uint8_t>* st = lr.Field("star_stat");
            const std::vector<uint8_t>* bg = lr.Field("star_bg");
            const std::vector<uint8_t>* a0 = lr.Field("active_r0");
            REQUIRE((st && bg && a0));
            REQUIRE(st->size() == (size_t)p.cols * p.rows * 16);
            const float* sv = (const float*)st->data();
            const float* bv = (const float*)bg->data();
            const float* av = (const float*)a0->data();
            size_t badPeak = 0, badMin = 0, badSum = 0, badArg = 0, badActive = 0;
            for (uint32_t cy = 0; cy < p.rows; cy++)
                for (uint32_t cx = 0; cx < p.cols; cx++) {
                    const uint32_t x0 = p.originX + edgeX(cx), y0 = p.originY + edgeY(cy);
                    const uint32_t zw = edgeX(cx + 1) - edgeX(cx), zh = edgeY(cy + 1) - edgeY(cy);
                    double peak = -1.0, mn = 1e30, total = 0.0;
                    uint32_t arg = 0;
                    for (uint32_t ly = 0; ly < zh; ly++)
                        for (uint32_t lx = 0; lx < zw; lx++) {
                            const double v = std::min(GreyNitsPq(f[(((size_t)(y0 + ly)) * W + (x0 + lx)) * 4]), 1000.0);
                            if (v > peak) { peak = v; arg = ly * zw + lx; }
                            mn = std::min(mn, v);
                            if (v > 0.5) total += v;
                        }
                    const size_t z = (size_t)cy * p.cols + cx;
                    if (std::fabs(sv[4 * z] - peak) > 1e-5 * peak) badPeak++;
                    if (std::fabs(std::exp((double)bv[4 * z]) - mn) > 1e-5 * mn) badMin++;
                    if (std::fabs(bv[4 * z + 2] - total) > 2e-5 * total) badSum++;
                    if ((uint32_t)(bv[4 * z + 1] + 0.5f) != arg) badArg++;
                    if (!(av[z] > 0.5f)) badActive++;
                }
            CHECK(badPeak == 0); CHECK(badMin == 0); CHECK(badSum == 0); CHECK(badArg == 0); CHECK(badActive == 0);
        }

        // (2) the per-pixel zone lookup (debug view 8 = each pixel shows ITS zone's non-black flag) and the zone flags of
        // both statistic rounds, on three on/off patterns: column stripes, row stripes, a checkerboard
        for (int pattern = 0; pattern < 3; pattern++) {
            CAPTURE(pattern);
            auto on = [&](uint32_t cx, uint32_t cy) { return pattern == 0 ? (cx % 2 == 0) : pattern == 1 ? (cy % 2 == 0) : ((cx + cy) % 2 == 0); };
            std::vector<uint16_t> f((size_t)W * H * 4, 0);
            for (int y = 0; y < H; y++)
                for (int x = 0; x < W; x++) {
                    uint32_t cx, cy;
                    const float g = !zoneOf(x, y, cx, cy) ? 3.0f / 80.0f : (on(cx, cy) ? 100.0f / 80.0f : 0.0f);
                    PutPixel(f, W, x, y, g, g, g);
                }
            LayerRun lr;
            REQUIRE_MESSAGE(RunLayer(w, tf.path, true, W, H, f, 8, false, lr), lr.error);
            const std::vector<uint8_t>* a0 = lr.Field("active_r0");
            const std::vector<uint8_t>* a1 = lr.Field("active_r1");
            REQUIRE((a0 && a1 && a0->size() == (size_t)p.cols * p.rows * 4));
            size_t badFlags = 0;
            for (uint32_t cy = 0; cy < p.rows; cy++)
                for (uint32_t cx = 0; cx < p.cols; cx++) {
                    const size_t z = (size_t)cy * p.cols + cx;
                    const bool want = on(cx, cy);
                    if ((((const float*)a0->data())[z] > 0.5f) != want) badFlags++;
                    if ((((const float*)a1->data())[z] > 0.5f) != want) badFlags++;
                }
            CHECK(badFlags == 0);
            REQUIRE(lr.out.size() == f.size() * 2);
            const uint16_t* o = (const uint16_t*)lr.out.data();
            const uint16_t white = Half(100.0f / 80.0f), one = Half(1.0f);
            size_t badPx = 0;
            for (int y = 0; y < H; y++)
                for (int x = 0; x < W; x++) {
                    const size_t i = ((size_t)y * W + x) * 4;
                    uint32_t cx, cy;
                    if (!zoneOf(x, y, cx, cy)) {                              // outside the lattice: the source, untouched
                        if (o[i] != f[i] || o[i + 1] != f[i + 1] || o[i + 2] != f[i + 2] || o[i + 3] != f[i + 3]) badPx++;
                        continue;
                    }
                    const uint16_t v = on(cx, cy) ? white : (uint16_t)0;
                    if (o[i] != v || o[i + 1] != v || o[i + 2] != v || o[i + 3] != one) badPx++;
                }
            CHECK(badPx == 0);
        }

        // (3) the correcting path on this lattice: finite output, the layer did something
        {
            std::vector<uint16_t> f = SyntheticFrame(W, H, false);
            LayerRun lr;
            REQUIRE_MESSAGE(RunLayer(w, tf.path, true, W, H, f, 0, true, lr), lr.error);
            REQUIRE(lr.out.size() == f.size() * 2);
            const uint16_t* o = (const uint16_t*)lr.out.data();
            size_t nonFinite = 0, changed = 0;
            for (size_t i = 0; i < f.size(); i++) {
                if (!std::isfinite(FromHalf(o[i]))) nonFinite++;
                if (o[i] != f[i]) changed++;
            }
            CHECK(nonFinite == 0);
            CHECK(changed > 0);
        }
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// The black-frame boost below 1 (small-highlight SUPPRESSION, steps 0.25..4 since 2026-10-19) on the device
// ---------------------------------------------------------------------------------------------------------------------
TEST_CASE("FALD boost LUT: a 0.35 suppression step on WARP = the CPU reference (48 x 24 at 2560 x 1440, fractional)") {
    Warp w;
    REQUIRE(w.ok);
    REQUIRE(InitFaldShaders());
    PanelSpec s; s.cols = 48; s.rows = 24; s.cellW = 0; s.cellH = 0; s.extentW = 2560; s.extentH = 1440;
    s.lut = { { 0.0f, 0.35f }, { 12.0f / 1152.0f, 0.6f }, { 116.0f / 1152.0f, 0.8f }, { 0.5f, 1.0f } };
    PitchTempFile tf(L"test_fald_pitch_suppress.bin");
    WritePanel(tf.path, MakePanel(s));
    FaldPanelParams p; std::string err;
    REQUIRE_MESSAGE(LoadFaldPanelParams(tf.path, p, err), err);
    REQUIRE(p.boostVal[0] == 0.35f);
    const int W = 2560, H = 1440;
    for (unsigned int k : { 1u, 11u, 12u, 115u, 116u, 600u }) {        // non-black zones of the frame (row-major from 0)
        CAPTURE(k);
        std::vector<uint16_t> f((size_t)W * H * 4, 0);
        for (int y = 0; y < H; y++)
            for (int x = 0; x < W; x++) {
                const uint32_t cx = FaldZoneOfPixel((uint32_t)x, p.cols, p.latW), cy = FaldZoneOfPixel((uint32_t)y, p.rows, p.latH);
                const float g = (cy * p.cols + cx < k) ? 300.0f / 80.0f : 0.0f;
                PutPixel(f, W, x, y, g, g, g);
            }
        LayerRun lr;
        REQUIRE_MESSAGE(RunLayer(w, tf.path, true, W, H, f, 0, false, lr), lr.error);
        const std::vector<uint8_t>* b0 = lr.Field("boost_r0");
        const std::vector<uint8_t>* b1 = lr.Field("boost_r1");
        REQUIRE((b0 && b1 && b0->size() == 8 && b1->size() == 8));
        const float* v0 = (const float*)b0->data();
        const float* v1 = (const float*)b1->data();
        CHECK(v0[1] == (float)k);                                      // round 0 counts the source's k zones ...
        CHECK(v0[0] == FaldBoostOfCount(p, k));                        // ... and looks the CPU reference's step up
        CHECK(v1[0] == FaldBoostOfCount(p, (unsigned int)v1[1]));      // round 1 (the corrected frame): its own count
        if (k <= 11u) CHECK(v0[0] == 0.35f);
        REQUIRE(lr.out.size() == f.size() * 2);
        const uint16_t* o = (const uint16_t*)lr.out.data();
        size_t nonFinite = 0;
        for (size_t i = 0; i < f.size(); i++) nonFinite += std::isfinite(FromHalf(o[i])) ? 0 : 1;
        CHECK(nonFinite == 0);
    }
}
