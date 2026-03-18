// DesktopLUT - dwm_inject.h
// DWM Hook DLL injection/uninjection

#pragma once

#include <string>
#include <vector>
#include "../shared/dwm_hook_config.h"

struct DwmHookMonitorLUT {
    int left = 0, top = 0;           // Desktop position from MONITORINFO
    std::wstring sdrLutPath;          // SDR .cube path (empty = no SDR LUT)
    std::wstring hdrLutPath;          // HDR .cube path (empty = no HDR LUT)
};

// Check if DwmHook.dll is loaded in any dwm.exe process (requires SYSTEM elevation, slow)
bool IsDwmHookInjected();

// Check if the DWM hook is active via named event (lightweight, no elevation needed).
// The injected DLL creates Global\DesktopLUT_DwmHook_Active on attach.
bool IsDwmHookActive();

// Inject DwmHook.dll into dwm.exe with the given LUT configuration.
// Stages LUT files to %SYSTEMROOT%\Temp\DesktopLUT_luts\, copies DLL,
// elevates to SYSTEM via lsass token, and injects via CreateRemoteThread.
// Returns empty string on success, error message on failure.
std::wstring InjectDwmHook(const std::vector<DwmHookMonitorLUT>& monitors);

// Uninject DwmHook.dll from all dwm.exe processes via FreeLibrary.
// Returns empty string on success, error message on failure.
std::wstring UninjectDwmHook();

// --- Shared memory IPC (live parameter updates to hook without re-injection) ---

// Create shared memory mapping. Called before injection so DLL can open it in DLL_PROCESS_ATTACH.
bool CreateDwmHookSharedMemory();

// Update shared memory with current tonemap params. Called on GUI changes and WM_DISPLAYCHANGE.
void UpdateDwmHookSharedConfig();

// Close shared memory mapping. Called on uninject/stop.
void CloseDwmHookSharedMemory();

