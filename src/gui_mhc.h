// DesktopLUT - gui_mhc.h
// MHC settings dialog and helpers

#pragma once

#include <windows.h>
#include <string>

struct MHCSettings;

// Start overlay/processing for MHC live preview if not already running (defined
// in gui.cpp). Sets livePreview=true only when the running monitor's mode matches
// isHDR; startedForPreview / startedOverlayForPreview report what this call spun up
// so the caller can tear it back down. Drives the correction-grayscale live editor
// from both the GUI and the calibration IPC server.
void EnsureProcessingForPreview(int monIdx, bool isHDR,
                                bool& livePreview,
                                bool& startedForPreview,
                                bool& startedOverlayForPreview);

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

// ============================================================================
// Permutation Profile System
// ============================================================================
// MHC inline corrections (WB, DG, GS) are baked into ICC profiles. To toggle
// any correction at runtime (hotkey, whitelist) without regeneration delay, we
// cache profile variants keyed by a 3-bit permutation bitmask (see MHCSettings::PERM_*).
// Profiles are generated on-demand and cached until the base calibration data changes.

// Compute the permutation bitmask from current MHCSettings enable flags
uint8_t ComputeMhcPermutation(const MHCSettings& mhc, bool isHDR);

// Ensure a specific permutation profile exists in the system color directory.
// Generates on-demand if not cached. Thread-safe (takes g_monitorSettingsMutex internally).
bool EnsureMhcPermProfile(int monitorIndex, bool isHDR, uint8_t perm);

// Swap the active MHC ICC profile to a different permutation.
// Calls EnsureMhcPermProfile, then Remove+Reassociate. Updates profileName/profilePath.
bool SwapMhcToPermutation(int monitorIndex, bool isHDR, uint8_t newPerm);

// Toggle the DG bit in the active permutation for all HDR monitors.
// Called by hotkey handlers and whitelist when desktop gamma changes at runtime.
void SwapDgForAllMonitors(bool dgEnabled);
