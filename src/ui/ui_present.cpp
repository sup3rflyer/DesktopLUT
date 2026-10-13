#include "ui_present.h"

namespace ui {

namespace {

template <typename T> void SafeRelease(T*& p) {
    if (p) { p->Release(); p = nullptr; }
}

bool CreateDevice(D3D_DRIVER_TYPE type, ID3D11Device** dev, ID3D11DeviceContext** ctx) {
    const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_0 };
    D3D_FEATURE_LEVEL got = {};
    HRESULT hr = D3D11CreateDevice(nullptr, type, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels,
                                   (UINT)(sizeof(levels) / sizeof(levels[0])), D3D11_SDK_VERSION, dev, &got, ctx);
    return SUCCEEDED(hr);
}

}  // namespace

bool Presenter::Create(HWND hwnd) {
    Destroy();
    m_hwnd = hwnd;
    if (!CreateDevice(D3D_DRIVER_TYPE_HARDWARE, &m_device, &m_context) &&
        !CreateDevice(D3D_DRIVER_TYPE_WARP, &m_device, &m_context))
        return false;

    IDXGIDevice* dxgiDevice = nullptr;
    IDXGIAdapter* adapter = nullptr;
    IDXGIFactory2* factory = nullptr;
    bool ok = SUCCEEDED(m_device->QueryInterface(__uuidof(IDXGIDevice), (void**)&dxgiDevice)) &&
              SUCCEEDED(dxgiDevice->GetAdapter(&adapter)) &&
              SUCCEEDED(adapter->GetParent(__uuidof(IDXGIFactory2), (void**)&factory));
    if (ok) {
        RECT rc = {};
        GetClientRect(hwnd, &rc);
        m_width = (UINT)(rc.right > rc.left ? rc.right - rc.left : 1);
        m_height = (UINT)(rc.bottom > rc.top ? rc.bottom - rc.top : 1);
        DXGI_SWAP_CHAIN_DESC1 sd = {};
        sd.Width = m_width;
        sd.Height = m_height;
        sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        sd.SampleDesc.Count = 1;
        sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        sd.BufferCount = 2;
        sd.Scaling = DXGI_SCALING_NONE;
        sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        sd.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
        ok = SUCCEEDED(factory->CreateSwapChainForHwnd(m_device, hwnd, &sd, nullptr, nullptr, &m_swap));
        if (ok) factory->MakeWindowAssociation(hwnd, DXGI_MWA_NO_ALT_ENTER);
    }
    SafeRelease(factory);
    SafeRelease(adapter);
    SafeRelease(dxgiDevice);
    if (!ok || !CreateTarget()) {
        Destroy();
        return false;
    }
    return true;
}

void Presenter::Destroy() {
    ReleaseTarget();
    if (m_context) m_context->ClearState();
    SafeRelease(m_swap);
    SafeRelease(m_context);
    SafeRelease(m_device);
    m_width = m_height = 0;
}

bool Presenter::CreateTarget() {
    ID3D11Texture2D* back = nullptr;
    if (FAILED(m_swap->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&back))) return false;
    HRESULT hr = m_device->CreateRenderTargetView(back, nullptr, &m_rtv);
    back->Release();
    return SUCCEEDED(hr);
}

void Presenter::ReleaseTarget() {
    if (m_context) m_context->OMSetRenderTargets(0, nullptr, nullptr);
    SafeRelease(m_rtv);
}

bool Presenter::Resize(UINT width, UINT height) {
    if (!m_swap || width == 0 || height == 0) return false;
    if (width == m_width && height == m_height) return true;
    ReleaseTarget();
    if (FAILED(m_swap->ResizeBuffers(0, width, height, DXGI_FORMAT_UNKNOWN, 0))) return false;
    m_width = width;
    m_height = height;
    return CreateTarget();
}

void Presenter::BeginFrame(const float clearRgba[4]) {
    if (!m_rtv) return;
    m_context->OMSetRenderTargets(1, &m_rtv, nullptr);
    m_context->ClearRenderTargetView(m_rtv, clearRgba);
}

bool Presenter::Present() {
    if (!m_swap) return false;
    HRESULT hr = m_swap->Present(1, 0);
    return !(hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET);
}

}  // namespace ui
