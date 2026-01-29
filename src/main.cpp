// DesktopLUT - main.cpp
// Entry point only

#include "types.h"
#include "globals.h"
#include "gui.h"
#include <objbase.h>

// ============================================================================
// Entry Point (Windows subsystem)
// ============================================================================

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;

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

    int result = RunGUI();
    CloseHandle(g_singleInstanceMutex);
    return result;
}
