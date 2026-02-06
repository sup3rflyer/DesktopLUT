// DesktopLUT - globals.h
// Global variable declarations

#pragma once

#include "types.h"
#include <d3d11_4.h>
#include <dcomp.h>
#include <atomic>
#include <mutex>
#include <chrono>
#include <vector>

// ============================================================================
// D3D Device and Resources (shared across all monitors)
// ============================================================================

extern ID3D11Device* g_device;
extern ID3D11DeviceContext* g_context;
extern ID3D11VertexShader* g_vs;
extern ID3D11PixelShader* g_ps;
extern ID3D11ComputeShader* g_peakDetectCS;  // Compute shader for dynamic peak detection
extern ID3D11Buffer* g_peakCB;               // Constant buffer for peak detection parameters
extern ID3D11ComputeShader* g_analysisCS;    // Compute shader for frame analysis
extern ID3D11Buffer* g_analysisCB;           // Constant buffer for analysis parameters
extern ID3D11SamplerState* g_samplerPoint;
extern ID3D11SamplerState* g_samplerLinear;
extern ID3D11SamplerState* g_samplerWrap;
extern ID3D11Buffer* g_constantBuffer;

// DirectComposition device (shared)
extern IDCompositionDevice* g_dcompDevice;

// Blue noise texture (shared)
extern ID3D11Texture2D* g_blueNoiseTexture;
extern ID3D11ShaderResourceView* g_blueNoiseSRV;

// ============================================================================
// Monitor State
// ============================================================================

extern std::vector<MonitorContext> g_monitors;

// ============================================================================
// Atomic Control Flags
// ============================================================================

extern std::atomic<bool> g_desktopGammaMode;   // Effective gamma state (may be overridden by whitelist)
extern std::atomic<bool> g_tetrahedralInterp;  // true = tetrahedral, false = trilinear
extern std::atomic<bool> g_running;            // Main loop control
extern std::atomic<bool> g_forceReinit;        // Force reinit on next frame
extern std::atomic<bool> g_forceTopmostReassert; // Force TOPMOST reassert on next frame
extern std::atomic<bool> g_logPeakDetection;   // Debug: log detected peak nits to console
extern std::atomic<bool> g_consoleEnabled;     // Show console window (GUI mode only)
extern std::atomic<bool> g_showFrameTiming;    // Show frame timing in analysis overlay

// ============================================================================
// Hotkey Settings
// ============================================================================

extern std::atomic<bool> g_hotkeyGammaEnabled;    // Enable Win+Shift+G hotkey
extern std::atomic<bool> g_hotkeyHdrEnabled;      // Enable Win+Shift+H hotkey
extern std::atomic<bool> g_hotkeyAnalysisEnabled; // Enable Win+Shift+X hotkey
extern char g_hotkeyGammaKey;                     // Key for gamma toggle (default 'G')
extern char g_hotkeyHdrKey;                       // Key for HDR toggle (default 'H')
extern char g_hotkeyAnalysisKey;                  // Key for analysis toggle (default 'X')
extern std::atomic<bool> g_startMinimized;        // Start minimized to tray

// ============================================================================
// Gamma Whitelist (auto-disable gamma when whitelisted apps are running)
// ============================================================================

extern std::atomic<bool> g_userDesktopGammaMode;       // User's preference (checkbox state)
extern std::vector<std::wstring> g_gammaWhitelist;     // Parsed exe names (lowercase) - protected by g_gammaWhitelistMutex
extern std::wstring g_gammaWhitelistRaw;               // Raw comma-separated string for GUI/persistence
extern std::atomic<bool> g_gammaWhitelistActive;       // A whitelisted process is currently running
extern std::wstring g_gammaWhitelistMatch;             // Name of the matched process - protected by g_gammaWhitelistMutex
extern std::atomic<bool> g_gammaWhitelistThreadRunning; // Control flag for whitelist polling thread
extern std::atomic<bool> g_gammaWhitelistUserOverride; // User manually toggled while whitelist was active
extern std::wstring g_gammaWhitelistOverrideProcess;   // Process name when user overrode - protected by g_gammaWhitelistMutex

// ============================================================================
// VRR Whitelist (auto-hide overlay when whitelisted apps are running)
// ============================================================================

extern std::atomic<bool> g_vrrWhitelistEnabled;        // Feature enabled
extern std::vector<std::wstring> g_vrrWhitelist;       // Parsed exe names (lowercase) - protected by g_vrrWhitelistMutex
extern std::wstring g_vrrWhitelistRaw;                 // Raw comma-separated string for GUI/persistence
extern std::atomic<bool> g_vrrWhitelistActive;         // A whitelisted process is currently running (overlay hidden)
extern std::wstring g_vrrWhitelistMatch;               // Name of the matched process - protected by g_vrrWhitelistMutex
extern std::mutex g_vrrWhitelistMutex;                 // Protects g_vrrWhitelist, g_vrrWhitelistMatch

// ============================================================================
// Thread Synchronization
// ============================================================================

extern std::mutex g_gammaWhitelistMutex;  // Protects g_gammaWhitelist, g_gammaWhitelistMatch, g_gammaWhitelistOverrideProcess
extern std::mutex g_colorCorrectionMutex;
extern std::vector<PendingColorCorrection> g_pendingColorCorrections;
extern std::atomic<bool> g_hasPendingColorCorrections;  // Fast check to avoid mutex lock

// ============================================================================
// Global Window Handles
// ============================================================================

extern HWND g_mainHwnd;     // First monitor's overlay window (for hotkey registration)
extern HWND g_osdHwnd;      // On-screen display window
extern HWND g_analysisHwnd; // Analysis overlay window
extern std::atomic<bool> g_analysisEnabled;  // Analysis overlay visibility

// ============================================================================
// Single Instance Mutex
// ============================================================================

extern HANDLE g_singleInstanceMutex;

// ============================================================================
// Tearing Support
// ============================================================================

extern bool g_tearingSupported;

// ============================================================================
// SDR White Point
// ============================================================================

extern float g_sdrWhiteNits;

// ============================================================================
// Watchdog Timer
// ============================================================================

extern std::chrono::steady_clock::time_point g_lastSuccessfulFrame;

// ============================================================================
// Display Power State
// ============================================================================

extern std::atomic<bool> g_displayOff;  // Display is off - skip recovery attempts, wait for wake signal

// ============================================================================
// GUI State
// ============================================================================

extern GUIState g_gui;

// ============================================================================
// Grayscale Editor
// ============================================================================

extern GrayscaleEditorData* g_grayscaleEditor;
