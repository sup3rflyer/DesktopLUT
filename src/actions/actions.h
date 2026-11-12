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

// ---- Windows tone map (what Windows tells apps the HDR peak is: windows_tonemap.h) ---------------
Result SetWindowsToneMap(int mon, WindowsToneMap mode);   // stored, and in force at once

// ---- FALD (per mode, plus rows shared by both modes) ----------------------------------------------
// The refusal message when a panel file's transfer (PQ / gamma fit) does not match the mode; empty when
// it matches or the file is unreadable (the render side then decides).
std::wstring FaldTransferMismatch(const std::wstring& path, bool hdr);
Result SetFaldEnabled(int mon, bool hdr, bool on);
// Sets the panel file; `reload` also bumps reloadSeq when the path is unchanged (re-browsing = reload).
Result SetFaldPanelFile(int mon, bool hdr, const std::wstring& path, bool reload);
// Takes a mode's panel file away and switches its layer off (the stage goes; the shared rows keep their values).
Result ClearFaldPanelFile(int mon, bool hdr);
// Applies `edit` to both modes' FALD settings under the lock (the shared rows: view, pedestal, LED lag,
// starfield). `edit` returns true when it changed something; only then is the change pushed and saved.
Result EditFaldShared(int mon, const std::function<bool(FaldSettings&)>& edit);

// Edits for EditFaldShared (pure; tested). Each returns true when it changed something.
// The debug view (FaldSettings::debugMode, not persisted): 0 the corrected image, 1..9 the views fald_shader.h
// draws; out of range = 0.
constexpr unsigned int kFaldDebugViews = 10;
bool SetFaldDebugView(FaldSettings& f, unsigned int view);
// A view is set on some connected display, either mode (it replaces the picture while that layer runs). GUI thread.
bool FaldViewOn();
// Every display back to the corrected picture (Esc, the tray's "End FALD view"). True = a view was on.
bool ClearFaldViews();
// The starfield fields the interfaces show (the others are INI / pipe only), clamped to the ranges the classic rows
// use: even and strength 0..1, spread (targetSigma) 0..4, keep 0..10000 nits, reach (evenReach) whole zones
// 0..FALD_STAR_EVEN_REACH_MAX. NaN and negatives = 0.
enum class FaldStarField { Even, Strength, Spread, Keep, Reach };
bool SetFaldStarField(FaldStarfieldSettings& s, FaldStarField field, float v);

// ---- MHC profile (per mode) -----------------------------------------------------------------------
Result MhcInstall(int mon, bool hdr);
Result MhcRemove(int mon, bool hdr);               // identity first, then deletes the profile and its variants
Result MhcSetWhiteBalanceEnabled(int mon, bool hdr, bool on);
Result MhcSetWhiteBalanceXY(int mon, bool hdr, float wx, float wy);   // clamped x 0.20-0.60, y 0.20-0.50
Result MhcSetCorrectionGsEnabled(int mon, bool hdr, bool on);
Result MhcSetCorrectionGsPoints(int mon, bool hdr, int count);        // 10 / 20 / 32; resets the curve
Result MhcResetCorrectionGs(int mon, bool hdr);
Result MhcSet24Gamma(int mon, bool on);                               // SDR fine-tune

// ---- Stage bypass (the interface's per-stage switches) -------------------------------------------
// A bypassed stage keeps its settings and is left out of what runs: the 3D LUT stays loaded (the hook
// skips it; the overlay restarts without it), an MHC stage is swapped for the profile variant without it.
// Refused while a calibration session or live grayscale edit runs (it measures what it set up).
enum class MhcStage { Primaries, Grayscale };
Result SetLutBypass(int mon, bool hdr, bool bypass);
Result MhcSetStageBypass(int mon, bool hdr, MhcStage stage, bool bypass);
Result MhcSetCorrectionGsPeak(int mon, float nits);                   // HDR fine-tune, 10..10000
Result MhcSetDesktopGamma(int mon, bool on);                          // HDR

// ---- SDR content in HDR: gamma 2.2 (the Pipeline page's one-step desktop gamma) -----------------------
// On: exactly MhcSetDesktopGamma(mon, true) — the GRAYSCALE inspector's DESKTOP GAMMA switch, which installs a neutral
// HDR MHC profile when the display has none. Off: desktop gamma off and, while the HDR MHC settings hold nothing else
// (ui::HdrMhcOnlyDesktopGamma), the profile removed by MhcRemove's path (the identity profile first, then the
// profile and its variants deleted). With anything else in the profile it is MhcSetDesktopGamma(mon, false).
Result SetSdrGamma22(int mon, bool on);

// Pure (tested): the peak the one step's profile reports (MHCSettings::panelPeakNits), decided just before it puts the
// first profile of ours on the display. What Windows reports now is what HDR apps are told today — the EDID's value or
// the user's own default profile's (the Windows HDR Calibration app's) — unless it is ours (our profile is the default:
// its peak came from us; or the Windows tone map's 10000-nit bypass): then the stored value, else the EDID's, else none
// (0: the profile keeps the 1000-nit params default, and the interface says so). Only plausible panel peaks count.
enum class NeutralPeakSource { Reported, Kept, Edid, Unknown };
struct NeutralPeak { float nits = 0.0f; NeutralPeakSource source = NeutralPeakSource::Unknown; };
NeutralPeak DecideNeutralProfilePeak(float storedNits, bool inHdr, float reportedNits, bool defaultIsOurs, float edidNits);
// A profile name DesktopLUT made (every one of ours starts with "DesktopLUT_").
bool IsDesktopLutProfileName(const std::wstring& name);
// Pure (tested): after the one step's off removed our profile, whether the user's own previous default profile is made
// the default again — recorded, not ours, and its file still in the colour directory.
bool ShouldRestorePriorDefault(const std::wstring& prior, bool fileExists);

// The refresh-rate trim's switch (HDR, refresh_trim.h): on = the trim the display's DLC DIP holds for its live rate.
// Either way the DIP is read again (the switch doubles as the RELOAD of a new measurement).
Result MhcSetRefreshTrim(int mon, bool on);
void ReloadRefreshTrim();                                             // read the DIP again, then resolve every display

// ---- Display name ----------------------------------------------------------------------------------
// The user's name for a display, live or remembered. Found by storage slot (live or parked); a live
// display not yet given a slot is found by its live index. Empty = back to the automatic name.
Result SetDisplayLabel(int slot, int liveIndex, const std::wstring& label);

// ---- Settings ------------------------------------------------------------------------------------
void SetHotkeyEnabled(int hotkeyId, bool on);      // HOTKEY_GAMMA / HOTKEY_HDR_TOGGLE / HOTKEY_ANALYSIS

// ---- Global hotkeys (the hub window's, GUI thread) ----------------------------------------------------
// Registered once at startup and kept until exit, each while its Settings switch is on: they work whether or not the
// live layers run (the HDR switch needs nothing; analysis starts the engine it needs: SetAnalysis). A combination
// another program holds cannot be registered: a warning says which, and the hotkey stays unregistered.
void RegisterHotkeys();
void UnregisterHotkeys();
void ApplyHotkeyRegistration(int hotkeyId, bool on);   // WM_HOTKEY_REGISTER (SetHotkeyEnabled posts it)
void OnHotkey(int hotkeyId);                            // WM_HOTKEY

// ---- Analysis overlay (the readout of what is on the primary display) ---------------------------------------
// On brings up what it measures through (the live layers start for it if nothing else runs them: the analysis-only
// thread in hook mode, the overlay on display 0 otherwise); off stops whatever ran only for it. Refused while the
// live layers are suspended. The header's ANALYSIS switch and the hotkey.
Result SetAnalysis(bool on);
bool AnalysisOn();

void SetStartMinimized(bool on);
void SetRunAtStartup(bool on);
void SetConsoleLog(bool on);
void SetVrrWhitelistEnabled(bool on);
// The whitelists, as a list of process names (any separator; normalised to "a.exe, b.exe"). Applied and saved.
void SetGammaWhitelist(const std::wstring& raw);
void SetVrrWhitelist(const std::wstring& raw);
// The DLC folder ([UI] DlcFolder, authoritative). Empty = found automatically (dlc::FolderCandidates); clearing it
// also forgets the folder last found ([UI] DlcFolderLast), so CLEAR goes back to what is next to DesktopLUT.
void SetDlcFolder(const std::wstring& folder);
void SetDlcFolderLastUsed(const std::wstring& folder);   // a scan found DLC here (no folder configured)
void SetUiTheme(int theme);                     // 0 dark, 1 gray, 2 light (the interface applies it)
void SetUiDlcCharts(const std::wstring& encoded);   // the DLC tab's chart picks and options ([UI] DlcCharts)
void SetRecalReminderDays(int days);             // 0 = off

// ---- Live layers -----------------------------------------------------------------------------------
// The classic Start / Apply: resume (act::SetLiveLayersSuspended) and restart, which re-reads the cube
// files at their paths. Starting and stopping otherwise follow the configuration (actions/reconcile.h).
void ReloadLiveLayers();

}  // namespace act
