"""Verification metrics from Argyll TI3 measurements."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .colormath import rgb_to_xyz_matrix
from .events import EventWriter
from .gamut import point_in_triangle
from .mhc import Ti3Sample, white_xyz
from .runs import RunContext


SRGB_TO_XYZ_D65 = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)

# sRGB / Rec.709 primaries — the gamut the pipeline targets (same primaries the engine's
# colour.RGB_COLOURSPACES["sRGB"] uses in optimize).
SRGB_PRIMARIES = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))

# ---------------------------------------------------------------------------
# The §0 practical-core content zone — ONE definition, shared by the SCORED
# practical summary below and the dashboard's live ΔE core/limits split
# (dashboard.state imports these), so the scored number and the live number can
# never disagree about what "core" means (fable roadmap §0 / Phase 6).
# ---------------------------------------------------------------------------
# BT.2408 diffuse/graphics reference white: ~99% of graded content lives at or
# below this luminance and within Rec.709.
HDR_REF_WHITE_NITS = 203.0
# Small headroom so a target sitting exactly ON reference white classifies core.
CORE_Y_HEADROOM = 1.02
# Saturation ceiling of the near-neutral "tube" ((max-min)/max on the signal) —
# the same band the patch generator's near_neutral_tube_patches occupy and the
# Phase 2 density artifact reports (phase-2.md §2).
TUBE_SATURATION_MAX = 0.20
# Luminance-band edges (absolute cd/m²) — the Phase 2 density-artifact bands, so
# score buckets line up with the measured patch investment.
PRACTICAL_BAND_EDGES_NITS = (1.0, 10.0, 100.0, HDR_REF_WHITE_NITS)
PRACTICAL_BAND_LABELS = ("<1", "1-10", "10-100", "100-203", ">203")


def is_core_target(target_xy: tuple[float, float] | None, target_y_nits: float | None) -> bool:
    """Is a patch's TARGET in the §0 practical core — inside Rec.709 at/below the
    BT.2408 diffuse-white band (≤ ~203 nit)? ``target_y_nits=None`` (unknown luminance)
    counts as core when the chromaticity is inside Rec.709 — matching the dashboard's
    live split, which classifies retroactively as data arrives. ``target_xy=None``
    (degenerate chromaticity, e.g. a black target) counts as core: a neutral dark
    target is the practical core by definition."""
    if target_xy is None:
        return True
    if not point_in_triangle(target_xy, *SRGB_PRIMARIES):
        return False
    return target_y_nits is None or target_y_nits <= HDR_REF_WHITE_NITS * CORE_Y_HEADROOM


def sanitize_reachable_primaries(prim: dict | None) -> dict | None:
    """Degenerate-guard a ``{"R": [x, y], "G": [...], "B": [...]}`` native-primaries dict
    (#C3): a collinear/point triangle would make the native NPM singular inside the gamut
    clamp, and real panel primaries are never collinear — so near-zero area ⇒ ``None``
    (no clamp) rather than a crash. Shared by the live orchestrator and the stage tools."""
    if not prim or len(prim) != 3 or not all(ch in prim for ch in ("R", "G", "B")):
        return None
    (rx, ry), (gx, gy), (bx, by) = prim["R"], prim["G"], prim["B"]
    area = abs((gx - rx) * (by - ry) - (bx - rx) * (gy - ry)) / 2.0
    return prim if area > 1e-6 else None


def reachable_primaries_from_mhc_params(mhc_params: dict | None) -> dict | None:
    """The panel's measured native primaries from a run record's ``mhc_params`` block
    (``dlc_state.json``, persisted at build), in the ``{"R": [x, y], ...}`` shape the
    engine's gamut clamp takes — or ``None`` when absent/degenerate. This is the SAME
    first-preference source the live orchestrator's ``_reachable_primaries`` uses, so a
    stage-CLI score and the live verify clamp against the same measured gamut (P1)."""
    mp = (mhc_params or {}).get("primaries")
    if not mp or not all(k in mp for k in ("rx", "ry", "gx", "gy", "bx", "by")):
        return None
    prim = {"R": [float(mp["rx"]), float(mp["ry"])],
            "G": [float(mp["gx"]), float(mp["gy"])],
            "B": [float(mp["bx"]), float(mp["by"])]}
    return sanitize_reachable_primaries(prim)


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
    """One scored patch. ``de2000`` is the generic primary-ΔE carrier (CIEDE2000 on SDR,
    dE_ITP on HDR — the summary's ``metric`` label names the units). ``gamut_clamped``
    marks a target the reachable-gamut clamp MOVED — the patch is scored against the
    panel's gamut boundary ("at the gamut floor"), not the raw target."""
    rgb: tuple[float, float, float]
    measured_xyz: tuple[float, float, float]
    target_xyz: tuple[float, float, float]
    de2000: float
    grayscale: bool
    gamut_clamped: bool = False


@dataclass(frozen=True)
class MetricsSummary:
    """The scored-run summary every producer (live verify, intermediate stage scores,
    the score/report stage CLIs) emits in ONE shape (P4). The ``*_de2000`` field names
    are the generic ΔE carrier — ``metric`` names the actual units (CIEDE2000 / dE_ITP)."""
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
    p99_de2000: float = 0.0
    colour_avg_de2000: float | None = None   # None when the set has no colour patches

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
                  white_xy: tuple[float, float] | None = None,
                  reachable_primaries=None) -> tuple[list[PatchMetric], float]:
    """Score TI3 samples as CIEDE2000 vs the ideal target.

    ``white_xy`` is the run's RESOLVED target white (what stage_whitepoint fed into the MHC
    matrix + its grayscale refine, and the 3D-LUT target). When given, both the per-patch target and
    the Lab reference white are built from sRGB primaries + that white, so a non-D65 white
    (e.g. the SPD-derived CRT-like white at strength>0) is the GOAL rather than scored as error.
    When ``None`` (legacy callers), it falls back to textbook D65 — unchanged behaviour.

    ``reachable_primaries`` optionally clamps the SDR target to the measured native gamut for
    offline experiments. It is intentionally off in the production SDR path after CV gating found
    that clamp worse there. It lazy-imports the engine only when used, preserving the
    dependency-free default path."""
    if not samples:
        raise ValueError("no TI3 samples to score")
    target_luminance = luminance if luminance is not None else infer_target_luminance(samples)
    if white_xy is not None:
        matrix: tuple[tuple[float, ...], ...] | list[list[float]] = npm_for_white(white_xy)
        white = white_xyz(target_luminance, white_xy[0], white_xy[1])
    else:
        matrix = SRGB_TO_XYZ_D65
        white = white_xyz(target_luminance)
    clamped_targets = None
    clamped_mask: list[bool] | None = None
    if reachable_primaries is not None:
        from .engine.model import Target, TargetSpace
        target = Target.sdr_srgb_power(gamma=gamma, white_nits=target_luminance, white_xy=white_xy)
        signals = [s.rgb for s in samples]
        clamped_targets = TargetSpace(target, reachable_primaries=reachable_primaries).ideal_xyz(signals)
        # Which targets did the clamp MOVE? In-gamut rows come back bit-identical (the clip
        # is a no-op there), so any real difference marks an at-the-gamut-floor patch.
        raw_targets = TargetSpace(target).ideal_xyz(signals)
        clamped_mask = [bool(max(abs(float(a) - float(b)) for a, b in zip(row_c, row_r)) > 1e-6)
                        for row_c, row_r in zip(clamped_targets, raw_targets)]

    metrics: list[PatchMetric] = []
    for sample in samples:
        meas = _finite_nonneg_xyz(sample.xyz)
        if clamped_targets is None:
            target = target_xyz_for_rgb(sample.rgb, target_luminance, gamma, matrix)
            clamped = False
        else:
            target = tuple(float(c) for c in clamped_targets[len(metrics)])
            clamped = clamped_mask[len(metrics)] if clamped_mask else False
        de = delta_e2000(xyz_to_lab(meas, white), xyz_to_lab(target, white))
        metrics.append(PatchMetric(sample.rgb, sample.xyz, target, de, is_grayscale(sample.rgb),
                                   gamut_clamped=clamped))
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
    clamped = res.get("gamut_clamped")
    metrics = [
        PatchMetric(s.rgb, s.xyz, tuple(float(c) for c in ideal_xyz[i]),
                    float(de_itp[i]), is_grayscale(s.rgb),
                    gamut_clamped=bool(clamped[i]) if clamped is not None else False)
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
    values = [m.de2000 for m in patch_metrics]
    grayscale = [m.de2000 for m in patch_metrics if m.grayscale]
    colour = [m.de2000 for m in patch_metrics if not m.grayscale]
    white_patch = max(patch_metrics, key=lambda m: sum(m.rgb))
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
        p99_de2000=percentile(values, 99),
        max_de2000=max(values),
        white_de2000=white_patch.de2000,
        grayscale_avg_de2000=(sum(grayscale) / len(grayscale)) if grayscale else 0.0,
        grayscale_max_de2000=max(grayscale) if grayscale else 0.0,
        colour_avg_de2000=(sum(colour) / len(colour)) if colour else None,
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


def _signal_saturation(rgb: tuple[float, float, float]) -> float:
    """Signal-space saturation ``(max-min)/max`` — the Phase 2 density artifact's measure
    (0 = grey axis, 1 = a pure primary/secondary). 0 for black (max <= 0)."""
    mx = max(rgb)
    return 0.0 if mx <= 0 else (mx - min(rgb)) / mx


def _bucket_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"avg": None, "p95": None, "max": None, "n": 0}
    return {"avg": round(sum(values) / len(values), 3),
            "p95": round(percentile(values, 95), 3),
            "max": round(max(values), 3), "n": len(values)}


def practical_summary(patch_metrics: list[PatchMetric], *, is_hdr: bool,
                      gamut_aware: bool = False) -> dict[str, Any]:
    """The §0 practically-weighted view of a scored set — the content-priority split that
    rides ALONGSIDE the raw avg/p95/max in every summary, so the number a human/LLM sees
    reads the run the way content does: neutral axis and the Rec.709-volume core first,
    reachability frontier last, and never traded against each other.

    The weighting IS the measured patch investment: DLC's patch geography already spends
    its budget where content lives (the neutral tube, the shadow toe, the low-mid bands —
    phase-2.md §2), so an equal-per-patch average WITHIN each zone is already
    luminance-frequency weighted by construction; no invented scalar weights.

    Zones (targets classified with the SAME constants the dashboard's live core/limits
    split uses — :func:`is_core_target`):

    * ``core``    — target inside Rec.709 at/below diffuse white (~203 nit), reachable.
      **The practical verdict.** For an SDR run every unclamped target is core by
      construction (sRGB targets at the OSD-set white).
    * ``limits``  — reachable but outside the core (wide-gamut and/or >203 nit): honest,
      rarely-hit territory; never the headline.
    * ``clamped`` — the target itself is beyond the panel's measured gamut and was scored
      against the reachable boundary ("at the gamut floor"): a reachability fact, not a
      calibration miss. Empty unless ``gamut_aware`` (the HDR #C3 clamp).

    Plus the two §0 honesty breakdowns that keep a flattering average from hiding a
    visible defect: ``tube`` (neutral + near-neutral ≤ 0.20 saturation — where a cast is
    most visible) and ``bands`` (the Phase 2 luminance bands — a low-light drift shows up
    in ``<1``/``1-10`` no matter how good the overall average looks)."""
    zones: dict[str, list[float]] = {"core": [], "limits": [], "clamped": []}
    tube: list[float] = []
    bands: dict[str, list[float]] = {label: [] for label in PRACTICAL_BAND_LABELS}
    for m in patch_metrics:
        x, y, z = m.target_xyz
        total = x + y + z
        target_xy = (x / total, y / total) if total > 1e-9 else None
        target_y = y
        if m.gamut_clamped:
            zones["clamped"].append(m.de2000)
        elif not is_hdr or is_core_target(target_xy, target_y):
            zones["core"].append(m.de2000)
        else:
            zones["limits"].append(m.de2000)
        if m.grayscale or _signal_saturation(m.rgb) <= TUBE_SATURATION_MAX:
            tube.append(m.de2000)
        band_idx = sum(1 for edge in PRACTICAL_BAND_EDGES_NITS if target_y > edge)
        bands[PRACTICAL_BAND_LABELS[band_idx]].append(m.de2000)
    return {
        "gamut_aware": bool(gamut_aware),
        "core": _bucket_stats(zones["core"]),
        "limits": _bucket_stats(zones["limits"]),
        "clamped": _bucket_stats(zones["clamped"]),
        "tube": _bucket_stats(tube),
        "bands": {label: _bucket_stats(vals) for label, vals in bands.items()},
    }


def metrics_scored_payload(summary: MetricsSummary, *, label: str,
                           practical: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ONE ``metrics_scored`` event shape every producer emits (P4) — the live
    orchestrator passes it to ``runlog.metrics_scored``, the stage tools to
    ``EventWriter`` — so the dashboard's ΔE panel/history render identically whichever
    path scored the run. Keys ride the generic ``*_de2000`` carrier; ``metric`` names
    the units; ``practical`` (when given) carries the §0 core/limits/clamped split."""
    payload: dict[str, Any] = {
        "label": label,
        "iteration": summary.iteration,
        "metric": summary.metric,
        "avg_de2000": round(summary.avg_de2000, 3),
        "p95_de2000": round(summary.p95_de2000, 3),
        "p99_de2000": round(summary.p99_de2000, 3),
        "max_de2000": round(summary.max_de2000, 3),
        "white_de2000": round(summary.white_de2000, 3),
        "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 3),
        "colour_avg_de2000": (round(summary.colour_avg_de2000, 3)
                              if summary.colour_avg_de2000 is not None else None),
        "patch_count": summary.patch_count,
        "grayscale_count": summary.grayscale_count,
    }
    if practical is not None:
        payload["practical"] = practical
    return payload


def _strict_json_patch_rows(patch_metrics: list[PatchMetric]) -> list[dict[str, Any]]:
    """Per-patch rows safe for STRICT JSON. ``measured_xyz`` is the RAW meter read (kept
    raw on purpose — the artifact is evidence), so a dropped/saturated read can carry
    NaN/inf; ``json.dumps`` would emit bare ``NaN`` tokens, which Python re-parses but a
    browser's ``JSON.parse`` (the dashboard's ``/api/patch_metrics``) throws on. Map
    non-finite components to ``null`` — an honest "no usable number" — and leave every
    finite value untouched. The scored ``de2000`` is always finite (it is computed from
    the sanitized copy — see ``_finite_nonneg_xyz``)."""
    rows = []
    for metric in patch_metrics:
        row = asdict(metric)
        row["measured_xyz"] = tuple(c if math.isfinite(c) else None for c in metric.measured_xyz)
        rows.append(row)
    return rows


def write_metrics(
    *,
    ctx: RunContext,
    phase: str,
    iteration: int,
    source: Path,
    patch_metrics: list[PatchMetric],
    target_luminance: float,
    metric: str = "CIEDE2000",
    practical: dict[str, Any] | None = None,
    label: str | None = None,
    emit_event: bool = True,
) -> MetricsSummary:
    """Persist a scored set as the run's metrics artifacts + spine event — the ONE
    producer of the ``*_metrics.json`` / ``*_patch_metrics.json`` shapes (the dashboard's
    ``/api/patch_metrics`` globs for the latter) and of the canonical ``metrics_scored``
    event (P4). Takes PRE-SCORED patch metrics so every caller keeps its own mode-gated
    scorer (CIEDE2000 SDR / dE_ITP HDR, resolved white, gamut clamp) — this function only
    summarizes, serializes, and emits. ``emit_event=False`` for callers that emit through
    their own phase-stamped :class:`RunLog` (the live orchestrator) to avoid a double event."""
    output_dir = ctx.root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / f"{phase}_iter{iteration:02d}_metrics.json"
    patches_path = output_dir / f"{phase}_iter{iteration:02d}_patch_metrics.json"
    summary = summarize_metrics(
        phase=phase,
        iteration=iteration,
        source=source,
        patch_metrics=patch_metrics,
        target_luminance=target_luminance,
        metrics_path=metrics_path,
        patches_path=patches_path,
        metric=metric,
    )
    # allow_nan=False: if a non-finite ever reaches these artifacts again it fails HERE,
    # loudly, instead of writing JSON a browser cannot parse.
    doc = summary.as_dict()
    if practical is not None:
        doc["practical"] = practical
    metrics_path.write_text(json.dumps(doc, indent=2, allow_nan=False), encoding="utf-8")
    patches_path.write_text(
        json.dumps(_strict_json_patch_rows(patch_metrics), indent=2, allow_nan=False),
        encoding="utf-8")
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
    if emit_event:
        EventWriter(ctx.events_path).write(
            "INFO",
            f"{phase}_metrics",
            "metrics_scored",
            tier="digest",
            **metrics_scored_payload(summary, label=label or phase, practical=practical),
        )
    return summary

