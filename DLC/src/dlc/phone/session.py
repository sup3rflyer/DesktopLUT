"""One verified phone capture: record -> pull -> verify the file -> write a sidecar manifest.

The manifest (``<label>.json`` next to ``<label>.mov``) records what the sensor was *actually* asked to do (camera
service read-back taken ~1 s into the clip), what the app displayed, what the file really is (ffprobe), host-side
timing of the start/stop key events and the phone/host clock offset - so a clip is never separated from the
conditions it was shot under, and a silently-wrong mode (8-bit 30 fps instead of 10-bit 120 fps) is flagged, not
trusted.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import clip as clipmod
from .adb import Adb
from .mcpro import MEDIA_DIR, Mcpro, McproError

MIN_FREE_GB = 3.0


@dataclass
class Capture:
    label: str
    local: Path
    manifest: Path
    info: clipmod.ClipInfo
    problems: list[str] = field(default_factory=list)
    during: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


class PhoneRig:
    def __init__(self, out_dir: str | Path, adb: Adb | None = None):
        self.out_dir = Path(out_dir)
        self.adb = adb or Adb()
        self.cam = Mcpro(self.adb)

    def preflight(self, min_free_gb: float = MIN_FREE_GB) -> dict:
        """Device present, enough storage, app up. Returns the facts (also written into each manifest)."""
        self.adb.ensure_device()
        free = self.adb.free_gb()
        if free < min_free_gb:
            raise McproError(f"phone has {free:.1f} GB free (< {min_free_gb} GB) - clear space before recording")
        batt = self.adb.battery_pct()
        self.cam.ensure_running()
        return dict(free_gb=round(free, 2), battery_pct=batt)

    def capture(self, label: str, seconds: float, *, expect: dict | None = None, delete_remote: bool = True,
                keep_going: bool = False) -> Capture:
        """Record ``seconds`` and return a verified :class:`Capture`.

        ``expect`` keys (all optional) feed :func:`clip.check`: ``fps``, ``bits``, ``size``, ``codec``, ``max_gaps``.
        Problems do not raise (the caller - the LLM - decides); they are in ``Capture.problems`` and the manifest.
        """
        pre = self.preflight()
        before = self.cam.state()
        offset, unc = self.adb.clock_offset()
        during: dict = {}

        def snap() -> None:
            during.update(self.cam.state(with_camera=True).as_dict())

        name, t0, t1 = self.cam.record(seconds, on_recording=snap)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        local = self.out_dir / f"{label}{Path(name).suffix}"
        self.adb.pull(f"{MEDIA_DIR}/{name}", local)
        info = clipmod.probe(local)
        problems = clipmod.check(info, **(expect or {}))
        if before.camera.high_speed != during.get("high_speed", before.camera.high_speed):
            problems.append("session mode changed between preflight and recording")
        manifest = self.out_dir / f"{label}.json"
        manifest.write_text(json.dumps(dict(
            label=label, requested_s=seconds, remote=f"{MEDIA_DIR}/{name}", local=str(local),
            host_key_start=t0, host_key_stop=t1, phone_minus_host_s=offset, clock_uncertainty_s=unc,
            preflight=pre, state_before=before.as_dict(), state_during=during,
            clip=dict(summary=info.summary(), codec=info.codec, profile=info.profile, pix_fmt=info.pix_fmt, bits=info.bits,
                      size=[info.width, info.height], fps_container=info.fps, fps_measured=info.measured_fps,
                      frames=info.frames, duration_s=info.duration_s, dt_ms=[info.dt_ms_min, info.dt_ms_median,
                                                                           info.dt_ms_max],
                      gaps=info.gaps, first_gap_ms=info.first_gap_ms, color_transfer=info.color_transfer,
                      app_tags=info.app_tags),
            expect=expect or {}, problems=problems, captured_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ), indent=1), encoding="utf-8")
        if delete_remote and (not problems or not keep_going):
            self.adb.rm(f"{MEDIA_DIR}/{name}")
        return Capture(label=label, local=local, manifest=manifest, info=info, problems=problems, during=during)
