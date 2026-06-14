"""MHC candidate generation and application."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import DesktopLutClient
from .events import EventWriter
from .runs import RunContext


D65_X = 0.3127
D65_Y = 0.3290
SRGB_PRIMARIES = {
    "rx": 0.64,
    "ry": 0.33,
    "gx": 0.30,
    "gy": 0.60,
    "bx": 0.15,
    "by": 0.06,
}


@dataclass(frozen=True)
class Ti3Sample:
    rgb: tuple[float, float, float]
    xyz: tuple[float, float, float]


@dataclass(frozen=True)
class ChannelModel:
    samples: list[tuple[float, float]]
    peak_xyz: tuple[float, float, float]


@dataclass(frozen=True)
class MhcCandidate:
    iteration: int
    mode: str
    source: str
    target_white: dict[str, float]
    target_gamma: float
    target_luminance: float
    measured_primaries: dict[str, float]
    cube_path: str
    summary_path: str
    candidate_path: str
    fallback: bool
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_ti3(path: Path) -> list[Ti3Sample]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    fields: list[str] = []
    data_rows: list[list[str]] = []
    in_format = False
    in_data = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "BEGIN_DATA_FORMAT":
            in_format = True
            continue
        if line == "END_DATA_FORMAT":
            in_format = False
            continue
        if line == "BEGIN_DATA":
            in_data = True
            continue
        if line == "END_DATA":
            in_data = False
            continue
        if in_format:
            fields.extend(line.split())
        elif in_data:
            data_rows.append(line.split())

    index = {name.upper(): i for i, name in enumerate(fields)}
    required = ["RGB_R", "RGB_G", "RGB_B", "XYZ_X", "XYZ_Y", "XYZ_Z"]
    if not all(name in index for name in required):
        raise ValueError(f"missing required TI3 fields in {path}: {required}")

    samples: list[Ti3Sample] = []
    for row in data_rows:
        try:
            rgb = tuple(_normalize_rgb(float(row[index[name]])) for name in ["RGB_R", "RGB_G", "RGB_B"])
            xyz = tuple(max(0.0, float(row[index[name]])) for name in ["XYZ_X", "XYZ_Y", "XYZ_Z"])
        except (IndexError, ValueError):
            continue
        samples.append(Ti3Sample(rgb=rgb, xyz=xyz))  # type: ignore[arg-type]
    return samples


def _normalize_rgb(value: float) -> float:
    return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))


def xy_from_xyz(xyz: tuple[float, float, float]) -> tuple[float, float]:
    total = sum(xyz)
    if total <= 0:
        return 0.0, 0.0
    return xyz[0] / total, xyz[1] / total


def white_xyz(y_value: float, x: float = D65_X, y: float = D65_Y) -> tuple[float, float, float]:
    return (x / y * y_value, y_value, (1.0 - x - y) / y * y_value)


def classify_samples(samples: list[Ti3Sample]) -> dict[str, list[Ti3Sample]]:
    groups = {"grey": [], "red": [], "green": [], "blue": []}
    for sample in samples:
        r, g, b = sample.rgb
        if abs(r - g) < 1e-6 and abs(g - b) < 1e-6:
            groups["grey"].append(sample)
        elif r > 0 and g < 1e-6 and b < 1e-6:
            groups["red"].append(sample)
        elif g > 0 and r < 1e-6 and b < 1e-6:
            groups["green"].append(sample)
        elif b > 0 and r < 1e-6 and g < 1e-6:
            groups["blue"].append(sample)
    for group in groups.values():
        group.sort(key=lambda sample: max(sample.rgb))
    return groups


def channel_model(samples: list[Ti3Sample], channel_index: int) -> ChannelModel:
    points = [(0.0, 0.0)]
    peak_xyz = (0.0, 0.0, 0.0)
    for sample in samples:
        level = sample.rgb[channel_index]
        points.append((level, sample.xyz[1]))
        if sample.xyz[1] >= peak_xyz[1]:
            peak_xyz = sample.xyz
    points = sorted(set((round(level, 8), y) for level, y in points), key=lambda p: p[0])
    monotonic: list[tuple[float, float]] = []
    last_y = -1.0
    for level, y_value in points:
        y_value = max(y_value, last_y + 1e-9)
        monotonic.append((level, y_value))
        last_y = y_value
    return ChannelModel(samples=monotonic, peak_xyz=peak_xyz)


def invert_y_to_level(model: ChannelModel, target_y: float) -> float:
    samples = model.samples
    if target_y <= samples[0][1]:
        return samples[0][0]
    if target_y >= samples[-1][1]:
        return samples[-1][0]
    for (x0, y0), (x1, y1) in zip(samples, samples[1:]):
        if y0 <= target_y <= y1:
            span = y1 - y0
            if span <= 0:
                return x0
            return x0 + ((target_y - y0) / span) * (x1 - x0)
    return samples[-1][0]


def invert_3x3(matrix: list[list[float]]) -> list[list[float]]:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("native primary matrix is singular")
    return [
        [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
        [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
        [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
    ]


def matvec(matrix: list[list[float]], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(sum(row[i] * vector[i] for i in range(3)) for row in matrix)  # type: ignore[return-value]


def build_curves_from_ti3(samples: list[Ti3Sample], *, size: int, gamma: float) -> tuple[list[tuple[float, float, float]], dict[str, float], float]:
    groups = classify_samples(samples)
    if not all(groups[name] for name in ["red", "green", "blue"]):
        raise ValueError("TI3 data needs red, green, and blue ramp samples for measured MHC generation")

    models = {
        "red": channel_model(groups["red"], 0),
        "green": channel_model(groups["green"], 1),
        "blue": channel_model(groups["blue"], 2),
    }
    matrix = [
        [models["red"].peak_xyz[0], models["green"].peak_xyz[0], models["blue"].peak_xyz[0]],
        [models["red"].peak_xyz[1], models["green"].peak_xyz[1], models["blue"].peak_xyz[1]],
        [models["red"].peak_xyz[2], models["green"].peak_xyz[2], models["blue"].peak_xyz[2]],
    ]
    inv_matrix = invert_3x3(matrix)
    grey_or_white = groups["grey"] or samples
    target_luminance = max(sample.xyz[1] for sample in grey_or_white)
    curves: list[tuple[float, float, float]] = []
    for index in range(size):
        input_value = index / (size - 1)
        if input_value <= 0:
            curves.append((0.0, 0.0, 0.0))
            continue
        target = white_xyz(target_luminance * (input_value**gamma))
        amounts = matvec(inv_matrix, target)
        channels = []
        for amount, name in zip(amounts, ["red", "green", "blue"]):
            desired_y = max(0.0, min(1.0, amount)) * models[name].peak_xyz[1]
            channels.append(max(0.0, min(1.0, invert_y_to_level(models[name], desired_y))))
        curves.append(tuple(channels))  # type: ignore[arg-type]

    measured_primaries: dict[str, float] = {}
    for prefix, name in [("r", "red"), ("g", "green"), ("b", "blue")]:
        x, y = xy_from_xyz(models[name].peak_xyz)
        measured_primaries[f"{prefix}x"] = x
        measured_primaries[f"{prefix}y"] = y
    return curves, measured_primaries, target_luminance


def identity_curves(size: int, gamma: float) -> list[tuple[float, float, float]]:
    curves = []
    for index in range(size):
        value = index / (size - 1)
        corrected = value ** (gamma / 2.2)
        curves.append((corrected, corrected, corrected))
    return curves


def write_cube(path: Path, curves: list[tuple[float, float, float]], title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii") as handle:
        handle.write(f'TITLE "{title}"\n')
        handle.write(f"LUT_1D_SIZE {len(curves)}\n")
        handle.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        handle.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in curves:
            handle.write(f"{r:.8f} {g:.8f} {b:.8f}\n")


def write_summary(path: Path, candidate: MhcCandidate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "DesktopLUT Calibrator MHC Candidate",
        "=" * 38,
        "",
        f"Mode: {candidate.mode}",
        f"Iteration: {candidate.iteration}",
        f"Source: {candidate.source}",
        f"Fallback: {candidate.fallback}",
        f"Target white: x={candidate.target_white['x']:.6f} y={candidate.target_white['y']:.6f}",
        f"Target gamma: {candidate.target_gamma:.3f}",
        f"Target luminance: {candidate.target_luminance:.3f} cd/m2",
        f"1D LUT: {candidate.cube_path}",
        "",
        "Measured/native primaries:",
    ]
    for prefix, label in [("r", "Red"), ("g", "Green"), ("b", "Blue")]:
        lines.append(
            f"  {label}: x={candidate.measured_primaries[f'{prefix}x']:.6f} "
            f"y={candidate.measured_primaries[f'{prefix}y']:.6f}"
        )
    if candidate.notes:
        lines.extend(["", "Notes:"])
        lines.extend(f"  - {note}" for note in candidate.notes)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def find_stage_artifact(ctx: RunContext, stage: str, role: str) -> Path | None:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") != stage:
            continue
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get(role), str):
            return resolve_run_path(ctx, artifacts[role])
    return None


def resolve_run_path(ctx: RunContext, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    candidate = ctx.root / path
    if candidate.exists():
        return candidate
    return path


def build_mhc_candidate(
    *,
    ctx: RunContext,
    iteration: int = 1,
    source_ti3: Path | None = None,
    allow_defaults: bool = False,
    lut_size: int = 4096,
    gamma: float = 2.2,
    white_x: float = D65_X,
    white_y: float = D65_Y,
) -> MhcCandidate:
    source_ti3 = source_ti3 or find_stage_artifact(ctx, "raw-mhc", "ti3")
    fallback = False
    notes: list[str] = []
    if source_ti3 and source_ti3.exists():
        samples = parse_ti3(source_ti3)
        curves, measured_primaries, target_luminance = build_curves_from_ti3(samples, size=lut_size, gamma=gamma)
        source = str(source_ti3)
    elif allow_defaults:
        fallback = True
        source = "defaults"
        measured_primaries = dict(SRGB_PRIMARIES)
        target_luminance = 100.0
        curves = identity_curves(lut_size, gamma)
        notes.append("No raw-MHC TI3 measurement was available; generated conservative sRGB/default candidate.")
    else:
        raise FileNotFoundError("raw-MHC TI3 measurement not found; pass --source-ti3 or --allow-defaults")

    generated = ctx.root / "generated"
    cube_path = generated / f"mhc_iter{iteration:02d}_{ctx.manifest.mode.lower()}_1dlut.cube"
    summary_path = generated / f"mhc_iter{iteration:02d}_{ctx.manifest.mode.lower()}_summary.txt"
    candidate_path = generated / f"mhc_iter{iteration:02d}_{ctx.manifest.mode.lower()}_candidate.json"
    write_cube(cube_path, curves, f"DesktopLUT Calibrator {ctx.manifest.mode} MHC 1D LUT iter {iteration}")

    candidate = MhcCandidate(
        iteration=iteration,
        mode=ctx.manifest.mode,
        source=source,
        target_white={"x": white_x, "y": white_y},
        target_gamma=gamma,
        target_luminance=target_luminance,
        measured_primaries=measured_primaries,
        cube_path=str(cube_path),
        summary_path=str(summary_path),
        candidate_path=str(candidate_path),
        fallback=fallback,
        notes=notes,
    )
    candidate_path.write_text(json.dumps(candidate.as_dict(), indent=2), encoding="utf-8")
    write_summary(summary_path, candidate)

    ctx.manifest.stages.append(
        {
            "stage": "build_mhc_baseline",
            "iteration": iteration,
            "status": "candidate_built",
            "candidate": str(candidate_path),
            "artifacts": {
                "candidate": str(candidate_path),
                "cube": str(cube_path),
                "summary": str(summary_path),
            },
        }
    )
    ctx.save()
    ctx.log(f"Built MHC candidate iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        "build_mhc_baseline",
        "mhc_candidate_built",
        iteration=iteration,
        candidate=str(candidate_path),
        fallback=fallback,
    )
    return candidate


def load_mhc_candidate(path: Path) -> MhcCandidate:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return MhcCandidate(**raw)


def latest_mhc_candidate(ctx: RunContext) -> Path | None:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") == "build_mhc_baseline" and isinstance(entry.get("candidate"), str):
            return Path(str(entry["candidate"]))
    return None


def apply_mhc_candidate(
    *,
    ctx: RunContext,
    client: DesktopLutClient,
    candidate_path: Path | None = None,
    monitor: int = 0,
) -> dict[str, Any]:
    candidate_path = candidate_path or latest_mhc_candidate(ctx)
    if candidate_path is None:
        raise FileNotFoundError("no MHC candidate found")
    candidate = load_mhc_candidate(candidate_path)
    responses = []
    for command in [
        client.snapshot(),
        client.disable_all(),
        client.set_mhc_primaries(monitor, candidate.mode, candidate.measured_primaries),
        client.set_mhc_white(monitor, candidate.mode, candidate.target_white["x"], candidate.target_white["y"]),
        client.set_mhc_1dlut(monitor, candidate.mode, candidate.cube_path),
        client.apply_mhc(monitor, candidate.mode),
        client.verify_mhc(monitor, candidate.mode),
    ]:
        response = client.send(command, raise_on_error=False)
        responses.append({"command": command.as_dict(), "response": response.as_dict()})
        if not response.ok:
            break
    ok = all(item["response"]["ok"] for item in responses)
    result = {"ok": ok, "candidate": str(candidate_path), "monitor": monitor, "responses": responses}
    ctx.manifest.stages.append(
        {
            "stage": "apply_mhc_baseline",
            "iteration": candidate.iteration,
            "status": "applied" if ok else "failed",
            "candidate": str(candidate_path),
            "result": result,
        }
    )
    ctx.manifest.desktoplut["last_mhc_apply"] = result
    ctx.save()
    ctx.log(f"{'Applied' if ok else 'Failed to apply'} MHC candidate iteration {candidate.iteration}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "apply_mhc_baseline",
        "mhc_candidate_applied",
        ok=ok,
        candidate=str(candidate_path),
        monitor=monitor,
    )
    return result

