"""Black-aware HDR scoring: the display's black as a PANEL LIMIT, like an out-of-gamut colour.
A SCORING option and EVIDENCE ONLY. It changes no calibration target, cube or picture, and no gate reads it.

Why (owner decision 2026-10-09). Near black, the PA32UCXR's HDR verify reads high by a roughly constant
~+0.006 nit from 0.01 to 0.25 nit (D1 run 20261002_145601: 0.0022 -> 0.0063 nit, 5.6 dE_ITP; 0.011 -> 0.013;
0.039 -> 0.046; 0.105 -> 0.111; 0.25 -> 0.262). Repeat reads agree to 0.02 dE_ITP, so it is a real raised
floor, not noise. That floor dominates the content-weighted practical score (:mod:`dlc.content_score`). It
is a panel limit, so near black is scored against a target that rolls smoothly into the display's black.

TARGET MODEL: the BT.2390 EETF black-level lift (Rec. ITU-R BT.2390 §5.4.1), the toe only.

    E1 = (PQ(L) - PQ(L_B)) / (PQ(L_W) - PQ(L_B))          b = (PQ(L_min) - PQ(L_B)) / (PQ(L_W) - PQ(L_B))
    E3 = E1 + b * (1 - E1)^4      (0 <= E1 <= 1; at or above the source white: no lift)
    L' = PQ^-1(E3 * (PQ(L_W) - PQ(L_B)) + PQ(L_B))

* Source black ``L_B`` = 0.
* Source white ``L_W`` = the run's target peak. The EETF's highlight roll-off is the identity there (target
  display max = source white), so only the toe acts.
* ``L_min`` = the display's black (the FLOOR).
* ``L'(0) = L_min`` exactly, and ``L'(L_W) = L_W``.

It is applied to the PQ target's LUMINANCE with the chroma kept: the target XYZ is scaled by ``L'/L`` at the
target's own chromaticity, and a zero-luminance target takes the run's white chromaticity.

WHICH PATCHES (``floor_limited``). A patch is floor-limited when its PQ target luminance is below
:data:`FLOOR_LIMITED_FACTOR` times the floor. That is where the black-aware target differs materially from
the PQ target. Every other patch keeps its raw error, bit for bit. Like the gamut clamp's ``clamped`` zone,
the label exists so the RAW number stays visible beside the black-aware one.

SCORING SEMANTICS: the panel-limit BAND. A floor-limited patch is scored (dE_ITP, the engine's own ICtCp) against
the point of the luminance segment [PQ target, BT.2390 target] (at the target's chromaticity) nearest its
measured luminance. The limit allows the lift but does not demand it:

* inside the band, only the chroma error counts;
* below the PQ target (crush), the raw error stands;
* above the BT.2390 target, the patch is scored against the BT.2390 target.

Why not score against the BT.2390 point itself (the literal reading)? That point is recorded beside the score
as the ``vs_toe_point`` variant, but it is not the score, for two reasons. First, BT.2390's lift is additive in
PQ, not in linear light: at a 0.006-nit floor it lifts 0.0022 -> 0.014 nit and still lifts 0.58 nit by about
20 %, far beyond an additive pedestal. Second, a local-dimming panel reaches true black with its zones off: D1's
[0,0,0] reads 0, and the point would charge it 11.9 dE_ITP. On D1 (floor 0.006) the literal point scored the
content-weighted hdr_live 3.60 dE_ITP, against 1.68 raw.

FLOOR SOURCE, in this order (:func:`resolve_black_floor`):

1. an explicit option: ``--score-black-floor-nits`` on a run, or ``--black-floor-nits`` on
   ``python -m dlc.content_score rescore``;
2. the display's recorded black: the DIP's ``native_black_nits`` (characterize's black read; a recorded
   run carries the value it ran with in its preflight ``panel_limits`` tell). CAVEAT: this is a FULL-FIELD
   black frame. A local-dimming panel switches its LEDs off there (PA32UCXR: 0.0), and an LCD with a
   black-frame backlight dip reads low (BenQ: x1.7 below its in-content black). On those panels it
   understates the in-content floor, so pass the explicit option;
3. otherwise UNAVAILABLE: the block says so and the headline stays raw.

What is NEVER a floor source:

* the DIP ``noise_floor_nits``: the meter's single-read trust floor, not the panel's black;
* ``mhc_params.dark_floor``: the MHC refine's chroma-trust floor;
* the verify's own greys or black patch: they are what is being scored, so using them would be circular.

A recorded floor of 0 is AVAILABLE but lifts nothing. The BT.2390 toe is then the identity, the black-aware
score equals the raw one, and the headline stays raw.

numpy at import; the engine (``colour``, the dE_ITP the verify scores with) is imported lazily.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from . import _pq

__all__ = ["FLOOR_LIMITED_FACTOR", "BlackFloor", "resolve_black_floor", "bt2390_black_lift",
           "black_aware_patch_scores", "MODEL_TEXT", "SCORING_TEXT"]

# Below this multiple of the floor a PQ target is "floor-limited": there the BT.2390 lift differs materially
# from the PQ target. Above it, the patch keeps its raw error.
FLOOR_LIMITED_FACTOR = 10.0

MODEL_TEXT = ("BT.2390 EETF black-level lift (toe only): E3 = E1 + b(1-E1)^4 in PQ normalised to "
              "[PQ(0), PQ(source white)], b = the normalised PQ of the floor; source black 0, source white = the "
              "run's target peak; applied to the PQ target luminance, chroma kept")
SCORING_TEXT = ("floor-limited signals are scored (dE_ITP) against the point of the luminance segment "
                "[PQ target, BT.2390 target] nearest the measured luminance, at the target chromaticity. The "
                "panel limit allows the lift but does not demand it: a patch reaching deeper black is not "
                "charged, and a crush below the PQ target keeps its raw error. 'vs_toe_point' = the literal "
                "score against the BT.2390 point, shown beside")


@dataclass(frozen=True)
class BlackFloor:
    """The display black the black-aware score lifts near-black targets to.

    * ``nits`` is ``None`` when the floor is UNAVAILABLE, and ``source`` then says why.
    * ``peak_nits`` is the BT.2390 source white: the run's target peak."""
    nits: Optional[float]
    source: str
    peak_nits: Optional[float] = None

    @property
    def available(self) -> bool:
        return self.nits is not None

    def as_dict(self) -> dict[str, Any]:
        return {"floor_nits": self.nits, "floor_source": self.source, "source_white_nits": self.peak_nits}


def _finite_nonneg(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0.0 else None


def resolve_black_floor(*, explicit: Any = None, explicit_source: str = "explicit option",
                        recorded: Any = None, recorded_source: str = "DIP native_black_nits",
                        peak_nits: Any = None) -> BlackFloor:
    """The floor in the documented order: ``explicit``, then ``recorded`` (the display's characterized black),
    else unavailable.

    * An explicit value that is not finite and non-negative raises ``ValueError``: an operator typo must
      never silently fall through to another source.
    * An invalid recorded value is skipped, and the result says so.

    ``peak_nits`` (the run's target peak, the BT.2390 source white) rides along. Without a valid peak the
    floor is unavailable: the lift has no source white to normalise to."""
    peak = _finite_nonneg(peak_nits)
    peak = peak if peak and peak > 0 else None
    if explicit is not None:
        val = _finite_nonneg(explicit)
        if val is None:
            raise ValueError(f"black floor must be a finite, non-negative luminance (nit), got {explicit!r}")
        floor, source = val, explicit_source
    else:
        val = _finite_nonneg(recorded)
        if val is None:
            why = ("no recorded display black" if recorded is None
                   else f"the recorded display black {recorded!r} is not a valid luminance")
            return BlackFloor(None, f"unavailable: {why} and no explicit floor option", peak)
        floor, source = val, recorded_source
    if peak is None:
        return BlackFloor(None, f"unavailable: no target peak (BT.2390 source white) for the {source} floor", None)
    return BlackFloor(floor, source, peak)


_pq_oetf = np.vectorize(_pq.oetf_norm, otypes=[float])
_pq_eotf = np.vectorize(_pq.eotf_norm, otypes=[float])


def bt2390_black_lift(nits: Any, *, min_nits: float, source_white_nits: float,
                      source_black_nits: float = 0.0) -> np.ndarray:
    """The BT.2390 EETF black-level lift (toe only) of absolute luminance ``nits``. See the module doc.

    * ``L'(L_B) = L_min`` and ``L'(L_W) = L_W``.
    * At or above the source white the input is returned unchanged.
    * ``min_nits`` 0 is the identity (up to the PQ round trip)."""
    y = np.asarray(nits, dtype=float)
    lw, lb, lmin = float(source_white_nits), float(source_black_nits), float(min_nits)
    if not (lw > lb >= 0.0) or lmin < 0.0:
        raise ValueError(f"bt2390_black_lift needs source white > source black >= 0 and min >= 0 "
                         f"(got L_W {lw}, L_B {lb}, L_min {lmin})")
    c = _pq.CONTAINER_NITS
    pb, pw = _pq.oetf_norm(lb / c), _pq.oetf_norm(lw / c)
    span = pw - pb
    e1 = (_pq_oetf(np.clip(y, 0.0, None) / c) - pb) / span
    b = (_pq.oetf_norm(lmin / c) - pb) / span
    lift = np.where((e1 >= 0.0) & (e1 < 1.0), b * np.power(np.clip(1.0 - e1, 0.0, 1.0), 4), 0.0)
    out = _pq_eotf((e1 + lift) * span + pb) * c
    # Outside the toe the input passes through bit for bit (no PQ round-trip noise).
    return np.where(lift > 0.0, out, np.clip(y, 0.0, None))


def _xyz_white(white_xy: Optional[Sequence[float]]) -> np.ndarray:
    wx, wy = (float(white_xy[0]), float(white_xy[1])) if white_xy is not None else (0.3127, 0.3290)
    return np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy])


def black_aware_patch_scores(patch_metrics: Sequence[Any], floor: BlackFloor, *,
                             white_xy: Optional[Sequence[float]] = None) -> dict[str, np.ndarray]:
    """Per scored patch (HDR, dE_ITP), with ``floor`` available and positive:

    * ``floor_limited``: the PQ target Y is below :data:`FLOOR_LIMITED_FACTOR` x the floor;
    * ``target_y`` / ``toe_y`` / ``band_y``: the PQ, BT.2390 and scored luminance;
    * ``e_black_aware``: the band score, which is the raw ``de2000`` wherever the patch is not
      floor-limited;
    * ``e_toe_point``: the literal score against the BT.2390 point, beside the band score.

    The measured XYZ is sanitised as :func:`dlc.engine.model.score_hdr` does (non-finite -> 0, negatives -> 0)."""
    if not floor.available or not floor.nits or floor.nits <= 0 or not floor.peak_nits:
        raise ValueError("black_aware_patch_scores needs an available, positive floor and a target peak")
    n = len(patch_metrics)
    raw = np.array([float(m.de2000) for m in patch_metrics], dtype=float)
    tgt = np.array([[float(c) for c in m.target_xyz] for m in patch_metrics], dtype=float).reshape(n, 3)
    meas = np.nan_to_num(np.array([[float(c) if c is not None else 0.0 for c in m.measured_xyz]
                                   for m in patch_metrics], dtype=float).reshape(n, 3),
                         nan=0.0, posinf=0.0, neginf=0.0)
    meas = np.maximum(meas, 0.0)
    ty = np.maximum(tgt[:, 1], 0.0)
    limited = ty < FLOOR_LIMITED_FACTOR * float(floor.nits)
    toe_y = ty.copy()
    band_y = ty.copy()
    e_ba = raw.copy()
    e_toe = raw.copy()
    if limited.any():
        from .engine.model import TargetSpace, de_itp

        idx = np.flatnonzero(limited)
        toe_y[idx] = bt2390_black_lift(ty[idx], min_nits=float(floor.nits),
                                       source_white_nits=float(floor.peak_nits))
        band_y[idx] = np.clip(meas[idx, 1], ty[idx], toe_y[idx])
        # The target's own chromaticity, scaled to the new luminance. A black target has none, so it takes
        # the run's white chromaticity.
        unit = np.where(ty[idx, None] > 0.0, tgt[idx] / np.where(ty[idx, None] > 0.0, ty[idx, None], 1.0),
                        _xyz_white(white_xy)[None, :])
        m_ict = TargetSpace.xyz_to_ictcp(meas[idx])
        e_ba[idx] = de_itp(m_ict - TargetSpace.xyz_to_ictcp(unit * band_y[idx, None]))
        e_toe[idx] = de_itp(m_ict - TargetSpace.xyz_to_ictcp(unit * toe_y[idx, None]))
    return {"floor_limited": limited, "target_y": ty, "toe_y": toe_y, "band_y": band_y,
            "measured_y": meas[:, 1], "e_black_aware": e_ba, "e_toe_point": e_toe}
