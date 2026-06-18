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
from .colorimetry import neutral_metrics, planckian_locus_xy

# The target gamut the charts draw the reference triangle against (sRGB / Rec.709 primaries).
_SRGB_PRIMARIES = {"r": [0.64, 0.33], "g": [0.30, 0.60], "b": [0.15, 0.06]}


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a spine timestamp to a tz-NAIVE datetime. The spine writes naive local
    wall-clock, and snapshot() compares against a naive ``datetime.now()`` — but a
    foreign/drifted producer could write a tz-aware string (``...Z`` / ``+00:00``),
    which 3.11+ accepts. Mixing naive and aware datetimes raises TypeError, and that
    would be swallowed by the server's loop guard → the dashboard silently freezes (the
    worst failure for a liveness tool). So normalise any aware value to naive local."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
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
    last_progress_iso: Optional[str] = None   # last event that meant real forward progress
    ended_iso: Optional[str] = None

    # -- liveness inputs ------------------------------------------------------
    # The producer's authoritative progress-age (monotonic, reset by soak blocks too) rides
    # the heartbeat. Tracking it lets the light tell "alive AND progressing" from "alive but
    # WEDGED" — the exact 53-min failure shape — before the producer's own stall fires.
    last_heartbeat: dict[str, Any] = field(default_factory=dict)
    awaiting_decision: bool = False           # paused at a seam (NOT a stall) — don't show red

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

    # chart accumulators (bounded; served via /api/charts, OFF the fast SSE path so the 2 s
    # state push stays lean while the scatter can grow to thousands of points)
    _cie_points: Deque[dict] = field(default_factory=lambda: deque(maxlen=5000))
    _grayscale: dict = field(default_factory=dict)          # signal-level → latest neutral sample
    _white_track: Deque[dict] = field(default_factory=lambda: deque(maxlen=600))
    _optimizer_history: list = field(default_factory=list)

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
            # A stage boundary is real progress and resets the per-stage rate window — the
            # patch counter restarts at ~0, so an ETA computed across the reset is garbage.
            self._mark_progress(ev.time)
            self._progress_marks.clear()
            self._clear_alarm()
        elif name == Ev.STAGE_ABORTED:
            # keep self.stage as the failing stage for the header
            pass
        elif name == Ev.PROGRESS:
            # patches_done/total are per-stage (the bar shows the CURRENT stage's progress);
            # the cumulative read total is tracked in _ingest_patch_read so it stays
            # consistent with reads_ok/reads_failed (PROGRESS.reads resets each stage).
            self.patches_done = _as_int(data.get("patches_done"), self.patches_done)
            self.patches_total = _as_int(data.get("patches_total"), self.patches_total)
            self._mark_progress(ev.time)
            self._clear_alarm()
            mark_t = _parse_iso(ev.time)
            if mark_t is not None:
                self._progress_marks.append((mark_t, self.patches_done))
        elif name == Ev.PATCH_READ:
            self._ingest_patch_read(ev, data)
        elif name == Ev.HEARTBEAT:
            # The producer's authoritative progress-age (monotonic) — the one signal that
            # survives a syscall wedge AND a silent soak (soak blocks reset it producer-side).
            self.last_heartbeat = {"time": ev.time,
                                   "since_progress_s": _as_float(data.get("since_progress_s")),
                                   "stall_after_s": _as_float(data.get("stall_after_s"))}
        elif name == "metrics_scored":
            self._ingest_metrics(ev, data)
            self._mark_progress(ev.time)
        elif name == Ev.OPTIMIZER_ITER:
            self.optimizer = dict(data)
            self._optimizer_history.append(dict(data))
            if len(self._optimizer_history) > 256:
                self._optimizer_history = self._optimizer_history[-256:]
            self._mark_progress(ev.time)
            self._clear_alarm()
        elif name == Ev.SEAM:
            self.last_seam = {"stage": ev.stage, "time": ev.time, **data}
            # A paused seam is a healthy human-in-the-loop wait, NOT a stall — but heartbeats
            # stop (the run process exits to await the decision), so without this flag the
            # light would drift to red and read identically to a hang. Latch it; any later
            # progress (the resuming run) clears it.
            self.awaiting_decision = (data.get("status") == "paused")
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
            self.awaiting_decision = False

        return self._wire(ev, data)

    def _mark_progress(self, iso: Optional[str]) -> None:
        if iso:
            self.last_progress_iso = iso

    def _clear_alarm(self) -> None:
        """Fresh forward progress arrived → the run is moving again. Clear a paused seam,
        promote IDLE→RUNNING (the run is observably live), and un-latch a prior stall (if a
        stall guard self-recovered within the stage) — so the light reflects the data, not a
        stale status. Never resurrects a TERMINAL run (completed/reverted/aborted)."""
        self.awaiting_decision = False
        if self.run_status in (RUN_IDLE, RUN_STALLED):
            self.run_status = RUN_RUNNING

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
        has_xy = bool(xy) and len(xy) >= 2 and _as_float(xy[0]) is not None and _as_float(xy[1]) is not None
        enriched: dict[str, Any] = {}
        if ok and has_xy:
            enriched = neutral_metrics(float(xy[0]), float(xy[1]))
            self._mark_progress(ev.time)   # a good read is forward progress
            self._clear_alarm()
            self._accumulate_charts(ev, data, float(xy[0]), float(xy[1]), Y, enriched)
        self.last_read = {
            "seq": data.get("seq"), "role": data.get("role"), "label": data.get("label"),
            "rgb": data.get("rgb"), "Y": Y, "xy": xy, "ok": ok,
            "disposition": data.get("disposition"), **enriched,
        }
        # The most recent neutral read drives the live white-point readout (needs a usable xy
        # so the readout shape stays consistent with the enrichment gate above).
        if ok and has_xy and _is_neutral(data.get("rgb")):
            self.last_white = {"xy": xy, "Y": Y, "rgb": data.get("rgb"), **enriched}

    def _elapsed_at(self, iso: Optional[str]) -> Optional[float]:
        t, start = _parse_iso(iso), _parse_iso(self.run_started_iso)
        if t is None or start is None:
            return None
        return round(max(0.0, (t - start).total_seconds()), 1)

    def _accumulate_charts(self, ev: Event, data: dict[str, Any], x: float, y: float,
                           Y: Any, enriched: dict[str, Any]) -> None:
        """Fold a good read into the bounded chart datasets (served via /api/charts)."""
        rgb = data.get("rgb")
        neutral = _is_neutral(rgb)
        self._cie_points.append({"x": round(x, 5), "y": round(y, 5),
                                 "role": data.get("role"), "neutral": neutral})
        sig = data.get("signal")
        level = _as_float(sig[0]) if (neutral and sig and len(sig) >= 1) else None
        if level is not None:
            # latest measurement at this grayscale level wins (re-measures overwrite)
            self._grayscale[round(level, 5)] = {
                "signal": round(level, 5), "Y": Y, "x": round(x, 5), "y": round(y, 5),
                "cct": enriched.get("cct"), "duv": enriched.get("duv")}
        if neutral:
            self._white_track.append({"elapsed_s": self._elapsed_at(ev.time),
                                      "cct": enriched.get("cct"), "duv": enriched.get("duv"), "Y": Y})

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
    def charts(self) -> dict[str, Any]:
        """Chart-ready datasets, built from the bounded accumulators. Served via
        /api/charts (NOT the SSE state) so the heavy scatter stays off the fast path.
        Everything is already numeric/derived — the browser only draws SVG."""
        gray = [self._grayscale[k] for k in sorted(self._grayscale)]
        white = (self.header.get("white") or {}).get("xy")
        return {
            "cie": {
                "points": list(self._cie_points),
                "white": white,
                "primaries": _SRGB_PRIMARIES,
                "locus": [[round(x, 5), round(y, 5)] for (x, y) in planckian_locus_xy()],
            },
            "grayscale": gray,
            "eotf": {
                "gamma": self.header.get("gamma"),
                "luminance": self.header.get("luminance"),
                "points": [{"signal": g["signal"], "Y": g["Y"]} for g in gray if g.get("Y") is not None],
            },
            "optimizer": list(self._optimizer_history),
            "white_track": [w for w in self._white_track if w.get("elapsed_s") is not None],
        }

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
                x, y = _as_float(xy[0]), _as_float(xy[1])
                if x is not None and y is not None:
                    out["derived"] = neutral_metrics(x, y)
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

    def _progress_age(self, now: datetime) -> Optional[float]:
        """Seconds since the run last made REAL forward progress (a good read, a completed
        iteration, a soak block, a stage boundary). Distinct from event-age: heartbeats keep
        event-age fresh during a wedge, but progress-age keeps growing — that gap is exactly
        the 53-min "alive but stuck" failure. Two estimates of the same quantity; take the
        smaller (the more recent progress wins):

        * **events** — ``now - last_progress_iso`` (fresh, but blind to the silent soak,
          which emits no patch/progress events).
        * **heartbeat** — the producer's monotonic ``since_progress_s`` (authoritative; reset
          by soak blocks too) carried forward by the age of that heartbeat.
        """
        candidates: list[float] = []
        lp = _parse_iso(self.last_progress_iso)
        if lp is not None:
            candidates.append(max(0.0, (now - lp).total_seconds()))
        hb = self.last_heartbeat
        hb_t = _parse_iso(hb.get("time")) if hb else None
        since = hb.get("since_progress_s") if hb else None
        if hb_t is not None and since is not None:
            candidates.append(max(0.0, since + (now - hb_t).total_seconds()))
        return min(candidates) if candidates else None

    def _liveness(self, now: datetime) -> dict[str, Any]:
        """The light, judged from DATA FRESHNESS (not the socket). Order matters:
        terminal → done; a paused seam → a calm 'paused' (NOT red — a healthy decision
        wait, even though heartbeats stop); a tripped stall → red; otherwise weigh
        event-age (is the process alive at all?) against progress-age (is it actually
        advancing?). The progress-age arm is what catches an alive-but-wedged run before
        the producer's own stall fires."""
        event_age = None
        last = _parse_iso(self.last_event_iso)
        if last is not None:
            event_age = max(0.0, (now - last).total_seconds())
        prog_age = self._progress_age(now)

        if self.run_status in _TERMINAL_BY_STATUS.values():
            light = "done"
        elif self.awaiting_decision:
            light = "paused"
        elif self.run_status == RUN_STALLED:
            light = "stalled"
        elif event_age is None:
            light = "unknown"
        else:
            spr = self._rolling_s_per_read()
            # Event budget: heartbeats arrive ≤15 s apart, so any larger silence means the
            # process itself is gone — go red. Floored generously against read jitter.
            event_budget = max(45.0, (spr or 10.0) * 4.0)
            # Progress budget: prefer the producer's own stall threshold (from the heartbeat)
            # so the dashboard warns in step with the guard; else a generous default.
            stall_after = (self.last_heartbeat or {}).get("stall_after_s")
            prog_budget = stall_after if (stall_after and stall_after > 0) else max(120.0, (spr or 10.0) * 12.0)
            if event_age > event_budget:
                light = "stalled"            # no heartbeat even → process likely dead
            elif prog_age is not None and prog_age > prog_budget:
                light = "stalled"            # alive but past the stall threshold (guard should fire ~now)
            elif prog_age is not None and prog_age > 0.5 * prog_budget:
                light = "slow"               # alive but not advancing — the early wedge warning
            else:
                light = "live"
        return {"light": light,
                "age_s": round(event_age, 1) if event_age is not None else None,
                "progress_age_s": round(prog_age, 1) if prog_age is not None else None,
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
