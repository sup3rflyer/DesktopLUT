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

// Main GUI window procedure
LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Run GUI mode (entry point)
int RunGUI();
