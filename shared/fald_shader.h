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
//   CS boost (opt.)   : non-black zone count of the frame -> step LUT -> the frame's LED boost (2 x 1 texture)
//   CS temporal (opt.): per cell, first-order drive STATE (rise/fall time constants) -> the drive the kernels see
//   CS conv           : drives (x) K_true [x boost], drives (x) K_est on the sub-cell grid (cols*sub x rows*sub)
//   CS stat  (round 1): same statistic (and zone flags) on the CORRECTED frame (the correction moves the drives, and
//                       the panel counts the zones of the frame it RECEIVES)
//   CS boost (opt.)   : the corrected frame's boost
//   CS temporal (opt.): again from the committed state; the result is committed after this round
//   CS conv           : final backlight fields
//   CS glow G0-G3 (opt., glow fill, work guide S2; rules in the header above g_faldGlowZoneSource): after EACH round's
//                       conv pass — zone pedestal -> box maximum -> box minimum (= the closing) -> blur + deficit; the
//                       round-1 statistic and the pixel pass add GlowAdd(...) to the corrected request; off = none of it
//   PS                : per pixel: req = (img + ped_ref - ped) * one scale (gain; soft knee toward the ceiling) [+ fill]
#pragma once

inline const char* g_faldCommonSource = R"(
cbuffer FaldCB : register(b0) {
    uint frameW; uint frameH; uint cols; uint rows;
    uint sub; uint cellW; uint cellH; uint roundIdx;
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
    float starEven; float starLift; float starTargetGain; float starCapNits; // starfield balancing (DLC dlc/fald/starfield.py
                                                                    // StarfieldParams; read only when starOn != 0): pull of a
                                                                    // peak above the local target (fraction of the way, log
                                                                    // domain), lift of one below it, gain on the local target
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
    uint boostRule; float boostMeanGamma; float boostMeanThresh; uint glowOn; // the boost count's zone rule (work guide C12b; read
                                                                    // only when boostN != 0): 0 = LIT-or-DIM as above, 1 =
                                                                    // LIT-or-MEAN: LIT OR the zone mean of (brightest channel,
                                                                    // as-if-white nits)^boostMeanGamma >= boostMeanThresh;
                                                                    // glowOn: 1 = glow fill (GlowAdd after Correct, deficit
                                                                    // in t23), 0 = the layer without it
    float glowStrength; float glowCapNits; uint glowReach; float glowReqCeil; // glow fill (DLC dlc/fald/glowfill.py
                                                                    // GlowFillParams; read only when glowOn != 0 / by the
                                                                    // glow passes): share of the zone deficit that is filled,
                                                                    // the fill's ceiling (as-if-white nits), the closing's
                                                                    // box reach in zones (1..4), and the level a filled
                                                                    // pixel's brightest channel never exceeds (C++
                                                                    // FaldGlowReqCeil: below the drive floor / the LIT level)
    uint glowBand; uint _padG1; uint _padG2; uint _padG3;           // glowBand: 1 = the count-threshold band is on (panel file
                                                                    // with a boost LUT AND the mean zone rule): GlowAdd scales
                                                                    // the want of a pixel by its OWN zone's k (t24)
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
Texture2D<float>  gainTex     : register(t9); // smoothed gain on the fine grid (pass 2b)
Texture2D<float>  driveEstTex : register(t10); // conv: the drive map the ESTIMATE kernel sees (= driveTex unless tempMode 2 / 3);
                                               // pixel pass view 7: the filtered drive (driveTex = the instantaneous one)
Texture2D<float>  stateTex    : register(t11); // temporal pass: the committed drive state of the previous frame
Buffer<float>     boostLut    : register(t12); // [boostN][2]: (first non-black zone COUNT of the step, LED boost), ascending
Texture2D<float>  activeTex   : register(t13); // cols x rows: 1 = the zone counts as non-black (boost pass; pixel view 8)
Texture2D<float>  boostTex    : register(t14); // 2 x 1: [0] = the frame's LED boost, [1] = its non-black zone count (conv pass)
// Starfield balancing, all cols x rows RGBA32F, texel centres = zone centres (rules: the star-pass header below)
Texture2D<float4> starPlanTex  : register(t15); // S2 out: (w0_field, ln target, ln lift, ln peak) — Balance samples it bilinearly
                                                // (statistic rounds + pixel pass; view 9 loads it)
Texture2D<float4> starStatTex  : register(t16); // S0 out: (peak, speck-zone flag, sparse, solid) — read by S1 and S2
Texture2D<float4> starWTex     : register(t17); // S1 out: (wt = the zone's weight in the target average, wt * ln peak, flank
                                                // flag, speck-zone flag) — read by S2
Texture2D<float4> starPlan2Tex : register(t18); // S1 out: (ln background, near, speck-zone flag, w) — Balance samples .xy
                                                // bilinearly and loads .z of the pixel's OWN zone (nearest)
Texture2D<float4> starBgTex    : register(t19); // S0 out: (ln background, the brightest pixel's index ly * cellW + lx inside
                                                // the zone, lit sum, a_eff) — read by S1
// Glow fill, R32F unless noted, texel centres = zone centres (rules: the glow-pass header below)
Texture2D<float>  glowVTex   : register(t20); // G0 out: the zone pedestal Vz (cols x rows) — read by G1 and G3
Texture2D<float>  glowDilTex : register(t21); // G1 out: box maximum; (cols + 2 MAX) x (rows + 2 MAX), zone z at texel z + MAX
Texture2D<float>  glowCTex   : register(t22); // G2 out: the closing Cz (cols x rows) — read by G3
Texture2D<float4> glowEnvTex : register(t23); // G3 out, RGBA32F: (Ez, Dz, Cz, Vz) — GlowAdd samples .y bilinearly (statistic
                                              // round 1 + pixel pass); .zw = evidence for the dump
Texture2D<float>  glowKTex   : register(t24); // G4 out (glowBand only, every round): the zone's count-threshold scale k —
                                              // GlowAdd loads it for the pixel's OWN zone (nearest)
SamplerState linearClamp : register(s0);
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
// cols*cellW), so texel centres map to pixel centres with plain clamped bilinear sampling
// (= FaldModel._bilinear) once the lattice origin is removed.
float2 FineUV(float2 px) {
    return (px - float2((float)originX, (float)originY) + 0.5f) / float2((float)(cols * cellW), (float)(rows * cellH));
}
// Both fields at a frame pixel, divided by their flat-lattice response (FaldModel.backlights with
// flat_norm): a uniform field then gives B_true == B_est everywhere -> gain 1, no sub-cell sawtooth,
// no border ramp.
void SampleFields(float2 px, out float bTrue, out float bEst) {
    float2 uv = FineUV(px);
    bTrue = bTrueTex.SampleLevel(linearClamp, uv, 0) / max(flatTrueTex.SampleLevel(linearClamp, uv, 0), 1e-6f);
    bEst  = bEstTex.SampleLevel(linearClamp, uv, 0)  / max(flatEstTex.SampleLevel(linearClamp, uv, 0), 1e-6f);
}
// Pixels outside the lattice have no cell and no reference behaviour: pass them through.
bool InLattice(int2 px) {
    return px.x >= (int)originX && px.y >= (int)originY &&
           px.x < (int)(originX + cols * cellW) && px.y < (int)(originY + rows * cellH);
}

// Starfield balancing (work guide S1; reference DLC dlc/fald/starfield.py, GPU-order twin dlc/fald/gpuemu.py).
// starfield._smoothstep: the denominator floor makes lo == hi a step at lo instead of a division by zero.
float StarSmooth(float lo, float hi, float x) {
    float t = saturate((x - lo) / max(hi - lo, 1e-12f));
    return t * t * (3.0f - 2.0f * t);
}
// A pixel counts as a SPECK when it lies 0.25 .. 0.5 of the way from the (interpolated) zone background to the
// (interpolated) zone peak — background-relative, so a sky at half the star level is still sky. Speck pixels take the
// lift. The pull takes every pixel above its THRESHOLD T': the target while the target is well above the background (a
// soft star comes down as a whole: no bright ring around a pulled core), rising to the bottom of the speck band
// b + SPECK_LO (peak - b) as target / background falls from GATE_HI to GATE_LO — a target below the sky leaves the sky
// alone, and out(m) stays monotone in m (starfield.pixel_rule; DLC tests/test_fald_transfer.py pins the constants equal).
static const float FALD_STAR_SPECK_LO = 0.25f;
static const float FALD_STAR_SPECK_HI = 0.5f;
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
// (px - origin + 0.5) / cell - 0.5) IS starfield._bilinear_zones: interpolation between zone CENTRES, the border
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
    uint2 zone = uint2((uint)(px.x - (int)originX) / cellW, (uint)(px.y - (int)originY) / cellH);   // integer math, as view 9
    if (!(starPlan2Tex.Load(int3((int2)zone, 0)).z > 0.5f)) return img;   // ... and the own-zone gate, before the two taps
    float2 uv = FineUV(float2(px));
    float4 plan = starPlanTex.SampleLevel(linearClamp, uv, 0);           // w0, ln target, ln lift, ln peak
    float2 p2 = starPlan2Tex.SampleLevel(linearClamp, uv, 0).xy;         // ln background, near
    float wPx = plan.x * (1.0f - StarSmooth(starNbLo, starNbHi, p2.y));
    if (!(wPx > 0.0f)) return img;
    float safe = max(m, 1e-12f);
    float tPx = exp(plan.y);
    float bPx = exp(p2.x);                                                        // >= 1e-12: a black sky gates fully open
    float spanPx = exp(plan.w) - bPx;
    float isSpeck = (spanPx > 0.0f) ? StarSmooth(FALD_STAR_SPECK_LO, FALD_STAR_SPECK_HI, (safe - bPx) / max(spanPx, 1e-12f)) : 0.0f;
    float gPx = exp(plan.z * wPx * isSpeck);
    // the pull threshold T': the target while it is well above the background (the whole profile of a soft star comes
    // down together), rising to the bottom of the speck band as target / background falls from GATE_HI to GATE_LO — the
    // sky is never reached. out(m) = m up to T', m^(1 - a) T'^a above it: MONOTONE in m (no bright ring, no "donut")
    float g = StarSmooth(FALD_STAR_GATE_LO, FALD_STAR_GATE_HI, tPx / bPx);
    float tFloor = bPx + FALD_STAR_SPECK_LO * max(spanPx, 0.0f);
    float lnTp = lerp(log(max(tPx, tFloor)), plan.y, g);
    float outM;
    bool acts;
    if (m > exp(lnTp)) {
        float lnShown = log(min(safe, white));                                    // from the level the panel SHOWS (clips at white)
        outM = exp(lnShown + wPx * starEven * (lnTp - lnShown));                  // pulled toward T' (log domain)
        outM = max(outM, min(bPx, safe));                                         // ... never below the background
        acts = outM < safe * (1.0f - FALD_STAR_PULL_EPS);
    } else if (m <= tPx) {
        outM = min(safe * gPx, max(tPx, safe));                                   // lifted, never past the target
        acts = gPx > 1.0f;
    } else return img;                                                            // between the target and T': untouched
    if (!acts) return img;
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
// gain = the (smoothed) gain sampled at the pixel.
// Ceiling rule (work guide C10 + C11, 2026-09-15): ONE scale for all three channels (hue cannot rotate). Darkening
// applies the full gain; brightening goes through a soft knee on the brightest channel toward the LCD ceiling
// C = white * B_est — identity up to FALD_KNEE_START * C, then a smooth roll-off that never ends below the original
// and asymptotes to max(original, C): the layer never brightens INTO the ceiling (isolated highlights keep their
// gradients) and saturated highlights keep their request. The scale always lies between 1 and the gain, so no fade
// gate is needed (the fades already pulled the gain toward 1).
float3 Correct(float3 img, float bTrue, float bEst, float gain) {
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
        float cap = white * max(bEst, 1e-9f);
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
// Glow fill (work guide S2; reference DLC dlc/fald/glowfill.py, GPU-order twin dlc/fald/gpuemu.py Emu.glow_zones /
// glow_add; DLC tests/test_fald_transfer.py pins the constants equal). The rules: the header above g_faldGlowZoneSource.
static const int FALD_GLOW_REACH_MAX = 4;               // fald.h FALD_GLOW_REACH_MAX: the dilation texture's margin
static const float FALD_GLOW_SIGMA_BASE = 0.5f;         // blur sigma (zones) = BASE + PER_REACH * reach
static const float FALD_GLOW_SIGMA_PER_REACH = 0.5f;
static const float FALD_GLOW_DEFICIT_REL_LO = 0.05f;    // a dip this shallow (relative to the zone's own glow) is no hole ...
static const float FALD_GLOW_DEFICIT_REL_HI = 0.15f;    // ... from here on it is filled in full
static const float FALD_GLOW_WANT_EPS = 1e-5f;          // a wanted fill below this (as-if-white nits) touches no pixel
static const float FALD_GLOW_VIEW_SCALE = 1000.0f;      // debug view 10: 0.1 nit of added request shows as 100 nits
static const float FALD_GLOW_BAND_LO = 0.8f;            // the count-threshold band, x the mean rule's threshold: a zone whose
static const float FALD_GLOW_BAND_HI = 1.25f;           // predicted statistic would land inside is scaled down to LO
// round_fill for one pixel: req = the round's corrected request (as-if-white nits per channel), bTrue / bEst = the
// fields Correct used, px = the frame pixel (inside the lattice). want = the zone deficit interpolated between zone
// CENTRES (clamped hardware bilinear at FineUV, as the starfield plan) x strength, minus the dust threshold, capped.
// shown = the LCD light the panel will show for the request itself; only what is missing is filled, x the correction's
// own deep-dark trust in B_est (where the panel's estimate is ~0 a tiny request opens the LCD fully: NO fill there).
// The request that displays it: x B_est / B_true (never above gainMax; never raised by the lower gain clip), in the
// pedestal's colour (tminRGB / tmin: luminance-neutral), limited so the brightest channel stays at / below glowReqCeil.
// A pixel nothing is added to is returned untouched (bit-identical). k = the count-threshold band's scale of the pixel's
// own zone (pass G4; 1 = none), applied to the want AFTER the cap.
float3 GlowAddK(float3 req, float bTrue, float bEst, float2 px, float k) {
    float want = min(max(glowStrength * glowEnvTex.SampleLevel(linearClamp, FineUV(px), 0).y - FALD_GLOW_WANT_EPS, 0.0f), glowCapNits) * k;
    if (!(want > 0.0f)) return req;
    bTrue = max(bTrue, 0.0f);
    float r = max(req.r, max(req.g, req.b));
    float shown = r * bTrue / max(bEst, 1e-9f);
    float fill = max(want - shown, 0.0f) * smoothstep(fadeLo, fadeHi, bEst);
    float add = fill * min(bEst / max(bTrue, 1e-9f), gainMax);
    float3 m = float3(tminR, tminG, tminB) / max(tmin, 1e-30f);
    float room = max(glowReqCeil - r, 0.0f);
    add *= min(1.0f, room / max(add * max(m.r, max(m.g, m.b)), 1e-30f));
    if (!(add > 0.0f)) return req;
    return req + add * m;
}
// What the statistic round 1 and the pixel pass call: k of the pixel's OWN zone (a nearest Load — NOT interpolated: only
// then does the zone's predicted statistic scale exactly as the band pass assumes), 1 without the band.
float3 GlowAdd(float3 req, float bTrue, float bEst, int2 px) {
    float k = 1.0f;
    if (glowBand != 0u) {
        uint2 zone = uint2((uint)(px.x - (int)originX) / cellW, (uint)(px.y - (int)originY) / cellH);
        k = glowKTex.Load(int3((int2)zone, 0));
    }
    return GlowAddK(req, bTrue, bEst, float2(px), k);
}
)";

// Pass 1: per-cell area statistic -> drive. One thread group per cell, 256 threads sweep the block.
// With a boost LUT (boostN != 0) the same sweep counts the zone's pixels above boostLitNits / boostDimNits and writes
// the zone's NON-BLACK flag (u1) — on the frame the PANEL receives: the source in round 0, the corrected frame in
// round 1 (DLC correct_image re-reads the boost from each round's request). boostRule 1 (LIT-or-MEAN, C12b) also sums
// nits^boostMeanGamma over the zone's pixels (exp(gamma * log) under mc > 0: no pow(0) / negative / NaN case) and
// replaces the DIM test by mean >= boostMeanThresh; rule 0 computes exactly what it did before the rule existed.
inline const char* g_faldStatSource = R"(
RWTexture2D<float> driveOut : register(u0);
RWTexture2D<float> activeOut : register(u1);
groupshared float gMax[256];
groupshared float gSum[256];
groupshared uint gLit[256];
groupshared uint gDim[256];
groupshared float gPow[256];

[numthreads(256, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint cx = gid.x, cy = gid.y;
    uint n = cellW * cellH;
    float m = 0.0f, sum = 0.0f;
    uint lit = 0, dim = 0;
    float powSum = 0.0f;
    for (uint k = tid.x; k < n; k += 256) {
        uint px = originX + cx * cellW + (k % cellW);
        uint py = originY + cy * cellH + (k / cellW);
        if (px >= frameW || py >= frameH) continue;
        float3 img = PanelNits(frameTex.Load(int3(px, py, 0)).rgb);
        if (starOn != 0u) img = Balance(img, int2((int)px, (int)py));   // the frame the layer works on (S1)
        if (roundIdx == 1) {
            float bT, bE; SampleFields(float2((float)px, (float)py), bT, bE);
            float g = gainTex.SampleLevel(linearClamp, FineUV(float2((float)px, (float)py)), 0);
            img = Correct(img, bT, bE, g);
            if (glowOn != 0u) img = GlowAdd(img, bT, bE, int2((int)px, (int)py));   // the frame the panel receives (S2)
        }
        float mc = max(img.r, max(img.g, img.b));
        float s = min(mc, white);
        if (s > driveFloor) { m = max(m, s); sum += s; }
        if (boostN != 0u) {
            if (mc > boostLitNits) lit++;
            if (mc > boostDimNits) dim++;
            if (boostRule == 1u && mc > 0.0f) powSum += exp(boostMeanGamma * log(mc));
        }
    }
    gMax[tid.x] = m; gSum[tid.x] = sum;
    gLit[tid.x] = lit; gDim[tid.x] = dim;
    gPow[tid.x] = powSum;
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) {
            gMax[tid.x] = max(gMax[tid.x], gMax[tid.x + stride]);
            gSum[tid.x] += gSum[tid.x + stride];
            gLit[tid.x] += gLit[tid.x + stride];
            gDim[tid.x] += gDim[tid.x + stride];
            gPow[tid.x] += gPow[tid.x + stride];
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0) {
        float stat = min(gMax[0], gSum[0] / area0);   // min(brightest lit px, sum lit nits*px^2 / A0)
        driveOut[uint2(cx, cy)] = DriveOf(stat);
        if (boostN != 0u) {
            // LIT-or-DIM / LIT-or-MEAN (FaldModel.active_zones): fractions of the zone's pixels (the lattice lies inside
            // the frame — FaldLatticeFits — so every zone has cellW * cellH of them)
            float litF = (float)gLit[0] / (float)n;
            float dimF = (float)gDim[0] / (float)n;
            bool second = (boostRule == 1u) ? (gPow[0] / (float)n >= boostMeanThresh) : (dimF > boostDimFrac);
            activeOut[uint2(cx, cy)] = (litF > boostLitFrac || second) ? 1.0f : 0.0f;
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
//   arg     = ly * cellW + lx of the brightest pixel (ties: nearest a zone border, key max(|2lx - (cw-1)| ch,
//             |2ly - (ch-1)| cw), then the first in row-major order)                                        S0   bg.y
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
//   ln_t = ln target (ln white if 0);  ln_g = spk && peak < target ? lift * (ln_t - ln peak) : 0;  ln_pk = spk ? ln peak : ln_t
//                                                                                                           S2   plan.yzw
//   ln_b = ln max(b, 1e-12)                                                                                 S0   bg.x -> S1 plan2.x
// Textures: stat (S0 u0) = peak, spk, sparse, solid | bg (S0 u1) = ln b, arg, total, a_eff | w (S1 u0) = wt, wt ln peak,
//   flank, spk | plan2 (S1 u1) = ln b, near, spk, w | plan (S2 u0) = w0_field, ln_t, ln_g, ln_pk.
// Bindings: S0 reads t0 frame + t1 curve; S1 reads t16 stat + t19 bg; S2 reads t16 stat + t17 w; the statistic rounds and
//   the pixel pass (Balance) read t15 plan + t18 plan2. Order per frame: S0, S1, S2, then the unchanged layer.
//
// Per pixel (Balance; m = its brightest channel): plan and plan2.xy sampled BILINEARLY at FineUV (= between zone centres,
//   border zones held), plan2.z loaded for the pixel's OWN zone (nearest).
//   w_px = w0_px * (1 - ss(nbLo, nbHi, near_px));   untouched unless w_px > 0, m > 0 and the own zone is a speck zone
//   is_speck = pk_px > b_px ? ss(0.25, 0.5, (m - b_px) / (pk_px - b_px)) : 0                 (background-relative band)
//   pull threshold T' = exp(lerp(ln max(t_px, b_px + 0.25 max(pk_px - b_px, 0)), ln t_px, ss(GATE_LO 1, GATE_HI 2, t_px / b_px)))
//   m >  T'  : mc = min(m, white) (the level the panel SHOWS); out = max(exp(ln mc + w_px * even * (ln T' - ln mc)),
//              min(b_px, m))   MONOTONE in m (out(T') = T', slope 1 - w_px even >= 0, flat above white);
//              acts if out < m (1 - PULL_EPS 1e-5)
//   m <= t_px: out = min(m * exp(ln_g_px * w_px * is_speck), max(t_px, m));                 acts if that factor > 1
//   t_px < m <= T': untouched
//   acts -> rgb * out / m (one scale, hue-preserving); else the pixel is returned BIT-identical.
//   Consequences: a field whose peaks all lie below keepNits is bit-identical; with the target below / near the sky a star
//   stops at the bottom of its speck band b + 0.25 (pk - b), so capNits below that is not reached.
// Settings (CB words 35, 52-65; defaults): on 0 | even 0.8, lift 0, targetGain 1, capNits 0 | strength 1, areaLo 40, areaHi 160,
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
//     the lift and the peak field of the plan pass and the own-zone gate of Balance key on it;
//   arg = the brightest pixel's position inside the zone, ly * cellW + lx (un-gated maximum; ties: the pixel nearest a
//     zone border — mirror-symmetric — then the first in row-major order; StarEdgeKey / StarBetter) — S1's flank test.
// u0 = (peak, spk, sparse, solid); u1 = (ln b, arg, lit sum, a_eff) — S1 copies ln b into the texture the pixels sample.
inline const char* g_faldStarStatSource = R"(
RWTexture2D<float4> starStatOut : register(u0);
RWTexture2D<float4> starBgOut : register(u1);
groupshared float gMax[256];
groupshared float gSum[256];
groupshared float gMaxAll[256];
groupshared float gMin[256];
groupshared float gSumAll[256];
groupshared uint gCount[256];
groupshared uint gArg[256];

// Position of the zone's brightest pixel: k = ly * cellW + lx. Ties: the pixel nearest the zone border (largest key),
// then the first in row-major order — a total order, so the parallel reduction gives the same pixel whatever its shape
// (the emulator applies the same rule). A flat-topped speck at an edge then counts as AT the edge on either side.
uint StarEdgeKey(uint k) {
    int lx = (int)(k % cellW), ly = (int)(k / cellW);
    return (uint)max(abs(2 * lx - ((int)cellW - 1)) * (int)cellH, abs(2 * ly - ((int)cellH - 1)) * (int)cellW);
}
bool StarBetter(float va, uint ka, float vb, uint kb) {          // is (vb, kb) preferred over (va, ka)?
    if (vb != va) return vb > va;
    uint ea = StarEdgeKey(ka), eb = StarEdgeKey(kb);
    if (eb != ea) return eb > ea;
    return kb < ka;
}

[numthreads(256, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint cx = gid.x, cy = gid.y;
    uint n = cellW * cellH;
    float m = 0.0f, sum = 0.0f, mAll = -1.0f, mn = 3.0e38f, sumAll = 0.0f;
    uint count = 0, kAll = 0;
    for (uint k = tid.x; k < n; k += 256) {
        uint px = originX + cx * cellW + (k % cellW);
        uint py = originY + cy * cellH + (k / cellW);
        if (px >= frameW || py >= frameH) continue;          // only in-frame pixels count (n, min and sums alike)
        float3 img = PanelNits(frameTex.Load(int3(px, py, 0)).rgb);
        float s = min(max(img.r, max(img.g, img.b)), white);
        if (s > driveFloor) { m = max(m, s); sum += s; }
        if (StarBetter(mAll, kAll, s, k)) { mAll = s; kAll = k; }
        mn = min(mn, s); sumAll += s; count++;
    }
    gMax[tid.x] = m; gSum[tid.x] = sum;
    gMaxAll[tid.x] = mAll; gArg[tid.x] = kAll; gMin[tid.x] = mn; gSumAll[tid.x] = sumAll; gCount[tid.x] = count;
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) {
            gMax[tid.x] = max(gMax[tid.x], gMax[tid.x + stride]);
            gSum[tid.x] += gSum[tid.x + stride];
            if (StarBetter(gMaxAll[tid.x], gArg[tid.x], gMaxAll[tid.x + stride], gArg[tid.x + stride])) {
                gMaxAll[tid.x] = gMaxAll[tid.x + stride]; gArg[tid.x] = gArg[tid.x + stride];
            }
            gMin[tid.x] = min(gMin[tid.x], gMin[tid.x + stride]);
            gSumAll[tid.x] += gSumAll[tid.x + stride];
            gCount[tid.x] += gCount[tid.x + stride];
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0) {
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
        starStatOut[uint2(cx, cy)] = float4(peak, (has && speck && aEff < starAreaHi) ? 1.0f : 0.0f, sparse, solid);
        starBgOut[uint2(cx, cy)] = float4(log(max(b, 1e-12f)), (gCount[0] != 0u) ? (float)gArg[0] : 0.0f, total, aEff);
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
    int lx = (int)(k0 % cellW), ly = (int)(k0 / cellW);
    bool flank = false;
    float partners = 0.0f;                                        // neighbours with the same geometry and an EQUAL peak
    for (int fy = -1; fy <= 1; fy++) {
        int y = (int)id.y + fy; if (y < 0 || y >= (int)rows) continue;
        for (int fx = -1; fx <= 1; fx++) {
            int x = (int)id.x + fx; if (x < 0 || x >= (int)cols || (fx == 0 && fy == 0)) continue;
            float np_ = starStatTex.Load(int3(x, y, 0)).r;        // a LARGER peak = this zone is its flank; an EQUAL one = a
            if (!(np_ >= st.r && np_ > 0.0f)) continue;           // flat-topped feature straddling the border: they share a vote
            uint k1 = (uint)(starBgTex.Load(int3(x, y, 0)).y + 0.5f);
            int nx = (int)(k1 % cellW), ny = (int)(k1 / cellW);
            bool okx = (fx == 1) ? (lx >= (int)cellW - FALD_STAR_FLANK_PX && nx < FALD_STAR_FLANK_NEAR_PX)
                     : (fx == -1) ? (lx < FALD_STAR_FLANK_PX && nx >= (int)cellW - FALD_STAR_FLANK_NEAR_PX)
                     : (abs(lx - nx) <= FALD_STAR_FLANK_NEAR_PX);
            bool oky = (fy == 1) ? (ly >= (int)cellH - FALD_STAR_FLANK_PX && ny < FALD_STAR_FLANK_NEAR_PX)
                     : (fy == -1) ? (ly < FALD_STAR_FLANK_PX && ny >= (int)cellH - FALD_STAR_FLANK_NEAR_PX)
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
//   ln_t    = ln(target) (ln(white) where the target is 0), ln_g = lift * (ln_t - ln peak) for a speck zone below its
//             target (else 0), ln_pk = ln(peak) of a speck zone (ln_t for a zone without a speck: its sky is no "peak")
inline const char* g_faldStarPlanSource = R"(
RWTexture2D<float4> starPlanOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float4 st = starStatTex.Load(int3(id.xy, 0));
    float peak = st.r;
    float4 sw = starWTex.Load(int3(id.xy, 0));
    bool spk = sw.a > 0.5f;
    float lp = log(max(peak, 1e-12f));
    int E = (int)starEvenReach;
    float wsum = 0.0f, wl = 0.0f;
    for (int dy = -E; dy <= E; dy++) {
        int y = (int)id.y + dy; if (y < 0 || y >= (int)rows) continue;
        for (int dx = -E; dx <= E; dx++) {
            int x = (int)id.x + dx; if (x < 0 || x >= (int)cols) continue;
            float4 t = starWTex.Load(int3(x, y, 0));
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
                    float4 t2 = starWTex.Load(int3(x2, y2, 0));
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
            s3 += starStatTex.Load(int3(x, y, 0)).b; n3 += starWTex.Load(int3(x, y, 0)).a;
        }
    }
    float carry = s3 * starStrength / max(n3, 1.0f) * (1.0f - StarSmooth(starNbLo, starNbHi, st.a));   // sparse > 0 only in speck zones
    float wField = spk ? st.b * starStrength : carry;
    float lnT = log(max((target > 0.0f) ? target : white, 1e-12f));
    float lnG = (spk && peak < target) ? starLift * (lnT - lp) : 0.0f;
    starPlanOut[id.xy] = float4(wField, lnT, lnG, spk ? lp : lnT);
}
)";

// =====================================================================================================================
// GLOW FILL — THE RULES (2026-09-20; experimental, default off). Reference: DLC dlc/fald/glowfill.py (module docstring =
// this text), GPU-order twin dlc/fald/gpuemu.py; tests DLC tests/test_fald_glowfill*.py. Run after EACH round's conv pass
// on that round's B_true texture (LED boost included): round 0's fill is part of the frame the round-1 statistic / boost
// count see, round 1's is part of the output.
//
// Per zone                                                                                          pass  texture
//   Vz = white * tmin * mean over the zone's sub x sub fine texels of max(bTrue / flatTrue, 0)        G0   glowV
//   dil(e) = max of V over the (2 reach + 1)^2 box around e, V continued beyond the lattice by its border values
//            (clamped reads), for every e within `reach` zones of the lattice                          G1   glowDil
//   Cz = min of dil over the (2 reach + 1)^2 box = the grey CLOSING: holes / valleys narrower than 2 reach zones are
//        filled to the lowest level around them; a glow that only falls away from its source is kept (no skirt around a
//        window, no filled letterbox bars; exact at the frame edge)                                   G2   glowC
//   Ez = min(Gaussian blur of Cz, Cz); sigma = SIGMA_BASE + SIGMA_PER_REACH * reach zones, radius ceil(3 sigma), border
//        values held, normalised                                                                       G3   glowEnv.x
//   Dz = (Ez - Vz) * smoothstep(DEFICIT_REL_LO, DEFICIT_REL_HI, (Ez - Vz) / Vz) where Ez > Vz, else 0  G3   glowEnv.y
// Count-threshold band (glowBand: a boost LUT AND the mean zone rule; EVERY round, from its own request) G4   glowK
//   Pc / Pf = the zone mean of (brightest channel)^boostMeanGamma of the round's request WITHOUT / WITH the unscaled
//   fill — the statistic the firmware will form. A zone not counted by its content (not LIT, Pc < T) whose Pf lies in
//   [BAND_LO T, BAND_HI T] gets k = ((BAND_LO T - Pc) / (Pf - Pc))^(1 / gamma): its own pixels' want x k puts the
//   statistic at BAND_LO T — clearly uncounted (T is known to +-7 %). Every other zone: k = 1. The statistic round 1
//   reads round 0's k, the pixel pass round 1's (exact for the frame that is sent).
// Per pixel: GlowAdd (common source). Settings (CB words 75-80; defaults): on 0 | strength 1 (0..1), capNits 0.05
//   (0.005..0.5), reach 2 (1..4) | reqCeil = min(0.4 driveFloor, 0.55 boostLitNits [file with a boost LUT]) | band.
// HDR (PQ panel files) only: the C++ keeps the option off for a gamma-transfer file.
// Bindings: G0 reads t5 bTrue + t7 flatTrue; G1 reads t20; G2 reads t21; G3 reads t20 + t22; G4 reads what the statistic
//   round 1 reads (t0, t1, t5-t9, t15 / t18, t23); the statistic round 1 and the pixel pass read t23 (+ t24 with the band).
// =====================================================================================================================

// Glow pass G0: the zone pedestal (glowfill.zone_pedestal). Sum order: oy outer, ox inner (the twin's).
inline const char* g_faldGlowZoneSource = R"(
RWTexture2D<float> glowVOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float acc = 0.0f;
    for (uint oy = 0; oy < sub; oy++) {
        for (uint ox = 0; ox < sub; ox++) {
            int3 f = int3((int)(id.x * sub + ox), (int)(id.y * sub + oy), 0);
            acc += max(bTrueTex.Load(f) / max(flatTrueTex.Load(f), 1e-6f), 0.0f);
        }
    }
    glowVOut[id.xy] = acc * (white * tmin / (float)(sub * sub));
}
)";

// Glow pass G1: box maximum on the lattice extended by glowReach on every side (texel = zone + FALD_GLOW_REACH_MAX); V
// beyond the lattice = its border value (clamped reads). Texels further out are never read: 0.
inline const char* g_faldGlowDilateSource = R"(
RWTexture2D<float> glowDilOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols + 2u * (uint)FALD_GLOW_REACH_MAX || id.y >= rows + 2u * (uint)FALD_GLOW_REACH_MAX) return;
    int R = (int)glowReach;
    int ex = (int)id.x - FALD_GLOW_REACH_MAX, ey = (int)id.y - FALD_GLOW_REACH_MAX;      // lattice coordinates
    if (ex < -R || ey < -R || ex >= (int)cols + R || ey >= (int)rows + R) { glowDilOut[id.xy] = 0.0f; return; }
    float m = 0.0f;                                                                      // V >= 0
    for (int dy = -R; dy <= R; dy++) {
        int y = clamp(ey + dy, 0, (int)rows - 1);
        for (int dx = -R; dx <= R; dx++) {
            int x = clamp(ex + dx, 0, (int)cols - 1);
            m = max(m, glowVTex.Load(int3(x, y, 0)));
        }
    }
    glowDilOut[id.xy] = m;
}
)";

// Glow pass G2: box minimum of the dilation = the closing.
inline const char* g_faldGlowErodeSource = R"(
RWTexture2D<float> glowCOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    int R = (int)glowReach;
    float m = 3.0e38f;
    for (int dy = -R; dy <= R; dy++) {
        for (int dx = -R; dx <= R; dx++) {
            m = min(m, glowDilTex.Load(int3((int)id.x + dx + FALD_GLOW_REACH_MAX, (int)id.y + dy + FALD_GLOW_REACH_MAX, 0)));
        }
    }
    glowCOut[id.xy] = m;
}
)";

// Glow pass G3: the envelope (blur of the closing, never above it) and the zone deficit.
inline const char* g_faldGlowEnvSource = R"(
RWTexture2D<float4> glowEnvOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float sigma = FALD_GLOW_SIGMA_BASE + FALD_GLOW_SIGMA_PER_REACH * (float)glowReach;
    int R = (int)ceil(3.0f * sigma);
    float acc = 0.0f, wsum = 0.0f;
    for (int dy = -R; dy <= R; dy++) {
        int y = clamp((int)id.y + dy, 0, (int)rows - 1);
        for (int dx = -R; dx <= R; dx++) {
            int x = clamp((int)id.x + dx, 0, (int)cols - 1);
            float w = exp(-0.5f * (float)(dx * dx + dy * dy) / (sigma * sigma));
            acc += w * glowCTex.Load(int3(x, y, 0)); wsum += w;
        }
    }
    float c = glowCTex.Load(int3(id.xy, 0));
    float e = min(acc / wsum, c);
    float v = glowVTex.Load(int3(id.xy, 0));
    float d = max(e - v, 0.0f);
    d *= smoothstep(FALD_GLOW_DEFICIT_REL_LO, FALD_GLOW_DEFICIT_REL_HI, d / max(v, 1e-12f));
    glowEnvOut[id.xy] = float4(e, d, c, v);
}
)";

// Glow pass G4 (glowBand only, every round): the count-threshold band's zone scale (glowfill.band_scale). One thread group
// per zone, 256 threads sweep its pixels exactly like the statistic pass (the same thread / reduction order: the twin's
// Emu.zone_pow_sum); the request is the round's corrected one, the fill the UNSCALED one (k = 1).
inline const char* g_faldGlowBandSource = R"(
RWTexture2D<float> glowKOut : register(u0);
groupshared float gPowC[256];
groupshared float gPowF[256];
groupshared uint gLitC[256];

[numthreads(256, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint cx = gid.x, cy = gid.y;
    uint n = cellW * cellH;
    float powC = 0.0f, powF = 0.0f;
    uint lit = 0;
    for (uint k = tid.x; k < n; k += 256) {
        uint px = originX + cx * cellW + (k % cellW);
        uint py = originY + cy * cellH + (k / cellW);
        if (px >= frameW || py >= frameH) continue;
        float3 img = PanelNits(frameTex.Load(int3(px, py, 0)).rgb);
        if (starOn != 0u) img = Balance(img, int2((int)px, (int)py));
        float bT, bE; SampleFields(float2((float)px, (float)py), bT, bE);
        float g = gainTex.SampleLevel(linearClamp, FineUV(float2((float)px, (float)py)), 0);
        float3 c3 = Correct(img, bT, bE, g);
        float3 f3 = GlowAddK(c3, bT, bE, float2((float)px, (float)py), 1.0f);
        float c = max(c3.r, max(c3.g, c3.b)), f = max(f3.r, max(f3.g, f3.b));
        if (c > 0.0f) powC += exp(boostMeanGamma * log(c));
        if (f > 0.0f) powF += exp(boostMeanGamma * log(f));
        if (c > boostLitNits) lit++;
    }
    gPowC[tid.x] = powC; gPowF[tid.x] = powF; gLitC[tid.x] = lit;
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) {
            gPowC[tid.x] += gPowC[tid.x + stride];
            gPowF[tid.x] += gPowF[tid.x + stride];
            gLitC[tid.x] += gLitC[tid.x + stride];
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0) {
        float pc = gPowC[0] / (float)n, pf = gPowF[0] / (float)n;
        bool litZone = (float)gLitC[0] / (float)n > boostLitFrac;
        float t = boostMeanThresh;
        float kz = 1.0f;
        if (!litZone && pc < t && pf >= FALD_GLOW_BAND_LO * t && pf <= FALD_GLOW_BAND_HI * t) {
            float share = saturate((FALD_GLOW_BAND_LO * t - pc) / max(pf - pc, 1e-30f));
            kz = (share > 0.0f) ? exp(log(share) / boostMeanGamma) : 0.0f;
        }
        glowKOut[uint2(cx, cy)] = kz;
    }
}
)";

// Pass 1a (boost LUT only): the frame's black-frame LED boost. Counts the non-black zones of the statistic pass
// (t13) and looks the staircase up by COUNT: the last step whose first count is <= N applies, below the first step
// the boost is 1 (FaldModel.boost_of_fraction; the C++ turns the file's zone fractions into counts —
// FaldBoostZoneThreshold — so no float division decides a step edge on the GPU). Instantaneous by design: the panel
// switches within one meter read in both directions, so the temporal drive state does not filter it.
// The count is a 256-thread reduction (an integer sum: any order gives the same N), then thread 0 looks the step up.
// (One thread walking every zone cost ~53 us per round at 2304 zones on an RTX 5090; the loader allows 512 x 512.)
inline const char* g_faldBoostSource = R"(
RWTexture2D<float> boostOut : register(u0);
groupshared uint gCount[256];

[numthreads(256, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    uint count = 0;
    for (uint k = tid.x; k < cols * rows; k += 256) {
        uint x = k % cols, y = k / cols;
        if (activeTex.Load(int3(x, y, 0)) > 0.5f) count++;
    }
    gCount[tid.x] = count;
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) gCount[tid.x] += gCount[tid.x + stride];
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x != 0) return;
    count = gCount[0];
    float b = 1.0f;
    [loop] for (uint i = 0; i < boostN; i++) {
        if ((float)count < boostLut[2 * i]) break;
        b = boostLut[2 * i + 1];
    }
    boostOut[uint2(0, 0)] = b;
    boostOut[uint2(1, 0)] = (float)count;
}
)";

// Pass 2: the two backlight fields on the sub-cell grid. out[k] = sum_i d[k - i] * kern[i]
// (scipy fftconvolve 'same', odd kernels, zero outside the lattice), kernel chosen by sub-offset.
// The black-frame LED boost (boostN != 0) multiplies B_true ONLY — the panel's own estimate does not know it
// (FaldModel.backlights). The flat-lattice normalisation fields are built with boostN = 0.
// Thread layout: one thread per (cell, sub-offset). A group is FALD_CONV_THREADS consecutive cells (row-major) of ONE
// sub-offset (SV_GroupID.y), so all its threads read the SAME kernel tap at every step of the sum, and neighbouring
// drive texels. The kernel table is sub * sub separate slices; the previous 16 x 16 block of the fine grid spanned 64
// sub-offsets and so read 64 slices per step (2x slower on a 5090). Layout only: every output's terms, their order and
// each operation are what they were (bit-identical, HW + WARP, 2026-09-22). A linear cell index wastes no lanes on a
// lattice that is not a multiple of 16 (edge-lit strips, odd grids). Dispatch: FaldConvGroupsX(cols, rows) x sub * sub.
inline const char* g_faldConvSource = R"(
RWTexture2D<float> bTrueOut : register(u0);
RWTexture2D<float> bEstOut  : register(u1);

[numthreads(64, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint cell = gid.x * 64u + tid.x;
    if (cell >= cols * rows) return;
    uint so = gid.y;                                    // sub-offset = (fy % sub) * sub + (fx % sub)
    int cx = (int)(cell % cols), cy = (int)(cell / cols);
    uint fx = (uint)cx * sub + so % sub, fy = (uint)cy * sub + so / sub;

    uint Wt = 2 * reachTrueC + 1, Ht = 2 * reachTrueR + 1;
    float accT = 0.0f;
    for (int j = -(int)reachTrueR; j <= (int)reachTrueR; j++) {
        int sy = cy - j; if (sy < 0 || sy >= (int)rows) continue;
        for (int i = -(int)reachTrueC; i <= (int)reachTrueC; i++) {
            int sx = cx - i; if (sx < 0 || sx >= (int)cols) continue;
            accT += driveTex.Load(int3(sx, sy, 0)) * kTrue[(so * Ht + (uint)(j + (int)reachTrueR)) * Wt + (uint)(i + (int)reachTrueC)];
        }
    }
    uint We = 2 * reachEstC + 1, He = 2 * reachEstR + 1;
    float accE = 0.0f;
    for (int j2 = -(int)reachEstR; j2 <= (int)reachEstR; j2++) {
        int sy = cy - j2; if (sy < 0 || sy >= (int)rows) continue;
        for (int i2 = -(int)reachEstC; i2 <= (int)reachEstC; i2++) {
            int sx = cx - i2; if (sx < 0 || sx >= (int)cols) continue;
            accE += driveEstTex.Load(int3(sx, sy, 0)) * kEst[(so * He + (uint)(j2 + (int)reachEstR)) * We + (uint)(i2 + (int)reachEstC)];
        }
    }
    if (boostN != 0u) accT *= boostTex.Load(int3(0, 0, 0));
    bTrueOut[uint2(fx, fy)] = accT;
    bEstOut[uint2(fx, fy)] = accE;
}
)";

// The conv pass's group size: the literal in its [numthreads(64, 1, 1)] and `gid.x * 64u` above (DLC
// tests/test_fald_shader_layout.py pins that all three agree). Both RunConv sites (src/fald.cpp, dwm_hook/hook_fald.cpp)
// dispatch FaldConvGroupsX(cols, rows) x sub * sub groups; the loader's limits (cols, rows <= 512, sub <= 16) keep that
// inside D3D11's 65535 groups per dimension (<= 4096 x 256).
static const unsigned int FALD_CONV_THREADS = 64u;
inline unsigned int FaldConvGroupsX(unsigned int cols, unsigned int rows) {
    return (cols * rows + FALD_CONV_THREADS - 1u) / FALD_CONV_THREADS;
}

// Pass 1b (tempMode != 0 only): per-cell first-order drive state. driveTex (t4) = this round's instantaneous
// drive, stateTex (t11) = the state committed after the previous frame's round 1. out = s + a * (d - s) with
// a = tempAlphaRise when the drive rises, tempAlphaFall when it falls (DLC temporal.DriveState.peek); with no
// valid state yet (tempInit) the drive is copied. Both rounds of a frame read the SAME committed state; the
// C++ copies round 1's output into the state texture afterwards (DriveState.commit).
inline const char* g_faldTemporalSource = R"(
RWTexture2D<float> driveFiltOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float d = driveTex.Load(int3(id.xy, 0));
    if (tempInit != 0u) { driveFiltOut[id.xy] = d; return; }
    float s = stateTex.Load(int3(id.xy, 0));
    float a = (d > s) ? tempAlphaRise : tempAlphaFall;
    driveFiltOut[id.xy] = s + a * (d - s);
}
)";

// Pass 1c (temporal mode 3 "panel clock" only; rules in fald.h above FALD_TEMPORAL_PANEL, reference DLC
// dlc/fald/paneltime.py, GPU-order twin gpuemu.GpuPanelDriveState.advance). Runs ONCE per frame, before round 0, and
// only with a valid state: driveTex (t4) = the PREVIOUS processed frame's round-1 instantaneous drives = the target of
// every dimming-engine tick since that frame was first shown; clkState0 / clkState1 = the LED states of the two parity
// clocks, advanced in place to this frame's first refresh. Out: the drive maps the real-spread kernel (u0) and the
// estimate kernel (u1: the LED state one refresh earlier) see in BOTH rounds — the clocks' weighted mean. Nothing here
// depends on this frame's content; the C++ stores this frame's round-1 drives as the next target afterwards.
inline const char* g_faldPanelClockSource = R"(
RWTexture2D<float> clkTrueOut : register(u0);
RWTexture2D<float> clkEstOut  : register(u1);
RWTexture2D<float> clkState0  : register(u2);
RWTexture2D<float> clkState1  : register(u3);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= cols || id.y >= rows) return;
    float d = driveTex.Load(int3(id.xy, 0));
    float s0 = clkState0[id.xy], s1 = clkState1[id.xy];
    float g0 = d - s0, g1 = d - s1;
    float t0 = s0 + clkTrue0 * g0, t1 = s1 + clkTrue1 * g1;
    clkTrueOut[id.xy] = clkW0 * t0 + clkW1 * t1;
    clkEstOut[id.xy] = clkW0 * (s0 + clkEst0 * g0) + clkW1 * (s1 + clkEst1 * g1);
    clkState0[id.xy] = t0; clkState1[id.xy] = t1;
}
)";

// Pass 2b: gain on the fine grid (flat-normalised fields, clamp, deep-dark fade) -> u0.
inline const char* g_faldGainSource = R"(
RWTexture2D<float> gainOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    uint fx = id.x, fy = id.y;
    if (fx >= cols * sub || fy >= rows * sub) return;
    float bT = bTrueTex.Load(int3(fx, fy, 0)) / max(flatTrueTex.Load(int3(fx, fy, 0)), 1e-6f);
    float bE = bEstTex.Load(int3(fx, fy, 0))  / max(flatEstTex.Load(int3(fx, fy, 0)), 1e-6f);
    gainOut[uint2(fx, fy)] = RawGain(bT, bE);
}
)";

// Pass 2c: separable Gaussian blur of the gain (t9 -> u0), sigma = gainSmoothFine fine samples, radius 3 sigma,
// clamped at the grid edge. Run twice (blurDir 0 then 1). With gainSmoothFine == 0 it copies.
inline const char* g_faldBlurSource = R"(
RWTexture2D<float> blurOut : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    int fx = (int)id.x, fy = (int)id.y;
    int W = (int)(cols * sub), H = (int)(rows * sub);
    if (fx >= W || fy >= H) return;
    if (gainSmoothFine <= 0.0f) { blurOut[uint2(fx, fy)] = gainTex.Load(int3(fx, fy, 0)); return; }
    int R = (int)ceil(3.0f * gainSmoothFine);
    float acc = 0.0f, wsum = 0.0f;
    for (int k = -R; k <= R; k++) {
        int x = fx, y = fy;
        if (blurDir == 0) x = clamp(fx + k, 0, W - 1); else y = clamp(fy + k, 0, H - 1);
        float w = exp(-0.5f * (float)(k * k) / (gainSmoothFine * gainSmoothFine));
        acc += w * gainTex.Load(int3(x, y, 0)); wsum += w;
    }
    blurOut[uint2(fx, fy)] = acc / wsum;
}
)";

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
//     pixels — its own pixels are never acted on), tinted blue by how far the zone's peak is pulled down and red by
//     how far it is lifted (ln ratio, 2 stops = full tint); passthrough when the option is off. View 4 stays a pure
//     passthrough of the SOURCE frame.
//     10 = glow fill: the request the fill ADDS at each pixel (as-if-white nits per channel, in the pedestal's colour),
//     x FALD_GLOW_VIEW_SCALE (0.1 nit shows as 100 nits); black = nothing added; passthrough when the option is off.
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
    float gain = gainTex.SampleLevel(linearClamp, FineUV(float2(px)), 0);
    if (debugMode == 1) {
        // diverging map, the DLC analysis convention: white = no change, red = brighten, blue = darken,
        // +-25 % full scale (saturated red/blue), on a 100-nit white
        float t = saturate(abs(gain - 1.0f) * 4.0f);
        float3 c = (gain >= 1.0f) ? float3(1.0f, 1.0f - t, 1.0f - t) : float3(1.0f - t, 1.0f - t, 1.0f);
        return float4(c * DebugWhite(), 1.0f);
    }
    if (debugMode == 2) { float v = saturate(bT) * DebugWhite(); return float4(v, v, v, 1.0f); }
    if (debugMode == 3) { float v = saturate(bE) * DebugWhite(); return float4(v, v, v, 1.0f); }
    if (debugMode == 7) {
        uint2 c = uint2((uint)(px.x - (int)originX) / cellW, (uint)(px.y - (int)originY) / cellH);   // inside the lattice here
        float dInst = driveTex.Load(int3((int2)c, 0));
        float dFilt = driveEstTex.Load(int3((int2)c, 0));
        float diff = dInst - dFilt;                         // > 0: instantaneous drive above the state (rising)
        float t = saturate(abs(diff) * 4.0f);
        float3 cc = (diff >= 0.0f) ? float3(1.0f, 1.0f - t, 1.0f - t) : float3(1.0f - t, 1.0f - t, 1.0f);
        return float4(cc * DebugWhite(), 1.0f);
    }
    if (debugMode == 8) {
        if (boostN == 0u) return src;
        uint2 c8 = uint2((uint)(px.x - (int)originX) / cellW, (uint)(px.y - (int)originY) / cellH);
        float v8 = (activeTex.Load(int3((int2)c8, 0)) > 0.5f) ? DebugWhite() : 0.0f;
        return float4(v8, v8, v8, 1.0f);
    }
    if (debugMode == 9) {
        if (starOn == 0u) return src;
        uint2 c9 = uint2((uint)(px.x - (int)originX) / cellW, (uint)(px.y - (int)originY) / cellH);
        float4 zp = starPlanTex.Load(int3((int2)c9, 0));              // w0_field, ln target, ln lift, ln peak
        float wz = zp.x * (1.0f - StarSmooth(starNbLo, starNbHi, starPlan2Tex.Load(int3((int2)c9, 0)).y));   // x the protection
        float down = wz * starEven * min(zp.y - zp.w, 0.0f);          // ln(new peak / peak) of a zone above its target
        float up = wz * zp.z;                                         // ... of a lifted zone
        float tb = saturate(-down / 1.3862944f), tr = saturate(up / 1.3862944f);   // 2 stops = ln 4
        float3 col9 = wz * float3(1.0f - tb, 1.0f - max(tb, tr), 1.0f - tr);
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
    if (debugMode == 10) {
        if (glowOn == 0u) return src;
        float3 r10 = Correct(img, bT, bE, gain);
        float3 added = GlowAdd(r10, bT, bE, px) - r10;
        return float4(PanelNitsToScRGB(min(added * FALD_GLOW_VIEW_SCALE, white)), 1.0f);
    }
    float3 req = Correct(img, bT, bE, gain);
    if (glowOn != 0u) req = GlowAdd(req, bT, bE, px);   // the glow fill (S2): added to the corrected request
    return float4(PanelNitsToScRGB(req), src.a);
}
)";
