"""One-command first-demo run preparation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .dashboard import DashboardOptions, write_dashboard_html, write_readout_html
from .demo import build_demo_readiness
from .handoff import write_agent_handoff
from .live_setup import write_live_setup
from .runs import RunContext, create_run, open_run
from .windows_local import write_windows_local_audit


def _open_or_create_run(*, run: Path | None, mode: str, display: str | None) -> tuple[RunContext, bool]:
    if run is not None and (run / "manifest.json").exists():
        return open_run(run), False
    return create_run(mode, display, run), True


def _demo_readiness_path(ctx: RunContext, probe_match: bool) -> Path:
    suffix = "_probe_match" if probe_match else ""
    return ctx.root / "reports" / f"demo_readiness{suffix}.json"


def _latest_demo_readiness_path(ctx: RunContext) -> Path | None:
    report_dir = ctx.root / "reports"
    if not report_dir.exists():
        return None
    records = sorted(report_dir.glob("demo_readiness*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return records[0] if records else None


def _command(parts: list[str | Path | int | None]) -> str:
    return " ".join(str(part) for part in parts if part is not None)


def _mission_control_payload(
    *,
    ctx: RunContext,
    meter_port: int | None,
    host: str,
    port: int,
    live_desktoplut: bool,
    allow_builds: bool,
    dashboard: Path,
    readout: Path,
) -> dict[str, Any]:
    command_parts: list[str | Path | int | None] = [
        "python",
        "-m",
        "dlc.cli",
        "dashboard-server",
        "--run",
        ctx.root,
        "--host",
        host,
        "--port",
        port,
        "--execute-safe",
        "--allow-hardware",
        "--meter-port" if meter_port is not None else None,
        meter_port,
        "--allow-live-desktoplut" if live_desktoplut else "--mock-desktoplut",
        "--allow-builds" if allow_builds else None,
    ]
    base_url = f"http://{host}:{port}"
    return {
        "dashboard_server_command": _command(command_parts),
        "dashboard_url": f"{base_url}/",
        "readout_url": f"{base_url}/readout",
        "status_url": f"{base_url}/status.json",
        "dashboard_file": str(dashboard),
        "readout_file": str(readout),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _rewrite_run_arg(command: str, run: Path) -> str:
    pattern = re.compile(r"--run\s+(\"[^\"]+\"|'[^']+'|\S+)")
    return pattern.sub(lambda _match: f"--run {run}", command)


def _rewrite_packet_run_args(value: Any, run: Path) -> Any:
    if isinstance(value, str):
        return _rewrite_run_arg(value, run) if "--run" in value else value
    if isinstance(value, list):
        return [_rewrite_packet_run_args(item, run) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_packet_run_args(item, run) for key, item in value.items()}
    return value


def launch_state_from_handoff(handoff: Any) -> dict[str, Any]:
    operator = handoff.operator_handoff if isinstance(getattr(handoff, "operator_handoff", None), dict) else {}
    status = operator.get("status")
    next_action = operator.get("next_operator_action")
    run_command = operator.get("run_command")
    ready = status == "ready_to_run" and isinstance(run_command, str) and bool(run_command)
    return {
        "ready": ready,
        "status": status,
        "pending_operator_action": next_action,
        "blocked_by": next_action if next_action else (None if ready else status),
        "command": run_command if ready else None,
        "run_command": run_command,
    }


def refresh_first_demo_packets(
    *,
    ctx: RunContext,
    handoff: Any,
    dashboard: Path | None = None,
    readout: Path | None = None,
) -> dict[str, str]:
    """Persist refreshed handoff/demo state after a compact operator transition."""

    updated: dict[str, str] = {}
    demo_gate = handoff.status.get("demo_gate") if isinstance(handoff.status.get("demo_gate"), dict) else None
    if isinstance(demo_gate, dict):
        demo_path = _latest_demo_readiness_path(ctx)
        if demo_path is not None:
            demo_gate = dict(demo_gate)
            demo_gate["artifact"] = str(demo_path)
            _write_json(demo_path, demo_gate)
            updated["demo_readiness"] = str(demo_path)

    prep_path = ctx.root / "reports" / "first_demo_prepare.json"
    prep = _read_json(prep_path)
    if prep is not None:
        prep["ok"] = bool(demo_gate.get("ok")) if isinstance(demo_gate, dict) else prep.get("ok")
        if isinstance(demo_gate, dict):
            prep["target"] = demo_gate.get("target", prep.get("target"))
            prep["demo_readiness"] = demo_gate
        prep["handoff"] = handoff.as_dict()
        prep["operator_handoff"] = handoff.operator_handoff
        prep["operator_next"] = handoff.suggested_commands.get("operator_next")
        prep["operator_run"] = handoff.suggested_commands.get("operator_run")
        launch = launch_state_from_handoff(handoff)
        prep["launch"] = launch
        prep["launch_ready"] = launch["ready"]
        prep["launch_command"] = launch["command"]
        if dashboard is not None:
            prep["dashboard"] = str(dashboard)
            if isinstance(prep.get("mission_control"), dict):
                prep["mission_control"]["dashboard_file"] = str(dashboard)
        if readout is not None:
            prep["readout"] = str(readout)
            if isinstance(prep.get("mission_control"), dict):
                prep["mission_control"]["readout_file"] = str(readout)
        prep = _rewrite_packet_run_args(prep, ctx.root)
        _write_json(prep_path, prep)
        updated["first_demo_prepare"] = str(prep_path)
    return updated


def prepare_first_demo(
    *,
    run: Path | None = None,
    mode: str = "SDR",
    display: str | None = "DISPLAY_MODEL",
    meter_port: int | None = None,
    monitor_hint: str | None = None,
    probe_match: bool = False,
    probe_match_kind: str = "ccmx",
    probe_match_display_tech: str = "u",
    probe_match_high_res: bool = False,
    probe_match_display_index: int = 1,
    probe_match_patch_window: str = "0.5,0.5,1.0",
    adaptive_drift: bool = False,
    adaptive_drift_stages: list[str] | None = None,
    default_quality_policy: bool = True,
    windows_local_audit: bool = False,
    live_desktoplut: bool = False,
    refresh_seconds: int = 5,
    allow_builds: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8765,
) -> dict[str, Any]:
    """Create the run-local artifacts an agent needs before the first live demo."""

    ctx, created = _open_or_create_run(run=run, mode=mode, display=display)
    live_setup = write_live_setup(
        ctx=ctx,
        meter_port=meter_port,
        monitor_hint=monitor_hint,
        probe_match=probe_match,
        probe_match_kind=probe_match_kind,
        probe_match_display_tech=probe_match_display_tech,
        probe_match_high_res=probe_match_high_res,
        probe_match_display_index=probe_match_display_index,
        probe_match_patch_window=probe_match_patch_window,
        adaptive_drift=adaptive_drift,
        adaptive_drift_stages=adaptive_drift_stages,
        default_quality_policy=default_quality_policy,
    )

    audit_payload = None
    if windows_local_audit:
        audit_payload = write_windows_local_audit(ctx=open_run(ctx.root), monitor_hint=monitor_hint).as_dict()

    readiness_path = _demo_readiness_path(ctx, probe_match)
    readiness = build_demo_readiness(
        run=ctx.root,
        port=meter_port,
        monitor_hint=monitor_hint,
        probe_match=probe_match,
        mock_desktoplut=not live_desktoplut,
    )
    readiness["artifact"] = str(readiness_path)
    _write_json(readiness_path, readiness)

    options = DashboardOptions(
        port=meter_port,
        refresh_seconds=refresh_seconds,
        execute_safe=True,
        allow_hardware=True,
        allow_live_desktoplut=live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=not live_desktoplut,
        simulate_execution=False,
    )
    dashboard_args = {
        "port": options.port,
        "refresh_seconds": options.refresh_seconds,
        "execute_safe": options.execute_safe,
        "allow_hardware": options.allow_hardware,
        "allow_live_desktoplut": options.allow_live_desktoplut,
        "allow_builds": options.allow_builds,
        "mock_desktoplut": options.mock_desktoplut,
        "simulate_execution": options.simulate_execution,
    }
    dashboard = write_dashboard_html(open_run(ctx.root), **dashboard_args)
    readout = write_readout_html(open_run(ctx.root), **dashboard_args)
    handoff = write_agent_handoff(open_run(ctx.root), options=options)
    mission_control = _mission_control_payload(
        ctx=ctx,
        meter_port=meter_port,
        host=dashboard_host,
        port=dashboard_port,
        live_desktoplut=live_desktoplut,
        allow_builds=allow_builds,
        dashboard=dashboard,
        readout=readout,
    )

    artifact = ctx.root / "reports" / "first_demo_prepare.json"
    launch = launch_state_from_handoff(handoff)
    payload = {
        "ok": bool(readiness.get("ok")),
        "run": str(ctx.root),
        "created_run": created,
        "target": readiness.get("target"),
        "live_setup": live_setup.as_dict(),
        "windows_local_audit": audit_payload,
        "demo_readiness": readiness,
        "mission_control": mission_control,
        "dashboard_server_command": mission_control["dashboard_server_command"],
        "dashboard_url": mission_control["dashboard_url"],
        "readout_url": mission_control["readout_url"],
        "status_url": mission_control["status_url"],
        "dashboard": str(dashboard),
        "readout": str(readout),
        "handoff": handoff.as_dict(),
        "operator_handoff": handoff.operator_handoff,
        "operator_next": handoff.suggested_commands.get("operator_next"),
        "operator_run": handoff.suggested_commands.get("operator_run"),
        "launch": launch,
        "launch_ready": launch["ready"],
        "launch_command": launch["command"],
        "artifact": str(artifact),
    }
    _write_json(artifact, payload)
    return payload
