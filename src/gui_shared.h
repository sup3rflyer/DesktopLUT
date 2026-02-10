// DesktopLUT - gui_shared.h
// Shared GUI helpers: fonts, brushes, drawing, numeric input

#pragma once

#include <windows.h>
#include <commctrl.h>

// Custom colors for Windows 11-like scheme
extern HBRUSH g_tabBgBrush;
extern HBRUSH g_inactiveTabBrush;
extern HFONT g_mainFont;
extern HFONT g_grayscaleFont;
extern const COLORREF TAB_BG_COLOR;
extern const COLORREF INACTIVE_TAB_COLOR;

// Slider range is +/-2500 representing +/-25.00% with 0.01 precision
extern const int GRAYSCALE_SLIDER_SCALE;

// Helper functions for grayscale editor
void UpdateEditFromSlider(int index);
void UpdateSliderFromEdit(int index);

// Numeric edit box subclass - filters input to only allow valid decimal numbers
LRESULT CALLBACK NumericEditSubclassProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam,
                                          UINT_PTR uIdSubclass, DWORD_PTR dwRefData);

// Apply numeric validation to an edit control
void SetNumericEdit(HWND hwnd, int maxDecimals);

// Helper to set path text - shows just the filename for readability
void SetPathText(HWND hwndEdit, const wchar_t* path);

// Draw a Windows 11-style rounded button
void DrawRoundedButton(LPDRAWITEMSTRUCT pDIS);
