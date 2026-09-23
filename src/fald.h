// FALD (mini-LED local dimming) context-dependence correction — the OVERLAY path's layer (HDR, and SDR under
// Windows ACM where the overlay frame is FP16 scRGB too). This file never runs in DWM hook mode; there the
// hook runs the layer's stateless core itself (dwm_hook/hook_fald.h), out of the same HLSL and the same
// panel-file reader. Everything below that hook_fald.h does not have — starfield, glow fill, the temporal
// modes, the settle hold, the dump — is what keeps this the experiment bench of the two.
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
// FALD_CB_BYTES, the transfer / boost-rule constants, FaldGlowReqCeil).
#include "../shared/fald_panel.h"

struct MonitorContext;
struct FaldSettings;
struct FaldStarfieldSettings;
struct FaldGlowSettings;
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

// Glow fill (EXPERIMENT, default off; work guide ticket S2). The complete rules: the header above g_faldGlowZoneSource
// in fald_shader.h = the module docstring of DLC dlc/fald/glowfill.py (the reference). In short: a CALCULATED black lift
// that evens the LED glow on dark content — per zone the white pedestal Vz = white * tmin * B_true (the correction's own
// field: boost, starfield, temporal state included), its grey CLOSING over a (2 reach + 1)^2 box (only holes / valleys
// enclosed by glow are filled: no skirt around a window, no filled letterbox bars), a blur under the closing, the zone
// deficit; per pixel the interpolated deficit (x strength, <= capNits) minus what the pixel's own content already shows,
// x the correction's deep-dark trust in B_est, requested in the pedestal's colour and never above glowReqCeil (no LED is
// lit, no zone becomes LIT for the boost count). Each inverse round adds the fill of ITS fields, so round 1's statistic /
// boost count see the frame the panel receives. Off = no resources, no dispatches, the shaders' results bit-identical.
constexpr unsigned int FALD_GLOW_REACH_MIN = 1;
constexpr unsigned int FALD_GLOW_REACH_MAX = 4;       // also sizes the dilation texture (cols + 2 MAX) x (rows + 2 MAX)
constexpr float FALD_GLOW_CAP_MIN = 0.005f;           // as-if-white nits
constexpr float FALD_GLOW_CAP_MAX = 0.5f;
// The request ceiling (FALD_GLOW_REQ_* and FaldGlowReqCeil) is derived from the panel file, so it lives with it in
// shared/fald_panel.h — both render paths write it into CB word 79.
// Clamp every field to its documented range (NaN -> the default).
void FaldGlowClamp(FaldGlowSettings& s);
// HDR only: every level behind the ceiling and the band (drive floor, LIT level, count threshold) is an HDR measurement,
// so the fill never runs on a gamma-transfer (SDR / ACM) panel file; the settings / pipe / GUI keep the SDR switch off.
bool FaldGlowSupported(const FaldPanelParams& p);
extern const char* const FALD_GLOW_NEEDS_STAR_NOTE;    // glow fill is part of starfield: stored switch, runs only with it
extern const char* const FALD_GLOW_SDR_NOTE;          // the one text the pipe / state.get / GUI use to say why
// The count-threshold band applies: the file has a boost LUT AND the mean zone rule (CB word 80; DLC glowfill.band_active).
bool FaldGlowBandActive(const FaldPanelParams& p);

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
    ID3D11Buffer* kTrueBuf = nullptr;      ID3D11ShaderResourceView* kTrueSRV = nullptr;
    ID3D11Buffer* kEstBuf = nullptr;       ID3D11ShaderResourceView* kEstSRV = nullptr;
    // per-frame fields
    ID3D11Texture2D* driveTex = nullptr;   ID3D11UnorderedAccessView* driveUAV = nullptr;  ID3D11ShaderResourceView* driveSRV = nullptr;
    ID3D11Texture2D* bTrueTex = nullptr;   ID3D11UnorderedAccessView* bTrueUAV = nullptr;  ID3D11ShaderResourceView* bTrueSRV = nullptr;
    ID3D11Texture2D* bEstTex = nullptr;    ID3D11UnorderedAccessView* bEstUAV = nullptr;   ID3D11ShaderResourceView* bEstSRV = nullptr;
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
    // zone sweeps (work guide C14; fald_shader.h above ZoneSlices): slices per zone, and for a lattice of more than one
    // the slice partials the statistic / S0 / G4 passes write and their combine variants fold (u2); null for one slice
    unsigned int zoneSlices = 1;
    ID3D11Buffer* zonePartBuf = nullptr; ID3D11UnorderedAccessView* zonePartUAV = nullptr;
    // starfield balancing: five zone textures (created on the first frame the option is on, released when it goes off;
    // all cols x rows RGBA32F; channel layout = the rules header in fald_shader.h). S0 writes stat (peak, speck-zone
    // flag, sparse, solid) and bg; S1 writes w (wt = target weight, wt * ln peak, flank flag, speck-zone flag) and plan2; S2 writes plan
    // (w0_field, ln target, ln lift, ln peak). The statistic rounds and the pixel pass read plan (t15) and plan2 (t18).
    ID3D11Texture2D* starStatTex = nullptr; ID3D11UnorderedAccessView* starStatUAV = nullptr; ID3D11ShaderResourceView* starStatSRV = nullptr;
    ID3D11Texture2D* starWTex = nullptr;    ID3D11UnorderedAccessView* starWUAV = nullptr;    ID3D11ShaderResourceView* starWSRV = nullptr;
    ID3D11Texture2D* starPlanTex = nullptr; ID3D11UnorderedAccessView* starPlanUAV = nullptr; ID3D11ShaderResourceView* starPlanSRV = nullptr;
    // bg = (ln b, the brightest pixel's index ly * cellW + lx, lit sum, a_eff), b = the zone's darkest pixel (S0 -> S1;
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
    // glow fill (work guide S2): four zone textures, created on the first frame the option is on and released when it
    // goes off. glowV = Vz (cols x rows R32F, pass G0), glowDil = the box maximum on the lattice extended by
    // FALD_GLOW_REACH_MAX on every side (R32F, G1), glowC = the closing (R32F, G2), glowEnv = (Ez, Dz, Cz, Vz) (RGBA32F,
    // G3) — the statistic round 1 and the pixel pass sample glowEnv.y (t23). Rewritten after EACH round's conv pass.
    ID3D11Texture2D* glowVTex = nullptr;   ID3D11UnorderedAccessView* glowVUAV = nullptr;   ID3D11ShaderResourceView* glowVSRV = nullptr;
    ID3D11Texture2D* glowDilTex = nullptr; ID3D11UnorderedAccessView* glowDilUAV = nullptr; ID3D11ShaderResourceView* glowDilSRV = nullptr;
    ID3D11Texture2D* glowCTex = nullptr;   ID3D11UnorderedAccessView* glowCUAV = nullptr;   ID3D11ShaderResourceView* glowCSRV = nullptr;
    ID3D11Texture2D* glowEnvTex = nullptr; ID3D11UnorderedAccessView* glowEnvUAV = nullptr; ID3D11ShaderResourceView* glowEnvSRV = nullptr;
    // the count-threshold band's zone scale k (cols x rows R32F; pass G5 after each round's G4, read by GlowAdd; t24)
    ID3D11Texture2D* glowKTex = nullptr;   ID3D11UnorderedAccessView* glowKUAV = nullptr;   ID3D11ShaderResourceView* glowKSRV = nullptr;
    // C16, only while the band applies (FaldGlowBandActive): G4's per-zone record (Pc, Pf, LIT flag, k0) (cols x rows
    // RGBA32F, t25) and neighbour bound A_0..A_7 ((2 cols) x rows RGBA32F, t26), G5's Jacobi scratch k (R32F), and G4's
    // slice partials of A (GlowBandPart records at u3; zones of more than one slice only)
    ID3D11Texture2D* glowBandTex = nullptr; ID3D11UnorderedAccessView* glowBandUAV = nullptr; ID3D11ShaderResourceView* glowBandSRV = nullptr;
    ID3D11Texture2D* glowATex = nullptr;    ID3D11UnorderedAccessView* glowAUAV = nullptr;    ID3D11ShaderResourceView* glowASRV = nullptr;
    ID3D11Texture2D* glowKTmpTex = nullptr; ID3D11UnorderedAccessView* glowKTmpUAV = nullptr; ID3D11ShaderResourceView* glowKTmpSRV = nullptr;
    ID3D11Buffer* glowBandPartBuf = nullptr; ID3D11UnorderedAccessView* glowBandPartUAV = nullptr;
    // G5's report (4 x 1 R32F, u2): iterations evaluated, converged (1 / 0), the worst-case pass ran (1 / 0), its zones
    ID3D11Texture2D* glowGuardTex = nullptr; ID3D11UnorderedAccessView* glowGuardUAV = nullptr; ID3D11ShaderResourceView* glowGuardSRV = nullptr;
    bool glowBand = false;                   // the band runs this frame (glowOn AND FaldGlowBandActive): CB word 80
    bool glowOn = false;                     // the fill runs this frame (setting on AND the textures exist): CB word 75
    bool glowFailLogged = false;
    unsigned int glowRetryCounter = 0;       // frames since the glow textures failed to create (retry every ~300, like Build)
    struct GlowCB {                          // the clamped settings of the last FaldRunPasses (CB words 76-78, dump)
        float strength = 1.0f, capNits = 0.05f; uint32_t reach = 2;
    } glow;
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
void FaldRunPasses(MonitorContext* ctx, ID3D11RenderTargetView* finalRT, bool newContent = true);
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
