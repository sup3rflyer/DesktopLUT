// The frozen fade stack (F12B_V8d = ASF10 + T11_TMTC + A + B16 + V8d; DLC results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md
// and its deltas r12a/PORT_DELTA.md, r12b/PORT_DELTA.md) — WARP gates.
//
// OFF gate (PORT_PLAN §5.1): with the flank fade OFF the layer is today's, bit for bit. Every field the overlay's layer
// leaves behind and the output frame are hashed (FNV-1a 64) for a fixed matrix of runs: the gate's still cells x the
// owner's PA32 panel files (HDR + SDR) x starfield on / off, the debug views, a timing-free LED-lag sequence (mode 2,
// tau 0, delay 2) and an odd-sized frame on its own panel file. FALD_TEST_FADE_RECORD=<file> writes the manifest (run
// on main BEFORE the port: tests/fixtures/fald_fade_off_main.hashes), FALD_TEST_FADE_REF=<file> checks every line against it.
// Inputs: FALD_TEST_FADE_DIR = the cell directory DLC tests/fald_fade_cells.py writes (cells.txt + <name>.rgba16f +
// odd_panel.bin); the owner's panel files are found beside this checkout or in the main checkout (OwnerPanel).
// Without FALD_TEST_FADE_DIR the cases check nothing (opt-in, like the other WARP cases).
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_fade.h"
#include "../shared/fald_fade_shader.h"
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#pragma comment(lib, "dxgi.lib")
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace {

std::string EnvStr(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

std::wstring Widen(const std::string& s) { return std::wstring(s.begin(), s.end()); }   // ASCII paths (a test convenience)

// The WARP device as the process device (g_device / g_context, as the overlay's InitD3D sets them) + the fullscreen VS.
struct FadeWarp {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11VertexShader* vs = nullptr;
    bool ok = false;
    FadeWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        // FALD_TEST_FADE_ADAPTER = a substring of a hardware adapter's name: the same runs on that GPU instead of WARP (a
        // diagnostic: two builds' fields compared on hardware)
        char* want = nullptr; size_t wantN = 0;
        IDXGIAdapter1* pick = nullptr;
        if (_dupenv_s(&want, &wantN, "FALD_TEST_FADE_ADAPTER") == 0 && want && *want) {
            IDXGIFactory1* fac = nullptr;
            if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&fac))) {
                for (UINT i = 0; !pick; i++) {
                    IDXGIAdapter1* a = nullptr;
                    if (fac->EnumAdapters1(i, &a) == DXGI_ERROR_NOT_FOUND) break;
                    DXGI_ADAPTER_DESC1 d; a->GetDesc1(&d);
                    char name[256] = {};
                    WideCharToMultiByte(CP_UTF8, 0, d.Description, -1, name, sizeof(name), nullptr, nullptr);
                    if (std::strstr(name, want) && !(d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) pick = a; else a->Release();
                }
                fac->Release();
            }
        }
        if (want) free(want);
        const HRESULT hr = pick ? D3D11CreateDevice(pick, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic)
                                : D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic);
        if (pick) pick->Release();
        if (FAILED(hr)) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main",
                              "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        ok = vs != nullptr && InitFaldShaders();
    }
    ~FadeWarp() {
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

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

struct Cell { std::string name; int w = 0, h = 0; };

std::vector<Cell> ReadCells(const std::string& dir) {
    std::vector<Cell> out;
    std::ifstream f(dir + "/cells.txt");
    std::string line;
    while (std::getline(f, line)) {
        std::istringstream is(line);
        Cell c;
        if (is >> c.name >> c.w >> c.h) out.push_back(c);
    }
    return out;
}

bool ReadFrame(const std::string& path, int w, int h, std::vector<uint16_t>& out) {
    out.assign((size_t)w * h * 4, 0);
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    f.read((char*)out.data(), (std::streamsize)(out.size() * sizeof(uint16_t)));
    return (size_t)f.gcount() == out.size() * sizeof(uint16_t);
}

// The owner's panel files (local data, not in the repo): beside this checkout's DLC, or in the main checkout's.
std::wstring OwnerPanel(const wchar_t* rel) {
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

// One run's settings (everything else at the FaldSettings defaults: flank fade OFF).
struct RunSpec {
    std::string id;
    std::wstring panel;
    bool hdr = true;
    std::vector<std::string> frames;    // cell names, run in order on ONE monitor context (one frame = a still)
    int w = 0, h = 0;
    bool star = false;
    unsigned int debug = 0;
    unsigned int temporal = 0;          // 0 off; 2 = B_true from the filtered drive (tau 0, delay 2: no run timing in it)
};

using Manifest = std::vector<std::pair<std::string, std::string>>;   // "<run> <field>" -> "<bytes> <fnv>"

// Runs one spec on WARP and appends "<id>/<frame index> <field>" -> "<bytes> <fnv hex>" for every field + the output.
bool RunHashes(FadeWarp& w, const std::string& cellDir, const RunSpec& rs, Manifest& man, std::string& err) {
    MonitorContext ctx;
    ctx.width = rs.w; ctx.height = rs.h;
    ctx.isHDREnabled = rs.hdr; ctx.isFP16SDR = !rs.hdr;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    ctx.frameTimeExactMs = 16.667f;
    FaldSettings& fs = rs.hdr ? ctx.hdrColorCorrection.fald : ctx.sdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = rs.panel; fs.debugMode = rs.debug; fs.star.enabled = rs.star;
    fs.temporalMode = rs.temporal; fs.tauRiseMs = 0.0f; fs.tauFallMs = 0.0f; fs.delayFrames = rs.temporal ? 2u : 0u;
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
        if (!ReadFrame(cellDir + "/" + rs.frames[i] + ".rgba16f", rs.w, rs.h, frame)) { err = "frame " + rs.frames[i]; break; }
        D3D11_VIEWPORT vp = { 0, 0, (float)rs.w, (float)rs.h, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)rs.w * 8u, 0);
        FaldRunPasses(&ctx, rtv, true);
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        const std::pair<const char*, ID3D11Texture2D*> tex[] = {
            { "drive", r->driveTex }, { "drive_filt", r->driveFiltTex }, { "drive_state", r->driveStateTex },
            { "delay0", r->delayTex[0] }, { "delay1", r->delayTex[1] }, { "delay2", r->delayTex[2] },
            { "active_r0", r->activeTex[0] }, { "active_r1", r->activeTex[1] },
            { "boost_r0", r->boostTex[0] }, { "boost_r1", r->boostTex[1] }, { "btrue", r->bTrueTex }, { "best", r->bEstTex },
            { "flat_btrue", r->flatTrueTex }, { "flat_best", r->flatEstTex }, { "gainA", r->gainATex }, { "gainB", r->gainBTex },
            { "star_stat", r->starStatTex }, { "star_w", r->starWTex }, { "star_plan", r->starPlanTex },
            { "star_plan2", r->starPlan2Tex }, { "star_bg", r->starBgTex } };
        const std::string key = rs.id + "/" + std::to_string(i);
        char buf[64];
        for (const auto& t : tex) {
            const std::vector<uint8_t> b = ReadBack(t.second);
            snprintf(buf, sizeof(buf), "%zu %016llx", b.size(), (unsigned long long)Fnv(b));
            man.emplace_back(key + " " + t.first, buf);
        }
        const std::vector<uint8_t> out = ReadBack(target);
        snprintf(buf, sizeof(buf), "%zu %016llx", out.size(), (unsigned long long)Fnv(out));
        man.emplace_back(key + " OUTPUT", buf);
    }
    rtv->Release(); target->Release();
    FaldReleaseResources(&ctx);
    return err.empty();
}

// The OFF gate's run matrix (PORT_PLAN §5.1): fixed, so a manifest recorded on main is checked line for line.
std::vector<RunSpec> OffMatrix(const std::vector<Cell>& cells, const std::string& cellDir) {
    std::vector<RunSpec> out;
    const std::wstring C = OwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    const std::wstring B07 = OwnerPanel(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin");
    const std::wstring SDR = OwnerPanel(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin");
    const std::wstring ODD = Widen(cellDir + "/odd_panel.bin");
    auto add = [&](const std::string& id, const std::wstring& panel, bool hdr, const std::vector<std::string>& frames, int w, int h,
                   bool star, unsigned int debug, unsigned int temporal) {
        RunSpec r; r.id = id; r.panel = panel; r.hdr = hdr; r.frames = frames; r.w = w; r.h = h; r.star = star; r.debug = debug;
        r.temporal = temporal;
        out.push_back(r);
    };
    for (const Cell& c : cells) {
        if (c.w == 3840 && c.h == 2160) {
            if (!C.empty()) for (int s = 0; s < 2; s++) add("C." + c.name + ".star" + std::to_string(s), C, true, { c.name }, c.w, c.h, s != 0, 0, 0);
            if (!B07.empty()) add("B07." + c.name + ".star1", B07, true, { c.name }, c.w, c.h, true, 0, 0);
        } else if (c.name == "x_odd1366") {
            for (int s = 0; s < 2; s++) add("ODD." + c.name + ".star" + std::to_string(s), ODD, true, { c.name }, c.w, c.h, s != 0, 0, 0);
        }
    }
    if (!SDR.empty())
        for (const char* n : { "n7_lamp", "p07_dark_bar0", "s10_sm_seattle" })
            for (int s = 0; s < 2; s++) add(std::string("SDR.") + n + ".star" + std::to_string(s), SDR, false, { n }, 3840, 2160, s != 0, 0, 0);
    if (!C.empty()) {
        for (unsigned int d : { 1u, 2u, 3u, 4u, 5u, 6u, 7u, 8u, 9u })
            add("C.n7_lamp.star1.debug" + std::to_string(d), C, true, { "n7_lamp" }, 3840, 2160, true, d, 0);
        add("C.seq.mode2", C, true, { "p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus" }, 3840, 2160, true, 0, 2);
    }
    return out;
}

bool WriteManifest(const std::string& path, const Manifest& man) {
    std::ofstream f(path);
    if (!f) return false;
    for (const auto& kv : man) f << kv.first << " " << kv.second << "\n";
    return (bool)f;
}

std::map<std::string, std::string> ReadManifest(const std::string& path) {
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

TEST_CASE("FALD fade OFF: every field and the output hash as on main (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cellDir = EnvStr("FALD_TEST_FADE_DIR");
    if (cellDir.empty()) return;
    const std::string record = EnvStr("FALD_TEST_FADE_RECORD"), ref = EnvStr("FALD_TEST_FADE_REF");
    const std::vector<Cell> cells = ReadCells(cellDir);
    REQUIRE_FALSE(cells.empty());
    FadeWarp w;
    REQUIRE(w.ok);
    const std::vector<RunSpec> runs = OffMatrix(cells, cellDir);
    REQUIRE_FALSE(runs.empty());
    Manifest man;
    const auto t0 = std::chrono::steady_clock::now();
    for (const RunSpec& rs : runs) {
        std::string err;
        const bool ok = RunHashes(w, cellDir, rs, man, err);
        CHECK_MESSAGE(ok, rs.id << ": " << err);
    }
    const double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    MESSAGE(runs.size() << " runs, " << man.size() << " hashes, " << s << " s");
    if (!record.empty()) REQUIRE(WriteManifest(record, man));
    if (!ref.empty()) {
        const std::map<std::string, std::string> want = ReadManifest(ref);
        REQUIRE(want.size() == man.size());
        size_t bad = 0;
        for (const auto& kv : man) {
            auto it = want.find(kv.first);
            const bool same = it != want.end() && it->second == kv.second;
            if (!same && bad++ < 20) MESSAGE("MISMATCH " << kv.first << ": " << kv.second << " vs " << (it == want.end() ? "absent" : it->second));
        }
        CHECK(bad == 0u);
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// ON
// ---------------------------------------------------------------------------------------------------------------------
namespace {

// DXBC disassembly lines whose instruction is a float mad (indentation varies with nesting; imad / umad are integer)
size_t CountFloatMads(const std::string& text) {
    size_t n = 0, a = 0;
    while (a < text.size()) {
        size_t b = text.find('\n', a);
        if (b == std::string::npos) b = text.size();
        size_t s = a;
        while (s < b && (text[s] == ' ' || text[s] == '\t')) s++;
        if (text.compare(s, 4, "mad ") == 0 || text.compare(s, 5, "mad_s") == 0) n++;
        a = b + 1;
    }
    return n;
}

bool WriteBytes(const std::string& path, const void* p, size_t n) {
    std::ofstream f(path, std::ios::binary);
    if (!f) return false;
    f.write((const char*)p, (std::streamsize)n);
    return (bool)f;
}

std::vector<uint8_t> ReadBackBuffer(ID3D11Buffer* buf) {
    std::vector<uint8_t> out;
    if (!buf) return out;
    D3D11_BUFFER_DESC d; buf->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0; d.StructureByteStride = 0;
    ID3D11Buffer* st = nullptr;
    if (FAILED(g_device->CreateBuffer(&d, nullptr, &st))) return out;
    g_context->CopyResource(st, buf);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.assign((const uint8_t*)m.pData, (const uint8_t*)m.pData + d.ByteWidth);
        g_context->Unmap(st, 0);
    }
    st->Release();
    return out;
}

// one run of the ON gate: frames in order on ONE monitor context (the first = a still: no history), each dumped
struct OnSpec {
    std::string id;
    std::wstring panel;
    bool hdr = true;
    bool star = true;
    std::vector<std::string> frames;   // paths relative to the cell dir, without extension
    int w = 3840, h = 2160;
    bool fade = true;                  // false = the layer without the fade (base fields + output only: a diagnostic)
};

int g_lastFadeWarmupRuns = 0;   // RunOn: runs of the first frame before the paced fade was on

// Hash-only ON runs (FALD_TEST_FADE_HASHES=<manifest>): every file the dump would write is hashed instead ("<run>/tNN
// <file> <bytes> <fnv>"), for a byte-for-byte comparison of two builds without 30 GB of dumps. FALD_TEST_FADE_STILL=1
// adds, after every content frame, one run of the layer WITHOUT new content on the same frame (dumped as tNNs): the
// presents a desktop makes between content frames (the fade's reach reuse, fald_fade.h FaldFadeReachCurrent).
Manifest* g_onHashes = nullptr;

// The run of the layer the harness makes: newContent + (where this build has it) the frame's key — content frames count
// up, a run without new content repeats the last key. The governor is forced on: WARP is a software adapter (no VRAM),
// which the governor would never start the fade on (shared/fald_fade_governor.h).
void HarnessRun(MonitorContext* ctx, ID3D11RenderTargetView* rtv, bool newContent, unsigned long long key) {
#ifdef FALD_RUN_HAS_FRAME_KEY
    g_faldFadeForce.store(true);
    FaldRunPasses(ctx, rtv, newContent, key);
#else
    (void)key;
    FaldRunPasses(ctx, rtv, newContent);
#endif
}

void HarnessMaterialize(FaldFade* f, ID3D11DeviceContext* ic) {
#ifdef FALD_FADE_HAS_DEBUG_MATERIALIZE
    FaldFadeDebugMaterialize(f, ic);
#else
    (void)f; (void)ic;
#endif
}

// round 12b's halo stills (DLC tests/fald_fade_cells.py F12B_STILLS; tests/test_fald_hook.cpp runs them too)
const char* const kHaloCells[] = { "ax_552", "ax_412", "ax_422", "ax_442", "ax_492", "ax_522", "s10_sm_windmill", "s10_sm_cows",
                                   "s10_sm_snowfence", "s10_ac_rec2020" };

bool RunOn(FadeWarp& w, const std::string& cellDir, const std::string& outDir, const OnSpec& rs, std::string& err, double& secs) {
    MonitorContext ctx;
    ctx.width = rs.w; ctx.height = rs.h;
    ctx.isHDREnabled = rs.hdr; ctx.isFP16SDR = !rs.hdr;
    ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
    ctx.frameTimeExactMs = 16.667f;
    FaldSettings& fs = rs.hdr ? ctx.hdrColorCorrection.fald : ctx.sdrColorCorrection.fald;
    fs.enabled = true; fs.paramsPath = rs.panel; fs.star.enabled = rs.star; fs.flankFade = rs.fade;
    if (!FaldEnsureResources(&ctx, fs)) { err = ctx.fald ? ctx.fald->lastError : std::string("no resources"); FaldReleaseResources(&ctx); return false; }
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = (UINT)rs.w; td.Height = (UINT)rs.h; td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_RENDER_TARGET;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    if (FAILED(w.dev->CreateTexture2D(&td, nullptr, &target)) || FAILED(w.dev->CreateRenderTargetView(target, nullptr, &rtv))) {
        err = "target"; if (target) target->Release(); FaldReleaseResources(&ctx); return false;
    }
    std::vector<uint16_t> frame;
    if (rs.fade) {
        // The fade's creation is paced (shaders + resources over several runs, the layer without the fade meanwhile —
        // fald_fade.h): run the first frame until the fade is on, then reset its T state, so the gate's frame 0 is a still
        // exactly as it was before the pacing existed.
        if (!ReadFrame(cellDir + "/" + rs.frames[0] + ".rgba16f", rs.w, rs.h, frame)) err = "frame " + rs.frames[0];
        int runs = 0;
        for (; err.empty() && runs < 400 && !ctx.fald->fadeOn; runs++) {
            D3D11_VIEWPORT vp = { 0, 0, (float)rs.w, (float)rs.h, 0, 1 };
            w.ic->RSSetViewports(1, &vp);
            w.ic->VSSetShader(w.vs, nullptr, 0);
            w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)rs.w * 8u, 0);
            HarnessRun(&ctx, rtv, true, 0);
            w.ic->Flush();
        }
        if (err.empty() && !ctx.fald->fadeOn) err = "the fade did not turn on within 400 runs";
        if (err.empty()) FaldFadeReset(ctx.fald->fade);
        g_lastFadeWarmupRuns = runs;
    }
    const bool still = EnvStr("FALD_TEST_FADE_STILL") == "1";
    const auto t0 = std::chrono::steady_clock::now();
    for (size_t i = 0; i < rs.frames.size() && err.empty(); i++) {
        if (!ReadFrame(cellDir + "/" + rs.frames[i] + ".rgba16f", rs.w, rs.h, frame)) { err = "frame " + rs.frames[i]; break; }
        for (int pass = 0; pass < (still ? 2 : 1) && err.empty(); pass++) {   // pass 1: the same frame, no new content
        D3D11_VIEWPORT vp = { 0, 0, (float)rs.w, (float)rs.h, 0, 1 };
        w.ic->RSSetViewports(1, &vp);
        w.ic->VSSetShader(w.vs, nullptr, 0);
        w.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        if (pass == 0) w.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)rs.w * 8u, 0);
        HarnessRun(&ctx, rtv, pass == 0, 1000ull + i);
        w.ic->Flush();
        const FaldResources* r = ctx.fald;
        if (rs.fade != r->fadeOn) { err = rs.fade ? "the fade did not turn on" : "the fade is on"; break; }
        FaldFadeDebug d = {};
        if (rs.fade) { HarnessMaterialize(r->fade, w.ic); d = FaldFadeDebugViews(r->fade); }
        else { d.width = (unsigned)rs.w; d.height = (unsigned)rs.h; }
        char sub[32]; snprintf(sub, sizeof(sub), pass == 0 ? "/t%02zu" : "/t%02zus", i);
        const std::string dir = outDir + "/" + rs.id + sub;
        if (!g_onHashes) {
            CreateDirectoryA((outDir + "/" + rs.id).c_str(), nullptr);
            CreateDirectoryA(dir.c_str(), nullptr);
        }
        const std::pair<const char*, ID3D11Texture2D*> tex[] = {
            { "mi.f32", d.mi }, { "fade.f32", d.fade }, { "fade_t.f32", d.fadeT }, { "floor.f32", d.floorF },
            { "reach0.f32", d.reach[0] }, { "reach1.f32", d.reach[1] }, { "s.f32", d.s }, { "fq.f32", d.fq },
            { "dec.f32", d.dec }, { "kq1.f32", d.kq }, { "v8.f32", d.v8 }, { "drive.f32", r->driveTex }, { "btrue.f32", r->bTrueTex },
            { "best.f32", r->bEstTex }, { "gain.rg32f", r->gainBTex }, { "boost_r0.f32", r->boostTex[0] },
            { "boost_r1.f32", r->boostTex[1] }, { "out.rgba16f", target } };
        for (const auto& t : tex) {
            if (!t.second) continue;
            const std::vector<uint8_t> b = ReadBack(t.second);
            if (g_onHashes) {
                char buf[64];
                snprintf(buf, sizeof(buf), "%zu %016llx", b.size(), (unsigned long long)Fnv(b));
                g_onHashes->emplace_back(rs.id + sub + " " + t.first, buf);
            } else if (!WriteBytes(dir + "/" + t.first, b.data(), b.size())) { err = "write " + dir; break; }
        }
        if (rs.fade) {
            const std::vector<uint8_t> fl = ReadBackBuffer(d.flags);
            if (g_onHashes) {
                char buf[64];
                snprintf(buf, sizeof(buf), "%zu %016llx", fl.size(), (unsigned long long)Fnv(fl));
                g_onHashes->emplace_back(rs.id + sub + " flags.u32", buf);
            } else WriteBytes(dir + "/flags.u32", fl.data(), fl.size());
        }
        if (g_onHashes) continue;
        std::ofstream meta(dir + "/meta.txt");
        meta << "width " << d.width << "\nheight " << d.height << "\nwq " << d.wq << "\nhq " << d.hq << "\nw8 " << d.w8 << "\nh8 " << d.h8
             << "\nhas_history " << (d.hasHistory ? 1 : 0) << "\nstar " << (rs.star ? 1 : 0) << "\nhdr " << (rs.hdr ? 1 : 0)
             << "\nframe " << rs.frames[i] << "\nvram_bytes " << (rs.fade ? FaldFadeVramBytes(r->fade) : 0ull)
             << "\nfade " << (rs.fade ? 1 : 0) << "\n";
        }
    }
    secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    rtv->Release(); target->Release();
    FaldReleaseResources(&ctx);
    return err.empty();
}

}  // namespace

// The fade passes compile under IEEE strictness and their float arithmetic is never fused: DLC dlc/fald/fadeemu.py
// emulates the operation order, which a `mad` would break (D3D lets a device run it fused or not).
// The passes ship as bytecode (shared/fald_fade_bytecode.h): it must be exactly what D3DCompile makes of the current sources
// (cs_5_0 + D3DCOMPILE_IEEE_STRICTNESS, fald_fade.cpp FaldFadeCompileKernel) — a source edit without a regenerated header
// fails here. FALD_TEST_FADE_WRITE_BYTECODE=<path of shared/fald_fade_bytecode.h> (re)writes it instead (then rebuild).
// What ships contains no float mad: D3D lets a device run a mad fused or not, and fadeemu emulates the unfused order.
TEST_CASE("FALD fade: the embedded bytecode is a fresh compile of the sources, with no float mad") {
    const unsigned int n = FaldFadeKernelCount();
    REQUIRE(n == 39u);   // 17 passes + the tiled Gaussians (8 radius classes, lv's four, num / den Kahan + plain), max / min (8),
                         // cone, FFT, mi's block reductions
    std::vector<std::vector<unsigned char>> fresh(n);
    const auto t0 = std::chrono::steady_clock::now();
    for (unsigned int k = 0; k < n; k++) {
        std::string err;
        CHECK_MESSAGE(FaldFadeCompileKernel(k, fresh[k], &err), std::string(FaldFadeKernelName(k)) << ": " << err);
        CHECK(fresh[k].size() % 4u == 0u);   // DXBC is a sequence of 4-byte words
    }
    MESSAGE("fade passes compiled in " << std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count() << " s");
    const std::string writePath = EnvStr("FALD_TEST_FADE_WRITE_BYTECODE");
    if (!writePath.empty()) {
        std::ostringstream h;
        size_t total = 0;
        h << "// DesktopLUT - shared/fald_fade_bytecode.h — GENERATED, do not edit.\n"
             "// The fade passes (shared/fald_fade.h) as D3DCompile builds them from fald_shader.h's common source +\n"
             "// fald_fade_shader.h: cs_5_0, entry \"main\", D3DCOMPILE_IEEE_STRICTNESS (fald_fade.cpp FaldFadeCompileKernel).\n"
             "// Written by tests/test_fald_fade.cpp with FALD_TEST_FADE_WRITE_BYTECODE=<this file>; the same test fails while\n"
             "// this file is not a fresh compile of the current sources. Shipping bytecode keeps the shader compiler out of the\n"
             "// hook's present and the overlay's render thread.\n"
             "#pragma once\n#include <stddef.h>\n#include <stdint.h>\n\n"
             "struct FaldFadeBytecodeEntry { const uint32_t* words; size_t bytes; };\n\n";
        for (unsigned int k = 0; k < n; k++) {
            const std::vector<unsigned char>& b = fresh[k];
            REQUIRE(b.size() % 4u == 0u);
            total += b.size();
            h << "static const uint32_t kFaldFadeBc" << k << "[] = {   // " << FaldFadeKernelName(k) << ", " << b.size() << " bytes\n";
            for (size_t i = 0; i < b.size(); i += 4) {
                const uint32_t w = (uint32_t)b[i] | ((uint32_t)b[i + 1] << 8) | ((uint32_t)b[i + 2] << 16) | ((uint32_t)b[i + 3] << 24);
                char word[16];
                snprintf(word, sizeof(word), "0x%08x,", w);
                h << ((i / 4) % 8 == 0 ? "    " : " ") << word << ((i / 4) % 8 == 7 || i + 4 >= b.size() ? "\n" : "");
            }
            h << "};\n";
        }
        h << "\nstatic const unsigned int kFaldFadeBytecodeCount = " << n << ";\n"
          << "static const FaldFadeBytecodeEntry g_faldFadeBytecode[" << n << "] = {\n";
        for (unsigned int k = 0; k < n; k++)
            h << "    { kFaldFadeBc" << k << ", " << fresh[k].size() << " },   // " << FaldFadeKernelName(k) << "\n";
        h << "};\n";
        const std::string text = h.str();
        REQUIRE(WriteBytes(writePath, text.data(), text.size()));
        MESSAGE("wrote " << writePath << ": " << n << " passes, " << total << " bytes of bytecode");
        return;
    }
    for (unsigned int k = 0; k < n; k++) {
        const void* bc = nullptr; size_t bytes = 0;
        const bool have = FaldFadeEmbeddedBytecode(k, &bc, &bytes);
        CHECK_MESSAGE(have, std::string(FaldFadeKernelName(k)) << ": no embedded bytecode (FALD_TEST_FADE_WRITE_BYTECODE)");
        if (!have) continue;
        const bool same = bytes == fresh[k].size() && std::memcmp(bc, fresh[k].data(), bytes) == 0;
        CHECK_MESSAGE(same, std::string(FaldFadeKernelName(k)) << ": shared/fald_fade_bytecode.h is not a fresh compile of the sources "
                            "(regenerate: FALD_TEST_FADE_WRITE_BYTECODE)");
        ID3DBlob* dis = nullptr;
        REQUIRE(SUCCEEDED(D3DDisassemble(bc, bytes, 0, nullptr, &dis)));
        const std::string text((const char*)dis->GetBufferPointer(), dis->GetBufferSize());
        const size_t mads = CountFloatMads(text);
        CHECK_MESSAGE(mads == 0u, std::string(FaldFadeKernelName(k)) << ": " << mads << " float mad instruction(s)");
        dis->Release();
    }
    // the detector itself: the layer's statistic pass (no `precise`, default flags) does fuse — else the check above
    // proves nothing
    const std::string src = std::string(g_faldCommonSource) + g_faldStatSource;
    ID3DBlob* blob = nullptr;
    REQUIRE(SUCCEEDED(D3DCompile(src.c_str(), src.size(), "stat", nullptr, nullptr, "main", "cs_5_0", 0, 0, &blob, nullptr)));
    ID3DBlob* dis = nullptr;
    REQUIRE(SUCCEEDED(D3DDisassemble(blob->GetBufferPointer(), blob->GetBufferSize(), 0, nullptr, &dis)));
    const std::string text((const char*)dis->GetBufferPointer(), dis->GetBufferSize());
    const size_t mads = CountFloatMads(text);
    const std::string dump = EnvStr("FALD_TEST_FADE_DISASM");
    if (!dump.empty()) WriteBytes(dump, text.data(), text.size());
    CHECK(mads > 0u);
    dis->Release(); blob->Release();
}

// The layer's own passes (src/fald.cpp CompileOne: the default flags; the hook compiles the same sources) hold no
// `precise`: it propagates backwards through everything that feeds the marked value, so one in FadePost compiled ALL of
// Correct and the passes around it precise (no mad) and moved the layer's output with the fade OFF (2026-10-22: 5 of the
// OFF gate's 1210 hashes). FadePost keeps the fade passes' rounding without it (fald_shader.h FadeUpAtPost).
TEST_CASE("FALD layer: no `precise` in the layer's compiled passes (the fade-OFF layer compiles as main's)") {
    const FaldShaderSources s = FaldDefaultShaderSources();
    struct P { const char* name; const char* src; const char* target; };
    const P passes[] = { { "pixel", s.pixel, "ps_5_0" }, { "stat", s.stat, "cs_5_0" }, { "conv", s.conv, "cs_5_0" },
                         { "gain", s.gain, "cs_5_0" }, { "blur", s.blur, "cs_5_0" }, { "temporal", s.temporal, "cs_5_0" },
                         { "panelClock", s.panelClock, "cs_5_0" }, { "boost", s.boost, "cs_5_0" },
                         { "starStat", s.starStat, "cs_5_0" }, { "starWeight", s.starWeight, "cs_5_0" },
                         { "starPlan", s.starPlan, "cs_5_0" } };
    for (const P& p : passes) {
        const std::string src = std::string(s.common) + p.src;
        ID3DBlob* blob = nullptr; ID3DBlob* err = nullptr;
        const HRESULT hr = D3DCompile(src.c_str(), src.size(), p.name, nullptr, nullptr, "main", p.target, 0, 0, &blob, &err);
        if (err) err->Release();
        REQUIRE_MESSAGE(SUCCEEDED(hr), p.name);
        ID3DBlob* dis = nullptr;
        REQUIRE(SUCCEEDED(D3DDisassemble(blob->GetBufferPointer(), blob->GetBufferSize(), 0, nullptr, &dis)));
        const std::string text((const char*)dis->GetBufferPointer(), dis->GetBufferSize());
        CHECK_MESSAGE(text.find("precise") == std::string::npos, p.name << ": a `precise` instruction in a layer pass");
        dis->Release(); blob->Release();
    }
}

// The ON gate's device side (PORT_PLAN 5.2 step 2): every gate cell through the overlay's layer on WARP with the flank fade
// on, one monitor context per still (= no history) and per sequence (16 content frames in order), each frame's fade-stack
// fields, base fields, T flags and output dumped to FALD_TEST_FADE_ON/<run>/tNN/ for DLC tests/test_fald_fade_gate2.py.
// FALD_TEST_FADE_ONLY (optional) = a substring of the run ids to do.
// FALD_TEST_FADE_FRAMES (optional) = at most this many frames per sequence (the negative control's short runs).
// FALD_TEST_FADE_BASEONLY=1 (diagnostic) = the same runs with the flank fade OFF: base fields + output only.
TEST_CASE("FALD fade ON: WARP dumps for the DLC replay (FALD_TEST_FADE_DIR + FALD_TEST_FADE_ON)") {
    const std::string cellDir = EnvStr("FALD_TEST_FADE_DIR"), outDir = EnvStr("FALD_TEST_FADE_ON"), only = EnvStr("FALD_TEST_FADE_ONLY");
    if (cellDir.empty() || outDir.empty()) return;
    const std::string framesEnv = EnvStr("FALD_TEST_FADE_FRAMES");
    const int seqFrames = framesEnv.empty() ? 16 : (std::max)(1, (std::min)(16, atoi(framesEnv.c_str())));
    const bool baseOnly = EnvStr("FALD_TEST_FADE_BASEONLY") == "1";
    CreateDirectoryA(outDir.c_str(), nullptr);
    FadeWarp w;
    REQUIRE(w.ok);
    const std::wstring C = OwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    const std::wstring SDR = OwnerPanel(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin");
    REQUIRE_FALSE(C.empty());
    std::vector<OnSpec> runs;
    auto add = [&](const std::string& id, const std::wstring& panel, bool hdr, bool star, const std::vector<std::string>& frames, int fw, int fh) {
        OnSpec s; s.id = id; s.panel = panel; s.hdr = hdr; s.star = star; s.frames = frames; s.w = fw; s.h = fh; s.fade = !baseOnly;
        runs.push_back(s);
    };
    for (const char* n : { "p03_hl_grey5", "p02_hl_black", "n7_lamp", "n8_chess", "n9_cactus", "k1_held", "o1_held", "p07_dark_bar0",
                           "s10_sm_seattle", "s10_ac_firewater" })
        add(std::string("still_") + n, C, true, true, { n }, 3840, 2160);
    // round 12a's three stills (F12A_B16, r12a/PORT_DELTA.md §4: the ACES markers 18 / 52 and A's largest effect), when the
    // cell dir holds them (DLC tests/fald_fade_cells.py f12a; not in cells.txt, which is the OFF matrix's frozen set)
    for (const char* n : { "aces_m18", "aces_m52", "s10_ac_redspot" })
        if (GetFileAttributesA((cellDir + "/" + n + ".rgba16f").c_str()) != INVALID_FILE_ATTRIBUTES)
            add(std::string("still_") + n, C, true, true, { n }, 3840, 2160);
    // round 12b's ten halo stills (F12B_V8d, r12b/PORT_DELTA.md §4: its held-out frames with dark objects next to glows, and
    // an R-2 band frame), when the cell dir holds them (DLC tests/fald_fade_cells.py f12b; not in cells.txt either)
    for (const char* n : kHaloCells)
        if (GetFileAttributesA((cellDir + "/" + n + ".rgba16f").c_str()) != INVALID_FILE_ATTRIBUTES)
            add(std::string("still_") + n, C, true, true, { n }, 3840, 2160);
    add("still_x_naninf", C, true, false, { "x_naninf" }, 3840, 2160);                       // star off: the sanitise cell
    add("still_x_odd1366", Widen(cellDir + "/odd_panel.bin"), true, true, { "x_odd1366" }, 1366, 768);
    if (!SDR.empty()) add("still_sdr_n7_lamp", SDR, false, true, { "n7_lamp" }, 3840, 2160);
    for (const char* q : { "s10_sm_seattle__p6n", "s10_ac_firewater__p6", "t_hl0_v16__mv", "t_cutdark__mv" }) {
        std::vector<std::string> fr;
        for (int t = 0; t < seqFrames; t++) { char b[64]; snprintf(b, sizeof(b), "seq_%s/t%02d", q, t); fr.push_back(b); }
        add(std::string("seq_") + q, C, true, true, fr, 3840, 2160);
    }
    // the references of tests/test_fald_hook.cpp's resets: a FRESH context from a mid-sequence frame (= what the hook must
    // show after a live panel swap to B07 at frame 8, an idle reset at frame 8, a re-injection at frame 12)
    const std::wstring B07 = OwnerPanel(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin");
    auto seattleFrom = [&](int first) {
        std::vector<std::string> fr;
        for (int t = first; t < 16; t++) { char b[64]; snprintf(b, sizeof(b), "seq_s10_sm_seattle__p6n/t%02d", t); fr.push_back(b); }
        return fr;
    };
    if (!B07.empty()) add("ref_seattle_from8_B07", B07, true, true, seattleFrom(8), 3840, 2160);
    add("ref_seattle_from8_C", C, true, true, seattleFrom(8), 3840, 2160);
    add("ref_seattle_from12_C", C, true, true, seattleFrom(12), 3840, 2160);
    // a fractional zone pitch with the fade on (sanity, no twin reference): a DLC export of a 27" 2560 x 1440 panel with
    // 48 x 24 zones (53.33 x 60 px; docs/fald-fractional-pitch.md) and a 2560 x 1440 crop, when the cell dir holds them
    const std::string fracPanel = cellDir + "/frac_panel_48x24.bin";
    if (GetFileAttributesA(fracPanel.c_str()) != INVALID_FILE_ATTRIBUTES)
        add("still_frac2560", Widen(fracPanel), true, true, { "x_frac2560" }, 2560, 1440);
    const std::string hashPath = EnvStr("FALD_TEST_FADE_HASHES");
    Manifest hashes;
    if (!hashPath.empty()) g_onHashes = &hashes;
    for (const OnSpec& rs : runs) {
        if (!only.empty() && rs.id.find(only) == std::string::npos) continue;
        std::string err; double secs = 0.0;
        const bool ok = RunOn(w, cellDir, outDir, rs, err, secs);
        CHECK_MESSAGE(ok, rs.id << ": " << err);
        MESSAGE(rs.id << ": " << rs.frames.size() << " frame(s), " << secs << " s (the fade on after " << g_lastFadeWarmupRuns
                << " paced run(s) of frame 0)");
        if (!hashPath.empty()) WriteManifest(hashPath, hashes);   // as it goes (a long run can be read while it runs)
    }
    g_onHashes = nullptr;
    if (!hashPath.empty()) { REQUIRE(WriteManifest(hashPath, hashes)); MESSAGE(hashes.size() << " hashes -> " << hashPath); }
}
