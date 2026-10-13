#include "actions/gs_live.h"

#include "actions/actions.h"
#include "actions/notify.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "mhc_preview.h"
#include "processing.h"
#include "settings.h"
#include <memory>
#include <mutex>

namespace act {

namespace {

struct GsEdit {
    bool active = false;
    int mon = -1;
    bool hdr = false;
    GrayscaleSettings before;              // restored on cancel
    bool startedForPreview = false;        // this edit started processing / the overlay: stop it after
    bool startedOverlayForPreview = false;
    MhcPreviewSession preview;             // the overlay applies the profile the bake installs
    std::unique_ptr<MonitorSettingsPin> pin;   // display re-attach waits for the edit
};
GsEdit g_edit;

MHCSettings& MhcOf(int mon, bool hdr) {
    MonitorSettings& ms = g_gui.monitorSettings[(size_t)mon];
    return hdr ? ms.hdrMHC : ms.sdrMHC;
}

// Undoes Begin's processing start (a failed Begin, or End).
void StopPreviewProcessing(const GsEdit& e) {
    if (e.startedForPreview) StopProcessing();
    if (e.startedOverlayForPreview) {
        g_mhcEditDialogOpen.store(false);   // what kept the hook-mode overlay awake (EnsureProcessingForPreview)
        DwmHookReevaluateOverlay();
    }
}

}  // namespace

bool BeginGsEdit(int mon, bool hdr) {
    if (g_edit.active || !ValidMonitor(mon)) return false;
    if (IsCalibrationOrLiveEditActive() || g_mhcEditDialogOpen.load() || MhcPreviewActive()) {
        notify::Post(notify::Sev::Warning, mon, L"mhc", L"Another edit or a calibration is in progress");
        return false;
    }
    GsEdit e;
    e.pin = std::make_unique<MonitorSettingsPin>();
    e.mon = mon;
    e.hdr = hdr;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MHCSettings& m = MhcOf(mon, hdr);
        GrayscaleSettings& gs = m.correctionGrayscale;
        if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
            gs.points.resize(gs.pointCount);
            if (hdr) gs.initLinearPQ(); else gs.initLinear();
        }
        e.before = gs;
        // Editing the curve switches it on: the bake leaves a switched-off curve out, so neither the preview nor DONE
        // would show the edit. CANCEL restores `before`, switch included.
        gs.enabled = true;
    }

    // The overlay (or, in hook mode, the hook) applies the profile while scanout runs an identity one.
    const bool livePreview = ReadyMhcPreviewDisplay(mon, hdr, e.startedForPreview, e.startedOverlayForPreview);
    std::wstring why = L"The display is not in this mode";
    if (!livePreview || !BeginMhcPreview(mon, hdr, e.preview, &why)) {
        StopPreviewProcessing(e);
        notify::Post(notify::Sev::Warning, mon, L"mhc", L"Live edit unavailable: " + why);
        return false;
    }
    // MHC profile monitoring, re-attach and the desktop gamma follow wait for the edit (see g_mhcEditDialogOpen).
    g_mhcEditDialogOpen.store(true);
    e.active = true;
    g_edit = std::move(e);
    return true;
}

void PreviewGsEdit(const GrayscaleSettings& gs) {
    if (!g_edit.active) return;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (ValidMonitor(g_edit.mon)) MhcOf(g_edit.mon, g_edit.hdr).correctionGrayscale = gs;
    }
    UpdateMhcPreview(g_edit.preview, gs);
}

void EndGsEdit(bool keep) {
    if (!g_edit.active) return;
    const int mon = g_edit.mon;
    const bool hdr = g_edit.hdr;
    if (!keep) {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (ValidMonitor(mon)) MhcOf(mon, hdr).correctionGrayscale = g_edit.before;
    }
    // The overlay stops applying the profile, the bake puts the real one (with the kept or restored curve) on
    // scanout, and only then is the identity profile dropped: never a frame with nothing associated.
    StopMhcPreviewDraw(g_edit.preview);
    g_mhcEditDialogOpen.store(false);
    if (RegenerateMhcIfActive(mon, hdr) == MhcRegenResult::Failed)
        notify::Post(notify::Sev::Warning, mon, L"mhc", L"MHC profile not updated (source file unreadable?)");
    EndMhcPreview(g_edit.preview);
    UpdateMhcFlagsLive(mon);
    StopPreviewProcessing(g_edit);
    SaveSettings();
    g_edit = GsEdit{};   // releases the pin: a deferred display re-attach may run now
    // Desktop gamma waited for the live edit: catch up with the SDR white level (debounced re-check).
    if (g_gui.hwndMain) SetTimer(g_gui.hwndMain, SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS, nullptr);
    GuiNotifySettingsChanged(mon);
}

bool GsEditActive() { return g_edit.active; }
int GsEditMonitor() { return g_edit.active ? g_edit.mon : -1; }
bool GsEditHdr() { return g_edit.hdr; }
bool GsEditPreviewing() { return g_edit.active && MhcPreviewApplied(g_edit.preview); }

}  // namespace act
