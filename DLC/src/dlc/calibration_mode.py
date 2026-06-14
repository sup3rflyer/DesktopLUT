"""Calibration-mode evidence helpers shared by live gates and final audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .profiles import default_dummy_icc, resolve_profile_path
from .runs import RunContext


def _state_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    state = result.get("state")
    if isinstance(state, dict):
        return state
    return result


def _resolved_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    return resolve_profile_path(Path(value))


def calibration_mode_evidence(ctx: RunContext) -> dict[str, Any]:
    payload = ctx.manifest.desktoplut.get("calibration_mode")
    state = _state_from_payload(payload)
    expected_dummy = default_dummy_icc(ctx.manifest.mode)
    dummy_path = _resolved_path(state.get("dummy_icc_path"))
    active = isinstance(payload, dict) and payload.get("ok") is True and state.get("active") is True
    dummy_recorded = dummy_path is not None
    dummy_exists = dummy_path.exists() if dummy_path is not None else False
    dummy_expected = dummy_path == expected_dummy.path if dummy_path is not None else False
    corrections_reset = state.get("corrections_reset") is True
    ok = active and dummy_recorded and dummy_exists and dummy_expected and corrections_reset
    missing = []
    if not active:
        missing.append("active")
    if not dummy_recorded:
        missing.append("dummy_icc_path")
    elif not dummy_exists:
        missing.append("dummy_icc_exists")
    elif not dummy_expected:
        missing.append("dummy_icc_matches_mode_default")
    if not corrections_reset:
        missing.append("corrections_reset")
    return {
        "ok": ok,
        "active": active,
        "dummy_icc_path": str(dummy_path) if dummy_path else None,
        "expected_dummy_icc_path": str(expected_dummy.path),
        "dummy_icc_exists": dummy_exists,
        "dummy_icc_matches_mode_default": dummy_expected,
        "corrections_reset": corrections_reset,
        "missing": missing,
        "payload_recorded": isinstance(payload, dict),
    }

