// GPU timing of the FALD layer on a HARDWARE D3D11 device (standalone: its own device, no DWM, no DesktopLUT running),
// with the always-on core off (C) and on (KC5, the default), per run of the layer: content frames (new content each run)
// and runs without new content on the same frame (a desktop's presents between content frames). Every run's output is
// hashed (FNV-1a 64), so two builds' outputs on the same device can be compared bit for bit. (Until 2026-11-12 this was
// tests/test_fald_fade_hw.cpp and timed the flank fade too: tag archive/fald-flank-fade.)
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
#include "../shared/fald_change.h"
#include "../shared/fald_temporal.h"
#include "../shared/fald_core_governor.h"
#include "../shared/fald_panel.h"
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <fstream>
#include <sstream>
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
// first frame at 80 ms or at 6 ms for the same work. Before each timed run the heater keeps the GPU busy for
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

// A ring of timestamp pairs around the layer's passes: summed per kernel over the frames
// collected -> a CSV (the timestamps serialise the passes a little: shares, not a frame time).
struct HwStampRing {
    static const int kMax = 1024;
    ID3D11Query* q[2 * kMax] = {};
    unsigned int kern[kMax] = {};
    int n = 0;
    bool on = false;
    std::vector<double> sumUs;
    std::vector<unsigned long long> count;
    std::vector<double> seqUs;          // per dispatch of the frame, in order (the same sequence every content frame; main 4207200)
    std::vector<unsigned int> seqKern;
    int frames = 0;
    const char* (*name)(unsigned int);
    HwStampRing(ID3D11Device* dev, unsigned int kernels, const char* (*nameFn)(unsigned int))
        : sumUs(kernels, 0.0), count(kernels, 0), name(nameFn) {
        D3D11_QUERY_DESC qd = { D3D11_QUERY_TIMESTAMP, 0 };
        for (auto& x : q) dev->CreateQuery(&qd, &x);
    }
    ~HwStampRing() { for (auto& x : q) if (x) x->Release(); }
    void Before(ID3D11DeviceContext* ctx, unsigned int k) {
        if (!on || n >= kMax || k >= sumUs.size()) return;
        kern[n] = k;
        ctx->End(q[2 * n]);
    }
    void After(ID3D11DeviceContext* ctx, unsigned int k) {
        if (!on || n >= kMax || k >= sumUs.size()) return;
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
    void Reset() {
        std::fill(sumUs.begin(), sumUs.end(), 0.0); std::fill(count.begin(), count.end(), 0ull);
        seqUs.clear(); seqKern.clear(); frames = 0; n = 0;
    }
    // per kernel -> <path>; per dispatch in the frame's order -> <path minus .csv>_dispatch.csv (one kernel's dispatches told
    // apart: an up-sampled Gaussian from a plain one; main 4207200)
    void Write(const std::string& path) const {
        std::ofstream f(path);
        f << "kernel,dispatches_per_frame,us_per_frame,frames\n";
        for (size_t k = 0; k < sumUs.size(); k++)
            if (count[k]) f << name((unsigned int)k) << "," << (double)count[k] / frames << "," << sumUs[k] / frames << "," << frames << "\n";
        std::ofstream g(path.substr(0, path.size() - 4) + "_dispatch.csv");
        g << "index,kernel,us_per_frame\n";
        for (size_t i = 0; i < seqUs.size(); i++) g << i << "," << name(seqKern[i]) << "," << seqUs[i] / frames << "\n";
    }
};

#ifdef FALD_CORE_HAS_PASS_HOOK
// FALD_HW_PROFILE_CORE=1: a timestamp before and after every pass of the layer (src/fald.h FaldCorePass), for the content
// AND the still runs, the core off and on -> <out>/profile_core_<case>_core<c>_<phase>.csv.
struct HwCoreProfiler : FaldCorePassHook, HwStampRing {
    explicit HwCoreProfiler(ID3D11Device* dev) : HwStampRing(dev, FALD_PASS_COUNT, FaldCorePassName) {}
    void Before(ID3D11DeviceContext* ctx, unsigned int k) override { HwStampRing::Before(ctx, k); }
    void After(ID3D11DeviceContext* ctx, unsigned int k) override { HwStampRing::After(ctx, k); }
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

TEST_CASE("FALD HW: GPU time per run of the layer, the core off / on, content / no new content (FALD_HW_DIR + FALD_HW_OUT)") {
    const std::string dir = HwEnv("FALD_HW_DIR"), out = HwEnv("FALD_HW_OUT"), only = HwEnv("FALD_HW_ONLY");
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
#ifdef FALD_CORE_HAS_PASS_HOOK
    const bool profileCore = HwEnv("FALD_HW_PROFILE_CORE") == "1";
    HwCoreProfiler coreProf(hw.dev);
    if (profileCore) g_faldCorePassHook = &coreProf;
    struct ClearCoreHook { ~ClearCoreHook() { g_faldCorePassHook = nullptr; } } clearCoreHook;
#endif
    // FALD_HW_CORE = 0 / 1: only the core off (C) / on (KC5) (default: off then on)
    const std::string coreSel = HwEnv("FALD_HW_CORE");
    // FALD_HW_CONV_SO = 1 / 2 / 4 / 8: the conv pass's sub-offsets-per-thread variant (default: the hosts' policy)
    {
        const std::string so = HwEnv("FALD_HW_CONV_SO");
        g_faldConvVariantOverride = so == "1" ? 0 : so == "2" ? 1 : so == "4" ? 2 : so == "8" ? 3 : -1;
        MESSAGE("conv variant override: " << g_faldConvVariantOverride);
    }
    const std::wstring C = HwOwnerPanel(L"DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin");
    REQUIRE_FALSE(C.empty());
    auto widen = [](const std::string& s) { return std::wstring(s.begin(), s.end()); };
    const std::vector<HwCase> cases = {
        { "4k", C, { "n7_lamp", "s10_sm_seattle" }, 3840, 2160 },
        { "1440p", widen(dir + "/frac_panel_48x24.bin"), { "x_frac2560" }, 2560, 1440 },
        { "1080p", widen(dir + "/panel_1080p.bin"), { "hw1080_n7", "hw1080_seattle" }, 1920, 1080 } };
    std::ofstream csv(out + "/runs.csv");
    csv << "case,core,phase,run,new_content,gpu_us,out_fnv\n";
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
        if (frames.size() == 1) {   // a second content frame: the first mirrored (a cut every frame)
            std::vector<uint16_t> m(frames[0].size());
            for (int y = 0; y < hc.h; y++)
                for (int x = 0; x < hc.w; x++)
                    for (int c = 0; c < 4; c++) m[((size_t)y * hc.w + x) * 4 + c] = frames[0][((size_t)y * hc.w + (hc.w - 1 - x)) * 4 + c];
            frames.push_back(std::move(m));
        }
        for (int core = 0; core < 2 && !stop; core++) {
            if ((coreSel == "0" && core != 0) || (coreSel == "1" && core != 1)) continue;
            MonitorContext ctx;
            ctx.width = hc.w; ctx.height = hc.h; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
            ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = 41.708f;   // a 23.976 Hz grid
            FaldSettings& fs = ctx.hdrColorCorrection.fald;
            fs.enabled = true; fs.paramsPath = hc.panel; fs.star.enabled = true;
            fs.coreMode = core ? FALD_CORE_MODE_KC5 : FALD_CORE_MODE_C;   // shared/fald_core_mode.h
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
            // the change-driven path's presents (FALD_HW_PARTIAL=1): a frame edited inside a rect, run with that rect
            std::vector<uint16_t> edited;
            std::vector<FaldChangeRect> prects;
            int partialRuns = 0;
            auto run = [&](const char* phase, int i, bool newContent, const std::vector<FaldChangeRect>* rects = nullptr) -> bool {
                D3D11_VIEWPORT vp = { 0, 0, (float)hc.w, (float)hc.h, 0, 1 };
                hw.ic->RSSetViewports(1, &vp);
                hw.ic->VSSetShader(hw.vs, nullptr, 0);
                hw.ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
                if (rects) {                                        // the edited frame (the upload is not part of the run)
                    hw.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, edited.data(), (UINT)hc.w * 8u, 0);
                    key++;
                } else if (newContent) {
                    hw.ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frames[(size_t)frameIdx % frames.size()].data(), (UINT)hc.w * 8u, 0);
                    frameIdx++;
                    key++;
                }
                hw.ic->Flush();   // the upload is not part of the run
                if (heatMs > 0.0) heater.Heat(hw, heatMs);   // the clock up (queued ahead of the run's first timestamp)
                hw.ic->Begin(hw.dj);
                hw.ic->End(hw.q0);
#ifdef FALD_CORE_HAS_PASS_HOOK
                coreProf.on = profileCore && (std::strcmp(phase, "content") == 0 || std::strcmp(phase, "still") == 0);
#endif
                FaldRunPasses(&ctx, rtv, newContent, key, rects ? rects->data() : nullptr, rects ? (int)rects->size() : -1);
                if (ctx.fald->lastPartial) partialRuns++;
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
#ifdef FALD_CORE_HAS_PASS_HOOK
                if (!dj.Disjoint && dj.Frequency) coreProf.Collect(hw.ic, dj.Frequency); else coreProf.n = 0;
                coreProf.on = false;
#endif
                char hb[32] = "-";
                if (i % hashEvery == 0) snprintf(hb, sizeof(hb), "%016llx", (unsigned long long)HwFnvTex(target));
                csv << hc.id << "," << core << "," << phase << "," << i << "," << (newContent ? 1 : 0) << "," << us << "," << hb << "\n";
                // a run over 45 ms stops everything (the owner's GPU rule: no batch over 50 ms)
                if (us > 45000.0) { MESSAGE(hc.id << " " << phase << " run " << i << ": " << us << " us - stopping"); stop = true; }
                std::this_thread::sleep_for(std::chrono::milliseconds(2));   // the desktop gets the GPU between runs
                return !stop;
            };
            for (int i = 0; i < 4 && !stop; i++) run("build", i, true);   // the first runs: resources, the conv tune's start
            for (int i = 0; i < 4 && !stop; i++) run("warm", i, true);
            for (int i = 0; i < n && !stop; i++) run("content", i, true);
#ifdef FALD_CORE_HAS_PASS_HOOK
            if (profileCore) {
                coreProf.Write(out + "/profile_core_" + hc.id + "_core" + std::to_string(core) + "_content.csv");
                coreProf.Reset();
            }
#endif
            for (int i = 0; i < n && !stop; i++) run("still", i, false);
            if (HwEnv("FALD_HW_PARTIAL") == "1" && !stop) {
                // the frame of the last content run, edited inside a rect per phase; the first run of each phase is a full
                // one (the state of the previous run has to be complete and the frame known), then the partial presents
                edited = frames[(size_t)(frameIdx - 1) % frames.size()];
                auto paint = [&](const FaldChangeRect& rc, uint16_t v) {
                    for (int y = rc.top; y < rc.bottom; y++) for (int x = rc.left; x < rc.right; x++) for (int c = 0; c < 3; c++)
                        edited[((size_t)y * hc.w + x) * 4 + c] = v;
                };
                struct PPhase { const char* name; FaldChangeRect rc; uint16_t v0, v1; };   // v1: toggled every other run (0xFFFF = no toggle)
                const int cx = hc.w / 2, cy = hc.h / 2;
                const PPhase phases[] = {
                    { "part_none", { 0, 0, 0, 0 }, 0, 0xFFFF },                                   // no rect at all: the same frame
                    { "part_kick", { 0, 0, 1, 1 }, 0xFFFF, 0xFFFF },                               // a 1 x 1 rect of unchanged pixels
                    { "part_dark", { cx, cy, cx + 40, cy + 30 }, 0x0400, 0x0800 },                  // a 40 x 30 patch of ~0.004 / 0.008 nits
                    { "part_bright", { cx - 100, cy - 100, cx + 100, cy + 100 }, 0x3C00, 0x0000 }, // 200 x 200 white, toggled
                };
                for (const PPhase& ph : phases) {
                    partialRuns = 0;
                    std::vector<FaldChangeRect> rects;
                    if (ph.rc.right > ph.rc.left) rects.push_back(ph.rc);
                    if (ph.v0 != 0xFFFF) paint(ph.rc, ph.v0);
                    run("warm", 0, true, nullptr);                  // the frame as a whole (full), the state complete
                    hw.ic->Flush();
                    for (int i = 0; i < n && !stop; i++) {
                        if (ph.v1 != 0xFFFF) paint(ph.rc, (i & 1) ? ph.v1 : ph.v0);
                        if (!run(ph.name, i, true, &rects)) break;
                    }
                    MESSAGE(hc.id << " " << ph.name << ": " << partialRuns << " of " << n << " runs took the partial path");
                }
            }
#ifdef FALD_CORE_HAS_PASS_HOOK
            if (profileCore) {
                coreProf.Write(out + "/profile_core_" + hc.id + "_core" + std::to_string(core) + "_still.csv");
                coreProf.Reset();
            }
#endif
            csv.flush();
            rtv->Release(); target->Release();
            FaldReleaseResources(&ctx);
        }
    }
    CHECK_FALSE(stop);
}

// ---------------------------------------------------------------------------------------------------------------------
// EXPERIMENT (nothing here ships): the panel clock's maps held inside a DEAD-BAND, measured against the shipped pass.
// With LED lag = the panel clock, the clock pass flags "a map moved" by an exact compare, and on the owner's settings the
// maps never stop moving: the round-1 drives feed back through the correction and the float32 states end in a limit cycle
// one unit in the last place wide, so every present of a static desktop runs the convolutions, the round-1 sweep and the
// pixel pass in full (tests/test_perf.cpp DLUT_PERF_FALD: 300 of 300 presents). The variant below holds a zone's two maps
// at their previous values while the new ones are within EPS (relative) of them — the clocks' own states keep their exact
// evolution, so the held maps are never further than EPS from the shipped ones — and flags only real movement.
// The measurement: the SAME scheduled sequence (a cut, a long still, a caret-sized edit, a still) through the shipped
// pass and through the variant at each EPS; per variant how many still runs moved a map, and how far its output frames
// are from the shipped pass's (FP16 scRGB, 1.0 = 80 nits) at the sampled runs.
// FALD_HW_DEADBAND_OUT=<dir> (deadband.tsv); FALD_HW_DEADBAND_FRAMES="a.rgba16f;b.rgba16f" (3840 x 2160 FP16 frames: the
// layer's INPUT, e.g. the hook's intermediate dumped by DLUT_PERF_FALD_DUMP_INTER); FALD_HW_DEADBAND_EPS="1e-6;1e-5;1e-4".
// ---------------------------------------------------------------------------------------------------------------------
namespace {
void DbWaitUntil(long long targetQpc) {
    LARGE_INTEGER now;
    for (;;) {
        QueryPerformanceCounter(&now);
        if (now.QuadPart >= targetQpc) return;
        SwitchToThread();
    }
}
std::vector<unsigned char> DbReadBack(ID3D11Texture2D* tex) {
    std::vector<unsigned char> out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return out;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.resize((size_t)d.Width * d.Height * 8u);
        for (UINT y = 0; y < d.Height; y++)
            std::memcpy(out.data() + (size_t)y * d.Width * 8u, (const unsigned char*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * 8u);
        g_context->Unmap(st, 0);
    }
    st->Release();
    return out;
}
bool DbConvRan(const FaldResources* r) {                 // did the last partial run's decide order round 0's convolution?
    D3D11_BUFFER_DESC d; r->argsBuf->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0; d.StructureByteStride = 0;
    ID3D11Buffer* st = nullptr;
    if (FAILED(g_device->CreateBuffer(&d, nullptr, &st))) return true;
    g_context->CopyResource(st, r->argsBuf);
    bool ran = true;
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        const uint32_t* a = (const uint32_t*)((const unsigned char*)m.pData + FALD_ARGS_CONV0);
        ran = a[0] != 0u && a[1] != 0u && a[2] != 0u;
        g_context->Unmap(st, 0);
    }
    st->Release();
    return ran;
}
float DbHalf(uint16_t h) {
    const uint32_t s = (h >> 15) & 1u, e = (h >> 10) & 0x1Fu, m = h & 0x3FFu;
    float v;
    if (e == 0) v = std::ldexp((float)m, -24);
    else if (e == 31) v = m ? NAN : INFINITY;
    else v = std::ldexp((float)(m | 0x400u), (int)e - 25);
    return s ? -v : v;
}
}  // namespace

TEST_CASE("FALD HW experiment: the panel clock's maps held inside a dead-band, against the shipped pass (FALD_HW_DEADBAND_OUT)") {
    const std::string out = HwEnv("FALD_HW_DEADBAND_OUT");
    if (out.empty()) return;
    CreateDirectoryA(out.c_str(), nullptr);
    std::vector<std::string> frames, epsList;
    {
        std::stringstream fs(HwEnv("FALD_HW_DEADBAND_FRAMES")), es(HwEnv("FALD_HW_DEADBAND_EPS").empty() ? "1e-6;1e-5;1e-4" : HwEnv("FALD_HW_DEADBAND_EPS"));
        std::string t;
        while (std::getline(fs, t, ';')) if (!t.empty()) frames.push_back(t);
        while (std::getline(es, t, ';')) if (!t.empty()) epsList.push_back(t);
    }
    REQUIRE_MESSAGE(frames.size() >= 2, "FALD_HW_DEADBAND_FRAMES needs two 3840 x 2160 FP16 frames");
    const std::wstring panel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";
    REQUIRE(GetFileAttributesW(panel.c_str()) != INVALID_FILE_ATTRIBUTES);
    const int W = 3840, H = 2160;
    std::vector<uint16_t> fA((size_t)W * H * 4), fB((size_t)W * H * 4);
    for (int i = 0; i < 2; i++) {
        std::ifstream f(frames[(size_t)i], std::ios::binary);
        REQUIRE_MESSAGE(f.good(), frames[(size_t)i]);
        std::vector<uint16_t>& dst = i ? fB : fA;
        f.read((char*)dst.data(), (std::streamsize)(dst.size() * 2));
        REQUIRE((size_t)f.gcount() == dst.size() * 2);
    }
    // the schedule: k refreshes between runs alternate 2 / 3 (what a 120 Hz monitor sees of presents ~20 ms apart) on a
    // nominal period long enough for a run with its upload; the still phases are what the comparison is about
    const double periodMs = 12.0;
    const int nStill1 = 160, nStill2 = 60;
    const std::vector<int> sample = { 0, 1, 2, 4, 9, 19, 39, 79, 157, 158, 159 };   // still-1 runs whose output is kept (+ the edit, + sample2)
    // FALD_HW_DEADBAND_K = the refreshes between runs, a digit string cycled ("23" by default: presents ~20 ms apart on a
    // 120 Hz monitor; "1" = a present every refresh, the host's settle kicks)
    std::string kPattern = HwEnv("FALD_HW_DEADBAND_K");
    if (kPattern.empty() || kPattern.find_first_not_of("123456789") != std::string::npos) kPattern = "23";
    const std::vector<int> sample2 = { 0, 1, 4, 19, 59 };

    struct RunOut { std::vector<std::vector<unsigned char>> kept; int moved1 = 0, moved2 = 0, ran1 = 0, ran2 = 0; std::vector<unsigned long long> k; };
    auto sequence = [&](const std::string& eps, RunOut& ro) -> bool {
        // the device's layer shaders: the shipped set, or the same sources with the clock pass's dead-band variant
        ReleaseFaldShaders();
        FaldShaderSources src = FaldDefaultShaderSources();
        std::string clock = g_faldPanelClockSource;
        if (!eps.empty()) {
            const std::string from =
                "    if (RunModePartial() == 1u && (clkTrueOut[id.xy] != oTrue || clkEstOut[id.xy] != oEst)) InterlockedOr(changeFlags[0], 1u);\n";
            const std::string to =
                "    if (RunModePartial() == 1u) {\n"
                "        const float pT = clkTrueOut[id.xy], pE = clkEstOut[id.xy];\n"
                "        const bool mv = abs(oTrue - pT) > " + eps + "f * max(abs(pT), 1e-6f) || abs(oEst - pE) > " + eps + "f * max(abs(pE), 1e-6f);\n"
                "        if (mv) InterlockedOr(changeFlags[0], 1u); else { oTrue = pT; oEst = pE; }\n"
                "    }\n";
            const size_t at = clock.find(from);
            if (at == std::string::npos) { MESSAGE("the clock pass's compare line was not found - the experiment's variant needs updating"); return false; }
            clock.replace(at, from.size(), to);
        }
        src.panelClock = clock.c_str();
        if (!InitFaldShadersFrom(src)) return false;
        MonitorContext ctx;
        ctx.width = W; ctx.height = H; ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = (float)periodMs;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = true; fs.coreMode = FALD_CORE_MODE_KC5;
        fs.temporalMode = FALD_TEMPORAL_PANEL; fs.clockClosure = 0.72f; fs.clockParity = -1;
        if (!FaldEnsureResources(&ctx, fs)) { FaldReleaseResources(&ctx); return false; }
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)W; td.Height = (UINT)H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
        g_device->CreateTexture2D(&td, nullptr, &target);
        if (target) g_device->CreateRenderTargetView(target, nullptr, &rtv);
        if (!rtv) { if (target) target->Release(); FaldReleaseResources(&ctx); return false; }
        ID3D11VertexShader* vs = nullptr;
        {
            ID3DBlob* b = nullptr;
            if (SUCCEEDED(D3DCompile(g_faldFullscreenVsSource, std::strlen(g_faldFullscreenVsSource), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &b, nullptr))) {
                g_device->CreateVertexShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &vs);
                b->Release();
            }
        }
        LARGE_INTEGER freq, t0;
        QueryPerformanceFrequency(&freq);
        std::vector<uint16_t> frame = fA;
        unsigned long long key = 1;
        long long refresh = 0;
        int run = 0;
        const FaldChangeRect caret = { W / 5, H * 3 / 10, W / 5 + 2, H * 3 / 10 + 18 };
        auto one = [&](bool newContent, const FaldChangeRect* rects, int numRects, bool upload) {
            refresh += (run == 0) ? 0 : (long long)(kPattern[(size_t)run % kPattern.size()] - '0');
            if (run == 0) { QueryPerformanceCounter(&t0); t0.QuadPart += freq.QuadPart / 10; }
            if (upload) g_context->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)W * 8u, 0);
            g_context->Flush();
            DbWaitUntil(t0.QuadPart + (long long)((double)refresh * periodMs * (double)freq.QuadPart / 1000.0));
            D3D11_VIEWPORT vp = { 0, 0, (float)W, (float)H, 0, 1 };
            g_context->RSSetViewports(1, &vp);
            g_context->VSSetShader(vs, nullptr, 0);
            g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            FaldRunPasses(&ctx, rtv, newContent, key, rects, numRects);
            g_context->Flush();
            ro.k.push_back(ctx.fald->clkElapsed);
            run++;
        };
        for (int i = 0; i < 3; i++) one(true, nullptr, -1, true);                      // frame A, settled enough for the cut to be a cut
        frame = fB; key++;
        one(true, nullptr, -1, true);                                                  // the cut
        for (int i = 0; i < nStill1; i++) {                                            // a static desktop: no rect
            one(false, nullptr, 0, false);
            if (ctx.fald->lastPartial) { ro.ran1++; if (DbConvRan(ctx.fald)) ro.moved1++; }
            if (std::find(sample.begin(), sample.end(), i) != sample.end()) ro.kept.push_back(DbReadBack(target));
        }
        for (int y = caret.top; y < caret.bottom; y++)                                 // a caret: dark, 2 x 18 px
            for (int x = caret.left; x < caret.right; x++)
                for (int c = 0; c < 3; c++) frame[((size_t)y * W + x) * 4 + c] = 0x2800;   // 2^-5 scRGB = 2.5 nits
        key++;
        one(true, &caret, 1, true);
        ro.kept.push_back(DbReadBack(target));
        for (int i = 0; i < nStill2; i++) {
            one(false, nullptr, 0, false);
            if (ctx.fald->lastPartial) { ro.ran2++; if (DbConvRan(ctx.fald)) ro.moved2++; }
            if (std::find(sample2.begin(), sample2.end(), i) != sample2.end()) ro.kept.push_back(DbReadBack(target));
        }
        if (vs) vs->Release();
        rtv->Release(); target->Release();
        FaldReleaseResources(&ctx);
        return true;
    };

    HwDev hw;
    REQUIRE(hw.ok);
    std::ofstream tsv(out + "\\deadband.tsv");
    tsv << "adapter\teps\tstill1_partial\tstill1_moved\tstill2_partial\tstill2_moved\tschedule_equal\tframes_compared\tframes_differing\tcomponents_differing_max\tmax_abs_scrgb\tmax_abs_nits\tmax_rel\n";
    RunOut ref;
    REQUIRE(sequence(std::string(), ref));
    MESSAGE("shipped pass (k pattern " << kPattern << "): " << ref.moved1 << " of " << ref.ran1 << " still runs after the cut moved a map, " << ref.moved2 << " of " << ref.ran2 << " after the caret");
    // how far the SHIPPED output itself moves between presents of the static desktop, long after the cut (still runs 157,
    // 158, 159: kept frames 8, 9, 10): the yardstick for the variants' differences below
    auto frameDiff = [&](const std::vector<unsigned char>& fa, const std::vector<unsigned char>& fb, size_t& comp, double& maxAbs, double& maxRel) {
        comp = 0; maxAbs = 0.0; maxRel = 0.0;
        const uint16_t* a = (const uint16_t*)fa.data(); const uint16_t* b = (const uint16_t*)fb.data();
        for (size_t j = 0; j < fa.size() / 2 && j < fb.size() / 2; j++) {
            if (a[j] == b[j]) continue;
            comp++;
            const double x = (double)DbHalf(a[j]), y = (double)DbHalf(b[j]);
            const double d = std::fabs(x - y);
            if (d > maxAbs) maxAbs = d;
            const double rel = d / std::max(std::max(std::fabs(x), std::fabs(y)), 1e-6);
            if (rel > maxRel) maxRel = rel;
        }
    };
    if (ref.kept.size() >= 11) {
        size_t c1 = 0, c2 = 0; double a1 = 0, r1 = 0, a2 = 0, r2 = 0;
        frameDiff(ref.kept[9], ref.kept[10], c1, a1, r1);
        frameDiff(ref.kept[8], ref.kept[10], c2, a2, r2);
        MESSAGE("shipped pass, the static desktop's own movement: still run 158 -> 159: " << c1 << " components differ, max " << a1 * 80.0 << " nits, max relative " << r1
                << "; 157 -> 159: " << c2 << " components, max " << a2 * 80.0 << " nits, max relative " << r2);
        tsv << hw.adapter << "\tshipped_158_vs_159\t-\t-\t-\t-\t1\t1\t" << (c1 ? 1 : 0) << "\t" << c1 << "\t" << a1 << "\t" << a1 * 80.0 << "\t" << r1 << "\n";
        tsv << hw.adapter << "\tshipped_157_vs_159\t-\t-\t-\t-\t1\t1\t" << (c2 ? 1 : 0) << "\t" << c2 << "\t" << a2 << "\t" << a2 * 80.0 << "\t" << r2 << "\n";
    }
    tsv << hw.adapter << "\t0\t" << ref.ran1 << "\t" << ref.moved1 << "\t" << ref.ran2 << "\t" << ref.moved2 << "\t1\t" << ref.kept.size() << "\t0\t0\t0\t0\t0\n";
    {   // the same sequence through the shipped pass again: the harness's own repeatability (must be 0 everywhere)
        RunOut again;
        REQUIRE(sequence(std::string(), again));
        size_t differ = 0;
        for (size_t i = 0; i < ref.kept.size() && i < again.kept.size(); i++) if (ref.kept[i] != again.kept[i]) differ++;
        MESSAGE("repeat of the shipped pass: " << differ << " of " << ref.kept.size() << " kept frames differ, schedule " << std::string(ref.k == again.k ? "equal" : "DIFFERENT"));
        tsv << hw.adapter << "\trepeat\t" << again.ran1 << "\t" << again.moved1 << "\t" << again.ran2 << "\t" << again.moved2 << "\t" << (ref.k == again.k ? 1 : 0) << "\t"
            << ref.kept.size() << "\t" << differ << "\t-\t-\t-\t-\n";
    }
    for (const std::string& eps : epsList) {
        RunOut v;
        REQUIRE(sequence(eps, v));
        size_t framesDiffer = 0, compMax = 0;
        double maxAbs = 0.0, maxRel = 0.0;
        for (size_t i = 0; i < ref.kept.size() && i < v.kept.size(); i++) {
            if (ref.kept[i] == v.kept[i]) continue;
            framesDiffer++;
            const uint16_t* a = (const uint16_t*)ref.kept[i].data(); const uint16_t* b = (const uint16_t*)v.kept[i].data();
            size_t comp = 0;
            for (size_t j = 0; j < ref.kept[i].size() / 2; j++) {
                if (a[j] == b[j]) continue;
                comp++;
                const double x = (double)DbHalf(a[j]), y = (double)DbHalf(b[j]);
                const double d = std::fabs(x - y);
                if (d > maxAbs) maxAbs = d;
                const double rel = d / std::max(std::max(std::fabs(x), std::fabs(y)), 1e-6);
                if (rel > maxRel) maxRel = rel;
            }
            compMax = std::max(compMax, comp);
        }
        MESSAGE("dead-band " << eps << ": " << v.moved1 << " of " << v.ran1 << " still runs after the cut moved a map, " << v.moved2 << " of " << v.ran2
                << " after the caret; " << framesDiffer << " of " << ref.kept.size() << " kept output frames differ from the shipped pass's, at most " << compMax
                << " of " << (size_t)W * H * 4 << " components, max " << maxAbs << " scRGB (" << maxAbs * 80.0 << " nits), max relative " << maxRel
                << "; schedule " << std::string(ref.k == v.k ? "equal" : "DIFFERENT (compare with care)"));
        tsv << hw.adapter << "\t" << eps << "\t" << v.ran1 << "\t" << v.moved1 << "\t" << v.ran2 << "\t" << v.moved2 << "\t" << (ref.k == v.k ? 1 : 0) << "\t"
            << ref.kept.size() << "\t" << framesDiffer << "\t" << compMax << "\t" << maxAbs << "\t" << maxAbs * 80.0 << "\t" << maxRel << "\n";
    }
}

// ---------------------------------------------------------------------------------------------------------------------
// STUDY (opt-in, nothing here ships): the panel clock's steady state on a static frame. With LED lag = the panel clock
// the clock's maps keep moving on a desktop that stands still; this case records WHAT recurs and after how many runs.
// The clock's dynamical state after run n is X_n = (S_0, S_1, d): the two parity clocks' LED states and the round-1
// drives the next run aims them at; the run's maps and picture are functions of X_(n-1), the blend factors of run n and
// the frame. With a static frame and a factor sequence of period q, X_n == X_(n-q) bit for bit means the layer repeats
// with that period for good. Every run's X, maps and output are read back; per phase (after a cut, a caret-sized edit
// that moves no drive, an edit that moves drives) the case reports the first run from which X recurs, the period, how
// far the two phases are apart (zones, relative; output components, nits), under a set of schedules (the refreshes k
// between runs, SET through g_faldTestNowQpc), closures, parities, starfield / core on and off, HDR and SDR, and with
// the statistic pass's per-pixel floor removed (the "nofloor" variant: is the floor what keeps the loop from settling?).
// FALD_HW_CYCLE_OUT=<dir> (cycle_phases.tsv, cycle_runs.tsv); FALD_HW_CYCLE_FRAMES="a.rgba16f;b.rgba16f" (3840 x 2160
// FP16: the layer's input; the cut goes from a to b); FALD_HW_CYCLE_ONLY = a substring of the configuration ids;
// FALD_HW_CYCLE_N = still runs per phase (160).
// ---------------------------------------------------------------------------------------------------------------------
namespace {
uint64_t CyHash(const void* p, size_t n) {
    const unsigned char* b = (const unsigned char*)p;
    uint64_t h = 0x9E3779B97F4A7C15ull;
    size_t i = 0;
    for (; i + 8 <= n; i += 8) { uint64_t v; std::memcpy(&v, b + i, 8); h = (h ^ v) * 0xFF51AFD7ED558CCDull; h ^= h >> 29; }
    for (; i < n; i++) { h = (h ^ b[i]) * 0x100000001B3ull; }
    return h;
}
std::vector<float> CyReadR32(ID3D11Texture2D* tex) {
    std::vector<float> out;
    if (!tex) return out;
    D3D11_TEXTURE2D_DESC d; tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &st))) return out;
    g_context->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.resize((size_t)d.Width * d.Height);
        for (UINT y = 0; y < d.Height; y++)
            std::memcpy(out.data() + (size_t)y * d.Width, (const unsigned char*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * 4u);
        g_context->Unmap(st, 0);
    }
    st->Release();
    return out;
}
struct CyCfg {
    std::string id; bool hdr; bool star; unsigned int core; int parity; float closure; std::vector<int> k; double hz; std::string variant;
};
struct CyRun {
    int phase; int k; std::vector<float> s0, s1, d, mT, mE; uint64_t hx, hOut; bool outKept;
};
// zones whose value differs, and the largest relative difference
void CyDiff(const std::vector<float>& a, const std::vector<float>& b, size_t& zones, double& maxRel) {
    zones = 0; maxRel = 0.0;
    for (size_t i = 0; i < a.size() && i < b.size(); i++) {
        if (std::memcmp(&a[i], &b[i], 4) == 0) continue;
        zones++;
        const double r = std::fabs((double)a[i] - (double)b[i]) / std::max(std::max(std::fabs((double)a[i]), std::fabs((double)b[i])), 1e-9);
        if (r > maxRel) maxRel = r;
    }
}
}  // namespace

TEST_CASE("FALD HW study: what the panel clock's state does on a static frame (FALD_HW_CYCLE_OUT)") {
    const std::string out = HwEnv("FALD_HW_CYCLE_OUT");
    if (out.empty()) return;
    CreateDirectoryA(out.c_str(), nullptr);
    std::vector<std::string> frames;
    {
        std::stringstream fs(HwEnv("FALD_HW_CYCLE_FRAMES"));
        std::string t;
        while (std::getline(fs, t, ';')) if (!t.empty()) frames.push_back(t);
    }
    REQUIRE_MESSAGE(frames.size() >= 2, "FALD_HW_CYCLE_FRAMES needs two 3840 x 2160 FP16 frames");
    // FALD_HW_CYCLE_SDR_FRAMES: the same two for the SDR / ACM configurations (their layer input; without them those are skipped)
    std::vector<std::string> sdrFrames;
    {
        std::stringstream fs(HwEnv("FALD_HW_CYCLE_SDR_FRAMES"));
        std::string t;
        while (std::getline(fs, t, ';')) if (!t.empty()) sdrFrames.push_back(t);
    }
    const std::string only = HwEnv("FALD_HW_CYCLE_ONLY");
    const int nStill = std::max(8, atoi(HwEnv("FALD_HW_CYCLE_N").empty() ? "160" : HwEnv("FALD_HW_CYCLE_N").c_str()));
    const std::wstring hdrPanel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";
    const std::wstring sdrPanel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin";
    REQUIRE(GetFileAttributesW(hdrPanel.c_str()) != INVALID_FILE_ATTRIBUTES);
    const int W = 3840, H = 2160;
    std::vector<uint16_t> hA((size_t)W * H * 4), hB((size_t)W * H * 4);
    for (int i = 0; i < 2; i++) {
        std::ifstream f(frames[(size_t)i], std::ios::binary);
        REQUIRE_MESSAGE(f.good(), frames[(size_t)i]);
        std::vector<uint16_t>& dst = i ? hB : hA;
        f.read((char*)dst.data(), (std::streamsize)(dst.size() * 2));
        REQUIRE((size_t)f.gcount() == dst.size() * 2);
    }
    const unsigned int KC5 = FALD_CORE_MODE_KC5, CC = FALD_CORE_MODE_C;
    std::vector<CyCfg> cfgs = {
        // THE OWNER'S MONITOR (PA32UCXR, 60 Hz at most) at the rates it is driven at, his settings: a present every refresh
        // (the settle kicks), a caret's blink (0.533 s = 32 refreshes at 60 Hz) with the presents the host kicks after it
        // (the hold's 14 refreshes + the kicker's 100 ms = 6 at 60 Hz), the blinks alone; 24p video's rate; SDR / ACM
        { "o60_k1",        true, true,  KC5, -1, 0.72f, { 1 }, 60.0, "" },
        { "o5994_k1",      true, true,  KC5, -1, 0.72f, { 1 }, 60000.0 / 1001.0, "" },
        { "o50_k1",        true, true,  KC5, -1, 0.72f, { 1 }, 50.0, "" },
        { "o48_k1",        true, true,  KC5, -1, 0.72f, { 1 }, 48.0, "" },
        { "o47952_k1",     true, true,  KC5, -1, 0.72f, { 1 }, 48000.0 / 1001.0, "" },
        { "o60_blink",     true, true,  KC5, -1, 0.72f, { 32, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 60.0, "" },
        { "o50_blink",     true, true,  KC5, -1, 0.72f, { 27, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 50.0, "" },
        { "o47952_blink",  true, true,  KC5, -1, 0.72f, { 26, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 48000.0 / 1001.0, "" },
        { "o60_k32",       true, true,  KC5, -1, 0.72f, { 32 }, 60.0, "" },
        { "o60_k2",        true, true,  KC5, -1, 0.72f, { 2 }, 60.0, "" },
        { "s60_k1",        false, true, KC5, -1, 0.72f, { 1 }, 60.0, "" },
        { "s60_blink",     false, true, KC5, -1, 0.72f, { 32, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 60.0, "" },
        { "s60_k32",       false, true, KC5, -1, 0.72f, { 32 }, 60.0, "" },
        // other panels' rates and schedules: a present every refresh (the settle kicks), every 2nd,
        // every 3rd, 2 / 3 and 1 / 2 alternating, sparse presents (a caret's blink: half a second apart)
        { "owner_k1",      true, true,  KC5, -1, 0.72f, { 1 }, 120.0, "" },
        { "owner_k2",      true, true,  KC5, -1, 0.72f, { 2 }, 120.0, "" },
        { "owner_k3",      true, true,  KC5, -1, 0.72f, { 3 }, 120.0, "" },
        { "owner_k23",     true, true,  KC5, -1, 0.72f, { 2, 3 }, 120.0, "" },
        { "owner_k12",     true, true,  KC5, -1, 0.72f, { 1, 2 }, 120.0, "" },
        { "owner_k1112",   true, true,  KC5, -1, 0.72f, { 1, 1, 1, 2 }, 120.0, "" },
        { "owner_k60",     true, true,  KC5, -1, 0.72f, { 60 }, 120.0, "" },
        { "owner_k7",      true, true,  KC5, -1, 0.72f, { 7 }, 120.0, "" },
        { "owner_burst",   true, true,  KC5, -1, 0.72f, { 60, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 120.0, "" },   // a blink + its 14 kicks
        { "owner_k1_144",  true, true,  KC5, -1, 0.72f, { 1 }, 144.0, "" },
        { "owner_k1_240",  true, true,  KC5, -1, 0.72f, { 1 }, 240.0, "" },
        { "owner_k1_60",   true, true,  KC5, -1, 0.72f, { 1 }, 60.0, "" },
        // the parity and the closure
        { "par0_k1",       true, true,  KC5, 0, 0.72f, { 1 }, 120.0, "" },
        { "par1_k1",       true, true,  KC5, 1, 0.72f, { 1 }, 120.0, "" },
        { "par0_k2",       true, true,  KC5, 0, 0.72f, { 2 }, 120.0, "" },
        { "clo050_k1",     true, true,  KC5, -1, 0.50f, { 1 }, 120.0, "" },
        { "clo090_k1",     true, true,  KC5, -1, 0.90f, { 1 }, 120.0, "" },
        { "clo100_k1",     true, true,  KC5, -1, 1.00f, { 1 }, 120.0, "" },
        { "clo020_k1",     true, true,  KC5, -1, 0.20f, { 1 }, 120.0, "" },
        // the starfield and the always-on core off
        { "nostar_k1",     true, false, KC5, -1, 0.72f, { 1 }, 120.0, "" },
        { "coreC_k1",      true, true,  CC,  -1, 0.72f, { 1 }, 120.0, "" },
        { "plain_k1",      true, false, CC,  -1, 0.72f, { 1 }, 120.0, "" },
        { "plain_k60",     true, false, CC,  -1, 0.72f, { 60 }, 120.0, "" },
        // SDR / ACM
        { "sdr_k1",        false, true, KC5, -1, 0.72f, { 1 }, 120.0, "" },
        { "sdr_k60",       false, true, KC5, -1, 0.72f, { 60 }, 120.0, "" },
        // the statistic's per-pixel floor removed (a variant shader: is the floor what the loop hops over?)
        { "nofloor_k1",    true, true,  KC5, -1, 0.72f, { 1 }, 120.0, "nofloor" },
        { "nofloor_k60",   true, true,  KC5, -1, 0.72f, { 60 }, 120.0, "nofloor" },
        { "nofloor_plain_k1", true, false, CC, -1, 0.72f, { 1 }, 120.0, "nofloor" },
        { "nofloor_plain_k60", true, false, CC, -1, 0.72f, { 60 }, 120.0, "nofloor" },
        // the drive curve by a float lerp instead of the bilinear sampler (is the sampler's 8-bit weight the staircase?)
        { "exactcurve_k1",  true, true,  KC5, -1, 0.72f, { 1 }, 120.0, "exactcurve" },
        { "exactcurve_k2",  true, true,  KC5, -1, 0.72f, { 2 }, 120.0, "exactcurve" },
        { "exactcurve_k60", true, true,  KC5, -1, 0.72f, { 60 }, 120.0, "exactcurve" },
        { "exactcurve_plain_k1", true, false, CC, -1, 0.72f, { 1 }, 120.0, "exactcurve" },
        // a blink and the kicks the host really sends: the hook's 14 refreshes + the kicker's 100 ms linger (12 at 120 Hz)
        { "owner_burst26", true, true,  KC5, -1, 0.72f, { 60, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 120.0, "" },
        { "sdr_burst26",   false, true, KC5, -1, 0.72f, { 60, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 }, 120.0, "" },
    };
    HwDev hw;
    REQUIRE(hw.ok);
    const int savedHold = g_faldCoreHoldOverride, savedPartial = g_faldPartialOverride;
    g_faldCoreHoldOverride = 0;            // the governor never holds a run here
    g_faldClockRestOverride = HwEnv("FALD_HW_CYCLE_REST") == "1" ? -1 : 0;   // the UNHELD layer (the study is of its loop), unless asked
    struct Restore { int h, p; ~Restore() { g_faldCoreHoldOverride = h; g_faldPartialOverride = p; g_faldTestNowQpc = 0; g_faldClockRestOverride = -1; } } restore{ savedHold, savedPartial };
    std::ofstream phTsv(out + "\\cycle_phases.tsv"), runTsv(out + "\\cycle_runs.tsv");
    phTsv << "adapter\tcfg\tphase\truns\tmaps_moved_runs\tfirst_recurrent_run\tperiod\tperiod_out\tdistinct_out_last8\tzones_d_phase\tmaxrel_d_phase\tzones_true_phase\tmaxrel_true_phase\tzones_est_phase\tmaxrel_est_phase\tout_comp_phase\tout_max_nits_phase\tzones_true_prev_end\n";
    runTsv << "cfg\trun\tphase\tk\trecur_q\tmaps_moved\tzones_d_vs_prev\tmaxrel_d_vs_prev\tzones_d_vs_prev2\tmaxrel_d_vs_prev2\thx\thout\n";
    LARGE_INTEGER freq; QueryPerformanceFrequency(&freq);
    const char* phaseName[] = { "warm", "cut", "caret", "tick" };

    std::vector<uint16_t> sA, sB;
    if (sdrFrames.size() >= 2) {
        sA.resize((size_t)W * H * 4); sB.resize((size_t)W * H * 4);
        for (int i = 0; i < 2; i++) {
            std::ifstream f(sdrFrames[(size_t)i], std::ios::binary);
            REQUIRE_MESSAGE(f.good(), sdrFrames[(size_t)i]);
            std::vector<uint16_t>& dst = i ? sB : sA;
            f.read((char*)dst.data(), (std::streamsize)(dst.size() * 2));
            REQUIRE((size_t)f.gcount() == dst.size() * 2);
        }
    }
    for (const CyCfg& c : cfgs) {
        if (!only.empty() && c.id.find(only) == std::string::npos) continue;
        if (!c.hdr && GetFileAttributesW(sdrPanel.c_str()) == INVALID_FILE_ATTRIBUTES) { MESSAGE(c.id << ": no SDR panel file - skipped"); continue; }
        if (!c.hdr && sA.empty()) { MESSAGE(c.id << ": no SDR frames (FALD_HW_CYCLE_SDR_FRAMES) - skipped"); continue; }
        const std::vector<uint16_t>& fA = c.hdr ? hA : sA;
        const std::vector<uint16_t>& fB = c.hdr ? hB : sB;
        ReleaseFaldShaders();
        std::string stat = g_faldStatSource;
        if (c.variant == "nofloor") {
            const std::string from = "        if (s > driveFloor) { m = max(m, s); sum += s; }\n";
            const size_t at = stat.find(from);
            REQUIRE_MESSAGE(at != std::string::npos, "the statistic pass's floor line was not found - the variant needs updating");
            stat.replace(at, from.size(), "        { m = max(m, s); sum += s; }\n");
        }
        std::string common = g_faldCommonSource;
        if (c.variant == "exactcurve") {
            // DriveOf through two loads and a float32 lerp: the sampler's interpolation weight has 8 fractional bits, so the
            // shipped drive is a staircase of the statistic (256 steps per knot interval)
            const std::string from = "    return curveTex.SampleLevel(linearClamp, float2((u * (float)(curveN - 1) + 0.5f) / (float)curveN, 0.5f), 0);\n";
            const size_t at = common.find(from);
            REQUIRE_MESSAGE(at != std::string::npos, "DriveOf's sampler line was not found - the variant needs updating");
            common.replace(at, from.size(),
                "    float xf = u * (float)(curveN - 1); float xi = floor(xf); int i0 = (int)xi; int i1 = min(i0 + 1, (int)curveN - 1);\n"
                "    float c0 = curveTex.Load(int3(i0, 0, 0)), c1 = curveTex.Load(int3(i1, 0, 0));\n"
                "    return c0 + (c1 - c0) * (xf - xi);\n");
        }
        if (c.variant.empty()) { REQUIRE(InitFaldShaders()); }
        else {
            FaldShaderSources src = FaldDefaultShaderSources();
            src.stat = stat.c_str();
            src.common = common.c_str();
            REQUIRE(InitFaldShadersFrom(src));
        }
        MonitorContext ctx;
        ctx.width = W; ctx.height = H; ctx.isHDREnabled = c.hdr; ctx.isFP16SDR = !c.hdr;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = (float)(1000.0 / c.hz);
        FaldSettings& fs = c.hdr ? ctx.hdrColorCorrection.fald : ctx.sdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = c.hdr ? hdrPanel : sdrPanel; fs.star.enabled = c.star; fs.coreMode = c.core;
        fs.temporalMode = FALD_TEMPORAL_PANEL; fs.clockClosure = c.closure; fs.clockParity = c.parity;
        if (!FaldEnsureResources(&ctx, fs)) { MESSAGE(c.id << ": the layer's resources failed - skipped"); FaldReleaseResources(&ctx); continue; }
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)W; td.Height = (UINT)H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
        g_device->CreateTexture2D(&td, nullptr, &target);
        if (target) g_device->CreateRenderTargetView(target, nullptr, &rtv);
        REQUIRE(rtv);

        const double periodQpc = (double)freq.QuadPart * (1000.0 / c.hz) / 1000.0;
        LARGE_INTEGER t0; QueryPerformanceCounter(&t0);
        std::vector<uint16_t> frame = fA;
        unsigned long long key = 1;
        long long refresh = 0;
        std::vector<CyRun> runs;
        std::vector<std::vector<unsigned char>> lastOut;   // the last outputs of the current phase (a ring of 4)
        std::vector<int> lastOutRun;
        g_faldPartialOverride = 0;         // the full path at every run: the study is about the values, not the path
        auto one = [&](int phase, bool newContent, bool upload, bool keepOut) {
            const int kk = runs.empty() ? 0 : c.k[(runs.size() - 1) % c.k.size()];
            refresh += kk;
            g_faldTestNowQpc = t0.QuadPart + (long long)((double)refresh * periodQpc + 0.5);
            if (upload) g_context->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)W * 8u, 0);
            D3D11_VIEWPORT vp = { 0, 0, (float)W, (float)H, 0, 1 };
            g_context->RSSetViewports(1, &vp);
            g_context->VSSetShader(hw.vs, nullptr, 0);
            g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            FaldRunPasses(&ctx, rtv, newContent, key, nullptr, -1);
            g_context->Flush();
            const FaldResources* r = ctx.fald;
            CyRun cr;
            cr.phase = phase; cr.k = (int)r->clkElapsed;
            cr.s0 = CyReadR32(r->clkStateTex[0]); cr.s1 = CyReadR32(r->clkStateTex[1]); cr.d = CyReadR32(r->clkPrevTex);
            cr.mT = CyReadR32(r->driveFiltTex); cr.mE = CyReadR32(r->clkEstTex);
            cr.hx = CyHash(cr.s0.data(), cr.s0.size() * 4) ^ (CyHash(cr.s1.data(), cr.s1.size() * 4) * 3ull) ^ (CyHash(cr.d.data(), cr.d.size() * 4) * 7ull);
            cr.hOut = 0; cr.outKept = keepOut;
            if (keepOut) {
                std::vector<unsigned char> o = DbReadBack(target);
                cr.hOut = CyHash(o.data(), o.size());
                lastOut.push_back(std::move(o)); lastOutRun.push_back((int)runs.size());
                if (lastOut.size() > 4) { lastOut.erase(lastOut.begin()); lastOutRun.erase(lastOutRun.begin()); }
            }
            runs.push_back(std::move(cr));
            std::this_thread::yield();
        };
        auto sameX = [&](size_t a, size_t b) {
            return runs[a].hx == runs[b].hx && runs[a].s0 == runs[b].s0 && runs[a].s1 == runs[b].s1 && runs[a].d == runs[b].d;
        };
        auto phaseReport = [&](int phase, size_t first) {
            // first .. runs.size() - 1 are the phase's runs (the event's run included)
            const size_t last = runs.size() - 1;
            int mapsMoved = 0;
            std::vector<int> q(runs.size(), 0);
            for (size_t n = first; n <= last; n++) {
                if (n > 0 && (runs[n].mT != runs[n - 1].mT || runs[n].mE != runs[n - 1].mE)) mapsMoved++;
                for (size_t p = 1; p <= 16 && p <= n; p++) {
                    if (n - p < first) break;                       // recurrence inside the phase only
                    // the factor sequence must repeat with the same period: the schedule's k of the last p runs = the p before
                    bool sched = true;
                    for (size_t j = 0; j < p && sched; j++) if (n - j < p || runs[n - j].k != runs[n - j - p].k) sched = false;
                    if (sched && sameX(n, n - p)) { q[n] = (int)p; break; }
                }
            }
            size_t firstRec = (size_t)-1;
            for (size_t n = last + 1; n-- > first;) { if (q[n] == 0) break; firstRec = n; }
            const int period = firstRec == (size_t)-1 ? 0 : q[last];
            // the output's own period over the kept tail, and how many distinct frames it holds
            int periodOut = 0, distinct = 0;
            {
                std::vector<uint64_t> hs;
                for (size_t n = (last >= 7 ? last - 7 : first); n <= last; n++) if (runs[n].outKept) hs.push_back(runs[n].hOut);
                std::vector<uint64_t> u = hs; std::sort(u.begin(), u.end()); u.erase(std::unique(u.begin(), u.end()), u.end());
                distinct = (int)u.size();
                for (int p = 1; p <= 4 && !periodOut; p++) {
                    bool ok = hs.size() > (size_t)p;
                    for (size_t i = p; i < hs.size() && ok; i++) if (hs[i] != hs[i - p]) ok = false;
                    if (ok) periodOut = p;
                }
            }
            size_t zd = 0, zt = 0, ze = 0, ztp = 0; double rd = 0, rt = 0, re = 0, rtp = 0;
            if (last >= first + 1) {
                CyDiff(runs[last].d, runs[last - 1].d, zd, rd);
                CyDiff(runs[last].mT, runs[last - 1].mT, zt, rt);
                CyDiff(runs[last].mE, runs[last - 1].mE, ze, re);
                ztp = zt; rtp = rt;
            }
            size_t comp = 0; double maxAbs = 0.0;
            if (lastOut.size() >= 2) {
                const std::vector<unsigned char>& fa = lastOut[lastOut.size() - 1]; const std::vector<unsigned char>& fb = lastOut[lastOut.size() - 2];
                const uint16_t* a = (const uint16_t*)fa.data(); const uint16_t* b = (const uint16_t*)fb.data();
                for (size_t j = 0; j < fa.size() / 2; j++) {
                    if (a[j] == b[j]) continue;
                    comp++;
                    const double dv = std::fabs((double)DbHalf(a[j]) - (double)DbHalf(b[j]));
                    if (dv > maxAbs) maxAbs = dv;
                }
            }
            const double nitsPer = c.hdr ? 80.0 : 0.0;   // HDR: scRGB 1.0 = 80 nits; SDR / ACM: the value is reported in scRGB units x 0 (see max below)
            phTsv << hw.adapter << "\t" << c.id << "\t" << phaseName[phase] << "\t" << (last - first + 1) << "\t" << mapsMoved << "\t"
                  << (firstRec == (size_t)-1 ? -1 : (int)(firstRec - first)) << "\t" << period << "\t" << periodOut << "\t" << distinct << "\t"
                  << zd << "\t" << rd << "\t" << zt << "\t" << rt << "\t" << ze << "\t" << re << "\t" << comp << "\t" << (c.hdr ? maxAbs * nitsPer : maxAbs) << "\t" << ztp << "\n";
            for (size_t n = first; n <= last; n++) {
                size_t z1 = 0, z2 = 0; double r1 = 0, r2 = 0;
                if (n >= 1) CyDiff(runs[n].d, runs[n - 1].d, z1, r1);
                if (n >= 2) CyDiff(runs[n].d, runs[n - 2].d, z2, r2);
                const bool mv = n > 0 && (runs[n].mT != runs[n - 1].mT || runs[n].mE != runs[n - 1].mE);
                runTsv << c.id << "\t" << n << "\t" << phaseName[phase] << "\t" << runs[n].k << "\t" << q[n] << "\t" << (mv ? 1 : 0) << "\t" << z1 << "\t" << r1 << "\t"
                       << z2 << "\t" << r2 << "\t" << std::hex << runs[n].hx << "\t" << runs[n].hOut << std::dec << "\n";
            }
            MESSAGE(c.id << " / " << std::string(phaseName[phase]) << ": " << mapsMoved << " of " << (last - first + 1) << " runs moved a map; X recurs from run "
                    << (firstRec == (size_t)-1 ? -1 : (int)(firstRec - first)) << " with period " << period << " (output period " << periodOut << ", " << distinct
                    << " distinct frames in the last 8); between the last two runs: d " << zd << " zones (max rel " << rd << "), true map " << zt << " zones (max rel " << rt
                    << "), output " << comp << " components, max " << (c.hdr ? maxAbs * 80.0 : maxAbs) << std::string(c.hdr ? " nits" : " (scRGB)"));
        };
        auto stills = [&](int phase, int n) {
            for (int i = 0; i < n; i++) one(phase, false, false, i >= n - 8);
        };
        // warm: three runs of frame A
        for (int i = 0; i < 3; i++) one(0, true, true, false);
        // the cut to frame B, then a static desktop
        size_t first = runs.size();
        frame = fB; key++;
        one(1, true, true, false);
        stills(1, nStill);
        phaseReport(1, first);
        // a caret: dark, 4 x 36 px, in the document of the desktop frame (moves no drive)
        first = runs.size();
        for (int y = 648; y < 684; y++) for (int x = 400; x < 404; x++) for (int ch = 0; ch < 3; ch++) frame[((size_t)y * W + x) * 4 + ch] = 0x2800;
        key++;
        one(2, true, true, false);
        stills(2, nStill);
        phaseReport(2, first);
        // the task bar clock's digits: 160 x 64 px of ink on dark at the bottom right (moves drives)
        first = runs.size();
        for (int y = H - 80; y < H - 16; y++)
            for (int x = W - 180; x < W - 20; x++) {
                const uint32_t hsh = (uint32_t)(x / 4) * 73856093u ^ (uint32_t)(y / 4) * 19349663u;
                const uint16_t v = (hsh >> 7) % 10u < 3u ? 0x4000 : 0x2C00;   // 2.0 (160 nits) ink on 0.0625
                for (int ch = 0; ch < 3; ch++) frame[((size_t)y * W + x) * 4 + ch] = v;
            }
        key++;
        one(3, true, true, false);
        stills(3, nStill);
        phaseReport(3, first);
        g_faldTestNowQpc = 0;
        if (HwEnv("FALD_HW_CYCLE_DUMP") == "1") {   // per run: phase, k, then s0, s1, d, true map, est map (cols x rows float32 each)
            std::ofstream raw(out + "\\" + c.id + ".f32", std::ios::binary);
            for (const CyRun& r : runs) {
                const float head[2] = { (float)r.phase, (float)r.k };
                raw.write((const char*)head, sizeof(head));
                for (const std::vector<float>* v : { &r.s0, &r.s1, &r.d, &r.mT, &r.mE }) raw.write((const char*)v->data(), (std::streamsize)(v->size() * 4));
            }
        }
        rtv->Release(); target->Release();
        FaldReleaseResources(&ctx);
        g_context->ClearState(); g_context->Flush();
    }
    ReleaseFaldShaders();
    InitFaldShaders();   // (HwDev's destructor releases the shipped set)
}

// ---------------------------------------------------------------------------------------------------------------------
// The panel clock AT REST (shared/fald_temporal.h) against the UNHELD layer, refresh for refresh, on the hardware adapter
// (opt-in; the overlay's layer at 4K, the owner's panel files). Two layers run the same desktop on the same refresh
// grid: U with g_faldClockRestOverride = 0 (the clock never rests: the layer as it was) and R with the shipped rule. A
// scenario is a cut to the desktop frame, the settle presents, then CYCLES of one content event (a caret, the clock's
// digits, a hover, a scroll, a moved window — or content every refresh: video) every 0.533 s; after a content run
// each layer is presented again every refresh while ITS settle hold is pending and for the kicker's linger after it (the
// host's FALD_KICK_QUIET_MS = 100 ms), as DWM would be kicked. Time is SET (g_faldTestNowQpc).
// Per refresh in which either layer ran, the two outputs are compared (what is on the panel then): components that
// differ and the largest difference in nits; for R's runs at rest on a still desktop, whether its frame is one the unheld
// layer itself produced since the last content event. Per layer: runs, runs that paid the convolutions (the full cost),
// the clock runs reported at rest. -> <dir>/rest_vs_unheld.tsv
// FALD_HW_REST_OUT=<dir>; FALD_HW_REST_FRAMES="warm.rgba16f;desktop.rgba16f" (3840 x 2160 FP16, the layer's input);
// FALD_HW_REST_SDR_FRAMES likewise for the SDR / ACM configuration (optional); FALD_HW_REST_ONLY = a substring of
// "<cfg>/<scenario>"; FALD_HW_REST_CYCLES (6).
// ---------------------------------------------------------------------------------------------------------------------
namespace {
uint16_t RhHalf(float f) {                        // non-negative finite floats
    if (!(f > 0.0f)) return 0;
    uint32_t u; std::memcpy(&u, &f, 4);
    const int e = (int)((u >> 23) & 0xFF) - 127 + 15;
    if (e <= 0) return 0;
    if (e >= 31) return 0x7BFF;
    return (uint16_t)((e << 10) | ((u >> 13) & 0x3FF));
}
// the watch pass's per-zone levels of a layer: zones with a toggle pair, the widest pair (relative to its upper target, and in
// drive units)
void RhPairs(const FaldResources* r, int& pairs, double& widestRel, double& widestAbs) {
    pairs = 0; widestRel = 0.0; widestAbs = 0.0;
    ID3D11Buffer* buf = r->clkRest.levels;
    if (!buf) return;
    D3D11_BUFFER_DESC bd; buf->GetDesc(&bd);
    const UINT bytes = bd.ByteWidth;
    bd.Usage = D3D11_USAGE_STAGING; bd.BindFlags = 0; bd.CPUAccessFlags = D3D11_CPU_ACCESS_READ; bd.MiscFlags = 0; bd.StructureByteStride = 0;
    ID3D11Buffer* st = nullptr;
    if (FAILED(g_device->CreateBuffer(&bd, nullptr, &st))) return;
    g_context->CopyResource(st, buf);
    D3D11_MAPPED_SUBRESOURCE mp;
    if (SUCCEEDED(g_context->Map(st, 0, D3D11_MAP_READ, 0, &mp))) {
        const float* v = (const float*)mp.pData;
        for (UINT z = 0; z + 16 <= bytes; z += 16, v += 4) {
            if (v[2] < 2.0f) continue;
            pairs++;
            const double hi = std::max(v[0], v[1]), lo = std::min(v[0], v[1]);
            widestAbs = std::max(widestAbs, hi - lo);
            if (hi > 0.0) widestRel = std::max(widestRel, (hi - lo) / hi);
        }
        g_context->Unmap(st, 0);
    }
    st->Release();
}
struct RhLayer {
    MonitorContext ctx;
    ID3D11Texture2D* target = nullptr; ID3D11RenderTargetView* rtv = nullptr;
    int restOverride = -1;
    bool ok = false;
    long long lastRun = -1000000, lastSettle = -1000000;   // refresh indices
    unsigned long long key = 1;
    int runs = 0, costly = 0, kicks = 0, contentRuns = 0;
    RhLayer(const std::wstring& panel, int W, int H, bool hdr, bool star, unsigned int core, int parity, float closure, double hz, int rest)
        : restOverride(rest) {
        ctx.width = W; ctx.height = H; ctx.isHDREnabled = hdr; ctx.isFP16SDR = !hdr;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT; ctx.frameTimeExactMs = (float)(1000.0 / hz);
        FaldSettings& fs = hdr ? ctx.hdrColorCorrection.fald : ctx.sdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.star.enabled = star; fs.coreMode = core;
        fs.temporalMode = FALD_TEMPORAL_PANEL; fs.clockClosure = closure; fs.clockParity = parity;
        if (!FaldEnsureResources(&ctx, fs)) return;
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = (UINT)W; td.Height = (UINT)H; td.MipLevels = 1; td.ArraySize = 1; td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ok = SUCCEEDED(g_device->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(g_device->CreateRenderTargetView(target, nullptr, &rtv));
    }
    ~RhLayer() { if (rtv) rtv->Release(); if (target) target->Release(); FaldReleaseResources(&ctx); }
    // One run at refresh `t`. rects: the content's dirty rects (already in `frame`); null + content = the whole frame.
    void Run(HwDev& hw, long long t, long long t0Qpc, double periodQpc, const std::vector<uint16_t>& frame, bool content,
             const std::vector<FaldChangeRect>* rects) {
        const int W = ctx.width, H = ctx.height;
        g_faldClockRestOverride = restOverride;
        g_faldTestNowQpc = t0Qpc + (long long)((double)t * periodQpc + 0.5);
        if (content) {
            key++;
            if (!rects) g_context->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)W * 8u, 0);
            else for (const FaldChangeRect& rc : *rects) {
                D3D11_BOX box = { (UINT)rc.left, (UINT)rc.top, 0, (UINT)rc.right, (UINT)rc.bottom, 1 };
                g_context->UpdateSubresource(ctx.fald->inter, 0, &box, frame.data() + ((size_t)rc.top * W + (size_t)rc.left) * 4u, (UINT)W * 8u, 0);
            }
        }
        D3D11_VIEWPORT vp = { 0, 0, (float)W, (float)H, 0, 1 };
        g_context->RSSetViewports(1, &vp);
        g_context->VSSetShader(hw.vs, nullptr, 0);
        g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        if (content && !rects) FaldRunPasses(&ctx, rtv, true, key, nullptr, -1);
        else if (content) FaldRunPasses(&ctx, rtv, true, key, rects->data(), (int)rects->size());
        else FaldRunPasses(&ctx, rtv, false, key, nullptr, 0);
        g_context->Flush();
        g_faldTestNowQpc = 0; g_faldClockRestOverride = -1;
        runs++; if (content) contentRuns++; else kicks++;
        if (!ctx.fald->lastPartial || DbConvRan(ctx.fald)) costly++;
        lastRun = t;
        if (FaldSettlePending(&ctx)) lastSettle = t;
    }
};
}  // namespace

TEST_CASE("FALD HW: the panel clock at rest against the unheld layer, refresh for refresh (FALD_HW_REST_OUT)") {
    const std::string out = HwEnv("FALD_HW_REST_OUT");
    if (out.empty()) return;
    CreateDirectoryA(out.c_str(), nullptr);
    auto split = [](const std::string& s) { std::vector<std::string> v; std::stringstream ss(s); std::string t; while (std::getline(ss, t, ';')) if (!t.empty()) v.push_back(t); return v; };
    const std::vector<std::string> hdrFrames = split(HwEnv("FALD_HW_REST_FRAMES")), sdrFrames = split(HwEnv("FALD_HW_REST_SDR_FRAMES"));
    REQUIRE_MESSAGE(hdrFrames.size() >= 2, "FALD_HW_REST_FRAMES needs two 3840 x 2160 FP16 frames");
    const std::string only = HwEnv("FALD_HW_REST_ONLY");
    const int cycles = std::max(2, atoi(HwEnv("FALD_HW_REST_CYCLES").empty() ? "6" : HwEnv("FALD_HW_REST_CYCLES").c_str()));
    const std::wstring hdrPanel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_candidate_2026-10-09\\candidate_C_fade3_only.bin";
    const std::wstring sdrPanel = L"H:\\Projects\\DesktopLUT\\DLC\\results\\fald_daily_2026-10-06\\pa32ucxr_refit_sdr_fald_panel_blur0.7.bin";
    const int W = 3840, H = 2160;
    auto load = [&](const std::string& path, std::vector<uint16_t>& dst) {
        dst.assign((size_t)W * H * 4, 0);
        std::ifstream f(path, std::ios::binary);
        if (!f.good()) return false;
        f.read((char*)dst.data(), (std::streamsize)(dst.size() * 2));
        return (size_t)f.gcount() == dst.size() * 2;
    };
    struct Cfg { std::string id; bool hdr, star; unsigned int core; int parity; float closure; double hz; };
    const std::vector<Cfg> cfgs = {
        // the owner's monitor (PA32UCXR: 60 Hz at most) at the rates it is driven at, HDR and SDR / ACM — the settings of
        // his DesktopLUT.ini: both panel files, PANEL CLOCK, closure 0.72, parity unknown, starfield on, the core KC5
        { "owner_60",    true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 60.0 },
        { "owner_5994",  true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 60000.0 / 1001.0 },
        { "owner_50",    true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 50.0 },
        { "owner_48",    true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 48.0 },
        { "owner_47952", true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 48000.0 / 1001.0 },
        { "sdr_60",      false, true, FALD_CORE_MODE_KC5, -1, 0.72f, 60.0 },
        { "sdr_47952",   false, true, FALD_CORE_MODE_KC5, -1, 0.72f, 48000.0 / 1001.0 },
        // other panels' rates, other settings
        { "owner_120", true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 120.0 },
        { "owner_144", true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 144.0 },
        { "owner_240", true, true, FALD_CORE_MODE_KC5, -1, 0.72f, 240.0 },
        { "sdr_120",   false, true, FALD_CORE_MODE_KC5, -1, 0.72f, 120.0 },
        { "plain_120", true, false, FALD_CORE_MODE_C, -1, 0.72f, 120.0 },
        { "par0_120",  true, true, FALD_CORE_MODE_KC5, 0, 0.72f, 120.0 },
        { "clo050_120", true, true, FALD_CORE_MODE_KC5, -1, 0.50f, 120.0 },
        { "clo100_120", true, true, FALD_CORE_MODE_KC5, -1, 1.00f, 120.0 },
    };
    // the events (4K coordinates; the desktop frame of tests/test_perf.cpp: a document left, a video window in the middle,
    // a dark editor right, the task bar's clock bottom right)
    struct Scn { const char* id; int kind; FaldChangeRect rc; bool everyRefresh; };
    const std::vector<Scn> scns = {
        { "still",        0, { 0, 0, 0, 0 }, false },                       // a present with no content every 64 refreshes
        { "caret",        1, { 400, 648, 404, 684 }, false },
        { "clock",        2, { W - 180, H - 80, W - 20, H - 16 }, false },
        { "hover",        3, { 600, 1900, 856, 2060 }, false },
        { "scroll_doc",   4, { 160, 120, 640, 1800 }, false },
        { "window_move",  5, { 2100, 600, 3148, 1200 }, false },
        { "video_window", 6, { 640, 360, 3200, 1800 }, true },
        { "full_video",   6, { 0, 0, W, H }, true },
    };
    HwDev hw;
    REQUIRE(hw.ok);
    const int savedHold = g_faldCoreHoldOverride, savedPartial = g_faldPartialOverride;
    g_faldCoreHoldOverride = 0; g_faldPartialOverride = -1;
    struct Restore { int h, p; ~Restore() { g_faldCoreHoldOverride = h; g_faldPartialOverride = p; g_faldTestNowQpc = 0; g_faldClockRestOverride = -1; } } restore{ savedHold, savedPartial };
    std::ofstream tsv(out + "\\rest_vs_unheld.tsv");
    tsv << "adapter\tcfg\tscenario\tphase\trefreshes_compared\tidentical\tmax_components\tmax_nits\tsum_components\t"
           "u_runs\tu_costly\tu_kicks\tr_runs\tr_costly\tr_kicks\tr_clock_runs\tr_rest_runs\tr_still_at_rest\tr_still_in_unheld_set\tfirst_rest_refresh\t"
           "pairs\twidest_pair_rel\twidest_pair_drive\n";
    LARGE_INTEGER freq; QueryPerformanceFrequency(&freq);

    for (const Cfg& c : cfgs) {
        std::vector<uint16_t> fA, fB;
        const std::vector<std::string>& fr = c.hdr ? hdrFrames : sdrFrames;
        if (fr.size() < 2) { MESSAGE(c.id << ": no frames for this mode - skipped"); continue; }
        if (!c.hdr && GetFileAttributesW(sdrPanel.c_str()) == INVALID_FILE_ATTRIBUTES) { MESSAGE(c.id << ": no SDR panel file - skipped"); continue; }
        bool loaded = false;
        for (const Scn& sc : scns) {
            if (!only.empty() && (c.id + "/" + sc.id).find(only) == std::string::npos) continue;
            if (!loaded) { REQUIRE(load(fr[0], fA)); REQUIRE(load(fr[1], fB)); loaded = true; }
            RhLayer U(c.hdr ? hdrPanel : sdrPanel, W, H, c.hdr, c.star, c.core, c.parity, c.closure, c.hz, 0);
            RhLayer R(c.hdr ? hdrPanel : sdrPanel, W, H, c.hdr, c.star, c.core, c.parity, c.closure, c.hz, -1);
            REQUIRE((U.ok && R.ok));
            const double periodQpc = (double)freq.QuadPart / c.hz;
            LARGE_INTEGER t0; QueryPerformanceCounter(&t0);
            const long long linger = (long long)std::ceil(100.0 * c.hz / 1000.0);   // the kicker's 100 ms, in refreshes
            const long long kGap = std::max(8ll, (long long)(0.533 * c.hz + 0.5));  // a content event every 0.533 s (a caret's blink)
            std::vector<uint16_t> frame = fA;
            const std::vector<uint16_t> base = fB;
            // per phase: the comparison's numbers
            struct Acc { long long compared = 0, identical = 0; size_t maxComp = 0, sumComp = 0; double maxNits = 0.0;
                         long long stillAtRest = 0, stillInSet = 0, firstRest = -1; };
            Acc acc;
            std::vector<uint64_t> unheldSet;            // U's output hashes since the last content event
            unsigned long long restRunsBefore = 0;
            int snapU[4] = {}, snapR[4] = {};
            auto snap = [&]() { snapU[0] = U.runs; snapU[1] = U.costly; snapU[2] = U.kicks; snapR[0] = R.runs; snapR[1] = R.costly; snapR[2] = R.kicks;
                                restRunsBefore = R.ctx.fald->clkRestRuns; snapR[3] = (int)R.ctx.fald->clkRunSeq; };
            auto flush = [&](const char* phase) {
                tsv << hw.adapter << "\t" << c.id << "\t" << sc.id << "\t" << phase << "\t" << acc.compared << "\t" << acc.identical << "\t" << acc.maxComp << "\t"
                    << acc.maxNits << "\t" << acc.sumComp << "\t" << (U.runs - snapU[0]) << "\t" << (U.costly - snapU[1]) << "\t" << (U.kicks - snapU[2]) << "\t"
                    << (R.runs - snapR[0]) << "\t" << (R.costly - snapR[1]) << "\t" << (R.kicks - snapR[2]) << "\t" << ((int)R.ctx.fald->clkRunSeq - snapR[3]) << "\t"
                    << (R.ctx.fald->clkRestRuns - restRunsBefore) << "\t" << acc.stillAtRest << "\t" << acc.stillInSet << "\t" << acc.firstRest << "\t";
                int pairs = 0; double wRel = 0.0, wAbs = 0.0;
                RhPairs(R.ctx.fald, pairs, wRel, wAbs);
                tsv << pairs << "\t" << wRel << "\t" << wAbs << "\n";
                tsv.flush();
                MESSAGE(c.id << " / " << std::string(sc.id) << " / " << std::string(phase) << ": " << acc.compared << " refreshes compared, " << acc.identical << " identical, at most "
                        << acc.maxComp << " components differ, max " << acc.maxNits << " nits; unheld " << (U.runs - snapU[0]) << " runs (" << (U.costly - snapU[1])
                        << " at full cost), at rest " << (R.runs - snapR[0]) << " runs (" << (R.costly - snapR[1]) << " at full cost; " << (R.ctx.fald->clkRestRuns - restRunsBefore)
                        << " clock runs at rest); R's still frames at rest: " << acc.stillInSet << " of " << acc.stillAtRest << " are frames the unheld layer produced; "
                        << pairs << " zones with a toggle pair, the widest " << wRel << " of its target");
                acc = Acc();
            };
            // one refresh: the content event (if any) for both, else each layer's own kick; then the comparison
            auto step = [&](long long t, bool content, const std::vector<FaldChangeRect>* rects) {
                bool ranU = false, ranR = false;
                for (RhLayer* L : { &U, &R }) {
                    const bool kick = !content && (t - L->lastSettle) <= linger && L->lastRun < t && L->lastSettle > -1000;
                    if (content || kick) { L->Run(hw, t, t0.QuadPart, periodQpc, frame, content, rects); (L == &U ? ranU : ranR) = true; }
                }
                if (!ranU && !ranR) return;
                const std::vector<unsigned char> a = DbReadBack(U.target), b = DbReadBack(R.target);
                const uint64_t ha = CyHash(a.data(), a.size()), hb = CyHash(b.data(), b.size());
                if (content) unheldSet.clear();
                if (ranU) unheldSet.push_back(ha);
                acc.compared++;
                if (a == b) acc.identical++;
                else {
                    const uint16_t* pa = (const uint16_t*)a.data(); const uint16_t* pb = (const uint16_t*)b.data();
                    size_t comp = 0; double mx = 0.0;
                    for (size_t j = 0; j < a.size() / 2; j++) {
                        if (pa[j] == pb[j]) continue;
                        comp++;
                        const double dv = std::fabs((double)DbHalf(pa[j]) - (double)DbHalf(pb[j]));
                        if (dv > mx) mx = dv;
                    }
                    acc.maxComp = std::max(acc.maxComp, comp); acc.sumComp += comp;
                    acc.maxNits = std::max(acc.maxNits, mx * 80.0);
                }
                // R ran a still present that cost nothing: is its frame one the unheld layer produced since the content event?
                if (ranR && !content && R.ctx.fald->lastPartial && !DbConvRan(R.ctx.fald)) {
                    acc.stillAtRest++;
                    if (std::find(unheldSet.begin(), unheldSet.end(), hb) != unheldSet.end()) acc.stillInSet++;
                    if (acc.firstRest < 0) acc.firstRest = t;
                }
                std::this_thread::yield();
            };
            long long t = 0;
            // warm: frame A, three presents; the cut to the desktop, its settle presents
            snap();
            for (int i = 0; i < 3; i++) { step(t, true, nullptr); t++; }
            frame = base;
            step(t, true, nullptr); t++;
            for (long long e = t + kGap; t < e; t++) step(t, false, nullptr);
            flush("cut");
            // the cycles
            snap();
            for (int cy = 0; cy < cycles; cy++) {
                const long long end = t + kGap;
                for (; t < end; t++) {
                    const bool first = (t == end - kGap);
                    if (!(first || sc.everyRefresh)) { step(t, false, nullptr); continue; }
                    const bool on = sc.everyRefresh ? true : (cy % 2 == 0);
                    const FaldChangeRect rc = sc.rc;
                    std::vector<FaldChangeRect> rects;
                    auto px = [&](int x, int y) { return &frame[((size_t)y * W + (size_t)x) * 4]; };
                    auto bpx = [&](int x, int y) { return &base[((size_t)y * W + (size_t)x) * 4]; };
                    switch (sc.kind) {
                    case 0: break;   // no content: a present that only wakes the layer (treated as a kick below)
                    case 1:          // the caret: dark, then the document's own pixels again
                        for (int y = rc.top; y < rc.bottom; y++) for (int x = rc.left; x < rc.right; x++)
                            for (int ch = 0; ch < 3; ch++) px(x, y)[ch] = on ? (uint16_t)0x2800 : bpx(x, y)[ch];
                        break;
                    case 2:          // the clock's digits: other ink every time
                        for (int y = rc.top; y < rc.bottom; y++) for (int x = rc.left; x < rc.right; x++) {
                            const uint32_t hsh = ((uint32_t)(x / 4) * 73856093u) ^ ((uint32_t)(y / 4) * 19349663u) ^ ((uint32_t)cy * 83492791u);
                            const uint16_t v = (hsh >> 7) % 10u < 3u ? RhHalf(c.hdr ? 2.0f : 0.8f) : RhHalf(0.02f);
                            for (int ch = 0; ch < 3; ch++) px(x, y)[ch] = v;
                        }
                        break;
                    case 3:          // a hover highlight: the wallpaper x 1.15, then back
                        for (int y = rc.top; y < rc.bottom; y++) for (int x = rc.left; x < rc.right; x++)
                            for (int ch = 0; ch < 3; ch++) px(x, y)[ch] = on ? RhHalf(DbHalf(bpx(x, y)[ch]) * 1.15f) : bpx(x, y)[ch];
                        break;
                    case 4:          // the document scrolls one text line (36 px), cyclically
                        for (int y = rc.top; y < rc.bottom; y++) {
                            const int sy = rc.top + ((y - rc.top) + 36 * (cy + 1)) % (rc.bottom - rc.top);
                            std::memcpy(px(rc.left, y), bpx(rc.left, sy), (size_t)(rc.right - rc.left) * 8u);
                        }
                        break;
                    case 5: {        // a light window moves 48 px over what is there, then back
                        for (int y = rc.top; y < rc.bottom; y++) std::memcpy(px(rc.left, y), bpx(rc.left, y), (size_t)(rc.right - rc.left) * 8u);
                        const int x0 = rc.left + (on ? 48 : 0), x1 = rc.right - (on ? 0 : 48);
                        const uint16_t v = RhHalf(c.hdr ? 2.4f : 0.92f);
                        for (int y = rc.top; y < rc.bottom; y++) for (int x = x0; x < x1; x++) for (int ch = 0; ch < 3; ch++) px(x, y)[ch] = v;
                        break;
                    }
                    case 6: {        // video: the rect's content moves 61 px per refresh
                        const int w = rc.right - rc.left, sh = (int)((t * 61) % w);
                        for (int y = rc.top; y < rc.bottom; y++) {
                            std::memcpy(px(rc.left, y), bpx(rc.left + sh, y), (size_t)(w - sh) * 8u);
                            if (sh) std::memcpy(px(rc.left + (w - sh), y), bpx(rc.left, y), (size_t)sh * 8u);
                        }
                        break;
                    }
                    }
                    if (sc.kind == 0) {   // wake both layers without content: one present, then whatever each still owes
                        for (RhLayer* L : { &U, &R }) L->lastSettle = t - 1;   // (a kick-zone present: the kicker runs one round)
                        step(t, false, nullptr);
                    } else { rects.push_back(rc); step(t, true, &rects); }
                }
            }
            flush("cycles");
        }
    }
}
