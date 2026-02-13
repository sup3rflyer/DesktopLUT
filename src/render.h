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

// Resize swapchain (display change handling)
void ResizeSwapChain(MonitorContext* ctx, int width, int height);

// Render a single monitor
void RenderMonitor(MonitorContext* ctx, FramePacer* fp);

// Main render loop for all monitors
void RenderAll(FramePacer* fp);

// Overlay window procedure
LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Cleanup a single monitor context
void CleanupMonitorContext(MonitorContext* ctx);

// Display power state notification (sleep/wake)
void RegisterDisplayPowerNotification(HWND hwnd);
void UnregisterDisplayPowerNotification();

// Compositor Clock API availability (for status display)
typedef DWORD (WINAPI *PFN_DCompositionWaitForCompositorClock)(UINT, const HANDLE*, DWORD);
extern PFN_DCompositionWaitForCompositorClock g_pfnWaitForCompositorClock;
