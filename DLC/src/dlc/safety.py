"""Supervisor safety gates shared by CLI, readiness, and dashboard."""

from __future__ import annotations

from typing import Protocol


class ActionLike(Protocol):
    status: str
    action: str


SAFE_ACTIONS = {
    "plan_probe_match",
    "plan_raw_mhc_drift",
    "plan_raw_mhc_drift_sequence",
    "plan_raw_mhc",
    "build_mhc_baseline",
    "build_mhc_iteration",
    "plan_mhc_verification_drift",
    "plan_mhc_verification_drift_sequence",
    "plan_mhc_verification",
    "plan_mhc_verification_iteration",
    "score_mhc_iteration",
    "decide_mhc_iteration",
    "plan_post_mhc_drift",
    "plan_post_mhc_drift_sequence",
    "plan_post_mhc",
    "plan_post_mhc_iteration",
    "plan_3dlut",
    "plan_3dlut_iteration",
    "plan_3dlut_verification_drift",
    "plan_3dlut_verification_drift_sequence",
    "plan_3dlut_verification",
    "plan_3dlut_verification_iteration",
    "score_3dlut_iteration",
    "check_3dlut_integrity",
    "decide_3dlut_iteration",
    "capture_final_desktoplut_state",
    "capture_final_windows_color_state",
    "write_tool_preflight",
    "write_pipeline_evidence",
    "write_report",
    "final_audit",
    "finalize_run",
}

HARDWARE_ACTIONS = {
    "execute_probe_match",
    "execute_raw_mhc",
    "execute_mhc_verification",
    "execute_mhc_verification_iteration",
    "execute_post_mhc",
    "execute_post_mhc_iteration",
    "execute_3dlut_verification",
    "execute_3dlut_verification_iteration",
}

LIVE_DESKTOPLUT_ACTIONS = {
    "desktoplut_contract_check",
    "enter_calibration_mode",
    "apply_mhc_baseline",
    "apply_mhc_iteration",
    "apply_3dlut",
    "apply_3dlut_iteration",
}

LONG_BUILD_ACTIONS = {"execute_3dlut", "execute_3dlut_iteration"}


def blocked_reason_for_action(
    action: ActionLike,
    *,
    execute_safe: bool,
    allow_hardware: bool,
    allow_live_desktoplut: bool,
    allow_builds: bool,
    mock_desktoplut: bool,
    simulate_execution: bool = False,
) -> str | None:
    if action.action == "complete":
        return "run is complete for the current automated slice"
    if action.status != "ready":
        return f"recommendation status is {action.status}"
    if not execute_safe:
        return "dry supervision only; pass --execute-safe to run allowlisted steps"
    if action.action in SAFE_ACTIONS:
        return None
    if action.action in HARDWARE_ACTIONS:
        return None if allow_hardware or simulate_execution else "hardware measurement requires --allow-hardware or --simulate"
    if action.action in LIVE_DESKTOPLUT_ACTIONS:
        if mock_desktoplut or allow_live_desktoplut:
            return None
        return "live DesktopLUT mutation requires --mock-desktoplut or --allow-live-desktoplut"
    if action.action in LONG_BUILD_ACTIONS:
        return None if allow_builds or simulate_execution else "long 3D LUT build requires --allow-builds or --simulate"
    return f"action is not in the supervisor allowlist: {action.action}"

