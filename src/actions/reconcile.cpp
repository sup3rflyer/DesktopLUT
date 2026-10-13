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

ReconcileDecision DecideTopologyTick(const TopologyInputs& in) {
    if (in.busy) return ReconcileDecision::Defer;
    if (!in.running) return (!in.suspended && in.needed) ? ReconcileDecision::Start : ReconcileDecision::None;
    return (in.lutsChanged || in.hookStale) ? ReconcileDecision::Restart : ReconcileDecision::None;
}

namespace {

const DwmHookMonitorLUT* EntryAt(const std::vector<DwmHookMonitorLUT>& set, int left, int top) {
    for (const DwmHookMonitorLUT& e : set)
        if (e.left == left && e.top == top) return &e;
    return nullptr;
}

// One live position: what the configuration stages there (`want`) against what the hook staged (`had`).
bool PositionStale(const DwmHookMonitorLUT& want, const DwmHookMonitorLUT& had, bool restartInjects) {
    // A staged cube applies to whatever display sits there: one it does not want must go, even if the
    // restart injects nothing again.
    if ((!had.sdrLutPath.empty() && had.sdrLutPath != want.sdrLutPath) ||
        (!had.hdrLutPath.empty() && had.hdrLutPath != want.hdrLutPath))
        return true;
    if (!restartInjects) return false;
    // A file the hook lacks there. A panel file it has that is not wanted is inert (the layer's switch needs
    // the display's own file); tone mapping stages no file.
    auto lacks = [](const std::wstring& w, const std::wstring& h) { return !w.empty() && w != h; };
    return lacks(want.sdrLutPath, had.sdrLutPath) || lacks(want.hdrLutPath, had.hdrLutPath) ||
           lacks(want.sdrFaldPath, had.sdrFaldPath) || lacks(want.hdrFaldPath, had.hdrFaldPath);
}

}  // namespace

bool DecideHookStale(const HookStaging& s) {
    // Never injected (no display had hook work at the start): any hook work now needs the hook resident.
    if (!s.hookActive) return s.restartInjects && s.startStagedNothing && !s.desired.empty();
    if (!s.injectedKnown) return false;
    const DwmHookMonitorLUT none;
    for (const DwmHookMonitorLUT& want : s.desired) {   // the displays with hook work (live)
        const DwmHookMonitorLUT* had = EntryAt(s.injected, want.left, want.top);
        if (PositionStale(want, had ? *had : none, s.restartInjects)) return true;
    }
    for (const DisplayPosition& p : s.identified) {     // the others: files staged for a display gone from there
        if (EntryAt(s.desired, p.left, p.top)) continue;
        const DwmHookMonitorLUT* had = EntryAt(s.injected, p.left, p.top);
        if (had && PositionStale(none, *had, s.restartInjects)) return true;
    }
    return false;   // entries at positions no live display has: a display that left (they are right again
                    // if it comes back there)
}

bool HookStale() {
    if (!g_dwmHookMode.load() || !g_gui.isRunning) return false;
    HookStaging s;
    s.desired = DesiredDwmHookStagingSet();
    s.hookActive = IsDwmHookActive();
    s.startStagedNothing = DwmHookStartStagedNothing();
    s.restartInjects = AnyMonitorNeedsProcessing();
    s.injected = DwmHookInjectedSet(&s.injectedKnown);
    for (size_t i = 0; i < g_gui.monitors.size() && i < g_gui.monitorSettings.size(); i++) {
        if (g_gui.monitorSettings[i].identity.empty()) continue;   // unknown: may be the display the files are for
        MONITORINFO mi = { sizeof(mi) };
        if (GetMonitorInfo(g_gui.monitors[i], &mi)) s.identified.push_back({ mi.rcMonitor.left, mi.rcMonitor.top });
    }
    return DecideHookStale(s);
}

bool LiveLayersBusy() {
    return IsProcessingTransitionActive() || g_monitorSettingsPins > 0 || g_mhcEditDialogOpen.load() ||
           IsCalibrationOrLiveEditActive();
}

ReconcileDecision ReconcileLiveLayers(const char* why) {
    if (!g_gui.hwndMain) return ReconcileDecision::None;
    ReconcileInputs in;
    in.shuttingDown = g_appShuttingDown.load();
    in.suspended = g_userStopped;
    in.busy = LiveLayersBusy();
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
        std::cout << "[Live layers] restart: the DWM hook's staged files do not match the displays (" << reason << ")"
                  << std::endl;
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
