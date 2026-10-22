"""The always-on core, round 2 — PROTOTYPE (branch fald/principled): numpy GPU-order twin of shared/fald_core2.h on top of
:class:`dlc.fald.gpuemu.Emu`.

The rule (KC2c; DLC results/fald_principled_2026-10-22/PREREG_CORE2.md §2, float64 reference ``pcore.CoreEmu`` with
``KVARIANTS["KC2c"]``): C with
  alpha    near-field BRIGHTENING shrunk by a(G) = clamp(0.8 G / (G + 1.4) - 0.1, 0, 1) of the zone's geometric-mean level G
           (the 2026-09-12 dark-halo rows' measured / modelled ratio), Gaussian 0.7 zone, bilinear between zone centres;
  deficit  per round and zone, over its S x S fine nodes + a one-node ring, the brightening the knee would clip,
           d = [gmax > 0] min(max(0, ln P + gmax - ln 0.9 - ln cmin), gmax), spread twice to the 3 x 3 neighbours with
           exp(-(1 + 4 max(0, |grad ln max(M, 0.05)| - ln 3))), Gaussian 0.7, bilinear; ln g -= min(D, max(ln g, 0));
  zone IIR alpha's state and each round's D: s <- s + a (x - s), a = 1 - 0.65^(k / 2) for k refreshes, reset on the first
           run and at a cut (mean over zones of |d ln max(M, 0.05)| > 0.25).
The shader's order is followed where it decides a value (bilinear weights through Emu.sample_zone with the device's
sub-texel fraction when ``subtexel_bits`` is set; the round-0 statistic over every zone pixel; the cut sum in 2^-14 units);
the reductions themselves are float64 here (the GPU's float32 tree differs by rounding only).
    core = CoreState(); out = CoreEmu(o, subtexel_bits=8, sampler="warp").run(frame, core=core, refreshes=2)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .gpuemu import KNEE_CAP_TRUST, Emu, clamp_star, trunc_half
from .starfield import StarfieldParams

CORE_ALPHA, CORE_DEFICIT, CORE_SOFT = 1, 2, 4
CORE_KC2C = CORE_ALPHA | CORE_DEFICIT
FIXED = 16384.0                 # the cut sum's fixed point (FALD_CORE2_FIXED)


@dataclass
class CoreParams:
    mu_inf: float = 0.8
    L_h: float = 1.4
    z: float = 0.1
    kstart: float = 0.9
    beta: float = 4.0
    tau: float = math.log(3.0)
    sigma: float = 0.7
    cut: float = 0.25
    a2: float = 0.35


def iir_factor(refreshes: int, a2: float = 0.35) -> float:
    """FaldCore2IirFactor: 1 - (1 - a2)^(k / 2); k = 0 -> 0 (nothing advances), k > 1000 -> 1."""
    k = int(refreshes)
    if k <= 0:
        return 0.0
    if k > 1000:
        return 1.0
    return float(np.float32(1.0 - (1.0 - a2) ** (k / 2.0)))


@dataclass
class CoreState:
    alpha: Optional[np.ndarray] = None
    lz: Optional[np.ndarray] = None
    D: list = field(default_factory=lambda: [None, None])
    valid: bool = False

    def invalidate(self):
        self.valid = False


def gauss_zone(Z, sigma=0.7):
    """scipy gaussian_filter(Z, sigma, mode='nearest', truncate 4): radius 3 for 0.7 (the shader's direct 7 x 7)."""
    r = int(4.0 * sigma + 0.5)
    x = np.arange(-r, r + 1, dtype=np.float64)
    w = np.exp(-0.5 * x * x / (sigma * sigma)); w /= w.sum()
    Z = np.asarray(Z, np.float64)
    p = np.pad(Z, ((r, r), (0, 0)), mode="edge")
    a = sum(w[i] * p[i: i + Z.shape[0]] for i in range(2 * r + 1))
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    return sum(w[i] * p[:, i: i + Z.shape[1]] for i in range(2 * r + 1))


def max3(Z):
    p = np.pad(Z, 1, mode="edge")
    out = np.full(Z.shape, -np.inf)
    for dy in range(3):
        for dx in range(3):
            out = np.maximum(out, p[dy: dy + Z.shape[0], dx: dx + Z.shape[1]])
    return out


def grad_mag(a):
    gy, gx = np.gradient(np.asarray(a, np.float64))
    return np.hypot(gy, gx)


class CoreEmu(Emu):
    """Emu + the core. ``flags`` 0 or ``core=None`` in run() = Emu.run exactly."""

    def __init__(self, o, flags: int = CORE_KC2C, params: Optional[CoreParams] = None, **kw):
        super().__init__(o, **kw)
        if int(flags) & CORE_SOFT:
            raise NotImplementedError("the soft leak floor (KC2) is not part of the prototype (KC2c)")
        self.flags = int(flags)
        self.cp = params or CoreParams()

    # ---- the stat pass, round 0 (+)
    def core_stats(self, img):
        mc = img.max(axis=0)[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw].astype(np.float64)
        b = mc.reshape(self.rows, self.ch, self.cols, self.cw)
        n = self.ch * self.cw
        P = np.minimum(b.max(axis=(1, 3)), self.white)
        M = b.sum(axis=(1, 3)) / n
        lnG = np.log(np.maximum(b, 0.05)).sum(axis=(1, 3)) / n
        return P, M, lnG

    def core_zone(self, P, M, lnG, st: CoreState, a):
        cp = self.cp
        lz = np.log(np.maximum(M, 0.05))
        cut = False
        if st.valid and st.lz is not None:
            q = np.floor(np.minimum(np.abs(lz - st.lz), 32.0) * FIXED + 0.5).sum()      # the shader's fixed-point sum
            cut = q / FIXED / lz.size > cp.cut
        reset = (not st.valid) or cut
        G = np.exp(lnG)
        raw = np.clip(cp.mu_inf * G / (G + cp.L_h) - cp.z, 0.0, 1.0)
        st.alpha = raw if (reset or st.alpha is None) else st.alpha + a * (raw - st.alpha)
        st.lz = lz
        alpha_z = np.clip(gauss_zone(st.alpha, cp.sigma), 0.0, 1.0)
        k = np.exp(-(1.0 + cp.beta * np.maximum(grad_mag(lz) - cp.tau, 0.0)))
        return alpha_z, k, np.log(np.maximum(P, 1e-6)), reset, cut

    def _z2n(self, Z):
        """zone field at the fine nodes, bilinear between zone centres (node f -> zone coordinate (f + 0.5) / S - 0.5)."""
        S = self.sub

        def ax(n_out, n_in):
            t = (np.arange(n_out) + 0.5) / S - 0.5
            f0 = np.floor(t)
            i0 = np.clip(f0.astype(int), 0, n_in - 1); i1 = np.clip(f0.astype(int) + 1, 0, n_in - 1)
            fr = np.where(t < 0, 0.0, np.clip(t - f0, 0.0, 1.0))
            return i0, i1, fr
        (y0, y1, fy), (x0, x1, fx) = ax(self.rows * S, self.rows), ax(self.cols * S, self.cols)
        Zy = Z[y0] * (1 - fy)[:, None] + Z[y1] * fy[:, None]
        return Zy[:, x0] * (1 - fx)[None] + Zy[:, x1] * fx[None]

    def core_deficit(self, gB, cB, alpha_z, k, lnP, reset, st: CoreState, rnd, a):
        cp = self.cp
        S = self.sub
        gn = np.log(np.maximum(gB.astype(np.float64), 1e-6))
        if self.flags & CORE_ALPHA:
            gn = np.where(gn > 0, self._z2n(alpha_z) * gn, gn)
        gmax = max3(gn).reshape(self.rows, S, self.cols, S).max(axis=(1, 3))
        cn = -max3(-cB.astype(np.float64))
        cmin = np.minimum(self.white * cn.reshape(self.rows, S, self.cols, S).min(axis=(1, 3)) / KNEE_CAP_TRUST, self.white)
        d = np.where(gmax > 0, np.maximum(lnP + gmax - math.log(cp.kstart) - np.log(np.maximum(cmin, 1e-9)), 0.0), 0.0)
        d = np.minimum(d, np.maximum(gmax, 0.0))
        D = d if (reset or st.D[rnd] is None) else st.D[rnd] + a * (d - st.D[rnd])
        st.D[rnd] = D
        for _ in range(2):
            D = np.maximum(D, max3(D) * k)
        return np.maximum(gauss_zone(D, cp.sigma), 0.0)

    def core_gain(self, g, alpha_px, D_px):
        up = g > 1.0
        lg = np.log(np.where(up, g, 1.0))
        if self.flags & CORE_ALPHA:
            lg = lg * np.clip(alpha_px, 0.0, 1.0)
        if self.flags & CORE_DEFICIT:
            lg = np.maximum(lg - np.maximum(D_px, 0.0), 0.0)
        return np.where(up, np.exp(lg), g)

    def run(self, frame_scrgb, fp16_out=True, temporal=None, star: Optional[StarfieldParams] = None, refreshes: int = 1,
            core: Optional[CoreState] = None):
        if not self.flags or core is None:
            return super().run(frame_scrgb, fp16_out=fp16_out, temporal=temporal, star=star, refreshes=refreshes)
        a = iir_factor(refreshes, self.cp.a2)
        img = self.panel_nits(frame_scrgb)
        star_out = None
        if star is not None:
            sp = clamp_star(star)
            src = img
            st_ = self.star_stat(src, sp)
            plan = self.star_plan(st_, sp)
            img, scale = self.balance(src, plan, sp)
            star_out = {"src": src, "stat": st_, "plan": plan, "scale": scale, "params": sp}
        if temporal is not None and hasattr(temporal, "advance"):
            temporal.advance(refreshes)
        d0, st0 = self.stat_drive(img)
        P, M, lnG = self.core_stats(img)
        alpha_z, k, lnP, reset, cut = self.core_zone(P, M, lnG, core, a)
        alpha_px = self.sample_zone(alpha_z)
        boost0, zones0, active0 = self.frame_boost(img)
        dT0, dE0 = temporal.pair(d0) if temporal is not None else (d0, d0)
        bT0, bE0 = self.fields(dT0, dE0, boost0)
        _, gB0 = self.gain(bT0, bE0)
        cB0 = self.ceil_est(bE0)
        D0 = self.core_deficit(gB0, cB0, alpha_z, k, lnP, reset, core, 0, a)
        sT, sE, g = self.sampled(bT0, bE0, gB0)
        g = self.core_gain(g, alpha_px, self.sample_zone(D0))
        gc = self.sample(cB0.astype(np.float64)) if self.c15 else None
        cor0, _ = self.correct(img, sT, sE, g, gc)
        d1, st1 = self.stat_drive(cor0)
        boost1, zones1, active1 = self.frame_boost(cor0)
        dT1, dE1 = temporal.pair(d1) if temporal is not None else (d1, d1)
        bT1, bE1 = self.fields(dT1, dE1, boost1)
        graw1, gB1 = self.gain(bT1, bE1)
        cB1 = self.ceil_est(bE1)
        D1 = self.core_deficit(gB1, cB1, alpha_z, k, lnP, reset, core, 1, a)
        sT, sE, g = self.sampled(bT1, bE1, gB1)
        g = self.core_gain(g, alpha_px, self.sample_zone(D1))
        gc = self.sample(cB1.astype(np.float64)) if self.c15 else None
        req, wfade = self.correct(img, sT, sE, g, gc)
        if temporal is not None:
            temporal.commit(d1)
        core.valid = True
        if fp16_out:
            out = trunc_half(self.nits_to_scrgb32(req))
        else:
            out = self.nits_to_scrgb(req)
        out = np.where(self.in_lattice[..., None], out, frame_scrgb.astype(out.dtype))
        return {"img": img, "req": req, "out": out, "out_nits": self.panel_nits(out.astype(np.float64)), "gain": gB1,
                "ceil_est": cB1, "drive0": d0, "drive1": d1, "star": star_out,
                "core": {"P": P, "M": M, "lnG": lnG, "alpha_z": alpha_z, "k": k, "reset": reset, "cut": cut,
                         "D0": D0, "D1": D1, "a": a}}
