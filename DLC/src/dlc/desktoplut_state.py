"""DesktopLUT state capture artifacts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import DesktopLutClient
from .events import EventWriter
from .runs import RunContext


@dataclass(frozen=True)
class DesktopLutStateCapture:
    ok: bool
    label: str
    response: dict[str, Any]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_desktoplut_state(
    *,
    ctx: RunContext,
    client: DesktopLutClient,
    label: str = "final",
    synthesize_from_manifest: bool = False,
) -> DesktopLutStateCapture:
    response = client.send(client.state_get(), raise_on_error=False)
    response_payload = response.as_dict()
    if synthesize_from_manifest and response.ok:
        response_payload["result"] = synthesize_state_from_manifest(ctx, response.result or {})
    output_dir = ctx.root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"desktoplut_state_{label}.json"
    capture = DesktopLutStateCapture(
        ok=response.ok,
        label=label,
        response=response_payload,
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(capture.as_dict(), indent=2), encoding="utf-8")

    captures = ctx.manifest.desktoplut.setdefault("state_captures", {})
    if isinstance(captures, dict):
        captures[label] = capture.as_dict()
    ctx.manifest.stages.append(
        {
            "stage": "desktoplut_state_capture",
            "status": "captured" if response.ok else "failed",
            "label": label,
            "artifact": str(artifact),
        }
    )
    ctx.save()
    ctx.log(f"DesktopLUT state capture {label}: {'ok' if response.ok else 'failed'}")
    EventWriter(ctx.events_path).write(
        "INFO" if response.ok else "ERROR",
        "desktoplut_state",
        "state_captured",
        label=label,
        ok=response.ok,
        artifact=str(artifact),
    )
    return capture


def synthesize_state_from_manifest(ctx: RunContext, base_state: dict[str, Any]) -> dict[str, Any]:
    state = dict(base_state)
    state["running"] = True
    calibration = ctx.manifest.desktoplut.get("calibration_mode")
    if isinstance(calibration, dict) and isinstance(calibration.get("result"), dict):
        state["calibration_mode"] = calibration["result"]

    mhc_apply = ctx.manifest.desktoplut.get("last_mhc_apply")
    if isinstance(mhc_apply, dict) and mhc_apply.get("ok"):
        monitor = int(mhc_apply.get("monitor", 0))
        candidate_path = mhc_apply.get("candidate")
        if isinstance(candidate_path, str):
            try:
                candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                candidate = {}
            mode = str(candidate.get("mode", ctx.manifest.mode)).upper()
            state.setdefault("mhc", {})[f"{monitor}:{mode}"] = {
                "primaries": candidate.get("measured_primaries", {}),
                "white": candidate.get("target_white", {}),
                "cube_path": candidate.get("cube_path"),
                "applied": True,
                "synthetic_from_manifest": True,
            }

    lut_apply = ctx.manifest.desktoplut.get("last_3dlut_apply")
    if isinstance(lut_apply, dict) and lut_apply.get("ok"):
        cube = lut_apply.get("cube")
        if isinstance(cube, str):
            iteration = None
            for entry in reversed(ctx.manifest.stages):
                if entry.get("stage") == "apply_3dlut" and entry.get("cube") == cube:
                    iteration = entry.get("iteration")
                    break
            state.setdefault("runtime", {})[f"0:{ctx.manifest.mode.upper()}"] = {
                "cube_path": cube,
                "iteration": iteration,
                "synthetic_from_manifest": True,
            }
    return state

