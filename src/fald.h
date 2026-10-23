// FALD (mini-LED local dimming) context-dependence correction — the OVERLAY path's layer (HDR, and SDR under
// Windows ACM where the overlay frame is FP16 scRGB too). This file never runs in DWM hook mode; there the
// hook runs the same layer itself (dwm_hook/hook_fald.h): the core, starfield, LED lag (panel clock, settle
// hold) and the flank fade, out of the same HLSL and the same panel-file reader.
// Design + measurements: DLC/docs/fald-shader-design.md, DLC/docs/fald-spatial-probe-2026-09-10.md.
// The per-panel parameter file (*.bin) is produced by `python -m dlc.fald.export <fit.json> <out.bin>`
// (layout documented there); the shaders live in shared/fald_shader.h — shared with the DWM hook, so both
// render paths run the same HLSL.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

// The panel parameter file and the pure functions over it live in shared/ so the DWM hook parses
// the same bytes with the same code (FaldPanelParams, LoadFaldPanelParams, the boost helpers,
// FALD_CB_BYTES, the transfer / boost-rule constants).
#include "../shared/fald_panel.h"
#include "../shared/fald_exempt.h"   // FaldExemptRect: DesktopLUT's own windows, exempt from the FALD views
#include "../shared/fald_fade_governor.h"   // the flank fade's safety governor (FaldResources::fadeGov)
#include "../shared/fald_core_governor.h"   // the core layer's rate-cap governor (FaldResources::coreGov)
#include "../shared/fald_change.h"          // the change-driven path: touched zones, the rules, the argument slots
#include "../shared/fald_conv_tune.h"       // the conv pass's variant auto-tune (FaldResources::convTune)

struct MonitorContext;
struct FaldSettings;
struct FaldStarfieldSettings;
struct FaldResources;

// Starfield balancing (EXPERIMENT, default off; work guide ticket S1). The complete rules: the header above
// g_faldStarStatSource in fald_shader.h = the module docstring of DLC dlc/fald/starfield.py (the reference). In short:
// a zone with star-like content (a small lit area ABOVE the zone's darkest pixel) has its peak pulled part of the way
// (`even`, log domain) toward the local TARGET = exp(mean + targetSigma std of ln peak) of the star-like peaks around it
// (tapered window, flank zones of a straddling star excluded), never below keepNits, optionally under an absolute
// ceiling; peaks below the target are optionally lifted (`lift`, speck pixels only); solid content protects its neighbourhood through a tapered field (`reach` zones fully, one more at
// half) that is interpolated per pixel; per pixel ONE hue-preserving scale, only in zones that hold a speck, the zone
// fields bilinear between zone centres. The balanced frame is what the rest of the layer sees (statistic rounds, boost
// flags, Correct, output). Off = no resources, no dispatches, the shaders' results bit-identical. Limits of the two reaches:
constexpr unsigned int FALD_STAR_EVEN_REACH_MAX = 12;
constexpr unsigned int FALD_STAR_REACH_MAX = 4;
// Clamp every field to its documented range (NaN -> the default; area / neighbour pairs kept ordered lo <= hi).
void FaldStarfieldClamp(FaldStarfieldSettings& s);

// Temporal drive state ("LED lag"): constants, the time law, the panel clock, the settle hold and the per-run
// orchestration live in shared/fald_temporal.h — ONE implementation for this path and the DWM hook. FaldResources
// derives from FaldTemporalState (below), so r->clkIndex etc. and FaldPanelClockStep(r, ...) work as before.
#include "../shared/fald_temporal.h"

// The panel parameter file itself — FaldPanelParams, LoadFaldPanelParams, the header peeks, the boost helpers and
// FaldTransferMatchesMode / FaldLatticeFits — is shared with the DWM hook: see shared/fald_panel.h, included above.


// Per-monitor GPU resources (heap-owned by MonitorContext::fald; released with the monitor's D3D
// resources and on resize — recreated lazily on the next frame).
struct FaldResources : FaldTemporalState {   // the temporal bookkeeping fields: shared/fald_temporal.h
    FaldPanelParams params;
    std::wstring paramsPath;
    unsigned int reloadSeq = 0;              // FaldSettings::reloadSeq the resources were built for
    unsigned long long fileSize = 0, fileMtime = 0;   // stamp of the params file at Build (re-export in place -> rebuild)
    unsigned int fileCheckCounter = 0;       // frames since the stamp was last polled
    int width = 0, height = 0;
    bool builtForHdr = false;                // monitor mode the resources were built for (transfer check)
    bool refusedByFile = false;              // the last Build failed on the FILE (unreadable, bad header, transfer/mode
                                             // mismatch, lattice does not fit): deterministic until the file, the path,
                                             // the mode or reloadSeq changes — no periodic re-read, no awake overlay
    bool valid = false;
    std::string lastError;
    // full-resolution intermediate: the main shader renders here, the FALD pixel pass reads it
    ID3D11Texture2D* inter = nullptr;
    ID3D11RenderTargetView* interRTV = nullptr;
    ID3D11ShaderResourceView* interSRV = nullptr;
    // temporal drive state (pass 1b): the filtered drive of the current round and the committed state
    ID3D11Texture2D* driveFiltTex = nullptr; ID3D11UnorderedAccessView* driveFiltUAV = nullptr; ID3D11ShaderResourceView* driveFiltSRV = nullptr;
    ID3D11Texture2D* driveStateTex = nullptr; ID3D11UnorderedAccessView* driveStateUAV = nullptr; ID3D11ShaderResourceView* driveStateSRV = nullptr;
    // pipeline delay ring: the round-1 instantaneous drive maps of the last FALD_DELAY_MAX frames (oldest overwritten);
    // with delayFrames = n the temporal pass is fed ring[head - n] once the ring holds n maps, else the current drive
    ID3D11Texture2D* delayTex[FALD_DELAY_MAX] = {}; ID3D11UnorderedAccessView* delayUAV[FALD_DELAY_MAX] = {}; ID3D11ShaderResourceView* delaySRV[FALD_DELAY_MAX] = {};
    // (ring indices, stateValid, temporalMode, the blend factors, the settle hold, dt: FaldTemporalState)
    // panel clock (temporal mode 3): four cols x rows R32F textures, created on the first frame the mode is on and
    // released when it goes off (modes 0-2 never see them). clkState[p] = the LED state of parity clock p (advanced in
    // place by the clock pass), clkPrev = the previous frame's round-1 instantaneous drives (every tick's target),
    // clkEst = this frame's map for the estimate kernel (the one for the real-spread kernel reuses driveFiltTex).
    // stateValid = the clocks hold a committed state. (The clock's time law and its fields: FaldTemporalState.)
    ID3D11Texture2D* clkStateTex[2] = {}; ID3D11UnorderedAccessView* clkStateUAV[2] = {}; ID3D11ShaderResourceView* clkStateSRV[2] = {};
    ID3D11Texture2D* clkPrevTex = nullptr; ID3D11UnorderedAccessView* clkPrevUAV = nullptr; ID3D11ShaderResourceView* clkPrevSRV = nullptr;
    ID3D11Texture2D* clkEstTex = nullptr;  ID3D11UnorderedAccessView* clkEstUAV = nullptr;  ID3D11ShaderResourceView* clkEstSRV = nullptr;
    bool clkFailLogged = false;
    unsigned int clkRetryCounter = 0;        // frames since the clock textures failed to create (retry every ~300, like Build)
    // panel tables
    ID3D11Texture2D* curveTex = nullptr;   ID3D11ShaderResourceView* curveSRV = nullptr;
    // the conv pass's kernel tables, re-laid out for its variant (shared/fald_panel.h FaldConvTableLayout; one float view
    // at t2 / t3, one float4 view of the same buffer at t36 / t37)
    // The conv pass's kernel tables for EVERY variant (shared/fald_conv_tune.h: the auto-tune times them all and switches
    // exactly): these arrays own them; kTrueBuf / kTrueSRV / kTrue4SRV (and kEst) below ALIAS the variant in use
    // (SelectConvVariant), so the passes bind as before.
    ID3D11Buffer* kTrueBufV[FALD_CONV_TUNE_VARIANTS] = {}; ID3D11ShaderResourceView* kTrueSRVV[FALD_CONV_TUNE_VARIANTS] = {}; ID3D11ShaderResourceView* kTrue4SRVV[FALD_CONV_TUNE_VARIANTS] = {};
    ID3D11Buffer* kEstBufV[FALD_CONV_TUNE_VARIANTS] = {};  ID3D11ShaderResourceView* kEstSRVV[FALD_CONV_TUNE_VARIANTS] = {};  ID3D11ShaderResourceView* kEst4SRVV[FALD_CONV_TUNE_VARIANTS] = {};
    ID3D11Buffer* kTrueBuf = nullptr;      ID3D11ShaderResourceView* kTrueSRV = nullptr; ID3D11ShaderResourceView* kTrue4SRV = nullptr;   // (aliases)
    ID3D11Buffer* kEstBuf = nullptr;       ID3D11ShaderResourceView* kEstSRV = nullptr;  ID3D11ShaderResourceView* kEst4SRV = nullptr;    // (aliases)
    unsigned int convVariant = 0;          // the variant in use: 0..3 <-> 1 / 2 / 4 / 8 sub-offsets per thread (the policy's, the cache's or the tuned one)
    // the auto-tune: scratch outputs of its timed dispatch (written, never read), its own timing ring (a slot per timed
    // dispatch, read a few runs later), the tuner, the cache key and whether the result was stored
    ID3D11Texture2D* tuneTrueTex = nullptr;   ID3D11UnorderedAccessView* tuneTrueUAV = nullptr;   ID3D11ShaderResourceView* tuneTrueSRV = nullptr;
    ID3D11Texture2D* tuneEstTex = nullptr;    ID3D11UnorderedAccessView* tuneEstUAV = nullptr;    ID3D11ShaderResourceView* tuneEstSRV = nullptr;
    ID3D11Texture2D* tuneFieldsTex = nullptr; ID3D11UnorderedAccessView* tuneFieldsUAV = nullptr; ID3D11ShaderResourceView* tuneFieldsSRV = nullptr;
    FaldCoreTiming tuneTiming;
    FaldConvTuner convTune;
    std::string convTuneKey;
    bool convTuneStored = false;
    bool gainFused = false;                // FaldGainBlurFused at Build: passes 2b + 2c in one dispatch into gainB (gainA unused)
    // per-frame fields
    ID3D11Texture2D* driveTex = nullptr;   ID3D11UnorderedAccessView* driveUAV = nullptr;  ID3D11ShaderResourceView* driveSRV = nullptr;
    ID3D11Texture2D* bTrueTex = nullptr;   ID3D11UnorderedAccessView* bTrueUAV = nullptr;  ID3D11ShaderResourceView* bTrueSRV = nullptr;
    ID3D11Texture2D* bEstTex = nullptr;    ID3D11UnorderedAccessView* bEstUAV = nullptr;   ID3D11ShaderResourceView* bEstSRV = nullptr;
    // the same two PACKED with their flat response, (B_true, flat, B_est, flat) RGBA32F (fald_shader.h t39): what
    // SampleFields and the gain passes read, one sample / load for the four; the conv pass writes it next to the two above
    ID3D11Texture2D* fieldsTex = nullptr;  ID3D11UnorderedAccessView* fieldsUAV = nullptr; ID3D11ShaderResourceView* fieldsSRV = nullptr;
    // gain on the fine grid (pass 2b) and its blurred version (pass 2c, ping-pong)
    ID3D11Texture2D* gainATex = nullptr; ID3D11UnorderedAccessView* gainAUAV = nullptr; ID3D11ShaderResourceView* gainASRV = nullptr;
    ID3D11Texture2D* gainBTex = nullptr; ID3D11UnorderedAccessView* gainBUAV = nullptr; ID3D11ShaderResourceView* gainBSRV = nullptr;
    // flat-lattice response of both kernels (computed once at build): a flat field must give gain 1
    ID3D11Texture2D* flatTrueTex = nullptr; ID3D11UnorderedAccessView* flatTrueUAV = nullptr; ID3D11ShaderResourceView* flatTrueSRV = nullptr;
    ID3D11Texture2D* flatEstTex = nullptr;  ID3D11UnorderedAccessView* flatEstUAV = nullptr;  ID3D11ShaderResourceView* flatEstSRV = nullptr;
    // black-frame LED boost, one set per statistic round (kept apart so a dump shows both): the per-zone non-black
    // flag (cols x rows, 0/1) the statistic pass writes, and the reduce pass's 2x1 result ([0] boost, [1] zone count).
    // The LUT buffer ((first zone count, boost) pairs) exists only when the panel file carries a LUT.
    ID3D11Texture2D* activeTex[2] = {}; ID3D11UnorderedAccessView* activeUAV[2] = {}; ID3D11ShaderResourceView* activeSRV[2] = {};
    ID3D11Texture2D* boostTex[2] = {};  ID3D11UnorderedAccessView* boostUAV[2] = {};  ID3D11ShaderResourceView* boostSRV[2] = {};
    ID3D11Buffer* boostLutBuf = nullptr; ID3D11ShaderResourceView* boostLutSRV = nullptr;
    // the statistic pass's non-black zone counters, one per round (zeroed at creation; + (new - old) per finished zone)
    ID3D11Buffer* boostCountBuf = nullptr; ID3D11UnorderedAccessView* boostCountUAV = nullptr;
    // The change-driven path (shared/fald_change.h). Round 0's own drive / filtered drive / fields (round 1's are the
    // textures above, which the dump and the tests read as before); the touched-zone list the sweeps read on the partial
    // path; the change flags the sweeps and the LED-lag passes set and the decide pass reads; the indirect arguments the
    // decide pass writes; what the previous run left behind.
    ID3D11Texture2D* drive0Tex = nullptr; ID3D11UnorderedAccessView* drive0UAV = nullptr; ID3D11ShaderResourceView* drive0SRV = nullptr;
    ID3D11Texture2D* driveFilt0Tex = nullptr; ID3D11UnorderedAccessView* driveFilt0UAV = nullptr; ID3D11ShaderResourceView* driveFilt0SRV = nullptr;
    ID3D11Texture2D* bTrue0Tex = nullptr; ID3D11UnorderedAccessView* bTrue0UAV = nullptr; ID3D11ShaderResourceView* bTrue0SRV = nullptr;
    ID3D11Texture2D* bEst0Tex = nullptr;  ID3D11UnorderedAccessView* bEst0UAV = nullptr;  ID3D11ShaderResourceView* bEst0SRV = nullptr;
    ID3D11Texture2D* fields0Tex = nullptr; ID3D11UnorderedAccessView* fields0UAV = nullptr; ID3D11ShaderResourceView* fields0SRV = nullptr;
    ID3D11Texture2D* gain0Tex = nullptr;  ID3D11UnorderedAccessView* gain0UAV = nullptr;  ID3D11ShaderResourceView* gain0SRV = nullptr;
    ID3D11Buffer* zoneListBuf = nullptr; ID3D11ShaderResourceView* zoneListSRV = nullptr;   // dynamic, cols * rows uints
    ID3D11Buffer* changeFlagsBuf = nullptr; ID3D11UnorderedAccessView* changeFlagsUAV = nullptr;   // 3 uints, zeroed
    ID3D11Buffer* argsBuf = nullptr; ID3D11UnorderedAccessView* argsUAV = nullptr;   // FALD_ARGS_BYTES, raw, indirect
    uint32_t runMode = 0;                  // CB word 79 of the next FillCB (fald_shader.h runMode)
    bool stateComplete = false;            // the previous run left every texture of a complete run (FaldChangeInputs)
    bool fieldsStand = false;              // a full or partial run's textures stand for this panel / size: a held run (the
                                           // rate-cap governor's) corrects the new frame with them; false after a build, idle
    unsigned long long heldRuns = 0;       // the runs that kept the fields (the governor's cap; the log, the tests)
    unsigned long long lastSettingsKey = 0, lastFrameKey = 0;   // of the previous run
    uint32_t lastTrueMap = 0xFFFFFFFFu, lastEstMap = 0xFFFFFFFFu;   // the maps (FaldDriveMap) its kernels read: a run whose
                                           // routing differs (a panel-clock seed / the first clock run after it) is full
    bool lastPartial = false;              // the previous run took the partial path (the dump, the tests)
    std::vector<uint32_t> touched;         // the touched-zone list of the current run
    // zone sweeps (work guide C14; fald_shader.h above ZoneSlices): slices per zone, and for a lattice of more than one
    // the slice partials the statistic / S0 passes write and their combine variants fold (u2); null for one slice
    unsigned int zoneSlices = 1;
    ID3D11Buffer* zonePartBuf = nullptr; ID3D11UnorderedAccessView* zonePartUAV = nullptr;
    // starfield balancing: five zone textures (created on the first frame the option is on, released when it goes off;
    // all cols x rows RGBA32F; channel layout = the rules header in fald_shader.h). S0 writes stat (peak, speck-zone
    // flag, sparse, solid) and bg; S1 writes w (wt = target weight, wt * ln peak, flank flag, speck-zone flag) and plan2; S2 writes plan
    // (w0_field, ln target, ln lift, ln peak). The statistic rounds and the pixel pass read plan (t15) and plan2 (t18).
    ID3D11Texture2D* starStatTex = nullptr; ID3D11UnorderedAccessView* starStatUAV = nullptr; ID3D11ShaderResourceView* starStatSRV = nullptr;
    ID3D11Texture2D* starWTex = nullptr;    ID3D11UnorderedAccessView* starWUAV = nullptr;    ID3D11ShaderResourceView* starWSRV = nullptr;
    ID3D11Texture2D* starPlanTex = nullptr; ID3D11UnorderedAccessView* starPlanUAV = nullptr; ID3D11ShaderResourceView* starPlanSRV = nullptr;
    // bg = (ln b, the brightest pixel's index ly * zw + lx, lit sum, a_eff), b = the zone's darkest pixel (S0 -> S1;
    // evidence in the dump)
    ID3D11Texture2D* starBgTex = nullptr;   ID3D11UnorderedAccessView* starBgUAV = nullptr;   ID3D11ShaderResourceView* starBgSRV = nullptr;
    // plan2 = the second field texture the pixels read: (ln b, tapered protection `near`, speck-zone flag, w) — .xy
    // bilinear (a pull never goes below the background; the protection is applied per pixel), .z nearest (own-zone gate)
    ID3D11Texture2D* starPlan2Tex = nullptr; ID3D11UnorderedAccessView* starPlan2UAV = nullptr; ID3D11ShaderResourceView* starPlan2SRV = nullptr;
    bool starOn = false;                     // the balancing runs this frame (setting on AND the textures exist): CB word 35
    bool starFailLogged = false;
    unsigned int starRetryCounter = 0;       // frames since the star textures failed to create (retry every ~300, like Build)
    struct StarCB {                          // the clamped settings of the last FaldRunPasses (CB words 52-65, dump)
        float even = 0.8f, lift = 0.0f, targetGain = 1.0f, capNits = 0.0f;
        float strength = 1.0f, areaLo = 40.0f, areaHi = 160.0f, peakHi = 0.0f;
        float nbLo = 0.15f, nbHi = 0.30f; uint32_t reach = 2, evenReach = 8;
        float targetSigma = 0.0f, keepNits = 100.0f;
    } star;
    // The frozen fade stack ASF10 + T11_TMTC (FaldSettings::flankFade; shared/fald_fade.h): its resources and T state exist
    // only while the setting is on (released when it goes off). fadeOn = this run applies it (CB word 75 bit 0, t20-t22).
    // fadeKey = the settings the cached frame part was computed under (the frame the layer works on: starfield and its
    // parameters): a change re-forms the fade on the next run without a T step.
    struct FaldFade* fade = nullptr;
    // The always-on core round 2 (PROTOTYPE, FaldSettings::core2; shared/fald_core2.h): its zone textures and IIR state exist
    // only while the setting is on; core2On = this run applies it (b3 flags, t42 field, the round-0 statistics).
    struct FaldCore2* core2 = nullptr;
    bool core2On = false;
    uint32_t core2Flags = 0;                 // FaldCore2FlagsOfMode(FaldSettings::coreMode) of this run (0 = C)
    bool fadeOn = false;
    bool fadeFailLogged = false;
    unsigned int fadeRetryCounter = 0;       // frames since the fade resources failed to create (retry every ~300)
    unsigned long long fadeKey = 0;
    // The fade's safety governor (shared/fald_fade_governor.h): its GPU time per content frame against 15 % of the content
    // interval; suspended = the layer runs without the fade (the setting stays on). Its status is published for the
    // interface / the pipe (FaldFadeGovPublish).
    FaldFadeGovernor fadeGov;
    bool fadeGovConfigured = false;
    bool fadeGovWeak = false;                // the device's adapter is weak for the fade (FaldFadeWeakAdapter)
    uint32_t fadeGovPublishedGen = 0xFFFFFFFFu;
    // The core layer's rate-cap governor (shared/fald_core_governor.h): the full path's GPU time per run (coreTiming:
    // timestamp queries read a few runs later) against 15 % of the refresh interval; capped = the full path every
    // `spacing`, held runs (the pixel pass on the standing fields) in between. Published as the fade's (FaldCoreGovPublish).
    FaldCoreGovernor coreGov;
    FaldCoreTiming coreTiming;
    uint32_t coreGovPublishedGen = 0xFFFFFFFFu;
    ID3D11Buffer* cb = nullptr;
    uint32_t debugMode = 0;
    uint32_t pedMode = 0;                    // FaldSettings::pedMode at the last FaldRunPasses (GUI toggle)
    unsigned long long framesRun = 0;
    unsigned int retryCounter = 0;     // frames since the last failed Build (retry every few seconds)
    std::string lastLoggedError;       // log each distinct failure once
};

// Diagnostic trace (appends "<ms> [tid] msg" to fald_trace.log next to the exe; thread-safe, cheap).
void FaldTrace(const char* msg);

// Process-global shaders (compiled in InitD3D via InitFaldShaders; released in ReleaseFaldShaders).
bool InitFaldShaders();
// The HLSL the overlay path compiles: by default the sources of shared/fald_shader.h (InitFaldShaders). A test swaps in
// another set — the frozen pre-fractional-pitch copy (tests/fald_shader_legacy.h) — to hold the current shaders to it
// bit for bit on integer lattices. Release the shaders (ReleaseFaldShaders) before compiling another set.
struct FaldShaderSources {
    const char* common; const char* stat; const char* conv; const char* gain; const char* blur; const char* gainBlur; const char* temporal;
    const char* panelClock; const char* starStat; const char* starWeight; const char* starPlan; const char* decide;
    const char* zoneCombineDefine; const char* pixel;
};
FaldShaderSources FaldDefaultShaderSources();
bool InitFaldShadersFrom(const FaldShaderSources& src);
void ReleaseFaldShaders();
bool FaldShadersReady();

// Render-thread API.
// Ensure the monitor's resources exist for (settings.paramsPath, ctx->width/height); returns true when
// the layer can run this frame. Logs and returns false (once per distinct error) otherwise. Rebuilds
// when the path changes, when runtime.set_fald_params re-sets it (settings.reloadSeq), or when the
// file's size/mtime changes (polled every ~2 s) — a panel file re-exported in place is picked up.
bool FaldEnsureResources(MonitorContext* ctx, const FaldSettings& settings);
// True when the layer is configured but its panel file was refused for THIS path / reloadSeq / mode and the file
// is unchanged since: the render loop must not keep the overlay awake for it (render thread only). A new
// set_fald_params / GUI path (reloadSeq or path change), a mode switch or a resize retries.
bool FaldLayerRefused(const MonitorContext* ctx, const FaldSettings& settings);
// Run the compute passes on ctx->fald->inter and draw the corrected frame into finalRT.
// Handles a pending debug dump (ctx->faldDumpRequested): the fields + input frame before the pixel
// pass, the OUTPUT frame (fald_out.*) after it. newContent = the frame is a NEW desktop frame (not a re-process
// of the cached one, not a cursor-only update, not a settle frame): with a temporal drive state it re-arms the
// settle hold (FaldSettlePending), so a static desktop keeps re-running the layer for ~5 tau.
// Starfield balancing (FaldSettings::star) recomputes its zone plan from ctx->fald->inter on EVERY call — new frame,
// C2 re-process and settle frame alike (the plan is a stateless function of the source frame the caller left there).
// frameKey = the caller's identity of the frame in ctx->fald->inter (equal keys = bit-identical frames; 0 = unknown):
// with the flank fade on and LED lag off, a run without new content on the frame the last reach was formed from, under
// the same settings, reuses both rounds' reach (shared/fald_fade.h FaldFadeReachCurrent) — the fields are a pure
// function of that frame then.
// rects / numRects (shared/fald_change.h, the change-driven path): the frame regions that changed since the previous run
// — the caller's promise that nothing outside them differs from the frame of the previous run. numRects < 0 = unknown
// (every pass runs); 0 = nothing changed. Without rects, a run without new content on the frame of the previous run
// (frameKey equal) is taken as "nothing changed". When the rules allow it (FaldChangeDecide) the sweeps run on the touched
// zones only and the GPU decides what else has to run (the decide pass + indirect dispatches); the pixel pass draws the
// whole target as always (this path's swap chain gets a whole frame per present). Bit for bit the full run.
// frameChanged / frameChangedOffset (fald_change.h changeFlags[3]): a buffer whose uint at that byte offset the GPU set to
// 1 when the pass that made the intermediate changed the whole frame this present (the dynamic tone map's peak moved:
// shared/peak_detect.h, the smoothing pass's draw arguments + PEAK_ARGS_MOVED_OFFSET) and 0 when it is the same function
// as at the previous run; null = the caller knows the pass is the same function whenever the key is equal (static). On a
// partial run the layer copies it into its change flags (GPU to GPU) and a decide pass orders full sweeps when set.
#define FALD_RUN_HAS_FRAME_KEY 1
#define FALD_RUN_HAS_RECTS 1
#define FALD_RUN_HAS_FRAME_CHANGED 1
void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT, bool newContent = true, unsigned long long frameKey = 0,
                   const FaldChangeRect* rects = nullptr, int numRects = -1, ID3D11Buffer* frameChanged = nullptr,
                   unsigned int frameChangedOffset = 0);
// True while the temporal drive state still owes settle frames after the last content change (render thread).
// The render loop then re-runs ONLY the FALD passes on the layer's own intermediate (the main pass output of the
// last frame) and presents — no Desktop Duplication read (the released frame texture is not a valid source; HW
// 2026-09-17: re-processing it every frame flickered black), no main pass.
bool FaldSettlePending(const MonitorContext* ctx);
// The layer did not run this frame (disabled, refused, hook mode, wrong format): forget the temporal state and the
// settle hold. HW-relevant (design review 2026-09-17): a state kept across a layer-OFF period would blend the new
// content with the OLD content's drives when the layer comes back on a static desktop, and nothing would settle it.
void FaldLayerIdle(MonitorContext* ctx);
void FaldReleaseResources(MonitorContext* ctx);

// The flank fade's governor, process-wide inputs (render thread reads): the INI force key ([General]
// FaldFlankFadeForce=1: never suspended, whatever the adapter or the cost; REFERENCE.md) and, for the tests, the same.
extern std::atomic<bool> g_faldFadeForce;
// The governor's status per monitor and mode, for the interface and the pipe (src/fald_fade_status.h reads both paths).
void FaldFadeGovPublish(int monitorIndex, bool hdr, const FaldFadeGovernorStatus& st);
// The core layer's rate-cap governor, likewise: the INI force key ([General] FaldCoreForce=1: never capped) and the
// status per monitor and mode (src/fald_core_status.h).
extern std::atomic<bool> g_faldCoreForce;
// extra: the conv variant / auto-tune state / core mode word (dwm_hook_config.h DwmHookFaldCoreExtra; 0 = unknown).
void FaldCoreGovPublish(int monitorIndex, bool hdr, const FaldCoreGovernorStatus& st, uint32_t extra = 0);

// Test seam (tests/test_fald_fade_hw.cpp FALD_HW_PROFILE_CORE=1): a hook called before and after every GPU command of
// FaldRunPasses, so a harness can put timestamp queries around each pass. Null in production: one pointer test per pass.
enum FaldCorePass : unsigned int {
    FALD_PASS_STAR_S0 = 0, FALD_PASS_STAR_S1, FALD_PASS_STAR_S2, FALD_PASS_FADE_FRAME, FALD_PASS_CLOCK,
    FALD_PASS_STAT0, FALD_PASS_TEMPORAL0, FALD_PASS_CONV0, FALD_PASS_GAIN0, FALD_PASS_BLURH0,
    FALD_PASS_BLURV0, FALD_PASS_COPY0, FALD_PASS_FADE_REACH0,
    FALD_PASS_STAT1, FALD_PASS_TEMPORAL1, FALD_PASS_CONV1, FALD_PASS_GAIN1, FALD_PASS_BLURH1,
    FALD_PASS_BLURV1, FALD_PASS_COPY1, FALD_PASS_FADE_REACH1,
    FALD_PASS_COMMIT, FALD_PASS_PIXEL, FALD_PASS_DECIDE_S, FALD_PASS_DECIDE0, FALD_PASS_DECIDE1, FALD_PASS_COUNT
};
const char* FaldCorePassName(unsigned int pass);
struct FaldCorePassHook {
    virtual void Before(ID3D11DeviceContext* ctx, unsigned int pass) = 0;
    virtual void After(ID3D11DeviceContext* ctx, unsigned int pass) = 0;
};
extern FaldCorePassHook* g_faldCorePassHook;
#define FALD_CORE_HAS_PASS_HOOK 1

// DesktopLUT's own windows, exempt from the FALD views (shared/fald_exempt.h): the host's list in desktop pixels (the GUI
// thread sets it while a view is on, empty otherwise; FaldRunPasses and the hook's shared config read it). Any thread.
// Set returns true when the list changed.
bool FaldSetExemptRects(const std::vector<FaldExemptRect>& desktop);
std::vector<FaldExemptRect> FaldExemptRects();
