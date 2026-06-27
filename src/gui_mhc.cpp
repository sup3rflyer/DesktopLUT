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
    // Snapshot MHC state under lock — callers may have just released the mutex
    bool sdrMhcActive, hdrMhcActive, sdrHasGs, hdrHasGs;
    bool sdrPrimEnabled, hdrPrimEnabled;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
        const auto& ms = g_gui.monitorSettings[monitorIndex];

        sdrMhcActive = ms.sdrMHC.enabled && !ms.sdrMHC.profileName.empty();
        hdrMhcActive = ms.hdrMHC.enabled && !ms.hdrMHC.profileName.empty();
        // Grayscale flag covers all MHC gamma/grayscale forms that suppress shader equivalents
        sdrHasGs = ms.sdrMHC.baseGrayscale.enabled || ms.sdrMHC.correctionGrayscale.enabled ||
                   ms.sdrMHC.hasPerChannelTRC || !ms.sdrMHC.sourceFilePath.empty();
        hdrHasGs = ms.hdrMHC.baseGrayscale.enabled || ms.hdrMHC.correctionGrayscale.enabled ||
                   ms.hdrMHC.hasPerChannelTRC || !ms.hdrMHC.sourceFilePath.empty() ||
                   ms.hdrMHC.desktopGammaEnabled;
        sdrPrimEnabled = ms.sdrMHC.primariesEnabled;
        hdrPrimEnabled = ms.hdrMHC.primariesEnabled;
    }

    // Update running MonitorContext if processing is active. The flag fields are
    // read by the render thread; the g_monitorsMutex here guards the container
    // traversal against a concurrent build/teardown (the snapshot lock above is
    // already released, so there is no nesting with g_monitorSettingsMutex).
    bool found = false;
    {
        std::lock_guard<std::mutex> lk(g_monitorsMutex);
        for (auto& ctx : g_monitors) {
            if (ctx.index == monitorIndex) {
                ctx.sdrMhcPrimariesActive = sdrMhcActive && sdrPrimEnabled;
                ctx.sdrMhcGrayscaleActive = sdrMhcActive && sdrHasGs;
                ctx.hdrMhcPrimariesActive = hdrMhcActive && hdrPrimEnabled;
                ctx.hdrMhcGrayscaleActive = hdrMhcActive && hdrHasGs;
                std::cout << "[MHC Flags] mon=" << monitorIndex
                          << " sdrPrim=" << ctx.sdrMhcPrimariesActive
                          << " sdrGs=" << ctx.sdrMhcGrayscaleActive
                          << " hdrPrim=" << ctx.hdrMhcPrimariesActive
                          << " hdrGs=" << ctx.hdrMhcGrayscaleActive << std::endl;
                found = true;
                break;
            }
        }
        if (!found && !g_monitors.empty()) {
            std::cout << "[MHC Flags] mon=" << monitorIndex
                      << " NOT FOUND in g_monitors (size=" << g_monitors.size() << ")" << std::endl;
        }
    }
}

// Compute metadata strings for display in MHC section labels
void ComputeMhcMetadata(MHCSettings& mhc, bool isHDR) {
    // --- Primaries label ---
    if (!mhc.primariesEnabled) {
        mhc.metaPrimaries = isHDR ? L"Rec.2020" : L"sRGB";
    } else if (mhc.primariesPreset == 0) {
        // Preset 0 = sRGB/Rec.709
        if (!isHDR && mhc.baseGrayscale.use24Gamma)
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
                if (i == 0 && !isHDR && mhc.baseGrayscale.use24Gamma)
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
    if (!mhc.hasPerChannelTRC && mhc.baseGrayscale.enabled && !mhc.baseGrayscale.points.empty()) {
        GrayscaleSettings defaultGs;
        defaultGs.pointCount = mhc.baseGrayscale.pointCount;
        if (isHDR) defaultGs.initLinearPQ();
        else defaultGs.initLinear();
        for (int i = 0; i < mhc.baseGrayscale.pointCount && i < (int)mhc.baseGrayscale.points.size(); i++) {
            if (i < (int)defaultGs.points.size() && fabsf(mhc.baseGrayscale.points[i] - defaultGs.points[i]) > 0.001f) {
                hasAdjustedPoints = true;
                break;
            }
        }
    }

    std::wstring gammaBase;
    if (isHDR) {
        if (hasAdjustedPoints) {
            wchar_t buf[32];
            swprintf_s(buf, L"%dpt-Custom", mhc.baseGrayscale.pointCount);
            gammaBase = buf;
        } else {
            gammaBase = L"PQ";
        }
    } else {
        if (hasAdjustedPoints) {
            wchar_t buf[32];
            swprintf_s(buf, L"Custom (%dpt)", mhc.baseGrayscale.pointCount);
            gammaBase = buf;
        } else if (mhc.baseGrayscale.use24Gamma) {
            gammaBase = L"2.2\u21922.4";
        } else {
            gammaBase = L"2.2";
        }
    }
    if (mhc.hasPerChannelTRC) {
        gammaBase += L" + TRC";
    }

    // --- White balance label ---
    if (mhc.whiteBalanceEnabled) {
        float wx = mhc.whiteBalanceWx, wy = mhc.whiteBalanceWy;
        if (fabsf(wx - 0.3127f) < 0.001f && fabsf(wy - 0.3290f) < 0.001f) {
            mhc.metaWhiteBalance = L"D65";
        } else {
            wchar_t wbBuf[48];
            _swprintf_s_l(wbBuf, _countof(wbBuf), L"Custom (%.4f, %.4f)", GetCLocale(), wx, wy);
            mhc.metaWhiteBalance = wbBuf;
        }
    } else {
        mhc.metaWhiteBalance.clear();
    }

    // Append desktop gamma and correction grayscale to gamma label
    if (isHDR && mhc.desktopGammaEnabled) {
        gammaBase += L" + DG";
    }
    if (mhc.correctionGrayscale.enabled) {
        gammaBase += L" + Corr";
    }
    mhc.metaGamma = gammaBase;

    // --- Peak nits (HDR only) ---
    if (isHDR) {
        mhc.metaPeakNits = mhc.baseGrayscale.peakNits;
    } else {
        mhc.metaPeakNits = 0.0f;
    }
}

// ============================================================================
// SECTION: MHC Profile Generation & Installation
// ============================================================================

// Build MHC2ProfileParams from current MHCSettings (shared by Generate and Regenerate)
static void BuildMHC2Params(const MHCSettings& mhc, bool isHDR, int monitorIndex, MHC2ProfileParams& params) {
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
        } else if (!isHDR) {
            // SDR ICC profile: use per-channel TRC for correction (shape reliable per research)
            // TRC inverted against target gamma (2.2/2.4)
            // HDR ICC TRC disabled: DisplayCal's sparse measurements + curve fitting produce
            // shadow corrections 10-20x too aggressive vs ColourSpace ground truth
            ICCProfileData icc;
            if (ReadICCProfile(mhc.sourceFilePath, icc) && icc.hasTRC) {
                params.hasPerChannelTRC = true;
                params.trcR = icc.trcR;
                params.trcG = icc.trcG;
                params.trcB = icc.trcB;
                params.grayscaleEnabled = true;
                params.grayscale.enabled = true;
                params.grayscale.use24Gamma = mhc.baseGrayscale.use24Gamma;
            }
        }
        // Peak nits is display metadata, needed regardless of file type
        params.peakNits = mhc.baseGrayscale.peakNits;
    } else if (mhc.baseGrayscale.enabled) {
        // Safety: if points are empty (e.g., dialog set enabled=true without init), use identity
        if (mhc.baseGrayscale.points.empty()) {
            params.grayscaleEnabled = false;
            params.grayscale.enabled = false;
        } else {
            params.grayscaleEnabled = true;
            params.grayscale.enabled = true;
            params.grayscale.pointCount = mhc.baseGrayscale.pointCount;
            for (int i = 0; i < mhc.baseGrayscale.pointCount && i < 32; i++) {
                params.grayscale.points[i] = (i < (int)mhc.baseGrayscale.points.size())
                    ? mhc.baseGrayscale.points[i] : 0.0f;
                // Compute per-channel values from base * deviation
                float base = params.grayscale.points[i];
                float devR = (i < (int)mhc.baseGrayscale.rgbDeviations[0].size()) ? mhc.baseGrayscale.rgbDeviations[0][i] : 1.0f;
                float devG = (i < (int)mhc.baseGrayscale.rgbDeviations[1].size()) ? mhc.baseGrayscale.rgbDeviations[1][i] : 1.0f;
                float devB = (i < (int)mhc.baseGrayscale.rgbDeviations[2].size()) ? mhc.baseGrayscale.rgbDeviations[2][i] : 1.0f;
                params.grayscale.pointsR[i] = base * devR;
                params.grayscale.pointsG[i] = base * devG;
                params.grayscale.pointsB[i] = base * devB;
            }
            params.grayscale.use24Gamma = mhc.baseGrayscale.use24Gamma;
            params.grayscale.peakNits = mhc.baseGrayscale.peakNits;
            params.peakNits = mhc.baseGrayscale.peakNits;
        }
    }

    // White balance gains (von Kries in wire RGB space)
    if (mhc.whiteBalanceEnabled) {
        float wx = mhc.whiteBalanceWx, wy = mhc.whiteBalanceWy;
        // Check if not D65 (within tolerance)
        bool isD65 = (fabsf(wx - 0.3127f) < 0.001f && fabsf(wy - 0.3290f) < 0.001f);
        if (!isD65 && wy > 0.001f) {
            // Convert target white from CIE xy to XYZ (Y=1)
            float tX = wx / wy;
            float tY = 1.0f;
            float tZ = (1.0f - wx - wy) / wy;
            // XYZ→RGB matrix for wire space
            static const float rec2020XYZtoRGB[9] = {
                 1.7166512f, -0.3556708f, -0.2533663f,
                -0.6666844f,  1.6164812f,  0.0157685f,
                 0.0176399f, -0.0427706f,  0.9421031f };
            static const float srgbXYZtoRGB[9] = {
                 3.2404542f, -1.5371385f, -0.4985314f,
                -0.9692660f,  1.8760108f,  0.0415560f,
                 0.0556434f, -0.2040259f,  1.0572252f };
            const float* m = isHDR ? rec2020XYZtoRGB : srgbXYZtoRGB;
            params.whiteBalanceGains[0] = m[0]*tX + m[1]*tY + m[2]*tZ;
            params.whiteBalanceGains[1] = m[3]*tX + m[4]*tY + m[5]*tZ;
            params.whiteBalanceGains[2] = m[6]*tX + m[7]*tY + m[8]*tZ;
        }
    }

    // Desktop gamma (HDR only): sRGB→2.2 baked into 1D LUT
    if (isHDR && mhc.desktopGammaEnabled) {
        params.desktopGammaEnabled = true;
    }

    // Correction grayscale (fine-tuning on top of base)
    if (mhc.correctionGrayscale.enabled && !mhc.correctionGrayscale.points.empty()) {
        params.correctionGrayscaleEnabled = true;
        params.correctionGrayscale.enabled = true;
        params.correctionGrayscale.pointCount = mhc.correctionGrayscale.pointCount;
        for (int i = 0; i < mhc.correctionGrayscale.pointCount && i < 32; i++) {
            params.correctionGrayscale.points[i] = (i < (int)mhc.correctionGrayscale.points.size())
                ? mhc.correctionGrayscale.points[i] : 0.0f;
            float base = params.correctionGrayscale.points[i];
            float devR = (i < (int)mhc.correctionGrayscale.rgbDeviations[0].size()) ? mhc.correctionGrayscale.rgbDeviations[0][i] : 1.0f;
            float devG = (i < (int)mhc.correctionGrayscale.rgbDeviations[1].size()) ? mhc.correctionGrayscale.rgbDeviations[1][i] : 1.0f;
            float devB = (i < (int)mhc.correctionGrayscale.rgbDeviations[2].size()) ? mhc.correctionGrayscale.rgbDeviations[2][i] : 1.0f;
            params.correctionGrayscale.pointsR[i] = base * devR;
            params.correctionGrayscale.pointsG[i] = base * devG;
            params.correctionGrayscale.pointsB[i] = base * devB;
        }
        params.correctionGrayscale.use24Gamma = mhc.correctionGrayscale.use24Gamma;
        params.correctionGrayscale.peakNits = mhc.correctionGrayscale.peakNits;
    }
}

// ============================================================================
// SECTION: Permutation Profile System
// ============================================================================

// Build MHC2ProfileParams for a specific permutation bitmask.
// Starts from the full MHCSettings, then disables corrections not in the bitmask.
static void BuildMHC2ParamsForPerm(const MHCSettings& mhc, bool isHDR, int monitorIndex,
                                    uint8_t perm, MHC2ProfileParams& params) {
    BuildMHC2Params(mhc, isHDR, monitorIndex, params);

    // Disable white balance if PERM_WB bit is not set
    if (!(perm & MHCSettings::PERM_WB)) {
        params.whiteBalanceGains[0] = 1.0f;
        params.whiteBalanceGains[1] = 1.0f;
        params.whiteBalanceGains[2] = 1.0f;
    }

    // Disable desktop gamma if PERM_DG bit is not set (or not HDR)
    if (!(perm & MHCSettings::PERM_DG) || !isHDR) {
        params.desktopGammaEnabled = false;
    }

    // Disable correction grayscale if PERM_GS bit is not set
    if (!(perm & MHCSettings::PERM_GS)) {
        params.correctionGrayscaleEnabled = false;
        params.correctionGrayscale.enabled = false;
    }
}

uint8_t ComputeMhcPermutation(const MHCSettings& mhc, bool isHDR) {
    uint8_t perm = 0;

    // WB: enabled AND not D65 (within tolerance) AND meaningful gains
    if (mhc.whiteBalanceEnabled) {
        bool isD65 = (fabsf(mhc.whiteBalanceWx - 0.3127f) < 0.001f &&
                      fabsf(mhc.whiteBalanceWy - 0.3290f) < 0.001f);
        if (!isD65 && mhc.whiteBalanceWy > 0.001f)
            perm |= MHCSettings::PERM_WB;
    }

    // DG: HDR only
    if (isHDR && mhc.desktopGammaEnabled)
        perm |= MHCSettings::PERM_DG;

    // GS: correction grayscale with actual data
    if (mhc.correctionGrayscale.enabled && !mhc.correctionGrayscale.points.empty())
        perm |= MHCSettings::PERM_GS;

    return perm;
}

// Delete all cached permutation profiles from disk (except the active one if keepActive=true)
static void ClearPermCache(MHCSettings& mhc, int monitorIndex, bool isHDR, bool keepActive) {
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring colorDir = std::wstring(sysDir) + L"\\spool\\drivers\\color\\";

    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
        if (keepActive && k == (int)mhc.activePerm) continue;
        if (!mhc.permNames[k].empty()) {
            DeleteFileW((colorDir + mhc.permNames[k]).c_str());
            mhc.permNames[k].clear();
            mhc.permPaths[k].clear();
        }
    }
}

bool EnsureMhcPermProfile(int monitorIndex, bool isHDR, uint8_t perm) {
    if (!IsMHC2ApiAvailable()) return false;

    // Check cache — fast path under lock
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
        const auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                                 : g_gui.monitorSettings[monitorIndex].sdrMHC;
        if (!mhc.permNames[perm].empty()) {
            // Verify file still exists on disk
            if (GetFileAttributesW(mhc.permPaths[perm].c_str()) != INVALID_FILE_ATTRIBUTES)
                return true;
            // File missing — fall through to generate
        }
    }

    // Snapshot settings for generation (release lock during I/O)
    MHCSettings mhcCopy;
    std::wstring currentActiveName;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        const auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                                 : g_gui.monitorSettings[monitorIndex].sdrMHC;
        mhcCopy = mhc;
        currentActiveName = mhc.profileName;
    }

    // Build params for the specific permutation
    MHC2ProfileParams params;
    BuildMHC2ParamsForPerm(mhcCopy, isHDR, monitorIndex, perm, params);

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return false;

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return false;

    // Unique filename encoding the permutation
    wchar_t monTag[8];
    swprintf_s(monTag, L"Mon%d", monitorIndex);
    std::wstring profileName = L"DesktopLUT_" + std::wstring(monTag)
        + L"_" + (isHDR ? L"HDR" : L"SDR") + L"_P" + std::to_wstring(perm)
        + L"_" + std::to_wstring(GetTickCount64()) + L".icm";

    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + profileName;

    if (!WriteMHC2Profile(profileData, tempPath)) return false;

    // Install to system color dir (disassociated — not the active profile)
    if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
        DeleteFileW(tempPath.c_str());
        return false;
    }
    // Immediately disassociate — we only want it in the color dir, ready for swap
    RemoveMHC2Profile(profileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
    // Re-associate the currently active profile (Install may have switched it)
    if (!currentActiveName.empty()) {
        ReassociateMHC2Profile(currentActiveName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
    }
    DeleteFileW(tempPath.c_str());

    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring profilePath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    // Store in cache
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                           : g_gui.monitorSettings[monitorIndex].sdrMHC;
        mhc.permNames[perm] = profileName;
        mhc.permPaths[perm] = profilePath;
    }

    std::cout << "MHC perm: generated P" << (int)perm << " for monitor " << monitorIndex
              << (isHDR ? " HDR" : " SDR") << std::endl;
    return true;
}

bool SwapMhcToPermutation(int monitorIndex, bool isHDR, uint8_t newPerm) {
    // Ensure the target variant exists
    if (!EnsureMhcPermProfile(monitorIndex, isHDR, newPerm)) return false;

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return false;

    std::wstring oldName, newName, newPath;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
        auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                           : g_gui.monitorSettings[monitorIndex].sdrMHC;
        if (newPerm == mhc.activePerm) return true;  // already active
        oldName = mhc.profileName;
        newName = mhc.permNames[newPerm];
        newPath = mhc.permPaths[newPerm];
    }

    if (newName.empty()) return false;

    // Swap ICC profiles via Windows Color Management
    RemoveMHC2Profile(oldName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
    ReassociateMHC2Profile(newName, displayInfo.adapterId, displayInfo.sourceId, isHDR);

    // Update state
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                           : g_gui.monitorSettings[monitorIndex].sdrMHC;
        mhc.activePerm = newPerm;
        mhc.profileName = newName;
        mhc.profilePath = newPath;
    }

    UpdateMhcFlagsLive(monitorIndex);
    std::cout << "MHC perm: swapped to P" << (int)newPerm << " for monitor " << monitorIndex
              << (isHDR ? " HDR" : " SDR") << std::endl;
    return true;
}

// ----------------------------------------------------------------------------
// SDR grayscale FULL-PREVIEW scanout support (realization A — see
// CODEX_PREVIEW_BAKE_PROMPT.md). During the SDR correction-grayscale live edit we
// neutralize scanout to IDENTITY (via a transient passthrough profile) and have the
// overlay shader reproduce the WHOLE MHC2 transform + the live correction, so the
// preview is bit-identical to the bake (including per-channel / white-balance).
// ----------------------------------------------------------------------------

// Compute the matrix + base 1D LUT the full-preview shader must reproduce, for the
// PERM_GS-stripped SDR perm (corrGS off — the shader applies it live). SDR only.
bool ComputeSdrPreviewScanout(int monitorIndex, uint8_t strippedPerm,
                              float outResult9[9],
                              std::vector<float>& outBaseLutR,
                              std::vector<float>& outBaseLutG,
                              std::vector<float>& outBaseLutB) {
    MHCSettings mhcCopy;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
        mhcCopy = g_gui.monitorSettings[monitorIndex].sdrMHC;
    }
    MHC2ProfileParams params;
    BuildMHC2ParamsForPerm(mhcCopy, /*isHDR=*/false, monitorIndex, strippedPerm, params);
    return ComputeSdrScanoutForShader(params, outResult9, outBaseLutR, outBaseLutG, outBaseLutB);
}

// Install + associate a transient identity (passthrough) SDR MHC profile so scanout
// contributes IDENTITY during the full-preview. Returns the installed profile name (for
// teardown) or empty on failure. The real profile name in MHCSettings is left untouched
// so FinishGsLive's RegenerateMhcIfActive can re-associate the real profile on exit.
std::wstring EngageSdrPassthroughScanout(int monitorIndex) {
    if (!IsMHC2ApiAvailable()) return L"";
    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return L"";

    // All-default SDR params ⇒ primaries off (identity matrix) + grayscale off (identity 1D LUT)
    // + WB off + corrGS off ⇒ a true passthrough profile.
    MHC2ProfileParams params;
    params.monitorName = (monitorIndex < (int)g_gui.monitorNames.size())
        ? g_gui.monitorNames[monitorIndex] : L"Monitor";
    params.isHDR = false;

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return L"";

    wchar_t monTag[8];
    swprintf_s(monTag, L"Mon%d", monitorIndex);
    std::wstring profileName = L"DesktopLUT_" + std::wstring(monTag)
        + L"_SDR_Passthru_" + std::to_wstring(GetTickCount64()) + L".icm";
    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + profileName;
    if (!WriteMHC2Profile(profileData, tempPath)) return L"";

    // InstallMHC2Profile copies to the system color dir AND associates as the active default.
    if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, false)) {
        DeleteFileW(tempPath.c_str());
        return L"";
    }
    DeleteFileW(tempPath.c_str());
    std::cout << "MHC: engaged SDR passthrough scanout for monitor " << monitorIndex << std::endl;
    return profileName;
}

// Disassociate + delete the transient passthrough profile. Scanout is restored to the real
// profile separately (RegenerateMhcIfActive re-associates it). Safe to call with empty name.
void DisengageSdrPassthroughScanout(int monitorIndex, const std::wstring& passthroughName) {
    if (passthroughName.empty()) return;
    DisplayInfo displayInfo;
    if (GetDisplayInfoForMonitor(monitorIndex, displayInfo)) {
        RemoveMHC2Profile(passthroughName, displayInfo.adapterId, displayInfo.sourceId, false);
    }
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    DeleteFileW((std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + passthroughName).c_str());
    std::cout << "MHC: disengaged SDR passthrough scanout for monitor " << monitorIndex << std::endl;
}

void SwapDgForAllMonitors(bool dgEnabled) {
    // First pass: auto-generate identity MHC profiles for monitors that have
    // DG enabled in settings but no MHC profile yet (DG needs MHC to carry it)
    if (dgEnabled) {
        std::vector<int> needGenerate;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            for (int i = 0; i < (int)g_gui.monitorSettings.size(); i++) {
                auto& mhc = g_gui.monitorSettings[i].hdrMHC;
                if (!mhc.enabled && mhc.desktopGammaEnabled)
                    needGenerate.push_back(i);
            }
        }
        for (int i : needGenerate) {
            GenerateAndInstallMhcProfile(i, true);
        }
    }

    // Second pass: swap permutations for all monitors with active MHC profiles
    int numMonitors;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        numMonitors = (int)g_gui.monitorSettings.size();
    }

    for (int i = 0; i < numMonitors; i++) {
        bool needSwap = false;
        uint8_t newPerm = 0;
        {
            std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
            if (i >= (int)g_gui.monitorSettings.size()) break;
            auto& mhc = g_gui.monitorSettings[i].hdrMHC;
            if (mhc.profileName.empty() || !mhc.enabled) continue;

            newPerm = mhc.activePerm;
            if (dgEnabled) newPerm |= MHCSettings::PERM_DG;
            else           newPerm &= ~MHCSettings::PERM_DG;

            if (newPerm != mhc.activePerm) needSwap = true;
        }

        if (needSwap) {
            SwapMhcToPermutation(i, true, newPerm);
        }
    }
}

// Generate, write, and install MHC2 ICC profile from current MHCSettings
// Updates mhc.enabled/profilePath/profileName on success, calls UpdateMhcFlagsLive
// Returns true if profile was generated and installed successfully
bool GenerateAndInstallMhcProfile(int monitorIndex, bool isHDR) {
    if (!IsMHC2ApiAvailable()) return false;

    // Snapshot settings under lock — this runs off the GUI thread too (e.g. via
    // SwapDgForAllMonitors on the whitelist/render thread), so we must not hold a live
    // reference into g_gui.monitorSettings across the I/O below: the GUI thread can
    // reassign the wstring fields or resize the vector (WM_DISPLAYCHANGE) concurrently.
    MHCSettings mhcCopy;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
        mhcCopy = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                        : g_gui.monitorSettings[monitorIndex].sdrMHC;
    }

    MHC2ProfileParams params;
    BuildMHC2Params(mhcCopy, isHDR, monitorIndex, params);

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return false;

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return false;

    // Use unique filename each time to bypass Windows profile caching
    wchar_t monTag[8];
    swprintf_s(monTag, L"Mon%d", monitorIndex);
    std::wstring profileName = L"DesktopLUT_" + std::wstring(monTag)
        + L"_" + (isHDR ? L"HDR" : L"SDR") + L"_" + std::to_wstring(GetTickCount64()) + L".icm";

    // Write to temp directory - InstallColorProfileW copies to system color dir
    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + profileName;

    // Save old profile info for rollback if new install fails
    std::wstring oldProfileName = (mhcCopy.enabled && !mhcCopy.profileName.empty()) ? mhcCopy.profileName : L"";

    if (!WriteMHC2Profile(profileData, tempPath)) return false;

    // Remove old profile AFTER new one is written and ready to install
    if (!oldProfileName.empty()) {
        RemoveMHC2Profile(oldProfileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
    }

    if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
        DeleteFileW(tempPath.c_str());
        // Rollback: re-associate old profile if it still exists in system color dir
        if (!oldProfileName.empty()) {
            ReassociateMHC2Profile(oldProfileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        }
        return false;
    }
    DeleteFileW(tempPath.c_str());

    // Clean up old profile file only after new profile is confirmed installed
    if (!oldProfileName.empty()) {
        wchar_t sysDir[MAX_PATH];
        GetSystemDirectory(sysDir, MAX_PATH);
        std::wstring oldPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + oldProfileName;
        DeleteFileW(oldPath.c_str());
    }

    // Store the system color directory path
    wchar_t sysDir[MAX_PATH];
    GetSystemDirectory(sysDir, MAX_PATH);
    std::wstring profilePath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + profileName;

    // Compute the active permutation from current settings
    uint8_t perm = ComputeMhcPermutation(mhcCopy, isHDR);

    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return false;
        auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                          : g_gui.monitorSettings[monitorIndex].sdrMHC;
        // Clear all old cached permutation profiles (base data changed)
        ClearPermCache(mhc, monitorIndex, isHDR, false);
        mhc.enabled = true;
        mhc.profilePath = profilePath;
        mhc.profileName = profileName;
        mhc.hasPerChannelTRC = params.hasPerChannelTRC || params.hasPrecomputedCorrection;
        mhc.activePerm = perm;
        mhc.permNames[perm] = profileName;
        mhc.permPaths[perm] = profilePath;
    }
    UpdateMhcFlagsLive(monitorIndex);
    return true;
}

// Auto-regenerate and reinstall MHC profile when MHC settings change
// Only acts if MHC is enabled and a profile is already installed for the given mode
void RegenerateMhcIfActive(int monitorIndex, bool isHDR) {
    if (!IsMHC2ApiAvailable()) return;

    // Snapshot under lock (see GenerateAndInstallMhcProfile — reachable off the GUI thread).
    MHCSettings mhcCopy;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
        mhcCopy = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                        : g_gui.monitorSettings[monitorIndex].sdrMHC;
    }
    // Only require a profile name — editing inline corrections re-enables MHC automatically.
    // An existing profileName means Apply was run at some point and a profile file exists.
    if (mhcCopy.profileName.empty()) return;

    MHC2ProfileParams params;
    BuildMHC2Params(mhcCopy, isHDR, monitorIndex, params);

    std::vector<uint8_t> profileData;
    if (!GenerateMHC2Profile(params, profileData)) return;

    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) return;

    // Unique filename to bypass caching
    wchar_t monTag[8];
    swprintf_s(monTag, L"Mon%d", monitorIndex);
    std::wstring newProfileName = L"DesktopLUT_" + std::wstring(monTag)
        + L"_" + (isHDR ? L"HDR" : L"SDR") + L"_" + std::to_wstring(GetTickCount64()) + L".icm";

    wchar_t tempDir[MAX_PATH];
    GetTempPathW(MAX_PATH, tempDir);
    std::wstring tempPath = std::wstring(tempDir) + newProfileName;

    // Save old profile info for rollback if new install fails
    std::wstring oldProfileName = mhcCopy.profileName;

    if (!WriteMHC2Profile(profileData, tempPath)) return;

    // Remove old profile AFTER new one is written and ready to install
    RemoveMHC2Profile(oldProfileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);

    if (!InstallMHC2Profile(tempPath, displayInfo.adapterId, displayInfo.sourceId, isHDR)) {
        std::cerr << "RegenerateMhcIfActive: InstallMHC2Profile failed for monitor "
                  << monitorIndex << (isHDR ? " HDR" : " SDR") << std::endl;
        DeleteFileW(tempPath.c_str());
        // Rollback: re-associate old profile if it still exists
        if (!oldProfileName.empty()) {
            ReassociateMHC2Profile(oldProfileName, displayInfo.adapterId, displayInfo.sourceId, isHDR);
        }
        return;
    }
    DeleteFileW(tempPath.c_str());

    // Clean up old profile file only after new one is confirmed installed
    if (!oldProfileName.empty()) {
        wchar_t sysDir[MAX_PATH];
        GetSystemDirectory(sysDir, MAX_PATH);
        std::wstring oldPath = std::wstring(sysDir) + L"\\spool\\drivers\\color\\" + oldProfileName;
        DeleteFileW(oldPath.c_str());
    }

    // Recompute active permutation (corrections may have changed)
    uint8_t perm = ComputeMhcPermutation(mhcCopy, isHDR);

    // Update stored name — profile is now active, ensure enabled is true
    wchar_t sysDir2[MAX_PATH];
    GetSystemDirectory(sysDir2, MAX_PATH);
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitorSettings.size()) return;
        auto& mhc = isHDR ? g_gui.monitorSettings[monitorIndex].hdrMHC
                          : g_gui.monitorSettings[monitorIndex].sdrMHC;
        // Clear all cached permutation profiles (base data changed, variants are stale)
        ClearPermCache(mhc, monitorIndex, isHDR, false);
        mhc.enabled = true;
        mhc.profilePath = std::wstring(sysDir2) + L"\\spool\\drivers\\color\\" + newProfileName;
        mhc.profileName = newProfileName;
        mhc.hasPerChannelTRC = params.hasPerChannelTRC || params.hasPrecomputedCorrection;
        mhc.activePerm = perm;
        mhc.permNames[perm] = newProfileName;
        mhc.permPaths[perm] = mhc.profilePath;
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
                _swprintf_s_l(buf, _countof(buf), L"%.4f", GetCLocale(), vals[i]);
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
        _swprintf_s_l(peakBuf, _countof(peakBuf), L"Peak: %.0f nits", GetCLocale(), mhc.metaPeakNits);
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
    } else {
        auto& cc = isHDR ? g_gui.monitorSettings[g_gui.currentMonitor].hdrColorCorrection
                         : g_gui.monitorSettings[g_gui.currentMonitor].sdrColorCorrection;
        if (cc.primariesEnabled) {
            StartProcessing();
        }
    }
    UpdateGUIState();
}
