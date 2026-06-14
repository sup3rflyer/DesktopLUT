"""One-command first-demo run preparation."""

from __future__ import annotations

import json
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
    readiness_path.parent.mkdir(parents=True, exist_ok=True)
    readiness_path.write_text(json.dumps(readiness, indent=2), encoding="utf-8")

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

    artifact = ctx.root / "reports" / "first_demo_prepare.json"
    payload = {
        "ok": bool(readiness.get("ok")),
        "run": str(ctx.root),
        "created_run": created,
        "target": readiness.get("target"),
        "live_setup": live_setup.as_dict(),
        "windows_local_audit": audit_payload,
        "demo_readiness": readiness,
        "dashboard": str(dashboard),
        "readout": str(readout),
        "handoff": handoff.as_dict(),
        "operator_handoff": handoff.operator_handoff,
        "operator_next": handoff.suggested_commands.get("operator_next"),
        "operator_run": handoff.suggested_commands.get("operator_run"),
        "artifact": str(artifact),
    }
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
