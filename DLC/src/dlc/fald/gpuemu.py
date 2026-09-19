"""numpy emulation of the DesktopLUT FALD layer (src/fald_shader.h + FaldRunPasses in src/fald.cpp), both
transfers (0 PQ / HDR, 1 gamma / ACM SDR). Every step follows the HLSL line for line; float32 where the GPU
stores textures (R32F), the output frame FP16. Inputs: a parsed panel file (:func:`dlc.fald.panelfile.read_panel_file`)
and a full-resolution scRGB frame (H, W, 3).

Origin: the 2026-09-15 SDR verification (work guide: "GPU = Python is proven at 2 nits" — bit-exact vs a real
``fald_dump``). Kept in the tree so the temporal drive state (:class:`GpuDriveState`, pass 1b) and later
shader changes have an offline GPU-order reference next to the model-side one (:mod:`dlc.fald.temporal`).
White-pedestal mode only (pedMode 0).

Black-frame LED boost (FLD4 panel files, work guide C12): the statistic pass also writes each zone's NON-BLACK flag
(full-resolution pixel counts — the model's :meth:`FaldModel.active_zone_fraction` counts scale-5 raster pixels, so
the two agree on lattice-aligned / >= 5-px content), pass 1a turns the count into the frame's boost
(:func:`dlc.fald.panelfile.boost_of_count`), the conv pass multiplies B_true by it — per round, on the frame the
panel receives (round 0 the source, round 1 the corrected frame), never filtered by the temporal state. A file
without a LUT runs none of it."""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.signal import convolve2d

from .panelfile import boost_of_count
from .temporal import MODE_BOTH, MODE_OFF, MODE_TRUE_ONLY, alpha_from_tau

f32 = np.float32
KNEE_START = 0.9          # FALD_KNEE_START / FALD_KNEE_CAP_TRUST (fald_shader.h; correct.py pins them equal)
KNEE_CAP_TRUST = 1.0


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def srgb_oetf(L):
    L = np.clip(L, 0.0, 1.0)
    return np.where(L <= 0.0031308, 12.92 * L, 1.055 * np.power(L, 1.0 / 2.4) - 0.055)


def srgb_eotf(V):
    V = np.clip(V, 0.0, 1.0)
    return np.where(V <= 0.04045, V / 12.92, np.power((V + 0.055) / 1.055, 2.4))


BT709_TO_BT2020 = np.array([[0.6274040, 0.3292820, 0.0433136], [0.0690970, 0.9195400, 0.0113612], [0.0163916, 0.0880132, 0.8955950]])
BT2020_TO_BT709 = np.array([[1.6604910, -0.5876411, -0.0728499], [-0.1245505, 1.1328999, -0.0083494], [-0.0181508, -0.1005789, 1.1187297]])


def trunc_half(v):
    """float -> IEEE half, truncating toward zero (normal range) — how the FP16 render target stored the dump."""
    v = np.asarray(v, dtype=np.float64)
    a = np.abs(v)
    _, e = np.frexp(np.maximum(a, 2.0 ** -14))      # a = m * 2^e, m in [0.5, 1)
    step = 2.0 ** (e - 1) / 1024.0
    step = np.maximum(step, 2.0 ** -14 / 1024.0)
    return (np.sign(v) * np.floor(a / step) * step).astype(np.float16)


class GpuDriveState:
    """The GPU side of :class:`dlc.fald.temporal.DriveState`: pass 1b ``g_faldTemporalSource`` on the R32F drive
    textures. ``tempInit`` (state texture not yet valid) copies the instantaneous drive; otherwise
    ``s + a * (d - s)`` with ``a = d > s ? aRise : aFall``, all float32. ``commit`` = the CopyResource after round 1."""

    def __init__(self, mode: int = MODE_OFF, tau_rise_ms: float = 0.0, tau_fall_ms: float = 0.0, dt_ms: float = 1000.0 / 60.0,
                 delay_frames: int = 0):
        self.mode = int(mode)
        self.alpha_rise = f32(alpha_from_tau(tau_rise_ms, dt_ms))
        self.alpha_fall = f32(alpha_from_tau(tau_fall_ms, dt_ms))
        self.delay = int(delay_frames)
        self.state: Optional[np.ndarray] = None
        self.ring: list[np.ndarray] = []      # committed instantaneous maps, oldest first (C++ FaldResources::delayTex)

    def delayed(self, d: np.ndarray) -> np.ndarray:
        """The t4 binding of the temporal pass: ring[head - delay] once the ring holds enough maps, else driveTex."""
        if self.mode == MODE_OFF or self.delay <= 0 or len(self.ring) < self.delay:
            return d.astype(np.float32)
        return self.ring[-self.delay]

    def filtered(self, d: np.ndarray) -> np.ndarray:
        d = self.delayed(d)
        if self.mode == MODE_OFF or self.state is None:
            return d.copy()
        a = np.where(d > self.state, self.alpha_rise, self.alpha_fall).astype(np.float32)
        return (self.state + a * (d - self.state)).astype(np.float32)

    def pair(self, d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(drive for K_true, drive for K_est) — what RunConv binds at t4 / t10."""
        f = self.filtered(d)
        if self.mode == MODE_TRUE_ONLY:
            return f, d.astype(np.float32)
        if self.mode == MODE_BOTH:
            return f, f
        return d.astype(np.float32), d.astype(np.float32)

    def commit(self, d: np.ndarray) -> None:
        if self.mode == MODE_OFF:
            self.state = None
            self.ring = []
            return
        self.state = self.filtered(d)
        if self.delay > 0:
            self.ring.append(d.astype(np.float32).copy())
            del self.ring[:-self.delay]


class Emu:
    def __init__(self, o, width=3840, height=2160, ped_mode=0):
        self.o = o
        self.W, self.H = width, height
        self.white = float(o["white"]); self.gamma = float(o["sdrGamma"]); self.transfer = o["transfer"]
        self.sub = o["sub"]; self.cols = o["cols"]; self.rows = o["rows"]; self.cw = o["cellW"]; self.ch = o["cellH"]
        self.ox, self.oy = int(o.get("originX", 0)), int(o.get("originY", 0))
        self.floor = float(o["driveFloor"]); self.area0 = float(o["area0"])
        self.fadeLo, self.fadeHi = float(o["fadeLo"]), float(o["fadeHi"])
        self.lumLo, self.lumHi = float(o["lumFadeLo"]), float(o["lumFadeHi"])
        self.gmin, self.gmax = float(o["gainMin"]), float(o["gainMax"])
        self.tmin = float(o["tmin"])
        self.sigma = float(f32(o["gainSmoothCells"]) * f32(o["sub"]))
        self.pedMode = 1 if (ped_mode == 1 and o["hasPedColour"]) else 0
        assert self.pedMode == 0, "channel pedestal mode not emulated"
        # black-frame LED boost (CB word 34 boostN + words 48-51); 0 = no LUT in the file: the passes below are skipped
        self.boostN = int(o.get("boostN", 0)) if o.get("hasBoost") else 0
        self.litNits, self.litFrac = f32(o.get("boostLitNits", 0.35)), f32(o.get("boostLitFrac", 0.0))
        self.dimNits, self.dimFrac = f32(o.get("boostDimNits", 0.011)), f32(o.get("boostDimFrac", 0.19))
        self.flatT = self.conv(np.ones((self.rows, self.cols)), o["kTrue"])
        self.flatE = self.conv(np.ones((self.rows, self.cols)), o["kEst"])
        # bilinear sample tables for pixel centres (SampleLevel, linear, clamp) — FineUV with the lattice origin removed
        S = self.sub
        xt = (np.arange(self.W) - self.ox + 0.5) / (self.cols * self.cw) * (self.cols * S) - 0.5
        yt = (np.arange(self.H) - self.oy + 0.5) / (self.rows * self.ch) * (self.rows * S) - 0.5
        self.bx = self._axis(xt, self.cols * S)
        self.by = self._axis(yt, self.rows * S)
        xs = np.arange(self.W); ys = np.arange(self.H)
        self.in_lattice = ((ys >= self.oy) & (ys < self.oy + self.rows * self.ch))[:, None] & \
                          ((xs >= self.ox) & (xs < self.ox + self.cols * self.cw))[None, :]

    @staticmethod
    def _axis(t, n):
        i0 = np.floor(t).astype(int); fr = t - i0
        return np.clip(i0, 0, n - 1), np.clip(i0 + 1, 0, n - 1), fr

    def sample(self, T):
        y0, y1, fy = self.by; x0, x1, fx = self.bx
        Ty = T[y0] * (1 - fy)[:, None] + T[y1] * fy[:, None]
        return Ty[:, x0] * (1 - fx)[None] + Ty[:, x1] * fx[None]

    # ---- PanelNits / PanelNitsToScRGB
    def panel_nits(self, scrgb):          # (H, W, 3) -> (3, H, W)
        if self.transfer == 1:
            code = srgb_oetf(scrgb.astype(np.float64))
            n = self.white * np.power(np.maximum(code, 0.0), self.gamma)
        else:
            n = np.maximum(scrgb.astype(np.float64) @ BT709_TO_BT2020.T, 0.0) * 80.0
        return np.ascontiguousarray(n.transpose(2, 0, 1))

    def nits_to_scrgb(self, nits):        # (3, H, W) -> (H, W, 3)
        n = nits.transpose(1, 2, 0)
        if self.transfer == 1:
            code = np.power(np.clip(n / self.white, 0.0, 1.0), 1.0 / self.gamma)
            return srgb_eotf(code)
        return (n / 80.0) @ BT2020_TO_BT709.T

    def nits_to_scrgb32(self, nits):
        n = nits.transpose(1, 2, 0).astype(np.float32)
        if self.transfer == 1:
            code = np.power(np.clip(n / f32(self.white), 0, 1), f32(1.0) / f32(self.gamma)).astype(np.float32)
            return np.where(code <= f32(0.04045), code / f32(12.92), np.power((code + f32(0.055)) / f32(1.055), f32(2.4))).astype(np.float32)
        return ((n / f32(80.0)) @ BT2020_TO_BT709.T.astype(np.float32)).astype(np.float32)

    # ---- DriveOf (curve LUT, linear filtering)
    def drive_of(self, stat):
        o = self.o
        N = o["curveN"]; lmin = float(o["curveLogMin"]); lmax = float(o["curveLogMax"])
        stat32 = stat.astype(np.float32)
        u = np.clip((np.log(np.maximum(stat32, f32(1e-3))) - f32(lmin)) / f32(lmax - lmin), 0, 1).astype(np.float64)
        x = u * (N - 1); i0 = np.clip(np.floor(x).astype(int), 0, N - 1); i1 = np.minimum(i0 + 1, N - 1); fr = x - i0
        d = o["curve"][i0] * (1 - fr) + o["curve"][i1] * fr
        return np.where(stat32 < f32(self.floor), 0.0, d).astype(np.float32)

    # ---- CS stat (lattice cells only; pixels outside the frame do not count)
    def stat_drive(self, img):
        s = np.minimum(img.max(axis=0), self.white)
        s = s[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw]
        blocks = s.reshape(self.rows, self.ch, self.cols, self.cw)
        lit = blocks > self.floor
        m = np.where(lit, blocks, 0.0).max(axis=(1, 3))
        tot = np.where(lit, blocks, 0.0).astype(np.float32).sum(axis=(1, 3), dtype=np.float32)
        stat = np.minimum(m, tot / f32(self.area0))
        return self.drive_of(stat), stat

    # ---- CS stat, the boost part (u1): per zone, LIT-or-DIM on the pixel's brightest channel (NOT capped at white)
    def stat_active(self, img):
        mc = img.max(axis=0)[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw].astype(np.float32)
        blocks = mc.reshape(self.rows, self.ch, self.cols, self.cw)
        n = f32(self.cw * self.ch)
        lit_f = (blocks > self.litNits).sum(axis=(1, 3)).astype(np.float32) / n
        dim_f = (blocks > self.dimNits).sum(axis=(1, 3)).astype(np.float32) / n
        return (lit_f > self.litFrac) | (dim_f > self.dimFrac)

    # ---- CS boost (pass 1a): zone count -> staircase. (boost float32, count, flags); (None, -1, None) without a LUT
    def frame_boost(self, img):
        if not self.boostN:
            return None, -1, None
        active = self.stat_active(img)
        count = int(active.sum())
        return boost_of_count(self.o, count), count, active

    # ---- CS conv (zero outside the lattice), kernel chosen by the sub-offset
    def conv(self, d, K):
        S = self.sub
        out = np.zeros((self.rows * S, self.cols * S), dtype=np.float64)
        for oy in range(S):
            for ox in range(S):
                out[oy::S, ox::S] = convolve2d(d.astype(np.float64), K[oy, ox].astype(np.float64), mode="same", boundary="fill")
        return out.astype(np.float32)

    # ---- gain + blur
    def gain(self, bT, bE):
        t = bT.astype(np.float64) / np.maximum(self.flatT, 1e-6)
        e = bE.astype(np.float64) / np.maximum(self.flatE, 1e-6)
        t = np.maximum(t, 0.0)
        g = np.clip(e / np.maximum(t, 1e-9), self.gmin, self.gmax)
        w = smoothstep(self.fadeLo, self.fadeHi, e)
        raw = 1.0 + (g - 1.0) * w
        return raw.astype(np.float32), self.blur(raw).astype(np.float32)

    def blur(self, g):
        if self.sigma <= 0:
            return g
        R = int(np.ceil(3.0 * np.float32(self.sigma)))
        k = np.exp(-0.5 * np.arange(-R, R + 1) ** 2 / (self.sigma * self.sigma)); k /= k.sum()
        gp = np.pad(g, ((0, 0), (R, R)), mode="edge")
        h = sum(k[i] * gp[:, i: i + g.shape[1]] for i in range(2 * R + 1))
        hp = np.pad(h, ((R, R), (0, 0)), mode="edge")
        return sum(k[i] * hp[i: i + g.shape[0]] for i in range(2 * R + 1))

    # ---- Correct (white pedestal mode; C10/C11 one-scale ceiling rule)
    def correct(self, img, bT, bE, gain):
        W = self.white
        maxc = img.max(axis=0)
        s = np.minimum(maxc, W)
        bT = np.maximum(bT, 0.0)
        wfade = smoothstep(self.fadeLo, self.fadeHi, bE)
        if self.lumHi > self.lumLo:
            wlum = smoothstep(self.lumLo, self.lumHi, maxc)
            gain = 1.0 + (gain - 1.0) * wlum
            wfade = wfade * wlum
        # PedestalAdjust mode 0
        pedRef = W * self.drive_of(s).astype(np.float64) * self.tmin
        ped = W * bT * self.tmin
        delta = pedRef - ped                          # same on all channels
        f = np.ones_like(delta)
        neg = delta < 0
        for c in range(3):
            f = np.where(neg, np.minimum(f, img[c] / np.where(neg, -delta, 1.0)), f)
        term = delta * f * wfade                      # PedestalTerm mode 0
        u = img + term[None]
        m = u.max(axis=0)
        cap = W * np.maximum(bE, 1e-9)
        C = np.minimum(W, cap / KNEE_CAP_TRUST) if KNEE_CAP_TRUST > 0 else np.full_like(cap, W)
        a = m * gain
        t = np.maximum(a / C - KNEE_START, 0.0) / (1.0 - KNEE_START)
        K = np.where(a > KNEE_START * C, C * (KNEE_START + (1.0 - KNEE_START) * t / (1.0 + t)), a)
        ge = np.where((gain > 1.0) & (m > 1e-9), np.maximum(m, K) / np.where(m > 1e-9, m, 1.0), gain)
        return np.maximum(u * ge[None], 0.0), wfade

    def fields(self, drive_true, drive_est=None, boost=None):
        """``boost``: the round's LED boost (float32) — multiplies B_true only, as the conv pass does when the file
        has a LUT (``None`` = no LUT: no multiply at all; the flat-lattice fields are always built without it)."""
        d_est = drive_true if drive_est is None else drive_est
        bT = self.conv(drive_true, self.o["kTrue"])
        if boost is not None:
            bT = (bT * f32(boost)).astype(np.float32)
        return bT, self.conv(d_est, self.o["kEst"])

    def sampled(self, bT, bE, gainB):
        sT = self.sample(bT.astype(np.float64)) / np.maximum(self.sample(self.flatT.astype(np.float64)), 1e-6)
        sE = self.sample(bE.astype(np.float64)) / np.maximum(self.sample(self.flatE.astype(np.float64)), 1e-6)
        g = self.sample(gainB.astype(np.float64))
        return sT, sE, g

    def run(self, frame_scrgb, fp16_out=True, temporal: Optional[GpuDriveState] = None):
        """One frame of FaldRunPasses. ``temporal``: a GpuDriveState carried across calls (pass 1b after each stat
        round; the state commits after round 1 — the CopyResource in the C++)."""
        img = self.panel_nits(frame_scrgb)
        d0, st0 = self.stat_drive(img)
        boost0, zones0, active0 = self.frame_boost(img)               # round 0: the source frame
        dT0, dE0 = temporal.pair(d0) if temporal is not None else (d0, d0)
        bT0, bE0 = self.fields(dT0, dE0, boost0)
        _, gB0 = self.gain(bT0, bE0)
        sT, sE, g = self.sampled(bT0, bE0, gB0)
        cor0, _ = self.correct(img, sT, sE, g)
        d1, st1 = self.stat_drive(cor0)
        boost1, zones1, active1 = self.frame_boost(cor0)              # round 1: the corrected frame the panel receives
        dT1, dE1 = temporal.pair(d1) if temporal is not None else (d1, d1)
        bT1, bE1 = self.fields(dT1, dE1, boost1)
        graw1, gB1 = self.gain(bT1, bE1)
        sT, sE, g = self.sampled(bT1, bE1, gB1)
        req, wfade = self.correct(img, sT, sE, g)
        if temporal is not None:
            temporal.commit(d1)
        if fp16_out:
            # float32 encode (HLSL) then float->half TRUNCATION: matches the real GPU dump 98.4 % bit-for-bit (round-to-nearest 56 %)
            out = self.nits_to_scrgb32(req)
            out = trunc_half(out)
        else:
            out = self.nits_to_scrgb(req)
        out = np.where(self.in_lattice[..., None], out, frame_scrgb.astype(out.dtype))   # pass-through outside the lattice
        out_nits = self.panel_nits(out.astype(np.float64))
        return {"img": img, "drive0": d0, "drive1": d1, "drive_true": dT1, "drive_est": dE1, "stat1": st1, "bT": bT1, "bE": bE1,
                "gain_raw": graw1, "gain": gB1, "req": req, "out": out, "out_nits": out_nits, "px_gain": g, "px_bT": sT,
                "px_bE": sE, "wfade": wfade,
                # black-frame LED boost per round (fald_dump.txt boost_r0/r1, active_zones_r0/r1, fald_active[_r0].f32)
                "boost0": 1.0 if boost0 is None else float(boost0), "boost1": 1.0 if boost1 is None else float(boost1),
                "zones0": zones0, "zones1": zones1, "active0": active0, "active1": active1}
