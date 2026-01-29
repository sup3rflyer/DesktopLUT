// DesktopLUT - gui.h
// Main GUI window and controls

#pragma once

#include <windows.h>
#include "types.h"

// Update GUI state (enable/disable controls)
void UpdateGUIState();

// Set status message
void SetStatus(const wchar_t* text);

// Browse for LUT file
bool BrowseForLUT(HWND hwndParent, wchar_t* path, size_t pathSize);

// Startup registry functions
bool IsStartupEnabled();
void SetStartupEnabled(bool enable);
void UpdateStartupPath();

// Tray icon functions
void AddTrayIcon(HWND hwnd);
void RemoveTrayIcon();
void ShowTrayMenu(HWND hwnd);

// Grayscale editor
void ShowGrayscaleEditor(HWND hwndParent, GrayscaleSettings& settings, bool isHDR);
LRESULT CALLBACK GrayscaleEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Gamma whitelist dialog
void ShowGammaWhitelistDialog(HWND hwndParent);

// Main GUI window procedure
LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Run GUI mode (entry point)
int RunGUI();
