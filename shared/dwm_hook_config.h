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

    // FALD correction, one packed word per monitors[] entry (same index). Taken from _reserved
    // rather than added to DwmHookMonitorConfig ON PURPOSE: that struct is 48 bytes behind a
    // static_assert with an offset contract checked in test_displayconfig.cpp (docs/NAMING.md §1)
    // and has no spare room, and there is no format-version field anywhere in this struct. Every
    // existing offset therefore stays put, sizeof stays 464, and a DwmHook.dll from an older build
    // still resident in a running dwm.exe reads what these words used to be — zero, i.e. FALD off —
    // instead of misreading a moved layout. Pack/unpack with the helpers below.
    // The panel parameter file itself does NOT come through here: it is staged as a file beside the
    // .cube LUTs and read once at attach (see DWM_HOOK_FALD_SUBDIR).
    uint32_t faldFlags[MAX_DWM_HOOK_MONITORS];

    uint32_t _reserved[5];           // Future expansion
};
static_assert(sizeof(DwmHookSharedConfig) == 464, "DwmHookSharedConfig must be 464 bytes");
#pragma pack(pop)

// ---------------------------------------------------------------------------
// FALD correction (mini-LED local dimming context-dependence) — host <-> hook
// ---------------------------------------------------------------------------
// Two channels, deliberately split by how often each changes:
//
//   * the per-panel parameter file (*.bin, `python -m dlc.fald.export`) is STAGED AS A FILE, in
//     this subdirectory of the LUT staging dir, and read once during DllMain like the .cube files.
//     Its own subdirectory because AddLUTs feeds every non-directory file whose name starts
//     "<int>_<int>" to the .cube parser, and a panel file is named the same way. Changing the panel
//     file therefore needs a re-injection — it is a rare, deliberate act (a new fit), and keeping it
//     out of the present path means no file polling inside dwm.exe.
//
//   * the three live settings below travel in faldFlags[] over the seqlock, so toggling the layer
//     or a debug view does NOT re-inject (a re-injection re-rolls twin-panel routing).
//
// Staged names mirror the LUTs: "<left>_<top>.bin" (SDR/ACM) and "<left>_<top>_hdr.bin" (HDR).
#define DWM_HOOK_FALD_SUBDIR_A  "fald"
#define DWM_HOOK_FALD_SUBDIR_W  L"fald"

// faldFlags[i] layout. debugMode is the same 0..10 scale as the overlay path (shared/fald_shader.h):
// 0 = normal output, 4 = identity passthrough (the H4 bit-for-bit check), others are debug views.
#define DWM_HOOK_FALD_ENABLED_BIT   0x00000001u
#define DWM_HOOK_FALD_DEBUG_SHIFT   1
#define DWM_HOOK_FALD_DEBUG_MASK    0x0000001Eu   // bits 1-4
#define DWM_HOOK_FALD_PEDMODE_BIT   0x00000020u   // bit 5 (per-channel pedestal)
#define DWM_HOOK_FALD_DEBUG_MAX     15u

// Starfield (work guide S1) and its glow-fill part (S2): ONE feature. Glow fill never runs without
// starfield — the host packs the glow bit only when both are on — and only on PQ (HDR) panel files.
#define DWM_HOOK_FALD_STAR_BIT      0x00000040u   // bit 6: starfield balancing
#define DWM_HOOK_FALD_GLOW_BIT      0x00000080u   // bit 7: its glow-fill part

static inline uint32_t DwmHookFaldPack(int enabled, uint32_t debugMode, int pedMode, int star = 0, int glow = 0) {
    if (debugMode > DWM_HOOK_FALD_DEBUG_MAX) debugMode = 0;
    return (enabled ? DWM_HOOK_FALD_ENABLED_BIT : 0u)
         | ((debugMode << DWM_HOOK_FALD_DEBUG_SHIFT) & DWM_HOOK_FALD_DEBUG_MASK)
         | (pedMode ? DWM_HOOK_FALD_PEDMODE_BIT : 0u)
         | (star ? DWM_HOOK_FALD_STAR_BIT : 0u)
         | ((star && glow) ? DWM_HOOK_FALD_GLOW_BIT : 0u);
}
static inline int      DwmHookFaldEnabled(uint32_t w)  { return (w & DWM_HOOK_FALD_ENABLED_BIT) != 0; }
static inline uint32_t DwmHookFaldDebugMode(uint32_t w) { return (w & DWM_HOOK_FALD_DEBUG_MASK) >> DWM_HOOK_FALD_DEBUG_SHIFT; }
static inline int      DwmHookFaldPedMode(uint32_t w)  { return (w & DWM_HOOK_FALD_PEDMODE_BIT) != 0; }
static inline int      DwmHookFaldStar(uint32_t w)     { return (w & DWM_HOOK_FALD_STAR_BIT) != 0; }
static inline int      DwmHookFaldGlow(uint32_t w)     { return (w & DWM_HOOK_FALD_STAR_BIT) && (w & DWM_HOOK_FALD_GLOW_BIT); }

// ---------------------------------------------------------------------------
// Tuning tail — the starfield / glow-fill parameters (floats), appended AFTER DwmHookSharedConfig
// ---------------------------------------------------------------------------
// The head struct above is frozen at 464 bytes (an older DwmHook.dll still resident in dwm.exe maps
// exactly that much). The host creates the mapping sizeof(DwmHookSharedConfigEx) long; an old DLL
// maps the first 464 bytes and never sees the tail, a new DLL paired with an old host (464-byte
// mapping) fails to map the full size, falls back to the head and runs starfield / glow on their
// defaults. The tail is written inside the SAME seqlock as the head (the head's `version`), so a
// reader copies head + tail in one consistent snapshot. `magic` + `layoutVersion` + `tuningBytes`
// let a reader reject a tail it does not understand instead of misreading it.
// Host <-> DLL signalling for the FALD layer in hook mode. Auto-reset events in the SESSION namespace (Local\: dwm.exe
// and the host share the user's session; another session's dwm.exe cannot cross-signal), created by the host, opened by
// the DLL (open retried rarely, never per frame), signalled with SetEvent only (no wait in the present path).
//
// LED-lag settle hold. DWM presents nothing on a static desktop, but the temporal state keeps moving for a few
// refreshes after the last content change. Per monitor (named by its desktop left/top):
//   SETTLE  — signalled on every run while that monitor still owes settle frames;
//   CONTENT — signalled on every run that carried new content (content is flowing: DWM is presenting anyway).
// The host kicks a monitor — re-paints a 1 x 1 px click-through window at its top-left pixel once per composition —
// only while it owes settle frames AND no content arrived since the last composition, and stops once SETTLE has been
// quiet for a while. The DLL does not count a present whose dirty rects all lie inside a KICK_ZONE box at any corner
// as new content (the kick must not re-arm the hold it exists to finish; any corner: a rotated back buffer).
#define DWM_HOOK_FALD_SETTLE_EVENT_FMT  L"Local\\DesktopLUT_DwmHook_FaldSettle_%d_%d"
#define DWM_HOOK_FALD_CONTENT_EVENT_FMT L"Local\\DesktopLUT_DwmHook_FaldContent_%d_%d"
// PRIME — the DLL asks for one full-screen recomposition: an enabled FALD monitor whose clean copy is not (or no
// longer) primed. Throttled on both sides (at most ~1 per second).
#define DWM_HOOK_FALD_PRIME_EVENT       L"Local\\DesktopLUT_DwmHook_FaldPrime"
#define DWM_HOOK_FALD_KICK_PX       1
#define DWM_HOOK_FALD_KICK_ZONE_PX  16

#define DWM_HOOK_TAIL_MAGIC          0x444C4654u   // 'TFLD'
#define DWM_HOOK_TAIL_LAYOUT_VERSION 1u

struct DwmHookFaldTuning {           // per monitors[] index: the settings of the mode it is in NOW
    // starfield (CB words 52-65; same fields and clamps as src FaldStarfieldSettings)
    float    starEven, starLift, starTargetGain, starTargetSigma;
    float    starKeepNits, starCapNits, starStrength, starAreaLo;
    float    starAreaHi, starPeakHi, starNbLo, starNbHi;
    uint32_t starReach, starEvenReach;
    // glow fill (CB words 76-78; same fields and clamps as src FaldGlowSettings)
    float    glowStrength, glowCapNits;
    uint32_t glowReach;
    // LED lag = the temporal drive state (shared/fald_temporal.h FaldTemporalSettings) + the monitor's refresh period
    // (mode 3's grid). Taken from _reserved: a tail from a host that predates them reads 0 = LED lag off.
    uint32_t tempMode;               // 0 off, 1 both fields, 2 B_true only, 3 panel clock
    float    tempTauRiseMs, tempTauFallMs;
    uint32_t tempDelayFrames;
    float    tempClockClosure;
    int32_t  tempClockParity;        // -1 unknown, 0, 1
    float    refreshMs;              // exact nominal refresh period of this monitor (DisplayConfig vSyncFreq); 0 = unknown
    uint32_t _reserved[8];           // room for later fields without a layout bump (24 used + 8 = 32 words)
};
static_assert(sizeof(DwmHookFaldTuning) == 128, "DwmHookFaldTuning must be 128 bytes");

struct DwmHookSharedConfigTail {
    uint32_t magic;                  // DWM_HOOK_TAIL_MAGIC
    uint32_t layoutVersion;          // DWM_HOOK_TAIL_LAYOUT_VERSION
    uint32_t tuningBytes;            // sizeof(DwmHookFaldTuning) as the writer knew it
    uint32_t _pad;
    DwmHookFaldTuning fald[MAX_DWM_HOOK_MONITORS];
};

struct DwmHookSharedConfigEx {
    DwmHookSharedConfig     head;    // offset 0: the frozen layout, seqlock `version` first
    DwmHookSharedConfigTail tail;    // offset 464
};
static_assert(sizeof(DwmHookSharedConfigTail) == 16 + 128 * MAX_DWM_HOOK_MONITORS, "tail layout");
static_assert(sizeof(DwmHookSharedConfigEx) == 464 + sizeof(DwmHookSharedConfigTail), "head must stay at 464");

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
