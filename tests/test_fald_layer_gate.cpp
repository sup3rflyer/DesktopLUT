// The FALD layer's WARP identity gate: every field the overlay's layer leaves behind and the output frame, hashed (FNV-1a
// 64) for a fixed matrix of runs — the still cells x the owner's PA32 panel files (HDR + SDR) x starfield on / off, the
// debug views, a multi-frame sequence and an odd-sized frame on its own panel file — recorded by one build and checked by
// another, line for line. FALD_TEST_CORE_MODE = the always-on core's mode for every run (absent = 0 = C, the core off:
// the matrix the 2026-10-20 manifest tests/fixtures/fald_fade_off_main.hashes was recorded with; 4 = KC5, the default).
// (Until 2026-11-12 this case lived in tests/test_fald_fade.cpp as the flank fade's OFF gate: with the fade gone it is the
// layer's own before / after gate for any change that must not move the picture.)
//
// FALD_TEST_FADE_DIR = the cell directory (cells.txt + <name>.rgba16f + odd_panel.bin; DLC tests/fald_fade_cells.py wrote
// it from the study's cached frames; the variable keeps its historical name, every opt-in WARP case reads it).
// FALD_TEST_LAYER_RECORD=<file> writes the manifest; FALD_TEST_LAYER_REF=<file> checks it: every line both have must be
// equal; lines only one side has are counted and listed (a removed texture, a run the other build's matrix lacks), and
// fail the case only with FALD_TEST_LAYER_STRICT=1. Without FALD_TEST_FADE_DIR the cases check nothing (opt-in).
// FALD_TEST_LAYER_DISASM=<dir> writes the disassembly of every layer pass as InitFaldShaders compiles it (a second way to
// compare two builds: an unchanged pass compiles to the same instructions).
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_core2.h"       // the always-on core: the mode (FALD_TEST_CORE_MODE) and its test seam
#include <d3dcompiler.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace {

std::string LgEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

std::wstring LgWiden(const std::string& s) { return std::wstring(s.begin(), s.end()); }   // ASCII paths (a test convenience)

// The WARP device as the process device (g_device / g_context, as the overlay's InitD3D sets them) + the fullscreen VS.
struct LgWarp {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11VertexShader* vs = nullptr;
    bool ok = false;
    LgWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main",
                              "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~LgWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

std::vector<uint8_t> LgReadBack(ID3D11Texture2D* tex) {
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

uint64_t LgFnv(const std::vector<uint8_t>& b) {
    uint64_t h = 1469598103934665603ull;
    for (uint8_t c : b) { h ^= c; h *= 1099511628211ull; }
    return h;
}

struct LgCell { std::string name; int w = 0, h = 0; };

std::vector<LgCell> LgReadCells(const std::string& dir) {
    std::vector<LgCell> out;
    std::ifstream f(dir + "/cells.txt");
    std::string line;
    while (std::getline(f, line)) {
        std::istringstream is(line);
        LgCell c;
        if (is >> c.name >> c.w >> c.h) out.push_back(c);
    }
    return out;
}

bool LgReadFrame(const std::string& path, int w, int h, std::vector<uint16_t>& out) {
    out.assign((size_t)w * h * 4, 0);
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    f.read((char*)out.data(), (std::streamsize)(out.size() * sizeof(uint16_t)));
    return (size_t)f.gcount() == out.size() * sizeof(uint16_t);
}

// The owner's panel files (local data, not in the repo): beside this checkout's DLC, or in the main checkout's.
std::wstring LgOwnerPanel(const wchar_t* rel) {
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir = exe;
    for (int up = 0; up < 7; up++) {
        const size_t slash = dir.find_last_of(L"\\/");
        if (slash == std::wstring::npos) break;
        dir.resize(slash);
        const std::wstring p = dir + L"\\" + rel;
        if (GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES) return p;
    }
    const std::wstring p = std::wstring(L"H:\\Projects\\DesktopLUT\\") + rel;
    return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES ? p : std::wstring();
}

// One run's settings (everything else at the FaldSettings defaults).
struct LgRunSpec {
    std::string id;
    std::wstring panel;
    bool hdr = true;
    std::vector<std::string> frames;    // cell names, run in order on ONE monitor context (one frame = a still)
    int w = 0, h = 0;
    bool star = false;
    unsigned int debug = 0;
};

using LgManifest = std::vector<std::pair<std::string, std::string>>;   // "<run> <field>" -> "<bytes> <fnv>"

// Runs one spec on WARP and appends "<id>/<frame index> <field>" -> "<bytes> <fnv hex>" for every field + the output.
bool LgRunHashes(LgWarp& w, const std::string& cellDir, const LgRunSpec& rs, LgManifest& man, std::string& err) {
    MonitorContext ctx;
    ctx.width = rs.w; ctx.height = rs.h;
    ctx.isHDREnabled = rs.hdr; ctx.isFP16SDR = !rs.hdr;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    ctx.frameTimeExactMs = 16.667f;
    FaldSettings& fs = rs.hdr ? ctx.hdrColorCorrection.fald : ctx.sdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = rs.panel; fs.debugMode = rs.debug; fs.star.enabled = rs.star;
    fs.coreMode = (uint32_t)atoi(LgEnv("FALD_TEST_CORE_MODE").c_str());
    if (!FaldEnsureResources(&ctx, fs)) {
        err = ctx.fald ? ctx.fald->lastError : std::string("no resources");
        FaldReleaseResources(&ctx);
        return false;
    }
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = (UINT)rs.w; td.Height = (UINT)rs.h; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    if (FAILED(w.dev->CreateTexture2D(&td, nullptr, &target)) || FAILED(w.dev->CreateRenderTargetView(target, nullptr, &rtv))) {
        err = "target";
        if (target) target->Release();
        FaldReleaseResources(&ctx);
        return false;
    }
    std::vector<uint16_t> frame;
    for (size_t i = 0; i < rs.frames.size(); i++) {
        if (!LgReadFrame(cellDir + "/" + rs.frames[i] + ".rgba16f", rs.w, rs.h, frame)) { err = "frame " + rs.frames[i]; break; }
        D3D11_VIEWPORT vp = { 0, 0, (float)rs.w, (float)rs.h, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)rs.w * 8u, 0);
        FaldRunPasses(&ctx, rtv, true);
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        const std::pair<const char*, ID3D11Texture2D*> tex[] = {
            { "drive", r->driveTex }, { "drive_filt", r->driveFiltTex },
            { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] },
            { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] }, { "btrue", r->bTrueTex }, { "best", r->bEstTex },
            { "flat_btrue", r->flatTrueTex }, { "flat_best", r->flatEstTex },
            // the final (gain, ceiling) map: the fused gain + blur pass writes it into gainB alone, so "gainA" names the
            // same final map then (the 2026-10-20 manifest's lines stay comparable)
            { "gainA", r->gainFused ? r->gainBTex : r->gainATex }, { "gainB", r->gainBTex },
            { "star_stat", r->starStatTex }, { "star_w", r->starWTex }, { "star_plan", r->starPlanTex },
            { "star_plan2", r->starPlan2Tex }, { "star_bg", r->starBgTex } };
        const std::string key = rs.id + "/" + std::to_string(i);
        char buf[64];
        for (const auto& t : tex) {
            const std::vector<uint8_t> b = LgReadBack(t.second);
            snprintf(buf, sizeof(buf), "%zu %016llx", b.size(), (unsigned long long)LgFnv(b));
            man.emplace_back(key + " " + t.first, buf);
        }
        const std::vector<uint8_t> out = LgReadBack(target);
        snprintf(buf, sizeof(buf), "%zu %016llx", out.size(), (unsigned long long)LgFnv(out));
        man.emplace_back(key + " OUTPUT", buf);
    }
    rtv->Release(); target->Release();
    FaldReleaseResources(&ctx);
    return err.empty();
}

// The gate's run matrix: fixed, so a manifest recorded by one build is checked line for line by another.
std::vector<LgRunSpec> LgMatrix(const std::vector<LgCell>& cells, const std::string& cellDir) {
    std::vector<LgRunSpec> out;
    const std::wstring C = LgOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    const std::wstring B07 = LgOwnerPanel(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin");
    const std::wstring SDR = LgOwnerPanel(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin");
    const std::wstring ODD = LgWiden(cellDir + "/odd_panel.bin");
    auto add = [&](const std::string& id, const std::wstring& panel, bool hdr, const std::vector<std::string>& frames, int w, int h,
                   bool star, unsigned int debug) {
        LgRunSpec r; r.id = id; r.panel = panel; r.hdr = hdr; r.frames = frames; r.w = w; r.h = h; r.star = star; r.debug = debug;
        out.push_back(r);
    };
    for (const LgCell& c : cells) {
        if (c.w == 3840 && c.h == 2160) {
            if (!C.empty()) for (int s = 0; s < 2; s++) add("C." + c.name + ".star" + std::to_string(s), C, true, { c.name }, c.w, c.h, s != 0, 0);
            if (!B07.empty()) add("B07." + c.name + ".star1", B07, true, { c.name }, c.w, c.h, true, 0);
        } else if (c.name == "x_odd1366") {
            for (int s = 0; s < 2; s++) add("ODD." + c.name + ".star" + std::to_string(s), ODD, true, { c.name }, c.w, c.h, s != 0, 0);
        }
    }
    if (!SDR.empty())
        for (const char* n : { "n7_lamp", "p07_dark_bar0", "s10_sm_seattle" })
            for (int s = 0; s < 2; s++) add(std::string("SDR.") + n + ".star" + std::to_string(s), SDR, false, { n }, 3840, 2160, s != 0, 0);
    if (!C.empty()) {
        for (unsigned int d : { 1u, 2u, 3u, 4u, 5u, 6u, 7u, 8u, 9u })
            add("C.n7_lamp.star1.debug" + std::to_string(d), C, true, { "n7_lamp" }, 3840, 2160, true, d);
        // five content frames in order on one context, LED lag off (the panel clock's own sequence: test_fald_clock_gate.cpp)
        add("C.seq.mode0", C, true, { "p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus" }, 3840, 2160, true, 0);
    }
    return out;
}

bool LgWriteText(const std::string& path, const std::string& text) {
    std::ofstream f(path, std::ios::binary);
    if (!f) return false;
    f.write(text.data(), (std::streamsize)text.size());
    return (bool)f;
}

std::map<std::string, std::string> LgReadManifest(const std::string& path) {
    std::map<std::string, std::string> out;
    std::ifstream f(path);
    std::string line;
    while (std::getline(f, line)) {
        // "<run> <field> <bytes> <fnv>": the key is the first two tokens
        std::istringstream is(line);
        std::string run, field, bytes, fnv;
        if (is >> run >> field >> bytes >> fnv) out[run + " " + field] = bytes + " " + fnv;
    }
    return out;
}

}  // namespace

TEST_CASE("FALD layer gate: every field and the output hash as the reference build's (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = LgEnv("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::string record = LgEnv("FALD_TEST_LAYER_RECORD"), ref = LgEnv("FALD_TEST_LAYER_REF");
    const bool strict = LgEnv("FALD_TEST_LAYER_STRICT") == "1";
    // with a stateful core mode the IIR advances exactly one refresh per run whatever WARP's timing (the test seam), so two
    // builds hash the same sequence (KC5 and C are stateless: the seam changes nothing there)
    const unsigned long long savedRefreshes = g_faldCore2TestRefreshes;
    g_faldCore2TestRefreshes = atoi(LgEnv("FALD_TEST_CORE_MODE").c_str()) > 0 ? 1ull : 0ull;
    const std::vector<LgCell> cells = LgReadCells(cellDir);
    REQUIRE_FALSE(cells.empty());
    LgWarp w;
    REQUIRE(w.ok);
    const std::vector<LgRunSpec> runs = LgMatrix(cells, cellDir);
    REQUIRE_FALSE(runs.empty());
    LgManifest man;
    const auto t0 = std::chrono::steady_clock::now();
    for (const LgRunSpec& rs : runs) {
        std::string err;
        const bool ok = LgRunHashes(w, cellDir, rs, man, err);
        CHECK_MESSAGE(ok, rs.id << ": " << err);
    }
    const double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    g_faldCore2TestRefreshes = savedRefreshes;
    MESSAGE(runs.size() << " runs, " << man.size() << " hashes, " << s << " s (core mode " << LgEnv("FALD_TEST_CORE_MODE") << ")");
    if (!record.empty()) {
        std::string text;
        for (const auto& kv : man) text += kv.first + " " + kv.second + "\n";
        REQUIRE(LgWriteText(record, text));
    }
    if (!ref.empty()) {
        const std::map<std::string, std::string> want = LgReadManifest(ref);
        REQUIRE_FALSE(want.empty());
        size_t bad = 0, same = 0, onlyHere = 0;
        std::map<std::string, bool> seen;
        for (const auto& kv : man) {
            auto it = want.find(kv.first);
            if (it == want.end()) { if (onlyHere++ < 20) MESSAGE("not in the reference: " << kv.first); continue; }
            seen[kv.first] = true;
            if (it->second == kv.second) same++;
            else if (bad++ < 20) MESSAGE("MISMATCH " << kv.first << ": " << kv.second << " vs " << it->second);
        }
        size_t onlyRef = 0;
        for (const auto& kv : want)
            if (!seen.count(kv.first) && onlyRef++ < 20) MESSAGE("only in the reference: " << kv.first);
        MESSAGE(same << " lines equal, " << bad << " differ, " << onlyHere << " only here, " << onlyRef << " only in the reference");
        CHECK(bad == 0u);
        CHECK(same > 0u);
        if (strict) { CHECK(onlyHere == 0u); CHECK(onlyRef == 0u); }
    }
}

// Passes 2b + 2c fused into one dispatch (shared/fald_shader.h g_faldGainBlurSource) against the three passes + copy they
// replace, on the same frame: the final (gain, ceiling) map and the output byte for byte (the gate above holds the fused
// path to main's three passes as well; this case keeps the fallback path — a sigma over FALD_GAIN_BLUR_RMAX — honest).
TEST_CASE("FALD gain + blur: the fused pass equals the three passes byte for byte (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = LgEnv("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::wstring C = LgOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    LgWarp w;
    REQUIRE(w.ok);
    std::string out[2], gain[2];
    for (int fused = 0; fused < 2; fused++) {
        g_faldGainFusedOverride = fused;
        LgRunSpec rs; rs.id = "fused"; rs.panel = C; rs.hdr = true; rs.frames = { "n7_lamp" }; rs.w = 3840; rs.h = 2160; rs.star = true;
        LgManifest man; std::string err;
        REQUIRE_MESSAGE(LgRunHashes(w, cellDir, rs, man, err), err);
        for (const auto& kv : man) { if (kv.first == "fused/0 OUTPUT") out[fused] = kv.second; if (kv.first == "fused/0 gainB") gain[fused] = kv.second; }
    }
    g_faldGainFusedOverride = -1;
    REQUIRE_FALSE(out[0].empty());
    CHECK(out[0] == out[1]);
    CHECK(gain[0] == gain[1]);
}

namespace {
// The layer's passes as InitFaldShadersFrom compiles them (src/fald.cpp CompileOne: the default flags; the DWM hook compiles
// the same sources): name -> (source, target).
struct LgPass { std::string name; std::string src; const char* target; };
std::vector<LgPass> LgLayerPasses() {
    const FaldShaderSources s = FaldDefaultShaderSources();
    const std::string common = s.common;
    std::vector<LgPass> p = {
        { "pixel", common + s.pixel, "ps_5_0" }, { "stat", common + s.stat, "cs_5_0" }, { "gain", common + s.gain, "cs_5_0" },
        { "blur", common + s.blur, "cs_5_0" }, { "gainBlur", common + s.gainBlur, "cs_5_0" }, { "decide", common + s.decide, "cs_5_0" },
        { "panelClock", common + s.panelClock, "cs_5_0" }, { "starStat", common + s.starStat, "cs_5_0" },
        { "starWeight", common + s.starWeight, "cs_5_0" }, { "starPlan", common + s.starPlan, "cs_5_0" },
        { "statCombine", std::string(s.zoneCombineDefine) + common + s.stat, "cs_5_0" },
        { "starStatCombine", std::string(s.zoneCombineDefine) + common + s.starStat, "cs_5_0" } };
    if (s.clockMeanDefine) p.push_back({ "panelClockMean", std::string(s.clockMeanDefine) + common + s.panelClock, "cs_5_0" });
    for (unsigned int v = 0; v < FALD_CONV_SO_VARIANTS; v++)
        p.push_back({ "conv" + std::to_string(v), std::string(g_faldConvSoDefine[v]) + common + s.conv, "cs_5_0" });
    return p;
}
}  // namespace

// The layer's own passes hold no `precise`: it propagates backwards through everything that feeds the marked value, so
// one in a helper compiles the passes around it precise (no mad) and moves the layer's output (2026-10-22: 5 of the
// gate's 1210 hashes moved that way).
TEST_CASE("FALD layer: no `precise` in the layer's compiled passes") {
    for (const LgPass& p : LgLayerPasses()) {
        ID3DBlob* blob = nullptr; ID3DBlob* err = nullptr;
        const HRESULT hr = D3DCompile(p.src.c_str(), p.src.size(), p.name.c_str(), nullptr, nullptr, "main", p.target, 0, 0, &blob, &err);
        if (err) err->Release();
        REQUIRE_MESSAGE(SUCCEEDED(hr), p.name);
        ID3DBlob* dis = nullptr;
        REQUIRE(SUCCEEDED(D3DDisassemble(blob->GetBufferPointer(), blob->GetBufferSize(), 0, nullptr, &dis)));
        const std::string text((const char*)dis->GetBufferPointer(), dis->GetBufferSize());
        CHECK_MESSAGE(text.find("precise") == std::string::npos, p.name << ": a `precise` instruction in a layer pass");
        const std::string dir = LgEnv("FALD_TEST_LAYER_DISASM");
        if (!dir.empty()) CHECK(LgWriteText(dir + "/" + p.name + ".asm", text));
        dis->Release(); blob->Release();
    }
}
