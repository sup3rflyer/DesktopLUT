// DesktopLUT - gui.cpp
// Main GUI window and controls

#include "gui.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "color.h"
#include "osd.h"
#include "displayconfig.h"
#include "../resource.h"
#include <commctrl.h>
#include <commdlg.h>
#include <algorithm>
#include <iostream>
#include <cstdio>

#pragma comment(lib, "comctl32.lib")

// Custom colors for Windows 11-like scheme
static HBRUSH g_tabBgBrush = nullptr;       // Active tab content: #f9f9f9
static HBRUSH g_inactiveTabBrush = nullptr; // Inactive tabs: #f3f3f3
static HFONT g_mainFont = nullptr;          // Main window font
static HFONT g_grayscaleFont = nullptr;     // Grayscale editor font
static const COLORREF TAB_BG_COLOR = RGB(0xf9, 0xf9, 0xf9);
static const COLORREF INACTIVE_TAB_COLOR = RGB(0xf3, 0xf3, 0xf3);

// Note: HDR swapchain metadata (MaxCLL) is always set to 10000 nits
// MaxTML override controls Windows tonemapping behavior system-wide

// Helper functions for grayscale editor
// Slider range is ±2500 representing ±25.00% with 0.01 precision
static const int GRAYSCALE_SLIDER_SCALE = 100;  // Slider units per 1%

static void UpdateEditFromSlider(int index) {
    auto* data = g_grayscaleEditor;
    if (!data || data->updatingFromEdit) return;

    data->updatingFromSlider = true;
    int pos = (int)SendMessage(data->sliders[index], TBM_GETPOS, 0, 0);
    float deviation = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;  // Negate because trackbar is inverted
    wchar_t text[16];
    swprintf_s(text, L"%.2f", deviation);
    SetWindowText(data->editBoxes[index], text);
    data->updatingFromSlider = false;
}

static void UpdateSliderFromEdit(int index) {
    auto* data = g_grayscaleEditor;
    if (!data || data->updatingFromSlider) return;

    data->updatingFromEdit = true;
    wchar_t text[16];
    GetWindowText(data->editBoxes[index], text, 16);
    float deviation = (float)_wtof(text);
    int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;
    int sliderVal = (int)(deviation * GRAYSCALE_SLIDER_SCALE + 0.5f);
    sliderVal = (std::max)(-maxRange, (std::min)(maxRange, sliderVal));
    SendMessage(data->sliders[index], TBM_SETPOS, TRUE, -sliderVal);
    data->updatingFromEdit = false;
}

// Numeric edit box subclass - filters input to only allow valid decimal numbers
// maxDecimals is stored in dwRefData (set when subclassing)
// Also handles Enter key to commit and unfocus
static LRESULT CALLBACK NumericEditSubclassProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam,
                                                  UINT_PTR uIdSubclass, DWORD_PTR dwRefData) {
    int maxDecimals = (int)dwRefData;

    // Handle Enter key - commit value and move focus to parent
    if (msg == WM_KEYDOWN && wParam == VK_RETURN) {
        HWND parent = GetParent(hwnd);
        if (parent) {
            SetFocus(parent);  // This triggers EN_KILLFOCUS which commits the value
        }
        return 0;  // Don't pass Enter to edit control (would beep)
    }

    // Handle Escape key - restore original value and unfocus
    if (msg == WM_KEYDOWN && wParam == VK_ESCAPE) {
        HWND parent = GetParent(hwnd);
        if (parent) {
            SetFocus(parent);
        }
        return 0;
    }

    if (msg == WM_CHAR) {
        wchar_t ch = (wchar_t)wParam;
        // Allow control characters (backspace, etc.)
        if (ch < 32) return DefSubclassProc(hwnd, msg, wParam, lParam);

        // Only allow digits, '.', and '-'
        if (ch != L'.' && ch != L'-' && (ch < L'0' || ch > L'9')) return 0;

        // Get current text and selection
        wchar_t text[32] = {};
        GetWindowText(hwnd, text, 32);
        DWORD selStart, selEnd;
        SendMessage(hwnd, EM_GETSEL, (WPARAM)&selStart, (LPARAM)&selEnd);

        // Only allow one '.'
        if (ch == L'.') {
            wchar_t* dot = wcschr(text, L'.');
            if (dot && ((DWORD)(dot - text) < selStart || (DWORD)(dot - text) >= selEnd)) return 0;
        }

        // Only allow '-' at the start
        if (ch == L'-') {
            if (selStart != 0) return 0;
            if (text[0] == L'-' && selEnd == 0) return 0;
        }

        // Check decimal places limit
        if (ch >= L'0' && ch <= L'9') {
            wchar_t* dot = wcschr(text, L'.');
            if (dot) {
                int dotPos = (int)(dot - text);
                int textLen = (int)wcslen(text);
                int decimalsAfter = textLen - dotPos - 1 - (int)(selEnd - selStart);
                // If cursor is after dot, check decimals
                if ((int)selStart > dotPos && decimalsAfter >= maxDecimals) return 0;
            }
        }
    }
    else if (msg == WM_PASTE) {
        if (OpenClipboard(hwnd)) {
            HANDLE hData = GetClipboardData(CF_UNICODETEXT);
            if (hData) {
                wchar_t* clipText = (wchar_t*)GlobalLock(hData);
                if (clipText) {
                    // Clean the pasted text: keep only digits, '.', '-'
                    wchar_t clean[32] = {};
                    int cleanIdx = 0;
                    bool hasDot = false;
                    bool hasNeg = false;
                    int decimals = 0;

                    for (int i = 0; clipText[i] && cleanIdx < 30; i++) {
                        wchar_t ch = clipText[i];
                        if (ch == L'-' && cleanIdx == 0 && !hasNeg) {
                            clean[cleanIdx++] = ch;
                            hasNeg = true;
                        } else if (ch == L'.' && !hasDot) {
                            clean[cleanIdx++] = ch;
                            hasDot = true;
                            decimals = 0;
                        } else if (ch >= L'0' && ch <= L'9') {
                            if (hasDot) {
                                if (decimals < maxDecimals) {
                                    clean[cleanIdx++] = ch;
                                    decimals++;
                                }
                            } else {
                                clean[cleanIdx++] = ch;
                            }
                        }
                    }
                    clean[cleanIdx] = 0;

                    GlobalUnlock(hData);
                    CloseClipboard();

                    // Replace selection with cleaned text
                    SendMessage(hwnd, EM_REPLACESEL, TRUE, (LPARAM)clean);
                    return 0;
                }
                GlobalUnlock(hData);
            }
            CloseClipboard();
        }
        return 0;
    }

    return DefSubclassProc(hwnd, msg, wParam, lParam);
}

// Apply numeric validation to an edit control
static void SetNumericEdit(HWND hwnd, int maxDecimals) {
    SetWindowSubclass(hwnd, NumericEditSubclassProc, 0, (DWORD_PTR)maxDecimals);
}

// Helper to set path text - shows just the filename for readability
static void SetPathText(HWND hwndEdit, const wchar_t* path) {
    if (!path || !*path) {
        SetWindowText(hwndEdit, L"");
        return;
    }
    // Extract just the filename
    const wchar_t* filename = wcsrchr(path, L'\\');
    if (!filename) filename = wcsrchr(path, L'/');
    SetWindowText(hwndEdit, filename ? filename + 1 : path);
}

// Draw a Windows 11-style rounded button
static void DrawRoundedButton(LPDRAWITEMSTRUCT pDIS) {
    bool isDisabled = (pDIS->itemState & ODS_DISABLED) != 0;
    bool isPressed = (pDIS->itemState & ODS_SELECTED) != 0;
    bool isFocused = (pDIS->itemState & ODS_FOCUS) != 0;

    HDC hdc = pDIS->hDC;
    RECT rc = pDIS->rcItem;

    // Windows 11 style colors
    COLORREF bgColor, textColor, borderColor;
    if (isDisabled) {
        bgColor = RGB(0xF0, 0xF0, 0xF0);
        textColor = RGB(0xA0, 0xA0, 0xA0);
        borderColor = RGB(0xD0, 0xD0, 0xD0);
    } else if (isPressed) {
        bgColor = RGB(0xE0, 0xE0, 0xE0);
        textColor = RGB(0x00, 0x00, 0x00);
        borderColor = RGB(0x80, 0x80, 0x80);
    } else {
        bgColor = RGB(0xFD, 0xFD, 0xFD);
        textColor = RGB(0x00, 0x00, 0x00);
        borderColor = RGB(0xC0, 0xC0, 0xC0);
    }

    // Create rounded rectangle region
    int radius = 4;
    HRGN hRgn = CreateRoundRectRgn(rc.left, rc.top, rc.right + 1, rc.bottom + 1, radius * 2, radius * 2);

    // Fill background
    HBRUSH hBrush = CreateSolidBrush(bgColor);
    FillRgn(hdc, hRgn, hBrush);
    DeleteObject(hBrush);

    // Draw border
    HPEN hPen = CreatePen(PS_SOLID, 1, borderColor);
    HPEN hOldPen = (HPEN)SelectObject(hdc, hPen);
    HBRUSH hOldBrush = (HBRUSH)SelectObject(hdc, GetStockObject(NULL_BRUSH));
    RoundRect(hdc, rc.left, rc.top, rc.right, rc.bottom, radius * 2, radius * 2);
    SelectObject(hdc, hOldBrush);
    SelectObject(hdc, hOldPen);
    DeleteObject(hPen);

    // Draw focus rectangle
    if (isFocused && !isDisabled) {
        RECT focusRect = rc;
        InflateRect(&focusRect, -3, -3);
        DrawFocusRect(hdc, &focusRect);
    }

    // Draw button text
    wchar_t text[64];
    GetWindowText(pDIS->hwndItem, text, 64);
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, textColor);
    DrawText(hdc, text, -1, &rc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

    DeleteObject(hRgn);
}

void UpdateGUIState() {
    // Monitor list, browse, clear buttons always enabled (can edit while running)
    EnableWindow(g_gui.hwndMonitorList, TRUE);
    EnableWindow(g_gui.hwndSdrPath, TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_SDR_BROWSE), TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_SDR_CLEAR), TRUE);
    EnableWindow(g_gui.hwndHdrPath, TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_HDR_BROWSE), TRUE);
    EnableWindow(GetDlgItem(g_gui.hwndMain, ID_HDR_CLEAR), TRUE);
    // Gamma checkbox stays enabled - can toggle while running
    EnableWindow(g_gui.hwndGammaCheck, TRUE);

    // Enable button: enabled if not running, OR if running but settings changed
    bool enableApply = !g_gui.isRunning || (g_gui.isRunning && SettingsChanged());
    EnableWindow(g_gui.hwndApply, enableApply);
    EnableWindow(g_gui.hwndStop, g_gui.isRunning);
}

void SetStatus(const wchar_t* text) {
    if (g_gui.hwndStatus) {
        SetWindowText(g_gui.hwndStatus, text);
    }
}

bool BrowseForLUT(HWND hwndParent, wchar_t* path, size_t pathSize) {
    OPENFILENAME ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = hwndParent;
    ofn.lpstrFilter = L"LUT Files (*.cube;*.txt)\0*.cube;*.txt\0All Files (*.*)\0*.*\0";
    ofn.lpstrFile = path;
    ofn.nMaxFile = (DWORD)pathSize;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
    ofn.lpstrTitle = L"Select LUT File";

    return GetOpenFileName(&ofn) == TRUE;
}

// Update color correction controls to reflect current monitor's settings
void UpdateColorCorrectionControls() {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    const auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];

    // SDR Primaries
    SendMessage(g_gui.hwndSdrPrimariesEnable, BM_SETCHECK,
        settings.sdrColorCorrection.primariesEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndSdrPrimariesPreset, CB_SETCURSEL,
        settings.sdrColorCorrection.primariesPreset, 0);

    // Update edit boxes to show current preset/custom values
    wchar_t buf[16];
    int sdrPreset = settings.sdrColorCorrection.primariesPreset;
    bool sdrCustom = (sdrPreset == g_numPresetPrimaries - 1);
    float sdrRx, sdrRy, sdrGx, sdrGy, sdrBx, sdrBy, sdrWx, sdrWy;
    if (sdrCustom) {
        const auto& cp = settings.sdrColorCorrection.customPrimaries;
        sdrRx = cp.Rx; sdrRy = cp.Ry; sdrGx = cp.Gx; sdrGy = cp.Gy;
        sdrBx = cp.Bx; sdrBy = cp.By; sdrWx = cp.Wx; sdrWy = cp.Wy;
    } else {
        const auto& p = g_presetPrimaries[sdrPreset];
        sdrRx = p.Rx; sdrRy = p.Ry; sdrGx = p.Gx; sdrGy = p.Gy;
        sdrBx = p.Bx; sdrBy = p.By; sdrWx = p.Wx; sdrWy = p.Wy;
    }
    swprintf_s(buf, L"%.4f", sdrRx); SetWindowText(g_gui.hwndSdrPrimariesRx, buf);
    swprintf_s(buf, L"%.4f", sdrRy); SetWindowText(g_gui.hwndSdrPrimariesRy, buf);
    swprintf_s(buf, L"%.4f", sdrGx); SetWindowText(g_gui.hwndSdrPrimariesGx, buf);
    swprintf_s(buf, L"%.4f", sdrGy); SetWindowText(g_gui.hwndSdrPrimariesGy, buf);
    swprintf_s(buf, L"%.4f", sdrBx); SetWindowText(g_gui.hwndSdrPrimariesBx, buf);
    swprintf_s(buf, L"%.4f", sdrBy); SetWindowText(g_gui.hwndSdrPrimariesBy, buf);
    swprintf_s(buf, L"%.4f", sdrWx); SetWindowText(g_gui.hwndSdrPrimariesWx, buf);
    swprintf_s(buf, L"%.4f", sdrWy); SetWindowText(g_gui.hwndSdrPrimariesWy, buf);

    // Enable/disable custom edit boxes based on preset
    EnableWindow(g_gui.hwndSdrPrimariesRx, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesRy, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesGx, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesGy, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesBx, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesBy, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesWx, sdrCustom);
    EnableWindow(g_gui.hwndSdrPrimariesWy, sdrCustom);

    // SDR Grayscale
    SendMessage(g_gui.hwndSdrGrayscaleEnable, BM_SETCHECK,
        settings.sdrColorCorrection.grayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndSdrGrayscale10, BM_SETCHECK,
        settings.sdrColorCorrection.grayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndSdrGrayscale20, BM_SETCHECK,
        settings.sdrColorCorrection.grayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndSdrGrayscale32, BM_SETCHECK,
        settings.sdrColorCorrection.grayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndSdrGrayscale24, BM_SETCHECK,
        settings.sdrColorCorrection.grayscale.use24Gamma ? BST_CHECKED : BST_UNCHECKED, 0);

    // HDR Primaries
    SendMessage(g_gui.hwndHdrPrimariesEnable, BM_SETCHECK,
        settings.hdrColorCorrection.primariesEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrPrimariesPreset, CB_SETCURSEL,
        settings.hdrColorCorrection.primariesPreset, 0);

    // Update edit boxes to show current preset/custom values
    int hdrPreset = settings.hdrColorCorrection.primariesPreset;
    bool hdrCustom = (hdrPreset == g_numPresetPrimaries - 1);
    float hdrRx, hdrRy, hdrGx, hdrGy, hdrBx, hdrBy, hdrWx, hdrWy;
    if (hdrCustom) {
        const auto& cp = settings.hdrColorCorrection.customPrimaries;
        hdrRx = cp.Rx; hdrRy = cp.Ry; hdrGx = cp.Gx; hdrGy = cp.Gy;
        hdrBx = cp.Bx; hdrBy = cp.By; hdrWx = cp.Wx; hdrWy = cp.Wy;
    } else {
        const auto& p = g_presetPrimaries[hdrPreset];
        hdrRx = p.Rx; hdrRy = p.Ry; hdrGx = p.Gx; hdrGy = p.Gy;
        hdrBx = p.Bx; hdrBy = p.By; hdrWx = p.Wx; hdrWy = p.Wy;
    }
    swprintf_s(buf, L"%.4f", hdrRx); SetWindowText(g_gui.hwndHdrPrimariesRx, buf);
    swprintf_s(buf, L"%.4f", hdrRy); SetWindowText(g_gui.hwndHdrPrimariesRy, buf);
    swprintf_s(buf, L"%.4f", hdrGx); SetWindowText(g_gui.hwndHdrPrimariesGx, buf);
    swprintf_s(buf, L"%.4f", hdrGy); SetWindowText(g_gui.hwndHdrPrimariesGy, buf);
    swprintf_s(buf, L"%.4f", hdrBx); SetWindowText(g_gui.hwndHdrPrimariesBx, buf);
    swprintf_s(buf, L"%.4f", hdrBy); SetWindowText(g_gui.hwndHdrPrimariesBy, buf);
    swprintf_s(buf, L"%.4f", hdrWx); SetWindowText(g_gui.hwndHdrPrimariesWx, buf);
    swprintf_s(buf, L"%.4f", hdrWy); SetWindowText(g_gui.hwndHdrPrimariesWy, buf);

    EnableWindow(g_gui.hwndHdrPrimariesRx, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesRy, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesGx, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesGy, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesBx, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesBy, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesWx, hdrCustom);
    EnableWindow(g_gui.hwndHdrPrimariesWy, hdrCustom);

    // HDR Grayscale
    SendMessage(g_gui.hwndHdrGrayscaleEnable, BM_SETCHECK,
        settings.hdrColorCorrection.grayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrGrayscale10, BM_SETCHECK,
        settings.hdrColorCorrection.grayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrGrayscale20, BM_SETCHECK,
        settings.hdrColorCorrection.grayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrGrayscale32, BM_SETCHECK,
        settings.hdrColorCorrection.grayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);
    wchar_t gsPeakBuf[16];
    swprintf_s(gsPeakBuf, L"%.0f", settings.hdrColorCorrection.grayscale.peakNits);
    SetWindowText(g_gui.hwndHdrGrayscalePeak, gsPeakBuf);

    // HDR Tonemapping
    SendMessage(g_gui.hwndHdrTonemapEnable, BM_SETCHECK,
        settings.hdrColorCorrection.tonemap.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrTonemapCurve, CB_SETCURSEL,
        TonemapCurveToDropdownIndex(settings.hdrColorCorrection.tonemap.curve), 0);
    wchar_t tonemapBuf[16];
    swprintf_s(tonemapBuf, L"%.0f", settings.hdrColorCorrection.tonemap.targetPeakNits);
    SetWindowText(g_gui.hwndHdrTonemapTarget, tonemapBuf);
    swprintf_s(tonemapBuf, L"%.0f", settings.hdrColorCorrection.tonemap.sourcePeakNits);
    SetWindowText(g_gui.hwndHdrTonemapSource, tonemapBuf);
    SendMessage(g_gui.hwndHdrTonemapDynamic, BM_SETCHECK,
        settings.hdrColorCorrection.tonemap.dynamicPeak ? BST_CHECKED : BST_UNCHECKED, 0);
    // Disable Source when Dynamic is checked
    EnableWindow(g_gui.hwndHdrTonemapSource, !settings.hdrColorCorrection.tonemap.dynamicPeak);

    // MaxTML
    SendMessage(g_gui.hwndHdrMaxTmlEnable, BM_SETCHECK,
        settings.maxTml.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    swprintf_s(tonemapBuf, L"%.0f", settings.maxTml.peakNits);
    SetWindowText(g_gui.hwndHdrMaxTmlEdit, tonemapBuf);
    // Select matching preset in combo, or Custom if no match
    float peakNits = settings.maxTml.peakNits;
    int comboSel = 0;  // Custom by default
    if (peakNits == 400.0f) comboSel = 1;
    else if (peakNits == 600.0f) comboSel = 2;
    else if (peakNits == 1000.0f) comboSel = 3;
    else if (peakNits == 1400.0f) comboSel = 4;
    else if (peakNits == 4000.0f) comboSel = 5;
    else if (peakNits == 10000.0f) comboSel = 6;
    SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_SETCURSEL, comboSel, 0);
}

// Helper to recalculate primaries matrix and apply live update
void ApplyPrimariesChange(bool isHDR) {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                     : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;

    // Calculate matrix from current primaries (display to sRGB)
    DisplayPrimariesData displayPrimaries;
    if (cc.primariesPreset == g_numPresetPrimaries - 1) {
        // Custom - use custom primaries
        displayPrimaries.Rx = cc.customPrimaries.Rx;
        displayPrimaries.Ry = cc.customPrimaries.Ry;
        displayPrimaries.Gx = cc.customPrimaries.Gx;
        displayPrimaries.Gy = cc.customPrimaries.Gy;
        displayPrimaries.Bx = cc.customPrimaries.Bx;
        displayPrimaries.By = cc.customPrimaries.By;
        displayPrimaries.Wx = cc.customPrimaries.Wx;
        displayPrimaries.Wy = cc.customPrimaries.Wy;
    } else {
        // Use preset
        const auto& preset = g_presetPrimaries[cc.primariesPreset];
        displayPrimaries.Rx = preset.Rx; displayPrimaries.Ry = preset.Ry;
        displayPrimaries.Gx = preset.Gx; displayPrimaries.Gy = preset.Gy;
        displayPrimaries.Bx = preset.Bx; displayPrimaries.By = preset.By;
        displayPrimaries.Wx = preset.Wx; displayPrimaries.Wy = preset.Wy;
    }

    // sRGB primaries (target for content)
    DisplayPrimariesData srgb = {
        0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f
    };

    CalculatePrimariesMatrix(srgb, displayPrimaries, cc.primariesMatrix);

    // Apply live update if running
    if (g_gui.isRunning) {
        UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
    }
    UpdateGUIState();
}

bool IsStartupEnabled() {
    HKEY hKey;
    if (RegOpenKeyEx(HKEY_CURRENT_USER, g_startupRegKey, 0, KEY_READ, &hKey) != ERROR_SUCCESS) {
        return false;
    }

    wchar_t value[MAX_PATH];
    DWORD valueSize = sizeof(value);
    DWORD type;
    bool exists = (RegQueryValueEx(hKey, g_startupValueName, nullptr, &type,
                                   (LPBYTE)value, &valueSize) == ERROR_SUCCESS);
    RegCloseKey(hKey);
    return exists;
}

void UpdateStartupPath() {
    // If startup is enabled but path is stale, update it to current exe location
    HKEY hKey;
    if (RegOpenKeyEx(HKEY_CURRENT_USER, g_startupRegKey, 0, KEY_READ | KEY_WRITE, &hKey) != ERROR_SUCCESS) {
        return;
    }

    wchar_t regPath[MAX_PATH];
    DWORD regPathSize = sizeof(regPath);
    DWORD type;
    if (RegQueryValueEx(hKey, g_startupValueName, nullptr, &type,
                        (LPBYTE)regPath, &regPathSize) != ERROR_SUCCESS) {
        RegCloseKey(hKey);
        return;  // Not enabled, nothing to update
    }

    // Get current exe path
    wchar_t currentPath[MAX_PATH];
    GetModuleFileName(nullptr, currentPath, MAX_PATH);

    // Compare paths (case-insensitive)
    if (_wcsicmp(regPath, currentPath) != 0) {
        // Path changed, update registry
        RegSetValueEx(hKey, g_startupValueName, 0, REG_SZ,
                      (LPBYTE)currentPath, (DWORD)((wcslen(currentPath) + 1) * sizeof(wchar_t)));
    }

    RegCloseKey(hKey);
}

void SetStartupEnabled(bool enable) {
    HKEY hKey;
    if (RegOpenKeyEx(HKEY_CURRENT_USER, g_startupRegKey, 0, KEY_WRITE, &hKey) != ERROR_SUCCESS) {
        return;
    }

    if (enable) {
        // Get the path to the current executable
        wchar_t exePath[MAX_PATH];
        GetModuleFileName(nullptr, exePath, MAX_PATH);

        // Set the registry value (just the exe path, no arguments - GUI mode)
        RegSetValueEx(hKey, g_startupValueName, 0, REG_SZ,
                      (LPBYTE)exePath, (DWORD)((wcslen(exePath) + 1) * sizeof(wchar_t)));
    } else {
        // Delete the registry value
        RegDeleteValue(hKey, g_startupValueName);
    }

    RegCloseKey(hKey);
}

void AddTrayIcon(HWND hwnd) {
    g_gui.nid.cbSize = sizeof(NOTIFYICONDATA);
    g_gui.nid.hWnd = hwnd;
    g_gui.nid.uID = ID_TRAY_ICON;
    g_gui.nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    g_gui.nid.uCallbackMessage = WM_TRAYICON;
    g_gui.nid.hIcon = LoadIcon(GetModuleHandle(nullptr), MAKEINTRESOURCE(IDI_APPICON));
    wcscpy_s(g_gui.nid.szTip, L"DesktopLUT");
    Shell_NotifyIcon(NIM_ADD, &g_gui.nid);
}

void RemoveTrayIcon() {
    Shell_NotifyIcon(NIM_DELETE, &g_gui.nid);
}

void ShowTrayMenu(HWND hwnd) {
    POINT pt;
    GetCursorPos(&pt);

    HMENU hMenu = CreatePopupMenu();
    AppendMenu(hMenu, MF_STRING, ID_TRAY_SHOW, L"Show");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, g_gui.isRunning ? MF_GRAYED : MF_STRING, ID_TRAY_APPLY, L"Enable");
    AppendMenu(hMenu, g_gui.isRunning ? MF_STRING : MF_GRAYED, ID_TRAY_STOP, L"Disable");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, IsStartupEnabled() ? (MF_STRING | MF_CHECKED) : MF_STRING,
               ID_TRAY_STARTUP, L"Run at startup");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, MF_STRING, ID_TRAY_EXIT, L"Exit");

    SetForegroundWindow(hwnd);
    TrackPopupMenu(hMenu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, nullptr);
    DestroyMenu(hMenu);
}

LRESULT CALLBACK GrayscaleEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_CREATE: {
        auto* data = g_grayscaleEditor;
        if (!data) return -1;

        data->hwndDialog = hwnd;

        int sliderW = 32;
        int sliderH = 150;
        int rgbLabelH = 16;   // Code value at top (8-bit SDR, 10-bit HDR)
        int pctLabelH = 16;   // Input percentage below slider
        int editH = 20;
        int pad = 2;
        int startX = 10;
        int startY = 10;

        // Calculate pqPeak for HDR label scaling (same formula as shader)
        float pqPeak = 1.0f;
        if (data->isHDR && data->peakNits < 10000.0f) {
            float peakLinear = data->peakNits / 10000.0f;
            float m1 = 0.1593017578125f, m2 = 78.84375f;
            float c1 = 0.8359375f, c2 = 18.8515625f, c3 = 18.6875f;
            float peakYm = powf(peakLinear, m1);
            pqPeak = powf((c1 + c2 * peakYm) / (1.0f + c3 * peakYm), m2);
        }

        // Create sliders, labels, and edit boxes
        // SDR: Square root distribution (input = (i/(N-1))^2) for more shadow granularity
        // HDR: Scaled by peak so labels match ColourSpace target (e.g., 1400 nits)
        for (int i = 0; i < data->pointCount; i++) {
            int x = startX + i * (sliderW + pad);

            float t = (float)i / (float)(data->pointCount - 1);
            // HDR: scale by pqPeak so labels match ColourSpace patch positions
            // SDR: sqrt distribution for shadow granularity
            float inputNorm = data->isHDR ? (t * pqPeak) : (t * t);

            // Top label: code value (8-bit for SDR, 10-bit for HDR to match ColourSpace)
            int codeValue = data->isHDR ? (int)(inputNorm * 1023.0f + 0.5f) : (int)(inputNorm * 255.0f + 0.5f);
            wchar_t rgbLabel[8];
            swprintf_s(rgbLabel, L"%d", codeValue);
            CreateWindow(L"STATIC", rgbLabel, WS_CHILD | WS_VISIBLE | SS_CENTER,
                x, startY, sliderW, rgbLabelH, hwnd, nullptr, nullptr, nullptr);

            // Vertical trackbar (slider) with tick marks
            HWND slider = CreateWindow(TRACKBAR_CLASS, nullptr,
                WS_CHILD | WS_VISIBLE | TBS_VERT | TBS_AUTOTICKS | TBS_BOTH,
                x, startY + rgbLabelH, sliderW, sliderH,
                hwnd, (HMENU)(INT_PTR)(ID_GRAYSCALE_SLIDER_BASE + i), nullptr, nullptr);

            // Range: ±2500 (representing ±25.00% with 0.01 precision)
            int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;
            SendMessage(slider, TBM_SETRANGE, TRUE, MAKELONG(-maxRange, maxRange));
            SendMessage(slider, TBM_SETTICFREQ, GRAYSCALE_SLIDER_SCALE * 5, 0);  // Tick every 5%

            // Calculate current deviation from target
            // HDR: points store fraction of pqPeak (0-1), targetVal = t
            // SDR: points store actual output values, targetVal = inputNorm (sqrt distribution)
            float targetVal = data->isHDR ? t : inputNorm;
            float currentVal = data->points[i];
            // HDR: proportional deviation, SDR: additive deviation
            float deviationPct;
            if (data->isHDR && targetVal > 0.001f) {
                deviationPct = ((currentVal / targetVal) - 1.0f) * 100.0f;  // Proportional
            } else {
                deviationPct = (currentVal - targetVal) * 100.0f;  // Additive
            }
            int sliderVal = (int)(deviationPct * GRAYSCALE_SLIDER_SCALE + 0.5f);
            sliderVal = (std::max)(-maxRange, (std::min)(maxRange, sliderVal));

            // Trackbar is inverted (top = max), so negate for intuitive up = brighter
            SendMessage(slider, TBM_SETPOS, TRUE, -sliderVal);

            data->sliders.push_back(slider);

            // Bottom label: percentage of range
            wchar_t pctLabel[8];
            int pct = (int)(t * 100.0f + 0.5f);  // Use t (not inputNorm) for consistent 0-100%
            swprintf_s(pctLabel, L"%d%%", pct);
            CreateWindow(L"STATIC", pctLabel, WS_CHILD | WS_VISIBLE | SS_CENTER,
                x, startY + rgbLabelH + sliderH + 2, sliderW, pctLabelH, hwnd, nullptr, nullptr, nullptr);

            // Edit box for manual input (deviation value with 2 decimal places)
            wchar_t editText[16];
            swprintf_s(editText, L"%.2f", deviationPct);
            HWND edit = CreateWindowEx(WS_EX_CLIENTEDGE, L"EDIT", editText,
                WS_CHILD | WS_VISIBLE | ES_CENTER,  // No ES_NUMBER - need decimals and minus
                x, startY + rgbLabelH + sliderH + pctLabelH + 4, sliderW, editH,
                hwnd, (HMENU)(INT_PTR)(ID_GRAYSCALE_EDIT_BASE + i), nullptr, nullptr);
            SetNumericEdit(edit, 2);  // 2 decimal places for grayscale deviation
            data->editBoxes.push_back(edit);
        }

        // Calculate dialog width based on point count
        int dialogContentW = startX * 2 + data->pointCount * (sliderW + pad) + 40;  // Extra for +/- labels
        int btnY = startY + rgbLabelH + sliderH + pctLabelH + editH + 15;

        // OK and Cancel buttons (owner-drawn for rounded corners)
        CreateWindow(L"BUTTON", L"OK", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            dialogContentW / 2 - 90, btnY, 80, 28, hwnd, (HMENU)ID_GRAYSCALE_OK, nullptr, nullptr);
        CreateWindow(L"BUTTON", L"Cancel", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            dialogContentW / 2 + 10, btnY, 80, 28, hwnd, (HMENU)ID_GRAYSCALE_CANCEL, nullptr, nullptr);

        // +/- labels at top and bottom of slider area (right side)
        int sliderTop = startY + rgbLabelH;
        wchar_t plusLabel[8], minusLabel[8];
        swprintf_s(plusLabel, L"+%d", GRAYSCALE_RANGE);
        swprintf_s(minusLabel, L"-%d", GRAYSCALE_RANGE);
        CreateWindow(L"STATIC", plusLabel, WS_CHILD | WS_VISIBLE | SS_CENTER,
            dialogContentW - 38, sliderTop + 2, 30, 16, hwnd, nullptr, nullptr, nullptr);
        CreateWindow(L"STATIC", minusLabel, WS_CHILD | WS_VISIBLE | SS_CENTER,
            dialogContentW - 38, sliderTop + sliderH - 18, 30, 16, hwnd, nullptr, nullptr, nullptr);

        // Set font for all controls (create once, reuse, cleanup in WM_DESTROY)
        if (!g_grayscaleFont) {
            g_grayscaleFont = CreateFont(14, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        }
        EnumChildWindows(hwnd, [](HWND hwndChild, LPARAM lParam) -> BOOL {
            SendMessage(hwndChild, WM_SETFONT, lParam, TRUE);
            return TRUE;
        }, (LPARAM)g_grayscaleFont);

        return 0;
    }

    case WM_VSCROLL: {
        // Slider moved - update corresponding edit box and apply live
        HWND sliderHwnd = (HWND)lParam;
        auto* data = g_grayscaleEditor;
        if (data) {
            for (int i = 0; i < data->pointCount; i++) {
                if (data->sliders[i] == sliderHwnd) {
                    UpdateEditFromSlider(i);
                    // Update the points array immediately for live preview
                    int pos = (int)SendMessage(sliderHwnd, TBM_GETPOS, 0, 0);
                    float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;  // Negate, convert to %
                    float t = (float)i / (float)(data->pointCount - 1);
                    // HDR uses linear PQ space, SDR uses sqrt distribution
                    float targetVal = data->isHDR ? t : (t * t);
                    // HDR: proportional deviation, SDR: additive deviation
                    float newVal;
                    if (data->isHDR && targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);  // Proportional
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);  // Additive
                    }
                    data->points[i] = (std::max)(0.0f, (std::min)(1.0f, newVal));
                    // Apply live update
                    if (g_gui.currentMonitor >= 0) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, data->isHDR);
                    }
                    break;
                }
            }
        }
        return 0;
    }

    case WM_COMMAND: {
        WORD code = HIWORD(wParam);
        WORD id = LOWORD(wParam);

        // Check if it's an edit box losing focus
        if (code == EN_KILLFOCUS) {
            auto* data = g_grayscaleEditor;
            if (data) {
                int editIndex = id - ID_GRAYSCALE_EDIT_BASE;
                if (editIndex >= 0 && editIndex < data->pointCount) {
                    UpdateSliderFromEdit(editIndex);
                    // Also reformat the text to be within range
                    UpdateEditFromSlider(editIndex);
                    // Update point value and apply live
                    int pos = (int)SendMessage(data->sliders[editIndex], TBM_GETPOS, 0, 0);
                    float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;
                    float t = (float)editIndex / (float)(data->pointCount - 1);
                    // HDR uses linear PQ space, SDR uses sqrt distribution
                    float targetVal = data->isHDR ? t : (t * t);
                    // HDR: proportional deviation, SDR: additive deviation
                    float newVal;
                    if (data->isHDR && targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);
                    }
                    data->points[editIndex] = (std::max)(0.0f, (std::min)(1.0f, newVal));
                    if (g_gui.currentMonitor >= 0) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, data->isHDR);
                    }
                }
            }
            return 0;
        }

        switch (id) {
        case ID_GRAYSCALE_OK: {
            auto* data = g_grayscaleEditor;
            if (data) {
                // Read slider values and convert back to absolute values
                // SDR: sqrt distribution (input = (i/(N-1))^2)
                // HDR: linear PQ space (input = i/(N-1))
                for (int i = 0; i < data->pointCount; i++) {
                    int pos = (int)SendMessage(data->sliders[i], TBM_GETPOS, 0, 0);
                    float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;
                    float t = (float)i / (float)(data->pointCount - 1);
                    // HDR uses linear PQ space, SDR uses sqrt distribution
                    float targetVal = data->isHDR ? t : (t * t);
                    // HDR: proportional deviation, SDR: additive deviation
                    float newVal;
                    if (data->isHDR && targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);
                    }
                    data->points[i] = (std::max)(0.0f, (std::min)(1.0f, newVal));
                }
            }
            DestroyWindow(hwnd);
            return 0;
        }
        case ID_GRAYSCALE_CANCEL: {
            // Restore original values
            auto* data = g_grayscaleEditor;
            if (data && !data->originalPoints.empty()) {
                for (int i = 0; i < data->pointCount && i < (int)data->originalPoints.size(); i++) {
                    data->points[i] = data->originalPoints[i];
                }
                // Apply live update to restore original state
                if (g_gui.currentMonitor >= 0) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, data->isHDR);
                }
            }
            DestroyWindow(hwnd);
            return 0;
        }
        }
        break;
    }

    case WM_ERASEBKGND: {
        // Fill dialog with custom background color
        HDC hdc = (HDC)wParam;
        RECT rc;
        GetClientRect(hwnd, &rc);
        if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
        FillRect(hdc, &rc, g_tabBgBrush);
        return 1;
    }

    case WM_CTLCOLORSTATIC: {
        // Match static control backgrounds to dialog
        HDC hdc = (HDC)wParam;
        if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
        SetBkColor(hdc, TAB_BG_COLOR);
        return (LRESULT)g_tabBgBrush;
    }

    case WM_DRAWITEM: {
        LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
        if (pDIS->CtlType == ODT_BUTTON) {
            DrawRoundedButton(pDIS);
            return TRUE;
        }
        break;
    }

    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        g_grayscaleEditor = nullptr;
        return 0;
    }

    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void ShowGrayscaleEditor(HWND hwndParent, GrayscaleSettings& settings, bool isHDR) {
    // Ensure points array is initialized
    if (settings.points.empty() || (int)settings.points.size() != settings.pointCount) {
        settings.points.resize(settings.pointCount);
        // HDR uses PQ-space grayscale (evenly spaced), SDR uses sqrt distribution
        if (isHDR) {
            settings.initLinearPQ();
        } else {
            settings.initLinear();
        }
    }

    // Register window class if needed
    static bool registered = false;
    if (!registered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = GrayscaleEditorProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = L"DesktopLUT_GrayscaleEditor";
        RegisterClassEx(&wc);
        registered = true;
    }

    // Setup editor data
    GrayscaleEditorData data;
    data.pointCount = settings.pointCount;
    data.points = settings.points.data();
    data.isHDR = isHDR;
    data.peakNits = settings.peakNits;  // Pass peak for HDR label calculation
    // Save original values for Cancel restore
    data.originalPoints.assign(settings.points.begin(), settings.points.end());
    g_grayscaleEditor = &data;

    // Calculate window size (must match layout in GrayscaleEditorProc)
    int sliderW = 32;
    int sliderH = 150;
    int rgbLabelH = 16;   // Code value at top (8-bit SDR, 10-bit HDR)
    int pctLabelH = 16;   // Input percentage below slider
    int editH = 20;
    int pad = 2;
    int btnH = 28;
    int startY = 10;

    int contentW = 20 + settings.pointCount * (sliderW + pad) + 40;  // Extra for +/- labels
    int contentH = startY + rgbLabelH + sliderH + pctLabelH + editH + 15 + btnH + 15;

    // Adjust for window chrome
    RECT rc = { 0, 0, contentW, contentH };
    AdjustWindowRect(&rc, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, FALSE);
    int winW = rc.right - rc.left;
    int winH = rc.bottom - rc.top;

    // Center on parent
    RECT parentRect;
    GetWindowRect(hwndParent, &parentRect);
    int x = parentRect.left + (parentRect.right - parentRect.left - winW) / 2;
    int y = parentRect.top + (parentRect.bottom - parentRect.top - winH) / 2;

    // Create dialog window
    HWND hwndEditor = CreateWindowEx(
        WS_EX_DLGMODALFRAME,
        L"DesktopLUT_GrayscaleEditor",
        L"Grayscale Correction",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
        x, y, winW, winH,
        hwndParent, nullptr, GetModuleHandle(nullptr), nullptr);

    if (!hwndEditor) {
        g_grayscaleEditor = nullptr;
        return;
    }

    ShowWindow(hwndEditor, SW_SHOW);
    UpdateWindow(hwndEditor);

    // Modal message loop
    EnableWindow(hwndParent, FALSE);
    MSG msg;
    BOOL bRet;
    while ((bRet = GetMessage(&msg, nullptr, 0, 0)) != 0 && IsWindow(hwndEditor)) {
        if (bRet == -1) break;  // Error occurred
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);

    // Ensure global pointer is cleared (WM_DESTROY should do this, but be safe)
    g_grayscaleEditor = nullptr;
}

// ============================================================================
// Gamma Whitelist Dialog
// ============================================================================

static HWND g_whitelistEdit = nullptr;

LRESULT CALLBACK GammaWhitelistProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_CREATE:
        {
            int pad = 10;
            int btnW = 75;
            int btnH = 26;

            // Info label
            CreateWindow(L"STATIC",
                L"Process names to auto-disable gamma correction:",
                WS_CHILD | WS_VISIBLE,
                pad, pad, 360, 18, hwnd, nullptr, nullptr, nullptr);

            // Multi-line edit box with word wrap for many entries
            g_whitelistEdit = CreateWindowEx(WS_EX_CLIENTEDGE, L"EDIT", g_gammaWhitelistRaw.c_str(),
                WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
                pad, pad + 22, 360, 80, hwnd, (HMENU)ID_WHITELIST_EDIT, nullptr, nullptr);

            // OK and Cancel buttons (owner-draw for rounded style)
            RECT rc;
            GetClientRect(hwnd, &rc);
            int btnY = rc.bottom - btnH - pad;
            CreateWindow(L"BUTTON", L"OK",
                WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                rc.right - 2*btnW - 2*pad, btnY, btnW, btnH, hwnd, (HMENU)ID_WHITELIST_OK, nullptr, nullptr);
            CreateWindow(L"BUTTON", L"Cancel",
                WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                rc.right - btnW - pad, btnY, btnW, btnH, hwnd, (HMENU)ID_WHITELIST_CANCEL, nullptr, nullptr);

            // Apply main font to all controls
            if (g_mainFont) {
                EnumChildWindows(hwnd, [](HWND hwndChild, LPARAM lParam) -> BOOL {
                    SendMessage(hwndChild, WM_SETFONT, lParam, TRUE);
                    return TRUE;
                }, (LPARAM)g_mainFont);
            }
        }
        return 0;

    case WM_DRAWITEM:
        {
            LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
            if (pDIS->CtlType == ODT_BUTTON) {
                DrawRoundedButton(pDIS);
                return TRUE;
            }
        }
        break;

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_WHITELIST_OK:
            {
                // Get text from edit box
                wchar_t buf[1024] = {};
                GetWindowText(g_whitelistEdit, buf, 1024);
                g_gammaWhitelistRaw = buf;
                ParseGammaWhitelist();
                SaveSettings();
                DestroyWindow(hwnd);
            }
            return 0;
        case ID_WHITELIST_CANCEL:
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        g_whitelistEdit = nullptr;
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void ShowGammaWhitelistDialog(HWND hwndParent) {
    // Register window class if needed
    static bool registered = false;
    if (!registered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = GammaWhitelistProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = L"DesktopLUT_GammaWhitelist";
        RegisterClassEx(&wc);
        registered = true;
    }

    // Calculate window size
    int contentW = 380;
    int contentH = 160;

    RECT rc = { 0, 0, contentW, contentH };
    AdjustWindowRect(&rc, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, FALSE);
    int winW = rc.right - rc.left;
    int winH = rc.bottom - rc.top;

    // Center on parent
    RECT parentRect;
    GetWindowRect(hwndParent, &parentRect);
    int x = parentRect.left + (parentRect.right - parentRect.left - winW) / 2;
    int y = parentRect.top + (parentRect.bottom - parentRect.top - winH) / 2;

    // Create dialog window
    HWND hwndDialog = CreateWindowEx(
        WS_EX_DLGMODALFRAME,
        L"DesktopLUT_GammaWhitelist",
        L"Gamma Whitelist",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
        x, y, winW, winH,
        hwndParent, nullptr, GetModuleHandle(nullptr), nullptr);

    if (!hwndDialog) {
        return;
    }

    ShowWindow(hwndDialog, SW_SHOW);
    UpdateWindow(hwndDialog);

    // Modal message loop
    EnableWindow(hwndParent, FALSE);
    MSG msg;
    BOOL bRet;
    while ((bRet = GetMessage(&msg, nullptr, 0, 0)) != 0 && IsWindow(hwndDialog)) {
        if (bRet == -1) break;  // Error occurred
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);
}

// ============================================================================
// VRR Whitelist Dialog
// ============================================================================

static HWND g_vrrWhitelistEdit = nullptr;

LRESULT CALLBACK VrrWhitelistProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_CREATE:
        {
            int pad = 10;
            int btnW = 75;
            int btnH = 26;

            // Info label
            CreateWindow(L"STATIC",
                L"Process names to disable overlay for:",
                WS_CHILD | WS_VISIBLE,
                pad, pad, 360, 18, hwnd, nullptr, nullptr, nullptr);

            // Multi-line edit box with word wrap for many entries
            g_vrrWhitelistEdit = CreateWindowEx(WS_EX_CLIENTEDGE, L"EDIT", g_vrrWhitelistRaw.c_str(),
                WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
                pad, pad + 22, 360, 80, hwnd, nullptr, nullptr, nullptr);

            // OK and Cancel buttons (owner-draw for rounded style)
            RECT rc;
            GetClientRect(hwnd, &rc);
            int btnY = rc.bottom - btnH - pad;
            CreateWindow(L"BUTTON", L"OK",
                WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                rc.right - 2*btnW - 2*pad, btnY, btnW, btnH, hwnd, (HMENU)ID_WHITELIST_OK, nullptr, nullptr);
            CreateWindow(L"BUTTON", L"Cancel",
                WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                rc.right - btnW - pad, btnY, btnW, btnH, hwnd, (HMENU)ID_WHITELIST_CANCEL, nullptr, nullptr);

            // Apply main font to all controls
            if (g_mainFont) {
                EnumChildWindows(hwnd, [](HWND hwndChild, LPARAM lParam) -> BOOL {
                    SendMessage(hwndChild, WM_SETFONT, lParam, TRUE);
                    return TRUE;
                }, (LPARAM)g_mainFont);
            }
        }
        return 0;

    case WM_DRAWITEM:
        {
            LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
            if (pDIS->CtlType == ODT_BUTTON) {
                DrawRoundedButton(pDIS);
                return TRUE;
            }
        }
        break;

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_WHITELIST_OK:
            {
                // Get text from edit box
                wchar_t buf[1024] = {};
                GetWindowText(g_vrrWhitelistEdit, buf, 1024);
                g_vrrWhitelistRaw = buf;
                ParseVrrWhitelist();
                SaveSettings();
                DestroyWindow(hwnd);
            }
            return 0;
        case ID_WHITELIST_CANCEL:
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        g_vrrWhitelistEdit = nullptr;
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void ShowVrrWhitelistDialog(HWND hwndParent) {
    // Register window class if needed
    static bool registered = false;
    if (!registered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = VrrWhitelistProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = L"DesktopLUT_VrrWhitelist";
        RegisterClassEx(&wc);
        registered = true;
    }

    // Calculate window size
    int contentW = 380;
    int contentH = 160;

    RECT rc = { 0, 0, contentW, contentH };
    AdjustWindowRect(&rc, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, FALSE);
    int winW = rc.right - rc.left;
    int winH = rc.bottom - rc.top;

    // Center on parent
    RECT parentRect;
    GetWindowRect(hwndParent, &parentRect);
    int x = parentRect.left + (parentRect.right - parentRect.left - winW) / 2;
    int y = parentRect.top + (parentRect.bottom - parentRect.top - winH) / 2;

    // Create dialog window
    HWND hwndDialog = CreateWindowEx(
        WS_EX_DLGMODALFRAME,
        L"DesktopLUT_VrrWhitelist",
        L"Passthrough Whitelist",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
        x, y, winW, winH,
        hwndParent, nullptr, GetModuleHandle(nullptr), nullptr);

    if (!hwndDialog) {
        return;
    }

    ShowWindow(hwndDialog, SW_SHOW);
    UpdateWindow(hwndDialog);

    // Modal message loop
    EnableWindow(hwndParent, FALSE);
    MSG msg;
    BOOL bRet;
    while ((bRet = GetMessage(&msg, nullptr, 0, 0)) != 0 && IsWindow(hwndDialog)) {
        if (bRet == -1) break;  // Error occurred
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);
}

// Monitor enumeration for GUI
static BOOL CALLBACK GUIMonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam) {
    auto* monitors = reinterpret_cast<std::vector<HMONITOR>*>(lParam);
    monitors->push_back(hMonitor);
    return TRUE;
}

// Forward declaration for DrawRoundedButton
static void DrawRoundedButton(LPDRAWITEMSTRUCT pDIS);

// Scroll panel window procedure
static LRESULT CALLBACK ScrollPanelProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    // Get tab index from window user data
    int tabIndex = (int)GetWindowLongPtr(hwnd, GWLP_USERDATA);

    switch (msg) {
    case WM_ERASEBKGND: {
        if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
        HDC hdc = (HDC)wParam;
        RECT rc;
        GetClientRect(hwnd, &rc);
        FillRect(hdc, &rc, g_tabBgBrush);
        return 1;
    }

    case WM_CTLCOLORSTATIC: {
        // Set background color for static controls inside the scroll panel
        HDC hdc = (HDC)wParam;
        if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
        SetBkColor(hdc, TAB_BG_COLOR);
        return (LRESULT)g_tabBgBrush;
    }

    case WM_DRAWITEM: {
        LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
        if (pDIS->CtlType != ODT_BUTTON) break;
        DrawRoundedButton(pDIS);
        return TRUE;
    }

    case WM_VSCROLL: {
        // Get current scroll info
        SCROLLINFO si = {};
        si.cbSize = sizeof(si);
        si.fMask = SIF_ALL;
        GetScrollInfo(hwnd, SB_VERT, &si);

        int oldPos = si.nPos;
        int newPos = oldPos;

        // Calculate new position based on scroll action
        switch (LOWORD(wParam)) {
        case SB_LINEUP:      newPos -= 20; break;
        case SB_LINEDOWN:    newPos += 20; break;
        case SB_PAGEUP:      newPos -= si.nPage; break;
        case SB_PAGEDOWN:    newPos += si.nPage; break;
        case SB_THUMBTRACK:  newPos = si.nTrackPos; break;
        case SB_THUMBPOSITION: newPos = si.nTrackPos; break;
        case SB_TOP:         newPos = si.nMin; break;
        case SB_BOTTOM:      newPos = si.nMax; break;
        }

        // Clamp to valid range
        int maxPos = max(0, si.nMax - (int)si.nPage);
        newPos = max(0, min(newPos, maxPos));

        if (newPos != oldPos) {
            // Update scroll position
            si.fMask = SIF_POS;
            si.nPos = newPos;
            SetScrollInfo(hwnd, SB_VERT, &si, TRUE);
            g_gui.scrollPos[tabIndex] = newPos;

            // Reposition all child controls
            std::vector<HWND>* controls = nullptr;
            std::vector<int>* originalY = nullptr;
            switch (tabIndex) {
            case 0: controls = &g_gui.tab0Controls; originalY = &g_gui.tab0OriginalY; break;
            case 1: controls = &g_gui.tab1Controls; originalY = &g_gui.tab1OriginalY; break;
            case 2: controls = &g_gui.tab2Controls; originalY = &g_gui.tab2OriginalY; break;
            }

            if (controls && originalY && controls->size() == originalY->size()) {
                // Hide panel before repositioning to force complete repaint
                // (groupboxes don't fill their background, causing artifacts with WS_CLIPCHILDREN)
                ShowWindow(hwnd, SW_HIDE);

                // Reposition all controls
                for (size_t i = 0; i < controls->size(); i++) {
                    RECT rc;
                    GetWindowRect((*controls)[i], &rc);
                    POINT pt = { rc.left, rc.top };
                    ScreenToClient(hwnd, &pt);
                    int width = rc.right - rc.left;
                    int height = rc.bottom - rc.top;
                    int newY = (*originalY)[i] - newPos;
                    SetWindowPos((*controls)[i], nullptr, pt.x, newY, width, height, SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW);
                }

                // Show panel - forces complete repaint with correct background
                ShowWindow(hwnd, SW_SHOW);
            }
        }
        return 0;
    }

    case WM_MOUSEWHEEL: {
        // Handle mouse wheel scrolling
        int delta = GET_WHEEL_DELTA_WPARAM(wParam);
        int lines = delta / WHEEL_DELTA * 3;  // 3 lines per wheel click
        SendMessage(hwnd, WM_VSCROLL, lines > 0 ? SB_LINEUP : SB_LINEDOWN, 0);
        for (int i = 1; i < abs(lines); i++) {
            SendMessage(hwnd, WM_VSCROLL, lines > 0 ? SB_LINEUP : SB_LINEDOWN, 0);
        }
        return 0;
    }

    case WM_COMMAND:
        // Forward command messages to main window
        return SendMessage(GetParent(GetParent(hwnd)), msg, wParam, lParam);
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

// Tab control subclass to paint custom background
static WNDPROC g_origTabProc = nullptr;

static LRESULT CALLBACK TabSubclassProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_ERASEBKGND: {
        // Fill the tab content area with our custom color
        if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);

        HDC hdc = (HDC)wParam;
        RECT rc;
        GetClientRect(hwnd, &rc);

        // Get content area (excludes tab headers)
        RECT contentRect = rc;
        TabCtrl_AdjustRect(hwnd, FALSE, &contentRect);

        // Fill content area with custom color
        FillRect(hdc, &contentRect, g_tabBgBrush);

        // Fill header area with button face color
        RECT headerRect = rc;
        headerRect.bottom = contentRect.top;
        FillRect(hdc, &headerRect, GetSysColorBrush(COLOR_BTNFACE));

        return 1; // We handled it
    }
    }
    return CallWindowProc(g_origTabProc, hwnd, msg, wParam, lParam);
}

LRESULT CALLBACK GUIWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_CTLCOLORSTATIC: {
        // Set background color for static controls inside the tab
        HWND hCtrl = (HWND)lParam;
        HDC hdc = (HDC)wParam;

        // Check if this control is inside the tab area (tab content controls)
        bool isTabControl = false;
        for (HWND h : g_gui.tab0Controls) { if (h == hCtrl) { isTabControl = true; break; } }
        if (!isTabControl) for (HWND h : g_gui.tab1Controls) { if (h == hCtrl) { isTabControl = true; break; } }
        if (!isTabControl) for (HWND h : g_gui.tab2Controls) { if (h == hCtrl) { isTabControl = true; break; } }

        if (isTabControl) {
            if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
            SetBkColor(hdc, TAB_BG_COLOR);
            return (LRESULT)g_tabBgBrush;
        }

        // Default: use button face color for other static controls
        SetBkColor(hdc, GetSysColor(COLOR_BTNFACE));
        return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
    }

    case WM_DRAWITEM: {
        LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
        if (pDIS->CtlType != ODT_BUTTON) break;
        DrawRoundedButton(pDIS);
        return TRUE;
    }

    case WM_CREATE: {
        // Get client area dimensions for layout
        RECT clientRect;
        GetClientRect(hwnd, &clientRect);
        int clientW = clientRect.right;
        int clientH = clientRect.bottom;

        // Layout constants
        int margin = 10;          // Window edge margin
        int labelW = 70;
        int btnW = 60;
        int h = 24;
        int pad = 6;              // Padding between elements
        int listH = 52;           // Monitor list height (~2.5 items)
        int statusH = h;
        int btnH = 28;

        // Calculate content width (fill to right margin)
        int contentW = clientW - margin * 2;
        int editW = contentW - labelW - pad - btnW * 2 - pad * 2;

        // Calculate vertical positions from bottom up
        // Layout: [tab] - pad - [buttons] - pad - [separator] - pad - [status] - bottomMargin
        int bottomMargin = -3;  // Negative to reduce visual gap (text has internal padding)
        int separatorH = 2;
        int statusY = clientH - bottomMargin - statusH;
        int separatorY = statusY - pad - separatorH;
        int btnY = separatorY - pad - btnH;
        int tabBottom = btnY - pad;

        // Top section
        int y = margin;

        // Monitor label and listbox
        CreateWindow(L"STATIC", L"Monitor:", WS_CHILD | WS_VISIBLE,
            margin, y + 2, labelW, h, hwnd, nullptr, nullptr, nullptr);
        g_gui.hwndMonitorList = CreateWindow(L"LISTBOX", nullptr,
            WS_CHILD | WS_VISIBLE | WS_BORDER | WS_VSCROLL | LBS_NOTIFY | LBS_NOINTEGRALHEIGHT,
            margin + labelW + pad, y, contentW - labelW - pad, listH, hwnd,
            (HMENU)ID_MONITOR_LIST, nullptr, nullptr);
        y += listH + pad;

        // Tab control (fill remaining space)
        int tabH = tabBottom - y - 28;  // Subtract tab header height
        g_gui.hwndTab = CreateWindow(WC_TABCONTROL, nullptr,
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            margin, y, contentW, tabH + 28,
            hwnd, (HMENU)ID_TAB_CONTROL, nullptr, nullptr);

        // Subclass tab control for custom background color
        g_origTabProc = (WNDPROC)SetWindowLongPtr(g_gui.hwndTab, GWLP_WNDPROC, (LONG_PTR)TabSubclassProc);

        // Add tabs
        TCITEM tie = { TCIF_TEXT };
        tie.pszText = (LPWSTR)L"LUT Options";
        TabCtrl_InsertItem(g_gui.hwndTab, 0, &tie);
        tie.pszText = (LPWSTR)L"SDR Options";
        TabCtrl_InsertItem(g_gui.hwndTab, 1, &tie);
        tie.pszText = (LPWSTR)L"HDR Options";
        TabCtrl_InsertItem(g_gui.hwndTab, 2, &tie);
        tie.pszText = (LPWSTR)L"Settings";
        TabCtrl_InsertItem(g_gui.hwndTab, 3, &tie);

        // Get tab content area rect (excludes tab headers)
        RECT tabContentRect;
        GetClientRect(g_gui.hwndTab, &tabContentRect);
        TabCtrl_AdjustRect(g_gui.hwndTab, FALSE, &tabContentRect);
        int panelX = tabContentRect.left + 2;
        int panelY = tabContentRect.top + 2;
        int panelW = tabContentRect.right - tabContentRect.left - 4;
        int panelH = tabContentRect.bottom - tabContentRect.top - 4;
        g_gui.panelHeight = panelH;  // Store for scroll calculations

        // Create scroll panels for each tab (with scrollbar for overflow)
        for (int i = 0; i < 4; i++) {
            DWORD style = WS_CHILD | WS_CLIPSIBLINGS | WS_CLIPCHILDREN | WS_VSCROLL;
            g_gui.hwndScrollPanel[i] = CreateWindowEx(
                0, L"DesktopLUT_ScrollPanel", nullptr, style,
                panelX, panelY, panelW, panelH,
                g_gui.hwndTab, nullptr, nullptr, nullptr);
            SetWindowLongPtr(g_gui.hwndScrollPanel[i], GWLP_USERDATA, i);
            if (i == 0) ShowWindow(g_gui.hwndScrollPanel[i], SW_SHOW);
        }

        // Tab content layout (controls are now relative to scroll panel, not main window)
        int innerY = 8;  // Starting Y inside scroll panel
        int innerX = 8;  // Starting X inside scroll panel
        int groupW = panelW - 16;  // Width for groupboxes (with padding inside panel)

        // === TAB 0: LUT Options ===
        HWND ctrl;
        HWND panel0 = g_gui.hwndScrollPanel[0];

        // LUT path edit width calculation
        int pathEditW = groupW - labelW - 3 * pad - 2 * btnW;

        // SDR LUT
        ctrl = CreateWindow(L"STATIC", L"SDR LUT:", WS_CHILD | WS_VISIBLE,
            innerX, innerY + 2, labelW, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndSdrPath = CreateWindow(L"EDIT", L"",
            WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL | ES_READONLY,
            innerX + labelW + pad, innerY, pathEditW, h, panel0, (HMENU)ID_SDR_PATH, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndSdrPath);
        ctrl = CreateWindow(L"BUTTON", L"Browse", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad, innerY, btnW, h, panel0, (HMENU)ID_SDR_BROWSE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        ctrl = CreateWindow(L"BUTTON", L"Clear", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad + btnW + pad, innerY, btnW, h, panel0, (HMENU)ID_SDR_CLEAR, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        innerY += h + pad;

        // HDR LUT
        ctrl = CreateWindow(L"STATIC", L"HDR LUT:", WS_CHILD | WS_VISIBLE,
            innerX, innerY + 2, labelW, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndHdrPath = CreateWindow(L"EDIT", L"",
            WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL | ES_READONLY,
            innerX + labelW + pad, innerY, pathEditW, h, panel0, (HMENU)ID_HDR_PATH, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrPath);
        ctrl = CreateWindow(L"BUTTON", L"Browse", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad, innerY, btnW, h, panel0, (HMENU)ID_HDR_BROWSE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        ctrl = CreateWindow(L"BUTTON", L"Clear", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad + btnW + pad, innerY, btnW, h, panel0, (HMENU)ID_HDR_CLEAR, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        innerY += h + pad;

        // Tetrahedral interpolation checkbox
        g_gui.hwndTetrahedralCheck = CreateWindow(L"BUTTON", L"Tetrahedral LUT interpolation",
            WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX,
            innerX + labelW + pad, innerY, 250, h, panel0, (HMENU)ID_TETRAHEDRAL_CHECK, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndTetrahedralCheck);
        SendMessage(g_gui.hwndTetrahedralCheck, BM_SETCHECK, g_tetrahedralInterp ? BST_CHECKED : BST_UNCHECKED, 0);
        innerY += h + pad;

        // Gamma checkbox and whitelist button
        g_gui.hwndGammaCheck = CreateWindow(L"BUTTON", L"Desktop gamma (2.2) - HDR only",
            WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX,
            innerX + labelW + pad, innerY, 200, h, panel0, (HMENU)ID_GAMMA_CHECK, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndGammaCheck);
        SendMessage(g_gui.hwndGammaCheck, BM_SETCHECK, g_userDesktopGammaMode.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.hwndGammaWhitelistBtn = CreateWindow(L"BUTTON", L"Whitelist...",
            WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + 205, innerY, 70, h, panel0, (HMENU)ID_GAMMA_WHITELIST_BTN, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndGammaWhitelistBtn);
        g_gui.contentHeight[0] = innerY + h + 8;  // Track content height

        // === TAB 1: SDR Color Correction (initially hidden) ===
        innerY = 8;  // Reset for scroll panel
        HWND panel1 = g_gui.hwndScrollPanel[1];

        // Primaries group
        ctrl = CreateWindow(L"BUTTON", L"Display Primaries", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 105, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);

        g_gui.hwndSdrPrimariesEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel1, (HMENU)ID_SDR_PRIMARIES_ENABLE, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesEnable);

        ctrl = CreateWindow(L"STATIC", L"Preset:", WS_CHILD,
            innerX + 80, innerY + 20, 45, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);

        g_gui.hwndSdrPrimariesPreset = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 130, innerY + 18, 150, 150, panel1, (HMENU)ID_SDR_PRIMARIES_PRESET, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesPreset);
        for (int i = 0; i < g_numPresetPrimaries; i++) {
            SendMessage(g_gui.hwndSdrPrimariesPreset, CB_ADDSTRING, 0, (LPARAM)g_presetPrimaries[i].name);
        }
        SendMessage(g_gui.hwndSdrPrimariesPreset, CB_SETCURSEL, 0, 0);

        ctrl = CreateWindow(L"BUTTON", L"Detect", WS_CHILD | BS_OWNERDRAW,
            innerX + 285, innerY + 18, 50, h, panel1, (HMENU)ID_SDR_PRIMARIES_DETECT, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);

        // Chromaticity coordinate inputs (2 rows)
        int chromY = innerY + 45;
        int chromX = innerX + 10;
        int chromW = 50;
        int chromLabelW = 25;

        ctrl = CreateWindow(L"STATIC", L"R:", WS_CHILD, chromX, chromY + 2, chromLabelW, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        g_gui.hwndSdrPrimariesRx = CreateWindow(L"EDIT", L"0.64", WS_CHILD | WS_BORDER,
            chromX + chromLabelW, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_RX, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesRx);
        g_gui.hwndSdrPrimariesRy = CreateWindow(L"EDIT", L"0.33", WS_CHILD | WS_BORDER,
            chromX + chromLabelW + chromW + 5, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_RY, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesRy);

        ctrl = CreateWindow(L"STATIC", L"G:", WS_CHILD, chromX + 140, chromY + 2, chromLabelW, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        g_gui.hwndSdrPrimariesGx = CreateWindow(L"EDIT", L"0.30", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_GX, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesGx);
        g_gui.hwndSdrPrimariesGy = CreateWindow(L"EDIT", L"0.60", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW + chromW + 5, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_GY, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesGy);

        chromY += h + 4;
        ctrl = CreateWindow(L"STATIC", L"B:", WS_CHILD, chromX, chromY + 2, chromLabelW, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        g_gui.hwndSdrPrimariesBx = CreateWindow(L"EDIT", L"0.15", WS_CHILD | WS_BORDER,
            chromX + chromLabelW, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_BX, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesBx);
        g_gui.hwndSdrPrimariesBy = CreateWindow(L"EDIT", L"0.06", WS_CHILD | WS_BORDER,
            chromX + chromLabelW + chromW + 5, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_BY, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesBy);

        ctrl = CreateWindow(L"STATIC", L"W:", WS_CHILD, chromX + 140, chromY + 2, chromLabelW, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        g_gui.hwndSdrPrimariesWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_WX, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesWx);
        g_gui.hwndSdrPrimariesWy = CreateWindow(L"EDIT", L"0.329", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW + chromW + 5, chromY, chromW, h, panel1, (HMENU)ID_SDR_PRIMARIES_WY, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPrimariesWy);

        // Apply numeric input validation (4 decimal places for coordinates)
        SetNumericEdit(g_gui.hwndSdrPrimariesRx, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesRy, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesGx, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesGy, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesBx, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesBy, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesWx, 4);
        SetNumericEdit(g_gui.hwndSdrPrimariesWy, 4);

        // Disable primaries edit boxes by default (only enabled for Custom preset)
        EnableWindow(g_gui.hwndSdrPrimariesRx, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesRy, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesGx, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesGy, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesBx, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesBy, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesWx, FALSE);
        EnableWindow(g_gui.hwndSdrPrimariesWy, FALSE);

        // Grayscale group (layout matches HDR Grayscale group)
        innerY += 110;
        ctrl = CreateWindow(L"BUTTON", L"Grayscale", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);

        g_gui.hwndSdrGrayscaleEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel1, (HMENU)ID_SDR_GRAYSCALE_ENABLE, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscaleEnable);

        ctrl = CreateWindow(L"STATIC", L"Points:", WS_CHILD, innerX + 80, innerY + 20, 45, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);

        g_gui.hwndSdrGrayscale10 = CreateWindow(L"BUTTON", L"10", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
            innerX + 130, innerY + 18, 40, h, panel1, (HMENU)ID_SDR_GRAYSCALE_10, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscale10);
        g_gui.hwndSdrGrayscale20 = CreateWindow(L"BUTTON", L"20", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 175, innerY + 18, 40, h, panel1, (HMENU)ID_SDR_GRAYSCALE_20, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscale20);
        g_gui.hwndSdrGrayscale32 = CreateWindow(L"BUTTON", L"32", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 220, innerY + 18, 40, h, panel1, (HMENU)ID_SDR_GRAYSCALE_32, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscale32);
        SendMessage(g_gui.hwndSdrGrayscale20, BM_SETCHECK, BST_CHECKED, 0);  // Default to 20

        g_gui.hwndSdrGrayscaleEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel1, (HMENU)ID_SDR_GRAYSCALE_EDIT, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscaleEdit);
        g_gui.hwndSdrGrayscaleReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel1, (HMENU)ID_SDR_GRAYSCALE_RESET, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscaleReset);

        // 2.4 Gamma checkbox (independent, below grayscale group)
        g_gui.hwndSdrGrayscale24 = CreateWindow(L"BUTTON", L"2.4 Gamma",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 80, 80, h, panel1, (HMENU)ID_SDR_GRAYSCALE_24, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrGrayscale24);

        g_gui.contentHeight[1] = innerY + 105 + 8;  // Track content height

        // === TAB 2: HDR Options (initially hidden) ===
        innerY = 8;  // Reset for scroll panel
        HWND panel2 = g_gui.hwndScrollPanel[2];

        // HDR Display Primaries group
        ctrl = CreateWindow(L"BUTTON", L"Display Primaries", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 105, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrPrimariesEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_HDR_PRIMARIES_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesEnable);

        ctrl = CreateWindow(L"STATIC", L"Preset:", WS_CHILD,
            innerX + 80, innerY + 20, 45, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrPrimariesPreset = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 130, innerY + 18, 150, 150, panel2, (HMENU)ID_HDR_PRIMARIES_PRESET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesPreset);
        for (int i = 0; i < g_numPresetPrimaries; i++) {
            SendMessage(g_gui.hwndHdrPrimariesPreset, CB_ADDSTRING, 0, (LPARAM)g_presetPrimaries[i].name);
        }
        SendMessage(g_gui.hwndHdrPrimariesPreset, CB_SETCURSEL, 3, 0);  // Default to Rec.2020 for HDR

        ctrl = CreateWindow(L"BUTTON", L"Detect", WS_CHILD | BS_OWNERDRAW,
            innerX + 285, innerY + 18, 50, h, panel2, (HMENU)ID_HDR_PRIMARIES_DETECT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        // HDR Chromaticity coordinate inputs (2 rows)
        chromY = innerY + 45;
        chromX = innerX + 10;

        // HDR defaults to Rec.2020 primaries (target for HDR content)
        ctrl = CreateWindow(L"STATIC", L"R:", WS_CHILD, chromX, chromY + 2, chromLabelW, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrPrimariesRx = CreateWindow(L"EDIT", L"0.708", WS_CHILD | WS_BORDER,
            chromX + chromLabelW, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_RX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesRx);
        g_gui.hwndHdrPrimariesRy = CreateWindow(L"EDIT", L"0.292", WS_CHILD | WS_BORDER,
            chromX + chromLabelW + chromW + 5, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_RY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesRy);

        ctrl = CreateWindow(L"STATIC", L"G:", WS_CHILD, chromX + 140, chromY + 2, chromLabelW, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrPrimariesGx = CreateWindow(L"EDIT", L"0.170", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_GX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesGx);
        g_gui.hwndHdrPrimariesGy = CreateWindow(L"EDIT", L"0.797", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW + chromW + 5, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_GY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesGy);

        chromY += h + 4;
        ctrl = CreateWindow(L"STATIC", L"B:", WS_CHILD, chromX, chromY + 2, chromLabelW, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrPrimariesBx = CreateWindow(L"EDIT", L"0.131", WS_CHILD | WS_BORDER,
            chromX + chromLabelW, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_BX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesBx);
        g_gui.hwndHdrPrimariesBy = CreateWindow(L"EDIT", L"0.046", WS_CHILD | WS_BORDER,
            chromX + chromLabelW + chromW + 5, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_BY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesBy);

        ctrl = CreateWindow(L"STATIC", L"W:", WS_CHILD, chromX + 140, chromY + 2, chromLabelW, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrPrimariesWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_WX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesWx);
        g_gui.hwndHdrPrimariesWy = CreateWindow(L"EDIT", L"0.329", WS_CHILD | WS_BORDER,
            chromX + 140 + chromLabelW + chromW + 5, chromY, chromW, h, panel2, (HMENU)ID_HDR_PRIMARIES_WY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesWy);

        // Apply numeric input validation (4 decimal places for coordinates)
        SetNumericEdit(g_gui.hwndHdrPrimariesRx, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesRy, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesGx, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesGy, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesBx, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesBy, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesWx, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesWy, 4);

        // Disable HDR primaries edit boxes by default (only enabled for Custom preset)
        EnableWindow(g_gui.hwndHdrPrimariesRx, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesRy, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesGx, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesGy, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesBx, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesBy, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesWx, FALSE);
        EnableWindow(g_gui.hwndHdrPrimariesWy, FALSE);

        // HDR Grayscale group
        innerY += 110;
        ctrl = CreateWindow(L"BUTTON", L"Grayscale", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrGrayscaleEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_HDR_GRAYSCALE_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscaleEnable);

        ctrl = CreateWindow(L"STATIC", L"Points:", WS_CHILD, innerX + 80, innerY + 20, 45, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrGrayscale10 = CreateWindow(L"BUTTON", L"10", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
            innerX + 130, innerY + 18, 40, h, panel2, (HMENU)ID_HDR_GRAYSCALE_10, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscale10);
        g_gui.hwndHdrGrayscale20 = CreateWindow(L"BUTTON", L"20", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 175, innerY + 18, 40, h, panel2, (HMENU)ID_HDR_GRAYSCALE_20, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscale20);
        g_gui.hwndHdrGrayscale32 = CreateWindow(L"BUTTON", L"32", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 220, innerY + 18, 40, h, panel2, (HMENU)ID_HDR_GRAYSCALE_32, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscale32);
        SendMessage(g_gui.hwndHdrGrayscale20, BM_SETCHECK, BST_CHECKED, 0);  // Default to 20

        g_gui.hwndHdrGrayscaleEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel2, (HMENU)ID_HDR_GRAYSCALE_EDIT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscaleEdit);
        g_gui.hwndHdrGrayscaleReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel2, (HMENU)ID_HDR_GRAYSCALE_RESET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscaleReset);
        ctrl = CreateWindow(L"STATIC", L"Peak:", WS_CHILD, innerX + 180, innerY + 47, 35, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrGrayscalePeak = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 215, innerY + 45, 45, h, panel2, (HMENU)ID_HDR_GRAYSCALE_PEAK, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscalePeak);

        // HDR Tonemapping group
        innerY += 80;
        ctrl = CreateWindow(L"BUTTON", L"Tonemapping", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrTonemapEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_HDR_TONEMAP_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrTonemapEnable);

        ctrl = CreateWindow(L"STATIC", L"Curve:", WS_CHILD, innerX + 80, innerY + 20, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrTonemapCurve = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 120, innerY + 18, 95, 120, panel2, (HMENU)ID_HDR_TONEMAP_CURVE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrTonemapCurve);
        // Order: BT2390, BT2446A, Reinhard, SoftClip, HardClip (matches g_tonemapDropdownOrder)
        SendMessage(g_gui.hwndHdrTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"BT.2390");
        SendMessage(g_gui.hwndHdrTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"BT.2446A");
        SendMessage(g_gui.hwndHdrTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"Reinhard");
        SendMessage(g_gui.hwndHdrTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"Soft Clip");
        SendMessage(g_gui.hwndHdrTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"Hard Clip");
        SendMessage(g_gui.hwndHdrTonemapCurve, CB_SETCURSEL, 0, 0);

        // Target and Source peak inputs
        int tonemapY = innerY + 45;
        ctrl = CreateWindow(L"STATIC", L"Target:", WS_CHILD, innerX + 10, tonemapY + 2, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrTonemapTarget = CreateWindow(L"EDIT", L"1000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 50, tonemapY, 45, h, panel2, (HMENU)ID_HDR_TONEMAP_TARGET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrTonemapTarget);

        ctrl = CreateWindow(L"STATIC", L"Source:", WS_CHILD, innerX + 100, tonemapY + 2, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrTonemapSource = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 140, tonemapY, 50, h, panel2, (HMENU)ID_HDR_TONEMAP_SOURCE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrTonemapSource);

        ctrl = CreateWindow(L"STATIC", L"nits", WS_CHILD, innerX + 192, tonemapY + 2, 25, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        // Dynamic peak detection checkbox (far right)
        g_gui.hwndHdrTonemapDynamic = CreateWindow(L"BUTTON", L"Dynamic",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 220, tonemapY, 70, h, panel2, (HMENU)ID_HDR_TONEMAP_DYNAMIC, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrTonemapDynamic);

        // MaxTML group (Windows HDR peak luminance override)
        innerY += 80;
        ctrl = CreateWindow(L"BUTTON", L"Display Peak Override (MaxTML)", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 55, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrMaxTmlEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 55, h, panel2, (HMENU)ID_HDR_MAXTML_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrMaxTmlEnable);

        g_gui.hwndHdrMaxTmlCombo = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 70, innerY + 18, 85, 150, panel2, (HMENU)ID_HDR_MAXTML_COMBO, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrMaxTmlCombo);
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"Custom");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"400 nits");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"600 nits");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"1000 nits");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"1400 nits");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"4000 nits");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"10000 nits");
        SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_SETCURSEL, 3, 0);  // Default to 1000

        g_gui.hwndHdrMaxTmlEdit = CreateWindow(L"EDIT", L"1000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 160, innerY + 18, 50, h, panel2, (HMENU)ID_HDR_MAXTML_EDIT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrMaxTmlEdit);

        ctrl = CreateWindow(L"STATIC", L"nits", WS_CHILD, innerX + 212, innerY + 20, 25, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrMaxTmlApply = CreateWindow(L"BUTTON", L"Apply",
            WS_CHILD | BS_OWNERDRAW, innerX + 245, innerY + 17, 45, h + 2, panel2, (HMENU)ID_HDR_MAXTML_APPLY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrMaxTmlApply);
        g_gui.contentHeight[2] = innerY + 55 + 8;  // Track content height (for future scroll support)

        // Apply Enter key handling to HDR numeric edit boxes (ES_NUMBER already filters input)
        SetNumericEdit(g_gui.hwndHdrGrayscalePeak, 0);
        SetNumericEdit(g_gui.hwndHdrTonemapTarget, 0);
        SetNumericEdit(g_gui.hwndHdrTonemapSource, 0);
        SetNumericEdit(g_gui.hwndHdrMaxTmlEdit, 0);

        // === TAB 3: Settings (initially hidden) ===
        innerY = 8;  // Reset for scroll panel
        HWND panel3 = g_gui.hwndScrollPanel[3];

        // Passthrough Mode group
        ctrl = CreateWindow(L"BUTTON", L"Passthrough Mode", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 46, panel3, nullptr, nullptr, nullptr);
        g_gui.tab3Controls.push_back(ctrl);

        g_gui.hwndSettingsVrrWhitelistCheck = CreateWindow(L"BUTTON", L"Hide overlay for apps",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 140, h, panel3, (HMENU)ID_SETTINGS_VRR_WHITELIST_CHECK, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsVrrWhitelistCheck);
        SendMessage(g_gui.hwndSettingsVrrWhitelistCheck, BM_SETCHECK, g_vrrWhitelistEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.hwndSettingsVrrWhitelistBtn = CreateWindow(L"BUTTON", L"Whitelist...",
            WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + 160, innerY + 18, 70, h, panel3, (HMENU)ID_SETTINGS_VRR_WHITELIST_BTN, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsVrrWhitelistBtn);

        // Startup group
        innerY += 51;
        ctrl = CreateWindow(L"BUTTON", L"Startup", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 68, panel3, nullptr, nullptr, nullptr);
        g_gui.tab3Controls.push_back(ctrl);

        g_gui.hwndSettingsStartMinimized = CreateWindow(L"BUTTON", L"Start minimized to tray",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 160, h, panel3, (HMENU)ID_SETTINGS_START_MINIMIZED, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsStartMinimized);
        SendMessage(g_gui.hwndSettingsStartMinimized, BM_SETCHECK, g_startMinimized.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.hwndSettingsRunAtStartup = CreateWindow(L"BUTTON", L"Run at Windows startup",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 42, 160, h, panel3, (HMENU)ID_SETTINGS_RUN_AT_STARTUP, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsRunAtStartup);
        SendMessage(g_gui.hwndSettingsRunAtStartup, BM_SETCHECK, IsStartupEnabled() ? BST_CHECKED : BST_UNCHECKED, 0);

        // Hotkeys group
        innerY += 73;
        ctrl = CreateWindow(L"BUTTON", L"Hotkeys (Win+Shift+Key)", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 90, panel3, nullptr, nullptr, nullptr);
        g_gui.tab3Controls.push_back(ctrl);

        // Gamma Toggle hotkey
        wchar_t hotkeyLabel[64];
        swprintf_s(hotkeyLabel, L"Gamma Toggle (Win+Shift+%c)", g_hotkeyGammaKey);
        g_gui.hwndSettingsHotkeyGamma = CreateWindow(L"BUTTON", hotkeyLabel,
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 220, h, panel3, (HMENU)ID_SETTINGS_HOTKEY_GAMMA_CHECK, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsHotkeyGamma);
        SendMessage(g_gui.hwndSettingsHotkeyGamma, BM_SETCHECK, g_hotkeyGammaEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        // HDR Toggle hotkey
        swprintf_s(hotkeyLabel, L"HDR Toggle (Win+Shift+%c)", g_hotkeyHdrKey);
        g_gui.hwndSettingsHotkeyHdr = CreateWindow(L"BUTTON", hotkeyLabel,
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 42, 220, h, panel3, (HMENU)ID_SETTINGS_HOTKEY_HDR_CHECK, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsHotkeyHdr);
        SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_SETCHECK, g_hotkeyHdrEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        // Analysis Overlay hotkey
        swprintf_s(hotkeyLabel, L"Analysis Overlay (Win+Shift+%c)", g_hotkeyAnalysisKey);
        g_gui.hwndSettingsHotkeyAnalysis = CreateWindow(L"BUTTON", hotkeyLabel,
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 64, 220, h, panel3, (HMENU)ID_SETTINGS_HOTKEY_ANALYSIS_CHECK, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsHotkeyAnalysis);
        SendMessage(g_gui.hwndSettingsHotkeyAnalysis, BM_SETCHECK, g_hotkeyAnalysisEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        // Debug group
        innerY += 95;
        ctrl = CreateWindow(L"BUTTON", L"Debug", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 46, panel3, nullptr, nullptr, nullptr);
        g_gui.tab3Controls.push_back(ctrl);

        g_gui.hwndSettingsConsoleLog = CreateWindow(L"BUTTON", L"Console log (requires restart)",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 190, h, panel3, (HMENU)ID_SETTINGS_CONSOLE_LOG, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsConsoleLog);
        SendMessage(g_gui.hwndSettingsConsoleLog, BM_SETCHECK, g_consoleEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.contentHeight[3] = innerY + 46 + 8;  // Track content height

        // Show all controls inside scroll panels (panels control visibility, not individual controls)
        for (HWND hCtrl : g_gui.tab0Controls) ShowWindow(hCtrl, SW_SHOW);
        for (HWND hCtrl : g_gui.tab1Controls) ShowWindow(hCtrl, SW_SHOW);
        for (HWND hCtrl : g_gui.tab2Controls) ShowWindow(hCtrl, SW_SHOW);
        for (HWND hCtrl : g_gui.tab3Controls) ShowWindow(hCtrl, SW_SHOW);

        // Store original Y positions for scroll support (query actual positions after creation)
        auto storeOriginalY = [](const std::vector<HWND>& controls, std::vector<int>& originalY, HWND panel) {
            originalY.clear();
            for (HWND hCtrl : controls) {
                RECT rc;
                GetWindowRect(hCtrl, &rc);
                POINT pt = { rc.left, rc.top };
                ScreenToClient(panel, &pt);
                originalY.push_back(pt.y);
            }
        };
        storeOriginalY(g_gui.tab0Controls, g_gui.tab0OriginalY, g_gui.hwndScrollPanel[0]);
        storeOriginalY(g_gui.tab1Controls, g_gui.tab1OriginalY, g_gui.hwndScrollPanel[1]);
        storeOriginalY(g_gui.tab2Controls, g_gui.tab2OriginalY, g_gui.hwndScrollPanel[2]);
        storeOriginalY(g_gui.tab3Controls, g_gui.tab3OriginalY, g_gui.hwndScrollPanel[3]);

        // Set up scroll info for each tab
        for (int i = 0; i < 4; i++) {
            int maxScroll = max(0, g_gui.contentHeight[i] - g_gui.panelHeight);
            SCROLLINFO si = {};
            si.cbSize = sizeof(si);
            si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
            si.nMin = 0;
            si.nMax = g_gui.contentHeight[i];
            si.nPage = g_gui.panelHeight;
            si.nPos = 0;
            SetScrollInfo(g_gui.hwndScrollPanel[i], SB_VERT, &si, TRUE);
            // Hide scrollbar if content fits
            ShowScrollBar(g_gui.hwndScrollPanel[i], SB_VERT, maxScroll > 0);
        }

        // Buttons anchored to bottom right (owner-drawn for rounded corners)
        int btnPad = 8;
        int enableW = 80, disableW = 80;
        g_gui.hwndStop = CreateWindow(L"BUTTON", L"Disable", WS_CHILD | WS_VISIBLE | WS_DISABLED | BS_OWNERDRAW,
            clientW - margin - disableW, btnY, disableW, btnH, hwnd, (HMENU)ID_STOP, nullptr, nullptr);
        g_gui.hwndApply = CreateWindow(L"BUTTON", L"Enable", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            clientW - margin - disableW - btnPad - enableW, btnY, enableW, btnH, hwnd, (HMENU)ID_APPLY, nullptr, nullptr);

        // Separator line between buttons and status
        CreateWindow(L"STATIC", nullptr, WS_CHILD | WS_VISIBLE | SS_ETCHEDHORZ,
            margin, separatorY, contentW, separatorH, hwnd, nullptr, nullptr, nullptr);

        // Status at bottom - label left-aligned, value right-aligned and wide enough for long messages
        int statusLabelW = 45;
        int statusLabelX = margin;
        int statusValueX = margin + statusLabelW + 4;
        int statusValueW = clientW - statusValueX - margin;  // Extends to right edge
        CreateWindow(L"STATIC", L"Status:", WS_CHILD | WS_VISIBLE,
            statusLabelX, statusY, statusLabelW, h, hwnd, nullptr, nullptr, nullptr);
        g_gui.hwndStatus = CreateWindow(L"STATIC", L"Inactive",
            WS_CHILD | WS_VISIBLE | SS_RIGHT,
            statusValueX, statusY, statusValueW, h, hwnd, (HMENU)ID_STATUS, nullptr, nullptr);

        // Set font for all controls (stored globally, cleaned up in WM_DESTROY)
        g_mainFont = CreateFont(16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        EnumChildWindows(hwnd, [](HWND hwndChild, LPARAM lParam) -> BOOL {
            SendMessage(hwndChild, WM_SETFONT, lParam, TRUE);
            return TRUE;
        }, (LPARAM)g_mainFont);

        // Populate monitor list
        std::vector<HMONITOR> monitors;
        EnumDisplayMonitors(nullptr, nullptr, GUIMonitorEnumProc, reinterpret_cast<LPARAM>(&monitors));
        g_gui.monitors = monitors;

        for (size_t i = 0; i < monitors.size(); i++) {
            MONITORINFO mi = { sizeof(mi) };
            GetMonitorInfo(monitors[i], &mi);
            int monW = mi.rcMonitor.right - mi.rcMonitor.left;
            int monH = mi.rcMonitor.bottom - mi.rcMonitor.top;
            wchar_t name[128];
            swprintf_s(name, L"Monitor %d: %dx%d%s", (int)i + 1, monW, monH,
                (mi.dwFlags & MONITORINFOF_PRIMARY) ? L" [Primary]" : L"");
            SendMessage(g_gui.hwndMonitorList, LB_ADDSTRING, 0, (LPARAM)name);
            g_gui.monitorNames.push_back(name);
            g_gui.monitorSettings.push_back({});  // Empty settings for each monitor
        }

        // Load saved settings from INI
        LoadSettings();

        // Update checkboxes from loaded settings
        SendMessage(g_gui.hwndGammaCheck, BM_SETCHECK,
            g_desktopGammaMode ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessage(g_gui.hwndTetrahedralCheck, BM_SETCHECK,
            g_tetrahedralInterp ? BST_CHECKED : BST_UNCHECKED, 0);

        // Update Settings tab checkboxes from loaded settings
        SendMessage(g_gui.hwndSettingsHotkeyGamma, BM_SETCHECK,
            g_hotkeyGammaEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_SETCHECK,
            g_hotkeyHdrEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessage(g_gui.hwndSettingsHotkeyAnalysis, BM_SETCHECK,
            g_hotkeyAnalysisEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessage(g_gui.hwndSettingsStartMinimized, BM_SETCHECK,
            g_startMinimized.load() ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessage(g_gui.hwndSettingsConsoleLog, BM_SETCHECK,
            g_consoleEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);
        SendMessage(g_gui.hwndSettingsVrrWhitelistCheck, BM_SETCHECK,
            g_vrrWhitelistEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        if (!monitors.empty()) {
            SendMessage(g_gui.hwndMonitorList, LB_SETCURSEL, 0, 0);
            g_gui.currentMonitor = 0;
            // Update UI with monitor 0's settings
            SetPathText(g_gui.hwndSdrPath, g_gui.monitorSettings[0].sdrPath.c_str());
            SetPathText(g_gui.hwndHdrPath, g_gui.monitorSettings[0].hdrPath.c_str());
            // Load color correction controls for initial monitor
            UpdateColorCorrectionControls();
        }

        // Add tray icon
        AddTrayIcon(hwnd);

        // Update startup registry path if exe was moved
        UpdateStartupPath();

        // Note: Auto-start is handled in RunGUI() after window creation
        // This allows proper control of button states and startup flags

        return 0;
    }

    case WM_NOTIFY: {
        NMHDR* nmhdr = (NMHDR*)lParam;
        if (nmhdr->hwndFrom == g_gui.hwndTab && nmhdr->code == TCN_SELCHANGE) {
            int newTab = TabCtrl_GetCurSel(g_gui.hwndTab);
            // Show/hide scroll panels based on tab
            for (int i = 0; i < 4; i++) {
                ShowWindow(g_gui.hwndScrollPanel[i], i == newTab ? SW_SHOW : SW_HIDE);
            }
            g_gui.currentTab = newTab;
        }
        break;
    }

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_MONITOR_LIST:
            if (HIWORD(wParam) == LBN_SELCHANGE) {
                // Load new monitor's settings
                int sel = (int)SendMessage(g_gui.hwndMonitorList, LB_GETCURSEL, 0, 0);
                if (sel >= 0 && sel < (int)g_gui.monitorSettings.size()) {
                    g_gui.currentMonitor = sel;
                    SetPathText(g_gui.hwndSdrPath, g_gui.monitorSettings[sel].sdrPath.c_str());
                    SetPathText(g_gui.hwndHdrPath, g_gui.monitorSettings[sel].hdrPath.c_str());
                    // Load color correction controls for this monitor
                    UpdateColorCorrectionControls();
                }
            }
            return 0;
        case ID_SDR_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            if (BrowseForLUT(hwnd, path, MAX_PATH)) {
                SetPathText(g_gui.hwndSdrPath, path);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].sdrPath = path;
                }
                UpdateGUIState();
            }
            return 0;
        }
        case ID_SDR_CLEAR:
            SetPathText(g_gui.hwndSdrPath, L"");
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrPath.clear();
            }
            UpdateGUIState();
            return 0;
        case ID_HDR_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            if (BrowseForLUT(hwnd, path, MAX_PATH)) {
                SetPathText(g_gui.hwndHdrPath, path);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrPath = path;
                }
                UpdateGUIState();
            }
            return 0;
        }
        case ID_HDR_CLEAR:
            SetPathText(g_gui.hwndHdrPath, L"");
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].hdrPath.clear();
            }
            UpdateGUIState();
            return 0;
        case ID_APPLY:
            g_desktopGammaMode = (SendMessage(g_gui.hwndGammaCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            g_tetrahedralInterp = (SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            if (g_gui.isRunning) {
                StopProcessing();
            }
            StartProcessing();
            return 0;
        case ID_STOP:
            StopProcessing();
            return 0;
        case ID_GAMMA_CHECK:
            {
                bool checked = (SendMessage(g_gui.hwndGammaCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_userDesktopGammaMode.store(checked);
                // Only update effective state if whitelist isn't overriding
                if (!g_gammaWhitelistActive.load()) {
                    g_desktopGammaMode.store(checked);
                }
                SaveSettings();
            }
            return 0;
        case ID_GAMMA_WHITELIST_BTN:
            ShowGammaWhitelistDialog(hwnd);
            return 0;
        case ID_TETRAHEDRAL_CHECK:
            g_tetrahedralInterp = (SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        // SDR Color Correction Controls
        case ID_SDR_PRIMARIES_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.primariesEnabled =
                    (SendMessage(g_gui.hwndSdrPrimariesEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                ApplyPrimariesChange(false);
                SaveSettings();
            }
            return 0;

        case ID_SDR_PRIMARIES_PRESET:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int sel = (int)SendMessage(g_gui.hwndSdrPrimariesPreset, CB_GETCURSEL, 0, 0);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& cc = g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
                    int oldPreset = cc.primariesPreset;

                    // Save current edit box values to customPrimaries if leaving Custom preset
                    if (oldPreset == g_numPresetPrimaries - 1) {
                        wchar_t buf[16];
                        GetWindowText(g_gui.hwndSdrPrimariesRx, buf, 16); cc.customPrimaries.Rx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesRy, buf, 16); cc.customPrimaries.Ry = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesGx, buf, 16); cc.customPrimaries.Gx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesGy, buf, 16); cc.customPrimaries.Gy = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesBx, buf, 16); cc.customPrimaries.Bx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesBy, buf, 16); cc.customPrimaries.By = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesWx, buf, 16); cc.customPrimaries.Wx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndSdrPrimariesWy, buf, 16); cc.customPrimaries.Wy = (float)_wtof(buf);
                    }

                    cc.primariesPreset = sel;
                    // Enable/disable custom edit boxes
                    bool customEnabled = (sel == g_numPresetPrimaries - 1);
                    EnableWindow(g_gui.hwndSdrPrimariesRx, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesRy, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesGx, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesGy, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesBx, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesBy, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesWx, customEnabled);
                    EnableWindow(g_gui.hwndSdrPrimariesWy, customEnabled);

                    // Update edit boxes - use customPrimaries for Custom preset, otherwise use preset values
                    wchar_t buf[16];
                    if (customEnabled) {
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Rx); SetWindowText(g_gui.hwndSdrPrimariesRx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Ry); SetWindowText(g_gui.hwndSdrPrimariesRy, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Gx); SetWindowText(g_gui.hwndSdrPrimariesGx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Gy); SetWindowText(g_gui.hwndSdrPrimariesGy, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Bx); SetWindowText(g_gui.hwndSdrPrimariesBx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.By); SetWindowText(g_gui.hwndSdrPrimariesBy, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Wx); SetWindowText(g_gui.hwndSdrPrimariesWx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Wy); SetWindowText(g_gui.hwndSdrPrimariesWy, buf);
                    } else {
                        const auto& preset = g_presetPrimaries[sel];
                        swprintf_s(buf, L"%.4f", preset.Rx); SetWindowText(g_gui.hwndSdrPrimariesRx, buf);
                        swprintf_s(buf, L"%.4f", preset.Ry); SetWindowText(g_gui.hwndSdrPrimariesRy, buf);
                        swprintf_s(buf, L"%.4f", preset.Gx); SetWindowText(g_gui.hwndSdrPrimariesGx, buf);
                        swprintf_s(buf, L"%.4f", preset.Gy); SetWindowText(g_gui.hwndSdrPrimariesGy, buf);
                        swprintf_s(buf, L"%.4f", preset.Bx); SetWindowText(g_gui.hwndSdrPrimariesBx, buf);
                        swprintf_s(buf, L"%.4f", preset.By); SetWindowText(g_gui.hwndSdrPrimariesBy, buf);
                        swprintf_s(buf, L"%.4f", preset.Wx); SetWindowText(g_gui.hwndSdrPrimariesWx, buf);
                        swprintf_s(buf, L"%.4f", preset.Wy); SetWindowText(g_gui.hwndSdrPrimariesWy, buf);
                    }
                    ApplyPrimariesChange(false);
                    SaveSettings();
                }
            }
            return 0;

        case ID_SDR_PRIMARIES_DETECT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                // Try EDID first (more reliable), fall back to DXGI
                MonitorPrimaries primaries = GetMonitorPrimariesFromEDID(g_gui.currentMonitor);
                if (!primaries.valid) {
                    primaries = GetMonitorPrimaries(g_gui.currentMonitor);
                }
                if (primaries.valid) {
                    auto& cc = g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
                    // Switch to Custom preset
                    cc.primariesPreset = g_numPresetPrimaries - 1;
                    SendMessage(g_gui.hwndSdrPrimariesPreset, CB_SETCURSEL, cc.primariesPreset, 0);
                    // Fill in detected values
                    cc.customPrimaries.Rx = primaries.Rx;
                    cc.customPrimaries.Ry = primaries.Ry;
                    cc.customPrimaries.Gx = primaries.Gx;
                    cc.customPrimaries.Gy = primaries.Gy;
                    cc.customPrimaries.Bx = primaries.Bx;
                    cc.customPrimaries.By = primaries.By;
                    // Use D65 white point instead of EDID white point
                    // EDID reports native panel white, but display presets already calibrate to D65
                    // Using EDID white would double-correct; user can manually enter if needed
                    cc.customPrimaries.Wx = 0.3127f;
                    cc.customPrimaries.Wy = 0.3290f;
                    // Update edit boxes
                    wchar_t buf[16];
                    swprintf_s(buf, L"%.4f", primaries.Rx); SetWindowText(g_gui.hwndSdrPrimariesRx, buf);
                    swprintf_s(buf, L"%.4f", primaries.Ry); SetWindowText(g_gui.hwndSdrPrimariesRy, buf);
                    swprintf_s(buf, L"%.4f", primaries.Gx); SetWindowText(g_gui.hwndSdrPrimariesGx, buf);
                    swprintf_s(buf, L"%.4f", primaries.Gy); SetWindowText(g_gui.hwndSdrPrimariesGy, buf);
                    swprintf_s(buf, L"%.4f", primaries.Bx); SetWindowText(g_gui.hwndSdrPrimariesBx, buf);
                    swprintf_s(buf, L"%.4f", primaries.By); SetWindowText(g_gui.hwndSdrPrimariesBy, buf);
                    swprintf_s(buf, L"%.4f", cc.customPrimaries.Wx); SetWindowText(g_gui.hwndSdrPrimariesWx, buf);
                    swprintf_s(buf, L"%.4f", cc.customPrimaries.Wy); SetWindowText(g_gui.hwndSdrPrimariesWy, buf);
                    // Enable custom edit boxes
                    EnableWindow(g_gui.hwndSdrPrimariesRx, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesRy, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesGx, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesGy, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesBx, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesBy, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesWx, TRUE);
                    EnableWindow(g_gui.hwndSdrPrimariesWy, TRUE);
                    ApplyPrimariesChange(false);
                    SaveSettings();
                } else {
                    MessageBox(hwnd, L"Could not detect monitor primaries.\nEDID data may not be available.", L"Detection Failed", MB_OK | MB_ICONWARNING);
                }
            }
            return 0;

        case ID_HDR_PRIMARIES_DETECT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                // Try EDID first (more reliable), fall back to DXGI
                MonitorPrimaries primaries = GetMonitorPrimariesFromEDID(g_gui.currentMonitor);
                if (!primaries.valid) {
                    primaries = GetMonitorPrimaries(g_gui.currentMonitor);
                }
                if (primaries.valid) {
                    auto& cc = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection;
                    // Switch to Custom preset
                    cc.primariesPreset = g_numPresetPrimaries - 1;
                    SendMessage(g_gui.hwndHdrPrimariesPreset, CB_SETCURSEL, cc.primariesPreset, 0);
                    // Fill in detected values
                    cc.customPrimaries.Rx = primaries.Rx;
                    cc.customPrimaries.Ry = primaries.Ry;
                    cc.customPrimaries.Gx = primaries.Gx;
                    cc.customPrimaries.Gy = primaries.Gy;
                    cc.customPrimaries.Bx = primaries.Bx;
                    cc.customPrimaries.By = primaries.By;
                    // Use D65 white point instead of EDID white point
                    // EDID reports native panel white, but display presets already calibrate to D65
                    // Using EDID white would double-correct; user can manually enter if needed
                    cc.customPrimaries.Wx = 0.3127f;
                    cc.customPrimaries.Wy = 0.3290f;
                    // Update edit boxes
                    wchar_t buf[16];
                    swprintf_s(buf, L"%.4f", primaries.Rx); SetWindowText(g_gui.hwndHdrPrimariesRx, buf);
                    swprintf_s(buf, L"%.4f", primaries.Ry); SetWindowText(g_gui.hwndHdrPrimariesRy, buf);
                    swprintf_s(buf, L"%.4f", primaries.Gx); SetWindowText(g_gui.hwndHdrPrimariesGx, buf);
                    swprintf_s(buf, L"%.4f", primaries.Gy); SetWindowText(g_gui.hwndHdrPrimariesGy, buf);
                    swprintf_s(buf, L"%.4f", primaries.Bx); SetWindowText(g_gui.hwndHdrPrimariesBx, buf);
                    swprintf_s(buf, L"%.4f", primaries.By); SetWindowText(g_gui.hwndHdrPrimariesBy, buf);
                    swprintf_s(buf, L"%.4f", cc.customPrimaries.Wx); SetWindowText(g_gui.hwndHdrPrimariesWx, buf);
                    swprintf_s(buf, L"%.4f", cc.customPrimaries.Wy); SetWindowText(g_gui.hwndHdrPrimariesWy, buf);
                    // Enable custom edit boxes
                    EnableWindow(g_gui.hwndHdrPrimariesRx, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesRy, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesGx, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesGy, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesBx, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesBy, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesWx, TRUE);
                    EnableWindow(g_gui.hwndHdrPrimariesWy, TRUE);
                    ApplyPrimariesChange(true);
                    SaveSettings();
                } else {
                    MessageBox(hwnd, L"Could not detect monitor primaries.\nEDID data may not be available.", L"Detection Failed", MB_OK | MB_ICONWARNING);
                }
            }
            return 0;

        case ID_SDR_PRIMARIES_RX: case ID_SDR_PRIMARIES_RY:
        case ID_SDR_PRIMARIES_GX: case ID_SDR_PRIMARIES_GY:
        case ID_SDR_PRIMARIES_BX: case ID_SDR_PRIMARIES_BY:
        case ID_SDR_PRIMARIES_WX: case ID_SDR_PRIMARIES_WY:
            if (HIWORD(wParam) == EN_CHANGE) {
                // Update Enable button state while typing
                UpdateGUIState();
            }
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& cp = g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.customPrimaries;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndSdrPrimariesRx, buf, 16); cp.Rx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesRy, buf, 16); cp.Ry = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesGx, buf, 16); cp.Gx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesGy, buf, 16); cp.Gy = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesBx, buf, 16); cp.Bx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesBy, buf, 16); cp.By = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesWx, buf, 16); cp.Wx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndSdrPrimariesWy, buf, 16); cp.Wy = (float)_wtof(buf);
                    ApplyPrimariesChange(false);
                    SaveSettings();
                }
            }
            return 0;

        case ID_SDR_GRAYSCALE_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale.enabled =
                    (SendMessage(g_gui.hwndSdrGrayscaleEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, false);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_SDR_GRAYSCALE_10:
        case ID_SDR_GRAYSCALE_20:
        case ID_SDR_GRAYSCALE_32:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                auto& gs = g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                int newCount = (LOWORD(wParam) == ID_SDR_GRAYSCALE_10) ? 10 :
                               (LOWORD(wParam) == ID_SDR_GRAYSCALE_20) ? 20 : 32;
                if (newCount != gs.pointCount) {
                    gs.pointCount = newCount;
                    gs.points.resize(newCount);
                    gs.initLinear();
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, false);
                    }
                    UpdateGUIState();
                }
            }
            return 0;

        case ID_SDR_GRAYSCALE_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                auto& gs = g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                    gs.points.resize(gs.pointCount);
                    gs.initLinear();
                }
                ShowGrayscaleEditor(hwnd, gs, false);
            }
            return 0;

        case ID_SDR_GRAYSCALE_RESET:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                auto& gs = g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                gs.initLinear();
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, false);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_SDR_GRAYSCALE_24:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale.use24Gamma =
                    (SendMessage(g_gui.hwndSdrGrayscale24, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, false);
                }
                UpdateGUIState();
            }
            return 0;

        // HDR Color Correction Controls
        case ID_HDR_PRIMARIES_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.primariesEnabled =
                    (SendMessage(g_gui.hwndHdrPrimariesEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                ApplyPrimariesChange(true);
                SaveSettings();
            }
            return 0;

        case ID_HDR_PRIMARIES_PRESET:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int sel = (int)SendMessage(g_gui.hwndHdrPrimariesPreset, CB_GETCURSEL, 0, 0);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& cc = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection;
                    int oldPreset = cc.primariesPreset;

                    // Save current edit box values to customPrimaries if leaving Custom preset
                    if (oldPreset == g_numPresetPrimaries - 1) {
                        wchar_t buf[16];
                        GetWindowText(g_gui.hwndHdrPrimariesRx, buf, 16); cc.customPrimaries.Rx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesRy, buf, 16); cc.customPrimaries.Ry = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesGx, buf, 16); cc.customPrimaries.Gx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesGy, buf, 16); cc.customPrimaries.Gy = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesBx, buf, 16); cc.customPrimaries.Bx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesBy, buf, 16); cc.customPrimaries.By = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesWx, buf, 16); cc.customPrimaries.Wx = (float)_wtof(buf);
                        GetWindowText(g_gui.hwndHdrPrimariesWy, buf, 16); cc.customPrimaries.Wy = (float)_wtof(buf);
                    }

                    cc.primariesPreset = sel;
                    bool customEnabled = (sel == g_numPresetPrimaries - 1);
                    EnableWindow(g_gui.hwndHdrPrimariesRx, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesRy, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesGx, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesGy, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesBx, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesBy, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesWx, customEnabled);
                    EnableWindow(g_gui.hwndHdrPrimariesWy, customEnabled);

                    // Update edit boxes - use customPrimaries for Custom preset, otherwise use preset values
                    wchar_t buf[16];
                    if (customEnabled) {
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Rx); SetWindowText(g_gui.hwndHdrPrimariesRx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Ry); SetWindowText(g_gui.hwndHdrPrimariesRy, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Gx); SetWindowText(g_gui.hwndHdrPrimariesGx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Gy); SetWindowText(g_gui.hwndHdrPrimariesGy, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Bx); SetWindowText(g_gui.hwndHdrPrimariesBx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.By); SetWindowText(g_gui.hwndHdrPrimariesBy, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Wx); SetWindowText(g_gui.hwndHdrPrimariesWx, buf);
                        swprintf_s(buf, L"%.4f", cc.customPrimaries.Wy); SetWindowText(g_gui.hwndHdrPrimariesWy, buf);
                    } else {
                        const auto& preset = g_presetPrimaries[sel];
                        swprintf_s(buf, L"%.4f", preset.Rx); SetWindowText(g_gui.hwndHdrPrimariesRx, buf);
                        swprintf_s(buf, L"%.4f", preset.Ry); SetWindowText(g_gui.hwndHdrPrimariesRy, buf);
                        swprintf_s(buf, L"%.4f", preset.Gx); SetWindowText(g_gui.hwndHdrPrimariesGx, buf);
                        swprintf_s(buf, L"%.4f", preset.Gy); SetWindowText(g_gui.hwndHdrPrimariesGy, buf);
                        swprintf_s(buf, L"%.4f", preset.Bx); SetWindowText(g_gui.hwndHdrPrimariesBx, buf);
                        swprintf_s(buf, L"%.4f", preset.By); SetWindowText(g_gui.hwndHdrPrimariesBy, buf);
                        swprintf_s(buf, L"%.4f", preset.Wx); SetWindowText(g_gui.hwndHdrPrimariesWx, buf);
                        swprintf_s(buf, L"%.4f", preset.Wy); SetWindowText(g_gui.hwndHdrPrimariesWy, buf);
                    }
                    ApplyPrimariesChange(true);
                    SaveSettings();
                }
            }
            return 0;

        case ID_HDR_PRIMARIES_RX: case ID_HDR_PRIMARIES_RY:
        case ID_HDR_PRIMARIES_GX: case ID_HDR_PRIMARIES_GY:
        case ID_HDR_PRIMARIES_BX: case ID_HDR_PRIMARIES_BY:
        case ID_HDR_PRIMARIES_WX: case ID_HDR_PRIMARIES_WY:
            if (HIWORD(wParam) == EN_CHANGE) {
                // Update Enable button state while typing
                UpdateGUIState();
            }
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& cp = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.customPrimaries;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndHdrPrimariesRx, buf, 16); cp.Rx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesRy, buf, 16); cp.Ry = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesGx, buf, 16); cp.Gx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesGy, buf, 16); cp.Gy = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesBx, buf, 16); cp.Bx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesBy, buf, 16); cp.By = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesWx, buf, 16); cp.Wx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndHdrPrimariesWy, buf, 16); cp.Wy = (float)_wtof(buf);
                    ApplyPrimariesChange(true);
                    SaveSettings();
                }
            }
            return 0;

        case ID_HDR_GRAYSCALE_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale.enabled =
                    (SendMessage(g_gui.hwndHdrGrayscaleEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_HDR_GRAYSCALE_10:
        case ID_HDR_GRAYSCALE_20:
        case ID_HDR_GRAYSCALE_32:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                auto& gs = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale;
                int newCount = (LOWORD(wParam) == ID_HDR_GRAYSCALE_10) ? 10 :
                               (LOWORD(wParam) == ID_HDR_GRAYSCALE_20) ? 20 : 32;
                if (newCount != gs.pointCount) {
                    gs.pointCount = newCount;
                    gs.points.resize(newCount);
                    gs.initLinearPQ();  // HDR uses PQ-space grayscale
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    }
                    UpdateGUIState();
                }
            }
            return 0;

        case ID_HDR_GRAYSCALE_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                auto& gs = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale;
                if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                    gs.points.resize(gs.pointCount);
                    gs.initLinearPQ();  // HDR uses PQ-space grayscale
                }
                ShowGrayscaleEditor(hwnd, gs, true);
            }
            return 0;

        case ID_HDR_GRAYSCALE_RESET:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                auto& gs = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale;
                gs.initLinearPQ();  // HDR uses PQ-space grayscale
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_HDR_GRAYSCALE_PEAK:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndHdrGrayscalePeak, buf, 16);
                    float peak = (float)_wtof(buf);
                    if (peak < 100.0f) peak = 100.0f;
                    if (peak > 10000.0f) peak = 10000.0f;
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale.peakNits = peak;
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    }
                    SaveSettings();
                }
            }
            return 0;

        // HDR Tonemapping controls
        case ID_HDR_TONEMAP_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndHdrTonemapEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.enabled = enabled;
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                }
                SaveSettings();
                UpdateGUIState();
            }
            return 0;

        case ID_HDR_TONEMAP_CURVE:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    int sel = (int)SendMessage(g_gui.hwndHdrTonemapCurve, CB_GETCURSEL, 0, 0);
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.curve =
                        DropdownIndexToTonemapCurve(sel);
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    }
                    SaveSettings();
                }
            }
            return 0;

        case ID_HDR_TONEMAP_TARGET:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& tm = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndHdrTonemapTarget, buf, 16);
                    tm.targetPeakNits = (float)_wtof(buf);
                    if (tm.targetPeakNits < 100.0f) tm.targetPeakNits = 100.0f;
                    if (tm.targetPeakNits > 10000.0f) tm.targetPeakNits = 10000.0f;
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    }
                    SaveSettings();
                }
            }
            return 0;

        case ID_HDR_TONEMAP_DYNAMIC:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndHdrTonemapDynamic, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.dynamicPeak = enabled;
                // Enable/disable Source textbox based on Dynamic checkbox
                EnableWindow(g_gui.hwndHdrTonemapSource, !enabled);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                }
                SaveSettings();
            }
            return 0;

        case ID_HDR_TONEMAP_SOURCE:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& tm = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndHdrTonemapSource, buf, 16);
                    tm.sourcePeakNits = (float)_wtof(buf);
                    if (tm.sourcePeakNits < 100.0f) tm.sourcePeakNits = 100.0f;
                    if (tm.sourcePeakNits > 10000.0f) tm.sourcePeakNits = 10000.0f;
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    }
                    SaveSettings();
                }
            }
            return 0;

        // MaxTML controls
        case ID_HDR_MAXTML_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndHdrMaxTmlEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].maxTml.enabled = enabled;
                SaveSettings();
            }
            return 0;

        case ID_HDR_MAXTML_COMBO:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int sel = (int)SendMessage(g_gui.hwndHdrMaxTmlCombo, CB_GETCURSEL, 0, 0);
                // Update edit box based on preset selection
                const wchar_t* values[] = { L"", L"400", L"600", L"1000", L"1400", L"4000", L"10000" };
                const float nitsValues[] = { 0, 400, 600, 1000, 1400, 4000, 10000 };
                if (sel > 0 && sel < 7) {
                    SetWindowText(g_gui.hwndHdrMaxTmlEdit, values[sel]);
                    // Save to settings
                    if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                        g_gui.monitorSettings[g_gui.currentMonitor].maxTml.peakNits = nitsValues[sel];
                        SaveSettings();
                    }
                }
            }
            return 0;

        case ID_HDR_MAXTML_APPLY:
            {
                // Get the nits value from edit box
                wchar_t buf[16];
                GetWindowText(g_gui.hwndHdrMaxTmlEdit, buf, 16);
                float nits = (float)_wtof(buf);
                if (nits < 100.0f) nits = 100.0f;
                if (nits > 10000.0f) nits = 10000.0f;

                // Save to settings
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].maxTml.peakNits = nits;
                    SaveSettings();
                }

                // Get display info for current monitor
                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(g_gui.currentMonitor, displayInfo)) {
                    if (SetDisplayMaxTml(displayInfo, nits)) {
                        // Success - show brief message
                        wchar_t msg[256];
                        const wchar_t* name = displayInfo.name.empty() ? L"selected monitor" : displayInfo.name.c_str();
                        swprintf_s(msg, L"MaxTML set to %.0f nits for %s", nits, name);
                        MessageBox(hwnd, msg, L"DesktopLUT", MB_OK | MB_ICONINFORMATION);
                    } else {
                        MessageBox(hwnd, L"Failed to set MaxTML. Make sure HDR is enabled.", L"Error", MB_OK | MB_ICONERROR);
                    }
                } else {
                    MessageBox(hwnd, L"Could not find display information for this monitor.", L"Error", MB_OK | MB_ICONERROR);
                }
            }
            return 0;

        // Settings tab controls - hotkeys register/unregister dynamically if running
        case ID_SETTINGS_HOTKEY_GAMMA_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyGamma, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyGammaEnabled.store(enable);
                if (g_mainHwnd) {
                    if (enable) RegisterHotKey(g_mainHwnd, HOTKEY_GAMMA, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyGammaKey);
                    else UnregisterHotKey(g_mainHwnd, HOTKEY_GAMMA);
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_HOTKEY_HDR_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyHdrEnabled.store(enable);
                if (g_mainHwnd) {
                    if (enable) RegisterHotKey(g_mainHwnd, HOTKEY_HDR_TOGGLE, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyHdrKey);
                    else UnregisterHotKey(g_mainHwnd, HOTKEY_HDR_TOGGLE);
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_HOTKEY_ANALYSIS_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyAnalysis, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyAnalysisEnabled.store(enable);
                if (g_mainHwnd) {
                    if (enable) RegisterHotKey(g_mainHwnd, HOTKEY_ANALYSIS, MOD_WIN | MOD_SHIFT | MOD_NOREPEAT, g_hotkeyAnalysisKey);
                    else UnregisterHotKey(g_mainHwnd, HOTKEY_ANALYSIS);
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_START_MINIMIZED:
            g_startMinimized.store(SendMessage(g_gui.hwndSettingsStartMinimized, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        case ID_SETTINGS_RUN_AT_STARTUP:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsRunAtStartup, BM_GETCHECK, 0, 0) == BST_CHECKED);
                SetStartupEnabled(enable);
            }
            return 0;

        case ID_SETTINGS_CONSOLE_LOG:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsConsoleLog, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_consoleEnabled.store(enable);
                if (enable) {
                    if (GetConsoleWindow() == nullptr) {
                        if (AllocConsole()) {
                            FILE* fp;
                            freopen_s(&fp, "CONOUT$", "w", stdout);
                            freopen_s(&fp, "CONOUT$", "w", stderr);
                            std::cout.clear();
                            std::cerr.clear();
                            std::cout << "Console enabled" << std::endl;
                        }
                    }
                } else {
                    HWND consoleWnd = GetConsoleWindow();
                    if (consoleWnd != nullptr) {
                        FreeConsole();
                    }
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_VRR_WHITELIST_CHECK:
            g_vrrWhitelistEnabled.store(SendMessage(g_gui.hwndSettingsVrrWhitelistCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        case ID_SETTINGS_VRR_WHITELIST_BTN:
            ShowVrrWhitelistDialog(hwnd);
            return 0;

        case ID_TRAY_SHOW:
            ShowWindow(hwnd, SW_RESTORE);
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetForegroundWindow(hwnd);
            return 0;
        case ID_TRAY_APPLY:
            StartProcessing();
            return 0;
        case ID_TRAY_STOP:
            StopProcessing();
            return 0;
        case ID_TRAY_STARTUP:
            SetStartupEnabled(!IsStartupEnabled());
            return 0;
        case ID_TRAY_EXIT:
            StopProcessing();
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    case WM_TRAYICON:
        if (lParam == WM_RBUTTONUP) {
            ShowTrayMenu(hwnd);
        } else if (lParam == WM_LBUTTONUP) {
            ShowWindow(hwnd, SW_RESTORE);
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            SetForegroundWindow(hwnd);
        }
        return 0;

    case WM_USER + 100:  // Processing stopped
        g_gui.isRunning = false;
        UpdateGUIState();
        return 0;

    case WM_SIZE:
        if (wParam == SIZE_MINIMIZED) {
            ShowWindow(hwnd, SW_HIDE);
        }
        return 0;

    case WM_POWERBROADCAST:
        // Handle power events for sleep/wake recovery (defense in depth with overlay WndProc)
        if (wParam == PBT_APMRESUMEAUTOMATIC || wParam == PBT_APMRESUMESUSPEND) {
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
            }
        }
        return TRUE;

    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        return 0;

    case WM_DESTROY:
        StopProcessing();
        RemoveTrayIcon();
        // Clean up custom brushes and fonts
        if (g_tabBgBrush) { DeleteObject(g_tabBgBrush); g_tabBgBrush = nullptr; }
        if (g_inactiveTabBrush) { DeleteObject(g_inactiveTabBrush); g_inactiveTabBrush = nullptr; }
        if (g_mainFont) { DeleteObject(g_mainFont); g_mainFont = nullptr; }
        if (g_grayscaleFont) { DeleteObject(g_grayscaleFont); g_grayscaleFont = nullptr; }
        PostQuitMessage(0);
        return 0;
    }

    return DefWindowProc(hwnd, msg, wParam, lParam);
}

int RunGUI() {
    // Boost process priority for faster startup and smoother operation
    SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);

    // Check if console logging is enabled for debugging
    std::wstring iniPath = GetIniPath();
    g_consoleEnabled.store(GetPrivateProfileBool(L"General", L"ConsoleLog", false, iniPath.c_str()));
    if (g_consoleEnabled.load()) {
        if (AllocConsole()) {
            FILE* fp;
            freopen_s(&fp, "CONOUT$", "w", stdout);
            freopen_s(&fp, "CONOUT$", "w", stderr);
            std::cout.clear();
            std::cerr.clear();
        }
    }

    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    // Initialize common controls
    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&icc);

    // Register window class
    WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
    wc.lpfnWndProc = GUIWndProc;
    wc.hInstance = GetModuleHandle(nullptr);
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = L"DesktopLUT_GUI";
    wc.hIcon = LoadIcon(wc.hInstance, MAKEINTRESOURCE(IDI_APPICON));
    wc.hIconSm = LoadIcon(wc.hInstance, MAKEINTRESOURCE(IDI_APPICON));
    RegisterClassEx(&wc);

    // Register scroll panel window class
    WNDCLASSEX wcScroll = { sizeof(WNDCLASSEX) };
    wcScroll.lpfnWndProc = ScrollPanelProc;
    wcScroll.hInstance = wc.hInstance;
    wcScroll.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wcScroll.hbrBackground = nullptr;  // We handle painting
    wcScroll.lpszClassName = L"DesktopLUT_ScrollPanel";
    RegisterClassEx(&wcScroll);

    // Create main window
    int winW = 580;  // Wider to fit all controls
    int winH = 530;  // Height to fit separator line between buttons and status
    int screenW = GetSystemMetrics(SM_CXSCREEN);
    int screenH = GetSystemMetrics(SM_CYSCREEN);

    g_gui.hwndMain = CreateWindowEx(
        0, L"DesktopLUT_GUI", L"DesktopLUT",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
        (screenW - winW) / 2, (screenH - winH) / 2, winW, winH,
        nullptr, nullptr, wc.hInstance, nullptr);

    if (!g_gui.hwndMain) {
        return 1;
    }

    // Check if any visual correction is enabled (LUT, Primaries, Grayscale, 2.4 Gamma, Desktop Gamma)
    bool hasAnyCorrection = g_userDesktopGammaMode.load();  // Desktop gamma is a global setting
    for (const auto& settings : g_gui.monitorSettings) {
        if (!settings.sdrPath.empty() ||
            !settings.hdrPath.empty() ||
            settings.sdrColorCorrection.primariesEnabled ||
            settings.sdrColorCorrection.grayscale.enabled ||
            settings.sdrColorCorrection.grayscale.use24Gamma ||
            settings.hdrColorCorrection.primariesEnabled ||
            settings.hdrColorCorrection.grayscale.enabled ||
            settings.hdrColorCorrection.tonemap.enabled) {
            hasAnyCorrection = true;
            break;
        }
    }

    // Auto-start processing if any correction is enabled
    if (hasAnyCorrection) {
        StartProcessing();
    }

    // Only start minimized if user explicitly enabled the setting
    if (!g_startMinimized.load()) {
        ShowWindow(g_gui.hwndMain, SW_SHOW);
        UpdateWindow(g_gui.hwndMain);
    }
    // If starting minimized, window stays hidden (tray icon provides access)

    // Message loop
    MSG msg = {};
    BOOL bRet;
    while ((bRet = GetMessage(&msg, nullptr, 0, 0)) != 0) {
        if (bRet == -1) break;  // Error occurred
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }

    return (int)msg.wParam;
}
