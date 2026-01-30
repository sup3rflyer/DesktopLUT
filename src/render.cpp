// DesktopLUT - render.cpp
// Frame rendering, swapchain, and DirectComposition

#include "render.h"
#include "globals.h"
#include "gpu.h"
#include "capture.h"
#include "osd.h"
#include "analysis.h"
#include "displayconfig.h"
#include "processing.h"
#include <dwmapi.h>
#include <tlhelp32.h>
#include <iostream>
#include <iomanip>
#include <algorithm>
#include <cctype>
#include <thread>

// Thread handle for gamma whitelist polling
static std::thread g_gammaWhitelistThread;

// Display power notification handle
static HPOWERNOTIFY g_displayPowerNotify = nullptr;

// GUID_CONSOLE_DISPLAY_STATE - notifies when display goes on/off/dimmed
// {6FE69556-704A-47A0-8F24-C28D936FDA47}
static const GUID GUID_CONSOLE_DISPLAY_STATE_LOCAL =
    { 0x6fe69556, 0x704a, 0x47a0, { 0x8f, 0x24, 0xc2, 0x8d, 0x93, 0x6f, 0xda, 0x47 } };

void RegisterDisplayPowerNotification(HWND hwnd) {
    if (g_displayPowerNotify) return;  // Already registered

    g_displayPowerNotify = RegisterPowerSettingNotification(
        hwnd, &GUID_CONSOLE_DISPLAY_STATE_LOCAL, DEVICE_NOTIFY_WINDOW_HANDLE);

    if (g_displayPowerNotify) {
        std::cout << "Registered for display power state notifications" << std::endl;
    }
}

void UnregisterDisplayPowerNotification() {
    if (g_displayPowerNotify) {
        UnregisterPowerSettingNotification(g_displayPowerNotify);
        g_displayPowerNotify = nullptr;
    }
}

// Check if any whitelisted process is running and update gamma state accordingly
// Returns true if a whitelisted process was found
static bool CheckGammaWhitelist() {
    // Copy whitelist data under lock for thread-safe access
    std::vector<std::wstring> localWhitelist;
    std::wstring localOverrideProcess;
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        localWhitelist = g_gammaWhitelist;
        localOverrideProcess = g_gammaWhitelistOverrideProcess;
    }

    // Early exit conditions - only check when:
    // 1. Whitelist is populated
    // 2. User has gamma enabled (checkbox checked)
    // 3. At least one monitor is in HDR mode
    if (localWhitelist.empty() || !g_userDesktopGammaMode.load()) {
        if (g_gammaWhitelistActive.load()) {
            // Was active, now conditions changed - restore user preference
            g_gammaWhitelistActive.store(false);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch.clear();
            }
            g_desktopGammaMode.store(g_userDesktopGammaMode.load());
        }
        // Also clear override if conditions no longer apply
        if (g_gammaWhitelistUserOverride.load()) {
            g_gammaWhitelistUserOverride.store(false);
            std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
            g_gammaWhitelistOverrideProcess.clear();
        }
        return false;
    }

    // Check if any monitor is in HDR mode
    bool anyHDR = false;
    for (const auto& ctx : g_monitors) {
        if (ctx.isHDREnabled) {
            anyHDR = true;
            break;
        }
    }
    if (!anyHDR) {
        if (g_gammaWhitelistActive.load()) {
            g_gammaWhitelistActive.store(false);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch.clear();
            }
            g_desktopGammaMode.store(g_userDesktopGammaMode.load());
        }
        // Also clear override if conditions no longer apply
        if (g_gammaWhitelistUserOverride.load()) {
            g_gammaWhitelistUserOverride.store(false);
            std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
            g_gammaWhitelistOverrideProcess.clear();
        }
        return false;
    }

    // Enumerate running processes
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) {
        return false;
    }

    PROCESSENTRY32W pe32;
    pe32.dwSize = sizeof(pe32);

    bool found = false;
    std::wstring matchedProcess;
    bool overrideProcessStillRunning = false;

    // Helper: case-insensitive length-limited compare
    auto matchesPattern = [](const wchar_t* str, size_t strLen, const std::wstring& pattern) -> bool {
        // Compare without .exe extension
        size_t baseLen = strLen;
        if (baseLen > 4 && _wcsnicmp(str + baseLen - 4, L".exe", 4) == 0) {
            baseLen -= 4;
        }
        // Match pattern against base name (without extension)
        if (baseLen == pattern.size() && _wcsnicmp(str, pattern.c_str(), baseLen) == 0) {
            return true;
        }
        // Match pattern against full name (with extension)
        if (strLen == pattern.size() && _wcsnicmp(str, pattern.c_str(), strLen) == 0) {
            return true;
        }
        // Match pattern+.exe against full name
        if (strLen == pattern.size() + 4 && _wcsnicmp(str, pattern.c_str(), pattern.size()) == 0 &&
            _wcsnicmp(str + pattern.size(), L".exe", 4) == 0) {
            return true;
        }
        return false;
    };

    if (Process32FirstW(snapshot, &pe32)) {
        do {
            const wchar_t* exeName = pe32.szExeFile;
            size_t exeLen = wcslen(exeName);

            // Check if the override process is still running
            if (g_gammaWhitelistUserOverride.load() && !localOverrideProcess.empty()) {
                if (matchesPattern(exeName, exeLen, localOverrideProcess)) {
                    overrideProcessStillRunning = true;
                }
            }

            // Check against whitelist (case-insensitive matching)
            for (const auto& pattern : localWhitelist) {
                if (matchesPattern(exeName, exeLen, pattern)) {
                    found = true;
                    matchedProcess = pe32.szExeFile;  // Original case for display
                    break;
                }
            }
            // Don't break early - need to check if override process is still running too
        } while (Process32NextW(snapshot, &pe32));
    }

    CloseHandle(snapshot);

    // Handle user override: if user manually toggled while whitelist was active,
    // the override persists until the whitelisted app that triggered it exits
    if (g_gammaWhitelistUserOverride.load()) {
        if (!overrideProcessStillRunning) {
            // Override process has exited - clear override and resume normal whitelist behavior
            std::wcout << L"Gamma whitelist: override process " << localOverrideProcess << L" exited, resuming normal whitelist" << std::endl;
            g_gammaWhitelistUserOverride.store(false);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistOverrideProcess.clear();
            }
            // Continue to normal whitelist handling below
        } else {
            // Override process still running - don't trigger whitelist
            return found;
        }
    }

    // Update state based on result
    bool wasActive = g_gammaWhitelistActive.load();
    if (found) {
        if (!wasActive) {
            // Just detected whitelisted app - disable gamma
            g_gammaWhitelistActive.store(true);
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                g_gammaWhitelistMatch = matchedProcess;
            }
            g_desktopGammaMode.store(false);
            std::wcout << L"Gamma whitelist: detected " << matchedProcess << L", disabling desktop gamma" << std::endl;
            ShowOSD(L"Gamma: sRGB");
        }
    } else {
        if (wasActive) {
            // Whitelisted app exited - restore user preference
            g_gammaWhitelistActive.store(false);
            std::wstring exitedProcess;
            {
                std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
                exitedProcess = g_gammaWhitelistMatch;
                g_gammaWhitelistMatch.clear();
            }
            std::wcout << L"Gamma whitelist: " << exitedProcess << L" exited, restoring desktop gamma" << std::endl;
            g_desktopGammaMode.store(g_userDesktopGammaMode.load());
            ShowOSD(g_userDesktopGammaMode.load() ? L"Gamma: 2.2" : L"Gamma: sRGB");
        }
    }

    return found;
}

// Dedicated thread function for gamma whitelist polling
// Runs every 500ms to avoid impacting frame timing
static void GammaWhitelistThreadFunc() {
    // Initial delay - let processing fully initialize before first check
    // Matches the original 500ms delay from inline check timing
    for (int i = 0; i < 10 && g_gammaWhitelistThreadRunning.load(); i++) {
        Sleep(50);  // 500ms total, in chunks for responsive shutdown
    }

    while (g_gammaWhitelistThreadRunning.load()) {
        CheckGammaWhitelist();

        // Sleep in small chunks to allow quick exit on shutdown
        for (int i = 0; i < 10 && g_gammaWhitelistThreadRunning.load(); i++) {
            Sleep(50);  // 10 x 50ms = 500ms total
        }
    }
}

void StartGammaWhitelistThread() {
    if (g_gammaWhitelistThreadRunning.load()) return;  // Already running

    g_gammaWhitelistThreadRunning.store(true);
    g_gammaWhitelistThread = std::thread(GammaWhitelistThreadFunc);
}

void StopGammaWhitelistThread() {
    if (!g_gammaWhitelistThreadRunning.load()) return;  // Not running

    g_gammaWhitelistThreadRunning.store(false);
    if (g_gammaWhitelistThread.joinable()) {
        g_gammaWhitelistThread.join();
    }

    // Reset state when thread stops
    g_gammaWhitelistActive.store(false);
    g_gammaWhitelistUserOverride.store(false);
    {
        std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
        g_gammaWhitelistMatch.clear();
        g_gammaWhitelistOverrideProcess.clear();
    }
}

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
    // HDR: FP16 scRGB for linear HDR content
    // SDR: R10G10B10A2 for 10-bit output (reduces banding after LUT)
    ctx->swapchainFormat = ctx->isHDREnabled ?
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
    // HDR: scRGB linear (G10 = linear gamma, P709 = BT.709 primaries)
    // SDR: sRGB (G22 = 2.2 gamma, P709 = BT.709 primaries)
    ctx->colorSpace = ctx->isHDREnabled ?
        DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709 :
        DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;

    UINT colorSpaceSupport = 0;
    hr = ctx->swapchain->CheckColorSpaceSupport(ctx->colorSpace, &colorSpaceSupport);
    if (SUCCEEDED(hr) && (colorSpaceSupport & DXGI_SWAP_CHAIN_COLOR_SPACE_SUPPORT_FLAG_PRESENT)) {
        hr = ctx->swapchain->SetColorSpace1(ctx->colorSpace);
        if (SUCCEEDED(hr)) {
            std::cout << "Monitor " << ctx->index << " color space: " << (ctx->isHDREnabled ? "scRGB linear (HDR)" : "sRGB (SDR)") << std::endl;
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

void RenderMonitor(MonitorContext* ctx) {
    // Entry validation - skip if monitor is disabled
    if (!ctx || !ctx->enabled) return;

    // If resources are missing (failed reinit), try to recover with backoff
    if (!ctx->duplication) {
        ctx->consecutiveFailures++;
        // Fast initial retry (50ms), exponential backoff to 5s for prolonged failures
        int backoffMs = (std::min)(50 * (1 << (std::min)(ctx->consecutiveFailures - 1, 7)), 5000);
        Sleep(backoffMs);

        if (ctx->consecutiveFailures % 10 == 0) {
            std::cout << "Monitor " << ctx->index << " attempting recovery, attempt "
                      << ctx->consecutiveFailures << "..." << std::endl;
        }

        if (InitDesktopDuplication(ctx)) {
            std::cout << "Monitor " << ctx->index << " recovery success" << std::endl;
            ctx->consecutiveFailures = 0;
            // Window will be shown after first successful frame render
        }
        return;
    }

    if (!ctx->swapchain || !ctx->rtv) return;

    // Acquire next frame from desktop duplication
    // First try with 0 timeout for immediate response to desktop changes (menus, etc.)
    // If no frame ready, use DwmFlush for pacing then wait with normal timeout
    DXGI_OUTDUPL_FRAME_INFO frameInfo;
    IDXGIResource* desktopResource = nullptr;
    bool frameAcquired = false;

    HRESULT hr = ctx->duplication->AcquireNextFrame(0, &frameInfo, &desktopResource);
    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        // No frame immediately available - sync to DWM then wait
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
        if (ctx->dcompCommitted && ctx->hwnd && !IsWindowVisible(ctx->hwnd)) {
            ctx->framesAfterCommit++;
            if (ctx->framesAfterCommit >= 1) {
                SetLayeredWindowAttributes(ctx->hwnd, 0, 255, LWA_ALPHA);
                ShowWindow(ctx->hwnd, SW_SHOWNA);
            }
        }
        return;
    } else if (hr == DXGI_ERROR_ACCESS_LOST || FAILED(hr)) {
        // Desktop duplication lost or other error - hide overlay and retry with backoff
        if (ctx->hwnd && IsWindowVisible(ctx->hwnd)) {
            ShowWindow(ctx->hwnd, SW_HIDE);
        }

        ctx->consecutiveFailures++;

        // Exponential backoff: 50ms, 100ms, 200ms, 400ms, 800ms, 1600ms, 3200ms, max 5s
        // Fast initial retry for transient issues, backs off for secure desktop (UAC) recovery
        int backoffMs = (std::min)(50 * (1 << (std::min)(ctx->consecutiveFailures - 1, 7)), 5000);
        Sleep(backoffMs);

        // Log occasionally (not every attempt)
        if (ctx->consecutiveFailures == 1 || ctx->consecutiveFailures % 10 == 0) {
            std::cout << "Monitor " << ctx->index << " duplication lost (0x" << std::hex << hr << std::dec
                      << "), attempt " << ctx->consecutiveFailures << "..." << std::endl;
        }

        // Try to reinit - never give up, secure desktop can take a while
        if (ReinitDesktopDuplication(ctx)) {
            std::cout << "Monitor " << ctx->index << " reinit success" << std::endl;
            ctx->consecutiveFailures = 0;

            if (ctx->isHDREnabled != ctx->wasHDREnabled) {
                // Passthrough if no applicable LUT for current mode:
                // - SDR mode: need SDR LUT (SDR LUTs expect sRGB input)
                // - HDR mode: need HDR LUT (HDR LUTs expect PQ Rec.2020 input)
                // No fallback - SDR and HDR LUTs are incompatible
                bool hasApplicableLUT = ctx->isHDREnabled
                    ? (ctx->lutSRV_HDR != nullptr)
                    : (ctx->lutSRV_SDR != nullptr);
                ctx->usePassthrough = !hasApplicableLUT;
                // RecreateSwapchain sets HDR metadata via CreateSwapChain -> UpdateHDRMetadata
                RecreateSwapchain(ctx);
                // Reapply MaxTML settings (may be lost after HDR mode change)
                ApplyMaxTmlSettings();
            }
            ctx->wasHDREnabled = ctx->isHDREnabled;
        }
        return;
    }

    frameAcquired = true;

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
        // Force reinit to properly detect new HDR state and recreate swapchain
        if (ctx->duplication) {
            ctx->duplication->Release();
            ctx->duplication = nullptr;
        }
        if (ReinitDesktopDuplication(ctx)) {
            // HDR state changed - update swapchain and settings
            bool hasApplicableLUT = ctx->isHDREnabled
                ? (ctx->lutSRV_HDR != nullptr)
                : (ctx->lutSRV_SDR != nullptr);
            ctx->usePassthrough = !hasApplicableLUT;
            RecreateSwapchain(ctx);
            ApplyMaxTmlSettings();
            ctx->wasHDREnabled = ctx->isHDREnabled;
            std::cout << "Monitor " << ctx->index << " switched to " << (ctx->isHDREnabled ? "HDR" : "SDR") << " mode" << std::endl;
        }
        return;
    }

    // Create SRV for captured frame (must be done while frame is held)
    if (ctx->captureSRV) {
        ctx->captureSRV->Release();
        ctx->captureSRV = nullptr;
    }

    D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
    srvDesc.Format = texDesc.Format;
    srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    srvDesc.Texture2D.MipLevels = 1;
    hr = g_device->CreateShaderResourceView(frameTexture, &srvDesc, &ctx->captureSRV);
    frameTexture->Release();

    if (FAILED(hr)) {
        ctx->duplication->ReleaseFrame();
        return;
    }

    // Update constant buffer with current HDR state, gamma mode, and manual corrections
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
        cbData[7] = (cc.primariesEnabled || cc.grayscale.enabled) ? 1.0f : 0.0f;  // useManualCorrection
        // Row 2: Grayscale control + tonemapping toggles
        cbData[8] = (float)cc.grayscale.pointCount;
        cbData[9] = cc.grayscale.enabled ? 1.0f : 0.0f;
        cbData[10] = (ctx->isHDREnabled && cc.tonemap.enabled) ? 1.0f : 0.0f;  // tonemapEnabled
        cbData[11] = (float)static_cast<int>(cc.tonemap.curve);  // tonemapCurve
        // Row 3-5: Primaries matrix (3 rows as float4, w unused)
        cbData[12] = cc.primariesMatrix[0];
        cbData[13] = cc.primariesMatrix[1];
        cbData[14] = cc.primariesMatrix[2];
        cbData[15] = 0.0f;
        cbData[16] = cc.primariesMatrix[3];
        cbData[17] = cc.primariesMatrix[4];
        cbData[18] = cc.primariesMatrix[5];
        cbData[19] = 0.0f;
        cbData[20] = cc.primariesMatrix[6];
        cbData[21] = cc.primariesMatrix[7];
        cbData[22] = cc.primariesMatrix[8];
        cbData[23] = 0.0f;
        // Row 6: Tonemapping parameters
        cbData[24] = cc.tonemap.sourcePeakNits;  // tonemapSourcePeak
        cbData[25] = cc.tonemap.targetPeakNits;
        cbData[26] = cc.tonemap.dynamicPeak ? 1.0f : 0.0f;  // tonemapDynamic
        cbData[27] = cc.grayscale.use24Gamma ? 1.0f : 0.0f;  // grayscale24 (SDR 2.2->2.4 transform)
        // Row 7: Grayscale peak + padding (white balance now handled by Bradford in primaries matrix)
        cbData[28] = cc.grayscale.peakNits;  // grayscalePeakNits (HDR only)
        cbData[29] = 0.0f;  // padding
        cbData[30] = 0.0f;  // padding
        cbData[31] = 0.0f;  // padding
        // Row 8-15: Grayscale LUT (32 points packed into 8 float4s)
        for (int i = 0; i < 32; i++) {
            cbData[32 + i] = (i < cc.grayscale.pointCount)
                ? cc.grayscale.points[i]
                : ((float)i / 31.0f);  // Linear fallback
        }
        g_context->Unmap(g_constantBuffer, 0);
    }

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

            // Read detected peak for analysis overlay or debug logging
            bool needPeakReadback = g_analysisEnabled.load() || g_logPeakDetection.load();
            if (needPeakReadback) {
                // Throttle readback to once per second per monitor (analysis has its own display throttle)
                static std::chrono::steady_clock::time_point lastReadback[8] = {};
                auto now = std::chrono::steady_clock::now();
                int idx = ctx->index < 8 ? ctx->index : 0;

                if (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastReadback[idx]).count() >= 500) {
                    // Create staging texture on first use
                    if (!ctx->peakStagingTexture) {
                        D3D11_TEXTURE2D_DESC stagingDesc = {};
                        stagingDesc.Width = 1;
                        stagingDesc.Height = 1;
                        stagingDesc.MipLevels = 1;
                        stagingDesc.ArraySize = 1;
                        stagingDesc.Format = DXGI_FORMAT_R32_FLOAT;
                        stagingDesc.SampleDesc.Count = 1;
                        stagingDesc.Usage = D3D11_USAGE_STAGING;
                        stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                        g_device->CreateTexture2D(&stagingDesc, nullptr, &ctx->peakStagingTexture);
                    }

                    if (ctx->peakStagingTexture) {
                        g_context->CopyResource(ctx->peakStagingTexture, ctx->peakTexture);
                        D3D11_MAPPED_SUBRESOURCE mapped;
                        if (SUCCEEDED(g_context->Map(ctx->peakStagingTexture, 0, D3D11_MAP_READ, 0, &mapped))) {
                            float peakNits = *((float*)mapped.pData);  // Already in nits from compute shader
                            g_context->Unmap(ctx->peakStagingTexture, 0);
                            ctx->detectedPeakNits = peakNits;  // Store for analysis overlay
                            if (g_logPeakDetection.load()) {
                                std::cout << "Monitor " << ctx->index << " detected peak: "
                                          << std::fixed << std::setprecision(1) << peakNits << " nits" << std::endl;
                            }
                        }
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
        // Two-phase visibility: first commit DirectComposition, then show window on next frame
        // This prevents black flash by ensuring DirectComposition has processed the visual
        if (!ctx->dcompCommitted && g_dcompDevice) {
            // First successful frame: commit DirectComposition but don't show yet
            g_context->Flush();
            g_dcompDevice->Commit();
            ctx->dcompCommitted = true;
            ctx->framesAfterCommit = 0;  // Start counting frames after commit
        } else if (ctx->dcompCommitted && ctx->hwnd && !IsWindowVisible(ctx->hwnd)) {
            // Wait one frame after commit for DirectComposition to process, then show
            ctx->framesAfterCommit++;
            if (ctx->framesAfterCommit >= 1) {
                // Make window opaque and show it now that DirectComposition content is ready
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

void RenderAll() {
    int activeCount = 0;

    // Check device health periodically (not every frame - driver call has some overhead)
    static int deviceCheckCounter = 0;
    if (++deviceCheckCounter >= 60) {  // Check every ~60 frames (~1 second at 60Hz)
        deviceCheckCounter = 0;
        if (g_device) {
            HRESULT reason = g_device->GetDeviceRemovedReason();
            if (reason != S_OK) {
                std::cerr << "GPU device lost (TDR/driver crash): 0x" << std::hex << reason << std::dec << std::endl;
                // Hide all overlay windows immediately to prevent black screen
                for (auto& ctx : g_monitors) {
                    if (ctx.hwnd) {
                        ShowWindow(ctx.hwnd, SW_HIDE);
                    }
                }

                // Attempt recovery once
                if (AttemptDeviceRecovery()) {
                    // Recovery succeeded - reset watchdog and continue
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                    std::cout << "Resuming after TDR recovery" << std::endl;
                    return;
                }

                // Recovery failed - exit with error sound
                std::cerr << "TDR recovery failed, exiting" << std::endl;
                MessageBeep(MB_ICONERROR);
                for (auto& ctx : g_monitors) {
                    ctx.enabled = false;
                }
                g_running = false;
                return;
            }
        }
    }

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
        }
        // Reset watchdog to avoid timeout during recovery
        g_lastSuccessfulFrame = std::chrono::steady_clock::now();
        // Reapply MaxTML settings (may be lost after sleep/wake)
        ApplyMaxTmlSettings();
    }

    // Periodically reassert TOPMOST to prevent other windows pushing us down
    static auto lastTopmost = std::chrono::steady_clock::now();
    auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastTopmost).count() >= 100) {
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
        lastTopmost = now;
    }

    // Gamma whitelist is now checked on a separate thread (see GammaWhitelistThreadFunc)
    // The render loop just reads the atomic g_gammaWhitelistActive flag via constant buffer

    // Apply any pending color correction updates (fast path: skip mutex if no updates)
    if (g_hasPendingColorCorrections.load(std::memory_order_acquire)) {
        std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
        for (const auto& update : g_pendingColorCorrections) {
            if (update.monitorIndex >= 0 && update.monitorIndex < (int)g_monitors.size()) {
                auto& ctx = g_monitors[update.monitorIndex];
                if (update.isHDR) {
                    ctx.hdrColorCorrection = update.data;
                    // HDR metadata (MaxCLL=10000) is set once at swapchain creation
                    // and doesn't change based on color correction settings
                } else {
                    ctx.sdrColorCorrection = update.data;
                }
            }
        }
        g_pendingColorCorrections.clear();
        g_hasPendingColorCorrections.store(false, std::memory_order_release);
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
            ToggleHdrOnFocusedMonitor();
        }
        return 0;
    case WM_TIMER:
        HideOSD();
        return 0;
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
                    std::cout << "Display waking from sleep, forcing reinit..." << std::endl;
                    g_forceReinit.store(true);
                } else if (displayState == 0) {
                    std::cout << "Display entering sleep mode" << std::endl;
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
