"""Local Windows color-state audit helpers."""

from __future__ import annotations

import ctypes
import json
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .events import EventWriter
from .runs import RunContext


PROFILE_ASSOCIATIONS_ROOT = r"Software\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display"
DEFAULT_ALLOWED_PROFILE_NAMES = {"sRGB Gamma22.icc", "sRGB.icm", "Rec2020.icm"}


@dataclass(frozen=True)
class WindowsLocalFinding:
    severity: str
    name: str
    detail: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WindowsLocalAudit:
    ok: bool
    label: str
    monitor_hint: str | None
    registry: dict[str, Any]
    gamma_ramp: dict[str, Any]
    findings: list[WindowsLocalFinding]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["findings"] = [finding.as_dict() for finding in self.findings]
        return payload


def profile_name(value: Any) -> str:
    text = str(value)
    return Path(text.replace("\\", "/")).name


def is_allowed_profile(value: Any, allowed_profile_names: set[str] | None = None) -> bool:
    allowed = {name.lower() for name in (allowed_profile_names or DEFAULT_ALLOWED_PROFILE_NAMES)}
    return profile_name(value).lower() in allowed


def _entry_matches_hint(entry: dict[str, Any], monitor_hint: str | None) -> bool:
    if not monitor_hint:
        return True
    hint = monitor_hint.lower()
    return hint in str(entry.get("key", "")).lower() or hint in str(entry.get("value", "")).lower()


def read_profile_associations(monitor_hint: str | None = None) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {
            "available": False,
            "root": PROFILE_ASSOCIATIONS_ROOT,
            "monitor_hint": monitor_hint,
            "entries": [],
            "matched_entries": [],
            "error": "Windows registry is only available on Windows",
        }
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError as exc:
        return {
            "available": False,
            "root": PROFILE_ASSOCIATIONS_ROOT,
            "monitor_hint": monitor_hint,
            "entries": [],
            "matched_entries": [],
            "error": str(exc),
        }

    entries: list[dict[str, Any]] = []

    def walk(key: Any, subpath: str) -> None:
        index = 0
        while True:
            try:
                name, value, value_type = winreg.EnumValue(key, index)
            except OSError:
                break
            entries.append(
                {
                    "key": subpath,
                    "name": name,
                    "value": str(value),
                    "profile_name": profile_name(value),
                    "type": int(value_type),
                }
            )
            index += 1
        subkey_index = 0
        while True:
            try:
                child = winreg.EnumKey(key, subkey_index)
            except OSError:
                break
            child_path = f"{subpath}\\{child}" if subpath else child
            try:
                with winreg.OpenKey(key, child) as child_key:
                    walk(child_key, child_path)
            except OSError:
                entries.append({"key": child_path, "name": None, "value": None, "profile_name": "", "type": None})
            subkey_index += 1

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROFILE_ASSOCIATIONS_ROOT) as root:
            walk(root, PROFILE_ASSOCIATIONS_ROOT)
    except OSError as exc:
        return {
            "available": True,
            "root": PROFILE_ASSOCIATIONS_ROOT,
            "monitor_hint": monitor_hint,
            "entries": [],
            "matched_entries": [],
            "error": str(exc),
        }

    matched = [entry for entry in entries if _entry_matches_hint(entry, monitor_hint)]
    return {
        "available": True,
        "root": PROFILE_ASSOCIATIONS_ROOT,
        "monitor_hint": monitor_hint,
        "entries": entries,
        "matched_entries": matched,
        "error": None,
    }


def expected_identity_gamma_value(index: int) -> int:
    return round(index * 65535 / 255)


def evaluate_gamma_ramp_identity(channels: list[list[int]], tolerance: int = 257) -> dict[str, Any]:
    if len(channels) != 3 or any(len(channel) != 256 for channel in channels):
        return {
            "identity": False,
            "tolerance": tolerance,
            "max_abs_delta": None,
            "error": "gamma ramp must contain three 256-entry channels",
        }
    max_abs_delta = 0
    worst: dict[str, Any] = {"channel": None, "index": None, "actual": None, "expected": None}
    for channel_index, channel in enumerate(channels):
        for index, actual in enumerate(channel):
            expected = expected_identity_gamma_value(index)
            delta = abs(int(actual) - expected)
            if delta > max_abs_delta:
                max_abs_delta = delta
                worst = {"channel": channel_index, "index": index, "actual": int(actual), "expected": expected}
    return {
        "identity": max_abs_delta <= tolerance,
        "tolerance": tolerance,
        "max_abs_delta": max_abs_delta,
        "worst": worst,
        "error": None,
    }


def capture_desktop_gamma_ramp(tolerance: int = 257) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {
            "available": False,
            "identity": None,
            "tolerance": tolerance,
            "error": "GetDeviceGammaRamp is only available on Windows",
        }
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        hdc = user32.GetDC(None)
        if not hdc:
            raise OSError(ctypes.get_last_error(), "GetDC failed")
        try:
            Ramp = ctypes.c_ushort * (3 * 256)
            raw = Ramp()
            if not gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(raw)):
                raise OSError(ctypes.get_last_error(), "GetDeviceGammaRamp failed")
        finally:
            user32.ReleaseDC(None, hdc)
    except OSError as exc:
        return {"available": False, "identity": None, "tolerance": tolerance, "error": str(exc)}

    channels = [[int(raw[channel * 256 + index]) for index in range(256)] for channel in range(3)]
    evaluation = evaluate_gamma_ramp_identity(channels, tolerance=tolerance)
    samples = {
        "red": [channels[0][0], channels[0][64], channels[0][128], channels[0][192], channels[0][255]],
        "green": [channels[1][0], channels[1][64], channels[1][128], channels[1][192], channels[1][255]],
        "blue": [channels[2][0], channels[2][64], channels[2][128], channels[2][192], channels[2][255]],
    }
    return {"available": True, "samples": samples, **evaluation}


def evaluate_windows_local_state(
    *,
    monitor_hint: str | None = None,
    allowed_profile_names: set[str] | None = None,
    gamma_tolerance: int = 257,
    registry: dict[str, Any] | None = None,
    gamma_ramp: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[WindowsLocalFinding]]:
    registry_payload = registry or read_profile_associations(monitor_hint)
    gamma_payload = gamma_ramp or capture_desktop_gamma_ramp(tolerance=gamma_tolerance)
    findings: list[WindowsLocalFinding] = []

    if not registry_payload.get("available"):
        findings.append(
            WindowsLocalFinding(
                "warning",
                "registry_unavailable",
                "Windows ICC association registry data could not be read.",
                {"error": registry_payload.get("error")},
            )
        )
    else:
        entries = registry_payload.get("matched_entries") or []
        disallowed = [entry for entry in entries if entry.get("value") and not is_allowed_profile(entry.get("value"), allowed_profile_names)]
        findings.append(
            WindowsLocalFinding(
                "ok" if not disallowed else "warning",
                "profile_associations",
                "No non-benign ICC association strings matched the monitor hint."
                if not disallowed
                else "Non-benign ICC association strings are present; verify calibration mode replaces them before raw profiling.",
                {"monitor_hint": monitor_hint, "disallowed": disallowed, "matched_count": len(entries)},
            )
        )

    if not gamma_payload.get("available"):
        findings.append(
            WindowsLocalFinding(
                "warning",
                "gamma_ramp_unavailable",
                "Desktop gamma ramp could not be read locally.",
                {"error": gamma_payload.get("error")},
            )
        )
    elif gamma_payload.get("identity") is True:
        findings.append(
            WindowsLocalFinding(
                "ok",
                "gamma_ramp_identity",
                "Desktop gamma ramp appears identity within tolerance.",
                {"max_abs_delta": gamma_payload.get("max_abs_delta"), "tolerance": gamma_payload.get("tolerance")},
            )
        )
    else:
        findings.append(
            WindowsLocalFinding(
                "blocker",
                "gamma_ramp_not_identity",
                "Desktop gamma ramp is not identity; clear VCGT/video-LUT state before raw profiling.",
                {"max_abs_delta": gamma_payload.get("max_abs_delta"), "tolerance": gamma_payload.get("tolerance"), "worst": gamma_payload.get("worst")},
            )
        )

    return registry_payload, gamma_payload, findings


def write_windows_local_audit(
    *,
    ctx: RunContext,
    label: str = "preflight",
    monitor_hint: str | None = None,
    allowed_profile_names: set[str] | None = None,
    gamma_tolerance: int = 257,
    registry: dict[str, Any] | None = None,
    gamma_ramp: dict[str, Any] | None = None,
    output: Path | None = None,
) -> WindowsLocalAudit:
    registry_payload, gamma_payload, findings = evaluate_windows_local_state(
        monitor_hint=monitor_hint,
        allowed_profile_names=allowed_profile_names,
        gamma_tolerance=gamma_tolerance,
        registry=registry,
        gamma_ramp=gamma_ramp,
    )
    ok = not any(finding.severity == "blocker" for finding in findings)
    output_dir = ctx.root / "preflight"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output or output_dir / f"windows_local_audit_{label}.json"
    audit = WindowsLocalAudit(
        ok=ok,
        label=label,
        monitor_hint=monitor_hint,
        registry=registry_payload,
        gamma_ramp=gamma_payload,
        findings=findings,
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(audit.as_dict(), indent=2), encoding="utf-8")

    captures = ctx.manifest.desktoplut.setdefault("windows_local_audits", {})
    if isinstance(captures, dict):
        captures[label] = audit.as_dict()
    ctx.manifest.stages.append(
        {
            "stage": "windows_local_audit",
            "status": "passed" if ok else "blocked",
            "label": label,
            "monitor_hint": monitor_hint,
            "artifact": str(artifact),
        }
    )
    ctx.save()
    ctx.log(f"Windows local audit {label}: {'ok' if ok else 'blocked'}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "WARNING",
        "windows_local_audit",
        "windows_local_audit_written",
        ok=ok,
        label=label,
        monitor_hint=monitor_hint,
        artifact=str(artifact),
    )
    return audit

