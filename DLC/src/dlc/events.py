"""The run's single narrative log — the one stream both consumers read.

`events.jsonl` (one append-only file per run, at :pyattr:`RunContext.events_path`)
is the **spine**: every major event AND every patch read land here, so the
mission-control dashboard can tail one file and show the whole run, while the LLM
reads only a *digest-tier projection* of the same file (never the per-patch
firehose). This realises the v2-design three-consumer model (§1) and §12's "one
digest channel … a 3-hour run never quietly finishes; it is gated."

Two tiers ride the one stream:

* **digest** — boundaries, anomalies, seams, timed check-ins, stalls, the run
  header, phase changes, optimizer iterations. The LLM sees these.
* **stream** — per-patch reads, heartbeats, fine progress. Dashboard-only; the
  LLM projection drops them so it never tails the firehose.

The tier is stamped explicitly when known and otherwise *derived* from the event
name + level (so the ~dozen legacy ``EventWriter(...).write(...)`` call sites keep
working unchanged and still project correctly). :class:`RunLog` is the typed
front door new code should use; it stamps the active phase onto every event so the
dashboard's event-log header always knows where the run is.

Schema (one JSON object per line)::

    {"time","level","stage","event","data",  "tier"?, "phase"?}

Dependency-free (stdlib only) — the spine must stay importable without the engine
extras, like the rest of the core.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

# Bumped when the on-disk event schema changes in a way a consumer must notice.
# Carried in the run_header so a long-lived dashboard tailing an older/newer run's
# file can adapt (read_events stays tolerant of unknown/missing fields regardless).
SCHEMA_VERSION = 1

# One process-wide lock serialises every append to any events.jsonl. The spine is
# about to gain a second writer (the liveness watchdog thread) on top of the measure
# loop's reader-thread context, and on Windows O_APPEND is NOT atomic across threads
# — two open/write/close races could tear an interior line, which read_events drops
# silently (so we'd lose exactly the stall event that matters). Writes are tiny, so a
# single lock costs nothing and guarantees whole lines.
_WRITE_LOCK = threading.Lock()


def _now_iso() -> str:
    # Wall-clock, for HUMAN reading in the log/dashboard. Liveness/stall math must use
    # time.monotonic() (a clock step must never make "since_progress" go negative) —
    # that lives in the liveness helper, never derived by subtracting these strings.
    return datetime.now().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Event vocabulary — the shared contract between the producers (the run) and the
# two consumers (dashboard tails all; LLM reads the digest projection). Keep the
# names stable: the dashboard and the projection switch on them.
# ---------------------------------------------------------------------------

class Ev:
    RUN_HEADER = "run_header"        # target + active ccmx/spd + mode/monitor/display (dash status bar)
    PHASE = "phase"                  # the run entered a new phase (dash event-log header)
    STAGE_START = "stage_start"
    STAGE_DONE = "stage_done"
    STAGE_ABORTED = "stage_aborted"
    PATCH_READ = "patch_read"        # one meter read (compact; the dense record is in measurements.ndjson)
    HEARTBEAT = "heartbeat"          # "still alive" tick with elapsed + last-progress age
    PROGRESS = "progress"            # coarse progress counter (patches done / total, stage n/m)
    OPTIMIZER_ITER = "optimizer_iteration"
    SEAM = "seam"                    # an adjudication seam was reached (LLM/human decides)
    ANOMALY = "anomaly"              # threshold ping — something looks wrong
    CHECK_IN = "check_in"            # §12 timed check-in ("status, continue?")
    STALL = "stall"                  # the stall guard tripped (no progress beyond threshold)
    RUN_DONE = "run_done"            # terminal: the run finished (completed / reverted / aborted)
    NOTE = "note"                    # free-form narrative line


# Stream-tier events are dashboard-only: the LLM digest projection drops them so it
# never tails the per-patch / heartbeat firehose. Everything else defaults to digest.
STREAM_EVENTS = frozenset({Ev.PATCH_READ, Ev.HEARTBEAT, Ev.PROGRESS})

# Levels that ALWAYS reach the LLM regardless of event name — a WARN/ERROR on even a
# stream-tier event (e.g. a failed patch read storm) must surface in the digest.
_DIGEST_LEVELS = frozenset({"WARN", "ERROR"})


def derive_tier(event: str, level: str) -> str:
    """The fallback tier when an event was written without an explicit one (legacy
    call sites, or producers that don't care): a warning/error is always digest;
    otherwise the stream-tier event names are dashboard-only, the rest are digest."""
    if level in _DIGEST_LEVELS:
        return "digest"
    return "stream" if event in STREAM_EVENTS else "digest"


@dataclass
class Event:
    level: str
    stage: str
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    time: str = field(default_factory=_now_iso)
    tier: Optional[str] = None      # "digest" (LLM+dash) | "stream" (dash-only); None ⇒ derive
    phase: Optional[str] = None     # run phase active when emitted (dash event-log header)

    @property
    def effective_tier(self) -> str:
        return self.tier or derive_tier(self.event, self.level)


class EventWriter:
    """Append one :class:`Event` per line to the run's spine.

    Backward-compatible: the long-standing ``write(level, stage, event, **data)``
    signature is unchanged; ``tier`` / ``phase`` are optional keyword-only extras
    so existing call sites need no edit and still project correctly (their tier is
    derived). Append-only (never truncates) — the spine spans the whole run."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, level: str, stage: str, event: str, *,
              tier: Optional[str] = None, phase: Optional[str] = None, **data: Any) -> Event:
        record = Event(level=level, stage=stage, event=event, data=data, tier=tier, phase=phase)
        line = json.dumps(asdict(record), separators=(",", ":")) + "\n"
        # Best-effort, never let logging crash a run: a transient file lock / disk blip
        # must not abort calibration. The dashboard tolerates gaps; the run goes on.
        # The lock keeps concurrent writers (watchdog thread + main thread) from tearing
        # an interior line on Windows (where append isn't atomic across threads).
        try:
            with _WRITE_LOCK, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass
        return record


class RunLog:
    """Typed front door to the spine, carrying the **current phase** so every event
    is stamped with where the run is (the dashboard's event-log header reads it).

    New producers (orchestrator, measure loop, optimizer) should use this; the typed
    helpers pin the tier so the digest/stream split is correct by construction. Wraps
    an :class:`EventWriter`, so it shares the one append-only ``events.jsonl``."""

    def __init__(self, path: Path, *, phase: Optional[str] = None) -> None:
        self.writer = EventWriter(path)
        self.path = path
        self.phase = phase
        # Running per-event-name counts. A timed check-in snapshots this and diffs against the
        # previous snapshot to report "what happened since the last check-in" (anomalies, seams,
        # reads, optimizer iterations) without re-reading the firehose off disk.
        self.tally: dict[str, int] = {}

    # -- core emit ---------------------------------------------------------
    def emit(self, level: str, stage: str, event: str, *,
             tier: Optional[str] = None, **data: Any) -> Event:
        if tier is None:
            tier = derive_tier(event, level)
        self.tally[event] = self.tally.get(event, 0) + 1
        return self.writer.write(level, stage, event, tier=tier, phase=self.phase, **data)

    # -- phase tracking ----------------------------------------------------
    def set_phase(self, phase: str, **data: Any) -> Event:
        """Enter a new phase. Stamps subsequent events with it and logs the change
        (digest tier — the LLM and the dash header both want phase transitions)."""
        self.phase = phase
        return self.emit("INFO", "run", Ev.PHASE, tier="digest", phase_name=phase, **data)

    # -- typed helpers (tier pinned) --------------------------------------
    def header(self, **data: Any) -> Event:
        """The run header — target, active ccmx/spd correction, mode, monitor,
        display, and the schema version. Emitted once near the start; the dashboard
        status bar reads it (and uses ``run_id`` to hard-reset cleanly between runs)."""
        data.setdefault("schema_version", SCHEMA_VERSION)
        return self.emit("INFO", "run", Ev.RUN_HEADER, tier="digest", **data)

    def stage_start(self, stage: str, **data: Any) -> Event:
        return self.emit("INFO", stage, Ev.STAGE_START, tier="digest", **data)

    def stage_done(self, stage: str, **data: Any) -> Event:
        return self.emit("INFO", stage, Ev.STAGE_DONE, tier="digest", **data)

    def stage_aborted(self, stage: str, **data: Any) -> Event:
        return self.emit("ERROR", stage, Ev.STAGE_ABORTED, tier="digest", **data)

    def patch_read(self, stage: str, **data: Any) -> Event:
        """A compact mirror of one measurements.ndjson read, so the dashboard can
        render the firehose from the single spine. The dumb-browser contract: carry
        ``role, seq, signal, rgb, Y, xy, dE, ok, disposition`` — with ``dE`` computed
        Python-side against the RESOLVED target white (not textbook D65), so the
        dashboard's numbers match verify + the LLM digest. The dense record (spectral,
        provenance, per-read agreement) stays in measurements.ndjson."""
        return self.emit("INFO", stage, Ev.PATCH_READ, tier="stream", **data)

    def heartbeat(self, stage: str, **data: Any) -> Event:
        return self.emit("DEBUG", stage, Ev.HEARTBEAT, tier="stream", **data)

    def progress(self, stage: str, **data: Any) -> Event:
        return self.emit("INFO", stage, Ev.PROGRESS, tier="stream", **data)

    def optimizer_iteration(self, **data: Any) -> Event:
        return self.emit("INFO", "optimize", Ev.OPTIMIZER_ITER, tier="digest", **data)

    def metrics_scored(self, stage: str, **data: Any) -> Event:
        """A scored-metrics summary (the dE big-numbers). Digest tier — the dashboard's
        ΔE panel and the LLM both want it. Carry ``avg/p95/p99/max/white`` plus the
        grayscale-vs-colour split so the dashboard renders the whole panel from this one
        event (no dependence on the per-patch report file)."""
        return self.emit("INFO", stage, "metrics_scored", tier="digest", **data)

    def seam(self, stage: str, **data: Any) -> Event:
        return self.emit("INFO", stage, Ev.SEAM, tier="digest", **data)

    def anomaly(self, stage: str, **data: Any) -> Event:
        return self.emit("WARN", stage, Ev.ANOMALY, tier="digest", **data)

    def check_in(self, stage: str, **data: Any) -> Event:
        return self.emit("INFO", stage, Ev.CHECK_IN, tier="digest", **data)

    def stall(self, stage: str, **data: Any) -> Event:
        return self.emit("ERROR", stage, Ev.STALL, tier="digest", **data)

    def run_done(self, status: str, **data: Any) -> Event:
        """Terminal marker — the run reached an end state (completed / reverted /
        aborted). The dashboard flips the liveness light to a neutral 'done' on this;
        §12's completion gate (later) hangs off it too."""
        level = "ERROR" if status == "aborted" else "INFO"
        return self.emit(level, "run", Ev.RUN_DONE, tier="digest", status=status, **data)

    def note(self, stage: str, message: str, *, level: str = "INFO", **data: Any) -> Event:
        return self.emit(level, stage, Ev.NOTE, message=message, **data)


def event_from_dict(raw: dict[str, Any]) -> Event:
    """Build an :class:`Event` from one parsed JSON line, tolerant of schema drift
    (keep only known fields, default the rest). Shared by :func:`read_events` and the
    dashboard's live tailer so both decode the spine identically."""
    return Event(
        level=raw.get("level", "INFO"),
        stage=raw.get("stage", ""),
        event=raw.get("event", ""),
        data=raw.get("data", {}) or {},
        time=raw.get("time", ""),
        tier=raw.get("tier"),
        phase=raw.get("phase"),
    )


def read_events(path: Path) -> list[Event]:
    events: list[Event] = []
    if not path.exists():
        return events
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue  # a half-written final line during a live tail — skip, don't crash
            events.append(event_from_dict(raw))
    return events


def digest_projection(events: Iterable[Event]) -> list[Event]:
    """The LLM-facing view of the spine: digest-tier events only (boundaries,
    anomalies, seams, check-ins, stalls, the header, phases, optimizer iterations).
    Per-patch reads and heartbeats are dropped — the LLM never tails the firehose."""
    return [e for e in events if e.effective_tier == "digest"]
