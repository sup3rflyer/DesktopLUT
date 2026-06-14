"""Live-run setup manifest for agent-supervised calibration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .decisions import MetricThresholds, write_quality_policy
from .events import EventWriter
from .runs import RunContext

LIVE_SETUP_MHC_THRESHOLDS = MetricThresholds(avg_de2000=1.0, p95_de2000=2.5, max_de2000=4.0, white_de2000=1.5, max_iterations=4)
LIVE_SETUP_3DLUT_THRESHOLDS = MetricThresholds(avg_de2000=0.8, p95_de2000=2.0, max_de2000=4.0, white_de2000=1.2, max_iterations=4)


@dataclass(frozen=True)
class LiveSetupPlan:
    ok: bool
    run: str
    generated_at: str
    mode: str
    display: str | None
    meter_port: int | None
    monitor_hint: str | None
    probe_match: dict[str, Any]
    adaptive_drift: dict[str, Any]
    quality_policy: dict[str, Any] | None
    human_actions: list[dict[str, str]]
    suggested_commands: dict[str, str]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def live_setup_config(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("live_setup")
    return payload if isinstance(payload, dict) else {}


def live_setup_meter_port(ctx: RunContext) -> int | None:
    value = live_setup_config(ctx).get("meter_port")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def live_setup_monitor_hint(ctx: RunContext) -> str | None:
    value = live_setup_config(ctx).get("monitor_hint")
    return value if isinstance(value, str) and value else None


def resolve_live_meter_port(ctx: RunContext, explicit: int | None) -> int | None:
    return explicit if explicit is not None else live_setup_meter_port(ctx)


def resolve_live_monitor_hint(ctx: RunContext, explicit: str | None) -> str | None:
    return explicit if explicit else live_setup_monitor_hint(ctx)


def _command(parts: list[str | Path | int | None]) -> str:
    return " ".join(str(part) for part in parts if part is not None)


def _probe_match_request(
    *,
    enabled: bool,
    kind: str,
    display_tech: str,
    high_res: bool,
    display_index: int,
    patch_window: str,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "kind": kind,
        "display_tech": display_tech,
        "display_index": display_index,
        "patch_window": patch_window,
        "high_res": high_res,
    }


def _human_actions(probe_match_enabled: bool) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if probe_match_enabled:
        actions.append(
            {
                "action": "spectro_placed",
                "instrument": "ColorChecker Studio",
                "position": "patch area",
                "reason": "spectrometer reference for optional probe matching",
            }
        )
    actions.append(
        {
            "action": "colorimeter_placed",
            "instrument": "i1 Display Pro",
            "position": "center",
            "reason": "unattended Argyll dispread measurement",
        }
    )
    return actions


def _suggested_commands(
    *,
    run: Path,
    meter_port: int | None,
    monitor_hint: str | None,
    probe_match_enabled: bool,
    probe_match_kind: str,
    probe_match_display_tech: str,
    probe_match_high_res: bool,
    adaptive_drift_enabled: bool,
) -> dict[str, str]:
    port = str(meter_port) if meter_port is not None else "PORT"
    commands = {
        "preflight": _command(["dlc", "preflight", "--run", run]),
        "self_test": _command(["dlc", "self-test", "--port", port]),
        "quality_policy_mhc": _command(
            [
                "dlc",
                "quality-policy",
                "--run",
                run,
                "--phase",
                "mhc",
                "--avg-threshold",
                "1.0",
                "--p95-threshold",
                "2.5",
                "--max-threshold",
                "4.0",
                "--white-threshold",
                "1.5",
                "--max-iterations",
                "4",
            ]
        ),
        "quality_policy_3dlut": _command(
            [
                "dlc",
                "quality-policy",
                "--run",
                run,
                "--phase",
                "3dlut",
                "--avg-threshold",
                "0.8",
                "--p95-threshold",
                "2.0",
                "--max-threshold",
                "4.0",
                "--white-threshold",
                "1.2",
                "--max-iterations",
                "4",
            ]
        ),
        "windows_local_audit": _command(["dlc", "windows-local-audit", "--run", run, "--monitor-hint", monitor_hint or "MONITOR_HINT"]),
        "ack_self_test_gate_override": _command(
            [
                "dlc",
                "ack",
                "--run",
                run,
                "--action",
                "self_test_gate_override",
                "--note",
                "\"Operator accepts bypassing recent self-test gate for this live run\"",
            ]
        ),
        "ack_windows_local_audit_gate_override": _command(
            [
                "dlc",
                "ack",
                "--run",
                run,
                "--action",
                "windows_local_audit_gate_override",
                "--note",
                "\"Operator accepts bypassing local Windows ICC/gamma audit gate for this live run\"",
            ]
        ),
        "readiness_mock": _command(["dlc", "readiness", "--run", run, "--port", port, "--execute-safe", "--mock-desktoplut", "--simulate"]),
        "handoff_mock": _command(["dlc", "handoff", "--run", run, "--port", port, "--execute-safe", "--mock-desktoplut", "--simulate"]),
        "dashboard_server": _command(["dlc", "dashboard-server", "--run", run, "--meter-port", port, "--execute-safe", "--mock-desktoplut", "--simulate"]),
        "readout": _command(["dlc", "readout", "--run", run, "--port", port, "--execute-safe", "--mock-desktoplut", "--simulate"]),
        "run_unattended_rehearsal": _command(["dlc", "run-unattended", "--run", run, "--port", port, "--execute-safe", "--mock-desktoplut", "--simulate", "--update-dashboard"]),
        "run_unattended_live": _command(
            [
                "dlc",
                "run-unattended",
                "--run",
                run,
                "--port",
                port,
                "--execute-safe",
                "--allow-hardware",
                "--allow-live-desktoplut",
                "--allow-builds",
                "--windows-monitor-hint",
                monitor_hint or "MONITOR_HINT",
                "--update-dashboard",
            ]
        ),
    }
    if probe_match_enabled:
        probe_args = ["--probe-match", "--probe-match-kind", probe_match_kind, "--probe-match-display-tech", probe_match_display_tech]
        if probe_match_high_res:
            probe_args.append("--probe-match-high-res")
        commands["self_test_probe_match"] = _command(["dlc", "self-test", "--port", port, *probe_args])
        commands["ack_spectro_placed"] = _command(["dlc", "ack", "--run", run, "--action", "spectro_placed", "--instrument", '"ColorChecker Studio"'])
    if adaptive_drift_enabled:
        commands["adaptive_drift_status"] = _command(["dlc", "next", "--run", run, "--port", port])
    commands["ack_colorimeter_placed"] = _command(["dlc", "ack", "--run", run, "--action", "colorimeter_placed", "--instrument", '"i1 Display Pro"'])
    return commands


def write_live_setup(
    *,
    ctx: RunContext,
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
    output: Path | None = None,
) -> LiveSetupPlan:
    probe_request = _probe_match_request(
        enabled=probe_match,
        kind=probe_match_kind,
        display_tech=probe_match_display_tech,
        high_res=probe_match_high_res,
        display_index=probe_match_display_index,
        patch_window=probe_match_patch_window,
    )
    setup = {
        "meter_port": meter_port,
        "monitor_hint": monitor_hint,
        "probe_match": probe_request,
        "human_actions": _human_actions(probe_match),
    }
    drift_request = {
        "enabled": adaptive_drift,
        "stages": adaptive_drift_stages or ["mhc-verification", "post-mhc", "3dlut-verification"],
        "delta_threshold": 0.003,
        "bias": 4,
        "max_repeats": 3,
        "settle_required": 2,
    }
    if adaptive_drift:
        setup["adaptive_drift"] = drift_request
    ctx.manifest.desktoplut["live_setup"] = setup
    if adaptive_drift:
        ctx.manifest.desktoplut["adaptive_drift"] = drift_request
    existing_probe_request = ctx.manifest.desktoplut.get("probe_match_request")
    if probe_match:
        ctx.manifest.desktoplut["probe_match_request"] = probe_request
    elif isinstance(existing_probe_request, dict) and existing_probe_request.get("enabled") is True:
        ctx.manifest.desktoplut["probe_match_request"] = {"enabled": False}
    quality_policy = None
    if default_quality_policy:
        quality_policy = write_quality_policy(ctx=ctx, phase="mhc", thresholds=LIVE_SETUP_MHC_THRESHOLDS)
        quality_policy = write_quality_policy(ctx=ctx, phase="3dlut", thresholds=LIVE_SETUP_3DLUT_THRESHOLDS)

    target = output or ctx.root / "reports" / "live_setup.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    result = LiveSetupPlan(
        ok=True,
        run=str(ctx.root),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        mode=ctx.manifest.mode,
        display=ctx.manifest.display,
        meter_port=meter_port,
        monitor_hint=monitor_hint,
        probe_match=probe_request,
        adaptive_drift=drift_request,
        quality_policy=quality_policy,
        human_actions=setup["human_actions"],
        suggested_commands=_suggested_commands(
            run=ctx.root,
            meter_port=meter_port,
            monitor_hint=monitor_hint,
            probe_match_enabled=probe_match,
            probe_match_kind=probe_match_kind,
            probe_match_display_tech=probe_match_display_tech,
            probe_match_high_res=probe_match_high_res,
            adaptive_drift_enabled=adaptive_drift,
        ),
        artifact=str(target),
    )
    target.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "live_setup",
            "status": "written",
            "artifact": str(target),
            "meter_port": meter_port,
            "monitor_hint": monitor_hint,
            "probe_match": probe_match,
            "adaptive_drift": adaptive_drift,
            "default_quality_policy": default_quality_policy,
        }
    )
    ctx.save()
    ctx.log(f"Live setup written: {target}")
    EventWriter(ctx.events_path).write(
        "INFO",
        "live_setup",
        "live_setup_written",
        artifact=str(target),
        meter_port=meter_port,
        monitor_hint=monitor_hint,
        probe_match=probe_match,
        adaptive_drift=adaptive_drift,
    )
    return result

