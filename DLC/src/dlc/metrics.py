"""Verification metrics from Argyll TI3 measurements."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

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


def run_oog_mapping(calib: dict | None, default: str = "vertex") -> str:
    """The out-of-gamut target policy a run record (``dlc_state.json['calib']``) was built/scored
    with. The orchestrator memoises it (``calib['oog_mapping']``) at the run's first HDR score or
    cube build. A record that already holds measured stages but NO memo predates the 2026-09-23
    vertex policy — it was built and scored with the legacy "chroma-clip" clamp, and resuming or
    re-scoring it must keep that, not silently switch its verify to a different target. A fresh
    record gets ``default`` (the profile's policy)."""
    c = calib or {}
    memo = c.get("oog_mapping")
    if memo:
        return str(memo)
    stages = c.get("stages") or {}
    if any(str(k).startswith("measure:") for k in stages):
        return "chroma-clip"
    return default


# ---------------------------------------------------------------------------
# Level edge (design D4): the luminance-dependent confirmed gamut edge
# ---------------------------------------------------------------------------
LEVEL_EDGE_SWITCHES = ("off", "auto")
LEVEL_EDGE_STAGE_MODES = ("run", "off", "on")
# Stages whose presence (without a memo) marks a record built / verified before the level edge existed.
_LEVEL_EDGE_LEGACY_STAGES = ("build-install-3dlut", "measure:verify", "verify")


def level_edge_block(mhc_params: dict | None) -> dict | None:
    """The MHC build's persisted level-edge block (``mhc_params["level_edge"]``), or None."""
    block = (mhc_params or {}).get("level_edge")
    return block if isinstance(block, dict) else None


def run_level_edge(calib: dict | None, mhc_params: dict | None, *, switch: str = "off",
                   oog_mapping: str = "vertex", oog_solve: str = "direct", is_hdr: bool = True,
                   falsify: Any = None, white_xy: Any = None) -> tuple[Any, dict[str, Any]]:
    """Whether a run's 3D-LUT build + verify use the level edge — ``(gamut | None, memo)``; the orchestrator pins
    ``memo`` in ``calib["oog_level_edge"]`` = ``{enabled, key, reason, falsification}`` at the FIRST 3D-LUT build,
    so a resume and the stage CLIs score against the target the cube was built for.

    * A pinned memo is honoured: disabled ⇒ None; enabled ⇒ the gamut rebuilt from ``mhc_params`` — unless the
      block's key no longer matches (``memo["key_mismatch"] = True``, gamut None: the caller decides — a seam when a
      cube was already built for the pinned edge).
    * No memo but the record already built a cube / verified (a DONE stage record — an aborted attempt does not
      count) ⇒ OFF (a legacy record keeps scoring bit-identically).
    * Fresh record ⇒ enabled only when HDR ∧ switch "auto" ∧ oog_mapping "vertex" ∧ oog_solve "projection" ∧ the
      block's deterministic gates passed (status "ok") ∧ ``falsify(gamut)`` passes (no measured read > 1 JND outside
      the edge). A falsification that fails — or cannot decide — returns the gamut with ``enabled: False,
      falsified: True``: a judgment for the LLM at the ``level_edge_falsified`` seam, never silently applied.

    ``white_xy`` (the engine target's white): an edge anchored on another white is refused (fresh) or reported as a
    mismatch (pinned) — its anchors and gates were computed around its own white, and the target space refuses it.

    Every refusal carries its reason. Engine code is imported lazily (the spine stays dependency-free)."""
    c = calib or {}
    block = level_edge_block(mhc_params)
    cur_key = block.get("key") if block else None
    memo = c.get("oog_level_edge")
    if isinstance(memo, dict):
        rec = dict(memo)
        if not rec.get("enabled"):
            return None, rec
        if block is None or block.get("status") != "ok" or cur_key != rec.get("key"):
            rec.update(key_mismatch=True, current_key=cur_key)
            return None, rec
        if not _level_edge_white_matches(block, white_xy):
            rec.update(key_mismatch=True, current_key=cur_key, white_mismatch=True)
            return None, rec
        try:
            return _level_gamut(block), rec
        except ValueError as exc:                       # a block whose stored key no longer matches its content
            rec.update(key_mismatch=True, current_key=None, error=str(exc))
            return None, rec
    rec: dict[str, Any] = {"enabled": False, "key": cur_key, "reason": None, "falsification": None}
    stages = c.get("stages") or {}
    if any(isinstance(stages.get(k), dict) and stages[k].get("status", "done") == "done"
           for k in _LEVEL_EDGE_LEGACY_STAGES):
        rec["reason"] = "legacy record: a cube was built / verified before the level-edge memo existed"
        return None, rec
    if not is_hdr:
        rec["reason"] = "SDR run (the level edge is an HDR construct)"
    elif str(switch) != "auto":
        rec["reason"] = f"profile level_edge is {switch!r}"
    elif oog_mapping != "vertex":
        rec["reason"] = f"oog_mapping {oog_mapping!r} (the level edge rides the vertex map)"
    elif oog_solve != "projection":
        rec["reason"] = f"oog_solve {oog_solve!r} (the level edge ships with the projection solve only)"
    elif block is None:
        rec["reason"] = "the MHC build persisted no level-edge block"
    elif block.get("status") != "ok":
        rec["reason"] = f"level edge {block.get('status')}: {block.get('reason')}"
    elif not _level_edge_white_matches(block, white_xy):
        rec["reason"] = (f"the level edge is anchored on white {block.get('white_xy')}, the engine target uses "
                         f"{[round(float(c), 6) for c in white_xy]}")
    if rec["reason"] is not None:
        return None, rec
    try:
        gamut = _level_gamut(block)
    except ValueError as exc:
        rec["reason"] = f"level edge could not be built: {exc}"
        return None, rec
    if falsify is not None:
        fals = falsify(gamut)
        rec["falsification"] = fals
        if not (isinstance(fals, dict) and fals.get("passed") is True):
            rec.update(falsified=True, reason=("measured reads lie more than 1 JND outside the edge"
                                               if isinstance(fals, dict) and fals.get("passed") is False
                                               else "the falsification could not decide (no reads at the floor)"))
            return gamut, rec
    rec["enabled"] = True
    return gamut, rec


def stage_level_gamut(calib: dict | None, mhc_params: dict | None, *, mode: str = "run",
                      oog_mapping: str = "vertex", white_xy: Any = None) -> tuple[Any, str | None]:
    """The level gamut a stage CLI (score / report) scores with — ``(gamut | None, note)``. ``mode`` "run" (default)
    follows the run's pinned memo (enabled and the key still matching; anything else ⇒ None, the note says why);
    "off" never uses it; "on" forces the MHC build's block when its gates passed (vertex policy only) — an explicit
    what-if, labelled as such. ``white_xy`` (the scoring white): an edge anchored on a different white is not used
    (the note says so) — its hue anchors and gates were computed around its own white."""
    gamut, note = _stage_level_gamut(calib, mhc_params, mode=mode, oog_mapping=oog_mapping)
    if gamut is not None and white_xy is not None:
        gw = gamut.white_xy
        if max(abs(float(gw[0]) - float(white_xy[0])), abs(float(gw[1]) - float(white_xy[1]))) > 1e-6:
            return None, (f"level edge not used: it is anchored on white {tuple(round(float(c), 6) for c in gw)}, "
                          f"the score uses {tuple(round(float(c), 6) for c in white_xy)}")
    return gamut, note


def _stage_level_gamut(calib: dict | None, mhc_params: dict | None, *, mode: str,
                       oog_mapping: str) -> tuple[Any, str | None]:
    if mode not in LEVEL_EDGE_STAGE_MODES:
        raise ValueError(f"level-edge mode must be one of {LEVEL_EDGE_STAGE_MODES}, got {mode!r}")
    if mode == "off":
        return None, "level edge off (--level-edge off)"
    block = level_edge_block(mhc_params)
    if mode == "on":
        if oog_mapping != "vertex":
            return None, f"level edge needs the vertex OOG policy (run uses {oog_mapping!r})"
        if block is None or block.get("status") != "ok":
            return None, ("no level-edge block in the run record" if block is None
                          else f"level edge {block.get('status')}: {block.get('reason')}")
        try:
            return _level_gamut(block), "level edge forced on (--level-edge on)"
        except ValueError as exc:
            return None, f"level edge unusable: {exc}"
    memo = (calib or {}).get("oog_level_edge")
    if not isinstance(memo, dict):
        return None, None
    if not memo.get("enabled"):
        return None, f"level edge off for this run ({memo.get('reason')})"
    if block is None or block.get("status") != "ok" or block.get("key") != memo.get("key"):
        return None, "the run's pinned level-edge key no longer matches mhc_params.level_edge — scored full-drive"
    try:
        return _level_gamut(block), "level edge (the run's pinned memo)"
    except ValueError as exc:
        return None, f"the run's level-edge block is unusable ({exc}) — scored full-drive"


def _level_edge_white_matches(block: dict, white_xy: Any) -> bool:
    if white_xy is None:
        return True
    bw = block.get("white_xy") or (None, None)
    try:
        return max(abs(float(bw[0]) - float(white_xy[0])), abs(float(bw[1]) - float(white_xy[1]))) <= 1e-6
    except (TypeError, ValueError, IndexError):
        return False


def _level_gamut(block: dict) -> Any:
    from .engine.level_gamut import LevelGamut
    return LevelGamut.from_params(block)


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
                      peak_nits: float, reachable_primaries=None,
                      oog_mapping: str = "vertex") -> tuple[list[PatchMetric], float]:
    """Score TI3 samples for an **HDR (PQ/Rec.2020)** run in ``dE_ITP`` (BT.2124) — the
    perceptually-uniform metric the 3D-LUT cube converges in. The heavy PQ/ICtCp math is
    lazy-imported from :mod:`dlc.engine` (numpy/colour), so importing this spine module
    stays dependency-free; only the HDR path pulls the engine in.

    The returned :class:`PatchMetric` reuses the ``de2000`` field as the generic primary
    ΔE carrier (it holds **dE_ITP** here); the run/summary ``metric`` label disambiguates
    — callers must pass ``metric="dE_ITP"`` to :func:`summarize_metrics`. ``target_xyz``
    is the ideal absolute XYZ; ``target_luminance`` is the target ``peak_nits`` (reported,
    not used to rescale — PQ is absolute). ``oog_mapping`` is the out-of-gamut target policy
    (``Target.oog_mapping``; default the owner's 2026-09-23 "vertex" map) — pass the run's value so
    the score clamps exactly as the cube build did."""
    if not samples:
        raise ValueError("no TI3 samples to score")
    from .engine.model import score_hdr

    res = score_hdr([s.rgb for s in samples], [s.xyz for s in samples], white_xy=white_xy,
                    reachable_primaries=reachable_primaries, oog_mapping=oog_mapping)
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


bucket_stats = _bucket_stats   # the {avg, p95, max, n} shape every practical bucket uses (public)


# ---------------------------------------------------------------------------
# Content-weighted practical score (owner directive 2026-10-09: "practical numbers must lead").
# EVIDENCE ONLY — no gate reads any of it; the LLM judges it at the verify seam.
# ---------------------------------------------------------------------------
# The reach of the content kernel score (dE_ITP): ~ one 33-node PQ cube cell along I (study §5.1).
DEFAULT_CONTENT_REACH = 20.0
# Below this measured luminance a single colorimeter read is floor / noise-limited when the display
# has no characterized DIP noise floor (i1D3-class; the study's 0.05 nit).
METER_FLOOR_NITS_FALLBACK = 0.05
_METER_FLOOR_MEASURED_NOTE = "single reads below are noise-dominated"


@dataclass(frozen=True)
class MeterFloor:
    """The meter floor every consumer uses (:func:`resolve_meter_floor`): the read evidence's ``at_floor``
    flag, the black-aware score's ``below_meter_floor`` flag and the raw near-black floor fit's grey flags.
    ``measured`` is ``False`` for the documented fallback."""
    nits: float
    source: str
    measured: bool


def resolve_meter_floor(dip_noise_floor_nits: Any, *, where: str = "DIP noise_floor_nits") -> MeterFloor:
    """THE meter floor: the DIP's ``noise_floor_nits`` when it is finite and > 0, else
    :data:`METER_FLOOR_NITS_FALLBACK`. A missing or non-positive value means NOT MEASURED, never "no floor"
    (characterize clamps an all-clean noise survey to the full-field black, which is 0 on a local-dimming
    panel). ``where`` names the DIP the value came from; the fallback's source states the value it replaced."""
    try:
        v = float(dip_noise_floor_nits) if dip_noise_floor_nits is not None else None
    except (TypeError, ValueError):
        v = None
    if v is not None and math.isfinite(v) and v > 0.0:
        return MeterFloor(v, f"{where} ({_METER_FLOOR_MEASURED_NOTE})", True)
    why = ("no DIP noise floor" if dip_noise_floor_nits is None
           else f"{where} {dip_noise_floor_nits!r} is not a measured floor (non-positive = not measured)")
    return MeterFloor(METER_FLOOR_NITS_FALLBACK,
                      f"fallback {METER_FLOOR_NITS_FALLBACK:g} nit (i1D3-class single-read floor; {why})", False)


@dataclass(frozen=True)
class ReadEvidence:
    """How much each verify signal's ΔE can be trusted (the content-weighted block reports the share
    of content / score resting on weak evidence). ``reads`` = meter reads behind each signal (keyed by
    :func:`signal_key`; ``None`` → the scored rows per signal); ``low_snr`` = signals the measure loop's
    dark-level noise machinery flags (error within repeatability noise, or an unstable level);
    ``noise_floor_nits`` = below this MEASURED luminance a read is at the meter floor (always
    :func:`resolve_meter_floor`, the one value the black-aware score uses too). ``read_xyz`` =
    the meter reads (absolute XYZ) the scored value rests on — each patch's FINAL round, only the reads
    the measure loop kept (:func:`dlc.content_score.final_round_reads`); their spread is the signal's read
    noise. ``loop_se_de`` = the measure loop's own count-floored SE of the accepted mean (noise sidecar
    ``se_de``, ΔE2000) per grey signal; ``loop_round_se`` = the loop's own raw SE of each row's kept mean
    (its round records, ΔE2000) per signal — both the loop's own numbers, PREFERRED for SDR (the same
    metric family); HDR scores dE_ITP, so it uses the spread of ``read_xyz``. ``reads_basis`` describes
    where ``reads`` / ``read_xyz`` came from (the loop's round records, or a reconstruction)."""
    reads: Optional[Mapping[tuple, int]] = None
    low_snr: frozenset = frozenset()
    noise_floor_nits: float = METER_FLOOR_NITS_FALLBACK
    noise_floor_source: str = (f"fallback {METER_FLOOR_NITS_FALLBACK:g} nit (i1D3-class single-read floor; "
                               "no DIP noise floor)")     # = resolve_meter_floor(None)
    read_xyz: Optional[Mapping[tuple, Sequence[Any]]] = None
    loop_se_de: Optional[Mapping[tuple, float]] = None
    loop_round_se: Optional[Mapping[tuple, Sequence[Optional[float]]]] = None
    reads_basis: Optional[str] = None


@dataclass(frozen=True)
class ContentWeights:
    """Per-signal content weights carried by a content-sampled verify set (``--verify-patches-file``):
    ``weights`` keyed by :func:`signal_key`; Σ w·E / Σ w over the measured signals is the
    content-weighted score of the content the set was drawn from. ``coverage_gap_pct`` = the file's own
    stated gap per reach (``{"reach_20": 6.99, ...}`` — content with no patch nearby, as drawn)."""
    weights: Mapping[tuple, float]
    label: str
    source: Optional[str] = None
    coverage_gap_pct: Mapping[str, float] = field(default_factory=dict)


_TRUST_FLAGS = ("single_read", "at_floor", "low_snr", "single_read_at_floor", "weak")
_NOISE_FLAGS = ("noise_limited", "noise_unknown")
# The black-aware tags (present on the trust rows only when the black-aware pedestal applies).
_BLACK_FLAGS = ("floor_limited", "below_meter_floor")


def _scored_y(rep: PatchMetric) -> float:
    """The signal's measured luminance (the target's when the measurement has none)."""
    y = rep.measured_xyz[1]
    return float(y) if isinstance(y, (int, float)) and math.isfinite(y) else float(rep.target_xyz[1])


def _signal_trust(rep: PatchMetric, n_rows: int, evidence: ReadEvidence) -> dict[str, Any]:
    key = signal_key(rep.rgb)
    reads = int((evidence.reads or {}).get(key, n_rows)) if evidence.reads else int(n_rows)
    y = _scored_y(rep)
    single = reads <= 1
    floor = y < float(evidence.noise_floor_nits)
    low_snr = key in evidence.low_snr
    return {"reads": reads, "single_read": single, "at_floor": floor, "low_snr": low_snr,
            "single_read_at_floor": single and floor, "weak": single or floor or low_snr}


# A signal whose scored E is within this many of its own read-noise SEs is "noise-limited": the
# number is mostly meter noise (E[dE] ~ sqrt(true^2 + noise^2) — dE is a magnitude, so noise BIASES it up).
NOISE_LIMITED_SE = 1.0


def _signal_noise(groups: Sequence[tuple[PatchMetric, int]], evidence: ReadEvidence, *, is_hdr: bool,
                  white_xy: Optional[tuple[float, float]]) -> tuple[list[Optional[dict[str, Any]]], list[str]]:
    """Per signal: the read-noise SE (PER ROW) of its scored E in the run's metric, where DLC has it, +
    why not where it does not. Order of preference:

    * at / below the meter floor (scored Y <= ``noise_floor_nits``): NONE — ``noise_unknown``. A read
      spread there is no noise estimate (the dark early stop takes two agreeing reads; counts quantise:
      D1 [3,3,3] read 0,0,0 twice → SE 0.0175 claimed "not noise-limited");
    * SDR — the measure loop's OWN SE: the noise sidecar's count-floored ``se_de`` (multi-read grey
      levels), else its round records' raw SE of each row's kept mean (RMS over the signal's rows),
      floored at the meter's print quantisation; both are ΔE2000 against the loop's running white;
    * else the spread of the reads the scored value rests on (``read_xyz`` — final round, loop-kept
      reads only; :func:`dlc.content_score.read_noise_se`) — the only route for HDR (dE_ITP);
    * else NONE (a single read, nothing recorded)."""
    out: list[Optional[dict[str, Any]]] = [None] * len(groups)
    why: list[str] = ["no noise evidence recorded"] * len(groups)
    white = None
    if not is_hdr and groups:
        wy = max(float(m.target_xyz[1]) for m, _ in groups)
        wx, wyy = white_xy if white_xy is not None else (0.3127, 0.3290)
        white = white_xyz(wy, wx, wyy)
    floor = float(evidence.noise_floor_nits)
    for i, (m, rows) in enumerate(groups):
        key = signal_key(m.rgb)
        if _scored_y(m) <= floor:
            why[i] = f"at/below the meter floor ({floor:g} nit): a read spread there is not a noise estimate"
            continue
        reads = [r for r in ((evidence.read_xyz or {}).get(key) or ()) if r is not None]
        if not is_hdr:
            se = (evidence.loop_se_de or {}).get(key)
            if se is not None:
                out[i] = {"se": float(se), "basis": "measure-loop noise sidecar se_de (count-floored ΔE2000 SE of "
                                                    "the accepted mean, final round; loop's running white)"}
                continue
            row_se = list((evidence.loop_round_se or {}).get(key) or ())
            if row_se and all(v is not None for v in row_se):
                se = math.sqrt(sum(float(v) ** 2 for v in row_se) / len(row_se))
                if reads:
                    from .content_score import print_quantum_se

                    mean = tuple(sum(float(r[c]) for r in reads) / len(reads) for c in range(3))
                    se = max(se, print_quantum_se(mean, is_hdr=False, white_xyz=white))
                out[i] = {"se": se, "basis": "measure-loop round SE (raw ΔE2000 SE of each row's kept mean, "
                                             "final round; loop's running white), print-quantisation floored"}
                continue
        if len(reads) >= 2:
            from .content_score import read_noise_se

            res = read_noise_se(reads, rows=rows, is_hdr=is_hdr, white_xyz=white)
            if res is not None:
                out[i] = {**res, "basis": "spread of the final round's loop-kept reads"}
                continue
        why[i] = "single read behind the scored value (no spread)" if reads else "no reads recorded"
    return out, why


def _evidence_header(evidence: ReadEvidence, trust: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "noise_floor_nits": evidence.noise_floor_nits, "noise_floor_source": evidence.noise_floor_source,
        "reads_basis": ((evidence.reads_basis or "meter reads per signal (measure NDJSON)") if evidence.reads else
                        "scored rows per signal (no NDJSON read counts)"),
        "weak_definition": "single read OR measured Y below the noise floor OR flagged low-SNR",
        "n_signals": len(trust)}
    for k in _TRUST_FLAGS:
        out[f"n_{k}"] = sum(1 for t in trust if t[k])
    return out


def _patch_weight_block(groups: Sequence[tuple[PatchMetric, int]], trust: Sequence[Mapping[str, Any]],
                        cw: ContentWeights, *, reach: float,
                        e_corr: Optional[Sequence[float]] = None,
                        e: Optional[Sequence[float]] = None) -> dict[str, Any]:
    """Σ w·E / Σ w over the measured unique signals that carry a content weight (E = the per-signal mean).

    * ``e`` replaces the per-signal E (default: the raw ``de2000``). The black-aware basis passes its own E,
      and its own ``trust`` rows (the noise-limited flag on that E), so every share describes that score.
    * ``e_corr`` adds the labelled noise bias-corrected variant Σ w·E_corr / Σ w.
    * ``floor_limited`` (the black-aware class) gets its own weight and score share when the trust rows carry it."""
    total_w = float(sum(max(0.0, float(v)) for v in cw.weights.values()))
    flags = (("out_of_gamut",) + _TRUST_FLAGS + _NOISE_FLAGS
             + tuple(k for k in _BLACK_FLAGS if any(k in t for t in trust)))
    num_c = 0.0
    w_share = dict.fromkeys(flags, 0.0)
    s_share = dict.fromkeys(flags, 0.0)
    num = den = 0.0
    n = 0
    seen: set[tuple] = set()
    for i, ((m, _n), t) in enumerate(zip(groups, trust)):
        key = signal_key(m.rgb)
        seen.add(key)
        w = max(0.0, float(cw.weights.get(key, 0.0)))
        if w <= 0:
            continue
        ei = float(e[i]) if e is not None else m.de2000
        n += 1
        num += w * ei
        den += w
        if e_corr is not None:
            num_c += w * e_corr[i]
        on = {"out_of_gamut": bool(m.gamut_clamped), **{k: bool(t.get(k)) for k in _BLACK_FLAGS},
              **{k: bool(t[k]) for k in _TRUST_FLAGS + _NOISE_FLAGS}}
        for k in flags:
            if on[k]:
                w_share[k] += w
                s_share[k] += w * ei
    unmeasured = sum(max(0.0, float(v)) for k, v in cw.weights.items() if k not in seen)
    return {
        "label": cw.label, "source": cw.source, "score": round(num / den, 3) if den > 0 else None, "n": n,
        "score_bias_corrected": (round(num_c / den, 3) if (den > 0 and e_corr is not None) else None),
        "weight_measured": round(den, 6), "weight_total": round(total_w, 6),
        "weight_unmeasured_share": round(unmeasured / total_w, 4) if total_w > 0 else None,
        "coverage_gap_pct_as_drawn": (cw.coverage_gap_pct or {}).get(f"reach_{reach:g}"),
        "weight_share": {k: (round(v / den, 4) if den > 0 else None) for k, v in w_share.items()},
        "score_share": {k: (round(v / num, 4) if num > 1e-9 * max(den, 1e-300) else None)
                        for k, v in s_share.items()}}


_BIAS_LABEL = ("noise bias-corrected (quadrature subtraction of each signal's read-noise SE, floored at 0) "
               "— a VARIANT beside the raw score, not the score")
_TOE_POINT_LABEL = ("literal BT.2390 toe point: floor-limited signals scored against the BT.2390 EETF black-lift "
                    "target itself (others raw); a VARIANT beside the black-aware score, not the score")
_BT2390_BAND_LABEL = ("superseded BT.2390-toe band: floor-limited signals scored against the luminance band "
                      "[PQ target, BT.2390 toe target] at the target chromaticity (others raw); a VARIANT, not the "
                      "score")
_BLACK_GATE_NOTE = ("evidence only. No gate reads it, and the practical zones (core / limits / clamped) and the verify "
                    "gate keep their basis: a floor-limited core patch stays in core")
_BLACK_BASIS_NOTE = ("the weak-evidence share, the noise bias-corrected variant and the noise-limited share beside a "
                     "black-aware score are computed on the black-aware basis (its per-signal E)")
# Continuity evidence: the largest per-signal |E_black_aware - E_raw| above these PQ target luminances (nit).
_CONTINUITY_LEVELS = (1.0, 10.0)


def _black_aware_signals(patch_metrics: Sequence[PatchMetric], groups: Sequence[tuple[PatchMetric, int]],
                         floor: Any, *, is_hdr: bool, white_xy: Optional[tuple[float, float]],
                         evidence: Optional[ReadEvidence] = None,
                         ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """The ``black_aware`` block of the content-weighted summary (:mod:`dlc.black_aware`) and, when the
    pedestal applies, the per-signal lists in :func:`group_per_signal` order: ``e_black`` (the score), ``e_toe``
    / ``e_band`` (the BT.2390 variants, ``None`` without a target peak), ``limited`` (the descriptive
    floor-limited class) and ``below_meter`` (PQ target Y AND measured Y below the meter floor: ``evidence``'s
    ``noise_floor_nits``, the read evidence's own value). Every E is a per-signal MEAN over the signal's reads,
    like the raw E.

    ``(None, None)`` without a floor. When the floor is unavailable, the floor is 0 or the verify is SDR, the
    block says why and the per-signal lists are ``None``."""
    if floor is None:
        return None, None
    import numpy as np                                   # lazy: the spine imports this module dependency-free

    from . import black_aware as ba

    head: dict[str, Any] = {**floor.as_dict(), "model": ba.MODEL_TEXT, "gate": _BLACK_GATE_NOTE}
    if not is_hdr:
        return ({**head, "available": False, "applied": False,
                 "reason": "SDR verify: SDR scores CIEDE2000 against a zero-black power law relative to the measured "
                           "white; the black-aware pedestal is an HDR (absolute-luminance) construct"}, None)
    if not floor.available:
        return {**head, "available": False, "applied": False, "reason": floor.source}, None
    if float(floor.nits) <= 0.0:
        return ({**head, "available": True, "applied": False,
                 "reason": "the floor is 0 nit, so the pedestal is empty: black-aware = raw (the headline stays raw)"},
                None)
    per = ba.black_aware_patch_scores(patch_metrics, floor, white_xy=white_xy)
    has_var = per["e_toe_point"] is not None
    by_key: dict[tuple, list[int]] = {}
    for i, m in enumerate(patch_metrics):
        by_key.setdefault(signal_key(m.rgb), []).append(i)

    def mean(arr: Any, idx: list[int]) -> float:
        return float(np.mean(arr[idx]))

    ev = evidence or ReadEvidence()
    meter = float(ev.noise_floor_nits)
    e_black: list[float] = []
    e_toe: list[float] = []
    e_band: list[float] = []
    limited: list[bool] = []
    below_meter: list[bool] = []
    to_black: list[bool] = []
    rows: list[dict[str, Any]] = []
    changes: list[tuple[float, float, bool, dict[str, Any]]] = []
    for m, n in groups:
        idx = by_key[signal_key(m.rgb)]
        eb = mean(per["e_black_aware"], idx)
        fl = bool(per["floor_limited"][idx[0]])          # depends on the signal's target only
        ty = float(per["target_y"][idx[0]])
        my = mean(per["measured_y"], idx)
        bm = ty < meter and my < meter
        vb = bool(np.any(per["scored_vs_black"][idx]))
        e_black.append(eb)
        limited.append(fl)
        below_meter.append(bm)
        to_black.append(vb)
        if has_var:
            e_toe.append(mean(per["e_toe_point"], idx))
            e_band.append(mean(per["e_bt2390_band"], idx))
        row = {"rgb": [round(float(c), 4) for c in m.rgb], "rows": n, "target_Y": round(ty, 6),
               "measured_Y": round(my, 6),
               "pedestal_used_nits": round(mean(per["pedestal_y"], idx), 6),
               "black_reachable": bool(per["black_reachable"][idx[0]]), "scored_vs_black": vb,
               "below_meter_floor": bm,
               "E_raw": round(m.de2000, 4), "E_black_aware": round(eb, 4)}
        if has_var:
            row.update({"bt2390_Y": round(float(per["toe_y"][idx[0]]), 6), "E_vs_bt2390_band": round(e_band[-1], 4),
                        "E_vs_toe_point": round(e_toe[-1], 4)})
        if fl or bm or vb:
            rows.append(row)
        changes.append((ty, abs(m.de2000 - eb), bool(m.grayscale), row))
    f = float(floor.nits)
    thr = ba.FLOOR_LIMITED_FACTOR * f

    def largest(level: float, *, greys: bool = False) -> dict[str, Any]:
        sel = [c for c in changes if c[0] >= level and (c[2] or not greys)]
        if not sel:
            return {"n": 0, "max_abs_change_dEITP": None}
        top = max(sel, key=lambda c: c[1])
        return {"n": len(sel), "max_abs_change_dEITP": round(top[1], 4),
                "at": {k: top[3][k] for k in ("rgb", "target_Y", "E_raw", "E_black_aware")}}

    raw_all = [m.de2000 for m, _n in groups]
    fit_meter = (floor.fit or {}).get("meter_floor_nits") if getattr(floor, "fit", None) else None
    block = {**head, "available": True, "applied": True,
             "scoring_rule": ba.SCORING_TEXT, "basis_note": _BLACK_BASIS_NOTE,
             "floor_limited_rule": (f"descriptive class: PQ target Y < {ba.FLOOR_LIMITED_FACTOR:g} x the floor "
                                    f"(< {thr:g} nit; the pedestal >= 10 % of the target). The black-aware score "
                                    f"applies to EVERY signal; the BT.2390 variants only to this class"),
             "n_signals": len(groups),
             "n_floor_limited_signals": sum(limited),
             "n_floor_limited_reads": int(np.sum(per["floor_limited"])),
             # True black is reachable below the floor: the signals it scored (strictly nearer than the segment).
             "black_reachable_rule": ba.BLACK_REACHABLE_RULE,
             "n_black_reachable_signals": sum(1 for (m, _n) in groups
                                              if bool(per["black_reachable"][by_key[signal_key(m.rgb)][0]])),
             "n_signals_scored_vs_black": sum(to_black),
             # Evidence, never dropped: still scored above. The content-weight shares are filled per class / for
             # the patch weights by content_weighted_summary.
             "below_meter_floor": {"rule": ba.BELOW_METER_FLOOR_RULE, "meter_floor_nits": meter,
                                   "meter_floor_source": ev.noise_floor_source, "n_signals": sum(below_meter),
                                   "n_reads": int(sum(n for (_m, n), bm in zip(groups, below_meter) if bm)),
                                   "raw": _bucket_stats([e for e, bm in zip(raw_all, below_meter) if bm]),
                                   "black_aware": _bucket_stats([e for e, bm in zip(e_black, below_meter) if bm])},
             "all_signals": {"raw": _bucket_stats(raw_all), "black_aware": _bucket_stats(e_black)},
             # The floor-limited class next to core / limits / clamped: the RAW number stays visible beside the
             # black-aware one (per unique signal).
             "floor_limited": {"raw": _bucket_stats([e for e, fl in zip(raw_all, limited) if fl]),
                               "black_aware": _bucket_stats([e for e, fl in zip(e_black, limited) if fl]),
                               **({"vs_bt2390_band": _bucket_stats([e for e, fl in zip(e_band, limited) if fl]),
                                   "vs_toe_point": _bucket_stats([e for e, fl in zip(e_toe, limited) if fl])}
                                  if has_var else {})},
             # No cutoff, so the score is continuous: the largest per-signal change above 10 F / 1 nit / 10 nit.
             # A saturated colour keeps a near-black channel well above 1 nit, so the white pedestal still moves
             # its chroma there (e.g. a 1.7-nit Rec.2020 green); greys converge first.
             "continuity": {"above_floor_limited": largest(thr),
                            **{f"target_ge_{lv:g}_nit": largest(lv) for lv in _CONTINUITY_LEVELS},
                            **{f"greys_target_ge_{lv:g}_nit": largest(lv, greys=True) for lv in _CONTINUITY_LEVELS}},
             "raw_recompute_max_abs_dEITP": round(per["d0_vs_raw_max"], 6),
             "per_signal": sorted(rows, key=lambda r: r["target_Y"])}
    if fit_meter is not None:
        # one meter floor everywhere: the raw floor fit flags its greys against the read evidence's value
        block["below_meter_floor"]["same_as_floor_fit"] = float(fit_meter) == meter
    if not has_var:
        block["variants_unavailable"] = "no target peak: the BT.2390 variants need the source white"
    return block, {"e_black": e_black, "e_toe": e_toe if has_var else None, "e_band": e_band if has_var else None,
                   "limited": limited, "below_meter": below_meter}


def _below_meter_shares(black_block: dict[str, Any], block: Mapping[str, Any]) -> None:
    """Fill the black-aware block's ``below_meter_floor`` content-weight shares (in place): per content class
    (the black-aware kernel's evidence share: content share + its share of the black-aware score) and for the
    patch weights (weight share + black-aware score share)."""
    bm = black_block.get("below_meter_floor")
    if not isinstance(bm, dict):
        return
    by_class: dict[str, Any] = {}
    for name, res in (block.get("classes") or {}).items():
        ev = (((res or {}).get("black_aware") or {}).get("evidence") or {}).get("below_meter_floor")
        if ev is not None:
            by_class[name] = {"content_share_pct": ev.get("content_share_pct"),
                              "black_aware_score_share_pct": ev.get("score_share_pct")}
    if by_class:
        bm["content_share_by_class"] = by_class
    pb = (block.get("patch_weights") or {}).get("black_aware") or {}
    if pb.get("weight_share") is not None and "below_meter_floor" in pb["weight_share"]:
        ws, ss = pb["weight_share"]["below_meter_floor"], (pb.get("score_share") or {}).get("below_meter_floor")
        bm["patch_weights"] = {"weight_share_pct": round(100.0 * ws, 2) if ws is not None else None,
                               "black_aware_score_share_pct": round(100.0 * ss, 2) if ss is not None else None}


def _black_aware_class(cs: Any, dist: Any, loc: Any, info: Sequence[Mapping[str, Any]],
                       ses: Sequence[Optional[float]], per: Mapping[str, Any],
                       e_corr_b: Sequence[float], *, err: Sequence[float], reach: float) -> dict[str, Any]:
    """One class's BLACK-AWARE kernel result: the same kernel over the black-aware per-signal E. Its evidence
    shares (noise-limited recomputed on that E), breakdowns and top contributors describe that score. The noise
    bias-corrected variant (on the same basis) and the BT.2390 variants ride as labelled variants. Signals are
    tagged ``zone: floor_limited`` when floor-limited and keep their raw ``dE_raw``. A failure is recorded here
    and never touches the raw class result."""
    try:
        e_black = per["e_black"]
        rows = [{**row, "zone": "floor_limited" if fl else row.get("zone"), "dE_raw": round(float(e), 2),
                 "noise_limited": se is not None and eb <= NOISE_LIMITED_SE * se}
                for row, fl, e, eb, se in zip(info, per["limited"], err, e_black, ses)]
        alt = {"bias_corrected": e_corr_b}
        if per.get("e_band") is not None:
            alt.update({"vs_bt2390_band": per["e_band"], "vs_toe_point": per["e_toe"]})
        res = cs.kernel_score(dist, loc, e_black, reach=reach, sig_info=rows, alt_err=alt)
    except Exception as exc:  # noqa: BLE001 - evidence only
        return {"score": None, "error": f"{type(exc).__name__}: {exc}"}
    var = res.pop("variants", None) or {}
    for k in ("class", "reach_dEITP", "sigma_dEITP", "n_signals", "content_bins"):
        res.pop(k, None)                                   # already on the raw class result
    out = {"label": "black-aware: every signal scored against the nearest point of [target, target + floor "
                    "x pedestal colour], plus true black below the floor (an additive raised black as a panel "
                    "limit)", **res}
    for key, label in (("bias_corrected", f"{_BIAS_LABEL} (black-aware basis)"),
                       ("vs_bt2390_band", _BT2390_BAND_LABEL), ("vs_toe_point", _TOE_POINT_LABEL)):
        if var.get(key) is not None:
            out[key] = {**var[key], "label": label}
    return out


def _headline_black_aware(head: dict[str, Any], black_block: Optional[Mapping[str, Any]], *,
                          raw_score: Any, applied: bool, metric: str, what: str) -> None:
    """Mark the headline black-aware (in place) when the pedestal applied: the label names the floor and its
    source, ``score_raw`` keeps the raw number, and the basis says the variants beside it share the black-aware
    basis. Otherwise, when a floor was considered, it records why the headline stayed raw."""
    if black_block is None:
        return
    if applied and black_block.get("applied"):
        floor = black_block.get("floor_nits")
        head.update({
            "label": (f"content-weighted {metric}, BLACK-AWARE ({what}; display floor {floor:g} nit from "
                      f"{black_block.get('floor_source')})"),
            "basis": (f"{head.get('basis')}; every signal scored against the nearest point of [target, target + "
                      f"floor x pedestal colour], plus true black for a target below the floor (additive raised "
                      f"black as a panel limit). {_BLACK_BASIS_NOTE}"),
            "black_aware": True, "score_raw": raw_score, "black_floor_nits": floor,
            "black_floor_source": black_block.get("floor_source"),
            "black_pedestal_xy": black_block.get("pedestal_xy"),
            "black_pedestal_source": black_block.get("pedestal_source"),
            "n_floor_limited_signals": black_block.get("n_floor_limited_signals")})
    else:
        head.update({"black_aware": False,
                     "black_aware_reason": black_block.get("reason") or "the black-aware score is unavailable"})


def content_weighted_summary(patch_metrics: list[PatchMetric], *, is_hdr: bool,
                             content_weights: Optional[ContentWeights] = None,
                             read_evidence: Optional[ReadEvidence] = None,
                             content: Any = None, content_reach: float = DEFAULT_CONTENT_REACH,
                             white_xy: Optional[tuple[float, float]] = None,
                             black_floor: Any = None) -> Optional[dict[str, Any]]:
    """The ``content_weighted`` block of :func:`practical_summary` — ``None`` without weights or a
    content distribution. EVIDENCE ONLY: no gate reads it (the verify gate keeps scoring the practical
    core / tube / white); it LEADS the practical block so the number a human / the LLM reads first is
    the one content sees.

    * ``patch_weights`` — the verify set's own per-signal ``content_weight`` (``--verify-patches-file``):
      Σ w·E / Σ w over measured signals, with ``n``, the out-of-gamut weight share and the weight / score
      share resting on weak evidence (single read, below the meter floor, low-SNR).
    * ``classes`` — per content class (:class:`dlc.content_score.ContentDistribution`): the kernel score,
      ``coverage_gap_pct``, the nearest-signal fallback, zone / band breakdowns, the evidence shares and
      the top contributors (:func:`dlc.content_score.kernel_score`).
    * ``headline`` — the first class's kernel score (else the patch-weight score) + its gap, labelled.
    * ``black_aware`` — with ``black_floor`` (:class:`dlc.black_aware.BlackFloor`, HDR): every signal is
      also scored against the display's raised black as a panel limit (the nearest point of [target, target +
      floor x pedestal colour], plus true black for a target below the floor). Every class and the patch
      weights then carry the black-aware score beside the raw one, which stays recorded; the variants and
      evidence shares beside a black-aware score are on the black-aware basis. Signals whose target AND read
      are below the meter floor (the read evidence's) are flagged ``below_meter_floor`` (count + content-weight
      share in the block), still scored. The HEADLINE becomes the black-aware score, labelled with the floor and
      its source, when the pedestal applies (a positive floor). When the floor is unavailable or 0, the block
      says why and the headline stays raw.

    Heavy imports (numpy / scipy / the engine) happen only with ``content`` / ``black_floor``."""
    contents = [] if content is None else (list(content) if isinstance(content, (list, tuple)) else [content])
    if content_weights is None and not contents:
        return None
    evidence = read_evidence or ReadEvidence()
    groups = group_per_signal(patch_metrics)
    trust = [_signal_trust(m, n, evidence) for m, n in groups]
    metric = "dE_ITP" if is_hdr else "CIEDE2000"
    # NOISE-AWARE (2026-10-09): dE is a magnitude, so read noise biases it UP near the meter floor
    # (E[dE] ~ sqrt(true^2 + noise^2)). Per signal: the noise SE where DLC has it, a noise-limited flag
    # (E within NOISE_LIMITED_SE of it) and the quadrature bias-corrected E (floored at 0) — a LABELLED
    # variant reported beside the raw score, never instead of it.
    errors: list[dict[str, str]] = []

    def failed(part: str, exc: BaseException) -> None:   # evidence must never fail the verify: record it
        errors.append({"part": part, "error": f"{type(exc).__name__}: {exc}"})

    try:
        noise, noise_why = _signal_noise(groups, evidence, is_hdr=is_hdr, white_xy=white_xy)
    except Exception as exc:  # noqa: BLE001 - evidence only
        failed("noise", exc)
        noise, noise_why = [None] * len(groups), ["noise evidence failed"] * len(groups)
    e_corr: list[float] = []
    ses: list[Optional[float]] = []
    for (m, _n), t, nz in zip(groups, trust, noise):
        se = nz["se"] if nz else None
        ses.append(se)
        t["noise_se"] = round(se, 4) if se is not None else None
        t["noise_limited"] = se is not None and m.de2000 <= NOISE_LIMITED_SE * se
        t["noise_unknown"] = se is None
        e_corr.append(math.sqrt(max(m.de2000 ** 2 - se ** 2, 0.0)) if se is not None else m.de2000)
    # BLACK-AWARE (owner 2026-10-09, dlc.black_aware): the display's raised black is a PANEL LIMIT, like an
    # out-of-gamut colour. Every signal is also scored against the nearest point of [target, target + pedestal]
    # (plus true black for a target below the floor).
    # The raw score stays recorded beside it, and no gate reads either.
    black_block = per_black = None
    try:
        black_block, per_black = _black_aware_signals(patch_metrics, groups, black_floor,
                                                      is_hdr=is_hdr, white_xy=white_xy, evidence=evidence)
    except Exception as exc:  # noqa: BLE001 - evidence only
        failed("black_aware", exc)
        black_block = {**(black_floor.as_dict() if hasattr(black_floor, "as_dict") else {}), "available": False,
                       "applied": False, "reason": f"black-aware scoring failed ({type(exc).__name__}: {exc})"}
    e_black: Optional[list[float]] = per_black["e_black"] if per_black is not None else None
    e_corr_b: Optional[list[float]] = None
    trust_b: Optional[list[dict[str, Any]]] = None
    if per_black is not None:
        for t, fl, bm in zip(trust, per_black["limited"], per_black["below_meter"]):
            t["floor_limited"] = fl
            t["below_meter_floor"] = bm
        e_corr_b = [math.sqrt(max(eb ** 2 - se ** 2, 0.0)) if se is not None else eb for eb, se in zip(e_black, ses)]
        # the black-aware basis' own trust rows: noise-limited judged on the black-aware E
        trust_b = [{**t, "noise_limited": se is not None and eb <= NOISE_LIMITED_SE * se}
                   for t, eb, se in zip(trust, e_black, ses)]
    block: dict[str, Any] = {"headline": None, "metric": metric,
                             "evidence_only": "no gate reads this block — the LLM judges it at the verify seam"}
    if contents:
        from . import content_score as cs

        reps = [m for m, _ in groups]
        try:
            nominal = cs.nominal_signal_xyz([m.rgb for m in reps], is_hdr=is_hdr, white_xy=white_xy,
                                            target_xyz=[m.target_xyz for m in reps])
            loc = cs.xyz_to_itp(nominal)
        except Exception as exc:  # noqa: BLE001 - evidence only
            failed("nominal_location", exc)
            nominal = loc = None
        err = [m.de2000 for m in reps]
        classes: dict[str, Any] = {}
        for dist in (contents if loc is not None else ()):
            try:
                info = [{"rgb": [round(float(c), 4) for c in m.rgb], "nominal_Y": round(float(nominal[i][1]), 4),
                         "zone": practical_zone(m, is_hdr=is_hdr), **t} for i, (m, t) in enumerate(zip(reps, trust))]
                res = cs.kernel_score(dist, loc, err, reach=content_reach, sig_info=info,
                                      alt_err={"bias_corrected": e_corr})
                bc = (res.pop("variants", None) or {}).get("bias_corrected")
                if bc is not None:
                    res["bias_corrected"] = {**bc, "label": _BIAS_LABEL}
                res["provenance"] = dist.provenance()
                if per_black is not None:
                    res["black_aware"] = _black_aware_class(cs, dist, loc, info, ses, per_black, e_corr_b,
                                                            err=err, reach=content_reach)
            except Exception as exc:  # noqa: BLE001 - evidence must never break the verify
                res = {"class": getattr(dist, "label", "?"), "score": None,
                       "error": f"{type(exc).__name__}: {exc}"}
            classes[str(res.get("class"))] = res
        block["classes"] = classes
        first = next(iter(classes.values()), None)
        if first is not None and first.get("score") is not None:
            fb = first.get("black_aware") or {}
            src = fb if fb.get("score") is not None else first
            block["headline"] = {
                "label": f"content-weighted {metric} ({first['class']}, R {content_reach:g} dE_ITP)",
                "basis": "content kernel (study §5.1)", "class": first["class"], "reach_dEITP": content_reach,
                "score": src["score"], "coverage_gap_pct": first.get("coverage_gap_pct"),
                "score_with_nearest_fallback": src.get("score_with_nearest_fallback"),
                "weak_evidence_score_share_pct": ((src.get("evidence") or {}).get("weak") or {}).get(
                    "score_share_pct"),
                "score_bias_corrected": (src.get("bias_corrected") or {}).get("score"),
                "noise_limited_content_share_pct": ((src.get("evidence") or {}).get("noise_limited") or {}).get(
                    "content_share_pct")}
            _headline_black_aware(block["headline"], black_block, raw_score=first["score"], applied=src is fb,
                                  metric=metric, what=f"{first['class']}, R {content_reach:g} dE_ITP")
    if content_weights is not None:
        try:
            pw = _patch_weight_block(groups, trust, content_weights, reach=content_reach, e_corr=e_corr)
            if per_black is not None:
                # the black-aware score on ITS OWN basis: E, the bias-corrected E and the trust rows all black-aware
                pwb = _patch_weight_block(groups, trust_b, content_weights, reach=content_reach, e_corr=e_corr_b,
                                          e=e_black)
                pw["black_aware"] = {
                    "label": ("black-aware: Σ w·E_black / Σ w; its bias-corrected variant and its weight / score "
                              "shares are on the black-aware basis"),
                    **{k: pwb[k] for k in ("score", "score_bias_corrected", "weight_share", "score_share")}}
        except Exception as exc:  # noqa: BLE001 - evidence only
            failed("patch_weights", exc)
            pw = None
        if pw is not None:
            block["patch_weights"] = pw
            if block["headline"] is None and pw["score"] is not None:
                pb = pw.get("black_aware") or {}
                src = pb if pb.get("score") is not None else pw     # every number below on ONE basis
                weak = src["score_share"].get("weak")
                block["headline"] = {
                    "label": f"content-weighted {metric} (patch weights: {pw['label']})",
                    "basis": "per-patch content weights (Σ w·E / Σ w)", "class": pw["label"],
                    "reach_dEITP": content_reach, "score": src["score"],
                    "coverage_gap_pct": pw.get("coverage_gap_pct_as_drawn"),
                    "weak_evidence_score_share_pct": round(100.0 * weak, 1) if weak is not None else None,
                    "score_bias_corrected": src.get("score_bias_corrected"),
                    "noise_limited_weight_share_pct": (round(100.0 * src["weight_share"]["noise_limited"], 1)
                                                       if src["weight_share"].get("noise_limited") is not None
                                                       else None)}
                _headline_black_aware(block["headline"], black_block, raw_score=pw["score"],
                                      applied=src is pb, metric=metric,
                                      what=f"patch weights: {pw['label']}")
    block["evidence"] = _evidence_header(evidence, trust)
    floor_txt = f"at/below the meter floor ({float(evidence.noise_floor_nits):g} nit)"
    block["noise"] = {
        "label": _BIAS_LABEL,
        "noise_limited_rule": f"E <= {NOISE_LIMITED_SE:g}x its own read-noise SE",
        "correction": "E_corr = sqrt(max(E^2 - SE^2, 0)) per signal; signals without a noise estimate keep E",
        "unknown_rule": (f"no estimate (noise_unknown, E kept) for a single read and for a signal measured "
                         f"{floor_txt}: a read spread there is not a noise estimate"),
        "reads_basis": evidence.reads_basis,
        "n_with_estimate": sum(1 for nz in noise if nz),
        "n_noise_limited": sum(1 for t in trust if t["noise_limited"]),
        "n_unknown_at_floor": sum(1 for w in noise_why if w.startswith("at/below")),
        "per_signal": [{"rgb": [round(float(c), 4) for c in m.rgb], "E": round(m.de2000, 4),
                        "noise_se": round(nz["se"], 4), "E_bias_corrected": round(ec, 4),
                        "noise_limited": t["noise_limited"], "reads": t["reads"], "basis": nz["basis"]}
                       for (m, _n), t, nz, ec in zip(groups, trust, noise, e_corr) if nz]}
    if black_block is not None:
        _below_meter_shares(black_block, block)
        block["black_aware"] = black_block
    if errors:
        block["errors"] = errors
    return block


def _content_weighted_or_error(patch_metrics: list[PatchMetric], **kw: Any) -> Optional[dict[str, Any]]:
    """:func:`content_weighted_summary`, which can never fail the summary it rides in: an exception
    becomes the block's recorded ``error`` (evidence for the LLM) and the practical summary goes on."""
    try:
        return content_weighted_summary(patch_metrics, **kw)
    except Exception as exc:  # noqa: BLE001 - evidence must never break the verify gate
        return {"headline": None, "evidence_only": "no gate reads this block — the LLM judges it at the verify seam",
                "error": f"content-weighted evidence failed ({type(exc).__name__}: {exc}); the verify is unaffected"}


def practical_summary(patch_metrics: list[PatchMetric], *, is_hdr: bool,
                      gamut_aware: bool = False,
                      content_weights: Optional[ContentWeights] = None,
                      read_evidence: Optional[ReadEvidence] = None,
                      content: Any = None, content_reach: float = DEFAULT_CONTENT_REACH,
                      white_xy: Optional[tuple[float, float]] = None,
                      black_floor: Any = None) -> dict[str, Any]:
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
    in ``<1``/``1-10`` no matter how good the overall average looks).

    The buckets above are READ-weighted (every read counts) and stay exactly that — the
    verify-only deltas and history compare recorded numbers. ``per_signal`` (V2, 2026-10-02)
    is the same split over UNIQUE signals (:func:`per_signal_summary`): a signal read 7× (the
    saturation-sweep bookends) counts once, so the repeated, in-training sweep cannot carry
    the average (PA32UCXR 2026-10-02: 28 of 141 signals held 63 % of the read weight).

    CONTENT-WEIGHTED LEAD (2026-10-09, :func:`content_weighted_summary`). The "patch geography already
    spends its budget where content lives" premise above did not survive the owner's library survey
    (``results/practical_score_2026-10-09``): the < 1 nit core is under-sampled ~8x and the low-chroma
    shell ~13x, limits over-sampled ~7x, clamped ~50x. So the zones stay a BREAKDOWN, and when the
    verify set carries per-patch ``content_weights`` or a content distribution is given (``content``),
    the block opens with ``content_weighted`` (score + ``coverage_gap_pct``, labelled with the class
    and reach R) -- evidence only: no gate reads it, the LLM does.

    BLACK-AWARE (2026-10-09, ``black_floor``, HDR; :mod:`dlc.black_aware`). With a display floor, the
    content-weighted block also scores every signal against the display's raised black (an additive pedestal)
    as a panel limit. Its headline becomes that score (the raw one stays recorded), and its ``black_aware``
    block carries the descriptive ``floor_limited`` class with raw, black-aware and variant stats. That class
    lives ONLY in the content-weighted evidence. The zones below (and so :func:`practical_gate_view` / the verify gate) keep their basis: a
    floor-limited core patch stays in ``core``, scored raw."""
    lead = _content_weighted_or_error(patch_metrics, is_hdr=is_hdr, content_weights=content_weights,
                                      read_evidence=read_evidence, content=content,
                                      content_reach=content_reach, white_xy=white_xy, black_floor=black_floor)
    return {
        **({"content_weighted": lead} if lead is not None else {}),
        "gamut_aware": bool(gamut_aware),
        **_practical_buckets(patch_metrics, is_hdr=is_hdr),
        "per_signal": per_signal_summary(patch_metrics, is_hdr=is_hdr),
    }


def practical_zone(m: PatchMetric, *, is_hdr: bool) -> str:
    """The §0 zone of one scored patch — ``core`` / ``limits`` / ``clamped`` (see
    :func:`practical_summary`). The ONE classifier the read-weighted and per-signal views share.

    Deliberately NO ``floor_limited`` here. The black-aware class (:mod:`dlc.black_aware`) is
    content-weighted evidence only. Adding it here would move near-black core patches out of the core and
    change the verify gate's basis (:func:`practical_gate_view`), which stays as it was."""
    x, y, z = m.target_xyz
    total = x + y + z
    target_xy = (x / total, y / total) if total > 1e-9 else None
    if m.gamut_clamped:
        return "clamped"
    if not is_hdr or is_core_target(target_xy, y):
        return "core"
    return "limits"


def _practical_buckets(patch_metrics: list[PatchMetric], *, is_hdr: bool) -> dict[str, Any]:
    zones: dict[str, list[float]] = {"core": [], "limits": [], "clamped": []}
    tube: list[float] = []
    bands: dict[str, list[float]] = {label: [] for label in PRACTICAL_BAND_LABELS}
    for m in patch_metrics:
        zones[practical_zone(m, is_hdr=is_hdr)].append(m.de2000)
        if m.grayscale or _signal_saturation(m.rgb) <= TUBE_SATURATION_MAX:
            tube.append(m.de2000)
        band_idx = sum(1 for edge in PRACTICAL_BAND_EDGES_NITS if m.target_xyz[1] > edge)
        bands[PRACTICAL_BAND_LABELS[band_idx]].append(m.de2000)
    return {
        "core": _bucket_stats(zones["core"]),
        "limits": _bucket_stats(zones["limits"]),
        "clamped": _bucket_stats(zones["clamped"]),
        "tube": _bucket_stats(tube),
        "bands": {label: _bucket_stats(vals) for label, vals in bands.items()},
    }


# Signals are code values / max_cv: at <= 12 bits adjacent codes sit >= 2.4e-4 apart, so 4
# decimals separate every code while merging the float noise of one code's repeated reads.
SIGNAL_KEY_DECIMALS = 4


def signal_key(rgb: tuple[float, float, float] | list[float]) -> tuple[float, float, float]:
    """The grouping key of a signal (rounded to :data:`SIGNAL_KEY_DECIMALS`)."""
    return tuple(round(float(c), SIGNAL_KEY_DECIMALS) for c in rgb[:3])  # type: ignore[return-value]


def group_per_signal(patch_metrics: list[PatchMetric]) -> list[tuple[PatchMetric, int]]:
    """One representative :class:`PatchMetric` per UNIQUE signal, in first-read order, with its
    read count. The representative's ``de2000`` is the MEAN of that signal's reads' ΔE (and
    ``measured_xyz`` the mean read); target / grayscale / gamut_clamped come from the signal
    (identical across its reads — they depend on the signal only)."""
    groups: dict[tuple[float, float, float], list[PatchMetric]] = {}
    for m in patch_metrics:
        groups.setdefault(signal_key(m.rgb), []).append(m)
    out: list[tuple[PatchMetric, int]] = []
    for reads in groups.values():
        n = len(reads)
        first = reads[0]
        mean_xyz = tuple(sum(r.measured_xyz[i] for r in reads) / n for i in range(3))
        out.append((PatchMetric(first.rgb, mean_xyz, first.target_xyz,   # type: ignore[arg-type]
                                sum(r.de2000 for r in reads) / n, first.grayscale,
                                gamut_clamped=first.gamut_clamped), n))
    return out


# Below this many held-out signals the held-out bucket is reported, never gated (V1).
HELD_OUT_GATE_MIN_SIGNALS = 8


def practical_gate_view(practical: dict[str, Any] | None) -> dict[str, Any]:
    """The buckets the verify quality gate scores — ONE selection shared by the live gate
    (``Calibration._quality_gate``) and the stage-CLI advisory verdict
    (``stages._common.policy_advice``), so the two can never judge different numbers.

    * ``core`` / ``tube`` — PER UNIQUE SIGNAL when the practical split carries ``per_signal``
      (V2: a signal read 7× counts once), else the read-weighted buckets; ``basis`` names which.
    * ``held_out_gate`` — whether the held-out per-signal core avg (V1) is a gate input: only
      with >= :data:`HELD_OUT_GATE_MIN_SIGNALS` held-out signals; otherwise reported with the
      reason (unavailable classification, too few signals)."""
    practical = practical or {}
    read_core = practical.get("core") or {}
    per = practical.get("per_signal") or {}
    if (per.get("core") or {}).get("n"):
        core, tube, basis = per["core"], per.get("tube") or {}, "per_signal"
    else:
        core, tube, basis = read_core, practical.get("tube") or {}, "read_weighted"
    held = practical.get("held_out") or {}
    ho = held.get("held_out") or {}
    n_ho = int(ho.get("n") or 0)
    min_n = HELD_OUT_GATE_MIN_SIGNALS
    if held.get("available") and n_ho >= min_n and ho.get("avg") is not None:
        held_gate: dict[str, Any] = {"gated": True, "held_out_avg": ho["avg"], "held_out_n": n_ho,
                                     "min_n": min_n}
    else:
        reason = ((held.get("reason") or "no held-out classification") if not held.get("available")
                  else f"held-out n {n_ho} < {min_n}")
        held_gate = {"gated": False, "reason": reason, "held_out_avg": ho.get("avg"),
                     "held_out_n": n_ho, "min_n": min_n}
    return {"basis": basis, "core": core, "tube": tube, "read_core": read_core,
            "n_signals": per.get("n_signals"), "n_reads": per.get("n_reads"),
            "held_out_gate": held_gate}


def per_signal_summary(patch_metrics: list[PatchMetric], *, is_hdr: bool) -> dict[str, Any]:
    """The practical split over UNIQUE signals (V2): each signal's ΔE is the mean of its reads,
    then ``overall`` / ``core`` / ``limits`` / ``clamped`` / ``tube`` / ``bands`` are stats over
    signals — repeats count once. ``n_signals`` / ``n_reads`` say how much the two views differ."""
    reps = [m for m, _n in group_per_signal(patch_metrics)]
    return {"n_signals": len(reps), "n_reads": len(patch_metrics),
            "overall": _bucket_stats([m.de2000 for m in reps]),
            **_practical_buckets(reps, is_hdr=is_hdr)}


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

