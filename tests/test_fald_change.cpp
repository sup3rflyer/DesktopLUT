// The change-driven path's pure decisions (shared/fald_change.h): the zones a present's rects touch, the preconditions.
#include "doctest.h"
#include "../shared/fald_change.h"
#include "../shared/fald_core2.h"       // the always-on core: its textures in the hash list, the mode per layer
#include "../shared/fald_panel.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/peak_detect.h"
#include <d3dcompiler.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
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
// One layer context (its own monitor context + target) run on a frame with a path override. `partialOverride` is applied
// at EVERY run (g_faldPartialOverride is read by FaldRunPasses, not at build): 0 = the full path always (the reference),
// -1 = the change-driven path by its rules.
struct ChLayer {
    MonitorContext ctx;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    bool ok = false;
    int partialOverride = -1;
    // coreMode: the always-on core's rule (shared/fald_core_mode.h; 0 = C, the 2026-10-22 gates; 2 = KC3, the default)
    ChLayer(ChWarp& w, const std::wstring& panel, int fw, int fh, bool star, unsigned int temporal, int partialOverrideAtRun = -1,
            unsigned int coreMode = FALD_CORE_MODE_C)
        : partialOverride(partialOverrideAtRun) {
        ctx.width = fw; ctx.height = fh; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = 16.667f;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = star; fs.coreMode = coreMode;
        fs.temporalMode = temporal; fs.tauRiseMs = 40.0f; fs.tauFallMs = 60.0f; fs.delayFrames = temporal ? 1u : 0u;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)fw; td.Height = (UINT)fh; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(w.dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(w.dev->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~ChLayer() { if (rtv) rtv->Release(); if (target) target->Release(); FaldReleaseResources(&ctx); }
    // upload the whole frame, run with the rects (numRects < 0: unknown = full), return the hashes of every field + the output;
    // word = the caller's "the frame changed everywhere" buffer (fald.h frameChanged; (4, moved, 0, 0) as the peak pass writes)
    std::vector<std::pair<std::string, uint64_t>> Run(ChWarp& w, const std::vector<uint16_t>& frame, unsigned long long key, bool newContent,
                                                     const std::vector<FaldChangeRect>* rects, ID3D11Buffer* word = nullptr) {
        D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
        g_faldPartialOverride = partialOverride;
        FaldRunPasses(&ctx, rtv, newContent, key, rects ? rects->data() : nullptr, rects ? (int)rects->size() : -1, word, 4u);
        g_faldPartialOverride = -1;
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        const std::pair<const char*, ID3D11Texture2D*> tex[] = {
            { "drive0", r->drive0Tex }, { "drive", r->driveTex }, { "drive_filt0", r->driveFilt0Tex }, { "drive_filt", r->driveFiltTex },
            { "drive_state", r->driveStateTex }, { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] },
            { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] }, { "btrue0", r->bTrue0Tex }, { "best0", r->bEst0Tex },
            { "gain0", r->gain0Tex }, { "btrue", r->bTrueTex }, { "best", r->bEstTex }, { "gainB", r->gainBTex },
            { "fields0", r->fields0Tex }, { "fields", r->fieldsTex },
            { "star_stat", r->starStatTex }, { "star_w", r->starWTex }, { "star_plan", r->starPlanTex }, { "star_plan2", r->starPlan2Tex },
            { "star_bg", r->starBgTex }, { "OUTPUT", target } };
        std::vector<std::pair<std::string, uint64_t>> out;
        for (const auto& t : tex) if (t.second) out.emplace_back(t.first, ChFnv(ChReadBack(t.second)));
        if (r->core2On) {                                          // the always-on core's textures (shared/fald_core2.h)
            const FaldCore2Debug cd = FaldCore2DebugViews(r->core2);
            const std::pair<const char*, ID3D11Texture2D*> core[] = {
                { "core_stat", cd.stat }, { "core_zone", cd.zone }, { "core_field0", cd.field0 }, { "core_field", cd.field },
                { "core_node_peak", cd.nodePeak }, { "core_node_d0", cd.nodeD0 }, { "core_node_d", cd.nodeD } };
            for (const auto& t : core) if (t.second) out.emplace_back(t.first, ChFnv(ChReadBack(t.second)));
        }
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
            ChLayer full(w, C, W, H, star != 0, temporal, 0);    // the reference: the full path at every run
            ChLayer part(w, C, W, H, star != 0, temporal, -1);   // the change-driven path by its rules
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

// The same steps with the always-on core on (shared/fald_core2.h). KC3 (mode 2, the default) is stateless: the touched-zone
// sweep recomputes and compares the zones' core statistics / node peaks, the decide pass orders the core's lattice passes
// only when they or the gain moved, and the previous run's core fields stand otherwise — every field (the core's included)
// and the output equal the full path's, and every step after the first is partial. KC2c (mode 1) has zone IIRs: the
// change-driven context must take the FULL path on every run (FaldChangeInputs::coreStateful) and equal the reference.
TEST_CASE("FALD change: the partial path equals the full path with the always-on core on (KC3 partial, KC2c held full) (WARP; FALD_TEST_FADE_DIR)") {
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
    struct Step { FaldChangeRect rc; uint16_t value; bool newContent; const char* what; };
    const Step steps[] = {
        { { 1000, 1500, 1040, 1530 }, 0x0400, true, "a dark 40 x 30 patch (0.004 nits / channel): no drive moves, the zone's M / lnG do" },
        { { 300, 300, 500, 500 }, 0x3C00, true, "a 200 x 200 white patch: drives and node peaks move" },
        { { 0, 0, 0, 0 }, 0, false, "the same frame again (no rect): nothing moves, the core's fields stand" },
        { { 300, 300, 500, 500 }, 0x0000, true, "the white patch to black: drives move back" },
        { { 2000, 1000, 2002, 1002 }, 0x3C00, true, "a 2 x 2 white speck: a node peak moves, round 0's drive may not" },
        { { 79, 44, 81, 46 }, 0x2C00, true, "a 2 x 2 patch across four zones' corner" },
        { { 1000, 1500, 1040, 1530 }, 0x0000, true, "the dark patch to black again" },
    };
    const unsigned long long savedRefreshes = g_faldCore2TestRefreshes;
    g_faldCore2TestRefreshes = 1;                                  // KC2c's IIR: one refresh per run whatever WARP's timing
    for (unsigned int coreMode : { FALD_CORE_MODE_KC3, FALD_CORE_MODE_KC2C })
        for (unsigned int temporal : { 0u, 2u })
            for (int star = 0; star < 2; star++) {
                ChLayer full(w, C, W, H, star != 0, temporal, 0, coreMode);    // the reference: the full path at every run
                ChLayer part(w, C, W, H, star != 0, temporal, -1, coreMode);   // the change-driven path by its rules
                REQUIRE(full.ok); REQUIRE(part.ok);
                std::vector<uint16_t> frame = base;
                unsigned long long key = 2000;
                auto a = full.Run(w, frame, key, true, nullptr);
                auto b = part.Run(w, frame, key, true, nullptr);
                REQUIRE(a == b);
                REQUIRE(full.ctx.fald->core2On);
                CHECK_FALSE(part.ctx.fald->lastPartial);
                int partialRuns = 0;
                for (const Step& s : steps) {
                    std::vector<FaldChangeRect> rects;
                    if (s.newContent) { ChPatch(frame, W, s.rc, s.value); rects.push_back(s.rc); key++; }
                    const std::vector<FaldChangeRect> none;
                    auto fa = full.Run(w, frame, key, s.newContent, nullptr);
                    auto pb = part.Run(w, frame, key, s.newContent, s.newContent ? &rects : &none);
                    const bool same = fa == pb;
                    CHECK_MESSAGE(same, "core mode " << coreMode << " temporal " << temporal << " star " << star << ": " << s.what);
                    if (!same)
                        for (size_t i = 0; i < fa.size() && i < pb.size(); i++)
                            if (fa[i] != pb[i]) MESSAGE("  differs: " << fa[i].first);
                    if (part.ctx.fald->lastPartial) partialRuns++;
                }
                const int expect = coreMode == FALD_CORE_MODE_KC3 ? (int)(sizeof(steps) / sizeof(steps[0])) : 0;
                CHECK_MESSAGE(partialRuns == expect, "core mode " << coreMode << " temporal " << temporal << " star " << star
                              << ": " << partialRuns << " partial runs, expected " << expect
                              << (coreMode == FALD_CORE_MODE_KC3 ? " (KC3 is stateless: every step after the first)" : " (KC2c's IIRs: the full path)"));
            }
    g_faldCore2TestRefreshes = savedRefreshes;
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
    CHECK(fails([](FaldChangeInputs& x) { x.coreStateful = true; }));   // the always-on core with zone IIRs (KC2c): nothing stands
    // a kick-only present (no new content) is partial like any other small change
    { FaldChangeInputs x = in; x.newContent = false; x.touchedZones = 1; CHECK(FaldChangeDecide(x, cap) == FALD_CHANGE_PARTIAL); }
    { FaldChangeInputs x = in; x.touchedZones = 0; CHECK(FaldChangeDecide(x, cap) == FALD_CHANGE_PARTIAL); }
    // LED lag: every known mode is partial-able (its passes compare their outputs on the GPU and flag a movement)
    for (unsigned int mode : { 1u, 2u, 3u }) { FaldChangeInputs x = in; x.temporalMode = mode; CHECK(FaldChangeDecide(x, cap) == FALD_CHANGE_PARTIAL); }
}

namespace {
// every pixel of a half frame scaled (alpha untouched): "the tone map's peak moved"
void ChScaleAll(std::vector<uint16_t>& f, float k) {
    for (size_t i = 0; i < f.size(); i++) {
        if (i % 4 == 3) continue;
        const uint16_t h = f[i];
        const uint32_t s = (h >> 15) & 1u, e = (h >> 10) & 0x1Fu, m = h & 0x3FFu;
        float v = e == 0 ? (float)m * (1.0f / 16777216.0f) : (e == 31 ? 0.0f : std::ldexp(1.0f + (float)m / 1024.0f, (int)e - 15));
        if (s) v = -v;
        v *= k;
        uint16_t o = 0;
        if (v != 0.0f) {
            const float av = v < 0 ? -v : v;
            int ex; const float fr = std::frexp(av, &ex);
            const int e2 = ex - 1 + 15;
            if (e2 >= 1 && e2 <= 30) o = (uint16_t)((v < 0 ? 0x8000u : 0u) | ((uint32_t)e2 << 10) | ((uint32_t)((fr * 2.0f - 1.0f) * 1024.0f + 0.5f) & 0x3FFu));
        }
        f[i] = o;
    }
}
}  // namespace

// The dynamic tone map's peak in the overlay (fald.h FaldRunPasses frameChanged; shared/peak_detect.h): the pass that
// makes the intermediate is the same function between two runs unless the smoothed peak MOVED, which the smoothing pass
// decides on the GPU and writes as (4, moved, 0, 0). The layer copies `moved` into its change flags: set, every zone
// counts as changed and the sweeps run in full; clear, the rects' zones alone. The full-path context and the
// change-driven one run the same frames (the whole frame scaled when the "peak" moved, a rect edited otherwise) and must
// agree field for field; without the word (the negative control) a whole-frame change under a small rect is wrong.
TEST_CASE("FALD change: the frame-changed word (the dynamic peak) keeps the partial path exact when the whole frame moved (WARP; FALD_TEST_FADE_DIR)") {
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
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = 16; bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_DRAWINDIRECT_ARGS | D3D11_RESOURCE_MISC_BUFFER_ALLOW_RAW_VIEWS;
    const uint32_t init[4] = { 4u, 0u, 0u, 0u };
    D3D11_SUBRESOURCE_DATA sd = { init, 0, 0 };
    ID3D11Buffer* word = nullptr;
    REQUIRE(SUCCEEDED(w.dev->CreateBuffer(&bd, &sd, &word)));
    struct Step { FaldChangeRect rc; uint16_t value; float scale; const char* what; };
    const Step steps[] = {
        { { 1000, 1500, 1040, 1530 }, 0x0400, 1.0f, "a dark patch, the pass as it was" },
        { { 1000, 1500, 1040, 1530 }, 0x0400, 0.9f, "the same rect, the pass 0.9 x on the whole frame (the peak moved)" },
        { { 0, 0, 1, 1 }, 0x0000, 0.9f, "a kick pixel, the pass as it was" },
        { { 300, 300, 500, 500 }, 0x3C00, 1.1f, "a white patch and the pass 1.1 x (moved)" },
        { { 2000, 1000, 2002, 1002 }, 0x3C00, 1.1f, "a speck, the pass as it was" },
        { { 2000, 1000, 2002, 1002 }, 0x3C00, 0.8f, "the same speck, the pass 0.8 x (moved)" },
    };
    const int n = (int)(sizeof(steps) / sizeof(steps[0]));
    for (int star = 0; star < 2; star++) {
        ChLayer full(w, C, W, H, star != 0, 0, 0);
        ChLayer part(w, C, W, H, star != 0, 0, -1);
        ChLayer bare(w, C, W, H, star != 0, 0, -1);   // the negative control: the partial path without the word
        REQUIRE(full.ok); REQUIRE(part.ok); REQUIRE(bare.ok);
        std::vector<uint16_t> composed = base, passed = base;
        unsigned long long key = 7000;
        auto a0 = full.Run(w, passed, key, true, nullptr);
        auto b0 = part.Run(w, passed, key, true, nullptr);
        auto c0 = bare.Run(w, passed, key, true, nullptr);
        REQUIRE(a0 == b0); REQUIRE(a0 == c0);
        float prevScale = 1.0f;
        int partialRuns = 0, movedSteps = 0;
        for (int i = 0; i < n; i++) {
            const Step& s = steps[i];
            ChPatch(composed, W, s.rc, s.value);
            passed = composed;
            if (s.scale != 1.0f) ChScaleAll(passed, s.scale);
            const bool moved = s.scale != prevScale;
            prevScale = s.scale;
            const uint32_t movedWord[4] = { 4u, moved ? 1u : 0u, 0u, 0u };
            w.ic->UpdateSubresource(word, 0, nullptr, movedWord, 0, 0);
            key++;
            std::vector<FaldChangeRect> rects = { s.rc };
            auto fa = full.Run(w, passed, key, true, nullptr);
            auto pb = part.Run(w, passed, key, true, &rects, word);
            auto cb = bare.Run(w, passed, key, true, &rects, nullptr);
            const bool same = fa == pb;
            CHECK_MESSAGE(same, "star " << star << ": " << std::string(s.what) << ": the partial path with the word differs from the full one");
            if (!same)
                for (size_t j = 0; j < fa.size() && j < pb.size(); j++)
                    if (fa[j] != pb[j]) MESSAGE("  differs: " << fa[j].first);
            CHECK_MESSAGE(part.ctx.fald->lastPartial, "star " << star << ": " << std::string(s.what) << ": should be partial");
            if (part.ctx.fald->lastPartial) partialRuns++;
            if (moved) {
                movedSteps++;
                CHECK_MESSAGE(cb != fa, "star " << star << ": " << std::string(s.what) << ": WITHOUT the word the partial path should be wrong (the negative control)");
            }
        }
        MESSAGE("star " << star << ": " << partialRuns << " of " << n << " runs partial with the word, exact on all; the whole frame redone on "
                << movedSteps << " of them by the GPU's own decision");
    }
    word->Release();
}

namespace {
// The REAL dynamic-peak detector (shared/peak_detect.h: the two passes, the per-monitor state, the draw arguments) on the
// test's WARP device, as the hosts run it on the composed frame before their tone-map pass.
struct ChPeak {
    ID3D11ComputeShader* reduceCS = nullptr; ID3D11ComputeShader* smoothCS = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11Texture2D* peakTex = nullptr; ID3D11UnorderedAccessView* peakUAV = nullptr;
    ID3D11Texture2D* rawTex = nullptr; ID3D11UnorderedAccessView* rawUAV = nullptr;
    ID3D11Buffer* argsBuf = nullptr; ID3D11UnorderedAccessView* argsUAV = nullptr;
    ID3D11Texture2D* staging = nullptr; ID3D11Buffer* argsStaging = nullptr;
    ID3D11Texture2D* frameTex = nullptr; ID3D11ShaderResourceView* frameSRV = nullptr;
    bool ok = false;
    ChPeak(ChWarp& w, int W, int H) {
        auto compile = [&](const char* src, const char* name, ID3D11ComputeShader** out) {
            ID3DBlob* b = nullptr;
            if (FAILED(D3DCompile(src, std::strlen(src), name, nullptr, nullptr, "main", "cs_5_0", 0, 0, &b, nullptr))) return false;
            const HRESULT hr = w.dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, out);
            b->Release();
            return SUCCEEDED(hr);
        };
        if (!compile(g_peakReduceCSSource, "PeakReduceCS", &reduceCS) || !compile(g_peakSmoothCSSource, "PeakSmoothCS", &smoothCS)) return;
        D3D11_BUFFER_DESC cbd = {};
        cbd.ByteWidth = sizeof(PeakParams); cbd.Usage = D3D11_USAGE_DYNAMIC; cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        if (FAILED(w.dev->CreateBuffer(&cbd, nullptr, &cb))) return;
        D3D11_TEXTURE2D_DESC pd = {};
        pd.Width = 1; pd.Height = 1; pd.MipLevels = 1; pd.ArraySize = 1; pd.Format = DXGI_FORMAT_R32_FLOAT; pd.SampleDesc.Count = 1;
        pd.Usage = D3D11_USAGE_DEFAULT; pd.BindFlags = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
        const float zero = 0.0f;
        D3D11_SUBRESOURCE_DATA init = { &zero, sizeof(float), 0 };
        if (FAILED(w.dev->CreateTexture2D(&pd, &init, &peakTex)) || FAILED(w.dev->CreateUnorderedAccessView(peakTex, nullptr, &peakUAV))) return;
        if (FAILED(CreatePeakRawTexture(w.dev, w.ic, &rawTex, &rawUAV))) return;
        if (FAILED(CreatePeakArgsBuffer(w.dev, &argsBuf, &argsUAV))) return;
        pd.Usage = D3D11_USAGE_STAGING; pd.BindFlags = 0; pd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(w.dev->CreateTexture2D(&pd, nullptr, &staging))) return;
        D3D11_BUFFER_DESC ad = {};
        ad.ByteWidth = 16; ad.Usage = D3D11_USAGE_STAGING; ad.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(w.dev->CreateBuffer(&ad, nullptr, &argsStaging))) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)W; td.Height = (UINT)H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        if (FAILED(w.dev->CreateTexture2D(&td, nullptr, &frameTex)) || FAILED(w.dev->CreateShaderResourceView(frameTex, nullptr, &frameSRV))) return;
        ok = true;
    }
    ~ChPeak() {
        auto rel = [](IUnknown* p) { if (p) p->Release(); };
        rel(frameSRV); rel(frameTex); rel(argsStaging); rel(staging); rel(argsUAV); rel(argsBuf); rel(rawUAV); rel(rawTex);
        rel(peakUAV); rel(peakTex); rel(cb); rel(smoothCS); rel(reduceCS);
    }
    // the detector on this present's composed frame; returns the smoothed PQ peak and the arguments' `moved` (read back:
    // a test, not a host)
    float Run(ChWarp& w, const std::vector<uint16_t>& composed, int W, int H, uint32_t& moved) {
        w.ic->UpdateSubresource(frameTex, 0, nullptr, composed.data(), (UINT)W * 8u, 0);
        DispatchPeakDetection(w.ic, reduceCS, smoothCS, cb, frameSRV, peakUAV, rawUAV, (unsigned)W, (unsigned)H, nullptr, argsUAV);
        w.ic->CopyResource(staging, peakTex);
        w.ic->CopyResource(argsStaging, argsBuf);
        D3D11_MAPPED_SUBRESOURCE m = {};
        float pq = 0.0f;
        if (SUCCEEDED(w.ic->Map(staging, 0, D3D11_MAP_READ, 0, &m))) { pq = *(const float*)m.pData; w.ic->Unmap(staging, 0); }
        uint32_t a[4] = {};
        if (SUCCEEDED(w.ic->Map(argsStaging, 0, D3D11_MAP_READ, 0, &m))) { std::memcpy(a, m.pData, 16); w.ic->Unmap(argsStaging, 0); }
        moved = a[1];
        return pq;
    }
};
// a stand-in for the tone map: every pixel scaled by a smooth function of the smoothed PQ peak (any ulp of the peak moves
// every pixel, as a real curve's knee would), in float, re-packed to half
void ChToneMap(const std::vector<uint16_t>& in, std::vector<uint16_t>& out, float pq) {
    const float k = 0.5f + 0.5f * pq;
    out = in;
    for (size_t i = 0; i < out.size(); i++) {
        if (i % 4 == 3) continue;
        const uint16_t h = in[i];
        const uint32_t s = (h >> 15) & 1u, e = (h >> 10) & 0x1Fu, mm = h & 0x3FFu;
        float v = e == 0 ? (float)mm * (1.0f / 16777216.0f) : (e == 31 ? 0.0f : std::ldexp(1.0f + (float)mm / 1024.0f, (int)e - 15));
        if (s) v = -v;
        v *= k;
        uint16_t o = 0;
        if (v != 0.0f) {
            const float av = v < 0 ? -v : v;
            int ex; const float fr = std::frexp(av, &ex);
            const int e2 = ex - 1 + 15;
            if (e2 >= 1 && e2 <= 30) o = (uint16_t)((v < 0 ? 0x8000u : 0u) | ((uint32_t)e2 << 10) | ((uint32_t)((fr * 2.0f - 1.0f) * 1024.0f + 0.5f) & 0x3FFu));
        }
        out[i] = o;
    }
}
}  // namespace

// A desktop with the DYNAMIC peak, end to end on WARP: the real detector runs on each present's composed frame (as the
// hosts run it on the clean copy), a stand-in tone map scales every pixel by the smoothed peak, and the layer gets the
// detector's draw arguments as its frame-changed word. The change-driven context must equal the full one on every
// present, and the share of partial presents is what a dynamic-peak desktop gets: a present whose peak stands is partial;
// the presents while the peak settles after a content change (30 % rise / 5 % fall per frame, slew-limited) are full —
// and rightly so, the tone map moves every pixel then. (Before 2026-10-22 a dynamic peak keyed 0: never partial.)
TEST_CASE("FALD change: a desktop with the dynamic peak (the real detector): the partial share, exact against the full path (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = ChEnv("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::wstring C = ChOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    const int W = 3840, H = 2160;
    std::map<std::string, std::vector<uint16_t>> frames;
    for (const char* name : { "n7_lamp", "p02_hl_black", "n8_chess" }) {
        std::vector<uint16_t>& f = frames[name];
        f.resize((size_t)W * H * 4);
        std::ifstream in(cellDir + "/" + name + ".rgba16f", std::ios::binary);
        REQUIRE(in.good());
        in.read((char*)f.data(), (std::streamsize)(f.size() * 2));
        REQUIRE((size_t)in.gcount() == f.size() * 2);
    }
    ChWarp w;
    REQUIRE(w.ok);
    ChPeak peak(w, W, H);
    REQUIRE(peak.ok);
    // (2026-10-22: the reference context ran by the rules too — the override was set at construction, which the layer does
    // not read — and at a still whose frame the peak had moved it rightly took "the same frame again": a harness bug, not
    // the layer's; a fresh third context arbitrated it)
    ChLayer full(w, C, W, H, true, 0, 0);
    ChLayer part(w, C, W, H, true, 0, -1);
    REQUIRE(full.ok); REQUIRE(part.ok);
    // the desktop: a content frame, stills (the settle re-runs / the kick), carets (a 2 x 12 rect toggling), another frame...
    struct Present { const char* frame; int stills; int carets; };
    const Present script[] = { { "n7_lamp", 12, 8 }, { "p02_hl_black", 24, 8 }, { "n8_chess", 12, 8 } };
    std::vector<uint16_t> composed, passed;
    unsigned long long key = 9000;
    int presents = 0, partialRuns = 0, movedPresents = 0, firstMoved = 0;
    const FaldChangeRect caret = { 500, 500, 502, 512 };
    auto present = [&](bool newContent, const FaldChangeRect* rc, const char* what) {
        uint32_t moved = 0;
        const float pq = peak.Run(w, composed, W, H, moved);
        static std::vector<uint16_t> prevPassed;
        ChToneMap(composed, passed, pq);
        const bool frameSame = passed == prevPassed;
        size_t frameDiffs = 0;
        if (!frameSame && prevPassed.size() == passed.size()) for (size_t t = 0; t < passed.size(); t++) if (passed[t] != prevPassed[t]) frameDiffs++;
        prevPassed = passed;
        if (newContent) key++;
        std::vector<FaldChangeRect> rects;
        if (rc) rects.push_back(*rc);
        const bool full0 = rc == nullptr && newContent;                       // a whole new frame: unknown rects
        auto fa = full.Run(w, passed, key, newContent, nullptr);
        auto pb = part.Run(w, passed, key, newContent, full0 ? nullptr : &rects, peak.argsBuf);
        const bool same = fa == pb;
        CHECK_MESSAGE(same, "present " << presents << " (" << std::string(what) << ", peak pq " << pq << ", moved " << moved << ", partial "
                      << part.ctx.fald->lastPartial << ", the frame as the previous present's " << frameSame << " (" << frameDiffs
                      << " halves differ)): the change-driven context differs from the full one");
        if (!same) {
            for (size_t j = 0; j < fa.size() && j < pb.size(); j++)
                if (fa[j] != pb[j]) MESSAGE("  differs: " << fa[j].first);
            // where: the first differing texels of the star statistic, the round-1 drive and the boost counts
            struct Pair { const char* name; ID3D11Texture2D* a; ID3D11Texture2D* b; unsigned comps; };
            const FaldResources* ra = full.ctx.fald; const FaldResources* rb = part.ctx.fald;
            const Pair pairs[] = { { "star_stat", ra->starStatTex, rb->starStatTex, 4 }, { "star_bg", ra->starBgTex, rb->starBgTex, 4 },
                                   { "drive", ra->driveTex, rb->driveTex, 1 }, { "boost_r1", ra->boostTex[1], rb->boostTex[1], 1 },
                                   { "active_r1", ra->activeTex[1], rb->activeTex[1], 1 } };
            for (const Pair& pr : pairs) {
                if (!pr.a || !pr.b) continue;
                const std::vector<uint8_t> A = ChReadBack(pr.a), B = ChReadBack(pr.b);
                if (A.size() != B.size() || A.size() % 4) { MESSAGE("  " << pr.name << ": sizes " << A.size() << " / " << B.size()); continue; }
                const float* fA = (const float*)A.data(); const float* fB = (const float*)B.data();
                size_t n = A.size() / 4, nd = 0;
                std::string first;
                for (size_t t = 0; t < n; t++) {
                    if (std::memcmp(&fA[t], &fB[t], 4) == 0) continue;
                    if (nd < 6) {
                        char buf[160];
                        snprintf(buf, sizeof(buf), " [texel %zu c%u: %.9g vs %.9g]", t / pr.comps, (unsigned)(t % pr.comps), fA[t], fB[t]);
                        first += buf;
                    }
                    nd++;
                }
                MESSAGE("  " << std::string(pr.name) << ": " << nd << " of " << n << " floats differ" << first);
            }
            auto readBuf = [&](ID3D11Buffer* b) {
                std::vector<uint32_t> out;
                if (!b) return out;
                D3D11_BUFFER_DESC d; b->GetDesc(&d);
                d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0; d.StructureByteStride = 0;
                ID3D11Buffer* st = nullptr;
                if (FAILED(g_device->CreateBuffer(&d, nullptr, &st))) return out;
                g_context->CopyResource(st, b);
                D3D11_MAPPED_SUBRESOURCE mm;
                if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &mm))) { out.assign((const uint32_t*)mm.pData, (const uint32_t*)mm.pData + d.ByteWidth / 4); g_context->Unmap(st, 0); }
                st->Release();
                return out;
            };
            {   // the arbiter: a fresh context with no history, one full run on this frame — which of the two does it agree with?
                ChLayer fresh(w, C, W, H, true, 0, 0);
                REQUIRE(fresh.ok);
                auto fr = fresh.Run(w, passed, key, true, nullptr);
                MESSAGE("  a fresh context's full run equals every texture of: the full context " << (int)(fr == fa) << ", the change-driven context " << (int)(fr == pb));
            }
            // did the word arrive? the decide pass's arguments after the run (slot 240 = S0 over the whole lattice: (cols,
            // rows, slices) iff flag [3] was set; 32 = round 0's full sweep) and the detector's own arguments
            const std::vector<uint32_t> args = readBuf(part.ctx.fald->argsBuf), word = readBuf(peak.argsBuf);
            if (args.size() >= 68 && word.size() >= 4)
                MESSAGE("  args: S0 full (" << args[60] << "," << args[61] << "," << args[62] << ") stat0 full (" << args[8] << "," << args[9] << "," << args[10]
                        << ") stat0 part (" << args[16] << "," << args[17] << "," << args[18] << ") conv0 (" << args[24] << "," << args[25] << ") pixel ("
                        << args[56] << "," << args[57] << "); the detector's word (" << word[0] << "," << word[1] << "," << word[2] << "," << word[3] << ")");
        }
        presents++;
        if (part.ctx.fald->lastPartial) partialRuns++;
        if (moved) movedPresents++;
        if (moved && presents > 1 && firstMoved == 0) firstMoved = presents;
    };
    for (const Present& p : script) {
        composed = frames[p.frame];
        present(true, nullptr, p.frame);
        for (int i = 0; i < p.stills; i++) present(false, nullptr, "still");
        for (int i = 0; i < p.carets; i++) {
            ChPatch(composed, W, caret, (i & 1) ? 0x3C00 : 0x0000);
            present(true, &caret, "caret");
        }
    }
    MESSAGE(partialRuns << " of " << presents << " presents took the change-driven path with the dynamic peak (the peak moved on "
            << movedPresents << " of them: the content frames and the frames while it settled after each)");
    CHECK(partialRuns >= 20);   // at least n7_lamp's stills and carets once its peak stands (the rise settles within ~30 frames)
}
