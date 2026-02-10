// DesktopLUT - processing.cpp
// Processing thread management

#include "processing.h"
#include "globals.h"
#include "lut.h"
#include "color.h"
#include "render.h"
#include "capture.h"
#include "osd.h"
#include "analysis.h"
#include "settings.h"
#include "gui.h"
#include "gpu.h"
#include "displayconfig.h"
#include "mhc.h"
#include <objbase.h>
#include <iostream>
#include <map>
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>

// Monitor enumeration callback
BOOL CALLBACK MonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam) {
    auto* data = reinterpret_cast<std::vector<HMONITOR>*>(lParam);
    data->push_back(hMonitor);
    return TRUE;
}

ColorCorrectionData ConvertColorCorrection(const ColorCorrectionSettings& src, bool isHDR) {
    ColorCorrectionData dst;
    dst.primariesEnabled = src.primariesEnabled;
    dst.primariesPreset = src.primariesPreset;
    dst.customPrimaries.Rx = src.customPrimaries.Rx;
    dst.customPrimaries.Ry = src.customPrimaries.Ry;
    dst.customPrimaries.Gx = src.customPrimaries.Gx;
    dst.customPrimaries.Gy = src.customPrimaries.Gy;
    dst.customPrimaries.Bx = src.customPrimaries.Bx;
    dst.customPrimaries.By = src.customPrimaries.By;
    dst.customPrimaries.Wx = src.customPrimaries.Wx;
    dst.customPrimaries.Wy = src.customPrimaries.Wy;

    // === Gamut mapping matrix (primaries only, D65 white for both sides) ===
    if (src.primariesEnabled) {
        const DisplayPrimaries& userPrimRef = (src.primariesPreset == g_numPresetPrimaries - 1)
            ? src.customPrimaries : g_presetPrimaries[src.primariesPreset];

        // Content space primaries
        const auto& contentPrim = isHDR ? g_presetPrimaries[3] : g_presetPrimaries[0];

        // Source: content space + D65 white
        DisplayPrimariesData srcPrim = { contentPrim.Rx, contentPrim.Ry,
                                         contentPrim.Gx, contentPrim.Gy,
                                         contentPrim.Bx, contentPrim.By,
                                         0.3127f, 0.3290f };  // D65

        // Target: display primaries + D65 white (pure gamut mapping, no white shift)
        DisplayPrimariesData tgtPrim = { userPrimRef.Rx, userPrimRef.Ry,
                                         userPrimRef.Gx, userPrimRef.Gy,
                                         userPrimRef.Bx, userPrimRef.By,
                                         0.3127f, 0.3290f };  // D65

        CalculatePrimariesMatrix(srcPrim, tgtPrim, dst.primariesMatrix);
    } else {
        // Identity matrix (no gamut mapping)
        dst.primariesMatrix[0] = 1; dst.primariesMatrix[1] = 0; dst.primariesMatrix[2] = 0;
        dst.primariesMatrix[3] = 0; dst.primariesMatrix[4] = 1; dst.primariesMatrix[5] = 0;
        dst.primariesMatrix[6] = 0; dst.primariesMatrix[7] = 0; dst.primariesMatrix[8] = 1;
    }

    // === White balance gains (von Kries diagonal, independent of primaries) ===
    // Compute RGB gains that shift D65 → target white in content space
    // gains = contentXYZtoRGB * targetWhiteXYZ
    {
        float Wx = src.customPrimaries.Wx;
        float Wy = src.customPrimaries.Wy;
        if (Wy < 1e-6f) Wy = 1e-6f;
        bool isD65 = (fabs(Wx - 0.3127f) < 0.001f && fabs(Wy - 0.3290f) < 0.001f);
        if (isD65) {
            dst.whiteBalanceGains[0] = 1.0f;
            dst.whiteBalanceGains[1] = 1.0f;
            dst.whiteBalanceGains[2] = 1.0f;
        } else {
            // Target white in XYZ (Y=1)
            float tX = Wx / Wy;
            float tY = 1.0f;
            float tZ = (1.0f - Wx - Wy) / Wy;
            // Content space XYZ-to-RGB matrix (sRGB for SDR, Rec.2020 for HDR)
            // sRGB XYZ→RGB (IEC 61966-2-1)
            const float srgbXYZtoRGB[9] = {
                 3.2404542f, -1.5371385f, -0.4985314f,
                -0.9692660f,  1.8760108f,  0.0415560f,
                 0.0556434f, -0.2040259f,  1.0572252f
            };
            // Rec.2020 XYZ→RGB
            const float rec2020XYZtoRGB[9] = {
                 1.7166512f, -0.3556708f, -0.2533663f,
                -0.6666844f,  1.6164812f,  0.0157685f,
                 0.0176399f, -0.0427706f,  0.9421031f
            };
            const float* m = isHDR ? rec2020XYZtoRGB : srgbXYZtoRGB;
            dst.whiteBalanceGains[0] = m[0] * tX + m[1] * tY + m[2] * tZ;
            dst.whiteBalanceGains[1] = m[3] * tX + m[4] * tY + m[5] * tZ;
            dst.whiteBalanceGains[2] = m[6] * tX + m[7] * tY + m[8] * tZ;
        }
    }

    // Copy grayscale settings
    dst.grayscale.enabled = src.grayscale.enabled;
    dst.grayscale.pointCount = src.grayscale.pointCount;
    dst.grayscale.peakNits = src.grayscale.peakNits;
    dst.grayscale.use24Gamma = src.grayscale.use24Gamma;
    // Defensive: ensure pointCount is valid to prevent division by zero
    if (dst.grayscale.pointCount < 2) dst.grayscale.pointCount = 20;
    for (int i = 0; i < 32; i++) {
        if (i < (int)src.grayscale.points.size()) {
            dst.grayscale.points[i] = src.grayscale.points[i];
        } else {
            // Square root distribution fallback: output = input = (i/(N-1))^2
            float t = (float)i / (float)(dst.grayscale.pointCount - 1);
            dst.grayscale.points[i] = t * t;
        }
    }

    // Copy tonemapping settings
    dst.tonemap.enabled = src.tonemap.enabled;
    dst.tonemap.dynamicPeak = src.tonemap.dynamicPeak;
    dst.tonemap.curve = src.tonemap.curve;
    dst.tonemap.sourcePeakNits = src.tonemap.sourcePeakNits;
    dst.tonemap.targetPeakNits = src.tonemap.targetPeakNits;

    return dst;
}

void ProcessingThreadFunc(std::vector<MonitorLUTConfig> configs) {
    // Initialize COM for this thread (separate apartment from GUI thread)
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    // Set DPI awareness for this thread
    SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    // Enumerate monitors
    std::vector<HMONITOR> monitors;
    EnumDisplayMonitors(nullptr, nullptr, MonitorEnumProc, reinterpret_cast<LPARAM>(&monitors));

    // Create window class for overlays
    WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandle(nullptr);
    wc.lpszClassName = g_windowClassName;
    RegisterClassEx(&wc);

    // Initialize D3D
    if (!InitD3D()) {
        SetStatus(L"Failed to initialize D3D11");
        ReleaseSharedD3DResources();  // Clean up any partially initialized resources
        return;
    }

    CheckTearingSupport();

    if (!InitDirectCompositionDevice()) {
        SetStatus(L"Failed to initialize DirectComposition");
        ReleaseSharedD3DResources();  // Clean up D3D resources
        return;
    }

    // Initialize Compositor Clock API for VRR-aware frame timing
    InitCompositorClock();

    // LUT cache
    std::map<std::wstring, std::pair<std::vector<float>, int>> lutCache;

    for (const auto& config : configs) {
        if (config.monitorIndex >= (int)monitors.size()) continue;

        MonitorContext ctx;
        ctx.index = config.monitorIndex;
        ctx.monitor = monitors[config.monitorIndex];
        ctx.sdrLutPath = config.sdrLutPath;
        ctx.hdrLutPath = config.hdrLutPath;
        ctx.sdrColorCorrection = config.sdrColorCorrection;
        ctx.hdrColorCorrection = config.hdrColorCorrection;

        // Set MHC profile flags to prevent double-correction
        // When MHC is active at GPU scanout, shader skips those stages
        // MHC has its own primaries/grayscale (Layer 1), separate from shader corrections (Layer 3)
        if (config.monitorIndex < (int)g_gui.monitorSettings.size()) {
            const auto& ms = g_gui.monitorSettings[config.monitorIndex];
            ctx.sdrMhcPrimariesActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()
                && ms.sdrMHC.primariesEnabled;
            ctx.sdrMhcGrayscaleActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty()
                && ms.sdrMHC.grayscale.enabled;
            ctx.hdrMhcPrimariesActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty()
                && ms.hdrMHC.primariesEnabled;
            ctx.hdrMhcGrayscaleActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty()
                && ms.hdrMHC.grayscale.enabled;
        }

        MONITORINFO mi = { sizeof(mi) };
        GetMonitorInfo(ctx.monitor, &mi);
        ctx.width = mi.rcMonitor.right - mi.rcMonitor.left;
        ctx.height = mi.rcMonitor.bottom - mi.rcMonitor.top;
        ctx.x = mi.rcMonitor.left;
        ctx.y = mi.rcMonitor.top;

        // Load SDR LUT (optional if color correction is enabled)
        std::vector<float> lutDataSDR;
        bool hasSDRLUT = false;
        if (!config.sdrLutPath.empty()) {
            if (lutCache.find(config.sdrLutPath) != lutCache.end()) {
                lutDataSDR = lutCache[config.sdrLutPath].first;
                ctx.lutSizeSDR = lutCache[config.sdrLutPath].second;
                hasSDRLUT = true;
            } else {
                if (LoadLUT(config.sdrLutPath, lutDataSDR, ctx.lutSizeSDR)) {
                    hasSDRLUT = true;
                    lutCache[config.sdrLutPath] = { lutDataSDR, ctx.lutSizeSDR };
                } else {
                    SetStatus(L"Failed to load SDR LUT");
                    continue;
                }
            }
        }

        // Set passthrough mode if no SDR LUT (color correction only)
        ctx.usePassthrough = !hasSDRLUT;

        // Load HDR LUT if specified
        std::vector<float> lutDataHDR;
        bool hasHDRLUT = false;
        if (!config.hdrLutPath.empty()) {
            if (lutCache.find(config.hdrLutPath) != lutCache.end()) {
                lutDataHDR = lutCache[config.hdrLutPath].first;
                ctx.lutSizeHDR = lutCache[config.hdrLutPath].second;
                hasHDRLUT = true;
            } else {
                if (LoadLUT(config.hdrLutPath, lutDataHDR, ctx.lutSizeHDR)) {
                    hasHDRLUT = true;
                    lutCache[config.hdrLutPath] = { lutDataHDR, ctx.lutSizeHDR };
                }
            }
        }

        // Create overlay window
        wchar_t windowTitle[64];
        swprintf_s(windowTitle, L"DesktopLUT_Monitor%d", ctx.index);

        ctx.hwnd = CreateWindowEx(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            g_windowClassName, windowTitle,
            WS_POPUP,
            ctx.x, ctx.y, ctx.width, ctx.height,
            nullptr, nullptr, wc.hInstance, nullptr);

        if (!ctx.hwnd) continue;

        SetWindowDisplayAffinity(ctx.hwnd, WDA_EXCLUDEFROMCAPTURE);
        // Start fully transparent - will be made opaque after first frame renders
        SetLayeredWindowAttributes(ctx.hwnd, 0, 0, LWA_ALPHA);

        if (!InitDesktopDuplication(&ctx)) {
            DestroyWindow(ctx.hwnd);
            continue;
        }

        // Set passthrough mode if no applicable LUT for current mode
        // Overlay still runs for live color corrections, desktop gamma, MHC profiles, etc.
        if (ctx.isHDREnabled) {
            ctx.usePassthrough = (ctx.lutSRV_HDR == nullptr && !hasHDRLUT);
        }

        if (!CreateSwapChain(&ctx)) {
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            continue;
        }

        if (!InitDirectComposition(&ctx)) {
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            continue;
        }

        // Create LUT textures (only if we have LUT data)
        if (hasSDRLUT) {
            if (!CreateLUTTexture(lutDataSDR, ctx.lutSizeSDR, &ctx.lutTextureSDR, &ctx.lutSRV_SDR)) {
                ReleaseMonitorD3DResources(&ctx);
                DestroyWindow(ctx.hwnd);
                continue;
            }
        }

        if (hasHDRLUT) {
            CreateLUTTexture(lutDataHDR, ctx.lutSizeHDR, &ctx.lutTextureHDR, &ctx.lutSRV_HDR);
        }

        // Don't show window yet - render loop will show it after first frame is rendered
        g_monitors.push_back(ctx);
    }

    if (g_monitors.empty()) {
        SetStatus(L"No monitors initialized");
        ReleaseSharedD3DResources();  // Clean up D3D resources on early exit
        return;
    }

    // Clean up orphaned MHC profiles from previous sessions and reapply all active profiles
    CleanupOrphanedMhcProfiles();
    ReapplyAllMhcProfiles();

    g_mainHwnd = g_monitors[0].hwnd;

    // Register hotkeys (conditional based on settings, MOD_NOREPEAT prevents repeat when held)
    if (g_hotkeyGammaEnabled.load()) {
        RegisterHotKey(g_mainHwnd, HOTKEY_GAMMA, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyGammaKey);
    }
    if (g_hotkeyAnalysisEnabled.load()) {
        RegisterHotKey(g_mainHwnd, HOTKEY_ANALYSIS, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyAnalysisKey);
    }
    if (g_hotkeyHdrEnabled.load()) {
        RegisterHotKey(g_mainHwnd, HOTKEY_HDR_TOGGLE, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyHdrKey);
    }

    // Register for display power state notifications (display sleep/wake)
    RegisterDisplayPowerNotification(g_mainHwnd);

    // Create OSD
    CreateOSDWindow(GetModuleHandle(nullptr));

    // Create analysis overlay
    CreateAnalysisOverlay(GetModuleHandle(nullptr));

    // Start gamma whitelist polling thread (runs independently from frame timing)
    StartGammaWhitelistThread();

    SetStatus(L"Active");

    // Initialize watchdog timestamp
    g_lastSuccessfulFrame = std::chrono::steady_clock::now();

    // Main loop
    MSG msg = {};
    while (g_running) {
        while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
        }

        if (g_running) {
            RenderAll();
            // AcquireNextFrame timeout provides CPU yielding
        }
    }

    // Stop gamma whitelist polling thread
    StopGammaWhitelistThread();

    // Unregister hotkeys before cleanup
    if (g_mainHwnd) {
        UnregisterHotKey(g_mainHwnd, HOTKEY_GAMMA);
        UnregisterHotKey(g_mainHwnd, HOTKEY_ANALYSIS);
        UnregisterHotKey(g_mainHwnd, HOTKEY_HDR_TOGGLE);
    }

    // Unregister display power notifications
    UnregisterDisplayPowerNotification();

    // Cleanup analysis overlay
    DestroyAnalysisOverlay();

    // Cleanup OSD
    if (g_osdHwnd) {
        DestroyWindow(g_osdHwnd);
        g_osdHwnd = nullptr;
    }

    // Cleanup monitor contexts
    for (auto& ctx : g_monitors) {
        CleanupMonitorContext(&ctx);
    }
    g_monitors.clear();
    g_mainHwnd = nullptr;

    // Pump any remaining messages
    while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

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

    CoUninitialize();

    SetStatus(L"Inactive");
    PostMessage(g_gui.hwndMain, WM_USER + 100, 0, 0);  // Signal GUI to update
}

void StartProcessing() {
    if (g_gui.isRunning) return;

    // Ensure any previous thread is joined before creating a new one
    // This handles the case where the thread exited (e.g., watchdog timeout)
    // but wasn't joined through StopProcessing()
    if (g_gui.processingThread.joinable()) {
        g_gui.processingThread.join();
    }

    // Build config from all monitors with SDR LUT or color correction configured
    std::vector<MonitorLUTConfig> configs;
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        const auto& ms = g_gui.monitorSettings[i];
        bool hasLUT = !ms.sdrPath.empty();
        bool hasSdrColorCorrection = ms.sdrColorCorrection.primariesEnabled || ms.sdrColorCorrection.grayscale.enabled;
        bool hasHdrColorCorrection = ms.hdrColorCorrection.primariesEnabled ||
                                     ms.hdrColorCorrection.grayscale.enabled ||
                                     ms.hdrColorCorrection.tonemap.enabled;

        if (hasLUT || hasSdrColorCorrection || hasHdrColorCorrection) {
            MonitorLUTConfig config;
            config.monitorIndex = (int)i;
            config.sdrLutPath = ms.sdrPath;
            config.hdrLutPath = ms.hdrPath;
            config.sdrColorCorrection = ConvertColorCorrection(ms.sdrColorCorrection, false);
            config.hdrColorCorrection = ConvertColorCorrection(ms.hdrColorCorrection, true);
            configs.push_back(config);
        }
    }

    if (configs.empty()) {
        SetStatus(L"Configure at least one monitor with LUT or color correction");
        return;
    }

    // Apply MaxTML settings for monitors that have it enabled
    ApplyMaxTmlSettings();

    // Save current settings as active (for comparison to detect changes)
    g_gui.activeSettings = g_gui.monitorSettings;

    g_running = true;
    g_gui.isRunning = true;
    g_gui.processingThread = std::thread(ProcessingThreadFunc, configs);

    // Directly set button states - don't call UpdateGUIState which may re-enable via SettingsChanged
    EnableWindow(g_gui.hwndApply, FALSE);
    EnableWindow(g_gui.hwndStop, TRUE);
    SetStatus(L"Active");
}

void StopProcessing() {
    if (!g_gui.isRunning) return;

    SetStatus(L"Stopping...");
    g_running = false;

    if (g_gui.processingThread.joinable()) {
        // Wait for thread with timeout to prevent GUI freeze
        // Process GUI messages while waiting so window stays responsive
        auto handle = g_gui.processingThread.native_handle();
        DWORD startTime = GetTickCount();
        DWORD timeout = 2000;  // 2 second timeout

        while (true) {
            DWORD elapsed = GetTickCount() - startTime;
            if (elapsed >= timeout) {
                // Timeout - detach thread
                g_gui.processingThread.detach();
                SetStatus(L"Inactive");
                break;
            }

            DWORD waitTime = (100 < timeout - elapsed) ? 100 : (timeout - elapsed);  // Wait in 100ms chunks
            DWORD result = WaitForSingleObject(handle, waitTime);

            if (result == WAIT_OBJECT_0) {
                g_gui.processingThread.join();
                SetStatus(L"Inactive");
                break;
            }

            // Pump GUI messages to keep window responsive
            MSG msg;
            while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
                TranslateMessage(&msg);
                DispatchMessage(&msg);
            }
        }
    }

    g_gui.isRunning = false;
    g_gui.activeSettings.clear();  // No longer running, clear active settings
    UpdateGUIState();
}

void UpdateColorCorrectionLive(int monitorIndex, bool isHDR) {
    if (!g_gui.isRunning || monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    // Convert GUI settings to runtime format
    const auto& src = isHDR ? g_gui.monitorSettings[monitorIndex].hdrColorCorrection
                            : g_gui.monitorSettings[monitorIndex].sdrColorCorrection;
    ColorCorrectionData cc = ConvertColorCorrection(src, isHDR);

    // Queue the update for the processing thread
    std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
    // Remove any existing pending update for this monitor and mode
    g_pendingColorCorrections.erase(
        std::remove_if(g_pendingColorCorrections.begin(), g_pendingColorCorrections.end(),
            [monitorIndex, isHDR](const PendingColorCorrection& p) {
                return p.monitorIndex == monitorIndex && p.isHDR == isHDR;
            }),
        g_pendingColorCorrections.end());
    g_pendingColorCorrections.push_back({ monitorIndex, isHDR, cc });
    g_hasPendingColorCorrections.store(true, std::memory_order_release);
}

// Helper to get white point from edit boxes
static void GetWhitePointFromEditBoxes(float& Wx, float& Wy, bool isHDR) {
    wchar_t buf[16];
    HWND hwndWx = isHDR ? g_gui.hwndHdrPrimariesWx : g_gui.hwndPrimariesWx;
    HWND hwndWy = isHDR ? g_gui.hwndHdrPrimariesWy : g_gui.hwndPrimariesWy;
    if (hwndWx) { GetWindowText(hwndWx, buf, 16); Wx = (float)_wtof(buf); }
    if (hwndWy) { GetWindowText(hwndWy, buf, 16); Wy = (float)_wtof(buf); }
}

bool SettingsChanged() {
    if (g_gui.monitorSettings.size() != g_gui.activeSettings.size()) {
        return true;
    }
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        if (g_gui.monitorSettings[i].sdrPath != g_gui.activeSettings[i].sdrPath ||
            g_gui.monitorSettings[i].hdrPath != g_gui.activeSettings[i].hdrPath) {
            return true;
        }
    }

    // Compare per-monitor settings that affect rendering
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        const auto& cur = g_gui.monitorSettings[i];
        const auto& act = g_gui.activeSettings[i];

        // Check SDR color correction
        if (cur.sdrColorCorrection.primariesEnabled != act.sdrColorCorrection.primariesEnabled ||
            cur.sdrColorCorrection.primariesPreset != act.sdrColorCorrection.primariesPreset ||
            cur.sdrColorCorrection.grayscale.enabled != act.sdrColorCorrection.grayscale.enabled ||
            cur.sdrColorCorrection.grayscale.pointCount != act.sdrColorCorrection.grayscale.pointCount ||
            fabsf(cur.sdrColorCorrection.customPrimaries.Wx - act.sdrColorCorrection.customPrimaries.Wx) > 0.0001f ||
            fabsf(cur.sdrColorCorrection.customPrimaries.Wy - act.sdrColorCorrection.customPrimaries.Wy) > 0.0001f) {
            return true;
        }

        // Check HDR color correction
        if (cur.hdrColorCorrection.primariesEnabled != act.hdrColorCorrection.primariesEnabled ||
            cur.hdrColorCorrection.primariesPreset != act.hdrColorCorrection.primariesPreset ||
            cur.hdrColorCorrection.grayscale.enabled != act.hdrColorCorrection.grayscale.enabled ||
            cur.hdrColorCorrection.grayscale.pointCount != act.hdrColorCorrection.grayscale.pointCount ||
            cur.hdrColorCorrection.tonemap.enabled != act.hdrColorCorrection.tonemap.enabled ||
            fabsf(cur.hdrColorCorrection.customPrimaries.Wx - act.hdrColorCorrection.customPrimaries.Wx) > 0.0001f ||
            fabsf(cur.hdrColorCorrection.customPrimaries.Wy - act.hdrColorCorrection.customPrimaries.Wy) > 0.0001f) {
            return true;
        }
    }

    // Check current monitor's white point from edit boxes (may differ from stored settings)
    if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.activeSettings.size()) {
        // Check SDR white point
        {
            float Wx = 0.3127f, Wy = 0.3290f;
            GetWhitePointFromEditBoxes(Wx, Wy, false);
            const auto& activeCC = g_gui.activeSettings[g_gui.currentMonitor].sdrColorCorrection;
            if (fabsf(Wx - activeCC.customPrimaries.Wx) > 0.0001f ||
                fabsf(Wy - activeCC.customPrimaries.Wy) > 0.0001f) {
                return true;
            }
        }
        // Check HDR white point
        {
            float Wx = 0.3127f, Wy = 0.3290f;
            GetWhitePointFromEditBoxes(Wx, Wy, true);
            const auto& activeCC = g_gui.activeSettings[g_gui.currentMonitor].hdrColorCorrection;
            if (fabsf(Wx - activeCC.customPrimaries.Wx) > 0.0001f ||
                fabsf(Wy - activeCC.customPrimaries.Wy) > 0.0001f) {
                return true;
            }
        }
    }

    return false;
}

void ApplyMaxTmlSettings() {
    // Apply MaxTML (Display Peak Override) for all monitors that have it enabled
    // This should be called on:
    // - Startup (StartProcessing)
    // - Sleep/wake recovery
    // - TDR recovery
    // - After HDR mode changes (swapchain recreation)
    // Reapplying is cheap and safe, so we do it liberally to ensure the setting isn't lost
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        const auto& ms = g_gui.monitorSettings[i];
        if (ms.maxTml.enabled) {
            DisplayInfo displayInfo;
            if (GetDisplayInfoForMonitor((int)i, displayInfo)) {
                if (SetDisplayMaxTml(displayInfo, ms.maxTml.peakNits)) {
                    std::cout << "Applied MaxTML " << ms.maxTml.peakNits << " nits to monitor " << i << std::endl;
                }
            }
        }
    }
}
