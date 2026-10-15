"""The live state publisher (live/state.json + live/charts.json) that DesktopLUT's DLC tab reads."""

from __future__ import annotations

import datetime as _dt
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dlc import calibration_profile as cp
from dlc.adjudication import AdjudicationRequired, AutoAdjudicator, MappingAdjudicator
from dlc.calibrate import Calibration, PatchSizes
from dlc.controller import CalibrationController
from dlc.dashboard.publish import (
    CHARTS_FILE, FEED_MAX, LIVE_SCHEMA, STATE_FILE, LivePublisher, feed_entry, live_dir, publish_once,
)
from dlc.engine.patches import Transfer
from dlc.events import EventWriter
from dlc.measure_loop import SyntheticPanel
from dlc.note import post_note
from dlc.optimize import OptimizeConfig
from dlc.runs import create_run

T0 = datetime(2026, 10, 15, 21, 0, 0)


def _strict_load(path: Path) -> dict:
    """Parse like a strict (C++) JSON reader would: NaN / Infinity are errors."""
    def _bad(token):
        raise ValueError(f"non-finite JSON token {token}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_bad)


def _spine(run: Path) -> EventWriter:
    run.mkdir(parents=True, exist_ok=True)
    w = EventWriter(run / "events.jsonl")
    w.write("INFO", "run", "run_header", run_id="r1", target="srgb_g22", mode="SDR", flow="full",
            stage_plan=[{"key": "measure", "label": "Raw"}, {"key": "verify", "label": "Verify"}])
    w.write("INFO", "measure", "stage_start")
    w.write("INFO", "measure", "patch_read", tier="stream", seq=0, role="measurement", label="W",
            rgb=[1.0, 1.0, 1.0], signal=[1.0, 1.0, 1.0], Y=120.0, xy=[0.3127, 0.329], ok=True)
    return w


def test_publish_once_writes_both_files_strict_json(tmp_path: Path):
    run = tmp_path / "r1"
    w = _spine(run)
    w.write("WARN", "measure", "anomaly", kind="drift", message="blue drifted 0.4 %")
    w.write("INFO", "measure", "seam", key="brightness:adjust", status="paused",
            question="white 152 nits", options=["accept", "abort"])
    assert publish_once(run, T0)

    state = _strict_load(live_dir(run) / STATE_FILE)
    charts = _strict_load(live_dir(run) / CHARTS_FILE)
    assert state["live_schema"] == LIVE_SCHEMA and charts["live_schema"] == LIVE_SCHEMA
    assert charts["seq"] > state["seq"] >= 1                      # one counter across both files
    assert state["header"]["flow"] == "full"
    assert state["pipeline"]["steps"][0]["key"] == "measure"
    assert state["seam"]["key"] == "brightness:adjust"
    assert state["liveness"]["light"] == "paused"
    assert state["run_dir"] == str(run)
    # The feed is the digest tier only (no patch_read firehose), with renderer-ready wording.
    events = [r["event"] for r in state["feed"]]
    assert "patch_read" not in events
    assert {"run_header", "stage_start", "anomaly", "seam"} <= set(events)
    seam_row = next(r for r in state["feed"] if r["event"] == "seam")
    assert seam_row["title"] == "brightness:adjust" and seam_row["text"] == "paused"
    assert charts["cie"]["points"] and "grayscale" in charts and "eotf" in charts


def test_nan_is_written_as_null(tmp_path: Path):
    run = tmp_path / "r1"
    w = _spine(run)
    w.write("INFO", "verify", "metrics_scored", avg_de2000=float("nan"), p95_de2000=0.7,
            max_de2000=float("inf"))
    assert publish_once(run, T0)
    state = _strict_load(live_dir(run) / STATE_FILE)   # would raise on a bare NaN
    row = next(r for r in state["feed"] if r["event"] == "metrics_scored")
    assert row["text"] == "p95 0.70"


def test_pump_throttles_and_ticks(tmp_path: Path):
    run = tmp_path / "r1"
    w = _spine(run)
    pub = LivePublisher(run, state_interval=0.5, tick_interval=2.0, charts_interval=3.0)
    assert pub.pump(T0)                                        # first pump writes both
    seq0 = _strict_load(live_dir(run) / STATE_FILE)["seq"]
    assert not pub.pump(T0 + timedelta(seconds=0.1))           # nothing new, not due
    w.write("INFO", "measure", "patch_read", tier="stream", seq=1, role="measurement", label="G50",
            rgb=[0.5, 0.5, 0.5], signal=[0.5, 0.5, 0.5], Y=26.0, xy=[0.3127, 0.329], ok=True)
    assert not pub.pump(T0 + timedelta(seconds=0.2))           # dirty but inside state_interval
    assert pub.pump(T0 + timedelta(seconds=0.6))               # dirty + due
    assert _strict_load(live_dir(run) / STATE_FILE)["seq"] > seq0
    # Liveness ages advance with no events: a tick rewrites state even when nothing changed.
    seq1 = _strict_load(live_dir(run) / STATE_FILE)["seq"]
    assert pub.pump(T0 + timedelta(seconds=3.0))
    assert _strict_load(live_dir(run) / STATE_FILE)["seq"] > seq1


def test_charts_follow_stage_changes_immediately(tmp_path: Path):
    run = tmp_path / "r1"
    w = _spine(run)
    pub = LivePublisher(run, charts_interval=60.0)
    pub.pump(T0)
    first = _strict_load(live_dir(run) / CHARTS_FILE)["seq"]
    w.write("INFO", "verify", "stage_start")
    w.write("INFO", "verify", "patch_read", tier="stream", seq=2, role="verify", label="W",
            rgb=[1.0, 1.0, 1.0], signal=[1.0, 1.0, 1.0], Y=119.0, xy=[0.3128, 0.3291], ok=True)
    pub.pump(T0 + timedelta(seconds=1))                         # stage moved: no 60 s wait
    assert _strict_load(live_dir(run) / CHARTS_FILE)["seq"] > first


def test_publisher_never_raises_and_logs_once(tmp_path: Path):
    run = tmp_path / "r1"
    _spine(run)
    (run / "live").write_text("a file where the folder should be", encoding="utf-8")
    logged: list[str] = []
    pub = LivePublisher(run, log=logged.append)
    assert pub.pump(T0) is False
    assert pub.pump(T0 + timedelta(seconds=5)) is False
    assert pub.flush(T0 + timedelta(seconds=6)) is False
    assert len(logged) == 1 and "the run is unaffected" in logged[0]


def test_notes_merge_into_feed_by_time(tmp_path: Path):
    run = tmp_path / "r1"
    _spine(run)
    post_note(run, "red max is the panel floor at 75 %;\n no action", stage="measure")
    assert publish_once(run, T0)
    feed = _strict_load(live_dir(run) / STATE_FILE)["feed"]
    note = next(r for r in feed if r["event"] == "supervisor_note")
    assert note["title"] == "claude"
    assert note["text"] == "red max is the panel floor at 75 %; no action"   # one line
    assert len(feed) <= FEED_MAX


def test_feed_entry_check_in_wording():
    row = feed_entry({"event": "check_in", "stage": "measure", "time": "t", "level": "INFO",
                      "data": {"seq": 3, "evidence": {"max_dE": 2.104, "max_dE_patch": "R75",
                                                      "warnings": [{"event": "anomaly"},
                                                                   {"truncated": 4}]}}})
    assert row["title"] == "check-in #3"
    assert row["text"] == "max 2.10 @ R75 · 5 warnings"
    quartile = feed_entry({"event": "check_in", "stage": "measure", "data": {
        "progress": 0.5, "patches_done": 50, "patches_total": 100}})
    assert quartile["title"] == "check-in 50%" and quartile["text"] == "50/100"


# ---------------------------------------------------------------------------
# Wiring: a live (opt-in) run writes the files; a seam pause leaves the paused state on disk.
# ---------------------------------------------------------------------------

_SMALL = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
_OPT = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)


def _calib(tmp_path: Path, adjudicator, *, live: bool) -> Calibration:
    ctx = create_run("SDR", display="synthetic", run_dir=tmp_path / "run")
    panel = SyntheticPanel(transfer=Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10),
                           start_temp=1.0, cold_blue_gain=1.0)
    return Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")),
                       monitor=0, mode="SDR", controller=CalibrationController.mock(), measure=panel,
                       adjudicator=adjudicator, optimize_config=_OPT, patch_sizes=_SMALL,
                       run_date=_dt.date(2026, 10, 15), enable_live_state=live)


def test_live_run_publishes_final_state(tmp_path: Path):
    calib = _calib(tmp_path, AutoAdjudicator(), live=True)
    calib.run("mhc-only")
    state = _strict_load(live_dir(calib.ctx.root) / STATE_FILE)
    assert state["liveness"]["light"] == "done"
    assert any(r["event"] == "run_done" for r in state["feed"])
    assert (live_dir(calib.ctx.root) / CHARTS_FILE).exists()
    assert calib._live_publisher is None                       # stopped + flushed


def test_seam_pause_leaves_paused_state(tmp_path: Path):
    calib = _calib(tmp_path, MappingAdjudicator(), live=True)
    with pytest.raises(AdjudicationRequired):
        calib.run("mhc-only")
    state = _strict_load(live_dir(calib.ctx.root) / STATE_FILE)
    assert state["seam"].get("status") == "paused"
    assert state["liveness"]["light"] == "paused"


def test_live_state_is_opt_in(tmp_path: Path):
    calib = _calib(tmp_path, AutoAdjudicator(), live=False)
    calib.run("mhc-only")
    assert not live_dir(calib.ctx.root).exists()
