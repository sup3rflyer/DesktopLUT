#pragma once
// User actions shared by the classic window, the ImGui interface and (where it fits) the calibration
// pipe. Each takes the display and mode explicitly (never g_gui.currentMonitor), writes the settings
// under g_monitorSettingsMutex, calls the existing helpers OUTSIDE that lock (they lock internally,
// pump messages, or both), saves, and refreshes whichever window shows the display.
//
// GUI thread only. Several actions stop/start processing or regenerate MHC profiles, which pump
// messages: a caller must not hold references into g_gui.monitorSettings across a call.

#include "types.h"
#include "actions/notify.h"
#include <functional>
#include <string>

namespace act {

struct Result {
    notify::Sev sev = notify::Sev::Info;
    std::wstring text;      // empty = nothing to tell the user
    bool changed = false;   // a setting actually changed
    bool ok() const { return sev != notify::Sev::Warning && sev != notify::Sev::Error; }
};

bool ValidMonitor(int mon);

// ---- 3D LUT / engine -------------------------------------------------------------------------
Result SetLutPath(int mon, bool hdr, const std::wstring& path);   // empty path = clear
void SetTetrahedral(bool on);
void SetHdrDither(bool on);
void SetDwmHookMode(bool on);

// ---- Tone mapping (HDR settings) ---------------------------------------------------------------
// Values are clamped to 10..10000 nits. Enabling while stopped starts processing (as the classic box did).
Result SetTonemap(int mon, const TonemapSettings& t);

// ---- Windows HDR peak (MaxTML) -----------------------------------------------------------------
Result SetMaxTmlEnabled(int mon, bool on);         // stored; applied at start
Result SetMaxTmlPeak(int mon, float nits);         // clamped 100..10000; stored
Result ApplyMaxTmlNow(int mon, float nits);        // stores and writes it to the display now

// ---- FALD (per mode, plus rows shared by both modes) ----------------------------------------------
// The refusal message when a panel file's transfer (PQ / gamma fit) does not match the mode; empty when
// it matches or the file is unreadable (the render side then decides).
std::wstring FaldTransferMismatch(const std::wstring& path, bool hdr);
Result SetFaldEnabled(int mon, bool hdr, bool on);
// Sets the panel file; `reload` also bumps reloadSeq when the path is unchanged (re-browsing = reload).
Result SetFaldPanelFile(int mon, bool hdr, const std::wstring& path, bool reload);
// Applies `edit` to both modes' FALD settings under the lock (the shared rows: view, pedestal, LED lag,
// starfield). `edit` returns true when it changed something; only then is the change pushed and saved.
Result EditFaldShared(int mon, const std::function<bool(FaldSettings&)>& edit);

// ---- MHC profile (per mode) -----------------------------------------------------------------------
Result MhcInstall(int mon, bool hdr);
Result MhcRemove(int mon, bool hdr);               // identity first, then deletes the profile and its variants
Result MhcSetWhiteBalanceEnabled(int mon, bool hdr, bool on);
Result MhcSetWhiteBalanceXY(int mon, bool hdr, float wx, float wy);   // clamped x 0.20-0.60, y 0.20-0.50
Result MhcSetCorrectionGsEnabled(int mon, bool hdr, bool on);
Result MhcSetCorrectionGsPoints(int mon, bool hdr, int count);        // 10 / 20 / 32; resets the curve
Result MhcResetCorrectionGs(int mon, bool hdr);
Result MhcSet24Gamma(int mon, bool on);                               // SDR fine-tune
Result MhcSetCorrectionGsPeak(int mon, float nits);                   // HDR fine-tune, 10..10000
Result MhcSetDesktopGamma(int mon, bool on);                          // HDR

// ---- Display name ----------------------------------------------------------------------------------
// The user's name for a display, live or remembered. Found by storage slot (live or parked); a live
// display not yet given a slot is found by its live index. Empty = back to the automatic name.
Result SetDisplayLabel(int slot, int liveIndex, const std::wstring& label);

// ---- Settings ------------------------------------------------------------------------------------
void SetHotkeyEnabled(int hotkeyId, bool on);      // HOTKEY_GAMMA / HOTKEY_HDR_TOGGLE / HOTKEY_ANALYSIS
void SetStartMinimized(bool on);
void SetRunAtStartup(bool on);
void SetConsoleLog(bool on);
void SetVrrWhitelistEnabled(bool on);

// ---- Live layers -----------------------------------------------------------------------------------
// The classic Start / Apply: resume (act::SetLiveLayersSuspended) and restart, which re-reads the cube
// files at their paths. Starting and stopping otherwise follow the configuration (actions/reconcile.h).
void ReloadLiveLayers();

}  // namespace act
