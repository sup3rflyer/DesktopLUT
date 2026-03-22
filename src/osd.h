// DesktopLUT - osd.h
// On-screen display notification

#pragma once

#include <windows.h>

// OSD window procedure
LRESULT CALLBACK OSDWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Show OSD with message (must be called from the thread that owns g_mainHwnd)
void ShowOSD(const wchar_t* message);

// Thread-safe OSD request — posts WM_SHOW_OSD to the overlay window.
// Safe to call from any thread (whitelist thread, GUI thread, etc.).
void RequestShowOSD(const wchar_t* message);

// Hide OSD
void HideOSD();

// Create OSD window
bool CreateOSDWindow(HINSTANCE hInstance);

// Clean up cached OSD font
void DestroyOSDFont();
