#pragma once
// Live layers follow the configuration (3.0.0: no Start / Stop). The processing thread and the DWM hook
// run while some display needs them (a 3D LUT, tone mapping, the FALD layer, the shader tweak, desktop
// gamma) and stop when none does, unless the user suspended them. The MHC profiles are not live
// layers: they stay at scanout either way.

#include "dwm_inject.h"
#include <vector>

namespace act {

enum class ReconcileDecision { None, Start, Stop, Restart, Defer };

struct ReconcileInputs {
    bool shuttingDown = false;
    bool suspended = false;        // the user's Suspend (g_userStopped)
    bool busy = false;             // LiveLayersBusy(): nothing may start or stop now
    bool running = false;          // g_gui.isRunning
    bool needed = false;           // LiveLayersNeeded()
    bool hookStale = false;        // HookStale(): the resident DWM hook must be re-injected
};

// Pure (tested): what reconciling should do in this state.
ReconcileDecision DecideReconcile(const ReconcileInputs& in);

// The DWM hook reads its staged files (DesiredDwmHookStagingSet: the cubes and FALD panel files) only at
// injection and matches them to displays by desktop position. Everything else reaches a resident hook live
// through the shared config, which lists every display by position: the tone mapper and the FALD switches.
struct DisplayPosition { int left = 0, top = 0; };
struct HookStaging {
    std::vector<DwmHookMonitorLUT> desired;    // DesiredDwmHookStagingSet()
    std::vector<DwmHookMonitorLUT> injected;   // DwmHookInjectedSet(): what the resident hook was injected with
    bool injectedKnown = false;                // ...and whether that set describes the resident hook
    bool hookActive = false;                   // IsDwmHookActive()
    bool hookIdle = false;                     // DwmHookIdle(): none resident because no display had hook work
                                               // when one was last due (the start, or the watchdog's re-inject)
    bool restartInjects = false;               // LiveLayersNeeded(): a restart starts again and injects `desired`
                                               // (otherwise it only ejects the hook and stays stopped)
    std::vector<DisplayPosition> identified;   // live displays whose settings carry a display identity
};

// Pure (tested): hook mode, running — would a re-inject change what the hook applies to a live display?
// Stale when:
//   - a cube staged at a display's position is not the one that display wants (a BenQ with no cube swapped
//     onto the TV's connector would show the TV's cube): even when the restart injects nothing again;
//   - a restart injects, and a display stages a file the hook lacks at its position: a cube, or a panel file
//     whatever its switch (so that switching the layer on rides the shared config) — it gained one, arrived
//     late, or the attachment change moved it;
//   - a restart injects, no hook is resident because none had work when it was last due (the running start
//     staged nothing, or the watchdog found the hook gone while the TV — the only display with hook work —
//     was off), and now some display has hook work: tone mapping too, it needs the hook resident.
// Not stale:
//   - tone mapping on a display the hook was not injected with: a resident hook tone maps every display;
//   - a display that only lost live-switch work (tone mapping off, the FALD layer off);
//   - a panel file under a display that does not want it: inert, the layer runs only where that display's
//     own settings switch it on, with its own file (which then is a file the hook lacks);
//   - a display that left (the owner's TV powering off): nothing sits under its files, and they are right
//     again when it comes back at the same position;
//   - an unidentified display over files staged for another: it may be the very display they are for (the
//     identity query lags), so it waits for its identity, as the settings re-attach does;
//   - a hook that is gone while some display had work for it (DWM restarted: the watchdog re-injects; or the
//     watchdog gave up, flapping or failing: that stands);
//   - a resident hook whose staging set is unknown.
bool DecideHookStale(const HookStaging& s);

// GUI thread. DecideHookStale for the live state (false outside hook mode or when not running).
bool HookStale();

// GUI thread. A start/stop transition, a pinned settings vector, an MHC edit dialog, a calibration session
// or live edit: nothing may start, stop or re-inject the live layers now.
bool LiveLayersBusy();

// GUI thread. Starts, stops or restarts the live layers to match the configuration; when it must wait
// (busy), it retries on RECONCILE_TIMER_ID. `why` goes to the log. Returns what it did.
ReconcileDecision ReconcileLiveLayers(const char* why);

// TOPOLOGY_PIPELINE_TIMER_ID: the live settings attachment changed (a display arrived, left, was swapped, or
// was identified late), or, in hook mode while running, any modeset (a display may have moved).
struct TopologyInputs {
    bool busy = false;             // LiveLayersBusy()
    bool suspended = false;        // the user's Suspend (g_userStopped)
    bool running = false;          // g_gui.isRunning
    bool needed = false;           // AnyMonitorNeedsProcessing() (the tick's own start rule, kept as it was)
    bool lutsChanged = false;      // SettingsChanged(): a live display's cube paths differ from the running ones
    bool hookStale = false;        // HookStale()
    bool fullscreenExclusive = false;   // a D3D exclusive-fullscreen app runs (SHQueryUserNotificationState)
};

// Pure (tested): what the topology tick does. Busy: wait (Defer = re-arm the tick). Not running: Start when
// a live display needs processing and the user has not suspended. Running: Restart when the pipeline was
// built without what the attached displays now need (lutsChanged, hookStale) — but not while an
// exclusive-fullscreen game runs: its resolution change moves the displays beside it, a restart would
// re-inject and re-apply the MHC profiles under the game, and when it exits the positions usually come
// back to what the hook has (nothing to do then). Never Stop: a display that left (the owner's TV powering
// off) does not stop what runs for it, so its return costs no re-inject; a configuration change still
// stops what nothing needs (ReconcileLiveLayers).
ReconcileDecision DecideTopologyTick(const TopologyInputs& in);

// The DWM hook watchdog's tick (DWM_HOOK_WATCHDOG_TIMER_ID, hook mode, running).
enum class WatchdogAction {
    Healthy,     // the hook is resident
    GiveUp,      // it keeps dropping after re-injections (the flap cap): stop the re-inject flash loop, for good
    StandDown,   // gone, and no display has hook work: idle (SetDwmHookIdle), not a give-up — HookStale
                 // re-injects once a display has work again (the TV returns, a layer is switched on)
    Reinject,    // gone (DWM restarted?): re-inject the staging set
};

// Pure (tested). `reinjects` = the watchdog's re-injection cycles so far, `maxReinjects` its cap.
WatchdogAction DecideHookWatchdog(bool hookActive, int reinjects, int maxReinjects, bool nothingToInject);

// GUI thread. Suspend stops the live layers and keeps them stopped (display changes and wakes do not
// start them); resume reconciles. Suspending is refused while a calibration session or live edit is
// open (a DLC run owns the pipeline until calibration.exit).
void SetLiveLayersSuspended(bool suspended);
bool LiveLayersSuspended();

// GUI thread. A calibration-pipe verb is about to start the live layers (DLC applies what it
// configures): a Suspend gives way, announced in the event log, instead of being silently overridden.
void ResumeLiveLayersForPipe();

}  // namespace act
