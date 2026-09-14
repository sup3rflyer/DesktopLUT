// DesktopLUT - displayconfig.h
// Display configuration utilities (MaxTML, etc.)

#pragma once

#include <windows.h>
#include <dxgi1_6.h>
#include <string>
#include <vector>

// Display info for MaxTML operations
struct DisplayInfo {
    std::wstring name;
    std::wstring devicePath;
    LUID adapterId = {};
    UINT32 targetId = 0;
    UINT32 sourceId = 0;        // path.sourceInfo.id for ICC profile APIs
    float currentMaxTml = 0.0f;  // Current MaxTML in nits (0 if unknown)
    bool isHdrCapable = false;
};

// Enumerate displays that support HDR/advanced color
bool EnumerateDisplaysForMaxTml(std::vector<DisplayInfo>& displays);

// Get current MaxTML for a display (returns 0 if not available)
float GetDisplayMaxTml(const DisplayInfo& display);

// Set MaxTML for a display (nits value, e.g., 1000, 4000, 10000)
// Returns true on success
bool SetDisplayMaxTml(const DisplayInfo& display, float nits);

// Get display info for a specific monitor index (matches g_monitors order)
bool GetDisplayInfoForMonitor(int monitorIndex, DisplayInfo& outInfo);

// Query fresh DXGI output descriptor for a monitor (creates new factory to avoid stale data)
bool QueryFreshOutputDesc(HMONITOR hMonitor, DXGI_OUTPUT_DESC1& outDesc);

// Get/set HDR state for a display
bool GetDisplayHdrState(const DisplayInfo& display, bool& outEnabled);
bool SetDisplayHdrState(const DisplayInfo& display, bool enable);

// Live colour MODE of a display target (DLC work guide C8, 2026-09-14): SDR (8-bit composition),
// ACM SDR (Windows "Automatically manage color for apps": FP16 scRGB composition at SDR luminance)
// or HDR. The DXGI output colour space cannot see ACM (it stays G22_P709 with ACM on), so this asks
// DisplayConfig: DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2 (Windows 11 24H2+, activeColorMode
// SDR / WCG / HDR) with the older GET_ADVANCED_COLOR_INFO as the fallback (advancedColorEnabled and
// not HDR = ACM SDR). The FALD overlay layer runs in HDR and in ACM SDR, never on a plain SDR desktop.
enum class DisplayColorMode { Unknown = 0, SDR, AcmSdr, HDR };
struct DisplayColorModeResult {
    DisplayColorMode mode = DisplayColorMode::Unknown;
    const char* source = "none";   // "dxgi" | "displayconfig2" | "displayconfig": which query decided
};
// dxgiHdrActive / dxgiFp16Sdr = the caller's fresh DXGI verdict (G2084_P2020 / G10_P709): HDR always
// wins; the DisplayConfig queries classify SDR vs ACM SDR.
DisplayColorModeResult QueryDisplayColorMode(const DisplayInfo& display, bool dxgiHdrActive, bool dxgiFp16Sdr = false);
// Pure classification (exposed for tests). info2Ok / activeColorMode: GET_ADVANCED_COLOR_INFO_2
// (0 = SDR, 1 = WCG i.e. ACM, 2 = HDR); legacyOk / legacyAdvancedColorEnabled: GET_ADVANCED_COLOR_INFO.
DisplayColorModeResult ClassifyDisplayColorMode(bool dxgiHdrActive, bool dxgiFp16Sdr,
                                                bool info2Ok, unsigned int activeColorMode,
                                                bool legacyOk, bool legacyAdvancedColorEnabled);
const char* DisplayColorModeName(DisplayColorMode mode);   // "SDR" | "ACM_SDR" | "HDR" | "UNKNOWN"

// Toggle HDR on the monitor containing the focused window
// Returns true if toggled, false if failed (e.g., monitor not HDR-capable)
bool ToggleHdrOnFocusedMonitor();

// Monitor primaries from EDID/DXGI
struct MonitorPrimaries {
    float Rx, Ry;  // Red primary chromaticity
    float Gx, Gy;  // Green primary chromaticity
    float Bx, By;  // Blue primary chromaticity
    float Wx, Wy;  // White point chromaticity
    bool valid = false;
};

// Get monitor primaries for a specific monitor index via IDXGIOutput6
// Returns primaries with valid=true if successful
// Note: Often returns sRGB defaults on many drivers - prefer GetMonitorPrimariesFromEDID
MonitorPrimaries GetMonitorPrimaries(int monitorIndex);

// Get monitor primaries by parsing EDID data from Windows registry
// This is more reliable than GetMonitorPrimaries() as it reads actual EDID values
// Returns primaries with valid=true if successful
MonitorPrimaries GetMonitorPrimariesFromEDID(int monitorIndex);

// EDID parsing (exposed for testing)
bool ParseEDIDChromaticity(const BYTE* edid, size_t edidSize, MonitorPrimaries& primaries);
std::wstring ExtractHardwareIdFromPath(const std::wstring& devicePath);
