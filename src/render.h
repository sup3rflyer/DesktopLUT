// DesktopLUT - render.h
// Frame rendering and swapchain management

#pragma once

#include "types.h"

// Create swapchain for a monitor
bool CreateSwapChain(MonitorContext* ctx);

// Initialize DirectComposition device (shared)
bool InitDirectCompositionDevice();

// Initialize Compositor Clock API (VRR-aware frame timing)
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
void RenderMonitor(MonitorContext* ctx);

// Main render loop for all monitors
void RenderAll();

// Overlay window procedure
LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam);

// Cleanup a single monitor context
void CleanupMonitorContext(MonitorContext* ctx);

// Gamma whitelist polling thread control
void StartGammaWhitelistThread();
void StopGammaWhitelistThread();

// Display power state notification (sleep/wake)
void RegisterDisplayPowerNotification(HWND hwnd);
void UnregisterDisplayPowerNotification();

// Compositor Clock API availability (for status display)
typedef HRESULT (WINAPI *PFN_DCompositionWaitForCompositorClock)(UINT, const HANDLE*, DWORD);
extern PFN_DCompositionWaitForCompositorClock g_pfnWaitForCompositorClock;
