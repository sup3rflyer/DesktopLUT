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
