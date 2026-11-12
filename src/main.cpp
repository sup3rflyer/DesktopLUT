// DesktopLUT - main.cpp
// Entry point only

#include "types.h"
#include "globals.h"
#include "gui.h"
#include "crash_handler.h"
#include "uninstall_win.h"
#include "actions/logcap.h"
#include <objbase.h>
#include <shellapi.h>

// A switch on the command line: /name, -name or --name, any case.
static bool HasSwitch(const wchar_t* name) {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return false;
    bool found = false;
    for (int i = 1; i < argc && !found; i++) {
        const wchar_t* a = argv[i];
        if (a[0] == L'-' && a[1] == L'-') a += 2;
        else if (a[0] == L'/' || a[0] == L'-') a += 1;
        else continue;
        found = _wcsicmp(a, name) == 0;
    }
    LocalFree(argv);
    return found;
}

// ============================================================================
// Entry Point (Windows subsystem)
// ============================================================================

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;

    // Remove everything acts on the real PC only from this process (the test exe links the same code, never this).
    uninst::ArmRealRun();

    // Minidump on any crash / std::terminate (crashdumps\ next to the exe).
    InstallCrashHandler();

    // Initialize COM for DirectComposition and shell APIs
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    // `/uninstall [/quiet]`: remove everything DesktopLUT changed outside its folder (uninstall_win.h), no window.
    const bool uninstall = HasSwitch(L"uninstall");
    const bool quiet = HasSwitch(L"quiet");

    // Single instance check - prevent multiple copies from running
    g_singleInstanceMutex = CreateMutexW(nullptr, TRUE, L"DesktopLUT_SingleInstance_Mutex");
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        HWND existingWnd = FindWindowW(L"DesktopLUT_GUI", L"DesktopLUT");
        if (uninstall) {
            // The running instance holds the hook and the profiles, and would put back what this removes: it exits
            // first (its ordered teardown, as the tray's Exit), then this process owns the mutex and goes on.
            const UINT exitMsg = uninst::ExitForUninstallMessage();
            if (existingWnd && exitMsg) PostMessageW(existingWnd, exitMsg, 0, 0);
            const DWORD w = g_singleInstanceMutex ? WaitForSingleObject(g_singleInstanceMutex, 60000) : WAIT_FAILED;
            if (w != WAIT_OBJECT_0 && w != WAIT_ABANDONED) {
                if (g_singleInstanceMutex) CloseHandle(g_singleInstanceMutex);
                return uninst::ReportOtherInstanceRunning(quiet);
            }
            const int code = uninst::RunCommandLine(quiet);
            ReleaseMutex(g_singleInstanceMutex);
            CloseHandle(g_singleInstanceMutex);
            return code;
        }
        // Another instance is running: ask it to show the interface it uses (the classic window is its
        // hub; the new one is a separate window only it knows about). This process was just launched by
        // the user, so it may hand the foreground on.
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

    if (uninstall) {
        const int code = uninst::RunCommandLine(quiet);
        if (g_singleInstanceMutex) {
            ReleaseMutex(g_singleInstanceMutex);
            CloseHandle(g_singleInstanceMutex);
        }
        return code;
    }

    logcap::Install();   // the diagnostic log for the Events page, before anything else writes
    int result = RunGUI();
    CloseHandle(g_singleInstanceMutex);
    return result;
}
