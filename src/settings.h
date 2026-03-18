// DesktopLUT - settings.h
// INI settings persistence

#pragma once

#include "types.h"
#include <string>

// Cached C locale for locale-independent float parsing/writing.
// _wtof and swprintf_s use the thread locale which may use comma as decimal
// separator on European systems, breaking float round-trips.
_locale_t GetCLocale();

// Get path to INI file (next to exe)
std::wstring GetIniPath();

// Helper to write float to INI
void WritePrivateProfileFloat(const wchar_t* section, const wchar_t* key, float value, const wchar_t* file);

// Helper to read float from INI
float GetPrivateProfileFloat(const wchar_t* section, const wchar_t* key, float def, const wchar_t* file);

// Helper to read bool from INI (accepts "true"/"false", "1"/"0", "yes"/"no")
bool GetPrivateProfileBool(const wchar_t* section, const wchar_t* key, bool def, const wchar_t* file);

// Save color correction settings with a prefix (SDR_ or HDR_)
void SaveColorCorrectionSettings(const wchar_t* section, const wchar_t* prefix,
                                  const ColorCorrectionSettings& cc, const wchar_t* iniPath);

// Load color correction settings with a prefix (SDR_ or HDR_)
void LoadColorCorrectionSettings(const wchar_t* section, const wchar_t* prefix,
                                  ColorCorrectionSettings& cc, const wchar_t* iniPath);

// Save MHC settings with a prefix (SDR_ or HDR_)
void SaveMHCSettings(const wchar_t* section, const wchar_t* prefix,
                      const MHCSettings& mhc, const wchar_t* iniPath);

// Load MHC settings with a prefix (SDR_ or HDR_)
void LoadMHCSettings(const wchar_t* section, const wchar_t* prefix,
                      MHCSettings& mhc, const wchar_t* iniPath);

// Save all settings to INI file
void SaveSettings();

// Load all settings from INI file
void LoadSettings();

// Tonemap curve enum conversion
const wchar_t* TonemapCurveToString(TonemapCurve curve);
TonemapCurve StringToTonemapCurve(const wchar_t* str);

// Whitelist string parsing (exposed for testing)
void ParseWhitelistString(const std::wstring& raw, std::vector<std::wstring>& out);

// Parse g_gammaWhitelistRaw into g_gammaWhitelist vector
void ParseGammaWhitelist();

// Parse g_vrrWhitelistRaw into g_vrrWhitelist vector
void ParseVrrWhitelist();
