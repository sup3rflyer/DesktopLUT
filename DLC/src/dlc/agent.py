"""Agent-facing next-action recommendations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .calibration_mode import calibration_mode_evidence
from .human_actions import has_human_action
from .live_setup import resolve_live_meter_port
from .runs import RunContext
from .tools import REQUIRED_TOOL_NAMES

DRIFT_STAGE_ACTIONS = {
    "raw-mhc": ("plan_raw_mhc_drift", "plan_raw_mhc_drift_sequence"),
    "mhc-verification": ("plan_mhc_verification_drift", "plan_mhc_verification_drift_sequence"),
    "post-mhc": ("plan_post_mhc_drift", "plan_post_mhc_drift_sequence"),
    "3dlut-verification": ("plan_3dlut_verification_drift", "plan_3dlut_verification_drift_sequence"),
}


@dataclass(frozen=True)
class NextAction:
    status: str
    action: str
    reason: str
    command: str | None = None
    stage: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _entry_iteration(entry: dict[str, object]) -> int | None:
    value = entry.get("iteration")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _stage_entries(ctx: RunContext, stage: str, iteration: int | None = None) -> list[dict[str, object]]:
    entries = [entry for entry in ctx.manifest.stages if entry.get("stage") == stage]
    if iteration is None:
        return entries
    return [entry for entry in entries if _entry_iteration(entry) == iteration]


def _latest_plan(ctx: RunContext, stage: str, iteration: int | None = None) -> str | None:
    for entry in reversed(_stage_entries(ctx, stage, iteration)):
        plan = entry.get("plan")
        if isinstance(plan, str):
            return plan
    return None


def _latest_drift_plan(ctx: RunContext, target_stage: str, iteration: int) -> str | None:
    for entry in reversed(_stage_entries(ctx, "adaptive_drift", iteration)):
        if entry.get("target_stage") != target_stage:
            continue
        plan = entry.get("plan")
        if isinstance(plan, str):
            return plan
    return None


def _has_drift_sequence(ctx: RunContext, target_stage: str, iteration: int) -> bool:
    return any(
        entry.get("target_stage") == target_stage and entry.get("kind") == "drift"
        for entry in _stage_entries(ctx, "patch_sequence", iteration)
    )


def _latest_metrics(ctx: RunContext, phase: str, iteration: int | None = None) -> str | None:
    for entry in reversed(_stage_entries(ctx, f"{phase}_metrics", iteration)):
        metrics = entry.get("metrics")
        if isinstance(metrics, str):
            return metrics
    return None


def _latest_lut_integrity(ctx: RunContext, phase: str, iteration: int | None = None) -> str | None:
    for entry in reversed(_stage_entries(ctx, f"{phase}_lut_integrity", iteration)):
        integrity = entry.get("integrity")
        if isinstance(integrity, str):
            return integrity
    return None


def _latest_3dlut_cube(ctx: RunContext, iteration: int | None = None) -> str | None:
    for entry in reversed(ctx.manifest.stages):
        if iteration is not None and _entry_iteration(entry) != iteration:
            continue
        cube = entry.get("cube")
        if isinstance(cube, str):
            return cube
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("cube"), str):
            return str(artifacts["cube"])
    return None


def _latest_artifact(ctx: RunContext, stage: str, key: str, iteration: int | None = None) -> str | None:
    for entry in reversed(_stage_entries(ctx, stage, iteration)):
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get(key), str):
            return str(artifacts[key])
    return None


def _latest_candidate(ctx: RunContext, iteration: int | None = None) -> str | None:
    for entry in reversed(_stage_entries(ctx, "build_mhc_baseline", iteration)):
        candidate = entry.get("candidate")
        if isinstance(candidate, str):
            return candidate
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("candidate"), str):
            return str(artifacts["candidate"])
    return None


def _has_completed_execution(ctx: RunContext, stage: str, iteration: int | None = None) -> bool:
    return any(entry.get("status") == "completed" for entry in _stage_entries(ctx, stage, iteration))


def _has_any_execution(ctx: RunContext, stage: str, iteration: int | None = None) -> bool:
    return any(
        str(entry.get("status", "")).startswith("execute_") or entry.get("status") == "completed"
        for entry in _stage_entries(ctx, stage, iteration)
    )


def _latest_decision(ctx: RunContext, phase: str) -> dict[str, Any] | None:
    for entry in reversed(_stage_entries(ctx, f"{phase}_decision")):
        payload: dict[str, Any] = dict(entry)
        decision_path = entry.get("decision")
        if isinstance(decision_path, str):
            path = Path(decision_path)
            try:
                if path.exists():
                    payload.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return payload
    return None


def _latest_decision_status(ctx: RunContext, phase: str) -> str | None:
    decision = _latest_decision(ctx, phase)
    if decision:
        status = decision.get("decision") or decision.get("status")
        if status in {"continue", "stop"}:
            return str(status)
        manifest_status = decision.get("status")
        return manifest_status if isinstance(manifest_status, str) else None
    return None


def _latest_decision_iteration(ctx: RunContext, phase: str) -> int | None:
    decision = _latest_decision(ctx, phase)
    if decision:
        value = decision.get("iteration")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _latest_decision_next_params(ctx: RunContext, phase: str) -> dict[str, Any]:
    decision = _latest_decision(ctx, phase) or {}
    params = decision.get("next_params")
    return params if isinstance(params, dict) else {}


def _stage_has_any_for_iteration(ctx: RunContext, stage: str, iteration: int) -> bool:
    return bool(_stage_entries(ctx, stage, iteration))


def _has_passed_final_audit(ctx: RunContext) -> bool:
    return any(entry.get("stage") == "final_audit" and entry.get("status") == "passed" for entry in _stage_entries(ctx, "final_audit"))


def _has_finalized_run(ctx: RunContext) -> bool:
    return any(entry.get("stage") == "finalization" and entry.get("status") == "finalized" for entry in _stage_entries(ctx, "finalization"))


def _has_desktoplut_state_capture(ctx: RunContext, label: str) -> bool:
    return any(
        entry.get("stage") == "desktoplut_state_capture" and entry.get("label") == label and entry.get("status") == "captured"
        for entry in _stage_entries(ctx, "desktoplut_state_capture")
    )


def _has_windows_color_state_capture(ctx: RunContext, label: str) -> bool:
    return any(
        entry.get("stage") == "windows_color_state_capture" and entry.get("label") == label and entry.get("status") == "captured"
        for entry in _stage_entries(ctx, "windows_color_state_capture")
    )


def _has_pipeline_evidence(ctx: RunContext) -> bool:
    def evidence_ready(payload: dict[str, Any]) -> bool:
        fingerprints = payload.get("missing_tool_fingerprints")
        refs = payload.get("colourspace_stage_references")
        missing_required = payload.get("missing_required_tools")
        missing_contained = payload.get("missing_contained_tools")
        return (
            payload.get("ok") is True
            and payload.get("tool_evidence_source") == "tool_preflight"
            and payload.get("colourspace_required") is False
            and payload.get("contained_tools_ready") is True
            and payload.get("contained_paths_ready") is True
            and isinstance(fingerprints, list)
            and len(fingerprints) == 0
            and isinstance(refs, list)
            and len(refs) == 0
            and isinstance(missing_required, list)
            and len(missing_required) == 0
            and isinstance(missing_contained, list)
            and len(missing_contained) == 0
        )

    def read_evidence(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def resolve_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ctx.root / path

    payload = ctx.manifest.desktoplut.get("pipeline_evidence")
    if isinstance(payload, dict):
        artifact = payload.get("artifact")
        if isinstance(artifact, str):
            artifact_payload = read_evidence(resolve_path(artifact))
            if artifact_payload is not None:
                return evidence_ready(artifact_payload)
        return False
    for entry in reversed(_stage_entries(ctx, "pipeline_evidence")):
        if entry.get("status") != "passed":
            continue
        artifact = entry.get("artifact")
        if isinstance(artifact, str):
            payload = read_evidence(resolve_path(artifact))
            if payload is not None and evidence_ready(payload):
                return True
    return False


def _has_tool_preflight(ctx: RunContext) -> bool:
    def preflight_ready(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        if (
            payload.get("required_ready") is not True
            or payload.get("contained_ready") is not True
            or payload.get("contained_paths_ready") is not True
        ):
            return False
        tools = payload.get("tools")
        if not isinstance(tools, dict):
            return False
        for name in REQUIRED_TOOL_NAMES:
            record = tools.get(name)
            if not isinstance(record, dict) or record.get("ok") is not True:
                return False
            fingerprint = record.get("sha256")
            if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                return False
        return True

    for entry in reversed(_stage_entries(ctx, "tool_preflight")):
        if entry.get("status") == "passed":
            artifact = entry.get("artifact")
            if isinstance(artifact, str):
                path = Path(artifact)
                if not path.is_absolute():
                    path = ctx.root / path
                if preflight_ready(path):
                    return True
    return preflight_ready(ctx.root / "preflight" / "tool_preflight.json")


def _has_passed_desktoplut_contract(ctx: RunContext) -> bool:
    return any(entry.get("stage") == "desktoplut_contract_check" and entry.get("status") == "passed" for entry in _stage_entries(ctx, "desktoplut_contract_check"))


def _probe_match_request(ctx: RunContext) -> dict[str, Any] | None:
    request = ctx.manifest.desktoplut.get("probe_match_request")
    if isinstance(request, dict) and request.get("enabled") is True:
        return request
    return None


def _probe_match_completed(ctx: RunContext) -> bool:
    if isinstance(ctx.manifest.desktoplut.get("probe_match_correction"), str):
        return True
    return any(entry.get("stage") == "probe_match" and entry.get("status") == "completed" for entry in _stage_entries(ctx, "probe_match"))


def _probe_match_plan_command(ctx: RunContext, request: dict[str, Any]) -> str:
    option_map = {
        "kind": "--kind",
        "display_tech": "--display-tech",
        "display_index": "--display-index",
        "patch_window": "--patch-window",
        "colorimeter_display_type": "--colorimeter-display-type",
        "spectro_display_type": "--spectro-display-type",
        "observer": "--observer",
        "description": "--description",
        "steps": "--steps",
    }
    parts = [f"dlc probe-match-plan --run {ctx.root}"]
    for key, option in option_map.items():
        value = request.get(key)
        if value is not None:
            parts.append(f"{option} {value}")
    if request.get("high_res"):
        parts.append("--high-res")
    return " ".join(parts)


def _format_optional_args(params: dict[str, Any], mapping: dict[str, str]) -> str:
    parts: list[str] = []
    for key, option in mapping.items():
        value = params.get(key)
        if value is not None:
            parts.append(f"{option} {value}")
    return (" " + " ".join(parts)) if parts else ""


def _adaptive_drift_config(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("adaptive_drift")
    return payload if isinstance(payload, dict) and payload.get("enabled") is True else {}


def _adaptive_drift_enabled_for(ctx: RunContext, target_stage: str) -> bool:
    config = _adaptive_drift_config(ctx)
    if not config:
        return False
    stages = config.get("stages")
    if not isinstance(stages, list) or not stages:
        return target_stage in {"mhc-verification", "post-mhc", "3dlut-verification"}
    normalized = {str(stage) for stage in stages}
    return target_stage in normalized or (target_stage.endswith("-verification") and "verification" in normalized)


def _drift_command_options(ctx: RunContext) -> str:
    config = _adaptive_drift_config(ctx)
    parts: list[str] = []
    if isinstance(config.get("coldest_channel"), str):
        parts.append(f"--coldest-channel {config['coldest_channel']}")
    if isinstance(config.get("gray_levels"), list) and config["gray_levels"]:
        parts.append("--gray-levels " + ",".join(str(level) for level in config["gray_levels"]))
    mapping = {
        "bias": "--bias",
        "delta_threshold": "--delta-threshold",
        "max_repeats": "--max-repeats",
        "settle_required": "--settle-required",
    }
    for key, option in mapping.items():
        if config.get(key) is not None:
            parts.append(f"{option} {config[key]}")
    return (" " + " ".join(parts)) if parts else ""


def _adaptive_drift_action(ctx: RunContext, target_stage: str, iteration: int) -> NextAction | None:
    if not _adaptive_drift_enabled_for(ctx, target_stage):
        return None
    plan_action, sequence_action = DRIFT_STAGE_ACTIONS[target_stage]
    plan = _latest_drift_plan(ctx, target_stage, iteration)
    if plan is None:
        return NextAction(
            status="ready",
            action=plan_action,
            reason=f"Adaptive drift is enabled; plan gray-balance drift probes before {target_stage} iteration {iteration}.",
            command=f"dlc drift-plan --run {ctx.root} --stage {target_stage} --iteration {iteration}{_drift_command_options(ctx)}",
            stage="adaptive_drift",
        )
    if not _has_drift_sequence(ctx, target_stage, iteration):
        return NextAction(
            status="ready",
            action=sequence_action,
            reason=f"Adaptive drift plan exists; convert it to a DLC patch sequence before {target_stage} iteration {iteration}.",
            command=f"dlc patch-sequence --run {ctx.root} --kind drift --stage {target_stage} --iteration {iteration} --drift-plan {Path(plan)}",
            stage="patch_sequence",
        )
    return None


def _continue_mhc_action(ctx: RunContext, *, port: int | None) -> NextAction:
    previous_iteration = _latest_decision_iteration(ctx, "mhc") or 1
    iteration = previous_iteration + 1
    source = _latest_artifact(ctx, "mhc-verification", "ti3", previous_iteration)
    params = _latest_decision_next_params(ctx, "mhc")
    lut_size = int(params.get("lut_size", 4096))
    gamma = float(params.get("gamma", 2.2))

    if not _stage_has_any_for_iteration(ctx, "build_mhc_baseline", iteration):
        if source is None:
            return NextAction(
                status="needs_repair",
                action="locate_mhc_verification_ti3",
                reason=f"MHC iteration {iteration} needs the previous verification TI3 as its source.",
                command=None,
                stage="mhc_verification_loop",
            )
        return NextAction(
            status="ready",
            action="build_mhc_iteration",
            reason=f"MHC decision requested another pass; build candidate iteration {iteration} from verification iteration {previous_iteration}.",
            command=(
                f"dlc mhc-build --run {ctx.root} --iteration {iteration} --source-ti3 {Path(source)} "
                f"--lut-size {lut_size} --gamma {gamma}"
            ),
            stage="build_mhc_baseline",
        )

    if not _stage_has_any_for_iteration(ctx, "apply_mhc_baseline", iteration):
        candidate = _latest_candidate(ctx, iteration)
        if candidate is None:
            return NextAction(
                status="needs_repair",
                action="locate_mhc_candidate",
                reason=f"MHC candidate iteration {iteration} was recorded without a candidate artifact.",
                command=None,
                stage="apply_mhc_baseline",
            )
        return NextAction(
            status="ready",
            action="apply_mhc_iteration",
            reason=f"MHC candidate iteration {iteration} should be applied before re-verification.",
            command=f"dlc mhc-apply --run {ctx.root} --candidate {Path(candidate)}",
            stage="apply_mhc_baseline",
        )

    if not _stage_has_any_for_iteration(ctx, "mhc-verification", iteration):
        drift_action = _adaptive_drift_action(ctx, "mhc-verification", iteration)
        if drift_action is not None:
            return drift_action
        if port is None:
            return NextAction(
                status="needs_input",
                action="select_meter_port",
                reason=f"MHC verification iteration {iteration} needs an Argyll instrument port.",
                command="dlc instruments",
                stage="mhc-verification",
            )
        return NextAction(
            status="ready",
            action="plan_mhc_verification_iteration",
            reason=f"MHC candidate iteration {iteration} is applied; plan its verification measurement.",
            command=f"dlc profile-plan --run {ctx.root} --stage mhc-verification --port {port} --iteration {iteration}",
            stage="mhc-verification",
        )

    if not _has_completed_execution(ctx, "mhc-verification", iteration):
        plan = _latest_plan(ctx, "mhc-verification", iteration)
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_mhc_verification_plan",
                reason=f"MHC verification iteration {iteration} exists without a plan path.",
                command=f"dlc profile-plan --run {ctx.root} --stage mhc-verification --port {port or 'PORT'} --iteration {iteration}",
                stage="mhc-verification",
            )
        return NextAction(
            status="ready",
            action="execute_mhc_verification_iteration",
            reason=f"MHC verification iteration {iteration} has not completed actual execution.",
            command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="mhc-verification",
        )

    metrics = _latest_metrics(ctx, "mhc", iteration)
    if metrics is None:
        source = _latest_artifact(ctx, "mhc-verification", "ti3", iteration)
        if source is None:
            return NextAction(
                status="needs_repair",
                action="locate_mhc_verification_ti3",
                reason=f"MHC verification iteration {iteration} completed but no TI3 artifact path is recorded.",
                command=None,
                stage="mhc_verification_loop",
            )
        return NextAction(
            status="ready",
            action="score_mhc_iteration",
            reason=f"MHC verification iteration {iteration} is complete; score it before the next loop decision.",
            command=f"dlc metrics --run {ctx.root} --phase mhc --iteration {iteration} --source-ti3 {Path(source)}",
            stage="mhc_verification_loop",
        )

    if not _stage_has_any_for_iteration(ctx, "mhc_decision", iteration):
        return NextAction(
            status="ready",
            action="decide_mhc_iteration",
            reason=f"MHC metrics iteration {iteration} exist but no loop decision record has been written.",
            command=f"dlc decide --run {ctx.root} --phase mhc --metrics-json {Path(metrics)}",
            stage="mhc_verification_loop",
        )
    return NextAction(
        status="needs_repair",
        action="refresh_mhc_decision_state",
        reason="The latest MHC decision was continue, but a newer decision record also exists; refresh the run state.",
        command=f"dlc next --run {ctx.root}",
        stage="mhc_verification_loop",
    )


def _continue_3dlut_action(ctx: RunContext, *, port: int | None) -> NextAction:
    previous_iteration = _latest_decision_iteration(ctx, "3dlut") or 1
    iteration = previous_iteration + 1
    params = _latest_decision_next_params(ctx, "3dlut")
    grid_size = int(params.get("grid_size", 33))
    quality = str(params.get("quality", "u"))
    intent = str(params.get("intent", "r"))
    eotf = str(params.get("eotf", "b"))
    patch_count = params.get("post_mhc_patch_count")
    patch_arg = f" --patch-count {int(patch_count)}" if isinstance(patch_count, int | float) else ""
    lut_args = _format_optional_args({"grid_size": grid_size, "quality": quality, "intent": intent, "eotf": eotf}, {
        "grid_size": "--grid-size",
        "quality": "--quality",
        "intent": "--intent",
        "eotf": "--eotf",
    })

    if not _stage_has_any_for_iteration(ctx, "post-mhc", iteration):
        drift_action = _adaptive_drift_action(ctx, "post-mhc", iteration)
        if drift_action is not None:
            return drift_action
        if port is None:
            return NextAction(
                status="needs_input",
                action="select_meter_port",
                reason=f"Post-MHC profiling iteration {iteration} needs an Argyll instrument port.",
                command="dlc instruments",
                stage="post-mhc",
            )
        return NextAction(
            status="ready",
            action="plan_post_mhc_iteration",
            reason=f"3D LUT decision requested another pass; re-profile the post-MHC display for iteration {iteration}.",
            command=f"dlc profile-plan --run {ctx.root} --stage post-mhc --port {port} --iteration {iteration}{patch_arg}",
            stage="post-mhc",
        )

    if not _has_completed_execution(ctx, "post-mhc", iteration):
        plan = _latest_plan(ctx, "post-mhc", iteration)
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_post_mhc_plan",
                reason=f"Post-MHC iteration {iteration} exists without a plan path.",
                command=f"dlc profile-plan --run {ctx.root} --stage post-mhc --port {port or 'PORT'} --iteration {iteration}{patch_arg}",
                stage="post-mhc",
            )
        return NextAction(
            status="ready",
            action="execute_post_mhc_iteration",
            reason=f"Post-MHC profiling iteration {iteration} has not completed actual execution.",
            command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="post-mhc",
        )

    if not _stage_has_any_for_iteration(ctx, "build_3dlut", iteration):
        return NextAction(
            status="ready",
            action="plan_3dlut_iteration",
            reason=f"Post-MHC profiling iteration {iteration} is complete; plan the tuned 3D LUT rebuild.",
            command=f"dlc 3dlut-plan --run {ctx.root} --iteration {iteration}{lut_args}",
            stage="build_3dlut",
        )

    if not _has_completed_execution(ctx, "build_3dlut", iteration):
        plan = _latest_plan(ctx, "build_3dlut", iteration)
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_3dlut_plan",
                reason=f"3D LUT build iteration {iteration} exists without a plan path.",
                command=f"dlc 3dlut-plan --run {ctx.root} --iteration {iteration}{lut_args}",
                stage="build_3dlut",
            )
        return NextAction(
            status="ready",
            action="execute_3dlut_iteration",
            reason=f"3D LUT build iteration {iteration} has not completed actual execution.",
            command=f"dlc 3dlut-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="build_3dlut",
        )

    if not _stage_has_any_for_iteration(ctx, "apply_3dlut", iteration):
        cube = _latest_3dlut_cube(ctx, iteration)
        if cube is None:
            return NextAction(
                status="needs_repair",
                action="locate_3dlut_cube",
                reason=f"3D LUT build iteration {iteration} completed but no cube path is recorded.",
                command=None,
                stage="apply_3dlut",
            )
        return NextAction(
            status="ready",
            action="apply_3dlut_iteration",
            reason=f"3D LUT iteration {iteration} should be applied before verification.",
            command=f"dlc 3dlut-apply --run {ctx.root} --cube {Path(cube)}",
            stage="apply_3dlut",
        )

    if not _stage_has_any_for_iteration(ctx, "3dlut-verification", iteration):
        drift_action = _adaptive_drift_action(ctx, "3dlut-verification", iteration)
        if drift_action is not None:
            return drift_action
        if port is None:
            return NextAction(
                status="needs_input",
                action="select_meter_port",
                reason=f"3D LUT verification iteration {iteration} needs an Argyll instrument port.",
                command="dlc instruments",
                stage="3dlut-verification",
            )
        return NextAction(
            status="ready",
            action="plan_3dlut_verification_iteration",
            reason=f"3D LUT iteration {iteration} is applied; plan final verification for that pass.",
            command=f"dlc profile-plan --run {ctx.root} --stage 3dlut-verification --port {port} --iteration {iteration}",
            stage="3dlut-verification",
        )

    if not _has_completed_execution(ctx, "3dlut-verification", iteration):
        plan = _latest_plan(ctx, "3dlut-verification", iteration)
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_3dlut_verification_plan",
                reason=f"3D LUT verification iteration {iteration} exists without a plan path.",
                command=f"dlc profile-plan --run {ctx.root} --stage 3dlut-verification --port {port or 'PORT'} --iteration {iteration}",
                stage="3dlut-verification",
            )
        return NextAction(
            status="ready",
            action="execute_3dlut_verification_iteration",
            reason=f"3D LUT verification iteration {iteration} has not completed actual execution.",
            command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="3dlut-verification",
        )

    metrics = _latest_metrics(ctx, "3dlut", iteration)
    if metrics is None:
        source = _latest_artifact(ctx, "3dlut-verification", "ti3", iteration)
        if source is None:
            return NextAction(
                status="needs_repair",
                action="locate_3dlut_verification_ti3",
                reason=f"3D LUT verification iteration {iteration} completed but no TI3 artifact path is recorded.",
                command=None,
                stage="3dlut_verification_loop",
            )
        return NextAction(
            status="ready",
            action="score_3dlut_iteration",
            reason=f"3D LUT verification iteration {iteration} is complete; score it before integrity and loop decisions.",
            command=f"dlc metrics --run {ctx.root} --phase 3dlut --iteration {iteration} --source-ti3 {Path(source)}",
            stage="3dlut_verification_loop",
        )

    integrity = _latest_lut_integrity(ctx, "3dlut", iteration)
    if integrity is None:
        cube = _latest_3dlut_cube(ctx, iteration)
        if cube is None:
            return NextAction(
                status="needs_repair",
                action="locate_3dlut_cube",
                reason=f"3D LUT metrics iteration {iteration} exist but no generated cube path is recorded.",
                command=None,
                stage="3dlut_verification_loop",
            )
        return NextAction(
            status="ready",
            action="check_3dlut_integrity",
            reason=f"3D LUT verification metrics iteration {iteration} exist; check cube integrity before deciding.",
            command=f"dlc 3dlut-check --run {ctx.root} --cube {Path(cube)} --iteration {iteration}",
            stage="3dlut_verification_loop",
        )

    if not _stage_has_any_for_iteration(ctx, "3dlut_decision", iteration):
        return NextAction(
            status="ready",
            action="decide_3dlut_iteration",
            reason=f"3D LUT metrics and cube integrity iteration {iteration} exist but no loop decision has been written.",
            command=f"dlc decide --run {ctx.root} --phase 3dlut --metrics-json {Path(metrics)} --lut-integrity-json {Path(integrity)}",
            stage="3dlut_verification_loop",
        )
    return NextAction(
        status="needs_repair",
        action="refresh_3dlut_decision_state",
        reason="The latest 3D LUT decision was continue, but a newer decision record also exists; refresh the run state.",
        command=f"dlc next --run {ctx.root}",
        stage="3dlut_verification_loop",
    )

def _calibration_mode_active(ctx: RunContext) -> bool:
    return bool(calibration_mode_evidence(ctx).get("ok"))


def recommend_next_action(ctx: RunContext, *, port: int | None = None) -> NextAction:
    port = resolve_live_meter_port(ctx, port)
    probe_match_request = _probe_match_request(ctx)
    probe_match_pending = bool(probe_match_request and not _probe_match_completed(ctx))
    if probe_match_pending and not has_human_action(ctx, "spectro_placed"):
        return NextAction(
            status="human_required",
            action="ack_spectro_placed",
            reason="Optional probe matching was requested, so the spectrometer must be placed before the agent starts ccxxmake.",
            command=f"dlc ack --run {ctx.root} --action spectro_placed --instrument \"ColorChecker Studio\"",
            stage="probe_match_setup",
        )

    if not has_human_action(ctx, "colorimeter_placed"):
        reason = "Unattended display measurement is gated until the colorimeter is physically placed."
        if probe_match_pending and str(probe_match_request.get("kind", "ccmx")) == "ccmx":
            reason = "CCMX probe matching was requested; acknowledge the colorimeter after spectrometer placement so ccxxmake can run."
        return NextAction(
            status="human_required",
            action="ack_colorimeter_placed",
            reason=reason,
            command=f"dlc ack --run {ctx.root} --action colorimeter_placed --instrument \"i1 Display Pro\"",
            stage="probe_match_setup" if probe_match_pending else "colorimeter_ready",
        )

    if not _has_passed_desktoplut_contract(ctx) and not _calibration_mode_active(ctx):
        return NextAction(
            status="ready",
            action="desktoplut_contract_check",
            reason="Before measurement, verify the DesktopLUT API supports the calibration/runtime/Windows-state commands DLC will rely on.",
            command=f"dlc desktoplut-contract-check --run {ctx.root} --mode {ctx.manifest.mode}",
            stage="desktoplut_contract_check",
        )

    if not _calibration_mode_active(ctx):
        return NextAction(
            status="ready",
            action="enter_calibration_mode",
            reason="DesktopLUT should install the dummy ICC and reset MHC/runtime correction layers before raw measurement.",
            command=f"dlc desktoplut-calibration-mode enter --run {ctx.root} --mode {ctx.manifest.mode}",
            stage="snapshot_desktoplut",
        )

    if probe_match_pending:
        if not _stage_entries(ctx, "probe_match"):
            return NextAction(
                status="ready",
                action="plan_probe_match",
                reason="Probe matching was requested and DesktopLUT is in calibration mode; plan the ccxxmake correction before profiling.",
                command=_probe_match_plan_command(ctx, probe_match_request),
                stage="probe_match",
            )
        plan = _latest_plan(ctx, "probe_match")
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_probe_match_plan",
                reason="A probe-match stage entry exists but no plan path was recorded.",
                command=_probe_match_plan_command(ctx, probe_match_request),
                stage="probe_match",
            )
        return NextAction(
            status="ready",
            action="execute_probe_match",
            reason="The probe-match plan exists but no completed correction has been recorded.",
            command=f"dlc probe-match-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="probe_match",
        )

    if not _stage_entries(ctx, "raw-mhc"):
        drift_action = _adaptive_drift_action(ctx, "raw-mhc", 1)
        if drift_action is not None:
            return drift_action
        if port is None:
            return NextAction(
                status="needs_input",
                action="select_meter_port",
                reason="The raw-MHC profile plan needs an Argyll instrument port.",
                command="dlc instruments",
                stage="raw-mhc",
            )
        return NextAction(
            status="ready",
            action="plan_raw_mhc",
            reason="Colorimeter placement is acknowledged and no raw-MHC measurement plan exists yet.",
            command=f"dlc profile-plan --run {ctx.root} --stage raw-mhc --port {port} --iteration 1",
            stage="raw-mhc",
        )

    if not _has_completed_execution(ctx, "raw-mhc"):
        plan = _latest_plan(ctx, "raw-mhc")
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_raw_mhc_plan",
                reason="A raw-MHC stage entry exists but no plan path was recorded.",
                command=f"dlc profile-plan --run {ctx.root} --stage raw-mhc --port {port or 'PORT'} --iteration 1",
                stage="raw-mhc",
            )
        suffix = "" if _has_any_execution(ctx, "raw-mhc") else " Start with a dry-run if command review is desired."
        return NextAction(
            status="ready",
            action="execute_raw_mhc",
            reason="The raw-MHC plan exists but has not completed actual execution." + suffix,
            command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="raw-mhc",
        )

    if not _stage_entries(ctx, "build_mhc_baseline"):
        return NextAction(
            status="ready",
            action="build_mhc_baseline",
            reason="Raw-MHC measurement execution is complete; build the first MHC candidate from the measured TI3.",
            command=f"dlc mhc-build --run {ctx.root} --iteration 1",
            stage="build_mhc_baseline",
        )

    if not _stage_entries(ctx, "apply_mhc_baseline"):
        return NextAction(
            status="ready",
            action="apply_mhc_baseline",
            reason="An MHC candidate exists and should be applied through the DesktopLUT API.",
            command=f"dlc mhc-apply --run {ctx.root}",
            stage="apply_mhc_baseline",
        )

    mhc_decision = _latest_decision_status(ctx, "mhc")
    if mhc_decision is None:
        metrics = _latest_metrics(ctx, "mhc")
        if metrics is not None:
            return NextAction(
                status="ready",
                action="decide_mhc_iteration",
                reason="MHC verification metrics exist but no loop decision record has been written.",
                command=f"dlc decide --run {ctx.root} --phase mhc --metrics-json {Path(metrics)}",
                stage="mhc_verification_loop",
            )
        if not _stage_entries(ctx, "mhc-verification"):
            drift_action = _adaptive_drift_action(ctx, "mhc-verification", 1)
            if drift_action is not None:
                return drift_action
            if port is None:
                return NextAction(
                    status="needs_input",
                    action="select_meter_port",
                    reason="The MHC verification measurement needs an Argyll instrument port.",
                    command="dlc instruments",
                    stage="mhc-verification",
                )
            return NextAction(
                status="ready",
                action="plan_mhc_verification",
                reason="MHC was applied; measure the corrected neutral/color state before deciding the MHC loop.",
                command=f"dlc profile-plan --run {ctx.root} --stage mhc-verification --port {port} --iteration 1",
                stage="mhc-verification",
            )
        if not _has_completed_execution(ctx, "mhc-verification"):
            plan = _latest_plan(ctx, "mhc-verification")
            if plan is None:
                return NextAction(
                    status="needs_repair",
                    action="regenerate_mhc_verification_plan",
                    reason="An MHC verification stage entry exists but no plan path was recorded.",
                    command=f"dlc profile-plan --run {ctx.root} --stage mhc-verification --port {port or 'PORT'} --iteration 1",
                    stage="mhc-verification",
                )
            return NextAction(
                status="ready",
                action="execute_mhc_verification",
                reason="The MHC verification plan exists but has not completed actual execution.",
                command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
                stage="mhc-verification",
            )
        source = _latest_artifact(ctx, "mhc-verification", "ti3")
        if source is not None:
            return NextAction(
                status="ready",
                action="score_mhc_iteration",
                reason="MHC verification measurement is complete; score it before deciding whether to continue.",
                command=f"dlc metrics --run {ctx.root} --phase mhc --iteration 1 --source-ti3 {Path(source)}",
                stage="mhc_verification_loop",
            )
        return NextAction(
            status="needs_repair",
            action="locate_mhc_verification_ti3",
            reason="MHC verification completed but no TI3 artifact path is recorded.",
            command=None,
            stage="mhc_verification_loop",
        )

    if mhc_decision != "stop":
        return _continue_mhc_action(ctx, port=port)

    if not _stage_entries(ctx, "post-mhc"):
        drift_action = _adaptive_drift_action(ctx, "post-mhc", 1)
        if drift_action is not None:
            return drift_action
        if port is None:
            return NextAction(
                status="needs_input",
                action="select_meter_port",
                reason="The post-MHC profile plan for 3D LUT generation needs an Argyll instrument port.",
                command="dlc instruments",
                stage="post-mhc",
            )
        return NextAction(
            status="ready",
            action="plan_post_mhc",
            reason="MHC thresholds are satisfied; measure the post-MHC display state for 3D LUT generation.",
            command=f"dlc profile-plan --run {ctx.root} --stage post-mhc --port {port} --iteration 1",
            stage="post-mhc",
        )

    if not _has_completed_execution(ctx, "post-mhc"):
        plan = _latest_plan(ctx, "post-mhc")
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_post_mhc_plan",
                reason="A post-MHC stage entry exists but no plan path was recorded.",
                command=f"dlc profile-plan --run {ctx.root} --stage post-mhc --port {port or 'PORT'} --iteration 1",
                stage="post-mhc",
            )
        return NextAction(
            status="ready",
            action="execute_post_mhc",
            reason="The post-MHC plan exists but has not completed actual execution.",
            command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="post-mhc",
        )

    if not _stage_entries(ctx, "build_3dlut"):
        return NextAction(
            status="ready",
            action="plan_3dlut",
            reason="Post-MHC profiling is complete; plan the Argyll collink 3D LUT build.",
            command=f"dlc 3dlut-plan --run {ctx.root} --iteration 1",
            stage="build_3dlut",
        )

    if not _has_completed_execution(ctx, "build_3dlut"):
        plan = _latest_plan(ctx, "build_3dlut")
        if plan is None:
            return NextAction(
                status="needs_repair",
                action="regenerate_3dlut_plan",
                reason="A 3D LUT build stage entry exists but no plan path was recorded.",
                command=f"dlc 3dlut-plan --run {ctx.root} --iteration 1",
                stage="build_3dlut",
            )
        return NextAction(
            status="ready",
            action="execute_3dlut",
            reason="The 3D LUT build plan exists but has not completed actual execution.",
            command=f"dlc 3dlut-execute --run {ctx.root} --plan {Path(plan)} --execute",
            stage="build_3dlut",
        )

    if not _stage_entries(ctx, "apply_3dlut"):
        return NextAction(
            status="ready",
            action="apply_3dlut",
            reason="A 3D LUT build has completed; apply the generated cube through the DesktopLUT API.",
            command=f"dlc 3dlut-apply --run {ctx.root}",
            stage="apply_3dlut",
        )

    lut_decision = _latest_decision_status(ctx, "3dlut")
    if lut_decision is None:
        metrics = _latest_metrics(ctx, "3dlut")
        if metrics is None:
            if not _stage_entries(ctx, "3dlut-verification"):
                drift_action = _adaptive_drift_action(ctx, "3dlut-verification", 1)
                if drift_action is not None:
                    return drift_action
                if port is None:
                    return NextAction(
                        status="needs_input",
                        action="select_meter_port",
                        reason="The 3D LUT verification measurement needs an Argyll instrument port.",
                        command="dlc instruments",
                        stage="3dlut-verification",
                    )
                return NextAction(
                    status="ready",
                    action="plan_3dlut_verification",
                    reason="3D LUT was applied; measure the final corrected display state before loop decision.",
                    command=f"dlc profile-plan --run {ctx.root} --stage 3dlut-verification --port {port} --iteration 1",
                    stage="3dlut-verification",
                )
            if not _has_completed_execution(ctx, "3dlut-verification"):
                plan = _latest_plan(ctx, "3dlut-verification")
                if plan is None:
                    return NextAction(
                        status="needs_repair",
                        action="regenerate_3dlut_verification_plan",
                        reason="A 3D LUT verification stage entry exists but no plan path was recorded.",
                        command=f"dlc profile-plan --run {ctx.root} --stage 3dlut-verification --port {port or 'PORT'} --iteration 1",
                        stage="3dlut-verification",
                    )
                return NextAction(
                    status="ready",
                    action="execute_3dlut_verification",
                    reason="The 3D LUT verification plan exists but has not completed actual execution.",
                    command=f"dlc profile-execute --run {ctx.root} --plan {Path(plan)} --execute",
                    stage="3dlut-verification",
                )
            source = _latest_artifact(ctx, "3dlut-verification", "ti3")
            if source is not None:
                return NextAction(
                    status="ready",
                    action="score_3dlut_iteration",
                    reason="3D LUT verification measurement is complete; score it before integrity and loop decisions.",
                    command=f"dlc metrics --run {ctx.root} --phase 3dlut --iteration 1 --source-ti3 {Path(source)}",
                    stage="3dlut_verification_loop",
                )
            return NextAction(
                status="needs_repair",
                action="locate_3dlut_verification_ti3",
                reason="3D LUT verification completed but no TI3 artifact path is recorded.",
                command=None,
                stage="3dlut_verification_loop",
            )
        integrity = _latest_lut_integrity(ctx, "3dlut")
        if integrity is None:
            cube = _latest_3dlut_cube(ctx)
            if cube is None:
                return NextAction(
                    status="needs_repair",
                    action="locate_3dlut_cube",
                    reason="3D LUT metrics exist but no generated cube path is recorded for integrity analysis.",
                    command=None,
                    stage="3dlut_verification_loop",
                )
            return NextAction(
                status="ready",
                action="check_3dlut_integrity",
                reason="3D LUT verification metrics exist; check cube integrity before deciding whether to stop.",
                command=f"dlc 3dlut-check --run {ctx.root} --cube {Path(cube)} --iteration 1",
                stage="3dlut_verification_loop",
            )
        return NextAction(
            status="ready",
            action="decide_3dlut_iteration",
            reason="3D LUT verification metrics and cube integrity exist but no loop decision has been written.",
            command=f"dlc decide --run {ctx.root} --phase 3dlut --metrics-json {Path(metrics)} --lut-integrity-json {Path(integrity)}",
            stage="3dlut_verification_loop",
        )

    if lut_decision != "stop":
        return _continue_3dlut_action(ctx, port=port)

    if not _has_desktoplut_state_capture(ctx, "final"):
        return NextAction(
            status="ready",
            action="capture_final_desktoplut_state",
            reason="3D LUT decision is stop; capture DesktopLUT final runtime state before writing the report.",
            command=f"dlc desktoplut-state-capture --run {ctx.root} --label final",
            stage="desktoplut_state_capture",
        )

    if not _has_windows_color_state_capture(ctx, "final"):
        return NextAction(
            status="ready",
            action="capture_final_windows_color_state",
            reason="DesktopLUT final state is captured; capture Windows profile/gamma state before writing the report.",
            command=f"dlc windows-state-capture --run {ctx.root} --label final",
            stage="windows_color_state_capture",
        )

    if not _has_tool_preflight(ctx):
        return NextAction(
            status="ready",
            action="write_tool_preflight",
            reason="Final state is captured; refresh run-local tool preflight before deriving pipeline evidence.",
            command=f"dlc preflight --run {ctx.root}",
            stage="tool_preflight",
        )

    if not _has_pipeline_evidence(ctx):
        return NextAction(
            status="ready",
            action="write_pipeline_evidence",
            reason="Final state is captured; record that this run used the scriptable DLC/Argyll path instead of requiring ColourSpace.",
            command=f"dlc pipeline-evidence --run {ctx.root}",
            stage="pipeline_evidence",
        )

    if not _stage_entries(ctx, "final_report"):
        return NextAction(
            status="ready",
            action="write_report",
            reason="3D LUT decision is stop; write the current calibration report.",
            command=f"dlc report --run {ctx.root}",
            stage="final_report",
        )

    if not _has_passed_final_audit(ctx):
        return NextAction(
            status="ready",
            action="final_audit",
            reason="A report exists; write the final machine-readable audit before treating the run as complete.",
            command=f"dlc final-audit --run {ctx.root}",
            stage="final_audit",
        )

    if not _has_finalized_run(ctx):
        return NextAction(
            status="ready",
            action="finalize_run",
            reason="The final audit passed; accept the audited calibration as the finalized run result.",
            command=f"dlc finalize-run --run {ctx.root}",
            stage="finalization",
        )

    return NextAction(
        status="ready",
        action="complete",
        reason="A final report, passing final audit, and finalization record exist for the current automated slice.",
        command=None,
        stage="finalization",
    )

