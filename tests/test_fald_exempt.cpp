// DesktopLUT's own windows are exempt from the FALD views (shared/fald_exempt.h): while a view (debug mode 1..9) is on,
// both paths draw the pixel pass again with the view off, scissored to DesktopLUT's windows, so the interface stays
// readable. Covered here:
//   - the pure geometry: per-monitor clipping and merging, the wire packing, the mapping onto a frame (same size, a
//     turned back buffer, a scaled one), outward rounding;
//   - on the WARP software device, the overlay's FaldRunPasses and the DWM hook's FaldRun (hook_fald.cpp compiled into
//     this exe, DLUT_HOOK_TESTING) on a synthetic panel file and frame:
//       * view on + rects: inside every rect the output is the CORRECTED output byte for byte, outside it is the view's;
//       * view off + rects: the output is the corrected output byte for byte (no rect is drawn);
//       * the rasterizer state and the scissor rects the caller had bound are bound again afterwards (DWM's own state).
//   The normal output with no rects is held to main by the OFF manifest (tests/test_fald_fade.cpp, opt-in).
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include "doctest.h"
#include "../src/fald.h"
#include "../src/types.h"
#include "../src/globals.h"
#include "../shared/fald_shader.h"
#include "../shared/fald_exempt.h"
#include "../dwm_hook/hook_fald.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <DirectXPackedVector.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------------------------------------------------
namespace {

bool Same(const FaldExemptRect& a, const FaldExemptRect& b) { return a.x0 == b.x0 && a.y0 == b.y0 && a.x1 == b.x1 && a.y1 == b.y1; }

bool Covers(const FaldExemptRect* r, int n, int x, int y) {
    for (int i = 0; i < n; i++) if (x >= r[i].x0 && x < r[i].x1 && y >= r[i].y0 && y < r[i].y1) return true;
    return false;
}

}  // namespace

TEST_CASE("FALD views exempt: a window lands on the monitors it covers, in their pixels, clipped") {
    // Two monitors side by side: 2560 x 1440 at (0, 0), 3840 x 2160 at (2560, -200). A window straddles both.
    const FaldExemptRect desk[] = { { 2400, 100, 2900, 600 },     // straddles
                                    { -50, -30, 200, 100 },       // off the left monitor's top-left corner
                                    { 5000, 3000, 5200, 3100 } }; // on no monitor
    FaldExemptRect a[FALD_EXEMPT_WIRE_RECTS], b[FALD_EXEMPT_WIRE_RECTS];
    const int na = FaldExemptForMonitor(desk, 3, 0, 0, 2560, 1440, false, a, FALD_EXEMPT_WIRE_RECTS);
    REQUIRE(na == 2);
    CHECK(Same(a[0], { 2400, 100, 2560, 600 }));
    CHECK(Same(a[1], { 0, 0, 200, 100 }));
    const int nb = FaldExemptForMonitor(desk, 3, 2560, -200, 3840, 2160, false, b, FALD_EXEMPT_WIRE_RECTS);
    REQUIRE(nb == 1);
    CHECK(Same(b[0], { 0, 300, 340, 800 }));
    // A minimised or hidden window never reaches here (the host skips it): an empty list is empty.
    CHECK(FaldExemptForMonitor(desk, 0, 0, 0, 2560, 1440, false, a, FALD_EXEMPT_WIRE_RECTS) == 0);
}

TEST_CASE("FALD views exempt: more windows than slots merge, never leaving a window's pixel out") {
    const FaldExemptRect desk[] = { { 10, 10, 110, 60 }, { 1000, 500, 1400, 900 }, { 1390, 880, 1500, 950 },
                                    { 20, 20, 80, 50 },  { 300, 300, 320, 320 },  { 1800, 100, 1900, 200 } };
    FaldExemptRect out[FALD_EXEMPT_WIRE_RECTS];
    const int n = FaldExemptForMonitor(desk, 6, 0, 0, 2560, 1440, false, out, FALD_EXEMPT_WIRE_RECTS);
    CHECK(n == FALD_EXEMPT_WIRE_RECTS);
    for (const FaldExemptRect& d : desk)
        for (int y = d.y0; y < d.y1; y += 7)
            for (int x = d.x0; x < d.x1; x += 7) CHECK(Covers(out, n, x, y));
    // the one inside another goes first: the small window inside the first one adds nothing
    FaldExemptRect two[2] = { { 10, 10, 110, 60 }, { 20, 20, 80, 50 } };
    CHECK(FaldExemptMerge(two, 2, 1) == 1);
    CHECK(Same(two[0], { 10, 10, 110, 60 }));
    // the host's list is bounded: windows beyond it join the last slot, none is dropped
    std::vector<FaldExemptRect> many;
    for (int i = 0; i < 40; i++) many.push_back({ i * 50, i * 30, i * 50 + 20, i * 30 + 10 });
    const int m = FaldExemptForMonitor(many.data(), (int)many.size(), 0, 0, 2560, 1440, false, out, FALD_EXEMPT_WIRE_RECTS);
    CHECK(m == FALD_EXEMPT_WIRE_RECTS);
    for (const FaldExemptRect& d : many)
        if (d.x0 < 2560 && d.y0 < 1440) CHECK(Covers(out, m, d.x0, d.y0));
    // an upside-down monitor also gets each rect turned 180 degrees
    const FaldExemptRect one[] = { { 100, 200, 300, 260 } };
    FaldExemptRect r[FALD_EXEMPT_WIRE_RECTS];
    REQUIRE(FaldExemptForMonitor(one, 1, 0, 0, 1920, 1080, true, r, FALD_EXEMPT_WIRE_RECTS) == 2);
    CHECK(Same(r[0], { 100, 200, 300, 260 }));
    CHECK(Same(r[1], { 1620, 820, 1820, 880 }));
}

TEST_CASE("FALD views exempt: the wire packs three rects in six words; zero is none") {
    const FaldExemptRect in[] = { { 0, 0, 640, 360 }, { 100, 7000, 65535, 65535 }, { -5, 3, 70000, 9 } };
    uint32_t w[FALD_EXEMPT_WIRE_WORDS];
    FaldExemptPack(in, 3, w);
    FaldExemptRect out[FALD_EXEMPT_WIRE_RECTS];
    REQUIRE(FaldExemptUnpack(w, out) == 3);
    CHECK(Same(out[0], in[0]));
    CHECK(Same(out[1], in[1]));
    CHECK(Same(out[2], { 0, 3, 65535, 9 }));          // clamped to the 16-bit range
    uint32_t zero[FALD_EXEMPT_WIRE_WORDS] = {};
    CHECK(FaldExemptUnpack(zero, out) == 0);           // a host that predates the field: no rect
    FaldExemptPack(in, 1, w);
    CHECK(FaldExemptUnpack(w, out) == 1);
    CHECK(w[2] == 0u); CHECK(w[3] == 0u); CHECK(w[4] == 0u); CHECK(w[5] == 0u);
    const FaldExemptRect empty[] = { { 10, 10, 10, 50 } };
    FaldExemptPack(empty, 1, w);
    CHECK(FaldExemptUnpack(w, out) == 0);
}

TEST_CASE("FALD views exempt: onto the frame - as is, both quarter turns of a turned buffer, scaled outward") {
    const FaldExemptRect r[] = { { 100, 200, 300, 260 } };
    FaldExemptRect out[FALD_EXEMPT_FRAME_RECTS];
    // the frame is the monitor
    REQUIRE(FaldExemptToFrame(r, 1, 1920, 1080, 1920, 1080, out) == 1);
    CHECK(Same(out[0], r[0]));
    // unknown monitor size: the frame's
    REQUIRE(FaldExemptToFrame(r, 1, 0, 0, 1920, 1080, out) == 1);
    CHECK(Same(out[0], r[0]));
    // a 1080 x 1920 (portrait) monitor whose back buffer is 1920 x 1080: both turns, each the window's size
    const FaldExemptRect p[] = { { 100, 200, 300, 260 } };   // monitor pixels, 1080 wide x 1920 high
    REQUIRE(FaldExemptToFrame(p, 1, 1080, 1920, 1920, 1080, out) == 2);
    CHECK(Same(out[0], { 200, 1080 - 300, 260, 1080 - 100 }));
    CHECK(Same(out[1], { 1920 - 260, 100, 1920 - 200, 300 }));
    for (int i = 0; i < 2; i++) CHECK((out[i].x1 - out[i].x0) * (out[i].y1 - out[i].y0) == 200 * 60);
    // any other size: scaled, the start floored and the end ceiled
    const FaldExemptRect s[] = { { 1, 1, 3, 3 } };
    REQUIRE(FaldExemptToFrame(s, 1, 3, 3, 4, 4, out) == 1);
    CHECK(Same(out[0], { 1, 1, 4, 4 }));
    // clipped to the frame; empties dropped
    const FaldExemptRect c[] = { { -10, -10, 50, 50 }, { 2000, 0, 2100, 10 } };
    REQUIRE(FaldExemptToFrame(c, 2, 1920, 1080, 1920, 1080, out) == 1);
    CHECK(Same(out[0], { 0, 0, 50, 50 }));
}

// ---------------------------------------------------------------------------------------------------------------------
// WARP: both paths
// ---------------------------------------------------------------------------------------------------------------------
namespace {

constexpr int kW = 640, kH = 270;   // 8 x 6 zones of 80 x 45 px

std::vector<char> ExemptPanel() {
    const uint32_t cols = 8, rows = 6, sub = 4, cellW = 80, cellH = 45, rT = 3, rE = 2, curveN = 256;
    std::vector<uint32_t> h(104, 0);
    auto U = [&](size_t i, uint32_t v) { h[i] = v; };
    auto F = [&](size_t i, float v) { std::memcpy(&h[i], &v, 4); };
    U(0, 0x464C4434u);                                                       // 'FLD4'
    U(1, cols); U(2, rows); U(3, sub); U(4, cellW); U(5, cellH); U(6, 0); U(7, 0);
    U(8, rT); U(9, rT); U(10, rE); U(11, rE); U(12, curveN);
    const float white = 1000.0f, lnMin = std::log(1e-2f), lnMax = std::log(10000.0f);
    F(13, white); F(14, 1.0e-3f); F(15, 1150.0f);
    F(16, 0.2627f); F(17, 0.678f); F(18, 0.0593f);
    F(19, 0.25f); F(20, 4.0f); F(21, 0.5f);
    F(22, lnMin); F(23, lnMax); F(24, -10.0f); F(25, -12.0f);
    F(26, 0.004f); F(27, 0.03f); F(28, 0.7f); F(29, 0.5f); F(30, 5.0f);
    std::vector<float> t;
    for (uint32_t i = 0; i < curveN; i++) {
        const float ln = lnMin + (lnMax - lnMin) * (float)i / (float)(curveN - 1);
        float d = (ln - std::log(0.3f)) / (std::log(white) - std::log(0.3f));
        d = d < 0.0f ? 0.0f : (d > 1.0f ? 1.0f : d);
        t.push_back(std::pow(d, 1.3f));
    }
    auto kernels = [&](uint32_t reach, double scale) {
        std::vector<double> k;
        double sum = 0.0;
        const uint32_t W = 2 * reach + 1;
        for (uint32_t oy = 0; oy < sub; oy++)
            for (uint32_t ox = 0; ox < sub; ox++)
                for (uint32_t j = 0; j < W; j++)
                    for (uint32_t i = 0; i < W; i++) {
                        const double dx = ((int)i - (int)reach) + (ox + 0.5) / sub - 0.5;
                        const double dy = ((int)j - (int)reach) + (oy + 0.5) / sub - 0.5;
                        const double v = std::exp(-std::sqrt(dx * dx + 1.4 * dy * dy) / scale);
                        k.push_back(v); sum += v;
                    }
        const double norm = sum / (double)(sub * sub);
        for (double v : k) t.push_back((float)(v / norm));
    };
    kernels(rT, 0.9);
    kernels(rE, 0.6);
    std::vector<char> b(h.size() * 4 + t.size() * 4);
    std::memcpy(b.data(), h.data(), h.size() * 4);
    std::memcpy(b.data() + h.size() * 4, t.data(), t.size() * 4);
    return b;
}

// FP16 scRGB: a dim gradient and bright windows straddling zone borders (a gain map with something on it).
std::vector<uint16_t> ExemptFrame() {
    using DirectX::PackedVector::XMConvertFloatToHalf;
    std::vector<uint16_t> f((size_t)kW * kH * 4);
    for (int y = 0; y < kH; y++)
        for (int x = 0; x < kW; x++) {
            float v = 0.003f + 0.04f * (float)(x + 2 * y) / (float)(kW + 2 * kH);
            if (x >= 190 && x < 330 && y >= 60 && y < 140) v = 10.0f;
            if (x >= 420 && x < 470 && y >= 150 && y < 230) v = 5.0f;
            const size_t o = ((size_t)y * kW + x) * 4;
            f[o] = f[o + 1] = f[o + 2] = XMConvertFloatToHalf(v);
            f[o + 3] = XMConvertFloatToHalf(1.0f);
        }
    return f;
}

struct ExemptTempFile {
    std::wstring path;
    explicit ExemptTempFile(const std::vector<char>& bytes) {
        wchar_t dir[MAX_PATH] = {};
        GetTempPathW(MAX_PATH, dir);
        path = std::wstring(dir) + L"dlut_fald_exempt_" + std::to_wstring(GetCurrentProcessId()) + L".bin";
        std::ofstream f(path, std::ios::binary);
        f.write(bytes.data(), (std::streamsize)bytes.size());
    }
    ~ExemptTempFile() { DeleteFileW(path.c_str()); }
};

std::vector<uint8_t> ReadTex(ID3D11Device* dev, ID3D11DeviceContext* ic, ID3D11Texture2D* tex) {
    std::vector<uint8_t> out;
    D3D11_TEXTURE2D_DESC d;
    tex->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ; d.MiscFlags = 0;
    ID3D11Texture2D* st = nullptr;
    if (FAILED(dev->CreateTexture2D(&d, nullptr, &st))) return out;
    ic->CopyResource(st, tex);
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(ic->Map(st, 0, D3D11_MAP_READ, 0, &m))) {
        out.resize((size_t)d.Width * d.Height * 8);
        for (UINT y = 0; y < d.Height; y++)
            std::memcpy(out.data() + (size_t)y * d.Width * 8, (const uint8_t*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * 8);
        ic->Unmap(st, 0);
    }
    st->Release();
    return out;
}

bool PixelEq(const std::vector<uint8_t>& a, const std::vector<uint8_t>& b, int x, int y) {
    const size_t o = ((size_t)y * kW + x) * 8;
    return std::memcmp(a.data() + o, b.data() + o, 8) == 0;
}

// Pixels of `got` inside the rects equal `inside`, outside equal `outside` (both byte for byte). Counts the mismatches.
void CheckSplit(const std::vector<uint8_t>& got, const std::vector<uint8_t>& inside, const std::vector<uint8_t>& outside,
                const FaldExemptRect* r, int n, const char* what) {
    REQUIRE(got.size() == (size_t)kW * kH * 8);
    int badIn = 0, badOut = 0, nIn = 0;
    for (int y = 0; y < kH; y++)
        for (int x = 0; x < kW; x++) {
            if (Covers(r, n, x, y)) { nIn++; if (!PixelEq(got, inside, x, y)) badIn++; }
            else if (!PixelEq(got, outside, x, y)) badOut++;
        }
    CHECK_MESSAGE(nIn > 0, what);
    CHECK_MESSAGE(badIn == 0, what << ": " << badIn << " pixels inside the windows are not the corrected picture");
    CHECK_MESSAGE(badOut == 0, what << ": " << badOut << " pixels outside the windows are not the view");
}

// A state of the caller's own (DWM's, in the hook): a rasterizer state and two scissor rects, checked afterwards.
struct CallerState {
    ID3D11RasterizerState* rs = nullptr;
    D3D11_RECT sc[2] = { { 1, 2, 3, 4 }, { 5, 6, 7, 8 } };
    void Bind(ID3D11Device* dev, ID3D11DeviceContext* ic) {
        if (!rs) {
            D3D11_RASTERIZER_DESC d = {};
            d.FillMode = D3D11_FILL_SOLID; d.CullMode = D3D11_CULL_NONE; d.DepthClipEnable = TRUE;   // scissor OFF
            dev->CreateRasterizerState(&d, &rs);
        }
        ic->RSSetState(rs);
        ic->RSSetScissorRects(2, sc);
    }
    void Check(ID3D11DeviceContext* ic, const char* what) {
        ID3D11RasterizerState* now = nullptr;
        ic->RSGetState(&now);
        CHECK_MESSAGE(now == rs, what << ": the caller's rasterizer state is not bound again");
        if (now) now->Release();
        D3D11_RECT got[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};
        UINT n = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
        ic->RSGetScissorRects(&n, got);
        CHECK_MESSAGE(n == 2u, what << ": scissor rect count");
        if (n == 2u)
            for (int i = 0; i < 2; i++)
                CHECK_MESSAGE((got[i].left == sc[i].left && got[i].top == sc[i].top && got[i].right == sc[i].right &&
                               got[i].bottom == sc[i].bottom), what << ": scissor rect " << i);
    }
    ~CallerState() { if (rs) rs->Release(); }
};

// ---- the overlay path ----------------------------------------------------------------------------------------------
struct OverlayWarp {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11VertexShader* vs = nullptr;
    bool ok = false;
    OverlayWarp() {
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
    ~OverlayWarp() {
        ReleaseFaldShaders();
        FaldSetExemptRects({});
        if (vs) vs->Release();
        g_context = nullptr; g_device = nullptr;
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
    // One run of the layer at desktop position (x, y); `desktop` = the host's list of DesktopLUT's windows.
    std::vector<uint8_t> Run(const std::wstring& panel, const std::vector<uint16_t>& frame, unsigned int view,
                             const std::vector<FaldExemptRect>& desktop, int x = 0, int y = 0, CallerState* caller = nullptr) {
        std::vector<uint8_t> out;
        FaldSetExemptRects(desktop);
        MonitorContext ctx;
        ctx.width = kW; ctx.height = kH; ctx.x = x; ctx.y = y;
        ctx.isHDREnabled = true; ctx.isFP16SDR = false;
        ctx.swapchainFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
        FaldSettings& fs = ctx.hdrColorCorrection.fald;
        fs.enabled = true; fs.paramsPath = panel; fs.debugMode = view;
        if (!FaldEnsureResources(&ctx, fs)) { FaldReleaseResources(&ctx); return out; }
        D3D11_TEXTURE2D_DESC td = {};
        td.Width = kW; td.Height = kH; td.MipLevels = 1; td.ArraySize = 1;
        td.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; td.SampleDesc.Count = 1; td.Usage = D3D11_USAGE_DEFAULT;
        td.BindFlags = D3D11_BIND_RENDER_TARGET;
        ID3D11Texture2D* target = nullptr;
        ID3D11RenderTargetView* rtv = nullptr;
        if (SUCCEEDED(dev->CreateTexture2D(&td, nullptr, &target)) && SUCCEEDED(dev->CreateRenderTargetView(target, nullptr, &rtv))) {
            D3D11_VIEWPORT vp = { 0, 0, (float)kW, (float)kH, 0, 1 };
            ic->RSSetViewports(1, &vp);
            ic->VSSetShader(vs, nullptr, 0);
            ic->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            if (caller) caller->Bind(dev, ic);
            ic->UpdateSubresource(ctx.fald->inter, 0, nullptr, frame.data(), (UINT)kW * 8u, 0);
            FaldRunPasses(&ctx, rtv, true);
            if (caller) caller->Check(ic, "overlay");
            ic->RSSetState(nullptr);
            ic->RSSetScissorRects(0, nullptr);
            ic->Flush();
            out = ReadTex(dev, ic, target);
        }
        if (rtv) rtv->Release();
        if (target) target->Release();
        FaldReleaseResources(&ctx);
        return out;
    }
};

// ---- the DWM hook path ---------------------------------------------------------------------------------------------
struct HookStage {
    std::string root;
    explicit HookStage(const std::vector<char>& panel) {
        char tmp[MAX_PATH] = {};
        GetTempPathA(MAX_PATH, tmp);
        root = std::string(tmp) + "dlut_fald_exempt_hook_" + std::to_string(GetCurrentProcessId());
        CreateDirectoryA(root.c_str(), nullptr);
        CreateDirectoryA((root + "\\fald").c_str(), nullptr);
        std::ofstream f(root + "\\fald\\0_0_hdr.bin", std::ios::binary);
        f.write(panel.data(), (std::streamsize)panel.size());
    }
    ~HookStage() {
        DeleteFileA((root + "\\fald\\0_0_hdr.bin").c_str());
        RemoveDirectoryA((root + "\\fald").c_str());
        RemoveDirectoryA(root.c_str());
    }
};

struct HookRig {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ic = nullptr;
    ID3D11Texture2D* back = nullptr;
    ID3D11RenderTargetView* backRTV = nullptr;
    bool ok = false;
    HookRig() {
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &ic))) return;
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = kW; d.Height = kH; d.MipLevels = 1; d.ArraySize = 1; d.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT; d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        if (FAILED(dev->CreateTexture2D(&d, nullptr, &back)) || FAILED(dev->CreateRenderTargetView(back, nullptr, &backRTV))) return;
        ok = FaldInitShaders(dev, ic);
    }
    ~HookRig() {
        FaldReleaseAll();
        FaldReleaseShaders();
        if (backRTV) backRTV->Release();
        if (back) back->Release();
        if (ic) { ic->ClearState(); ic->Flush(); ic->Release(); }
        if (dev) dev->Release();
    }
    // One present of the monitor at (0, 0) in HDR: the frame composed into the back buffer, the layer run into it.
    // `wire` = the shared config's exempt words (monitor pixels); monW / monH = the monitor's desktop size.
    std::vector<uint8_t> Present(const std::vector<uint16_t>& frame, unsigned int view, const uint32_t* wire,
                                 unsigned int monW, unsigned int monH, CallerState* caller = nullptr) {
        std::vector<uint8_t> out;
        DwmHookFaldTuning tuning = {};
        tuning.starEven = 0.8f; tuning.starTargetGain = 1.0f; tuning.starKeepNits = 100.0f; tuning.starStrength = 1.0f;
        tuning.starAreaLo = 40.0f; tuning.starAreaHi = 160.0f; tuning.starNbLo = 0.15f; tuning.starNbHi = 0.30f;
        tuning.starReach = 2u; tuning.starEvenReach = 8u;
        if (wire) for (int i = 0; i < FALD_EXEMPT_WIRE_WORDS; i++) tuning.exempt[i] = wire[i];
        for (int attempt = 0; attempt < 8; attempt++) {
            ic->UpdateSubresource(back, 0, nullptr, frame.data(), (UINT)kW * 8u, 0);
            FaldMonitor* m = FaldAcquire(0, 0, true, kW, kH, DXGI_FORMAT_R16G16B16A16_FLOAT);
            if (!m) return out;
            FaldSetLiveSettings(m, view, 0, false, &tuning, false, monW, monH);
            RECT full = { 0, 0, kW, kH };
            if (!FaldUpdateClean(m, back, &full, 1)) { FaldReturn(m); FaldMarkStale(0, 0); continue; }
            ic->CopyResource(FaldIntermediateTexture(m), FaldCleanTexture(m));
            if (caller) caller->Bind(dev, ic);
            const bool ran = FaldRun(m, backRTV, true);
            if (caller) caller->Check(ic, "hook");
            ic->RSSetState(nullptr);
            ic->RSSetScissorRects(0, nullptr);
            FaldReturn(m);
            ic->Flush();
            if (ran) out = ReadTex(dev, ic, back);
            return out;
        }
        return out;
    }
};

}  // namespace

TEST_CASE("FALD views exempt (overlay, WARP): DesktopLUT's windows show the corrected picture, the rest the view") {
    OverlayWarp w;
    REQUIRE(w.ok);
    const ExemptTempFile panel(ExemptPanel());
    const std::vector<uint16_t> frame = ExemptFrame();
    const std::vector<FaldExemptRect> none;
    const std::vector<uint8_t> corrected = w.Run(panel.path, frame, 0, none);
    REQUIRE(corrected.size() == (size_t)kW * kH * 8);
    // windows over the bright patch's edge, a zone corner, the frame's edge (partly off the monitor)
    const std::vector<FaldExemptRect> windows = { { 150, 40, 260, 120 }, { 400, 140, 520, 200 }, { 600, 250, 700, 300 } };
    const FaldExemptRect inFrame[] = { { 150, 40, 260, 120 }, { 400, 140, 520, 200 }, { 600, 250, 640, 270 } };
    for (unsigned int view : { 1u, 2u, 4u, 9u }) {
        const std::vector<uint8_t> v = w.Run(panel.path, frame, view, none);
        REQUIRE(v.size() == corrected.size());
        if (view != 4) CHECK_MESSAGE(v != corrected, "view " << view << " looks like the corrected picture: the test proves nothing");
        CallerState caller;
        const std::vector<uint8_t> got = w.Run(panel.path, frame, view, windows, 0, 0, &caller);
        CheckSplit(got, corrected, v, inFrame, 3, ("overlay view " + std::to_string(view)).c_str());
    }
    // the view off: the windows change nothing (no draw is added), byte for byte
    CallerState caller;
    CHECK(w.Run(panel.path, frame, 0, windows, 0, 0, &caller) == corrected);
    // a second monitor at (2560, -100): the windows are found in its pixels
    std::vector<FaldExemptRect> shifted;
    for (const FaldExemptRect& r : windows) shifted.push_back({ r.x0 + 2560, r.y0 - 100, r.x1 + 2560, r.y1 - 100 });
    const std::vector<uint8_t> v1 = w.Run(panel.path, frame, 1, none, 2560, -100);
    CheckSplit(w.Run(panel.path, frame, 1, shifted, 2560, -100), corrected, v1, inFrame, 3, "overlay on a second monitor");
    // windows on another monitor only: the view everywhere
    std::vector<FaldExemptRect> elsewhere = { { 5000, 0, 5200, 100 } };
    CHECK(w.Run(panel.path, frame, 1, elsewhere) == v1);
}

TEST_CASE("FALD views exempt (DWM hook, WARP): DesktopLUT's windows show the corrected picture, DWM's state put back") {
    const std::vector<char> bytes = ExemptPanel();
    const std::vector<uint16_t> frame = ExemptFrame();
    HookStage stage(bytes);
    HookRig rig;
    REQUIRE(rig.ok);
    REQUIRE(FaldLoadPanelFiles(stage.root.c_str()) == 1);
    const std::vector<uint8_t> corrected = rig.Present(frame, 0, nullptr, kW, kH);
    REQUIRE(corrected.size() == (size_t)kW * kH * 8);
    const FaldExemptRect monitorRects[] = { { 150, 40, 260, 120 }, { 400, 140, 520, 200 }, { 600, 250, 640, 270 } };
    uint32_t wire[FALD_EXEMPT_WIRE_WORDS];
    FaldExemptPack(monitorRects, 3, wire);
    for (unsigned int view : { 1u, 3u, 9u }) {
        const std::vector<uint8_t> v = rig.Present(frame, view, nullptr, kW, kH);
        REQUIRE(v.size() == corrected.size());
        CHECK_MESSAGE(v != corrected, "view " << view << " looks like the corrected picture: the test proves nothing");
        CallerState caller;
        CheckSplit(rig.Present(frame, view, wire, kW, kH, &caller), corrected, v, monitorRects, 3,
                   ("hook view " + std::to_string(view)).c_str());
    }
    // the view off: the words change nothing, byte for byte
    CallerState caller;
    CHECK(rig.Present(frame, 0, wire, kW, kH, &caller) == corrected);
    // a turned monitor (270 x 640 on the desktop) whose buffer is 640 x 270: both quarter turns of each window
    const FaldExemptRect portrait[] = { { 20, 100, 120, 200 } };
    uint32_t pw[FALD_EXEMPT_WIRE_WORDS];
    FaldExemptPack(portrait, 1, pw);
    FaldExemptRect mapped[FALD_EXEMPT_FRAME_RECTS];
    const int nm = FaldExemptToFrame(portrait, 1, kH, kW, kW, kH, mapped);
    REQUIRE(nm == 2);
    const std::vector<uint8_t> v1 = rig.Present(frame, 1, nullptr, kH, kW);
    CheckSplit(rig.Present(frame, 1, pw, kH, kW), corrected, v1, mapped, nm, "hook, turned monitor");
}

TEST_CASE("FALD views exempt: the overlay and the hook give the same bytes (corrected, and a view with windows)") {
    const std::vector<char> bytes = ExemptPanel();
    const std::vector<uint16_t> frame = ExemptFrame();
    std::vector<uint8_t> overlayCorrected, overlayView;
    const std::vector<FaldExemptRect> windows = { { 150, 40, 260, 120 }, { 400, 140, 520, 200 } };
    {
        OverlayWarp w;
        REQUIRE(w.ok);
        const ExemptTempFile panel(bytes);
        overlayCorrected = w.Run(panel.path, frame, 0, {});
        overlayView = w.Run(panel.path, frame, 1, windows);
    }
    HookStage stage(bytes);
    HookRig rig;
    REQUIRE(rig.ok);
    REQUIRE(FaldLoadPanelFiles(stage.root.c_str()) == 1);
    uint32_t wire[FALD_EXEMPT_WIRE_WORDS];
    FaldExemptPack(windows.data(), (int)windows.size(), wire);
    CHECK(rig.Present(frame, 0, nullptr, kW, kH) == overlayCorrected);
    CHECK(rig.Present(frame, 1, wire, kW, kH) == overlayView);
}
