"""Client contract for DesktopLUT's local calibration API."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


DEFAULT_PIPE_NAME = r"\\.\pipe\DesktopLUT.Calibration"


@dataclass(frozen=True)
class DesktopLutCommand:
    method: str
    params: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "params": self.params or {}}

    def encode(self) -> bytes:
        return encode_message(self.as_dict())


@dataclass(frozen=True)
class DesktopLutResponse:
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DesktopLutResponse":
        return cls(
            ok=bool(payload.get("ok")),
            result=payload.get("result"),
            error=payload.get("error"),
        )


class DesktopLutApiError(RuntimeError):
    """Raised when DesktopLUT returns an error response."""


class DesktopLutTransport(Protocol):
    def request(self, command: DesktopLutCommand) -> DesktopLutResponse:
        ...


def encode_message(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


class NamedPipeTransport:
    """One-request-per-connection NDJSON transport for DesktopLUT.

    On Windows, named pipes can be opened through the normal file API when the
    server has created the pipe. The DesktopLUT-side server does not exist yet;
    this transport is the client contract DLC will use once it does.
    """

    def __init__(self, pipe_name: str = DEFAULT_PIPE_NAME) -> None:
        self.pipe_name = pipe_name

    def request(self, command: DesktopLutCommand) -> DesktopLutResponse:
        with open(self.pipe_name, "r+b", buffering=0) as pipe:
            pipe.write(command.encode())
            raw = pipe.readline()
        if not raw:
            raise DesktopLutApiError("DesktopLUT API returned an empty response")
        return DesktopLutResponse.from_dict(decode_message(raw))


class JsonlFileTransport:
    """Append-only file transport useful for contract diagnostics.

    It records requests as NDJSON and returns a synthetic success response. The
    real simulator lives in `desktoplut_mock.py`; this transport is intentionally
    dumb and file-based.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def request(self, command: DesktopLutCommand) -> DesktopLutResponse:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(command.encode())
        return DesktopLutResponse(ok=True, result={"recorded": str(self.path), "method": command.method})


class DesktopLutClient:
    """Client for DesktopLUT's calibration control API."""

    def __init__(self, pipe_name: str = DEFAULT_PIPE_NAME, transport: DesktopLutTransport | None = None) -> None:
        self.pipe_name = pipe_name
        self.transport = transport or NamedPipeTransport(pipe_name)

    def send(self, command: DesktopLutCommand, *, raise_on_error: bool = True) -> DesktopLutResponse:
        response = self.transport.request(command)
        if raise_on_error and not response.ok:
            raise DesktopLutApiError(response.error or f"DesktopLUT API command failed: {command.method}")
        return response

    def call(self, method: str, params: dict[str, Any] | None = None) -> DesktopLutResponse:
        return self.send(DesktopLutCommand(method, params))

    def state_get(self) -> DesktopLutCommand:
        return DesktopLutCommand("state.get")

    def snapshot(self) -> DesktopLutCommand:
        return DesktopLutCommand("state.snapshot")

    def restore(self, snapshot_id: str | None = None) -> DesktopLutCommand:
        params = {"snapshot_id": snapshot_id} if snapshot_id else {}
        return DesktopLutCommand("state.restore", params)

    def disable_all(self) -> DesktopLutCommand:
        return DesktopLutCommand("corrections.disable_all")

    def enter_calibration_mode(
        self,
        monitor: int,
        mode: str,
        dummy_icc_path: str,
        reason: str = "DesktopLUT Calibrator run",
    ) -> DesktopLutCommand:
        return DesktopLutCommand(
            "calibration.enter",
            {
                "monitor": monitor,
                "mode": mode,
                "dummy_icc_path": dummy_icc_path,
                "reason": reason,
            },
        )

    def calibration_status(self) -> DesktopLutCommand:
        return DesktopLutCommand("calibration.status")

    def exit_calibration_mode(self, restore_snapshot: bool = False) -> DesktopLutCommand:
        return DesktopLutCommand("calibration.exit", {"restore_snapshot": restore_snapshot})

    def set_mhc_1dlut(self, monitor: int, mode: str, cube_path: str) -> DesktopLutCommand:
        return DesktopLutCommand(
            "mhc.set_1dlut",
            {"monitor": monitor, "mode": mode, "cube_path": cube_path},
        )

    def set_mhc_primaries(self, monitor: int, mode: str, primaries: dict[str, float]) -> DesktopLutCommand:
        return DesktopLutCommand(
            "mhc.set_primaries",
            {"monitor": monitor, "mode": mode, "primaries": primaries},
        )

    def set_mhc_white(self, monitor: int, mode: str, x: float, y: float) -> DesktopLutCommand:
        return DesktopLutCommand(
            "mhc.set_white",
            {"monitor": monitor, "mode": mode, "x": x, "y": y},
        )

    def apply_mhc(self, monitor: int, mode: str) -> DesktopLutCommand:
        return DesktopLutCommand("mhc.apply", {"monitor": monitor, "mode": mode})

    def remove_mhc(self, monitor: int, mode: str) -> DesktopLutCommand:
        return DesktopLutCommand("mhc.remove", {"monitor": monitor, "mode": mode})

    def set_3dlut(self, monitor: int, mode: str, cube_path: str) -> DesktopLutCommand:
        return DesktopLutCommand(
            "runtime.set_3dlut",
            {"monitor": monitor, "mode": mode, "cube_path": cube_path},
        )

    def clear_3dlut(self, monitor: int, mode: str) -> DesktopLutCommand:
        return DesktopLutCommand("runtime.clear_3dlut", {"monitor": monitor, "mode": mode})

    def set_grayscale_tweak(self, monitor: int, mode: str, grayscale_tweak: dict[str, Any]) -> DesktopLutCommand:
        return DesktopLutCommand(
            "runtime.set_grayscale_tweak",
            {"monitor": monitor, "mode": mode, "grayscale_tweak": grayscale_tweak},
        )

    def disable_grayscale_tweak(self, monitor: int, mode: str) -> DesktopLutCommand:
        return DesktopLutCommand("runtime.disable_grayscale_tweak", {"monitor": monitor, "mode": mode})

    def verify_mhc(self, monitor: int, mode: str) -> DesktopLutCommand:
        return DesktopLutCommand("maintenance.verify_mhc", {"monitor": monitor, "mode": mode})

    def windows_query_profiles(self, monitor: int | None = None) -> DesktopLutCommand:
        params = {"monitor": monitor} if monitor is not None else {}
        return DesktopLutCommand("windows.query_profiles", params)

    def windows_query_gamma_ramp(self, monitor: int | None = None) -> DesktopLutCommand:
        params = {"monitor": monitor} if monitor is not None else {}
        return DesktopLutCommand("windows.query_gamma_ramp", params)

