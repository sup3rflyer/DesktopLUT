"""Physics-grounded convergence for the closed-loop MHC grayscale refine (HDR + SDR).

Replaces the fixed ``target_de`` stop (2.0 dE_ITP HDR / 0.5 CIEDE2000 SDR). The 2026-09-24
PA32UCXR run accepted round 1 because the grey average (1.26) sat under 2.0 — leaving a uniform
~0.001 x cool cast that one more round would have removed (the panel was stable to 0.0002 x and
the meter repeatable to ~0.0001). A constant cannot know that; the panel and the meter can. So
each round asks: **is there still error the refine can remove, and is removing it worth seeing?**

On the CORRECTABLE band — the levels the refine actually corrects (target luminance between the
build's measured dark floor and the Peak-Chroma cap; below/above are held by design) — per level:

1. **Physical floor** (what no refine round can beat), root-sum-square of:
   * meter repeatability — SE of the mean chromaticity from the loop's multi-read noise sidecar
     (pooled median for single-read levels; an ``unstable`` level is uncorrectable);
   * thermal wander between rounds — the run's own thermal-alignment reference track;
   * output quantization — a channel lands on a code of the panel's bit depth; the ±½-code
     rounding (σ = step/√12 per channel, independent) propagated through THIS panel's measured
     primaries at that level. At 10-bit PQ that alone is ~0.15 dE_ITP of luminance per level.
2. **Removable error** — each level's chroma / luminance error above its floor (a correction
   can shrink an error to the floor, never below it).
3. **Predicted gain of another round** — band mean ΔE now minus the band mean with every
   removable component taken down to its floor, discounted by the refine's MEASURED efficacy on
   this panel (realized ÷ predicted gain of the previous step). An optimistic floor (two-read σ
   under-states the true scatter) therefore self-corrects after one step instead of chasing noise.

Stop when the predicted gain is below a quarter of a JND (:data:`MATERIAL_GAIN_JND` — both
dE_ITP and CIEDE2000 put a just-noticeable difference at ~1): ``converged`` when what is left is
within the floor or imperceptible; ``floored`` when material removable error remains but the
last step itself realized an imperceptible gain — the refine demonstrably can't remove it;
``unjudged`` when the band holds no trustworthy grey. The last two are judgment calls the
orchestrator hands to the LLM as seams. The band's common-mode cast (the exact failure above)
is reported with its own significance so the LLM sees WHY. Everything the decision used rides
in the returned dict.

Spine-tier: stdlib only. The ΔE function is injected (dE_ITP for HDR, CIEDE2000 for SDR).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .colormath import invert3x3, matvec, xy_to_XYZ

__all__ = [
    "MATERIAL_GAIN_JND",
    "SIGNIFICANCE_K",
    "GreyLevel",
    "PanelFloor",
    "analyse_round",
    "channel_quantization",
    "panel_floor_from_thermal",
]

# A quarter of a just-noticeable difference, on the band MEAN. dE_ITP (BT.2124) and CIEDE2000
# both put a JND at ~1; a uniform grey error of 0.25 is invisible even side by side, so a round
# (~5 min of panel time) that is predicted to gain less than this buys nothing a viewer sees.
MATERIAL_GAIN_JND = 0.25

# Statistical significance of the band's common-mode cast / luminance gain: the conventional 3σ.
SIGNIFICANCE_K = 3.0

Vec3 = tuple[float, float, float]
DeFn = Callable[[Sequence[float], Sequence[float]], float]


@dataclass(frozen=True)
class GreyLevel:
    """One measured grey of a refine round, with this level's own noise terms."""

    signal: float
    measured_xyz: Vec3            # absolute (cd/m²)
    target_xyz: Vec3              # absolute (cd/m²) — the target white at the target luminance
    meter_se_xy: Optional[float] = None   # SE of the mean xy (per-read σ/√n); inf = unstable level
    meter_se_rel: Optional[float] = None  # meter luminance repeatability (relative), if known
    quant_xy: float = 0.0         # output-quantization σ in xy at this level
    quant_rel: float = 0.0        # output-quantization σ in relative luminance at this level


@dataclass(frozen=True)
class PanelFloor:
    """Panel stability between rounds (from the run's thermal-alignment reference track)."""

    drift_xy: float = 0.0         # white-point wander (xy)
    drift_rel: float = 0.0        # luminance wander (relative)
    source: Optional[str] = None


def _xy(xyz: Sequence[float]) -> Optional[tuple[float, float]]:
    t = float(xyz[0]) + float(xyz[1]) + float(xyz[2])
    if not (t > 0.0) or not math.isfinite(t):
        return None
    return float(xyz[0]) / t, float(xyz[1]) / t


def _median(vals: Sequence[float]) -> Optional[float]:
    v = sorted(vals)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m])


def _shrink(err: float, floor: float) -> float:
    """An error after a correction: it can be taken down to the floor, never below it."""
    if floor <= 0.0 or abs(err) <= floor:
        return err if floor > 0.0 else 0.0
    return math.copysign(floor, err)


def _r(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def channel_quantization(target_xyz: Sequence[float], channel_peak_xyz: Sequence[Sequence[float]],
                         rel_step: float) -> tuple[float, float]:
    """Output-quantization σ at one grey level: ``(σ_xy, σ_rel_luminance)``.

    ``rel_step`` is the relative light change of ONE output code at this level's drive
    (``d ln L / d code``; every channel of a near-neutral sits near the same code, so one value
    serves all three). Each channel rounds independently to its nearest code — uniform error,
    σ = step/√12 — and its light enters the grey in proportion to this panel's measured primary
    ``channel_peak_xyz[c]`` scaled to the level (``s = disp⁻¹ · target``). The per-channel XYZ
    perturbations are propagated to xy through the chromaticity Jacobian and summed in quadrature.
    """
    disp = [[float(channel_peak_xyz[c][row]) for c in range(3)] for row in range(3)]
    try:
        s = matvec(invert3x3(disp), [float(v) for v in target_xyz])
    except (ValueError, ZeroDivisionError):
        return 0.0, 0.0
    X, Y, Z = (float(v) for v in target_xyz)
    tot = X + Y + Z
    if not (tot > 0.0 and Y > 0.0):
        return 0.0, 0.0
    sigma = rel_step / math.sqrt(12.0)
    var_xy = 0.0
    var_rel = 0.0
    for c in range(3):
        share = max(0.0, s[c])
        dX, dY, dZ = (sigma * share * float(channel_peak_xyz[c][k]) for k in range(3))
        dt = dX + dY + dZ
        dx = (dX * tot - X * dt) / (tot * tot)
        dy = (dY * tot - Y * dt) / (tot * tot)
        var_xy += dx * dx + dy * dy
        var_rel += (dY / Y) ** 2
    return math.sqrt(var_xy), math.sqrt(var_rel)


def panel_floor_from_thermal(thermal_align: Optional[Mapping[str, Any]]) -> PanelFloor:
    """The between-rounds wander from the run's thermal-alignment evidence (``calib
    ["thermal_align"]``): the most recent stage with a reference track. The SETTLED wander
    (``tail_span_x``, the reference's spread once warm) is the floor a correction can't beat —
    the whole-stage span includes the warm-in the loop no longer sees. xy wander is taken as
    isotropic (the track is x-primary); luminance wander from the track's start/end nits, scaled
    to the same settled fraction."""
    if not isinstance(thermal_align, Mapping):
        return PanelFloor()
    for stage in reversed(list(thermal_align)):
        rec = thermal_align.get(stage)
        track = ((rec or {}).get("evidence") or {}).get("track") if isinstance(rec, Mapping) else None
        if not isinstance(track, Mapping):
            continue
        dxy = track.get("tail_span_x")
        if dxy is None:
            dxy = track.get("span_x")
        lum = track.get("luminance_nits")
        drel = 0.0
        if isinstance(lum, Sequence) and len(lum) >= 2 and lum[0] and lum[-1]:
            try:
                drel = abs(float(lum[-1]) / float(lum[0]) - 1.0)
            except (TypeError, ValueError, ZeroDivisionError):
                drel = 0.0
            # The track keeps only its luminance ENDPOINTS, so their ratio includes the warm-in
            # the refine no longer sees (09-23: 0.84 % over the stage vs a settled x tail of 8 %
            # of the x span). Scale it by the chroma track's settled fraction (tail ÷ whole span).
            try:
                tail, span = track.get("tail_span_x"), track.get("span_x")
                if tail is not None and span and float(span) > 0.0:
                    drel *= min(1.0, float(tail) / float(span))
            except (TypeError, ValueError):
                pass
        try:
            dxy_f = max(0.0, float(dxy)) if dxy is not None else 0.0
        except (TypeError, ValueError):
            dxy_f = 0.0
        return PanelFloor(drift_xy=dxy_f, drift_rel=drel, source=f"thermal_align:{stage}")
    return PanelFloor()


def analyse_round(levels: Sequence[GreyLevel], *, de_fn: DeFn, floor: PanelFloor,
                  previous: Optional[Mapping[str, Any]] = None,
                  materiality: float = MATERIAL_GAIN_JND,
                  k_sigma: float = SIGNIFICANCE_K) -> dict[str, Any]:
    """Judge one refine round on its correctable band (see the module docstring).

    ``levels`` are the round's greys ALREADY restricted to the correctable band. ``previous`` is
    this function's result for the previous round (a refine step was applied in between) — its
    ``band_avg`` / ``raw_gain`` measure the step's efficacy. Returns the evidence + ``decision``
    (``continue`` | ``converged`` | ``floored``) and a one-line ``reason``."""
    rows: list[dict[str, Any]] = []
    for lv in levels:
        mxy = _xy(lv.measured_xyz)
        txy = _xy(lv.target_xyz)
        if mxy is None or txy is None or not (lv.target_xyz[1] > 0.0):
            continue
        rows.append({"lv": lv, "mxy": mxy, "txy": txy,
                     "ex": mxy[0] - txy[0], "ey": mxy[1] - txy[1],
                     "el": float(lv.measured_xyz[1]) / float(lv.target_xyz[1]) - 1.0,
                     "de": float(de_fn(lv.measured_xyz, lv.target_xyz))})
    out: dict[str, Any] = {"band_n": len(rows), "materiality": materiality}
    if not rows:
        # No evidence is not convergence — the orchestrator hands 'unjudged' to the LLM.
        out.update(decision="unjudged", reason="no measurable grey in the correctable band",
                   band_avg=None)
        return out

    known = [r["lv"].meter_se_xy for r in rows
             if r["lv"].meter_se_xy is not None and math.isfinite(r["lv"].meter_se_xy)]
    pooled_se = _median(known) or 0.0
    known_rel = [r["lv"].meter_se_rel for r in rows if r["lv"].meter_se_rel is not None]
    pooled_rel = _median(known_rel) or 0.0

    unstable = 0
    for r in rows:
        lv = r["lv"]
        se = lv.meter_se_xy if lv.meter_se_xy is not None else pooled_se
        if not math.isfinite(se):
            unstable += 1            # the loop couldn't pin it — its error is not removable
        se_rel = lv.meter_se_rel if lv.meter_se_rel is not None else pooled_rel
        r["floor_xy"] = math.sqrt(se * se + floor.drift_xy ** 2 + lv.quant_xy ** 2) \
            if math.isfinite(se) else math.inf
        r["floor_rel"] = math.sqrt(se_rel ** 2 + floor.drift_rel ** 2 + lv.quant_rel ** 2)

    # -- the common-mode cast + luminance gain (precision-weighted), with their significance --
    fin = [r for r in rows if math.isfinite(r["floor_xy"])]

    def _common(key: str, fkey: str) -> tuple[Optional[float], Optional[float]]:
        if not fin:
            return None, None
        w = [1.0 / max(r[fkey], 1e-9) ** 2 for r in fin]
        sw = sum(w)
        mean = sum(wi * r[key] for wi, r in zip(w, fin)) / sw
        se_model = 1.0 / math.sqrt(sw)
        # Empirical: the level-to-level scatter about the mean, as the SE of a weighted mean
        # (Kish effective n). The larger of the two — the scatter carries every per-level term
        # the model can't see (FALD zone state, cube interpolation, meter linearity).
        n_eff = sw * sw / sum(wi * wi for wi in w)
        var = sum(wi * (r[key] - mean) ** 2 for wi, r in zip(w, fin)) / sw
        se_emp = math.sqrt(var / n_eff) if n_eff > 1.0 else 0.0
        return mean, max(se_model, se_emp)

    cx, cse_x = _common("ex", "floor_xy")
    cy, cse_y = _common("ey", "floor_xy")
    gl, gse = _common("el", "floor_rel")
    cast = math.hypot(cx, cy) if cx is not None else None
    cast_se = math.hypot(cse_x or 0.0, cse_y or 0.0) / math.sqrt(2.0) if cx is not None else None
    cast_real = bool(cast is not None and cast > max(k_sigma * (cast_se or 0.0), floor.drift_xy))
    gain_real = bool(gl is not None and abs(gl) > max(k_sigma * (gse or 0.0), floor.drift_rel))

    # -- what one more round could remove: every error taken down to its own floor --
    after = []
    for r in rows:
        lv = r["lv"]
        if math.isfinite(r["floor_xy"]):
            e = math.hypot(r["ex"], r["ey"])
            scale = 1.0 if e <= r["floor_xy"] or e == 0.0 else r["floor_xy"] / e
            ax, ay = r["txy"][0] + r["ex"] * scale, r["txy"][1] + r["ey"] * scale
            al = _shrink(r["el"], r["floor_rel"])
            y_after = float(lv.target_xyz[1]) * (1.0 + al)
            pred = xy_to_XYZ(ax, ay, y_after)
            after.append(float(de_fn(pred, lv.target_xyz)))
        else:
            after.append(r["de"])    # an unstable level keeps its error
    band_avg = sum(r["de"] for r in rows) / len(rows)
    after_avg = sum(after) / len(after)
    raw_gain = max(0.0, band_avg - after_avg)

    efficacy = 1.0
    realized = None
    if previous and previous.get("band_avg") is not None and previous.get("raw_gain"):
        realized = float(previous["band_avg"]) - band_avg
        efficacy = min(1.0, max(0.0, realized / float(previous["raw_gain"])))
    predicted_gain = raw_gain * efficacy

    if not fin:
        # Every level was flagged unstable: nothing is judgeable, so nothing is "converged".
        decision = "unjudged"
        reason = f"all {len(rows)} greys in the correctable band are unstable (not pinnable)"
    elif predicted_gain >= materiality:
        decision = "continue"
        reason = (f"another round is predicted to gain {predicted_gain:.2f} "
                  f"(≥ {materiality:g}): removable error above the panel floor"
                  + (f", common-mode cast {cast:.4f} xy at {cast / cast_se:.0f}σ"
                     if cast_real and cast_se else ""))
    elif raw_gain >= materiality and realized is not None and realized < materiality:
        # The damped refine's efficacy is < 1 by design, so a low PREDICTION alone is not a
        # floor — only a step whose own realized gain was imperceptible while material,
        # removable error remains says the refine can't move this panel.
        decision = "floored"
        reason = (f"{raw_gain:.2f} of removable error remains above the physical floor, but the "
                  f"last step realized only {realized:.2f} ({efficacy:.0%} of its prediction) — "
                  "the refine cannot take this panel further (a floor the model doesn't see)")
    else:
        decision = "converged"
        reason = (f"what remains is within the panel's physical floor or immaterial "
                  f"(predicted gain {predicted_gain:.2f} < {materiality:g})")

    se_known = [r["lv"].meter_se_xy for r in rows if r["lv"].meter_se_xy is not None]
    out.update({
        "band_avg": round(band_avg, 3),
        "band_max": round(max(r["de"] for r in rows), 3),
        "predicted_after_avg": round(after_avg, 3),
        "raw_gain": round(raw_gain, 3),
        "efficacy": round(efficacy, 3),
        "realized_gain": _r(realized, 3),
        "predicted_gain": round(predicted_gain, 3),
        "cast_xy": [_r(cx, 5), _r(cy, 5)] if cx is not None else None,
        "cast_se_xy": _r(cast_se, 6),
        "cast_sigma": (round(cast / cast_se, 1) if cast is not None and cast_se else None),
        "cast_real": cast_real,
        "lum_gain": _r(gl, 5),
        "lum_gain_se": _r(gse, 6),
        "lum_gain_real": gain_real,
        "floor": {
            "meter_se_xy_median": _r(_median([s for s in se_known if math.isfinite(s)]), 6),
            "drift_xy": _r(floor.drift_xy, 6),
            "drift_rel": _r(floor.drift_rel, 5),
            "quant_xy_median": _r(_median([r["lv"].quant_xy for r in rows]), 6),
            "quant_rel_median": _r(_median([r["lv"].quant_rel for r in rows]), 5),
            "floor_xy_median": _r(_median([r["floor_xy"] for r in rows
                                           if math.isfinite(r["floor_xy"])]), 6),
            "source": floor.source,
        },
        "unstable_levels": unstable,
        "decision": decision,
        "reason": reason,
    })
    return out
