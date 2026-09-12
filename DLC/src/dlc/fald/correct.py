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

clamped to ``[0, white · B_est]`` (the LCD cannot open past 100 %). Where the request would clip we
keep the ORIGINAL request (the pixel is a highlight the LCD saturates on anyway) so the dimming
algorithm's input is not disturbed — this is what makes the fixed-point iteration converge in one
step (review 2026-09-10: feeding the clipped value back dimmed the highlight's own cell drive and
the iteration drifted for 8+ rounds).

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


def load_fitted_params(path: Path) -> FaldParams:
    """Load a ``fald_fit_result.json`` (written by fald_fit.py) into :class:`FaldParams`."""
    fit = json.load(open(path))
    kw = dict(fit["params"])
    kw["drive_curve"] = [tuple(x) for x in kw["drive_curve"]]
    kw["chan_weights"] = tuple(kw["chan_weights"])
    return FaldParams(**{k: v for k, v in kw.items() if k in FaldParams.__dataclass_fields__})


def reference_pedestal(model: FaldModel, img: np.ndarray) -> np.ndarray:
    """Per-pixel pedestal (as-if-white nits) a UNIFORM field of the pixel's own level would carry:
    white · drive_curve(max channel) · tmin. Shape (h, w)."""
    p = model.p
    s = np.minimum(np.max(img, axis=0), p.white_nits)
    return p.white_nits * model.drive_of(s) * p.tmin


def correct_image(model: FaldModel, img: np.ndarray, iters: int = 2,
                  gain_clip: tuple[float, float] = (0.25, 4.0)) -> dict:
    """Return the corrected request image for ``img`` (3, h, w, as-if-white nits).

    Result dict: ``req`` (corrected image), ``gain`` (B_est/B_true of the last iteration),
    ``pedestal`` (per channel, nits, from the last iteration's drives), ``clipped`` (LCD would
    need > 100 %: original request kept), ``floored`` (target below the pedestal)."""
    p = model.p
    w = np.array(p.chan_weights)[:, None, None]
    lmax = p.white_nits * w
    ped_ref = reference_pedestal(model, img)[None]             # as-if-white, same for all channels
    cur = img.copy()
    gain = np.ones_like(img[0])
    for _ in range(max(1, iters)):
        drives = model.cell_drives(cur)
        b_true, b_est = model.backlights(drives)
        b_true = np.maximum(b_true, 0.0)
        gain = np.clip(b_est / np.maximum(b_true, 1e-9), gain_clip[0], gain_clip[1])
        # deep-dark fade: trust the model only where the panel's estimate is not ~zero (FaldParams.fade_*)
        t = np.clip((b_est - p.fade_lo) / max(p.fade_hi - p.fade_lo, 1e-9), 0.0, 1.0)
        wfade = t * t * (3.0 - 2.0 * t)
        gain = 1.0 + (gain - 1.0) * wfade
        ped = lmax * b_true[None] * p.tmin                     # per channel, nits, actual context
        # Pedestal term, HUE-PRESERVING (2026-09-12, live A/B showed blue rims on dark edges): the
        # panel adds the same leak to all three channels, so the correction subtracts the same
        # amount from all three — limited by the darkest channel. What cannot be removed stays as
        # white desaturation (the physical residual) instead of a per-channel clip that zeroes R/G
        # and leaves B (a hue rotation). delta > 0 (uniform field leaks more than this context) is
        # a plain lift and never clips.
        delta = ped_ref - ped / w                              # as-if-white, identical for R/G/B
        darkest = img.min(axis=0, keepdims=True)
        floored_amt = np.maximum(-delta - darkest, 0.0)        # part of the subtraction the pixel cannot take
        adj = np.where(delta < 0.0, delta + floored_amt, delta) * wfade
        req = (img + adj) * gain[None]
        floored = np.broadcast_to(floored_amt > 0.0, req.shape)
        req = np.maximum(req, 0.0)
        cap = p.white_nits * np.maximum(b_est, 1e-9)[None]     # T ≤ 1  ⇔  req ≤ white·B_est
        clipped = (req > cap) & (wfade[None] >= 1.0)           # only where the model is trusted
        req = np.where(clipped, np.maximum(img, cap), req)     # saturated highlight: keep the original
        cur = req
    return {"req": cur, "gain": gain, "pedestal": ped, "clipped": clipped, "floored": floored}


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
        b_true, b_est = model.backlights(drives)
        g = (b_est / np.maximum(b_true, 1e-9))[mask]
        ped = (p.white_nits * np.maximum(b_true, 0.0) * p.tmin)[mask]      # as-if-white
        req = ((field[:, None] + ped_ref - ped[None]) * g[None]).mean(axis=1)
        gain = float(g.mean())
        codes = tuple(corrected_code(v) for v in np.maximum(req, 0.0))
    return {"codes": codes, "req_nits": req, "orig_nits": field, "gain": gain,
            "pedestal_nits": float(ped.mean()), "ped_ref_nits": float(ped_ref)}
