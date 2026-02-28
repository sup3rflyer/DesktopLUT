// DesktopLUT - gui_whitelist.cpp
// Whitelist dialog windows (gamma and VRR/passthrough)

#include "gui_whitelist.h"
#include "gui_shared.h"
#include "globals.h"
#include "settings.h"

// ============================================================================
// SECTION: Gamma Whitelist Dialog
// ============================================================================

static HWND g_whitelistEdit = nullptr;

static LRESULT CALLBACK GammaWhitelistProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
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
        if (msg.message == WM_QUIT) { PostQuitMessage((int)msg.wParam); break; }
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);
}

// ============================================================================
// SECTION: VRR Whitelist Dialog
// ============================================================================

static HWND g_vrrWhitelistEdit = nullptr;

static LRESULT CALLBACK VrrWhitelistProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
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
        if (msg.message == WM_QUIT) { PostQuitMessage((int)msg.wParam); break; }
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);
}
