"""Shared mechanics of the probe flows (``peak-window``, ``refresh-response``) inside the calibration orchestrator.

A probe flow is a :class:`~dlc.calibrate.Calibration` flow: it inherits the run spine (``events.jsonl``, the live
files the DLC tab reads), the preflight (pipe, monitor map, meter correction, backup), the adjudicated seams
(exit 10 / ``--decide``), the liveness watchdog, ``control.json`` cancel and the rollback guard. What this module
adds is what a probe needs and a calibration does not:

* :class:`ProbeReader` — one read of a held or new frame with its time stamp: liveness + cancel at every read, the
  read row to the probe's ``*.jsonl``, a stream-tier ``probe_read`` event for the dashboard, and a meter-down stop
  (``meter_down_reads`` failed reads in a row, or the meter's own terminal fault).
* :class:`CheckIns` — the probe's NON-BLOCKING evidence packets: at 25 / 50 / 75 % of the planned steps and at least
  every ``interval_s`` (the run's ``--checkin-interval``), each carrying the probe's own evidence and no
  recommendation (DESIGN LAW: a check-in never pauses and never decides).
* :func:`publish_chart` — a ``probe_chart`` event; the dashboard fold serves the latest payload per chart under
  ``charts.json["probes"][<chart>]`` (:mod:`dlc.dashboard.state`).
* :func:`probe_clock` — the measure fn's ``probe_clock`` when it has one (the simulator), else the wall clock.
* :func:`other_session_open` — the "another run is live" refusal: DesktopLUT holds a calibration session or capture
  this run did not open.

Stdlib only; the orchestrator is imported lazily (it imports the probes).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .liveness import MeterDown
from .measure_loop import MeasurePatch, Reading
from .paths import atomic_write_text
from .probe_sim import RealClock

__all__ = ["ProbeReader", "CheckIns", "publish_chart", "probe_clock", "other_session_open", "Read", "jsonable",
           "write_json"]


def probe_clock(cal: Any) -> Any:
    clock = getattr(getattr(cal, "measure", None), "probe_clock", None)
    return clock if clock is not None else RealClock()


def jsonable(v: Any) -> Any:
    """NaN / inf → None, tuples → lists, recursively (every probe file is read by a strict JSON parser)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def write_json(path: Path, obj: Any) -> Path:
    atomic_write_text(Path(path), json.dumps(jsonable(obj), indent=1))
    return Path(path)


class Read:
    """One read: the reading, when the frame it belongs to was painted, and when the read ended (clock seconds)."""

    __slots__ = ("reading", "t_paint", "t_end", "duration")

    def __init__(self, reading: Reading, t_paint: float, t_end: float, duration: float) -> None:
        self.reading, self.t_paint, self.t_end, self.duration = reading, t_paint, t_end, duration

    @property
    def y(self) -> Optional[float]:
        r = self.reading
        return float(r.xyz[1]) if (r.ok and r.xyz is not None) else None


class ProbeReader:
    def __init__(self, cal: Any, stage: str, *, rows_path: Optional[Path] = None, meter_down_reads: int = 3,
                 clock: Any = None) -> None:
        self.cal = cal
        self.stage = stage
        self.rows_path = rows_path
        self.meter_down_reads = max(1, int(meter_down_reads))
        self.clock = clock or probe_clock(cal)
        self.failures = 0
        self.n_reads = 0
        self.n_failed = 0

    def check(self) -> None:
        """Cancel / stall checkpoint without a read (long waits call it)."""
        self.cal._poll_cancel()
        self.cal.liveness.check(self.stage)

    def wait(self, seconds: float, *, chunk_s: float = 2.0) -> None:
        """A wait the run can be cancelled out of (checkpoint every ``chunk_s``); waiting on purpose is progress."""
        end = self.clock.now() + max(0.0, float(seconds))
        while True:
            self.check()
            left = end - self.clock.now()
            if left <= 0:
                return
            self.clock.sleep(min(chunk_s, left))
            self.cal.liveness.progress(self.stage)

    def read(self, patch: MeasurePatch, *, t_paint: Optional[float] = None, row: Optional[dict] = None,
             soft_fail_ok: bool = False) -> Read:
        """``soft_fail_ok``: a failed read that is not a meter-PROCESS fault (spotread's under-range warning on a
        true black, an OLED's black) does not count toward meter-down — only a fault does."""
        self.check()
        self.cal.liveness.activity(self.stage)
        t0 = self.clock.now()
        reading = self.cal.measure(patch)
        t1 = self.clock.now()
        self.n_reads += 1
        rd = Read(reading, t0 if t_paint is None else t_paint, t1, t1 - t0)
        ok = rd.y is not None
        raw = reading.raw or {}
        soft = (not ok) and soft_fail_ok and not (raw.get("meter_fault") or raw.get("meter_down"))
        if ok or soft:
            self.failures = 0
            self.cal.liveness.progress(self.stage)
        else:
            self.failures += 1
        if not ok:
            self.n_failed += 1
        rec = {"label": patch.label, "rgb": list(patch.rgb), "area_pct": patch.window_area_pct,
               "t_end": round(t1, 3), "since_paint_s": round(t1 - rd.t_paint, 3), "dur_s": round(t1 - t0, 3),
               "ok": ok, "Y": rd.y, "xyz": list(reading.xyz) if reading.xyz else None, "error": reading.error,
               **(row or {})}
        if self.rows_path is not None:
            try:
                with open(self.rows_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(jsonable(rec)) + "\n")
            except OSError:
                pass
        self.cal.runlog.emit("INFO" if ok else "WARN", self.stage, "probe_read", tier="stream" if ok else "digest",
                             **jsonable(rec))
        if not ok and (raw.get("meter_down") or self.failures >= self.meter_down_reads):
            raise MeterDown(self.stage, reading.error or "no reading", consecutive_failures=self.failures,
                            detail={"last_error": reading.error, "fault": raw.get("meter_fault")})
        return rd


class CheckIns:
    """The probe's evidence packets (25 / 50 / 75 % of ``total`` steps, and every ``interval_s``)."""

    FRACTIONS = (0.25, 0.5, 0.75)

    def __init__(self, cal: Any, stage: str, total: int, *, clock: Any = None, interval_s: Optional[float] = None) -> None:
        self.cal, self.stage, self.total = cal, stage, max(1, int(total))
        self.clock = clock or probe_clock(cal)
        iv = interval_s if interval_s is not None else float(getattr(cal, "_checkin_interval_s", 600.0) or 600.0)
        self.interval_s = max(30.0, min(iv, 600.0))
        self.fired: set = set()
        self.last = self.clock.now()
        self.seq = 0

    def tick(self, done: int, evidence: Callable[[], dict[str, Any]]) -> Optional[dict[str, Any]]:
        frac = done / self.total
        trig = [f"progress_{int(f * 100)}" for f in self.FRACTIONS if frac >= f and f not in self.fired]
        self.fired.update(f for f in self.FRACTIONS if frac >= f)
        now = self.clock.now()
        if now - self.last >= self.interval_s:
            trig.append("timed")
        if not trig:
            return None
        self.seq += 1
        self.last = now
        packet = {"seq": self.seq, "trigger": ",".join(trig), "done": done, "of": self.total,
                  "progress": f"{done}/{self.total}", "evidence": jsonable(evidence())}
        self.cal.runlog.check_in(self.stage, **packet)
        return packet


def publish_chart(cal: Any, stage: str, chart: str, payload: dict[str, Any]) -> None:
    """The latest state of one probe chart, for ``charts.json["probes"][chart]`` (stream tier: dashboard only)."""
    cal.runlog.emit("INFO", stage, "probe_chart", tier="stream", chart=chart, **jsonable(payload))


def other_session_open(cal: Any) -> Optional[dict[str, Any]]:
    """DesktopLUT holds a calibration session / capture this run did not open (another run is live, or one died
    in calibration mode). ``None`` = none, or this run entered it itself."""
    if "enter-neutral" in (cal.calib.get("stages") or {}):
        return None
    from .stages import _common
    is_open, status = _common.calibration_session_open(cal.controller)
    return status if is_open else None


def hdr_active(cal: Any) -> Optional[bool]:
    """The pipe's view of whether this run's monitor is in HDR (None = unknown)."""
    try:
        mons = (cal.controller.query_monitors() or {}).get("monitors") or []
    except Exception:  # noqa: BLE001
        return None
    for m in mons:
        if int(m.get("index", -1)) == int(cal.monitor):
            v = m.get("hdr_active")
            return bool(v) if v is not None else None
    return None


def monitor_entry(cal: Any) -> dict[str, Any]:
    try:
        mons = (cal.controller.query_monitors() or {}).get("monitors") or []
    except Exception:  # noqa: BLE001
        return {}
    return next((dict(m) for m in mons if int(m.get("index", -1)) == int(cal.monitor)), {})


def stack_view(cal: Any) -> dict[str, Any]:
    """What the meter measures through, from ``state.get`` (the record of the stack state)."""
    key = f"{cal.monitor}:{cal.mode}"
    try:
        st = cal.controller.state() or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    mhc = (st.get("mhc") or {}).get(key) or {}
    return {"mhc_profile": mhc.get("profile_name"), "active_perm": mhc.get("active_perm"),
            "mhc_source": mhc.get("source_file"),
            "cube_path": ((st.get("runtime") or {}).get(key) or {}).get("cube_path"),
            "layers": dict((st.get("layers") or {}).get(key) or {}),
            "hook": {k: (st.get("hook") or {}).get(k) for k in ("active",)},
            "overlay": dict(st.get("overlay") or {})}


def median(v: Sequence[float]) -> Optional[float]:
    s = sorted(v)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def viewing_layers_diff(controller: Any, calib_state: Optional[dict[str, Any]], *, monitor: Optional[int],
                        mode: Optional[str]) -> dict[str, Any]:
    """Read back the user's viewing layers against what the run captured before switching them off.
    ``{"ok": bool | None, "diff": {layer: [before, now]}}``; ``ok`` None = nothing captured / unreadable."""
    rec = (calib_state or {}).get("viewing_layers")
    if not isinstance(rec, dict) or not rec.get("captured"):
        return {"ok": None, "diff": {}, "note": "no viewing layers were captured (nothing switched)"}
    before = rec.get("before") or {}
    kept = set(rec.get("kept") or ())
    mon = rec.get("monitor", monitor)
    md = rec.get("mode", mode)
    try:
        from .controller import CalibrationController
        now = CalibrationController.layers_from_state(controller.state() or {}, int(mon), str(md))
    except Exception as exc:  # noqa: BLE001
        return {"ok": None, "diff": {}, "error": f"{type(exc).__name__}: {exc}"}
    if now is None:
        return {"ok": None, "diff": {}, "note": "this DesktopLUT reports no viewing layers"}
    diff = {k: [bool(v), bool(now.get(k))] for k, v in before.items() if k not in kept and bool(now.get(k)) != bool(v)}
    return {"ok": not diff, "diff": diff}


def confirm_native_restored(controller: Any, calib_state: Optional[dict[str, Any]], *, monitor: Optional[int],
                            mode: Optional[str]) -> dict[str, Any]:
    """A run that entered calibration mode is back as it started, by read-back: no calibration session / capture
    open, the viewing layers as captured, and an MHC profile associated again when the user had one."""
    from .stages import _common
    out: dict[str, Any] = {"confirmed": False}
    is_open, status = _common.calibration_session_open(controller)
    out["session_open"] = is_open
    layers = viewing_layers_diff(controller, calib_state, monitor=monitor, mode=mode)
    out["viewing_layers"] = layers
    bak = (calib_state or {}).get("backup") or {}
    mhc_ok: Optional[bool] = None
    if bak.get("had_mhc") and monitor is not None and mode:
        try:
            entry = ((controller.state() or {}).get("mhc") or {}).get(f"{int(monitor)}:{str(mode).upper()}") or {}
            mhc_ok = bool(entry.get("profile_name") or entry.get("applied") or entry.get("enabled"))
        except Exception:  # noqa: BLE001
            mhc_ok = None
    out["user_mhc_associated"] = mhc_ok
    out["confirmed"] = is_open is False and layers.get("ok") is not False and mhc_ok is not False
    return out
