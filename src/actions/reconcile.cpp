#include "actions/reconcile.h"

#include "actions/notify.h"
#include "desktoplut_ipc_server.h"
#include "dwm_inject.h"
#include "globals.h"
#include "gui.h"
#include "processing.h"
#include <iostream>

namespace act {

ReconcileDecision DecideReconcile(const ReconcileInputs& in) {
    if (in.shuttingDown) return ReconcileDecision::None;
    if (in.busy) return ReconcileDecision::Defer;
    if (in.suspended || !in.needed) return in.running ? ReconcileDecision::Stop : ReconcileDecision::None;
    if (!in.running) return ReconcileDecision::Start;
    return in.hookStale ? ReconcileDecision::Restart : ReconcileDecision::None;
}

namespace {

// Hook mode, running: is the resident hook the one this configuration wants? A display that gained hook
// work (tone mapping switched on, a FALD panel file) while the pipeline ran without it would otherwise
// wait for the next restart; one that lost all hook work would keep the DLL resident for nothing.
bool HookStale() {
    if (!g_dwmHookMode.load() || !g_gui.isRunning) return false;
    const std::vector<DwmHookMonitorLUT> desired = DesiredDwmHookStagingSet();
    if (desired.empty()) return IsDwmHookActive();
    return !DwmHookResidentWith(desired);
}

}  // namespace

ReconcileDecision ReconcileLiveLayers(const char* why) {
    if (!g_gui.hwndMain) return ReconcileDecision::None;
    ReconcileInputs in;
    in.shuttingDown = g_appShuttingDown.load();
    in.suspended = g_userStopped;
    in.busy = IsProcessingTransitionActive() || g_monitorSettingsPins > 0 || g_mhcEditDialogOpen.load() ||
              IsCalibrationOrLiveEditActive();
    in.running = g_gui.isRunning.load();
    in.needed = AnyMonitorNeedsProcessing();
    in.hookStale = !in.busy && in.running && in.needed && HookStale();
    const char* reason = why ? why : "";
    const ReconcileDecision decision = DecideReconcile(in);
    switch (decision) {
    case ReconcileDecision::Start:
        std::cout << "[Live layers] start (" << reason << ")" << std::endl;
        StartProcessing();
        break;
    case ReconcileDecision::Stop:
        std::cout << "[Live layers] stop (" << reason << ")" << std::endl;
        StopProcessing();
        break;
    case ReconcileDecision::Restart:
        std::cout << "[Live layers] restart: the DWM hook's display list changed (" << reason << ")" << std::endl;
        StopProcessing();
        StartProcessing();
        break;
    case ReconcileDecision::Defer:
        SetTimer(g_gui.hwndMain, RECONCILE_TIMER_ID, RECONCILE_RETRY_MS, nullptr);
        break;
    case ReconcileDecision::None:
        break;
    }
    return decision;
}

void SetLiveLayersSuspended(bool suspended) {
    if (g_userStopped == suspended) return;
    g_userStopped = suspended;
    notify::Post(notify::Sev::Info, -1, L"live", suspended ? L"Live layers suspended" : L"Live layers resumed");
    if (suspended) {
        KillTimer(g_gui.hwndMain, RECONCILE_TIMER_ID);
        StopProcessing();
    } else {
        ReconcileLiveLayers("resume");
    }
    GuiNotifySettingsChanged(-1);
}

bool LiveLayersSuspended() { return g_userStopped; }

}  // namespace act
