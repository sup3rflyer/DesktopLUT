// DesktopLUT - dwm_inject.h
// DWM Hook DLL injection/uninjection

#pragma once

#include <cstdint>
#include <string>
#include <vector>
#include "../shared/dwm_hook_config.h"

struct DwmHookMonitorLUT {
    int left = 0, top = 0;           // Desktop position from MONITORINFO
    std::wstring sdrLutPath;          // SDR .cube path (empty = no SDR LUT)
    std::wstring hdrLutPath;          // HDR .cube path (empty = no HDR LUT)
};

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

// Invalidate cached DXGI monitor info (call on WM_DISPLAYCHANGE).
void InvalidateDxgiMonitorCache();

// True when at least two monitors are indistinguishable to the hook (same size + bit depth):
// the only rigs where the identity beacon has anything to tell apart.
bool DwmHookHasTwinMonitors();

// --- Twin-panel routing (25H2 first-present order-match) ---------------------
// The hook DLL persists its overlay-context -> monitor assignment in
// DWM_HOOK_ROUTING_FILE_W (see dwm_hook_config.h). The host reads it for state.get
// and rewrites it for hook.set_routing; the DLL honours the rewritten pins on its
// next injection of the same dwm.exe.
struct DwmHookRoutingMon { int left = 0, top = 0, width = 0, height = 0, bpc = 0; };
struct DwmHookRoutingEntry {
    std::string ctx;      // hex pointer, lower-case, no 0x (as the DLL writes it)
    int left = 0, top = 0;
    std::string method;   // unique|bpc|scan|pinned|order|legacy|provisional|replaced|beacon|unknown
};
struct DwmHookRouting {
    bool present = false;          // a routing file exists and parsed
    std::string session;           // "<pid>-<createHigh>-<createLow>" of the dwm.exe that wrote it
    bool stale = false;            // that dwm.exe is no longer running (DWM restarted since)
    bool confirmed = false;        // a client verified the assignment through a meter
    std::vector<DwmHookRoutingMon> monitors;
    std::vector<DwmHookRoutingEntry> entries;
};
DwmHookRouting ReadDwmHookRouting();
bool WriteDwmHookRouting(const DwmHookRouting& r);   // rewrites the file (keeps the DLL's session key)
bool ClearDwmHookRouting();                          // deletes it — the next injection re-rolls
// Swap the assignment of the monitor at (left, top) with its single indistinguishable twin
// (same size + bpc): every context recorded at either position moves to the other one.
// Returns an error message (empty on success). The caller re-injects.
std::wstring SwapDwmHookRouting(DwmHookRouting& r, int left, int top);

