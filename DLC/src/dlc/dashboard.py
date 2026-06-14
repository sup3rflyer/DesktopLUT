"""Second-monitor status dashboard for unattended runs."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent import recommend_next_action
from .events import read_events
from .monitor import evaluate_run_health
from .runs import RunContext, open_run
from .safety import blocked_reason_for_action


@dataclass(frozen=True)
class DashboardOptions:
    port: int | None = None
    refresh_seconds: int = 5
    execute_safe: bool = False
    allow_hardware: bool = False
    allow_live_desktoplut: bool = False
    allow_builds: bool = False
    mock_desktoplut: bool = False
    simulate_execution: bool = False
    self_test_max_age_hours: float = 24.0
    windows_local_audit_label: str = "preflight"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_artifact(ctx: RunContext, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return ctx.root / path


def _latest_stage(ctx: RunContext) -> dict[str, Any]:
    return ctx.manifest.stages[-1] if ctx.manifest.stages else {}


def _latest_readiness(ctx: RunContext) -> dict[str, Any] | None:
    return _read_json(ctx.root / "reports" / "readiness.json")


def _latest_metrics(ctx: RunContext) -> dict[str, Any] | None:
    for entry in reversed(ctx.manifest.stages):
        if not str(entry.get("stage", "")).endswith("_metrics"):
            continue
        metrics = entry.get("metrics")
        if isinstance(metrics, str):
            payload = _read_json(Path(metrics))
            if payload:
                return payload
    return None


def _latest_supervision(ctx: RunContext) -> dict[str, Any] | None:
    report_dir = ctx.root / "reports"
    if not report_dir.exists():
        return None
    records = sorted(report_dir.glob("supervise_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in records:
        payload = _read_json(path)
        if payload:
            payload.setdefault("artifact", str(path))
            return payload
    return None


def _latest_unattended(ctx: RunContext) -> dict[str, Any] | None:
    return _read_json(ctx.root / "reports" / "unattended.json")


def _completion_evidence(ctx: RunContext) -> dict[str, Any]:
    latest_unattended = _latest_unattended(ctx)
    evidence = latest_unattended.get("completion_evidence") if isinstance(latest_unattended, dict) else None
    if not isinstance(evidence, dict):
        return {
            "ok": False,
            "recorded": False,
            "reason": "completion evidence has not been written",
            "payload": None,
        }
    failures = evidence.get("finalization_current_audit_failures")
    manifest_finalized = evidence.get("manifest_finalized") is True
    final_audit_ok = evidence.get("final_audit_ok") is True
    finalization_ok = evidence.get("finalization_ok") is True
    finalization_current_audit_ok = evidence.get("finalization_current_audit_ok") is True
    finalization_links_audit = evidence.get("finalization_links_audit") is True
    final_report_exists = evidence.get("final_report_exists") is True
    ok = all(
        [
            manifest_finalized,
            final_audit_ok,
            finalization_ok,
            finalization_current_audit_ok,
            finalization_links_audit,
            final_report_exists,
        ]
    )
    if ok:
        reason = "finalized with linked audit, current-audit revalidation, and accepted report"
    elif not finalization_current_audit_ok:
        reason = "finalization current-audit revalidation is missing or failed"
    elif not finalization_links_audit:
        reason = "finalization does not link to the passing audit"
    elif not final_report_exists:
        reason = "accepted final report is missing"
    else:
        reason = "completion evidence is incomplete"
    return {
        "ok": ok,
        "recorded": True,
        "manifest_finalized": manifest_finalized,
        "final_audit_ok": final_audit_ok,
        "finalization_ok": finalization_ok,
        "finalization_current_audit_ok": finalization_current_audit_ok,
        "finalization_current_audit_failures": failures if isinstance(failures, list) else None,
        "finalization_links_audit": finalization_links_audit,
        "final_report_exists": final_report_exists,
        "artifact": latest_unattended.get("artifact") if isinstance(latest_unattended, dict) else None,
        "reason": reason,
        "payload": evidence,
    }


def _latest_windows_local_audit(ctx: RunContext, label: str) -> dict[str, Any] | None:
    audits = ctx.manifest.desktoplut.get("windows_local_audits")
    if isinstance(audits, dict):
        audit = audits.get(label)
        if isinstance(audit, dict):
            return audit
    return _read_json(ctx.root / "preflight" / f"windows_local_audit_{label}.json")


def _tool_preflight(ctx: RunContext) -> dict[str, Any]:
    path = None
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") == "tool_preflight" and isinstance(entry.get("artifact"), str):
            path = _resolve_artifact(ctx, str(entry["artifact"]))
            break
    path = path or ctx.root / "preflight" / "tool_preflight.json"
    payload = _read_json(path) or {}
    missing_required = payload.get("missing_required")
    missing_contained = payload.get("missing_contained")
    vendor_ready = payload.get("vendor_manifest_ready") is True
    contained_paths_ready = payload.get("contained_paths_ready") is True
    contained_path_issues = payload.get("contained_path_issues")
    tools = payload.get("tools")
    missing_fingerprints = []
    if isinstance(tools, dict):
        missing_fingerprints = [
            name
            for name, record in tools.items()
            if isinstance(record, dict)
            and record.get("ok") is True
            and (not isinstance(record.get("sha256"), str) or len(str(record.get("sha256"))) != 64)
        ]
    ok = (
        bool(payload)
        and payload.get("required_ready") is True
        and payload.get("contained_ready") is True
        and contained_paths_ready
        and not missing_fingerprints
    )
    if not payload:
        reason = "tool preflight has not been written"
    elif ok:
        reason = "run-local tool preflight passed"
    elif not contained_paths_ready:
        reason = "contained tool paths need attention"
    else:
        reason = "tool preflight needs attention"
    return {
        "ok": ok,
        "recorded": bool(payload),
        "required_ready": payload.get("required_ready") if payload else None,
        "contained_ready": payload.get("contained_ready") if payload else None,
        "contained_paths_ready": contained_paths_ready if payload else None,
        "contained_path_root": payload.get("contained_path_root") if payload else None,
        "contained_path_issues": contained_path_issues if isinstance(contained_path_issues, list) else [],
        "vendor_manifest_ready": vendor_ready if payload else None,
        "missing_required": missing_required if isinstance(missing_required, list) else [],
        "missing_contained": missing_contained if isinstance(missing_contained, list) else [],
        "missing_tool_fingerprints": missing_fingerprints,
        "artifact": str(path),
        "reason": reason,
        "payload": payload or None,
    }


def _pipeline_evidence(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("pipeline_evidence")
    if isinstance(payload, dict):
        evidence = dict(payload)
    else:
        evidence = _read_json(ctx.root / "reports" / "pipeline_evidence.json") or {}
    refs = evidence.get("colourspace_stage_references")
    ref_count = len(refs) if isinstance(refs, list) else 0
    missing_fingerprints = evidence.get("missing_tool_fingerprints")
    fingerprint_missing_count = len(missing_fingerprints) if isinstance(missing_fingerprints, list) else None
    ok = bool(evidence.get("ok"))
    if not evidence:
        reason = "pipeline evidence has not been written"
    elif ok:
        reason = "contained DLC/Argyll pipeline evidence passed"
    else:
        reason = "pipeline evidence needs attention"
    return {
        "ok": ok,
        "recorded": bool(evidence),
        "primary_pipeline": evidence.get("primary_pipeline"),
        "contained_tools_ready": evidence.get("contained_tools_ready"),
        "contained_paths_ready": evidence.get("contained_paths_ready"),
        "contained_path_issues": evidence.get("contained_path_issues") if isinstance(evidence.get("contained_path_issues"), list) else [],
        "tool_fingerprints_ready": fingerprint_missing_count == 0 if fingerprint_missing_count is not None else None,
        "missing_tool_fingerprints": missing_fingerprints if isinstance(missing_fingerprints, list) else None,
        "colourspace_required": evidence.get("colourspace_required"),
        "colourspace_stage_reference_count": ref_count,
        "artifact": evidence.get("artifact"),
        "reason": reason,
        "payload": evidence or None,
    }


def _loop_status(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("loop_status")
    if isinstance(payload, dict):
        evidence = dict(payload)
    else:
        evidence = _read_json(ctx.root / "reports" / "loop_status.json") or {}
    phases = evidence.get("phases") if isinstance(evidence.get("phases"), dict) else {}
    mhc = phases.get("mhc") if isinstance(phases.get("mhc"), dict) else {}
    lut3d = phases.get("3dlut") if isinstance(phases.get("3dlut"), dict) else {}
    ok = bool(evidence.get("ok"))
    mhc_status = str(mhc.get("status", "pending"))
    lut3d_status = str(lut3d.get("status", "pending"))
    if not evidence:
        reason = "loop status has not been written"
    elif ok:
        reason = "MHC and 3D LUT loops are stopped"
    else:
        reason = f"MHC {mhc_status}, 3D LUT {lut3d_status}"
    return {
        "ok": ok,
        "recorded": bool(evidence),
        "mhc_status": mhc_status,
        "mhc_iteration": mhc.get("latest_iteration"),
        "mhc_reason": mhc.get("reason"),
        "lut3d_status": lut3d_status,
        "lut3d_iteration": lut3d.get("latest_iteration"),
        "lut3d_reason": lut3d.get("reason"),
        "artifact": evidence.get("artifact"),
        "reason": reason,
        "payload": evidence or None,
    }


def _quality_policy(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("quality_policy")
    policy = payload if isinstance(payload, dict) else {}
    default = policy.get("default") if isinstance(policy.get("default"), dict) else {}
    mhc = policy.get("mhc") if isinstance(policy.get("mhc"), dict) else {}
    lut3d = policy.get("3dlut") if isinstance(policy.get("3dlut"), dict) else {}
    return {
        "recorded": bool(policy),
        "default": default,
        "mhc": mhc,
        "3dlut": lut3d,
        "mhc_summary": _policy_summary(mhc or default),
        "3dlut_summary": _policy_summary(lut3d or default),
        "payload": policy or None,
    }


def _policy_summary(policy: dict[str, Any]) -> str:
    if not policy:
        return "defaults"
    parts = []
    for label, key in [("avg", "avg_de2000"), ("p95", "p95_de2000"), ("max", "max_de2000"), ("white", "white_de2000"), ("iters", "max_iterations")]:
        if key in policy:
            parts.append(f"{label}={policy[key]}")
    return ", ".join(parts) if parts else "custom"


def _safety_snapshot(ctx: RunContext, options: DashboardOptions) -> dict[str, Any]:
    from .selftest import latest_self_test_status

    probe_request = ctx.manifest.desktoplut.get("probe_match_request")
    require_probe_match = isinstance(probe_request, dict) and probe_request.get("enabled") is True
    self_test = latest_self_test_status(
        max_age_hours=options.self_test_max_age_hours,
        require_probe_match=require_probe_match,
    )
    windows_audit = _latest_windows_local_audit(ctx, options.windows_local_audit_label)
    windows_ok = bool(windows_audit and windows_audit.get("ok") is True)
    return {
        "self_test": self_test,
        "self_test_require_probe_match": require_probe_match,
        "windows_local_audit": {
            "ok": windows_ok,
            "label": options.windows_local_audit_label,
            "artifact": windows_audit.get("artifact") if windows_audit else None,
            "finding_count": len(windows_audit.get("findings", [])) if isinstance(windows_audit, dict) and isinstance(windows_audit.get("findings"), list) else 0,
            "reason": "passing local Windows audit is recorded" if windows_ok else "no passing local Windows audit is recorded",
            "payload": windows_audit,
        },
    }


def _event_summary(ctx: RunContext) -> dict[str, Any]:
    last_step = None
    last_command = None
    last_command_started = None
    active_command = None
    first_command_event_seen = False
    for event in reversed(read_events(ctx.events_path)):
        if last_step is None and event.event in {"supervise_step", "run_stage_recommended"}:
            recommendation = event.data.get("recommendation")
            last_step = {
                "time": event.time,
                "stage": event.stage,
                "index": event.data.get("index"),
                "action": recommendation.get("action") if isinstance(recommendation, dict) else None,
                "executable": event.data.get("executable"),
                "blocked_reason": event.data.get("blocked_reason"),
            }
        if event.event in {"supervise_command_started", "run_stage_command_started"}:
            argv = event.data.get("argv")
            command = {
                "time": event.time,
                "stage": event.stage,
                "index": event.data.get("index"),
                "action": event.data.get("action"),
                "argv": argv if isinstance(argv, list) else [],
                "running": False,
            }
            if last_command_started is None:
                last_command_started = command
            if not first_command_event_seen:
                active_command = dict(command) | {"running": True}
                first_command_event_seen = True
        if last_command is None and event.event in {"supervise_command_finished", "run_stage_command_finished"}:
            argv = event.data.get("argv")
            returncode = event.data.get("returncode")
            last_command = {
                "time": event.time,
                "stage": event.stage,
                "argv": argv if isinstance(argv, list) else [],
                "returncode": returncode,
                "ok": returncode == 0,
            }
            if not first_command_event_seen:
                first_command_event_seen = True
        if last_step is not None and last_command is not None and last_command_started is not None:
            break
    return {
        "last_step": last_step,
        "last_command": last_command,
        "last_command_started": last_command_started,
        "active_command": active_command,
    }


def _stage_complete(ctx: RunContext, stage: str, statuses: set[str]) -> bool:
    for entry in ctx.manifest.stages:
        if entry.get("stage") == stage and str(entry.get("status", "")).lower() in statuses:
            return True
    return False


def _workflow_progress(ctx: RunContext, next_action: dict[str, Any]) -> dict[str, Any]:
    milestones = [
        ("desktoplut_contract_check", "DesktopLUT API", {"passed", "completed"}),
        ("snapshot_desktoplut", "Calibration mode", {"completed", "applied", "entered"}),
        ("raw-mhc", "Raw MHC profile", {"completed"}),
        ("build_mhc_baseline", "MHC candidate", {"built", "completed"}),
        ("apply_mhc_baseline", "MHC applied", {"applied", "completed"}),
        ("mhc_decision", "MHC accepted", {"stop", "completed"}),
        ("post-mhc", "Post-MHC profile", {"completed"}),
        ("build_3dlut", "3D LUT built", {"completed"}),
        ("apply_3dlut", "3D LUT applied", {"applied", "completed"}),
        ("3dlut_lut_integrity", "3D LUT integrity", {"passed", "completed"}),
        ("3dlut_decision", "3D LUT accepted", {"stop", "completed"}),
        ("pipeline_evidence", "Toolchain evidence", {"passed", "completed"}),
        ("final_report", "Report written", {"written", "completed"}),
        ("final_audit", "Final audit", {"passed", "completed"}),
        ("finalization", "Finalized", {"finalized", "completed"}),
    ]
    items = [
        {"stage": stage, "label": label, "done": _stage_complete(ctx, stage, statuses)}
        for stage, label, statuses in milestones
    ]
    completed = sum(1 for item in items if item["done"])
    remaining = next((item for item in items if not item["done"]), None)
    return {
        "completed": completed,
        "total": len(items),
        "percent": round((completed / len(items)) * 100, 1) if items else 0.0,
        "current": str(next_action.get("stage") or next_action.get("action") or "unknown"),
        "next_milestone": remaining,
        "items": items,
    }


def _operator_snapshot(ctx: RunContext, next_action: dict[str, Any], safety: dict[str, Any]) -> dict[str, Any]:
    latest_supervision = _latest_supervision(ctx)
    latest_unattended = _latest_unattended(ctx)
    event_summary = _event_summary(ctx)
    supervision_summary = None
    if latest_supervision:
        steps = latest_supervision.get("steps", [])
        last_step = steps[-1] if isinstance(steps, list) and steps else None
        supervision_summary = {
            "artifact": latest_supervision.get("artifact"),
            "ok": latest_supervision.get("ok"),
            "steps": len(steps) if isinstance(steps, list) else 0,
            "stopped_reason": latest_supervision.get("stopped_reason"),
            "last_action": (
                last_step.get("recommendation", {}).get("action")
                if isinstance(last_step, dict) and isinstance(last_step.get("recommendation"), dict)
                else None
            ),
        }
    unattended_summary = None
    if latest_unattended:
        completion = latest_unattended.get("completion_evidence")
        unattended_summary = {
            "artifact": latest_unattended.get("artifact"),
            "ok": latest_unattended.get("ok"),
            "complete": latest_unattended.get("complete"),
            "supervised": latest_unattended.get("supervised"),
            "stopped_reason": latest_unattended.get("stopped_reason"),
            "dashboard": latest_unattended.get("dashboard"),
            "completion_evidence": completion if isinstance(completion, dict) else None,
        }
    completion_summary = _completion_evidence(ctx)
    return {
        "progress": _workflow_progress(ctx, next_action),
        "last_step": event_summary["last_step"],
        "last_command": event_summary["last_command"],
        "last_command_started": event_summary["last_command_started"],
        "active_command": event_summary["active_command"],
        "latest_supervision": supervision_summary,
        "unattended": unattended_summary,
        "completion": completion_summary,
        "safety": safety,
        "tool_preflight": _tool_preflight(ctx),
        "pipeline_evidence": _pipeline_evidence(ctx),
        "loop_status": _loop_status(ctx),
    }


def _status_class(next_action: dict[str, Any], readiness: dict[str, Any] | None) -> str:
    if next_action.get("action") == "complete":
        return "done"
    if next_action.get("status") == "human_required":
        return "human"
    if readiness and readiness.get("ready_to_continue") is False:
        return "blocked"
    return "running"


def _event_rows(ctx: RunContext, count: int = 8) -> str:
    rows = []
    for event in read_events(ctx.events_path)[-count:]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(event.time)}</td>"
            f"<td>{html.escape(event.level)}</td>"
            f"<td>{html.escape(event.stage)}</td>"
            f"<td>{html.escape(event.event)}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="4">No events yet</td></tr>'


def _recent_events(ctx: RunContext, count: int = 8) -> list[dict[str, Any]]:
    return [
        {
            "time": event.time,
            "level": event.level,
            "stage": event.stage,
            "event": event.event,
            "data": event.data,
        }
        for event in read_events(ctx.events_path)[-count:]
    ]


def _check_items(readiness: dict[str, Any] | None) -> str:
    if not readiness:
        return '<li class="muted">No readiness audit yet</li>'
    items = []
    for check in readiness.get("checks", []):
        if not isinstance(check, dict):
            continue
        ok = bool(check.get("ok"))
        name = html.escape(str(check.get("name", "check")))
        detail = html.escape(str(check.get("detail", "")))
        items.append(f'<li class="{"ok" if ok else "fail"}"><span>{name}</span><small>{detail}</small></li>')
    return "\n".join(items)


def _gate_class(blocked_reason: str | None) -> str:
    return "ok" if blocked_reason is None else "fail"


def _gate_text(blocked_reason: str | None) -> str:
    return "Open" if blocked_reason is None else blocked_reason


def _short_command(command: dict[str, Any] | None) -> str:
    if not command:
        return "No command recorded"
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv:
        return "No command recorded"
    return " ".join(str(part) for part in argv[:4]) + (" ..." if len(argv) > 4 else "")


def _operator_html(operator: dict[str, Any]) -> str:
    progress = operator.get("progress") if isinstance(operator.get("progress"), dict) else {}
    last_step = operator.get("last_step") if isinstance(operator.get("last_step"), dict) else None
    last_command = operator.get("last_command") if isinstance(operator.get("last_command"), dict) else None
    last_command_started = operator.get("last_command_started") if isinstance(operator.get("last_command_started"), dict) else None
    active_command = operator.get("active_command") if isinstance(operator.get("active_command"), dict) else None
    supervision = operator.get("latest_supervision") if isinstance(operator.get("latest_supervision"), dict) else None
    unattended = operator.get("unattended") if isinstance(operator.get("unattended"), dict) else None
    safety = operator.get("safety") if isinstance(operator.get("safety"), dict) else {}
    tool_preflight = operator.get("tool_preflight") if isinstance(operator.get("tool_preflight"), dict) else {}
    pipeline = operator.get("pipeline_evidence") if isinstance(operator.get("pipeline_evidence"), dict) else {}
    loop_status = operator.get("loop_status") if isinstance(operator.get("loop_status"), dict) else {}
    completion = operator.get("completion") if isinstance(operator.get("completion"), dict) else {}
    self_test = safety.get("self_test") if isinstance(safety.get("self_test"), dict) else {}
    windows_audit = safety.get("windows_local_audit") if isinstance(safety.get("windows_local_audit"), dict) else {}
    next_milestone = progress.get("next_milestone") if isinstance(progress.get("next_milestone"), dict) else None
    last_action = last_step.get("action") if last_step else "none"
    last_block = last_step.get("blocked_reason") if last_step else None
    command_text = _short_command(last_command)
    command_ok = "ok" if last_command and last_command.get("ok") else ("fail" if last_command else "muted")
    started_text = _short_command(active_command or last_command_started)
    started_class = "ok" if active_command else ("muted" if not last_command_started else "")
    supervision_text = "No supervision record yet"
    if supervision:
        supervision_text = f"{supervision.get('steps', 0)} step(s), stopped: {supervision.get('stopped_reason', 'unknown')}"
    unattended_text = "No unattended record yet"
    if unattended:
        unattended_text = f"ok={str(unattended.get('ok')).lower()} stopped: {unattended.get('stopped_reason', 'unknown')}"
    self_test_ok = bool(self_test.get("ok"))
    windows_ok = bool(windows_audit.get("ok"))
    self_test_text = "fresh" if self_test_ok else str(self_test.get("reason", "missing"))
    windows_text = "passed" if windows_ok else str(windows_audit.get("reason", "missing"))
    tool_preflight_ok = bool(tool_preflight.get("ok"))
    tool_preflight_text = "ready" if tool_preflight_ok else str(tool_preflight.get("reason", "missing"))
    if tool_preflight_ok and tool_preflight.get("vendor_manifest_ready") is not True:
        tool_preflight_text = "ready; vendor manifest missing"
    if tool_preflight.get("contained_paths_ready") is False:
        issues = tool_preflight.get("contained_path_issues")
        issue_count = len(issues) if isinstance(issues, list) else 0
        tool_preflight_text = f"contained paths failed ({issue_count})"
    pipeline_ok = bool(pipeline.get("ok"))
    pipeline_text = "DLC/Argyll" if pipeline_ok else str(pipeline.get("reason", "missing"))
    loops_ok = bool(loop_status.get("ok"))
    loops_text = "stopped" if loops_ok else str(loop_status.get("reason", "missing"))
    completion_ok = bool(completion.get("ok"))
    completion_text = "accepted" if completion_ok else str(completion.get("reason", "missing"))
    return f"""
      <div class="kv"><span>Workflow</span><strong>{html.escape(str(progress.get("completed", 0)))}/{html.escape(str(progress.get("total", 0)))} ({html.escape(str(progress.get("percent", 0)))}%)</strong></div>
      <div class="kv"><span>Next Milestone</span><strong>{html.escape(str((next_milestone or {}).get("label", "complete")))}</strong></div>
      <div class="kv"><span>Self-Test Gate</span><strong class="{"ok" if self_test_ok else "fail"}">{html.escape(self_test_text)}</strong></div>
      <div class="kv"><span>Windows Gate</span><strong class="{"ok" if windows_ok else "fail"}">{html.escape(windows_text)}</strong></div>
      <div class="kv"><span>Tool Preflight</span><strong class="{"ok" if tool_preflight_ok else "fail"}">{html.escape(tool_preflight_text)}</strong></div>
      <div class="kv"><span>Toolchain Gate</span><strong class="{"ok" if pipeline_ok else "fail"}">{html.escape(pipeline_text)}</strong></div>
      <div class="kv"><span>Loop Gate</span><strong class="{"ok" if loops_ok else "fail"}">{html.escape(loops_text)}</strong></div>
      <div class="kv"><span>Completion Proof</span><strong class="{"ok" if completion_ok else "fail"}">{html.escape(completion_text)}</strong></div>
      <div class="kv"><span>Last Step</span><strong>{html.escape(str(last_action))}</strong></div>
      <div class="kv"><span>Step Gate</span><strong class="{_gate_class(str(last_block) if last_block else None)}">{html.escape(str(last_block or "open"))}</strong></div>
      <div class="kv"><span>Command Started</span><strong class="{started_class}">{html.escape(started_text)}</strong></div>
      <div class="kv"><span>Last Command</span><strong class="{command_ok}">{html.escape(command_text)}</strong></div>
      <div class="kv"><span>Supervisor</span><strong>{html.escape(supervision_text)}</strong></div>
      <div class="kv"><span>Unattended</span><strong>{html.escape(unattended_text)}</strong></div>
    """


def _status_label(status_class: str) -> str:
    return {
        "done": "Complete",
        "human": "Human Needed",
        "blocked": "Blocked",
        "running": "Ready",
    }[status_class]


def _readout_snapshot(
    ctx: RunContext,
    *,
    next_action: dict[str, Any],
    operator: dict[str, Any],
    health: dict[str, Any],
    latest_metrics: dict[str, Any] | None,
    status_class: str,
    blocked_reason: str | None,
    refresh_seconds: int,
) -> dict[str, Any]:
    progress = operator.get("progress") if isinstance(operator.get("progress"), dict) else {}
    safety = operator.get("safety") if isinstance(operator.get("safety"), dict) else {}
    self_test = safety.get("self_test") if isinstance(safety.get("self_test"), dict) else {}
    windows_audit = safety.get("windows_local_audit") if isinstance(safety.get("windows_local_audit"), dict) else {}
    tool_preflight = operator.get("tool_preflight") if isinstance(operator.get("tool_preflight"), dict) else {}
    pipeline = operator.get("pipeline_evidence") if isinstance(operator.get("pipeline_evidence"), dict) else {}
    loop_status = operator.get("loop_status") if isinstance(operator.get("loop_status"), dict) else {}
    completion = operator.get("completion") if isinstance(operator.get("completion"), dict) else {}
    last_command = operator.get("last_command") if isinstance(operator.get("last_command"), dict) else None
    active_command = operator.get("active_command") if isinstance(operator.get("active_command"), dict) else None
    metric_value = "n/a"
    metric_label = "Latest dE00 avg"
    if latest_metrics:
        metric_value = str(latest_metrics.get("avg_de2000", "n/a"))
        metric_label = f"{latest_metrics.get('phase', 'metrics')} iter {latest_metrics.get('iteration', '?')} avg dE00"
    return {
        "title": "DesktopLUT Calibrator",
        "run": ctx.manifest.name,
        "mode": ctx.manifest.mode,
        "display": ctx.manifest.display,
        "status": _status_label(status_class),
        "status_class": status_class,
        "next_action": next_action.get("action"),
        "stage": next_action.get("stage"),
        "reason": next_action.get("reason"),
        "progress_percent": progress.get("percent", 0.0),
        "progress_completed": progress.get("completed", 0),
        "progress_total": progress.get("total", 0),
        "metric_label": metric_label,
        "metric_value": metric_value,
        "supervisor_gate": "open" if blocked_reason is None else blocked_reason,
        "supervisor_gate_open": blocked_reason is None,
        "health": health.get("status", "unknown"),
        "health_ok": bool(health.get("ok")),
        "self_test_ok": bool(self_test.get("ok")),
        "self_test": "ready" if self_test.get("ok") else str(self_test.get("reason", "missing")),
        "self_test_require_probe_match": bool(self_test.get("require_probe_match")),
        "windows_audit_ok": bool(windows_audit.get("ok")),
        "tool_preflight_ok": bool(tool_preflight.get("ok")),
        "tool_preflight": (
            "ready"
            if tool_preflight.get("ok") and tool_preflight.get("vendor_manifest_ready") is True
            else "ready; vendor manifest missing"
            if tool_preflight.get("ok")
            else f"contained paths failed ({len(tool_preflight.get('contained_path_issues'))})"
            if tool_preflight.get("contained_paths_ready") is False and isinstance(tool_preflight.get("contained_path_issues"), list)
            else str(tool_preflight.get("reason", "missing"))
        ),
        "pipeline_evidence_ok": bool(pipeline.get("ok")),
        "pipeline_evidence": "DLC/Argyll" if pipeline.get("ok") else str(pipeline.get("reason", "missing")),
        "loop_status_ok": bool(loop_status.get("ok")),
        "loop_status": "stopped" if loop_status.get("ok") else str(loop_status.get("reason", "missing")),
        "completion_ok": bool(completion.get("ok")),
        "completion": "accepted" if completion.get("ok") else str(completion.get("reason", "missing")),
        "completion_current_audit_ok": bool(completion.get("finalization_current_audit_ok")),
        "last_command_ok": bool(last_command and last_command.get("ok")),
        "last_command": _short_command(last_command),
        "active_command": _short_command(active_command) if active_command else "",
        "active_command_running": bool(active_command),
        "refresh_seconds": int(refresh_seconds),
    }


def dashboard_status_payload(ctx: RunContext, options: DashboardOptions | None = None) -> dict[str, Any]:
    options = options or DashboardOptions()
    action = recommend_next_action(ctx, port=options.port)
    blocked_reason = blocked_reason_for_action(
        action,
        execute_safe=options.execute_safe,
        allow_hardware=options.allow_hardware,
        allow_live_desktoplut=options.allow_live_desktoplut,
        allow_builds=options.allow_builds,
        mock_desktoplut=options.mock_desktoplut,
        simulate_execution=options.simulate_execution,
    )
    readiness = _latest_readiness(ctx)
    latest_metrics = _latest_metrics(ctx)
    next_action = action.as_dict()
    safety = _safety_snapshot(ctx, options)
    health = evaluate_run_health(ctx).as_dict()
    operator = _operator_snapshot(ctx, next_action, safety)
    tool_preflight = operator.get("tool_preflight") if isinstance(operator.get("tool_preflight"), dict) else {}
    pipeline_evidence = operator.get("pipeline_evidence") if isinstance(operator.get("pipeline_evidence"), dict) else {}
    loop_status = operator.get("loop_status") if isinstance(operator.get("loop_status"), dict) else {}
    completion = operator.get("completion") if isinstance(operator.get("completion"), dict) else {}
    quality_policy = _quality_policy(ctx)
    status_class = _status_class(next_action, readiness)
    readout = _readout_snapshot(
        ctx,
        next_action=next_action,
        operator=operator,
        health=health,
        latest_metrics=latest_metrics,
        status_class=status_class,
        blocked_reason=blocked_reason,
        refresh_seconds=options.refresh_seconds,
    )
    return {
        "run": str(ctx.root),
        "name": ctx.manifest.name,
        "mode": ctx.manifest.mode,
        "display": ctx.manifest.display,
        "manifest_status": ctx.manifest.status,
        "status_class": status_class,
        "latest_stage": _latest_stage(ctx),
        "latest_metrics": latest_metrics,
        "next_action": next_action,
        "readiness": readiness,
        "health": health,
        "operator": operator,
        "completion": completion,
        "readout": readout,
        "tool_preflight": tool_preflight,
        "pipeline_evidence": pipeline_evidence,
        "loop_status": loop_status,
        "quality_policy": quality_policy,
        "safety": safety,
        "supervisor_gate": {
            "open": blocked_reason is None,
            "blocked_reason": blocked_reason,
            "execute_safe": options.execute_safe,
            "allow_hardware": options.allow_hardware,
            "allow_live_desktoplut": options.allow_live_desktoplut,
            "allow_builds": options.allow_builds,
            "mock_desktoplut": options.mock_desktoplut,
            "simulate_execution": options.simulate_execution,
            "port": options.port,
        },
        "recent_events": _recent_events(ctx),
    }


def render_dashboard_html(
    ctx: RunContext,
    *,
    port: int | None = None,
    refresh_seconds: int = 5,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
) -> str:
    options = DashboardOptions(
        port=port,
        refresh_seconds=refresh_seconds,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
    )
    status = dashboard_status_payload(ctx, options)
    next_action = status["next_action"]
    blocked_reason = status["supervisor_gate"]["blocked_reason"]
    readiness = _latest_readiness(ctx)
    latest_stage = status["latest_stage"]
    latest_metrics = status["latest_metrics"]
    health = status["health"]
    operator = status["operator"]
    status_class = _status_class(next_action, readiness)
    status_label = _status_label(status_class)
    metric_value = "n/a"
    metric_label = "Latest dE00 avg"
    if latest_metrics:
        metric_value = str(latest_metrics.get("avg_de2000", "n/a"))
        metric_label = f"{latest_metrics.get('phase', 'metrics')} iter {latest_metrics.get('iteration', '?')} avg dE00"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DesktopLUT Calibrator Status</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #111411;
      --panel: #1b211d;
      --panel2: #222923;
      --text: #edf4ed;
      --muted: #aab6aa;
      --accent: #72d38a;
      --warn: #f0c45c;
      --bad: #f07b6e;
      --line: #344035;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Arial, sans-serif;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(360px, 1fr) 1.4fr;
      gap: 18px;
      padding: 22px;
    }}
    .hero {{
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: end;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 34px; font-weight: 700; }}
    h2 {{ font-size: 18px; margin-bottom: 12px; color: var(--muted); font-weight: 600; }}
    .status {{
      min-width: 220px;
      text-align: center;
      padding: 16px 20px;
      border: 1px solid var(--line);
      background: var(--panel2);
    }}
    .status strong {{ display: block; font-size: 28px; }}
    .status.running strong, .ok span, .big.ok {{ color: var(--accent); }}
    .status.human strong {{ color: var(--warn); }}
    .status.blocked strong, .status.done strong, .fail span, .big.fail {{ color: var(--bad); }}
    .big.warn {{ color: var(--warn); }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 18px;
      min-height: 160px;
    }}
    .big {{
      font-size: 30px;
      line-height: 1.15;
      margin-bottom: 10px;
    }}
    .muted, small {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    .metric {{ font-size: 42px; font-weight: 700; }}
    ul {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
    li {{ display: grid; gap: 2px; padding: 9px 0; border-bottom: 1px solid var(--line); }}
    li span {{ font-weight: 650; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); color: var(--muted); }}
    td:nth-child(2), td:nth-child(3) {{ color: var(--text); }}
    .kv {{
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
      align-items: baseline;
    }}
    .kv span {{ color: var(--muted); }}
    .kv strong {{ font-weight: 650; overflow-wrap: anywhere; }}
    @media (max-width: 900px) {{
      main, .hero, .grid {{ display: block; }}
      .panel, .status {{ margin-top: 14px; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div>
      <h1>DesktopLUT Calibrator</h1>
      <p class="muted">{html.escape(ctx.manifest.name)} - {html.escape(ctx.manifest.mode)} - {html.escape(str(ctx.manifest.display or "display"))}</p>
    </div>
    <div class="status {status_class}">
      <small>Run State</small>
      <strong>{status_label}</strong>
    </div>
  </section>
  <section class="panel">
    <h2>Next Action</h2>
    <p class="big">{html.escape(str(next_action.get("action", "unknown")))}</p>
    <p class="muted">{html.escape(str(next_action.get("reason", "")))}</p>
  </section>
  <section class="grid">
    <div class="panel">
      <h2>Latest Stage</h2>
      <p class="big">{html.escape(str(latest_stage.get("stage", "none")))}</p>
      <p class="muted">{html.escape(str(latest_stage.get("status", "not started")))}</p>
    </div>
    <div class="panel">
      <h2>{html.escape(metric_label)}</h2>
      <p class="metric">{html.escape(metric_value)}</p>
      <p class="muted">Refreshes every {int(refresh_seconds)} seconds</p>
    </div>
  </section>
  <section class="panel">
    <h2>Supervisor Gate</h2>
    <p class="big {_gate_class(blocked_reason)}">{html.escape(_gate_text(blocked_reason))}</p>
    <p class="muted">execute_safe={str(execute_safe).lower()} hardware={str(allow_hardware).lower()} desktoplut={str(allow_live_desktoplut or mock_desktoplut).lower()} builds={str(allow_builds).lower()} simulated={str(simulate_execution).lower()}</p>
  </section>
  <section class="panel">
    <h2>Operator Console</h2>
    {_operator_html(operator)}
  </section>
  <section class="panel">
    <h2>Run Health</h2>
    <p class="big {"ok" if health.get("ok") else "warn"}">{html.escape(str(health.get("status", "unknown")))}</p>
    <p class="muted">{html.escape("; ".join(str(reason) for reason in health.get("reasons", [])) or "No monitor warnings")}</p>
  </section>
  <section class="panel">
    <h2>Readiness</h2>
    <ul>{_check_items(readiness)}</ul>
  </section>
  <section class="panel">
    <h2>Recent Events</h2>
    <table><tbody>{_event_rows(ctx)}</tbody></table>
  </section>
</main>
</body>
</html>
"""


def render_readout_html(
    ctx: RunContext,
    *,
    port: int | None = None,
    refresh_seconds: int = 5,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
) -> str:
    options = DashboardOptions(
        port=port,
        refresh_seconds=refresh_seconds,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
    )
    status = dashboard_status_payload(ctx, options)
    readout = status["readout"]
    status_class = str(readout.get("status_class", "running"))
    gate_open = bool(readout.get("supervisor_gate_open"))
    health_ok = bool(readout.get("health_ok"))
    self_test_ok = bool(readout.get("self_test_ok"))
    windows_ok = bool(readout.get("windows_audit_ok"))
    tool_preflight_ok = bool(readout.get("tool_preflight_ok"))
    pipeline_ok = bool(readout.get("pipeline_evidence_ok"))
    loops_ok = bool(readout.get("loop_status_ok"))
    completion_ok = bool(readout.get("completion_ok"))
    progress = float(readout.get("progress_percent", 0.0) or 0.0)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{int(refresh_seconds)}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DesktopLUT Calibrator Readout</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #090b0a;
      --panel: #171b18;
      --panel2: #22261d;
      --text: #f4f7f2;
      --muted: #aab3a9;
      --line: #353c35;
      --good: #83dc8d;
      --warn: #f1c85b;
      --bad: #ff786b;
      --cool: #83c7f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Arial, sans-serif;
    }}
    main {{
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 22px;
      padding: 28px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: start;
      gap: 18px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }}
    h1, p {{ margin: 0; }}
    h1 {{ font-size: 44px; font-weight: 720; }}
    .meta {{ color: var(--muted); font-size: 18px; margin-top: 6px; overflow-wrap: anywhere; }}
    .state {{
      min-width: 300px;
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 20px 24px;
      text-align: center;
    }}
    .state span, .label {{ color: var(--muted); font-size: 16px; text-transform: uppercase; letter-spacing: 0; }}
    .state strong {{ display: block; font-size: 44px; line-height: 1.05; margin-top: 4px; }}
    .running strong, .ok {{ color: var(--good); }}
    .human strong, .warn {{ color: var(--warn); }}
    .blocked strong, .done strong, .fail {{ color: var(--bad); }}
    .center {{
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 22px;
      align-items: stretch;
    }}
    .primary, .tile {{
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 24px;
    }}
    .primary {{
      display: grid;
      align-content: center;
      gap: 22px;
    }}
    .action {{ font-size: 70px; line-height: 1; font-weight: 760; overflow-wrap: anywhere; }}
    .reason {{ font-size: 26px; line-height: 1.25; color: var(--muted); max-width: 1200px; }}
    .side {{ display: grid; gap: 22px; }}
    .number {{ font-size: 58px; line-height: 1; font-weight: 760; margin-top: 8px; }}
    .bar {{ height: 18px; background: #30352d; border: 1px solid var(--line); margin-top: 16px; }}
    .fill {{ height: 100%; width: {max(0.0, min(progress, 100.0))}%; background: var(--good); }}
    footer {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }}
    .mini {{
      border: 1px solid var(--line);
      background: var(--panel2);
      padding: 16px;
      min-height: 96px;
    }}
    .mini strong {{ display: block; margin-top: 8px; font-size: 25px; line-height: 1.05; overflow-wrap: anywhere; }}
    @media (max-width: 1000px) {{
      main {{ padding: 18px; }}
      header, .center, footer {{ display: block; }}
      .state, .tile, .primary, .mini {{ margin-top: 14px; }}
      .action {{ font-size: 44px; }}
      .reason {{ font-size: 20px; }}
      h1 {{ font-size: 34px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>DesktopLUT Calibrator</h1>
      <p class="meta">{html.escape(str(readout.get("run", "")))} - {html.escape(str(readout.get("mode", "")))} - {html.escape(str(readout.get("display") or "display"))}</p>
    </div>
    <div class="state {html.escape(status_class)}">
      <span>Run State</span>
      <strong>{html.escape(str(readout.get("status", "Ready")))}</strong>
    </div>
  </header>
  <section class="center">
    <div class="primary">
      <div class="label">Current Agent Action</div>
      <p class="action">{html.escape(str(readout.get("next_action") or "unknown"))}</p>
      <p class="reason">{html.escape(str(readout.get("reason") or ""))}</p>
    </div>
    <div class="side">
      <div class="tile">
        <div class="label">Workflow Progress</div>
        <p class="number">{html.escape(str(readout.get("progress_percent", 0)))}%</p>
        <div class="bar"><div class="fill"></div></div>
        <p class="meta">{html.escape(str(readout.get("progress_completed", 0)))}/{html.escape(str(readout.get("progress_total", 0)))} milestones</p>
      </div>
      <div class="tile">
        <div class="label">{html.escape(str(readout.get("metric_label", "Latest metric")))}</div>
        <p class="number">{html.escape(str(readout.get("metric_value", "n/a")))}</p>
      </div>
    </div>
  </section>
  <footer>
    <div class="mini"><span class="label">Supervisor Gate</span><strong class="{"ok" if gate_open else "fail"}">{html.escape(str(readout.get("supervisor_gate", "unknown")))}</strong></div>
    <div class="mini"><span class="label">Run Health</span><strong class="{"ok" if health_ok else "warn"}">{html.escape(str(readout.get("health", "unknown")))}</strong></div>
    <div class="mini"><span class="label">Self-Test</span><strong class="{"ok" if self_test_ok else "fail"}">{html.escape(str(readout.get("self_test", "missing")))}</strong></div>
    <div class="mini"><span class="label">Windows Audit</span><strong class="{"ok" if windows_ok else "fail"}">{html.escape("ready" if windows_ok else "missing")}</strong></div>
    <div class="mini"><span class="label">Tool Preflight</span><strong class="{"ok" if tool_preflight_ok else "fail"}">{html.escape(str(readout.get("tool_preflight", "missing")))}</strong></div>
    <div class="mini"><span class="label">Toolchain</span><strong class="{"ok" if pipeline_ok else "fail"}">{html.escape(str(readout.get("pipeline_evidence", "missing")))}</strong></div>
    <div class="mini"><span class="label">Loops</span><strong class="{"ok" if loops_ok else "fail"}">{html.escape(str(readout.get("loop_status", "missing")))}</strong></div>
    <div class="mini"><span class="label">Completion</span><strong class="{"ok" if completion_ok else "fail"}">{html.escape(str(readout.get("completion", "missing")))}</strong></div>
    <div class="mini"><span class="label">Active Command</span><strong class="{"ok" if readout.get("active_command_running") else "muted"}">{html.escape(str(readout.get("active_command") or "idle"))}</strong></div>
    <div class="mini"><span class="label">Last Command</span><strong>{html.escape(str(readout.get("last_command", "none")))}</strong></div>
  </footer>
</main>
</body>
</html>
"""


def write_dashboard_html(
    ctx: RunContext,
    *,
    output: Path | None = None,
    port: int | None = None,
    refresh_seconds: int = 5,
    record_stage: bool = True,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
) -> Path:
    target = output or ctx.root / "reports" / "dashboard.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_dashboard_html(
            ctx,
            port=port,
            refresh_seconds=refresh_seconds,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        ),
        encoding="utf-8",
    )
    if record_stage:
        ctx.manifest.stages.append(
            {
                "stage": "dashboard",
                "status": "written",
                "artifact": str(target),
                "refresh_seconds": refresh_seconds,
            }
        )
        ctx.save()
        ctx.log(f"Dashboard written: {target}")
    return target


def write_readout_html(
    ctx: RunContext,
    *,
    output: Path | None = None,
    port: int | None = None,
    refresh_seconds: int = 5,
    record_stage: bool = True,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
) -> Path:
    target = output or ctx.root / "reports" / "readout.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_readout_html(
            ctx,
            port=port,
            refresh_seconds=refresh_seconds,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        ),
        encoding="utf-8",
    )
    if record_stage:
        ctx.manifest.stages.append(
            {
                "stage": "readout",
                "status": "written",
                "artifact": str(target),
                "refresh_seconds": refresh_seconds,
            }
        )
        ctx.save()
        ctx.log(f"Readout written: {target}")
    return target


def make_dashboard_server(
    *,
    run_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    options: DashboardOptions | None = None,
) -> ThreadingHTTPServer:
    options = options or DashboardOptions()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "DLCStatus/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

        def _send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback
            route = urlparse(self.path).path
            try:
                ctx = open_run(run_dir)
                if route in {"/", "/dashboard.html"}:
                    payload = render_dashboard_html(
                        ctx,
                        port=options.port,
                        refresh_seconds=options.refresh_seconds,
                        execute_safe=options.execute_safe,
                        allow_hardware=options.allow_hardware,
                        allow_live_desktoplut=options.allow_live_desktoplut,
                        allow_builds=options.allow_builds,
                        mock_desktoplut=options.mock_desktoplut,
                        simulate_execution=options.simulate_execution,
                    ).encode("utf-8")
                    self._send_bytes(payload, "text/html; charset=utf-8")
                    return
                if route in {"/readout", "/readout.html"}:
                    payload = render_readout_html(
                        ctx,
                        port=options.port,
                        refresh_seconds=options.refresh_seconds,
                        execute_safe=options.execute_safe,
                        allow_hardware=options.allow_hardware,
                        allow_live_desktoplut=options.allow_live_desktoplut,
                        allow_builds=options.allow_builds,
                        mock_desktoplut=options.mock_desktoplut,
                        simulate_execution=options.simulate_execution,
                    ).encode("utf-8")
                    self._send_bytes(payload, "text/html; charset=utf-8")
                    return
                if route == "/status.json":
                    payload = json.dumps(dashboard_status_payload(ctx, options), indent=2).encode("utf-8")
                    self._send_bytes(payload, "application/json; charset=utf-8")
                    return
                if route == "/health":
                    self._send_bytes(b'{"ok":true}\n', "application/json; charset=utf-8")
                    return
                self._send_bytes(b"not found\n", "text/plain; charset=utf-8", status=404)
            except Exception as exc:
                payload = json.dumps({"ok": False, "error": str(exc)}, indent=2).encode("utf-8")
                self._send_bytes(payload, "application/json; charset=utf-8", status=500)

    return ThreadingHTTPServer((host, port), DashboardHandler)

