#pragma once
// Live layers follow the configuration (3.0.0: no Start / Stop). The processing thread and the DWM hook
// run while some display needs them (a 3D LUT, tone mapping, the FALD layer, the shader tweak, desktop
// gamma) and stop when none does, unless the user suspended them. The MHC profiles are not live
// layers: they stay at scanout either way.

namespace act {

enum class ReconcileDecision { None, Start, Stop, Restart, Defer };

struct ReconcileInputs {
    bool shuttingDown = false;
    bool suspended = false;        // the user's Suspend (g_userStopped)
    bool busy = false;             // a start/stop transition, a pinned settings vector, an MHC edit dialog,
                                   // a calibration session or live edit: nothing may start or stop now
    bool running = false;          // g_gui.isRunning
    bool needed = false;           // AnyMonitorNeedsProcessing()
    bool hookStale = false;        // hook mode: the resident hook was injected with another staging set
                                   // than the configuration wants (a display gained or lost hook work
                                   // while running; the DLL reads its display list only at attach)
};

// Pure (tested): what reconciling should do in this state.
ReconcileDecision DecideReconcile(const ReconcileInputs& in);

// GUI thread. Starts, stops or restarts the live layers to match the configuration; when it must wait
// (busy), it retries on RECONCILE_TIMER_ID. `why` goes to the log. Returns what it did.
ReconcileDecision ReconcileLiveLayers(const char* why);

// GUI thread. Suspend stops the live layers and keeps them stopped (display changes and wakes do not
// start them); resume reconciles. Suspending is refused while a calibration session or live edit is
// open (a DLC run owns the pipeline until calibration.exit).
void SetLiveLayersSuspended(bool suspended);
bool LiveLayersSuspended();

// GUI thread. A calibration-pipe verb is about to start the live layers (DLC applies what it
// configures): a Suspend gives way, announced in the event log, instead of being silently overridden.
void ResumeLiveLayersForPipe();

}  // namespace act
