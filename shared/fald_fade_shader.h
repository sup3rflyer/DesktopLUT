// The frozen fade stack F12B_V8d — HLSL: ASF10 + T11_TMTC (frozen by the owner 2026-10-19: "freeze without the floor fix")
// + round 12a's line fixes A (FADE_CL_PAD: the CL closing on the zero-padded map) and B16 (the lit floor's sigma-16
// Gaussian), FREEZE 2026-10-20 F12A_B16 (r12a/PORT_DELTA.md; twin r12a/cands12a.py F12A_B16), + round 12b's halo fix V8d
// (FADE_FV_*: the floor-valley gate V on the flank term, raw = G4(maxf25(raw0 (1 - (1 - w) V)))), FREEZE 2026-10-21
// F12B_V8d (r12b/PORT_DELTA.md; twin r12b/cands12b.py F12B_V8d).
//
// What it is: a field `fade` in [0, 1] per pixel that pulls the FALD layer's correction back toward "layer off" on smooth
// steep flanks (the ASF9o fade: flank term, robust thin-step protection, push-pull + hybrid fill, closing CL, cone), low-
// passed over content frames along the content's motion with a cut test (T11_TMTC), and kept off on lit floors outside the
// soft knee's reach where the correction brightens (the outer-flank keep OF). It is applied after Correct in the round-1
// statistic and in the pixel pass (fald_shader.h FadePost): req <- req * exp(ls (1 - fade_r) - ls), ls = ln(max req /
// max img), fade_r = fade_T (1 - keep). The rule is DLC results/fald_nearfield_2026-10-17/r11/PORT_PLAN.md section 1;
// the reference is the float64 study twin named there; DLC dlc/fald/fadeemu.py is the float32 twin of THIS file, operation
// for operation (it is the gate: tests/test_fald_fade.cpp dumps, DLC tests/test_fald_fade_gate2.py replays).
//
// Numerics (fadeemu.py's docstring has the why): every pass here is compiled with D3DCOMPILE_IEEE_STRICTNESS and keeps
// its float locals `precise`, so nothing is fused or reordered; long sums are Kahan-compensated (FadeKAdd); min / max /
// clamp are written as min(max(..)) — a NaN operand yields the other one (D3D's rule), which the twin follows; the knee-cap
// fraction is ln(a / K) / ln(g_eff) (well-conditioned in float32), the 1-D upsampling and the warps are explicit Loads
// with float32 weights (never the sampler's 8-bit fractions). Tables (Gaussian weights, DFT twiddles, the Hann window, the
// spectral Gaussian) come from the C++ (shared/fald_fade.cpp), computed in float64.
//
// Resources (the common FALD source, fald_shader.h, is compiled in front of every pass: b0 / t0..t22 / s0):
//   b1 FadeCB        per dispatch (immutable buffers built with the monitor's fade resources)
//   b2 FadeFrameCB   per content frame (hasHistory)
//   t24..t28 float inputs, t29 / t30 complex (float2) inputs, t31 / t32 twiddles (width / height), t33 Gaussian weights,
//   t34 the Hann window, t35 the spectral Gaussian; u0..u3 float outputs, u4 / u5 complex outputs, u7 the flags buffer.
#pragma once

// MSVC caps one string literal at 16380 bytes (C2026): each pass is its own literal; the common fade source too.
inline const char* g_faldFadeCommonSource = R"(
cbuffer FadeCB : register(b1) {
    uint fInW; uint fInH; uint fOutW; uint fOutH;      // the dispatch's input / output sizes
    uint fRadius; uint fDir; uint fFactor; uint fOp;   // separable radius, 0 = vertical (axis 0) / 1 = horizontal, down /
                                                       // up factor, the pass's variant bits
    uint fWOff; uint fN; uint fPad0; uint fPad1;       // Gaussian weights offset in t33; a count (reductions)
    float fC0; float fC1; float fC2; float fC3;        // the pass's constants
};
cbuffer FadeFrameCB : register(b2) {
    uint fHasHistory; uint fFramePad0; uint fFramePad1; uint fFramePad2;   // T: 0 = the first content frame after a reset
};
Texture2D<float>  fIn0 : register(t24);
Texture2D<float>  fIn1 : register(t25);
Texture2D<float>  fIn2 : register(t26);
Texture2D<float>  fIn3 : register(t27);
Texture2D<float>  fIn4 : register(t28);
Texture2D<float2> fInC0 : register(t29);
Texture2D<float2> fInC1 : register(t30);
Buffer<float2>    fTwW : register(t31);          // (cos, sin)(2 pi j / W8), j = 0 .. W8 - 1
Buffer<float2>    fTwH : register(t32);          // ... / H8
Buffer<float>     fGW : register(t33);           // half Gaussian kernels w[0..r], all sigmas, offsets in fWOff
Texture2D<float>  fHann : register(t34);         // W8 x H8: the twin's non-periodic 2-D Hann window
Texture2D<float>  fSpecG : register(t35);        // W8 x H8: exp(-2 pi^2 3^2 (fy^2 + fx^2)), fftfreq units
RWStructuredBuffer<uint> fFlags : register(u7);  // T bookkeeping, FADE_FLAG_* below (asuint / asfloat)

// the T flags (fFlags words)
static const uint FADE_FLAG_ADVANCE = 0;   // 1 = this content frame's 1/8-res map differs from the previous (any bit)
static const uint FADE_FLAG_PC_DY = 1;     // raw phase-correlation shift (float)
static const uint FADE_FLAG_PC_DX = 2;
static const uint FADE_FLAG_PC_PEAK = 3;   // correlation peak value (float)
static const uint FADE_FLAG_MAXMAG = 4;    // max |cross power| (float)
static const uint FADE_FLAG_RES0 = 5;      // mean |l8 - l8Prev| over the overlap (float)
static const uint FADE_FLAG_RES1 = 6;      // ... with the shift (float; +inf when not formed)
static const uint FADE_FLAG_USED = 7;      // 1 = the shift is accepted
static const uint FADE_FLAG_DY = 8;        // the shift in use (float; 0 when rejected)
static const uint FADE_FLAG_DX = 9;
static const uint FADE_FLAG_FRAC = 10;     // TC: share of overlap texels with |l8 - warp(l8Prev)| > 0.5 (float)
static const uint FADE_FLAG_EMD = 11;      // TC: histogram EMD, ln units (float)
static const uint FADE_FLAG_CUT = 12;      // 1 = a cut
static const uint FADE_FLAG_PEAK_IDX = 13; // the correlation argmax (row-major)
static const uint FADE_FLAGS = 16;

// frozen constants (PORT_PLAN 1.4; = DLC fadeemu.py)
static const float FADE_LOG_FLOOR = 0.05f;
static const float FADE_LAM = 80.0f;
static const float FADE_RS_K_LO = 2.5f, FADE_RS_K_HI = 4.0f, FADE_RS_CAP = 0.02f, FADE_RS_LO = 0.01f, FADE_RS_HI = 0.03f;
static const float FADE_RS_RATIO_LO = 1.4f, FADE_RS_RATIO_HI = 1.9f;
static const float FADE_CARE_LO = 0.3f, FADE_CARE_HI = 0.6f;
static const float FADE_PP_CONF = 4.0f;
static const float FADE_HYB_W0 = 0.25f;
static const float FADE_CL_TH_LO = 0.7f, FADE_CL_TH_HI = 1.5f;
static const uint FADE_CL_PAD = 80u;   // A (F12A_B16): the CL closing's zero padding = 2 x its radius 40 (1/8-res texels)
// V8d (F12B_V8d): the floor-valley gate's thresholds, float32 of ln 1.5 / ln 2 / ln 3 (0x3ECF991F / 0x3F317218 /
// 0x3F8C9F54), and the darkest-object test's soft offset (ln units); its radii are the C++'s (fald_fade.cpp kFv*)
static const float FADE_FV_LN15 = 0.405465096f, FADE_FV_LN2 = 0.693147182f, FADE_FV_LN3 = 1.09861231f;
static const float FADE_FV_DARK = 20.0f;
static const float FADE_OF_LIT_LO = 1.0f, FADE_OF_LIT_HI = 3.0f, FADE_OF_POS_HI = 0.03f;
static const float FADE_T_EPS = 0.02f, FADE_T_A0 = 0.25f, FADE_T_R_LO = 0.05f, FADE_T_R_HI = 0.2f;
static const float FADE_T_CD = 0.5f, FADE_T_CFRAC = 0.15f, FADE_T_CH = 0.2f;
static const float FADE_KC_EPS = 1e-6f;

// One Kahan step: y = x - c; t = s + y; c = (t - s) - y; s = t.
void FadeKAdd(inout float s, inout float c, float x) {
    precise float y = x - c;
    precise float t = s + y;
    precise float cc = (t - s) - y;
    c = cc;
    s = t;
}
// the twin's smooth_t: t = clamp((x - a) / (b - a), 0, 1); t t (3 - 2 t)
float FadeSmooth(float a, float b, float x) {
    precise float t = saturate((x - a) / (b - a));
    precise float r = t * t * (3.0f - 2.0f * t);
    return r;
}
// log(1 + x) for x >= 0, accurate in float32 (Goldberg): u = 1 + x; u == 1 ? x : log(u) x / (u - 1)
float FadeLog1p(float x) {
    precise float u = 1.0f + x;
    precise float den = u - 1.0f;
    precise float q = log(u) * x / den;
    return (u == 1.0f) ? x : q;
}
float FadeLd0(int x, int y) { return fIn0.Load(int3(x, y, 0)); }
// Catmull-Rom weights for the fractional part f (tl11._taps): ((-f^3 + 2f^2) - f) / 2, ((3f^3 - 5f^2) + 2) / 2, ...
void FadeCubicW(float d, out int fl, out float w0, out float w1, out float w2, out float w3) {
    precise float p0 = -d;
    precise float flf = floor(p0);
    precise float f = p0 - flf;
    precise float f2 = f * f;
    precise float f3 = f2 * f;
    fl = (int)flf;
    precise float a0 = ((-f3 + 2.0f * f2) - f) * 0.5f;
    precise float a1 = ((3.0f * f3 - 5.0f * f2) + 2.0f) * 0.5f;
    precise float a2 = ((-3.0f * f3 + 4.0f * f2) + f) * 0.5f;
    precise float a3 = (f3 - f2) * 0.5f;
    w0 = a0; w1 = a1; w2 = a2; w3 = a3;
}
// source position i - d inside [0, n - 1] (the twin's +-1e-9 is below float32 resolution there)
bool FadeWarpValid(int i, float d, int n) {
    precise float pos = (float)i + (-d);
    return pos >= -1e-9f && pos <= (float)(n - 1) + 1e-9f;
}
)";

// ---- the separable Gaussian (scipy mode 'nearest', truncate 4): one axis per dispatch, Kahan sum of the symmetric
// pairs in ascending distance (the twin's _corr1d order). fOp bit 0 = clamp the INPUT into [0, 1e4] (fade9's sanitise:
// the max channel of the frame), bit 1 = saturate the OUTPUT (step / reach).
inline const char* g_faldFadeGaussSource = R"(
RWTexture2D<float> gOut : register(u0);
float GTap(int x, int y, int k) {
    float v;
    if (fDir == 0u) v = fIn0.Load(int3(x, clamp(k, 0, (int)fInH - 1), 0));
    else v = fIn0.Load(int3(clamp(k, 0, (int)fInW - 1), y, 0));
    if ((fOp & 1u) != 0u) v = min(max(v, 0.0f), 1e4f);
    return v;
}
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fInW || id.y >= fInH) return;
    int x = (int)id.x, y = (int)id.y, r = (int)fRadius;
    int c = (fDir == 0u) ? y : x;
    precise float s = GTap(x, y, c) * fGW[fWOff];
    precise float comp = 0.0f;
    for (int j = 1; j <= r; j++) {
        precise float term = (GTap(x, y, c - j) + GTap(x, y, c + j)) * fGW[fWOff + (uint)j];
        FadeKAdd(s, comp, term);
    }
    if ((fOp & 2u) != 0u) s = saturate(s);
    gOut[id.xy] = s;
}
)";

// ---- separable max (fOp 0) / min (fOp 1) filter of radius fRadius along one axis, replicate edges.
inline const char* g_faldFadeExtSource = R"(
RWTexture2D<float> eOut : register(u0);
float ETap(int x, int y, int k) {
    if (fDir == 0u) return fIn0.Load(int3(x, clamp(k, 0, (int)fInH - 1), 0));
    return fIn0.Load(int3(clamp(k, 0, (int)fInW - 1), y, 0));
}
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fInW || id.y >= fInH) return;
    int x = (int)id.x, y = (int)id.y, r = (int)fRadius;
    int c = (fDir == 0u) ? y : x;
    float m = ETap(x, y, c - r);
    for (int k = -r + 1; k <= r; k++) {
        float v = ETap(x, y, c + k);
        m = (fOp == 0u) ? max(m, v) : min(m, v);
    }
    eOut[id.xy] = m;
}
)";

// ---- |grad| = hypot of np.gradient (central x 0.5 inside, one-sided at the borders), as sqrt(gy^2 + gx^2).
inline const char* g_faldFadeGradSource = R"(
RWTexture2D<float> grOut : register(u0);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fInW || id.y >= fInH) return;
    int x = (int)id.x, y = (int)id.y, W = (int)fInW, H = (int)fInH;
    precise float gy, gx;
    if (y == 0) gy = FadeLd0(x, 1) - FadeLd0(x, 0);
    else if (y == H - 1) gy = FadeLd0(x, H - 1) - FadeLd0(x, H - 2);
    else gy = (FadeLd0(x, y + 1) - FadeLd0(x, y - 1)) * 0.5f;
    if (x == 0) gx = FadeLd0(1, y) - FadeLd0(0, y);
    else if (x == W - 1) gx = FadeLd0(W - 1, y) - FadeLd0(W - 2, y);
    else gx = (FadeLd0(x + 1, y) - FadeLd0(x - 1, y)) * 0.5f;
    precise float g = sqrt(gy * gy + gx * gx);
    grOut[id.xy] = g;
}
)";

// ---- f x f mean (edge replicate): row-major sum, x fC0 (= 1 / f^2). fOp bit 0 = clamp the input into [0, 1e4]; bit 1 =
// the f x f block MIN instead (the twin's replicate-padded -max_pool2d(-x): V8d's min8; the same indexing, row-major).
inline const char* g_faldFadeDownSource = R"(
RWTexture2D<float> dOut : register(u0);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fOutW || id.y >= fOutH) return;
    int f = (int)fFactor;
    if ((fOp & 2u) != 0u) {
        float mn = 0.0f;
        for (int my = 0; my < f; my++) {
            int sy = min((int)id.y * f + my, (int)fInH - 1);
            for (int mx = 0; mx < f; mx++) {
                float v = fIn0.Load(int3(min((int)id.x * f + mx, (int)fInW - 1), sy, 0));
                if ((fOp & 1u) != 0u) v = min(max(v, 0.0f), 1e4f);
                mn = (my == 0 && mx == 0) ? v : min(mn, v);
            }
        }
        dOut[id.xy] = mn;
        return;
    }
    precise float s = 0.0f;
    for (int dy = 0; dy < f; dy++) {
        int sy = min((int)id.y * f + dy, (int)fInH - 1);
        for (int dx = 0; dx < f; dx++) {
            float v = fIn0.Load(int3(min((int)id.x * f + dx, (int)fInW - 1), sy, 0));
            if ((fOp & 1u) != 0u) v = min(max(v, 0.0f), 1e4f);
            s = s + v;
        }
    }
    precise float m = s * fC0;
    dOut[id.xy] = m;
}
)";

// ---- f x f block MAX (the twin's replicate-padded max_pool2d) — the knee-cap fraction's quarter-res map.
inline const char* g_faldFadeBlockMaxSource = R"(
RWTexture2D<float> bmOut : register(u0);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fOutW || id.y >= fOutH) return;
    int f = (int)fFactor;
    float m = fIn0.Load(int3(min((int)id.x * f, (int)fInW - 1), min((int)id.y * f, (int)fInH - 1), 0));
    for (int dy = 0; dy < f; dy++)
        for (int dx = 0; dx < f; dx++)
            m = max(m, fIn0.Load(int3(min((int)id.x * f + dx, (int)fInW - 1), min((int)id.y * f + dy, (int)fInH - 1), 0)));
    bmOut[id.xy] = m;
}
)";

// ---- bilinear upsample by fFactor with texel-centre alignment t = (i + 0.5) / f - 0.5 (fC1 = 1 / f), the edge texel
// held outside; rows (y) first, then x; four Loads, float32 weights. fOp bit 0 = exp() of the result (the lit floor F).
inline const char* g_faldFadeUpSource = R"(
RWTexture2D<float> uOut : register(u0);
void UpAxis(int i, int nIn, out int i0, out int i1, out float fr) {
    precise float t = ((float)i + 0.5f) * fC1 - 0.5f;
    precise float fl = floor(t);
    i0 = clamp((int)fl, 0, nIn - 1);
    i1 = clamp(i0 + 1, 0, nIn - 1);
    precise float r = min(max(t - fl, 0.0f), 1.0f);
    if (t < 0.0f) r = 0.0f;
    else if (t > (float)(nIn - 1)) r = 1.0f;
    fr = r;
}
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fOutW || id.y >= fOutH) return;
    int y0, y1, x0, x1; float fy, fx;
    UpAxis((int)id.y, (int)fInH, y0, y1, fy);
    UpAxis((int)id.x, (int)fInW, x0, x1, fx);
    precise float ofy = 1.0f - fy;
    precise float ofx = 1.0f - fx;
    precise float c0 = FadeLd0(x0, y0) * ofy + FadeLd0(x0, y1) * fy;
    precise float c1 = FadeLd0(x1, y0) * ofy + FadeLd0(x1, y1) * fy;
    precise float v = c0 * ofx + c1 * fx;
    if ((fOp & 1u) != 0u) v = exp(v);
    uOut[id.xy] = v;
}
)";

// ---- per-pixel steps of the ASF9o field (fade9_t), one entry per fOp:
//   0 MI     t0 frame -> u0 mi = max channel of Balance(PanelNits(frame)) (unsanitised), u1 lv = ln max(clamp(mi, 0,
//            1e4), 0.05)
//   1 RAW0   t24 gs, t25 gl -> u0 1 - 1 / (1 + (80 min(gs, gl))^2)
//   2 THIN   t24 g1, t25 g2, t26 minf17(G3(g1)) -> u0 the robust thin-step flag
//   3 W      t24 G4(maxf65(thin)) (saturated) -> u0 w = 1 - step
//   4 CARE   t24 G2(mi) -> u0 smooth(0.3, 0.6, .)
//   5 PP0    t24 raw, t25 w, t26 care -> u0 S0 = v clamp(ws, 0, 1), u1 W0 = clamp(ws, 0, 1) + fC0 (prior), u2 vsum = raw ws,
//            u3 ws
//   6 DIV    t24 S, t25 W -> u0 S / max(W, 1e-12) (the push-pull's top level)
//   7 BLEND  t24 S, t25 W, t26 Eu -> u0 a S / max(W, 1e-12) + (1 - a) Eu, a = clamp(4 W, 0, 1)
//   8 HYB    t24 num, t25 den, t26 F_pp, t27 w, t28 raw -> u0 fade = clamp(w raw + (1 - w) F, 0, 1)
//   9 LOG    t24 -> u0 ln max(x, 0.05)
//  10 LOGEPS t24 -> u0 ln(x + 0.02)
//  11 CLGATE t24 l8, t25 its opening -> u0 smooth(0.7, 1.5, l8 - open)
//  12 CLADD8 t24 fc (the PADDED closing: read at +FADE_CL_PAD), t25 f8, t26 gate -> u0 max(fc - f8, 0) gate
//  13 ADDF   u1 fade (in place), t24 add -> clamp(fade + add (1 - fade), 0, 1)
//  14 CONE   u1 cone (in place), t24 maxf3(cone): cone = max(cone, mf - fC0)
//  15 CONEF  u1 cone (in place), t24 maxf3(cone), t25 dec: cone = max(cone, mf - dec)
//  16 CONEAP u1 fade (in place), t24 w, t25 cu: clamp(fade + w max(cu - fade, 0), 0, 1)
//  17 DEC    t24 grad -> u0 fC0 + g / 2 (knee_reach's per-texel step, fC0 = 4 / 96)
//  18 SUB    t24 a, t25 b -> u0 a - b
//  19 TOUT   t24 fade, t25 up4(s - fq) -> u0 clamp(fade + d, 0, 1)
//  20 PAD    t24 f8 -> u0 f8 zero-padded by FADE_CL_PAD on every side (dispatched over the padded domain; A)
// V8d, the floor-valley gate (r12b/cands12b.py floor_valley8 + fade12b; 1/8 res unless noted):
//  21 FVV    t24 Lc = grey_close(l8, 33), t25 lmin8, t26 grey_open(Lc, 133) -> u0 v = (smooth(ln 1.5, ln 2, Lc - lmin8)
//            smooth(0, ln 3, Lc)) (1 - smooth(ln 1.5, ln 3, Lc - open))
//  22 FVLEV  t24 lmin8, t25 v -> u0 lmin8 + (1 - v) 20 (the object level; its minf 11 follows)
//  23 FVGATE t24 Vd = maxf11(v), t25 vlev = minf11(FVLEV), t26 minf11(lmin8) -> u0 V8 = Vd (1 - smooth(ln 1.5, ln 2,
//            vlev - dmin))
//  24 FVSRC  full res: t24 raw0, t25 w, t26 up8(V8) -> u0 raw0 (1 - (1 - w) clamp(V, 0, 1)) (the flank term's source)
inline const char* g_faldFadePixSource = R"(
RWTexture2D<float> pOut0 : register(u0);
RWTexture2D<float> pOut1 : register(u1);
RWTexture2D<float> pOut2 : register(u2);
RWTexture2D<float> pOut3 : register(u3);
float L(Texture2D<float> t, uint2 p) { return t.Load(int3(p, 0)); }
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fOutW || id.y >= fOutH) return;
    uint2 p = id.xy;
    if (fOp == 0u) {
        float3 img = PanelNits(frameTex.Load(int3(p, 0)).rgb);
        if (starOn != 0u && InLattice(int2(p))) img = Balance(img, int2(p));   // the frame the layer works on
        precise float mi = max(img.r, max(img.g, img.b));
        precise float ms = min(max(mi, 0.0f), 1e4f);
        precise float lv = log(max(ms, FADE_LOG_FLOOR));
        pOut0[p] = mi; pOut1[p] = lv;
    } else if (fOp == 1u) {
        precise float y = FADE_LAM * min(L(fIn0, p), L(fIn1, p));
        precise float r = 1.0f - 1.0f / (1.0f + y * y);
        pOut0[p] = r;
    } else if (fOp == 2u) {
        precise float g1 = L(fIn0, p), g2 = L(fIn1, p);
        precise float nf = min(L(fIn2, p), FADE_RS_CAP);
        precise float lo = max(FADE_RS_K_LO * nf, FADE_RS_LO);
        precise float hi = max(FADE_RS_K_HI * nf, FADE_RS_HI);
        precise float t = min(max((g1 - lo) / max(hi - lo, 1e-12f), 0.0f), 1.0f);
        precise float gate = t * t * (3.0f - 2.0f * t);
        precise float ratio = g1 / max(g2, 1e-9f);
        precise float thin = FadeSmooth(FADE_RS_RATIO_LO, FADE_RS_RATIO_HI, ratio) * gate;
        pOut0[p] = thin;
    } else if (fOp == 3u) {
        precise float w = 1.0f - L(fIn0, p);
        pOut0[p] = w;
    } else if (fOp == 4u) {
        pOut0[p] = FadeSmooth(FADE_CARE_LO, FADE_CARE_HI, L(fIn0, p));
    } else if (fOp == 5u) {
        precise float ws = L(fIn1, p) * L(fIn2, p);
        precise float vsum = L(fIn0, p) * ws;
        precise float v = (vsum / max(ws, 1e-12f)) * ((ws > 1e-9f) ? 1.0f : 0.0f);
        precise float wg = min(max(ws, 0.0f), 1.0f);
        precise float s0 = v * wg;
        precise float w0 = wg + fC0;
        pOut0[p] = s0; pOut1[p] = w0; pOut2[p] = vsum; pOut3[p] = ws;
    } else if (fOp == 6u) {
        precise float e = L(fIn0, p) / max(L(fIn1, p), 1e-12f);
        pOut0[p] = e;
    } else if (fOp == 7u) {
        precise float w = L(fIn1, p);
        precise float a = min(max(w * FADE_PP_CONF, 0.0f), 1.0f);
        precise float el = L(fIn0, p) / max(w, 1e-12f);
        precise float e = a * el + (1.0f - a) * L(fIn2, p);
        pOut0[p] = e;
    } else if (fOp == 8u) {
        precise float den = L(fIn1, p);
        precise float a = min(max(den / FADE_HYB_W0, 0.0f), 1.0f);
        precise float F = a * L(fIn0, p) / max(den, 1e-9f) + (1.0f - a) * L(fIn2, p);
        precise float w = L(fIn3, p);
        precise float fade = min(max(w * L(fIn4, p) + (1.0f - w) * F, 0.0f), 1.0f);
        pOut0[p] = fade;
    } else if (fOp == 9u) {
        pOut0[p] = log(max(L(fIn0, p), FADE_LOG_FLOOR));
    } else if (fOp == 10u) {
        precise float v = L(fIn0, p) + FADE_T_EPS;
        pOut0[p] = log(v);
    } else if (fOp == 11u) {
        precise float th = L(fIn0, p) - L(fIn1, p);
        pOut0[p] = FadeSmooth(FADE_CL_TH_LO, FADE_CL_TH_HI, th);
    } else if (fOp == 12u) {
        precise float a = max(L(fIn0, p + uint2(FADE_CL_PAD, FADE_CL_PAD)) - L(fIn1, p), 0.0f) * L(fIn2, p);
        pOut0[p] = a;
    } else if (fOp == 13u) {
        precise float f = pOut1[p];
        precise float v = min(max(f + L(fIn0, p) * (1.0f - f), 0.0f), 1.0f);
        pOut1[p] = v;
    } else if (fOp == 14u || fOp == 15u) {
        precise float dec = (fOp == 14u) ? fC0 : L(fIn1, p);
        precise float mf = L(fIn0, p) - dec;
        pOut1[p] = max(pOut1[p], mf);
    } else if (fOp == 16u) {
        precise float f = pOut1[p];
        precise float v = f + L(fIn0, p) * max(L(fIn1, p) - f, 0.0f);
        pOut1[p] = min(max(v, 0.0f), 1.0f);
    } else if (fOp == 17u) {
        precise float d = fC0 + L(fIn0, p) / 2.0f;
        pOut0[p] = d;
    } else if (fOp == 18u) {
        precise float d = L(fIn0, p) - L(fIn1, p);
        pOut0[p] = d;
    } else if (fOp == 19u) {
        precise float v = min(max(L(fIn0, p) + L(fIn1, p), 0.0f), 1.0f);
        pOut0[p] = v;
    } else if (fOp == 20u) {
        uint w8, h8;
        fIn0.GetDimensions(w8, h8);
        int2 q = int2(p) - (int)FADE_CL_PAD;
        bool inside = q.x >= 0 && q.y >= 0 && q.x < (int)w8 && q.y < (int)h8;
        pOut0[p] = inside ? L(fIn0, uint2(q)) : 0.0f;
    } else if (fOp == 21u) {
        precise float lc = L(fIn0, p);
        precise float v = FadeSmooth(FADE_FV_LN15, FADE_FV_LN2, lc - L(fIn1, p)) * FadeSmooth(0.0f, FADE_FV_LN3, lc)
                          * (1.0f - FadeSmooth(FADE_FV_LN15, FADE_FV_LN3, lc - L(fIn2, p)));
        pOut0[p] = v;
    } else if (fOp == 22u) {
        precise float lev = L(fIn0, p) + (1.0f - L(fIn1, p)) * FADE_FV_DARK;
        pOut0[p] = lev;
    } else if (fOp == 23u) {
        precise float gap = L(fIn1, p) - L(fIn2, p);
        precise float v8 = L(fIn0, p) * (1.0f - FadeSmooth(FADE_FV_LN15, FADE_FV_LN2, gap));
        pOut0[p] = v8;
    } else if (fOp == 24u) {
        precise float V = min(max(L(fIn2, p), 0.0f), 1.0f);
        precise float s = L(fIn0, p) * (1.0f - (1.0f - L(fIn1, p)) * V);
        pOut0[p] = s;
    }
}
)";

// ---- the knee-cap fraction (OF, per round; TEmu.correct_tex's kc) as the 4 x 4 block MAX at quarter res, from the
// arithmetic of Correct (fald_shader.h) with the round's fields bound at t5 / t6 / t9:
//   g_eff = 1 + d, d = (gain - 1) smoothstep(lumLo, lumHi, max img) (the lum-faded gain); u = img + PedestalTerm; m = max u;
//   C = min(white, white max(gs.y, 1e-9) / CAP_TRUST); a = m g_eff;
//   kc = 0 unless d > 1e-6 and m > 1e-9 and the knee binds (a > KNEE_START C); then 1 where it pulls below the original
//   (K < m), else ln(a / K) / ln(g_eff) = log1p((1 - ks) t^2 / (ks + t)) / log1p(d), t = (a / C - ks) / (1 - ks) — the
//   twin's 1 - ln(max(ge, 1)) / ln(g_eff) with ge = max(m, K) / m, written so float32 keeps it as g_eff -> 1.
inline const char* g_faldFadeKneeSource = R"(
RWTexture2D<float> kOut : register(u0);
float KneeCap(int2 px) {
    float3 img = PanelNits(frameTex.Load(int3(px, 0)).rgb);
    if (starOn != 0u && InLattice(px)) img = Balance(img, px);
    float bT, bE; SampleFields(float2(px), bT, bE);
    float2 gs = gainTex.SampleLevel(linearClamp, FineUV(float2(px)), 0);
    precise float gain = gs.x;
    precise float maxc = max(img.r, max(img.g, img.b));
    precise float s = min(maxc, white);
    bT = max(bT, 0.0f);
    precise float d = gain - 1.0f;
    if (lumFadeHi > lumFadeLo) d = (gain - 1.0f) * smoothstep(lumFadeLo, lumFadeHi, maxc);
    precise float gEff = 1.0f + d;
    precise float3 u = img + PedestalTerm(img, s, bT, bE, maxc, pedMode);
    precise float m = max(u.r, max(u.g, u.b));
    if (!(d > FADE_KC_EPS) || !(m > 1e-9f)) return 0.0f;
    precise float cap = white * max(gs.y, 1e-9f);
    precise float C = (FALD_KNEE_CAP_TRUST > 0.0f) ? min(white, cap / FALD_KNEE_CAP_TRUST) : white;
    precise float a = m * gEff;
    if (!(a > FALD_KNEE_START * C)) return 0.0f;
    precise float t = (a / C - FALD_KNEE_START) / (1.0f - FALD_KNEE_START);
    precise float K = C * (FALD_KNEE_START + (1.0f - FALD_KNEE_START) * t / (1.0f + t));
    if (K < m) return 1.0f;
    precise float num = FadeLog1p((1.0f - FALD_KNEE_START) * t * t / (FALD_KNEE_START + t));
    precise float kc = saturate(num / FadeLog1p(d));
    return kc;
}
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fOutW || id.y >= fOutH) return;
    float m = 0.0f;
    for (int dy = 0; dy < 4; dy++)
        for (int dx = 0; dx < 4; dx++) {
            int2 px = int2(min((int)id.x * 4 + dx, (int)frameW - 1), min((int)id.y * 4 + dy, (int)frameH - 1));
            float k = KneeCap(px);
            m = (dx == 0 && dy == 0) ? k : max(m, k);
        }
    kOut[id.xy] = m;
}
)";

// ---- T: the advance test (any texel of the 1/8-res map differs from the previous frame's, bit for bit). One group.
inline const char* g_faldFadeAdvanceSource = R"(
groupshared uint gAny[1024];
[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    uint n = fInW * fInH, any = 0u;
    for (uint i = tid.x; i < n; i += 1024u) {
        int2 q = int2(i % fInW, i / fInW);
        if (asuint(fIn0.Load(int3(q, 0))) != asuint(fIn1.Load(int3(q, 0)))) any = 1u;
    }
    gAny[tid.x] = any;
    GroupMemoryBarrierWithGroupSync();
    for (uint s = 512u; s > 0u; s >>= 1) {
        if (tid.x < s) gAny[tid.x] = gAny[tid.x] | gAny[tid.x + s];
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0u) fFlags[FADE_FLAG_ADVANCE] = (fHasHistory != 0u) ? gAny[0] : 1u;
}
)";

// ---- T: mean removal + Hann window of the 1/8-res map (the phase correlation's input). One group: the Kahan sum of
// the map (thread t: elements t, t + 1024, ...; then the tree), mean = sum / n, then every texel (l8 - mean) win.
inline const char* g_faldFadeMeanWinSource = R"(
RWTexture2D<float> mwOut : register(u0);
groupshared float gS[1024];
[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    uint n = fInW * fInH;
    precise float s = 0.0f, c = 0.0f;
    for (uint i = tid.x; i < n; i += 1024u) FadeKAdd(s, c, fIn0.Load(int3(i % fInW, i / fInW, 0)));
    gS[tid.x] = s;
    GroupMemoryBarrierWithGroupSync();
    for (uint st = 512u; st > 0u; st >>= 1) {
        if (tid.x < st) { precise float v = gS[tid.x] + gS[tid.x + st]; gS[tid.x] = v; }
        GroupMemoryBarrierWithGroupSync();
    }
    precise float mean = gS[0] / (float)n;
    for (uint j = tid.x; j < n; j += 1024u) {
        int2 q = int2(j % fInW, j / fInW);
        precise float v = (fIn0.Load(int3(q, 0)) - mean) * fHann.Load(int3(q, 0));
        mwOut[q] = v;
    }
}
)";

// ---- T: dense DFTs (sizes need not be powers of two). Kahan sums over the input index in ascending order.
//   fOp 0: forward along x of a REAL map (t24) -> u4 complex; 1: forward along y of a complex map (t29) -> u4;
//   2: inverse along x of a complex map (t29) -> u4; 3: inverse along y, real part x fC0 (= 1 / (W H)) -> u0.
inline const char* g_faldFadeDftSource = R"(
RWTexture2D<float> dfReal : register(u0);
RWTexture2D<float2> dfOut : register(u4);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    uint W = fInW, H = fInH;
    if (id.x >= W || id.y >= H) return;
    uint x = id.x, y = id.y;
    precise float re = 0.0f, cre = 0.0f, im = 0.0f, cim = 0.0f;
    if (fOp == 0u) {                       // X[y, k = x] = sum_n x[y, n] (cos - i sin)(2 pi k n / W)
        for (uint n = 0; n < W; n++) {
            float v = fIn0.Load(int3(n, y, 0));
            float2 tw = fTwW[(x * n) % W];
            precise float tr = v * tw.x;
            precise float ti = -(v * tw.y);
            FadeKAdd(re, cre, tr); FadeKAdd(im, cim, ti);
        }
        dfOut[id.xy] = float2(re, im);
    } else if (fOp == 1u) {                // B[ky = y, kx = x] = sum_m Z[m, kx] (cos - i sin)(2 pi ky m / H)
        for (uint m = 0; m < H; m++) {
            float2 z = fInC0.Load(int3(x, m, 0));
            float2 tw = fTwH[(y * m) % H];
            precise float tr = z.x * tw.x + z.y * tw.y;
            precise float ti = z.y * tw.x - z.x * tw.y;
            FadeKAdd(re, cre, tr); FadeKAdd(im, cim, ti);
        }
        dfOut[id.xy] = float2(re, im);
    } else if (fOp == 2u) {                // Z[ky = y, x] = sum_kx R[ky, kx] (cos + i sin)(2 pi kx x / W)
        for (uint k = 0; k < W; k++) {
            float2 r = fInC0.Load(int3(k, y, 0));
            float2 tw = fTwW[(k * x) % W];
            precise float tr = r.x * tw.x - r.y * tw.y;
            precise float ti = r.x * tw.y + r.y * tw.x;
            FadeKAdd(re, cre, tr); FadeKAdd(im, cim, ti);
        }
        dfOut[id.xy] = float2(re, im);
    } else {                               // r[y, x] = sum_ky Re(Z[ky, x] (cos + i sin)(2 pi ky y / H)) / (W H)
        for (uint k = 0; k < H; k++) {
            float2 z = fInC0.Load(int3(x, k, 0));
            float2 tw = fTwH[(k * y) % H];
            precise float tr = z.x * tw.x - z.y * tw.y;
            FadeKAdd(re, cre, tr);
        }
        precise float v = re * fC0;
        dfReal[id.xy] = v;
    }
}
)";

// ---- T: the normalised, Gaussian-weighted cross-power spectrum, three passes: R = B conj(A) (t29 B = this frame's
// spectrum, t30 A = the previous one) -> u4 R, u0 |R|; the max |R| (one group) -> flags; R / (|R| + 1e-12 max) x the
// spectral Gaussian (0 where the denominator is 0: the twin's + 1e-300 underflows in float32) -> u4.
inline const char* g_faldFadeCrossSource = R"(
RWTexture2D<float> crMag : register(u0);
RWTexture2D<float2> crOut : register(u4);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fInW || id.y >= fInH) return;
    float2 b = fInC0.Load(int3(id.xy, 0)), a = fInC1.Load(int3(id.xy, 0));
    precise float rr = b.x * a.x + b.y * a.y;
    precise float ri = b.y * a.x - b.x * a.y;
    precise float mag = sqrt(rr * rr + ri * ri);
    crOut[id.xy] = float2(rr, ri);
    crMag[id.xy] = mag;
}
)";
inline const char* g_faldFadeNormSource = R"(
RWTexture2D<float2> nmOut : register(u4);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fInW || id.y >= fInH) return;
    float2 r = fInC0.Load(int3(id.xy, 0));
    precise float den = fIn0.Load(int3(id.xy, 0)) + 1e-12f * asfloat(fFlags[FADE_FLAG_MAXMAG]);
    precise float g = fSpecG.Load(int3(id.xy, 0));
    precise float rr = (den > 0.0f) ? (r.x / den) * g : 0.0f;
    precise float ri = (den > 0.0f) ? (r.y / den) * g : 0.0f;
    nmOut[id.xy] = float2(rr, ri);
}
)";
inline const char* g_faldFadeMaxMagSource = R"(
groupshared float gMx[1024];
[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    uint n = fInW * fInH;
    float m = 0.0f;
    for (uint i = tid.x; i < n; i += 1024u) m = max(m, fIn0.Load(int3(i % fInW, i / fInW, 0)));
    gMx[tid.x] = m;
    GroupMemoryBarrierWithGroupSync();
    for (uint s = 512u; s > 0u; s >>= 1) {
        if (tid.x < s) gMx[tid.x] = max(gMx[tid.x], gMx[tid.x + s]);
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x == 0u) fFlags[FADE_FLAG_MAXMAG] = asuint(gMx[0]);
}
)";

// ---- T: the correlation peak (one group): argmax of r (t24; ties: the smallest row-major index, NaN never wins), then
// per axis the parabolic sub-texel fit 0.5 (m- - m+) / ((m- - 2c) + m+) (0 when |den| < 1e-15), clipped to +-0.5, the
// shift wrapped into [-N/2, N/2) (v in [0, 2N) here: v >= N ? v - N : v) -> flags PC_DY / PC_DX / PC_PEAK / PEAK_IDX.
inline const char* g_faldFadeArgmaxSource = R"(
groupshared float gV[1024];
groupshared uint gI[1024];
float Para(float m1, float c, float p1) {
    precise float den = (m1 - 2.0f * c) + p1;
    precise float o = (0.5f * (m1 - p1)) / den;
    return (abs(den) < 1e-15f) ? 0.0f : o;
}
float Wrap(float v, float n) {
    precise float w = (v >= n) ? v - n : v;
    precise float d = w - 0.5f * n;
    return d;
}
[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    uint W = fInW, H = fInH, n = W * H;
    float best = asfloat(0xff800000u);
    uint bi = 0xffffffffu;
    for (uint i = tid.x; i < n; i += 1024u) {
        float v = fIn0.Load(int3(i % W, i / W, 0));
        if (v > best) { best = v; bi = i; }
    }
    gV[tid.x] = best; gI[tid.x] = bi;
    GroupMemoryBarrierWithGroupSync();
    for (uint s = 512u; s > 0u; s >>= 1) {
        if (tid.x < s) {
            float vb = gV[tid.x + s]; uint ib = gI[tid.x + s];
            if (vb > gV[tid.x] || (vb == gV[tid.x] && ib < gI[tid.x])) { gV[tid.x] = vb; gI[tid.x] = ib; }
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x != 0u) return;
    uint k = (gI[0] == 0xffffffffu) ? 0u : gI[0];
    int py = (int)(k / W), px = (int)(k % W);
    float c0 = fIn0.Load(int3(px, py, 0));
    float oy = Para(fIn0.Load(int3(px, (py + (int)H - 1) % (int)H, 0)), c0, fIn0.Load(int3(px, (py + 1) % (int)H, 0)));
    float ox = Para(fIn0.Load(int3((px + (int)W - 1) % (int)W, py, 0)), c0, fIn0.Load(int3((px + 1) % (int)W, py, 0)));
    oy = min(max(oy, -0.5f), 0.5f);
    ox = min(max(ox, -0.5f), 0.5f);
    precise float vy = ((float)py + oy) + 0.5f * (float)H;
    precise float vx = ((float)px + ox) + 0.5f * (float)W;
    fFlags[FADE_FLAG_PC_DY] = asuint(Wrap(vy, (float)H));
    fFlags[FADE_FLAG_PC_DX] = asuint(Wrap(vx, (float)W));
    fFlags[FADE_FLAG_PC_PEAK] = asuint(c0);
    fFlags[FADE_FLAG_PEAK_IDX] = k;
}
)";

// The Catmull-Rom warp of a map at a texel (tl11.warp): out(y, x) = a(y - dy, x - dx), x first (per source row, the four
// taps in order from 0), then y; indices clamped to the map. Compiled in front of the overlap, cut and TM-warp passes.
inline const char* g_faldFadeWarpFnSource = R"(
float FadeWarpAt(Texture2D<float> a, int x, int y, int W, int H, int flx, float wx0, float wx1, float wx2, float wx3,
                 int fly, float wy0, float wy1, float wy2, float wy3) {
    precise float o = 0.0f;
    for (int j = 0; j < 4; j++) {
        int row = clamp(y + fly + j - 1, 0, H - 1);
        precise float t = 0.0f;
        t = t + wx0 * a.Load(int3(clamp(x + flx - 1, 0, W - 1), row, 0));
        t = t + wx1 * a.Load(int3(clamp(x + flx, 0, W - 1), row, 0));
        t = t + wx2 * a.Load(int3(clamp(x + flx + 1, 0, W - 1), row, 0));
        t = t + wx3 * a.Load(int3(clamp(x + flx + 2, 0, W - 1), row, 0));
        float wy = (j == 0) ? wy0 : ((j == 1) ? wy1 : ((j == 2) ? wy2 : wy3));
        o = o + wy * t;
    }
    return o;
}
bool FadeFinite(float v) { return (asuint(v) & 0x7f800000u) != 0x7f800000u; }
)";

// ---- T: the overlap residual and the shift's acceptance (one group; global_shift): t24 l8, t25 l8Prev; the raw shift
// from the flags. m0 = mean |l8 - l8Prev| over the overlap without the 1-texel border; when the shift is inside a quarter
// map, finite and not ~0 (|d| > 1e-6 on an axis), m1 = the same with warp(l8Prev, shift) and the shift's valid overlap,
// accepted iff m1 < m0. Kahan sums per thread (a texel outside the overlap adds 0), tree, / count -> flags RES0 / RES1 /
// USED / DY / DX.
inline const char* g_faldFadeOverlapSource = R"(
groupshared float gS0[1024];
groupshared float gS1[1024];
groupshared uint gN0[1024];
groupshared uint gN1[1024];
[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    int W = (int)fInW, H = (int)fInH;
    uint n = fInW * fInH;
    float dy = asfloat(fFlags[FADE_FLAG_PC_DY]), dx = asfloat(fFlags[FADE_FLAG_PC_DX]);
    bool ok = abs(dy) < 0.25f * (float)H && abs(dx) < 0.25f * (float)W && FadeFinite(dy) && FadeFinite(dx);
    bool need1 = ok && (abs(dy) > 1e-6f || abs(dx) > 1e-6f);
    int fly, flx; float wy0, wy1, wy2, wy3, wx0, wx1, wx2, wx3;
    FadeCubicW(dy, fly, wy0, wy1, wy2, wy3);
    FadeCubicW(dx, flx, wx0, wx1, wx2, wx3);
    precise float s0 = 0.0f, c0 = 0.0f, s1 = 0.0f, c1 = 0.0f;
    uint n0 = 0u, n1 = 0u;
    for (uint i = tid.x; i < n; i += 1024u) {
        int x = (int)(i % fInW), y = (int)(i / fInW);
        bool interior = x >= 1 && x <= W - 2 && y >= 1 && y <= H - 2;
        float a = fIn0.Load(int3(x, y, 0));
        precise float d0 = abs(a - fIn1.Load(int3(x, y, 0)));
        FadeKAdd(s0, c0, interior ? d0 : 0.0f);
        n0 += interior ? 1u : 0u;
        if (need1) {
            bool v1 = interior && FadeWarpValid(y, dy, H) && FadeWarpValid(x, dx, W);
            precise float d1 = abs(a - FadeWarpAt(fIn1, x, y, W, H, flx, wx0, wx1, wx2, wx3, fly, wy0, wy1, wy2, wy3));
            FadeKAdd(s1, c1, v1 ? d1 : 0.0f);
            n1 += v1 ? 1u : 0u;
        }
    }
    gS0[tid.x] = s0; gS1[tid.x] = s1; gN0[tid.x] = n0; gN1[tid.x] = n1;
    GroupMemoryBarrierWithGroupSync();
    for (uint st = 512u; st > 0u; st >>= 1) {
        if (tid.x < st) {
            precise float a0 = gS0[tid.x] + gS0[tid.x + st];
            precise float a1 = gS1[tid.x] + gS1[tid.x + st];
            gS0[tid.x] = a0; gS1[tid.x] = a1;
            gN0[tid.x] += gN0[tid.x + st]; gN1[tid.x] += gN1[tid.x + st];
        }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x != 0u) return;
    float inf = asfloat(0x7f800000u);
    precise float m0 = (gN0[0] > 0u) ? gS0[0] / (float)gN0[0] : inf;
    precise float m1 = (need1 && gN1[0] > 0u) ? gS1[0] / (float)gN1[0] : inf;
    if (need1) ok = m1 < m0;
    fFlags[FADE_FLAG_RES0] = asuint(m0);
    fFlags[FADE_FLAG_RES1] = asuint(m1);
    fFlags[FADE_FLAG_USED] = ok ? 1u : 0u;
    fFlags[FADE_FLAG_DY] = asuint(ok ? dy : 0.0f);
    fFlags[FADE_FLAG_DX] = asuint(ok ? dx : 0.0f);
}
)";

// ---- T: the cut test TC (one group): frac = share of the overlap (border excluded, the USED shift's valid texels) with
// |l8 - warp(l8Prev)| > 0.5 (1 when the overlap is empty); EMD between the 64-bin histograms of l8 and l8Prev over
// [ln 0.02, ln 2000] (values clamped in; bin = floor((x - lo) fC1), fC0 = lo, fC2 = the clamp top, fC3 = bin width);
// cut iff frac >= 0.15 or EMD > 0.2 -> flags FRAC / EMD / CUT.
inline const char* g_faldFadeCutSource = R"(
groupshared uint gHa[64];
groupshared uint gHb[64];
groupshared uint gCnt[1024];
groupshared uint gGt[1024];
uint Bin(float x) {
    precise float xc = min(max(x, fC0), fC2);
    precise float tb = (xc - fC0) * fC1;
    int b = (int)floor(tb);
    return (uint)clamp(b, 0, 63);
}
[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_GroupThreadID) {
    int W = (int)fInW, H = (int)fInH;
    uint n = fInW * fInH;
    if (tid.x < 64u) { gHa[tid.x] = 0u; gHb[tid.x] = 0u; }
    GroupMemoryBarrierWithGroupSync();
    float dy = asfloat(fFlags[FADE_FLAG_DY]), dx = asfloat(fFlags[FADE_FLAG_DX]);
    int fly, flx; float wy0, wy1, wy2, wy3, wx0, wx1, wx2, wx3;
    FadeCubicW(dy, fly, wy0, wy1, wy2, wy3);
    FadeCubicW(dx, flx, wx0, wx1, wx2, wx3);
    uint cnt = 0u, gt = 0u;
    for (uint i = tid.x; i < n; i += 1024u) {
        int x = (int)(i % fInW), y = (int)(i / fInW);
        float a = fIn0.Load(int3(x, y, 0)), b = fIn1.Load(int3(x, y, 0));
        bool v = x >= 1 && x <= W - 2 && y >= 1 && y <= H - 2 && FadeWarpValid(y, dy, H) && FadeWarpValid(x, dx, W);
        if (v) {
            cnt++;
            precise float d = abs(a - FadeWarpAt(fIn1, x, y, W, H, flx, wx0, wx1, wx2, wx3, fly, wy0, wy1, wy2, wy3));
            if (d > FADE_T_CD) gt++;
        }
        InterlockedAdd(gHa[Bin(a)], 1u);
        InterlockedAdd(gHb[Bin(b)], 1u);
    }
    gCnt[tid.x] = cnt; gGt[tid.x] = gt;
    GroupMemoryBarrierWithGroupSync();
    for (uint st = 512u; st > 0u; st >>= 1) {
        if (tid.x < st) { gCnt[tid.x] += gCnt[tid.x + st]; gGt[tid.x] += gGt[tid.x + st]; }
        GroupMemoryBarrierWithGroupSync();
    }
    if (tid.x != 0u) return;
    precise float frac = (gCnt[0] > 0u) ? (float)gGt[0] / (float)gCnt[0] : 1.0f;
    precise float na = (float)n;
    precise float ca = 0.0f, cb = 0.0f, acc = 0.0f;
    for (uint k = 0u; k < 64u; k++) {
        ca = ca + (float)gHa[k] / na;
        cb = cb + (float)gHb[k] / na;
        acc = acc + abs(ca - cb);
    }
    precise float emd = acc * fC3;
    fFlags[FADE_FLAG_FRAC] = asuint(frac);
    fFlags[FADE_FLAG_EMD] = asuint(emd);
    fFlags[FADE_FLAG_CUT] = (frac >= FADE_T_CFRAC || emd > FADE_T_CH) ? 1u : 0u;
}
)";

// ---- T: the TM warp at quarter res — the state s (t24) and the previous lq (t25) moved by 2x the USED shift ->
// u0 warp(s), u1 warp(lqPrev).
inline const char* g_faldFadeWarpQSource = R"(
RWTexture2D<float> wqS : register(u0);
RWTexture2D<float> wqL : register(u1);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    int W = (int)fInW, H = (int)fInH;
    if ((int)id.x >= W || (int)id.y >= H) return;
    precise float dy = 2.0f * asfloat(fFlags[FADE_FLAG_DY]);
    precise float dx = 2.0f * asfloat(fFlags[FADE_FLAG_DX]);
    int fly, flx; float wy0, wy1, wy2, wy3, wx0, wx1, wx2, wx3;
    FadeCubicW(dy, fly, wy0, wy1, wy2, wy3);
    FadeCubicW(dx, flx, wx0, wx1, wx2, wx3);
    wqS[id.xy] = FadeWarpAt(fIn0, (int)id.x, (int)id.y, W, H, flx, wx0, wx1, wx2, wx3, fly, wy0, wy1, wy2, wy3);
    wqL[id.xy] = FadeWarpAt(fIn1, (int)id.x, (int)id.y, W, H, flx, wx0, wx1, wx2, wx3, fly, wy0, wy1, wy2, wy3);
}
)";

// ---- T: the state update (TL11State.apply), per quarter-res texel: no history or a cut -> s = f_q; no advance -> s kept;
// else TM: a = 0.25 + 0.75 smooth(0.05, 0.2, |G1(lq) - G1(warp(lqPrev))|) (1 where the warp has no source), s = sw +
// a (f_q - sw). t24 s, t25 f_q, t26 warp(s), t27 G1(lq), t28 G1(warp(lqPrev)) -> u0 the new s.
inline const char* g_faldFadeTmSource = R"(
RWTexture2D<float> tmOut : register(u0);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    int W = (int)fInW, H = (int)fInH;
    if ((int)id.x >= W || (int)id.y >= H) return;
    float fq = fIn1.Load(int3(id.xy, 0));
    if (fHasHistory == 0u || fFlags[FADE_FLAG_CUT] != 0u) { tmOut[id.xy] = fq; return; }
    if (fFlags[FADE_FLAG_ADVANCE] == 0u) { tmOut[id.xy] = fIn0.Load(int3(id.xy, 0)); return; }
    precise float dy = 2.0f * asfloat(fFlags[FADE_FLAG_DY]);
    precise float dx = 2.0f * asfloat(fFlags[FADE_FLAG_DX]);
    bool valid = FadeWarpValid((int)id.y, dy, H) && FadeWarpValid((int)id.x, dx, W);
    precise float r = abs(fIn3.Load(int3(id.xy, 0)) - fIn4.Load(int3(id.xy, 0)));
    precise float a = FADE_T_A0 + (1.0f - FADE_T_A0) * FadeSmooth(FADE_T_R_LO, FADE_T_R_HI, r);
    if (!valid) a = 1.0f;
    float sw = fIn2.Load(int3(id.xy, 0));
    precise float s = sw + a * (fq - sw);
    tmOut[id.xy] = s;
}
)";

// ---- T: the state's history maps, predicated on the GPU (no readback): no history or an advancing frame -> the
// current map, else the previous one is kept. fOp 0: float (t24 current, t25 previous -> u0); 1: float2 (t29, t30 -> u4).
inline const char* g_faldFadeSelectSource = R"(
RWTexture2D<float> selOut : register(u0);
RWTexture2D<float2> selOut2 : register(u4);
[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID) {
    if (id.x >= fInW || id.y >= fInH) return;
    bool take = (fHasHistory == 0u) || (fFlags[FADE_FLAG_ADVANCE] != 0u);
    if (fOp == 0u) selOut[id.xy] = take ? fIn0.Load(int3(id.xy, 0)) : fIn1.Load(int3(id.xy, 0));
    else selOut2[id.xy] = take ? fInC0.Load(int3(id.xy, 0)) : fInC1.Load(int3(id.xy, 0));
}
)";
