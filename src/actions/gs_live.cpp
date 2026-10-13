#include "actions/gs_live.h"

#include "actions/actions.h"
#include "actions/notify.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "processing.h"
#include "settings.h"
#include <algorithm>
#include <memory>
#include <mutex>
#include <vector>

namespace act {

namespace {

struct GsEdit {
    bool active = false;
    int mon = -1;
    bool hdr = false;
    GrayscaleSettings before;              // restored on cancel
    bool livePreview = false;              // the overlay previews (the display is in the edited mode)
    bool startedForPreview = false;        // this edit started processing / the overlay: stop it after
    bool startedOverlayForPreview = false;
    std::wstring passthrough;              // SDR full preview: the transient identity scanout profile
    std::unique_ptr<MonitorSettingsPin> pin;   // display re-attach waits for the edit
};
GsEdit g_edit;

MHCSettings& MhcOf(int mon, bool hdr) {
    MonitorSettings& ms = g_gui.monitorSettings[(size_t)mon];
    return hdr ? ms.hdrMHC : ms.sdrMHC;
}

// The running context's preview flags for one display.
void SetPreviewFlags(int mon, bool on, bool full, const float* result9, std::vector<float>* baseLut) {
    std::lock_guard<std::mutex> lk(g_monitorsMutex);
    for (auto& ctx : g_monitors) {
        if (ctx.index != mon) continue;
        ctx.corrGsPreviewActive = on;
        if (on && full && result9 && baseLut) {
            memcpy(ctx.previewResult, result9, sizeof(float) * 9);
            for (int ch = 0; ch < 3; ch++) ctx.previewBaseLut[ch] = std::move(baseLut[ch]);
            ctx.previewBaseLutSize = 1024;
            ctx.previewBaseLutDirty = true;
        }
        ctx.corrGsFullPreviewActive = on && full;
        ctx.cbDirty = true;
        break;
    }
}

}  // namespace

bool BeginGsEdit(int mon, bool hdr) {
    if (g_edit.active || !ValidMonitor(mon)) return false;
    if (IsCalibrationOrLiveEditActive() || g_mhcEditDialogOpen.load()) {
        notify::Post(notify::Sev::Warning, mon, L"mhc", L"Another edit or a calibration is in progress");
        return false;
    }
    g_edit = GsEdit{};
    g_edit.pin = std::make_unique<MonitorSettingsPin>();
    g_edit.mon = mon;
    g_edit.hdr = hdr;
    bool hadProfile;
    uint8_t savedPerm;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        MHCSettings& m = MhcOf(mon, hdr);
        GrayscaleSettings& gs = m.correctionGrayscale;
        if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
            gs.points.resize(gs.pointCount);
            if (hdr) gs.initLinearPQ(); else gs.initLinear();
        }
        g_edit.before = gs;
        hadProfile = m.enabled && !m.profileName.empty();
        savedPerm = m.activePerm;
    }

    EnsureProcessingForPreview(mon, hdr, g_edit.livePreview, g_edit.startedForPreview, g_edit.startedOverlayForPreview);

    // The profile stays on screen without the correction (the overlay draws it on top). SDR: identity
    // scanout and the overlay reproduces the whole profile, so the preview is bit-identical to the bake.
    if (g_edit.livePreview && hadProfile) {
        const uint8_t previewPerm = (uint8_t)(savedPerm & ~MHCSettings::PERM_GS);
        float result9[9] = { 1, 0, 0, 0, 1, 0, 0, 0, 1 };
        std::vector<float> baseLut[3];
        bool full = false;
        if (!hdr && ComputeSdrPreviewScanout(mon, previewPerm, result9, baseLut[0], baseLut[1], baseLut[2])) {
            g_edit.passthrough = EngageSdrPassthroughScanout(mon);
            full = !g_edit.passthrough.empty();
        }
        if (!full && previewPerm != savedPerm) SwapMhcToPermutation(mon, hdr, previewPerm);
        SetPreviewFlags(mon, true, full, result9, baseLut);
    }
    if (!g_edit.startedOverlayForPreview) g_mhcEditDialogOpen.store(true);
    g_edit.active = true;
    PreviewGsEdit(g_edit.before);
    return true;
}

void PreviewGsEdit(const GrayscaleSettings& gs) {
    if (!g_edit.active) return;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (ValidMonitor(g_edit.mon)) MhcOf(g_edit.mon, g_edit.hdr).correctionGrayscale = gs;
    }
    if (!g_edit.livePreview) return;   // the display is in the other mode: nothing to show
    ColorCorrectionSettings tempCC;
    tempCC.grayscale = gs;
    const ColorCorrectionData data = ConvertColorCorrection(tempCC, g_edit.hdr);
    const int mon = g_edit.mon;
    std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
    g_pendingColorCorrections.erase(
        std::remove_if(g_pendingColorCorrections.begin(), g_pendingColorCorrections.end(),
                       [mon](const PendingColorCorrection& p) { return p.monitorIndex == mon; }),
        g_pendingColorCorrections.end());
    g_pendingColorCorrections.push_back({ mon, g_edit.hdr, data, false, false });
    g_hasPendingColorCorrections.store(true, std::memory_order_release);
    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
}

void EndGsEdit(bool keep) {
    if (!g_edit.active) return;
    const int mon = g_edit.mon;
    const bool hdr = g_edit.hdr;
    g_mhcEditDialogOpen.store(false);
    if (!keep) {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (ValidMonitor(mon)) MhcOf(mon, hdr).correctionGrayscale = g_edit.before;
    }
    SetPreviewFlags(mon, false, false, nullptr, nullptr);
    // The profile is rebuilt from the settings either way: the preview swapped the correction out of it.
    if (RegenerateMhcIfActive(mon, hdr) == MhcRegenResult::Failed)
        notify::Post(notify::Sev::Warning, mon, L"mhc", L"MHC profile not updated (source file unreadable?)");
    if (!g_edit.passthrough.empty()) DisengageSdrPassthroughScanout(mon, g_edit.passthrough);
    if (g_edit.livePreview) {
        UpdateMhcFlagsLive(mon);
        if (!g_edit.startedForPreview) UpdateColorCorrectionLive(mon, hdr);
    }
    if (g_edit.startedForPreview) StopProcessing();
    if (g_edit.startedOverlayForPreview) DwmHookReevaluateOverlay();
    SaveSettings();
    g_edit = GsEdit{};   // releases the pin: a deferred display re-attach may run now
    GuiNotifySettingsChanged(mon);
}

bool GsEditActive() { return g_edit.active; }
int GsEditMonitor() { return g_edit.active ? g_edit.mon : -1; }
bool GsEditHdr() { return g_edit.hdr; }
bool GsEditPreviewing() { return g_edit.active && g_edit.livePreview; }

}  // namespace act
