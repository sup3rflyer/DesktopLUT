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
//   CS stat  (round 0): per cell, area statistic over every pixel -> drive texture (cols x rows)
//   CS temporal (opt.): per cell, first-order drive STATE (rise/fall time constants) -> the drive the kernels see
//   CS conv           : drives (x) K_true, drives (x) K_est on the sub-cell grid (cols*sub x rows*sub)
//   CS stat  (round 1): same statistic on the CORRECTED frame (the correction moves the drives)
//   CS temporal (opt.): again from the committed state; the result is committed after this round
//   CS conv           : final backlight fields
//   PS                : per pixel: req = (img + ped_ref - ped) * one scale (gain; soft knee toward the ceiling)
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
    float lumFadeLo; float lumFadeHi; float _pad3; float _pad4;     // pixel-luminance fade, as-if-white nits (lo = hi = 0: off)
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
Texture2D<float>  driveEstTex : register(t10); // conv: the drive map the ESTIMATE kernel sees (= driveTex unless tempMode 2);
                                               // pixel pass view 7: the filtered drive (driveTex = the instantaneous one)
Texture2D<float>  stateTex    : register(t11); // temporal pass: the committed drive state of the previous frame
SamplerState linearClamp : register(s0);

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
)";

// Pass 1: per-cell area statistic -> drive. One thread group per cell, 256 threads sweep the block.
inline const char* g_faldStatSource = R"(
RWTexture2D<float> driveOut : register(u0);
groupshared float gMax[256];
groupshared float gSum[256];

[numthreads(256, 1, 1)]
void main(uint3 gid : SV_GroupID, uint3 tid : SV_GroupThreadID) {
    uint cx = gid.x, cy = gid.y;
    uint n = cellW * cellH;
    float m = 0.0f, sum = 0.0f;
    for (uint k = tid.x; k < n; k += 256) {
        uint px = originX + cx * cellW + (k % cellW);
        uint py = originY + cy * cellH + (k / cellW);
        if (px >= frameW || py >= frameH) continue;
        float3 img = PanelNits(frameTex.Load(int3(px, py, 0)).rgb);
        if (roundIdx == 1) {
            float bT, bE; SampleFields(float2((float)px, (float)py), bT, bE);
            float g = gainTex.SampleLevel(linearClamp, FineUV(float2((float)px, (float)py)), 0);
            img = Correct(img, bT, bE, g);
        }
        float s = min(max(img.r, max(img.g, img.b)), white);
        if (s > driveFloor) { m = max(m, s); sum += s; }
    }
    gMax[tid.x] = m; gSum[tid.x] = sum;
    GroupMemoryBarrierWithGroupSync();
    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (tid.x < stride) {
            gMax[tid.x] = max(gMax[tid.x], gMax[tid.x + stride]);
            gSum[tid.x] += gSum[tid.x + stride];
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0) {
        float stat = min(gMax[0], gSum[0] / area0);   // min(brightest lit px, sum lit nits*px^2 / A0)
        driveOut[uint2(cx, cy)] = DriveOf(stat);
    }
}
)";

// Pass 2: the two backlight fields on the sub-cell grid. out[k] = sum_i d[k - i] * kern[i]
// (scipy fftconvolve 'same', odd kernels, zero outside the lattice), kernel chosen by sub-offset.
inline const char* g_faldConvSource = R"(
RWTexture2D<float> bTrueOut : register(u0);
RWTexture2D<float> bEstOut  : register(u1);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    uint fx = id.x, fy = id.y;
    if (fx >= cols * sub || fy >= rows * sub) return;
    int cx = (int)(fx / sub), cy = (int)(fy / sub);
    uint so = (fy % sub) * sub + (fx % sub);

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
    bTrueOut[uint2(fx, fy)] = accT;
    bEstOut[uint2(fx, fy)] = accE;
}
)";

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
inline const char* g_faldPixelSource = R"(
struct PS_INPUT { float4 pos : SV_POSITION; float2 uv : TEXCOORD0; };

float4 main(PS_INPUT i) : SV_Target {
    int2 px = int2(i.pos.xy);
    float4 src = frameTex.Load(int3(px, 0));
    if (!InLattice(px) || debugMode == 4) return src;
    float3 img = PanelNits(src.rgb);
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
    if (debugMode == 5 || debugMode == 6) {
        float maxc = max(img.r, max(img.g, img.b));
        float s = min(maxc, white);
        float3 t1 = PedestalTerm(img, s, max(bT, 0.0f), bE, maxc, 1u);   // per-channel, as applied (gain + fades)
        float3 t0 = PedestalTerm(img, s, max(bT, 0.0f), bE, maxc, 0u);   // white
        float3 shown = (debugMode == 5) ? abs((pedMode == 1) ? t1 : t0) : abs(t1 - t0);
        return float4(PanelNitsToScRGB(min(shown * 100.0f, white)), 1.0f);   // x100 nits per nit
    }
    float3 req = Correct(img, bT, bE, gain);
    return float4(PanelNitsToScRGB(req), src.a);
}
)";
