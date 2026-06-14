"""One-command unattended run launcher."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .dashboard import DashboardOptions, write_dashboard_html, write_readout_html
from .events import EventWriter
from .live_setup import resolve_live_meter_port, resolve_live_monitor_hint
from .monitor import write_run_health
from .preflight import record_tool_preflight_stage, write_tool_preflight
from .readiness import ReadinessResult, write_readiness_audit
from .runs import RunContext, open_run
from .supervise import SuperviseResult, supervise_run
from .tools import ToolSet
from .windows_local import write_windows_local_audit


@dataclass(frozen=True)
class UnattendedRunResult:
    ok: bool
    run: str
    readiness: dict[str, Any]
    supervised: bool
    supervision: dict[str, Any] | None
    health: dict[str, Any]
    dashboard: str | None
    readout: str | None
    handoff: str | None
    tool_preflight: dict[str, Any] | None
    windows_local_audit: dict[str, Any] | None
    artifact: str
    stopped_reason: str
    simulate_execution: bool
    complete: bool
    completion_evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_run_path(ctx: RunContext, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else ctx.root / path


def _latest_stage_artifact(ctx: RunContext, stage: str, status: str) -> str | None:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") == stage and entry.get("status") == status:
            artifact = entry.get("artifact")
            return artifact if isinstance(artifact, str) else None
    return None


def _same_path(ctx: RunContext, left: str | None, right: str | None) -> bool:
    left_path = _resolve_run_path(ctx, left)
    right_path = _resolve_run_path(ctx, right)
    if left_path is None or right_path is None:
        return False
    return str(left_path).lower() == str(right_path).lower()


def completion_evidence(ctx: RunContext) -> dict[str, Any]:
    audit_artifact = _latest_stage_artifact(ctx, "final_audit", "passed")
    finalization_artifact = _latest_stage_artifact(ctx, "finalization", "finalized")
    audit_payload = _read_json(_resolve_run_path(ctx, audit_artifact))
    finalization_payload = _read_json(_resolve_run_path(ctx, finalization_artifact))
    final_report = finalization_payload.get("final_report") if isinstance(finalization_payload, dict) else None
    audit_ok = isinstance(audit_payload, dict) and audit_payload.get("ok") is True
    finalization_ok = isinstance(finalization_payload, dict) and finalization_payload.get("ok") is True
    finalization_current_audit_ok = isinstance(finalization_payload, dict) and finalization_payload.get("current_audit_ok") is True
    finalization_current_audit_failures = (
        finalization_payload.get("current_audit_failures")
        if isinstance(finalization_payload, dict) and isinstance(finalization_payload.get("current_audit_failures"), list)
        else None
    )
    finalization_links_audit = (
        isinstance(finalization_payload, dict)
        and isinstance(finalization_payload.get("audit_artifact"), str)
        and _same_path(ctx, str(finalization_payload["audit_artifact"]), audit_artifact)
    )
    final_report_exists = isinstance(final_report, str) and bool((_resolve_run_path(ctx, final_report) or Path()).exists())
    manifest_finalized = ctx.manifest.status == "finalized"
    ok = all([manifest_finalized, audit_ok, finalization_ok, finalization_current_audit_ok, finalization_links_audit, final_report_exists])
    return {
        "ok": ok,
        "manifest_finalized": manifest_finalized,
        "final_audit_ok": audit_ok,
        "final_audit_artifact": audit_artifact,
        "finalization_ok": finalization_ok,
        "finalization_artifact": finalization_artifact,
        "finalization_current_audit_ok": finalization_current_audit_ok,
        "finalization_current_audit_failures": finalization_current_audit_failures,
        "finalization_links_audit": finalization_links_audit,
        "final_report_exists": final_report_exists,
        "final_report": final_report,
    }


def run_unattended(
    *,
    ctx: RunContext,
    tools: ToolSet,
    port: int | None,
    max_steps: int = 50,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
    skip_self_test_gate: bool = False,
    self_test_max_age_hours: float = 24.0,
    skip_windows_local_audit_gate: bool = False,
    windows_local_audit_label: str = "preflight",
    auto_tool_preflight: bool = True,
    auto_windows_local_audit: bool = True,
    windows_monitor_hint: str | None = None,
    windows_gamma_tolerance: int = 257,
    update_dashboard: bool = False,
    dashboard_refresh_seconds: int = 5,
    write_handoff: bool = True,
    stale_after_seconds: int = 900,
    output: Path | None = None,
) -> UnattendedRunResult:
    port = resolve_live_meter_port(ctx, port)
    windows_monitor_hint = resolve_live_monitor_hint(ctx, windows_monitor_hint)
    current = open_run(ctx.root)
    current.manifest.desktoplut["unattended_options"] = {
        "execute_safe": execute_safe,
        "allow_hardware": allow_hardware,
        "allow_live_desktoplut": allow_live_desktoplut,
        "allow_builds": allow_builds,
        "mock_desktoplut": mock_desktoplut,
        "simulate_execution": simulate_execution,
    }
    current.save()
    live_side_effects = (allow_hardware or allow_live_desktoplut or allow_builds) and not simulate_execution
    tool_preflight = None
    if auto_tool_preflight:
        tool_preflight_path = ctx.root / "preflight" / "tool_preflight.json"
        tool_preflight = write_tool_preflight(tools, tool_preflight_path)
        current = open_run(ctx.root)
        record_tool_preflight_stage(current, tool_preflight)
    windows_local_audit = None
    if live_side_effects and not skip_windows_local_audit_gate and auto_windows_local_audit:
        current = open_run(ctx.root)
        audits = current.manifest.desktoplut.get("windows_local_audits")
        existing = audits.get(windows_local_audit_label) if isinstance(audits, dict) else None
        if isinstance(existing, dict) and existing.get("ok") is True:
            windows_local_audit = existing
        else:
            audit = write_windows_local_audit(
                ctx=current,
                label=windows_local_audit_label,
                monitor_hint=windows_monitor_hint,
                gamma_tolerance=windows_gamma_tolerance,
            )
            windows_local_audit = audit.as_dict()

    readiness: ReadinessResult = write_readiness_audit(
        ctx=open_run(ctx.root),
        tools=tools,
        port=port,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
        skip_self_test_gate=skip_self_test_gate,
        self_test_max_age_hours=self_test_max_age_hours,
        skip_windows_local_audit_gate=skip_windows_local_audit_gate,
        windows_local_audit_label=windows_local_audit_label,
    )
    dashboard_path = None
    readout_path = None
    if update_dashboard:
        dashboard_path = str(
            write_dashboard_html(
                open_run(ctx.root),
                port=port,
                refresh_seconds=dashboard_refresh_seconds,
                record_stage=False,
                execute_safe=execute_safe,
                allow_hardware=allow_hardware,
                allow_live_desktoplut=allow_live_desktoplut,
                allow_builds=allow_builds,
                mock_desktoplut=mock_desktoplut,
                simulate_execution=simulate_execution,
            )
        )
        readout_path = str(
            write_readout_html(
                open_run(ctx.root),
                port=port,
                refresh_seconds=dashboard_refresh_seconds,
                record_stage=False,
                execute_safe=execute_safe,
                allow_hardware=allow_hardware,
                allow_live_desktoplut=allow_live_desktoplut,
                allow_builds=allow_builds,
                mock_desktoplut=mock_desktoplut,
                simulate_execution=simulate_execution,
            )
        )

    supervision: SuperviseResult | None = None
    stopped_reason = "readiness blocked"
    if readiness.ready_to_continue:
        supervision = supervise_run(
            open_run(ctx.root),
            port=port,
            max_steps=max_steps,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
            update_dashboard=update_dashboard,
            dashboard_refresh_seconds=dashboard_refresh_seconds,
        )
        stopped_reason = supervision.stopped_reason
        dashboard_path = supervision.dashboard or dashboard_path
        readout_path = supervision.readout or readout_path

    health = write_run_health(open_run(ctx.root), stale_after_seconds=stale_after_seconds)
    evidence = completion_evidence(open_run(ctx.root))
    complete = bool(evidence["ok"])
    supervision_ok = supervision.ok if supervision else False
    ok = readiness.ready_to_continue and supervision_ok and health.ok and complete
    stage_status = "completed" if ok else ("incomplete" if readiness.ready_to_continue and supervision_ok and health.ok else "blocked")

    report_dir = ctx.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact = output or report_dir / "unattended.json"
    result = UnattendedRunResult(
        ok=ok,
        run=str(ctx.root),
        readiness=readiness.as_dict(),
        supervised=supervision is not None,
        supervision=supervision.as_dict() if supervision else None,
        health=health.as_dict(),
        dashboard=dashboard_path,
        readout=readout_path,
        handoff=None,
        tool_preflight=tool_preflight,
        windows_local_audit=windows_local_audit,
        artifact=str(artifact),
        stopped_reason=stopped_reason,
        simulate_execution=simulate_execution,
        complete=complete,
        completion_evidence=evidence,
    )
    artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    current = open_run(ctx.root)
    current.manifest.stages.append(
        {
            "stage": "unattended",
            "status": stage_status,
            "artifact": str(artifact),
            "supervised": supervision is not None,
            "stopped_reason": stopped_reason,
            "simulate_execution": simulate_execution,
            "complete": complete,
        }
    )
    current.save()
    current.log(f"Unattended launcher stopped: {stopped_reason}")
    EventWriter(current.events_path).write(
        "INFO" if ok else "WARNING",
        "unattended",
        "unattended_finished",
        ok=ok,
        supervised=supervision is not None,
        stopped_reason=stopped_reason,
        artifact=str(artifact),
        simulate_execution=simulate_execution,
    )
    if write_handoff:
        from .handoff import write_agent_handoff

        handoff = write_agent_handoff(
            open_run(ctx.root),
            options=DashboardOptions(
                port=port,
                refresh_seconds=dashboard_refresh_seconds,
                execute_safe=execute_safe,
                allow_hardware=allow_hardware,
                allow_live_desktoplut=allow_live_desktoplut,
                allow_builds=allow_builds,
                mock_desktoplut=mock_desktoplut,
                simulate_execution=simulate_execution,
                self_test_max_age_hours=self_test_max_age_hours,
                windows_local_audit_label=windows_local_audit_label,
            ),
        )
        result = UnattendedRunResult(
            ok=ok,
            run=str(ctx.root),
            readiness=readiness.as_dict(),
            supervised=supervision is not None,
            supervision=supervision.as_dict() if supervision else None,
            health=health.as_dict(),
            dashboard=dashboard_path,
            readout=readout_path,
            handoff=handoff.artifact,
            tool_preflight=tool_preflight,
            windows_local_audit=windows_local_audit,
            artifact=str(artifact),
            stopped_reason=stopped_reason,
            simulate_execution=simulate_execution,
            complete=complete,
            completion_evidence=evidence,
        )
        artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
        current = open_run(ctx.root)
        for entry in reversed(current.manifest.stages):
            if entry.get("stage") == "unattended" and entry.get("artifact") == str(artifact):
                entry["handoff"] = handoff.artifact
                break
        current.save()
    return result

