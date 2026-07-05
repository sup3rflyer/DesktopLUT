"""3D LUT build planning and DesktopLUT application."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .argyll import command_for_log
from .desktoplut_client import DesktopLutClient
from .events import EventWriter
from .mhc import resolve_run_path
from .profiles import argyll_ref_profile
from .runs import RunContext
from .simulation import write_identity_cube, write_placeholder_icc
from .tools import ToolSet


@dataclass(frozen=True)
class Lut3dBuildPlan:
    phase: str
    mode: str
    iteration: int
    source_icc: str
    display_icc: str
    output_base: str
    grid_size: int
    quality: str
    intent: str
    eotf: str
    artifacts: dict[str, str]
    command_argv: list[str]
    command: str
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Lut3dBuildResult:
    iteration: int
    dry_run: bool
    simulated: bool
    ok: bool
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    error: str
    cube_path: str
    result_path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_source_icc(mode: str) -> Path:
    # PROJECT_DIR-anchored (absolute), so the default does not silently depend on
    # the caller's cwd — a relative path here only resolved when the orchestrator
    # happened to be launched from the DLC directory.
    if mode.upper() == "SDR":
        return argyll_ref_profile("Rec709.icm")
    return argyll_ref_profile("Rec2020.icm")


def latest_post_mhc_icc(ctx: RunContext) -> Path | None:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") != "post-mhc":
            continue
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("icc"), str):
            return resolve_run_path(ctx, str(artifacts["icc"]))
    return None


def build_3dlut_plan(
    *,
    tools: ToolSet,
    mode: str,
    iteration: int,
    source_icc: Path,
    display_icc: Path,
    output_dir: Path,
    grid_size: int = 33,
    quality: str = "h",
    intent: str = "r",
    eotf: str = "b",
) -> Lut3dBuildPlan:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / f"3dlut_iter{iteration:02d}_{mode.lower()}"
    cube_path = base.with_suffix(".cube")
    device_link_path = base.with_suffix(".icc")
    plan_path = output_dir.parent / "sequences" / f"3dlut_iter{iteration:02d}_build_plan.json"
    command_argv = [
        str(tools.collink.path or "collink.exe"),
        "-v",
        "-3c",
        f"-q{quality}",
        f"-r{grid_size}",
        f"-I{eotf}",
        "-b",
        "-G",
        f"-i{intent}",
        str(source_icc),
        str(display_icc),
        str(device_link_path),
    ]
    notes = [
        "Uses Argyll collink -3c to emit an IRIDAS .cube alongside the ICC device-link output.",
        "Default SDR target is Argyll's contained Rec709.icm; override source_icc for alternate targets.",
        "Run after MHC is active and runtime 3D LUT/grayscale tweak layers are clear.",
    ]
    if not tools.collink.ok:
        notes.append("collink is missing; this plan cannot execute until contained Argyll is available.")
    return Lut3dBuildPlan(
        phase="3dlut",
        mode=mode,
        iteration=iteration,
        source_icc=str(source_icc),
        display_icc=str(display_icc),
        output_base=str(base),
        grid_size=grid_size,
        quality=quality,
        intent=intent,
        eotf=eotf,
        artifacts={
            "cube": str(cube_path),
            "device_link": str(device_link_path),
            "plan": str(plan_path),
        },
        command_argv=command_argv,
        command=command_for_log(command_argv),
        notes=notes,
    )


def write_3dlut_build_plan(
    *,
    ctx: RunContext,
    tools: ToolSet,
    iteration: int = 1,
    source_icc: Path | None = None,
    display_icc: Path | None = None,
    grid_size: int = 33,
    quality: str = "h",
    intent: str = "r",
    eotf: str = "b",
) -> Lut3dBuildPlan:
    resolved_source = resolve_run_path(ctx, source_icc or default_source_icc(ctx.manifest.mode))
    display_candidate = display_icc or latest_post_mhc_icc(ctx)
    if display_candidate is None:
        raise FileNotFoundError("display ICC not found; complete post-mhc profile measurement or pass --display-icc")
    resolved_display = resolve_run_path(ctx, display_candidate)
    if not resolved_source.exists():
        raise FileNotFoundError(f"source ICC not found: {resolved_source}")
    if not resolved_display.exists():
        raise FileNotFoundError("display ICC not found; complete post-mhc profile measurement or pass --display-icc")

    plan = build_3dlut_plan(
        tools=tools,
        mode=ctx.manifest.mode,
        iteration=iteration,
        source_icc=resolved_source,
        display_icc=resolved_display,
        output_dir=ctx.root / "generated",
        grid_size=grid_size,
        quality=quality,
        intent=intent,
        eotf=eotf,
    )
    plan_path = Path(plan.artifacts["plan"])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "build_3dlut",
            "iteration": iteration,
            "status": "planned",
            "plan": str(plan_path),
            "artifacts": plan.artifacts,
        }
    )
    ctx.save()
    ctx.log(f"Planned 3D LUT build iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        "build_3dlut",
        "3dlut_build_planned",
        iteration=iteration,
        plan=str(plan_path),
        source_icc=str(resolved_source),
        display_icc=str(resolved_display),
        cube=plan.artifacts["cube"],
    )
    return plan


def load_3dlut_build_plan(plan_path: Path) -> Lut3dBuildPlan:
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    return Lut3dBuildPlan(
        phase=raw["phase"],
        mode=raw["mode"],
        iteration=int(raw["iteration"]),
        source_icc=raw["source_icc"],
        display_icc=raw["display_icc"],
        output_base=raw["output_base"],
        grid_size=int(raw["grid_size"]),
        quality=raw["quality"],
        intent=raw["intent"],
        eotf=raw["eotf"],
        artifacts=raw["artifacts"],
        command_argv=raw["command_argv"],
        command=raw["command"],
        notes=raw.get("notes", []),
    )


def execute_3dlut_build_plan(
    *,
    ctx: RunContext,
    plan_path: Path,
    dry_run: bool = True,
    simulate: bool = False,
    timeout_seconds: int = 7200,
) -> Lut3dBuildResult:
    plan = load_3dlut_build_plan(plan_path)
    log_dir = ctx.root / "generated" / f"3dlut_iter{plan.iteration:02d}_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "collink_stdout.txt"
    stderr_path = log_dir / "collink_stderr.txt"
    result_path = log_dir / "build_result.json"
    cube_path = resolve_run_path(ctx, plan.artifacts["cube"])

    EventWriter(ctx.events_path).write(
        "INFO",
        "build_3dlut",
        "3dlut_build_execute_started",
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        plan=str(plan_path),
    )

    returncode: int | None = None
    stdout = ""
    stderr = ""
    error = ""
    if simulate:
        write_identity_cube(cube_path, size=min(plan.grid_size, 17), title=f"DLC simulated 3D LUT iter {plan.iteration}")
        write_placeholder_icc(resolve_run_path(ctx, plan.artifacts["device_link"]), description=f"3D LUT device link iteration {plan.iteration}")
        stdout = f"simulated command: {plan.command}\n"
    elif not dry_run:
        timed_out = False
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                plan.command_argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            EventWriter(ctx.events_path).write(
                "INFO",
                "build_3dlut",
                "3dlut_build_collink_started",
                iteration=plan.iteration,
                argv=plan.command_argv,
                pid=proc.pid,
                timeout_seconds=timeout_seconds,
                stdout=str(stdout_path),
                stderr=str(stderr_path),
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
            error = str(exc)
        finally:
            if proc is not None:
                EventWriter(ctx.events_path).write(
                    "ERROR" if error or returncode != 0 else "INFO",
                    "build_3dlut",
                    "3dlut_build_collink_finished",
                    iteration=plan.iteration,
                    argv=plan.command_argv,
                    pid=proc.pid,
                    returncode=returncode,
                    error=error,
                    timed_out=timed_out,
                    stdout=str(stdout_path),
                    stderr=str(stderr_path),
                )
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if returncode == 0 and not cube_path.exists():
            error = f"collink completed but expected cube was not found: {cube_path}"

    if simulate:
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        returncode = 0

    ok = dry_run or (returncode == 0 and not error)
    result = Lut3dBuildResult(
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        ok=ok,
        command=plan.command,
        returncode=returncode,
        stdout="" if dry_run else str(stdout_path),
        stderr="" if dry_run else str(stderr_path),
        error=error,
        cube_path=str(cube_path),
        result_path=str(result_path),
    )
    result_path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "build_3dlut",
            "iteration": plan.iteration,
            "status": "execute_dry_run" if dry_run else ("completed" if ok else "failed"),
            "plan": str(plan_path),
            "execution_result": str(result_path),
            "artifacts": plan.artifacts,
            "simulated": simulate,
        }
    )
    ctx.save()
    ctx.log(f"{'Dry-ran' if dry_run else 'Executed'} 3D LUT build iteration {plan.iteration}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "build_3dlut",
        "3dlut_build_execute_finished",
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        ok=ok,
        cube=str(cube_path),
        result=str(result_path),
        error=error,
    )
    return result


def latest_3dlut_cube(ctx: RunContext) -> Path | None:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") != "build_3dlut":
            continue
        artifacts = entry.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("cube"), str):
            return resolve_run_path(ctx, str(artifacts["cube"]))
    return None


def iteration_for_3dlut_cube(ctx: RunContext, cube: Path) -> int | None:
    resolved = str(cube)
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") != "build_3dlut":
            continue
        artifacts = entry.get("artifacts")
        artifact_cube = artifacts.get("cube") if isinstance(artifacts, dict) else None
        if artifact_cube is not None and str(artifact_cube) == resolved:
            value = entry.get("iteration")
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def apply_3dlut_candidate(
    *,
    ctx: RunContext,
    client: DesktopLutClient,
    cube_path: Path | None = None,
    monitor: int = 0,
) -> dict[str, Any]:
    cube = resolve_run_path(ctx, cube_path or latest_3dlut_cube(ctx) or Path(""))
    if not cube.exists():
        raise FileNotFoundError(f"3D LUT cube not found: {cube}")
    commands = [
        client.disable_grayscale_tweak(monitor, ctx.manifest.mode),
        client.set_3dlut(monitor, ctx.manifest.mode, str(cube)),
        client.state_get(),
    ]
    responses = []
    for command in commands:
        response = client.send(command, raise_on_error=False)
        responses.append({"command": command.as_dict(), "response": response.as_dict()})
        if not response.ok:
            break
    ok = all(item["response"]["ok"] for item in responses)
    result = {"ok": ok, "cube": str(cube), "responses": responses}
    iteration = iteration_for_3dlut_cube(ctx, cube)
    ctx.manifest.desktoplut["last_3dlut_apply"] = result
    ctx.manifest.stages.append(
        {
            "stage": "apply_3dlut",
            "iteration": iteration,
            "status": "applied" if ok else "failed",
            "cube": str(cube),
        }
    )
    ctx.save()
    ctx.log(f"3D LUT apply {'succeeded' if ok else 'failed'}: {cube}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "apply_3dlut",
        "3dlut_apply_finished",
        ok=ok,
        cube=str(cube),
    )
    return result

