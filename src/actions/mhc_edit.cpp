#include "actions/mhc_edit.h"

#include "actions/actions.h"
#include "actions/notify.h"
#include "desktoplut_ipc_server.h"   // IsCalibrationOrLiveEditActive
#include "displayconfig.h"           // GetMonitorPrimariesFromEDID
#include "globals.h"
#include "gui.h"
#include "gui_mhc.h"
#include "mhc.h"
#include "mhc_preview.h"
#include "processing.h"
#include "settings.h"
#include <atomic>
#include <cwctype>
#include <memory>
#include <mutex>

namespace act {

namespace {

struct MhcEdit {
    bool active = false;
    int mon = -1;
    bool hdr = false;
    bool previewing = false;               // the session below runs
    bool startedForPreview = false;        // this edit started processing / the overlay: stop it after
    bool startedOverlayForPreview = false;
    MhcPreviewSession preview;
    std::unique_ptr<MonitorSettingsPin> pin;   // display re-attach waits for the edit (the index stays the display's)
};
MhcEdit g_edit;
// Begin pumps messages (the processing start, the hook's acknowledgement): an End meanwhile (the window hidden, the
// app exiting) cannot end what has not begun, so it asks, and Begin cancels the edit before returning. End pumps too:
// a nested End (hidden mid-DONE) is ignored, and nothing re-publishes the preview while it runs.
std::atomic<bool> g_beginning{ false };
bool g_cancelRequested = false;
bool g_ending = false;

MHCSettings& MhcOf(int mon, bool hdr) {
    MonitorSettings& ms = g_gui.monitorSettings[(size_t)mon];
    return hdr ? ms.hdrMHC : ms.sdrMHC;
}

std::wstring LowerExt(const std::wstring& path) {
    const size_t dot = path.find_last_of(L'.');
    const size_t slash = path.find_last_of(L"\\/");
    if (dot == std::wstring::npos || (slash != std::wstring::npos && dot < slash)) return std::wstring();
    std::wstring ext = path.substr(dot);
    for (auto& c : ext) c = (wchar_t)towlower(c);
    return ext;
}

void UseIccPrimaries(const ICCProfileData& icc, MHCSettings& work) {
    work.primariesPreset = g_numPresetPrimaries - 1;   // Custom
    DisplayPrimaries& cp = work.customPrimaries;
    cp.Rx = icc.primaries.Rx; cp.Ry = icc.primaries.Ry;
    cp.Gx = icc.primaries.Gx; cp.Gy = icc.primaries.Gy;
    cp.Bx = icc.primaries.Bx; cp.By = icc.primaries.By;
    cp.Wx = icc.primaries.Wx; cp.Wy = icc.primaries.Wy;
    work.primariesEnabled = true;
}

// Undoes Begin's processing start (a failed Begin, or End).
void StopPreviewProcessing(const MhcEdit& e) {
    if (e.startedForPreview) StopProcessing();
    if (e.startedOverlayForPreview) {
        g_mhcEditDialogOpen.store(false);   // what kept the hook-mode overlay awake (EnsureProcessingForPreview)
        DwmHookReevaluateOverlay();
    }
}

}  // namespace

void CopyMhcEditFields(const MHCSettings& from, MHCSettings& to) {
    to.sourceFilePath = from.sourceFilePath;
    to.sourceIs1DCube = from.sourceIs1DCube;
    if (from.sourceFilePath.empty()) to.hasPerChannelTRC = false;   // the bake sets it again for a file
    to.primariesEnabled = from.primariesEnabled;
    to.primariesPreset = from.primariesPreset;
    to.customPrimaries = from.customPrimaries;
    to.baseGrayscale = from.baseGrayscale;
}

MhcImport ImportMhcSourceFile(const std::wstring& path, bool hdr, MHCSettings& work) {
    const std::wstring ext = LowerExt(path);
    if (ext == L".icm" || ext == L".icc") {
        ICCProfileData icc;
        if (!LoadSourceIcc(path, icc)) return MhcImport::Unreadable;   // also: neither primaries nor curves
        // A LUT-based profile's curves are approximate fallbacks with divergent per-channel gammas: not usable for
        // the MHC curve. Only Curves + Matrix profiles carry authoritative curves.
        if (icc.isLUTBased) return MhcImport::LutBased;
        if (icc.hasPrimaries) UseIccPrimaries(icc, work);
        if (hdr) {
            // HDR never uses an ICC profile's curves (sparse measurements over-correct the shadows): the profile
            // gives its primaries and its peak. Named as the source (the bake then scales to its peak) only where
            // that displaces nothing: a source's curve (a 1D cube) or the base curve would otherwise be dropped.
            if (icc.hasLuminance && icc.luminance >= 10.0f) work.baseGrayscale.peakNits = icc.luminance;
            const bool curveInUse = !work.sourceFilePath.empty() ||
                                    (work.baseGrayscale.enabled && !work.baseGrayscale.points.empty());
            if (curveInUse) return MhcImport::PrimariesOnly;
        } else {
            if (!icc.hasTRC) return MhcImport::PrimariesOnly;   // nothing for the SDR curve: the source stays
            if (ExtractGrayscaleFromICC(icc, work.baseGrayscale, hdr)) work.baseGrayscale.enabled = true;
        }
        work.sourceFilePath = path;
        work.sourceIs1DCube = false;
        return MhcImport::Source;
    }
    if (ext == L".cube") {
        std::vector<float> r, g, b;
        if (!LoadSourceCube(path, r, g, b)) return MhcImport::NotOneD;   // a 3D cube (or unreadable)
        work.sourceFilePath = path;
        work.sourceIs1DCube = true;
        work.baseGrayscale.enabled = true;   // the cube is the curve
        return MhcImport::Source;
    }
    return MhcImport::UnknownType;
}

void ClearMhcSourceFile(MHCSettings& work, bool hdr) {
    work.sourceFilePath.clear();
    work.sourceIs1DCube = false;
    work.hasPerChannelTRC = false;
    if (work.baseGrayscale.pointCount <= 1) work.baseGrayscale.pointCount = 20;
    if (hdr) work.baseGrayscale.initLinearPQ();
    else work.baseGrayscale.initLinear();
}

bool DetectMhcPrimaries(int mon, MHCSettings& work) {
    MonitorPrimaries p = GetMonitorPrimariesFromEDID(mon);
    if (!p.valid) p = GetMonitorPrimaries(mon);
    if (!p.valid) return false;
    work.primariesPreset = g_numPresetPrimaries - 1;
    DisplayPrimaries& cp = work.customPrimaries;
    cp.Rx = p.Rx; cp.Ry = p.Ry;
    cp.Gx = p.Gx; cp.Gy = p.Gy;
    cp.Bx = p.Bx; cp.By = p.By;
    cp.Wx = 0.3127f; cp.Wy = 0.3290f;   // D65: an EDID white is the nominal one, not a measurement
    work.primariesEnabled = true;
    return true;
}

MhcSourceInfo DescribeMhcSource(const MHCSettings& work) {
    MhcSourceInfo info;
    if (work.sourceFilePath.empty()) return info;
    info.present = true;
    info.cube = work.sourceIs1DCube;
    if (info.cube) {
        std::vector<float> r, g, b;
        info.readable = LoadSourceCube(work.sourceFilePath, r, g, b);
        info.entries = (int)r.size();
    } else {
        ICCProfileData icc;
        info.readable = LoadSourceIcc(work.sourceFilePath, icc);
        info.primaries = info.readable && icc.hasPrimaries;
        info.curves = info.readable && icc.hasTRC;
        info.luminance = info.readable && icc.hasLuminance ? icc.luminance : 0.0f;
    }
    return info;
}

bool MhcSourceGivesPrimaries(const MHCSettings& work) {
    if (work.sourceFilePath.empty() || work.sourceIs1DCube) return false;
    ICCProfileData icc;
    return LoadSourceIcc(work.sourceFilePath, icc) && icc.hasPrimaries;
}

bool BeginMhcEdit(int mon, bool hdr, MhcEditStart& out) {
    if (g_edit.active || g_beginning.load() || !ValidMonitor(mon)) return false;
    if (IsCalibrationOrLiveEditActive() || g_mhcEditDialogOpen.load() || MhcPreviewActive()) {
        notify::Post(notify::Sev::Warning, mon, L"mhc", L"Another edit or a calibration is in progress");
        return false;
    }
    if (!IsMHC2ApiAvailable()) {
        notify::Post(notify::Sev::Error, mon, L"mhc", L"MHC profiles need Windows 10 21H2 or later");
        return false;
    }
    g_beginning = true;   // other edits, calibrations and the pipe's profile verbs wait from here (MhcEditActive)
    g_cancelRequested = false;
    MhcEdit e;
    e.pin = std::make_unique<MonitorSettingsPin>();
    e.mon = mon;
    e.hdr = hdr;
    out = MhcEditStart{};
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        out.work = MhcOf(mon, hdr);
    }
    // A source file that cannot be read (moved, deleted) would block every preview and the bake: the edit goes on
    // without it (CANCEL keeps the settings as they are, file name included).
    MhcScanout probe;
    if (!out.work.sourceFilePath.empty() && !ComputeMhcPreviewProfile(out.work, hdr, mon, probe)) {
        out.work.sourceFilePath.clear();
        out.work.sourceIs1DCube = false;
        out.sourceDropped = true;
    }

    // The overlay (or, in hook mode, the hook) applies the profile while scanout runs an identity one. Without a
    // preview (the display shows the other mode) the edit still works: DONE installs what was set.
    std::wstring why;
    const bool ready = ReadyMhcPreviewDisplay(mon, hdr, e.startedForPreview, e.startedOverlayForPreview);
    if (ready && BeginMhcPreview(mon, hdr, e.preview, &why, false, &out.work)) {
        e.previewing = true;
        UpdateMhcPreviewProfile(e.preview, out.work);   // the session's profile-edit state (later drags: matrix only)
    } else {
        StopPreviewProcessing(e);
        e.startedForPreview = e.startedOverlayForPreview = false;
        if (MhcPreviewActive()) {
            // Another live edit's preview (begun while this one pumped): DONE would install under it. No edit.
            g_beginning = false;
            notify::Post(notify::Sev::Warning, mon, L"mhc", L"Another edit or a calibration is in progress");
            return false;
        }
        if (!ready)
            why = !MhcPreviewDisplayShowsMode(mon, hdr) ? (hdr ? L"The display is in SDR" : L"The display is in HDR")
                                                        : L"No overlay could run for the display";
        out.why = why;
    }
    out.previewing = e.previewing;
    // While the preview runs, MHC profile monitoring, the desktop gamma follow and the whitelist swaps wait for it (see
    // g_mhcEditDialogOpen), and the overlay stays awake for it. Without a preview nothing on screen depends on the
    // edit (the settings are untouched until DONE): only the pin (display re-attach) and MhcEditActive (other edits,
    // calibrations) hold.
    if (e.previewing) g_mhcEditDialogOpen.store(true);
    e.active = true;
    g_edit = std::move(e);
    g_beginning = false;
    if (g_cancelRequested) {   // the window was hidden or the app is exiting while this pumped
        g_cancelRequested = false;
        EndMhcEdit(false);
        return false;
    }
    return true;
}

bool PreviewMhcEdit(const MHCSettings& work, bool matrixOnly) {
    if (!g_edit.active) return false;
    if (!g_edit.previewing || g_ending) return true;   // nothing shown / the edit is ending (its preview is gone)
    return UpdateMhcPreviewProfile(g_edit.preview, work, matrixOnly);
}

void EndMhcEdit(bool keep, const MHCSettings* work) {
    if (g_beginning.load()) {   // Begin is pumping: it cancels the edit when it returns
        g_cancelRequested = true;
        return;
    }
    if (!g_edit.active || g_ending) return;
    g_ending = true;
    const int mon = g_edit.mon;
    const bool hdr = g_edit.hdr;
    // What DONE was pressed on: the editor's copy must not change what is installed while this pumps.
    const bool install = keep && work;
    const MHCSettings edited = install ? *work : MHCSettings{};
    // The overlay / the hook stops applying the edit before anything is installed: the new profile never sits
    // under preview pixels (applied twice). Scanout runs the identity profile meanwhile.
    if (g_edit.previewing) {
        StopMhcPreviewDraw(g_edit.preview);
        g_mhcEditDialogOpen.store(false);
    }

    bool installed = false;
    if (install) {
        MHCSettings before;
        bool otherPreview = false;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (ValidMonitor(mon)) {
                MHCSettings& m = MhcOf(mon, hdr);
                // Another live edit previews this display and mode (ours stopped above): an install now would sit
                // under its preview. The begin guards keep this from happening; never install into it regardless.
                otherPreview = !m.previewScanoutName.empty();
                if (!otherPreview) {
                    before = m;
                    CopyMhcEditFields(edited, m);
                }
            }
        }
        installed = !otherPreview && ValidMonitor(mon) && GenerateAndInstallMhcProfile(mon, hdr);
        if (otherPreview) {
            notify::Post(notify::Sev::Error, mon, L"mhc", L"Profile not installed: another live edit runs on this display");
        } else if (installed) {
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
                if (ValidMonitor(mon)) ComputeMhcMetadata(MhcOf(mon, hdr), hdr);
            }
            notify::Post(notify::Sev::Success, mon, L"mhc",
                         hdr ? L"HDR calibration profile installed" : L"SDR calibration profile installed");
        } else {
            {
                std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);   // the settings as they were
                if (ValidMonitor(mon)) {
                    MHCSettings& m = MhcOf(mon, hdr);
                    CopyMhcEditFields(before, m);
                    m.hasPerChannelTRC = before.hasPerChannelTRC;   // the forward copy may have cleared it
                }
            }
            notify::Post(notify::Sev::Error, mon, L"mhc",
                         L"Profile not installed (the source file cannot be read, or the GPU refused it): "
                         L"the previous one is back");
        }
    }
    // Cancelled (or failed): what scanout ran before goes back on, THEN the identity profile goes (Windows keeps
    // applying the last associated MHC2 transform: never a frame with nothing associated).
    if (g_edit.previewing) {
        if (!installed) RestoreMhcScanout(g_edit.preview);
        EndMhcPreview(g_edit.preview);
    }
    UpdateMhcFlagsLive(mon);
    StopPreviewProcessing(g_edit);
    if (installed) SaveSettings();
    g_edit = MhcEdit{};   // releases the pin: a deferred display re-attach may run now
    g_ending = false;
    // Desktop gamma waited for the edit: catch up with the SDR white level (debounced re-check).
    if (g_gui.hwndMain) SetTimer(g_gui.hwndMain, SDR_WHITE_CHECK_TIMER_ID, SDR_WHITE_CHECK_DEBOUNCE_MS, nullptr);
    GuiNotifySettingsChanged(mon);
}

bool MhcEditActive() { return g_edit.active || g_beginning.load(); }
int MhcEditMonitor() { return g_edit.active ? g_edit.mon : -1; }
bool MhcEditHdr() { return g_edit.hdr; }
bool MhcEditPreviewing() { return g_edit.active && g_edit.previewing && MhcPreviewApplied(g_edit.preview); }
unsigned MhcEditHeld() { return g_edit.active && g_edit.previewing ? g_edit.preview.held : 0u; }

}  // namespace act
