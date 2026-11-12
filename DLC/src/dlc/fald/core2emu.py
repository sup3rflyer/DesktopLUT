"""The always-on core KC5: numpy GPU-order twin of shared/fald_core2.h on top of :class:`dlc.fald.gpuemu.Emu`.

The rule (KC5, round 5; DLC results/fald_principled_2026-10-22/PREREG_CORE5.md, float64 reference ``pcore5`` ``V5["KC5"]``):
C with
  alpha    near-field BRIGHTENING shrunk by a(G) = clamp(0.8 G / (G + 1.4) - 0.1, 0, 1) of the zone's geometric-mean level G
           (the 2026-09-12 dark-halo rows' measured / modelled ratio), raised by the level law a5 to 1 between 0.5 and 2 nit
           (smoothstep in ln G), Gaussian 0.7 zone, bilinear between zone centres;
  cap      per round on the fine grid: the headroom h = max(0, ln(0.9 c3) - ln P3) (P3 = the 3 x 3 node max of the node
           peaks, c3 = the 3 x 3 node min of the ceiling), A = min_n [h_n + s |x - n|_1 px] over the whole lattice
           (separable min-plus, x then y); ln g' = min(alpha max(ln g, 0), A) for g > 1.
Stateless: every run is a pure function of its frame. The shader's order is followed where it decides a value (bilinear
weights through Emu.sample_zone with the device's sub-texel fraction when ``subtexel_bits`` is set; the round-0 statistic
over every zone pixel; the cap's per-node step in float32); the reductions themselves are float64 here (the GPU's float32
tree differs by rounding only).
    out = CoreEmu(o, subtexel_bits=8, sampler="warp").run(frame, core=CoreState())
(The earlier rounds' twins — KC2c's zone deficit with a zone IIR, KC3's fine-grid deficit — went with their test modes on
2026-11-12; the C++ no longer has them.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .gpuemu import KNEE_CAP_TRUST, Emu, clamp_star, commit_temporal, trunc_half
from .starfield import StarfieldParams

CORE_ALPHA, CORE_FINE = 1, 8
# shared/fald_core2.cpp C1 / CX / CY: the brightening CAP on the fine grid; and the alpha law a5
CORE_CAP = 64
CORE_A5 = 128
CORE_KC5 = CORE_ALPHA | CORE_FINE | CORE_CAP | CORE_A5   # = 201 (shared/fald_core_mode.h FALD_CORE2_KC5)


@dataclass
class CoreParams:
    mu_inf: float = 0.8
    L_h: float = 1.4
    z: float = 0.1
    kstart: float = 0.9
    beta: float = 4.0
    tau: float = math.log(3.0)
    sigma: float = 0.7
    cap_s: float = 0.10                 # FALD_CORE2_CAP_S: ln gain per 100 px
    a5_g0: float = 0.5                  # FALD_CORE2_A5_G0 / G1 (nit)
    a5_g1: float = 2.0


@dataclass
class CoreState:
    """The core is stateless; the object only says "the core runs" to :meth:`CoreEmu.run` (None = the layer without it)."""
    valid: bool = False


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

    def __init__(self, o, flags: int = CORE_KC5, params: Optional[CoreParams] = None, **kw):
        super().__init__(o, **kw)
        if int(flags) not in (0, CORE_KC5):
            raise ValueError(f"core flags {flags}: only KC5 ({CORE_KC5}) or 0 (the core off) exist")
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

    def core_zone(self, P, M, lnG):
        """Z1 + Z2: the zone's alpha (with the level law a5), its Gaussian, the spread gate k (dumped; the cap does not use it)."""
        cp = self.cp
        lz = np.log(np.maximum(M, 0.05))
        G = np.exp(lnG)
        raw = np.clip(cp.mu_inf * G / (G + cp.L_h) - cp.z, 0.0, 1.0)
        t = np.clip((lnG - math.log(cp.a5_g0)) / (math.log(cp.a5_g1) - math.log(cp.a5_g0)), 0.0, 1.0)
        raw = raw + (1.0 - raw) * (t * t * (3.0 - 2.0 * t))
        alpha_z = np.clip(gauss_zone(raw, cp.sigma), 0.0, 1.0)
        k = np.exp(-(1.0 + cp.beta * np.maximum(grad_mag(lz) - cp.tau, 0.0)))
        return alpha_z, k, raw

    def node_peaks(self, img):
        """per fine node the max of min(mc, white) over the pixels whose NEAREST node it is (the stat pass, round 0)"""
        S = self.sub
        mc = np.minimum(img.max(axis=0), self.white)[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw]
        ty = (np.arange(self.rows * self.ch) + 0.5) * S / self.ch - 0.5
        tx = (np.arange(self.cols * self.cw) + 0.5) * S / self.cw - 0.5
        ny = np.clip(np.floor(ty + 0.5).astype(int), 0, self.rows * S - 1)
        nx = np.clip(np.floor(tx + 0.5).astype(int), 0, self.cols * S - 1)
        out = np.zeros((self.rows * S, self.cols * S))
        np.maximum.at(out, (ny[:, None].repeat(mc.shape[1], 1), nx[None, :].repeat(mc.shape[0], 0)), mc)
        return out

    # ---- the brightening cap (shared/fald_core2.cpp C1 / CX / CY; the study twin pcore5 V5["KC5"])
    def cap_steps(self):
        """the min-plus cost per node step (x, y): s / 100 x the node pitch in px, in the shader's float32 order"""
        f32 = np.float32
        sx = f32(f32(f32(self.cp.cap_s) * f32(0.01)) * f32(self.cols * self.cw)) / f32(self.cols * self.sub)
        sy = f32(f32(f32(self.cp.cap_s) * f32(0.01)) * f32(self.rows * self.ch)) / f32(self.rows * self.sub)
        return float(sx), float(sy)

    @staticmethod
    def minplus_l1(h, cx, cy):
        """min_n [h_n + cx |dx| + cy |dy|] over the whole lattice, x then y (the shader's forward / backward scans)"""
        def ax1(a, c, axis):
            n = a.shape[axis]
            i = np.arange(n, dtype=np.float64).reshape((1, n) if axis == 1 else (n, 1))
            fwd = np.minimum.accumulate(a - i * c, axis=axis) + i * c
            bwd = np.flip(np.minimum.accumulate(np.flip(a + i * c, axis=axis), axis=axis), axis=axis) - i * c
            return np.minimum(fwd, bwd)
        return ax1(ax1(np.asarray(h, np.float64), cx, 1), cy, 0)

    def cap_field(self, pn, cB):
        """C1 (the headroom h per node), then CX / CY (the min-plus scans, x then y) -> the cap A on the fine grid"""
        cp = self.cp
        cn = np.minimum(self.white * cB.astype(np.float64) / KNEE_CAP_TRUST, self.white)
        P3 = max3(pn)
        c3 = -max3(-cn)
        h = np.maximum(math.log(cp.kstart) + np.log(np.maximum(c3, 1e-9)) - np.log(np.maximum(P3, 1e-6)), 0.0)
        sx, sy = self.cap_steps()
        return self.minplus_l1(h, sx, sy)

    def core_gain(self, g, alpha_px, A_px):
        up = g > 1.0
        lg = np.log(np.where(up, g, 1.0))
        lg = lg * np.clip(alpha_px, 0.0, 1.0)
        lg = np.minimum(lg, np.maximum(A_px, 0.0))
        return np.where(up, np.exp(lg), g)

    def run(self, frame_scrgb, fp16_out=True, temporal=None, star: Optional[StarfieldParams] = None, refreshes: int = 1,
            core: Optional[CoreState] = None, new_content: bool = True):
        if not self.flags or core is None:
            return super().run(frame_scrgb, fp16_out=fp16_out, temporal=temporal, star=star, refreshes=refreshes,
                               new_content=new_content)
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
        alpha_z, k, alpha_raw = self.core_zone(P, M, lnG)
        alpha_px = self.sample_zone(alpha_z)
        pn = self.node_peaks(img)
        boost0, zones0, active0 = self.frame_boost(img)
        dT0, dE0 = temporal.pair(d0) if temporal is not None else (d0, d0)
        bT0, bE0 = self.fields(dT0, dE0, boost0)
        _, gB0 = self.gain(bT0, bE0)
        cB0 = self.ceil_est(bE0)
        A0 = self.cap_field(pn, cB0)
        sT, sE, g = self.sampled(bT0, bE0, gB0)
        g = self.core_gain(g, alpha_px, self.sample(A0))
        gc = self.sample(cB0.astype(np.float64)) if self.c15 else None
        cor0, _ = self.correct(img, sT, sE, g, gc)
        d1, st1 = self.stat_drive(cor0)
        boost1, zones1, active1 = self.frame_boost(cor0)
        dT1, dE1 = temporal.pair(d1) if temporal is not None else (d1, d1)
        bT1, bE1 = self.fields(dT1, dE1, boost1)
        graw1, gB1 = self.gain(bT1, bE1)
        cB1 = self.ceil_est(bE1)
        A1 = self.cap_field(pn, cB1)
        sT, sE, g = self.sampled(bT1, bE1, gB1)
        g = self.core_gain(g, alpha_px, self.sample(A1))
        gc = self.sample(cB1.astype(np.float64)) if self.c15 else None
        req, wfade = self.correct(img, sT, sE, g, gc)
        if temporal is not None:
            commit_temporal(temporal, d1, new_content)
        core.valid = True
        if fp16_out:
            out = trunc_half(self.nits_to_scrgb32(req))
        else:
            out = self.nits_to_scrgb(req)
        out = np.where(self.in_lattice[..., None], out, frame_scrgb.astype(out.dtype))
        return {"img": img, "req": req, "out": out, "out_nits": self.panel_nits(out.astype(np.float64)), "gain": gB1,
                "ceil_est": cB1, "drive0": d0, "drive1": d1, "star": star_out,
                "core": {"P": P, "M": M, "lnG": lnG, "alpha_raw": alpha_raw, "alpha_z": alpha_z, "k": k,
                         "A0": A0, "A1": A1, "D0": A0, "D1": A1, "node_peaks": pn}}
