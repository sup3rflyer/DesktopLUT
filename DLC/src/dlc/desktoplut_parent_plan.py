"""Generated parent-app implementation plan for DesktopLUT's local API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .desktoplut_api_spec import build_desktoplut_api_spec


PARENT_FILES = [
    "src/desktoplut_ipc_server.h",
    "src/desktoplut_ipc_server.cpp",
    "src/gui.cpp",
    "src/gui.h",
    "src/globals.h",
    "src/globals.cpp",
    "DesktopLUT.vcxproj",
]


SAFE_FIRST_METHODS = {
    "state.get",
    "state.snapshot",
    "state.restore",
    "calibration.enter",
    "calibration.status",
    "calibration.exit",
    "corrections.disable_all",
    "runtime.set_3dlut",
    "runtime.clear_3dlut",
    "runtime.set_grayscale_tweak",
    "runtime.disable_grayscale_tweak",
    "windows.query_profiles",
    "windows.query_gamma_ramp",
}


def _methods(spec: dict[str, Any]) -> list[dict[str, Any]]:
    methods = spec.get("methods")
    return methods if isinstance(methods, list) else []


def build_parent_implementation_plan(parent_root: str = "<DesktopLUT repo>") -> dict[str, Any]:
    spec = build_desktoplut_api_spec()
    methods = _methods(spec)
    gui_methods = [method["method"] for method in methods if method.get("gui_thread_required")]
    pipe_methods = [method["method"] for method in methods if not method.get("gui_thread_required")]
    safe_first = [method["method"] for method in methods if method.get("method") in SAFE_FIRST_METHODS]
    later = [method["method"] for method in methods if method.get("method") not in SAFE_FIRST_METHODS]
    return {
        "name": "DesktopLUT parent API implementation plan",
        "parent_root": parent_root,
        "api_version": spec.get("version"),
        "pipe": spec.get("transport", {}).get("default_pipe"),
        "framing": spec.get("transport", {}).get("framing"),
        "recommended_files": PARENT_FILES,
        "method_count": len(methods),
        "safe_first_methods": safe_first,
        "later_methods": later,
        "gui_thread_methods": gui_methods,
        "pipe_thread_safe_methods": pipe_methods,
        "milestones": [
            {
                "id": "server_lifecycle",
                "title": "Start and stop the named-pipe server with the GUI process",
                "files": ["src/main.cpp", "src/gui.cpp", "src/desktoplut_ipc_server.*", "DesktopLUT.vcxproj"],
                "notes": [
                    "Start the server after GUI initialization succeeds.",
                    "Stop it before process exit and before COM teardown.",
                    "Use one UTF-8 NDJSON request per pipe connection.",
                ],
            },
            {
                "id": "gui_marshal",
                "title": "Marshal mutating commands onto the GUI thread",
                "files": ["src/gui.cpp", "src/gui.h", "src/desktoplut_ipc_server.cpp"],
                "methods": gui_methods,
                "notes": [
                    "Pipe worker threads may parse requests, but must not touch g_gui.monitorSettings directly.",
                    "Use a private WM_APP message or a synchronized command queue and completion event.",
                ],
            },
            {
                "id": "state_and_windows_queries",
                "title": "Implement non-mutating state and Windows query methods",
                "methods": ["state.get", "calibration.status", "windows.query_profiles", "windows.query_gamma_ramp"],
                "notes": [
                    "state.get should expose running, corrections_enabled, calibration_mode, mhc, and runtime maps.",
                    "Windows query responses must return ok=true even when some details are unavailable, with available=false in result.",
                ],
            },
            {
                "id": "calibration_mode",
                "title": "Implement calibration mode snapshot, reset, and exit",
                "methods": ["state.snapshot", "state.restore", "calibration.enter", "calibration.exit", "corrections.disable_all"],
                "notes": [
                    "Snapshot per-monitor settings before reset.",
                    "Associate the requested dummy ICC where possible.",
                    "Reset runtime LUT, runtime grayscale tweak, primaries/white shader correction, and HDR-only tonemapping layers.",
                ],
            },
            {
                "id": "runtime_3dlut",
                "title": "Implement runtime 3D LUT set/clear",
                "methods": ["runtime.set_3dlut", "runtime.clear_3dlut"],
                "notes": [
                    "Validate monitor, mode, and cube parseability before mutating settings.",
                    "Update g_gui.monitorSettings under g_monitorSettingsMutex.",
                    "Save settings and restart/update processing and DWM hook shared config as needed.",
                ],
            },
            {
                "id": "mhc_methods",
                "title": "Implement MHC setters/apply/remove after runtime path is proven",
                "methods": later,
                "notes": [
                    "Keep MHC profile-building concepts separate from runtime grayscale tweak.",
                    "Only report maintenance.verify_mhc=true when the target monitor/mode has coherent applied MHC state.",
                ],
            },
            {
                "id": "live_contract_gate",
                "title": "Pass DLC's live contract check without --mock",
                "commands": [
                    "python -m dlc.cli desktoplut-probe",
                    "python -m dlc.cli desktoplut-contract-check --run RUN",
                    "python -m dlc.cli desktoplut-calibration-mode enter --run RUN --mode SDR",
                    "python -m dlc.cli 3dlut-apply --run RUN --cube RUN\\generated\\3dlut_iter01_sdr.cube",
                ],
            },
        ],
        "contract_check_sequence": spec.get("contract_check_sequence", []),
        "final_state_checks": spec.get("final_state_checks", []),
    }


def render_parent_implementation_plan_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# DesktopLUT Parent API Implementation Plan",
        "",
        f"Parent root: `{plan['parent_root']}`",
        f"Pipe: `{plan['pipe']}`",
        f"API version: `{plan['api_version']}`",
        f"Method count: `{plan['method_count']}`",
        "",
        "## Recommended Files",
        "",
    ]
    lines.extend(f"- `{path}`" for path in plan["recommended_files"])
    lines.extend(["", "## Safe First Methods", ""])
    lines.extend(f"- `{method}`" for method in plan["safe_first_methods"])
    lines.extend(["", "## Later Methods", ""])
    lines.extend(f"- `{method}`" for method in plan["later_methods"])
    lines.extend(["", "## Milestones", ""])
    for milestone in plan["milestones"]:
        lines.append(f"### {milestone['title']}")
        if milestone.get("files"):
            lines.append("")
            lines.append("Files:")
            lines.extend(f"- `{path}`" for path in milestone["files"])
        if milestone.get("methods"):
            lines.append("")
            lines.append("Methods:")
            lines.extend(f"- `{method}`" for method in milestone["methods"])
        if milestone.get("commands"):
            lines.append("")
            lines.append("Verification commands:")
            lines.extend(f"- `{command}`" for command in milestone["commands"])
        if milestone.get("notes"):
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"- {note}" for note in milestone["notes"])
        lines.append("")
    lines.extend(["## Final State Checks", ""])
    lines.extend(f"- {check}" for check in plan["final_state_checks"])
    lines.append("")
    return "\n".join(lines)


def write_parent_implementation_plan(output: Path, *, parent_root: str = "<DesktopLUT repo>") -> dict[str, Any]:
    plan = build_parent_implementation_plan(parent_root=parent_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".json":
        output.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    else:
        output.write_text(render_parent_implementation_plan_markdown(plan), encoding="utf-8")
    return plan

