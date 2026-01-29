// DesktopLUT - capture.cpp
// Desktop duplication and capture

#include "capture.h"
#include "globals.h"
#include "render.h"
#include <iostream>
#include <iomanip>

// Forward declaration
void DetectHDRCapability(MonitorContext* ctx, IDXGIOutput* output);

bool InitDesktopDuplication(MonitorContext* ctx) {
    IDXGIDevice* dxgiDevice = nullptr;
    if (FAILED(g_device->QueryInterface(IID_PPV_ARGS(&dxgiDevice))) || !dxgiDevice) {
        std::cerr << "Failed to get DXGI device for duplication" << std::endl;
        return false;
    }

    IDXGIAdapter* adapter = nullptr;
    if (FAILED(dxgiDevice->GetAdapter(&adapter)) || !adapter) {
        std::cerr << "Failed to get adapter for duplication" << std::endl;
        dxgiDevice->Release();
        return false;
    }

    // Find the output that matches our target monitor
    IDXGIOutput* output = nullptr;
    IDXGIOutput5* output5 = nullptr;

    for (UINT i = 0; adapter->EnumOutputs(i, &output) != DXGI_ERROR_NOT_FOUND; i++) {
        DXGI_OUTPUT_DESC desc;
        output->GetDesc(&desc);

        if (desc.Monitor == ctx->monitor) {
            // Check HDR capability before duplication
            DetectHDRCapability(ctx, output);

            // Try to get IDXGIOutput5 for DuplicateOutput1 (HDR support)
            HRESULT hr = output->QueryInterface(IID_PPV_ARGS(&output5));
            if (FAILED(hr)) {
                // Fallback to IDXGIOutput1 for older systems
                std::cout << "Monitor " << ctx->index << ": IDXGIOutput5 not available, using legacy duplication" << std::endl;
                IDXGIOutput1* output1 = nullptr;
                hr = output->QueryInterface(IID_PPV_ARGS(&output1));
                output->Release();

                if (FAILED(hr)) {
                    std::cerr << "Failed to get IDXGIOutput1: 0x" << std::hex << hr << std::endl;
                    adapter->Release();
                    dxgiDevice->Release();
                    return false;
                }

                hr = output1->DuplicateOutput(g_device, &ctx->duplication);
                output1->Release();

                if (FAILED(hr)) {
                    std::cerr << "DuplicateOutput failed for monitor " << ctx->index << ": 0x" << std::hex << hr << std::endl;
                    adapter->Release();
                    dxgiDevice->Release();
                    return false;
                }

                ctx->captureFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
                ctx->isHDREnabled = false;
                adapter->Release();
                dxgiDevice->Release();
                goto done;
            }
            output->Release();
            break;
        }
        output->Release();
        output = nullptr;
    }

    adapter->Release();
    dxgiDevice->Release();

    if (!output5) {
        std::cerr << "Failed to find output for monitor " << ctx->index << std::endl;
        return false;
    }

    {
        // Use DuplicateOutput1 with supported formats list
        // Order matters: prefer HDR format if Windows HDR is enabled
        DXGI_FORMAT supportedFormats[] = {
            DXGI_FORMAT_R16G16B16A16_FLOAT,  // HDR (preferred when HDR enabled)
            DXGI_FORMAT_R10G10B10A2_UNORM,   // 10-bit fallback
            DXGI_FORMAT_B8G8R8A8_UNORM       // SDR fallback
        };

        HRESULT hr = output5->DuplicateOutput1(g_device, 0,
            ARRAYSIZE(supportedFormats), supportedFormats, &ctx->duplication);
        output5->Release();

        if (FAILED(hr)) {
            if (hr == E_ACCESSDENIED) {
                std::cerr << "Monitor " << ctx->index << ": Desktop duplication access denied - running as admin may help" << std::endl;
            } else if (hr == DXGI_ERROR_UNSUPPORTED) {
                std::cerr << "Monitor " << ctx->index << ": Desktop duplication not supported on this system" << std::endl;
            } else if (hr == DXGI_ERROR_SESSION_DISCONNECTED) {
                std::cerr << "Monitor " << ctx->index << ": Desktop duplication failed - session disconnected" << std::endl;
            } else {
                std::cerr << "Monitor " << ctx->index << ": DuplicateOutput1 failed: 0x" << std::hex << hr << std::endl;
            }
            return false;
        }
    }

done:
    DXGI_OUTDUPL_DESC duplDesc;
    ctx->duplication->GetDesc(&duplDesc);

    // Store actual capture format and determine HDR state
    ctx->captureFormat = duplDesc.ModeDesc.Format;
    ctx->isHDREnabled = (ctx->captureFormat == DXGI_FORMAT_R16G16B16A16_FLOAT);

    // Calculate frame time from refresh rate (with 5ms margin for timing tolerance)
    if (duplDesc.ModeDesc.RefreshRate.Numerator > 0) {
        double frameTimeExact = 1000.0 * duplDesc.ModeDesc.RefreshRate.Denominator / duplDesc.ModeDesc.RefreshRate.Numerator;
        ctx->frameTimeMs = static_cast<UINT>(frameTimeExact + 5.0);  // Add 5ms margin
    } else {
        ctx->frameTimeMs = 20;  // Fallback for unknown refresh rate
    }

    const char* formatName = "Unknown";
    switch (duplDesc.ModeDesc.Format) {
        case DXGI_FORMAT_B8G8R8A8_UNORM: formatName = "B8G8R8A8_UNORM (8-bit SDR)"; break;
        case DXGI_FORMAT_R10G10B10A2_UNORM: formatName = "R10G10B10A2_UNORM (10-bit SDR)"; break;
        case DXGI_FORMAT_R16G16B16A16_FLOAT: formatName = "R16G16B16A16_FLOAT (FP16 scRGB HDR)"; break;
    }

    double refreshRate = duplDesc.ModeDesc.RefreshRate.Denominator > 0
        ? static_cast<double>(duplDesc.ModeDesc.RefreshRate.Numerator) / duplDesc.ModeDesc.RefreshRate.Denominator
        : 0;
    std::cout << "Monitor " << ctx->index << " Desktop Duplication: " << duplDesc.ModeDesc.Width << "x"
              << duplDesc.ModeDesc.Height << " @ " << std::fixed << std::setprecision(3) << refreshRate
              << "Hz (frame timeout: " << ctx->frameTimeMs << "ms)" << std::endl;
    std::cout << "  Capture format: " << formatName << std::endl;
    std::cout << "  HDR mode: " << (ctx->isHDREnabled ? "ENABLED" : "disabled") << std::endl;

    return true;
}

bool ReinitDesktopDuplication(MonitorContext* ctx) {
    if (ctx->duplication) {
        ctx->duplication->Release();
        ctx->duplication = nullptr;
    }

    bool wasHDR = ctx->isHDREnabled;

    if (!InitDesktopDuplication(ctx)) {
        return false;
    }

    // Check if HDR state changed and trigger swapchain recreation
    if (ctx->isHDREnabled != wasHDR) {
        // Passthrough if no applicable LUT for current mode:
        // - SDR mode: need SDR LUT (SDR LUTs expect sRGB input)
        // - HDR mode: need HDR LUT (HDR LUTs expect PQ Rec.2020 input)
        // No fallback - SDR and HDR LUTs are incompatible
        bool hasApplicableLUT = ctx->isHDREnabled
            ? (ctx->lutSRV_HDR != nullptr)
            : (ctx->lutSRV_SDR != nullptr);
        ctx->usePassthrough = !hasApplicableLUT;
        RecreateSwapchain(ctx);
    }

    return true;
}

void DetectHDRCapability(MonitorContext* ctx, IDXGIOutput* output) {
    IDXGIOutput6* output6 = nullptr;
    HRESULT hr = output->QueryInterface(IID_PPV_ARGS(&output6));
    if (FAILED(hr)) {
        return;
    }

    DXGI_OUTPUT_DESC1 desc1;
    hr = output6->GetDesc1(&desc1);
    output6->Release();

    if (FAILED(hr)) {
        return;
    }

    ctx->isHDRCapable = (desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
    ctx->maxDisplayNits = desc1.MaxLuminance;

    std::cout << "Monitor " << ctx->index << " capabilities:" << std::endl;
    std::cout << "  Color space: " << (ctx->isHDRCapable ? "HDR (BT.2020 PQ)" : "SDR (sRGB)") << std::endl;
    std::cout << "  Max luminance: " << desc1.MaxLuminance << " nits" << std::endl;
    std::cout << "  Max full-frame: " << desc1.MaxFullFrameLuminance << " nits" << std::endl;
    std::cout << "  Min luminance: " << desc1.MinLuminance << " nits" << std::endl;
}
