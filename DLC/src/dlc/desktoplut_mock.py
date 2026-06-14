"""In-process DesktopLUT API simulator for DLC development."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .desktoplut_client import DesktopLutCommand, DesktopLutResponse, DesktopLutTransport


@dataclass
class MockDesktopLutState:
    running: bool = True
    corrections_enabled: bool = True
    calibration_mode: dict[str, Any] | None = None
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    mhc: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    command_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "corrections_enabled": self.corrections_enabled,
            "calibration_mode": deepcopy(self.calibration_mode),
            "snapshots": deepcopy(self.snapshots),
            "mhc": deepcopy(self.mhc),
            "runtime": deepcopy(self.runtime),
            "command_count": self.command_count,
        }


class MockDesktopLutServer:
    """Small command handler matching the DesktopLUT API contract."""

    def __init__(self) -> None:
        self.state = MockDesktopLutState()

    def handle(self, command: DesktopLutCommand) -> DesktopLutResponse:
        self.state.command_count += 1
        params = command.params or {}
        method = command.method
        try:
            if method == "state.get":
                return self.ok(self.state.as_dict())
            if method == "state.snapshot":
                snapshot_id = f"snapshot-{len(self.state.snapshots) + 1}"
                self.state.snapshots[snapshot_id] = self.state.as_dict()
                return self.ok({"snapshot_id": snapshot_id})
            if method == "state.restore":
                return self.restore(str(params.get("snapshot_id", "")))
            if method == "corrections.disable_all":
                self.state.corrections_enabled = False
                return self.ok({"corrections_enabled": False})
            if method.startswith("calibration."):
                return self.handle_calibration(method, params)
            if method.startswith("mhc."):
                return self.handle_mhc(method, params)
            if method.startswith("runtime."):
                return self.handle_runtime(method, params)
            if method == "maintenance.verify_mhc":
                return self.ok({"verified": bool(self.state.mhc.get(self.key(params)))})
            if method == "windows.query_profiles":
                return self.ok(
                    {
                        "available": False,
                        "simulated": True,
                        "monitor": params.get("monitor"),
                        "profiles": [],
                        "active_profile": None,
                    }
                )
            if method == "windows.query_gamma_ramp":
                return self.ok(
                    {
                        "available": False,
                        "simulated": True,
                        "monitor": params.get("monitor"),
                        "gamma_ramp_loaded": None,
                        "vcgt_present": None,
                    }
                )
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        except KeyError as exc:
            return DesktopLutResponse(ok=False, error=f"missing parameter: {exc.args[0]}")

    def ok(self, result: dict[str, Any]) -> DesktopLutResponse:
        return DesktopLutResponse(ok=True, result=result)

    def key(self, params: dict[str, Any]) -> str:
        return f"{params['monitor']}:{str(params['mode']).upper()}"

    def restore(self, snapshot_id: str) -> DesktopLutResponse:
        if snapshot_id not in self.state.snapshots:
            return DesktopLutResponse(ok=False, error=f"unknown snapshot: {snapshot_id}")
        snapshot = deepcopy(self.state.snapshots[snapshot_id])
        self.state.corrections_enabled = bool(snapshot.get("corrections_enabled", True))
        self.state.calibration_mode = deepcopy(snapshot.get("calibration_mode"))
        self.state.mhc = deepcopy(snapshot.get("mhc", {}))
        self.state.runtime = deepcopy(snapshot.get("runtime", {}))
        return self.ok({"snapshot_id": snapshot_id, "restored": True})

    def handle_calibration(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        if method == "calibration.status":
            return self.ok({"active": self.state.calibration_mode is not None, "state": deepcopy(self.state.calibration_mode)})
        if method == "calibration.enter":
            snapshot_id = f"snapshot-{len(self.state.snapshots) + 1}"
            self.state.snapshots[snapshot_id] = self.state.as_dict()
            self.state.corrections_enabled = False
            self.state.mhc.clear()
            self.state.runtime.clear()
            self.state.calibration_mode = {
                "active": True,
                "snapshot_id": snapshot_id,
                "monitor": params["monitor"],
                "mode": str(params["mode"]).upper(),
                "dummy_icc_path": params["dummy_icc_path"],
                "reason": params.get("reason", ""),
                "corrections_reset": True,
            }
            return self.ok(deepcopy(self.state.calibration_mode))
        if method == "calibration.exit":
            current = deepcopy(self.state.calibration_mode)
            restore = bool(params.get("restore_snapshot", False))
            if restore and current and current.get("snapshot_id") in self.state.snapshots:
                snapshot_id = str(current["snapshot_id"])
                restored = self.restore(snapshot_id)
                if restored.ok:
                    self.state.calibration_mode = None
                return restored
            self.state.calibration_mode = None
            return self.ok({"active": False, "restored": False})
        return DesktopLutResponse(ok=False, error=f"unknown method: {method}")

    def handle_mhc(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        key = self.key(params)
        state = self.state.mhc.setdefault(key, {})
        if method == "mhc.set_primaries":
            state["primaries"] = deepcopy(params["primaries"])
        elif method == "mhc.set_white":
            state["white"] = {"x": params["x"], "y": params["y"]}
        elif method == "mhc.set_1dlut":
            state["cube_path"] = params["cube_path"]
        elif method == "mhc.apply":
            state["applied"] = True
        elif method == "mhc.remove":
            self.state.mhc.pop(key, None)
            return self.ok({"monitor_mode": key, "removed": True})
        else:
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        return self.ok({"monitor_mode": key, "mhc": deepcopy(self.state.mhc.get(key, {}))})

    def handle_runtime(self, method: str, params: dict[str, Any]) -> DesktopLutResponse:
        key = self.key(params)
        state = self.state.runtime.setdefault(key, {})
        if method == "runtime.set_3dlut":
            state["cube_path"] = params["cube_path"]
        elif method == "runtime.clear_3dlut":
            state.pop("cube_path", None)
        elif method == "runtime.set_grayscale_tweak":
            state["grayscale_tweak"] = deepcopy(params.get("grayscale_tweak", {}))
        elif method == "runtime.disable_grayscale_tweak":
            state.pop("grayscale_tweak", None)
        else:
            return DesktopLutResponse(ok=False, error=f"unknown method: {method}")
        return self.ok({"monitor_mode": key, "runtime": deepcopy(self.state.runtime.get(key, {}))})


class MockDesktopLutTransport:
    def __init__(self, server: MockDesktopLutServer | None = None) -> None:
        self.server = server or MockDesktopLutServer()
        self.requests: list[DesktopLutCommand] = []

    def request(self, command: DesktopLutCommand) -> DesktopLutResponse:
        self.requests.append(command)
        return self.server.handle(command)

