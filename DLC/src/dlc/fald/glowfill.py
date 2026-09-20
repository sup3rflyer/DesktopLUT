"""Glow fill — EXPERIMENT (work guide ticket S2, owner 2026-09-20): a CALCULATED black lift that makes the LED glow on
dark content EVEN instead of splotchy. Default off.

Why. On dark content the panel shows its pedestal ``V(x) = white · B_true(x) · tmin`` — the closed-LCD leak of whatever
the LEDs do. Over a field of scattered highlights that glow is uneven at the scale of a few zones: lit zones glow, the
empty zones between them do not. Nothing can be subtracted from a pedestal; the one lever left is to ADD LCD light where
the glow is weaker than around it. The amount is derived from the panel model (the same ``B_true`` the correction uses,
so the black-frame boost, starfield balancing and the temporal drive state are already in it), not from the picture.

This module is the REFERENCE (numpy, the model's scale-5 raster, as-if-white nits). Twins that must stay in lockstep:
``dlc/fald/gpuemu.py`` (GPU order, full resolution) and ``src/fald_shader.h`` (passes G0 - G3 + ``GlowAdd``);
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
      ``REQ_CEIL`` = min(REQ_FLOOR_FRAC · drive_floor, REQ_LIT_FRAC · boost_lit_nits [with a boost table]) — the fill can
      never light a LED or make a zone LIT for the firmware's count.
Within the model the panel then shows ``V + shown + fill`` at the pixel: on black the LCD adds exactly ``want · trust``
(x min(1, gain_max · B_true / B_est)), never more than ``want``; the zone mean of the glow never exceeds ``Ez``.

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

Parameters (``GlowFillParams`` defaults = C++ ``FaldGlowSettings`` = the mock's): strength 1 (0..1), reach 2 (1..4 zones),
cap_nits 0.10 (0.005..0.5).
Constants: GLOW_SIGMA_BASE 0.5, GLOW_SIGMA_PER_REACH 0.5, DEFICIT_REL_LO 0.05, DEFICIT_REL_HI 0.15, WANT_EPS 1e-5,
REQ_FLOOR_FRAC 0.6, REQ_LIT_FRAC 0.85.
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
REQ_FLOOR_FRAC = 0.6                     # HLSL FALD_GLOW_REQ_FLOOR_FRAC: a filled pixel's request stays below this x drive floor
REQ_LIT_FRAC = 0.85                      # HLSL FALD_GLOW_REQ_LIT_FRAC: ... and below this x the boost count's LIT level


@dataclass(frozen=True)
class GlowFillParams:
    strength: float = 1.0                # share of the glow deficit that is filled (0 = off, 1 = up to the envelope)
    reach: int = 2                       # zones (each side): holes / valleys up to 2·reach zones wide are filled
    cap_nits: float = 0.10               # the fill never exceeds this (as-if-white nits)
    envelope: str = "close"              # OFFLINE-ONLY switch kept for the stress tests: "close" (the rule) | "dilate" (the
                                         # 2026-09-20 spec's first form: blur of the box maximum — paints a skirt)


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


def zone_fields(model: FaldModel, d_true: np.ndarray, boost: float, gp: GlowFillParams) -> dict:
    """The zone fields of a round: ``vz``, ``ez`` and ``dz`` (the dump's fald_glow_vz.f32 / fald_glow_env.f32 [ez, dz])."""
    gp = clamp_params(gp)
    vz = zone_pedestal(model, d_true, boost)
    ez = envelope(vz, gp)
    return {"vz": vz, "ez": ez, "dz": deficit(vz, ez)}


def round_fill(model: FaldModel, req: np.ndarray, b_true: np.ndarray, b_est: np.ndarray, vz: np.ndarray, ez: np.ndarray,
               gp: GlowFillParams, gain_max: float = 4.0) -> dict:
    """Items 4-6 for every pixel. ``req`` = the round's corrected request WITHOUT fill (3, h, w); ``b_true`` / ``b_est`` =
    the round's pixel fields. Returns ``add`` (3, h, w; exactly 0 where nothing is filled), ``fill`` (the luminance the
    panel is asked to add, as-if-white nits), ``want``, ``trust``, ``e_px``, ``v_px``."""
    from .starfield import _bilinear_zones
    gp = clamp_params(gp)
    p = model.p
    e_px = _bilinear_zones(model, ez)
    v_px = p.white_nits * p.tmin * np.maximum(b_true, 0.0)
    if gp.envelope == "dilate":                                          # the spec's first form: against the pixel's own pedestal
        d_px = np.maximum(e_px - v_px, 0.0)
    else:                                                                # the rule: the ZONE-level deficit, interpolated
        d_px = _bilinear_zones(model, deficit(vz, ez))
    want = np.minimum(np.maximum(gp.strength * d_px - WANT_EPS, 0.0), gp.cap_nits)
    r = req.max(axis=0)
    shown = r * np.maximum(b_true, 0.0) / np.maximum(b_est, 1e-9)
    trust = _smoothstep(p.fade_lo, p.fade_hi, b_est)
    fill = np.maximum(want - shown, 0.0) * trust
    m = pedestal_colour(model)
    add_lum = fill * np.minimum(b_est / np.maximum(b_true, 1e-9), gain_max)
    room = np.maximum(req_ceiling(model) - r, 0.0)                       # what the brightest channel may still take
    scale = np.minimum(1.0, room / np.maximum(add_lum * m.max(), 1e-30))
    add_lum = add_lum * scale
    add = add_lum[None] * m[:, None, None]
    return {"add": np.where(add_lum[None] > 0.0, add, 0.0), "fill": fill * scale, "want": want, "trust": trust,
            "e_px": e_px, "v_px": v_px, "shown": shown}


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
