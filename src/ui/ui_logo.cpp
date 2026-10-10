#include "ui_logo.h"

#include "../../resource.h"
#include <wincodec.h>
#include <vector>

#pragma comment(lib, "windowscodecs.lib")

namespace ui {

namespace {

template <typename T> void SafeRelease(T*& p) {
    if (p) { p->Release(); p = nullptr; }
}

// The PNG's pixels as straight-alpha RGBA8 (what ImGui's DX11 renderer blends).
bool DecodeLogo(std::vector<unsigned char>& rgba, UINT& width, UINT& height) {
    HMODULE module = GetModuleHandleW(nullptr);
    HRSRC res = FindResourceW(module, MAKEINTRESOURCEW(IDR_LOGO_PNG), MAKEINTRESOURCEW(10) /* RT_RCDATA */);
    if (!res) return false;
    HGLOBAL data = LoadResource(module, res);
    const void* bytes = data ? LockResource(data) : nullptr;
    const DWORD size = SizeofResource(module, res);
    if (!bytes || size == 0) return false;

    IWICImagingFactory* factory = nullptr;
    IWICStream* stream = nullptr;
    IWICBitmapDecoder* decoder = nullptr;
    IWICBitmapFrameDecode* frame = nullptr;
    IWICFormatConverter* converter = nullptr;
    bool ok = SUCCEEDED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                         IID_PPV_ARGS(&factory))) &&
              SUCCEEDED(factory->CreateStream(&stream)) &&
              SUCCEEDED(stream->InitializeFromMemory(static_cast<BYTE*>(const_cast<void*>(bytes)), size)) &&
              SUCCEEDED(factory->CreateDecoderFromStream(stream, nullptr, WICDecodeMetadataCacheOnDemand, &decoder)) &&
              SUCCEEDED(decoder->GetFrame(0, &frame)) &&
              SUCCEEDED(factory->CreateFormatConverter(&converter)) &&
              SUCCEEDED(converter->Initialize(frame, GUID_WICPixelFormat32bppRGBA, WICBitmapDitherTypeNone, nullptr,
                                              0.0, WICBitmapPaletteTypeCustom)) &&
              SUCCEEDED(converter->GetSize(&width, &height)) && width > 0 && height > 0;
    if (ok) {
        rgba.resize((size_t)width * height * 4);
        ok = SUCCEEDED(converter->CopyPixels(nullptr, width * 4, (UINT)rgba.size(), rgba.data()));
    }
    SafeRelease(converter);
    SafeRelease(frame);
    SafeRelease(decoder);
    SafeRelease(stream);
    SafeRelease(factory);
    return ok;
}

}  // namespace

ID3D11ShaderResourceView* CreateLogoTexture(ID3D11Device* device, ID3D11DeviceContext* context) {
    if (!device || !context) return nullptr;
    std::vector<unsigned char> rgba;
    UINT w = 0, h = 0;
    if (!DecodeLogo(rgba, w, h)) return nullptr;

    // Full mip chain, generated on the GPU: the header shows the 256 px artwork at roughly 32-48 px, and
    // without mips that minification aliases the frame and the glass edges.
    D3D11_TEXTURE2D_DESC desc = {};
    desc.Width = w;
    desc.Height = h;
    desc.MipLevels = 0;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    desc.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;
    ID3D11Texture2D* tex = nullptr;
    if (FAILED(device->CreateTexture2D(&desc, nullptr, &tex))) return nullptr;
    context->UpdateSubresource(tex, 0, nullptr, rgba.data(), w * 4, 0);
    ID3D11ShaderResourceView* srv = nullptr;
    D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = desc.Format;
    sd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    sd.Texture2D.MipLevels = (UINT)-1;
    if (SUCCEEDED(device->CreateShaderResourceView(tex, &sd, &srv))) context->GenerateMips(srv);
    tex->Release();
    return srv;
}

}  // namespace ui
