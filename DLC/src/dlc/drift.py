"""Adaptive gray-drift planning helpers.

This is a scheduler contract for a future DLC-owned patch display. Argyll
`dispread` cannot adapt mid-sequence, but these artifacts let an agent plan and
evaluate the drift logic that a custom presenter should eventually execute.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from .events import EventWriter
from .runs import RunContext

Channel = Literal["R", "G", "B"]
CHANNEL_INDEX: dict[Channel, int] = {"R": 0, "G": 1, "B": 2}
CHANNELS: tuple[Channel, Channel, Channel] = ("R", "G", "B")


@dataclass(frozen=True)
class DriftPatch:
    rgb: tuple[int, int, int]
    role: str
    gray_level: int
    bias_channel: Channel | None = None
    bias: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DriftPlan:
    stage: str
    iteration: int
    delta_threshold: float
    coldest_channel: Channel | None
    gray_levels: list[int]
    bias: int
    max_repeats: int
    settle_required: int
    patches: list[DriftPatch]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["patches"] = [patch.as_dict() for patch in self.patches]
        return payload


@dataclass(frozen=True)
class DriftEvaluation:
    coldest_channel: Channel
    channel_deltas: dict[Channel, float]
    max_channel_delta: float
    repeat: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clamp_8bit(value: int) -> int:
    return min(255, max(0, int(value)))


def adaptive_gray_patch(gray_level: int, coldest_channel: Channel, bias: int) -> tuple[int, int, int]:
    values = [clamp_8bit(gray_level)] * 3
    values[CHANNEL_INDEX[coldest_channel]] = clamp_8bit(values[CHANNEL_INDEX[coldest_channel]] + bias)
    return (values[0], values[1], values[2])


def xyz_to_linear_srgb(xyz: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = xyz
    return (
        (3.2406 * x) + (-1.5372 * y) + (-0.4986 * z),
        (-0.9689 * x) + (1.8758 * y) + (0.0415 * z),
        (0.0557 * x) + (-0.2040 * y) + (1.0570 * z),
    )


def normalized_channels(xyz: tuple[float, float, float]) -> dict[Channel, float]:
    """A meter XYZ reading expressed as per-channel **linear-sRGB intensities normalized to the
    peak channel** — i.e. {"R","G","B"} each in [0, 1] with the brightest = 1.0. Converts XYZ →
    linear sRGB (clamping negatives), then divides by the peak, so it is a hue/balance fingerprint
    independent of overall luminance (used to find the coldest channel for drift tracking)."""
    rgb = tuple(max(0.0, value) for value in xyz_to_linear_srgb(xyz))
    peak = max(rgb)
    if peak <= 0:
        return {"R": 0.0, "G": 0.0, "B": 0.0}
    return {channel: rgb[index] / peak for channel, index in CHANNEL_INDEX.items()}


def coldest_channel_from_xyz(xyz: tuple[float, float, float]) -> Channel:
    normalized = normalized_channels(xyz)
    return min(CHANNELS, key=lambda channel: normalized[channel])


def evaluate_drift(
    *,
    stabilized_xyz: tuple[float, float, float],
    current_xyz: tuple[float, float, float],
    delta_threshold: float,
) -> DriftEvaluation:
    stabilized = normalized_channels(stabilized_xyz)
    current = normalized_channels(current_xyz)
    channel_deltas = {channel: abs(current[channel] - stabilized[channel]) for channel in CHANNELS}
    max_delta = max(channel_deltas.values())
    repeat = max_delta > delta_threshold
    coldest = min(CHANNELS, key=lambda channel: current[channel])
    reason = (
        f"max channel delta {max_delta:.6f} exceeds threshold {delta_threshold:.6f}"
        if repeat
        else f"max channel delta {max_delta:.6f} is within threshold {delta_threshold:.6f}"
    )
    return DriftEvaluation(
        coldest_channel=coldest,
        channel_deltas=channel_deltas,
        max_channel_delta=max_delta,
        repeat=repeat,
        reason=reason,
    )


def build_drift_plan(
    *,
    stage: str,
    iteration: int = 1,
    coldest_channel: Channel | None = None,
    gray_levels: Iterable[int] = (32, 64, 96, 128, 160, 192, 224, 242),
    bias: int = 4,
    delta_threshold: float = 0.003,
    max_repeats: int = 3,
    settle_required: int = 2,
) -> DriftPlan:
    levels = [clamp_8bit(level) for level in gray_levels]
    if not levels:
        raise ValueError("at least one gray level is required")
    if coldest_channel is not None and coldest_channel not in CHANNELS:
        raise ValueError(f"unknown coldest channel: {coldest_channel}")
    if bias < 0:
        raise ValueError("bias must be non-negative")
    if delta_threshold <= 0:
        raise ValueError("delta threshold must be positive")

    patches: list[DriftPatch] = []
    for level in levels:
        neutral = (level, level, level)
        patches.append(DriftPatch(rgb=neutral, role="stabilize_gray", gray_level=level))
        if coldest_channel is None:
            for channel in CHANNELS:
                patches.append(
                    DriftPatch(
                        rgb=adaptive_gray_patch(level, channel, bias),
                        role="candidate_cold_channel_probe",
                        gray_level=level,
                        bias_channel=channel,
                        bias=bias,
                    )
                )
        else:
            patches.append(
                DriftPatch(
                    rgb=adaptive_gray_patch(level, coldest_channel, bias),
                    role="cold_channel_balance_probe",
                    gray_level=level,
                    bias_channel=coldest_channel,
                    bias=bias,
                )
            )

    notes = [
        "Use neutral stabilize_gray patches to establish the current settled channel balance.",
        "Repeat the current gray level when evaluated channel delta exceeds delta_threshold.",
        "Bias probes raise the coldest channel slightly; final measured data must still preserve the original target patch.",
        "This plan is for a future DLC-owned presenter; Argyll dispread cannot adapt mid-run.",
    ]
    return DriftPlan(
        stage=stage,
        iteration=iteration,
        delta_threshold=delta_threshold,
        coldest_channel=coldest_channel,
        gray_levels=levels,
        bias=bias,
        max_repeats=max_repeats,
        settle_required=settle_required,
        patches=patches,
        notes=notes,
    )


def write_drift_plan(
    *,
    ctx: RunContext,
    stage: str,
    iteration: int = 1,
    coldest_channel: Channel | None = None,
    gray_levels: Iterable[int] = (32, 64, 96, 128, 160, 192, 224, 242),
    bias: int = 4,
    delta_threshold: float = 0.003,
    max_repeats: int = 3,
    settle_required: int = 2,
) -> Path:
    plan = build_drift_plan(
        stage=stage,
        iteration=iteration,
        coldest_channel=coldest_channel,
        gray_levels=gray_levels,
        bias=bias,
        delta_threshold=delta_threshold,
        max_repeats=max_repeats,
        settle_required=settle_required,
    )
    output = ctx.root / "sequences" / f"{stage}_iter{iteration:02d}_adaptive_drift_plan.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "adaptive_drift",
            "target_stage": stage,
            "iteration": iteration,
            "status": "planned",
            "plan": str(output),
            "coldest_channel": coldest_channel,
            "delta_threshold": delta_threshold,
        }
    )
    ctx.save()
    ctx.log(f"Planned adaptive drift schedule for {stage} iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        "adaptive_drift",
        "adaptive_drift_planned",
        target_stage=stage,
        iteration=iteration,
        plan=str(output),
        coldest_channel=coldest_channel,
        delta_threshold=delta_threshold,
        patch_count=len(plan.patches),
    )
    return output

