"""Agent handoff packet for resumable calibration runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import scan_artifacts
from .dashboard import DashboardOptions, dashboard_status_payload
from .events import EventWriter
from .runs import RunContext, open_run


@dataclass(frozen=True)
class AgentHandoff:
    ok: bool
    run: str
    generated_at: str
    status: dict[str, Any]
    latest_self_test: dict[str, Any] | None
    self_test_gate: dict[str, Any]
    latest_unattended: dict[str, Any] | None
    latest_tool_preflight: dict[str, Any] | None
    operator_handoff: dict[str, Any]
    artifact_count: int
    suggested_commands: dict[str, str]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _flag_args(options: DashboardOptions, *, for_dashboard_server: bool = False) -> list[str]:
    args = ["--execute-safe"] if options.execute_safe else []
    if options.mock_desktoplut:
        args.append("--mock-desktoplut")
    if options.allow_hardware:
        args.append("--allow-hardware")
    if options.allow_live_desktoplut:
        args.append("--allow-live-desktoplut")
    if options.allow_builds:
        args.append("--allow-builds")
    if options.simulate_execution:
        args.append("--simulate")
    return args


def _port_value(options: DashboardOptions) -> str:
    return str(options.port) if options.port is not None else "PORT"


def _command(parts: list[str | Path | None]) -> str:
    return " ".join(str(part) for part in parts if part is not None)


def _probe_match_rehearsal_args(probe_request: dict[str, Any] | None) -> list[str]:
    if not isinstance(probe_request, dict) or probe_request.get("enabled") is not True:
        return ["--probe-match", "--probe-match-display-tech", "DISPLAY_TECH"]
    args = [
        "--probe-match",
        "--probe-match-kind",
        str(probe_request.get("kind", "ccmx")),
        "--probe-match-display-tech",
        str(probe_request.get("display_tech", "u")),
    ]
    if probe_request.get("high_res") is True:
        args.append("--probe-match-high-res")
    return args


def suggested_commands(
    run: Path,
    status: dict[str, Any],
    options: DashboardOptions,
    probe_request: dict[str, Any] | None = None,
) -> dict[str, str]:
    port = _port_value(options)
    flags = _flag_args(options)
    next_action = status.get("next_action") if isinstance(status.get("next_action"), dict) else {}
    action = next_action.get("action")
    commands = {
        "refresh_handoff": _command(["dlc", "handoff", "--run", run, "--port", port, *flags]),
        "tool_preflight": _command(["dlc", "preflight", "--run", run]),
        "next": _command(["dlc", "next", "--run", run, "--port", port]),
        "readiness": _command(["dlc", "readiness", "--run", run, "--port", port, *flags]),
        "readout": _command(["dlc", "readout", "--run", run, "--port", port, *flags]),
        "dashboard_server": _command(["dlc", "dashboard-server", "--run", run, "--meter-port", port, *_flag_args(options, for_dashboard_server=True)]),
        "supervise": _command(["dlc", "supervise", "--run", run, "--port", port, "--max-steps", "10", *flags, "--update-dashboard"]),
        "run_unattended": _command(["dlc", "run-unattended", "--run", run, "--port", port, *flags, "--update-dashboard"]),
        "quality_policy_mhc": _command(["dlc", "quality-policy", "--run", run, "--phase", "mhc", "--avg-threshold", "1.0", "--p95-threshold", "2.5", "--max-threshold", "4.0", "--white-threshold", "1.5", "--max-iterations", "4"]),
        "quality_policy_3dlut": _command(["dlc", "quality-policy", "--run", run, "--phase", "3dlut", "--avg-threshold", "0.8", "--p95-threshold", "2.0", "--max-threshold", "4.0", "--white-threshold", "1.2", "--max-iterations", "4"]),
        "loop_status": _command(["dlc", "loop-status", "--run", run]),
        "pipeline_evidence": _command(["dlc", "pipeline-evidence", "--run", run]),
        "self_test": _command(["dlc", "self-test", "--port", port]),
        "self_test_probe_match": _command(["dlc", "self-test", "--port", port, *_probe_match_rehearsal_args(probe_request)]),
        "windows_local_audit": _command(["dlc", "windows-local-audit", "--run", run, "--monitor-hint", "MONITOR_HINT"]),
        "ack_self_test_gate_override": _command(["dlc", "ack", "--run", run, "--action", "self_test_gate_override", "--note", "\"Operator accepts bypassing recent self-test gate for this live run\""]),
        "ack_windows_local_audit_gate_override": _command(["dlc", "ack", "--run", run, "--action", "windows_local_audit_gate_override", "--note", "\"Operator accepts bypassing local Windows ICC/gamma audit gate for this live run\""]),
    }
    if action == "ack_spectro_placed":
        commands["ack_spectro_placed"] = str(next_action.get("command") or _command(["dlc", "ack", "--run", run, "--action", "spectro_placed", "--instrument", "\"ColorChecker Studio\""]))
    if action == "ack_colorimeter_placed":
        commands["ack_colorimeter_placed"] = str(next_action.get("command") or _command(["dlc", "ack", "--run", run, "--action", "colorimeter_placed", "--instrument", "\"i1 Display Pro\""]))
    operator_handoff = status.get("operator_handoff") if isinstance(status.get("operator_handoff"), dict) else {}
    handoff_command = operator_handoff.get("next_operator_command")
    if isinstance(handoff_command, str) and handoff_command:
        handoff_action = operator_handoff.get("next_operator_action")
        if handoff_action == "spectro_placed":
            commands["ack_spectro_placed"] = handoff_command
        elif handoff_action == "colorimeter_placed":
            commands["ack_colorimeter_placed"] = handoff_command
        commands["operator_next"] = handoff_command
    run_command = operator_handoff.get("run_command")
    if isinstance(run_command, str) and run_command:
        commands["operator_run"] = run_command
    if isinstance(action, str) and next_action.get("status") == "ready" and action != "complete":
        commands["run_stage"] = _command(["dlc", "run-stage", "--run", run, "--port", port, action, *flags, "--update-dashboard"])
    return commands


def _handoff_ok(*, health: dict[str, Any], next_action: dict[str, Any], supervisor_gate: dict[str, Any], operator_handoff: dict[str, Any]) -> bool:
    actionable = (
        next_action.get("action") == "complete"
        or next_action.get("status") == "human_required"
        or supervisor_gate.get("open") is True
    )
    if not actionable:
        return False
    if health.get("ok") is True:
        return True
    if int(health.get("error_event_count", 0) or 0) > 0:
        return False
    return operator_handoff.get("status") in {"waiting_for_operator", "ready_to_run"}


def write_agent_handoff(
    ctx: RunContext,
    *,
    options: DashboardOptions | None = None,
    output: Path | None = None,
) -> AgentHandoff:
    options = options or DashboardOptions()
    status = dashboard_status_payload(ctx, options)
    health = status.get("health") if isinstance(status.get("health"), dict) else {}
    supervisor_gate = status.get("supervisor_gate") if isinstance(status.get("supervisor_gate"), dict) else {}
    artifact_records = scan_artifacts(ctx.root)
    target = output or ctx.root / "reports" / "agent_handoff.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    latest_self_test = _read_json(ctx.root / "reports" / "self_test.json")
    safety = status.get("safety") if isinstance(status.get("safety"), dict) else {}
    self_test_gate = safety.get("self_test") if isinstance(safety.get("self_test"), dict) else {}
    latest_unattended = _read_json(ctx.root / "reports" / "unattended.json")
    latest_tool_preflight = _read_json(ctx.root / "preflight" / "tool_preflight.json")
    next_action = status.get("next_action") if isinstance(status.get("next_action"), dict) else {}
    operator_handoff = status.get("operator_handoff") if isinstance(status.get("operator_handoff"), dict) else {}
    ok = _handoff_ok(health=health, next_action=next_action, supervisor_gate=supervisor_gate, operator_handoff=operator_handoff)
    result = AgentHandoff(
        ok=ok,
        run=str(ctx.root),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        status=status,
        latest_self_test=latest_self_test,
        self_test_gate=self_test_gate,
        latest_unattended=latest_unattended,
        latest_tool_preflight=latest_tool_preflight,
        operator_handoff=operator_handoff,
        artifact_count=len(artifact_records),
        suggested_commands=suggested_commands(
            ctx.root,
            status,
            options,
            ctx.manifest.desktoplut.get("probe_match_request") if isinstance(ctx.manifest.desktoplut.get("probe_match_request"), dict) else None,
        ),
        artifact=str(target),
    )
    target.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    current = open_run(ctx.root)
    current.manifest.stages.append(
        {
            "stage": "agent_handoff",
            "status": "written" if ok else "attention",
            "artifact": str(target),
            "next_action": status.get("next_action", {}).get("action") if isinstance(status.get("next_action"), dict) else None,
            "artifact_count": len(artifact_records),
        }
    )
    current.save()
    current.log(f"Agent handoff written: {target}")
    EventWriter(current.events_path).write(
        "INFO" if ok else "WARNING",
        "agent_handoff",
        "agent_handoff_written",
        ok=ok,
        artifact=str(target),
    )
    return result

