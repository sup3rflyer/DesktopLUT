"""The run-spine contract every viewer of a run relies on — the browser dashboard, the planned
DesktopLUT DLC tab, and the overseeing LLM (DLC tab plan, phase A):

1. a PAUSED seam carries the whole judgment packet on the spine (not only in the exit-10 stdout);
2. the manifest records the exact launch argv + cwd, keeping the first launch across resumes;
3. every ``patch_read`` carries its ΔE once the target is known, so a check-in's max ΔE / worst
   patch are real;
4. the dashboard's control.json writes are atomic;
5. ``runs/active.json`` says who is running what, and in which state.

Engine-tier (the orchestrator runs against the synthetic panel + mock controller), so guarded
like ``test_calibrate.py``. No pipe, meter, dogegen window or display is touched."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc.adjudication import (
    SEAM_PLAN,
    AdjudicationRequest,
    AdjudicationRequired,
    Decision,
    MappingAdjudicator,
    SupervisedAdjudicator,
)
from dlc.calibrate import Calibration, patch_read_scorer
from dlc.checkin import window_evidence
from dlc.dashboard.state import DashboardState
from dlc.events import Ev, EventWriter, digest_projection, read_events
from dlc.paths import runs_dir
from dlc.runs import create_run, open_run, publish_active_pointer, update_active_pointer
from test_calibrate import _make, _perfect_hdr_panel


def _pointer() -> dict[str, Any]:
    return json.loads((runs_dir() / "active.json").read_text(encoding="utf-8"))


def _seams(calib: Calibration, status: str) -> list:
    return [e for e in read_events(calib.ctx.events_path)
            if e.event == Ev.SEAM and e.data.get("status") == status]


def _wire(value: Any) -> Any:
    """What a value looks like after a trip through the spine's JSON."""
    return json.loads(json.dumps(value))


# ---------------------------------------------------------------------------
# 1. paused seams carry the full packet
# ---------------------------------------------------------------------------

def _assert_packet_matches_request(data: dict[str, Any], req: AdjudicationRequest) -> None:
    """The seam event holds everything the exit-10 request prints (key + stage ride the event)."""
    exit10 = req.as_dict()
    assert data["key"] == exit10["key"]
    for k, v in exit10.items():
        if k in ("key", "stage"):
            continue
        assert data[k] == _wire(v), k


def test_paused_seam_carries_the_full_packet_and_checklist(tmp_path: Path):
    calib = _make(tmp_path, "bc_pause", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("build-correction")
    req = exc.value.request
    assert req.key == "probe-match:build"

    paused = _seams(calib, "paused")
    assert len(paused) == 1
    ev = paused[0]
    assert ev.stage == req.stage
    assert ev.effective_tier == "digest" and ev in digest_projection(read_events(calib.ctx.events_path))
    _assert_packet_matches_request(ev.data, req)
    assert ev.data["seam"] == "probe_match"
    assert ev.data["recommendation"] == "done"
    # The operator checklist is lifted to the top of the packet (it also stays in the digest).
    assert ev.data["checklist"] == req.digest["checklist"] and len(ev.data["checklist"]) >= 4
    assert ev.data["digest"]["checklist"] == ev.data["checklist"]


def test_hardware_readiness_pause_carries_its_checklist(tmp_path: Path):
    calib = _make(tmp_path, "ready", adjudicator=SupervisedAdjudicator(),
                  require_hardware_readiness=True)
    with pytest.raises(AdjudicationRequired) as exc:
        calib.run("mhc-only")
    req = exc.value.request
    assert req.key == "hardware-readiness:confirm"
    ev = _seams(calib, "paused")[-1]
    _assert_packet_matches_request(ev.data, req)
    checklist = ev.data["checklist"]
    assert len(checklist) == 3
    assert "monitor 0" in checklist[0] and "DogeGen" in checklist[1] and "sensor" in checklist[2]
    # seam semantics unchanged: same options + recommendation
    assert ev.data["options"] == ["ready", "abort"] and ev.data["recommendation"] == "ready"


class _OffVocabulary:
    def adjudicate(self, request: AdjudicationRequest) -> Decision:
        return Decision("zzz")


def _structured_request() -> AdjudicationRequest:
    return AdjudicationRequest(
        key="adaptive-planning:plan", seam=SEAM_PLAN, stage="adaptive-planning",
        question="which strategy?", options=("apply", "abort"), recommendation="apply",
        # a numpy scalar in a digest must not crash the spine writer
        digest={"checklist": ["look at the panel"], "n": np.int64(3), "path": Path("x") / "y"},
        recommended_payload={"shadow_treatment": "standard"})


@pytest.mark.parametrize("adjudicator", [MappingAdjudicator({}), _OffVocabulary()],
                         ids=["undecided", "invalid_choice"])
def test_both_pause_paths_carry_recommended_payload_and_checklist(tmp_path: Path, adjudicator):
    calib = _make(tmp_path, "structured", adjudicator=adjudicator)
    req = _structured_request()
    with pytest.raises(AdjudicationRequired):
        calib.adjudicate(req)
    ev = _seams(calib, "paused")[-1]
    assert ev.data["recommended_payload"] == {"shadow_treatment": "standard"}
    assert ev.data["checklist"] == ["look at the panel"]
    assert ev.data["digest"]["n"] == 3 and ev.data["digest"]["path"] == str(Path("x") / "y")
    assert ev.data["question"] == "which strategy?" and ev.data["options"] == ["apply", "abort"]


def test_auto_accepted_packet_uses_the_same_shape(tmp_path: Path):
    calib = _make(tmp_path, "supervised", adjudicator=SupervisedAdjudicator())
    req = AdjudicationRequest(key="probe-match:build", seam="probe_match", stage="probe-match",
                              question="q", options=("done", "skip"), recommendation="done",
                              digest={"checklist": ["a", "b"]})
    assert calib.adjudicate(req).choice == "done"
    ev = _seams(calib, "auto_accepted")[-1]
    _assert_packet_matches_request(ev.data, req)
    assert ev.data["checklist"] == ["a", "b"] and "veto" in ev.data and ev.data["choice"] == "done"


# ---------------------------------------------------------------------------
# 2. the launch argv in the manifest
# ---------------------------------------------------------------------------

def test_record_launch_keeps_the_first_launch_and_appends_resumes(tmp_path: Path):
    ctx = create_run("SDR", display="synthetic", run_dir=tmp_path / "run")
    ctx.record_launch(["--flow", "full", "--profile", "p.yaml"], cwd=tmp_path, program="dlc-calibrate")
    reopened = open_run(tmp_path / "run")
    reopened.record_launch(["--run", str(tmp_path / "run"), "--decide", "a:b=c"], cwd=tmp_path / "x")
    raw = json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))
    assert raw["launch"]["argv"] == ["--flow", "full", "--profile", "p.yaml"]
    assert raw["launch"]["cwd"] == str(tmp_path) and raw["launch"]["program"] == "dlc-calibrate"
    assert raw["launch"]["python"] and raw["launch"]["time"] and raw["launch"]["pid"] == os.getpid()
    assert [r["argv"] for r in raw["resumes"]] == [["--run", str(tmp_path / "run"), "--decide", "a:b=c"]]
    assert raw["resumes"][0]["cwd"] == str(tmp_path / "x") and "program" not in raw["resumes"][0]
    # a manifest from before the field existed opens with empty records
    legacy = tmp_path / "legacy"
    create_run("SDR", run_dir=legacy)
    data = json.loads((legacy / "manifest.json").read_text(encoding="utf-8"))
    data.pop("launch"), data.pop("resumes")
    (legacy / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert open_run(legacy).manifest.launch == {} and open_run(legacy).manifest.resumes == []


def test_main_records_argv_across_a_resume_and_moves_the_active_state(tmp_path, monkeypatch, capsys):
    from test_park_window import _run_main

    full_field = {"patch_size_pct": 100, "area_pct": 100.0}
    rc, out, _ = _run_main(tmp_path, monkeypatch, capsys, tech="WOLED", window=full_field)
    assert rc == 10                                    # paused at preflight:patch-window
    run = tmp_path / "run"
    first = ["--flow", "full", "--monitor", "0", "--mode", "SDR", "--dogegen-server", "127.0.0.1:9",
             "--legacy-meter", "--run", str(run)]
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["launch"]["argv"] == first and manifest["launch"]["cwd"] == os.getcwd()
    assert manifest["resumes"] == []
    pointer = _pointer()
    assert Path(pointer["run"]) == run.resolve()
    assert pointer["state"] == "paused_seam" and pointer["seam"] == "preflight:patch-window"
    # The paused seam on the spine is decidable without the stdout.
    request = json.loads(out[out.index('{\n  "status": "adjudication_required"'):])["request"]
    seam = [e for e in read_events(run / "events.jsonl")
            if e.event == Ev.SEAM and e.data.get("status") == "paused"][-1]
    assert seam.data["key"] == request["key"] and seam.data["digest"] == request["digest"]
    assert seam.data["question"] == request["question"] and seam.data["options"] == request["options"]

    resume = ["--decide", "preflight:patch-window=abort"]
    rc, _out, _ = _run_main(tmp_path, monkeypatch, capsys, tech="WOLED", window=full_field, extra=resume)
    assert rc == 1                                     # the judge aborted
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["launch"]["argv"] == first         # the first launch is kept…
    assert [r["argv"] for r in manifest["resumes"]] == [first + resume]   # …and the resume appended
    pointer = _pointer()
    assert pointer["state"] == "failed" and pointer["result_status"] == "aborted"
    assert pointer["aborted_at"] == "preflight"


# ---------------------------------------------------------------------------
# 3. patch_read carries ΔE; check-ins report a real max ΔE
# ---------------------------------------------------------------------------

def _scorable_reads(events: list) -> list:
    """Good, lit patch_read events after the first header that names a target. (A read of exactly
    0 cd/m² — the synthetic panel's black — has no chromaticity to score: it carries no dE, as on
    the dashboard, rather than a made-up one.)"""
    out, known = [], False
    for e in events:
        if e.event == Ev.RUN_HEADER and e.data.get("target"):
            known = True
        elif (e.event == Ev.PATCH_READ and known and e.data.get("ok") and e.data.get("xy")
              and (e.data.get("Y") or 0) > 0):
            out.append(e)
    return out


def test_sdr_patch_reads_carry_de2000_and_checkins_report_max_de(tmp_path: Path):
    calib = _make(tmp_path, "de_sdr", checkin_interval_s=0.0)   # 0 ⇒ check-ins at progress milestones
    result = calib.run("full")
    assert result.status == "completed", result.digest
    events = read_events(calib.ctx.events_path)
    reads = _scorable_reads(events)
    assert len(reads) > 50
    assert all(e.data.get("dE_metric") == "CIEDE2000" for e in reads)
    assert all(isinstance(e.data.get("dE"), float) and e.data["dE"] >= 0 for e in reads)
    assert {e.data.get("role") for e in reads} >= {"measurement", "probe"}   # loop + build probes

    # Same number the dashboard's live ΔE computes from the header it has seen at that point.
    st = DashboardState()
    for e in events:
        if e.event == Ev.PATCH_READ and "dE" in e.data:
            assert st._patch_delta_e(e.data) == pytest.approx(e.data["dE"], abs=1e-3)
        st.ingest(e)

    checkins = [e for e in events if e.event == Ev.CHECK_IN]
    assert checkins
    scored = [c.data["evidence"] for c in checkins if c.data["evidence"].get("max_dE") is not None]
    assert scored, "no check-in carried a max ΔE"
    for ev in scored:
        assert ev["dE_metric"] == "CIEDE2000" and ev["scored_reads"] > 0
        worst = ev["max_dE_read"]
        assert worst["label"] == ev["max_dE_patch"] and worst["role"] not in ("warmup", "probe")
        assert worst["rgb"] and worst["signal"] and worst["phase"]
    # the build's probe reads are summarised apart (a drive is not a target colour)
    assert any((c.data["evidence"].get("probe") or {}).get("reads", 0) > 0 for c in checkins)


def test_hdr_patch_reads_carry_de_itp(tmp_path: Path):
    calib = _make(tmp_path, "de_hdr", mode="HDR", panel=_perfect_hdr_panel(), bit_depth=10,
                  checkin_interval_s=0.0)
    result = calib.run("mhc-only")
    assert result.status == "completed", result.digest
    events = read_events(calib.ctx.events_path)
    reads = _scorable_reads(events)
    assert reads and all(e.data.get("dE_metric") == "dE_ITP" for e in reads)
    st = DashboardState()
    for e in events:
        if e.event == Ev.PATCH_READ and "dE" in e.data:
            assert st._patch_delta_e(e.data) == pytest.approx(e.data["dE"], abs=1e-3)
        st.ingest(e)
    maxes = [c.data["evidence"]["max_dE"] for c in events
             if c.event == Ev.CHECK_IN and c.data["evidence"].get("max_dE") is not None]
    assert maxes and all(m >= 0 for m in maxes)
    assert all(c.data["evidence"]["dE_metric"] == "dE_ITP" for c in events
               if c.event == Ev.CHECK_IN and c.data["evidence"].get("max_dE") is not None)


def test_patch_read_scorer_needs_a_target_and_a_usable_read():
    assert patch_read_scorer({"mode": "SDR"}) is None                         # target unknown
    assert patch_read_scorer({"target": "srgb_g22", "is_hdr": False}) is None  # SDR without white nits
    score = patch_read_scorer({"target": "srgb_g22", "is_hdr": False, "luminance": 120.0,
                               "gamma": 2.2, "white": {"xy": [0.3127, 0.3290]}})
    on_target = {"ok": True, "signal": [1.0, 1.0, 1.0], "Y": 120.0, "xy": [0.3127, 0.3290]}
    assert score(on_target)["dE"] == pytest.approx(0.0, abs=0.01)
    assert score(on_target)["dE_metric"] == "CIEDE2000"
    assert score({**on_target, "Y": 100.0})["dE"] > 1.0
    assert score({**on_target, "ok": False}) is None
    assert score({**on_target, "xy": None}) is None
    assert score({"ok": True, "Y": 1.0, "xy": [0.3, 0.3]}) is None         # no signal
    hdr = patch_read_scorer({"target": "rec2020_pq", "is_hdr": True})
    assert hdr({**on_target, "signal": [0.5, 0.5, 0.5], "Y": 92.25})["dE_metric"] == "dE_ITP"


def test_a_failing_scorer_never_breaks_a_read(tmp_path: Path):
    from dlc.events import RunLog

    log = RunLog(tmp_path / "events.jsonl")
    log.patch_scorer = lambda data: 1 / 0
    log.patch_read("measure", seq=0, ok=True, xy=[0.3, 0.3], Y=1.0, signal=[0.1, 0.1, 0.1])
    (ev,) = read_events(tmp_path / "events.jsonl")
    assert ev.event == Ev.PATCH_READ and "dE" not in ev.data


def test_window_evidence_reports_the_worst_target_read(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    w = EventWriter(path)

    def read(role: str, label: str, de: Any, **extra: Any) -> None:
        w.write("INFO", "measure", "patch_read", tier="stream", phase="measure:raw", role=role,
                label=label, rgb=[1, 2, 3], signal=[0.1, 0.2, 0.3], Y=5.0, xy=[0.3, 0.3], ok=True,
                **({"dE": de, "dE_metric": "CIEDE2000"} if de is not None else {}), **extra)

    read("warmup", "warmup", 40.0)          # settling reference: never the max
    read("measurement", "p001", 1.5)
    read("measurement", "p002", 3.25)
    read("neutral_ref", "neutral", 0.4)
    read("measurement", "p003", float("nan"))   # non-finite: ignored
    read("probe", "probe0001", 25.0, disposition="probe")
    read("measurement", "p004", None)       # target not known yet: no dE
    ev = window_evidence(path, 0)
    assert ev["reads"] == 7 and ev["scored_reads"] == 3
    assert ev["max_dE"] == 3.25 and ev["max_dE_patch"] == "p002" and ev["dE_metric"] == "CIEDE2000"
    assert ev["max_dE_read"]["role"] == "measurement" and ev["max_dE_read"]["phase"] == "measure:raw"
    assert ev["max_dE_read"]["rgb"] == [1, 2, 3]
    assert ev["probe"] == {"reads": 1, "max_dE": 25.0, "max_dE_patch": "probe0001"}
    # a window with no scored reads keeps the old shape (max_dE None, no extra keys)
    empty = tmp_path / "empty.jsonl"
    EventWriter(empty).write("INFO", "measure", "patch_read", tier="stream", role="measurement", ok=True)
    assert window_evidence(empty, 0) == {"reads": 1, "max_dE": None, "max_dE_patch": None, "warnings": []}


# ---------------------------------------------------------------------------
# 4. atomic control.json writes from the dashboard
# ---------------------------------------------------------------------------

def test_dashboard_control_writes_are_atomic(tmp_path: Path, monkeypatch):
    import dlc.dashboard.server as server
    from test_dashboard_server import _csrf_token, _hub_with_events, _post, _serve

    calls: list[Path] = []
    real = server.atomic_write_text
    monkeypatch.setattr(server, "atomic_write_text",
                        lambda p, text, **kw: (calls.append(Path(p)), real(p, text, **kw))[1])
    hub = _hub_with_events(tmp_path)
    httpd, base = _serve(hub)
    ctrl = tmp_path / "control.json"
    try:
        token = _csrf_token(base)
        for route in ("/api/pause", "/api/resume", "/api/cancel"):
            status, body = _post(base + route, token=token)
            assert status == 200 and json.loads(body)["ok"] is True
        assert calls == [ctrl] * 3
        assert json.loads(ctrl.read_text(encoding="utf-8"))["action"] == "cancel"

        # A write that dies before the swap leaves the previous COMPLETE file, never a torn one,
        # and no temp file behind.
        def boom(*_a, **_k):
            raise OSError("disk went away")
        monkeypatch.setattr(os, "replace", boom)
        status, body = _post(base + "/api/pause", token=token)
        assert status == 200 and json.loads(body)["ok"] is False
        assert json.loads(ctrl.read_text(encoding="utf-8"))["action"] == "cancel"
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 5. runs/active.json: who, what, and which state
# ---------------------------------------------------------------------------

def test_active_pointer_running_then_done_with_identity(tmp_path: Path):
    calib = _make(tmp_path, "ap_done")
    seen: list[dict[str, Any]] = []
    flow = calib._run_flow

    def spy(name: str):
        seen.append(_pointer())
        return flow(name)

    calib._run_flow = spy   # type: ignore[method-assign]
    result = calib.run("full")
    assert result.status == "completed"
    running = seen[0]
    assert running["state"] == "running" and running["pid"] == os.getpid()
    assert Path(running["run"]) == calib.ctx.root and Path(running["events"]) == calib.ctx.events_path
    assert running["display"] == calib.display.name and running["monitor"] == 0
    assert running["mode"] == "SDR" and running["flow"] == "full"
    assert running["started"] and running["run_created"] == calib.ctx.manifest.created
    done = _pointer()
    assert done["state"] == "done" and done["result_status"] == "completed"
    assert done["started"] == running["started"]
    # the profile has no recorded EDID; the preflight's live enumeration supplies it
    assert done["hardware_id"] == "SIM0000" and done["hardware_id_source"] == "preflight"


def test_active_pointer_prefers_the_profiles_hardware_id(tmp_path: Path):
    import dataclasses

    calib = _make(tmp_path, "ap_hw")
    calib.display = dataclasses.replace(calib.display, quirks={**calib.display.quirks,
                                                               "hardware_id": "SIM0000"})
    calib.run("build-correction")
    pointer = _pointer()
    assert pointer["hardware_id"] == "SIM0000" and pointer["hardware_id_source"] == "profile"


def test_active_pointer_paused_seam_then_resumed_to_done(tmp_path: Path):
    calib = _make(tmp_path, "ap_pause", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired):
        calib.run("build-correction")
    pointer = _pointer()
    assert pointer["state"] == "paused_seam" and pointer["seam"] == "probe-match:build"
    resumed = _make(tmp_path, "ap_pause",
                    adjudicator=MappingAdjudicator({"probe-match:build": Decision("skip")}))
    assert resumed.run("build-correction").status == "completed"
    pointer = _pointer()
    assert pointer["state"] == "done" and "seam" not in pointer


def test_active_pointer_cancelled_by_control_json(tmp_path: Path):
    calib = _make(tmp_path, "ap_cancel")
    (calib.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")
    result = calib.run("full")
    assert result.status == "aborted" and result.digest.get("cancelled") is True
    assert _pointer()["state"] == "cancelled"


def test_active_pointer_failed_on_a_seam_abort_and_on_an_exception(tmp_path: Path):
    calib = _make(tmp_path, "ap_abort",
                  adjudicator=MappingAdjudicator({"probe-match:build": Decision("abort")}))
    result = calib.run("build-correction")
    assert result.status == "aborted" and not result.digest.get("cancelled")
    pointer = _pointer()
    assert pointer["state"] == "failed" and pointer["aborted_at"] == "probe-match"

    crash = _make(tmp_path, "ap_crash")

    def explode(_flow: str):
        raise RuntimeError("synthetic crash")

    crash._run_flow = explode   # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        crash.run("full")
    pointer = _pointer()
    assert Path(pointer["run"]) == crash.ctx.root
    assert pointer["state"] == "failed" and "synthetic crash" in pointer["error"]


def test_a_finishing_run_never_clobbers_a_newer_runs_pointer(tmp_path: Path):
    calib = _make(tmp_path, "ap_old")
    newer = (tmp_path / "newer").resolve()
    flow = calib._run_flow

    def takeover(name: str):
        out = flow(name)
        publish_active_pointer({"run": str(newer), "state": "running", "pid": 1})
        return out

    calib._run_flow = takeover   # type: ignore[method-assign]
    assert calib.run("full").status == "completed"
    assert _pointer() == {"run": str(newer), "state": "running", "pid": 1}
    assert update_active_pointer(calib.ctx.root, state="done") is False


def test_active_pointer_failures_never_touch_the_run(tmp_path: Path, monkeypatch):
    import dlc.calibrate as calibrate

    def broken(*_a, **_k):
        raise OSError("runs root is read-only")

    monkeypatch.setattr(calibrate, "publish_active_pointer", broken)
    monkeypatch.setattr(calibrate, "update_active_pointer", broken)
    calib = _make(tmp_path, "ap_broken")
    assert calib.run("full").status == "completed"
    paused = _make(tmp_path, "ap_broken_pause", adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as exc:   # the pause propagates unchanged
        paused.run("build-correction")
    assert exc.value.request.key == "probe-match:build"
