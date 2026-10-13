#include "ui_patch.h"

#include "ui_window.h"
#include <d3d11_1.h>
#include <dxgi1_4.h>
#include <cmath>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

namespace ui {

namespace {

constexpr wchar_t kClass[] = L"DesktopLUT_Patch";

template <typename T> void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

struct Patch {
    HWND hwnd = nullptr;
    HMONITOR monitor = nullptr;
    PatchSize size = PatchSize::Off;
    PatchLevel level;
    bool backdrop = false;
    RECT rect = {};                    // the window, screen coordinates
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext1* context = nullptr;
    IDXGISwapChain1* swap = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    bool hdrChain = false;             // the swap chain's format (FP16 scRGB) matches level.hdr
    UINT width = 0, height = 0;
    bool classRegistered = false;
};
Patch g_patch;

LRESULT CALLBACK PatchProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;   // a click never takes focus from the editor
    case WM_NCHITTEST: return HTCLIENT;
    case WM_ERASEBKGND: return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps;
        BeginPaint(hwnd, &ps);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_SETCURSOR: SetCursor(nullptr); return TRUE;   // no pointer over the patch (the meter sees no cursor)
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

void ReleaseChain() {
    SafeRelease(g_patch.rtv);
    SafeRelease(g_patch.swap);
}

void ReleaseAll() {
    ReleaseChain();
    SafeRelease(g_patch.context);
    SafeRelease(g_patch.device);
}

bool EnsureDevice() {
    if (g_patch.device) return true;
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ctx = nullptr;
    const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0 };
    if (FAILED(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, 2,
                                 D3D11_SDK_VERSION, &dev, nullptr, &ctx)))
        return false;
    ID3D11DeviceContext1* ctx1 = nullptr;   // ClearView: clear a rectangle (the patch) after the backdrop
    if (FAILED(ctx->QueryInterface(__uuidof(ID3D11DeviceContext1), (void**)&ctx1))) {
        ctx->Release();
        dev->Release();
        return false;
    }
    ctx->Release();
    g_patch.device = dev;
    g_patch.context = ctx1;
    return true;
}

// The swap chain for the window's size and the level's kind: FP16 scRGB for HDR, 10-bit sRGB for SDR.
bool EnsureChain(UINT w, UINT h, bool hdr) {
    if (g_patch.swap && g_patch.width == w && g_patch.height == h && g_patch.hdrChain == hdr) return true;
    ReleaseChain();
    IDXGIDevice* dxgiDevice = nullptr;
    IDXGIAdapter* adapter = nullptr;
    IDXGIFactory2* factory = nullptr;
    bool ok = SUCCEEDED(g_patch.device->QueryInterface(__uuidof(IDXGIDevice), (void**)&dxgiDevice)) &&
              SUCCEEDED(dxgiDevice->GetAdapter(&adapter)) &&
              SUCCEEDED(adapter->GetParent(__uuidof(IDXGIFactory2), (void**)&factory));
    if (ok) {
        DXGI_SWAP_CHAIN_DESC1 sd = {};
        sd.Width = w;
        sd.Height = h;
        sd.Format = hdr ? DXGI_FORMAT_R16G16B16A16_FLOAT : DXGI_FORMAT_R10G10B10A2_UNORM;
        sd.SampleDesc.Count = 1;
        sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        sd.BufferCount = 2;
        sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        sd.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
        ok = SUCCEEDED(factory->CreateSwapChainForHwnd(g_patch.device, g_patch.hwnd, &sd, nullptr, nullptr, &g_patch.swap));
        if (ok) factory->MakeWindowAssociation(g_patch.hwnd, DXGI_MWA_NO_ALT_ENTER);
    }
    if (ok) {
        IDXGISwapChain3* sc3 = nullptr;
        if (SUCCEEDED(g_patch.swap->QueryInterface(__uuidof(IDXGISwapChain3), (void**)&sc3))) {
            // scRGB (linear, 1.0 = 80 nits) for HDR; the default sRGB for SDR.
            sc3->SetColorSpace1(hdr ? DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709 : DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709);
            sc3->Release();
        }
        ID3D11Texture2D* back = nullptr;
        ok = SUCCEEDED(g_patch.swap->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&back)) &&
             SUCCEEDED(g_patch.device->CreateRenderTargetView(back, nullptr, &g_patch.rtv));
        SafeRelease(back);
    }
    SafeRelease(factory);
    SafeRelease(adapter);
    SafeRelease(dxgiDevice);
    if (!ok) { ReleaseChain(); return false; }
    g_patch.width = w;
    g_patch.height = h;
    g_patch.hdrChain = hdr;
    return true;
}

void Paint() {
    if (!g_patch.rtv) return;
    const float v = g_patch.level.hdr ? g_patch.level.value / 80.0f : g_patch.level.value;   // scRGB / signal
    const float black[4] = { 0, 0, 0, 1 };
    const float gray[4] = { v, v, v, 1 };
    if (g_patch.backdrop && g_patch.size == PatchSize::Window10) {
        g_patch.context->ClearRenderTargetView(g_patch.rtv, black);
        // 10 % of the display's area, its aspect, centred.
        const float s = std::sqrt(0.10f);
        const LONG pw = (LONG)(g_patch.width * s), ph = (LONG)(g_patch.height * s);
        const D3D11_RECT r = { (LONG)(g_patch.width - pw) / 2, (LONG)(g_patch.height - ph) / 2,
                               (LONG)(g_patch.width + pw) / 2, (LONG)(g_patch.height + ph) / 2 };
        g_patch.context->ClearView(g_patch.rtv, gray, &r, 1);
    } else {
        g_patch.context->ClearRenderTargetView(g_patch.rtv, gray);
    }
    g_patch.swap->Present(1, 0);
}

}  // namespace

void ShowPatch(HMONITOR monitor, PatchSize size, const PatchLevel& level) {
    if (size == PatchSize::Off || !monitor) { HidePatch(); return; }
    MONITORINFO mi = { sizeof(mi) };
    if (!GetMonitorInfoW(monitor, &mi)) { HidePatch(); return; }
    // A window patch on the display the interface is on stands alone (no black field over the editor).
    HWND ui = Window();
    const bool sameDisplay = ui && MonitorFromWindow(ui, MONITOR_DEFAULTTONULL) == monitor;
    const bool backdrop = size == PatchSize::Window10 && !sameDisplay;
    RECT rect = mi.rcMonitor;
    if (size == PatchSize::Window10 && !backdrop) {
        const LONG w = mi.rcMonitor.right - mi.rcMonitor.left, h = mi.rcMonitor.bottom - mi.rcMonitor.top;
        const float s = std::sqrt(0.10f);
        const LONG pw = (LONG)(w * s), ph = (LONG)(h * s);
        rect = { mi.rcMonitor.left + (w - pw) / 2, mi.rcMonitor.top + (h - ph) / 2,
                 mi.rcMonitor.left + (w + pw) / 2, mi.rcMonitor.top + (h + ph) / 2 };
    }
    if (!g_patch.classRegistered) {
        WNDCLASSEXW wc = { sizeof(wc) };
        wc.lpfnWndProc = PatchProc;
        wc.hInstance = GetModuleHandleW(nullptr);
        wc.lpszClassName = kClass;
        g_patch.classRegistered = RegisterClassExW(&wc) != 0;
        if (!g_patch.classRegistered) return;
    }
    if (!EnsureDevice()) return;
    const bool moved = !g_patch.hwnd || !EqualRect(&rect, &g_patch.rect);
    if (!g_patch.hwnd) {
        // Not topmost: in overlay mode the 3D LUT and the live preview are DesktopLUT's own topmost overlay, and
        // a patch above it would be measured uncorrected. Never activated: the editor keeps the keys.
        g_patch.hwnd = CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, kClass, L"DesktopLUT patch", WS_POPUP,
                                       rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, nullptr, nullptr,
                                       GetModuleHandleW(nullptr), nullptr);
        if (!g_patch.hwnd) return;
    } else if (moved) {
        SetWindowPos(g_patch.hwnd, HWND_TOP, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
                     SWP_NOACTIVATE);
    }
    const bool changed = moved || g_patch.size != size || g_patch.backdrop != backdrop || !(g_patch.level == level);
    g_patch.monitor = monitor;
    g_patch.size = size;
    g_patch.backdrop = backdrop;
    g_patch.level = level;
    g_patch.rect = rect;
    if (!EnsureChain((UINT)(rect.right - rect.left), (UINT)(rect.bottom - rect.top), level.hdr)) return;
    if (!IsWindowVisible(g_patch.hwnd)) {
        ShowWindow(g_patch.hwnd, SW_SHOWNOACTIVATE);
        Paint();
    } else if (changed) {
        Paint();
    }
}

void HidePatch() {
    if (!g_patch.hwnd) return;
    DestroyWindow(g_patch.hwnd);
    g_patch.hwnd = nullptr;
    ReleaseAll();
    g_patch.size = PatchSize::Off;
    g_patch.rect = {};
    g_patch.width = g_patch.height = 0;
}

bool PatchVisible() { return g_patch.hwnd && IsWindowVisible(g_patch.hwnd); }

}  // namespace ui
