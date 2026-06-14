"""First live-demo readiness checklist."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .argyll import Argyll
from .desktoplut_client import DesktopLutApiError, DesktopLutClient
from .desktoplut_mock import MockDesktopLutTransport
from .human_actions import has_human_action
from .preflight import build_tool_preflight_payload
from .runs import RunContext, open_run
from .selftest import latest_self_test_status
from .tools import discover_tools


@dataclass(frozen=True)
class DemoCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _instrument_kind(description: str) -> str:
    text = description.lower()
    if "display" in text or "colorimeter" in text:
        return "colorimeter"
    if "spectro" in text or "colorchecker" in text or "i1pro" in text or "i1 pro" in text:
        return "spectrometer"
    if "colormunki" in text and "display" not in text:
        return "spectrometer"
    return "unknown"


def _instrument_inventory() -> tuple[list[dict[str, Any]], str | None]:
    tools = discover_tools()
    if not tools.spotread.ok or tools.spotread.path is None:
        return [], "spotread.exe is missing"
    try:
        instruments = Argyll(tools.spotread.path).enumerate_instruments()
    except Exception as exc:  # pragma: no cover - defensive boundary around external exe
        return [], str(exc)
    return [
        {
            "port": instrument.port,
            "description": instrument.description,
            "kind": _instrument_kind(instrument.description),
        }
        for instrument in instruments
    ], None


def _desktoplut_probe(*, mock_desktoplut: bool) -> dict[str, Any]:
    client = DesktopLutClient(transport=MockDesktopLutTransport()) if mock_desktoplut else DesktopLutClient()
    try:
        response = client.send(client.state_get(), raise_on_error=False)
    except (OSError, DesktopLutApiError) as exc:
        return {"ok": False, "mock": mock_desktoplut, "error": str(exc)}
    return {"ok": response.ok, "mock": mock_desktoplut, "response": response.as_dict()}


def _latest_windows_local_audit(run: Path | None, label: str) -> dict[str, Any] | None:
    if run is None:
        return None
    try:
        ctx = open_run(run)
    except FileNotFoundError:
        return None
    audits = ctx.manifest.desktoplut.get("windows_local_audits")
    if isinstance(audits, dict) and isinstance(audits.get(label), dict):
        return audits[label]
    path = ctx.root / "preflight" / f"windows_local_audit_{label}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _live_setup(run: Path | None) -> dict[str, Any] | None:
    if run is None:
        return None
    try:
        ctx = open_run(run)
    except FileNotFoundError:
        return None
    setup = ctx.manifest.desktoplut.get("live_setup")
    return setup if isinstance(setup, dict) else None


def _run_context(run: Path | None) -> RunContext | None:
    if run is None:
        return None
    try:
        return open_run(run)
    except FileNotFoundError:
        return None


def _audit_cautions(windows_audit: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(windows_audit, dict):
        return []
    findings = windows_audit.get("findings")
    if not isinstance(findings, list):
        return []
    cautions = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity", ""))
        if severity in {"", "ok"}:
            continue
        cautions.append(
            {
                "severity": severity,
                "name": finding.get("name"),
                "detail": finding.get("detail"),
                "evidence": _compact_caution_evidence(finding.get("evidence", {})),
            }
        )
    return cautions


def _compact_caution_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    compact = {key: value for key, value in evidence.items() if key != "disallowed"}
    disallowed = evidence.get("disallowed")
    if isinstance(disallowed, list):
        compact["disallowed_count"] = len(disallowed)
        compact["disallowed_samples"] = [
            {
                "key": item.get("key"),
                "name": item.get("name"),
                "profile_name": item.get("profile_name"),
            }
            for item in disallowed[:3]
            if isinstance(item, dict)
        ]
    return compact


def _operator_actions(
    *,
    ctx: RunContext | None,
    run: Path | None,
    probe_match: bool,
    port: int | None,
    mock_desktoplut: bool,
) -> list[dict[str, Any]]:
    if ctx is None or run is None:
        return []
    actions = []
    if probe_match:
        acknowledged = has_human_action(ctx, "spectro_placed")
        actions.append(
            {
                "action": "spectro_placed",
                "required": True,
                "acknowledged": acknowledged,
                "command": _ack_handoff_command(
                    run=run,
                    action="spectro_placed",
                    instrument="ColorChecker Studio",
                    port=port,
                    mock_desktoplut=mock_desktoplut,
                ),
                "reason": "Place the spectrometer for the optional probe-match reference before ccxxmake runs.",
            }
        )
    acknowledged = has_human_action(ctx, "colorimeter_placed")
    actions.append(
        {
            "action": "colorimeter_placed",
            "required": True,
            "acknowledged": acknowledged,
            "command": _ack_handoff_command(
                run=run,
                action="colorimeter_placed",
                instrument="i1 Display Pro",
                port=port,
                mock_desktoplut=mock_desktoplut,
            ),
            "reason": "Place the colorimeter at screen center for unattended Argyll measurements.",
        }
    )
    return actions


def _ack_handoff_command(*, run: Path, action: str, instrument: str, port: int | None, mock_desktoplut: bool) -> str:
    command = f'python -m dlc.cli ack --run {run} --action {action} --instrument "{instrument}" --compact-handoff --execute-safe --allow-hardware'
    if port is not None:
        command += f" --port {port}"
    command += " --mock-desktoplut" if mock_desktoplut else " --allow-live-desktoplut"
    return command


def _next_operator_action(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for action in actions:
        if action.get("required") is True and action.get("acknowledged") is not True:
            return action
    return None


def _suggested_commands(
    *,
    run: Path | None,
    port: int | None,
    monitor_hint: str | None,
    probe_match: bool,
    mock_desktoplut: bool,
) -> dict[str, str]:
    port_value = str(port if port is not None else "PORT")
    run_value = str(run) if run is not None else "RUN"
    run_unattended = f"python -m dlc.cli run-unattended --run {run_value} --port {port_value} --execute-safe --allow-hardware --update-dashboard"
    if mock_desktoplut:
        run_unattended += " --mock-desktoplut"
    else:
        run_unattended += " --allow-live-desktoplut"
    commands = {
        "write_vendor_manifest": "python -m dlc.cli vendor-tools --manifest-existing",
        "preflight": "python -m dlc.cli preflight" + (f" --run {run_value}" if run is not None else ""),
        "self_test": f"python -m dlc.cli self-test --port {port_value}",
        "instruments": "python -m dlc.cli instruments",
        "live_setup": f"python -m dlc.cli live-setup --run {run_value} --meter-port {port_value}",
        "windows_local_audit": f"python -m dlc.cli windows-local-audit --run {run_value}",
        "readiness": f"python -m dlc.cli readiness --run {run_value} --port {port_value} --execute-safe --mock-desktoplut --allow-hardware",
        "run_unattended": run_unattended,
    }
    if mock_desktoplut:
        commands["run_live_hardware_mock_desktoplut"] = run_unattended
    else:
        commands["run_live_hardware_live_desktoplut"] = run_unattended
        commands["readiness"] = f"python -m dlc.cli readiness --run {run_value} --port {port_value} --execute-safe --allow-hardware --allow-live-desktoplut"
    if monitor_hint:
        commands["live_setup"] += f" --monitor-hint {monitor_hint}"
        commands["windows_local_audit"] += f" --monitor-hint {monitor_hint}"
        commands["run_unattended"] += f" --windows-monitor-hint {monitor_hint}"
        if mock_desktoplut:
            commands["run_live_hardware_mock_desktoplut"] = commands["run_unattended"]
        else:
            commands["run_live_hardware_live_desktoplut"] = commands["run_unattended"]
    if probe_match:
        commands["self_test"] += " --probe-match --probe-match-display-tech u --probe-match-high-res"
        commands["live_setup"] += " --probe-match --probe-match-display-tech u --probe-match-high-res"
    return commands


def build_demo_readiness(
    *,
    run: Path | None = None,
    port: int | None = None,
    monitor_hint: str | None = None,
    probe_match: bool = False,
    mock_desktoplut: bool = True,
    self_test_max_age_hours: float = 24.0,
) -> dict[str, Any]:
    tools = discover_tools()
    preflight = build_tool_preflight_payload(tools, artifact=Path("preflight") / "tool_preflight.json")
    instruments, instrument_error = _instrument_inventory()
    colorimeters = [item for item in instruments if item["kind"] == "colorimeter"]
    spectrometers = [item for item in instruments if item["kind"] == "spectrometer"]
    self_test = latest_self_test_status(max_age_hours=self_test_max_age_hours, require_probe_match=probe_match)
    desktoplut = _desktoplut_probe(mock_desktoplut=mock_desktoplut)
    setup = _live_setup(run)
    windows_audit = _latest_windows_local_audit(run, "preflight")
    ctx = _run_context(run)
    operator_actions = _operator_actions(ctx=ctx, run=run, probe_match=probe_match, port=port, mock_desktoplut=mock_desktoplut)
    cautions = _audit_cautions(windows_audit)

    checks = [
        DemoCheck(
            "contained_tool_preflight",
            bool(
                preflight.get("required_ready")
                and preflight.get("contained_ready")
                and preflight.get("contained_paths_ready")
                and preflight.get("vendor_manifest_ready")
            ),
            "Contained tools, fingerprints, dummy ICCs, and vendor manifest are ready.",
            evidence={
                "required_ready": preflight.get("required_ready"),
                "contained_ready": preflight.get("contained_ready"),
                "contained_paths_ready": preflight.get("contained_paths_ready"),
                "vendor_manifest_ready": preflight.get("vendor_manifest_ready"),
                "missing_required": preflight.get("missing_required"),
                "missing_contained": preflight.get("missing_contained"),
            },
        ),
        DemoCheck(
            "colorimeter_connected",
            bool(colorimeters),
            "At least one colorimeter is visible to Argyll.",
            evidence={"instruments": instruments, "error": instrument_error},
        ),
        DemoCheck(
            "spectrometer_connected",
            (not probe_match) or bool(spectrometers),
            "A spectrometer is visible when probe matching is requested.",
            required=probe_match,
            evidence={"instruments": instruments, "error": instrument_error},
        ),
        DemoCheck(
            "fresh_self_test",
            bool(self_test.get("ok")),
            "A recent passing self-test marker exists for the requested branch.",
            evidence=self_test,
        ),
        DemoCheck(
            "desktoplut_api",
            bool(desktoplut.get("ok")),
            "DesktopLUT API is reachable, or mock DesktopLUT is selected for the first live-hardware demo.",
            evidence=desktoplut,
        ),
        DemoCheck(
            "live_setup_recorded",
            run is None or setup is not None,
            "The run has a live setup manifest.",
            required=run is not None,
            evidence={"run": str(run) if run else None, "live_setup": setup},
        ),
        DemoCheck(
            "windows_local_audit_recorded",
            run is None or bool(windows_audit and windows_audit.get("ok") is True),
            "The run has a passing local Windows ICC/gamma audit.",
            required=run is not None,
            evidence={"run": str(run) if run else None, "audit": windows_audit},
        ),
    ]
    required_checks = [check for check in checks if check.required]
    ok = all(check.ok for check in required_checks)
    return {
        "ok": ok,
        "target": "live hardware measurement with mock DesktopLUT" if mock_desktoplut else "live hardware and live DesktopLUT",
        "run": str(run) if run else None,
        "port": port,
        "monitor_hint": monitor_hint,
        "probe_match": probe_match,
        "mock_desktoplut": mock_desktoplut,
        "checks": [check.as_dict() for check in checks],
        "operator_actions": operator_actions,
        "next_operator_action": _next_operator_action(operator_actions),
        "cautions": cautions,
        "caution_count": len(cautions),
        "suggested_commands": _suggested_commands(run=run, port=port, monitor_hint=monitor_hint, probe_match=probe_match, mock_desktoplut=mock_desktoplut),
    }
