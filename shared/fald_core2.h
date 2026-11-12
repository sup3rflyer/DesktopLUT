// FALD always-on core KC5 (round 5; ported from the fald/principled prototype d01e714 on fald/core-kc5; DLC
// results/fald_principled_2026-10-22/PREREG_CORE5.md, RESULT_CORE5.md, RESULT_LIVE5.md; results/fald_core_kc5_2026-10-23/).
// C (the layer) with its own artefact mechanisms fixed at their source, LATTICE-SCALE only:
//   alpha    the near-field BRIGHTENING shrunk by the calibration's measured / modelled halo ratio at the zone's level
//            (the 2026-09-12 dark-halo rows: the model is too deep 1.34 x at 20 nit ... 3 x at 1 nit), with the level law
//            a5 (FALD_CORE2_ALPHA5): the round-3 a(G) below 0.5 nit, 1 above 2 nit (smoothstep in ln G) — the fill test
//            found the model's halo too deep only at 1-2 nit and below;
//   cap      C's near-field FILL kept (the colorimeter fill tests 2026-10-23: C's fill is right at 5-50 nit; KC5 1.77 % RMS
//            vs C 1.51 %), the knee-edge ring / dark band held off by a brightening CAP on the fine grid (kernels C1, CX, CY):
//              C1  per node h = max(0, ln(0.9 c3) - ln P3): P3 = the 3 x 3 node max of the node peaks (stat round 0), c3 = the
//                  3 x 3 node min of the round's ceiling min(white, white gain.y / KNEE_CAP_TRUST) — the headroom of the
//                  brightest pixel the node's gain reaches before the knee starts;
//              CX / CY  A = min_n [h_n + s |x - n|_1] over the WHOLE lattice (separable min-plus, x then y; |.|_1 in px, the
//                  node pitch latW / (cols sub) x latH / (rows sub); s = FALD_CORE2_CAP_S per 100 px): a forward and a
//                  backward scan per lattice line in groupshared memory (<= FALD_CORE2_SCAN_MAX nodes per axis, else the
//                  run is C) -> the fine-grid field (t48), PER ROUND (round 1's ceiling differs from round 0's);
//              CoreGain   ln g' = min(alpha ln g, A) for g > 1 (darkening untouched): a capped object's rim gets ~no
//                  brightening, rising at most s per 100 px away from it — no knee edge and no filled surround hard against
//                  a capped core, C's full fill beyond.
//            Stateless, lattice-only. The live blind test (RESULT_LIVE5.md): no dark band in 12 / 12 clips (C 5 / 12). The
//            slope s = 0.10 was picked by the calibration rule "the smallest s whose fill estimate stays within C's + 0.3".
// The earlier rounds' rules (KC2c: a zone deficit with a zone IIR; KC3: the fine-grid deficit) were A/B test modes until
// 2026-11-12 and are gone; their flag bits (1, 2, 4) are never set (fald_core_mode.h).
// Reference (float64): pcore5 V5["KC5"]; GPU-order twin (numpy): DLC dlc/fald/core2emu.py (cap_field, minplus_l1,
// cap_steps in the shader's float32 order).
//
// What runs (cols x rows zones, the fine grid cols * sub x rows * sub; nothing at full resolution is added):
//   stat round 0 (+): per zone over ALL its pixels, mc = the brightest channel (as-if-white nits):
//       P = min(max mc, white), M = mean mc, lnG = mean ln max(mc, 0.05)                    -> coreStat (t40, u4)
//       and per FINE node the peak min(mc, white) of the pixels whose nearest node it is      -> nodePeak (u6 / u7)
//   Z1 (per run): a_raw = clamp(0.8 G / (G + 1.4) - 0.1, 0, 1), G = exp(lnG), with the level law a5     -> alphaState
//   Z2 (per run): alpha_z = saturate(G_0.7(alphaState)); k = exp(-(1 + 4 max(0, |grad ln max(M, 0.05)| - ln 3)))
//       (np.gradient); ln P; rate = -ln k                                                      -> coreZone (t41)
//   per round after RunGain (the fine grid): C1 the headroom h per node (3 x 3 node max of the node peaks, 3 x 3 node min
//       of the ceiling); CX / CY the exact min-plus scans along x then y (the step st = s / 100 x the node pitch in px)
//                                                                                              -> coreNodeD[round] (t48) = A
//   Correct's two call sites (round-1 statistic, pixel pass): g' = CoreGain(g): ln g' = min(alpha ln g, max(A, 0)) for
//       g > 1. The round-1 statistic reads ROUND 0's A (the hosts bind it for compute), the pixel pass round 1's.
// G_0.7 = scipy gaussian_filter(sigma 0.7, truncate 4 -> radius 3, mode nearest) — direct 7 x 7, taps in coreW0..3.
//
// Off (coreFlags 0, mode C): no core resource exists, no core dispatch runs, CoreGain returns its input and the round-0
// additions are skipped — C bit for bit (tests: the layer gate's C manifest, the hook = overlay lists). C is what runs
// when the core's shaders or textures are unavailable or the lattice is too long for the cap's scans; the tests select
// it (FaldSettings::coreMode) to hold the layer to its references.
//
// (2026-11-12: the path follows what the core's passes CHANGED, not what their inputs were: a zone's statistics moving
// orders Z1 / Z2 only; the round-1 sweep and the pixel pass run in full only when alpha_z or a cap moved — Z2 and each
// round's last cap pass compare their output with the previous run's and re-order what reads it themselves.
// shared/fald_change.h lists the bits.)
// The change-driven path (shared/fald_change.h) with the core on: KC5 is STATELESS (the fields are a pure function of this
// frame's zone statistics / node peaks and the round's gain — the cap is a GLOBAL min-plus, but of exactly those inputs:
// when none of them moved the previous run's h / A are this run's bytes), so the partial path applies: the touched-zone
// sweep of round 0 recomputes those zones' core statistics and node peaks by the same thread mapping and COMPARES them with
// the texture's previous values — a change sets changeFlags[0] bit 1 (value 2u: FALD_CHANGE_CORE_MOVED) — and the decide
// pass orders the core's lattice passes (Z1 / Z2, the fine passes of a round) by indirect arguments only when the core
// statistics or that round's gain moved (FALD_ARGS_CORE_*); untouched, the previous run's fields stand and are the full
// path's bytes. A zone no rect touched has the same pixels, so its P / M / lnG / node peaks are unchanged by definition.
// The node peaks are stored by plain stores per zone (a one-slice lattice: the sweep's own group; larger zones: the slice
// partials in u7, folded by the combine variant) — max is order-free, so this equals the prototype's cleared-map
// InterlockedMax bit for bit, and no per-run clear is needed (which a touched-zone sweep could not survive).
// Bindings: b3 (FaldCoreCB), t40, t41, t48 in the common seam; the stat pass's u4 (coreStat), u6 (node peaks), u7 (node
// slice partials); the core's own kernels t44 / t45, t49 / t51, u0 / u1 — the stat pass binds eight UAVs (u0..u7: FL 11.0's limit).
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <cstdint>
#include <string>
// The flag bits, the mode table (FaldSettings::coreMode; state.get fald_core_mode, hook faldFlags bits 16-19) and THE
// DEFAULT (KC5): shared/fald_core_mode.h (no D3D: types.h too).
#include "fald_core_mode.h"

constexpr unsigned int FALD_CORE2_CB_BYTES = 64u;
constexpr unsigned int FALD_CORE2_NODE_SUB_MAX = 16u; // the stat pass's node peaks need sub * sub <= 256 (its group size)
// The cap's slope (ln gain per 100 px; the prototype's one constant — the mirror computes the per-node step as
// float32((s * 0.01) * latW) / (cols * sub)), alpha's level law a5 (nit; ln G0 / ln G1 as float32 in Z1), and the scans'
// groupshared length: a lattice axis of more than FALD_CORE2_SCAN_MAX nodes runs as C (none known: the PA32 is 384).
constexpr float FALD_CORE2_CAP_S = 0.10f;
constexpr float FALD_CORE2_A5_G0 = 0.5f, FALD_CORE2_A5_G1 = 2.0f;
constexpr unsigned int FALD_CORE2_SCAN_MAX = 2048u;
// Whether the cap's scans fit this lattice (FaldCore2BeginRun drops the flags to 0 — the run is C — when they do not).
inline bool FaldCore2CapFits(unsigned int cols, unsigned int rows, unsigned int sub) {
    return cols * sub <= FALD_CORE2_SCAN_MAX && rows * sub <= FALD_CORE2_SCAN_MAX;
}

// The HLSL declarations (FaldCoreCB b3, t40, t41, t48, CoreGain, CoreW) are g_faldCommonSource's third seam
// (fald_shader.h): every pass compiles with them; the core's own kernels are compiled from g_faldCommonSource + theirs.

// Process-global shaders (Z1, Z2, C1, CX, CY), compiled from `common` (g_faldCommonSource) + the core source.
bool FaldCore2InitShaders(ID3D11Device* dev, const char* common, std::string* err);
// The five kernels as FaldCore2InitShaders compiles them (cs_5_0, entry "main"): kernel k's whole source text and name.
constexpr unsigned int FALD_CORE2_SHADERS = 5u;
// Kernel k from precompiled bytecode (shared/fald_shader_set.h FaldShaderBytecode(FALD_SH_CORE_Z1 + k)): nothing is compiled.
// One kernel per call, so a host inside another process's present can spread them (dwm_hook/hook_fald.cpp). True when the
// kernel exists afterwards.
bool FaldCore2CreateShader(ID3D11Device* dev, unsigned int k, const void* bytecode, size_t bytes);
std::string FaldCore2ShaderSource(unsigned int k, const char* common);
const char* FaldCore2ShaderName(unsigned int k);
void FaldCore2ReleaseShaders();
bool FaldCore2ShadersReady();

struct FaldCore2;                                      // per monitor: the zone / fine textures
FaldCore2* FaldCore2Create();
void FaldCore2Destroy(FaldCore2* c);                   // null ok
// A lattice: cols x rows zones, sub fine nodes per zone side, slices = the zone sweeps' slices per zone (fald_shader.h
// FaldZoneSlices: the node-peak partials exist for lattices of more than one).
bool FaldCore2Ensure(FaldCore2* c, ID3D11Device* dev, unsigned int cols, unsigned int rows, unsigned int sub, unsigned int slices);
void FaldCore2Release(FaldCore2* c);
unsigned long long FaldCore2VramBytes(const FaldCore2* c);

// One run, in this order (the caller binds its FaldCB on b0 for every core dispatch; the core binds b3 itself):
//   BeginRun -> stat round 0 with StatViews bound (u4, u6, u7) -> [the decide pass] -> RunZone -> RunGain(0) ->
//   RunCap(0) -> stat round 1 (BindCommon for compute: b3 + round 0's t48) -> RunGain(1) -> RunCap(1) ->
//   pixel pass (BindCommon for the pixel stage: round 1's).
// args / slot (the change-driven path): when `args` is given the passes dispatch INDIRECT from that raw buffer — RunZone's
// two from `latticeSlot` (FALD_ARGS_CORE_LATTICE: (cols + 15) / 16 x (rows + 15) / 16 x 1 or nothing), a round's fine
// passes from `fineSlot` (FALD_ARGS_CORE_FINE0 / 1: the fine grid's 16 x 16 groups or nothing — the line scans map that
// group grid onto the lattice lines themselves, so one slot gates C1, CX and CY alike).
void FaldCore2BeginRun(FaldCore2* c, ID3D11DeviceContext* ctx, uint32_t flags);
void FaldCore2StatViews(const FaldCore2* c, ID3D11UnorderedAccessView** statU4, ID3D11UnorderedAccessView** nodePeakU6,
                        ID3D11UnorderedAccessView** nodePartU7);
// changeFlags / lateArgs (the partial path; the layer's FALD_CHANGE_FLAGS buffer at u5, its LATE arguments buffer as a
// UAV at u6 — never the buffer `args` these passes are dispatched from): Z2 compares alpha_z — the one zone value the
// correction reads — with the previous run's; moved = [0] |= FALD_CHANGE_ALPHA_MOVED and the round-1 sweep re-ordered
// over the whole lattice. A round's last cap pass compares A; moved = what reads it re-ordered in full (round 0's: the
// round-1 sweep; round 1's: the pixel pass). They store those arguments themselves (fald_shader.h FALD_ORDER_*;
// shared/fald_change.h "THE LATE ARGUMENTS"): the caller's decide pass of the round must have run BEFORE them, and
// nothing rewrites those slots after. The caller's FaldCB must hold the run's path bit (runMode) and, for RunCap,
// roundIdx = `round`. Null on a full run.
void FaldCore2RunZone(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11Buffer* args = nullptr,
                      unsigned int latticeSlot = 0, ID3D11UnorderedAccessView* changeFlags = nullptr,
                      ID3D11UnorderedAccessView* lateArgs = nullptr);
void FaldCore2RunCap(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11ShaderResourceView* gainB,
                     unsigned int round, ID3D11Buffer* args = nullptr, unsigned int fineSlot = 0,
                     ID3D11UnorderedAccessView* lateArgs = nullptr);
ID3D11Buffer* FaldCore2CB(const FaldCore2* c);         // b3 (with flags 0 when the core does not run: CoreGain = identity)
ID3D11ShaderResourceView* FaldCore2ZoneSRV(const FaldCore2* c);                       // t41 (alpha_z, k, ln P, rate)
ID3D11ShaderResourceView* FaldCore2NodeDSRV(const FaldCore2* c, unsigned int round);  // t48 the cap A of that round

// Dump access (zone textures, cols x rows): stat (P, M, lnG, 0), zone (alpha_z, k, ln P, rate), alphaState (a_raw); the
// fine grid: nodePeak (uint bits), nodeD = round 1's cap A and nodeD0 = round 0's.
struct FaldCore2Debug {
    ID3D11Texture2D* stat = nullptr; ID3D11Texture2D* zone = nullptr; ID3D11Texture2D* alphaState = nullptr;
    unsigned int cols = 0, rows = 0;
    ID3D11Texture2D* nodePeak = nullptr; ID3D11Texture2D* nodeD = nullptr; ID3D11Texture2D* nodeD0 = nullptr; unsigned int sub = 0;
    uint32_t flags = 0;
};
FaldCore2Debug FaldCore2DebugViews(const FaldCore2* c);
