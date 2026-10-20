"""The frozen fade stack (ASF10 + T11_TMTC, frozen 2026-10-19; the owner: "freeze without the floor fix") as a float32,
GPU-order twin: what shared/fald_fade_shader.h computes, operation for operation, next to :class:`dlc.fald.gpuemu.Emu`
(which this module uses unchanged for the layer underneath).

Reference = the float64 study twin (DLC results/fald_nearfield_2026-10-17: r10/temu.py ``fade9_t`` + r10/cands10.py
``floor_lit`` / ``knee_reach`` + r11/cands11.py ``make_post11`` (gc = oc = None) + r11/T/tl11.py ``TL11State`` with
``T11_DEF``, variant ``F10_TMTC``), hash-logged in that folder's FROZEN_hashlog.txt ("FREEZE 2026-10-19"). The port brief
is r11/PORT_PLAN.md. To reproduce the frozen twin itself, import those study sources with this package on the path (they
use :mod:`dlc.fald.gpuemu`, whose file is unchanged): DLC tests/test_fald_fade_gate1.py does exactly that.

What is float32 GPU order here (and therefore in the HLSL):
  * every texture is float32 (R32F; complex = two float32);
  * every operation rounds to float32 after it, in the order written (the shaders are compiled with IEEE strictness, so
    the compiler fuses and reorders nothing);
  * min / max / clamp follow HLSL's NaN rule (a NaN operand yields the other one): ``np.fmin`` / ``np.fmax``;
  * every long sum (the separable Gaussians, the DFTs, the reductions) is Kahan-compensated: a plain float32 sigma-24
    Gaussian of ln L carries ~1e-6 of rounding, which the flank term's slope (up to 52 per unit gradient) turns into
    ~2e-4 of fade — the size of the gate;
  * the knee-cap fraction kc is evaluated as ln(a / K) / ln(g_eff) (= the twin's 1 - ln(ge) / ln(g_eff) in exact
    arithmetic): the twin's form has no float32 precision left as g_eff -> 1 (ge / g_eff = 1 +- 1 ulp);
  * the phase correlation is a dense DFT (sizes need not be powers of two) with float32 twiddle tables.
Tables (Gaussian weights, twiddles, the Hann window, the spectral Gaussian) are computed in float64 and stored float32,
exactly as the C++ uploads them (shared/fald_fade.cpp).

Entry points: :func:`fade9`, :func:`floor_lit`, :func:`reach_of`, :func:`fade_post`, :class:`TState` (T11_TMTC), and
:class:`FadeEmu` (one frame of the whole layer with the stack on, as FaldRunPasses runs it)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .gpuemu import KNEE_CAP_TRUST, KNEE_START, Emu, clamp_star, trunc_half
from .starfield import StarfieldParams

f32 = np.float32

# ---- frozen constants (PORT_PLAN 1.4; = shared/fald_fade_shader.h's static consts) -------------------------------
LAM = 80.0                           # flank term
SIG_S, SIG_L = 4.0, 24.0
RCLOSE, RMAX = 12, 32                # max-filter radii of raw / the thin-step flag
RS_WIN, RS_K_LO, RS_K_HI, RS_CAP = 17, 2.5, 4.0, 0.02
RS_LO, RS_HI = 0.01, 0.03
RS_RATIO = (1.4, 1.9)
CARE = (0.3, 0.6)
PP_PRIOR, PP_CONF = 0.05, 4.0
HYB_SIG, HYB_W0 = 32.0, 0.25
CL_Q, CL_OPEN_R, CL_CLOSE_R, CL_TH, CL_SIG = 8, 50, 40, (0.7, 1.5), 8.0
CONE_Q, CONE_DEC, CONE_ITERS, CONE_SIG = 4, 4.0 / 64.0, 16, 4.0
LOG_FLOOR = 0.05
OF_LIT, OF_POS_HI, OF_D, OF_LAM, OF_OPEN_R, OF_ITERS, OF_SIG = (1.0, 3.0), 0.03, 96.0, 2.0, 50, 24, 4.0
T_EPS, T_A0, T_R, T_RSIG = 0.02, 0.25, (0.05, 0.2), 1.0
T_CD, T_CFRAC, T_CH, T_NBINS = 0.5, 0.15, 0.2, 64
T_HLO, T_HHI = math.log(0.02), math.log(2000.0)
T_PCSIG = 3.0
KC_EPS = 1e-6
GROUP = 1024                         # threads of the one-group reductions (shared/fald_fade_shader.h FADE_RED_THREADS)


# ================================================================================================= HLSL scalar rules
def hmax(a, b):
    return np.fmax(a, b)


def hmin(a, b):
    return np.fmin(a, b)


def hclamp(x, lo, hi):
    """HLSL clamp(x, lo, hi) = min(max(x, lo), hi): a NaN x gives lo."""
    return np.fmin(np.fmax(x, f32(lo)), f32(hi)).astype(f32)


def smooth(a, b, x):
    """The twin's smooth_t (= HLSL FadeSmooth): t = clamp((x - a) / (b - a), 0, 1); t * t * (3 - 2 t)."""
    a32, b32 = f32(a), f32(b)
    t = hclamp((x - a32) / (b32 - a32), 0.0, 1.0)
    return (t * t * (f32(3.0) - f32(2.0) * t)).astype(f32)


def sanitize(mi):
    """fade9's first step: NaN -> 0, +inf -> 1e4, -inf -> 0, then [0, 1e4]."""
    m = np.where(np.isnan(mi), f32(0.0), mi).astype(f32)
    m = np.where(np.isposinf(m), f32(1e4), m)
    m = np.where(np.isneginf(m), f32(0.0), m)
    return hclamp(m, 0.0, 1e4)


def _kadd(s, c, x):
    """One Kahan step (HLSL FadeKAdd): y = x - c; t = s + y; c = (t - s) - y; s = t."""
    y = x - c
    t = s + y
    c = (t - s) - y
    return t, c


# ================================================================================================= tables (C++ twins)
def gauss_weights(sigma: float):
    """(r, w[0..r]) of the twin's _gauss_w (truncate 4: r = int(4 sigma + 0.5)), summed in ascending order in float64,
    then stored float32 — shared/fald_fade.cpp FadeGaussWeights."""
    r = int(4.0 * float(sigma) + 0.5)
    w = [math.exp((-0.5 * float(x)) * float(x) / (float(sigma) * float(sigma))) for x in range(-r, r + 1)]
    s = 0.0
    for v in w:
        s += v
    return r, np.array([w[r + j] / s for j in range(r + 1)], dtype=np.float64).astype(f32)


def twiddles(n: int):
    """float32 (cos, sin)(2 pi j / n), j = 0 .. n - 1 (FadeTwiddles)."""
    th = [(2.0 * math.pi) * float(j) / float(n) for j in range(n)]
    return np.array([math.cos(t) for t in th]).astype(f32), np.array([math.sin(t) for t in th]).astype(f32)


def hann2(h: int, w: int):
    """the twin's non-periodic 2-D Hann window hy[:, None] * hx[None, :] (float64), stored float32 (FadeHann)."""
    def hann(n):
        if n == 1:
            return np.ones(1)
        return np.array([0.5 - 0.5 * math.cos(2.0 * math.pi * float(k) / float(n - 1)) for k in range(n)])
    return (hann(h)[:, None] * hann(w)[None, :]).astype(f32)


def spectral_gauss(h: int, w: int, sigma: float = T_PCSIG):
    """exp(-2 pi^2 sigma^2 (fy^2 + fx^2)) with fftfreq (float64), stored float32 (FadeSpecGauss)."""
    def freq(n):
        return np.array([(k if k < (n + 1) // 2 else k - n) / float(n) for k in range(n)])
    fy, fx = freq(h), freq(w)
    f2 = fy[:, None] ** 2 + fx[None, :] ** 2
    return np.exp(-2.0 * math.pi ** 2 * float(sigma) ** 2 * f2).astype(f32)


_GW: dict = {}


def _gw(sigma):
    if sigma not in _GW:
        _GW[sigma] = gauss_weights(sigma)
    return _GW[sigma]


# ================================================================================================= separable filters
def _pad(x, r, axis):
    return np.pad(x, ((r, r), (0, 0)) if axis == 0 else ((0, 0), (r, r)), mode="edge")


def _gauss1d(x, r, w, axis):
    n = x.shape[axis]
    xp = _pad(x, r, axis)
    sl = (lambda k: xp[r + k: r + k + n]) if axis == 0 else (lambda k: xp[:, r + k: r + k + n])
    s = (sl(0) * w[0]).astype(f32)
    c = np.zeros_like(s)
    for j in range(1, r + 1):
        term = ((sl(-j) + sl(j)) * w[j]).astype(f32)
        s, c = _kadd(s, c, term)
    return s


def gauss(x, sigma):
    """scipy gaussian_filter(mode='nearest', truncate 4): vertical (axis 0) then horizontal, Kahan sums (FadeGauss)."""
    r, w = _gw(float(sigma))
    x = np.asarray(x, f32)
    return _gauss1d(_gauss1d(x, r, w, 0), r, w, 1)


def _ext1d(x, r, axis, fn):
    n = x.shape[axis]
    xp = _pad(x, r, axis)
    sl = (lambda k: xp[r + k: r + k + n]) if axis == 0 else (lambda k: xp[:, r + k: r + k + n])
    out = sl(-r).copy()
    for k in range(-r + 1, r + 1):
        out = fn(out, sl(k))
    return out


def maxf(x, size):
    r = int(size) // 2
    return _ext1d(_ext1d(np.asarray(x, f32), r, 0, np.fmax), r, 1, np.fmax)


def minf(x, size):
    r = int(size) // 2
    return _ext1d(_ext1d(np.asarray(x, f32), r, 0, np.fmin), r, 1, np.fmin)


def grey_open(x, size):
    return maxf(minf(x, size), size)


def grey_close(x, size):
    return minf(maxf(x, size), size)


def grad_mag(a):
    """np.hypot(*np.gradient(a)) in float32 GPU order: central differences x 0.5 inside, one-sided at the borders,
    sqrt(gy^2 + gx^2) (FadeGrad)."""
    a = np.asarray(a, f32)
    gy = np.empty_like(a)
    gx = np.empty_like(a)
    gy[1:-1] = (a[2:] - a[:-2]) * f32(0.5)
    gy[0] = a[1] - a[0]
    gy[-1] = a[-1] - a[-2]
    gx[:, 1:-1] = (a[:, 2:] - a[:, :-2]) * f32(0.5)
    gx[:, 0] = a[:, 1] - a[:, 0]
    gx[:, -1] = a[:, -1] - a[:, -2]
    return np.sqrt(gy * gy + gx * gx).astype(f32)


def down(a, f):
    """f x f mean, edge-replicated to a multiple of f: row-major sum of the block, then x 1/f^2 (FadeDown)."""
    a = np.asarray(a, f32)
    H, W = a.shape
    Ho, Wo = -(-H // f), -(-W // f)
    yy, xx = np.arange(Ho) * f, np.arange(Wo) * f
    s = np.zeros((Ho, Wo), f32)
    for dy in range(f):
        ys = np.minimum(yy + dy, H - 1)
        rows = a[ys]
        for dx in range(f):
            s = s + rows[:, np.minimum(xx + dx, W - 1)]
    return (s * f32(1.0 / (f * f))).astype(f32)


def block_max(a, f):
    """f x f block max (the twin's replicate-padded max_pool2d: the max over the block's in-frame pixels)."""
    a = np.asarray(a, f32)
    H, W = a.shape
    Ho, Wo = -(-H // f), -(-W // f)
    yy, xx = np.arange(Ho) * f, np.arange(Wo) * f
    out = None
    for dy in range(f):
        rows = a[np.minimum(yy + dy, H - 1)]
        for dx in range(f):
            v = rows[:, np.minimum(xx + dx, W - 1)]
            out = v.copy() if out is None else np.fmax(out, v)
    return out


def _up_axis(n_out, n_in, f):
    t = ((np.arange(n_out, dtype=f32) + f32(0.5)) * f32(1.0 / f) - f32(0.5)).astype(f32)
    fl = np.floor(t)
    i0 = np.clip(fl.astype(np.int64), 0, n_in - 1)
    i1 = np.clip(i0 + 1, 0, n_in - 1)
    fr = hclamp(t - fl, 0.0, 1.0)
    fr = np.where(t < 0, f32(0.0), np.where(t > f32(n_in - 1), f32(1.0), fr)).astype(f32)
    return i0, i1, fr


def up(a, f, shape):
    """bilinear upsample by f with texel-centre alignment, t = (i + 0.5) / f - 0.5, the edge texel held outside;
    y first then x (FadeUp: four Loads, float32 weights — not the hardware sampler's 8-bit fractions)."""
    a = np.asarray(a, f32)
    y0, y1, fy = _up_axis(shape[0], a.shape[0], f)
    x0, x1, fx = _up_axis(shape[1], a.shape[1], f)
    ofy, ofx = (f32(1.0) - fy), (f32(1.0) - fx)
    ay = a[y0] * ofy[:, None] + a[y1] * fy[:, None]
    return (ay[:, x0] * ofx[None] + ay[:, x1] * fx[None]).astype(f32)


# ================================================================================================= ASF9o fade (1.1)
def pushpull(v, w, prior=PP_PRIOR, conf=PP_CONF, sig=1.0):
    """cands9 / temu pushpull (prior_top False: the prior enters W0 only, down() carries it to every level)."""
    S = [(v * w).astype(f32)]
    Wt = [(w + f32(prior)).astype(f32)]
    while min(S[-1].shape) > 2:
        S.append(down(S[-1], 2))
        Wt.append(down(Wt[-1], 2))
    E = (S[-1] / hmax(Wt[-1], f32(1e-12))).astype(f32)
    for lv in range(len(S) - 2, -1, -1):
        Eu = gauss(up(E, 2, S[lv].shape), sig)
        a = hclamp(Wt[lv] * f32(conf), 0.0, 1.0)
        El = (S[lv] / hmax(Wt[lv], f32(1e-12))).astype(f32)
        E = (a * El + (f32(1.0) - a) * Eu).astype(f32)
    return E


def fade9(mi, keep: bool = False):
    """r10/temu.fade9_t with cands9.DEF and intr off (ASF9o), float32 GPU order. mi = the frame's max channel
    (unsanitised; the sanitise step is the first one here). Returns fade (H, W) float32 (+ the parts with keep)."""
    ms = sanitize(np.asarray(mi, f32))
    H, W = ms.shape
    lv = np.log(hmax(ms, f32(LOG_FLOOR))).astype(f32)
    gs = grad_mag(gauss(lv, SIG_S))
    gl = grad_mag(gauss(lv, SIG_L))
    y = (f32(LAM) * hmin(gs, gl)).astype(f32)
    raw0 = (f32(1.0) - f32(1.0) / (f32(1.0) + y * y)).astype(f32)
    raw = gauss(maxf(raw0, 2 * RCLOSE + 1), 4.0)
    # thin step flag with the robust (noise-floor) gate
    g1 = grad_mag(gauss(lv, 2.0))
    g2 = grad_mag(gauss(lv, 6.0))
    nf = hmin(minf(gauss(g1, 3.0), RS_WIN), f32(RS_CAP))
    lo_t = hmax(f32(RS_K_LO) * nf, f32(RS_LO))
    hi_t = hmax(f32(RS_K_HI) * nf, f32(RS_HI))
    t = hclamp((g1 - lo_t) / hmax(hi_t - lo_t, f32(1e-12)), 0.0, 1.0)
    gate = (t * t * (f32(3.0) - f32(2.0) * t)).astype(f32)
    thin = (smooth(RS_RATIO[0], RS_RATIO[1], (g1 / hmax(g2, f32(1e-9))).astype(f32)) * gate).astype(f32)
    step = hclamp(gauss(maxf(thin, 2 * RMAX + 1), 4.0), 0.0, 1.0)
    w = (f32(1.0) - step).astype(f32)
    care = smooth(CARE[0], CARE[1], gauss(ms, 2.0))
    # push-pull + hybrid fill
    ws = (w * care).astype(f32)
    vsum = (raw * ws).astype(f32)
    wsum = ws
    v = ((vsum / hmax(wsum, f32(1e-12))) * (wsum > f32(1e-9)).astype(f32)).astype(f32)
    Fpp = pushpull(v, hclamp(wsum, 0.0, 1.0))
    num = gauss(vsum, HYB_SIG)                       # = gauss(raw * ws): the same float32 product
    den = gauss(ws, HYB_SIG)
    a = hclamp(den / f32(HYB_W0), 0.0, 1.0)
    F = (a * num / hmax(den, f32(1e-9)) + (f32(1.0) - a) * Fpp).astype(f32)
    fade = hclamp(w * raw + (f32(1.0) - w) * F, 0.0, 1.0)
    fade0 = fade
    # CL: closing at 1/8 res over regions above their large-scale floor
    f8 = down(fade, CL_Q)
    l8 = np.log(hmax(down(ms, CL_Q), f32(LOG_FLOOR))).astype(f32)
    th = (l8 - grey_open(l8, 2 * CL_OPEN_R + 1)).astype(f32)
    gate8 = smooth(CL_TH[0], CL_TH[1], th)
    fc = grey_close(f8, 2 * CL_CLOSE_R + 1)
    add = gauss(up((hmax(fc - f8, f32(0.0)) * gate8).astype(f32), CL_Q, (H, W)), CL_SIG)
    fade = hclamp(fade + add * (f32(1.0) - fade), 0.0, 1.0)
    # cone at 1/4 res
    cone = down(fade, CONE_Q)
    for _ in range(CONE_ITERS):
        cone = hmax(cone, (maxf(cone, 3) - f32(CONE_DEC)).astype(f32))
    cu = gauss(up(cone, CONE_Q, (H, W)), CONE_SIG)
    fade = (fade + w * hmax(cu - fade, f32(0.0))).astype(f32)
    fade = hclamp(fade, 0.0, 1.0)
    if keep:
        return fade, {"raw": raw, "w": w, "care": care, "fade0": fade0, "fill": F, "cl_add": add, "cone": cu}
    return fade


# ================================================================================================= OF (1.3)
def floor_lit(mi):
    """cands10.floor_lit: F = exp(up8(grey_open(ln max(down8(mi), 0.05), 101))), mi UNSANITISED (as the twin)."""
    mi = np.asarray(mi, f32)
    l8 = np.log(hmax(down(mi, CL_Q), f32(LOG_FLOOR))).astype(f32)
    return np.exp(up(grey_open(l8, 2 * OF_OPEN_R + 1), CL_Q, mi.shape)).astype(f32)


def reach_dec(mi):
    """knee_reach's per-texel step: 4 / 96 + grad_mag(gauss(ln max(down4(mi), 0.05), 1)) / 2 (once per frame)."""
    lq = np.log(hmax(down(np.asarray(mi, f32), CONE_Q), f32(LOG_FLOOR))).astype(f32)
    return (f32(CONE_Q / OF_D) + grad_mag(gauss(lq, 1.0)) / f32(OF_LAM)).astype(f32)


def reach_of(kc, dec, shape):
    """cands10.knee_reach(kc, 96, mi, 2): the Chebyshev cone of the 4 x 4 block max of kc, 24 steps of maxf3 - dec,
    up 4 + sigma 4, clamped. kc = full-res knee-cap fraction, dec = :func:`reach_dec`."""
    cone = block_max(kc, CONE_Q)
    for _ in range(int(math.ceil(OF_D / CONE_Q))):
        cone = hmax(cone, (maxf(cone, 3) - dec).astype(f32))
    return hclamp(gauss(up(cone, CONE_Q, shape), OF_SIG), 0.0, 1.0)


def log1p32(x):
    """HLSL FadeLog1p: u = 1 + x; u == 1 ? x : log(u) * x / (u - 1) (accurate for small x in float32)."""
    x = np.asarray(x, f32)
    u = (f32(1.0) + x).astype(f32)
    den = (u - f32(1.0)).astype(f32)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = (np.log(u).astype(f32) * x / np.where(den == 0, f32(1.0), den)).astype(f32)
    return np.where(u == f32(1.0), x, q).astype(f32)


def knee_cap(e: Emu, img32, bT, bE, gs_x, gs_y):
    """The knee-cap fraction kc per pixel (HLSL FadeKneeCap; the twin's kc of TEmu.correct_tex, want_kc) in float32,
    from the arithmetic of Correct: img32 = the balanced as-if-white nits (3, H, W) float32, bT / bE = the sampled
    flat-normalised fields, gs_x / gs_y = the sampled (gain, ceiling B_est). White pedestal (pedMode 0) only.
    kc = ln(a / K) / ln(g_eff) where the knee binds (K >= m), 1 where it pulls below the original, else 0."""
    Wn = f32(e.white)
    img32 = np.asarray(img32, f32)
    bT = hmax(np.asarray(bT, f32), f32(0.0))
    bE = np.asarray(bE, f32)
    gain = np.asarray(gs_x, f32)
    maxc = hmax(img32[0], hmax(img32[1], img32[2]))
    s = hmin(maxc, Wn)
    lum = e.lumHi > e.lumLo
    wl = _hlsl_smoothstep(f32(e.lumLo), f32(e.lumHi), maxc) if lum else np.ones_like(maxc)
    d = ((gain - f32(1.0)) * wl).astype(f32) if lum else (gain - f32(1.0)).astype(f32)
    g_eff = (f32(1.0) + d).astype(f32) if lum else gain
    # PedestalTerm, mode 0 (white): a0 * FadeWeight(maxc, bE)
    drv = e.drive_of(s).astype(f32)
    tmin = f32(e.tmin)
    pedRef = (Wn * drv * tmin).astype(f32)
    ped = (Wn * bT * tmin).astype(f32)
    delta = (pedRef - ped).astype(f32)
    fct = np.ones_like(delta)
    neg = delta < 0
    with np.errstate(divide="ignore", invalid="ignore"):
        for ch in range(3):
            fct = np.where(neg, hmin(fct, (img32[ch] / np.where(neg, -delta, f32(1.0))).astype(f32)), fct)
    wf = _hlsl_smoothstep(f32(e.fadeLo), f32(e.fadeHi), bE)
    if lum:
        wf = (wf * _hlsl_smoothstep(f32(e.lumLo), f32(e.lumHi), maxc)).astype(f32)
    term = (delta * fct * wf).astype(f32)
    m = hmax(img32[0] + term, hmax(img32[1] + term, img32[2] + term))
    cap = (Wn * hmax(np.asarray(gs_y, f32), f32(1e-9))).astype(f32)
    C = hmin(Wn, (cap / f32(KNEE_CAP_TRUST)).astype(f32)) if KNEE_CAP_TRUST > 0 else np.full_like(cap, Wn)
    a = (m * g_eff).astype(f32)
    binds = a > f32(KNEE_START) * C
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (((a / C) - f32(KNEE_START)) / (f32(1.0) - f32(KNEE_START))).astype(f32)
        K = (C * (f32(KNEE_START) + (f32(1.0) - f32(KNEE_START)) * t / (f32(1.0) + t))).astype(f32)
        num = log1p32(((f32(1.0) - f32(KNEE_START)) * t * t / (f32(KNEE_START) + t)).astype(f32))
        kc = hclamp(num / log1p32(d), 0.0, 1.0)
    kc = np.where(K < m, f32(1.0), kc)
    kc = np.where(binds, kc, f32(0.0))
    kc = np.where((d > f32(KC_EPS)) & (m > f32(1e-9)), kc, f32(0.0))
    return kc.astype(f32)


def _hlsl_smoothstep(a, b, x):
    """HLSL smoothstep(a, b, x): t = saturate((x - a) / (b - a)); t * t * (3 - 2 t)."""
    t = hclamp((np.asarray(x, f32) - a) / (b - a), 0.0, 1.0)
    return (t * t * (f32(3.0) - f32(2.0) * t)).astype(f32)


def fade_post(req, img, fade_t, F, reach):
    """HLSL FadePost (make_post11 with gc = oc = None, OF on): one scale for the three channels,
    req * exp(ls (1 - fade_r) - ls), fade_r = fade_T (1 - keep), keep = lit * pos * (1 - reach). float32."""
    req = np.asarray(req, f32)
    img = np.asarray(img, f32)
    mi = hmax(img[0], hmax(img[1], img[2]))
    mr = hmax(req[0], hmax(req[1], req[2]))
    ok = (mi > f32(1e-9)) & (mr > f32(1e-12))
    with np.errstate(divide="ignore", invalid="ignore"):
        ls = np.where(ok, np.log((hmax(mr, f32(1e-12)) / hmax(mi, f32(1e-9))).astype(f32)), f32(0.0)).astype(f32)
    lit = smooth(OF_LIT[0], OF_LIT[1], np.asarray(F, f32))
    pos = smooth(0.0, OF_POS_HI, ls)
    keep = (lit * pos * (f32(1.0) - np.asarray(reach, f32))).astype(f32)
    fade = (np.asarray(fade_t, f32) * (f32(1.0) - keep)).astype(f32)
    ls_eff = (ls * (f32(1.0) - fade)).astype(f32)
    out = hmax(req * np.exp((ls_eff - ls).astype(f32))[None], f32(0.0)).astype(f32)
    return out, {"ls": ls, "keep": keep, "fade_r": fade}


# ================================================================================================= T11_TMTC (1.2)
def _group_sum(vals):
    """One 1024-thread group's Kahan-compensated float32 sum (FadeReduce): thread t adds elements t, t + 1024, ... in
    order, then the 512 / 256 / ... / 1 tree of plain adds."""
    v = np.asarray(vals, f32).ravel()
    s = np.zeros(GROUP, f32)
    c = np.zeros(GROUP, f32)
    for i0 in range(0, v.size, GROUP):
        seg = v[i0: i0 + GROUP]
        n = seg.size
        s[:n], c[:n] = _kadd(s[:n], c[:n], seg)
    stride = GROUP // 2
    while stride:
        s[:stride] = s[:stride] + s[stride: 2 * stride]
        stride //= 2
    return f32(s[0])


def _group_count(mask):
    return int(np.count_nonzero(mask))


def _argmax_first(r):
    """argmax with ties on the smallest flattened index (torch.argmax's first maximum; NaN never wins)."""
    flat = np.asarray(r, f32).ravel()
    m = np.nanmax(flat) if np.any(~np.isnan(flat)) else None
    if m is None:
        return 0
    return int(np.flatnonzero(flat == m)[0])


def dft2(x, tw_h, tw_w):
    """Forward 2-D DFT of a real map (rows then columns), Kahan sums in float32 (FadeDftRow / FadeDftCol).
    Returns (re, im) float32 (H, W)."""
    H, W = x.shape
    cw, sw = tw_w
    ch_, sh_ = tw_h
    k = np.arange(W)
    re = np.zeros((H, W), f32)
    cre = np.zeros_like(re)
    im = np.zeros((H, W), f32)
    cim = np.zeros_like(im)
    for n in range(W):
        idx = (k * n) % W
        xn = x[:, n:n + 1]
        re, cre = _kadd(re, cre, (xn * cw[idx][None, :]).astype(f32))
        im, cim = _kadd(im, cim, (-(xn * sw[idx][None, :])).astype(f32))
    ky = np.arange(H)
    Br = np.zeros((H, W), f32)
    cbr = np.zeros_like(Br)
    Bi = np.zeros((H, W), f32)
    cbi = np.zeros_like(Bi)
    for m in range(H):
        idx = (ky * m) % H
        c = ch_[idx][:, None]
        s = sh_[idx][:, None]
        zr, zi = re[m:m + 1], im[m:m + 1]
        Br, cbr = _kadd(Br, cbr, (zr * c + zi * s).astype(f32))
        Bi, cbi = _kadd(Bi, cbi, (zi * c - zr * s).astype(f32))
    return Br, Bi


def idft2_real(Rr, Ri, tw_h, tw_w):
    """Re(ifft2(R)): inverse along x (complex), then along y keeping the real part, / (H W) (FadeIdftRow / FadeIdftCol)."""
    H, W = Rr.shape
    cw, sw = tw_w
    ch_, sh_ = tw_h
    xs = np.arange(W)
    Zr = np.zeros((H, W), f32)
    czr = np.zeros_like(Zr)
    Zi = np.zeros((H, W), f32)
    czi = np.zeros_like(Zi)
    for kx in range(W):
        idx = (kx * xs) % W
        c = cw[idx][None, :]
        s = sw[idx][None, :]
        rr, ri = Rr[:, kx:kx + 1], Ri[:, kx:kx + 1]
        Zr, czr = _kadd(Zr, czr, (rr * c - ri * s).astype(f32))
        Zi, czi = _kadd(Zi, czi, (rr * s + ri * c).astype(f32))
    ys = np.arange(H)
    r = np.zeros((H, W), f32)
    cr = np.zeros_like(r)
    for ky in range(H):
        idx = (ky * ys) % H
        c = ch_[idx][:, None]
        s = sh_[idx][:, None]
        r, cr = _kadd(r, cr, (Zr[ky:ky + 1] * c - Zi[ky:ky + 1] * s).astype(f32))
    return (r * f32(1.0 / (H * W))).astype(f32)


def _cubic_taps(n, d):
    """tl11._taps (Catmull-Rom) in float32: indices (4, n), weights (4,) float32, valid (n,) — source i - d in [0, n-1]."""
    d = f32(d)
    p0 = f32(-d)
    fl = f32(np.floor(p0))
    f = f32(p0 - fl)
    f2 = f32(f * f)
    f3 = f32(f2 * f)
    h = f32(0.5)
    w = (f32((f32(-f3 + f32(2.0) * f2) - f) * h), f32((f32(f32(3.0) * f3 - f32(5.0) * f2) + f32(2.0)) * h),
         f32((f32(f32(-3.0) * f3 + f32(4.0) * f2) + f) * h), f32((f3 - f2) * h))
    i = np.arange(n)
    idx = np.stack([np.clip(i + int(fl) + o, 0, n - 1) for o in (-1, 0, 1, 2)])
    pos = (i.astype(f32) + p0).astype(f32)
    valid = (pos >= f32(-1e-9)) & (pos <= f32(n - 1) + f32(1e-9))
    return idx, w, valid


def warp(a, dy, dx):
    """tl11.warp (Catmull-Rom, separable, x then y; sums in the twin's order) in float32: (out, valid)."""
    a = np.asarray(a, f32)
    H, W = a.shape
    iy, wy, vy = _cubic_taps(H, dy)
    ix, wx, vx = _cubic_taps(W, dx)
    t = np.zeros_like(a)
    for j in range(4):
        t = (t + wx[j] * a[:, ix[j]]).astype(f32)
    out = np.zeros_like(a)
    for j in range(4):
        out = (out + wy[j] * t[iy[j]]).astype(f32)
    return out, vy[:, None] & vx[None, :]


def _interior(v):
    v = v.copy()
    v[0] = False
    v[-1] = False
    v[:, 0] = False
    v[:, -1] = False
    return v


def _overlap_mean(a_new, a_prev, dy, dx):
    """tl11._overlap_mean_abs: mean |a_new - warp(a_prev)| over the valid overlap without the 1-texel border. The
    device's reduction adds 0 for a texel outside the overlap (a Kahan step of 0 is not a no-op: it folds the
    compensation in), so the emulation sums the masked map over every texel."""
    w, v = warp(a_prev, dy, dx)
    v = _interior(v)
    n = _group_count(v)
    if n == 0:
        return f32(np.inf), w, v
    d = np.where(v, np.abs((a_new - w).astype(f32)), f32(0.0)).astype(f32)
    return f32(_group_sum(d) / f32(n)), w, v


def _fmod_floor(x, n):
    """x mod n for x in [0, 2n) (the shift's range here): x >= n ? x - n : x (FadeArgmax Wrap)."""
    x = f32(x)
    n = f32(n)
    return f32(x - n) if x >= n else x


def phase_shift(l8p, l8, spec_prev=None, spec_cur=None, tables=None):
    """tl11.phase_shift in float32 GPU order: (dy, dx, c0, spec_cur). spec_* = (re, im) of the windowed, mean-removed
    map (the previous frame's is cached by the device)."""
    H, W = l8.shape
    tw_h, tw_w, win, G = tables or (twiddles(H), twiddles(W), hann2(H, W), spectral_gauss(H, W))

    def spec(a):
        mean = f32(_group_sum(a) / f32(H * W))
        return dft2(((a - mean).astype(f32) * win).astype(f32), tw_h, tw_w)
    Ar, Ai = spec_prev if spec_prev is not None else spec(l8p)
    Br, Bi = spec_cur if spec_cur is not None else spec(l8)
    Rr = (Br * Ar + Bi * Ai).astype(f32)
    Ri = (Bi * Ar - Br * Ai).astype(f32)
    mag = np.sqrt(Rr * Rr + Ri * Ri).astype(f32)
    mmax = f32(np.nanmax(mag)) if mag.size else f32(0.0)
    den = (mag + f32(1e-12) * mmax).astype(f32)
    with np.errstate(divide="ignore", invalid="ignore"):
        Rr = np.where(den > 0, (Rr / den).astype(f32), f32(0.0)) * G
        Ri = np.where(den > 0, (Ri / den).astype(f32), f32(0.0)) * G
    r = idft2_real(Rr.astype(f32), Ri.astype(f32), tw_h, tw_w)
    k = _argmax_first(r)
    py, px = divmod(k, W)
    c0 = f32(r[py, px])

    def para(m1, c, p1):
        den_ = f32(f32(m1 - f32(2.0) * c) + p1)
        return f32(0.0) if abs(float(den_)) < 1e-15 else f32(f32(f32(0.5) * f32(m1 - p1)) / den_)
    oy = para(r[(py - 1) % H, px], c0, r[(py + 1) % H, px])
    ox = para(r[py, (px - 1) % W], c0, r[py, (px + 1) % W])
    oy = f32(min(max(float(oy), -0.5), 0.5))
    ox = f32(min(max(float(ox), -0.5), 0.5))
    dy = f32(_fmod_floor(f32(f32(f32(py) + oy) + f32(H / 2.0)), H) - f32(H / 2.0))
    dx = f32(_fmod_floor(f32(f32(f32(px) + ox) + f32(W / 2.0)), W) - f32(W / 2.0))
    return dy, dx, c0, (Br, Bi)


def hist_constants():
    """(lo, bins per ln unit, clamp top, bin width) as float32 — the CB constants of FadeCut (shared/fald_fade.cpp)."""
    return (f32(T_HLO), f32(T_NBINS / (T_HHI - T_HLO)), f32(T_HHI - 1e-9), f32((T_HHI - T_HLO) / T_NBINS))


def hist_emd(a, b):
    """tl11.hist_emd in float32 GPU order (FadeCut): 64-bin histograms of the two maps over [ln 0.02, ln 2000] (values
    clamped in; bin = floor((x - lo) k)), CDFs accumulated in float32, sum |CDF_a - CDF_b| x bin width."""
    lo, k, top, bw = hist_constants()

    def hist(x):
        xc = hclamp(np.asarray(x, f32), float(lo), float(top))
        tb = ((xc - lo) * k).astype(f32)
        bins = np.clip(np.floor(tb).astype(np.int64), 0, T_NBINS - 1)
        return np.bincount(bins.ravel(), minlength=T_NBINS)[:T_NBINS]
    ha, hb = hist(a), hist(b)
    na = f32(np.asarray(a).size)
    ca = cb = f32(0.0)
    acc = f32(0.0)
    for j in range(T_NBINS):
        ca = f32(ca + f32(f32(ha[j]) / na))
        cb = f32(cb + f32(f32(hb[j]) / na))
        acc = f32(acc + f32(abs(f32(ca - cb))))
    return f32(acc * bw)


def maps_of(mi):
    """tl11.maps_of without the TL cut statistic: (l8 = ln(L8 + 0.02), lq = ln(L4 + 0.02)), mi UNSANITISED."""
    mi = np.asarray(mi, f32)
    return (np.log((down(mi, 8) + f32(T_EPS)).astype(f32)).astype(f32),
            np.log((down(mi, 4) + f32(T_EPS)).astype(f32)).astype(f32))


@dataclass
class TState:
    """r11/T/tl11.TL11State (motion = TM, cut = TC, T11_DEF) in float32 GPU order — the device's persistent T state
    (shared/fald_fade.cpp: s, l8Prev, lqPrev, the previous windowed spectrum, the flags). ``apply`` once per CONTENT
    frame; ``reset`` = no history (the next frame sets s = f_q). ``advance`` = the hook's GPU advance test: a frame
    whose 1/8-res map is bit-identical to the previous one steps nothing."""
    s: Optional[np.ndarray] = None
    l8p: Optional[np.ndarray] = None
    lqp: Optional[np.ndarray] = None
    spec_p: Optional[tuple] = None
    t: int = -1
    log: list = field(default_factory=list)
    tables: Optional[tuple] = None

    def reset(self):
        self.s = self.l8p = self.lqp = self.spec_p = None

    def apply(self, fade, mi):
        H, W = fade.shape
        self.t += 1
        fq = down(fade, 4)
        l8, lq = maps_of(mi)
        if self.tables is None or self.tables[0][0].shape[0] != l8.shape[0] or self.tables[1][0].shape[0] != l8.shape[1]:
            self.tables = (twiddles(l8.shape[0]), twiddles(l8.shape[1]), hann2(*l8.shape), spectral_gauss(*l8.shape))
        rec = {"t": self.t}
        if self.s is None:
            self.s = fq.copy()
            self.l8p, self.lqp = l8, lq
            self.spec_p = self._spec(l8)
            rec.update(init=True, cut=False, advance=True)
            self.log.append(rec)
            return hclamp(fade + up((self.s - fq).astype(f32), 4, (H, W)), 0.0, 1.0)
        advance = not np.array_equal(l8.view(np.uint32), self.l8p.view(np.uint32))
        rec["advance"] = advance
        if not advance:
            rec.update(cut=False)
            self.log.append(rec)
            return hclamp(fade + up((self.s - fq).astype(f32), 4, (H, W)), 0.0, 1.0)
        Hq, Wq = l8.shape
        spec_cur = self._spec(l8)
        dy, dx, c0, _ = phase_shift(self.l8p, l8, self.spec_p, spec_cur, self.tables)
        ok = bool(abs(float(dy)) < Hq / 4.0 and abs(float(dx)) < Wq / 4.0 and math.isfinite(float(dy)) and math.isfinite(float(dx)))
        m0, _, _ = _overlap_mean(l8, self.l8p, f32(0.0), f32(0.0))
        m1 = f32(np.inf)
        if ok and (abs(float(dy)) > 1e-6 or abs(float(dx)) > 1e-6):
            m1, _, _ = _overlap_mean(l8, self.l8p, dy, dx)
            ok = bool(m1 < m0)
        used = (dy, dx) if ok else (f32(0.0), f32(0.0))
        w, v = warp(self.l8p, used[0], used[1])
        v = _interior(v)
        n = _group_count(v)
        frac = f32(1.0) if n == 0 else f32(f32(_group_count(v & (np.abs((l8 - w).astype(f32)) > f32(T_CD)))) / f32(n))
        emd = hist_emd(l8, self.l8p)
        cut = bool(frac >= f32(T_CFRAC) or emd > f32(T_CH))
        rec.update(pc_dy=float(dy), pc_dx=float(dx), pc_peak=float(c0), shift_used=ok, res0=float(m0),
                   res1=(float(m1) if math.isfinite(float(m1)) else None), shift8=[float(used[0]), float(used[1])],
                   frac_cd=float(frac), emd=float(emd), cut=cut)
        if cut:
            self.s = fq.copy()
        else:
            dq = (f32(2.0) * used[0], f32(2.0) * used[1])
            sw, vq = warp(self.s, *dq)
            lw, _ = warp(self.lqp, *dq)
            r = np.abs((gauss(lq, T_RSIG) - gauss(lw, T_RSIG)).astype(f32))
            a = (f32(T_A0) + f32(1.0 - T_A0) * smooth(T_R[0], T_R[1], r)).astype(f32)
            a = np.where(vq, a, f32(1.0)).astype(f32)
            self.s = (sw + a * (fq - sw)).astype(f32)
            rec.update(a_mean=float(np.mean(a, dtype=np.float64)))
        self.l8p, self.lqp, self.spec_p = l8, lq, spec_cur
        self.log.append(rec)
        return hclamp(fade + up((self.s - fq).astype(f32), 4, (H, W)), 0.0, 1.0)

    def _spec(self, a):
        H, W = a.shape
        tw_h, tw_w, win, _G = self.tables
        mean = f32(_group_sum(a) / f32(H * W))
        return dft2(((a - mean).astype(f32) * win).astype(f32), tw_h, tw_w)


# ================================================================================================= the whole layer
class FrozenDrives:
    """Zone state given, not recomputed (r10/temu.FixedMaps pattern): round r's kernels see drives[r] and its B_true
    the boost boosts[r] (None = the file has no LUT)."""

    def __init__(self, d0, d1, boost0=None, boost1=None):
        self.d = [np.asarray(d0, f32), np.asarray(d1, f32)]
        self.b = [boost0, boost1]


class FadeEmu:
    """One frame of FaldRunPasses with the flank fade ON (ASF10 + T11_TMTC), the layer underneath = :class:`Emu`
    (unchanged: its own float64 arithmetic where the existing WARP replays accept it). Order (TEmu.run / PORT_PLAN
    2.2): Balance -> the fade, F, T (once per content frame) -> round 0 -> kc -> reach_0 -> FadePost -> round 1's
    statistic -> round 1 -> kc -> reach_1 -> FadePost -> FP16 (truncating, as gpuemu)."""

    def __init__(self, emu: Emu, hlsl_nan: bool = False):
        self.e = emu
        self.hlsl_nan = bool(hlsl_nan)   # PanelNits as the HLSL computes it: max(M scrgb, 0) drops a NaN (-> 0)

    def panel_nits(self, frame):
        if not self.hlsl_nan:
            return self.e.panel_nits(frame)
        from .gpuemu import BT709_TO_BT2020
        assert self.e.transfer == 0, "hlsl_nan models the PQ path only"
        n = np.fmax(np.asarray(frame, np.float64) @ BT709_TO_BT2020.T, 0.0) * 80.0
        return np.ascontiguousarray(n.transpose(2, 0, 1))

    def run(self, frame_scrgb, star: Optional[StarfieldParams] = None, tstate: Optional[TState] = None,
            frozen: Optional[FrozenDrives] = None, fp16_out: bool = True, img=None):
        """``img`` (3, H, W) float64 = the frame the layer works on, given (else Balance(PanelNits(frame)) in nits, as the
        device forms it). The study twin forms it through an scRGB round trip (TEmu: panel_nits(nits_to_scrgb(Balance))):
        the 7-digit 709 / 2020 matrices leak ~1.2e-6 across channels there, which a saturated colour whose BT.709 channel
        cancels to ~1e-4 shows as tens to hundreds of FP16 steps in that channel — gate 1 passes the twin's own img."""
        e = self.e
        plan = None
        if img is None:
            src = self.panel_nits(frame_scrgb)
            img = src
            if star is not None:
                sp = clamp_star(star)
                plan = e.star_plan(e.star_stat(src, sp), sp)
                img, _ = e.balance(src, plan, sp)
        img32 = np.asarray(img, f32)
        mi = hmax(img32[0], hmax(img32[1], img32[2]))
        fade = fade9(mi)
        fade_t = tstate.apply(fade, mi) if tstate is not None else fade
        F = floor_lit(mi)
        dec = reach_dec(mi)
        sT1 = e.sample(e.flatT.astype(np.float64))
        sE1 = e.sample(e.flatE.astype(np.float64))
        out = {"mi": mi, "fade": fade, "fade_t": fade_t, "F": F, "dec": dec, "s": None if tstate is None else tstate.s}

        def round_(r, stat_img, img_for_correct):
            if frozen is not None:
                d = frozen.d[r]
                boost = frozen.b[r]
            else:
                d, _st = e.stat_drive(stat_img)
                boost, _z, _a = e.frame_boost(stat_img)
            bT, bE = e.fields(d, d, boost)
            _, gB = e.gain(bT, bE)
            cB = e.ceil_est(bE)
            sT = e.sample(bT.astype(np.float64)) / np.maximum(sT1, 1e-6)
            sE = e.sample(bE.astype(np.float64)) / np.maximum(sE1, 1e-6)
            g = e.sample(gB.astype(np.float64))
            gc = e.sample(cB.astype(np.float64))
            req, _ = e.correct(img_for_correct, sT, sE, g, gc)
            kc = knee_cap(e, img32, sT, sE, g, gc)
            reach = reach_of(kc, dec, mi.shape)
            post, parts = fade_post(req, img32, fade_t, F, reach)
            out[f"drive{r}"] = np.asarray(d, f32)
            out[f"bT{r}"], out[f"bE{r}"], out[f"gain{r}"], out[f"boost{r}"] = bT, bE, gB, boost
            out[f"kc{r}"] = kc
            out[f"reach{r}"] = reach
            out[f"keep{r}"] = parts["keep"]
            return post
        cor0 = round_(0, img, img)
        req = round_(1, cor0.astype(np.float64), img)
        out["req"] = req
        if fp16_out:
            o = trunc_half(e.nits_to_scrgb32(req.astype(np.float64)))
        else:
            o = e.nits_to_scrgb(req.astype(np.float64))
        out["out"] = np.where(e.in_lattice[..., None], o, np.asarray(frame_scrgb).astype(o.dtype))
        return out
