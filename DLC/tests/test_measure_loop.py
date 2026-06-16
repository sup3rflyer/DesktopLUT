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
# immediate repeatability gate
# ---------------------------------------------------------------------------

def _solo_loop(panel: SyntheticPanel, transfer: Transfer, cfg: MeasureLoopConfig) -> _Loop:
    return _Loop(
        patches=[],
        transfer=transfer,
        measure=panel,
        config=cfg,
        ndjson=_NdjsonWriter(None),
        events=None,
    )


def test_transient_flaky_patch_is_re_measured_and_converges():
    t = _sdr()
    panel = SyntheticPanel(transfer=t, start_temp=1.0, flaky_label="px", flaky_chroma=0.06)
    loop = _solo_loop(panel, t, MeasureLoopConfig(confirm_reads=2, repeat_threshold=0.5, max_repeats=3))
    rec = loop.measure_patch(_patch("px", (512, 512, 512), t, 0), phase="main")
    assert rec.immediate_remeasures >= 2  # extra re-read beyond the clean single confirm
    assert rec.unstable is False          # converged to a good read
    # The accepted value matches a clean (non-glitch) read of the same patch.
    clean = SyntheticPanel(transfer=t, start_temp=1.0)(_patch("px", (512, 512, 512), t, 0))
    assert abs(rec.xyz[1] - clean.xyz[1]) < 1e-6


def test_persistent_flaky_patch_stays_unstable():
    t = _sdr()
    panel = SyntheticPanel(transfer=t, start_temp=1.0, flaky_label="px",
                           flaky_persistent=True, flaky_chroma=0.06)
    loop = _solo_loop(panel, t, MeasureLoopConfig(confirm_reads=2, repeat_threshold=0.5, max_repeats=3))
    rec = loop.measure_patch(_patch("px", (512, 512, 512), t, 0), phase="main")
    assert rec.unstable is True
    assert rec.immediate_remeasures == 3  # exhausted max_repeats without agreement


def test_single_read_mode_does_no_immediate_remeasures(tmp_path: Path):
    t = _sdr()
    panel = SyntheticPanel(transfer=t, start_temp=1.0)
    res = run_measure_loop(
        patches=_grey_ramp(t, 8),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(confirm_reads=1),
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.immediate_remeasures == 0
    assert res.patch_count == 8


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


def test_persistent_flaky_patch_surfaces_as_unresolved(tmp_path: Path):
    t = _sdr()
    # start warm so warm-up settles immediately; only p0003 misbehaves.
    panel = SyntheticPanel(transfer=t, start_temp=1.0, flaky_label="p0003",
                           flaky_persistent=True, flaky_chroma=0.06)
    res = run_measure_loop(
        patches=_grey_ramp(t, 8),
        transfer=t,
        measure=panel,
        config=MeasureLoopConfig(),
        ndjson_path=tmp_path / "m.ndjson",
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
    assert len(lines) == res.total_reads
    # seq is a dense 0..N-1 index (every probe read accounted for, none dropped).
    assert [ln["seq"] for ln in lines] == list(range(len(lines)))
    required = {
        "t", "seq", "phase", "role", "label", "rgb", "signal", "read_index",
        "xyz", "yxy", "nits", "ok", "accepted", "agreement_de", "drift",
        "settle", "disposition", "note",
    }
    for ln in lines:
        assert required <= set(ln)
    phases = {ln["phase"] for ln in lines}
    assert {"warmup", "main"} <= phases
    # the interleaved checkpoints carry a drift verdict
    assert any(ln["role"] == "neutral_ref" and ln["drift"] is not None for ln in lines)


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

    def run_spotread_once(self, request):  # noqa: ANN001 - duck-typed
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
        def run_spotread_once(self, request):  # noqa: ANN001
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
