// DesktopLUT - processing.h
// Processing thread management

#pragma once

#include "types.h"
#include <vector>

// Monitor enumeration callback
BOOL CALLBACK MonitorEnumProc(HMONITOR hMonitor, HDC, LPRECT, LPARAM lParam);

// Processing thread function
void ProcessingThreadFunc(std::vector<MonitorLUTConfig> configs, unsigned renderGen);

// Start processing (GUI mode). Requested from inside a running Stop (a pumped message), it is
// deferred until that Stop completes.
void StartProcessing();

// Stop processing (GUI mode). Re-entrant calls from inside a running Stop are absorbed; from
// inside a running Start, deferred until it completes. Also joins a thread that exited on its
// own while not running.
void StopProcessing();

// The auto-restart after the overlay thread died (RESTART_TIMER_ID): like StartProcessing, but a DWM hook
// still resident with exactly the staging set it would inject is kept (DwmHookResidentWith) instead of
// being ejected and re-injected on every retry. Every other start re-injects.
void StartProcessingAfterOverlayLoss();

// One display needs the processing pipeline (a LUT, a shader correction, tonemap, the FALD layer — in hook
// mode only with that mode's panel file — or desktop gamma) — StartProcessing's per-monitor filter.
bool MonitorNeedsProcessing(const MonitorSettings& ms);
// Any LIVE display needs the processing pipeline.
bool AnyMonitorNeedsProcessing();
// Any LIVE display has any correction at all, MHC profiles included (the startup auto-start test).
bool AnyMonitorHasCorrections();
// Desktop gamma is configured on some live display (its persisted setting, not the hotkey's toggle).
bool AnyDesktopGammaConfigured();
// The live layers are needed: a live display needs processing, or desktop gamma is configured (by its persisted
// setting: its hotkey toggles only the in-memory flag, and the running pipeline hosts that hotkey — stopping on
// the toggle would unregister the very hotkey that turns it back on). The reconcile's `needed`. A start builds
// a non-empty pipeline exactly then (BuildPipelineConfigs), so a restart does not end stopped for want of work
// while this holds (it still can on a failed hook injection or a start deferred behind a stuck thread). GUI thread.
bool LiveLayersNeeded();
// The per-display configs a start builds: every display that needs processing, and every display while desktop
// gamma is configured anywhere (as with the mode on, so the hotkey turning it back on finds them all). Empty
// exactly when !LiveLayersNeeded() (an MHC-only setup). GUI thread. Exposed for tests (StartProcessing uses it).
std::vector<MonitorLUTConfig> BuildPipelineConfigs();
// Hook mode: no hook is resident because no display had hook work the last time one was due — the running start
// staged nothing, or the watchdog found the hook gone with nothing to re-inject and stood down. Any hook work
// then makes the resident hook stale (act::HookStale), which re-injects. A start with hook work, or a watchdog
// re-injection attempt (its give-ups stand), clears it. GUI thread.
bool DwmHookIdle();
void SetDwmHookIdle(bool idle);
// Stop + Start that leaves the live layers stopped when the user suspended them meanwhile (the Stop's
// join pumps messages: a Suspend can arrive inside it). GUI thread.
void RestartLiveLayers();

// A processing thread a Stop gave up joining is still running. While it is, StartProcessing defers
// (no successor may share the globals it still uses) and the overlay / analysis-only starts skip.
bool AbandonedProcessingThreadAlive();
// A StartProcessing was deferred for that reason and the thread has now exited: true once (consumes it).
bool TakeDeferredStartReady();
// Hook mode: the overlay thread hung (GUI-side render-health check). Join and restart only that thread;
// the hook keeps running. If the join times out the overlay restarts once the thread has exited.
void RestartHungOverlayThread();

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

struct DwmHookMonitorLUT;
// The staging set a hook-mode start injects for the current settings and display positions: the live
// displays with hook work (MonitorNeedsDwmHook; not gated on the global desktop-gamma flag, which would
// add and remove displays on every toggle). StartProcessing, the watchdog's re-injection and the
// live-layer reconcile (is the resident hook still the one this configuration wants?) all use it, so
// they cannot disagree. GUI thread.
std::vector<DwmHookMonitorLUT> DesiredDwmHookStagingSet();

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
// Any overlay work at all (analysis included): the render thread's last verdict while it runs, else the
// GUI-state check a start uses to pick the overlay / analysis-only thread. Hook-mode tonemap and FALD are
// the hook's, not overlay work. Exposed for tests.
bool HasActiveShaderCorrections();

// Check if current settings differ from active (running) settings
bool SettingsChanged();

// Convert GUI ColorCorrectionSettings to runtime ColorCorrectionData
// isHDR: affects primaries matrix direction
//   SDR: sRGB → measured (gamut mapping for uncalibrated displays)
//   HDR: Rec.2020 → measured (correction in Rec.2020 space after BT.709→Rec.2020)
ColorCorrectionData ConvertColorCorrection(const ColorCorrectionSettings& src, bool isHDR);

