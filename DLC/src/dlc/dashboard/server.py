"""The dashboard server — stdlib ``http.server`` + Server-Sent Events over the spine.

One background thread tails ``events.jsonl`` and folds each event into a single
:class:`~dlc.dashboard.state.DashboardState`; a ticker thread re-pushes the distilled
state on a cadence so timers and the liveness light advance even when the run is silent
(the soak). Browsers connect to ``/events`` (SSE) and only render what the server sends —
no colour math, no aggregation client-side.

Robustness choices that matter:

* **The server is the source of truth for aggregates.** A late-joining or reconnecting
  browser gets a full ``state`` + a backlog of recent log lines on connect, so it's
  correct immediately and after any network blip.
* **Slow clients can't wedge the run-watcher.** Each SSE client has a bounded queue;
  if it can't keep up we drop the oldest message for *that* client (it self-heals on the
  next ``state`` push) rather than blocking the broadcaster.
* **It survives the run.** Liveness is judged from event-age, not the socket, and on a
  run switch (``active.json`` repoints) the hub resets cleanly and tells every client.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from ..events import Event
from .report import render_report_html
from .state import DashboardState
from .tail import EventTail

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
_BACKLOG = 4000          # log lines a fresh client receives on connect (browser caps its own DOM)
_CLIENT_QUEUE_MAX = 8000  # per-client SSE backlog before we shed oldest messages


class Hub:
    """Shared run state + the SSE fan-out. Thread-safe; one per server."""

    def __init__(self, tail: EventTail, *, backlog: int = _BACKLOG) -> None:
        self.tail = tail
        self.state = DashboardState()
        self.recent: deque[dict[str, Any]] = deque(maxlen=backlog)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()          # guards the subscriber list / broadcast
        # Separate lock for state+recent: HTTP handler threads snapshot() while the tail
        # thread mutates/reassigns state. Kept distinct from _lock so a broadcast (under
        # _lock) inside the ingest path can't deadlock against a snapshot (under this).
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- subscription ------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_CLIENT_QUEUE_MAX)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Slow client: shed its oldest message and retry once. A dropped append
                # is recovered by the next periodic state push, so the client self-heals.
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except queue.Empty:
                    pass
                except queue.Full:
                    pass

    # -- snapshots ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return self.state.snapshot(datetime.now())

    def backlog(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self.recent)

    def charts(self) -> dict[str, Any]:
        with self._state_lock:
            return self.state.charts()

    def digest(self) -> list[dict[str, Any]]:
        """The LLM-facing projection: the digest-tier slice of what we've tailed (the
        per-patch / heartbeat / progress firehose dropped). The live twin of
        ``python -m dlc.digest`` — an assistant can poll this to check in on a run."""
        with self._state_lock:
            return [w for w in self.recent if w.get("tier") == "digest"]

    def run_root(self) -> Optional[Path]:
        cur = self.tail.current
        return cur.parent if cur is not None else None

    # -- background loops --------------------------------------------------
    def start(self, *, poll_interval: float = 0.25, tick_interval: float = 2.0) -> None:
        self._threads = [
            threading.Thread(target=self._tail_loop, args=(poll_interval,),
                             name="dash-tail", daemon=True),
            threading.Thread(target=self._tick_loop, args=(tick_interval,),
                             name="dash-tick", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _ingest_event(self, ev: Event) -> None:
        try:
            with self._state_lock:
                wire = self.state.ingest(ev)
                self.recent.append(wire)
        except Exception:  # noqa: BLE001 - one malformed event must never poison the batch
            return
        self._broadcast({"type": "append", "event": wire})

    def _tail_loop(self, poll_interval: float) -> None:
        while not self._stop.is_set():
            try:
                events, switched = self.tail.poll()
                if switched:
                    with self._state_lock:
                        self.state = DashboardState()
                        self.recent.clear()
                    self._broadcast({"type": "reset", "state": self.snapshot(),
                                     "backlog": []})
                for ev in events:
                    self._ingest_event(ev)
                if events:
                    self._broadcast({"type": "state", "state": self.snapshot()})
            except Exception:  # noqa: BLE001 - a watcher must never die on a bad line
                pass
            self._stop.wait(poll_interval)

    def _tick_loop(self, tick_interval: float) -> None:
        while not self._stop.is_set():
            self._stop.wait(tick_interval)
            if self._stop.is_set():
                break
            try:
                self._broadcast({"type": "state", "state": self.snapshot()})
            except Exception:  # noqa: BLE001
                pass


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves the SPA + the SSE stream + the small JSON API. ``hub`` is bound per-server."""

    hub: Hub = None  # type: ignore[assignment]
    assets_dir: Path = ASSETS_DIR
    server_version = "DLCDashboard/1.0"

    # Quiet by default — the dashboard's own event log is the place to look, not stderr.
    def log_message(self, *args: Any) -> None:  # noqa: D401
        pass

    # -- helpers -----------------------------------------------------------
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", status)

    def _send_asset(self, name: str) -> None:
        # Only serve known, flat asset names — no traversal.
        safe = Path(name).name
        path = self.assets_dir / safe
        if not path.is_file():
            self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)
            return
        self._send_bytes(path.read_bytes(),
                         _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))

    # -- routing -----------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/cancel":
            self._send_json(self._cancel_run())
        else:
            self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)

    def _cancel_run(self) -> dict[str, Any]:
        """Write control.json into the watched run so the live calibration process rolls back
        to the pre-run setup at its next checkpoint/stage boundary — the Cancel button (and any
        assistant POSTing here). The read-only dashboard's one mutating action; it touches only
        the run's own control file, never the display."""
        root = self.hub.run_root()
        if root is None:
            return {"ok": False, "error": "no run is being watched"}
        try:
            ctrl = root / "control.json"
            ctrl.write_text(json.dumps({"action": "cancel",
                                        "requested": datetime.now().isoformat(timespec="seconds")}),
                            encoding="utf-8")
            return {"ok": True, "control": str(ctrl)}
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_asset("index.html")
        elif path.startswith("/static/"):
            self._send_asset(path[len("/static/"):])
        elif path == "/events":
            self._serve_sse()
        elif path == "/api/snapshot":
            self._send_json({"state": self.hub.snapshot(), "backlog": self.hub.backlog()})
        elif path == "/api/charts":
            self._send_json(self.hub.charts())
        elif path == "/api/digest":
            digest = self.hub.digest()
            self._send_json({"count": len(digest), "digest": digest})
        elif path == "/api/patch_metrics":
            self._send_json(self._latest_patch_metrics())
        elif path == "/api/export":
            self._send_json(self._export_snapshot())
        else:
            self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)

    # -- SSE ---------------------------------------------------------------
    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # defeat any proxy buffering
        self.end_headers()

        q = self.hub.subscribe()
        try:
            # Prime the client with full truth: current state + the recent log backlog.
            self.wfile.write(_sse("state", self.hub.snapshot()))
            self.wfile.write(_sse("backlog", {"events": self.hub.backlog()}))
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keep the connection (and proxies) alive
                    self.wfile.flush()
                    continue
                kind = msg.get("type")
                if kind == "append":
                    self.wfile.write(_sse("append", msg["event"]))
                elif kind == "state":
                    self.wfile.write(_sse("state", msg["state"]))
                elif kind == "reset":
                    self.wfile.write(_sse("reset", msg["state"]))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # client went away — normal
        finally:
            self.hub.unsubscribe(q)

    # -- API helpers -------------------------------------------------------
    def _latest_patch_metrics(self) -> dict[str, Any]:
        """The newest scored ``*_patch_metrics.json`` from the run's reports/ — the full
        per-patch dE the spine summary doesn't carry (for p99 + grayscale/colour split)."""
        root = self.hub.run_root()
        if root is None:
            return {"available": False}
        reports = root / "reports"
        if not reports.is_dir():
            return {"available": False}
        files = sorted(reports.glob("*_patch_metrics.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return {"available": False}
        try:
            metrics = json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False}
        return {"available": True, "source": files[-1].name, "patches": metrics}

    def _export_snapshot(self) -> dict[str, Any]:
        """Write a self-contained HTML report (+ a JSON snapshot) to the run's reports/ and
        return where they landed. The HTML embeds the state + chart data and inlines the
        chart renderer, so it opens later with no server — a permanent run artifact."""
        snap = self.hub.snapshot()
        charts = self.hub.charts()
        payload = {"exported_at": datetime.now().isoformat(timespec="seconds"),
                   "state": snap, "charts": charts, "backlog_lines": len(self.hub.backlog())}
        root = self.hub.run_root()
        if root is None:
            return {**payload, "saved_to": None}
        try:
            reports = root / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            json_out = reports / f"dashboard_snapshot_{stamp}.json"
            json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            html_out = reports / f"report_{stamp}.html"
            html_out.write_text(render_report_html(snap, charts, payload["exported_at"],
                                                   self.assets_dir), encoding="utf-8")
            return {**payload, "saved_to": str(html_out), "json_saved_to": str(json_out)}
        except OSError:
            return {**payload, "saved_to": None}


def make_server(hub: Hub, *, host: str = "127.0.0.1", port: int = 8765,
                assets_dir: Path = ASSETS_DIR) -> ThreadingHTTPServer:
    # Bind the hub/assets onto a fresh subclass so concurrent servers don't share state.
    class _Bound(DashboardHandler):
        pass

    _Bound.hub = hub
    _Bound.assets_dir = assets_dir
    httpd = ThreadingHTTPServer((host, port), _Bound)
    httpd.daemon_threads = True
    return httpd


def serve(*, run: Optional[Path] = None, runs_dir: Optional[Path] = None,
          host: str = "127.0.0.1", port: int = 8765) -> None:
    """Blocking: start the tail/ticker + the HTTP server and serve until interrupted."""
    if run is not None:
        tail = EventTail(path=run / "events.jsonl" if run.is_dir() else run)
    elif runs_dir is not None:
        tail = EventTail(runs_dir=runs_dir)
    else:
        raise ValueError("serve() needs run= or runs_dir=")

    hub = Hub(tail)
    hub.start()
    httpd = make_server(hub, host=host, port=port)
    target = tail.current if run is not None else f"{runs_dir} (following active.json)"
    print(f"DLC dashboard → http://{host}:{port}   watching {target}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        httpd.shutdown()
