// The change-driven path's pure decisions (shared/fald_change.h): the zones a present's rects touch, the preconditions.
#include "doctest.h"
#include "../shared/fald_change.h"
#include "../shared/fald_panel.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include <d3dcompiler.h>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

namespace {
std::vector<uint32_t> Zones(const std::vector<FaldChangeRect>& rects, unsigned int ox, unsigned int oy, unsigned int latW, unsigned int latH,
                            unsigned int cols, unsigned int rows, unsigned int cap = 1u << 30, bool* ok = nullptr) {
    std::vector<uint32_t> out;
    const bool r = FaldTouchedZones(rects.data(), (int)rects.size(), ox, oy, latW, latH, cols, rows, cap, out);
    if (ok) *ok = r;
    return out;
}
}  // namespace

TEST_CASE("FALD change: the zones a rect touches follow the shaders' zone mapping, deduplicated and ascending") {
    // 48 x 48 zones of 80 x 45 px on a 3840 x 2160 lattice at (0, 0)
    CHECK(Zones({ { 0, 0, 1, 1 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 0 });
    CHECK(Zones({ { 79, 44, 80, 45 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 0 });        // the zone's last pixel
    CHECK(Zones({ { 80, 45, 81, 46 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 49 });       // the next zone's first
    CHECK(Zones({ { 79, 44, 81, 46 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 0, 1, 48, 49 });
    CHECK(Zones({ { 3839, 2159, 3840, 2160 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 48 * 48 - 1 });
    // two rects sharing zones: each zone once, ascending
    CHECK(Zones({ { 0, 0, 100, 10 }, { 50, 0, 200, 50 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 0, 1, 2, 48, 49, 50 });
    // a rect beyond the lattice is clipped; one entirely outside touches nothing; empty rects touch nothing
    CHECK(Zones({ { -100, -100, 1, 1 } }, 0, 0, 3840, 2160, 48, 48) == std::vector<uint32_t>{ 0 });
    CHECK(Zones({ { 3840, 0, 5000, 100 } }, 0, 0, 3840, 2160, 48, 48).empty());
    CHECK(Zones({ { 10, 10, 10, 50 } }, 0, 0, 3840, 2160, 48, 48).empty());
    // the full frame touches every zone
    CHECK(Zones({ { 0, 0, 3840, 2160 } }, 0, 0, 3840, 2160, 48, 48).size() == 48u * 48u);
    // a lattice with an origin: pixels left of it belong to no zone
    CHECK(Zones({ { 0, 0, 5, 5 } }, 100, 200, 1000, 500, 10, 5).empty());
    CHECK(Zones({ { 100, 200, 101, 201 } }, 100, 200, 1000, 500, 10, 5) == std::vector<uint32_t>{ 0 });
    // a fractional pitch (48 x 24 over 2560 x 1440: 53.33-px zones): the mapping is the shaders' (FaldZoneOfPixel)
    for (unsigned int x : { 0u, 52u, 53u, 54u, 106u, 107u, 2559u }) {
        const std::vector<uint32_t> z = Zones({ { (int)x, 0, (int)x + 1, 1 } }, 0, 0, 2560, 1440, 48, 24);
        REQUIRE(z.size() == 1);
        CHECK(z[0] == FaldZoneOfPixel(x, 48, 2560));
    }
}

TEST_CASE("FALD change: the cap turns a large touched set into the full path") {
    bool ok = true;
    std::vector<uint32_t> z = Zones({ { 0, 0, 3840, 2160 } }, 0, 0, 3840, 2160, 48, 48, FaldChangeMaxZones(48 * 48), &ok);
    CHECK_FALSE(ok);
    CHECK(z.empty());
    z = Zones({ { 0, 0, 3840, 1000 } }, 0, 0, 3840, 2160, 48, 48, FaldChangeMaxZones(48 * 48), &ok);   // 23 rows = 1104 zones <= 1152
    CHECK(ok);
    CHECK(z.size() == 48u * 23u);
    CHECK(FaldChangeMaxZones(1) == 1);
    CHECK(FaldChangeMaxZones(2304) == 1152);
}

namespace {
std::string ChEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}
struct ChWarp {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr; ID3D11VertexShader* vs = nullptr; bool ok = false;
    ChWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~ChWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};
std::vector<uint8_t> ChReadBack(ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    if (!tex) return out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
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
uint64_t ChFnv(const std::vector<uint8_t>& b) { uint64_t h = 1469598103934665603ull; for (uint8_t c : b) { h ^= c; h *= 1099511628211ull; } return h; }
std::wstring ChOwnerPanel(const wchar_t* rel) {
    const std::wstring p = std::wstring(L"H:\\Projects\\DesktopLUT\\") + rel;
    return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES ? p : std::wstring();
}
// One layer context (its own monitor context + target) run on a frame with a path override.
struct ChLayer {
    MonitorContext ctx;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    bool ok = false;
    ChLayer(ChWarp& w, const std::wstring& panel, int fw, int fh, bool star, unsigned int temporal) {
        ctx.width = fw; ctx.height = fh; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = 16.667f;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = star;
        fs.temporalMode = temporal; fs.tauRiseMs = 40.0f; fs.tauFallMs = 60.0f; fs.delayFrames = temporal ? 1u : 0u;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)fw; td.Height = (UINT)fh; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(w.dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(w.dev->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~ChLayer() { if (rtv) rtv->Release(); if (target) target->Release(); FaldReleaseResources(&ctx); }
    // upload the whole frame, run with the rects (numRects < 0: unknown = full), return the hashes of every field + the output
    std::vector<std::pair<std::string, uint64_t>> Run(ChWarp& w, const std::vector<uint16_t>& frame, unsigned long long key, bool newContent,
                                                     const std::vector<FaldChangeRect>* rects) {
        D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        FaldRunPasses(&ctx, rtv, newContent, key, rects ? rects->data() : nullptr, rects ? (int)rects->size() : -1);
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        const std::pair<const char*, ID3D11Texture2D*> tex[] = {
            { "drive0", r->drive0Tex }, { "drive", r->driveTex }, { "drive_filt0", r->driveFilt0Tex }, { "drive_filt", r->driveFiltTex },
            { "drive_state", r->driveStateTex }, { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] },
            { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] }, { "btrue0", r->bTrue0Tex }, { "best0", r->bEst0Tex },
            { "gain0", r->gain0Tex }, { "btrue", r->bTrueTex }, { "best", r->bEstTex }, { "gainB", r->gainBTex },
            { "star_stat", r->starStatTex }, { "star_w", r->starWTex }, { "star_plan", r->starPlanTex }, { "star_plan2", r->starPlan2Tex },
            { "star_bg", r->starBgTex }, { "OUTPUT", target } };
        std::vector<std::pair<std::string, uint64_t>> out;
        for (const auto& t : tex) if (t.second) out.emplace_back(t.first, ChFnv(ChReadBack(t.second)));
        return out;
    }
};
// a frame edited in place: the pixels of a rect scaled (dark: a change that moves no drive; bright: one that does)
void ChPatch(std::vector<uint16_t>& f, int W, const FaldChangeRect& rc, uint16_t value) {
    for (int y = rc.top; y < rc.bottom; y++)
        for (int x = rc.left; x < rc.right; x++)
            for (int c = 0; c < 3; c++) f[((size_t)y * W + x) * 4 + c] = value;
}
}  // namespace

// The change-driven path against the full path, run for run on the same frames (WARP): every field texture and the output
// hash equal, whatever the rects touch — a dark patch that moves no zone's drive, a bright one that moves drives (round 0),
// one that only moves round 1's, no change at all (the same frame again), and a sequence with LED lag (mode 2, a delay
// ring: the filtered maps move by themselves and are flagged on the GPU). The full-path context runs with the override.
TEST_CASE("FALD change: the partial path equals the full path, field for field and in the output (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = ChEnv("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::wstring C = ChOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    const int W = 3840, H = 2160;
    std::vector<uint16_t> base((size_t)W * H * 4);
    {
        std::ifstream f(cellDir + "/n7_lamp.rgba16f", std::ios::binary);
        REQUIRE(f.good());
        f.read((char*)base.data(), (std::streamsize)(base.size() * 2));
        REQUIRE((size_t)f.gcount() == base.size() * 2);
    }
    ChWarp w;
    REQUIRE(w.ok);
    // the steps: (rects, the edit) — a frame is the previous one with the edit applied inside the rect
    struct Step { FaldChangeRect rc; uint16_t value; bool newContent; const char* what; };
    const Step steps[] = {
        { { 1000, 1500, 1040, 1530 }, 0x0400, true, "a dark 40 x 30 patch (0.004 nits / channel): no drive moves" },   // 2^-7 scRGB
        { { 300, 300, 500, 500 }, 0x3C00, true, "a 200 x 200 white patch: drives move" },
        { { 0, 0, 0, 0 }, 0, false, "the same frame again (no rect)" },
        { { 300, 300, 500, 500 }, 0x0000, true, "the white patch to black: drives move back" },
        { { 2000, 1000, 2002, 1002 }, 0x3C00, true, "a 2 x 2 white speck: round 1 may move, round 0 may not" },
        { { 79, 44, 81, 46 }, 0x2C00, true, "a 2 x 2 patch across four zones' corner" },
    };
    for (unsigned int temporal : { 0u, 2u })
        for (int star = 0; star < 2; star++) {
            g_faldPartialOverride = 0;
            ChLayer full(w, C, W, H, star != 0, temporal);
            g_faldPartialOverride = -1;
            ChLayer part(w, C, W, H, star != 0, temporal);
            REQUIRE(full.ok); REQUIRE(part.ok);
            std::vector<uint16_t> frame = base;
            unsigned long long key = 1000;
            // the first run: both full (the partial context has no state yet)
            auto a = full.Run(w, frame, key, true, nullptr);
            auto b = part.Run(w, frame, key, true, nullptr);
            REQUIRE(a == b);
            CHECK_FALSE(part.ctx.fald->lastPartial);
            int partialRuns = 0;
            for (const Step& s : steps) {
                std::vector<FaldChangeRect> rects;
                if (s.newContent) { ChPatch(frame, W, s.rc, s.value); rects.push_back(s.rc); key++; }
                const std::vector<FaldChangeRect> none;
                auto fa = full.Run(w, frame, key, s.newContent, nullptr);
                auto pb = part.Run(w, frame, key, s.newContent, s.newContent ? &rects : &none);
                const bool same = fa == pb;
                CHECK_MESSAGE(same, "temporal " << temporal << " star " << star << ": " << s.what);
                if (!same)
                    for (size_t i = 0; i < fa.size() && i < pb.size(); i++)
                        if (fa[i] != pb[i]) MESSAGE("  differs: " << fa[i].first);
                if (part.ctx.fald->lastPartial) partialRuns++;
            }
            CHECK_MESSAGE(partialRuns == (int)(sizeof(steps) / sizeof(steps[0])), "temporal " << temporal << " star " << star
                          << ": every step after the first should take the partial path (" << partialRuns << ")");
        }
}

TEST_CASE("FALD change: the partial path needs a complete state, the same settings, a known frame, the fused gain pass") {
    FaldChangeInputs in = {};
    in.stateComplete = true; in.settingsSame = true; in.frameKnown = true; in.newContent = true; in.gainFused = true;
    in.temporalMode = 0; in.fadeOn = false;
    in.touchedZones = 3; in.totalZones = 2304;
    const unsigned int cap = FaldChangeMaxZones(2304);
    CHECK(FaldChangeDecide(in, cap) == FALD_CHANGE_PARTIAL);
    auto fails = [&](auto&& mutate) { FaldChangeInputs x = in; mutate(x); return FaldChangeDecide(x, cap) == FALD_CHANGE_FULL; };
    CHECK(fails([](FaldChangeInputs& x) { x.stateComplete = false; }));
    CHECK(fails([](FaldChangeInputs& x) { x.settingsSame = false; }));
    CHECK(fails([](FaldChangeInputs& x) { x.frameKnown = false; }));
    CHECK(fails([](FaldChangeInputs& x) { x.fadeOn = true; }));
    CHECK(fails([](FaldChangeInputs& x) { x.gainFused = false; }));
    CHECK(fails([](FaldChangeInputs& x) { x.touchedZones = 1153; }));
    CHECK(fails([](FaldChangeInputs& x) { x.totalZones = 0; }));
    CHECK(fails([](FaldChangeInputs& x) { x.temporalMode = 7; }));
    // a kick-only present (no new content) is partial like any other small change
    { FaldChangeInputs x = in; x.newContent = false; x.touchedZones = 1; CHECK(FaldChangeDecide(x, cap) == FALD_CHANGE_PARTIAL); }
    { FaldChangeInputs x = in; x.touchedZones = 0; CHECK(FaldChangeDecide(x, cap) == FALD_CHANGE_PARTIAL); }
    // LED lag: every known mode is partial-able (its passes compare their outputs on the GPU and flag a movement)
    for (unsigned int mode : { 1u, 2u, 3u }) { FaldChangeInputs x = in; x.temporalMode = mode; CHECK(FaldChangeDecide(x, cap) == FALD_CHANGE_PARTIAL); }
}
