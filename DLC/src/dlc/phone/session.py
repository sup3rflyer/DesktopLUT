"""PhoneRig - the one object a measurement talks to.

    rig = PhoneRig("results/my_run")
    rig.set(fps=60, iso=400, shutter_s=1/120, wb_k=5600, focus=0.3)      # applied + verified against the camera HAL
    with rig.recording("ladder", fps=60, iso=400, shutter_s=1/120) as rec:   # records around YOUR action
        show_pattern(...);  rec.mark("patch=64")                          # host time + the clip's own timecode
    ...                                                                   # leaving the block stops, pulls, verifies
    cap = rig.capture("flat", 5, fps=60, iso=400)                         # or: fixed length

A test never names an app: ``fps <= 60`` goes to the Blackmagic REST backend (deterministic, preferred), above that to
mcpro24fps. Every clip gets ``<label>.json`` beside it: what was requested, what the camera HAL actually reported, the
file's real format (ffprobe), the marks, host/phone clock offset and any problems - so a clip is never separated from
the conditions it was shot under and a silently wrong mode is flagged rather than trusted. Problems are *reported*
(``Capture.problems``) for the LLM to judge; nothing is auto-accepted.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import clip as clipmod
from .adb import Adb
from .backend import CameraBackend
from .settings import BackendError, Settings

MIN_FREE_GB = 3.0
_PREFERENCE = ("blackmagic", "mcpro")


@dataclass
class Mark:
    label: str
    t_host: float                       # epoch seconds at the REST/adb call (midpoint of its round trip)
    clip_s: float | None = None         # seconds into the clip (backend timecode), when the backend can say
    data: dict = field(default_factory=dict)


@dataclass
class Capture:
    label: str
    local: Path
    manifest: Path
    info: clipmod.ClipInfo
    problems: list[str] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)
    state: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


class Recording:
    """Handle for a running clip; ``mark()`` timestamps events against it. Serialisable so start and stop can be
    separate processes (:meth:`to_json` / :meth:`PhoneRig.resume`)."""

    def __init__(self, backend: CameraBackend, label: str, token: dict, t_start: float, t_ack: float, state: dict,
                 expect: dict, pre: dict, offset: float, unc: float, delete_remote: bool = True,
                 settings: dict | None = None, marks: list[Mark] | None = None):
        self.backend, self.label, self.token = backend, label, token
        self.t_start, self.t_ack, self.state = t_start, t_ack, state
        self.expect, self.pre, self.offset, self.unc = expect, pre, offset, unc
        self.delete_remote, self.settings = delete_remote, settings or {}
        self.marks: list[Mark] = marks or []
        self.capture: Capture | None = None

    def mark(self, label: str, **data) -> Mark:
        t0 = time.time()
        try:
            clip_s = self.backend.clip_time(self.token)
        except BackendError:
            clip_s = None
        t1 = time.time()
        m = Mark(label=label, t_host=(t0 + t1) / 2, clip_s=clip_s, data=data)
        self.marks.append(m)
        return m

    @property
    def elapsed(self) -> float:
        return time.time() - self.t_ack

    def to_json(self) -> dict:
        return dict(backend=self.backend.name, label=self.label, token=self.token, t_start=self.t_start,
                    t_ack=self.t_ack, state=self.state, expect={k: (list(v) if isinstance(v, tuple) else v)
                                                                for k, v in self.expect.items()},
                    pre=self.pre, offset=self.offset, unc=self.unc, delete_remote=self.delete_remote,
                    settings=self.settings, marks=[dict(label=m.label, t_host=m.t_host, clip_s=m.clip_s, data=m.data)
                                                   for m in self.marks])


class PhoneRig:
    def __init__(self, out_dir: str | Path, adb: Adb | None = None, backends: list[CameraBackend] | None = None):
        self.out_dir = Path(out_dir)
        self.adb = adb or Adb()
        if backends is None:
            from .blackmagic import BlackmagicBackend
            from .mcpro_backend import McproBackend
            backends = [BlackmagicBackend(self.adb), McproBackend(self.adb)]
        self.backends = {b.name: b for b in backends}
        self.active: CameraBackend | None = None

    # -- choosing a backend -----------------------------------------------------------------------------
    def use(self, name: str) -> CameraBackend:
        """Pin the backend explicitly (``'blackmagic'`` / ``'mcpro'``)."""
        if name not in self.backends:
            raise BackendError(f"unknown backend {name!r}; have {sorted(self.backends)}")
        self.active = self.backends[name]
        return self.active

    def backend_for(self, s: Settings) -> CameraBackend:
        need = s.fps or 0.0
        cands = [self.backends[n] for n in _PREFERENCE if n in self.backends] + \
                [b for n, b in self.backends.items() if n not in _PREFERENCE]
        if self.active is not None and need <= self.active.max_fps:
            return self.active                                   # stay on the pinned / last-used one when it can do it
        reasons = []
        for b in cands:
            if need > b.max_fps:
                reasons.append(f"{b.name}: max {b.max_fps:g} fps")
                continue
            ok, why = b.ready()
            if ok:
                return b
            reasons.append(f"{b.name}: {why}")
        raise BackendError(f"no camera backend can do {need:g} fps right now ({'; '.join(reasons)})")

    # -- status / settings ------------------------------------------------------------------------------
    def status(self) -> dict:
        """Everything worth knowing before a test: device, storage, which backends are usable, current camera state."""
        out: dict = dict(device=self.adb.ensure_device(), free_gb=round(self.adb.free_gb(), 2),
                         battery_pct=self.adb.battery_pct(), focus=self.adb.focus_window(), backends={})
        for n, b in self.backends.items():
            ok, why = b.ready()
            out["backends"][n] = dict(b.describe(), ready=ok, why_not=why)
        if self.active is not None:
            out["active"] = self.active.name
            out["state"] = self.active.state()
        return out

    def set(self, **settings) -> dict:
        """Apply settings via the right backend; returns what the camera actually reports (raises if it cannot)."""
        s = Settings.from_kwargs(**settings)
        b = self.backend_for(s)
        b.prepare()
        self.active = b
        return b.apply(s)

    def _preflight(self, b: CameraBackend, min_free_gb: float) -> dict:
        free = self.adb.free_gb()
        if free < min_free_gb:
            raise BackendError(f"phone has {free:.1f} GB free (< {min_free_gb} GB) - clear space before recording")
        return dict(free_gb=round(free, 2), battery_pct=self.adb.battery_pct())

    # -- recording --------------------------------------------------------------------------------------
    def begin(self, label: str, *, expect: dict | None = None, delete_remote: bool = True,
              min_free_gb: float = MIN_FREE_GB, **settings) -> Recording:
        """Apply ``settings`` (verified), then start recording. Pair with :meth:`end`; or use :meth:`recording`.

        ``settings`` (fps, iso, shutter_s, ...) double as the clip expectations (fps / size / codec) unless
        ``expect`` overrides (extra keys: ``bits``, ``max_gaps``)."""
        s = Settings.from_kwargs(**settings)
        b = self.backend_for(s)
        b.prepare()
        self.active = b
        pre = self._preflight(b, min_free_gb)
        state = b.apply(s) if s.given() else b.state()
        offset, unc = self.adb.clock_offset()
        exp = dict(expect or {})
        if s.fps is not None:
            exp.setdefault("fps", s.fps)
        if s.size is not None:
            exp.setdefault("size", s.size)
        if s.codec is not None:
            exp.setdefault("codec", "hevc" if s.codec.lower() in ("hevc", "h265") else "h264")
        t_start = time.time()
        token = b.start()
        return Recording(b, label, token, t_start, time.time(), state, exp, pre, offset, unc, delete_remote,
                         s.given())

    def end(self, rec: Recording, err: BaseException | None = None) -> Capture:
        """Stop, pull, verify, write the manifest. Never raises for a bad *clip* (see ``Capture.problems``)."""
        t_stop = time.time()
        name = rec.backend.stop(rec.token)
        rec.capture = self._finish(rec.label, rec.backend, name, rec, rec.expect, rec.pre, rec.offset, rec.unc,
                                   t_stop, rec.delete_remote, err)
        return rec.capture

    def resume(self, data: dict) -> Recording:
        """Rebuild a :class:`Recording` started by another process (CLI ``rec start`` ... ``rec stop``)."""
        b = self.backends[data["backend"]]
        marks = [Mark(m["label"], m["t_host"], m.get("clip_s"), m.get("data") or {}) for m in data.get("marks", [])]
        exp = {k: (tuple(v) if k == "size" and isinstance(v, list) else v) for k, v in data["expect"].items()}
        self.active = b
        return Recording(b, data["label"], data["token"], data["t_start"], data["t_ack"], data["state"], exp,
                         data["pre"], data["offset"], data["unc"], data.get("delete_remote", True),
                         data.get("settings"), marks)

    @contextmanager
    def recording(self, label: str, **kw) -> Iterator[Recording]:
        """Record around the ``with`` body; on exit stop, pull, verify, write the manifest (``rec.capture``).

        An exception in the body still stops recording and pulls what was captured (flagged ``interrupted``), then
        re-raises - the phone is never left recording."""
        rec = self.begin(label, **kw)
        err: BaseException | None = None
        try:
            yield rec
        except BaseException as e:  # noqa: BLE001 - always stop the camera, then re-raise
            err = e
        finally:
            self.end(rec, err)
        if err is not None:
            raise err

    def capture(self, label: str, seconds: float, **kw) -> Capture:
        """Fixed-length convenience wrapper around :meth:`recording`."""
        with self.recording(label, **kw) as rec:
            time.sleep(max(0.0, seconds - rec.elapsed))
        assert rec.capture is not None
        return rec.capture

    def _finish(self, label: str, b: CameraBackend, name: str, rec: Recording, expect: dict, pre: dict, offset: float,
                unc: float, t_stop: float, delete_remote: bool, err: BaseException | None) -> Capture:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        local = self.out_dir / f"{label}{Path(name).suffix}"
        remote = f"{b.media_dir}/{name}"
        self.adb.pull(remote, local)
        problems: list[str] = []
        remote_size = self.adb.stat_dir(b.media_dir).get(name, (None, 0))[0]
        if remote_size is not None and local.stat().st_size != remote_size:
            problems.append(f"pulled {local.stat().st_size} B != phone {remote_size} B - transfer incomplete")
        info = clipmod.probe(local)
        problems += clipmod.check(info, **expect)
        if err is not None:
            problems.append(f"interrupted: {type(err).__name__}: {err}")
        manifest = self.out_dir / f"{label}.json"
        manifest.write_text(json.dumps(dict(
            label=label, backend=b.describe(), remote=remote, local=str(local),
            host_t_start=rec.t_start, host_t_recording=rec.t_ack, host_t_stop=t_stop,
            phone_minus_host_s=offset, clock_uncertainty_s=unc, preflight=pre, state_at_start=rec.state,
            marks=[dict(label=m.label, t_host=m.t_host, clip_s=m.clip_s, **({"data": m.data} if m.data else {}))
                   for m in rec.marks],
            clip=dict(summary=info.summary(), codec=info.codec, profile=info.profile, pix_fmt=info.pix_fmt,
                      bits=info.bits, size=[info.width, info.height], fps_container=info.fps,
                      fps_measured=info.measured_fps, frames=info.frames, duration_s=info.duration_s,
                      dt_ms=[info.dt_ms_min, info.dt_ms_median, info.dt_ms_max], gaps=info.gaps,
                      first_gap_ms=info.first_gap_ms, color_transfer=info.color_transfer, app_tags=info.app_tags),
            expect={k: (list(v) if isinstance(v, tuple) else v) for k, v in expect.items()}, problems=problems,
            captured_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ), indent=1), encoding="utf-8")
        if delete_remote and not any(p.startswith("pulled") for p in problems):
            self.adb.rm(remote)
        return Capture(label=label, local=local, manifest=manifest, info=info, problems=problems, marks=rec.marks,
                       state=rec.state)
