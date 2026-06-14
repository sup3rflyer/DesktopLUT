"""DLC-owned patch sequence and presenter primitives."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .dogegen import RGBW_HDR, RGBW_SDR
from .drift import DriftPlan
from .events import EventWriter
from .runs import RunContext

SequenceKind = Literal["rgbw", "drift", "custom"]


@dataclass(frozen=True)
class PatchStep:
    name: str
    rgb: tuple[int, int, int]
    bit_depth: int = 8
    duration_seconds: float = 0.5
    role: str = "measurement"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatchSequence:
    name: str
    mode: str
    kind: SequenceKind
    bit_depth: int
    patch_size_percent: int
    background_rgb: tuple[int, int, int]
    steps: list[PatchStep]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.as_dict() for step in self.steps]
        return payload


@dataclass(frozen=True)
class PresenterEvent:
    index: int
    name: str
    rgb: tuple[int, int, int]
    css_rgb: tuple[int, int, int]
    role: str
    duration_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


PresenterCallback = Callable[[PresenterEvent], Any]


def _max_code(bit_depth: int) -> int:
    if bit_depth <= 0:
        raise ValueError("bit depth must be positive")
    return (2**bit_depth) - 1


def code_to_css_rgb(rgb: tuple[int, int, int], bit_depth: int) -> tuple[int, int, int]:
    scale = _max_code(bit_depth)
    return tuple(min(255, max(0, round((channel / scale) * 255))) for channel in rgb)  # type: ignore[return-value]


def build_rgbw_sequence(
    *,
    mode: str,
    patch_size_percent: int = 100,
    duration_seconds: float = 0.5,
) -> PatchSequence:
    upper_mode = mode.upper()
    source = RGBW_SDR if upper_mode == "SDR" else RGBW_HDR
    bit_depth = 8 if upper_mode == "SDR" else 10
    steps = [
        PatchStep(
            name=name,
            rgb=rgb,
            bit_depth=bit_depth,
            duration_seconds=duration_seconds,
            role="probe_match_rgbw",
        )
        for name, rgb in source.items()
    ]
    notes = [
        "This is the DLC-native replacement path for Dogegen RGBW patch display.",
        "The current Tk presenter is SDR/desktop preview oriented; HDR code values are preserved in the sequence artifact.",
    ]
    return PatchSequence(
        name=f"{upper_mode.lower()}_rgbw_probe_match",
        mode=upper_mode,
        kind="rgbw",
        bit_depth=bit_depth,
        patch_size_percent=patch_size_percent,
        background_rgb=(0, 0, 0),
        steps=steps,
        notes=notes,
    )


def build_drift_sequence(
    *,
    drift_plan: DriftPlan,
    mode: str,
    patch_size_percent: int = 100,
    duration_seconds: float = 0.5,
) -> PatchSequence:
    upper_mode = mode.upper()
    steps = [
        PatchStep(
            name=f"{patch.role}_{index:03d}",
            rgb=patch.rgb,
            bit_depth=8,
            duration_seconds=duration_seconds,
            role=patch.role,
            metadata={
                "gray_level": patch.gray_level,
                "bias_channel": patch.bias_channel,
                "bias": patch.bias,
            },
        )
        for index, patch in enumerate(drift_plan.patches, start=1)
    ]
    notes = [
        "Adaptive drift sequence generated from drift-plan output.",
        "A future live scheduler should decide repeats from drift-evaluate results between steps.",
    ]
    return PatchSequence(
        name=f"{upper_mode.lower()}_{drift_plan.stage}_adaptive_drift",
        mode=upper_mode,
        kind="drift",
        bit_depth=8,
        patch_size_percent=patch_size_percent,
        background_rgb=(0, 0, 0),
        steps=steps,
        notes=notes,
    )


def load_drift_plan(path: Path) -> DriftPlan:
    from .drift import DriftPatch

    raw = json.loads(path.read_text(encoding="utf-8"))
    patches = [
        DriftPatch(
            rgb=tuple(item["rgb"]),  # type: ignore[arg-type]
            role=item["role"],
            gray_level=int(item["gray_level"]),
            bias_channel=item.get("bias_channel"),
            bias=int(item.get("bias", 0)),
        )
        for item in raw.get("patches", [])
    ]
    return DriftPlan(
        stage=raw["stage"],
        iteration=int(raw["iteration"]),
        delta_threshold=float(raw["delta_threshold"]),
        coldest_channel=raw.get("coldest_channel"),
        gray_levels=[int(level) for level in raw.get("gray_levels", [])],
        bias=int(raw["bias"]),
        max_repeats=int(raw["max_repeats"]),
        settle_required=int(raw["settle_required"]),
        patches=patches,
        notes=raw.get("notes", []),
    )


def write_patch_sequence(
    *,
    ctx: RunContext,
    sequence: PatchSequence,
    stage: str,
    iteration: int = 1,
) -> Path:
    output = ctx.root / "sequences" / f"{stage}_iter{iteration:02d}_{sequence.kind}_patch_sequence.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(sequence.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "patch_sequence",
            "target_stage": stage,
            "iteration": iteration,
            "status": "planned",
            "kind": sequence.kind,
            "sequence": str(output),
            "patch_count": len(sequence.steps),
        }
    )
    ctx.save()
    ctx.log(f"Planned {sequence.kind} patch sequence for {stage} iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        "patch_sequence",
        "patch_sequence_planned",
        target_stage=stage,
        iteration=iteration,
        kind=sequence.kind,
        sequence=str(output),
        patch_count=len(sequence.steps),
    )
    return output


def load_patch_sequence(path: Path) -> PatchSequence:
    raw = json.loads(path.read_text(encoding="utf-8"))
    steps = [
        PatchStep(
            name=item["name"],
            rgb=tuple(item["rgb"]),  # type: ignore[arg-type]
            bit_depth=int(item.get("bit_depth", raw.get("bit_depth", 8))),
            duration_seconds=float(item.get("duration_seconds", 0.5)),
            role=item.get("role", "measurement"),
            metadata=item.get("metadata", {}),
        )
        for item in raw.get("steps", [])
    ]
    return PatchSequence(
        name=raw["name"],
        mode=raw["mode"],
        kind=raw.get("kind", "custom"),
        bit_depth=int(raw.get("bit_depth", 8)),
        patch_size_percent=int(raw.get("patch_size_percent", 100)),
        background_rgb=tuple(raw.get("background_rgb", [0, 0, 0])),  # type: ignore[arg-type]
        steps=steps,
        notes=raw.get("notes", []),
    )


def preview_sequence(sequence: PatchSequence) -> list[PresenterEvent]:
    return [
        PresenterEvent(
            index=index,
            name=step.name,
            rgb=step.rgb,
            css_rgb=code_to_css_rgb(step.rgb, step.bit_depth),
            role=step.role,
            duration_seconds=step.duration_seconds,
        )
        for index, step in enumerate(sequence.steps, start=1)
    ]


def run_scripted_presenter(
    sequence: PatchSequence,
    on_patch: PresenterCallback | None = None,
    *,
    sleep: bool = True,
) -> list[PresenterEvent]:
    events = preview_sequence(sequence)
    for event in events:
        if sleep:
            time.sleep(max(0.0, event.duration_seconds))
        if on_patch is not None:
            on_patch(event)
    return events


def run_tk_presenter(sequence: PatchSequence, on_patch: PresenterCallback | None = None) -> list[PresenterEvent]:
    import tkinter as tk

    events = preview_sequence(sequence)
    root = tk.Tk()
    root.title("DesktopLUT Calibrator Patch Presenter")
    root.configure(bg="black")
    root.attributes("-fullscreen", True)
    root.config(cursor="none")
    canvas = tk.Canvas(root, highlightthickness=0, bg=_hex_color(code_to_css_rgb(sequence.background_rgb, sequence.bit_depth)))
    canvas.pack(fill="both", expand=True)
    root.update_idletasks()

    def draw_patch(event: PresenterEvent) -> None:
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        size = max(1, min(width, height) * sequence.patch_size_percent / 100.0)
        left = (width - size) / 2.0
        top = (height - size) / 2.0
        canvas.create_rectangle(
            left,
            top,
            left + size,
            top + size,
            fill=_hex_color(event.css_rgb),
            outline=_hex_color(event.css_rgb),
        )
        root.update()

    try:
        for event in events:
            draw_patch(event)
            time.sleep(max(0.0, event.duration_seconds))
            if on_patch is not None:
                on_patch(event)
    finally:
        root.destroy()
    return events


def _hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)

