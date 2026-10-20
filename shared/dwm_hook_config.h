// DesktopLUT - dwm_hook_config.h
// Shared memory IPC structure between host (DesktopLUT.exe) and DWM hook (DwmHook.dll)
// Standalone header — no dependencies on either project's internals.

#pragma once
#include <stdint.h>
#include <stddef.h>  // offsetof

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
    // .cube LUTs and read at attach (see DWM_HOOK_FALD_SUBDIR); a later change of it travels in its own
    // section (DwmHookFaldPanelBlob below), announced by DwmHookFaldTuning::panelGen.
    uint32_t faldFlags[MAX_DWM_HOOK_MONITORS];

    // HDR output dither kill switch (shared/hdr_dither.h), global. Taken from _reserved for the same reason as
    // faldFlags: every offset and sizeof stay put, and 0 = dither ON, so a host that predates the field (whose
    // reserved words read 0) leaves the dither on. 1 = off (INI [General] HdrDither=0 / the GUI checkbox).
    uint32_t hdrDitherOff;

    // 3D LUT stage bypass (the interface's stage switch): bit i = monitors[i]'s cube is skipped for the mode
    // it is in RIGHT NOW (the host picks the SDR or HDR flag by monitors[i].isHdr, as for faldFlags). The
    // cube stays staged and loaded, so switching back is instant. Taken from _reserved for the same reason
    // as faldFlags: 0 = applied, so a host that predates the field leaves every cube on.
    uint32_t lutOffMask;

    // Live edit preview (DwmHookMhcPreview below): the generation of the preview block the host last wrote, 0 = no
    // preview. Taken from _reserved like the fields above: a host that predates it leaves 0 (no preview).
    uint32_t mhcPreviewGen;

    uint32_t _reserved[2];           // Future expansion
};
static_assert(sizeof(DwmHookSharedConfig) == 464, "DwmHookSharedConfig must be 464 bytes");
static_assert(offsetof(DwmHookSharedConfig, mhcPreviewGen) == 464 - 3 * sizeof(uint32_t),
              "mhcPreviewGen must sit where _reserved[0] was");
static_assert(offsetof(DwmHookSharedConfig, hdrDitherOff) == 464 - 5 * sizeof(uint32_t),
              "hdrDitherOff must sit where _reserved[0] was");
static_assert(offsetof(DwmHookSharedConfig, lutOffMask) == 464 - 4 * sizeof(uint32_t),
              "lutOffMask must sit where _reserved[1] was");
static_assert(MAX_DWM_HOOK_MONITORS <= 32, "lutOffMask holds one bit per monitor");
#pragma pack(pop)

// ---------------------------------------------------------------------------
// FALD correction (mini-LED local dimming context-dependence) — host <-> hook
// ---------------------------------------------------------------------------
// Three channels, deliberately split by how often each changes:
//
//   * the per-panel parameter file (*.bin, `python -m dlc.fald.export`) is STAGED AS A FILE, in
//     this subdirectory of the LUT staging dir, and read once during DllMain like the .cube files.
//     Its own subdirectory because AddLUTs feeds every non-directory file whose name starts
//     "<int>_<int>" to the .cube parser, and a panel file is named the same way. That is the FIRST
//     load of an injection (and of every re-injection: it always stages the current file).
//
//   * a CHANGE of the panel file while the hook is resident (another path, the same path re-exported,
//     an explicit reload) is delivered live through shared memory — see "FALD panel file live
//     delivery" below — so the layer never blinks off for a re-injection: a blind A/B of two panel
//     files must show the files, not the blink. No file I/O inside dwm.exe either way.
//
//   * the three live settings below travel in faldFlags[] over the seqlock, so toggling the layer
//     or a debug view does NOT re-inject (a re-injection re-rolls twin-panel routing).
//
// Staged names mirror the LUTs: "<left>_<top>.bin" (SDR/ACM) and "<left>_<top>_hdr.bin" (HDR).
#define DWM_HOOK_FALD_SUBDIR_A  "fald"
#define DWM_HOOK_FALD_SUBDIR_W  L"fald"

// faldFlags[i] layout. debugMode is the same 0..9 scale as the overlay path (shared/fald_shader.h):
// 0 = normal output, 4 = identity passthrough (the H4 bit-for-bit check), others are debug views.
#define DWM_HOOK_FALD_ENABLED_BIT   0x00000001u
#define DWM_HOOK_FALD_DEBUG_SHIFT   1
#define DWM_HOOK_FALD_DEBUG_MASK    0x0000001Eu   // bits 1-4
#define DWM_HOOK_FALD_PEDMODE_BIT   0x00000020u   // bit 5 (per-channel pedestal)
#define DWM_HOOK_FALD_DEBUG_MAX     15u

// Starfield (work guide S1). Bit 7 was the glow fill's (S2, removed 2026-10-07): never set, ignored.
#define DWM_HOOK_FALD_STAR_BIT      0x00000040u   // bit 6: starfield balancing
// The frozen fade stack ASF10 + T11_TMTC (shared/fald_fade.h): bit 8. Bit 9 = item L (the 1-3-nit extension, round 12):
// RESERVED, never set by this host and ignored by this DLL. Bits 10-31 free. A host that predates bit 8 sends 0 = off.
#define DWM_HOOK_FALD_FADE_BIT      0x00000100u   // bit 8: flank fade
#define DWM_HOOK_FALD_LOWEXT_BIT    0x00000200u   // bit 9: item L (reserved)

static inline uint32_t DwmHookFaldPack(int enabled, uint32_t debugMode, int pedMode, int star = 0, int fade = 0) {
    if (debugMode > DWM_HOOK_FALD_DEBUG_MAX) debugMode = 0;
    return (enabled ? DWM_HOOK_FALD_ENABLED_BIT : 0u)
         | ((debugMode << DWM_HOOK_FALD_DEBUG_SHIFT) & DWM_HOOK_FALD_DEBUG_MASK)
         | (pedMode ? DWM_HOOK_FALD_PEDMODE_BIT : 0u)
         | (star ? DWM_HOOK_FALD_STAR_BIT : 0u)
         | (fade ? DWM_HOOK_FALD_FADE_BIT : 0u);
}
static inline int      DwmHookFaldEnabled(uint32_t w)  { return (w & DWM_HOOK_FALD_ENABLED_BIT) != 0; }
static inline uint32_t DwmHookFaldDebugMode(uint32_t w) { return (w & DWM_HOOK_FALD_DEBUG_MASK) >> DWM_HOOK_FALD_DEBUG_SHIFT; }
static inline int      DwmHookFaldPedMode(uint32_t w)  { return (w & DWM_HOOK_FALD_PEDMODE_BIT) != 0; }
static inline int      DwmHookFaldStar(uint32_t w)     { return (w & DWM_HOOK_FALD_STAR_BIT) != 0; }
static inline int      DwmHookFaldFade(uint32_t w)     { return (w & DWM_HOOK_FALD_FADE_BIT) != 0; }

// ---------------------------------------------------------------------------
// Tuning tail — the starfield parameters (floats), appended AFTER DwmHookSharedConfig
// ---------------------------------------------------------------------------
// The head struct above is frozen at 464 bytes (an older DwmHook.dll still resident in dwm.exe maps
// exactly that much). The host creates the mapping sizeof(DwmHookSharedConfigEx) long; an old DLL
// maps the first 464 bytes and never sees the tail, a new DLL paired with an old host (464-byte
// mapping) fails to map the full size, falls back to the head and runs starfield on its
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

// ---------------------------------------------------------------------------
// Live edit preview (src/mhc_preview.h)
// ---------------------------------------------------------------------------
// While a display's MHC correction grayscale is edited, its scanout runs an identity profile and the hook applies the
// display's profile instead, as the last step of its pass (shared/mhc_scanout_hlsl.h): exactly the matrix and LUT
// entries the bake writes, so the preview is the bake. Its own mapping: the LUTs are 48 KB, and the config is copied
// onto the present thread's stack on every change. The host writes the block under its own seqlock, THEN publishes
// its `gen` in DwmHookSharedConfig::mhcPreviewGen (0 = no preview). The DLL copies the block when that generation
// moves (accepting it only when `gen` matches), uploads it, and writes the generation to `ackGen`: the host knows the
// resident DLL applies it (an older DLL never acks, and the host then does not start the edit).
#define DWM_HOOK_MHC_PREVIEW_NAME     L"Global\\DesktopLUT_DwmHook_MhcPreview"
#define DWM_HOOK_MHC_PREVIEW_MAGIC    0x56504D44u   // 'DMPV'
#define DWM_HOOK_MHC_PREVIEW_LUT_MAX  4096

#pragma pack(push, 4)
struct DwmHookMhcPreview {
    volatile uint32_t version;       // seqlock: odd = write in progress, even = complete
    uint32_t magic;                  // DWM_HOOK_MHC_PREVIEW_MAGIC
    uint32_t gen;                    // the generation this block is (DwmHookSharedConfig::mhcPreviewGen)
    int32_t  left, top;              // the monitor (DwmHookMonitorConfig match key)
    uint32_t isHdr;                  // the mode it is for: applied only while the monitor is in it
    uint32_t lutSize;                // entries per channel: 1024 SDR, 4096 HDR
    uint32_t _pad;
    float    matrix[9];              // as-applied linear RGB->RGB, row-major (MhcScanout::applied)
    uint32_t _pad2[3];
    float    lut[3][DWM_HOOK_MHC_PREVIEW_LUT_MAX];   // R, G, B: the profile's LUT entries (s15Fixed16 values)
    volatile uint32_t ackGen;        // written by the DLL (outside the seqlock): the generation it now applies
};
#pragma pack(pop)
static_assert(offsetof(DwmHookMhcPreview, matrix) == 32, "DwmHookMhcPreview layout");
static_assert(offsetof(DwmHookMhcPreview, lut) == 80, "DwmHookMhcPreview layout");
static_assert(sizeof(DwmHookMhcPreview) == 80 + 3 * 4 * DWM_HOOK_MHC_PREVIEW_LUT_MAX + 4, "DwmHookMhcPreview layout");

#define DWM_HOOK_TAIL_MAGIC          0x444C4654u   // 'TFLD'
#define DWM_HOOK_TAIL_LAYOUT_VERSION 1u

struct DwmHookFaldTuning {           // per monitors[] index: the settings of the mode it is in NOW
    // starfield (CB words 52-65; same fields and clamps as src FaldStarfieldSettings)
    float    starEven, starLift, starTargetGain, starTargetSigma;
    float    starKeepNits, starCapNits, starStrength, starAreaLo;
    float    starAreaHi, starPeakHi, starNbLo, starNbHi;
    uint32_t starReach, starEvenReach;
    // three words that held the glow fill's settings (S2, removed 2026-10-07): written 0, never read. Kept so the
    // fields below stay where an already-resident DLL (layoutVersion 1) reads them.
    uint32_t _retiredGlow[3];
    // LED lag = the temporal drive state (shared/fald_temporal.h FaldTemporalSettings) + the monitor's refresh period
    // (mode 3's grid). Taken from _reserved: a tail from a host that predates them reads 0 = LED lag off.
    uint32_t tempMode;               // 0 off, 1 both fields, 2 B_true only, 3 panel clock
    float    tempTauRiseMs, tempTauFallMs;
    uint32_t tempDelayFrames;
    float    tempClockClosure;
    int32_t  tempClockParity;        // -1 unknown, 0, 1
    float    refreshMs;              // exact nominal refresh period of this monitor (DisplayConfig vSyncFreq); 0 = unknown
    // FALD panel file live delivery (DwmHookFaldPanelBlob below): the generation of the file this monitor's layer is
    // to hold, per mode — index 0 = SDR (ACM), 1 = HDR. BOTH modes, unlike the fields above (a file delivered for the
    // other mode waits there). 0 = the file staged at injection. Taken from _reserved: a host that predates it writes
    // 0, and a DLL that predates it never reads it (the host then re-injects: DWM_HOOK_FALD_PANEL_LIVE_EVENT).
    uint32_t panelGen[2];
    uint32_t _reserved[6];           // room for later fields without a layout bump (26 used + 6 = 32 words)
};
static_assert(sizeof(DwmHookFaldTuning) == 128, "DwmHookFaldTuning must be 128 bytes");
static_assert(offsetof(DwmHookFaldTuning, panelGen) == 96, "panelGen must sit where _reserved[0] was");

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
// FALD panel file live delivery (no re-injection)
// ---------------------------------------------------------------------------
// The panel file is staged as a file and read at attach (DWM_HOOK_FALD_SUBDIR above). A CHANGE of it while the hook is
// resident reaches the DLL through shared memory instead (re-injecting blinked the layer off for ~1 s on every switch,
// which made a live A/B of two panel files impossible to judge). Per delivery the host
//   1. reads and validates the file with the parser the DLL uses (shared/fald_panel.h ParseFaldPanelParams), its
//      transfer against the mode, its lattice and zone sweep against the display — a file it refuses is never
//      delivered (the host re-injects instead, the old path);
//   2. creates a NEW named section DWM_HOOK_FALD_PANEL_NAME_FMT (its pid + a host-wide generation, never 0): a
//      DwmHookFaldPanelBlob header, then the file's bytes unchanged. Written completely, then never again;
//   3. publishes the generation in the shared config (DwmHookFaldTuning::panelGen[mode] of that monitor's entry,
//      inside the config's seqlock) — the DLL cannot know the name before the section is complete — and only then
//      closes the section it superseded.
// The DLL, on a present of that monitor whose config carries a generation it has not processed for that mode, opens
// the section, copies the header out (SEH-guarded), checks it (magic, size, generation, position, mode:
// shared/fald_panel_live.h FaldPanelBlobCheck) and parses the payload straight from the view with the staged file's
// parser (+ transfer vs mode), under the panel table's lock (TRIED: a present that finds it busy takes the delivery
// on a later present). Accepted: it replaces the file the DLL holds for that (position, mode), keeping the last BUILT
// one; the monitor's layer rebuilds its panel resources at its next run in that mode and switches over between two
// presents, keeping its clean source primed. A file whose resources cannot be built goes back to the last built one
// (the layer keeps running on it; a layer not running stays off). Refused: nothing changes. Either way the DLL writes
// its verdict into the section (ackGen / ackStatus), and a generation is processed once (even with a full table). The
// host follows the verdicts: its staging set and state.get name what the hook HOLDS, and a delivery refused, not
// buildable or unanswered falls back to the re-injection, once per failed file. One section per generation, so a
// reader can never see a half-rewritten block. A re-injection stages the CURRENT file again and starts every
// generation over at 0 (= the staged file). Two dwm.exe processes (a second session) read the same sections and share
// one verdict word per section: the last writer's verdict is what the host sees (status reporting only).
#define DWM_HOOK_FALD_PANEL_NAME_FMT    L"Global\\DesktopLUT_DwmHook_FaldPanel_%lu_%lu"   // host pid, generation
// Created by a DLL that takes live deliveries (beside the heartbeat event, closed with it). An older resident DLL has
// none: the host then changes a panel file by re-injecting, as before.
#define DWM_HOOK_FALD_PANEL_LIVE_EVENT  L"Global\\DesktopLUT_DwmHook_FaldPanelLive"
#define DWM_HOOK_FALD_PANEL_MAGIC       0x504C4446u   // 'FDLP'
// A payload above this is refused on both sides (real panel files are < 1 MB; the parser's own bounds allow ~34 MB).
#define DWM_HOOK_FALD_PANEL_MAX_BYTES   (64u << 20)

// DwmHookFaldPanelBlob::ackStatus (written by the DLL)
#define DWM_HOOK_FALD_PANEL_ACK_NONE         0u   // not processed yet
#define DWM_HOOK_FALD_PANEL_ACK_INGESTED     1u   // parsed and held for that (position, mode): the layer takes it at its next run there
#define DWM_HOOK_FALD_PANEL_ACK_APPLIED      2u   // the layer runs on it
#define DWM_HOOK_FALD_PANEL_ACK_REFUSED      3u   // the block or the file is invalid: the previous file stays
#define DWM_HOOK_FALD_PANEL_ACK_BUILD_FAILED 4u   // its GPU resources could not be built: the DLL went back to the last
                                                  // file it built (a layer not running stays off, as with a staged file)

#pragma pack(push, 4)
struct DwmHookFaldPanelBlob {
    uint32_t magic;                  // DWM_HOOK_FALD_PANEL_MAGIC
    uint32_t headerBytes;            // sizeof(DwmHookFaldPanelBlob): the payload starts here
    uint32_t gen;                    // the generation this section is (its name carries it too)
    int32_t  left, top;              // the monitor (DwmHookMonitorConfig match key)
    uint32_t isHdr;                  // the mode the file is for: 1 = HDR (a PQ fit), 0 = SDR under ACM (a gamma fit)
    uint32_t payloadBytes;           // the panel file's size; its bytes follow the header unchanged
    uint32_t _pad;
    volatile uint32_t ackGen;        // written by the DLL: the generation of its verdict (== gen once processed)
    volatile uint32_t ackStatus;     // written by the DLL: DWM_HOOK_FALD_PANEL_ACK_*
    uint32_t _reserved[2];
};
#pragma pack(pop)
static_assert(sizeof(DwmHookFaldPanelBlob) == 48, "DwmHookFaldPanelBlob layout");
static_assert(offsetof(DwmHookFaldPanelBlob, ackGen) == 32, "DwmHookFaldPanelBlob layout");

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
