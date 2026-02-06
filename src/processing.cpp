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

    // Calculate primaries matrix if enabled
    // SDR: sRGB content → display primaries (gamut mapping for uncalibrated displays)
    // HDR: Rec.2020 → measured display primaries (correction applied in Rec.2020 space)
    if (src.primariesEnabled) {
        const DisplayPrimaries& userPrimRef = (src.primariesPreset == g_numPresetPrimaries - 1)
            ? src.customPrimaries : g_presetPrimaries[src.primariesPreset];

        DisplayPrimariesData srcPrim, tgtPrim;

        // Two-step approach:
        // 1. Gamut mapping matrix (direction depends on SDR vs HDR)
        // White point correction is handled by Bradford adaptation in the primaries matrix

        // Step 1: Gamut mapping with actual white points
        // Note: CalculatePrimariesMatrix includes Bradford adaptation when white points differ
        if (isHDR) {
            // HDR: Rec.2020 → measured display primaries
            // Applied AFTER BT.709→Rec.2020 conversion in shader, in linear Rec.2020 space
            // This corrects the signal so the display (with its actual primaries) shows intended colors
            srcPrim = { g_presetPrimaries[3].Rx, g_presetPrimaries[3].Ry,  // Rec.2020
                        g_presetPrimaries[3].Gx, g_presetPrimaries[3].Gy,
                        g_presetPrimaries[3].Bx, g_presetPrimaries[3].By,
                        g_presetPrimaries[3].Wx, g_presetPrimaries[3].Wy };
            tgtPrim = { userPrimRef.Rx, userPrimRef.Ry, userPrimRef.Gx, userPrimRef.Gy,
                        userPrimRef.Bx, userPrimRef.By,
                        userPrimRef.Wx, userPrimRef.Wy };  // Display's measured primaries
        } else {
            // SDR: sRGB → display primaries
            // Applied in linear sRGB space before LUT
            srcPrim = { g_presetPrimaries[0].Rx, g_presetPrimaries[0].Ry,  // sRGB
                        g_presetPrimaries[0].Gx, g_presetPrimaries[0].Gy,
                        g_presetPrimaries[0].Bx, g_presetPrimaries[0].By,
                        g_presetPrimaries[0].Wx, g_presetPrimaries[0].Wy };
            tgtPrim = { userPrimRef.Rx, userPrimRef.Ry, userPrimRef.Gx, userPrimRef.Gy,
                        userPrimRef.Bx, userPrimRef.By,
                        userPrimRef.Wx, userPrimRef.Wy };  // Display primaries with ACTUAL white
        }

        CalculatePrimariesMatrix(srcPrim, tgtPrim, dst.primariesMatrix);

        // White point adaptation is handled by Bradford chromatic adaptation
        // inside CalculatePrimariesMatrix() - no separate RGB gains needed
    } else {
        // Identity matrix (no primaries correction)
        dst.primariesMatrix[0] = 1; dst.primariesMatrix[1] = 0; dst.primariesMatrix[2] = 0;
        dst.primariesMatrix[3] = 0; dst.primariesMatrix[4] = 1; dst.primariesMatrix[5] = 0;
        dst.primariesMatrix[6] = 0; dst.primariesMatrix[7] = 0; dst.primariesMatrix[8] = 1;
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

        // Check if we have any HDR processing to do
        bool hasHdrColorCorrection = ctx.hdrColorCorrection.primariesEnabled ||
                                     ctx.hdrColorCorrection.grayscale.enabled ||
                                     ctx.hdrColorCorrection.tonemap.enabled;

        if (ctx.isHDREnabled && !hasHDRLUT && !hasHdrColorCorrection) {
            SetStatus(L"HDR mode requires HDR LUT or color correction");
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            continue;
        }

        // Set passthrough mode if no applicable LUT for current mode
        if (ctx.isHDREnabled) {
            ctx.usePassthrough = !hasHDRLUT;
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

// Helper to compare primaries (DisplayPrimariesData vs DisplayPrimaries)
static bool PrimariesChanged(const DisplayPrimariesData& a, const DisplayPrimaries& b) {
    const float eps = 0.0001f;
    return fabsf(a.Rx - b.Rx) > eps || fabsf(a.Ry - b.Ry) > eps ||
           fabsf(a.Gx - b.Gx) > eps || fabsf(a.Gy - b.Gy) > eps ||
           fabsf(a.Bx - b.Bx) > eps || fabsf(a.By - b.By) > eps ||
           fabsf(a.Wx - b.Wx) > eps || fabsf(a.Wy - b.Wy) > eps;
}

// Helper to get primaries from edit boxes
static DisplayPrimariesData GetPrimariesFromEditBoxes(bool isHDR) {
    DisplayPrimariesData p = {};
    wchar_t buf[16];
    if (isHDR) {
        if (g_gui.hwndHdrPrimariesRx) { GetWindowText(g_gui.hwndHdrPrimariesRx, buf, 16); p.Rx = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesRy) { GetWindowText(g_gui.hwndHdrPrimariesRy, buf, 16); p.Ry = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesGx) { GetWindowText(g_gui.hwndHdrPrimariesGx, buf, 16); p.Gx = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesGy) { GetWindowText(g_gui.hwndHdrPrimariesGy, buf, 16); p.Gy = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesBx) { GetWindowText(g_gui.hwndHdrPrimariesBx, buf, 16); p.Bx = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesBy) { GetWindowText(g_gui.hwndHdrPrimariesBy, buf, 16); p.By = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesWx) { GetWindowText(g_gui.hwndHdrPrimariesWx, buf, 16); p.Wx = (float)_wtof(buf); }
        if (g_gui.hwndHdrPrimariesWy) { GetWindowText(g_gui.hwndHdrPrimariesWy, buf, 16); p.Wy = (float)_wtof(buf); }
    } else {
        if (g_gui.hwndSdrPrimariesRx) { GetWindowText(g_gui.hwndSdrPrimariesRx, buf, 16); p.Rx = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesRy) { GetWindowText(g_gui.hwndSdrPrimariesRy, buf, 16); p.Ry = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesGx) { GetWindowText(g_gui.hwndSdrPrimariesGx, buf, 16); p.Gx = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesGy) { GetWindowText(g_gui.hwndSdrPrimariesGy, buf, 16); p.Gy = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesBx) { GetWindowText(g_gui.hwndSdrPrimariesBx, buf, 16); p.Bx = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesBy) { GetWindowText(g_gui.hwndSdrPrimariesBy, buf, 16); p.By = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesWx) { GetWindowText(g_gui.hwndSdrPrimariesWx, buf, 16); p.Wx = (float)_wtof(buf); }
        if (g_gui.hwndSdrPrimariesWy) { GetWindowText(g_gui.hwndSdrPrimariesWy, buf, 16); p.Wy = (float)_wtof(buf); }
    }
    return p;
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

    // Check if current monitor's custom primaries have changed from active settings
    // Only check when Custom preset (5) is selected in the dropdown
    if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.activeSettings.size()) {
        // Check SDR primaries only if Custom preset selected
        if (g_gui.hwndSdrPrimariesPreset) {
            int sdrPreset = (int)SendMessage(g_gui.hwndSdrPrimariesPreset, CB_GETCURSEL, 0, 0);
            if (sdrPreset == 5) {  // Custom preset
                DisplayPrimariesData sdrFromUI = GetPrimariesFromEditBoxes(false);
                if (PrimariesChanged(sdrFromUI, g_gui.activeSettings[g_gui.currentMonitor].sdrColorCorrection.customPrimaries)) {
                    return true;
                }
            }
        }
        // Check HDR primaries only if Custom preset selected
        if (g_gui.hwndHdrPrimariesPreset) {
            int hdrPreset = (int)SendMessage(g_gui.hwndHdrPrimariesPreset, CB_GETCURSEL, 0, 0);
            if (hdrPreset == 5) {  // Custom preset
                DisplayPrimariesData hdrFromUI = GetPrimariesFromEditBoxes(true);
                if (PrimariesChanged(hdrFromUI, g_gui.activeSettings[g_gui.currentMonitor].hdrColorCorrection.customPrimaries)) {
                    return true;
                }
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
