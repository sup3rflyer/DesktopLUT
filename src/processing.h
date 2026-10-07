// DesktopLUT - processing.h
// Processing thread management

#pragma once

#include "types.h"
#include <vector>

// Monitor enumeration callback
BOOL CALLBACK MonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam);

// Processing thread function
void ProcessingThreadFunc(std::vector<MonitorLUTConfig> configs);

// Start processing (GUI mode). Requested from inside a running Stop (a pumped message), it is
// deferred until that Stop completes.
void StartProcessing();

// Stop processing (GUI mode). Re-entrant calls from inside a running Stop are absorbed; from
// inside a running Start, deferred until it completes. Also joins a thread that exited on its
// own while not running.
void StopProcessing();

// One display needs the processing pipeline (a LUT, a shader correction, tonemap, FALD in overlay mode,
// or desktop gamma) — StartProcessing's per-monitor filter.
bool MonitorNeedsProcessing(const MonitorSettings& ms);
// Any LIVE display needs the processing pipeline.
bool AnyMonitorNeedsProcessing();
// Any LIVE display has any correction at all, MHC profiles included (the startup auto-start test).
bool AnyMonitorHasCorrections();

// One display needs the DWM hook resident (hook mode): a .cube, a FALD panel file, or the HDR
// tonemapper. NOT gated on the display's current HDR mode — the user flips HDR while running and the
// hook must already be there (it installs its hooks at attach from this configuration); the DLL gates
// the tonemapper on the live, debounced mode itself. Shared by StartProcessing and the watchdog's
// re-injection so the two cannot disagree. `CC` = ColorCorrectionData or ColorCorrectionSettings.
template <class CC>
bool MonitorNeedsDwmHook(const std::wstring& sdrLut, const std::wstring& hdrLut, const CC& sdr, const CC& hdr) {
    return !sdrLut.empty() || !hdrLut.empty() ||
           !sdr.fald.paramsPath.empty() || !hdr.fald.paramsPath.empty() ||
           hdr.tonemap.enabled;
}

// True while StartProcessing/StopProcessing is executing (their joins pump messages): handlers
// that would start, join or replace the processing thread must leave it to the transition.
bool IsProcessingTransitionActive();

// In DWM hook mode, check if overlay is needed and auto-start/stop it
void DwmHookReevaluateOverlay();

// Lightweight analysis-only thread (DWM hook mode: DD capture + analysis compute, no overlay)
void AnalysisOnlyThreadFunc(unsigned generation);

// Update color correction for a running monitor in real-time
// ictcpMode: when true (HDR editing), shader uses ICtCp offsets for perceptually accurate preview
void UpdateColorCorrectionLive(int monitorIndex, bool isHDR, bool ictcpMode = false);

// Evaluate whether non-analysis shader corrections need the full overlay.
// Always iterates g_gui.monitorSettings — MUST only be called from the GUI thread.
bool EvalNonAnalysisShaderCorrections();

// Check if current settings differ from active (running) settings
bool SettingsChanged();

// Convert GUI ColorCorrectionSettings to runtime ColorCorrectionData
// isHDR: affects primaries matrix direction
//   SDR: sRGB → measured (gamut mapping for uncalibrated displays)
//   HDR: Rec.2020 → measured (correction in Rec.2020 space after BT.709→Rec.2020)
ColorCorrectionData ConvertColorCorrection(const ColorCorrectionSettings& src, bool isHDR);

// Apply MaxTML settings for all monitors that have it enabled
// Call after any event that might reset MaxTML (startup, sleep/wake, TDR, HDR toggle)
void ApplyMaxTmlSettings();
