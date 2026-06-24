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


def test_native_primaries_from_raw_pure_channel_peaks():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="HDR", white={"xy": [0.3127, 0.329]}))
    for sig, xy in [([1.0, 0.0, 0.0], [0.69, 0.30]), ([0.0, 1.0, 0.0], [0.21, 0.71]),
                    ([0.0, 0.0, 1.0], [0.15, 0.05])]:
        st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0,
                      role="measurement", rgb=[int(c * 255) for c in sig], signal=sig,
                      Y=100.0, xy=xy, ok=True))
    nat = st.charts()["cie"]["native"]
    assert nat is not None
    assert abs(nat["r"][0] - 0.69) < 1e-3 and abs(nat["g"][1] - 0.71) < 1e-3 and abs(nat["b"][0] - 0.15) < 1e-3


def test_grayscale_near_black_flagged_dim_for_cct_chart():
    """A near-black neutral (0.006 nits) solves to a junk CCT (xy→noise as Y→0). The grayscale
    CCT/Duv charts must flag it ``dim`` so they drop it from the trace + autoscale, while a normal
    bright neutral stays solid. Reproduces the 11130 K @ 0.006-nit point on the live dashboard."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="HDR", white={"xy": [0.3127, 0.329]}))
    # bright neutral (above the 1-nit floor) → meaningful CCT
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:post-mhc", tier="stream", seq=0,
                  role="measurement", rgb=[512, 512, 512], signal=[0.5, 0.5, 0.5],
                  Y=92.0, xy=[0.3127, 0.329], ok=True))
    # near-black neutral (0.006 nits) with a blue-shifted noise chromaticity → must be dimmed
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:post-mhc", tier="stream", seq=0,
                  role="measurement", rgb=[7, 7, 7], signal=[0.0068, 0.0068, 0.0068],
                  Y=0.0056, xy=[0.262, 0.305], ok=True))
    gray = st.charts()["grayscale"]
    by_sig = {round(p["signal"], 4): p for p in gray}
    assert by_sig[0.5]["dim"] is False        # bright neutral: real CCT, plotted
    assert by_sig[0.0068]["dim"] is True       # near-black: noise, excluded from trace + autoscale


def test_native_primaries_none_without_saturated_patches():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="HDR", white={"xy": [0.3127, 0.329]}))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[200, 200, 200], signal=[0.78, 0.78, 0.78], Y=90.0, xy=[0.31, 0.33], ok=True))
    assert st.charts()["cie"]["native"] is None    # neutral-only raw → no native gamut


def test_native_primaries_picks_highest_luminance_green_over_near_black():
    """A green ramp where the near-black green ([0,0.035,0], Y=0.027) is ~100% saturated but
    sub-noise — its chromaticity (0.232, 0.466) is meaningless. The native-green overlay must
    come from the brightest full-drive green (mirroring channel_model().peak_xyz), NOT the
    first/dim saturated read. Reproduces run 20260623_093924_400363 (PA32UCXR HDR)."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="HDR", white={"xy": [0.3127, 0.329]}))
    # Bright red + blue primaries.
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[255, 0, 0], signal=[1.0, 0.0, 0.0], Y=100.0, xy=[0.69, 0.30], ok=True))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[0, 0, 255], signal=[0.0, 0.0, 1.0], Y=20.0, xy=[0.15, 0.05], ok=True))
    # Green ramp: the dim near-black green is ingested FIRST (would win under saturation-only),
    # then the bright full-drive green with the real primary chromaticity.
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[0, 9, 0], signal=[0.0, 0.035, 0.0], Y=0.027, xy=[0.232, 0.466], ok=True))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[0, 195, 0], signal=[0.0, 0.764, 0.0], Y=80.0, xy=[0.183, 0.749], ok=True))
    nat = st.charts()["cie"]["native"]
    assert nat is not None
    assert abs(nat["g"][0] - 0.183) < 1e-3 and abs(nat["g"][1] - 0.749) < 1e-3


def test_native_primaries_none_when_a_primary_is_only_sub_noise():
    """If a whole family is only ever seen as a sub-noise near-black read, its chromaticity is
    unreliable — hide the overlay rather than plot the native gamut at a noise point."""
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="HDR", white={"xy": [0.3127, 0.329]}))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[255, 0, 0], signal=[1.0, 0.0, 0.0], Y=100.0, xy=[0.69, 0.30], ok=True))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[0, 0, 255], signal=[0.0, 0.0, 1.0], Y=20.0, xy=[0.15, 0.05], ok=True))
    # Only a sub-noise green was ever measured.
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:raw", tier="stream", seq=0, role="measurement",
                  rgb=[0, 9, 0], signal=[0.0, 0.035, 0.0], Y=0.027, xy=[0.232, 0.466], ok=True))
    assert st.charts()["cie"]["native"] is None


def test_per_patch_de_enriches_reads_and_live_header():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="SDR", flow="full",
                  luminance=120.0, gamma=2.2, white={"xy": [0.3127, 0.329], "cct": 6504}))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure:verify"))
    # near-perfect white → tiny ΔE; the event-log wire + last_read carry it, the live header counts it
    wire = st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure:verify", tier="stream", seq=1,
                         role="measurement", label="w", rgb=[255, 255, 255], signal=[1.0, 1.0, 1.0],
                         Y=120.0, xy=[0.3127, 0.329], ok=True))
    assert wire["derived"]["de"] is not None and wire["derived"]["de"] < 1.0
    snap = st.snapshot(T0)
    assert snap["last_read"]["de"] is not None
    # SDR scores CIEDE2000 and offers Jzazbz as a view lens; the rolling summary carries both.
    assert snap["live_de"]["n"] == 1 and snap["live_de"]["scoring"] == "de2000"
    assert set(snap["live_de"]["metrics"]) == {"de2000", "jzazbz"}
    # the last patch carries the full per-metric split so the dashboard can switch view client-side
    assert snap["last_read"]["deltas"]["scoring"] == "de2000"
    assert all(k in snap["last_read"]["deltas"]["metrics"]["de2000"] for k in ("de", "L", "C", "H"))
    # a visibly-off red patch raises the rolling max above the near-zero white
    st.ingest(_ev(Ev.PATCH_READ, t=T0 + timedelta(seconds=1), stage="measure:verify", tier="stream",
                  seq=2, role="measurement", label="r", rgb=[255, 0, 0], signal=[1.0, 0.0, 0.0],
                  Y=40.0, xy=[0.60, 0.34], ok=True))
    snap2 = st.snapshot(T0 + timedelta(seconds=1))
    assert snap2["live_de"]["n"] == 2 and snap2["live_de"]["metrics"]["de2000"]["max"] > 1.0
    # a new stage clears the live ΔE window (it tracks the CURRENT stage)
    st.ingest(_ev(Ev.STAGE_START, t=T0 + timedelta(seconds=2), stage="measure:post-mhc"))
    assert st.snapshot(T0 + timedelta(seconds=2))["live_de"]["n"] == 0


def test_live_de_dedups_rereads():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="SDR", luminance=120.0, gamma=2.2,
                  white={"xy": [0.3127, 0.329]}))
    st.ingest(_ev(Ev.STAGE_START, t=T0, stage="measure:verify"))
    # the SAME neutral patch read twice (noisy → settled) must count ONCE (latest wins), not twice
    for i, (Y, xy) in enumerate([(60.0, [0.33, 0.33]), (120.0, [0.3127, 0.329])]):
        st.ingest(_ev(Ev.PATCH_READ, t=T0 + timedelta(seconds=i), stage="measure:verify", tier="stream",
                      seq=i, role="measurement", label="w", rgb=[255, 255, 255], signal=[1.0, 1.0, 1.0],
                      Y=Y, xy=xy, ok=True))
    assert st.snapshot(T0)["live_de"]["n"] == 1


def test_warmup_reads_excluded_from_live_de():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="SDR", luminance=120.0, gamma=2.2,
                  white={"xy": [0.3127, 0.329]}))
    st.ingest(_ev(Ev.PATCH_READ, t=T0, stage="measure", tier="stream", seq=0, role="warmup",
                  rgb=[255, 255, 255], signal=[1.0, 1.0, 1.0], Y=120.0, xy=[0.31, 0.33], ok=True))
    assert st.snapshot(T0)["live_de"]["n"] == 0          # warm-up reads don't feed the live header


def test_saturation_tracking_normalises_per_family():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="SDR", flow="full",
                  white={"xy": [0.3127, 0.329], "cct": 6504}))
    # Three red patches sweeping saturation (secondary channels below half-max → stay family "R"),
    # measured chroma growing with commanded saturation.
    sweeps = [([255, 26, 26], [1.0, 0.10, 0.10], [0.52, 0.33]),    # ~100% sat, farthest from white
              ([255, 77, 77], [1.0, 0.30, 0.30], [0.45, 0.33]),    # ~75%
              ([255, 115, 115], [1.0, 0.45, 0.45], [0.40, 0.33])]  # ~50%, closest
    for i, (rgb, sig, xy) in enumerate(sweeps):
        st.ingest(_ev(Ev.PATCH_READ, t=T0 + timedelta(seconds=i), stage="measure", tier="stream",
                      seq=i, role="measurement", label=f"r{i}", rgb=rgb, signal=sig,
                      Y=40.0, xy=xy, ok=True))
    sat = st.charts()["saturation"]
    reds = [p for p in sat if p["family"] == "R"]
    assert len(reds) == 3
    # commanded saturation buckets recovered (50/75/100%), sorted ascending
    assert [p["target"] for p in reds] == [0.5, 0.75, 1.0]
    # normalised so the most-saturated red = 1.0 and chroma tracks monotonically with command
    assert reds[-1]["measured"] == 1.0
    assert reds[0]["measured"] < reds[1]["measured"] < reds[2]["measured"]


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


def test_run_done_freezes_the_stage_clock():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.STAGE_START, t=T0 + timedelta(seconds=30), stage="measure"))
    st.ingest(_ev(Ev.RUN_DONE, t=T0 + timedelta(seconds=120), stage="run", status="completed"))
    snap = st.snapshot(T0 + timedelta(seconds=9999))
    assert abs(snap["timers"]["stage_elapsed_s"] - 90.0) < 1.0


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


def test_resumed_pause_seam_clears_paused_light():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run"))
    st.ingest(_ev(Ev.SEAM, t=T0, stage="measure", key="operator_pause", status="paused"))
    assert st.snapshot(T0 + timedelta(seconds=10))["liveness"]["light"] == "paused"

    st.ingest(_ev(Ev.SEAM, t=T0 + timedelta(seconds=20), stage="measure",
                  key="operator_pause", status="resumed"))
    assert st.snapshot(T0 + timedelta(seconds=21))["liveness"]["light"] == "live"


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
    # drift/white track (no neutral_ref here) falls back to WHITE-level (signal>=0.9) neutrals
    # only — the 0.5 grayscale step is excluded so the drift trace isn't buried in ramp noise.
    assert len(ch["white_track"]) == 1
    assert [w["signal"] for w in ch["white_track"]] == [1.0]
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


def test_hdr_header_drives_rec2020_pq_charts():
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", mode="HDR", is_hdr=True,
                  transfer="pq", luminance=1600.0, gamma=2.2,
                  white={"xy": [0.3127, 0.329], "cct": 6504}))
    _read(st, 1, [1023, 1023, 1023], [0.3127, 0.329], 1600.0, signal=[1.0, 1.0, 1.0])
    _read(st, 2, [1023, 0, 0], [0.708, 0.292], 0.2627 * 1600.0, signal=[1.0, 0.0, 0.0])

    ch = st.charts()
    assert ch["cie"]["gamut_label"] == "Rec.2020"
    assert ch["cie"]["primaries"]["r"] == [0.708, 0.292]
    assert ch["eotf"]["kind"] == "pq"
    assert ch["eotf"]["reference"][20][1] < 0.1
    cl = {c["label"]: c for c in ch["color_lum"]}
    assert "R100" in cl and abs(cl["R100"]["error"]) < 0.01


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
    # When neutral_ref checkpoints exist they ARE the drift series — the mid-grayscale
    # measurement neutral at signal 0.5 is excluded (it would just add noise).
    assert len(ch["white_track"]) == 1
    assert ch["white_track"][0]["cct"] is not None
    assert len(ch["cie"]["points"]) == 1               # the drift-ref stays OFF the snapshot
    assert [g["signal"] for g in ch["grayscale"]] == [0.5]


def test_drift_series_excludes_grayscale_ramp_noise():
    # The real-run bug: the drift chart plotted EVERY neutral measurement read at every grayscale
    # level, so dark/mid steps (noisy CCT) buried the actual white drift. With neutral_ref
    # checkpoints present, only those fixed re-reads form the drift series.
    st = DashboardState()
    st.ingest(_ev(Ev.RUN_HEADER, t=T0, stage="run", gamma=2.2, white={"xy": [0.3127, 0.329]}))
    # a full grayscale ramp of measurement neutrals — must NOT pollute the drift series
    for i, lvl in enumerate((0.1, 0.25, 0.5, 0.75, 1.0), start=1):
        v = int(round(lvl * 255))
        _read(st, i, [v, v, v], [0.31 + 0.001 * i, 0.33], 10.0 * i,
              signal=[lvl, lvl, lvl], phase="measure:raw")
    # three interleaved drift checkpoints (the fixed re-read neutral) over time
    for j, at in enumerate((6, 7, 8)):
        _read(st, at, [128, 128, 128], [0.312, 0.329], 40.0 + j, signal=[0.5, 0.5, 0.5],
              role="neutral_ref", disposition="drift_ref", phase="measure:raw")
    track = st.charts()["white_track"]
    assert len(track) == 3                              # only the neutral_ref checkpoints, not the ramp


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
