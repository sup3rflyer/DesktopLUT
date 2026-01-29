// DesktopLUT - osd.h
// On-screen display notification

#pragma once

#include <windows.h>

// OSD window procedure
LRESULT CALLBACK OSDWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Show OSD with message
void ShowOSD(const wchar_t* message);

// Hide OSD
void HideOSD();

// Create OSD window
bool CreateOSDWindow(HINSTANCE hInstance);
