"""The dashboard's server-side brain: folding the spine into renderable state."""

from __future__ import annotations

from datetime import datetime, timedelta

from dlc.events import Ev, Event
from dlc.dashboard.state import DashboardState

T0 = datetime(2026, 6, 18, 12, 0, 0)


def _ev(name, *, t, level="INFO", stage="", phase=None, tier=None, **data):
    return Event(level=level, stage=stage, event=name, data=data,
                 time=t.isoformat(timespec="milliseconds"), tier=tier, phase=phase)


def test_header_drives_status_bar_and_marks_running():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run",
                  run_id="r1", display="PA32", monitor=0, mode="SDR",
                  flow="full", bit_depth=10, target="srgb_g22",
                  white={"xy": [0.3127, 0.329], "cct": 6504}, ccmx="probe.ccmx",
                  schema_version=1))
    snap = st.snapshot(T0)
    assert snap["run_status"] == "running"
    assert snap["header"]["target"] == "srgb_g22"
    assert snap["header"]["ccmx"] == "probe.ccmx"
    assert snap["run_id"] == "r1"
    assert snap["schema_version"] == 1


def test_phase_stage_and_progress_counters():
    st = DashboardState()
    st.ingest(_ev(Ev.PHASE, t=T0, stage="run", phase="measure", phase_name="measure"))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure", phase="measure"))
    st.ingest(_ev(Ev.PROGRESS, t=T0, stage="measure", phase="measure",
                  patches_done=4, patches_total=33, reads=9))
    snap = st.snapshot(T0)
    assert snap["phase"] == "measure"
    assert snap["stage"] == "measure"
    # patches_done/total come from PROGRESS (per-stage, drives the bar)
    assert snap["counters"]["patches_done"] == 4
    assert snap["counters"]["patches_total"] == 33
    # the read counter is cumulative from patch_read events (consistent with ok/fail),
    # NOT the per-stage PROGRESS.reads field, so it's 0 here with no reads ingested
    assert snap["counters"]["reads"] == 0


def test_patch_read_enriches_cct_and_tracks_white():
    st = DashboardState()
    # A neutral, good read near D65 → drives the live white point + per-row derived CCT.
    wire = st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure", tier="stream",
                         seq=1, role="measurement", label="w", rgb=[255, 255, 255],
                         Y=120.0, xy=[0.3127, 0.329], ok=True, disposition="accepted"))
    assert "derived" in wire and abs(wire["derived"]["cct"] - 6504) < 60
    snap = st.snapshot(T0)
    assert abs(snap["last_white"]["cct"] - 6504) < 60
    assert snap["counters"]["reads_ok"] == 1
    assert snap["counters"]["reads_failed"] == 0


def test_failed_read_counts_and_does_not_set_white():
    st = DashboardState()
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure", tier="stream",
                  seq=0, role="measurement", rgb=[255, 255, 255], ok=False))
    snap = st.snapshot(T0)
    assert snap["counters"]["reads_failed"] == 1
    assert snap["last_white"] == {}


def test_non_neutral_read_does_not_move_white():
    st = DashboardState()
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure", tier="stream",
                  seq=0, role="measurement", rgb=[255, 0, 0], Y=40.0,
                  xy=[0.64, 0.33], ok=True))
    assert st.snapshot(T0)["last_white"] == {}


def test_metrics_scored_feeds_the_de_bignumbers():
    st = DashboardState()
    st.ingest(_ev("metrics_scored", t=T0, stage="verify", label="verification",
                  iteration=0, avg_de2000=0.31, p95_de2000=0.9, p99_de2000=1.2,
                  max_de2000=1.4, white_de2000=0.5,
                  grayscale_avg_de2000=0.4, colour_avg_de2000=0.25))
    de = st.snapshot(T0)["de"]
    assert de["avg"] == 0.31 and de["p95"] == 0.9 and de["p99"] == 1.2
    assert de["max"] == 1.4 and de["white"] == 0.5
    # the grayscale-vs-colour split rides the same event (single-sourced from the spine)
    assert de["grayscale"] == 0.4 and de["colour"] == 0.25
    assert de["phase"] == "verification"


def test_eta_from_progress_rate():
    st = DashboardState()
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure"))   # puts the run in 'running' (ETA only shows then)
    st.ingest(_ev(Ev.PROGRESS, t=T0, stage="measure", patches_done=0, patches_total=10, reads=0))
    st.ingest(_ev(Ev.PROGRESS, t=T0 + timedelta(seconds=20), stage="measure",
                  patches_done=4, patches_total=10, reads=8))
    snap = st.snapshot(T0 + timedelta(seconds=20))
    # 4 patches in 20 s ⇒ 5 s/patch; 6 remaining ⇒ ~30 s ETA.
    assert abs(snap["timers"]["s_per_patch"] - 5.0) < 0.6
    assert abs(snap["timers"]["eta_s"] - 30.0) < 4.0


def test_liveness_light_live_then_stalled_then_done():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", run_id="r"))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure", tier="stream",
                  seq=0, rgb=[128, 128, 128], xy=[0.31, 0.33], Y=10.0, ok=True))
    # Fresh: green.
    assert st.snapshot(T0 + timedelta(seconds=5))["liveness"]["light"] == "live"
    # Long silence with no heartbeat: red.
    assert st.snapshot(T0 + timedelta(seconds=600))["liveness"]["light"] == "stalled"
    # A terminal run_done flips it to a neutral 'done' regardless of age.
    st.ingest(_ev(Ev.RUN_DONE, t=T0 + timedelta(seconds=601), stage="run", status="completed"))
    assert st.snapshot(T0 + timedelta(seconds=999))["liveness"]["light"] == "done"


def test_stall_event_sets_status_and_flag():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.STALL, t=T0, stage="build-install-3dlut", level="ERROR",
                  message="no progress", via="checkpoint"))
    snap = st.snapshot(T0)
    assert snap["run_status"] == "stalled"
    assert snap["stall"]["via"] == "checkpoint"
    assert snap["liveness"]["light"] == "stalled"


def test_run_done_freezes_the_run_clock():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.RUN_DONE, t=T0 + timedelta(seconds=120), stage="run", status="completed"))
    # Querying much later must not keep advancing run_elapsed past the end.
    snap = st.snapshot(T0 + timedelta(seconds=9999))
    assert abs(snap["timers"]["run_elapsed_s"] - 120.0) < 1.0
