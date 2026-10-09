"""Black-aware HDR scoring: the display's raised black as a PANEL LIMIT, like an out-of-gamut colour.
A SCORING option and EVIDENCE ONLY. It changes no calibration target, cube or picture, and no gate reads it.

Why (owner decision 2026-10-09). Near black the PA32UCXR shows an ADDITIVE raised black, measured natively (a few
thousandths of a nit over the PQ target), and it also reaches true black at the lowest codes (local dimming turns
the LEDs off). Repeat reads agree to 0.02 dE_ITP, so the lift is real, not noise. It dominates the
content-weighted practical score (:mod:`dlc.content_score`) although it is a panel limit, so the verify score
should not charge the calibration for it.

THE MODEL: an additive pedestal, in XYZ. At best the panel shows ``target_xyz + F * w``:

* ``F`` = the display floor in nit (:func:`resolve_black_floor`);
* ``w`` = the pedestal's colour as a unit-Y XYZ: the panel's NATIVE WHITE chromaticity (the raw stage's measured
  native white, else the DIP's ``native_white_xy``, else D65, which the block then states as ASSUMED).

THE SCORE (:func:`black_aware_patch_scores`). EVERY HDR verify patch is scored (dE_ITP, the engine's own ICtCp)
against the NEAREST point of the segment ``[target_xyz, target_xyz + F * w]``:

* the panel limit ALLOWS the pedestal but does not demand it. A patch anywhere between its target and
  target + pedestal costs nothing. True black (target 0) reading 0 costs nothing, and reading the pedestal costs
  nothing either;
* a lift in any other colour, or beyond the pedestal, is charged. The segment only adds ``F`` nit of
  pedestal-coloured light: a lifted saturated blue, or a near-black grey lifted to several times the floor,
  keeps (most of) its error;
* a crush below the target keeps its raw error;
* NO CUTOFF. Bright patches are scored by the same rule. There ``F * w`` is negligible next to the target, so
  the score is continuous and converges to the raw one. Greys converge first. A saturated colour keeps a
  near-black channel well above 1 nit, and the white pedestal is still a few percent of that channel, so its
  chroma can move by a tenth of a dE_ITP there (D1: a 1.7-nit Rec.2020 green, 0.17; greys above 1 nit, 0.0001).
  The block records the largest per-signal change above 10 F, 1 nit and 10 nit, all signals and greys only
  (``continuity``).

The nearest point is found numerically along the segment: a :data:`SEGMENT_GRID`-point grid on
``t in [0, 1]``, then a golden-section refinement in the cells around the grid minimum. Where no point of the
segment beats the target itself, the raw error stands bit for bit.

VARIANTS, recorded beside the score and never instead of it. Both apply only to the FLOOR-LIMITED signals
(below), and every other signal keeps its raw error in them:

* ``vs_bt2390_band``: the SUPERSEDED first rule (2026-10-09). The luminance band ``[PQ target, BT.2390 toe
  target]`` at the target's chromaticity. It is too permissive: BT.2390 lifts near black to 2.5-7x the floor,
  so a cube defect lifting near black to the toe cost 0, and it forgave a lifted saturated blue (Y at fixed
  chroma).
* ``vs_toe_point``: the literal BT.2390 toe point itself. It charges a reached black, which a local-dimming
  panel shows.

BT.2390 EETF black-level lift (Rec. ITU-R BT.2390 §5.4.1), toe only, for the variants:

    E1 = (PQ(L) - PQ(L_B)) / (PQ(L_W) - PQ(L_B))          b = (PQ(L_min) - PQ(L_B)) / (PQ(L_W) - PQ(L_B))
    E3 = E1 + b * (1 - E1)^4      (0 <= E1 <= 1; at or above the source white: no lift)
    L' = PQ^-1(E3 * (PQ(L_W) - PQ(L_B)) + PQ(L_B))

with source black ``L_B`` = 0, source white ``L_W`` = the run's target peak, and ``L_min`` = the floor. The
variants need the peak, and without one they are not computed. The score itself does not use the peak.

FLOOR-LIMITED (descriptive, ``floor_limited``). A signal is floor-limited when its PQ target Y is below
:data:`FLOOR_LIMITED_FACTOR` x the floor, i.e. where the pedestal is at least 10 % of the target. It is a class
for the breakdown (the raw number stays visible beside the black-aware one there) and the scope of the
variants. It is NOT a scoring cutoff.

FLOOR SOURCE, in this order (:func:`resolve_black_floor`):

1. an explicit option: ``--score-black-floor-nits`` on a run, or ``--black-floor-nits`` on
   ``python -m dlc.content_score rescore``;
2. the NATIVE near-black floor fitted from a RAW stage (:func:`fit_native_floor`). First the run's own raw
   stage (when this run measured raw), then a recorded run's raw stage (:func:`raw_floor_from_run`). A recorded
   run is used only when its display, EDID hardware id, mode and colorimeter correction match the scored run's
   (:func:`identity_check`). It is labelled by what it is to the scored run: the installed stack's training
   run, the installed MHC's applying run, or, when this run built its own MHC (``full`` / ``mhc-only``), a
   PREVIOUSLY applied stack's run that is not this run's stack. The raw stage measures the native panel
   (identity MHC, no cube) as its own stage, separately from the verify, so it is not circular. A refused or
   mismatched source falls through to the next one, and the block records why;
3. the display's recorded black: the DIP's ``native_black_nits`` (characterize's black read; a recorded
   run carries the value it ran with in its preflight ``panel_limits`` tell). CAVEAT: this is a FULL-FIELD
   black frame. A local-dimming panel switches its LEDs off there (PA32UCXR: 0.0), and an LCD with a
   black-frame backlight dip reads low (BenQ: x1.7 below its in-content black). On those panels it
   understates the in-content floor, which is why the raw fit comes first;
4. otherwise UNAVAILABLE: the block says so and the headline stays raw.

THE RAW FIT (:func:`fit_native_floor`, :data:`RAW_FIT_RULE`). The INTERCEPT ``F`` of the straight line
``measured Y = F + g * PQ target Y`` (least squares) over the LOWEST lit native greys of the raw ramp. A pure
offset would take the bottom greys' offset, but a native tone (gain) error grows with the target. A median
offset over a wide window therefore picks up the tone error of the 0.03-0.26-nit greys, and the intercept
separates the two. The window is the first of :data:`RAW_FIT_WINDOWS_NITS` holding at least
:data:`RAW_FIT_MIN_GREYS` greys: 0.05 nit, widened only when there are too few lit greys there. A grey counts
when it is lit: its mean measured Y is above the METER FLOOR, the DIP's ``noise_floor_nits`` (else
:data:`RAW_FIT_METER_FLOOR_FALLBACK_NITS`, stated). The black at code 0 and the greys a local-dimming panel
shows with its LEDs off read 0 and stay out. They need no floor: the score never charges a reached black.
The fit is REFUSED when there are too few greys, when ``F <= 0`` (no lift), when ``g <= 0``, or when the
intercept's standard error is not below ``F`` (unresolved). It reports ``F``, ``g``, ``n``, the residuals and
the window.

What is NEVER a floor source:

* the DIP ``noise_floor_nits``: the meter's single-read trust floor, not the panel's black (it only sets which
  raw greys count as lit);
* ``mhc_params.dark_floor``: the MHC refine's chroma-trust floor;
* the verify's own greys or black patch: they are what is being scored, so using them would be circular.

A recorded floor of 0 is AVAILABLE but lifts nothing: the black-aware score equals the raw one, and the
headline stays raw.

numpy at import; the engine (``colour``, the dE_ITP the verify scores with) is imported lazily.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from . import _pq

__all__ = ["FLOOR_LIMITED_FACTOR", "SEGMENT_GRID", "BlackFloor", "resolve_black_floor", "bt2390_black_lift",
           "black_aware_patch_scores", "MODEL_TEXT", "SCORING_TEXT", "BT2390_BAND_TEXT", "RawFloorFit",
           "fit_native_floor", "raw_floor_from_ti3", "raw_floor_from_run", "run_identity", "identity_check",
           "xy_from_xyz", "RAW_FIT_WINDOWS_NITS", "RAW_FIT_METER_FLOOR_FALLBACK_NITS", "RAW_FIT_MIN_GREYS",
           "RAW_FIT_RULE", "D65_XY", "PEDESTAL_ASSUMED"]

# The descriptive floor-limited class (and the scope of the BT.2390 variants): PQ target Y below this multiple
# of the floor, i.e. the pedestal is >= 10 % of the target. NOT a scoring cutoff.
FLOOR_LIMITED_FACTOR = 10.0

# The nearest-point search along the pedestal segment: a grid on t in [0, 1], then golden-section refinement.
SEGMENT_GRID = 33
_GOLDEN_ITERS = 40

D65_XY = (0.3127, 0.3290)
PEDESTAL_ASSUMED = "D65 (ASSUMED: no native white available for the pedestal colour)"

# The raw-stage native floor fit (see the module doc).
RAW_FIT_WINDOWS_NITS = (0.05, 0.1, 0.2, 0.3)   # the first holding >= RAW_FIT_MIN_GREYS lit greys is used
RAW_FIT_METER_FLOOR_FALLBACK_NITS = 0.001       # only without a DIP noise_floor_nits
RAW_FIT_MIN_GREYS = 3
RAW_FIT_RULE = (f"F = the intercept of measured Y = F + g x PQ target Y (least squares) over the raw ramp's lit native "
                f"greys (R = G = B, the mean of each signal's reads, mean measured Y above the meter floor: the DIP "
                f"noise_floor_nits, else {RAW_FIT_METER_FLOOR_FALLBACK_NITS:g} nit) with 0 < PQ target <= W, W the "
                f"first of {', '.join(f'{w:g}' for w in RAW_FIT_WINDOWS_NITS)} nit holding >= {RAW_FIT_MIN_GREYS} "
                f"greys; refused with fewer greys in the widest window, F <= 0 (no lift), g <= 0, or a standard "
                f"error of F >= F (unresolved)")

MODEL_TEXT = ("additive pedestal in XYZ: the panel at best shows target + F x w, F = the display floor (nit), w = the "
              "pedestal colour as a unit-Y XYZ at the panel's native white chromaticity")
SCORING_TEXT = ("EVERY HDR patch is scored (dE_ITP) against the NEAREST point of the segment [target XYZ, target XYZ + "
                "F x w]. The limit allows the pedestal but does not demand it: true black reading 0 costs nothing, a "
                "lift beyond F or in another colour (e.g. a lifted saturated blue) is charged, a crush keeps its raw "
                "error. No cutoff: at bright levels F is negligible and the score converges to the raw one. Variants "
                "beside it, floor-limited signals only: 'vs_bt2390_band' (the superseded luminance band to the "
                "BT.2390 toe) and 'vs_toe_point' (the literal BT.2390 toe point)")
BT2390_BAND_TEXT = ("superseded rule: floor-limited signals scored against the luminance band [PQ target, BT.2390 "
                    "EETF toe target] at the target chromaticity (too permissive: the toe lifts 2.5-7x the floor)")


@dataclass(frozen=True)
class BlackFloor:
    """The display floor the black-aware score allows as an additive pedestal.

    * ``nits`` is ``None`` when the floor is UNAVAILABLE, and ``source`` then says why.
    * ``peak_nits`` is the run's target peak: the BT.2390 source white of the VARIANTS. Without it the variants
      are not computed. The score itself does not need it.
    * ``pedestal_xy`` / ``pedestal_source``: the pedestal's chromaticity (the native white) and where it came
      from. ``None`` = D65, ASSUMED.
    * ``fit`` is the raw-stage fit's statistics when the floor came from one (:class:`RawFloorFit`).
    * ``skipped`` lists the sources tried before the one used (or before giving up), each with why."""
    nits: Optional[float]
    source: str
    peak_nits: Optional[float] = None
    fit: Optional[Mapping[str, Any]] = None
    skipped: tuple[str, ...] = ()
    pedestal_xy: Optional[tuple[float, float]] = None
    pedestal_source: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.nits is not None

    @property
    def pedestal(self) -> tuple[tuple[float, float], str]:
        """``(xy, source)`` of the pedestal colour, D65 (stated as assumed) when none was resolved."""
        if self.pedestal_xy is not None:
            return (float(self.pedestal_xy[0]), float(self.pedestal_xy[1])), self.pedestal_source or "given"
        return D65_XY, PEDESTAL_ASSUMED

    def as_dict(self) -> dict[str, Any]:
        xy, src = self.pedestal
        out: dict[str, Any] = {"floor_nits": self.nits, "floor_source": self.source,
                               "source_white_nits": self.peak_nits,
                               "pedestal_xy": [round(xy[0], 5), round(xy[1], 5)], "pedestal_source": src}
        if self.fit is not None:
            out["floor_fit"] = dict(self.fit)
        if self.skipped:
            out["floor_sources_skipped"] = list(self.skipped)
        return out


@dataclass(frozen=True)
class RawFloorFit:
    """The native near-black floor fitted from one RAW stage (:func:`fit_native_floor`).

    * ``nits`` is ``None`` when the fit is REFUSED (or the run's identity does not match), and ``reason`` then
      says why.
    * ``source`` is the label the floor carries when it is used (e.g. ``raw run 20260923_120740 (...)``).
    * ``stats`` is the evidence: the rule, F, g, n, the residuals, the window and per-grey rows.
    * ``white_xy`` / ``white_source``: the raw stage's measured native white chromaticity, the pedestal colour
      when this floor is used."""
    nits: Optional[float]
    source: str
    reason: Optional[str] = None
    stats: dict[str, Any] = field(default_factory=dict)
    white_xy: Optional[tuple[float, float]] = None
    white_source: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.nits is not None


def xy_from_xyz(xyz: Any) -> Optional[tuple[float, float]]:
    """The chromaticity of an XYZ triple, ``None`` when it is not a valid colour."""
    try:
        x, y, z = (float(c) for c in xyz)
    except (TypeError, ValueError):
        return None
    s = x + y + z
    if not (math.isfinite(s) and s > 0 and min(x, y, z) >= 0):
        return None
    return x / s, y / s


def _valid_xy(xy: Any) -> Optional[tuple[float, float]]:
    try:
        x, y = (float(c) for c in xy)
    except (TypeError, ValueError):
        return None
    return (x, y) if (math.isfinite(x) and math.isfinite(y) and x > 0 and y > 0 and x + y < 1) else None


def _finite_nonneg(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0.0 else None


def fit_native_floor(samples: Iterable[Any], *, meter_floor_nits: Any = None,
                     meter_floor_source: Optional[str] = None,
                     ) -> tuple[Optional[float], Optional[str], dict[str, Any]]:
    """The additive native near-black floor from a raw ramp: ``(floor_nits, refusal, stats)``.

    ``samples`` carry ``rgb`` (the HDR signal, 0..1) and ``xyz`` (absolute, nit) like :class:`dlc.mhc.Ti3Sample`.
    ``meter_floor_nits`` is the DIP's ``noise_floor_nits`` (a grey must read above it to count as lit), else
    :data:`RAW_FIT_METER_FLOOR_FALLBACK_NITS`. The rule is :data:`RAW_FIT_RULE` (see the module doc).
    ``floor_nits`` is ``None`` when the fit is refused, and ``refusal`` then states which part of the rule
    failed."""
    meter = _finite_nonneg(meter_floor_nits)
    if meter is None:
        meter, meter_src = RAW_FIT_METER_FLOOR_FALLBACK_NITS, (
            f"fallback {RAW_FIT_METER_FLOOR_FALLBACK_NITS:g} nit (no DIP noise_floor_nits)")
    else:
        meter_src = meter_floor_source or "DIP noise_floor_nits"
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
    lit: list[dict[str, Any]] = []
    below = 0
    for key in sorted(by_sig):
        sig = sig_of[key]
        target = _pq.eotf_norm(sig) * _pq.CONTAINER_NITS
        if not (0.0 < target <= RAW_FIT_WINDOWS_NITS[-1]):
            continue
        meas = float(np.mean(by_sig[key]))
        if meas <= meter:
            below += 1
            continue
        lit.append({"signal_pct": round(100.0 * sig, 4), "target_nits": round(target, 6),
                    "measured_nits": round(meas, 6), "offset_nits": round(meas - target, 6),
                    "reads": len(by_sig[key]), "_t": target, "_m": meas})
    window = next((w for w in RAW_FIT_WINDOWS_NITS
                   if sum(1 for r in lit if r["_t"] <= w) >= RAW_FIT_MIN_GREYS), None)
    stats: dict[str, Any] = {"rule": RAW_FIT_RULE, "model": "measured Y = F + g x target Y",
                             "meter_floor_nits": meter, "meter_floor_source": meter_src,
                             "n_greys_total": len(by_sig), "n_below_meter_floor": below,
                             "window_nits": window,
                             "window_widened": window is not None and window != RAW_FIT_WINDOWS_NITS[0]}
    rows = [r for r in lit if window is not None and r["_t"] <= window]
    stats["n"] = len(rows)
    if window is None:
        stats["greys"] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in lit]
        return None, (f"{len(lit)} lit native grey(s) up to {RAW_FIT_WINDOWS_NITS[-1]:g} nit (need >= "
                      f"{RAW_FIT_MIN_GREYS}; {below} at or below the {meter:g}-nit meter floor)"), stats
    t = np.array([r["_t"] for r in rows], dtype=float)
    m = np.array([r["_m"] for r in rows], dtype=float)
    n = len(rows)
    tm = float(t.mean())
    sxx = float(np.sum((t - tm) ** 2))
    g = float(np.sum((t - tm) * (m - m.mean())) / sxx)
    f = float(m.mean() - g * tm)
    res = m - (f + g * t)
    dof = n - 2
    s2 = float(np.sum(res ** 2)) / dof if dof > 0 else float("nan")
    se_f = math.sqrt(s2 * (1.0 / n + tm * tm / sxx)) if dof > 0 else float("nan")
    for r, e in zip(rows, res):
        r["fitted_nits"] = round(float(f + g * r["_t"]), 6)
        r["residual_nits"] = round(float(e), 6)
    stats.update({"F_nits": round(f, 6), "g": round(g, 5), "se_F_nits": round(se_f, 6),
                  "residual_rms_nits": round(float(np.sqrt(np.mean(res ** 2))), 6),
                  "residual_max_abs_nits": round(float(np.max(np.abs(res))), 6),
                  "signal_range_pct": [rows[0]["signal_pct"], rows[-1]["signal_pct"]],
                  "target_range_nits": [rows[0]["target_nits"], rows[-1]["target_nits"]],
                  "offset_range_nits": [round(float(np.min(m - t)), 6), round(float(np.max(m - t)), 6)],
                  "greys": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]})
    if f <= 0.0:
        return None, f"incoherent: the intercept F {f:+.5f} nit is not a lift (n {n}, window {window:g} nit)", stats
    if g <= 0.0:
        return None, f"incoherent: the gain g {g:+.4f} is not positive (n {n}, window {window:g} nit)", stats
    if not (math.isfinite(se_f) and se_f < f):
        return None, (f"incoherent: the intercept's standard error {se_f:.5f} nit is not below F {f:.5f} nit "
                      f"(n {n}, window {window:g} nit)"), stats
    return round(f, 6), None, stats


def _run_short(name: str) -> str:
    """``20260923_120740_186046_hdr_...`` -> ``20260923_120740`` (the run's date + time)."""
    parts = name.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() else name


def _fit_label(run_name: str, role: str) -> str:
    return f"raw run {_run_short(run_name)} ({role}; native near-black grey intercept)"


def raw_floor_from_ti3(ti3_path: Path, *, run_name: str, role: str, meter_floor_nits: Any = None,
                       meter_floor_source: Optional[str] = None, white_xyz: Any = None) -> RawFloorFit:
    """:func:`fit_native_floor` over a raw stage's ``.ti3``. ``role`` says which run this is to the verify
    being scored (e.g. ``this run's raw stage``). ``white_xyz`` is that raw stage's measured native white (the
    pedestal colour). Never raises: an unreadable file is a refusal."""
    label = _fit_label(run_name, role)
    wxy = xy_from_xyz(white_xyz) if white_xyz is not None else None
    wsrc = f"raw run {_run_short(run_name)}'s native white (measure:raw white_xyz)" if wxy else None
    try:
        from .mhc import parse_ti3

        samples = parse_ti3(Path(ti3_path))
    except Exception as exc:  # noqa: BLE001 - a refusal, never a crash
        return RawFloorFit(None, label, f"raw run {run_name}: {Path(ti3_path).name} unreadable "
                                        f"({type(exc).__name__}: {exc})")
    nits, why, stats = fit_native_floor(samples, meter_floor_nits=meter_floor_nits,
                                        meter_floor_source=meter_floor_source)
    stats = {"run": run_name, "role": role, "ti3": str(ti3_path), **stats}
    if nits is None:
        return RawFloorFit(None, label, f"raw run {run_name} ({role}): {why}", stats)
    return RawFloorFit(nits, label, None, stats, white_xy=wxy, white_source=wsrc)


def _norm_correction(corr: Any) -> Optional[str]:
    """A preflight ``correction`` tell -> the correction's identity: its file's basename (case-folded), ``"none"``
    when the run had no correction, ``None`` when unknown."""
    if not isinstance(corr, Mapping):
        return None
    f = corr.get("file")
    if f:
        return os.path.normcase(Path(str(f).replace("\\", "/")).name)
    return "none" if corr.get("has_correction") is False else None


def run_identity(state: Mapping[str, Any], root: Optional[Path] = None) -> dict[str, Optional[str]]:
    """The identity a raw floor must match: the run's display name (preflight, else its manifest), EDID hardware
    id (preflight ``monitor_map``), mode and colorimeter correction (preflight ``correction`` file). Unknown
    fields are ``None``."""
    stages = ((state.get("calib") or {}).get("stages") or {}) if isinstance(state, Mapping) else {}
    dg = ((stages.get("preflight") or {}).get("digest") or {}) if isinstance(stages, Mapping) else {}
    display = dg.get("display")
    if not display and root is not None:
        try:
            display = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8")).get("display")
        except (OSError, ValueError, AttributeError):
            display = None
    hw = (dg.get("monitor_map") or {}).get("hardware_id") if isinstance(dg.get("monitor_map"), Mapping) else None
    mode = str(state.get("mode") or "").upper() or None
    return {"display": str(display) if display else None, "hardware_id": str(hw) if hw else None,
            "mode": mode, "correction": _norm_correction(dg.get("correction"))}


def identity_check(expect: Mapping[str, Any], got: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(mismatches, unverified)``: the identity fields (display, hardware id, mode, correction) that differ
    between the scored run (``expect``) and a raw run (``got``), and those unknown on either side."""
    mismatches: list[str] = []
    unverified: list[str] = []
    for key, what in (("display", "display"), ("hardware_id", "EDID hardware id"), ("mode", "mode"),
                      ("correction", "colorimeter correction")):
        e, g = expect.get(key), got.get(key)
        if e is None or g is None:
            unverified.append(key)
        elif str(e) != str(g):
            mismatches.append(f"{what} {g!r} != the scored run's {e!r}")
    return mismatches, unverified


def raw_floor_from_run(run_dir: Path, *, role: str, mode: str = "HDR",
                       expect: Optional[Mapping[str, Any]] = None, meter_floor_nits: Any = None,
                       meter_floor_source: Optional[str] = None) -> RawFloorFit:
    """The native floor of a recorded run's RAW stage.

    The run's ``dlc_state.json`` must be a ``mode`` run with a done ``measure:raw`` stage, its ``raw.ti3`` must
    be on disk (looked up in the run folder first, then at the recorded path), and, with ``expect`` (the scored
    run's :func:`run_identity`), its display / EDID hardware id / mode / correction must match. A field unknown
    on either side does not refuse, but the fit's stats list it under ``identity_unverified``. Anything else is a
    refusal that says why. Never raises."""
    root = Path(run_dir)
    label = _fit_label(root.name, role)
    try:
        state = json.loads((root / "dlc_state.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        return RawFloorFit(None, label, f"raw run {root.name} ({role}): dlc_state.json unreadable "
                                        f"({type(exc).__name__})")
    run_mode = str(state.get("mode") or "").upper()
    if run_mode != mode.upper():
        return RawFloorFit(None, label, f"raw run {root.name} ({role}): a {run_mode or 'unknown'}-mode run, "
                                        f"not {mode.upper()}")
    unverified: list[str] = []
    if expect is not None:
        mism, unverified = identity_check(expect, run_identity(state, root))
        if mism:
            return RawFloorFit(None, label, f"raw run {root.name} ({role}): identity mismatch: {'; '.join(mism)}",
                               {"run": root.name, "role": role, "identity_mismatch": mism})
    rec = (((state.get("calib") or {}).get("stages") or {}).get("measure:raw") or {})
    if rec.get("status") != "done":
        return RawFloorFit(None, label, f"raw run {root.name} ({role}): no completed raw stage")
    data = rec.get("data") or {}
    recorded = data.get("ti3")
    white = data.get("white_xyz") or (rec.get("digest") or {}).get("white_xyz")
    name = Path(str(recorded)).name if recorded else "raw.ti3"
    for cand in (root / "measurements" / name, Path(str(recorded)) if recorded else None):
        if cand is not None and cand.is_file():
            fit = raw_floor_from_ti3(cand, run_name=root.name, role=role, meter_floor_nits=meter_floor_nits,
                                     meter_floor_source=meter_floor_source, white_xyz=white)
            if expect is not None:
                fit.stats["identity_unverified"] = unverified
            return fit
    return RawFloorFit(None, label, f"raw run {root.name} ({role}): its {name} is not on disk")


def resolve_black_floor(*, explicit: Any = None, explicit_source: str = "explicit option",
                        raw: Sequence[RawFloorFit] = (),
                        recorded: Any = None, recorded_source: str = "DIP native_black_nits",
                        peak_nits: Any = None,
                        pedestal: Sequence[tuple[Any, str]] = ()) -> BlackFloor:
    """The floor in the documented order: ``explicit``, then the first available ``raw`` fit (the run's own raw
    stage, then a recorded run's; :func:`raw_floor_from_run`), then ``recorded`` (the display's characterized
    black), else unavailable.

    * An explicit value that is not finite and non-negative raises ``ValueError``: an operator typo must
      never silently fall through to another source.
    * A refused raw fit and an invalid recorded value are skipped, and the result lists why (``skipped``).
    * The pedestal colour: the used raw fit's native white, else the first valid ``pedestal`` candidate
      (``(xy, source)``, e.g. the DIP's ``native_white_xy``), else D65 stated as assumed.

    ``peak_nits`` (the run's target peak) rides along as the BT.2390 source white of the variants."""
    peak = _finite_nonneg(peak_nits)
    peak = peak if peak and peak > 0 else None
    skipped: list[str] = []
    fit: Optional[Mapping[str, Any]] = None
    used: Optional[RawFloorFit] = None
    if explicit is not None:
        val = _finite_nonneg(explicit)
        if val is None:
            raise ValueError(f"black floor must be a finite, non-negative luminance (nit), got {explicit!r}")
        floor, source = val, explicit_source
    else:
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
    ped_xy, ped_src = None, None
    cands = ([(used.white_xy, used.white_source or "the raw run's native white")] if used is not None else [])
    for xy, src in list(cands) + list(pedestal):
        ok = _valid_xy(xy) if xy is not None else None
        if ok is not None:
            ped_xy, ped_src = ok, src
            break
    return BlackFloor(floor, source, peak, fit=fit, skipped=tuple(skipped), pedestal_xy=ped_xy,
                      pedestal_source=ped_src)


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


def _xyz_unit(xy: Sequence[float]) -> np.ndarray:
    wx, wy = float(xy[0]), float(xy[1])
    return np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy])


def _nearest_on_segment(m_ict: np.ndarray, tgt: np.ndarray, ped: np.ndarray
                        ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row: ``(t*, d_min, d0)``. ``d(t)`` = dE_ITP of the measured ICtCp ``m_ict`` against
    ``tgt + t * ped``, ``t in [0, 1]``; ``t*`` minimises it (grid, then golden section in the bracketing cells);
    ``d0 = d(0)`` (the target itself)."""
    from .engine.model import TargetSpace, de_itp

    n = len(tgt)

    def dist(t: np.ndarray) -> np.ndarray:
        return de_itp(TargetSpace.xyz_to_ictcp(tgt + t[:, None] * ped[None, :]) - m_ict)

    ts = np.linspace(0.0, 1.0, SEGMENT_GRID)
    pts = (tgt[:, None, :] + ts[None, :, None] * ped[None, None, :]).reshape(-1, 3)
    d = de_itp(TargetSpace.xyz_to_ictcp(pts).reshape(n, SEGMENT_GRID, 3) - m_ict[:, None, :])
    k = np.argmin(d, axis=1)
    rows = np.arange(n)
    best_t, best_d = ts[k], d[rows, k]
    a = ts[np.maximum(k - 1, 0)]
    b = ts[np.minimum(k + 1, SEGMENT_GRID - 1)]
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    c, e = b - gr * (b - a), a + gr * (b - a)
    fc, fe = dist(c), dist(e)
    for _ in range(_GOLDEN_ITERS):
        left = fc < fe                       # the minimum lies in [a, e]: drop (e, b]
        b = np.where(left, e, b)
        a = np.where(left, a, c)
        nc, ne = np.where(left, c, e), np.where(left, fc, fe)   # the surviving interior point and its value
        x = np.where(left, b - gr * (b - a), a + gr * (b - a))
        fx = dist(x)
        c, fc = np.where(left, x, nc), np.where(left, fx, ne)
        e, fe = np.where(left, nc, x), np.where(left, ne, fx)
    t_gs = np.where(fc < fe, c, e)
    d_gs = np.minimum(fc, fe)
    better = d_gs < best_d
    return np.where(better, t_gs, best_t), np.where(better, d_gs, best_d), d[:, 0]


def black_aware_patch_scores(patch_metrics: Sequence[Any], floor: BlackFloor, *,
                             white_xy: Optional[Sequence[float]] = None) -> dict[str, Any]:
    """Per scored patch (HDR, dE_ITP), with ``floor`` available and positive:

    * ``e_black_aware``: dE_ITP against the nearest point of ``[target, target + F * w]`` (the module doc), the
      raw ``de2000`` bit for bit wherever the segment does not beat the target itself;
    * ``pedestal_y``: the pedestal luminance of that nearest point (``t* x F``);
    * ``floor_limited``: the descriptive class, PQ target Y below :data:`FLOOR_LIMITED_FACTOR` x the floor;
    * ``e_bt2390_band`` / ``e_toe_point``: the variants (floor-limited patches only, raw elsewhere), ``None``
      without a target peak; ``toe_y`` / ``band2390_y`` their luminances;
    * ``target_y`` / ``measured_y``; ``d0_vs_raw_max``: the largest |d(target) - raw| (a consistency check:
      the recorded raw error recomputed from the rows).

    ``white_xy`` (the run's target white) colours a black target in the BT.2390 variants. The measured XYZ is
    sanitised as :func:`dlc.engine.model.score_hdr` does (non-finite -> 0, negatives -> 0)."""
    if not floor.available or not floor.nits or floor.nits <= 0:
        raise ValueError("black_aware_patch_scores needs an available, positive floor")
    from .engine.model import TargetSpace, de_itp

    n = len(patch_metrics)
    raw = np.array([float(m.de2000) for m in patch_metrics], dtype=float)
    tgt = np.array([[float(c) for c in m.target_xyz] for m in patch_metrics], dtype=float).reshape(n, 3)
    meas = np.nan_to_num(np.array([[float(c) if c is not None else 0.0 for c in m.measured_xyz]
                                   for m in patch_metrics], dtype=float).reshape(n, 3),
                         nan=0.0, posinf=0.0, neginf=0.0)
    meas = np.maximum(meas, 0.0)
    f = float(floor.nits)
    ped_xy, _src = floor.pedestal
    ped = f * _xyz_unit(ped_xy)
    m_ict = TargetSpace.xyz_to_ictcp(meas)
    t_star, d_min, d0 = _nearest_on_segment(m_ict, tgt, ped)
    gain = d_min < d0 - 1e-9                  # the segment beats the target itself (beyond rounding)
    e_ba = np.where(gain, np.minimum(d_min, raw), raw)
    ty = np.maximum(tgt[:, 1], 0.0)
    limited = ty < FLOOR_LIMITED_FACTOR * f
    out: dict[str, Any] = {"floor_limited": limited, "target_y": ty, "measured_y": meas[:, 1],
                           "pedestal_y": np.where(gain, t_star, 0.0) * f, "e_black_aware": e_ba,
                           "d0_vs_raw_max": float(np.max(np.abs(d0 - raw))) if n else 0.0,
                           "e_toe_point": None, "e_bt2390_band": None, "toe_y": None, "band2390_y": None}
    if floor.peak_nits:
        toe_y = ty.copy()
        band_y = ty.copy()
        e_toe = raw.copy()
        e_band = raw.copy()
        if limited.any():
            idx = np.flatnonzero(limited)
            toe_y[idx] = bt2390_black_lift(ty[idx], min_nits=f, source_white_nits=float(floor.peak_nits))
            band_y[idx] = np.clip(meas[idx, 1], ty[idx], toe_y[idx])
            # The target's own chromaticity, scaled to the new luminance. A black target has none, so it takes
            # the run's white chromaticity.
            unit = np.where(ty[idx, None] > 0.0, tgt[idx] / np.where(ty[idx, None] > 0.0, ty[idx, None], 1.0),
                            _xyz_unit(white_xy if white_xy is not None else D65_XY)[None, :])
            mi = m_ict[idx]
            e_band[idx] = de_itp(mi - TargetSpace.xyz_to_ictcp(unit * band_y[idx, None]))
            e_toe[idx] = de_itp(mi - TargetSpace.xyz_to_ictcp(unit * toe_y[idx, None]))
        out.update({"e_toe_point": e_toe, "e_bt2390_band": e_band, "toe_y": toe_y, "band2390_y": band_y})
    return out
