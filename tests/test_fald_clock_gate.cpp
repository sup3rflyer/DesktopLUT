// The panel clock (LED lag mode 3, shared/fald_temporal.h) run for run, as a hash manifest: every field texture, the
// clock's state textures and the output of a SCHEDULED sequence of runs — content frames, stills (the settle runs of a
// static desktop), small edits inside a rect — on the owner's panel file, recorded on main (FALD_TEST_CLOCK_RECORD=<file>)
// and checked on a branch (FALD_TEST_CLOCK_REF=<file>): the branch's full path must reproduce main's bytes, run for run.
// (2026-10-22: the core-speed branch routed the clock pass's B_true map into round 0's filtered slot while round 1's
// convolution read round 1's — a map nothing writes in mode 3 — so the final field came from a stale map, and no gate
// covered mode 3 end to end: the OFF manifest's LED-lag sequence is mode 2, the change gate runs modes 0 and 2, the DLC
// replay of the mode-3 WARP dumps is opt-in and was skipped.)
//
// The clock's k (the refreshes elapsed between two runs) comes from the wall clock, so the runs are SCHEDULED: the
// monitor's nominal period is set long (FALD_TEST_CLOCK_PERIOD_MS, default 2000: longer than a 4K run on WARP with its
// readback), run n starts at t0 + N_n * period (N_n = the sum of the k targets so far), and the k each run saw goes into
// the manifest — a slipped schedule shows as a mismatch of its own line, never as a false field mismatch. A gap longer
// than FALD_RESUME_GAP_MS only re-arms the settle hold (bookkeeping, no GPU work), so the long period changes nothing
// the textures see: the blend factors depend on k and the parity alone.
//
// With the change-driven path built (FALD_RUN_HAS_RECTS) a second context runs the same schedule half a period later
// by the path's rules with the edits' rects, and every texture of the two (the branch's round-0 maps included) is compared
// run for run; from the second clock run on, every run must take the partial path (the first clock run after the seed
// changes the kernels' map routing and is full by the rule in FaldRunPasses).
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_temporal.h"
#include <d3dcompiler.h>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace {

std::string CkEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

struct CkWarp {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr; ID3D11VertexShader* vs = nullptr; bool ok = false;
    CkWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        static const char* vsSrc = "struct O { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };"
                                   "O main(uint id : SV_VertexID) { O o; o.uv = float2((id << 1) & 2, id & 2);"
                                   "o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1); return o; }";
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(vsSrc, std::strlen(vsSrc), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~CkWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

std::vector<uint8_t> CkReadBack(ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    if (!tex) return out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    UINT bpp = 0;
    switch (d.Format) {
    case DXGI_FORMAT_R32_FLOAT: case DXGI_FORMAT_R32_UINT: bpp = 4; break;
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
std::string CkFnvLine(const std::vector<uint8_t>& b) {
    uint64_t h = 1469598103934665603ull;
    for (uint8_t c : b) { h ^= c; h *= 1099511628211ull; }
    char buf[64];
    snprintf(buf, sizeof(buf), "%zu %016llx", b.size(), (unsigned long long)h);
    return buf;
}
std::wstring CkOwnerPanel(const wchar_t* rel) {
    const std::wstring p = std::wstring(L"H:\\Projects\\DesktopLUT\\") + rel;
    return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES ? p : std::wstring();
}
bool CkReadFrame(const std::string& path, std::vector<uint16_t>& out, int W, int H) {
    out.assign((size_t)W * H * 4, 0);
    std::ifstream f(path, std::ios::binary);
    if (!f.good()) return false;
    f.read((char*)out.data(), (std::streamsize)(out.size() * 2));
    return (size_t)f.gcount() == out.size() * 2;
}
struct CkRect { int left, top, right, bottom; };
void CkPatch(std::vector<uint16_t>& f, int W, const CkRect& rc, uint16_t value) {
    for (int y = rc.top; y < rc.bottom; y++)
        for (int x = rc.left; x < rc.right; x++)
            for (int c = 0; c < 3; c++) f[((size_t)y * W + x) * 4 + c] = value;
}
long long CkQpc() { LARGE_INTEGER t; QueryPerformanceCounter(&t); return t.QuadPart; }
long long CkQpcFreq() { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f.QuadPart; }
void CkWaitUntil(long long targetQpc) {
    const long long f = CkQpcFreq();
    for (;;) {
        const long long now = CkQpc();
        if (now >= targetQpc) return;
        const double ms = (double)(targetQpc - now) * 1000.0 / (double)f;
        if (ms > 3.0) Sleep(1); else YieldProcessor();
    }
}

// One layer context on its own monitor context + target, temporal mode 3. partialOverride (the branch only): the path
// override applied at EVERY run — 0 = the full path (the reference), -1 = the change-driven path by its rules.
struct CkLayer {
    MonitorContext ctx;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    bool ok = false;
    int partialOverride = 0;
    CkLayer(CkWarp& w, const std::wstring& panel, int fw, int fh, bool star, int parity, float periodMs, int partialOverrideAtRun = 0)
        : partialOverride(partialOverrideAtRun) {
        ctx.width = fw; ctx.height = fh; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = periodMs;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = star;
        fs.temporalMode = FALD_TEMPORAL_PANEL; fs.clockParity = parity; fs.clockClosure = FALD_CLOCK_CLOSURE_DEFAULT;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)fw; td.Height = (UINT)fh; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(w.dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(w.dev->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~CkLayer() { if (rtv) rtv->Release(); if (target) target->Release(); FaldReleaseResources(&ctx); }
    // the frame uploaded whole, the layer run (rects: the change-driven path's, built only with FALD_RUN_HAS_RECTS; numRects
    // < 0 = unknown), the hashes of the textures: the ones main has (CkIsCommon) and, on the branch, its round-0 maps too
    typedef std::vector<std::pair<std::string, std::string>> Hashes;
    Hashes Run(CkWarp& w, const std::vector<uint16_t>& frame, unsigned long long key, bool newContent, const CkRect* rects, int numRects) {
        D3D11_VIEWPORT vp = { 0, 0, (float)ctx.width, (float)ctx.height, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)ctx.width * 8u, 0);
#ifdef FALD_RUN_HAS_RECTS
        std::vector<FaldChangeRect> rc;
        for (int i = 0; i < numRects; i++) rc.push_back({ rects[i].left, rects[i].top, rects[i].right, rects[i].bottom });
        g_faldPartialOverride = partialOverride;
        FaldRunPasses(&ctx, rtv, newContent, key, numRects >= 0 ? rc.data() : nullptr, numRects);
        g_faldPartialOverride = -1;
#else
        (void)rects; (void)numRects;
        FaldRunPasses(&ctx, rtv, newContent, key);
#endif
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        std::vector<std::pair<const char*, ID3D11Texture2D*>> tex = {
            { "drive", r->driveTex }, { "drive_filt", r->driveFiltTex }, { "drive_state", r->driveStateTex },
            { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] }, { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] },
            { "btrue", r->bTrueTex }, { "best", r->bEstTex }, { "gainB", r->gainBTex },
            { "star_stat", r->starStatTex }, { "star_w", r->starWTex }, { "star_plan", r->starPlanTex }, { "star_plan2", r->starPlan2Tex },
            { "star_bg", r->starBgTex },
            { "clk_state0", r->clkStateTex[0] }, { "clk_state1", r->clkStateTex[1] }, { "clk_prev", r->clkPrevTex }, { "clk_est", r->clkEstTex },
            { "OUTPUT", target } };
#ifdef FALD_RUN_HAS_RECTS
        const std::pair<const char*, ID3D11Texture2D*> more[] = {   // the branch's round-0 maps (full vs partial only)
            { "drive0", r->drive0Tex }, { "drive_filt0", r->driveFilt0Tex }, { "btrue0", r->bTrue0Tex }, { "best0", r->bEst0Tex },
            { "gain0", r->gain0Tex }, { "fields0", r->fields0Tex }, { "fields", r->fieldsTex } };
        for (const auto& t : more) tex.push_back(t);
#endif
        Hashes out;
        for (const auto& t : tex) if (t.second) out.emplace_back(t.first, CkFnvLine(CkReadBack(t.second)));
        return out;
    }
};
// the textures main 4207200 has (the manifest's lines); the branch's round-0 maps are compared between its two contexts only
bool CkIsCommon(const std::string& name) {
    static const char* const common[] = { "drive", "drive_filt", "drive_state", "active_r0", "active_r1", "boost_r0", "boost_r1", "btrue", "best",
                                          "gainB", "star_stat", "star_w", "star_plan", "star_plan2", "star_bg", "clk_state0", "clk_state1",
                                          "clk_prev", "clk_est", "OUTPUT" };
    for (const char* c : common) if (name == c) return true;
    return false;
}

struct CkStep { int k; const char* frame; CkRect rc; uint16_t value; bool newContent; const char* what; };
// the owner's situation: content frames, the settle runs on a static desktop, small edits (a dirty rect) — with k = 1
// (one clock ticks), 2 (both tick once) and 3 between runs
const CkStep kSteps[] = {
    { 0, "n7_lamp", { 0, 0, 0, 0 }, 0, true, "the first frame: the seeding run" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still (settle)" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still (settle)" },
    { 2, "n8_chess", { 0, 0, 0, 0 }, 0, true, "a new frame" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still" },
    { 1, nullptr, { 1000, 1500, 1040, 1530 }, 0x0400, true, "a dark 40 x 30 patch (no drive moves)" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still" },
    { 3, "p02_hl_black", { 0, 0, 0, 0 }, 0, true, "a new frame after three refreshes" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still" },
    { 2, nullptr, { 0, 0, 0, 0 }, 0, false, "still after two refreshes" },
    { 1, nullptr, { 300, 300, 500, 500 }, 0x3C00, true, "a 200 x 200 white patch (drives move)" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still" },
    { 1, "n9_cactus", { 0, 0, 0, 0 }, 0, true, "a new frame" },
    { 1, nullptr, { 2000, 1000, 2002, 1002 }, 0x3C00, true, "a 2 x 2 white speck" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still" },
    { 1, nullptr, { 0, 0, 0, 0 }, 0, false, "still" },
};
const int kStepCount = (int)(sizeof(kSteps) / sizeof(kSteps[0]));

}  // namespace

TEST_CASE("FALD panel clock gate: a scheduled mode-3 sequence hashes as on main, run for run (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = CkEnv("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::string record = CkEnv("FALD_TEST_CLOCK_RECORD"), ref = CkEnv("FALD_TEST_CLOCK_REF");
    const std::string periodEnv = CkEnv("FALD_TEST_CLOCK_PERIOD_MS");
    const float periodMs = periodEnv.empty() ? 2000.0f : (float)std::atof(periodEnv.c_str());
    REQUIRE(periodMs >= 100.0f);
    const std::wstring C = CkOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    const int W = 3840, H = 2160;
    std::map<std::string, std::vector<uint16_t>> frames;
    for (const CkStep& s : kSteps)
        if (s.frame && !frames.count(s.frame)) REQUIRE_MESSAGE(CkReadFrame(cellDir + "/" + s.frame + ".rgba16f", frames[s.frame], W, H), s.frame);
    CkWarp w;
    REQUIRE(w.ok);
    const long long f = CkQpcFreq();
    const long long periodQpc = (long long)((double)periodMs * (double)f / 1000.0);
    std::map<std::string, std::string> man;   // "<cfg>/<step> <field>" -> "<bytes> <fnv>"; "<cfg>/<step> k" -> "<k> <seeded>"
    struct Cfg { bool star; int parity; const char* name; };
    const Cfg cfgs[] = { { true, -1, "star1.par-1" }, { false, 0, "star0.par0" } };
    for (const Cfg& cfg : cfgs) {
        CkLayer full(w, C, W, H, cfg.star, cfg.parity, periodMs, 0);    // the reference: the full path at every run
        REQUIRE(full.ok);
#ifdef FALD_RUN_HAS_RECTS
        CkLayer part(w, C, W, H, cfg.star, cfg.parity, periodMs, -1);   // the change-driven path by its rules
        REQUIRE(part.ok);
        int partialRuns = 0, clockRuns = 0;
#endif
        std::vector<uint16_t> frame;
        unsigned long long key = 5000;
        long long N = 0;
        const long long t0 = CkQpc() + periodQpc / 4;   // the first run a quarter period from now
        for (int i = 0; i < kStepCount; i++) {
            const CkStep& s = kSteps[i];
            CkRect rc = s.rc;
            int numRects = 0;
            if (s.frame) { frame = frames[s.frame]; rc = { 0, 0, W, H }; numRects = 1; key++; }
            else if (s.newContent) { CkPatch(frame, W, s.rc, s.value); numRects = 1; key++; }
            N += s.k;
            const std::string id = std::string(cfg.name) + "/" + std::to_string(i);
            // the full context on the schedule
            CkWaitUntil(t0 + N * periodQpc);
            const CkLayer::Hashes fa = full.Run(w, frame, key, s.newContent, &rc, i == 0 ? -1 : numRects);
            {
                const FaldResources* r = full.ctx.fald;
                const bool seedOk = i == 0 ? r->clkSeeded : !r->clkSeeded;
                CHECK_MESSAGE(seedOk, id << " (" << std::string(s.what) << "): seeded " << r->clkSeeded);
                if (i > 0) CHECK_MESSAGE(r->clkElapsed == (unsigned long long)s.k, id << " (" << std::string(s.what) << "): k " << r->clkElapsed << " (target " << s.k << ": the schedule slipped - rerun)");
                man[id + " k"] = std::to_string(i == 0 ? 0 : (int)r->clkElapsed) + " " + (r->clkSeeded ? "1" : "0");
                for (const auto& kv : fa) if (CkIsCommon(kv.first)) man[id + " " + kv.first] = kv.second;
            }
#ifdef FALD_RUN_HAS_RECTS
            // the change-driven context half a period later, by its rules, with the edit's rect (the first run: unknown)
            CkWaitUntil(t0 + N * periodQpc + periodQpc / 2);
            const CkLayer::Hashes pb = part.Run(w, frame, key, s.newContent, &rc, i == 0 ? -1 : numRects);
            {
                const FaldResources* rp = part.ctx.fald;
                CHECK_MESSAGE(rp->clkElapsed == full.ctx.fald->clkElapsed, id << ": the two contexts saw different k");
                const bool same = fa == pb;
                CHECK_MESSAGE(same, id << " (" << std::string(s.what) << "): the change-driven context differs from the full one");
                if (!same)
                    for (size_t j = 0; j < fa.size() && j < pb.size(); j++)
                        if (fa[j] != pb[j]) MESSAGE("  differs: " << fa[j].first);
                // the seed and the first clock run after it (the routing rule) are full, as is a whole new frame (the cap)
                const bool expectPartial = i >= 2 && s.frame == nullptr;
                CHECK_MESSAGE(rp->lastPartial == expectPartial, id << " (" << std::string(s.what) << "): partial " << rp->lastPartial << ", expected " << expectPartial);
                if (rp->lastPartial) partialRuns++;
                if (!rp->clkSeeded && rp->clkElapsed >= 1ull) clockRuns++;
            }
#endif
        }
#ifdef FALD_RUN_HAS_RECTS
        MESSAGE(std::string(cfg.name) << ": " << partialRuns << " of " << kStepCount << " runs partial, " << clockRuns << " clock runs");
#endif
    }
    MESSAGE(man.size() << " manifest lines");
    if (!record.empty()) {
        std::ofstream o(record);
        REQUIRE(o.good());
        for (const auto& kv : man) o << kv.first << " " << kv.second << "\n";
    }
    if (!ref.empty()) {
        std::map<std::string, std::string> want;
        std::ifstream in(ref);
        REQUIRE(in.good());
        std::string line;
        while (std::getline(in, line)) {
            std::istringstream is(line);
            std::string cfgStep, field, a, b;
            if (is >> cfgStep >> field >> a >> b) want[cfgStep + " " + field] = a + " " + b;
        }
        REQUIRE(want.size() == man.size());
        size_t bad = 0;
        for (const auto& kv : man) {
            auto it = want.find(kv.first);
            const bool same = it != want.end() && it->second == kv.second;
            if (!same && bad++ < 40) MESSAGE("MISMATCH " << kv.first << ": " << kv.second << " vs " << (it == want.end() ? "absent" : it->second));
        }
        CHECK(bad == 0u);
    }
}
