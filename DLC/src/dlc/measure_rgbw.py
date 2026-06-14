"""RGBW probe-match measurement stage."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .argyll import Argyll, Instrument, SpotreadRequest, command_for_log, parse_xyz, parse_yxy
from .dogegen import DogegenPatchDisplay, RGBW_HDR, RGBW_SDR
from .events import EventWriter
from .patch_presenter import PresenterEvent, build_rgbw_sequence, preview_sequence, run_tk_presenter, write_patch_sequence
from .runs import RunContext


@dataclass(frozen=True)
class RgbwPatch:
    name: str
    rgb: tuple[int, int, int]


@dataclass
class PatchMeasurement:
    patch: RgbwPatch
    xyz: tuple[float, float, float] | None = None
    yxy: tuple[float, float, float] | None = None
    spectral_file: str | None = None
    stdout_file: str | None = None
    stderr_file: str | None = None
    command: str | None = None
    returncode: int | None = None
    error: str | None = None


@dataclass
class RgbwMeasurementResult:
    mode: str
    instrument_port: int
    high_res: bool
    display_type: str | None
    patch_size: int
    dry_run: bool
    presenter: str = "dogegen"
    patch_sequence: str | None = None
    instrument_resolution: dict[str, Any] | None = None
    setup_error: str | None = None
    measurements: list[PatchMeasurement] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.dry_run:
            return True
        if self.setup_error:
            return False
        if not self.measurements:
            return False
        return all(m.error is None and m.yxy is not None for m in self.measurements)


def rgbw_patches(mode: str) -> list[RgbwPatch]:
    source = RGBW_SDR if mode.upper() == "SDR" else RGBW_HDR
    return [RgbwPatch(name=name, rgb=rgb) for name, rgb in source.items()]


def resolve_spotread_instrument_port(
    spotread: Argyll,
    planned_port: int,
    *,
    instrument_enumerator: Callable[[], list[Instrument]] | None = None,
) -> tuple[int, dict[str, Any]]:
    evidence: dict[str, Any] = {
        "ok": True,
        "applicable": True,
        "changed": False,
        "planned_port": planned_port,
        "resolved_port": planned_port,
        "instrument_count": None,
        "instruments": [],
        "reason": "planned port is currently attached",
    }
    enumerator = instrument_enumerator
    if enumerator is None and hasattr(spotread, "enumerate_instruments"):
        enumerator = spotread.enumerate_instruments
    if enumerator is None:
        evidence.update({"applicable": False, "reason": "spotread instrument enumeration is not available"})
        return planned_port, evidence
    try:
        instruments = enumerator()
    except Exception as exc:
        evidence.update({"ok": False, "reason": f"could not enumerate instruments: {exc}"})
        return planned_port, evidence
    evidence.update({"instrument_count": len(instruments), "instruments": [asdict(instrument) for instrument in instruments]})
    if any(instrument.port == planned_port for instrument in instruments):
        return planned_port, evidence
    if len(instruments) == 1:
        resolved_port = instruments[0].port
        evidence.update(
            {
                "changed": True,
                "resolved_port": resolved_port,
                "reason": "planned port was stale; selected the only currently attached instrument",
            }
        )
        return resolved_port, evidence
    if instruments:
        evidence.update(
            {
                "ok": False,
                "reason": "planned port is not attached and multiple instruments are present; refusing ambiguous RGBW measurement",
            }
        )
    else:
        evidence.update({"ok": False, "reason": "no Argyll instruments are currently attached"})
    return planned_port, evidence


def plan_rgbw_measurement(
    mode: str,
    spotread: Argyll,
    dogegen: DogegenPatchDisplay | None,
    port: int,
    output_dir: Path,
    patch_size: int = 100,
    spectral: bool = False,
    high_res: bool = False,
    display_type: str | None = None,
    presenter: str = "dogegen",
    patch_sequence: Path | None = None,
) -> RgbwMeasurementResult:
    result = RgbwMeasurementResult(
        mode=mode.upper(),
        instrument_port=port,
        high_res=high_res,
        display_type=display_type,
        patch_size=patch_size,
        dry_run=True,
        presenter=presenter,
        patch_sequence=str(patch_sequence) if patch_sequence else None,
    )
    if presenter == "dogegen":
        if dogegen is None:
            raise ValueError("Dogegen presenter selected but dogegen is not available")
        result.commands.append(command_for_log([str(dogegen.executable), dogegen.startup_mode]))
    elif presenter == "dlc":
        if patch_sequence is None:
            raise ValueError("DLC presenter planning requires a patch sequence path")
        result.commands.append(f"dlc patch-presenter --sequence {patch_sequence} --execute")
    else:
        raise ValueError(f"unknown presenter: {presenter}")

    for patch in rgbw_patches(mode):
        r, g, b = patch.rgb
        if presenter == "dogegen":
            result.commands.append(f"dogegen: window {patch_size} {r} {g} {b}")
        else:
            result.commands.append(f"dlc-presenter: patch {patch.name} {r} {g} {b}")
        request = _spotread_request(
            patch=patch,
            port=port,
            output_dir=output_dir,
            spectral=spectral,
            high_res=high_res,
            display_type=display_type,
        )
        result.commands.append(command_for_log(spotread.spotread_command(request)))
    if presenter == "dlc":
        result.commands.append("dlc-presenter: complete")
    else:
        result.commands.append("dogegen: quit")
    return result


def run_rgbw_measurement(
    ctx: RunContext,
    spotread: Argyll,
    dogegen: DogegenPatchDisplay | None,
    port: int,
    patch_size: int = 100,
    spectral: bool = False,
    high_res: bool = False,
    display_type: str | None = None,
    dry_run: bool = True,
    presenter: str = "dogegen",
    presenter_runner: Callable[[Any, Callable[[PresenterEvent], None]], list[PresenterEvent]] | None = None,
    instrument_enumerator: Callable[[], list[Instrument]] | None = None,
) -> RgbwMeasurementResult:
    output_dir = ctx.root / "probe_match"
    output_dir.mkdir(exist_ok=True)
    events = EventWriter(ctx.events_path)
    events.write(
        "INFO",
        "probe_match_rgbw",
        "started",
        mode=ctx.manifest.mode,
        port=port,
        dry_run=dry_run,
        spectral=spectral,
        presenter=presenter,
    )

    resolved_port = port
    instrument_resolution = None
    if not dry_run:
        resolved_port, instrument_resolution = resolve_spotread_instrument_port(
            spotread,
            port,
            instrument_enumerator=instrument_enumerator,
        )
        events.write(
            "INFO" if instrument_resolution.get("ok") else "ERROR",
            "probe_match_rgbw",
            "instrument_resolution",
            **instrument_resolution,
        )
        if instrument_resolution.get("ok") is not True:
            result = RgbwMeasurementResult(
                mode=ctx.manifest.mode,
                instrument_port=port,
                high_res=high_res,
                display_type=display_type,
                patch_size=patch_size,
                dry_run=False,
                presenter=presenter,
                instrument_resolution=instrument_resolution,
                setup_error=str(instrument_resolution.get("reason", "instrument resolution failed")),
            )
            write_rgbw_result(result, output_dir / "rgbw_measurements.json")
            events.write("WARN", "probe_match_rgbw", "completed", ok=False, setup_error=result.setup_error)
            return result

    if dry_run:
        sequence_path = None
        if presenter == "dlc":
            sequence = build_rgbw_sequence(
                mode=ctx.manifest.mode,
                patch_size_percent=patch_size,
                duration_seconds=0.5,
            )
            sequence_path = write_patch_sequence(ctx=ctx, sequence=sequence, stage="probe_match")
            preview_sequence(sequence)
        result = plan_rgbw_measurement(
            ctx.manifest.mode,
            spotread,
            dogegen,
            port,
            output_dir,
            patch_size,
            spectral,
            high_res,
            display_type,
            presenter=presenter,
            patch_sequence=sequence_path,
        )
        write_rgbw_result(result, output_dir / "rgbw_plan.json")
        events.write("INFO", "probe_match_rgbw", "planned", command_count=len(result.commands))
        return result

    if presenter != "dogegen":
        return _run_rgbw_measurement_with_dlc_presenter(
            ctx=ctx,
            spotread=spotread,
            port=resolved_port,
            patch_size=patch_size,
            spectral=spectral,
            high_res=high_res,
            display_type=display_type,
            instrument_resolution=instrument_resolution,
            presenter_runner=presenter_runner,
            events=events,
        )
    if dogegen is None:
        raise ValueError("Dogegen presenter selected but dogegen is not available")

    result = RgbwMeasurementResult(
        mode=ctx.manifest.mode,
        instrument_port=resolved_port,
        high_res=high_res,
        display_type=display_type,
        patch_size=patch_size,
        dry_run=False,
        presenter=presenter,
        instrument_resolution=instrument_resolution,
    )

    proc = dogegen.start()
    try:
        for patch in rgbw_patches(ctx.manifest.mode):
            r, g, b = patch.rgb
            patch_command = f"window {patch_size} {r} {g} {b}"
            DogegenPatchDisplay.send(proc, patch_command, settle_seconds=0.5)
            request = _spotread_request(
                patch=patch,
                port=resolved_port,
                output_dir=output_dir,
                spectral=spectral,
                high_res=high_res,
                display_type=display_type,
            )
            measurement = PatchMeasurement(
                patch=patch,
                command=command_for_log(spotread.spotread_command(request)),
            )
            events.write("INFO", "probe_match_rgbw", "patch_displayed", patch=patch.name, rgb=patch.rgb)
            completed = spotread.run_spotread_once(request)
            measurement.returncode = completed.returncode
            measurement.stdout_file = str(output_dir / f"{patch.name}_spotread_stdout.txt")
            measurement.stderr_file = str(output_dir / f"{patch.name}_spotread_stderr.txt")
            Path(measurement.stdout_file).write_text(completed.stdout or "", encoding="utf-8")
            Path(measurement.stderr_file).write_text(completed.stderr or "", encoding="utf-8")
            combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
            measurement.xyz = parse_xyz(combined)
            measurement.yxy = parse_yxy(combined)
            if request.output_sp:
                measurement.spectral_file = str(request.output_sp)
            if completed.returncode != 0:
                measurement.error = f"spotread exited with {completed.returncode}"
            elif measurement.yxy is None:
                measurement.error = "spotread output did not contain Yxy"
            result.measurements.append(measurement)
            events.write(
                "INFO" if measurement.error is None else "WARN",
                "probe_match_rgbw",
                "patch_measured",
                patch=patch.name,
                yxy=measurement.yxy,
                xyz=measurement.xyz,
                error=measurement.error,
            )
    finally:
        try:
            DogegenPatchDisplay.send(proc, "quit", settle_seconds=0.1)
            proc.wait(timeout=2)
        except Exception:
            proc.terminate()

    write_rgbw_result(result, output_dir / "rgbw_measurements.json")
    events.write("INFO" if result.ok else "WARN", "probe_match_rgbw", "completed", ok=result.ok)
    return result


def _measure_patch(
    *,
    spotread: Argyll,
    patch: RgbwPatch,
    port: int,
    output_dir: Path,
    spectral: bool,
    high_res: bool,
    display_type: str | None,
) -> PatchMeasurement:
    request = _spotread_request(
        patch=patch,
        port=port,
        output_dir=output_dir,
        spectral=spectral,
        high_res=high_res,
        display_type=display_type,
    )
    measurement = PatchMeasurement(
        patch=patch,
        command=command_for_log(spotread.spotread_command(request)),
    )
    completed = spotread.run_spotread_once(request)
    measurement.returncode = completed.returncode
    measurement.stdout_file = str(output_dir / f"{patch.name}_spotread_stdout.txt")
    measurement.stderr_file = str(output_dir / f"{patch.name}_spotread_stderr.txt")
    Path(measurement.stdout_file).write_text(completed.stdout or "", encoding="utf-8")
    Path(measurement.stderr_file).write_text(completed.stderr or "", encoding="utf-8")
    combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
    measurement.xyz = parse_xyz(combined)
    measurement.yxy = parse_yxy(combined)
    if request.output_sp:
        measurement.spectral_file = str(request.output_sp)
    if completed.returncode != 0:
        measurement.error = f"spotread exited with {completed.returncode}"
    elif measurement.yxy is None:
        measurement.error = "spotread output did not contain Yxy"
    return measurement


def _run_rgbw_measurement_with_dlc_presenter(
    *,
    ctx: RunContext,
    spotread: Argyll,
    port: int,
    patch_size: int,
    spectral: bool,
    high_res: bool,
    display_type: str | None,
    instrument_resolution: dict[str, Any] | None,
    presenter_runner: Callable[[Any, Callable[[PresenterEvent], None]], list[PresenterEvent]] | None,
    events: EventWriter,
) -> RgbwMeasurementResult:
    output_dir = ctx.root / "probe_match"
    sequence = build_rgbw_sequence(
        mode=ctx.manifest.mode,
        patch_size_percent=patch_size,
        duration_seconds=0.5,
    )
    sequence_path = write_patch_sequence(ctx=ctx, sequence=sequence, stage="probe_match")
    patch_by_name = {patch.name: patch for patch in rgbw_patches(ctx.manifest.mode)}
    result = RgbwMeasurementResult(
        mode=ctx.manifest.mode,
        instrument_port=port,
        high_res=high_res,
        display_type=display_type,
        patch_size=patch_size,
        dry_run=False,
        presenter="dlc",
        patch_sequence=str(sequence_path),
        instrument_resolution=instrument_resolution,
    )

    runner = presenter_runner or run_tk_presenter

    def on_patch(event: PresenterEvent) -> None:
        patch = patch_by_name.get(event.name)
        if patch is None:
            return
        events.write("INFO", "probe_match_rgbw", "patch_displayed", patch=patch.name, rgb=patch.rgb, presenter="dlc")
        measurement = _measure_patch(
            spotread=spotread,
            patch=patch,
            port=port,
            output_dir=output_dir,
            spectral=spectral,
            high_res=high_res,
            display_type=display_type,
        )
        result.measurements.append(measurement)
        events.write(
            "INFO" if measurement.error is None else "WARN",
            "probe_match_rgbw",
            "patch_measured",
            patch=patch.name,
            yxy=measurement.yxy,
            xyz=measurement.xyz,
            error=measurement.error,
            presenter="dlc",
        )

    runner(sequence, on_patch)
    write_rgbw_result(result, output_dir / "rgbw_measurements.json")
    events.write("INFO" if result.ok else "WARN", "probe_match_rgbw", "completed", ok=result.ok, presenter="dlc")
    return result


def write_rgbw_result(result: RgbwMeasurementResult, path: Path) -> None:
    path.write_text(json.dumps(_result_to_json(result), indent=2), encoding="utf-8")


def _result_to_json(result: RgbwMeasurementResult) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "instrument_port": result.instrument_port,
        "high_res": result.high_res,
        "display_type": result.display_type,
        "patch_size": result.patch_size,
        "dry_run": result.dry_run,
        "presenter": result.presenter,
        "patch_sequence": result.patch_sequence,
        "instrument_resolution": result.instrument_resolution,
        "setup_error": result.setup_error,
        "ok": result.ok,
        "commands": result.commands,
        "measurements": [asdict(measurement) for measurement in result.measurements],
    }


def _spotread_request(
    patch: RgbwPatch,
    port: int,
    output_dir: Path,
    spectral: bool,
    high_res: bool,
    display_type: str | None,
) -> SpotreadRequest:
    return SpotreadRequest(
        port=port,
        output_sp=(output_dir / f"{patch.name}.sp") if spectral else None,
        logfile=(output_dir / f"{patch.name}_spotread_log.txt") if spectral else None,
        high_res=high_res,
        display_type=display_type,
    )

