// DesktopLUT - settings.cpp
// INI settings persistence

#include "settings.h"
#include "globals.h"
#include "monitor_identity.h"
#include "fald.h"    // FALD_TAU_MAX_MS, FaldStarfieldClamp
#include "displayconfig.h"   // IsValidSdrWhiteNits
#include "grayscale_validate.h"
#include <algorithm>
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
    // The whole value must be a finite number: a hand-edited "abc" used to parse as 0 (then
    // clamped to a nonsense-but-in-range value and applied, e.g. a 10-nit MaxTML override).
    wchar_t* end = nullptr;
    double v = _wcstod_l(buf, &end, GetCLocale());
    if (end == buf) return def;
    while (*end == L' ' || *end == L'\t') end++;
    if (*end != L'\0' || !std::isfinite(v) || std::fabs(v) > 3.0e38) return def;
    return (float)v;
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
    // FALD compensation layer: per mode (HDR_ and SDR_ — SDR runs it under Windows ACM).
    WritePrivateProfileBool(section, (p + L"FaldEnabled").c_str(), cc.fald.enabled, iniPath);
    WritePrivateProfileStringW(section, (p + L"FaldParamsPath").c_str(), cc.fald.paramsPath.c_str(), iniPath);
    WritePrivateProfileBool(section, (p + L"FaldPerChannelPedestal").c_str(), cc.fald.pedMode == 1, iniPath);
    // temporal drive state (LED-lag filter): mode 0/1/2 + rise/fall time constants in ms (0 = instant on that edge);
    // mode 3 = panel clock (work guide C13) with its closure per tick + tick parity (-1 unknown / 0 / 1)
    WritePrivateProfileStringW(section, (p + L"FaldTemporalMode").c_str(), std::to_wstring(cc.fald.temporalMode).c_str(), iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldTauRiseMs").c_str(), cc.fald.tauRiseMs, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldTauFallMs").c_str(), cc.fald.tauFallMs, iniPath);
    WritePrivateProfileStringW(section, (p + L"FaldDelayFrames").c_str(), std::to_wstring(cc.fald.delayFrames).c_str(), iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldTemporalClosure").c_str(), cc.fald.clockClosure, iniPath);
    WritePrivateProfileStringW(section, (p + L"FaldTemporalParity").c_str(), std::to_wstring(cc.fald.clockParity).c_str(), iniPath);
    // starfield balancing (experimental, default off; work guide S1): the GUI row's values + the INI/pipe-only ones
    const FaldStarfieldSettings& st = cc.fald.star;
    WritePrivateProfileBool(section, (p + L"FaldStarfield").c_str(), st.enabled, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarEven").c_str(), st.even, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarLift").c_str(), st.lift, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarTargetGain").c_str(), st.targetGain, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarTargetSigma").c_str(), st.targetSigma, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarKeepNits").c_str(), st.keepNits, iniPath);
    WritePrivateProfileStringW(section, (p + L"FaldStarEvenReach").c_str(), std::to_wstring(st.evenReach).c_str(), iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarCapNits").c_str(), st.capNits, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarStrength").c_str(), st.strength, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarAreaLo").c_str(), st.areaLo, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarAreaHi").c_str(), st.areaHi, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarPeakHi").c_str(), st.peakHi, iniPath);
    WritePrivateProfileStringW(section, (p + L"FaldStarReach").c_str(), std::to_wstring(st.reach).c_str(), iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarNbLo").c_str(), st.nbLo, iniPath);
    WritePrivateProfileFloat(section, (p + L"FaldStarNbHi").c_str(), st.nbHi, iniPath);
    // the glow fill's keys (work guide S2, removed 2026-10-07): deleted, so a saved file carries no dead switch
    for (const wchar_t* k : { L"FaldGlowFill", L"FaldGlowStrength", L"FaldGlowReach", L"FaldGlowCapNits" })
        WritePrivateProfileStringW(section, (p + k).c_str(), nullptr, iniPath);
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
        float srcPeak = GetPrivateProfileFloat(section, (p + L"TonemapSourcePeak").c_str(), 1000.0f, iniPath);
        float tgtPeak = GetPrivateProfileFloat(section, (p + L"TonemapTargetPeak").c_str(), 1000.0f, iniPath);
        cc.tonemap.sourcePeakNits = (srcPeak >= 10.0f && srcPeak <= 10000.0f) ? srcPeak : 1000.0f;
        cc.tonemap.targetPeakNits = (tgtPeak >= 10.0f && tgtPeak <= 10000.0f) ? tgtPeak : 1000.0f;
        cc.tonemap.dynamicPeak = GetPrivateProfileBool(section, (p + L"TonemapDynamic").c_str(), false, iniPath);
    }
    // FALD compensation layer: per mode (HDR_ and SDR_).
    cc.fald.enabled = GetPrivateProfileBool(section, (p + L"FaldEnabled").c_str(), false, iniPath);
    wchar_t faldBuf[1024] = {};
    GetPrivateProfileStringW(section, (p + L"FaldParamsPath").c_str(), L"", faldBuf, 1024, iniPath);
    cc.fald.paramsPath = faldBuf;
    cc.fald.pedMode = GetPrivateProfileBool(section, (p + L"FaldPerChannelPedestal").c_str(), false, iniPath) ? 1u : 0u;
    {
        // temporal drive state: an unknown mode is OFF, time constants are clamped to 0..FALD_TAU_MAX_MS
        int tmode = (int)GetPrivateProfileIntW(section, (p + L"FaldTemporalMode").c_str(), 0, iniPath);
        cc.fald.temporalMode = (tmode >= 0 && tmode <= (int)FALD_TEMPORAL_PANEL) ? (unsigned int)tmode : 0u;
        float rise = GetPrivateProfileFloat(section, (p + L"FaldTauRiseMs").c_str(), 0.0f, iniPath);
        float fall = GetPrivateProfileFloat(section, (p + L"FaldTauFallMs").c_str(), 0.0f, iniPath);
        auto clampTau = [](float v) { return (v != v || v < 0.0f) ? 0.0f : (v > FALD_TAU_MAX_MS ? FALD_TAU_MAX_MS : v); };
        cc.fald.tauRiseMs = clampTau(rise);
        cc.fald.tauFallMs = clampTau(fall);
        int delay = (int)GetPrivateProfileIntW(section, (p + L"FaldDelayFrames").c_str(), 0, iniPath);
        cc.fald.delayFrames = delay < 0 ? 0u : (delay > (int)FALD_DELAY_MAX ? FALD_DELAY_MAX : (unsigned int)delay);
        // panel clock (mode 3): closure clamped to 0.05..1 (NaN -> 0.72), parity -1 / 0 / 1 (anything else -> -1 = unknown).
        // Read as text: GetPrivateProfileInt does not promise negative numbers, and an empty / garbage value must not
        // parse to 0 (a KNOWN parity).
        cc.fald.clockClosure = FaldPanelClockClosure(GetPrivateProfileFloat(section, (p + L"FaldTemporalClosure").c_str(), FALD_CLOCK_CLOSURE_DEFAULT, iniPath));
        wchar_t parityBuf[16] = {};
        GetPrivateProfileStringW(section, (p + L"FaldTemporalParity").c_str(), L"-1", parityBuf, 16, iniPath);
        cc.fald.clockParity = FaldPanelClockParityFromText(parityBuf);
    }
    {
        // starfield balancing: absent keys = the defaults (off); every value is clamped to its range (FaldStarfieldClamp)
        FaldStarfieldSettings st;   // defaults = DLC StarfieldParams
        st.enabled = GetPrivateProfileBool(section, (p + L"FaldStarfield").c_str(), false, iniPath);
        st.even = GetPrivateProfileFloat(section, (p + L"FaldStarEven").c_str(), st.even, iniPath);
        st.lift = GetPrivateProfileFloat(section, (p + L"FaldStarLift").c_str(), st.lift, iniPath);
        st.targetGain = GetPrivateProfileFloat(section, (p + L"FaldStarTargetGain").c_str(), st.targetGain, iniPath);
        st.targetSigma = GetPrivateProfileFloat(section, (p + L"FaldStarTargetSigma").c_str(), st.targetSigma, iniPath);
        st.keepNits = GetPrivateProfileFloat(section, (p + L"FaldStarKeepNits").c_str(), st.keepNits, iniPath);
        int er = (int)GetPrivateProfileIntW(section, (p + L"FaldStarEvenReach").c_str(), (int)st.evenReach, iniPath);
        st.evenReach = er < 0 ? 0u : (unsigned int)er;
        st.capNits = GetPrivateProfileFloat(section, (p + L"FaldStarCapNits").c_str(), st.capNits, iniPath);
        st.strength = GetPrivateProfileFloat(section, (p + L"FaldStarStrength").c_str(), st.strength, iniPath);
        st.areaLo = GetPrivateProfileFloat(section, (p + L"FaldStarAreaLo").c_str(), st.areaLo, iniPath);
        st.areaHi = GetPrivateProfileFloat(section, (p + L"FaldStarAreaHi").c_str(), st.areaHi, iniPath);
        st.peakHi = GetPrivateProfileFloat(section, (p + L"FaldStarPeakHi").c_str(), st.peakHi, iniPath);
        int rc = (int)GetPrivateProfileIntW(section, (p + L"FaldStarReach").c_str(), (int)st.reach, iniPath);
        st.reach = rc < 0 ? 0u : (unsigned int)rc;
        st.nbLo = GetPrivateProfileFloat(section, (p + L"FaldStarNbLo").c_str(), st.nbLo, iniPath);
        st.nbHi = GetPrivateProfileFloat(section, (p + L"FaldStarNbHi").c_str(), st.nbHi, iniPath);
        FaldStarfieldClamp(st);
        cc.fald.star = st;
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

    // Desktop gamma (HDR only) + the SDR white level the installed profile's DG was baked with
    if (isHDR) {
        WritePrivateProfileBool(section, (p + L"MHCDesktopGamma").c_str(), mhc.desktopGammaEnabled, iniPath);
        WritePrivateProfileFloat(section, (p + L"MHCDgSdrWhiteNits").c_str(), mhc.dgSdrWhiteNits, iniPath);
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
        // HDR: the SDR white each cached variant's desktop gamma was baked with (0 = no DG in that bake)
        if (isHDR)
            WritePrivateProfileFloat(section, (p + L"MHCPermDgWhite" + std::to_wstring(k)).c_str(),
                                     mhc.permDgWhiteNits[k], iniPath);
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

// A ';'-separated float list; an entry that is not entirely a number becomes NaN, so the shared
// validity rules (grayscale_validate.h) reject the whole list instead of reading it as 0.
static std::vector<float> ReadFloatListKey(const wchar_t* section, const std::wstring& key, const wchar_t* iniPath) {
    std::vector<float> out;
    wchar_t buf[1024] = {};
    GetPrivateProfileStringW(section, key.c_str(), L"", buf, 1024, iniPath);
    if (buf[0] == L'\0') return out;
    wchar_t* ctx = nullptr;
    for (wchar_t* token = wcstok_s(buf, L";", &ctx); token; token = wcstok_s(nullptr, L";", &ctx)) {
        while (*token == L' ' || *token == L'\t') token++;
        wchar_t* end = nullptr;
        double v = _wcstod_l(token, &end, GetCLocale());
        while (end && (*end == L' ' || *end == L'\t')) end++;
        out.push_back((end == token || !end || *end != L'\0') ? std::nanf("") : (float)v);
    }
    return out;
}

// One grayscale slot: count (10/20/32, else 20), points, three per-channel gain lists. Whatever
// fails the shared rules is reset — the points to the slot's identity curve, a channel's gains to
// 1 — with a log line, and never reaches the MHC LUT bake. Base and correction slots alike.
static void LoadGrayscaleBlock(const wchar_t* section, const std::wstring& countKey, const std::wstring& dataKey,
                               const std::wstring (&devKeys)[3], GrayscaleSettings& gs, bool isHDR,
                               const wchar_t* iniPath) {
    const int n = GetPrivateProfileIntW(section, countKey.c_str(), 20, iniPath);
    gs.pointCount = IsValidGrayscalePointCount(n) ? n : 20;
    gs.points = ReadFloatListKey(section, dataKey, iniPath);
    if (!GrayscalePointsValid(gs.points, gs.pointCount)) {
        if (!gs.points.empty()) {
            std::wcerr << L"Warning: " << section << L"/" << dataKey
                       << L" invalid (count/NaN/range), reinitializing to the identity curve" << std::endl;
        }
        InitIdentityGrayscale(gs, isHDR);
    }
    for (int ch = 0; ch < 3; ch++) {
        std::vector<float> gains = ReadFloatListKey(section, devKeys[ch], iniPath);
        if (!GrayscaleGainsValid(gains, gs.pointCount)) {
            if (!gains.empty()) {
                std::wcerr << L"Warning: " << section << L"/" << devKeys[ch]
                           << L" invalid (count/NaN/range), reset to 1" << std::endl;
            }
            gains.assign(gs.pointCount, 1.0f);
        }
        gs.rgbDeviations[ch] = std::move(gains);
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
    {
        const std::wstring devKeys[3] = { p + L"MHCGrayscaleDevR", p + L"MHCGrayscaleDevG", p + L"MHCGrayscaleDevB" };
        LoadGrayscaleBlock(section, p + L"MHCGrayscalePoints", p + L"MHCGrayscaleData", devKeys,
                           mhc.baseGrayscale, isHDR, iniPath);
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

    // Desktop gamma (HDR only). A missing reference white = a profile baked before it was tracked, i.e. at
    // the original fixed 80 nits; RefreshDesktopGammaSdrWhite re-bakes it once the live level differs.
    if (isHDR) {
        mhc.desktopGammaEnabled = GetPrivateProfileBool(section, (p + L"MHCDesktopGamma").c_str(), false, iniPath);
        float dgWhite = GetPrivateProfileFloat(section, (p + L"MHCDgSdrWhiteNits").c_str(), 80.0f, iniPath);
        mhc.dgSdrWhiteNits = IsValidSdrWhiteNits(dgWhite) ? dgWhite : 80.0f;
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
        // HDR DG stamp. Absent = a cache saved before stamps existed: baked at the recorded level (loaded above;
        // 80 for those INIs). 0 = no DG in that bake; anything else unusable marks the entry stale (-1).
        mhc.permDgWhiteNits[k] = mhc.dgSdrWhiteNits;
        if (isHDR) {
            wchar_t stampBuf[32] = {};
            GetPrivateProfileStringW(section, (p + L"MHCPermDgWhite" + std::to_wstring(k)).c_str(), L"",
                                     stampBuf, 32, iniPath);
            if (stampBuf[0] != L'\0') {
                wchar_t* end = nullptr;
                float stamp = (float)_wcstod_l(stampBuf, &end, GetCLocale());
                bool parsed = end && end != stampBuf;
                mhc.permDgWhiteNits[k] = (parsed && (stamp == 0.0f || IsValidSdrWhiteNits(stamp))) ? stamp : -1.0f;
            }
        }
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
    {
        // Same rules as the base slot (it used to check only the counts: a NaN or 1e9 point went
        // straight into the LUT bake).
        const std::wstring devKeys[3] = { p + L"MHCCorrGSDevR", p + L"MHCCorrGSDevG", p + L"MHCCorrGSDevB" };
        LoadGrayscaleBlock(section, p + L"MHCCorrGSPoints", p + L"MHCCorrGSData", devKeys,
                           mhc.correctionGrayscale, isHDR, iniPath);
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

// Whitelist separators: comma, semicolon, and line breaks (the editor is multi-line, so one exe per
// line is the natural way to type it). Not spaces: exe names can contain them.
static bool IsWhitelistSeparator(wchar_t c) { return c == L',' || c == L';' || c == L'\r' || c == L'\n'; }

std::wstring NormalizeWhitelistRaw(const std::wstring& raw) {
    std::wstring out, item;
    auto flush = [&]() {
        size_t start = item.find_first_not_of(L" \t");
        if (start != std::wstring::npos) {
            size_t end = item.find_last_not_of(L" \t");
            if (!out.empty()) out += L", ";
            out += item.substr(start, end - start + 1);
        }
        item.clear();
    };
    for (wchar_t c : raw) {
        if (IsWhitelistSeparator(c)) flush();
        else item += c;
    }
    flush();
    return out;
}

// Helper to parse comma-separated whitelist into vector of lowercase exe names
void ParseWhitelistString(const std::wstring& raw, std::vector<std::wstring>& out) {
    out.clear();
    if (raw.empty()) return;

    std::wstring item;
    for (wchar_t c : raw) {
        if (IsWhitelistSeparator(c)) {
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

// ============================================================================
// SECTION: Per-monitor sections (identity-keyed [Display<slot>] + legacy [Monitor<N>])
// ============================================================================

static std::vector<std::wstring> ListIniSections(const wchar_t* iniPath) {
    // GetPrivateProfileSectionNamesW returns a double-NUL-terminated list; a
    // return of size-2 means truncation, so grow until it fits.
    std::vector<std::wstring> names;
    std::vector<wchar_t> buf(4096);
    for (;;) {
        DWORD ret = GetPrivateProfileSectionNamesW(buf.data(), (DWORD)buf.size(), iniPath);
        if (ret < buf.size() - 2) break;
        if (buf.size() >= (1u << 20)) return names;  // 1MB safety cap — treat as unreadable
        buf.resize(buf.size() * 2);
    }
    const wchar_t* p = buf.data();
    while (*p) {
        names.emplace_back(p);
        p += names.back().size() + 1;
    }
    return names;
}

std::wstring TodayIsoDate() {
    SYSTEMTIME st = {};
    GetLocalTime(&st);
    wchar_t buf[16];
    swprintf_s(buf, L"%04u-%02u-%02u", (unsigned)st.wYear, (unsigned)st.wMonth, (unsigned)st.wDay);
    return buf;
}

std::vector<int> EnumerateSavedSectionIndices(const wchar_t* prefix, const wchar_t* iniPath) {
    std::vector<int> out;
    const std::wstring pre(prefix);
    for (const std::wstring& name : ListIniSections(iniPath)) {
        // Form "<prefix><digits>" — no sign, no whitespace, no other suffix. The prefix compare
        // is case-insensitive like the profile API itself: "[display3]" receives every write
        // addressed to "Display3", so it must also be loaded as slot 3.
        if (name.size() <= pre.size() || _wcsnicmp(name.c_str(), pre.c_str(), pre.size()) != 0) continue;
        std::wstring digits = name.substr(pre.size());
        bool allDigits = true;
        for (wchar_t c : digits) if (c < L'0' || c > L'9') { allDigits = false; break; }
        if (!allDigits || digits.size() > 6) continue;
        if (digits.size() > 1 && digits[0] == L'0') {
            // "Display007" is not the section the canonical name "Display7" addresses, so it
            // could be listed but never read back; leave the hand-edited section alone.
            std::wcout << L"Settings: ignoring non-canonical INI section [" << name << L"]" << std::endl;
            continue;
        }
        int idx = (int)wcstoul(digits.c_str(), nullptr, 10);
        if (idx >= (int)kMaxSavedMonitorSections) {
            std::wcout << L"Settings: ignoring out-of-range INI section [" << name << L"]" << std::endl;
            continue;
        }
        out.push_back(idx);
    }
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}

static std::wstring SectionName(const wchar_t* prefix, int n) {
    return std::wstring(prefix) + std::to_wstring(n);
}

void LoadMonitorSettings(const wchar_t* section, MonitorSettings& ms, const wchar_t* iniPath) {
    wchar_t sdrPath[MAX_PATH] = {};
    wchar_t hdrPath[MAX_PATH] = {};

    GetPrivateProfileStringW(section, L"LUT_SDR", L"", sdrPath, MAX_PATH, iniPath);
    GetPrivateProfileStringW(section, L"LUT_HDR", L"", hdrPath, MAX_PATH, iniPath);

    ms.sdrPath = sdrPath;
    ms.hdrPath = hdrPath;

    // Load color correction settings for both SDR and HDR
    LoadColorCorrectionSettings(section, L"SDR_", ms.sdrColorCorrection, iniPath);
    LoadColorCorrectionSettings(section, L"HDR_", ms.hdrColorCorrection, iniPath);

    // Load MaxTML settings
    ms.maxTml.enabled = GetPrivateProfileBool(section, L"MaxTmlEnabled", false, iniPath);
    float rawPeak = GetPrivateProfileFloat(section, L"MaxTmlPeak", 1000.0f, iniPath);
    ms.maxTml.peakNits = (std::min)(10000.0f, (std::max)(10.0f, rawPeak));

    // Load MHC settings (with own primaries and grayscale)
    LoadMHCSettings(section, L"SDR_", ms.sdrMHC, iniPath);
    LoadMHCSettings(section, L"HDR_", ms.hdrMHC, iniPath);
}

void SaveMonitorSettings(const wchar_t* section, const MonitorSettings& ms, const wchar_t* iniPath) {
    WritePrivateProfileStringW(section, L"LUT_SDR", ms.sdrPath.c_str(), iniPath);
    WritePrivateProfileStringW(section, L"LUT_HDR", ms.hdrPath.c_str(), iniPath);

    // Save color correction settings for both SDR and HDR
    SaveColorCorrectionSettings(section, L"SDR_", ms.sdrColorCorrection, iniPath);
    SaveColorCorrectionSettings(section, L"HDR_", ms.hdrColorCorrection, iniPath);

    // Save MaxTML settings
    WritePrivateProfileBool(section, L"MaxTmlEnabled", ms.maxTml.enabled, iniPath);
    WritePrivateProfileFloat(section, L"MaxTmlPeak", ms.maxTml.peakNits, iniPath);

    // Save MHC settings (with own primaries and grayscale)
    SaveMHCSettings(section, L"SDR_", ms.sdrMHC, iniPath);
    SaveMHCSettings(section, L"HDR_", ms.hdrMHC, iniPath);
}

static void LoadIdentityKeys(const wchar_t* section, MonitorSettings& ms, const wchar_t* iniPath) {
    ms.identity.devicePath   = ReadLongINIString(section, L"DevicePath", iniPath);
    ms.identity.edidId       = ReadLongINIString(section, L"EdidId", iniPath);
    ms.identity.friendlyName = ReadLongINIString(section, L"DisplayName", iniPath);
    ms.firstSeen             = ReadLongINIString(section, L"FirstSeen", iniPath);
    ms.lastSeen              = ReadLongINIString(section, L"LastSeen", iniPath);
}

static void SaveIdentityKeys(const wchar_t* section, const MonitorSettings& ms, const wchar_t* iniPath) {
    if (!ms.firstSeen.empty()) WritePrivateProfileStringW(section, L"FirstSeen", ms.firstSeen.c_str(), iniPath);
    if (!ms.lastSeen.empty())  WritePrivateProfileStringW(section, L"LastSeen", ms.lastSeen.c_str(), iniPath);
    WritePrivateProfileStringW(section, L"DisplayName", ms.identity.friendlyName.c_str(), iniPath);
    WritePrivateProfileStringW(section, L"EdidId", ms.identity.edidId.c_str(), iniPath);
    // Written last of all the section's keys: a section with a DevicePath is a complete one.
    WritePrivateProfileStringW(section, L"DevicePath", ms.identity.devicePath.c_str(), iniPath);
}

void LoadMonitorSettingsPool(std::vector<MonitorSettings>& pool, const wchar_t* iniPath) {
    pool.clear();

    // Identity-keyed sections: one per display ever seen.
    for (int slot : EnumerateSavedSectionIndices(kDisplaySectionPrefix, iniPath)) {
        MonitorSettings ms;
        std::wstring section = SectionName(kDisplaySectionPrefix, slot);
        LoadMonitorSettings(section.c_str(), ms, iniPath);
        LoadIdentityKeys(section.c_str(), ms, iniPath);
        ms.slot = slot;
        if (ms.identity.empty()) {
            // Hand-edited, or a save cut short before its identity keys (written last): nothing
            // can ever match it, but it is the user's data — it stays parked with its slot
            // reserved, and the section on disk is left as is.
            std::wcout << L"Settings: [" << section << L"] has no display identity (kept, unmatched)" << std::endl;
        }
        pool.push_back(std::move(ms));
    }

    // Pre-identity sections: adopted by enumeration index the first time a display
    // shows up at that index, then marked Migrated=Display<slot> by SaveSettings (kept on
    // disk as the user's copy, never adopted again). Sections whose display is not
    // connected right now stay untouched until it is.
    for (int n : EnumerateSavedSectionIndices(kLegacySectionPrefix, iniPath)) {
        std::wstring section = SectionName(kLegacySectionPrefix, n);
        if (!ReadLongINIString(section.c_str(), L"Migrated", iniPath).empty()) continue;
        MonitorSettings ms;
        LoadMonitorSettings(section.c_str(), ms, iniPath);
        ms.legacyIndex = n;
        pool.push_back(std::move(ms));
    }
}

// Write every known display (live + parked). Payload first, identity keys last (the commit
// marker), then — for an entry just adopted from a legacy [Monitor<N>] section — the
// Migrated= mark on that section. Returns the slots whose legacy claim was retired, so the
// caller can clear legacyIndex on the in-memory entries.
static std::vector<int> SaveMonitorSettingsPool(const std::vector<MonitorSettings>& live,
                                                const std::vector<MonitorSettings>& parked,
                                                const wchar_t* iniPath) {
    std::vector<int> migratedSlots;
    auto saveOne = [&](const MonitorSettings& ms) {
        if (ms.identity.empty() || ms.slot < 0) {
            // Unclaimed legacy entry: its [Monitor<N>] section is still on disk, untouched.
            // Identity-less [Display<slot>] from disk: left exactly as it is.
            // Anonymous live entry (identity query never succeeded): nothing to key it by.
            if (ms.legacyIndex < 0 && ms.slot < 0) {
                std::wcout << L"Settings: skipping an unidentified display's settings (not persisted)"
                           << std::endl;
            }
            return;
        }
        std::wstring section = SectionName(kDisplaySectionPrefix, ms.slot);
        SaveMonitorSettings(section.c_str(), ms, iniPath);
        SaveIdentityKeys(section.c_str(), ms, iniPath);
        if (ms.legacyIndex >= 0) {
            // Migrated: mark (never delete) the pre-identity section so it is not adopted
            // twice, and the user's original copy survives a mis-adoption (another panel on
            // the old display's connector).
            std::wstring legacy = SectionName(kLegacySectionPrefix, ms.legacyIndex);
            WritePrivateProfileStringW(legacy.c_str(), L"Migrated", section.c_str(), iniPath);
            std::wcout << L"Settings: migrated [" << legacy << L"] -> [" << section << L"]" << std::endl;
            migratedSlots.push_back(ms.slot);
        }
    };
    for (const auto& ms : live) saveOne(ms);
    for (const auto& ms : parked) saveOne(ms);
    return migratedSlots;
}

void SaveSettings() {
    std::wstring iniPath = GetIniPath();

    // Save general settings
    // DesktopGamma is now per-monitor (MHCDesktopGamma in each monitor section) — not saved globally
    WritePrivateProfileBool(L"General", L"TetrahedralInterp", g_tetrahedralInterp.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"HdrDither", g_hdrDither.load(), iniPath.c_str());
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
    // One line in the INI: a raw line break in the value would end it there (the rest of the list lost).
    WritePrivateProfileStringW(L"General", L"GammaWhitelist", NormalizeWhitelistRaw(g_gammaWhitelistRaw).c_str(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"VRRWhitelistEnabled", g_vrrWhitelistEnabled.load(), iniPath.c_str());
    WritePrivateProfileStringW(L"General", L"VRRWhitelist", NormalizeWhitelistRaw(g_vrrWhitelistRaw).c_str(), iniPath.c_str());

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

    WritePrivateProfileStringW(L"General", L"IniVersion", std::to_wstring(kIniVersion).c_str(), iniPath.c_str());

    // Save per-display settings: live displays and parked (currently disconnected) ones,
    // each under its identity-keyed [Display<slot>] section. Written from a snapshot taken
    // under g_monitorSettingsMutex: other threads assign profileName/activePerm (heap
    // strings) under it, and the INI writes are far too slow to hold the lock across.
    std::vector<MonitorSettings> live, parked;
    {
        const std::wstring today = TodayIsoDate();
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (auto& ms : g_gui.monitorSettings) {
            if (!ms.identity.empty()) ms.lastSeen = today;   // connected right now
        }
        live = g_gui.monitorSettings;
        parked = g_gui.parkedSettings;
    }
    std::vector<int> migratedSlots = SaveMonitorSettingsPool(live, parked, iniPath.c_str());
    if (!migratedSlots.empty()) {
        // The legacy claim is retired on disk: clear it on the entries that made it (by slot,
        // since the vectors may have been re-attached meanwhile).
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (auto* vec : { &g_gui.monitorSettings, &g_gui.parkedSettings }) {
            for (auto& ms : *vec) {
                if (ms.legacyIndex >= 0 &&
                    std::find(migratedSlots.begin(), migratedSlots.end(), ms.slot) != migratedSlots.end()) {
                    ms.legacyIndex = -1;
                }
            }
        }
    }
}

bool LoadSettings() {
    std::wstring iniPath = GetIniPath();

    // Load general settings
    // DesktopGamma is now per-monitor (derived from MHCDesktopGamma after monitors load below)
    g_tetrahedralInterp.store(GetPrivateProfileBool(L"General", L"TetrahedralInterp", true, iniPath.c_str()));
    g_hdrDither.store(GetPrivateProfileBool(L"General", L"HdrDither", true, iniPath.c_str()));
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

    // Per-display settings: read every saved display into the parked pool, then attach
    // each live monitor's entry by identity. A display that is powered off right now
    // simply stays parked — its settings (and its MHC profile files) are held until it
    // returns, and they follow the panel if Windows enumerates it at another index.
    std::vector<MonitorSettings> pool;
    LoadMonitorSettingsPool(pool, iniPath.c_str());
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        g_gui.monitorSettings.clear();
        g_gui.parkedSettings = std::move(pool);
    }
    const bool allIdentified = ResolveMonitorSettings(g_gui.monitors).allIdentified;

    // Derive desktop gamma global from the live monitors' MHC settings.
    // DG is user intent — if desktopGammaEnabled is set, flag it active.
    // Processing init will auto-generate an identity MHC profile if needed.
    bool anyDG = false;
    {
        std::lock_guard<std::mutex> lock(g_monitorSettingsMutex);
        for (const auto& ms : g_gui.monitorSettings)
            if (ms.hdrMHC.desktopGammaEnabled) { anyDG = true; break; }
    }
    g_userDesktopGammaMode.store(anyDG);
    g_desktopGammaMode.store(anyDG);
    return allIdentified;
}

