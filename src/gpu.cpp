// DesktopLUT - gpu.cpp
// D3D device management and GPU resources

#include "gpu.h"
#include "globals.h"
#include "shader.h"
#include "lut.h"
#include "capture.h"
#include "render.h"
#include "processing.h"
#include <d3dcompiler.h>
#include <iostream>

#pragma comment(lib, "d3dcompiler.lib")

bool InitD3D() {
    D3D_FEATURE_LEVEL featureLevel;
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
#ifdef _DEBUG
    flags |= D3D11_CREATE_DEVICE_DEBUG;
#endif

    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
        nullptr, 0, D3D11_SDK_VERSION, &g_device, &featureLevel, &g_context);
    if (FAILED(hr)) {
        std::cerr << "D3D11CreateDevice failed: 0x" << std::hex << hr << std::endl;
        return false;
    }

    // Compile shaders
    ID3DBlob* vsBlob = nullptr;
    ID3DBlob* psBlob = nullptr;
    ID3DBlob* errorBlob = nullptr;

    hr = D3DCompile(g_vsSource, strlen(g_vsSource), "VS", nullptr, nullptr,
        "main", "vs_5_0", 0, 0, &vsBlob, &errorBlob);
    if (FAILED(hr)) {
        if (errorBlob) {
            std::cerr << "VS Error: " << (char*)errorBlob->GetBufferPointer() << std::endl;
            errorBlob->Release();
        }
        return false;
    }
    if (errorBlob) { errorBlob->Release(); errorBlob = nullptr; }  // May contain warnings

    hr = D3DCompile(g_psSource, strlen(g_psSource), "PS", nullptr, nullptr,
        "main", "ps_5_0", 0, 0, &psBlob, &errorBlob);
    if (FAILED(hr)) {
        if (errorBlob) {
            std::cerr << "PS Error: " << (char*)errorBlob->GetBufferPointer() << std::endl;
            errorBlob->Release();
        }
        vsBlob->Release();  // Clean up VS blob before returning
        return false;
    }
    if (errorBlob) { errorBlob->Release(); errorBlob = nullptr; }  // May contain warnings

    hr = g_device->CreateVertexShader(vsBlob->GetBufferPointer(), vsBlob->GetBufferSize(), nullptr, &g_vs);
    if (FAILED(hr)) {
        std::cerr << "Failed to create vertex shader: 0x" << std::hex << hr << std::dec << std::endl;
        vsBlob->Release();
        psBlob->Release();
        return false;
    }
    hr = g_device->CreatePixelShader(psBlob->GetBufferPointer(), psBlob->GetBufferSize(), nullptr, &g_ps);
    if (FAILED(hr)) {
        std::cerr << "Failed to create pixel shader: 0x" << std::hex << hr << std::dec << std::endl;
        vsBlob->Release();
        psBlob->Release();
        return false;
    }
    vsBlob->Release();
    psBlob->Release();

    // Compile compute shader for dynamic peak detection
    ID3DBlob* csBlob = nullptr;
    hr = D3DCompile(g_csSource, strlen(g_csSource), "CS", nullptr, nullptr,
        "main", "cs_5_0", 0, 0, &csBlob, &errorBlob);
    if (FAILED(hr)) {
        if (errorBlob) {
            std::cerr << "CS Error: " << (char*)errorBlob->GetBufferPointer() << std::endl;
            errorBlob->Release();
        }
        // Non-fatal: dynamic peak detection just won't work
        std::cerr << "Warning: Compute shader compilation failed, dynamic peak detection disabled" << std::endl;
    } else {
        if (errorBlob) { errorBlob->Release(); errorBlob = nullptr; }  // May contain warnings
        hr = g_device->CreateComputeShader(csBlob->GetBufferPointer(), csBlob->GetBufferSize(), nullptr, &g_peakDetectCS);
        csBlob->Release();
        if (FAILED(hr)) {
            std::cerr << "Failed to create peak detection compute shader: 0x" << std::hex << hr << std::dec << std::endl;
            g_peakDetectCS = nullptr;
        }

        // Create constant buffer for peak detection parameters (only if shader succeeded)
        if (g_peakDetectCS) {
            D3D11_BUFFER_DESC peakCbDesc = {};
            peakCbDesc.ByteWidth = 32;  // 8 floats: width, height, riseRate, fallRate, maxRise, maxFall, pad, pad
            peakCbDesc.Usage = D3D11_USAGE_DYNAMIC;
            peakCbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
            peakCbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
            hr = g_device->CreateBuffer(&peakCbDesc, nullptr, &g_peakCB);
            if (FAILED(hr)) {
                std::cerr << "Failed to create peak CB: 0x" << std::hex << hr << std::endl;
                g_peakDetectCS->Release();
                g_peakDetectCS = nullptr;
            }
        }
    }

    // Compile analysis compute shader
    ID3DBlob* analysisBlob = nullptr;
    hr = D3DCompile(g_analysisCSSource, strlen(g_analysisCSSource), "AnalysisCS", nullptr, nullptr,
        "main", "cs_5_0", 0, 0, &analysisBlob, &errorBlob);
    if (FAILED(hr)) {
        if (errorBlob) {
            std::cerr << "Analysis CS Error: " << (char*)errorBlob->GetBufferPointer() << std::endl;
            errorBlob->Release();
        }
        std::cerr << "Warning: Analysis compute shader compilation failed, frame analysis disabled" << std::endl;
    } else {
        if (errorBlob) { errorBlob->Release(); errorBlob = nullptr; }  // May contain warnings
        hr = g_device->CreateComputeShader(analysisBlob->GetBufferPointer(), analysisBlob->GetBufferSize(), nullptr, &g_analysisCS);
        analysisBlob->Release();
        if (FAILED(hr)) {
            std::cerr << "Failed to create analysis compute shader: 0x" << std::hex << hr << std::dec << std::endl;
            g_analysisCS = nullptr;
        }

        // Create constant buffer for analysis parameters (only if shader succeeded)
        if (g_analysisCS) {
            D3D11_BUFFER_DESC analysisCbDesc = {};
            analysisCbDesc.ByteWidth = 16;  // 4 uints: width, height, isHDR, pad
            analysisCbDesc.Usage = D3D11_USAGE_DYNAMIC;
            analysisCbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
            analysisCbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
            hr = g_device->CreateBuffer(&analysisCbDesc, nullptr, &g_analysisCB);
            if (FAILED(hr)) {
                std::cerr << "Failed to create analysis CB: 0x" << std::hex << hr << std::endl;
                g_analysisCS->Release();
                g_analysisCS = nullptr;
            } else {
                std::cout << "Analysis compute shader: enabled" << std::endl;
            }
        }
    }

    // Create samplers
    D3D11_SAMPLER_DESC sdPoint = {};
    sdPoint.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sdPoint.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sdPoint.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sdPoint.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    hr = g_device->CreateSamplerState(&sdPoint, &g_samplerPoint);
    if (FAILED(hr)) {
        std::cerr << "Failed to create point sampler: 0x" << std::hex << hr << std::dec << std::endl;
        return false;
    }

    D3D11_SAMPLER_DESC sdLinear = {};
    sdLinear.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sdLinear.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sdLinear.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sdLinear.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    hr = g_device->CreateSamplerState(&sdLinear, &g_samplerLinear);
    if (FAILED(hr)) {
        std::cerr << "Failed to create linear sampler: 0x" << std::hex << hr << std::dec << std::endl;
        return false;
    }

    // Wrap sampler for blue noise tiling
    D3D11_SAMPLER_DESC sdWrap = {};
    sdWrap.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sdWrap.AddressU = D3D11_TEXTURE_ADDRESS_WRAP;
    sdWrap.AddressV = D3D11_TEXTURE_ADDRESS_WRAP;
    sdWrap.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
    hr = g_device->CreateSamplerState(&sdWrap, &g_samplerWrap);
    if (FAILED(hr)) {
        std::cerr << "Failed to create wrap sampler: 0x" << std::hex << hr << std::dec << std::endl;
        return false;
    }

    // Create constant buffer for shader parameters
    D3D11_BUFFER_DESC cbDesc = {};
    cbDesc.ByteWidth = 256;  // 64 floats (16 float4s) - includes grayscale peak
    cbDesc.Usage = D3D11_USAGE_DYNAMIC;
    cbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = g_device->CreateBuffer(&cbDesc, nullptr, &g_constantBuffer);
    if (FAILED(hr)) {
        std::cerr << "Failed to create constant buffer: 0x" << std::hex << hr << std::endl;
        return false;
    }

    // Create blue noise texture for SDR dithering
    D3D11_TEXTURE2D_DESC noiseDesc = {};
    noiseDesc.Width = 64;
    noiseDesc.Height = 64;
    noiseDesc.MipLevels = 1;
    noiseDesc.ArraySize = 1;
    noiseDesc.Format = DXGI_FORMAT_R8_UNORM;
    noiseDesc.SampleDesc.Count = 1;
    noiseDesc.Usage = D3D11_USAGE_IMMUTABLE;
    noiseDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

    D3D11_SUBRESOURCE_DATA noiseData = {};
    noiseData.pSysMem = g_blueNoiseData;
    noiseData.SysMemPitch = 64;

    hr = g_device->CreateTexture2D(&noiseDesc, &noiseData, &g_blueNoiseTexture);
    if (FAILED(hr)) {
        std::cerr << "Failed to create blue noise texture: 0x" << std::hex << hr << std::endl;
        return false;
    }

    hr = g_device->CreateShaderResourceView(g_blueNoiseTexture, nullptr, &g_blueNoiseSRV);
    if (FAILED(hr)) {
        std::cerr << "Failed to create blue noise SRV: 0x" << std::hex << hr << std::endl;
        return false;
    }

    std::cout << "Blue noise dithering: enabled (64x64 texture)" << std::endl;

    return true;
}

bool CheckTearingSupport() {
    IDXGIDevice* dxgiDevice = nullptr;
    if (FAILED(g_device->QueryInterface(IID_PPV_ARGS(&dxgiDevice))) || !dxgiDevice) {
        std::cerr << "Failed to get DXGI device for tearing check" << std::endl;
        return false;
    }

    IDXGIAdapter* adapter = nullptr;
    if (FAILED(dxgiDevice->GetAdapter(&adapter)) || !adapter) {
        std::cerr << "Failed to get adapter for tearing check" << std::endl;
        dxgiDevice->Release();
        return false;
    }

    IDXGIFactory5* factory = nullptr;
    if (FAILED(adapter->GetParent(IID_PPV_ARGS(&factory))) || !factory) {
        std::cerr << "Failed to get factory for tearing check" << std::endl;
        adapter->Release();
        dxgiDevice->Release();
        return false;
    }

    BOOL allowTearing = FALSE;
    if (SUCCEEDED(factory->CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING, &allowTearing, sizeof(allowTearing)))) {
        g_tearingSupported = (allowTearing == TRUE);
    }

    factory->Release();
    adapter->Release();
    dxgiDevice->Release();

    std::cout << "Tearing support: " << (g_tearingSupported ? "yes" : "no") << std::endl;
    return g_tearingSupported;
}

void ReleaseMonitorD3DResources(MonitorContext* ctx) {
    if (ctx->duplication) { ctx->duplication->Release(); ctx->duplication = nullptr; }
    if (ctx->dcompVisual) { ctx->dcompVisual->Release(); ctx->dcompVisual = nullptr; }
    if (ctx->dcompTarget) { ctx->dcompTarget->Release(); ctx->dcompTarget = nullptr; }
    if (ctx->captureSRV) { ctx->captureSRV->Release(); ctx->captureSRV = nullptr; }
    if (ctx->lutSRV_SDR) { ctx->lutSRV_SDR->Release(); ctx->lutSRV_SDR = nullptr; }
    if (ctx->lutTextureSDR) { ctx->lutTextureSDR->Release(); ctx->lutTextureSDR = nullptr; }
    if (ctx->lutSRV_HDR) { ctx->lutSRV_HDR->Release(); ctx->lutSRV_HDR = nullptr; }
    if (ctx->lutTextureHDR) { ctx->lutTextureHDR->Release(); ctx->lutTextureHDR = nullptr; }
    if (ctx->peakSRV) { ctx->peakSRV->Release(); ctx->peakSRV = nullptr; }
    if (ctx->peakUAV) { ctx->peakUAV->Release(); ctx->peakUAV = nullptr; }
    if (ctx->peakTexture) { ctx->peakTexture->Release(); ctx->peakTexture = nullptr; }
    if (ctx->peakStagingTexture) { ctx->peakStagingTexture->Release(); ctx->peakStagingTexture = nullptr; }
    // Analysis resources
    if (ctx->analysisUAV) { ctx->analysisUAV->Release(); ctx->analysisUAV = nullptr; }
    if (ctx->analysisBuffer) { ctx->analysisBuffer->Release(); ctx->analysisBuffer = nullptr; }
    for (int i = 0; i < 2; i++) {
        if (ctx->analysisStagingBuffer[i]) {
            ctx->analysisStagingBuffer[i]->Release();
            ctx->analysisStagingBuffer[i] = nullptr;
        }
    }
    if (ctx->rtv) { ctx->rtv->Release(); ctx->rtv = nullptr; }
    if (ctx->swapchain) { ctx->swapchain->Release(); ctx->swapchain = nullptr; }
    // Keep hwnd - we'll reuse it
}

void ReleaseSharedD3DResources() {
    if (g_dcompDevice) { g_dcompDevice->Release(); g_dcompDevice = nullptr; }
    if (g_blueNoiseSRV) { g_blueNoiseSRV->Release(); g_blueNoiseSRV = nullptr; }
    if (g_blueNoiseTexture) { g_blueNoiseTexture->Release(); g_blueNoiseTexture = nullptr; }
    if (g_constantBuffer) { g_constantBuffer->Release(); g_constantBuffer = nullptr; }
    if (g_samplerPoint) { g_samplerPoint->Release(); g_samplerPoint = nullptr; }
    if (g_samplerLinear) { g_samplerLinear->Release(); g_samplerLinear = nullptr; }
    if (g_samplerWrap) { g_samplerWrap->Release(); g_samplerWrap = nullptr; }
    if (g_peakDetectCS) { g_peakDetectCS->Release(); g_peakDetectCS = nullptr; }
    if (g_peakCB) { g_peakCB->Release(); g_peakCB = nullptr; }
    if (g_analysisCS) { g_analysisCS->Release(); g_analysisCS = nullptr; }
    if (g_analysisCB) { g_analysisCB->Release(); g_analysisCB = nullptr; }
    if (g_ps) { g_ps->Release(); g_ps = nullptr; }
    if (g_vs) { g_vs->Release(); g_vs = nullptr; }
    if (g_context) { g_context->Release(); g_context = nullptr; }
    if (g_device) { g_device->Release(); g_device = nullptr; }
}

bool AttemptDeviceRecovery() {
    std::cout << "Attempting GPU device recovery..." << std::endl;

    // Release all D3D resources
    for (auto& ctx : g_monitors) {
        ReleaseMonitorD3DResources(&ctx);
    }
    ReleaseSharedD3DResources();

    // Wait for driver to stabilize after TDR
    std::cout << "Waiting for driver to stabilize..." << std::endl;
    Sleep(2000);

    // Reinit D3D
    if (!InitD3D()) {
        std::cerr << "Failed to reinit D3D after TDR" << std::endl;
        return false;
    }
    std::cout << "D3D reinitialized" << std::endl;

    // Check tearing support again
    g_tearingSupported = CheckTearingSupport();

    // Reinit each monitor
    for (auto& ctx : g_monitors) {
        // Reload LUT data from stored paths
        std::vector<float> lutDataSDR, lutDataHDR;
        int lutSizeSDR = 0, lutSizeHDR = 0;

        if (!LoadLUT(ctx.sdrLutPath, lutDataSDR, lutSizeSDR)) {
            std::cerr << "Failed to reload SDR LUT for monitor " << ctx.index << std::endl;
            return false;
        }

        if (!ctx.hdrLutPath.empty()) {
            LoadLUT(ctx.hdrLutPath, lutDataHDR, lutSizeHDR);
        }

        // Recreate swapchain (window already exists)
        if (!CreateSwapChain(&ctx)) {
            std::cerr << "Failed to recreate swapchain for monitor " << ctx.index << std::endl;
            return false;
        }

        // Reinit DirectComposition
        if (!InitDirectComposition(&ctx)) {
            std::cerr << "Failed to reinit DirectComposition for monitor " << ctx.index << std::endl;
            return false;
        }

        // Recreate LUT textures
        if (!CreateLUTTexture(lutDataSDR, lutSizeSDR, &ctx.lutTextureSDR, &ctx.lutSRV_SDR)) {
            std::cerr << "Failed to recreate SDR LUT texture for monitor " << ctx.index << std::endl;
            return false;
        }
        ctx.lutSizeSDR = lutSizeSDR;

        if (!lutDataHDR.empty()) {
            if (!CreateLUTTexture(lutDataHDR, lutSizeHDR, &ctx.lutTextureHDR, &ctx.lutSRV_HDR)) {
                std::cerr << "Failed to recreate HDR LUT texture for monitor " << ctx.index << std::endl;
                return false;
            }
            ctx.lutSizeHDR = lutSizeHDR;
        }

        // Reinit desktop duplication
        if (!InitDesktopDuplication(&ctx)) {
            std::cerr << "Failed to reinit desktop duplication for monitor " << ctx.index << std::endl;
            return false;
        }

        ctx.enabled = true;
        ctx.consecutiveFailures = 0;
        std::cout << "Monitor " << ctx.index << " recovered" << std::endl;
    }

    // Reapply MaxTML settings (may be lost after TDR/driver recovery)
    ApplyMaxTmlSettings();

    std::cout << "GPU device recovery successful" << std::endl;
    return true;
}
