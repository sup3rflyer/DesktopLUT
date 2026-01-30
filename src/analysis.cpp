// DesktopLUT - analysis.cpp
// Real-time frame analysis overlay

#include "analysis.h"
#include "globals.h"
#include "shader.h"
#include <iostream>
#include <iomanip>
#include <sstream>
#include <atomic>

// Window class name for analysis overlay
static const wchar_t* g_analysisClassName = L"DesktopLUT_Analysis";

// Timing constants
static const int ANALYSIS_DISPATCH_INTERVAL = 30;  // Dispatch every 30 frames (~0.5 sec at 60Hz)
static const int ANALYSIS_READBACK_DELAY = 2;      // Read back 2 frames after dispatch

// Custom message for async UI update (offloads formatting from render thread)
static const UINT WM_UPDATE_ANALYSIS = WM_USER + 1;

// Data passed to UI thread for formatting
struct AnalysisDisplayData {
    AnalysisResult result;
    bool isHDR;
    float targetPeak;
    float sessionMaxCLL;
    float sessionMaxFALL;
    // Tonemap state for TM indicator
    bool tonemapEnabled;
    bool tonemapDynamic;
    float tonemapSourcePeak;   // Static mode: configured source peak
    float tonemapTargetPeak;   // Target peak (display capability)
    float detectedPeak;        // Dynamic mode: GPU-detected peak
};
static AnalysisDisplayData g_pendingAnalysis = {};
static std::atomic<bool> g_analysisDataReady{false};

// Analysis overlay window procedure
static LRESULT CALLBACK AnalysisWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);

        // Get client rect
        RECT rc;
        GetClientRect(hwnd, &rc);

        // Double buffering: create memory DC and bitmap
        HDC memDC = CreateCompatibleDC(hdc);
        HBITMAP memBitmap = CreateCompatibleBitmap(hdc, rc.right, rc.bottom);
        HBITMAP oldBitmap = (HBITMAP)SelectObject(memDC, memBitmap);

        // Fill background
        HBRUSH bgBrush = CreateSolidBrush(RGB(32, 32, 32));
        FillRect(memDC, &rc, bgBrush);
        DeleteObject(bgBrush);

        // Draw border
        HPEN borderPen = CreatePen(PS_SOLID, 1, RGB(80, 80, 80));
        HPEN oldPen = (HPEN)SelectObject(memDC, borderPen);
        HBRUSH oldBrush = (HBRUSH)SelectObject(memDC, GetStockObject(NULL_BRUSH));
        Rectangle(memDC, 0, 0, rc.right, rc.bottom);
        SelectObject(memDC, oldPen);
        SelectObject(memDC, oldBrush);
        DeleteObject(borderPen);

        // Get stored text from window property
        wchar_t* text = (wchar_t*)GetProp(hwnd, L"AnalysisText");
        if (text) {
            // Draw text
            SetBkMode(memDC, TRANSPARENT);
            HFONT font = CreateFont(16, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                CLEARTYPE_QUALITY, FIXED_PITCH | FF_MODERN, L"Consolas");
            HFONT oldFont = (HFONT)SelectObject(memDC, font);

            // Parse and draw lines with different colors
            RECT textRc = rc;
            textRc.left += 10;
            textRc.top += 8;
            textRc.right -= 10;

            // Split text by newlines and draw with appropriate colors
            std::wstring str(text);
            std::wistringstream stream(str);
            std::wstring line;
            int lineHeight = 18;

            while (std::getline(stream, line)) {
                // Section headers in gray
                if (line.find(L"ANALYSIS") != std::wstring::npos ||
                    line.find(L"GAMUT") != std::wstring::npos ||
                    line.find(L"HISTOGRAM") != std::wstring::npos ||
                    line.find(L"CLIPPING") != std::wstring::npos ||
                    line.find(L"SESSION") != std::wstring::npos ||
                    line.find(L"---") != std::wstring::npos) {
                    SetTextColor(memDC, RGB(180, 180, 180));
                    DrawText(memDC, line.c_str(), -1, &textRc, DT_LEFT | DT_SINGLELINE);
                } else if (line.find(L"TM: ~") != std::wstring::npos ||
                           line.find(L"TM: <") != std::wstring::npos) {
                    // Line with TM indicator - draw Peak part white, TM part colored
                    size_t tmPos = line.find(L"TM:");
                    if (tmPos != std::wstring::npos) {
                        // Draw first part (Peak) in white
                        std::wstring peakPart = line.substr(0, tmPos);
                        SetTextColor(memDC, RGB(255, 255, 255));
                        DrawText(memDC, peakPart.c_str(), -1, &textRc, DT_LEFT | DT_SINGLELINE);

                        // Calculate width of first part to offset TM part
                        SIZE textSize;
                        GetTextExtentPoint32(memDC, peakPart.c_str(), (int)peakPart.length(), &textSize);

                        // Draw TM part in appropriate color
                        std::wstring tmPart = line.substr(tmPos);
                        RECT tmRc = textRc;
                        tmRc.left += textSize.cx;
                        if (line.find(L"TM: ~") != std::wstring::npos) {
                            SetTextColor(memDC, RGB(255, 200, 100));  // Yellow - compressing
                        } else {
                            SetTextColor(memDC, RGB(100, 255, 100));  // Green - below threshold
                        }
                        DrawText(memDC, tmPart.c_str(), -1, &tmRc, DT_LEFT | DT_SINGLELINE);
                    }
                } else {
                    SetTextColor(memDC, RGB(255, 255, 255));
                    DrawText(memDC, line.c_str(), -1, &textRc, DT_LEFT | DT_SINGLELINE);
                }
                textRc.top += lineHeight;
            }

            SelectObject(memDC, oldFont);
            DeleteObject(font);
        }

        // Blit to screen in one operation
        BitBlt(hdc, 0, 0, rc.right, rc.bottom, memDC, 0, 0, SRCCOPY);

        // Cleanup
        SelectObject(memDC, oldBitmap);
        DeleteObject(memBitmap);
        DeleteDC(memDC);

        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_UPDATE_ANALYSIS: {
        // Format text on UI thread (offloaded from render thread)
        if (!g_analysisDataReady.load()) return 0;

        AnalysisDisplayData data = g_pendingAnalysis;  // Copy
        g_analysisDataReady.store(false);

        std::wstringstream ss;

        if (data.isHDR) {
            ss << L" ANALYSIS (HDR)\n";
            ss << L"--------------------\n";
            ss << std::fixed << std::setprecision(1);
            float totalF = (float)data.result.totalPixels;
            // Format TM indicator based on tonemap state
            // Colors: Off=white, <threshold=green, ~compressing=yellow
            std::wstring tmStr;
            if (!data.tonemapEnabled) {
                tmStr = L"Off";
            } else if (!data.tonemapDynamic) {
                // Static mode: show source peak, color based on content vs source
                std::wstringstream tmss;
                tmss << std::fixed << std::setprecision(0);
                if (data.result.peakNits > data.tonemapSourcePeak) {
                    // Content exceeds configured source - clipping (~ prefix, yellow)
                    tmss << L"~" << data.tonemapSourcePeak;
                } else {
                    // Content within range (< prefix, green)
                    tmss << L"<" << data.tonemapSourcePeak;
                }
                tmStr = tmss.str();
            } else {
                // Dynamic mode: show detected peak or target threshold
                std::wstringstream tmss;
                tmss << std::fixed << std::setprecision(0);
                if (data.detectedPeak > data.tonemapTargetPeak) {
                    // Above threshold - compressing (~ prefix, yellow)
                    tmss << L"~" << data.detectedPeak;
                } else {
                    // Below threshold - passing through (< prefix, green)
                    tmss << L"<" << data.tonemapTargetPeak;
                }
                tmStr = tmss.str();
            }
            ss << L" Peak: " << std::setw(7) << data.result.peakNits << L"  TM: " << tmStr << L"\n";
            // Show Min>0 (if all pixels were black, show 0)
            float minNonZero = (data.result.minNonZeroNits < 99999.0f) ? data.result.minNonZeroNits : 0.0f;
            ss << L" Avg:  " << std::setw(7) << data.result.avgNits << L"  Min>0:" << std::setprecision(3) << std::setw(6) << minNonZero << L"\n";
            ss << std::setprecision(1);
            // APL relative to target peak
            float apl = (data.result.avgNits / data.targetPeak) * 100.0f;
            ss << L" APL:  " << std::setw(6) << apl << L"%  Min:  " << std::setprecision(3) << std::setw(6) << data.result.minNits << L"\n";
            ss << std::setprecision(1);
            ss << L"\n";
            ss << L" GAMUT\n";
            if (totalF > 0) {
                ss << L"   Rec.709:  " << std::setw(5) << (data.result.pixelsRec709 / totalF * 100.0f) << L"%\n";
                ss << L"   P3-D65:   " << std::setw(5) << (data.result.pixelsP3Only / totalF * 100.0f) << L"%\n";
                ss << L"   Rec.2020: " << std::setw(5) << (data.result.pixelsRec2020Only / totalF * 100.0f) << L"%\n";
                ss << L"   Out:      " << std::setw(5) << (data.result.pixelsOutOfGamut / totalF * 100.0f) << L"%\n";
            }
            ss << L"\n";
            ss << L" HISTOGRAM\n";
            if (totalF > 0) {
                ss << L"   0-203:    " << std::setw(5) << (data.result.histogram[0] / totalF * 100.0f) << L"%\n";
                ss << L"   203-1k:   " << std::setw(5) << (data.result.histogram[1] / totalF * 100.0f) << L"%\n";
                ss << L"   1k-2k:    " << std::setw(5) << (data.result.histogram[2] / totalF * 100.0f) << L"%\n";
                ss << L"   2k-4k:    " << std::setw(5) << (data.result.histogram[3] / totalF * 100.0f) << L"%\n";
                ss << L"   4000+:    " << std::setw(5) << (data.result.histogram[4] / totalF * 100.0f) << L"%\n";
            }
            ss << L"\n";
            ss << L" SESSION\n";
            ss << L"   MaxCLL:  " << std::setw(6) << (int)data.sessionMaxCLL << L" nits\n";
            ss << L"   MaxFALL: " << std::setw(6) << (int)data.sessionMaxFALL << L" nits\n";
        } else {
            ss << L" ANALYSIS (SDR)\n";
            ss << L"--------------------\n";
            ss << std::fixed << std::setprecision(0);
            // Convert to 8-bit values for SDR display
            int peak8 = (int)(data.result.peakNits / 80.0f * 255.0f);
            int min8 = (int)(data.result.minNits / 80.0f * 255.0f);
            int avg8 = (int)(data.result.avgNits / 80.0f * 255.0f);
            ss << L" Peak: " << std::setw(3) << peak8 << L" (" << std::setprecision(2) << (data.result.peakNits / 80.0f) << L")\n";
            ss << std::setprecision(0);
            ss << L" Min:  " << std::setw(3) << min8 << L" (" << std::setprecision(2) << (data.result.minNits / 80.0f) << L")\n";
            ss << std::setprecision(0);
            ss << L" Avg:  " << std::setw(3) << avg8 << L" (" << std::setprecision(2) << (data.result.avgNits / 80.0f) << L")\n";
            ss << L"\n";
            ss << L" CLIPPING\n";
            float totalF = (float)data.result.totalPixels;
            if (totalF > 0) {
                ss << std::setprecision(1);
                ss << L"   Black (<1):   " << std::setw(5) << (data.result.pixelsClipBlack / totalF * 100.0f) << L"%\n";
                ss << L"   White (>254): " << std::setw(5) << (data.result.pixelsClipWhite / totalF * 100.0f) << L"%\n";
            }
            ss << L"\n";
            ss << L" GAMUT\n";
            if (totalF > 0) {
                ss << L"   sRGB:  " << std::setw(5) << (data.result.pixelsRec709 / totalF * 100.0f) << L"%\n";
                ss << L"   Wide:  " << std::setw(5) << ((data.result.pixelsP3Only + data.result.pixelsRec2020Only + data.result.pixelsOutOfGamut) / totalF * 100.0f) << L"%\n";
            }
        }

        // Update window
        std::wstring text = ss.str();
        wchar_t* textCopy = (wchar_t*)malloc((text.length() + 1) * sizeof(wchar_t));
        if (textCopy) {
            wcscpy_s(textCopy, text.length() + 1, text.c_str());

            wchar_t* oldText = (wchar_t*)GetProp(hwnd, L"AnalysisText");
            if (oldText) free(oldText);
            SetProp(hwnd, L"AnalysisText", textCopy);

            int height = data.isHDR ? 430 : 260;
            SetWindowPos(hwnd, nullptr, 0, 0, 260, height, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
            InvalidateRect(hwnd, nullptr, FALSE);  // FALSE = don't erase, prevents flicker
        }
        return 0;
    }
    case WM_DESTROY:
        // Free stored text
        {
            wchar_t* text = (wchar_t*)RemoveProp(hwnd, L"AnalysisText");
            if (text) free(text);
        }
        return 0;
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

bool CreateAnalysisOverlay(HINSTANCE hInstance) {
    // Register window class
    WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
    wc.lpfnWndProc = AnalysisWndProc;
    wc.hInstance = hInstance;
    wc.lpszClassName = g_analysisClassName;
    wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    RegisterClassEx(&wc);

    // Create window (initially hidden)
    // Position: top-right with 40px margin
    int width = 260;
    int height = 430;  // Tall enough for all HDR stats
    int margin = 40;
    int screenW = GetSystemMetrics(SM_CXSCREEN);
    int x = screenW - width - margin;
    int y = margin;

    g_analysisHwnd = CreateWindowEx(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        g_analysisClassName, L"",
        WS_POPUP,
        x, y, width, height,
        nullptr, nullptr, hInstance, nullptr);

    if (!g_analysisHwnd) {
        std::cerr << "Failed to create analysis overlay window" << std::endl;
        return false;
    }

    // Set window opacity (90% opaque)
    SetLayeredWindowAttributes(g_analysisHwnd, 0, 230, LWA_ALPHA);

    // Exclude from capture
    SetWindowDisplayAffinity(g_analysisHwnd, WDA_EXCLUDEFROMCAPTURE);

    std::cout << "Analysis overlay created" << std::endl;
    return true;
}

void DestroyAnalysisOverlay() {
    if (g_analysisHwnd) {
        DestroyWindow(g_analysisHwnd);
        g_analysisHwnd = nullptr;
    }
}

void ShowAnalysisOverlay() {
    if (g_analysisHwnd) {
        // Reset session tracking when overlay is opened
        for (auto& ctx : g_monitors) {
            ctx.sessionMaxCLL = 0.0f;
            ctx.sessionMaxFALL = 0.0f;
        }

        // Set initial placeholder text
        const wchar_t* initialText = L" ANALYSIS\n--------------------\n Collecting data...";
        wchar_t* textCopy = (wchar_t*)malloc((wcslen(initialText) + 1) * sizeof(wchar_t));
        if (textCopy) {
            wcscpy_s(textCopy, wcslen(initialText) + 1, initialText);
            wchar_t* oldText = (wchar_t*)GetProp(g_analysisHwnd, L"AnalysisText");
            if (oldText) free(oldText);
            SetProp(g_analysisHwnd, L"AnalysisText", textCopy);
        }

        // Show window and force to top of z-order
        SetWindowPos(g_analysisHwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE);

        // Force immediate repaint
        InvalidateRect(g_analysisHwnd, nullptr, TRUE);
        UpdateWindow(g_analysisHwnd);

        g_analysisEnabled.store(true);
        std::cout << "Analysis overlay shown" << std::endl;
    }
}

void HideAnalysisOverlay() {
    if (g_analysisHwnd) {
        ShowWindow(g_analysisHwnd, SW_HIDE);
        g_analysisEnabled.store(false);
        std::cout << "Analysis overlay hidden" << std::endl;
    }
}

void ToggleAnalysisOverlay() {
    if (g_analysisEnabled.load()) {
        HideAnalysisOverlay();
    } else {
        ShowAnalysisOverlay();
    }
}

bool IsAnalysisOverlayVisible() {
    return g_analysisEnabled.load();
}

bool CreateAnalysisResources(MonitorContext* ctx) {
    if (!g_analysisCS || !g_analysisCB) {
        return false;  // Compute shader not available
    }

    // Create structured buffer for analysis results (16 uint elements = 64 bytes)
    D3D11_BUFFER_DESC bufDesc = {};
    bufDesc.ByteWidth = 16 * sizeof(uint32_t);  // 16 uint elements
    bufDesc.Usage = D3D11_USAGE_DEFAULT;
    bufDesc.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bufDesc.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    bufDesc.StructureByteStride = sizeof(uint32_t);  // Each element is a uint

    HRESULT hr = g_device->CreateBuffer(&bufDesc, nullptr, &ctx->analysisBuffer);
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " failed to create analysis buffer: 0x"
                  << std::hex << hr << std::dec << std::endl;
        return false;
    }

    // Create UAV for 16 uint elements
    D3D11_UNORDERED_ACCESS_VIEW_DESC uavDesc = {};
    uavDesc.Format = DXGI_FORMAT_UNKNOWN;
    uavDesc.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
    uavDesc.Buffer.FirstElement = 0;
    uavDesc.Buffer.NumElements = 16;

    hr = g_device->CreateUnorderedAccessView(ctx->analysisBuffer, &uavDesc, &ctx->analysisUAV);
    if (FAILED(hr)) {
        std::cerr << "Monitor " << ctx->index << " failed to create analysis UAV: 0x"
                  << std::hex << hr << std::dec << std::endl;
        ctx->analysisBuffer->Release();
        ctx->analysisBuffer = nullptr;
        return false;
    }

    // Create double-buffered staging buffers for async readback
    D3D11_BUFFER_DESC stagingDesc = {};
    stagingDesc.ByteWidth = 16 * sizeof(uint32_t);  // 16 uint elements
    stagingDesc.Usage = D3D11_USAGE_STAGING;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;

    for (int i = 0; i < 2; i++) {
        hr = g_device->CreateBuffer(&stagingDesc, nullptr, &ctx->analysisStagingBuffer[i]);
        if (FAILED(hr)) {
            std::cerr << "Monitor " << ctx->index << " failed to create analysis staging buffer " << i << std::endl;
            // Clean up
            if (ctx->analysisUAV) { ctx->analysisUAV->Release(); ctx->analysisUAV = nullptr; }
            if (ctx->analysisBuffer) { ctx->analysisBuffer->Release(); ctx->analysisBuffer = nullptr; }
            for (int j = 0; j < i; j++) {
                if (ctx->analysisStagingBuffer[j]) {
                    ctx->analysisStagingBuffer[j]->Release();
                    ctx->analysisStagingBuffer[j] = nullptr;
                }
            }
            return false;
        }
    }

    std::cout << "Monitor " << ctx->index << " analysis resources created" << std::endl;
    return true;
}

void ReleaseAnalysisResources(MonitorContext* ctx) {
    if (ctx->analysisUAV) { ctx->analysisUAV->Release(); ctx->analysisUAV = nullptr; }
    if (ctx->analysisBuffer) { ctx->analysisBuffer->Release(); ctx->analysisBuffer = nullptr; }
    for (int i = 0; i < 2; i++) {
        if (ctx->analysisStagingBuffer[i]) {
            ctx->analysisStagingBuffer[i]->Release();
            ctx->analysisStagingBuffer[i] = nullptr;
        }
    }
}

void DispatchAnalysisCompute(MonitorContext* ctx) {
    if (!g_analysisCS || !g_analysisCB || !ctx->captureSRV) return;

    // Create resources on first use
    if (!ctx->analysisBuffer) {
        if (!CreateAnalysisResources(ctx)) {
            return;
        }
    }

    // Only dispatch every N frames
    ctx->analysisFrameCounter++;
    int frameInCycle = ctx->analysisFrameCounter % ANALYSIS_DISPATCH_INTERVAL;

    if (frameInCycle == 0) {
        // Update constant buffer with frame dimensions and HDR state
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (SUCCEEDED(g_context->Map(g_analysisCB, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
            uint32_t* udata = (uint32_t*)mapped.pData;
            udata[0] = (uint32_t)ctx->width;
            udata[1] = (uint32_t)ctx->height;
            udata[2] = ctx->isHDREnabled ? 1 : 0;
            udata[3] = 0;  // pad
            g_context->Unmap(g_analysisCB, 0);
        }

        // Clear the analysis buffer (reset counters)
        UINT clearVal[4] = { 0, 0, 0, 0 };
        g_context->ClearUnorderedAccessViewUint(ctx->analysisUAV, clearVal);

        // Dispatch compute shader
        g_context->CSSetShader(g_analysisCS, nullptr, 0);
        g_context->CSSetConstantBuffers(0, 1, &g_analysisCB);
        g_context->CSSetShaderResources(0, 1, &ctx->captureSRV);
        g_context->CSSetUnorderedAccessViews(0, 1, &ctx->analysisUAV, nullptr);
        g_context->Dispatch(1, 1, 1);

        // Unbind resources
        ID3D11UnorderedAccessView* nullUAV = nullptr;
        g_context->CSSetUnorderedAccessViews(0, 1, &nullUAV, nullptr);
        ID3D11ShaderResourceView* nullSRV = nullptr;
        g_context->CSSetShaderResources(0, 1, &nullSRV);

        // Copy to staging buffer (this frame's results go to current staging index)
        int stagingIdx = ctx->analysisStagingIndex;
        g_context->CopyResource(ctx->analysisStagingBuffer[stagingIdx], ctx->analysisBuffer);

        // Flip staging index for next time
        ctx->analysisStagingIndex = 1 - stagingIdx;
    }
}

void UpdateAnalysisDisplay(MonitorContext* ctx) {
    if (!g_analysisHwnd || !IsWindowVisible(g_analysisHwnd)) return;
    if (!ctx->analysisStagingBuffer[0] || !ctx->analysisStagingBuffer[1]) return;

    // Only read back 2 frames after dispatch to avoid GPU sync stall
    int frameInCycle = ctx->analysisFrameCounter % ANALYSIS_DISPATCH_INTERVAL;
    if (frameInCycle != ANALYSIS_READBACK_DELAY) return;

    // Read from the OTHER staging buffer (the one we copied to 2 frames ago)
    int readIdx = ctx->analysisStagingIndex;  // Current index points to buffer we'll write NEXT

    D3D11_MAPPED_SUBRESOURCE mapped;
    HRESULT hr = g_context->Map(ctx->analysisStagingBuffer[readIdx], 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) return;

    // Read uint array and convert to AnalysisResult
    uint32_t* data = (uint32_t*)mapped.pData;
    AnalysisResult result = {};

    result.peakNits = *(float*)&data[0];
    result.minNits = *(float*)&data[1];
    float sumNits = *(float*)&data[2];
    result.totalPixels = data[3];
    result.pixelsRec709 = data[4];
    result.pixelsP3Only = data[5];
    result.pixelsRec2020Only = data[6];
    result.pixelsOutOfGamut = data[7];
    result.pixelsClipBlack = data[8];
    result.pixelsClipWhite = data[9];
    result.histogram[0] = data[10];
    result.histogram[1] = data[11];
    result.histogram[2] = data[12];
    result.histogram[3] = data[13];
    result.histogram[4] = data[14];
    result.minNonZeroNits = *(float*)&data[15];

    g_context->Unmap(ctx->analysisStagingBuffer[readIdx], 0);

    // Calculate derived values
    if (result.totalPixels > 0) {
        result.avgNits = sumNits / (float)result.totalPixels;
    }

    // Update session maximums
    if (result.peakNits > ctx->sessionMaxCLL) {
        ctx->sessionMaxCLL = result.peakNits;
    }
    if (result.avgNits > ctx->sessionMaxFALL) {
        ctx->sessionMaxFALL = result.avgNits;
    }

    // Store latest result
    ctx->analysisResult = result;

    // Get tonemap settings for APL calculation and TM indicator
    float referencePeak = 1000.0f;
    bool tmEnabled = false, tmDynamic = false;
    float tmSourcePeak = 10000.0f, tmTargetPeak = 1000.0f;
    if (ctx->index < (int)g_gui.activeSettings.size()) {
        const auto& tonemap = g_gui.activeSettings[ctx->index].hdrColorCorrection.tonemap;
        tmEnabled = tonemap.enabled;
        tmDynamic = tonemap.dynamicPeak;
        tmSourcePeak = tonemap.sourcePeakNits;
        tmTargetPeak = tonemap.targetPeakNits;
        // Reference peak for APL: static mode uses source peak, dynamic uses 1000 nits
        if (!tonemap.dynamicPeak && tonemap.sourcePeakNits > 0.0f) {
            referencePeak = tonemap.sourcePeakNits;
        }
    }

    // Queue data for UI thread (offloads formatting from render thread)
    g_pendingAnalysis.result = result;
    g_pendingAnalysis.isHDR = ctx->isHDREnabled;
    g_pendingAnalysis.targetPeak = referencePeak;
    g_pendingAnalysis.sessionMaxCLL = ctx->sessionMaxCLL;
    g_pendingAnalysis.sessionMaxFALL = ctx->sessionMaxFALL;
    g_pendingAnalysis.tonemapEnabled = tmEnabled;
    g_pendingAnalysis.tonemapDynamic = tmDynamic;
    g_pendingAnalysis.tonemapSourcePeak = tmSourcePeak;
    g_pendingAnalysis.tonemapTargetPeak = tmTargetPeak;
    g_pendingAnalysis.detectedPeak = ctx->detectedPeakNits;
    g_analysisDataReady.store(true);

    // Post message to trigger UI update on window's thread
    PostMessage(g_analysisHwnd, WM_UPDATE_ANALYSIS, 0, 0);
}
