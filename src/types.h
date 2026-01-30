// DesktopLUT - types.h
// All data structures, constants, and control IDs

#pragma once

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>
#include <d3d11_4.h>
#include <dxgi1_6.h>
#include <dcomp.h>
#include <string>
#include <vector>
#include <thread>

// ============================================================================
// Control IDs
// ============================================================================

// GUI Control IDs
#define ID_MONITOR_LIST     101
#define ID_SDR_PATH         102
#define ID_SDR_BROWSE       103
#define ID_SDR_CLEAR        104
#define ID_HDR_PATH         105
#define ID_HDR_BROWSE       106
#define ID_HDR_CLEAR        107
#define ID_APPLY            108
#define ID_STOP             109
#define ID_GAMMA_CHECK      110
#define ID_STATUS           111
#define ID_TETRAHEDRAL_CHECK 112
#define ID_GAMMA_WHITELIST_BTN 113
#define ID_TRAY_ICON        1
#define WM_TRAYICON         (WM_USER + 1)
#define ID_TRAY_SHOW        2001
#define ID_TRAY_APPLY       2002
#define ID_TRAY_STOP        2003
#define ID_TRAY_EXIT        2004
#define ID_TRAY_STARTUP     2005

// Tab control and color correction control IDs
#define ID_TAB_CONTROL      200

// SDR Options (Tab 1) control IDs
#define ID_SDR_PRIMARIES_ENABLE 201
#define ID_SDR_PRIMARIES_PRESET 202
#define ID_SDR_PRIMARIES_RX     203
#define ID_SDR_PRIMARIES_RY     204
#define ID_SDR_PRIMARIES_GX     205
#define ID_SDR_PRIMARIES_GY     206
#define ID_SDR_PRIMARIES_BX     207
#define ID_SDR_PRIMARIES_BY     208
#define ID_SDR_PRIMARIES_WX     209
#define ID_SDR_PRIMARIES_WY     210
#define ID_SDR_GRAYSCALE_ENABLE 211
#define ID_SDR_GRAYSCALE_10     212
#define ID_SDR_GRAYSCALE_20     213
#define ID_SDR_GRAYSCALE_32     214
#define ID_SDR_GRAYSCALE_EDIT   215
#define ID_SDR_GRAYSCALE_RESET  216
#define ID_SDR_GRAYSCALE_24     218
#define ID_SDR_PRIMARIES_DETECT 217

// HDR Options (Tab 2) control IDs
#define ID_HDR_PRIMARIES_ENABLE 301
#define ID_HDR_PRIMARIES_PRESET 302
#define ID_HDR_PRIMARIES_RX     303
#define ID_HDR_PRIMARIES_RY     304
#define ID_HDR_PRIMARIES_GX     305
#define ID_HDR_PRIMARIES_GY     306
#define ID_HDR_PRIMARIES_BX     307
#define ID_HDR_PRIMARIES_BY     308
#define ID_HDR_PRIMARIES_WX     309
#define ID_HDR_PRIMARIES_WY     310
#define ID_HDR_GRAYSCALE_ENABLE 311
#define ID_HDR_GRAYSCALE_10     312
#define ID_HDR_GRAYSCALE_20     313
#define ID_HDR_GRAYSCALE_32     314
#define ID_HDR_GRAYSCALE_EDIT   315
#define ID_HDR_GRAYSCALE_RESET  316
#define ID_HDR_GRAYSCALE_PEAK   317
#define ID_HDR_PRIMARIES_DETECT 318

// HDR Tonemapping control IDs
#define ID_HDR_TONEMAP_ENABLE   320
#define ID_HDR_TONEMAP_CURVE    321
#define ID_HDR_TONEMAP_TARGET   323
#define ID_HDR_TONEMAP_DYNAMIC  324
#define ID_HDR_TONEMAP_SOURCE   325

// MaxTML (Display Peak Luminance) control IDs
#define ID_HDR_MAXTML_ENABLE    330
#define ID_HDR_MAXTML_COMBO     331
#define ID_HDR_MAXTML_EDIT      332
#define ID_HDR_MAXTML_APPLY     333

// Settings tab (Tab 3) control IDs
#define ID_SETTINGS_HOTKEY_GAMMA_CHECK    401
#define ID_SETTINGS_HOTKEY_HDR_CHECK      402
#define ID_SETTINGS_HOTKEY_ANALYSIS_CHECK 403
#define ID_SETTINGS_START_MINIMIZED       404
#define ID_SETTINGS_RUN_AT_STARTUP        405
#define ID_SETTINGS_CONSOLE_LOG           406
#define ID_SETTINGS_LOG_PEAK              407

// Grayscale editor control IDs
#define ID_GRAYSCALE_OK     5001
#define ID_GRAYSCALE_CANCEL 5002
#define ID_GRAYSCALE_SLIDER_BASE 5100
#define ID_GRAYSCALE_EDIT_BASE 2500

// Gamma whitelist dialog control IDs
#define ID_WHITELIST_EDIT   6001
#define ID_WHITELIST_OK     6002
#define ID_WHITELIST_CANCEL 6003

// ============================================================================
// Constants
// ============================================================================

const int OSD_TIMER_ID = 100;
const int OSD_DURATION_MS = 3000;
const int WATCHDOG_TIMEOUT_SECONDS = 5;
const int GRAYSCALE_RANGE = 25;  // +/- 25% deviation from linear
const int HOTKEY_GAMMA = 2;      // Win+Shift+G for gamma toggle
const int HOTKEY_ANALYSIS = 4;   // Win+Shift+X for analysis toggle
const int HOTKEY_HDR_TOGGLE = 5; // Win+Shift+H for HDR toggle on focused monitor

// ============================================================================
// Data Structures
// ============================================================================

// Preset display primaries (chromaticity coordinates) - for calculations
struct DisplayPrimariesData {
    float Rx, Ry, Gx, Gy, Bx, By;  // RGB chromaticity
    float Wx, Wy;                   // White point
};

// Preset display primaries with name - for GUI presets
struct DisplayPrimaries {
    float Rx, Ry, Gx, Gy, Bx, By;  // RGB chromaticity
    float Wx, Wy;                   // White point
    const wchar_t* name;
};

// Grayscale correction settings (used in MonitorContext and runtime)
struct GrayscaleData {
    bool enabled = false;
    int pointCount = 20;           // 10, 20, or 32
    float points[32] = {};         // Fixed size, values 0-1 (max 32 points)
    float peakNits = 10000.0f;     // HDR only: peak luminance for curve scaling
    bool use24Gamma = false;       // SDR only: apply 2.2->2.4 gamma transform

    void initLinear() {
        // Initialize to linear response using square root distribution (for SDR)
        // Point i corresponds to input (i/(N-1))^2, output should match input for linear
        for (int i = 0; i < pointCount && i < 32; i++) {
            float t = (float)i / (float)(pointCount - 1);
            points[i] = t * t;  // Square root distribution: output = input = t^2
        }
    }

    void initLinearPQ() {
        // Initialize to linear response for PQ space (for HDR)
        // Point i corresponds to input PQ value i/(N-1), output matches input for linear
        for (int i = 0; i < pointCount && i < 32; i++) {
            float t = (float)i / (float)(pointCount - 1);
            points[i] = t;  // Evenly spaced in PQ: output = input = t
        }
    }
};

// Analysis result structure (matches GPU buffer layout - 64 bytes aligned)
struct AnalysisResult {
    float peakNits = 0.0f;
    float minNits = 0.0f;
    float avgNits = 0.0f;
    float minNonZeroNits = 0.0f;   // Min excluding near-black (<0.1 nit)
    uint32_t totalPixels = 0;
    uint32_t pixelsRec709 = 0;
    uint32_t pixelsP3Only = 0;
    uint32_t pixelsRec2020Only = 0;
    uint32_t pixelsOutOfGamut = 0;
    uint32_t pixelsClipBlack = 0;
    uint32_t pixelsClipWhite = 0;
    uint32_t histogram[5] = {0, 0, 0, 0, 0};  // 0-203, 203-1k, 1k-2k, 2k-4k, 4k+ nits
};

// Tonemapping curve types (values match shader constants)
enum class TonemapCurve {
    BT2390 = 0,    // ITU-R BT.2390 EETF (Hermite spline)
    SoftClip = 1,  // Simple exponential rolloff
    Reinhard = 2,  // Shoulder-only Reinhard (hyperbolic)
    BT2446A = 3,   // ITU-R BT.2446 Method A (logarithmic)
    HardClip = 4,  // Hard clamp at target (for colorists)
};

// Dropdown order: BT2390, BT2446A, Reinhard, SoftClip, HardClip
inline const TonemapCurve g_tonemapDropdownOrder[] = {
    TonemapCurve::BT2390,
    TonemapCurve::BT2446A,
    TonemapCurve::Reinhard,
    TonemapCurve::SoftClip,
    TonemapCurve::HardClip,
};

inline TonemapCurve DropdownIndexToTonemapCurve(int index) {
    if (index >= 0 && index < 5) return g_tonemapDropdownOrder[index];
    return TonemapCurve::BT2390;
}

inline int TonemapCurveToDropdownIndex(TonemapCurve curve) {
    for (int i = 0; i < 5; i++) {
        if (g_tonemapDropdownOrder[i] == curve) return i;
    }
    return 0;
}

// Tonemapping settings (HDR only)
// Source peak is user-specified or dynamically detected
struct TonemapData {
    bool enabled = false;
    bool dynamicPeak = false;         // Detect source peak per-frame (GPU-based)
    TonemapCurve curve = TonemapCurve::BT2390;
    float sourcePeakNits = 10000.0f;  // Content source peak (ignored when dynamicPeak=true)
    float targetPeakNits = 1000.0f;   // Actual display capability
};

// Color correction settings (used in MonitorContext and runtime)
struct ColorCorrectionData {
    bool primariesEnabled = false;
    int primariesPreset = 0;       // Index into preset list
    DisplayPrimariesData customPrimaries = { 0.64f, 0.33f, 0.30f, 0.60f, 0.15f, 0.06f, 0.3127f, 0.329f };
    float primariesMatrix[9] = { 1,0,0, 0,1,0, 0,0,1 };  // Identity by default (includes Bradford adaptation)
    GrayscaleData grayscale;
    TonemapData tonemap;  // HDR tonemapping (only used in HDR mode)
};

// Per-monitor context (holds all state for one monitor)
struct MonitorContext {
    // Identity
    HMONITOR monitor = nullptr;
    int index = 0;  // 0, 1, 2...
    std::wstring name;

    // Window
    HWND hwnd = nullptr;
    int width = 0;
    int height = 0;
    int x = 0;
    int y = 0;  // Monitor position

    // Desktop duplication
    IDXGIOutputDuplication* duplication = nullptr;
    DXGI_FORMAT captureFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
    bool isHDREnabled = false;
    bool isHDRCapable = false;
    bool wasHDREnabled = false;  // Track previous HDR state for mode change detection
    float maxDisplayNits = 1000.0f;

    // Rendering
    IDXGISwapChain4* swapchain = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11ShaderResourceView* captureSRV = nullptr;
    DXGI_FORMAT swapchainFormat = DXGI_FORMAT_R10G10B10A2_UNORM;
    DXGI_COLOR_SPACE_TYPE colorSpace = DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;

    // DirectComposition
    IDCompositionTarget* dcompTarget = nullptr;
    IDCompositionVisual* dcompVisual = nullptr;

    // Per-monitor LUTs
    ID3D11Texture3D* lutTextureSDR = nullptr;
    ID3D11ShaderResourceView* lutSRV_SDR = nullptr;
    ID3D11Texture3D* lutTextureHDR = nullptr;
    ID3D11ShaderResourceView* lutSRV_HDR = nullptr;
    int lutSizeSDR = 0;
    int lutSizeHDR = 0;

    // Dynamic peak detection (for adaptive tonemapping)
    ID3D11Texture2D* peakTexture = nullptr;           // 1x1 R32_FLOAT for smoothed peak
    ID3D11UnorderedAccessView* peakUAV = nullptr;     // UAV for compute shader write
    ID3D11ShaderResourceView* peakSRV = nullptr;      // SRV for pixel shader read
    ID3D11Texture2D* peakStagingTexture = nullptr;    // Staging texture for CPU readback
    int lastPeakCBWidth = 0;                          // Track last written dimensions to avoid redundant CB updates
    int lastPeakCBHeight = 0;
    float detectedPeakNits = 0.0f;                    // Last detected peak (for analysis overlay)

    // Analysis resources (frame statistics overlay)
    ID3D11Buffer* analysisBuffer = nullptr;           // Structured buffer for results
    ID3D11UnorderedAccessView* analysisUAV = nullptr; // UAV for compute shader write
    ID3D11Buffer* analysisStagingBuffer[2] = {nullptr, nullptr};  // Double-buffered for async readback
    int analysisStagingIndex = 0;                     // Which staging buffer to use
    int analysisFrameCounter = 0;                     // For dispatch/readback timing
    float sessionMaxCLL = 0.0f;                       // Session peak tracking
    float sessionMaxFALL = 0.0f;                      // Session average tracking
    AnalysisResult analysisResult = {};               // Latest analysis result for display

    // LUT file paths (for reload/info)
    std::wstring sdrLutPath;
    std::wstring hdrLutPath;

    // Frame timing (calculated from refresh rate)
    UINT frameTimeMs = 16;  // Default for 60Hz, updated on init

    // Per-monitor error tracking
    bool enabled = true;           // false = skip in render loop
    int consecutiveFailures = 0;   // track failures for retry logic
    bool usePassthrough = false;   // true = no LUT applied (no applicable LUT for current mode)
    bool dcompCommitted = false;   // true after first frame rendered (prevents black flash)
    int framesAfterCommit = 0;     // frames rendered since dcompCommitted, for visibility delay

    // Manual color correction settings (separate for SDR and HDR)
    ColorCorrectionData sdrColorCorrection;
    ColorCorrectionData hdrColorCorrection;
};

// Per-monitor LUT configuration from command line
struct MonitorLUTConfig {
    int monitorIndex;
    std::wstring sdrLutPath;
    std::wstring hdrLutPath;
    ColorCorrectionData sdrColorCorrection;  // Color correction for SDR mode
    ColorCorrectionData hdrColorCorrection;  // Color correction for HDR mode
};

// Monitor enumeration callback data
struct EnumMonitorData {
    std::vector<HMONITOR> monitors;
};

// Real-time color correction updates
struct PendingColorCorrection {
    int monitorIndex;
    bool isHDR;  // true = update HDR settings, false = update SDR settings
    ColorCorrectionData data;
};

// Grayscale correction settings for GUI (uses vector)
struct GrayscaleSettings {
    bool enabled = false;
    int pointCount = 20;           // 10, 20, or 32
    std::vector<float> points;     // Size = pointCount, values 0-1
    float peakNits = 10000.0f;     // HDR only: peak luminance for curve scaling
    bool use24Gamma = false;       // SDR only: apply 2.2->2.4 gamma transform

    void initLinear() {
        // Initialize to linear response using square root distribution (for SDR)
        // Point i corresponds to input (i/(N-1))^2, output should match input for linear
        points.resize(pointCount);
        for (int i = 0; i < pointCount; i++) {
            float t = (float)i / (float)(pointCount - 1);
            points[i] = t * t;  // Square root distribution: output = input = t^2
        }
    }

    void initLinearPQ() {
        // Initialize to linear response for PQ space (for HDR)
        // Point i corresponds to input PQ value i/(N-1), output matches input for linear
        points.resize(pointCount);
        for (int i = 0; i < pointCount; i++) {
            float t = (float)i / (float)(pointCount - 1);
            points[i] = t;  // Evenly spaced in PQ: output = input = t
        }
    }
};

// Tonemapping settings for GUI
struct TonemapSettings {
    bool enabled = false;
    bool dynamicPeak = false;
    TonemapCurve curve = TonemapCurve::BT2390;
    float sourcePeakNits = 10000.0f;  // Content source peak (ignored when dynamicPeak=true)
    float targetPeakNits = 1000.0f;
};

// MaxTML (Display Peak Override) settings for GUI
struct MaxTmlSettings {
    bool enabled = false;
    float peakNits = 1000.0f;
};

// Color correction settings for GUI
struct ColorCorrectionSettings {
    bool primariesEnabled = false;
    int primariesPreset = 0;       // Index into g_presetPrimaries
    DisplayPrimaries customPrimaries = { 0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f, L"Custom" };
    float primariesMatrix[9] = { 1,0,0, 0,1,0, 0,0,1 };  // Identity
    GrayscaleSettings grayscale;
    TonemapSettings tonemap;  // HDR tonemapping (only used in HDR mode)
};

// Per-monitor settings for persistence
struct MonitorSettings {
    std::wstring sdrPath;
    std::wstring hdrPath;
    ColorCorrectionSettings sdrColorCorrection;  // Color correction for SDR mode
    ColorCorrectionSettings hdrColorCorrection;  // Color correction for HDR mode
    MaxTmlSettings maxTml;                       // Display Peak Override settings
};

// GUI state
struct GUIState {
    HWND hwndMain = nullptr;
    HWND hwndMonitorList = nullptr;
    HWND hwndSdrPath = nullptr;
    HWND hwndHdrPath = nullptr;
    HWND hwndStatus = nullptr;
    HWND hwndGammaCheck = nullptr;
    HWND hwndGammaWhitelistBtn = nullptr;
    HWND hwndTetrahedralCheck = nullptr;
    HWND hwndApply = nullptr;
    HWND hwndStop = nullptr;
    NOTIFYICONDATA nid = {};
    bool isRunning = false;
    std::thread processingThread;
    std::vector<HMONITOR> monitors;
    std::vector<std::wstring> monitorNames;
    std::vector<MonitorSettings> monitorSettings;  // Per-monitor LUT paths (editable)
    std::vector<MonitorSettings> activeSettings;   // Settings currently running (for comparison)
    int currentMonitor = 0;  // Currently selected monitor in list

    // Tab control
    HWND hwndTab = nullptr;
    int currentTab = 0;

    // SDR color correction controls (Tab 1)
    HWND hwndSdrPrimariesEnable = nullptr;
    HWND hwndSdrPrimariesPreset = nullptr;
    HWND hwndSdrPrimariesRx = nullptr;
    HWND hwndSdrPrimariesRy = nullptr;
    HWND hwndSdrPrimariesGx = nullptr;
    HWND hwndSdrPrimariesGy = nullptr;
    HWND hwndSdrPrimariesBx = nullptr;
    HWND hwndSdrPrimariesBy = nullptr;
    HWND hwndSdrPrimariesWx = nullptr;
    HWND hwndSdrPrimariesWy = nullptr;
    HWND hwndSdrGrayscaleEnable = nullptr;
    HWND hwndSdrGrayscale10 = nullptr;
    HWND hwndSdrGrayscale20 = nullptr;
    HWND hwndSdrGrayscale32 = nullptr;
    HWND hwndSdrGrayscale24 = nullptr;
    HWND hwndSdrGrayscaleEdit = nullptr;
    HWND hwndSdrGrayscaleReset = nullptr;

    // HDR color correction controls (Tab 2)
    HWND hwndHdrPrimariesEnable = nullptr;
    HWND hwndHdrPrimariesPreset = nullptr;
    HWND hwndHdrPrimariesRx = nullptr;
    HWND hwndHdrPrimariesRy = nullptr;
    HWND hwndHdrPrimariesGx = nullptr;
    HWND hwndHdrPrimariesGy = nullptr;
    HWND hwndHdrPrimariesBx = nullptr;
    HWND hwndHdrPrimariesBy = nullptr;
    HWND hwndHdrPrimariesWx = nullptr;
    HWND hwndHdrPrimariesWy = nullptr;
    HWND hwndHdrGrayscaleEnable = nullptr;
    HWND hwndHdrGrayscale10 = nullptr;
    HWND hwndHdrGrayscale20 = nullptr;
    HWND hwndHdrGrayscale32 = nullptr;
    HWND hwndHdrGrayscaleEdit = nullptr;
    HWND hwndHdrGrayscaleReset = nullptr;
    HWND hwndHdrGrayscalePeak = nullptr;

    // HDR Tonemapping controls (Tab 2)
    HWND hwndHdrTonemapEnable = nullptr;
    HWND hwndHdrTonemapCurve = nullptr;
    HWND hwndHdrTonemapTarget = nullptr;
    HWND hwndHdrTonemapSource = nullptr;
    HWND hwndHdrTonemapDynamic = nullptr;

    // MaxTML controls (Tab 2)
    HWND hwndHdrMaxTmlEnable = nullptr;
    HWND hwndHdrMaxTmlCombo = nullptr;
    HWND hwndHdrMaxTmlEdit = nullptr;
    HWND hwndHdrMaxTmlApply = nullptr;

    // Scrollable panels for each tab
    HWND hwndScrollPanel[4] = { nullptr, nullptr, nullptr, nullptr };
    int scrollPos[4] = { 0, 0, 0, 0 };           // Current scroll position per tab
    int contentHeight[4] = { 0, 0, 0, 0 };       // Total content height per tab

    // Tab 0 controls (LUT Settings - to show/hide)
    std::vector<HWND> tab0Controls;
    std::vector<int> tab0OriginalY;   // Original Y positions for scroll
    // Tab 1 controls (SDR Options)
    std::vector<HWND> tab1Controls;
    std::vector<int> tab1OriginalY;
    // Tab 2 controls (HDR Options)
    std::vector<HWND> tab2Controls;
    std::vector<int> tab2OriginalY;
    // Tab 3 controls (Settings)
    std::vector<HWND> tab3Controls;
    std::vector<int> tab3OriginalY;

    // Settings tab controls
    HWND hwndSettingsHotkeyGamma = nullptr;
    HWND hwndSettingsHotkeyHdr = nullptr;
    HWND hwndSettingsHotkeyAnalysis = nullptr;
    HWND hwndSettingsStartMinimized = nullptr;
    HWND hwndSettingsRunAtStartup = nullptr;
    HWND hwndSettingsConsoleLog = nullptr;

    int panelHeight = 0;  // Visible height of scroll panels
};

// Grayscale editor dialog data
struct GrayscaleEditorData {
    int pointCount;
    float* points;           // Pointer to the grayscale points array
    std::vector<float> originalPoints;  // Copy for Cancel restore
    std::vector<HWND> sliders;
    std::vector<HWND> editBoxes;
    HWND hwndDialog;
    bool updatingFromSlider = false;  // Prevent feedback loops
    bool updatingFromEdit = false;
    bool isHDR = false;      // true if editing HDR grayscale, false for SDR
    float peakNits = 10000.0f;  // HDR peak for label calculation (must match ColourSpace target)
};

// ============================================================================
// Static Data
// ============================================================================

// Preset display primaries
inline const DisplayPrimaries g_presetPrimaries[] = {
    { 0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f, L"sRGB/Rec.709" },
    { 0.6800f, 0.3200f, 0.2650f, 0.6900f, 0.1500f, 0.0600f, 0.3127f, 0.3290f, L"P3-D65" },
    { 0.6400f, 0.3300f, 0.2100f, 0.7100f, 0.1500f, 0.0600f, 0.3127f, 0.3290f, L"Adobe RGB" },
    { 0.7080f, 0.2920f, 0.1700f, 0.7970f, 0.1310f, 0.0460f, 0.3127f, 0.3290f, L"Rec.2020" },
    { 0.6400f, 0.3300f, 0.3000f, 0.6000f, 0.1500f, 0.0600f, 0.3127f, 0.3290f, L"Custom" },
};
inline const int g_numPresetPrimaries = sizeof(g_presetPrimaries) / sizeof(g_presetPrimaries[0]);

// 64x64 blue noise texture data (single channel, 8-bit)
// Source: momentsingraphics.de (Christoph Peters) - CC0 Public Domain
inline const unsigned char g_blueNoiseData[64 * 64] = {
     65,247,203,177, 54,149, 96,135,122, 62,109,206, 27,217,152,103,
    250, 78,122,228,  3, 83,233,160, 45,242,108, 40,125, 93,201, 35,
    231,187,254,207,147, 13, 87,134,246,197,177,224, 59, 92,132,169,
     49,183,140,  3, 58,165, 27,204, 12, 83,196,  4,159,183, 92,197,
    170,140, 24,127,109,255, 35,210, 79,193,178,141,168, 11, 69,130,
    182, 27,147, 47,191,170, 66, 13,187, 76,  0,197,161, 66,146,172,
    104,134, 58, 97,182,232,162,115, 34, 73,  2,238,162,188,  6,243,
    218, 31, 69,193,244, 87,146,130,248,172,225,104,235, 21,218,117,
    236, 49, 87,155,228, 69, 15,166,235, 24, 48, 86,119,238,195, 90,
      6,221,165,105, 20,255,120,146,211,129, 88,236, 21, 52,245, 17,
     73,158, 24,  7,126, 43, 64,190,218, 95,128, 23,207, 46,113,145,
     85,102,229,119, 40,106,222, 66, 49,152, 31,126, 46,145, 57, 10,
    191,104,213,  3, 42,197,182,104,147,  1,223,252, 60, 34,161, 45,
    244, 61,208,133, 89,199, 37, 56,245, 29,174,152,114,190,212,127,
    179,238,216,195,246,109, 26,240,170, 51,155,108, 81,249, 28,195,
     60,175,153, 19,208,177, 15,187,114,211, 93, 72,178,203, 82,162,
     28, 72,179,242,160, 83,120, 55,214,128,156,100,180,136,214,106,
    144,117, 30,231, 71,155,177,106, 94,224, 47, 69,229, 99, 83,  4,
     45,114, 87,141, 72,156,203, 79,139, 13,232,181,137, 67,159,212,
     10,130,254, 77, 52,160,235, 80,  5,241,192, 18,254,111,227,131,
    248,147,115, 59,133,207, 26,248, 91, 67, 31,202, 13, 78,229, 16,
    201, 82,183, 52,240, 18,221,  7,139,163,202, 12,134, 32,164,224,
    198, 63, 33,170, 51,224,101, 19,116,211, 61,198, 36,226,121, 93,
    236, 38,200, 97,141,123, 33,102,139,165, 58,133,157,  4, 96, 41,
    199, 13,219, 98, 16,227,144, 39,189,172,237,113, 53,189,126, 67,
    173,156,  4,101,141,114,205, 63,191, 79,118,241,185, 57,143,248,
    102,154,229,121,  0,178, 38,150,186,254, 89,  4,101,173, 17,186,
     54,112,167,  0,215,247, 63,203,227, 42, 85,220, 34,207, 64,173,
     80, 51,186, 37,171, 73,110,161,  9,220, 81,140,164,241, 25, 95,
    253, 38,215,194,170, 43, 85,125,250, 21, 40,149,108,208, 76, 21,
    130, 12,183,252, 94,210,241,129, 68, 44,165,127,242, 47,152, 82,
    142,223, 71, 28,179, 86,189,150, 24,176,122,104,184,141,239,120,
    225,135, 89,253,125,193,243, 60, 97,123, 44,  5,212,104,148, 50,
    223,135,120, 74,248, 29,229,158, 53,177,214, 88,  5,168, 42,192,
     90,213, 74, 28,135, 59, 83,  9,225,110, 27,145,215, 70,205,251,
     22,194,155,243,133, 46,  8,115, 73,249, 14,233, 77, 47, 23,154,
    106,165,205,  2,152, 49, 24,206,232,150,183,251, 66, 34,203,185,
     20, 86, 57, 10,150, 96,186,  3,137,234,102, 63,254,221,119,232,
    174, 53,148,202,162,115,195,173,154,203,235, 79,189,114,  6,131,
    105, 43, 91,118, 59,226,162, 95,213,136, 55,194,163, 94,212, 10,
    240, 28, 64,232,104, 84,178,137, 74, 17,198, 89,131,171, 77,113,
    160,236,199,225,128, 65,209,108, 75, 34,155,196,132, 29,157, 67,
    112, 36,240,105, 19, 46,220, 32, 93, 53, 10,178, 58, 33,226,169,
     63,182,235,206, 18,107,198,236, 39,157,205,  1,127,252,177, 72,
    191,122, 42,143,199, 12,218,119, 35,108, 54,159, 23,240,219,  0,
    143,100,177, 36,166,243, 15,173,224,123, 19,183, 49, 82, 15,244,
    141,  3,217, 81,185,250,142, 73,106,246,124,137, 99,156,240, 86,
    202, 10,135, 35,170,143, 69,182, 27, 87,109, 66,147, 30,115, 53,
    150,174,221, 76,164,241, 57,156,248,173,226,214, 99,120, 60, 43,
    247, 67, 16,110, 51,144, 89,200, 56,246, 94,208,146,105,179,205,
     94,190,128, 65,170,  7,122,229, 22,193,162,218, 15,196, 46,123,
     29,159, 73,217, 84,255, 13, 53,130,244,174,230, 43,220,202, 86,
    245, 22, 93,131, 33,114, 95,190,  2, 84,140, 40,  9,146,179,194,
    130,210, 80,191,219, 25,119, 41,139,163, 11, 70,239,219,124, 57,
    162, 44,228,152, 97, 56,209,156,180, 66, 42, 85,253, 71,143,103,
    224,248,113,178, 44,124,224,104,150,216, 23, 81,185,101,137,  6,
    111,210, 52,229,180, 17,213, 45,131,236, 63,188,206, 81,232, 93,
     28,163,121,253,152, 70,237,187, 80,215,116, 43,169,  6, 34, 77,
    253, 12,113, 29,239,199, 38, 83,131,  3,232,112, 27,167,214,  1,
    188, 53, 96,148,  4,196,161, 75,189,  7,119, 58,159, 17,234, 67,
    185,158, 11,193, 65,247,144, 72,204, 26,122,105,255,158, 52, 12,
    223,141, 40,  6, 92,207,172,  1, 31,102,252,142,191, 91,234,197,
    135,182, 87,211,138, 16,116,248, 98,212,146,201,182, 59,131, 82,
    153, 17,207,241, 63, 91,238, 32,205, 96,144,250,198,126,169, 39,
    254, 98,139,121, 86,170,107,160, 92,183,168, 15, 70, 33,117,174,
    106, 62,233,183, 55,132,107,158,230,198, 59, 23,128, 64,111,151,
     21,222, 53,166, 74,177,191, 62, 30,172, 52,121, 92,238, 38,246,
    171, 69, 34,128,184, 23,117, 49,168, 67,222, 34, 88, 51,214, 78,
     29, 57,206,233, 40, 21,221,  7,250, 54,228,152,198,133,215,245,
     75,204,169,101, 22,244, 44, 66,124, 88,180,226,157,212,175, 47,
    102, 68,127,246,106, 46,226,158,136,242, 77,  9,155, 19,105,198,
    118,225,142,105,229,153,215,138,246, 17,130,176,229,  3,116,148,
    129,173,  6, 75,153,199, 59,117, 34,138, 80, 43,242, 87, 20,186,
    149,  9,128, 81,220,194,140,213, 16,148, 49,  8, 81, 32,249,  0,
    231,205,155, 27,  4,147, 88, 12,109,219, 41,186,228,208,138, 49,
     21,190, 85,167, 14, 56, 80,101,187, 42,111, 74,156,103,240,195,
     90,225,111,244,178,127,238,190,210,100,218,  2,112,165, 56, 98,
     35, 48,249,156,114, 34,177, 78,250,166,110,241, 99,199,123,143,
     88,172, 39,194,217,125,255,204, 24,194, 96,128, 61,164, 77,234,
     97, 59,212, 42,243,201,175,  0,230,150,210,192, 25, 61,180, 16,
     69, 35,144, 50, 99, 28, 88, 70,151,173,125, 65,181,140,200,232,
    119,214,191, 18, 68,236,  7, 97,200, 39,219,184,138, 55, 72,187,
     16,115, 79,236, 99, 66,181, 79, 57,166,148,251, 31,114,  7,176,
    149,253,  9,133, 71,114, 31,126, 64, 88, 12,247,142,220, 45,208,
    249,158,189,216, 15,137,164, 47, 10, 22,193,235, 31,222, 14, 70,
    163,142, 60, 91,168,146,121, 57,133, 19, 65,119, 13,230,161,213,
    241, 58,137,177, 51,160, 33,134,240,118,  1, 71,216, 90,189,221,
     32,121,162,184, 94,226,157,252,204,166, 53, 98,122, 82,164,134,
    118,  0, 82, 63,202,253,185,228,109,246, 91,146, 48,103,128, 84,
    242,  1,108,227,201, 45,216,187,238,154, 90,207,174, 44, 25, 95,
     36,153,223,  6,119,210, 16,222, 92,175, 50,197,139,243, 45,131,
     70,106,204, 22,144,195, 47,106, 22,137,217, 35,173,231, 28, 95,
    236,175,224,125,103, 39, 76,215,132, 57,201, 77,159,253,209, 28,
    175,188, 37,132,254, 26, 85,107,164, 30,247, 76,147,107,252,132,
    202,109, 22,249, 88,193,149,107, 40,231,211, 23,105,154, 18,166,
    237, 51, 82,246, 61,  6, 86,179, 77,240,112,185, 68, 10,193, 55,
    108, 43, 23,167,148,  8,118,154, 27,168, 38,121,178,  6, 62,154,
     95,223, 54, 76,153,176,  4, 70,222, 51,128,190,  2,218, 81,169,
     65,184, 75,166, 45,233, 71,186, 11,158, 76,124,181, 62, 84,193,
      2,215,175, 36,219,130,237,149, 40,192,  3,131,249,153,205,143,
    213, 73,198,243, 85,230,179, 65, 96,209,240, 19,219,111,195, 43,
    135,117,208, 12,101,124,233,141,201, 15,101,231, 60,117,195, 48,
     12,125,206,101,139, 25,127,245, 54,141, 98,247, 35,227,208, 96,
    118,137,153,100,114,165,207, 15,223, 60,161, 90, 47,104, 79, 18,
    255,156,135, 57, 31,204, 48,248,  1,187,136, 69, 90,143,233, 79,
    248, 21,164,243,184, 59,194, 34,114,172,151, 40,180, 23,156,235,
    224,146,244, 35,220, 62,174, 86,115,206,191,  5,169, 52,142,251,
     29, 64,233,  9,190, 49, 69,122,102,142,200,235, 29,225,126,180,
     93,  4,120, 97,187,111,137,162, 78,104,226, 46,165, 30,183, 10,
    203, 66,145, 83,217, 44,159, 93,252, 65,208, 85,243,140, 99, 30,
     89, 57,  1,160,113,199,  7,214,163, 20, 66,221,129,112, 11,160,
    180, 44,198, 76,255, 92, 27,175,245, 83, 19,116,168,188, 61, 36,
    165, 50,223,173, 12,218, 21,233,125,151, 14,199,251, 57,102,125,
    171, 48,110, 31,134, 16,238, 78,  8,132,226, 19,124, 72,205,171,
    115,191, 80,180, 93,251,151, 37,236, 46, 94,148, 79,237,201, 71,
     90,221,126, 18,157,136,231,187, 37,210, 54, 71,216,  9,147,231,
     69,193,240, 76,147, 60, 90,193, 37, 55,177,114,131,214,157,224,
     92,239,196,229, 98,206,119,177,215, 49, 96,166,197,  5,255, 44,
    135,239,216, 23,131, 50,103, 77,123,178,254, 26,187, 39,103, 20,
    242,147,105,171,209, 58,111, 11,162,124,150,252,134, 99,206,112,
    141, 17, 33,207,127,252,169, 72,212,245, 92, 26, 82,  3, 71, 39,
     18,150,181,  1,167, 69,143, 24,155,188, 35,146, 55,109,220, 65,
     19,152, 39, 70,233,189, 15,227,136,196,109,161,215, 59,170,132,
    189, 54, 33, 82,227, 40,145, 74,195, 93,  0,178, 42, 83, 25,246,
    125, 89,107,157, 45,100, 29,121,  5,158,203,235,145,188,244,208,
    116, 78, 61,129, 46,249,192, 57,105,246,116,235,178, 82,158,185,
     98,202,122,173,145, 61,166,205, 28, 55,  0, 85,121,140,231,210,
      3,118,249,200,  8, 98,246,215, 50,234,220,107,199,158, 56,175,
     41,212,235,177,  7,200,227,185,106,134, 65, 44,167,108, 54,175,
    140,255, 23,220, 90,113, 36,231, 83,  2, 70,200, 15, 31,126,230,
      9, 84,250,107, 10,213,115, 90,156, 72,223,242, 14, 32, 75, 45,
     87,158,176, 68,133,190,168,116, 20,132, 33, 64,241, 13,226,188,
      2,148, 61, 82,136,239, 53,149, 82,220, 17, 99,226, 31,126,  8,
    193,100,160,204,185, 16,148,209,127,172,219,136, 93,242,143, 52,
    214,164, 47,197, 32, 78,247, 41,235,144,102,173,205,183,152, 99,
    238,216, 17,108,151, 28, 86, 61,181,154, 78,171,143,116, 95, 75,
    167,221,192, 20,115, 68,165, 13, 40,253,176,196, 76,154,237, 87,
     44,230, 30,136, 76,239,164, 95, 47, 22,157, 61, 43,206,181, 71,
    118, 25,140, 94,224,179,132,  7,184,200,126, 48, 64,249,112,195,
     26,127, 56,234, 42,219,239,  5,207,250, 91,189, 27,211,134,253,
    103,121, 48,248,182, 95,217,129,192, 58,117,138, 22,202, 60,169,
    216, 68,109, 52,122,  5, 64,195,227,183,102,250,113,167,  6,103,
    244,192, 64,237,150, 54,162, 97, 67, 20, 34,163, 90,134,  6,168,
     69,142,182,201, 78,124,162,142,104, 39,123, 12,233, 49, 66, 32,
    144, 14, 90,160, 36,205, 24,109,232, 92,159,  0,245,111, 94,130,
     16,183,153,247,176,222,141, 31, 74,133, 10,212, 28,147, 81,222,
     38,174,124,  2,111, 21,209,121,221,253,110,214, 17,229, 53,220,
     37,254, 93,  1,171,100, 50, 71,192,223, 56,202,109,162,182,198,
    238,209,227, 64,140,243,153, 74,172, 33,212, 49,179,219, 38,250,
    144,205,  9, 84,211, 44,106,254,118,233, 86,191, 67,236,197,133,
     15,156, 86,217, 74,245,194, 44, 84,171,143,188, 77,150,117,203,
     81,159,120, 30,212,247, 14,232, 26,134,168,148, 74,245,  8, 83,
     53, 26,170,126,  3, 84, 50,201,  9,241,132, 85, 69,149, 25,191,
     77,117, 35,163, 96, 20,150,181, 56,167, 40,155,122, 48, 95, 58,
    254,203, 46,185,167, 35,137,154, 10, 56,235, 99, 40,246,178,102,
     13,189,228, 60,135,186,154,113,177, 83,  6,228, 41, 93,154,130,
    180, 73, 98,196,111,230,184,123, 62,146,106,188,231,123,165, 54,
    103,223, 63,242,129,198, 79,  8,203, 25,138,221,  0,181,164, 24,
    114,100,144,234, 61,106, 91,226,181, 72,131,  1,210, 60, 22,138,
    240, 47,107,148, 75, 38, 89,209, 62,255, 99,120,214, 20,206,114,
     37,223,147,254, 43, 19,166, 97,222, 20,207, 41, 10, 97,211, 14,
    236,172,140,189, 52,230,160,217, 92,110,239, 73,103,247,214,141,
    229, 72,  8, 28,130,207, 13,250,112,198, 30,162,121,194, 91,156,
     68,215,  9,200,237, 19,225,127, 46,198, 32,186,136,173, 63,248,
    160,190, 11, 58,135,217,151, 33,248, 79,174,157,254, 65,196,130,
     42, 87, 26,  1,109, 69, 30,123,246, 60,172,197,131, 20, 64, 35,
    188,170,216,155,241, 79,172, 26, 50,150,222,242, 80,171,226, 34,
    129,164, 88,175,123,101,168,  2,142,161,239, 70, 50,234,104,  0,
     92,120, 79,174,199, 89, 70,118,191,136, 55,115, 30,141, 81,245,
    155,184,120,210,252,177,143, 46,185,149, 16, 38, 89,159,205, 82,
    125, 44, 89,117, 54,193,146,124,212, 87,103, 44,141, 17, 51,252,
    112,187, 27,248, 42, 67,195,243, 80,108, 14,151, 87, 29,196,140,
     46,211,232, 25,106,245, 13, 49,232,  5, 94,201,222,181,  4,107,
    217, 72,233,149, 39, 85,101,227,  4, 81,211,229, 53,179,112,237,
     18,197,250,179,  3, 98, 39,237, 63,168,  6,185, 68,209,100,200,
      2, 78,222, 55,139,155,217, 31, 58,176,205,218,126,165,225, 68,
     18,243,155,128, 37,209,183,154,212,169, 73,238, 45,122,163, 55,
     34, 17, 97, 59,165,192, 22,204,134,164,106,121,143,244,  4,153,
     98,139, 66, 32,224,204, 73,187,138, 23,249,110,228,130,152,174,
     63,144,119,204,  8,111,183, 92,121,230, 23, 98,  6,252,111,182,
    145, 99, 52, 72,168,139, 60,101,128, 28,110,149, 20, 89,230,193,
    145,175,203,129, 11,218,116,237, 54, 68,255, 28,190, 74, 40,217,
     56,228,163,113,133,159,107, 11,219,120,201,156, 36, 12, 87,231,
     43,243,160, 89,234, 74, 24,251,149, 41,136,192, 78, 56, 38,204,
    171,  6,194,223,113,  2,227, 80, 41,251,186, 59,172,210, 68,132,
    247, 80,110,239, 47, 75,157, 91, 36,199,176, 11,222, 94,169,129,
    184, 11, 78,240, 19, 55,254,174, 91, 50, 76,178, 58,244,122, 24,
    192,102, 35, 18,189,171,132, 51,201,167, 64,243,180,157,130, 82,
     28,117,252, 88,178, 23,242,161,200, 11,220,135,242,  7,103, 42,
     26,220,  3,183,138,249,174, 14,127,149,100, 47,137, 62,208, 24,
    105,201, 45,176,212, 85, 36,148,230, 26,132, 96,217,187, 71,211,
    135,168,218,125, 64,210, 98, 14,225, 84,105, 33,119, 11,239,216,
    232,134, 59, 34,144,206, 95,120, 67,145, 83, 99, 34,156,202,117,
    167, 62,155, 93, 30, 64,107,213,186,241, 81,231,160,119,251, 85,
    233,145,124, 96,152,195,116, 66,207,161,238,  2,142,164, 46,110,
      8, 78, 52,250,146, 39,240,160,116,  4,210,229,140,196, 94, 66,
     43,184,162,215, 77, 50,190, 32,234,180, 48,125,189, 77,255,141,
     88,236,210,122,199,150,225, 24, 56,  1,206,111, 26,196,  5,152,
     37, 68,220,  0, 29,243,138,  8,186, 41,107,197, 30, 88,255,151,
    180,230, 94,197,  5,108, 76,185, 58,145,172, 72, 22, 51,165,107,
    147,200,  8,100,245,127,154,  9,109,166, 22,217,232, 14, 55,181,
     19,190, 51, 13,231, 42, 83,120,142, 70,169, 38,182, 77, 54,177,
    114,192,249, 59,183, 75,225,100,126, 83,248, 55,118,225, 18, 62,
    202, 31,118,157,176,221,139, 29,195,253, 43, 91,184,247,207, 14,
    125, 71,237,115, 21,174, 60,221,253, 88,199, 66,112,173,128,224,
     36,110, 78,134,176,102,190,163,252, 95,219,127,245,139,215,237,
     91, 16,166,132,108,157, 50, 16,216,169,151, 73,176,207,129,101,
    240,137, 68, 21, 84, 48,236, 94,123, 17,216,132,153,114, 81,224,
     27, 92,151, 45,225,194,138, 75, 39,129,157,  4,144, 43, 95, 72,
    239,146,165,251, 62,  5,237, 32,202, 48,153,  9, 62, 99, 13,161,
    128, 48, 80,213, 36,232,176,200, 63, 25,228,  7,137, 36, 81,169,
      0, 45,185,247,211,129, 10,203, 68,166,104,231,  1, 62, 37,179,
    255, 56,209,168, 86,  3,101,211,182, 54,235,102,245,214,195,158,
      0,202, 96, 27,213,154, 74,133, 16,108,188, 87,227,198,110, 32,
    203,228,148,190,  7, 92,115,253,145, 97,193,109,184,246, 51,218,
    192,148,228,104,164, 61,151,175,245, 50, 79, 31,189,239,139,161,
      7,192,130, 33, 67,249,122, 15,148, 25,206, 79, 32,169, 20,120,
     63,219, 45,126,194,113, 90,222,178, 67,234, 27,173,149, 51,254,
     71, 20,102,244, 65,139, 26, 80, 37,131, 47,238, 14, 93,159,112,
     74,123, 89, 33, 15,115,227, 37,110,143,208,158,121, 87,214,104,
    228,112,176,234,142,162,198,230, 91,172,117,188,133, 50, 87,249,
    139,174, 84,241, 11, 52,170, 38,246,143,117,206, 41,123, 84,167,
    138,182,120, 42,170,197,156,236,209,163, 70,213,146, 60,230, 25,
     11,210, 56,179,206, 75,191, 88,  5,182,224, 57, 11,199, 45, 73,
    149, 82, 18, 97, 49, 27,111, 58, 41,247, 69, 10,220,151,107,200,
     12, 35,106,181,226,147,207,124,  2, 54,161, 76, 13,237,189,222,
      2, 58,208, 86,224,126, 52,  1,181,117, 21, 85,172,124,201,136,
    253,167,234,131,147,250, 47,136,236, 20,128, 94,251,171,133, 25,
    203, 55,218,188,241, 75,180,221,158,140, 97,239, 61,180,228, 75,
    234,155, 59,134, 72, 19,101,186, 85,213, 97,250,136, 62, 25, 96,
    113,241,152, 30, 14,249, 94, 67,104,226,195,250, 32,103, 43,187,
     65,100, 42,  3, 85, 23,160,100,197, 73,168, 39,113, 66,184,235,
    164,116,  0,126,151,209, 18, 84,123,  3,194,163, 18,125, 41, 24,
    186,118,208,253, 31,163,238, 65,230, 22,194,179,108,215,159,202,
     37,175,133, 73,185,111,217,167, 18,138, 56,153,  4,223, 78,161,
     29,150,199,119,184,222, 62,212,119, 29,244,145,218, 17,100, 35,
    138,252, 69,171, 40,103,135,252, 33,214, 48,112,204, 93,145,167,
     97, 48,  5,193, 92,116, 46,140,155, 37,129, 49,  5,146, 74,127,
     52, 90,231,211,161, 44,147,201,232, 41, 91,129,179,205,244,116,
     89,219,242, 71,105,238, 36,176,153, 53,204, 77,191,157,242, 86,
    215, 23,192, 90,228, 12,202,170, 70,182,235, 80, 30,254,213, 65,
    243,221,141, 77,171,218,197,  7,105,171,226, 89,241, 33,229,180,
    248, 21,105,  4, 60, 84,130, 32, 76,186,238,110, 68, 50,139, 24,
    174,  8,135, 53,169, 13,134, 88,  1,229,105,131,  8, 51,124, 61,
    179,108, 48,144,244, 64,115, 51,145,101,129,154, 57,175,  2, 84,
    128,159,109, 16, 57,244,127, 80,251,203, 70,118,165,191,102, 15,
     67,166,196,142,242,190, 10,251,120,158,  9,216,166, 14, 98,234,
     61,188, 38,209,151,196,113,255, 67,186, 24, 91,173,227,200, 12,
    234,159,211,122, 30,163, 86,196,219, 22,  9,225,188,136,115,196,
     19, 36,237,184,152, 40, 27,181, 59, 15,150, 25, 55, 80,137,209,
    153,221,124, 38,113,225,100,175, 63,208, 83, 29,255,194,152,204,
    123, 81,251, 95, 21, 76, 47,218,144,163,240,211, 41,110,151, 79,
     39, 98,  8, 75,223,187,  5,239, 42,161,247, 74, 95, 41,233, 52,
    170,204, 63, 96,213,135,112,208, 96,138,223,178,216,251,  7,116,
     49, 86, 26, 75,170, 53,213, 21,149, 46,103,142,119, 37, 73,227,
     17,108,159,216,125,233,181, 99, 38,118, 58,137, 71,251, 29,133
};

// Window class names
inline const wchar_t* g_windowClassName = L"DesktopLUT";
inline const wchar_t* g_osdClassName = L"DesktopLUT_OSD";

// Startup registry
inline const wchar_t* g_startupRegKey = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
inline const wchar_t* g_startupValueName = L"DesktopLUT";
