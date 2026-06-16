"""Argyll profile measurement planning."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .argyll import Argyll, Instrument, command_for_log
from .events import EventWriter
from .mhc import resolve_run_path
from .runs import RunContext
from .simulation import write_placeholder_icc, write_placeholder_ti1, write_synthetic_ti3
from .tools import ToolSet


@dataclass(frozen=True)
class ProfileStagePreset:
    stage: str
    description: str
    patches: int
    gray_steps: int
    wedge_steps: int
    white_repeats: int
    black_repeats: int
    profile_type: str


STAGE_PRESETS: dict[str, ProfileStagePreset] = {
    "raw-mhc": ProfileStagePreset(
        stage="raw-mhc",
        description="Raw display ramp/primary characterization before MHC",
        patches=96,
        gray_steps=33,
        wedge_steps=9,
        white_repeats=4,
        black_repeats=4,
        profile_type="-as",
    ),
    "post-mhc": ProfileStagePreset(
        stage="post-mhc",
        description="Post-MHC volumetric characterization for 3D LUT generation",
        patches=729,
        gray_steps=33,
        wedge_steps=17,
        white_repeats=8,
        black_repeats=8,
        profile_type="-ax",
    ),
    "verification": ProfileStagePreset(
        stage="verification",
        description="Final verification ramp plus representative color volume",
        patches=256,
        gray_steps=33,
        wedge_steps=17,
        white_repeats=4,
        black_repeats=4,
        profile_type="-as",
    ),
    "mhc-verification": ProfileStagePreset(
        stage="mhc-verification",
        description="Post-MHC verification ramp before 3D LUT profiling",
        patches=256,
        gray_steps=33,
        wedge_steps=17,
        white_repeats=4,
        black_repeats=4,
        profile_type="-as",
    ),
    "3dlut-verification": ProfileStagePreset(
        stage="3dlut-verification",
        description="Final 3D LUT verification ramp plus representative color volume",
        patches=256,
        gray_steps=33,
        wedge_steps=17,
        white_repeats=4,
        black_repeats=4,
        profile_type="-as",
    ),
}


@dataclass(frozen=True)
class ProfileMeasurementPlan:
    stage: str
    mode: str
    iteration: int
    description: str
    base_name: str
    artifacts: dict[str, str]
    command_argv: list[list[str]]
    commands: list[str]
    notes: list[str]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_profile_measurement_plan(
    *,
    tools: ToolSet,
    mode: str,
    stage: str,
    output_dir: Path,
    iteration: int,
    port: int,
    display_index: int = 1,
    patch_window: str = "0.5,0.5,50,50",
    patch_count: int | None = None,
    correction: Path | None = None,
    high_res: bool = False,
    observer: str | None = None,
    drift_comp: str = "w",
) -> ProfileMeasurementPlan:
    if stage not in STAGE_PRESETS:
        raise ValueError(f"unknown profile stage: {stage}")
    preset = STAGE_PRESETS[stage]
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / f"{stage}_iter{iteration:02d}_{mode.lower()}"
    patches = patch_count or preset.patches

    targen_cmd = [
        str(tools.targen.path or "targen.exe"),
        "-v",
        "-d3",
        "-G",
        "-e",
        str(preset.white_repeats),
        "-B",
        str(preset.black_repeats),
        "-g",
        str(preset.gray_steps),
        "-s",
        str(preset.wedge_steps),
        "-f",
        str(patches),
        str(base),
    ]

    dispread_cmd = [
        str(tools.dispread.path or "dispread.exe"),
        "-v",
        "-d",
        str(display_index),
        "-c",
        str(port),
        "-P",
        patch_window,
        "-F",
        "-Yp",
        "-N",
        "-w",
    ]
    if drift_comp:
        dispread_cmd.append(f"-I{drift_comp}")
    if high_res:
        dispread_cmd.append("-H")
    if correction:
        dispread_cmd.extend(["-X", str(correction)])
    if observer:
        dispread_cmd.extend(["-Q", observer])
    dispread_cmd.append(str(base))

    colprof_cmd = [
        str(tools.colprof.path or "colprof.exe"),
        "-v",
        "-qh",
        "-D",
        f"DesktopLUT Calibrator {mode} {stage} iteration {iteration}",
        preset.profile_type,
        str(base),
    ]

    artifacts = {
        "ti1": str(base.with_suffix(".ti1")),
        "ti3": str(base.with_suffix(".ti3")),
        "icc": str(base.with_suffix(".icc")),
        "plan": str((output_dir.parent / "sequences" / f"{stage}_iter{iteration:02d}_profile_plan.json")),
    }
    if correction:
        artifacts["correction"] = str(correction)
    notes = [
        "dispread -Yp skips the placement prompt; only run after the meter is physically placed.",
        "The generated ICC is an inspection/input artifact for analysis, not the final DesktopLUT MHC install profile.",
    ]
    if correction:
        notes.append(f"Uses colorimeter correction via Argyll -X: {correction}")
    if not tools.dispread.ok:
        notes.append("dispread is missing; this plan cannot execute until Argyll is available.")

    command_argv = [targen_cmd, dispread_cmd, colprof_cmd]

    return ProfileMeasurementPlan(
        stage=stage,
        mode=mode,
        iteration=iteration,
        description=preset.description,
        base_name=str(base),
        artifacts=artifacts,
        command_argv=command_argv,
        commands=[command_for_log(cmd) for cmd in command_argv],
        notes=notes,
    )


def latest_probe_match_correction(ctx: RunContext) -> Path | None:
    manifest_value = ctx.manifest.desktoplut.get("probe_match_correction")
    if isinstance(manifest_value, str):
        candidate = resolve_run_path(ctx, manifest_value)
        if candidate.exists():
            return candidate

    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") != "probe_match":
            continue
        if entry.get("status") not in {"completed", "execute_dry_run", "planned"}:
            continue
        correction = entry.get("correction")
        if not isinstance(correction, str):
            continue
        candidate = resolve_run_path(ctx, correction)
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class CommandExecutionResult:
    index: int
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""
    skipped: bool = False
    instrument_resolution: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProfileExecutionResult:
    stage: str
    iteration: int
    dry_run: bool
    simulated: bool
    ok: bool
    results: list[CommandExecutionResult]
    log_dir: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def resolve_dispread_instrument_port(
    argv: list[str],
    *,
    instrument_enumerator: Callable[[Path], list[Instrument]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    evidence: dict[str, Any] = {
        "ok": True,
        "applicable": False,
        "changed": False,
        "planned_port": None,
        "resolved_port": None,
        "instrument_count": None,
        "instruments": [],
        "reason": "command does not use a dispread -c instrument port",
    }
    if not argv:
        return argv, evidence
    executable = Path(argv[0])
    if executable.name.lower() not in {"dispread.exe", "dispread"} or "-c" not in argv:
        return argv, evidence
    port_index = argv.index("-c") + 1
    if port_index >= len(argv):
        evidence.update({"ok": False, "applicable": True, "reason": "dispread command has -c without a port"})
        return argv, evidence
    try:
        planned_port = int(argv[port_index])
    except ValueError:
        evidence.update({"ok": False, "applicable": True, "reason": f"dispread command has non-numeric port: {argv[port_index]}"})
        return argv, evidence

    spotread = executable.with_name("spotread.exe")
    enumerator = instrument_enumerator or (lambda path: Argyll(path).enumerate_instruments())
    try:
        instruments = enumerator(spotread)
    except (OSError, subprocess.SubprocessError) as exc:
        evidence.update(
            {
                "ok": False,
                "applicable": True,
                "planned_port": planned_port,
                "resolved_port": planned_port,
                "reason": f"could not enumerate instruments with {spotread}: {exc}",
            }
        )
        return argv, evidence

    serialized = [asdict(instrument) for instrument in instruments]
    evidence.update(
        {
            "applicable": True,
            "planned_port": planned_port,
            "resolved_port": planned_port,
            "instrument_count": len(instruments),
            "instruments": serialized,
        }
    )
    if any(instrument.port == planned_port for instrument in instruments):
        evidence["reason"] = "planned port is currently attached"
        return argv, evidence
    if len(instruments) == 1:
        resolved_port = instruments[0].port
        updated = list(argv)
        updated[port_index] = str(resolved_port)
        evidence.update(
            {
                "changed": True,
                "resolved_port": resolved_port,
                "reason": "planned port was stale; selected the only currently attached instrument",
            }
        )
        return updated, evidence
    if instruments:
        evidence.update(
            {
                "ok": False,
                "reason": "planned port is not attached and multiple instruments are present; refusing ambiguous measurement",
            }
        )
    else:
        evidence.update({"ok": False, "reason": "no Argyll instruments are currently attached"})
    return argv, evidence


def load_profile_measurement_plan(plan_path: Path) -> ProfileMeasurementPlan:
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    command_argv = raw.get("command_argv")
    if command_argv is None:
        raise ValueError("profile plan is missing command_argv; regenerate it before execution")
    return ProfileMeasurementPlan(
        stage=raw["stage"],
        mode=raw["mode"],
        iteration=int(raw["iteration"]),
        description=raw["description"],
        base_name=raw["base_name"],
        artifacts=raw["artifacts"],
        command_argv=command_argv,
        commands=raw["commands"],
        notes=raw.get("notes", []),
    )


def execute_profile_measurement_plan(
    *,
    ctx: RunContext,
    plan_path: Path,
    dry_run: bool = True,
    simulate: bool = False,
    timeout_seconds: int = 7200,
    instrument_enumerator: Callable[[Path], list[Instrument]] | None = None,
) -> ProfileExecutionResult:
    plan = load_profile_measurement_plan(plan_path)
    log_dir = ctx.root / "measurements" / f"{plan.stage}_iter{plan.iteration:02d}_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    results: list[CommandExecutionResult] = []

    EventWriter(ctx.events_path).write(
        "INFO",
        plan.stage,
        "profile_measurement_execute_started",
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        plan=str(plan_path),
    )

    for index, argv in enumerate(plan.command_argv, start=1):
        effective_argv = list(argv)
        instrument_resolution = None
        if simulate:
            command = command_for_log(effective_argv)
            stdout_path = log_dir / f"{index:02d}_stdout.txt"
            stderr_path = log_dir / f"{index:02d}_stderr.txt"
            stdout_path.write_text(f"simulated command: {command}\n", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            results.append(CommandExecutionResult(index, command, 0, str(stdout_path), str(stderr_path)))
            continue
        if dry_run:
            command = command_for_log(effective_argv)
            results.append(CommandExecutionResult(index, command, None, "", "", skipped=True))
            continue

        effective_argv, resolution = resolve_dispread_instrument_port(
            effective_argv,
            instrument_enumerator=instrument_enumerator,
        )
        instrument_resolution = resolution if resolution.get("applicable") else None
        command = command_for_log(effective_argv)

        stdout_path = log_dir / f"{index:02d}_stdout.txt"
        stderr_path = log_dir / f"{index:02d}_stderr.txt"
        error = ""
        if instrument_resolution is not None and instrument_resolution.get("ok") is not True:
            returncode = None
            stdout = ""
            stderr = ""
            error = str(instrument_resolution.get("reason", "instrument resolution failed"))
        else:
            timed_out = False
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    effective_argv,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                EventWriter(ctx.events_path).write(
                    "INFO",
                    plan.stage,
                    "profile_measurement_command_started",
                    iteration=plan.iteration,
                    index=index,
                    command=command,
                    argv=effective_argv,
                    pid=proc.pid,
                    timeout_seconds=timeout_seconds,
                    stdout=str(stdout_path),
                    stderr=str(stderr_path),
                    instrument_resolution=instrument_resolution,
                )
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
                returncode = proc.returncode
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                if proc is not None:
                    proc.kill()
                    killed_stdout, killed_stderr = proc.communicate()
                    stdout = killed_stdout or (exc.stdout if isinstance(exc.stdout, str) else "")
                    stderr = killed_stderr or (exc.stderr if isinstance(exc.stderr, str) else "")
                    returncode = proc.returncode
                else:
                    stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
                error = f"timeout after {timeout_seconds} seconds"
            except OSError as exc:
                returncode = None
                stdout = ""
                stderr = ""
                error = str(exc)
            finally:
                if proc is not None:
                    EventWriter(ctx.events_path).write(
                        "ERROR" if error or returncode != 0 else "INFO",
                        plan.stage,
                        "profile_measurement_command_finished",
                        iteration=plan.iteration,
                        index=index,
                        command=command,
                        argv=effective_argv,
                        pid=proc.pid,
                        returncode=returncode,
                        error=error,
                        timed_out=timed_out,
                        stdout=str(stdout_path),
                        stderr=str(stderr_path),
                        instrument_resolution=instrument_resolution,
                    )
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        results.append(
            CommandExecutionResult(
                index=index,
                command=command,
                returncode=returncode,
                stdout=str(stdout_path),
                stderr=str(stderr_path),
                error=error,
                instrument_resolution=instrument_resolution,
            )
        )
        if instrument_resolution is not None and instrument_resolution.get("ok") is not True:
            EventWriter(ctx.events_path).write(
                "ERROR",
                plan.stage,
                "profile_measurement_command_finished",
                iteration=plan.iteration,
                index=index,
                command=command,
                argv=effective_argv,
                returncode=returncode,
                error=error,
                timed_out=False,
                stdout=str(stdout_path),
                stderr=str(stderr_path),
                instrument_resolution=instrument_resolution,
            )
        if returncode != 0:
            break

    simulated_artifacts: dict[str, str] = {}
    if simulate:
        ti1 = resolve_run_path(ctx, Path(plan.artifacts["ti1"]))
        ti3 = resolve_run_path(ctx, Path(plan.artifacts["ti3"]))
        icc = resolve_run_path(ctx, Path(plan.artifacts["icc"]))
        write_placeholder_ti1(ti1, description=f"{plan.stage} iteration {plan.iteration}")
        write_synthetic_ti3(ti3)
        write_placeholder_icc(icc, description=f"{plan.stage} iteration {plan.iteration}")
        simulated_artifacts = {"ti1": str(ti1), "ti3": str(ti3), "icc": str(icc)}

    ok = dry_run or all(result.returncode == 0 for result in results)
    result = ProfileExecutionResult(
        stage=plan.stage,
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        ok=ok,
        results=results,
        log_dir=str(log_dir),
    )
    result_path = log_dir / "execution_result.json"
    result_path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    ctx.manifest.stages.append(
        {
            "stage": plan.stage,
            "iteration": plan.iteration,
            "status": "execute_dry_run" if dry_run else ("completed" if ok else "failed"),
            "plan": str(plan_path),
            "execution_result": str(result_path),
            "artifacts": simulated_artifacts if simulate else plan.artifacts if ok and not dry_run else {},
            "simulated": simulate,
        }
    )
    ctx.save()
    ctx.log(f"{'Dry-ran' if dry_run else 'Executed'} {plan.stage} profile measurement iteration {plan.iteration}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        plan.stage,
        "profile_measurement_execute_finished",
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        ok=ok,
        result=str(result_path),
    )
    return result


def write_profile_measurement_plan(
    *,
    ctx: RunContext,
    tools: ToolSet,
    stage: str,
    iteration: int,
    port: int,
    display_index: int = 1,
    patch_window: str = "0.5,0.5,50,50",
    patch_count: int | None = None,
    correction: Path | None = None,
    use_probe_correction: bool = True,
    high_res: bool = False,
    observer: str | None = None,
    drift_comp: str = "w",
) -> ProfileMeasurementPlan:
    resolved_correction = resolve_run_path(ctx, correction) if correction else None
    if resolved_correction is None and use_probe_correction:
        resolved_correction = latest_probe_match_correction(ctx)
    plan = build_profile_measurement_plan(
        tools=tools,
        mode=ctx.manifest.mode,
        stage=stage,
        output_dir=ctx.root / "measurements",
        iteration=iteration,
        port=port,
        display_index=display_index,
        patch_window=patch_window,
        patch_count=patch_count,
        correction=resolved_correction,
        high_res=high_res,
        observer=observer,
        drift_comp=drift_comp,
    )
    plan_path = Path(str(plan.artifacts["plan"]))
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan.as_dict(), indent=2), encoding="utf-8")

    ctx.manifest.stages.append(
        {
            "stage": stage,
            "iteration": iteration,
            "status": "planned",
            "plan": str(plan_path),
            "artifacts": plan.artifacts,
            "correction": str(resolved_correction) if resolved_correction else None,
        }
    )
    ctx.save()
    ctx.log(f"Planned {stage} profile measurement iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        stage,
        "profile_measurement_planned",
        iteration=iteration,
        plan=str(plan_path),
        commands=len(plan.commands),
        correction=str(resolved_correction) if resolved_correction else None,
    )
    return plan

