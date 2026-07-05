"""Structural checks for generated 3D LUT cube files."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .events import EventWriter
from .mhc import resolve_run_path
from .runs import RunContext


@dataclass(frozen=True)
class CubeData:
    path: str
    title: str
    size: int
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    values: list[tuple[float, float, float]]
    parse_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LutIntegritySummary:
    phase: str
    iteration: int
    cube_path: str
    ok: bool
    size: int
    expected_entries: int
    actual_entries: int
    parse_error_count: int
    out_of_bounds_count: int
    monotonicity_violations: int
    max_neighbor_delta: float
    avg_neighbor_delta: float
    max_neighbor_delta_allowed: float
    monotonicity_violations_allowed: int
    integrity_path: str
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_cube(path: Path) -> CubeData:
    title = ""
    size = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    values: list[tuple[float, float, float]] = []
    errors: list[str] = []

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        keyword = parts[0].upper()
        try:
            if keyword == "TITLE":
                title = line.partition(" ")[2].strip().strip('"')
            elif keyword == "LUT_3D_SIZE":
                size = int(parts[1])
            elif keyword == "DOMAIN_MIN":
                domain_min = parse_triplet(parts[1:4])
            elif keyword == "DOMAIN_MAX":
                domain_max = parse_triplet(parts[1:4])
            elif keyword.startswith("LUT_"):
                continue
            else:
                values.append(parse_triplet(parts[0:3]))
        except (IndexError, ValueError) as exc:
            errors.append(f"line {line_number}: {exc}")

    if size <= 0:
        errors.append("missing or invalid LUT_3D_SIZE")
    return CubeData(
        path=str(path),
        title=title,
        size=size,
        domain_min=domain_min,
        domain_max=domain_max,
        values=values,
        parse_errors=errors,
    )


def parse_triplet(parts: list[str]) -> tuple[float, float, float]:
    if len(parts) < 3:
        raise ValueError("expected three numeric values")
    return (float(parts[0]), float(parts[1]), float(parts[2]))


# The largest per-node correction the optimizer may express, in signal units — the SDR
# 3D-LUT cap (`calibrate.SDR_CORRECTION_CAP`, 0.5; HDR caps tighter at 0.25). Used only to
# derive the structural neighbour-delta ceiling below; duplicating the constant would drift,
# so it is named for what it means here: the correction budget a legitimate cube can spend.
_MAX_LEGITIMATE_CORRECTION = 0.5


def default_neighbor_delta_allowed(size: int) -> float:
    """The grid-pitch-derived structural ceiling for a neighbour step (fable Phase 6, from
    the Phase 5 lead): the old fixed default of 1.0 admitted a FULL-RANGE jump between
    adjacent nodes — toothless, since an identity step on a 33-grid is ~0.031 and the
    optimizer's soft clamp bounds any legitimate correction to ±0.5 (SDR cap; HDR 0.25).
    A legitimate neighbour step is therefore at most the identity pitch plus the largest
    correction swing the caps allow; anything beyond that cannot come from a capped
    correction on top of identity — it is a tear (corrupt write, transposed axis, garbage
    parse). 2× pitch adds slack for domain-scaled cubes. ~0.56 at 33³, ~0.63 at 17³."""
    if size <= 1:
        return 1.0
    return min(1.0, 2.0 / (size - 1) + _MAX_LEGITIMATE_CORRECTION)


def summarize_lut_integrity(
    *,
    cube: CubeData,
    phase: str,
    iteration: int,
    integrity_path: Path,
    max_neighbor_delta_allowed: float | None = None,
    monotonicity_violations_allowed: int = 0,
    bounds_epsilon: float = 1e-6,
) -> LutIntegritySummary:
    if max_neighbor_delta_allowed is None:
        max_neighbor_delta_allowed = default_neighbor_delta_allowed(cube.size)
    expected_entries = cube.size**3 if cube.size > 0 else 0
    notes: list[str] = []
    if expected_entries != len(cube.values):
        notes.append(f"expected {expected_entries} entries but found {len(cube.values)}")

    out_of_bounds_count = sum(
        1
        for value in cube.values
        for channel in value
        if channel < -bounds_epsilon or channel > 1.0 + bounds_epsilon or not math.isfinite(channel)
    )
    if out_of_bounds_count:
        notes.append(f"{out_of_bounds_count} output channels are outside 0..1")

    monotonicity_violations, neighbor_deltas = cube_axis_checks(cube)
    if monotonicity_violations > monotonicity_violations_allowed:
        notes.append(f"{monotonicity_violations} monotonic axis violations exceed allowance {monotonicity_violations_allowed}")

    max_neighbor_delta = max(neighbor_deltas or [0.0])
    avg_neighbor_delta = (sum(neighbor_deltas) / len(neighbor_deltas)) if neighbor_deltas else 0.0
    if max_neighbor_delta > max_neighbor_delta_allowed:
        notes.append(f"max neighbor delta {max_neighbor_delta:.6f} exceeds allowance {max_neighbor_delta_allowed:.6f}")

    if cube.parse_errors:
        notes.extend(cube.parse_errors)

    ok = (
        not cube.parse_errors
        and expected_entries == len(cube.values)
        and out_of_bounds_count == 0
        and monotonicity_violations <= monotonicity_violations_allowed
        and max_neighbor_delta <= max_neighbor_delta_allowed
    )
    if ok:
        notes.append("LUT structure is within configured integrity thresholds")

    return LutIntegritySummary(
        phase=phase,
        iteration=iteration,
        cube_path=cube.path,
        ok=ok,
        size=cube.size,
        expected_entries=expected_entries,
        actual_entries=len(cube.values),
        parse_error_count=len(cube.parse_errors),
        out_of_bounds_count=out_of_bounds_count,
        monotonicity_violations=monotonicity_violations,
        max_neighbor_delta=max_neighbor_delta,
        avg_neighbor_delta=avg_neighbor_delta,
        max_neighbor_delta_allowed=max_neighbor_delta_allowed,
        monotonicity_violations_allowed=monotonicity_violations_allowed,
        integrity_path=str(integrity_path),
        notes=notes,
    )


def cube_axis_checks(cube: CubeData) -> tuple[int, list[float]]:
    size = cube.size
    if size <= 1 or len(cube.values) != size**3:
        return (0, [])

    def value_at(r: int, g: int, b: int) -> tuple[float, float, float]:
        # Standard .cube order is R-fastest (write_cube / DesktopLUT LoadLUT write
        # `for b: for g: for r:`), so the flat index is b*size² + g*size + r. Indexing
        # R-slowest here would check monotonicity on transposed axes (real violations missed).
        return cube.values[(b * size * size) + (g * size) + r]

    violations = 0
    neighbor_deltas: list[float] = []
    for r in range(size):
        for g in range(size):
            for b in range(size):
                current = value_at(r, g, b)
                if r + 1 < size:
                    nxt = value_at(r + 1, g, b)
                    neighbor_deltas.append(max(abs(nxt[i] - current[i]) for i in range(3)))
                    if nxt[0] + 1e-8 < current[0]:
                        violations += 1
                if g + 1 < size:
                    nxt = value_at(r, g + 1, b)
                    neighbor_deltas.append(max(abs(nxt[i] - current[i]) for i in range(3)))
                    if nxt[1] + 1e-8 < current[1]:
                        violations += 1
                if b + 1 < size:
                    nxt = value_at(r, g, b + 1)
                    neighbor_deltas.append(max(abs(nxt[i] - current[i]) for i in range(3)))
                    if nxt[2] + 1e-8 < current[2]:
                        violations += 1
    return violations, neighbor_deltas


def write_lut_integrity(
    *,
    ctx: RunContext,
    cube_path: Path,
    phase: str = "3dlut",
    iteration: int = 1,
    max_neighbor_delta_allowed: float | None = None,
    monotonicity_violations_allowed: int = 0,
) -> LutIntegritySummary:
    cube_path = resolve_run_path(ctx, cube_path)
    if not cube_path.exists():
        raise FileNotFoundError(f"cube not found: {cube_path}")
    output_dir = ctx.root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    integrity_path = output_dir / f"{phase}_iter{iteration:02d}_lut_integrity.json"
    cube = parse_cube(cube_path)
    summary = summarize_lut_integrity(
        cube=cube,
        phase=phase,
        iteration=iteration,
        integrity_path=integrity_path,
        max_neighbor_delta_allowed=max_neighbor_delta_allowed,
        monotonicity_violations_allowed=monotonicity_violations_allowed,
    )
    integrity_path.write_text(json.dumps(summary.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": f"{phase}_lut_integrity",
            "iteration": iteration,
            "status": "passed" if summary.ok else "failed",
            "integrity": str(integrity_path),
            "cube": str(cube_path),
        }
    )
    ctx.save()
    ctx.log(f"Checked {phase} LUT integrity iteration {iteration}: {'passed' if summary.ok else 'failed'}")
    EventWriter(ctx.events_path).write(
        "INFO" if summary.ok else "ERROR",
        f"{phase}_lut_integrity",
        "lut_integrity_checked",
        iteration=iteration,
        ok=summary.ok,
        cube=str(cube_path),
        integrity=str(integrity_path),
        monotonicity_violations=summary.monotonicity_violations,
        max_neighbor_delta=summary.max_neighbor_delta,
    )
    return summary

