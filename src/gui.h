// DesktopLUT - gui.h
// Main GUI window and controls

#pragma once

#include <windows.h>
#include "types.h"
#include "gui_shared.h"
#include "gui_mhc.h"
#include "gui_whitelist.h"

// Update GUI state (enable/disable controls)
void UpdateGUIState();

// Set status message
void SetStatus(const wchar_t* text);

// Browse for LUT file
bool BrowseForLUT(HWND hwndParent, wchar_t* path, size_t pathSize);

// Update color correction controls for current monitor. Edits that have the keyboard focus
// are left alone (a value being typed) unless forceEdits — a change of the selected display,
// where the half-typed value belongs to the other display.
void UpdateColorCorrectionControls(bool forceEdits = false);

// Show monitor `index`'s settings in every per-monitor control and highlight it in the list
// (clamped to the live range). The one place the selected monitor changes.
void SelectMonitor(int index);

// GUI-thread pin on g_gui.monitorSettings: while held, a handler keeps a reference / index
// into the vector across a message pump (editor dialogs, the preview spin-up that precedes
// them, the pipe's grayscale live-begin), so a display change pumped meanwhile must not
// re-attach (move entries / shrink) underneath it — the re-attach is deferred and retried.
// Take it BEFORE the first pump, which is earlier than g_mhcEditDialogOpen is set (that flag
// also switches the overlay preview on, so it cannot simply be raised sooner).
extern int g_monitorSettingsPins;
struct MonitorSettingsPin {
    MonitorSettingsPin() { g_monitorSettingsPins++; }
    ~MonitorSettingsPin() { g_monitorSettingsPins--; }
    MonitorSettingsPin(const MonitorSettingsPin&) = delete;
    MonitorSettingsPin& operator=(const MonitorSettingsPin&) = delete;
};

// Rebuild g_gui.monitors / g_gui.monitorNames (swapped under g_monitorSettingsMutex — the
// calibration pipe thread reads both under it) and the monitor list box's entries.
void SetLiveMonitors(const std::vector<HMONITOR>& monitors);
// Hook FALD: one full-screen DWM recomposition (primes the layer's clean copy / clears corrected pixels). Any thread.
void RequestFaldFullRecompose();
// Any mode: force one full-screen DWM composition (a fresh Desktop Duplication frame of a static desktop).
void RequestDesktopRecompose();
// Hook FALD: a panel file was set or changed — re-inject so the DLL loads it (hook mode, running). GUI thread.
void FaldPanelFileChangedReinject();

// Startup registry functions
bool IsStartupEnabled();
void SetStartupEnabled(bool enable);
void UpdateStartupPath();

// Tray icon functions
void AddTrayIcon(HWND hwnd);
void RemoveTrayIcon();
void UpdateTrayIcon(bool active);
void ShowTrayMenu(HWND hwnd);

// Grayscale editor
void ShowGrayscaleEditor(HWND hwndParent, GrayscaleSettings& settings, bool isHDR,
                         std::function<void()> liveUpdateCallback = nullptr);

// GUI layout creation (WM_CREATE body, in gui_layout.cpp)
void CreateGUILayout(HWND hwnd);
LRESULT CALLBACK ScrollPanelProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);
BOOL CALLBACK GUIMonitorEnumProc(HMONITOR hMonitor, HDC hdc, LPRECT lprcMonitor, LPARAM lParam);

// Display power notification (shared between gui.cpp and gui_layout.cpp)
extern HPOWERNOTIFY g_guiDisplayPowerNotify;
extern const GUID GUID_CONSOLE_DISPLAY_STATE_GUI;

// DWM-hook identity beacon (twin-panel LUT routing) — gui.cpp
void StartDwmHookBeacon(HWND hwnd, const char* why);      // timer-driven session; no-op without twins / hook mode
void RunDwmHookBeaconBlocking(HWND hwnd, const char* why); // same, ticked in place (calibration-pipe handlers)
void StopDwmHookBeacon(HWND hwnd);
void RefreshHookRoutingLabel();                             // Settings tab status line

// Main GUI window procedure
LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Run GUI mode (entry point)
int RunGUI();
