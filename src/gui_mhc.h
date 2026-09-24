// DesktopLUT - gui_mhc.h
// MHC settings dialog and helpers

#pragma once

#include <windows.h>
#include <string>

struct MHCSettings;

// Outcome of the live-preview mode gate (see EvaluatePreviewModeGate).
enum class PreviewModeGate {
    Ready,     // context mode matches the requested mode — preview can engage
    Mismatch,  // monitor genuinely is not in the requested mode — fail the gate
    StaleCtx,  // OS is in the requested mode but the cached context mode predates
               // a runtime HDR toggle — force a duplication reinit, then re-check
};

// Pure decision for the live-preview mode gate (defined in gui.cpp, exposed for
// tests). requestedHDR = the mode the caller wants to preview in; ctxHDR = the
// processing thread's cached MonitorContext::isHDREnabled; freshQueryOk/freshHDR =
// result of a fresh-factory DXGI query of the monitor's actual color space (the
// same source windows.query_monitors uses). The cached context mode goes stale
// when the overlay sleeps through a runtime HDR toggle: an auto-slept render loop
// never pumps AcquireNextFrame, so it never sees the ACCESS_LOST/format change
// that re-derives the mode. Trusting the cache alone therefore both false-fails
// the monitor's actual mode and false-passes the stale one; the fresh query is
// authoritative when available, with the cache as fallback.
PreviewModeGate EvaluatePreviewModeGate(bool requestedHDR, bool ctxHDR,
                                        bool freshQueryOk, bool freshHDR);

// Start overlay/processing for MHC live preview if not already running (defined
// in gui.cpp). Sets livePreview=true only when the monitor's actual mode matches
// isHDR (verified against a fresh DXGI query, resyncing a stale running context
// if needed); startedForPreview / startedOverlayForPreview report what this call
// spun up so the caller can tear it back down. Drives the correction-grayscale
// live editor from both the GUI and the calibration IPC server.
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

// ============================================================================
// SDR grayscale FULL-PREVIEW scanout (realization A; CODEX_PREVIEW_BAKE_PROMPT.md)
// ============================================================================
#include <vector>

// Compute the matrix (row-major 3x3) + per-channel base 1D LUT the full-preview shader
// reproduces, for the PERM_GS-stripped SDR perm. SDR only; false on failure.
bool ComputeSdrPreviewScanout(int monitorIndex, uint8_t strippedPerm,
                              float outResult9[9],
                              std::vector<float>& outBaseLutR,
                              std::vector<float>& outBaseLutG,
                              std::vector<float>& outBaseLutB);

// Engage/disengage a transient identity SDR scanout profile during the full-preview.
// Engage returns the installed passthrough profile name (empty on failure).
std::wstring EngageSdrPassthroughScanout(int monitorIndex);
void DisengageSdrPassthroughScanout(int monitorIndex, const std::wstring& passthroughName);

// ============================================================================
// Identity (neutral) MHC2 profile on explicit Remove / disable (see mhc.h)
// ============================================================================
// Windows keeps applying the LAST associated MHC2 transform after a disassociation (HW-proven
// 2026-09-03 / 2026-09-23), so explicit Remove/disable paths associate the stable per display/mode
// identity profile (DesktopLUT_Display<slot>_<MODE>_Identity.icm; Mon<N> for an unidentified
// display) FIRST, then disassociate the old profile. Never on app exit. The Engage/Replace/Disengage
// helpers briefly take g_monitorSettingsMutex (settings slot lookup) — call them WITHOUT it held.

// The luminance-metadata peak (lumi tag / MHC2 MaxCLL) the mode's normal profile carries for these
// settings — single source of truth shared by BuildMHC2Params and the identity profile.
struct MonitorSettings;
float MhcProfileMetadataPeakNits(const MHCSettings& mhc, bool isHDR);

// Peak metadata the identity profile carries (HDR; SDR is pinned to 80 nits by the writer):
// the monitor's Display Peak Override (MaxTML) peak when enabled, else the removed profile's
// MhcProfileMetadataPeakNits (its configured peak, or the params default of 1000). Pure.
float MhcIdentityPeakNits(const MonitorSettings& ms, bool isHDR);

// Install + associate (as the active default) the identity profile for monitor/mode.
// Returns the associated profile name (empty on failure).
std::wstring EngageIdentityMhcProfile(int monitorIndex, bool isHDR, float peakNits);

// Associate the identity profile, THEN disassociate oldProfileName (skipped when empty). The old
// profile is disassociated even if the identity install fails (the caller asked for removal).
// Returns the associated identity profile name (empty if the identity association failed).
std::wstring ReplaceMhcProfileWithIdentity(int monitorIndex, bool isHDR, float peakNits,
                                           const std::wstring& oldProfileName);

// Drop the identity association for monitor/mode (a real profile is the active default again).
// Quiet no-op when not associated. GenerateAndInstallMhcProfile / RegenerateMhcIfActive call this
// after every successful real install.
void DisengageIdentityMhcProfile(int monitorIndex, bool isHDR);
