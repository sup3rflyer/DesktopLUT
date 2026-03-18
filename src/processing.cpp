// DesktopLUT - processing.cpp
// Processing thread management

#include "processing.h"
#include "globals.h"
#include "lut.h"
#include "color.h"
#include "render.h"
#include "framepacer.h"
#include "capture.h"
#include "osd.h"
#include "analysis.h"
#include "settings.h"
#include "gui.h"
#include "gpu.h"
#include "displayconfig.h"
#include "mhc.h"
#include "dwm_inject.h"
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
        // Compute per-channel final values from base * deviation
        float base = dst.grayscale.points[i];
        float devR = (i < (int)src.grayscale.rgbDeviations[0].size()) ? src.grayscale.rgbDeviations[0][i] : 1.0f;
        float devG = (i < (int)src.grayscale.rgbDeviations[1].size()) ? src.grayscale.rgbDeviations[1][i] : 1.0f;
        float devB = (i < (int)src.grayscale.rgbDeviations[2].size()) ? src.grayscale.rgbDeviations[2][i] : 1.0f;
        dst.grayscale.pointsR[i] = base * devR;
        dst.grayscale.pointsG[i] = base * devG;
        dst.grayscale.pointsB[i] = base * devB;
    }

    // Copy tonemapping settings
    dst.tonemap.enabled = src.tonemap.enabled;
    dst.tonemap.dynamicPeak = src.tonemap.dynamicPeak;
    dst.tonemap.curve = src.tonemap.curve;
    dst.tonemap.sourcePeakNits = src.tonemap.sourcePeakNits;
    dst.tonemap.targetPeakNits = src.tonemap.targetPeakNits;

    return dst;
}

// TOPMOST reassert helper thread — runs at low priority to avoid impacting
// the MMCSS render loop. Periodically calls SetWindowPos(HWND_TOPMOST) for
// all overlay windows, either on-demand (event signaled) or every 30 seconds.
static DWORD WINAPI TopmostHelperThread(LPVOID) {
    while (true) {
        DWORD result = WaitForSingleObject(g_topmostEvent, 500);
        if (!g_running) break;

        static auto lastReassert = std::chrono::steady_clock::now();
        auto now = std::chrono::steady_clock::now();
        bool doReassert = (result == WAIT_OBJECT_0) ||
            (std::chrono::duration_cast<std::chrono::milliseconds>(now - lastReassert).count() >= 30000);

        if (doReassert) {
            // Guard: suppress WM_WINDOWPOSCHANGING handler during our own reasserts
            // to prevent feedback loop (our analysis TOPMOST triggers overlay's handler
            // which re-signals this thread → rapid z-order bouncing → visible flicker)
            g_selfReassertInProgress.store(true, std::memory_order_release);

            // Batch all z-order changes atomically via DeferWindowPos to prevent
            // intermediate state where overlay is above analysis (causes brief flash)
            int count = 0;
            for (auto& ctx : g_monitors) { if (ctx.hwnd) count++; }
            bool showAnalysis = g_analysisHwnd && g_analysisEnabled.load();
            if (showAnalysis) count++;

            HDWP hdwp = BeginDeferWindowPos(count);
            if (hdwp) {
                for (auto& ctx : g_monitors) {
                    if (ctx.hwnd) {
                        hdwp = DeferWindowPos(hdwp, ctx.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                        if (!hdwp) break;
                    }
                }
                // Analysis last = highest in z-order (above overlays)
                if (hdwp && showAnalysis) {
                    hdwp = DeferWindowPos(hdwp, g_analysisHwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                }
                if (hdwp) EndDeferWindowPos(hdwp);
            }

            g_selfReassertInProgress.store(false, std::memory_order_release);
            lastReassert = std::chrono::steady_clock::now();
        }
    }
    return 0;
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
        CoUninitialize();
        PostMessage(g_gui.hwndMain, WM_USER + 100, 0, 0);
        return;
    }

    CheckTearingSupport();

    if (!InitDirectCompositionDevice()) {
        SetStatus(L"Failed to initialize DirectComposition");
        ReleaseSharedD3DResources();  // Clean up D3D resources
        CoUninitialize();
        PostMessage(g_gui.hwndMain, WM_USER + 100, 0, 0);
        return;
    }

    // Initialize Compositor Clock API for VRR-aware frame timing
    InitCompositorClock();

    // Initialize high-precision frame pacer (uses CompositorClock result for strategy selection)
    FramePacer framePacer = {};
    InitFramePacer(&framePacer);

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

        // Track which MHC corrections are active at GPU scanout (for diagnostics and live preview)
        // Shader corrections are independent — all layers stack (MHC = Layer 1, shader = Layer 3)
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (config.monitorIndex < (int)g_gui.monitorSettings.size()) {
                const auto& ms = g_gui.monitorSettings[config.monitorIndex];
                bool sdrActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty();
                bool hdrActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty();
                bool sdrHasGs = ms.sdrMHC.grayscale.enabled || ms.sdrMHC.correctionGrayscale.enabled ||
                                ms.sdrMHC.hasPerChannelTRC || !ms.sdrMHC.sourceFilePath.empty();
                bool hdrHasGs = ms.hdrMHC.grayscale.enabled || ms.hdrMHC.correctionGrayscale.enabled ||
                                ms.hdrMHC.hasPerChannelTRC || !ms.hdrMHC.sourceFilePath.empty() ||
                                ms.hdrMHC.desktopGammaEnabled;
                ctx.sdrMhcPrimariesActive = sdrActive && ms.sdrMHC.primariesEnabled;
                ctx.sdrMhcGrayscaleActive = sdrActive && sdrHasGs;
                ctx.hdrMhcPrimariesActive = hdrActive && ms.hdrMHC.primariesEnabled;
                ctx.hdrMhcGrayscaleActive = hdrActive && hdrHasGs;
            }
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
        CoUninitialize();
        PostMessage(g_gui.hwndMain, WM_USER + 100, 0, 0);
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

    // Create auto-sleep wake event (auto-reset: resets after WaitForSingleObject returns)
    g_overlayWakeEvent = CreateEvent(nullptr, FALSE, FALSE, nullptr);

    // Start TOPMOST reassert helper thread (offloads SetWindowPos from MMCSS render thread)
    g_topmostEvent = CreateEvent(nullptr, FALSE, FALSE, nullptr);
    HANDLE topmostThread = CreateThread(nullptr, 0, TopmostHelperThread, nullptr, 0, nullptr);

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
            RenderAll(&framePacer);
            // AcquireNextFrame timeout provides CPU yielding
        }
    }

    // Stop TOPMOST helper thread
    if (g_topmostEvent) SetEvent(g_topmostEvent);
    if (topmostThread) { WaitForSingleObject(topmostThread, 1000); CloseHandle(topmostThread); }
    if (g_topmostEvent) { CloseHandle(g_topmostEvent); g_topmostEvent = nullptr; }

    // Clean up frame pacer (MMCSS, timers, timeEndPeriod)
    CleanupFramePacer(&framePacer);

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
    DestroyOSDFont();

    // Clean up auto-sleep wake event
    if (g_overlayWakeEvent) {
        CloseHandle(g_overlayWakeEvent);
        g_overlayWakeEvent = nullptr;
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

    ReleaseSharedD3DResources();

    CoUninitialize();

    SetStatus(L"Inactive");
    PostMessage(g_gui.hwndMain, WM_USER + 100, 0, 0);  // Signal GUI to update
}

// Check if any shader corrections are active.
// In DWM hook mode this determines whether the overlay thread is needed.
// Reads g_shaderCorrectionsActive (set by render thread) when overlay is running,
// falls back to GUI state check when overlay isn't running yet.
// Must mirror the conditions in render.cpp's shaderCorrActive evaluation to avoid
// silently dropping corrections when the overlay thread isn't started.
static bool HasActiveShaderCorrections() {
    // Analysis overlay requires the render thread regardless of corrections
    if (g_analysisEnabled.load()) return true;
    // Render thread is the source of truth when running — use its last evaluation
    if (g_gui.processingThread.joinable())
        return g_shaderCorrectionsActive.load(std::memory_order_relaxed);
    // MHC/grayscale editor live preview needs the overlay
    if (g_mhcEditDialogOpen.load()) return true;
    // Static GUI check when render thread is not running.
    // MHC suppression mirrors UpdateMhcFlagsLive() and the render loop's mhcG evaluation.
    bool desktopGamma = g_userDesktopGammaMode.load();
    for (const auto& ms : g_gui.monitorSettings) {
        bool sdrMhcP = ms.sdrMHC.enabled && ms.sdrMHC.primariesEnabled;
        bool sdrMhcG = ms.sdrMHC.enabled && (ms.sdrMHC.grayscale.enabled ||
                       ms.sdrMHC.correctionGrayscale.enabled ||
                       !ms.sdrMHC.sourceFilePath.empty() || ms.sdrMHC.hasPerChannelTRC);
        bool hdrMhcP = ms.hdrMHC.enabled && ms.hdrMHC.primariesEnabled;
        bool hdrMhcG = ms.hdrMHC.enabled && (ms.hdrMHC.grayscale.enabled ||
                       ms.hdrMHC.correctionGrayscale.enabled ||
                       !ms.hdrMHC.sourceFilePath.empty() || ms.hdrMHC.hasPerChannelTRC ||
                       ms.hdrMHC.desktopGammaEnabled);

        // SDR: corrections not baked into MHC (DG is HDR-only, skip for SDR)
        const auto& sdr = ms.sdrColorCorrection;
        if (sdr.primariesEnabled && !sdrMhcP) return true;
        if (sdr.grayscale.enabled && !sdrMhcG) return true;
        if (sdr.grayscale.use24Gamma && !sdrMhcG) return true;

        // HDR: tonemapping is the primary shader correction; DG only active if MHC not handling it
        const auto& hdr = ms.hdrColorCorrection;
        if (hdr.primariesEnabled && !hdrMhcP) return true;
        if (hdr.grayscale.enabled && !hdrMhcG) return true;
        if (hdr.tonemap.enabled && !g_dwmHookMode.load()) return true;
        if (desktopGamma && !hdrMhcG) return true;
    }
    return false;
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
        bool hasLUT = !ms.sdrPath.empty() || !ms.hdrPath.empty();
        bool hasSdrColorCorrection = ms.sdrColorCorrection.primariesEnabled ||
                                     ms.sdrColorCorrection.grayscale.enabled ||
                                     ms.sdrColorCorrection.grayscale.use24Gamma;
        bool hasHdrColorCorrection = ms.hdrColorCorrection.primariesEnabled ||
                                     ms.hdrColorCorrection.grayscale.enabled ||
                                     ms.hdrColorCorrection.tonemap.enabled;
        bool hasDesktopGamma = g_userDesktopGammaMode.load();

        if (hasLUT || hasSdrColorCorrection || hasHdrColorCorrection || hasDesktopGamma) {
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

    if (g_dwmHookMode.load()) {
        // DWM Hook Mode: inject DLL into dwm.exe for LUT application
        std::wcout << L"[DWM Hook] Starting in DWM Hook mode" << std::endl;

        // Build monitor LUT list for injection
        std::vector<DwmHookMonitorLUT> dwmMonitors;
        for (const auto& cfg : configs) {
            if (cfg.sdrLutPath.empty() && cfg.hdrLutPath.empty()) continue;
            if (cfg.monitorIndex < 0 || cfg.monitorIndex >= (int)g_gui.monitors.size()) continue;

            MONITORINFO mi = { sizeof(mi) };
            if (GetMonitorInfo(g_gui.monitors[cfg.monitorIndex], &mi)) {
                DwmHookMonitorLUT lut;
                lut.left = mi.rcMonitor.left;
                lut.top = mi.rcMonitor.top;
                lut.sdrLutPath = cfg.sdrLutPath;
                lut.hdrLutPath = cfg.hdrLutPath;
                dwmMonitors.push_back(lut);
            }
        }

        std::wcout << L"[DWM Hook] " << dwmMonitors.size() << L" monitor(s) with LUT paths" << std::endl;

        if (!dwmMonitors.empty()) {
            std::wstring err = InjectDwmHook(dwmMonitors);
            if (!err.empty()) {
                std::wcout << L"[DWM Hook] Injection failed" << std::endl;
                SetStatus(err.c_str());
                return;
            }
        }

        // Check if overlay is also needed (corrections or analysis configured)
        bool needOverlay = HasActiveShaderCorrections();

        if (needOverlay) {
            // Start overlay in passthrough for LUT (DWM hook handles LUT)
            // Clear LUT paths so shader only applies corrections
            std::cout << "[DWM Hook] Shader overlay ACTIVE (corrections-only passthrough, DWM hook handles LUT)" << std::endl;
            for (auto& cfg : configs) {
                cfg.sdrLutPath.clear();
                cfg.hdrLutPath.clear();
            }
            g_running = true;
            g_gui.isRunning = true;
            g_gui.restartRetryCount = 0;
            if (g_gui.hwndMain) KillTimer(g_gui.hwndMain, RESTART_TIMER_ID);
            g_gui.processingThread = std::thread(ProcessingThreadFunc, configs);
        } else {
            // LUT-only mode: no overlay needed
            std::cout << "[DWM Hook] Shader overlay DISABLED (LUT-only mode, no overlay needed)" << std::endl;
            g_running = true;
            g_gui.isRunning = true;
            g_gui.restartRetryCount = 0;
            if (g_gui.hwndMain) KillTimer(g_gui.hwndMain, RESTART_TIMER_ID);

            // Register hotkeys on GUI window since overlay WndProc doesn't exist
            if (g_gui.hwndMain) {
                if (g_hotkeyGammaEnabled.load())
                    RegisterHotKey(g_gui.hwndMain, HOTKEY_GAMMA, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyGammaKey);
                if (g_hotkeyAnalysisEnabled.load())
                    RegisterHotKey(g_gui.hwndMain, HOTKEY_ANALYSIS, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyAnalysisKey);
                if (g_hotkeyHdrEnabled.load())
                    RegisterHotKey(g_gui.hwndMain, HOTKEY_HDR_TOGGLE, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyHdrKey);
                g_hookOnlyHotkeys = true;
            }

            // Create analysis overlay (for potential hotkey toggle)
            CreateAnalysisOverlay(GetModuleHandle(nullptr));
        }

        EnableWindow(g_gui.hwndApply, FALSE);
        EnableWindow(g_gui.hwndStop, TRUE);
        SetStatus(L"Active (DWM Hook)");

        // Start DWM hook watchdog timer (detects DWM restart / hook loss)
        g_dwmHookWatchdogRetries = 0;
        if (g_gui.hwndMain)
            SetTimer(g_gui.hwndMain, DWM_HOOK_WATCHDOG_TIMER_ID, DWM_HOOK_WATCHDOG_INTERVAL_MS, nullptr);

        return;
    }

    g_running = true;
    g_gui.isRunning = true;
    g_gui.restartRetryCount = 0;  // Reset backoff on successful start
    if (g_gui.hwndMain) KillTimer(g_gui.hwndMain, RESTART_TIMER_ID);
    g_gui.processingThread = std::thread(ProcessingThreadFunc, configs);

    // Directly set button states - don't call UpdateGUIState which may re-enable via SettingsChanged
    EnableWindow(g_gui.hwndApply, FALSE);
    EnableWindow(g_gui.hwndStop, TRUE);
    SetStatus(L"Active");
}

void StopProcessing() {
    // Cancel any pending auto-restart (user explicitly wants stopped)
    g_gui.restartRetryCount = 0;
    if (g_gui.hwndMain) KillTimer(g_gui.hwndMain, RESTART_TIMER_ID);

    if (!g_gui.isRunning) return;

    SetStatus(L"Stopping...");
    g_running = false;

    // Kill DWM hook watchdog timer
    if (g_gui.hwndMain)
        KillTimer(g_gui.hwndMain, DWM_HOOK_WATCHDOG_TIMER_ID);

    // Unregister hotkeys from GUI window if registered there (hook-only mode)
    if (g_hookOnlyHotkeys && g_gui.hwndMain) {
        UnregisterHotKey(g_gui.hwndMain, HOTKEY_GAMMA);
        UnregisterHotKey(g_gui.hwndMain, HOTKEY_ANALYSIS);
        UnregisterHotKey(g_gui.hwndMain, HOTKEY_HDR_TOGGLE);
        g_hookOnlyHotkeys = false;
    }

    // Destroy analysis overlay if created in hook-only mode
    if (g_dwmHookMode.load() && !g_gui.processingThread.joinable()) {
        DestroyAnalysisOverlay();
    }

    // Uninject DWM hook if in DWM hook mode (always try — harmless if not injected)
    if (g_dwmHookMode.load()) {
        std::cout << "[DWM Hook] Stopping — uninjecting from dwm.exe" << std::endl;
        UninjectDwmHook();
    }

    // Signal wake event to unblock any CompClock/WaitForSingleObject in the render loop
    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);

    if (g_gui.processingThread.joinable()) {
        // Wait for thread with timeout to prevent GUI freeze
        // Process GUI messages while waiting so window stays responsive
        auto handle = g_gui.processingThread.native_handle();
        DWORD startTime = GetTickCount();
        DWORD timeout = 6000;  // 6 second timeout (accounts for Sleep(500) in forced reinit + frame sync)

        while (true) {
            DWORD elapsed = GetTickCount() - startTime;
            if (elapsed >= timeout) {
                // Timeout - detach thread (last resort, should not normally happen)
                std::cerr << "Processing thread shutdown timed out after " << timeout << "ms, detaching" << std::endl;
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
    g_shaderCorrectionsActive.store(false);
    UpdateTrayIcon(false);
    UpdateGUIState();
}

void DwmHookReevaluateOverlay() {
    if (!g_gui.isRunning || !g_dwmHookMode.load()) return;

    bool needOverlay = HasActiveShaderCorrections();
    bool overlayRunning = g_gui.processingThread.joinable();

    if (overlayRunning && !needOverlay) {
        // Render thread's auto-sleep already hides windows and stops DD when nothing to do.
        // No need to kill the thread here — auto-sleep is effectively free (500ms wait loop).
        // Thread lifecycle is only managed by StartProcessing/StopProcessing.
        return;
    }

    if (!overlayRunning && needOverlay) {
        // Join any previously-exited thread (e.g., after watchdog exit) before spawning new one
        if (g_gui.processingThread.joinable()) {
            g_gui.processingThread.join();
        }
        // Unregister hook-only hotkeys — overlay thread will register its own on the overlay window
        if (g_hookOnlyHotkeys && g_gui.hwndMain) {
            UnregisterHotKey(g_gui.hwndMain, HOTKEY_GAMMA);
            UnregisterHotKey(g_gui.hwndMain, HOTKEY_ANALYSIS);
            UnregisterHotKey(g_gui.hwndMain, HOTKEY_HDR_TOGGLE);
            g_hookOnlyHotkeys = false;
        }
        std::cout << "[DWM Hook] Shader overlay now needed, starting DD" << std::endl;
        std::vector<MonitorLUTConfig> configs;
        for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
            const auto& ms = g_gui.monitorSettings[i];
            MonitorLUTConfig config;
            config.monitorIndex = (int)i;
            config.sdrColorCorrection = ConvertColorCorrection(ms.sdrColorCorrection, false);
            config.hdrColorCorrection = ConvertColorCorrection(ms.hdrColorCorrection, true);
            configs.push_back(config);
        }
        g_running = true;
        g_gui.processingThread = std::thread(ProcessingThreadFunc, configs);
        // Render thread evaluates shaderCorrActive on first frame and posts WM_SHADER_STATE_CHANGED
    }
}

void UpdateColorCorrectionLive(int monitorIndex, bool isHDR, bool ictcpMode) {
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
    g_pendingColorCorrections.push_back({ monitorIndex, isHDR, cc, false, ictcpMode });
    g_hasPendingColorCorrections.store(true, std::memory_order_release);
    // Wake the render thread in case it is auto-sleeping — without this, corrections
    // queued while the overlay is dormant wait up to 500ms and then are still missed
    // because the auto-sleep path used to return unconditionally before the queue check.
    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
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
    // Only check settings that require a full restart (LUT paths).
    // Color corrections (primaries, grayscale, tonemapping, white point) are
    // live-updated via the pending queue and don't need a restart.
    if (g_gui.monitorSettings.size() != g_gui.activeSettings.size()) {
        return true;
    }
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        if (g_gui.monitorSettings[i].sdrPath != g_gui.activeSettings[i].sdrPath ||
            g_gui.monitorSettings[i].hdrPath != g_gui.activeSettings[i].hdrPath) {
            return true;
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

    // Snapshot MaxTML settings under lock (called from render thread, GUI thread writes settings)
    struct MaxTmlEntry { bool enabled; float peakNits; };
    std::vector<MaxTmlEntry> entries;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        entries.reserve(g_gui.monitorSettings.size());
        for (const auto& ms : g_gui.monitorSettings) {
            entries.push_back({ms.maxTml.enabled, ms.maxTml.peakNits});
        }
    }

    for (size_t i = 0; i < entries.size(); i++) {
        if (entries[i].enabled) {
            DisplayInfo displayInfo;
            if (GetDisplayInfoForMonitor((int)i, displayInfo)) {
                if (SetDisplayMaxTml(displayInfo, entries[i].peakNits)) {
                    std::cout << "Applied MaxTML " << entries[i].peakNits << " nits to monitor " << i << std::endl;
                }
            }
        }
    }
}
