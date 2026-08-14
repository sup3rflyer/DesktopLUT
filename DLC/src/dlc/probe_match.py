"""Argyll ccxxmake probe-match planning and execution."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal

from .argyll import Argyll, Instrument, command_for_log
from .events import EventWriter
from .human_actions import has_human_action
from .mhc import resolve_run_path
from .runs import RunContext
from .tools import ToolSet

CorrectionKind = Literal["ccmx", "ccss"]


@dataclass(frozen=True)
class ProbeMatchPlan:
    kind: CorrectionKind
    mode: str
    iteration: int
    display_tech: str
    output: str
    artifacts: dict[str, str]
    command_argv: list[str]
    command: str
    measurement_mode: str
    required_human_actions: list[str]
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeMatchResult:
    kind: CorrectionKind
    iteration: int
    dry_run: bool
    simulated: bool
    ok: bool
    command: str
    returncode: int | None
    stdout: str
    stderr: str
    error: str
    correction: str
    result_path: str
    instrument_inventory: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_match_instrument_inventory(
    plan: ProbeMatchPlan,
    *,
    instrument_enumerator: Callable[[Path], list[Instrument]] | None = None,
) -> dict[str, Any]:
    required_count = 2 if plan.kind == "ccmx" else 1
    if plan.measurement_mode != "live":
        return {
            "ok": True,
            "applicable": False,
            "kind": plan.kind,
            "required_count": 0,
            "instrument_count": None,
            "instruments": [],
            "reason": "probe-match plan uses existing TI3 files and does not need live instruments",
        }
    # The sibling spotread next to the plan's ccxxmake, preserving the plan's separator
    # convention AND its suffix convention (same portability class as profile_plan's
    # dispread gate, F-0.1/F2-3): plans carry contained-tool paths in Windows form, and a
    # POSIX plan carries "ccxxmake", not "ccxxmake.exe" — hardcoding ".exe" here would
    # derive a nonexistent sibling and fail enumeration with a misleading reason.
    raw = str(plan.command_argv[0]) if plan.command_argv else "ccxxmake.exe"
    executable_name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    sibling = "spotread.exe" if executable_name.lower().endswith(".exe") else "spotread"
    # A concrete Path would re-render the string in the host OS's separator (a POSIX
    # plan inspected on Windows becomes \opt\argyll\spotread), so pick the pure-path
    # flavor from the plan path's own separators instead.
    path_cls = PureWindowsPath if "\\" in raw else PurePosixPath
    spotread = path_cls(raw[: len(raw) - len(executable_name)] + sibling)
    enumerator = instrument_enumerator or (lambda path: Argyll(path).enumerate_instruments())
    try:
        instruments = enumerator(spotread)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "applicable": True,
            "kind": plan.kind,
            "required_count": required_count,
            "instrument_count": None,
            "instruments": [],
            "spotread": str(spotread),
            "reason": f"could not enumerate instruments with {spotread}: {exc}",
        }
    instrument_count = len(instruments)
    ok = instrument_count >= required_count
    return {
        "ok": ok,
        "applicable": True,
        "kind": plan.kind,
        "required_count": required_count,
        "instrument_count": instrument_count,
        "instruments": [asdict(instrument) for instrument in instruments],
        "spotread": str(spotread),
        "reason": (
            f"found {instrument_count} instrument(s), enough for live {plan.kind.upper()} probe matching"
            if ok
            else f"live {plan.kind.upper()} probe matching requires at least {required_count} attached Argyll instrument(s)"
        ),
    }


def build_probe_match_plan(
    *,
    tools: ToolSet,
    mode: str,
    output_dir: Path,
    kind: CorrectionKind = "ccmx",
    iteration: int = 1,
    display_tech: str = "u",
    display_index: int = 1,
    patch_window: str = "0.5,0.5,1.0",
    high_res: bool = False,
    colorimeter_display_type: str | None = None,
    spectro_display_type: str | None = None,
    observer: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    steps: int | None = None,
    reference_ti3: Path | None = None,
    target_ti3: Path | None = None,
) -> ProbeMatchPlan:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".ccss" if kind == "ccss" else ".ccmx"
    output = output_dir / f"probe_match_iter{iteration:02d}_{mode.lower()}{suffix}"
    plan_path = output_dir / f"probe_match_iter{iteration:02d}_{kind}_plan.json"

    command_argv = [str(tools.ccxxmake.path or "ccxxmake.exe"), "-v", "-t", display_tech]
    measurement_mode = "live"
    required_human_actions: list[str] = ["spectro_placed"]

    if kind == "ccss":
        command_argv.append("-S")
    else:
        required_human_actions.append("colorimeter_placed")

    if reference_ti3:
        measurement_mode = "from_ti3"
        ref_arg = str(reference_ti3)
        if target_ti3:
            ref_arg += f",{target_ti3}"
        command_argv.extend(["-f", ref_arg])
        required_human_actions = []

    command_argv.extend(["-d", str(display_index), "-P", patch_window, "-F", "-N"])
    if high_res:
        command_argv.append("-H")
    if colorimeter_display_type:
        command_argv.extend(["-y", colorimeter_display_type])
    if spectro_display_type:
        command_argv.extend(["-z", spectro_display_type])
    if observer:
        command_argv.extend(["-o", observer])
    if steps is not None:
        command_argv.extend(["-s", str(steps)])
    if description:
        command_argv.extend(["-E", description])
    if display_name:
        command_argv.extend(["-I", display_name])
    command_argv.append(str(output))

    notes = [
        "CCMX is preferred when calibrating with this exact spectrometer, colorimeter, and display.",
        "CCSS is better for sharing a display spectral sample with other colorimeters.",
        "Live ccxxmake measurement may still present Argyll UI prompts; use --execute only after placement acknowledgements.",
        "The resulting .ccmx/.ccss can be passed to spotread/dispread with -X.",
    ]
    if display_tech == "u":
        notes.append("Display technology is set to Argyll 'u' (Unknown); pass --display-tech for a more specific display technology choice.")
    if not tools.ccxxmake.ok:
        notes.append("ccxxmake is missing; copy contained Argyll tools before execution.")

    return ProbeMatchPlan(
        kind=kind,
        mode=mode,
        iteration=iteration,
        display_tech=display_tech,
        output=str(output),
        artifacts={"correction": str(output), "plan": str(plan_path)},
        command_argv=command_argv,
        command=command_for_log(command_argv),
        measurement_mode=measurement_mode,
        required_human_actions=required_human_actions,
        notes=notes,
    )


def write_probe_match_plan(
    *,
    ctx: RunContext,
    tools: ToolSet,
    kind: CorrectionKind = "ccmx",
    iteration: int = 1,
    display_tech: str = "u",
    display_index: int = 1,
    patch_window: str = "0.5,0.5,1.0",
    high_res: bool = False,
    colorimeter_display_type: str | None = None,
    spectro_display_type: str | None = None,
    observer: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    steps: int | None = None,
    reference_ti3: Path | None = None,
    target_ti3: Path | None = None,
) -> ProbeMatchPlan:
    resolved_reference = resolve_run_path(ctx, reference_ti3) if reference_ti3 else None
    resolved_target = resolve_run_path(ctx, target_ti3) if target_ti3 else None
    plan = build_probe_match_plan(
        tools=tools,
        mode=ctx.manifest.mode,
        output_dir=ctx.root / "probe_match",
        kind=kind,
        iteration=iteration,
        display_tech=display_tech,
        display_index=display_index,
        patch_window=patch_window,
        high_res=high_res,
        colorimeter_display_type=colorimeter_display_type,
        spectro_display_type=spectro_display_type,
        observer=observer,
        display_name=display_name or ctx.manifest.display,
        description=description or f"DesktopLUT Calibrator {ctx.manifest.display or 'display'} {kind.upper()}",
        steps=steps,
        reference_ti3=resolved_reference,
        target_ti3=resolved_target,
    )
    Path(plan.artifacts["plan"]).write_text(json.dumps(plan.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "probe_match",
            "iteration": iteration,
            "status": "planned",
            "kind": kind,
            "plan": plan.artifacts["plan"],
            "correction": plan.artifacts["correction"],
            "measurement_mode": plan.measurement_mode,
            "required_human_actions": plan.required_human_actions,
        }
    )
    ctx.save()
    ctx.log(f"Planned {kind.upper()} probe match iteration {iteration}")
    EventWriter(ctx.events_path).write(
        "INFO",
        "probe_match",
        "probe_match_planned",
        kind=kind,
        iteration=iteration,
        plan=plan.artifacts["plan"],
        correction=plan.artifacts["correction"],
        measurement_mode=plan.measurement_mode,
    )
    return plan


def load_probe_match_plan(plan_path: Path) -> ProbeMatchPlan:
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    return ProbeMatchPlan(
        kind=raw["kind"],
        mode=raw["mode"],
        iteration=int(raw["iteration"]),
        display_tech=raw["display_tech"],
        output=raw["output"],
        artifacts=raw["artifacts"],
        command_argv=raw["command_argv"],
        command=raw["command"],
        measurement_mode=raw["measurement_mode"],
        required_human_actions=raw.get("required_human_actions", []),
        notes=raw.get("notes", []),
    )


def execute_probe_match_plan(
    *,
    ctx: RunContext,
    plan_path: Path,
    dry_run: bool = True,
    simulate: bool = False,
    force: bool = False,
    timeout_seconds: int = 7200,
    instrument_enumerator: Callable[[Path], list[Instrument]] | None = None,
) -> ProbeMatchResult:
    plan = load_probe_match_plan(plan_path)
    log_dir = ctx.root / "probe_match" / f"probe_match_iter{plan.iteration:02d}_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    result_path = log_dir / "execution_result.json"
    stdout_path = log_dir / "ccxxmake_stdout.txt"
    stderr_path = log_dir / "ccxxmake_stderr.txt"
    correction = resolve_run_path(ctx, Path(plan.output))

    missing_actions = [action for action in plan.required_human_actions if not has_human_action(ctx, action)]
    if not dry_run and missing_actions and not force:
        error = "missing required human action acknowledgement(s): " + ", ".join(missing_actions)
        result = ProbeMatchResult(
            kind=plan.kind,
            iteration=plan.iteration,
            dry_run=dry_run,
            simulated=simulate,
            ok=False,
            command=plan.command,
            returncode=None,
            stdout="",
            stderr="",
            error=error,
            correction=str(correction),
            result_path=str(result_path),
            instrument_inventory=None,
        )
        result_path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
        return result

    EventWriter(ctx.events_path).write(
        "INFO",
        "probe_match",
        "probe_match_execute_started",
        kind=plan.kind,
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        plan=str(plan_path),
    )

    returncode: int | None = None
    stdout = ""
    stderr = ""
    error = ""
    instrument_inventory = None
    if simulate:
        correction.parent.mkdir(parents=True, exist_ok=True)
        correction.write_text(
            "\n".join(
                [
                    "# Synthetic probe-match correction",
                    f"# kind={plan.kind}",
                    f"# mode={plan.mode}",
                    f"# iteration={plan.iteration}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        returncode = 0
    elif not dry_run:
        instrument_inventory = probe_match_instrument_inventory(
            plan,
            instrument_enumerator=instrument_enumerator,
        )
        EventWriter(ctx.events_path).write(
            "INFO" if instrument_inventory.get("ok") else "ERROR",
            "probe_match",
            "probe_match_instrument_inventory",
            **instrument_inventory,
        )
        if instrument_inventory.get("ok") is not True:
            error = str(instrument_inventory.get("reason", "probe-match instrument inventory failed"))
        else:
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
                    "probe_match",
                    "probe_match_ccxxmake_started",
                    kind=plan.kind,
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
                        "ERROR" if error else "INFO",
                        "probe_match",
                        "probe_match_ccxxmake_finished",
                        kind=plan.kind,
                        iteration=plan.iteration,
                        argv=plan.command_argv,
                        pid=proc.pid,
                        returncode=returncode,
                        timed_out=timed_out,
                        stdout=str(stdout_path),
                        stderr=str(stderr_path),
                    )
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if returncode == 0 and not correction.exists():
            error = f"ccxxmake completed but expected correction was not found: {correction}"

    ok = dry_run or (returncode == 0 and not error)
    result = ProbeMatchResult(
        kind=plan.kind,
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        ok=ok,
        command=plan.command,
        returncode=returncode,
        stdout="" if dry_run else str(stdout_path),
        stderr="" if dry_run else str(stderr_path),
        error=error,
        correction=str(correction),
        result_path=str(result_path),
        instrument_inventory=instrument_inventory,
    )
    result_path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "probe_match",
            "iteration": plan.iteration,
            "status": "execute_dry_run" if dry_run else ("completed" if ok else "failed"),
            "simulated": simulate,
            "kind": plan.kind,
            "plan": str(plan_path),
            "execution_result": str(result_path),
            "correction": str(correction),
            "instrument_inventory": instrument_inventory,
        }
    )
    if ok and not dry_run:
        ctx.manifest.desktoplut["probe_match_correction"] = str(correction)
    ctx.save()
    ctx.log(f"{'Dry-ran' if dry_run else 'Executed'} {plan.kind.upper()} probe match iteration {plan.iteration}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "probe_match",
        "probe_match_execute_finished",
        kind=plan.kind,
        iteration=plan.iteration,
        dry_run=dry_run,
        simulated=simulate,
        ok=ok,
        correction=str(correction),
        result=str(result_path),
        error=error,
        instrument_inventory=instrument_inventory,
    )
    return result

