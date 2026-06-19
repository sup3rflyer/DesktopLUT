"""The dashboard HTTP/SSE server + the hub fan-out."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from dlc.events import EventWriter
from dlc.dashboard.server import Hub, make_server
from dlc.dashboard.tail import EventTail


def _drain(hub: Hub) -> None:
    """Pump the tail once, synchronously (no threads), into the hub state."""
    events, switched = hub.tail.poll()
    if switched:
        from dlc.dashboard.state import DashboardState
        hub.state = DashboardState()
        hub.recent.clear()
    for ev in events:
        hub._ingest_event(ev)


def _hub_with_events(tmp_path: Path) -> Hub:
    path = tmp_path / "events.jsonl"
    w = EventWriter(path)
    w.write("INFO", "run", "run_header", run_id="r1", target="srgb_g22", mode="SDR")
    w.write("INFO", "measure", "stage_start")
    w.write("INFO", "measure", "patch_read", tier="stream",
            seq=0, role="measurement", rgb=[255, 255, 255], Y=120.0, xy=[0.3127, 0.329], ok=True)
    hub = Hub(EventTail(path=path))
    _drain(hub)
    return hub


def test_hub_folds_events_into_snapshot(tmp_path):
    hub = _hub_with_events(tmp_path)
    snap = hub.snapshot()
    assert snap["header"]["target"] == "srgb_g22"
    assert snap["run_status"] == "running"
    assert snap["counters"]["reads_ok"] == 1
    assert abs(snap["last_white"]["cct"] - 6504) < 60
    assert len(hub.backlog()) == 3


def test_hub_broadcast_reaches_subscribers(tmp_path):
    hub = Hub(EventTail(path=tmp_path / "events.jsonl"))
    q = hub.subscribe()
    EventWriter(tmp_path / "events.jsonl").write("INFO", "s", "stage_start")
    _drain(hub)
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    kinds = [m["type"] for m in msgs]
    assert "append" in kinds


def _serve(hub: Hub):
    httpd = make_server(hub, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}"


def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def _post(url: str, *, token: str | None = None, origin: str | None = None,
          host: str | None = None, timeout: float = 5.0):
    headers = {}
    if token:
        headers["X-DLC-CSRF-Token"] = token
    if origin:
        headers["Origin"] = origin
    if host:
        headers["Host"] = host
    req = urllib.request.Request(url, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _csrf_token(base: str) -> str:
    status, body = _get(base + "/api/snapshot")
    assert status == 200
    return json.loads(body)["csrf_token"]


def test_http_serves_spa_and_assets(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        status, body = _get(base + "/")
        assert status == 200 and b"mission control" in body
        assert b'tabindex="0" role="button"' in body
        assert b'role="dialog" aria-modal="true" aria-labelledby="lb-title"' in body
        assert b'aria-label="Close chart view"' in body
        status, js = _get(base + "/static/dashboard.js")
        assert status == 200 and b"EventSource" in js
        assert b"lightboxReturnFocus" in js
        assert b'e.key !== "Enter" && e.key !== " "' in js
        assert b'e.key !== "Tab"' in js
        status, css = _get(base + "/static/dashboard.css")
        assert status == 200
        assert b".chart:focus-visible" in css
    finally:
        httpd.shutdown()


def test_api_snapshot_returns_state_and_backlog(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        status, body = _get(base + "/api/snapshot")
        assert status == 200
        payload = json.loads(body)
        assert payload["state"]["header"]["target"] == "srgb_g22"
        assert len(payload["backlog"]) == 3
        assert isinstance(payload["csrf_token"], str) and payload["csrf_token"]
    finally:
        httpd.shutdown()


def test_api_digest_projects_digest_tier_only(tmp_path):
    # The live twin of `python -m dlc.digest`: the LLM pulls /api/digest to check in on a
    # run and gets the boundaries, NOT the per-patch firehose.
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        status, body = _get(base + "/api/digest")
        assert status == 200
        data = json.loads(body)
        names = [e["event"] for e in data["digest"]]
        assert "run_header" in names and "stage_start" in names
        assert "patch_read" not in names              # firehose dropped from the LLM view
        assert data["count"] == len(data["digest"])
    finally:
        httpd.shutdown()


def test_api_cancel_writes_control_json(tmp_path):
    # The dashboard's Cancel button POSTs here; it writes control.json into the watched run
    # so the live process rolls back at its next checkpoint (the actionable half of gating).
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        status, body = _post(base + "/api/cancel", token=_csrf_token(base))
        assert status == 200
        data = json.loads(body)
        assert data["ok"] is True
        ctrl = tmp_path / "control.json"               # run root == events.jsonl's parent
        assert ctrl.exists() and json.loads(ctrl.read_text())["action"] == "cancel"
    finally:
        httpd.shutdown()


def test_api_pause_and_resume_write_control_json(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        token = _csrf_token(base)
        status, body = _post(base + "/api/pause", token=token)
        assert status == 200
        assert json.loads(body)["ok"] is True
        ctrl = tmp_path / "control.json"
        pause = json.loads(ctrl.read_text())
        assert pause["action"] == "pause"
        assert pause["timeout_s"] == 180
        assert pause["on_timeout"] == "rollback"

        status, body = _post(base + "/api/resume", token=token)
        assert status == 200
        assert json.loads(body)["ok"] is True
        assert json.loads(ctrl.read_text())["action"] == "resume"
    finally:
        httpd.shutdown()


def test_api_cancel_requires_csrf_token(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        try:
            _post(base + "/api/cancel")
            assert False, "expected 403"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        assert not (tmp_path / "control.json").exists()
    finally:
        httpd.shutdown()


def test_mutating_api_rejects_cross_origin(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        token = _csrf_token(base)
        try:
            _post(base + "/api/cancel", token=token, origin="http://evil.example")
            assert False, "expected 403"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        assert not (tmp_path / "control.json").exists()
    finally:
        httpd.shutdown()


def test_mutating_api_rejects_unexpected_host(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        token = _csrf_token(base)
        try:
            _post(base + "/api/cancel", token=token, host="evil.example")
            assert False, "expected 403"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        assert not (tmp_path / "control.json").exists()
    finally:
        httpd.shutdown()


def test_sse_primes_with_state_then_streams(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        with urllib.request.urlopen(base + "/events", timeout=5.0) as resp:
            assert resp.headers.get("Content-Type") == "text/event-stream"
            head = resp.read(120)  # enough to capture the priming 'state' frame
            assert b"event: state" in head
    finally:
        httpd.shutdown()


def test_export_writes_html_report_to_reports(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        status, body = _post(base + "/api/export", token=_csrf_token(base))
        assert status == 200
        payload = json.loads(body)
        saved = payload["saved_to"]
        assert saved and Path(saved).exists()
        assert Path(saved).name.endswith(".html") and Path(saved).parent.name == "reports"
        html = Path(saved).read_text(encoding="utf-8")
        # self-contained: inlines the chart renderer + embeds data + shows the summary
        assert "DLCCharts" in html
        assert 'data-chart="cie"' in html
        assert "srgb_g22" in html               # the target made it into the summary
        # the JSON sidecar is written too
        assert Path(payload["json_saved_to"]).exists()
    finally:
        httpd.shutdown()


def test_export_is_post_only(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        try:
            _get(base + "/api/export")
            assert False, "expected 405"
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
        assert not (tmp_path / "reports").exists()
    finally:
        httpd.shutdown()


def test_api_charts_returns_chart_datasets(tmp_path):
    hub = _hub_with_events(tmp_path)   # has one neutral white read
    httpd, base = _serve(hub)
    try:
        status, body = _get(base + "/api/charts")
        assert status == 200
        ch = json.loads(body)
        assert set(ch) >= {"cie", "grayscale", "eotf", "optimizer", "white_track"}
        assert len(ch["cie"]["points"]) == 1            # the one good read
        assert len(ch["cie"]["locus"]) == 31            # Planckian locus always present
    finally:
        httpd.shutdown()


def test_unknown_route_404(tmp_path):
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    try:
        try:
            _get(base + "/nope")
            assert False, "expected 404"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
