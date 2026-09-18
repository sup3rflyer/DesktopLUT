"""Starfield balancing — EXPERIMENT (work guide ticket S1, owner 2026-09-18): EVEN OUT the backlight over a field of
scattered tiny highlights. It changes the content on purpose; the question is how imperceptible it can be made.

Why (HW 2026-09-18, work guide "HW 2026-09-18" item 2h). On a mini-LED panel a sparse sub-zone highlight is shown with
the LCD wide open, so its luminance AND the haze around it are both set by the LED drive of its zone — and that drive
follows the highlight's REQUESTED level (~request^0.6), hardly its size: one white pixel drives its zone to ~12 %.
Real content rarely sits on a code-0 black, so the zones of such a field are lit anyway; what shows is the UNEVENNESS —
the zone holding a brighter speck drives harder than its neighbours, a zone-shaped patch of haze that comes and goes
as the specks move. Pixel-side compensation cannot remove it near black (nothing can go below the pedestal). The one
lever is the requested level of the specks themselves: equal peaks -> equal drives -> an even veil instead of patches.

The rule, per zone (the layer's statistic pass already has the brightest lit pixel ``m`` and the lit sum ``S``):
  * star-like   : effective lit area ``a_eff = S / m`` (px² at the peak level) small (``area_lo`` .. ``area_hi``), and
                  optionally peak below ``peak_hi`` (leave real specular highlights alone);
  * unprotected : no SOLID content within ``reach`` zones (the non-sparse drive there below ``nb_lo`` .. ``nb_hi``) —
                  next to real content the backlight is set by that content and evening a speck buys nothing;
  * target      : the local GEOMETRIC MEAN of the star-like peaks within ``even_reach`` zones (weighted by the zone
                  weights), times ``target_gain``; optionally under the absolute ceiling ``cap_nits`` (0 = none);
  * action      : peaks above the target are pulled toward it by ``even`` (1 = fully equalised); peaks below it are
                  lifted by ``lift`` (default 0: cap-only — lifting raises the total haze).
Per pixel, hue-preserving (one scale for the three channels): pixels above the zone's new peak are compressed onto it,
pixels below it are untouched; with ``lift`` the pixels near the zone's peak (the specks, not their sky) get the
zone's lift factor. Zone quantities
are interpolated BILINEARLY between zone centres before use, so a speck drifting toward real content, or from a bright
neighbourhood into a dim one, changes smoothly instead of popping at a zone border.

This module is the REFERENCE (numpy, the model's reduced-resolution image domain, as-if-white nits). The HLSL port must
match it in a dump-compare like every other pass of the layer.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model import FaldModel


@dataclass(frozen=True)
class StarfieldParams:
    even: float = 1.0                # how far a peak above the local target is pulled onto it (0 = off, 1 = equalised)
    lift: float = 0.0                # how far a peak below the target is lifted (0 = cap-only)
    target_gain: float = 1.0         # the target relative to the local geometric mean (< 1 = a calmer field)
    even_reach: int = 8              # zones (each side) the local mean looks at (offline: 3 leaves the target noisy at
                                     # ~10 % star occupancy; 8 cuts the predicted veil spread 25 % cap-only, 38 % with lift)
    cap_nits: float = 0.0            # absolute ceiling of qualifying content (as-if-white nits); 0 = none
    area_lo: float = 40.0            # px² effective lit area: fully star-like at / below this ...
    area_hi: float = 160.0           # ... not at all at / above this (a 12x12-px highlight)
    peak_hi: float = 0.0             # > 0: zones whose peak exceeds this are left alone; 0 = no limit
    reach: int = 2                   # zones (each side) searched for solid content
    nb_lo: float = 0.15              # non-sparse neighbour drive: full effect at / below this (the dim floor of a
    nb_hi: float = 0.30              # non-black field is ~0.1) ... none at / above this
    strength: float = 1.0            # overall blend 0..1 (the live knob)


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
    """Per-zone quantities: ``sparse`` (star-likeness), ``near`` (max solid drive within ``reach``), ``w`` (the zone's
    weight), ``target`` (local geometric mean of the weighted peaks x target_gain, under cap_nits) and ``new_peak``."""
    p = model.p
    peak, total = zone_stats(model, img)
    has = peak > 0.0
    a_eff = np.where(has, total / np.maximum(peak, 1e-12), 0.0)
    sparse = np.where(has, 1.0 - _smoothstep(sp.area_lo, sp.area_hi, a_eff), 0.0)
    if sp.peak_hi > 0.0:
        sparse = sparse * (1.0 - _smoothstep(sp.peak_hi, 2.0 * sp.peak_hi, peak))
    drive = model.drive_of(np.minimum(peak, total / p.stat_area0_px2))
    solid = (1.0 - sparse) * drive * has
    near = _box(solid, int(sp.reach), np.max)
    w = sparse * (1.0 - _smoothstep(sp.nb_lo, sp.nb_hi, near)) * float(np.clip(sp.strength, 0.0, 1.0))
    lp = np.log(np.maximum(peak, 1e-12))
    wsum = _box(w, int(sp.even_reach), np.sum)
    target = np.exp(_box(w * lp, int(sp.even_reach), np.sum) / np.maximum(wsum, 1e-12)) * float(sp.target_gain)
    if sp.cap_nits > 0.0:
        target = np.minimum(target, float(sp.cap_nits))
    target = np.where(wsum > 0.0, target, peak)
    lt = np.log(np.maximum(target, 1e-12))
    pull = np.where(lp > lt, float(np.clip(sp.even, 0.0, 1.0)), float(np.clip(sp.lift, 0.0, 1.0)))
    new_peak = np.where(has, np.exp(lp + w * pull * (lt - lp)), 0.0)
    return {"peak": peak, "a_eff": a_eff, "sparse": sparse, "near": near, "w": w, "target": target, "new_peak": new_peak}


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


def pixel_weight(model: FaldModel, w_zone: np.ndarray) -> np.ndarray:
    return _bilinear_zones(model, w_zone)


def balance_image(model: FaldModel, img: np.ndarray, sp: StarfieldParams) -> dict:
    """The balanced request image (``img``: (3, h, w) as-if-white nits) + the evidence. Three zone fields go to the
    pixels bilinearly: the weight, ln(target) and ln(lift factor). A zone WITHOUT content inside a star field carries
    its neighbours' mean weight and the local target, so a speck near the border of an empty zone keeps its full
    treatment; a protected zone carries weight 0, so nothing of it changes whatever target sits next to it.
    Per pixel: above the target -> pulled toward it by weight x even (log domain); every lit pixel x lift^weight."""
    z = zone_plan(model, img, sp)
    has = z["peak"] > 0.0
    n_has = _box(has.astype(float), 1, np.sum)
    w_field = np.where(has, z["w"], _box(z["w"], 1, np.sum) / np.maximum(n_has, 1.0))
    ln_t = np.log(np.maximum(np.where(z["target"] > 0.0, z["target"], model.p.white_nits), 1e-12))
    below = has & (z["peak"] < z["target"])
    ln_g = np.where(below, float(np.clip(sp.lift, 0.0, 1.0)) * (ln_t - np.log(np.maximum(z["peak"], 1e-12))), 0.0)
    w_px = _bilinear_zones(model, w_field)
    t_px = np.exp(_bilinear_zones(model, ln_t))
    m = np.max(img, axis=0)
    safe = np.maximum(m, 1e-12)
    # the lift belongs to the SPECKS, not to the sky they sit on: only pixels near their zone's peak take it
    # (smooth from 25 % to 50 % of the interpolated zone peak; an empty zone carries the local target as its peak)
    pk_px = np.exp(_bilinear_zones(model, np.where(has, np.log(np.maximum(z["peak"], 1e-12)), ln_t)))
    is_speck = _smoothstep(0.25, 0.5, safe / np.maximum(pk_px, 1e-12))
    g_px = np.exp(_bilinear_zones(model, ln_g) * w_px * is_speck)
    pulled = np.exp(np.log(safe) + w_px * float(np.clip(sp.even, 0.0, 1.0)) * (np.log(t_px) - np.log(safe)))
    out_m = np.where(m > t_px, pulled, np.minimum(safe * g_px, np.maximum(t_px, safe)))
    # exactly 1 wherever nothing acts (weight 0, or a pixel at / below the target with no lift): untouched content
    # must be BIT-identical, not exp(log(x))
    acts = (w_px > 0.0) & (m > 0.0) & ((m > t_px) | (g_px > 1.0))
    scale = np.where(acts, out_m / safe, 1.0)
    return {"img": img * scale[None], "scale": scale, "w_pixel": w_px, "target_pixel": t_px, "w_field": w_field, **z}


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
