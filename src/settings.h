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

// Per-display sections. Settings are keyed by physical display identity:
//   [Display<slot>]  DevicePath= / EdidId= / DisplayName= + the per-monitor keys.
//   [Monitor<N>]     pre-identity format (enumeration index). Loaded as "legacy"
//                    entries, adopted by index the first time a display appears at
//                    that index, then retired on the next save.
constexpr const wchar_t* kDisplaySectionPrefix = L"Display";
constexpr const wchar_t* kLegacySectionPrefix  = L"Monitor";
// Section numbers at or above this are ignored so a hand-edited "Display99999"
// cannot balloon anything.
constexpr size_t kMaxSavedMonitorSections = 256;

// Sorted, unique numeric suffixes of sections named exactly "<prefix><digits>".
std::vector<int> EnumerateSavedSectionIndices(const wchar_t* prefix, const wchar_t* iniPath);

// One display's per-monitor keys (LUT paths, corrections, MaxTML, MHC) — the
// identity keys and slot/legacy bookkeeping are handled by the pool functions.
// Missing keys fall back to defaults.
void LoadMonitorSettings(const wchar_t* section, MonitorSettings& ms, const wchar_t* iniPath);
void SaveMonitorSettings(const wchar_t* section, const MonitorSettings& ms, const wchar_t* iniPath);

// Every saved display: [Display<slot>] entries (identity + slot set) followed by
// unclaimed [Monitor<N>] entries (legacyIndex set, no identity).
void LoadMonitorSettingsPool(std::vector<MonitorSettings>& pool, const wchar_t* iniPath);

// Save all settings to INI file: general keys, then every known display (live and
// parked) under its [Display<slot>] section; migrated legacy sections are deleted.
void SaveSettings();

// Load all settings from INI file, then attach each live monitor's entry
// (g_gui.monitorSettings, one per g_gui.monitors) by identity; the rest of the
// saved displays stay in g_gui.parkedSettings.
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
