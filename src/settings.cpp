// DesktopLUT - settings.cpp
// INI settings persistence

#include "settings.h"
#include "globals.h"
#include <cwchar>
#include <cmath>
#include <iostream>
#include <locale.h>

// Cached C locale for locale-independent float parsing/writing.
// _wtof and swprintf_s use the thread locale which may use comma as decimal
// separator on European systems, breaking INI round-trips.
_locale_t GetCLocale() {
    static _locale_t loc = _create_locale(LC_ALL, "C");
    return loc;
}

std::wstring GetIniPath() {
    wchar_t exePath[MAX_PATH];
    GetModuleFileNameW(nullptr, exePath, MAX_PATH);
    std::wstring path(exePath);
    size_t lastSlash = path.find_last_of(L"\\/");
    if (lastSlash != std::wstring::npos) {
        path = path.substr(0, lastSlash + 1);
    }
    return path + L"DesktopLUT.ini";
}

void WritePrivateProfileFloat(const wchar_t* section, const wchar_t* key, float value, const wchar_t* file) {
    wchar_t buf[32];
    _swprintf_s_l(buf, _countof(buf), L"%.4f", GetCLocale(), value);
    WritePrivateProfileStringW(section, key, buf, file);
}

float GetPrivateProfileFloat(const wchar_t* section, const wchar_t* key, float def, const wchar_t* file) {
    wchar_t buf[32] = {};
    GetPrivateProfileStringW(section, key, L"", buf, 32, file);
    if (buf[0] == L'\0') return def;
    return (float)_wcstod_l(buf, nullptr, GetCLocale());
}

void WritePrivateProfileBool(const wchar_t* section, const wchar_t* key, bool value, const wchar_t* file) {
    WritePrivateProfileStringW(section, key, value ? L"true" : L"false", file);
}

bool GetPrivateProfileBool(const wchar_t* section, const wchar_t* key, bool def, const wchar_t* file) {
    wchar_t buf[16] = {};
    GetPrivateProfileStringW(section, key, L"", buf, 16, file);
    if (buf[0] == L'\0') return def;
    // Accept "true", "1", "yes" as true; "false", "0", "no" as false (case-insensitive)
    if (_wcsicmp(buf, L"true") == 0 || wcscmp(buf, L"1") == 0 || _wcsicmp(buf, L"yes") == 0)
        return true;
    if (_wcsicmp(buf, L"false") == 0 || wcscmp(buf, L"0") == 0 || _wcsicmp(buf, L"no") == 0)
        return false;
    return def;
}

void WritePrivateProfileXY(const wchar_t* section, const wchar_t* key, float x, float y, const wchar_t* file) {
    wchar_t buf[64];
    _swprintf_s_l(buf, _countof(buf), L"%.4f, %.4f", GetCLocale(), x, y);
    WritePrivateProfileStringW(section, key, buf, file);
}

bool GetPrivateProfileXY(const wchar_t* section, const wchar_t* key, float& x, float& y, const wchar_t* file) {
    wchar_t buf[64] = {};
    GetPrivateProfileStringW(section, key, L"", buf, 64, file);
    if (buf[0] == L'\0') return false;
    // Parse "x, y" format
    wchar_t* comma = wcschr(buf, L',');
    if (!comma) return false;
    *comma = L'\0';
    x = (float)_wcstod_l(buf, nullptr, GetCLocale());
    y = (float)_wcstod_l(comma + 1, nullptr, GetCLocale());
    // Validate: chromaticity coords must be in [0,1] with y > 0 (avoid div-by-zero in Bradford)
    if (x < 0.0f || x > 1.0f || y < 0.001f || y > 1.0f) return false;
    return true;
}

const wchar_t* TonemapCurveToString(TonemapCurve curve) {
    switch (curve) {
        case TonemapCurve::BT2390:   return L"BT2390";
        case TonemapCurve::SoftClip: return L"SoftClip";
        case TonemapCurve::Reinhard: return L"Reinhard";
        case TonemapCurve::BT2446A:  return L"BT2446A";
        case TonemapCurve::HardClip: return L"HardClip";
        default:                     return L"BT2390";
    }
}

TonemapCurve StringToTonemapCurve(const wchar_t* str) {
    if (_wcsicmp(str, L"BT2390") == 0)   return TonemapCurve::BT2390;
    if (_wcsicmp(str, L"SoftClip") == 0) return TonemapCurve::SoftClip;
    if (_wcsicmp(str, L"Reinhard") == 0) return TonemapCurve::Reinhard;
    if (_wcsicmp(str, L"BT2446A") == 0)  return TonemapCurve::BT2446A;
    if (_wcsicmp(str, L"HardClip") == 0) return TonemapCurve::HardClip;
    return TonemapCurve::BT2390;
}

void SaveColorCorrectionSettings(const wchar_t* section, const wchar_t* prefix,
                                  const ColorCorrectionSettings& cc, const wchar_t* iniPath) {
    std::wstring p(prefix);
    // Primaries, grayscale, white balance, and desktop gamma are now in MHC settings.
    // Only tonemapping remains as a shader-level correction.
    bool isHDR = (p.find(L"HDR") != std::wstring::npos);
    if (isHDR) {
        WritePrivateProfileBool(section, (p + L"TonemapEnabled").c_str(), cc.tonemap.enabled, iniPath);
        WritePrivateProfileStringW(section, (p + L"TonemapCurve").c_str(),
            TonemapCurveToString(cc.tonemap.curve), iniPath);
        WritePrivateProfileFloat(section, (p + L"TonemapSourcePeak").c_str(), cc.tonemap.sourcePeakNits, iniPath);
        WritePrivateProfileFloat(section, (p + L"TonemapTargetPeak").c_str(), cc.tonemap.targetPeakNits, iniPath);
        WritePrivateProfileBool(section, (p + L"TonemapDynamic").c_str(), cc.tonemap.dynamicPeak, iniPath);
    }
}

void LoadColorCorrectionSettings(const wchar_t* section, const wchar_t* prefix,
                                  ColorCorrectionSettings& cc, const wchar_t* iniPath) {
    std::wstring p(prefix);
    // Primaries, grayscale, white balance, and desktop gamma are now in MHC settings.
    // Only tonemapping remains as a shader-level correction.
    bool isHDR = (p.find(L"HDR") != std::wstring::npos);
    if (isHDR) {
        cc.tonemap.enabled = GetPrivateProfileBool(section, (p + L"TonemapEnabled").c_str(), false, iniPath);
        wchar_t curveBuf[32] = {};
        GetPrivateProfileStringW(section, (p + L"TonemapCurve").c_str(), L"BT2390", curveBuf, 32, iniPath);
        cc.tonemap.curve = StringToTonemapCurve(curveBuf);
        float srcPeak = GetPrivateProfileFloat(section, (p + L"TonemapSourcePeak").c_str(), 10000.0f, iniPath);
        float tgtPeak = GetPrivateProfileFloat(section, (p + L"TonemapTargetPeak").c_str(), 1000.0f, iniPath);
        cc.tonemap.sourcePeakNits = (srcPeak >= 10.0f && srcPeak <= 10000.0f) ? srcPeak : 10000.0f;
        cc.tonemap.targetPeakNits = (tgtPeak >= 10.0f && tgtPeak <= 10000.0f) ? tgtPeak : 1000.0f;
        cc.tonemap.dynamicPeak = GetPrivateProfileBool(section, (p + L"TonemapDynamic").c_str(), false, iniPath);
    }
}

void SaveMHCSettings(const wchar_t* section, const wchar_t* prefix,
                      const MHCSettings& mhc, const wchar_t* iniPath) {
    std::wstring p(prefix);
    WritePrivateProfileBool(section, (p + L"MHCEnabled").c_str(), mhc.enabled, iniPath);
    WritePrivateProfileStringW(section, (p + L"MHCProfilePath").c_str(), mhc.profilePath.c_str(), iniPath);
    WritePrivateProfileStringW(section, (p + L"MHCSourceFile").c_str(), mhc.sourceFilePath.c_str(), iniPath);
    WritePrivateProfileBool(section, (p + L"MHCSourceIs1DCube").c_str(), mhc.sourceIs1DCube, iniPath);
    WritePrivateProfileBool(section, (p + L"MHCPerChannelTRC").c_str(), mhc.hasPerChannelTRC, iniPath);

    // MHC's own primaries settings
    WritePrivateProfileBool(section, (p + L"MHCPrimariesEnabled").c_str(), mhc.primariesEnabled, iniPath);
    wchar_t presetBuf[8];
    swprintf_s(presetBuf, L"%d", mhc.primariesPreset);
    WritePrivateProfileStringW(section, (p + L"MHCPrimariesPreset").c_str(), presetBuf, iniPath);
    WritePrivateProfileXY(section, (p + L"MHCPrimariesRed").c_str(),
        mhc.customPrimaries.Rx, mhc.customPrimaries.Ry, iniPath);
    WritePrivateProfileXY(section, (p + L"MHCPrimariesGreen").c_str(),
        mhc.customPrimaries.Gx, mhc.customPrimaries.Gy, iniPath);
    WritePrivateProfileXY(section, (p + L"MHCPrimariesBlue").c_str(),
        mhc.customPrimaries.Bx, mhc.customPrimaries.By, iniPath);
    WritePrivateProfileXY(section, (p + L"MHCPrimariesWhite").c_str(),
        mhc.customPrimaries.Wx, mhc.customPrimaries.Wy, iniPath);

    // MHC's own grayscale settings
    WritePrivateProfileBool(section, (p + L"MHCGrayscaleEnabled").c_str(), mhc.baseGrayscale.enabled, iniPath);
    wchar_t pointsBuf[8];
    swprintf_s(pointsBuf, L"%d", mhc.baseGrayscale.pointCount);
    WritePrivateProfileStringW(section, (p + L"MHCGrayscalePoints").c_str(), pointsBuf, iniPath);

    std::wstring grayscaleData;
    for (size_t j = 0; j < mhc.baseGrayscale.points.size(); j++) {
        wchar_t val[16];
        _swprintf_s_l(val, _countof(val), L"%.4f", GetCLocale(), mhc.baseGrayscale.points[j]);
        if (j > 0) grayscaleData += L"; ";
        grayscaleData += val;
    }
    WritePrivateProfileStringW(section, (p + L"MHCGrayscaleData").c_str(), grayscaleData.c_str(), iniPath);

    // Save per-channel RGB deviations for MHC grayscale
    {
        const wchar_t* devSuffix[] = { L"MHCGrayscaleDevR", L"MHCGrayscaleDevG", L"MHCGrayscaleDevB" };
        for (int ch = 0; ch < 3; ch++) {
            auto& dev = mhc.baseGrayscale.rgbDeviations[ch];
            if (!dev.empty()) {
                std::wstring devData;
                for (size_t j = 0; j < dev.size(); j++) {
                    wchar_t val[16]; _swprintf_s_l(val, _countof(val), L"%.4f", GetCLocale(), dev[j]);
                    if (j > 0) devData += L"; ";
                    devData += val;
                }
                WritePrivateProfileStringW(section, (p + devSuffix[ch]).c_str(), devData.c_str(), iniPath);
            }
        }
    }

    bool isHDR = (p.find(L"HDR") != std::wstring::npos);
    if (isHDR) {
        WritePrivateProfileFloat(section, (p + L"MHCGrayscalePeak").c_str(), mhc.baseGrayscale.peakNits, iniPath);
    } else {
        WritePrivateProfileBool(section, (p + L"MHCGrayscale24").c_str(), mhc.baseGrayscale.use24Gamma, iniPath);
    }

    // White balance settings
    WritePrivateProfileBool(section, (p + L"MHCWhiteBalanceEnabled").c_str(), mhc.whiteBalanceEnabled, iniPath);
    WritePrivateProfileFloat(section, (p + L"MHCWhiteBalanceWx").c_str(), mhc.whiteBalanceWx, iniPath);
    WritePrivateProfileFloat(section, (p + L"MHCWhiteBalanceWy").c_str(), mhc.whiteBalanceWy, iniPath);

    // Desktop gamma (HDR only)
    if (isHDR) {
        WritePrivateProfileBool(section, (p + L"MHCDesktopGamma").c_str(), mhc.desktopGammaEnabled, iniPath);
    }

    // Permutation profile cache
    {
        wchar_t permBuf[8];
        swprintf_s(permBuf, L"%d", (int)mhc.activePerm);
        WritePrivateProfileStringW(section, (p + L"MHCActivePerm").c_str(), permBuf, iniPath);
    }
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
        std::wstring key = p + L"MHCPermPath" + std::to_wstring(k);
        WritePrivateProfileStringW(section, key.c_str(), mhc.permPaths[k].c_str(), iniPath);
    }

    // Correction grayscale (fine-tuning on top of base)
    WritePrivateProfileBool(section, (p + L"MHCCorrGSEnabled").c_str(), mhc.correctionGrayscale.enabled, iniPath);
    {
        wchar_t ptsBuf[8];
        swprintf_s(ptsBuf, L"%d", mhc.correctionGrayscale.pointCount);
        WritePrivateProfileStringW(section, (p + L"MHCCorrGSPoints").c_str(), ptsBuf, iniPath);
    }
    {
        std::wstring gsData;
        for (size_t j = 0; j < mhc.correctionGrayscale.points.size(); j++) {
            wchar_t val[16];
            _swprintf_s_l(val, _countof(val), L"%.4f", GetCLocale(), mhc.correctionGrayscale.points[j]);
            if (j > 0) gsData += L"; ";
            gsData += val;
        }
        WritePrivateProfileStringW(section, (p + L"MHCCorrGSData").c_str(), gsData.c_str(), iniPath);
    }
    {
        const wchar_t* devSuffix[] = { L"MHCCorrGSDevR", L"MHCCorrGSDevG", L"MHCCorrGSDevB" };
        for (int ch = 0; ch < 3; ch++) {
            auto& dev = mhc.correctionGrayscale.rgbDeviations[ch];
            if (!dev.empty()) {
                std::wstring devData;
                for (size_t j = 0; j < dev.size(); j++) {
                    wchar_t val[16]; _swprintf_s_l(val, _countof(val), L"%.4f", GetCLocale(), dev[j]);
                    if (j > 0) devData += L"; ";
                    devData += val;
                }
                WritePrivateProfileStringW(section, (p + devSuffix[ch]).c_str(), devData.c_str(), iniPath);
            }
        }
    }
    if (isHDR) {
        WritePrivateProfileFloat(section, (p + L"MHCCorrGSPeak").c_str(), mhc.correctionGrayscale.peakNits, iniPath);
    } else {
        WritePrivateProfileBool(section, (p + L"MHCCorrGS24").c_str(), mhc.correctionGrayscale.use24Gamma, iniPath);
    }

    // Metadata for display labels
    WritePrivateProfileStringW(section, (p + L"MHCMetaPrimaries").c_str(), mhc.metaPrimaries.c_str(), iniPath);
    WritePrivateProfileStringW(section, (p + L"MHCMetaGamma").c_str(), mhc.metaGamma.c_str(), iniPath);
    WritePrivateProfileStringW(section, (p + L"MHCMetaWhiteBalance").c_str(), mhc.metaWhiteBalance.c_str(), iniPath);
    if (isHDR) {
        WritePrivateProfileFloat(section, (p + L"MHCMetaPeakNits").c_str(), mhc.metaPeakNits, iniPath);
    }
}

void LoadMHCSettings(const wchar_t* section, const wchar_t* prefix,
                      MHCSettings& mhc, const wchar_t* iniPath) {
    std::wstring p(prefix);
    mhc.enabled = GetPrivateProfileBool(section, (p + L"MHCEnabled").c_str(), false, iniPath);

    wchar_t mhcPath[MAX_PATH] = {};
    GetPrivateProfileStringW(section, (p + L"MHCProfilePath").c_str(), L"", mhcPath, MAX_PATH, iniPath);
    mhc.profilePath = mhcPath;
    // Extract filename
    std::wstring name = mhc.profilePath;
    size_t slash = name.find_last_of(L"\\/");
    if (slash != std::wstring::npos) name = name.substr(slash + 1);
    mhc.profileName = name;

    wchar_t srcFile[MAX_PATH] = {};
    GetPrivateProfileStringW(section, (p + L"MHCSourceFile").c_str(), L"", srcFile, MAX_PATH, iniPath);
    mhc.sourceFilePath = srcFile;
    mhc.sourceIs1DCube = GetPrivateProfileBool(section, (p + L"MHCSourceIs1DCube").c_str(), false, iniPath);
    mhc.hasPerChannelTRC = GetPrivateProfileBool(section, (p + L"MHCPerChannelTRC").c_str(), false, iniPath);

    // MHC's own primaries
    mhc.primariesEnabled = GetPrivateProfileBool(section, (p + L"MHCPrimariesEnabled").c_str(), false, iniPath);
    int preset = GetPrivateProfileIntW(section, (p + L"MHCPrimariesPreset").c_str(), 0, iniPath);
    mhc.primariesPreset = (preset >= 0 && preset < g_numPresetPrimaries) ? preset : 0;

    if (!GetPrivateProfileXY(section, (p + L"MHCPrimariesRed").c_str(),
            mhc.customPrimaries.Rx, mhc.customPrimaries.Ry, iniPath)) {
        mhc.customPrimaries.Rx = 0.6400f; mhc.customPrimaries.Ry = 0.3300f;
    }
    if (!GetPrivateProfileXY(section, (p + L"MHCPrimariesGreen").c_str(),
            mhc.customPrimaries.Gx, mhc.customPrimaries.Gy, iniPath)) {
        mhc.customPrimaries.Gx = 0.3000f; mhc.customPrimaries.Gy = 0.6000f;
    }
    if (!GetPrivateProfileXY(section, (p + L"MHCPrimariesBlue").c_str(),
            mhc.customPrimaries.Bx, mhc.customPrimaries.By, iniPath)) {
        mhc.customPrimaries.Bx = 0.1500f; mhc.customPrimaries.By = 0.0600f;
    }
    if (!GetPrivateProfileXY(section, (p + L"MHCPrimariesWhite").c_str(),
            mhc.customPrimaries.Wx, mhc.customPrimaries.Wy, iniPath)) {
        mhc.customPrimaries.Wx = 0.3127f; mhc.customPrimaries.Wy = 0.3290f;
    }

    // MHC's own grayscale
    bool isHDR = (p.find(L"HDR") != std::wstring::npos);
    mhc.baseGrayscale.enabled = GetPrivateProfileBool(section, (p + L"MHCGrayscaleEnabled").c_str(), false, iniPath);
    int points = GetPrivateProfileIntW(section, (p + L"MHCGrayscalePoints").c_str(), 20, iniPath);
    mhc.baseGrayscale.pointCount = (points == 10 || points == 20 || points == 32) ? points : 20;

    wchar_t grayscaleData[1024] = {};
    GetPrivateProfileStringW(section, (p + L"MHCGrayscaleData").c_str(), L"", grayscaleData, 1024, iniPath);
    mhc.baseGrayscale.points.clear();
    if (grayscaleData[0] != L'\0') {
        wchar_t* ctx = nullptr;
        wchar_t* token = wcstok_s(grayscaleData, L";", &ctx);
        while (token) {
            while (*token == L' ' || *token == L'\t') token++;
            mhc.baseGrayscale.points.push_back((float)_wcstod_l(token, nullptr, GetCLocale()));
            token = wcstok_s(nullptr, L";", &ctx);
        }
    }
    // Reinitialize on a count mismatch OR any corrupt value (NaN/Inf/out-of-[0,1]) — a
    // hand-edited or truncated INI must not feed garbage into MHC LUT generation.
    bool gsNeedsReinit = mhc.baseGrayscale.points.empty() ||
                         (int)mhc.baseGrayscale.points.size() != mhc.baseGrayscale.pointCount;
    if (!gsNeedsReinit) {
        for (float v : mhc.baseGrayscale.points) {
            if (!std::isfinite(v) || v < 0.0f || v > 1.0f) { gsNeedsReinit = true; break; }
        }
    }
    if (gsNeedsReinit) {
        if (!mhc.baseGrayscale.points.empty()) {
            std::wcerr << L"Warning: " << section << L"/" << p
                       << L"MHCGrayscaleData invalid (count/NaN/range), reinitializing to linear"
                       << std::endl;
        }
        mhc.baseGrayscale.points.resize(mhc.baseGrayscale.pointCount);
        if (isHDR) mhc.baseGrayscale.initLinearPQ();
        else mhc.baseGrayscale.initLinear();
    }

    // Load per-channel RGB deviations for MHC grayscale
    {
        const wchar_t* devSuffix[] = { L"MHCGrayscaleDevR", L"MHCGrayscaleDevG", L"MHCGrayscaleDevB" };
        for (int ch = 0; ch < 3; ch++) {
            wchar_t devBuf[1024] = {};
            GetPrivateProfileStringW(section, (p + devSuffix[ch]).c_str(), L"", devBuf, 1024, iniPath);
            mhc.baseGrayscale.rgbDeviations[ch].clear();
            if (devBuf[0] != L'\0') {
                wchar_t* ctx2 = nullptr;
                wchar_t* token = wcstok_s(devBuf, L";", &ctx2);
                while (token) {
                    while (*token == L' ' || *token == L'\t') token++;
                    mhc.baseGrayscale.rgbDeviations[ch].push_back((float)_wcstod_l(token, nullptr, GetCLocale()));
                    token = wcstok_s(nullptr, L";", &ctx2);
                }
            }
            bool devValid = !mhc.baseGrayscale.rgbDeviations[ch].empty() &&
                            (int)mhc.baseGrayscale.rgbDeviations[ch].size() == mhc.baseGrayscale.pointCount;
            if (devValid) {
                for (float v : mhc.baseGrayscale.rgbDeviations[ch]) {
                    // Per-channel gains; reject NaN/Inf and absurd magnitudes.
                    if (!std::isfinite(v) || v < 0.0f || v > 8.0f) { devValid = false; break; }
                }
            }
            if (!devValid) {
                mhc.baseGrayscale.rgbDeviations[ch].assign(mhc.baseGrayscale.pointCount, 1.0f);
            }
        }
    }

    if (isHDR) {
        float peakNits = GetPrivateProfileFloat(section, (p + L"MHCGrayscalePeak").c_str(), 10000.0f, iniPath);
        mhc.baseGrayscale.peakNits = (peakNits >= 10.0f && peakNits <= 10000.0f) ? peakNits : 10000.0f;
    } else {
        mhc.baseGrayscale.use24Gamma = GetPrivateProfileBool(section, (p + L"MHCGrayscale24").c_str(), false, iniPath);
    }

    // White balance settings
    mhc.whiteBalanceEnabled = GetPrivateProfileBool(section, (p + L"MHCWhiteBalanceEnabled").c_str(), false, iniPath);
    mhc.whiteBalanceWx = GetPrivateProfileFloat(section, (p + L"MHCWhiteBalanceWx").c_str(), 0.3127f, iniPath);
    mhc.whiteBalanceWy = GetPrivateProfileFloat(section, (p + L"MHCWhiteBalanceWy").c_str(), 0.3290f, iniPath);
    // Chromaticity coordinates must be finite and in (0,1); a corrupt INI value would
    // otherwise flow into the von Kries white-balance matrix.
    if (!std::isfinite(mhc.whiteBalanceWx) || mhc.whiteBalanceWx <= 0.0f || mhc.whiteBalanceWx >= 1.0f)
        mhc.whiteBalanceWx = 0.3127f;
    if (!std::isfinite(mhc.whiteBalanceWy) || mhc.whiteBalanceWy <= 0.0f || mhc.whiteBalanceWy >= 1.0f)
        mhc.whiteBalanceWy = 0.3290f;

    // Desktop gamma (HDR only)
    if (isHDR) {
        mhc.desktopGammaEnabled = GetPrivateProfileBool(section, (p + L"MHCDesktopGamma").c_str(), false, iniPath);
    }

    // Permutation profile cache
    int rawPerm = GetPrivateProfileIntW(section, (p + L"MHCActivePerm").c_str(), 0, iniPath);
    mhc.activePerm = (rawPerm >= 0 && rawPerm < MHCSettings::PERM_COUNT) ? (uint8_t)rawPerm : 0;
    for (int k = 0; k < MHCSettings::PERM_COUNT; k++) {
        std::wstring key = p + L"MHCPermPath" + std::to_wstring(k);
        wchar_t permPath[MAX_PATH] = {};
        GetPrivateProfileStringW(section, key.c_str(), L"", permPath, MAX_PATH, iniPath);
        mhc.permPaths[k] = permPath;
        // Extract filename from path
        std::wstring permName = mhc.permPaths[k];
        size_t permSlash = permName.find_last_of(L"\\/");
        if (permSlash != std::wstring::npos) permName = permName.substr(permSlash + 1);
        mhc.permNames[k] = permName;
    }
    // Backward compatibility: if no permutation data but old DG path exists, migrate
    if (mhc.permNames[mhc.activePerm].empty() && !mhc.profileName.empty()) {
        // Old format: profilePath is the active profile, compute perm from settings
        uint8_t perm = 0;
        if (mhc.whiteBalanceEnabled) {
            bool isD65 = (fabsf(mhc.whiteBalanceWx - 0.3127f) < 0.001f &&
                          fabsf(mhc.whiteBalanceWy - 0.3290f) < 0.001f);
            if (!isD65 && mhc.whiteBalanceWy > 0.001f) perm |= MHCSettings::PERM_WB;
        }
        if (isHDR && mhc.desktopGammaEnabled) perm |= MHCSettings::PERM_DG;
        if (mhc.correctionGrayscale.enabled) perm |= MHCSettings::PERM_GS;
        mhc.activePerm = perm;
        mhc.permNames[perm] = mhc.profileName;
        mhc.permPaths[perm] = mhc.profilePath;
        // Migrate old DG variant if present
        wchar_t dgPath[MAX_PATH] = {};
        GetPrivateProfileStringW(section, (p + L"MHCProfilePathDG").c_str(), L"", dgPath, MAX_PATH, iniPath);
        if (dgPath[0] != L'\0') {
            uint8_t dgPerm = perm ^ MHCSettings::PERM_DG;  // opposite DG state
            std::wstring dgPathStr = dgPath;
            std::wstring dgName = dgPathStr;
            size_t dgSlash = dgName.find_last_of(L"\\/");
            if (dgSlash != std::wstring::npos) dgName = dgName.substr(dgSlash + 1);
            mhc.permNames[dgPerm] = dgName;
            mhc.permPaths[dgPerm] = dgPathStr;
        }
    }

    // Correction grayscale (fine-tuning on top of base)
    mhc.correctionGrayscale.enabled = GetPrivateProfileBool(section, (p + L"MHCCorrGSEnabled").c_str(), false, iniPath);
    int corrPts = GetPrivateProfileIntW(section, (p + L"MHCCorrGSPoints").c_str(), 20, iniPath);
    mhc.correctionGrayscale.pointCount = (corrPts == 10 || corrPts == 20 || corrPts == 32) ? corrPts : 20;

    {
        wchar_t corrGsData[1024] = {};
        GetPrivateProfileStringW(section, (p + L"MHCCorrGSData").c_str(), L"", corrGsData, 1024, iniPath);
        mhc.correctionGrayscale.points.clear();
        if (corrGsData[0] != L'\0') {
            wchar_t* ctx3 = nullptr;
            wchar_t* token = wcstok_s(corrGsData, L";", &ctx3);
            while (token) {
                while (*token == L' ' || *token == L'\t') token++;
                mhc.correctionGrayscale.points.push_back((float)_wcstod_l(token, nullptr, GetCLocale()));
                token = wcstok_s(nullptr, L";", &ctx3);
            }
        }
        if (mhc.correctionGrayscale.points.empty() || (int)mhc.correctionGrayscale.points.size() != mhc.correctionGrayscale.pointCount) {
            mhc.correctionGrayscale.points.resize(mhc.correctionGrayscale.pointCount);
            if (isHDR) mhc.correctionGrayscale.initLinearPQ();
            else mhc.correctionGrayscale.initLinear();
        }
    }

    {
        const wchar_t* devSuffix[] = { L"MHCCorrGSDevR", L"MHCCorrGSDevG", L"MHCCorrGSDevB" };
        for (int ch = 0; ch < 3; ch++) {
            wchar_t devBuf2[1024] = {};
            GetPrivateProfileStringW(section, (p + devSuffix[ch]).c_str(), L"", devBuf2, 1024, iniPath);
            mhc.correctionGrayscale.rgbDeviations[ch].clear();
            if (devBuf2[0] != L'\0') {
                wchar_t* ctx4 = nullptr;
                wchar_t* token = wcstok_s(devBuf2, L";", &ctx4);
                while (token) {
                    while (*token == L' ' || *token == L'\t') token++;
                    mhc.correctionGrayscale.rgbDeviations[ch].push_back((float)_wcstod_l(token, nullptr, GetCLocale()));
                    token = wcstok_s(nullptr, L";", &ctx4);
                }
            }
            if (mhc.correctionGrayscale.rgbDeviations[ch].empty() ||
                (int)mhc.correctionGrayscale.rgbDeviations[ch].size() != mhc.correctionGrayscale.pointCount) {
                mhc.correctionGrayscale.rgbDeviations[ch].assign(mhc.correctionGrayscale.pointCount, 1.0f);
            }
        }
    }

    if (isHDR) {
        float corrPeak = GetPrivateProfileFloat(section, (p + L"MHCCorrGSPeak").c_str(), 10000.0f, iniPath);
        mhc.correctionGrayscale.peakNits = (corrPeak >= 10.0f && corrPeak <= 10000.0f) ? corrPeak : 10000.0f;
    } else {
        mhc.correctionGrayscale.use24Gamma = GetPrivateProfileBool(section, (p + L"MHCCorrGS24").c_str(), false, iniPath);
    }

    // Metadata for display labels
    wchar_t metaBuf[256] = {};
    GetPrivateProfileStringW(section, (p + L"MHCMetaPrimaries").c_str(), L"", metaBuf, 256, iniPath);
    mhc.metaPrimaries = metaBuf;
    GetPrivateProfileStringW(section, (p + L"MHCMetaGamma").c_str(), L"", metaBuf, 256, iniPath);
    mhc.metaGamma = metaBuf;
    GetPrivateProfileStringW(section, (p + L"MHCMetaWhiteBalance").c_str(), L"", metaBuf, 256, iniPath);
    mhc.metaWhiteBalance = metaBuf;
    if (isHDR) {
        mhc.metaPeakNits = GetPrivateProfileFloat(section, (p + L"MHCMetaPeakNits").c_str(), 0.0f, iniPath);
    }
}

// Helper to parse comma-separated whitelist into vector of lowercase exe names
void ParseWhitelistString(const std::wstring& raw, std::vector<std::wstring>& out) {
    out.clear();
    if (raw.empty()) return;

    std::wstring item;
    for (wchar_t c : raw) {
        if (c == L',' || c == L';') {
            // Trim whitespace
            size_t start = item.find_first_not_of(L" \t");
            size_t end = item.find_last_not_of(L" \t");
            if (start != std::wstring::npos) {
                std::wstring trimmed = item.substr(start, end - start + 1);
                // Convert to lowercase
                for (wchar_t& ch : trimmed) {
                    ch = towlower(ch);
                }
                // Remove .exe extension if present (we'll match with and without)
                if (trimmed.size() > 4 && trimmed.substr(trimmed.size() - 4) == L".exe") {
                    trimmed = trimmed.substr(0, trimmed.size() - 4);
                }
                if (!trimmed.empty()) {
                    out.push_back(trimmed);
                }
            }
            item.clear();
        } else {
            item += c;
        }
    }
    // Handle last item
    size_t start = item.find_first_not_of(L" \t");
    size_t end = item.find_last_not_of(L" \t");
    if (start != std::wstring::npos) {
        std::wstring trimmed = item.substr(start, end - start + 1);
        for (wchar_t& ch : trimmed) {
            ch = towlower(ch);
        }
        if (trimmed.size() > 4 && trimmed.substr(trimmed.size() - 4) == L".exe") {
            trimmed = trimmed.substr(0, trimmed.size() - 4);
        }
        if (!trimmed.empty()) {
            out.push_back(trimmed);
        }
    }
}

// Parse comma-separated whitelist string into vector of lowercase exe names
void ParseGammaWhitelist() {
    std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
    ParseWhitelistString(g_gammaWhitelistRaw, g_gammaWhitelist);
}

void ParseVrrWhitelist() {
    std::lock_guard<std::mutex> lock(g_vrrWhitelistMutex);
    ParseWhitelistString(g_vrrWhitelistRaw, g_vrrWhitelist);
}

// Read potentially long INI strings with expanding buffer (avoids truncation)
static std::wstring ReadLongINIString(const wchar_t* section, const wchar_t* key, const wchar_t* path) {
    DWORD size = 1024;
    std::wstring buf(size, L'\0');
    for (;;) {
        DWORD ret = GetPrivateProfileStringW(section, key, L"", buf.data(), size, path);
        if (ret < size - 2) { buf.resize(ret); return buf; }
        if (size >= (1u << 20)) { buf.resize(0); return buf; }  // 1MB safety cap
        size *= 2;
        buf.resize(size);
    }
}

void SaveSettings() {
    std::wstring iniPath = GetIniPath();

    // Save general settings
    // DesktopGamma is now per-monitor (MHCDesktopGamma in each monitor section) — not saved globally
    WritePrivateProfileBool(L"General", L"TetrahedralInterp", g_tetrahedralInterp.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"LogPeakDetection", g_logPeakDetection.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"ConsoleLog", g_consoleEnabled.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"ShowFrameTiming", g_showFrameTiming.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"ShowMotionBar", g_showMotionBar.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"FramePacerEnabled", g_framePacerEnabled.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"FramePacerSpinWait", g_framePacerSpinWait.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"FrameBuffer", g_frameBufferEnabled.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"FramePacerLog", g_framePacerLogEnabled.load(), iniPath.c_str());
    {
        wchar_t buf[32];
        swprintf_s(buf, L"%d", g_frameBufferIdleMs.load());
        WritePrivateProfileStringW(L"General", L"FrameBufferIdleMs", buf, iniPath.c_str());
    }
    WritePrivateProfileBool(L"General", L"DwmHookMode", g_dwmHookMode.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"CalibrationControl", g_calibrationControlEnabled.load(), iniPath.c_str());
    WritePrivateProfileStringW(L"General", L"GammaWhitelist", g_gammaWhitelistRaw.c_str(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"VRRWhitelistEnabled", g_vrrWhitelistEnabled.load(), iniPath.c_str());
    WritePrivateProfileStringW(L"General", L"VRRWhitelist", g_vrrWhitelistRaw.c_str(), iniPath.c_str());

    // Save hotkey settings
    WritePrivateProfileBool(L"General", L"HotkeyGammaEnabled", g_hotkeyGammaEnabled.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"HotkeyHdrEnabled", g_hotkeyHdrEnabled.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"HotkeyAnalysisEnabled", g_hotkeyAnalysisEnabled.load(), iniPath.c_str());
    wchar_t keyBuf[2] = { (wchar_t)g_hotkeyGammaKey, 0 };
    WritePrivateProfileStringW(L"General", L"HotkeyGammaKey", keyBuf, iniPath.c_str());
    keyBuf[0] = (wchar_t)g_hotkeyHdrKey;
    WritePrivateProfileStringW(L"General", L"HotkeyHdrKey", keyBuf, iniPath.c_str());
    keyBuf[0] = (wchar_t)g_hotkeyAnalysisKey;
    WritePrivateProfileStringW(L"General", L"HotkeyAnalysisKey", keyBuf, iniPath.c_str());

    // Save startup settings
    WritePrivateProfileBool(L"General", L"StartMinimized", g_startMinimized.load(), iniPath.c_str());

    // Save per-monitor settings
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        wchar_t section[32];
        swprintf_s(section, L"Monitor%d", (int)i);

        WritePrivateProfileStringW(section, L"LUT_SDR", g_gui.monitorSettings[i].sdrPath.c_str(), iniPath.c_str());
        WritePrivateProfileStringW(section, L"LUT_HDR", g_gui.monitorSettings[i].hdrPath.c_str(), iniPath.c_str());

        // Save color correction settings for both SDR and HDR
        SaveColorCorrectionSettings(section, L"SDR_", g_gui.monitorSettings[i].sdrColorCorrection, iniPath.c_str());
        SaveColorCorrectionSettings(section, L"HDR_", g_gui.monitorSettings[i].hdrColorCorrection, iniPath.c_str());

        // Save MaxTML settings
        WritePrivateProfileBool(section, L"MaxTmlEnabled", g_gui.monitorSettings[i].maxTml.enabled, iniPath.c_str());
        WritePrivateProfileFloat(section, L"MaxTmlPeak", g_gui.monitorSettings[i].maxTml.peakNits, iniPath.c_str());

        // Save MHC settings (with own primaries and grayscale)
        SaveMHCSettings(section, L"SDR_", g_gui.monitorSettings[i].sdrMHC, iniPath.c_str());
        SaveMHCSettings(section, L"HDR_", g_gui.monitorSettings[i].hdrMHC, iniPath.c_str());
    }
}

void LoadSettings() {
    std::wstring iniPath = GetIniPath();

    // Load general settings
    // DesktopGamma is now per-monitor (derived from MHCDesktopGamma after monitors load below)
    g_tetrahedralInterp.store(GetPrivateProfileBool(L"General", L"TetrahedralInterp", false, iniPath.c_str()));
    g_logPeakDetection.store(GetPrivateProfileBool(L"General", L"LogPeakDetection", false, iniPath.c_str()));
    g_consoleEnabled.store(GetPrivateProfileBool(L"General", L"ConsoleLog", false, iniPath.c_str()));
    g_showFrameTiming.store(GetPrivateProfileBool(L"General", L"ShowFrameTiming", false, iniPath.c_str()));
    g_showMotionBar.store(GetPrivateProfileBool(L"General", L"ShowMotionBar", false, iniPath.c_str()));
    g_framePacerEnabled.store(GetPrivateProfileBool(L"General", L"FramePacerEnabled", true, iniPath.c_str()));
    g_framePacerSpinWait.store(GetPrivateProfileBool(L"General", L"FramePacerSpinWait", true, iniPath.c_str()));
    g_frameBufferEnabled.store(GetPrivateProfileBool(L"General", L"FrameBuffer", true, iniPath.c_str()));
    g_framePacerLogEnabled.store(GetPrivateProfileBool(L"General", L"FramePacerLog", false, iniPath.c_str()));
    g_frameBufferIdleMs.store((int)GetPrivateProfileIntW(L"General", L"FrameBufferIdleMs", 3000, iniPath.c_str()));
    g_dwmHookMode.store(GetPrivateProfileBool(L"General", L"DwmHookMode", false, iniPath.c_str()));
    g_calibrationControlEnabled.store(GetPrivateProfileBool(L"General", L"CalibrationControl", false, iniPath.c_str()));

    // Load gamma whitelist (expanding buffer to avoid truncation)
    g_gammaWhitelistRaw = ReadLongINIString(L"General", L"GammaWhitelist", iniPath.c_str());
    ParseGammaWhitelist();

    // Load VRR whitelist
    g_vrrWhitelistEnabled.store(GetPrivateProfileBool(L"General", L"VRRWhitelistEnabled", false, iniPath.c_str()));
    g_vrrWhitelistRaw = ReadLongINIString(L"General", L"VRRWhitelist", iniPath.c_str());
    ParseVrrWhitelist();

    // Load hotkey settings
    g_hotkeyGammaEnabled.store(GetPrivateProfileBool(L"General", L"HotkeyGammaEnabled", true, iniPath.c_str()));
    g_hotkeyHdrEnabled.store(GetPrivateProfileBool(L"General", L"HotkeyHdrEnabled", true, iniPath.c_str()));
    g_hotkeyAnalysisEnabled.store(GetPrivateProfileBool(L"General", L"HotkeyAnalysisEnabled", true, iniPath.c_str()));
    wchar_t keyBuf[4] = {};
    GetPrivateProfileStringW(L"General", L"HotkeyGammaKey", L"G", keyBuf, 4, iniPath.c_str());
    { wchar_t ch = towupper(keyBuf[0]); g_hotkeyGammaKey = (ch >= L'A' && ch <= L'Z') ? (char)ch : 'G'; }
    GetPrivateProfileStringW(L"General", L"HotkeyHdrKey", L"Z", keyBuf, 4, iniPath.c_str());
    { wchar_t ch = towupper(keyBuf[0]); g_hotkeyHdrKey = (ch >= L'A' && ch <= L'Z') ? (char)ch : 'Z'; }
    GetPrivateProfileStringW(L"General", L"HotkeyAnalysisKey", L"X", keyBuf, 4, iniPath.c_str());
    { wchar_t ch = towupper(keyBuf[0]); g_hotkeyAnalysisKey = (ch >= L'A' && ch <= L'Z') ? (char)ch : 'X'; }

    // Load startup settings
    g_startMinimized.store(GetPrivateProfileBool(L"General", L"StartMinimized", false, iniPath.c_str()));

    // Load per-monitor settings
    for (size_t i = 0; i < g_gui.monitorSettings.size(); i++) {
        wchar_t section[32];
        swprintf_s(section, L"Monitor%d", (int)i);

        wchar_t sdrPath[MAX_PATH] = {};
        wchar_t hdrPath[MAX_PATH] = {};

        GetPrivateProfileStringW(section, L"LUT_SDR", L"", sdrPath, MAX_PATH, iniPath.c_str());
        GetPrivateProfileStringW(section, L"LUT_HDR", L"", hdrPath, MAX_PATH, iniPath.c_str());

        g_gui.monitorSettings[i].sdrPath = sdrPath;
        g_gui.monitorSettings[i].hdrPath = hdrPath;

        // Load color correction settings for both SDR and HDR
        LoadColorCorrectionSettings(section, L"SDR_", g_gui.monitorSettings[i].sdrColorCorrection, iniPath.c_str());
        LoadColorCorrectionSettings(section, L"HDR_", g_gui.monitorSettings[i].hdrColorCorrection, iniPath.c_str());

        // Load MaxTML settings
        g_gui.monitorSettings[i].maxTml.enabled = GetPrivateProfileBool(section, L"MaxTmlEnabled", false, iniPath.c_str());
        float rawPeak = GetPrivateProfileFloat(section, L"MaxTmlPeak", 1000.0f, iniPath.c_str());
        g_gui.monitorSettings[i].maxTml.peakNits = (std::min)(10000.0f, (std::max)(10.0f, rawPeak));

        // Load MHC settings (with own primaries and grayscale)
        LoadMHCSettings(section, L"SDR_", g_gui.monitorSettings[i].sdrMHC, iniPath.c_str());
        LoadMHCSettings(section, L"HDR_", g_gui.monitorSettings[i].hdrMHC, iniPath.c_str());
    }

    // Derive desktop gamma global from per-monitor MHC settings.
    // DG is user intent — if desktopGammaEnabled is set, flag it active.
    // Processing init will auto-generate an identity MHC profile if needed.
    bool anyDG = false;
    for (const auto& ms : g_gui.monitorSettings)
        if (ms.hdrMHC.desktopGammaEnabled) { anyDG = true; break; }
    g_userDesktopGammaMode.store(anyDG);
    g_desktopGammaMode.store(anyDG);
}
