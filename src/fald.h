// FALD (mini-LED local dimming) context-dependence correction — overlay-path layer (HDR, and SDR under
// Windows ACM where the overlay frame is FP16 scRGB too; never in DWM hook mode).
// Design + measurements: DLC/docs/fald-shader-design.md, DLC/docs/fald-spatial-probe-2026-09-10.md.
// The per-panel parameter file (*.bin) is produced by `python -m dlc.fald.export <fit.json> <out.bin>`
// (layout documented there); the shaders live in fald_shader.h.
#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11.h>
#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

struct MonitorContext;
struct FaldSettings;
struct FaldStarfieldSettings;

// Constant-buffer size shared by FillCB (fald.cpp) and cbuffer FaldCB (fald_shader.h): 68 words.
constexpr unsigned int FALD_CB_BYTES = 272;

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

// Black-frame LED boost (FLD4 panel files; DLC FaldParams.boost_lut, work guide C12): the panel firmware multiplies
// every LED drive by a staircase function of the number of NON-BLACK zones of the frame it receives. At most this
// many steps fit the file's fixed-size block.
constexpr unsigned int FALD_BOOST_MAX_STEPS = 24;

// Temporal drive state (FaldSettings::temporalMode; DLC dlc/fald/temporal.py is the reference):
// 0 = off (stateless layer, byte for byte), 1 = both fields from the filtered drive (LEDs AND the panel's estimate
// lag), 2 = B_true from the filtered drive, B_est from the instantaneous one (LEDs lag, the LCD compensation follows
// the command). Default OFF: the PA32UCXR's LED law is unmeasured (work guide H5).
constexpr unsigned int FALD_TEMPORAL_OFF = 0;
constexpr unsigned int FALD_TEMPORAL_BOTH = 1;
constexpr unsigned int FALD_TEMPORAL_TRUE_ONLY = 2;
constexpr float FALD_TAU_MAX_MS = 5000.0f;
constexpr unsigned int FALD_DELAY_MAX = 3;    // pipeline delay ring depth (FaldSettings::delayFrames 0..3)
// Per-frame blend factor 1 - exp(-dt/tau) of a first-order response; tau <= 0 (or dt <= 0) = instant (1).
float FaldTemporalAlpha(float tauMs, float dtMs);
// Frames the layer keeps re-rendering after the last content change so the state settles (5 tau_max, ceil, plus the
// pipeline delay; 0 when neither edge has a time constant and there is no delay): Desktop Duplication delivers no
// frames on a static desktop.
unsigned int FaldSettleFrames(float tauRiseMs, float tauFallMs, float dtMs, unsigned int delayFrames = 0);

// Panel-file signal transfer (FLD3 header word 40; see FaldPanelParams::transfer).
constexpr uint32_t FALD_TRANSFER_PQ = 0;      // HDR: PQ codes (FLD1/FLD2 files are implicitly PQ)
constexpr uint32_t FALD_TRANSFER_GAMMA = 1;   // SDR under ACM: gamma-encoded codes, panel EOTF = power law

// Parsed panel parameter file.
struct FaldPanelParams {
    uint32_t cols = 0, rows = 0, sub = 0, cellW = 0, cellH = 0, originX = 0, originY = 0;
    uint32_t reachTrueC = 0, reachTrueR = 0, reachEstC = 0, reachEstR = 0, curveN = 0;
    float white = 0, tmin = 0, area0 = 0, gainMin = 0, gainMax = 0, driveFloor = 0;
    float w[3] = { 0, 0, 0 };                // channel shares of white (header words 16-18). Carried into the CB as
                                             // wR/wG/wB but UNUSED by the shaders: the inverse is as-if-white (the
                                             // pixel's max channel), and a per-channel pedestal is an open colour item
                                             // (work guide H2). Do not "use the weights" without that measurement.
    float curveLogMin = 0, curveLogMax = 0, estPhasePx = 0, estPhasePy = 0;
    float fadeLo = 0.004f, fadeHi = 0.03f;   // correction fades to identity where B_est < fadeHi (0 at fadeLo)
    float gainSmoothCells = 0.35f;           // Gaussian sigma of the gain low-pass, in cells (0 = off)
    float lumFadeLo = 0.5f, lumFadeHi = 5.0f; // pixel-luminance fade (as-if-white nits of the pixel's max channel):
                                             // the model has no baseline below ~1 nit (dark-halo probe 2026-09-12)
    float pedRGB[3] = { 1.0f, 1.0f, 1.0f };   // pedestal colour multipliers m_c on tmin (FLD2 words 32-34; sum w*m = 1).
                                             // FLD1 = white (1,1,1). Used only with FaldSettings::pedMode == 1.
    unsigned int pedModeFile = 0;            // FLD2 word 35: the mode the fit was validated with (informational)
    float chromaGain = 1.0f;                 // FLD2 word 36: strength of the COLOUR part of the pedestal term (channel mode)
    float chromaLo = -1.0f, chromaHi = -1.0f; // FLD2 words 37/38: its own pixel-luminance fade; -1/-1 = follow lumFade;
                                             // 0/0 = no pixel-luminance fade on the colour part (experiment knob)
    bool hasPedColour = false;               // FLD2/FLD3 file carrying a pedestal colour (words 32-35 non-zero)
    // Signal transfer of the panel-bound codes (FLD3 words 40/41; FLD1/FLD2 = PQ). 0 = PQ: the panel receives a
    // BT.2020 PQ code (HDR), as-if-white nits = rec2020_c x 80 nits of the scRGB frame. 1 = gamma: the panel
    // receives an 8/10-bit gamma-encoded code (SDR under Windows ACM, FP16 scRGB composition), as-if-white nits =
    // white x code^sdrGamma with code = sRGB_OETF(scRGB) (Windows' own scRGB -> SDR encode). DLC reference:
    // FaldParams.code_to_nits / scrgb_to_nits (dlc/fald/model.py). A file's transfer must match the monitor's
    // mode (Build refuses otherwise): an SDR fit decoded as PQ would be wrong by orders of magnitude.
    uint32_t transfer = FALD_TRANSFER_PQ;
    float sdrGamma = 0.0f;                   // the panel's own power-law EOTF exponent (transfer 1 only; measured by DLC)
    bool hasTransfer = false;                // FLD3/FLD4 file (the loader saw words 40/41)
    // Black-frame LED boost (FLD4 words 48-103; absent = no boost, the layer is then bit-identical to a build without
    // the term). Step function over the non-black zone FRACTION: the last step with lo <= fraction applies (below the
    // first step: 1.0) — DLC FaldModel.boost_of_fraction. It multiplies B_true only (the panel's own estimate does not
    // know it). A zone counts as non-black when it is LIT (more than boostLitFrac of its pixels above boostLitNits;
    // 0 = any pixel) or DIM (more than boostDimFrac of its pixels above boostDimNits) — as-if-white nits of the
    // pixel's brightest channel, on the frame the panel RECEIVES (DLC FaldModel.active_zone_fraction).
    uint32_t boostN = 0;                     // steps in use (0 = none)
    float boostLo[FALD_BOOST_MAX_STEPS] = {};   // zone fraction where step i starts, strictly ascending, 0..1
    float boostVal[FALD_BOOST_MAX_STEPS] = {};  // its LED boost, 0.5..2
    float boostLitNits = 0.35f, boostLitFrac = 0.0f, boostDimNits = 0.011f, boostDimFrac = 0.19f;
    bool hasBoost = false;                   // FLD4 file with a non-empty LUT
    std::vector<float> curve, kTrue, kEst;
};
// Resets `out` first: nothing of a previously loaded file (transfer, pedestal colour, optional words) survives.
bool LoadFaldPanelParams(const std::wstring& path, FaldPanelParams& out, std::string& err);
// Cheap header peek: does the file at `path` carry a pedestal colour (FLD2/FLD3)? false for FLD1, unreadable or missing.
bool FaldPanelFileHasPedColour(const std::wstring& path);
// Cheap header peek: the file's signal transfer (FALD_TRANSFER_PQ / FALD_TRANSFER_GAMMA) without loading the
// tables. false when the file is unreadable or not a FALD panel file (then `transfer` is left untouched).
bool FaldPanelFileTransfer(const std::wstring& path, uint32_t& transfer);
// Cheap header peek: does the file carry a black-frame LED boost LUT (FLD4 with a step count 1..24)?
bool FaldPanelFileHasBoost(const std::wstring& path);
// The GPU looks the boost up by the integer zone COUNT (no float division on the GPU): step i applies from the first
// count N with N / zonesTotal >= lo (DLC FaldModel.boost_of_fraction; the tolerance absorbs the float32 rounding of
// the file's lo so a step edge that is an exact zone count stays on its side). DLC twin: panelfile.boost_zone_threshold.
unsigned int FaldBoostZoneThreshold(float lo, unsigned int zonesTotal);
// CPU reference of the shader's lookup: the boost of a frame with `activeZones` non-black zones (1.0 without a LUT).
float FaldBoostOfCount(const FaldPanelParams& p, unsigned int activeZones);
// Does a file with this transfer belong to this monitor mode? (PQ <-> HDR, gamma <-> SDR/ACM.)
bool FaldTransferMatchesMode(uint32_t transfer, bool monitorHdr);
// The panel lattice (origin + cols*cellW x rows*cellH) must lie inside the monitor's frame.
bool FaldLatticeFits(const FaldPanelParams& p, int width, int height);

// Per-monitor GPU resources (heap-owned by MonitorContext::fald; released with the monitor's D3D
// resources and on resize — recreated lazily on the next frame).
struct FaldResources {
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
    unsigned int delayHead = 0, delayCount = 0;
    unsigned int delayFrames = 0;            // FaldSettings::delayFrames at the last FaldRunPasses
    bool stateValid = false;                 // the state texture holds a committed map (else the next pass copies the drive)
    unsigned int temporalMode = 0;           // FaldSettings::temporalMode at the last FaldRunPasses (a change resets the state)
    float tempAlphaRise = 1.0f, tempAlphaFall = 1.0f;   // the CB words of the last frame (dump)
    unsigned int settleLeft = 0;             // frames of redraw hold still owed after the last content change
    float dtMs = 16.667f;                    // EMA of the interval between consecutive runs while rendering continuously
    long long lastRunQpc = 0;
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
