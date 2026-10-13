// DesktopLUT - mhc_preview.cpp
// The live edit preview of a display's MHC profile, exact to the bake (see mhc_preview.h).

#include "mhc_preview.h"

#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include <cstring>
#include <iostream>

std::mutex g_mhcPreviewStageMutex;
MhcPreviewStage g_mhcPreviewStage;
std::atomic<unsigned> g_mhcPreviewStageGen{ 0 };

namespace {

std::atomic<bool> g_previewActive{ false };   // one preview at a time (the overlay has one LUT texture for it)

MHCSettings& MhcOf(int mon, bool hdr) {
    MonitorSettings& ms = g_gui.monitorSettings[(size_t)mon];
    return hdr ? ms.hdrMHC : ms.sdrMHC;
}

// Hands the overlay what to apply (nullptr = nothing) and wakes it: a sleeping overlay starts, and a static desktop
// is redrawn with the change (render.cpp SyncMhcPreview).
void PublishStage(int mon, bool hdr, const MhcScanout* scan) {
    {
        std::lock_guard<std::mutex> lk(g_mhcPreviewStageMutex);
        MhcPreviewStage& st = g_mhcPreviewStage;
        if (scan) {
            st.mon = mon;
            st.hdr = hdr;
            memcpy(st.matrix, scan->applied, sizeof(st.matrix));
            st.lutSize = scan->lutSize;
            for (int ch = 0; ch < 3; ch++) st.lut[ch] = scan->lut[ch];
        } else {
            st.mon = -1;
            st.lutSize = 0;
        }
    }
    g_mhcPreviewStageGen.fetch_add(1, std::memory_order_acq_rel);
    if (g_overlayWakeEvent) SetEvent(g_overlayWakeEvent);
}

}  // namespace

bool BeginMhcPreview(int mon, bool hdr, MhcPreviewSession& s, std::wstring* why) {
    auto fail = [why](const wchar_t* text) {
        if (why) *why = text;
        return false;
    };
    if (s.active || g_previewActive) return fail(L"Another live edit is in progress");
    MHCSettings mhc;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (mon < 0 || mon >= (int)g_gui.monitorSettings.size()) return fail(L"No such display");
        mhc = MhcOf(mon, hdr);
    }
    // The bake (RegenerateMhcIfActive) installs a profile only where one is named: without one there is nothing
    // the edit would change on screen, so nothing to preview either.
    if (mhc.profileName.empty()) return fail(L"No MHC profile installed for this mode");
    if (!IsMHC2ApiAvailable()) return fail(L"MHC profiles need Windows 10 21H2 or later");

    MhcPreviewSession n;
    if (!PrepareMhcPreview(mhc, hdr, mon, n)) return fail(L"The profile's source file cannot be read");

    n.passthrough = EngagePassthroughScanout(mon, hdr, mhc);
    if (n.passthrough.empty()) return fail(L"Scanout could not be switched for the preview");
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (mon < (int)g_gui.monitorSettings.size()) MhcOf(mon, hdr).previewScanoutName = n.passthrough;
    }
    n.active = true;
    g_previewActive = true;
    s = std::move(n);
    UpdateMhcPreview(s, mhc.correctionGrayscale);
    return true;
}

bool PrepareMhcPreview(const MHCSettings& mhc, bool hdr, int mon, MhcPreviewSession& s) {
    s = MhcPreviewSession{};
    s.mon = mon;
    s.hdr = hdr;
    // The variant the bake will install, without its correction (composed live). Built from the settings, like the
    // bake: a desktop gamma variant the whitelist or the hotkey has swapped in meanwhile is not what it installs.
    s.perm = (uint8_t)(ComputeMhcPermutation(mhc, hdr) & ~MHCSettings::PERM_GS);
    return BuildMHC2ParamsForPerm(mhc, hdr, mon, s.perm, s.params) && ComputeMhcScanoutBase(s.params, s.base);
}

void ComposeMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction, MhcScanout& out) {
    // Exactly the bake's decision: BuildMHC2ParamsForPerm keeps the correction when the permutation has PERM_GS,
    // which ComputeMhcPermutation sets for an enabled, non-empty curve and NormalizeMhcPerm drops under
    // PERM_NOGRAY (the grayscale stage switched off).
    MHC2ProfileParams p = s.params;
    const bool withCorrection = !(s.perm & MHCSettings::PERM_NOGRAY) && correction.enabled && !correction.points.empty();
    p.correctionGrayscaleEnabled = false;
    p.correctionGrayscale.enabled = false;
    if (withCorrection) SetMhcCorrectionParams(correction, p);
    ComposeMhcScanout(p, s.base, out);
}

void UpdateMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction) {
    if (!s.active) return;
    MhcScanout scan;
    ComposeMhcPreview(s, correction, scan);
    PublishStage(s.mon, s.hdr, &scan);
}

void StopMhcPreviewOverlay(const MhcPreviewSession& s) {
    if (!s.active) return;
    PublishStage(-1, false, nullptr);
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    if (s.mon >= 0 && s.mon < (int)g_gui.monitorSettings.size()) MhcOf(s.mon, s.hdr).previewScanoutName.clear();
}

void EndMhcPreview(MhcPreviewSession& s) {
    if (!s.active) return;
    StopMhcPreviewOverlay(s);   // no-op when the caller already stopped it
    DisengagePassthroughScanout(s.mon, s.hdr, s.passthrough);
    g_previewActive = false;
    s = MhcPreviewSession{};
}

bool MhcPreviewActive() { return g_previewActive; }
