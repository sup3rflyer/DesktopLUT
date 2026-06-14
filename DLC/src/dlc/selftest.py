"""Full simulated pipeline self-test for DesktopLUT Calibrator."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifacts import scan_artifacts
from .decisions import IterationMetrics, MetricThresholds, decide_iteration, quality_policy_coverage, write_decision_record, write_quality_policy
from .events import EventWriter
from .human_actions import acknowledge_human_action
from .paths import RUNS_DIR
from .pipeline_evidence import tool_evidence_from_tools
from .profiles import default_dummy_icc
from .runs import create_run, open_run
from .simulation import write_identity_cube, write_placeholder_icc, write_synthetic_ti3
from .supervise import run_stage_once
from .tools import ToolSet, discover_tools
from .unattended import UnattendedRunResult, run_unattended


@dataclass(frozen=True)
class SelfTestCheck:
    name: str
    ok: bool
    detail: str
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelfTestResult:
    ok: bool
    run: str
    mode: str
    display: str
    port: int
    max_steps: int
    probe_match: bool
    unattended: dict[str, Any]
    loop_rehearsal: dict[str, Any]
    checks: list[SelfTestCheck]
    artifact_count: int
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checks"] = [check.as_dict() for check in self.checks]
        return payload


def _default_run_dir(mode: str) -> Path:
    return RUNS_DIR / f"selftest_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode.lower()}"


def latest_self_test_marker_path() -> Path:
    return RUNS_DIR / "latest_self_test.json"


def _json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def latest_self_test_status(
    *,
    max_age_hours: float = 24.0,
    now: datetime | None = None,
    require_probe_match: bool = False,
) -> dict[str, Any]:
    marker = latest_self_test_marker_path()
    payload = _json(marker)
    if payload is None:
        return {
            "ok": False,
            "reason": "latest self-test marker is missing or unreadable",
            "marker": str(marker),
            "max_age_hours": max_age_hours,
            "require_probe_match": require_probe_match,
        }
    timestamp = None
    generated_at = payload.get("generated_at")
    if isinstance(generated_at, str):
        try:
            timestamp = datetime.fromisoformat(generated_at)
        except ValueError:
            timestamp = None
    if timestamp is None:
        return {
            "ok": False,
            "reason": "latest self-test marker timestamp is unreadable",
            "marker": str(marker),
            "max_age_hours": max_age_hours,
            "require_probe_match": require_probe_match,
            "payload": payload,
        }
    now = now or datetime.now()
    age_hours = max(0.0, (now - timestamp).total_seconds() / 3600)
    probe_match_ok = (not require_probe_match) or payload.get("probe_match") is True
    ok = bool(payload.get("ok")) and age_hours <= max_age_hours and probe_match_ok
    reason = "latest self-test is fresh and passing" if ok else "latest self-test is missing, failed, or stale"
    if bool(payload.get("ok")) and age_hours <= max_age_hours and not probe_match_ok:
        reason = "latest self-test did not rehearse the requested probe-match branch"
    return {
        "ok": ok,
        "reason": reason,
        "marker": str(marker),
        "max_age_hours": max_age_hours,
        "require_probe_match": require_probe_match,
        "probe_match": payload.get("probe_match"),
        "age_hours": age_hours,
        "payload": payload,
    }


def write_latest_self_test_marker(result: "SelfTestResult") -> Path:
    marker = latest_self_test_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "ok": result.ok,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "run": result.run,
                "artifact": result.artifact,
                "artifact_count": result.artifact_count,
                "check_count": len(result.checks),
                "mode": result.mode,
                "display": result.display,
                "port": result.port,
                "probe_match": result.probe_match,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return marker


def _path_check(name: str, path: Path, detail: str) -> SelfTestCheck:
    return SelfTestCheck(name=name, ok=path.exists(), detail=detail if path.exists() else f"missing {detail}", evidence=str(path))


def _probe_match_plan_uses_correction(ctx_root: Path) -> SelfTestCheck:
    ctx = open_run(ctx_root)
    correction = ctx.manifest.desktoplut.get("probe_match_correction")
    plan_path = ctx.root / "sequences" / f"raw-mhc_iter01_profile_plan.json"
    payload = _json(plan_path)
    artifacts = payload.get("artifacts") if payload else None
    artifact_correction = artifacts.get("correction") if isinstance(artifacts, dict) else None
    command_argv = payload.get("command_argv") if payload else None
    dispread = command_argv[1] if isinstance(command_argv, list) and len(command_argv) > 1 else None
    dispread_args = [str(item) for item in dispread] if isinstance(dispread, list) else []
    metadata_ok = isinstance(correction, str) and artifact_correction == correction
    command_ok = isinstance(correction, str) and "-X" in dispread_args and correction in dispread_args
    ok = metadata_ok and command_ok
    detail = (
        "raw-MHC plan used the probe-match correction via dispread -X"
        if ok
        else "raw-MHC plan did not link the probe-match correction through metadata and dispread -X"
    )
    return SelfTestCheck("probe_match_used_by_raw_profile", ok, detail, str(plan_path))


def _loop_rehearsal_check(ctx_root: Path) -> SelfTestCheck:
    path = ctx_root / "reports" / "loop_rehearsal.json"
    payload = _json(path)
    ok = bool(payload and payload.get("ok") is True)
    detail = "MHC and 3D LUT continuation paths reached stopped loop status" if ok else "loop continuation rehearsal is missing or failed"
    return SelfTestCheck("loop_rehearsal", ok, detail, str(path))


def _quality_policy_check(ctx_root: Path) -> SelfTestCheck:
    ctx = open_run(ctx_root)
    coverage = quality_policy_coverage(ctx.manifest.desktoplut.get("quality_policy"))
    ok = bool(coverage["ok"])
    detail = "quality policy covers MHC and 3D LUT loop decisions" if ok else "quality policy is missing MHC or 3D LUT thresholds"
    return SelfTestCheck("quality_policy", ok, detail, "manifest.desktoplut.quality_policy")


def build_self_test_checks(ctx_root: Path, unattended: UnattendedRunResult, *, probe_match: bool = False) -> list[SelfTestCheck]:
    ctx = open_run(ctx_root)
    final_audit = _json(ctx.root / "reports" / "final_audit.json")
    checks = [
        SelfTestCheck(
            "unattended_ok",
            unattended.ok,
            "unattended simulated run reached finalization" if unattended.ok else "unattended simulated run did not reach finalization",
            unattended.artifact,
        ),
        SelfTestCheck(
            "manifest_finalized",
            ctx.manifest.status == "finalized",
            "manifest status is finalized" if ctx.manifest.status == "finalized" else f"manifest status is {ctx.manifest.status}",
            str(ctx.manifest_path),
        ),
        SelfTestCheck(
            "final_audit_ok",
            bool(final_audit and final_audit.get("ok") is True),
            "final audit passed" if final_audit and final_audit.get("ok") is True else "final audit is missing or failed",
            str(ctx.root / "reports" / "final_audit.json"),
        ),
        _path_check("finalization_artifact", ctx.root / "reports" / "finalization.json", "finalization artifact"),
        _path_check("final_report", ctx.root / "reports" / "calibration_report.html", "calibration report"),
        _path_check("raw_mhc_ti3", ctx.root / "measurements" / f"raw-mhc_iter01_{ctx.manifest.mode.lower()}.ti3", "simulated raw-MHC TI3"),
        _path_check("post_mhc_icc", ctx.root / "measurements" / f"post-mhc_iter01_{ctx.manifest.mode.lower()}.icc", "simulated post-MHC ICC"),
        _path_check("runtime_3dlut_cube", ctx.root / "generated" / f"3dlut_iter01_{ctx.manifest.mode.lower()}.cube", "simulated runtime 3D LUT cube"),
        _path_check("loop_status", ctx.root / "reports" / "loop_status.json", "MHC/3D LUT loop status"),
        _quality_policy_check(ctx.root),
        _path_check("pipeline_evidence", ctx.root / "reports" / "pipeline_evidence.json", "scriptable pipeline evidence"),
        _loop_rehearsal_check(ctx.root),
        _path_check("dashboard", ctx.root / "reports" / "dashboard.html", "second-monitor dashboard"),
        _path_check("readout", ctx.root / "reports" / "readout.html", "large second-monitor readout"),
    ]
    if probe_match:
        request = ctx.manifest.desktoplut.get("probe_match_request")
        kind = request.get("kind") if isinstance(request, dict) else "ccmx"
        suffix = ".ccss" if kind == "ccss" else ".ccmx"
        checks.extend(
            [
                _path_check("probe_match_correction", ctx.root / "probe_match" / f"probe_match_iter01_{ctx.manifest.mode.lower()}{suffix}", "simulated probe-match correction"),
                _probe_match_plan_uses_correction(ctx.root),
            ]
        )
    return checks


def _run_loop_step(ctx_root: Path, expected_action: str, *, port: int) -> dict[str, Any]:
    result = run_stage_once(
        open_run(ctx_root),
        expected_action=expected_action,
        port=port,
        execute_safe=True,
        mock_desktoplut=True,
        simulate_execution=True,
    )
    return result.as_dict()


def _write_continue_decision(ctx_root: Path, phase: str) -> None:
    thresholds = MetricThresholds()
    if phase == "mhc":
        metrics = IterationMetrics(iteration=1, avg_de2000=2.4, p95_de2000=4.2, max_de2000=6.2, white_de2000=2.3)
    else:
        metrics = IterationMetrics(
            iteration=1,
            avg_de2000=2.4,
            p95_de2000=4.2,
            max_de2000=6.2,
            white_de2000=2.3,
            extra={"lut_integrity": {"ok": True, "max_neighbor_delta": 0.2, "monotonicity_violations": 0}},
        )
    write_decision_record(
        ctx=open_run(ctx_root),
        decision=decide_iteration(phase, metrics, thresholds),
        metrics=metrics,
        thresholds=thresholds,
    )


def run_loop_rehearsal(
    *,
    parent_run: Path,
    mode: str,
    display: str,
    port: int,
    tools: ToolSet,
) -> dict[str, Any]:
    rehearsal_root = parent_run / "loop_rehearsal_run"
    ctx = create_run(mode, f"{display} Loop Rehearsal", rehearsal_root)
    ctx.manifest.tools = tools.as_manifest()
    ctx.manifest.desktoplut["tool_evidence"] = tool_evidence_from_tools(tools)
    ctx.manifest.desktoplut["calibration_mode"] = {
        "ok": True,
        "result": {
            "active": True,
            "dummy_icc_path": str(default_dummy_icc(mode).path),
            "corrections_reset": True,
        },
    }
    write_quality_policy(ctx=ctx, phase="mhc", thresholds=MetricThresholds())
    write_quality_policy(ctx=open_run(ctx.root), phase="3dlut", thresholds=MetricThresholds())
    ctx = open_run(ctx.root)
    acknowledge_human_action(ctx, "colorimeter_placed", instrument="simulated", position="center", note="DLC loop rehearsal")

    verification_ti3 = ctx.root / "measurements" / f"mhc-verification_iter01_{mode.lower()}.ti3"
    write_synthetic_ti3(verification_ti3)
    ctx = open_run(ctx.root)
    ctx.manifest.stages.extend(
        [
            {"stage": "raw-mhc", "iteration": 1, "status": "completed"},
            {"stage": "build_mhc_baseline", "iteration": 1, "status": "built"},
            {"stage": "apply_mhc_baseline", "iteration": 1, "status": "applied"},
            {"stage": "mhc-verification", "iteration": 1, "status": "completed", "artifacts": {"ti3": str(verification_ti3)}},
        ]
    )
    ctx.save()
    _write_continue_decision(ctx.root, "mhc")

    steps: list[dict[str, Any]] = []
    for action in [
        "build_mhc_iteration",
        "apply_mhc_iteration",
        "plan_mhc_verification_iteration",
        "execute_mhc_verification_iteration",
        "score_mhc_iteration",
        "decide_mhc_iteration",
    ]:
        steps.append(_run_loop_step(ctx.root, action, port=port))

    post_icc = ctx.root / "measurements" / f"post-mhc_iter01_{mode.lower()}.icc"
    cube = ctx.root / "generated" / f"3dlut_iter01_{mode.lower()}.cube"
    write_placeholder_icc(post_icc, description="3D LUT loop rehearsal seed post-MHC profile")
    write_identity_cube(cube, size=3, title="DLC loop rehearsal seed 3D LUT")
    ctx = open_run(ctx.root)
    ctx.manifest.stages.extend(
        [
            {"stage": "post-mhc", "iteration": 1, "status": "completed", "artifacts": {"icc": str(post_icc)}},
            {"stage": "build_3dlut", "iteration": 1, "status": "completed", "artifacts": {"cube": str(cube)}},
            {"stage": "apply_3dlut", "iteration": 1, "status": "applied", "cube": str(cube)},
        ]
    )
    ctx.save()
    _write_continue_decision(ctx.root, "3dlut")

    for action in [
        "plan_post_mhc_iteration",
        "execute_post_mhc_iteration",
        "plan_3dlut_iteration",
        "execute_3dlut_iteration",
        "apply_3dlut_iteration",
        "plan_3dlut_verification_iteration",
        "execute_3dlut_verification_iteration",
        "score_3dlut_iteration",
        "check_3dlut_integrity",
        "decide_3dlut_iteration",
    ]:
        steps.append(_run_loop_step(ctx.root, action, port=port))

    current = open_run(ctx.root)
    loop_status = current.manifest.desktoplut.get("loop_status")
    phases = loop_status.get("phases") if isinstance(loop_status, dict) else {}
    mhc = phases.get("mhc") if isinstance(phases, dict) and isinstance(phases.get("mhc"), dict) else {}
    lut3d = phases.get("3dlut") if isinstance(phases, dict) and isinstance(phases.get("3dlut"), dict) else {}
    ok = all(step.get("ok") is True for step in steps) and mhc.get("status") == "stopped" and lut3d.get("status") == "stopped"
    report_dir = parent_run / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact = report_dir / "loop_rehearsal.json"
    payload = {
        "ok": ok,
        "run": str(current.root),
        "artifact": str(artifact),
        "mhc_status": mhc.get("status"),
        "mhc_latest_iteration": mhc.get("latest_iteration"),
        "3dlut_status": lut3d.get("status"),
        "3dlut_latest_iteration": lut3d.get("latest_iteration"),
        "step_count": len(steps),
        "steps": steps,
    }
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def run_self_test(
    *,
    run_dir: Path | None = None,
    mode: str = "SDR",
    display: str = "DLC Self Test",
    port: int = 1,
    max_steps: int = 60,
    update_dashboard: bool = True,
    dashboard_refresh_seconds: int = 2,
    probe_match: bool = False,
    probe_match_kind: str = "ccmx",
    probe_match_display_tech: str = "u",
    probe_match_high_res: bool = False,
    tools: ToolSet | None = None,
) -> SelfTestResult:
    tools = tools or discover_tools()
    ctx = create_run(mode, display, run_dir or _default_run_dir(mode))
    ctx.manifest.tools = tools.as_manifest()
    ctx.manifest.desktoplut["tool_evidence"] = tool_evidence_from_tools(tools)
    if probe_match:
        ctx.manifest.desktoplut["probe_match_request"] = {
            "enabled": True,
            "kind": probe_match_kind,
            "display_tech": probe_match_display_tech,
            "display_index": 1,
            "patch_window": "0.5,0.5,1.0",
            "high_res": probe_match_high_res,
        }
    ctx.save()
    write_quality_policy(ctx=open_run(ctx.root), phase="mhc", thresholds=MetricThresholds())
    write_quality_policy(ctx=open_run(ctx.root), phase="3dlut", thresholds=MetricThresholds())
    ctx = open_run(ctx.root)
    if probe_match:
        acknowledge_human_action(ctx, "spectro_placed", instrument="simulated", position="center", note="DLC self-test probe match")
    acknowledge_human_action(ctx, "colorimeter_placed", instrument="simulated", position="center", note="DLC self-test")

    unattended = run_unattended(
        ctx=open_run(ctx.root),
        tools=tools,
        port=port,
        max_steps=max_steps,
        execute_safe=True,
        mock_desktoplut=True,
        simulate_execution=True,
        auto_tool_preflight=False,
        update_dashboard=update_dashboard,
        dashboard_refresh_seconds=dashboard_refresh_seconds,
    )
    loop_rehearsal = run_loop_rehearsal(parent_run=ctx.root, mode=mode, display=display, port=port, tools=tools)

    checks = build_self_test_checks(ctx.root, unattended, probe_match=probe_match)
    artifacts = scan_artifacts(ctx.root)
    report_dir = ctx.root / "reports"
    artifact = report_dir / "self_test.json"
    result = SelfTestResult(
        ok=all(check.ok for check in checks),
        run=str(ctx.root),
        mode=mode,
        display=display,
        port=port,
        max_steps=max_steps,
        probe_match=probe_match,
        unattended=unattended.as_dict(),
        loop_rehearsal=loop_rehearsal,
        checks=checks,
        artifact_count=len(artifacts),
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    latest_marker = write_latest_self_test_marker(result)

    current = open_run(ctx.root)
    current.manifest.stages.append(
        {
            "stage": "self_test",
            "status": "passed" if result.ok else "failed",
            "artifact": str(artifact),
            "artifact_count": len(artifacts),
            "latest_marker": str(latest_marker),
        }
    )
    current.save()
    current.log(f"Self-test {'passed' if result.ok else 'failed'}: {artifact}")
    EventWriter(current.events_path).write(
        "INFO" if result.ok else "ERROR",
        "self_test",
        "self_test_finished",
        ok=result.ok,
        artifact=str(artifact),
    )
    return result

