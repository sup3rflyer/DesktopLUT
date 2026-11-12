// Shared pieces of the performance instruments (tests/test_perf.cpp): devices, GPU timestamps, robust statistics,
// deterministic synthetic frames, texture read-back / hashing, and the HLSL text tools the shader variants are made with.
// Test-side only. Nothing here runs unless a DLUT_PERF_* environment variable asks for it.
#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <iterator>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace perf {

inline std::string Env(const char* name) {
    char buf[2048] = {};
    const DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? std::string(buf, n) : std::string();
}
inline int EnvInt(const char* name, int dflt) {
    const std::string v = Env(name);
    return v.empty() ? dflt : atoi(v.c_str());
}
inline bool Contains(const std::string& hay, const std::string& needle) {
    return needle.empty() || hay.find(needle) != std::string::npos;
}
// A filter of comma-separated substrings: empty = everything, else any one must occur in `id`.
inline bool Selected(const std::string& filter, const std::string& id) {
    if (filter.empty()) return true;
    size_t at = 0;
    while (at <= filter.size()) {
        const size_t comma = filter.find(',', at);
        const std::string part = filter.substr(at, comma == std::string::npos ? std::string::npos : comma - at);
        if (!part.empty() && id.find(part) != std::string::npos) return true;
        if (comma == std::string::npos) break;
        at = comma + 1;
    }
    return false;
}

// The owner works on this PC while the instruments run: below-normal priority, CPUs 0-27 at most (cpuMask narrows it
// further for WARP, whose rasteriser threads would otherwise take every CPU the process may use).
inline void BePolite(DWORD_PTR cpuMask = 0x0FFFFFFFull) {
    SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS);
    DWORD_PTR proc = 0, sys = 0;
    if (GetProcessAffinityMask(GetCurrentProcess(), &proc, &sys)) {
        const DWORD_PTR want = proc & cpuMask;
        if (want) SetProcessAffinityMask(GetCurrentProcess(), want);
    }
}

inline double QpcUs() {
    static LARGE_INTEGER f = {};
    if (!f.QuadPart) QueryPerformanceFrequency(&f);
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return (double)t.QuadPart * 1e6 / (double)f.QuadPart;
}

struct Stats { double med = -1, p10 = -1, p90 = -1, mn = -1, mx = -1; int n = 0; };
inline Stats MakeStats(std::vector<double> v) {
    Stats s;
    v.erase(std::remove_if(v.begin(), v.end(), [](double x) { return x < 0.0; }), v.end());
    s.n = (int)v.size();
    if (v.empty()) return s;
    std::sort(v.begin(), v.end());
    auto q = [&](double p) { return v[(size_t)std::min<double>((double)v.size() - 1.0, std::floor(p * (double)(v.size() - 1) + 0.5))]; };
    s.med = q(0.5); s.p10 = q(0.1); s.p90 = q(0.9); s.mn = v.front(); s.mx = v.back();
    return s;
}

inline uint64_t Fnv(const void* data, size_t n, uint64_t h = 1469598103934665603ull) {
    const unsigned char* b = (const unsigned char*)data;
    for (size_t i = 0; i < n; i++) { h ^= b[i]; h *= 1099511628211ull; }
    return h;
}

// ---------------------------------------------------------------------------------------------------------------
// Half floats
// ---------------------------------------------------------------------------------------------------------------
inline uint16_t FloatToHalf(float f) {   // round to nearest even
    uint32_t x; std::memcpy(&x, &f, 4);
    const uint32_t sign = (x >> 16) & 0x8000u;
    const uint32_t e8 = (x >> 23) & 0xFFu;
    uint32_t mant = x & 0x7FFFFFu;
    if (e8 == 0xFFu) return (uint16_t)(sign | 0x7C00u | (mant ? 0x200u : 0u));
    const int exp = (int)e8 - 127 + 15;
    if (exp >= 31) return (uint16_t)(sign | 0x7C00u);
    if (exp <= 0) {
        if (exp < -10 || e8 == 0) return (uint16_t)sign;
        mant |= 0x800000u;
        const uint32_t shift = (uint32_t)(14 - exp);
        uint32_t h = mant >> shift;
        const uint32_t rem = mant & ((1u << shift) - 1u), half = 1u << (shift - 1u);
        if (rem > half || (rem == half && (h & 1u))) h++;
        return (uint16_t)(sign | h);
    }
    uint32_t h = ((uint32_t)exp << 10) | (mant >> 13);
    const uint32_t rem = mant & 0x1FFFu;
    if (rem > 0x1000u || (rem == 0x1000u && (h & 1u))) h++;
    return (uint16_t)(sign | h);
}
inline float HalfToFloat(uint16_t h) {
    const uint32_t sign = (h >> 15) & 1u, mant0 = h & 0x3FFu;
    uint32_t exp = (h >> 10) & 0x1Fu, mant = mant0, f;
    if (exp == 0) {
        if (mant == 0) f = sign << 31;
        else {
            exp = 113;
            while (!(mant & 0x400u)) { mant <<= 1; exp--; }
            mant &= 0x3FFu;
            f = (sign << 31) | (exp << 23) | (mant << 13);
        }
    } else if (exp == 31) f = (sign << 31) | 0x7F800000u | (mant << 13);
    else f = (sign << 31) | ((exp + 112u) << 23) | (mant << 13);
    float out; std::memcpy(&out, &f, 4);
    return out;
}

// ---------------------------------------------------------------------------------------------------------------
// Synthetic frames. Deterministic (integer hashes, std::sin / std::pow of exactly representable arguments are not
// guaranteed identical across CRT versions, so the frames are part of what a reference manifest was recorded with:
// re-record after a toolset change; the manifest's first line carries the frames' own hash).
// ---------------------------------------------------------------------------------------------------------------
enum class Dom { Sdr8, Sdr10, Acm, Hdr };          // B8G8R8A8 code, R10G10B10A2 code, FP16 scRGB SDR, FP16 scRGB HDR
enum class Kind { Video, Mix, Torture };           // full-frame HDR-video-like; a desktop with a video window; ramps + noise

inline const char* DomName(Dom d) { return d == Dom::Sdr8 ? "sdr8" : d == Dom::Sdr10 ? "sdr10" : d == Dom::Acm ? "acm" : "hdr"; }
inline DXGI_FORMAT DomFormat(Dom d) {
    return d == Dom::Sdr8 ? DXGI_FORMAT_B8G8R8A8_UNORM : d == Dom::Sdr10 ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R16G16B16A16_FLOAT;
}
inline unsigned DomBpp(Dom d) { return (d == Dom::Sdr8 || d == Dom::Sdr10) ? 4u : 8u; }

inline float Hash01(uint32_t x, uint32_t y, uint32_t s) {
    uint32_t h = x * 374761393u + y * 668265263u + s * 2246822519u;
    h = (h ^ (h >> 13)) * 1274126177u;
    h ^= h >> 16;
    return (float)(h & 0xFFFFFFu) * (1.0f / 16777216.0f);
}
inline float SrgbEotfF(float v) { return v <= 0.04045f ? v / 12.92f : std::pow((v + 0.055f) / 1.055f, 2.4f); }
inline float SrgbOetfF(float l) { return l <= 0.0031308f ? l * 12.92f : 1.055f * std::pow(l, 1.0f / 2.4f) - 0.055f; }
inline float PqEotfNits(float pq) {
    const float vm = std::pow(std::max(pq, 0.0f), 1.0f / 78.84375f);
    return 10000.0f * std::pow(std::max(vm - 0.8359375f, 0.0f) / (18.8515625f - 18.6875f * vm), 1.0f / 0.1593017578125f);
}

// The video window of the Mix frames (2/3 of the frame, centred): also the "video in a window" dirty rect.
inline RECT VideoWindow(int W, int H) {
    const int w = (W * 2 / 3) & ~1, h = (H * 2 / 3) & ~1;
    RECT r = { (W - w) / 2, (H - h) / 2, (W - w) / 2 + w, (H - h) / 2 + h };
    return r;
}

// A desktop in sRGB code values (u, v in 1080p units so the layout scales with the resolution).
inline void DeskCode(float u, float v, int x, int y, float c[3]) {
    if (v >= 1032.0f) {                                         // task bar
        const int icon = (int)(u / 56.0f);
        const float iu = u - (float)icon * 56.0f;
        if ((icon & 1) == 0 && iu > 12.0f && iu < 44.0f && v > 1040.0f && v < 1072.0f) {
            c[0] = 0.25f + 0.6f * Hash01((uint32_t)icon, 1, 11); c[1] = 0.25f + 0.6f * Hash01((uint32_t)icon, 2, 11);
            c[2] = 0.25f + 0.6f * Hash01((uint32_t)icon, 3, 11);
        } else c[0] = c[1] = c[2] = 0.16f;
        return;
    }
    if (u >= 80.0f && u < 1000.0f && v >= 60.0f && v < 900.0f) {   // a light document window with text
        const float row = std::fmod(v - 60.0f, 18.0f);
        const bool ink = row > 4.0f && row < 14.0f && Hash01((uint32_t)(u / 8.0f), (uint32_t)((v - 60.0f) / 18.0f), 1) < 0.7f &&
                         Hash01((uint32_t)x, (uint32_t)(y / 2), 7) < 0.45f;
        c[0] = c[1] = c[2] = ink ? 0.10f : 0.96f;
        return;
    }
    if (u >= 1040.0f && u < 1840.0f && v >= 120.0f && v < 700.0f) {   // a dark editor with coloured text
        const float row = std::fmod(v - 120.0f, 20.0f);
        const uint32_t word = (uint32_t)(u / 40.0f), line = (uint32_t)((v - 120.0f) / 20.0f);
        const bool ink = row > 5.0f && row < 15.0f && Hash01(word, line, 3) < 0.6f && Hash01((uint32_t)x, (uint32_t)(y / 2), 9) < 0.4f;
        if (ink) { c[0] = 0.45f + 0.5f * Hash01(word, line, 4); c[1] = 0.45f + 0.5f * Hash01(word, line, 5); c[2] = 0.45f + 0.5f * Hash01(word, line, 6); }
        else c[0] = c[1] = c[2] = 0.11f;
        return;
    }
    if (u >= 1100.0f && u < 1800.0f && v >= 720.0f && v < 1000.0f) {  // a photo
        c[0] = 0.5f + 0.45f * std::sin(u * 0.013f + v * 0.004f);
        c[1] = 0.5f + 0.45f * std::sin(u * 0.007f - v * 0.011f + 1.0f);
        c[2] = 0.5f + 0.45f * std::sin(u * 0.003f + v * 0.017f + 2.0f);
        return;
    }
    const float g = 0.05f + 0.25f * (0.5f + 0.5f * std::sin(u * 0.002f + v * 0.003f));   // wallpaper
    c[0] = g * 0.6f; c[1] = g * 0.8f; c[2] = g;
}

// An HDR video frame in linear nits (BT.709 primaries; a wide-gamut corner goes negative).
inline void VideoNits(float u, float v, int x, int y, float n[3]) {
    const float a = 0.5f + 0.5f * std::sin(u * 0.004f + v * 0.003f), b = 0.5f + 0.5f * std::cos(v * 0.005f - u * 0.001f);
    float base = 1.5f + 180.0f * a * a * b;
    base *= 0.96f + 0.08f * Hash01((uint32_t)x, (uint32_t)y, 21);                       // grain
    n[0] = base * (0.9f + 0.3f * std::sin(u * 0.0021f));
    n[1] = base * (0.9f + 0.2f * std::sin(v * 0.0033f + 1.0f));
    n[2] = base * (0.8f + 0.4f * std::cos(u * 0.0017f + v * 0.0013f));
    static const float hl[3][4] = { { 420.0f, 260.0f, 38.0f, 1200.0f }, { 1380.0f, 380.0f, 26.0f, 2500.0f }, { 960.0f, 820.0f, 14.0f, 4000.0f } };
    for (const auto& h : hl) {
        const float d2 = (u - h[0]) * (u - h[0]) + (v - h[1]) * (v - h[1]);
        const float g = std::exp(-d2 / (2.0f * h[2] * h[2]));
        if (g > 1e-4f) { n[0] += h[3] * g; n[1] += h[3] * g * 0.95f; n[2] += h[3] * g * 0.85f; }
    }
    if (u > 1500.0f && v > 800.0f) { n[1] = -0.04f * n[0]; n[2] *= 0.1f; n[0] *= 1.4f; }   // outside BT.709
}

inline void PutPixel(Dom d, unsigned char* p, const float v[3], bool isCode) {
    // isCode: v is an sRGB code triple in [0, 1]; else linear nits (BT.709 primaries)
    float code[3], lin[3];
    for (int c = 0; c < 3; c++) {
        if (isCode) { code[c] = std::clamp(v[c], 0.0f, 1.0f); lin[c] = SrgbEotfF(code[c]); }
        else { lin[c] = v[c]; code[c] = SrgbOetfF(std::clamp(v[c] / 300.0f, 0.0f, 1.0f)); }
    }
    switch (d) {
    case Dom::Sdr8:
        p[0] = (unsigned char)std::lround(code[2] * 255.0f); p[1] = (unsigned char)std::lround(code[1] * 255.0f);
        p[2] = (unsigned char)std::lround(code[0] * 255.0f); p[3] = 255;
        break;
    case Dom::Sdr10: {
        const uint32_t r = (uint32_t)std::lround(code[0] * 1023.0f), g = (uint32_t)std::lround(code[1] * 1023.0f),
                       b = (uint32_t)std::lround(code[2] * 1023.0f);
        const uint32_t w = r | (g << 10) | (b << 20) | (3u << 30);
        std::memcpy(p, &w, 4);
        break;
    }
    case Dom::Acm: {
        uint16_t h[4];
        for (int c = 0; c < 3; c++) h[c] = FloatToHalf(isCode ? lin[c] : std::clamp(v[c] / 300.0f, -0.05f, 1.0f));
        h[3] = FloatToHalf(1.0f);
        std::memcpy(p, h, 8);
        break;
    }
    case Dom::Hdr: {
        uint16_t h[4];
        for (int c = 0; c < 3; c++) h[c] = FloatToHalf(isCode ? lin[c] * 2.5f : v[c] / 80.0f);   // SDR white 200 nits
        h[3] = FloatToHalf(1.0f);
        std::memcpy(p, h, 8);
        break;
    }
    }
}

inline std::vector<unsigned char> MakeFrame(Kind kind, Dom d, int W, int H) {
    const unsigned bpp = DomBpp(d);
    std::vector<unsigned char> out((size_t)W * H * bpp);
    const float s = 1080.0f / (float)H;
    const RECT vw = VideoWindow(W, H);
    for (int y = 0; y < H; y++) {
        unsigned char* row = out.data() + (size_t)y * W * bpp;
        const int band = kind == Kind::Torture ? (y * 8) / H : -1;
        for (int x = 0; x < W; x++) {
            float v[3]; bool isCode = true;
            const float u = (float)x * s, vv = (float)y * s;
            if (kind == Kind::Video || band == 7) { VideoNits(u, vv, x, y, v); isCode = false; }
            else if (kind == Kind::Mix) {
                if (x >= vw.left && x < vw.right && y >= vw.top && y < vw.bottom) {
                    VideoNits((float)(x - vw.left) * 1.5f * s, (float)(y - vw.top) * 1.5f * s, x, y, v); isCode = false;
                } else DeskCode(u, vv, x, y, v);
            } else if (band == 6) DeskCode(u, vv, x, y, v);
            else if (band <= 3) {                                  // grey, R, G, B ramps over the whole range of the domain
                const float t = (float)x / (float)(W - 1);
                const float val = d == Dom::Hdr ? PqEotfNits(t) : t;
                isCode = d != Dom::Hdr;
                for (int c = 0; c < 3; c++) v[c] = (band == 0 || band == c + 1) ? val : 0.0f;
            } else if (band == 4) {                                // random colours inside the domain
                for (int c = 0; c < 3; c++) {
                    const float r = Hash01((uint32_t)x, (uint32_t)y, 31u + (uint32_t)c);
                    v[c] = d == Dom::Hdr ? 0.01f * std::pow(100000.0f, r) : r;
                }
                isCode = d != Dom::Hdr;
            } else {                                               // band 5: beyond the domain where it has a beyond
                uint16_t h[4];
                if (d == Dom::Hdr || d == Dom::Acm) {
                    for (int c = 0; c < 3; c++) {
                        const float r = Hash01((uint32_t)x, (uint32_t)y, 41u + (uint32_t)c);
                        h[c] = FloatToHalf(d == Dom::Hdr ? -2.0f + 62.0f * r * r : -0.2f + 1.5f * r);
                    }
                    h[3] = FloatToHalf(1.0f);
                    std::memcpy(row + (size_t)x * bpp, h, 8);
                    continue;
                }
                for (int c = 0; c < 3; c++) v[c] = Hash01((uint32_t)x, (uint32_t)y, 51u + (uint32_t)c);
            }
            PutPixel(d, row + (size_t)x * bpp, v, isCode);
        }
    }
    return out;
}

// ---------------------------------------------------------------------------------------------------------------
// Device, timestamps
// ---------------------------------------------------------------------------------------------------------------
struct Dev {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11Query* dj = nullptr; ID3D11Query* q0 = nullptr; ID3D11Query* q1 = nullptr;
    std::string name;
    bool warp = false, ok = false;

    // want: "WARP" = the software rasteriser; else a substring of a hardware adapter's name ("" = the first one).
    explicit Dev(const std::string& want) {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (want == "WARP" || want == "warp") {
            warp = true; name = "WARP";
            if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        } else {
            IDXGIFactory1* fac = nullptr;
            if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&fac))) return;
            IDXGIAdapter1* pick = nullptr;
            for (UINT i = 0;; i++) {
                IDXGIAdapter1* a = nullptr;
                if (fac->EnumAdapters1(i, &a) == DXGI_ERROR_NOT_FOUND) break;
                DXGI_ADAPTER_DESC1 d; a->GetDesc1(&d);
                char nm[256] = {};
                WideCharToMultiByte(CP_UTF8, 0, d.Description, -1, nm, sizeof(nm), nullptr, nullptr);
                if (!pick && !(d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) && Contains(nm, want)) { pick = a; name = nm; }
                else a->Release();
            }
            fac->Release();
            if (!pick) return;
            const HRESULT hr = D3D11CreateDevice(pick, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic);
            pick->Release();
            if (FAILED(hr)) return;
        }
        D3D11_QUERY_DESC qd = { D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
        dev->CreateQuery(&qd, &dj);
        qd.Query = D3D11_QUERY_TIMESTAMP;
        dev->CreateQuery(&qd, &q0);
        dev->CreateQuery(&qd, &q1);
        ok = true;
    }
    ~Dev() {
        for (IUnknown* p : { (IUnknown*)dj, (IUnknown*)q0, (IUnknown*)q1 }) if (p) p->Release();
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
    Dev(const Dev&) = delete;
    Dev& operator=(const Dev&) = delete;

    // One timed submission: gpuUs = the GPU's own clock between the first and the last command (-1 when the adapter has
    // no usable timestamps or the interval was disjoint), cpuUs = the time the calls themselves took on this thread,
    // wallUs = from the first call until the GPU reported the last one done.
    template <typename Fn> void Time(Fn&& fn, double& gpuUs, double& cpuUs, double& wallUs) {
        gpuUs = -1.0;
        const bool stamps = dj && q0 && q1;
        if (stamps) { ic->Begin(dj); ic->End(q0); }
        const double t0 = QpcUs();
        fn();
        const double t1 = QpcUs();
        cpuUs = t1 - t0;
        if (stamps) { ic->End(q1); ic->End(dj); }
        ic->Flush();
        if (stamps) {
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT d = {};
            const double give = QpcUs() + 10e6;
            while (ic->GetData(dj, &d, sizeof(d), 0) != S_OK) { if (QpcUs() > give) { wallUs = -1.0; return; } SwitchToThread(); }
            wallUs = QpcUs() - t0;
            UINT64 a = 0, b = 0;
            while (ic->GetData(q0, &a, 8, 0) != S_OK) SwitchToThread();
            while (ic->GetData(q1, &b, 8, 0) != S_OK) SwitchToThread();
            if (!d.Disjoint && d.Frequency && b >= a) gpuUs = (double)(b - a) * 1e6 / (double)d.Frequency;
        } else {
            D3D11_QUERY_DESC qd = { D3D11_QUERY_EVENT, 0 };
            ID3D11Query* ev = nullptr;
            dev->CreateQuery(&qd, &ev);
            if (ev) {
                ic->End(ev); ic->Flush();
                BOOL done = FALSE;
                while (ic->GetData(ev, &done, sizeof(done), 0) != S_OK || !done) SwitchToThread();
                ev->Release();
            }
            wallUs = QpcUs() - t0;
        }
    }
};

// The GPU's clock (tests/test_fald_fade_hw.cpp HwHeater has the measurement behind this): an idle high-end GPU sits at
// a few hundred MHz and takes several frames of load to clock up, so the same pass times 5-10 x apart depending on what
// ran in the milliseconds before it. Heat(ms) queues ~ms of integer ALU work in ~2 ms batches (each its own flush, so the
// desktop compositor is never behind more than one batch) right before a timed run.
struct Heater {
    ID3D11ComputeShader* cs = nullptr; ID3D11Buffer* buf = nullptr; ID3D11UnorderedAccessView* uav = nullptr; ID3D11Buffer* cb = nullptr;
    unsigned iters = 256;
    bool ok = false;
    explicit Heater(Dev& d) {
        static const char* src =
            "cbuffer C : register(b0) { uint iters; uint3 pad; };\n"
            "RWStructuredBuffer<uint> o : register(u0);\n"
            "[numthreads(256, 1, 1)] void main(uint3 id : SV_DispatchThreadID) {\n"
            "    uint x = id.x;\n"
            "    [loop] for (uint i = 0; i < iters; i++) { x = x * 1664525u + 1013904223u; }\n"
            "    if (x == 0x12345678u) o[0] = x;\n"
            "}\n";
        ID3DBlob* b = nullptr;
        if (FAILED(D3DCompile(src, std::strlen(src), "heater", nullptr, nullptr, "main", "cs_5_0", 0, 0, &b, nullptr))) return;
        d.dev->CreateComputeShader(b->GetBufferPointer(), b->GetBufferSize(), nullptr, &cs);
        b->Release();
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 16; bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; bd.StructureByteStride = 4;
        d.dev->CreateBuffer(&bd, nullptr, &buf);
        if (buf) d.dev->CreateUnorderedAccessView(buf, nullptr, &uav);
        D3D11_BUFFER_DESC cd = {};
        cd.ByteWidth = 16; cd.Usage = D3D11_USAGE_DEFAULT; cd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        d.dev->CreateBuffer(&cd, nullptr, &cb);
        ok = cs && uav && cb;
        for (int k = 0; ok && k < 6; k++) {   // calibrate to ~2 ms a batch at the clock this reaches
            double g = -1, c = 0, w = 0;
            d.Time([&] { Dispatch(d); }, g, c, w);
            if (g <= 0.0) break;
            iters = (unsigned)std::min<double>((double)(1u << 22), std::max(16.0, (double)iters * 2000.0 / g));
        }
    }
    ~Heater() { for (IUnknown* p : { (IUnknown*)cs, (IUnknown*)uav, (IUnknown*)buf, (IUnknown*)cb }) if (p) p->Release(); }
    void Dispatch(Dev& d) {
        const unsigned c[4] = { iters, 0, 0, 0 };
        d.ic->UpdateSubresource(cb, 0, nullptr, c, 0, 0);
        d.ic->CSSetShader(cs, nullptr, 0);
        d.ic->CSSetConstantBuffers(0, 1, &cb);
        d.ic->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);
        d.ic->Dispatch(4096, 1, 1);
        ID3D11UnorderedAccessView* nu = nullptr; ID3D11Buffer* nb = nullptr;
        d.ic->CSSetUnorderedAccessViews(0, 1, &nu, nullptr);
        d.ic->CSSetConstantBuffers(0, 1, &nb);
        d.ic->CSSetShader(nullptr, nullptr, 0);
    }
    void Heat(Dev& d, double ms) { for (double done = 0.0; ok && done < ms; done += 2.0) { Dispatch(d); d.ic->Flush(); } }
};

inline ID3D11Texture2D* MakeTex(ID3D11Device* dev, int w, int h, DXGI_FORMAT fmt, UINT bind, const void* data = nullptr, UINT pitch = 0) {
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = (UINT)w; d.Height = (UINT)h; d.MipLevels = 1; d.ArraySize = 1; d.Format = fmt; d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT; d.BindFlags = bind;
    D3D11_SUBRESOURCE_DATA init = { data, pitch, 0 };
    ID3D11Texture2D* t = nullptr;
    dev->CreateTexture2D(&d, data ? &init : nullptr, &t);
    return t;
}

inline std::vector<unsigned char> ReadBack(ID3D11Device* dev, ID3D11DeviceContext* ic, ID3D11Texture2D* t) {
    std::vector<unsigned char> out;
    D3D11_TEXTURE2D_DESC d; t->GetDesc(&d);
    const unsigned bpp = d.Format == DXGI_FORMAT_R16G16B16A16_FLOAT ? 8u : d.Format == DXGI_FORMAT_R32G32B32A32_FLOAT ? 16u : 4u;
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

// How far two read-backs of the same format are apart, in the format's own units: FP16 -> absolute difference of the
// decoded floats (and the largest relative one above 1e-3); UNORM -> codes.
struct Diff { size_t differing = 0, total = 0; double maxAbs = 0.0, maxRel = 0.0; bool sizeMismatch = false; };
inline Diff Compare(const std::vector<unsigned char>& a, const std::vector<unsigned char>& b, DXGI_FORMAT fmt) {
    Diff d;
    if (a.size() != b.size()) { d.sizeMismatch = true; return d; }
    if (fmt == DXGI_FORMAT_R16G16B16A16_FLOAT) {
        d.total = a.size() / 2;
        for (size_t i = 0; i + 1 < a.size(); i += 2) {
            uint16_t x, y; std::memcpy(&x, &a[i], 2); std::memcpy(&y, &b[i], 2);
            if (x == y) continue;
            d.differing++;
            const double fx = HalfToFloat(x), fy = HalfToFloat(y), ad = std::fabs(fx - fy);
            if (ad > d.maxAbs) d.maxAbs = ad;
            const double mag = std::max(std::fabs(fx), std::fabs(fy));
            if (mag > 1e-3 && ad / mag > d.maxRel) d.maxRel = ad / mag;
        }
    } else if (fmt == DXGI_FORMAT_R10G10B10A2_UNORM) {
        d.total = a.size() / 4 * 3;
        for (size_t i = 0; i + 3 < a.size(); i += 4) {
            uint32_t x, y; std::memcpy(&x, &a[i], 4); std::memcpy(&y, &b[i], 4);
            for (int c = 0; c < 3; c++) {
                const int cx = (int)((x >> (10 * c)) & 0x3FFu), cy = (int)((y >> (10 * c)) & 0x3FFu);
                if (cx != cy) { d.differing++; d.maxAbs = std::max(d.maxAbs, (double)std::abs(cx - cy)); }
            }
        }
    } else {
        d.total = a.size();
        for (size_t i = 0; i < a.size(); i++)
            if (a[i] != b[i]) { d.differing++; d.maxAbs = std::max(d.maxAbs, (double)std::abs((int)a[i] - (int)b[i])); }
    }
    return d;
}

inline bool SaveBytes(const std::string& path, const void* data, size_t n) {
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write((const char*)data, (std::streamsize)n);
    return (bool)f;
}
inline bool LoadBytes(const std::string& path, std::vector<unsigned char>& out) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    out.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    return true;
}

// ---------------------------------------------------------------------------------------------------------------
// HLSL text tools (the variants of a shipped shader are made from its own text, so they follow its edits)
// ---------------------------------------------------------------------------------------------------------------
inline bool ReplaceOnce(std::string& s, const std::string& from, const std::string& to, std::string* err) {
    const size_t at = s.find(from);
    if (at == std::string::npos) { if (err) *err += "text not found: [" + from + "] "; return false; }
    if (s.find(from, at + 1) != std::string::npos) { if (err) *err += "text not unique: [" + from + "] "; return false; }
    s.replace(at, from.size(), to);
    return true;
}
// A switch the shader reads from a constant buffer becomes a compile-time constant: the field keeps its place in the
// buffer under another name (the layout, and so the bindings, stay as they are) and `type name = value` is declared
// right after the buffer's closing brace `anchor`.
inline bool FoldField(std::string& s, const std::string& decl, const std::string& type, const std::string& name,
                      const std::string& value, std::string* err) {
    // decl = the field's declaration as written in the shader, e.g. "int colorMode;"
    std::string renamed = decl;
    const size_t at = renamed.find(name);
    if (at == std::string::npos) { if (err) *err += "bad fold decl: " + decl + " "; return false; }
    renamed.replace(at, name.size(), "cbx_" + name);
    if (!ReplaceOnce(s, decl, renamed, err)) return false;
    // the constant: after the next "};" following the renamed field
    const size_t f = s.find(renamed);
    const size_t close = s.find("};", f);
    if (close == std::string::npos) { if (err) *err += "no buffer end after " + decl + " "; return false; }
    s.insert(close + 2, "\nstatic const " + type + " " + name + " = " + value + ";");
    return true;
}
// Every implicit-gradient `.Sample(` becomes `.SampleLevel(..., 0)` (the same texel for a one-mip texture, and legal
// inside flow control).
inline int SampleToSampleLevel(std::string& s) {
    int n = 0;
    size_t at = 0;
    while ((at = s.find(".Sample(", at)) != std::string::npos) {
        size_t p = at + 8; int depth = 1;
        while (p < s.size() && depth > 0) { if (s[p] == '(') depth++; else if (s[p] == ')') depth--; p++; }
        if (depth != 0) break;
        s.insert(p - 1, ", 0");
        s.replace(at, 8, ".SampleLevel(");
        at += 13; n++;
    }
    return n;
}

struct ShaderCost {
    bool ok = false; std::string error;
    unsigned bytes = 0, instr = 0, temps = 0, texNormal = 0, texLoad = 0, texBias = 0, texGrad = 0, texComp = 0;
    unsigned fltInstr = 0, intInstr = 0, uintInstr = 0, staticFlow = 0, dynFlow = 0, movc = 0, cbBytes = 0, resources = 0;
    // from the disassembly
    unsigned aSample = 0, aSampleL = 0, aLd = 0, aLog = 0, aExp = 0, aSqrt = 0, aRsq = 0, aDiv = 0, aIf = 0, aLoop = 0, aMovc = 0, aSinCos = 0;
    std::string asmText;
};
inline ShaderCost CostOfBlob(ID3DBlob* blob) {
    ShaderCost c;
    c.bytes = (unsigned)blob->GetBufferSize();
    ID3D11ShaderReflection* r = nullptr;
    if (FAILED(D3DReflect(blob->GetBufferPointer(), blob->GetBufferSize(), __uuidof(ID3D11ShaderReflection), (void**)&r))) { c.error = "reflect"; return c; }
    D3D11_SHADER_DESC d = {};
    r->GetDesc(&d);
    c.instr = d.InstructionCount; c.temps = d.TempRegisterCount; c.texNormal = d.TextureNormalInstructions;
    c.texLoad = d.TextureLoadInstructions; c.texBias = d.TextureBiasInstructions; c.texGrad = d.TextureGradientInstructions;
    c.texComp = d.TextureCompInstructions; c.fltInstr = d.FloatInstructionCount; c.intInstr = d.IntInstructionCount;
    c.uintInstr = d.UintInstructionCount; c.staticFlow = d.StaticFlowControlCount; c.dynFlow = d.DynamicFlowControlCount;
    c.movc = r->GetMovcInstructionCount(); c.resources = d.BoundResources;
    for (UINT i = 0; i < d.ConstantBuffers; i++) {
        D3D11_SHADER_BUFFER_DESC bd = {};
        if (SUCCEEDED(r->GetConstantBufferByIndex(i)->GetDesc(&bd))) c.cbBytes += bd.Size;
    }
    r->Release();
    ID3DBlob* dis = nullptr;
    if (SUCCEEDED(D3DDisassemble(blob->GetBufferPointer(), blob->GetBufferSize(), 0, nullptr, &dis)) && dis) {
        c.asmText.assign((const char*)dis->GetBufferPointer(), dis->GetBufferSize());
        dis->Release();
        std::istringstream in(c.asmText);
        std::string line;
        while (std::getline(in, line)) {
            const size_t b = line.find_first_not_of(" \t");
            if (b == std::string::npos || line[b] == '/') continue;
            const size_t e = line.find_first_of(" \t(", b);
            const std::string op = line.substr(b, e == std::string::npos ? std::string::npos : e - b);
            if (op == "sample" || op == "sample_indexable" || op == "sample_b" || op == "sample_b_indexable" ||
                op == "sample_d" || op == "sample_d_indexable") c.aSample++;
            else if (op == "sample_l" || op == "sample_l_indexable") c.aSampleL++;
            else if (op == "ld" || op == "ld_indexable" || op == "ld_uav_typed" || op == "ld_raw" || op == "ld_structured" ||
                     op == "ld_raw_indexable" || op == "ld_structured_indexable" || op == "ld_uav_typed_indexable") c.aLd++;
            else if (op == "log") c.aLog++;
            else if (op == "exp") c.aExp++;
            else if (op == "sqrt") c.aSqrt++;
            else if (op == "rsq") c.aRsq++;
            else if (op == "div" || op == "udiv") c.aDiv++;
            else if (op == "if_nz" || op == "if_z") c.aIf++;
            else if (op == "loop") c.aLoop++;
            else if (op == "movc" || op == "swapc") c.aMovc++;
            else if (op == "sincos") c.aSinCos++;
        }
    }
    c.ok = true;
    return c;
}
inline ShaderCost CostOf(const char* src, size_t len, const char* entry, const char* target, UINT flags = 0, ID3DBlob** keep = nullptr) {
    ID3DBlob* blob = nullptr; ID3DBlob* msgs = nullptr;
    const HRESULT hr = D3DCompile(src, len, entry, nullptr, nullptr, entry, target, flags, 0, &blob, &msgs);
    ShaderCost c;
    if (FAILED(hr) || !blob) {
        c.error = msgs ? std::string((const char*)msgs->GetBufferPointer(), msgs->GetBufferSize()) : "compile failed";
        if (msgs) msgs->Release();
        return c;
    }
    if (msgs) msgs->Release();
    c = CostOfBlob(blob);
    if (keep) *keep = blob; else blob->Release();
    return c;
}
inline const char* CostHeader() {
    return "shader\ttarget\tbytes\tinstr\ttemps\ttex_sample\ttex_sample_l\ttex_load\tlog\texp\tsqrt\trsq\tdiv\tif\tloop\tmovc\tflt_instr\tint_instr\tuint_instr\tdyn_flow\tstatic_flow\tcb_bytes\tresources";
}
inline std::string CostRow(const std::string& name, const char* target, const ShaderCost& c) {
    std::ostringstream o;
    o << name << "\t" << target << "\t" << c.bytes << "\t" << c.instr << "\t" << c.temps << "\t" << c.aSample << "\t" << c.aSampleL << "\t" << c.aLd
      << "\t" << c.aLog << "\t" << c.aExp << "\t" << c.aSqrt << "\t" << c.aRsq << "\t" << c.aDiv << "\t" << c.aIf << "\t" << c.aLoop << "\t" << c.aMovc
      << "\t" << c.fltInstr << "\t" << c.intInstr << "\t" << c.uintInstr << "\t" << c.dynFlow << "\t" << c.staticFlow << "\t" << c.cbBytes << "\t" << c.resources;
    return o.str();
}

}  // namespace perf
