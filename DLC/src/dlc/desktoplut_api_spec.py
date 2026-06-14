"""Machine-readable DesktopLUT API contract for parent-app implementation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import DEFAULT_PIPE_NAME, DesktopLutClient
from .desktoplut_contract import _contract_commands


@dataclass(frozen=True)
class ApiParamSpec:
    type: str
    required: bool = True
    description: str = ""
    values: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.values is None:
            payload.pop("values")
        return payload


@dataclass(frozen=True)
class ApiMethodSpec:
    method: str
    purpose: str
    params: dict[str, ApiParamSpec]
    result: dict[str, str]
    mutates_state: bool
    gui_thread_required: bool

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["params"] = {name: spec.as_dict() for name, spec in self.params.items()}
        return payload


def _mode_param() -> ApiParamSpec:
    return ApiParamSpec("string", description="Target DesktopLUT mode.", values=["SDR", "HDR"])


def _monitor_param() -> ApiParamSpec:
    return ApiParamSpec("integer", description="Zero-based DesktopLUT monitor index.")


def build_desktoplut_api_spec() -> dict[str, Any]:
    """Return the API contract DLC expects the DesktopLUT pipe server to expose."""

    methods = [
        ApiMethodSpec(
            "state.get",
            "Return current DesktopLUT automation state.",
            {},
            {
                "running": "boolean",
                "corrections_enabled": "boolean",
                "calibration_mode": "object or null",
                "mhc": "object keyed by '<monitor>:<MODE>'",
                "runtime": "object keyed by '<monitor>:<MODE>'",
            },
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "state.snapshot",
            "Capture enough DesktopLUT runtime/settings state to restore later.",
            {},
            {"snapshot_id": "string"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "state.restore",
            "Restore a previously captured DesktopLUT snapshot.",
            {"snapshot_id": ApiParamSpec("string", required=False, description="Snapshot id returned by state.snapshot or calibration.enter.")},
            {"snapshot_id": "string", "restored": "boolean"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "corrections.disable_all",
            "Disable all runtime correction layers for a clean measurement baseline.",
            {},
            {"corrections_enabled": "boolean false"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "calibration.enter",
            "Enter calibration mode: snapshot state, associate dummy ICC, and reset correction layers.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "dummy_icc_path": ApiParamSpec("string", description="Absolute path to the neutral/dummy ICC DLC wants associated."),
                "reason": ApiParamSpec("string", required=False, description="Human-readable reason recorded by DesktopLUT."),
            },
            {
                "active": "boolean true",
                "snapshot_id": "string",
                "monitor": "integer",
                "mode": "string",
                "dummy_icc_path": "string",
                "corrections_reset": "boolean true",
            },
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "calibration.status",
            "Return current calibration-mode bookkeeping.",
            {},
            {"active": "boolean", "state": "object or null"},
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "calibration.exit",
            "Exit calibration mode, optionally restoring the calibration snapshot.",
            {"restore_snapshot": ApiParamSpec("boolean", required=False, description="Restore the snapshot captured by calibration.enter.")},
            {"active": "boolean false", "restored": "boolean"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_primaries",
            "Set measured/native MHC primaries for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "primaries": ApiParamSpec("object", description="Chromaticity object with rx, ry, gx, gy, bx, by."),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_white",
            "Set MHC white point target or measured white for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "x": ApiParamSpec("number", description="CIE x chromaticity."),
                "y": ApiParamSpec("number", description="CIE y chromaticity."),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_1dlut",
            "Load the MHC profile grayscale/1D LUT cube for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "cube_path": ApiParamSpec("string", description="Absolute or run-relative path to a 1D cube file."),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.apply",
            "Apply the staged MHC settings to DesktopLUT.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "mhc": "object with applied=true"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.remove",
            "Remove active MHC settings for the target monitor/mode.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "removed": "boolean true"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "maintenance.verify_mhc",
            "Report whether DesktopLUT has a coherent applied MHC state.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"verified": "boolean"},
            mutates_state=False,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.set_3dlut",
            "Load the runtime 3D LUT cube for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "cube_path": ApiParamSpec("string", description="Absolute or run-relative path to a 3D cube file."),
            },
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.clear_3dlut",
            "Clear the runtime 3D LUT for the target monitor/mode.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.set_grayscale_tweak",
            "Set the separate runtime grayscale tweak layer.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "grayscale_tweak": ApiParamSpec("object", description="DesktopLUT runtime tweak payload."),
            },
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "runtime.disable_grayscale_tweak",
            "Disable the separate runtime grayscale tweak layer.",
            {"monitor": _monitor_param(), "mode": _mode_param()},
            {"monitor_mode": "string", "runtime": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "windows.query_profiles",
            "Return active Windows ICC/profile association data for the target monitor.",
            {"monitor": ApiParamSpec("integer", required=False, description="Zero-based DesktopLUT monitor index.")},
            {"available": "boolean", "profiles": "array", "active_profile": "string or null"},
            mutates_state=False,
            gui_thread_required=False,
        ),
        ApiMethodSpec(
            "windows.query_gamma_ramp",
            "Return Windows gamma ramp/VCGT state for the target monitor.",
            {"monitor": ApiParamSpec("integer", required=False, description="Zero-based DesktopLUT monitor index.")},
            {"available": "boolean", "gamma_ramp_loaded": "boolean or null", "vcgt_present": "boolean or null"},
            mutates_state=False,
            gui_thread_required=False,
        ),
    ]

    client = DesktopLutClient()
    sequence = [
        {"step": name, "request": command.as_dict()}
        for name, command in _contract_commands(
            client,
            monitor=0,
            mode="SDR",
            dummy_icc_path=r"<DLC>\third_party\argyll\3.3.0\ref\sRGB.icm",
            mhc_lut_path=r"RUN\generated\desktoplut_contract_contract_1dlut.cube",
            runtime_lut_path=r"RUN\generated\desktoplut_contract_contract_3dlut.cube",
        )
    ]

    return {
        "name": "DesktopLUT Calibrator API",
        "version": 1,
        "transport": {
            "default_pipe": DEFAULT_PIPE_NAME,
            "framing": "one UTF-8 JSON object per line; one request per named-pipe connection",
            "request_envelope": {"method": "string", "params": "object"},
            "response_envelope": {"ok": "boolean", "result": "object when ok", "error": "string when not ok"},
        },
        "threading": {
            "pipe_thread": "decode request and marshal GUI mutations",
            "gui_thread": "perform DesktopLUT state/settings mutations for gui_thread_required methods",
        },
        "methods": [method.as_dict() for method in methods],
        "contract_check_sequence": sequence,
        "final_state_checks": [
            "all commands return ok=true",
            "state.get reports running=true",
            "calibration_mode is active after calibration.enter",
            "corrections_enabled=false after disable_all/calibration.enter",
            "mhc entry for 0:SDR has applied=true and a cube_path",
            "runtime entry for 0:SDR has a cube_path",
            "windows.query_profiles and windows.query_gamma_ramp return ok=true",
        ],
    }


def write_desktoplut_api_spec(output: Path) -> dict[str, Any]:
    spec = build_desktoplut_api_spec()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec

