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

// The GPU's clock: an idle RTX 5090 runs its SM at a few hundred MHz and needs load to boost, so a run of the layer
// after the harness's own gaps (upload, readback, hashing, sleeps) would be timed at whatever clock the gap left — a
// first fade frame at 80 ms or at 6 ms for the same work. Before each timed run the heater keeps the GPU busy for
// FALD_HW_HEAT_MS (default 12; 0 = off) in ~2 ms batches of integer ALU work (no memory traffic), each flushed alone.
struct HwHeater {
    ID3D11ComputeShader* cs = nullptr;
    ID3D11Buffer* buf = nullptr; ID3D11UnorderedAccessView* uav = nullptr; ID3D11Buffer* cb = nullptr;
    unsigned int iters = 256;
    bool ok = false;
    explicit HwHeater(HwDev& hw) {
        static const char* src = R"(
cbuffer C : register(b0) { uint iters; uint3 pad; };
RWStructuredBuffer<uint> o : register(u0);
[numthreads(256, 1, 1)] void main(uint3 id : SV_DispatchThreadID) {
    uint x = id.x;
    [loop] for (uint i = 0; i < iters; i++) { x = x * 1664525u + 1013904223u; }
    if (x == 0x12345678u) o[0] = x;
})";
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(src, std::strlen(src), "heater", nullptr, nullptr, "main", "cs_5_0", 0, 0, &b, nullptr))) return;
        hw.dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &cs);
        b->Release();
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16; bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; bd.StructureByteStride = 4;
        hw.dev->CreateBuffer(&bd, nullptr, &buf);
        if (buf) hw.dev->CreateUnorderedAccessView(buf, nullptr, &uav);
        D3D11_BUFFER_DESC cd = {};
        cd.ByteWidth = 16; cd.Usage = D3D11_USAGE_DEFAULT; cd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        hw.dev->CreateBuffer(&cd, nullptr, &cb);
        ok = cs && uav && cb;
        if (!ok) return;
        for (int k = 0; k < 6; k++) {   // calibrate to ~2 ms a batch at the clock this reaches
            const double us = TimeOne(hw);
            if (us <= 0.0) return;
            iters = (unsigned int)std::min(1u << 22, std::max(16u, (unsigned int)(iters * 2000.0 / us)));
        }
    }
    void Dispatch(HwDev& hw) {
        const unsigned int c[4] = { iters, 0, 0, 0 };
        hw.ic->UpdateSubresource(cb, 0, nullptr, c, 0, 0);
        hw.ic->CSSetShader(cs, nullptr, 0);
        hw.ic->CSSetConstantBuffers(0, 1, &cb);
        hw.ic->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
        hw.ic->Dispatch(4096, 1, 1);
        ID3D11UnorderedAccessView* nu = nullptr; ID3D11Buffer* nb = nullptr;
        hw.ic->CSSetUnorderedAccessViews(0, 1, &nu, nullptr);
        hw.ic->CSSetConstantBuffers(0, 1, &nb);
        hw.ic->CSSetShader(nullptr, nullptr, 0);
    }
    double TimeOne(HwDev& hw) {
        hw.ic->Begin(hw.dj); hw.ic->End(hw.q0);
        Dispatch(hw);
        hw.ic->End(hw.q1); hw.ic->End(hw.dj); hw.ic->Flush();
        D3D11_QUERY_DATA_TIMESTAMP_DISJOINT dj = {};
        while (hw.ic->GetData(hw.dj, &dj, sizeof(dj), 0) != S_OK) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        UINT64 a = 0, b = 0;
        while (hw.ic->GetData(hw.q0, &a, 8, 0) != S_OK) {}
        while (hw.ic->GetData(hw.q1, &b, 8, 0) != S_OK) {}
        return (!dj.Disjoint && dj.Frequency) ? (double)(b - a) * 1e6 / (double)dj.Frequency : -1.0;
    }
    void Heat(HwDev& hw, double ms) {   // ~ms of GPU work, ~2 ms batches, each flushed alone (no wait here)
        for (double done = 0.0; ok && done < ms; done += 2.0) { Dispatch(hw); hw.ic->Flush(); }
    }
    ~HwHeater() { for (IUnknown* p : { (IUnknown*)cs, (IUnknown*)uav, (IUnknown*)buf, (IUnknown*)cb }) if (p) p->Release(); }
};

#ifdef FALD_FADE_HAS_PASS_HOOK
// FALD_HW_PROFILE=1: a timestamp before and after every fade pass of the timed content frames (fade on), summed per
// kernel -> <out>/profile_<case>.csv, and per dispatch in the frame's order -> <out>/profile_<case>_dispatch.csv (one
// kernel's dispatches told apart: an up-sampled Gaussian from a plain one; the timestamps serialise the passes a little:
// shares, not a frame time).
struct HwProfiler : FaldFadePassHook {
    static const int kMax = 1024;
    ID3D11Query* q[2 * kMax] = {};
    unsigned int kern[kMax] = {};
    int n = 0;
    bool on = false;
    std::vector<double> sumUs;
    std::vector<unsigned long long> count;
    std::vector<double> seqUs;          // per dispatch of the frame, in order (the same sequence every content frame)
    std::vector<unsigned int> seqKern;
    int frames = 0;
    explicit HwProfiler(ID3D11Device* dev) : sumUs(FaldFadeKernelCount(), 0.0), count(FaldFadeKernelCount(), 0) {
        D3D11_QUERY_DESC qd = { D3D11_QUERY_TIMESTAMP, 0 };
        for (auto& x : q) dev->CreateQuery(&qd, &x);
    }
    ~HwProfiler() { for (auto& x : q) if (x) x->Release(); }
    void Before(ID3D11DeviceContext* ctx, unsigned int k) override {
        if (!on || n >= kMax) return;
        kern[n] = k;
        ctx->End(q[2 * n]);
    }
    void After(ID3D11DeviceContext* ctx, unsigned int) override {
        if (!on || n >= kMax) return;
        ctx->End(q[2 * n + 1]);
        n++;
    }
    void Collect(ID3D11DeviceContext* ctx, UINT64 freq) {   // after the frame's disjoint query is done
        for (int i = 0; i < n; i++) {
            UINT64 a = 0, b = 0;
            while (ctx->GetData(q[2 * i], &a, 8, 0) != S_OK) {}
            while (ctx->GetData(q[2 * i + 1], &b, 8, 0) != S_OK) {}
            sumUs[kern[i]] += (double)(b - a) * 1e6 / (double)freq;
            count[kern[i]]++;
            if ((size_t)i >= seqUs.size()) { seqUs.push_back(0.0); seqKern.push_back(kern[i]); }
            if (seqKern[(size_t)i] == kern[i]) seqUs[(size_t)i] += (double)(b - a) * 1e6 / (double)freq;
        }
        if (n) frames++;
        n = 0;
    }
    void Write(const std::string& path) const {
        std::ofstream f(path);
        f << "kernel,dispatches_per_frame,us_per_frame\n";
        for (size_t k = 0; k < sumUs.size(); k++)
            if (count[k]) f << FaldFadeKernelName((unsigned int)k) << "," << (double)count[k] / frames << "," << sumUs[k] / frames << "\n";
        std::ofstream g(path.substr(0, path.size() - 4) + "_dispatch.csv");   // FALD_HW_PROFILE: each dispatch of the frame
        g << "index,kernel,us_per_frame\n";
        for (size_t i = 0; i < seqUs.size(); i++) g << i << "," << FaldFadeKernelName(seqKern[i]) << "," << seqUs[i] / frames << "\n";
    }
};
#endif

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
    const bool core2Mode = HwEnv("FALD_HW_CORE2") == "1";   // the always-on core round 2 prototype off / on instead of the fade
    if (dir.empty() || out.empty()) return;
    const int n = std::max(4, atoi(HwEnv("FALD_HW_N").empty() ? "30" : HwEnv("FALD_HW_N").c_str()));
    CreateDirectoryA(out.c_str(), nullptr);
    HwDev hw;
    REQUIRE(hw.ok);
    MESSAGE("adapter: " << hw.adapter);
    const double heatMs = HwEnv("FALD_HW_HEAT_MS").empty() ? 12.0 : atof(HwEnv("FALD_HW_HEAT_MS").c_str());
    // the output is hashed on every FALD_HW_HASH_EVERY-th run of each phase (default 4; 1 = all): a 4K readback + hash
    // is ~70 ms of CPU, a gap the GPU would clock down in
    const int hashEvery = std::max(1, atoi(HwEnv("FALD_HW_HASH_EVERY").empty() ? "4" : HwEnv("FALD_HW_HASH_EVERY").c_str()));
    HwHeater heater(hw);
    if (heatMs > 0.0) REQUIRE(heater.ok);
    MESSAGE("heater: " << heatMs << " ms before each run, " << heater.iters << " iterations a batch; hash every " << hashEvery);
#ifdef FALD_FADE_HAS_PASS_HOOK
    const bool profile = HwEnv("FALD_HW_PROFILE") == "1";
    HwProfiler prof(hw.dev);
    if (profile) g_faldFadePassHook = &prof;
    struct ClearHook { ~ClearHook() { g_faldFadePassHook = nullptr; } } clearHook;
#endif
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
            if (core2Mode) { fs.flankFade = false; fs.core2 = fade != 0; }   // FALD_HW_CORE2=1: column `fade` = the core round 2
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
                if (heatMs > 0.0) heater.Heat(hw, heatMs);   // the clock up (queued ahead of the run's first timestamp)
                hw.ic->Begin(hw.dj);
                hw.ic->End(hw.q0);
#ifdef FALD_FADE_HAS_PASS_HOOK
                prof.on = profile && fade && std::strcmp(phase, "content") == 0;
#endif
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
#ifdef FALD_FADE_HAS_PASS_HOOK
                if (!dj.Disjoint && dj.Frequency) prof.Collect(hw.ic, dj.Frequency);
                prof.on = false;
#endif
                double gov = -1.0;
#ifdef FALD_RUN_HAS_FRAME_KEY
                if (ctx.fald->fade) {   // the governor's own measurement of this content frame (read later, without waiting)
                    double s[4];
                    const unsigned int k = FaldFadeCollectTiming(ctx.fald->fade, hw.ic, s, 4);
                    if (k) gov = s[k - 1];
                }
#endif
                char hb[32] = "-";
                if (i % hashEvery == 0) snprintf(hb, sizeof(hb), "%016llx", (unsigned long long)HwFnvTex(target));
                csv << hc.id << "," << fade << "," << phase << "," << i << "," << (newContent ? 1 : 0) << "," << us << "," << hb << ","
                    << gov << "\n";
                // A steady run over 45 ms stops everything (the owner's GPU rule: no batch over 50 ms). The paced build's
                // runs only report it: the first content frame with the fade IS a measurement here (main eecda39's is ~51
                // ms at 4K, once per case); anything over 100 ms stops all the same.
                const bool build = std::strcmp(phase, "build") == 0;
                if (us > (build ? 100000.0 : 45000.0)) {
                    MESSAGE(hc.id << " " << phase << " run " << i << ": " << us << " us - stopping"); stop = true;
                } else if (us > 45000.0) {
                    MESSAGE(hc.id << " " << phase << " run " << i << ": " << us << " us (the paced build's last run: reported)");
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(2));   // the desktop gets the GPU between runs
                return !stop;
            };
            // the paced build (shaders, their warm-up, resources): content frames until the fade is on; the run that turns it
            // on is the first content frame with the fade
            int warm = 0;
            for (; warm < 400 && !stop && ((fade && !core2Mode) ? !ctx.fald->fadeOn : warm < 4); warm++) run("build", warm, true);
            if (fade && !core2Mode) {
                CHECK(ctx.fald->fadeOn);
                MESSAGE(hc.id << ": the fade on after " << warm << " runs, " << (FaldFadeVramBytes(ctx.fald->fade) >> 20) << " MB");
            }
            for (int i = 0; i < 4 && !stop; i++) run("warm", i, true);
            for (int i = 0; i < n && !stop; i++) run("content", i, true);
            for (int i = 0; i < n && !stop; i++) run("still", i, false);
            csv.flush();
#ifdef FALD_FADE_HAS_PASS_HOOK
            if (profile && fade) {
                prof.Write(out + "/profile_" + hc.id + ".csv");
                std::fill(prof.sumUs.begin(), prof.sumUs.end(), 0.0);
                std::fill(prof.count.begin(), prof.count.end(), 0ull);
                prof.seqUs.clear(); prof.seqKern.clear();
                prof.frames = 0;
            }
#endif
            rtv->Release(); target->Release();
            FaldReleaseResources(&ctx);
        }
    }
#ifdef FALD_RUN_HAS_FRAME_KEY
    g_faldFadeForce.store(false);
#endif
    CHECK_FALSE(stop);
}
