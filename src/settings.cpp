// DesktopLUT - settings.cpp
// INI settings persistence

#include "settings.h"
#include "globals.h"
#include <cwchar>

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
    swprintf_s(buf, L"%.4f", value);
    WritePrivateProfileStringW(section, key, buf, file);
}

float GetPrivateProfileFloat(const wchar_t* section, const wchar_t* key, float def, const wchar_t* file) {
    wchar_t buf[32] = {};
    GetPrivateProfileStringW(section, key, L"", buf, 32, file);
    if (buf[0] == L'\0') return def;
    return (float)_wtof(buf);
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
    swprintf_s(buf, L"%.4f, %.4f", x, y);
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
    x = (float)_wtof(buf);
    y = (float)_wtof(comma + 1);
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
    WritePrivateProfileBool(section, (p + L"PrimariesEnabled").c_str(), cc.primariesEnabled, iniPath);
    wchar_t presetBuf[8];
    swprintf_s(presetBuf, L"%d", cc.primariesPreset);
    WritePrivateProfileStringW(section, (p + L"PrimariesPreset").c_str(), presetBuf, iniPath);

    // Primaries as xy coordinate pairs (more readable)
    WritePrivateProfileXY(section, (p + L"PrimariesRed").c_str(),
        cc.customPrimaries.Rx, cc.customPrimaries.Ry, iniPath);
    WritePrivateProfileXY(section, (p + L"PrimariesGreen").c_str(),
        cc.customPrimaries.Gx, cc.customPrimaries.Gy, iniPath);
    WritePrivateProfileXY(section, (p + L"PrimariesBlue").c_str(),
        cc.customPrimaries.Bx, cc.customPrimaries.By, iniPath);
    WritePrivateProfileXY(section, (p + L"PrimariesWhite").c_str(),
        cc.customPrimaries.Wx, cc.customPrimaries.Wy, iniPath);

    WritePrivateProfileBool(section, (p + L"GrayscaleEnabled").c_str(), cc.grayscale.enabled, iniPath);
    wchar_t pointsBuf[8];
    swprintf_s(pointsBuf, L"%d", cc.grayscale.pointCount);
    WritePrivateProfileStringW(section, (p + L"GrayscalePoints").c_str(), pointsBuf, iniPath);

    // Grayscale data: space after semicolons for readability
    std::wstring grayscaleData;
    for (size_t j = 0; j < cc.grayscale.points.size(); j++) {
        wchar_t val[16];
        swprintf_s(val, L"%.4f", cc.grayscale.points[j]);
        if (j > 0) grayscaleData += L"; ";
        grayscaleData += val;
    }
    WritePrivateProfileStringW(section, (p + L"GrayscaleData").c_str(), grayscaleData.c_str(), iniPath);

    // HDR-specific and SDR-specific settings
    bool isHDR = (p.find(L"HDR") != std::wstring::npos);
    if (isHDR) {
        WritePrivateProfileFloat(section, (p + L"GrayscalePeak").c_str(), cc.grayscale.peakNits, iniPath);
    } else {
        // SDR only: 2.4 gamma option
        WritePrivateProfileBool(section, (p + L"Grayscale24").c_str(), cc.grayscale.use24Gamma, iniPath);
    }
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
    cc.primariesEnabled = GetPrivateProfileBool(section, (p + L"PrimariesEnabled").c_str(), false, iniPath);
    int preset = GetPrivateProfileIntW(section, (p + L"PrimariesPreset").c_str(), 0, iniPath);
    cc.primariesPreset = (preset >= 0 && preset < g_numPresetPrimaries) ? preset : 0;

    // HDR defaults to Rec.2020 primaries, SDR defaults to sRGB
    bool isHDR = (p.find(L"HDR") != std::wstring::npos);
    float defRx = isHDR ? 0.708f : 0.64f;
    float defRy = isHDR ? 0.292f : 0.33f;
    float defGx = isHDR ? 0.170f : 0.30f;
    float defGy = isHDR ? 0.797f : 0.60f;
    float defBx = isHDR ? 0.131f : 0.15f;
    float defBy = isHDR ? 0.046f : 0.06f;

    // Load primaries as xy coordinate pairs
    if (!GetPrivateProfileXY(section, (p + L"PrimariesRed").c_str(),
            cc.customPrimaries.Rx, cc.customPrimaries.Ry, iniPath)) {
        cc.customPrimaries.Rx = defRx;
        cc.customPrimaries.Ry = defRy;
    }
    if (!GetPrivateProfileXY(section, (p + L"PrimariesGreen").c_str(),
            cc.customPrimaries.Gx, cc.customPrimaries.Gy, iniPath)) {
        cc.customPrimaries.Gx = defGx;
        cc.customPrimaries.Gy = defGy;
    }
    if (!GetPrivateProfileXY(section, (p + L"PrimariesBlue").c_str(),
            cc.customPrimaries.Bx, cc.customPrimaries.By, iniPath)) {
        cc.customPrimaries.Bx = defBx;
        cc.customPrimaries.By = defBy;
    }
    if (!GetPrivateProfileXY(section, (p + L"PrimariesWhite").c_str(),
            cc.customPrimaries.Wx, cc.customPrimaries.Wy, iniPath)) {
        cc.customPrimaries.Wx = 0.3127f;
        cc.customPrimaries.Wy = 0.329f;
    }

    cc.grayscale.enabled = GetPrivateProfileBool(section, (p + L"GrayscaleEnabled").c_str(), false, iniPath);
    int points = GetPrivateProfileIntW(section, (p + L"GrayscalePoints").c_str(), 20, iniPath);
    cc.grayscale.pointCount = (points == 10 || points == 20 || points == 32) ? points : 20;

    wchar_t grayscaleData[1024] = {};
    GetPrivateProfileStringW(section, (p + L"GrayscaleData").c_str(), L"", grayscaleData, 1024, iniPath);
    cc.grayscale.points.clear();
    if (grayscaleData[0] != L'\0') {
        wchar_t* ctx = nullptr;
        wchar_t* token = wcstok_s(grayscaleData, L";", &ctx);
        while (token) {
            // Skip leading whitespace
            while (*token == L' ' || *token == L'\t') token++;
            cc.grayscale.points.push_back((float)_wtof(token));
            token = wcstok_s(nullptr, L";", &ctx);
        }
    }
    // Ensure points vector size matches pointCount, reinitialize if mismatch or empty
    if (cc.grayscale.points.empty() || (int)cc.grayscale.points.size() != cc.grayscale.pointCount) {
        cc.grayscale.points.resize(cc.grayscale.pointCount);
        if (isHDR) {
            cc.grayscale.initLinearPQ();
        } else {
            cc.grayscale.initLinear();
        }
    }
    // HDR-specific settings
    if (isHDR) {
        float peakNits = GetPrivateProfileFloat(section, (p + L"GrayscalePeak").c_str(), 10000.0f, iniPath);
        cc.grayscale.peakNits = (peakNits >= 100.0f && peakNits <= 10000.0f) ? peakNits : 10000.0f;
    } else {
        cc.grayscale.use24Gamma = GetPrivateProfileBool(section, (p + L"Grayscale24").c_str(), false, iniPath);
    }

    // Tonemapping settings (HDR only)
    if (isHDR) {
        cc.tonemap.enabled = GetPrivateProfileBool(section, (p + L"TonemapEnabled").c_str(), false, iniPath);
        wchar_t curveBuf[32] = {};
        GetPrivateProfileStringW(section, (p + L"TonemapCurve").c_str(), L"BT2390", curveBuf, 32, iniPath);
        cc.tonemap.curve = StringToTonemapCurve(curveBuf);
        float srcPeak = GetPrivateProfileFloat(section, (p + L"TonemapSourcePeak").c_str(), 10000.0f, iniPath);
        float tgtPeak = GetPrivateProfileFloat(section, (p + L"TonemapTargetPeak").c_str(), 1000.0f, iniPath);
        cc.tonemap.sourcePeakNits = (srcPeak >= 100.0f && srcPeak <= 10000.0f) ? srcPeak : 10000.0f;
        cc.tonemap.targetPeakNits = (tgtPeak >= 100.0f && tgtPeak <= 10000.0f) ? tgtPeak : 1000.0f;
        cc.tonemap.dynamicPeak = GetPrivateProfileBool(section, (p + L"TonemapDynamic").c_str(), false, iniPath);
    }
}

// Parse comma-separated whitelist string into vector of lowercase exe names
void ParseGammaWhitelist() {
    std::lock_guard<std::mutex> lock(g_gammaWhitelistMutex);
    g_gammaWhitelist.clear();
    if (g_gammaWhitelistRaw.empty()) return;

    std::wstring item;
    for (wchar_t c : g_gammaWhitelistRaw) {
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
                    g_gammaWhitelist.push_back(trimmed);
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
            g_gammaWhitelist.push_back(trimmed);
        }
    }
}

void SaveSettings() {
    std::wstring iniPath = GetIniPath();

    // Save general settings (save user preference, not effective state)
    WritePrivateProfileBool(L"General", L"DesktopGamma", g_userDesktopGammaMode.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"TetrahedralInterp", g_tetrahedralInterp.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"LogPeakDetection", g_logPeakDetection.load(), iniPath.c_str());
    WritePrivateProfileBool(L"General", L"ConsoleLog", g_consoleEnabled.load(), iniPath.c_str());
    WritePrivateProfileStringW(L"General", L"GammaWhitelist", g_gammaWhitelistRaw.c_str(), iniPath.c_str());

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
    }
}

void LoadSettings() {
    std::wstring iniPath = GetIniPath();

    // Load general settings
    bool desktopGamma = GetPrivateProfileBool(L"General", L"DesktopGamma", true, iniPath.c_str());
    g_userDesktopGammaMode.store(desktopGamma);
    g_desktopGammaMode.store(desktopGamma);  // Effective starts at user preference
    g_tetrahedralInterp.store(GetPrivateProfileBool(L"General", L"TetrahedralInterp", false, iniPath.c_str()));
    g_logPeakDetection.store(GetPrivateProfileBool(L"General", L"LogPeakDetection", false, iniPath.c_str()));
    g_consoleEnabled.store(GetPrivateProfileBool(L"General", L"ConsoleLog", false, iniPath.c_str()));

    // Load gamma whitelist
    wchar_t whitelistBuf[1024] = {};
    GetPrivateProfileStringW(L"General", L"GammaWhitelist", L"", whitelistBuf, 1024, iniPath.c_str());
    g_gammaWhitelistRaw = whitelistBuf;
    ParseGammaWhitelist();

    // Load hotkey settings
    g_hotkeyGammaEnabled.store(GetPrivateProfileBool(L"General", L"HotkeyGammaEnabled", true, iniPath.c_str()));
    g_hotkeyHdrEnabled.store(GetPrivateProfileBool(L"General", L"HotkeyHdrEnabled", true, iniPath.c_str()));
    g_hotkeyAnalysisEnabled.store(GetPrivateProfileBool(L"General", L"HotkeyAnalysisEnabled", true, iniPath.c_str()));
    wchar_t keyBuf[4] = {};
    GetPrivateProfileStringW(L"General", L"HotkeyGammaKey", L"G", keyBuf, 4, iniPath.c_str());
    g_hotkeyGammaKey = (keyBuf[0] >= 'A' && keyBuf[0] <= 'Z') ? (char)keyBuf[0] : 'G';
    GetPrivateProfileStringW(L"General", L"HotkeyHdrKey", L"Z", keyBuf, 4, iniPath.c_str());
    g_hotkeyHdrKey = (keyBuf[0] >= 'A' && keyBuf[0] <= 'Z') ? (char)keyBuf[0] : 'Z';
    GetPrivateProfileStringW(L"General", L"HotkeyAnalysisKey", L"X", keyBuf, 4, iniPath.c_str());
    g_hotkeyAnalysisKey = (keyBuf[0] >= 'A' && keyBuf[0] <= 'Z') ? (char)keyBuf[0] : 'X';

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
        g_gui.monitorSettings[i].maxTml.peakNits = GetPrivateProfileFloat(section, L"MaxTmlPeak", 1000.0f, iniPath.c_str());
    }
}
