#include "actions/reconcile.h"

#include "actions/notify.h"
#include "desktoplut_ipc_server.h"
#include "dwm_inject.h"
#include "globals.h"
#include "gui.h"
#include "processing.h"
#include <algorithm>
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

// Hook mode, running: does the configuration want hook work the resident hook was not injected with? A
// display that gained hook work (tone mapping switched on, a FALD panel file) while the pipeline ran
// without it would otherwise wait for the next restart. Not stale:
//   - a display that only LOST hook work: the live switches reach the DLL through the shared config, and
//     a re-inject would flash for nothing;
//   - a hook that is gone (DWM restarted, or the watchdog gave up re-injecting): that is the watchdog's
//     business, and a restart here would undo its give-up.
bool HookStale() {
    if (!g_dwmHookMode.load() || !g_gui.isRunning) return false;
    const std::vector<DwmHookMonitorLUT> desired = DesiredDwmHookStagingSet();
    if (desired.empty()) return false;
    if (!IsDwmHookActive()) return DwmHookStartStagedNothing();   // never injected: now there is hook work
    bool valid = false;
    const std::vector<DwmHookMonitorLUT> injected = DwmHookInjectedSet(&valid);
    if (!valid) return false;
    for (const DwmHookMonitorLUT& want : desired)
        if (std::find(injected.begin(), injected.end(), want) == injected.end()) return true;
    return false;
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
    // Desktop gamma counts by its persisted setting: its hotkey toggles the in-memory flag, and stopping
    // on that toggle would unregister the very hotkey that turns it back on.
    in.needed = AnyMonitorNeedsProcessing() || AnyDesktopGammaConfigured();
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
        std::cout << "[Live layers] restart: a display gained DWM hook work (" << reason << ")" << std::endl;
        RestartLiveLayers();
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
    if (g_userStopped == suspended) {
        // Already suspended, yet something started the pipeline (a routing swap, a preview): Stop still stops.
        if (suspended && g_gui.isRunning && !IsCalibrationOrLiveEditActive()) StopProcessing();
        return;
    }
    if (suspended && IsCalibrationOrLiveEditActive()) {
        notify::Post(notify::Sev::Warning, -1, L"live", L"Calibration in progress: live layers stay on");
        GuiNotifySettingsChanged(-1);   // the classic control shows the state it keeps
        return;
    }
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

void ResumeLiveLayersForPipe() {
    if (!g_userStopped) return;
    g_userStopped = false;
    notify::Post(notify::Sev::Info, -1, L"live", L"Live layers resumed by DLC");
    GuiNotifySettingsChanged(-1);
}

}  // namespace act
