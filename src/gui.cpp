// DesktopLUT - gui.cpp
// Main GUI window and controls

#include "gui.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "color.h"
#include "osd.h"
#include "displayconfig.h"
#include "mhc.h"
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

// Forward declarations
void UpdateMhcInfoDisplay(int monitorIndex, bool isHDR);

// Forward declaration
void RecalcCorrectionsLayout(bool isHDR);

// Update color correction controls to reflect current monitor's settings
// Uses unified controls - reads from SDR or HDR settings based on toggle state
void UpdateColorCorrectionControls() {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    const auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];
    bool isHDR = g_gui.sdrHdrToggleHDR;
    const auto& cc = isHDR ? settings.hdrColorCorrection : settings.sdrColorCorrection;

    // White Point
    SendMessage(g_gui.hwndPrimariesEnable, BM_SETCHECK,
        cc.primariesEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    wchar_t buf[16];
    swprintf_s(buf, L"%.4f", cc.customPrimaries.Wx); SetWindowText(g_gui.hwndPrimariesWx, buf);
    swprintf_s(buf, L"%.4f", cc.customPrimaries.Wy); SetWindowText(g_gui.hwndPrimariesWy, buf);

    // Grayscale
    SendMessage(g_gui.hwndGrayscaleEnable, BM_SETCHECK,
        cc.grayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale10, BM_SETCHECK,
        cc.grayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale20, BM_SETCHECK,
        cc.grayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale32, BM_SETCHECK,
        cc.grayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);

    // SDR-only: 2.4 gamma checkbox
    if (!isHDR) {
        SendMessage(g_gui.hwndGrayscale24, BM_SETCHECK,
            cc.grayscale.use24Gamma ? BST_CHECKED : BST_UNCHECKED, 0);
    }

    // HDR-only: peak nits
    if (isHDR) {
        wchar_t gsPeakBuf[16];
        swprintf_s(gsPeakBuf, L"%.0f", cc.grayscale.peakNits);
        SetWindowText(g_gui.hwndGrayscalePeak, gsPeakBuf);
    }

    // Tonemapping (HDR only, controls exist regardless - just populate)
    SendMessage(g_gui.hwndTonemapEnable, BM_SETCHECK,
        cc.tonemap.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndTonemapCurve, CB_SETCURSEL,
        TonemapCurveToDropdownIndex(cc.tonemap.curve), 0);
    wchar_t tonemapBuf[16];
    swprintf_s(tonemapBuf, L"%.0f", cc.tonemap.targetPeakNits);
    SetWindowText(g_gui.hwndTonemapTarget, tonemapBuf);
    swprintf_s(tonemapBuf, L"%.0f", cc.tonemap.sourcePeakNits);
    SetWindowText(g_gui.hwndTonemapSource, tonemapBuf);
    SendMessage(g_gui.hwndTonemapDynamic, BM_SETCHECK,
        cc.tonemap.dynamicPeak ? BST_CHECKED : BST_UNCHECKED, 0);
    EnableWindow(g_gui.hwndTonemapSource, !cc.tonemap.dynamicPeak);

    // MaxTML
    SendMessage(g_gui.hwndMaxTmlEnable, BM_SETCHECK,
        settings.maxTml.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    swprintf_s(tonemapBuf, L"%.0f", settings.maxTml.peakNits);
    SetWindowText(g_gui.hwndMaxTmlEdit, tonemapBuf);
    float peakNits = settings.maxTml.peakNits;
    int comboSel = 0;
    if (peakNits == 400.0f) comboSel = 1;
    else if (peakNits == 600.0f) comboSel = 2;
    else if (peakNits == 1000.0f) comboSel = 3;
    else if (peakNits == 1400.0f) comboSel = 4;
    else if (peakNits == 4000.0f) comboSel = 5;
    else if (peakNits == 10000.0f) comboSel = 6;
    SendMessage(g_gui.hwndMaxTmlCombo, CB_SETCURSEL, comboSel, 0);

    // MHC info display (reads from toggle state)
    UpdateMhcInfoDisplay(g_gui.currentMonitor, isHDR);

    // Show/hide HDR-only and SDR-only controls + reflow
    RecalcCorrectionsLayout(isHDR);
}

// Update MHC flags on the running MonitorContext to prevent double-correction
// Called after MHC install/remove/enable toggle so shader immediately skips/restores stages
void UpdateMhcFlagsLive(int monitorIndex) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
    const auto& ms = g_gui.monitorSettings[monitorIndex];

    bool sdrMhcActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty();
    bool hdrMhcActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty();

    // Update running MonitorContext if processing is active
    // MHC has its own primaries/grayscale (Layer 1), separate from shader corrections (Layer 3)
    bool found = false;
    for (auto& ctx : g_monitors) {
        if (ctx.index == monitorIndex) {
            ctx.sdrMhcPrimariesActive = sdrMhcActive && ms.sdrMHC.primariesEnabled;
            ctx.sdrMhcGrayscaleActive = sdrMhcActive && ms.sdrMHC.grayscale.enabled;
            ctx.hdrMhcPrimariesActive = hdrMhcActive && ms.hdrMHC.primariesEnabled;
            ctx.hdrMhcGrayscaleActive = hdrMhcActive && ms.hdrMHC.grayscale.enabled;
            std::cout << "[MHC Flags] mon=" << monitorIndex
                      << " sdrPrim=" << ctx.sdrMhcPrimariesActive
                      << " sdrGs=" << ctx.sdrMhcGrayscaleActive
                      << " hdrPrim=" << ctx.hdrMhcPrimariesActive
                      << " hdrGs=" << ctx.hdrMhcGrayscaleActive << std::endl;
            found = true;
            break;
        }
    }
    if (!found) {
        std::cout << "[MHC Flags] mon=" << monitorIndex
                  << " NOT FOUND in g_monitors (size=" << g_monitors.size() << ")" << std::endl;
    }
}

// Generate, write, and install MHC2 ICC profile from current MHCSettings
// Updates mhc.enabled/profilePath/profileName on success, calls UpdateMhcFlagsLive
// Returns true if profile was generated and installed successfully
static bool GenerateAndInstallMhcProfile(int monitorIndex, bool isHDR) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
    if (!IsMHC2ApiAvailable()) return false;

    auto& settings = g_gui.monitorSettings[monitorIndex];
    auto& mhc = isHDR ? settings.hdrMHC : settings.sdrMHC;

    MHC2ProfileParams params;
    params.monitorName = (monitorIndex < (int)g_gui.monitorNames.size())
        ? g_gui.monitorNames[monitorIndex] : L"Monitor";
    params.isHDR = isHDR;

    if (mhc.primariesEnabled) {
        params.primariesEnabled = true;
        int preset = mhc.primariesPreset;
        if (preset == g_numPresetPrimaries - 1) {
            const auto& cp = mhc.customPrimaries;
            params.displayPrimaries = { cp.Rx, cp.Ry, cp.Gx, cp.Gy, cp.Bx, cp.By, cp.Wx, cp.Wy };
        } else {
            const auto& p = g_presetPrimaries[preset];
            params.displayPrimaries = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
        }
    }

    // If source file is set, re-read ICC and use per-channel TRC directly
    if (!mhc.sourceFilePath.empty()) {
        ICCProfileData icc;
        if (ReadICCProfile(mhc.sourceFilePath, icc) && icc.hasTRC) {
            params.hasPerChannelTRC = true;
            params.trcR = icc.trcR;
            params.trcG = icc.trcG;
            params.trcB = icc.trcB;
            // Grayscale from file TRC is handled by per-channel LUT generation
            params.grayscaleEnabled = true;
            params.grayscale.enabled = true;
        }
    } else if (mhc.grayscale.enabled) {
        params.grayscaleEnabled = true;
        params.grayscale.enabled = true;
        params.grayscale.pointCount = mhc.grayscale.pointCount;
        for (int i = 0; i < mhc.grayscale.pointCount && i < 32; i++) {
            params.grayscale.points[i] = (i < (int)mhc.grayscale.points.size())
                ? mhc.grayscale.points[i] : 0.0f;
        }
        params.grayscale.use24Gamma = mhc.grayscale.use24Gamma;
        params.grayscale.peakNits = mhc.grayscale.peakNits;
        params.peakNits = mhc.grayscale.peakNits;
    }

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return false;

    // Use unique filename each time to bypass Windows profile caching
    static int profileSeq = 0;
    std::wstring profileName = L"DesktopLUT_" + (isHDR ? std::wstring(L"HDR") : std::wstring(L"SDR"))
        + L"_" + std::to_wstring(GetTickCount()) + L".icm";

    // Write to temp directory - InstallColorProfileW copies to system color dir
    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + profileName;

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return false;

    // Remove old profile association and delete old file from color dir
    if (mhc.enabled && !mhc.profileName.empty()) {
        RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        // Delete old file from system color directory
        wchar_t sysDir[MAX_PATH];
        GetSystemDirectory(sysDir, MAX_PATH);
        std::wstring oldPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + mhc.profileName;
        DeleteFileW(oldPath.c_str());
    }

    if (!WriteMHC2Profile(profileData, tempPath)) return false;

    if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
        DeleteFileW(tempPath.c_str());
        return false;
    }
    DeleteFileW(tempPath.c_str());

    // Store the system color directory path
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring profilePath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    mhc.enabled = true;
    mhc.profilePath = profilePath;
    mhc.profileName = profileName;
    mhc.hasPerChannelTRC = params.hasPerChannelTRC;
    UpdateMhcFlagsLive(monitorIndex);
    return true;
}

// Auto-regenerate and reinstall MHC profile when MHC settings change
// Only acts if MHC is enabled and a profile is already installed for the given mode
void RegenerateMhcIfActive(int monitorIndex, bool isHDR) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
    auto& settings = g_gui.monitorSettings[monitorIndex];
    auto& mhc = isHDR ? settings.hdrMHC : settings.sdrMHC;
    if (!mhc.enabled || mhc.profileName.empty()) return;
    if (!IsMHC2ApiAvailable()) return;

    // Read from MHC's own primaries/grayscale (Layer 1), not shader's (Layer 3)
    MHC2ProfileParams params;
    params.monitorName = (monitorIndex < (int)g_gui.monitorNames.size())
        ? g_gui.monitorNames[monitorIndex] : L"Monitor";
    params.isHDR = isHDR;

    if (mhc.primariesEnabled) {
        params.primariesEnabled = true;
        int preset = mhc.primariesPreset;
        if (preset == g_numPresetPrimaries - 1) {
            const auto& cp = mhc.customPrimaries;
            params.displayPrimaries = { cp.Rx, cp.Ry, cp.Gx, cp.Gy, cp.Bx, cp.By, cp.Wx, cp.Wy };
        } else {
            const auto& p = g_presetPrimaries[preset];
            params.displayPrimaries = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
        }
    }

    // If source file is set, re-read ICC and use per-channel TRC directly
    if (!mhc.sourceFilePath.empty()) {
        ICCProfileData icc;
        if (ReadICCProfile(mhc.sourceFilePath, icc) && icc.hasTRC) {
            params.hasPerChannelTRC = true;
            params.trcR = icc.trcR;
            params.trcG = icc.trcG;
            params.trcB = icc.trcB;
            params.grayscaleEnabled = true;
            params.grayscale.enabled = true;
        }
    } else if (mhc.grayscale.enabled) {
        params.grayscaleEnabled = true;
        params.grayscale.enabled = true;
        params.grayscale.pointCount = mhc.grayscale.pointCount;
        for (int i = 0; i < mhc.grayscale.pointCount && i < 32; i++) {
            params.grayscale.points[i] = (i < (int)mhc.grayscale.points.size())
                ? mhc.grayscale.points[i] : 0.0f;
        }
        params.grayscale.use24Gamma = mhc.grayscale.use24Gamma;
        params.grayscale.peakNits = mhc.grayscale.peakNits;
        params.peakNits = mhc.grayscale.peakNits;
    }

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return;

    // Unique filename to bypass caching
    std::wstring newProfileName = L"DesktopLUT_" + (isHDR ? std::wstring(L"HDR") : std::wstring(L"SDR"))
        + L"_" + std::to_wstring(GetTickCount()) + L".icm";

    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + newProfileName;

    DisplayInfo displayInfo;
    if (GetDisplayInfoForMonitor(monitorIndex, displayInfo)) {
        // Remove old profile and clean up old file
        RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        wchar_t sysDir[MAX_PATH];
        GetSystemDirectory(sysDir, MAX_PATH);
        std::wstring oldPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + mhc.profileName;
        DeleteFileW(oldPath.c_str());
    }

    if (!WriteMHC2Profile(profileData, tempPath)) return;

    if (GetDisplayInfoForMonitor(monitorIndex, displayInfo)) {
        InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR);
    }
    DeleteFileW(tempPath.c_str());

    // Update stored name
    wchar_t sysDir2[MAX_PATH];
    GetSystemDirectory(sysDir2, MAX_PATH);
    mhc.profilePath = std::wstring(sysDir2) + L"\\spool\\drivers\\color\\" + newProfileName;
    mhc.profileName = newProfileName;
    mhc.hasPerChannelTRC = params.hasPerChannelTRC;
    UpdateMhcFlagsLive(monitorIndex);
}

// Update MHC info labels in the main groupbox (unified controls)
void UpdateMhcInfoDisplay(int monitorIndex, bool isHDR) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
    const auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                             : g_gui.monitorSettings[monitorIndex].sdrMHC;

    HWND hwndStatus = g_gui.hwndMhcStatus;
    HWND* coords = g_gui.hwndMhcIccCoords;

    if (!hwndStatus) return;

    bool installed = !mhc.profileName.empty();

    // Status text with indicator
    if (installed) {
        SetWindowText(hwndStatus, (L"\x25CF Active: " + mhc.profileName).c_str());
    } else {
        SetWindowText(hwndStatus, L"\x25CB Not installed");
    }

    // Show target display primaries from MHC settings (not ICC file colorants)
    // SDR ICC colorants are always sRGB by design, which is useless info
    if (installed && mhc.primariesEnabled) {
        // Resolve preset to actual primaries
        const DisplayPrimaries& p = (mhc.primariesPreset >= 0 && mhc.primariesPreset < g_numPresetPrimaries - 1)
            ? g_presetPrimaries[mhc.primariesPreset]
            : mhc.customPrimaries;
        float vals[8] = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
        for (int i = 0; i < 8; i++) {
            if (coords[i]) {
                wchar_t buf[16];
                swprintf_s(buf, L"%.4f", vals[i]);
                SetWindowText(coords[i], buf);
            }
        }
    } else {
        for (int i = 0; i < 8; i++)
            if (coords[i]) SetWindowText(coords[i], L"");
    }

    // Show/hide TRC indicator
    if (g_gui.hwndMhcTrcLabel) {
        ShowWindow(g_gui.hwndMhcTrcLabel,
            (installed && mhc.hasPerChannelTRC) ? SW_SHOW : SW_HIDE);
    }
}

// Helper to recalculate primaries matrix and apply live update
// isHDR parameter controls which settings struct is modified
void ApplyPrimariesChange(bool isHDR) {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                     : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;

    // White point only mode: RGB primaries match content space, only Wx/Wy differs
    // This produces a pure Bradford chromatic adaptation matrix
    DisplayPrimariesData displayPrimaries;
    displayPrimaries.Rx = cc.customPrimaries.Rx;
    displayPrimaries.Ry = cc.customPrimaries.Ry;
    displayPrimaries.Gx = cc.customPrimaries.Gx;
    displayPrimaries.Gy = cc.customPrimaries.Gy;
    displayPrimaries.Bx = cc.customPrimaries.Bx;
    displayPrimaries.By = cc.customPrimaries.By;
    displayPrimaries.Wx = cc.customPrimaries.Wx;
    displayPrimaries.Wy = cc.customPrimaries.Wy;

    // sRGB primaries (source content space)
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

// Draw the SDR/HDR pill toggle switch (owner-draw button)
void DrawToggleSwitch(LPDRAWITEMSTRUCT pDIS) {
    HDC hdc = pDIS->hDC;
    RECT rc = pDIS->rcItem;
    int w = rc.right - rc.left;
    int h = rc.bottom - rc.top;
    bool isHDR = g_gui.sdrHdrToggleHDR;

    // Double-buffer to prevent flicker
    HDC memDC = CreateCompatibleDC(hdc);
    HBITMAP memBmp = CreateCompatibleBitmap(hdc, w, h);
    HBITMAP oldBmp = (HBITMAP)SelectObject(memDC, memBmp);

    // Colors
    COLORREF accentColor = RGB(0, 120, 215);   // Windows accent blue
    COLORREF inactiveColor = RGB(200, 200, 200);
    COLORREF activeTextColor = RGB(255, 255, 255);
    COLORREF inactiveTextColor = RGB(80, 80, 80);
    COLORREF circleColor = RGB(255, 255, 255);

    // Draw pill background (two halves)
    int halfW = w / 2;
    int radius = 14;

    // Left half (SDR)
    HBRUSH leftBrush = CreateSolidBrush(isHDR ? inactiveColor : accentColor);
    HRGN leftRgn = CreateRoundRectRgn(0, 0, halfW + radius, h + 1, radius * 2, radius * 2);
    FillRgn(memDC, leftRgn, leftBrush);
    DeleteObject(leftRgn);
    DeleteObject(leftBrush);

    // Right half (HDR)
    HBRUSH rightBrush = CreateSolidBrush(isHDR ? accentColor : inactiveColor);
    HRGN rightRgn = CreateRoundRectRgn(halfW - radius, 0, w + 1, h + 1, radius * 2, radius * 2);
    FillRgn(memDC, rightRgn, rightBrush);
    DeleteObject(rightRgn);
    DeleteObject(rightBrush);

    // Draw text
    SetBkMode(memDC, TRANSPARENT);
    HFONT font = CreateFont(14, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
        CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    HFONT oldFont = (HFONT)SelectObject(memDC, font);

    RECT leftTextRC = { 0, 0, halfW, h };
    SetTextColor(memDC, isHDR ? inactiveTextColor : activeTextColor);
    DrawText(memDC, L"SDR", -1, &leftTextRC, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

    RECT rightTextRC = { halfW, 0, w, h };
    SetTextColor(memDC, isHDR ? activeTextColor : inactiveTextColor);
    DrawText(memDC, L"HDR", -1, &rightTextRC, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

    // Draw sliding circle indicator
    int circleR = 10;
    int circleX = isHDR ? (halfW + halfW / 2) : (halfW / 2);
    int circleY = h / 2;
    HBRUSH circleBrush = CreateSolidBrush(circleColor);
    HPEN noPen = CreatePen(PS_NULL, 0, 0);
    HPEN oldPen = (HPEN)SelectObject(memDC, noPen);
    HBRUSH oldBrushDC = (HBRUSH)SelectObject(memDC, circleBrush);
    Ellipse(memDC, circleX - circleR, circleY - circleR, circleX + circleR, circleY + circleR);
    SelectObject(memDC, oldBrushDC);
    SelectObject(memDC, oldPen);
    DeleteObject(circleBrush);
    DeleteObject(noPen);

    SelectObject(memDC, oldFont);
    DeleteObject(font);

    // Blit to screen
    BitBlt(hdc, 0, 0, w, h, memDC, 0, 0, SRCCOPY);
    SelectObject(memDC, oldBmp);
    DeleteObject(memBmp);
    DeleteDC(memDC);
}

// Recalculate Corrections tab layout based on SDR/HDR toggle state
// Shows/hides HDR-only and SDR-only controls, reflows positions, recalculates content height
void RecalcCorrectionsLayout(bool isHDR) {
    // Show/hide SDR-only controls
    for (HWND h : g_gui.sdrOnlyControls) {
        ShowWindow(h, isHDR ? SW_HIDE : SW_SHOW);
    }
    // Show/hide HDR-only controls
    for (HWND h : g_gui.hdrOnlyControls) {
        ShowWindow(h, isHDR ? SW_SHOW : SW_HIDE);
    }

    // Reflow: Desktop Gamma group (first 3 controls: groupbox, checkbox, whitelist button)
    // is HDR-only. When hidden in SDR, shift all subsequent controls up by its height.
    const int desktopGammaControls = 3;
    const int desktopGammaHeight = 51;  // 46px groupbox + 5px spacing
    int yShift = isHDR ? 0 : -desktopGammaHeight;

    if (g_gui.tab2BaseY.size() == g_gui.tab2OriginalY.size()) {
        for (size_t i = 0; i < g_gui.tab2OriginalY.size(); i++) {
            g_gui.tab2OriginalY[i] = g_gui.tab2BaseY[i] + ((int)i >= desktopGammaControls ? yShift : 0);
        }
    }

    // Content height based on mode
    // HDR: Desktop Gamma(51) + White Point(51) + Grayscale(80) + Tonemap(80) + MaxTML(63) + margins
    // SDR: White Point(51) + Grayscale(80) + margins (no Desktop Gamma, Tonemap, MaxTML)
    g_gui.contentHeight[2] = isHDR ? (8 + 51 + 51 + 80 + 80 + 63) : (8 + 51 + 80);

    // Reset scroll and reposition all controls
    g_gui.scrollPos[2] = 0;
    HWND panel = g_gui.hwndScrollPanel[2];

    if (panel && g_gui.tab2Controls.size() == g_gui.tab2OriginalY.size()) {
        ShowWindow(panel, SW_HIDE);
        for (size_t i = 0; i < g_gui.tab2Controls.size(); i++) {
            RECT rc;
            GetWindowRect(g_gui.tab2Controls[i], &rc);
            POINT pt = { rc.left, rc.top };
            ScreenToClient(panel, &pt);
            int w = rc.right - rc.left;
            int h = rc.bottom - rc.top;
            SetWindowPos(g_gui.tab2Controls[i], nullptr, pt.x, g_gui.tab2OriginalY[i], w, h,
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW);
        }
        ShowWindow(panel, SW_SHOW);
    }

    // Update scroll info
    int maxScroll = max(0, g_gui.contentHeight[2] - g_gui.panelHeight);
    SCROLLINFO si = {};
    si.cbSize = sizeof(si);
    si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
    si.nMin = 0;
    si.nMax = g_gui.contentHeight[2];
    si.nPage = g_gui.panelHeight;
    si.nPos = 0;
    SetScrollInfo(panel, SB_VERT, &si, TRUE);
    ShowScrollBar(panel, SB_VERT, maxScroll > 0);
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
                    if (data->liveUpdateCallback) {
                        data->liveUpdateCallback();
                    } else if (g_gui.currentMonitor >= 0) {
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
                    if (data->liveUpdateCallback) {
                        data->liveUpdateCallback();
                    } else if (g_gui.currentMonitor >= 0) {
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
                if (data->liveUpdateCallback) {
                    data->liveUpdateCallback();
                } else if (g_gui.currentMonitor >= 0) {
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

void ShowGrayscaleEditor(HWND hwndParent, GrayscaleSettings& settings, bool isHDR,
                         std::function<void()> liveUpdateCallback) {
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
    data.liveUpdateCallback = liveUpdateCallback;
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

// ============================================================================
// MHC Settings Dialog
// ============================================================================

struct MhcDialogData {
    MHCSettings* settings;  // Pointer to the MHCSettings being edited
    MHCSettings backup;     // Copy for Cancel restore
    bool isHDR;
    int monitorIndex;
    // Live preview state (when processing is running)
    bool livePreview = false;       // True if previewing MHC corrections through shader
    bool hadProfile = false;        // True if ICC profile was installed before Edit
    std::wstring origProfileName;   // Original ICC profile name for Cancel restore
    std::wstring origProfilePath;   // Original ICC profile path for Cancel restore
    // Control handles
    HWND hwndPrimariesEnable, hwndPreset, hwndRx, hwndRy, hwndGx, hwndGy, hwndBx, hwndBy, hwndWx, hwndWy;
    HWND hwndGrayscaleEnable, hwndGs10, hwndGs20, hwndGs32, hwndGs24, hwndGsPeak;
    // New controls for redesigned dialog
    HWND hwndFilePath = nullptr;
    HWND hwndFileClear = nullptr;
    HWND hwndGrayscaleReset = nullptr;
    HWND hwndScrollPanel = nullptr;  // Horizontal scroll panel for trackbars
    HWND hwndDialog = nullptr;
    // File import state
    ICCProfileData loadedICC;
    bool hasLoadedICC = false;
    std::wstring loadedFilePath;
    bool loadedFileIsCube = false;
    bool fileLoaded = false;  // True when a file provides the profile data (disables manual controls)
    // Embedded grayscale trackbars
    std::vector<HWND> sliders;
    std::vector<HWND> pctLabels;
    bool updatingSliders = false;
};

static MhcDialogData* g_mhcDialog = nullptr;

// Rebuild embedded grayscale trackbars in the scroll panel
static void MhcRebuildTrackbars(MhcDialogData* d) {
    if (!d || !d->hwndScrollPanel) return;

    // Destroy existing trackbars and labels
    for (HWND h : d->sliders) if (h) DestroyWindow(h);
    for (HWND h : d->pctLabels) if (h) DestroyWindow(h);
    d->sliders.clear();
    d->pctLabels.clear();

    auto& gs = d->settings->grayscale;
    if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
        gs.points.resize(gs.pointCount);
        if (d->isHDR) gs.initLinearPQ();
        else gs.initLinear();
    }

    int N = gs.pointCount;
    int sliderW = 32, sliderH = 120, pad = 2;
    int pctLabelH = 14;
    int startX = 10, startY = 4;
    int totalW = startX + N * (sliderW + pad) + 10;

    HFONT font = (HFONT)SendMessage(d->hwndDialog, WM_GETFONT, 0, 0);
    if (!font) font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);

    HFONT smallFont = CreateFont(12, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
        CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");

    int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;

    for (int i = 0; i < N; i++) {
        int x = startX + i * (sliderW + pad);

        // Input % label above slider
        wchar_t pctBuf[8];
        if (d->isHDR) {
            float pctVal = (float)(i + 1) / (float)N * 100.0f;
            swprintf_s(pctBuf, L"%.0f%%", pctVal);
        } else {
            float t = (float)i / (float)(N - 1);
            float inputPct = t * t * 100.0f;
            swprintf_s(pctBuf, L"%.0f%%", inputPct);
        }
        HWND label = CreateWindow(L"STATIC", pctBuf, WS_CHILD | WS_VISIBLE | SS_CENTER,
            x, startY, sliderW, pctLabelH, d->hwndScrollPanel, nullptr, nullptr, nullptr);
        SendMessage(label, WM_SETFONT, (WPARAM)smallFont, TRUE);
        d->pctLabels.push_back(label);

        // Vertical trackbar
        HWND slider = CreateWindow(TRACKBAR_CLASS, nullptr,
            WS_CHILD | WS_VISIBLE | TBS_VERT | TBS_NOTICKS,
            x, startY + pctLabelH, sliderW, sliderH,
            d->hwndScrollPanel, (HMENU)(INT_PTR)(ID_GRAYSCALE_SLIDER_BASE + i),
            nullptr, nullptr);
        SendMessage(slider, TBM_SETRANGE, TRUE, MAKELONG(-maxRange, maxRange));

        // Calculate deviation from linear
        float expected;
        if (d->isHDR) {
            expected = (float)i / (float)(N - 1);
        } else {
            float t = (float)i / (float)(N - 1);
            expected = t * t;
        }
        float actual = (i < (int)gs.points.size()) ? gs.points[i] : expected;
        float deviation = 0.0f;
        if (expected > 0.001f) {
            deviation = (actual - expected) / expected * 100.0f;
        }
        int sliderPos = (int)(-deviation * GRAYSCALE_SLIDER_SCALE);
        sliderPos = (std::max)(-maxRange, (std::min)(maxRange, sliderPos));
        SendMessage(slider, TBM_SETPOS, TRUE, sliderPos);
        d->sliders.push_back(slider);
    }

    // Update scroll panel content width
    SCROLLINFO si = {};
    si.cbSize = sizeof(si);
    si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
    si.nMin = 0;
    si.nMax = totalW;
    RECT rc;
    GetClientRect(d->hwndScrollPanel, &rc);
    si.nPage = rc.right;
    si.nPos = 0;
    SetScrollInfo(d->hwndScrollPanel, SB_HORZ, &si, TRUE);
    ShowScrollBar(d->hwndScrollPanel, SB_HORZ, totalW > rc.right);
}

// Read slider positions back into grayscale points
static void MhcReadSlidersToPoints(MhcDialogData* d) {
    if (!d || d->updatingSliders) return;
    auto& gs = d->settings->grayscale;
    int N = gs.pointCount;
    for (int i = 0; i < N && i < (int)d->sliders.size(); i++) {
        int pos = (int)SendMessage(d->sliders[i], TBM_GETPOS, 0, 0);
        float deviation = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;

        float expected;
        if (d->isHDR) {
            expected = (float)i / (float)(N - 1);
        } else {
            float t = (float)i / (float)(N - 1);
            expected = t * t;
        }
        float actual = expected * (1.0f + deviation / 100.0f);
        if (i < (int)gs.points.size()) {
            gs.points[i] = std::clamp(actual, 0.0f, 1.0f);
        }
    }
}

// Horizontal scroll panel for embedded trackbars
static LRESULT CALLBACK MhcScrollPanelProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_HSCROLL: {
        SCROLLINFO si = {};
        si.cbSize = sizeof(si);
        si.fMask = SIF_ALL;
        GetScrollInfo(hwnd, SB_HORZ, &si);

        int oldPos = si.nPos;
        int newPos = oldPos;
        switch (LOWORD(wParam)) {
        case SB_LINELEFT:     newPos -= 20; break;
        case SB_LINERIGHT:    newPos += 20; break;
        case SB_PAGELEFT:     newPos -= si.nPage; break;
        case SB_PAGERIGHT:    newPos += si.nPage; break;
        case SB_THUMBTRACK:   newPos = si.nTrackPos; break;
        case SB_THUMBPOSITION: newPos = si.nTrackPos; break;
        }

        int maxPos = (std::max)(0, si.nMax - (int)si.nPage);
        newPos = (std::max)(0, (std::min)(newPos, maxPos));

        if (newPos != oldPos) {
            int delta = newPos - oldPos;
            si.fMask = SIF_POS;
            si.nPos = newPos;
            SetScrollInfo(hwnd, SB_HORZ, &si, TRUE);
            ScrollWindowEx(hwnd, -delta, 0, nullptr, nullptr, nullptr, nullptr,
                SW_SCROLLCHILDREN | SW_INVALIDATE | SW_ERASE);
        }
        return 0;
    }

    case WM_VSCROLL:
        // Forward vertical trackbar notifications to parent dialog for live preview
        SendMessage(GetParent(hwnd), WM_HSCROLL, wParam, lParam);
        return 0;

    case WM_MOUSEWHEEL: {
        int delta = GET_WHEEL_DELTA_WPARAM(wParam);
        SendMessage(hwnd, WM_HSCROLL, delta > 0 ? SB_LINELEFT : SB_LINERIGHT, 0);
        for (int i = 1; i < 3; i++)
            SendMessage(hwnd, WM_HSCROLL, delta > 0 ? SB_LINELEFT : SB_LINERIGHT, 0);
        return 0;
    }

    case WM_ERASEBKGND: {
        HDC hdc = (HDC)wParam;
        RECT rc;
        GetClientRect(hwnd, &rc);
        FillRect(hdc, &rc, GetSysColorBrush(COLOR_BTNFACE));
        return 1;
    }

    case WM_CTLCOLORSTATIC: {
        HDC hdc = (HDC)wParam;
        SetBkColor(hdc, GetSysColor(COLOR_BTNFACE));
        return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
    }
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

static void MhcUpdatePrimariesFields(MhcDialogData* d) {
    int sel = (int)SendMessage(d->hwndPreset, CB_GETCURSEL, 0, 0);
    bool custom = (sel == g_numPresetPrimaries - 1) && !d->fileLoaded;
    EnableWindow(d->hwndRx, custom); EnableWindow(d->hwndRy, custom);
    EnableWindow(d->hwndGx, custom); EnableWindow(d->hwndGy, custom);
    EnableWindow(d->hwndBx, custom); EnableWindow(d->hwndBy, custom);
    EnableWindow(d->hwndWx, custom); EnableWindow(d->hwndWy, custom);

    wchar_t buf[16];
    float Rx, Ry, Gx, Gy, Bx, By, Wx, Wy;
    if (custom) {
        const auto& cp = d->settings->customPrimaries;
        Rx = cp.Rx; Ry = cp.Ry; Gx = cp.Gx; Gy = cp.Gy;
        Bx = cp.Bx; By = cp.By; Wx = cp.Wx; Wy = cp.Wy;
    } else {
        const auto& p = g_presetPrimaries[sel];
        Rx = p.Rx; Ry = p.Ry; Gx = p.Gx; Gy = p.Gy;
        Bx = p.Bx; By = p.By; Wx = p.Wx; Wy = p.Wy;
    }
    swprintf_s(buf, L"%.4f", Rx); SetWindowText(d->hwndRx, buf);
    swprintf_s(buf, L"%.4f", Ry); SetWindowText(d->hwndRy, buf);
    swprintf_s(buf, L"%.4f", Gx); SetWindowText(d->hwndGx, buf);
    swprintf_s(buf, L"%.4f", Gy); SetWindowText(d->hwndGy, buf);
    swprintf_s(buf, L"%.4f", Bx); SetWindowText(d->hwndBx, buf);
    swprintf_s(buf, L"%.4f", By); SetWindowText(d->hwndBy, buf);
    swprintf_s(buf, L"%.4f", Wx); SetWindowText(d->hwndWx, buf);
    swprintf_s(buf, L"%.4f", Wy); SetWindowText(d->hwndWy, buf);
}

static void MhcSaveCustomFromFields(MhcDialogData* d) {
    wchar_t buf[16];
    auto& cp = d->settings->customPrimaries;
    GetWindowText(d->hwndRx, buf, 16); cp.Rx = (float)_wtof(buf);
    GetWindowText(d->hwndRy, buf, 16); cp.Ry = (float)_wtof(buf);
    GetWindowText(d->hwndGx, buf, 16); cp.Gx = (float)_wtof(buf);
    GetWindowText(d->hwndGy, buf, 16); cp.Gy = (float)_wtof(buf);
    GetWindowText(d->hwndBx, buf, 16); cp.Bx = (float)_wtof(buf);
    GetWindowText(d->hwndBy, buf, 16); cp.By = (float)_wtof(buf);
    GetWindowText(d->hwndWx, buf, 16); cp.Wx = (float)_wtof(buf);
    GetWindowText(d->hwndWy, buf, 16); cp.Wy = (float)_wtof(buf);
}

// Enable/disable manual controls based on whether a file provides the profile data
static void MhcSetFileLoadedState(MhcDialogData* d, bool loaded) {
    d->fileLoaded = loaded;
    BOOL enable = loaded ? FALSE : TRUE;
    // Primaries controls
    if (d->hwndPreset) EnableWindow(d->hwndPreset, enable);
    // Coordinate fields are already managed by MhcUpdatePrimariesFields for custom vs preset,
    // but when file is loaded, all should be disabled
    EnableWindow(d->hwndRx, enable); EnableWindow(d->hwndRy, enable);
    EnableWindow(d->hwndGx, enable); EnableWindow(d->hwndGy, enable);
    EnableWindow(d->hwndBx, enable); EnableWindow(d->hwndBy, enable);
    EnableWindow(d->hwndWx, enable); EnableWindow(d->hwndWy, enable);
    // Gamma controls
    if (d->hwndGs10) EnableWindow(d->hwndGs10, enable);
    if (d->hwndGs20) EnableWindow(d->hwndGs20, enable);
    if (d->hwndGs32) EnableWindow(d->hwndGs32, enable);
    if (d->hwndGsPeak) EnableWindow(d->hwndGsPeak, enable);
    if (d->hwndGrayscaleReset) EnableWindow(d->hwndGrayscaleReset, enable);
    // Detect button
    // Find Detect button by control ID
    HWND hwndDetect = GetDlgItem(d->hwndDialog, ID_MHC_PRIMARIES_DETECT);
    if (hwndDetect) EnableWindow(hwndDetect, enable);
    // Edit Points button - also disable when file loaded
    HWND hwndEdit = GetDlgItem(d->hwndDialog, ID_MHC_GRAYSCALE_EDIT);
    if (hwndEdit) EnableWindow(hwndEdit, enable);
}

// Push current MHC settings as temporary shader corrections for live preview
static void MhcPushLivePreview(MhcDialogData* d) {
    if (!d || !d->livePreview) return;
    // Skip preview when a file (cube/ICC) is loaded - manual controls are locked
    // and the extracted data isn't suitable for direct shader preview
    if (d->fileLoaded) return;

    // Check if the current display mode matches the edit mode.
    // SDR corrections don't work in the HDR pipeline and vice versa.
    bool displayIsHDR = false;
    for (const auto& ctx : g_monitors) {
        if (ctx.index == d->monitorIndex) {
            displayIsHDR = ctx.isHDREnabled;
            break;
        }
    }
    if (displayIsHDR != d->isHDR) return;  // Mode mismatch, skip preview

    // Save custom primaries from edit boxes
    if (d->settings->primariesPreset == g_numPresetPrimaries - 1)
        MhcSaveCustomFromFields(d);

    // Build temporary ColorCorrectionSettings from MHC settings
    ColorCorrectionSettings tempCC;
    tempCC.primariesEnabled = d->settings->primariesEnabled;
    tempCC.primariesPreset = d->settings->primariesPreset;
    tempCC.customPrimaries = d->settings->customPrimaries;
    tempCC.grayscale.enabled = d->settings->grayscale.enabled;
    tempCC.grayscale.pointCount = d->settings->grayscale.pointCount;
    tempCC.grayscale.points = d->settings->grayscale.points;
    tempCC.grayscale.peakNits = d->settings->grayscale.peakNits;
    tempCC.grayscale.use24Gamma = d->settings->grayscale.use24Gamma;

    ColorCorrectionData data = ConvertColorCorrection(tempCC, d->isHDR);

    std::cout << "[MHC Preview] PUSH: mon=" << d->monitorIndex << " isHDR=" << d->isHDR
              << " primEnabled=" << data.primariesEnabled
              << " gsEnabled=" << data.grayscale.enabled
              << " gsPts=" << data.grayscale.pointCount
              << std::endl;

    // Push to render thread (single slot matching display mode)
    std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
    g_pendingColorCorrections.erase(
        std::remove_if(g_pendingColorCorrections.begin(), g_pendingColorCorrections.end(),
            [&](const PendingColorCorrection& p) {
                return p.monitorIndex == d->monitorIndex;
            }),
        g_pendingColorCorrections.end());
    g_pendingColorCorrections.push_back({ d->monitorIndex, d->isHDR, data, true });
    g_hasPendingColorCorrections.store(true, std::memory_order_release);
}

static LRESULT CALLBACK MhcDialogProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* d = g_mhcDialog;
    switch (msg) {
    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_MHC_PRIMARIES_ENABLE:
            if (d->hwndPrimariesEnable)
                d->settings->primariesEnabled = (SendMessage(d->hwndPrimariesEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_PRIMARIES_PRESET:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int oldPreset = d->settings->primariesPreset;
                if (oldPreset == g_numPresetPrimaries - 1) MhcSaveCustomFromFields(d);
                d->settings->primariesPreset = (int)SendMessage(d->hwndPreset, CB_GETCURSEL, 0, 0);
                MhcUpdatePrimariesFields(d);
                MhcPushLivePreview(d);
            }
            return 0;

        case ID_MHC_PRIMARIES_DETECT: {
            MonitorPrimaries primaries = GetMonitorPrimariesFromEDID(d->monitorIndex);
            if (!primaries.valid) primaries = GetMonitorPrimaries(d->monitorIndex);
            if (primaries.valid) {
                d->settings->primariesPreset = g_numPresetPrimaries - 1;
                SendMessage(d->hwndPreset, CB_SETCURSEL, d->settings->primariesPreset, 0);
                auto& cp = d->settings->customPrimaries;
                cp.Rx = primaries.Rx; cp.Ry = primaries.Ry;
                cp.Gx = primaries.Gx; cp.Gy = primaries.Gy;
                cp.Bx = primaries.Bx; cp.By = primaries.By;
                cp.Wx = 0.3127f; cp.Wy = 0.3290f;  // D65 white point
                d->settings->primariesEnabled = true;
                if (d->hwndPrimariesEnable)
                    SendMessage(d->hwndPrimariesEnable, BM_SETCHECK, BST_CHECKED, 0);
                MhcUpdatePrimariesFields(d);
                MhcPushLivePreview(d);
            } else {
                MessageBox(hwnd, L"Could not detect monitor primaries.\nEDID data may not be available.",
                    L"Detection Failed", MB_OK | MB_ICONWARNING);
            }
            return 0;
        }

        // ID_MHC_PRIMARIES_EXTRACT removed - file import auto-populates

        case ID_MHC_GRAYSCALE_ENABLE:
            if (d->hwndGrayscaleEnable)
                d->settings->grayscale.enabled = (SendMessage(d->hwndGrayscaleEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_GRAYSCALE_10:
        case ID_MHC_GRAYSCALE_20:
        case ID_MHC_GRAYSCALE_32: {
            int newCount = (LOWORD(wParam) == ID_MHC_GRAYSCALE_10) ? 10 :
                           (LOWORD(wParam) == ID_MHC_GRAYSCALE_20) ? 20 : 32;
            if (newCount != d->settings->grayscale.pointCount) {
                d->settings->grayscale.pointCount = newCount;
                d->settings->grayscale.points.resize(newCount);
                if (d->isHDR) d->settings->grayscale.initLinearPQ();
                else d->settings->grayscale.initLinear();
                MhcPushLivePreview(d);
            }
            return 0;
        }

        case ID_MHC_GRAYSCALE_RESET:
            if (d->isHDR) d->settings->grayscale.initLinearPQ();
            else d->settings->grayscale.initLinear();
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_GRAYSCALE_EDIT: {
            // Read HDR peak before opening editor
            if (d->isHDR && d->hwndGsPeak) {
                wchar_t buf[16];
                GetWindowText(d->hwndGsPeak, buf, 16);
                d->settings->grayscale.peakNits = (float)_wtof(buf);
                if (d->settings->grayscale.peakNits < 100.0f) d->settings->grayscale.peakNits = 100.0f;
            }
            // Ensure points are initialized
            auto& gs = d->settings->grayscale;
            if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                gs.points.resize(gs.pointCount);
                if (d->isHDR) gs.initLinearPQ(); else gs.initLinear();
            }
            // Open popup grayscale editor with MHC live preview callback
            auto* dCapture = d;
            ShowGrayscaleEditor(hwnd, gs, d->isHDR,
                [dCapture]() { MhcPushLivePreview(dCapture); });
            // After editor closes, push final state
            MhcPushLivePreview(d);
            return 0;
        }

        // ID_MHC_GRAYSCALE_EXTRACT removed - file import auto-populates

        case ID_MHC_GRAYSCALE_24:
            if (d->hwndGs24)
                d->settings->grayscale.use24Gamma = (SendMessage(d->hwndGs24, BM_GETCHECK, 0, 0) == BST_CHECKED);
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_FILE_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            OPENFILENAME ofn = { sizeof(ofn) };
            ofn.hwndOwner = hwnd;
            ofn.lpstrFilter = L"ICC/Cube Files\0*.icm;*.icc;*.cube\0ICC Profiles\0*.icm;*.icc\0Cube LUTs\0*.cube\0All Files\0*.*\0";
            ofn.lpstrFile = path;
            ofn.nMaxFile = MAX_PATH;
            ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
            if (GetOpenFileName(&ofn)) {
                SetWindowText(d->hwndFilePath, path);
                d->loadedFilePath = path;

                // Determine file type and parse
                std::wstring ext = path;
                size_t dot = ext.find_last_of(L'.');
                if (dot != std::wstring::npos) ext = ext.substr(dot);
                for (auto& c : ext) c = towlower(c);

                d->hasLoadedICC = false;
                d->loadedFileIsCube = false;
                d->loadedICC = {};

                if (ext == L".icm" || ext == L".icc") {
                    if (ReadICCProfile(path, d->loadedICC)) {
                        d->hasLoadedICC = true;
                    }
                } else if (ext == L".cube") {
                    d->loadedFileIsCube = true;
                }

                // Auto-populate primaries from loaded file
                if (d->hasLoadedICC && d->loadedICC.hasPrimaries) {
                    d->settings->primariesPreset = g_numPresetPrimaries - 1;
                    SendMessage(d->hwndPreset, CB_SETCURSEL, d->settings->primariesPreset, 0);
                    auto& cp = d->settings->customPrimaries;
                    cp.Rx = d->loadedICC.primaries.Rx; cp.Ry = d->loadedICC.primaries.Ry;
                    cp.Gx = d->loadedICC.primaries.Gx; cp.Gy = d->loadedICC.primaries.Gy;
                    cp.Bx = d->loadedICC.primaries.Bx; cp.By = d->loadedICC.primaries.By;
                    cp.Wx = d->loadedICC.primaries.Wx; cp.Wy = d->loadedICC.primaries.Wy;
                    d->settings->primariesEnabled = true;
                    MhcUpdatePrimariesFields(d);
                }

                // Auto-populate grayscale from loaded file
                bool gsExtracted = false;
                if (d->hasLoadedICC && d->loadedICC.hasTRC && !d->loadedFileIsCube) {
                    gsExtracted = ExtractGrayscaleFromICC(d->loadedICC, d->settings->grayscale, d->isHDR);
                } else if (d->loadedFileIsCube) {
                    gsExtracted = ExtractGrayscaleFromCube(d->loadedFilePath, d->settings->grayscale);
                }
                if (gsExtracted) {
                    d->settings->grayscale.enabled = true;
                }

                // Lock manual controls - file provides the profile data
                // No live preview for file-loaded state (Apply generates the ICC profile)
                MhcSetFileLoadedState(d, true);
                if (d->hwndFileClear) ShowWindow(d->hwndFileClear, SW_SHOW);
            }
            return 0;
        }

        case ID_MHC_FILE_CLEAR: {
            // Clear loaded file and unlock manual controls
            SetWindowText(d->hwndFilePath, L"");
            d->loadedFilePath.clear();
            d->hasLoadedICC = false;
            d->loadedFileIsCube = false;
            d->loadedICC = {};
            MhcSetFileLoadedState(d, false);
            if (d->hwndFileClear) ShowWindow(d->hwndFileClear, SW_HIDE);
            // Reset grayscale to linear (file extraction data is no longer valid)
            if (d->isHDR)
                d->settings->grayscale.initLinearPQ();
            else
                d->settings->grayscale.initLinear();
            // Re-enable coordinate fields based on preset selection
            MhcUpdatePrimariesFields(d);
            MhcPushLivePreview(d);
            return 0;
        }

        case ID_MHC_OK:
            // Save custom primaries from fields before closing
            if (d->settings->primariesPreset == g_numPresetPrimaries - 1)
                MhcSaveCustomFromFields(d);
            // Save grayscale peak if HDR
            if (d->isHDR && d->hwndGsPeak) {
                wchar_t buf[16];
                GetWindowText(d->hwndGsPeak, buf, 16);
                d->settings->grayscale.peakNits = (float)_wtof(buf);
                if (d->settings->grayscale.peakNits < 100.0f) d->settings->grayscale.peakNits = 100.0f;
            }
            // Store source file path for per-channel TRC regeneration
            if (d->fileLoaded && !d->loadedFilePath.empty()) {
                d->settings->sourceFilePath = d->loadedFilePath;
            } else {
                d->settings->sourceFilePath.clear();
                d->settings->hasPerChannelTRC = false;
            }
            // When live previewing, generate + install ICC profile to bake settings
            if (d->livePreview) {
                if (!GenerateAndInstallMhcProfile(d->monitorIndex, d->isHDR)) {
                    MessageBox(hwnd, L"Failed to generate or install MHC2 profile.",
                        L"Error", MB_OK | MB_ICONERROR);
                }
            }
            DestroyWindow(hwnd);
            return 0;

        case ID_MHC_CANCEL:
            *d->settings = d->backup;
            // Restore original ICC profile if one was installed before Edit
            if (d->livePreview && d->hadProfile) {
                d->settings->enabled = true;
                d->settings->profileName = d->origProfileName;
                d->settings->profilePath = d->origProfilePath;
                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(d->monitorIndex, displayInfo)) {
                    ReassociateMHC2Profile(d->origProfileName, displayInfo.adapterId, displayInfo.sourceId, d->isHDR);
                }
                UpdateMhcFlagsLive(d->monitorIndex);
            }
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    // WM_HSCROLL removed - no embedded trackbars, grayscale uses popup editor

    case WM_CLOSE:
        *d->settings = d->backup;
        // Restore original ICC profile if one was installed before Edit
        if (d->livePreview && d->hadProfile) {
            d->settings->enabled = true;
            d->settings->profileName = d->origProfileName;
            d->settings->profilePath = d->origProfilePath;
            DisplayInfo displayInfo;
            if (GetDisplayInfoForMonitor(d->monitorIndex, displayInfo)) {
                ReassociateMHC2Profile(d->origProfileName, displayInfo.adapterId, displayInfo.sourceId, d->isHDR);
            }
            UpdateMhcFlagsLive(d->monitorIndex);
        }
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        g_mhcDialog = nullptr;
        return 0;

    case WM_DRAWITEM: {
        LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
        if (pDIS->CtlType == ODT_BUTTON) {
            DrawRoundedButton(pDIS);
            return TRUE;
        }
        break;
    }

    case WM_CTLCOLORSTATIC:
    case WM_CTLCOLORBTN:
        SetBkColor((HDC)wParam, GetSysColor(COLOR_BTNFACE));
        return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void ShowMhcSettingsDialog(HWND hwndParent, MHCSettings& settings, bool isHDR, int monitorIndex,
                           bool livePreview = false, bool hadProfile = false,
                           const std::wstring& origProfileName = L"",
                           const std::wstring& origProfilePath = L"") {
    static bool mhcRegistered = false;
    if (!mhcRegistered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = MhcDialogProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = GetSysColorBrush(COLOR_BTNFACE);
        wc.lpszClassName = L"DesktopLUT_MHC";
        RegisterClassEx(&wc);
        mhcRegistered = true;
    }

    static bool scrollRegistered = false;
    if (!scrollRegistered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = MhcScrollPanelProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = GetSysColorBrush(COLOR_BTNFACE);
        wc.lpszClassName = L"DesktopLUT_MHC_Scroll";
        RegisterClassEx(&wc);
        scrollRegistered = true;
    }

    MhcDialogData data = {};
    data.settings = &settings;
    data.backup = settings;
    data.isHDR = isHDR;
    data.monitorIndex = monitorIndex;
    data.livePreview = livePreview;
    data.hadProfile = hadProfile;
    data.origProfileName = origProfileName;
    data.origProfilePath = origProfilePath;
    g_mhcDialog = &data;

    // Dialog size: 460 wide
    int dlgW = 460, dlgH = isHDR ? 355 : 330;
    RECT adjustRect = { 0, 0, dlgW, dlgH };
    AdjustWindowRectEx(&adjustRect, WS_POPUP | WS_CAPTION | WS_SYSMENU, FALSE, WS_EX_DLGMODALFRAME);
    int winW = adjustRect.right - adjustRect.left;
    int winH = adjustRect.bottom - adjustRect.top;

    RECT parentRect;
    GetWindowRect(hwndParent, &parentRect);
    int x = parentRect.left + (parentRect.right - parentRect.left - winW) / 2;
    int y = parentRect.top + (parentRect.bottom - parentRect.top - winH) / 2;

    HWND dlg = CreateWindowEx(WS_EX_DLGMODALFRAME,
        L"DesktopLUT_MHC", isHDR ? L"MHC Settings (HDR)" : L"MHC Settings (SDR)",
        WS_POPUP | WS_CAPTION | WS_SYSMENU,
        x, y, winW, winH, hwndParent, nullptr, GetModuleHandle(nullptr), nullptr);
    data.hwndDialog = dlg;

    int cx = 10, cy = 8, h = 20, w = dlgW - 20;
    HFONT font = (HFONT)SendMessage(hwndParent, WM_GETFONT, 0, 0);
    if (!font) font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);

    auto makeCtrl = [&](const wchar_t* cls, const wchar_t* text, DWORD style, int px, int py, int pw, int ph, int id) -> HWND {
        HWND h = CreateWindow(cls, text, WS_CHILD | WS_VISIBLE | style, px, py, pw, ph, dlg, (HMENU)(INT_PTR)id, nullptr, nullptr);
        SendMessage(h, WM_SETFONT, (WPARAM)font, TRUE);
        return h;
    };

    // === File Import Bar ===
    makeCtrl(L"STATIC", L"File:", 0, cx, cy + 2, 28, h, 0);
    data.hwndFilePath = makeCtrl(L"EDIT", L"", ES_AUTOHSCROLL | ES_READONLY | WS_BORDER,
        cx + 30, cy, w - 140, h, ID_MHC_FILE_PATH);
    makeCtrl(L"BUTTON", L"Browse", BS_OWNERDRAW, cx + w - 105, cy, 55, h, ID_MHC_FILE_BROWSE);
    data.hwndFileClear = makeCtrl(L"BUTTON", L"Clear", BS_OWNERDRAW, cx + w - 45, cy, 45, h, ID_MHC_FILE_CLEAR);
    ShowWindow(data.hwndFileClear, SW_HIDE);  // Hidden until file loaded

    // === Display Primaries Section ===
    cy += h + 8;
    makeCtrl(L"BUTTON", L"Display Primaries", BS_GROUPBOX, cx, cy, w, 110, 0);

    // Always enabled when editing - no checkbox needed
    data.hwndPrimariesEnable = nullptr;
    settings.primariesEnabled = true;

    makeCtrl(L"STATIC", L"Preset:", 0, cx + 10, cy + 20, 40, h, 0);
    data.hwndPreset = makeCtrl(L"COMBOBOX", nullptr, CBS_DROPDOWNLIST | WS_VSCROLL,
        cx + 55, cy + 18, 150, 150, ID_MHC_PRIMARIES_PRESET);
    for (int i = 0; i < g_numPresetPrimaries; i++)
        SendMessage(data.hwndPreset, CB_ADDSTRING, 0, (LPARAM)g_presetPrimaries[i].name);
    SendMessage(data.hwndPreset, CB_SETCURSEL, settings.primariesPreset, 0);

    makeCtrl(L"BUTTON", L"Detect", BS_OWNERDRAW, cx + 210, cy + 18, 55, h, ID_MHC_PRIMARIES_DETECT);

    // Coordinate fields
    int chromY = cy + 48, chromX = cx + 10, chromW = 52, chromLW = 18;
    makeCtrl(L"STATIC", L"R:", 0, chromX, chromY + 2, chromLW, h, 0);
    data.hwndRx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_RX);
    data.hwndRy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_RY);
    makeCtrl(L"STATIC", L"G:", 0, chromX + 160, chromY + 2, chromLW, h, 0);
    data.hwndGx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_GX);
    data.hwndGy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_GY);

    chromY += h + 4;
    makeCtrl(L"STATIC", L"B:", 0, chromX, chromY + 2, chromLW, h, 0);
    data.hwndBx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_BX);
    data.hwndBy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_BY);
    makeCtrl(L"STATIC", L"W:", 0, chromX + 160, chromY + 2, chromLW, h, 0);
    data.hwndWx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_WX);
    data.hwndWy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_WY);

    MhcUpdatePrimariesFields(&data);

    // === Gamma Section ===
    cy += 118;
    int gsGroupH = isHDR ? 95 : 72;
    makeCtrl(L"BUTTON", L"Gamma", BS_GROUPBOX, cx, cy, w, gsGroupH, 0);

    // Always enabled when editing
    data.hwndGrayscaleEnable = nullptr;
    data.hwndGs24 = nullptr;
    data.hwndGsPeak = nullptr;
    data.hwndScrollPanel = nullptr;
    settings.grayscale.enabled = true;

    // HDR: Peak nits on first row
    int gsRowY = cy + 18;
    if (isHDR) {
        makeCtrl(L"STATIC", L"Peak:", 0, cx + 10, gsRowY + 2, 30, h, 0);
        data.hwndGsPeak = makeCtrl(L"EDIT", L"", WS_BORDER | ES_NUMBER,
            cx + 42, gsRowY, 50, h, ID_MHC_GRAYSCALE_PEAK);
        wchar_t peakBuf[16];
        swprintf_s(peakBuf, L"%.0f", settings.grayscale.peakNits);
        SetWindowText(data.hwndGsPeak, peakBuf);
        makeCtrl(L"STATIC", L"nits", 0, cx + 95, gsRowY + 2, 25, h, 0);
        gsRowY += h + 4;
    }

    // Row 1: Radio buttons for point count (matching corrections tab)
    int rbX = cx + 10;
    data.hwndGs10 = makeCtrl(L"BUTTON", L"10", BS_AUTORADIOBUTTON | WS_GROUP,
        rbX, gsRowY, 40, h, ID_MHC_GRAYSCALE_10);
    data.hwndGs20 = makeCtrl(L"BUTTON", L"20", BS_AUTORADIOBUTTON,
        rbX + 45, gsRowY, 40, h, ID_MHC_GRAYSCALE_20);
    data.hwndGs32 = makeCtrl(L"BUTTON", L"32", BS_AUTORADIOBUTTON,
        rbX + 90, gsRowY, 40, h, ID_MHC_GRAYSCALE_32);

    // Select current point count
    int pc = settings.grayscale.pointCount;
    SendMessage(data.hwndGs10, BM_SETCHECK, pc == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(data.hwndGs20, BM_SETCHECK, pc == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(data.hwndGs32, BM_SETCHECK, pc == 32 ? BST_CHECKED : BST_UNCHECKED, 0);

    // Row 2: Edit Points... + Reset buttons (below radio buttons)
    gsRowY += h + 5;
    makeCtrl(L"BUTTON", L"Edit Points...", BS_OWNERDRAW, rbX, gsRowY, 90, h, ID_MHC_GRAYSCALE_EDIT);
    data.hwndGrayscaleReset = makeCtrl(L"BUTTON", L"Reset", BS_OWNERDRAW, rbX + 100, gsRowY, 60, h, ID_MHC_GRAYSCALE_RESET);

    // Restore file-loaded state if settings have a source file
    if (!settings.sourceFilePath.empty()) {
        SetWindowText(data.hwndFilePath, settings.sourceFilePath.c_str());
        data.loadedFilePath = settings.sourceFilePath;
        // Try to re-read ICC data
        std::wstring ext = settings.sourceFilePath;
        size_t dot = ext.find_last_of(L'.');
        if (dot != std::wstring::npos) ext = ext.substr(dot);
        for (auto& c : ext) c = towlower(c);
        if (ext == L".icm" || ext == L".icc") {
            if (ReadICCProfile(settings.sourceFilePath, data.loadedICC))
                data.hasLoadedICC = true;
        } else if (ext == L".cube") {
            data.loadedFileIsCube = true;
        }
        MhcSetFileLoadedState(&data, true);
        ShowWindow(data.hwndFileClear, SW_SHOW);
    }

    // Push initial live preview if processing is running
    if (livePreview) {
        MhcPushLivePreview(&data);
    }

    // === Apply / Cancel Buttons ===
    cy += gsGroupH + 8;
    int btnW = 70, btnH = 26;
    makeCtrl(L"BUTTON", L"Apply", BS_OWNERDRAW, dlgW / 2 - btnW - 10, cy, btnW, btnH, ID_MHC_OK);
    makeCtrl(L"BUTTON", L"Cancel", BS_OWNERDRAW, dlgW / 2 + 10, cy, btnW, btnH, ID_MHC_CANCEL);

    // Show as modal
    EnableWindow(hwndParent, FALSE);
    ShowWindow(dlg, SW_SHOW);
    UpdateWindow(dlg);

    MSG winMsg;
    while (GetMessage(&winMsg, nullptr, 0, 0)) {
        if (!IsWindow(dlg)) break;
        // Enter: push live preview (confirm text box edits) or apply if not previewing
        if (winMsg.message == WM_KEYDOWN && winMsg.wParam == VK_RETURN) {
            if (data.livePreview) {
                MhcPushLivePreview(&data);
            } else {
                SendMessage(dlg, WM_COMMAND, ID_MHC_OK, 0);
            }
            continue;
        }
        // Esc: cancel dialog
        if (winMsg.message == WM_KEYDOWN && winMsg.wParam == VK_ESCAPE) {
            SendMessage(dlg, WM_COMMAND, ID_MHC_CANCEL, 0);
            continue;
        }
        TranslateMessage(&winMsg);
        DispatchMessage(&winMsg);
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
        if (!isTabControl) for (HWND h : g_gui.tab3Controls) { if (h == hCtrl) { isTabControl = true; break; } }

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
        // SDR/HDR toggle gets special drawing
        if (pDIS->CtlID == ID_SDR_HDR_TOGGLE) {
            DrawToggleSwitch(pDIS);
            return TRUE;
        }
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

        // SDR/HDR pill toggle switch (owner-draw button between monitor list and tabs)
        int toggleW = 120, toggleH = 28;
        int toggleX = margin + (contentW - toggleW) / 2;
        g_gui.hwndToggle = CreateWindow(L"BUTTON", L"",
            WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            toggleX, y, toggleW, toggleH, hwnd, (HMENU)ID_SDR_HDR_TOGGLE, nullptr, nullptr);
        y += toggleH + pad;

        // Tab control (fill remaining space)
        int tabH = tabBottom - y - 28;  // Subtract tab header height
        g_gui.hwndTab = CreateWindow(WC_TABCONTROL, nullptr,
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            margin, y, contentW, tabH + 28,
            hwnd, (HMENU)ID_TAB_CONTROL, nullptr, nullptr);

        // Subclass tab control for custom background color
        g_origTabProc = (WNDPROC)SetWindowLongPtr(g_gui.hwndTab, GWLP_WNDPROC, (LONG_PTR)TabSubclassProc);

        // Add tabs: MHC / 3D LUT / Corrections / Settings
        TCITEM tie = { TCIF_TEXT };
        tie.pszText = (LPWSTR)L"MHC";
        TabCtrl_InsertItem(g_gui.hwndTab, 0, &tie);
        tie.pszText = (LPWSTR)L"3D LUT";
        TabCtrl_InsertItem(g_gui.hwndTab, 1, &tie);
        tie.pszText = (LPWSTR)L"Corrections";
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

        // === TAB 0: Display Calibration ===
        HWND ctrl;
        HWND panel0 = g_gui.hwndScrollPanel[0];

        // MHC groupbox
        ctrl = CreateWindow(L"BUTTON", L"Display Calibration", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 115, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        // Row 1: Apply, Remove, Edit buttons + status indicator
        g_gui.hwndMhcApply = CreateWindow(L"BUTTON", L"Apply",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 18, 55, h, panel0, (HMENU)ID_MHC_TAB_APPLY, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcApply);

        g_gui.hwndMhcRemove = CreateWindow(L"BUTTON", L"Remove",
            WS_CHILD | BS_OWNERDRAW, innerX + 70, innerY + 18, 55, h, panel0, (HMENU)ID_MHC_TAB_REMOVE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcRemove);

        g_gui.hwndMhcEdit = CreateWindow(L"BUTTON", L"Edit",
            WS_CHILD | BS_OWNERDRAW, innerX + 130, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_TAB_EDIT, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcEdit);

        g_gui.hwndMhcStatus = CreateWindow(L"STATIC", L"\x25CB Not installed", WS_CHILD,
            innerX + 180, innerY + 20, groupW - 190, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcStatus);

        // Row 2-3: Display target RGBW coordinates (disabled read-only boxes)
        {
            int cY = innerY + 50;
            int cX = innerX + 10;
            int cW = 50;
            int cLabelW = 25;
            DWORD editStyle = WS_CHILD | WS_BORDER | ES_READONLY | ES_CENTER | WS_DISABLED;

            ctrl = CreateWindow(L"STATIC", L"R:", WS_CHILD, cX, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            g_gui.hwndMhcIccCoords[0] = CreateWindow(L"EDIT", L"", editStyle,
                cX + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[0]);
            g_gui.hwndMhcIccCoords[1] = CreateWindow(L"EDIT", L"", editStyle,
                cX + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[1]);

            ctrl = CreateWindow(L"STATIC", L"G:", WS_CHILD, cX + 140, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            g_gui.hwndMhcIccCoords[2] = CreateWindow(L"EDIT", L"", editStyle,
                cX + 140 + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[2]);
            g_gui.hwndMhcIccCoords[3] = CreateWindow(L"EDIT", L"", editStyle,
                cX + 140 + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[3]);

            cY += h + 4;
            ctrl = CreateWindow(L"STATIC", L"B:", WS_CHILD, cX, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            g_gui.hwndMhcIccCoords[4] = CreateWindow(L"EDIT", L"", editStyle,
                cX + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[4]);
            g_gui.hwndMhcIccCoords[5] = CreateWindow(L"EDIT", L"", editStyle,
                cX + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[5]);

            ctrl = CreateWindow(L"STATIC", L"W:", WS_CHILD, cX + 140, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            g_gui.hwndMhcIccCoords[6] = CreateWindow(L"EDIT", L"", editStyle,
                cX + 140 + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[6]);
            g_gui.hwndMhcIccCoords[7] = CreateWindow(L"EDIT", L"", editStyle,
                cX + 140 + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcIccCoords[7]);

            // TRC indicator label (shown to the right of coordinates when per-channel TRC active)
            g_gui.hwndMhcTrcLabel = CreateWindow(L"STATIC", L"+ TRC", WS_CHILD,
                cX + 140 + cLabelW + cW + 5 + cW + 10, cY + 3, 40, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(g_gui.hwndMhcTrcLabel);
        }

        g_gui.contentHeight[0] = innerY + 115 + 8;

        // === TAB 1: 3D LUT (file selection) ===
        innerY = 8;
        HWND panel1 = g_gui.hwndScrollPanel[1];

        // LUT path edit width calculation
        int pathEditW = groupW - labelW - 3 * pad - 2 * btnW;

        // SDR LUT
        ctrl = CreateWindow(L"STATIC", L"SDR LUT:", WS_CHILD | WS_VISIBLE,
            innerX, innerY + 2, labelW, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        g_gui.hwndSdrPath = CreateWindow(L"EDIT", L"",
            WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL | ES_READONLY,
            innerX + labelW + pad, innerY, pathEditW, h, panel1, (HMENU)ID_SDR_PATH, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndSdrPath);
        ctrl = CreateWindow(L"BUTTON", L"Browse", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad, innerY, btnW, h, panel1, (HMENU)ID_SDR_BROWSE, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        ctrl = CreateWindow(L"BUTTON", L"Clear", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad + btnW + pad, innerY, btnW, h, panel1, (HMENU)ID_SDR_CLEAR, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        innerY += h + pad;

        // HDR LUT
        ctrl = CreateWindow(L"STATIC", L"HDR LUT:", WS_CHILD | WS_VISIBLE,
            innerX, innerY + 2, labelW, h, panel1, nullptr, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        g_gui.hwndHdrPath = CreateWindow(L"EDIT", L"",
            WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL | ES_READONLY,
            innerX + labelW + pad, innerY, pathEditW, h, panel1, (HMENU)ID_HDR_PATH, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndHdrPath);
        ctrl = CreateWindow(L"BUTTON", L"Browse", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad, innerY, btnW, h, panel1, (HMENU)ID_HDR_BROWSE, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        ctrl = CreateWindow(L"BUTTON", L"Clear", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + labelW + pad + pathEditW + pad + btnW + pad, innerY, btnW, h, panel1, (HMENU)ID_HDR_CLEAR, nullptr, nullptr);
        g_gui.tab1Controls.push_back(ctrl);
        innerY += h + pad;

        // Tetrahedral interpolation checkbox
        g_gui.hwndTetrahedralCheck = CreateWindow(L"BUTTON", L"Tetrahedral LUT interpolation",
            WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX,
            innerX + labelW + pad, innerY, 250, h, panel1, (HMENU)ID_TETRAHEDRAL_CHECK, nullptr, nullptr);
        g_gui.tab1Controls.push_back(g_gui.hwndTetrahedralCheck);
        SendMessage(g_gui.hwndTetrahedralCheck, BM_SETCHECK, g_tetrahedralInterp ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.contentHeight[1] = innerY + h + 8;

        // === TAB 2: Corrections (unified SDR/HDR - repopulated on toggle) ===
        innerY = 8;
        HWND panel2 = g_gui.hwndScrollPanel[2];
        int chromW = 50;

        // Desktop Gamma groupbox (HDR only - hidden in SDR mode)
        ctrl = CreateWindow(L"BUTTON", L"Desktop Gamma", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 46, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hdrOnlyControls.push_back(ctrl);

        g_gui.hwndGammaCheck = CreateWindow(L"BUTTON", L"sRGB\x2192""2.2 Gamma (HDR only)",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 200, h, panel2, (HMENU)ID_GAMMA_CHECK, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGammaCheck);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndGammaCheck);
        SendMessage(g_gui.hwndGammaCheck, BM_SETCHECK, g_desktopGammaMode ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.hwndGammaWhitelistBtn = CreateWindow(L"BUTTON", L"Whitelist...",
            WS_CHILD | BS_OWNERDRAW,
            innerX + 220, innerY + 18, 70, h, panel2, (HMENU)ID_GAMMA_WHITELIST_BTN, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGammaWhitelistBtn);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndGammaWhitelistBtn);

        // White Point Correction (simplified from full primaries - uses Bradford adaptation)
        innerY += 51;
        ctrl = CreateWindow(L"BUTTON", L"White Point", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 46, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndPrimariesEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 60, h, panel2, (HMENU)ID_CORR_PRIMARIES_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndPrimariesEnable);

        ctrl = CreateWindow(L"STATIC", L"x:", WS_CHILD,
            innerX + 80, innerY + 22, 14, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndPrimariesWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            innerX + 94, innerY + 20, chromW, h, panel2, (HMENU)ID_CORR_PRIMARIES_WX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndPrimariesWx);

        ctrl = CreateWindow(L"STATIC", L"y:", WS_CHILD,
            innerX + 150, innerY + 22, 14, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndPrimariesWy = CreateWindow(L"EDIT", L"0.3290", WS_CHILD | WS_BORDER,
            innerX + 164, innerY + 20, chromW, h, panel2, (HMENU)ID_CORR_PRIMARIES_WY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndPrimariesWy);

        SetNumericEdit(g_gui.hwndPrimariesWx, 4);
        SetNumericEdit(g_gui.hwndPrimariesWy, 4);

        // Grayscale group (unified, repopulated on toggle)
        innerY += 51;
        ctrl = CreateWindow(L"BUTTON", L"Grayscale", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndGrayscaleEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_CORR_GRAYSCALE_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscaleEnable);

        ctrl = CreateWindow(L"STATIC", L"Points:", WS_CHILD, innerX + 80, innerY + 20, 45, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndGrayscale10 = CreateWindow(L"BUTTON", L"10", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
            innerX + 130, innerY + 18, 40, h, panel2, (HMENU)ID_CORR_GRAYSCALE_10, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscale10);
        g_gui.hwndGrayscale20 = CreateWindow(L"BUTTON", L"20", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 175, innerY + 18, 40, h, panel2, (HMENU)ID_CORR_GRAYSCALE_20, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscale20);
        g_gui.hwndGrayscale32 = CreateWindow(L"BUTTON", L"32", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 220, innerY + 18, 40, h, panel2, (HMENU)ID_CORR_GRAYSCALE_32, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscale32);
        SendMessage(g_gui.hwndGrayscale20, BM_SETCHECK, BST_CHECKED, 0);  // Default to 20

        g_gui.hwndGrayscaleEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel2, (HMENU)ID_CORR_GRAYSCALE_EDIT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscaleEdit);
        g_gui.hwndGrayscaleReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel2, (HMENU)ID_CORR_GRAYSCALE_RESET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscaleReset);

        // SDR only: 2.4 Gamma checkbox (after Edit/Reset buttons)
        g_gui.hwndGrayscale24 = CreateWindow(L"BUTTON", L"2.4 Gamma (BT.1886)",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 180, innerY + 47, 150, h, panel2, (HMENU)ID_CORR_GRAYSCALE_24, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscale24);
        g_gui.sdrOnlyControls.push_back(g_gui.hwndGrayscale24);

        // HDR only: Peak nits label + edit
        g_gui.hwndGrayscalePeakLabel = CreateWindow(L"STATIC", L"Peak:", WS_CHILD,
            innerX + 180, innerY + 47, 35, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscalePeakLabel);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndGrayscalePeakLabel);

        g_gui.hwndGrayscalePeak = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 215, innerY + 45, 45, h, panel2, (HMENU)ID_CORR_GRAYSCALE_PEAK, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscalePeak);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndGrayscalePeak);

        // Tonemapping group (HDR only)
        innerY += 80;
        g_gui.hwndTonemapGroup = CreateWindow(L"BUTTON", L"Tonemapping", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapGroup);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndTonemapGroup);

        g_gui.hwndTonemapEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_CORR_TONEMAP_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapEnable);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndTonemapEnable);

        ctrl = CreateWindow(L"STATIC", L"Curve:", WS_CHILD, innerX + 80, innerY + 20, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hdrOnlyControls.push_back(ctrl);

        g_gui.hwndTonemapCurve = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 120, innerY + 18, 95, 120, panel2, (HMENU)ID_CORR_TONEMAP_CURVE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapCurve);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndTonemapCurve);
        // Order: BT2390, BT2446A, Reinhard, SoftClip, HardClip (matches g_tonemapDropdownOrder)
        SendMessage(g_gui.hwndTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"BT.2390");
        SendMessage(g_gui.hwndTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"BT.2446A");
        SendMessage(g_gui.hwndTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"Reinhard");
        SendMessage(g_gui.hwndTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"Soft Clip");
        SendMessage(g_gui.hwndTonemapCurve, CB_ADDSTRING, 0, (LPARAM)L"Hard Clip");
        SendMessage(g_gui.hwndTonemapCurve, CB_SETCURSEL, 0, 0);

        // Target and Source peak inputs
        int tonemapY = innerY + 45;
        ctrl = CreateWindow(L"STATIC", L"Target:", WS_CHILD, innerX + 10, tonemapY + 2, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hdrOnlyControls.push_back(ctrl);
        g_gui.hwndTonemapTarget = CreateWindow(L"EDIT", L"1000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 50, tonemapY, 45, h, panel2, (HMENU)ID_CORR_TONEMAP_TARGET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapTarget);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndTonemapTarget);

        ctrl = CreateWindow(L"STATIC", L"Source:", WS_CHILD, innerX + 100, tonemapY + 2, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hdrOnlyControls.push_back(ctrl);
        g_gui.hwndTonemapSource = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 140, tonemapY, 50, h, panel2, (HMENU)ID_CORR_TONEMAP_SOURCE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapSource);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndTonemapSource);

        ctrl = CreateWindow(L"STATIC", L"nits", WS_CHILD, innerX + 192, tonemapY + 2, 25, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hdrOnlyControls.push_back(ctrl);

        // Dynamic peak detection checkbox (far right)
        g_gui.hwndTonemapDynamic = CreateWindow(L"BUTTON", L"Dynamic",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 220, tonemapY, 70, h, panel2, (HMENU)ID_CORR_TONEMAP_DYNAMIC, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapDynamic);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndTonemapDynamic);

        // MaxTML group (Windows HDR peak luminance override, HDR only)
        innerY += 80;
        g_gui.hwndMaxTmlGroup = CreateWindow(L"BUTTON", L"Display Peak Override (MaxTML)", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 55, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlGroup);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndMaxTmlGroup);

        g_gui.hwndMaxTmlEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 55, h, panel2, (HMENU)ID_CORR_MAXTML_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlEnable);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndMaxTmlEnable);

        g_gui.hwndMaxTmlCombo = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 70, innerY + 18, 85, 150, panel2, (HMENU)ID_CORR_MAXTML_COMBO, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlCombo);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndMaxTmlCombo);
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"Custom");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"400 nits");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"600 nits");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"1000 nits");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"1400 nits");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"4000 nits");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_ADDSTRING, 0, (LPARAM)L"10000 nits");
        SendMessage(g_gui.hwndMaxTmlCombo, CB_SETCURSEL, 3, 0);  // Default to 1000

        g_gui.hwndMaxTmlEdit = CreateWindow(L"EDIT", L"1000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 160, innerY + 18, 50, h, panel2, (HMENU)ID_CORR_MAXTML_EDIT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlEdit);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndMaxTmlEdit);

        ctrl = CreateWindow(L"STATIC", L"nits", WS_CHILD, innerX + 212, innerY + 20, 25, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hdrOnlyControls.push_back(ctrl);

        g_gui.hwndMaxTmlApply = CreateWindow(L"BUTTON", L"Apply",
            WS_CHILD | BS_OWNERDRAW, innerX + 245, innerY + 17, 45, h + 2, panel2, (HMENU)ID_CORR_MAXTML_APPLY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlApply);
        g_gui.hdrOnlyControls.push_back(g_gui.hwndMaxTmlApply);

        g_gui.contentHeight[2] = innerY + 60 + 8;  // Track content height (will be recalculated by RecalcCorrectionsLayout)

        // Apply Enter key handling to numeric edit boxes
        SetNumericEdit(g_gui.hwndGrayscalePeak, 0);
        SetNumericEdit(g_gui.hwndTonemapTarget, 0);
        SetNumericEdit(g_gui.hwndTonemapSource, 0);
        SetNumericEdit(g_gui.hwndMaxTmlEdit, 0);

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
        g_gui.tab2BaseY = g_gui.tab2OriginalY;  // Immutable copy for reflow calculations
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

        // SDR/HDR Toggle switch
        case ID_SDR_HDR_TOGGLE:
            g_gui.sdrHdrToggleHDR = !g_gui.sdrHdrToggleHDR;
            InvalidateRect(g_gui.hwndToggle, nullptr, FALSE);
            UpdateColorCorrectionControls();
            return 0;

        // Unified Corrections tab controls (read toggle state to determine SDR/HDR)
        case ID_CORR_PRIMARIES_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
                cc.primariesEnabled = (SendMessage(g_gui.hwndPrimariesEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                // Auto-fill RGB primaries with content space defaults (only white point is user-adjustable)
                cc.primariesPreset = g_numPresetPrimaries - 1;  // Custom
                const auto& defaults = isHDR ? g_presetPrimaries[3] : g_presetPrimaries[0];  // Rec.2020 or sRGB
                cc.customPrimaries.Rx = defaults.Rx; cc.customPrimaries.Ry = defaults.Ry;
                cc.customPrimaries.Gx = defaults.Gx; cc.customPrimaries.Gy = defaults.Gy;
                cc.customPrimaries.Bx = defaults.Bx; cc.customPrimaries.By = defaults.By;
                ApplyPrimariesChange(isHDR);
                SaveSettings();
            }
            return 0;

        case ID_CORR_PRIMARIES_WX: case ID_CORR_PRIMARIES_WY:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    bool isHDR = g_gui.sdrHdrToggleHDR;
                    auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                                     : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndPrimariesWx, buf, 16); cc.customPrimaries.Wx = (float)_wtof(buf);
                    GetWindowText(g_gui.hwndPrimariesWy, buf, 16); cc.customPrimaries.Wy = (float)_wtof(buf);
                    // Ensure RGB primaries are set to content space defaults
                    cc.primariesPreset = g_numPresetPrimaries - 1;
                    const auto& defaults = isHDR ? g_presetPrimaries[3] : g_presetPrimaries[0];
                    cc.customPrimaries.Rx = defaults.Rx; cc.customPrimaries.Ry = defaults.Ry;
                    cc.customPrimaries.Gx = defaults.Gx; cc.customPrimaries.Gy = defaults.Gy;
                    cc.customPrimaries.Bx = defaults.Bx; cc.customPrimaries.By = defaults.By;
                    ApplyPrimariesChange(isHDR);
                    SaveSettings();
                }
            }
            return 0;

        case ID_CORR_GRAYSCALE_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                gs.enabled = (SendMessage(g_gui.hwndGrayscaleEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
                }
                SaveSettings();
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_GRAYSCALE_10:
        case ID_CORR_GRAYSCALE_20:
        case ID_CORR_GRAYSCALE_32:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                int newCount = (LOWORD(wParam) == ID_CORR_GRAYSCALE_10) ? 10 :
                               (LOWORD(wParam) == ID_CORR_GRAYSCALE_20) ? 20 : 32;
                if (newCount != gs.pointCount) {
                    gs.pointCount = newCount;
                    gs.points.resize(newCount);
                    if (isHDR) gs.initLinearPQ(); else gs.initLinear();
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
                    }
                    UpdateGUIState();
                }
            }
            return 0;

        case ID_CORR_GRAYSCALE_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                    gs.points.resize(gs.pointCount);
                    if (isHDR) gs.initLinearPQ(); else gs.initLinear();
                }
                ShowGrayscaleEditor(hwnd, gs, isHDR);
            }
            return 0;

        case ID_CORR_GRAYSCALE_RESET:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                if (isHDR) gs.initLinearPQ(); else gs.initLinear();
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_GRAYSCALE_24:  // SDR only: 2.4 gamma checkbox
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale.use24Gamma =
                    (SendMessage(g_gui.hwndGrayscale24, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, false);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_GRAYSCALE_PEAK:  // HDR only: peak nits
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndGrayscalePeak, buf, 16);
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

        // Tonemapping controls (HDR only, but unified IDs)
        case ID_CORR_TONEMAP_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndTonemapEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.enabled = enabled;
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                }
                SaveSettings();
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_TONEMAP_CURVE:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    int sel = (int)SendMessage(g_gui.hwndTonemapCurve, CB_GETCURSEL, 0, 0);
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.curve =
                        DropdownIndexToTonemapCurve(sel);
                    if (g_gui.isRunning) {
                        UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                    }
                    SaveSettings();
                }
            }
            return 0;

        case ID_CORR_TONEMAP_TARGET:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& tm = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndTonemapTarget, buf, 16);
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

        case ID_CORR_TONEMAP_DYNAMIC:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndTonemapDynamic, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap.dynamicPeak = enabled;
                EnableWindow(g_gui.hwndTonemapSource, !enabled);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, true);
                }
                SaveSettings();
            }
            return 0;

        case ID_CORR_TONEMAP_SOURCE:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    auto& tm = g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.tonemap;
                    wchar_t buf[16];
                    GetWindowText(g_gui.hwndTonemapSource, buf, 16);
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

        // MaxTML controls (HDR only)
        case ID_CORR_MAXTML_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool enabled = (SendMessage(g_gui.hwndMaxTmlEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].maxTml.enabled = enabled;
                SaveSettings();
            }
            return 0;

        case ID_CORR_MAXTML_COMBO:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int sel = (int)SendMessage(g_gui.hwndMaxTmlCombo, CB_GETCURSEL, 0, 0);
                const wchar_t* values[] = { L"", L"400", L"600", L"1000", L"1400", L"4000", L"10000" };
                const float nitsValues[] = { 0, 400, 600, 1000, 1400, 4000, 10000 };
                if (sel > 0 && sel < 7) {
                    SetWindowText(g_gui.hwndMaxTmlEdit, values[sel]);
                    if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                        g_gui.monitorSettings[g_gui.currentMonitor].maxTml.peakNits = nitsValues[sel];
                        SaveSettings();
                    }
                }
            }
            return 0;

        case ID_CORR_MAXTML_APPLY:
            {
                wchar_t buf[16];
                GetWindowText(g_gui.hwndMaxTmlEdit, buf, 16);
                float nits = (float)_wtof(buf);
                if (nits < 100.0f) nits = 100.0f;
                if (nits > 10000.0f) nits = 10000.0f;

                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].maxTml.peakNits = nits;
                    SaveSettings();
                }

                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(g_gui.currentMonitor, displayInfo)) {
                    if (SetDisplayMaxTml(displayInfo, nits)) {
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

        // MHC Hardware Calibration controls (unified, use toggle state)
        case ID_MHC_TAB_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                int monIdx = g_gui.currentMonitor;
                bool isHDR = g_gui.sdrHdrToggleHDR;
                auto& mhc = isHDR ? g_gui.monitorSettings[monIdx].hdrMHC
                                   : g_gui.monitorSettings[monIdx].sdrMHC;
                auto& otherMhc = isHDR ? g_gui.monitorSettings[monIdx].sdrMHC
                                        : g_gui.monitorSettings[monIdx].hdrMHC;

                // Save original ICC state for live preview restore
                bool hadProfile = mhc.enabled && !mhc.profileName.empty();
                std::wstring origProfileName = mhc.profileName;
                std::wstring origProfilePath = mhc.profilePath;

                // Remove ICC profile before starting preview
                if (hadProfile) {
                    DisplayInfo displayInfo;
                    if (GetDisplayInfoForMonitor(monIdx, displayInfo)) {
                        RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
                    }
                }

                // Clear BOTH modes' MHC profile state before StartProcessing so the processing
                // thread initialization sees no active profiles and sets all MHC flags to false.
                // Save and restore the non-edited mode's state after the dialog closes.
                bool otherWasEnabled = otherMhc.enabled;
                std::wstring otherProfileName = otherMhc.profileName;
                mhc.enabled = false;
                mhc.profileName.clear();
                mhc.profilePath.clear();
                otherMhc.enabled = false;
                otherMhc.profileName.clear();

                // Ensure overlay is running for this monitor to enable live preview
                // If not running, temporarily start processing with passthrough for this monitor
                bool startedForPreview = false;
                if (!g_gui.isRunning) {
                    auto& ms = g_gui.monitorSettings[monIdx];
                    auto& cc = isHDR ? ms.hdrColorCorrection : ms.sdrColorCorrection;
                    bool origPrimEnabled = cc.primariesEnabled;
                    cc.primariesEnabled = true;  // Ensure this monitor is included in processing
                    StartProcessing();
                    cc.primariesEnabled = origPrimEnabled;  // Restore (processing thread has its own copy)
                    if (g_gui.isRunning) startedForPreview = true;
                }

                bool livePreview = g_gui.isRunning;

                // Force-clear MHC flags on existing MonitorContext (for already-running case).
                // MhcPushLivePreview also sets clearMhcFlags on each push for ongoing protection.
                if (livePreview) {
                    for (auto& ctx : g_monitors) {
                        if (ctx.index == monIdx) {
                            ctx.sdrMhcPrimariesActive = false;
                            ctx.sdrMhcGrayscaleActive = false;
                            ctx.hdrMhcPrimariesActive = false;
                            ctx.hdrMhcGrayscaleActive = false;
                            break;
                        }
                    }
                }

                ShowMhcSettingsDialog(hwnd, mhc, isHDR, monIdx,
                                      livePreview, hadProfile, origProfileName, origProfilePath);

                // Restore non-edited mode's MHC state
                otherMhc.enabled = otherWasEnabled;
                otherMhc.profileName = otherProfileName;

                // After dialog closes, restore MHC flags and shader corrections
                if (livePreview) {
                    UpdateMhcFlagsLive(monIdx);
                    if (!startedForPreview) {
                        UpdateColorCorrectionLive(monIdx, isHDR);
                    }
                }
                // Stop temporary processing if we started it for preview
                if (startedForPreview) {
                    StopProcessing();
                }
                SaveSettings();
                UpdateMhcInfoDisplay(monIdx, isHDR);
            }
            return 0;

        case ID_MHC_TAB_APPLY:
            {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size())
                    return 0;

                if (!IsMHC2ApiAvailable()) {
                    MessageBox(hwnd, L"MHC2 color management APIs not available.\nRequires Windows 10 21H2 or later.", L"Not Available", MB_OK | MB_ICONWARNING);
                    return 0;
                }

                if (GenerateAndInstallMhcProfile(g_gui.currentMonitor, isHDR)) {
                    UpdateMhcInfoDisplay(g_gui.currentMonitor, isHDR);
                    SaveSettings();
                    MessageBox(hwnd, L"MHC2 profile installed successfully.\nProfile persists even when overlay is off.",
                        L"DesktopLUT", MB_OK | MB_ICONINFORMATION);
                } else {
                    MessageBox(hwnd, L"Failed to install MHC2 profile.\nCheck that HDR is enabled and GPU supports MHC2.",
                        L"Error", MB_OK | MB_ICONERROR);
                }
            }
            return 0;

        case ID_MHC_TAB_REMOVE:
            {
                bool isHDR = g_gui.sdrHdrToggleHDR;
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size())
                    return 0;

                auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];
                auto& mhc = isHDR ? settings.hdrMHC : settings.sdrMHC;

                if (mhc.profileName.empty()) {
                    MessageBox(hwnd, L"No MHC2 profile is installed for this monitor.", L"Info", MB_OK | MB_ICONINFORMATION);
                    return 0;
                }

                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(g_gui.currentMonitor, displayInfo)) {
                    RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
                }

                mhc.enabled = false;
                mhc.profilePath.clear();
                mhc.profileName.clear();
                mhc.hasPerChannelTRC = false;
                UpdateMhcInfoDisplay(g_gui.currentMonitor, isHDR);
                UpdateMhcFlagsLive(g_gui.currentMonitor);
                SaveSettings();
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
