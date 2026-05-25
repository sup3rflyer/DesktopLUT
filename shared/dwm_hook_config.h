// DesktopLUT - dwm_hook_config.h
// Shared memory IPC structure between host (DesktopLUT.exe) and DWM hook (DwmHook.dll)
// Standalone header — no dependencies on either project's internals.

#pragma once
#include <stdint.h>

#define DWM_HOOK_CONFIG_NAME  L"Global\\DesktopLUT_DwmHook_Config"
#define MAX_DWM_HOOK_MONITORS 8

enum DwmHookTonemapCurve : uint32_t {
    DWMHOOK_TONEMAP_BT2390   = 0,
    DWMHOOK_TONEMAP_SOFTCLIP = 1,
    DWMHOOK_TONEMAP_REINHARD = 2,
    DWMHOOK_TONEMAP_BT2446A  = 3,
    DWMHOOK_TONEMAP_HARDCLIP = 4,
};

#pragma pack(push, 4)
struct DwmHookMonitorConfig {
    int32_t  left, top;              // Desktop position (match key)
    uint32_t width, height;
    uint32_t bpc;
    uint32_t isHdr;                  // 1=HDR, 0=SDR/ACM

    // Tonemap (HDR only)
    uint32_t tonemapEnabled;
    DwmHookTonemapCurve tonemapCurve;
    float    sourcePeakNits;         // Static source peak
    float    targetPeakNits;         // Display peak
    uint32_t dynamicPeak;            // 0=static, 1=dynamic

    uint32_t _pad[1];               // Align to 48 bytes
};
static_assert(sizeof(DwmHookMonitorConfig) == 48, "DwmHookMonitorConfig must be 48 bytes");

struct DwmHookSharedConfig {
    // Seqlock version: odd = write in progress, even = complete.
    // Accessed via volatile + explicit fences (not std::atomic) because:
    // (1) struct is memcpy'd across process boundary — std::atomic memcpy is UB
    // (2) MSVC /volatile:ms provides acquire/release on x86-64
    // (3) Interlocked ops add ~20 cycles per Present hook call for no benefit on x86-64
    volatile uint32_t version;
    uint32_t numMonitors;
    uint32_t hostPid;                // Replaces host.pid file
    uint32_t lutReloadFlag;          // Host sets 1, hook resets 0 after reload

    DwmHookMonitorConfig monitors[MAX_DWM_HOOK_MONITORS];

    uint32_t _reserved[16];          // Future expansion
};
static_assert(sizeof(DwmHookSharedConfig) == 464, "DwmHookSharedConfig must be 464 bytes");
#pragma pack(pop)
