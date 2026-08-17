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

// Desktop gamma LUT (shared) - precomputed sRGB→2.2 correction curve
extern ID3D11Texture2D* g_desktopGammaTexture;
extern ID3D11ShaderResourceView* g_desktopGammaSRV;

// PQ transfer function LUTs (shared) - replace all pow() in HDR pixel shader
extern ID3D11Texture2D* g_pqOetfTexture;   // Linear→PQ, sqrt-domain (4096 entries)
extern ID3D11ShaderResourceView* g_pqOetfSRV;
extern ID3D11Texture2D* g_pqEotfTexture;   // PQ→Linear, uniform (4096 entries)
extern ID3D11ShaderResourceView* g_pqEotfSRV;

// SDR transfer function LUTs (shared) - replace all pow() in SDR pixel shader
extern ID3D11Texture2D* g_srgbOetfTexture;     // Linear→sRGB (1024 entries)
extern ID3D11ShaderResourceView* g_srgbOetfSRV;
extern ID3D11Texture2D* g_srgbEotfTexture;     // sRGB→Linear (1024 entries)
extern ID3D11ShaderResourceView* g_srgbEotfSRV;
extern ID3D11Texture2D* g_gammaRatioTexture;   // pow(Y, 1/11) ratio for 2.4 gamma (1024 entries)
extern ID3D11ShaderResourceView* g_gammaRatioSRV;
extern ID3D11Texture2D* g_wbGammaTexture;      // pow(gain, 1/2.2) for WB gains [0,2] (512 entries)
extern ID3D11ShaderResourceView* g_wbGammaSRV;

// Per-channel SDR base 1D LUT for the grayscale FULL-PREVIEW (realization A; t11/t12/t13).
// DYNAMIC — re-uploaded by the render thread on full-preview begin (1024 entries each).
extern ID3D11Texture2D* g_baseLutPreviewTexR;
extern ID3D11ShaderResourceView* g_baseLutPreviewSRV_R;
extern ID3D11Texture2D* g_baseLutPreviewTexG;
extern ID3D11ShaderResourceView* g_baseLutPreviewSRV_G;
extern ID3D11Texture2D* g_baseLutPreviewTexB;
extern ID3D11ShaderResourceView* g_baseLutPreviewSRV_B;

// ============================================================================
// Monitor State
// ============================================================================

extern std::vector<MonitorContext> g_monitors;
extern std::mutex g_monitorsMutex; // Protects g_monitors structure (push_back/clear) against
                                   // cross-thread iteration. Render thread reads lock-free.

// ============================================================================
// Atomic Control Flags
// ============================================================================

extern std::atomic<bool> g_desktopGammaMode;   // Effective gamma state (may be overridden by whitelist)
extern std::atomic<bool> g_tetrahedralInterp;  // true = tetrahedral, false = trilinear
extern std::atomic<bool> g_running;            // Main loop control
extern std::atomic<bool> g_forceReinit;        // Force reinit on next frame
extern std::atomic<bool> g_forceMhcReapply;    // Force MHC profile reapply on next reinit (sleep/wake, TDR)
extern std::atomic<bool> g_forceTopmostReassert; // Force TOPMOST reassert on next frame
extern std::atomic<bool> g_selfReassertInProgress; // Guard: suppress WM_WINDOWPOSCHANGING during our own reasserts
extern std::atomic<bool> g_logPeakDetection;   // Debug: log detected peak nits to console
extern std::atomic<bool> g_consoleEnabled;     // Show console window (GUI mode only)
extern std::atomic<bool> g_showFrameTiming;    // Show frame timing in analysis overlay
extern std::atomic<bool> g_showMotionBar;      // Show motion bar for judder detection (UFO test style)
extern std::atomic<bool> g_overlayAutoSleep;   // true = overlay has nothing to do, windows hidden
// true ⇔ the analysis/correction OVERLAY is active for ≥1 monitor (= anyMonitorNeedsOverlay).
// Drives the tray icon + IPC corrections_enabled. NOT "calibration is applied" and NOT the
// DWM-hook state: in hook mode this is FALSE even while a cube is live. See docs/NAMING.md §4.
extern std::atomic<bool> g_shaderCorrectionsActive;  // true = shader is applying corrections (not just LUT passthrough)
extern std::atomic<bool> g_nonAnalysisCorrectionsActive;  // true = non-analysis corrections need the overlay (cached for analysis-only thread)
extern HANDLE g_overlayWakeEvent;              // Auto-reset event for auto-sleep wake (replaces Sleep polling)
extern HANDLE g_topmostEvent;                 // Signaled when TOPMOST reassert needed (helper thread)
extern std::atomic<bool> g_framePacerEnabled;  // Enable predictive frame pacer (default: true)
extern std::atomic<bool> g_framePacerSpinWait; // Enable spin-wait phase (default: true)
extern std::atomic<bool> g_frameBufferEnabled; // Enable auto frame buffer (engages on idle)
extern std::atomic<bool> g_framePacerLogEnabled; // Log frame pacer stats to CSV
extern std::atomic<int> g_frameBufferIdleMs;   // Idle timeout before buffer engages (ms, 0 = always active)
extern std::atomic<bool> g_dwmHookMode;        // Use DWM hook injection instead of overlay for LUT
extern std::atomic<bool> g_calibrationControlEnabled;  // Arm the opt-in DLC calibration IPC server (default off)
extern int g_dwmHookWatchdogRetries;           // Consecutive re-injection failures (GUI thread only)
extern int g_dwmHookConfigResends;             // Countdown of extra shared-config resends after a topology
                                               // change or HDR/SDR mode flip so the hook's suspicious-change
                                               // debounce can converge (GUI thread only —
                                               // DWM_HOOK_RESEND_TIMER_ID decrements it)
extern std::atomic<bool> g_hookOnlyHotkeys;    // Hotkeys registered on GUI window (hook-only mode)
extern std::atomic<bool> g_analysisOnlyMode;   // Lightweight analysis-only thread running (no overlay)
extern std::atomic<unsigned> g_analysisThreadGen; // Analysis-only thread generation. A thread whose captured
                                               // generation goes stale (bumped on detach or by a successor
                                               // start) exits its loop and skips shared-state teardown, so a
                                               // detached zombie can't corrupt g_monitors/shared D3D under a
                                               // newly started thread
extern std::atomic<int> g_analysisThreadAlive; // Count of analysis-only threads currently executing (normally
                                               // 0/1; >0 while a detached zombie is still winding down)

// ============================================================================
// Hotkey Settings
// ============================================================================

extern std::atomic<bool> g_hotkeyGammaEnabled;    // Enable Win+Shift+G hotkey
extern std::atomic<bool> g_hotkeyHdrEnabled;      // Enable Win+Shift+H hotkey
extern std::atomic<bool> g_hotkeyAnalysisEnabled; // Enable Win+Shift+X hotkey
extern std::atomic<char> g_hotkeyGammaKey;        // Key for gamma toggle (default 'G')
extern std::atomic<char> g_hotkeyHdrKey;          // Key for HDR toggle (default 'Z')
extern std::atomic<char> g_hotkeyAnalysisKey;     // Key for analysis toggle (default 'X')
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
extern std::mutex g_monitorSettingsMutex; // Protects g_gui.monitorSettings (MHC profileName/enabled fields)
extern std::vector<PendingColorCorrection> g_pendingColorCorrections;
extern std::atomic<bool> g_hasPendingColorCorrections;  // Fast check to avoid mutex lock

// ============================================================================
// Global Window Handles
// ============================================================================

extern std::atomic<HWND> g_mainHwnd;     // First monitor's overlay window (for hotkey registration)
extern HWND g_osdHwnd;      // On-screen display window
extern HWND g_analysisHwnd; // Analysis overlay window
extern std::atomic<bool> g_analysisEnabled;       // Analysis overlay visibility
extern std::atomic<bool> g_resetPacerStats;       // Signal render thread to reset pacer diagnostics

// ============================================================================
// Single Instance Mutex
// ============================================================================

extern HANDLE g_singleInstanceMutex;

// ============================================================================
// Tearing Support
// ============================================================================

extern std::atomic<bool> g_tearingSupported;

// ============================================================================
// SDR White Point
// ============================================================================

extern float g_sdrWhiteNits;

// ============================================================================
// Watchdog Timer
// ============================================================================

extern AtomicTimePoint g_lastSuccessfulFrame;

// ============================================================================
// Display Power State
// ============================================================================

extern std::atomic<bool> g_displayOff;  // Display is off - skip recovery attempts, wait for wake signal

// ============================================================================
// MHC Edit Dialog State
// ============================================================================

extern std::atomic<bool> g_mhcEditDialogOpen;  // Suppress profile monitoring during edit dialog

// ============================================================================
// GUI State
// ============================================================================

extern GUIState g_gui;

// ============================================================================
// Grayscale Editor
// ============================================================================

extern GrayscaleEditorData* g_grayscaleEditor;
