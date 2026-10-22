// FALD always-on core, round 2 — PROTOTYPE (branch fald/principled; DLC results/fald_principled_2026-10-22/PREREG_CORE2.md,
// RESULT_CORE2.md). C (the layer) with two of its own artefact mechanisms fixed at their source, LATTICE-SCALE only:
//   alpha    the near-field BRIGHTENING shrunk by the calibration's measured / modelled halo ratio at the zone's level
//            (the 2026-09-12 dark-halo rows: the model is too deep 1.34 x at 20 nit ... 3 x at 1 nit);
//   deficit  the soft knee's SPATIAL EDGE (the ring round a highlight whose brightening hits the LCD ceiling) removed by
//            taking, per zone, exactly the brightening the knee would clip away, spread to the neighbours unless a step
//            between zone levels stops it, low-passed, applied per pixel before Correct;
//   zone IIR the two zone fields filtered over time (0.35 per 2-refresh content frame), reset at a cut.
//   (KC2's soft leak floor, coreFlags bit 2, is reserved: the prototype is KC2c = C's own leak floor.)
// Reference (float64): pcore.py CoreEmu, KVARIANTS["KC2c"]; GPU-order twin (numpy): DLC dlc/fald/core2emu.py.
//
// What runs (cols x rows zones, the fine grid cols * sub x rows * sub; nothing at full resolution is added):
//   stat round 0 (+): per zone over ALL its pixels, mc = the brightest channel (as-if-white nits):
//       P = min(max mc, white), M = mean mc, lnG = mean ln max(mc, 0.05)                    -> coreStat (t40)
//       and |ln max(M, 0.05) - the previous run's| into one fixed-point sum (u4, 2^-14 units) for the cut test
//   Z1 (per run): cut = mean |d ln M| > 0.25; reset = no valid state or a cut;
//       a_raw = clamp(0.8 G / (G + 1.4) - 0.1, 0, 1), G = exp(lnG); alphaState <- reset ? a_raw : s + coreA (a_raw - s)
//   Z2 (per run): alpha_z = saturate(G_0.7(alphaState)); k = exp(-(1 + 4 max(0, |grad ln max(M, 0.05)| - ln 3)))
//       (np.gradient); ln P                                                                    -> coreZone (t41)
//   D1 (per round, after RunGain; one 64-thread group per zone): over the zone's S x S fine nodes + a one-node ring
//       (clamped): n = alpha_n max(ln g, 0) + min(ln g, 0) (g = gainTex.x, alpha_n = alpha_z bilinear at the node),
//       gmax = max n, cmin = min(white, white min(gainTex.y) / KNEE_CAP_TRUST);
//       d = gmax > 0 ? min(max(0, ln P + gmax - ln 0.9 - ln cmin), gmax) : 0; D <- reset ? d : s + coreA (d - s)
//   D2 (per round): D <- max(D, max_3x3(D) k) twice (one pass, 5 x 5 window)
//   D3 (per round): D = max(G_0.7(D), 0)                                                 -> coreField (t42) = (alpha_z, D)
//   Correct's two call sites (round-1 statistic, pixel pass): g' = CoreGain(g): g > 1 ? max(exp(alpha ln g - D), 1) : g
//       (= ln g' = alpha max(ln g, 0) + min(ln g, 0) - min(D, max(alpha ln g, 0)), D >= 0; darkening is C's)
// G_0.7 = scipy gaussian_filter(sigma 0.7, truncate 4 -> radius 3, mode nearest) — direct 7 x 7, taps in coreW0..3.
// coreA = 1 - 0.65^(k / 2), k = the panel refreshes since the previous run (the twin: 0.35 per content frame of 2 refreshes).
//
// Off (coreFlags 0): no core resource exists, no core dispatch runs, CoreGain returns its input and the round-0 additions
// are skipped — C bit for bit (tests: the fade's OFF manifest).
// Bindings: b3 (FaldCoreCB), t40..t47, u3 / u4 in stat round 0; slots chosen above the fade stack's b1 / b2 / t0..t35 and
// fald/core-speed's t38.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <cstdint>
#include <string>

constexpr uint32_t FALD_CORE2_ALPHA = 1u;
constexpr uint32_t FALD_CORE2_DEFICIT = 2u;
constexpr uint32_t FALD_CORE2_SOFT = 4u;               // reserved (KC2), never set by the prototype's hosts
constexpr uint32_t FALD_CORE2_KC2C = FALD_CORE2_ALPHA | FALD_CORE2_DEFICIT;
constexpr float FALD_CORE2_A2 = 0.35f;                // the zone IIR's blend per 2-refresh content frame
constexpr float FALD_CORE2_CUT = 0.25f;               // the cut: mean over zones of |d ln max(M, 0.05)|
constexpr unsigned int FALD_CORE2_FIXED = 16384u;     // the cut sum's fixed point (2^14 per unit)
constexpr unsigned int FALD_CORE2_CB_BYTES = 64u;

// The HLSL declarations (FaldCoreCB b3, t40..t43, CoreGain, CoreResetNow, CoreW) are g_faldCommonSource's third seam
// (fald_shader.h): every pass compiles with them; the core's own kernels are compiled from g_faldCommonSource + theirs.

// IIR factor for k panel refreshes since the previous run (k = 0: the same refresh, nothing advances).
float FaldCore2IirFactor(unsigned long long refreshes);
// The refreshes since the previous run: the panel clock's k (mode 3), else the run interval over the refresh period, >= 1.
unsigned long long FaldCore2Refreshes(bool panelClock, unsigned long long clkElapsed, float dtMs, float refreshMs);
// Test seam (tests/test_fald_core2.cpp, test_fald_hook.cpp): > 0 = every run advances the IIR by exactly this many
// refreshes, whatever the run timing (WARP is slow). 0 in the app and the hook.
extern unsigned long long g_faldCore2TestRefreshes;

// Process-global shaders (Z1, Z2, D1, D2, D3), compiled from `common` (g_faldCommonSource) + the core source.
bool FaldCore2InitShaders(ID3D11Device* dev, const char* common, std::string* err);
void FaldCore2ReleaseShaders();
bool FaldCore2ShadersReady();

struct FaldCore2;                                      // per monitor: the zone textures and the IIR state
FaldCore2* FaldCore2Create();
void FaldCore2Destroy(FaldCore2* c);                   // null ok
bool FaldCore2Ensure(FaldCore2* c, ID3D11Device* dev, unsigned int cols, unsigned int rows);   // (re)build for a lattice
void FaldCore2Release(FaldCore2* c);
void FaldCore2Invalidate(FaldCore2* c);                // the next run resets the IIR (layer idle, resume, mode change)
unsigned long long FaldCore2VramBytes(const FaldCore2* c);

// One run, in this order (the caller binds its FaldCB on b0 for every core dispatch; the core binds b3 itself):
//   BeginRun -> stat round 0 with StatViews bound (u3, u4) and the lz SRV on t45 -> RunZone -> RunGain(0) ->
//   RunDeficit(0) -> stat round 1 (Bind: b3 + t42) -> RunGain(1) -> RunDeficit(1) -> pixel pass (Bind) -> EndRun.
void FaldCore2BeginRun(FaldCore2* c, ID3D11DeviceContext* ctx, uint32_t flags, float coreA);
void FaldCore2StatViews(FaldCore2* c, ID3D11UnorderedAccessView* uav[2], ID3D11ShaderResourceView** lzSrv);
void FaldCore2RunZone(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB);
void FaldCore2RunDeficit(FaldCore2* c, ID3D11DeviceContext* ctx, ID3D11Buffer* faldCB, ID3D11ShaderResourceView* gainB,
                         unsigned int round);
ID3D11Buffer* FaldCore2CB(const FaldCore2* c);         // b3 (with flags 0 when the core does not run: CoreGain = identity)
ID3D11ShaderResourceView* FaldCore2FieldSRV(const FaldCore2* c);   // t42 (alpha_z, D of the last RunDeficit)
void FaldCore2EndRun(FaldCore2* c);                    // the state is valid from now on

// Dump access (zone textures, cols x rows): stat (P, M, lnG, 0), zone (alpha_z, k, ln P, 0), field (alpha_z, D),
// alphaState, D state of round 0 / 1.
struct FaldCore2Debug {
    ID3D11Texture2D* stat = nullptr; ID3D11Texture2D* zone = nullptr; ID3D11Texture2D* field = nullptr;
    ID3D11Texture2D* alphaState = nullptr; ID3D11Texture2D* dState[2] = {}; unsigned int cols = 0, rows = 0;
    bool reset = false;                                // the last BeginRun's reset request
    float coreA = 0.0f; uint32_t flags = 0;
};
FaldCore2Debug FaldCore2DebugViews(const FaldCore2* c);
