// FALD always-on core, rounds 2 / 3 / 5 (branch fald/core-kc = fald/core-speed + the fald/principled prototype b443145; KC5
// from d01e714 on fald/core-kc5; DLC results/fald_principled_2026-10-22/PREREG_CORE2.md, RESULT_CORE3.md, RESULT_LIVE.md,
// PREREG_CORE5.md, RESULT_CORE5.md, RESULT_LIVE5.md; results/fald_core_kc_2026-10-23/, fald_core_kc5_2026-10-23/).
// C (the layer) with its own artefact mechanisms fixed at their source, LATTICE-SCALE only:
//   alpha    the near-field BRIGHTENING shrunk by the calibration's measured / modelled halo ratio at the zone's level
//            (the 2026-09-12 dark-halo rows: the model is too deep 1.34 x at 20 nit ... 3 x at 1 nit);
//   deficit  the soft knee's SPATIAL EDGE (the ring round a highlight whose brightening hits the LCD ceiling) removed by
//            taking exactly the brightening the knee would clip away, spread to the neighbours unless a step between zone
//            levels stops it, low-passed, applied per pixel before Correct — per ZONE with a zone IIR (round 2, KC2c), or on
//            the FINE grid as a sliding-window worst case with no temporal state at all (round 3, KC3);
//   zone IIR (round 2 only) the two zone fields filtered over time (0.35 per 2-refresh content frame), reset at a cut;
//   cap      (round 5, KC5 — THE DEFAULT, fald_core_mode.h) C's near-field FILL kept (the colorimeter fill tests 2026-10-23:
//            C's fill is right at 5-50 nit, KC3's deficit removed most of it; KC5 1.77 % RMS vs C 1.51 %), the knee-edge ring
//            / dark band held off by a brightening CAP on the fine grid IN PLACE OF the deficit (kernels C1, CX, CY):
//              C1  per node h = max(0, ln(0.9 c3) - ln P3): P3 = the 3 x 3 node max of the node peaks (stat round 0), c3 = the
//                  3 x 3 node min of the round's ceiling min(white, white gain.y / KNEE_CAP_TRUST) — the headroom of the
//                  brightest pixel the node's gain reaches before the knee starts;
//              CX / CY  A = min_n [h_n + s |x - n|_1] over the WHOLE lattice (separable min-plus, x then y; |.|_1 in px, the
//                  node pitch latW / (cols sub) x latH / (rows sub); s = FALD_CORE2_CAP_S per 100 px): a forward and a
//                  backward scan per lattice line in groupshared memory (<= FALD_CORE2_SCAN_MAX nodes per axis, else the
//                  mode runs as C) -> the fine-grid field (t48), PER ROUND (round 1's ceiling differs from round 0's);
//              CoreGain   ln g' = min(alpha ln g, A) for g > 1 (darkening untouched): a capped object's rim gets ~no
//                  brightening, rising at most s per 100 px away from it — no knee edge and no filled surround hard against
//                  a capped core, C's full fill beyond;
//            and (FALD_CORE2_ALPHA5) alpha's level law a5 in Z1: KC3's a(G) below 0.5 nit, 1 above 2 nit (smoothstep in
//            ln G) — the fill test found the model's halo too deep only at 1-2 nit and below. Stateless, lattice-only.
//            The live blind test (RESULT_LIVE5.md): no dark band in 12 / 12 clips (C 5 / 12). The slope s = 0.10 was picked
//            by the calibration rule "the smallest s whose fill estimate stays within C's + 0.3" (s 0.07 = the prototype's
//            test mode 5 / KC5b lost fill on the meter: not ported — one slope is one constant).
//   (KC2's soft leak floor, coreFlags bit 2, is reserved: never set by the hosts; bits 4 / 5 = the prototype's KC4 hold.)
// Reference (float64): pcore.py CoreEmu, KVARIANTS["KC2c"] / ["K3sL"], pcore5 V5["KC5"]; GPU-order twin (numpy): DLC
// dlc/fald/core2emu.py (CORE_KC5: cap_field, minplus_l1, cap_steps in the shader's float32 order).
//
// What runs (cols x rows zones, the fine grid cols * sub x rows * sub; nothing at full resolution is added):
//   stat round 0 (+): per zone over ALL its pixels, mc = the brightest channel (as-if-white nits):
//       P = min(max mc, white), M = mean mc, lnG = mean ln max(mc, 0.05)                    -> coreStat (t40, u4)
//       and (round 3) per FINE node the peak min(mc, white) of the pixels whose nearest node it is  -> nodePeak (u6 / u7)
//   Z0 (per run, round 2 only): the cut sum = sum over zones of |ln max(M, 0.05) - the previous run's| (fixed point 2^-14)
//   Z1 (per run): cut = mean |d ln M| > 0.25; reset = no valid state or a cut (round 3: every run);
//       a_raw = clamp(0.8 G / (G + 1.4) - 0.1, 0, 1), G = exp(lnG); alphaState <- reset ? a_raw : s + coreA (a_raw - s)
//   Z2 (per run): alpha_z = saturate(G_0.7(alphaState)); k = exp(-(1 + 4 max(0, |grad ln max(M, 0.05)| - ln 3)))
//       (np.gradient); ln P; rate = -ln k                                                      -> coreZone (t41)
//   round 2, per round after RunGain:  D1 (one 64-thread group per zone): over the zone's S x S fine nodes + a one-node
//       ring (clamped): n = alpha_n max(ln g, 0) + min(ln g, 0) (g = gainTex.x, alpha_n = alpha_z bilinear at the node),
//       gmax = max n, cmin = min(white, white min(gainTex.y) / KNEE_CAP_TRUST);
//       d = gmax > 0 ? min(max(0, ln P + gmax - ln 0.9 - ln cmin), gmax) : 0; D <- reset ? d : s + coreA (d - s)
//       D2: D <- max(D, max_3x3(D) k) twice (one pass, 5 x 5 window);  D3: D = max(G_0.7(D), 0) -> coreField[round] (t42)
//   round 3, per round after RunGain (the fine grid): F1 / F2 the sliding window (W = sub + 3 nodes) of (node peak,
//       alpha-shrunk brightening, ceiling) along x then y -> d per node; F3 / F4 a separable L1 max-plus cone (reach 2
//       zones, the zone gate's rate); F5 / F6 the Gaussian 0.7 zone in nodes (rows then columns, clamped at 0)
//                                                                                              -> coreNodeD[round] (t48)
//   round 5, per round after RunGain (the fine grid, in place of F1..F6): C1 the headroom h per node (3 x 3 node max of the
//       node peaks, 3 x 3 node min of the ceiling); CX / CY the exact min-plus scans along x then y (the step st = s / 100 x
//       the node pitch in px)                                                                   -> coreNodeD[round] (t48) = A
//   Correct's two call sites (round-1 statistic, pixel pass): g' = CoreGain(g): g > 1 ? max(exp(alpha ln g - D), 1) : g
//       (= ln g' = alpha max(ln g, 0) + min(ln g, 0) - min(D, max(alpha ln g, 0)), D >= 0; darkening is C's); round 5:
//       ln g' = min(alpha ln g, max(A, 0)). The round-1 statistic reads ROUND 0's D / A (the hosts bind it for compute), the
//       pixel pass round 1's.
// G_0.7 = scipy gaussian_filter(sigma 0.7, truncate 4 -> radius 3, mode nearest) — direct 7 x 7, taps in coreW0..3.
// coreA = 1 - 0.65^(k / 2), k = the panel refreshes since the previous run (the twin: 0.35 per content frame of 2 refreshes).
//
// Off (coreFlags 0, mode 0): no core resource exists, no core dispatch runs, CoreGain returns its input and the round-0
// additions are skipped — C bit for bit (tests: the fade's OFF manifest, the hook = overlay lists).
//
// The change-driven path (shared/fald_change.h) with the core on:
//   - KC2c has TEMPORAL STATE (the IIRs advance every run): a run with it on takes the FULL path (FaldChangeInputs::
//     coreStateful) — its fields on a present that moved nothing would still differ from the previous run's.
//   - KC3 and KC5 are STATELESS (every run is a reset: the fields are a pure function of this frame's zone statistics / node
//     peaks and the round's gain — KC5's cap is a GLOBAL min-plus, but of exactly those inputs: when none of them moved the
//     previous run's h / A are this run's bytes), so the partial path applies: the touched-zone sweep of round 0 recomputes those zones' core
//     statistics and node peaks by the same thread mapping and COMPARES them with the texture's previous values — a change
//     sets changeFlags[0] bit 1 (value 2u: FALD_CHANGE_CORE_MOVED) — and the decide pass orders the core's lattice passes
//     (Z1 / Z2, the fine passes of a round) by indirect arguments only when the core statistics or that round's gain
//     moved (FALD_ARGS_CORE_*); untouched, the previous run's fields stand and are the full path's bytes. A zone no rect
//     touched has the same pixels, so its P / M / lnG / node peaks are unchanged by definition. The node peaks are stored by
//     plain stores per zone (a one-slice lattice: the sweep's own group; larger zones: the slice partials in u7, folded by
//     the combine variant) — max is order-free, so this equals the prototype's cleared-map InterlockedMax bit for bit, and
//     no per-run clear is needed (which a touched-zone sweep could not survive).
// Bindings: b3 (FaldCoreCB), t40..t43, t48 in the common seam; the stat pass's u4 (coreStat), u6 (node peaks), u7 (node
// slice partials); the core's own kernels t44..t47, t49..t52, u0 / u1. Slots above the fade stack's b1 / b2 / t0..t35 and
// fald/core-speed's t36..t39, u3 / u5 / u6 (the decide pass) — the stat pass binds eight UAVs (u0..u7: FL 11.0's limit).
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <cstdint>
#include <string>
// The flag bits, the mode table (FaldSettings::coreMode; INI <mode>_FaldCoreModeTest, pipe runtime.fald_core_mode,
// state.get fald_core_mode, hook faldFlags bits 16-19) and THE DEFAULT (KC5): shared/fald_core_mode.h (no D3D: types.h too).
#include "fald_core_mode.h"

constexpr float FALD_CORE2_A2 = 0.35f;                // the zone IIR's blend per 2-refresh content frame
constexpr float FALD_CORE2_CUT = 0.25f;               // the cut: mean over zones of |d ln max(M, 0.05)|
constexpr unsigned int FALD_CORE2_FIXED = 16384u;     // the cut sum's fixed point (2^14 per unit)
constexpr unsigned int FALD_CORE2_CB_BYTES = 64u;
constexpr unsigned int FALD_CORE2_NODE_SUB_MAX = 16u; // the stat pass's node peaks need sub * sub <= 256 (its group size)
// Round 5 (KC5): the cap's slope (ln gain per 100 px; the prototype's one constant — the mirror computes the per-node step
// as float32((s * 0.01) * latW) / (cols * sub)), alpha's level law a5 (nit; ln G0 / ln G1 as float32 in Z1), and the scans'
// groupshared length: a lattice axis of more than FALD_CORE2_SCAN_MAX nodes runs the mode as C (none known: the PA32 is 384).
constexpr float FALD_CORE2_CAP_S = 0.10f;
constexpr float FALD_CORE2_A5_G0 = 0.5f, FALD_CORE2_A5_G1 = 2.0f;
constexpr unsigned int FALD_CORE2_SCAN_MAX = 2048u;
// Whether the cap's scans fit this lattice (FaldCore2BeginRun drops the flags to 0 — the run is C — when they do not).
inline bool FaldCore2CapFits(unsigned int cols, unsigned int rows, unsigned int sub) {
    return cols * sub <= FALD_CORE2_SCAN_MAX && rows * sub <= FALD_CORE2_SCAN_MAX;
}

// The HLSL declarations (FaldCoreCB b3, t40..t43, t48, CoreGain, CoreResetNow, CoreW) are g_faldCommonSource's third seam
// (fald_shader.h): every pass compiles with them; the core's own kernels are compiled from g_faldCommonSource + theirs.

// IIR factor for k panel refreshes since the previous run (k = 0: the same refresh, nothing advances).
float FaldCore2IirFactor(unsigned long long refreshes);
// The refreshes since the previous run: the panel clock's k (mode 3), else the run interval over the refresh period, >= 1.
unsigned long long FaldCore2Refreshes(bool panelClock, unsigned long long clkElapsed, float dtMs, float refreshMs);
// Test seam (tests/test_fald_core2.cpp, test_fald_hook.cpp): > 0 = every run advances the IIR by exactly this many
// refreshes, whatever the run timing (WARP is slow). 0 in the app and the hook.
extern unsigned long long g_faldCore2TestRefreshes;

// Process-global shaders (Z0, Z1, Z2, D1, D2, D3, F1..F6, C1, CX, CY), compiled from `common` (g_faldCommonSource) + the core source.
bool FaldCore2InitShaders(ID3D11Device* dev, const char* common, std::string* err);
void FaldCore2ReleaseShaders();
bool FaldCore2ShadersReady();

struct FaldCore2;                                      // per monitor: the zone / fine textures and the IIR state
FaldCore2* FaldCore2Create();
void FaldCore2Destroy(FaldCore2* c);                   // null ok
// A lattice: cols x rows zones, sub fine nodes per zone side, slices = the zone sweeps' slices per zone (fald_shader.h
// FaldZoneSlices: the node-peak partials exist for lattices of more than one).
bool FaldCore2Ensure(FaldCore2* c, ID3D11Device* dev, unsigned int cols, unsigned int rows, unsigned int sub, unsigned int slices);
void FaldCore2Release(FaldCore2* c);
void FaldCore2Invalidate(FaldCore2* c);                // the next run resets the IIR (layer idle, resume, mode change)
unsigned long long FaldCore2VramBytes(const FaldCore2* c);

// One run, in this order (the caller binds its FaldCB on b0 for every core dispatch; the core binds b3 itself):
//   BeginRun -> stat round 0 with StatViews bound (u4, u6, u7) -> [the decide pass] -> RunZone -> RunGain(0) ->
//   RunDeficit(0) -> stat round 1 (BindCommon for compute: b3 + round 0's t42 / t48) -> RunGain(1) -> RunDeficit(1) ->
//   pixel pass (BindCommon for the pixel stage: round 1's) -> EndRun.
// args / slot (the change-driven path, KC3 / KC5): when `args` is given the passes dispatch INDIRECT from that raw buffer —
// RunZone's three from `latticeSlot` (FALD_ARGS_CORE_LATTICE: (cols + 15) / 16 x (rows + 15) / 16 x 1 or nothing), a
// round's fine passes from `fineSlot` (FALD_ARGS_CORE_FINE0 / 1: the fine grid's 16 x 16 groups or nothing — KC5's line
// scans map that group grid onto the lattice lines themselves, so one slot gates C1, CX and CY alike). The round-2
// passes (D1..D3, the IIR) are never indirect: a stateful core takes the full path (args is then ignored).
void FaldCore2BeginRun(FaldCore2* c, ID3D11DeviceContext* ctx, uint32_t flags, float coreA);
void FaldCore2StatViews(const FaldCore2* c, ID3D11UnorderedAccessView** statU4, ID3D11UnorderedAccessView** nodePeakU6,
                        ID3D11UnorderedAccessView** nodePartU7);
void FaldCore2RunZone(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11Buffer* args = nullptr,
                      unsigned int latticeSlot = 0);
void FaldCore2RunDeficit(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11ShaderResourceView* gainB,
                         unsigned int round, ID3D11Buffer* args = nullptr, unsigned int fineSlot = 0);
ID3D11Buffer* FaldCore2CB(const FaldCore2* c);         // b3 (with flags 0 when the core does not run: CoreGain = identity)
ID3D11ShaderResourceView* FaldCore2ZoneSRV(const FaldCore2* c);                       // t41 (alpha_z, k, ln P, rate)
ID3D11ShaderResourceView* FaldCore2FieldSRV(const FaldCore2* c, unsigned int round);  // t42 round 2's (alpha_z, D) of that round
ID3D11ShaderResourceView* FaldCore2NodeDSRV(const FaldCore2* c, unsigned int round);  // t48 round 3's fine-grid D / round 5's cap A of that round
void FaldCore2EndRun(FaldCore2* c);                    // the state is valid from now on

// Dump access (zone textures, cols x rows): stat (P, M, lnG, 0), zone (alpha_z, k, ln P, rate), field = round 1's (alpha_z,
// D) and field0 = round 0's, alphaState, D state of round 0 / 1; the fine grid (round 3 / 5): nodePeak (uint bits), nodeD (round 5: the cap A) =
// round 1's D and nodeD0 = round 0's.
struct FaldCore2Debug {
    ID3D11Texture2D* stat = nullptr; ID3D11Texture2D* zone = nullptr; ID3D11Texture2D* field = nullptr; ID3D11Texture2D* field0 = nullptr;
    ID3D11Texture2D* alphaState = nullptr; ID3D11Texture2D* dState[2] = {}; unsigned int cols = 0, rows = 0;
    ID3D11Texture2D* nodePeak = nullptr; ID3D11Texture2D* nodeD = nullptr; ID3D11Texture2D* nodeD0 = nullptr; unsigned int sub = 0;
    bool reset = false;                                // the last BeginRun's reset request
    float coreA = 0.0f; uint32_t flags = 0;
};
FaldCore2Debug FaldCore2DebugViews(const FaldCore2* c);
