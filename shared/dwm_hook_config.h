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

    // Identity beacon (25H2 twin routing): while DwmHookSharedConfig::beaconActive, the host
    // shows a beaconSize x beaconSize solid window at this monitor's top-left corner painted
    // DwmHookBeaconRGB(beaconColorId); the DLL reads that corner of each overlay context's
    // back buffer and assigns the context to the monitor whose colour it sees. 0 = no beacon.
    uint32_t beaconColorId;
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

    // Identity beacon session (see DwmHookMonitorConfig::beaconColorId). The host raises
    // beaconActive with a new beaconGeneration for every session; the DLL probes each twin
    // context once per generation and drops the flag's effect when it clears.
    uint32_t beaconActive;           // 1 while the beacon windows are shown
    uint32_t beaconGeneration;       // increments per beacon session
    uint32_t beaconSize;             // beacon square edge in device pixels (host default 8)

    uint32_t _reserved[13];          // Future expansion
};
static_assert(sizeof(DwmHookSharedConfig) == 464, "DwmHookSharedConfig must be 464 bytes");
#pragma pack(pop)

// ---------------------------------------------------------------------------
// Identity beacon palette — shared by the host (paints) and the DLL (classifies)
// ---------------------------------------------------------------------------
// Six saturated primaries/secondaries: any transfer function or white-level scaling DWM
// applies when composing the window into the back buffer (8-bit, 10-bit, or scRGB FP16 in
// HDR / ACM) keeps each channel either "on" or "off", so classification is by channel
// dominance, not absolute value. Id 0 = none. Monitor i gets id (i % 6) + 1.
#define DWM_HOOK_BEACON_COLORS  6
#define DWM_HOOK_BEACON_SIZE    8

static inline uint32_t DwmHookBeaconColorIdForMonitor(uint32_t monitorIndex) {
    return (monitorIndex % DWM_HOOK_BEACON_COLORS) + 1;
}

// 0/1 per channel for a colour id (1..6 = R, G, B, C, M, Y); all zero for id 0 / out of range.
static inline void DwmHookBeaconRGB(uint32_t id, int* r, int* g, int* b) {
    static const int tbl[7][3] = { {0,0,0}, {1,0,0}, {0,1,0}, {0,0,1}, {0,1,1}, {1,0,1}, {1,1,0} };
    if (id > DWM_HOOK_BEACON_COLORS) id = 0;
    *r = tbl[id][0]; *g = tbl[id][1]; *b = tbl[id][2];
}

// Classify a linear-or-encoded RGB sample (any positive scale) into a beacon id, 0 when it is
// not a beacon colour (black, white, grey, a pastel, or a dim sample below `minLevel`).
// "on" channels must be >= 60% of the brightest channel, "off" channels <= 25% of it.
static inline uint32_t DwmHookBeaconClassify(float r, float g, float b, float minLevel) {
    float mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    if (!(mx >= minLevel)) return 0;   // also rejects NaN
    int on[3] = { r >= 0.6f * mx, g >= 0.6f * mx, b >= 0.6f * mx };
    int off[3] = { r <= 0.25f * mx, g <= 0.25f * mx, b <= 0.25f * mx };
    for (uint32_t id = 1; id <= DWM_HOOK_BEACON_COLORS; id++) {
        int er, eg, eb;
        DwmHookBeaconRGB(id, &er, &eg, &eb);
        int e[3] = { er, eg, eb };
        int ok = 1;
        for (int c = 0; c < 3; c++) {
            if (e[c] ? !on[c] : !off[c]) { ok = 0; break; }
        }
        if (ok) return id;
    }
    return 0;
}

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
//   ctx <hex pointer> <left> <top> <method>       method: unique|bpc|scan|pinned|order|legacy|
//                                                 provisional|replaced. `provisional` = a context
//                                                 that arrived while every twin position was held
//                                                 (DWM recreated one): a replacement GUESS, never
//                                                 loaded as a pin; `replaced` = that guess settled
//                                                 by liveness (the twin kept presenting, the old
//                                                 holder went silent) - a pin, `confirmed` cleared.
//                                                 On load, pins sharing a position are all dropped.
//                                                 `beacon` = identified positively by the identity
//                                                 beacon (colour read from the back buffer corner):
//                                                 authoritative, overrides pins/order, never a coin
//                                                 toss; loaded as a pin by the next injection.
// Lives OUTSIDE the LUT staging dir (which is wiped on every injection). One file PER dwm.exe
// (pid-suffixed): the host injects into every dwm.exe on the machine (fast-user-switch / RDP /
// lock-screen sessions each have one) and they must not overwrite each other's pins; the host
// reads the file of the dwm.exe in its own session.
// printf-style (the env var's percent signs are escaped for the formatter, expanded afterwards).
#define DWM_HOOK_ROUTING_FILE_FMT_A  "%%SYSTEMROOT%%\\Temp\\DesktopLUT_hook_routing_%lu.dat"
#define DWM_HOOK_ROUTING_FILE_FMT_W L"%%SYSTEMROOT%%\\Temp\\DesktopLUT_hook_routing_%lu.dat"
#define DWM_HOOK_ROUTING_MAGIC   "DesktopLUT-hook-routing"
