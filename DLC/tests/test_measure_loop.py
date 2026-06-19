"""Tests for the adaptive measurement loop (``dlc.measure_loop``).

Pure stdlib + a deterministic :class:`SyntheticPanel` — no numpy, no hardware, no
display, no meter — so this runs in the dependency-free spine suite. Mirrors the
engine's synthetic-panel style: drive the loop against a modelled QD mini-LED with
a temperamental blue channel and assert the self-healing behaviour (warm-up
settle, immediate repeatability gate, interleaved drift → appended re-measure,
escalation when a point won't stabilise).
"""

from __future__ import annotations

import json
from pathlib import Path

from dlc.engine.patches import Transfer, to_signal
from dlc.measure_loop import (
    AcceptedRead,
    MeasureLoopConfig,
    MeasurePatch,
    Reading,
    SyntheticPanel,
    _Loop,
    _NdjsonWriter,
    biased_neutral,
    make_spotread_meter,
    run_measure_loop,
    write_ti3,
)
from dlc.dip import DisplayInstrumentProfile, NoiseBand
from dlc.mhc import parse_ti3


def _sdr() -> Transfer:
    return Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)


def _grey_ramp(transfer: Transfer, n: int) -> list[tuple[int, int, int]]:
    """A dark→bright neutral ramp (luminance order) — maximises thermal creep, so
    a slowly-warming panel drifts mid-pass (exercises the appended re-measure)."""
    max_cv = transfer.max_cv
    levels = [round(i * max_cv / (n - 1)) for i in range(n)]
    return [(v, v, v) for v in levels]


def _patch(label: str, cv: tuple[int, int, int], transfer: Transfer, seq: int = 0) -> MeasurePatch:
    return MeasurePatch(label=label, rgb=cv, signal=to_signal([cv], transfer)[0], seq=seq)


# ---------------------------------------------------------------------------
# helpers: biased_neutral, write_ti3
# ---------------------------------------------------------------------------

def test_biased_neutral_is_bit_depth_aware_and_biases_cold_channel():
    t = _sdr()
    plain = biased_neutral(0.5, t)
    assert plain[0] == plain[1] == plain[2]
    assert 0 <= plain[0] <= t.max_cv

    biased = biased_neutral(0.5, t, cold_channel="B", bias_signal=0.02)
    assert biased[0] == biased[1] == plain[0]
    assert biased[2] == plain[2] + round(0.02 * t.max_cv)
    assert biased[2] <= t.max_cv


def test_write_ti3_round_trips_through_parse_ti3():
    t = _sdr()
    acc = [
        AcceptedRead(patch=_patch("p0", (0, 0, 0), t, 0), xyz=(0.10, 0.10, 0.11)),
        AcceptedRead(patch=_patch("p1", (1023, 1023, 1023), t, 1), xyz=(95.0, 100.0, 108.0)),
    ]
    out = Path("__ti3_tmp__.ti3")
    try:
        write_ti3(out, acc)
        samples = parse_ti3(out)
        assert len(samples) == 2
        assert samples[1].rgb == (1.0, 1.0, 1.0)
        assert abs(samples[1].xyz[1] - 100.0) < 1e-3
        assert samples[0].rgb == (0.0, 0.0, 0.0)
    finally:
        out.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# warm-up-settle
# ---------------------------------------------------------------------------

def test_warmup_settles_and_detects_cold_blue(tmp_path: Path):
    t = _sdr()
    panel = SyntheticPanel(transfer=t, warm_tau=0.06, cold_blue_gain=0.90)
    res = run_measure_loop(
        patches=_grey_ramp(t, 12),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(),
        ti3_path=tmp_path / "m.ti3",
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.warm is True
    assert res.digest["cold_channel"] == "B"  # the temperamental channel
    assert res.reference_xyz is not None
    assert res.warmup_reads >= MeasureLoopConfig().settle_required


def test_warmup_escalates_when_it_never_settles(tmp_path: Path):
    t = _sdr()
    panel = SyntheticPanel(transfer=t, warm_tau=0.06)
    # An impossibly tight settle tolerance can never be met by a creeping panel.
    res = run_measure_loop(
        patches=_grey_ramp(t, 8),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(settle_threshold=1e-9, max_warmup_reads=8),
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.warm is False
    assert res.needs_adjudication is True
    assert res.question is not None and "settle" in res.question


# ---------------------------------------------------------------------------
# per-patch read policy (single-read default + DIP-driven escalation)
# ---------------------------------------------------------------------------

def _solo_loop(panel, transfer: Transfer, cfg: MeasureLoopConfig,
               dip: DisplayInstrumentProfile | None = None) -> _Loop:
    return _Loop(
        patches=[],
        transfer=transfer,
        measure=panel,
        config=cfg,
        ndjson=_NdjsonWriter(None),
        events=None,
        dip=dip,
    )


class _ScriptedPanel:
    """A :data:`MeasureFn` returning a fixed XYZ sequence (cycling on the last entry)
    — for precise read-policy tests independent of the synthetic panel's glitch model."""

    def __init__(self, seq: list[tuple[float, float, float]]) -> None:
        self._seq = list(seq)
        self._i = 0

    def __call__(self, patch: MeasurePatch) -> Reading:
        xyz = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return Reading(xyz=xyz, yxy=(xyz[1], 0.31, 0.33), ok=True)


def _dip_for(nits: float, sigma_de: float) -> DisplayInstrumentProfile:
    return DisplayInstrumentProfile(display="x", noise_model=[NoiseBand(nits=nits, sigma_de=sigma_de, reads=20)])


def test_single_read_is_the_default_without_a_dip(tmp_path: Path):
    # No DIP ⇒ one adaptive-integration read per patch (the professional default).
    t = _sdr()
    panel = SyntheticPanel(transfer=t, start_temp=1.0)
    res = run_measure_loop(
        patches=_grey_ramp(t, 8), transfer=t, measure=panel,
        config=MeasureLoopConfig(), ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.immediate_remeasures == 0
    assert res.patch_count == 8


def test_dip_drives_extra_averaged_reads_where_snr_is_poor():
    # σ=0.4 dE at this luminance, tolerance 0.2 ⇒ N=(0.4/0.2)²=4 reads averaged for SNR.
    t = _sdr()
    panel = _ScriptedPanel([(50.0, 50.0, 55.0)])   # a clean, perfectly-repeatable read
    loop = _solo_loop(panel, t, MeasureLoopConfig(read_tolerance_de=0.2), dip=_dip_for(50.0, 0.4))
    rec = loop.measure_patch(_patch("px", (512, 512, 512), t, 0), phase="main")
    assert rec.reads_taken == 4
    assert rec.immediate_remeasures == 3
    assert rec.unstable is False
    assert abs(rec.xyz[1] - 50.0) < 1e-9


def test_gross_glitch_is_rejected_not_averaged_in():
    # A big one-off outlier (read 0) must be DROPPED from the mean, not diluted in.
    t = _sdr()
    clean = (50.0, 50.0, 55.0)
    glitch = (50.0, 80.0, 55.0)
    panel = _ScriptedPanel([glitch, clean, clean, clean, clean, clean])
    loop = _solo_loop(panel, t, MeasureLoopConfig(read_tolerance_de=0.3), dip=_dip_for(50.0, 0.5))
    rec = loop.measure_patch(_patch("px", (512, 512, 512), t, 0), phase="main")
    assert rec.unstable is False
    assert abs(rec.xyz[1] - 50.0) < 0.5    # accepted ≈ clean — the Y=80 glitch was rejected
    assert rec.reads_taken >= 4            # one extra read to replace the rejected glitch


def test_unconverging_patch_is_flagged_not_silently_capped():
    # Reads that never settle (a steady drift, no stable cluster) → SE never tightens →
    # FLAG at the abnormal bound. Not unbounded, not silently accepted.
    t = _sdr()
    panel = _ScriptedPanel([(50.0, 30.0 + 4.0 * i, 55.0) for i in range(30)])
    loop = _solo_loop(panel, t, MeasureLoopConfig(read_tolerance_de=0.2, abnormal_reads=8),
                      dip=_dip_for(50.0, 0.3))
    rec = loop.measure_patch(_patch("px", (512, 512, 512), t, 0), phase="main")
    assert rec.unstable is True
    assert rec.note and "abnormal" in rec.note
    assert rec.reads_taken <= 12           # bounded — flagged, not run forever, not capped-and-accepted


# ---------------------------------------------------------------------------
# interleaved drift reference → appended re-measure
# ---------------------------------------------------------------------------

def test_warmup_creep_triggers_drift_episode_and_appended_remeasure(tmp_path: Path):
    t = _sdr()
    # Warm-up settles mid-creep; the panel keeps warming through the main pass, so
    # the interleaved neutral reference detects the absolute drift the per-step
    # settle threshold missed → the early patches are redone once stable.
    panel = SyntheticPanel(transfer=t, warm_tau=0.06, cold_blue_gain=0.88)
    ti3 = tmp_path / "m.ti3"
    res = run_measure_loop(
        patches=_grey_ramp(t, 24),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(neutral_interval=6),
        ti3_path=ti3,
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.warm is True
    assert res.drift_episodes >= 1
    assert res.appended_remeasures >= 1
    assert res.patch_count == 24
    # The clean .ti3 holds exactly one accepted row per distinct patch.
    assert len(parse_ti3(ti3)) == 24
    assert res.needs_adjudication is False


def test_remeasure_cap_is_advisory_and_surfaces_for_adjudication(tmp_path: Path):
    # The appended remeasure cap is an LLM-review threshold, not a hard data-loss limit.
    # Even with a zero budget, queued drift casualties are remeasured and the .ti3 remains
    # one final accepted row per patch; the abnormal volume is surfaced in the digest.
    t = _sdr()
    panel = SyntheticPanel(transfer=t, warm_tau=0.06, cold_blue_gain=0.88)
    ti3 = tmp_path / "m.ti3"
    res = run_measure_loop(
        patches=_grey_ramp(t, 24),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(neutral_interval=6, remeasure_cap=0),
        ti3_path=ti3,
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.appended_remeasures > 0
    assert res.unresolved == []
    assert res.digest["remeasure_budget_exceeded"] is True
    assert res.digest["remeasure_cap"] == 0
    assert res.needs_adjudication is True
    assert res.question is not None and "advisory budget" in res.question
    assert len(parse_ti3(ti3)) == 24


def test_dense_drift_tightens_interval_and_flags_density():
    # Repeated neutral-reference excursions in the same direction are not just ordinary
    # bounded wander: the loop tightens its checkpoint cadence and surfaces the density.
    t = _sdr()
    loop = _solo_loop(
        _ScriptedPanel([(10.0, 10.0, 10.5), (10.0, 10.0, 11.0), (9.8, 10.0, 11.0)]),
        t,
        MeasureLoopConfig(
            neutral_interval=16,
            adaptive_neutral_min=4,
            drift_threshold=0.004,
            drift_density_window=3,
            drift_density_limit=3,
        ),
    )
    loop.reference_xyz = (10.0, 10.0, 10.0)
    warmup = loop._warmup_patch()
    for idx in range(3):
        pending = [f"p{idx:04d}"]
        loop._neutral_checkpoint(warmup, pending, final=True, patch_index=(idx + 1) * 16)

    assert loop.drift_density_exceeded is True
    assert loop.drift_regime == "directional_warm_in"
    assert loop.neutral_interval_current == 4
    assert loop.neutral_interval_adjustments == 2
    assert loop.drift_checkpoints[-1]["repeat_density"] == 1.0
    assert loop.drift_checkpoints[-1]["dominant_channel"] == "B"


# ---------------------------------------------------------------------------
# thermal preheat: soak-into-calibration
# ---------------------------------------------------------------------------

def _bright_content(t: Transfer) -> list[tuple[int, int, int]]:
    """A mid-to-bright load set (greys + R/G/B) — enough drive to warm the panel."""
    cv = t.max_cv
    greys = [(round(s * cv),) * 3 for s in (0.4, 0.55, 0.7, 0.85, 1.0)]
    hi = round(0.8 * cv)
    return greys + [(hi, 0, 0), (0, hi, 0), (0, 0, hi)]


def _loop_with(panel, t: Transfer, cfg: MeasureLoopConfig, patches, dip=None) -> _Loop:
    return _Loop(patches=patches, transfer=t, measure=panel, config=cfg,
                 ndjson=_NdjsonWriter(None), events=None, dip=dip)


def test_preheat_soaks_a_cold_panel_before_measuring():
    # A cold, content-driven panel can't be warmed by holding grey; the preheat soaks it (using
    # the run's OWN patch set as the load) so the panel is parked at its operating load first.
    t = _sdr()
    panel = SyntheticPanel(transfer=t, cold_blue_gain=0.85, load_thermal=True,
                           thermal_rate=0.06, start_temp=0.0)
    loop = _loop_with(panel, t, MeasureLoopConfig(preheat="always"), _bright_content(t))
    assert panel.temp == 0.0
    digest = loop.preheat()
    assert digest is not None and digest["content_reads"] > 0
    assert panel.temp > 0.3                       # the soak actually heated the panel
    assert loop.cold_channel == "B"               # the biggest thermal mover, discovered not assumed


def test_preheat_auto_runs_controller_and_decides_live():
    # "auto" runs the closed-loop controller on ANY characterized panel and lets it decide from
    # LIVE state — NO regime-label branch. A COLD panel soaks (heats up) for EVERY regime label
    # (the decision is live, not label-driven — this is the cold-next-morning fix: a convergent DIP
    # no longer skips the soak on a cold panel). An ALREADY-STABLE panel self-deactivates: the
    # controller runs but converges cheaply without a real soak. Uncharacterized / compromised ⇒
    # no controller (the static-grey settle gate covers it).
    t = _sdr()

    def cold_panel():   # cold + temperamental ⇒ genuinely warms in (balance drifts with temp)
        return SyntheticPanel(transfer=t, cold_blue_gain=0.85, load_thermal=True,
                              thermal_rate=0.06, start_temp=0.0)

    def stable_panel():  # inert balance (no chroma drift with temp) ⇒ reads in-band immediately
        return SyntheticPanel(transfer=t, cold_blue_gain=1.0, load_thermal=True,
                              thermal_rate=0.06, start_temp=0.0)

    def dip(regime, **kw):
        return DisplayInstrumentProfile(display="x", thermal_regime=regime, **kw)

    # Characterized + COLD ⇒ the controller engages and actually warms the panel — for EVERY regime.
    cold_reads = {}
    for regime in ("fluctuating", "warming", "convergent"):
        cold = cold_panel()
        loop = _loop_with(cold, t, MeasureLoopConfig(preheat="auto"), _bright_content(t),
                          dip=dip(regime, recommended_drift_threshold=0.004))
        digest = loop.preheat()
        assert digest is not None, regime
        assert cold.temp > 0.3, regime            # live-cold ⇒ the soak actually heated it
        cold_reads[regime] = digest["content_reads"]

    # Characterized + ALREADY STABLE ⇒ the controller self-deactivates: it runs but converges with
    # far fewer content reads than the cold case (no real soak), so the warm path stays cheap.
    stable = stable_panel()
    stable_loop = _loop_with(stable, t, MeasureLoopConfig(preheat="auto"), _bright_content(t),
                             dip=dip("convergent", recommended_drift_threshold=0.004))
    stable_digest = stable_loop.preheat()
    assert stable_digest is not None
    assert stable_digest["content_reads"] < cold_reads["convergent"]   # self-deactivated, no soak

    # Uncharacterized / compromised ⇒ no controller (fall back to the static-grey settle gate).
    no_dip = _loop_with(cold_panel(), t, MeasureLoopConfig(preheat="auto"), _bright_content(t))
    assert no_dip.preheat() is None
    compromised = _loop_with(cold_panel(), t, MeasureLoopConfig(preheat="auto"),
                             _bright_content(t), dip=dip("compromised"))
    assert compromised.preheat() is None


def test_cold_calibration_after_warm_characterization_still_soaks():
    # The cold-next-morning bug this change kills: the panel was characterized WARM (so the DIP
    # records a tiny warmin_magnitude + a warm_balance), then calibrated COLD. The OLD gate read the
    # stale warmin_magnitude, decided 'convergent + negligible warm-in ⇒ skip', and silently measured
    # a still-warming panel. The unified live-state controller MUST engage and soak the cold panel.
    t = _sdr()
    cold = SyntheticPanel(transfer=t, cold_blue_gain=0.85, load_thermal=True,
                          thermal_rate=0.06, start_temp=0.0)
    warm_characterized = DisplayInstrumentProfile(
        display="x", thermal_regime="convergent",
        warmin_magnitude=0.001,                         # characterize saw ~no warm-in (it ran warm)
        warm_balance=[0.33, 0.33, 0.34],
        recommended_drift_threshold=0.004)
    loop = _loop_with(cold, t, MeasureLoopConfig(preheat="auto"), _bright_content(t),
                      dip=warm_characterized)
    digest = loop.preheat()
    assert digest is not None                            # the controller engaged — no stale-gate skip
    assert cold.temp > 0.3                               # and actually warmed the cold panel
    assert digest["baseline_distance"] is not None       # the warm_balance distance was shadow-logged


def test_preheat_wires_into_run_and_is_off_by_default(tmp_path: Path):
    t = _sdr()
    # Default (auto, no DIP) ⇒ no preheat, behaviour unchanged.
    plain = run_measure_loop(patches=_grey_ramp(t, 8), transfer=t,
                             measure=SyntheticPanel(transfer=t, start_temp=1.0),
                             config=MeasureLoopConfig(), ndjson_path=tmp_path / "a.ndjson")
    assert plain.digest["preheat"] is None
    assert plain.patch_count == 8
    # preheat="always" ⇒ the soak runs and is reported; the run still completes normally.
    cold = SyntheticPanel(transfer=t, cold_blue_gain=0.85, load_thermal=True,
                          thermal_rate=0.06, start_temp=0.0)
    soaked = run_measure_loop(patches=_grey_ramp(t, 8), transfer=t, measure=cold,
                              config=MeasureLoopConfig(preheat="always"),
                              ndjson_path=tmp_path / "b.ndjson")
    assert soaked.digest["preheat"] is not None
    assert soaked.digest["preheat"]["content_reads"] > 0
    assert soaked.patch_count == 8
    # the preheat summary marker lands in the ndjson (per-block soak rows do NOT — kept clean).
    rows = [json.loads(ln) for ln in (tmp_path / "b.ndjson").read_text().splitlines()]
    assert any(r.get("role") == "preheat_complete" for r in rows)
    assert not any(r.get("phase") == "thermal" for r in rows)


def test_persistent_flaky_patch_surfaces_as_unresolved(tmp_path: Path):
    t = _sdr()
    # start warm so warm-up settles immediately; only p0003 misbehaves.
    panel = SyntheticPanel(transfer=t, start_temp=1.0, flaky_label="p0003",
                           flaky_persistent=True, flaky_chroma=0.06)
    # A DIP makes the loop take >1 read per patch (σ=0.3 ⇒ target 3); the persistent
    # glitch never agrees with itself → SE never tightens → flagged at the abnormal
    # bound → surfaced for adjudication. (Without a DIP it would be a trusted single read.)
    res = run_measure_loop(
        patches=_grey_ramp(t, 8),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(read_tolerance_de=0.2, abnormal_reads=6),
        ndjson_path=tmp_path / "m.ndjson",
        dip=_dip_for(50.0, 0.3),
    )
    assert res.warm is True
    assert "p0003" in res.unresolved
    assert res.needs_adjudication is True
    assert res.question is not None and "stabilise" in res.question


# ---------------------------------------------------------------------------
# measurements.ndjson stream
# ---------------------------------------------------------------------------

def test_ndjson_stream_is_one_line_per_read_with_pinned_schema(tmp_path: Path):
    t = _sdr()
    panel = SyntheticPanel(transfer=t, warm_tau=0.06)
    ndj = tmp_path / "m.ndjson"
    res = run_measure_loop(
        patches=_grey_ramp(t, 16),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(neutral_interval=5),
        ndjson_path=ndj,
    )
    lines = [json.loads(ln) for ln in ndj.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # The stream is read records (one line per probe read) interleaved with control markers
    # (warm-up completion verdicts — NOT reads, no seq). Separate them.
    reads = [ln for ln in lines if ln.get("role") != "warmup_complete"]
    markers = [ln for ln in lines if ln.get("role") == "warmup_complete"]
    assert len(reads) == res.total_reads
    # seq is a dense 0..N-1 index over READS (every probe read accounted for, none dropped).
    assert [ln["seq"] for ln in reads] == list(range(len(reads)))
    required = {
        "t", "seq", "phase", "role", "label", "rgb", "signal", "read_index",
        "xyz", "yxy", "nits", "ok", "accepted", "agreement_de", "drift",
        "settle", "disposition", "note",
    }
    for ln in reads:
        assert required <= set(ln)
    phases = {ln["phase"] for ln in reads}
    assert {"warmup", "main"} <= phases
    # the interleaved checkpoints carry a drift verdict
    assert any(ln["role"] == "neutral_ref" and ln["drift"] is not None for ln in reads)
    # the warm-up completion marker streams the honest settle verdict (for the readout)
    assert markers and all(set(m) >= {"role", "settled", "phase"} for m in markers)
    assert markers[-1]["settled"] == res.warm


# ---------------------------------------------------------------------------
# live spotread meter wiring (no hardware: fake presenter + fake spotread)
# ---------------------------------------------------------------------------

class _FakePresenter:
    def __init__(self) -> None:
        self.shown: list[tuple[int, int, int]] = []

    def show(self, patch: MeasurePatch) -> None:
        self.shown.append(patch.rgb)

    def close(self) -> None:
        pass


class _FakeCompleted:
    returncode = 0
    stdout = "Result: Yxy: 100.0 0.3127 0.3290\n XYZ: 95.0 100.0 108.0\n"
    stderr = ""


class _FakeSpotread:
    def __init__(self) -> None:
        self.calls = 0

    def run_spotread_once(self, request, timeout_seconds=None):  # noqa: ANN001 - duck-typed
        self.calls += 1
        return _FakeCompleted()


def test_make_spotread_meter_presents_then_reads(tmp_path: Path):
    t = _sdr()
    presenter = _FakePresenter()
    spotread = _FakeSpotread()
    meter = make_spotread_meter(
        presenter=presenter,
        spotread=spotread,
        port=1,
        output_dir=tmp_path / "probe",
    )
    reading = meter(_patch("p0", (512, 512, 512), t, 0))
    assert presenter.shown == [(512, 512, 512)]   # presented before reading
    assert spotread.calls == 1
    assert reading.ok is True
    assert reading.xyz == (95.0, 100.0, 108.0)
    assert reading.yxy == (100.0, 0.3127, 0.3290)


def test_make_spotread_meter_reports_spotread_failure(tmp_path: Path):
    t = _sdr()

    class _Bad(_FakeCompleted):
        returncode = 1

    class _BadSpot:
        def run_spotread_once(self, request, timeout_seconds=None):  # noqa: ANN001
            return _Bad()

    meter = make_spotread_meter(
        presenter=_FakePresenter(),
        spotread=_BadSpot(),
        port=1,
        output_dir=tmp_path / "probe",
    )
    reading = meter(_patch("p0", (512, 512, 512), t, 0))
    assert reading.ok is False
    assert reading.error is not None and "spotread" in reading.error


def test_make_spotread_meter_bounded_on_timeout(tmp_path: Path):
    """A hung one-shot meter must come back ok=False, NOT raise TimeoutExpired and
    crash the run — the whole checkpoint-liveness design depends on this contract."""
    import subprocess

    t = _sdr()

    class _HangSpot:
        def run_spotread_once(self, request, timeout_seconds=None):  # noqa: ANN001
            # subprocess.run would have killed the hung process and raised this.
            raise subprocess.TimeoutExpired(cmd="spotread", timeout=timeout_seconds or 60)

    meter = make_spotread_meter(
        presenter=_FakePresenter(),
        spotread=_HangSpot(),
        port=1,
        output_dir=tmp_path / "probe",
        read_timeout=5.0,
    )
    reading = meter(_patch("p0", (512, 512, 512), t, 0))
    assert reading.ok is False
    assert reading.xyz is None
    assert reading.error is not None and "timed out" in reading.error


def test_make_spotread_meter_bounded_on_spawn_error(tmp_path: Path):
    """A spawn failure (meter unplugged mid-run) is also a bounded ok=False, not a crash."""
    t = _sdr()

    class _NoSpot:
        def run_spotread_once(self, request, timeout_seconds=None):  # noqa: ANN001
            raise OSError("device not found")

    meter = make_spotread_meter(
        presenter=_FakePresenter(),
        spotread=_NoSpot(),
        port=1,
        output_dir=tmp_path / "probe",
    )
    reading = meter(_patch("p0", (512, 512, 512), t, 0))
    assert reading.ok is False
    assert reading.error is not None and "spawn failed" in reading.error


def test_measure_loop_aborts_on_stall(tmp_path: Path):
    """A panel whose reads never SUCCEED makes no progress; with a tiny stall threshold
    the loop's guard must raise RunStalled (a clean abort the orchestrator rolls back)
    instead of grinding forever — the 53-min wedge, prevented."""
    import time as _t

    import pytest

    from dlc.events import Ev, RunLog, read_events
    from dlc.liveness import Liveness, RunStalled

    t = _sdr()

    def hung(patch: MeasurePatch) -> Reading:
        _t.sleep(0.01)                       # reads "work" but never succeed → no progress
        return Reading(xyz=None, yxy=None, ok=False, error="hung")

    epath = tmp_path / "e.jsonl"
    live = Liveness(RunLog(epath), stall_after_s=0.05)
    with pytest.raises(RunStalled):
        run_measure_loop(patches=_grey_ramp(t, 16), transfer=t, measure=hung,
                         config=MeasureLoopConfig(), liveness=live)
    assert any(e.event == Ev.STALL for e in read_events(epath))


def test_measure_loop_emits_quartile_check_ins_and_completion_digest(tmp_path: Path):
    """The LLM's digest projection is otherwise blind during a long measure (patch_read +
    heartbeat are stream-tier): progress-driven quartile check-ins + a completion digest give
    it the forward-motion signal + the stage outcome it needs to gate the run."""
    from dlc.events import RunLog, read_events, digest_projection

    t = _sdr()
    panel = SyntheticPanel(transfer=t, white_nits=120.0)
    epath = tmp_path / "e.jsonl"
    runlog = RunLog(epath, phase="measure:post-mhc")
    run_measure_loop(patches=_grey_ramp(t, 16), transfer=t, measure=panel,
                     config=MeasureLoopConfig(), runlog=runlog,
                     ti3_path=tmp_path / "m.ti3", ndjson_path=tmp_path / "m.ndjson")

    digest = digest_projection(read_events(epath))
    names = [e.event for e in digest]
    assert "patch_read" not in names and "heartbeat" not in names   # firehose stays off the digest

    check_ins = [e for e in digest if e.event == "check_in"]
    assert len(check_ins) == 3                                      # 25 / 50 / 75 % of 16 patches
    assert {round(e.data["progress"], 2) for e in check_ins} == {0.25, 0.5, 0.75}
    assert check_ins[0].phase == "measure:post-mhc"

    completed = [e for e in digest if e.event == "completed" and e.stage == "measure_loop"]
    assert len(completed) == 1                                      # the measure OUTCOME on the spine
    assert completed[0].data["patch_count"] == 16 and "warm" in completed[0].data


def test_write_ti3_excludes_unusable_holes(tmp_path: Path):
    """A sentinel hole (no usable read → (0,0,0)) must never reach the .ti3 — the MHC /
    cube builders parse it with no knowledge of `unstable`, so a black row would poison
    the build."""
    from dlc.measure_loop import AcceptedRead, write_ti3

    t = _sdr()
    good = AcceptedRead(patch=_patch("g", (512, 512, 512), t, 0), xyz=(50.0, 52.0, 55.0))
    hole = AcceptedRead(patch=_patch("h", (0, 0, 0), t, 1), xyz=(0.0, 0.0, 0.0),
                        unstable=True, usable=False)
    text = write_ti3(tmp_path / "m.ti3", [good, hole]).read_text()
    assert "NUMBER_OF_SETS 1" in text     # only the good row survived
    assert "52.000000" in text            # the good read's XYZ_Y is present
    assert "0.000000 0.000000 0.000000" not in text   # the black hole is not a data row
