#pragma once
// The logo artwork (assets/logo/logo_256.png, embedded as RCDATA) as a mipmapped D3D11 texture, so
// the interface draws the real logo, with its hazy RGB cloud, frame and transparent corners, cleanly
// at any size. GUI thread; tied to the interface's device.

#include <d3d11.h>

namespace ui {

// Decodes the embedded PNG (WIC) and creates the texture with a full mip chain. nullptr on failure
// (the caller falls back to the drawn cube).
ID3D11ShaderResourceView* CreateLogoTexture(ID3D11Device* device, ID3D11DeviceContext* context);

}  // namespace ui
