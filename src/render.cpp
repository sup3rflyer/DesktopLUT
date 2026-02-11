// DesktopLUT - render.cpp
// Frame rendering, swapchain, and DirectComposition

#include "render.h"
#include "globals.h"
#include "gpu.h"
#include "color.h"
#include "capture.h"
#include "osd.h"
#include "analysis.h"
#include "displayconfig.h"
#include "processing.h"
#include "mhc.h"
#include <dwmapi.h>
#include <iostream>
#include <iomanip>
#include <algorithm>

// ============================================================================
// SECTION: Compositor Clock & Display Power
// ============================================================================

// Compositor Clock API (Windows 10 1903+) for VRR-aware frame timing
// Dynamically loaded to maintain compatibility with older Windows
PFN_DCompositionWaitForCompositorClock g_pfnWaitForCompositorClock = nullptr;

void InitCompositorClock() {
    HMODULE hDcomp = GetModuleHandleW(L"dcomp.dll");
    if (hDcomp) {
        g_pfnWaitForCompositorClock = (PFN_DCompositionWaitForCompositorClock)
            GetProcAddress(hDcomp, "DCompositionWaitForCompositorClock");
    }
    if (g_pfnWaitForCompositorClock) {
        std::cout << "Compositor Clock API: available" << std::endl;
    } else {
        std::cout << "Compositor Clock API: not available (using DwmFlush fallback)" << std::endl;
    }
}

// Motion bar time origin (set on first frame)
static std::chrono::steady_clock::time_point s_motionBarOrigin;
static bool s_motionBarOriginSet = false;

// Display power notification handle
static HPOWERNOTIFY g_displayPowerNotify = nullptr;
static std::chrono::steady_clock::time_point g_powerNotifyRegisteredTime;

// GUID_CONSOLE_DISPLAY_STATE - notifies when display goes on/off/dimmed
// {6FE69556-704A-47A0-8F24-C28D936FDA47}
static const GUID GUID_CONSOLE_DISPLAY_STATE_LOCAL =
    { 0x6fe69556, 0x704a, 0x47a0, { 0x8f, 0x24, 0xc2, 0x8d, 0x93, 0x6f, 0xda, 0x47 } };

void RegisterDisplayPowerNotification(HWND hwnd) {
    if (g_displayPowerNotify) return;  // Already registered

    g_displayPowerNotify = RegisterPowerSettingNotification(
        hwnd, &GUID_CONSOLE_DISPLAY_STATE_LOCAL, DEVICE_NOTIFY_WINDOW_HANDLE);

    if (g_displayPowerNotify) {
        g_powerNotifyRegisteredTime = std::chrono::steady_clock::now();
        std::cout << "Registered for display power state notifications" << std::endl;
    }
}

void UnregisterDisplayPowerNotification() {
    if (g_displayPowerNotify) {
        UnregisterPowerSettingNotification(g_displayPowerNotify);
        g_displayPowerNotify = nullptr;
    }
}

// ============================================================================
// SECTION: Peak Detection Resources
// ============================================================================

// Create peak detection resources for dynamic tonemapping
bool CreatePeakDetectionResources(MonitorContext* ctx) {
    if (!g_peakDetectCS || !g_peakCB) {
        return false;  // Compute shader not available
    }

    // Create 1x1 R32_FLOAT texture for peak storage
    D3D11_TEXTURE2D_DESC texDesc = {};
    texDesc.Width = 1;
    texDesc.Height = 1;
    texDesc.MipLevels = 1;
    texDesc.ArraySize = 1;
    texDesc.Format = DXGI_FORMAT_R32_FLOAT;
    texDesc.SampleDesc.Count = 1;
    texDesc.Usage = D3D11_USAGE_DEFAULT;
    texDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;

    HRESULT hr = g_device->CreateTexture2D(&texDesc, nullptr, &ctx->peakTexture);
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " failed to create peak texture: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    // Create UAV for compute shader write
    hr = g_device->CreateUnorderedAccessView(ctx->peakTexture, nullptr, &ctx->peakUAV);
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " failed to create peak UAV: 0x"
                  << std::hex << hr << std::dec << std::endl;
        ctx->peakTexture->Release();
        ctx->peakTexture = nullptr;
        return false;
    }

    // Create SRV for pixel shader read
    hr = g_device->CreateShaderResourceView(ctx->peakTexture, nullptr, &ctx->peakSRV);
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " failed to create peak SRV: 0x"
                  << std::hex << hr << std::dec << std::endl;
        ctx->peakUAV->Release();
        ctx->peakUAV = nullptr;
        ctx->peakTexture->Release();
        ctx->peakTexture = nullptr;
        return false;
    }

    std::cout << "Monitor " << ctx->index << " peak detection resources created" << std::endl;
    return true;
}

// ============================================================================
// SECTION: HDR Metadata
// ============================================================================

// Update HDR metadata on swapchain to tell Windows our content's peak brightness
// This allows us to bypass Windows tonemapping by declaring our output peak
void UpdateHDRMetadata(MonitorContext* ctx) {
    if (!ctx->swapchain || !ctx->isHDREnabled) return;

    const auto& tm = ctx->hdrColorCorrection.tonemap;

    // Determine our output peak brightness
    // If tonemapping enabled: we handle tonemapping, tell Windows max (10000) so it passes through
    // If tonemapping disabled: content is unclamped, tell Windows max so it applies system tonemapping based on MaxTML
    // In both cases, 10000 nits ensures Windows uses MaxTML setting to decide tonemapping behavior
    float contentPeakNits = 10000.0f;
    (void)tm;  // Tonemapping settings no longer affect metadata

    // HDR10 metadata (static metadata)
    DXGI_HDR_METADATA_HDR10 metadata = {};

    // MaxCLL: Maximum Content Light Level (peak brightness in nits)
    metadata.MaxContentLightLevel = (UINT16)contentPeakNits;

    // MaxFALL: Maximum Frame Average Light Level (typically lower than peak)
    metadata.MaxFrameAverageLightLevel = (UINT16)(contentPeakNits * 0.5f);

    // Display primaries (Rec.709/sRGB in 0.00002 units)
    // These describe our content's color space, not the display
    metadata.RedPrimary[0] = 32000;   // 0.64
    metadata.RedPrimary[1] = 16500;   // 0.33
    metadata.GreenPrimary[0] = 15000; // 0.30
    metadata.GreenPrimary[1] = 30000; // 0.60
    metadata.BluePrimary[0] = 7500;   // 0.15
    metadata.BluePrimary[1] = 3000;   // 0.06
    metadata.WhitePoint[0] = 15635;   // 0.3127
    metadata.WhitePoint[1] = 16450;   // 0.329

    // Luminance range (in 0.0001 nits units)
    metadata.MinMasteringLuminance = 0;
    metadata.MaxMasteringLuminance = (UINT)(contentPeakNits * 10000);

    HRESULT hr = ctx->swapchain->SetHDRMetaData(DXGI_HDR_METADATA_TYPE_HDR10, sizeof(metadata), &metadata);
    if (SUCCEEDED(hr)) {
        std::cout << "Monitor " << ctx->index << " HDR metadata: MaxCLL=" << contentPeakNits << " nits" << std::endl;
    } else {
        std::cerr << "Monitor " << ctx->index << " failed to set HDR metadata: 0x" << std::hex << hr << std::dec << std::endl;
    }
}

// ============================================================================
// SECTION: Swapchain Management
// ============================================================================

bool CreateSwapChain(MonitorContext* ctx) {
    IDXGIDevice* dxgiDevice = nullptr;
    if (FAILED(g_device->QueryInterface(IID_PPV_ARGS(&dxgiDevice))) || !dxgiDevice) {
        std::cerr << "Failed to get DXGI device for swapchain" << std::endl;
        return false;
    }

    IDXGIAdapter* adapter = nullptr;
    if (FAILED(dxgiDevice->GetAdapter(&adapter)) || !adapter) {
        std::cerr << "Failed to get adapter for swapchain" << std::endl;
        dxgiDevice->Release();
        return false;
    }

    IDXGIFactory5* factory = nullptr;
    if (FAILED(adapter->GetParent(IID_PPV_ARGS(&factory))) || !factory) {
        std::cerr << "Failed to get factory for swapchain" << std::endl;
        adapter->Release();
        dxgiDevice->Release();
        return false;
    }

    // Select format based on HDR state
    // HDR / ACM: FP16 scRGB for linear content (ACM needs FP16 for transparent passthrough)
    // SDR legacy: R10G10B10A2 for 10-bit output (reduces banding after LUT)
    ctx->swapchainFormat = (ctx->isHDREnabled || ctx->isFP16SDR) ?
        DXGI_FORMAT_R16G16B16A16_FLOAT :
        DXGI_FORMAT_R10G10B10A2_UNORM;

    DXGI_SWAP_CHAIN_DESC1 scd = {};
    scd.Width = ctx->width;
    scd.Height = ctx->height;
    scd.Format = ctx->swapchainFormat;
    scd.SampleDesc.Count = 1;
    scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    scd.BufferCount = 2;
    scd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
    scd.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
    scd.Flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
    if (g_tearingSupported) {
        scd.Flags |= DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING;
    }

    IDXGISwapChain1* swapchain1 = nullptr;
    HRESULT hr = factory->CreateSwapChainForComposition(g_device, &scd, nullptr, &swapchain1);

    factory->Release();
    adapter->Release();
    dxgiDevice->Release();

    if (FAILED(hr)) {
        std::cerr << "CreateSwapChainForComposition failed for monitor " << ctx->index << ": 0x" << std::hex << hr << std::endl;
        return false;
    }

    hr = swapchain1->QueryInterface(IID_PPV_ARGS(&ctx->swapchain));
    swapchain1->Release();
    if (FAILED(hr)) return false;

    // Set color space based on HDR state
    // HDR / ACM: scRGB linear (G10 = linear gamma, P709 = BT.709 primaries)
    // SDR legacy: sRGB (G22 = 2.2 gamma, P709 = BT.709 primaries)
    ctx->colorSpace = (ctx->isHDREnabled || ctx->isFP16SDR) ?
        DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709 :
        DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;

    UINT colorSpaceSupport = 0;
    hr = ctx->swapchain->CheckColorSpaceSupport(ctx->colorSpace, &colorSpaceSupport);
    if (SUCCEEDED(hr) && (colorSpaceSupport & DXGI_SWAP_CHAIN_COLOR_SPACE_SUPPORT_FLAG_PRESENT)) {
        hr = ctx->swapchain->SetColorSpace1(ctx->colorSpace);
        const char* csName = ctx->isHDREnabled ? "scRGB linear (HDR)" :
                             ctx->isFP16SDR ? "scRGB linear (ACM SDR)" : "sRGB (SDR)";
        if (SUCCEEDED(hr)) {
            std::cout << "Monitor " << ctx->index << " color space: " << csName << std::endl;
        }
    } else {
        std::cout << "Warning: Monitor " << ctx->index << " requested color space not supported" << std::endl;
    }

    // Set HDR metadata to inform Windows of our content's peak brightness
    if (ctx->isHDREnabled) {
        UpdateHDRMetadata(ctx);
    }

    // Set maximum frame latency to 1 for minimum latency
    // Note: We don't use the waitable object for pacing (DwmFlush works better with DirectComposition)
    // but SetMaximumFrameLatency still limits the present queue to prevent frame buildup
    ctx->swapchain->SetMaximumFrameLatency(1);

    // Create RTV
    ID3D11Texture2D* backBuffer = nullptr;
    hr = ctx->swapchain->GetBuffer(0, IID_PPV_ARGS(&backBuffer));
    if (FAILED(hr) || !backBuffer) {
        std::cerr << "Failed to get swapchain back buffer: 0x" << std::hex << hr << std::dec << std::endl;
        return false;
    }
    hr = g_device->CreateRenderTargetView(backBuffer, nullptr, &ctx->rtv);
    backBuffer->Release();
    if (FAILED(hr)) {
        std::cerr << "Failed to create RTV: 0x" << std::hex << hr << std::dec << std::endl;
        return false;
    }

    return true;
}

// ============================================================================
// SECTION: DirectComposition
// ============================================================================

bool InitDirectCompositionDevice() {
    HRESULT hr = DCompositionCreateDevice(nullptr, IID_PPV_ARGS(&g_dcompDevice));
    if (FAILED(hr)) {
        std::cerr << "DCompositionCreateDevice failed: 0x" << std::hex << hr << std::endl;
        return false;
    }
    return true;
}

bool InitDirectComposition(MonitorContext* ctx) {
    HRESULT hr = g_dcompDevice->CreateTargetForHwnd(ctx->hwnd, TRUE, &ctx->dcompTarget);
    if (FAILED(hr)) {
        std::cerr << "CreateTargetForHwnd failed for monitor " << ctx->index << ": 0x" << std::hex << hr << std::dec << std::endl;
        return false;
    }

    hr = g_dcompDevice->CreateVisual(&ctx->dcompVisual);
    if (FAILED(hr)) {
        std::cerr << "CreateVisual failed for monitor " << ctx->index << ": 0x" << std::hex << hr << std::dec << std::endl;
        ctx->dcompTarget->Release();
        ctx->dcompTarget = nullptr;
        return false;
    }

    hr = ctx->dcompVisual->SetContent(ctx->swapchain);
    if (FAILED(hr)) {
        std::cerr << "SetContent failed for monitor " << ctx->index << ": 0x" << std::hex << hr << std::dec << std::endl;
        ctx->dcompVisual->Release();
        ctx->dcompVisual = nullptr;
        ctx->dcompTarget->Release();
        ctx->dcompTarget = nullptr;
        return false;
    }

    hr = ctx->dcompTarget->SetRoot(ctx->dcompVisual);
    if (FAILED(hr)) {
        std::cerr << "SetRoot failed for monitor " << ctx->index << ": 0x" << std::hex << hr << std::dec << std::endl;
        ctx->dcompVisual->Release();
        ctx->dcompVisual = nullptr;
        ctx->dcompTarget->Release();
        ctx->dcompTarget = nullptr;
        return false;
    }

    // Don't Commit() yet - will be done after first frame is rendered to prevent black flash
    ctx->dcompCommitted = false;
    ctx->framesAfterCommit = 0;
    return true;
}

void ResizeSwapChain(MonitorContext* ctx, int width, int height) {
    if (ctx->rtv) {
        ctx->rtv->Release();
        ctx->rtv = nullptr;
    }

    UINT flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
    if (g_tearingSupported) flags |= DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING;
    HRESULT hr = ctx->swapchain->ResizeBuffers(2, width, height,
        ctx->swapchainFormat, flags);

    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " ResizeBuffers failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        // Don't disable - will retry on next reinit cycle
        return;
    }

    ID3D11Texture2D* backBuffer = nullptr;
    hr = ctx->swapchain->GetBuffer(0, IID_PPV_ARGS(&backBuffer));
    if (FAILED(hr) || !backBuffer) {
        std::cerr << "Monitor " << ctx->index << " GetBuffer failed after resize: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return;
    }
    hr = g_device->CreateRenderTargetView(backBuffer, nullptr, &ctx->rtv);
    backBuffer->Release();
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " CreateRTV failed after resize: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return;
    }

    ctx->width = width;
    ctx->height = height;
}

bool RecreateSwapchain(MonitorContext* ctx) {
    // Hide window and reset to fully transparent during recreation to prevent black flash
    if (ctx->hwnd) {
        if (IsWindowVisible(ctx->hwnd)) {
            ShowWindow(ctx->hwnd, SW_HIDE);
        }
        SetLayeredWindowAttributes(ctx->hwnd, 0, 0, LWA_ALPHA);  // Reset to transparent
    }

    // Clean up existing DirectComposition content
    if (ctx->dcompVisual) {
        ctx->dcompVisual->SetContent(nullptr);
    }

    // Clean up existing swapchain resources
    if (ctx->rtv) {
        ctx->rtv->Release();
        ctx->rtv = nullptr;
    }
    if (ctx->swapchain) {
        ctx->swapchain->Release();
        ctx->swapchain = nullptr;
    }

    // Create new swapchain with appropriate format for current HDR state
    if (!CreateSwapChain(ctx)) {
        std::cerr << "Failed to recreate swapchain for monitor " << ctx->index << std::endl;
        return false;
    }

    // Rebind to DirectComposition (but don't commit yet - wait for first frame)
    if (ctx->dcompVisual) {
        ctx->dcompVisual->SetContent(ctx->swapchain);
    }
    ctx->dcompCommitted = false;  // Will commit after first frame is rendered
    ctx->framesAfterCommit = 0;   // Reset frame counter for visibility delay

    std::cout << "Monitor " << ctx->index << " swapchain recreated for " << (ctx->isHDREnabled ? "HDR" : "SDR") << " mode" << std::endl;
    return true;
}

// ============================================================================
// SECTION: MHC Profile Mode Switch
// ============================================================================

// Reapply MHC ICC profiles after HDR/SDR mode switch
// When HDR is toggled programmatically via DisplayConfigSetDeviceInfo, Windows changes
// the display mode but doesn't refresh its color management pipeline. The previously
// associated MHC profile for the new mode exists but isn't activated. A simple reassociate
// is a no-op since Windows already considers it associated. Remove + re-add forces Windows
// to reprocess the profile and apply it to the active pipeline.
static void ReapplyMhcProfilesOnModeSwitch(MonitorContext* ctx) {
    if (ctx->index >= (int)g_gui.monitorSettings.size()) return;
    const auto& ms = g_gui.monitorSettings[ctx->index];

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(ctx->index, displayInfo)) return;

    // Remove + re-add profile for current mode to force Windows to apply it
    const auto& mhc = ctx->isHDREnabled ? ms.hdrMHC : ms.sdrMHC;
    if (mhc.enabled && !mhc.profileName.empty()) {
        std::wcout << L"Mode switch: reapplying " << (ctx->isHDREnabled ? L"HDR" : L"SDR")
                   << L" MHC profile '" << mhc.profileName
                   << L"' for monitor " << ctx->index << std::endl;
        RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, ctx->isHDREnabled);
        ReassociateMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, ctx->isHDREnabled);
    }

    // Update MHC flags to match current settings
    ctx->sdrMhcPrimariesActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty() && ms.sdrMHC.primariesEnabled;
    ctx->sdrMhcGrayscaleActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty() && ms.sdrMHC.grayscale.enabled;
    ctx->hdrMhcPrimariesActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty() && ms.hdrMHC.primariesEnabled;
    ctx->hdrMhcGrayscaleActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty() && ms.hdrMHC.grayscale.enabled;
}

// ============================================================================
// SECTION: Render Monitor
// ============================================================================

void RenderMonitor(MonitorContext* ctx) {
    // Entry validation - skip if monitor is disabled
    if (!ctx || !ctx->enabled) return;

    // If resources are missing (failed reinit), try to recover with non-blocking backoff.
    // No Sleep() — returns immediately so other monitors keep rendering at full frame rate.
    if (!ctx->duplication) {
        if (g_displayOff.load()) {
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return;
        }

        // Non-blocking backoff: skip this monitor until enough time has passed
        auto now = std::chrono::steady_clock::now();
        if (ctx->recoveryBackoffMs > 0) {
            auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - ctx->lastRecoveryAttempt);
            if (elapsed.count() < ctx->recoveryBackoffMs)
                return;  // Not time yet — skip, don't block
        }

        ctx->lastRecoveryAttempt = now;
        ctx->consecutiveFailures++;
        ctx->recoveryBackoffMs = (std::min)(50 * (1 << (std::min)(ctx->consecutiveFailures - 1, 7)), 5000);

        if (ctx->consecutiveFailures % 10 == 0) {
            std::cout << "Monitor " << ctx->index << " attempting recovery, attempt "
                      << ctx->consecutiveFailures << "..." << std::endl;
        }

        g_lastSuccessfulFrame = std::chrono::steady_clock::now();

        if (ReinitDesktopDuplication(ctx)) {
            std::cout << "Monitor " << ctx->index << " reinit success" << std::endl;
            ctx->consecutiveFailures = 0;
            ctx->recoveryBackoffMs = 0;
            ctx->lastCaptureTexture = nullptr;  // Invalidate SRV cache after reinit
            // ReinitDesktopDuplication handles HDR change + swapchain recreation internally.
            // Apply additional mode-switch steps not covered by reinit:
            if (ctx->isHDREnabled != ctx->wasHDREnabled) {
                ApplyMaxTmlSettings();
                ctx->cbDirty = true;
            }
            ReapplyMhcProfilesOnModeSwitch(ctx);
            ctx->wasHDREnabled = ctx->isHDREnabled;
        }
        return;
    }

    if (!ctx->swapchain || !ctx->rtv) return;

    // Acquire next frame from desktop duplication (non-blocking).
    // Frame pacing is handled by compositor sync in RenderAll(), so we only need
    // Try instant capture first (catches context menus, cursor changes, etc.)
    // If no frame ready, DwmFlush syncs to compositor then blocking acquire gets the frame.
    // DWM composes at 60Hz regardless of display refresh rate — DD delivery matches compositor.
    DXGI_OUTDUPL_FRAME_INFO frameInfo;
    IDXGIResource* desktopResource = nullptr;
    bool frameAcquired = false;

    HRESULT hr = ctx->duplication->AcquireNextFrame(0, &frameInfo, &desktopResource);
    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        DwmFlush();
        hr = ctx->duplication->AcquireNextFrame(ctx->frameTimeMs, &frameInfo, &desktopResource);
    }

    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        // No new frame available - nothing to render
        // Reset watchdog since the duplication interface is working (just no desktop changes)
        // This prevents false watchdog triggers when monitor is off or desktop is static
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        // Still need to handle initial visibility even without new frames
        // (window waits to be shown after DirectComposition commit)
        // Skip if VRR whitelist is hiding overlays (passthrough mode)
        if (ctx->dcompCommitted && ctx->hwnd && !IsWindowVisible(ctx->hwnd)
            && !g_vrrWhitelistActive.load() && !g_overlayAutoSleep.load()) {
            ctx->framesAfterCommit++;
            if (ctx->framesAfterCommit >= 1) {
                SetLayeredWindowAttributes(ctx->hwnd, 0, 255, LWA_ALPHA);
                ShowWindow(ctx->hwnd, SW_SHOWNA);
            }
        }
        return;
    } else if (hr == DXGI_ERROR_ACCESS_LOST || FAILED(hr)) {
        // Desktop duplication lost — hide overlay, release, set up non-blocking recovery.
        // NO Sleep() here: return immediately so other monitors keep rendering.
        // Recovery will be attempted on next RenderMonitor call via the !ctx->duplication path.
        if (ctx->hwnd && IsWindowVisible(ctx->hwnd)) {
            ShowWindow(ctx->hwnd, SW_HIDE);
        }
        if (ctx->duplication) {
            ctx->duplication->Release();
            ctx->duplication = nullptr;
        }
        ctx->lastCaptureTexture = nullptr;  // Invalidate SRV cache

        if (g_displayOff.load()) {
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            ctx->consecutiveFailures = 0;
            return;
        }

        ctx->consecutiveFailures++;
        ctx->recoveryBackoffMs = (std::min)(50 * (1 << (std::min)(ctx->consecutiveFailures - 1, 7)), 5000);
        ctx->lastRecoveryAttempt = std::chrono::steady_clock::now();

        if (ctx->consecutiveFailures == 1 || ctx->consecutiveFailures % 10 == 0) {
            std::cout << "Monitor " << ctx->index << " duplication lost (0x" << std::hex << hr << std::dec
                      << "), attempt " << ctx->consecutiveFailures << "..." << std::endl;
        }

        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        return;
    }

    frameAcquired = true;

    // Skip mouse-only frames (no desktop pixels changed, just cursor position update).
    // Desktop Duplication delivers a frame for every cursor update even when no desktop
    // pixels changed. No additional sync needed — next iteration starts with compositor sync.
    if (frameInfo.LastPresentTime.QuadPart == 0 && frameInfo.AccumulatedFrames == 0) {
        desktopResource->Release();
        ctx->duplication->ReleaseFrame();
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        return;
    }

    // Reset consecutive failures on successful frame acquisition
    ctx->consecutiveFailures = 0;

    // Got a new frame - get the texture
    ID3D11Texture2D* frameTexture = nullptr;
    hr = desktopResource->QueryInterface(IID_PPV_ARGS(&frameTexture));
    desktopResource->Release();

    if (FAILED(hr)) {
        ctx->duplication->ReleaseFrame();
        return;
    }

    // Check if size changed
    D3D11_TEXTURE2D_DESC texDesc;
    frameTexture->GetDesc(&texDesc);

    if ((int)texDesc.Width != ctx->width || (int)texDesc.Height != ctx->height) {
        ctx->width = texDesc.Width;
        ctx->height = texDesc.Height;
        ResizeSwapChain(ctx, ctx->width, ctx->height);

        // Also resize window
        SetWindowPos(ctx->hwnd, nullptr, 0, 0, ctx->width, ctx->height,
            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    }

    // Check if capture format changed (Windows HDR toggle can change format without ACCESS_LOST)
    if (texDesc.Format != ctx->captureFormat) {
        std::cout << "Monitor " << ctx->index << " capture format changed, forcing full reinit..." << std::endl;
        frameTexture->Release();
        ctx->duplication->ReleaseFrame();
        if (ctx->duplication) {
            ctx->duplication->Release();
            ctx->duplication = nullptr;
        }
        ctx->lastCaptureTexture = nullptr;  // Invalidate SRV cache
        if (ReinitDesktopDuplication(ctx)) {
            // HDR state changed - update swapchain and settings
            bool hasApplicableLUT = ctx->isHDREnabled
                ? (ctx->lutSRV_HDR != nullptr)
                : (ctx->lutSRV_SDR != nullptr);
            ctx->usePassthrough = !hasApplicableLUT;
            RecreateSwapchain(ctx);
            ApplyMaxTmlSettings();
            // Always reapply MHC ICC profiles after duplication reinit
            ReapplyMhcProfilesOnModeSwitch(ctx);
            ctx->wasHDREnabled = ctx->isHDREnabled;
            ctx->cbDirty = true;
            std::cout << "Monitor " << ctx->index << " switched to " << (ctx->isHDREnabled ? "HDR" : "SDR") << " mode" << std::endl;
        }
        return;
    }

    // Create SRV for captured frame (reuse if same texture pointer — common case)
    if (frameTexture != ctx->lastCaptureTexture) {
        if (ctx->captureSRV) {
            ctx->captureSRV->Release();
            ctx->captureSRV = nullptr;
        }
        D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = texDesc.Format;
        srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        srvDesc.Texture2D.MipLevels = 1;
        hr = g_device->CreateShaderResourceView(frameTexture, &srvDesc, &ctx->captureSRV);
        ctx->lastCaptureTexture = frameTexture;  // Weak ref for comparison (not AddRef'd)
    }
    frameTexture->Release();

    if (FAILED(hr)) {
        ctx->duplication->ReleaseFrame();
        return;
    }

    // Check if atomic toggles changed → mark constant buffer dirty
    bool curGamma = g_desktopGammaMode.load();
    bool curTetrahedral = g_tetrahedralInterp.load();
    if (curGamma != ctx->lastDesktopGamma || curTetrahedral != ctx->lastTetrahedralInterp) {
        ctx->cbDirty = true;
        ctx->lastDesktopGamma = curGamma;
        ctx->lastTetrahedralInterp = curTetrahedral;
    }

    // Motion bar position changes every frame — force CB update when enabled
    bool motionBar = g_showMotionBar.load();
    if (motionBar) ctx->cbDirty = true;

    // Update constant buffer only when dirty (avoids Map/Unmap overhead on static frames)
    if (ctx->cbDirty) {
    D3D11_MAPPED_SUBRESOURCE mapped;
    hr = g_context->Map(g_constantBuffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped);
    if (SUCCEEDED(hr)) {
        float* cbData = (float*)mapped.pData;
        // Row 0: Core settings
        cbData[0] = ctx->isHDREnabled ? 1.0f : 0.0f;
        cbData[1] = g_sdrWhiteNits;
        cbData[2] = ctx->maxDisplayNits;
        cbData[3] = (float)(ctx->isHDREnabled ? ctx->lutSizeHDR : ctx->lutSizeSDR);
        // Row 1: Toggles
        cbData[4] = g_desktopGammaMode.load() ? 1.0f : 0.0f;  // Desktop gamma toggle
        cbData[5] = g_tetrahedralInterp.load() ? 1.0f : 0.0f; // Tetrahedral interpolation
        cbData[6] = ctx->usePassthrough ? 1.0f : 0.0f;  // HDR passthrough (no LUT)
        // Select color correction based on HDR state
        const auto& cc = ctx->isHDREnabled ? ctx->hdrColorCorrection : ctx->sdrColorCorrection;
        // MHC primaries and shader primaries are independent layers:
        // MHC = base calibration at GPU scanout (yearly), shader = fine-tuning on top (anytime)
        // Same principle as grayscale: both layers can be active simultaneously
        bool shaderPrimaries = cc.primariesEnabled;  // White point correction (primaries gamut mapping is MHC-only)
        bool shaderGrayscale = cc.grayscale.enabled;
        // White balance gains only apply when explicitly enabled — prevents leftover
        // non-D65 values in INI from silently shifting white point when corrections are off
        bool shaderWhiteBalance = cc.primariesEnabled &&
            (cc.whiteBalanceGains[0] != 1.0f || cc.whiteBalanceGains[1] != 1.0f || cc.whiteBalanceGains[2] != 1.0f);
        cbData[7] = (shaderPrimaries || shaderGrayscale || shaderWhiteBalance) ? 1.0f : 0.0f;  // useManualCorrection
        // Row 2: Grayscale control + tonemapping toggles
        cbData[8] = (float)cc.grayscale.pointCount;
        cbData[9] = shaderGrayscale ? 1.0f : 0.0f;
        cbData[10] = (ctx->isHDREnabled && cc.tonemap.enabled) ? 1.0f : 0.0f;  // tonemapEnabled
        cbData[11] = (float)static_cast<int>(cc.tonemap.curve);  // tonemapCurve
        // Row 3-5: Primaries matrix (xyz) + white balance gains (w)
        cbData[12] = cc.primariesMatrix[0];
        cbData[13] = cc.primariesMatrix[1];
        cbData[14] = cc.primariesMatrix[2];
        cbData[15] = cc.whiteBalanceGains[0];  // R gain
        cbData[16] = cc.primariesMatrix[3];
        cbData[17] = cc.primariesMatrix[4];
        cbData[18] = cc.primariesMatrix[5];
        cbData[19] = cc.whiteBalanceGains[1];  // G gain
        cbData[20] = cc.primariesMatrix[6];
        cbData[21] = cc.primariesMatrix[7];
        cbData[22] = cc.primariesMatrix[8];
        cbData[23] = cc.whiteBalanceGains[2];  // B gain
        // Row 6: Tonemapping parameters
        // Slot [24]: PQ-encoded source peak (avoids per-pixel pow() in pixel shader)
        if (cc.tonemap.dynamicPeak) {
            // Dynamic: PQ of (targetPeak * 1.25) as floor for GPU-detected peak
            cbData[24] = LinearToPQScalar(cc.tonemap.targetPeakNits * 1.25f / 10000.0f);
        } else {
            // Static: PQ of user-specified source peak (fallback 1000 nits)
            float srcPeak = (cc.tonemap.sourcePeakNits > 0.0f) ? cc.tonemap.sourcePeakNits : 1000.0f;
            cbData[24] = LinearToPQScalar(srcPeak / 10000.0f);
        }
        cbData[25] = cc.tonemap.targetPeakNits;
        cbData[26] = cc.tonemap.dynamicPeak ? 1.0f : 0.0f;  // tonemapDynamic
        cbData[27] = cc.grayscale.use24Gamma ? 1.0f : 0.0f;  // grayscale24
        // Row 7: Grayscale peak + ACM flag
        cbData[28] = cc.grayscale.peakNits;  // grayscalePeakNits (HDR only)
        cbData[29] = ctx->isFP16SDR ? 1.0f : 0.0f;  // ACM: FP16 SDR, input is linear scRGB
        cbData[30] = LinearToPQScalar(cc.tonemap.targetPeakNits / 10000.0f);  // pqTargetPeak (precomputed)
        cbData[31] = LinearToPQScalar((std::max)(cc.grayscale.peakNits, 1.0f) / 10000.0f);  // pqGrayscalePeak (precomputed)
        // Row 8-15: Grayscale LUT (32 points packed into 8 float4s)
        for (int i = 0; i < 32; i++) {
            cbData[32 + i] = (i < cc.grayscale.pointCount && i < 32)
                ? cc.grayscale.points[i]
                : ((float)i / 31.0f);  // Linear fallback
        }
        // Row 16: Motion bar (UFO test-style judder detection)
        if (motionBar) {
            if (!s_motionBarOriginSet) {
                s_motionBarOrigin = std::chrono::steady_clock::now();
                s_motionBarOriginSet = true;
            }
            float elapsed = std::chrono::duration<float>(
                std::chrono::steady_clock::now() - s_motionBarOrigin).count();
            cbData[64] = 1.0f;  // motionBarEnabled
            cbData[65] = fmodf(elapsed * 0.5f, 1.0f);  // position: 0.5 traversals/sec
        } else {
            cbData[64] = 0.0f;
            cbData[65] = 0.0f;
        }
        cbData[66] = 0.0f;  // reserved
        cbData[67] = 0.0f;  // reserved
        g_context->Unmap(g_constantBuffer, 0);
    }
    ctx->cbDirty = false;
    } // end if cbDirty

    // Select the appropriate LUT based on HDR mode (no fallback - SDR/HDR LUTs are incompatible)
    // If no applicable LUT, usePassthrough is true and shader skips LUT sampling
    ID3D11ShaderResourceView* activeLUT = ctx->isHDREnabled ? ctx->lutSRV_HDR : ctx->lutSRV_SDR;

    // Run peak detection compute shader if dynamic tonemapping enabled
    const auto& cc = ctx->isHDREnabled ? ctx->hdrColorCorrection : ctx->sdrColorCorrection;
    if (ctx->isHDREnabled && cc.tonemap.enabled && cc.tonemap.dynamicPeak &&
        g_peakDetectCS && g_peakCB && ctx->captureSRV) {
        // Create peak resources on first use
        if (!ctx->peakTexture) {
            CreatePeakDetectionResources(ctx);
        }

        if (ctx->peakTexture && ctx->peakUAV) {
            // Update peak constant buffer only when dimensions change (static values stay valid)
            if (ctx->width != ctx->lastPeakCBWidth || ctx->height != ctx->lastPeakCBHeight) {
                D3D11_MAPPED_SUBRESOURCE mapped;
                if (SUCCEEDED(g_context->Map(g_peakCB, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
                    // frameWidth/frameHeight are uint in shader, must write as uint
                    uint32_t* udata = (uint32_t*)mapped.pData;
                    udata[0] = (uint32_t)ctx->width;
                    udata[1] = (uint32_t)ctx->height;
                    float* fdata = (float*)mapped.pData;
                    fdata[2] = 0.3f;    // riseRate - exponential rise (0.3 = 30% per frame)
                    fdata[3] = 0.05f;   // fallRate - exponential fall (0.05 = 5% per frame)
                    fdata[4] = 100.0f;  // maxRisePerFrame - slew limit (nits/frame)
                    fdata[5] = 50.0f;   // maxFallPerFrame - slew limit (nits/frame)
                    fdata[6] = 0.0f;    // padding
                    fdata[7] = 0.0f;    // padding
                    g_context->Unmap(g_peakCB, 0);
                    ctx->lastPeakCBWidth = ctx->width;
                    ctx->lastPeakCBHeight = ctx->height;
                }
            }

            // Dispatch compute shader
            g_context->CSSetShader(g_peakDetectCS, nullptr, 0);
            g_context->CSSetConstantBuffers(0, 1, &g_peakCB);
            g_context->CSSetShaderResources(0, 1, &ctx->captureSRV);
            g_context->CSSetUnorderedAccessViews(0, 1, &ctx->peakUAV, nullptr);
            g_context->Dispatch(1, 1, 1);

            // Unbind UAV to allow SRV binding
            ID3D11UnorderedAccessView* nullUAV = nullptr;
            g_context->CSSetUnorderedAccessViews(0, 1, &nullUAV, nullptr);
            ID3D11ShaderResourceView* nullSRV = nullptr;
            g_context->CSSetShaderResources(0, 1, &nullSRV);

            // Double-buffered peak readback: copy to staging[N], read staging[N-1] from previous frame.
            // This avoids GPU pipeline stalls by reading data that's guaranteed to be ready.
            bool needPeakReadback = g_analysisEnabled.load() || g_logPeakDetection.load();
            if (needPeakReadback) {
                static std::chrono::steady_clock::time_point lastReadback[8] = {};
                auto now = std::chrono::steady_clock::now();
                int idx = ctx->index < 8 ? ctx->index : 0;

                if (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastReadback[idx]).count() >= 500) {
                    // Create both staging textures on first use
                    auto createStaging = [&](ID3D11Texture2D** tex) {
                        if (*tex) return;
                        D3D11_TEXTURE2D_DESC stagingDesc = {};
                        stagingDesc.Width = 1;
                        stagingDesc.Height = 1;
                        stagingDesc.MipLevels = 1;
                        stagingDesc.ArraySize = 1;
                        stagingDesc.Format = DXGI_FORMAT_R32_FLOAT;
                        stagingDesc.SampleDesc.Count = 1;
                        stagingDesc.Usage = D3D11_USAGE_STAGING;
                        stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                        g_device->CreateTexture2D(&stagingDesc, nullptr, tex);
                    };
                    createStaging(&ctx->peakStagingTexture);
                    createStaging(&ctx->peakStagingTexture2);

                    if (ctx->peakStagingTexture && ctx->peakStagingTexture2) {
                        // Write: copy GPU result to current staging buffer
                        ID3D11Texture2D* writeTarget = (ctx->peakStagingReadIndex == 0)
                            ? ctx->peakStagingTexture : ctx->peakStagingTexture2;
                        g_context->CopyResource(writeTarget, ctx->peakTexture);

                        // Read: map the OTHER staging buffer (from previous readback cycle)
                        ID3D11Texture2D* readSource = (ctx->peakStagingReadIndex == 0)
                            ? ctx->peakStagingTexture2 : ctx->peakStagingTexture;
                        D3D11_MAPPED_SUBRESOURCE mapped;
                        if (SUCCEEDED(g_context->Map(readSource, 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped))) {
                            float pqValue = *((float*)mapped.pData);
                            g_context->Unmap(readSource, 0);
                            // Convert PQ-encoded peak back to nits for analysis display
                            ctx->detectedPeakNits = PQToLinearScalar(pqValue) * 10000.0f;
                            if (g_logPeakDetection.load()) {
                                std::cout << "Monitor " << ctx->index << " detected peak: "
                                          << std::fixed << std::setprecision(1) << ctx->detectedPeakNits << " nits" << std::endl;
                            }
                        }
                        // Alternate for next cycle
                        ctx->peakStagingReadIndex = 1 - ctx->peakStagingReadIndex;
                    }
                    lastReadback[idx] = now;
                }
            }
        }
    }

    // Render
    float clearColor[4] = { 0, 0, 0, 0 };
    g_context->ClearRenderTargetView(ctx->rtv, clearColor);

    D3D11_VIEWPORT vp = { 0, 0, (float)ctx->width, (float)ctx->height, 0, 1 };
    g_context->RSSetViewports(1, &vp);
    g_context->OMSetRenderTargets(1, &ctx->rtv, nullptr);

    g_context->VSSetShader(g_vs, nullptr, 0);
    g_context->PSSetShader(g_ps, nullptr, 0);
    g_context->PSSetConstantBuffers(0, 1, &g_constantBuffer);
    g_context->PSSetShaderResources(0, 1, &ctx->captureSRV);
    g_context->PSSetShaderResources(1, 1, &activeLUT);
    g_context->PSSetShaderResources(2, 1, &g_blueNoiseSRV);
    // Bind peak texture for dynamic tonemapping (t3)
    if (ctx->peakSRV) {
        g_context->PSSetShaderResources(3, 1, &ctx->peakSRV);
    }

    ID3D11SamplerState* samplers[] = { g_samplerPoint, g_samplerLinear, g_samplerWrap };
    g_context->PSSetSamplers(0, 3, samplers);

    g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    g_context->Draw(3, 0);

    // Analysis overlay (primary monitor only)
    if (ctx->index == 0 && g_analysisEnabled.load()) {
        DispatchAnalysisCompute(ctx);
        UpdateAnalysisDisplay(ctx);
    }

    // Present immediately - DwmFlush at start of loop handles sync
    UINT presentFlags = g_tearingSupported ? DXGI_PRESENT_ALLOW_TEARING : 0;
    HRESULT presentHr = ctx->swapchain->Present(0, presentFlags);

    if (presentHr == DXGI_ERROR_DEVICE_REMOVED || presentHr == DXGI_ERROR_DEVICE_RESET) {
        std::cerr << "Monitor " << ctx->index << " device lost during Present: 0x"
                  << std::hex << presentHr << std::dec << std::endl;
        if (g_device) {
            HRESULT reason = g_device->GetDeviceRemovedReason();
            std::cerr << "  Device removed reason: 0x" << std::hex << reason << std::dec << std::endl;
        }
        // Hide overlay immediately to prevent black screen blocking desktop
        if (ctx->hwnd) {
            ShowWindow(ctx->hwnd, SW_HIDE);
        }
        ctx->enabled = false;
    } else {
        // Successful frame - update watchdog timestamp
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();

        // Track frame timing only when analysis overlay is active (avoid unnecessary work)
        if (g_analysisEnabled.load()) {
            if (ctx->lastFrameTime.time_since_epoch().count() > 0) {
                auto now = std::chrono::steady_clock::now();
                float frameMs = std::chrono::duration<float, std::milli>(now - ctx->lastFrameTime).count();
                ctx->lastFrameTime = now;

                ctx->frameTimeHistory[ctx->frameTimeIndex] = frameMs;
                ctx->frameTimeIndex = (ctx->frameTimeIndex + 1) % 64;
                if (ctx->frameTimeCount < 64) ctx->frameTimeCount++;
            } else {
                ctx->lastFrameTime = std::chrono::steady_clock::now();
            }
        } else {
            // Reset so we get a fresh baseline when analysis is toggled on
            ctx->lastFrameTime = {};
            ctx->frameTimeCount = 0;
        }

        // Two-phase visibility: first commit DirectComposition, then show window on next frame
        // This prevents black flash by ensuring DirectComposition has processed the visual
        if (!ctx->dcompCommitted && g_dcompDevice) {
            // First successful frame: commit DirectComposition but don't show yet
            g_context->Flush();
            g_dcompDevice->Commit();
            ctx->dcompCommitted = true;
            ctx->framesAfterCommit = 0;  // Start counting frames after commit
        } else if (ctx->dcompCommitted && ctx->hwnd && !IsWindowVisible(ctx->hwnd)
                   && !g_vrrWhitelistActive.load() && !g_overlayAutoSleep.load()) {
            // Wait one frame after commit for DirectComposition to process, then show
            // Skip if VRR whitelist or auto-sleep is hiding overlays
            ctx->framesAfterCommit++;
            if (ctx->framesAfterCommit >= 1) {
                SetLayeredWindowAttributes(ctx->hwnd, 0, 255, LWA_ALPHA);
                ShowWindow(ctx->hwnd, SW_SHOWNA);
            }
        }
    }

    // Release the frame after rendering is complete
    if (frameAcquired) {
        ctx->duplication->ReleaseFrame();
    }
}

// ============================================================================
// SECTION: Render Loop
// ============================================================================

void RenderAll() {
    int activeCount = 0;

    // Device health is checked reactively when Present() or AcquireNextFrame() returns
    // DXGI_ERROR_DEVICE_REMOVED — no need for periodic proactive polling.
    // The watchdog below catches any case where rendering is silently stuck.

    // Watchdog: if no successful frame for N seconds, exit gracefully
    // This catches cases where device appears healthy but rendering is stuck
    auto timeSinceLastFrame = std::chrono::steady_clock::now() - g_lastSuccessfulFrame;
    if (timeSinceLastFrame > std::chrono::seconds(WATCHDOG_TIMEOUT_SECONDS)) {
        std::cerr << "Watchdog timeout: no successful frame for " << WATCHDOG_TIMEOUT_SECONDS << " seconds" << std::endl;
        MessageBeep(MB_ICONERROR);
        // Hide all overlay windows
        for (auto& ctx : g_monitors) {
            if (ctx.hwnd) {
                ShowWindow(ctx.hwnd, SW_HIDE);
            }
            ctx.enabled = false;
        }
        g_running = false;
        return;
    }

    // Check for forced reinit (e.g., resume from sleep)
    if (g_forceReinit.exchange(false)) {
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
        std::cout << "Forcing reinit of all monitors..." << std::endl;
        // Give system time to stabilize after wake
        Sleep(500);
        // Release all duplication interfaces to force reinit
        for (auto& ctx : g_monitors) {
            if (ctx.duplication) {
                ctx.duplication->Release();
                ctx.duplication = nullptr;
            }
            ctx.consecutiveFailures = 0;  // Reset backoff
            ctx.recoveryBackoffMs = 0;    // Reset non-blocking backoff
            ctx.cbDirty = true;           // Force constant buffer refresh
            ctx.lastCaptureTexture = nullptr;  // Invalidate SRV cache
        }
        // Reset watchdog to avoid timeout during recovery
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        // Reapply MaxTML settings (may be lost after sleep/wake)
        ApplyMaxTmlSettings();
        // Reapply MHC profiles (may be silently dropped after sleep/wake)
        ReapplyAllMhcProfiles();
        // Force TOPMOST reassert after wake (z-order most likely disrupted)
        g_forceTopmostReassert.store(true);
    }

    // Periodically reassert TOPMOST to prevent other windows pushing us down
    static auto lastTopmost = std::chrono::steady_clock::now();
    auto now = std::chrono::steady_clock::now();
    bool forceReassert = g_forceTopmostReassert.exchange(false);
    if (forceReassert || std::chrono::duration_cast<std::chrono::milliseconds>(now - lastTopmost).count() >= 30000) {
        for (auto& ctx : g_monitors) {
            if (ctx.hwnd) {
                SetWindowPos(ctx.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }
        }
        // Keep analysis overlay above monitor overlays
        if (g_analysisHwnd && g_analysisEnabled.load()) {
            SetWindowPos(g_analysisHwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
        // Our own SetWindowPos calls trigger WM_WINDOWPOSCHANGING (no SWP_NOZORDER),
        // which re-sets g_forceTopmostReassert — creating a feedback loop that would
        // reassert on EVERY frame. Clear the self-triggered flag to break the cycle.
        // External z-order changes between here and next check are caught by 30s fallback.
        g_forceTopmostReassert.store(false);
        lastTopmost = now;
    }

    // Gamma whitelist is now checked on a separate thread (see GammaWhitelistThreadFunc)
    // The render loop just reads the atomic g_gammaWhitelistActive flag via constant buffer

    // Apply any pending color correction updates (fast path: skip mutex if no updates)
    // Swap-under-lock pattern: grab pending updates quickly, process without holding lock
    // Also signal wake event in case we're about to enter auto-sleep check
    if (g_hasPendingColorCorrections.load(std::memory_order_acquire)) {
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
        std::vector<PendingColorCorrection> localUpdates;
        {
            std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
            localUpdates.swap(g_pendingColorCorrections);
            g_hasPendingColorCorrections.store(false, std::memory_order_release);
        }
        for (const auto& update : localUpdates) {
            bool matched = false;
            for (auto& ctx : g_monitors) {
                if (ctx.index == update.monitorIndex) {
                    if (update.isHDR) {
                        ctx.hdrColorCorrection = update.data;
                    } else {
                        ctx.sdrColorCorrection = update.data;
                    }
                    if (update.clearMhcFlags) {
                        ctx.sdrMhcPrimariesActive = false;
                        ctx.sdrMhcGrayscaleActive = false;
                        ctx.hdrMhcPrimariesActive = false;
                        ctx.hdrMhcGrayscaleActive = false;
                    }
                    ctx.cbDirty = true;
                    std::cout << "[Render] Applied CC: mon=" << ctx.index
                              << " isHDR=" << update.isHDR
                              << " mhcPrim=" << (ctx.isHDREnabled ? ctx.hdrMhcPrimariesActive : ctx.sdrMhcPrimariesActive)
                              << " primEn=" << update.data.primariesEnabled
                              << " gsEn=" << update.data.grayscale.enabled
                              << " gsPts=" << update.data.grayscale.pointCount
                              << " clearMhc=" << update.clearMhcFlags
                              << std::endl;
                    matched = true;
                    break;
                }
            }
            if (!matched) {
                std::cout << "[Render] DROPPED CC: mon=" << update.monitorIndex
                          << " (no matching ctx, g_monitors.size=" << g_monitors.size() << ")" << std::endl;
            }
        }
    }

    // Auto-sleep: hide overlay when no monitor needs processing (e.g., only MHC ICC active)
    {
        bool anyMonitorNeedsOverlay = false;
        for (auto& ctx : g_monitors) {
            if (!ctx.enabled) continue;
            const auto& cc = ctx.isHDREnabled ? ctx.hdrColorCorrection : ctx.sdrColorCorrection;
            bool shaderPrimaries = cc.primariesEnabled;
            bool shaderGrayscale = cc.grayscale.enabled;
            bool shaderWhiteBalance = cc.primariesEnabled &&
                (cc.whiteBalanceGains[0] != 1.0f || cc.whiteBalanceGains[1] != 1.0f || cc.whiteBalanceGains[2] != 1.0f);
            bool overlayNeeded = !ctx.usePassthrough              // Has LUT
                || shaderPrimaries || shaderGrayscale || shaderWhiteBalance  // Has corrections
                || (ctx.isHDREnabled && cc.tonemap.enabled)       // Has tonemap
                || g_desktopGammaMode.load();                     // Has desktop gamma
            if (overlayNeeded) {
                anyMonitorNeedsOverlay = true;
                break;
            }
        }

        if (!anyMonitorNeedsOverlay && !g_overlayAutoSleep.load()) {
            g_overlayAutoSleep.store(true);
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd) ShowWindow(ctx.hwnd, SW_HIDE);
            }
            std::cout << "Auto-sleep: overlay has nothing to do, hiding" << std::endl;
        } else if (anyMonitorNeedsOverlay && g_overlayAutoSleep.load()) {
            g_overlayAutoSleep.store(false);
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            // Don't force-show here — RenderMonitor's two-phase visibility handles it
            // Just mark dcompCommitted = false so windows go through proper show sequence
            for (auto& ctx : g_monitors) {
                if (ctx.hwnd && ctx.enabled) {
                    ctx.dcompCommitted = false;
                    ctx.framesAfterCommit = 0;
                }
            }
            std::cout << "Auto-sleep: overlay needed again, waking" << std::endl;
        }
    }

    // When auto-sleeping, skip rendering entirely — wait for wake event or 500ms timeout
    if (g_overlayAutoSleep.load()) {
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        if (g_overlayWakeEvent) {
            WaitForSingleObject(g_overlayWakeEvent, 500);
        } else {
            Sleep(50);
        }
        return;
    }

    for (auto& ctx : g_monitors) {
        if (ctx.enabled) {
            RenderMonitor(&ctx);
            activeCount++;
        }
    }
    // Only stop if ALL monitors have failed
    if (activeCount == 0 && !g_monitors.empty()) {
        std::cerr << "All monitors failed, stopping" << std::endl;
        g_running = false;
    }
}

// ============================================================================
// SECTION: Overlay Window Procedure
// ============================================================================

LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_DESTROY:
        if (hwnd == g_mainHwnd) {
            UnregisterHotKey(hwnd, HOTKEY_GAMMA);  // Gamma toggle
            UnregisterHotKey(hwnd, HOTKEY_ANALYSIS);  // Analysis toggle
            UnregisterHotKey(hwnd, HOTKEY_HDR_TOGGLE); // HDR toggle
            g_running = false;
            PostQuitMessage(0);
        }
        return 0;
    case WM_HOTKEY:
        // Wake auto-sleep on any hotkey (overlay may need to start rendering)
        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
        if (wParam == HOTKEY_GAMMA) {
            // Win+Shift+G - toggle gamma mode (HDR only)
            // Check if any monitor is in HDR mode
            bool anyHDR = false;
            for (const auto& ctx : g_monitors) {
                if (ctx.isHDREnabled) {
                    anyHDR = true;
                    break;
                }
            }
            if (anyHDR) {
                bool newMode = !g_desktopGammaMode.load();
                g_desktopGammaMode.store(newMode);
                // Update user preference to match manual toggle
                // This ensures whitelist respects the user's manual choice
                g_userDesktopGammaMode.store(newMode);
                // If user toggled while whitelist was active, set override flag
                // Override persists until the whitelisted app exits
                if (g_gammaWhitelistActive.load()) {
                    std::wstring overrideProcess;
                    {
                        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                        // Store the matched process name (lowercase) for tracking
                        g_gammaWhitelistOverrideProcess = g_gammaWhitelistMatch;
                        for (wchar_t& c : g_gammaWhitelistOverrideProcess) {
                            c = towlower(c);
                        }
                        overrideProcess = g_gammaWhitelistOverrideProcess;
                        g_gammaWhitelistMatch.clear();
                    }
                    g_gammaWhitelistUserOverride.store(true);
                    g_gammaWhitelistActive.store(false);
                    std::wcout << L"Gamma whitelist: user override active until " << overrideProcess << L" exits" << std::endl;
                }
                std::cout << "Gamma mode: " << (newMode ? "Desktop (2.2)" : "Content (sRGB)") << std::endl;
                ShowOSD(newMode ? L"Gamma: 2.2" : L"Gamma: sRGB");
            }
            // Silent ignore if no HDR monitors
        }
        else if (wParam == HOTKEY_ANALYSIS) {
            // Win+Shift+X - toggle analysis overlay
            ToggleAnalysisOverlay();
        }
        else if (wParam == HOTKEY_HDR_TOGGLE) {
            // Win+Shift+H - toggle HDR on focused monitor
            if (ToggleHdrOnFocusedMonitor()) {
                // On ACM displays, HDR→SDR keeps FP16 format so format-change detection
                // doesn't fire. Schedule a delayed reinit to re-detect HDR state after
                // Windows completes the transition (no render thread blocking).
                SetTimer(hwnd, HDR_REINIT_TIMER_ID, HDR_REINIT_DELAY_MS, nullptr);
            }
        }
        return 0;
    case WM_TIMER:
        if (wParam == HDR_REINIT_TIMER_ID) {
            KillTimer(hwnd, HDR_REINIT_TIMER_ID);
            // Release duplication so render loop re-detects HDR state and reapplies MHC profiles
            for (auto& ctx : g_monitors) {
                if (ctx.duplication) {
                    ctx.duplication->Release();
                    ctx.duplication = nullptr;
                }
                ctx.consecutiveFailures = 0;
            }
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            return 0;
        }
        HideOSD();
        return 0;
    case WM_WINDOWPOSCHANGING: {
        // If another window is changing our z-order, trigger TOPMOST reassertion
        WINDOWPOS* wp = reinterpret_cast<WINDOWPOS*>(lParam);
        if (wp && !(wp->flags & SWP_NOZORDER)) {
            g_forceTopmostReassert.store(true);
        }
        break;
    }
    case WM_POWERBROADCAST:
        // Handle power events for sleep/wake recovery
        if (wParam == PBT_APMRESUMEAUTOMATIC || wParam == PBT_APMRESUMESUSPEND) {
            std::cout << "System power resume detected, forcing reinit..." << std::endl;
            g_forceReinit.store(true);
        }
        // Handle display power state changes (sleep/wake of monitor only)
        else if (wParam == PBT_POWERSETTINGCHANGE) {
            POWERBROADCAST_SETTING* pbs = reinterpret_cast<POWERBROADCAST_SETTING*>(lParam);
            if (pbs && pbs->PowerSetting == GUID_CONSOLE_DISPLAY_STATE_LOCAL) {
                DWORD displayState = *reinterpret_cast<DWORD*>(pbs->Data);
                // 0 = off, 1 = on, 2 = dimmed
                if (displayState == 1) {
                    // Ignore spurious "display on" notification that fires immediately
                    // after registering (display is already on at startup)
                    auto elapsed = std::chrono::steady_clock::now() - g_powerNotifyRegisteredTime;
                    if (elapsed < std::chrono::seconds(3)) {
                        std::cout << "Ignoring initial display power notification" << std::endl;
                        g_displayOff.store(false);
                        return TRUE;
                    }
                    std::cout << "Display waking from sleep, forcing reinit..." << std::endl;
                    g_displayOff.store(false);
                    g_forceReinit.store(true);
                } else if (displayState == 0) {
                    std::cout << "Display entering sleep mode" << std::endl;
                    g_displayOff.store(true);
                    // Reset watchdog to prevent timeout during display sleep
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                }
            }
        }
        return TRUE;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void CleanupMonitorContext(MonitorContext* ctx) {
    // Release D3D resources (keeps hwnd)
    ReleaseMonitorD3DResources(ctx);
    // Also destroy the window for full cleanup
    if (ctx->hwnd) { DestroyWindow(ctx->hwnd); ctx->hwnd = nullptr; }
}
