// DesktopLUT - globals.cpp
// Global variable definitions

#include "globals.h"

// ============================================================================
// D3D Device and Resources (shared across all monitors)
// ============================================================================

ID3D11Device* g_device = nullptr;
ID3D11DeviceContext* g_context = nullptr;
ID3D11VertexShader* g_vs = nullptr;
ID3D11PixelShader* g_ps = nullptr;
ID3D11ComputeShader* g_peakDetectCS = nullptr;
ID3D11Buffer* g_peakCB = nullptr;
ID3D11ComputeShader* g_analysisCS = nullptr;
ID3D11Buffer* g_analysisCB = nullptr;
ID3D11SamplerState* g_samplerPoint = nullptr;
ID3D11SamplerState* g_samplerLinear = nullptr;
ID3D11SamplerState* g_samplerWrap = nullptr;
ID3D11Buffer* g_constantBuffer = nullptr;

// DirectComposition device (shared)
IDCompositionDevice* g_dcompDevice = nullptr;

// Blue noise texture (shared)
ID3D11Texture2D* g_blueNoiseTexture = nullptr;
ID3D11ShaderResourceView* g_blueNoiseSRV = nullptr;

// ============================================================================
// Monitor State
// ============================================================================

std::vector<MonitorContext> g_monitors;

// ============================================================================
// Atomic Control Flags
// ============================================================================

std::atomic<bool> g_desktopGammaMode{ true };   // Effective gamma state (may be overridden by whitelist)
std::atomic<bool> g_tetrahedralInterp{ false };  // Default: trilinear (tetrahedral opt-in for quality)
std::atomic<bool> g_running{ true };            // Main loop control
std::atomic<bool> g_forceReinit{ false };       // Force reinit on next frame
std::atomic<bool> g_logPeakDetection{ false };  // Debug: log detected peak nits to console
std::atomic<bool> g_consoleEnabled{ false };   // Show console window (GUI mode only, default off)

// ============================================================================
// Hotkey Settings
// ============================================================================

std::atomic<bool> g_hotkeyGammaEnabled{ true };    // Enable Win+Shift+G hotkey
std::atomic<bool> g_hotkeyHdrEnabled{ true };      // Enable Win+Shift+H hotkey
std::atomic<bool> g_hotkeyAnalysisEnabled{ true }; // Enable Win+Shift+X hotkey
char g_hotkeyGammaKey = 'G';                       // Key for gamma toggle
char g_hotkeyHdrKey = 'Z';                         // Key for HDR toggle
char g_hotkeyAnalysisKey = 'X';                    // Key for analysis toggle
std::atomic<bool> g_startMinimized{ false };       // Start minimized to tray

// ============================================================================
// Gamma Whitelist
// ============================================================================

std::atomic<bool> g_userDesktopGammaMode{ true };      // User's preference (checkbox state)
std::vector<std::wstring> g_gammaWhitelist;            // Parsed exe names (lowercase)
std::wstring g_gammaWhitelistRaw;                      // Raw comma-separated string
std::atomic<bool> g_gammaWhitelistActive{ false };     // A whitelisted process is running
std::wstring g_gammaWhitelistMatch;                    // Name of matched process (for OSD)
std::atomic<bool> g_gammaWhitelistThreadRunning{ false }; // Control flag for whitelist polling thread
std::atomic<bool> g_gammaWhitelistUserOverride{ false };  // User manually toggled while whitelist was active
std::wstring g_gammaWhitelistOverrideProcess;             // Process name when user overrode (lowercase)

// ============================================================================
// Thread Synchronization
// ============================================================================

std::mutex g_gammaWhitelistMutex;  // Protects g_gammaWhitelist, g_gammaWhitelistMatch, g_gammaWhitelistOverrideProcess
std::mutex g_colorCorrectionMutex;
std::vector<PendingColorCorrection> g_pendingColorCorrections;
std::atomic<bool> g_hasPendingColorCorrections{ false };

// ============================================================================
// Global Window Handles
// ============================================================================

HWND g_mainHwnd = nullptr;
HWND g_osdHwnd = nullptr;
HWND g_analysisHwnd = nullptr;
std::atomic<bool> g_analysisEnabled{ false };

// ============================================================================
// Single Instance Mutex
// ============================================================================

HANDLE g_singleInstanceMutex = nullptr;

// ============================================================================
// Tearing Support
// ============================================================================

bool g_tearingSupported = false;

// ============================================================================
// SDR White Point
// ============================================================================

float g_sdrWhiteNits = 80.0f;

// ============================================================================
// Watchdog Timer
// ============================================================================

std::chrono::steady_clock::time_point g_lastSuccessfulFrame;

// ============================================================================
// GUI State
// ============================================================================

GUIState g_gui;

// ============================================================================
// Grayscale Editor
// ============================================================================

GrayscaleEditorData* g_grayscaleEditor = nullptr;
