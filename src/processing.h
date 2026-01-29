// DesktopLUT - processing.h
// Processing thread management

#pragma once

#include "types.h"
#include <vector>

// Monitor enumeration callback
BOOL CALLBACK MonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam);

// Processing thread function
void ProcessingThreadFunc(std::vector<MonitorLUTConfig> configs);

// Start processing (GUI mode)
void StartProcessing();

// Stop processing (GUI mode)
void StopProcessing();

// Update color correction for a running monitor in real-time
void UpdateColorCorrectionLive(int monitorIndex, bool isHDR);

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
