"""Final run audit before an agent treats calibration as complete."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Any

from .artifacts import scan_artifacts
from .calibration_mode import calibration_mode_evidence
from .decisions import quality_policy_coverage
from .events import EventWriter
from .runs import RunContext
from .tools import REQUIRED_TOOL_NAMES


@dataclass(frozen=True)
class FinalAuditCheck:
    name: str
    ok: bool
    detail: str
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FinalAuditResult:
    ok: bool
    run: str
    checks: list[FinalAuditCheck] = field(default_factory=list)
    artifact_count: int = 0
    audit_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checks"] = [check.as_dict() for check in self.checks]
        return payload


def resolve_run_path(ctx: RunContext, value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    candidate = ctx.root / path
    return candidate if candidate.exists() else path


def stage_entries(ctx: RunContext, stage: str) -> list[dict[str, Any]]:
    return [entry for entry in ctx.manifest.stages if entry.get("stage") == stage]


def latest_stage(ctx: RunContext, stage: str) -> dict[str, Any] | None:
    entries = stage_entries(ctx, stage)
    return entries[-1] if entries else None


def latest_stage_with_status(ctx: RunContext, stage: str, status: str) -> dict[str, Any] | None:
    for entry in reversed(stage_entries(ctx, stage)):
        if entry.get("status") == status:
            return entry
    return None


def latest_path_from_stage(ctx: RunContext, stage: str, key: str) -> Path | None:
    for entry in reversed(stage_entries(ctx, stage)):
        value = entry.get(key)
        if isinstance(value, str):
            return resolve_run_path(ctx, value)
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get(key), str):
            return resolve_run_path(ctx, str(artifacts[key]))
    return None


def path_check(name: str, path: Path | None, detail: str) -> FinalAuditCheck:
    ok = path is not None and path.exists()
    return FinalAuditCheck(name=name, ok=ok, detail=detail if ok else f"missing {detail}", evidence=str(path) if path else None)


def load_json_path(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None or not path.exists():
        return None, "missing artifact"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "artifact is not a JSON object"
    return payload, None


def simulated_execution_mode(ctx: RunContext) -> bool:
    for key in ("supervision_options", "unattended_options"):
        options = ctx.manifest.desktoplut.get(key)
        if isinstance(options, dict) and options.get("simulate_execution") is True:
            return True
    for stage in reversed(ctx.manifest.stages):
        if stage.get("simulate_execution") is True:
            return True
    payload, _ = load_json_path(ctx.root / "reports" / "unattended.json")
    return isinstance(payload, dict) and payload.get("simulate_execution") is True


SIMULATED_EXECUTION_STAGES = {
    "probe_match",
    "raw-mhc",
    "mhc-verification",
    "post-mhc",
    "build_3dlut",
    "3dlut-verification",
}


def simulated_execution_records(ctx: RunContext) -> list[str]:
    records: list[str] = []
    for index, stage in enumerate(ctx.manifest.stages):
        stage_name = stage.get("stage")
        if not isinstance(stage_name, str) or stage_name not in SIMULATED_EXECUTION_STAGES:
            continue
        stage_simulated = stage.get("simulated") is True
        result_simulated = False
        result_path = resolve_run_path(ctx, stage.get("execution_result") if isinstance(stage.get("execution_result"), str) else None)
        payload, _ = load_json_path(result_path)
        if isinstance(payload, dict) and payload.get("simulated") is True:
            result_simulated = True
        if stage_simulated or result_simulated:
            iteration = stage.get("iteration")
            suffix = f" iter{iteration}" if iteration is not None else ""
            records.append(f"{index}:{stage_name}{suffix}")
    return records


def simulation_boundary_check(ctx: RunContext) -> FinalAuditCheck:
    records = simulated_execution_records(ctx)
    explicitly_simulated = simulated_execution_mode(ctx)
    ok = not records or explicitly_simulated
    if not records:
        detail = "no simulated execution artifacts are present"
    elif explicitly_simulated:
        detail = f"{len(records)} simulated execution artifact(s) are allowed by explicit simulate_execution provenance"
    else:
        detail = "simulated execution artifacts are present without explicit simulate_execution provenance: " + ", ".join(records)
    return FinalAuditCheck(
        "simulation_boundary",
        ok,
        detail,
        "manifest.desktoplut.supervision_options/unattended_options",
    )


def decision_stop_check(ctx: RunContext, phase: str) -> FinalAuditCheck:
    stage_name = f"{phase}_decision"
    entry = latest_stage_with_status(ctx, stage_name, "stop")
    evidence = str(entry.get("decision")) if entry and entry.get("decision") else None
    return FinalAuditCheck(
        name=f"{phase}_decision_stop",
        ok=entry is not None,
        detail=f"{phase} loop has a stop decision" if entry is not None else f"{phase} loop is missing a stop decision",
        evidence=evidence,
    )


def calibration_mode_check(ctx: RunContext) -> FinalAuditCheck:
    evidence = calibration_mode_evidence(ctx)
    ok = bool(evidence["ok"])
    missing = evidence.get("missing")
    return FinalAuditCheck(
        name="desktoplut_calibration_mode",
        ok=ok,
        detail=(
            "DesktopLUT calibration mode is active with the expected dummy ICC and reset correction layers"
            if ok
            else "DesktopLUT calibration mode evidence is incomplete: " + ", ".join(str(item) for item in missing)
        ),
        evidence="manifest.desktoplut.calibration_mode",
    )


def applied_stage_check(ctx: RunContext, stage: str, label: str) -> FinalAuditCheck:
    entry = latest_stage_with_status(ctx, stage, "applied")
    return FinalAuditCheck(
        name=stage,
        ok=entry is not None,
        detail=f"{label} has been applied" if entry is not None else f"{label} has not been applied",
        evidence=str(entry.get("candidate") or entry.get("cube") or "") if entry else None,
    )


def applied_mhc_candidate(ctx: RunContext) -> str | None:
    entry = latest_stage_with_status(ctx, "apply_mhc_baseline", "applied")
    candidate = entry.get("candidate") if entry else None
    return candidate if isinstance(candidate, str) else None


def applied_3dlut_cube(ctx: RunContext) -> str | None:
    entry = latest_stage_with_status(ctx, "apply_3dlut", "applied")
    cube = entry.get("cube") if entry else None
    return cube if isinstance(cube, str) else None


def same_resolved_path(ctx: RunContext, left: str | Path | None, right: str | Path | None) -> bool:
    left_path = resolve_run_path(ctx, left)
    right_path = resolve_run_path(ctx, right)
    if left_path is None or right_path is None:
        return False
    return str(left_path).lower() == str(right_path).lower()


def latest_3dlut_build_for_cube(ctx: RunContext, cube: str) -> dict[str, Any] | None:
    for entry in reversed(stage_entries(ctx, "build_3dlut")):
        if entry.get("status") != "completed":
            continue
        artifacts = entry.get("artifacts")
        build_cube = artifacts.get("cube") if isinstance(artifacts, dict) else None
        if isinstance(build_cube, str) and same_resolved_path(ctx, build_cube, cube):
            return entry
    return None


def latest_tool_preflight_record(ctx: RunContext, name: str) -> dict[str, Any] | None:
    path = latest_path_from_stage(ctx, "tool_preflight", "artifact") or ctx.root / "preflight" / "tool_preflight.json"
    payload, _ = load_json_path(path)
    tools = payload.get("tools") if isinstance(payload, dict) else None
    record = tools.get(name) if isinstance(tools, dict) else None
    return record if isinstance(record, dict) else None


def command_executable_matches_preflight(ctx: RunContext, argv: Any, tool_name: str) -> bool:
    if not isinstance(argv, list) or not argv:
        return False
    record = latest_tool_preflight_record(ctx, tool_name)
    path = record.get("path") if isinstance(record, dict) else None
    return isinstance(path, str) and same_resolved_path(ctx, str(argv[0]), path)


def latest_completed_profile_stage(ctx: RunContext, stage: str) -> dict[str, Any] | None:
    return latest_stage_with_status(ctx, stage, "completed")


def profile_measurement_lineage_check(ctx: RunContext, stage: str, required_artifacts: tuple[str, ...]) -> FinalAuditCheck:
    entry = latest_completed_profile_stage(ctx, stage)
    name = f"{stage.replace('-', '_')}_profile_lineage"
    if entry is None:
        return FinalAuditCheck(name, False, f"{stage} has no completed profile execution stage", stage)

    plan_path = resolve_run_path(ctx, entry.get("plan") if isinstance(entry.get("plan"), str) else None)
    result_path = resolve_run_path(ctx, entry.get("execution_result") if isinstance(entry.get("execution_result"), str) else None)
    plan, plan_error = load_json_path(plan_path)
    result, result_error = load_json_path(result_path)
    details = []
    if plan is None:
        details.append(f"could not read {stage} profile plan: {plan_error}")
    if result is None:
        details.append(f"could not read {stage} execution result: {result_error}")
    if details:
        return FinalAuditCheck(name, False, "; ".join(details), str(plan_path or result_path or stage))

    plan_artifacts = plan.get("artifacts")
    stage_artifacts = entry.get("artifacts")
    command_argv = plan.get("command_argv")
    results = result.get("results")
    if plan.get("stage") != stage or result.get("stage") != stage:
        details.append("profile plan/result stage does not match manifest stage")
    if entry.get("iteration") != plan.get("iteration") or entry.get("iteration") != result.get("iteration"):
        details.append("profile plan/result iteration does not match manifest stage")
    if result.get("ok") is not True or result.get("dry_run") is not False:
        details.append("profile execution result is not a completed execution")
    if not isinstance(results, list) or not results or any(not isinstance(item, dict) or item.get("returncode") != 0 for item in results):
        details.append("profile execution commands did not all finish with returncode 0")
    if not isinstance(command_argv, list) or len(command_argv) < 3:
        details.append("profile plan is missing targen/dispread/colprof commands")
    else:
        for index, tool_name in enumerate(("targen", "dispread", "colprof")):
            if not command_executable_matches_preflight(ctx, command_argv[index], tool_name):
                details.append(f"profile plan {tool_name} executable does not match run-local tool preflight")
        dispread = command_argv[1]
        if not isinstance(dispread, list) or "-Yp" not in [str(item) for item in dispread]:
            details.append("profile plan dispread command does not use unattended -Yp")

    for key in required_artifacts:
        plan_value = plan_artifacts.get(key) if isinstance(plan_artifacts, dict) else None
        stage_value = stage_artifacts.get(key) if isinstance(stage_artifacts, dict) else None
        if not isinstance(plan_value, str) or not isinstance(stage_value, str) or not same_resolved_path(ctx, plan_value, stage_value):
            details.append(f"{stage} {key} artifact does not match profile plan")
            continue
        resolved = resolve_run_path(ctx, stage_value)
        if resolved is None or not resolved.exists():
            details.append(f"{stage} {key} artifact is missing")

    ok = not details
    return FinalAuditCheck(
        name,
        ok,
        f"{stage} artifacts are linked to completed preflighted targen/dispread/colprof execution"
        if ok
        else "; ".join(details),
        str(result_path),
    )


def latest_mhc_build_for_candidate(ctx: RunContext, candidate: str) -> dict[str, Any] | None:
    for entry in reversed(stage_entries(ctx, "build_mhc_baseline")):
        if entry.get("status") != "candidate_built":
            continue
        entry_candidate = entry.get("candidate")
        artifacts = entry.get("artifacts")
        artifact_candidate = artifacts.get("candidate") if isinstance(artifacts, dict) else None
        if (
            isinstance(entry_candidate, str)
            and same_resolved_path(ctx, entry_candidate, candidate)
            and isinstance(artifact_candidate, str)
            and same_resolved_path(ctx, artifact_candidate, candidate)
        ):
            return entry
    return None


def mhc_candidate_lineage_check(ctx: RunContext) -> FinalAuditCheck:
    candidate = applied_mhc_candidate(ctx)
    if candidate is None:
        return FinalAuditCheck("mhc_candidate_lineage", False, "no applied MHC candidate is recorded", "apply_mhc_baseline.candidate")

    candidate_path = resolve_run_path(ctx, candidate)
    payload, error = load_json_path(candidate_path)
    if payload is None:
        return FinalAuditCheck("mhc_candidate_lineage", False, f"could not read MHC candidate: {error}", str(candidate_path or candidate))

    build = latest_mhc_build_for_candidate(ctx, candidate)
    raw_ti3 = latest_path_from_stage(ctx, "raw-mhc", "ti3")
    applied = latest_stage_with_status(ctx, "apply_mhc_baseline", "applied")
    result = applied.get("result") if isinstance(applied, dict) else None
    artifacts = build.get("artifacts") if isinstance(build, dict) else None
    details = []

    if build is None:
        details.append("applied MHC candidate has no candidate_built build_mhc_baseline stage")
    candidate_json_path = payload.get("candidate_path")
    cube_path = payload.get("cube_path")
    source = payload.get("source")
    fallback = payload.get("fallback")
    iteration = payload.get("iteration")

    if isinstance(candidate_json_path, str) and not same_resolved_path(ctx, candidate_json_path, candidate):
        details.append("candidate JSON path does not match applied candidate")
    if not isinstance(cube_path, str) or not (resolve_run_path(ctx, cube_path) or Path()).exists():
        details.append("candidate cube artifact is missing")
    if artifacts is not None:
        build_cube = artifacts.get("cube") if isinstance(artifacts, dict) else None
        if not isinstance(build_cube, str) or not isinstance(cube_path, str) or not same_resolved_path(ctx, build_cube, cube_path):
            details.append("build stage cube does not match candidate JSON")
    if raw_ti3 is None:
        details.append("raw-MHC TI3 source is missing")
    elif not isinstance(source, str) or source == "defaults" or not same_resolved_path(ctx, source, raw_ti3):
        details.append("candidate source is not the recorded raw-MHC TI3")
    if fallback is not False:
        details.append("candidate is fallback/default-generated")
    if applied is not None and applied.get("iteration") != iteration:
        details.append("applied iteration does not match candidate JSON")
    result_candidate = result.get("candidate") if isinstance(result, dict) else None
    if result_candidate is not None and (not isinstance(result_candidate, str) or not same_resolved_path(ctx, result_candidate, candidate)):
        details.append("apply result candidate does not match applied candidate")
    if isinstance(result, dict) and result.get("ok") is not True:
        details.append("apply result is not ok")

    ok = not details
    return FinalAuditCheck(
        "mhc_candidate_lineage",
        ok,
        "applied MHC candidate is linked to the recorded raw-MHC TI3 and generated cube"
        if ok
        else "; ".join(details),
        str(candidate_path or candidate),
    )


def lut_build_lineage_check(ctx: RunContext) -> FinalAuditCheck:
    cube = applied_3dlut_cube(ctx)
    if cube is None:
        return FinalAuditCheck("3dlut_build_lineage", False, "no applied 3D LUT cube is recorded", "apply_3dlut.cube")
    build = latest_3dlut_build_for_cube(ctx, cube)
    if build is None:
        return FinalAuditCheck("3dlut_build_lineage", False, "applied 3D LUT cube has no completed build_3dlut stage", cube)

    plan_path = resolve_run_path(ctx, build.get("plan") if isinstance(build.get("plan"), str) else None)
    result_path = resolve_run_path(ctx, build.get("execution_result") if isinstance(build.get("execution_result"), str) else None)
    plan, plan_error = load_json_path(plan_path)
    result, result_error = load_json_path(result_path)
    details = []
    if plan is None:
        details.append(f"could not read 3D LUT build plan: {plan_error}")
    if result is None:
        details.append(f"could not read 3D LUT build result: {result_error}")
    if details:
        return FinalAuditCheck("3dlut_build_lineage", False, "; ".join(details), str(plan_path or result_path or cube))

    command_argv = plan.get("command_argv")
    artifacts = plan.get("artifacts")
    plan_cube = artifacts.get("cube") if isinstance(artifacts, dict) else None
    command_uses_collink = isinstance(command_argv, list) and any("collink" in str(item).lower() for item in command_argv)
    command_has_cube_flag = isinstance(command_argv, list) and "-3c" in [str(item) for item in command_argv]
    command_executable = str(command_argv[0]) if isinstance(command_argv, list) and command_argv else None
    preflight_collink = latest_tool_preflight_record(ctx, "collink")
    preflight_collink_path = preflight_collink.get("path") if isinstance(preflight_collink, dict) else None
    result_ok = result.get("ok") is True and result.get("dry_run") is False
    execution_finished = result.get("simulated") is True or result.get("returncode") == 0
    result_cube = result.get("cube_path")
    cube_exists = (resolve_run_path(ctx, cube) or Path()).exists()

    if not command_uses_collink:
        details.append("build plan does not use collink")
    if not command_has_cube_flag:
        details.append("build plan does not include -3c cube output")
    if not isinstance(preflight_collink_path, str):
        details.append("run-local tool preflight is missing collink path")
    elif not isinstance(command_executable, str) or not same_resolved_path(ctx, command_executable, preflight_collink_path):
        details.append("build plan collink executable does not match run-local tool preflight")
    if not isinstance(plan_cube, str) or not same_resolved_path(ctx, plan_cube, cube):
        details.append("build plan cube does not match applied cube")
    if not isinstance(result_cube, str) or not same_resolved_path(ctx, result_cube, cube):
        details.append("build result cube does not match applied cube")
    if not result_ok:
        details.append("build result is not a completed execution")
    if not execution_finished:
        details.append("build result is neither simulated nor returncode 0")
    if not cube_exists:
        details.append("applied cube file is missing")

    ok = not details
    return FinalAuditCheck(
        "3dlut_build_lineage",
        ok,
        "applied 3D LUT cube is linked to a completed DLC/Argyll collink -3c build"
        if ok
        else "; ".join(details),
        str(result_path),
    )


def lut_integrity_check(ctx: RunContext) -> FinalAuditCheck:
    path = latest_path_from_stage(ctx, "3dlut_lut_integrity", "integrity")
    if path is None or not path.exists():
        return FinalAuditCheck("3dlut_integrity", False, "missing 3D LUT integrity artifact", str(path) if path else None)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return FinalAuditCheck("3dlut_integrity", False, f"could not read 3D LUT integrity artifact: {exc}", str(path))
    ok = bool(payload.get("ok"))
    return FinalAuditCheck(
        name="3dlut_integrity",
        ok=ok,
        detail="3D LUT integrity passed" if ok else "3D LUT integrity did not pass",
        evidence=str(path),
    )


def desktoplut_final_state_content_check(ctx: RunContext) -> FinalAuditCheck:
    path = latest_path_from_stage(ctx, "desktoplut_state_capture", "artifact")
    payload, error = load_json_path(path)
    if payload is None:
        return FinalAuditCheck("desktoplut_final_state_content", False, f"could not read DesktopLUT final state: {error}", str(path) if path else None)
    response = payload.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    if not payload.get("ok") or not isinstance(result, dict):
        return FinalAuditCheck("desktoplut_final_state_content", False, "DesktopLUT final state response is not ok", str(path))
    mhc = result.get("mhc")
    runtime = result.get("runtime")
    mhc_applied = isinstance(mhc, dict) and any(isinstance(entry, dict) and entry.get("applied") is True for entry in mhc.values())
    runtime_cube = isinstance(runtime, dict) and any(isinstance(entry, dict) and bool(entry.get("cube_path")) for entry in runtime.values())
    applied_mhc_cube = None
    candidate = applied_mhc_candidate(ctx)
    candidate_payload, _ = load_json_path(resolve_run_path(ctx, candidate))
    if isinstance(candidate_payload, dict) and isinstance(candidate_payload.get("cube_path"), str):
        applied_mhc_cube = str(candidate_payload["cube_path"])
    mhc_cube_matches = (
        isinstance(mhc, dict)
        and applied_mhc_cube is not None
        and any(
            isinstance(entry, dict)
            and entry.get("applied") is True
            and isinstance(entry.get("cube_path"), str)
            and same_resolved_path(ctx, str(entry["cube_path"]), applied_mhc_cube)
            for entry in mhc.values()
        )
    )
    applied_cube = applied_3dlut_cube(ctx)
    runtime_cube_matches = (
        isinstance(runtime, dict)
        and applied_cube is not None
        and any(
            isinstance(entry, dict)
            and isinstance(entry.get("cube_path"), str)
            and same_resolved_path(ctx, str(entry["cube_path"]), applied_cube)
            for entry in runtime.values()
        )
    )
    running = result.get("running") is True
    ok = running and mhc_applied and runtime_cube and mhc_cube_matches and runtime_cube_matches
    details = []
    if not running:
        details.append("DesktopLUT is not reported running")
    if not mhc_applied:
        details.append("no applied MHC entry in DesktopLUT final state")
    if not runtime_cube:
        details.append("no runtime 3D LUT cube path in DesktopLUT final state")
    if not mhc_cube_matches:
        details.append("final MHC cube does not match applied MHC candidate")
    if not runtime_cube_matches:
        details.append("final runtime 3D LUT cube does not match applied cube")
    return FinalAuditCheck(
        "desktoplut_final_state_content",
        ok,
        "DesktopLUT final state matches the applied MHC candidate and runtime 3D LUT" if ok else "; ".join(details),
        str(path),
    )


def profile_basename(value: Any) -> str:
    return Path(str(value).replace("\\", "/")).name.lower()


def expected_windows_profile(ctx: RunContext) -> str | None:
    calibration = ctx.manifest.desktoplut.get("calibration_mode")
    result = calibration.get("result") if isinstance(calibration, dict) else None
    dummy = result.get("dummy_icc_path") if isinstance(result, dict) else None
    return str(dummy) if isinstance(dummy, str) else None


def windows_color_state_content_check(ctx: RunContext) -> FinalAuditCheck:
    path = latest_path_from_stage(ctx, "windows_color_state_capture", "artifact")
    payload, error = load_json_path(path)
    if payload is None:
        return FinalAuditCheck("windows_color_state_content", False, f"could not read Windows color-state capture: {error}", str(path) if path else None)
    profiles = payload.get("profiles")
    gamma_ramp = payload.get("gamma_ramp")
    profiles_ok = isinstance(profiles, dict) and profiles.get("ok") is True
    gamma_ok = isinstance(gamma_ramp, dict) and gamma_ramp.get("ok") is True
    profiles_result = profiles.get("result") if isinstance(profiles, dict) else None
    gamma_result = gamma_ramp.get("result") if isinstance(gamma_ramp, dict) else None
    details = []
    if not bool(payload.get("ok")) or not profiles_ok or not gamma_ok:
        details.append("Windows profile/gamma query response is missing or not ok")

    if isinstance(profiles_result, dict) and profiles_result.get("available") is True:
        expected_profile = expected_windows_profile(ctx)
        active_profile = profiles_result.get("active_profile")
        if not isinstance(expected_profile, str):
            details.append("expected Windows profile is missing from calibration mode")
        elif not isinstance(active_profile, str) or profile_basename(active_profile) != profile_basename(expected_profile):
            details.append("active Windows ICC profile does not match calibration-mode dummy ICC")

    if isinstance(gamma_result, dict) and gamma_result.get("available") is True:
        if gamma_result.get("gamma_ramp_loaded") is True:
            details.append("Windows gamma ramp is still loaded")
        if gamma_result.get("vcgt_present") is True:
            details.append("Windows VCGT is still present")

    ok = not details
    return FinalAuditCheck(
        "windows_color_state_content",
        ok,
        "Windows profile and gamma-ramp query responses are readable and compatible with calibration mode"
        if ok
        else "; ".join(details),
        str(path),
    )


def probe_match_request(ctx: RunContext) -> dict[str, Any] | None:
    request = ctx.manifest.desktoplut.get("probe_match_request")
    if isinstance(request, dict) and request.get("enabled") is True:
        return request
    return None


def probe_match_correction_check(ctx: RunContext) -> FinalAuditCheck:
    value = ctx.manifest.desktoplut.get("probe_match_correction")
    path = resolve_run_path(ctx, value) if isinstance(value, str) else None
    ok = path is not None and path.exists()
    return FinalAuditCheck(
        "probe_match_correction",
        ok,
        "requested probe-match correction exists" if ok else "probe matching was requested but no completed correction artifact is recorded",
        str(path) if path else "manifest.desktoplut.probe_match_correction",
    )


def latest_probe_match_for_correction(ctx: RunContext, correction: str) -> dict[str, Any] | None:
    for entry in reversed(stage_entries(ctx, "probe_match")):
        if entry.get("status") != "completed":
            continue
        entry_correction = entry.get("correction")
        if isinstance(entry_correction, str) and same_resolved_path(ctx, entry_correction, correction):
            return entry
    return None


def probe_match_correction_lineage_check(ctx: RunContext) -> FinalAuditCheck:
    correction = ctx.manifest.desktoplut.get("probe_match_correction")
    if not isinstance(correction, str):
        return FinalAuditCheck(
            "probe_match_correction_lineage",
            False,
            "probe matching was requested but no correction path is recorded",
            "manifest.desktoplut.probe_match_correction",
        )
    entry = latest_probe_match_for_correction(ctx, correction)
    if entry is None:
        return FinalAuditCheck(
            "probe_match_correction_lineage",
            False,
            "probe-match correction has no completed probe_match execution stage",
            correction,
        )

    plan_path = resolve_run_path(ctx, entry.get("plan") if isinstance(entry.get("plan"), str) else None)
    result_path = resolve_run_path(ctx, entry.get("execution_result") if isinstance(entry.get("execution_result"), str) else None)
    plan, plan_error = load_json_path(plan_path)
    result, result_error = load_json_path(result_path)
    details = []
    if plan is None:
        details.append(f"could not read probe-match plan: {plan_error}")
    if result is None:
        details.append(f"could not read probe-match execution result: {result_error}")
    if details:
        return FinalAuditCheck("probe_match_correction_lineage", False, "; ".join(details), str(plan_path or result_path or correction))

    plan_artifacts = plan.get("artifacts")
    plan_correction = plan_artifacts.get("correction") if isinstance(plan_artifacts, dict) else None
    result_correction = result.get("correction")
    command_argv = plan.get("command_argv")
    if not isinstance(plan_correction, str) or not same_resolved_path(ctx, plan_correction, correction):
        details.append("probe-match plan correction does not match recorded correction")
    if not isinstance(result_correction, str) or not same_resolved_path(ctx, result_correction, correction):
        details.append("probe-match result correction does not match recorded correction")
    if result.get("ok") is not True or result.get("dry_run") is not False:
        details.append("probe-match execution result is not a completed execution")
    if result.get("simulated") is not True and result.get("returncode") != 0:
        details.append("probe-match execution result is neither simulated nor returncode 0")
    if not command_executable_matches_preflight(ctx, command_argv, "ccxxmake"):
        details.append("probe-match ccxxmake executable does not match run-local tool preflight")
    if not (resolve_run_path(ctx, correction) or Path()).exists():
        details.append("probe-match correction artifact is missing")
    inventory = result.get("instrument_inventory")
    if result.get("simulated") is not True and plan.get("measurement_mode") == "live":
        if not isinstance(inventory, dict) or inventory.get("ok") is not True:
            details.append("live probe-match execution is missing passing instrument inventory")

    ok = not details
    return FinalAuditCheck(
        "probe_match_correction_lineage",
        ok,
        "probe-match correction is linked to completed preflighted ccxxmake execution"
        if ok
        else "; ".join(details),
        str(result_path),
    )


def probe_match_raw_profile_usage_check(ctx: RunContext) -> FinalAuditCheck:
    correction = ctx.manifest.desktoplut.get("probe_match_correction")
    plan_path = latest_path_from_stage(ctx, "raw-mhc", "plan")
    payload, error = load_json_path(plan_path)
    if payload is None:
        return FinalAuditCheck(
            "probe_match_used_by_raw_profile",
            False,
            f"could not read raw-MHC plan while verifying probe-match correction usage: {error}",
            str(plan_path) if plan_path else None,
        )
    artifacts = payload.get("artifacts")
    plan_correction = artifacts.get("correction") if isinstance(artifacts, dict) else None
    command_argv = payload.get("command_argv")
    dispread = command_argv[1] if isinstance(command_argv, list) and len(command_argv) > 1 else None
    dispread_uses_correction = (
        isinstance(correction, str)
        and isinstance(dispread, list)
        and "-X" in [str(item) for item in dispread]
        and any(same_resolved_path(ctx, str(item), correction) for item in dispread)
    )
    ok = isinstance(correction, str) and plan_correction == correction and dispread_uses_correction
    return FinalAuditCheck(
        "probe_match_used_by_raw_profile",
        ok,
        "raw-MHC profile plan used the requested probe-match correction via dispread -X"
        if ok
        else "raw-MHC profile plan did not record the requested correction and dispread -X argument",
        str(plan_path),
    )


def pipeline_evidence_check(ctx: RunContext) -> FinalAuditCheck:
    path = latest_path_from_stage(ctx, "pipeline_evidence", "artifact")
    payload, error = load_json_path(path)
    if payload is None:
        return FinalAuditCheck(
            "pipeline_evidence",
            False,
            f"could not read scriptable pipeline evidence: {error}",
            str(path) if path else None,
        )
    colourspace_refs = payload.get("colourspace_stage_references")
    colourspace_ref_count = len(colourspace_refs) if isinstance(colourspace_refs, list) else 0
    fingerprints = payload.get("missing_tool_fingerprints")
    fingerprints_recorded = isinstance(fingerprints, list)
    fingerprint_count = len(fingerprints) if fingerprints_recorded else 0
    ok = (
        payload.get("ok") is True
        and payload.get("tool_evidence_source") == "tool_preflight"
        and payload.get("colourspace_required") is False
        and payload.get("contained_tools_ready") is True
        and payload.get("contained_paths_ready") is True
        and fingerprints_recorded
        and fingerprint_count == 0
        and colourspace_ref_count == 0
    )
    details = []
    if payload.get("ok") is not True:
        details.append("pipeline evidence is not ok")
    if payload.get("tool_evidence_source") != "tool_preflight":
        details.append("pipeline evidence did not use run-local tool preflight")
    if payload.get("colourspace_required") is not False:
        details.append("ColourSpace is marked required")
    if payload.get("contained_tools_ready") is not True:
        details.append("contained tools are not ready")
    if payload.get("contained_paths_ready") is not True:
        details.append("contained tool paths are not ready")
    if not fingerprints_recorded:
        details.append("tool fingerprints are not recorded")
    if fingerprint_count:
        details.append(f"{fingerprint_count} tool fingerprint(s) missing")
    if colourspace_ref_count:
        details.append(f"{colourspace_ref_count} ColourSpace stage reference(s) found")
    return FinalAuditCheck(
        "pipeline_evidence",
        ok,
        "scriptable DLC/Argyll pipeline evidence is present"
        if ok
        else "; ".join(details),
        str(path),
    )


def adaptive_drift_config(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("adaptive_drift")
    return payload if isinstance(payload, dict) and payload.get("enabled") is True else {}


def adaptive_drift_targets(ctx: RunContext) -> list[str]:
    config = adaptive_drift_config(ctx)
    if not config:
        return []
    stages = config.get("stages")
    values = [str(stage) for stage in stages] if isinstance(stages, list) and stages else ["mhc-verification", "post-mhc", "3dlut-verification"]
    expanded: list[str] = []
    for stage in values:
        if stage == "verification":
            expanded.extend(["mhc-verification", "3dlut-verification"])
        else:
            expanded.append(stage)
    allowed = {"raw-mhc", "mhc-verification", "post-mhc", "3dlut-verification"}
    return [stage for stage in dict.fromkeys(expanded) if stage in allowed]


def stage_iterations(ctx: RunContext, stage: str) -> list[int]:
    iterations = []
    for entry in stage_entries(ctx, stage):
        value = entry.get("iteration")
        if isinstance(value, int):
            iterations.append(value)
        elif isinstance(value, str) and value.isdigit():
            iterations.append(int(value))
    return sorted(set(iterations))


def coerce_int(value: Any, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def latest_target_stage_path(ctx: RunContext, stage: str, key: str, *, target_stage: str, iteration: int) -> Path | None:
    for entry in reversed(stage_entries(ctx, stage)):
        if entry.get("target_stage") != target_stage:
            continue
        value = entry.get("iteration")
        entry_iteration = value if isinstance(value, int) else int(value) if isinstance(value, str) and value.isdigit() else None
        if entry_iteration != iteration:
            continue
        item = entry.get(key)
        if isinstance(item, str):
            return resolve_run_path(ctx, item)
    return None


def adaptive_drift_artifacts_check(ctx: RunContext) -> FinalAuditCheck:
    targets = adaptive_drift_targets(ctx)
    if not targets:
        return FinalAuditCheck("adaptive_drift", True, "adaptive drift was not requested", "manifest.desktoplut.adaptive_drift")
    missing: list[str] = []
    checked = 0
    for target in targets:
        iterations = stage_iterations(ctx, target)
        if not iterations:
            missing.append(f"{target}: no profiled iteration")
            continue
        for iteration in iterations:
            plan_path = latest_target_stage_path(ctx, "adaptive_drift", "plan", target_stage=target, iteration=iteration)
            plan, plan_error = load_json_path(plan_path)
            if plan is None:
                missing.append(f"{target} iter {iteration}: missing/read-failed drift plan ({plan_error})")
            elif plan.get("stage") != target or coerce_int(plan.get("iteration"), -1) != iteration or not isinstance(plan.get("patches"), list) or not plan["patches"]:
                missing.append(f"{target} iter {iteration}: drift plan content mismatch")

            sequence_path = latest_target_stage_path(ctx, "patch_sequence", "sequence", target_stage=target, iteration=iteration)
            sequence, sequence_error = load_json_path(sequence_path)
            if sequence is None:
                missing.append(f"{target} iter {iteration}: missing/read-failed drift patch sequence ({sequence_error})")
            elif sequence.get("kind") != "drift" or not isinstance(sequence.get("steps"), list) or not sequence["steps"]:
                missing.append(f"{target} iter {iteration}: drift patch sequence content mismatch")
            checked += 1
    ok = not missing
    return FinalAuditCheck(
        "adaptive_drift",
        ok,
        f"adaptive drift artifacts cover {checked} profiled stage iteration(s)" if ok else "; ".join(missing),
        "manifest.desktoplut.adaptive_drift",
    )


def final_report_content_check(ctx: RunContext) -> FinalAuditCheck:
    path = latest_path_from_stage(ctx, "final_report", "artifact")
    if path is None or not path.exists():
        return FinalAuditCheck("final_report_content", False, "missing final HTML report", str(path) if path else None)
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as exc:
        return FinalAuditCheck("final_report_content", False, f"could not read final HTML report: {exc}", str(path))
    required_fragments = {
        "DesktopLUT Calibrator": "product heading",
        "Executive Summary": "executive summary section",
        "Calibration Evidence": "calibration evidence summary",
        "System Evidence": "system evidence summary",
        "Toolchain Evidence": "toolchain evidence section",
        "Automation Provenance": "automation provenance section",
        "Completion Proof": "completion proof section",
        "Current Audit Revalidated": "finalization current-audit revalidation evidence",
        "Simulation Boundary": "simulation/live boundary evidence",
        "Raw MHC Profile Lineage": "raw MHC profile provenance",
        "Post MHC Profile Lineage": "post-MHC profile provenance",
        "MHC Candidate Lineage": "MHC candidate provenance",
        "3D LUT Build Lineage": "3D LUT build provenance",
        "Quality Policy": "quality policy section",
        "DesktopLUT Final State": "DesktopLUT final-state section",
        "Windows Color State": "Windows color-state section",
        "Iteration History": "iteration history section",
        "Artifact Index": "artifact index section",
        "SHA-256": "artifact hash column",
        "ColourSpace Required": "ColourSpace replacement evidence",
        html_escape(ctx.manifest.name): "current run name",
        html_escape(ctx.manifest.mode): "current run mode",
    }
    if ctx.manifest.display:
        required_fragments[html_escape(ctx.manifest.display)] = "current display name"
    if adaptive_drift_config(ctx):
        required_fragments["Adaptive Drift"] = "adaptive drift report section"
    missing = [label for fragment, label in required_fragments.items() if fragment and fragment not in html]
    ok = not missing
    return FinalAuditCheck(
        "final_report_content",
        ok,
        "final HTML report contains the current run identity, evidence summaries, loop history, and artifact hashes"
        if ok
        else "final HTML report is missing: " + ", ".join(missing),
        str(path),
    )


def loop_status_check(ctx: RunContext) -> FinalAuditCheck:
    payload = ctx.manifest.desktoplut.get("loop_status")
    path = None
    if isinstance(payload, dict):
        artifact = payload.get("artifact")
        path = resolve_run_path(ctx, artifact) if isinstance(artifact, str) else None
    if not isinstance(payload, dict):
        path = ctx.root / "reports" / "loop_status.json"
        payload, error = load_json_path(path)
        if payload is None:
            return FinalAuditCheck("loop_status", False, f"could not read loop status: {error}", str(path))
    phases = payload.get("phases")
    mhc = phases.get("mhc") if isinstance(phases, dict) else None
    lut3d = phases.get("3dlut") if isinstance(phases, dict) else None
    mhc_stopped = isinstance(mhc, dict) and mhc.get("status") == "stopped"
    lut3d_stopped = isinstance(lut3d, dict) and lut3d.get("status") == "stopped"
    ok = payload.get("ok") is True and mhc_stopped and lut3d_stopped
    return FinalAuditCheck(
        "loop_status",
        ok,
        "MHC and 3D LUT loop status both stopped"
        if ok
        else f"loop status is not stopped for both phases: mhc={mhc.get('status') if isinstance(mhc, dict) else 'missing'}, 3dlut={lut3d.get('status') if isinstance(lut3d, dict) else 'missing'}",
        str(path) if path else "manifest.desktoplut.loop_status",
    )


def quality_policy_check(ctx: RunContext) -> FinalAuditCheck:
    coverage = quality_policy_coverage(ctx.manifest.desktoplut.get("quality_policy"))
    return FinalAuditCheck(
        "quality_policy",
        bool(coverage["ok"]),
        "quality policy covers MHC and 3D LUT loop decisions"
        if coverage["ok"]
        else "missing " + ", ".join(str(item) for item in coverage["missing"]),
        "manifest.desktoplut.quality_policy",
    )


def tool_preflight_check(ctx: RunContext) -> FinalAuditCheck:
    path = latest_path_from_stage(ctx, "tool_preflight", "artifact") or ctx.root / "preflight" / "tool_preflight.json"
    payload, error = load_json_path(path)
    if payload is None:
        return FinalAuditCheck("tool_preflight", False, f"could not read tool preflight evidence: {error}", str(path))
    tools = payload.get("tools")
    missing_fingerprints = []
    missing_required = []
    if not isinstance(tools, dict):
        missing_required = list(REQUIRED_TOOL_NAMES)
    else:
        for name in REQUIRED_TOOL_NAMES:
            record = tools.get(name)
            if not isinstance(record, dict) or record.get("ok") is not True:
                missing_required.append(name)
            elif not isinstance(record.get("sha256"), str) or len(str(record.get("sha256"))) != 64:
                missing_fingerprints.append(name)
    vendor_manifest_ready = payload.get("vendor_manifest_ready") is True
    simulated = simulated_execution_mode(ctx)
    ok = (
        payload.get("required_ready") is True
        and payload.get("contained_ready") is True
        and payload.get("contained_paths_ready") is True
        and not missing_required
        and not missing_fingerprints
        and (vendor_manifest_ready or simulated)
    )
    details = []
    if payload.get("required_ready") is not True:
        details.append("required tools are not ready")
    if payload.get("contained_ready") is not True:
        details.append("contained tools are not ready")
    if payload.get("contained_paths_ready") is not True:
        details.append("contained tool paths are not under the expected third_party root")
    if missing_required:
        details.append("missing required tool records: " + ", ".join(missing_required))
    if missing_fingerprints:
        details.append("missing tool fingerprints: " + ", ".join(missing_fingerprints))
    if not vendor_manifest_ready and not simulated:
        details.append("vendor manifest is missing or not fingerprinted")
    if not vendor_manifest_ready and simulated:
        details.append("vendor manifest is missing or not fingerprinted; allowed for simulated run")
    return FinalAuditCheck(
        "tool_preflight",
        ok,
        "run-local tool preflight is ready with required fingerprints"
        if ok and vendor_manifest_ready
        else (
            "run-local tool preflight is ready with required fingerprints; vendor manifest missing allowed for simulated run"
            if ok
            else "; ".join(details)
        ),
        str(path),
    )


def build_final_audit(ctx: RunContext, *, output: Path | None = None) -> FinalAuditResult:
    artifacts = scan_artifacts(ctx.root)
    output = output or ctx.root / "reports" / "final_audit.json"
    checks = [
        FinalAuditCheck(
            name="colorimeter_placed",
            ok="colorimeter_placed" in ctx.manifest.human_actions,
            detail=(
                "colorimeter placement was acknowledged"
                if "colorimeter_placed" in ctx.manifest.human_actions
                else "colorimeter placement acknowledgement is missing"
            ),
            evidence="manifest.human_actions.colorimeter_placed",
        ),
        calibration_mode_check(ctx),
        path_check("raw_mhc_measurement", latest_path_from_stage(ctx, "raw-mhc", "ti3"), "raw-MHC TI3 measurement"),
        profile_measurement_lineage_check(ctx, "raw-mhc", ("ti1", "ti3", "icc")),
        applied_stage_check(ctx, "apply_mhc_baseline", "MHC candidate"),
        mhc_candidate_lineage_check(ctx),
        decision_stop_check(ctx, "mhc"),
        path_check("post_mhc_profile", latest_path_from_stage(ctx, "post-mhc", "icc"), "post-MHC display ICC"),
        profile_measurement_lineage_check(ctx, "post-mhc", ("ti1", "ti3", "icc")),
        applied_stage_check(ctx, "apply_3dlut", "3D LUT"),
        lut_build_lineage_check(ctx),
        path_check("3dlut_verification_metrics", latest_path_from_stage(ctx, "3dlut_metrics", "metrics"), "3D LUT verification metrics"),
        lut_integrity_check(ctx),
        decision_stop_check(ctx, "3dlut"),
        loop_status_check(ctx),
        quality_policy_check(ctx),
        tool_preflight_check(ctx),
        simulation_boundary_check(ctx),
        adaptive_drift_artifacts_check(ctx),
        path_check("desktoplut_final_state", latest_path_from_stage(ctx, "desktoplut_state_capture", "artifact"), "DesktopLUT final state capture"),
        desktoplut_final_state_content_check(ctx),
        path_check("windows_color_state", latest_path_from_stage(ctx, "windows_color_state_capture", "artifact"), "Windows color-state capture"),
        windows_color_state_content_check(ctx),
        pipeline_evidence_check(ctx),
        path_check("final_report", latest_path_from_stage(ctx, "final_report", "artifact"), "final HTML report"),
        final_report_content_check(ctx),
        FinalAuditCheck(
            name="artifact_index",
            ok=bool(artifacts),
            detail=f"{len(artifacts)} artifact(s) are available for hashing" if artifacts else "no artifacts available for hashing",
        ),
    ]
    if probe_match_request(ctx) is not None:
        checks.extend(
            [
                probe_match_correction_check(ctx),
                probe_match_correction_lineage_check(ctx),
                probe_match_raw_profile_usage_check(ctx),
            ]
        )
    ok = all(check.ok for check in checks)
    return FinalAuditResult(ok=ok, run=str(ctx.root), checks=checks, artifact_count=len(artifacts), audit_path=str(output))


def write_final_audit(ctx: RunContext, *, output: Path | None = None) -> FinalAuditResult:
    result = build_final_audit(ctx, output=output)
    audit_path = Path(result.audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "final_audit",
            "status": "passed" if result.ok else "failed",
            "artifact": str(audit_path),
            "checks": len(result.checks),
        }
    )
    if result.ok:
        ctx.manifest.status = "audited"
    ctx.save()
    ctx.log(f"Final audit {'passed' if result.ok else 'failed'}: {audit_path}")
    EventWriter(ctx.events_path).write(
        "INFO" if result.ok else "ERROR",
        "final_audit",
        "final_audit_written",
        ok=result.ok,
        artifact=str(audit_path),
        checks=len(result.checks),
    )
    return result

