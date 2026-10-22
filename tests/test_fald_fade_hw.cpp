// GPU timing of the FALD layer with and without the flank fade on a HARDWARE D3D11 device (standalone: its own device, no
// DWM, no DesktopLUT running), per run of the layer: content frames (new content each run), runs without new content on
// the same frame (a desktop's presents between content frames), and the first content frame with the fade on (the
// paced build's last run). Every run's output is hashed (FNV-1a 64), so two builds' outputs on the same device can be
// compared bit for bit. It compiles against main's fade too (the frame key / governor only where the build has them).
// Opt-in: FALD_HW_DIR = a directory with n7_lamp.rgba16f + s10_sm_seattle.rgba16f (3840 x 2160), x_frac2560.rgba16f +
// frac_panel_48x24.bin (2560 x 1440), hw1080_n7.rgba16f + hw1080_seattle.rgba16f + panel_1080p.bin (1920 x 1080);
// FALD_HW_OUT = the output directory (runs.csv); FALD_HW_N = content / still runs per case (default 30); FALD_HW_ONLY =
// a substring of the case ids; FALD_HW_ADAPTER = a substring of the adapter name (default "NVIDIA"). One run of the
// layer per submission and a wait for its queries (a test harness may wait; the layer never does); a run over 45 ms
// stops the case (the owner's GPU rule: no submitted batch over 50 ms).
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_fade.h"
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#pragma comment(lib, "dxgi.lib")

namespace {

std::string HwEnv(const char* name) {
    char* v = nullptr; size_t n = 0;
    if (_dupenv_s(&v, &n, name) != 0 || !v) return std::string();
    std::string s(v); free(v);
    return s;
}

struct HwDev {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11VertexShader* vs = nullptr;
    ID3D11Query* dj = nullptr;
    ID3D11Query* q0 = nullptr;
    ID3D11Query* q1 = nullptr;
    std::string adapter;
    bool ok = false;
    HwDev() {
        std::string want = HwEnv("FALD_HW_ADAPTER");
        if (want.empty()) want = "NVIDIA";
        IDXGIFactory1* fac = nullptr;
        if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&fac))) return;
        IDXGIAdapter1* pick = nullptr;
        for (UINT i = 0;; i++) {
            IDXGIAdapter1* a = nullptr;
            if (fac->EnumAdapters1(i, &a) == DXGI_ERROR_NOT_FOUND) break;
            DXGI_ADAPTER_DESC1 d; a->GetDesc1(&d);
            char name[256] = {};
            WideCharToMultiByte(CP_UTF8, 0, d.Description, -1, name, sizeof(name), nullptr, nullptr);
            if (!pick && std::string(name).find(want) != std::string::npos && !(d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
                pick = a; adapter = name;
            } else a->Release();
        }
        fac->Release();
        if (!pick) return;
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        const HRESULT hr = D3D11CreateDevice(pick, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic);
        pick->Release();
        if (FAILED(hr)) return;
        g_device = dev; g_context = ic;
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main",
                              "vs_5_0", 0, 0, &b, nullptr))) return;
        dev->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
        b->Release();
        D3D11_QUERY_DESC qd = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
        dev->CreateQuery(&qd, &dj);
        qd.Query = D3D11_QUERY_TIMESTAMP;
        dev->CreateQuery(&qd, &q0);
        dev->CreateQuery(&qd, &q1);
        ok = vs && dj && q0 && q1 && InitFaldShaders();
    }
    ~HwDev() {
        ReleaseFaldShaders();
        for (IUnknown* p : { (IUnknown*)vs, (IUnknown*)dj, (IUnknown*)q0, (IUnknown*)q1 }) if (p) p->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
};

bool HwReadFrame(const std::string& path, int w, int h, std::vector<uint16_t>& out) {
    out.assign((size_t)w * h * 4, 0);
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    f.read((char*)out.data(), (std::streamsize)(out.size() * sizeof(uint16_t)));
    return (size_t)f.gcount() == out.size() * sizeof(uint16_t);
}

uint64_t HwFnvTex(ID3D11Texture2D* tex) {
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return 0;
    g_context->CopyResource(st, tex);
    uint64_t h = 1469598103934665603ull;
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        for (UINT y = 0; y < d.Height; y++) {
            const uint8_t* row = (const uint8_t*)m.pData + (size_t)y * m.RowPitch;
            for (size_t i = 0; i < (size_t)d.Width * 8; i++) { h ^= row[i]; h *= 1099511628211ull; }
        }
        g_context->Unmap(st, 0);
    }
    st->Release();
    return h;
}

std::wstring HwOwnerPanel(const wchar_t* rel) {
    const std::wstring p = std::wstring(L"H:\\Projects\\DesktopLUT\\") + rel;
    return GetFileAttributesW(p.c_str()) != INVALID_FILE_ATTRIBUTES ? p : std::wstring();
}

struct HwCase { std::string id; std::wstring panel; std::vector<std::string> frames; int w, h; };

}  // namespace

TEST_CASE("FALD fade HW: GPU time per run of the layer, fade off / on, content / no new content (FALD_HW_DIR + FALD_HW_OUT)") {
    const std::string dir = HwEnv("FALD_HW_DIR"), out = HwEnv("FALD_HW_OUT"), only = HwEnv("FALD_HW_ONLY");
    if (dir.empty() || out.empty()) return;
    const int n = std::max(4, atoi(HwEnv("FALD_HW_N").empty() ? "30" : HwEnv("FALD_HW_N").c_str()));
    CreateDirectoryA(out.c_str(), nullptr);
    HwDev hw;
    REQUIRE(hw.ok);
    MESSAGE("adapter: " << hw.adapter);
    const std::wstring C = HwOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    auto widen = [](const std::string& s) { return std::wstring(s.begin(), s.end()); };
    const std::vector<HwCase> cases = {
        { "4k", C, { "n7_lamp", "s10_sm_seattle" }, 3840, 2160 },
        { "1440p", widen(dir + "/frac_panel_48x24.bin"), { "x_frac2560" }, 2560, 1440 },
        { "1080p", widen(dir + "/panel_1080p.bin"), { "hw1080_n7", "hw1080_seattle" }, 1920, 1080 } };
    std::ofstream csv(out + "/runs.csv");
    csv << "case,fade,phase,run,new_content,gpu_us,out_fnv,fade_gov_us\n";
    bool stop = false;
    for (const HwCase& hc : cases) {
        if (stop) break;
        if (!only.empty() && hc.id.find(only) == std::string::npos) continue;
        std::vector<std::vector<uint16_t>> frames;
        for (const std::string& f : hc.frames) {
            std::vector<uint16_t> v;
            REQUIRE_MESSAGE(HwReadFrame(dir + "/" + f + ".rgba16f", hc.w, hc.h, v), f);
            frames.push_back(std::move(v));
        }
        if (frames.size() == 1) {   // a second content frame: the first mirrored (a cut every frame: the T path's worst)
            std::vector<uint16_t> m(frames[0].size());
            for (int y = 0; y < hc.h; y++)
                for (int x = 0; x < hc.w; x++)
                    for (int c = 0; c < 4; c++) m[((size_t)y * hc.w + x) * 4 + c] = frames[0][((size_t)y * hc.w + (hc.w - 1 - x)) * 4 + c];
            frames.push_back(std::move(m));
        }
        for (int fade = 0; fade < 2 && !stop; fade++) {
            MonitorContext ctx;
            ctx.width = hc.w; ctx.height = hc.h; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
            ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = 41.708f;   // a 23.976 Hz grid
            FaldSettings& fs = ctx.hdrColorCorrection.fald;
            fs.enabled = true; fs.paramsPath = hc.panel; fs.star.enabled = true; fs.flankFade = fade != 0;
            REQUIRE(FaldEnsureResources(&ctx, fs));
            D3D11_TEXTURE2D_DESC td = {};
            td.Width = (UINT)hc.w; td.Height = (UINT)hc.h; td.MipLevels = 1; td.ArraySize = 1;
            td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
            td.BindFlags = D3D11_BIND_RENDER_TARGET;
            ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
            REQUIRE(SUCCEEDED(hw.dev->CreateTexture2D(&td, nullptr, &target)));
            REQUIRE(SUCCEEDED(hw.dev->CreateRenderTargetView(target, nullptr, &rtv)));
            unsigned long long key = 100;
            int frameIdx = 0;
            auto run = [&](const char* phase, int i, bool newContent) -> bool {
                D3D11_VIEWPORT vp = { 0, 0, (float)hc.w, (float)hc.h, 0, 1 };
                hw.ic->RSSetViewports(1, &vp);
                hw.ic->VSSetShader(hw.vs, nullptr, 0);
                hw.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
                if (newContent) {
                    hw.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frames[(size_t)frameIdx % frames.size()].data(), (UINT)hc.w * 8u, 0);
                    frameIdx++;
                    key++;
                }
                hw.ic->Flush();   // the upload is not part of the run
                hw.ic->Begin(hw.dj);
                hw.ic->End(hw.q0);
#ifdef FALD_RUN_HAS_FRAME_KEY
                g_faldFadeForce.store(true);   // the governor measures but never pauses this harness
                FaldRunPasses(&ctx, rtv, newContent, key);
#else
                FaldRunPasses(&ctx, rtv, newContent);
#endif
                hw.ic->End(hw.q1);
                hw.ic->End(hw.dj);
                hw.ic->Flush();
                D3D11_QUERY_DATA_TIMESTAMP_DISJOINT dj = {};
                const auto t0 = std::chrono::steady_clock::now();
                while (hw.ic->GetData(hw.dj, &dj, sizeof(dj), 0) != S_OK) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    if (std::chrono::steady_clock::now() - t0 > std::chrono::seconds(5)) { stop = true; return false; }
                }
                UINT64 a = 0, b = 0;
                while (hw.ic->GetData(hw.q0, &a, 8, 0) != S_OK) {}
                while (hw.ic->GetData(hw.q1, &b, 8, 0) != S_OK) {}
                const double us = (!dj.Disjoint && dj.Frequency) ? (double)(b - a) * 1e6 / (double)dj.Frequency : -1.0;
                double gov = -1.0;
#ifdef FALD_RUN_HAS_FRAME_KEY
                if (ctx.fald->fade) {   // the governor's own measurement of this content frame (read later, without waiting)
                    double s[4];
                    const unsigned int k = FaldFadeCollectTiming(ctx.fald->fade, hw.ic, s, 4);
                    if (k) gov = s[k - 1];
                }
#endif
                const uint64_t h = HwFnvTex(target);
                char hb[32]; snprintf(hb, sizeof(hb), "%016llx", (unsigned long long)h);
                csv << hc.id << "," << fade << "," << phase << "," << i << "," << (newContent ? 1 : 0) << "," << us << "," << hb << ","
                    << gov << "\n";
                if (us > 45000.0) { MESSAGE(hc.id << " " << phase << " run " << i << ": " << us << " us - stopping"); stop = true; }
                std::this_thread::sleep_for(std::chrono::milliseconds(8));   // the desktop gets the GPU between runs
                return !stop;
            };
            // the paced build (shaders, their warm-up, resources): content frames until the fade is on; the run that turns it
            // on is the first content frame with the fade
            int warm = 0;
            for (; warm < 400 && !stop && (fade ? !ctx.fald->fadeOn : warm < 4); warm++) run("build", warm, true);
            if (fade) {
                CHECK(ctx.fald->fadeOn);
                MESSAGE(hc.id << ": the fade on after " << warm << " runs, " << (FaldFadeVramBytes(ctx.fald->fade) >> 20) << " MB");
            }
            for (int i = 0; i < 4 && !stop; i++) run("warm", i, true);
            for (int i = 0; i < n && !stop; i++) run("content", i, true);
            for (int i = 0; i < n && !stop; i++) run("still", i, false);
            csv.flush();
            rtv->Release(); target->Release();
            FaldReleaseResources(&ctx);
        }
    }
#ifdef FALD_RUN_HAS_FRAME_KEY
    g_faldFadeForce.store(false);
#endif
    CHECK_FALSE(stop);
}
