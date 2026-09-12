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
// Passes per frame (HDR overlay path, on the processed frame, after tonemap/LUT/WB):
//   CS stat  (round 0): per cell, area statistic over every pixel -> drive texture (cols x rows)
//   CS conv           : drives (x) K_true, drives (x) K_est on the sub-cell grid (cols*sub x rows*sub)
//   CS stat  (round 1): same statistic on the CORRECTED frame (the correction moves the drives)
//   CS conv           : final backlight fields
//   PS                : per pixel: req = (img + ped_ref - ped) * B_est/B_true, clamped, highlights kept
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
    float fadeLo; float fadeHi; float gainSmoothFine; float _pad2;  // gainSmoothFine: Gaussian sigma in fine samples (0 = off)
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
SamplerState linearClamp : register(s0);

static const float3x3 BT709_TO_BT2020 = float3x3(
    0.6274040f, 0.3292820f, 0.0433136f,
    0.0690970f, 0.9195400f, 0.0113612f,
    0.0163916f, 0.0880132f, 0.8955950f);
static const float3x3 BT2020_TO_BT709 = float3x3(
    1.6604910f, -0.5876411f, -0.0728499f,
   -0.1245505f,  1.1328999f, -0.0083494f,
   -0.0181508f, -0.1005789f,  1.1187297f);

// Per-channel "as-if-white" nits of the panel-bound signal: the PQ code the panel receives for a
// channel decodes to rec2020_c * 80 nits (Windows composes scRGB -> BT.2020 PQ). The physical
// contribution of channel c is w_c times this; the model works in as-if-white units throughout.
float3 PanelNits(float3 scrgb) {
    float3 rec2020 = mul(BT709_TO_BT2020, scrgb);
    return max(rec2020, 0.0f) * 80.0f;
}
float3 PanelNitsToScRGB(float3 nits) {
    return mul(BT2020_TO_BT709, nits / 80.0f);
}

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

// correct.py::correct_image for one pixel. img = as-if-white nits per channel (original frame);
// gain = the (smoothed) gain sampled at the pixel.
float3 Correct(float3 img, float bTrue, float bEst, float gain) {
    float s = min(max(img.r, max(img.g, img.b)), white);
    bTrue = max(bTrue, 0.0f);
    // deep-dark fade: the model is trusted only where the panel's estimate is not ~zero
    float wfade = smoothstep(fadeLo, fadeHi, bEst);
    float pedRef = white * DriveOf(s) * tmin;      // pedestal a uniform field of this level carries
    float ped = white * bTrue * tmin;              // pedestal in THIS context (as-if-white)
    // Hue-preserving pedestal term: subtract the same amount from all channels, limited by the
    // darkest one (a per-channel clip zeroes R/G and leaves B -> blue rims on dark edges).
    float delta = pedRef - ped;
    float darkest = min(img.r, min(img.g, img.b));
    float adj = ((delta < 0.0f) ? -min(-delta, darkest) : delta) * wfade;
    float3 req = max((img + adj) * gain, 0.0f);
    if (wfade < 1.0f) return req;                  // ceiling rule only where the model is trusted
    float cap = white * max(bEst, 1e-9f);          // LCD cannot open past 100 %
    float3 keep = max(img, cap);                   // saturated highlight: keep the original request
    return float3(req.r > cap ? keep.r : req.r, req.g > cap ? keep.g : req.g, req.b > cap ? keep.b : req.b);
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
            accE += driveTex.Load(int3(sx, sy, 0)) * kEst[(so * He + (uint)(j2 + (int)reachEstR)) * We + (uint)(i2 + (int)reachEstC)];
        }
    }
    bTrueOut[uint2(fx, fy)] = accT;
    bEstOut[uint2(fx, fy)] = accE;
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
// debugMode 1 = visualise gain-1 (grey 0.5 = no change, +-25 % full scale), 2 = B_true, 3 = B_est,
// 4 = identity passthrough (the layer runs its passes but outputs the source: isolates the overlay path itself).
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
        float v = saturate(0.5f + 2.0f * (gain - 1.0f)) * (100.0f / 80.0f);     // 100-nit grey ramp, +-25 % full scale
        return float4(v, v, v, 1.0f);
    }
    if (debugMode == 2) { float v = saturate(bT) * (100.0f / 80.0f); return float4(v, v, v, 1.0f); }
    if (debugMode == 3) { float v = saturate(bE) * (100.0f / 80.0f); return float4(v, v, v, 1.0f); }
    float3 req = Correct(img, bT, bE, gain);
    return float4(PanelNitsToScRGB(req), src.a);
}
)";
