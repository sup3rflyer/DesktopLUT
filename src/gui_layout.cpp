// DesktopLUT - gui_layout.cpp
// GUI layout creation (WM_CREATE) and scroll panel infrastructure

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
#include "analysis.h"
#include "../resource.h"
#include <commctrl.h>
#include <commdlg.h>
#include <algorithm>
#include <iostream>
#include <cstdio>
#include <locale>
#include <wtsapi32.h>
#include <dbt.h>

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "Wtsapi32.lib")

// ============================================================================
// SECTION: Scroll Panels
// ============================================================================

// Monitor enumeration for GUI
BOOL CALLBACK GUIMonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam) {
    auto* monitors = reinterpret_cast<std::vector<HMONITOR>*>(lParam);
    monitors->push_back(hMonitor);
    return TRUE;
}

// Scroll panel window procedure
LRESULT CALLBACK ScrollPanelProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
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
        // Handle mouse wheel scrolling (accumulate delta per-panel for smooth-scroll mice)
        static int wheelAccum[4] = {};
        int tabIdx = (int)GetWindowLongPtr(hwnd, GWLP_USERDATA);
        if (tabIdx < 0 || tabIdx >= 4) return 0;
        int delta = GET_WHEEL_DELTA_WPARAM(wParam);
        wheelAccum[tabIdx] += delta;
        int lines = (wheelAccum[tabIdx] * 3) / WHEEL_DELTA;
        wheelAccum[tabIdx] -= (lines * WHEEL_DELTA) / 3;
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
// SECTION: WM_CREATE Layout
// ============================================================================

void CreateGUILayout(HWND hwnd) {
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

    innerY += 120;

    // --- SDR inline corrections (White Balance + Grayscale) ---
    {
        int chromW = 50;

        // White Balance groupbox (48px)
        ctrl = CreateWindow(L"BUTTON", L"White Balance", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndMhcWbEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel0, (HMENU)ID_MHC_SDR_WB_ENABLE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcWbEnable);

        ctrl = CreateWindow(L"STATIC", L"x:", WS_CHILD,
            innerX + 80, innerY + 20, 14, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndMhcWbWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            innerX + 94, innerY + 18, chromW, h, panel0, (HMENU)ID_MHC_SDR_WB_WX, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcWbWx);

        ctrl = CreateWindow(L"STATIC", L"y:", WS_CHILD,
            innerX + 150, innerY + 20, 14, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndMhcWbWy = CreateWindow(L"EDIT", L"0.3290", WS_CHILD | WS_BORDER,
            innerX + 164, innerY + 18, chromW, h, panel0, (HMENU)ID_MHC_SDR_WB_WY, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcWbWy);

        SetNumericEdit(g_gui.hwndMhcWbWx, 4);
        SetNumericEdit(g_gui.hwndMhcWbWy, 4);

        innerY += 53;

        // Grayscale groupbox (75px)
        ctrl = CreateWindow(L"BUTTON", L"Grayscale", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndMhcGsEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel0, (HMENU)ID_MHC_SDR_GS_ENABLE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGsEnable);

        ctrl = CreateWindow(L"STATIC", L"Points:", WS_CHILD, innerX + 80, innerY + 20, 45, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndMhcGs10 = CreateWindow(L"BUTTON", L"10", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
            innerX + 130, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_SDR_GS_10, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGs10);
        g_gui.hwndMhcGs20 = CreateWindow(L"BUTTON", L"20", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 175, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_SDR_GS_20, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGs20);
        g_gui.hwndMhcGs32 = CreateWindow(L"BUTTON", L"32", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 220, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_SDR_GS_32, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGs32);
        SendMessage(g_gui.hwndMhcGs20, BM_SETCHECK, BST_CHECKED, 0);

        g_gui.hwndMhcGsEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel0, (HMENU)ID_MHC_SDR_GS_EDIT, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGsEdit);
        g_gui.hwndMhcGsReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel0, (HMENU)ID_MHC_SDR_GS_RESET, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGsReset);

        g_gui.hwndMhcGs24 = CreateWindow(L"BUTTON", L"2.2\u21922.4 Gamma",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 180, innerY + 47, 130, h, panel0, (HMENU)ID_MHC_SDR_GS_24, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndMhcGs24);

        innerY += 80;
    }

    innerY += 5;

    // HDR Display Calibration section
    createMhcSection(L"HDR Display Calibration", innerY,
        g_gui.hwndHdrMhcApply, g_gui.hwndHdrMhcRemove, g_gui.hwndHdrMhcEdit, g_gui.hwndHdrMhcStatus,
        g_gui.hwndHdrMhcIccCoords, g_gui.hwndHdrMhcMetaLabels,
        (HMENU)ID_MHC_HDR_APPLY, (HMENU)ID_MHC_HDR_REMOVE, (HMENU)ID_MHC_HDR_EDIT);

    innerY += 120;

    // --- HDR inline corrections (Desktop Gamma + White Balance + Grayscale) ---
    {
        int chromW = 50;

        // Desktop Gamma groupbox (48px, HDR only)
        ctrl = CreateWindow(L"BUTTON", L"Desktop Gamma", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndHdrMhcDgEnable = CreateWindow(L"BUTTON", L"sRGB\x2192""2.2 Gamma",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 200, h, panel0, (HMENU)ID_MHC_HDR_DG_ENABLE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcDgEnable);

        g_gui.hwndHdrMhcDgWhitelist = CreateWindow(L"BUTTON", L"Whitelist...",
            WS_CHILD | BS_OWNERDRAW,
            innerX + 220, innerY + 18, 70, h, panel0, (HMENU)ID_MHC_HDR_DG_WHITELIST, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcDgWhitelist);

        innerY += 53;

        // White Balance groupbox (48px)
        ctrl = CreateWindow(L"BUTTON", L"White Balance", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 48, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndHdrMhcWbEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel0, (HMENU)ID_MHC_HDR_WB_ENABLE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcWbEnable);

        ctrl = CreateWindow(L"STATIC", L"x:", WS_CHILD,
            innerX + 80, innerY + 20, 14, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndHdrMhcWbWx = CreateWindow(L"EDIT", L"0.3127", WS_CHILD | WS_BORDER,
            innerX + 94, innerY + 18, chromW, h, panel0, (HMENU)ID_MHC_HDR_WB_WX, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcWbWx);

        ctrl = CreateWindow(L"STATIC", L"y:", WS_CHILD,
            innerX + 150, innerY + 20, 14, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndHdrMhcWbWy = CreateWindow(L"EDIT", L"0.3290", WS_CHILD | WS_BORDER,
            innerX + 164, innerY + 18, chromW, h, panel0, (HMENU)ID_MHC_HDR_WB_WY, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcWbWy);

        SetNumericEdit(g_gui.hwndHdrMhcWbWx, 4);
        SetNumericEdit(g_gui.hwndHdrMhcWbWy, 4);

        innerY += 53;

        // Grayscale groupbox (75px)
        ctrl = CreateWindow(L"BUTTON", L"Grayscale", WS_CHILD | BS_GROUPBOX,
            innerX, innerY, groupW, 75, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndHdrMhcGsEnable = CreateWindow(L"BUTTON", L"Enable",
            WS_CHILD | BS_AUTOCHECKBOX,
            innerX + 10, innerY + 18, 60, h, panel0, (HMENU)ID_MHC_HDR_GS_ENABLE, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGsEnable);

        ctrl = CreateWindow(L"STATIC", L"Points:", WS_CHILD, innerX + 80, innerY + 20, 45, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);

        g_gui.hwndHdrMhcGs10 = CreateWindow(L"BUTTON", L"10", WS_CHILD | BS_AUTORADIOBUTTON | WS_GROUP,
            innerX + 130, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_HDR_GS_10, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGs10);
        g_gui.hwndHdrMhcGs20 = CreateWindow(L"BUTTON", L"20", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 175, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_HDR_GS_20, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGs20);
        g_gui.hwndHdrMhcGs32 = CreateWindow(L"BUTTON", L"32", WS_CHILD | BS_AUTORADIOBUTTON,
            innerX + 220, innerY + 18, 40, h, panel0, (HMENU)ID_MHC_HDR_GS_32, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGs32);
        SendMessage(g_gui.hwndHdrMhcGs20, BM_SETCHECK, BST_CHECKED, 0);

        g_gui.hwndHdrMhcGsEdit = CreateWindow(L"BUTTON", L"Edit Points...",
            WS_CHILD | BS_OWNERDRAW, innerX + 10, innerY + 45, 90, h, panel0, (HMENU)ID_MHC_HDR_GS_EDIT, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGsEdit);
        g_gui.hwndHdrMhcGsReset = CreateWindow(L"BUTTON", L"Reset",
            WS_CHILD | BS_OWNERDRAW, innerX + 110, innerY + 45, 60, h, panel0, (HMENU)ID_MHC_HDR_GS_RESET, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGsReset);

        // HDR only: Peak nits label + edit
        ctrl = CreateWindow(L"STATIC", L"Peak:", WS_CHILD,
            innerX + 180, innerY + 47, 35, h, panel0, nullptr, nullptr, nullptr);
        g_gui.tab0Controls.push_back(ctrl);
        g_gui.hwndHdrMhcGsPeak = CreateWindow(L"EDIT", L"10000", WS_CHILD | WS_BORDER | ES_NUMBER,
            innerX + 215, innerY + 45, 45, h, panel0, (HMENU)ID_MHC_HDR_GS_PEAK, nullptr, nullptr);
        g_gui.tab0Controls.push_back(g_gui.hwndHdrMhcGsPeak);

        innerY += 80;
    }

    g_gui.contentHeight[0] = innerY + 8;

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

    // === TAB 2: Corrections (HDR-only: Tonemapping + MaxTML) ===
    // SDR corrections (White Point, Grayscale, Desktop Gamma) moved to MHC tab
    innerY = 8;
    HWND panel2 = g_gui.hwndScrollPanel[2];

    // HDR Corrections header
    ctrl = CreateWindow(L"STATIC", L"HDR Corrections", WS_CHILD | SS_LEFT,
        innerX, innerY, groupW, 16, panel2, nullptr, nullptr, nullptr);
    g_gui.tab2Controls.push_back(ctrl);
    innerY += 18;

    // Tonemapping group (HDR only)
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


    g_gui.contentHeight[2] = innerY + 48 + 8;  // HDR-only: Tonemapping + MaxTML

    // Apply Enter key handling to numeric edit boxes
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
    swprintf_s(hotkeyLabel, L"Gamma Toggle (Win+Shift+%c)", g_hotkeyGammaKey.load());
    g_gui.hwndSettingsHotkeyGamma = CreateWindow(L"BUTTON", hotkeyLabel,
        WS_CHILD | BS_AUTOCHECKBOX,
        innerX + 10, innerY + 20, 220, h, panel3, (HMENU)ID_SETTINGS_HOTKEY_GAMMA_CHECK, nullptr, nullptr);
    g_gui.tab3Controls.push_back(g_gui.hwndSettingsHotkeyGamma);
    SendMessage(g_gui.hwndSettingsHotkeyGamma, BM_SETCHECK, g_hotkeyGammaEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

    // HDR Toggle hotkey
    swprintf_s(hotkeyLabel, L"HDR Toggle (Win+Shift+%c)", g_hotkeyHdrKey.load());
    g_gui.hwndSettingsHotkeyHdr = CreateWindow(L"BUTTON", hotkeyLabel,
        WS_CHILD | BS_AUTOCHECKBOX,
        innerX + 10, innerY + 42, 220, h, panel3, (HMENU)ID_SETTINGS_HOTKEY_HDR_CHECK, nullptr, nullptr);
    g_gui.tab3Controls.push_back(g_gui.hwndSettingsHotkeyHdr);
    SendMessage(g_gui.hwndSettingsHotkeyHdr, BM_SETCHECK, g_hotkeyHdrEnabled.load() ? BST_CHECKED : BST_UNCHECKED, 0);

    // Analysis Overlay hotkey
    swprintf_s(hotkeyLabel, L"Analysis Overlay (Win+Shift+%c)", g_hotkeyAnalysisKey.load());
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

    // Update startup task path if exe was moved (migrates old registry Run key)
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

    // Kick off the periodic MHC profile verification. Windows can silently drop
    // our ICC profile association (calibration tools, Color Management panel,
    // driver resets, Auto-HDR re-brokering). Running on the GUI thread means it
    // fires even when no processing thread is active — MHC-only users are also
    // covered. Timer fires every MHC_VERIFY_INTERVAL_MS.
    SetTimer(hwnd, MHC_VERIFY_TIMER_ID, MHC_VERIFY_INTERVAL_MS, nullptr);

    // Periodic hardware-LUT reload: catches silent drops that verify can't see
    // (the profile stays associated but the compositor/driver stopped honoring
    // the MHC2 tag). Uses Windows' own Calibration Loader scheduled task, so
    // no flicker through a fallback profile.
    SetTimer(hwnd, MHC_BLIND_KICK_TIMER_ID, MHC_BLIND_KICK_INTERVAL_MS, nullptr);

    // Launch the registry watcher that detects third-party writes to the ICM
    // keys (calibration tools, GPU control panels, colorcpl) and fires a kick
    // on demand. Started here so it's tied to GUI window lifetime.
    StartIcmRegistryWatcher(hwnd);
}
