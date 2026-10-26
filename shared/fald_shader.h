// FALD (mini-LED local dimming) context-dependence correction — HLSL sources.
//
// The panel sets every pixel's LCD opening from ITS OWN estimate of the backlight under that pixel;
// the estimate differs from the real spread (width, a sample-point offset, a hard support limit), so
// content beside highlights renders too bright on one side and too dark on the other. This layer
// predicts both fields from the frame the panel receives and pre-distorts each pixel so the panel
// lands on the requested luminance. Reference: DLC/src/dlc/fald/{model,correct}.py — every formula
// here mirrors that code; the drive curve and the two sub-cell kernel tables are TABULATED by the
// Python exporter (dlc.fald.export) so no kernel math is duplicated on the GPU.
//
// Signal domain: the processed frame is FP16 scRGB linear BT.709 (1.0 = 80 nits) in HDR and in SDR under
// Windows ACM alike; what differs is the CODE the panel receives. transfer 0 (PQ, HDR): Windows composes
// scRGB -> BT.2020 PQ, as-if-white nits = rec2020_c x 80. transfer 1 (gamma, ACM SDR): Windows encodes
// scRGB -> the 8/10-bit sRGB code (sRGB_OETF, the same encode the main SDR shader assumes) and the panel
// shows it with its own power law, as-if-white nits = white x sRGB_OETF(scRGB)^sdrGamma (DLC
// FaldParams.code_to_nits with transfer "gamma"; the reference formula is FaldParams.scrgb_to_nits).
// That recovers the panel-bound code only under the output profile the panel file was profiled with (DLC's
// native state: sRGB / identity); an SDR MHC2 calibration changes the per-channel code at scanout (HDR: same
// limit with an HDR MHC).
//
// Passes per frame (overlay path, on the processed frame, after tonemap/LUT/WB):
//   CS star S0 / S1 / S2 (opt., starfield balancing, work guide S1; rules in the header above g_faldStarStatSource):
//                       per-zone statistics of the SOURCE frame -> tapered protection field + zone weights -> the
//                       fields the pixels read. From here on every pass reads the BALANCED frame Balance(source)
//                       (statistic rounds, boost flags, Correct, output); off = none of it
//   CS panel clock (opt., temporal mode 3; rules in fald.h above FALD_TEMPORAL_PANEL): once per frame, from PAST frames
//                       only — both parity clocks' LED states advance toward the previous frame's drives, the two maps
//                       the kernels see in BOTH rounds are written; modes 1 / 2 run "CS temporal" below instead
//   CS stat  (round 0): per cell, area statistic over every pixel -> drive texture (cols x rows)
//                       (+ the zone's NON-BLACK flag for the black-frame LED boost when the panel file has a boost LUT)
//                       (+ with a boost LUT: the frame's non-black zone count -> step LUT -> LED boost, 2 x 1 texture,
//                       finished by the last zone of the sweep)
//   CS temporal (opt.): per cell, first-order drive STATE (rise/fall time constants) -> the drive the kernels see
//   CS conv           : drives (x) K_true [x boost], drives (x) K_est on the sub-cell grid (cols*sub x rows*sub)
//   CS stat  (round 1): same statistic (and zone flags) on the CORRECTED frame (the correction moves the drives, and
//                       the panel counts the zones of the frame it RECEIVES)
//   CS temporal (opt.): again from the committed state; the result is committed after this round
//   CS conv           : final backlight fields
//   PS                : per pixel: req = (img + ped_ref - ped) * one scale (gain; soft knee toward the ceiling)
// The change-driven (PARTIAL) path, shared/fald_change.h: with a present's dirty rects and an otherwise unchanged state,
// the sweeps run on the touched zones only and compare; a decide pass (g_faldDecideSource) after each writes the indirect
// arguments of what follows, so the GPU skips the convolution, the gain, the full round-1 sweep and the full pixel pass
// when nothing moved — the same shaders, the same values, bit for bit.
//
// Zone lattice (docs/fald-fractional-pitch.md): cols x rows zones over latW x latH px from (originX, originY) — a
// RATIONAL pitch latW / cols, so a 48 x 24 lattice on a 2560 x 1440 frame has 53.33-px zones. Every pixel -> zone map
// goes through ZoneEdgeX / ZoneEdgeY / ZoneOfPixel (integer math, exact on every device); an integer pitch gives
// exactly the whole-pixel formulas (c * cellW, lx / cellW). The fine grid and the kernel tables are in CELL units.
#pragma once
#include <cmath>   // FaldGainBlurFused (ceilf) for every includer

inline const char* g_faldCommonSource = R"(
cbuffer FaldCB : register(b0) {
    uint frameW; uint frameH; uint cols; uint rows;
    uint sub; uint cellW; uint cellH; uint roundIdx;                // cellW / cellH: the WIDEST zone (= the pitch of an
                                                                    // integer lattice); the zones' own spans: ZoneEdgeX / Y
    uint reachTrueC; uint reachTrueR; uint reachEstC; uint reachEstR;
    uint curveN; float white; float tmin; float area0;
    float wR; float wG; float wB; float gainMin;
    float gainMax; float driveFloor; float curveLogMin; float curveLogMax;
    uint debugMode; uint originX; uint originY; uint blurDir;      // blurDir: 0 = horizontal, 1 = vertical pass
    float fadeLo; float fadeHi; float gainSmoothFine; uint transfer; // gainSmoothFine: Gaussian sigma in fine samples (0 = off);
                                                                    // transfer: 0 = PQ codes (HDR), 1 = gamma codes (ACM SDR)
    float lumFadeLo; float lumFadeHi; uint boostN; uint starOn;     // pixel-luminance fade, as-if-white nits (lo = hi = 0: off);
                                                                    // boostN: steps of the black-frame LED boost LUT (t12),
                                                                    // 0 = no boost term (no LUT in the panel file, or the
                                                                    // flat-lattice normalisation pass): the layer is then
                                                                    // exactly the boost-less one; starOn: 1 = starfield
                                                                    // balancing (the frame every pass reads is
                                                                    // Balance(source), plan in t15), 0 = the layer without it
    float tminR; float tminG; float tminB; uint pedMode;            // tmin * the panel file's leak colour (= tmin for FLD1);
                                                                    // pedMode 0 = white pedestal, common-factor subtraction;
                                                                    // 1 = coloured pedestal, per-channel floor (GUI toggle)
    float chromaGain; float chromaLo; float chromaHi; float sdrGamma; // colour part of the pedestal term: strength + its own
                                                                    // pixel-luminance fade (lo = hi = 0: none); sdrGamma: the
                                                                    // panel's EOTF exponent (transfer 1 only)
    float tempAlphaRise; float tempAlphaFall; uint tempMode; uint tempInit; // per-cell drive state (pass 1b; DLC dlc/fald/temporal.py):
                                                                    // per-frame blend factors 1 - exp(-dt/tau) (1 = instant);
                                                                    // tempMode 0 = off (stateless), 1 = both fields from the
                                                                    // state, 2 = B_true from the state, B_est instantaneous;
                                                                    // tempInit 1 = no valid state yet: copy the drive
    float boostLitNits; float boostLitFrac; float boostDimNits; float boostDimFrac; // zone activation rule of the boost count
                                                                    // (DLC FaldModel.active_zone_fraction): a zone is non-black
                                                                    // when more than LitFrac of its pixels exceed LitNits (0 =
                                                                    // any pixel) OR more than DimFrac of them exceed DimNits
                                                                    // (as-if-white nits of the pixel's brightest channel)
    float starEven; float starRetired53; float starTargetGain; float starCapNits; // starfield balancing (DLC
                                                                    // dlc/fald/starfield.py StarfieldParams; read only when
                                                                    // starOn != 0): pull of a peak above the local target
                                                                    // (fraction of the way, log domain); word 53 held the lift
                                                                    // (retired 2026-10-26): written 0, never read; gain on the local target
                                                                    // (words 64 / 65 shape it), absolute ceiling in as-if-white
                                                                    // nits (0 = none)
    float starStrength; float starAreaLo; float starAreaHi; float starPeakHi; // overall blend; effective lit area px^2 (fully
                                                                    // star-like at / below Lo, not at all at / above Hi); zones
                                                                    // whose peak exceeds PeakHi are left alone (0 = no limit)
    float starNbLo; float starNbHi; uint starReach; uint starEvenReach; // solid drive (the tapered protection field / a carrying
                                                                    // zone's own): full effect at / below Lo, none at / above
                                                                    // Hi; full protection within starReach zones (the taper
                                                                    // adds one); the local target looks starEvenReach zones
                                                                    // (tapered window)
    float starTargetSigma; float starKeepNits; float clkW0; float clkW1; // the target sits starTargetSigma standard deviations
                                                                    // of ln peak above the local mean (0 = the geometric mean;
                                                                    // 0..4) and never below starKeepNits (as-if-white nits);
                                                                    // clkW0 / clkW1: panel clock (temporal mode 3, pass 1c
                                                                    // only), the two parity clocks' weights (unknown parity
                                                                    // 1/2 each, known parity 1 / 0)
    float clkTrue0; float clkEst0; float clkTrue1; float clkEst1;   // panel clock: per parity clock the blend toward the
                                                                    // previous frame's drives that gives its LED state of
                                                                    // this frame's first refresh (True) and of the refresh
                                                                    // before it, which the panel's compensation uses (Est):
                                                                    // 1 - (1 - closure)^ticks (C++ FaldPanelClockFactors)
    uint boostRule; float boostMeanGamma; float boostMeanThresh; uint fadeFlags; // the boost count's zone rule (work guide C12b;
                                                                    // read only when boostN != 0): 0 = LIT-or-DIM as above, 1 =
                                                                    // LIT-or-MEAN: LIT OR the zone mean of (brightest channel,
                                                                    // as-if-white nits)^boostMeanGamma >= boostMeanThresh;
                                                                    // fadeFlags: bit 0 = the frozen fade stack ASF10 + T11_TMTC
                                                                    // (FadePost, t20-t22; shared/fald_fade.h), bit 1 = item L
                                                                    // (reserved, always 0); 0 = the layer without it
    uint latW; uint latH; uint _pad78; uint runMode;                // the zone lattice's extent in px (cols * cellW / rows *
                                                                    // cellH for an integer pitch; docs/fald-fractional-pitch.md);
                                                                    // _pad78 = 0 always: FadePost's FadeNoFuse operand;
                                                                    // runMode (shared/fald_change.h, the partial path): bits
                                                                    // 0-7 = 0 full sweep (a zone per group id) / 1 partial
                                                                    // (the zones of t38, the touched-zone list, and a change
                                                                    // flag per moved value), bits 8-15 = the conv pass's
                                                                    // sub-offsets per thread, bits 16-31 = the list's length
};
Texture2D<float4> frameTex : register(t0);   // processed frame, scRGB linear BT.709, 1.0 = 80 nits
Texture2D<float>  curveTex : register(t1);   // drive vs ln(nits), curveN x 1, linear in ln(nits)
Buffer<float>     kTrue    : register(t2);   // [sub][sub][2*reachTrueR+1][2*reachTrueC+1]
Buffer<float>     kEst     : register(t3);   // [sub][sub][2*reachEstR+1][2*reachEstC+1]
Texture2D<float>  driveTex : register(t4);   // cols x rows
Texture2D<float>  bTrueTex : register(t5);   // cols*sub x rows*sub
Texture2D<float>  bEstTex  : register(t6);
Texture2D<float>  flatTrueTex : register(t7); // the same two fields for a fully driven lattice (normalisation)
Texture2D<float>  flatEstTex  : register(t8);
Texture2D<float2> gainTex     : register(t9); // R32G32F, fine grid, low-passed together (passes 2b / 2c): .x = the gain,
                                              // .y = the flat-normalised B_est the soft knee takes its CEILING from (C15)
Texture2D<float>  driveEstTex : register(t10); // conv: the drive map the ESTIMATE kernel sees (= driveTex unless tempMode 2 / 3);
                                               // pixel pass view 7: the filtered drive (driveTex = the instantaneous one)
Texture2D<float>  stateTex    : register(t11); // temporal pass: the committed drive state of the previous frame
Buffer<float>     boostLut    : register(t12); // [boostN][2]: (first non-black zone COUNT of the step, LED boost), ascending
Texture2D<float>  activeTex   : register(t13); // cols x rows: 1 = the zone counts as non-black (boost pass; pixel view 8)
Texture2D<float>  boostTex    : register(t14); // 2 x 1: [0] = the frame's LED boost, [1] = its non-black zone count (conv pass)
// Starfield balancing, all cols x rows RGBA32F, texel centres = zone centres (rules: the star-pass header below)
Texture2D<float4> starPlanTex  : register(t15); // S2 out: (w0_field, ln target, 0, ln peak) — Balance samples it bilinearly
                                                // (statistic rounds + pixel pass; view 9 loads it)
Texture2D<float4> starStatTex  : register(t16); // S0 out: (peak, speck-zone flag, sparse, solid) — read by S1 and S2
Texture2D<float4> starWTex     : register(t17); // S1 out: (wt = the zone's weight in the target average, wt * ln peak, flank
                                                // flag, speck-zone flag) — read by S2
Texture2D<float4> starPlan2Tex : register(t18); // S1 out: (ln background, near, speck-zone flag, w) — Balance samples .xy
                                                // bilinearly and loads .z of the pixel's OWN zone (nearest)
Texture2D<float4> starBgTex    : register(t19); // S0 out: (ln background, the brightest pixel's index ly * zw + lx inside
                                                // the zone (zw = the zone's own width), lit sum, a_eff) — read by S1
// The fade stack (fadeFlags bit 0; shared/fald_fade.h), R32F, read by FadePost only. fade_T and the lit floor F are
// formed per pixel in FadePost from the small maps (up-sampled exactly as the fade passes' FadeUp: FadeUpAt below):
Texture2D<float>  fadeFieldTex : register(t20); // full res: the ASF9o field of the last content frame (fade)
Texture2D<float>  floorOpenTex : register(t21); // 1/8 res: G16(grey_open(ln max(down8(mi), 0.05), 101)); F = exp(up8(.))
Texture2D<float>  reachTex     : register(t22); // full res: reach of THIS round's soft knee (OF): round 0 in the round-1
                                                // statistic, round 1 in the pixel pass
Texture2D<float>  fadeDTex     : register(t23); // 1/4 res: s - f_q (T11_TMTC); fade_T = clamp(fade + up4(.), 0, 1)
Buffer<uint>      zoneList     : register(t38); // the partial path (runMode bits 0-7 = 1): the zones (row * cols + col) a
                                                // present's rects touched, one per group of the statistic / S0 sweeps
Texture2D<float4> fieldsTex    : register(t39); // the round's fields PACKED, (B_true, flat B_true, B_est, flat B_est) per fine
                                                // texel (the conv pass's u2): SampleFields and the gain passes read all four
                                                // with ONE sample / load (bit-identical: the filter runs per channel on the
                                                // same texels with the same weights; t5..t8 stay for the dumps and the tests)
SamplerState linearClamp : register(s0);
uint RunModePartial() { return runMode & 0xFFu; }
// The zone of a sweep's group: its id in a full sweep, the list's entry on the partial path (the group's x)
uint2 SweepZone(uint3 gid) {
    if (RunModePartial() == 1u) { uint z = zoneList[gid.x]; return uint2(z % cols, z / cols); }
    return gid.xy;
}
)" /* MSVC caps ONE string literal at 16380 bytes (C2026); adjacent literals concatenate (limit 65535), so the common
      source is split here. Tools that read this header as text (DLC tests, the fxc checkers) drop the seam. */ R"(
// Ceiling-rule soft knee (correct.py KNEE_START / KNEE_CAP_TRUST; DLC tests/test_fald_transfer.py pins them equal).
static const float FALD_KNEE_START = 0.9f;
static const float FALD_KNEE_CAP_TRUST = 1.0f;

static const float3x3 BT709_TO_BT2020 = float3x3(
    0.6274040f, 0.3292820f, 0.0433136f,
    0.0690970f, 0.9195400f, 0.0113612f,
    0.0163916f, 0.0880132f, 0.8955950f);
static const float3x3 BT2020_TO_BT709 = float3x3(
    1.6604910f, -0.5876411f, -0.0728499f,
   -0.1245505f,  1.1328999f, -0.0083494f,
   -0.0181508f, -0.1005789f,  1.1187297f);

// The sRGB piecewise transfer (IEC 61966-2-1) — Windows' scRGB <-> 8/10-bit SDR composition encode under ACM,
// NOT the panel's EOTF (that is the measured power law sdrGamma). Analytic, so a code round-trips exactly.
float SrgbOetf(float L) {
    L = saturate(L);
    return (L <= 0.0031308f) ? 12.92f * L : 1.055f * pow(L, 1.0f / 2.4f) - 0.055f;
}
float SrgbEotf(float V) {
    V = saturate(V);
    return (V <= 0.04045f) ? V / 12.92f : pow((V + 0.055f) / 1.055f, 2.4f);
}

// Per-channel "as-if-white" nits of the panel-bound signal. transfer 0 (HDR): the PQ code the panel
// receives for a channel decodes to rec2020_c * 80 nits (Windows composes scRGB -> BT.2020 PQ).
// transfer 1 (ACM SDR): the panel receives code = sRGB_OETF(scRGB_c) and shows white * code^sdrGamma
// (values outside 0..1 are clipped by the composition; saturate mirrors that). The physical
// contribution of channel c is w_c times this; the model works in as-if-white units throughout.
float3 PanelNits(float3 scrgb) {
    float3 nits;
    if (transfer == 1u) {
        float3 code = float3(SrgbOetf(scrgb.r), SrgbOetf(scrgb.g), SrgbOetf(scrgb.b));   // 0..1 by construction
        nits = white * pow(max(code, 0.0f), sdrGamma);
    } else {
        nits = max(mul(BT709_TO_BT2020, scrgb), 0.0f) * 80.0f;
    }
    return nits;
}
float3 PanelNitsToScRGB(float3 nits) {
    float3 scrgb;
    if (transfer == 1u) {
        float3 code = pow(saturate(nits / white), 1.0f / sdrGamma);
        scrgb = float3(SrgbEotf(code.r), SrgbEotf(code.g), SrgbEotf(code.b));
    } else {
        scrgb = mul(BT2020_TO_BT709, nits / 80.0f);
    }
    return scrgb;
}
// scRGB value of a debug grey: 100 nits in HDR; the SDR white (1.0) under ACM.
float DebugWhite() { return (transfer == 1u) ? 1.0f : (100.0f / 80.0f); }

// Panel LED drive for a cell statistic (nits). LUT is linear in ln(nits); below the first knot the
// table already holds the floor (0 below driveFloor) exactly as FaldModel.drive_of does.
float DriveOf(float statNits) {
    if (statNits < driveFloor) return 0.0f;      // exact floor (the LUT would interpolate across the step)
    float u = saturate((log(max(statNits, 1e-3f)) - curveLogMin) / (curveLogMax - curveLogMin));
    return curveTex.SampleLevel(linearClamp, float2((u * (float)(curveN - 1) + 0.5f) / (float)curveN, 0.5f), 0);
}

// Fine-grid sample position for a frame pixel: the fine grid spans the cell LATTICE (origin..origin+
// latW), so texel centres map to pixel centres with plain clamped bilinear sampling
// (= FaldModel._bilinear) once the lattice origin is removed.
float2 FineUV(float2 px) {
    return (px - float2((float)originX, (float)originY) + 0.5f) / float2((float)latW, (float)latH);
}
// Both fields at a frame pixel, divided by their flat-lattice response (FaldModel.backlights with
// flat_norm): a uniform field then gives B_true == B_est everywhere -> gain 1, no sub-cell sawtooth,
// no border ramp.
void SampleFields(float2 px, out float bTrue, out float bEst) {
    float4 f = fieldsTex.SampleLevel(linearClamp, FineUV(px), 0);   // (B_true, flat B_true, B_est, flat B_est): one sample
    bTrue = f.x / max(f.y, 1e-6f);
    bEst  = f.z / max(f.w, 1e-6f);
}
// Pixels outside the lattice have no cell and no reference behaviour: pass them through.
bool InLattice(int2 px) {
    return px.x >= (int)originX && px.y >= (int)originY &&
           px.x < (int)(originX + latW) && px.y < (int)(originY + latH);
}
// The zones (C++ reference FaldZoneEdge / FaldZoneOfPixel): zone c spans the lattice-local pixels [ZoneEdgeX(c),
// ZoneEdgeX(c + 1)) — c * pitch rounded to the nearest pixel edge, ties to the lower one — and a pixel belongs to the
// zone its CENTRE lies in. Integer pitch P: c * P and lx / P. < 2^32 for the loader's 512 zones x 65535 px.
uint ZoneEdgeX(uint c) { return (2u * c * latW + cols - 1u) / (2u * cols); }
uint ZoneEdgeY(uint r) { return (2u * r * latH + rows - 1u) / (2u * rows); }
uint2 ZoneOfPixel(int2 px) {                                              // px inside the lattice
    uint2 l = uint2((uint)(px.x - (int)originX), (uint)(px.y - (int)originY));
    return uint2(((2u * l.x + 1u) * cols) / (2u * latW), ((2u * l.y + 1u) * rows) / (2u * latH));
}

// Starfield balancing (work guide S1; reference DLC dlc/fald/starfield.py, GPU-order twin dlc/fald/gpuemu.py).
// starfield._smoothstep: the denominator floor makes lo == hi a step at lo instead of a division by zero.
float StarSmooth(float lo, float hi, float x) {
    float t = saturate((x - lo) / max(hi - lo, 1e-12f));
    return t * t * (3.0f - 2.0f * t);
}
// The pull takes every pixel above its THRESHOLD T': the target while the target is well above the background (a
// soft star comes down as a whole: no bright ring around a pulled core), rising to the bottom of the speck band
// b + SPECK_LO (peak - b) — 0.25 of the way from the (interpolated) zone background to the (interpolated) zone peak —
// as target / background falls from GATE_HI to GATE_LO: a target below the sky leaves the sky alone, and out(m) stays
// monotone in m (starfield.pixel_rule; DLC tests/test_fald_transfer.py pins the constants equal). Nothing is ever
// brightened: the lift of dim specks toward the target was retired 2026-10-26 (it raised the haze it was meant to even,
// multiplied lifted stars x8-x50, and lit 1-px UI lines — a line across a zone passes the speck-area test).
static const float FALD_STAR_SPECK_LO = 0.25f;
static const float FALD_STAR_GATE_LO = 1.0f;
static const float FALD_STAR_GATE_HI = 2.0f;
// Flank zones (S1): a zone whose brightest pixel lies within FLANK_PX of the edge / corner it shares with a neighbour of
// LARGER peak whose own brightest pixel lies within FLANK_NEAR_PX of that edge is the spill of that neighbour's star, not
// an independent dim star: it carries no weight in the target average (starfield.FLANK_PX / FLANK_NEAR_PX).
static const int FALD_STAR_FLANK_PX = 2;
static const int FALD_STAR_FLANK_NEAR_PX = 12;
// A zone whose peak is not more than max(ABS, REL * peak) above its darkest pixel is flat — no speck to speak of: not
// star-like, and the area quotient is never formed (starfield.FLAT_ABS / FLAT_REL).
static const float FALD_STAR_FLAT_ABS = 1e-6f;
static const float FALD_STAR_FLAT_REL = 0.02f;
// A pull that ends within this (relative) of the pixel itself is no pull: the pixel is returned untouched. The
// background floor is exp(bilinear ln b) — equal to b only to rounding — so without it a sky ABOVE the target (cap_nits
// or target_gain below the sky) would be rewritten as exp(log(sky)) instead of staying bit-identical (starfield.PULL_EPS).
static const float FALD_STAR_PULL_EPS = 1e-5f;
// balance_image for one pixel: img = as-if-white nits per channel of the SOURCE frame, px = the frame pixel. The plan
// texture holds one texel per zone, so clamped hardware bilinear at FineUV (texel coordinate
// (px - origin + 0.5) / pitch - 0.5) IS starfield._bilinear_zones: interpolation between zone CENTRES, the border
// zones' values held outside the outermost centres (t18's ln background and near likewise). The protection (near) is
// interpolated on its own and applied per pixel, so a star drifting away from solid content gains weight continuously.
// OWN-ZONE GATE: a pixel is acted on only when the zone it lies in is a speck zone (nearest-zone Load of the flag) — a
// non-star shape in a zone that merely CARRIES its neighbours' weight is never touched. ONE scale for the three
// channels; a pixel nothing acts on is returned untouched (bit-identical), not as exp(log(x)). A pull stops at the
// (interpolated) zone background — a target below the sky must not dig a hole into it — and never ends above the pixel
// itself (a neighbour zone's brighter background, interpolated in, must not brighten it). px lies inside the lattice
// (the statistic pass sweeps lattice cells, the pixel pass returns the source outside it).
float3 Balance(float3 img, int2 px) {
    float m = max(img.r, max(img.g, img.b));
    if (!(m > 0.0f)) return img;                                          // the cheap tests first: black pixels ...
    uint2 zone = ZoneOfPixel(px);                                         // integer math, as view 9
    if (!(starPlan2Tex.Load(int3((int2)zone, 0)).z > 0.5f)) return img;   // ... and the own-zone gate, before the two taps
    float2 uv = FineUV(float2(px));
    float4 plan = starPlanTex.SampleLevel(linearClamp, uv, 0);           // w0, ln target, (unused, 0), ln peak
    float2 p2 = starPlan2Tex.SampleLevel(linearClamp, uv, 0).xy;         // ln background, near
    float wPx = plan.x * (1.0f - StarSmooth(starNbLo, starNbHi, p2.y));
    if (!(wPx > 0.0f)) return img;
    float safe = max(m, 1e-12f);
    float tPx = exp(plan.y);
    float bPx = exp(p2.x);                                                        // >= 1e-12: a black sky gates fully open
    float spanPx = exp(plan.w) - bPx;
    // the pull threshold T': the target while it is well above the background (the whole profile of a soft star comes
    // down together), rising to the bottom of the speck band as target / background falls from GATE_HI to GATE_LO — the
    // sky is never reached. out(m) = m up to T', m^(1 - a) T'^a above it: MONOTONE in m (no bright ring, no "donut")
    float g = StarSmooth(FALD_STAR_GATE_LO, FALD_STAR_GATE_HI, tPx / bPx);
    float tFloor = bPx + FALD_STAR_SPECK_LO * max(spanPx, 0.0f);
    float lnTp = lerp(log(max(tPx, tFloor)), plan.y, g);
    if (!(m > exp(lnTp))) return img;                                             // at / below T': untouched
    float lnShown = log(min(safe, white));                                        // from the level the panel SHOWS (clips at white)
    float outM = exp(lnShown + wPx * starEven * (lnTp - lnShown));                // pulled toward T' (log domain)
    outM = max(outM, min(bPx, safe));                                             // ... never below the background
    if (!(outM < safe * (1.0f - FALD_STAR_PULL_EPS))) return img;
    return img * (outM / safe);
}

// Raw per-sample gain with clamp and deep-dark fade (used by the gain pass; the pixel pass reads the
// smoothed version from gainTex).
float RawGain(float bTrue, float bEst) {
    bTrue = max(bTrue, 0.0f);
    float gain = clamp(bEst / max(bTrue, 1e-9f), gainMin, gainMax);
    float wfade = smoothstep(fadeLo, fadeHi, bEst);
    return 1.0f + (gain - 1.0f) * wfade;
}

// Pedestal term per channel (correct.py pedestal_adjust), as-if-white nits, BEFORE the fades. mode 0 = "white":
// tminV = tmin on all channels, one common factor on the subtraction vector so no channel goes below zero (the
// 2026-09-12 rule: the same amount from all channels, limited by the darkest one; a per-channel clip of a WHITE
// pedestal zeroed R/G and left B -> blue rims on dark edges). mode 1 = "channel": tminV = tmin * the file's leak
// colour, each channel floors on its own. delta_c = ref - actual (uniform field of the pixel's own level vs this
// context); delta > 0 is a plain lift.
float3 PedestalAdjust(float3 img, float s, float bTrue, uint mode) {
    float3 tminV = (mode == 1) ? float3(tminR, tminG, tminB) : float3(tmin, tmin, tmin);
    float3 pedRef = white * DriveOf(s) * tminV;    // pedestal a uniform field of this level carries
    float3 ped = white * bTrue * tminV;            // pedestal in THIS context
    float3 delta = pedRef - ped;
    if (mode == 1) return max(delta, -img);
    float f = 1.0f;
    if (delta.r < 0.0f) f = min(f, img.r / -delta.r);
    if (delta.g < 0.0f) f = min(f, img.g / -delta.g);
    if (delta.b < 0.0f) f = min(f, img.b / -delta.b);
    return delta * f;
}

// The fade weight the correction applies at this pixel (deep-dark fade on B_est x pixel-luminance fade).
float FadeWeight(float maxc, float bEst) {
    float wfade = smoothstep(fadeLo, fadeHi, bEst);
    if (lumFadeHi > lumFadeLo) wfade *= smoothstep(lumFadeLo, lumFadeHi, maxc);
    return wfade;
}

// The pedestal term as applied (as-if-white nits, fades included). pedMode 1 splits it: the white part (the "white"
// rule) keeps the correction's fade, the COLOUR part (adj_channel - adj_white, luminance-neutral) runs at chromaGain
// with its own pixel-luminance fade (chromaLo/Hi; 0/0 = none) on top of the B_est deep-dark fade.
float3 PedestalTerm(float3 img, float s, float bTrue, float bEst, float maxc, uint mode) {
    float3 a0 = PedestalAdjust(img, s, bTrue, 0u);
    float wfade = FadeWeight(maxc, bEst);
    if (mode != 1) return a0 * wfade;
    float3 a1 = PedestalAdjust(img, s, bTrue, 1u);
    float wchroma = smoothstep(fadeLo, fadeHi, bEst);
    if (chromaHi > chromaLo) wchroma *= smoothstep(chromaLo, chromaHi, maxc);
    return a0 * wfade + (a1 - a0) * chromaGain * wchroma;
}

// correct.py::correct_image for one pixel. img = as-if-white nits per channel (original frame);
// gs = gainTex sampled at the pixel: .x the (smoothed) gain, .y the smoothed B_est of the ceiling.
// Ceiling rule (work guide C10 + C11, 2026-09-15): ONE scale for all three channels (hue cannot rotate). Darkening
// applies the full gain; brightening goes through a soft knee on the brightest channel toward the LCD ceiling
// C = white * B_est — identity up to FALD_KNEE_START * C, then a smooth roll-off that never ends below the original
// and asymptotes to max(original, C): the layer never brightens INTO the (low-passed) ceiling (isolated highlights keep their
// gradients) and saturated highlights keep their request. The scale always lies between 1 and the gain, so no fade
// gate is needed (the fades already pulled the gain toward 1).
// C15 (2026-09-22): the ceiling's B_est is gs.y, low-passed exactly like the gain — NOT the per-pixel bEst. The fitted
// estimate kernel peaks sharply at every LED sample point; inside a small bright shape the per-pixel ceiling therefore
// let the knee brighten only near the LEDs, which printed the zone lattice once the knee bound (>= ~500 nits; owner
// photos + gpuemu, results/phone_camera_2026-09-22/circles_xraw/). The pedestal term's fade keeps the per-pixel bEst.
float3 Correct(float3 img, float bTrue, float bEst, float2 gs) {
    float gain = gs.x;
    float maxc = max(img.r, max(img.g, img.b));
    float s = min(maxc, white);
    bTrue = max(bTrue, 0.0f);
    // pixel-luminance fade (doc S33): no baseline below ~1 nit -> the correction ramps in over lumFadeLo..lumFadeHi
    // of the pixel's own level (applied after the gain low-pass, per pixel; gainTex/debug view stay unfaded)
    if (lumFadeHi > lumFadeLo) gain = 1.0f + (gain - 1.0f) * smoothstep(lumFadeLo, lumFadeHi, maxc);
    float3 u = img + PedestalTerm(img, s, bTrue, bEst, maxc, pedMode);   // GUI toggle: 0 white, 1 per-channel
    float m = max(u.r, max(u.g, u.b));
    float ge = gain;
    if (gain > 1.0f && m > 1e-9f) {
        float cap = white * max(gs.y, 1e-9f);
        float C = (FALD_KNEE_CAP_TRUST > 0.0f) ? min(white, cap / FALD_KNEE_CAP_TRUST) : white;
        float a = m * gain;
        float K = a;
        if (a > FALD_KNEE_START * C) {
            float t = (a / C - FALD_KNEE_START) / (1.0f - FALD_KNEE_START);
            K = C * (FALD_KNEE_START + (1.0f - FALD_KNEE_START) * t / (1.0f + t));
        }
        ge = max(m, K) / m;
    }
    return max(u * ge, 0.0f);
}
)" /* the second seam of the common source (same 16380-byte cap) */ R"(
// Zone sweeps — the statistic pass and starfield S0 (work guide C14). A zone's pixels k = 0 .. n - 1
// (row-major inside the zone) are cut into slices of FALD_ZONE_SLICE_PX; each (zone, slice) is one 256-thread group
// (SV_GroupID.xy = the zone, .z = the slice), so the sweep's parallelism is pixels / FALD_ZONE_SLICE_PX whatever the zone
// count. One group per zone ran an 8 x 2 edge-lit lattice at 6 ms and a 1 x 1 at 111 ms per frame on an RTX 5090.
// A zone of ONE slice (n <= FALD_ZONE_SLICE_PX: every mini-LED lattice at 4K down to ~2000 zones) is finished by its own
// group exactly as before (bit-identical). With more slices each group writes its partial (ZonePart, u2) and the pass's
// combine variant (compiled with FALD_ZONE_COMBINE, one group per zone) folds them — thread t the slices t, t + 256, ...
// in order, then the same tree — and finishes the zone. C++: FALD_ZONE_SLICE_PX / FaldZoneSlices (below the shaders);
// DLC gpuemu.Emu.zone_pow_sum emulates the order. Each zone sweeps its OWN zw x zh pixels (ZoneEdgeX / Y; a fractional
// pitch makes zones differ by one pixel); the slice count is the WIDEST zone's (cellW x cellH), so a narrower zone's last
// slice may hold no pixel — its group writes the identity partial (max 0, sums 0, min 3e38, count 0) and the fold is
// unchanged.
static const uint FALD_ZONE_SLICE_PX = 4096u;
struct ZonePart { float4 f; uint4 u; };
uint ZoneSlices() { return (cellW * cellH + FALD_ZONE_SLICE_PX - 1u) / FALD_ZONE_SLICE_PX; }
uint ZonePartIndex(uint cx, uint cy, uint s) { return (cy * cols + cx) * ZoneSlices() + s; }

// The fade stack's post-Correct scale (fadeFlags bit 0; DLC results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md 1.3, the
// study twin's make_post11 with gc = oc = None; float32 twin dlc.fald.fadeemu.fade_post). req = Correct(img, ...), img = the
// as-if-white nits Correct received, px = the pixel. ls = ln(max req / max img) (0 unless max img > 1e-9 and max req >
// 1e-12); keep = smooth(1, 3, F) smooth(0, 0.03, ls) (1 - reach); fade = fade_T (1 - keep); ONE scale for the three
// channels: req exp(ls (1 - fade) - ls) = req (max img / max req)^fade. Off: req unchanged.
// NO `precise` here: precise propagates backwards to every operation feeding the value — it would turn all of Correct
// (and the passes around it) precise, i.e. change the compiled layer with the fade OFF. The compiler may fuse a
// multiply-add in these few lines (~1e-7 relative; the WARP gate allows one FP16 step).
float FadePostSmooth(float a, float b, float x) {
    float t = saturate((x - a) / (b - a));
    return t * t * (3.0f - 2.0f * t);
}
// The fade passes' bilinear up-sampling (fald_fade_shader.h FadeUp, the twin's up(): texel-centre alignment t = (i + 0.5) /
// f - 0.5 with rcp = 1 / f, the edge texel held outside, rows first, four Loads, float32 weights) evaluated at one output
// texel: the same operations in the same order, `precise`, so the value is FadeUp's bit for bit. For the fade passes ONLY
// (fald_fade_shader.h, strict compile): a layer pass must never call it (see FadeUpAtPost).
void FadeUpAxisAt(int i, int nIn, float rcp, out int i0, out int i1, out float fr) {
    precise float t = ((float)i + 0.5f) * rcp - 0.5f;
    precise float fl = floor(t);
    i0 = clamp((int)fl, 0, nIn - 1);
    i1 = clamp(i0 + 1, 0, nIn - 1);
    precise float r = min(max(t - fl, 0.0f), 1.0f);
    if (t < 0.0f) r = 0.0f;
    else if (t > (float)(nIn - 1)) r = 1.0f;
    fr = r;
}
float FadeUpAt(Texture2D<float> t, int2 p, float rcp) {
    uint w, h;
    t.GetDimensions(w, h);
    int y0, y1, x0, x1; float fy, fx;
    FadeUpAxisAt(p.y, (int)h, rcp, y0, y1, fy);
    FadeUpAxisAt(p.x, (int)w, rcp, x0, x1, fx);
    precise float ofy = 1.0f - fy;
    precise float ofx = 1.0f - fx;
    precise float c0 = t.Load(int3(x0, y0, 0)) * ofy + t.Load(int3(x0, y1, 0)) * fy;
    precise float c1 = t.Load(int3(x1, y0, 0)) * ofy + t.Load(int3(x1, y1, 0)) * fy;
    precise float v = c0 * ofx + c1 * fx;
    return v;
}
// FadeUpAt for FadePost (the layer's pixel pass and round-1 statistic, compiled with the default flags), WITHOUT
// `precise`: precise propagates backwards through the compiled layer — with it here every operation of Correct and of
// the passes around it turned precise, the compiled layer changed and its output moved with the fade OFF (the OFF gate,
// tests/test_fald_fade.cpp). Instead every product (and every sum a product or an add follows) passes FadeNoFuse, an
// integer XOR with _pad78 (always 0: both hosts write it so): neither the compiler nor the driver can fuse a multiply
// into an add or re-associate across it, and D3D11's mul and add round correctly — the value is FadeUpAt's bit for bit.
// The texel-centre arithmetic is exact for rcp = 1/4 and 1/8 in any order (multiples of 1/16 below 2^12).
float FadeNoFuse(float x) { return asfloat(asuint(x) ^ _pad78); }
void FadeUpAxisPost(int i, int nIn, float rcp, out int i0, out int i1, out float fr) {
    float t = ((float)i + 0.5f) * rcp - 0.5f;
    float fl = floor(t);
    i0 = clamp((int)fl, 0, nIn - 1);
    i1 = clamp(i0 + 1, 0, nIn - 1);
    float r = min(max(t - fl, 0.0f), 1.0f);
    if (t < 0.0f) r = 0.0f;
    else if (t > (float)(nIn - 1)) r = 1.0f;
    fr = r;
}
float FadeUpAtPost(Texture2D<float> t, int2 p, float rcp) {
    uint w, h;
    t.GetDimensions(w, h);
    int y0, y1, x0, x1; float fy, fx;
    FadeUpAxisPost(p.y, (int)h, rcp, y0, y1, fy);
    FadeUpAxisPost(p.x, (int)w, rcp, x0, x1, fx);
    const float ofy = 1.0f - fy;
    const float ofx = 1.0f - fx;
    const float c0 = FadeNoFuse(FadeNoFuse(t.Load(int3(x0, y0, 0)) * ofy) + FadeNoFuse(t.Load(int3(x0, y1, 0)) * fy));
    const float c1 = FadeNoFuse(FadeNoFuse(t.Load(int3(x1, y0, 0)) * ofy) + FadeNoFuse(t.Load(int3(x1, y1, 0)) * fy));
    return FadeNoFuse(FadeNoFuse(c0 * ofx) + FadeNoFuse(c1 * fx));
}
float3 FadePost(float3 req, float3 img, int2 px) {
    if ((fadeFlags & 1u) == 0u) return req;
    float mi = max(img.r, max(img.g, img.b));
    float mr = max(req.r, max(req.g, req.b));
    float ls = (mi > 1e-9f && mr > 1e-12f) ? log(max(mr, 1e-12f) / max(mi, 1e-9f)) : 0.0f;
    // the lit floor F = exp(up8(the 1/8-res opening)) and fade_T = clamp(fade + up4(s - f_q), 0, 1): the values the fade
    // passes' FadeUp (fOp bit 0 = exp) + FadePix TOUT wrote to full-res maps before (fald_fade.h)
    const float floorF = exp(FadeUpAtPost(floorOpenTex, px, 0.125f));
    const float fadeT = min(max(FadeNoFuse(fadeFieldTex.Load(int3(px, 0)) + FadeUpAtPost(fadeDTex, px, 0.25f)), 0.0f), 1.0f);
    float lit = FadePostSmooth(1.0f, 3.0f, floorF);
    float pos = FadePostSmooth(0.0f, 0.03f, ls);
    float keep = lit * pos * (1.0f - reachTex.Load(int3(px, 0)));
    float fade = fadeT * (1.0f - keep);
    float lsEff = ls * (1.0f - fade);
    // = max(req * exp(lsEff - ls), 0): req >= 0 (Correct clamps it) and exp >= 0, so the optimiser drops a plain max — and
    // with it the max's NaN -> 0 (an inf input pixel: ls = ln(inf / inf) = NaN). Seen on WARP 2026-10-20: the NaN reached
    // the round-1 statistic, where min(NaN, white) counts the pixel as white (that zone's drive 0.59 instead of 0.10).
    // An integer NaN test the optimiser cannot fold restores the max: NaN -> 0, everything else unchanged.
    float3 o = req * exp(lsEff - ls);
    return ((asuint(o) & 0x7fffffffu) > 0x7f800000u) ? 0.0f : o;
}
)" /* the third seam: the always-on core, rounds 2 / 3 (shared/fald_core2.h; off = coreFlags 0, CoreGain = identity). The hosts
      bind t42 / t48 per consumer: round 0's fields for the round-1 statistic (compute), round 1's for the pixel pass. */ R"(
cbuffer FaldCoreCB : register(b3) {
    uint coreFlags; float coreA; uint coreReset; float coreCutThr;       // flags: bit 0 alpha, 1 deficit (2 soft: reserved)
    float coreMuInf; float coreLh; float coreZ; float coreKStart;        // alpha(L) = clamp(muInf L / (L + Lh) - z, 0, 1)
    float coreBeta; float coreTau; float coreFixedRcp; uint _corePad;    // spread gate; 1 / the cut sum's fixed point
    float coreW0; float coreW1; float coreW2; float coreW3;              // the sigma-0.7 Gaussian's taps (centre .. 3)
};
Texture2D<float4> coreStatTex  : register(t40);   // P, M, lnG (stat round 0)
Texture2D<float4> coreZoneTex  : register(t41);   // alpha_z, k, ln P
Texture2D<float2> coreFieldTex : register(t42);   // alpha_z, D (this round)
Buffer<uint>      coreCutSum   : register(t43);   // [0]: sum over zones of |d ln max(M, 0.05)| (fixed point)
Texture2D<float>  coreNodeDTex : register(t48);   // round 3 (coreFlags bit 3): the deficit D on the FINE grid; round 5
                                                  // (bit 6): the brightening cap A there
// Correct's gain at both call sites (fald_core2.h): brightening shrunk by alpha, minus the deficit D (round 5: capped at A);
// darkening untouched. With coreFlags 0 (the core off, b3 bound with zeros or not at all) the input is returned unchanged.
float2 CoreGain(float2 g, float2 px) {
    if ((coreFlags & 3u) == 0u || !(g.x > 1.0f)) return g;
    float lg = log(g.x);
    if ((coreFlags & 8u) != 0u) {             // round 3 (KC3): alpha from the zone field, D from the fine-grid field
        const float2 uv = FineUV(px);
        if ((coreFlags & 1u) != 0u) lg *= saturate(coreZoneTex.SampleLevel(linearClamp, uv, 0).x);
        if ((coreFlags & 2u) != 0u) lg = max(lg - max(coreNodeDTex.SampleLevel(linearClamp, uv, 0), 0.0f), 0.0f);
        if ((coreFlags & 64u) != 0u) lg = min(lg, max(coreNodeDTex.SampleLevel(linearClamp, uv, 0), 0.0f));   // round 5 (KC5): the cap A
    } else {                                  // round 2 (KC2c): both from the zone field
        float2 f = coreFieldTex.SampleLevel(linearClamp, FineUV(px), 0);
        if ((coreFlags & 1u) != 0u) lg *= saturate(f.x);
        if ((coreFlags & 2u) != 0u) lg = max(lg - max(f.y, 0.0f), 0.0f);
    }
    g.x = exp(lg);
    return g;
}
bool CoreResetNow() {
    float mean = (float)coreCutSum[0] * coreFixedRcp / (float)(cols * rows);
    return coreReset != 0u || mean > coreCutThr;
}
float CoreW(int d) { d = abs(d); return d == 0 ? coreW0 : (d == 1 ? coreW1 : (d == 2 ? coreW2 : coreW3)); }
)";

// Pass 1: per-cell area statistic -> drive. One thread group per cell, 256 threads sweep the block.
// With a boost LUT (boostN != 0) the same sweep counts the zone's pixels above boostLitNits / boostDimNits and writes
// the zone's NON-BLACK flag (u1) — on the frame the PANEL receives: the source in round 0, the corrected frame in
// round 1 (DLC correct_image re-reads the boost from each round's request). boostRule 1 (LIT-or-MEAN, C12b) also sums
// nits^boostMeanGamma over the zone's pixels (exp(gamma * log) under mc > 0: no pow(0) / negative / NaN case) and
// replaces the DIM test by mean >= boostMeanThresh; rule 0 computes exactly what it did before the rule existed.
// Zones of more than FALD_ZONE_SLICE_PX pixels are swept in slices (the rules above ZoneSlices in the common source).
// The frame's LED boost (2026-10-22, work guide core-speed; before: a 1-group pass per round, ~5-18 us of a dispatch each):
// every finished zone adds (its new flag - its old flag) to boostCount[round], so the counter is the map's non-black zone
// count after any sweep, full or partial (the flag map and the counter start zeroed; a device loss rebuilds them); the
// decide pass (g_faldDecideSource) that follows each round's sweep reads it and looks the staircase up by COUNT exactly
// as FaldModel.boost_of_fraction (the last step whose first count is <= N; below the first step 1) into the 2 x 1
// texture the conv reads. An integer count in any order: the same N, the same step, bit for bit the separate pass.
// The partial path (shared/fald_change.h; runMode): the group's zone comes from the touched-zone list, and a drive or a
// flag that differs from the texture's previous value sets changeFlags[round] — a zone the rects did not touch has the
// same pixels, so its values stand unchanged.
inline const char* g_faldStatSource = R"(
RWTexture2D<float> driveOut : register(u0);
RWTexture2D<float> activeOut : register(u1);
RWStructuredBuffer<ZonePart> zonePart : register(u2);   // slice partials (zones of more than one slice only)
RWStructuredBuffer<uint> boostCount : register(u3);     // [round] = the non-black zones of that round's map (boost LUT only):
                                                        // every finished zone adds (new flag - old flag), so the count
                                                        // follows the map whether the sweep is full or partial
RWTexture2D<float4> coreStatOut : register(u4);          // the always-on core (fald_core2.h), round 0 only: P, M, lnG over
                                                        // ALL the zone's pixels (null = the core off: never written)
RWStructuredBuffer<uint> changeFlags : register(u5);    // the partial path: [round] = a drive / flag of that round moved,
                                                        // [2] = the starfield's S0 moved, [3] = the caller's "the frame changed
                                                        // everywhere" (read and cleared by the decide pass; fald_change.h);
                                                        // [0] bit 1 (2u) = a zone's core statistics / node peaks moved
RWTexture2D<uint> coreNodePeakOut : register(u6);        // core round 3: per FINE node the peak min(mc, white) of the pixels whose
                                                        // nearest node it is — the finished zone's S x S nodes stored whole
RWStructuredBuffer<uint> coreNodePart : register(u7);    // core round 3, lattices of more than one slice: each slice's S x S node
                                                        // peaks ([ZonePartIndex * S * S + node]), folded by the combine variant
groupshared uint gNodeMax[256];                          // round 3: this zone's S x S node peaks (asuint, S <= 16)
groupshared float gMax[256];
groupshared float gSum[256];
groupshared uint gLit[256];
groupshared uint gDim[256];
groupshared float gPow[256];
groupshared float gCMax[256];
groupshared float gCSum[256];
groupshared float gCLn[256];

[numthreads(256, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint2 zone = SweepZone(gid);                                  // the group's zone (full sweep) or the list's (partial)
    uint cx = zone.x, cy = zone.y;
    uint x0 = ZoneEdgeX(cx), y0 = ZoneEdgeY(cy);                  // the zone's own pixels (lattice-local)
    uint zw = ZoneEdgeX(cx + 1u) - x0, zh = ZoneEdgeY(cy + 1u) - y0;
    uint n = zw * zh;
    float m = 0.0f, sum = 0.0f;
    uint lit = 0, dim = 0;
    float powSum = 0.0f;
    const bool coreOn = roundIdx == 0 && (coreFlags & 3u) != 0u;   // core round 2: the source's zone statistics
    const bool coreNodes = coreOn && (coreFlags & 8u) != 0u && sub <= 16u;   // round 3: the per-node peaks too
    float cMax = 0.0f, cSum = 0.0f, cLn = 0.0f;
    if (coreNodes) { gNodeMax[tid.x] = 0u; }
    GroupMemoryBarrierWithGroupSync();
#ifdef FALD_ZONE_COMBINE
    for (uint s = tid.x; s < ZoneSlices(); s += 256) {            // the zone's slice partials, in order
        ZonePart q = zonePart[ZonePartIndex(cx, cy, s)];
        m = max(m, q.f.x); sum += q.f.y; powSum += q.f.z; lit += q.u.x; dim += q.u.y;
        if (coreOn) { cMax = max(cMax, q.f.w); cSum += asfloat(q.u.z); cLn += asfloat(q.u.w); }
    }
    if (coreNodes && tid.x < sub * sub) {                         // the zone's node peaks: the max over its slices' partials
        uint v = 0u;
        for (uint s2 = 0; s2 < ZoneSlices(); s2++) v = max(v, coreNodePart[ZonePartIndex(cx, cy, s2) * sub * sub + tid.x]);
        gNodeMax[tid.x] = v;
    }
#else
    uint kEnd = min(n, (gid.z + 1u) * FALD_ZONE_SLICE_PX);
    // thread t sweeps k = t, t + 256, ... of its slice (the same pixels in the same order as ever); k = ly * zw + lx is
    // stepped with a carry (256 = sq * zw + sr) instead of a division and a modulo per pixel
    uint kS = gid.z * FALD_ZONE_SLICE_PX + tid.x, lx = kS % zw, ly = kS / zw, sq = 256u / zw, sr = 256u % zw;
    for (uint k = kS; k < kEnd; k += 256, ly += sq + ((lx + sr >= zw) ? 1u : 0u), lx = (lx + sr >= zw) ? lx + sr - zw : lx + sr) {
        uint px = originX + x0 + lx;
        uint py = originY + y0 + ly;
        if (px >= frameW || py >= frameH) continue;
        float3 img = PanelNits(frameTex.Load(int3(px, py, 0)).rgb);
        if (starOn != 0u) img = Balance(img, int2((int)px, (int)py));   // the frame the layer works on (S1)
        if (roundIdx == 1) {
            float bT, bE; SampleFields(float2((float)px, (float)py), bT, bE);
            float2 g = gainTex.SampleLevel(linearClamp, FineUV(float2((float)px, (float)py)), 0);
            img = FadePost(Correct(img, bT, bE, CoreGain(g, float2((float)px, (float)py))), img, int2((int)px, (int)py));   // fade stack: round 0's reach (t22)
        }
        float mc = max(img.r, max(img.g, img.b));
        float s = min(mc, white);
        if (s > driveFloor) { m = max(m, s); sum += s; }
        if (coreOn) { cMax = max(cMax, s); cSum += mc; cLn += log(max(mc, 0.05f)); }
        if (coreNodes) {                                          // the pixel's NEAREST fine node, inside this zone
            const float tx = ((float)(px - originX) + 0.5f) * (float)(cols * sub) / (float)latW - 0.5f;
            const float ty = ((float)(py - originY) + 0.5f) * (float)(rows * sub) / (float)latH - 0.5f;
            const int nx = clamp((int)floor(tx + 0.5f) - (int)(cx * sub), 0, (int)sub - 1);
            const int ny = clamp((int)floor(ty + 0.5f) - (int)(cy * sub), 0, (int)sub - 1);
            InterlockedMax(gNodeMax[ny * (int)sub + nx], asuint(s));
        }
        if (boostN != 0u) {
            if (mc > boostLitNits) lit++;
            if (mc > boostDimNits) dim++;
            if (boostRule == 1u && mc > 0.0f) powSum += exp(boostMeanGamma * log(mc));
        }
    }
#endif
    gMax[tid.x] = m; gSum[tid.x] = sum;
    gLit[tid.x] = lit; gDim[tid.x] = dim;
    gPow[tid.x] = powSum;
    if (coreOn) { gCMax[tid.x] = cMax; gCSum[tid.x] = cSum; gCLn[tid.x] = cLn; }
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) {
            gMax[tid.x] = max(gMax[tid.x], gMax[tid.x + stride]);
            gSum[tid.x] += gSum[tid.x + stride];
            gLit[tid.x] += gLit[tid.x + stride];
            gDim[tid.x] += gDim[tid.x + stride];
            gPow[tid.x] += gPow[tid.x + stride];
            if (coreOn) {
                gCMax[tid.x] = max(gCMax[tid.x], gCMax[tid.x + stride]);
                gCSum[tid.x] += gCSum[tid.x + stride];
                gCLn[tid.x] += gCLn[tid.x + stride];
            }
        }
        GroupMemoryBarrierWithGroupSync();
    }
#ifndef FALD_ZONE_COMBINE
    if (ZoneSlices() > 1u) {                                      // one slice of a larger zone: its partials, finished later
        if (coreNodes && tid.x < sub * sub) coreNodePart[ZonePartIndex(cx, cy, gid.z) * sub * sub + tid.x] = gNodeMax[tid.x];
        if (tid.x == 0) {
            ZonePart q;
            q.f = float4(gMax[0], gSum[0], gPow[0], coreOn ? gCMax[0] : 0.0f);
            q.u = uint4(gLit[0], gDim[0], coreOn ? asuint(gCSum[0]) : 0u, coreOn ? asuint(gCLn[0]) : 0u);
            zonePart[ZonePartIndex(cx, cy, gid.z)] = q;
        }
        return;
    }
#endif
    if (coreNodes && tid.x < sub * sub) {                         // the finished zone's node peaks, stored whole (an order-free
        const uint2 node = uint2(cx * sub + tid.x % sub, cy * sub + tid.x / sub);   // max: = the prototype's cleared InterlockedMax map);
        if (RunModePartial() == 1u && coreNodePeakOut[node] != gNodeMax[tid.x]) InterlockedOr(changeFlags[0], 2u);   // moved -> the core
        coreNodePeakOut[node] = gNodeMax[tid.x];
    }
    if (tid.x == 0) {
        float stat = min(gMax[0], gSum[0] / area0);   // min(brightest lit px, sum lit nits*px^2 / A0)
        float drive = DriveOf(stat);
        // the partial path: a drive that moved flags this round (the decide pass then runs everything downstream)
        if (RunModePartial() == 1u && driveOut[uint2(cx, cy)] != drive) InterlockedOr(changeFlags[roundIdx], 1u);
        driveOut[uint2(cx, cy)] = drive;
        if (coreOn) {                                 // P, M, lnG over ALL the zone's pixels (round 0; fald_core2.h) — on the
            const float M = gCSum[0] / (float)n;      // partial path a value that moved flags the core (bit 1 of [0])
            const float4 cs = float4(gCMax[0], M, gCLn[0] / (float)n, 0.0f);
            if (RunModePartial() == 1u && any(coreStatOut[uint2(cx, cy)].xyz != cs.xyz)) InterlockedOr(changeFlags[0], 2u);
            coreStatOut[uint2(cx, cy)] = cs;
        }
        if (boostN != 0u) {
            // LIT-or-DIM / LIT-or-MEAN (FaldModel.active_zones): fractions of the zone's pixels (the lattice lies inside
            // the frame — FaldLatticeFits — so every zone has its zw * zh of them)
            float litF = (float)gLit[0] / (float)n;
            float dimF = (float)gDim[0] / (float)n;
            bool second = (boostRule == 1u) ? (gPow[0] / (float)n >= boostMeanThresh) : (dimF > boostDimFrac);
            bool active = litF > boostLitFrac || second;
            // the round's non-black count follows the flag map: + (new - old) per zone (the map starts all zero)
            uint newA = active ? 1u : 0u, oldA = (activeOut[uint2(cx, cy)] > 0.5f) ? 1u : 0u;
            if (newA != oldA) {
                InterlockedAdd(boostCount[roundIdx], newA - oldA);
                if (RunModePartial() == 1u) InterlockedOr(changeFlags[roundIdx], 1u);
            }
            activeOut[uint2(cx, cy)] = active ? 1.0f : 0.0f;
        }
    }
}
)";

// =====================================================================================================================
// STARFIELD BALANCING — THE RULES (final, 2026-09-19; experimental, default off). Reference: DLC dlc/fald/starfield.py
// (module docstring = this text), GPU-order twin dlc/fald/gpuemu.py; tests DLC tests/test_fald_starfield*.py.
// s = a pixel's brightest channel (as-if-white nits) clipped to white; ss = StarSmooth; all zone textures cols x rows
// RGBA32F with texel centres = zone centres.
//
// Per zone                                                                                          pass  texture.channel
//   peak, total   floored statistic (pixels above driveFloor), has = peak > 0, drive = DriveOf(min(peak, total / A0))  S0
//   peakAll, b, sumAll, n   over ALL in-frame pixels (no floor): b = min = the zone's BACKGROUND            S0
//   arg     = ly * zw + lx of the brightest pixel (zw x zh = the zone's own size; ties: nearest a zone border, key
//             max(|2lx - (zw-1)| zh, |2ly - (zh-1)| zw), then the first in row-major order)                 S0   bg.y
//   speck   = peakAll - b > max(FLAT_ABS 1e-6, FLAT_REL 0.02 * peakAll)                                     S0
//   a_eff   = (sumAll - b n) / (peakAll - b)   px^2 at the peak level above the background (0 if !speck)    S0   bg.w
//   sparse  = 1 - ss(areaLo, areaHi, a_eff) [x 1 - ss(peakHi, 2 peakHi, peak) if peakHi > 0], 0 unless has && speck   stat.z
//   solid   = (1 - sparse) * drive * has                                                                    S0   stat.w
//   spk     = has && speck && a_eff < areaHi          the SPECK-ZONE flag                                   S0   stat.y
//   near    = max over chebyshev d <= reach + 1 of solid * clamp((reach + 1 - d) / 2, 0, 1)   (tapered)     S1   plan2.y
//   w       = sparse * strength * (1 - ss(nbLo, nbHi, near))   the zone's protected weight                  S1   plan2.w
//   flank   = the zone's brightest pixel hugs (FLANK_PX 2) the edge / corner shared with a neighbour of LARGER peak whose
//             brightest pixel lies just behind it (FLANK_NEAR_PX 12): the spill of that star; n = neighbours with that
//             geometry and an EQUAL peak (a flat-topped straddler: one vote, shared);
//             wt = flank ? 0 : w / (1 + n) = the weight in the target average (a flank zone keeps w / w0_field: its
//             pixels are still acted on, with the shared target)                                          S1   w.x = wt, w.y = wt ln peak, w.z = flank
//   target  = max(exp(mean + targetSigma * std) * targetGain, keepNits), min capNits if > 0, min white; mean / std of ln peak with
//             weights wt * (E + 1 - d) / (E + 1) over box(evenReach) (tapered; the variance is summed ABOUT the mean in a
//             second sweep); own peak if no weight within reach                                             S2
//   w0_field= spk ? sparse * strength : mean(sparse * strength over the 3x3 speck zones) * (1 - ss(nbLo, nbHi, solid))   plan.x
//   ln_t = ln target (ln white if 0);  ln_pk = spk ? ln peak : ln_t;  plan.z = 0 (was the lift, retired 2026-10-26)
//                                                                                                           S2   plan.yzw
//   ln_b = ln max(b, 1e-12)                                                                                 S0   bg.x -> S1 plan2.x
// Textures: stat (S0 u0) = peak, spk, sparse, solid | bg (S0 u1) = ln b, arg, total, a_eff | w (S1 u0) = wt, wt ln peak,
//   flank, spk | plan2 (S1 u1) = ln b, near, spk, w | plan (S2 u0) = w0_field, ln_t, 0, ln_pk.
// Bindings: S0 reads t0 frame + t1 curve; S1 reads t16 stat + t19 bg; S2 reads t16 stat + t17 w; the statistic rounds and
//   the pixel pass (Balance) read t15 plan + t18 plan2. Order per frame: S0, S1, S2, then the unchanged layer.
//
// Per pixel (Balance; m = its brightest channel): plan and plan2.xy sampled BILINEARLY at FineUV (= between zone centres,
//   border zones held), plan2.z loaded for the pixel's OWN zone (nearest).
//   w_px = w0_px * (1 - ss(nbLo, nbHi, near_px));   untouched unless w_px > 0, m > 0 and the own zone is a speck zone
//   pull threshold T' = exp(lerp(ln max(t_px, b_px + 0.25 max(pk_px - b_px, 0)), ln t_px, ss(GATE_LO 1, GATE_HI 2, t_px / b_px)))
//   m >  T'  : mc = min(m, white) (the level the panel SHOWS); out = max(exp(ln mc + w_px * even * (ln T' - ln mc)),
//              min(b_px, m))   MONOTONE in m (out(T') = T', slope 1 - w_px even >= 0, flat above white);
//              acts if out < m (1 - PULL_EPS 1e-5)
//   m <= T'  : untouched (nothing is ever brightened)
//   acts -> rgb * out / m (one scale, hue-preserving); else the pixel is returned BIT-identical.
//   Consequences: a field whose peaks all lie below keepNits is bit-identical; with the target below / near the sky a star
//   stops at the bottom of its speck band b + 0.25 (pk - b), so capNits below that is not reached.
// Settings (CB words 35, 52-65; defaults): on 0 | even 0.8, (word 53 unused, 0), targetGain 1, capNits 0 | strength 1, areaLo 40, areaHi 160,
//   peakHi 0 | nbLo 0.15, nbHi 0.30, reach 2 (0..4), evenReach 8 (0..12) | targetSigma 0 (0..4), keepNits 100 (0..10000).
// =====================================================================================================================

// Starfield pass S0 (starOn only): the star statistic of the SOURCE frame (starfield.zone_stats / zone_levels /
// zone_plan). Two statistics per zone over every in-frame pixel, s = brightest channel clipped to white:
//   floored (exactly the statistic pass's): peak = max, sum over pixels above driveFloor -> has = peak > 0 and the
//     zone's drive DriveOf(min(peak, sum / A0));
//   un-gated (NO drive floor — real content never sits on code 0, a lit sky would count into sum / peak): peakAll = max,
//     b = min (the zone's BACKGROUND), sumAll, n = pixel count.
//   a_eff = (sumAll - b n) / (peakAll - b): px^2 at the peak level ABOVE the background; a flat / near-flat zone
//     (peakAll - b <= max(FLAT_ABS, FLAT_REL peakAll)) is not star-like and the quotient is never formed;
//   sparse = 1 - smoothstep(areaLo, areaHi, a_eff) [x the peakHi term] where the peak is LIT (has), else 0;
//   solid = (1 - sparse) * drive * has — a star-free zone of a lit sky is "solid" at its dim drive (below nbLo for a
//     dark sky, so the sky does not protect the stars sitting on it);
//   spk = has && speck && a_eff < areaHi: the SPECK-ZONE flag (a lit peak that rises above the zone's own background
//     with a STAR-SIZED area above it — a grainy sky passes the flat rule but reads about half the zone) — the carry,
//     the peak field of the plan pass and the own-zone gate of Balance key on it;
//   arg = the brightest pixel's position inside the zone, ly * zw + lx (un-gated maximum; ties: the pixel nearest a
//     zone border — mirror-symmetric — then the first in row-major order; StarEdgeKey / StarBetter) — S1's flank test.
// u0 = (peak, spk, sparse, solid); u1 = (ln b, arg, lit sum, a_eff) — S1 copies ln b into the texture the pixels sample.
// Zones of more than FALD_ZONE_SLICE_PX pixels are swept in slices (the rules above ZoneSlices in the common source); the
// brightest pixel's position merges across slices by the same total order (StarBetter), so it is the same pixel.
inline const char* g_faldStarStatSource = R"(
RWTexture2D<float4> starStatOut : register(u0);
RWTexture2D<float4> starBgOut : register(u1);
RWStructuredBuffer<ZonePart> zonePart : register(u2);   // slice partials (zones of more than one slice only)
RWStructuredBuffer<uint> changeFlags : register(u5);    // the partial path: [2] = a zone's stat / bg moved (the statistic pass's u5)
groupshared float gMax[256];
groupshared float gSum[256];
groupshared float gMaxAll[256];
groupshared float gMin[256];
groupshared float gSumAll[256];
groupshared uint gCount[256];
groupshared uint gArg[256];

// Position of the zone's brightest pixel: k = ly * zw + lx (zw x zh = the zone's own size). Ties: the pixel nearest the
// zone border (largest key), then the first in row-major order — a total order, so the parallel reduction gives the same
// pixel whatever its shape (the emulator applies the same rule). A flat-topped speck at an edge then counts as AT the
// edge on either side.
uint StarEdgeKey(uint k, uint zw, uint zh) {
    int lx = (int)(k % zw), ly = (int)(k / zw);
    return (uint)max(abs(2 * lx - ((int)zw - 1)) * (int)zh, abs(2 * ly - ((int)zh - 1)) * (int)zw);
}
bool StarBetter(float va, uint ka, float vb, uint kb, uint zw, uint zh) {   // is (vb, kb) preferred over (va, ka)?
    if (vb != va) return vb > va;
    uint ea = StarEdgeKey(ka, zw, zh), eb = StarEdgeKey(kb, zw, zh);
    if (eb != ea) return eb > ea;
    return kb < ka;
}

[numthreads(256, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint2 zone = SweepZone(gid);                                  // the group's zone (full sweep) or the list's (partial)
    uint cx = zone.x, cy = zone.y;
    uint x0 = ZoneEdgeX(cx), y0 = ZoneEdgeY(cy);                  // the zone's own pixels (lattice-local)
    uint zw = ZoneEdgeX(cx + 1u) - x0, zh = ZoneEdgeY(cy + 1u) - y0;
    uint n = zw * zh;
    float m = 0.0f, sum = 0.0f, mAll = -1.0f, mn = 3.0e38f, sumAll = 0.0f;
    uint count = 0, kAll = 0;
#ifdef FALD_ZONE_COMBINE
    for (uint s = tid.x; s < ZoneSlices(); s += 256) {            // the zone's slice partials, in order
        ZonePart q = zonePart[ZonePartIndex(cx, cy, s)];
        m = max(m, q.f.x); sum += q.f.y;
        if (StarBetter(mAll, kAll, q.f.z, q.u.x, zw, zh)) { mAll = q.f.z; kAll = q.u.x; }
        mn = min(mn, q.f.w); sumAll += asfloat(q.u.z); count += q.u.y;
    }
#else
    uint kEnd = min(n, (gid.z + 1u) * FALD_ZONE_SLICE_PX);
    uint kS = gid.z * FALD_ZONE_SLICE_PX + tid.x, lx = kS % zw, ly = kS / zw, sq = 256u / zw, sr = 256u % zw;   // as the statistic pass
    for (uint k = kS; k < kEnd; k += 256, ly += sq + ((lx + sr >= zw) ? 1u : 0u), lx = (lx + sr >= zw) ? lx + sr - zw : lx + sr) {
        uint px = originX + x0 + lx;
        uint py = originY + y0 + ly;
        if (px >= frameW || py >= frameH) continue;          // only in-frame pixels count (n, min and sums alike)
        float3 img = PanelNits(frameTex.Load(int3(px, py, 0)).rgb);
        float s = min(max(img.r, max(img.g, img.b)), white);
        if (s > driveFloor) { m = max(m, s); sum += s; }
        if (StarBetter(mAll, kAll, s, k, zw, zh)) { mAll = s; kAll = k; }
        mn = min(mn, s); sumAll += s; count++;
    }
#endif
    gMax[tid.x] = m; gSum[tid.x] = sum;
    gMaxAll[tid.x] = mAll; gArg[tid.x] = kAll; gMin[tid.x] = mn; gSumAll[tid.x] = sumAll; gCount[tid.x] = count;
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) {
            gMax[tid.x] = max(gMax[tid.x], gMax[tid.x + stride]);
            gSum[tid.x] += gSum[tid.x + stride];
            if (StarBetter(gMaxAll[tid.x], gArg[tid.x], gMaxAll[tid.x + stride], gArg[tid.x + stride], zw, zh)) {
                gMaxAll[tid.x] = gMaxAll[tid.x + stride]; gArg[tid.x] = gArg[tid.x + stride];
            }
            gMin[tid.x] = min(gMin[tid.x], gMin[tid.x + stride]);
            gSumAll[tid.x] += gSumAll[tid.x + stride];
            gCount[tid.x] += gCount[tid.x + stride];
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0) {
#ifndef FALD_ZONE_COMBINE
        if (ZoneSlices() > 1u) {                                  // one slice of a larger zone: its partial, finished later
            ZonePart q;
            q.f = float4(gMax[0], gSum[0], gMaxAll[0], gMin[0]);
            q.u = uint4(gArg[0], gCount[0], asuint(gSumAll[0]), 0u);
            zonePart[ZonePartIndex(cx, cy, gid.z)] = q;
            return;
        }
#endif
        float peak = gMax[0], total = gSum[0];
        float peakAll = max(gMaxAll[0], 0.0f), sumAll0 = gSumAll[0];   // (-1 = a thread / zone without an in-frame pixel)
        float cnt = (float)gCount[0];
        float b = (gCount[0] != 0u) ? gMin[0] : 0.0f;        // a zone without an in-frame pixel: background 0, nothing below acts
        bool has = peak > 0.0f;                              // the brightest pixel is LIT (above the drive floor)
        float span = peakAll - b;
        bool speck = span > max(FALD_STAR_FLAT_ABS, FALD_STAR_FLAT_REL * peakAll);
        float aEff = speck ? (sumAll0 - b * cnt) / max(span, 1e-12f) : 0.0f;
        float sparse = (has && speck) ? 1.0f - StarSmooth(starAreaLo, starAreaHi, aEff) : 0.0f;
        if (starPeakHi > 0.0f) sparse *= 1.0f - StarSmooth(starPeakHi, 2.0f * starPeakHi, peak);
        float solid = has ? (1.0f - sparse) * DriveOf(min(peak, total / area0)) : 0.0f;
        float4 statV = float4(peak, (has && speck && aEff < starAreaHi) ? 1.0f : 0.0f, sparse, solid);
        float4 bgV = float4(log(max(b, 1e-12f)), (gCount[0] != 0u) ? (float)gArg[0] : 0.0f, total, aEff);
        // the partial path: a zone whose star statistic moved flags the starfield (its plan is then re-formed everywhere)
        if (RunModePartial() == 1u && (any(starStatOut[uint2(cx, cy)] != statV) || any(starBgOut[uint2(cx, cy)] != bgV)))
            InterlockedOr(changeFlags[2], 1u);
        starStatOut[uint2(cx, cy)] = statV;
        starBgOut[uint2(cx, cy)] = bgV;
    }
}
)";

// Starfield pass S1: the tapered protection field, the flank test and the zone weight of the target average.
//   near = max over zones at chebyshev distance d <= starReach + 1 of solid * k(d), k(d) = clamp((starReach + 1 - d) / 2,
//          0, 1) (reach 2: d <= 1 -> 1, d = 2 -> 0.5, d = 3 -> 0; reach 0: the zone itself at 0.5); zero outside the lattice
//   w    = sparse * strength * (1 - smoothstep(nbLo, nbHi, near))
//   flank: the zone's brightest pixel lies within FLANK_PX of the edge / corner shared with a neighbour of LARGER peak
//          whose own brightest pixel lies within FLANK_NEAR_PX behind that edge (and, along a shared edge, within
//          FLANK_NEAR_PX of this one): the spill of that star, no independent dim star. The same geometry with an EQUAL
//          peak = a flat-topped feature straddling the border: a partner, the zones share one vote.
//   wt   = flank ? 0 : w / (1 + partners)   — the weight in S2's target sums (w itself still drives the zone's pixels)
// u0 = (wt, wt * ln peak, flank, spk) for the sums of S2; u1 = the second field texture the pixels read:
// (ln background [copied from S0's t19], near, spk, w).
inline const char* g_faldStarWeightSource = R"(
RWTexture2D<float4> starWOut : register(u0);
RWTexture2D<float4> starPlan2Out : register(u1);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float4 st = starStatTex.Load(int3(id.xy, 0));
    int R = (int)starReach + 1;
    float near = 0.0f;
    for (int dy = -R; dy <= R; dy++) {
        int y = (int)id.y + dy; if (y < 0 || y >= (int)rows) continue;
        for (int dx = -R; dx <= R; dx++) {
            int x = (int)id.x + dx; if (x < 0 || x >= (int)cols) continue;
            float k = clamp((float)(R - max(abs(dx), abs(dy))) * 0.5f, 0.0f, 1.0f);
            near = max(near, starStatTex.Load(int3(x, y, 0)).a * k);
        }
    }
    float w = st.b * starStrength * (1.0f - StarSmooth(starNbLo, starNbHi, near));
    // flank: this zone's brightest pixel hugs the edge / corner shared with a neighbour of LARGER peak whose own brightest
    // pixel lies just behind that edge (and near this one along it): one feature straddling the border
    uint k0 = (uint)(starBgTex.Load(int3(id.xy, 0)).y + 0.5f);
    int zw0 = (int)(ZoneEdgeX(id.x + 1u) - ZoneEdgeX(id.x)), zh0 = (int)(ZoneEdgeY(id.y + 1u) - ZoneEdgeY(id.y));   // own size
    int lx = (int)(k0 % (uint)zw0), ly = (int)(k0 / (uint)zw0);
    bool flank = false;
    float partners = 0.0f;                                        // neighbours with the same geometry and an EQUAL peak
    for (int fy = -1; fy <= 1; fy++) {
        int y = (int)id.y + fy; if (y < 0 || y >= (int)rows) continue;
        for (int fx = -1; fx <= 1; fx++) {
            int x = (int)id.x + fx; if (x < 0 || x >= (int)cols || (fx == 0 && fy == 0)) continue;
            float np_ = starStatTex.Load(int3(x, y, 0)).r;        // a LARGER peak = this zone is its flank; an EQUAL one = a
            if (!(np_ >= st.r && np_ > 0.0f)) continue;           // flat-topped feature straddling the border: they share a vote
            uint k1 = (uint)(starBgTex.Load(int3(x, y, 0)).y + 0.5f);
            int zw1 = (int)(ZoneEdgeX((uint)x + 1u) - ZoneEdgeX((uint)x)), zh1 = (int)(ZoneEdgeY((uint)y + 1u) - ZoneEdgeY((uint)y));
            int nx = (int)(k1 % (uint)zw1), ny = (int)(k1 / (uint)zw1);   // the neighbour's index in ITS own size
            bool okx = (fx == 1) ? (lx >= zw0 - FALD_STAR_FLANK_PX && nx < FALD_STAR_FLANK_NEAR_PX)
                     : (fx == -1) ? (lx < FALD_STAR_FLANK_PX && nx >= zw1 - FALD_STAR_FLANK_NEAR_PX)
                     : (abs(lx - nx) <= FALD_STAR_FLANK_NEAR_PX);
            bool oky = (fy == 1) ? (ly >= zh0 - FALD_STAR_FLANK_PX && ny < FALD_STAR_FLANK_NEAR_PX)
                     : (fy == -1) ? (ly < FALD_STAR_FLANK_PX && ny >= zh1 - FALD_STAR_FLANK_NEAR_PX)
                     : (abs(ly - ny) <= FALD_STAR_FLANK_NEAR_PX);
            if (okx && oky) { if (np_ > st.r) flank = true; else partners += 1.0f; }
        }
    }
    float wt = flank ? 0.0f : w / (1.0f + partners);              // the zone's weight in the target average
    starWOut[id.xy] = float4(wt, wt * log(max(st.r, 1e-12f)), flank ? 1.0f : 0.0f, st.g);
    starPlan2Out[id.xy] = float4(starBgTex.Load(int3(id.xy, 0)).x, near, st.g, w);
}
)";

// Starfield pass S2: the plan the pixels sample (starfield.zone_plan + balance_image's zone fields).
//   target  = max(exp(mean + targetSigma * std) * targetGain, keepNits), then min capNits (when > 0), then min white;
//             mean / std of ln peak with the weights wt * (E + 1 - d) / (E + 1) over the zones at chebyshev distance
//             d <= E = starEvenReach (a tapered window: a star entering it does not swing the target); the zone's own
//             peak where no weight lies within reach
//   w0_field = a SPECK zone's own w0 = sparse * strength (WITHOUT the protection: that is the near field, applied per
//             pixel); a zone WITHOUT a speck (empty, star-free (grainy) sky, a non-star shape) carries the mean w0 of
//             the speck zones in its 3x3 neighbourhood x its own non-protection 1 - smoothstep(nbLo, nbHi, own solid
//             drive): a window / UI zone carries 0, a dark sky zone the full mean. The carry only serves the border
//             pixels of the neighbouring speck zones (own-zone gate in Balance)
//   ln_t    = ln(target) (ln(white) where the target is 0), ln_pk = ln(peak) of a speck zone (ln_t for a zone without a
//             speck: its sky is no "peak"); plan.z is written 0 (it held the lift, retired 2026-10-26)
// (2026-10-22, work guide core-speed) The window of starWTex a 16 x 16 group of zones reads — the tile + an apron of E
// each side, E <= FALD_STAR_EVEN_REACH_MAX 12: 40 x 40 texels — is staged in groupshared memory by the whole group (a few
// loads per thread) instead of ~300 serial texture loads per thread: the pass was a latency floor of ~38 us at every
// resolution. The sums are the same terms in the same order (dy, then dx, ascending; the lattice's zones only); the
// barrier comes before any thread returns.
inline const char* g_faldStarPlanSource = R"(
RWTexture2D<float4> starPlanOut : register(u0);
groupshared float4 gW[40 * 40];   // starWTex over the tile + apron (row-major, 40 wide); outside the lattice unread

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID, uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    int E = (int)starEvenReach;
    int span = 16 + 2 * E;
    int wx0 = (int)gid.x * 16 - E, wy0 = (int)gid.y * 16 - E;               // the window's origin in zones
    for (uint e = tid.y * 16u + tid.x; e < (uint)(span * span); e += 256u) {
        int ax = (int)(e % (uint)span), ay = (int)(e / (uint)span);
        int x = wx0 + ax, y = wy0 + ay;
        if (x >= 0 && x < (int)cols && y >= 0 && y < (int)rows) gW[ay * 40 + ax] = starWTex.Load(int3(x, y, 0));
    }
    GroupMemoryBarrierWithGroupSync();
    if (id.x >= cols || id.y >= rows) return;
    float4 st = starStatTex.Load(int3(id.xy, 0));
    float peak = st.r;
    float4 sw = gW[((int)tid.y + E) * 40 + (int)tid.x + E];                  // = starWTex.Load(id.xy)
    bool spk = sw.a > 0.5f;
    float lp = log(max(peak, 1e-12f));
    float wsum = 0.0f, wl = 0.0f;
    for (int dy = -E; dy <= E; dy++) {
        int y = (int)id.y + dy; if (y < 0 || y >= (int)rows) continue;
        for (int dx = -E; dx <= E; dx++) {
            int x = (int)id.x + dx; if (x < 0 || x >= (int)cols) continue;
            float4 t = gW[((int)tid.y + E + dy) * 40 + (int)tid.x + E + dx];
            float k = (float)(E + 1 - max(abs(dx), abs(dy))) / (float)(E + 1);   // tapered window: continuous at its rim
            wsum += t.r * k; wl += t.g * k;
        }
    }
    float target = peak;                                          // no weight within reach: the zone's own peak
    if (wsum > 0.0f) {
        float mean = wl / wsum;
        float var = 0.0f;
        if (starTargetSigma > 0.0f) {
            // the SPREAD of ln peak, summed about the mean in a second sweep (sum2 / wsum - mean^2 would lose a uniform
            // field's exact 0 in float32 and nudge every target up); ln peak of a neighbour = t.g / t.r
            for (int vy = -E; vy <= E; vy++) {
                int y2 = (int)id.y + vy; if (y2 < 0 || y2 >= (int)rows) continue;
                for (int vx = -E; vx <= E; vx++) {
                    int x2 = (int)id.x + vx; if (x2 < 0 || x2 >= (int)cols) continue;
                    float4 t2 = gW[((int)tid.y + E + vy) * 40 + (int)tid.x + E + vx];
                    if (!(t2.r > 0.0f)) continue;
                    float k2 = (float)(E + 1 - max(abs(vx), abs(vy))) / (float)(E + 1);
                    float dl = t2.g / t2.r - mean;
                    var += t2.r * k2 * dl * dl;
                }
            }
            var = max(var / wsum, 0.0f);
        }
        target = exp(mean + starTargetSigma * sqrt(var)) * starTargetGain;   // mean + k std: compress the outliers only
        target = max(target, starKeepNits);                       // the absolute floor: below ~100 nits there is no haze to fix
        if (starCapNits > 0.0f) target = min(target, starCapNits);
        target = min(target, white);
    }
    float s3 = 0.0f, n3 = 0.0f;
    for (int ey = -1; ey <= 1; ey++) {
        int y = (int)id.y + ey; if (y < 0 || y >= (int)rows) continue;
        for (int ex = -1; ex <= 1; ex++) {
            int x = (int)id.x + ex; if (x < 0 || x >= (int)cols) continue;
            s3 += starStatTex.Load(int3(x, y, 0)).b; n3 += gW[((int)tid.y + E + ey) * 40 + (int)tid.x + E + ex].a;
        }
    }
    float carry = s3 * starStrength / max(n3, 1.0f) * (1.0f - StarSmooth(starNbLo, starNbHi, st.a));   // sparse > 0 only in speck zones
    float wField = spk ? st.b * starStrength : carry;
    float lnT = log(max((target > 0.0f) ? target : white, 1e-12f));
    starPlanOut[id.xy] = float4(wField, lnT, 0.0f, spk ? lp : lnT);
}
)";

// (Pass 1a, the black-frame LED boost's own dispatch, was folded into the statistic pass on 2026-10-22: see its header.)

// Pass 2: the two backlight fields on the sub-cell grid. out[k] = sum_i d[k - i] * kern[i]
// (scipy fftconvolve 'same', odd kernels, zero outside the lattice), kernel chosen by sub-offset.
// The black-frame LED boost (boostN != 0) multiplies B_true ONLY — the panel's own estimate does not know it
// (FaldModel.backlights). The flat-lattice normalisation fields are built with boostN = 0.
// Thread layout (2026-10-22, work guide core-speed): one thread per (cell, GROUP of FALD_CONV_SO consecutive sub-offsets
// so = g * S .. g * S + S - 1, S = 1 / 2 / 4 / 8). The C++ compiles the pass once per S (g_faldConvSoDefine) and picks
// one per device and lattice (FaldConvVariant): a thread's drive texel feeds S multiply-adds instead of one, and the S
// weights of a tap are CONTIGUOUS — the panel file's [sub][sub][H][W] table is re-laid out as [g][jj][ii][s] with every
// (g, jj) row starting at a multiple of 4 floats (shared/fald_panel.h FaldConvTableLayout), so they are read four at a
// time through the float4 views at t36 / t37 (the same buffers as t2 / t3). Every row starts with one float4 header:
// the first and last tap column (as ints) at which any of the row's S slices is non-zero — the estimate kernel's table
// holds exact zeros outside its hard support (28-86 % of it on the owner's files) and a tap that is exactly +0 on every
// slice adds +0 to every accumulator, which leaves a non-negative sum bit for bit (the C++ writes full rows for a table
// with a negative tap, where a -0 could differ: FaldConvTableLayout). A group is FALD_CONV_THREADS consecutive
// cells (row-major) of ONE sub-offset group, so all its threads read the SAME kernel taps at every step and neighbouring
// drive texels; a linear cell index wastes no lanes on a lattice that is not a multiple of 16. The loops run over the
// taps whose source cell lies on the lattice (the range is clamped once instead of testing every tap).
// Per OUTPUT the multiply-adds are the same taps in the same order as ever — rows, then columns, ascending, one add after
// the other (the pre-2026-09-22 loop; S = 1 = the 2026-09-22 pass with four loads in flight) — so the fields are
// bit-identical for every S (HW + WARP gates); only how the taps are fetched changed.
inline const char* g_faldConvSource = R"(
#ifndef FALD_CONV_SO
#define FALD_CONV_SO 1
#endif
RWTexture2D<float> bTrueOut : register(u0);
RWTexture2D<float> bEstOut  : register(u1);
RWTexture2D<float4> fieldsOut : register(u2);   // the two with their flat response: (B_true, flat, B_est, flat) = t39
Buffer<float4> kTrue4 : register(t36);   // t2 viewed four floats wide (every table row starts at a multiple of 4)
Buffer<float4> kEst4  : register(t37);   // t3 likewise

// acc[s] += the sum over i = i0 .. i1 (ascending) of dmap(cx - i, sy) * k(tap ii = i + RC, s), s = 0 .. S - 1.
// rowHead = the (g, jj) row's first float: its header float4 (lo, hi: the row's non-zero tap columns), then the taps —
// the S weights of tap ii sit at rowHead + 4 + ii * S (a multiple of 4 for S >= 4: the tap's float4 index is (rowHead + 4
// + ii * S) / 4; S = 2: a float4 holds taps ii, ii + 1 for even ii; S = 1: the taps one at a time through t2, four loads
// in flight).
void ConvRow(Texture2D<float> dmap, Buffer<float> kt, Buffer<float4> kt4, int cx, int sy, uint rowHead, int RC, int i0, int i1,
             inout float acc[FALD_CONV_SO]) {
    float4 head = kt4[rowHead >> 2];
    i0 = max(i0, asint(head.x) - RC); i1 = min(i1, asint(head.y) - RC);   // the row's non-zero taps only (header: lo, hi)
    uint rowBase = rowHead + 4u;
    int i = i0;
#if FALD_CONV_SO == 1
    // the 2026-09-22 loop: four taps' loads in flight, the adds one after the other (a float4 fetch with its alignment
    // head / tail measured slower at 1080p on an RTX 5090: short rows)
    uint kb = rowBase + (uint)RC;
    [loop] for (; i + 3 <= i1; i += 4) {
        float d0 = dmap.Load(int3(cx - i, sy, 0)),     d1 = dmap.Load(int3(cx - i - 1, sy, 0));
        float d2 = dmap.Load(int3(cx - i - 2, sy, 0)), d3 = dmap.Load(int3(cx - i - 3, sy, 0));
        float k0 = kt[kb + (uint)i], k1 = kt[kb + (uint)(i + 1)], k2 = kt[kb + (uint)(i + 2)], k3 = kt[kb + (uint)(i + 3)];
        acc[0] += d0 * k0; acc[0] += d1 * k1; acc[0] += d2 * k2; acc[0] += d3 * k3;
    }
    [loop] for (; i <= i1; i++) acc[0] += dmap.Load(int3(cx - i, sy, 0)) * kt[kb + (uint)i];
#elif FALD_CONV_SO == 2
    if (i <= i1 && (((uint)(i + RC)) & 1u) != 0u) {                                // an odd first tap: its float4's second pair
        float d = dmap.Load(int3(cx - i, sy, 0));
        float4 k = kt4[(rowBase + 2u * (uint)(i + RC)) >> 2];
        acc[0] += d * k.z; acc[1] += d * k.w;
        i++;
    }
    [loop] for (; i + 1 <= i1; i += 2) {
        float d0 = dmap.Load(int3(cx - i, sy, 0)), d1 = dmap.Load(int3(cx - i - 1, sy, 0));
        float4 k = kt4[(rowBase + 2u * (uint)(i + RC)) >> 2];
        acc[0] += d0 * k.x; acc[1] += d0 * k.y; acc[0] += d1 * k.z; acc[1] += d1 * k.w;
    }
    if (i <= i1) {
        float d = dmap.Load(int3(cx - i, sy, 0));
        float4 k = kt4[(rowBase + 2u * (uint)(i + RC)) >> 2];
        acc[0] += d * k.x; acc[1] += d * k.y;
    }
#elif FALD_CONV_SO == 4
    [loop] for (; i + 1 <= i1; i += 2) {
        float d0 = dmap.Load(int3(cx - i, sy, 0)), d1 = dmap.Load(int3(cx - i - 1, sy, 0));
        uint q = (rowBase + 4u * (uint)(i + RC)) >> 2;
        float4 ka = kt4[q], kb = kt4[q + 1u];
        acc[0] += d0 * ka.x; acc[1] += d0 * ka.y; acc[2] += d0 * ka.z; acc[3] += d0 * ka.w;
        acc[0] += d1 * kb.x; acc[1] += d1 * kb.y; acc[2] += d1 * kb.z; acc[3] += d1 * kb.w;
    }
    if (i <= i1) {
        float d = dmap.Load(int3(cx - i, sy, 0));
        float4 k = kt4[(rowBase + 4u * (uint)(i + RC)) >> 2];
        acc[0] += d * k.x; acc[1] += d * k.y; acc[2] += d * k.z; acc[3] += d * k.w;
    }
#else
    [loop] for (; i <= i1; i++) {
        float d = dmap.Load(int3(cx - i, sy, 0));
        uint q = (rowBase + 8u * (uint)(i + RC)) >> 2;
        float4 ka = kt4[q], kb = kt4[q + 1u];
        acc[0] += d * ka.x; acc[1] += d * ka.y; acc[2] += d * ka.z; acc[3] += d * ka.w;
        acc[4] += d * kb.x; acc[5] += d * kb.y; acc[6] += d * kb.z; acc[7] += d * kb.w;
    }
#endif
}

[numthreads(64, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint cell = gid.x * 64u + tid.x;
    if (cell >= cols * rows) return;
    uint g = gid.y;                                     // the sub-offset group: so = g * FALD_CONV_SO + s
    int cx = (int)(cell % cols), cy = (int)(cell / cols);

    // taps whose source cell sx = cx - i, sy = cy - j lies on the lattice (zero outside it: those terms are absent)
    int RC = (int)reachTrueC, RR = (int)reachTrueR;
    uint Wt = 2 * reachTrueC + 1, Ht = 2 * reachTrueR + 1;
    uint strideT = 4u + ((Wt * FALD_CONV_SO + 3u) & ~3u);   // = FaldConvRowStride(Wt, S): the header + the padded taps
    int i0 = max(-RC, cx - (int)cols + 1), i1 = min(RC, cx);
    int j0 = max(-RR, cy - (int)rows + 1), j1 = min(RR, cy);
    float accT[FALD_CONV_SO], accE[FALD_CONV_SO];
    [unroll] for (uint s0 = 0; s0 < FALD_CONV_SO; s0++) { accT[s0] = 0.0f; accE[s0] = 0.0f; }
    [loop] for (int j = j0; j <= j1; j++)
        ConvRow(driveTex, kTrue, kTrue4, cx, cy - j, (g * Ht + (uint)(j + RR)) * strideT, RC, i0, i1, accT);
    int EC = (int)reachEstC, ER = (int)reachEstR;
    uint We = 2 * reachEstC + 1, He = 2 * reachEstR + 1;
    uint strideE = 4u + ((We * FALD_CONV_SO + 3u) & ~3u);
    int e0 = max(-EC, cx - (int)cols + 1), e1 = min(EC, cx);
    int f0 = max(-ER, cy - (int)rows + 1), f1 = min(ER, cy);
    [loop] for (int j2 = f0; j2 <= f1; j2++)
        ConvRow(driveEstTex, kEst, kEst4, cx, cy - j2, (g * He + (uint)(j2 + ER)) * strideE, EC, e0, e1, accE);
    if (boostN != 0u) {
        float b = boostTex.Load(int3(0, 0, 0));
        [unroll] for (uint s1 = 0; s1 < FALD_CONV_SO; s1++) accT[s1] *= b;
    }
    [unroll] for (uint s2 = 0; s2 < FALD_CONV_SO; s2++) {
        uint so = g * FALD_CONV_SO + s2;
        if (so < sub * sub) {                           // (the last group of a sub * sub not divisible by S)
            uint fx = (uint)cx * sub + so % sub, fy = (uint)cy * sub + so / sub;
            bTrueOut[uint2(fx, fy)] = accT[s2];
            bEstOut[uint2(fx, fy)] = accE[s2];
            fieldsOut[uint2(fx, fy)] = float4(accT[s2], flatTrueTex.Load(int3(fx, fy, 0)), accE[s2], flatEstTex.Load(int3(fx, fy, 0)));
        }
    }
}
)";

// The conv pass's group size: the literal in its [numthreads(64, 1, 1)] and `gid.x * 64u` above (DLC
// tests/test_fald_shader_layout.py pins that all three agree). Both RunConv sites (src/fald.cpp, dwm_hook/hook_fald.cpp)
// dispatch FaldConvGroupsX(cols, rows) x FaldConvGroups(sub, S) groups; the loader's limits (cols, rows <= 512, sub <= 16)
// keep that inside D3D11's 65535 groups per dimension (<= 4096 x 256).
static const unsigned int FALD_CONV_THREADS = 64u;
inline unsigned int FaldConvGroupsX(unsigned int cols, unsigned int rows) {
    return (cols * rows + FALD_CONV_THREADS - 1u) / FALD_CONV_THREADS;
}
// The sub-offsets-per-thread variants of the conv pass (FALD_CONV_SO above): index v = 0..3 <-> S = 1, 2, 4, 8. Each
// host compiles the pass once per variant with g_faldConvSoDefine[v] in front of the common source, and runs the one
// FaldConvVariant picks for the device and lattice (the kernel tables re-laid out for that S: fald_panel.h).
static const unsigned int FALD_CONV_SO_VARIANTS = 4u;
// The packed fields texture's SRV slot (fieldsTex above): bound per round next to t5 / t6 by the hosts (round 0's for the
// round-1 sweep and round 0's gain / reach, round 1's for the pixel pass and round 1's gain / reach).
static const unsigned int FALD_FIELDS_SLOT = 39u;
inline unsigned int FaldConvSoOf(unsigned int variant) { return 1u << (variant < FALD_CONV_SO_VARIANTS ? variant : 0u); }
inline const char* const g_faldConvSoDefine[FALD_CONV_SO_VARIANTS] = {
    "#define FALD_CONV_SO 1\n", "#define FALD_CONV_SO 2\n", "#define FALD_CONV_SO 4\n", "#define FALD_CONV_SO 8\n" };

// Zone sweeps (work guide C14; the rules above ZoneSlices in the common source). The statistic pass and starfield S0
// dispatch cols x rows x FaldZoneSlices(cellW, cellH) groups (cellW x cellH = the WIDEST zone); with more than one slice
// per zone the pass's combine variant (its source compiled after g_faldZoneCombineDefine) follows over cols x rows. The
// partials: one structured buffer per monitor of cols * rows * slices ZonePart records, bound at u2. A 1-slice lattice has no buffer
// and runs neither the partial write nor the combine. A lattice needing more than FALD_ZONE_SLICES_MAX (D3D11's 65535
// groups per dimension) is refused at build; real frames stay far below (a 7680 x 4320 frame as one zone = 8100).
static const unsigned int FALD_ZONE_SLICE_PX = 4096u;     // = the HLSL's FALD_ZONE_SLICE_PX
static const unsigned int FALD_ZONE_PART_BYTES = 32u;     // = the HLSL's ZonePart (float4 + uint4)
static const unsigned int FALD_ZONE_SLICES_MAX = 65535u;  // D3D11_CS_DISPATCH_MAX_THREAD_GROUPS_PER_DIMENSION
inline const char* g_faldZoneCombineDefine = "#define FALD_ZONE_COMBINE 1\n";
inline unsigned int FaldZoneSlices(unsigned int cellW, unsigned int cellH) {
    return (cellW * cellH + FALD_ZONE_SLICE_PX - 1u) / FALD_ZONE_SLICE_PX;
}

// Pass 1b (tempMode != 0 only): per-cell first-order drive state. driveTex (t4) = this round's instantaneous
// drive, stateTex (t11) = the state committed after the previous frame's round 1. out = s + a * (d - s) with
// a = tempAlphaRise when the drive rises, tempAlphaFall when it falls (DLC temporal.DriveState.peek); with no
// valid state yet (tempInit) the drive is copied. Both rounds of a frame read the SAME committed state; the
// C++ copies round 1's output into the state texture afterwards (DriveState.commit).
inline const char* g_faldTemporalSource = R"(
RWTexture2D<float> driveFiltOut : register(u0);
RWStructuredBuffer<uint> changeFlags : register(u5);    // the partial path: a filtered drive that moved flags this round

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float d = driveTex.Load(int3(id.xy, 0));
    float o;
    if (tempInit != 0u) o = d;
    else {
        float s = stateTex.Load(int3(id.xy, 0));
        float a = (d > s) ? tempAlphaRise : tempAlphaFall;
        o = s + a * (d - s);
    }
    if (RunModePartial() == 1u && driveFiltOut[id.xy] != o) InterlockedOr(changeFlags[roundIdx], 1u);
    driveFiltOut[id.xy] = o;
}
)";

// Pass 1c (temporal mode 3 "panel clock" only; rules in fald.h above FALD_TEMPORAL_PANEL, reference DLC
// dlc/fald/paneltime.py, GPU-order twin gpuemu.GpuPanelDriveState.advance). Runs ONCE per frame, before round 0, and
// only with a valid state: driveTex (t4) = the PREVIOUS processed frame's round-1 instantaneous drives = the target of
// every dimming-engine tick since that frame was first shown; clkState0 / clkState1 = the LED states of the two parity
// clocks, advanced in place to this frame's first refresh. Out: the drive maps the real-spread kernel (u0) and the
// estimate kernel (u1: the LED state one refresh earlier) see in BOTH rounds — the clocks' weighted mean. Nothing here
// depends on this frame's content; the C++ stores this frame's round-1 drives as the next target afterwards.
// Unknown parity (both weights > 0; shared/fald_temporal.h above FALD_CLOCK_ROBUST_CAP): B_est's map is the mean of the
// two compensation states e_p, B_true's that mean plus the mean of the clocks' TRANSIENTS t_p - e_p, clamped to
// +-FALD_CLOCK_ROBUST_CAP x the mean e — on a cut the two clocks want their step in different refreshes, and the half a
// plain mean applies where the panel needs none must not dim the frame by more than that (DLC
// paneltime.robust_transient, gpuemu.clock_robust_true32).
// Compiled a second time with g_faldClockMeanDefine: the plain mean of both maps (the pre-2026-10-23 pass, parity
// FALD_CLOCK_PARITY_MEAN = the blind A/B's other arm); known parity runs the same arithmetic in both variants.
inline const char* g_faldClockMeanDefine = "#define FALD_CLOCK_MEAN 1\n";
inline const char* g_faldPanelClockSource = R"(
RWTexture2D<float> clkTrueOut : register(u0);
RWTexture2D<float> clkEstOut  : register(u1);
RWTexture2D<float> clkState0  : register(u2);
RWTexture2D<float> clkState1  : register(u3);
RWStructuredBuffer<uint> changeFlags : register(u5);    // the partial path: a map that moved flags round 0 (both rounds read them)

static const float FALD_CLOCK_ROBUST_CAP = 0.1f;        // shared/fald_temporal.h (DLC tests pin the three equal)

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float d = driveTex.Load(int3(id.xy, 0));
    float s0 = clkState0[id.xy], s1 = clkState1[id.xy];
    float g0 = d - s0, g1 = d - s1;
    float t0 = s0 + clkTrue0 * g0, t1 = s1 + clkTrue1 * g1;
    float e0 = s0 + clkEst0 * g0, e1 = s1 + clkEst1 * g1;
    float oEst = clkW0 * e0 + clkW1 * e1;
#ifdef FALD_CLOCK_MEAN
    float oTrue = clkW0 * t0 + clkW1 * t1;
#else
    float tr = clkW0 * (t0 - e0) + clkW1 * (t1 - e1), lim = FALD_CLOCK_ROBUST_CAP * oEst;
    float oTrue = (clkW0 > 0.0f && clkW1 > 0.0f) ? oEst + min(max(tr, -lim), lim) : clkW0 * t0 + clkW1 * t1;
#endif
    if (RunModePartial() == 1u && (clkTrueOut[id.xy] != oTrue || clkEstOut[id.xy] != oEst)) InterlockedOr(changeFlags[0], 1u);
    clkTrueOut[id.xy] = oTrue;
    clkEstOut[id.xy] = oEst;
    clkState0[id.xy] = t0; clkState1[id.xy] = t1;
}
)";

// Pass 2b: gain on the fine grid (flat-normalised fields, clamp, deep-dark fade) and the flat-normalised B_est of the
// knee's ceiling (C15) -> u0 (R32G32F).
inline const char* g_faldGainSource = R"(
RWTexture2D<float2> gainOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    uint fx = id.x, fy = id.y;
    if (fx >= cols * sub || fy >= rows * sub) return;
    float4 f = fieldsTex.Load(int3(fx, fy, 0));                        // (B_true, flat, B_est, flat): one load
    float bT = f.x / max(f.y, 1e-6f);
    float bE = f.z / max(f.w, 1e-6f);
    gainOut[uint2(fx, fy)] = float2(RawGain(bT, bE), bE);
}
)";

// Pass 2c: separable Gaussian blur of BOTH channels (gain, ceiling B_est; t9 -> u0), sigma = gainSmoothFine fine
// samples, radius 3 sigma, clamped at the grid edge. Run twice (blurDir 0 then 1). With gainSmoothFine == 0 it copies.
inline const char* g_faldBlurSource = R"(
RWTexture2D<float2> blurOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    int fx = (int)id.x, fy = (int)id.y;
    int W = (int)(cols * sub), H = (int)(rows * sub);
    if (fx >= W || fy >= H) return;
    if (gainSmoothFine <= 0.0f) { blurOut[uint2(fx, fy)] = gainTex.Load(int3(fx, fy, 0)); return; }
    int R = (int)ceil(3.0f * gainSmoothFine);
    float2 acc = float2(0.0f, 0.0f);
    float wsum = 0.0f;
    for (int k = -R; k <= R; k++) {
        int x = fx, y = fy;
        if (blurDir == 0) x = clamp(fx + k, 0, W - 1); else y = clamp(fy + k, 0, H - 1);
        float w = exp(-0.5f * (float)(k * k) / (gainSmoothFine * gainSmoothFine));
        acc += w * gainTex.Load(int3(x, y, 0)); wsum += w;
    }
    blurOut[uint2(fx, fy)] = acc / wsum;
}
)";

// The decide pass (2026-10-22, work guide core-speed; shared/fald_change.h): one thread after each round's statistic sweep
// (roundIdx 0 / 1) and, with the starfield on, after S0 (roundIdx 2). It finishes the round's black-frame boost from the
// persistent count (the 2 x 1 texture the conv reads), and on the PARTIAL path (runMode bits 0-7 = 1) writes the indirect
// arguments of everything downstream from the change flags: a moved star statistic re-forms the plan (S1, S2) and
// re-sweeps round 0 in full; a moved drive / flag / LED-lag map of round 0 runs the convolution, the gain, the round-1
// sweep in full, and so on; nothing moved runs the round-1 sweep on the touched zones only, and the pixel pass over the
// rects alone (the hosts draw that one; the full-frame pixel pass is the indirect draw here). The flags are cleared by
// the last decide of the run. On the full path only the boost is written (the hosts dispatch everything directly).
// roundIdx 3 = BEFORE any sweep, when the caller's pass may have changed the whole frame (changeFlags[3], the dynamic
// tone map's peak: shared/fald_change.h): it orders S0 and round 0's sweep over the whole lattice (the touched-zone
// sweeps still run first: harmless, their values are rewritten), and every later decide takes that flag as "moved".
// Argument slots (bytes; 3 uints each, the pixel pass 4): shared/fald_change.h FALD_ARGS_*.
inline const char* g_faldDecideSource = R"(
RWStructuredBuffer<uint> boostCount : register(u3);
RWTexture2D<float> boostOut : register(u4);              // this round's 2 x 1 (boost, count)
RWStructuredBuffer<uint> changeFlags : register(u5);
RWByteAddressBuffer args : register(u6);                 // the indirect arguments
static const uint ARGS_STAR_S1 = 0, ARGS_STAR_S2 = 16, ARGS_STAT0_FULL = 32, ARGS_STAT0_FULL_COMBINE = 48, ARGS_STAT0_PART = 64,
                  ARGS_STAT0_PART_COMBINE = 80, ARGS_CONV0 = 96, ARGS_GAIN0 = 112, ARGS_STAT1_FULL = 128, ARGS_STAT1_FULL_COMBINE = 144,
                  ARGS_STAT1_PART = 160, ARGS_STAT1_PART_COMBINE = 176, ARGS_CONV1 = 192, ARGS_GAIN1 = 208, ARGS_PIXEL = 224,
                  ARGS_STAR_S0_FULL = 240, ARGS_STAR_S0_FULL_COMBINE = 256,
                  ARGS_CORE_LATTICE = 272, ARGS_CORE_FINE0 = 288, ARGS_CORE_FINE1 = 304;   // the always-on core (fald_core2.h)

[numthreads(1, 1, 1)]
void main() {
    uint mode = runMode & 0xFFu, so = (runMode >> 8) & 0xFFu, nList = runMode >> 16;
    if (boostN != 0u && roundIdx < 2u) {                                 // this round's boost from the persistent count
        uint count = boostCount[roundIdx];
        float b = 1.0f;
        [loop] for (uint i = 0; i < boostN; i++) {
            if ((float)count < boostLut[2 * i]) break;
            b = boostLut[2 * i + 1];
        }
        boostOut[uint2(0, 0)] = b;
        boostOut[uint2(1, 0)] = (float)count;
    }
    if (mode != 1u) return;
    uint flagS = changeFlags[2], flag0 = changeFlags[0], flag1 = changeFlags[1], flagF = changeFlags[3];
    uint3 none = uint3(0u, 0u, 0u);
    uint3 zonesFull = uint3(cols, rows, ZoneSlices()), zonesFullC = uint3(cols, rows, 1u);
    uint3 zonesPart = uint3(nList, 1u, ZoneSlices()), zonesPartC = uint3(nList, 1u, 1u);
    uint3 lattice = uint3((cols + 15u) / 16u, (rows + 15u) / 16u, 1u);
    uint3 conv = uint3((cols * rows + 63u) / 64u, (sub * sub + so - 1u) / so, 1u);
    uint3 fine = uint3((cols * sub + 15u) / 16u, (rows * sub + 15u) / 16u, 1u);
    if (roundIdx == 3u) {                                                // before any sweep: the frame changed everywhere?
        bool all = flagF != 0u;
        args.Store3(ARGS_STAR_S0_FULL, all ? zonesFull : none);
        args.Store3(ARGS_STAR_S0_FULL_COMBINE, all ? zonesFullC : none);
        args.Store3(ARGS_STAT0_FULL, all ? zonesFull : none);
        args.Store3(ARGS_STAT0_FULL_COMBINE, all ? zonesFullC : none);
        args.Store3(ARGS_STAT0_PART, all ? none : zonesPart);
        args.Store3(ARGS_STAT0_PART_COMBINE, all ? none : zonesPartC);
        return;
    }
    if (roundIdx == 2u) {                                                // after S0: the plan passes + round 0's sweep
        bool star = (flagS | flagF) != 0u;
        args.Store3(ARGS_STAR_S1, star ? lattice : none);
        args.Store3(ARGS_STAR_S2, star ? lattice : none);
        args.Store3(ARGS_STAT0_FULL, star ? zonesFull : none);
        args.Store3(ARGS_STAT0_FULL_COMBINE, star ? zonesFullC : none);
        args.Store3(ARGS_STAT0_PART, star ? none : zonesPart);
        args.Store3(ARGS_STAT0_PART_COMBINE, star ? none : zonesPartC);
        return;
    }
    // flag0's bit 1 (2u) = a zone's CORE statistics / node peaks moved (fald_core2.h, round 3): the correction changes with
    // no drive moving — the core's lattice passes, the round-1 sweep in full and the pixel pass run; the convolution and
    // the gain (pure functions of the drive maps) only when a drive / zone flag / LED-lag map did (the other bits). With
    // the core off bit 1 is never set and drv == any: the arguments of 2026-10-22.
    uint drv0 = flagS | (flag0 & ~2u) | flagF, any0 = drv0 | (flag0 & 2u);
    if (roundIdx == 0u) {                                                // after round 0's sweep (+ LED lag)
        args.Store3(ARGS_CONV0, drv0 != 0u ? conv : none);
        args.Store3(ARGS_GAIN0, drv0 != 0u ? fine : none);
        args.Store3(ARGS_CORE_LATTICE, any0 != 0u ? lattice : none);     // the core's Z1 / Z2 (stateless: from this frame's stats)
        args.Store3(ARGS_CORE_FINE0, any0 != 0u ? fine : none);          // ... and its fine passes of round 0 (the gain or the stats moved)
        args.Store3(ARGS_STAT1_FULL, any0 != 0u ? zonesFull : none);
        args.Store3(ARGS_STAT1_FULL_COMBINE, any0 != 0u ? zonesFullC : none);
        args.Store3(ARGS_STAT1_PART, any0 != 0u ? none : zonesPart);
        args.Store3(ARGS_STAT1_PART_COMBINE, any0 != 0u ? none : zonesPartC);
        return;
    }
    uint drv1 = drv0 | flag1, any1 = any0 | flag1;                       // after round 1's sweep (+ LED lag)
    args.Store3(ARGS_CONV1, drv1 != 0u ? conv : none);
    args.Store3(ARGS_GAIN1, drv1 != 0u ? fine : none);
    args.Store3(ARGS_CORE_FINE1, any1 != 0u ? fine : none);              // the core's fine passes of round 1
    args.Store4(ARGS_PIXEL, uint4(3u, any1 != 0u ? 1u : 0u, 0u, 0u));   // DrawInstancedIndirect: the fullscreen triangle or nothing
    changeFlags[0] = 0u; changeFlags[1] = 0u; changeFlags[2] = 0u; changeFlags[3] = 0u;
}
)";

// Passes 2b + 2c in ONE dispatch (2026-10-22, work guide core-speed): (gain, ceiling B_est) on the fine grid and its
// separable Gaussian low-pass, bit for bit the three dispatches + copy above. Per fine texel the same gain from the same
// four loads, the same horizontal sum (k = -R .. R, the weight the same expression, acc and wsum in the same order) of
// the same clamped neighbours, then the same vertical sum of those sums — staged through groupshared memory instead of
// the two ping-pong textures, so the per-texel arithmetic is unchanged. A 16 x 16 tile of outputs with an apron of R
// each side; the apron's texels beyond the grid hold the gain of the clamped coordinate, exactly what the passes'
// clamp(x + k, 0, W - 1) read (a replicated edge row's horizontal sum is the edge row's). R = ceil(3 sigma) <=
// FALD_GAIN_BLUR_RMAX only (the groupshared limit); a larger sigma runs the three passes (the hosts decide at build:
// FaldGainBlurFused). gainSmoothFine <= 0: the gain itself (the blur passes copied it).
static const int FALD_GAIN_BLUR_RMAX = 20;                                     // 56 x 56 + 56 x 16 float2 = 32256 bytes
inline const char* g_faldGainBlurSource = R"(
RWTexture2D<float2> gainBlurOut : register(u0);
groupshared float2 gG[56 * 56];   // the gain over the tile + apron (row-major, 56 wide)
groupshared float2 gH[56 * 16];   // its horizontal blur on the tile's 16 columns, apron rows included

[numthreads(16, 16, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    int W = (int)(cols * sub), H = (int)(rows * sub);
    int R = (gainSmoothFine > 0.0f) ? (int)ceil(3.0f * gainSmoothFine) : 0;
    int span = 16 + 2 * R;
    int x0 = (int)gid.x * 16 - R, y0 = (int)gid.y * 16 - R;              // the apron's origin on the fine grid
    uint t = tid.y * 16u + tid.x;
    // the gain of every apron texel, at the clamped coordinate (= the blur passes' clamped loads of pass 2b's texture)
    for (uint e = t; e < (uint)(span * span); e += 256u) {
        int ax = (int)(e % (uint)span), ay = (int)(e / (uint)span);
        int fx = clamp(x0 + ax, 0, W - 1), fy = clamp(y0 + ay, 0, H - 1);
        float4 f = fieldsTex.Load(int3(fx, fy, 0));                    // (B_true, flat, B_est, flat): one load
        float bT = f.x / max(f.y, 1e-6f);
        float bE = f.z / max(f.w, 1e-6f);
        gG[ay * 56 + ax] = float2(RawGain(bT, bE), bE);
    }
    GroupMemoryBarrierWithGroupSync();
    if (gainSmoothFine <= 0.0f) {                                            // no low-pass: the gain as it is
        int ox0 = (int)gid.x * 16 + (int)tid.x, oy0 = (int)gid.y * 16 + (int)tid.y;
        if (ox0 < W && oy0 < H) gainBlurOut[uint2(ox0, oy0)] = gG[(tid.y + R) * 56 + tid.x + R];
        return;
    }
    // horizontal: the tile's 16 columns on every apron row (the vertical sum reads them), the passes' k order and weights
    for (uint e2 = t; e2 < (uint)(span * 16); e2 += 256u) {
        int cx = (int)(e2 % 16u), ry = (int)(e2 / 16u);
        float2 acc = float2(0.0f, 0.0f);
        float wsum = 0.0f;
        for (int k = -R; k <= R; k++) {
            float w = exp(-0.5f * (float)(k * k) / (gainSmoothFine * gainSmoothFine));
            acc += w * gG[ry * 56 + cx + R + k]; wsum += w;
        }
        gH[ry * 16 + cx] = acc / wsum;
    }
    GroupMemoryBarrierWithGroupSync();
    // vertical, on the tile's own texels
    int ox = (int)gid.x * 16 + (int)tid.x, oy = (int)gid.y * 16 + (int)tid.y;
    if (ox >= W || oy >= H) return;
    float2 acc2 = float2(0.0f, 0.0f);
    float wsum2 = 0.0f;
    for (int k2 = -R; k2 <= R; k2++) {
        float w2 = exp(-0.5f * (float)(k2 * k2) / (gainSmoothFine * gainSmoothFine));
        acc2 += w2 * gH[((int)tid.y + R + k2) * 16 + (int)tid.x]; wsum2 += w2;
    }
    gainBlurOut[uint2(ox, oy)] = acc2 / wsum2;
}
)";
// Both hosts run the fused pass when the file's sigma allows it (the same float32 arithmetic as the shader's R).
inline bool FaldGainBlurFused(float gainSmoothCells, unsigned int sub) {
    const float sigma = gainSmoothCells * (float)sub;
    const int R = sigma > 0.0f ? (int)ceilf(3.0f * sigma) : 0;
    return R <= FALD_GAIN_BLUR_RMAX;
}

// Pass 3: per-pixel correction of the ORIGINAL processed frame with the final fields.
// debugMode 1 = visualise gain-1 (white = no change, red = brighten, blue = darken, +-25 % full scale), 2 = B_true, 3 = B_est,
// 4 = identity passthrough (the layer runs its passes but outputs the source: isolates the overlay path itself),
// 5 = the pedestal term actually applied (|adj| * fade, per channel, x100: 1 nit of subtraction shows as 100 nits;
//     the colour is the colour of what is subtracted or lifted), 6 = the influence of the per-channel toggle:
//     |adj_channel - adj_white| * fade, x100 (what changes on screen when the toggle flips; black = nothing),
//     7 = temporal settling: per cell, the instantaneous drive minus the filtered one (red = the state is still BELOW
//     the frame's drive, i.e. the LEDs are modelled as still rising; blue = above, falling), +-25 % drive full scale.
//     In temporal mode 3 (panel clock) the "filtered" map is the clocks' mean LED state of this frame.
//     8 = the black-frame boost's zone map of the corrected frame (round 1): white = the zone counts as non-black,
//     black = it does not; passthrough when the panel file has no boost LUT.
//     9 = starfield balancing at a glance, per ZONE (single pixels are invisible): grey level = the zone's weight
//     w0_field x its protection (a zone WITHOUT a speck shows the weight it carries for its neighbours' border
//     pixels — its own pixels are never acted on), tinted blue by how far the zone's peak is pulled down (ln ratio,
//     2 stops = full tint); passthrough when the option is off. View 4 stays a pure
//     passthrough of the SOURCE frame.
//     (10 was the glow fill's view; the fill was removed 2026-10-07.)
// The vertex shader of the pixel pass: a fullscreen triangle with no vertex buffer and no input
// layout (Draw(3, 0) on a triangle list). The overlay path binds its own identical g_vsSource
// (src/shader.h) before calling FaldRunPasses, because its main pass uses the same one; the DWM
// hook cannot — its LUT vertex shader reads a POSITION/TEXCOORD vertex buffer — so it compiles
// this. Its output signature IS PS_INPUT below; keep the two in step.
inline const char* g_faldFullscreenVsSource = R"(
struct VS_OUTPUT {
    float4 pos : SV_POSITION;
    float2 uv : TEXCOORD0;
};

VS_OUTPUT main(uint id : SV_VertexID) {
    VS_OUTPUT o;
    o.uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}
)";

inline const char* g_faldPixelSource = R"(
struct PS_INPUT { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };

float4 main(PS_INPUT i) : SV_Target {
    int2 px = int2(i.pos.xy);
    float4 src = frameTex.Load(int3(px, 0));
    if (!InLattice(px) || debugMode == 4) return src;
    float3 img = PanelNits(src.rgb);
    if (starOn != 0u) img = Balance(img, px);           // the frame the layer works on (S1); the fields below come from it
    float bT, bE; SampleFields(float2(px), bT, bE);
    float2 gain = gainTex.SampleLevel(linearClamp, FineUV(float2(px)), 0);   // .x gain, .y the ceiling's B_est (C15)
    if (debugMode == 1) {
        // diverging map, the DLC analysis convention: white = no change, red = brighten, blue = darken,
        // +-25 % full scale (saturated red/blue), on a 100-nit white
        float t = saturate(abs(gain.x - 1.0f) * 4.0f);
        float3 c = (gain.x >= 1.0f) ? float3(1.0f, 1.0f - t, 1.0f - t) : float3(1.0f - t, 1.0f - t, 1.0f);
        return float4(c * DebugWhite(), 1.0f);
    }
    if (debugMode == 2) { float v = saturate(bT) * DebugWhite(); return float4(v, v, v, 1.0f); }
    if (debugMode == 3) { float v = saturate(bE) * DebugWhite(); return float4(v, v, v, 1.0f); }
    if (debugMode == 7) {
        uint2 c = ZoneOfPixel(px);                          // inside the lattice here
        float dInst = driveTex.Load(int3((int2)c, 0));
        float dFilt = driveEstTex.Load(int3((int2)c, 0));
        float diff = dInst - dFilt;                         // > 0: instantaneous drive above the state (rising)
        float t = saturate(abs(diff) * 4.0f);
        float3 cc = (diff >= 0.0f) ? float3(1.0f, 1.0f - t, 1.0f - t) : float3(1.0f - t, 1.0f - t, 1.0f);
        return float4(cc * DebugWhite(), 1.0f);
    }
    if (debugMode == 8) {
        if (boostN == 0u) return src;
        uint2 c8 = ZoneOfPixel(px);
        float v8 = (activeTex.Load(int3((int2)c8, 0)) > 0.5f) ? DebugWhite() : 0.0f;
        return float4(v8, v8, v8, 1.0f);
    }
    if (debugMode == 9) {
        if (starOn == 0u) return src;
        uint2 c9 = ZoneOfPixel(px);
        float4 zp = starPlanTex.Load(int3((int2)c9, 0));              // w0_field, ln target, 0, ln peak
        float wz = zp.x * (1.0f - StarSmooth(starNbLo, starNbHi, starPlan2Tex.Load(int3((int2)c9, 0)).y));   // x the protection
        float down = wz * starEven * min(zp.y - zp.w, 0.0f);          // ln(new peak / peak) of a zone above its target
        float tb = saturate(-down / 1.3862944f);                      // 2 stops = ln 4
        float3 col9 = wz * float3(1.0f - tb, 1.0f - tb, 1.0f);
        return float4(col9 * DebugWhite(), 1.0f);
    }
    if (debugMode == 5 || debugMode == 6) {
        float maxc = max(img.r, max(img.g, img.b));
        float s = min(maxc, white);
        float3 t1 = PedestalTerm(img, s, max(bT, 0.0f), bE, maxc, 1u);   // per-channel, as applied (gain + fades)
        float3 t0 = PedestalTerm(img, s, max(bT, 0.0f), bE, maxc, 0u);   // white
        float3 shown = (debugMode == 5) ? abs((pedMode == 1) ? t1 : t0) : abs(t1 - t0);
        return float4(PanelNitsToScRGB(min(shown * 100.0f, white)), 1.0f);   // x100 nits per nit
    }
    float3 req = FadePost(Correct(img, bT, bE, CoreGain(gain, float2(px))), img, px);   // fade stack: round 1's reach (t22); core round 2 (fald_core2.h)
    return float4(PanelNitsToScRGB(req), src.a);
}
)";
