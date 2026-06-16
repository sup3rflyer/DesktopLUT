"""High-level calibration controller.

A thin, typed wrapper over the DesktopLUT NDJSON client that exposes exactly the
operations the calibration harness needs. Every "apply"/"clear"/"refine" in the
harness goes through here, so the rest of DLC never touches the wire protocol.

Transport is swappable: `CalibrationController.mock()` runs against the in-process
simulator for tests and `--simulate` rehearsals; `CalibrationController.connect()`
talks to the real named pipe once the DesktopLUT C++ IPC server is running.
"""

from __future__ import annotations

from typing import Any

from .desktoplut_client import (
    DEFAULT_PIPE_NAME,
    DesktopLutClient,
    DesktopLutTransport,
    NamedPipeTransport,
)


def normalize_mode(mode: str) -> str:
    m = str(mode).upper()
    if m not in ("SDR", "HDR"):
        raise ValueError(f"mode must be SDR or HDR, got {mode!r}")
    return m


class CalibrationController:
    def __init__(self, client: DesktopLutClient) -> None:
        self.client = client

    # -- construction ------------------------------------------------------
    @classmethod
    def connect(cls, pipe_name: str = DEFAULT_PIPE_NAME) -> "CalibrationController":
        return cls(DesktopLutClient(pipe_name, NamedPipeTransport(pipe_name)))

    @classmethod
    def with_transport(cls, transport: DesktopLutTransport) -> "CalibrationController":
        return cls(DesktopLutClient(transport=transport))

    @classmethod
    def mock(cls) -> "CalibrationController":
        """Controller bound to a fresh in-process simulator (tests / --simulate)."""
        from .desktoplut_mock import MockDesktopLutTransport

        return cls.with_transport(MockDesktopLutTransport())

    # -- low-level ---------------------------------------------------------
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.client.call(method, params).result or {}

    # -- state / windows ---------------------------------------------------
    def state(self) -> dict[str, Any]:
        return self.call("state.get")

    def query_profiles(self, monitor: int | None = None) -> dict[str, Any]:
        params = {"monitor": monitor} if monitor is not None else {}
        return self.call("windows.query_profiles", params)

    def query_gamma_ramp(self, monitor: int | None = None) -> dict[str, Any]:
        params = {"monitor": monitor} if monitor is not None else {}
        return self.call("windows.query_gamma_ramp", params)

    # -- calibration mode / clearing --------------------------------------
    def enter_neutral(
        self,
        monitor: int,
        mode: str,
        dummy_icc_path: str,
        reason: str = "DLC calibration run",
    ) -> dict[str, Any]:
        return self.call(
            "calibration.enter",
            {
                "monitor": monitor,
                "mode": normalize_mode(mode),
                "dummy_icc_path": dummy_icc_path,
                "reason": reason,
            },
        )

    def calibration_status(self) -> dict[str, Any]:
        return self.call("calibration.status")

    def exit_calibration(self, restore_snapshot: bool = False) -> dict[str, Any]:
        return self.call("calibration.exit", {"restore_snapshot": restore_snapshot})

    def disable_all(self) -> dict[str, Any]:
        return self.call("corrections.disable_all")

    # -- MHC staging + apply ----------------------------------------------
    def set_primaries(self, monitor: int, mode: str, primaries: dict[str, float]) -> dict[str, Any]:
        return self.call(
            "mhc.set_primaries",
            {"monitor": monitor, "mode": normalize_mode(mode), "primaries": dict(primaries)},
        )

    def set_white(self, monitor: int, mode: str, x: float, y: float) -> dict[str, Any]:
        return self.call(
            "mhc.set_white",
            {"monitor": monitor, "mode": normalize_mode(mode), "x": float(x), "y": float(y)},
        )

    def set_base_grayscale(
        self,
        monitor: int,
        mode: str,
        point_count: int,
        points: list[float],
        deviations: dict[str, list[float]],
    ) -> dict[str, Any]:
        return self.call(
            "mhc.set_base_grayscale",
            {
                "monitor": monitor,
                "mode": normalize_mode(mode),
                "point_count": int(point_count),
                "points": [float(p) for p in points],
                "deviations": _coerce_deviations(deviations),
            },
        )

    def set_correction_grayscale(
        self,
        monitor: int,
        mode: str,
        point_count: int,
        points: list[float],
        deviations: dict[str, list[float]],
    ) -> dict[str, Any]:
        return self.call(
            "mhc.set_correction_grayscale",
            {
                "monitor": monitor,
                "mode": normalize_mode(mode),
                "point_count": int(point_count),
                "points": [float(p) for p in points],
                "deviations": _coerce_deviations(deviations),
            },
        )

    def apply_mhc(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.apply", {"monitor": monitor, "mode": normalize_mode(mode)})

    def remove_mhc(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("mhc.remove", {"monitor": monitor, "mode": normalize_mode(mode)})

    def verify_mhc(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call("maintenance.verify_mhc", {"monitor": monitor, "mode": normalize_mode(mode)})

    # -- runtime 3D LUT ----------------------------------------------------
    def set_3dlut(self, monitor: int, mode: str, cube_path: str) -> dict[str, Any]:
        return self.call(
            "runtime.set_3dlut",
            {"monitor": monitor, "mode": normalize_mode(mode), "cube_path": str(cube_path)},
        )

    def clear_3dlut(self, monitor: int, mode: str) -> dict[str, Any]:
        return self.call(
            "runtime.clear_3dlut",
            {"monitor": monitor, "mode": normalize_mode(mode)},
        )


def _coerce_deviations(deviations: dict[str, list[float]]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for key in ("r", "g", "b"):
        col = deviations.get(key, []) if isinstance(deviations, dict) else []
        out[key] = [float(x) for x in col]
    return out
