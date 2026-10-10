// DesktopLUT - main.cpp
// Entry point only

#include "types.h"
#include "globals.h"
#include "gui.h"
#include "crash_handler.h"
#include <objbase.h>

// ============================================================================
// Entry Point (Windows subsystem)
// ============================================================================

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;

    // Minidump on any crash / std::terminate (crashdumps\ next to the exe).
    InstallCrashHandler();

    // Initialize COM for DirectComposition and shell APIs
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    // Single instance check - prevent multiple copies from running
    g_singleInstanceMutex = CreateMutexW(nullptr, TRUE, L"DesktopLUT_SingleInstance_Mutex");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        // Another instance is running: ask it to show the interface it uses (the classic window is its
        // hub; the new one is a separate window only it knows about). This process was just launched by
        // the user, so it may hand the foreground on.
        HWND existingWnd = FindWindowW(L"DesktopLUT_GUI", L"DesktopLUT");
        if (existingWnd) {
            DWORD pid = 0;
            GetWindowThreadProcessId(existingWnd, &pid);
            if (pid) AllowSetForegroundWindow(pid);
            const UINT showUi = RegisterWindowMessageW(DESKTOPLUT_SHOW_UI_MESSAGE);
            if (!showUi || !PostMessageW(existingWnd, showUi, 0, 0)) {
                ShowWindow(existingWnd, SW_RESTORE);
                SetForegroundWindow(existingWnd);
            }
        }
        CloseHandle(g_singleInstanceMutex);
        return 0;
    }

    int result = RunGUI();
    CloseHandle(g_singleInstanceMutex);
    return result;
}
