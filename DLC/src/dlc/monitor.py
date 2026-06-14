"""Run health monitoring for long unattended calibration sessions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import EventWriter, read_events
from .runs import RunContext


TERMINAL_HEALTHY_STATUSES = {"finalized", "audited"}


@dataclass(frozen=True)
class RunHealth:
    ok: bool
    status: str
    reasons: list[str]
    stale_after_seconds: int
    seconds_since_last_event: float | None
    seconds_since_active_command: float | None
    last_event: dict[str, Any] | None
    active_command: dict[str, Any] | None
    failed_stage_count: int
    error_event_count: int
    artifact: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _failed_stages(ctx: RunContext) -> list[dict[str, Any]]:
    failed = []
    for entry in ctx.manifest.stages:
        status = str(entry.get("status", "")).lower()
        if status in {"failed", "blocked"}:
            failed.append(entry)
    return failed


def _latest_active_command(events: list[Any]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.event in {"supervise_command_finished", "run_stage_command_finished"}:
            return None
        if event.event in {"supervise_command_started", "run_stage_command_started"}:
            argv = event.data.get("argv")
            return {
                "time": event.time,
                "stage": event.stage,
                "event": event.event,
                "index": event.data.get("index"),
                "action": event.data.get("action"),
                "argv": argv if isinstance(argv, list) else [],
                "running": True,
            }
    return None


def evaluate_run_health(
    ctx: RunContext,
    *,
    stale_after_seconds: int = 900,
    now: datetime | None = None,
) -> RunHealth:
    now = now or datetime.now()
    events = read_events(ctx.events_path)
    last_event = events[-1] if events else None
    last_event_dict = asdict(last_event) if last_event else None
    error_events = [event for event in events if event.level.upper() == "ERROR"]
    failed_stages = _failed_stages(ctx)
    active_command = _latest_active_command(events)
    reasons: list[str] = []
    seconds_since_last_event: float | None = None
    seconds_since_active_command: float | None = None

    if last_event is None:
        reasons.append("no events recorded")
    else:
        timestamp = _parse_time(last_event.time)
        if timestamp is None:
            reasons.append("last event timestamp is unreadable")
        else:
            seconds_since_last_event = max(0.0, (now - timestamp).total_seconds())
            if (
                seconds_since_last_event > stale_after_seconds
                and ctx.manifest.status not in TERMINAL_HEALTHY_STATUSES
            ):
                reasons.append(f"last event is stale by {int(seconds_since_last_event)} seconds")

    if active_command is not None:
        timestamp = _parse_time(str(active_command.get("time", "")))
        if timestamp is None:
            reasons.append("active command timestamp is unreadable")
        else:
            seconds_since_active_command = max(0.0, (now - timestamp).total_seconds())
            if (
                seconds_since_active_command > stale_after_seconds
                and ctx.manifest.status not in TERMINAL_HEALTHY_STATUSES
            ):
                action = active_command.get("action") or active_command.get("stage") or "command"
                reasons.append(f"active command {action} is stale after running for {int(seconds_since_active_command)} seconds")

    if error_events:
        reasons.append(f"{len(error_events)} error event(s) recorded")
    if failed_stages:
        reasons.append(f"{len(failed_stages)} failed/blocked stage(s) recorded")

    if failed_stages or error_events:
        status = "failed"
    elif reasons:
        status = "stale" if any("stale" in reason for reason in reasons) else "attention"
    elif ctx.manifest.status == "finalized":
        status = "finalized"
    else:
        status = "healthy"

    return RunHealth(
        ok=status in {"healthy", "finalized"},
        status=status,
        reasons=reasons,
        stale_after_seconds=stale_after_seconds,
        seconds_since_last_event=seconds_since_last_event,
        seconds_since_active_command=seconds_since_active_command,
        last_event=last_event_dict,
        active_command=active_command,
        failed_stage_count=len(failed_stages),
        error_event_count=len(error_events),
    )


def write_run_health(
    ctx: RunContext,
    *,
    stale_after_seconds: int = 900,
    output: Path | None = None,
) -> RunHealth:
    report_dir = ctx.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact = output or report_dir / "monitor.json"
    health = evaluate_run_health(ctx, stale_after_seconds=stale_after_seconds)
    health = RunHealth(
        ok=health.ok,
        status=health.status,
        reasons=health.reasons,
        stale_after_seconds=health.stale_after_seconds,
        seconds_since_last_event=health.seconds_since_last_event,
        seconds_since_active_command=health.seconds_since_active_command,
        last_event=health.last_event,
        active_command=health.active_command,
        failed_stage_count=health.failed_stage_count,
        error_event_count=health.error_event_count,
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(health.as_dict(), indent=2), encoding="utf-8")

    ctx.manifest.stages.append(
        {
            "stage": "monitor",
            "status": health.status,
            "artifact": str(artifact),
            "ok": health.ok,
        }
    )
    ctx.save()
    ctx.log(f"Monitor health: {health.status}")
    EventWriter(ctx.events_path).write(
        "INFO" if health.ok else "WARNING",
        "monitor",
        "run_health_written",
        status=health.status,
        ok=health.ok,
        artifact=str(artifact),
    )
    return health

