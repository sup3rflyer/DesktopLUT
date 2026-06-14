"""Run-specific unattended readiness audit."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agent import recommend_next_action
from .calibration_mode import calibration_mode_evidence
from .decisions import quality_policy_coverage
from .events import EventWriter
from .human_actions import has_human_action
from .live_setup import live_setup_config, live_setup_meter_port, live_setup_monitor_hint, resolve_live_meter_port
from .profiles import default_dummy_icc
from .runs import RunContext
from .safety import blocked_reason_for_action
from .tools import REQUIRED_TOOL_NAMES, ToolSet


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    severity: str
    detail: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReadinessResult:
    ok: bool
    ready_to_continue: bool
    run: str
    mode: str
    checks: list[ReadinessCheck]
    next_action: dict[str, Any]
    supervisor: dict[str, Any]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checks"] = [check.as_dict() for check in self.checks]
        return payload


def _stage_entries(ctx: RunContext, stage: str) -> list[dict[str, Any]]:
    return [entry for entry in ctx.manifest.stages if entry.get("stage") == stage]


def _has_passed_stage(ctx: RunContext, stage: str) -> bool:
    return any(entry.get("status") in {"passed", "captured", "finalized"} for entry in _stage_entries(ctx, stage))


def _windows_local_audit_passed(ctx: RunContext, label: str) -> dict[str, Any] | None:
    audits = ctx.manifest.desktoplut.get("windows_local_audits")
    if not isinstance(audits, dict):
        return None
    audit = audits.get(label)
    return audit if isinstance(audit, dict) and audit.get("ok") is True else None


def _probe_match_request(ctx: RunContext) -> dict[str, Any] | None:
    request = ctx.manifest.desktoplut.get("probe_match_request")
    if isinstance(request, dict) and request.get("enabled") is True:
        return request
    return None


def _probe_match_completed(ctx: RunContext) -> bool:
    if isinstance(ctx.manifest.desktoplut.get("probe_match_correction"), str):
        return True
    return any(entry.get("stage") == "probe_match" and entry.get("status") == "completed" for entry in _stage_entries(ctx, "probe_match"))


def _check(name: str, ok: bool, severity: str, detail: str, **evidence: Any) -> ReadinessCheck:
    return ReadinessCheck(name=name, ok=ok, severity=severity, detail=detail, evidence=evidence)


def _resolve_artifact(ctx: RunContext, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return ctx.root / path


def _tool_preflight_snapshot(ctx: RunContext) -> dict[str, Any]:
    path = None
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") == "tool_preflight" and isinstance(entry.get("artifact"), str):
            path = _resolve_artifact(ctx, str(entry["artifact"]))
            break
    path = path or ctx.root / "preflight" / "tool_preflight.json"
    payload = None
    error = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else None
            if payload is None:
                error = "tool preflight artifact is not a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            error = str(exc)
    else:
        error = "tool preflight artifact is missing"

    missing_required_records: list[str] = []
    missing_fingerprints: list[str] = []
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if isinstance(tools, dict):
        for name in REQUIRED_TOOL_NAMES:
            record = tools.get(name)
            if not isinstance(record, dict) or record.get("ok") is not True:
                missing_required_records.append(name)
            elif not isinstance(record.get("sha256"), str) or len(str(record.get("sha256"))) != 64:
                missing_fingerprints.append(name)
    elif payload is not None:
        missing_required_records = list(REQUIRED_TOOL_NAMES)

    required_ready = payload.get("required_ready") is True if isinstance(payload, dict) else False
    contained_ready = payload.get("contained_ready") is True if isinstance(payload, dict) else False
    contained_paths_ready = payload.get("contained_paths_ready") is True if isinstance(payload, dict) else False
    vendor_manifest_ready = payload.get("vendor_manifest_ready") is True if isinstance(payload, dict) else False
    ready = (
        payload is not None
        and required_ready
        and contained_ready
        and contained_paths_ready
        and vendor_manifest_ready
        and not missing_required_records
        and not missing_fingerprints
    )
    return {
        "ready": ready,
        "recorded": payload is not None,
        "artifact": str(path),
        "error": error,
        "required_ready": required_ready,
        "contained_ready": contained_ready,
        "contained_paths_ready": contained_paths_ready,
        "contained_path_root": payload.get("contained_path_root") if isinstance(payload, dict) else None,
        "contained_path_issues": payload.get("contained_path_issues", []) if isinstance(payload, dict) else [],
        "vendor_manifest_ready": vendor_manifest_ready,
        "missing_required_records": missing_required_records,
        "missing_tool_fingerprints": missing_fingerprints,
    }


def write_readiness_audit(
    *,
    ctx: RunContext,
    tools: ToolSet,
    port: int | None = None,
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
    output: Path | None = None,
) -> ReadinessResult:
    from .selftest import latest_self_test_status

    requested_port = port
    port = resolve_live_meter_port(ctx, port)
    setup = live_setup_config(ctx)
    setup_meter_port = live_setup_meter_port(ctx)
    setup_monitor_hint = live_setup_monitor_hint(ctx)
    next_action = recommend_next_action(ctx, port=port)
    blocked_reason = blocked_reason_for_action(
        next_action,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
    )
    dummy = default_dummy_icc(ctx.manifest.mode)
    missing_required = tools.missing_required()
    missing_contained = tools.missing_contained()
    quality_policy = quality_policy_coverage(ctx.manifest.desktoplut.get("quality_policy"))
    contract_passed = _has_passed_stage(ctx, "desktoplut_contract_check")
    calibration = calibration_mode_evidence(ctx)
    calibration_can_be_established = next_action.action in {"desktoplut_contract_check", "enter_calibration_mode"}
    live_side_effects = (allow_hardware or allow_live_desktoplut or allow_builds) and not simulate_execution
    windows_local_audit = _windows_local_audit_passed(ctx, windows_local_audit_label)
    windows_audit_hint = windows_local_audit.get("monitor_hint") if windows_local_audit else None
    windows_audit_matches_setup = (
        not live_side_effects
        or setup_monitor_hint is None
        or (windows_local_audit is not None and windows_audit_hint == setup_monitor_hint)
    )
    windows_local_audit_required = live_side_effects and not skip_windows_local_audit_gate
    tool_preflight = _tool_preflight_snapshot(ctx)
    tool_preflight_required = live_side_effects
    probe_request = _probe_match_request(ctx)
    probe_match_pending = probe_request is not None and not _probe_match_completed(ctx)
    spectro_required = bool(probe_match_pending)
    self_test_requires_probe_match = probe_request is not None
    self_test_status = latest_self_test_status(
        max_age_hours=self_test_max_age_hours,
        require_probe_match=self_test_requires_probe_match,
    )
    self_test_required = live_side_effects and not skip_self_test_gate
    self_test_override_required = live_side_effects and skip_self_test_gate
    self_test_override_acknowledged = has_human_action(ctx, "self_test_gate_override")
    windows_override_required = live_side_effects and skip_windows_local_audit_gate
    windows_override_acknowledged = has_human_action(ctx, "windows_local_audit_gate_override")

    checks = [
        _check(
            "required_tools_available",
            not missing_required,
            "blocker",
            "Required Argyll/DLC tools are present.",
            missing=missing_required,
        ),
        _check(
            "contained_tools_available",
            not missing_contained,
            "blocker",
            "All third-party tools resolve inside the DLC directory.",
            missing_or_fallback=missing_contained,
        ),
        _check(
            "dummy_icc_available",
            dummy.ok,
            "blocker",
            "The mode-specific dummy ICC for DesktopLUT calibration mode exists.",
            role=dummy.role,
            path=str(dummy.path),
            note=dummy.note,
        ),
        _check(
            "tool_preflight_snapshot",
            (not tool_preflight_required) or bool(tool_preflight["ready"]),
            "blocker" if tool_preflight_required else "info",
            "A run-local tool preflight snapshot with fingerprints and copied-vendor manifest is required before live side-effect execution."
            if tool_preflight_required
            else "Run-local tool preflight provenance is not required for this dry/mock/simulated gate.",
            required=tool_preflight_required,
            **tool_preflight,
        ),
        _check(
            "quality_policy",
            bool(quality_policy["ok"]),
            "blocker",
            "Run-level acceptance thresholds cover both MHC and 3D LUT loop decisions.",
            coverage=quality_policy,
        ),
        _check(
            "live_setup_meter_port",
            setup_meter_port is None or port == setup_meter_port,
            "blocker",
            "The effective Argyll meter port matches the live setup manifest.",
            configured=setup_meter_port,
            requested=requested_port,
            effective=port,
            setup_present=bool(setup),
        ),
        _check(
            "spectro_placed",
            (not spectro_required) or has_human_action(ctx, "spectro_placed"),
            "blocker" if spectro_required else "info",
            "The spectrometer placement has been acknowledged for the requested probe-match branch."
            if spectro_required
            else "Spectrometer placement is not required because probe matching is not pending.",
            required=spectro_required,
            acknowledged=has_human_action(ctx, "spectro_placed"),
            probe_match_request=probe_request,
            probe_match_pending=probe_match_pending,
        ),
        _check(
            "colorimeter_placed",
            has_human_action(ctx, "colorimeter_placed"),
            "blocker",
            "The center-screen colorimeter placement has been acknowledged.",
            acknowledged=has_human_action(ctx, "colorimeter_placed"),
        ),
        _check(
            "desktoplut_contract_passed",
            contract_passed or mock_desktoplut,
            "blocker" if not mock_desktoplut else "warning",
            "DesktopLUT API contract has passed for this run, or mock DesktopLUT is explicitly selected.",
            passed=contract_passed,
            mock_desktoplut=mock_desktoplut,
        ),
        _check(
            "calibration_mode_active",
            bool(calibration["ok"]) or calibration_can_be_established,
            "blocker",
            "DesktopLUT calibration mode has dummy-ICC/reset evidence, or the next action will establish/reset it before measurement.",
            calibration=calibration,
            can_be_established_by_next_action=calibration_can_be_established,
            next_action=next_action.action,
        ),
        _check(
            "next_action_ready",
            next_action.status == "ready",
            "blocker",
            "The next recommendation is executable by the supervisor.",
            status=next_action.status,
            action=next_action.action,
        ),
        _check(
            "supervisor_gate_open",
            blocked_reason is None,
            "blocker",
            "The supplied supervisor safety flags permit the next recommendation.",
            blocked_reason=blocked_reason,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        ),
        _check(
            "recent_self_test",
            (not self_test_required) or bool(self_test_status.get("ok")),
            "blocker" if self_test_required else "info",
            "A recent passing probe-match self-test is required before live side-effect execution for this run."
            if self_test_required and self_test_requires_probe_match
            else "A recent passing self-test is required before live side-effect execution."
            if self_test_required
            else "Self-test freshness is not required for this dry/mock/simulated gate.",
            required=self_test_required,
            require_probe_match=self_test_requires_probe_match,
            skipped=skip_self_test_gate,
            live_side_effects=live_side_effects,
            max_age_hours=self_test_max_age_hours,
            status=self_test_status,
        ),
        _check(
            "self_test_gate_override",
            (not self_test_override_required) or self_test_override_acknowledged,
            "blocker" if self_test_override_required else "info",
            "The operator explicitly acknowledged bypassing the recent self-test gate for live side-effect execution."
            if self_test_override_required
            else "No self-test gate override is active.",
            required=self_test_override_required,
            acknowledged=self_test_override_acknowledged,
            action="self_test_gate_override",
            live_side_effects=live_side_effects,
        ),
        _check(
            "windows_local_audit",
            (not windows_local_audit_required) or (windows_local_audit is not None and windows_audit_matches_setup),
            "blocker" if windows_local_audit_required else "info",
            "A passing local Windows ICC/gamma audit is required before live side-effect execution."
            if windows_local_audit_required
            else "Local Windows ICC/gamma audit is not required for this dry/mock/simulated gate.",
            required=windows_local_audit_required,
            skipped=skip_windows_local_audit_gate,
            label=windows_local_audit_label,
            found=windows_local_audit is not None,
            artifact=windows_local_audit.get("artifact") if windows_local_audit else None,
            setup_monitor_hint=setup_monitor_hint,
            audit_monitor_hint=windows_audit_hint,
            matches_setup=windows_audit_matches_setup,
        ),
        _check(
            "windows_local_audit_gate_override",
            (not windows_override_required) or windows_override_acknowledged,
            "blocker" if windows_override_required else "info",
            "The operator explicitly acknowledged bypassing the local Windows ICC/gamma audit gate for live side-effect execution."
            if windows_override_required
            else "No Windows local-audit gate override is active.",
            required=windows_override_required,
            acknowledged=windows_override_acknowledged,
            action="windows_local_audit_gate_override",
            live_side_effects=live_side_effects,
        ),
    ]

    ok = all(check.ok for check in checks if check.severity == "blocker")
    ready_to_continue = ok and blocked_reason is None
    report_dir = ctx.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact = output or report_dir / "readiness.json"
    result = ReadinessResult(
        ok=ok,
        ready_to_continue=ready_to_continue,
        run=str(ctx.root),
        mode=ctx.manifest.mode,
        checks=checks,
        next_action=next_action.as_dict(),
        supervisor={
            "blocked_reason": blocked_reason,
            "execute_safe": execute_safe,
            "allow_hardware": allow_hardware,
            "allow_live_desktoplut": allow_live_desktoplut,
            "allow_builds": allow_builds,
            "mock_desktoplut": mock_desktoplut,
            "simulate_execution": simulate_execution,
            "skip_self_test_gate": skip_self_test_gate,
            "self_test_max_age_hours": self_test_max_age_hours,
            "skip_windows_local_audit_gate": skip_windows_local_audit_gate,
            "windows_local_audit_label": windows_local_audit_label,
            "port": port,
        },
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    ctx.manifest.stages.append(
        {
            "stage": "readiness",
            "status": "ready" if ready_to_continue else "blocked",
            "artifact": str(artifact),
            "next_action": next_action.action,
            "blocked_reason": blocked_reason,
        }
    )
    ctx.save()
    ctx.log(f"Readiness audit: {'ready' if ready_to_continue else 'blocked'}")
    EventWriter(ctx.events_path).write(
        "INFO" if ready_to_continue else "WARNING",
        "readiness",
        "readiness_audit_written",
        ok=ok,
        ready_to_continue=ready_to_continue,
        next_action=next_action.action,
        blocked_reason=blocked_reason,
        artifact=str(artifact),
    )
    return result

