"""Machine-readable DesktopLUT API contract for parent-app implementation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import DEFAULT_PIPE_NAME


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
            "mhc.set_base_grayscale",
            "Set the MHC base grayscale (per-channel 1D tone correction) for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "point_count": ApiParamSpec("integer", description="Number of grayscale control points."),
                "points": ApiParamSpec("array", description="Ascending input levels in [0,1]."),
                "deviations": ApiParamSpec(
                    "object", description="Per-channel multiplicative deviations centered at 1.0: {r:[],g:[],b:[]}."
                ),
            },
            {"monitor_mode": "string", "mhc": "object"},
            mutates_state=True,
            gui_thread_required=True,
        ),
        ApiMethodSpec(
            "mhc.set_correction_grayscale",
            "Set the MHC correction grayscale (the refinement layer composed on top of the base) "
            "for the target monitor/mode.",
            {
                "monitor": _monitor_param(),
                "mode": _mode_param(),
                "point_count": ApiParamSpec("integer", description="Number of grayscale control points."),
                "points": ApiParamSpec("array", description="Ascending input levels in [0,1]."),
                "deviations": ApiParamSpec(
                    "object", description="Per-channel multiplicative deviations centered at 1.0: {r:[],g:[],b:[]}."
                ),
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

    # The per-phase acceptance sequence DLC runs against the live pipe (and the
    # mock). Final contract: MHC is staged via primaries + white + base grayscale
    # (no 1D-cube import), then applied/verified, then the runtime 3D LUT is set.
    _m, _mode = 0, "SDR"
    _grayscale = {"point_count": 2, "points": [0.0, 1.0], "deviations": {"r": [1.0, 1.0], "g": [1.0, 1.0], "b": [1.0, 1.0]}}
    sequence_steps = [
        ("initial_state", "state.get", {}),
        (
            "enter",
            "calibration.enter",
            {"monitor": _m, "mode": _mode, "dummy_icc_path": r"<DLC>\third_party\argyll\3.3.0\ref\sRGB.icm", "reason": "DLC contract check"},
        ),
        ("primaries", "mhc.set_primaries", {"monitor": _m, "mode": _mode, "primaries": {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}}),
        ("white", "mhc.set_white", {"monitor": _m, "mode": _mode, "x": 0.3127, "y": 0.3290}),
        ("base_grayscale", "mhc.set_base_grayscale", {"monitor": _m, "mode": _mode, **_grayscale}),
        ("apply_mhc", "mhc.apply", {"monitor": _m, "mode": _mode}),
        ("verify_mhc", "maintenance.verify_mhc", {"monitor": _m, "mode": _mode}),
        ("runtime_3dlut", "runtime.set_3dlut", {"monitor": _m, "mode": _mode, "cube_path": r"RUN\generated\final.cube"}),
        ("final_state", "state.get", {}),
    ]
    sequence = [{"step": step, "request": {"method": method, "params": params}} for step, method, params in sequence_steps]

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
            "corrections_enabled=false after calibration.enter",
            "mhc entry for 0:SDR has applied=true",
            "maintenance.verify_mhc reports verified=true",
            "runtime entry for 0:SDR has a cube_path",
        ],
    }


def write_desktoplut_api_spec(output: Path) -> dict[str, Any]:
    spec = build_desktoplut_api_spec()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec

