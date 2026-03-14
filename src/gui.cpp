// DesktopLUT - gui.cpp
// Main GUI window and controls

#include "gui.h"
#include "gui_shared.h"
#include "gui_mhc.h"
#include "gui_whitelist.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "color.h"
#include "osd.h"
#include "displayconfig.h"
#include "mhc.h"
#include "dwm_inject.h"
#include "../resource.h"
#include <commctrl.h>
#include <commdlg.h>
#include <algorithm>
#include <iostream>
#include <cstdio>
#include <wtsapi32.h>
#include <dbt.h>

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "Wtsapi32.lib")

// ============================================================================
// SECTION: Display Power Notification (GUI-side)
// ============================================================================

// GUI-side display power notification — fires on the main thread regardless of
// whether the processing thread is blocked in WaitForCompositorClock/DwmFlush.
// The overlay WndProc handler is defense-in-depth (only fires when PeekMessage runs).
static HPOWERNOTIFY g_guiDisplayPowerNotify = nullptr;

// GUID_CONSOLE_DISPLAY_STATE: {6FE69556-704A-47A0-8F24-C28D936FDA47}
static const GUID GUID_CONSOLE_DISPLAY_STATE_GUI =
    { 0x6fe69556, 0x704a, 0x47a0, { 0x8f, 0x24, 0xc2, 0x8d, 0x93, 0x6f, 0xda, 0x47 } };

// ============================================================================
// SECTION: Utilities
// ============================================================================

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
// Populates both SDR and HDR sections simultaneously
void UpdateColorCorrectionControls() {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }

    // Suppress repaints during bulk control update to prevent cascade (~24 controls → 1 repaint)
    HWND scrollPanel = g_gui.hwndScrollPanel[2];  // Corrections tab
    SendMessage(scrollPanel, WM_SETREDRAW, FALSE, 0);

    const auto& settings = g_gui.monitorSettings[g_gui.currentMonitor];
    wchar_t buf[16];

    // === SDR section ===
    const auto& sdrCC = settings.sdrColorCorrection;

    SendMessage(g_gui.hwndPrimariesEnable, BM_SETCHECK,
        sdrCC.primariesEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    swprintf_s(buf, L"%.4f", sdrCC.customPrimaries.Wx); SetWindowText(g_gui.hwndPrimariesWx, buf);
    swprintf_s(buf, L"%.4f", sdrCC.customPrimaries.Wy); SetWindowText(g_gui.hwndPrimariesWy, buf);

    SendMessage(g_gui.hwndGrayscaleEnable, BM_SETCHECK,
        sdrCC.grayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale10, BM_SETCHECK,
        sdrCC.grayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale20, BM_SETCHECK,
        sdrCC.grayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale32, BM_SETCHECK,
        sdrCC.grayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndGrayscale24, BM_SETCHECK,
        sdrCC.grayscale.use24Gamma ? BST_CHECKED : BST_UNCHECKED, 0);

    // === HDR section ===
    const auto& hdrCC = settings.hdrColorCorrection;

    SendMessage(g_gui.hwndHdrPrimariesEnable, BM_SETCHECK,
        hdrCC.primariesEnabled ? BST_CHECKED : BST_UNCHECKED, 0);
    swprintf_s(buf, L"%.4f", hdrCC.customPrimaries.Wx); SetWindowText(g_gui.hwndHdrPrimariesWx, buf);
    swprintf_s(buf, L"%.4f", hdrCC.customPrimaries.Wy); SetWindowText(g_gui.hwndHdrPrimariesWy, buf);

    SendMessage(g_gui.hwndHdrGrayscaleEnable, BM_SETCHECK,
        hdrCC.grayscale.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrGrayscale10, BM_SETCHECK,
        hdrCC.grayscale.pointCount == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrGrayscale20, BM_SETCHECK,
        hdrCC.grayscale.pointCount == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndHdrGrayscale32, BM_SETCHECK,
        hdrCC.grayscale.pointCount == 32 ? BST_CHECKED : BST_UNCHECKED, 0);

    wchar_t gsPeakBuf[16];
    swprintf_s(gsPeakBuf, L"%.0f", hdrCC.grayscale.peakNits);
    SetWindowText(g_gui.hwndGrayscalePeak, gsPeakBuf);

    // Tonemapping (HDR only)
    SendMessage(g_gui.hwndTonemapEnable, BM_SETCHECK,
        hdrCC.tonemap.enabled ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(g_gui.hwndTonemapCurve, CB_SETCURSEL,
        TonemapCurveToDropdownIndex(hdrCC.tonemap.curve), 0);
    wchar_t tonemapBuf[16];
    swprintf_s(tonemapBuf, L"%.0f", hdrCC.tonemap.targetPeakNits);
    SetWindowText(g_gui.hwndTonemapTarget, tonemapBuf);
    swprintf_s(tonemapBuf, L"%.0f", hdrCC.tonemap.sourcePeakNits);
    SetWindowText(g_gui.hwndTonemapSource, tonemapBuf);
    SendMessage(g_gui.hwndTonemapDynamic, BM_SETCHECK,
        hdrCC.tonemap.dynamicPeak ? BST_CHECKED : BST_UNCHECKED, 0);
    EnableWindow(g_gui.hwndTonemapSource, !hdrCC.tonemap.dynamicPeak);

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

    // MHC info display (both sections)
    UpdateMhcInfoDisplay(g_gui.currentMonitor, false);
    UpdateMhcInfoDisplay(g_gui.currentMonitor, true);

    // Re-enable repaints and trigger single
    SendMessage(scrollPanel, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(scrollPanel, nullptr, TRUE);
}


// ============================================================================
// SECTION: System Tray
// ============================================================================

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
    AppendMenu(hMenu, g_gui.isRunning ? MF_GRAYED : MF_STRING, ID_TRAY_APPLY, L"Start");
    AppendMenu(hMenu, g_gui.isRunning ? MF_STRING : MF_GRAYED, ID_TRAY_STOP, L"Stop");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, IsStartupEnabled() ? (MF_STRING | MF_CHECKED) : MF_STRING,
               ID_TRAY_STARTUP, L"Run at startup");
    AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
    AppendMenu(hMenu, MF_STRING, ID_TRAY_EXIT, L"Exit");

    SetForegroundWindow(hwnd);
    TrackPopupMenu(hMenu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, nullptr);
    DestroyMenu(hMenu);
}

// ============================================================================
// SECTION: Scroll Panels
// ============================================================================

// Monitor enumeration for GUI
static BOOL CALLBACK GUIMonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam) {
    auto* monitors = reinterpret_cast<std::vector<HMONITOR>*>(lParam);
    monitors->push_back(hMonitor);
    return TRUE;
}

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
            case 3: controls = &g_gui.tab3Controls; originalY = &g_gui.tab3OriginalY; break;
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
        // Handle mouse wheel scrolling (accumulate delta for smooth-scroll mice)
        static int wheelAccum = 0;
        int delta = GET_WHEEL_DELTA_WPARAM(wParam);
        wheelAccum += delta;
        int lines = (wheelAccum * 3) / WHEEL_DELTA;
        wheelAccum -= (lines * WHEEL_DELTA) / 3;
        for (int i = 0; i < abs(lines); i++) {
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

// ============================================================================
// SECTION: Main Window Procedure
// ============================================================================

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

        // Add tabs: I. MHC / II. 3D LUT / III. Corrections / Settings
        TCITEM tie = { TCIF_TEXT };
        tie.pszText = (LPWSTR)L"I. MHC";
        TabCtrl_InsertItem(g_gui.hwndTab, 0, &tie);
        tie.pszText = (LPWSTR)L"II. 3D LUT";
        TabCtrl_InsertItem(g_gui.hwndTab, 1, &tie);
        tie.pszText = (LPWSTR)L"III. Corrections";
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

        // === TAB 0: Display Calibration (SDR + HDR sections) ===
        HWND ctrl;
        HWND panel0 = g_gui.hwndScrollPanel[0];

        // Helper lambda to create one MHC section
        auto createMhcSection = [&](const wchar_t* title, int baseY,
            HWND& hApply, HWND& hRemove, HWND& hEdit, HWND& hStatus,
            HWND* hCoords, HWND* hMetaLabels,
            HMENU idApply, HMENU idRemove, HMENU idEdit)
        {
            ctrl = CreateWindow(L"BUTTON", title, WS_CHILD | BS_GROUPBOX,
                innerX, baseY, groupW, 115, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);

            hApply = CreateWindow(L"BUTTON", L"Apply",
                WS_CHILD | BS_OWNERDRAW, innerX + 10, baseY + 18, 55, h, panel0, idApply, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hApply);

            hRemove = CreateWindow(L"BUTTON", L"Remove",
                WS_CHILD | BS_OWNERDRAW, innerX + 70, baseY + 18, 55, h, panel0, idRemove, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hRemove);

            hEdit = CreateWindow(L"BUTTON", L"Edit",
                WS_CHILD | BS_OWNERDRAW, innerX + 130, baseY + 18, 40, h, panel0, idEdit, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hEdit);

            hStatus = CreateWindow(L"STATIC", L"\x25CB Not installed", WS_CHILD,
                innerX + 180, baseY + 20, groupW - 190, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hStatus);

            int cY = baseY + 50;
            int cX = innerX + 10;
            int cW = 50;
            int cLabelW = 25;
            int metaX = cX + 285;  // Metadata labels X position
            int metaW = groupW - 285 - 20;  // Remaining width
            DWORD editStyle = WS_CHILD | WS_BORDER | ES_READONLY | ES_CENTER | WS_DISABLED;

            ctrl = CreateWindow(L"STATIC", L"R:", WS_CHILD, cX, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            hCoords[0] = CreateWindow(L"EDIT", L"", editStyle, cX + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[0]);
            hCoords[1] = CreateWindow(L"EDIT", L"", editStyle, cX + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[1]);

            ctrl = CreateWindow(L"STATIC", L"G:", WS_CHILD, cX + 140, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            hCoords[2] = CreateWindow(L"EDIT", L"", editStyle, cX + 140 + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[2]);
            hCoords[3] = CreateWindow(L"EDIT", L"", editStyle, cX + 140 + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[3]);

            // Metadata labels: 3 lines packed within the RGBW box height
            int metaY = cY + 3;       // Top-aligned with R/G row text
            int metaLineH = 16;       // Compact line height for text-only labels
            for (int mi = 0; mi < 3; mi++) {
                hMetaLabels[mi] = CreateWindow(L"STATIC", L"", WS_CHILD,
                    metaX, metaY + mi * metaLineH, metaW, metaLineH, panel0, nullptr, nullptr, nullptr);
                g_gui.tab0Controls.push_back(hMetaLabels[mi]);
            }

            cY += h + 4;
            ctrl = CreateWindow(L"STATIC", L"B:", WS_CHILD, cX, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            hCoords[4] = CreateWindow(L"EDIT", L"", editStyle, cX + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[4]);
            hCoords[5] = CreateWindow(L"EDIT", L"", editStyle, cX + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[5]);

            ctrl = CreateWindow(L"STATIC", L"W:", WS_CHILD, cX + 140, cY + 3, cLabelW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(ctrl);
            hCoords[6] = CreateWindow(L"EDIT", L"", editStyle, cX + 140 + cLabelW, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[6]);
            hCoords[7] = CreateWindow(L"EDIT", L"", editStyle, cX + 140 + cLabelW + cW + 5, cY, cW, h, panel0, nullptr, nullptr, nullptr);
            g_gui.tab0Controls.push_back(hCoords[7]);
        };

        // SDR Display Calibration section
        createMhcSection(L"SDR Display Calibration", innerY,
            g_gui.hwndMhcApply, g_gui.hwndMhcRemove, g_gui.hwndMhcEdit, g_gui.hwndMhcStatus,
            g_gui.hwndMhcIccCoords, g_gui.hwndMhcMetaLabels,
            (HMENU)ID_MHC_TAB_APPLY, (HMENU)ID_MHC_TAB_REMOVE, (HMENU)ID_MHC_TAB_EDIT);

        innerY += 115 + 5;

        // HDR Display Calibration section
        createMhcSection(L"HDR Display Calibration", innerY,
            g_gui.hwndHdrMhcApply, g_gui.hwndHdrMhcRemove, g_gui.hwndHdrMhcEdit, g_gui.hwndHdrMhcStatus,
            g_gui.hwndHdrMhcIccCoords, g_gui.hwndHdrMhcMetaLabels,
            (HMENU)ID_MHC_HDR_APPLY, (HMENU)ID_MHC_HDR_REMOVE, (HMENU)ID_MHC_HDR_EDIT);

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

        // === TAB 2: Corrections (SDR + HDR sections shown simultaneously) ===
        innerY = 8;
        HWND panel2 = g_gui.hwndScrollPanel[2];
        int chromW = 50;

        // --- SDR Corrections section ---
        ctrl = CreateWindow(L"STATIC", L"SDR Corrections", WS_CHILD | SS_LEFT,
            innerX, innerY, groupW, 16, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        innerY += 18;

        // SDR White Point
        ctrl = CreateWindow(L"BUTTON", L"White Point", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndPrimariesEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_CORR_PRIMARIES_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndPrimariesEnable);

        ctrl = CreateWindow(L"STATIC", L"x:", WS_CHILD,
            innerX + 80, innerY + 20, 14, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndPrimariesWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            innerX + 94, innerY + 18, chromW, h, panel2, (HMENU)ID_CORR_PRIMARIES_WX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndPrimariesWx);

        ctrl = CreateWindow(L"STATIC", L"y:", WS_CHILD,
            innerX + 150, innerY + 20, 14, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndPrimariesWy = CreateWindow(L"EDIT", L"0.3290", WS_CHILD | WS_BORDER,
            innerX + 164, innerY + 18, chromW, h, panel2, (HMENU)ID_CORR_PRIMARIES_WY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndPrimariesWy);

        SetNumericEdit(g_gui.hwndPrimariesWx, 4);
        SetNumericEdit(g_gui.hwndPrimariesWy, 4);

        // SDR Grayscale
        innerY += 53;
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
        SendMessage(g_gui.hwndGrayscale20, BM_SETCHECK, BST_CHECKED, 0);

        g_gui.hwndGrayscaleEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel2, (HMENU)ID_CORR_GRAYSCALE_EDIT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscaleEdit);
        g_gui.hwndGrayscaleReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel2, (HMENU)ID_CORR_GRAYSCALE_RESET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscaleReset);

        g_gui.hwndGrayscale24 = CreateWindow(L"BUTTON", L"2.2\u21922.4 Gamma",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 180, innerY + 47, 130, h, panel2, (HMENU)ID_CORR_GRAYSCALE_24, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscale24);

        // --- HDR Corrections section ---
        innerY += 85;

        // Separator
        ctrl = CreateWindow(L"STATIC", L"HDR Corrections", WS_CHILD | SS_LEFT,
            innerX, innerY, groupW, 16, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        innerY += 18;

        // Desktop Gamma groupbox (HDR only)
        ctrl = CreateWindow(L"BUTTON", L"Desktop Gamma", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndGammaCheck = CreateWindow(L"BUTTON", L"sRGB\x2192""2.2 Gamma",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 200, h, panel2, (HMENU)ID_GAMMA_CHECK, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGammaCheck);
        SendMessage(g_gui.hwndGammaCheck, BM_SETCHECK, g_desktopGammaMode ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.hwndGammaWhitelistBtn = CreateWindow(L"BUTTON", L"Whitelist...",
            WS_CHILD | BS_OWNERDRAW,
            innerX + 220, innerY + 18, 70, h, panel2, (HMENU)ID_GAMMA_WHITELIST_BTN, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGammaWhitelistBtn);

        // HDR White Point
        innerY += 53;
        ctrl = CreateWindow(L"BUTTON", L"White Point", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrPrimariesEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_CORR_HDR_PRIMARIES_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesEnable);

        ctrl = CreateWindow(L"STATIC", L"x:", WS_CHILD,
            innerX + 80, innerY + 20, 14, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrPrimariesWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            innerX + 94, innerY + 18, chromW, h, panel2, (HMENU)ID_CORR_HDR_PRIMARIES_WX, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesWx);

        ctrl = CreateWindow(L"STATIC", L"y:", WS_CHILD,
            innerX + 150, innerY + 20, 14, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndHdrPrimariesWy = CreateWindow(L"EDIT", L"0.3290", WS_CHILD | WS_BORDER,
            innerX + 164, innerY + 18, chromW, h, panel2, (HMENU)ID_CORR_HDR_PRIMARIES_WY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrPrimariesWy);

        SetNumericEdit(g_gui.hwndHdrPrimariesWx, 4);
        SetNumericEdit(g_gui.hwndHdrPrimariesWy, 4);

        // HDR Grayscale
        innerY += 53;
        ctrl = CreateWindow(L"BUTTON", L"Grayscale", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrGrayscaleEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscaleEnable);

        ctrl = CreateWindow(L"STATIC", L"Points:", WS_CHILD, innerX + 80, innerY + 20, 45, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndHdrGrayscale10 = CreateWindow(L"BUTTON", L"10", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
            innerX + 130, innerY + 18, 40, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_10, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscale10);
        g_gui.hwndHdrGrayscale20 = CreateWindow(L"BUTTON", L"20", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 175, innerY + 18, 40, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_20, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscale20);
        g_gui.hwndHdrGrayscale32 = CreateWindow(L"BUTTON", L"32", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 220, innerY + 18, 40, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_32, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscale32);
        SendMessage(g_gui.hwndHdrGrayscale20, BM_SETCHECK, BST_CHECKED, 0);

        g_gui.hwndHdrGrayscaleEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_EDIT, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscaleEdit);
        g_gui.hwndHdrGrayscaleReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_RESET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndHdrGrayscaleReset);

        // HDR only: Peak nits label + edit
        g_gui.hwndGrayscalePeakLabel = CreateWindow(L"STATIC", L"Peak:", WS_CHILD,
            innerX + 180, innerY + 47, 35, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscalePeakLabel);

        g_gui.hwndGrayscalePeak = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 215, innerY + 45, 45, h, panel2, (HMENU)ID_CORR_HDR_GRAYSCALE_PEAK, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndGrayscalePeak);

        // Tonemapping group (HDR only)
        innerY += 80;
        ctrl = CreateWindow(L"BUTTON", L"Tonemapping", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndTonemapEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel2, (HMENU)ID_CORR_TONEMAP_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapEnable);

        ctrl = CreateWindow(L"STATIC", L"Curve:", WS_CHILD, innerX + 80, innerY + 20, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndTonemapCurve = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 120, innerY + 18, 95, 120, panel2, (HMENU)ID_CORR_TONEMAP_CURVE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapCurve);
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
        g_gui.hwndTonemapTarget = CreateWindow(L"EDIT", L"1000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 50, tonemapY, 45, h, panel2, (HMENU)ID_CORR_TONEMAP_TARGET, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapTarget);

        ctrl = CreateWindow(L"STATIC", L"Source:", WS_CHILD, innerX + 100, tonemapY + 2, 40, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);
        g_gui.hwndTonemapSource = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 140, tonemapY, 50, h, panel2, (HMENU)ID_CORR_TONEMAP_SOURCE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapSource);

        ctrl = CreateWindow(L"STATIC", L"nits", WS_CHILD, innerX + 192, tonemapY + 2, 25, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndTonemapDynamic = CreateWindow(L"BUTTON", L"Dynamic",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 220, tonemapY, 70, h, panel2, (HMENU)ID_CORR_TONEMAP_DYNAMIC, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndTonemapDynamic);

        // MaxTML group (Windows HDR peak luminance override, HDR only)
        innerY += 80;
        ctrl = CreateWindow(L"BUTTON", L"Display Peak Override (MaxTML)", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);

        g_gui.hwndMaxTmlEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 55, h, panel2, (HMENU)ID_CORR_MAXTML_ENABLE, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlEnable);


        g_gui.hwndMaxTmlCombo = CreateWindow(L"COMBOBOX", nullptr,
            WS_CHILD | CBS_DROPDOWNLIST | WS_VSCROLL,
            innerX + 70, innerY + 18, 85, 150, panel2, (HMENU)ID_CORR_MAXTML_COMBO, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlCombo);

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


        ctrl = CreateWindow(L"STATIC", L"nits", WS_CHILD, innerX + 212, innerY + 20, 25, h, panel2, nullptr, nullptr, nullptr);
        g_gui.tab2Controls.push_back(ctrl);


        g_gui.hwndMaxTmlApply = CreateWindow(L"BUTTON", L"Apply",
            WS_CHILD | BS_OWNERDRAW, innerX + 245, innerY + 18, 45, h, panel2, (HMENU)ID_CORR_MAXTML_APPLY, nullptr, nullptr);
        g_gui.tab2Controls.push_back(g_gui.hwndMaxTmlApply);


        g_gui.contentHeight[2] = innerY + 48 + 8;  // Fixed content height (both SDR + HDR sections always visible)

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
            innerX, innerY, groupW, 48, panel3, nullptr, nullptr, nullptr);
        g_gui.tab3Controls.push_back(ctrl);

        g_gui.hwndSettingsVrrWhitelistCheck = CreateWindow(L"BUTTON", L"Hide overlay for apps",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 140, h, panel3, (HMENU)ID_SETTINGS_VRR_WHITELIST_CHECK, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsVrrWhitelistCheck);
        SendMessage(g_gui.hwndSettingsVrrWhitelistCheck, BM_SETCHECK, g_vrrWhitelistEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

        g_gui.hwndSettingsVrrWhitelistBtn = CreateWindow(L"BUTTON", L"Whitelist...",
            WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
            innerX + 160, innerY + 18, 70, h, panel3, (HMENU)ID_SETTINGS_VRR_WHITELIST_BTN, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsVrrWhitelistBtn);

        // Startup group
        innerY += 53;
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

        // Experimental group
        innerY += 51;
        ctrl = CreateWindow(L"BUTTON", L"Experimental", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 46, panel3, nullptr, nullptr, nullptr);
        g_gui.tab3Controls.push_back(ctrl);

        g_gui.hwndSettingsDwmHook = CreateWindow(L"BUTTON", L"DWM Hook Mode (requires admin)",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 20, 210, h, panel3, (HMENU)ID_SETTINGS_DWM_HOOK, nullptr, nullptr);
        g_gui.tab3Controls.push_back(g_gui.hwndSettingsDwmHook);
        SendMessage(g_gui.hwndSettingsDwmHook, BM_SETCHECK, g_dwmHookMode.load() ? BST_CHECKED : BST_UNCHECKED, 0);

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
        g_gui.hwndStop = CreateWindow(L"BUTTON", L"Stop", WS_CHILD | WS_VISIBLE | WS_DISABLED | BS_OWNERDRAW,
            clientW - margin - disableW, btnY, disableW, btnH, hwnd, (HMENU)ID_STOP, nullptr, nullptr);
        g_gui.hwndApply = CreateWindow(L"BUTTON", L"Start", WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
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
            bool isPrimary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
            // Query friendly display name (e.g. "PA32UCXR") from Windows display config
            DisplayInfo dispInfo;
            std::wstring friendlyName;
            if (GetDisplayInfoForMonitor((int)i, dispInfo) && !dispInfo.name.empty()) {
                friendlyName = dispInfo.name;
            }
            wchar_t name[128];
            if (!friendlyName.empty()) {
                swprintf_s(name, L"Monitor %d - %s: %dx%d%s", (int)i, friendlyName.c_str(),
                    monW, monH, isPrimary ? L" [Primary]" : L"");
            } else {
                swprintf_s(name, L"Monitor %d: %dx%d%s", (int)i,
                    monW, monH, isPrimary ? L" [Primary]" : L"");
            }
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
        SendMessage(g_gui.hwndSettingsDwmHook, BM_SETCHECK,
            g_dwmHookMode.load() ? BST_CHECKED : BST_UNCHECKED, 0);

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

        // Register for display power state notifications on the GUI window.
        // The GUI thread's message pump is never blocked by CompClock/DwmFlush,
        // so this fires immediately when the display goes off/on.
        g_guiDisplayPowerNotify = RegisterPowerSettingNotification(
            hwnd, &GUID_CONSOLE_DISPLAY_STATE_GUI, DEVICE_NOTIFY_WINDOW_HANDLE);

        // Register for session change notifications (lock/unlock/RDP connect/disconnect)
        WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION);

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
                if (g_gui.isRunning) {
                    SaveSettings();
                    StopProcessing();
                    StartProcessing();
                } else {
                    UpdateGUIState();
                }
            }
            return 0;
        }
        case ID_SDR_CLEAR:
            SetPathText(g_gui.hwndSdrPath, L"");
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].sdrPath.clear();
            }
            if (g_gui.isRunning) {
                SaveSettings();
                StopProcessing();
                StartProcessing();
            } else {
                UpdateGUIState();
            }
            return 0;
        case ID_HDR_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            if (BrowseForLUT(hwnd, path, MAX_PATH)) {
                SetPathText(g_gui.hwndHdrPath, path);
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings[g_gui.currentMonitor].hdrPath = path;
                }
                if (g_gui.isRunning) {
                    SaveSettings();
                    StopProcessing();
                    StartProcessing();
                } else {
                    UpdateGUIState();
                }
            }
            return 0;
        }
        case ID_HDR_CLEAR:
            SetPathText(g_gui.hwndHdrPath, L"");
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                g_gui.monitorSettings[g_gui.currentMonitor].hdrPath.clear();
            }
            if (g_gui.isRunning) {
                SaveSettings();
                StopProcessing();
                StartProcessing();
            } else {
                UpdateGUIState();
            }
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
                if (!g_gui.isRunning && checked) {
                    StartProcessing();
                }
            }
            return 0;
        case ID_GAMMA_WHITELIST_BTN:
            ShowGammaWhitelistDialog(hwnd);
            return 0;
        case ID_TETRAHEDRAL_CHECK:
            g_tetrahedralInterp = (SendMessage(g_gui.hwndTetrahedralCheck, BM_GETCHECK, 0, 0) == BST_CHECKED);
            SaveSettings();
            return 0;

        // SDR Corrections controls
        case ID_CORR_PRIMARIES_ENABLE:
        case ID_CORR_HDR_PRIMARIES_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = (LOWORD(wParam) == ID_CORR_HDR_PRIMARIES_ENABLE);
                HWND hwndEnable = isHDR ? g_gui.hwndHdrPrimariesEnable : g_gui.hwndPrimariesEnable;
                auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
                cc.primariesEnabled = (SendMessage(hwndEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                cc.primariesPreset = g_numPresetPrimaries - 1;  // Custom
                ApplyPrimariesChange(isHDR);
                SaveSettings();
            }
            return 0;

        case ID_CORR_PRIMARIES_WX: case ID_CORR_PRIMARIES_WY:
        case ID_CORR_HDR_PRIMARIES_WX: case ID_CORR_HDR_PRIMARIES_WY:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                    bool isHDR = (LOWORD(wParam) == ID_CORR_HDR_PRIMARIES_WX || LOWORD(wParam) == ID_CORR_HDR_PRIMARIES_WY);
                    HWND hwndWx = isHDR ? g_gui.hwndHdrPrimariesWx : g_gui.hwndPrimariesWx;
                    HWND hwndWy = isHDR ? g_gui.hwndHdrPrimariesWy : g_gui.hwndPrimariesWy;
                    HWND hwndEnable = isHDR ? g_gui.hwndHdrPrimariesEnable : g_gui.hwndPrimariesEnable;
                    auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                                     : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
                    wchar_t buf[16];
                    GetWindowText(hwndWx, buf, 16); cc.customPrimaries.Wx = (float)_wtof(buf);
                    GetWindowText(hwndWy, buf, 16); cc.customPrimaries.Wy = (float)_wtof(buf);
                    cc.primariesEnabled = true;
                    SendMessage(hwndEnable, BM_SETCHECK, BST_CHECKED, 0);
                    cc.primariesPreset = g_numPresetPrimaries - 1;
                    ApplyPrimariesChange(isHDR);
                    SaveSettings();
                }
            }
            return 0;

        case ID_CORR_GRAYSCALE_ENABLE:
        case ID_CORR_HDR_GRAYSCALE_ENABLE:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = (LOWORD(wParam) == ID_CORR_HDR_GRAYSCALE_ENABLE);
                HWND hwndEnable = isHDR ? g_gui.hwndHdrGrayscaleEnable : g_gui.hwndGrayscaleEnable;
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                gs.enabled = (SendMessage(hwndEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
                } else if (gs.enabled) {
                    StartProcessing();
                }
                SaveSettings();
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_GRAYSCALE_10:
        case ID_CORR_GRAYSCALE_20:
        case ID_CORR_GRAYSCALE_32:
        case ID_CORR_HDR_GRAYSCALE_10:
        case ID_CORR_HDR_GRAYSCALE_20:
        case ID_CORR_HDR_GRAYSCALE_32:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                int id = LOWORD(wParam);
                bool isHDR = (id == ID_CORR_HDR_GRAYSCALE_10 || id == ID_CORR_HDR_GRAYSCALE_20 || id == ID_CORR_HDR_GRAYSCALE_32);
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                int newCount = (id == ID_CORR_GRAYSCALE_10 || id == ID_CORR_HDR_GRAYSCALE_10) ? 10 :
                               (id == ID_CORR_GRAYSCALE_20 || id == ID_CORR_HDR_GRAYSCALE_20) ? 20 : 32;
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
        case ID_CORR_HDR_GRAYSCALE_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = (LOWORD(wParam) == ID_CORR_HDR_GRAYSCALE_EDIT);
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
        case ID_CORR_HDR_GRAYSCALE_RESET:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool isHDR = (LOWORD(wParam) == ID_CORR_HDR_GRAYSCALE_RESET);
                auto& gs = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection.grayscale
                                 : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale;
                if (isHDR) gs.initLinearPQ(); else gs.initLinear();
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
                }
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_GRAYSCALE_24:  // SDR only: 2.2->2.4 gamma checkbox
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                bool use24 = (SendMessage(g_gui.hwndGrayscale24, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection.grayscale.use24Gamma = use24;
                if (g_gui.isRunning) {
                    UpdateColorCorrectionLive(g_gui.currentMonitor, false);
                } else if (use24) {
                    StartProcessing();
                }
                UpdateGUIState();
            }
            return 0;

        case ID_CORR_HDR_GRAYSCALE_PEAK:  // HDR only: peak nits
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
                } else if (enabled) {
                    StartProcessing();
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
                    if (tm.targetPeakNits < 10.0f) tm.targetPeakNits = 10.0f;
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
                    if (tm.sourcePeakNits < 10.0f) tm.sourcePeakNits = 10.0f;
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

        // MHC Hardware Calibration controls (SDR and HDR sections)
        case ID_MHC_TAB_EDIT:
        case ID_MHC_HDR_EDIT:
            if (g_gui.currentMonitor >= 0 && g_gui.currentMonitor < (int)g_gui.monitorSettings.size()) {
                int monIdx = g_gui.currentMonitor;
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_EDIT);
                auto& mhc = isHDR ? g_gui.monitorSettings[monIdx].hdrMHC
                                   : g_gui.monitorSettings[monIdx].sdrMHC;
                auto& otherMhc = isHDR ? g_gui.monitorSettings[monIdx].sdrMHC
                                        : g_gui.monitorSettings[monIdx].hdrMHC;

                // Save original ICC state for live preview restore
                bool hadProfile = mhc.enabled && !mhc.profileName.empty();
                std::wstring origProfileName = mhc.profileName;
                std::wstring origProfilePath = mhc.profilePath;

                // Determine if live preview is possible:
                // 1. Processing must be running (or we can start it)
                // 2. Display HDR/SDR mode must match the section being edited
                bool livePreview = false;
                bool startedForPreview = false;

                // Check mode match against running monitors
                if (g_gui.isRunning) {
                    for (const auto& ctx : g_monitors) {
                        if (ctx.index == monIdx) {
                            livePreview = (ctx.isHDREnabled == isHDR);
                            break;
                        }
                    }
                }

                // If not running but mode could match, try starting processing
                if (!g_gui.isRunning) {
                    auto& ms = g_gui.monitorSettings[monIdx];
                    auto& cc = isHDR ? ms.hdrColorCorrection : ms.sdrColorCorrection;
                    bool origPrimEnabled = cc.primariesEnabled;
                    cc.primariesEnabled = true;  // Ensure this monitor is included in processing
                    StartProcessing();
                    cc.primariesEnabled = origPrimEnabled;  // Restore (processing thread has its own copy)
                    if (g_gui.isRunning) {
                        startedForPreview = true;
                        // Check mode match now that we have monitor contexts
                        for (const auto& ctx : g_monitors) {
                            if (ctx.index == monIdx) {
                                livePreview = (ctx.isHDREnabled == isHDR);
                                break;
                            }
                        }
                        // Mode mismatch — stop the processing we just started
                        if (!livePreview) {
                            StopProcessing();
                            startedForPreview = false;
                        }
                    }
                }

                // Only remove ICC and clear flags when live preview is active
                // (shader replaces ICC corrections during preview, restores on Cancel/Apply)
                std::wstring otherProfileName;
                bool otherWasEnabled = false;
                if (livePreview) {
                    // Remove ICC profile so shader preview isn't double-corrected
                    if (hadProfile) {
                        DisplayInfo displayInfo;
                        if (GetDisplayInfoForMonitor(monIdx, displayInfo)) {
                            RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
                        }
                    }

                    // Clear BOTH modes' MHC profile state so processing thread
                    // sees no active profiles and sets all MHC flags to false.
                    otherWasEnabled = otherMhc.enabled;
                    otherProfileName = otherMhc.profileName;
                    mhc.enabled = false;
                    mhc.profileName.clear();
                    mhc.profilePath.clear();
                    otherMhc.enabled = false;
                    otherMhc.profileName.clear();

                    // Clear MHC active flags so shader applies corrections
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

                g_mhcEditDialogOpen.store(true);
                ShowMhcSettingsDialog(hwnd, mhc, isHDR, monIdx,
                                      livePreview, hadProfile, origProfileName, origProfilePath);
                g_mhcEditDialogOpen.store(false);

                // Restore state after dialog closes
                if (livePreview) {
                    otherMhc.enabled = otherWasEnabled;
                    otherMhc.profileName = otherProfileName;
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
        case ID_MHC_HDR_APPLY:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_APPLY);
                if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size())
                    return 0;

                if (!IsMHC2ApiAvailable()) {
                    MessageBox(hwnd, L"MHC2 color management APIs not available.\nRequires Windows 10 21H2 or later.", L"Not Available", MB_OK | MB_ICONWARNING);
                    return 0;
                }

                if (GenerateAndInstallMhcProfile(g_gui.currentMonitor, isHDR)) {
                    auto& mhc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrMHC
                                      : g_gui.monitorSettings[g_gui.currentMonitor].sdrMHC;
                    ComputeMhcMetadata(mhc, isHDR);
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
        case ID_MHC_HDR_REMOVE:
            {
                bool isHDR = (LOWORD(wParam) == ID_MHC_HDR_REMOVE);
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

                // Delete the .icm file from system color directory
                {
                    wchar_t sysDir[MAX_PATH];
                    GetSystemDirectory(sysDir, MAX_PATH);
                    std::wstring icmPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + mhc.profileName;
                    if (DeleteFileW(icmPath.c_str())) {
                        std::wcout << L"MHC: Deleted profile file " << mhc.profileName << std::endl;
                    }
                }

                mhc.enabled = false;
                mhc.profilePath.clear();
                mhc.profileName.clear();
                mhc.hasPerChannelTRC = false;
                mhc.metaPrimaries.clear();
                mhc.metaGamma.clear();
                mhc.metaPeakNits = 0.0f;
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
                    PostMessage(g_mainHwnd, WM_HOTKEY_REGISTER, HOTKEY_GAMMA, enable ? 1 : 0);
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_HOTKEY_HDR_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyHdrEnabled.store(enable);
                if (g_mainHwnd) {
                    PostMessage(g_mainHwnd, WM_HOTKEY_REGISTER, HOTKEY_HDR_TOGGLE, enable ? 1 : 0);
                }
                SaveSettings();
            }
            return 0;

        case ID_SETTINGS_HOTKEY_ANALYSIS_CHECK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsHotkeyAnalysis, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_hotkeyAnalysisEnabled.store(enable);
                if (g_mainHwnd) {
                    PostMessage(g_mainHwnd, WM_HOTKEY_REGISTER, HOTKEY_ANALYSIS, enable ? 1 : 0);
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

        case ID_SETTINGS_DWM_HOOK:
            {
                bool enable = (SendMessage(g_gui.hwndSettingsDwmHook, BM_GETCHECK, 0, 0) == BST_CHECKED);
                g_dwmHookMode.store(enable);
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
            if (g_gui.hwndSettingsRunAtStartup)
                SendMessage(g_gui.hwndSettingsRunAtStartup, BM_SETCHECK, IsStartupEnabled() ? BST_CHECKED : BST_UNCHECKED, 0);
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
        // Auto-restart if user didn't click Stop (activeSettings still populated)
        if (!g_gui.activeSettings.empty()) {
            int delay = RESTART_INITIAL_DELAY_MS * (1 << (std::min)(g_gui.restartRetryCount, 3));
            if (delay > RESTART_MAX_DELAY_MS) delay = RESTART_MAX_DELAY_MS;
            g_gui.restartRetryCount++;
            SetTimer(hwnd, RESTART_TIMER_ID, delay, nullptr);
            wchar_t status[64];
            swprintf_s(status, L"Restarting in %ds...", delay / 1000);
            SetStatus(status);
        }
        return 0;

    case WM_MHC_PROFILE_REAPPLIED: {
        int monIdx = (int)wParam;
        bool isHDR = (lParam != 0);
        if (monIdx == g_gui.currentMonitor) {
            UpdateMhcInfoDisplay(monIdx, isHDR);
        }
        return 0;
    }

    case WM_SIZE:
        if (wParam == SIZE_MINIMIZED) {
            ShowWindow(hwnd, SW_HIDE);
        }
        return 0;

    case WM_TIMER:
        if (wParam == RESTART_TIMER_ID) {
            KillTimer(hwnd, RESTART_TIMER_ID);
            if (!g_gui.isRunning && !g_gui.activeSettings.empty()) {
                StartProcessing();
                if (g_gui.isRunning) {
                    // Success — reset backoff
                    g_gui.restartRetryCount = 0;
                } else {
                    // Still failing — schedule next retry with backoff
                    int delay = RESTART_INITIAL_DELAY_MS * (1 << (std::min)(g_gui.restartRetryCount, 3));
                    if (delay > RESTART_MAX_DELAY_MS) delay = RESTART_MAX_DELAY_MS;
                    g_gui.restartRetryCount++;
                    SetTimer(hwnd, RESTART_TIMER_ID, delay, nullptr);
                    wchar_t status[64];
                    swprintf_s(status, L"Restart failed, retrying in %ds...", delay / 1000);
                    SetStatus(status);
                }
            }
            return 0;
        }
        if (wParam == SETTINGS_CHANGE_TIMER_ID) {
            KillTimer(hwnd, SETTINGS_CHANGE_TIMER_ID);
            std::cout << "[GUI] Settings change debounce fired, forcing reinit..." << std::endl;
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            return 0;
        }
        if (wParam == DEVICE_CHANGE_TIMER_ID) {
            KillTimer(hwnd, DEVICE_CHANGE_TIMER_ID);
            std::cout << "[GUI] Device change debounce fired, forcing reinit..." << std::endl;
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            return 0;
        }
        if (wParam == DWM_HOOK_WATCHDOG_TIMER_ID) {
            // Periodic health check for DWM hook injection
            if (!g_gui.isRunning || !g_dwmHookMode.load()) {
                KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                return 0;
            }
            if (!IsDwmHookActive()) {
                std::cout << "[DWM Hook Watchdog] Hook lost (DWM restart?), attempting re-injection..." << std::endl;

                // Re-derive monitor LUT config from current settings + monitors
                std::vector<DwmHookMonitorLUT> dwmMonitors;
                for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
                    const auto& ms = g_gui.monitorSettings[i];
                    if (ms.sdrPath.empty() && ms.hdrPath.empty()) continue;
                    if (i >= g_gui.monitors.size()) continue;

                    MONITORINFO mi = { sizeof(mi) };
                    if (GetMonitorInfo(g_gui.monitors[i], &mi)) {
                        DwmHookMonitorLUT lut;
                        lut.left = mi.rcMonitor.left;
                        lut.top = mi.rcMonitor.top;
                        lut.sdrLutPath = ms.sdrPath;
                        lut.hdrLutPath = ms.hdrPath;
                        dwmMonitors.push_back(lut);
                    }
                }

                if (!dwmMonitors.empty()) {
                    std::wstring err = InjectDwmHook(dwmMonitors);
                    if (err.empty()) {
                        std::cout << "[DWM Hook Watchdog] Re-injection successful" << std::endl;
                        g_dwmHookWatchdogRetries = 0;
                        SetStatus(L"Active (DWM Hook)");
                    } else {
                        g_dwmHookWatchdogRetries++;
                        std::wcout << L"[DWM Hook Watchdog] Re-injection failed (attempt "
                                   << g_dwmHookWatchdogRetries << L"/" << DWM_HOOK_WATCHDOG_MAX_RETRIES
                                   << L"): " << err << std::endl;
                        if (g_dwmHookWatchdogRetries >= DWM_HOOK_WATCHDOG_MAX_RETRIES) {
                            KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                            SetStatus(L"DWM Hook lost — re-injection failed");
                            std::cout << "[DWM Hook Watchdog] Max retries reached, giving up" << std::endl;
                        }
                    }
                } else {
                    // No LUT paths configured — nothing to inject
                    KillTimer(hwnd, DWM_HOOK_WATCHDOG_TIMER_ID);
                }
            } else {
                // Hook is healthy — reset retry counter
                g_dwmHookWatchdogRetries = 0;
            }
            return 0;
        }
        break;  // Let other timers pass through to DefWindowProc

    case WM_DISPLAYCHANGE: {
        // Monitor hotplug: re-enumerate and update if count or handles changed
        std::vector<HMONITOR> newMonitors;
        EnumDisplayMonitors(nullptr, nullptr, GUIMonitorEnumProc, reinterpret_cast<LPARAM>(&newMonitors));
        bool changed = (newMonitors.size() != g_gui.monitors.size());
        if (!changed) {
            for (size_t i = 0; i < newMonitors.size(); i++) {
                if (newMonitors[i] != g_gui.monitors[i]) { changed = true; break; }
            }
        }
        if (changed) {
            std::cout << "Display change: monitor count " << g_gui.monitors.size()
                      << " -> " << newMonitors.size() << std::endl;
            g_gui.monitors = newMonitors;

            // Grow monitorSettings if needed — never shrink, to preserve settings for
            // monitors that may temporarily disappear (physical power-off, KVM switch).
            // Render/whitelist threads bounds-check via monitor index.
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (newMonitors.size() > g_gui.monitorSettings.size()) {
                    g_gui.monitorSettings.resize(newMonitors.size());
                }
            }

            // Update monitor names and combo box
            g_gui.monitorNames.clear();
            SendMessage(g_gui.hwndMonitorList, LB_RESETCONTENT, 0, 0);
            for (size_t i = 0; i < newMonitors.size(); i++) {
                MONITORINFO mi = { sizeof(mi) };
                GetMonitorInfo(newMonitors[i], &mi);
                int monW = mi.rcMonitor.right - mi.rcMonitor.left;
                int monH = mi.rcMonitor.bottom - mi.rcMonitor.top;
                bool isPrimary = (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
                DisplayInfo dispInfo;
                std::wstring friendlyName;
                if (GetDisplayInfoForMonitor((int)i, dispInfo) && !dispInfo.name.empty()) {
                    friendlyName = dispInfo.name;
                }
                wchar_t name[128];
                if (!friendlyName.empty()) {
                    swprintf_s(name, L"Monitor %d - %s: %dx%d%s", (int)i, friendlyName.c_str(),
                        monW, monH, isPrimary ? L" [Primary]" : L"");
                } else {
                    swprintf_s(name, L"Monitor %d: %dx%d%s", (int)i,
                        monW, monH, isPrimary ? L" [Primary]" : L"");
                }
                g_gui.monitorNames.push_back(name);
                SendMessage(g_gui.hwndMonitorList, LB_ADDSTRING, 0, (LPARAM)name);
            }

            // Clamp current monitor selection
            if (g_gui.currentMonitor >= (int)newMonitors.size()) {
                g_gui.currentMonitor = (int)newMonitors.size() - 1;
            }
            if (g_gui.currentMonitor < 0) g_gui.currentMonitor = 0;
            SendMessage(g_gui.hwndMonitorList, LB_SETCURSEL, g_gui.currentMonitor, 0);

            // Force reinit if processing is running, or restart if it exited
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            } else if (!g_gui.activeSettings.empty()) {
                // Processing exited (e.g., monitors were off during init) — restart now
                std::cout << "[GUI] Display change with active settings, restarting processing..." << std::endl;
                StartProcessing();
            }
        }
        return 0;
    }

    case WM_WTSSESSION_CHANGE:
        switch (wParam) {
        case WTS_SESSION_UNLOCK:
        case WTS_CONSOLE_CONNECT:
            std::cout << "[GUI] Session unlock/connect, forcing reinit..." << std::endl;
            g_displayOff.store(false);
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
                if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            }
            break;
        case WTS_SESSION_LOCK:
        case WTS_CONSOLE_DISCONNECT:
            std::cout << "[GUI] Session lock/disconnect" << std::endl;
            g_displayOff.store(true);
            g_lastSuccessfulFrame = std::chrono::steady_clock::now();
            if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
            break;
        }
        return 0;

    case WM_SETTINGCHANGE:
        // Detect color settings changes that can affect capture format (Night Light, ACM, HDR)
        // SPI_SETWORKAREA (taskbar resize) removed — unrelated to color pipeline
        if (lParam && wcscmp(reinterpret_cast<LPCWSTR>(lParam), L"ImmersiveColorSet") == 0) {
            std::cout << "[GUI] ImmersiveColorSet changed, debouncing reinit..." << std::endl;
            SetTimer(hwnd, SETTINGS_CHANGE_TIMER_ID, 500, nullptr);
        }
        break;  // Let DefWindowProc also process

    case WM_DEVICECHANGE:
        if (wParam == DBT_DEVNODES_CHANGED) {
            std::cout << "[GUI] Device tree changed, debouncing reinit..." << std::endl;
            SetTimer(hwnd, DEVICE_CHANGE_TIMER_ID, 2000, nullptr);
        }
        return TRUE;

    case WM_POWERBROADCAST:
        // Handle power events for sleep/wake recovery (defense in depth with overlay WndProc)
        if (wParam == PBT_APMRESUMEAUTOMATIC || wParam == PBT_APMRESUMESUSPEND) {
            if (g_gui.isRunning) {
                g_forceReinit.store(true);
                g_forceMhcReapply.store(true);
            }
        }
        // Handle display power state changes — GUI-side handler fires immediately on the
        // main thread even when the processing thread is blocked in CompClock/DwmFlush.
        // This prevents the watchdog from firing during display sleep.
        else if (wParam == PBT_POWERSETTINGCHANGE) {
            POWERBROADCAST_SETTING* pbs = reinterpret_cast<POWERBROADCAST_SETTING*>(lParam);
            if (pbs && pbs->PowerSetting == GUID_CONSOLE_DISPLAY_STATE_GUI) {
                DWORD displayState = *reinterpret_cast<DWORD*>(pbs->Data);
                if (displayState == 0) {
                    // Display off — set flag and unblock processing thread
                    std::cout << "[GUI] Display entering sleep mode" << std::endl;
                    g_displayOff.store(true);
                    g_lastSuccessfulFrame = std::chrono::steady_clock::now();
                    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                } else if (displayState == 1) {
                    // Display on — trigger reinit or restart processing
                    std::cout << "[GUI] Display waking from sleep" << std::endl;
                    g_displayOff.store(false);
                    if (g_gui.isRunning) {
                        g_forceReinit.store(true);
                        g_forceMhcReapply.store(true);
                        if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
                    } else if (!g_gui.activeSettings.empty()) {
                        // Processing thread exited during display-off (e.g., watchdog timeout).
                        // Restart it automatically since the user had it running before.
                        std::cout << "[GUI] Processing was interrupted during display-off, restarting..." << std::endl;
                        StartProcessing();
                    }
                }
            }
        }
        return TRUE;

    case WM_QUERYENDSESSION:
        return TRUE;

    case WM_ENDSESSION:
        if (wParam) {
            StopProcessing();
            RemoveTrayIcon();
            DestroyWindow(hwnd);
        }
        return 0;

    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        return 0;

    case WM_DESTROY:
        StopProcessing();
        RemoveTrayIcon();
        // Unregister session change notifications
        WTSUnRegisterSessionNotification(hwnd);
        // Unregister GUI-side display power notification
        if (g_guiDisplayPowerNotify) {
            UnregisterPowerSettingNotification(g_guiDisplayPowerNotify);
            g_guiDisplayPowerNotify = nullptr;
        }
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

// ============================================================================
// SECTION: Application Entry
// ============================================================================

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
