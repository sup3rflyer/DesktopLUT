"""FALD pixel-side correction — the inverse of :mod:`dlc.fald.model`.

The monitor sets each pixel's LCD transmittance from ITS OWN backlight estimate,
``T = req / (Lmax · B_est)``, while the light actually arriving is ``Lmax · B_true``. What the
viewer sees is ``Y = Lmax · B_true · T + pedestal`` with ``pedestal = Lmax · B_true · tmin``.

**What the layer targets.** Not the bare requested luminance: a panel can never show less than its
own pedestal, and the uniform-field tone curve (pedestal included) is what the 1-D calibration
already owns. The FALD layer removes the CONTEXT dependence — a pixel next to a highlight should
look exactly like the same pixel in a uniform field of its own level. So the target is
``w·img + ped_ref`` where ``ped_ref = Lmax · d_own · tmin`` is the pedestal a uniform field of that
pixel's level carries (``d_own`` = the drive curve at the pixel's own value). Then

    req = (img + ped_ref/w − ped/w) · B_est / B_true          (as-if-white nits, per channel)

followed by ONE per-pixel ceiling rule (work guide C10 + C11, 2026-09-15; replaces the per-channel clamp; C15 2026-09-22:
the ceiling's B_est is low-passed exactly like the gain):
the request is ``u · g_eff`` with ``u = img + term`` and a single scalar ``g_eff`` for all three channels,
so the correction can never rotate hue. Darkening (gain ≤ 1) applies the full gain. Brightening (gain > 1)
goes through a soft knee on the brightest channel toward the LCD ceiling ``C = white · B_est`` (the LCD
cannot open past 100 %): identity up to ``KNEE_START · C``, then a smooth roll-off that never ends below
the pixel's original value and asymptotes to ``max(original, C)`` — so the layer never brightens INTO
the (low-passed, C15) ceiling (an isolated highlight keeps whatever gradient the panel leaves it) and a saturated highlight
keeps its original request (the dimming algorithm's input is not disturbed; review 2026-09-10: feeding a
clipped value back dimmed the highlight's own cell drive and the iteration drifted 8+ rounds).
Since C15 the ceiling is the LOW-PASSED B_est, so between LED sample points the knee may request past the model's
per-pixel ``white · B_est`` — deliberate: that per-pixel structure is the fitted kernel's centre spike, which the panel
does not show (owner RAW 2026-09-22; refit = work guide P11); revisit C15 after P11.
The old per-channel rule kept the brightest channel of an over-ceiling pixel undarkened while darkening
the others: warm greys turned salmon along the left/top edges of bright content on black (owner photo,
SDR, 2026-09-14).

Everything here is in the model's reduced-resolution "as-if-white nits" image domain
(see :meth:`FaldModel.render`); :func:`corrected_code` converts a corrected linear value back to
a 10-bit PQ code for the pattern generator. Edge caveat: the kernels have no edge reflection, so
gains within ~2 cells of the panel border are not trustworthy (fine for a centre meter).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

from dlc._pq import oetf_norm

from .model import FaldModel, FaldParams


# Soft-knee constants of the ceiling rule — compile-time in the shader too (FALD_KNEE_START / FALD_KNEE_CAP_TRUST in
# src/fald_shader.h; tests/test_fald_transfer.py pins them equal). KNEE_CAP_TRUST scales how far the ceiling is
# trusted (C = min(white, white·B_est / trust)); 0 = knee toward white only (not meter-gated: brightens isolated
# highlights, pushing their LED drive).
KNEE_START = 0.9
KNEE_CAP_TRUST = 1.0


def ceiling_gain(m: np.ndarray, gain: np.ndarray, b_est: np.ndarray, white: float,
                 k: float = KNEE_START, trust: float = KNEE_CAP_TRUST) -> np.ndarray:
    """The single scale a pixel gets: ``gain`` where it darkens (gain ≤ 1), else the soft knee of the brightest
    channel ``m`` (as-if-white nits, > 0) toward the ceiling, never below 1. Continuous and monotone in m, gain and
    B_est; always between 1 and gain."""
    m = np.asarray(m, dtype=float); gain = np.asarray(gain, dtype=float)
    cap = white * np.maximum(b_est, 1e-9)
    C = np.minimum(white, cap / trust) if trust > 0 else np.full_like(cap, white)
    a = m * gain
    t = np.maximum(a / C - k, 0.0) / (1.0 - k)
    knee = np.where(a <= k * C, a, C * (k + (1.0 - k) * t / (1.0 + t)))
    return np.where(gain > 1.0, np.maximum(m, knee) / m, gain)


def load_fitted_params(path: Path) -> FaldParams:
    """Load a ``fald_fit_result.json`` (written by fald_fit.py) into :class:`FaldParams`."""
    fit = json.load(open(path))
    kw = dict(fit["params"])
    kw["drive_curve"] = [tuple(x) for x in kw["drive_curve"]]
    kw["chan_weights"] = tuple(kw["chan_weights"])
    kw["boost_lut"] = tuple((float(a), float(b)) for a, b in (kw.get("boost_lut") or ()))
    return FaldParams(**{k: v for k, v in kw.items() if k in FaldParams.__dataclass_fields__})


# The panel file carries the black-frame LED boost since FLD4 (work guide C12, 2026-09-18): dlc.fald.export writes
# the LUT of every fit that has one (or raises), LoadFaldPanelParams reads it and the HLSL applies it to B_true.
BOOST_IN_PANEL_FILE = True


def shader_model(model: FaldModel, boost_in_file: bool = True) -> FaldModel:
    """The model the running SHADER implements for ``model``'s fit. With the fit's own export (FLD4 when it has a
    boost LUT) that is ``model`` itself — the default. ``boost_in_file=False``: the layer runs a panel file WITHOUT
    the boost block (an FLD1-3 export from before C12, e.g. ``fald_profile --phase verify --bin old.bin``) while the
    fit knows the boost — the shader then corrects boost-blind and the prediction of a layer-ON read is
    ``model.meter_img(correct_image(shader_model(model, False), img)["req"], meter)``: the boost-blind correction
    seen through the boosted panel."""
    if boost_in_file or not model.p.boost_lut:
        return model
    cached = getattr(model, "_shader_model", None)
    if cached is None or cached[0] != model.p.__dict__:
        from dataclasses import replace
        cached = (dict(model.p.__dict__), FaldModel(replace(model.p, boost_lut=())))
        model._shader_model = cached
    return cached[1]


def reference_pedestal(model: FaldModel, img: np.ndarray) -> np.ndarray:
    """Per-pixel pedestal (as-if-white nits, LUMINANCE-equivalent) a UNIFORM field of the pixel's own
    level would carry: white · drive_curve(max channel) · tmin. Shape (h, w). With a coloured pedestal
    (``ped_mode == "channel"``) this is still the luminance (Σ w_c·m_c = 1); see :func:`reference_pedestal_rgb`."""
    p = model.p
    s = np.minimum(np.max(img, axis=0), p.white_nits)
    return p.white_nits * model.drive_of(s) * p.tmin


def pedestal_multipliers(model: FaldModel) -> np.ndarray:
    """m_c (3,): the pedestal colour in effect — tmin_rgb in "channel" mode, (1, 1, 1) otherwise."""
    return model.p.tmin_vec() / max(model.p.tmin, 1e-30)


def reference_pedestal_rgb(model: FaldModel, img: np.ndarray) -> np.ndarray:
    """Per-channel reference pedestal (3, h, w), as-if-white nits per channel: ref · m_c."""
    return reference_pedestal(model, img)[None] * pedestal_multipliers(model)[:, None, None]


def pedestal_adjust(delta: np.ndarray, img: np.ndarray, ped_mode: str) -> tuple[np.ndarray, np.ndarray]:
    """The pedestal term the correction adds to ``img`` (as-if-white, per channel) given the per-channel
    excess ``delta`` = ref − actual (< 0: this context leaks MORE than a uniform field → subtract).

    ``"white"``: subtract the delta vector scaled by ONE common factor so no channel goes below zero (with the
    white pedestal of this mode = the 2026-09-12 rule: the same amount from all channels, limited by the
    darkest). ``"channel"``: subtract each channel's own excess, floored at 0 per channel (the least-error
    inverse; the residual where a channel floors is the pedestal's own colour). Returns (adj, floored (h, w))."""
    if ped_mode == "channel":
        adj = np.maximum(delta, -img)
        return adj, (adj > delta + 1e-12).any(axis=0)
    if ped_mode != "white":
        raise ValueError(f"ped_mode must be 'white' or 'channel', got {ped_mode!r}")
    neg = delta < 0.0
    frac = np.where(neg, np.minimum(1.0, img / np.where(neg, -delta, 1.0)), 1.0)   # fraction each channel can take
    f = frac.min(axis=0)
    return delta * f[None], f < 1.0


def correct_image(model: FaldModel, img: np.ndarray, iters: int = 2,
                  gain_clip: tuple[float, float] = (0.25, 4.0), drive_filter=None, glow=None) -> dict:
    """Return the corrected request image for ``img`` (3, h, w, as-if-white nits).

    ``glow``: optional :class:`dlc.fald.glowfill.GlowFillParams` — the glow fill (work guide S2): every round adds the
    fill of ITS fields to the request (so round 1's drives / boost count see the frame the panel receives, fill
    included); ``None`` = the layer without it, bit for bit. Evidence of the last round under ``"glow"``.

    ``drive_filter``: optional ``drives -> (drives_true, drives_est)`` applied to every round's instantaneous
    cell drives before the kernels — the temporal drive state of :mod:`dlc.fald.temporal` (the shader's
    per-cell LED-law filter). ``None`` = the stateless layer (both fields from the frame's own drives).

    Result dict: ``req`` (corrected image), ``gain`` (B_est/B_true of the last iteration),
    ``pedestal`` (per channel, nits, from the last iteration's drives), ``clipped`` (LCD would
    need > 100 %: original request kept), ``floored`` (target below the pedestal), ``drives`` (the last
    round's INSTANTANEOUS drives — what a temporal state commits after the frame)."""
    p = model.p
    w = np.array(p.chan_weights)[:, None, None]
    lmax = p.white_nits * w
    tv = p.tmin_vec()[:, None, None]                          # per-channel closed-LCD transmittance
    ped_ref = reference_pedestal_rgb(model, img)               # as-if-white, per channel
    ped_ref_w = reference_pedestal(model, img)[None]           # the white pedestal (luminance) for the split
    tv_w = p.tmin * np.ones((3, 1, 1))
    cur = img.copy()
    gain = np.ones_like(img[0])
    for _ in range(max(1, iters)):
        drives = model.cell_drives(cur)
        # black-frame LED boost (FaldParams.boost_lut): the panel counts the non-black zones of the frame it RECEIVES —
        # this round's request. The pedestal term can floor dim pixels and move the count (review 2026-09-18: <= 1 zone
        # with the shipped lum-fade, ~200 zones on a code-20 surround without it), so it is re-read every round.
        boost = model.led_boost(cur)
        d_true, d_est = drive_filter(drives) if drive_filter is not None else (drives, drives)
        b_true, b_est = model.backlights(d_true, d_est, boost=boost)
        b_true = np.maximum(b_true, 0.0)
        gain = np.clip(b_est / np.maximum(b_true, 1e-9), gain_clip[0], gain_clip[1])
        # deep-dark fade: trust the model only where the panel's estimate is not ~zero (FaldParams.fade_*)
        t = np.clip((b_est - p.fade_lo) / max(p.fade_hi - p.fade_lo, 1e-9), 0.0, 1.0)
        wfade = t * t * (3.0 - 2.0 * t)
        wbest = wfade                                          # B_est fade alone (the colour part keeps it)
        gain = 1.0 + (gain - 1.0) * wfade
        # C15 (2026-09-22): the knee's ceiling reads B_est low-passed by the SAME filter as the gain (the shader blurs the
        # two together, gainTex .xy). The fitted estimate kernel peaks sharply at every LED sample point; a per-pixel
        # ceiling let the knee brighten a small bright shape only near the LEDs and printed the zone lattice (>= ~500
        # nits, owner photos 2026-09-22). The pedestal term's deep-dark fade keeps the per-pixel B_est.
        b_est_ceil = b_est
        if p.gain_smooth_cells > 0:
            from scipy.ndimage import gaussian_filter
            sig = (p.gain_smooth_cells * model.ch, p.gain_smooth_cells * model.cw)
            gain = gaussian_filter(gain, sigma=sig, mode="nearest")
            b_est_ceil = gaussian_filter(b_est, sigma=sig, mode="nearest")
        # pixel-luminance fade (2026-09-12, doc S33): the model has no baseline below ~1 nit (drive floor), and the
        # dark-halo probe showed the correction wrong in sign on 0.5-nit grey next to a bright stroke (the owner's
        # dark band around text). Weight 0 -> 1 over lum_fade_lo -> lum_fade_hi of the pixel's own max channel,
        # applied per pixel AFTER the gain low-pass (the gain field stays smooth; the fade follows the content).
        if p.lum_fade_hi > p.lum_fade_lo >= 0:
            tl = np.clip((img.max(axis=0) - p.lum_fade_lo) / (p.lum_fade_hi - p.lum_fade_lo), 0.0, 1.0)
            wlum = tl * tl * (3.0 - 2.0 * tl)
            gain = 1.0 + (gain - 1.0) * wlum
            wfade = wfade * wlum
        ped = lmax * b_true[None] * tv                         # per channel, nits, actual context
        # Pedestal term. delta_c = ref − actual per channel (as-if-white); in "white" mode the three are
        # identical. delta > 0 (uniform field leaks more than this context) is a plain lift and never clips;
        # delta < 0 is limited by what the pixel can take, per FaldParams.ped_mode:
        #   "white"   (2026-09-12, live A/B showed blue rims when a WHITE pedestal was clipped per channel):
        #             one common factor on the whole vector, so what cannot be removed stays as desaturation
        #             instead of a hue rotation;
        #   "channel" (2026-09-13, coloured pedestal): each channel floors independently.
        delta = ped_ref - ped / w                              # as-if-white, per channel
        adj, floored_px = pedestal_adjust(delta, img, p.ped_mode)
        if p.ped_mode == "channel" and (p.ped_chroma_gain != 1.0 or p.ped_chroma_lum_fade is not None):
            # split: white part (the "white" rule on the white pedestal) faded as before; colour part
            # (adj − adj_white, luminance-neutral) × ped_chroma_gain × its own pixel-luminance fade
            adj_w, _ = pedestal_adjust(ped_ref_w - lmax * b_true[None] * tv_w / w, img, "white")
            clo, chi = p.chroma_lum_fade()
            if chi > clo >= 0:
                tc = np.clip((img.max(axis=0) - clo) / (chi - clo), 0.0, 1.0)
                wchroma = wbest * (tc * tc * (3.0 - 2.0 * tc))
            else:
                wchroma = wbest
            term = adj_w * wfade + (adj - adj_w) * p.ped_chroma_gain * wchroma[None]
        else:
            term = adj * wfade
        u = img + term
        floored = np.broadcast_to(floored_px[None], u.shape)
        mu = u.max(axis=0)
        ok = mu > 1e-9
        g_eff = np.where(ok, ceiling_gain(np.where(ok, mu, 1.0), gain, b_est_ceil, p.white_nits), gain)
        req = np.maximum(u * g_eff[None], 0.0)                  # ONE scale per pixel: hue cannot rotate
        clipped = np.broadcast_to((g_eff < gain - 1e-12)[None], req.shape)   # brightening limited by the knee
        if glow is not None:
            from . import glowfill
            zf = glowfill.zone_fields(model, d_true, boost, glow)
            # keep the fill off the firmware's count threshold: the zone scale k, from THIS round's request and fields
            glow_band = glowfill.band_scale(model, req, b_true, b_est, zf["dz"], zf["ez"], glow, gain_max=gain_clip[1])
            glow_out = glowfill.round_fill(model, req, b_true, b_est, zf["dz"], zf["ez"], glow, gain_max=gain_clip[1], k=glow_band["k"])
            glow_out.update(zf, round_input=cur, req_nofill=req, band=glow_band)
            req = np.where(glow_out["add"] > 0.0, req + glow_out["add"], req)   # untouched pixels stay bit-identical
        cur = req
    out = {"req": cur, "gain": gain, "pedestal": ped, "clipped": clipped, "floored": floored, "drives": drives,
           "boost": boost}
    if glow is not None:
        out["glow"] = glow_out
    return out


def corrected_code(value_nits: float, bits: int = 10) -> int:
    """As-if-white linear nits → PQ code."""
    return int(round(oetf_norm(max(float(value_nits), 0.0) / 10000.0) * ((1 << bits) - 1)))


def corrected_patch_code(model: FaldModel, shapes, meter_px: tuple[float, float],
                         patch_rect: tuple[float, float, float, float],
                         aperture_px: Optional[float] = None, iters: int = 3) -> dict:
    """The single corrected code a small patch UNDER the meter should carry so the meter reads the
    intended field value despite nearby highlights (the hardware demo of the inverse).

    Iterates on the DEMO frame itself (``shapes`` + the patch at the current code), so the drives
    the correction assumes are the drives that frame produces; averages the per-pixel correction
    over the aperture disc. ±1 PQ code at 10 nits is ±1.2 % luminance — the demo's floor."""
    p = model.p
    w = np.array(p.chan_weights)[:, None, None]
    mask = model.aperture_mask(meter_px, aperture_px)
    base_img = model.render(shapes)
    field = base_img[:, mask].mean(axis=1)                     # the field under the meter (as-if-white)
    ped_ref = reference_pedestal(model, base_img)[mask].mean()
    codes = tuple(corrected_code(v) for v in field)
    gain = 1.0
    for _ in range(max(1, iters)):
        frame = model.render(list(shapes) + [(codes, patch_rect)])
        drives = model.cell_drives(frame)
        b_true, b_est = model.backlights(drives, boost=model.led_boost(frame))
        g = (b_est / np.maximum(b_true, 1e-9))[mask]
        ped = (p.white_nits * np.maximum(b_true, 0.0) * p.tmin)[mask]      # as-if-white (luminance)
        mch = pedestal_multipliers(model)[:, None]                         # pedestal colour in effect
        req = ((field[:, None] + (ped_ref - ped[None]) * mch) * g[None]).mean(axis=1)
        gain = float(g.mean())
        codes = tuple(corrected_code(v) for v in np.maximum(req, 0.0))
    return {"codes": codes, "req_nits": req, "orig_nits": field, "gain": gain,
            "pedestal_nits": float(ped.mean()), "ped_ref_nits": float(ped_ref)}
