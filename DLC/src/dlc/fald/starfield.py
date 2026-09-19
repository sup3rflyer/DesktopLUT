"""Starfield balancing — EXPERIMENT (work guide ticket S1, owner 2026-09-18): EVEN OUT the backlight over a field of
scattered tiny highlights. It changes the content on purpose; the question is how imperceptible it can be made.

Why (HW 2026-09-18, work guide "HW 2026-09-18" item 2h). On a mini-LED panel a sparse sub-zone highlight is shown with
the LCD wide open, so its luminance AND the haze around it are both set by the LED drive of its zone — and that drive
follows the highlight's REQUESTED level (~request^0.6), hardly its size: one white pixel drives its zone to ~12 %.
Real content rarely sits on a code-0 black, so the zones of such a field are lit anyway; what shows is the UNEVENNESS —
the zone holding a brighter speck drives harder than its neighbours, a zone-shaped patch of haze that comes and goes
as the specks move. Pixel-side compensation cannot remove it near black (nothing can go below the pedestal). The one
lever is the requested level of the specks themselves: equal peaks -> equal drives -> an even veil instead of patches.

This module is the REFERENCE (numpy, the model's scale-5 raster, as-if-white nits). Twins that must stay in lockstep:
``dlc/fald/gpuemu.py`` (GPU order, full resolution) and ``src/fald_shader.h`` (passes S0 / S1 / S2 + ``Balance``);
``tests/test_fald_starfield*.py`` and ``tests/test_fald_transfer.py`` pin the numbers and the shared constants.

THE RULES (final, 2026-09-19, review round 6). s = a pixel's brightest channel clipped to white; ss = smoothstep with a
floored denominator; box(r) = the (2r+1)^2 zone neighbourhood, zero outside the lattice; d = chebyshev zone distance.

Per zone, in this order
   1. floored statistic (the layer's own): ``peak`` = max s, ``total`` = sum s over pixels ABOVE the drive floor;
      ``has`` = peak > 0 (the brightest pixel is lit); ``drive`` = DriveOf(min(peak, total / A0)).
   2. un-gated statistic over ALL pixels (real content never sits on code 0: a lit sky must not count as lit area):
      ``peak_all`` = max s, ``b`` = min s (the zone's BACKGROUND), ``sum_all``, ``n`` = pixel count (px^2), and
      ``arg`` = the zone-local position (lx, ly) of the brightest pixel (ties: the one nearest the zone border —
      largest max(|2 lx - (cw - 1)| ch, |2 ly - (ch - 1)| cw) — then the first in row-major order).
   3. ``speck`` = peak_all - b > max(FLAT_ABS, FLAT_REL * peak_all)  (a flat zone has no speck; the quotient below is
      never formed for it);  ``a_eff`` = (sum_all - b n) / (peak_all - b) = px^2 at the peak level ABOVE the background
      (0 when not speck). Limitation: b is the MINIMUM, so a sky varying inside the zone over-counts the area by
      (mean sky - min sky) n / (peak - b) — a 1 -> 3-nit ramp under a 100-nit star: + 36 px^2.
   4. ``sparse`` = 1 - ss(area_lo, area_hi, a_eff), x (1 - ss(peak_hi, 2 peak_hi, peak)) when peak_hi > 0; 0 unless
      has and speck.  ``solid`` = (1 - sparse) * drive * has  (the zone as ordinary content; a star-free zone of a lit
      sky is "solid" at its dim drive — 0.03 .. 0.11 for 1 .. 20 nits on the PA32UCXR fit, below nb_lo).
   5. ``spk`` = has and speck and a_eff < area_hi — the SPECK-ZONE flag: a lit peak above its own background with a
      star-sized area (grain or a gradient passes the flat rule but reads about half the zone).
   6. ``near`` = max over zones z' at chebyshev distance d <= reach + 1 of solid(z') * k(d), k(d) = clamp((reach + 1 -
      d) / 2, 0, 1): a TAPERED protection field (reach 2: d <= 1 -> 1, d = 2 -> 0.5, d = 3 -> 0; reach 0: only the zone
      itself, at 0.5). Next to real content the backlight is set by that content; evening a speck there buys nothing.
   7. ``w0`` = sparse * strength;  ``w`` = w0 * (1 - ss(nb_lo, nb_hi, near)).  ``flank``: the zone's brightest pixel
      lies within FLANK_PX (2) px of the edge / corner it shares with a neighbour z', z' has a LARGER peak, and z''s
      brightest pixel lies within FLANK_NEAR_PX (12) px of that same edge (and, along the edge, within FLANK_NEAR_PX of
      this zone's) — the two maxima are ONE feature straddling the border: the spill of a bright star is not an
      independent dim star.  ``wt`` = the zone's weight in the target average: 0 for a flank zone; w / (1 + n) when n
      neighbours fulfil the same geometry with an EQUAL peak (a flat-topped feature straddling the border: its zones
      share one vote); else w. (A flank zone keeps w / w0: its pixels are still acted on, with the shared target.)
   8. ``target`` = max(exp(mean + target_sigma * std) * target_gain, keep_nits), min cap_nits when > 0, min white
      (keep_nits = an ABSOLUTE floor: hardware says a field of 100-nit specks makes 0.02-0.03 nits of haze — nothing to
      fix below ~100 nits, so a field whose every peak is below keep_nits is left bit-identical);  mean = S(wt ln
      peak) / S(wt), std = sqrt(S(wt (ln peak - mean)^2) / S(wt)) — the weighted mean and SPREAD of ln peak;  S = the
      TAPERED sum over box(even_reach) with zone weight (E + 1 - d) / (E + 1) (a star entering or leaving the window
      changes the target continuously); the zone's own peak where S(wt) = 0. A real star field is a heavy-tailed
      population of faint specks (a film frame: median 2 nits, p99 74, max 175): its bare geometric mean would flatten
      the stars that matter — keep_nits is what spares it (default); target_sigma > 0 is the tunable alternative: mean +
      1 std leaves the body of the field alone and COMPRESSES the outliers above it (even < 1 keeps their order). (The variance is summed about the mean, not as S(wt ln^2) / S - mean^2:
      in float32 the latter loses a uniform field's exact 0.)
   9. the fields the pixels read:
        ``w0_field`` = w0 of a speck zone; a zone WITHOUT a speck (empty, star-free (grainy) sky, a non-star shape)
                       carries the mean w0 of the speck zones in its 3x3 neighbourhood x (1 - ss(nb_lo, nb_hi, its own
                       solid)) — so a speck near the border of a star-free zone keeps its weight (the carry only
                       serves the neighbours' border pixels: see the own-zone gate), a window / UI zone carries 0;
        ``ln_t`` = ln target (ln white where the target is 0);  ``ln_g`` = lift * (ln_t - ln peak) for a speck zone
        below its target, else 0;  ``ln_pk`` = ln peak of a speck zone, ln_t otherwise;  ``ln_b`` = ln max(b, 1e-12);
        ``near`` (item 6);  ``spk`` (item 5).

Per pixel, m = its brightest channel. Bilinear between zone CENTRES, clamped (the border zones' values are held outside
the outermost centres): w0_field, ln_t, ln_g, ln_pk, ln_b, near. NEAREST (the pixel's own zone): spk.
      w_px     = w0_px * (1 - ss(nb_lo, nb_hi, near_px))      protection interpolated on its own: a star drifting
                                                              away from solid content gains weight continuously
      is_speck = ss(0.25, 0.5, (m - b_px) / (pk_px - b_px)), 0 where pk_px <= b_px       (background-relative band)
      pull     : g = ss(GATE_LO, GATE_HI, t_px / b_px);  T_floor = b_px + SPECK_LO * max(pk_px - b_px, 0);
                 T' = exp(lerp(ln max(t_px, T_floor), ln t_px, g))   the pull threshold: the target while it is well
                 above the background (the whole profile of a soft star comes down together), rising to the bottom
                 of the speck band as the target nears / undercuts the background (the sky is never reached)
      m > T'   : mc = min(m, white)  (the level the panel SHOWS: a 10 000-nit PQ request and a panel-white one balance alike)
                 out = max( exp(ln mc + w_px * even * (ln T' - ln mc)),  min(b_px, m) )   MONOTONE in m (no gate inside
                 the exponent: out(T') = T', slope 1 - w_px even >= 0 above it, flat above white)
                 acts when out < m * (1 - PULL_EPS)   (the floor is exp(ln b): equal to b only to rounding)
      m <= t_px: out = min(m * exp(ln_g_px * w_px * is_speck), max(t_px, m));  acts when that factor > 1
      t_px < m <= T': untouched
      a pixel is acted on only when w_px > 0, m > 0 AND its OWN zone is a speck zone (own-zone gate: a non-star shape in
      a carrying zone is never touched); then all three channels x out / m (hue-preserving). Every other pixel is
      returned BIT-identical (scale exactly 1).
Consequences: a field whose peaks all lie below keep_nits is bit-identical; with the target below / near the sky a star
stops at the bottom of its speck band b + 0.25 (pk - b) — cap_nits below that level is not reached, by design (the sky
is never dug into); T' then follows the INTERPOLATED zone peak and may tilt by ~10 % across a wide star.
Why target_sigma defaults to 0 (owner's real clips, 2026-09-19, star-zone LED drive change as the haze proxy): with mean +
1 std / even 0.6 the FALD stress clips lose the win the owner liked (Christmas lights -4 / -0.1 / -1 %, Chroma Galaxies
-6 / -0.1 %: their specks mostly sit AT the clip level, so mean + sigma lands near the top), while the geometric mean at
even 0.8 keeps it (-42 / -36 / -18 % and -39 / -13 %). keep_nits 100 is what protects a dim star field — Gravity is left
alone with it (-0.4 %, 4 stars touched, 175 -> 165 nits) — so the spread term is a tunable, not a default.

Parameters (``StarfieldParams`` defaults = C++ ``FaldStarfieldSettings`` = the mock's): even 0.8, lift 0, target_gain 1,
target_sigma 0, keep_nits 100, even_reach 8, cap_nits 0, area_lo 40, area_hi 160, peak_hi 0, reach 2, nb_lo 0.15, nb_hi 0.30,
strength 1.
Constants: speck band 0.25 .. 0.5, FLAT_ABS 1e-6, FLAT_REL 0.02, GATE_LO / GATE_HI 1 / 2, PULL_EPS 1e-5, FLANK_PX 2,
FLANK_NEAR_PX 12, floors 1e-12.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model import FaldModel


@dataclass(frozen=True)
class StarfieldParams:
    even: float = 0.8                # how far a peak above the local target is pulled onto it in the log domain (0 = off,
                                     # 1 = equalised; < 1 COMPRESSES and keeps the order of the stars)
    lift: float = 0.0                # how far a speck below the target is lifted (0 = cap-only; lifting raises the haze)
    target_gain: float = 1.0         # the target relative to exp(mean + target_sigma std) (< 1 = a calmer field)
    target_sigma: float = 0.0        # the target sits this many standard deviations of ln peak above the local mean
                                     # (0 = the geometric mean, the default; a tunable, range 0..4 — see "Why" above)
    keep_nits: float = 100.0         # the target never falls below this (as-if-white nits; 0 = no floor): specks up to
                                     # ~100 nits make no visible haze (HW: 0.02-0.03 nits for a 100-nit speck in EVERY zone)
    even_reach: int = 8              # zones (each side) the local target looks at, tapered (offline, even 1 / sigma 0: 3
                                     # leaves the target noisy at ~10 % star occupancy; 8 cut the predicted veil spread
                                     # 25 % cap-only, 38 % with lift)
    cap_nits: float = 0.0            # absolute ceiling of the target (as-if-white nits); 0 = none
    area_lo: float = 40.0            # px² effective lit area above the background: fully star-like at / below this ...
    area_hi: float = 160.0           # ... not at all at / above this (a 12x12-px highlight); also the speck-zone limit
    peak_hi: float = 0.0             # > 0: zones whose peak exceeds this are left alone (gone by 2x); 0 = no limit
    reach: int = 2                   # zones (each side) of full protection around solid content; the taper adds one more
    nb_lo: float = 0.15              # solid drive (tapered field / a carrying zone's own): full effect at / below this
    nb_hi: float = 0.30              # (the dim floor of a non-black field is ~0.1) ... none at / above this
    strength: float = 1.0            # overall blend 0..1 (the live knob)


FLAT_ABS = 1e-6               # a zone whose peak is not more than max(FLAT_ABS, FLAT_REL * peak) above its darkest pixel is
FLAT_REL = 0.02               # flat: no speck (HLSL FALD_STAR_FLAT_ABS / _REL, gpuemu STAR_FLAT_ABS / _REL)
SPECK_LO, SPECK_HI = 0.25, 0.5   # speck pixels: this far from the background toward the zone peak (FALD_STAR_SPECK_LO / _HI)
FLANK_PX = 2                  # a zone's brightest pixel this close to a shared edge ... (HLSL FALD_STAR_FLANK_PX)
FLANK_NEAR_PX = 12            # ... with the brighter neighbour's maximum this close behind it = one straddling feature
GATE_LO, GATE_HI = 1.0, 2.0   # pull threshold over target / background: the bottom of the speck band at / below LO, the
                              # target itself from HI on (HLSL FALD_STAR_GATE_LO / _HI, gpuemu STAR_GATE_LO / _HI)
PULL_EPS = 1e-5               # a pull ending within this (relative) of the pixel itself is no pull (FALD_STAR_PULL_EPS)


def _smoothstep(lo: float, hi: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - lo) / max(hi - lo, 1e-12), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _box(a: np.ndarray, r: int, fn) -> np.ndarray:
    """``fn`` (np.max / np.sum) over the (2r+1)² zone neighbourhood, zero-padded."""
    if r <= 0:
        return a.copy()
    rows, cols = a.shape
    pad = np.pad(a, r, mode="constant")
    return fn([pad[r + dy: r + dy + rows, r + dx: r + dx + cols] for dy in range(-r, r + 1) for dx in range(-r, r + 1)], axis=0)


def near_tapered(solid: np.ndarray, reach: int) -> np.ndarray:
    """The tapered protection field: max over chebyshev distance d <= reach + 1 of solid x clamp((reach + 1 - d) / 2, 0, 1).
    (k falls with d, so the ring maximum equals the running maximum of k(d) x box-max(d).)"""
    out = np.zeros_like(solid, dtype=float)
    for d in range(int(reach) + 2):
        k = min(max((int(reach) + 1 - d) / 2.0, 0.0), 1.0)
        if k > 0.0:
            out = np.maximum(out, k * _box(solid, d, np.max))
    return out


def box_tapered_sum(a: np.ndarray, r: int) -> np.ndarray:
    """Sum over the (2r+1)² zone neighbourhood with weight (r + 1 - d) / (r + 1), d = chebyshev distance; zero-padded."""
    rows, cols = a.shape
    pad = np.pad(a, r, mode="constant")
    out = np.zeros_like(a, dtype=float)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out += pad[r + dy: r + dy + rows, r + dx: r + dx + cols] * ((r + 1 - max(abs(dx), abs(dy))) / (r + 1.0))
    return out


def edge_key(lx: np.ndarray, ly: np.ndarray, cw: int, ch: int) -> np.ndarray:
    """Tie-break of the brightest-pixel position: how near the zone border (larger = nearer), integer."""
    return np.maximum(np.abs(2 * lx - (cw - 1)) * ch, np.abs(2 * ly - (ch - 1)) * cw)


def zone_argmax(model: FaldModel, img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Zone-local FULL-RESOLUTION position (lx, ly) of each zone's brightest pixel. A raster pixel stands for scale x
    scale equal full-resolution pixels: the rule's tie-break (nearest the zone border, then row-major first) picks one."""
    p = model.p
    k = int(p.scale)
    s = np.minimum(np.max(img, axis=0), p.white_nits)
    blocks = s.reshape(p.rows, model.ch, p.cols, model.cw).transpose(0, 2, 1, 3).reshape(p.rows, p.cols, -1)
    cw, ch = model.cw * k, model.ch * k
    ry, rx = np.divmod(np.arange(model.ch * model.cw), model.cw)
    # the full-resolution pixel of each raster pixel the tie-break would choose (largest key, then row-major first)
    sub_y, sub_x = np.divmod(np.arange(k * k), k)
    fx = rx[:, None] * k + sub_x[None, :]; fy = ry[:, None] * k + sub_y[None, :]
    order_sub = edge_key(fx, fy, cw, ch).astype(np.int64) * (cw * ch) + (cw * ch - 1 - (fy * cw + fx))   # larger = preferred
    best = np.argmax(order_sub, axis=1)
    idx = np.arange(rx.size)
    bx, by, order = fx[idx, best], fy[idx, best], order_sub[idx, best]
    is_max = blocks >= blocks.max(axis=2, keepdims=True)
    pick = np.argmax(np.where(is_max, order[None, None, :], -1), axis=2)
    return bx[pick], by[pick]


def flank_zones(peak: np.ndarray, lx: np.ndarray, ly: np.ndarray, cw: int, ch: int) -> tuple[np.ndarray, np.ndarray]:
    """(flank flag, number of equal-peak partners) of the module docstring's item 7 (cw, ch = the zone size in
    full-resolution px)."""
    rows, cols = peak.shape
    flank = np.zeros((rows, cols), dtype=bool)
    equal = np.zeros((rows, cols), dtype=int)
    pad = lambda a, fill: np.pad(a, 1, mode="constant", constant_values=fill)
    pp, px, py = pad(peak, 0.0), pad(lx, 0), pad(ly, 0)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            npk = pp[1 + dy: 1 + dy + rows, 1 + dx: 1 + dx + cols]
            nx = px[1 + dy: 1 + dy + rows, 1 + dx: 1 + dx + cols]
            ny = py[1 + dy: 1 + dy + rows, 1 + dx: 1 + dx + cols]
            ok = (npk >= peak) & (npk > 0.0)             # the geometry below, for a LARGER (flank) or an EQUAL peak (sharing)
            if dx == 1:
                ok &= (lx >= cw - FLANK_PX) & (nx < FLANK_NEAR_PX)
            elif dx == -1:
                ok &= (lx < FLANK_PX) & (nx >= cw - FLANK_NEAR_PX)
            else:
                ok &= np.abs(lx - nx) <= FLANK_NEAR_PX
            if dy == 1:
                ok &= (ly >= ch - FLANK_PX) & (ny < FLANK_NEAR_PX)
            elif dy == -1:
                ok &= (ly < FLANK_PX) & (ny >= ch - FLANK_NEAR_PX)
            else:
                ok &= np.abs(ly - ny) <= FLANK_NEAR_PX
            flank |= ok & (npk > peak)
            equal += (ok & (npk == peak)).astype(int)
    return flank, equal


def zone_levels(model: FaldModel, img: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """(peak, background = darkest pixel, sum, pixel count) per zone over ALL its pixels — NO drive-floor gate —
    of the brightest channel clipped to white; sum in nits·px² and count in px² of FULL-resolution pixels."""
    p = model.p
    s = np.minimum(np.max(img, axis=0), p.white_nits)
    blocks = s.reshape(p.rows, model.ch, p.cols, model.cw).transpose(0, 2, 1, 3)
    k = float(p.scale ** 2)
    return blocks.max(axis=(2, 3)), blocks.min(axis=(2, 3)), blocks.sum(axis=(2, 3)) * k, float(model.ch * model.cw) * k


def zone_stats(model: FaldModel, img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(peak, lit sum) per zone exactly as the layer's statistic pass has them: brightest channel clipped to white,
    pixels above the drive floor only; the sum in nits·px² of FULL-resolution pixels."""
    p = model.p
    s = np.minimum(np.max(img, axis=0), p.white_nits)
    blocks = s.reshape(p.rows, model.ch, p.cols, model.cw).transpose(0, 2, 1, 3)
    lit = blocks > p.drive_floor_nits
    peak = (blocks * lit).max(axis=(2, 3))
    total = (blocks * lit).sum(axis=(2, 3)) * float(p.scale ** 2)
    return peak, total


def zone_plan(model: FaldModel, img: np.ndarray, sp: StarfieldParams) -> dict:
    """The per-zone quantities of the module docstring, items 1-8 (+ ``new_peak``, evidence: where the zone's peak lands)."""
    p = model.p
    peak, total = zone_stats(model, img)
    peak_all, b, sum_all, n = zone_levels(model, img)
    has = peak > 0.0
    span = peak_all - b
    speck = span > np.maximum(FLAT_ABS, FLAT_REL * peak_all)
    a_eff = np.where(speck, (sum_all - b * n) / np.maximum(span, 1e-12), 0.0)
    sparse = np.where(has & speck, 1.0 - _smoothstep(sp.area_lo, sp.area_hi, a_eff), 0.0)
    if sp.peak_hi > 0.0:
        sparse = sparse * (1.0 - _smoothstep(sp.peak_hi, 2.0 * sp.peak_hi, peak))
    drive = model.drive_of(np.minimum(peak, total / p.stat_area0_px2))
    solid = (1.0 - sparse) * drive * has
    spk = has & speck & (a_eff < sp.area_hi)
    near = near_tapered(solid, int(sp.reach))
    w0 = sparse * float(np.clip(sp.strength, 0.0, 1.0))
    w = w0 * (1.0 - _smoothstep(sp.nb_lo, sp.nb_hi, near))
    lx, ly = zone_argmax(model, img)
    flank, equal = flank_zones(peak, lx, ly, model.cw * int(p.scale), model.ch * int(p.scale))
    wt = np.where(flank, 0.0, w / (1.0 + equal))         # a flank zone is no independent star; equal partners share one vote
    lp = np.log(np.maximum(peak, 1e-12))
    wsum = box_tapered_sum(wt, int(sp.even_reach))
    safe_sum = np.where(wsum > 0.0, wsum, 1.0)
    mean = box_tapered_sum(wt * lp, int(sp.even_reach)) / safe_sum
    # S(wt (lp - mean)^2) = S(wt lp^2) - 2 mean S(wt lp) + mean^2 S(wt): exact enough in float64 (the GPU sums the centred form)
    var = np.maximum(box_tapered_sum(wt * lp * lp, int(sp.even_reach)) / safe_sum - mean * mean, 0.0)
    std = np.sqrt(var)
    target = np.exp(mean + float(np.clip(sp.target_sigma, 0.0, 4.0)) * std) * float(sp.target_gain)
    target = np.maximum(target, float(max(sp.keep_nits, 0.0)))     # the absolute floor (before the cap and the white clamp)
    if sp.cap_nits > 0.0:
        target = np.minimum(target, float(sp.cap_nits))
    target = np.minimum(target, p.white_nits)
    target = np.where(wsum > 0.0, target, peak)
    lt = np.log(np.maximum(target, 1e-12))
    pull = np.where(lp > lt, float(np.clip(sp.even, 0.0, 1.0)), float(np.clip(sp.lift, 0.0, 1.0)))
    new_peak = np.where(has, np.exp(lp + w * pull * (lt - lp)), 0.0)
    return {"peak": peak, "a_eff": a_eff, "sparse": sparse, "near": near, "w0": w0, "w": w, "target": target,
            "new_peak": new_peak, "b": b, "solid": solid, "spk": spk, "flank": flank, "wt": wt, "arg": (lx, ly),
            "ln_mean": mean, "ln_std": std}


def _bilinear_zones(model: FaldModel, z: np.ndarray) -> np.ndarray:
    """A per-zone field interpolated bilinearly between zone CENTRES on the model's pixel grid (clamped at the border)."""
    p = model.p
    ys = (np.arange(model.h) + 0.5) / model.ch - 0.5
    xs = (np.arange(model.w) + 0.5) / model.cw - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, p.rows - 1); y1 = np.clip(y0 + 1, 0, p.rows - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, p.cols - 1); x1 = np.clip(x0 + 1, 0, p.cols - 1)
    fy = np.where((ys < 0) | (ys > p.rows - 1), 0.0, ys - np.floor(ys))[:, None]
    fx = np.where((xs < 0) | (xs > p.cols - 1), 0.0, xs - np.floor(xs))[None, :]
    top = z[np.ix_(y0, x0)] * (1 - fx) + z[np.ix_(y0, x1)] * fx
    bot = z[np.ix_(y1, x0)] * (1 - fx) + z[np.ix_(y1, x1)] * fx
    return top * (1 - fy) + bot * fy


def _nearest_zones(model: FaldModel, z: np.ndarray) -> np.ndarray:
    """A per-zone field expanded to the pixels of each zone (no interpolation)."""
    return np.repeat(np.repeat(z, model.ch, axis=0), model.cw, axis=1)


def pixel_weight(model: FaldModel, w_zone: np.ndarray) -> np.ndarray:
    return _bilinear_zones(model, w_zone)


def pixel_rule(m, w_px, t_px, b_px, pk_px, ln_g_px, even: float, white: float = np.inf) -> dict:
    """The per-pixel formula of the module docstring for brightest-channel levels ``m`` (arrays broadcast): ``out`` =
    the new level, ``acts`` = the pixel is changed at all (without the own-zone gate), + the intermediate quantities.
    out(m) is NON-DECREASING in m for fixed fields: m up to the pull threshold T', m^(1 - a) T'^a above it."""
    m = np.asarray(m, dtype=float)
    safe = np.maximum(m, 1e-12)
    span_px = pk_px - b_px
    is_speck = np.where(span_px > 0.0, _smoothstep(SPECK_LO, SPECK_HI, (safe - b_px) / np.maximum(span_px, 1e-12)), 0.0)
    g_px = np.exp(ln_g_px * w_px * is_speck)
    # the pull threshold T': the target while it is well above the background, the bottom of the speck band as it nears /
    # undercuts it — the sky is never reached
    gate = _smoothstep(GATE_LO, GATE_HI, t_px / b_px)
    t_floor = b_px + SPECK_LO * np.maximum(span_px, 0.0)
    ln_floor = np.log(np.maximum(t_px, t_floor))
    ln_tp = ln_floor + gate * (np.log(t_px) - ln_floor)
    tp_px = np.exp(ln_tp)
    shown = np.minimum(safe, white)                        # the pull starts from what the panel SHOWS (it clips at white)
    pulled = np.exp(np.log(shown) + w_px * even * (ln_tp - np.log(shown)))
    pulled = np.maximum(pulled, np.minimum(b_px, safe))    # never below the background, never above the pixel itself
    lifted = np.minimum(safe * g_px, np.maximum(t_px, safe))
    out_m = np.where(m > tp_px, pulled, np.where(m <= t_px, lifted, safe))
    acts = (w_px > 0.0) & (m > 0.0) & np.where(m > tp_px, out_m < safe * (1.0 - PULL_EPS), (m <= t_px) & (g_px > 1.0))
    return {"out": out_m, "acts": acts, "is_speck": is_speck, "gate": gate, "pull_threshold": tp_px + 0.0 * m}


def balance_image(model: FaldModel, img: np.ndarray, sp: StarfieldParams) -> dict:
    """The balanced request image (``img``: (3, h, w) as-if-white nits) + the evidence: the zone fields of the module
    docstring's item 9 and the per-pixel formula below it."""
    z = zone_plan(model, img, sp)
    spk = z["spk"]
    n_spk = _box(spk.astype(float), 1, np.sum)
    carry = _box(z["w0"], 1, np.sum) / np.maximum(n_spk, 1.0) * (1.0 - _smoothstep(sp.nb_lo, sp.nb_hi, z["solid"]))
    w0_field = np.where(spk, z["w0"], carry)               # (w0 > 0 only in speck zones, so the box sum is theirs)
    ln_t = np.log(np.maximum(np.where(z["target"] > 0.0, z["target"], model.p.white_nits), 1e-12))
    below = spk & (z["peak"] < z["target"])
    ln_g = np.where(below, float(np.clip(sp.lift, 0.0, 1.0)) * (ln_t - np.log(np.maximum(z["peak"], 1e-12))), 0.0)
    ln_pk = np.where(spk, np.log(np.maximum(z["peak"], 1e-12)), ln_t)
    # per pixel
    near_px = _bilinear_zones(model, z["near"])
    w_px = _bilinear_zones(model, w0_field) * (1.0 - _smoothstep(sp.nb_lo, sp.nb_hi, near_px))
    t_px = np.exp(_bilinear_zones(model, ln_t))
    b_px = np.exp(_bilinear_zones(model, np.log(np.maximum(z["b"], 1e-12))))          # >= 1e-12: a black sky gates fully open
    pk_px = np.exp(_bilinear_zones(model, ln_pk))
    m = np.max(img, axis=0)
    safe = np.maximum(m, 1e-12)
    r = pixel_rule(m, w_px, t_px, b_px, pk_px, _bilinear_zones(model, ln_g), float(np.clip(sp.even, 0.0, 1.0)),
                   float(model.p.white_nits))
    out_m, is_speck, gate, tp_px = r["out"], r["is_speck"], r["gate"], r["pull_threshold"]
    own = _nearest_zones(model, spk)                       # the own-zone gate
    acts = own & r["acts"]
    scale = np.where(acts, out_m / safe, 1.0)              # exactly 1 wherever nothing acts: BIT-identical content
    return {"img": img * scale[None], "scale": scale, "w_pixel": w_px, "target_pixel": t_px, "w_field": w0_field,
            "near_pixel": near_px, "b_pixel": b_px, "peak_pixel": pk_px, "is_speck": is_speck, "gate": gate,
            "pull_threshold_pixel": tp_px, **z}


def predict(model: FaldModel, img: np.ndarray, sp: StarfieldParams) -> dict:
    """What the model says the panel shows without / with the balancing: luminance fields, the zone drives and their
    spread over the zones that carry star-like content (the unevenness the feature is after)."""
    out = balance_image(model, img, sp)
    y0 = model.forward_img(img)
    y1 = model.forward_img(out["img"])
    sel = out["sparse"] > 0.5

    def spread(d):
        v = d[sel]
        return float(v.std() / max(v.mean(), 1e-12)) if v.size else 0.0

    dark = np.max(img, axis=0) <= np.maximum(0.0, np.percentile(np.max(img, axis=0), 50))
    lum0, lum1 = y0["y"].sum(axis=0), y1["y"].sum(axis=0)
    return {"balanced": out, "y_off": lum0, "y_on": lum1, "drives_off": y0["drives"], "drives_on": y1["drives"],
            "drive_spread_off": spread(y0["drives"]), "drive_spread_on": spread(y1["drives"]),
            "veil_off": float(lum0[dark].mean()), "veil_on": float(lum1[dark].mean()),
            "veil_std_off": float(lum0[dark].std()), "veil_std_on": float(lum1[dark].std())}
