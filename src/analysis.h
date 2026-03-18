// DesktopLUT - analysis.h
// Real-time frame analysis overlay

#pragma once

#include "types.h"

// AnalysisResult is defined in types.h

// Data passed to UI thread for formatting (used by both render thread and hook polling)
struct AnalysisDisplayData {
    AnalysisResult result;
    bool isHDR;
    float targetPeak;
    float sessionMaxCLL;
    float sessionMaxFALL;
    // Tonemap state for TM indicator
    bool tonemapEnabled;
    bool tonemapDynamic;
    float tonemapSourcePeak;   // Static mode: configured source peak
    float tonemapTargetPeak;   // Target peak (display capability)
    float detectedPeak;        // Dynamic mode: GPU-detected peak
    // Frame timing
    FrameTimingStats frameTiming;
};

// Shared state for analysis data handoff (non-static for hook polling access)
extern AnalysisDisplayData g_pendingAnalysis;
extern std::atomic<bool> g_analysisDataReady;

// WM_UPDATE_ANALYSIS defined in types.h alongside other WM_USER messages

// Overlay management
bool CreateAnalysisOverlay(HINSTANCE hInstance);
void DestroyAnalysisOverlay();
void ShowAnalysisOverlay();
void HideAnalysisOverlay();
void ToggleAnalysisOverlay();
bool IsAnalysisOverlayVisible();

// GPU resources (per-monitor, but only used for primary)
bool CreateAnalysisResources(MonitorContext* ctx);
// Per-frame dispatch (called from RenderMonitor)
void DispatchAnalysisCompute(MonitorContext* ctx);

// Async readback and display update (called from RenderMonitor)
void UpdateAnalysisDisplay(MonitorContext* ctx);
