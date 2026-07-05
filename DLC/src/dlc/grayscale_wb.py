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

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

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
