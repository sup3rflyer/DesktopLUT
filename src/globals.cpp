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

// Desktop gamma LUT (shared) - precomputed sRGB→2.2 correction curve
ID3D11Texture2D* g_desktopGammaTexture = nullptr;
ID3D11ShaderResourceView* g_desktopGammaSRV = nullptr;

// PQ transfer function LUTs (shared) - replace all pow() in HDR pixel shader
ID3D11Texture2D* g_pqOetfTexture = nullptr;
ID3D11ShaderResourceView* g_pqOetfSRV = nullptr;
ID3D11Texture2D* g_pqEotfTexture = nullptr;
ID3D11ShaderResourceView* g_pqEotfSRV = nullptr;

// SDR transfer function LUTs (shared) - replace all pow() in SDR pixel shader
ID3D11Texture2D* g_srgbOetfTexture = nullptr;
ID3D11ShaderResourceView* g_srgbOetfSRV = nullptr;
ID3D11Texture2D* g_srgbEotfTexture = nullptr;
ID3D11ShaderResourceView* g_srgbEotfSRV = nullptr;
ID3D11Texture2D* g_gammaRatioTexture = nullptr;
ID3D11ShaderResourceView* g_gammaRatioSRV = nullptr;
ID3D11Texture2D* g_wbGammaTexture = nullptr;
ID3D11ShaderResourceView* g_wbGammaSRV = nullptr;

// ============================================================================
// Monitor State
// ============================================================================

std::vector<MonitorContext> g_monitors;

// ============================================================================
// Atomic Control Flags
// ============================================================================

std::atomic<bool> g_desktopGammaMode{ false };   // Effective gamma state (may be overridden by whitelist)
std::atomic<bool> g_tetrahedralInterp{ false };  // Default: trilinear (tetrahedral opt-in for quality)
std::atomic<bool> g_running{ true };            // Main loop control
std::atomic<bool> g_forceReinit{ false };       // Force reinit on next frame
std::atomic<bool> g_forceMhcReapply{ false };  // Force MHC profile reapply on next reinit (sleep/wake, TDR)
std::atomic<bool> g_forceTopmostReassert{ false }; // Force TOPMOST reassert on next frame
std::atomic<bool> g_selfReassertInProgress{ false }; // Guard: suppress WM_WINDOWPOSCHANGING during our own reasserts
std::atomic<bool> g_logPeakDetection{ false };  // Debug: log detected peak nits to console
std::atomic<bool> g_consoleEnabled{ false };   // Show console window (GUI mode only, default off)
std::atomic<bool> g_showFrameTiming{ false };  // Show frame timing in analysis overlay (default off)
std::atomic<bool> g_showMotionBar{ false };    // Show motion bar for judder detection (default off)
std::atomic<bool> g_overlayAutoSleep{ false };  // true = overlay has nothing to do, windows hidden
HANDLE g_overlayWakeEvent = nullptr;           // Auto-reset event for auto-sleep wake
HANDLE g_topmostEvent = nullptr;              // Signaled when TOPMOST reassert needed
std::atomic<bool> g_framePacerEnabled{ true };  // Enable predictive frame pacer (default: true)
std::atomic<bool> g_framePacerSpinWait{ true }; // Enable spin-wait phase (default: true)
std::atomic<bool> g_frameBufferEnabled{ true };  // Enable auto frame buffer (default: on)
std::atomic<bool> g_framePacerLogEnabled{ false }; // Log frame pacer stats to CSV (default: off)
std::atomic<int> g_frameBufferIdleMs{ 3000 };  // Idle timeout before buffer engages (default: 3s)

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

std::atomic<bool> g_userDesktopGammaMode{ false };      // User's preference (checkbox state)
std::vector<std::wstring> g_gammaWhitelist;            // Parsed exe names (lowercase)
std::wstring g_gammaWhitelistRaw;                      // Raw comma-separated string
std::atomic<bool> g_gammaWhitelistActive{ false };     // A whitelisted process is running
std::wstring g_gammaWhitelistMatch;                    // Name of matched process (for OSD)
std::atomic<bool> g_gammaWhitelistThreadRunning{ false }; // Control flag for whitelist polling thread
std::atomic<bool> g_gammaWhitelistUserOverride{ false };  // User manually toggled while whitelist was active
std::wstring g_gammaWhitelistOverrideProcess;             // Process name when user overrode (lowercase)

// ============================================================================
// VRR Whitelist
// ============================================================================

std::atomic<bool> g_vrrWhitelistEnabled{ false };        // Feature disabled by default
std::vector<std::wstring> g_vrrWhitelist;                // Parsed exe names (lowercase)
std::wstring g_vrrWhitelistRaw;                          // Raw comma-separated string
std::atomic<bool> g_vrrWhitelistActive{ false };         // A whitelisted process is running (overlay hidden)
std::wstring g_vrrWhitelistMatch;                        // Name of matched process
std::mutex g_vrrWhitelistMutex;                          // Protects g_vrrWhitelist, g_vrrWhitelistMatch

// ============================================================================
// Thread Synchronization
// ============================================================================

std::mutex g_gammaWhitelistMutex;  // Protects g_gammaWhitelist, g_gammaWhitelistMatch, g_gammaWhitelistOverrideProcess
std::mutex g_colorCorrectionMutex;
std::mutex g_monitorSettingsMutex; // Protects g_gui.monitorSettings (MHC profileName/enabled fields)
std::vector<PendingColorCorrection> g_pendingColorCorrections;
std::atomic<bool> g_hasPendingColorCorrections{ false };

// ============================================================================
// Global Window Handles
// ============================================================================

std::atomic<HWND> g_mainHwnd{ nullptr };
HWND g_osdHwnd = nullptr;
HWND g_analysisHwnd = nullptr;
std::atomic<bool> g_analysisEnabled{ false };
std::atomic<bool> g_resetPacerStats{ false };

// ============================================================================
// Single Instance Mutex
// ============================================================================

HANDLE g_singleInstanceMutex = nullptr;

// ============================================================================
// Tearing Support
// ============================================================================

std::atomic<bool> g_tearingSupported{ false };

// ============================================================================
// SDR White Point
// ============================================================================

float g_sdrWhiteNits = 80.0f;

// ============================================================================
// Watchdog Timer
// ============================================================================

std::chrono::steady_clock::time_point g_lastSuccessfulFrame;

// ============================================================================
// Display Power State
// ============================================================================

std::atomic<bool> g_displayOff{ false };  // Display is off - skip recovery attempts, wait for wake signal

// ============================================================================
// MHC Edit Dialog State
// ============================================================================

std::atomic<bool> g_mhcEditDialogOpen{ false };

// ============================================================================
// GUI State
// ============================================================================

GUIState g_gui;

// ============================================================================
// Grayscale Editor
// ============================================================================

GrayscaleEditorData* g_grayscaleEditor = nullptr;
