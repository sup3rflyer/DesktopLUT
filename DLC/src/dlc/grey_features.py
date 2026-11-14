"""Narrow features in a measured grey ramp — what the MHC base 1D LUT may correct, and where to look closer.

The base 1D LUT (:func:`dlc.mhc_cube.build_sdr_cube` / :func:`~dlc.mhc_cube.build_hdr_cube`) inverts each
channel's measured grey share with a PIECEWISE-LINEAR interpolant between the measured grey levels. That
is exact AT the levels and a guess BETWEEN them. Where the panel's response carries structure narrower
than the measurement spacing, the guess is wrong, and the LUT bakes it in (PA32UCXR SDR, 2026-09-25 raw
ramp: grey 157 read +1 % and grey 165 -4.5 % against the 132..198 trend — repeatable across three months
of raw ramps, so a real panel feature — and the two straight segments through those 8-code-apart levels
put a +1.6 % drive bump into entries 149-157 whose shape no measurement supports; the channels' bumps
also land a few entries apart, which is what tinted grey 153 green).

This module decides, from the data alone, which measured levels carry a deviation their neighbours do
not predict (a NARROW feature) and what the build may do with it:

* **Leave-one-out envelope** (:func:`flag_series`): every eligible level is predicted from its unflagged
  neighbours (a local quadratic through up to two levels each side, linear when fewer); a level is
  flagged when it misses that prediction by more than ``z`` × its measured noise plus the model's own
  curvature uncertainty (how far the quadratic and the linear prediction disagree there). The worst
  level is flagged first and excluded from its neighbours' predictions, so two adjacent features (157
  high, 165 low) do not hide each other. Luminance and the two channel-balance ratios are judged
  SEPARATELY: a luminance feature does not license a per-channel correction, and vice versa.
* **Resolution** (:func:`resolution_codes`): the narrowest correction the LUT can place for all three
  channels at that level — two LUT entries, plus how far apart the matrix puts the channels' LUT
  indices for one wire grey (rowsums), plus how far apart the build drives the channels there (their
  share imbalance). A flagged level whose neighbours lie FURTHER away than that is UNRESOLVED: more
  measurement can resolve it (:func:`densification_plan`); one whose neighbours already lie that close
  is narrower than anything the LUT can place.
* **The model** (:func:`model_grey_samples`): a flagged level's luminance and/or balance is replaced by
  the envelope prediction; every other level passes through BIT-IDENTICAL, so a smooth panel builds
  exactly the LUT it built before. A feature the data resolves (densely measured, shared by its
  neighbours) is not flagged and is corrected as measured.
* **Densification + repeat** (:func:`densification_plan` / :func:`merge_dense_greys`): an unresolved
  feature asks for greys across the span between its neighbours at the resolution step, with the
  bracketing and the flagged levels themselves RE-READ (anchors) — the re-read is the outlier check
  (a level that does not reproduce is a transient, dropped; one that does is a confirmed feature) and
  the anchors put the dense pass on the raw pass's luminance scale (drift between passes).

Stdlib only (the spine imports it without the engine extra)."""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Optional, Sequence

from .colormath import invert3x3, matvec, rgb_to_xyz_matrix

__all__ = [
    "DEFAULT_Z",
    "MIN_RESOLUTION_CODES",
    "DEFAULT_LUMINANCE_SIGMA",
    "flag_series",
    "resolution_codes",
    "model_grey_samples",
    "densification_plan",
    "merge_dense_greys",
    "luminance_repeatability",
    "held_out_codes",
    "grey_series",
    "held_out_check",
]

# A level is a feature when it misses its neighbours' prediction by more than DEFAULT_Z standard
# deviations of the combined (level + prediction) noise — the same z the DIP read policy uses for an
# outlier vs expectation (4 σ: a false flag per ~16 000 levels from noise alone).
DEFAULT_Z = 4.0
# Two LUT entries: the narrowest correction a 1D LUT with one entry per code can shape at all (one
# entry is a step, two make a slope). Format fact for a 1024-entry SDR LUT on 10-bit codes.
MIN_RESOLUTION_CODES = 2.0
# Relative luminance repeatability used ONLY when the run carries no evidence of its own (no ndjson
# beside the ti3, no repeat reads, no drift reference): 0.5 %, the i1Display-class colorimeter's
# published short-term repeatability at mid-grey levels. Any measured value replaces it.
DEFAULT_LUMINANCE_SIGMA = 0.005
# The relative noise floor no measurement can claim to beat: the ti3's 6-decimal print step on a
# 1-nit level is 1e-6; 1e-4 is the float/print round-trip slack of a whole ramp (ti3 percent ->
# signal -> code), so identical synthetic reads are never judged "noise-free" below it.
_SIGMA_FLOOR = 1e-4


# ---------------------------------------------------------------------------------------------
# leave-one-out envelope on one series
# ---------------------------------------------------------------------------------------------

def _solve3(a: list[list[float]], b: list[float]) -> Optional[list[float]]:
    try:
        inv = invert3x3(a)
    except (ValueError, ZeroDivisionError):
        return None
    out = matvec(inv, b)
    return out if all(math.isfinite(v) for v in out) else None


def _predict(xs: Sequence[float], vs: Sequence[float], i: int, usable: Sequence[bool],
             window: int) -> Optional[tuple[float, float, list[int]]]:
    """Leave-one-out prediction of ``vs[i]`` from up to ``window`` usable neighbours each side.

    Returns ``(quadratic_or_best, linear, neighbours)`` — ``linear`` = the straight line through the
    nearest usable neighbour on each side; ``quadratic`` = the least-squares quadratic (centred on
    ``xs[i]``) through all chosen neighbours when there are >= 3 of them, else the linear value.
    ``None`` when a side has no usable neighbour (an end level cannot be judged)."""
    n = len(xs)
    left = [k for k in range(i - 1, -1, -1) if usable[k]][:window]
    right = [k for k in range(i + 1, n) if usable[k]][:window]
    if not left or not right:
        return None
    x0 = xs[i]
    l0, r0 = left[0], right[0]
    dx = xs[r0] - xs[l0]
    t = (x0 - xs[l0]) / dx if dx > 0 else 0.5
    lin = vs[l0] + (vs[r0] - vs[l0]) * t
    pts = left + right
    if len(pts) < 3:
        return lin, lin, pts
    s = [[0.0] * 3 for _ in range(3)]
    rhs = [0.0, 0.0, 0.0]
    scale = max(abs(xs[k] - x0) for k in pts) or 1.0     # unit-scaled abscissa: well-conditioned
    for k in pts:
        u = (xs[k] - x0) / scale
        basis = (1.0, u, u * u)
        for a in range(3):
            rhs[a] += basis[a] * vs[k]
            for b in range(3):
                s[a][b] += basis[a] * basis[b]
    coef = _solve3(s, rhs)
    quad = coef[0] if coef is not None else lin
    return quad, lin, pts


def flag_series(xs: Sequence[float], vs: Sequence[float], sigmas: Sequence[float],
                eligible: Sequence[bool], *, z: float = DEFAULT_Z, window: int = 2,
                exempt: Optional[Sequence[bool]] = None) -> list[dict[str, Any]]:
    """Judge every eligible level of one series against its neighbours (leave-one-out).

    ``xs`` ascending (signal), ``vs`` the series (e.g. ``ln(Y / eotf(s))``), ``sigmas`` each level's
    1-σ noise in the series' units, ``eligible`` which levels take part (ineligible levels are neither
    judged nor used as neighbours). Iterative: the level that misses its prediction by the most
    tolerances is flagged and excluded from every other level's neighbours, then all are re-judged,
    until no unflagged level misses. ``tolerance = z·sqrt(σ_i² + σ_pred²) + |quadratic − linear|``:
    the second term is the local model's own uncertainty (it is large where the curve bends within the
    window, small on a straight run), so a smoothly curving panel is never flagged for its curvature.

    ``exempt`` levels are judged (reported) but never flagged, and stay usable as neighbours: the
    caller's RESOLVED levels (sampled at or finer than the LUT's resolution — what they show is what
    the LUT can place, so there is no envelope to prefer over them).

    Returns one dict per level: ``{"index", "x", "value", "predicted", "residual", "tolerance",
    "flagged", "judged"}`` (``predicted``/``residual``/``tolerance`` ``None`` for an unjudged level)."""
    n = len(xs)
    flagged = [False] * n
    elig = [bool(e) for e in eligible]
    ex = [bool(e) for e in exempt] if exempt is not None else [False] * n

    # The panel's own structure AT the sampling scale: real panels are not smooth at every scale (the
    # PA32UCXR's native grey wanders ~0.5 % from level to level, repeatably), and a level that only
    # does what most levels do is not a narrow feature — it is the resolution of this measurement.
    # Its scale is the robust spread (1.4826 × median |residual|) of the first-pass leave-one-out
    # residuals of ALL judged levels; a narrow feature is an outlier against it (and against the read
    # noise, whichever is larger). Fixed for the run, so excluding a flagged level cannot cascade.
    first = []
    for i in range(n):
        if not elig[i]:
            continue
        pr = _predict(xs, vs, i, [elig[k] and k != i for k in range(n)], window)
        if pr is not None:
            first.append(abs(vs[i] - pr[0]))
    structure = 1.4826 * _median_list(first) if first else 0.0

    def judge(i: int) -> Optional[tuple[float, float, float]]:
        usable = [elig[k] and not flagged[k] and k != i for k in range(n)]
        pr = _predict(xs, vs, i, usable, window)
        if pr is None:
            return None
        quad, lin, pts = pr
        s_pred = math.sqrt(sum(sigmas[k] ** 2 for k in pts) / len(pts))
        noise = math.sqrt(sigmas[i] ** 2 + s_pred ** 2)
        tol = z * max(noise, structure) + abs(quad - lin)
        return quad, vs[i] - quad, tol

    for _round in range(n):
        worst, worst_ratio = None, 1.0
        for i in range(n):
            if not elig[i] or flagged[i] or ex[i]:
                continue
            j = judge(i)
            if j is None:
                continue
            _p, res, tol = j
            ratio = abs(res) / tol if tol > 0 else (math.inf if res else 0.0)
            if ratio > worst_ratio:
                worst, worst_ratio = i, ratio
        if worst is None:
            break
        flagged[worst] = True

    out = []
    for i in range(n):
        j = judge(i) if elig[i] else None
        out.append({
            "index": i, "x": xs[i], "value": vs[i],
            "predicted": None if j is None else j[0],
            "residual": None if j is None else j[1],
            "tolerance": None if j is None else j[2],
            "flagged": flagged[i], "judged": j is not None, "structure": structure,
        })
    return out


def _local_quad_at(xs: Sequence[float], vs: Sequence[float], ws: Sequence[float], pts: Sequence[int],
                   x0: float) -> Optional[float]:
    s = [[0.0] * 3 for _ in range(3)]
    rhs = [0.0, 0.0, 0.0]
    scale = max((abs(xs[k] - x0) for k in pts), default=1.0) or 1.0
    for k in pts:
        if ws[k] <= 0.0:
            continue
        u = (xs[k] - x0) / scale
        basis = (1.0, u, u * u)
        for a in range(3):
            rhs[a] += ws[k] * basis[a] * vs[k]
            for b in range(3):
                s[a][b] += ws[k] * basis[a] * basis[b]
    coef = _solve3(s, rhs)
    return coef[0] if coef is not None else None


def smooth_series(xs: Sequence[float], vs: Sequence[float], eligible: Sequence[bool], *,
                  scale: float, window: int = 3) -> list[Optional[float]]:
    """A robust local-quadratic SMOOTH of one series through every eligible level: at each level a
    least-squares quadratic through it and up to ``window`` eligible levels on each side (seven levels:
    a single level's phase of a sub-sampling structure is averaged with six others), then once more
    with bisquare weights (``6 × scale``, ``scale`` = the structure's robust σ) so a level far outside
    the structure — a glitch — does not pull its neighbours. ``None`` for ineligible levels."""
    n = len(xs)
    idx = [i for i in range(n) if eligible[i]]
    w1 = [1.0] * n

    def fit(i: int, w: Sequence[float]) -> Optional[float]:
        pos = idx.index(i)
        pts = idx[max(0, pos - window): pos + window + 1]
        if len(pts) < 3:
            return vs[i]
        v = _local_quad_at(xs, vs, w, pts, xs[i])
        return v if v is not None else vs[i]

    first = {i: fit(i, w1) for i in idx}
    s6 = 6.0 * max(scale, 1e-12)
    w2 = [1.0] * n
    for i in idx:
        u = (vs[i] - first[i]) / s6
        w2[i] = (1.0 - u * u) ** 2 if abs(u) < 1.0 else 0.0
    out: list[Optional[float]] = [None] * n
    for i in idx:
        out[i] = fit(i, w2)
    return out


# ---------------------------------------------------------------------------------------------
# resolution: the narrowest correction the LUT can place for all three channels
# ---------------------------------------------------------------------------------------------

def _local_gamma(eotf: Callable[[float], float], s: float) -> float:
    """d ln eotf / d ln s at ``s`` (2.2 for a pure power law; varies along PQ)."""
    h = max(1e-4, 1e-3 * s)
    lo, hi = max(s - h, 1e-9), s + h
    e0, e1 = eotf(lo), eotf(hi)
    if e0 <= 0.0 or e1 <= 0.0:
        return 2.2
    return max(0.1, (math.log(e1) - math.log(e0)) / (math.log(hi) - math.log(lo)))


def resolution_codes(signal: float, shares: Sequence[float], *, max_cv: int,
                     eotf: Callable[[float], float],
                     index_scale: Optional[Callable[[int, float], float]] = None) -> float:
    """The narrowest base-LUT correction (in codes) that lands in the same place for all three channels
    at grey ``signal``: :data:`MIN_RESOLUTION_CODES` + the matrix's channel registration (how far apart
    ``index_scale(c, s)`` puts the channels' LUT index for one wire grey; DesktopLUT applies the LUT
    AFTER the MHC2 matrix) + the build's drive spread (the channels reach the same target at drives that
    differ by their share imbalance at that level, ``(share_c / share_c')^(1/γ_local)``). Data-derived:
    a balanced panel with an identity matrix resolves to two codes."""
    s = max(float(signal), 1e-9)
    reg = 0.0
    if index_scale is not None:
        idx = [index_scale(c, s) for c in range(3)]
        reg = (max(idx) - min(idx)) * max_cv
    spread = 0.0
    pos = [v for v in shares if v > 0.0]
    if len(pos) == 3:
        g = _local_gamma(eotf, s)
        lo, hi = min(pos), max(pos)
        spread = s * ((hi / lo) ** (1.0 / g) - 1.0) * max_cv
    return MIN_RESOLUTION_CODES + reg + spread


# ---------------------------------------------------------------------------------------------
# the model the build consumes
# ---------------------------------------------------------------------------------------------

def _is_grey(rgb: Sequence[float], eps: float = 1e-4) -> bool:
    return abs(rgb[0] - rgb[1]) < eps and abs(rgb[1] - rgb[2]) < eps


def model_grey_samples(samples: Sequence[Any], primaries: Mapping[str, float],
                       white_xy: tuple[float, float], peak_luminance: float, *,
                       eotf: Callable[[float], float], max_cv: int,
                       y_floor: float = 0.0, signal_ceiling: Optional[float] = None,
                       luminance_sigma: float = DEFAULT_LUMINANCE_SIGMA,
                       chroma_sigma: Optional[Mapping[float, float]] = None,
                       index_scale: Optional[Callable[[int, float], float]] = None,
                       z: float = DEFAULT_Z, sample_factory: Optional[Callable[..., Any]] = None,
                       basis: Optional[Sequence[Sequence[float]]] = None
                       ) -> tuple[list[Any], dict[str, Any]]:
    """Return ``(samples', report)``: the raw samples with every flagged grey level's luminance and/or
    channel balance replaced by its neighbours' envelope; all other samples (colours, unflagged greys)
    are the SAME objects, so a panel with no narrow feature builds a bit-identical LUT.

    ``samples``: ``Ti3Sample``-like (``.rgb`` in 0..1 signal, ``.xyz``). Greys are judged above
    ``y_floor`` nits (the build's dark floor: below it the LUT blends to identity anyway) and at or
    below ``signal_ceiling`` (HDR: the cube ceiling — the headroom extension and the clip plateau are
    not tone the base corrects). Three series per level: ``L = ln(Y / eotf(s))`` (luminance) and
    ``ln(r/g)``, ``ln(b/g)`` (balance, from the linear shares ``P⁻¹·XYZ`` the build inverts).
    ``luminance_sigma`` = relative Y repeatability (:func:`luminance_repeatability`); ``chroma_sigma`` =
    ``{signal: xy σ}`` per level when measured (the noise sidecar), else the luminance σ stands in
    (a balance ratio moves at most as much as the luminance of one channel). ``basis`` (optional): the
    3×3 channel→XYZ matrix to take the shares in instead of the one built from ``primaries`` /
    ``white_xy`` / ``peak_luminance`` (the HDR refine's measured full-drive primary columns)."""
    if basis is not None:
        P = [list(map(float, row)) for row in basis]
    else:
        P = rgb_to_xyz_matrix(primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
                              primaries["bx"], primaries["by"], white_xy[0], white_xy[1],
                              white_Y=peak_luminance)
    Pinv = invert3x3(P)
    grey_idx: dict[float, int] = {}
    for k, smp in enumerate(samples):
        if _is_grey(smp.rgb):
            grey_idx[float(smp.rgb[0])] = k        # last write wins (as mhc_cube._gray_shares)
    sigs = sorted(grey_idx)
    xs, L, rr, rb, elig, shares_at = [], [], [], [], [], []
    for s in sigs:
        xyz = samples[grey_idx[s]].xyz
        sh = matvec(Pinv, list(xyz))
        e = eotf(s) if s > 0 else 0.0
        ok = (s > 0 and e > 0 and xyz[1] > max(y_floor, 0.0) and all(v > 0 for v in sh)
              and (signal_ceiling is None or s <= signal_ceiling + 1e-12))
        xs.append(s)
        shares_at.append(sh)
        if ok:
            L.append(math.log(xyz[1] / e))
            rr.append(math.log(sh[0] / sh[1]))
            rb.append(math.log(sh[2] / sh[1]))
        else:
            L.append(0.0)
            rr.append(0.0)
            rb.append(0.0)
        elig.append(ok)
    lum_sig = [max(float(luminance_sigma), _SIGMA_FLOOR)] * len(xs)
    bal_sig = []
    for s in sigs:
        cs = (chroma_sigma or {}).get(s)
        # an xy σ moves a channel ratio by ~ σ / (the channel's chromaticity lever ≈ 0.1): convert
        # conservatively (x10), and never below the luminance σ (one channel's own light noise).
        bal_sig.append(max(lum_sig[0], 10.0 * float(cs)) if cs is not None and math.isfinite(cs)
                       else lum_sig[0])
    # Per level: the gap to its eligible neighbours vs the LUT's resolution there. A level sampled at
    # or finer than the resolution on both sides is RESOLVED — exempt from the envelope (what it shows
    # is placeable; a densified span lands here).
    nbr, gap_codes, res_codes, exempt = [], [], [], []
    for i, s in enumerate(xs):
        left = next((xs[k] for k in range(i - 1, -1, -1) if elig[k]), None)
        right = next((xs[k] for k in range(i + 1, len(xs)) if elig[k]), None)
        gap = max((s - left) if left is not None else 0.0, (right - s) if right is not None else 0.0)
        res = (resolution_codes(s, shares_at[i], max_cv=max_cv, eotf=eotf, index_scale=index_scale)
               if elig[i] else MIN_RESOLUTION_CODES)
        nbr.append((left, right))
        gap_codes.append(gap * max_cv)
        res_codes.append(res)
        exempt.append(bool(elig[i]) and gap * max_cv <= res + 1e-9)
    judged = {
        "luminance": flag_series(xs, L, lum_sig, elig, z=z, exempt=exempt),
        "balance_rg": flag_series(xs, rr, bal_sig, elig, z=z, exempt=exempt),
        "balance_bg": flag_series(xs, rb, bal_sig, elig, z=z, exempt=exempt),
    }
    # LIGHT SMOOTHING. When the light's level-to-level structure is far above the read noise, the panel
    # carries light structure FINER than the sampling (PA32UCXR SDR, dense read 2026-11-14: full-field
    # light is a sawtooth in the code, teeth of 2-4 % every 4-12 codes, chromaticity flat across them).
    # Each read then samples one PHASE of it: a per-level light correction is right at that code and
    # wrong by up to a tooth a few codes away, and two close levels on opposite phases make a steep
    # correction whose channels land entries apart (the matrix puts them ~1.3 entries apart) — the
    # tinted bump of the 2026-09-25 base. So the light is then fitted as a smooth curve through all
    # levels (teeth average out, nothing chases them); the channel BALANCE stays per level (it is
    # smooth, so per-level reads of it are sound). The brightest level is the target white's own
    # reference and is never moved. A panel whose structure is at the noise floor (no structure) keeps
    # its measured light exactly.
    lum_structure = judged["luminance"][0]["structure"] if judged["luminance"] else 0.0
    smooth_light = lum_structure > z * lum_sig[0] if lum_sig else False
    L_model = list(L)
    top = max((i for i in range(len(xs)) if elig[i]), default=None)
    if smooth_light:
        sm = smooth_series(xs, L, elig, scale=lum_structure)
        for i in range(len(xs)):
            if elig[i] and i != top and sm[i] is not None:
                L_model[i] = sm[i]
    out = list(samples)
    features = []
    max_adjust = 0.0
    for i, s in enumerate(xs):
        flags = {k: v[i] for k, v in judged.items() if v[i]["flagged"]
                 and not (smooth_light and k == "luminance")}
        light_moved = smooth_light and L_model[i] != L[i]
        if not flags and not light_moved:
            continue
        code = s * max_cv
        left, right = nbr[i]
        gap = gap_codes[i] / max_cv
        res = res_codes[i]
        if light_moved:
            max_adjust = max(max_adjust, abs(L_model[i] - L[i]))
        Lm = (L_model[i] if smooth_light else
              judged["luminance"][i]["predicted"] if "luminance" in flags else L[i])
        rrm = judged["balance_rg"][i]["predicted"] if "balance_rg" in flags else rr[i]
        rbm = judged["balance_bg"][i]["predicted"] if "balance_bg" in flags else rb[i]
        y_new = eotf(s) * math.exp(Lm)
        er, eb = math.exp(rrm), math.exp(rbm)
        g = y_new / (P[1][0] * er + P[1][1] + P[1][2] * eb)
        xyz_new = tuple(matvec(P, [er * g, g, eb * g]))
        smp = samples[grey_idx[s]]
        if sample_factory is not None:
            out[grey_idx[s]] = sample_factory(rgb=tuple(smp.rgb), xyz=xyz_new)
        else:
            out[grey_idx[s]] = type(smp)(rgb=tuple(smp.rgb), xyz=xyz_new)
        if not flags:
            continue                     # light smoothed only: not a feature (no densify, no envelope)
        features.append({
            "signal": round(s, 6), "code": round(code, 2),
            "series": sorted(flags),
            "luminance_residual_pct": (round(100.0 * (math.exp(judged["luminance"][i]["residual"]) - 1.0), 3)
                                       if judged["luminance"][i]["residual"] is not None else None),
            "rg_residual_pct": (round(100.0 * (math.exp(judged["balance_rg"][i]["residual"]) - 1.0), 3)
                                if judged["balance_rg"][i]["residual"] is not None else None),
            "bg_residual_pct": (round(100.0 * (math.exp(judged["balance_bg"][i]["residual"]) - 1.0), 3)
                                if judged["balance_bg"][i]["residual"] is not None else None),
            "neighbour_gap_codes": round(gap * max_cv, 2),
            "resolution_codes": round(res, 2),
            "resolved": gap * max_cv <= res,
            "left_signal": None if left is None else round(left, 6),
            "right_signal": None if right is None else round(right, 6),
            "action": "envelope",
        })
    report = {
        "schema": 1,
        "z": z,
        "luminance_sigma": round(lum_sig[0], 6) if lum_sig else None,
        "levels_judged": sum(1 for r in judged["luminance"] if r["judged"]),
        "structure_pct": {k: (round(100.0 * v[0]["structure"], 4) if v else None)
                          for k, v in judged.items()},
        "levels": len(xs),
        "resolved_levels": sum(1 for e in exempt if e),
        "light_smoothing": {"applied": bool(smooth_light),
                            "structure_pct": round(100.0 * lum_structure, 4),
                            "noise_pct": round(100.0 * lum_sig[0], 4) if lum_sig else None,
                            "max_adjust_pct": round(100.0 * (math.exp(max_adjust) - 1.0), 3)},
        "features": features,
        "unresolved": [f for f in features if not f["resolved"]],
    }
    return out, report


# ---------------------------------------------------------------------------------------------
# densification: where to measure more, and how to fold the dense pass back in
# ---------------------------------------------------------------------------------------------

def densification_plan(report: Mapping[str, Any], *, max_cv: int, existing_codes: Sequence[int],
                       max_reads: int, signal_ceiling: Optional[float] = None) -> dict[str, Any]:
    """Greys to measure so every UNRESOLVED feature of ``report`` (:func:`model_grey_samples`) becomes
    resolved: the codes strictly between the feature's eligible neighbours at HALF its resolution (>= 1
    code — every placeable width sampled at least twice), plus
    ANCHORS — the two neighbours and the flagged level itself, read again in the dense pass (the repeat
    is the outlier check, and the anchors carry the dense pass onto the raw pass's luminance scale).
    Overlapping spans merge (the finer step wins). When the total exceeds ``max_reads`` every span's
    step is widened by the same factor (``coarsened``) — never silently dropped; ``max_reads <= 0`` ⇒
    an empty plan. Returns ``{"spans": [...], "codes": [...], "anchors": [...], "reads", "coarsened"}``
    (``codes`` excludes the anchors; the measure pass reads the anchors at its start AND its end)."""
    existing = {int(c) for c in existing_codes}
    spans: list[dict[str, Any]] = []
    for f in report.get("unresolved") or ():
        lo_s, hi_s = f.get("left_signal"), f.get("right_signal")
        if lo_s is None or hi_s is None:
            continue
        lo, hi = int(round(lo_s * max_cv)), int(round(hi_s * max_cv))
        if signal_ceiling is not None:
            hi = min(hi, int(round(signal_ceiling * max_cv)))
        if hi - lo < 2:
            continue
        # Half the resolution (>= one code): a feature as narrow as the LUT can place is then sampled
        # at least twice across its width — resolved, not aliased.
        spans.append({"lo": lo, "hi": hi, "step": max(1.0, 0.5 * float(f["resolution_codes"])),
                      "flagged": [int(round(f["signal"] * max_cv))]})
    spans.sort(key=lambda d: d["lo"])
    merged: list[dict[str, Any]] = []
    for sp in spans:
        if merged and sp["lo"] < merged[-1]["hi"]:
            m = merged[-1]
            m["hi"] = max(m["hi"], sp["hi"])
            m["step"] = min(m["step"], sp["step"])
            m["flagged"] = sorted(set(m["flagged"]) | set(sp["flagged"]))
        else:
            merged.append(dict(sp))

    def codes_for(factor: float) -> tuple[list[int], list[int]]:
        cs: set[int] = set()
        an: set[int] = set()
        for m in merged:
            step = m["step"] * factor
            an.update((m["lo"], m["hi"], *m["flagged"]))
            k = 1
            while True:
                c = int(round(m["lo"] + k * step))
                if c >= m["hi"]:
                    break
                cs.add(c)
                k += 1
        an_sorted = sorted(an)
        return sorted(c for c in cs if c not in existing and c not in an), an_sorted

    if max_reads <= 0 or not merged:
        return {"spans": [], "codes": [], "anchors": [], "reads": 0, "coarsened": 1.0,
                "max_reads": max_reads}
    factor = 1.0
    codes, anchors = codes_for(factor)
    while len(codes) + 2 * len(anchors) > max_reads and factor < 1e3:
        factor *= 1.25
        codes, anchors = codes_for(factor)
    for m in merged:
        m["step"] = round(m["step"] * factor, 2)
    return {"spans": merged, "codes": codes, "anchors": anchors,
            "reads": len(codes) + 2 * len(anchors), "coarsened": round(factor, 4),
            "max_reads": max_reads}


def merge_dense_greys(raw: Sequence[Any], dense: Sequence[Any], *, max_cv: int,
                      luminance_sigma: float = DEFAULT_LUMINANCE_SIGMA, z: float = DEFAULT_Z,
                      y_floor: float = 0.0) -> tuple[list[Any], dict[str, Any]]:
    """Fold a densification pass (greys only are used) into the raw samples.

    Levels read in BOTH passes are the anchors. The dense pass is put on the raw pass's luminance
    scale by the median anchor ratio ``k = Y_raw / Y_dense`` (a whole-XYZ scale: chromaticity is
    untouched). Then each anchor's repeat is judged: when ``|ln(Y_raw / (k·Y_dense))|`` exceeds
    ``z·σ·√2`` the level did NOT reproduce — a transient — and BOTH reads of it are dropped (the dense
    neighbours carry the shape there); otherwise the raw read stays (a confirmed level). Dense levels
    read more than once are averaged. Non-grey dense rows are ignored. Returns ``(samples, info)``."""
    def code(s: Any) -> int:
        return int(round(float(s.rgb[0]) * max_cv))

    raw_grey = {code(s): s for s in raw if _is_grey(s.rgb)}
    dense_by: dict[int, list[Any]] = {}
    for s in dense:
        if _is_grey(s.rgb):
            dense_by.setdefault(code(s), []).append(s)
    dense_avg: dict[int, tuple[tuple[float, float, float], Sequence[float], int]] = {}
    for c, rows in dense_by.items():
        xyz = tuple(sum(r.xyz[k] for r in rows) / len(rows) for k in range(3))
        dense_avg[c] = (xyz, rows[0].rgb, len(rows))
    anchors = sorted(c for c in dense_avg if c in raw_grey
                     and raw_grey[c].xyz[1] > y_floor and dense_avg[c][0][1] > 0)
    ratios = [raw_grey[c].xyz[1] / dense_avg[c][0][1] for c in anchors]
    k = _median_list(ratios) if ratios else 1.0
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    tol = z * sig * math.sqrt(2.0)
    unstable, confirmed, anchor_rows = [], [], []
    for c, r in zip(anchors, ratios):
        dev = math.log(r / k) if r > 0 and k > 0 else math.inf
        anchor_rows.append({"code": c, "raw_over_dense_pct": round(100.0 * (r / k - 1.0), 3)})
        (unstable if abs(dev) > tol else confirmed).append(c)
    drop = set(unstable)
    out = [s for s in raw if not (_is_grey(s.rgb) and code(s) in drop)]
    added = 0
    for c, (xyz, rgb, _n) in sorted(dense_avg.items()):
        if c in raw_grey:
            continue                      # anchors: the raw read stays (or both are dropped above)
        typ = type(raw_grey[next(iter(raw_grey))]) if raw_grey else type(dense_by[c][0])
        out.append(typ(rgb=tuple(rgb), xyz=tuple(v * k for v in xyz)))
        added += 1
    spread = (max(ratios) / min(ratios) - 1.0) if len(ratios) >= 2 and min(ratios) > 0 else None
    info = {
        "scale_k": round(k, 6), "anchors": anchor_rows, "anchor_count": len(anchors),
        "confirmed_levels": confirmed, "unstable_levels": unstable,
        "anchor_ratio_spread_pct": round(100.0 * spread, 3) if spread is not None else None,
        "added_levels": added, "repeat_tolerance_pct": round(100.0 * (math.exp(tol) - 1.0), 3),
    }
    return out, info


def _median_list(vals: Sequence[float]) -> float:
    s = sorted(vals)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def luminance_repeatability(ndjson_rows: Sequence[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """The run's measured relative luminance repeatability from its own measure log rows: the
    consecutive repeat reads of one patch (``read_index`` 0/1/... of the same ``rgb``, accepted) give
    the read-to-read σ; the interleaved drift references (``role == "neutral_ref"``, one patch re-read
    through the pass) give the slower time-to-time σ. Returns ``{"sigma", "read_sigma", "drift_sigma",
    "pairs", "refs"}`` with ``sigma = sqrt(read² + drift²)`` — or ``None`` with no evidence at all.
    Robust: each σ is 1.4826 × the median absolute deviation (in ln units), so a single glitch does not
    widen the tolerance every feature is judged against."""
    groups: dict[tuple, list[float]] = {}
    refs: list[float] = []
    prev_key = None
    for d in ndjson_rows:
        rgb = d.get("rgb")
        nits = d.get("nits")
        if not rgb or nits is None or not isinstance(nits, (int, float)) or nits <= 0:
            continue
        if d.get("phase") not in (None, "main"):
            continue
        if d.get("role") == "neutral_ref":
            refs.append(math.log(float(nits)))
            prev_key = None
            continue
        key = (d.get("seq_patch") or d.get("label"), tuple(rgb))
        if d.get("read_index") in (0, None):
            prev_key = key
            groups.setdefault(key + (len(groups),), []).append(math.log(float(nits)))
        elif prev_key == key:
            last = list(groups)[-1]
            groups[last].append(math.log(float(nits)))
    devs = []
    for vals in groups.values():
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            devs.extend(abs(v - m) * math.sqrt(len(vals) / (len(vals) - 1)) for v in vals)
    read_sigma = 1.4826 * _median_list(devs) if devs else None
    drift_sigma = None
    if len(refs) >= 3:
        med = _median_list(refs)
        drift_sigma = 1.4826 * _median_list([abs(v - med) for v in refs])
    if read_sigma is None and drift_sigma is None:
        return None
    sigma = math.sqrt((read_sigma or 0.0) ** 2 + (drift_sigma or 0.0) ** 2)
    return {"sigma": sigma, "read_sigma": read_sigma, "drift_sigma": drift_sigma,
            "pairs": sum(1 for v in groups.values() if len(v) >= 2), "refs": len(refs)}


# ---------------------------------------------------------------------------------------------
# post-build self-check on greys the fit never saw
# ---------------------------------------------------------------------------------------------

def held_out_codes(fit_codes: Sequence[int], report: Mapping[str, Any], *, max_cv: int,
                   floor_code: int, ceiling_code: Optional[int] = None, uniform: int = 6,
                   max_points: int = 10) -> list[dict[str, int]]:
    """Held-out greys for the post-install self-check: the midpoint of the fit-level gap on each side of
    every feature the build modelled (where the envelope replaced data — exactly where the LUT is
    least supported), plus ``uniform`` midpoints spread over the rest of the range above ``floor_code``.
    Each entry ``{"code", "lo", "hi"}`` names the two FIT levels bracketing it (measured again in the
    same check pass, so the comparison is local and immune to a smooth whole-range error, which the
    closed-loop refine owns). At most ``max_points`` held-out codes."""
    fit = sorted({int(c) for c in fit_codes if c >= floor_code
                  and (ceiling_code is None or c <= ceiling_code)})
    if len(fit) < 2:
        return []
    gaps = [(fit[k], fit[k + 1]) for k in range(len(fit) - 1) if fit[k + 1] - fit[k] >= 2]
    chosen: list[tuple[int, int]] = []
    for f in report.get("features") or ():
        c = int(round(float(f["code"])))
        for lo, hi in gaps:
            if (hi == c or lo == c) and (lo, hi) not in chosen:
                chosen.append((lo, hi))
    rest = [g for g in gaps if g not in chosen]
    if rest and uniform > 0:
        n = min(uniform, len(rest))
        for k in range(n):
            g = rest[int(round((k + 0.5) * len(rest) / n - 0.5))]
            if g not in chosen:
                chosen.append(g)
    chosen = chosen[:max_points]
    return [{"code": (lo + hi) // 2, "lo": lo, "hi": hi} for lo, hi in sorted(chosen)]


def grey_series(xyz: Sequence[float], signal: float, *, eotf: Callable[[float], float],
                Pinv: Sequence[Sequence[float]]) -> Optional[tuple[float, float, float]]:
    """``(ln(Y/eotf(s)), ln(r/g), ln(b/g))`` of one grey read (shares ``P⁻¹·XYZ``); ``None`` when a
    term is not positive."""
    e = eotf(signal) if signal > 0 else 0.0
    sh = matvec(Pinv, list(xyz))
    if not (e > 0 and xyz[1] > 0 and all(v > 0 for v in sh)):
        return None
    return math.log(xyz[1] / e), math.log(sh[0] / sh[1]), math.log(sh[2] / sh[1])


def held_out_check(points: Sequence[Mapping[str, int]], reads: Mapping[int, Sequence[float]], *,
                   max_cv: int, eotf: Callable[[float], float], primaries: Mapping[str, float],
                   white_xy: tuple[float, float], peak_luminance: float,
                   luminance_sigma: float = DEFAULT_LUMINANCE_SIGMA,
                   structure_pct: Optional[Mapping[str, Optional[float]]] = None,
                   z: float = DEFAULT_Z) -> dict[str, Any]:
    """Judge each held-out grey against the straight line through its two bracketing fit levels, all
    read in the same pass through the installed base LUT — in the SAME three series the build judged
    (luminance ``ln(Y/eotf)``, balance ``ln(r/g)``, ``ln(b/g)``) and against the SAME scales: the read
    noise and the panel's structure at the raw sampling scale (``structure_pct`` from the build's
    report), whichever is larger, × ``z`` × √1.5 (the held-out read + the bracketing line). A miss
    beyond that is a narrow error the fit never saw. ``reads``: ``{code: XYZ}``. Returns
    ``{"points": [...], "out": [...], "max_luminance_pct", "max_balance_pct", "max_dxy", ...}`` —
    evidence for a seam, never a decision."""
    P = rgb_to_xyz_matrix(primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
                          primaries["bx"], primaries["by"], white_xy[0], white_xy[1],
                          white_Y=peak_luminance)
    Pinv = invert3x3(P)
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    st = structure_pct or {}
    names = ("luminance", "balance_rg", "balance_bg")

    def scale(name: str) -> float:
        v = st.get(name)
        return max(sig, (float(v) / 100.0) if v is not None else 0.0)

    tol = {name: z * scale(name) * math.sqrt(1.5) for name in names}
    rows, out = [], []
    max_l = max_b = max_c = 0.0

    def ser(c: int):
        xyz = reads.get(c)
        if xyz is None:
            return None
        v = grey_series(xyz, c / max_cv, eotf=eotf, Pinv=Pinv)
        if v is None:
            return None
        tot = sum(xyz)
        return v, (xyz[0] / tot, xyz[1] / tot)

    for p in points:
        h, lo, hi = ser(p["code"]), ser(p["lo"]), ser(p["hi"])
        if h is None or lo is None or hi is None:
            rows.append({**p, "status": "unread"})
            continue
        t = (p["code"] - p["lo"]) / (p["hi"] - p["lo"])
        res = [h[0][k] - (lo[0][k] + (hi[0][k] - lo[0][k]) * t) for k in range(3)]
        ex = lo[1][0] + (hi[1][0] - lo[1][0]) * t
        ey = lo[1][1] + (hi[1][1] - lo[1][1]) * t
        dxy = math.hypot(h[1][0] - ex, h[1][1] - ey)
        missed = [n for n, r in zip(names, res) if abs(r) > tol[n]]
        row = {**p, "luminance_pct": round(100.0 * (math.exp(res[0]) - 1.0), 3),
               "rg_pct": round(100.0 * (math.exp(res[1]) - 1.0), 3),
               "bg_pct": round(100.0 * (math.exp(res[2]) - 1.0), 3),
               "dx": round(h[1][0] - ex, 5), "dy": round(h[1][1] - ey, 5), "dxy": round(dxy, 5),
               "missed": missed, "status": "out" if missed else "ok"}
        rows.append(row)
        if missed:
            out.append(row)
        max_l = max(max_l, abs(row["luminance_pct"]))
        max_b = max(max_b, abs(row["rg_pct"]), abs(row["bg_pct"]))
        max_c = max(max_c, dxy)
    return {"points": rows, "out": out, "max_luminance_pct": round(max_l, 3),
            "max_balance_pct": round(max_b, 3), "max_dxy": round(max_c, 5),
            "tolerance_pct": {k: round(100.0 * (math.exp(v) - 1.0), 3) for k, v in tol.items()},
            "z": z}


