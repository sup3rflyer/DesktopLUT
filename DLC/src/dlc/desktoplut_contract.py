"""DesktopLUT API contract checks for calibration automation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import DesktopLutClient, DesktopLutCommand, DesktopLutResponse
from .events import EventWriter
from .runs import RunContext


@dataclass(frozen=True)
class DesktopLutContractStep:
    name: str
    command: dict[str, Any]
    response: dict[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.response.get("ok"))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DesktopLutContractResult:
    ok: bool
    label: str
    monitor: int
    mode: str
    dummy_icc_path: str
    lut_paths: dict[str, str]
    steps: list[DesktopLutContractStep]
    checks: dict[str, bool]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.as_dict() for step in self.steps]
        return payload


def _contract_commands(
    client: DesktopLutClient,
    *,
    monitor: int,
    mode: str,
    dummy_icc_path: str,
    mhc_lut_path: str,
    runtime_lut_path: str,
) -> list[tuple[str, DesktopLutCommand]]:
    return [
        ("initial_state", client.state_get()),
        (
            "calibration_enter",
            client.enter_calibration_mode(
                monitor=monitor,
                mode=mode,
                dummy_icc_path=dummy_icc_path,
                reason="DesktopLUT Calibrator API contract check",
            ),
        ),
        ("disable_all", client.disable_all()),
        (
            "mhc_set_primaries",
            client.set_mhc_primaries(
                monitor,
                mode,
                {
                    "rx": 0.64,
                    "ry": 0.33,
                    "gx": 0.30,
                    "gy": 0.60,
                    "bx": 0.15,
                    "by": 0.06,
                },
            ),
        ),
        ("mhc_set_white", client.set_mhc_white(monitor, mode, 0.3127, 0.3290)),
        ("mhc_set_1dlut", client.set_mhc_1dlut(monitor, mode, mhc_lut_path)),
        ("mhc_apply", client.apply_mhc(monitor, mode)),
        ("mhc_verify", client.verify_mhc(monitor, mode)),
        ("runtime_disable_grayscale_tweak", client.disable_grayscale_tweak(monitor, mode)),
        ("runtime_set_3dlut", client.set_3dlut(monitor, mode, runtime_lut_path)),
        ("windows_query_profiles", client.windows_query_profiles(monitor)),
        ("windows_query_gamma_ramp", client.windows_query_gamma_ramp(monitor)),
        ("final_state", client.state_get()),
    ]


def _response_result(step: DesktopLutContractStep) -> dict[str, Any]:
    result = step.response.get("result")
    return result if isinstance(result, dict) else {}


def _write_identity_1dlut(path: Path, size: int = 17) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        'TITLE "DesktopLUT Calibrator API contract 1D LUT"',
        f"LUT_1D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    for index in range(size):
        value = index / (size - 1)
        lines.append(f"{value:.8f} {value:.8f} {value:.8f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _write_identity_3dlut(path: Path, size: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        'TITLE "DesktopLUT Calibrator API contract 3D LUT"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    scale = size - 1
    for r in range(size):
        for g in range(size):
            for b in range(size):
                lines.append(f"{r / scale:.8f} {g / scale:.8f} {b / scale:.8f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _write_contract_luts(ctx: RunContext, label: str) -> dict[str, str]:
    generated = ctx.root / "generated"
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label)
    mhc_lut = generated / f"desktoplut_contract_{safe_label}_1dlut.cube"
    runtime_lut = generated / f"desktoplut_contract_{safe_label}_3dlut.cube"
    _write_identity_1dlut(mhc_lut)
    _write_identity_3dlut(runtime_lut)
    return {"mhc_1dlut": str(mhc_lut), "runtime_3dlut": str(runtime_lut)}


def _final_state_checks(
    *,
    steps: list[DesktopLutContractStep],
    monitor: int,
    mode: str,
) -> dict[str, bool]:
    by_name = {step.name: step for step in steps}
    final_state = _response_result(by_name["final_state"])
    key = f"{monitor}:{mode.upper()}"
    mhc = final_state.get("mhc") if isinstance(final_state.get("mhc"), dict) else {}
    runtime = final_state.get("runtime") if isinstance(final_state.get("runtime"), dict) else {}
    mhc_state = mhc.get(key) if isinstance(mhc.get(key), dict) else {}
    runtime_state = runtime.get(key) if isinstance(runtime.get(key), dict) else {}
    profiles = by_name["windows_query_profiles"].response
    gamma = by_name["windows_query_gamma_ramp"].response
    return {
        "all_commands_ok": all(step.ok for step in steps),
        "desktoplut_running": final_state.get("running") is True,
        "calibration_mode_active": bool(final_state.get("calibration_mode")),
        "corrections_disabled": final_state.get("corrections_enabled") is False,
        "mhc_applied": bool(mhc_state.get("applied")),
        "mhc_cube_recorded": bool(mhc_state.get("cube_path")),
        "runtime_3dlut_recorded": bool(runtime_state.get("cube_path")),
        "windows_profiles_ok": bool(profiles.get("ok")),
        "windows_gamma_ramp_ok": bool(gamma.get("ok")),
    }


def run_desktoplut_contract_check(
    *,
    ctx: RunContext,
    client: DesktopLutClient,
    dummy_icc_path: str | Path,
    monitor: int = 0,
    mode: str | None = None,
    label: str = "contract",
) -> DesktopLutContractResult:
    contract_mode = (mode or ctx.manifest.mode).upper()
    lut_paths = _write_contract_luts(ctx, label)
    steps: list[DesktopLutContractStep] = []
    for name, command in _contract_commands(
        client,
        monitor=monitor,
        mode=contract_mode,
        dummy_icc_path=str(dummy_icc_path),
        mhc_lut_path=lut_paths["mhc_1dlut"],
        runtime_lut_path=lut_paths["runtime_3dlut"],
    ):
        response: DesktopLutResponse = client.send(command, raise_on_error=False)
        steps.append(DesktopLutContractStep(name=name, command=command.as_dict(), response=response.as_dict()))
        if not response.ok:
            break

    checks = _final_state_checks(steps=steps, monitor=monitor, mode=contract_mode) if steps and steps[-1].name == "final_state" else {
        "all_commands_ok": False,
    }
    ok = all(checks.values())

    output_dir = ctx.root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"desktoplut_contract_{label}.json"
    result = DesktopLutContractResult(
        ok=ok,
        label=label,
        monitor=monitor,
        mode=contract_mode,
        dummy_icc_path=str(dummy_icc_path),
        lut_paths=lut_paths,
        steps=steps,
        checks=checks,
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    checks_by_label = ctx.manifest.desktoplut.setdefault("contract_checks", {})
    if isinstance(checks_by_label, dict):
        checks_by_label[label] = result.as_dict()
    ctx.manifest.stages.append(
        {
            "stage": "desktoplut_contract_check",
            "status": "passed" if ok else "failed",
            "label": label,
            "monitor": monitor,
            "mode": contract_mode,
            "artifacts": {"contract": str(artifact), **lut_paths},
        }
    )
    ctx.save()
    ctx.log(f"DesktopLUT contract check {label}: {'passed' if ok else 'failed'}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "desktoplut_api",
        "contract_check",
        label=label,
        monitor=monitor,
        mode=contract_mode,
        ok=ok,
        artifact=str(artifact),
    )
    return result

