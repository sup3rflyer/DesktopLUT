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


GAMMA24_WARP = 2.4 / 2.2


def _lerp_curve(curve: Sequence[float], t: float) -> float:
    """mhc_icc.cpp ``resampleCurve``: linear interpolation of a 1D cube's entries at ``t`` in [0, 1]."""
    n = len(curve)
    x = max(0.0, min(1.0, t)) * (n - 1)
    i0 = int(math.floor(x))
    i1 = min(i0 + 1, n - 1)
    return max(0.0, min(1.0, float(curve[i0]) + (float(curve[i1]) - float(curve[i0])) * (x - i0)))


def base_positions(code: float, *, max_cv: int, cap: int, n: int, kind: str,
                   base: Optional[Mapping[str, Sequence[float]]] = None, gamma24: bool = False) -> dict[str, float]:
    """The fine-tune curve coordinate ``u`` PER CHANNEL of a grey patch at ``code``, as the bake evaluates it
    (src/mhc_icc.cpp ``ComposeMhcScanout``: the curve runs on the BASE 1D LUT's OUTPUT; with SDR GAMMA 2.4 the
    base is read at t^(2.4/2.2) - ``ComputeMhcScanoutBaseLuts``). ``base`` = the installed base 1D cube
    (per channel, None = identity). SDR: u = sqrt(base(t)) (n-1); HDR: u = base(t) / (cap/max_cv) (n-1)."""
    kd = _kind(kind)
    t = max(0.0, min(1.0, float(code) / float(max_cv)))
    if gamma24 and kd == "sdr" and t > 0:
        t = t ** GAMMA24_WARP
    out = {}
    for ch in _CHANNELS:
        v = _lerp_curve(base[ch], t) if base is not None else t
        out[ch] = (math.sqrt(v) if kd == "sdr" else v / (float(cap) / float(max_cv))) * (n - 1)
    return out


def heldout_codes_on_base(node_codes: Sequence[int], intervals: Iterable[int], *, max_cv: int, cap: int,
                          kind: str, base: Optional[Mapping[str, Sequence[float]]] = None,
                          gamma24: bool = False) -> list[int]:
    """The held-out grey of each interval ``i`` (between slots i and i+1): the patch code whose curve
    coordinate is closest to the slot midpoint u = i + 1/2 THROUGH THE BASE the bake uses. Three channels,
    one patch: the code that minimises the LARGEST channel distance max_c |u_c - (i + 1/2)| (no channel
    lands further from the midpoint than it must). Node codes are never chosen."""
    n = len(node_codes)
    nodes = {int(c) for c in node_codes}
    pos = {c: base_positions(c, max_cv=max_cv, cap=cap, n=n, kind=kind, base=base, gamma24=gamma24)
           for c in range(1, int(cap))}
    out: list[int] = []
    for i in intervals:
        mid = i + 0.5
        best = min((c for c in pos if c not in nodes and c not in out),
                   key=lambda c: (max(abs(u - mid) for u in pos[c].values()), c))
        out.append(best)
    return out


def neighbour_hold_gain(gains: Sequence[float], k: int, left: int, right: int, kind: str) -> float:
    """The gain node ``k`` takes when it is put ON the curve its nearest kept neighbours ``left`` <
    k < ``right`` draw through it (the C++ interpolation law, so the held curve is exactly the line
    the baked curve would draw if node k were absent)."""
    kd = _kind(kind)
    q = ((right - k) * _node_q(gains[left], left, kd) + (k - left) * _node_q(gains[right], right, kd)) \
        / float(right - left)
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
    """Narrow features under the nodes: one node, or two adjacent nodes, whose correction departs from
    the straight lines BOTH sides draw toward it - in the same direction.

    For each channel, in ln(gain): the LEFT line through the two solved nodes just below the feature and
    the RIGHT line through the two just above it, each extrapolated onto the feature's node(s). A narrow
    feature departs from both lines the same way. A KNEE in a smooth correction (a change of slope at a
    node) sits on one of the two lines - it is not a feature (review item 7); a smooth bend departs from
    both by the same small amount at every node, which the per-channel median removes.

    Scale: s = 1.4826 MAD of all one-step one-sided residuals of the channel (eligible nodes, both sides
    pooled) about their median; a two-step extrapolation (a pair's far node) has sqrt(14/6) that noise
    (var(3y1 - 2y2 - y) / var(2y1 - y2 - y) for independent node noise). A single node is a feature when,
    in some channel, both its residuals exceed ``z * s`` with the same sign; a PAIR (k, k+1) when all four
    of its residuals do (its partner is then not used as a neighbour - review item 7: the second node of a
    pair used to be called an echo and was used as the first one's neighbour). z = the two-sided Bonferroni
    critical value over 3 channels x (singles + pairs tested) at ``alpha``. Materiality (``materiality_de``,
    the touch-up's own acceptance target): flagged only when bridging the feature moves its grey by at
    least that much (:func:`gain_change_de`, light = gain ** ``light_exponent``)."""
    kd = _kind(kind)
    n = len(codes)
    ok = {int(k) for k in solved}
    y = {ch: [math.log(max(float(v), 1e-9)) for v in (deviations.get(ch) or [1.0] * n)] for ch in _CHANNELS}
    singles = [k for k in range(2, n - 2) if {k - 2, k - 1, k, k + 1, k + 2} <= ok]
    pairs = [k for k in range(2, n - 3) if {k - 2, k - 1, k, k + 1, k + 2, k + 3} <= ok]
    m = len(singles) + len(pairs)
    out: dict[str, Any] = {
        "rule": ("per channel in ln(gain): the left line (two solved nodes below) and the right line (two above), "
                 "extrapolated onto a node or an adjacent pair; a feature departs from both lines in the same "
                 "direction by more than z x s (s = 1.4826 MAD of the one-sided residuals; x sqrt(14/6) two steps "
                 f"out); z = two-sided Bonferroni over 3 x the tests at family-wise alpha {alpha:g}; a knee sits "
                 "on one line and is not a feature; flagged only when bridging moves the grey by the touch-up's "
                 "own acceptance target"),
        "alpha": alpha, "eligible_nodes": len(singles), "eligible_pairs": len(pairs), "kind": kd,
        "flagged": [], "immaterial": [], "materiality_de": materiality_de,
    }
    if len(singles) < MIN_SCALE_SAMPLES:
        out.update(available=False, reason=f"only {len(singles)} solved nodes have two solved neighbours on each "
                                           f"side (need {MIN_SCALE_SAMPLES} for a robust scale)")
        return out
    z = NormalDist().inv_cdf(1.0 - alpha / (2.0 * 3.0 * m))

    def left(ch: str, k: int, steps: int) -> float:   # line through k-steps-1, k-steps onto k
        a, b = y[ch][k - steps], y[ch][k - steps - 1]
        return y[ch][k] - (a + steps * (a - b))

    def right(ch: str, k: int, steps: int) -> float:
        a, b = y[ch][k + steps], y[ch][k + steps + 1]
        return y[ch][k] - (a + steps * (a - b))

    scale = {}
    for ch in _CHANNELS:
        res = [left(ch, k, 1) for k in singles] + [right(ch, k, 1) for k in singles]
        scale[ch] = _mad_scale(res)
    s2 = math.sqrt(14.0 / 6.0)
    out.update(available=True, z_crit=round(z, 4),
               residual_scale={ch: {"median": round(v[0], 6), "sigma": round(v[1], 6)} for ch, v in scale.items()})

    def zr(ch: str, r: float, far: bool) -> float:
        med, s = scale[ch]
        return (r - med) / (max(s, SCALE_FLOOR) * (s2 if far else 1.0))

    found: list[tuple[list[int], dict[str, list[float]]]] = []
    for k in pairs:
        zs = {ch: [zr(ch, left(ch, k, 1), False), zr(ch, left(ch, k + 1, 2), True),
                   zr(ch, right(ch, k + 1, 1), False), zr(ch, right(ch, k, 2), True)] for ch in _CHANNELS}
        if any(all(v > z for v in zz) or all(v < -z for v in zz) for zz in zs.values()):
            found.append(([k, k + 1], zs))
    in_pair = {j for nodes, _z in found for j in nodes}
    for k in singles:
        if k in in_pair:
            continue
        zs = {ch: [zr(ch, left(ch, k, 1), False), zr(ch, right(ch, k, 1), False)] for ch in _CHANNELS}
        if any(all(v > z for v in zz) or all(v < -z for v in zz) for zz in zs.values()):
            found.append(([k], zs))
    found.sort(key=lambda f: f[0][0])
    gains = {ch: [math.exp(v) for v in y[ch]] for ch in _CHANNELS}
    for nodes, zs in found:
        lo, hi = nodes[0] - 1, nodes[-1] + 1
        bridged = {ch: [neighbour_hold_gain(gains[ch], k, lo, hi, kd) for k in nodes] for ch in _CHANNELS}
        effect = None
        if materiality_de is not None and light_exponent is not None:
            effect = max(gain_change_de({ch: bridged[ch][j] / gains[ch][k] for ch in _CHANNELS},
                                        light_exponent=float(light_exponent[k]), white_xy=white_xy)
                         for j, k in enumerate(nodes))
            if effect < materiality_de:
                out["immaterial"].append({"nodes": nodes, "codes": [int(codes[k]) for k in nodes],
                                          "hold_changes_grey_de": round(effect, 4)})
                continue
        maxz = max(abs(v) for zz in zs.values() for v in zz)
        out["flagged"].append({
            "index": nodes[0], "nodes": nodes, "code": int(codes[nodes[0]]), "codes": [int(codes[k]) for k in nodes],
            "solved_gain": {ch: [round(gains[ch][k], 6) for k in nodes] for ch in _CHANNELS},
            "neighbour_interpolation": {ch: [round(v, 6) for v in bridged[ch]] for ch in _CHANNELS},
            "solved_vs_neighbours_pct": {ch: [round(100.0 * (gains[ch][k] / bridged[ch][j] - 1.0), 4)
                                              for j, k in enumerate(nodes)] for ch in _CHANNELS},
            "z": {ch: [round(v, 3) for v in zz] for ch, zz in zs.items()},
            "max_abs_z": round(maxz, 3),
            "hold_changes_grey_de": round(effect, 4) if effect is not None else None,
            "neighbours": {side: {"index": j, "code": int(codes[j]),
                                  "gain": {ch: round(gains[ch][j], 6) for ch in _CHANNELS}}
                           for side, j in (("left", lo), ("right", hi))},
            "spreads_over_codes": [int(codes[lo]), int(codes[hi])],
        })
    return out


def apply_balance_smoothing(payload: Mapping[str, Any], features: Sequence[Sequence[int]]
                            ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The owner's outcome for a narrow feature: a SMOOTH white-balance correction with no per-node outlier.
    Each flagged node's BALANCE (rgb) takes the straight line in ln(balance) between the nearest unflagged
    neighbours; its LIGHT (the luminance slider, already committed as the shared light) is kept. The
    display's own defect at that code is then left uncorrected by this flow (it belongs to the base LUT)."""
    points = [float(p) for p in payload["points"]]
    current = coerce_payload(payload, points)
    n = len(points)
    flagged = sorted({int(k) for f in features for k in f if 0 < int(k) < n - 1})
    rgb = {ch: list(current["rgb"][ch]) for ch in _CHANNELS}
    detail: dict[str, Any] = {}
    for k in flagged:
        lo = max(j for j in range(k) if j not in flagged)
        hi = min(j for j in range(k + 1, n) if j not in flagged)
        w = (k - lo) / float(hi - lo)
        new = {ch: math.exp((1.0 - w) * math.log(rgb[ch][lo]) + w * math.log(rgb[ch][hi])) for ch in _CHANNELS}
        gm = (new["r"] * new["g"] * new["b"]) ** (1.0 / 3.0)
        new = {ch: v / gm for ch, v in new.items()}       # zero-mean balance, as the solver keeps it
        detail[str(k)] = {"from": {ch: round(rgb[ch][k], 6) for ch in _CHANNELS},
                          "to": {ch: round(new[ch], 6) for ch in _CHANNELS}, "between_nodes": [lo, hi]}
        for ch in _CHANNELS:
            rgb[ch][k] = new[ch]
    return compose_payload(points, current["luminance"], rgb), detail


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


def curve_gains(points: Sequence[float], deviations: Mapping[str, Sequence[float]], kind: str) -> dict[str, list[float]]:
    """The composed per-channel slot gains of a curve AS DESKTOPLUT STORES IT (``points`` already carry
    the luminance, ``deviations`` the balance): gain_c[i] = points[i] * dev_c[i] / identity_point[i]."""
    kd = _kind(kind)
    n = len(points)
    ident = [(i / (n - 1)) if kd == "hdr" else (i / (n - 1)) ** 2 for i in range(n)]
    return {ch: [(float(points[i]) * float((deviations.get(ch) or [1.0] * n)[i]) / ident[i]) if ident[i] > 0 else 1.0
                 for i in range(n)] for ch in _CHANNELS}


def light_ratios(after: Mapping[str, Sequence[float]], before: Optional[Mapping[str, Sequence[float]]],
                 u: Mapping[str, float], *, light_exponent: float, kind: str) -> dict[str, float]:
    """Per channel, the LIGHT ratio the change from the ``before`` curve (None = identity) to the ``after``
    curve makes at a grey whose curve coordinate is ``u[ch]`` (signal gain ** light exponent)."""
    out = {}
    for ch in _CHANNELS:
        ga = interpolated_gain(after[ch], u[ch], kind)
        gb = interpolated_gain(before[ch], u[ch] * (len(before[ch]) - 1) / (len(after[ch]) - 1), kind) \
            if before is not None else 1.0
        out[ch] = (ga / gb) ** light_exponent if gb > 0 else 1.0
    return out


def _srgb_matrix(white_xy: tuple[float, float]):
    return rgb_to_xyz_matrix(_SRGB[0][0], _SRGB[0][1], _SRGB[1][0], _SRGB[1][1], _SRGB[2][0], _SRGB[2][1],
                             white_xy[0], white_xy[1], white_Y=1.0)


def commanded_light(ratios: Mapping[str, float], white_xy: tuple[float, float] = (0.3127, 0.3290)) -> float:
    """ln of the grey's luminance ratio for per-channel light ratios (the solver's sRGB basis)."""
    m = _srgb_matrix(white_xy)
    num = sum(m[1][j] * float(ratios[ch]) for j, ch in enumerate(_CHANNELS))
    return math.log(num / sum(m[1])) if num > 0 else 0.0


def carry_through(xyz: Sequence[float], ratios: Mapping[str, float],
                  white_xy: tuple[float, float] = (0.3127, 0.3290)) -> list[float]:
    """A read carried through per-channel light ratios (the solver's sRGB basis): the PREDICTED read."""
    m = _srgb_matrix(white_xy)
    rgb = matvec(invert3x3(m), [float(v) for v in xyz])
    return list(matvec(m, [rgb[j] * float(ratios[ch]) for j, ch in enumerate(_CHANNELS)]))


def _err(xyz: Sequence[float], target_y: float, config: GrayTouchupConfig) -> dict[str, Any]:
    return point_error(GrayTouchupPatch(level=0.0, measured_xyz=tuple(float(v) for v in xyz),  # type: ignore[arg-type]
                                        target_y=float(target_y)), config)


def _uv(xyz: Sequence[float]) -> tuple[float, float]:
    x, y, z = (float(v) for v in xyz)
    d = x + 15.0 * y + 3.0 * z
    return (4.0 * x / d, 9.0 * y / d) if d > 0 else (0.0, 0.0)


def _white_uv(white_xy: tuple[float, float]) -> tuple[float, float]:
    x, y = white_xy
    d = -2.0 * x + 12.0 * y + 3.0
    return 4.0 * x / d, 9.0 * y / d


def chroma_de(xyz: Sequence[float], ref_xyz: Sequence[float], white_xy: tuple[float, float]) -> float:
    """dE2000 between two stimuli's CHROMATICITIES alone (both brought to the same luminance) - the colour
    part of the touch-up's own point metric, with the light part removed."""
    if float(xyz[1]) <= 0 or float(ref_xyz[1]) <= 0:
        return float("nan")
    w = white_xyz(1.0, white_xy[0], white_xy[1])
    a = tuple(float(v) / float(xyz[1]) for v in xyz)
    b = tuple(float(v) / float(ref_xyz[1]) for v in ref_xyz)
    return float(delta_e2000(xyz_to_lab(a, w), xyz_to_lab(b, w)))   # type: ignore[arg-type]


def grey_metrics(xyz: Sequence[float], target_y: float, white_xy: tuple[float, float]) -> dict[str, float]:
    """The two things a grey can get wrong, measured separately: LIGHT ``e = ln(Y / target)`` (signed;
    its error is ``|e|``) and CHROMATICITY ``c`` = the dE2000 of its chromaticity alone against the target
    white (:func:`chroma_de` - the touch-up's own metric with the light taken out). ``du``/``dv`` = its u'v'
    offset from the target white, for the record."""
    y = float(xyz[1])
    wu, wv = _white_uv(white_xy)
    u, v = _uv(xyz)
    return {"e": math.log(y / target_y) if y > 0 and target_y > 0 else float("nan"),
            "c": chroma_de(xyz, xy_to_XYZ(white_xy[0], white_xy[1], 1.0), white_xy),
            "du": u - wu, "dv": v - wv}


def _se(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) / math.sqrt(len(vals))


def _read_summary(xyz: Sequence[float], target_y: float, config: GrayTouchupConfig) -> dict[str, Any]:
    e = _err(xyz, target_y, config)
    x, y = e["measured_xy"]
    gm = grey_metrics(xyz, target_y, config.white_xy)
    return {"Y": e["measured_Y"],
            "Y_err_pct": round(100.0 * (math.exp(gm["e"]) - 1.0), 3) if math.isfinite(gm["e"]) else None,
            "chroma_de": round(gm["c"], 4),
            "dxy": [round(x - config.white_xy[0], 5), round(y - config.white_xy[1], 5)],
            "de2000": e["de2000"]}


def _local(pairs: Sequence[Mapping[str, Any]], code: int, key: str) -> tuple[float, float, list[int]]:
    near = sorted(pairs, key=lambda p: (abs(int(p["code"]) - code), int(p["code"])))[:MIN_SCALE_SAMPLES]
    med, sig = _mad_scale([float(p[key]) for p in near])
    return med, sig, [int(p["code"]) for p in near]


def judge_held_out(greys: Sequence[Mapping[str, Any]], after: Mapping[int, Sequence[float]],
                   null_pairs: Sequence[Mapping[str, Any]], *, config: GrayTouchupConfig,
                   alpha: float = FAMILY_ALPHA,
                   bracket: Optional[Sequence[Mapping[str, Any]]] = None,
                   after_reads: Optional[Mapping[int, Sequence[Sequence[float]]]] = None,
                   light_tol: float = math.log(1.01), chroma_tol: float = 0.6) -> dict[str, Any]:
    """Judge the touch-up at greys it was NOT fitted on, LIGHT and CHROMATICITY separately (review item 5:
    one scalar dE hid code 160 going -3.6 % in light while its dE improved), each against the grey's OWN
    before-read and its own repeatability. Either regressing counts.

    ``greys``: ``code``, ``target_Y``, ``before_xyz``, ``before_reads``, optional ``commanded_e`` (ln of the
    light ratio the committed curve commands at that code against the before table - known exactly from
    the curves) and, for a held / smoothed node, ``predicted_xyz`` (the before-read carried through the
    curve change: judged against the PREDICTION, not by the regression rule).

    Repeatability, per metric m in {e, c}, all measured in this run:
      * the grey's own repeat reads: the SE of its before reads and of its after reads (``after_reads``;
        without them the after SE is taken equal to the before SE);
      * BRACKET (drift over the touch-up session): greys re-read in the before state late in the session;
        ``d = m(late) - m(early)``; median = the drift carried into the comparison, 1.4826 MAD = its
        scatter; needs :data:`MIN_SCALE_SAMPLES` re-reads;
      * NULL (session -> verify): solved nodes whose committed value is exactly the table their last
        session read saw (``null_pairs``; moved nodes are never used - nothing is predicted), read again on
        the bake; median and MAD over the :data:`MIN_SCALE_SAMPLES` pairs nearest in code. With fewer than
        that in total the null is not used and the digest says so; the 8 nearest are taken however far
        they lie, and each grey reports the code span its pairs came from.
    change_m = m_after - drift_bracket - drift_null vs m_before (light: |e| after vs |e| before; chroma:
    c after vs c before); sigma_m = sqrt(SE_b^2 + SE_a^2 + MAD_bracket^2 + MAD_null^2), floored at
    :data:`SCALE_FLOOR`; regressed when change_m > z sigma_m, z = one-sided Bonferroni over 2 x J tests at
    ``alpha``. LIGHT regresses only when the curve's own command explains it (``|e_b + commanded| - |e_b|``
    beyond the same threshold): light that moved where the curve commanded ~nothing - a drive change across
    a tooth of a sawtooth-light panel, or drift the bracket did not see - is listed as
    ``uncommanded_light`` with its size, as evidence, not as the curve's regression. Without
    ``commanded_e`` no gating is applied."""
    wxy = config.white_xy
    pairs = []
    for p in null_pairs:
        try:
            a, f = grey_metrics(p["verify_xyz"], p["target_Y"], wxy), grey_metrics(p["final_xyz"], p["target_Y"], wxy)
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(a["e"]) and math.isfinite(f["e"]):
            pairs.append({"code": int(p["code"]), "de": a["e"] - f["e"], "dc": a["c"] - f["c"]})
    null_ok = len(pairs) >= MIN_SCALE_SAMPLES
    brk = []
    for b in bracket or ():
        try:
            e0, e1 = grey_metrics(b["early_xyz"], b["target_Y"], wxy), grey_metrics(b["late_xyz"], b["target_Y"], wxy)
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(e0["e"]) and math.isfinite(e1["e"]):
            brk.append({"code": int(b["code"]), "de": e1["e"] - e0["e"], "dc": e1["c"] - e0["c"]})
    brk_ok = len(brk) >= MIN_SCALE_SAMPLES
    b_e = _mad_scale([b["de"] for b in brk]) if brk_ok else (0.0, 0.0)
    b_c = _mad_scale([b["dc"] for b in brk]) if brk_ok else (0.0, 0.0)

    rows: list[dict[str, Any]] = []
    for g in greys:
        code = int(g["code"])
        ty = float(g["target_Y"])
        row: dict[str, Any] = {"code": code, "kind": g.get("kind", "between"),
                               "between_nodes": g.get("between_nodes"), "target_Y": round(ty, 5)}
        bxyz = g.get("before_xyz")
        axyz = after.get(code)
        if bxyz is None or axyz is None:
            row.update(judged=False, reason=("no usable before read" if bxyz is None else "not measured by the verify"))
            rows.append(row)
            continue
        mb, ma = grey_metrics(bxyz, ty, wxy), grey_metrics(axyz, ty, wxy)
        b_reads = [grey_metrics(r, ty, wxy) for r in g.get("before_reads") or ()]
        a_reads = [grey_metrics(r, ty, wxy) for r in (after_reads or {}).get(code) or ()]
        se_be, se_bc = _se([r["e"] for r in b_reads]), _se([r["c"] for r in b_reads])
        se_ae, se_ac = _se([r["e"] for r in a_reads]), _se([r["c"] for r in a_reads])
        if se_ae is None:
            se_ae = se_be
        if se_ac is None:
            se_ac = se_bc
        n_e = n_c = (0.0, 0.0)
        span = None
        if null_ok:
            me, se_, codes_e = _local(pairs, code, "de")
            mc, sc, _codes = _local(pairs, code, "dc")
            n_e, n_c, span = (me, se_), (mc, sc), [min(codes_e), max(codes_e)]
        have_noise = se_be is not None or null_ok or brk_ok
        row.update(before=_read_summary(bxyz, ty, config), after=_read_summary(axyz, ty, config),
                   null_span_codes=span)
        if not have_noise:
            row.update(judged=False, reason=f"no repeatability estimate (a single read, fewer than "
                                            f"{MIN_SCALE_SAMPLES} unmoved node pairs and bracket re-reads)")
            rows.append(row)
            continue
        sig_e = max(math.sqrt((se_be or 0.0) ** 2 + (se_ae or 0.0) ** 2 + b_e[1] ** 2 + n_e[1] ** 2), SCALE_FLOOR)
        sig_c = max(math.sqrt((se_bc or 0.0) ** 2 + (se_ac or 0.0) ** 2 + b_c[1] ** 2 + n_c[1] ** 2), SCALE_FLOOR)
        e_a = ma["e"] - b_e[0] - n_e[0]
        c_a = ma["c"] - b_c[0] - n_c[0]
        light_change = abs(e_a) - abs(mb["e"])
        chroma_change = c_a - mb["c"]
        cmd = g.get("commanded_e")
        row.update(judged=True,
                   light={"before_pct": round(100.0 * mb["e"], 3), "after_pct": round(100.0 * ma["e"], 3),
                          "drift_pct": round(100.0 * (b_e[0] + n_e[0]), 3),
                          "change_pct": round(100.0 * light_change, 3), "sigma_pct": round(100.0 * sig_e, 4),
                          "commanded_pct": round(100.0 * float(cmd), 3) if cmd is not None else None},
                   chroma={"before_de": round(mb["c"], 4), "after_de": round(ma["c"], 4),
                           "drift_de": round(b_c[0] + n_c[0], 4),
                           "change_de": round(chroma_change, 4), "sigma_de": round(sig_c, 4)},
                   _le=light_change, _ce=chroma_change, _se=sig_e, _sc=sig_c, _eb=mb["e"], _cmd=cmd)
        if g.get("predicted_xyz") is not None:
            mp = grey_metrics(g["predicted_xyz"], ty, wxy)
            row["predicted"] = {"light_pct": round(100.0 * mp["e"], 3), "chroma_de": round(mp["c"], 4),
                                "_de": ma["e"] - b_e[0] - n_e[0] - mp["e"],
                                "_dc": chroma_de(axyz, g["predicted_xyz"], wxy),
                                "note": g.get("predicted_note")}
        rows.append(row)
    judged = [r for r in rows if r.get("judged")]
    regular = [r for r in judged if "predicted" not in r]
    predicted = [r for r in judged if "predicted" in r]
    n_tests = 2 * len(judged)
    z = NormalDist().inv_cdf(1.0 - alpha / n_tests) if n_tests else None
    for r in regular:
        th_e, th_c = max(z * r["_se"], light_tol), max(z * r["_sc"], chroma_tol)   # type: ignore[operator]
        light_reg = r["_le"] > th_e
        explained = True
        if r["_cmd"] is not None:
            explained = (abs(r["_eb"] + float(r["_cmd"])) - abs(r["_eb"])) > th_e
        r["light"].update(threshold_pct=round(100.0 * th_e, 4), regressed=bool(light_reg and explained),
                          uncommanded=bool(light_reg and not explained))
        r["chroma"].update(threshold_de=round(th_c, 4), regressed=bool(r["_ce"] > th_c))
        r["regressed"] = bool(r["light"]["regressed"] or r["chroma"]["regressed"])
        r["score"] = round(max(r["_le"] / r["_se"], r["_ce"] / r["_sc"]), 3)
    for r in predicted:
        p = r["predicted"]
        ok_e = abs(p.pop("_de")) <= max(z * r["_se"], light_tol)      # type: ignore[operator]
        ok_c = p.pop("_dc") <= max(z * r["_sc"], chroma_tol)           # type: ignore[operator]
        p.update(as_predicted=bool(ok_e and ok_c), light_ok=bool(ok_e), chroma_ok=bool(ok_c))
        r["regressed"] = False
    for r in judged:
        for k in ("_le", "_ce", "_se", "_sc", "_eb", "_cmd"):
            r.pop(k, None)
    regressed = [r for r in regular if r["regressed"]]
    worst = sorted(regular, key=lambda r: r["score"], reverse=True)[:5]
    return {
        "available": bool(judged),
        "rule": ("per grey, LIGHT (|ln(Y/target)|) and CHROMATICITY (dE2000 of the chromaticity alone vs the target "
                 "white) separately, after (drift-corrected: bracket re-reads of the before state late in the session, "
                 "plus the null of unmoved nodes session -> verify) vs the grey's own before-read; sigma = "
                 "sqrt(SE_before^2 + SE_after^2 + MAD_bracket^2 + MAD_null^2); regressed when the change > max(z x "
                 f"sigma, the touch-up's own tolerance: {100.0 * (math.exp(light_tol) - 1.0):.2g} % light, "
                 f"{chroma_tol:g} dE colour), z = one-sided Bonferroni over 2 x the judged greys at alpha {alpha:g}; "
                 "light counts only when the curve's own command explains it (else 'uncommanded light'); held / "
                 "smoothed node codes are judged against their predicted outcome within the same bounds"),
        "light_tol_pct": round(100.0 * (math.exp(light_tol) - 1.0), 3), "chroma_tol_de": chroma_tol,
        "alpha": alpha, "z_crit": round(z, 4) if z is not None else None,
        "n_greys": len(rows), "n_judged": len(regular), "n_regressed": len(regressed),
        "regressed_codes": [r["code"] for r in regressed],
        "light_regressed_codes": [r["code"] for r in regressed if r["light"]["regressed"]],
        "chroma_regressed_codes": [r["code"] for r in regressed if r["chroma"]["regressed"]],
        "uncommanded_light": [{"code": r["code"], "change_pct": r["light"]["change_pct"],
                               "commanded_pct": r["light"]["commanded_pct"]}
                              for r in regular if r["light"].get("uncommanded")],
        "improved_codes": [r["code"] for r in regular
                           if r["light"]["change_pct"] <= 0 and r["chroma"]["change_de"] <= 0],
        "predicted": [{"code": r["code"], "kind": r["kind"], **r["predicted"],
                       "light_after_pct": r["light"]["after_pct"], "chroma_after_de": r["chroma"]["after_de"]}
                      for r in predicted],
        "worst": [{"code": r["code"], "kind": r["kind"], "regressed": r["regressed"], "score": r["score"],
                   "light": r["light"], "chroma": r["chroma"], "before": r["before"], "after": r["after"]}
                  for r in worst],
        "bracket": {"n": len(brk), "used": brk_ok,
                    "drift_light_pct": round(100.0 * b_e[0], 4), "scatter_light_pct": round(100.0 * b_e[1], 4),
                    "drift_chroma_de": round(b_c[0], 4), "scatter_chroma_de": round(b_c[1], 4),
                    "note": None if brk_ok else f"fewer than {MIN_SCALE_SAMPLES} bracket re-reads: session drift not in sigma"},
        "null": {"n_pairs": len(pairs), "used": null_ok,
                 "note": None if null_ok else (f"only {len(pairs)} solved nodes kept their table unchanged (fewer than "
                                               f"{MIN_SCALE_SAMPLES}): the session -> verify drift is not in sigma"),
                 "pairs": [{"code": p["code"], "d_light_pct": round(100.0 * p["de"], 4), "d_chroma_de": round(p["dc"], 4)}
                           for p in pairs]},
        "greys": rows,
    }
