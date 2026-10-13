// DesktopLUT - mhc_preview.cpp
// The live edit preview of a display's MHC profile, exact to the bake (see mhc_preview.h).

#include "mhc_preview.h"

#include "displayconfig.h"   // QueryFreshOutputDesc
#include "dwm_inject.h"
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "mhc.h"            // QueryDisplayDefaultProfile, ReassociateMHC2Profile
#include "processing.h"
#include <cstring>
#include <iostream>

std::mutex g_mhcPreviewStageMutex;
MhcPreviewStage g_mhcPreviewStage;
std::atomic<unsigned> g_mhcPreviewStageGen{ 0 };

namespace {

std::atomic<bool> g_previewActive{ false };   // one preview at a time (one LUT texture in the overlay, one block in the hook)
std::atomic<bool> g_previewOnHook{ false };   // ...and the hook applies it (the overlay is not needed for it)
std::atomic<int> g_previewMon{ -1 };          // the display it previews (-1 = none)
std::atomic<bool> g_previewHdr{ false };      // ...in this mode
uint32_t g_hookGen = 0;                       // the generation last handed to the hook (GUI thread)

constexpr DWORD kHookAckWaitMs = 2000;        // the DLL acknowledges at its next present (DWM is made to present)

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

// Pumps the GUI thread's messages (the recomposition that makes DWM present runs on its timer) until `done` or the
// deadline.
template <typename Done> bool PumpUntil(Done done, DWORD timeoutMs) {
    const ULONGLONG until = GetTickCount64() + timeoutMs;
    for (;;) {
        if (done()) return true;
        if (GetTickCount64() >= until) return false;
        MSG msg;
        while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessage(&msg);
        }
        MsgWaitForMultipleObjects(0, nullptr, FALSE, 10, QS_ALLINPUT);
    }
}

// The hook acknowledges the generation the config carries NOW (a re-injection re-publishes under a new one).
bool HookShowsCurrentGen() {
    const uint32_t gen = DwmHookMhcPreviewGen();
    return gen != 0 && DwmHookMhcPreviewAck() == gen;
}

// The display shows `hdr` mode now (fresh DXGI: cached state may predate a toggle).
bool DisplayShowsMode(int mon, bool hdr) {
    if (mon < 0 || mon >= (int)g_gui.monitors.size() || !g_gui.monitors[mon]) return false;
    DXGI_OUTPUT_DESC1 desc;
    if (!QueryFreshOutputDesc(g_gui.monitors[mon], desc)) return false;
    return (desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020) == hdr;
}

}  // namespace

bool MhcPreviewDisplayShowsMode(int mon, bool hdr) { return DisplayShowsMode(mon, hdr); }

bool MhcPreviewUsesHook() {
    return g_dwmHookMode.load() && g_gui.isRunning && IsDwmHookActive();
}

bool MhcEditorNeedsOverlay() {
    return g_mhcEditDialogOpen.load() && !g_previewOnHook.load();
}

bool ReadyMhcPreviewDisplay(int mon, bool hdr, bool& startedForPreview, bool& startedOverlayForPreview) {
    startedForPreview = startedOverlayForPreview = false;
    if (g_dwmHookMode.load() && !g_gui.isRunning) {
        // Hook mode with the live layers suspended: start them for the edit (stopped again after it). The injection
        // is synchronous; with no hook work for any display nothing is injected and the overlay path below applies.
        StartProcessing();
        startedForPreview = g_gui.isRunning;
    }
    if (MhcPreviewUsesHook()) return DisplayShowsMode(mon, hdr);
    bool live = false, started = false;
    EnsureProcessingForPreview(mon, hdr, live, started, startedOverlayForPreview);
    startedForPreview = startedForPreview || started;
    return live;
}

bool BeginMhcPreview(int mon, bool hdr, MhcPreviewSession& s, std::wstring* why, bool requireProfile,
                     const MHCSettings* work) {
    auto fail = [why](const wchar_t* text) {
        if (why) *why = text;
        return false;
    };
    if (s.active || g_previewActive) return fail(L"Another live edit is in progress");
    MHCSettings mhc;
    unsigned held = 0;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (mon < 0 || mon >= (int)g_gui.monitorSettings.size()) return fail(L"No such display");
        mhc = MhcOf(mon, hdr);
        const ColorCorrectionSettings& cc = hdr ? g_gui.monitorSettings[mon].hdrColorCorrection
                                                : g_gui.monitorSettings[mon].sdrColorCorrection;
        if (hdr && cc.tonemap.enabled) held |= kMhcHeldTonemap;
        if (cc.fald.enabled && !cc.fald.paramsPath.empty()) held |= kMhcHeldFald;
        if (hdr && mhc.desktopGammaEnabled) held |= kMhcHeldDesktopGamma;
        if (ComputeMhcPermutation(mhc, hdr) & MHCSettings::PERM_WB) held |= kMhcHeldWhiteBalance;   // a real move
    }
    // The bake (RegenerateMhcIfActive) installs a profile only where one is named: without one there is nothing
    // the edit would change on screen, so nothing to preview either.
    if (requireProfile && mhc.profileName.empty()) return fail(L"No MHC profile installed for this mode");
    if (!IsMHC2ApiAvailable()) return fail(L"MHC profiles need Windows 10 21H2 or later");

    const MHCSettings& start = work ? *work : mhc;
    MhcPreviewSession n;
    if (!PrepareMhcPreview(start, hdr, mon, n)) return fail(L"The profile's source file cannot be read");
    n.held = held;
    n.onHook = MhcPreviewUsesHook();
    // What scanout runs now, to put back if the preview cannot start after the identity profile went on.
    DisplayInfo di;
    const bool haveDisplay = GetDisplayInfoForMonitor(mon, di);
    std::wstring restore = haveDisplay ? QueryDisplayDefaultProfile(di.adapterId, di.sourceId, hdr) : std::wstring();
    if (restore.empty() && mhc.enabled) restore = mhc.profileName;
    n.restore = restore;

    n.passthrough = EngagePassthroughScanout(mon, hdr, mhc);
    if (n.passthrough.empty()) return fail(L"Scanout could not be switched for the preview");
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (mon < (int)g_gui.monitorSettings.size()) MhcOf(mon, hdr).previewScanoutName = n.passthrough;
    }
    n.active = true;
    // The hold starts with the first publish (the hook's config carries both; the overlay reads it each frame).
    g_previewMon = mon;
    g_previewHdr = hdr;
    g_previewOnHook = n.onHook;
    g_previewActive = true;
    s = std::move(n);
    UpdateMhcPreview(s, start.correctionGrayscale);

    // The hook must acknowledge: a DLL from before the preview (still resident in dwm.exe) never does, and the display
    // would then show no profile at all while the edit runs.
    if (s.onHook) {
        if (g_hookGen == 0 || !PumpUntil(HookShowsCurrentGen, kHookAckWaitMs)) {
            std::wcerr << L"[MHC] live edit preview: the DWM hook did not acknowledge (generation "
                       << DwmHookMhcPreviewGen() << L")" << std::endl;
            // Undo in order: the hook stops, the profile that ran before goes back on, THEN the identity profile goes
            // (Windows keeps applying the last associated MHC2 transform after a disassociation: dropping the identity
            // first would leave the display with no correction).
            StopMhcPreviewDraw(s);
            if (haveDisplay && !restore.empty()) ReassociateMHC2Profile(restore, di.adapterId, di.sourceId, hdr);
            EndMhcPreview(s);
            return fail(L"The DWM hook did not apply the preview (an older hook: turn LIVE LAYERS off and on)");
        }
        std::wcout << L"[MHC] live edit preview: applied by the DWM hook" << std::endl;
    }
    return true;
}

bool PrepareMhcPreview(const MHCSettings& mhc, bool hdr, int mon, MhcPreviewSession& s) {
    s = MhcPreviewSession{};
    s.mon = mon;
    s.hdr = hdr;
    // The variant the bake will install, without its correction (composed live), without desktop gamma and without the
    // user's white balance tweak (held for the edit: the points are measured against the calibration — PQ / the target
    // gamma at its white — and the bake puts both back, as they always sit). Built from the settings, like the bake.
    s.perm = (uint8_t)(ComputeMhcPermutation(mhc, hdr) &
                       ~(MHCSettings::PERM_GS | MHCSettings::PERM_DG | MHCSettings::PERM_WB));
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

void ComposeMhcPreviewBase(const MhcPreviewSession& s, const GrayscaleSettings& base, const GrayscaleSettings& correction,
                           MhcScanout& out) {
    MHC2ProfileParams p = s.params;
    MhcScanout b = s.base;   // the matrix stays; the base LUTs follow the edited curve
    if (!(s.perm & MHCSettings::PERM_NOGRAY)) {
        // The bake's base fields for these settings (a session never edits a base an imported file supplies), and
        // the luminance metadata that follows them (MhcProfileMetadataPeakNits: the base curve's peak once it is on).
        p.grayscaleEnabled = false;
        p.grayscale = GrayscaleData{};
        SetMhcBaseParams(base, p);
        MHCSettings forPeak;
        forPeak.baseGrayscale = base;
        p.peakNits = MhcProfileMetadataPeakNits(forPeak, s.hdr);
        ComputeMhcScanoutBaseLuts(p, b);
    }
    const bool withCorrection = !(s.perm & MHCSettings::PERM_NOGRAY) && correction.enabled && !correction.points.empty();
    p.correctionGrayscaleEnabled = false;
    p.correctionGrayscale.enabled = false;
    if (withCorrection) SetMhcCorrectionParams(correction, p);
    ComposeMhcScanout(p, b, out);
}

void UpdateMhcPreviewBase(const MhcPreviewSession& s, const GrayscaleSettings& base, const GrayscaleSettings& correction) {
    if (!s.active) return;
    MhcScanout scan;
    ComposeMhcPreviewBase(s, base, correction, scan);
    if (s.onHook) g_hookGen = PublishDwmHookMhcPreview(s.mon, s.hdr, scan.applied, scan.lutSize, scan.lut);
    else PublishStage(s.mon, s.hdr, &scan);
}

void UpdateMhcPreview(const MhcPreviewSession& s, const GrayscaleSettings& correction) {
    if (!s.active) return;
    MhcScanout scan;
    ComposeMhcPreview(s, correction, scan);
    if (s.onHook) g_hookGen = PublishDwmHookMhcPreview(s.mon, s.hdr, scan.applied, scan.lutSize, scan.lut);
    else PublishStage(s.mon, s.hdr, &scan);
}

namespace {
// The variant a profile edit previews: the bake's, without desktop gamma and the white balance tweak (held).
uint8_t ProfileEditPerm(const MHCSettings& work, bool hdr) {
    return NormalizeMhcPerm(
        (uint8_t)(ComputeMhcPermutation(work, hdr) & ~(MHCSettings::PERM_DG | MHCSettings::PERM_WB)));
}
}  // namespace

bool ComputeMhcPreviewProfile(const MHCSettings& work, bool hdr, int mon, MhcScanout& out) {
    MhcQuietLog quiet;
    MHC2ProfileParams p;
    if (!BuildMHC2ParamsForPerm(work, hdr, mon, ProfileEditPerm(work, hdr), p)) return false;
    return ComputeMhcScanout(p, out);
}

void ComputeMhcPreviewMatrix(const MHCSettings& work, bool hdr, MhcScanout& inout) {
    // BuildMHC2ParamsForPerm's matrix fields for this variant: the primaries (identity under the stage switch) and no
    // white balance gains (the tweak is held; the white the primaries carry is the calibration's).
    MhcQuietLog quiet;
    MHC2ProfileParams p;
    BuildMHC2MatrixParams(work, hdr, p);
    if (ProfileEditPerm(work, hdr) & MHCSettings::PERM_NOPRIM) p.primariesEnabled = false;
    p.whiteBalanceGains[0] = p.whiteBalanceGains[1] = p.whiteBalanceGains[2] = 1.0f;
    ComputeMhcScanoutMatrix(p, inout);
}

bool UpdateMhcPreviewProfile(MhcPreviewSession& s, const MHCSettings& work, bool matrixOnly) {
    if (!s.active) return false;
    if (matrixOnly && s.shownValid) {
        ComputeMhcPreviewMatrix(work, s.hdr, s.shown);
    } else {
        MhcScanout scan;
        if (!ComputeMhcPreviewProfile(work, s.hdr, s.mon, scan)) return false;
        s.shown = std::move(scan);
        s.shownValid = true;
    }
    if (s.onHook) g_hookGen = PublishDwmHookMhcPreview(s.mon, s.hdr, s.shown.applied, s.shown.lutSize, s.shown.lut);
    else PublishStage(s.mon, s.hdr, &s.shown);
    return true;
}

void RestoreMhcScanout(const MhcPreviewSession& s) {
    if (!s.active || s.restore.empty()) return;
    DisplayInfo di;
    if (GetDisplayInfoForMonitor(s.mon, di)) ReassociateMHC2Profile(s.restore, di.adapterId, di.sourceId, s.hdr);
}

bool WaitMhcPreviewShown(const MhcPreviewSession& s, DWORD timeoutMs) {
    if (!s.active || !s.onHook) return true;   // the overlay draws the change on its next frame
    // The hook applies a generation from its next present, and the recomposition this publish requested makes DWM
    // present the whole screen (a static measurement patch included).
    return PumpUntil([]() { return HookShowsCurrentGen() && !DesktopRecomposePending(); }, timeoutMs);
}

void StopMhcPreviewDraw(const MhcPreviewSession& s) {
    if (!s.active) return;
    if (s.onHook) {
        // The hook redraws only what DWM re-composes: until the requested full recomposition has run, static parts of
        // the screen keep preview pixels. Wait for it, so the caller's re-bake never puts the real profile under them
        // (the profile applied twice). Scanout runs the identity profile meanwhile.
        if (ClearDwmHookMhcPreview()) PumpUntil([]() { return !DesktopRecomposePending(); }, 1500);
    } else {
        PublishStage(-1, false, nullptr);
    }
    std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
    if (s.mon >= 0 && s.mon < (int)g_gui.monitorSettings.size()) MhcOf(s.mon, s.hdr).previewScanoutName.clear();
}

void EndMhcPreview(MhcPreviewSession& s) {
    if (!s.active) return;
    StopMhcPreviewDraw(s);   // no-op work when the caller already stopped it
    DisengagePassthroughScanout(s.mon, s.hdr, s.passthrough);
    // The hold ends after the real profile is back on scanout: tone mapping and FALD return over it.
    const bool onHook = s.onHook;
    g_previewActive = false;
    g_previewOnHook = false;
    g_previewMon = -1;
    g_hookGen = 0;
    s = MhcPreviewSession{};
    if (onHook) UpdateDwmHookSharedConfig();   // the hook's tone mapping / FALD flags (the overlay reads it per frame)
}

bool MhcPreviewActive() { return g_previewActive; }

bool MhcPreviewHolds(int mon, bool hdr) {
    return g_previewActive.load() && g_previewMon.load() == mon && g_previewHdr.load() == hdr;
}

bool MhcPreviewApplied(const MhcPreviewSession& s) {
    if (!s.active) return false;
    return !s.onHook || (IsDwmHookActive() && DwmHookMhcPreviewGen() != 0);
}
