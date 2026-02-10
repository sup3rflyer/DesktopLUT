// DesktopLUT - gui_mhc.h
// MHC settings dialog and helpers

#pragma once

#include <windows.h>
#include <string>

struct MHCSettings;

// Update MHC flags on the running MonitorContext
void UpdateMhcFlagsLive(int monitorIndex);

// Compute metadata strings for display in MHC section labels
void ComputeMhcMetadata(MHCSettings& mhc, bool isHDR);

// Generate, write, and install MHC2 ICC profile from current MHCSettings
bool GenerateAndInstallMhcProfile(int monitorIndex, bool isHDR);

// Auto-regenerate and reinstall MHC profile when MHC settings change
void RegenerateMhcIfActive(int monitorIndex, bool isHDR);

// Update MHC info labels in the appropriate SDR or HDR groupbox
void UpdateMhcInfoDisplay(int monitorIndex, bool isHDR);

// Helper to recalculate primaries matrix and apply live update
void ApplyPrimariesChange(bool isHDR);

// Show MHC settings edit dialog (modal)
void ShowMhcSettingsDialog(HWND hwndParent, MHCSettings& settings, bool isHDR, int monitorIndex,
                           bool livePreview = false, bool hadProfile = false,
                           const std::wstring& origProfileName = L"",
                           const std::wstring& origProfilePath = L"");
