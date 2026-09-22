"""numpy emulation of the DesktopLUT FALD layer (src/fald_shader.h + FaldRunPasses in src/fald.cpp), both
transfers (0 PQ / HDR, 1 gamma / ACM SDR). Every step follows the HLSL line for line; float32 where the GPU
stores textures (R32F), the output frame FP16. Inputs: a parsed panel file (:func:`dlc.fald.panelfile.read_panel_file`)
and a full-resolution scRGB frame (H, W, 3).

Origin: the 2026-09-15 SDR verification (work guide: "GPU = Python is proven at 2 nits" — bit-exact vs a real
``fald_dump``). Kept in the tree so the temporal drive state (:class:`GpuDriveState`, pass 1b; the panel-clock mode
3 :class:`GpuPanelDriveState`, pass 1c — reference :mod:`dlc.fald.paneltime`) and later
shader changes have an offline GPU-order reference next to the model-side one (:mod:`dlc.fald.temporal`).
White-pedestal mode only (pedMode 0).

Black-frame LED boost (FLD4 panel files, work guide C12): the statistic pass also writes each zone's NON-BLACK flag
(full-resolution pixel counts — the model's :meth:`FaldModel.active_zone_fraction` counts scale-5 raster pixels, so
the two agree on lattice-aligned / >= 5-px content; the file's zone rule, FLD4 word 53: LIT-or-DIM or, work guide C12b,
LIT-or-MEAN with the zone's float32 sum of nits^gamma in the shader's thread / reduction order), pass 1a turns the count into the frame's boost
(:func:`dlc.fald.panelfile.boost_of_count`), the conv pass multiplies B_true by it — per round, on the frame the
panel receives (round 0 the source, round 1 the corrected frame), never filtered by the temporal state. A file
without a LUT runs none of it.

Starfield balancing (work guide S1; the rules = the module docstring of :mod:`dlc.fald.starfield`, the reference):
``run(..., star=StarfieldParams)`` runs the three star passes on the SOURCE frame at FULL resolution
(:meth:`Emu.star_stat` = S0 ``g_faldStarStatSource``; :meth:`Emu.star_plan` = S1 ``g_faldStarWeightSource`` + S2
``g_faldStarPlanSource``) and then feeds ``Balance(source)`` (:meth:`Emu.balance`, the HLSL ``Balance``: the zone
fields sampled bilinearly between zone centres, clamped — the same sampler maths as the fine-grid fields —, the
speck-zone flag of the pixel's OWN zone loaded nearest) to everything downstream: both statistic rounds, the boost
flags, Correct, the output. ``star=None`` runs none of it (the previous emulator, bit for bit). The reference works on
the model's scale-5 raster, the emulator on full-resolution pixels: they agree on raster-aligned content (>= 5-px
features), where the centre pixel of every 5 x 5 block has exactly the raster pixel's bilinear coordinates.

Glow fill (work guide S2; the rules = the module docstring of :mod:`dlc.fald.glowfill`, the reference):
``run(..., glow=GlowFillParams)`` runs the four glow passes after EACH round's conv pass (:meth:`Emu.glow_zones` = G0
``g_faldGlowZoneSource`` zone pedestal, G1 ``g_faldGlowDilateSource`` box maximum on the lattice extended by ``reach``,
G2 ``g_faldGlowErodeSource`` box minimum = the closing, G3 ``g_faldGlowEnvSource`` blur + min + deficit; float32, the
shaders' loop order), then — files with a boost LUT and the mean zone rule only — the count-threshold band
(:meth:`Emu.glow_band` = G4 ``g_faldGlowBandSource``: a full-resolution sweep like the statistic pass -> the zone's k0 and
the neighbour bound A_d; + G5 ``g_faldGlowGuardSource``, the neighbour guard -> the final k, which GlowAdd reads through the
feather of C16, :meth:`Emu.band_scale_px`; formed in EACH round from that round's request) and adds ``GlowAdd`` (:meth:`Emu.glow_add`) to that round's
corrected request — round 0's feeds the round-1 statistic / boost flags, round 1's is the output. ``glow=None`` runs none
of it (the previous emulator, bit for bit). HDR (PQ files) only, like the C++.
EXACTNESS of the fill against a real device (WARP, 2026-09-20): the zone fields agree to <= 1e-5 relative (float32 sum
order). With the default float64 sampler weights the filled pixels agree to <= 0.0006 nit but only 20-45 % are bit-equal
(up to ~2 % apart where the deficit is small at the foot of a ramp): the hardware's bilinear sampler weighs with 8-bit
sub-texel fractions — a 48-texel field stretched over 3840 px shows it. ``Emu(..., subtexel_bits=8)`` forms the sampler
fractions (zone AND fine-grid textures AND the drive-curve LUT) the same way: 99.4 % of the filled pixels and 99.9 % of the
whole frame bit-equal, <= 0.00008 nit. The default stays float64 (the comparisons against the reference need the exact
coordinates); it is not an order-of-operations difference.
HOW the device forms the 8-bit fraction differs (probe 2026-09-23, :func:`sampler_truncates`): a hardware GPU (RTX 5090)
rounds it to nearest on every axis (``sampler="hw"``, the default); the WARP software device TRUNCATES it on x when the
texture's width is a power of two and on y when both its dimensions are. A replay of WARP dumps passes ``sampler="warp"``.
Found on a few-zone lattice (work guide C14): 8 x 6 zones at 960 x 540 = zone textures 8 wide, the fine grid 64 wide, the
curve LUT 1024 — with the rounding model the output sat up to 106 FP16 steps off at the top zone row (a glow deficit
stepping 0 -> 0.18 nit between a lit corner zone and its neighbour, sampled at a fraction of 0.054: half a 1/256 step is
3.6 % of the interpolated fill), with WARP's rule every pixel is within one step, the drives equal, the band scales to a few
float32 ulps (also 7 x 5, 16 x 8 and 4 x 4 lattices, lattice origins != 0, glow off, temporal mode 3).
The 12 x 12 WARP lattices of the earlier gates (12 / 96 texels) round on WARP too — only their curve LUT truncates."""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.signal import convolve2d

from .glowfill import (BAND_HI, BAND_LO, DEFICIT_REL_HI, DEFICIT_REL_LO, FEATHER, GLOW_SIGMA_BASE, GLOW_SIGMA_PER_REACH, GUARD_ITER_MAX,
                       NEIGHBOURS, REQ_FLOOR_FRAC, REQ_LIT_FRAC, WANT_EPS, GlowFillParams, clamp_params as clamp_glow)
from .panelfile import boost_of_count
from .starfield import StarfieldParams
from .temporal import MODE_BOTH, MODE_OFF, MODE_TRUE_ONLY, alpha_from_tau

f32 = np.float32
KNEE_START = 0.9          # FALD_KNEE_START / FALD_KNEE_CAP_TRUST (fald_shader.h; correct.py pins them equal)
KNEE_CAP_TRUST = 1.0
STAR_SPECK_LO = 0.25      # FALD_STAR_SPECK_LO / _HI (fald_shader.h Balance; starfield.balance_image's 0.25 .. 0.5 band)
STAR_SPECK_HI = 0.5
STAR_EVEN_REACH_MAX = 12  # FALD_STAR_EVEN_REACH_MAX / FALD_STAR_REACH_MAX (fald.h; FaldStarfieldClamp)
STAR_REACH_MAX = 4
STAR_FLAT_ABS = 1e-6      # FALD_STAR_FLAT_ABS / _REL (fald_shader.h star statistic; starfield.FLAT_ABS / FLAT_REL): a zone whose
STAR_FLAT_REL = 0.02      # peak is not more than max(ABS, REL * peak) above its darkest pixel has no speck: not star-like
ZONE_SLICE_PX = 4096      # FALD_ZONE_SLICE_PX (fald_shader.h, work guide C14): pixels per thread group of a zone sweep
STAR_PULL_EPS = 1e-5      # FALD_STAR_PULL_EPS (starfield.PULL_EPS): a pull ending this close to the pixel itself is no pull
STAR_GATE_LO, STAR_GATE_HI = 1.0, 2.0   # FALD_STAR_GATE_LO / _HI (starfield.GATE_LO / GATE_HI): the pull threshold over target / background
STAR_FLANK_PX, STAR_FLANK_NEAR_PX = 2, 12   # FALD_STAR_FLANK_PX / _NEAR_PX (starfield.FLANK_PX / FLANK_NEAR_PX): one feature straddling a border
SAMPLER_HW, SAMPLER_WARP = "hw", "warp"     # how the device forms a bilinear sampler's sub-texel fraction (sampler_truncates)


def _pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def sampler_truncates(sampler: str, width: int, height: int) -> tuple[bool, bool]:
    """(x, y): whether the device TRUNCATES a width x height texture's bilinear sub-texel fraction on that axis instead of
    rounding it to nearest. Measured 2026-09-23 (a D3D11 probe: R32F / R32G32F / R32G32B32A32F textures, SampleLevel in pixel
    and compute shaders alike, the 26 sizes of DLC tests/test_fald_sampler_model.py; an independent re-probe, 54 sizes up to
    8192 x 2, clamp / border / wrap, Sample too, agrees): a hardware GPU (RTX 5090) rounds on every axis; WARP truncates
    (floor, toward -inf) x when the WIDTH is a power of two and y when BOTH dimensions are (8 x 6: x truncated, y rounded;
    6 x 8 and 48 x 64: both rounded; 4 x 4, 16 x 16, 128 x 32: both truncated). Ties: WARP rounds half to even on its float32
    coordinate, the 5090 half up; the twin (np.round, float64 coordinates) differs only within float32 precision of a tie."""
    if sampler == SAMPLER_HW:
        return False, False
    if sampler == SAMPLER_WARP:
        return _pow2(int(width)), _pow2(int(width)) and _pow2(int(height))
    raise ValueError(f"sampler must be {SAMPLER_HW!r} or {SAMPLER_WARP!r}, got {sampler!r}")


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def star_smooth(lo, hi, x):
    """HLSL StarSmooth = starfield._smoothstep: the denominator floor makes lo == hi a step at lo."""
    t = np.clip((x - lo) / max(hi - lo, 1e-12), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def clamp_star(sp: StarfieldParams) -> StarfieldParams:
    """C++ FaldStarfieldClamp: the ranges the settings / pipe / CB enforce (the reference clips even, lift and
    strength itself; the reaches and the ordered smoothstep pairs are the C++ side's)."""
    from dataclasses import replace
    c = lambda v, lo, hi: float(min(max(float(v), lo), hi))
    area_lo = c(sp.area_lo, 0.0, 1e6); nb_lo = c(sp.nb_lo, 0.0, 1.0)
    return replace(sp, even=c(sp.even, 0.0, 1.0), lift=c(sp.lift, 0.0, 1.0), target_gain=c(sp.target_gain, 0.05, 2.0),
                   target_sigma=c(sp.target_sigma, 0.0, 4.0), keep_nits=c(sp.keep_nits, 0.0, 10000.0),
                   even_reach=int(min(max(int(sp.even_reach), 0), STAR_EVEN_REACH_MAX)), cap_nits=c(sp.cap_nits, 0.0, 10000.0),
                   strength=c(sp.strength, 0.0, 1.0), area_lo=area_lo, area_hi=max(c(sp.area_hi, 0.0, 1e6), area_lo),
                   peak_hi=c(sp.peak_hi, 0.0, 10000.0), reach=int(min(max(int(sp.reach), 0), STAR_REACH_MAX)),
                   nb_lo=nb_lo, nb_hi=max(c(sp.nb_hi, 0.0, 1.0), nb_lo))


def _box32(a, r, fn):
    """(2r+1)^2 zone neighbourhood, zero outside the lattice (the HLSL loops skip out-of-range zones), float32."""
    a = a.astype(np.float32)
    if r <= 0:
        return a.copy()
    rows, cols = a.shape
    pad = np.pad(a, r, mode="constant")
    out = None
    for dy in range(-r, r + 1):                  # the shader's loop order: dy outer, dx inner
        for dx in range(-r, r + 1):
            v = pad[r + dy: r + dy + rows, r + dx: r + dx + cols]
            out = v.copy() if out is None else fn(out, v)
    return out.astype(np.float32)


def _box32_tapered(a, r):
    """S2's target sums: (2r+1)^2 neighbourhood, zone weight (r + 1 - d) / (r + 1) with d the chebyshev distance, zero
    outside the lattice, accumulated in float32 in the shader's loop order."""
    a = a.astype(np.float32)
    rows, cols = a.shape
    pad = np.pad(a, r, mode="constant")
    out = np.zeros_like(a)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            k = f32(r + 1 - max(abs(dx), abs(dy))) / f32(r + 1)
            out = (out + pad[r + dy: r + dy + rows, r + dx: r + dx + cols] * k).astype(np.float32)
    return out


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


def clock_factors32(n_a: int, k: int, closure: float, parity: int) -> tuple:
    """C++ ``FaldPanelClockFactors`` (float32, the same operation order): the six CB words of the panel-clock pass —
    ``(true0, est0, true1, est1, w0, w1)``. Per parity clock p the blend toward the previous frame's drives that makes
    its LED state OF this frame's first refresh (``true``) and of the refresh before it (``est``):
    a = 1 − (1 − closure)^ticks, the power by repeated float32 multiplication; ticks = :func:`paneltime.clock_ticks`.
    ``parity`` −1 (unknown) weighs both clocks ½, 0 / 1 that clock alone. k > ``MAX_REFRESHES`` (a long static pause):
    all four exactly 1 — the panel has settled on the previous frame."""
    from .paneltime import CLOSURE_MAX, CLOSURE_MIN, MAX_REFRESHES, clock_ticks
    q = f32(1.0) - f32(min(max(float(closure), CLOSURE_MIN), CLOSURE_MAX))

    def a(ticks):
        r = f32(1.0)
        for _ in range(ticks):
            r = f32(r * q)
        return f32(f32(1.0) - r)
    w = (f32(0.5), f32(0.5)) if parity not in (0, 1) else ((f32(1.0), f32(0.0)) if parity == 0 else (f32(0.0), f32(1.0)))
    if k > MAX_REFRESHES:
        return f32(1.0), f32(1.0), f32(1.0), f32(1.0), w[0], w[1]
    (ts0, tp0), (ts1, tp1) = clock_ticks(n_a, k, 0), clock_ticks(n_a, k, 1)
    return a(ts0), a(tp0), a(ts1), a(tp1), w[0], w[1]


class GpuPanelDriveState:
    """The GPU side of :class:`dlc.fald.paneltime.PanelDriveState` — temporal mode 3 "panel clock" (work guide C13):
    pass 1c ``g_faldPanelClockSource`` on the R32F zone textures, driven like the C++ ``FaldRunPasses``.

    Per frame: :meth:`advance` (``refreshes`` = k, the panel refreshes elapsed since the previous frame was first shown;
    C++ ``FaldPanelClockStep`` / :class:`paneltime.RefreshGrid` derive it from the run times on a phase-locked grid) runs the pass ONCE before round 0 — both parity clocks' LED
    states move toward ``d_prev`` (the previous frame's round-1 instantaneous drives) by the CPU-side blend factors
    (:func:`clock_factors32`; beyond ``MAX_REFRESHES`` exactly 1: a long pause is NOT a reset), in place, and the weighted
    maps for B_true / B_est are written; :meth:`pair` = what RunConv binds in BOTH rounds (the state of a frame depends on
    past frames only); :meth:`commit` = ``d_prev`` <- this frame's round-1 drives. k = 0 (a second frame inside one
    refresh): nothing advances, the previous frame's maps are read again and the commit REPLACES the target (the later
    frame is the one the panel shows); inside the seeding refresh (n = 0) it re-seeds. With no valid state (first frame,
    after :meth:`reset` = the C++ reset rules) the pass does not run, both rounds see their own instantaneous drives (the
    stateless layer) and the commit seeds both clocks with the round-1 drives: the panel is taken as settled on it."""

    def __init__(self, closure: float = 0.72, parity: int = -1):
        from .paneltime import CLOSURE_MAX, CLOSURE_MIN, MODE_PANEL
        self.mode = MODE_PANEL
        self.closure = float(min(max(float(closure), CLOSURE_MIN), CLOSURE_MAX))
        self.parity = int(parity) if parity in (0, 1) else -1
        self.reset()

    def reset(self) -> None:
        self.s: Optional[list[np.ndarray]] = None     # [S_0, S_1] (C++ FaldResources::clkStateTex)
        self.d_prev: Optional[np.ndarray] = None      # C++ clkPrevTex
        self.n = 0                                    # refresh index of the last committed frame (parity arithmetic)
        self.true: Optional[np.ndarray] = None        # this frame's maps (C++ driveFiltTex / clkEstTex)
        self.est: Optional[np.ndarray] = None
        self.factors: Optional[tuple] = None

    def advance(self, refreshes: int = 1) -> None:
        if self.s is None:
            return
        k = int(refreshes)
        if k < 0:
            raise ValueError(f"refreshes must be >= 0, got {refreshes!r}")
        if k == 0:
            if self.n == 0:                           # still inside the seeding refresh: this frame replaces the seed
                self.reset()
            return                                    # else: the previous frame's maps stand, nothing advances
        aT0, aE0, aT1, aE1, w0, w1 = self.factors = clock_factors32(self.n, k, self.closure, self.parity)
        d, (s0, s1) = self.d_prev, self.s
        g0 = (d - s0).astype(np.float32); g1 = (d - s1).astype(np.float32)
        t0 = (s0 + aT0 * g0).astype(np.float32); t1 = (s1 + aT1 * g1).astype(np.float32)
        self.true = (w0 * t0 + w1 * t1).astype(np.float32)
        self.est = (w0 * (s0 + aE0 * g0).astype(np.float32) + w1 * (s1 + aE1 * g1).astype(np.float32)).astype(np.float32)
        self.s = [t0, t1]
        self.n += k

    def pair(self, d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(drive for K_true, drive for K_est) — what RunConv binds at t4 / t10 (both rounds the same maps)."""
        if self.true is None:
            return d.astype(np.float32), d.astype(np.float32)
        return self.true, self.est

    def commit(self, d: np.ndarray) -> None:
        d = d.astype(np.float32).copy()
        if self.s is None:
            self.s = [d.copy(), d.copy()]
            self.n = 0
        self.d_prev = d


class Emu:
    def __init__(self, o, width=3840, height=2160, ped_mode=0, subtexel_bits: Optional[int] = None, c15: bool = True,
                 sampler: str = SAMPLER_HW):
        self.o = o
        self.c15 = c15                         # C15: the knee's ceiling from the low-passed B_est (False = the pre-C15 shader)
        self.subtexel_bits = subtexel_bits     # None = exact sampler fractions; 8 = a D3D11 device's bilinear weights
        sampler_truncates(sampler, 1, 1)       # (validates the name)
        if sampler != SAMPLER_HW and not subtexel_bits:
            raise ValueError(f"sampler={sampler!r} models a device's sub-texel fraction: give subtexel_bits (8)")
        self.sampler = sampler                 # with subtexel_bits: "hw" rounds the fraction, "warp" = WARP's rule
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
        # the zone rule (CB words 72-74, work guide C12b): 0 = LIT-or-DIM, 1 = LIT-or-MEAN
        self.boostRule = int(o.get("boostRule", 0))
        self.meanGamma, self.meanThresh = f32(o.get("boostMeanGamma", 0.62)), f32(o.get("boostMeanThresh", 0.0693))
        self.flatT = self.conv(np.ones((self.rows, self.cols)), o["kTrue"])
        self.flatE = self.conv(np.ones((self.rows, self.cols)), o["kEst"])
        # bilinear sample tables for pixel centres (SampleLevel, linear, clamp) — FineUV with the lattice origin removed
        S = self.sub
        xt = (np.arange(self.W) - self.ox + 0.5) / (self.cols * self.cw) * (self.cols * S) - 0.5
        yt = (np.arange(self.H) - self.oy + 0.5) / (self.rows * self.ch) * (self.rows * S) - 0.5
        tx, ty = sampler_truncates(sampler, self.cols * S, self.rows * S)
        self.bx = self._axis(xt, self.cols * S, tx)
        self.by = self._axis(yt, self.rows * S, ty)
        # the same sampler on a cols x rows texture (one texel per zone): texel coordinate (px - origin + 0.5) / cell - 0.5
        # = starfield._bilinear_zones — between zone CENTRES, the border zones held outside the outermost centres
        tx, ty = sampler_truncates(sampler, self.cols, self.rows)
        self.zx = self._axis((np.arange(self.W) - self.ox + 0.5) / self.cw - 0.5, self.cols, tx)
        self.zy = self._axis((np.arange(self.H) - self.oy + 0.5) / self.ch - 0.5, self.rows, ty)
        self.curve_trunc = sampler_truncates(sampler, int(o["curveN"]), 1)[0]   # the drive-curve LUT: curveN x 1
        xs = np.arange(self.W); ys = np.arange(self.H)
        self.in_lattice = ((ys >= self.oy) & (ys < self.oy + self.rows * self.ch))[:, None] & \
                          ((xs >= self.ox) & (xs < self.ox + self.cols * self.cw))[None, :]

    def _axis(self, t, n, trunc=False):
        i0 = np.floor(t).astype(int)
        return np.clip(i0, 0, n - 1), np.clip(i0 + 1, 0, n - 1), self._subtexel(t - i0, trunc)

    def _subtexel(self, fr, trunc):
        """The sampler's weight of the upper texel: exact (subtexel_bits None) or the device's subtexel_bits-bit fraction,
        rounded to nearest or truncated (``trunc``: :func:`sampler_truncates` for this texture and axis). A fraction that
        rounds up to 1 puts the whole weight on the upper texel, as the device does."""
        if not self.subtexel_bits:
            return fr
        s = float(1 << int(self.subtexel_bits))
        return (np.floor(fr * s) if trunc else np.round(fr * s)) / s

    def sample(self, T):
        y0, y1, fy = self.by; x0, x1, fx = self.bx
        Ty = T[y0] * (1 - fy)[:, None] + T[y1] * fy[:, None]
        return Ty[:, x0] * (1 - fx)[None] + Ty[:, x1] * fx[None]

    def sample_zone(self, Z):
        """SampleLevel(linearClamp, FineUV(px)) on a cols x rows zone texture, for every frame pixel."""
        y0, y1, fy = self.zy; x0, x1, fx = self.zx
        Z = Z.astype(np.float64)
        Zy = Z[y0] * (1 - fy)[:, None] + Z[y1] * fy[:, None]
        return Zy[:, x0] * (1 - fx)[None] + Zy[:, x1] * fx[None]

    # ---- starfield balancing (work guide S1): passes S0 / S1 / S2 + Balance
    def star_stat(self, img, sp: StarfieldParams):
        """S0, g_faldStarStatSource on the SOURCE frame, float32 (starfield.py docstring items 1-5): peak, lit sum,
        sparse, solid per zone from the layer's floored statistic, and — over ALL the zone's pixels, no drive-floor
        gate — the background b (darkest pixel), the un-gated sum and the effective lit area ABOVE the background
        a_eff = (sum - b n) / (peak_all - b); spk = the speck-zone flag. A zone whose peak is within max(STAR_FLAT_ABS,
        STAR_FLAT_REL x peak) of its background is flat: not star-like, never divided. (The lattice lies inside the frame
        — FaldLatticeFits — so every zone has n = cellW x cellH pixels; the shader counts the in-frame ones.)"""
        s = np.minimum(img.max(axis=0), self.white)
        s = s[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw]
        blocks = s.reshape(self.rows, self.ch, self.cols, self.cw)
        lit = blocks > self.floor
        peak = np.where(lit, blocks, 0.0).max(axis=(1, 3)).astype(np.float32)
        total = np.where(lit, blocks, 0.0).astype(np.float32).sum(axis=(1, 3), dtype=np.float32)
        peak_all = blocks.max(axis=(1, 3)).astype(np.float32)
        b = blocks.min(axis=(1, 3)).astype(np.float32)
        sum_all = blocks.astype(np.float32).sum(axis=(1, 3), dtype=np.float32)
        n = f32(self.cw * self.ch)
        has = peak > 0
        span = (peak_all - b).astype(np.float32)
        speck = span > np.maximum(f32(STAR_FLAT_ABS), f32(STAR_FLAT_REL) * peak_all)
        a_eff = np.where(speck, (sum_all - b * n) / np.maximum(span, f32(1e-12)), f32(0.0)).astype(np.float32)
        sparse = np.where(has & speck, 1.0 - star_smooth(sp.area_lo, sp.area_hi, a_eff), 0.0)
        if sp.peak_hi > 0.0:
            sparse = sparse * (1.0 - star_smooth(sp.peak_hi, 2.0 * sp.peak_hi, peak))
        drive = self.drive_of(np.minimum(peak, total / f32(self.area0)))
        solid = np.where(has, (1.0 - sparse) * drive, 0.0)
        ln_b = np.log(np.maximum(b, f32(1e-12))).astype(np.float32)
        # the zone-local position of the brightest pixel (float32 values as the GPU compares them; ties: nearest the
        # zone border — largest max(|2 lx - (cw - 1)| ch, |2 ly - (ch - 1)| cw) — then the first in row-major order)
        flat = blocks.astype(np.float32).transpose(0, 2, 1, 3).reshape(self.rows, self.cols, -1)
        ly_, lx_ = np.divmod(np.arange(self.ch * self.cw), self.cw)
        key = np.maximum(np.abs(2 * lx_ - (self.cw - 1)) * self.ch, np.abs(2 * ly_ - (self.ch - 1)) * self.cw).astype(np.int64)
        order = key * (self.cw * self.ch) + (self.cw * self.ch - 1 - np.arange(self.ch * self.cw))
        arg = np.argmax(np.where(flat >= flat.max(axis=2, keepdims=True), order[None, None, :], -1), axis=2)
        return {"peak": peak, "total": total, "sparse": sparse.astype(np.float32), "solid": solid.astype(np.float32),
                "b": b, "ln_b": ln_b, "sum_all": sum_all, "a_eff": a_eff, "arg": arg,       # arg = ly * cellW + lx
                "spk": has & speck & (a_eff < f32(sp.area_hi))}     # spk: the speck-zone flag (a star-sized area above the background)

    def star_plan(self, st, sp: StarfieldParams):
        """g_faldStarWeightSource (S1) + g_faldStarPlanSource (S2), float32: the tapered protection field, the zone
        weights, the target and the fields the pixels read (starfield.py module docstring, items 6-9)."""
        peak = st["peak"]
        spk = st["spk"]              # the SPECK-ZONE flag: carry / lift / peak field / own-zone gate key on it
        near = np.zeros_like(st["solid"], dtype=np.float32)             # S1: max over d <= reach + 1 of solid x k(d)
        for d in range(int(sp.reach) + 2):
            k = f32(min(max((int(sp.reach) + 1 - d) * 0.5, 0.0), 1.0))
            near = np.maximum(near, (k * _box32(st["solid"], d, np.maximum)).astype(np.float32))
        w0 = (st["sparse"] * f32(sp.strength)).astype(np.float32)
        w = (w0 * (1.0 - star_smooth(sp.nb_lo, sp.nb_hi, near))).astype(np.float32)
        flank, equal = self.star_flank(peak, st["arg"])                 # S1: the spill of a brighter neighbour's star
        # ... carries no weight in the target average; equal-peak partners (a flat-topped straddler) share one vote
        wt = np.where(flank, f32(0.0), w / (f32(1.0) + equal.astype(np.float32))).astype(np.float32)
        lp = np.log(np.maximum(peak, f32(1e-12))).astype(np.float32)
        wsum = _box32_tapered(wt, int(sp.even_reach))                   # S2: tapered window (E + 1 - d) / (E + 1)
        wlp = (wt * lp).astype(np.float32)                              # what S1 stores (starW.y)
        wl = _box32_tapered(wlp, int(sp.even_reach))
        mean = (wl / np.where(wsum > 0, wsum, f32(1.0))).astype(np.float32)
        std = np.zeros_like(mean)
        if sp.target_sigma > 0.0:                                       # the spread, summed ABOUT the mean (a second sweep)
            std = np.sqrt(np.maximum(self._box32_centred_var(wt, wlp, mean, int(sp.even_reach)) / np.where(wsum > 0, wsum, f32(1.0)),
                                     f32(0.0))).astype(np.float32)
        target = (np.exp(mean + f32(sp.target_sigma) * std) * f32(sp.target_gain)).astype(np.float32)
        target = np.maximum(target, f32(sp.keep_nits))                 # the absolute floor (before the cap / white clamp)
        if sp.cap_nits > 0.0:
            target = np.minimum(target, f32(sp.cap_nits))
        target = np.minimum(target, f32(self.white))
        target = np.where(wsum > 0, target, peak).astype(np.float32)
        s3 = _box32(st["sparse"], 1, np.add); n3 = _box32(spk.astype(np.float32), 1, np.add)
        # a zone without a speck carries its 3x3 speck neighbours' mean w0 x its own non-protection (sparse > 0 only in
        # speck zones); the protection itself is NOT in this field: it is interpolated on its own (near) per pixel
        carry = s3 * f32(sp.strength) / np.maximum(n3, f32(1.0)) * (1.0 - star_smooth(sp.nb_lo, sp.nb_hi, st["solid"]))
        w_field = np.where(spk, w0, carry).astype(np.float32)
        ln_t = np.log(np.maximum(np.where(target > 0, target, f32(self.white)), f32(1e-12))).astype(np.float32)
        ln_g = np.where(spk & (peak < target), f32(sp.lift) * (ln_t - lp), f32(0.0)).astype(np.float32)
        ln_pk = np.where(spk, lp, ln_t).astype(np.float32)
        return {"w": w, "w0": w0, "wt": wt, "flank": flank, "near": near, "target": target, "w_field": w_field, "ln_t": ln_t, "ln_g": ln_g,
                "ln_pk": ln_pk, "spk": spk, "ln_b": st["ln_b"]}     # plan (t15): w_field (= w0_field), ln_t, ln_g, ln_pk; plan2 (t18): ln_b, near, spk, w

    @staticmethod
    def _box32_centred_var(wt, wlp, mean, r):
        """S2's second sweep: sum over the tapered window of wt_n k (ln peak_n - mean)^2, ln peak_n = wlp_n / wt_n as the
        shader recovers it from starW; float32, the shader's loop order."""
        rows, cols = wt.shape
        lp_n = np.where(wt > 0, wlp / np.where(wt > 0, wt, f32(1.0)), f32(0.0)).astype(np.float32)
        pw, pl = np.pad(wt, r, mode="constant"), np.pad(lp_n, r, mode="constant")
        out = np.zeros_like(wt, dtype=np.float32)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                k = f32(r + 1 - max(abs(dx), abs(dy))) / f32(r + 1)
                d = (pl[r + dy: r + dy + rows, r + dx: r + dx + cols] - mean).astype(np.float32)
                out = (out + pw[r + dy: r + dy + rows, r + dx: r + dx + cols] * k * d * d).astype(np.float32)
        return out

    def star_flank(self, peak, arg):
        """S1's flank flag (starfield.py docstring item 7): the zone's brightest pixel within STAR_FLANK_PX of the edge /
        corner shared with a neighbour of LARGER peak whose own brightest pixel lies within STAR_FLANK_NEAR_PX of that
        edge (and, along the edge, within STAR_FLANK_NEAR_PX of this zone's); + the number of neighbours that fulfil the same
        geometry with an EQUAL peak."""
        cw, ch = self.cw, self.ch
        ly, lx = np.divmod(arg, cw)
        rows, cols = peak.shape
        pad = lambda a: np.pad(a, 1, mode="constant")
        pp, px, py = pad(peak), pad(lx), pad(ly)
        flank = np.zeros((rows, cols), dtype=bool)
        equal = np.zeros((rows, cols), dtype=int)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                sl = (slice(1 + dy, 1 + dy + rows), slice(1 + dx, 1 + dx + cols))
                ok = (pp[sl] >= peak) & (pp[sl] > 0)                     # larger = flank, equal = a partner sharing the vote
                nx, ny = px[sl], py[sl]
                if dx == 1:
                    ok &= (lx >= cw - STAR_FLANK_PX) & (nx < STAR_FLANK_NEAR_PX)
                elif dx == -1:
                    ok &= (lx < STAR_FLANK_PX) & (nx >= cw - STAR_FLANK_NEAR_PX)
                else:
                    ok &= np.abs(lx - nx) <= STAR_FLANK_NEAR_PX
                if dy == 1:
                    ok &= (ly >= ch - STAR_FLANK_PX) & (ny < STAR_FLANK_NEAR_PX)
                elif dy == -1:
                    ok &= (ly < STAR_FLANK_PX) & (ny >= ch - STAR_FLANK_NEAR_PX)
                else:
                    ok &= np.abs(ly - ny) <= STAR_FLANK_NEAR_PX
                flank |= ok & (pp[sl] > peak)
                equal += (ok & (pp[sl] == peak)).astype(int)
        return flank, equal

    def balance(self, img, plan, sp: StarfieldParams):
        """HLSL Balance for every lattice pixel (the per-pixel formula of the starfield.py docstring): (balanced
        (3, H, W), scale (H, W)); untouched pixels keep scale exactly 1 (the HLSL returns ``img`` itself there)."""
        # the protection is interpolated on its own and applied per pixel
        w_px = self.sample_zone(plan["w_field"]) * (1.0 - star_smooth(sp.nb_lo, sp.nb_hi, self.sample_zone(plan["near"])))
        ln_t = self.sample_zone(plan["ln_t"])
        m = img.max(axis=0)
        own = np.zeros((self.H, self.W), dtype=bool)                    # the own-zone gate: a nearest-zone Load of spk
        own[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw] = \
            np.repeat(np.repeat(plan["spk"], self.ch, axis=0), self.cw, axis=1)
        live = (w_px > 0.0) & (m > 0.0) & own & self.in_lattice
        safe = np.maximum(m, 1e-12)
        t_px = np.exp(ln_t)
        b_px = np.exp(self.sample_zone(plan["ln_b"]))                      # >= 1e-12: a black sky gates fully open
        span_px = np.exp(self.sample_zone(plan["ln_pk"])) - b_px
        # speck pixels: 25 .. 50 % of the way from the interpolated background to the interpolated zone peak
        is_speck = np.where(span_px > 0.0, star_smooth(STAR_SPECK_LO, STAR_SPECK_HI, (safe - b_px) / np.maximum(span_px, 1e-12)), 0.0)
        g_px = np.exp(self.sample_zone(plan["ln_g"]) * w_px * is_speck)
        # the pull threshold T': the target while it is well above the background, the bottom of the speck band as it
        # nears / undercuts it; out(m) = m up to T', m^(1 - a) T'^a above: monotone in m, the sky is never reached
        gate = star_smooth(STAR_GATE_LO, STAR_GATE_HI, t_px / b_px)
        t_floor = b_px + STAR_SPECK_LO * np.maximum(span_px, 0.0)
        ln_floor = np.log(np.maximum(t_px, t_floor))
        ln_tp = ln_floor + gate * (ln_t - ln_floor)                      # HLSL lerp
        above = m > np.exp(ln_tp)
        shown = np.minimum(safe, self.white)                               # the pull starts from what the panel SHOWS
        pulled = np.exp(np.log(shown) + w_px * float(sp.even) * (ln_tp - np.log(shown)))
        # never below the (interpolated) zone background, never above the pixel itself
        pulled = np.maximum(pulled, np.minimum(b_px, safe))
        lifted = np.minimum(safe * g_px, np.maximum(t_px, safe))
        out_m = np.where(above, pulled, np.where(m <= t_px, lifted, safe))
        acts = live & np.where(above, out_m < safe * (1.0 - STAR_PULL_EPS), (m <= t_px) & (g_px > 1.0))
        scale = np.where(acts, out_m / safe, 1.0)
        return np.where(acts[None], img * scale[None], img), scale

    # ---- glow fill (work guide S2): passes G0-G3 + GlowAdd
    def glow_zones(self, bT, gp: GlowFillParams):
        """G0-G3 on this round's fine B_true texture (boost included), float32 in the shaders' loop order: ``vz`` (zone
        mean of white * tmin * max(bT / flatT, 0) over the zone's sub x sub fine texels), ``dil`` (box maximum on the
        lattice extended by ``reach`` on every side, the field continued by its border values), ``cz`` (box minimum of
        ``dil`` = the grey closing), ``ez`` = min(Gaussian blur of cz, cz), ``dz`` = (ez - vz) x smoothstep(DEFICIT_REL_LO, DEFICIT_REL_HI, (ez - vz) / vz)."""
        S, r = self.sub, int(gp.reach)
        n = (bT.astype(np.float32) / np.maximum(self.flatT, f32(1e-6))).astype(np.float32)
        n = np.maximum(n, f32(0.0)).reshape(self.rows, S, self.cols, S)
        acc = np.zeros((self.rows, self.cols), dtype=np.float32)
        for oy in range(S):                                   # the shader's order: oy outer, ox inner
            for ox in range(S):
                acc = (acc + n[:, oy, :, ox]).astype(np.float32)
        vz = (acc * (f32(self.white) * f32(self.tmin) / f32(S * S))).astype(np.float32)
        ext = np.pad(vz, 2 * r, mode="edge")                  # V(clamp(z)): the field continued by its border values
        rows_e, cols_e = self.rows + 2 * r, self.cols + 2 * r
        dil = None
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                v = ext[r + dy: r + dy + rows_e, r + dx: r + dx + cols_e]
                dil = v.copy() if dil is None else np.maximum(dil, v)
        cz = None
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                v = dil[r + dy: r + dy + self.rows, r + dx: r + dx + self.cols]
                cz = v.copy() if cz is None else np.minimum(cz, v)
        sigma = f32(GLOW_SIGMA_BASE) + f32(GLOW_SIGMA_PER_REACH) * f32(r)
        R = int(np.ceil(3.0 * float(sigma)))
        pad = np.pad(cz, R, mode="edge")
        acc = np.zeros_like(cz); wsum = f32(0.0)
        for dy in range(-R, R + 1):
            for dx in range(-R, R + 1):
                w = np.exp(f32(-0.5) * f32(dx * dx + dy * dy) / (sigma * sigma), dtype=np.float32)
                acc = (acc + w * pad[R + dy: R + dy + self.rows, R + dx: R + dx + self.cols]).astype(np.float32)
                wsum = f32(wsum + w)
        ez = np.minimum((acc / wsum).astype(np.float32), cz)
        d = np.maximum(ez - vz, f32(0.0)).astype(np.float32)
        dz = (d * smoothstep(f32(DEFICIT_REL_LO), f32(DEFICIT_REL_HI), d / np.maximum(vz, f32(1e-12)))).astype(np.float32)
        return {"vz": vz, "dil": dil, "cz": cz, "ez": ez, "dz": dz}

    def glow_band_active(self):
        """C++ FaldGlowBandActive: the count-threshold band applies (a boost LUT AND the mean zone rule)."""
        return bool(self.boostN) and self.boostRule == 1

    def glow_band(self, req, sT, sE, dz, gp: GlowFillParams):
        """G4 g_faldGlowBandSource + G5 g_faldGlowGuardSource (glowfill.band_scale, item 7). G4: per zone, over every pixel
        in the zone sweeps' thread / reduction order, float32 — the sum of (brightest channel)^gamma of the round's request
        WITHOUT (pc) and WITH the fill of the unscaled deficit (pf), the LIT count of the content, k0 = ((BAND_LO T - pc) /
        (pf - pc))^(1 / gamma) for a zone not counted by its content whose pf lies in [BAND_LO T, BAND_HI T] (else 1), and
        (C16) the neighbour bound A_d = mean of w_d (pf_px - pc_px) / (1 - s0) (0 toward a neighbour outside the lattice).
        G5: the neighbour guard's Jacobi iterations from k0 (:meth:`guard32`) -> the final k GlowAdd reads.
        ``gp.band_feather`` False: the band before C16 (k = k0; no A, no guard)."""
        filled, _ = self.glow_add(req, sT, sE, dz, gp)                # k = 1: the unscaled rule
        n = f32(self.cw * self.ch)
        rc = self._zone_px(req.max(axis=0).astype(np.float32))       # (rows, cols, cellW * cellH): the sweep's k order
        rf = self._zone_px(filled.max(axis=0).astype(np.float32))
        pwc, pwf = self._pow32(rc), self._pow32(rf)
        pc = (self.zone_sweep_sum(pwc) / n).astype(np.float32)
        pf = (self.zone_sweep_sum(pwf) / n).astype(np.float32)
        lit = (rc > self.litNits).sum(axis=-1).astype(np.float32) / n > self.litFrac
        t = self.meanThresh
        band0 = (~lit) & (pc < t) & (pf >= f32(BAND_LO) * t) & (pf <= f32(BAND_HI) * t)
        share = np.clip((f32(BAND_LO) * t - pc) / np.maximum(pf - pc, f32(1e-30)), f32(0.0), f32(1.0)).astype(np.float32)
        pos = share > 0
        kk = np.where(pos, np.exp(np.log(np.where(pos, share, f32(1.0)), dtype=np.float32) / self.meanGamma, dtype=np.float32), f32(0.0))
        k0 = np.where(band0, kk, f32(1.0)).astype(np.float32)
        none = np.zeros_like(band0)
        out = {"k": k0, "k0": k0, "pc": pc, "pf": pf, "lit": lit, "band": band0, "band0": band0, "guard_added": none,
               "iterations": 0, "A": None}
        if not gp.band_feather:
            return out
        # G4's neighbour bound: s0 = saturate(shown / want) with want / shown as GlowAddK forms them (unscaled want)
        want = self._zone_px(self.glow_want(dz, gp).astype(np.float32))
        shown = (rc * self._zone_px(np.maximum(sT, 0.0).astype(np.float32)) /
                 np.maximum(self._zone_px(sE.astype(np.float32)), f32(1e-9))).astype(np.float32)
        s0 = np.clip(shown / np.where(want > 0.0, want, f32(1.0)), f32(0.0), f32(1.0)).astype(np.float32)
        live = (want > 0.0) & (s0 < f32(1.0))
        q = np.where(live, (pwf - pwc) / np.where(live, f32(1.0) - s0, f32(1.0)), f32(0.0)).astype(np.float32)
        u, v = self.zone_local32()
        a = np.zeros((len(NEIGHBOURS), self.rows, self.cols), dtype=np.float32)
        zy, zx = np.mgrid[0: self.rows, 0: self.cols]
        for d, (i, j) in enumerate(NEIGHBOURS):
            w = self._zone_px(self._feather32(i, j, u, v))
            ad = (self.zone_sweep_sum((w * q).astype(np.float32)) / n).astype(np.float32)
            exists = (zx + i >= 0) & (zx + i < self.cols) & (zy + j >= 0) & (zy + j < self.rows)
            a[d] = np.where(exists, ad, f32(0.0))
        # G5: the zones counted only by the fill (not LIT, pc < T, pf > BAND_HI T, not band0)
        cand = (~lit) & (pc < t) & (pf > f32(BAND_HI) * t) & ~band0
        k, iterations = self.guard32(k0, cand, kk, pf, a, (f32(BAND_HI) * t).astype(np.float32))
        band = band0 | (k < f32(1.0))
        out.update(k=k, band=band, guard_added=band & ~band0, iterations=iterations, A=a)
        return out

    def guard32(self, k0, cand, k_join, pf, a, hi):
        """G5 g_faldGlowGuardSource, float32: Jacobi iterations — every zone reads the PREVIOUS iteration's k; a candidate
        still at k 1 joins (k_join) when pf - loss < hi, loss = the sum over its existing neighbours d (NEIGHBOURS order) of
        (1 - k_{z+d}) A_d. Until no zone joins, at most GUARD_ITER_MAX iterations. (k, the iterations evaluated)."""
        rows, cols = k0.shape
        k = k0.astype(np.float32).copy()
        iterations = 0
        for it in range(GUARD_ITER_MAX):
            iterations = it + 1
            loss = np.zeros((rows, cols), dtype=np.float32)
            for d, (i, j) in enumerate(NEIGHBOURS):
                ys, xs = slice(max(0, -j), rows - max(0, j)), slice(max(0, -i), cols - max(0, i))
                kn = k[max(0, j): rows + min(0, j), max(0, i): cols + min(0, i)]
                loss[ys, xs] = (loss[ys, xs] + ((f32(1.0) - kn) * a[d][ys, xs]).astype(np.float32)).astype(np.float32)
            new = cand & (k >= f32(1.0)) & ((pf - loss).astype(np.float32) < hi)
            if not new.any():
                break
            k = np.where(new, k_join, k).astype(np.float32)
        return k, iterations

    def _zone_px(self, a):
        """(H, W) -> (rows, cols, cellW * cellH): each zone's pixels, row-major inside the zone (the sweeps' index k)."""
        s = a[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw]
        return s.reshape(self.rows, self.ch, self.cols, self.cw).transpose(0, 2, 1, 3).reshape(self.rows, self.cols, self.ch * self.cw)

    def zone_local32(self):
        """HLSL GlowZoneLocal for every frame column / row (float32; meaningful on the lattice): u = (px + 0.5 - originX) /
        cellW - zx, v likewise, zx / zy the pixel's zone."""
        xs, ys = np.arange(self.W), np.arange(self.H)
        zx = np.clip((xs - self.ox) // self.cw, 0, self.cols - 1)
        zy = np.clip((ys - self.oy) // self.ch, 0, self.rows - 1)
        u = ((xs.astype(np.float32) + f32(0.5) - f32(self.ox)) / f32(self.cw) - zx.astype(np.float32)).astype(np.float32)
        v = ((ys.astype(np.float32) + f32(0.5) - f32(self.oy)) / f32(self.ch) - zy.astype(np.float32)).astype(np.float32)
        return u, v

    @staticmethod
    def _feather32(i, j, u, v):
        """HLSL GlowFeatherW (glowfill.feather_weight) for columns u (W,) and rows v (H,): (H, W) float32."""
        dx = np.maximum(f32(0.0), np.maximum(f32(i) - u, u - f32(i + 1))).astype(np.float32)
        dy = np.maximum(f32(0.0), np.maximum(f32(j) - v, v - f32(j + 1))).astype(np.float32)
        dist = np.sqrt(dy[:, None] * dy[:, None] + dx[None, :] * dx[None, :], dtype=np.float32)
        t = np.clip(dist / f32(FEATHER), f32(0.0), f32(1.0)).astype(np.float32)
        return (f32(1.0) - t * t * (f32(3.0) - f32(2.0) * t)).astype(np.float32)

    def band_scale_px(self, k):
        """HLSL GlowBandScale for every frame pixel (1 outside the lattice), float32: s = min(k_z, min over the existing
        neighbours n with w_n > 0 of 1 - (1 - k_n) w_n)."""
        k = np.asarray(k, dtype=np.float32)
        xs, ys = np.arange(self.W), np.arange(self.H)
        zx = np.clip((xs - self.ox) // self.cw, 0, self.cols - 1)
        zy = np.clip((ys - self.oy) // self.ch, 0, self.rows - 1)
        u, v = self.zone_local32()
        s = k[np.ix_(zy, zx)].copy()
        for i, j in NEIGHBOURS:
            w = self._feather32(i, j, u, v)
            ok = ((zy + j >= 0) & (zy + j < self.rows))[:, None] & ((zx + i >= 0) & (zx + i < self.cols))[None, :] & (w > 0.0)
            kn = k[np.ix_(np.clip(zy + j, 0, self.rows - 1), np.clip(zx + i, 0, self.cols - 1))]
            s = np.where(ok, np.minimum(s, (f32(1.0) - (f32(1.0) - kn) * w).astype(np.float32)), s)
        return np.where(self.in_lattice, s, f32(1.0)).astype(np.float32)

    def glow_ceiling(self):
        """HLSL GlowReqCeil: a filled pixel's brightest channel stays at / below this (glowfill.req_ceiling)."""
        c = REQ_FLOOR_FRAC * self.floor
        return min(c, REQ_LIT_FRAC * float(self.litNits)) if self.boostN else c

    def glow_want(self, dz, gp: GlowFillParams):
        """HLSL GlowWant for every frame pixel: the unscaled want (item 4)."""
        return np.minimum(np.maximum(float(gp.strength) * self.sample_zone(dz) - WANT_EPS, 0.0), float(gp.cap_nits))

    def glow_add(self, req, sT, sE, dz, gp: GlowFillParams, k=None):
        """HLSL GlowAdd for every lattice pixel: (request + fill (3, H, W), the fill's request luminance (H, W)). ``req``
        = the round's corrected request, ``sT`` / ``sE`` = the pixel fields Correct used, ``k`` = the band's zone scale
        (None = 1), reaching the pixels through the feather (:meth:`band_scale_px`; ``gp.band_feather`` False: the
        pixel's OWN zone's k, the rule before C16). Untouched pixels are returned as they came (the HLSL returns ``req``
        itself there)."""
        want = self.glow_want(dz, gp)
        if k is not None:
            if gp.band_feather:
                kpx = self.band_scale_px(k).astype(np.float64)
            else:
                kpx = np.ones((self.H, self.W))
                kpx[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw] = \
                    np.repeat(np.repeat(k.astype(np.float64), self.ch, axis=0), self.cw, axis=1)
            want = want * kpx
        bT = np.maximum(sT, 0.0)
        r = req.max(axis=0)
        shown = r * bT / np.maximum(sE, 1e-9)
        fill = np.maximum(want - shown, 0.0) * smoothstep(self.fadeLo, self.fadeHi, sE)
        ped = self.o.get("pedRGB")
        m = np.ones(3) if ped is None else np.asarray(ped, dtype=np.float64)
        add = fill * np.minimum(sE / np.maximum(bT, 1e-9), self.gmax)
        room = np.maximum(self.glow_ceiling() - r, 0.0)
        add = add * np.minimum(1.0, room / np.maximum(add * m.max(), 1e-30))
        acts = (want > 0.0) & (add > 0.0) & self.in_lattice
        return np.where(acts[None], req + add[None] * m[:, None, None], req), np.where(acts, add, 0.0)

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

    # ---- DriveOf (curve LUT, linear filtering; the device's sub-texel fraction when subtexel_bits is set)
    def drive_of(self, stat):
        o = self.o
        N = o["curveN"]; lmin = float(o["curveLogMin"]); lmax = float(o["curveLogMax"])
        stat32 = stat.astype(np.float32)
        u = np.clip((np.log(np.maximum(stat32, f32(1e-3))) - f32(lmin)) / f32(lmax - lmin), 0, 1).astype(np.float64)
        if self.subtexel_bits:
            # the texel coordinate as the device forms it: the HLSL's float32 uv = (u (N - 1) + 0.5) / N, then N uv - 0.5
            # — where WARP truncates, a fraction a float64 u (N - 1) puts just below k / 256 is k / 256 on the device
            u32 = u.astype(np.float32)
            x = (((u32 * f32(N - 1)).astype(np.float32) + f32(0.5)) / f32(N)).astype(np.float32).astype(np.float64) * N - 0.5
        else:
            x = u * (N - 1)
        i0 = np.clip(np.floor(x).astype(int), 0, N - 1); i1 = np.minimum(i0 + 1, N - 1)
        fr = self._subtexel(x - i0, self.curve_trunc)
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

    # ---- CS stat, the boost part (u1): per zone, LIT-or-DIM / LIT-or-MEAN on the pixel's brightest channel (NOT capped
    # at white). Rule 1 sums pow(mc, gamma) over the pixels with mc > 0 in the shader's order (zone_pow_sum).
    def stat_active(self, img):
        mc = img.max(axis=0)[self.oy: self.oy + self.rows * self.ch, self.ox: self.ox + self.cols * self.cw].astype(np.float32)
        blocks = mc.reshape(self.rows, self.ch, self.cols, self.cw)
        n = f32(self.cw * self.ch)
        lit_f = (blocks > self.litNits).sum(axis=(1, 3)).astype(np.float32) / n
        if self.boostRule == 1:
            return (lit_f > self.litFrac) | (self.zone_pow_sum(blocks) / n >= self.meanThresh)
        dim_f = (blocks > self.dimNits).sum(axis=(1, 3)).astype(np.float32) / n
        return (lit_f > self.litFrac) | (dim_f > self.dimFrac)

    def zone_pow_sum(self, blocks):
        """(rows, cols) float32: per zone the shader's sum of pow(mc, meanGamma) over its pixels with mc > 0, in the
        shader's order (:meth:`zone_sweep_sum`)."""
        npx = self.cw * self.ch
        return self.zone_sweep_sum(self._pow32(blocks.transpose(0, 2, 1, 3).reshape(self.rows, self.cols, npx)))

    def _pow32(self, v):
        """HLSL (mc > 0) ? exp(gamma * log(mc)) : 0, float32."""
        pos = v > 0
        return np.where(pos, np.exp(self.meanGamma * np.log(np.where(pos, v, f32(1)), dtype=np.float32), dtype=np.float32), f32(0)).astype(np.float32)

    def zone_sweep_sum(self, vals):
        """(rows, cols) float32: per zone the sum of ``vals`` (rows, cols, cellW * cellH; the zone's pixels row-major k) in
        the zone sweeps' order (fald_shader.h above ZoneSlices, work guide C14). The pixels are cut into slices of
        ZONE_SLICE_PX, one 256-thread group each: thread t adds k = slice start + t, + 256, ... into a float32 partial,
        then the 128 / 64 / ... / 1 tree. A zone of one slice ends there (the order before C14). With more slices the
        combine group's thread t adds the slice partials t, t + 256, ... in order, then the same tree."""
        npx = vals.shape[-1]
        nsl = (npx + ZONE_SLICE_PX - 1) // ZONE_SLICE_PX
        slices = np.zeros((self.rows, self.cols, nsl), dtype=np.float32)
        for s in range(nsl):
            slices[:, :, s] = self._group_sum(vals[:, :, s * ZONE_SLICE_PX: (s + 1) * ZONE_SLICE_PX])
        return slices[:, :, 0] if nsl == 1 else self._group_sum(slices)

    @staticmethod
    def _group_sum(vals):
        """One 256-thread group's float32 sum of vals[..., i] (i ascending): thread t adds i = t, t + 256, ..., then the
        128 / 64 / ... / 1 tree (the zone sweeps' order in fald_shader.h)."""
        part = np.zeros(vals.shape[:-1] + (256,), dtype=np.float32)
        for j in range(0, vals.shape[-1], 256):              # thread t adds its element t + j, ascending j
            seg = vals[..., j: j + 256]
            part[..., : seg.shape[-1]] += seg
        stride = 128
        while stride > 0:
            part[..., :stride] += part[..., stride: 2 * stride]
            stride >>= 1
        return part[..., 0]

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

    def ceil_est(self, bE):
        """C15: the gain pass's second channel (the flat-normalised B_est the soft knee's CEILING reads), low-passed by the
        same separable blur as the gain (the shader blurs gainTex .xy together)."""
        e = bE.astype(np.float64) / np.maximum(self.flatE, 1e-6)
        return self.blur(e.astype(np.float32).astype(np.float64)).astype(np.float32)

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
    def correct(self, img, bT, bE, gain, bEc=None):
        """HLSL Correct; ``bEc`` = the pixel's sampled ceiling estimate (gainTex .y, C15) — None = the per-pixel ``bE``
        (the rule before C15, kept for offline A/B only)."""
        W = self.white
        bEc = bE if bEc is None else bEc
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
        cap = W * np.maximum(bEc, 1e-9)
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

    def run(self, frame_scrgb, fp16_out=True, temporal=None, star: Optional[StarfieldParams] = None, refreshes: int = 1,
            glow: Optional[GlowFillParams] = None):
        """One frame of FaldRunPasses. ``temporal``: a GpuDriveState carried across calls (pass 1b after each stat
        round; the state commits after round 1 — the CopyResource in the C++), or a GpuPanelDriveState (mode 3: pass 1c
        once before round 0 with ``refreshes`` = the panel refreshes elapsed since the previous frame was first shown;
        both rounds read the same maps; the commit stores round 1's instantaneous drives as the next frame's target).
        ``star``: starfield balancing settings
        (None = the option off: no star pass runs and nothing below changes) — the zone fields come from the SOURCE
        frame and every later step works on Balance(source). ``glow``: glow fill settings (None = the option off: no
        glow pass runs and nothing below changes)."""
        gp = clamp_glow(glow) if glow is not None else None
        assert gp is None or self.transfer != 1, "glow fill is HDR (PQ panel files) only"
        img = self.panel_nits(frame_scrgb)
        star_out = None
        if star is not None:
            sp = clamp_star(star)
            src = img
            st = self.star_stat(src, sp)
            plan = self.star_plan(st, sp)
            img, scale = self.balance(src, plan, sp)
            star_out = {"src": src, "stat": st, "plan": plan, "scale": scale, "params": sp}
        if temporal is not None and hasattr(temporal, "advance"):
            temporal.advance(refreshes)                               # pass 1c (mode 3): needs nothing of this frame
        d0, st0 = self.stat_drive(img)
        boost0, zones0, active0 = self.frame_boost(img)               # round 0: the source frame
        dT0, dE0 = temporal.pair(d0) if temporal is not None else (d0, d0)
        bT0, bE0 = self.fields(dT0, dE0, boost0)
        _, gB0 = self.gain(bT0, bE0)
        sT, sE, g = self.sampled(bT0, bE0, gB0)
        gc = self.sample(self.ceil_est(bE0).astype(np.float64)) if self.c15 else None
        cor0, _ = self.correct(img, sT, sE, g, gc)
        glow0 = None
        if gp is not None:                                            # round 0's fill: the round-1 statistic sees it
            glow0 = self.glow_zones(bT0, gp)
            glow0["band"] = self.glow_band(cor0, sT, sE, glow0["dz"], gp) if (gp.band and self.glow_band_active()) else None
            glow_k = glow0["band"]["k"] if glow0["band"] is not None else None
            cor0, _ = self.glow_add(cor0, sT, sE, glow0["dz"], gp, glow_k)
        d1, st1 = self.stat_drive(cor0)
        boost1, zones1, active1 = self.frame_boost(cor0)              # round 1: the corrected frame the panel receives
        dT1, dE1 = temporal.pair(d1) if temporal is not None else (d1, d1)
        bT1, bE1 = self.fields(dT1, dE1, boost1)
        graw1, gB1 = self.gain(bT1, bE1)
        sT, sE, g = self.sampled(bT1, bE1, gB1)
        cB1 = self.ceil_est(bE1)
        gc = self.sample(cB1.astype(np.float64)) if self.c15 else None
        req, wfade = self.correct(img, sT, sE, g, gc)
        glow_out = None
        if gp is not None:                                            # round 1's fill: part of the output
            glow_out = self.glow_zones(bT1, gp)
            glow_out["band"] = self.glow_band(req, sT, sE, glow_out["dz"], gp) if (gp.band and self.glow_band_active()) else None
            glow_k = glow_out["band"]["k"] if glow_out["band"] is not None else None   # round 1's OWN scale (None: no band rule)
            glow_out["k"] = glow_k
            glow_out["r0"] = glow0
            glow_out["req_nofill"] = req
            req, glow_out["add"] = self.glow_add(req, sT, sE, glow_out["dz"], gp, glow_k)
            glow_out["params"] = gp
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
                # C15: the ceiling estimate on the fine grid (fald_gain_fine.rg32f .y) and as the pixel pass samples it
                "ceil_est": cB1, "px_bEc": gc if gc is not None else sE,
                # black-frame LED boost per round (fald_dump.txt boost_r0/r1, active_zones_r0/r1, fald_active[_r0].f32)
                "boost0": 1.0 if boost0 is None else float(boost0), "boost1": 1.0 if boost1 is None else float(boost1),
                "zones0": zones0, "zones1": zones1, "active0": active0, "active1": active1,
                # starfield balancing: None when off; else the source image ("img" above is then the BALANCED one), the
                # star statistic / plan zone fields (fald_star_stat / _bg / _w / _plan / _plan2.f32) and the per-pixel scale
                "star": star_out,
                # glow fill: None when off; else round 1's zone fields (fald_glow_vz / _env / _k.f32: vz, [ez, dz, cz, vz],
                # k), the request luminance added per pixel ("add") and round 0's zone fields ("r0")
                "glow": glow_out}
