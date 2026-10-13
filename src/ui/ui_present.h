#pragma once
// D3D11 device + flip-model swap chain for the interface window.
//
// The interface owns its device. It never shares g_device: that one belongs to the processing
// thread, is recreated on stop and on device loss, and its immediate context is used lock-free on
// the render hot path. GUI thread only.

#include <d3d11.h>
#include <dxgi1_2.h>
#include <windows.h>

namespace ui {

class Presenter {
public:
    bool Create(HWND hwnd);           // hardware device, WARP fallback; false = neither worked
    void Destroy();
    bool Valid() const { return m_swap != nullptr; }
    bool Resize(UINT width, UINT height);
    void BeginFrame(const float clearRgba[4]);
    // Presents with vsync. Returns false when the device was lost (caller re-creates next frame).
    bool Present();

    ID3D11Device* Device() const { return m_device; }
    ID3D11DeviceContext* Context() const { return m_context; }

private:
    bool CreateTarget();
    void ReleaseTarget();

    HWND m_hwnd = nullptr;
    ID3D11Device* m_device = nullptr;
    ID3D11DeviceContext* m_context = nullptr;
    IDXGISwapChain1* m_swap = nullptr;
    ID3D11RenderTargetView* m_rtv = nullptr;
    UINT m_width = 0, m_height = 0;
};

}  // namespace ui
