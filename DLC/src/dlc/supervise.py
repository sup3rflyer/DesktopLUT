"""Bounded supervisor loop for agent-run calibration sessions."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import NextAction, recommend_next_action
from .dashboard import write_dashboard_html, write_readout_html
from .events import EventWriter
from .live_setup import resolve_live_meter_port
from .runs import RunContext, open_run
from .safety import blocked_reason_for_action

DRIFT_PLAN_ACTION_TARGETS = {
    "plan_raw_mhc_drift": "raw-mhc",
    "plan_mhc_verification_drift": "mhc-verification",
    "plan_post_mhc_drift": "post-mhc",
    "plan_3dlut_verification_drift": "3dlut-verification",
}
DRIFT_SEQUENCE_ACTION_TARGETS = {
    "plan_raw_mhc_drift_sequence": "raw-mhc",
    "plan_mhc_verification_drift_sequence": "mhc-verification",
    "plan_post_mhc_drift_sequence": "post-mhc",
    "plan_3dlut_verification_drift_sequence": "3dlut-verification",
}


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


@dataclass(frozen=True)
class SuperviseStep:
    index: int
    recommendation: dict[str, str | None]
    executable: bool
    executed: bool
    blocked_reason: str | None = None
    command_result: CommandResult | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.command_result is not None:
            payload["command_result"] = self.command_result.as_dict()
        return payload


@dataclass(frozen=True)
class StageRunResult:
    run: str
    recommendation: dict[str, str | None]
    expected_action: str | None
    executable: bool
    executed: bool
    blocked_reason: str | None = None
    command_result: CommandResult | None = None
    dashboard: str | None = None
    readout: str | None = None

    @property
    def ok(self) -> bool:
        if self.expected_action and self.recommendation.get("action") != self.expected_action:
            return False
        return self.command_result is None or self.command_result.ok

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.command_result is not None:
            payload["command_result"] = self.command_result.as_dict()
        payload["ok"] = self.ok
        return payload


@dataclass(frozen=True)
class SuperviseResult:
    run: str
    started: str
    finished: str
    execute_safe: bool
    allow_hardware: bool
    allow_live_desktoplut: bool
    allow_builds: bool
    mock_desktoplut: bool
    simulate_execution: bool
    stopped_reason: str
    steps: list[SuperviseStep] = field(default_factory=list)
    artifact: str | None = None
    dashboard: str | None = None
    readout: str | None = None
    complete: bool = False

    @property
    def ok(self) -> bool:
        return not any(step.command_result and not step.command_result.ok for step in self.steps)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.as_dict() for step in self.steps]
        payload["ok"] = self.ok
        return payload


def _stage_entries(ctx: RunContext, stage: str) -> list[dict[str, object]]:
    return [entry for entry in ctx.manifest.stages if entry.get("stage") == stage]


def _entry_iteration(entry: dict[str, object]) -> int | None:
    value = entry.get("iteration")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _stage_entries_for_iteration(ctx: RunContext, stage: str, iteration: int | None) -> list[dict[str, object]]:
    entries = _stage_entries(ctx, stage)
    if iteration is None:
        return entries
    return [entry for entry in entries if _entry_iteration(entry) == iteration]


def _latest_iteration(ctx: RunContext, stage: str) -> int | None:
    iterations = [_entry_iteration(entry) for entry in _stage_entries(ctx, stage)]
    known = [iteration for iteration in iterations if iteration is not None]
    return max(known) if known else None


def _latest_completed_iteration(ctx: RunContext, stage: str) -> int | None:
    iterations = [
        _entry_iteration(entry)
        for entry in _stage_entries(ctx, stage)
        if entry.get("status") == "completed" and _entry_iteration(entry) is not None
    ]
    return max(iterations) if iterations else None


def _latest_plan(ctx: RunContext, stage: str, iteration: int | None = None) -> Path | None:
    for entry in reversed(_stage_entries_for_iteration(ctx, stage, iteration)):
        plan = entry.get("plan")
        if isinstance(plan, str):
            return Path(plan)
    return None


def _latest_drift_plan(ctx: RunContext, target_stage: str, iteration: int) -> Path | None:
    for entry in reversed(_stage_entries_for_iteration(ctx, "adaptive_drift", iteration)):
        if entry.get("target_stage") != target_stage:
            continue
        plan = entry.get("plan")
        if isinstance(plan, str):
            return Path(plan)
    return None


def _latest_metrics(ctx: RunContext, phase: str, iteration: int | None = None) -> Path | None:
    for entry in reversed(_stage_entries_for_iteration(ctx, f"{phase}_metrics", iteration)):
        metrics = entry.get("metrics")
        if isinstance(metrics, str):
            return Path(metrics)
    return None


def _latest_lut_integrity(ctx: RunContext, phase: str, iteration: int | None = None) -> Path | None:
    for entry in reversed(_stage_entries_for_iteration(ctx, f"{phase}_lut_integrity", iteration)):
        integrity = entry.get("integrity")
        if isinstance(integrity, str):
            return Path(integrity)
    return None


def _latest_3dlut_cube(ctx: RunContext, iteration: int | None = None) -> Path | None:
    for entry in reversed(ctx.manifest.stages):
        if iteration is not None and _entry_iteration(entry) != iteration:
            continue
        cube = entry.get("cube")
        if isinstance(cube, str):
            return Path(cube)
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("cube"), str):
            return Path(str(artifacts["cube"]))
    return None


def _latest_artifact(ctx: RunContext, stage: str, key: str, iteration: int | None = None) -> Path | None:
    for entry in reversed(_stage_entries_for_iteration(ctx, stage, iteration)):
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get(key), str):
            return Path(str(artifacts[key]))
    return None


def _latest_candidate(ctx: RunContext, iteration: int | None = None) -> Path | None:
    for entry in reversed(_stage_entries_for_iteration(ctx, "build_mhc_baseline", iteration)):
        candidate = entry.get("candidate")
        if isinstance(candidate, str):
            return Path(candidate)
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("candidate"), str):
            return Path(str(artifacts["candidate"]))
    return None


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


def _latest_decision_iteration(ctx: RunContext, phase: str) -> int | None:
    decision = _latest_decision(ctx, phase) or {}
    value = decision.get("iteration")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _latest_decision_status(ctx: RunContext, phase: str) -> str | None:
    decision = _latest_decision(ctx, phase) or {}
    status = decision.get("decision") or decision.get("status")
    return status if status in {"continue", "stop"} else None


def _latest_decision_next_params(ctx: RunContext, phase: str) -> dict[str, Any]:
    decision = _latest_decision(ctx, phase) or {}
    params = decision.get("next_params")
    return params if isinstance(params, dict) else {}


def _require_path(path: Path | None, reason: str) -> Path:
    if path is None:
        raise ValueError(reason)
    return path


def _continuation_iteration(ctx: RunContext, phase: str) -> int:
    return (_latest_decision_iteration(ctx, phase) or 1) + 1


def _drift_target_iteration(ctx: RunContext, target_stage: str) -> int:
    if target_stage == "mhc-verification" and _latest_decision_status(ctx, "mhc") == "continue":
        return _continuation_iteration(ctx, "mhc")
    if target_stage in {"post-mhc", "3dlut-verification"} and _latest_decision_status(ctx, "3dlut") == "continue":
        return _continuation_iteration(ctx, "3dlut")
    return 1


def _optional_arg(value: Any, option: str) -> list[str]:
    return [option, str(value)] if value is not None else []


def _adaptive_drift_cli_args(ctx: RunContext) -> list[str]:
    payload = ctx.manifest.desktoplut.get("adaptive_drift")
    config = payload if isinstance(payload, dict) else {}
    argv: list[str] = []
    if isinstance(config.get("coldest_channel"), str):
        argv.extend(["--coldest-channel", str(config["coldest_channel"])])
    if isinstance(config.get("gray_levels"), list) and config["gray_levels"]:
        argv.extend(["--gray-levels", ",".join(str(level) for level in config["gray_levels"])])
    mapping = {
        "bias": "--bias",
        "delta_threshold": "--delta-threshold",
        "max_repeats": "--max-repeats",
        "settle_required": "--settle-required",
    }
    for key, option in mapping.items():
        argv.extend(_optional_arg(config.get(key), option))
    return argv


def _probe_match_request(ctx: RunContext) -> dict[str, Any]:
    request = ctx.manifest.desktoplut.get("probe_match_request")
    if not isinstance(request, dict) or request.get("enabled") is not True:
        raise ValueError("probe-match planning requires an enabled probe_match_request")
    return request


def _probe_match_plan_args(request: dict[str, Any]) -> list[str]:
    argv: list[str] = []
    mapping = {
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
    for key, option in mapping.items():
        argv.extend(_optional_arg(request.get(key), option))
    if request.get("high_res"):
        argv.append("--high-res")
    return argv


def argv_for_action(
    ctx: RunContext,
    action: NextAction,
    *,
    port: int | None,
    mock_desktoplut: bool,
    simulate_execution: bool = False,
) -> list[str]:
    port = resolve_live_meter_port(ctx, port)
    root = str(ctx.root)
    if action.action == "desktoplut_contract_check":
        argv = ["desktoplut-contract-check", "--run", root, "--mode", ctx.manifest.mode]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "enter_calibration_mode":
        argv = ["desktoplut-calibration-mode", "enter", "--run", root, "--mode", ctx.manifest.mode]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "plan_probe_match":
        argv = ["probe-match-plan", "--run", root]
        argv.extend(_probe_match_plan_args(_probe_match_request(ctx)))
        return argv
    if action.action == "execute_probe_match":
        plan = _require_path(_latest_plan(ctx, "probe_match"), "probe-match execution requires a recorded plan")
        return ["probe-match-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action in DRIFT_PLAN_ACTION_TARGETS:
        target_stage = DRIFT_PLAN_ACTION_TARGETS[action.action]
        iteration = _drift_target_iteration(ctx, target_stage)
        argv = ["drift-plan", "--run", root, "--stage", target_stage, "--iteration", str(iteration)]
        argv.extend(_adaptive_drift_cli_args(ctx))
        return argv
    if action.action in DRIFT_SEQUENCE_ACTION_TARGETS:
        target_stage = DRIFT_SEQUENCE_ACTION_TARGETS[action.action]
        iteration = _drift_target_iteration(ctx, target_stage)
        plan = _require_path(_latest_drift_plan(ctx, target_stage, iteration), "drift patch-sequence requires a recorded adaptive drift plan")
        return ["patch-sequence", "--run", root, "--kind", "drift", "--stage", target_stage, "--iteration", str(iteration), "--drift-plan", str(plan)]
    if action.action == "plan_raw_mhc":
        if port is None:
            raise ValueError("profile planning requires --port")
        return ["profile-plan", "--run", root, "--stage", "raw-mhc", "--port", str(port), "--iteration", "1"]
    if action.action == "execute_raw_mhc":
        plan = _require_path(_latest_plan(ctx, "raw-mhc"), "raw-MHC execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "build_mhc_baseline":
        return ["mhc-build", "--run", root, "--iteration", "1"]
    if action.action == "build_mhc_iteration":
        previous_iteration = _latest_decision_iteration(ctx, "mhc") or 1
        iteration = previous_iteration + 1
        source = _require_path(_latest_artifact(ctx, "mhc-verification", "ti3", previous_iteration), "MHC continuation requires previous verification TI3")
        params = _latest_decision_next_params(ctx, "mhc")
        return [
            "mhc-build",
            "--run",
            root,
            "--iteration",
            str(iteration),
            "--source-ti3",
            str(source),
            "--lut-size",
            str(params.get("lut_size", 4096)),
            "--gamma",
            str(params.get("gamma", 2.2)),
        ]
    if action.action == "apply_mhc_baseline":
        argv = ["mhc-apply", "--run", root]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "apply_mhc_iteration":
        iteration = _continuation_iteration(ctx, "mhc")
        candidate = _require_path(_latest_candidate(ctx, iteration), "MHC apply continuation requires a recorded candidate")
        argv = ["mhc-apply", "--run", root, "--candidate", str(candidate)]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "plan_mhc_verification":
        if port is None:
            raise ValueError("profile planning requires --port")
        return ["profile-plan", "--run", root, "--stage", "mhc-verification", "--port", str(port), "--iteration", "1"]
    if action.action == "plan_mhc_verification_iteration":
        if port is None:
            raise ValueError("profile planning requires --port")
        return ["profile-plan", "--run", root, "--stage", "mhc-verification", "--port", str(port), "--iteration", str(_continuation_iteration(ctx, "mhc"))]
    if action.action == "execute_mhc_verification":
        plan = _require_path(_latest_plan(ctx, "mhc-verification"), "MHC verification execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "execute_mhc_verification_iteration":
        plan = _require_path(_latest_plan(ctx, "mhc-verification", _continuation_iteration(ctx, "mhc")), "MHC verification execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "score_mhc_iteration":
        iteration = _latest_completed_iteration(ctx, "mhc-verification") or _latest_iteration(ctx, "mhc-verification") or 1
        source = _require_path(_latest_artifact(ctx, "mhc-verification", "ti3", iteration), "MHC scoring requires a recorded verification TI3")
        return ["metrics", "--run", root, "--phase", "mhc", "--iteration", str(iteration), "--source-ti3", str(source)]
    if action.action == "decide_mhc_iteration":
        metrics = _require_path(_latest_metrics(ctx, "mhc"), "MHC decision requires metrics")
        return ["decide", "--run", root, "--phase", "mhc", "--metrics-json", str(metrics)]
    if action.action == "plan_post_mhc":
        if port is None:
            raise ValueError("profile planning requires --port")
        return ["profile-plan", "--run", root, "--stage", "post-mhc", "--port", str(port), "--iteration", "1"]
    if action.action == "plan_post_mhc_iteration":
        if port is None:
            raise ValueError("profile planning requires --port")
        params = _latest_decision_next_params(ctx, "3dlut")
        argv = ["profile-plan", "--run", root, "--stage", "post-mhc", "--port", str(port), "--iteration", str(_continuation_iteration(ctx, "3dlut"))]
        argv.extend(_optional_arg(params.get("post_mhc_patch_count"), "--patch-count"))
        return argv
    if action.action == "execute_post_mhc":
        plan = _require_path(_latest_plan(ctx, "post-mhc"), "post-MHC execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "execute_post_mhc_iteration":
        plan = _require_path(_latest_plan(ctx, "post-mhc", _continuation_iteration(ctx, "3dlut")), "post-MHC execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "plan_3dlut":
        return ["3dlut-plan", "--run", root, "--iteration", "1"]
    if action.action == "plan_3dlut_iteration":
        params = _latest_decision_next_params(ctx, "3dlut")
        argv = ["3dlut-plan", "--run", root, "--iteration", str(_continuation_iteration(ctx, "3dlut"))]
        argv.extend(_optional_arg(params.get("grid_size", 33), "--grid-size"))
        argv.extend(_optional_arg(params.get("quality", "u"), "--quality"))
        argv.extend(_optional_arg(params.get("intent", "r"), "--intent"))
        argv.extend(_optional_arg(params.get("eotf", "b"), "--eotf"))
        return argv
    if action.action == "execute_3dlut":
        plan = _require_path(_latest_plan(ctx, "build_3dlut"), "3D LUT execution requires a recorded plan")
        return ["3dlut-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "execute_3dlut_iteration":
        plan = _require_path(_latest_plan(ctx, "build_3dlut", _continuation_iteration(ctx, "3dlut")), "3D LUT execution requires a recorded plan")
        return ["3dlut-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "apply_3dlut":
        argv = ["3dlut-apply", "--run", root]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "apply_3dlut_iteration":
        iteration = _continuation_iteration(ctx, "3dlut")
        cube = _require_path(_latest_3dlut_cube(ctx, iteration), "3D LUT apply continuation requires a recorded cube path")
        argv = ["3dlut-apply", "--run", root, "--cube", str(cube)]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "plan_3dlut_verification":
        if port is None:
            raise ValueError("profile planning requires --port")
        return ["profile-plan", "--run", root, "--stage", "3dlut-verification", "--port", str(port), "--iteration", "1"]
    if action.action == "plan_3dlut_verification_iteration":
        if port is None:
            raise ValueError("profile planning requires --port")
        return ["profile-plan", "--run", root, "--stage", "3dlut-verification", "--port", str(port), "--iteration", str(_continuation_iteration(ctx, "3dlut"))]
    if action.action == "execute_3dlut_verification":
        plan = _require_path(_latest_plan(ctx, "3dlut-verification"), "3D LUT verification execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "execute_3dlut_verification_iteration":
        plan = _require_path(_latest_plan(ctx, "3dlut-verification", _continuation_iteration(ctx, "3dlut")), "3D LUT verification execution requires a recorded plan")
        return ["profile-execute", "--run", root, "--plan", str(plan), "--simulate" if simulate_execution else "--execute"]
    if action.action == "score_3dlut_iteration":
        iteration = _latest_completed_iteration(ctx, "3dlut-verification") or _latest_iteration(ctx, "3dlut-verification") or 1
        source = _require_path(_latest_artifact(ctx, "3dlut-verification", "ti3", iteration), "3D LUT scoring requires a recorded verification TI3")
        return ["metrics", "--run", root, "--phase", "3dlut", "--iteration", str(iteration), "--source-ti3", str(source)]
    if action.action == "check_3dlut_integrity":
        iteration = _latest_iteration(ctx, "3dlut_metrics") or _latest_iteration(ctx, "build_3dlut") or 1
        cube = _require_path(_latest_3dlut_cube(ctx, iteration), "3D LUT integrity check requires a recorded cube path")
        return ["3dlut-check", "--run", root, "--cube", str(cube), "--iteration", str(iteration)]
    if action.action == "decide_3dlut_iteration":
        metrics = _require_path(_latest_metrics(ctx, "3dlut"), "3D LUT decision requires metrics")
        integrity = _require_path(_latest_lut_integrity(ctx, "3dlut"), "3D LUT decision requires integrity")
        return [
            "decide",
            "--run",
            root,
            "--phase",
            "3dlut",
            "--metrics-json",
            str(metrics),
            "--lut-integrity-json",
            str(integrity),
        ]
    if action.action == "capture_final_desktoplut_state":
        argv = ["desktoplut-state-capture", "--run", root, "--label", "final"]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "capture_final_windows_color_state":
        argv = ["windows-state-capture", "--run", root, "--label", "final"]
        if mock_desktoplut:
            argv.append("--mock")
        return argv
    if action.action == "write_tool_preflight":
        return ["preflight", "--run", root]
    if action.action == "write_pipeline_evidence":
        return ["pipeline-evidence", "--run", root]
    if action.action == "write_report":
        return ["report", "--run", root]
    if action.action == "final_audit":
        return ["final-audit", "--run", root]
    if action.action == "finalize_run":
        return ["finalize-run", "--run", root]
    raise ValueError(f"no supervisor argv mapping for action: {action.action}")


def _run_cli_argv(argv: list[str]) -> CommandResult:
    from .cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = int(main(argv))
    except SystemExit as exc:
        returncode = int(exc.code or 0)
    except Exception as exc:  # pragma: no cover - defensive event capture
        returncode = 1
        print(f"{type(exc).__name__}: {exc}", file=stderr)
    return CommandResult(argv=argv, returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def _refresh_operator_views(
    ctx: RunContext,
    *,
    port: int | None,
    refresh_seconds: int,
    execute_safe: bool,
    allow_hardware: bool,
    allow_live_desktoplut: bool,
    allow_builds: bool,
    mock_desktoplut: bool,
    simulate_execution: bool,
) -> tuple[str, str]:
    dashboard = write_dashboard_html(
        ctx,
        port=port,
        refresh_seconds=refresh_seconds,
        record_stage=False,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
    )
    readout = write_readout_html(
        open_run(ctx.root),
        port=port,
        refresh_seconds=refresh_seconds,
        record_stage=False,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
    )
    return str(dashboard), str(readout)


def run_stage_once(
    ctx: RunContext,
    *,
    expected_action: str | None = None,
    port: int | None = None,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
    update_dashboard: bool = False,
    dashboard_refresh_seconds: int = 5,
) -> StageRunResult:
    action = recommend_next_action(ctx, port=port)
    blocked_reason = None
    command_result = None
    executable = False
    executed = False

    if expected_action and action.action != expected_action:
        blocked_reason = f"expected action {expected_action!r}, but next recommendation is {action.action!r}"
    else:
        blocked_reason = blocked_reason_for_action(
            action,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        )
        executable = blocked_reason is None

    writer = EventWriter(ctx.events_path)
    writer.write(
        "INFO",
        "run_stage",
        "run_stage_recommended",
        recommendation=action.as_dict(),
        expected_action=expected_action,
        executable=executable,
        blocked_reason=blocked_reason,
    )

    if executable:
        try:
            argv = argv_for_action(ctx, action, port=port, mock_desktoplut=mock_desktoplut, simulate_execution=simulate_execution)
            writer.write(
                "INFO",
                "run_stage",
                "run_stage_command_started",
                argv=argv,
                action=action.action,
            )
            command_result = _run_cli_argv(argv)
            executed = True
            writer.write(
                "INFO" if command_result.ok else "ERROR",
                "run_stage",
                "run_stage_command_finished",
                argv=argv,
                returncode=command_result.returncode,
            )
            if not command_result.ok:
                blocked_reason = "command failed"
        except Exception as exc:
            blocked_reason = str(exc)
            command_result = CommandResult(argv=[], returncode=1, stdout="", stderr=blocked_reason)
            writer.write("ERROR", "run_stage", "run_stage_command_failed", error=blocked_reason)

    dashboard_path = None
    readout_path = None
    current = open_run(ctx.root)
    stage_result_ok = not (expected_action and action.action != expected_action) and (command_result is None or command_result.ok)
    current.manifest.stages.append(
        {
            "stage": "run_stage",
            "status": "executed" if executed and stage_result_ok else ("blocked" if blocked_reason else "observed"),
            "action": action.action,
            "expected_action": expected_action,
            "blocked_reason": blocked_reason,
            "executed": executed,
            "returncode": command_result.returncode if command_result else None,
        }
    )
    current.save()
    if update_dashboard:
        dashboard_path, readout_path = _refresh_operator_views(
            open_run(ctx.root),
            port=port,
            refresh_seconds=dashboard_refresh_seconds,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        )
    result = StageRunResult(
        run=str(ctx.root),
        recommendation=action.as_dict(),
        expected_action=expected_action,
        executable=executable,
        executed=executed,
        blocked_reason=blocked_reason,
        command_result=command_result,
        dashboard=dashboard_path,
        readout=readout_path,
    )
    return result


def supervise_run(
    ctx: RunContext,
    *,
    port: int | None = None,
    max_steps: int = 10,
    execute_safe: bool = False,
    allow_hardware: bool = False,
    allow_live_desktoplut: bool = False,
    allow_builds: bool = False,
    mock_desktoplut: bool = False,
    simulate_execution: bool = False,
    update_dashboard: bool = False,
    dashboard_refresh_seconds: int = 5,
) -> SuperviseResult:
    started = datetime.now().isoformat(timespec="seconds")
    current = open_run(ctx.root)
    current.manifest.desktoplut["supervision_options"] = {
        "execute_safe": execute_safe,
        "allow_hardware": allow_hardware,
        "allow_live_desktoplut": allow_live_desktoplut,
        "allow_builds": allow_builds,
        "mock_desktoplut": mock_desktoplut,
        "simulate_execution": simulate_execution,
    }
    current.save()
    writer = EventWriter(ctx.events_path)
    writer.write(
        "INFO",
        "supervise",
        "supervise_started",
        max_steps=max_steps,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
    )

    steps: list[SuperviseStep] = []
    stopped_reason = "max_steps reached"
    dashboard_path = None
    readout_path = None
    for index in range(1, max_steps + 1):
        current = open_run(ctx.root)
        recommendation = recommend_next_action(current, port=port)
        blocked_reason = blocked_reason_for_action(
            recommendation,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        )
        command_result = None
        executable = blocked_reason is None
        executed = False

        writer.write(
            "INFO",
            "supervise",
            "supervise_step",
            index=index,
            recommendation=recommendation.as_dict(),
            executable=executable,
            blocked_reason=blocked_reason,
        )

        if executable:
            try:
                argv = argv_for_action(current, recommendation, port=port, mock_desktoplut=mock_desktoplut, simulate_execution=simulate_execution)
                writer.write(
                    "INFO",
                    "supervise",
                    "supervise_command_started",
                    index=index,
                    argv=argv,
                    action=recommendation.action,
                )
                command_result = _run_cli_argv(argv)
                executed = True
                writer.write(
                    "INFO" if command_result.ok else "ERROR",
                    "supervise",
                    "supervise_command_finished",
                    index=index,
                    argv=argv,
                    returncode=command_result.returncode,
                )
                if not command_result.ok:
                    blocked_reason = "command failed"
                    stopped_reason = blocked_reason
            except Exception as exc:
                blocked_reason = str(exc)
                command_result = CommandResult(argv=[], returncode=1, stdout="", stderr=blocked_reason)
                writer.write("ERROR", "supervise", "supervise_command_failed", index=index, error=blocked_reason)
                stopped_reason = blocked_reason

        step = SuperviseStep(
            index=index,
            recommendation=recommendation.as_dict(),
            executable=executable,
            executed=executed,
            blocked_reason=blocked_reason,
            command_result=command_result,
        )
        steps.append(step)

        if update_dashboard:
            dashboard_path, readout_path = _refresh_operator_views(
                open_run(ctx.root),
                port=port,
                refresh_seconds=dashboard_refresh_seconds,
                execute_safe=execute_safe,
                allow_hardware=allow_hardware,
                allow_live_desktoplut=allow_live_desktoplut,
                allow_builds=allow_builds,
                mock_desktoplut=mock_desktoplut,
                simulate_execution=simulate_execution,
            )

        if blocked_reason is not None:
            stopped_reason = blocked_reason
            break
        if command_result is not None and not command_result.ok:
            break

    finished = datetime.now().isoformat(timespec="seconds")
    complete = open_run(ctx.root).manifest.status == "finalized"
    report_dir = ctx.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact = report_dir / f"supervise_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    result = SuperviseResult(
        run=str(ctx.root),
        started=started,
        finished=finished,
        execute_safe=execute_safe,
        allow_hardware=allow_hardware,
        allow_live_desktoplut=allow_live_desktoplut,
        allow_builds=allow_builds,
        mock_desktoplut=mock_desktoplut,
        simulate_execution=simulate_execution,
        stopped_reason=stopped_reason,
        steps=steps,
        artifact=str(artifact),
        dashboard=dashboard_path,
        readout=readout_path,
        complete=complete,
    )
    artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    current = open_run(ctx.root)
    current.manifest.stages.append(
        {
            "stage": "supervise",
            "status": "completed" if result.ok else "failed",
            "steps": len(steps),
            "stopped_reason": stopped_reason,
            "artifact": str(artifact),
        }
    )
    current.save()
    if update_dashboard:
        dashboard_path, readout_path = _refresh_operator_views(
            open_run(ctx.root),
            port=port,
            refresh_seconds=dashboard_refresh_seconds,
            execute_safe=execute_safe,
            allow_hardware=allow_hardware,
            allow_live_desktoplut=allow_live_desktoplut,
            allow_builds=allow_builds,
            mock_desktoplut=mock_desktoplut,
            simulate_execution=simulate_execution,
        )
        result = SuperviseResult(
            run=result.run,
            started=result.started,
            finished=result.finished,
            execute_safe=result.execute_safe,
            allow_hardware=result.allow_hardware,
            allow_live_desktoplut=result.allow_live_desktoplut,
            allow_builds=result.allow_builds,
            mock_desktoplut=result.mock_desktoplut,
            simulate_execution=result.simulate_execution,
            stopped_reason=result.stopped_reason,
            steps=result.steps,
            artifact=result.artifact,
            dashboard=dashboard_path,
            readout=readout_path,
            complete=open_run(ctx.root).manifest.status == "finalized",
        )
        artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    current.log(f"Supervisor stopped after {len(steps)} step(s): {stopped_reason}")
    EventWriter(current.events_path).write(
        "INFO" if result.ok else "ERROR",
        "supervise",
        "supervise_finished",
        steps=len(steps),
        stopped_reason=stopped_reason,
        artifact=str(artifact),
    )
    return result

