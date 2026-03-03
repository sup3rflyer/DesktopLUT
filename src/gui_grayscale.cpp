// DesktopLUT - gui_grayscale.cpp
// Grayscale correction editor dialog with per-channel RGB vertical strips

#include "gui.h"
#include "gui_shared.h"
#include "globals.h"
#include "processing.h"
#include <commctrl.h>
#include <windowsx.h>
#include <algorithm>
#include <cmath>

// RGB strip dimensions
static constexpr int RGB_STRIP_W = 32;
static constexpr int RGB_STRIP_H = 50;   // Vertical bars height
static constexpr int RGB_BAR_W = 8;
static constexpr int RGB_BAR_GAP = 2;
// RGB deviation range (same as GRAYSCALE_RANGE for consistency: +/-25%)
static constexpr float RGB_DEV_STEP = 0.001f;  // 0.1% per scroll notch

static const COLORREF kStripColors[3] = {
    RGB(200, 50, 50),    // Red
    RGB(50, 160, 50),    // Green
    RGB(50, 90, 200),    // Blue
};

// Channel color indicator bars under RGB edit boxes (file-static, one editor at a time)
static std::vector<HWND> s_rgbIndicators;
static HBRUSH s_indicatorBrush = nullptr;

static constexpr int RGB_INDICATOR_H = 3;

// Forward declarations
static void FireLiveUpdate();
static void UpdateRGBEditBoxes();
static void InvalidateAllStrips();
static void InvalidateIndicators();
static int ChannelFromX(int x, int controlW = RGB_STRIP_W);

// ============================================================================
// RGB Strip custom control — 3 vertical colored bars with white line indicators
// ============================================================================

struct RGBStripData {
    int pointIndex;
};

static int ChannelFromX(int x, int controlW) {
    int totalBarsW = 3 * RGB_BAR_W + 2 * RGB_BAR_GAP;
    int startX = (controlW - totalBarsW) / 2;
    for (int ch = 0; ch < 3; ch++) {
        int barX = startX + ch * (RGB_BAR_W + RGB_BAR_GAP);
        if (x >= barX && x < barX + RGB_BAR_W) return ch;
    }
    return -1;
}

static void PaintRGBStrip(HWND hwnd, int pointIndex) {
    PAINTSTRUCT ps;
    HDC hdc = BeginPaint(hwnd, &ps);
    auto* data = g_grayscaleEditor;
    if (!data) { EndPaint(hwnd, &ps); return; }

    RECT rc;
    GetClientRect(hwnd, &rc);
    int h = rc.bottom - rc.top;

    // Background
    if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
    FillRect(hdc, &rc, g_tabBgBrush);

    // Even spacing: 3 bars of RGB_BAR_W with RGB_BAR_GAP between, centered in control
    int totalBarsW = 3 * RGB_BAR_W + 2 * RGB_BAR_GAP;
    int startX = (rc.right - totalBarsW) / 2;

    for (int ch = 0; ch < 3; ch++) {
        int barX = startX + ch * (RGB_BAR_W + RGB_BAR_GAP);

        // Fill entire bar with channel color
        RECT barRect = { barX, 0, barX + RGB_BAR_W, h };
        HBRUSH barBrush = CreateSolidBrush(kStripColors[ch]);
        FillRect(hdc, &barRect, barBrush);
        DeleteObject(barBrush);

        // Get deviation value (1.0 = center = no offset)
        float dev = 1.0f;
        if (data->rgbDeviations[ch] && pointIndex < data->pointCount)
            dev = data->rgbDeviations[ch][pointIndex];

        // Map deviation to Y: top=+GRAYSCALE_RANGE%, center=0%, bottom=-GRAYSCALE_RANGE%
        float devPct = (dev - 1.0f) * 100.0f;
        float frac = devPct / (float)GRAYSCALE_RANGE;  // -1 to +1
        frac = (std::max)(-1.0f, (std::min)(1.0f, frac));
        int lineY = (int)((1.0f - frac) * 0.5f * (h - 1));

        // Center tick (thin dark line)
        HPEN darkPen = CreatePen(PS_SOLID, 1, RGB(0, 0, 0));
        HPEN oldPen = (HPEN)SelectObject(hdc, darkPen);
        MoveToEx(hdc, barX, h / 2, nullptr);
        LineTo(hdc, barX + RGB_BAR_W, h / 2);
        SelectObject(hdc, oldPen);
        DeleteObject(darkPen);

        // White indicator line
        HPEN whitePen = CreatePen(PS_SOLID, 2, RGB(255, 255, 255));
        oldPen = (HPEN)SelectObject(hdc, whitePen);
        MoveToEx(hdc, barX, lineY, nullptr);
        LineTo(hdc, barX + RGB_BAR_W, lineY);
        SelectObject(hdc, oldPen);
        DeleteObject(whitePen);
    }

    EndPaint(hwnd, &ps);
}

static LRESULT CALLBACK RGBStripProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    RGBStripData* stripData = (RGBStripData*)GetWindowLongPtr(hwnd, GWLP_USERDATA);
    auto* data = g_grayscaleEditor;

    switch (msg) {
    case WM_PAINT:
        if (stripData) PaintRGBStrip(hwnd, stripData->pointIndex);
        return 0;

    case WM_LBUTTONDOWN: {
        if (!data || !stripData) break;
        int x = LOWORD(lParam);
        int ch = ChannelFromX(x);
        if (ch >= 0) {
            data->selectedChannel = ch;
            UpdateRGBEditBoxes();
            InvalidateAllStrips();
        }
        return 0;
    }

    case WM_MBUTTONDOWN: {
        // Middle click: reset channel deviation to 1.0
        if (!data || !stripData) break;
        int x = LOWORD(lParam);
        int ch = ChannelFromX(x);
        if (ch < 0) ch = data->selectedChannel;
        if (ch >= 0 && data->rgbDeviations[ch]) {
            data->rgbDeviations[ch][stripData->pointIndex] = 1.0f;
            data->selectedChannel = ch;
            UpdateRGBEditBoxes();
            InvalidateRect(hwnd, nullptr, FALSE);
            FireLiveUpdate();
        }
        return 0;
    }

    case WM_MOUSEWHEEL: {
        if (!data || !stripData) break;
        POINT pt = { GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
        ScreenToClient(hwnd, &pt);
        int ch = ChannelFromX(pt.x);
        if (ch < 0) ch = data->selectedChannel;
        if (ch < 0) ch = 0;

        // Accumulate delta for smooth-scroll mice
        static int wheelAccum = 0;
        wheelAccum += GET_WHEEL_DELTA_WPARAM(wParam);
        int notches = wheelAccum / WHEEL_DELTA;
        if (notches == 0) break;
        wheelAccum -= notches * WHEEL_DELTA;

        if (data->rgbDeviations[ch]) {
            float& dev = data->rgbDeviations[ch][stripData->pointIndex];
            dev += notches * RGB_DEV_STEP;
            float maxDev = 1.0f + (float)GRAYSCALE_RANGE / 100.0f;
            float minDev = 1.0f - (float)GRAYSCALE_RANGE / 100.0f;
            dev = (std::max)(minDev, (std::min)(maxDev, dev));
            data->selectedChannel = ch;
            UpdateRGBEditBoxes();
            InvalidateRect(hwnd, nullptr, FALSE);
            FireLiveUpdate();
        }
        return 0;
    }

    case WM_DESTROY:
        if (stripData) { delete stripData; SetWindowLongPtr(hwnd, GWLP_USERDATA, 0); }
        return 0;
    }

    return DefWindowProc(hwnd, msg, wParam, lParam);
}

// ============================================================================
// Helpers
// ============================================================================

static void FireLiveUpdate() {
    auto* data = g_grayscaleEditor;
    if (!data) return;
    if (data->liveUpdateCallback) {
        data->liveUpdateCallback();
    } else if (g_gui.currentMonitor >= 0) {
        // HDR editing uses ICtCp mode for perceptually accurate preview (no hue shifts)
        UpdateColorCorrectionLive(g_gui.currentMonitor, data->isHDR, data->isHDR);
    }
}

static void UpdateRGBEditBoxes() {
    auto* data = g_grayscaleEditor;
    if (!data) return;
    int ch = data->selectedChannel;
    if (ch < 0) return;

    for (int i = 0; i < data->pointCount && i < (int)data->rgbEditBoxes.size(); i++) {
        float dev = (data->rgbDeviations[ch] && i < data->pointCount)
            ? data->rgbDeviations[ch][i] : 1.0f;
        float pct = (dev - 1.0f) * 100.0f;
        wchar_t text[16];
        swprintf_s(text, L"%.2f", pct);
        SetWindowText(data->rgbEditBoxes[i], text);
    }
    InvalidateIndicators();
}

static void InvalidateAllStrips() {
    auto* data = g_grayscaleEditor;
    if (!data) return;
    for (HWND strip : data->rgbStrips) {
        if (strip) InvalidateRect(strip, nullptr, FALSE);
    }
}

static void InvalidateIndicators() {
    for (HWND ind : s_rgbIndicators) {
        if (ind) InvalidateRect(ind, nullptr, TRUE);
    }
    // Recreate brush for current channel
    if (s_indicatorBrush) { DeleteObject(s_indicatorBrush); s_indicatorBrush = nullptr; }
    auto* data = g_grayscaleEditor;
    int ch = data ? data->selectedChannel : 0;
    if (ch < 0) ch = 0;
    s_indicatorBrush = CreateSolidBrush(kStripColors[ch]);
}

static void ApplyRGBEditValue(int pointIndex) {
    auto* data = g_grayscaleEditor;
    if (!data || pointIndex < 0 || pointIndex >= data->pointCount) return;
    int ch = data->selectedChannel;
    if (ch < 0) return;
    if (pointIndex >= (int)data->rgbEditBoxes.size()) return;

    wchar_t text[16];
    GetWindowText(data->rgbEditBoxes[pointIndex], text, 16);
    float pct = (float)_wtof(text);
    pct = (std::max)(-(float)GRAYSCALE_RANGE, (std::min)((float)GRAYSCALE_RANGE, pct));
    float dev = 1.0f + pct / 100.0f;

    if (data->rgbDeviations[ch] && pointIndex < data->pointCount) {
        data->rgbDeviations[ch][pointIndex] = dev;
        // Reformat
        wchar_t fmt[16];
        swprintf_s(fmt, L"%.2f", pct);
        SetWindowText(data->rgbEditBoxes[pointIndex], fmt);
        InvalidateAllStrips();
        FireLiveUpdate();
    }
}

// ============================================================================
// Main editor window procedure
// ============================================================================

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

        // Create sliders, labels, edit boxes, RGB strips, and RGB edit boxes
        for (int i = 0; i < data->pointCount; i++) {
            int x = startX + i * (sliderW + pad);

            float t = (float)i / (float)(data->pointCount - 1);
            float inputNorm = data->isHDR ? (t * pqPeak) : (t * t);

            // Top label: code value
            int codeValue = data->isHDR ? (int)(inputNorm * 1023.0f + 0.5f) : (int)(inputNorm * 255.0f + 0.5f);
            wchar_t rgbLabel[8];
            swprintf_s(rgbLabel, L"%d", codeValue);
            CreateWindow(L"STATIC", rgbLabel, WS_CHILD | WS_VISIBLE | SS_CENTER,
                x, startY, sliderW, rgbLabelH, hwnd, nullptr, nullptr, nullptr);

            // Vertical trackbar (slider)
            HWND slider = CreateWindow(TRACKBAR_CLASS, nullptr,
                WS_CHILD | WS_VISIBLE | TBS_VERT | TBS_AUTOTICKS | TBS_BOTH,
                x, startY + rgbLabelH, sliderW, sliderH,
                hwnd, (HMENU)(INT_PTR)(ID_GRAYSCALE_SLIDER_BASE + i), nullptr, nullptr);

            int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;
            SendMessage(slider, TBM_SETRANGE, TRUE, MAKELONG(-maxRange, maxRange));
            SendMessage(slider, TBM_SETTICFREQ, GRAYSCALE_SLIDER_SCALE * 5, 0);

            float targetVal = data->isHDR ? t : inputNorm;
            float currentVal = data->points[i];
            float deviationPct;
            if (targetVal > 0.001f) {
                deviationPct = ((currentVal / targetVal) - 1.0f) * 100.0f;
            } else {
                deviationPct = (currentVal - targetVal) * 100.0f;
            }
            int sliderVal = (int)(deviationPct * GRAYSCALE_SLIDER_SCALE + 0.5f);
            sliderVal = (std::max)(-maxRange, (std::min)(maxRange, sliderVal));
            SendMessage(slider, TBM_SETPOS, TRUE, -sliderVal);

            data->sliders.push_back(slider);

            // Bottom label: percentage of range
            wchar_t pctLabel[8];
            int pct = (int)(t * 100.0f + 0.5f);
            swprintf_s(pctLabel, L"%d%%", pct);
            CreateWindow(L"STATIC", pctLabel, WS_CHILD | WS_VISIBLE | SS_CENTER,
                x, startY + rgbLabelH + sliderH + 2, sliderW, pctLabelH, hwnd, nullptr, nullptr, nullptr);

            // Edit box for luminance deviation
            wchar_t editText[16];
            swprintf_s(editText, L"%.2f", deviationPct);
            HWND edit = CreateWindowEx(WS_EX_CLIENTEDGE, L"EDIT", editText,
                WS_CHILD | WS_VISIBLE | ES_CENTER,
                x, startY + rgbLabelH + sliderH + pctLabelH + 4, sliderW, editH,
                hwnd, (HMENU)(INT_PTR)(ID_GRAYSCALE_EDIT_BASE + i), nullptr, nullptr);
            SetNumericEdit(edit, 2);
            data->editBoxes.push_back(edit);

            // RGB strip below edit box (3 vertical colored bars)
            int stripY = startY + rgbLabelH + sliderH + pctLabelH + editH + 6;
            HWND strip = CreateWindow(L"DesktopLUT_RGBStrip", nullptr,
                WS_CHILD | WS_VISIBLE,
                x, stripY, sliderW, RGB_STRIP_H,
                hwnd, (HMENU)(INT_PTR)(ID_GRAYSCALE_RGB_STRIP_BASE + i), nullptr, nullptr);
            RGBStripData* sd = new RGBStripData{ i };
            SetWindowLongPtr(strip, GWLP_USERDATA, (LONG_PTR)sd);
            data->rgbStrips.push_back(strip);

            // Per-point RGB edit box below strip
            int rgbEditY = stripY + RGB_STRIP_H + 2;
            int ch = data->selectedChannel;
            float dev = (ch >= 0 && data->rgbDeviations[ch]) ? data->rgbDeviations[ch][i] : 1.0f;
            float devPct = (dev - 1.0f) * 100.0f;
            wchar_t rgbEditText[16];
            swprintf_s(rgbEditText, L"%.2f", devPct);
            HWND rgbEdit = CreateWindowEx(WS_EX_CLIENTEDGE, L"EDIT", rgbEditText,
                WS_CHILD | WS_VISIBLE | ES_CENTER,
                x, rgbEditY, sliderW, editH,
                hwnd, (HMENU)(INT_PTR)(ID_GRAYSCALE_RGB_EDIT_BASE + i), nullptr, nullptr);
            SetNumericEdit(rgbEdit, 2);
            data->rgbEditBoxes.push_back(rgbEdit);

            // Channel color indicator bar below RGB edit
            HWND indicator = CreateWindow(L"STATIC", L"", WS_CHILD | WS_VISIBLE,
                x, rgbEditY + editH, sliderW, RGB_INDICATOR_H,
                hwnd, nullptr, nullptr, nullptr);
            s_rgbIndicators.push_back(indicator);
        }

        // Calculate dialog content width
        int dialogContentW = startX * 2 + data->pointCount * (sliderW + pad) + 40;

        // R/G/B channel selector labels (right side, aligned with strips)
        int stripY = startY + rgbLabelH + sliderH + pctLabelH + editH + 6;
        int labelX = dialogContentW - 36;
        const wchar_t* chLabels[] = { L"R", L"G", L"B" };
        for (int ch = 0; ch < 3; ch++) {
            int barCenterY = stripY + (int)((1.0f) * 0.5f * (RGB_STRIP_H - 1));
            // Position labels at top/middle/bottom of strip area
            int labelY = stripY + ch * (RGB_STRIP_H / 3);
            CreateWindow(L"STATIC", chLabels[ch], WS_CHILD | WS_VISIBLE | SS_CENTER,
                labelX, labelY, 20, 16, hwnd, nullptr, nullptr, nullptr);
        }

        // Initialize indicator brush for default channel
        if (s_indicatorBrush) { DeleteObject(s_indicatorBrush); s_indicatorBrush = nullptr; }
        s_indicatorBrush = CreateSolidBrush(kStripColors[data->selectedChannel >= 0 ? data->selectedChannel : 0]);

        // Buttons
        int rgbEditY = stripY + RGB_STRIP_H + 2;
        int btnY = rgbEditY + editH + RGB_INDICATOR_H + 8;
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

        // Set font for all controls
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

    case WM_MOUSEWHEEL: {
        // Handle hover-scroll on edit boxes and RGB edit boxes
        auto* data = g_grayscaleEditor;
        if (!data) break;

        POINT pt = { GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
        HWND hwndUnder = WindowFromPoint(pt);

        // Check if hovering over a luminance edit box
        for (int i = 0; i < data->pointCount; i++) {
            if (data->editBoxes[i] == hwndUnder) {
                static int editWheelAccum = 0;
                editWheelAccum += GET_WHEEL_DELTA_WPARAM(wParam);
                int notches = editWheelAccum / WHEEL_DELTA;
                if (notches == 0) return 0;
                editWheelAccum -= notches * WHEEL_DELTA;

                int pos = (int)SendMessage(data->sliders[i], TBM_GETPOS, 0, 0);
                pos -= notches;
                int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;
                pos = (std::max)(-maxRange, (std::min)(maxRange, pos));
                SendMessage(data->sliders[i], TBM_SETPOS, TRUE, pos);
                UpdateEditFromSlider(i);

                float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;
                float t = (float)i / (float)(data->pointCount - 1);
                float targetVal = data->isHDR ? t : (t * t);
                float newVal;
                if (targetVal > 0.001f)
                    newVal = targetVal * (1.0f + deviationPct / 100.0f);
                else
                    newVal = targetVal + (deviationPct / 100.0f);
                data->points[i] = (std::max)(0.0f, (std::min)(1.0f, newVal));
                FireLiveUpdate();
                return 0;
            }
        }

        // Check if hovering over a per-point RGB edit box
        for (int i = 0; i < data->pointCount && i < (int)data->rgbEditBoxes.size(); i++) {
            if (data->rgbEditBoxes[i] == hwndUnder) {
                int ch = data->selectedChannel;
                if (ch < 0) break;

                static int rgbEditWheelAccum = 0;
                rgbEditWheelAccum += GET_WHEEL_DELTA_WPARAM(wParam);
                int notches = rgbEditWheelAccum / WHEEL_DELTA;
                if (notches == 0) return 0;
                rgbEditWheelAccum -= notches * WHEEL_DELTA;

                if (data->rgbDeviations[ch] && i < data->pointCount) {
                    float& dev = data->rgbDeviations[ch][i];
                    dev += notches * RGB_DEV_STEP;
                    float maxDev = 1.0f + (float)GRAYSCALE_RANGE / 100.0f;
                    float minDev = 1.0f - (float)GRAYSCALE_RANGE / 100.0f;
                    dev = (std::max)(minDev, (std::min)(maxDev, dev));
                    // Update just this edit box
                    wchar_t text[16];
                    swprintf_s(text, L"%.2f", (dev - 1.0f) * 100.0f);
                    SetWindowText(data->rgbEditBoxes[i], text);
                    InvalidateAllStrips();
                    FireLiveUpdate();
                }
                return 0;
            }
        }

        break;
    }

    case WM_PARENTNOTIFY: {
        if (LOWORD(wParam) != WM_MBUTTONDOWN) break;
        auto* data = g_grayscaleEditor;
        if (!data) break;

        // Find which child was middle-clicked
        POINT pt = { GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam) };
        HWND child = ChildWindowFromPoint(hwnd, pt);
        if (!child) break;

        // Luminance edit box: reset deviation to 0% (slider centered)
        for (int i = 0; i < data->pointCount; i++) {
            if (data->editBoxes[i] == child) {
                SendMessage(data->sliders[i], TBM_SETPOS, TRUE, 0);
                UpdateEditFromSlider(i);
                float t = (float)i / (float)(data->pointCount - 1);
                data->points[i] = data->isHDR ? t : (t * t);
                FireLiveUpdate();
                return 0;
            }
        }

        // RGB edit box: reset selected channel's deviation to 1.0
        for (int i = 0; i < data->pointCount && i < (int)data->rgbEditBoxes.size(); i++) {
            if (data->rgbEditBoxes[i] == child) {
                int ch = data->selectedChannel;
                if (ch >= 0 && data->rgbDeviations[ch]) {
                    data->rgbDeviations[ch][i] = 1.0f;
                    wchar_t text[16];
                    swprintf_s(text, L"%.2f", 0.0f);
                    SetWindowText(data->rgbEditBoxes[i], text);
                    InvalidateAllStrips();
                    FireLiveUpdate();
                }
                return 0;
            }
        }
        break;
    }

    case WM_VSCROLL: {
        HWND sliderHwnd = (HWND)lParam;
        auto* data = g_grayscaleEditor;
        if (data) {
            for (int i = 0; i < data->pointCount; i++) {
                if (data->sliders[i] == sliderHwnd) {
                    UpdateEditFromSlider(i);
                    int pos = (int)SendMessage(sliderHwnd, TBM_GETPOS, 0, 0);
                    float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;
                    float t = (float)i / (float)(data->pointCount - 1);
                    float targetVal = data->isHDR ? t : (t * t);
                    float newVal;
                    if (targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);
                    }
                    data->points[i] = (std::max)(0.0f, (std::min)(1.0f, newVal));
                    FireLiveUpdate();
                    break;
                }
            }
        }
        return 0;
    }

    case WM_COMMAND: {
        WORD code = HIWORD(wParam);
        WORD id = LOWORD(wParam);

        // Luminance edit box losing focus
        if (code == EN_KILLFOCUS && id >= ID_GRAYSCALE_EDIT_BASE &&
            id < ID_GRAYSCALE_EDIT_BASE + 32) {
            auto* data = g_grayscaleEditor;
            if (data) {
                int editIndex = id - ID_GRAYSCALE_EDIT_BASE;
                if (editIndex >= 0 && editIndex < data->pointCount) {
                    UpdateSliderFromEdit(editIndex);
                    UpdateEditFromSlider(editIndex);
                    int pos = (int)SendMessage(data->sliders[editIndex], TBM_GETPOS, 0, 0);
                    float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;
                    float t = (float)editIndex / (float)(data->pointCount - 1);
                    float targetVal = data->isHDR ? t : (t * t);
                    float newVal;
                    if (targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);
                    }
                    data->points[editIndex] = (std::max)(0.0f, (std::min)(1.0f, newVal));
                    FireLiveUpdate();
                }
            }
            return 0;
        }

        // Per-point RGB edit box losing focus
        if (code == EN_KILLFOCUS && id >= ID_GRAYSCALE_RGB_EDIT_BASE &&
            id < ID_GRAYSCALE_RGB_EDIT_BASE + 32) {
            ApplyRGBEditValue(id - ID_GRAYSCALE_RGB_EDIT_BASE);
            return 0;
        }

        switch (id) {
        case ID_GRAYSCALE_OK: {
            auto* data = g_grayscaleEditor;
            if (data) {
                for (int i = 0; i < data->pointCount; i++) {
                    int pos = (int)SendMessage(data->sliders[i], TBM_GETPOS, 0, 0);
                    float deviationPct = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;
                    float t = (float)i / (float)(data->pointCount - 1);
                    float targetVal = data->isHDR ? t : (t * t);
                    float newVal;
                    if (targetVal > 0.001f) {
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
            auto* data = g_grayscaleEditor;
            if (data && !data->originalPoints.empty()) {
                for (int i = 0; i < data->pointCount && i < (int)data->originalPoints.size(); i++) {
                    data->points[i] = data->originalPoints[i];
                }
                // Restore RGB deviations
                for (int ch = 0; ch < 3; ch++) {
                    if (data->rgbDeviations[ch] && !data->originalRGBDev[ch].empty()) {
                        for (int i = 0; i < data->pointCount && i < (int)data->originalRGBDev[ch].size(); i++) {
                            data->rgbDeviations[ch][i] = data->originalRGBDev[ch][i];
                        }
                    }
                }
                FireLiveUpdate();
            }
            DestroyWindow(hwnd);
            return 0;
        }
        }
        break;
    }

    case WM_ERASEBKGND: {
        HDC hdc = (HDC)wParam;
        RECT rc;
        GetClientRect(hwnd, &rc);
        if (!g_tabBgBrush) g_tabBgBrush = CreateSolidBrush(TAB_BG_COLOR);
        FillRect(hdc, &rc, g_tabBgBrush);
        return 1;
    }

    case WM_CTLCOLORSTATIC: {
        HDC hdc = (HDC)wParam;
        HWND hwndStatic = (HWND)lParam;
        // Channel color indicator bars
        for (HWND ind : s_rgbIndicators) {
            if (ind == hwndStatic && s_indicatorBrush) {
                auto* data = g_grayscaleEditor;
                int ch = data ? data->selectedChannel : 0;
                if (ch < 0) ch = 0;
                SetBkColor(hdc, kStripColors[ch]);
                return (LRESULT)s_indicatorBrush;
            }
        }
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
        s_rgbIndicators.clear();
        if (s_indicatorBrush) { DeleteObject(s_indicatorBrush); s_indicatorBrush = nullptr; }
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
        if (isHDR) settings.initLinearPQ();
        else settings.initLinear();
    }
    // Ensure RGB deviations are initialized
    for (int ch = 0; ch < 3; ch++) {
        if (settings.rgbDeviations[ch].empty() ||
            (int)settings.rgbDeviations[ch].size() != settings.pointCount) {
            settings.rgbDeviations[ch].assign(settings.pointCount, 1.0f);
        }
    }

    // Register window classes if needed
    static bool registered = false;
    if (!registered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = GrayscaleEditorProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = L"DesktopLUT_GrayscaleEditor";
        RegisterClassEx(&wc);

        // Register RGB strip class
        WNDCLASSEX wcStrip = { sizeof(WNDCLASSEX) };
        wcStrip.lpfnWndProc = RGBStripProc;
        wcStrip.hInstance = GetModuleHandle(nullptr);
        wcStrip.hCursor = LoadCursor(nullptr, IDC_HAND);
        wcStrip.hbrBackground = nullptr;
        wcStrip.lpszClassName = L"DesktopLUT_RGBStrip";
        RegisterClassEx(&wcStrip);

        registered = true;
    }

    // Setup editor data
    GrayscaleEditorData data;
    data.pointCount = settings.pointCount;
    data.points = settings.points.data();
    data.isHDR = isHDR;
    data.peakNits = settings.peakNits;
    data.originalPoints.assign(settings.points.begin(), settings.points.end());
    data.liveUpdateCallback = liveUpdateCallback;
    data.selectedChannel = 0;  // Default to Red
    // Setup per-channel deviation pointers and backups
    for (int ch = 0; ch < 3; ch++) {
        data.rgbDeviations[ch] = settings.rgbDeviations[ch].data();
        data.originalRGBDev[ch].assign(settings.rgbDeviations[ch].begin(),
                                        settings.rgbDeviations[ch].end());
    }
    g_grayscaleEditor = &data;

    // Calculate window size (must match layout in GrayscaleEditorProc)
    int sliderW = 32;
    int sliderH = 150;
    int rgbLabelH = 16;
    int pctLabelH = 16;
    int editH = 20;
    int pad = 2;
    int btnH = 28;
    int startY = 10;

    int contentW = 20 + settings.pointCount * (sliderW + pad) + 40;
    // Height: labels + slider + pctLabel + editBox + strip + rgbEdit + indicator + buttons
    int contentH = startY + rgbLabelH + sliderH + pctLabelH + editH + 6
                   + RGB_STRIP_H + 2 + editH + RGB_INDICATOR_H + 8 + btnH + 12;

    RECT rc = { 0, 0, contentW, contentH };
    AdjustWindowRect(&rc, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, FALSE);
    int winW = rc.right - rc.left;
    int winH = rc.bottom - rc.top;

    RECT parentRect;
    GetWindowRect(hwndParent, &parentRect);
    int x = parentRect.left + (parentRect.right - parentRect.left - winW) / 2;
    int y = parentRect.top + (parentRect.bottom - parentRect.top - winH) / 2;

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
        if (bRet == -1) break;
        if (msg.message == WM_QUIT) { PostQuitMessage((int)msg.wParam); break; }
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);

    // Revert ICtCp editing mode → PQ/linear gains (saves 6 pow/pixel when tonemap off)
    // Only for direct Corrections tab usage (no callback); MHC path handles its own revert
    if (data.isHDR && !data.liveUpdateCallback && g_gui.currentMonitor >= 0) {
        UpdateColorCorrectionLive(g_gui.currentMonitor, true, false);
    }

    g_grayscaleEditor = nullptr;
}
