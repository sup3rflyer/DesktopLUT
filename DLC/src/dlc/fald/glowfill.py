"""Glow fill — EXPERIMENT (work guide ticket S2, owner 2026-09-20): a CALCULATED black lift that makes the LED glow on
dark content EVEN instead of splotchy. Default off.

Why. On dark content the panel shows its pedestal ``V(x) = white · B_true(x) · tmin`` — the closed-LCD leak of whatever
the LEDs do. Over a field of scattered highlights that glow is uneven at the scale of a few zones: lit zones glow, the
empty zones between them do not. Nothing can be subtracted from a pedestal; the one lever left is to ADD LCD light where
the glow is weaker than around it. The amount is derived from the panel model (the same ``B_true`` the correction uses,
so the black-frame boost, starfield balancing and the temporal drive state are already in it), not from the picture.

This module is the REFERENCE (numpy, the model's scale-5 raster, as-if-white nits). Twins that must stay in lockstep:
``dlc/fald/gpuemu.py`` (GPU order, full resolution) and ``shared/fald_shader.h`` (passes G0 - G5 + ``GlowAdd``);
``tests/test_fald_glowfill*.py`` and ``tests/test_fald_transfer.py`` pin the numbers and the shared constants.

THE RULES. Evaluated once per inverse round, from that round's drives (``correct_image``: round 0 the source frame's,
round 1 the corrected frame's — the frame the panel receives, fill included).

Per zone
   1. ``Vz`` = the zone mean of the white pedestal ``white · tmin · B_true`` over the zone's sub x sub fine-grid samples
      (``FaldModel.true_fine``: LED boost and flat-lattice normalisation included, floored at 0). As-if-white nits.
   2. ``Cz`` = the grey CLOSING of ``Vz`` with the (2·reach+1)² box: dilation (box maximum), then erosion (box minimum),
      the field continued beyond the lattice by its border values (so the closing is exact at the frame edge: ``Cz`` >=
      ``Vz`` everywhere, = ``Vz`` wherever the glow only falls away from its source). A closing fills the HOLES and
      VALLEYS narrower than 2·reach zones — the gaps between lit zones — up to the lowest level around them, and leaves
      the outer slope of a glow alone: no skirt around a bright window, no filled letterbox bars.
   3. ``Ez`` = Gaussian blur of ``Cz`` (sigma = GLOW_SIGMA_BASE + GLOW_SIGMA_PER_REACH · reach zones, radius
      ceil(3 sigma), border values held, normalised), then ``min(·, Cz)``: round and smooth inside a filled region, never
      above the closing (the blur alone would inflate every convex tail, i.e. every halo).
      ``Dz`` = (Ez − Vz) · smoothstep(DEFICIT_REL_LO, DEFICIT_REL_HI, (Ez − Vz) / Vz) where Ez > Vz, else 0: the zone's
      glow DEFICIT. A dip of less than 5 % is no hole (invisible at these levels, and the model's own frame-edge ripple
      reaches 4.4 % beside a bright window in a frame corner); from 15 % on a hole is filled in full.

Per pixel
   4. ``want`` = min(max(strength · D_px − WANT_EPS, 0), cap_nits), ``D_px`` = ``Dz`` bilinear between zone CENTRES,
      clamped (the same sampler as the starfield plan); WANT_EPS (1e-5 nit) keeps rounding dust of the fields from
      touching a pixel. The deficit is formed per ZONE and then interpolated — not ``E_px − V_px`` against
      the pixel's own full-resolution pedestal: a zone mean of a convex falloff lies above the falloff itself, and the
      difference would paint zone-periodic scallops (+8 % at 60 px) into the halo of every bright window.
   5. ``shown`` = the LCD light the panel will show for the pixel's CORRECTED request ``r`` (brightest channel):
      r · B_true / max(B_est, 1e-9). ``fill`` = max(0, want − shown) · trust, ``trust`` = smoothstep(fade_lo, fade_hi,
      B_est) — the correction's own deep-dark fade: where the panel's estimate is ~0 a tiny request opens the LCD fully,
      so there is NO fill where the model is not trusted. The fill fades out continuously as content gets brighter; a
      pixel whose content already shows ``want`` or more is returned BIT-identical.
   6. the request that displays ``fill``: ``fill · min(B_est / max(B_true, 1e-9), gain_max)`` (never raised by the lower
      gain clip: a smaller request is always safe), in the PEDESTAL'S colour ``m_c`` (the panel file's multipliers, white
      without them; luminance-neutral: Σ w_c m_c = 1), and limited so the pixel's brightest channel stays at / below
      ``REQ_CEIL`` = min(REQ_FLOOR_FRAC · drive_floor, REQ_LIT_FRAC · boost_lit_nits [with a boost table]) = 0.1925 nit
      on the PA32UCXR — the fill can never light a LED or make a zone LIT for the firmware's count. The fractions come
      from what was MEASURED (work guide, probe pixrule): a 2-px column at 0.298 nit does not make a zone LIT, 0.4 does
      (the rule's 0.35 is the midpoint), and whether a 0.3-nit AREA lights LEDs was never measured (risk R4; the 0.5-nit
      floor is a fit value) — so the ceiling keeps a factor ~1.5 below the one measured "not LIT" level and 2.5 below
      the drive floor instead of sitting on them.
Within the model — with the round's OWN fields — the panel then shows ``V + shown + fill`` at the pixel: on black the
LCD adds exactly ``want · trust`` (x min(1, gain_max · B_true / B_est)), never more than ``want``; the zone mean of the
glow never exceeds ``Ez``. Against the fields of the frame that is finally SENT (one more inverse round: its drives
differ slightly from round 1's) the bound is not exact where B_est is small and steep: real frame xmas_20, 73 of 102 277
filled pixels showed more than want + 1 %, the worst + 25 % = + 0.013 nit (cap 0.10).

WHY A CLOSING (stress tests 2026-09-20, ``results/fald_inside_2026-09-18/glowfill/``; the first form of the spec was the
blur of the box MAXIMUM against the pixel's own pedestal, kept here as the offline switch ``envelope="dilate"``). The
maximum extends every glow by ``reach`` zones: a 1000-nit window on black got a 0.10-nit skirt (black at 200 px 0.106 ->
0.200 nit), letterbox bars filled at the cap along the whole picture edge, the non-black zone count of that frame went
100 -> 259 (LED boost 1.167 -> 1.071: the window itself 8 % dimmer). And the trust factor (item 5) cuts any fill off
where the panel's estimate is ~0 — exactly the far, dark zones an "even glow" would need — so the maximum rule builds a
bright mesa that ENDS at the trust contour: on the owner's six real frames the dark-sky unevenness got WORSE (+12 .. +68
%) for +50 .. +70 % more black light. The closing only fills what is enclosed by glow: window 0 added, bars only in the
valleys between bright picture areas, real frames: 1-4-zone splotch contrast inside the trusted dark zones -5 .. -23 %
for +2 .. +10 % light. What it cannot do: even out a glow whose surroundings the panel's estimate does not reach
(Gravity: nothing to fill, nothing filled).

The boost loop (fill -> non-black zone count N -> boost -> B_true -> fill). A zone filled at >= ~0.0135 nit counts for the
firmware (mean rule). Round 0 computes the fill with the SOURCE frame's boost and adds it to the request; round 1 reads
N from that request (fill included), so the final fill is computed with the boost of a frame that already carries a
fill. What is left: the sent frame's N can differ from round 1's by the zones whose fill crosses the count threshold
when the boost moves between the rounds — see ``predict`` (``zones_sent`` vs ``zones_assumed``).

   7. THE COUNT-THRESHOLD BAND (review 2026-09-20; panel files with a boost table AND the mean zone rule only). A smooth
      fill necessarily parks zones AT the firmware's count threshold T (zone mean of request^gamma; measured to +-7 %):
      12-13 zones within +-15 % of it in a panned star lattice, where a miscount near a stair edge is a whole-frame
      error of 1-3 % (7-9.6 % at the dead band) — an exposure the layer does not have without the fill. So in EACH
      round, per zone, the statistic the firmware will form is PREDICTED on that round's request — ``Pf`` = zone
      mean of (brightest channel of request + fill)^gamma, ``Pc`` = the same without the fill — and a zone that is not
      counted because of its content (not LIT, ``Pc`` < T) and whose ``Pf`` falls inside [BAND_LO T, BAND_HI T] (``band0``)
      gets ``k = ((BAND_LO T − Pc) / (Pf − Pc))^(1 / gamma)`` (0 .. 1), which puts the prediction at BAND_LO T: the zone
      stays clearly uncounted. Never up; zones counted by their content are left alone; a zone filled well above the band
      is clearly counted. ``k`` is formed in EVERY round from that round's own request and fields — round 1's k is exact
      for the frame that is sent; a k carried over from round 0 is not (dim-star lattice, fill in the B_est fade band: the
      trust factor moves between the rounds, round 0 predicted 0.97 T where the sent frame had 0.63 T, and parked zones
      the prediction had not seen).
      THE FEATHER (C16, 2026-09-22). A k per zone is exact for its own zone, but applied as the pixel's OWN zone's k (a
      nearest lookup, the rule until C16) it printed the lattice: on 200-nit windows over a 0.004-0.012-nit sky 38-285 band
      zones, displayed steps of +15-21 % at 0.06-0.19 nit on straight zone edges. The pixel's scale is now
      ``s = min(k_z, min over the EXISTING neighbours n of 1 − (1 − k_n) w_n)``, ``w_n = 1 − smoothstep(0, FEATHER,
      distance from the pixel to n's rectangle, in zones)``, applied where k was (want x s, after the cap): continuous
      across every zone edge (C1 at the edge; between two band zones of close k the min switches branch ~0.06 zone inside
      the higher one: a kink, not a step), <= k_z inside a band zone (the zone never exceeds what the band assumed),
      exactly 1 where no band zone lies within FEATHER — there a pixel takes exactly the fill of the rule without the band.
      (FEATHER < 0.5: only the 3 neighbours on the pixel's side of its zone can reach it; the GPU evaluates those.) A plain
      blur of k moves fill between zones (a neighbour pushed into the margin — 134-189 zones on dense synthetic scenes); an
      inward ramp cannot fit the zone's budget. So the ramp lies OUTSIDE the band zone, and THE NEIGHBOUR GUARD keeps
      what it takes from the neighbours out of the margin. Per pixel, F(σ) = its statistic (brightest channel of the
      request + the fill at scale σ)^gamma is flat below s0 = clamp(shown / want, 0, 1) (the fill starts only above what
      the pixel already shows) and, between its kinks, concave (a linear request, one brightest channel, under the
      concave power); the kinks are s0, the fill levels where the brightest channel changes (pedestal multipliers that
      differ — none with a white pedestal) and the request ceiling's cap (F flat above it). So ``q = the largest chord
      slope (F(1) − F(σ_k)) / (1 − σ_k)`` over those kinks (:func:`bound_slope`; white pedestal: (F(1) − F(0)) / (1 − s0))
      gives F(1) − F(σ) <= (1 − σ) q for EVERY σ, and per zone and neighbour direction d (8, fixed order NEIGHBOURS)
      ``A_d = mean_p [w_d(p) q_p]`` (0 toward a neighbour outside the lattice) bounds how far a neighbour at scale k_n can
      lower the zone's statistic: ``(1 − k_n) A_d`` (the min over neighbours <= their sum). The zones counted only by the
      fill (not LIT, Pc < T, Pf > BAND_HI T, not band0) whose ``Pf − Σ_d (1 − k_{z+d}) A_d`` drops below BAND_HI T join
      the band with the same k formula; Jacobi iterations (every zone reads the previous iteration's k) until none joins.
      A chain of joins advances one zone per iteration (stripes: 43-45, or 73 on the in-repo fit), so the cap is
      GUARD_ITER_MAX 64, and if the cap ends it while its last iteration still added zones, ONE worst-case pass bands
      every candidate whose ``Pf − Σ_d A_d`` (every neighbour at k = 0) is below BAND_HI T — the guarantee holds whatever
      the cap (``converged`` / ``worst_case`` in the evidence, fald_glow_guard.f32 on the GPU). Within the model: band
      zones end <= their band0 prediction (≈ BAND_LO T), fill-counted zones not banded stay >= BAND_HI T, zones below
      BAND_LO T stay below, content-counted zones are unaffected. Offline (owner meanrule fit, scale-5 raster, cap 0.05;
      the table's scenes converged in 1-6 iterations): 200-nit windows on a 0.004 / 0.008-nit sky — the largest relative
      step of the displayed fill across a zone edge 45.8 -> 8.7 % / 63.2 -> 3.1 % (a band zone's edge to an unbanded one:
      never a larger step than inside the two zones; between two band zones of close k the ramp takes the k difference
      within one raster pixel — at 0.004, 119 such edges, <= 2.9 % of the local level, was 31 %), band zones 626 -> 792 /
      981 -> 1254, zones parked inside the margin 0 / 0, fill -41 / -97 %; the six owner clips kept their margin counts
      (xmas_20 band 57 -> 71), fill -1 .. -5 %. (The 1 / (1 − s0) of A_d matters: the prototype's bound without it banded
      1184 zones at 0.008 and left 5 inside the margin.) The price: in dense near-threshold synthetic scenes the guard
      bands whole fill-counted regions (fill -41 .. -100 %: safe — smooth and uncounted — but no evening there), the
      worst-case pass more than the converged guard would; a coloured pedestal's kinks raise A up to ~3 x (more zones
      banded); a zone crossing BAND_HI T still changes its fill by x ~0.49 in one frame (now a ramped patch, not a
      hard-edged one); two more passes per round on the GPU (the band sweep G4 accumulates the 8 A_d; the guard G5 is one
      thread group on the zone lattice).

HDR only: every level behind the request ceiling and the band (drive floor, LIT level, count threshold) was measured in
HDR; a gamma-transfer (SDR / ACM) fit is refused (``ValueError``; the C++ keeps the option off and says why).

Parameters (``GlowFillParams`` defaults = C++ ``FaldGlowSettings`` = the mock's): strength 1 (0..1), reach 2 (1..4 zones),
cap_nits 0.05 (0.005..0.5).
Constants: GLOW_SIGMA_BASE 0.5, GLOW_SIGMA_PER_REACH 0.5, DEFICIT_REL_LO 0.05, DEFICIT_REL_HI 0.15, WANT_EPS 1e-5,
REQ_FLOOR_FRAC 0.4, REQ_LIT_FRAC 0.55, BAND_LO 0.8, BAND_HI 1.25, FEATHER 0.35, GUARD_ITER_MAX 16.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

import numpy as np

from .model import FaldModel

REACH_MIN, REACH_MAX = 1, 4              # C++ FALD_GLOW_REACH_MIN / _MAX
CAP_MIN, CAP_MAX = 0.005, 0.5            # C++ FALD_GLOW_CAP_MIN / _MAX (as-if-white nits)
GLOW_SIGMA_BASE = 0.5                    # HLSL FALD_GLOW_SIGMA_BASE / _PER_REACH: blur sigma (zones) = base + per_reach * reach
GLOW_SIGMA_PER_REACH = 0.5
DEFICIT_REL_LO = 0.05                    # HLSL FALD_GLOW_DEFICIT_REL_LO / _HI: a dip this shallow (relative to the zone's own glow)
DEFICIT_REL_HI = 0.15                    # is no hole ... from here on it is filled in full (smoothstep between)
WANT_EPS = 1e-5                          # HLSL FALD_GLOW_WANT_EPS: a deficit below this (as-if-white nits) is no deficit
REQ_FLOOR_FRAC = 0.4                     # C++ FALD_GLOW_REQ_FLOOR_FRAC: a filled pixel's request stays below this x drive floor
REQ_LIT_FRAC = 0.55                      # C++ FALD_GLOW_REQ_LIT_FRAC: ... and below this x the boost count's LIT level
                                         # (PA32UCXR: min(0.2, 0.1925) nit; the one measured "not LIT" point is 0.298 nit)
BAND_LO, BAND_HI = 0.8, 1.25             # HLSL FALD_GLOW_BAND_LO / _HI: the count-threshold band, x the mean rule's threshold
FEATHER = 0.35                           # HLSL FALD_GLOW_FEATHER (C16): zones — the band scale's ramp width outside a band zone
GUARD_ITER_MAX = 64                      # HLSL FALD_GLOW_GUARD_ITER_MAX (C16): cap of the neighbour guard's Jacobi iterations
                                         # (a chain of joins needs one each: stripes 43-45; beyond it the worst-case pass)
# (i, j) = (zone column, zone row) offsets of the 8 neighbours, in the order of A_d (HLSL G4 / G5: 3 x 3 row-major, centre skipped)
NEIGHBOURS = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))


@dataclass(frozen=True)
class GlowFillParams:
    strength: float = 1.0                # share of the glow deficit that is filled (0 = off, 1 = up to the envelope)
    reach: int = 2                       # zones (each side): holes / valleys up to 2·reach zones wide are filled
    cap_nits: float = 0.05               # the fill never exceeds this (as-if-white nits)
    envelope: str = "close"              # OFFLINE-ONLY switch kept for the stress tests: "close" (the rule) | "dilate" (the
                                         # 2026-09-20 spec's first form: blur of the box maximum — paints a skirt)
    band: bool = True                    # OFFLINE-ONLY switch: False = without the count-threshold band (item 7), to cost it
    band_feather: bool = True            # OFFLINE-ONLY switch: False = the band before C16 (the pixel's OWN zone's k, no
                                         # neighbour guard) — to compare / cost it


def clamp_params(gp: GlowFillParams) -> GlowFillParams:
    """C++ FaldGlowClamp: every field into its documented range (NaN -> the default)."""
    d = GlowFillParams()
    f = lambda v, lo, hi, dv: float(min(max(float(v), lo), hi)) if np.isfinite(v) else dv
    return replace(gp, strength=f(gp.strength, 0.0, 1.0, d.strength), reach=int(min(max(int(gp.reach), REACH_MIN), REACH_MAX)),
                   cap_nits=f(gp.cap_nits, CAP_MIN, CAP_MAX, d.cap_nits))


def _smoothstep(lo: float, hi: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - lo) / max(hi - lo, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def pedestal_colour(model: FaldModel) -> np.ndarray:
    """m_c (3,): the fill's colour = the panel file's pedestal multipliers whatever ``ped_mode`` says; white without them."""
    rgb = model.p.tmin_rgb
    return np.ones(3) if rgb is None else np.asarray(rgb, dtype=float)


def req_ceiling(model: FaldModel) -> float:
    """The level (as-if-white nits, brightest channel) a filled pixel's request never exceeds."""
    p = model.p
    c = REQ_FLOOR_FRAC * p.drive_floor_nits
    return min(c, REQ_LIT_FRAC * p.boost_lit_nits) if p.boost_lut else c


def zone_pedestal(model: FaldModel, d_true: np.ndarray, boost: float = 1.0) -> np.ndarray:
    """Item 1: ``Vz`` (rows, cols), as-if-white nits."""
    p = model.p
    fine = model.true_fine(d_true, boost)
    return p.white_nits * p.tmin * fine.reshape(p.rows, p.sub, p.cols, p.sub).mean(axis=(1, 3))


def _box_ext(a: np.ndarray, r: int, fn) -> np.ndarray:
    """``fn`` (np.max / np.min) over the (2r+1)² neighbourhood of every texel of ``a`` — ``a`` is NOT padded here: the
    output has the input's shape minus r on every side (the caller pads)."""
    rows, cols = a.shape[0] - 2 * r, a.shape[1] - 2 * r
    return fn([a[r + dy: r + dy + rows, r + dx: r + dx + cols] for dy in range(-r, r + 1) for dx in range(-r, r + 1)], axis=0)


def closing(vz: np.ndarray, reach: int) -> np.ndarray:
    """Item 2: grey closing with the (2·reach+1)² box; the field is continued by its border values (edge padding by
    2·reach before the dilation), so ``closing >= vz`` and a field falling monotonically toward the frame edge is kept."""
    r = int(reach)
    ext = np.pad(vz, 2 * r, mode="edge")
    dil = _box_ext(ext, r, np.max)                       # valid on the lattice extended by r
    return _box_ext(dil, r, np.min)


def dilation(vz: np.ndarray, reach: int) -> np.ndarray:
    r = int(reach)
    return _box_ext(np.pad(vz, r, mode="edge"), r, np.max)


def blur_sigma(reach: int) -> float:
    return GLOW_SIGMA_BASE + GLOW_SIGMA_PER_REACH * int(reach)


def gauss_blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Normalised Gaussian, radius ceil(3 sigma), border values held (2-D weights exp(-(dx² + dy²) / 2 sigma²))."""
    R = int(np.ceil(3.0 * sigma))
    k = np.exp(-0.5 * np.arange(-R, R + 1) ** 2 / (sigma * sigma))
    k2 = np.outer(k, k); k2 /= k2.sum()
    rows, cols = a.shape
    pad = np.pad(a, R, mode="edge")
    out = np.zeros_like(a, dtype=float)
    for j in range(2 * R + 1):
        for i in range(2 * R + 1):
            out += k2[j, i] * pad[j: j + rows, i: i + cols]
    return out


def envelope(vz: np.ndarray, gp: GlowFillParams) -> np.ndarray:
    """Items 2 + 3: ``Ez``."""
    if gp.envelope == "dilate":
        return gauss_blur(dilation(vz, gp.reach), blur_sigma(gp.reach))
    if gp.envelope != "close":
        raise ValueError(f"envelope must be 'close' or 'dilate', got {gp.envelope!r}")
    cz = closing(vz, gp.reach)
    return np.minimum(gauss_blur(cz, blur_sigma(gp.reach)), cz)


def deficit(vz: np.ndarray, ez: np.ndarray) -> np.ndarray:
    """Item 3: ``Dz``."""
    d = np.maximum(ez - vz, 0.0)
    return d * _smoothstep(DEFICIT_REL_LO, DEFICIT_REL_HI, d / np.maximum(vz, 1e-12))


def check_supported(model: FaldModel) -> None:
    """The fill is HDR only (module docstring): refuse a gamma-transfer (SDR / ACM) fit."""
    if model.p.transfer != "pq":
        raise ValueError("glow fill is HDR only: the levels behind its request ceiling (drive floor, LIT level, count "
                         f"threshold) are HDR measurements (transfer {model.p.transfer!r})")


def zone_fields(model: FaldModel, d_true: np.ndarray, boost: float, gp: GlowFillParams) -> dict:
    """The zone fields of a round: ``vz``, ``ez`` and ``dz`` (the dump's fald_glow_vz.f32 / fald_glow_env.f32 [ez, dz, cz,
    vz])."""
    check_supported(model)
    gp = clamp_params(gp)
    vz = zone_pedestal(model, d_true, boost)
    ez = envelope(vz, gp)
    dz = deficit(vz, ez)
    return {"vz": vz, "ez": ez, "dz": dz}


def band_active(model: FaldModel) -> bool:
    """Item 7 applies: the fit has a boost table AND the mean zone rule (the only rule with a threshold to park at)."""
    return bool(model.p.boost_lut) and model.p.boost_rule == "mean"


def zone_local(model: FaldModel) -> tuple:
    """Item 7 (C16) geometry on the model's raster: per pixel column / row its zone ``zx`` (w,) / ``zy`` (h,) and its
    zone-local position ``u`` = (x + 0.5) / cw − zx, ``v`` = (y + 0.5) / ch − zy, both in [0, 1) (HLSL GlowZoneLocal)."""
    p = model.p
    xs = (np.arange(model.w) + 0.5) / model.cw
    ys = (np.arange(model.h) + 0.5) / model.ch
    zx = np.minimum(np.floor(xs).astype(int), p.cols - 1)
    zy = np.minimum(np.floor(ys).astype(int), p.rows - 1)
    return zx, zy, xs - zx, ys - zy


def feather_weight(i: int, j: int, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Item 7 (C16): ``w`` (h, w) of the neighbour at zone offset (i, j) for pixels at zone-local ``u`` (w,) / ``v`` (h,):
    1 on the neighbour's rectangle, C1 down to 0 at FEATHER zones from it (HLSL GlowFeatherW; zone units, so the ramp
    adapts to any lattice)."""
    dx = np.maximum(0.0, np.maximum(i - u, u - (i + 1)))
    dy = np.maximum(0.0, np.maximum(j - v, v - (j + 1)))
    return 1.0 - _smoothstep(0.0, FEATHER, np.sqrt(dy[:, None] * dy[:, None] + dx[None, :] * dx[None, :]))


def neighbour_weights(model: FaldModel) -> np.ndarray:
    """(8, h, w): ``w_d`` of every pixel toward its zone's neighbour d (order NEIGHBOURS); 0 where that neighbour lies
    outside the lattice."""
    p = model.p
    zx, zy, u, v = zone_local(model)
    out = np.empty((len(NEIGHBOURS), model.h, model.w))
    for d, (i, j) in enumerate(NEIGHBOURS):
        ok = ((zy + j >= 0) & (zy + j < p.rows))[:, None] & ((zx + i >= 0) & (zx + i < p.cols))[None, :]
        out[d] = np.where(ok, feather_weight(i, j, u, v), 0.0)
    return out


def band_pixel_scale(model: FaldModel, k: np.ndarray) -> np.ndarray:
    """Item 7 (C16), the pixel side: ``s = min(k_z, min over the existing neighbours n of 1 − (1 − k_n) w_n)`` (h, w) —
    continuous across every zone edge (C1 there; a kink ~0.06 zone inside the higher of two band zones of close k), <= k_z
    inside a band zone, EXACTLY 1 wherever no band zone (k < 1) lies within FEATHER of the pixel (1 − 0 · w = 1)."""
    p = model.p
    zx, zy, _, _ = zone_local(model)
    k = np.asarray(k, dtype=float)
    s = k[np.ix_(zy, zx)]
    if not (k < 1.0).any():
        return s
    w = neighbour_weights(model)
    for d, (i, j) in enumerate(NEIGHBOURS):
        kn = k[np.ix_(np.clip(zy + j, 0, p.rows - 1), np.clip(zx + i, 0, p.cols - 1))]
        s = np.minimum(s, 1.0 - (1.0 - kn) * w[d])                      # w = 0 toward a missing neighbour: no effect
    return s


def _guard_loss(k: np.ndarray, a: np.ndarray, dtype) -> np.ndarray:
    """``Σ_d (1 − k_{z+d}) A_d`` per zone, NEIGHBOURS order, a neighbour outside the lattice adding nothing (G5's sum)."""
    rows, cols = k.shape
    one = k.dtype.type(1.0)
    kp = np.pad(k, 1, constant_values=one)
    loss = np.zeros((rows, cols), dtype=dtype)
    for d, (i, j) in enumerate(NEIGHBOURS):
        ok = np.zeros((rows, cols), dtype=bool)
        ok[max(0, -j): rows - max(0, j), max(0, -i): cols - max(0, i)] = True
        term = ((one - kp[1 + j: 1 + j + rows, 1 + i: 1 + i + cols]) * a[d]).astype(dtype)
        loss = np.where(ok, loss + term, loss).astype(dtype)
    return loss


def guard(k0: np.ndarray, band0: np.ndarray, cand: np.ndarray, k_join: np.ndarray, pf: np.ndarray, a: np.ndarray,
          hi: float, iter_max: int = GUARD_ITER_MAX) -> dict:
    """Item 7 (C16), the neighbour guard (HLSL pass G5): Jacobi iterations from ``k0`` — per zone ``loss = Σ_d (1 −
    k_{z+d}) A_d`` (order NEIGHBOURS; a neighbour outside the lattice adds nothing), every zone reading the PREVIOUS
    iteration's k; every candidate not yet banded whose ``pf − loss < hi`` joins with ``k_join``. Until no zone joins
    (``converged``), at most ``iter_max`` iterations (the band set only grows). If the cap ends it while its last iteration
    still added zones, ONE worst-case pass follows: every candidate still out of the band whose ``pf − Σ_d A_d < hi`` (every
    neighbour taken at k = 0) joins — so the guarantee holds whatever the cap. Returns ``k``, ``band``, ``added`` (the
    guard's zones), ``iterations`` (evaluated, the last one included), ``converged``, ``worst_case`` (the pass ran) and
    ``worst_case_added`` (zones it banded). The dtype of ``k0`` / ``pf`` is kept: the twin runs it in float32."""
    k, band = k0.copy(), band0.copy()
    iterations, converged = 0, False
    for it in range(iter_max):
        iterations = it + 1
        new = cand & ~band & ((pf - _guard_loss(k, a, pf.dtype)).astype(pf.dtype) < hi)
        if not new.any():
            converged = True
            break
        band = band | new
        k = np.where(new, k_join, k).astype(k0.dtype)
    worst_added = np.zeros_like(band)
    if not converged:
        worst_added = cand & ~band & ((pf - _guard_loss(np.zeros_like(k), a, pf.dtype)).astype(pf.dtype) < hi)
        band = band | worst_added
        k = np.where(worst_added, k_join, k).astype(k0.dtype)
    return {"k": k, "band": band, "added": band & ~band0, "iterations": iterations, "converged": converged,
            "worst_case": not converged, "worst_case_added": worst_added}


def bound_slope(req: np.ndarray, m: np.ndarray, want: np.ndarray, shown: np.ndarray, add_lum: np.ndarray,
                add_lum_u: np.ndarray, cpow: np.ndarray, fpow: np.ndarray, gamma: float) -> np.ndarray:
    """Item 7 (C16): per pixel the slope ``q`` of the neighbour bound — the LARGEST chord slope ``(F(1) − F(σ)) / (1 − σ)``
    of the pixel's statistic ``F(σ)`` = (brightest channel of ``req + a(σ) m``)^gamma over the fill scale σ in [0, 1).
    ``a(σ)`` = the fill's request luminance at scale σ: 0 up to s0 = shown / want, then linear (``add_lum_u`` at σ = 1
    uncapped), capped by the request ceiling (``add_lum`` = what σ = 1 really adds). Between its kinks F is concave (a
    linear request, one brightest channel, under the concave power), so the largest chord slope to σ = 1 is taken AT a
    kink: σ = s0 (below it F is flat), the points where the brightest channel changes (channel pairs of different
    pedestal multipliers m_c; a white pedestal has none), and the ceiling's cap point (above it F is flat at F(1): its
    chord is 0). With a white pedestal q = (F(1) − F(0)) / (1 − s0). 0 where want <= 0 or s0 >= 1 (nothing is filled)."""
    s0 = np.clip(shown / np.where(want > 0.0, want, 1.0), 0.0, 1.0)
    live = (want > 0.0) & (s0 < 1.0)
    rest = np.where(live, 1.0 - s0, 1.0)
    q = np.where(live, (fpow - cpow) / rest, 0.0)
    for i, j in ((0, 1), (0, 2), (1, 2)):
        dm = float(m[j] - m[i])
        if dm == 0.0:
            continue
        ak = (req[i] - req[j]) / dm                                     # channels i and j cross at a = ak
        ok = live & (ak > 0.0) & (ak < add_lum)
        if not ok.any():
            continue
        ak = np.where(ok, ak, 0.0)
        fk = np.power(np.maximum((req + ak[None] * m[:, None, None]).max(axis=0), 0.0), gamma)
        chord = (fpow - fk) / (rest * (1.0 - ak / np.where(ok, add_lum_u, 1.0)))
        q = np.where(ok, np.maximum(q, chord), q)
    return q


def band_scale(model: FaldModel, req: np.ndarray, b_true: np.ndarray, b_est: np.ndarray, dz: np.ndarray, ez: np.ndarray,
               gp: GlowFillParams, gain_max: float = 4.0) -> dict:
    """Item 7: the per-zone scale ``k`` (rows, cols) the pixels' want is scaled by (through the feather,
    :func:`band_pixel_scale`), from the request ``req`` of the round (without fill) and the fill the unscaled rule would
    add. Returns ``k`` (final), ``k0`` / ``band0`` (the zones the prediction puts in the band), ``band`` (final), ``pf`` /
    ``pc`` (the predicted statistic with / without the fill), ``lit``, the neighbour guard's evidence (HLSL G5:
    ``guard_added``, ``iterations``, ``converged``, ``worst_case``, ``worst_case_added``), ``A`` (8, rows, cols: the
    neighbour bound, HLSL G4) and ``q`` (per pixel, :func:`bound_slope`). All ones / empty when the fit has no mean rule /
    boost table. ``gp.band_feather`` False: the band before C16 (k = k0, no guard; the pixels take their own zone's k)."""
    p = model.p
    ones = np.ones((p.rows, p.cols))
    none = np.zeros((p.rows, p.cols), dtype=bool)
    idle = {"guard_added": none, "iterations": 0, "converged": True, "worst_case": False, "worst_case_added": none,
            "A": None, "q": None}
    if not (gp.band and band_active(model)):
        return {"k": ones, "k0": ones, "pf": None, "pc": None, "lit": None, "band": none, "band0": none, **idle}
    f = round_fill(model, req, b_true, b_est, dz, ez, gp, gain_max)
    zmean = lambda a: a.reshape(p.rows, model.ch, p.cols, model.cw).mean(axis=(1, 3))  # noqa: E731
    rc = req.max(axis=0)
    rf = (req + f["add"]).max(axis=0)
    g, t = float(p.boost_mean_gamma), float(p.boost_mean_thresh)
    cpow = np.power(np.maximum(rc, 0.0), g)
    fpow = np.power(np.maximum(rf, 0.0), g)
    pc, pf = zmean(cpow), zmean(fpow)
    lit = zmean((rc > p.boost_lit_nits).astype(float)) > p.boost_lit_frac
    band0 = (~lit) & (pc < t) & (pf >= BAND_LO * t) & (pf <= BAND_HI * t)
    k_join = np.power(np.clip((BAND_LO * t - pc) / np.maximum(pf - pc, 1e-30), 0.0, 1.0), 1.0 / g)
    k0 = np.where(band0, k_join, 1.0)
    out = {"k": k0, "k0": k0, "pf": pf, "pc": pc, "lit": lit, "band": band0, "band0": band0, **idle}
    if not gp.band_feather:
        return out
    # the neighbour bound A_d (HLSL G4): a neighbour at scale k lowers the zone's statistic by at most (1 - k) A_d
    q = bound_slope(req, pedestal_colour(model), f["want"], f["shown"], f["add_lum"], f["add_lum_u"], cpow, fpow, g)
    w = neighbour_weights(model)
    a = np.stack([zmean(w[d] * q) for d in range(len(NEIGHBOURS))])
    # the neighbour guard (HLSL G5): the zones counted only by the fill that the feather could pull below BAND_HI T
    cand = (~lit) & (pc < t) & (pf > BAND_HI * t) & ~band0
    gd = guard(k0, band0, cand, k_join, pf, a, BAND_HI * t, iter_max=GUARD_ITER_MAX)
    out.update(k=gd["k"], band=gd["band"], guard_added=gd["added"], iterations=gd["iterations"], converged=gd["converged"],
               worst_case=gd["worst_case"], worst_case_added=gd["worst_case_added"], A=a, q=q)
    return out


def round_fill(model: FaldModel, req: np.ndarray, b_true: np.ndarray, b_est: np.ndarray, dz: np.ndarray, ez: np.ndarray,
               gp: GlowFillParams, gain_max: float = 4.0, k: Optional[np.ndarray] = None) -> dict:
    """Items 4-6 for every pixel. ``req`` = the round's corrected request WITHOUT fill (3, h, w); ``b_true`` / ``b_est`` =
    the round's pixel fields; ``dz`` = the zone deficit; ``k`` = the count-threshold band's zone scale (item 7; None = 1),
    reaching the pixels through the feather (:func:`band_pixel_scale`). Returns ``add`` (3, h, w; exactly 0 where nothing
    is filled), ``fill`` (the luminance the panel is asked to add, as-if-white nits), ``want`` (after the band's scale),
    ``s`` (the band's scale per pixel), ``trust``, ``e_px``, ``v_px``, ``shown``, ``add_lum`` (the request luminance the fill
    adds, in the pedestal's colour: add = add_lum x m) and ``add_lum_u`` (the same before the request ceiling)."""
    from .starfield import _bilinear_zones, _nearest_zones
    check_supported(model)
    gp = clamp_params(gp)
    p = model.p
    e_px = _bilinear_zones(model, ez)
    v_px = p.white_nits * p.tmin * np.maximum(b_true, 0.0)
    if gp.envelope == "dilate":                                          # the spec's first form: against the pixel's own pedestal
        d_px = np.maximum(e_px - v_px, 0.0)
    else:                                                                # the rule: the ZONE-level deficit, interpolated
        d_px = _bilinear_zones(model, dz)
    want = np.minimum(np.maximum(gp.strength * d_px - WANT_EPS, 0.0), gp.cap_nits)
    s = np.ones_like(want)
    if k is not None:                                                    # after the cap; before C16: the pixel's OWN zone
        s = band_pixel_scale(model, k) if gp.band_feather else _nearest_zones(model, np.asarray(k, dtype=float))
        want = want * s
    r = req.max(axis=0)
    shown = r * np.maximum(b_true, 0.0) / np.maximum(b_est, 1e-9)
    trust = _smoothstep(p.fade_lo, p.fade_hi, b_est)
    fill = np.maximum(want - shown, 0.0) * trust
    m = pedestal_colour(model)
    add_lum_u = fill * np.minimum(b_est / np.maximum(b_true, 1e-9), gain_max)
    room = np.maximum(req_ceiling(model) - r, 0.0)                       # what the brightest channel may still take
    scale = np.minimum(1.0, room / np.maximum(add_lum_u * m.max(), 1e-30))
    add_lum = add_lum_u * scale
    add = add_lum[None] * m[:, None, None]
    return {"add": np.where(add_lum[None] > 0.0, add, 0.0), "fill": fill * scale, "want": want, "s": s, "trust": trust,
            "e_px": e_px, "v_px": v_px, "shown": shown, "add_lum": add_lum, "add_lum_u": add_lum_u}


def fill_image(model: FaldModel, img: np.ndarray, gp: Optional[GlowFillParams] = None, **kw) -> dict:
    """``correct_image`` with the glow fill on (``gp`` None = the defaults): its result dict, with the last round's
    evidence under ``"glow"``."""
    from .correct import correct_image
    return correct_image(model, img, glow=GlowFillParams() if gp is None else gp, **kw)


def predict(model: FaldModel, img: np.ndarray, gp: Optional[GlowFillParams] = None, **kw) -> dict:
    """What the model says the panel shows for the layer's output without / with the fill (luminance fields, nits), the
    fill itself, and the boost-loop bookkeeping: ``zones_assumed`` = the non-black zone count the final fields were
    computed with (round 1's input), ``zones_sent`` = the count of the frame that is actually sent."""
    from .correct import correct_image
    off = correct_image(model, img, **kw)
    on = fill_image(model, img, gp, **kw)
    y_off = model.forward_img(off["req"])
    y_on = model.forward_img(on["req"])
    return {"off": off, "on": on, "y_off": y_off["y"].sum(axis=0), "y_on": y_on["y"].sum(axis=0),
            "fwd_off": y_off, "fwd_on": y_on,
            "zones_assumed": int(model.active_zones(on["glow"]["round_input"]).sum()),
            "zones_sent": int(model.active_zones(on["req"]).sum()),
            "zones_off": int(model.active_zones(off["req"]).sum()),
            "boost_assumed": float(on["boost"]), "boost_sent": float(y_on["boost"]), "boost_off": float(y_off["boost"])}
