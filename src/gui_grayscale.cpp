// DesktopLUT - gui_grayscale.cpp
// Grayscale correction editor dialog

#include "gui.h"
#include "gui_shared.h"
#include "globals.h"
#include "processing.h"
#include <commctrl.h>
#include <algorithm>

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

            // Range: +/-2500 (representing +/-25.00% with 0.01 precision)
            int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;
            SendMessage(slider, TBM_SETRANGE, TRUE, MAKELONG(-maxRange, maxRange));
            SendMessage(slider, TBM_SETTICFREQ, GRAYSCALE_SLIDER_SCALE * 5, 0);  // Tick every 5%

            // Calculate current deviation from target
            // HDR: points store fraction of pqPeak (0-1), targetVal = t
            // SDR: points store actual output values, targetVal = inputNorm (sqrt distribution)
            float targetVal = data->isHDR ? t : inputNorm;
            float currentVal = data->points[i];
            // Proportional deviation: slider % = proportional change from target
            // Consistent for both HDR and SDR, matches MHC grayscale editor
            float deviationPct;
            if (targetVal > 0.001f) {
                deviationPct = ((currentVal / targetVal) - 1.0f) * 100.0f;  // Proportional
            } else {
                deviationPct = (currentVal - targetVal) * 100.0f;  // Additive for near-zero
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
                    // Proportional deviation for both HDR and SDR (matches MHC editor)
                    float newVal;
                    if (targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);  // Proportional
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);  // Additive for near-zero
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
                    // Proportional deviation for both HDR and SDR (matches MHC editor)
                    float newVal;
                    if (targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);  // Additive for near-zero
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
                    // Proportional deviation for both HDR and SDR (matches MHC editor)
                    float newVal;
                    if (targetVal > 0.001f) {
                        newVal = targetVal * (1.0f + deviationPct / 100.0f);
                    } else {
                        newVal = targetVal + (deviationPct / 100.0f);  // Additive for near-zero
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
        if (msg.message == WM_QUIT) { PostQuitMessage((int)msg.wParam); break; }
        TranslateMessage(&msg);
        DispatchMessage(&msg);
    }
    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);

    // Ensure global pointer is cleared (WM_DESTROY should do this, but be safe)
    g_grayscaleEditor = nullptr;
}
