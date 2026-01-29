// DesktopLUT - gpu.h
// D3D device management and GPU resources

#pragma once

#include "types.h"

// Initialize D3D11 device, context, shaders, and resources
bool InitD3D();

// Check if tearing (immediate present) is supported
bool CheckTearingSupport();

// Release D3D resources for a specific monitor
void ReleaseMonitorD3DResources(MonitorContext* ctx);

// Release all shared D3D resources
void ReleaseSharedD3DResources();

// Attempt to recover from GPU device loss (TDR)
bool AttemptDeviceRecovery();
