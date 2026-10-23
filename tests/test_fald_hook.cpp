// The DWM hook's FALD layer WITH the flank fade (faldFlags bit 8) on WARP — dwm_hook/hook_fald.cpp compiled into this exe as
// the DLL builds it (DesktopLUT.Tests.vcxproj: DLUT_HOOK_TESTING = the test seams, no host events, a %TEMP% log), driven
// the way hook_render.cpp RenderLUT drives it with no LUT and no tone map: FaldAcquire -> FaldSetLiveSettings ->
// FaldUpdateClean (the present's dirty rects) -> the clean copy into the intermediate -> FaldRun into the back buffer ->
// FaldReturn. Its output and fade-stack fields must equal, BYTE FOR BYTE, the overlay path's WARP dumps of the same frames
// (tests/test_fald_fade.cpp "FALD fade ON", FALD_TEST_FADE_ON) — the hook and the overlay run the same passes.
//
// Cases: the stills (HDR with the starfield on — n7_lamp, round 12a's three, round 12b's ten halo frames when the cell dir
// holds them; the sanitise cell with it off; the odd size on its own panel; ACM SDR; a fractional pitch);
// the seattle pan (16 content frames, two presents WITHOUT new content in between: nothing steps, the output repeats);
// the same pan with a live panel swap at frame 8 (the fade moves with the frame part, T resets: frames 8..15 = a fresh
// context from frame 8 on the new file) and with an idle reset at frame 8 (FaldMarkStale) plus a re-injection at frame
// 12 (FaldReleaseAll + the shaders released and re-created, the paced creation run again). The paced creation itself:
// the layer runs without the fade (CB word 75 = 0) until the fade's shaders and resources exist, then word 75 = 1.
//
// Opt-in: FALD_TEST_FADE_DIR (the cells) + FALD_TEST_FADE_ON (the overlay's dumps, including its ref_* runs);
// FALD_TEST_FADE_HOOK_ONLY = a substring of the case names to run.
#include "doctest.h"
#include "../dwm_hook/hook_fald.h"
#include "../shared/fald_fade.h"
#include "../shared/fald_core_governor.h"   // g_faldCoreHoldOverride (the held-run case)
#include "../shared/fald_core_mode.h"       // the always-on core's modes (the change-driven case runs C and KC3)

#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <algorithm>
#include <fstream>
#include <iterator>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace {

std::string HookEnv(const char* name) {
    char buf[1024] = {};
    const DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? std::string(buf, n) : std::string();
}

bool ReadAll(const std::string& path, std::vector<unsigned char>& out) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    out.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    return true;
}

std::wstring OwnerPanelPath(const wchar_t* rel) {
    // beside this checkout (a worktree) or in the main checkout
    wchar_t exe[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, exe, MAX_PATH);
    std::wstring dir(exe);
    for (int up = 0; up < 8; up++) {
        const size_t s = dir.find_last_of(L"\\/");
        if (s == std::wstring::npos) break;
        dir.resize(s);
        const std::wstring cand = dir + L"\\" + rel;
        if (GetFileAttributesW(cand.c_str()) != INVALID_FILE_ATTRIBUTES) return cand;
    }
    const std::wstring main = std::wstring(L"H:\\Projects\\DesktopLUT\\") + rel;
    return GetFileAttributesW(main.c_str()) != INVALID_FILE_ATTRIBUTES ? main : std::wstring();
}

struct HookWarp {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    bool ok = false;
    HookWarp() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        ok = FaldInitShaders(dev, ic);   // the hook's own init (compiles the layer's core + feature shaders, as at attach)
    }
    ~HookWarp() {
        FaldReleaseAll();
        FaldReleaseShaders();
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

std::vector<unsigned char> ReadBackTex(ID3D11Device* dev, ID3D11DeviceContext* ic, ID3D11Texture2D* t) {
    std::vector<unsigned char> out;
    if (!t) return out;
    D3D11_TEXTURE2D_DESC d; t->GetDesc(&d);
    const unsigned bpp = d.Format == DXGI_FORMAT_R16G16B16A16_FLOAT ? 8u : d.Format == DXGI_FORMAT_R32G32_FLOAT ? 8u : 4u;
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(dev->CreateTexture2D(&d, nullptr, &st))) return out;
    ic->CopyResource(st, t);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(ic->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.resize((size_t)d.Width * d.Height * bpp);
        for (UINT y = 0; y < d.Height; y++)
            std::memcpy(out.data() + (size_t)y * d.Width * bpp, (const unsigned char*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * bpp);
        ic->Unmap(st, 0);
    }
    st->Release();
    return out;
}

std::vector<unsigned char> ReadBackBuf(ID3D11Device* dev, ID3D11DeviceContext* ic, ID3D11Buffer* b) {
    std::vector<unsigned char> out;
    if (!b) return out;
    D3D11_BUFFER_DESC d; b->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0; d.StructureByteStride = 0;
    ID3D11Buffer* st = nullptr;
    if (FAILED(dev->CreateBuffer(&d, nullptr, &st))) return out;
    ic->CopyResource(st, b);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(ic->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.assign((const unsigned char*)m.pData, (const unsigned char*)m.pData + d.ByteWidth);
        ic->Unmap(st, 0);
    }
    st->Release();
    return out;
}

// One monitor's presents through the hook layer (position 0,0), the back buffer standing for DWM's.
struct HookMonitor {
    HookWarp& w;
    unsigned W, H;
    bool hdr, star;
    ID3D11Texture2D* back = nullptr;
    ID3D11RenderTargetView* backRTV = nullptr;
    std::vector<unsigned char> frame;   // the frame DWM composed for this present
    HookMonitor(HookWarp& warp, unsigned width, unsigned height, bool isHdr, bool starOn)
        : w(warp), W(width), H(height), hdr(isHdr), star(starOn) {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = W; d.Height = H; d.MipLevels = 1; d.ArraySize = 1; d.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT; d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        if (SUCCEEDED(w.dev->CreateTexture2D(&d, nullptr, &back))) w.dev->CreateRenderTargetView(back, nullptr, &backRTV);
    }
    ~HookMonitor() { if (backRTV) backRTV->Release(); if (back) back->Release(); }
    bool Load(const std::string& path) {
        if (!ReadAll(path, frame) || frame.size() != (size_t)W * H * 8u) return false;
        return true;
    }
    // One present. newContent: DWM composed a new frame (one full-frame dirty rect); false = a present with no dirty rect.
    // Returns the run's state; out / fields are read only when asked.
    struct Result { bool ran = false, fadeOn = false, partial = false; uint32_t word75 = 0xFFFFFFFFu; FaldFade* fade = nullptr; std::string err;
                    unsigned int govState = 0; unsigned int coreGovState = 0; unsigned long long heldRuns = 0;
                    unsigned long long clockElapsed = 0; bool clockSeeded = false, lastPartial = false; };
    bool forceFade = true;   // the governor forced on (false: it decides — on WARP that is "weak GPU": never on)
    bool fadeWanted = true;  // the flank fade setting (faldFlags bit 8)
    const DwmHookFaldTuning* tuning = nullptr;   // the shared config's tuning tail (LED lag, the refresh period); null = none
    // The caller's "the frame changed everywhere" word (hook_fald.h FaldRun frameChanged: the dynamic tone map's peak
    // moved): a 16-byte buffer as the smoothing pass writes, (4, moved, 0, 0); null = a static pass. With
    // `intermediateWhole` the WHOLE frame goes into the intermediate on a partial present (what the LUT pass's indirect
    // full-frame draw does when the peak moved), instead of the rects alone.
    ID3D11Buffer* frameChangedBuf = nullptr;
    bool intermediateWhole = false;
    // The frame as the LUT / tone-map pass would leave it in the intermediate (null = the clean copy itself, no pass): on
    // a partial present its rects go in (or the whole of it with intermediateWhole), on a full one the whole of it.
    const std::vector<unsigned char>* intermediateFrame = nullptr;
    unsigned int coreMode = 0;   // the always-on core's switch (faldFlags bits 16-19; shared/fald_core_mode.h): 0 C, 1 KC2c, 2 KC3, 4 KC5
    // rects: this present's dirty rects (the frame already edited inside them); null = a full-frame rect (new content) or none
    Result Present(bool newContent, bool readCB, const std::vector<RECT>* rects = nullptr) {
        Result r;
        // DWM composes only the dirty rects into its back buffer (elsewhere it holds the previous output); with rects, the
        // stand-in gets exactly those regions of the frame
        if (newContent && !rects) w.ic->UpdateSubresource(back, 0, nullptr, frame.data(), W * 8u, 0);
        else if (rects)
            for (const RECT& rc : *rects) {
                if (rc.right <= rc.left || rc.bottom <= rc.top) continue;
                D3D11_BOX box = { (UINT)rc.left, (UINT)rc.top, 0, (UINT)rc.right, (UINT)rc.bottom, 1 };
                w.ic->UpdateSubresource(back, 0, &box, frame.data() + ((size_t)rc.top * W + (size_t)rc.left) * 8u, W * 8u, 0);
            }
        FaldMonitor* m = FaldAcquire(0, 0, hdr, W, H, DXGI_FORMAT_R16G16B16A16_FLOAT);
        if (!m) { r.err = "FaldAcquire: no layer"; return r; }
#ifdef FALD_RUN_HAS_FRAME_KEY
        // the governor forced on (faldFlags bit 10): WARP is a software adapter, which it would never start the fade on
        FaldSetLiveSettings(m, 0, 0, star, tuning, fadeWanted, 0, 0, forceFade, false, coreMode);
#else
        FaldSetLiveSettings(m, 0, 0, star, nullptr, true);
#endif
        RECT full = { 0, 0, (LONG)W, (LONG)H };
        const RECT* rc = rects ? rects->data() : (newContent ? &full : nullptr);
        const int nrc = rects ? (int)rects->size() : (newContent ? 1 : 0);
        if (!FaldUpdateClean(m, back, rc, nrc)) {
            FaldReturn(m);
            FaldMarkStale(0, 0);
            r.err = "unprimed";
            return r;
        }
        // no LUT, no tone map (RenderLUT): the whole clean copy into the intermediate, or this present's rects only on the
        // change-driven path (hook_render.cpp)
        const bool partial = FaldPlanPartial(m, 0x9E3779B97F4A7C15ull);
        const std::vector<unsigned char>* inter = intermediateFrame;
        if (partial && intermediateWhole && inter) w.ic->UpdateSubresource(FaldIntermediateTexture(m), 0, nullptr, inter->data(), W * 8u, 0);
        else if (partial) {
            for (int i = 0; i < nrc; i++) {
                if (rc[i].right <= rc[i].left || rc[i].bottom <= rc[i].top) continue;
                D3D11_BOX box = { (UINT)rc[i].left, (UINT)rc[i].top, 0, (UINT)rc[i].right, (UINT)rc[i].bottom, 1 };
                if (inter) w.ic->UpdateSubresource(FaldIntermediateTexture(m), 0, &box, inter->data() + ((size_t)rc[i].top * W + (size_t)rc[i].left) * 8u, W * 8u, 0);
                else w.ic->CopySubresourceRegion(FaldIntermediateTexture(m), 0, (UINT)rc[i].left, (UINT)rc[i].top, 0, FaldCleanTexture(m), 0, &box);
            }
        } else if (inter) w.ic->UpdateSubresource(FaldIntermediateTexture(m), 0, nullptr, inter->data(), W * 8u, 0);
        else w.ic->CopyResource(FaldIntermediateTexture(m), FaldCleanTexture(m));
        r.partial = partial;
#ifdef FALD_RUN_HAS_FRAME_CHANGED
        r.ran = FaldRun(m, backRTV, newContent, 0x9E3779B97F4A7C15ull, frameChangedBuf, 4u);   // hook_render.cpp's key for "no LUT pass"
#elif defined(FALD_RUN_HAS_FRAME_KEY)
        r.ran = FaldRun(m, backRTV, newContent, 0x9E3779B97F4A7C15ull);   // hook_render.cpp's key for "no LUT pass"
#else
        r.ran = FaldRun(m, backRTV, newContent);
#endif
#ifdef FALD_RUN_HAS_FRAME_KEY
        r.govState = FaldTestFadeGovState(m);
        r.coreGovState = FaldTestCoreGovState(m);
        r.heldRuns = FaldTestHeldRuns(m);
        r.clockElapsed = FaldTestClockElapsed(m);
        r.clockSeeded = FaldTestClockSeeded(m);
        r.lastPartial = FaldTestLastPartial(m);
#endif
        if (!r.ran) w.ic->CopyResource(back, FaldIntermediateTexture(m));
        r.fadeOn = FaldTestFadeOn(m);
        r.fade = FaldTestFade(m);
        if (readCB) {
            const std::vector<unsigned char> cb = ReadBackBuf(w.dev, w.ic, FaldTestCB(m));
            if (cb.size() >= 76 * 4) std::memcpy(&r.word75, cb.data() + 75 * 4, 4);
        }
        FaldReturn(m);
        w.ic->Flush();
        return r;
    }
};

// The overlay's ON run as a hash manifest instead of dumps (FALD_TEST_FADE_HOOK_HASHES = tests/test_fald_fade.cpp's
// FALD_TEST_FADE_HASHES output): "<run>/tNN <file>" -> "<bytes> <fnv>". `g_hookOnRoot` = the FALD_TEST_FADE_ON prefix the
// reference paths carry (stripped to form the key).
std::map<std::string, std::string>* g_hookHashes = nullptr;
std::string g_hookOnRoot;

std::string HookFnvLine(const std::vector<unsigned char>& b) {
    uint64_t h = 1469598103934665603ull;
    for (unsigned char c : b) { h ^= c; h *= 1099511628211ull; }
    char buf[64];
    snprintf(buf, sizeof(buf), "%zu %016llx", b.size(), (unsigned long long)h);
    return buf;
}

// Byte-compare this present's output + fade fields with the overlay's dump directory `ref`.
void CompareWithOverlay(HookMonitor& hm, FaldFade* fade, const std::string& ref, const std::string& what, int& compared, int& equal) {
    struct Item { const char* file; ID3D11Texture2D* tex; ID3D11Buffer* buf; };
#ifdef FALD_FADE_HAS_DEBUG_MATERIALIZE
    FaldFadeDebugMaterialize(fade, hm.w.ic);   // fade_T and F as full-res maps (FadePost forms them per pixel)
#endif
    const FaldFadeDebug d = FaldFadeDebugViews(fade);
    const Item items[] = { { "out.rgba16f", hm.back, nullptr }, { "fade.f32", d.fade, nullptr }, { "fade_t.f32", d.fadeT, nullptr },
                           { "floor.f32", d.floorF, nullptr }, { "reach0.f32", d.reach[0], nullptr },
                           { "reach1.f32", d.reach[1], nullptr }, { "s.f32", d.s, nullptr }, { "mi.f32", d.mi, nullptr },
                           { "flags.u32", nullptr, d.flags } };
    for (const Item& it : items) {
        if (g_hookHashes) {
            const std::string key = ref.substr((std::min)(ref.size(), g_hookOnRoot.size() + 1)) + " " + it.file;
            auto f = g_hookHashes->find(key);
            CHECK_MESSAGE(f != g_hookHashes->end(), what << ": no overlay hash for " << key);
            if (f == g_hookHashes->end()) continue;
            const std::vector<unsigned char> got = it.tex ? ReadBackTex(hm.w.dev, hm.w.ic, it.tex) : ReadBackBuf(hm.w.dev, hm.w.ic, it.buf);
            const bool same = HookFnvLine(got) == f->second;
            compared++;
            if (same) equal++;
            CHECK_MESSAGE(same, what << ": " << std::string(it.file) << " differs from the overlay's " << key);
            continue;
        }
        std::vector<unsigned char> want;
        const bool have = ReadAll(ref + "/" + it.file, want);
        CHECK_MESSAGE(have, what << ": no overlay dump " << ref << "/" << std::string(it.file));
        if (!have) continue;
        const std::vector<unsigned char> got = it.tex ? ReadBackTex(hm.w.dev, hm.w.ic, it.tex) : ReadBackBuf(hm.w.dev, hm.w.ic, it.buf);
        const bool same = got.size() == want.size() && std::memcmp(got.data(), want.data(), got.size()) == 0;
        compared++;
        if (same) equal++;
        CHECK_MESSAGE(same, what << ": " << std::string(it.file) << " differs from the overlay's " << ref);
    }
}

struct Staged {
    std::string root;   // what FaldLoadPanelFiles is given (it reads <root>\fald\*.bin)
    bool ok = false;
    Staged(const std::wstring& panel, bool hdr) {
        char tmp[MAX_PATH] = {};
        GetTempPathA(MAX_PATH, tmp);
        root = std::string(tmp) + "dlut_hook_fald_test_" + std::to_string(GetCurrentProcessId());
        CreateDirectoryA(root.c_str(), nullptr);
        const std::string dir = root + "\\fald";
        CreateDirectoryA(dir.c_str(), nullptr);
        // one file per test: whatever an earlier case left is removed
        DeleteFileA((dir + "\\0_0.bin").c_str());
        DeleteFileA((dir + "\\0_0_hdr.bin").c_str());
        const std::string dst = dir + (hdr ? "\\0_0_hdr.bin" : "\\0_0.bin");
        std::wstring dstW(dst.begin(), dst.end());
        ok = CopyFileW(panel.c_str(), dstW.c_str(), FALSE) != FALSE;
    }
    ~Staged() {
        DeleteFileA((root + "\\fald\\0_0.bin").c_str());
        DeleteFileA((root + "\\fald\\0_0_hdr.bin").c_str());
        RemoveDirectoryA((root + "\\fald").c_str());
        RemoveDirectoryA(root.c_str());
    }
};

// The paced creation: present frame 0 until the fade is on; the run that turns it on is frame 0's first fade run (a fresh
// T state = a still). Checks word 75 = 0 while it is off. Returns the presents it took (0 = never on).
int WarmUp(HookMonitor& hm, HookMonitor::Result& last) {
    for (int i = 1; i <= 400; i++) {
        last = hm.Present(true, true);
        if (!last.ran) { if (last.err == "unprimed") continue; return 0; }
        if (last.fadeOn) {
            CHECK(last.word75 == 1u);   // FALD_FADE_FLAG_FLANK
            return i;
        }
        CHECK(last.word75 == 0u);       // the layer without the fade meanwhile
    }
    return 0;
}

bool HaveCell(const std::string& cells, const char* name) {
    return GetFileAttributesA((cells + "/" + name + ".rgba16f").c_str()) != INVALID_FILE_ATTRIBUTES;
}

std::string SeqFrame(const std::string& cells, int t) {
    char b[160];
    snprintf(b, sizeof(b), "%s/seq_s10_sm_seattle__p6n/t%02d.rgba16f", cells.c_str(), t);
    return b;
}
std::string RefDir(const std::string& on, const char* run, int t) {
    char b[64];
    snprintf(b, sizeof(b), "/%s/t%02d", run, t);
    return on + b;
}

}  // namespace

// The switch's trip host -> DLL: dwm_inject.cpp packs FaldSettings::flankFade into faldFlags bit 8, hook_render.cpp
// RenderLUT unpacks it for FaldSetLiveSettings. Bit 9 (item L) is never set; the other fields keep their bits.
TEST_CASE("FALD hook: faldFlags bit 8 carries the flank fade, alone") {
    for (int fade = 0; fade < 2; fade++)
        for (int star = 0; star < 2; star++)
            for (int ped = 0; ped < 2; ped++)
                for (uint32_t dbg : { 0u, 4u, DWM_HOOK_FALD_DEBUG_MAX }) {
                    const uint32_t w = DwmHookFaldPack(1, dbg, ped, star, fade);
                    CHECK(DwmHookFaldFade(w) == fade);
                    CHECK(DwmHookFaldStar(w) == star);
                    CHECK(DwmHookFaldPedMode(w) == ped);
                    CHECK(DwmHookFaldDebugMode(w) == dbg);
                    CHECK(DwmHookFaldEnabled(w) == 1);
                    CHECK((w & DWM_HOOK_FALD_LOWEXT_BIT) == 0u);
                    CHECK(((w & DWM_HOOK_FALD_FADE_BIT) != 0u) == (fade != 0));
                }
    CHECK(DWM_HOOK_FALD_FADE_BIT == 0x100u);
    CHECK(DwmHookFaldFade(DwmHookFaldPack(1, 0, 0, 0)) == 0);   // an older host (no fade argument) sends it off
}

TEST_CASE("FALD hook: the flank fade in the DWM hook's layer equals the overlay's, frame for frame (WARP; FALD_TEST_FADE_ON)") {
    const std::string cells = HookEnv("FALD_TEST_FADE_DIR"), on = HookEnv("FALD_TEST_FADE_ON"), only = HookEnv("FALD_TEST_FADE_HOOK_ONLY");
    if (cells.empty() || on.empty()) return;
    std::map<std::string, std::string> hashes;
    const std::string hashPath = HookEnv("FALD_TEST_FADE_HOOK_HASHES");
    if (!hashPath.empty()) {   // "<run>/tNN <file> <bytes> <fnv>" per line
        std::ifstream f(hashPath);
        std::string line;
        while (std::getline(f, line)) {
            std::istringstream is(line);
            std::string run, file, bytes, fnv;
            if (is >> run >> file >> bytes >> fnv) hashes[run + " " + file] = bytes + " " + fnv;
        }
        REQUIRE_FALSE(hashes.empty());
        g_hookHashes = &hashes;
        g_hookOnRoot = on;
    }
    struct ClearHashes { ~ClearHashes() { g_hookHashes = nullptr; } } clearHashes;
    const std::wstring C = OwnerPanelPath(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    const std::wstring B07 = OwnerPanelPath(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_fald_panel_boost_meanrule_blur0.7.bin");
    const std::wstring SDR = OwnerPanelPath(L"DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin");
    const std::wstring ODD(cells.begin(), cells.end());
    REQUIRE_FALSE(C.empty());
    auto want = [&](const char* name) { return only.empty() || std::string(name).find(only) != std::string::npos; };
    int compared = 0, equal = 0;

    // ---- stills
    struct Still { const char* name; const char* cell; std::wstring panel; bool hdr, star; unsigned w, h; };
    const Still stills[] = {
        { "still_n7_lamp", "n7_lamp", C, true, true, 3840, 2160 },
        // round 12a's three (F12A_B16), when the cell dir holds them
        { "still_aces_m18", "aces_m18", HaveCell(cells, "aces_m18") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_aces_m52", "aces_m52", HaveCell(cells, "aces_m52") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_s10_ac_redspot", "s10_ac_redspot", HaveCell(cells, "s10_ac_redspot") ? C : std::wstring(), true, true, 3840, 2160 },
        // round 12b's ten halo stills (F12B_V8d: dark objects next to glows, the R-2 band frame), when the cell dir holds them
        { "still_ax_552", "ax_552", HaveCell(cells, "ax_552") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_ax_412", "ax_412", HaveCell(cells, "ax_412") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_ax_422", "ax_422", HaveCell(cells, "ax_422") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_ax_442", "ax_442", HaveCell(cells, "ax_442") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_ax_492", "ax_492", HaveCell(cells, "ax_492") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_ax_522", "ax_522", HaveCell(cells, "ax_522") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_s10_sm_windmill", "s10_sm_windmill", HaveCell(cells, "s10_sm_windmill") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_s10_sm_cows", "s10_sm_cows", HaveCell(cells, "s10_sm_cows") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_s10_sm_snowfence", "s10_sm_snowfence", HaveCell(cells, "s10_sm_snowfence") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_s10_ac_rec2020", "s10_ac_rec2020", HaveCell(cells, "s10_ac_rec2020") ? C : std::wstring(), true, true, 3840, 2160 },
        { "still_x_naninf", "x_naninf", C, true, false, 3840, 2160 },
        { "still_x_odd1366", "x_odd1366", ODD + L"\\odd_panel.bin", true, true, 1366, 768 },
        { "still_sdr_n7_lamp", "n7_lamp", SDR, false, true, 3840, 2160 },
        // a fractional zone pitch (48 x 24 over 2560 x 1440), when the cell dir holds it (test_fald_fade.cpp still_frac2560)
        { "still_frac2560", "x_frac2560",
          GetFileAttributesW((ODD + L"\\frac_panel_48x24.bin").c_str()) != INVALID_FILE_ATTRIBUTES ? ODD + L"\\frac_panel_48x24.bin"
                                                                                                    : std::wstring(),
          true, true, 2560, 1440 } };
    for (const Still& s : stills) {
        if (!want(s.name) || s.panel.empty()) continue;
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(s.panel, s.hdr);
        REQUIRE(staged.ok);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, s.w, s.h, s.hdr, s.star);
        REQUIRE(hm.Load(cells + "/" + s.cell + ".rgba16f"));
        HookMonitor::Result r;
        const int presents = WarmUp(hm, r);
        CHECK_MESSAGE(presents > 1, s.name << ": the fade was on at the first present (the creation is not paced)");
        REQUIRE_MESSAGE(presents > 0, s.name << ": the fade never turned on (" << r.err << ")");
        MESSAGE(std::string(s.name) << ": the fade on at present " << presents);
        CompareWithOverlay(hm, r.fade, on + "/" + s.name + "/t00", s.name, compared, equal);
    }

    // ---- the seattle pan: 16 content frames, presents without new content after frames 3 and 9
    if (want("seq_plain")) {
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(C, true);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, 3840, 2160, true, true);
        HookMonitor::Result r;
        for (int t = 0; t < 16; t++) {
            REQUIRE(hm.Load(SeqFrame(cells, t)));
            if (t == 0) { REQUIRE(WarmUp(hm, r) > 0); }
            else { r = hm.Present(true, false); REQUIRE_MESSAGE((r.ran && r.fadeOn), "seq_plain t" << t << ": " << r.err); }
            CompareWithOverlay(hm, r.fade, RefDir(on, "seq_s10_sm_seattle__p6n", t), "seq_plain t" + std::to_string(t), compared, equal);
            if (t == 3 || t == 9) {   // DWM recomposes with no dirty rect: nothing steps, the same output
                r = hm.Present(false, false);
                REQUIRE(r.ran);
                CompareWithOverlay(hm, r.fade, RefDir(on, "seq_s10_sm_seattle__p6n", t), "seq_plain repeat t" + std::to_string(t),
                                   compared, equal);
            }
        }
    }

    // ---- a live panel swap at frame 8 (the hot reload): the fade moves with the frame part and its T state resets
    if (want("seq_swap") && !B07.empty()) {
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(C, true);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, 3840, 2160, true, true);
        HookMonitor::Result r;
        std::vector<unsigned char> b07;
        REQUIRE(ReadAll(std::string(B07.begin(), B07.end()), b07));
        FaldFade* before = nullptr;
        for (int t = 0; t < 16; t++) {
            REQUIRE(hm.Load(SeqFrame(cells, t)));
            if (t == 8) REQUIRE(FaldTestDeliver(0, 0, true, 1u, b07.data(), b07.size()));
            if (t == 0) { REQUIRE(WarmUp(hm, r) > 0); }
            else { r = hm.Present(true, false); REQUIRE_MESSAGE((r.ran && r.fadeOn), "seq_swap t" << t << ": " << r.err); }
            if (t == 7) before = r.fade;
            if (t == 8) CHECK_MESSAGE(r.fade == before, "the swap rebuilt the fade's resources instead of moving them");
            if (t < 8) CompareWithOverlay(hm, r.fade, RefDir(on, "seq_s10_sm_seattle__p6n", t), "seq_swap t" + std::to_string(t), compared, equal);
            else CompareWithOverlay(hm, r.fade, RefDir(on, "ref_seattle_from8_B07", t - 8), "seq_swap t" + std::to_string(t), compared, equal);
        }
    }

    // ---- an idle reset at frame 8 (FaldMarkStale: the copy unprimed, T reset) and a re-injection at frame 12
    if (want("seq_reset")) {
        HookWarp* w = new HookWarp();
        REQUIRE(w->ok);
        Staged staged(C, true);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor* hm = new HookMonitor(*w, 3840, 2160, true, true);
        HookMonitor::Result r;
        for (int t = 0; t < 16; t++) {
            if (t == 12) {   // re-injection: everything of the DLL released, a fresh init on the same device, the files staged again
                FaldReleaseAll();
                FaldReleaseShaders();
                REQUIRE(FaldInitShaders(w->dev, w->ic));
                REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
            }
            REQUIRE(hm->Load(SeqFrame(cells, t)));
            if (t == 8) FaldMarkStale(0, 0);
            if (t == 0 || t == 12) { REQUIRE(WarmUp(*hm, r) > 0); }
            else {
                r = hm->Present(true, false);
                if (!r.ran && r.err == "unprimed") r = hm->Present(true, false);   // (the stale mark costs one present)
                REQUIRE_MESSAGE((r.ran && r.fadeOn), "seq_reset t" << t << ": " << r.err);
            }
            const char* ref = t < 8 ? "seq_s10_sm_seattle__p6n" : t < 12 ? "ref_seattle_from8_C" : "ref_seattle_from12_C";
            const int rt = t < 8 ? t : t < 12 ? t - 8 : t - 12;
            CompareWithOverlay(*hm, r.fade, RefDir(on, ref, rt), "seq_reset t" + std::to_string(t), compared, equal);
        }
        delete hm;
        delete w;
    }
    MESSAGE("hook vs overlay: " << equal << " of " << compared << " files byte-identical");
}

// The change-driven path in the hook (hook_fald.h FaldPlanPartial; shared/fald_change.h): presents with small dirty rects
// after a full one, the layer run twice over the same sequence — once with the path forced off, once by its rules — and
// the back buffer compared present for present (the overlay's case holds every field). The intermediate receives only the
// rects on the partial path, as hook_render.cpp draws.
TEST_CASE("FALD hook: the change-driven path equals the full path present for present (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cells = HookEnv("FALD_TEST_FADE_DIR");
    if (cells.empty()) return;
    const std::wstring C = OwnerPanelPath(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    struct Step { RECT rc; uint16_t value; const char* what; };
    const Step steps[] = {
        { { 1000, 1500, 1040, 1530 }, 0x0400, "a dark 40 x 30 patch: no drive moves" },
        { { 300, 300, 500, 500 }, 0x3C00, "a 200 x 200 white patch: drives move" },
        { { 0, 0, 1, 1 }, 0x0000, "the kick pixel (no content change)" },
        { { 300, 300, 500, 500 }, 0x0000, "the white patch to black" },
        { { 2000, 1000, 2002, 1002 }, 0x3C00, "a 2 x 2 white speck" },
    };
    // the always-on core (shared/fald_core_mode.h): C, KC3 and KC5 (the default) — the two stateless rules whose fields the path reuses
    for (unsigned int coreMode : { FALD_CORE_MODE_C, FALD_CORE_MODE_KC3, FALD_CORE_MODE_KC5 })
    for (int star = 0; star < 2; star++) {
        std::vector<std::vector<unsigned char>> outs[2];
        int partialPresents = 0;
        for (int forced = 0; forced < 2; forced++) {
            g_faldPartialOverride = forced == 0 ? 0 : -1;
            HookWarp w;
            REQUIRE(w.ok);
            Staged staged(C, true);
            REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
            HookMonitor hm(w, 3840, 2160, true, star != 0);
            hm.fadeWanted = false;
            hm.coreMode = coreMode;
            REQUIRE(hm.Load(cells + "/n7_lamp.rgba16f"));
            HookMonitor::Result r;
            for (int i = 0; i < 3; i++) { r = hm.Present(true, false); if (r.ran) break; }   // primed on the first full present
            REQUIRE_MESSAGE(r.ran, r.err);
            outs[forced].push_back(ReadBackTex(w.dev, w.ic, hm.back));
            for (const Step& s : steps) {
                for (LONG y = s.rc.top; y < s.rc.bottom; y++)
                    for (LONG x = s.rc.left; x < s.rc.right; x++)
                        for (int c = 0; c < 3; c++) std::memcpy(&hm.frame[((size_t)y * 3840 + x) * 8 + c * 2], &s.value, 2);
                std::vector<RECT> rects = { s.rc };
                r = hm.Present(true, false, &rects);
                REQUIRE_MESSAGE(r.ran, std::string(s.what) << ": " << r.err);
                if (forced == 1 && r.partial) partialPresents++;
                outs[forced].push_back(ReadBackTex(w.dev, w.ic, hm.back));
            }
            g_faldPartialOverride = -1;
        }
        REQUIRE(outs[0].size() == outs[1].size());
        for (size_t i = 0; i < outs[0].size(); i++)
            CHECK_MESSAGE(outs[0][i] == outs[1][i], "core mode " << coreMode << " star " << star << ": present " << i << " differs between the full and the change-driven path");
        CHECK_MESSAGE(partialPresents == (int)(sizeof(steps) / sizeof(steps[0])), "core mode " << coreMode << " star " << star << ": every present after the first should be partial (" << partialPresents << ")");
    }
}

#ifdef FALD_RUN_HAS_FRAME_KEY
// The fade's safety governor in the hook (shared/fald_fade_governor.h): WARP is a software adapter, so without the force
// bit the governor never starts the fade — the layer runs as C (CB word 75 = 0, nothing built) and its output equals the
// layer with the fade switched off, byte for byte; with the force bit it runs.
TEST_CASE("FALD hook: a fade the governor holds back is C byte for byte (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cells = HookEnv("FALD_TEST_FADE_DIR");
    if (cells.empty()) return;
    const std::wstring ODD = std::wstring(cells.begin(), cells.end()) + L"\\odd_panel.bin";
    auto run = [&](bool fade, bool force, int presents, HookMonitor::Result& last) {
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(ODD, true);
        REQUIRE(staged.ok);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, 1366, 768, true, true);
        hm.fadeWanted = fade;
        hm.forceFade = force;
        REQUIRE(hm.Load(cells + "/x_odd1366.rgba16f"));
        for (int i = 0; i < presents; i++) {
            last = hm.Present(true, true);
            if (!last.ran && last.err == "unprimed") { i--; continue; }
            REQUIRE_MESSAGE(last.ran, last.err);
        }
        return ReadBackTex(w.dev, w.ic, hm.back);
    };
    HookMonitor::Result r;
    const std::vector<unsigned char> C = run(false, false, 3, r);
    CHECK(r.govState == 0u);                               // Off: the setting is off
    const std::vector<unsigned char> held = run(true, false, 40, r);
    CHECK(r.govState == 3u);                               // SuspendedWeakGpu
    CHECK_FALSE(r.fadeOn);
    CHECK(r.fade == nullptr);
    CHECK(r.word75 == 0u);
    REQUIRE(!C.empty());
    CHECK(held == C);
    const std::vector<unsigned char> forced = run(true, true, 40, r);
    CHECK(r.govState == 4u);                               // Forced
    CHECK(r.fadeOn);
    CHECK(r.word75 == 1u);
    CHECK(forced != C);
}

// The core governor's cap in the hook (shared/fald_core_governor.h): a held run keeps the entry's fields, reproduces the
// full run's output on the same frame, corrects a new frame with the old fields, and leaves no residue — the full run
// after it equals a fresh entry's full run on the same frames.
TEST_CASE("FALD hook: a held run (the core governor's cap) keeps the fields and corrects the new frame with them (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cells = HookEnv("FALD_TEST_FADE_DIR");
    if (cells.empty()) return;
    const std::wstring ODD = std::wstring(cells.begin(), cells.end()) + L"\\odd_panel.bin";
    const int saved = g_faldCoreHoldOverride;
    auto present = [&](HookMonitor& hm) {
        HookMonitor::Result r;
        for (int i = 0; i < 3; i++) { r = hm.Present(true, false); if (r.ran || r.err != "unprimed") break; }
        REQUIRE_MESSAGE(r.ran, r.err);
        return r;
    };
    auto whitePatch = [](std::vector<unsigned char>& f) {   // a 200 x 200 white patch (FP16 1.0 = 0x3C00, little-endian)
        for (int y = 100; y < 300; y++)
            for (int x = 100; x < 300; x++)
                for (int c = 0; c < 3; c++) { f[((size_t)y * 1366 + x) * 8 + c * 2] = 0x00; f[((size_t)y * 1366 + x) * 8 + c * 2 + 1] = 0x3C; }
    };
    std::vector<unsigned char> fullA, heldB, fullB, refB;
    {
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(ODD, true);
        REQUIRE(staged.ok);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, 1366, 768, true, false);
        hm.fadeWanted = false;
        REQUIRE(hm.Load(cells + "/x_odd1366.rgba16f"));
        g_faldCoreHoldOverride = 0;
        HookMonitor::Result r = present(hm);                   // full (primed)
        CHECK(r.heldRuns == 0ull);
        fullA = ReadBackTex(w.dev, w.ic, hm.back);
        g_faldCoreHoldOverride = 1;
        r = present(hm);                                       // held on the same frame: the same output
        CHECK(r.heldRuns == 1ull);
        CHECK(ReadBackTex(w.dev, w.ic, hm.back) == fullA);
        whitePatch(hm.frame);
        r = present(hm);                                       // held on the new frame: A's fields, B's picture
        CHECK(r.heldRuns == 2ull);
        heldB = ReadBackTex(w.dev, w.ic, hm.back);
        CHECK(heldB != fullA);
        g_faldCoreHoldOverride = 0;
        r = present(hm);                                       // the full run after: B's fields
        CHECK(r.heldRuns == 2ull);
        CHECK(r.coreGovState == 1u);                           // Running (the seam held, not the governor)
        fullB = ReadBackTex(w.dev, w.ic, hm.back);
        CHECK(fullB != heldB);
    }
    {   // no residue: a fresh entry's full runs on A then B give the same B
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(ODD, true);
        REQUIRE(staged.ok);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, 1366, 768, true, false);
        hm.fadeWanted = false;
        REQUIRE(hm.Load(cells + "/x_odd1366.rgba16f"));
        present(hm);
        whitePatch(hm.frame);
        present(hm);
        refB = ReadBackTex(w.dev, w.ic, hm.back);
    }
    REQUIRE(!fullB.empty());
    CHECK(fullB == refB);
    g_faldCoreHoldOverride = saved;
}
#endif

// ---------------------------------------------------------------------------------------------------------------------
// The panel clock (LED lag mode 3, shared/fald_temporal.h) in the hook's layer, present for present: the hook's full path
// against the overlay's (src/fald.cpp on its own WARP device: the reference tests/test_fald_clock_gate.cpp holds to main),
// and the hook's change-driven path against its full path. A SCHEDULED k sequence as the overlay's gate (the hook's
// tuning admits refresh periods below 1000 ms: 900 here, on the odd 1366 x 768 panel so three runs fit a period).
// (2026-10-22: the clock pass's B_true map went into round 0's slot while round 1's convolution read round 1's.)
// ---------------------------------------------------------------------------------------------------------------------
#include "../src/fald.h"          // the overlay's layer (FALD_RUN_HAS_RECTS: the change-driven path is built)
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_temporal.h"
#ifdef FALD_RUN_HAS_RECTS

namespace {

long long HkQpc() { LARGE_INTEGER t; QueryPerformanceCounter(&t); return t.QuadPart; }
long long HkQpcFreq() { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f.QuadPart; }
void HkWaitUntil(long long targetQpc) {
    const long long f = HkQpcFreq();
    for (;;) {
        const long long now = HkQpc();
        if (now >= targetQpc) return;
        if ((double)(targetQpc - now) * 1000.0 / (double)f > 3.0) Sleep(1); else YieldProcessor();
    }
}
struct HkStep { int k; RECT rc; uint16_t value; bool newContent, fullRect; const char* what; };
const HkStep kClockSteps[] = {
    { 0, { 0, 0, 0, 0 }, 0, true, true, "the first frame: the seeding run" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still (settle)" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still (settle)" },
    { 2, { 200, 150, 600, 450 }, 0x3C00, true, true, "a new frame (a white block)" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still" },
    { 1, { 300, 500, 340, 530 }, 0x0400, true, false, "a dark 40 x 30 patch" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still" },
    { 3, { 200, 150, 600, 450 }, 0x0000, true, true, "a new frame after three refreshes (the block black)" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still" },
    { 2, { 0, 0, 0, 0 }, 0, false, false, "still after two refreshes" },
    { 1, { 100, 100, 300, 300 }, 0x3C00, true, false, "a 200 x 200 white patch" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still" },
    { 1, { 700, 500, 702, 502 }, 0x3C00, true, false, "a 2 x 2 white speck" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still" },
    { 1, { 0, 0, 0, 0 }, 0, false, false, "still" },
};
const int kClockStepCount = (int)(sizeof(kClockSteps) / sizeof(kClockSteps[0]));
const float kClockPeriodMs = 900.0f;
const int kClockW = 1366, kClockH = 768;

void HkPatch(std::vector<unsigned char>& f, const RECT& rc, uint16_t value) {
    for (LONG y = rc.top; y < rc.bottom; y++)
        for (LONG x = rc.left; x < rc.right; x++)
            for (int c = 0; c < 3; c++) std::memcpy(&f[((size_t)y * kClockW + x) * 8 + c * 2], &value, 2);
}

// The overlay's layer in mode 3 on its own WARP device (the reference of the hook's output).
struct OverlayClockRef {
    ID3D11Device* dev = nullptr; ID3D11DeviceContext* ic = nullptr; ID3D11VertexShader* vs = nullptr;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    MonitorContext ctx;
    bool ok = false;
    explicit OverlayClockRef(const std::wstring& panel) {
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
        if (!vs || !InitFaldShaders()) return;
        ctx.width = kClockW; ctx.height = kClockH; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = kClockPeriodMs;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = true;
        fs.coreMode = FALD_CORE_MODE_C;   // the overlay reference runs C, as the hook harness's monitors do (coreMode 0)
        fs.temporalMode = FALD_TEMPORAL_PANEL; fs.clockParity = -1; fs.clockClosure = FALD_CLOCK_CLOSURE_DEFAULT;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = kClockW; td.Height = kClockH; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(dev->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~OverlayClockRef() {
        if (rtv) rtv->Release();
        if (target) target->Release();
        FaldReleaseResources(&ctx);
        ReleaseFaldShaders();
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
    std::vector<unsigned char> Run(const std::vector<unsigned char>& frame, bool newContent) {
        D3D11_VIEWPORT vp = { 0, 0, (float)kClockW, (float)kClockH, 0, 1 };
        ic->RSSetViewports(1, &vp);
        ic->VSSetShader(vs, nullptr, 0);
        ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)kClockW * 8u, 0);
        FaldRunPasses(&ctx, rtv, newContent, 0x9E3779B97F4A7C15ull);
        ic->Flush();
        return ReadBackTex(dev, ic, target);
    }
};

}  // namespace

TEST_CASE("FALD hook: the panel clock (LED lag mode 3) equals the overlay's and its change-driven path's, present for present (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cells = HookEnv("FALD_TEST_FADE_DIR");
    if (cells.empty()) return;
    const std::wstring ODD = std::wstring(cells.begin(), cells.end()) + L"\\odd_panel.bin";
    std::vector<unsigned char> base;
    REQUIRE(ReadAll(cells + "/x_odd1366.rgba16f", base));
    REQUIRE(base.size() == (size_t)kClockW * kClockH * 8u);
    const long long f = HkQpcFreq();
    const long long periodQpc = (long long)((double)kClockPeriodMs * (double)f / 1000.0);
    DwmHookFaldTuning tuning = {};
    tuning.starEven = 0.8f; tuning.starTargetGain = 1.0f; tuning.starKeepNits = 100.0f; tuning.starStrength = 1.0f;
    tuning.starAreaLo = 40.0f; tuning.starAreaHi = 160.0f; tuning.starNbLo = 0.15f; tuning.starNbHi = 0.30f;
    tuning.starReach = 2u; tuning.starEvenReach = 8u;
    tuning.tempMode = FALD_TEMPORAL_PANEL; tuning.tempClockClosure = FALD_CLOCK_CLOSURE_DEFAULT; tuning.tempClockParity = -1;
    tuning.refreshMs = kClockPeriodMs;
    // three passes on the same schedule: the overlay (full), the hook's full path, the hook's change-driven path
    std::vector<std::vector<unsigned char>> outs[3];
    std::vector<unsigned long long> ks[3];
    int partialPresents = 0;
    for (int pass = 0; pass < 3; pass++) {
        g_faldPartialOverride = pass == 2 ? -1 : 0;
        long long N = 0;
        if (pass == 0) {
            std::vector<unsigned char> frame = base;
            OverlayClockRef ov(ODD);
            REQUIRE(ov.ok);
            const long long t0 = HkQpc() + periodQpc / 4;   // the schedule starts after the shaders compiled
            for (int i = 0; i < kClockStepCount; i++) {
                const HkStep& s = kClockSteps[i];
                if (s.newContent && i > 0) HkPatch(frame, s.rc, s.value);
                N += s.k;
                HkWaitUntil(t0 + N * periodQpc);
                outs[0].push_back(ov.Run(frame, s.newContent));
                ks[0].push_back(i == 0 ? 0ull : ov.ctx.fald->clkElapsed);
                const bool seedOk = i == 0 ? ov.ctx.fald->clkSeeded : !ov.ctx.fald->clkSeeded;
                CHECK_MESSAGE(seedOk, "overlay " << i << " (" << std::string(s.what) << "): seeded " << ov.ctx.fald->clkSeeded);
            }
        } else {
            HookWarp w;
            REQUIRE(w.ok);
            Staged staged(ODD, true);
            REQUIRE(staged.ok);
            REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
            HookMonitor hm(w, kClockW, kClockH, true, true);
            hm.fadeWanted = false;
            hm.tuning = &tuning;
            hm.frame = base;
            const long long t0 = HkQpc() + periodQpc / 4;   // the schedule starts after the shaders compiled
            for (int i = 0; i < kClockStepCount; i++) {
                const HkStep& s = kClockSteps[i];
                if (s.newContent && i > 0) HkPatch(hm.frame, s.rc, s.value);
                std::vector<RECT> rects;
                if (s.newContent && !s.fullRect) rects.push_back(s.rc);
                N += s.k;
                HookMonitor::Result r;
                HkWaitUntil(t0 + N * periodQpc);
                if (i == 0) {
                    for (int a = 0; a < 3; a++) { r = hm.Present(true, false); if (r.ran) break; }   // primed on the first full present
                } else {
                    r = hm.Present(s.newContent, false, rects.empty() ? nullptr : &rects);
                }
                REQUIRE_MESSAGE(r.ran, "hook pass " << pass << " step " << i << ": " << r.err);
                outs[pass].push_back(ReadBackTex(w.dev, w.ic, hm.back));
                ks[pass].push_back(i == 0 ? 0ull : r.clockElapsed);
                const bool seedOk = i == 0 ? r.clockSeeded : !r.clockSeeded;
                CHECK_MESSAGE(seedOk, "hook pass " << pass << " step " << i << ": seeded " << r.clockSeeded);
                if (pass == 2) {
                    // the seed and the first clock run after it (the routing rule) are full, as is a whole new frame (the cap)
                    const bool expectPartial = i >= 2 && !s.fullRect;
                    CHECK_MESSAGE(r.lastPartial == expectPartial, "hook change-driven step " << i << " (" << std::string(s.what) << "): partial " << r.lastPartial << ", expected " << expectPartial);
                    if (r.lastPartial) partialPresents++;
                }
            }
        }
        g_faldPartialOverride = -1;
    }
    for (int i = 0; i < kClockStepCount; i++) {
        const HkStep& s = kClockSteps[i];
        const bool kOk = ks[0][i] == (unsigned long long)s.k && ks[1][i] == (unsigned long long)s.k && ks[2][i] == (unsigned long long)s.k;
        CHECK_MESSAGE(kOk, "step " << i << " (" << std::string(s.what) << "): k overlay " << ks[0][i] << ", hook " << ks[1][i] << ", hook change-driven " << ks[2][i]
                      << " (target " << s.k << ": the schedule slipped - rerun)");
        REQUIRE(!outs[0][i].empty());
        CHECK_MESSAGE(outs[1][i] == outs[0][i], "step " << i << " (" << std::string(s.what) << "): the hook's output differs from the overlay's");
        CHECK_MESSAGE(outs[2][i] == outs[1][i], "step " << i << " (" << std::string(s.what) << "): the hook's change-driven output differs from its full path's");
    }
    MESSAGE("hook panel clock: " << partialPresents << " of " << kClockStepCount << " presents took the change-driven path");
}

// The dynamic tone map's peak in the hook (hook_fald.h FaldRun frameChanged; shared/peak_detect.h): the LUT pass is the
// same function of every pixel between two presents unless the smoothed peak MOVED, which the smoothing pass decides on
// the GPU and writes as the pass's draw arguments (4, moved, 0, 0). The layer copies `moved` into its change flags: set,
// the whole frame counts as changed and every sweep runs in full (the hook's LUT pass drew the whole frame); clear, only
// the rects changed and the change-driven path stands. Here the word is a test buffer, the "tone map" an edit of the
// whole frame, and the presents compare with the full path's present for present: with the word the partial path is
// exact whatever changed; without it (the negative control) a whole-frame change under a small rect would be wrong —
// which is why a dynamic peak keyed 0 before 2026-10-22 and never engaged the path at all.
TEST_CASE("FALD hook: the frame-changed word (the dynamic peak) keeps the change-driven path exact when the whole frame moved (WARP; FALD_TEST_FADE_DIR)") {
    const std::string cells = HookEnv("FALD_TEST_FADE_DIR");
    if (cells.empty()) return;
    const std::wstring ODD = std::wstring(cells.begin(), cells.end()) + L"\\odd_panel.bin";
    std::vector<unsigned char> base;
    REQUIRE(ReadAll(cells + "/x_odd1366.rgba16f", base));
    REQUIRE(base.size() == (size_t)kClockW * kClockH * 8u);
    {   // a mid-grey region (320 nits: drives between the floor and the ceiling), so a scale of the whole frame moves
        // zone drives the rects never touch — what the negative control needs to show; and a black band at the bottom,
        // where the control's rects lie: a black rect in a black zone moves no statistic (no star / drive flag on the
        // GPU), so without the word nothing downstream runs and the stale output shows
        const uint16_t grey = 0x4400, black = 0;
        for (LONG y = 50; y < 350; y++)
            for (LONG x = 800; x < 1300; x++)
                for (int c = 0; c < 3; c++) std::memcpy(&base[((size_t)y * kClockW + x) * 8 + c * 2], &grey, 2);
        for (LONG y = 500; y < kClockH; y++)   // (the odd panel: 17 x 9 zones of 80 x 85 px; rows 510-765 wholly black)
            for (LONG x = 0; x < kClockW; x++)
                for (int c = 0; c < 3; c++) std::memcpy(&base[((size_t)y * kClockW + x) * 8 + c * 2], &black, 2);
    }
    // the whole frame scaled (every pixel's half multiplied in float, re-packed): "the tone map's peak moved"
    auto scaleAll = [](std::vector<unsigned char>& f, float k) {
        for (size_t i = 0; i + 1 < f.size(); i += 2) {
            if ((i / 2) % 4 == 3) continue;                                    // alpha untouched
            uint16_t h; std::memcpy(&h, &f[i], 2);
            const uint32_t s = (h >> 15) & 1u, e = (h >> 10) & 0x1Fu, m = h & 0x3FFu;
            float v = e == 0 ? (float)m * (1.0f / 16777216.0f) : (e == 31 ? 0.0f : std::ldexp(1.0f + (float)m / 1024.0f, (int)e - 15));
            if (s) v = -v;
            v *= k;
            uint16_t o = 0;
            if (v != 0.0f) {
                const float av = v < 0 ? -v : v;
                int ex; const float fr = std::frexp(av, &ex);                   // av = fr * 2^ex, fr in [0.5, 1)
                const int e2 = ex - 1 + 15;
                if (e2 >= 1 && e2 <= 30) o = (uint16_t)((v < 0 ? 0x8000u : 0u) | ((uint32_t)e2 << 10) | ((uint32_t)((fr * 2.0f - 1.0f) * 1024.0f + 0.5f) & 0x3FFu));
            }
            std::memcpy(&f[i], &o, 2);
        }
    };
    // scale = the tone map's current gain on the whole frame (its "peak"): a step where it differs from the previous
    // step's is a present on which the peak MOVED (the word 1, the whole frame through the pass); the same scale = the
    // pass stands (the word 0, the rect alone)
    struct Step { RECT rc; uint16_t value; float scale; const char* what; };
    const Step steps[] = {
        { { 300, 650, 340, 680 }, 0x0000, 1.0f, "a kick in a black zone, the pass as it was" },
        { { 300, 650, 340, 680 }, 0x0000, 0.5f, "the same kick, the pass now 0.5 x on the whole frame (the peak moved)" },
        { { 100, 100, 300, 300 }, 0x3C00, 0.5f, "a white patch, the pass as it was" },
        { { 700, 650, 702, 652 }, 0x0000, 2.0f, "a kick in a black zone and the pass 2 x (moved)" },
        { { 700, 650, 702, 652 }, 0x0000, 2.0f, "the same kick, the pass as it was" },
        { { 700, 650, 702, 652 }, 0x0000, 0.25f, "the same kick, the pass 0.25 x (moved)" },
    };
    const int n = (int)(sizeof(steps) / sizeof(steps[0]));
    // three passes: the full path; the change-driven path with the word; the change-driven path WITHOUT the word on a
    // whole-frame change (the negative control: it must differ on the scaled steps — the word is load-bearing)
    std::vector<std::vector<unsigned char>> outs[3];
    std::vector<bool> partials[3];
    for (int pass = 0; pass < 3; pass++) {
        g_faldPartialOverride = pass == 0 ? 0 : -1;
        HookWarp w;
        REQUIRE(w.ok);
        Staged staged(ODD, true);
        REQUIRE(staged.ok);
        REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
        HookMonitor hm(w, kClockW, kClockH, true, true);
        hm.fadeWanted = false;
        hm.frame = base;
        // the word's buffer: (4, moved, 0, 0) as the smoothing pass writes it
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16; bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_DRAWINDIRECT_ARGS | D3D11_RESOURCE_MISC_BUFFER_ALLOW_RAW_VIEWS;
        const uint32_t init[4] = { 4u, 0u, 0u, 0u };
        D3D11_SUBRESOURCE_DATA sd = { init, 0, 0 };
        ID3D11Buffer* word = nullptr;
        REQUIRE(SUCCEEDED(w.dev->CreateBuffer(&bd, &sd, &word)));
        hm.frameChangedBuf = pass == 1 ? word : nullptr;
        HookMonitor::Result r;
        for (int i = 0; i < 3; i++) { r = hm.Present(true, false); if (r.ran) break; }   // primed on the first full present
        REQUIRE_MESSAGE(r.ran, r.err);
        outs[pass].push_back(ReadBackTex(w.dev, w.ic, hm.back)); partials[pass].push_back(r.lastPartial);
        std::vector<unsigned char> composed = base, passed;                    // the composed frame; what the pass made of it
        float prevScale = 1.0f;
        for (int i = 0; i < n; i++) {
            const Step& s = steps[i];
            for (LONG y = s.rc.top; y < s.rc.bottom; y++)
                for (LONG x = s.rc.left; x < s.rc.right; x++)
                    for (int c = 0; c < 3; c++) std::memcpy(&composed[((size_t)y * kClockW + x) * 8 + c * 2], &s.value, 2);
            hm.frame = composed;                                               // the clean copy's rects
            passed = composed;
            if (s.scale != 1.0f) scaleAll(passed, s.scale);
            hm.intermediateFrame = &passed;
            const bool moved = s.scale != prevScale;
            prevScale = s.scale;
            const uint32_t movedWord[4] = { 4u, moved ? 1u : 0u, 0u, 0u };
            w.ic->UpdateSubresource(word, 0, nullptr, movedWord, 0, 0);
            hm.intermediateWhole = moved;                                      // the LUT pass's full-frame draw on a moved peak
            std::vector<RECT> rects = { s.rc };
            if (pass == 0) r = hm.Present(true, false);                        // the full path: the whole frame
            else r = hm.Present(true, false, &rects);
            REQUIRE_MESSAGE(r.ran, std::string(s.what) << ": " << r.err);
            outs[pass].push_back(ReadBackTex(w.dev, w.ic, hm.back)); partials[pass].push_back(r.lastPartial);
        }
        hm.intermediateFrame = nullptr;
        word->Release();
        g_faldPartialOverride = -1;
    }
    int partialWithWord = 0, movedSteps = 0;
    float prev = 1.0f;
    for (int i = 0; i <= n; i++) {
        const std::string what = i == 0 ? "the first frame" : steps[i - 1].what;
        const bool moved = i > 0 && steps[i - 1].scale != prev;
        if (i > 0) prev = steps[i - 1].scale;
        MESSAGE("step " << i << " (" << what << "): full " << HookFnvLine(outs[0][i]) << ", word " << HookFnvLine(outs[1][i]) << " partial "
                << (int)(bool)partials[1][i] << ", bare " << HookFnvLine(outs[2][i]) << " partial " << (int)(bool)partials[2][i]);
        CHECK_MESSAGE(outs[1][i] == outs[0][i], "with the word, step " << i << " (" << what << ") differs from the full path");
        if (i > 0) {
            CHECK_MESSAGE(partials[1][i], "with the word, step " << i << " (" << what << ") should be partial");
            if (partials[1][i]) partialWithWord++;
            if (moved) {
                movedSteps++;
                CHECK_MESSAGE(outs[2][i] != outs[0][i], "WITHOUT the word, step " << i << " (" << what << ") should be wrong (the negative control; partial "
                              << (int)(bool)partials[2][i] << ")");
            }
        }
    }
    MESSAGE("the dynamic peak: " << partialWithWord << " of " << n << " presents partial with the word, exact on all; the whole frame redone on "
            << movedSteps << " of them by the GPU's own decision");
}
// The always-on core round 2 — PROTOTYPE (shared/fald_core2.h; faldFlags bit 16): the hook's layer with the core on equals
// the overlay's, BYTE FOR BYTE, on the frames of DLC tests/test_fald_core2_warp.py (FALD_TEST_CORE2_DIR: panel.bin,
// fNN.rgba16f and the overlay's dumps dNN/fald_out.rgba16f from tests/test_fald_core2.cpp's case, run first). Both advance the
// zone IIR by one refresh per run (g_faldCore2TestRefreshes); the flank fade and the starfield are off.
#include "../shared/fald_core2.h"
TEST_CASE("FALD hook: the core round 2 in the DWM hook's layer equals the overlay's, frame for frame (WARP; FALD_TEST_CORE2_DIR)") {
    const std::string dir = HookEnv("FALD_TEST_CORE2_DIR");
    if (dir.empty()) return;
    std::ifstream probe(dir + "/d00/fald_out.rgba16f", std::ios::binary);
    if (!probe) return;                                    // the overlay's case has not run on this directory
    probe.close();
    HookWarp w;
    REQUIRE(w.ok);
    REQUIRE(FaldCore2ShadersReady());
    const std::wstring panel = std::wstring(dir.begin(), dir.end()) + L"\\panel.bin";
    Staged staged(panel, true);
    REQUIRE(staged.ok);
    REQUIRE(FaldLoadPanelFiles(staged.root.c_str()) == 1);
    HookMonitor hm(w, 960, 540, true, false);
    hm.fadeWanted = false;
    hm.forceFade = false;
    {
        char mv[8] = {};
        const bool got = GetEnvironmentVariableA("FALD_TEST_CORE2_MODE", mv, sizeof(mv)) > 0;
        hm.coreMode = (got && mv[0] == '2') ? 2u : ((got && mv[0] == '4') ? 4u : 1u);   // 1 KC2c (default), 2 KC3, 4 KC5
    }
    g_faldCore2TestRefreshes = 1;
    int compared = 0;
    for (int run = 0; run < 100; run++) {
        char name[64];
        snprintf(name, sizeof(name), "/f%02d.rgba16f", run);
        if (!hm.Load(dir + name)) break;
        HookMonitor::Result r;
        for (int tries = 0; tries < 50; tries++) {          // unprimed presents do not run the layer (no IIR step)
            r = hm.Present(true, false);
            if (r.ran || r.err != "unprimed") break;
        }
        REQUIRE_MESSAGE(r.ran, r.err);
        std::vector<unsigned char> ref;
        snprintf(name, sizeof(name), "/d%02d/fald_out.rgba16f", run);
        REQUIRE(ReadAll(dir + name, ref));
        const std::vector<unsigned char> got = ReadBackTex(w.dev, w.ic, hm.back);
        CHECK_MESSAGE(got == ref, "run " << run << ": the hook's output differs from the overlay's");
        compared++;
    }
    g_faldCore2TestRefreshes = 0;
    CHECK(compared > 0);
}

#endif
