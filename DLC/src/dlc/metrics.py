"""Verification metrics from Argyll TI3 measurements."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .colormath import rgb_to_xyz_matrix
from .events import EventWriter
from .mhc import Ti3Sample, parse_ti3, resolve_run_path, white_xyz
from .runs import RunContext


SRGB_TO_XYZ_D65 = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)

# sRGB / Rec.709 primaries — the gamut the pipeline targets (same primaries the engine's
# colour.RGB_COLOURSPACES["sRGB"] uses in optimize).
SRGB_PRIMARIES = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))


def npm_for_white(white_xy: tuple[float, float],
                  primaries: tuple[tuple[float, float], ...] = SRGB_PRIMARIES) -> list[list[float]]:
    """Normalized primary matrix RGB(linear)→XYZ for ``primaries`` + ``white_xy``, normalized
    so RGB(1,1,1) maps to the white at Y=1 (row-major: ``XYZ = matrix @ linear_rgb``). Reuses
    the tested :func:`colormath.rgb_to_xyz_matrix` — the same construction the engine's
    TargetSpace uses (sRGB primaries, whitepoint replaced), so verify and optimize share one
    target white. At D65 it equals ``SRGB_TO_XYZ_D65`` to ~2e-4."""
    (rx, ry), (gx, gy), (bx, by) = primaries
    return rgb_to_xyz_matrix(rx, ry, gx, gy, bx, by, white_xy[0], white_xy[1])


@dataclass(frozen=True)
class PatchMetric:
    rgb: tuple[float, float, float]
    measured_xyz: tuple[float, float, float]
    target_xyz: tuple[float, float, float]
    de2000: float
    grayscale: bool


@dataclass(frozen=True)
class MetricsSummary:
    phase: str
    iteration: int
    source: str
    metric: str
    patch_count: int
    grayscale_count: int
    target_luminance: float
    avg_de2000: float
    p95_de2000: float
    max_de2000: float
    white_de2000: float
    grayscale_avg_de2000: float
    grayscale_max_de2000: float
    metrics_path: str | None
    patches_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def target_xyz_for_rgb(rgb: tuple[float, float, float], luminance: float, gamma: float,
                       matrix: tuple[tuple[float, ...], ...] | list[list[float]] = SRGB_TO_XYZ_D65) -> tuple[float, float, float]:
    linear = tuple(max(0.0, min(1.0, channel)) ** gamma for channel in rgb)
    return tuple(luminance * sum(row[i] * linear[i] for i in range(3)) for row in matrix)  # type: ignore[return-value]


def xyz_to_lab(xyz: tuple[float, float, float], white: tuple[float, float, float]) -> tuple[float, float, float]:
    def f(value: float) -> float:
        epsilon = 216 / 24389
        kappa = 24389 / 27
        return value ** (1 / 3) if value > epsilon else (kappa * value + 16) / 116

    # Clamp the relative tristimulus to >= 0: a dark/noisy measurement can read slightly
    # negative XYZ, which otherwise produces garbage Lab (and can quietly corrupt the dE
    # accept/iterate verdict). Clamping to 0 maps it to legitimate black.
    xr = max(0.0, xyz[0] / white[0]) if white[0] else 0.0
    yr = max(0.0, xyz[1] / white[1]) if white[1] else 0.0
    zr = max(0.0, xyz[2] / white[2]) if white[2] else 0.0
    fx, fy, fz = f(xr), f(yr), f(zr)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt((c_bar**7) / ((c_bar**7) + (25**7)))) if c_bar else 0.0
    ap1 = (1 + g) * a1
    ap2 = (1 + g) * a2
    cp1 = math.hypot(ap1, b1)
    cp2 = math.hypot(ap2, b2)
    hp1 = hue_angle(ap1, b1)
    hp2 = hue_angle(ap2, b2)
    delta_lp = l2 - l1
    delta_cp = cp2 - cp1
    if cp1 * cp2 == 0:
        delta_hp = 0.0
    else:
        diff = hp2 - hp1
        if diff > 180:
            diff -= 360
        elif diff < -180:
            diff += 360
        delta_hp = diff
    delta_hp_term = 2 * math.sqrt(cp1 * cp2) * math.sin(math.radians(delta_hp / 2))
    l_bar = (l1 + l2) / 2
    cp_bar = (cp1 + cp2) / 2
    if cp1 * cp2 == 0:
        hp_bar = hp1 + hp2
    else:
        diff = abs(hp1 - hp2)
        if diff <= 180:
            hp_bar = (hp1 + hp2) / 2
        elif hp1 + hp2 < 360:
            hp_bar = (hp1 + hp2 + 360) / 2
        else:
            hp_bar = (hp1 + hp2 - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    delta_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    rc = 2 * math.sqrt((cp_bar**7) / ((cp_bar**7) + (25**7))) if cp_bar else 0.0
    sl = 1 + ((0.015 * ((l_bar - 50) ** 2)) / math.sqrt(20 + ((l_bar - 50) ** 2)))
    sc = 1 + 0.045 * cp_bar
    sh = 1 + 0.015 * cp_bar * t
    rt = -math.sin(math.radians(2 * delta_theta)) * rc
    value = (
        (delta_lp / sl) ** 2
        + (delta_cp / sc) ** 2
        + (delta_hp_term / sh) ** 2
        + rt * (delta_cp / sc) * (delta_hp_term / sh)
    )
    return math.sqrt(max(0.0, value))


def hue_angle(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    angle = math.degrees(math.atan2(b, a))
    return angle + 360 if angle < 0 else angle


def _finite_nonneg_xyz(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    """Sanitize a measured XYZ before scoring. A dropped/saturated meter read can be NaN/inf or
    negative; scoring it directly NaN-poisons the CIEDE2000 avg/p95 (and ``max()`` can hide it).
    Non-finite -> 0.0, finite negatives -> 0.0, so a bad read scores a large FINITE error that
    surfaces instead. Mirrors the HDR scorer's ``nan_to_num``+clip guard in ``engine.model.score_hdr``."""
    return tuple(max(c, 0.0) if math.isfinite(c) else 0.0 for c in xyz)  # type: ignore[return-value]


def infer_target_luminance(samples: list[Ti3Sample]) -> float:
    def lum(sample: Ti3Sample) -> float:
        y = sample.xyz[1]
        return y if (math.isfinite(y) and y > 0.0) else 0.0
    whiteish = [lum(s) for s in samples if min(s.rgb) >= 0.99 and lum(s) > 0.0]
    if whiteish:
        return max(whiteish)
    grayscale = [lum(s) for s in samples if is_grayscale(s.rgb) and lum(s) > 0.0]
    if grayscale:
        return max(grayscale)
    finite = [lum(s) for s in samples if lum(s) > 0.0]
    return max(finite) if finite else 1.0  # all-dark/garbage set: safe nonzero (panel_dark caught upstream)


def is_grayscale(rgb: tuple[float, float, float]) -> bool:
    return abs(rgb[0] - rgb[1]) < 1e-6 and abs(rgb[1] - rgb[2]) < 1e-6


def score_samples(samples: list[Ti3Sample], *, luminance: float | None = None, gamma: float = 2.2,
                  white_xy: tuple[float, float] | None = None) -> tuple[list[PatchMetric], float]:
    """Score TI3 samples as CIEDE2000 vs the ideal target.

    ``white_xy`` is the run's RESOLVED target white (what stage_whitepoint fed into the MHC
    matrix, the 3D-LUT target, and the GS+WB tweak). When given, both the per-patch target and
    the Lab reference white are built from sRGB primaries + that white, so a non-D65 white
    (e.g. the SPD-derived CRT-like white at strength>0) is the GOAL rather than scored as error.
    When ``None`` (legacy callers), it falls back to textbook D65 — unchanged behaviour."""
    if not samples:
        raise ValueError("no TI3 samples to score")
    target_luminance = luminance if luminance is not None else infer_target_luminance(samples)
    if white_xy is not None:
        matrix: tuple[tuple[float, ...], ...] | list[list[float]] = npm_for_white(white_xy)
        white = white_xyz(target_luminance, white_xy[0], white_xy[1])
    else:
        matrix = SRGB_TO_XYZ_D65
        white = white_xyz(target_luminance)
    metrics: list[PatchMetric] = []
    for sample in samples:
        meas = _finite_nonneg_xyz(sample.xyz)
        target = target_xyz_for_rgb(sample.rgb, target_luminance, gamma, matrix)
        de = delta_e2000(xyz_to_lab(meas, white), xyz_to_lab(target, white))
        metrics.append(PatchMetric(sample.rgb, sample.xyz, target, de, is_grayscale(sample.rgb)))
    return metrics, target_luminance


def score_samples_hdr(samples: list[Ti3Sample], *, white_xy: tuple[float, float],
                      peak_nits: float, reachable_primaries=None) -> tuple[list[PatchMetric], float]:
    """Score TI3 samples for an **HDR (PQ/Rec.2020)** run in ``dE_ITP`` (BT.2124) — the
    perceptually-uniform metric the 3D-LUT cube converges in. The heavy PQ/ICtCp math is
    lazy-imported from :mod:`dlc.engine` (numpy/colour), so importing this spine module
    stays dependency-free; only the HDR path pulls the engine in.

    The returned :class:`PatchMetric` reuses the ``de2000`` field as the generic primary
    ΔE carrier (it holds **dE_ITP** here); the run/summary ``metric`` label disambiguates
    — callers must pass ``metric="dE_ITP"`` to :func:`summarize_metrics`. ``target_xyz``
    is the ideal absolute XYZ; ``target_luminance`` is the target ``peak_nits`` (reported,
    not used to rescale — PQ is absolute)."""
    if not samples:
        raise ValueError("no TI3 samples to score")
    from .engine.model import score_hdr

    res = score_hdr([s.rgb for s in samples], [s.xyz for s in samples], white_xy=white_xy,
                    reachable_primaries=reachable_primaries)
    de_itp = res["de_itp"]
    ideal_xyz = res["ideal_xyz"]
    metrics = [
        PatchMetric(s.rgb, s.xyz, tuple(float(c) for c in ideal_xyz[i]),
                    float(de_itp[i]), is_grayscale(s.rgb))
        for i, s in enumerate(samples)
    ]
    return metrics, float(peak_nits)


def summarize_metrics(
    *,
    phase: str,
    iteration: int,
    source: Path,
    patch_metrics: list[PatchMetric],
    target_luminance: float,
    metrics_path: Path | None = None,
    patches_path: Path | None = None,
    metric: str = "CIEDE2000",
) -> MetricsSummary:
    values = [metric.de2000 for metric in patch_metrics]
    grayscale = [metric.de2000 for metric in patch_metrics if metric.grayscale]
    white_patch = max(patch_metrics, key=lambda metric: sum(metric.rgb))
    return MetricsSummary(
        phase=phase,
        iteration=iteration,
        source=str(source),
        metric=metric,
        patch_count=len(patch_metrics),
        grayscale_count=len(grayscale),
        target_luminance=target_luminance,
        avg_de2000=sum(values) / len(values),
        p95_de2000=percentile(values, 95),
        max_de2000=max(values),
        white_de2000=white_patch.de2000,
        grayscale_avg_de2000=(sum(grayscale) / len(grayscale)) if grayscale else 0.0,
        grayscale_max_de2000=max(grayscale) if grayscale else 0.0,
        metrics_path=str(metrics_path) if metrics_path is not None else None,
        patches_path=str(patches_path) if patches_path is not None else None,
    )


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def write_metrics(
    *,
    ctx: RunContext,
    phase: str,
    iteration: int,
    source_ti3: Path,
    gamma: float = 2.2,
    luminance: float | None = None,
) -> MetricsSummary:
    source_ti3 = resolve_run_path(ctx, source_ti3)
    samples = parse_ti3(source_ti3)
    patch_metrics, target_luminance = score_samples(samples, luminance=luminance, gamma=gamma)
    output_dir = ctx.root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / f"{phase}_iter{iteration:02d}_metrics.json"
    patches_path = output_dir / f"{phase}_iter{iteration:02d}_patch_metrics.json"
    summary = summarize_metrics(
        phase=phase,
        iteration=iteration,
        source=source_ti3,
        patch_metrics=patch_metrics,
        target_luminance=target_luminance,
        metrics_path=metrics_path,
        patches_path=patches_path,
    )
    metrics_path.write_text(json.dumps(summary.as_dict(), indent=2), encoding="utf-8")
    patches_path.write_text(json.dumps([asdict(metric) for metric in patch_metrics], indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": f"{phase}_metrics",
            "iteration": iteration,
            "status": "scored",
            "metrics": str(metrics_path),
            "artifacts": {
                "metrics": str(metrics_path),
                "patch_metrics": str(patches_path),
            },
        }
    )
    ctx.save()
    ctx.log(f"Scored {phase} metrics iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        f"{phase}_metrics",
        "metrics_scored",
        iteration=iteration,
        avg_de2000=summary.avg_de2000,
        p95_de2000=summary.p95_de2000,
        max_de2000=summary.max_de2000,
        white_de2000=summary.white_de2000,
    )
    return summary

