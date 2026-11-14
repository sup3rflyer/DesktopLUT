"""Corrections-tab grayscale + white-balance touch-up math.

This module is deliberately about DesktopLUT's user-facing Corrections
grayscale editor, not the MHC base/correction grayscale layers. The solver emits
an editor-shaped table: one common luminance slider plus per-channel RGB balance
sliders at each grey point. For wire compatibility, the composed RGB deviations
are also included; older DesktopLUT IPC builds can apply the same correction as
per-channel deviations even if they ignore the explicit luminance field.

Mode note (Phase 4 audit): the ``grayscale-wb`` flow runs this touch-up in BOTH
modes (the HDR editor patch set is linear-in-code across the active peak — see
``calibrate.build_grayscale_wb_set``), but the solver's internals are SDR-shaped:
the per-channel basis is a hardcoded sRGB matrix and the step exponent is the
power-γ law (``ratio**(damping/γ)``), while ``point_error`` scores in Lab/ΔE2000.
That is SAFE here because the loop is fully closed (every nudge is re-measured
and per-step capped at ``per_iteration_cap``, so the basis/exponent only shape
step SIZE, never the fixed point) — but the reported ``de2000`` is a Lab number
even on HDR, and whether DesktopLUT's live editor semantics match on HDR is a
C++-side contract question (Phase 9 touchpoint), not established here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist, median
from typing import Any, Iterable, Mapping, Optional, Sequence

from .colormath import XYZ_to_xy, clamp, invert3x3, matvec, rgb_to_xyz_matrix, xy_to_XYZ
from .metrics import delta_e2000, xyz_to_lab
from .mhc import white_xyz

_SRGB = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))

_CHANNELS = ("r", "g", "b")


@dataclass(frozen=True)
class GrayTouchupPatch:
    level: float
    measured_xyz: tuple[float, float, float]
    target_y: float


@dataclass(frozen=True)
class GrayTouchupConfig:
    white_xy: tuple[float, float] = (0.3127, 0.3290)
    gamma: float = 2.2
    damping: float = 0.85
    per_iteration_cap: float = 0.035
    max_abs_delta: float = 0.15
    warn_abs_delta: float = 0.07
    warn_luminance_delta: float = 0.05
    # 0.25 nit (P5 provenance, Phase 4 audit): the touch-up HOLDS a point below this rather than
    # correcting it (update_point returns held_dark), so the cost of a too-low floor is one wasted
    # nudge attempt — unlike the build/refine floors (0.3 fallback / adaptive), where a wrong dark
    # correction is BAKED into the foundation. A slightly more permissive floor is therefore safe,
    # and lets the editor's darkest slider still be tuned on a meter good to ~0.3 nit.
    dark_floor_nits: float = 0.25


def identity_payload(points: Sequence[float]) -> dict[str, Any]:
    pts = [float(p) for p in points]
    n = len(pts)
    return {
        "point_count": n,
        "points": pts,
        "luminance": [1.0] * n,
        "rgb": {ch: [1.0] * n for ch in _CHANNELS},
        "deviations": {ch: [1.0] * n for ch in _CHANNELS},
    }


def coerce_payload(payload: Mapping[str, Any] | None, points: Sequence[float]) -> dict[str, Any]:
    pts = [float(p) for p in points]
    n = len(pts)
    if not isinstance(payload, Mapping):
        return identity_payload(pts)
    raw_points = payload.get("points")
    if not isinstance(raw_points, Sequence) or len(raw_points) != n:
        return identity_payload(pts)

    def col(name: str, default: float = 1.0) -> list[float]:
        raw = payload.get(name)
        if isinstance(raw, Sequence) and len(raw) == n:
            return [float(v) for v in raw]
        return [default] * n

    luminance = col("luminance")
    rgb_obj = payload.get("rgb")
    if not isinstance(rgb_obj, Mapping):
        rgb_obj = payload.get("deviations")
    rgb: dict[str, list[float]] = {}
    for ch in _CHANNELS:
        raw = rgb_obj.get(ch) if isinstance(rgb_obj, Mapping) else None
        rgb[ch] = [float(v) for v in raw] if isinstance(raw, Sequence) and len(raw) == n else [1.0] * n
    return compose_payload(pts, luminance, rgb)


def compose_payload(points: Sequence[float], luminance: Sequence[float],
                    rgb: Mapping[str, Sequence[float]]) -> dict[str, Any]:
    pts = [float(p) for p in points]
    lum = [float(v) for v in luminance]
    n = len(pts)
    rgb_out = {ch: [float(v) for v in rgb.get(ch, [1.0] * n)] for ch in _CHANNELS}
    deviations = {
        ch: [float(lum[i] * rgb_out[ch][i]) for i in range(n)]
        for ch in _CHANNELS
    }
    return {
        "point_count": n,
        "points": pts,
        "luminance": lum,
        "rgb": rgb_out,
        "deviations": deviations,
    }


def update_point(payload: Mapping[str, Any], index: int, patch: GrayTouchupPatch,
                 config: GrayTouchupConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one measured patch to one editor point and return ``(payload, digest)``."""
    points = [float(p) for p in payload.get("points", [])]
    current = coerce_payload(payload, points)
    n = len(points)
    if index < 0 or index >= n:
        raise IndexError("touch-up point index out of range")

    before = point_error(patch, config)
    if patch.measured_xyz[1] < config.dark_floor_nits or patch.target_y < config.dark_floor_nits:
        digest = {**before, "held_dark": True, "index": index, "level": patch.level}
        return current, digest

    matrix = rgb_to_xyz_matrix(
        _SRGB[0][0], _SRGB[0][1],
        _SRGB[1][0], _SRGB[1][1],
        _SRGB[2][0], _SRGB[2][1],
        config.white_xy[0], config.white_xy[1],
        white_Y=max(patch.target_y, patch.measured_xyz[1], 1e-6),
    )
    inv = invert3x3(matrix)
    target_xyz = xy_to_XYZ(config.white_xy[0], config.white_xy[1], patch.target_y)
    measured_rgb = matvec(inv, patch.measured_xyz)
    target_rgb = matvec(inv, target_xyz)

    ratios: list[float] = []
    for measured, target in zip(measured_rgb, target_rgb):
        ratios.append(clamp(target / measured, 0.35, 2.5) if measured > 1e-8 else 1.0)
    steps = [ratio ** (config.damping / max(config.gamma, 1e-6)) for ratio in ratios]
    steps = [clamp(step, 1.0 - config.per_iteration_cap, 1.0 + config.per_iteration_cap)
             for step in steps]

    lum = list(current["luminance"])
    rgb = {ch: list(current["rgb"][ch]) for ch in _CHANNELS}
    y_step = clamp(
        (patch.target_y / patch.measured_xyz[1]) ** (config.damping / max(config.gamma, 1e-6))
        if patch.measured_xyz[1] > 1e-8 else 1.0,
        1.0 - config.per_iteration_cap,
        1.0 + config.per_iteration_cap,
    )
    lum[index] = clamp(lum[index] * y_step, 1.0 - config.max_abs_delta, 1.0 + config.max_abs_delta)
    for ch, step in zip(_CHANNELS, steps):
        balance_step = clamp(step / y_step if y_step > 1e-8 else step,
                             1.0 - config.per_iteration_cap, 1.0 + config.per_iteration_cap)
        rgb[ch][index] = clamp(
            rgb[ch][index] * balance_step,
            1.0 - config.max_abs_delta,
            1.0 + config.max_abs_delta,
        )

    updated = compose_payload(points, lum, rgb)
    for ch in _CHANNELS:
        composed = updated["deviations"][ch][index]
        clamped = clamp(composed, 1.0 - config.max_abs_delta, 1.0 + config.max_abs_delta)
        if abs(clamped - composed) > 1e-12:
            rgb[ch][index] = clamped / lum[index] if lum[index] > 1e-8 else 1.0
    updated = compose_payload(points, lum, rgb)
    composed = [updated["deviations"][ch][index] for ch in _CHANNELS]
    capped = any(abs(v - 1.0) >= config.max_abs_delta - 1e-9 for v in composed)
    warning = max(abs(v - 1.0) for v in composed) >= config.warn_abs_delta
    y_warning = abs(lum[index] - 1.0) >= config.warn_luminance_delta
    digest = {
        **before,
        "held_dark": False,
        "index": index,
        "level": round(patch.level, 6),
        "target_Y": round(patch.target_y, 5),
        "measured_rgb_basis": [round(v, 6) for v in measured_rgb],
        "target_rgb_basis": [round(v, 6) for v in target_rgb],
        "step": {ch: round(v, 6) for ch, v in zip(_CHANNELS, steps)},
        "luminance": round(lum[index], 6),
        "rgb": {ch: round(rgb[ch][index], 6) for ch in _CHANNELS},
        "deviations": {ch: round(updated["deviations"][ch][index], 6) for ch in _CHANNELS},
        "capped": capped,
        "large_correction": warning or y_warning,
        "large_luminance_correction": y_warning,
    }
    return updated, digest


def point_error(patch: GrayTouchupPatch, config: GrayTouchupConfig) -> dict[str, Any]:
    target = xy_to_XYZ(config.white_xy[0], config.white_xy[1], patch.target_y)
    ref_y = max(patch.target_y, patch.measured_xyz[1], 1e-6)
    ref_white = white_xyz(ref_y, config.white_xy[0], config.white_xy[1])
    lab_m = xyz_to_lab(patch.measured_xyz, ref_white)
    lab_t = xyz_to_lab(target, ref_white)
    mx, my = XYZ_to_xy(*patch.measured_xyz)
    chroma = (lab_m[1] ** 2 + lab_m[2] ** 2) ** 0.5
    return {
        "measured_xy": [round(mx, 6), round(my, 6)],
        "target_xy": [round(config.white_xy[0], 6), round(config.white_xy[1], 6)],
        "measured_Y": round(patch.measured_xyz[1], 5),
        "target_Y": round(patch.target_y, 5),
        "delta_Y": round(patch.measured_xyz[1] - patch.target_y, 5),
        "delta_L": round(lab_m[0] - lab_t[0], 5),
        "a": round(lab_m[1], 5),
        "b": round(lab_m[2], 5),
        "C": round(chroma, 5),
        "de2000": round(delta_e2000(lab_m, lab_t), 5),
    }


def summarize_errors(errors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    usable = [e for e in errors if e]
    if not usable:
        return {"n": 0}
    de = [float(e.get("de2000", 0.0)) for e in usable]
    chroma = [float(e.get("C", 0.0)) for e in usable]
    delta_l = [abs(float(e.get("delta_L", 0.0))) for e in usable]
    delta_y = [abs(float(e.get("delta_Y", 0.0))) for e in usable]
    return {
        "n": len(usable),
        # The touch-up flow is mode-shared but this module scores in Lab — label the units
        # so an HDR run's digest can never read these as dE_ITP (fable Phase 6, P2/P4 lead).
        "metric": "CIEDE2000",
        "avg_de2000": round(sum(de) / len(de), 5),
        "max_de2000": round(max(de), 5),
        "avg_C": round(sum(chroma) / len(chroma), 5),
        "max_C": round(max(chroma), 5),
        "avg_abs_delta_L": round(sum(delta_l) / len(delta_l), 5),
        "max_abs_delta_L": round(max(delta_l), 5),
        "avg_abs_delta_Y": round(sum(delta_y) / len(delta_y), 5),
        "max_abs_delta_Y": round(max(delta_y), 5),
    }


# ---------------------------------------------------------------------------
# Between the nodes (2026-11-14 hardware finding)
# ---------------------------------------------------------------------------
# The touch-up is fitted AT the 32 editor nodes, but DesktopLUT bakes a CURVE: between two nodes the
# output is interpolated (src/mhc.cpp ``EvalGrayscaleSDR_Channel`` / ``EvalGrayscaleHDR_Channel``):
#
#   SDR: u = sqrt(signal) * (n-1); sqrt(output) is LINEAR in u between the node values
#        sqrt(points[i] * dev[i]) = (i/(n-1)) * sqrt(g_i)    (points = t^2 at slot i, g_i the composed gain)
#   HDR: u = (pq / pqPeak) * (n-1); output is LINEAR in u between (i/(n-1)) * g_i
#
# so a node's value reaches every grey out to its two neighbours. On the PA32UCXR (2026-11-14) the
# base calibration has a feature ~9 codes wide exactly at node 12 (code 153); the node got a
# correction opposite to all its neighbours and the interpolation spread it over codes ~130-179
# (148 went from +0.5 % to -7.6 % light) while a verify that re-read only the 32 node codes scored
# "within quality". The helpers below are the evidence that closes that blind spot:
#   * heldout_code(...)        - where to measure between two nodes (the interpolation law's worst place)
#   * find_node_outliers(...)  - a solved node that disagrees with its neighbours (a narrow feature)
#   * apply_node_holds(...)    - put such a node on its neighbours' interpolation
#   * judge_held_out(...)      - before vs after at greys the fit never saw, against the run's own
#                                repeatability
# Every statistic is robust (median / MAD) and every critical value is a stated family-wise false-alarm
# rate (Bonferroni over the tests made), not a tuned constant.

# Family-wise false-alarm rate of each test family below (outlier nodes; regressed greys): with this
# rate a clean run raises a false flag at most ~1 run in 20 even with every node / grey tested.
FAMILY_ALPHA = 0.05
# MAD -> sigma for a normal population (1 / Phi^-1(3/4)).
MAD_TO_SIGMA = 1.4826
# A numeric guard for a robust scale of exactly 0 (a noiseless simulation; identical solved gains):
# 1e-4 is a 0.01 % gain / 1e-4 dE2000 - below anything a meter resolves, never a tuning knob (a real
# run's scales sit 10-100x above it).
SCALE_FLOOR = 1e-4
# The fewest samples a median/MAD scale is taken over (the eligible nodes of the outlier test; the
# nearest node pairs of the regression test's local null): with 8 the MAD's own relative standard
# error is ~1.17/sqrt(8) ~ 40 %, and it still has a 50 % breakdown point (3 contaminated values of 8
# do not move it). Below that the scale is not trusted and the test says so instead of guessing.
MIN_SCALE_SAMPLES = 8


def _kind(kind: str) -> str:
    k = str(kind).lower()
    return "hdr" if k in ("hdr", "pq") else "sdr"


def _node_q(gain: float, i: int, kind: str) -> float:
    """The quantity the C++ interpolates linearly in u at slot ``i`` (times n-1): ``i*sqrt(g)`` (SDR),
    ``i*g`` (HDR)."""
    return i * math.sqrt(max(gain, 0.0)) if kind == "sdr" else i * gain


def _q_to_gain(q: float, u: float, kind: str) -> float:
    if u <= 0.0:
        return 1.0
    return (q / u) ** 2 if kind == "sdr" else q / u


def slot_position(code: float, cap: float, n: int, kind: str) -> float:
    """``u`` - the curve's interpolation coordinate of a grey at ``code`` (cap = the code the top
    node sits at): ``sqrt(code/cap)*(n-1)`` (SDR), ``code/cap*(n-1)`` (HDR)."""
    x = max(0.0, min(1.0, float(code) / float(cap))) if cap else 0.0
    return (math.sqrt(x) if _kind(kind) == "sdr" else x) * (n - 1)


def interpolated_gain(gains: Sequence[float], u: float, kind: str) -> float:
    """The effective per-channel gain (output / input) the baked curve applies at slot position
    ``u`` - the C++ law (mhc.cpp ``EvalGrayscaleSDR_Channel`` / ``EvalGrayscaleHDR_Channel``)."""
    k = _kind(kind)
    n = len(gains)
    u = max(0.0, min(float(n - 1), float(u)))
    if u <= 0.0:
        return float(gains[0])
    i0 = int(math.floor(u))
    i1 = min(i0 + 1, n - 1)
    f = u - i0
    q = _node_q(gains[i0], i0, k) * (1.0 - f) + _node_q(gains[i1], i1, k) * f
    return _q_to_gain(q, u, k)


def heldout_code(i: int, cap: int, n: int, kind: str) -> int:
    """The held-out grey between nodes ``i`` and ``i+1``: the midpoint in the interpolation
    coordinate ``u``. The baked curve is a straight line in (u, sqrt(output)) [SDR] / (u, output)
    [HDR] between the nodes, so its error against any smooth target is
    ``f''(xi)/2 * (u-i)(u-i-1)`` - largest at ``u = i + 1/2``. For a narrow feature under a node the
    damage is a tent over the neighbouring interval and this point sits on half of it. SDR: code
    ``cap*((i+1/2)/(n-1))^2`` (within 0.27 code of the arithmetic code midpoint of the two nodes,
    because the nodes sit at cap*t^2 - but far from the luminance midpoint); HDR: the code midpoint."""
    t = (i + 0.5) / (n - 1)
    return int(round(cap * (t * t if _kind(kind) == "sdr" else t)))


def neighbour_hold_gain(gains: Sequence[float], k: int, left: int, right: int, kind: str) -> float:
    """The gain node ``k`` takes when it is put ON the curve its nearest kept neighbours ``left`` <
    k < ``right`` draw through it (the C++ interpolation law, so the held curve is exactly the line
    the baked curve would draw if node k were absent)."""
    kd = _kind(kind)
    q = ((right - k) * _node_q(gains[left], left, kd) + (k - left) * _node_q(gains[right], right, kd)) \
        / float(right - left)
    return _q_to_gain(q, float(k), kd)


def local_trend_gain(gains: Sequence[float], k: int, nb: Sequence[int], kind: str) -> float:
    """The gain the curve's local TREND predicts at node ``k`` from two kept nodes on each side
    (``nb`` = [l2, l1, r1, r2]): the cubic through the interpolated quantity q_i (= i*sqrt(g_i) SDR,
    i*g_i HDR) at those four slots, evaluated at ``k``. Exact for any cubic, so a smooth bend of the
    correction (a real, gradual need) predicts itself and leaves no residual; only a departure
    narrower than the node spacing does."""
    kd = _kind(kind)
    xs = [float(j) for j in nb]
    qs = [_node_q(gains[j], j, kd) for j in nb]
    q = 0.0
    for a, (xa, qa) in enumerate(zip(xs, qs)):
        w = 1.0
        for b, xb in enumerate(xs):
            if b != a:
                w *= (k - xb) / (xa - xb)
        q += w * qa
    return _q_to_gain(q, float(k), kd)


def _sign(v: float, eps: float = 1e-9) -> int:
    return 0 if abs(v) <= eps else (1 if v > 0 else -1)


def _mad_scale(values: Sequence[float]) -> tuple[float, float]:
    med = median(values)
    return med, MAD_TO_SIGMA * median(abs(v - med) for v in values)


def gain_change_de(ratios: Mapping[str, float], *, light_exponent: float,
                   white_xy: tuple[float, float] = (0.3127, 0.3290)) -> float:
    """dE2000 (the touch-up's own point metric, :func:`point_error`) between a target grey and the
    same grey with its per-channel curve gains multiplied by ``ratios`` (light = gain**exponent,
    composed in the solver's sRGB basis)."""
    m = rgb_to_xyz_matrix(_SRGB[0][0], _SRGB[0][1], _SRGB[1][0], _SRGB[1][1], _SRGB[2][0], _SRGB[2][1],
                          white_xy[0], white_xy[1], white_Y=1.0)
    moved = matvec(m, [max(float(ratios[ch]), 0.0) ** light_exponent for ch in _CHANNELS])
    return float(point_error(GrayTouchupPatch(level=0.0, measured_xyz=tuple(moved), target_y=1.0),  # type: ignore[arg-type]
                             GrayTouchupConfig(white_xy=white_xy))["de2000"])


def find_node_outliers(deviations: Mapping[str, Sequence[float]], solved: Iterable[int], *,
                       codes: Sequence[int], kind: str,
                       alpha: float = FAMILY_ALPHA,
                       materiality_de: Optional[float] = None,
                       light_exponent: Optional[Sequence[float]] = None,
                       white_xy: tuple[float, float] = (0.3127, 0.3290)) -> dict[str, Any]:
    """Solved nodes whose correction departs from their neighbours - evidence of a feature narrower
    than the node spacing under that node (its correction will be smeared over both neighbouring
    intervals by the curve's interpolation).

    Statistic: per node ``k`` and channel, the log ratio of the solved composed gain to the gain the
    LOCAL TREND of its neighbours predicts there (:func:`local_trend_gain`: the cubic through nodes
    k-2, k-1, k+1, k+2 - all solved - in the quantity the curve interpolates). A smooth correction -
    flat, sloped or bending - predicts itself (residual ~0); a single node off its neighbours' trend
    does not. Scale = 1.4826 x the MAD of the residuals over all eligible nodes (robust to the few
    outliers it looks for). Candidates: nodes whose largest |z| exceeds the two-sided Bonferroni
    critical value for ``3 x m`` tests at ``alpha`` family-wise (m eligible nodes). A spike at k also
    moves the residuals of k+-1 and k+-2 (their trend runs through k), so those are not independent
    evidence: the candidates are taken largest |z| first and every candidate within two nodes of an
    accepted one is reported as its ECHO, not flagged. ``solved`` = the nodes the solver moved (a
    node left at its default is no evidence either way). The reported ``neighbour_interpolation`` is
    what a hold puts there (the curve's own law between the nearest neighbours).

    MATERIALITY (``materiality_de``, the touch-up's own acceptance target): a statistically clear
    departure is flagged only when holding the node would move the grey at its code by at least that
    much (:func:`gain_change_de`, light = gain**``light_exponent[k]``). The node's value reaches the
    greys around it as a tent that peaks at the node, so a departure below the flow's own "done"
    tolerance cannot damage any grey by more than the flow already accepts at a node; such nodes are
    listed under ``immaterial``."""
    kd = _kind(kind)
    n = len(codes)
    devs = {ch: [float(v) for v in (deviations.get(ch) or [1.0] * n)] for ch in _CHANNELS}
    ok = {int(k) for k in solved}
    rows: dict[int, tuple[dict[str, float], dict[str, float]]] = {}
    for k in range(2, n - 2):
        if not {k - 2, k - 1, k, k + 1, k + 2} <= ok:
            continue
        trend = {ch: local_trend_gain(devs[ch], k, (k - 2, k - 1, k + 1, k + 2), kd) for ch in _CHANNELS}
        if any(t <= 0.0 or devs[ch][k] <= 0.0 for ch, t in trend.items()):
            continue
        rows[k] = (trend, {ch: math.log(devs[ch][k] / trend[ch]) for ch in _CHANNELS})
    m = len(rows)
    out: dict[str, Any] = {
        "rule": ("node residual = ln(solved gain / the gain the cubic through solved nodes k-2, k-1, k+1, "
                 "k+2 predicts) per channel, in the quantity the C++ curve interpolates; z = (residual - "
                 "median) / (1.4826 MAD) over the eligible nodes; flag when max |z| > the two-sided "
                 f"Bonferroni critical value for 3 x m tests at family-wise alpha {alpha:g}; largest first, "
                 "candidates within 2 nodes of a flagged node are its echoes"),
        "alpha": alpha, "eligible_nodes": m, "kind": kd, "flagged": [], "echoes": [], "immaterial": [],
        "materiality_de": materiality_de,
    }
    if m < MIN_SCALE_SAMPLES:
        out.update(available=False,
                   reason=f"only {m} solved nodes have two solved neighbours on each side (need "
                          f"{MIN_SCALE_SAMPLES} for a robust scale)")
        return out
    z_crit = NormalDist().inv_cdf(1.0 - alpha / (2.0 * 3.0 * m))
    stats = {ch: _mad_scale([r[1][ch] for r in rows.values()]) for ch in _CHANNELS}
    zs = {k: {ch: (r[1][ch] - stats[ch][0]) / max(stats[ch][1], SCALE_FLOOR) for ch in _CHANNELS}
          for k, r in rows.items()}
    zmax = {k: max(abs(z) for z in zk.values()) for k, zk in zs.items()}
    out.update(available=True, z_crit=round(z_crit, 4),
               residual_scale={ch: {"median": round(stats[ch][0], 6), "sigma": round(stats[ch][1], 6)}
                               for ch in _CHANNELS},
               z_by_node={str(k): round(v, 3) for k, v in sorted(zmax.items())})
    accepted: list[int] = []
    for k in sorted((k for k in zmax if zmax[k] > z_crit), key=lambda k: -zmax[k]):
        near = [a for a in accepted if abs(a - k) <= 2]
        if near:
            out["echoes"].append({"index": k, "code": int(codes[k]), "max_abs_z": round(zmax[k], 3),
                                  "echo_of": near[0]})
            continue
        trend = rows[k][0]
        hold = {ch: neighbour_hold_gain(devs[ch], k, k - 1, k + 1, kd) for ch in _CHANNELS}
        effect = None
        if materiality_de is not None and light_exponent is not None:
            effect = gain_change_de({ch: hold[ch] / devs[ch][k] for ch in _CHANNELS},
                                    light_exponent=float(light_exponent[k]), white_xy=white_xy)
            if effect < materiality_de:
                out["immaterial"].append({"index": k, "code": int(codes[k]), "max_abs_z": round(zmax[k], 3),
                                          "hold_changes_grey_de": round(effect, 4)})
                continue
        accepted.append(k)
        nb_mean = {ch: 0.5 * (devs[ch][k - 1] + devs[ch][k + 1]) for ch in _CHANNELS}
        direction = {}
        for ch in _CHANNELS:
            s_node, s_nb = _sign(devs[ch][k] - 1.0), _sign(nb_mean[ch] - 1.0)
            direction[ch] = ("opposite" if s_node and s_nb and s_node != s_nb
                             else "same" if s_node and s_node == s_nb else "neutral")
        out["flagged"].append({
            "index": k, "code": int(codes[k]),
            "solved_gain": {ch: round(devs[ch][k], 6) for ch in _CHANNELS},
            "local_trend": {ch: round(trend[ch], 6) for ch in _CHANNELS},
            "solved_vs_trend_pct": {ch: round(100.0 * (devs[ch][k] / trend[ch] - 1.0), 4) for ch in _CHANNELS},
            "neighbour_interpolation": {ch: round(hold[ch], 6) for ch in _CHANNELS},
            "solved_vs_neighbours_pct": {ch: round(100.0 * (devs[ch][k] / hold[ch] - 1.0), 4)
                                         for ch in _CHANNELS},
            "z": {ch: round(zs[k][ch], 3) for ch in _CHANNELS},
            "max_abs_z": round(zmax[k], 3),
            "hold_changes_grey_de": round(effect, 4) if effect is not None else None,
            "direction_vs_neighbours": direction,
            "neighbours": {side: {"index": j, "code": int(codes[j]),
                                  "gain": {ch: round(devs[ch][j], 6) for ch in _CHANNELS}}
                           for side, j in (("left", k - 1), ("right", k + 1))},
            "spreads_over_codes": [int(codes[k - 1]), int(codes[k + 1])],
        })
    return out


def light_trend(nodes: Sequence[Mapping[str, Any]], *, alpha: float = FAMILY_ALPHA) -> dict[str, Any]:
    """The LIGHT part of the touch-up, committed only as light the nodes on BOTH sides share.

    Why (2026-11-14 HW): the PA32UCXR's full-field light is a sawtooth in the 10-bit code (teeth of
    2-4 % every 4-12 codes) while its chromaticity is flat. A node's light correction from reads at one
    code samples one phase of the sawtooth; it is right at that code and wrong by up to a tooth a few
    codes away. Its BALANCE correction is sound - this touches light only.

    ``nodes``: the solved nodes that take part (any order): ``index``, ``need`` = ln of the luminance
    (common) curve gain that puts the node exactly on its target light, from its own read; ``own`` = ln
    of the gain the solver left there; ``sigma`` = the read-noise SE of ``need`` (None: one read).

    Rule (no tuned constant, no noise dependence): the light committed at node k is what EVERY node of
    its centred window k-2 .. k+2 asks for at least - if all five ask the same sign, the smallest of
    the five asks; if their signs differ, or any of the five does not take part (an end of the
    supported range, a held-dark node, white), none. Consequences, each one a property:
      * never extrapolated: a value needs two supporting nodes on each side;
      * continuous to zero at the edges of the supported range (the two nodes at an edge get none);
      * bounded by what the supporting nodes themselves ask for (never more than any of them asks);
      * a tooth one or two nodes wide, or teeth that alternate, commit nothing; a light need shared over
        five nodes is kept (reduced to its smallest ask - a genuine trend is under-corrected by at most
        the scatter of its asks, never over-corrected);
      * independent of the assumed read noise: the noise only LABELS the evidence (``z`` = how far a
        node's own ask is from what is committed, in units of its read noise), it decides nothing.
    The window is the trend support of the node-outlier test (two nodes each side): the smallest
    centred support that can tell a node's own ask from what its neighbours ask on both sides.
    """
    seq = sorted(nodes, key=lambda d: int(d["index"]))
    by_idx = {int(d["index"]): d for d in seq}
    m = len(seq)
    out: dict[str, Any] = {
        "rule": ("committed light at node k = the smallest ask of the five nodes k-2..k+2 when all five take "
                 "part and ask the same sign, else none (no extrapolation, zero at the edges of the supported "
                 "range, never more than any supporting node asks, independent of the read noise)"),
        "alpha": alpha, "n_nodes": m, "teeth": [], "changed": [], "material": [], "nodes": [],
    }
    if m == 0:
        out.update(available=False, reason="no solved node takes part")
        return out
    z_crit = NormalDist().inv_cdf(1.0 - alpha / (2.0 * m))
    out.update(available=True, z_crit=round(z_crit, 4))
    for d in seq:
        k = int(d["index"])
        need, own = float(d["need"]), float(d.get("own", d["need"]))
        window = [by_idx.get(j) for j in range(k - 2, k + 3)]
        if any(w is None for w in window):
            committed, reason = 0.0, "not supported on both sides (an edge of the range): no light"
        else:
            asks = [float(w["need"]) for w in window]   # type: ignore[index]
            if all(a > 0 for a in asks):
                committed, reason = min(asks), "shared by k-2..k+2: the smallest ask"
            elif all(a < 0 for a in asks):
                committed, reason = max(asks), "shared by k-2..k+2: the smallest ask"
            else:
                committed, reason = 0.0, "k-2..k+2 ask for light of both signs: none is shared"
        s = d.get("sigma")
        if s is None:   # one read: the median read noise of the (up to 8) nearest nodes that have repeats
            near = sorted((x for x in seq if x.get("sigma") is not None),
                          key=lambda x: (abs(int(x["index"]) - k), int(x["index"])))[:MIN_SCALE_SAMPLES]
            s = median(float(x["sigma"]) for x in near) if near else None
        s_eff = max(float(s), SCALE_FLOOR) if s is not None else SCALE_FLOOR
        z = (need - committed) / s_eff
        tooth = abs(z) > z_crit
        # material = the light at this node moves by more than its reads can resolve
        material = abs(committed - own) > z_crit * s_eff
        row = {"index": k, "code": d.get("code"),
               "need_pct": round(100.0 * (math.exp(need) - 1.0), 4),
               "own_pct": round(100.0 * (math.exp(own) - 1.0), 4),
               "committed_pct": round(100.0 * (math.exp(committed) - 1.0), 4),
               "committed_gain": math.exp(committed),
               "sigma_pct": round(100.0 * float(s), 4) if s is not None else None,
               "z": round(z, 3) if z is not None else None, "tooth": bool(tooth), "reason": reason,
               "changed": abs(committed - own) > 1e-12, "material": bool(material)}
        out["nodes"].append(row)
        if tooth:
            out["teeth"].append(k)
        if material:
            out["material"].append(k)
        if row["changed"]:
            out["changed"].append(k)
    return out


def apply_light_trend(payload: Mapping[str, Any], trend: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Commit the light the trend decided (:func:`light_trend` ``committed_gain``) on every changed node;
    its rgb balance is kept. Returns ``(payload, detail)``."""
    points = [float(p) for p in payload["points"]]
    current = coerce_payload(payload, points)
    lum = list(current["luminance"])
    detail: dict[str, Any] = {}
    for row in trend.get("nodes") or ():
        if not row.get("changed"):
            continue
        k = int(row["index"])
        detail[str(k)] = {"from": round(lum[k], 6), "to": round(float(row["committed_gain"]), 6),
                          "reason": row.get("reason")}
        lum[k] = float(row["committed_gain"])
    return compose_payload(points, lum, current["rgb"]), detail


def apply_node_holds(payload: Mapping[str, Any], holds: Sequence[int], *,
                     kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Put each node in ``holds`` on the curve its nearest NON-held neighbours draw (a run of
    adjacent held nodes is bridged as one span - exactly the curve the bake would draw with those
    nodes absent). The composed gain is split the solver's way: luminance = the geometric mean of
    the three channel gains, rgb = the zero-mean balance. Returns ``(payload, detail)``."""
    kd = _kind(kind)
    points = [float(p) for p in payload["points"]]
    current = coerce_payload(payload, points)
    n = len(points)
    hs = sorted({int(k) for k in holds if 0 < int(k) < n - 1})
    devs = {ch: list(current["deviations"][ch]) for ch in _CHANNELS}
    lum = list(current["luminance"])
    rgb = {ch: list(current["rgb"][ch]) for ch in _CHANNELS}
    detail: dict[str, Any] = {}
    for k in hs:
        left = max(j for j in range(k) if j not in hs)
        right = min(j for j in range(k + 1, n) if j not in hs)
        held = {ch: neighbour_hold_gain(devs[ch], k, left, right, kd) for ch in _CHANNELS}
        lk = (held["r"] * held["g"] * held["b"]) ** (1.0 / 3.0)
        detail[str(k)] = {"from": {ch: round(devs[ch][k], 6) for ch in _CHANNELS},
                          "to": {ch: round(held[ch], 6) for ch in _CHANNELS},
                          "between_nodes": [left, right]}
        lum[k] = lk
        for ch in _CHANNELS:
            rgb[ch][k] = held[ch] / lk if lk > 1e-12 else 1.0
    return compose_payload(points, lum, rgb), detail


def _err(xyz: Sequence[float], target_y: float, config: GrayTouchupConfig) -> dict[str, Any]:
    return point_error(GrayTouchupPatch(level=0.0, measured_xyz=tuple(float(v) for v in xyz),  # type: ignore[arg-type]
                                        target_y=float(target_y)), config)


def _read_summary(xyz: Sequence[float], target_y: float, config: GrayTouchupConfig) -> dict[str, Any]:
    e = _err(xyz, target_y, config)
    x, y = e["measured_xy"]
    return {"Y": e["measured_Y"],
            "Y_err_pct": round(100.0 * (float(xyz[1]) / target_y - 1.0), 3) if target_y > 0 else None,
            "dxy": [round(x - config.white_xy[0], 5), round(y - config.white_xy[1], 5)],
            "de2000": e["de2000"]}


def _reads_se(reads: Sequence[Sequence[float]], target_y: float,
              config: GrayTouchupConfig) -> Optional[float]:
    """Standard error of the mean dE2000 (to target) over a grey's repeat reads; None for < 2."""
    des = [_err(r, target_y, config)["de2000"] for r in reads
           if r is not None and len(r) == 3 and all(math.isfinite(float(v)) for v in r)]
    if len(des) < 2:
        return None
    mu = sum(des) / len(des)
    sd = math.sqrt(sum((d - mu) ** 2 for d in des) / (len(des) - 1))
    return sd / math.sqrt(len(des))


def judge_held_out(greys: Sequence[Mapping[str, Any]], after: Mapping[int, Sequence[float]],
                   null_pairs: Sequence[Mapping[str, Any]], *, config: GrayTouchupConfig,
                   alpha: float = FAMILY_ALPHA) -> dict[str, Any]:
    """Judge the touch-up at greys it was NOT fitted on: each grey's dE2000 to its target AFTER the
    bake (``after[code]``, the verify read) against BEFORE the touch-up (``grey['before_xyz']``, read
    in the touch-up session through the before-state table).

    Repeatability (the measurement's own, from this run):
      * the NULL - every solved node read twice in an unchanged state: its last read in the
        touch-up session and its verify read of the bake (``null_pairs``). ``d = dE(verify) -
        dE(session)`` holds read noise + drift between the two occasions + any preview/bake gap.
        Per grey, the median (drift) and 1.4826 MAD (scatter) of ``d`` over the
        :data:`MIN_SCALE_SAMPLES` pairs nearest in code (noise and drift depend on the level);
      * the grey's own repeat reads: sqrt(2) x the SE of its before reads (the after read at the same
        level is assumed as noisy).
    Rule: ``change = dE_after - dE_before - median_local(d)``; sigma = max(MAD sigma, sqrt(2) SE)
    (floored at :data:`SCALE_FLOOR`); REGRESSED when ``change > z * sigma`` with z the one-sided
    Bonferroni critical value for the judged greys at ``alpha`` family-wise. A grey with no
    repeatability estimate at all (fewer than 8 pairs AND a single read) is reported, not judged."""
    pairs = []
    for p in null_pairs:
        try:
            d = (_err(p["verify_xyz"], p["target_Y"], config)["de2000"]
                 - _err(p["final_xyz"], p["target_Y"], config)["de2000"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(d):
            pairs.append({"code": int(p["code"]), "index": p.get("index"), "d": d})
    null_ok = len(pairs) >= MIN_SCALE_SAMPLES
    rows: list[dict[str, Any]] = []
    for g in greys:
        code = int(g["code"])
        ty = float(g["target_Y"])
        row: dict[str, Any] = {"code": code, "kind": g.get("kind", "between"),
                               "between_nodes": g.get("between_nodes"), "target_Y": round(ty, 5)}
        bxyz = g.get("before_xyz")
        axyz = after.get(code)
        if bxyz is None or axyz is None:
            row.update(judged=False, reason=("no usable before read" if bxyz is None
                                             else "not measured by the verify"))
            rows.append(row)
            continue
        before = _read_summary(bxyz, ty, config)
        aft = _read_summary(axyz, ty, config)
        se = _reads_se(g.get("before_reads") or (), ty, config)
        med_d, sig_d = 0.0, None
        if null_ok:
            local = sorted(pairs, key=lambda p: (abs(p["code"] - code), p["code"]))[:MIN_SCALE_SAMPLES]
            med_d, sig_d = _mad_scale([p["d"] for p in local])
        parts = [v for v in (sig_d, (math.sqrt(2.0) * se) if se is not None else None) if v is not None]
        row.update(before=before, after=aft,
                   drift_median=round(med_d, 5),
                   null_sigma=round(sig_d, 5) if sig_d is not None else None,
                   reads_se=round(se, 5) if se is not None else None)
        if not parts:
            row.update(judged=False, reason="no repeatability estimate (fewer than "
                                            f"{MIN_SCALE_SAMPLES} node pairs and a single read)")
            rows.append(row)
            continue
        sigma = max(max(parts), SCALE_FLOOR)
        change = aft["de2000"] - before["de2000"] - med_d
        row.update(judged=True, change=round(change, 5), sigma=round(sigma, 5),
                   score=round(change / sigma, 3))
        rows.append(row)
    judged = [r for r in rows if r.get("judged")]
    z_crit = NormalDist().inv_cdf(1.0 - alpha / len(judged)) if judged else None
    for r in judged:
        r["threshold"] = round(z_crit * r["sigma"], 5)   # type: ignore[operator]
        r["regressed"] = bool(r["change"] > z_crit * r["sigma"])   # type: ignore[operator]
    regressed = [r for r in judged if r["regressed"]]
    worst = sorted(judged, key=lambda r: r["score"], reverse=True)[:5]
    return {
        "available": bool(judged),
        "rule": ("change = dE_after - dE_before - median(d) over the 8 nearest node pairs (d = a solved "
                 "node's verify dE minus its last session dE: the same state read twice); sigma = "
                 "max(1.4826 MAD of those d, sqrt(2) x SE of the grey's before reads); regressed when "
                 f"change > z x sigma, z = one-sided Bonferroni critical value for the judged greys at "
                 f"alpha {alpha:g}"),
        "alpha": alpha, "z_crit": round(z_crit, 4) if z_crit is not None else None,
        "n_greys": len(rows), "n_judged": len(judged), "n_regressed": len(regressed),
        "regressed_codes": [r["code"] for r in regressed],
        "worst": [{**{k: r.get(k) for k in ("code", "kind", "change", "threshold", "score", "regressed")},
                   "before": r["before"], "after": r["after"]} for r in worst],
        "null": {"n_pairs": len(pairs), "used": null_ok,
                 "median_d": round(median([p["d"] for p in pairs]), 5) if pairs else None,
                 "pairs": [{"code": p["code"], "d": round(p["d"], 5)} for p in pairs]},
        "greys": rows,
    }
