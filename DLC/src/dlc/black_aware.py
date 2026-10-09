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
2. the NATIVE near-black floor fitted from a RAW stage (:func:`fit_native_floor`): first the run's own raw
   stage (when this run measured raw), then the raw stage of the installed stack's training run (the
   ``--verify-patches-from`` source, else the stack registry's applying run). The raw stage measures the
   native panel (identity MHC, no cube) as its own stage, separately from the verify, so it is not circular.
   On a local-dimming panel the in-content floor appears only once the LEDs are on, and a ramp of lit
   near-black greys shows it (PA32UCXR raw: 0.98 % -> 0.0059 nit against a 0.0022 PQ target, 6.35 % ->
   0.113 against 0.105). A refused fit falls through to the next source, and the block records why;
3. the display's recorded black: the DIP's ``native_black_nits`` (characterize's black read; a recorded
   run carries the value it ran with in its preflight ``panel_limits`` tell). CAVEAT: this is a FULL-FIELD
   black frame. A local-dimming panel switches its LEDs off there (PA32UCXR: 0.0), and an LCD with a
   black-frame backlight dip reads low (BenQ: x1.7 below its in-content black). On those panels it
   understates the in-content floor, which is why the raw fit comes first;
4. otherwise UNAVAILABLE: the block says so and the headline stays raw.

THE RAW FIT (:func:`fit_native_floor`). An ADDITIVE offset: the median of (measured Y - PQ target Y) over the
raw ramp's native greys (R = G = B, one value per unique signal: the mean of its reads) with
``0 < PQ target <=`` :data:`RAW_FIT_MAX_TARGET_NITS` and a measured Y above :data:`RAW_FIT_METER_FLOOR_NITS`.
It is REFUSED (unavailable from this source) when

* fewer than :data:`RAW_FIT_MIN_GREYS` greys are usable, or
* the fit is INCOHERENT: the median offset is not positive (no lift), or the offsets' robust spread
  (1.4826 x their median absolute deviation) is not below the median itself.

The black at code 0, and greys a local-dimming panel shows with its LEDs off (they read 0), are below the
meter floor and stay out of the fit. They need no floor: the band rule never charges a reached black.

What is NEVER a floor source:

* the DIP ``noise_floor_nits``: the meter's single-read trust floor, not the panel's black;
* ``mhc_params.dark_floor``: the MHC refine's chroma-trust floor;
* the verify's own greys or black patch: they are what is being scored, so using them would be circular.

A recorded floor of 0 is AVAILABLE but lifts nothing. The BT.2390 toe is then the identity, the black-aware
score equals the raw one, and the headline stays raw.

numpy at import; the engine (``colour``, the dE_ITP the verify scores with) is imported lazily.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from . import _pq

__all__ = ["FLOOR_LIMITED_FACTOR", "BlackFloor", "resolve_black_floor", "bt2390_black_lift",
           "black_aware_patch_scores", "MODEL_TEXT", "SCORING_TEXT", "RawFloorFit", "fit_native_floor",
           "raw_floor_from_ti3", "raw_floor_from_run", "RAW_FIT_MAX_TARGET_NITS", "RAW_FIT_METER_FLOOR_NITS",
           "RAW_FIT_MIN_GREYS", "RAW_FIT_RULE"]

# Below this multiple of the floor a PQ target is "floor-limited": there the BT.2390 lift differs materially
# from the PQ target. Above it, the patch keeps its raw error.
FLOOR_LIMITED_FACTOR = 10.0

# The raw-stage native floor fit (see the module doc).
RAW_FIT_MAX_TARGET_NITS = 0.3     # the near-black window: above it the native tone error outgrows the floor
RAW_FIT_METER_FLOOR_NITS = 0.001  # reads at or below it are the meter's / the LEDs-off zero, not a lit floor
RAW_FIT_MIN_GREYS = 3
_MAD_SIGMA = 1.4826               # MAD -> sigma for a normal spread
RAW_FIT_RULE = (f"additive offset = the median of (measured Y - PQ target Y) over the raw ramp's native greys "
                f"(R = G = B, the mean of each signal's reads) with 0 < PQ target <= {RAW_FIT_MAX_TARGET_NITS:g} "
                f"nit and measured Y > {RAW_FIT_METER_FLOOR_NITS:g} nit; refused with fewer than "
                f"{RAW_FIT_MIN_GREYS} usable greys, a median offset <= 0 (no lift), or a robust spread "
                f"(1.4826 x MAD) >= the median (incoherent)")

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
    * ``peak_nits`` is the BT.2390 source white: the run's target peak.
    * ``fit`` is the raw-stage fit's statistics when the floor came from one (:class:`RawFloorFit`).
    * ``skipped`` lists the sources tried before the one used (or before giving up), each with why."""
    nits: Optional[float]
    source: str
    peak_nits: Optional[float] = None
    fit: Optional[Mapping[str, Any]] = None
    skipped: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.nits is not None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"floor_nits": self.nits, "floor_source": self.source,
                               "source_white_nits": self.peak_nits}
        if self.fit is not None:
            out["floor_fit"] = dict(self.fit)
        if self.skipped:
            out["floor_sources_skipped"] = list(self.skipped)
        return out


@dataclass(frozen=True)
class RawFloorFit:
    """The native near-black floor fitted from one RAW stage (:func:`fit_native_floor`).

    * ``nits`` is ``None`` when the fit is REFUSED, and ``reason`` then says why.
    * ``source`` is the label the floor carries when it is used (e.g. ``raw run 20260923_120740 (...)``).
    * ``stats`` is the evidence: the rule, n, the median / spread, the signal range used and per-grey rows."""
    nits: Optional[float]
    source: str
    reason: Optional[str] = None
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.nits is not None


def fit_native_floor(samples: Iterable[Any]) -> tuple[Optional[float], Optional[str], dict[str, Any]]:
    """The additive native near-black floor from a raw ramp: ``(floor_nits, refusal, stats)``.

    ``samples`` carry ``rgb`` (the HDR signal, 0..1) and ``xyz`` (absolute, nit) like
    :class:`dlc.mhc.Ti3Sample`. The rule is :data:`RAW_FIT_RULE` (see the module doc). ``floor_nits`` is
    ``None`` when the fit is refused, and ``refusal`` then states which part of the rule failed."""
    by_sig: dict[int, list[float]] = {}
    sig_of: dict[int, float] = {}
    for s in samples:
        r, g, b = (float(c) for c in s.rgb)
        if not (abs(r - g) <= 1e-6 and abs(r - b) <= 1e-6):
            continue
        y = float(s.xyz[1])
        if not math.isfinite(y):
            continue
        key = int(round(r * 1e6))
        by_sig.setdefault(key, []).append(y)
        sig_of[key] = r
    rows: list[dict[str, Any]] = []
    below = above = 0
    for key in sorted(by_sig):
        sig = sig_of[key]
        target = _pq.eotf_norm(sig) * _pq.CONTAINER_NITS
        if not (0.0 < target <= RAW_FIT_MAX_TARGET_NITS):
            above += int(target > RAW_FIT_MAX_TARGET_NITS)
            continue
        meas = float(np.mean(by_sig[key]))
        if meas <= RAW_FIT_METER_FLOOR_NITS:
            below += 1
            continue
        rows.append({"signal_pct": round(100.0 * sig, 4), "target_nits": round(target, 6),
                     "measured_nits": round(meas, 6), "offset_nits": round(meas - target, 6),
                     "reads": len(by_sig[key])})
    stats: dict[str, Any] = {"rule": RAW_FIT_RULE, "n": len(rows), "n_greys_total": len(by_sig),
                             "n_below_meter_floor": below, "n_above_window": above, "greys": rows}
    if len(rows) < RAW_FIT_MIN_GREYS:
        return None, (f"{len(rows)} usable native grey(s) in the near-black window (need >= {RAW_FIT_MIN_GREYS}; "
                      f"{below} at or below the {RAW_FIT_METER_FLOOR_NITS:g}-nit meter floor)"), stats
    off = np.array([r["offset_nits"] for r in rows], dtype=float)
    med = float(np.median(off))
    mad = float(np.median(np.abs(off - med)))
    spread = _MAD_SIGMA * mad
    stats.update({"median_offset_nits": round(med, 6), "mad_nits": round(mad, 6),
                  "robust_spread_nits": round(spread, 6),
                  "offset_range_nits": [round(float(off.min()), 6), round(float(off.max()), 6)],
                  "n_positive": int(np.sum(off > 0.0)),
                  "signal_range_pct": [rows[0]["signal_pct"], rows[-1]["signal_pct"]],
                  "target_range_nits": [rows[0]["target_nits"], rows[-1]["target_nits"]]})
    if med <= 0.0:
        return None, f"incoherent: the median offset {med:+.5f} nit is not a lift (n {len(rows)})", stats
    if spread >= med:
        return None, (f"incoherent: the offsets' robust spread {spread:.5f} nit (1.4826 x MAD) is not below "
                      f"their median {med:.5f} nit (n {len(rows)})"), stats
    return round(med, 5), None, stats


def _run_short(name: str) -> str:
    """``20260923_120740_186046_hdr_...`` -> ``20260923_120740`` (the run's date + time)."""
    parts = name.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() else name


def raw_floor_from_ti3(ti3_path: Path, *, run_name: str, role: str) -> RawFloorFit:
    """:func:`fit_native_floor` over a raw stage's ``.ti3``. ``role`` says which run this is to the verify
    being scored (e.g. ``this run's raw stage``). Never raises: an unreadable file is a refusal."""
    label = f"raw run {_run_short(run_name)} ({role}; native near-black grey offset)"
    try:
        from .mhc import parse_ti3

        samples = parse_ti3(Path(ti3_path))
    except Exception as exc:  # noqa: BLE001 - a refusal, never a crash
        return RawFloorFit(None, label, f"raw run {run_name}: {Path(ti3_path).name} unreadable "
                                        f"({type(exc).__name__}: {exc})")
    nits, why, stats = fit_native_floor(samples)
    stats = {"run": run_name, "role": role, "ti3": str(ti3_path), **stats}
    if nits is None:
        return RawFloorFit(None, label, f"raw run {run_name} ({role}): {why}", stats)
    return RawFloorFit(nits, label, None, stats)


def raw_floor_from_run(run_dir: Path, *, role: str, mode: str = "HDR") -> RawFloorFit:
    """The native floor of a recorded run's RAW stage: the run's ``dlc_state.json`` must be a ``mode`` run with
    a done ``measure:raw`` stage, and its ``raw.ti3`` must be on disk (looked up in the run folder first, then at
    the recorded path). Anything else is a refusal that says why. Never raises."""
    root = Path(run_dir)
    label = f"raw run {_run_short(root.name)} ({role}; native near-black grey offset)"
    try:
        state = json.loads((root / "dlc_state.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        return RawFloorFit(None, label, f"raw run {root.name} ({role}): dlc_state.json unreadable "
                                        f"({type(exc).__name__})")
    run_mode = str(state.get("mode") or "").upper()
    if run_mode != mode.upper():
        return RawFloorFit(None, label, f"raw run {root.name} ({role}): a {run_mode or 'unknown'}-mode run, "
                                        f"not {mode.upper()}")
    rec = (((state.get("calib") or {}).get("stages") or {}).get("measure:raw") or {})
    if rec.get("status") != "done":
        return RawFloorFit(None, label, f"raw run {root.name} ({role}): no completed raw stage")
    recorded = (rec.get("data") or {}).get("ti3")
    name = Path(str(recorded)).name if recorded else "raw.ti3"
    for cand in (root / "measurements" / name, Path(str(recorded)) if recorded else None):
        if cand is not None and cand.is_file():
            return raw_floor_from_ti3(cand, run_name=root.name, role=role)
    return RawFloorFit(None, label, f"raw run {root.name} ({role}): its {name} is not on disk")


def _finite_nonneg(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0.0 else None


def resolve_black_floor(*, explicit: Any = None, explicit_source: str = "explicit option",
                        raw: Sequence[RawFloorFit] = (),
                        recorded: Any = None, recorded_source: str = "DIP native_black_nits",
                        peak_nits: Any = None) -> BlackFloor:
    """The floor in the documented order: ``explicit``, then the first available ``raw`` fit (the run's own raw
    stage, then the installed stack's training run's; :func:`raw_floor_from_run`), then ``recorded`` (the
    display's characterized black), else unavailable.

    * An explicit value that is not finite and non-negative raises ``ValueError``: an operator typo must
      never silently fall through to another source.
    * A refused raw fit and an invalid recorded value are skipped, and the result lists why (``skipped``).

    ``peak_nits`` (the run's target peak, the BT.2390 source white) rides along. Without a valid peak the
    floor is unavailable: the lift has no source white to normalise to."""
    peak = _finite_nonneg(peak_nits)
    peak = peak if peak and peak > 0 else None
    skipped: list[str] = []
    fit: Optional[Mapping[str, Any]] = None
    if explicit is not None:
        val = _finite_nonneg(explicit)
        if val is None:
            raise ValueError(f"black floor must be a finite, non-negative luminance (nit), got {explicit!r}")
        floor, source = val, explicit_source
    else:
        used = None
        for r in raw:
            if r.available and _finite_nonneg(r.nits) is not None:
                used = r
                break
            skipped.append(r.reason or f"{r.source}: refused")
        if used is not None:
            floor, source, fit = float(used.nits), used.source, used.stats
        else:
            val = _finite_nonneg(recorded)
            if val is None:
                why = ("no recorded display black" if recorded is None
                       else f"the recorded display black {recorded!r} is not a valid luminance")
                raw_why = (f", no usable raw near-black floor ({'; '.join(skipped)})" if skipped else "")
                return BlackFloor(None, f"unavailable: {why}{raw_why} and no explicit floor option", peak,
                                  skipped=tuple(skipped))
            floor, source = val, recorded_source
    if peak is None:
        return BlackFloor(None, f"unavailable: no target peak (BT.2390 source white) for the {source} floor", None,
                          fit=fit, skipped=tuple(skipped))
    return BlackFloor(floor, source, peak, fit=fit, skipped=tuple(skipped))


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
