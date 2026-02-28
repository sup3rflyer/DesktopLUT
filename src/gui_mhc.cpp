// DesktopLUT - gui_mhc.cpp
// MHC settings dialog and helpers

#include "gui_mhc.h"
#include "gui_shared.h"
#include "gui.h"
#include "globals.h"
#include "settings.h"
#include "processing.h"
#include "color.h"
#include "mhc.h"
#include "displayconfig.h"
#include <commctrl.h>
#include <commdlg.h>
#include <algorithm>
#include <iostream>

// ============================================================================
// SECTION: MHC Helper Functions
// ============================================================================

// Update MHC active flags on the running MonitorContext
// Called after MHC install/remove/enable toggle — tracks state for diagnostics and live preview
void UpdateMhcFlagsLive(int monitorIndex) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
    const auto& ms = g_gui.monitorSettings[monitorIndex];

    bool sdrMhcActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty();
    bool hdrMhcActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty();

    // Update running MonitorContext if processing is active
    bool found = false;
    for (auto& ctx : g_monitors) {
        if (ctx.index == monitorIndex) {
            ctx.sdrMhcPrimariesActive = sdrMhcActive && ms.sdrMHC.primariesEnabled;
            ctx.sdrMhcGrayscaleActive = sdrMhcActive && ms.sdrMHC.grayscale.enabled;
            ctx.hdrMhcPrimariesActive = hdrMhcActive && ms.hdrMHC.primariesEnabled;
            ctx.hdrMhcGrayscaleActive = hdrMhcActive && ms.hdrMHC.grayscale.enabled;
            std::cout << "[MHC Flags] mon=" << monitorIndex
                      << " sdrPrim=" << ctx.sdrMhcPrimariesActive
                      << " sdrGs=" << ctx.sdrMhcGrayscaleActive
                      << " hdrPrim=" << ctx.hdrMhcPrimariesActive
                      << " hdrGs=" << ctx.hdrMhcGrayscaleActive << std::endl;
            found = true;
            break;
        }
    }
    if (!found) {
        std::cout << "[MHC Flags] mon=" << monitorIndex
                  << " NOT FOUND in g_monitors (size=" << g_monitors.size() << ")" << std::endl;
    }
}

// Compute metadata strings for display in MHC section labels
void ComputeMhcMetadata(MHCSettings& mhc, bool isHDR) {
    // --- Primaries label ---
    if (!mhc.primariesEnabled) {
        mhc.metaPrimaries = isHDR ? L"Rec.2020" : L"sRGB";
    } else if (mhc.primariesPreset == 0) {
        // Preset 0 = sRGB/Rec.709
        if (!isHDR && mhc.grayscale.use24Gamma)
            mhc.metaPrimaries = L"Rec.709";
        else
            mhc.metaPrimaries = L"sRGB";
    } else if (mhc.primariesPreset < g_numPresetPrimaries - 1) {
        mhc.metaPrimaries = g_presetPrimaries[mhc.primariesPreset].name;
    } else {
        // Custom preset - try to match against known primaries for a nicer label
        const float tol = 0.005f;
        bool matched = false;
        for (int i = 0; i < g_numPresetPrimaries - 1; i++) {
            const auto& p = g_presetPrimaries[i];
            if (fabsf(mhc.customPrimaries.Rx - p.Rx) < tol && fabsf(mhc.customPrimaries.Ry - p.Ry) < tol &&
                fabsf(mhc.customPrimaries.Gx - p.Gx) < tol && fabsf(mhc.customPrimaries.Gy - p.Gy) < tol &&
                fabsf(mhc.customPrimaries.Bx - p.Bx) < tol && fabsf(mhc.customPrimaries.By - p.By) < tol) {
                mhc.metaPrimaries = p.name;
                // Refine sRGB vs Rec.709
                if (i == 0 && !isHDR && mhc.grayscale.use24Gamma)
                    mhc.metaPrimaries = L"Rec.709";
                matched = true;
                break;
            }
        }
        if (!matched)
            mhc.metaPrimaries = L"Custom";
    }

    // --- Gamma/EOTF label ---
    // Check if grayscale points are manually adjusted from default
    // (only meaningful when per-channel TRC is NOT present, since TRC overrides grayscale in profile)
    bool hasAdjustedPoints = false;
    if (!mhc.hasPerChannelTRC && mhc.grayscale.enabled && !mhc.grayscale.points.empty()) {
        GrayscaleSettings defaultGs;
        defaultGs.pointCount = mhc.grayscale.pointCount;
        if (isHDR) defaultGs.initLinearPQ();
        else defaultGs.initLinear();
        for (int i = 0; i < mhc.grayscale.pointCount && i < (int)mhc.grayscale.points.size(); i++) {
            if (i < (int)defaultGs.points.size() && fabsf(mhc.grayscale.points[i] - defaultGs.points[i]) > 0.001f) {
                hasAdjustedPoints = true;
                break;
            }
        }
    }

    std::wstring gammaBase;
    if (isHDR) {
        if (hasAdjustedPoints) {
            wchar_t buf[32];
            swprintf_s(buf, L"%dpt-Custom", mhc.grayscale.pointCount);
            gammaBase = buf;
        } else {
            gammaBase = L"PQ";
        }
    } else {
        if (hasAdjustedPoints) {
            wchar_t buf[32];
            swprintf_s(buf, L"Custom (%dpt)", mhc.grayscale.pointCount);
            gammaBase = buf;
        } else if (mhc.grayscale.use24Gamma) {
            gammaBase = L"2.2\u21922.4";
        } else {
            gammaBase = L"2.2";
        }
    }
    if (mhc.hasPerChannelTRC) {
        gammaBase += L" + TRC";
    }
    mhc.metaGamma = gammaBase;

    // --- Peak nits (HDR only) ---
    if (isHDR) {
        mhc.metaPeakNits = mhc.grayscale.peakNits;
    } else {
        mhc.metaPeakNits = 0.0f;
    }
}

// ============================================================================
// SECTION: MHC Profile Generation & Installation
// ============================================================================

// Generate, write, and install MHC2 ICC profile from current MHCSettings
// Updates mhc.enabled/profilePath/profileName on success, calls UpdateMhcFlagsLive
// Returns true if profile was generated and installed successfully
bool GenerateAndInstallMhcProfile(int monitorIndex, bool isHDR) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
    if (!IsMHC2ApiAvailable()) return false;

    auto& settings = g_gui.monitorSettings[monitorIndex];
    auto& mhc = isHDR ? settings.hdrMHC : settings.sdrMHC;

    MHC2ProfileParams params;
    params.monitorName = (monitorIndex < (int)g_gui.monitorNames.size())
        ? g_gui.monitorNames[monitorIndex] : L"Monitor";
    params.isHDR = isHDR;

    if (mhc.primariesEnabled) {
        params.primariesEnabled = true;
        int preset = mhc.primariesPreset;
        if (preset == g_numPresetPrimaries - 1) {
            const auto& cp = mhc.customPrimaries;
            params.displayPrimaries = { cp.Rx, cp.Ry, cp.Gx, cp.Gy, cp.Bx, cp.By, cp.Wx, cp.Wy };
        } else {
            const auto& p = g_presetPrimaries[preset];
            params.displayPrimaries = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
        }
    }

    // If source file is set, re-read and use per-channel data directly
    if (!mhc.sourceFilePath.empty()) {
        if (mhc.sourceIs1DCube) {
            // 1D cube: per-channel correction curves used directly as MHC2 LUT
            std::vector<float> corrR, corrG, corrB;
            if (Load1DCubeLUT(mhc.sourceFilePath, corrR, corrG, corrB)) {
                params.hasPrecomputedCorrection = true;
                params.corrR = std::move(corrR);
                params.corrG = std::move(corrG);
                params.corrB = std::move(corrB);
                params.grayscaleEnabled = true;
                params.grayscale.enabled = true;
            }
        } else {
            // ICC: per-channel TRC (characterization curves, need inversion)
            ICCProfileData icc;
            if (ReadICCProfile(mhc.sourceFilePath, icc) && icc.hasTRC) {
                params.hasPerChannelTRC = true;
                params.trcR = icc.trcR;
                params.trcG = icc.trcG;
                params.trcB = icc.trcB;
                params.grayscaleEnabled = true;
                params.grayscale.enabled = true;
                params.grayscale.use24Gamma = mhc.grayscale.use24Gamma;
            }
        }
        // Peak nits is display metadata, needed regardless of file type
        params.peakNits = mhc.grayscale.peakNits;
    } else if (mhc.grayscale.enabled) {
        // Safety: if points are empty (e.g., dialog set enabled=true without init), use identity
        if (mhc.grayscale.points.empty()) {
            params.grayscaleEnabled = false;
            params.grayscale.enabled = false;
        } else {
            params.grayscaleEnabled = true;
            params.grayscale.enabled = true;
            params.grayscale.pointCount = mhc.grayscale.pointCount;
            for (int i = 0; i < mhc.grayscale.pointCount && i < 32; i++) {
                params.grayscale.points[i] = (i < (int)mhc.grayscale.points.size())
                    ? mhc.grayscale.points[i] : 0.0f;
            }
            params.grayscale.use24Gamma = mhc.grayscale.use24Gamma;
            params.grayscale.peakNits = mhc.grayscale.peakNits;
            params.peakNits = mhc.grayscale.peakNits;
        }
    }

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return false;

    // Use unique filename each time to bypass Windows profile caching
    static int profileSeq = 0;
    std::wstring profileName = L"DesktopLUT_" + (isHDR ? std::wstring(L"HDR") : std::wstring(L"SDR"))
        + L"_" + std::to_wstring(GetTickCount64()) + L".icm";

    // Write to temp directory - InstallColorProfileW copies to system color dir
    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + profileName;

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return false;

    // Remove old profile association and delete old file from color dir
    if (mhc.enabled && !mhc.profileName.empty()) {
        RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        // Delete old file from system color directory
        wchar_t sysDir[MAX_PATH];
        GetSystemDirectory(sysDir, MAX_PATH);
        std::wstring oldPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + mhc.profileName;
        DeleteFileW(oldPath.c_str());
    }

    if (!WriteMHC2Profile(profileData, tempPath)) return false;

    if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
        DeleteFileW(tempPath.c_str());
        return false;
    }
    DeleteFileW(tempPath.c_str());

    // Store the system color directory path
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring profilePath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        mhc.enabled = true;
        mhc.profilePath = profilePath;
        mhc.profileName = profileName;
        mhc.hasPerChannelTRC = params.hasPerChannelTRC || params.hasPrecomputedCorrection;
    }
    UpdateMhcFlagsLive(monitorIndex);
    return true;
}

// Auto-regenerate and reinstall MHC profile when MHC settings change
// Only acts if MHC is enabled and a profile is already installed for the given mode
void RegenerateMhcIfActive(int monitorIndex, bool isHDR) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
    auto& settings = g_gui.monitorSettings[monitorIndex];
    auto& mhc = isHDR ? settings.hdrMHC : settings.sdrMHC;
    if (!mhc.enabled || mhc.profileName.empty()) return;
    if (!IsMHC2ApiAvailable()) return;

    // Read from MHC's own primaries/grayscale (Layer 1), not shader's (Layer 3)
    MHC2ProfileParams params;
    params.monitorName = (monitorIndex < (int)g_gui.monitorNames.size())
        ? g_gui.monitorNames[monitorIndex] : L"Monitor";
    params.isHDR = isHDR;

    if (mhc.primariesEnabled) {
        params.primariesEnabled = true;
        int preset = mhc.primariesPreset;
        if (preset == g_numPresetPrimaries - 1) {
            const auto& cp = mhc.customPrimaries;
            params.displayPrimaries = { cp.Rx, cp.Ry, cp.Gx, cp.Gy, cp.Bx, cp.By, cp.Wx, cp.Wy };
        } else {
            const auto& p = g_presetPrimaries[preset];
            params.displayPrimaries = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
        }
    }

    // If source file is set, re-read and use per-channel data directly
    if (!mhc.sourceFilePath.empty()) {
        if (mhc.sourceIs1DCube) {
            // 1D cube: per-channel correction curves used directly as MHC2 LUT
            std::vector<float> corrR, corrG, corrB;
            if (Load1DCubeLUT(mhc.sourceFilePath, corrR, corrG, corrB)) {
                params.hasPrecomputedCorrection = true;
                params.corrR = std::move(corrR);
                params.corrG = std::move(corrG);
                params.corrB = std::move(corrB);
                params.grayscaleEnabled = true;
                params.grayscale.enabled = true;
            }
        } else {
            // ICC: per-channel TRC (characterization curves, need inversion)
            ICCProfileData icc;
            if (ReadICCProfile(mhc.sourceFilePath, icc) && icc.hasTRC) {
                params.hasPerChannelTRC = true;
                params.trcR = icc.trcR;
                params.trcG = icc.trcG;
                params.trcB = icc.trcB;
                params.grayscaleEnabled = true;
                params.grayscale.enabled = true;
                params.grayscale.use24Gamma = mhc.grayscale.use24Gamma;
            }
        }
        // Peak nits is display metadata, needed regardless of file type
        params.peakNits = mhc.grayscale.peakNits;
    } else if (mhc.grayscale.enabled) {
        if (mhc.grayscale.points.empty()) {
            params.grayscaleEnabled = false;
            params.grayscale.enabled = false;
        } else {
            params.grayscaleEnabled = true;
            params.grayscale.enabled = true;
            params.grayscale.pointCount = mhc.grayscale.pointCount;
            for (int i = 0; i < mhc.grayscale.pointCount && i < 32; i++) {
                params.grayscale.points[i] = (i < (int)mhc.grayscale.points.size())
                    ? mhc.grayscale.points[i] : 0.0f;
            }
            params.grayscale.use24Gamma = mhc.grayscale.use24Gamma;
            params.grayscale.peakNits = mhc.grayscale.peakNits;
            params.peakNits = mhc.grayscale.peakNits;
        }
    }

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return;

    // Unique filename to bypass caching
    std::wstring newProfileName = L"DesktopLUT_" + (isHDR ? std::wstring(L"HDR") : std::wstring(L"SDR"))
        + L"_" + std::to_wstring(GetTickCount64()) + L".icm";

    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + newProfileName;

    DisplayInfo displayInfo;
    if (GetDisplayInfoForMonitor(monitorIndex, displayInfo)) {
        // Remove old profile and clean up old file
        RemoveMHC2Profile(mhc.profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        wchar_t sysDir[MAX_PATH];
        GetSystemDirectory(sysDir, MAX_PATH);
        std::wstring oldPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + mhc.profileName;
        DeleteFileW(oldPath.c_str());
    }

    if (!WriteMHC2Profile(profileData, tempPath)) return;

    if (GetDisplayInfoForMonitor(monitorIndex, displayInfo)) {
        if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
            std::cerr << "RegenerateMhcIfActive: InstallMHC2Profile failed for monitor "
                      << monitorIndex << (isHDR ? " HDR" : " SDR") << std::endl;
            DeleteFileW(tempPath.c_str());
            return;
        }
    }
    DeleteFileW(tempPath.c_str());

    // Update stored name
    wchar_t sysDir2[MAX_PATH];
    GetSystemDirectory(sysDir2, MAX_PATH);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        mhc.profilePath = std::wstring(sysDir2) + L"\\spool\\drivers\\color\\" + newProfileName;
        mhc.profileName = newProfileName;
        mhc.hasPerChannelTRC = params.hasPerChannelTRC;
    }
    UpdateMhcFlagsLive(monitorIndex);
}

// ============================================================================
// SECTION: MHC Info Display
// ============================================================================

// Update MHC info labels in the appropriate SDR or HDR groupbox
void UpdateMhcInfoDisplay(int monitorIndex, bool isHDR) {
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
    const auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                             : g_gui.monitorSettings[monitorIndex].sdrMHC;

    HWND hwndStatus = isHDR ? g_gui.hwndHdrMhcStatus : g_gui.hwndMhcStatus;
    HWND* coords = isHDR ? g_gui.hwndHdrMhcIccCoords : g_gui.hwndMhcIccCoords;

    if (!hwndStatus) return;

    bool installed = !mhc.profileName.empty();

    // Status text with indicator
    if (installed) {
        SetWindowText(hwndStatus, (L"\x25CF Active: " + mhc.profileName).c_str());
    } else {
        SetWindowText(hwndStatus, L"\x25CB Not installed");
    }

    // Show target display primaries from MHC settings (not ICC file colorants)
    // SDR ICC colorants are always sRGB by design, which is useless info
    if (installed && mhc.primariesEnabled) {
        // Resolve preset to actual primaries
        const DisplayPrimaries& p = (mhc.primariesPreset >= 0 && mhc.primariesPreset < g_numPresetPrimaries - 1)
            ? g_presetPrimaries[mhc.primariesPreset]
            : mhc.customPrimaries;
        float vals[8] = { p.Rx, p.Ry, p.Gx, p.Gy, p.Bx, p.By, p.Wx, p.Wy };
        for (int i = 0; i < 8; i++) {
            if (coords[i]) {
                wchar_t buf[16];
                swprintf_s(buf, L"%.4f", vals[i]);
                SetWindowText(coords[i], buf);
            }
        }
    } else {
        for (int i = 0; i < 8; i++)
            if (coords[i]) SetWindowText(coords[i], L"");
    }

    // Show/hide metadata labels
    HWND* metaLabels = isHDR ? g_gui.hwndHdrMhcMetaLabels : g_gui.hwndMhcMetaLabels;
    if (installed && !mhc.metaPrimaries.empty()) {
        std::wstring primText = L"Primaries: " + mhc.metaPrimaries;
        if (metaLabels[0]) { SetWindowText(metaLabels[0], primText.c_str()); ShowWindow(metaLabels[0], SW_SHOW); }
    } else {
        if (metaLabels[0]) { SetWindowText(metaLabels[0], L""); ShowWindow(metaLabels[0], SW_HIDE); }
    }
    if (installed && !mhc.metaGamma.empty()) {
        std::wstring gammaText = L"Gamma: " + mhc.metaGamma;
        if (metaLabels[1]) { SetWindowText(metaLabels[1], gammaText.c_str()); ShowWindow(metaLabels[1], SW_SHOW); }
    } else {
        if (metaLabels[1]) { SetWindowText(metaLabels[1], L""); ShowWindow(metaLabels[1], SW_HIDE); }
    }
    if (installed && isHDR && mhc.metaPeakNits > 0.0f) {
        wchar_t peakBuf[64];
        swprintf_s(peakBuf, L"Peak: %.0f nits", mhc.metaPeakNits);
        if (metaLabels[2]) { SetWindowText(metaLabels[2], peakBuf); ShowWindow(metaLabels[2], SW_SHOW); }
    } else {
        if (metaLabels[2]) { SetWindowText(metaLabels[2], L""); ShowWindow(metaLabels[2], SW_HIDE); }
    }
}

// Helper to recalculate primaries matrix and apply live update
// isHDR parameter controls which settings struct is modified
void ApplyPrimariesChange(bool isHDR) {
    if (g_gui.currentMonitor < 0 || g_gui.currentMonitor >= (int)g_gui.monitorSettings.size()) {
        return;
    }
    // Primaries matrix and white balance gains are computed in ConvertColorCorrection()
    // Just trigger the live update with current stored values
    if (g_gui.isRunning) {
        UpdateColorCorrectionLive(g_gui.currentMonitor, isHDR);
    }
    UpdateGUIState();
}

// ============================================================================
// SECTION: MHC Settings Dialog
// ============================================================================

struct MhcDialogData {
    MHCSettings* settings;  // Pointer to the MHCSettings being edited
    MHCSettings backup;     // Copy for Cancel restore
    bool isHDR;
    int monitorIndex;
    // Live preview state (when processing is running)
    bool livePreview = false;       // True if previewing MHC corrections through shader
    bool hadProfile = false;        // True if ICC profile was installed before Edit
    std::wstring origProfileName;   // Original ICC profile name for Cancel restore
    std::wstring origProfilePath;   // Original ICC profile path for Cancel restore
    // Control handles
    HWND hwndPrimariesEnable, hwndPreset, hwndRx, hwndRy, hwndGx, hwndGy, hwndBx, hwndBy, hwndWx, hwndWy;
    HWND hwndGrayscaleEnable, hwndGs10, hwndGs20, hwndGs32, hwndGs24, hwndGsPeak;
    // New controls for redesigned dialog
    HWND hwndFilePath = nullptr;
    HWND hwndFileClear = nullptr;
    HWND hwndGrayscaleReset = nullptr;
    HWND hwndScrollPanel = nullptr;  // Horizontal scroll panel for trackbars
    HWND hwndDialog = nullptr;
    // Font resources
    HFONT smallFont = nullptr;
    // File import state
    ICCProfileData loadedICC;
    bool hasLoadedICC = false;
    std::wstring loadedFilePath;
    bool loadedFileIs1DCube = false;    // 1D .cube (per-channel correction curves)
    std::vector<float> loaded1DR, loaded1DG, loaded1DB;  // 1D cube correction data
    bool fileLoaded = false;  // True when a file provides the profile data (disables manual controls)
    // Embedded grayscale trackbars
    std::vector<HWND> sliders;
    std::vector<HWND> pctLabels;
    bool updatingSliders = false;
};

static MhcDialogData* g_mhcDialog = nullptr;

// Rebuild embedded grayscale trackbars in the scroll panel
static void MhcRebuildTrackbars(MhcDialogData* d) {
    if (!d || !d->hwndScrollPanel) return;

    // Destroy existing trackbars and labels
    for (HWND h : d->sliders) if (h) DestroyWindow(h);
    for (HWND h : d->pctLabels) if (h) DestroyWindow(h);
    d->sliders.clear();
    d->pctLabels.clear();

    auto& gs = d->settings->grayscale;
    if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
        gs.points.resize(gs.pointCount);
        if (d->isHDR) gs.initLinearPQ();
        else gs.initLinear();
    }

    int N = gs.pointCount;
    int sliderW = 32, sliderH = 120, pad = 2;
    int pctLabelH = 14;
    int startX = 10, startY = 4;
    int totalW = startX + N * (sliderW + pad) + 10;

    HFONT font = (HFONT)SendMessage(d->hwndDialog, WM_GETFONT, 0, 0);
    if (!font) font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);

    if (d->smallFont) DeleteObject(d->smallFont);
    d->smallFont = CreateFont(12, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
        CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    HFONT smallFont = d->smallFont;

    int maxRange = GRAYSCALE_RANGE * GRAYSCALE_SLIDER_SCALE;

    for (int i = 0; i < N; i++) {
        int x = startX + i * (sliderW + pad);

        // Input % label above slider
        wchar_t pctBuf[8];
        if (d->isHDR) {
            float pctVal = (float)(i + 1) / (float)N * 100.0f;
            swprintf_s(pctBuf, L"%.0f%%", pctVal);
        } else {
            float t = (float)i / (float)(N - 1);
            float inputPct = t * t * 100.0f;
            swprintf_s(pctBuf, L"%.0f%%", inputPct);
        }
        HWND label = CreateWindow(L"STATIC", pctBuf, WS_CHILD | WS_VISIBLE | SS_CENTER,
            x, startY, sliderW, pctLabelH, d->hwndScrollPanel, nullptr, nullptr, nullptr);
        SendMessage(label, WM_SETFONT, (WPARAM)smallFont, TRUE);
        d->pctLabels.push_back(label);

        // Vertical trackbar
        HWND slider = CreateWindow(TRACKBAR_CLASS, nullptr,
            WS_CHILD | WS_VISIBLE | TBS_VERT | TBS_NOTICKS,
            x, startY + pctLabelH, sliderW, sliderH,
            d->hwndScrollPanel, (HMENU)(INT_PTR)(ID_GRAYSCALE_SLIDER_BASE + i),
            nullptr, nullptr);
        SendMessage(slider, TBM_SETRANGE, TRUE, MAKELONG(-maxRange, maxRange));

        // Calculate deviation from linear
        float expected;
        if (d->isHDR) {
            expected = (float)i / (float)(N - 1);
        } else {
            float t = (float)i / (float)(N - 1);
            expected = t * t;
        }
        float actual = (i < (int)gs.points.size()) ? gs.points[i] : expected;
        float deviation = 0.0f;
        if (expected > 0.001f) {
            deviation = (actual - expected) / expected * 100.0f;
        }
        int sliderPos = (int)(-deviation * GRAYSCALE_SLIDER_SCALE);
        sliderPos = (std::max)(-maxRange, (std::min)(maxRange, sliderPos));
        SendMessage(slider, TBM_SETPOS, TRUE, sliderPos);
        d->sliders.push_back(slider);
    }

    // Update scroll panel content width
    SCROLLINFO si = {};
    si.cbSize = sizeof(si);
    si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
    si.nMin = 0;
    si.nMax = totalW;
    RECT rc;
    GetClientRect(d->hwndScrollPanel, &rc);
    si.nPage = rc.right;
    si.nPos = 0;
    SetScrollInfo(d->hwndScrollPanel, SB_HORZ, &si, TRUE);
    ShowScrollBar(d->hwndScrollPanel, SB_HORZ, totalW > rc.right);
}

// Read slider positions back into grayscale points
static void MhcReadSlidersToPoints(MhcDialogData* d) {
    if (!d || d->updatingSliders) return;
    auto& gs = d->settings->grayscale;
    int N = gs.pointCount;
    for (int i = 0; i < N && i < (int)d->sliders.size(); i++) {
        int pos = (int)SendMessage(d->sliders[i], TBM_GETPOS, 0, 0);
        float deviation = (float)(-pos) / GRAYSCALE_SLIDER_SCALE;

        float expected;
        if (d->isHDR) {
            expected = (float)i / (float)(N - 1);
        } else {
            float t = (float)i / (float)(N - 1);
            expected = t * t;
        }
        float actual = expected * (1.0f + deviation / 100.0f);
        if (i < (int)gs.points.size()) {
            gs.points[i] = std::clamp(actual, 0.0f, 1.0f);
        }
    }
}

// Horizontal scroll panel for embedded trackbars
static LRESULT CALLBACK MhcScrollPanelProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_HSCROLL: {
        SCROLLINFO si = {};
        si.cbSize = sizeof(si);
        si.fMask = SIF_ALL;
        GetScrollInfo(hwnd, SB_HORZ, &si);

        int oldPos = si.nPos;
        int newPos = oldPos;
        switch (LOWORD(wParam)) {
        case SB_LINELEFT:     newPos -= 20; break;
        case SB_LINERIGHT:    newPos += 20; break;
        case SB_PAGELEFT:     newPos -= si.nPage; break;
        case SB_PAGERIGHT:    newPos += si.nPage; break;
        case SB_THUMBTRACK:   newPos = si.nTrackPos; break;
        case SB_THUMBPOSITION: newPos = si.nTrackPos; break;
        }

        int maxPos = (std::max)(0, si.nMax - (int)si.nPage);
        newPos = (std::max)(0, (std::min)(newPos, maxPos));

        if (newPos != oldPos) {
            int delta = newPos - oldPos;
            si.fMask = SIF_POS;
            si.nPos = newPos;
            SetScrollInfo(hwnd, SB_HORZ, &si, TRUE);
            ScrollWindowEx(hwnd, -delta, 0, nullptr, nullptr, nullptr, nullptr,
                SW_SCROLLCHILDREN | SW_INVALIDATE | SW_ERASE);
        }
        return 0;
    }

    case WM_VSCROLL:
        // Forward vertical trackbar notifications to parent dialog for live preview
        SendMessage(GetParent(hwnd), WM_HSCROLL, wParam, lParam);
        return 0;

    case WM_MOUSEWHEEL: {
        int delta = GET_WHEEL_DELTA_WPARAM(wParam);
        SendMessage(hwnd, WM_HSCROLL, delta > 0 ? SB_LINELEFT : SB_LINERIGHT, 0);
        for (int i = 1; i < 3; i++)
            SendMessage(hwnd, WM_HSCROLL, delta > 0 ? SB_LINELEFT : SB_LINERIGHT, 0);
        return 0;
    }

    case WM_ERASEBKGND: {
        HDC hdc = (HDC)wParam;
        RECT rc;
        GetClientRect(hwnd, &rc);
        FillRect(hdc, &rc, GetSysColorBrush(COLOR_BTNFACE));
        return 1;
    }

    case WM_CTLCOLORSTATIC: {
        HDC hdc = (HDC)wParam;
        SetBkColor(hdc, GetSysColor(COLOR_BTNFACE));
        return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
    }
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

static void MhcUpdatePrimariesFields(MhcDialogData* d) {
    int sel = (int)SendMessage(d->hwndPreset, CB_GETCURSEL, 0, 0);
    // Only block editing when the loaded file specifically provides primaries (ICC with primaries)
    // 1D cube files have no primaries data — coordinate fields should stay editable in Custom mode
    bool filePrimaries = d->fileLoaded && d->hasLoadedICC && d->loadedICC.hasPrimaries;
    bool custom = (sel == g_numPresetPrimaries - 1) && !filePrimaries;
    EnableWindow(d->hwndRx, custom); EnableWindow(d->hwndRy, custom);
    EnableWindow(d->hwndGx, custom); EnableWindow(d->hwndGy, custom);
    EnableWindow(d->hwndBx, custom); EnableWindow(d->hwndBy, custom);
    EnableWindow(d->hwndWx, custom); EnableWindow(d->hwndWy, custom);

    wchar_t buf[16];
    float Rx, Ry, Gx, Gy, Bx, By, Wx, Wy;
    if (custom) {
        const auto& cp = d->settings->customPrimaries;
        Rx = cp.Rx; Ry = cp.Ry; Gx = cp.Gx; Gy = cp.Gy;
        Bx = cp.Bx; By = cp.By; Wx = cp.Wx; Wy = cp.Wy;
    } else {
        const auto& p = g_presetPrimaries[sel];
        Rx = p.Rx; Ry = p.Ry; Gx = p.Gx; Gy = p.Gy;
        Bx = p.Bx; By = p.By; Wx = p.Wx; Wy = p.Wy;
    }
    swprintf_s(buf, L"%.4f", Rx); SetWindowText(d->hwndRx, buf);
    swprintf_s(buf, L"%.4f", Ry); SetWindowText(d->hwndRy, buf);
    swprintf_s(buf, L"%.4f", Gx); SetWindowText(d->hwndGx, buf);
    swprintf_s(buf, L"%.4f", Gy); SetWindowText(d->hwndGy, buf);
    swprintf_s(buf, L"%.4f", Bx); SetWindowText(d->hwndBx, buf);
    swprintf_s(buf, L"%.4f", By); SetWindowText(d->hwndBy, buf);
    swprintf_s(buf, L"%.4f", Wx); SetWindowText(d->hwndWx, buf);
    swprintf_s(buf, L"%.4f", Wy); SetWindowText(d->hwndWy, buf);
}

// Chromaticity bounds — generous enough for any real display, prevents screen-breaking values
static constexpr float CHROM_MIN = 0.01f;   // No real primary is at 0
static constexpr float CHROM_X_MAX = 0.80f;  // Spectral locus max x ~0.74
static constexpr float CHROM_Y_MAX = 0.90f;  // Spectral locus max y ~0.83
static constexpr float WHITE_MIN = 0.20f;    // All standard illuminants > 0.25
static constexpr float WHITE_MAX = 0.50f;    // Illuminant A ~0.45 is the warmest common white

static float ReadAndClamp(HWND hwnd, float lo, float hi) {
    wchar_t buf[16];
    GetWindowText(hwnd, buf, 16);
    return std::clamp((float)_wtof(buf), lo, hi);
}

static void MhcSaveCustomFromFields(MhcDialogData* d) {
    auto& cp = d->settings->customPrimaries;
    cp.Rx = ReadAndClamp(d->hwndRx, CHROM_MIN, CHROM_X_MAX);
    cp.Ry = ReadAndClamp(d->hwndRy, CHROM_MIN, CHROM_Y_MAX);
    cp.Gx = ReadAndClamp(d->hwndGx, CHROM_MIN, CHROM_X_MAX);
    cp.Gy = ReadAndClamp(d->hwndGy, CHROM_MIN, CHROM_Y_MAX);
    cp.Bx = ReadAndClamp(d->hwndBx, CHROM_MIN, CHROM_X_MAX);
    cp.By = ReadAndClamp(d->hwndBy, CHROM_MIN, CHROM_Y_MAX);
    cp.Wx = ReadAndClamp(d->hwndWx, WHITE_MIN, WHITE_MAX);
    cp.Wy = ReadAndClamp(d->hwndWy, WHITE_MIN, WHITE_MAX);
}

// Enable/disable manual controls based on what data the loaded file provides
// 1D cube: only provides TRC -> lock grayscale, leave primaries unlocked
// ICC with primaries+TRC: lock both
// ICC with primaries only: lock primaries, leave grayscale unlocked
// ICC with TRC only: lock grayscale, leave primaries unlocked
static void MhcSetFileLoadedState(MhcDialogData* d, bool loaded) {
    d->fileLoaded = loaded;

    // Determine what the file provides
    bool filePrimaries = loaded && d->hasLoadedICC && d->loadedICC.hasPrimaries;
    bool fileTRC = loaded && (d->loadedFileIs1DCube || (d->hasLoadedICC && d->loadedICC.hasTRC));

    BOOL primEnable = filePrimaries ? FALSE : TRUE;
    BOOL gsEnable = fileTRC ? FALSE : TRUE;

    // Primaries section: lock preset dropdown and Detect when file provides primaries
    // Coordinate fields are handled by MhcUpdatePrimariesFields (respects both file state and preset)
    if (d->hwndPreset) EnableWindow(d->hwndPreset, primEnable);
    HWND hwndDetect = GetDlgItem(d->hwndDialog, ID_MHC_PRIMARIES_DETECT);
    if (hwndDetect) EnableWindow(hwndDetect, primEnable);

    // Gamma controls - only lock when file provides TRC
    if (d->hwndGs10) EnableWindow(d->hwndGs10, gsEnable);
    if (d->hwndGs20) EnableWindow(d->hwndGs20, gsEnable);
    if (d->hwndGs32) EnableWindow(d->hwndGs32, gsEnable);
    // Peak nits stays enabled - it's display metadata for HDR, not a grayscale correction
    if (d->hwndGrayscaleReset) EnableWindow(d->hwndGrayscaleReset, gsEnable);
    HWND hwndEdit = GetDlgItem(d->hwndDialog, ID_MHC_GRAYSCALE_EDIT);
    if (hwndEdit) EnableWindow(hwndEdit, gsEnable);

    // Update coordinate field enable state (accounts for both file state and preset selection)
    MhcUpdatePrimariesFields(d);
}

// Push current MHC settings as temporary shader corrections for live preview
static void MhcPushLivePreview(MhcDialogData* d) {
    if (!d || !d->livePreview) return;

    // When a file is loaded, determine what it provides
    bool filePrimaries = d->fileLoaded && d->hasLoadedICC && d->loadedICC.hasPrimaries;
    bool fileGrayscale = d->fileLoaded && (d->loadedFileIs1DCube || (d->hasLoadedICC && d->loadedICC.hasTRC));

    // Skip preview entirely when file provides both primaries and grayscale (all controls locked)
    if (filePrimaries && fileGrayscale) return;
    // Note: display mode match is guaranteed by the Edit handler (livePreview=false when mismatched)

    // Save custom primaries from edit boxes
    if (d->settings->primariesPreset == g_numPresetPrimaries - 1)
        MhcSaveCustomFromFields(d);

    // Build temporary ColorCorrectionSettings from MHC settings
    ColorCorrectionSettings tempCC;
    tempCC.primariesEnabled = d->settings->primariesEnabled;
    tempCC.primariesPreset = d->settings->primariesPreset;
    tempCC.customPrimaries = d->settings->customPrimaries;

    // Only preview grayscale if the file doesn't handle it
    // (1D cube TRC / ICC TRC can't be previewed through shader — they're baked into the ICC at Apply)
    if (!fileGrayscale) {
        tempCC.grayscale.enabled = d->settings->grayscale.enabled;
        tempCC.grayscale.pointCount = d->settings->grayscale.pointCount;
        tempCC.grayscale.points = d->settings->grayscale.points;
        tempCC.grayscale.peakNits = d->settings->grayscale.peakNits;
        tempCC.grayscale.use24Gamma = d->settings->grayscale.use24Gamma;
    }

    ColorCorrectionData data = ConvertColorCorrection(tempCC, d->isHDR);

    std::cout << "[MHC Preview] PUSH: mon=" << d->monitorIndex << " isHDR=" << d->isHDR
              << " primEnabled=" << data.primariesEnabled
              << " gsEnabled=" << data.grayscale.enabled
              << " gsPts=" << data.grayscale.pointCount
              << std::endl;

    // Push to render thread (single slot matching display mode)
    std::lock_guard<std::mutex> lock(g_colorCorrectionMutex);
    g_pendingColorCorrections.erase(
        std::remove_if(g_pendingColorCorrections.begin(), g_pendingColorCorrections.end(),
            [&](const PendingColorCorrection& p) {
                return p.monitorIndex == d->monitorIndex;
            }),
        g_pendingColorCorrections.end());
    g_pendingColorCorrections.push_back({ d->monitorIndex, d->isHDR, data, true });
    g_hasPendingColorCorrections.store(true, std::memory_order_release);
}

static LRESULT CALLBACK MhcDialogProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* d = g_mhcDialog;
    switch (msg) {
    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case ID_MHC_PRIMARIES_ENABLE:
            if (d->hwndPrimariesEnable)
                d->settings->primariesEnabled = (SendMessage(d->hwndPrimariesEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
            MhcPushLivePreview(d);
            return 0;

        // Live preview when coordinate edit boxes lose focus (clamps + writes back)
        case ID_MHC_PRIMARIES_RX: case ID_MHC_PRIMARIES_RY:
        case ID_MHC_PRIMARIES_GX: case ID_MHC_PRIMARIES_GY:
        case ID_MHC_PRIMARIES_BX: case ID_MHC_PRIMARIES_BY:
        case ID_MHC_PRIMARIES_WX: case ID_MHC_PRIMARIES_WY:
            if (HIWORD(wParam) == EN_KILLFOCUS) {
                MhcSaveCustomFromFields(d);
                // Write clamped values back so user sees the actual value
                wchar_t clampBuf[16];
                const auto& cp = d->settings->customPrimaries;
                float vals[8] = { cp.Rx, cp.Ry, cp.Gx, cp.Gy, cp.Bx, cp.By, cp.Wx, cp.Wy };
                HWND fields[8] = { d->hwndRx, d->hwndRy, d->hwndGx, d->hwndGy,
                                   d->hwndBx, d->hwndBy, d->hwndWx, d->hwndWy };
                int idx = LOWORD(wParam) - ID_MHC_PRIMARIES_RX;
                if (idx >= 0 && idx < 8) {
                    swprintf_s(clampBuf, L"%.4f", vals[idx]);
                    SetWindowText(fields[idx], clampBuf);
                }
                MhcPushLivePreview(d);
            }
            return 0;

        case ID_MHC_PRIMARIES_PRESET:
            if (HIWORD(wParam) == CBN_SELCHANGE) {
                int oldPreset = d->settings->primariesPreset;
                if (oldPreset == g_numPresetPrimaries - 1) MhcSaveCustomFromFields(d);
                d->settings->primariesPreset = (int)SendMessage(d->hwndPreset, CB_GETCURSEL, 0, 0);
                MhcUpdatePrimariesFields(d);
                MhcPushLivePreview(d);
            }
            return 0;

        case ID_MHC_PRIMARIES_DETECT: {
            MonitorPrimaries primaries = GetMonitorPrimariesFromEDID(d->monitorIndex);
            if (!primaries.valid) primaries = GetMonitorPrimaries(d->monitorIndex);
            if (primaries.valid) {
                d->settings->primariesPreset = g_numPresetPrimaries - 1;
                SendMessage(d->hwndPreset, CB_SETCURSEL, d->settings->primariesPreset, 0);
                auto& cp = d->settings->customPrimaries;
                cp.Rx = primaries.Rx; cp.Ry = primaries.Ry;
                cp.Gx = primaries.Gx; cp.Gy = primaries.Gy;
                cp.Bx = primaries.Bx; cp.By = primaries.By;
                cp.Wx = 0.3127f; cp.Wy = 0.3290f;  // D65 white point
                d->settings->primariesEnabled = true;
                if (d->hwndPrimariesEnable)
                    SendMessage(d->hwndPrimariesEnable, BM_SETCHECK, BST_CHECKED, 0);
                MhcUpdatePrimariesFields(d);
                MhcPushLivePreview(d);
            } else {
                MessageBox(hwnd, L"Could not detect monitor primaries.\nEDID data may not be available.",
                    L"Detection Failed", MB_OK | MB_ICONWARNING);
            }
            return 0;
        }

        // ID_MHC_PRIMARIES_EXTRACT removed - file import auto-populates

        case ID_MHC_GRAYSCALE_ENABLE:
            if (d->hwndGrayscaleEnable)
                d->settings->grayscale.enabled = (SendMessage(d->hwndGrayscaleEnable, BM_GETCHECK, 0, 0) == BST_CHECKED);
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_GRAYSCALE_10:
        case ID_MHC_GRAYSCALE_20:
        case ID_MHC_GRAYSCALE_32: {
            int newCount = (LOWORD(wParam) == ID_MHC_GRAYSCALE_10) ? 10 :
                           (LOWORD(wParam) == ID_MHC_GRAYSCALE_20) ? 20 : 32;
            if (newCount != d->settings->grayscale.pointCount) {
                d->settings->grayscale.pointCount = newCount;
                d->settings->grayscale.points.resize(newCount);
                if (d->isHDR) d->settings->grayscale.initLinearPQ();
                else d->settings->grayscale.initLinear();
                MhcPushLivePreview(d);
            }
            return 0;
        }

        case ID_MHC_GRAYSCALE_RESET:
            if (d->isHDR) d->settings->grayscale.initLinearPQ();
            else d->settings->grayscale.initLinear();
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_GRAYSCALE_EDIT: {
            // Read HDR peak before opening editor
            if (d->isHDR && d->hwndGsPeak) {
                wchar_t buf[16];
                GetWindowText(d->hwndGsPeak, buf, 16);
                d->settings->grayscale.peakNits = (float)_wtof(buf);
                if (d->settings->grayscale.peakNits < 10.0f) d->settings->grayscale.peakNits = 10.0f;
            }
            // Ensure points are initialized
            auto& gs = d->settings->grayscale;
            if (gs.points.empty() || (int)gs.points.size() != gs.pointCount) {
                gs.points.resize(gs.pointCount);
                if (d->isHDR) gs.initLinearPQ(); else gs.initLinear();
            }
            // Open popup grayscale editor with MHC live preview callback
            auto* dCapture = d;
            ShowGrayscaleEditor(hwnd, gs, d->isHDR,
                [dCapture]() { MhcPushLivePreview(dCapture); });
            // After editor closes, push final state
            MhcPushLivePreview(d);
            return 0;
        }

        // ID_MHC_GRAYSCALE_EXTRACT removed - file import auto-populates

        case ID_MHC_GRAYSCALE_24:
            if (d->hwndGs24)
                d->settings->grayscale.use24Gamma = (SendMessage(d->hwndGs24, BM_GETCHECK, 0, 0) == BST_CHECKED);
            MhcPushLivePreview(d);
            return 0;

        case ID_MHC_FILE_BROWSE: {
            wchar_t path[MAX_PATH] = {};
            OPENFILENAME ofn = { sizeof(ofn) };
            ofn.hwndOwner = hwnd;
            ofn.lpstrFilter = L"ICC/Cube Files\0*.icm;*.icc;*.cube\0ICC Profiles\0*.icm;*.icc\0Cube LUTs\0*.cube\0All Files\0*.*\0";
            ofn.lpstrFile = path;
            ofn.nMaxFile = MAX_PATH;
            ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
            if (GetOpenFileName(&ofn)) {
                SetWindowText(d->hwndFilePath, path);
                d->loadedFilePath = path;

                // Determine file type and parse
                std::wstring ext = path;
                size_t dot = ext.find_last_of(L'.');
                if (dot != std::wstring::npos) ext = ext.substr(dot);
                for (auto& c : ext) c = towlower(c);

                d->hasLoadedICC = false;
                d->loadedFileIs1DCube = false;
                d->loaded1DR.clear();
                d->loaded1DG.clear();
                d->loaded1DB.clear();
                d->loadedICC = {};

                if (ext == L".icm" || ext == L".icc") {
                    if (ReadICCProfile(path, d->loadedICC)) {
                        if (!d->loadedICC.hasPrimaries && !d->loadedICC.hasTRC) {
                            SetWindowText(d->hwndFilePath, L"");
                            d->loadedFilePath.clear();
                            MessageBox(hwnd,
                                L"ICC profile contains no usable primaries or transfer curves.",
                                L"Unsupported Profile", MB_OK | MB_ICONWARNING);
                            return 0;
                        }
                        d->hasLoadedICC = true;
                    } else {
                        SetWindowText(d->hwndFilePath, L"");
                        d->loadedFilePath.clear();
                        MessageBox(hwnd,
                            L"Failed to read ICC profile. The file may be corrupt or unsupported.",
                            L"Read Error", MB_OK | MB_ICONERROR);
                        return 0;
                    }
                } else if (ext == L".cube") {
                    // Only 1D cube supported - 3D cubes don't provide per-channel data
                    if (Load1DCubeLUT(path, d->loaded1DR, d->loaded1DG, d->loaded1DB)) {
                        d->loadedFileIs1DCube = true;
                    } else {
                        SetWindowText(d->hwndFilePath, L"");
                        d->loadedFilePath.clear();
                        MessageBox(hwnd,
                            L"This is a 3D .cube file. Only 1D .cube files (e.g. BMD_4096) are supported for MHC profiles.\n\n"
                            L"3D .cube files should be loaded in the 3D LUT tab instead.",
                            L"Unsupported Format", MB_OK | MB_ICONWARNING);
                        return 0;
                    }
                }

                // Auto-populate primaries from loaded file
                if (d->hasLoadedICC && d->loadedICC.hasPrimaries) {
                    d->settings->primariesPreset = g_numPresetPrimaries - 1;
                    SendMessage(d->hwndPreset, CB_SETCURSEL, d->settings->primariesPreset, 0);
                    auto& cp = d->settings->customPrimaries;
                    cp.Rx = d->loadedICC.primaries.Rx; cp.Ry = d->loadedICC.primaries.Ry;
                    cp.Gx = d->loadedICC.primaries.Gx; cp.Gy = d->loadedICC.primaries.Gy;
                    cp.Bx = d->loadedICC.primaries.Bx; cp.By = d->loadedICC.primaries.By;
                    cp.Wx = d->loadedICC.primaries.Wx; cp.Wy = d->loadedICC.primaries.Wy;
                    d->settings->primariesEnabled = true;
                    MhcUpdatePrimariesFields(d);
                }

                // Auto-populate grayscale from loaded file
                // 1D cube: per-channel correction used directly (no grayscale extraction needed)
                // ICC: per-channel TRC extracted
                bool gsExtracted = false;
                if (d->loadedFileIs1DCube) {
                    // 1D cube provides per-channel correction - mark grayscale as handled
                    gsExtracted = true;
                } else if (d->hasLoadedICC && d->loadedICC.hasTRC) {
                    gsExtracted = ExtractGrayscaleFromICC(d->loadedICC, d->settings->grayscale, d->isHDR);
                }
                if (gsExtracted) {
                    d->settings->grayscale.enabled = true;
                }

                // Show summary of what was extracted from the file
                {
                    std::wstring msg = L"Loaded: ";
                    // Extract filename only
                    std::wstring fname = d->loadedFilePath;
                    size_t sl = fname.find_last_of(L"\\/");
                    if (sl != std::wstring::npos) fname = fname.substr(sl + 1);
                    msg += fname + L"\n\nUsing:";

                    if (d->loadedFileIs1DCube) {
                        int sz = (int)d->loaded1DR.size();
                        wchar_t buf[64];
                        swprintf_s(buf, L"\n  TRC R/G/B (%d points per channel)", sz);
                        msg += buf;
                        msg += L"\n\nNote: 1D cube files don't contain primaries.\nUse Detect or enter them manually.";
                    } else if (d->hasLoadedICC) {
                        if (!d->loadedICC.description.empty())
                            msg += L"\n  Description: " + d->loadedICC.description;
                        if (d->loadedICC.hasPrimaries)
                            msg += L"\n  Primaries (R/G/B/W chromaticity)";
                        if (d->loadedICC.hasTRC) {
                            if (d->loadedICC.hasGamma) {
                                wchar_t buf[64];
                                swprintf_s(buf, L"\n  TRC R/G/B (gamma %.2f)", d->loadedICC.gamma);
                                msg += buf;
                            } else {
                                wchar_t buf[64];
                                swprintf_s(buf, L"\n  TRC R(%d) G(%d) B(%d)",
                                    (int)d->loadedICC.trcR.size(), (int)d->loadedICC.trcG.size(), (int)d->loadedICC.trcB.size());
                                msg += buf;
                            }
                        }
                        if (d->loadedICC.hasLuminance) {
                            wchar_t buf[64];
                            swprintf_s(buf, L"\n  Luminance: %.0f cd/m\u00B2", d->loadedICC.luminance);
                            msg += buf;
                        }
                    }
                    MessageBox(hwnd, msg.c_str(), L"File Import", MB_OK | MB_ICONINFORMATION);
                }

                // Lock manual controls - file provides the profile data
                // No live preview for file-loaded state (Apply generates the ICC profile)
                MhcSetFileLoadedState(d, true);
                if (d->hwndFileClear) ShowWindow(d->hwndFileClear, SW_SHOW);
            }
            return 0;
        }

        case ID_MHC_FILE_CLEAR: {
            // Clear loaded file and unlock manual controls
            SetWindowText(d->hwndFilePath, L"");
            d->loadedFilePath.clear();
            d->hasLoadedICC = false;
            d->loadedFileIs1DCube = false;
            d->loaded1DR.clear();
            d->loaded1DG.clear();
            d->loaded1DB.clear();
            d->loadedICC = {};
            MhcSetFileLoadedState(d, false);
            if (d->hwndFileClear) ShowWindow(d->hwndFileClear, SW_HIDE);
            // Reset grayscale to linear (file extraction data is no longer valid)
            if (d->isHDR)
                d->settings->grayscale.initLinearPQ();
            else
                d->settings->grayscale.initLinear();
            // Re-enable coordinate fields based on preset selection
            MhcUpdatePrimariesFields(d);
            MhcPushLivePreview(d);
            return 0;
        }

        case ID_MHC_OK:
            // Save custom primaries from fields before closing
            if (d->settings->primariesPreset == g_numPresetPrimaries - 1)
                MhcSaveCustomFromFields(d);
            // Save grayscale peak if HDR
            if (d->isHDR && d->hwndGsPeak) {
                wchar_t buf[16];
                GetWindowText(d->hwndGsPeak, buf, 16);
                d->settings->grayscale.peakNits = (float)_wtof(buf);
                if (d->settings->grayscale.peakNits < 10.0f) d->settings->grayscale.peakNits = 10.0f;
            }
            // Store source file path and type for profile regeneration
            if (d->fileLoaded && !d->loadedFilePath.empty()) {
                d->settings->sourceFilePath = d->loadedFilePath;
                d->settings->sourceIs1DCube = d->loadedFileIs1DCube;
            } else {
                d->settings->sourceFilePath.clear();
                d->settings->sourceIs1DCube = false;
                d->settings->hasPerChannelTRC = false;
            }
            // When live previewing, generate + install ICC profile to bake settings
            if (d->livePreview) {
                if (!GenerateAndInstallMhcProfile(d->monitorIndex, d->isHDR)) {
                    MessageBox(hwnd, L"Failed to generate or install MHC2 profile.",
                        L"Error", MB_OK | MB_ICONERROR);
                }
            }
            ComputeMhcMetadata(*d->settings, d->isHDR);
            DestroyWindow(hwnd);
            return 0;

        case ID_MHC_CANCEL:
            *d->settings = d->backup;
            // Restore original ICC profile if one was installed before Edit
            if (d->livePreview && d->hadProfile) {
                d->settings->enabled = true;
                d->settings->profileName = d->origProfileName;
                d->settings->profilePath = d->origProfilePath;
                DisplayInfo displayInfo;
                if (GetDisplayInfoForMonitor(d->monitorIndex, displayInfo)) {
                    ReassociateMHC2Profile(d->origProfileName, displayInfo.adapterId, displayInfo.sourceId, d->isHDR);
                }
                UpdateMhcFlagsLive(d->monitorIndex);
            }
            DestroyWindow(hwnd);
            return 0;
        }
        break;

    // WM_HSCROLL removed - no embedded trackbars, grayscale uses popup editor

    case WM_CLOSE:
        *d->settings = d->backup;
        // Restore original ICC profile if one was installed before Edit
        if (d->livePreview && d->hadProfile) {
            d->settings->enabled = true;
            d->settings->profileName = d->origProfileName;
            d->settings->profilePath = d->origProfilePath;
            DisplayInfo displayInfo;
            if (GetDisplayInfoForMonitor(d->monitorIndex, displayInfo)) {
                ReassociateMHC2Profile(d->origProfileName, displayInfo.adapterId, displayInfo.sourceId, d->isHDR);
            }
            UpdateMhcFlagsLive(d->monitorIndex);
        }
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        if (g_mhcDialog && g_mhcDialog->smallFont) {
            DeleteObject(g_mhcDialog->smallFont);
            g_mhcDialog->smallFont = nullptr;
        }
        g_mhcDialog = nullptr;
        return 0;

    case WM_DRAWITEM: {
        LPDRAWITEMSTRUCT pDIS = (LPDRAWITEMSTRUCT)lParam;
        if (pDIS->CtlType == ODT_BUTTON) {
            DrawRoundedButton(pDIS);
            return TRUE;
        }
        break;
    }

    case WM_CTLCOLORSTATIC:
    case WM_CTLCOLORBTN:
        SetBkColor((HDC)wParam, GetSysColor(COLOR_BTNFACE));
        return (LRESULT)GetSysColorBrush(COLOR_BTNFACE);
    }
    return DefWindowProc(hwnd, msg, wParam, lParam);
}

void ShowMhcSettingsDialog(HWND hwndParent, MHCSettings& settings, bool isHDR, int monitorIndex,
                           bool livePreview, bool hadProfile,
                           const std::wstring& origProfileName,
                           const std::wstring& origProfilePath) {
    static bool mhcRegistered = false;
    if (!mhcRegistered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = MhcDialogProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = GetSysColorBrush(COLOR_BTNFACE);
        wc.lpszClassName = L"DesktopLUT_MHC";
        RegisterClassEx(&wc);
        mhcRegistered = true;
    }

    static bool scrollRegistered = false;
    if (!scrollRegistered) {
        WNDCLASSEX wc = { sizeof(WNDCLASSEX) };
        wc.lpfnWndProc = MhcScrollPanelProc;
        wc.hInstance = GetModuleHandle(nullptr);
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.hbrBackground = GetSysColorBrush(COLOR_BTNFACE);
        wc.lpszClassName = L"DesktopLUT_MHC_Scroll";
        RegisterClassEx(&wc);
        scrollRegistered = true;
    }

    MhcDialogData data = {};
    data.settings = &settings;
    data.backup = settings;
    data.isHDR = isHDR;
    data.monitorIndex = monitorIndex;
    data.livePreview = livePreview;
    data.hadProfile = hadProfile;
    data.origProfileName = origProfileName;
    data.origProfilePath = origProfilePath;
    g_mhcDialog = &data;

    // Dialog size: 460 wide
    int dlgW = 460, dlgH = isHDR ? 355 : 330;
    RECT adjustRect = { 0, 0, dlgW, dlgH };
    AdjustWindowRectEx(&adjustRect, WS_POPUP | WS_CAPTION | WS_SYSMENU, FALSE, WS_EX_DLGMODALFRAME);
    int winW = adjustRect.right - adjustRect.left;
    int winH = adjustRect.bottom - adjustRect.top;

    RECT parentRect;
    GetWindowRect(hwndParent, &parentRect);
    int x = parentRect.left + (parentRect.right - parentRect.left - winW) / 2;
    int y = parentRect.top + (parentRect.bottom - parentRect.top - winH) / 2;

    HWND dlg = CreateWindowEx(WS_EX_DLGMODALFRAME,
        L"DesktopLUT_MHC", isHDR ? L"MHC Settings (HDR)" : L"MHC Settings (SDR)",
        WS_POPUP | WS_CAPTION | WS_SYSMENU,
        x, y, winW, winH, hwndParent, nullptr, GetModuleHandle(nullptr), nullptr);
    data.hwndDialog = dlg;

    int cx = 10, cy = 8, h = 24, w = dlgW - 20;
    HFONT font = (HFONT)SendMessage(hwndParent, WM_GETFONT, 0, 0);
    if (!font) font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);

    auto makeCtrl = [&](const wchar_t* cls, const wchar_t* text, DWORD style, int px, int py, int pw, int ph, int id) -> HWND {
        HWND h = CreateWindow(cls, text, WS_CHILD | WS_VISIBLE | style, px, py, pw, ph, dlg, (HMENU)(INT_PTR)id, nullptr, nullptr);
        SendMessage(h, WM_SETFONT, (WPARAM)font, TRUE);
        return h;
    };

    // === File Import Bar ===
    makeCtrl(L"STATIC", L"File:", 0, cx, cy + 2, 28, h, 0);
    data.hwndFilePath = makeCtrl(L"EDIT", L"", ES_AUTOHSCROLL | ES_READONLY | WS_BORDER,
        cx + 30, cy, w - 140, h, ID_MHC_FILE_PATH);
    makeCtrl(L"BUTTON", L"Browse", BS_OWNERDRAW, cx + w - 105, cy, 55, h, ID_MHC_FILE_BROWSE);
    data.hwndFileClear = makeCtrl(L"BUTTON", L"Clear", BS_OWNERDRAW, cx + w - 45, cy, 45, h, ID_MHC_FILE_CLEAR);
    ShowWindow(data.hwndFileClear, SW_HIDE);  // Hidden until file loaded

    // === Display Primaries Section ===
    cy += h + 8;
    makeCtrl(L"BUTTON", L"Display Primaries", BS_GROUPBOX, cx, cy, w, 110, 0);

    // Always enabled when editing - no checkbox needed
    data.hwndPrimariesEnable = nullptr;
    settings.primariesEnabled = true;

    makeCtrl(L"STATIC", L"Preset:", 0, cx + 10, cy + 20, 40, h, 0);
    data.hwndPreset = makeCtrl(L"COMBOBOX", nullptr, CBS_DROPDOWNLIST | WS_VSCROLL,
        cx + 55, cy + 18, 150, 150, ID_MHC_PRIMARIES_PRESET);
    for (int i = 0; i < g_numPresetPrimaries; i++)
        SendMessage(data.hwndPreset, CB_ADDSTRING, 0, (LPARAM)g_presetPrimaries[i].name);
    SendMessage(data.hwndPreset, CB_SETCURSEL, settings.primariesPreset, 0);

    makeCtrl(L"BUTTON", L"Detect", BS_OWNERDRAW, cx + 210, cy + 18, 55, h, ID_MHC_PRIMARIES_DETECT);

    // Coordinate fields
    int chromY = cy + 48, chromX = cx + 10, chromW = 52, chromLW = 18;
    makeCtrl(L"STATIC", L"R:", 0, chromX, chromY + 2, chromLW, h, 0);
    data.hwndRx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_RX);
    data.hwndRy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_RY);
    makeCtrl(L"STATIC", L"G:", 0, chromX + 160, chromY + 2, chromLW, h, 0);
    data.hwndGx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_GX);
    data.hwndGy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_GY);

    chromY += h + 4;
    makeCtrl(L"STATIC", L"B:", 0, chromX, chromY + 2, chromLW, h, 0);
    data.hwndBx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_BX);
    data.hwndBy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_BY);
    makeCtrl(L"STATIC", L"W:", 0, chromX + 160, chromY + 2, chromLW, h, 0);
    data.hwndWx = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW, chromY, chromW, h, ID_MHC_PRIMARIES_WX);
    data.hwndWy = makeCtrl(L"EDIT", L"", WS_BORDER, chromX + 160 + chromLW + chromW + 4, chromY, chromW, h, ID_MHC_PRIMARIES_WY);

    MhcUpdatePrimariesFields(&data);

    // Apply numeric validation to coordinate edit boxes (4 decimal places, strips spaces)
    SetNumericEdit(data.hwndRx, 4); SetNumericEdit(data.hwndRy, 4);
    SetNumericEdit(data.hwndGx, 4); SetNumericEdit(data.hwndGy, 4);
    SetNumericEdit(data.hwndBx, 4); SetNumericEdit(data.hwndBy, 4);
    SetNumericEdit(data.hwndWx, 4); SetNumericEdit(data.hwndWy, 4);

    // === Gamma Section ===
    cy += 118;
    int gsGroupH = isHDR ? 105 : 77;
    makeCtrl(L"BUTTON", isHDR ? L"EOTF" : L"Gamma", BS_GROUPBOX, cx, cy, w, gsGroupH, 0);

    // Always enabled when editing
    data.hwndGrayscaleEnable = nullptr;
    data.hwndGs24 = nullptr;
    data.hwndGsPeak = nullptr;
    data.hwndScrollPanel = nullptr;
    settings.grayscale.enabled = true;
    // Ensure points are initialized (empty for never-configured monitors → all-zeros LUT → black screen)
    if (settings.grayscale.points.empty() || (int)settings.grayscale.points.size() != settings.grayscale.pointCount) {
        if (isHDR) settings.grayscale.initLinearPQ();
        else settings.grayscale.initLinear();
    }

    // HDR: Peak nits on first row
    int gsRowY = cy + 18;
    if (isHDR) {
        makeCtrl(L"STATIC", L"Peak:", 0, cx + 10, gsRowY + 2, 32, h, 0);
        data.hwndGsPeak = makeCtrl(L"EDIT", L"", WS_BORDER | ES_NUMBER,
            cx + 46, gsRowY, 50, h, ID_MHC_GRAYSCALE_PEAK);
        wchar_t peakBuf[16];
        swprintf_s(peakBuf, L"%.0f", settings.grayscale.peakNits);
        SetWindowText(data.hwndGsPeak, peakBuf);
        SetNumericEdit(data.hwndGsPeak, 0);
        makeCtrl(L"STATIC", L"nits", 0, cx + 100, gsRowY + 2, 25, h, 0);
        gsRowY += h + 4;
    }

    // Row 1: Radio buttons for point count (matching corrections tab)
    int rbX = cx + 10;
    data.hwndGs10 = makeCtrl(L"BUTTON", L"10", BS_AUTORADIOBUTTON | WS_GROUP,
        rbX, gsRowY, 40, h, ID_MHC_GRAYSCALE_10);
    data.hwndGs20 = makeCtrl(L"BUTTON", L"20", BS_AUTORADIOBUTTON,
        rbX + 45, gsRowY, 40, h, ID_MHC_GRAYSCALE_20);
    data.hwndGs32 = makeCtrl(L"BUTTON", L"32", BS_AUTORADIOBUTTON,
        rbX + 90, gsRowY, 40, h, ID_MHC_GRAYSCALE_32);

    // Select current point count
    int pc = settings.grayscale.pointCount;
    SendMessage(data.hwndGs10, BM_SETCHECK, pc == 10 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(data.hwndGs20, BM_SETCHECK, pc == 20 ? BST_CHECKED : BST_UNCHECKED, 0);
    SendMessage(data.hwndGs32, BM_SETCHECK, pc == 32 ? BST_CHECKED : BST_UNCHECKED, 0);

    // Row 2: Edit Points... + Reset buttons (below radio buttons)
    gsRowY += h + 5;
    makeCtrl(L"BUTTON", L"Edit Points...", BS_OWNERDRAW, rbX, gsRowY, 90, h, ID_MHC_GRAYSCALE_EDIT);
    data.hwndGrayscaleReset = makeCtrl(L"BUTTON", L"Reset", BS_OWNERDRAW, rbX + 100, gsRowY, 60, h, ID_MHC_GRAYSCALE_RESET);

    // Restore file-loaded state if settings have a source file
    if (!settings.sourceFilePath.empty()) {
        SetWindowText(data.hwndFilePath, settings.sourceFilePath.c_str());
        data.loadedFilePath = settings.sourceFilePath;
        if (settings.sourceIs1DCube) {
            // Re-load 1D cube correction data
            if (Load1DCubeLUT(settings.sourceFilePath, data.loaded1DR, data.loaded1DG, data.loaded1DB))
                data.loadedFileIs1DCube = true;
        } else {
            // Try to re-read ICC data
            std::wstring ext = settings.sourceFilePath;
            size_t dot = ext.find_last_of(L'.');
            if (dot != std::wstring::npos) ext = ext.substr(dot);
            for (auto& c : ext) c = towlower(c);
            if (ext == L".icm" || ext == L".icc") {
                if (ReadICCProfile(settings.sourceFilePath, data.loadedICC))
                    data.hasLoadedICC = true;
            }
        }
        MhcSetFileLoadedState(&data, true);
        ShowWindow(data.hwndFileClear, SW_SHOW);
    }

    // Push initial live preview if processing is running
    if (livePreview) {
        MhcPushLivePreview(&data);
    }

    // === Apply / Cancel Buttons ===
    cy += gsGroupH + 8;
    int btnW = 70, btnH = 26;
    makeCtrl(L"BUTTON", L"Apply", BS_OWNERDRAW, dlgW / 2 - btnW - 10, cy, btnW, btnH, ID_MHC_OK);
    makeCtrl(L"BUTTON", L"Cancel", BS_OWNERDRAW, dlgW / 2 + 10, cy, btnW, btnH, ID_MHC_CANCEL);

    // Show as modal
    EnableWindow(hwndParent, FALSE);
    ShowWindow(dlg, SW_SHOW);
    UpdateWindow(dlg);

    MSG winMsg;
    while (GetMessage(&winMsg, nullptr, 0, 0)) {
        if (!IsWindow(dlg)) break;
        // Enter: push live preview (confirm text box edits) or apply if not previewing
        if (winMsg.message == WM_KEYDOWN && winMsg.wParam == VK_RETURN) {
            if (data.livePreview) {
                MhcPushLivePreview(&data);
            } else {
                SendMessage(dlg, WM_COMMAND, ID_MHC_OK, 0);
            }
            continue;
        }
        // Esc: cancel dialog
        if (winMsg.message == WM_KEYDOWN && winMsg.wParam == VK_ESCAPE) {
            SendMessage(dlg, WM_COMMAND, ID_MHC_CANCEL, 0);
            continue;
        }
        TranslateMessage(&winMsg);
        DispatchMessage(&winMsg);
    }

    EnableWindow(hwndParent, TRUE);
    SetForegroundWindow(hwndParent);
}
