// DesktopLUT - fald_panel.h
// The FALD panel parameter file (*.bin, produced by `python -m dlc.fald.export`) and the pure
// functions over it. Shared by the host (src/fald.cpp, overlay path) and the DWM hook
// (dwm_hook/hook_fald.cpp), so there is ONE parser of the file format: the two paths are compared
// bit for bit against each other and against the DLC reference (dlc/fald/gpuemu.py), which only
// means something while they read the file the same way.
//
// Standalone: no D3D, no host internals, no hook internals. Windows headers are used only for the
// wide-char path type.
#pragma once
#include <stdint.h>
#include <string>
#include <vector>

// Constant-buffer size shared by the host's FillCB (src/fald.cpp), the hook's FillCB
// (dwm_hook/hook_fald.cpp) and cbuffer FaldCB (shared/fald_shader.h): 84 words.
constexpr unsigned int FALD_CB_BYTES = 336;

// Panel-file signal transfer (FLD3 header word 40; see FaldPanelParams::transfer).
constexpr uint32_t FALD_TRANSFER_PQ = 0;      // HDR: PQ codes (FLD1/FLD2 files are implicitly PQ)
constexpr uint32_t FALD_TRANSFER_GAMMA = 1;   // SDR under ACM: gamma-encoded codes, panel EOTF = power law

// Black-frame LED boost (FLD4 panel files; DLC FaldParams.boost_lut, work guide C12): the panel
// firmware multiplies every LED drive by a staircase function of the number of NON-BLACK zones of
// the frame it receives. At most this many steps fit the file's fixed-size block.
constexpr unsigned int FALD_BOOST_MAX_STEPS = 24;
// Which zones the firmware counts as NON-BLACK (FLD4 word 53, CB word 72; DLC FaldParams.boost_rule,
// work guide C12b). Both rules share the LIT criterion; 0 = LIT-or-DIM (every FLD4 file before
// 2026-09-20: the word was reserved, zero), 1 = LIT-or-MEAN (the 2026-09-20 refit over all 64 meter
// + camera observations; LIT-or-DIM miscounts 4 of them).
constexpr uint32_t FALD_BOOST_RULE_DIM = 0;
constexpr uint32_t FALD_BOOST_RULE_MEAN = 1;

// The glow fill's request ceiling, from what was MEASURED (work guide, probe pixrule): a 2-px column
// at 0.298 nit does NOT make a zone LIT, 0.4 nit does (the rule's 0.35 is the midpoint), and whether
// a ~0.3-nit AREA lights LEDs was never measured (risk R4; the 0.5-nit drive floor is a fit value).
// So the ceiling keeps a factor ~1.5 below the one measured "not LIT" level and 2.5 below the drive
// floor. The fill itself is host-only (the hook's phase-one core never runs it), but the ceiling is a
// CB word both paths write, so it lives with the file it is derived from.
constexpr float FALD_GLOW_REQ_FLOOR_FRAC = 0.4f;      // a filled pixel's request stays below this x the drive floor ...
constexpr float FALD_GLOW_REQ_LIT_FRAC = 0.55f;       // ... and below this x the boost count's LIT level (files with a LUT)

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
    float gainSmoothCells = 0.35f;           // Gaussian sigma of the gain low-pass, in cells (0 = off) — also the soft knee's ceiling B_est (C15; 0 = per-pixel again)
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
    // pixel's brightest channel, on the frame the panel RECEIVES (DLC FaldModel.active_zone_fraction). boostRule
    // FALD_BOOST_RULE_MEAN (FLD4 word 53 = 1; words 54 / 55) replaces DIM by: the zone mean of nits^boostMeanGamma is
    // >= boostMeanThresh (C12b). A file with word 53 = 0 behaves exactly as before the word existed.
    uint32_t boostN = 0;                     // steps in use (0 = none)
    float boostLo[FALD_BOOST_MAX_STEPS] = {};   // zone fraction where step i starts, strictly ascending, 0..1
    float boostVal[FALD_BOOST_MAX_STEPS] = {};  // its LED boost, 0.5..2
    float boostLitNits = 0.35f, boostLitFrac = 0.0f, boostDimNits = 0.011f, boostDimFrac = 0.19f;
    uint32_t boostRule = FALD_BOOST_RULE_DIM;   // FALD_BOOST_RULE_*
    float boostMeanGamma = 0.62f, boostMeanThresh = 0.0693f;   // rule MEAN only (gamma in (0, 4], thresh > 0)
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
// CPU reference of the statistic pass's zone flag (HLSL g_faldStatSource, DLC FaldModel.active_zones / gpuemu
// Emu.stat_active): `maxChannelNits` = the brightest channel (as-if-white nits) of each of the zone's `n` pixels.
// LIT || (boostRule MEAN ? mean(nits^gamma) >= thresh : DIM). Sequential float sums — the GPU's reduction order differs
// in the last bits only. false for n = 0.
bool FaldBoostZoneActive(const FaldPanelParams& p, const float* maxChannelNits, size_t n);
// Does a file with this transfer belong to this monitor mode? (PQ <-> HDR, gamma <-> SDR/ACM.)
bool FaldTransferMatchesMode(uint32_t transfer, bool monitorHdr);
// The panel lattice (origin + cols*cellW x rows*cellH) must lie inside the monitor's frame.
bool FaldLatticeFits(const FaldPanelParams& p, int width, int height);
// The level (as-if-white nits, brightest channel) a glow-filled pixel's request never exceeds: CB word 79 (DLC
// glowfill.req_ceiling). Written by both paths' FillCB; only the host's fill passes read it.
float FaldGlowReqCeil(const FaldPanelParams& p);
// Size + last-write stamp of the params file (false when it cannot be read). Both paths rebuild on a change.
bool FaldPanelFileStamp(const std::wstring& path, unsigned long long& size, unsigned long long& mtime);
