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
    IncrementalMeasureSession,
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
        # A near-black patch (cv 10 ≈ 0.978% signal) writes to a 0–100 value BELOW 1.0; it
        # MUST round-trip to its true sub-1% signal, not be mis-read as a ~0.978 near-peak
        # signal — the parse_ti3 scale bug that scored dark HDR patches at dE_ITP ~700.
        AcceptedRead(patch=_patch("p2", (10, 10, 10), t, 2), xyz=(0.20, 0.21, 0.23)),
    ]
    out = Path("__ti3_tmp__.ti3")
    try:
        write_ti3(out, acc)
        samples = parse_ti3(out)
        assert len(samples) == 3
        assert samples[1].rgb == (1.0, 1.0, 1.0)
        assert abs(samples[1].xyz[1] - 100.0) < 1e-3
        assert samples[0].rgb == (0.0, 0.0, 0.0)
        dark = samples[2].rgb[0]
        assert abs(dark - 10 / 1023) < 1e-4   # true sub-1% signal preserved
        assert dark < 0.02                     # NOT the ~0.978 the old >1.0 heuristic produced
    finally:
        out.unlink(missing_ok=True)


def test_parse_ti3_scales_and_clamps_per_spec(tmp_path: Path):
    # Argyll .ti3 device values are 0–100 percent: parse_ti3 divides by 100 UNCONDITIONALLY
    # (a sub-1.0 entry is a sub-1% patch, not an already-0–1 signal) and clamps to [0, 1]
    # for out-of-spec / meter-noise values.
    ti3 = tmp_path / "edge.ti3"
    ti3.write_text(
        "CTI3\nBEGIN_DATA_FORMAT\nRGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\nEND_DATA_FORMAT\n"
        "NUMBER_OF_SETS 5\nBEGIN_DATA\n"
        "0.0 0.0 0.0 0.0 0.0 0.0\n"                          # black
        "100.0 100.0 100.0 95.0 100.0 108.0\n"              # full white
        "0.977517 0.977517 0.977517 0.005 0.006 0.008\n"    # near-black (was mis-read as ~0.978)
        "150.0 150.0 150.0 1.0 1.0 1.0\n"                   # over-spec -> clamp 1.0
        "-0.5 -0.5 -0.5 0.0 0.0 0.0\n"                      # negative -> clamp 0.0
        "END_DATA\n",
        encoding="utf-8",
    )
    rgbs = [s.rgb for s in parse_ti3(ti3)]
    assert rgbs[0] == (0.0, 0.0, 0.0)
    assert rgbs[1] == (1.0, 1.0, 1.0)
    assert all(abs(c - 0.00977517) < 1e-6 for c in rgbs[2])   # sub-1% preserved, NOT ~0.978
    assert rgbs[3] == (1.0, 1.0, 1.0)                          # over-spec clamped
    assert rgbs[4] == (0.0, 0.0, 0.0)                          # negative clamped


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
# near-neutral read floor (chroma-critical region)
# ---------------------------------------------------------------------------

def _tube_patch(level: int, frac: float, t: Transfer, direction=(1, -1, -1)) -> MeasurePatch:
    """An off-axis near-neutral tube patch built like engine.patches.near_neutral_tube_patches:
    one channel +d, two -d, d = round(level*frac)."""
    d = max(1, round(level * frac))
    cv = tuple(max(0, min(t.max_cv, level + s * d)) for s in direction)
    return _patch("tube", cv, t, 0)  # type: ignore[arg-type]


def test_is_near_neutral_classifies_gray_and_tube_but_not_pure_channel():
    t = _sdr()
    loop = _solo_loop(_ScriptedPanel([(50.0, 50.0, 55.0)]), t, MeasureLoopConfig())
    # Grey axis: zero chroma span.
    assert loop._is_near_neutral(_patch("g", (512, 512, 512), t)) is True
    # Tube at both default offsets: relative span 2*frac/(1+frac) ≈ 0.11 / 0.26 — inside 0.35.
    assert loop._is_near_neutral(_tube_patch(512, 0.06, t)) is True
    assert loop._is_near_neutral(_tube_patch(512, 0.15, t)) is True
    # A pure-channel ramp patch has a zero channel ⇒ span == max ⇒ excluded (at any level).
    assert loop._is_near_neutral(_patch("r", (512, 0, 0), t)) is False
    assert loop._is_near_neutral(_patch("r-dark", (40, 0, 0), t)) is False
    # A secondary (two channels lit, one zero) is likewise off-axis, not near-neutral.
    assert loop._is_near_neutral(_patch("yellow", (512, 512, 0), t)) is False
    # Pure black has no defined chroma ⇒ not flagged.
    assert loop._is_near_neutral(_patch("k", (0, 0, 0), t)) is False


def test_neutral_floor_averages_the_grey_region_even_without_a_dip():
    # No DIP ⇒ dip_n is None; the floor is the fixed-N fallback for the chroma-critical region.
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])   # perfectly repeatable ⇒ SE=0, would stop at 1
    loop = _solo_loop(clean, t, MeasureLoopConfig(neutral_min_reads=4))
    rec = loop.measure_patch(_patch("g", (512, 512, 512), t, 0), phase="main")
    assert rec.reads_taken == 4
    assert rec.immediate_remeasures == 3
    assert rec.unstable is False


def test_neutral_floor_does_not_touch_off_axis_patches():
    # Same config, but a pure-channel patch is NOT near-neutral ⇒ single-read default holds.
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 0.0, 0.0)])
    loop = _solo_loop(clean, t, MeasureLoopConfig(neutral_min_reads=4))
    rec = loop.measure_patch(_patch("r", (512, 0, 0), t, 0), phase="main")
    assert rec.reads_taken == 1
    assert rec.immediate_remeasures == 0


def test_dip_escalates_above_the_neutral_floor():
    # Floor=2, but σ=0.4 @ tol 0.2 ⇒ dip_n=4. target = max(floor, dip_n) ⇒ 4, not capped at the floor.
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])
    loop = _solo_loop(clean, t, MeasureLoopConfig(read_tolerance_de=0.2, neutral_min_reads=2),
                      dip=_dip_for(50.0, 0.4))
    rec = loop.measure_patch(_patch("g", (512, 512, 512), t, 0), phase="main")
    assert rec.reads_taken == 4


def test_neutral_floor_gated_by_luminance_skips_slow_dim_patches():
    # A bright grey patch is floored (fast, larger σ); a dim grey patch below the nits gate is
    # NOT (slow to read, smallest σ, thermally risky) — even though both are near-neutral.
    t = _sdr()  # power 2.2, peak 120 nits, 10-bit
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])
    cfg = MeasureLoopConfig(neutral_min_reads=4, neutral_floor_min_nits=10.0)
    loop = _solo_loop(clean, t, cfg)
    bright = _patch("g-hi", (922, 922, 922), t, 0)   # ~0.9 signal → well above 10 nits
    assert loop._expected_patch_nits(bright) >= 10.0
    assert loop._read_floor_for(bright) == 4
    dim = _patch("g-lo", (102, 102, 102), t, 0)      # ~0.1 signal → a few nits, below the gate
    assert loop._expected_patch_nits(dim) < 10.0
    assert loop._read_floor_for(dim) == cfg.min_reads


def test_neutral_floor_defaults_off():
    # Default neutral_min_reads=1 ⇒ behaviour unchanged: single read on a clean grey patch.
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])
    loop = _solo_loop(clean, t, MeasureLoopConfig())
    rec = loop.measure_patch(_patch("g", (512, 512, 512), t, 0), phase="main")
    assert rec.reads_taken == 1


def test_dark_floor_reads_dim_grey_and_records_chroma_sigma():
    # The dark read floor: a DIM near-neutral patch is read several times so its chromaticity spread
    # can be estimated (the dark-level trust input) — and the spread is recorded on the accepted read.
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])
    cfg = MeasureLoopConfig(dark_min_reads=4, dark_floor_max_nits=120.0)   # gate high ⇒ dim grey floored
    loop = _solo_loop(clean, t, cfg)
    dim = _patch("g-lo", (102, 102, 102), t, 0)
    assert loop._read_floor_for(dim) == 4
    rec = loop.measure_patch(dim, phase="main")
    assert rec.reads_taken == 4
    assert rec.chroma_sigma is not None          # ≥2 reads ⇒ spread estimated (0.0 on a clean panel)
    assert rec.se_de is not None


def test_dark_floor_defaults_off_and_gated_by_nits():
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])
    # default off ⇒ single read on a dim grey
    loop = _solo_loop(clean, t, MeasureLoopConfig())
    assert loop._read_floor_for(_patch("g-lo", (102, 102, 102), t, 0)) == MeasureLoopConfig().min_reads
    # a BRIGHT grey above the dark ceiling is NOT floored by the dark rule
    cfg = MeasureLoopConfig(dark_min_reads=4, dark_floor_max_nits=2.0)
    loop2 = _solo_loop(clean, t, cfg)
    bright = _patch("g-hi", (922, 922, 922), t, 0)
    assert loop2._expected_patch_nits(bright) > 2.0
    assert loop2._read_floor_for(bright) == cfg.min_reads


def test_read_noise_sidecar_computes_se_and_flags_unstable(tmp_path: Path):
    import math
    from dlc.measure_loop import noise_sidecar_path, read_noise_sidecar
    ti3 = tmp_path / "raw.ti3"
    noise_sidecar_path(ti3).write_text(json.dumps({"schema": 1, "by_level": {
        "0.100000": {"chroma_sigma": 0.004, "reads": 4, "unstable": False},
        "0.500000": {"chroma_sigma": 0.002, "reads": 9, "unstable": False},
        "0.050000": {"chroma_sigma": 0.02, "reads": 5, "unstable": True},
        "0.900000": {"chroma_sigma": None, "reads": 1, "unstable": False},
    }}), encoding="utf-8")
    entries = dict(read_noise_sidecar(ti3))
    assert math.isclose(entries[0.1], 0.004 / 2.0)     # SE = σ/√4 (more reads → smaller → more trust)
    assert math.isclose(entries[0.5], 0.002 / 3.0)     # σ/√9
    assert entries[0.05] == math.inf                   # unstable → never trust (don't bake un-holdable)
    assert entries[0.9] is None                        # <2 reads → no spread


def test_match_level_noise_robust_to_ti3_roundtrip():
    from dlc.measure_loop import match_level_noise
    # the .ti3 ×100/÷100 percent roundtrip perturbs a level ~1e-7; nearest-match still finds it,
    # but a level not actually present is NOT matched (tol << gray-level spacing).
    entries = [(0.1, 0.002), (0.5, 0.001)]
    assert match_level_noise(entries, 0.1 + 7e-8) == 0.002
    assert match_level_noise(entries, 0.3) is None


def test_noise_sidecar_records_per_level_chroma_sigma(tmp_path: Path):
    # End-to-end: a warm + NOISY panel over a grey ramp with the dark floor on writes a noise
    # sidecar beside the .ti3 with a positive per-level chromaticity σ (the dark-trust input).
    from dlc.measure_loop import noise_sidecar_path
    t = _sdr()
    panel = SyntheticPanel(transfer=t, start_temp=1.0, cold_blue_gain=1.0, noise=0.04, seed=11)
    ti3 = tmp_path / "raw.ti3"
    run_measure_loop(
        patches=_grey_ramp(t, 10), transfer=t, measure=panel,
        config=MeasureLoopConfig(dark_min_reads=4, dark_floor_max_nits=120.0),
        ti3_path=ti3, ndjson_path=tmp_path / "raw.ndjson")
    sc = noise_sidecar_path(ti3)
    assert sc.exists()
    by_level = json.loads(sc.read_text(encoding="utf-8"))["by_level"]
    assert by_level                                          # neutral levels recorded
    assert any(v["chroma_sigma"] > 0.0 and v["reads"] >= 2 for v in by_level.values())


def test_noise_reads_is_per_round_not_lifetime_after_remeasure(tmp_path: Path):
    # An appended re-measure overwrites the accepted read in place: reads_taken ACCUMULATES (lifetime),
    # but chroma_sigma + noise_reads describe ONLY the final round — so the dark-trust σ/√n divides by
    # the matching per-round n, not the inflated lifetime sum (which would understate noise / over-trust).
    from dlc.measure_loop import noise_sidecar_path, _write_noise_sidecar, read_noise_sidecar
    t = _sdr()
    clean = _ScriptedPanel([(50.0, 50.0, 55.0)])
    cfg = MeasureLoopConfig(dark_min_reads=4, dark_floor_max_nits=120.0)   # gate high ⇒ dim grey floored to 4
    loop = _solo_loop(clean, t, cfg)
    dim = _patch("g-lo", (102, 102, 102), t, 0)
    rec1 = loop.measure_patch(dim, phase="main")
    assert rec1.reads_taken == 4 and rec1.noise_reads == 4
    rec2 = loop.measure_patch(dim, phase="main")          # re-measure SAME label → overwrite in place
    assert rec2 is rec1
    assert rec2.reads_taken == 8                          # lifetime sum across both rounds
    assert rec2.noise_reads == 4                          # final round only — the σ/√n divisor
    # And the sidecar carries the per-round count, so the consumed SE divides by √4, not √8.
    ti3 = tmp_path / "raw.ti3"
    _write_noise_sidecar(ti3, [rec2])
    by_level = json.loads(noise_sidecar_path(ti3).read_text(encoding="utf-8"))["by_level"]
    assert len(by_level) == 1
    assert next(iter(by_level.values()))["reads"] == 4


# ---------------------------------------------------------------------------
# cross-patch read-integrity guard (frozen presenter / mid-run dark panel)
# ---------------------------------------------------------------------------

def test_frozen_presenter_surfaces_a_read_integrity_anomaly():
    # A stuck frame reads fine + repeatably, so the per-read stall clock never trips. Across a window
    # of patches whose commanded luminance SHOULD differ widely yet all read the same frame → flag it.
    t = _sdr()
    stuck = _ScriptedPanel([(56.0, 56.0, 58.0)])   # every patch reads the identical stuck frame
    loop = _solo_loop(stuck, t, MeasureLoopConfig())
    # All mid/bright (no dim, no dark) so NO per-read plausibility fires — isolates the frozen signal.
    for i, cv in enumerate([(512, 512, 512), (1023, 1023, 1023), (724, 724, 724), (880, 880, 880)]):
        loop.measure_patch(_patch(f"p{i}", cv, t, i), phase="main")
    assert loop.measurement_path_compromised is True
    assert [a["reason"] for a in loop.read_anomalies] == ["frozen_presenter"]


def test_varied_reads_do_not_false_positive_frozen():
    # A working panel: each patch reads a luminance that tracks its commanded level ⇒ never frozen.
    t = _sdr()
    panel = SyntheticPanel(transfer=t, start_temp=1.0)
    loop = _solo_loop(panel, t, MeasureLoopConfig())
    for i, cv in enumerate([(512, 512, 512), (1023, 1023, 1023), (724, 724, 724), (880, 880, 880)]):
        loop.measure_patch(_patch(f"p{i}", cv, t, i), phase="main")
    assert all(a["reason"] != "frozen_presenter" for a in loop.read_anomalies)


def test_panel_dark_mid_run_surfaces_anomaly():
    # The panel sleeps mid-pass: lit patches read ~0 with ok=True (the stall clock keeps resetting).
    # A run of consecutive lit-but-dark reads surfaces a distinct panel_dark_mid_run anomaly.
    t = _sdr()
    dark = _ScriptedPanel([(0.0, 0.0, 0.0)])
    loop = _solo_loop(dark, t, MeasureLoopConfig())
    for i in range(4):
        loop.measure_patch(_patch(f"lit{i}", (922, 922, 922), t, i), phase="main")
    assert loop.measurement_path_compromised is True
    assert "panel_dark_mid_run" in {a["reason"] for a in loop.read_anomalies}


def test_read_integrity_disabled_when_window_zero():
    t = _sdr()
    stuck = _ScriptedPanel([(56.0, 56.0, 58.0)])
    loop = _solo_loop(stuck, t, MeasureLoopConfig(integrity_window=0))
    for i, cv in enumerate([(512, 512, 512), (1023, 1023, 1023), (724, 724, 724), (880, 880, 880)]):
        loop.measure_patch(_patch(f"p{i}", cv, t, i), phase="main")
    assert loop.read_anomalies == []


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
    # Digest-sufficiency (fable Phase 8): "would not stabilise" is judgeable only with the
    # numbers next to it — observed SE vs the loop tolerance vs the DIP's expected σ at
    # that luminance, and the reads burned trying.
    detail = {d["label"]: d for d in res.digest["unresolved_detail"]}
    d = detail["p0003"]
    assert d["tolerance_de"] == 0.2
    assert d["dip_expected_sigma_de"] is not None      # the DIP context rides along
    assert d["reads_taken"] >= 6                       # hit the abnormal bound
    assert d["nits"] is not None


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


def test_measure_check_in_reports_deltas_not_cumulative_totals(tmp_path: Path):
    """A check-in is "what happened since I last looked", not a restatement of the running
    totals. Across the quartile check-ins the ``since_last`` reads must PARTITION the run (sum
    to the final cumulative count, none double-counted) and the ``*_total`` fields are the only
    cumulative carry."""
    from dlc.events import RunLog, read_events, digest_projection

    t = _sdr()
    panel = SyntheticPanel(transfer=t, white_nits=120.0)
    epath = tmp_path / "e.jsonl"
    runlog = RunLog(epath, phase="measure:post-mhc")
    run_measure_loop(patches=_grey_ramp(t, 16), transfer=t, measure=panel,
                     config=MeasureLoopConfig(), runlog=runlog,
                     ti3_path=tmp_path / "m.ti3", ndjson_path=tmp_path / "m.ndjson")

    check_ins = [e for e in digest_projection(read_events(epath)) if e.event == "check_in"]
    assert check_ins, "expected quartile check-ins"
    # The per-window read deltas are disjoint and sum to the last check-in's running total.
    deltas = [e.data["since_last"]["reads"] for e in check_ins]
    assert all(d >= 0 for d in deltas)
    assert sum(deltas) == check_ins[-1].data["reads_total"]
    # The delta block carries no cumulative restatement; totals live only in the *_total fields.
    for e in check_ins:
        assert set(e.data["since_last"]) <= {"reads", "anomalies", "drift_episodes", "became_warm"}
        assert "reads_total" in e.data and "drift_episodes_total" in e.data


def test_measure_loop_wall_clock_backstop_emits_beyond_quartiles(tmp_path: Path, monkeypatch):
    """A slow / measure-only stage must still surface periodic check-ins when the 3 progress
    quartiles are sparse: the §12 wall-clock backstop (``checkin_interval_s``) fires EMIT-ONLY
    so a long run is never 0 check-ins. (Default interval 0 → backstop off → only quartiles,
    per the companion test.)"""
    import dlc.measure_loop as ml
    from dlc.events import RunLog, read_events, digest_projection

    # A monotonic clock that advances a fixed step every call, so the wall-clock floor is
    # crossed repeatedly during the main pass without real waiting. _now() uses datetime, so
    # only the backstop logic reads time.monotonic — making this deterministic.
    clock = {"t": 0.0}
    monkeypatch.setattr(ml.time, "monotonic", lambda: clock.__setitem__("t", clock["t"] + 60.0) or clock["t"])

    t = _sdr()
    panel = SyntheticPanel(transfer=t, white_nits=120.0)
    epath = tmp_path / "e.jsonl"
    runlog = RunLog(epath, phase="measure:raw")
    run_measure_loop(patches=_grey_ramp(t, 16), transfer=t, measure=panel,
                     config=MeasureLoopConfig(), runlog=runlog,
                     ti3_path=tmp_path / "m.ti3", ndjson_path=tmp_path / "m.ndjson",
                     checkin_interval_s=120.0)

    check_ins = [e for e in digest_projection(read_events(epath)) if e.event == "check_in"]
    assert len(check_ins) > 3                                       # backstop fired beyond the 3 quartiles
    assert {0.25, 0.5, 0.75} <= {round(e.data["progress"], 2) for e in check_ins}  # quartiles still present


def test_wall_clock_backstop_ticks_during_warmup_too(tmp_path: Path, monkeypatch):
    """NO-DARK-WINDOW rule (fable Phase 8): the wall-clock backstop lives on the loop's
    single read funnel (_read), so warm-up — before ANY patch is accepted and before the
    main pass's quartile arm exists — ticks the §12 clock too. (The same backstop hook
    also fires per preheat/rewarm soak block, whose reads bypass _read.)"""
    import dlc.measure_loop as ml
    from dlc.events import RunLog, read_events, digest_projection

    clock = {"t": 0.0}
    monkeypatch.setattr(ml.time, "monotonic",
                        lambda: clock.__setitem__("t", clock["t"] + 60.0) or clock["t"])

    t = _sdr()
    panel = SyntheticPanel(transfer=t, warm_tau=0.06)   # cold start → several warm-up reads
    epath = tmp_path / "e.jsonl"
    runlog = RunLog(epath, phase="measure:raw")
    run_measure_loop(patches=_grey_ramp(t, 8), transfer=t, measure=panel,
                     config=MeasureLoopConfig(), runlog=runlog,
                     ti3_path=tmp_path / "m.ti3", ndjson_path=tmp_path / "m.ndjson",
                     checkin_interval_s=120.0)
    check_ins = [e for e in digest_projection(read_events(epath)) if e.event == "check_in"]
    # at least one packet was emitted with ZERO accepted patches — i.e. during warm-up
    assert any(e.data.get("patches_done") == 0 for e in check_ins)


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


# ---------------------------------------------------------------------------
# Dark-panel guard: a mid-grey reference reading ~no light (asleep/off) must NOT
# "settle" on black and then meter for minutes — it is caught in a couple of reads.
# ---------------------------------------------------------------------------

def _dark(patch):
    return Reading(xyz=(0.0, 0.0, 0.0), ok=True)   # panel asleep/off — emits ~no light


def test_dark_panel_is_detected_and_skips_the_main_pass(tmp_path: Path):
    t = _sdr()
    res = run_measure_loop(patches=_grey_ramp(t, 8), transfer=t, measure=_dark,
                           config=MeasureLoopConfig(), ndjson_path=tmp_path / "m.ndjson")
    assert res.digest["panel_dark"] is True
    assert res.digest["dark_reference_nits"] == 0.0
    assert res.warm is False
    assert res.patch_count == 0                     # main pass skipped — no black data measured
    assert res.needs_adjudication is True
    assert "DARK" in (res.question or "")
    # caught fast: a couple of warm-up reads, not the full max_warmup_reads or a metered ramp
    assert res.digest["warmup_reads"] <= MeasureLoopConfig().dark_required + 1


def test_hdr_midcode_one_nit_is_detected_as_dark_path(tmp_path: Path):
    # Regression from the live HDR MHC run: a PQ mid-code warmup patch [~512,512,532]
    # reading ~1 nit is not a dim-but-valid HDR panel. The guard must scale with the
    # expected warmup luminance, not only the fixed 1 cd/m² floor.
    t = Transfer.pq(bit_depth=10)

    def barely_lit(patch):
        return Reading(xyz=(1.0, 1.02, 1.0), yxy=(1.02, 0.33, 0.34), ok=True)

    res = run_measure_loop(patches=_grey_ramp(t, 8), transfer=t, measure=barely_lit,
                           config=MeasureLoopConfig(), ndjson_path=tmp_path / "m.ndjson")
    assert res.digest["panel_dark"] is True
    assert res.digest["read_anomaly"] is True
    assert "panel_dark" in res.digest["anomaly_reasons"]
    assert res.digest["dark_reference_nits"] == 1.02
    assert res.patch_count == 0
    assert res.needs_adjudication is True


def test_compromised_preheat_surfaces_but_measurement_continues(tmp_path: Path):
    t = _sdr()
    # Frozen luminance/chroma across different soak patches is exactly what the thermal
    # controller calls compromised: wrong colorspace, frozen presenter, or meter issue.
    panel = _ScriptedPanel([(5.0, 5.0, 5.0)] * 64)
    res = run_measure_loop(
        patches=_grey_ramp(t, 8), transfer=t, measure=panel,
        config=MeasureLoopConfig(preheat="always"),
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.digest["preheat_compromised"] is True
    assert res.digest["read_anomaly"] is True
    assert "preheat_compromised" in res.digest["anomaly_reasons"]
    assert res.digest["needs_adjudication"] is True
    assert res.patch_count == 8
    assert "COMPROMISED" in (res.question or "")
    assert "measurement continued" in (res.question or "")


def test_low_drive_patch_reading_bright_flags_measurement_path(tmp_path: Path):
    t = _sdr()

    def incoherent(patch):
        if patch.role == "warmup":
            return Reading(xyz=(25.0, 26.0, 27.0), yxy=(26.0, 0.31, 0.33), ok=True)
        return Reading(xyz=(19.0, 20.0, 21.0), yxy=(20.0, 0.31, 0.33), ok=True)

    ti3 = tmp_path / "m.ti3"
    res = run_measure_loop(
        patches=_grey_ramp(t, 8),
        transfer=t,
        measure=incoherent,
        config=MeasureLoopConfig(),
        ti3_path=ti3,
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.digest["measurement_path_compromised"] is True
    assert res.digest["read_anomaly"] is True
    assert "measurement_path_compromised" in res.digest["anomaly_reasons"]
    assert res.digest["read_anomalies"][0]["reason"] == "low_drive_high_luminance"
    assert res.patch_count == 8
    assert ti3.exists()
    assert "plausible luminance envelope" in (res.question or "")


def test_lit_patch_reading_dark_flags_measurement_path(tmp_path: Path):
    t = _sdr()

    def incoherent(patch):
        if patch.role == "warmup":
            return Reading(xyz=(25.0, 26.0, 27.0), yxy=(26.0, 0.31, 0.33), ok=True)
        return Reading(xyz=(0.2, 0.5, 0.3), yxy=(0.5, 0.31, 0.33), ok=True)

    res = run_measure_loop(
        patches=[(t.max_cv, t.max_cv, t.max_cv)],
        transfer=t,
        measure=incoherent,
        config=MeasureLoopConfig(),
        ndjson_path=tmp_path / "m.ndjson",
    )
    assert res.digest["measurement_path_compromised"] is True
    assert res.digest["read_anomalies"][0]["reason"] == "lit_drive_low_luminance"
    assert res.patch_count == 1
    assert res.needs_adjudication is True


def test_a_dim_but_lit_panel_is_not_flagged_dark(tmp_path: Path):
    # No false positives: a real (if dim) mid-grey reads far above the 1 cd/m² floor.
    t = _sdr()
    panel = SyntheticPanel(transfer=t, warm_tau=0.06)
    res = run_measure_loop(patches=_grey_ramp(t, 8), transfer=t, measure=panel,
                           config=MeasureLoopConfig(), ndjson_path=tmp_path / "m.ndjson")
    assert res.digest["panel_dark"] is False
    assert res.patch_count == 8


def test_dark_guard_disabled_at_floor_zero_measures_the_ramp(tmp_path: Path):
    # floor 0 turns the guard off (escape hatch) — the loop runs as before.
    t = _sdr()
    res = run_measure_loop(patches=_grey_ramp(t, 6), transfer=t, measure=_dark,
                           config=MeasureLoopConfig(dark_floor_nits=0.0), ndjson_path=tmp_path / "m.ndjson")
    assert res.digest["panel_dark"] is False


# ---------------------------------------------------------------------------
# re-measure must never destroy a previously accepted read (fable audit F3-1)
# ---------------------------------------------------------------------------

class _DiesAfterFirstRead:
    """One clean read, then the meter is gone (every read fails)."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self, patch: MeasurePatch) -> Reading:
        self.n += 1
        if self.n <= 1:
            return Reading(xyz=(50.0, 50.0, 55.0), yxy=(50.0, 0.31, 0.33), ok=True)
        return Reading(xyz=None, ok=False, error="meter died")


def test_failed_remeasure_round_keeps_the_prior_accepted_read():
    # An appended re-measure round where the meter dies must NOT erase the previously
    # accepted read: a cold-but-real value beats a sentinel hole that silently drops the
    # patch from the .ti3. The failure is still loud — the record flags unstable, which
    # lands the label in `unresolved` for adjudication.
    t = _sdr()
    loop = _solo_loop(_DiesAfterFirstRead(), t, MeasureLoopConfig())
    p = _patch("p0", (512, 512, 512), t, 0)
    rec = loop.measure_patch(p, phase="main")
    assert rec.usable is True and rec.xyz == (50.0, 50.0, 55.0)

    rec2 = loop.measure_patch(p, phase="remeasure", disposition="appended")
    assert rec2 is rec
    assert rec2.usable is True                      # prior value retained → stays in the .ti3
    assert rec2.xyz == (50.0, 50.0, 55.0)
    assert rec2.unstable is True                    # ...but loudly flagged for adjudication
    assert "retained" in (rec2.note or "")
    assert rec2.reads_taken > 1                     # the failed round's reads still count


def test_fresh_patch_with_no_usable_read_is_still_a_sentinel_hole():
    # The F3-1 preservation only applies when there IS prior data: a patch whose very
    # first round produces nothing usable remains an unusable hole (kept off the .ti3).
    t = _sdr()

    class _AlwaysDead:
        def __call__(self, patch: MeasurePatch) -> Reading:
            return Reading(xyz=None, ok=False, error="dead")

    loop = _solo_loop(_AlwaysDead(), t, MeasureLoopConfig())
    rec = loop.measure_patch(_patch("p0", (512, 512, 512), t, 0), phase="main")
    assert rec.usable is False and rec.unstable is True


# ---------------------------------------------------------------------------
# drift-ref self-perturbation guard (2026-08-14 HDR grayscale-wb run, defect 3)
# ---------------------------------------------------------------------------

def _live_edited_panel(transfer: Transfer, gains: dict):
    """A perfect warm panel whose light passes through a mutable per-channel SIGNAL
    gain table — the live grayscale-editor tweak the touch-up flow mutates between
    reads. Every read (measurement AND drift reference) renders through it, exactly
    like the real preview shader."""
    from dataclasses import replace as _replace

    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0)

    def measure(patch: MeasurePatch) -> Reading:
        g = gains["rgb"]
        sig = tuple(min(1.0, s * gi) for s, gi in zip(patch.signal, g))
        return panel(_replace(patch, signal=sig))

    return measure


def _incremental_session(transfer: Transfer, measure, *, reference_guard=None):
    return IncrementalMeasureSession(
        patches=_grey_ramp(transfer, 9),
        transfer=transfer,
        measure=measure,
        config=MeasureLoopConfig(neutral_interval=2, preheat="never"),
        reference_guard=reference_guard,
    )


def test_live_edit_perturbs_drift_ref_without_guard():
    # The failure mode on record (2026-08-14 HDR run): the drift-reference patch renders
    # THROUGH the live editor table, so a chromatic nudge between reads moved the
    # reference and read as an 'excursion' drift episode → measurement_compromised.
    t = _sdr()
    gains = {"rgb": (1.0, 1.0, 1.0)}
    session = _incremental_session(t, _live_edited_panel(t, gains))
    session.start()
    session.measure_index(4)
    gains["rgb"] = (1.07, 1.0, 1.0)     # tune the mid grey: +7% red differential
    session.measure_index(5)            # interval-2 → neutral checkpoint fires here
    digest = session.finish()
    assert digest["drift_episodes"] >= 1          # the FALSE episode
    assert digest["needs_adjudication"] is True


def test_reference_guard_shields_drift_ref_from_live_edit():
    # Same edit, but reference reads run under the caller's fixed-display-state guard
    # (identity table) — the edit can no longer masquerade as panel drift.
    from contextlib import contextmanager

    t = _sdr()
    gains = {"rgb": (1.0, 1.0, 1.0)}

    @contextmanager
    def identity_guard():
        saved = gains["rgb"]
        gains["rgb"] = (1.0, 1.0, 1.0)
        try:
            yield
        finally:
            gains["rgb"] = saved

    session = _incremental_session(t, _live_edited_panel(t, gains),
                                   reference_guard=identity_guard)
    session.start()
    session.measure_index(4)
    gains["rgb"] = (1.07, 1.0, 1.0)
    session.measure_index(5)
    # the guard restores the live table after each reference read
    assert gains["rgb"] == (1.07, 1.0, 1.0)
    digest = session.finish()
    assert digest["drift_episodes"] == 0
    assert digest["needs_adjudication"] is False
