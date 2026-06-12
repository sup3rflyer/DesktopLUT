// DesktopLUT - displayconfig.cpp
// Display configuration utilities (MaxTML, EDID parsing, etc.)
// Sets Windows HDR peak luminance override via undocumented DisplayConfigSetDeviceInfo

#include "displayconfig.h"
#include "globals.h"
#include <iostream>
#include <wingdi.h>
#include <dxgi1_6.h>
#include <setupapi.h>
#include <devguid.h>
#include <algorithm>
#include <cctype>

#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "setupapi.lib")

// GUID for monitor device interface class
static const GUID GUID_DEVINTERFACE_MONITOR = { 0xe6f07b5f, 0xee97, 0x4a90, { 0xb0, 0x76, 0x33, 0xf5, 0x7b, 0xf4, 0xea, 0xa7 } };

// Undocumented display config device info type for setting advanced color parameters
// This is used by Windows 11's HDR calibration app internally
#define DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_PARAM ((DISPLAYCONFIG_DEVICE_INFO_TYPE)0xFFFFFFF0)

// Color parameters structure (matches Windows internal format)
// All chromaticity values are scaled by 10000 (e.g., 0.3127 = 3127)
// All luminance values are in nits * 10000 (e.g., 1000 nits = 10000000)
struct ColorParams {
    unsigned int RedPointX;
    unsigned int RedPointY;
    unsigned int GreenPointX;
    unsigned int GreenPointY;
    unsigned int BluePointX;
    unsigned int BluePointY;
    unsigned int WhitePointX;
    unsigned int WhitePointY;
    unsigned int MinLuminance;
    unsigned int MaxLuminance;          // MaxTML
    unsigned int MaxFullFrameLuminance; // MaxFFTML
};

// Structure for setting MaxTML via DisplayConfigSetDeviceInfo
struct DISPLAYCONFIG_SET_ADVANCED_COLOR_PARAM {
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    ColorParams colorParams;
    char padding[4];  // Additional padding observed in Windows
};

static bool GetDisplayInfoAtPoint(POINT pt, DisplayInfo& outInfo);  // fwd decl

// Query the active display paths/modes with a retry loop. The topology can change
// between GetDisplayConfigBufferSizes and QueryDisplayConfig (the latter then returns
// ERROR_INSUFFICIENT_BUFFER); MSDN prescribes re-querying sizes and retrying. This
// matters because these functions run during the exact windows (mode switches,
// transition bursts, MHC verify) when the topology is changing. On success the vectors
// are resized to the actual counts so callers can use .size().
static bool QueryActivePaths(std::vector<DISPLAYCONFIG_PATH_INFO>& paths,
                             std::vector<DISPLAYCONFIG_MODE_INFO>& modes) {
    for (int attempt = 0; attempt < 5; attempt++) {
        UINT32 pathCount = 0, modeCount = 0;
        if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &pathCount, &modeCount) != ERROR_SUCCESS)
            return false;
        paths.assign(pathCount, {});
        modes.assign(modeCount, {});
        LONG r = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &pathCount, paths.data(),
                                    &modeCount, modes.data(), nullptr);
        if (r == ERROR_SUCCESS) {
            paths.resize(pathCount);   // QueryDisplayConfig may report fewer than allocated
            modes.resize(modeCount);
            return true;
        }
        if (r != ERROR_INSUFFICIENT_BUFFER) return false;  // hard failure — don't spin
        // else: topology changed between the two calls — loop and re-query sizes
    }
    return false;
}

// Collect HMONITORs in EnumDisplayMonitors order (this is the order DesktopLUT indexes
// monitors by, in gui_layout / processing).
static BOOL CALLBACK CollectMonitorsProc(HMONITOR h, HDC, LPRECT, LPARAM lp) {
    reinterpret_cast<std::vector<HMONITOR>*>(lp)->push_back(h);
    return TRUE;
}

bool EnumerateDisplaysForMaxTml(std::vector<DisplayInfo>& displays) {
    displays.clear();

    std::vector<DISPLAYCONFIG_PATH_INFO> paths;
    std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    if (!QueryActivePaths(paths, modes)) {
        return false;
    }

    for (size_t i = 0; i < paths.size(); i++) {
        const auto& path = paths[i];

        // Get target device name
        DISPLAYCONFIG_TARGET_DEVICE_NAME targetName = {};
        targetName.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME;
        targetName.header.size = sizeof(targetName);
        targetName.header.adapterId = path.targetInfo.adapterId;
        targetName.header.id = path.targetInfo.id;

        if (DisplayConfigGetDeviceInfo(&targetName.header) != ERROR_SUCCESS) {
            continue;
        }

        // Get advanced color info to check HDR capability
        DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO colorInfo = {};
        colorInfo.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO;
        colorInfo.header.size = sizeof(colorInfo);
        colorInfo.header.adapterId = path.targetInfo.adapterId;
        colorInfo.header.id = path.targetInfo.id;

        bool isHdrCapable = false;
        if (DisplayConfigGetDeviceInfo(&colorInfo.header) == ERROR_SUCCESS) {
            // Bit 0 = advanced color supported
            isHdrCapable = (colorInfo.value & 0x1) != 0;
        }

        DisplayInfo info;
        info.name = targetName.monitorFriendlyDeviceName;
        info.devicePath = targetName.monitorDevicePath;
        info.adapterId = path.targetInfo.adapterId;
        info.targetId = path.targetInfo.id;
        info.sourceId = path.sourceInfo.id;
        info.currentMaxTml = 0.0f;  // Can't easily read current value
        info.isHdrCapable = isHdrCapable;

        displays.push_back(info);
    }

    return true;
}

float GetDisplayMaxTml(const DisplayInfo& display) {
    // The official API doesn't expose max luminance in a documented way
    // Return 0 to indicate unknown
    return 0.0f;
}

bool SetDisplayMaxTml(const DisplayInfo& display, float nits) {
    DISPLAYCONFIG_SET_ADVANCED_COLOR_PARAM params = {};
    params.header.type = DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_PARAM;
    params.header.size = sizeof(params);
    params.header.adapterId = display.adapterId;
    params.header.id = display.targetId;

    // sRGB/Rec.709 primaries (scaled by 10000)
    params.colorParams.RedPointX = 6400;    // 0.64
    params.colorParams.RedPointY = 3300;    // 0.33
    params.colorParams.GreenPointX = 3000;  // 0.30
    params.colorParams.GreenPointY = 6000;  // 0.60
    params.colorParams.BluePointX = 1500;   // 0.15
    params.colorParams.BluePointY = 600;    // 0.06
    // D65 white point (scaled by 10000)
    params.colorParams.WhitePointX = 3127;  // 0.3127
    params.colorParams.WhitePointY = 3290;  // 0.3290
    // Luminance values (nits * 10000)
    params.colorParams.MinLuminance = 0;
    params.colorParams.MaxLuminance = (unsigned int)(nits * 10000.0f);
    params.colorParams.MaxFullFrameLuminance = (unsigned int)(nits * 10000.0f);

    LONG result = DisplayConfigSetDeviceInfo(&params.header);
    if (result != ERROR_SUCCESS) {
        std::cerr << "SetDisplayMaxTml failed with error: " << result << std::endl;
        return false;
    }

    std::wcout << L"Set MaxTML to " << nits << L" nits for " << display.name << std::endl;
    return true;
}

bool GetDisplayInfoForMonitor(int monitorIndex, DisplayInfo& outInfo) {
    if (monitorIndex < 0) {
        return false;
    }

    // Preferred: correlate the DesktopLUT monitor index (EnumDisplayMonitors order) to the
    // correct display by POSITION, then match the DisplayInfo by target identity inside
    // GetDisplayInfoAtPoint. displays[monitorIndex] is unreliable — QueryDisplayConfig path
    // order need not match EnumDisplayMonitors order, and EnumerateDisplaysForMaxTml skips
    // paths whose target-name query failed (which shifts the indices). Wrong-monitor
    // targeting here would associate an MHC profile / toggle HDR on the wrong display.
    std::vector<HMONITOR> mons;
    EnumDisplayMonitors(nullptr, nullptr, CollectMonitorsProc, reinterpret_cast<LPARAM>(&mons));
    if (monitorIndex < (int)mons.size()) {
        MONITORINFO mi = { sizeof(mi) };
        if (GetMonitorInfo(mons[monitorIndex], &mi)) {
            POINT pt = { mi.rcMonitor.left, mi.rcMonitor.top };
            if (GetDisplayInfoAtPoint(pt, outInfo)) {
                return true;
            }
        }
    }

    // Fallback: legacy positional index (only as good as the order assumption, but no
    // worse than the previous behavior if the position match above could not resolve).
    std::vector<DisplayInfo> displays;
    if (!EnumerateDisplaysForMaxTml(displays)) {
        return false;
    }
    if (monitorIndex < (int)displays.size()) {
        outInfo = displays[monitorIndex];
        return true;
    }

    return false;
}

bool QueryFreshOutputDesc(HMONITOR hMonitor, DXGI_OUTPUT_DESC1& outDesc) {
    IDXGIFactory1* factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return false;

    bool found = false;
    IDXGIAdapter* adapter = nullptr;
    for (UINT a = 0; !found && factory->EnumAdapters(a, &adapter) != DXGI_ERROR_NOT_FOUND; a++) {
        IDXGIOutput* output = nullptr;
        for (UINT o = 0; !found && adapter->EnumOutputs(o, &output) != DXGI_ERROR_NOT_FOUND; o++) {
            DXGI_OUTPUT_DESC desc;
            output->GetDesc(&desc);
            if (desc.Monitor == hMonitor) {
                IDXGIOutput6* output6 = nullptr;
                if (SUCCEEDED(output->QueryInterface(IID_PPV_ARGS(&output6)))) {
                    if (SUCCEEDED(output6->GetDesc1(&outDesc))) {
                        found = true;
                    }
                    output6->Release();
                }
            }
            output->Release();
        }
        adapter->Release();
    }
    factory->Release();
    return found;
}

bool GetDisplayHdrState(const DisplayInfo& display, bool& outEnabled) {
    // Use fresh DXGI factory to check actual display color space.
    // DISPLAYCONFIG advancedColorEnabled is true for both HDR and ACM,
    // so we can't use it to distinguish HDR from ACM.
    // DXGI ColorSpace is G2084_P2020 only when actual HDR mode is active.

    // We need HMONITOR to match DXGI outputs - find it via display position
    std::vector<DISPLAYCONFIG_PATH_INFO> paths;
    std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    if (!QueryActivePaths(paths, modes))
        return false;

    for (size_t i = 0; i < paths.size(); i++) {
        const auto& path = paths[i];
        if (path.targetInfo.adapterId.LowPart == display.adapterId.LowPart &&
            path.targetInfo.adapterId.HighPart == display.adapterId.HighPart &&
            path.targetInfo.id == display.targetId) {
            // Found matching path - get source position to find HMONITOR
            if (path.sourceInfo.modeInfoIdx < modes.size()) {
                const auto& mode = modes[path.sourceInfo.modeInfoIdx];
                if (mode.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE) {
                    POINT pt = { mode.sourceMode.position.x, mode.sourceMode.position.y };
                    HMONITOR hMonitor = MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST);
                    if (hMonitor) {
                        DXGI_OUTPUT_DESC1 desc1;
                        if (QueryFreshOutputDesc(hMonitor, desc1)) {
                            outEnabled = (desc1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020);
                            return true;
                        }
                    }
                }
            }
            break;
        }
    }

    // Fallback: use DISPLAYCONFIG (may be wrong with ACM)
    DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO colorInfo = {};
    colorInfo.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO;
    colorInfo.header.size = sizeof(colorInfo);
    colorInfo.header.adapterId = display.adapterId;
    colorInfo.header.id = display.targetId;

    if (DisplayConfigGetDeviceInfo(&colorInfo.header) != ERROR_SUCCESS)
        return false;

    outEnabled = (colorInfo.value & 0x2) != 0;
    return true;
}

bool SetDisplayHdrState(const DisplayInfo& display, bool enable) {
    DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE colorState = {};
    colorState.header.type = DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE;
    colorState.header.size = sizeof(colorState);
    colorState.header.adapterId = display.adapterId;
    colorState.header.id = display.targetId;
    colorState.enableAdvancedColor = enable ? 1 : 0;

    LONG result = DisplayConfigSetDeviceInfo(&colorState.header);
    if (result != ERROR_SUCCESS) {
        std::cerr << "SetDisplayHdrState failed with error: " << result << std::endl;
        return false;
    }

    std::wcout << L"HDR " << (enable ? L"enabled" : L"disabled") << L" on " << display.name << std::endl;
    return true;
}

// Get DisplayInfo for the monitor containing a specific point
static bool GetDisplayInfoAtPoint(POINT pt, DisplayInfo& outInfo) {
    HMONITOR hMonitor = MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST);
    if (!hMonitor) return false;

    // Get monitor position to match with display enumeration
    MONITORINFO mi = { sizeof(mi) };
    if (!GetMonitorInfo(hMonitor, &mi)) return false;

    // Enumerate displays
    std::vector<DisplayInfo> displays;
    if (!EnumerateDisplaysForMaxTml(displays)) return false;

    // Match by finding the display at this position
    // We need to query each path's source mode to get position
    std::vector<DISPLAYCONFIG_PATH_INFO> paths;
    std::vector<DISPLAYCONFIG_MODE_INFO> modes;
    if (!QueryActivePaths(paths, modes)) {
        return false;
    }

    for (size_t i = 0; i < paths.size(); i++) {
        const auto& path = paths[i];

        // Find the source mode for this path to get position
        if (path.sourceInfo.modeInfoIdx < modes.size()) {
            const auto& mode = modes[path.sourceInfo.modeInfoIdx];
            if (mode.infoType == DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE) {
                POINTL pos = mode.sourceMode.position;
                // Check if monitor rect matches this source position
                if (mi.rcMonitor.left == pos.x && mi.rcMonitor.top == pos.y) {
                    // Match the DisplayInfo by target identity (adapterId+targetId), NOT by
                    // the parallel index i — EnumerateDisplaysForMaxTml skips paths whose
                    // target-name query failed, so displays[i] is not aligned with paths[i].
                    for (const auto& d : displays) {
                        if (d.adapterId.LowPart == path.targetInfo.adapterId.LowPart &&
                            d.adapterId.HighPart == path.targetInfo.adapterId.HighPart &&
                            d.targetId == path.targetInfo.id) {
                            outInfo = d;
                            return true;
                        }
                    }
                }
            }
        }
    }

    return false;
}

bool ToggleHdrOnFocusedMonitor() {
    POINT targetPoint = {};
    bool foundPoint = false;

    // Try to get the foreground window first
    HWND hwndFocus = GetForegroundWindow();
    if (hwndFocus) {
        RECT rect;
        if (GetWindowRect(hwndFocus, &rect)) {
            targetPoint.x = (rect.left + rect.right) / 2;
            targetPoint.y = (rect.top + rect.bottom) / 2;
            foundPoint = true;
        }
    }

    // Fallback to mouse cursor position
    if (!foundPoint) {
        if (GetCursorPos(&targetPoint)) {
            foundPoint = true;
            std::cout << "Using mouse cursor position for HDR toggle" << std::endl;
        }
    }

    if (!foundPoint) {
        std::cerr << "Could not determine target monitor" << std::endl;
        return false;
    }

    // Get display info for this monitor
    DisplayInfo display;
    if (!GetDisplayInfoAtPoint(targetPoint, display)) {
        std::cerr << "Could not find display info for target monitor" << std::endl;
        return false;
    }

    // Check if HDR capable
    if (!display.isHdrCapable) {
        std::wcerr << L"Monitor '" << display.name << L"' does not support HDR" << std::endl;
        return false;
    }

    // Get current state and toggle
    bool currentState = false;
    if (!GetDisplayHdrState(display, currentState)) {
        std::cerr << "Could not get current HDR state" << std::endl;
        return false;
    }

    return SetDisplayHdrState(display, !currentState);
}

MonitorPrimaries GetMonitorPrimaries(int monitorIndex) {
    MonitorPrimaries result = {};

    // Create DXGI factory
    IDXGIFactory6* factory = nullptr;
    HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory6), (void**)&factory);
    if (FAILED(hr) || !factory) {
        std::cerr << "Failed to create DXGI factory for primaries detection" << std::endl;
        return result;
    }

    // Get the HMONITOR for the requested index
    if (monitorIndex < 0 || monitorIndex >= (int)g_gui.monitors.size()) {
        factory->Release();
        return result;
    }
    HMONITOR targetMonitor = g_gui.monitors[monitorIndex];

    // Enumerate adapters and outputs to find matching monitor
    IDXGIAdapter1* adapter = nullptr;
    for (UINT adapterIdx = 0; factory->EnumAdapters1(adapterIdx, &adapter) != DXGI_ERROR_NOT_FOUND; adapterIdx++) {
        IDXGIOutput* output = nullptr;
        for (UINT outputIdx = 0; adapter->EnumOutputs(outputIdx, &output) != DXGI_ERROR_NOT_FOUND; outputIdx++) {
            DXGI_OUTPUT_DESC desc;
            if (SUCCEEDED(output->GetDesc(&desc))) {
                if (desc.Monitor == targetMonitor) {
                    // Found the output, try to get IDXGIOutput6 for extended info
                    IDXGIOutput6* output6 = nullptr;
                    if (SUCCEEDED(output->QueryInterface(__uuidof(IDXGIOutput6), (void**)&output6))) {
                        DXGI_OUTPUT_DESC1 desc1;
                        if (SUCCEEDED(output6->GetDesc1(&desc1))) {
                            // DXGI_OUTPUT_DESC1 stores primaries as FLOAT arrays [x, y]
                            result.Rx = desc1.RedPrimary[0];
                            result.Ry = desc1.RedPrimary[1];
                            result.Gx = desc1.GreenPrimary[0];
                            result.Gy = desc1.GreenPrimary[1];
                            result.Bx = desc1.BluePrimary[0];
                            result.By = desc1.BluePrimary[1];
                            result.Wx = desc1.WhitePoint[0];
                            result.Wy = desc1.WhitePoint[1];
                            result.valid = true;

                            std::cout << "Detected primaries for monitor " << monitorIndex << ":" << std::endl;
                            std::cout << "  R(" << result.Rx << ", " << result.Ry << ")" << std::endl;
                            std::cout << "  G(" << result.Gx << ", " << result.Gy << ")" << std::endl;
                            std::cout << "  B(" << result.Bx << ", " << result.By << ")" << std::endl;
                            std::cout << "  W(" << result.Wx << ", " << result.Wy << ")" << std::endl;
                        }
                        output6->Release();
                    }
                    output->Release();
                    adapter->Release();
                    factory->Release();
                    return result;
                }
            }
            output->Release();
        }
        adapter->Release();
    }

    factory->Release();
    std::cerr << "Could not find DXGI output for monitor " << monitorIndex << std::endl;
    return result;
}

// ============================================================================
// EDID-based Primaries Detection
// ============================================================================

// Parse chromaticity coordinates from EDID bytes 25-34
// EDID encodes each coordinate as a 10-bit value: 8 MSBs in one byte, 2 LSBs packed with others
// The value represents a CIE 1931 xy coordinate as a binary fraction (value / 1024)
bool ParseEDIDChromaticity(const BYTE* edid, size_t edidSize, MonitorPrimaries& primaries) {
    if (edidSize < 35) {
        return false;  // Need at least 35 bytes for chromaticity data
    }

    // Verify EDID header (bytes 0-7 should be 00 FF FF FF FF FF FF 00)
    static const BYTE edidHeader[] = { 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00 };
    if (memcmp(edid, edidHeader, 8) != 0) {
        return false;  // Not a valid EDID
    }

    // Byte 25: Red/Green LSBs
    //   Bits 7-6: Red X LSBs
    //   Bits 5-4: Red Y LSBs
    //   Bits 3-2: Green X LSBs
    //   Bits 1-0: Green Y LSBs
    // Byte 26: Blue/White LSBs
    //   Bits 7-6: Blue X LSBs
    //   Bits 5-4: Blue Y LSBs
    //   Bits 3-2: White X LSBs
    //   Bits 1-0: White Y LSBs
    // Bytes 27-34: MSBs (8 bits each) for Rx, Ry, Gx, Gy, Bx, By, Wx, Wy

    BYTE rgLsb = edid[25];
    BYTE bwLsb = edid[26];

    // Extract 10-bit values and convert to float (divide by 1024)
    auto extract = [](BYTE msb, BYTE lsb2bit) -> float {
        int value = (msb << 2) | lsb2bit;
        return value / 1024.0f;
    };

    primaries.Rx = extract(edid[27], (rgLsb >> 6) & 0x03);
    primaries.Ry = extract(edid[28], (rgLsb >> 4) & 0x03);
    primaries.Gx = extract(edid[29], (rgLsb >> 2) & 0x03);
    primaries.Gy = extract(edid[30], (rgLsb >> 0) & 0x03);
    primaries.Bx = extract(edid[31], (bwLsb >> 6) & 0x03);
    primaries.By = extract(edid[32], (bwLsb >> 4) & 0x03);
    primaries.Wx = extract(edid[33], (bwLsb >> 2) & 0x03);
    primaries.Wy = extract(edid[34], (bwLsb >> 0) & 0x03);
    primaries.valid = true;

    return true;
}

// Extract the device instance ID from a monitor device path
// Device path format: \\?\DISPLAY#<HardwareID>#<InstanceID>#{GUID}
// Example: \\?\DISPLAY#DELA1EE#5&2a3b4c5d&0&UID12345#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}
// We extract "DELA1EE" (the hardware ID / EDID manufacturer+product code)
std::wstring ExtractHardwareIdFromPath(const std::wstring& devicePath) {
    // Find "DISPLAY#" and extract the next segment
    size_t displayPos = devicePath.find(L"DISPLAY#");
    if (displayPos == std::wstring::npos) {
        return L"";
    }

    size_t startPos = displayPos + 8;  // Skip "DISPLAY#"
    size_t endPos = devicePath.find(L'#', startPos);
    if (endPos == std::wstring::npos) {
        return L"";
    }

    return devicePath.substr(startPos, endPos - startPos);
}

// Read EDID from registry via SetupAPI for a specific monitor
static bool ReadEDIDFromRegistry(const wchar_t* targetHardwareId, std::vector<BYTE>& edidData) {
    // Get device info set for monitors
    HDEVINFO devInfo = SetupDiGetClassDevsW(&GUID_DEVINTERFACE_MONITOR, nullptr, nullptr,
                                             DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (devInfo == INVALID_HANDLE_VALUE) {
        std::cerr << "SetupDiGetClassDevs failed: " << GetLastError() << std::endl;
        return false;
    }

    bool found = false;
    SP_DEVINFO_DATA devInfoData = {};
    devInfoData.cbSize = sizeof(SP_DEVINFO_DATA);

    // Enumerate all monitor devices
    for (DWORD i = 0; SetupDiEnumDeviceInfo(devInfo, i, &devInfoData); i++) {
        // Get device instance ID
        wchar_t instanceId[256] = {};
        if (!SetupDiGetDeviceInstanceIdW(devInfo, &devInfoData, instanceId, 256, nullptr)) {
            continue;
        }

        // Instance ID format: DISPLAY\<HardwareID>\<UID>
        // Example: DISPLAY\DELA1EE\5&2a3b4c5d&0&UID12345
        std::wstring instIdStr(instanceId);

        // Extract hardware ID from instance ID
        size_t firstSlash = instIdStr.find(L'\\');
        if (firstSlash == std::wstring::npos) continue;
        size_t secondSlash = instIdStr.find(L'\\', firstSlash + 1);
        if (secondSlash == std::wstring::npos) secondSlash = instIdStr.length();

        std::wstring hwId = instIdStr.substr(firstSlash + 1, secondSlash - firstSlash - 1);

        // Compare with target (case-insensitive)
        std::wstring targetLower(targetHardwareId);
        std::wstring hwIdLower = hwId;
        std::transform(targetLower.begin(), targetLower.end(), targetLower.begin(), ::towlower);
        std::transform(hwIdLower.begin(), hwIdLower.end(), hwIdLower.begin(), ::towlower);

        if (hwIdLower != targetLower) {
            continue;
        }

        // Found matching device - open registry key for EDID
        HKEY hKey = SetupDiOpenDevRegKey(devInfo, &devInfoData, DICS_FLAG_GLOBAL, 0, DIREG_DEV, KEY_READ);
        if (hKey == INVALID_HANDLE_VALUE) {
            std::cerr << "SetupDiOpenDevRegKey failed: " << GetLastError() << std::endl;
            continue;
        }

        // Query EDID data size
        DWORD edidSize = 0;
        DWORD regType = 0;
        LONG result = RegQueryValueExW(hKey, L"EDID", nullptr, &regType, nullptr, &edidSize);
        if (result == ERROR_SUCCESS && regType == REG_BINARY && edidSize >= 128) {
            edidData.resize(edidSize);
            result = RegQueryValueExW(hKey, L"EDID", nullptr, nullptr, edidData.data(), &edidSize);
            if (result == ERROR_SUCCESS) {
                found = true;
            }
        }

        RegCloseKey(hKey);

        if (found) {
            break;
        }
    }

    SetupDiDestroyDeviceInfoList(devInfo);
    return found;
}

MonitorPrimaries GetMonitorPrimariesFromEDID(int monitorIndex) {
    MonitorPrimaries result = {};

    // Get DisplayInfo for this monitor to get the device path
    DisplayInfo displayInfo;
    if (!GetDisplayInfoForMonitor(monitorIndex, displayInfo)) {
        std::cerr << "Could not get display info for monitor " << monitorIndex << std::endl;
        return result;
    }

    // Extract hardware ID from device path
    std::wstring hardwareId = ExtractHardwareIdFromPath(displayInfo.devicePath);
    if (hardwareId.empty()) {
        std::cerr << "Could not extract hardware ID from device path" << std::endl;
        return result;
    }

    std::wcout << L"Looking for EDID with hardware ID: " << hardwareId << std::endl;

    // Read EDID from registry
    std::vector<BYTE> edidData;
    if (!ReadEDIDFromRegistry(hardwareId.c_str(), edidData)) {
        std::cerr << "Could not read EDID from registry" << std::endl;
        return result;
    }

    std::cout << "Found EDID data: " << edidData.size() << " bytes" << std::endl;

    // Parse chromaticity from EDID
    if (!ParseEDIDChromaticity(edidData.data(), edidData.size(), result)) {
        std::cerr << "Failed to parse EDID chromaticity data" << std::endl;
        return result;
    }

    std::cout << "EDID primaries for monitor " << monitorIndex << " (" ;
    std::wcout << displayInfo.name;
    std::cout << "):" << std::endl;
    std::cout << "  R(" << result.Rx << ", " << result.Ry << ")" << std::endl;
    std::cout << "  G(" << result.Gx << ", " << result.Gy << ")" << std::endl;
    std::cout << "  B(" << result.Bx << ", " << result.By << ")" << std::endl;
    std::cout << "  W(" << result.Wx << ", " << result.Wy << ")" << std::endl;

    return result;
}
