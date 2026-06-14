"""Windows color-state capture through the DesktopLUT API."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .desktoplut_client import DesktopLutClient
from .events import EventWriter
from .runs import RunContext


@dataclass(frozen=True)
class WindowsColorStateCapture:
    ok: bool
    label: str
    monitor: int | None
    profiles: dict[str, Any]
    gamma_ramp: dict[str, Any]
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_windows_color_state(
    *,
    ctx: RunContext,
    client: DesktopLutClient,
    label: str = "final",
    monitor: int | None = None,
) -> WindowsColorStateCapture:
    profiles = client.send(client.windows_query_profiles(monitor), raise_on_error=False)
    gamma_ramp = client.send(client.windows_query_gamma_ramp(monitor), raise_on_error=False)
    ok = profiles.ok and gamma_ramp.ok

    output_dir = ctx.root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"windows_color_state_{label}.json"
    capture = WindowsColorStateCapture(
        ok=ok,
        label=label,
        monitor=monitor,
        profiles=profiles.as_dict(),
        gamma_ramp=gamma_ramp.as_dict(),
        artifact=str(artifact),
    )
    artifact.write_text(json.dumps(capture.as_dict(), indent=2), encoding="utf-8")

    captures = ctx.manifest.desktoplut.setdefault("windows_state_captures", {})
    if isinstance(captures, dict):
        captures[label] = capture.as_dict()
    ctx.manifest.stages.append(
        {
            "stage": "windows_color_state_capture",
            "status": "captured" if ok else "failed",
            "label": label,
            "monitor": monitor,
            "artifact": str(artifact),
        }
    )
    ctx.save()
    ctx.log(f"Windows color-state capture {label}: {'ok' if ok else 'failed'}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "windows_color_state",
        "windows_color_state_captured",
        label=label,
        monitor=monitor,
        ok=ok,
        artifact=str(artifact),
    )
    return capture

