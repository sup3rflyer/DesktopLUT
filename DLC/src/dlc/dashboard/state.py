"""The dashboard's brain — fold the event spine into a renderable state.

The browser is deliberately dumb: every number it shows is computed here. This module
ingests the run's :class:`~dlc.events.Event` stream and maintains one
:class:`DashboardState` — the status header, the current phase/stage, progress counters,
timers, rolling rates + ETA, the liveness verdict (judged from *data freshness*, not a
socket), the dE big-numbers (from the scoring stage's ``metrics_scored``), and the live
white-point CCT/Duv (enriched here, Python-side, from each patch's xy).

It is intentionally pure and clock-injectable so it can be tested without threads, files,
or sockets: :meth:`DashboardState.snapshot` takes the current time as an argument.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Optional

from ..events import Ev, Event
from .colorimetry import neutral_metrics


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_neutral(rgb: Optional[list]) -> bool:
    """A grayscale/white patch: all channels equal and non-black."""
    if not rgb or len(rgb) < 3:
        return False
    return rgb[0] == rgb[1] == rgb[2] and rgb[0] > 0


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


# Run lifecycle as the dashboard sees it. "running" is the only live state; the rest are
# terminal/observed and drive the liveness light's colour.
RUN_IDLE = "idle"
RUN_RUNNING = "running"
RUN_STALLED = "stalled"
_TERMINAL_BY_STATUS = {
    "completed": "completed",
    "reverted": "reverted",
    "aborted": "aborted",
}


@dataclass
class DashboardState:
    """Everything the dashboard shows, folded from the spine. One per run."""

    # -- identity / status bar ------------------------------------------------
    header: dict[str, Any] = field(default_factory=dict)
    schema_version: Optional[int] = None
    run_id: Optional[str] = None

    # -- phase / stage --------------------------------------------------------
    phase: Optional[str] = None
    stage: Optional[str] = None
    run_status: str = RUN_IDLE

    # -- timing (ISO wall-clock from the producer; freshness math is in snapshot) -
    run_started_iso: Optional[str] = None
    stage_started_iso: Optional[str] = None
    last_event_iso: Optional[str] = None
    last_read_iso: Optional[str] = None
    ended_iso: Optional[str] = None

    # -- counters -------------------------------------------------------------
    patches_done: int = 0
    patches_total: int = 0
    reads: int = 0
    reads_ok: int = 0
    reads_failed: int = 0

    # -- dE big-numbers (from the scoring stage; authoritative) --------------
    de: dict[str, Any] = field(default_factory=dict)
    de_history: list[dict[str, Any]] = field(default_factory=list)

    # -- live colour readout (enriched here from patch xy) -------------------
    last_white: dict[str, Any] = field(default_factory=dict)
    last_read: dict[str, Any] = field(default_factory=dict)

    # -- optimizer / seams / anomalies / stall ------------------------------
    optimizer: dict[str, Any] = field(default_factory=dict)
    last_seam: dict[str, Any] = field(default_factory=dict)
    last_anomaly: dict[str, Any] = field(default_factory=dict)
    last_check_in: dict[str, Any] = field(default_factory=dict)
    stall: dict[str, Any] = field(default_factory=dict)

    events_seen: int = 0

    # rolling windows for rates/ETA (not serialised raw — distilled in snapshot)
    _read_times: Deque[datetime] = field(default_factory=lambda: deque(maxlen=80))
    _progress_marks: Deque[tuple] = field(default_factory=lambda: deque(maxlen=80))

    # ----------------------------------------------------------------------
    def ingest(self, ev: Event) -> dict[str, Any]:
        """Fold one event into the state; return its enriched wire form (for the log)."""
        self.events_seen += 1
        if ev.time:
            self.last_event_iso = ev.time
        if self.run_started_iso is None and ev.time:
            self.run_started_iso = ev.time
        if ev.phase:
            self.phase = ev.phase

        name = ev.event
        data = ev.data or {}

        if name == Ev.RUN_HEADER:
            self.header = dict(data)
            self.schema_version = data.get("schema_version", self.schema_version)
            self.run_id = data.get("run_id", self.run_id)
            if self.run_status == RUN_IDLE:
                self.run_status = RUN_RUNNING
        elif name == Ev.PHASE:
            self.phase = data.get("phase_name", self.phase)
        elif name == Ev.STAGE_START:
            self.stage = ev.stage
            self.stage_started_iso = ev.time
            if self.run_status in (RUN_IDLE, RUN_STALLED):
                self.run_status = RUN_RUNNING
        elif name == Ev.STAGE_ABORTED:
            # keep self.stage as the failing stage for the header
            pass
        elif name == Ev.PROGRESS:
            # patches_done/total are per-stage (the bar shows the CURRENT stage's progress);
            # the cumulative read total is tracked in _ingest_patch_read so it stays
            # consistent with reads_ok/reads_failed (PROGRESS.reads resets each stage).
            self.patches_done = int(data.get("patches_done", self.patches_done))
            self.patches_total = int(data.get("patches_total", self.patches_total))
            mark_t = _parse_iso(ev.time)
            if mark_t is not None:
                self._progress_marks.append((mark_t, self.patches_done))
        elif name == Ev.PATCH_READ:
            self._ingest_patch_read(ev, data)
        elif name == "metrics_scored":
            self._ingest_metrics(ev, data)
        elif name == Ev.OPTIMIZER_ITER:
            self.optimizer = dict(data)
        elif name == Ev.SEAM:
            self.last_seam = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.ANOMALY:
            self.last_anomaly = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.CHECK_IN:
            self.last_check_in = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.STALL:
            self.run_status = RUN_STALLED
            self.stall = {"stage": ev.stage, "time": ev.time, **data}
        elif name == Ev.RUN_DONE:
            status = data.get("status", "completed")
            self.run_status = _TERMINAL_BY_STATUS.get(status, status)
            self.ended_iso = ev.time

        return self._wire(ev, data)

    def _ingest_patch_read(self, ev: Event, data: dict[str, Any]) -> None:
        ok = bool(data.get("ok"))
        if ok:
            self.reads_ok += 1
        else:
            self.reads_failed += 1
        self.reads = self.reads_ok + self.reads_failed  # cumulative; consistent with ok/fail
        read_t = _parse_iso(ev.time)
        if read_t is not None:
            self._read_times.append(read_t)
            self.last_read_iso = ev.time

        xy = data.get("xy")
        Y = data.get("Y")
        enriched = {}
        if ok and xy and len(xy) >= 2:
            enriched = neutral_metrics(float(xy[0]), float(xy[1]))
        self.last_read = {
            "seq": data.get("seq"), "role": data.get("role"), "label": data.get("label"),
            "rgb": data.get("rgb"), "Y": Y, "xy": xy, "ok": ok,
            "disposition": data.get("disposition"), **enriched,
        }
        # The most recent neutral read drives the live white-point readout.
        if ok and xy and _is_neutral(data.get("rgb")):
            self.last_white = {"xy": xy, "Y": Y, "rgb": data.get("rgb"), **enriched}

    def _ingest_metrics(self, ev: Event, data: dict[str, Any]) -> None:
        entry = {
            "phase": data.get("label") or data.get("phase") or ev.stage,
            "iteration": data.get("iteration"),
            "avg": data.get("avg_de2000"),
            "p95": data.get("p95_de2000"),
            "p99": data.get("p99_de2000"),
            "max": data.get("max_de2000"),
            "white": data.get("white_de2000"),
            "grayscale": data.get("grayscale_avg_de2000"),
            "colour": data.get("colour_avg_de2000"),
        }
        self.de = entry
        self.de_history.append(entry)
        if len(self.de_history) > 64:
            self.de_history = self.de_history[-64:]

    # ----------------------------------------------------------------------
    def _wire(self, ev: Event, data: dict[str, Any]) -> dict[str, Any]:
        """One event as the browser's event-log sees it. For patch reads we fold the
        CCT/Duv in so the log can show the colour readout without any JS colour math."""
        out: dict[str, Any] = {
            "time": ev.time, "level": ev.level, "stage": ev.stage,
            "event": ev.event, "phase": ev.phase, "tier": ev.effective_tier,
            "data": data,
        }
        if ev.event == Ev.PATCH_READ:
            xy = data.get("xy")
            if data.get("ok") and xy and len(xy) >= 2:
                out["derived"] = neutral_metrics(float(xy[0]), float(xy[1]))
        return out

    # ----------------------------------------------------------------------
    def _rolling_s_per_read(self) -> Optional[float]:
        times = list(self._read_times)
        if len(times) < 2:
            return None
        diffs = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
        diffs = [d for d in diffs if 0.0 < d < 600.0]  # drop pauses/soak gaps
        return _median(diffs)

    def _rolling_s_per_patch(self) -> Optional[float]:
        marks = list(self._progress_marks)
        if len(marks) < 2:
            return None
        (t0, p0), (t1, p1) = marks[0], marks[-1]
        dp = p1 - p0
        dt = (t1 - t0).total_seconds()
        if dp <= 0 or dt <= 0:
            return None
        return dt / dp

    def _liveness(self, now: datetime) -> dict[str, Any]:
        """Judge liveness from the age of the last event, not a socket. During the
        silent soak the heartbeat keeps the age fresh; if the whole process dies the
        age grows and the light goes red on its own."""
        age = None
        last = _parse_iso(self.last_event_iso)
        if last is not None:
            age = max(0.0, (now - last).total_seconds())

        if self.run_status in _TERMINAL_BY_STATUS.values():
            light = "done"
        elif self.run_status == RUN_STALLED:
            light = "stalled"
        elif age is None:
            light = "unknown"
        else:
            spr = self._rolling_s_per_read()
            # A generous freshness budget: a few read-intervals, floored so a fast run's
            # tiny interval doesn't trip on normal jitter, and the heartbeat (≤15 s)
            # comfortably keeps it green through the soak.
            budget = max(45.0, (spr or 10.0) * 4.0)
            if age <= budget:
                light = "live"
            elif age <= budget * 3.0:
                light = "slow"
            else:
                light = "stalled"
        return {"light": light, "age_s": round(age, 1) if age is not None else None,
                "last_event_iso": self.last_event_iso, "last_read_iso": self.last_read_iso}

    def snapshot(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """The full renderable state. ``now`` is injected for testability; the server
        passes ``datetime.now()`` so timers/liveness advance even with no new events."""
        if now is None:
            now = datetime.now()

        started = _parse_iso(self.run_started_iso)
        run_elapsed = (now - started).total_seconds() if started else None
        ended = _parse_iso(self.ended_iso)
        if ended and started:
            run_elapsed = (ended - started).total_seconds()  # freeze the clock when done

        stage_started = _parse_iso(self.stage_started_iso)
        stage_elapsed = (now - stage_started).total_seconds() if stage_started else None

        spr = self._rolling_s_per_read()
        spp = self._rolling_s_per_patch()
        remaining = max(0, self.patches_total - self.patches_done) if self.patches_total else 0
        eta = remaining * spp if (spp and remaining and self.run_status == RUN_RUNNING) else None

        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "header": self.header,
            "phase": self.phase,
            "stage": self.stage,
            "run_status": self.run_status,
            "events_seen": self.events_seen,
            "counters": {
                "patches_done": self.patches_done,
                "patches_total": self.patches_total,
                "reads": self.reads,
                "reads_ok": self.reads_ok,
                "reads_failed": self.reads_failed,
            },
            "timers": {
                "run_elapsed_s": round(run_elapsed, 1) if run_elapsed is not None else None,
                "stage_elapsed_s": round(stage_elapsed, 1) if stage_elapsed is not None else None,
                "s_per_read": round(spr, 2) if spr is not None else None,
                "s_per_patch": round(spp, 2) if spp is not None else None,
                "eta_s": round(eta, 1) if eta is not None else None,
                "run_started_iso": self.run_started_iso,
                "ended_iso": self.ended_iso,
            },
            "de": self.de,
            "de_history": self.de_history,
            "last_white": self.last_white,
            "last_read": self.last_read,
            "optimizer": self.optimizer,
            "seam": self.last_seam,
            "anomaly": self.last_anomaly,
            "check_in": self.last_check_in,
            "stall": self.stall,
            "liveness": self._liveness(now),
        }
