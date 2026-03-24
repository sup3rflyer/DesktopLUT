// DesktopLUT - gui_shared.cpp
// Shared GUI helpers: fonts, brushes, drawing, numeric input

#include "gui_shared.h"
#include "globals.h"
#include "settings.h"
#include <algorithm>

// Custom colors for Windows 11-like scheme
HBRUSH g_tabBgBrush = nullptr;
HBRUSH g_inactiveTabBrush = nullptr;
HFONT g_mainFont = nullptr;
HFONT g_grayscaleFont = nullptr;
const COLORREF TAB_BG_COLOR = RGB(0xf9, 0xf9, 0xf9);
const COLORREF INACTIVE_TAB_COLOR = RGB(0xf3, 0xf3, 0xf3);

const int GRAYSCALE_SLIDER_SCALE = 100;  // Slider units per 1%

void UpdateEditFromSlider(int index) {
    auto* data = g_grayscaleEditor;
    if (!data || data->updatingFromEdit) return;

    data->updatingFromSlider = true;
    int pos = (int)SendMessage(data->sliders[index], TBM_GETPOS, 0, 0);
    float deviation = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;  // Negate because trackbar is inverted
    wchar_t text[16];
    _swprintf_s_l(text, _countof(text), L"%.2f", GetCLocale(), deviation);
    SetWindowText(data->editBoxes[index], text);
    data->updatingFromSlider = false;
}

void UpdateSliderFromEdit(int index) {
    auto* data = g_grayscaleEditor;
    if (!data || data->updatingFromSlider) return;

    data->updatingFromEdit = true;
    wchar_t text[16];
    GetWindowText(data->editBoxes[index], text, 16);
    float deviation = (float)_wcstod_l(text, nullptr, GetCLocale());
    int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;
    int sliderVal = (int)(deviation * GRAYSCALE_SLIDER_SCALE + 0.5f);
    sliderVal = (std::max)(-maxRange, (std::min)(maxRange, sliderVal));
    SendMessage(data->sliders[index], TBM_SETPOS, TRUE, -sliderVal);
    data->updatingFromEdit = false;
}

// Numeric edit box subclass - filters input to only allow valid decimal numbers
// maxDecimals is stored in dwRefData (set when subclassing)
// Also handles Enter key to commit and unfocus
LRESULT CALLBACK NumericEditSubclassProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam,
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

void SetNumericEdit(HWND hwnd, int maxDecimals) {
    SetWindowSubclass(hwnd, NumericEditSubclassProc, 0, (DWORD_PTR)maxDecimals);
}

void SetPathText(HWND hwndEdit, const wchar_t* path) {
    if (!path || !*path) {
        SetWindowText(hwndEdit, L"");
        return;
    }
    // Extract just the filename
    const wchar_t* filename = wcsrchr(path, L'\\');
    if (!filename) filename = wcsrchr(path, L'/');
    SetWindowText(hwndEdit, filename ? filename + 1 : path);
}

void DrawRoundedButton(LPDRAWITEMSTRUCT pDIS) {
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
