// Dynamic-tonemap peak detection (shared/peak_detect.h) — the one copy the overlay and the DWM hook
// both run. Regression tests for the stride-4 dense reduction that replaced the 80x45 point lattice
// (HW defect 2026-09-11: highlights between lattice pixels were passed through uncompressed).
//
// Two layers:
//   1. CPU reference — group-count arithmetic, the old lattice (documents the miss), the stride-4
//      coverage guarantee, and the temporal smoothing exactly as the HLSL does it.
//   2. The REAL HLSL, compiled from shared/peak_detect.h and run on a WARP (software) D3D11 device
//      through the production DispatchPeakDetection, compared against the CPU reference. Skips
//      (with a message) only if no D3D11 device can be created.

#include "doctest.h"
#include "../shared/peak_detect.h"
#include "color.h"

#include <d3d11.h>
#include <d3dcompiler.h>
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace {

// ---------------------------------------------------------------------------------------------
// Synthetic frames: black background + bright rectangles (linear scRGB, 1.0 = 80 nits)
// ---------------------------------------------------------------------------------------------
struct BrightRect { unsigned x, y, w, h; float r, g, b; };
struct SyntheticFrame {
    unsigned width, height;
    std::vector<BrightRect> rects;
};

float LuminanceNits(float r, float g, float b) {
    return (0.2126f * r + 0.7152f * g + 0.0722f * b) * 80.0f;
}

bool RectCovers(const BrightRect& r, unsigned x, unsigned y) {
    return x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
}

// Max over the pixels a sampler visits; later rects overwrite earlier ones (as the upload does).
template <typename Visit>
float SampledPeakNits(const SyntheticFrame& f, Visit visitSamples) {
    float peak = 0.0f;
    visitSamples([&](unsigned x, unsigned y) {
        if (x >= f.width || y >= f.height) return;
        float nits = 0.0f;
        for (const auto& r : f.rects)
            if (RectCovers(r, x, y)) nits = (std::max)(LuminanceNits(r.r, r.g, r.b), 0.0f);
        peak = (std::max)(peak, nits);
    });
    return peak;
}

// Reference of the production reduction: every PEAK_STRIDE-th pixel in both axes.
float ReferenceFramePeakNits(const SyntheticFrame& f) {
    return SampledPeakNits(f, [&](auto sample) {
        for (unsigned y = 0; y < f.height; y += PEAK_STRIDE)
            for (unsigned x = 0; x < f.width; x += PEAK_STRIDE) sample(x, y);
    });
}

// The ORIGINAL sparse sampler (80x45 lattice, px = gx*W/80, py = gy*H/45), kept only to document
// what the old kernel would have seen for the same frame.
float OldLatticePeakNits(const SyntheticFrame& f) {
    return SampledPeakNits(f, [&](auto sample) {
        for (unsigned gy = 0; gy < 45; gy++)
            for (unsigned gx = 0; gx < 80; gx++) sample((gx * f.width) / 80, (gy * f.height) / 45);
    });
}

// Temporal smoothing exactly as the smoothing pass does it (nits domain).
float ReferenceSmoothedPeak(float prevPeakNits, bool hasHistory, float framePeakNits, const PeakParams& sp) {
    if (!hasHistory) prevPeakNits = framePeakNits;
    float target, maxDelta;
    if (framePeakNits > prevPeakNits) {
        target = prevPeakNits + (framePeakNits - prevPeakNits) * sp.riseRate;
        maxDelta = sp.maxRisePerFrame;
    } else {
        target = prevPeakNits + (framePeakNits - prevPeakNits) * sp.fallRate;
        maxDelta = sp.maxFallPerFrame;
    }
    float smoothed = (std::min)((std::max)(target, prevPeakNits - maxDelta), prevPeakNits + maxDelta);
    return (std::min)((std::max)(smoothed, 0.0f), 10000.0f);
}

PeakParams NoSmoothing() {
    PeakParams p{};
    p.riseRate = 1.0f; p.fallRate = 1.0f; p.maxRisePerFrame = 10000.0f; p.maxFallPerFrame = 10000.0f;
    return p;
}

// ---------------------------------------------------------------------------------------------
// FP16 packing for the R16G16B16A16_FLOAT frame texture (normal range only; enough for tests)
// ---------------------------------------------------------------------------------------------
uint16_t HalfFromFloat(float f) {
    uint32_t bits;
    std::memcpy(&bits, &f, sizeof(bits));
    uint32_t sign = (bits >> 16) & 0x8000u;
    int32_t exponent = (int32_t)((bits >> 23) & 0xFFu) - 127 + 15;
    uint32_t mantissa = bits & 0x7FFFFFu;
    if (f == 0.0f) return (uint16_t)sign;
    if (exponent >= 31) return (uint16_t)(sign | 0x7C00u);  // overflow -> inf
    if (exponent <= 0) return (uint16_t)sign;                // flush tiny values (unused here)
    return (uint16_t)(sign | ((uint32_t)exponent << 10) | (mantissa >> 13));
}

// ---------------------------------------------------------------------------------------------
// Minimal D3D11 harness on WARP running the production shaders + DispatchPeakDetection
// ---------------------------------------------------------------------------------------------
struct PeakGpuHarness {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* dc = nullptr;
    ID3D11ComputeShader* reduceCS = nullptr;
    ID3D11ComputeShader* smoothCS = nullptr;
    ID3D11Buffer* cb = nullptr;
    ID3D11Texture2D* staging = nullptr;
    std::string error;
    bool deviceUnavailable = false;

    // One detector state = what one monitor owns (smoothed peak + raw max)
    struct Detector {
        ID3D11Texture2D* peakTex = nullptr;
        ID3D11UnorderedAccessView* peakUAV = nullptr;
        ID3D11Texture2D* rawTex = nullptr;
        ID3D11UnorderedAccessView* rawUAV = nullptr;
    };
    std::vector<Detector> detectors;

    ~PeakGpuHarness() {
        auto rel = [](IUnknown* p) { if (p) p->Release(); };
        for (auto& d : detectors) { rel(d.rawUAV); rel(d.rawTex); rel(d.peakUAV); rel(d.peakTex); }
        rel(staging); rel(cb); rel(smoothCS); rel(reduceCS); rel(dc); rel(device);
    }

    bool CompileCS(const char* src, const char* name, ID3D11ComputeShader** out) {
        ID3DBlob* blob = nullptr;
        ID3DBlob* err = nullptr;
        HRESULT hr = D3DCompile(src, strlen(src), name, nullptr, nullptr, "main", "cs_5_0", 0, 0, &blob, &err);
        if (FAILED(hr)) {
            error = std::string(name) + " compile failed: " + (err ? (const char*)err->GetBufferPointer() : "?");
            if (err) err->Release();
            return false;
        }
        if (err) err->Release();
        hr = device->CreateComputeShader(blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, out);
        blob->Release();
        if (FAILED(hr)) { error = std::string(name) + " CreateComputeShader failed"; return false; }
        return true;
    }

    // Returns false with `error` set; `deviceUnavailable` means the test should skip, not fail.
    bool Init(int numDetectors = 1) {
        D3D_FEATURE_LEVEL fl;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
                                       D3D11_SDK_VERSION, &device, &fl, &dc);
        if (FAILED(hr)) {
            hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
                                   D3D11_SDK_VERSION, &device, &fl, &dc);
        }
        if (FAILED(hr)) { deviceUnavailable = true; error = "no D3D11 device (WARP or hardware)"; return false; }
        if (fl < D3D_FEATURE_LEVEL_11_0) { deviceUnavailable = true; error = "feature level < 11_0"; return false; }

        if (!CompileCS(g_peakReduceCSSource, "PeakReduceCS", &reduceCS)) return false;
        if (!CompileCS(g_peakSmoothCSSource, "PeakSmoothCS", &smoothCS)) return false;

        // Same buffer the overlay/hook create: 32 bytes, dynamic, rewritten by every dispatch
        D3D11_BUFFER_DESC cbd = {};
        cbd.ByteWidth = sizeof(PeakParams);
        cbd.Usage = D3D11_USAGE_DYNAMIC;
        cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        if (FAILED(device->CreateBuffer(&cbd, nullptr, &cb))) { error = "CB create failed"; return false; }

        for (int i = 0; i < numDetectors; i++) {
            Detector d;
            D3D11_TEXTURE2D_DESC pd = {};
            pd.Width = 1; pd.Height = 1; pd.MipLevels = 1; pd.ArraySize = 1;
            pd.Format = DXGI_FORMAT_R32_FLOAT; pd.SampleDesc.Count = 1;
            pd.Usage = D3D11_USAGE_DEFAULT; pd.BindFlags = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
            float zero = 0.0f;
            D3D11_SUBRESOURCE_DATA init = { &zero, sizeof(float), 0 };
            if (FAILED(device->CreateTexture2D(&pd, &init, &d.peakTex)) ||
                FAILED(device->CreateUnorderedAccessView(d.peakTex, nullptr, &d.peakUAV))) {
                detectors.push_back(d);
                error = "peak texture create failed"; return false;
            }
            if (FAILED(CreatePeakRawTexture(device, dc, &d.rawTex, &d.rawUAV))) {
                detectors.push_back(d);
                error = "raw-max texture create failed"; return false;
            }
            detectors.push_back(d);
        }

        D3D11_TEXTURE2D_DESC sd = {};
        sd.Width = 1; sd.Height = 1; sd.MipLevels = 1; sd.ArraySize = 1;
        sd.Format = DXGI_FORMAT_R32_FLOAT; sd.SampleDesc.Count = 1;
        sd.Usage = D3D11_USAGE_STAGING; sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(device->CreateTexture2D(&sd, nullptr, &staging))) { error = "staging create failed"; return false; }
        return true;
    }

    ID3D11ShaderResourceView* UploadFrame(const SyntheticFrame& f, ID3D11Texture2D** texOut) {
        std::vector<uint16_t> data((size_t)f.width * f.height * 4, 0);
        for (const auto& r : f.rects) {
            for (unsigned y = r.y; y < r.y + r.h && y < f.height; y++) {
                for (unsigned x = r.x; x < r.x + r.w && x < f.width; x++) {
                    size_t i = ((size_t)y * f.width + x) * 4;
                    data[i + 0] = HalfFromFloat(r.r);
                    data[i + 1] = HalfFromFloat(r.g);
                    data[i + 2] = HalfFromFloat(r.b);
                    data[i + 3] = HalfFromFloat(1.0f);
                }
            }
        }
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = f.width; td.Height = f.height; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA init = {};
        init.pSysMem = data.data();
        init.SysMemPitch = f.width * 4 * sizeof(uint16_t);
        *texOut = nullptr;
        if (FAILED(device->CreateTexture2D(&td, &init, texOut))) return nullptr;
        ID3D11ShaderResourceView* srv = nullptr;
        if (FAILED(device->CreateShaderResourceView(*texOut, nullptr, &srv))) { (*texOut)->Release(); *texOut = nullptr; return nullptr; }
        return srv;
    }

    // Run the production two-pass dispatch on a frame and return detector `which`'s smoothed peak (nits).
    float RunFrame(const SyntheticFrame& f, const PeakParams& params = PeakParams{}, int which = 0) {
        ID3D11Texture2D* tex = nullptr;
        ID3D11ShaderResourceView* srv = UploadFrame(f, &tex);
        REQUIRE(srv != nullptr);
        Detector& d = detectors[which];
        DispatchPeakDetection(dc, reduceCS, smoothCS, cb, srv, d.peakUAV, d.rawUAV, f.width, f.height, &params);
        dc->CopyResource(staging, d.peakTex);
        D3D11_MAPPED_SUBRESOURCE m = {};
        REQUIRE(SUCCEEDED(dc->Map(staging, 0, D3D11_MAP_READ, 0, &m)));  // blocks until the GPU is done
        float pq = *(const float*)m.pData;
        dc->Unmap(staging, 0);
        srv->Release();
        tex->Release();
        return PQToLinearScalar(pq) * 10000.0f;
    }
};

#define PEAK_GPU_INIT_OR_SKIP(gpu, ...)                                          \
    if (!(gpu).Init(__VA_ARGS__)) {                                              \
        if ((gpu).deviceUnavailable) {                                           \
            MESSAGE("skipping GPU peak-detect test: " << (gpu).error);          \
            return;                                                              \
        }                                                                        \
        FAIL("peak-detect GPU harness init failed: " << (gpu).error);           \
    }

} // namespace

// =============================================================================================
// CPU reference layer
// =============================================================================================

TEST_CASE("PeakDetect: C++ constants match the HLSL kernel") {
    CHECK(PEAK_TILE_PIXELS == 64);
    CHECK(std::string(g_peakReduceCSSource).find("#define PEAK_STRIDE " + std::to_string(PEAK_STRIDE)) != std::string::npos);
    CHECK(std::string(g_peakReduceCSSource).find("[numthreads(16, 16, 1)]") != std::string::npos);
    CHECK(PEAK_GROUP_SIZE == 16);
}

TEST_CASE("PeakDetect: group count covers every sampled pixel") {
    CHECK(PeakGroupCount(3840) == 60);
    CHECK(PeakGroupCount(2160) == 34);   // 33.75 -> partial bottom row of groups
    CHECK(PeakGroupCount(1920) == 30);
    CHECK(PeakGroupCount(1080) == 17);
    CHECK(PeakGroupCount(64) == 1);
    CHECK(PeakGroupCount(65) == 2);
    CHECK(PeakGroupCount(1) == 1);
    CHECK(PeakGroupCount(0) == 0);
    for (unsigned n : { 1u, 63u, 64u, 65u, 1080u, 1440u, 2160u, 2560u, 3440u, 3840u, 5120u, 7680u }) {
        INFO("n = " << n);
        CHECK(PeakGroupCount(n) * PEAK_TILE_PIXELS >= n);
        CHECK((PeakGroupCount(n) - 1) * PEAK_TILE_PIXELS < n);
    }
}

TEST_CASE("PeakDetect: the old lattice missed off-grid highlights; stride 4 sees any 4x4 highlight at every phase") {
    // 4K: old lattice pixels were (48k, 48m). A 4x4 highlight at (25, 25) lies between them.
    SyntheticFrame f{ 3840, 2160, { { 25, 25, 4, 4, 20.0f, 20.0f, 20.0f } } };  // 20.0 scRGB = 1600 nits
    CHECK(OldLatticePeakNits(f) == doctest::Approx(0.0f));
    CHECK(ReferenceFramePeakNits(f) == doctest::Approx(1600.0f).epsilon(1e-4));

    // Coverage guarantee: a 4x4 highlight is seen whatever its phase against the stride
    for (unsigned oy = 0; oy < PEAK_STRIDE; oy++) {
        for (unsigned ox = 0; ox < PEAK_STRIDE; ox++) {
            SyntheticFrame g{ 1920, 1080, { { 1000 + ox, 500 + oy, 4, 4, 12.5f, 12.5f, 12.5f } } };
            INFO("phase (" << ox << "," << oy << ")");
            CHECK(ReferenceFramePeakNits(g) == doctest::Approx(1000.0f).epsilon(1e-4));
        }
    }

    // Deliberate: a lone off-stride pixel does not drive the frame's curve
    SyntheticFrame lone{ 1920, 1080, { { 1001, 501, 1, 1, 12.5f, 12.5f, 12.5f } } };
    CHECK(ReferenceFramePeakNits(lone) == doctest::Approx(0.0f));
}

TEST_CASE("PeakDetect: temporal smoothing reference (rise/fall rates + slew limits)") {
    PeakParams sp{};
    CHECK(ReferenceSmoothedPeak(0.0f, false, 1600.0f, sp) == doctest::Approx(1600.0f));   // no history
    CHECK(ReferenceSmoothedPeak(1600.0f, true, 0.0f, sp) == doctest::Approx(1550.0f));    // 50 nits/frame fall slew
    CHECK(ReferenceSmoothedPeak(1000.0f, true, 990.0f, sp) == doctest::Approx(999.5f));   // 5% fall, no clamp
    CHECK(ReferenceSmoothedPeak(1500.0f, true, 1600.0f, sp) == doctest::Approx(1530.0f)); // 30% rise
    CHECK(ReferenceSmoothedPeak(1000.0f, true, 2000.0f, sp) == doctest::Approx(1100.0f)); // 100 nits/frame rise slew
}

// =============================================================================================
// Real HLSL on a D3D11 device (WARP) through the production DispatchPeakDetection
// =============================================================================================

TEST_CASE("PeakDetect: GPU reduction + smoothing match the CPU reference at 4K") {
    PeakGpuHarness gpu;
    PEAK_GPU_INIT_OR_SKIP(gpu);
    PeakParams sp{};

    // Frame A: 4x4 1600-nit highlight between the old lattice points (read 0 before)
    SyntheticFrame frameA{ 3840, 2160, { { 25, 25, 4, 4, 20.0f, 20.0f, 20.0f } } };
    float a = gpu.RunFrame(frameA);
    CHECK(a == doctest::Approx(ReferenceFramePeakNits(frameA)).epsilon(0.002));
    CHECK(a > 1500.0f);

    // Frame B: 400-nit highlight in the partial bottom-right group (60x34 groups, last row 16 px
    // tall) plus a dimmer one; the detector falls from A under the 50 nits/frame slew limit.
    SyntheticFrame frameB{ 3840, 2160, { { 3836, 2156, 4, 4, 5.0f, 5.0f, 5.0f }, { 1000, 1000, 4, 4, 1.0f, 1.0f, 1.0f } } };
    CHECK(ReferenceFramePeakNits(frameB) == doctest::Approx(400.0f).epsilon(1e-4));
    float prev = a;
    float b = gpu.RunFrame(frameB);
    CHECK(b == doctest::Approx(ReferenceSmoothedPeak(prev, true, 400.0f, sp)).epsilon(0.002));  // ~1550
    prev = b;
    float b2 = gpu.RunFrame(frameB);
    CHECK(b2 == doctest::Approx(ReferenceSmoothedPeak(prev, true, 400.0f, sp)).epsilon(0.002)); // ~1500
    prev = b2;

    // Frame C: pure green — same Rec.709 luminance weighting (10.0 scRGB -> 572.16 nits)
    SyntheticFrame frameC{ 3840, 2160, { { 1234, 777, 4, 4, 0.0f, 10.0f, 0.0f } } };
    float cRef = ReferenceFramePeakNits(frameC);
    CHECK(cRef == doctest::Approx(572.16f).epsilon(1e-4));
    float c = gpu.RunFrame(frameC);
    CHECK(c == doctest::Approx(ReferenceSmoothedPeak(prev, true, cRef, sp)).epsilon(0.002));
}

TEST_CASE("PeakDetect: GPU finds a 4x4 highlight at every group edge and phase, ignores off-stride single pixels") {
    PeakGpuHarness gpu;
    PEAK_GPU_INIT_OR_SKIP(gpu);
    const PeakParams raw = NoSmoothing();  // the smoothed output follows the frame max directly

    // Group edges, the partial bottom row (1080 = 16 groups + 56 px) and every stride phase
    const unsigned probes[][2] = {
        { 0, 0 }, { 60, 60 }, { 62, 62 }, { 63, 63 }, { 64, 64 }, { 1916, 1076 }, { 1916, 0 }, { 0, 1076 },
        { 13, 14 }, { 1911, 1071 }, { 641, 1030 }, { 1301, 1075 }, { 66, 1023 }
    };
    for (const auto& p : probes) {
        SyntheticFrame f{ 1920, 1080, { { p[0], p[1], 4, 4, 12.5f, 12.5f, 12.5f } } };  // 1000 nits
        INFO("probe (" << p[0] << "," << p[1] << ")");
        CHECK(gpu.RunFrame(f, raw) == doctest::Approx(1000.0f).epsilon(0.002));
    }

    // A single pixel off the stride grid is not sampled (deliberate — see peak_detect.h)
    SyntheticFrame lone{ 1920, 1080, { { 1001, 501, 1, 1, 12.5f, 12.5f, 12.5f } } };
    CHECK(gpu.RunFrame(lone, raw) < 0.01f);

    // Negative (out-of-gamut) luminance never counts; a black frame reads 0
    SyntheticFrame negative{ 1920, 1080, { { 0, 0, 64, 64, -2.0f, -2.0f, -2.0f } } };
    CHECK(gpu.RunFrame(negative, raw) < 0.01f);
    SyntheticFrame black{ 1920, 1080, {} };
    CHECK(gpu.RunFrame(black, raw) < 0.01f);
}

TEST_CASE("PeakDetect: two monitors of different sizes sharing one constant buffer each reduce their own frame") {
    // Overlay and hook share one peak CB across monitors. The old overlay only rewrote it when THAT
    // monitor's size changed, so after a 1080p monitor wrote it a 4K monitor reduced with 1920x1080
    // bounds and missed everything right of x=1920 / below y=1080.
    PeakGpuHarness gpu;
    PEAK_GPU_INIT_OR_SKIP(gpu, 2);
    const PeakParams raw = NoSmoothing();

    SyntheticFrame frame1080{ 1920, 1080, { { 100, 100, 4, 4, 5.0f, 5.0f, 5.0f } } };        // 400 nits
    SyntheticFrame frame4k{ 3840, 2160, { { 3000, 2000, 4, 4, 12.5f, 12.5f, 12.5f } } };    // 1000 nits
    for (int frame = 0; frame < 3; frame++) {
        INFO("frame " << frame);
        CHECK(gpu.RunFrame(frame1080, raw, 0) == doctest::Approx(400.0f).epsilon(0.002));
        CHECK(gpu.RunFrame(frame4k, raw, 1) == doctest::Approx(1000.0f).epsilon(0.002));
    }
}

// =============================================================================================
// Per-monitor peak state (DWM hook: AcquirePeakSlot + one detector pair per monitor)
// =============================================================================================

TEST_CASE("PeakDetect: AcquirePeakSlot keys by monitor position, reuses, fills free slots, recycles LRU") {
    PeakSlotKey keys[3] = {};
    bool fresh = false;
    unsigned long long clock = 0;

    int a = AcquirePeakSlot(keys, 3, 0, 0, ++clock, &fresh);
    CHECK(a == 0);
    CHECK(fresh);
    int b = AcquirePeakSlot(keys, 3, 3840, 0, ++clock, &fresh);
    CHECK(b == 1);
    CHECK(fresh);

    // Alternating presents (two HDR monitors) keep their own slots and never re-freshen
    for (int i = 0; i < 4; i++) {
        CHECK(AcquirePeakSlot(keys, 3, 0, 0, ++clock, &fresh) == a);
        CHECK_FALSE(fresh);
        CHECK(AcquirePeakSlot(keys, 3, 3840, 0, ++clock, &fresh) == b);
        CHECK_FALSE(fresh);
    }

    // Negative / off-origin positions are ordinary keys; x and y both matter
    int c = AcquirePeakSlot(keys, 3, -1920, 0, ++clock, &fresh);
    CHECK(c == 2);
    CHECK(fresh);

    // Table full: a new position takes the least-recently-used slot (monitor 0,0 is now oldest
    // after we touch the other two) and is reported fresh so the caller resets its history
    AcquirePeakSlot(keys, 3, 3840, 0, ++clock, &fresh);
    AcquirePeakSlot(keys, 3, -1920, 0, ++clock, &fresh);
    int d = AcquirePeakSlot(keys, 3, 0, 2160, ++clock, &fresh);
    CHECK(d == a);
    CHECK(fresh);
    CHECK(keys[d].left == 0);
    CHECK(keys[d].top == 2160);

    // The evicted monitor comes back: it gets a (fresh) slot again, the LRU one — (3840,0)
    int e = AcquirePeakSlot(keys, 3, 0, 0, ++clock, &fresh);
    CHECK(e == b);
    CHECK(fresh);

    // Degenerate table
    CHECK(AcquirePeakSlot(keys, 0, 0, 0, ++clock, &fresh) == -1);
    CHECK_FALSE(fresh);
}

TEST_CASE("PeakDetect: two HDR monitors on their own peak state smooth independently; reset drops history") {
    // The hook used ONE smoothed-peak texture for every monitor: with two HDR monitors on dynamic
    // tonemap each monitor's peak was slewed from the other monitor's last frame. Interleave
    // presents of a 1600-nit and a 200-nit monitor with smoothing ON: each must track its own
    // reference as if it were alone.
    PeakGpuHarness gpu;
    PEAK_GPU_INIT_OR_SKIP(gpu, 2);
    PeakParams sp{};

    SyntheticFrame bright{ 1920, 1080, { { 100, 100, 4, 4, 20.0f, 20.0f, 20.0f } } };   // 1600 nits
    SyntheticFrame dim{ 3840, 2160, { { 3000, 2000, 4, 4, 2.5f, 2.5f, 2.5f } } };       // 200 nits
    float refBright = 0.0f, refDim = 0.0f;
    bool haveBright = false, haveDim = false;
    for (int frame = 0; frame < 5; frame++) {
        INFO("frame " << frame);
        refBright = ReferenceSmoothedPeak(refBright, haveBright, ReferenceFramePeakNits(bright), sp);
        haveBright = true;
        refDim = ReferenceSmoothedPeak(refDim, haveDim, ReferenceFramePeakNits(dim), sp);
        haveDim = true;
        CHECK(gpu.RunFrame(bright, sp, 0) == doctest::Approx(refBright).epsilon(0.002));
        CHECK(gpu.RunFrame(dim, sp, 1) == doctest::Approx(refDim).epsilon(0.002));
    }
    CHECK(refBright == doctest::Approx(1600.0f).epsilon(0.002));
    CHECK(refDim == doctest::Approx(200.0f).epsilon(0.002));

    // A slot recycled to another monitor: without a reset it would slew down from 1600 at
    // 50 nits/frame; after ResetPeakState the first frame initializes to that frame's max.
    ResetPeakState(gpu.dc, gpu.detectors[0].peakUAV, gpu.detectors[0].rawUAV);
    CHECK(gpu.RunFrame(dim, sp, 0) == doctest::Approx(200.0f).epsilon(0.002));
}
