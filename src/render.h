// DesktopLUT - render.h
// Frame rendering and swapchain management

#pragma once

#include "types.h"
#include "whitelist.h"
#include "framepacer.h"

// Create swapchain for a monitor
bool CreateSwapChain(MonitorContext* ctx);

// Initialize DirectComposition device (shared)
bool InitDirectCompositionDevice();

// Initialize Compositor Clock API (Windows 11+ frame timing, DwmFlush fallback on Win10)
void InitCompositorClock();

// Initialize DirectComposition for a monitor
bool InitDirectComposition(MonitorContext* ctx);

// Recreate swapchain (HDR toggle handling)
bool RecreateSwapchain(MonitorContext* ctx);

// Update HDR metadata on swapchain (call when tonemapping settings change)
void UpdateHDRMetadata(MonitorContext* ctx);

// Resize swapchain (display change handling). Returns false on failure.
bool ResizeSwapChain(MonitorContext* ctx, int width, int height);

// Render a single monitor (bufferActive = auto frame buffer is currently engaged)
void RenderMonitor(MonitorContext* ctx, FramePacer* fp, bool bufferActive);

// Main render loop for all monitors
void RenderAll(FramePacer* fp);

// Overlay window procedure
LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Cleanup a single monitor context
void CleanupMonitorContext(MonitorContext* ctx);

// Display power state notification (sleep/wake)
void RegisterDisplayPowerNotification(HWND hwnd);
void UnregisterDisplayPowerNotification();

// Create peak detection resources for dynamic tonemapping
bool CreatePeakDetectionResources(MonitorContext* ctx);

// Reapply MHC ICC profiles after HDR/SDR mode switch
void ReapplyMhcProfilesOnModeSwitch(MonitorContext* ctx);

// ICtCp grayscale conversion (CPU precomputation for HDR per-channel grayscale)
void ComputeGrayscaleICtCpOffsets(GrayscaleData& gs);

// Maximum recovery retries before giving up on a monitor (~5 min at 5s backoff cap)
extern const int MAX_RECOVERY_RETRIES;

// Shared render state (defined in render_init.cpp, used by render.cpp)
extern std::chrono::steady_clock::time_point s_motionBarOrigin;
extern bool s_motionBarOriginSet;
extern int s_watchdogRecoveryAttempts;
extern std::chrono::steady_clock::time_point g_powerNotifyRegisteredTime;
extern const GUID GUID_CONSOLE_DISPLAY_STATE_LOCAL;

// Compositor Clock API availability (for status display)
typedef DWORD (WINAPI *PFN_DCompositionWaitForCompositorClock)(UINT, const HANDLE*, DWORD);
extern PFN_DCompositionWaitForCompositorClock g_pfnWaitForCompositorClock;

// DComposition frame statistics API (Win11+ SDK 10.0.22000+, forward-declared for older SDKs)
#if !__has_include(<dcomptypes.h>)
typedef enum {
    COMPOSITION_FRAME_ID_CREATED = 0,
    COMPOSITION_FRAME_ID_CONFIRMED = 1,
    COMPOSITION_FRAME_ID_COMPLETED = 2
} COMPOSITION_FRAME_ID_TYPE;
typedef ULONG64 COMPOSITION_FRAME_ID;
typedef struct {
    UINT64 startTime;
    UINT64 targetTime;
    UINT64 framePeriod;
} COMPOSITION_FRAME_STATS;
typedef struct {
    LUID displayAdapterLuid;
    LUID renderAdapterLuid;
    UINT vidPnSourceId;
    UINT vidPnTargetId;
    UINT uniqueId;
} COMPOSITION_TARGET_ID;
#endif

typedef HRESULT (WINAPI *PFN_DCompositionGetFrameId)(COMPOSITION_FRAME_ID_TYPE, COMPOSITION_FRAME_ID*);
typedef HRESULT (WINAPI *PFN_DCompositionGetStatistics)(COMPOSITION_FRAME_ID, COMPOSITION_FRAME_STATS*, UINT, COMPOSITION_TARGET_ID*, UINT*);
extern PFN_DCompositionGetFrameId g_pfnDCompGetFrameId;
extern PFN_DCompositionGetStatistics g_pfnDCompGetStatistics;
