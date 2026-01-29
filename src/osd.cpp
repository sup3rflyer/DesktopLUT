// DesktopLUT - osd.cpp
// On-screen display notification

#include "osd.h"
#include "globals.h"

LRESULT CALLBACK OSDWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);

        // Get window text
        wchar_t text[64];
        GetWindowText(hwnd, text, 64);

        // Get client rect
        RECT rc;
        GetClientRect(hwnd, &rc);

        // Fill background with dark semi-transparent color
        HBRUSH bgBrush = CreateSolidBrush(RGB(32, 32, 32));
        FillRect(hdc, &rc, bgBrush);
        DeleteObject(bgBrush);

        // Draw border
        HPEN borderPen = CreatePen(PS_SOLID, 1, RGB(80, 80, 80));
        HPEN oldPen = (HPEN)SelectObject(hdc, borderPen);
        HBRUSH oldBrush = (HBRUSH)SelectObject(hdc, GetStockObject(NULL_BRUSH));
        Rectangle(hdc, 0, 0, rc.right, rc.bottom);
        SelectObject(hdc, oldPen);
        SelectObject(hdc, oldBrush);
        DeleteObject(borderPen);

        // Draw text
        SetBkMode(hdc, TRANSPARENT);
        SetTextColor(hdc, RGB(255, 255, 255));
        HFONT font = CreateFont(24, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
        HFONT oldFont = (HFONT)SelectObject(hdc, font);
        DrawText(hdc, text, -1, &rc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SelectObject(hdc, oldFont);
        DeleteObject(font);

        EndPaint(hwnd, &ps);
        return 0;
    }
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void ShowOSD(const wchar_t* text) {
    if (!g_osdHwnd || !g_mainHwnd) return;

    // Update text
    SetWindowText(g_osdHwnd, text);

    // Calculate size based on text
    HDC hdc = GetDC(g_osdHwnd);
    HFONT font = CreateFont(24, 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
        CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    HFONT oldFont = (HFONT)SelectObject(hdc, font);
    SIZE textSize;
    GetTextExtentPoint32(hdc, text, (int)wcslen(text), &textSize);
    SelectObject(hdc, oldFont);
    DeleteObject(font);
    ReleaseDC(g_osdHwnd, hdc);

    int padding = 20;
    int width = textSize.cx + padding * 2;
    int height = textSize.cy + padding;

    // Position in bottom-right corner of primary monitor (above taskbar)
    int marginX = 40;
    int marginY = 80;  // Extra margin to clear taskbar
    int screenW = GetSystemMetrics(SM_CXSCREEN);
    int screenH = GetSystemMetrics(SM_CYSCREEN);
    int x = screenW - width - marginX;
    int y = screenH - height - marginY;

    SetWindowPos(g_osdHwnd, HWND_TOPMOST, x, y, width, height, SWP_SHOWWINDOW);
    InvalidateRect(g_osdHwnd, nullptr, TRUE);

    // Set timer to hide
    KillTimer(g_mainHwnd, OSD_TIMER_ID);
    SetTimer(g_mainHwnd, OSD_TIMER_ID, OSD_DURATION_MS, nullptr);
}

void HideOSD() {
    if (g_osdHwnd) {
        ShowWindow(g_osdHwnd, SW_HIDE);
    }
    if (g_mainHwnd) {
        KillTimer(g_mainHwnd, OSD_TIMER_ID);
    }
}

bool CreateOSDWindow(HINSTANCE hInstance) {
    WNDCLASSEX wcOSD = { sizeof(WNDCLASSEX) };
    wcOSD.lpfnWndProc = OSDWndProc;
    wcOSD.hInstance = hInstance;
    wcOSD.lpszClassName = g_osdClassName;
    wcOSD.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    RegisterClassEx(&wcOSD);

    g_osdHwnd = CreateWindowEx(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        g_osdClassName, L"",
        WS_POPUP,
        0, 0, 200, 50,
        nullptr, nullptr, hInstance, nullptr);

    if (!g_osdHwnd) return false;

    // Set window opacity (slightly transparent background)
    SetLayeredWindowAttributes(g_osdHwnd, 0, 230, LWA_ALPHA);

    return true;
}
