"""Live state files — the dashboard's fold, published to disk for DesktopLUT's DLC tab.

The browser dashboard serves :class:`~dlc.dashboard.state.DashboardState` over HTTP; DesktopLUT
(C++) is a second, equally dumb renderer of the SAME fold, so it reads files instead of a socket:

* ``runs/<id>/live/state.json``  — ``DashboardState.snapshot()`` + a digest-tier ``feed`` (the
  event log's judgment lines, each with a Python-made ``title``/``text``), rewritten while the
  run moves and at least every ``tick_interval`` (liveness ages advance with no new events).
* ``runs/<id>/live/charts.json`` — ``DashboardState.charts()``, rewritten every
  ``charts_interval`` while reads arrive, at every stage change, and on stop.

One authority for the colour maths and the liveness verdict (this package); the C++ side does no
colour science. Both files carry ``live_schema`` and a monotonically increasing ``seq``; both are
written atomically (temp + replace), with NaN/inf mapped to null so any JSON parser reads them.

A viewer must never touch a run: :class:`LivePublisher` runs on its own daemon thread, tails the
run's own ``events.jsonl`` (no hook into the writer), and swallows every error — a failure is
logged once through ``log`` and retried on the next tick. The final ``stop()`` flush makes the
files the run's record ("what the run looked like when it ended or paused").

``python -m dlc.dashboard.publish --run <dir>`` folds a finished run once (old runs, tests).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Optional

from ..events import Ev
from ..paths import atomic_write_text
from .state import DashboardState
from .tail import EventTail

LIVE_SCHEMA = 1
LIVE_DIR = "live"
STATE_FILE = "state.json"
CHARTS_FILE = "charts.json"
NOTES_FILE = "notes.jsonl"     # one-line notes the supervising LLM posts (dlc.note); never the spine
FEED_MAX = 60


def live_dir(run_root: Path) -> Path:
    return Path(run_root) / LIVE_DIR


def _finite(value: Any) -> Any:
    """JSON-safe copy: NaN/inf → None (Python's json writes bare NaN, which strict parsers reject)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _num(value: Any, digits: int = 2) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return f"{value:.{digits}f}"


def _join(*parts: Optional[str]) -> str:
    return " · ".join(p for p in parts if p)


def feed_entry(wire: dict[str, Any]) -> dict[str, Any]:
    """One digest-tier event as a feed row: ``title`` (short, upper-cased by the renderer) and a
    terse ``text``. The renderer prints these verbatim — all wording lives here."""
    ev = wire.get("event") or ""
    stage = wire.get("stage") or ""
    d = wire.get("data") or {}
    title, text = ev.replace("_", " "), ""
    if ev == Ev.RUN_HEADER:
        title, text = "run", _join(d.get("flow"), d.get("mode"), d.get("target"))
    elif ev == Ev.PHASE:
        title, text = "phase", str(d.get("phase_name") or "")
    elif ev == Ev.STAGE_START:
        title, text = f"stage {stage}", "start"
    elif ev == Ev.STAGE_DONE:
        title = f"stage {stage}"
        text = _join(str(d.get("status") or ""), "replayed" if d.get("replayed") else None)
    elif ev == Ev.STAGE_ABORTED:
        title = f"aborted {stage}"
        text = _join(str(d.get("message") or ""),
                     "stalled" if d.get("stalled") else None, "cancelled" if d.get("cancelled") else None)
    elif ev == Ev.SEAM:
        title = str(d.get("key") or stage or "seam")
        text = _join(str(d.get("status") or ""), d.get("choice"), d.get("note"))
    elif ev == Ev.ANOMALY:
        title = "anomaly" if not d.get("kind") else f"anomaly {d.get('kind')}"
        text = str(d.get("message") or d.get("reason") or "")
    elif ev == Ev.CHECK_IN:
        prog = d.get("progress")
        pct = f"{round(prog * 100)}%" if isinstance(prog, (int, float)) and math.isfinite(prog) else None
        seq = d.get("seq")
        tag = pct or (f"#{seq}" if seq else None)
        title = f"check-in {tag}" if tag else "check-in"
        evidence = d.get("evidence") or {}
        max_de = _num(evidence.get("max_dE"))
        warnings = evidence.get("warnings") or []
        n_warn = sum(1 for w in warnings if "truncated" not in w) + sum(
            int(w.get("truncated") or 0) for w in warnings)
        since = d.get("since_last") or {}
        text = _join(
            str(d.get("message")) if d.get("message") else None,
            f"max {max_de}" + (f" @ {evidence.get('max_dE_patch')}" if evidence.get("max_dE_patch") else "")
            if max_de else None,
            f"{d.get('patches_done')}/{d.get('patches_total')}" if d.get("patches_total") else None,
            f"{n_warn} warnings" if n_warn else None,
            f"{since.get('drift_episodes')} drift" if since.get("drift_episodes") else None)
    elif ev == Ev.STALL:
        title, text = "stall", _join(str(d.get("message") or ""), d.get("via"))
    elif ev == "metrics_scored":
        title = "scored"
        text = _join(*(f"{k} {_num(d.get(v))}" for k, v in (
            ("avg", "avg_de2000"), ("p95", "p95_de2000"), ("max", "max_de2000")) if _num(d.get(v))))
    elif ev == Ev.RUN_DONE:
        title, text = "run done", _join(str(d.get("status") or ""), d.get("message"))
    elif ev == Ev.NOTE:
        title, text = "note", str(d.get("message") or "")
    else:
        bits = [f"{k} {v}" for k, v in list(d.items())[:4]
                if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
        text = _join(*bits)
    return {"time": wire.get("time"), "level": wire.get("level"), "stage": stage, "event": ev,
            "title": title, "text": text}


def load_content_weights(path: Any) -> dict[tuple, tuple]:
    """A content-sampled verify set's per-patch weights ({code triple: (weight, role)}) from its file (the
    run header's ``verify_patches_file``); {} on any read problem. Never reads the run's own state files: the
    run replaces them atomically, and an open reader on Windows would make that replace fail."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        codes, weights = doc.get("codes") or [], doc.get("content_weight") or []
        roles = doc.get("role") or [None] * len(codes)
        out: dict[tuple, tuple] = {}
        for c, w, r in zip(codes, weights, roles):
            key = tuple(int(v) for v in c[:3])
            if key not in out:   # a repeated signal (a bookend) carries 0 after its first occurrence
                out[key] = (float(w), r)
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def read_notes(run_root: Path, limit: int = FEED_MAX) -> list[dict[str, Any]]:
    """The supervisor's notes (``notes.jsonl``) as feed rows; unreadable lines are skipped."""
    path = Path(run_root) / NOTES_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or not obj.get("message"):
            continue
        out.append({"time": obj.get("time"), "level": "INFO", "stage": obj.get("stage") or "",
                    "event": "supervisor_note", "title": str(obj.get("author") or "claude"),
                    "text": str(obj["message"])})
    return out


class LivePublisher:
    """Fold a run's spine and keep ``live/state.json`` + ``live/charts.json`` current.

    ``pump()`` is one poll + fold + (throttled) write — the thread calls it in a loop and tests
    call it directly with an injected ``now``. ``flush()`` writes both files unconditionally."""

    def __init__(self, run_root: Path, *, poll_interval: float = 0.25, state_interval: float = 0.5,
                 tick_interval: float = 2.0, charts_interval: float = 3.0,
                 log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.run_root = Path(run_root)
        self.out_dir = live_dir(self.run_root)
        self.state = DashboardState()
        self.tail = EventTail(path=self.run_root / "events.jsonl")
        self.feed: Deque[dict[str, Any]] = deque(maxlen=FEED_MAX)
        self.poll_interval = poll_interval
        self.state_interval = state_interval
        self.tick_interval = tick_interval
        self.charts_interval = charts_interval
        self._log = log
        self._clock = clock
        self._seq = 0
        self._state_at: Optional[datetime] = None
        self._charts_at: Optional[datetime] = None
        self._state_dirty = True
        self._charts_dirty = True
        self._chart_stage: Optional[str] = None
        self._failures = 0
        self._logged_failure = False
        self._weights_from: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- folding -------------------------------------------------------------
    def _maybe_load_weights(self) -> None:
        """Load the content weights once the fold knows the run's content-sampled verify file (the header, or the
        scored verification for older runs). One read of that file, never of the run's own state."""
        path = self.state.verify_patches_file
        if not path or self.state.content_weights or path == self._weights_from:
            return
        self._weights_from = path
        self.state.content_weights = load_content_weights(path)

    def _ingest_new(self) -> int:
        events, switched = self.tail.poll()
        if switched:   # fixed path: only on first resolve; a reset keeps the fold consistent
            self.state = DashboardState()
            self.feed.clear()
            self._weights_from = None
        n = self._ingest_events(events)
        self._maybe_load_weights()
        return n

    def _ingest_events(self, events) -> int:
        n = 0
        for ev in events:
            try:
                wire = self.state.ingest(ev)
            except Exception:  # noqa: BLE001 - one malformed event must never poison the fold
                continue
            n += 1
            if wire.get("tier") == "digest":
                self.feed.append(feed_entry(wire))
        if n:
            self._state_dirty = True
            self._charts_dirty = True
        return n

    # -- writing -------------------------------------------------------------
    def _feed_rows(self) -> list[dict[str, Any]]:
        rows = list(self.feed) + read_notes(self.run_root)
        rows.sort(key=lambda r: str(r.get("time") or ""))
        return rows[-FEED_MAX:]

    def _write(self, name: str, payload: dict[str, Any]) -> None:
        text = json.dumps(_finite(payload), separators=(",", ":"), allow_nan=False)
        atomic_write_text(self.out_dir / name, text)

    def _write_state(self, now: datetime) -> None:
        self._seq += 1
        snap = self.state.snapshot(now)
        snap.update({"live_schema": LIVE_SCHEMA, "seq": self._seq, "pid": os.getpid(),
                     "published_iso": now.isoformat(timespec="seconds"),
                     "run_dir": str(self.run_root), "feed": self._feed_rows()})
        self._write(STATE_FILE, snap)
        self._state_at = now
        self._state_dirty = False

    def _write_charts(self, now: datetime) -> None:
        self._seq += 1
        charts = self.state.charts()
        charts.update({"live_schema": LIVE_SCHEMA, "seq": self._seq,
                       "published_iso": now.isoformat(timespec="seconds"),
                       "events_seen": self.state.events_seen})
        self._write(CHARTS_FILE, charts)
        self._charts_at = now
        self._charts_dirty = False
        self._chart_stage = charts.get("stage")

    @staticmethod
    def _age(now: datetime, then: Optional[datetime]) -> float:
        return float("inf") if then is None else (now - then).total_seconds()

    def pump(self, now: Optional[datetime] = None) -> bool:
        """Poll, fold, and write what is due. Returns True when anything was written. Never raises."""
        with self._lock:
            now = now or self._clock()
            wrote = False
            try:
                self._ingest_new()
                if ((self._state_dirty and self._age(now, self._state_at) >= self.state_interval)
                        or self._age(now, self._state_at) >= self.tick_interval):
                    self._write_state(now)
                    wrote = True
                stage_moved = self.state.stage is not None and self.state.stage != self._chart_stage
                if self._charts_dirty and (stage_moved
                                           or self._age(now, self._charts_at) >= self.charts_interval):
                    self._write_charts(now)
                    wrote = True
                self._failures = 0
            except Exception as exc:  # noqa: BLE001 - a viewer must never touch the run
                self._failed(exc)
            return wrote

    def flush(self, now: Optional[datetime] = None) -> bool:
        """Fold everything outstanding and write both files now. Never raises."""
        with self._lock:
            now = now or self._clock()
            try:
                self._ingest_new()
                self._write_state(now)
                self._write_charts(now)
                return True
            except Exception as exc:  # noqa: BLE001
                self._failed(exc)
                return False

    def _failed(self, exc: Exception) -> None:
        self._failures += 1
        if self._log is not None and not self._logged_failure:
            self._logged_failure = True
            try:
                self._log(f"live state publisher: {type(exc).__name__}: {exc} (retrying; the run is unaffected)")
            except Exception:  # noqa: BLE001
                pass

    # -- thread --------------------------------------------------------------
    def start(self) -> "LivePublisher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="dlc-live-publisher", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.pump()
            self._stop.wait(self.poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread and write the final state (the run's record of how it ended / paused)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.flush()


def publish_once(run_root: Path, now: Optional[datetime] = None) -> bool:
    """Fold a run's whole spine and write both files (a finished run, or a test)."""
    return LivePublisher(run_root).flush(now)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - thin CLI
    parser = argparse.ArgumentParser(prog="python -m dlc.dashboard.publish",
                                     description="Write live/state.json + live/charts.json for a run.")
    parser.add_argument("--run", type=Path, required=True, help="run directory (holds events.jsonl)")
    args = parser.parse_args(argv)
    if not (args.run / "events.jsonl").exists():
        print(f"no events.jsonl in {args.run}")
        return 2
    ok = publish_once(args.run)
    print(str(live_dir(args.run)) if ok else "publish failed")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
