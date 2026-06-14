"""Compact calibration-loop status for agent supervision."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import EventWriter
from .runs import RunContext


@dataclass(frozen=True)
class PhaseLoopStatus:
    phase: str
    status: str
    latest_iteration: int | None
    latest_decision: str | None
    reason: str
    decision_artifact: str | None
    metrics_artifact: str | None
    next_params: dict[str, Any]
    decisions: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LoopStatus:
    ok: bool
    run: str
    generated_at: str
    phases: dict[str, PhaseLoopStatus]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["phases"] = {phase: status.as_dict() for phase, status in self.phases.items()}
        return payload


def _entry_iteration(entry: dict[str, Any]) -> int | None:
    value = entry.get("iteration")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _decision_entries(ctx: RunContext, phase: str) -> list[dict[str, Any]]:
    entries = [entry for entry in ctx.manifest.stages if entry.get("stage") == f"{phase}_decision"]
    return sorted(entries, key=lambda entry: (_entry_iteration(entry) or 0, str(entry.get("decision", ""))))


def _load_decision_payload(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    decision_path = Path(path)
    if not decision_path.exists():
        return {}
    try:
        payload = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _phase_status(ctx: RunContext, phase: str) -> PhaseLoopStatus:
    entries = _decision_entries(ctx, phase)
    decisions = []
    for entry in entries:
        payload = _load_decision_payload(entry.get("decision") if isinstance(entry.get("decision"), str) else None)
        decisions.append(
            {
                "iteration": _entry_iteration(entry),
                "decision": payload.get("decision", entry.get("status")),
                "reason": payload.get("reason", entry.get("reason", "")),
                "decision_artifact": entry.get("decision"),
                "metrics_artifact": payload.get("metrics_json", entry.get("metrics")),
                "next_params": payload.get("next_params", entry.get("next_params", {})),
            }
        )
    latest = decisions[-1] if decisions else {}
    latest_decision = latest.get("decision") if latest else None
    if latest_decision == "stop":
        status = "stopped"
    elif latest_decision == "continue":
        status = "continuing"
    else:
        status = "pending"
    return PhaseLoopStatus(
        phase=phase,
        status=status,
        latest_iteration=latest.get("iteration") if isinstance(latest.get("iteration"), int) else None,
        latest_decision=str(latest_decision) if latest_decision else None,
        reason=str(latest.get("reason", "no loop decision recorded")),
        decision_artifact=str(latest.get("decision_artifact")) if latest.get("decision_artifact") else None,
        metrics_artifact=str(latest.get("metrics_artifact")) if latest.get("metrics_artifact") else None,
        next_params=latest.get("next_params") if isinstance(latest.get("next_params"), dict) else {},
        decisions=decisions,
    )


def build_loop_status(ctx: RunContext, *, output: Path | None = None) -> LoopStatus:
    target = output or ctx.root / "reports" / "loop_status.json"
    phases = {phase: _phase_status(ctx, phase) for phase in ["mhc", "3dlut"]}
    ok = all(status.status == "stopped" for status in phases.values())
    return LoopStatus(
        ok=ok,
        run=str(ctx.root),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        phases=phases,
        artifact=str(target),
    )


def write_loop_status(
    *,
    ctx: RunContext,
    output: Path | None = None,
    record_stage: bool = True,
) -> LoopStatus:
    result = build_loop_status(ctx, output=output)
    target = Path(result.artifact)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.desktoplut["loop_status"] = result.as_dict()
    if record_stage:
        ctx.manifest.stages.append(
            {
                "stage": "loop_status",
                "status": "stopped" if result.ok else "active",
                "artifact": str(target),
                "mhc": result.phases["mhc"].status,
                "3dlut": result.phases["3dlut"].status,
            }
        )
    ctx.save()
    EventWriter(ctx.events_path).write(
        "INFO",
        "loop_status",
        "loop_status_written",
        ok=result.ok,
        artifact=str(target),
        mhc=result.phases["mhc"].status,
        lut3d=result.phases["3dlut"].status,
    )
    return result

