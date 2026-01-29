// DesktopLUT - main.cpp
// Entry point only

#include "types.h"
#include "globals.h"
#include "shader.h"
#include "lut.h"
#include "color.h"
#include "settings.h"
#include "gpu.h"
#include "capture.h"
#include "render.h"
#include "osd.h"
#include "analysis.h"
#include "processing.h"
#include "gui.h"
#include <objbase.h>
#include <shellapi.h>
#include <iostream>
#include <io.h>
#include <fcntl.h>
#include <map>

// ============================================================================
// Entry Point (Windows subsystem - no console flash)
// ============================================================================

// RAII wrapper for command line args
struct ArgvGuard {
    LPWSTR* argv;
    ArgvGuard(LPWSTR* a) : argv(a) {}
    ~ArgvGuard() { if (argv) LocalFree(argv); }
};

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;

    // Parse command line
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        return 1;
    }
    ArgvGuard argvGuard(argv);  // Auto-cleanup on any exit path

    // Initialize COM for DirectComposition and shell APIs
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    // Single instance check - prevent multiple copies from running
    g_singleInstanceMutex = CreateMutexW(nullptr, TRUE, L"DesktopLUT_SingleInstance_Mutex");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        // Another instance is running - try to bring its window to foreground
        HWND existingWnd = FindWindowW(L"DesktopLUT_GUI", L"DesktopLUT");
        if (existingWnd) {
            ShowWindow(existingWnd, SW_RESTORE);
            SetForegroundWindow(existingWnd);
        }
        CloseHandle(g_singleInstanceMutex);
        return 0;
    }

    // No args = GUI mode (no console needed)
    if (argc < 2) {
        int result = RunGUI();
        CloseHandle(g_singleInstanceMutex);
        return result;
    }

    // CLI mode - attach to parent console (cmd.exe) or create new one
    bool hasConsole = AttachConsole(ATTACH_PARENT_PROCESS);
    if (!hasConsole) {
        hasConsole = AllocConsole();  // Fallback if no parent console (double-clicked)
    }
    if (hasConsole) {
        FILE* fp;
        freopen_s(&fp, "CONOUT$", "w", stdout);
        freopen_s(&fp, "CONOUT$", "w", stderr);
        freopen_s(&fp, "CONIN$", "r", stdin);
        std::cout.clear();
        std::cerr.clear();
        std::cin.clear();
    }

    // ========================================================================
    // CLI mode
    // ========================================================================

    // Boost process priority for faster startup and smoother operation
    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);

    // Set DPI awareness
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    // Enumerate all monitors
    std::vector<HMONITOR> monitors;
    EnumDisplayMonitors(nullptr, nullptr, MonitorEnumProc, reinterpret_cast<LPARAM>(&monitors));

    std::cout << "Found " << monitors.size() << " monitor(s)" << std::endl;
    for (size_t i = 0; i < monitors.size(); i++) {
        MONITORINFO mi = { sizeof(mi) };
        GetMonitorInfo(monitors[i], &mi);
        int w = mi.rcMonitor.right - mi.rcMonitor.left;
        int h = mi.rcMonitor.bottom - mi.rcMonitor.top;
        std::cout << "  Monitor " << i << ": " << w << "x" << h
                  << " at (" << mi.rcMonitor.left << ", " << mi.rcMonitor.top << ")"
                  << (mi.dwFlags & MONITORINFOF_PRIMARY ? " [Primary]" : "") << std::endl;
    }

    // Parse command line arguments
    std::vector<MonitorLUTConfig> lutConfigs;
    bool hasMonitorFlag = false;

    for (int i = 1; i < argc; i++) {
        std::wstring arg = argv[i];
        if (arg == L"--monitor" || arg == L"-m") {
            hasMonitorFlag = true;
            if (i + 2 >= argc) {
                std::cerr << "Error: --monitor requires monitor index and at least one LUT file" << std::endl;
                return 1;
            }
            MonitorLUTConfig config;
            config.monitorIndex = _wtoi(argv[++i]);
            config.sdrLutPath = argv[++i];

            // Check if next arg is HDR LUT (not another --monitor flag)
            if (i + 1 < argc && wcsncmp(argv[i + 1], L"--", 2) != 0 && wcsncmp(argv[i + 1], L"-m", 2) != 0) {
                config.hdrLutPath = argv[++i];
            }

            if (config.monitorIndex < 0 || config.monitorIndex >= (int)monitors.size()) {
                std::cerr << "Error: Monitor index " << config.monitorIndex << " is out of range (0-"
                          << monitors.size() - 1 << ")" << std::endl;
                return 1;
            }
            lutConfigs.push_back(config);
        } else if (!hasMonitorFlag) {
            // Legacy mode: first arg is SDR LUT, optional second is HDR LUT
            // Apply to all monitors
            for (size_t m = 0; m < monitors.size(); m++) {
                MonitorLUTConfig config;
                config.monitorIndex = (int)m;
                config.sdrLutPath = arg;
                if (i + 1 < argc && wcsncmp(argv[i + 1], L"--", 2) != 0) {
                    config.hdrLutPath = argv[i + 1];
                }
                lutConfigs.push_back(config);
            }
            if (i + 1 < argc && wcsncmp(argv[i + 1], L"--", 2) != 0) {
                i++;  // Skip HDR LUT arg
            }
            break;  // Only process once in legacy mode
        }
    }

    if (lutConfigs.empty()) {
        std::cerr << "Error: No LUT configurations specified" << std::endl;
        PrintUsage();
        return 1;
    }

    // Create window class
    WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
    wc.lpfnWndProc = WndProc;
    wc.hInstance = GetModuleHandle(nullptr);
    wc.lpszClassName = g_windowClassName;
    RegisterClassEx(&wc);

    // Initialize D3D (shared across all monitors)
    if (!InitD3D()) {
        std::cerr << "Failed to initialize D3D11" << std::endl;
        return 1;
    }

    // Check tearing support
    CheckTearingSupport();

    // Initialize DirectComposition device (shared)
    if (!InitDirectCompositionDevice()) {
        std::cerr << "Failed to initialize DirectComposition device" << std::endl;
        return 1;
    }

    // Load LUTs and initialize each monitor
    std::map<std::wstring, std::pair<std::vector<float>, int>> lutCache;  // Cache loaded LUTs

    for (const auto& config : lutConfigs) {
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

        std::cout << "\n=== Initializing Monitor " << ctx.index << " ===" << std::endl;

        // Load SDR LUT (use cache if already loaded)
        std::vector<float> lutDataSDR;
        if (lutCache.find(config.sdrLutPath) != lutCache.end()) {
            lutDataSDR = lutCache[config.sdrLutPath].first;
            ctx.lutSizeSDR = lutCache[config.sdrLutPath].second;
            std::cout << "SDR LUT (cached): ";
            std::wcout << config.sdrLutPath << std::endl;
        } else {
            std::cout << "Loading SDR LUT: ";
            std::wcout << config.sdrLutPath << std::endl;
            if (!LoadLUT(config.sdrLutPath, lutDataSDR, ctx.lutSizeSDR)) {
                std::cerr << "Failed to load SDR LUT for monitor " << ctx.index << std::endl;
                return 1;
            }
            lutCache[config.sdrLutPath] = { lutDataSDR, ctx.lutSizeSDR };
        }

        // Load HDR LUT if specified
        std::vector<float> lutDataHDR;
        bool hasHDRLUT = false;
        if (!config.hdrLutPath.empty()) {
            if (lutCache.find(config.hdrLutPath) != lutCache.end()) {
                lutDataHDR = lutCache[config.hdrLutPath].first;
                ctx.lutSizeHDR = lutCache[config.hdrLutPath].second;
                hasHDRLUT = true;
                std::cout << "HDR LUT (cached): ";
                std::wcout << config.hdrLutPath << std::endl;
            } else {
                std::cout << "Loading HDR LUT: ";
                std::wcout << config.hdrLutPath << std::endl;
                if (LoadLUT(config.hdrLutPath, lutDataHDR, ctx.lutSizeHDR)) {
                    hasHDRLUT = true;
                    lutCache[config.hdrLutPath] = { lutDataHDR, ctx.lutSizeHDR };
                } else {
                    std::cerr << "Warning: Failed to load HDR LUT for monitor " << ctx.index << std::endl;
                }
            }
        }

        // Create window for this monitor
        wchar_t windowTitle[64];
        swprintf_s(windowTitle, L"DesktopLUT_Monitor%d", ctx.index);

        ctx.hwnd = CreateWindowEx(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            g_windowClassName, windowTitle,
            WS_POPUP,
            ctx.x, ctx.y, ctx.width, ctx.height,
            nullptr, nullptr, wc.hInstance, nullptr);

        if (!ctx.hwnd) {
            std::cerr << "Failed to create window for monitor " << ctx.index << std::endl;
            return 1;
        }

        // Exclude from capture to prevent feedback loop
        SetWindowDisplayAffinity(ctx.hwnd, WDA_EXCLUDEFROMCAPTURE);

        // Initialize desktop duplication for this monitor
        if (!InitDesktopDuplication(&ctx)) {
            std::cerr << "Failed to initialize desktop duplication for monitor " << ctx.index << std::endl;
            DestroyWindow(ctx.hwnd);
            return 1;
        }

        // Check HDR LUT requirement
        if (ctx.isHDREnabled && !hasHDRLUT) {
            std::cerr << "ERROR: Monitor " << ctx.index << " is in HDR mode but no HDR LUT provided." << std::endl;
            std::cerr << "HDR and SDR require separate calibration LUTs." << std::endl;
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            return 1;
        }

        // Create swapchain
        if (!CreateSwapChain(&ctx)) {
            std::cerr << "Failed to create swapchain for monitor " << ctx.index << std::endl;
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            return 1;
        }

        // Initialize DirectComposition
        if (!InitDirectComposition(&ctx)) {
            std::cerr << "Failed to initialize DirectComposition for monitor " << ctx.index << std::endl;
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            return 1;
        }

        // Create LUT textures
        if (!CreateLUTTexture(lutDataSDR, ctx.lutSizeSDR, &ctx.lutTextureSDR, &ctx.lutSRV_SDR)) {
            std::cerr << "Failed to create SDR LUT texture for monitor " << ctx.index << std::endl;
            ReleaseMonitorD3DResources(&ctx);
            DestroyWindow(ctx.hwnd);
            return 1;
        }

        if (hasHDRLUT) {
            if (!CreateLUTTexture(lutDataHDR, ctx.lutSizeHDR, &ctx.lutTextureHDR, &ctx.lutSRV_HDR)) {
                std::cerr << "Warning: Failed to create HDR LUT texture for monitor " << ctx.index << std::endl;
            }
        }

        // Don't show window yet - render loop will show it after first frame is rendered
        g_monitors.push_back(ctx);
    }

    if (g_monitors.empty()) {
        std::cerr << "No monitors initialized" << std::endl;
        return 1;
    }

    // Use first monitor's window for hotkey registration
    g_mainHwnd = g_monitors[0].hwnd;

    // Register global hotkeys (CLI mode uses hardcoded defaults, MOD_NOREPEAT prevents repeat when held)
    RegisterHotKey(g_mainHwnd, HOTKEY_GAMMA, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, 'G');
    RegisterHotKey(g_mainHwnd, HOTKEY_ANALYSIS, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, 'X');
    RegisterHotKey(g_mainHwnd, HOTKEY_HDR_TOGGLE, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, 'Z');

    // Register for display power state notifications (display sleep/wake)
    RegisterDisplayPowerNotification(g_mainHwnd);

    // Create OSD window for feedback
    CreateOSDWindow(wc.hInstance);

    // Create analysis overlay
    CreateAnalysisOverlay(wc.hInstance);

    std::cout << "\n=== DesktopLUT Running ===" << std::endl;
    std::cout << "Active monitors: " << g_monitors.size() << std::endl;
    for (const auto& ctx : g_monitors) {
        std::cout << "  Monitor " << ctx.index << ": " << ctx.width << "x" << ctx.height
                  << " [" << (ctx.isHDREnabled ? "HDR" : "SDR") << "]" << std::endl;
    }
    std::cout << "Gamma mode: " << (g_desktopGammaMode ? "Desktop (2.2 boost)" : "Content (sRGB)") << std::endl;
    std::cout << "LUT interpolation: " << (g_tetrahedralInterp ? "Tetrahedral" : "Trilinear") << std::endl;
    std::cout << "Press Win+Shift+G to toggle gamma mode" << std::endl;
    std::cout << "Press Win+Shift+Z to toggle HDR on focused monitor" << std::endl;
    std::cout << "Press Win+Shift+X to toggle analysis overlay" << std::endl;

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
        }
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

    // Cleanup monitors
    for (auto& ctx : g_monitors) {
        CleanupMonitorContext(&ctx);
    }
    g_monitors.clear();

    // Cleanup shared resources
    if (g_dcompDevice) g_dcompDevice->Release();
    if (g_blueNoiseSRV) g_blueNoiseSRV->Release();
    if (g_blueNoiseTexture) g_blueNoiseTexture->Release();
    if (g_constantBuffer) g_constantBuffer->Release();
    if (g_samplerPoint) g_samplerPoint->Release();
    if (g_samplerLinear) g_samplerLinear->Release();
    if (g_samplerWrap) g_samplerWrap->Release();
    if (g_peakDetectCS) g_peakDetectCS->Release();
    if (g_peakCB) g_peakCB->Release();
    if (g_analysisCS) g_analysisCS->Release();
    if (g_analysisCB) g_analysisCB->Release();
    if (g_ps) g_ps->Release();
    if (g_vs) g_vs->Release();
    if (g_context) g_context->Release();
    if (g_device) g_device->Release();

    // Release single instance mutex
    if (g_singleInstanceMutex) CloseHandle(g_singleInstanceMutex);

    return 0;
}
