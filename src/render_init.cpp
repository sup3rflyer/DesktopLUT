// DesktopLUT - render_init.cpp
// Swapchain, DirectComposition, resource initialization, and display power management

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
#include "gui_mhc.h"
#include <dwmapi.h>
#include <avrt.h>
#include <iostream>
#include <iomanip>
#include <algorithm>

// ============================================================================
// SECTION: Compositor Clock & Display Power
// ============================================================================

// Compositor Clock API (Windows 11+) for VBlank-aligned frame timing
// Dynamically loaded to maintain compatibility with older Windows
PFN_DCompositionWaitForCompositorClock g_pfnWaitForCompositorClock = nullptr;
PFN_DCompositionGetFrameId g_pfnDCompGetFrameId = nullptr;
PFN_DCompositionGetStatistics g_pfnDCompGetStatistics = nullptr;

void InitCompositorClock() {
    HMODULE hDcomp = GetModuleHandleW(L"dcomp.dll");
    if (hDcomp) {
        g_pfnWaitForCompositorClock = (PFN_DCompositionWaitForCompositorClock)
            GetProcAddress(hDcomp, "DCompositionWaitForCompositorClock");
    }
    if (g_pfnWaitForCompositorClock) {
        std::cout << "Frame sync: Compositor Clock API (VBlank-aligned)" << std::endl;
    } else {
        std::cout << "Frame sync: DwmFlush fallback (post-composition)" << std::endl;
    }

    // Load DComposition frame statistics API (Win11+)
    if (hDcomp) {
        g_pfnDCompGetFrameId = (PFN_DCompositionGetFrameId)
            GetProcAddress(hDcomp, "DCompositionGetFrameId");
        g_pfnDCompGetStatistics = (PFN_DCompositionGetStatistics)
            GetProcAddress(hDcomp, "DCompositionGetStatistics");
        if (g_pfnDCompGetFrameId && g_pfnDCompGetStatistics) {
            std::cout << "Frame pacer: DComposition frame statistics API available" << std::endl;
        }
    }
}

// Motion bar time origin (set on first frame)
std::chrono::steady_clock::time_point s_motionBarOrigin;
bool s_motionBarOriginSet = false;

// Watchdog recovery attempt counter (reset on successful frame)
int s_watchdogRecoveryAttempts = 0;

// Display power notification handle
static HPOWERNOTIFY g_displayPowerNotify = nullptr;
std::chrono::steady_clock::time_point g_powerNotifyRegisteredTime;

// GUID_CONSOLE_DISPLAY_STATE - notifies when display goes on/off/dimmed
// {6FE69556-704A-47A0-8F24-C28D936FDA47}
const GUID GUID_CONSOLE_DISPLAY_STATE_LOCAL =
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

    // Determine our output peak brightness
    // If tonemapping enabled: we handle tonemapping, tell Windows max (10000) so it passes through
    // If tonemapping disabled: content is unclamped, tell Windows max so it applies system tonemapping based on MaxTML
    // In both cases, 10000 nits ensures Windows uses MaxTML setting to decide tonemapping behavior
    float contentPeakNits = 10000.0f;

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
    scd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
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
    // Note: We don't use the waitable object for pacing (compositor sync in RenderAll handles it)
    // but SetMaximumFrameLatency still limits the present queue to prevent frame buildup
    ctx->swapchain->SetMaximumFrameLatency(1);

    // Create RTV
    ID3D11Texture2D* backBuffer = nullptr;
    hr = ctx->swapchain->GetBuffer(0, IID_PPV_ARGS(&backBuffer));
    if (FAILED(hr) || !backBuffer) {
        std::cerr << "Failed to get swapchain back buffer: 0x" << std::hex << hr << std::dec << std::endl;
        ctx->swapchain->Release(); ctx->swapchain = nullptr;
        return false;
    }
    hr = g_device->CreateRenderTargetView(backBuffer, nullptr, &ctx->rtv);
    backBuffer->Release();
    if (FAILED(hr)) {
        std::cerr << "Failed to create RTV: 0x" << std::hex << hr << std::dec << std::endl;
        ctx->swapchain->Release(); ctx->swapchain = nullptr;
        return false;
    }

    // Create frame buffer texture for experimental 1-frame buffer mode
    // Same format/size as swapchain — shader renders here, then CopyResource to backbuffer on next cycle
    {
        D3D11_TEXTURE2D_DESC bufDesc = {};
        bufDesc.Width = ctx->width;
        bufDesc.Height = ctx->height;
        bufDesc.MipLevels = 1;
        bufDesc.ArraySize = 1;
        bufDesc.Format = ctx->swapchainFormat;
        bufDesc.SampleDesc.Count = 1;
        bufDesc.Usage = D3D11_USAGE_DEFAULT;
        bufDesc.BindFlags = D3D11_BIND_RENDER_TARGET;

        hr = g_device->CreateTexture2D(&bufDesc, nullptr, &ctx->bufferTexture);
        if (SUCCEEDED(hr)) {
            hr = g_device->CreateRenderTargetView(ctx->bufferTexture, nullptr, &ctx->bufferRTV);
            if (FAILED(hr)) {
                ctx->bufferTexture->Release();
                ctx->bufferTexture = nullptr;
            }
        }
        ctx->bufferReady = false;
        // Non-fatal: buffer mode just won't work if creation fails
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

bool ResizeSwapChain(MonitorContext* ctx, int width, int height) {
    if (ctx->rtv) {
        ctx->rtv->Release();
        ctx->rtv = nullptr;
    }
    // Release frame buffer (will be recreated at new size)
    if (ctx->bufferRTV) { ctx->bufferRTV->Release(); ctx->bufferRTV = nullptr; }
    if (ctx->bufferTexture) { ctx->bufferTexture->Release(); ctx->bufferTexture = nullptr; }
    ctx->bufferReady = false;

    UINT flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
    if (g_tearingSupported) flags |= DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING;
    HRESULT hr = ctx->swapchain->ResizeBuffers(2, width, height,
        ctx->swapchainFormat, flags);

    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " ResizeBuffers failed: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    ID3D11Texture2D* backBuffer = nullptr;
    hr = ctx->swapchain->GetBuffer(0, IID_PPV_ARGS(&backBuffer));
    if (FAILED(hr) || !backBuffer) {
        std::cerr << "Monitor " << ctx->index << " GetBuffer failed after resize: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }
    hr = g_device->CreateRenderTargetView(backBuffer, nullptr, &ctx->rtv);
    backBuffer->Release();
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " CreateRTV failed after resize: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    ctx->width = width;
    ctx->height = height;

    // Recreate frame buffer at new size
    {
        D3D11_TEXTURE2D_DESC bufDesc = {};
        bufDesc.Width = width;
        bufDesc.Height = height;
        bufDesc.MipLevels = 1;
        bufDesc.ArraySize = 1;
        bufDesc.Format = ctx->swapchainFormat;
        bufDesc.SampleDesc.Count = 1;
        bufDesc.Usage = D3D11_USAGE_DEFAULT;
        bufDesc.BindFlags = D3D11_BIND_RENDER_TARGET;

        hr = g_device->CreateTexture2D(&bufDesc, nullptr, &ctx->bufferTexture);
        if (SUCCEEDED(hr)) {
            hr = g_device->CreateRenderTargetView(ctx->bufferTexture, nullptr, &ctx->bufferRTV);
            if (FAILED(hr)) {
                ctx->bufferTexture->Release();
                ctx->bufferTexture = nullptr;
            }
        }
    }
    return true;
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
    if (ctx->bufferRTV) { ctx->bufferRTV->Release(); ctx->bufferRTV = nullptr; }
    if (ctx->bufferTexture) { ctx->bufferTexture->Release(); ctx->bufferTexture = nullptr; }
    ctx->bufferReady = false;
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
void ReapplyMhcProfilesOnModeSwitch(MonitorContext* ctx) {
    // Snapshot MHC settings under lock (render thread reads, GUI thread writes)
    bool mhcEnabled = false;
    std::wstring profileName;
    bool isHDR = ctx->isHDREnabled;
    bool sdrPrimEn = false, sdrGsEn = false, hdrPrimEn = false, hdrGsEn = false;
    std::wstring sdrName, hdrName, sdrProfName, hdrProfName;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (ctx->index >= (int)g_gui.monitorSettings.size()) return;
        const auto& ms = g_gui.monitorSettings[ctx->index];
        const auto& mhc = isHDR ? ms.hdrMHC : ms.sdrMHC;
        mhcEnabled = mhc.enabled;
        profileName = mhc.profileName;
        bool sdrA = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty();
        bool hdrA = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty();
        bool sdrHasGs = ms.sdrMHC.baseGrayscale.enabled || ms.sdrMHC.correctionGrayscale.enabled ||
                        ms.sdrMHC.hasPerChannelTRC || !ms.sdrMHC.sourceFilePath.empty();
        bool hdrHasGs = ms.hdrMHC.baseGrayscale.enabled || ms.hdrMHC.correctionGrayscale.enabled ||
                        ms.hdrMHC.hasPerChannelTRC || !ms.hdrMHC.sourceFilePath.empty() ||
                        ms.hdrMHC.desktopGammaEnabled;
        sdrPrimEn = sdrA && ms.sdrMHC.primariesEnabled;
        sdrGsEn   = sdrA && sdrHasGs;
        hdrPrimEn = hdrA && ms.hdrMHC.primariesEnabled;
        hdrGsEn   = hdrA && hdrHasGs;
    }

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(ctx->index, displayInfo)) return;

    // Remove + re-add profile for current mode to force Windows to apply it
    if (mhcEnabled && !profileName.empty()) {
        std::wcout << L"Mode switch: reapplying " << (isHDR ? L"HDR" : L"SDR")
                   << L" MHC profile '" << profileName
                   << L"' for monitor " << ctx->index << std::endl;
        RemoveMHC2Profile(profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        ReassociateMHC2Profile(profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
    }

    // Update MHC flags to match current settings
    ctx->sdrMhcPrimariesActive = sdrPrimEn;
    ctx->sdrMhcGrayscaleActive = sdrGsEn;
    ctx->hdrMhcPrimariesActive = hdrPrimEn;
    ctx->hdrMhcGrayscaleActive = hdrGsEn;
}

// Maximum recovery retries before giving up on a monitor (~5 min at 5s backoff cap)
const int MAX_RECOVERY_RETRIES = 60;

// ============================================================================
// SECTION: ICtCp Grayscale Conversion (CPU precomputation)
// ============================================================================

// Rec.2020 → LMS matrix (Dolby, matches shader Rec2020_to_LMS)
static const float kRec2020ToLMS[9] = {
    0.41210938f, 0.52392578f, 0.06396484f,
    0.16674805f, 0.72045898f, 0.11279297f,
    0.02416992f, 0.07543945f, 0.90039063f
};

// L'M'S' → ICtCp matrix (Dolby, matches shader LMSprime_to_ICtCp)
static const float kLMSprimeToICtCp[9] = {
    0.50000000f,  0.50000000f,  0.00000000f,
    1.61376953f, -3.32348633f,  1.70971680f,
    4.37817383f, -4.24560547f, -0.13256836f
};

// Convert per-channel PQ corrections → ICtCp delta offsets for all 32 points.
// For each point: builds corrected Rec.2020 (R≠G≠B from per-channel corrections),
// transforms through LMS→PQ→ICtCp, and subtracts neutral reference (equal RGB).
// Result: additive deltas that can be applied in ICtCp space during the combined pass.
// Identity: when R=G=B (all deviations 1.0), deltas are zero (LMS row sums = 1.0).
void ComputeGrayscaleICtCpOffsets(GrayscaleData& gs) {
    const float pqPeak = LinearToPQScalar((std::max)(gs.peakNits, 1.0f) / 10000.0f);

    for (int i = 0; i < 32; i++) {
        float t = (gs.pointCount > 1) ? (float)i / (float)(gs.pointCount - 1) : 0.0f;

        // Get per-channel normalized PQ corrections (same values sent to shader in PQ mode)
        float nR = (i < gs.pointCount) ? gs.pointsR[i] : t;
        float nG = (i < gs.pointCount) ? gs.pointsG[i] : t;
        float nB = (i < gs.pointCount) ? gs.pointsB[i] : t;

        // Safety: if uninitialized (zero beyond first point), use identity
        if (i > 0 && nR == 0.0f) nR = t;
        if (i > 0 && nG == 0.0f) nG = t;
        if (i > 0 && nB == 0.0f) nB = t;

        // Corrected PQ values (scaled by peak)
        float pqR = nR * pqPeak;
        float pqG = nG * pqPeak;
        float pqB = nB * pqPeak;

        // Neutral PQ value (equal R=G=B at this point)
        float pqNeutral = t * pqPeak;

        // --- Corrected path: per-channel PQ → linear → LMS → PQ → ICtCp ---
        float linR = PQToLinearScalar(pqR);
        float linG = PQToLinearScalar(pqG);
        float linB = PQToLinearScalar(pqB);

        // Rec.2020 → LMS (matrix multiply)
        float lmsL = kRec2020ToLMS[0] * linR + kRec2020ToLMS[1] * linG + kRec2020ToLMS[2] * linB;
        float lmsM = kRec2020ToLMS[3] * linR + kRec2020ToLMS[4] * linG + kRec2020ToLMS[5] * linB;
        float lmsS = kRec2020ToLMS[6] * linR + kRec2020ToLMS[7] * linG + kRec2020ToLMS[8] * linB;

        // LMS → PQ (per-component, with 80/10000 normalization already in linear values)
        float lmsPqL = LinearToPQScalar((std::max)(lmsL, 1e-10f));
        float lmsPqM = LinearToPQScalar((std::max)(lmsM, 1e-10f));
        float lmsPqS = LinearToPQScalar((std::max)(lmsS, 1e-10f));

        // L'M'S' → ICtCp
        float corrI  = kLMSprimeToICtCp[0] * lmsPqL + kLMSprimeToICtCp[1] * lmsPqM + kLMSprimeToICtCp[2] * lmsPqS;
        float corrCt = kLMSprimeToICtCp[3] * lmsPqL + kLMSprimeToICtCp[4] * lmsPqM + kLMSprimeToICtCp[5] * lmsPqS;
        float corrCp = kLMSprimeToICtCp[6] * lmsPqL + kLMSprimeToICtCp[7] * lmsPqM + kLMSprimeToICtCp[8] * lmsPqS;

        // --- Neutral path: equal R=G=B at neutral PQ ---
        float linN = PQToLinearScalar(pqNeutral);

        float lmsNL = kRec2020ToLMS[0] * linN + kRec2020ToLMS[1] * linN + kRec2020ToLMS[2] * linN;
        float lmsNM = kRec2020ToLMS[3] * linN + kRec2020ToLMS[4] * linN + kRec2020ToLMS[5] * linN;
        float lmsNS = kRec2020ToLMS[6] * linN + kRec2020ToLMS[7] * linN + kRec2020ToLMS[8] * linN;

        float lmsPqNL = LinearToPQScalar((std::max)(lmsNL, 1e-10f));
        float lmsPqNM = LinearToPQScalar((std::max)(lmsNM, 1e-10f));
        float lmsPqNS = LinearToPQScalar((std::max)(lmsNS, 1e-10f));

        float neutI  = kLMSprimeToICtCp[0] * lmsPqNL + kLMSprimeToICtCp[1] * lmsPqNM + kLMSprimeToICtCp[2] * lmsPqNS;
        float neutCt = kLMSprimeToICtCp[3] * lmsPqNL + kLMSprimeToICtCp[4] * lmsPqNM + kLMSprimeToICtCp[5] * lmsPqNS;
        float neutCp = kLMSprimeToICtCp[6] * lmsPqNL + kLMSprimeToICtCp[7] * lmsPqNM + kLMSprimeToICtCp[8] * lmsPqNS;

        // Store deltas
        gs.ictcpI[i]  = corrI  - neutI;
        gs.ictcpCt[i] = corrCt - neutCt;
        gs.ictcpCp[i] = corrCp - neutCp;
    }

    gs.ictcpValid = true;
}
