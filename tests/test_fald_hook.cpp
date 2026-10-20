// The DWM hook's FALD layer WITH the flank fade (faldFlags bit 8) on WARP — dwm_hook/hook_fald.cpp compiled into this exe as
// the DLL builds it (DesktopLUT.Tests.vcxproj: DLUT_HOOK_TESTING = the test seams, no host events, a %TEMP% log), driven
// the way hook_render.cpp RenderLUT drives it with no LUT and no tone map: FaldAcquire -> FaldSetLiveSettings ->
// FaldUpdateClean (the present's dirty rects) -> the clean copy into the intermediate -> FaldRun into the back buffer ->
// FaldReturn. Its output and fade-stack fields must equal, BYTE FOR BYTE, the overlay path's WARP dumps of the same frames
// (tests/test_fald_fade.cpp "FALD fade ON", FALD_TEST_FADE_ON) — the hook and the overlay run the same passes.
//
// Cases: four stills (HDR with the starfield on; the sanitise cell with it off; the odd size on its own panel; ACM SDR);
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

#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
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
    struct Result { bool ran = false, fadeOn = false; uint32_t word75 = 0xFFFFFFFFu; FaldFade* fade = nullptr; std::string err; };
    Result Present(bool newContent, bool readCB) {
        Result r;
        if (newContent) w.ic->UpdateSubresource(back, 0, nullptr, frame.data(), W * 8u, 0);
        FaldMonitor* m = FaldAcquire(0, 0, hdr, W, H, DXGI_FORMAT_R16G16B16A16_FLOAT);
        if (!m) { r.err = "FaldAcquire: no layer"; return r; }
        FaldSetLiveSettings(m, 0, 0, star, nullptr, true);
        RECT full = { 0, 0, (LONG)W, (LONG)H };
        if (!FaldUpdateClean(m, back, newContent ? &full : nullptr, newContent ? 1 : 0)) {
            FaldReturn(m);
            FaldMarkStale(0, 0);
            r.err = "unprimed";
            return r;
        }
        w.ic->CopyResource(FaldIntermediateTexture(m), FaldCleanTexture(m));   // no LUT, no tone map (RenderLUT)
        r.ran = FaldRun(m, backRTV, newContent);
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

// Byte-compare this present's output + fade fields with the overlay's dump directory `ref`.
void CompareWithOverlay(HookMonitor& hm, FaldFade* fade, const std::string& ref, const std::string& what, int& compared, int& equal) {
    struct Item { const char* file; ID3D11Texture2D* tex; ID3D11Buffer* buf; };
    const FaldFadeDebug d = FaldFadeDebugViews(fade);
    const Item items[] = { { "out.rgba16f", hm.back, nullptr }, { "fade.f32", d.fade, nullptr }, { "fade_t.f32", d.fadeT, nullptr },
                           { "floor.f32", d.floorF, nullptr }, { "reach0.f32", d.reach[0], nullptr },
                           { "reach1.f32", d.reach[1], nullptr }, { "s.f32", d.s, nullptr }, { "mi.f32", d.mi, nullptr },
                           { "flags.u32", nullptr, d.flags } };
    for (const Item& it : items) {
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
        { "still_x_naninf", "x_naninf", C, true, false, 3840, 2160 },
        { "still_x_odd1366", "x_odd1366", ODD + L"\\odd_panel.bin", true, true, 1366, 768 },
        { "still_sdr_n7_lamp", "n7_lamp", SDR, false, true, 3840, 2160 } };
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
