// DesktopLUT - displayconfig.h
// Display configuration utilities (MaxTML, etc.)

#pragma once

#include <windows.h>
#include <string>
#include <vector>

// Display info for MaxTML operations
struct DisplayInfo {
    std::wstring name;
    std::wstring devicePath;
    LUID adapterId = {};
    UINT32 targetId = 0;
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

// Get/set HDR state for a display
bool GetDisplayHdrState(const DisplayInfo& display, bool& outEnabled);
bool SetDisplayHdrState(const DisplayInfo& display, bool enable);

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
