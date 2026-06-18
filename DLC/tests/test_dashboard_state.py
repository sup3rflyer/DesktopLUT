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


def test_paused_seam_shows_paused_not_stalled():
    """A healthy human-in-the-loop pause: the process exits to await a decision, so
    heartbeats STOP and event-age grows — but it must read 'paused' (calm), never
    'stalled' (red). This is the highest-value false-red fix from the audit."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="verify"))
    st.ingest(_ev(Ev.SEAM, t=T0, stage="verify", key="verify:accept", status="paused"))
    # Minutes later, with no further events, it stays paused — not red.
    assert st.snapshot(T0 + timedelta(seconds=900))["liveness"]["light"] == "paused"
    # The resuming run makes progress → the pause clears and it goes live again.
    st.ingest(_ev(Ev.PATCH_READ, t=T0 + timedelta(seconds=905), stage="verify", tier="stream",
                  seq=0, rgb=[128, 128, 128], xy=[0.31, 0.33], Y=10.0, ok=True))
    assert st.snapshot(T0 + timedelta(seconds=906))["liveness"]["light"] == "live"


def test_alive_but_wedged_goes_amber_then_red_while_heartbeats_continue():
    """The 53-min failure shape: the process is alive (heartbeats keep coming) but makes
    NO progress. Event-age stays fresh, yet the light must warn — progress-age is the
    signal. With the producer's stall threshold on the heartbeat, amber crosses at half
    the threshold and red at the threshold."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="build-install-3dlut"))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="build-install-3dlut", tier="stream",
                  seq=0, rgb=[128, 128, 128], xy=[0.31, 0.33], Y=10.0, ok=True))
    # Heartbeats keep arriving (process alive) but since_progress climbs; threshold 600 s.
    def beat(at, since):
        st.ingest(_ev(Ev.HEARTBEAT, t=T0 + timedelta(seconds=at), stage="build-install-3dlut",
                      level="DEBUG", tier="stream", since_progress_s=float(since),
                      stall_after_s=600.0, elapsed_s=float(at)))
    beat(60, 60)
    assert st.snapshot(T0 + timedelta(seconds=60))["liveness"]["light"] == "live"      # fresh progress
    beat(360, 360)   # past half the 600 s threshold → early wedge warning
    assert st.snapshot(T0 + timedelta(seconds=360))["liveness"]["light"] == "slow"
    beat(620, 620)   # past the threshold → red (the guard should be firing about now)
    assert st.snapshot(T0 + timedelta(seconds=620))["liveness"]["light"] == "stalled"


def test_soak_with_heartbeats_stays_live_even_without_patch_reads():
    """A healthy silent soak: no patch_read/progress events, but heartbeats carry a SMALL
    since_progress (soak blocks reset the producer's clock). Must stay green — not a false
    amber/red just because the patch firehose went quiet."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure:raw"))
    # 10 minutes of soak: only heartbeats, each with a tiny since_progress.
    for at in range(15, 600, 15):
        st.ingest(_ev(Ev.HEARTBEAT, t=T0 + timedelta(seconds=at), stage="measure:raw",
                      level="DEBUG", tier="stream", since_progress_s=5.0,
                      stall_after_s=600.0, elapsed_s=float(at)))
    assert st.snapshot(T0 + timedelta(seconds=600))["liveness"]["light"] == "live"


def test_stall_unlatches_on_fresh_progress():
    """If a stall fired but the run kept going (guard self-recovered within the stage),
    a subsequent good read clears the latched red."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure"))
    st.ingest(_ev(Ev.STALL, t=T0 + timedelta(seconds=1), stage="measure", level="ERROR",
                  message="no progress", via="checkpoint"))
    assert st.snapshot(T0 + timedelta(seconds=1))["run_status"] == "stalled"
    st.ingest(_ev(Ev.PATCH_READ, t=T0 + timedelta(seconds=2), stage="measure", tier="stream",
                  seq=0, rgb=[128, 128, 128], xy=[0.31, 0.33], Y=10.0, ok=True))
    assert st.snapshot(T0 + timedelta(seconds=2))["run_status"] == "running"


def test_eta_window_resets_at_stage_boundary():
    """ETA/s-per-patch must not be computed across a stage reset (patches_done restarts at
    ~0), which otherwise yields a wildly wrong number."""
    st = DashboardState()
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="stage_a"))
    st.ingest(_ev(Ev.PROGRESS, t=T0, stage="stage_a", patches_done=0, patches_total=50))
    st.ingest(_ev(Ev.PROGRESS, t=T0 + timedelta(seconds=80), stage="stage_a",
                  patches_done=40, patches_total=50))   # 2 s/patch
    # New stage: counters restart. The old marks must be discarded.
    st.ingest(_ev(Ev.STAGE_START, t=T0 + timedelta(seconds=90), stage="stage_b"))
    st.ingest(_ev(Ev.PROGRESS, t=T0 + timedelta(seconds=90), stage="stage_b",
                  patches_done=0, patches_total=10))
    st.ingest(_ev(Ev.PROGRESS, t=T0 + timedelta(seconds=110), stage="stage_b",
                  patches_done=4, patches_total=10))    # 5 s/patch, 6 remaining ⇒ ~30 s
    snap = st.snapshot(T0 + timedelta(seconds=110))
    assert abs(snap["timers"]["s_per_patch"] - 5.0) < 0.6   # NOT contaminated by stage_a
    assert abs(snap["timers"]["eta_s"] - 30.0) < 5.0


def test_tz_aware_timestamp_does_not_crash_snapshot():
    """A tz-aware producer timestamp must not raise (it would be swallowed and silently
    freeze the dashboard) — it's normalised to naive."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(Event(level="INFO", stage="measure", event=Ev.STAGE_START, data={},
                    time="2026-06-18T12:00:30+00:00"))
    snap = st.snapshot(T0 + timedelta(seconds=60))   # must not raise
    assert snap["run_status"] in ("running", "idle")


def test_malformed_progress_event_does_not_crash():
    """A PROGRESS with null counters (a drifted/partial producer) must not raise."""
    st = DashboardState()
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure"))
    st.ingest(_ev(Ev.PROGRESS, t=T0, stage="measure",
                  patches_done=None, patches_total="oops"))
    snap = st.snapshot(T0)   # must not raise
    assert snap["counters"]["patches_done"] == 0


# ---------------------------------------------------------------------------
# chart accumulators (Phase 5)
# ---------------------------------------------------------------------------

def _read(st, at, rgb, xy, Y, *, signal=None, role="measurement", phase=None, disposition=None):
    st.ingest(_ev(Ev.PATCH_READ, t=T0 + timedelta(seconds=at), stage="measure", tier="stream",
                  phase=phase, seq=at, role=role, rgb=rgb, signal=signal, xy=xy, Y=Y, ok=True,
                  disposition=disposition))


def test_charts_accumulate_cie_grayscale_and_drift():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", gamma=2.2, luminance=120.0,
                  white={"xy": [0.3127, 0.329], "cct": 6504}))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure"))
    # a couple of neutral (grayscale) reads + one colour read
    _read(st, 1, [128, 128, 128], [0.312, 0.329], 40.0, signal=[0.5, 0.5, 0.5])
    _read(st, 2, [255, 255, 255], [0.313, 0.329], 120.0, signal=[1.0, 1.0, 1.0])
    _read(st, 3, [255, 0, 0], [0.64, 0.33], 45.0, signal=[1.0, 0.0, 0.0])
    ch = st.charts()
    assert len(ch["cie"]["points"]) == 3            # every good read scatters
    assert ch["cie"]["white"] == [0.3127, 0.329]
    assert len(ch["cie"]["locus"]) == 31
    # grayscale keyed by signal level (neutrals only) → 2 steps, sorted
    assert [g["signal"] for g in ch["grayscale"]] == [0.5, 1.0]
    assert ch["eotf"]["gamma"] == 2.2 and len(ch["eotf"]["points"]) == 2
    # drift/white track only follows neutral reads
    assert len(ch["white_track"]) == 2
    assert all(w["elapsed_s"] is not None for w in ch["white_track"])


def test_grayscale_latest_measurement_wins_per_level():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", white={"xy": [0.3127, 0.329]}))
    _read(st, 1, [128, 128, 128], [0.30, 0.34], 40.0, signal=[0.5, 0.5, 0.5])
    _read(st, 9, [128, 128, 128], [0.313, 0.329], 41.0, signal=[0.5, 0.5, 0.5])  # re-measure
    gray = st.charts()["grayscale"]
    assert len(gray) == 1
    assert gray[0]["x"] == 0.313     # the later read overwrote the earlier one


def test_color_luminance_error_vs_target():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", gamma=2.2,
                  white={"xy": [0.3127, 0.329], "cct": 6504}))
    # white reference (full neutral) so the chart can normalise
    _read(st, 1, [255, 255, 255], [0.313, 0.329], 100.0, signal=[1.0, 1.0, 1.0])
    # a full-red patch reading exactly its target luminance (Kr=0.2126 → 21.26 cd/m²) → 0 error
    _read(st, 2, [255, 0, 0], [0.64, 0.33], 21.26, signal=[1.0, 0.0, 0.0])
    # a full-green patch reading 10% dim (target Kg=0.7152 → 71.52; measured 64.37)
    _read(st, 3, [0, 255, 0], [0.30, 0.60], 64.37, signal=[0.0, 1.0, 0.0])
    cl = {c["label"]: c for c in st.charts()["color_lum"]}
    assert "R100" in cl and abs(cl["R100"]["error"]) < 0.01           # on target
    assert "G100" in cl and abs(cl["G100"]["error"] - (-0.10)) < 0.01  # ~10% dim
    assert cl["R100"]["color"] == "#ff0000"                            # bar coloured as the patch


def test_charts_show_latest_measurement_stage_not_all_overlaid():
    # raw (uncorrected) then verify (corrected) measurements of the same patch set: the CIE
    # scatter must show the LATEST stage's points only, not both clouds superimposed.
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", gamma=2.2, white={"xy": [0.3127, 0.329]}))
    _read(st, 1, [255, 0, 0], [0.66, 0.32], 40.0, signal=[1.0, 0.0, 0.0], phase="measure:raw")
    _read(st, 2, [128, 128, 128], [0.30, 0.34], 35.0, signal=[0.5, 0.5, 0.5], phase="measure:raw")
    _read(st, 3, [255, 0, 0], [0.64, 0.33], 45.0, signal=[1.0, 0.0, 0.0], phase="measure:verify")
    _read(st, 4, [128, 128, 128], [0.313, 0.329], 36.0, signal=[0.5, 0.5, 0.5], phase="measure:verify")
    ch = st.charts()
    assert ch["stage"] == "measure:verify"
    assert ch["stages"] == ["measure:raw", "measure:verify"]
    assert len(ch["cie"]["points"]) == 2                 # verify only, not 4 overlaid
    # grayscale + colour-luminance also reflect verify (the corrected result)
    assert [g["x"] for g in ch["grayscale"]] == [0.313]
    assert any(c["family"] == "R" for c in ch["color_lum"])


def test_warmup_and_build_probe_reads_excluded_from_charts():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", gamma=2.2, white={"xy": [0.3127, 0.329]}))
    # warm-up + a 3D-LUT build probe read must NOT pollute the snapshot charts
    _read(st, 1, [130, 128, 128], [0.31, 0.33], 30.0, signal=[0.51, 0.5, 0.5],
          role="warmup", phase="measure:post-mhc")
    _read(st, 2, [200, 50, 50], [0.55, 0.34], 25.0, signal=[0.78, 0.2, 0.2],
          role="probe", disposition="probe", phase="build-install-3dlut")
    _read(st, 3, [255, 0, 0], [0.64, 0.33], 45.0, signal=[1.0, 0.0, 0.0], phase="measure:verify")
    ch = st.charts()
    assert ch["stage"] == "measure:verify"
    assert len(ch["cie"]["points"]) == 1                 # only the real measurement read
    assert ch["stages"] == ["measure:verify"]            # warmup/probe never opened a stage


def test_drift_reference_reads_feed_white_track_not_snapshot_charts():
    # The interleaved neutral-ref drift checkpoints (mirrored from the measure loop) are the
    # cleanest white-drift signal — they belong on the drift TIME series but NOT the per-stage
    # CIE/grayscale snapshot (they re-read one fixed neutral, not the patch set).
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", gamma=2.2, white={"xy": [0.3127, 0.329]}))
    _read(st, 1, [128, 128, 128], [0.312, 0.329], 40.0, signal=[0.5, 0.5, 0.5], phase="measure:post-mhc")
    _read(st, 2, [512, 512, 520], [0.311, 0.330], 39.0, signal=[0.5, 0.5, 0.51],
          role="neutral_ref", disposition="drift_ref", phase="measure:post-mhc")
    ch = st.charts()
    assert len(ch["white_track"]) == 2                  # both feed the drift series
    assert len(ch["cie"]["points"]) == 1               # the drift-ref stays OFF the snapshot
    assert [g["signal"] for g in ch["grayscale"]] == [0.5]


def test_check_in_event_surfaces_in_snapshot():
    st = DashboardState()
    st.ingest(_ev(Ev.CHECK_IN, t=T0, stage="measure", phase="measure:post-mhc",
                  progress=0.5, patches_done=64, patches_total=128, warm=True))
    snap = st.snapshot(T0)
    assert snap["check_in"]["progress"] == 0.5
    assert snap["check_in"]["patches_total"] == 128


def test_optimizer_history_accumulates():
    st = DashboardState()
    for i in range(3):
        st.ingest(_ev(Ev.OPTIMIZER_ITER, t=T0 + timedelta(seconds=i), stage="optimize",
                      iteration=i, measured_mean_de=1.0 - 0.2 * i, measured_max_de=2.0 - 0.3 * i))
    opt = st.charts()["optimizer"]
    assert [r["iteration"] for r in opt] == [0, 1, 2]
    assert opt[-1]["measured_max_de"] == 2.0 - 0.6
