// DesktopLUT - gpu.cpp
// D3D device management and GPU resources

#include "gpu.h"
#include "globals.h"
#include "shader.h"
#include "lut.h"
#include "capture.h"
#include "render.h"
#include "processing.h"
#include "mhc.h"
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
    cbDesc.ByteWidth = 544;  // 136 floats (34 float4s) - grayscaleR/G/B[8] + motion bar
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

    // Create desktop gamma LUT (precomputed sRGB→2.2 correction)
    // f(L) = (sRGB_OETF(L))^2.2 for L in [0,1]
    // Replaces 6 pow() per pixel with 3 texture samples
    {
        const int DG_LUT_SIZE = 1024;
        float dgData[DG_LUT_SIZE];
        for (int i = 0; i < DG_LUT_SIZE; i++) {
            float L = static_cast<float>(i) / static_cast<float>(DG_LUT_SIZE - 1);
            // sRGB OETF: linear → encoded signal
            float srgb;
            if (L <= 0.0031308f)
                srgb = 12.92f * L;
            else
                srgb = 1.055f * powf(L, 1.0f / 2.4f) - 0.055f;
            // Decode with 2.2 power law
            dgData[i] = powf(std::max(srgb, 0.0f), 2.2f);
        }

        D3D11_TEXTURE2D_DESC dgDesc = {};
        dgDesc.Width = DG_LUT_SIZE;
        dgDesc.Height = 1;
        dgDesc.MipLevels = 1;
        dgDesc.ArraySize = 1;
        dgDesc.Format = DXGI_FORMAT_R32_FLOAT;
        dgDesc.SampleDesc.Count = 1;
        dgDesc.Usage = D3D11_USAGE_IMMUTABLE;
        dgDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

        D3D11_SUBRESOURCE_DATA dgInitData = {};
        dgInitData.pSysMem = dgData;
        dgInitData.SysMemPitch = DG_LUT_SIZE * sizeof(float);

        hr = g_device->CreateTexture2D(&dgDesc, &dgInitData, &g_desktopGammaTexture);
        if (FAILED(hr)) {
            std::cerr << "Failed to create desktop gamma LUT texture: 0x" << std::hex << hr << std::dec << std::endl;
            return false;
        }

        hr = g_device->CreateShaderResourceView(g_desktopGammaTexture, nullptr, &g_desktopGammaSRV);
        if (FAILED(hr)) {
            std::cerr << "Failed to create desktop gamma LUT SRV: 0x" << std::hex << hr << std::dec << std::endl;
            g_desktopGammaTexture->Release();
            g_desktopGammaTexture = nullptr;
            return false;
        }

        std::cout << "Desktop gamma LUT: enabled (1024-entry sRGB->2.2)" << std::endl;
    }

    // Create PQ transfer function LUTs (replaces all pow() in HDR pixel shader)
    {
        const int PQ_LUT_SIZE = 4096;

        // PQ constants (ST.2084)
        const float m1 = 0.1593017578125f;
        const float m2 = 78.84375f;
        const float c1 = 0.8359375f;
        const float c2 = 18.8515625f;
        const float c3 = 18.6875f;

        // PQ OETF LUT: Linear [0,1] → PQ [0,1], sqrt-domain sampling for shadow precision
        // Entry i maps to L = (i/(N-1))^2, stores PQ(L)
        float oetfData[PQ_LUT_SIZE];
        for (int i = 0; i < PQ_LUT_SIZE; i++) {
            float t = static_cast<float>(i) / static_cast<float>(PQ_LUT_SIZE - 1);
            float L = t * t;  // sqrt-domain: L = t^2, so shader does t = sqrt(L)
            float Y = std::max(L, 1e-12f);
            float Ym = powf(Y, m1);
            oetfData[i] = powf((c1 + c2 * Ym) / (1.0f + c3 * Ym), m2);
        }

        // PQ EOTF LUT: PQ [0,1] → Linear [0,1], uniform sampling (PQ is perceptually uniform)
        float eotfData[PQ_LUT_SIZE];
        for (int i = 0; i < PQ_LUT_SIZE; i++) {
            float pq = static_cast<float>(i) / static_cast<float>(PQ_LUT_SIZE - 1);
            float Vm = powf(std::max(pq, 1e-12f), 1.0f / m2);
            float t = std::max(Vm - c1, 0.0f) / std::max(c2 - c3 * Vm, 1e-12f);
            eotfData[i] = powf(t, 1.0f / m1);
        }

        D3D11_TEXTURE2D_DESC pqDesc = {};
        pqDesc.Width = PQ_LUT_SIZE;
        pqDesc.Height = 1;
        pqDesc.MipLevels = 1;
        pqDesc.ArraySize = 1;
        pqDesc.Format = DXGI_FORMAT_R32_FLOAT;
        pqDesc.SampleDesc.Count = 1;
        pqDesc.Usage = D3D11_USAGE_IMMUTABLE;
        pqDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

        D3D11_SUBRESOURCE_DATA oetfInitData = {};
        oetfInitData.pSysMem = oetfData;
        oetfInitData.SysMemPitch = PQ_LUT_SIZE * sizeof(float);

        hr = g_device->CreateTexture2D(&pqDesc, &oetfInitData, &g_pqOetfTexture);
        if (FAILED(hr)) {
            std::cerr << "Failed to create PQ OETF LUT: 0x" << std::hex << hr << std::dec << std::endl;
            return false;
        }
        hr = g_device->CreateShaderResourceView(g_pqOetfTexture, nullptr, &g_pqOetfSRV);
        if (FAILED(hr)) {
            std::cerr << "Failed to create PQ OETF SRV: 0x" << std::hex << hr << std::dec << std::endl;
            g_pqOetfTexture->Release(); g_pqOetfTexture = nullptr;
            return false;
        }

        D3D11_SUBRESOURCE_DATA eotfInitData = {};
        eotfInitData.pSysMem = eotfData;
        eotfInitData.SysMemPitch = PQ_LUT_SIZE * sizeof(float);

        hr = g_device->CreateTexture2D(&pqDesc, &eotfInitData, &g_pqEotfTexture);
        if (FAILED(hr)) {
            std::cerr << "Failed to create PQ EOTF LUT: 0x" << std::hex << hr << std::dec << std::endl;
            return false;
        }
        hr = g_device->CreateShaderResourceView(g_pqEotfTexture, nullptr, &g_pqEotfSRV);
        if (FAILED(hr)) {
            std::cerr << "Failed to create PQ EOTF SRV: 0x" << std::hex << hr << std::dec << std::endl;
            g_pqEotfTexture->Release(); g_pqEotfTexture = nullptr;
            return false;
        }

        std::cout << "PQ transfer LUTs: enabled (4096-entry OETF sqrt-domain + EOTF uniform)" << std::endl;
    }

    // Create sRGB transfer function LUTs (replaces pow() in SDR pixel shader)
    {
        const int SRGB_LUT_SIZE = 1024;

        // sRGB OETF LUT: Linear [0,1] → sRGB encoded [0,1]
        float oetfData[SRGB_LUT_SIZE];
        for (int i = 0; i < SRGB_LUT_SIZE; i++) {
            float L = static_cast<float>(i) / static_cast<float>(SRGB_LUT_SIZE - 1);
            if (L <= 0.0031308f)
                oetfData[i] = 12.92f * L;
            else
                oetfData[i] = 1.055f * powf(L, 1.0f / 2.4f) - 0.055f;
        }

        // sRGB EOTF LUT: sRGB encoded [0,1] → Linear [0,1]
        float eotfData[SRGB_LUT_SIZE];
        for (int i = 0; i < SRGB_LUT_SIZE; i++) {
            float V = static_cast<float>(i) / static_cast<float>(SRGB_LUT_SIZE - 1);
            if (V <= 0.04045f)
                eotfData[i] = V / 12.92f;
            else
                eotfData[i] = powf((V + 0.055f) / 1.055f, 2.4f);
        }

        // Gamma 2.4/2.2 ratio LUT: stores pow(Y, 1/11) for Y in [0,1]
        // Apply24GammaLinear needs ratio = pow(Y, 12/11) / Y = pow(Y, 1/11)
        float gammaRatioData[SRGB_LUT_SIZE];
        for (int i = 0; i < SRGB_LUT_SIZE; i++) {
            float Y = static_cast<float>(i) / static_cast<float>(SRGB_LUT_SIZE - 1);
            if (Y < 1e-6f)
                gammaRatioData[i] = 1.0f;  // Avoid 0^(1/11) = 0 → ratio = 0/0
            else
                gammaRatioData[i] = powf(Y, 1.0f / 11.0f);  // pow(Y, 1/11) = pow(Y,12/11)/Y
        }

        // White balance gamma LUT: stores pow(gain, 1/2.2) for gain in [0,2]
        // Domain [0,2] covers typical WB gains; UV = gain/2
        const int WB_LUT_SIZE = 512;
        float wbGammaData[WB_LUT_SIZE];
        for (int i = 0; i < WB_LUT_SIZE; i++) {
            float gain = static_cast<float>(i) / static_cast<float>(WB_LUT_SIZE - 1) * 2.0f;
            wbGammaData[i] = powf(std::max(gain, 0.001f), 1.0f / 2.2f);
        }

        D3D11_TEXTURE2D_DESC srgbDesc = {};
        srgbDesc.Width = SRGB_LUT_SIZE;
        srgbDesc.Height = 1;
        srgbDesc.MipLevels = 1;
        srgbDesc.ArraySize = 1;
        srgbDesc.Format = DXGI_FORMAT_R32_FLOAT;
        srgbDesc.SampleDesc.Count = 1;
        srgbDesc.Usage = D3D11_USAGE_IMMUTABLE;
        srgbDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;

        // sRGB OETF
        D3D11_SUBRESOURCE_DATA initData = {};
        initData.pSysMem = oetfData;
        initData.SysMemPitch = SRGB_LUT_SIZE * sizeof(float);
        hr = g_device->CreateTexture2D(&srgbDesc, &initData, &g_srgbOetfTexture);
        if (FAILED(hr)) { std::cerr << "Failed to create sRGB OETF LUT\n"; return false; }
        hr = g_device->CreateShaderResourceView(g_srgbOetfTexture, nullptr, &g_srgbOetfSRV);
        if (FAILED(hr)) { g_srgbOetfTexture->Release(); g_srgbOetfTexture = nullptr; return false; }

        // sRGB EOTF
        initData.pSysMem = eotfData;
        hr = g_device->CreateTexture2D(&srgbDesc, &initData, &g_srgbEotfTexture);
        if (FAILED(hr)) { std::cerr << "Failed to create sRGB EOTF LUT\n"; return false; }
        hr = g_device->CreateShaderResourceView(g_srgbEotfTexture, nullptr, &g_srgbEotfSRV);
        if (FAILED(hr)) { g_srgbEotfTexture->Release(); g_srgbEotfTexture = nullptr; return false; }

        // Gamma 2.4/2.2 ratio
        initData.pSysMem = gammaRatioData;
        hr = g_device->CreateTexture2D(&srgbDesc, &initData, &g_gammaRatioTexture);
        if (FAILED(hr)) { std::cerr << "Failed to create gamma ratio LUT\n"; return false; }
        hr = g_device->CreateShaderResourceView(g_gammaRatioTexture, nullptr, &g_gammaRatioSRV);
        if (FAILED(hr)) { g_gammaRatioTexture->Release(); g_gammaRatioTexture = nullptr; return false; }

        // White balance gamma
        D3D11_TEXTURE2D_DESC wbDesc = srgbDesc;
        wbDesc.Width = WB_LUT_SIZE;
        initData.pSysMem = wbGammaData;
        initData.SysMemPitch = WB_LUT_SIZE * sizeof(float);
        hr = g_device->CreateTexture2D(&wbDesc, &initData, &g_wbGammaTexture);
        if (FAILED(hr)) { std::cerr << "Failed to create WB gamma LUT\n"; return false; }
        hr = g_device->CreateShaderResourceView(g_wbGammaTexture, nullptr, &g_wbGammaSRV);
        if (FAILED(hr)) { g_wbGammaTexture->Release(); g_wbGammaTexture = nullptr; return false; }

        std::cout << "SDR transfer LUTs: enabled (sRGB OETF/EOTF + gamma ratio + WB gamma)" << std::endl;
    }

    return true;
}

bool InitD3DAnalysisOnly() {
    // Minimal D3D init for analysis-only mode: device, context, analysis compute shader, analysis CB.
    // No VS/PS, no samplers, no constant buffer, no textures, no DComp.
    D3D_FEATURE_LEVEL featureLevel;
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
#ifdef _DEBUG
    flags |= D3D11_CREATE_DEVICE_DEBUG;
#endif

    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
        nullptr, 0, D3D11_SDK_VERSION, &g_device, &featureLevel, &g_context);
    if (FAILED(hr)) {
        std::cerr << "InitD3DAnalysisOnly: D3D11CreateDevice failed: 0x" << std::hex << hr << std::endl;
        return false;
    }

    // Compile analysis compute shader
    ID3DBlob* analysisBlob = nullptr;
    ID3DBlob* errorBlob = nullptr;
    hr = D3DCompile(g_analysisCSSource, strlen(g_analysisCSSource), "AnalysisCS", nullptr, nullptr,
        "main", "cs_5_0", 0, 0, &analysisBlob, &errorBlob);
    if (FAILED(hr)) {
        if (errorBlob) {
            std::cerr << "Analysis CS Error: " << (char*)errorBlob->GetBufferPointer() << std::endl;
            errorBlob->Release();
        }
        std::cerr << "InitD3DAnalysisOnly: Analysis compute shader compilation failed" << std::endl;
        return false;
    }
    if (errorBlob) { errorBlob->Release(); errorBlob = nullptr; }
    hr = g_device->CreateComputeShader(analysisBlob->GetBufferPointer(), analysisBlob->GetBufferSize(), nullptr, &g_analysisCS);
    analysisBlob->Release();
    if (FAILED(hr)) {
        std::cerr << "InitD3DAnalysisOnly: Failed to create analysis compute shader" << std::endl;
        return false;
    }

    // Create constant buffer for analysis parameters
    D3D11_BUFFER_DESC analysisCbDesc = {};
    analysisCbDesc.ByteWidth = 16;  // 4 uints: width, height, isHDR, pad
    analysisCbDesc.Usage = D3D11_USAGE_DYNAMIC;
    analysisCbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    analysisCbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    hr = g_device->CreateBuffer(&analysisCbDesc, nullptr, &g_analysisCB);
    if (FAILED(hr)) {
        std::cerr << "InitD3DAnalysisOnly: Failed to create analysis CB" << std::endl;
        g_analysisCS->Release();
        g_analysisCS = nullptr;
        return false;
    }

    std::cout << "InitD3DAnalysisOnly: device + analysis CS initialized" << std::endl;
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
    ctx->lastCaptureTexture = nullptr;  // Weak ref, just null it
    if (ctx->lutSRV_SDR) { ctx->lutSRV_SDR->Release(); ctx->lutSRV_SDR = nullptr; }
    if (ctx->lutTextureSDR) { ctx->lutTextureSDR->Release(); ctx->lutTextureSDR = nullptr; }
    if (ctx->lutSRV_HDR) { ctx->lutSRV_HDR->Release(); ctx->lutSRV_HDR = nullptr; }
    if (ctx->lutTextureHDR) { ctx->lutTextureHDR->Release(); ctx->lutTextureHDR = nullptr; }
    if (ctx->peakSRV) { ctx->peakSRV->Release(); ctx->peakSRV = nullptr; }
    if (ctx->peakUAV) { ctx->peakUAV->Release(); ctx->peakUAV = nullptr; }
    if (ctx->peakTexture) { ctx->peakTexture->Release(); ctx->peakTexture = nullptr; }
    if (ctx->peakStagingTexture) { ctx->peakStagingTexture->Release(); ctx->peakStagingTexture = nullptr; }
    if (ctx->peakStagingTexture2) { ctx->peakStagingTexture2->Release(); ctx->peakStagingTexture2 = nullptr; }
    // Frame buffer resources
    if (ctx->bufferRTV) { ctx->bufferRTV->Release(); ctx->bufferRTV = nullptr; }
    if (ctx->bufferTexture) { ctx->bufferTexture->Release(); ctx->bufferTexture = nullptr; }
    ctx->bufferReady = false;
    // Analysis resources
    if (ctx->analysisUAV) { ctx->analysisUAV->Release(); ctx->analysisUAV = nullptr; }
    if (ctx->analysisBuffer) { ctx->analysisBuffer->Release(); ctx->analysisBuffer = nullptr; }
    for (int i = 0; i < 2; i++) {
        if (ctx->analysisStagingBuffer[i]) {
            ctx->analysisStagingBuffer[i]->Release();
            ctx->analysisStagingBuffer[i] = nullptr;
        }
    }
    ctx->analysisStagingIndex = 0;
    ctx->analysisFrameCounter = 0;
    if (ctx->rtv) { ctx->rtv->Release(); ctx->rtv = nullptr; }
    if (ctx->swapchain) { ctx->swapchain->Release(); ctx->swapchain = nullptr; }
    // Keep hwnd - we'll reuse it
}

// Re-acquire a monitor's LUT textures from its persisted cube paths when a GPU-resource
// reinit dropped them. Idempotent: a no-op when the SRV is already valid or no path is
// configured, so it is safe (and cheap) to call on every duplication reinit — including
// the constant refresh-rate modesets video players trigger. Without this, a reinit that
// invalidates lutSRV_SDR/lutSRV_HDR leaves the overlay deriving usePassthrough from a null
// SRV → auto-sleep → the just-applied cube silently stops rendering until a full restart
// (see OVERLAY_LUT_RELOAD_BUG.md). Returns true if any LUT was reloaded.
bool EnsureMonitorLutTextures(MonitorContext* ctx) {
    bool reloaded = false;

    if (!ctx->sdrLutPath.empty() && ctx->lutSRV_SDR == nullptr) {
        std::vector<float> data;
        int size = 0;
        if (LoadLUT(ctx->sdrLutPath, data, size) && !data.empty()) {
            // Defensive: release any orphaned texture before recreating to avoid a leak
            // if the SRV/texture pair was left half-released.
            if (ctx->lutTextureSDR) { ctx->lutTextureSDR->Release(); ctx->lutTextureSDR = nullptr; }
            if (CreateLUTTexture(data, size, &ctx->lutTextureSDR, &ctx->lutSRV_SDR)) {
                ctx->lutSizeSDR = size;
                reloaded = true;
                std::wcout << L"[LUT] Reloaded SDR cube for monitor " << ctx->index
                           << L" after reinit: " << ctx->sdrLutPath << std::endl;
            } else {
                std::cerr << "[LUT] Failed to recreate SDR LUT texture for monitor "
                          << ctx->index << " after reinit" << std::endl;
            }
        } else {
            std::wcerr << L"[LUT] Failed to reload SDR cube for monitor " << ctx->index
                       << L": " << ctx->sdrLutPath << std::endl;
        }
    }

    if (!ctx->hdrLutPath.empty() && ctx->lutSRV_HDR == nullptr) {
        std::vector<float> data;
        int size = 0;
        if (LoadLUT(ctx->hdrLutPath, data, size) && !data.empty()) {
            if (ctx->lutTextureHDR) { ctx->lutTextureHDR->Release(); ctx->lutTextureHDR = nullptr; }
            if (CreateLUTTexture(data, size, &ctx->lutTextureHDR, &ctx->lutSRV_HDR)) {
                ctx->lutSizeHDR = size;
                reloaded = true;
                std::wcout << L"[LUT] Reloaded HDR cube for monitor " << ctx->index
                           << L" after reinit: " << ctx->hdrLutPath << std::endl;
            } else {
                std::cerr << "[LUT] Failed to recreate HDR LUT texture for monitor "
                          << ctx->index << " after reinit" << std::endl;
            }
        } else {
            std::wcerr << L"[LUT] Failed to reload HDR cube for monitor " << ctx->index
                       << L": " << ctx->hdrLutPath << std::endl;
        }
    }

    return reloaded;
}

void ReleaseSharedD3DResources() {
    if (g_dcompDevice) { g_dcompDevice->Release(); g_dcompDevice = nullptr; }
    if (g_blueNoiseSRV) { g_blueNoiseSRV->Release(); g_blueNoiseSRV = nullptr; }
    if (g_blueNoiseTexture) { g_blueNoiseTexture->Release(); g_blueNoiseTexture = nullptr; }
    if (g_desktopGammaSRV) { g_desktopGammaSRV->Release(); g_desktopGammaSRV = nullptr; }
    if (g_desktopGammaTexture) { g_desktopGammaTexture->Release(); g_desktopGammaTexture = nullptr; }
    if (g_pqOetfSRV) { g_pqOetfSRV->Release(); g_pqOetfSRV = nullptr; }
    if (g_pqOetfTexture) { g_pqOetfTexture->Release(); g_pqOetfTexture = nullptr; }
    if (g_pqEotfSRV) { g_pqEotfSRV->Release(); g_pqEotfSRV = nullptr; }
    if (g_pqEotfTexture) { g_pqEotfTexture->Release(); g_pqEotfTexture = nullptr; }
    if (g_srgbOetfSRV) { g_srgbOetfSRV->Release(); g_srgbOetfSRV = nullptr; }
    if (g_srgbOetfTexture) { g_srgbOetfTexture->Release(); g_srgbOetfTexture = nullptr; }
    if (g_srgbEotfSRV) { g_srgbEotfSRV->Release(); g_srgbEotfSRV = nullptr; }
    if (g_srgbEotfTexture) { g_srgbEotfTexture->Release(); g_srgbEotfTexture = nullptr; }
    if (g_gammaRatioSRV) { g_gammaRatioSRV->Release(); g_gammaRatioSRV = nullptr; }
    if (g_gammaRatioTexture) { g_gammaRatioTexture->Release(); g_gammaRatioTexture = nullptr; }
    if (g_wbGammaSRV) { g_wbGammaSRV->Release(); g_wbGammaSRV = nullptr; }
    if (g_wbGammaTexture) { g_wbGammaTexture->Release(); g_wbGammaTexture = nullptr; }
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

    // Recreate DirectComposition device (depends on D3D device)
    if (!InitDirectCompositionDevice()) {
        std::cerr << "Failed to reinit DirectComposition device after TDR" << std::endl;
        return false;
    }

    // Check tearing support again
    g_tearingSupported = CheckTearingSupport();

    // Reinit each monitor
    for (auto& ctx : g_monitors) {
        // Reload LUT data from stored paths
        std::vector<float> lutDataSDR, lutDataHDR;
        int lutSizeSDR = 0, lutSizeHDR = 0;

        if (!ctx.sdrLutPath.empty() && !LoadLUT(ctx.sdrLutPath, lutDataSDR, lutSizeSDR)) {
            std::cerr << "Failed to reload SDR LUT for monitor " << ctx.index << std::endl;
            return false;
        }

        if (!ctx.hdrLutPath.empty() && !LoadLUT(ctx.hdrLutPath, lutDataHDR, lutSizeHDR)) {
            std::cerr << "Failed to reload HDR LUT for monitor " << ctx.index << std::endl;
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

        // Recreate LUT textures (only when an SDR LUT is actually loaded — MHC-only,
        // HDR-LUT-only, and corrections-only setups have no SDR cube. CreateLUTTexture
        // with size 0 fails E_INVALIDARG, which would otherwise abort recovery and kill
        // the overlay after MAX_WATCHDOG_RECOVERY_ATTEMPTS. Mirror the startup gate.)
        if (!lutDataSDR.empty()) {
            if (!CreateLUTTexture(lutDataSDR, lutSizeSDR, &ctx.lutTextureSDR, &ctx.lutSRV_SDR)) {
                std::cerr << "Failed to recreate SDR LUT texture for monitor " << ctx.index << std::endl;
                return false;
            }
            ctx.lutSizeSDR = lutSizeSDR;
        }

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
    // Reapply MHC profiles (may be lost after TDR/driver recovery)
    ReapplyAllMhcProfiles();

    std::cout << "GPU device recovery successful" << std::endl;
    return true;
}
