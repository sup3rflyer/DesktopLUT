// DesktopLUT - analysis.h
// Real-time frame analysis overlay

#pragma once

#include "types.h"

// AnalysisResult is defined in types.h

// Overlay management
bool CreateAnalysisOverlay(HINSTANCE hInstance);
void DestroyAnalysisOverlay();
void ShowAnalysisOverlay();
void HideAnalysisOverlay();
void ToggleAnalysisOverlay();
bool IsAnalysisOverlayVisible();

// GPU resources (per-monitor, but only used for primary)
bool CreateAnalysisResources(MonitorContext* ctx);
void ReleaseAnalysisResources(MonitorContext* ctx);

// Per-frame dispatch (called from RenderMonitor)
void DispatchAnalysisCompute(MonitorContext* ctx);

// Async readback and display update (called from RenderMonitor)
void UpdateAnalysisDisplay(MonitorContext* ctx);
