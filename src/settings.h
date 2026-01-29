// DesktopLUT - settings.h
// INI settings persistence

#pragma once

#include "types.h"
#include <string>

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

// Save all settings to INI file
void SaveSettings();

// Load all settings from INI file
void LoadSettings();

// Parse g_gammaWhitelistRaw into g_gammaWhitelist vector
void ParseGammaWhitelist();
