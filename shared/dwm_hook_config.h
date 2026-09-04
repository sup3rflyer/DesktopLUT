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
    float    sourcePeakNits;         // Tonemap INPUT peak (static source). WIRE CONTRACT: offset-checked
    float    targetPeakNits;         // Tonemap OUTPUT peak (display). in test_displayconfig.cpp. docs/NAMING.md §1.
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

// ---------------------------------------------------------------------------
// Twin-panel routing state (Windows 11 25H2)
// ---------------------------------------------------------------------------
// On 25H2 the hook cannot read a monitor position from a DWM overlay context; it matches a
// context to monitors.dat by back-buffer size, then bit depth, then FIRST-PRESENT ORDER. Two
// identical panels (3840x2160 10-bit FP16) are therefore a coin toss, re-rolled on every
// injection — and every LUT set/clear re-injects (2026-09-03: a whole calibration run measured
// the wrong panel). The DLL persists its context->position assignment in this file, keyed by
// the dwm.exe process identity, and honours it on the next injection of the SAME dwm.exe; the
// host reads it for state.get and rewrites it for hook.set_routing (swap / assign / confirm /
// clear). Text, one record per line:
//   DesktopLUT-hook-routing 1
//   session <dwmPid> <createTimeHigh> <createTimeLow>
//   mon <left> <top> <width> <height> <bpc>       one per monitors.dat entry (topology guard)
//   confirmed <0|1>                               a client verified the assignment through a meter;
//                                                 the DLL clears it on any fresh order-match
//   ctx <hex pointer> <left> <top> <method>       method: unique|bpc|scan|pinned|order|legacy
// Lives OUTSIDE the LUT staging dir (which is wiped on every injection).
#define DWM_HOOK_ROUTING_FILE_A  "%SYSTEMROOT%\\Temp\\DesktopLUT_hook_routing.dat"
#define DWM_HOOK_ROUTING_FILE_W L"%SYSTEMROOT%\\Temp\\DesktopLUT_hook_routing.dat"
#define DWM_HOOK_ROUTING_MAGIC   "DesktopLUT-hook-routing"
