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

// Update color correction controls for current monitor
void UpdateColorCorrectionControls();

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
