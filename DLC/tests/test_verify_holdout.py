"""Held-out verify (V1), per-unique-signal verify stats (V2) and the stated scoring white (V3).

Why (adversarial review of the PA32UCXR SDR run 20261002_012945): the verify headline was partly
IN-SAMPLE — 141 unique signals behind 309 reads, 77 within 1 code of a training signal, and the 28
repeated sweep signals (7 reads each, all training, on-lattice) carried 63 % of the read-weighted
mean. These tests pin the mechanics that make that visible: repeats count once (V2), every verify
signal is classified against the run's training set in signal AND drive space (V1), fresh
held-out draws are deterministic per run and provably away from training (V1b), the gate only
leans on a held-out bucket big enough to mean something, and the digest states the white the ΔE is
relative to (V3).
"""

from __future__ import annotations

import json
import types
from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import metrics as M
from dlc import verify_holdout as vh
from dlc.calibrate import Calibration, PatchSizes, StageOutcome
from dlc.engine.patches import Transfer
from dlc.metrics import PatchMetric
from dlc.patch_sets import (_saturation_sweep_bookend, build_verify_set, flow_patch_counts,
                            insert_held_out_draws)

_SDR = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
_HDR = Transfer.pq(bit_depth=10)


def _pm(rgb, de, *, target=(30.0, 40.0, 20.0), grey=False, clamped=False) -> PatchMetric:
    return PatchMetric(tuple(rgb), (1.0, 1.0, 1.0), target, de, grey, gamut_clamped=clamped)


# ---------------------------------------------------------------------------
# V2 — per unique signal
# ---------------------------------------------------------------------------

def test_per_signal_counts_repeats_once_and_keeps_the_read_weighted_buckets():
    # one signal read 7x at 0.1 (the sweep shape) + three single-read signals at 1.0
    rows = [_pm((0.25, 0.0, 0.0), 0.1) for _ in range(7)]
    rows += [_pm((0.5, 0.1, 0.1), 1.0), _pm((0.1, 0.5, 0.1), 1.0), _pm((0.1, 0.1, 0.5), 1.0)]
    p = M.practical_summary(rows, is_hdr=False)
    assert p["core"]["avg"] == round((0.7 + 3.0) / 10, 3) and p["core"]["n"] == 10   # unchanged
    per = p["per_signal"]
    assert per["n_signals"] == 4 and per["n_reads"] == 10
    assert per["core"]["avg"] == round((0.1 + 3.0) / 4, 3) and per["core"]["n"] == 4
    assert per["overall"]["avg"] == per["core"]["avg"]


def test_per_signal_de_is_the_mean_of_the_signals_reads_and_groups_float_noise():
    rows = [_pm((0.5, 0.5, 0.5), 0.1, grey=True), _pm((0.50000004, 0.5, 0.5), 0.3, grey=True),
            _pm((0.2, 0.3, 0.4), 2.0)]
    groups = M.group_per_signal(rows)
    assert [n for _m, n in groups] == [2, 1]
    assert groups[0][0].de2000 == pytest.approx(0.2)
    per = M.per_signal_summary(rows, is_hdr=False)
    assert per["tube"]["n"] == 1 and per["tube"]["avg"] == 0.2


def test_per_signal_zones_follow_the_shared_classifier_on_hdr():
    rows = [_pm((0.3, 0.3, 0.3), 1.0, grey=True), _pm((0.3, 0.3, 0.3), 3.0, grey=True),
            _pm((0.9, 0.0, 0.0), 9.0, clamped=True)]
    per = M.practical_summary(rows, is_hdr=True, gamut_aware=True)["per_signal"]
    assert per["core"]["n"] == 1 and per["core"]["avg"] == 2.0
    assert per["clamped"]["n"] == 1 and per["clamped"]["avg"] == 9.0


# ---------------------------------------------------------------------------
# V1 — classification
# ---------------------------------------------------------------------------

def _sig(*codes, max_cv=1023):
    return np.asarray(codes, dtype=float) / max_cv


def test_classification_coincident_near_held_out_and_strict():
    training = _sig((500, 300, 100), (700, 700, 700))
    verify = _sig((500, 300, 100),        # the training signal itself
                  (503, 300, 100),        # 3 codes away
                  (520, 300, 100),        # 20 codes away, off-lattice
                  (256, 256, 256))        # far from training but ON a 33-node lattice node (8/32)
    rows = vh.classify_signals(verify, max_cv=1023, training=training)
    assert [r["class"] for r in rows] == ["coincident", "near", "held_out", "held_out"]
    assert [r["d_in"] for r in rows][:3] == [0.0, 3.0, 20.0]
    assert rows[2]["strict_held_out"] is True and rows[2]["on_lattice"] is False
    assert rows[3]["on_lattice"] is True and rows[3]["strict_held_out"] is False
    assert all(r["d_drive"] is None and r["drive"] is None for r in rows)   # no cube: signal space only


def test_classification_drive_space_catches_a_signal_driven_onto_a_probe_drive():
    training = _sig((100, 100, 900))
    probes = np.array([[400, 410, 420]])
    verify = _sig((600, 610, 620), (800, 50, 50))
    # the cube drives the first verify signal exactly onto a probe drive; the second far from all
    drives = _sig((400, 410, 421), (900, 20, 20))
    rows = vh.classify_signals(verify, max_cv=1023, training=training, probe_drives=probes,
                               cube=np.zeros((2, 2, 2, 3)), sample=lambda cube, s: drives)
    assert rows[0]["d_in"] > 4 and rows[0]["d_drive"] == 1.0 and rows[0]["class"] == "coincident"
    assert rows[1]["class"] == "held_out" and rows[1]["drive"] == [900, 20, 20]
    # the probe drives are TRAINING in signal space too (they are folded into the cube's model)
    rows = vh.classify_signals(_sig((402, 410, 420)), max_cv=1023, training=training, probe_drives=probes)
    assert rows[0]["class"] == "near" and rows[0]["d_in"] == 2.0


def test_classification_uses_the_cube_grid_for_the_lattice_and_the_runs_bit_depth():
    # 9-node cube: lattice coordinates k/8 → code 127.5k at 10 bits; (128,128,128) is on a node
    rows = vh.classify_signals(_sig((128, 128, 128)), max_cv=1023, cube=np.zeros((9, 9, 9, 3)),
                               sample=lambda cube, s: s)
    assert rows[0]["on_lattice"] is True
    # 8-bit codes: the same signal distance is ~4x fewer codes than at 10 bits
    t8 = np.asarray([[100, 100, 100]]) / 255.0
    v8 = np.asarray([[104, 100, 100]]) / 255.0
    assert vh.classify_signals(v8, max_cv=255, training=t8)[0]["d_in"] == 4.0


def test_held_out_summary_buckets_per_signal_over_the_core_population():
    rows = [{"class": "held_out", "strict_held_out": True, "de": 0.5, "zone": "core", "draw": True},
            {"class": "held_out", "strict_held_out": False, "de": 0.3, "zone": "core"},
            {"class": "coincident", "strict_held_out": False, "de": 0.1, "zone": "core"},
            {"class": "held_out", "strict_held_out": True, "de": 9.0, "zone": "clamped"}]
    s = vh.held_out_summary(rows, thresholds={"x": 1})
    assert s["available"] and s["n_signals"] == 3 and s["thresholds"] == {"x": 1}
    assert s["held_out"] == {"avg": 0.4, "p95": pytest.approx(0.49), "max": 0.5, "n": 2}
    assert s["strict_held_out"]["n"] == 1 and s["coincident"]["avg"] == 0.1 and s["near"]["n"] == 0
    assert s["fresh_draws"]["n"] == 1 and s["fresh_draws"]["held_out"] == 1


def test_training_loader_reads_every_non_verify_ti3_and_the_probe_ledger_plus_events(tmp_path: Path):
    meas = tmp_path / "measurements"
    meas.mkdir()
    ti3 = ("CTI3\nBEGIN_DATA_FORMAT\nRGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\nEND_DATA_FORMAT\n"
           "BEGIN_DATA\n{}\nEND_DATA\n")
    (meas / "post_mhc.ti3").write_text(ti3.format("50 50 50 10 10 10"), encoding="utf-8")
    (meas / "raw.ti3").write_text(ti3.format("100 0 0 20 10 1"), encoding="utf-8")
    (meas / "verify.ti3").write_text(ti3.format("25 25 25 3 3 3"), encoding="utf-8")
    (meas / "post_mhc.ti3.orig").write_text(ti3.format("1 1 1 1 1 1"), encoding="utf-8")
    tr = vh.load_training(tmp_path)
    assert sorted(s["file"] for s in tr["sources"]) == ["post_mhc.ti3", "raw.ti3"]
    assert sorted(map(tuple, tr["signals"].tolist())) == [(0.5, 0.5, 0.5), (1.0, 0.0, 0.0)]
    vh.append_probe_drives(meas / vh.PROBES_FILE, [[1, 2, 3], [4, 5, 6]])
    ev = {"event": "patch_read", "data": {"role": "probe", "rgb": [7, 8, 9], "ok": True}}
    bad = {"event": "patch_read", "data": {"role": "probe", "rgb": [9, 9, 9], "ok": False}}
    (tmp_path / "events.jsonl").write_text(json.dumps(ev) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    drives, info = vh.load_probe_drives(tmp_path)
    assert sorted(map(tuple, drives.tolist())) == [(1, 2, 3), (4, 5, 6), (7, 8, 9)]
    assert info["ledger_rows"] == 2 and info["event_rows"] == 1 and info["unique"] == 3


def test_training_context_is_honest_about_a_verify_only_run(tmp_path: Path):
    ctx = vh.training_context(tmp_path, {"flow": "verify-only"}, max_cv=1023)
    assert ctx["available"] is False and "verify-only" in ctx["reason"]
    empty = vh.training_context(tmp_path, {"flow": "full"}, max_cv=1023)
    assert empty["available"] is False and "no training" in empty["reason"]


# ---------------------------------------------------------------------------
# V1b — fresh held-out draws
# ---------------------------------------------------------------------------

def _draw(seed, **kw):
    base = dict(max_cv=1023, value_floor_cv=256, exclude_codes=None, existing=())
    base.update(kw)
    return vh.draw_held_out_signals(24, seed=seed, **base)


def test_draws_are_deterministic_per_run_id_and_differ_between_runs():
    assert vh.run_seed("20261002_012945_x") == vh.run_seed("20261002_012945_x")
    assert vh.run_seed("run_a") != vh.run_seed("run_b")
    a1 = _draw(vh.run_seed("run_a"))
    a2 = _draw(vh.run_seed("run_a"))
    b = _draw(vh.run_seed("run_b"))
    assert a1 == a2 and a1["n_drawn"] == 24
    assert a1["signals"] != b["signals"]


def _crosstalk_cube(n: int = 17) -> np.ndarray:
    """A real, NON-identity 3D LUT ([b, g, r] layout): channel crosstalk + a gain/offset, so a
    signal's drive differs from the signal by tens of codes (what a calibration cube does)."""
    ax = np.linspace(0.0, 1.0, n)
    b, g, r = np.meshgrid(ax, ax, ax, indexing="ij")
    rgb = np.stack([r, g, b], axis=-1)
    mix = np.array([[0.90, 0.07, 0.03], [0.04, 0.92, 0.04], [0.02, 0.06, 0.92]])
    return np.clip(rgb @ mix.T * 0.96 + 0.015, 0.0, 1.0)


def test_draws_respect_every_exclusion_and_spread_value_and_saturation():
    from dlc.optimize import sample_cube   # the production sampler the draw itself uses

    rng = np.random.default_rng(3)
    excl = rng.integers(0, 1024, size=(800, 3))
    existing = [tuple(int(c) for c in row) for row in rng.integers(256, 1024, size=(100, 3))]
    cube = _crosstalk_cube()
    rec = _draw(7, exclude_codes=excl, existing=existing, cube=cube)
    draws = np.asarray(rec["signals"])
    assert rec["n_drawn"] == 24 and rec["drive_space_checked"] is True
    drives = vh.to_codes(sample_cube(cube, draws / 1023.0), 1023)
    assert np.median(np.abs(drives - draws).max(axis=1)) > 8          # the cube really moves them
    assert np.all(vh.min_chebyshev(draws, excl) >= vh.DRAW_MIN_CODES)   # signal space
    assert np.all(vh.min_chebyshev(drives, excl) >= vh.DRAW_MIN_CODES)  # drive space, through the cube
    assert not set(map(tuple, draws.tolist())) & set(existing)
    assert np.all(vh.lattice_distance_codes(draws / 1023.0, 17, 1023) > vh.LATTICE_CODES)
    assert np.all(draws.max(axis=1) >= 256) and np.all(draws.max(axis=1) <= 1023)
    for i in range(len(draws)):   # the draws keep their distance from each other too
        others = np.delete(draws, i, axis=0)
        assert vh.min_chebyshev(draws[i:i + 1], others)[0] >= vh.DRAW_MIN_CODES
    sat = (draws.max(axis=1) - draws.min(axis=1)) / draws.max(axis=1)
    assert sat.min() < 0.5 < sat.max()                      # half-saturated AND saturated colours
    assert draws.max(axis=1).min() < 600                    # dim colours too, not just the shell
    # every draw classifies held-out against that same training set through that same cube
    rows = vh.classify_signals(draws / 1023.0, max_cv=1023, probe_drives=excl, cube=cube)
    assert all(r["class"] == "held_out" and r["strict_held_out"] for r in rows)
    assert [r["drive"] for r in rows] == drives.tolist()


def test_draws_are_hue_stratified():
    import colorsys

    for seed in (1, 2, 3, 4):
        rec = _draw(seed)
        assert rec["per_hue_sextant"] == {k: 4 for k in ("R-Y", "Y-G", "G-C", "C-B", "B-M", "M-R")}
        hues = [colorsys.rgb_to_hsv(*(np.asarray(c) / 1023.0))[0] for c in rec["signals"]]
        counts = np.bincount(np.minimum((np.asarray(hues) * 6).astype(int), 5), minlength=6)
        assert counts.min() >= 3, counts   # rounding to codes may nudge a draw over a sextant edge
    odd = vh.draw_held_out_signals(8, seed=5, max_cv=1023, value_floor_cv=256)
    assert sorted(odd["per_hue_sextant"].values()) == [1, 1, 1, 1, 2, 2]


def test_draws_in_drive_space_reject_signals_whose_cube_drive_lands_on_training():
    excl = np.array([[512, 512, 512]])
    onto = lambda cube, s: np.full_like(s, 512 / 1023.0)   # noqa: E731 - every drive onto training
    rec = _draw(1, exclude_codes=excl, cube=np.zeros((2, 2, 2, 3)), sample=onto, max_attempts=200)
    assert rec["n_drawn"] == 0 and rec["rejected"].get("near_training_drive", 0) > 0


def test_plan_counts_include_the_draws_sdr_only():
    ps = PatchSizes()
    base = len(build_verify_set(ps, _SDR))
    sdr = flow_patch_counts("full", ps, _SDR)
    assert sdr["stages"]["verify"] == base + 24 and sdr["verify_held_out_draws"] == 24
    assert flow_patch_counts("verify-only", ps, _SDR)["stages"]["verify"] == base + 24
    assert "verify_held_out_draws" not in flow_patch_counts("full", ps, _HDR)
    off = flow_patch_counts("full", replace(ps, verify_held_out_draws=0), _SDR)
    assert off["stages"]["verify"] == base and "verify_held_out_draws" not in off
    # flows whose verify is not the QC set carry no draws
    assert "verify_held_out_draws" not in flow_patch_counts("refine-mhc", ps, _SDR)


def test_draws_are_spread_through_the_core_and_the_bookends_stay_put():
    ps = PatchSizes()
    base = build_verify_set(ps, _SDR)
    span = len(_saturation_sweep_bookend(ps, _SDR))
    draws = [(900, 100, 300), (300, 700, 450), (612, 612, 100)]
    out = insert_held_out_draws(base, draws, ps, _SDR)
    assert len(out) == len(base) + 3
    assert out[:span] == base[:span] and out[-span:] == base[-span:]
    core = out[span:-span]
    assert [p for p in core if p not in draws] == base[span:-span]        # core order untouched
    pos = sorted(core.index(d) for d in draws)
    assert pos[0] > 0 and pos[-1] < len(core) - 1 and pos[1] - pos[0] > 10   # spread, not bunched


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def _summary(white=0.5):
    return types.SimpleNamespace(avg_de2000=0.4, p95_de2000=1.0, max_de2000=1.5, white_de2000=white)


def _q(avg=0.5):
    return types.SimpleNamespace(avg_de2000=avg, p95_de2000=3.0, max_de2000=5.0, white_de2000=2.0)


def _practical(held_n, held_avg, *, available=True):
    core_rw = {"avg": 0.45, "p95": 1.6, "max": 1.7, "n": 309}
    per_core = {"avg": 0.30, "p95": 1.4, "max": 1.7, "n": 141}
    return {"core": core_rw, "tube": {"avg": 0.33, "n": 45, "p95": 0.6, "max": 0.7},
            "per_signal": {"n_signals": 141, "n_reads": 309, "core": per_core,
                           "tube": {"avg": 0.25, "n": 21, "p95": 0.5, "max": 0.7}},
            "held_out": ({"available": True, "held_out": {"avg": held_avg, "p95": 1.0, "max": 1.7,
                                                          "n": held_n}}
                         if available else {"available": False, "reason": "verify-only builds nothing"})}


def test_gate_scores_core_and_tube_per_signal_and_records_the_basis():
    within, basis = Calibration._quality_gate(_summary(), _practical(64, 0.32), _q())
    assert within is True
    scored = basis["scored"]
    assert scored["basis"] == "per_signal" and scored["core_avg"] == 0.30 and scored["tube_avg"] == 0.25
    assert scored["n_signals"] == 141 and scored["read_weighted_core_avg"] == 0.45
    # no per_signal block (an older caller): the read-weighted buckets, said so
    legacy = {k: v for k, v in _practical(64, 0.32).items() if k != "per_signal"}
    _w, basis = Calibration._quality_gate(_summary(), legacy, _q())
    assert basis["scored"]["basis"] == "read_weighted" and basis["scored"]["core_avg"] == 0.45


def test_gate_leans_on_held_out_only_with_enough_signals():
    within, basis = Calibration._quality_gate(_summary(), _practical(8, 0.6), _q(avg=0.5))
    assert within is False and basis["checks"]["held_out_avg"] is False
    assert basis["held_out_gate"]["gated"] is True and basis["basis"].endswith("held-out avg (V1)")
    within, basis = Calibration._quality_gate(_summary(), _practical(8, 0.4), _q(avg=0.5))
    assert within is True and basis["checks"]["held_out_avg"] is True
    # below n 8: reported, never gated — and the reason is recorded
    within, basis = Calibration._quality_gate(_summary(), _practical(7, 9.9), _q(avg=0.5))
    assert within is True and "held_out_avg" not in basis["checks"]
    assert basis["held_out_gate"] == {"gated": False, "reason": "held-out n 7 < 8", "held_out_avg": 9.9,
                                      "held_out_n": 7, "min_n": 8}
    _w, basis = Calibration._quality_gate(_summary(), _practical(0, None, available=False), _q())
    assert basis["held_out_gate"]["gated"] is False and "verify-only" in basis["held_out_gate"]["reason"]


def test_severe_failure_judges_the_gates_per_signal_basis():
    calib = object.__new__(Calibration)
    practical = {"core": {"avg": 25.0, "p95": 30.0, "max": 40.0, "n": 300},     # repeats of one wreck
                 "per_signal": {"core": {"avg": 2.0, "p95": 5.0, "max": 40.0, "n": 140}}}
    digest = {"metric": "CIEDE2000", "white_de2000": 1.0, "practical": practical,
              "gate": {"basis": "practical core+tube+white (D3)", "scored": {"basis": "per_signal"}}}
    out = StageOutcome("verify", "done", digest=digest, data={"within_quality": False})
    assert Calibration._severe_verify_failure(calib, out) is False
    digest["gate"]["scored"]["basis"] = "read_weighted"
    assert Calibration._severe_verify_failure(calib, out) is True


# ---------------------------------------------------------------------------
# V3 — the scoring white, stated
# ---------------------------------------------------------------------------

def test_scored_white_evidence_states_the_measured_white_and_its_offset():
    from dlc.mhc import Ti3Sample
    samples = [Ti3Sample((1.0, 1.0, 1.0), (95.0, 100.4, 109.0)),
               Ti3Sample((1.0, 1.0, 1.0), (95.0, 100.2, 109.0)),
               Ti3Sample((0.5, 0.5, 0.5), (20.0, 21.0, 23.0))]
    _m, lum = M.score_samples(samples)
    ev = Calibration._scored_white_evidence(samples, lum, 100.0)
    assert ev["scored_white_nits"] == 100.4 == round(lum, 4)
    assert ev["scored_white_source"]["kind"] == "measured" and ev["scored_white_source"]["n_white_reads"] == 2
    assert ev["scored_white_source"]["mean_white_nits"] == 100.3
    assert ev["white_luminance_vs_calibrated_pct"] == 0.4
    assert Calibration._scored_white_evidence(samples, lum, None)["white_luminance_vs_calibrated_pct"] is None


# ---------------------------------------------------------------------------
# end to end (synthetic SDR full flow)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sdr_full(tmp_path_factory):
    from test_calibrate import _RecordingAuto, _make
    adj = _RecordingAuto()
    calib = _make(tmp_path_factory.mktemp("holdout"), "holdout_full", adjudicator=adj)
    result = calib.run("full")
    assert result.status == "completed", result.digest
    return calib, adj


def test_full_flow_verify_digest_carries_per_signal_held_out_draws_and_white(sdr_full):
    calib, adj = sdr_full
    v = calib.calib["stages"]["verify"]["digest"]
    per = v["practical"]["per_signal"]
    assert v["per_signal_avg"] == per["overall"]["avg"] and v["n_signals"] == per["n_signals"]
    assert v["n_reads"] == v["patch_count"] > v["n_signals"]
    held = v["held_out"]
    assert held is v["practical"]["held_out"] or held == v["practical"]["held_out"]
    assert held["available"] is True and held["training"]["drive_space"] is True
    assert {s["file"] for s in held["training"]["sources"]} >= {"post_mhc.ti3", "raw.ti3"}
    assert held["training"]["n_probe_drives"] > 0
    assert held["held_out"]["n"] + held["near"]["n"] + held["coincident"]["n"] == held["n_signals"]
    draws = v["held_out_draws"]
    assert draws["seed"] == vh.run_seed(calib.ctx.root.name) and draws["run_id"] == calib.ctx.root.name
    assert draws["n_drawn"] == 24 == draws["n_measured"] == len(draws["signals"])
    assert held["fresh_draws"]["n"] == 24 and held["fresh_draws"]["held_out"] == 24
    assert v["gate"]["scored"]["basis"] == "per_signal" and v["gate"]["held_out_gate"]["gated"] is True
    sw = v["sdr_white"]
    assert sw["scored_white_source"]["kind"] == "measured" and sw["scored_white_source"]["n_white_reads"] > 0
    scored = json.loads((calib.ctx.root / "reports" / "verification_iter00_metrics.json")
                        .read_text(encoding="utf-8"))
    assert sw["scored_white_nits"] == round(scored["target_luminance"], 4)   # the white the ΔE used
    assert sw["white_luminance_vs_calibrated_pct"] is not None
    # the per-signal rows are persisted for the judge (run-relative path)
    rows = json.loads((calib.ctx.root / v["held_out_rows"]).read_text(encoding="utf-8"))["rows"]
    assert len(rows) == v["n_signals"] and sum(r["draw"] for r in rows) == 24
    # the build probe ledger exists, every row tagged with the live build attempt the classification read
    ledger = [json.loads(line) for line in (calib.ctx.root / "measurements" / vh.PROBES_FILE)
              .read_text(encoding="utf-8").splitlines()]
    attempt = calib.calib["stages"]["build-install-3dlut"]["data"]["probe_attempt"]
    assert ledger and {row["attempt"] for row in ledger} == {attempt}
    assert held["training"]["probe_drives"][calib.ctx.root.name]["scope"].startswith(f"build attempt {attempt}")
    # the seam leads with the per-signal numbers and quotes held-out next to them
    q = next(r.question for r in adj.requests if r.key == "verify:accept")
    assert q.index("per-signal over") < q.index("held-out avg") < q.index("read-weighted overall avg")
    plan = calib.calib["patch_plan"]
    assert plan["verify_held_out_draws"] == 24
    assert calib.calib["stages"]["measure:verify"]["digest"]["patch_count"] == plan["stages"]["verify"]
    # the headline now includes the draws: the preset-only (run-to-run comparable) numbers ride beside
    preset = v["preset_set"]
    assert preset["available"] and preset["draw_reads_excluded"] == 24
    assert preset["n_reads"] == v["patch_count"] - 24 and preset["n_signals"] == v["n_signals"] - 24
    base = M.score_samples([s for s in __import__("dlc.mhc", fromlist=["parse_ti3"]).parse_ti3(
        calib.ctx.root / "measurements" / "verify.ti3")
        if tuple(int(round(c * 1023)) for c in s.rgb) not in {tuple(d) for d in draws["signals"]}],
        gamma=2.2, white_xy=tuple(v["target_white_xy"]))[0]
    assert preset["avg_de2000"] == round(sum(m.de2000 for m in base) / len(base), 3)
    assert f"preset set without the 24 draw reads: read-weighted avg {preset['avg_de2000']}" in q


def test_draws_are_memoised_and_never_drawn_for_an_already_measured_verify(sdr_full):
    calib, _adj = sdr_full
    memo = calib.calib["verify_held_out_draws"]
    assert calib._held_out_draw_record(calib._verify_patches()) is memo
    calib.calib.pop("verify_held_out_draws")
    try:   # measure:verify is done: an upgraded resume must not claim draws it never measured
        assert calib._held_out_draw_record(calib._verify_patches()) is None
    finally:
        calib.calib["verify_held_out_draws"] = memo


def test_score_cli_reproduces_the_live_held_out_view(sdr_full):
    from argparse import Namespace

    from dlc.stages import score as score_stage
    calib, _adj = sdr_full
    v = calib.calib["stages"]["verify"]["digest"]
    args = Namespace(stage="verify", iteration=1, source_ti3=str(calib.ctx.root / "measurements" / "verify.ti3"),
                     gamma=2.2, luminance=None, target_white_xy=None, level_edge="run", mode="SDR",
                     run=calib.ctx.root)
    res = score_stage.build(args, calib.ctx)
    assert res.metrics["practical"]["held_out"] == v["practical"]["held_out"]
    assert res.metrics["practical"]["per_signal"] == v["practical"]["per_signal"]


# Two REAL plan records approved by pre-V1 code (dlc_state.json['calib']['patch_plan'] of PA32UCXR
# runs; the fingerprints were computed by that code). The fresh draws must not move them.
_PA_SDR_PLAN_20261002_012945 = {
    "flow": "3dlut-only", "fingerprint": "9313a6d1b7d46445",
    "patch_sizes": {
        "raw_ramp_steps": 32, "raw_saturations": [1.0], "raw_include_secondaries": False,
        "raw_spacing": "uniform", "raw_color_min_nits": 1.0, "icc_tube_levels": 0,
        "icc_tube_offsets": [0.06, 0.15], "volumetric_mode": "cube", "cube_size": 7, "tube_size": 33,
        "tube_radius": 2, "grid_type": "cub", "spines": False, "gamut_lum_steps": 17, "gamut_hues": 12,
        "gamut_lum_bias": 1.3, "verify_steps": 13, "verify_saturations": [1.0, 0.5],
        "verify_color_min_signal": 0.25, "saturation_sweep_levels": [0.25, 0.5, 0.75, 1.0],
        "saturation_sweep_repeats": 3, "neutral_steps": 17, "low_light_steps": 9,
        "low_light_cube_size": 5, "low_light_signal": 0.2, "low_light_bias": 2.0, "order": "thermal"}}
_PA_HDR_VERIFY_ONLY_PLAN_20261002_143710 = {
    "flow": "verify-only", "fingerprint": "7ba114567aad1918", "patch_max_cv": 830, "n_patches": 303,
    "verify_source": {"run": "H:\\Projects\\DesktopLUT\\DLC\\runs\\20260924_132412_307436_hdr_asus_proart_pa32ucxr",
                      "patches_fingerprint": "e910d0936cb964d7", "patch_source": "ndjson"},
    "patch_sizes": {
        "raw_ramp_steps": 32, "raw_saturations": [1.0], "raw_include_secondaries": False,
        "raw_spacing": "uniform", "raw_color_min_nits": 1.0, "icc_tube_levels": 0,
        "icc_tube_offsets": [0.06, 0.15], "volumetric_mode": "tube", "cube_size": 9, "tube_size": 33,
        "tube_radius": 2, "grid_type": "cub", "spines": False, "gamut_lum_steps": 17, "gamut_hues": 12,
        "gamut_lum_bias": 1.3, "verify_steps": 13, "verify_saturations": [1.0, 0.5],
        "verify_color_min_signal": 0.25, "saturation_sweep_levels": [0.25, 0.5, 0.75, 1.0],
        "saturation_sweep_repeats": 3, "neutral_steps": 17, "low_light_steps": 9,
        "low_light_cube_size": 5, "low_light_signal": 0.2, "low_light_bias": 2.0, "order": "thermal"}}


def test_a_pre_v1_approved_sdr_plan_keeps_its_recorded_fingerprint(tmp_path: Path):
    from test_calibrate import _make
    recorded = _PA_SDR_PLAN_20261002_012945
    calib = _make(tmp_path, "fp_pa", patch_sizes=PatchSizes.from_dict(recorded["patch_sizes"]), bit_depth=10)
    calib.target_name = "srgb_g22"
    rec = calib._patch_plan_record(recorded["flow"])
    # the record COUNTS the draws (the run will measure them)...
    assert rec["stages"] == {"post-mhc": 753, "verify": 309 + 24} and rec["verify_held_out_draws"] == 24
    # ...but its identity is the one the pre-V1 code approved: the in-flight run resumes approved
    assert rec["fingerprint"] == recorded["fingerprint"]
    calib.patch_sizes = replace(calib.patch_sizes, verify_held_out_draws=12)   # a real plan change
    rec2 = calib._patch_plan_record(recorded["flow"])
    assert rec2["patch_sizes"]["verify_held_out_draws"] == 12
    assert rec2["fingerprint"] != recorded["fingerprint"]


def test_a_pre_v1_approved_hdr_verify_only_plan_keeps_its_recorded_fingerprint(tmp_path: Path):
    from test_calibrate import _make
    recorded = _PA_HDR_VERIFY_ONLY_PLAN_20261002_143710
    calib = _make(tmp_path, "fp_hdr", mode="HDR", patch_sizes=PatchSizes.from_dict(recorded["patch_sizes"]),
                  bit_depth=10)
    calib.target_name = "rec2020_pq"
    calib._patch_max_cv = lambda: recorded["patch_max_cv"]
    calib.calib["flow"] = "verify-only"
    calib.calib["stages"]["verify-source"] = {"status": "done", "data": {
        **recorded["verify_source"], "patches": [[0, 0, 0]] * recorded["n_patches"]}}
    rec = calib._patch_plan_record("verify-only")
    assert rec["stages"] == {"verify": 303} and "verify_held_out_draws" not in rec
    assert "verify_held_out_draws" not in rec["patch_sizes"]
    assert rec["fingerprint"] == recorded["fingerprint"]


def test_a_resume_with_the_memo_cleared_redraws_the_identical_set_and_a_changed_training_supersedes_it(sdr_full):
    """The memo is not what makes a resume reproducible — the run-id seed + the training are: a fresh
    process with the memo gone re-draws the identical list. When the training the draws were drawn
    against changes (a re-built cube's probe attempt, a re-plan), the memo is superseded."""
    import copy

    from test_calibrate import _make
    calib, _adj = sdr_full
    memo = copy.deepcopy(calib.calib["verify_held_out_draws"])
    resumed = _make(calib.ctx.root.parent, calib.ctx.root.name)        # reopen the run (a new process)
    measured = copy.deepcopy(resumed.calib["stages"]["measure:verify"])
    try:
        resumed.calib.pop("verify_held_out_draws")
        resumed.calib["stages"]["measure:verify"] = {**measured, "status": "running"}   # not measured yet
        redrawn = resumed._held_out_draw_record(resumed._verify_patches())
        assert redrawn["signals"] == memo["signals"] and redrawn["training_key"] == memo["training_key"]
        assert "superseded" not in redrawn
        # the training changes (here: three of the draws become probe drives of a re-built cube)
        changed = resumed._held_out_training()
        changed["probe_drives"] = np.vstack([changed["probe_drives"], np.asarray(memo["signals"][:3])])
        resumed._held_out_training = lambda: changed
        again = resumed._held_out_draw_record(resumed._verify_patches())
        assert again["training_key"] != memo["training_key"]
        assert again["superseded"]["training_key"] == memo["training_key"]
        assert not set(map(tuple, again["signals"])) & set(map(tuple, memo["signals"][:3]))
        # a MEASURED verify keeps exactly what it measured, whatever the training does afterwards
        resumed.calib["stages"]["measure:verify"] = measured
        assert resumed._held_out_draw_record(resumed._verify_patches()) is resumed.calib["verify_held_out_draws"]
    finally:
        resumed.calib["verify_held_out_draws"] = memo
        resumed.calib["stages"]["measure:verify"] = measured
        resumed._save()


def test_an_adaptive_replan_drops_the_draws_with_the_training_it_discards(tmp_path: Path):
    from test_calibrate import _make
    calib = _make(tmp_path, "replan_draws", adaptive_planning=True)
    calib.stage_resolve_target()
    calib.calib["flow"] = "full"
    calib.calib["verify_held_out_draws"] = {"signals": [[1, 2, 3]], "training_key": "old"}
    calib.calib["adaptive_plan"] = {"fingerprint": "OLD-FINGERPRINT"}
    calib.stage_adaptive_planning(raw_ti3=None)
    assert "verify_held_out_draws" not in calib.calib


def test_probe_ledger_reads_only_the_live_build_attempt(tmp_path: Path):
    ledger = tmp_path / "measurements" / vh.PROBES_FILE
    vh.append_probe_drives(ledger, [[1, 2, 3]], attempt=1)                 # a superseded build attempt
    vh.append_probe_drives(ledger, [[4, 5, 6], [7, 8, 9]], attempt=2)      # the live one
    ev = {"event": "patch_read", "data": {"role": "probe", "rgb": [10, 11, 12], "ok": True}}
    (tmp_path / "events.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")
    live, info = vh.load_probe_drives(tmp_path, attempt=2)
    assert sorted(map(tuple, live.tolist())) == [(4, 5, 6), (7, 8, 9)]
    assert info["superseded_rows"] == 1 and info["scope"].startswith("build attempt 2")
    # no live tag (a pre-ledger run): every recorded read, events included — a conservative superset
    every, info = vh.load_probe_drives(tmp_path)
    assert len(every) == 4 and "superset" in info["scope"]
    # a live tag with no row of its own also falls back to the superset, said so
    _rows, info = vh.load_probe_drives(tmp_path, attempt=3)
    assert "left no ledger row" in info["scope"]
    calib = {"flow": "full", "stages": {"build-install-3dlut": {"status": "done", "data": {"probe_attempt": 2}}}}
    ctx = vh.training_context(tmp_path, calib, max_cv=1023)
    assert sorted(map(tuple, ctx["probe_drives"].tolist())) == [(4, 5, 6), (7, 8, 9)]
    assert vh.training_key(ctx, max_cv=1023) == vh.training_key(vh.training_context(tmp_path, calib, max_cv=1023),
                                                                max_cv=1023)
    calib["stages"]["build-install-3dlut"]["data"]["probe_attempt"] = 1
    assert vh.training_key(vh.training_context(tmp_path, calib, max_cv=1023), max_cv=1023) \
        != vh.training_key(ctx, max_cv=1023)


def test_policy_advice_judges_the_gates_own_buckets():
    """stages._common.policy_advice claims the live gate's basis — it reads the same view
    (metrics.practical_gate_view): per-signal core/tube and the held-out avg at n >= 8."""
    from dlc.decisions import MetricThresholds
    from dlc.stages._common import policy_advice

    th = MetricThresholds(avg_de2000=0.4, p95_de2000=3.0, max_de2000=5.0, white_de2000=2.0)
    summary = _summary()
    for practical in (_practical(64, 0.32),          # per-signal core 0.30 passes; read-weighted 0.45 would not
                      _practical(8, 0.6),            # held-out gated and over
                      _practical(7, 9.9),            # held-out too small: reported, not gated
                      {k: v for k, v in _practical(64, 0.32).items() if k != "per_signal"}):   # legacy shape
        within, basis = Calibration._quality_gate(summary, practical, th)
        advice = policy_advice({"avg_de2000": summary.avg_de2000, "p95_de2000": summary.p95_de2000,
                                "max_de2000": summary.max_de2000, "white_de2000": summary.white_de2000,
                                "practical": practical}, thresholds=th)
        assert (advice["default_policy_verdict"] == "stop") is within, (practical, advice)
        assert basis["scored"]["basis"].replace("_", "-") in advice["reasons"][0]
    advice = policy_advice({"avg_de2000": 0.4, "p95_de2000": 1.0, "max_de2000": 1.5, "white_de2000": 0.5,
                            "practical": _practical(8, 0.6)}, thresholds=th)
    assert any("held_out_avg_de2000=0.600>0.400" in r for r in advice["reasons"])


# ---------------------------------------------------------------------------
# HDR: the gate is per-signal core/tube + held-out too; the build writes the ledger; no draws yet
# ---------------------------------------------------------------------------

def test_hdr_gate_judges_per_signal_core_not_oog_and_gates_held_out():
    q = types.SimpleNamespace(avg_de2000=3.0, p95_de2000=6.0, max_de2000=10.0, white_de2000=4.0)
    practical = {"gamut_aware": True,
                 "core": {"avg": 3.4, "p95": 5.0, "max": 9.0, "n": 200},      # repeats of one bad patch
                 "tube": {"avg": 1.4, "p95": 4.1, "max": 7.5, "n": 99},
                 "limits": {"avg": 8.1, "p95": 30.0, "max": 30.2, "n": 103},
                 "clamped": {"avg": 9.9, "p95": 62.8, "max": 62.9, "n": 114},
                 "per_signal": {"n_signals": 150, "n_reads": 417,
                                "core": {"avg": 1.1, "p95": 2.4, "max": 9.0, "n": 86},
                                "tube": {"avg": 1.3, "p95": 4.0, "max": 7.5, "n": 40},
                                "limits": {"avg": 8.0, "p95": 29.0, "max": 30.2, "n": 30},
                                "clamped": {"avg": 9.0, "p95": 60.0, "max": 62.9, "n": 34}},
                 "held_out": {"available": True, "held_out": {"avg": 3.3, "p95": 6.0, "max": 9.0, "n": 12}}}
    within, basis = Calibration._quality_gate(_summary(white=1.0), practical, q)
    assert basis["scored"]["basis"] == "per_signal" and basis["scored"]["core_avg"] == 1.1
    assert basis["checks"]["core_avg"] is True                         # read-weighted 3.4 would fail
    assert basis["checks"]["held_out_avg"] is False and within is False   # held-out over target gates it
    practical["held_out"]["held_out"]["n"] = 7
    within, basis = Calibration._quality_gate(_summary(white=1.0), practical, q)
    assert within is True and basis["held_out_gate"]["gated"] is False


@pytest.fixture(scope="module")
def hdr_full(tmp_path_factory):
    from test_calibrate import _RecordingAuto, _make, _perfect_hdr_panel
    adj = _RecordingAuto()
    calib = _make(tmp_path_factory.mktemp("holdout_hdr"), "holdout_hdr_full", mode="HDR",
                  panel=_perfect_hdr_panel(), bit_depth=10, adjudicator=adj)
    result = calib.run("full")
    assert result.status == "completed", result.digest
    return calib, adj


def test_hdr_full_flow_gates_per_signal_and_held_out_writes_the_ledger_and_draws_nothing(hdr_full):
    calib, adj = hdr_full
    v = calib.calib["stages"]["verify"]["digest"]
    assert v["metric"] == "dE_ITP"
    gate = v["gate"]
    per = v["practical"]["per_signal"]
    assert gate["scored"]["basis"] == "per_signal" and gate["scored"]["core_avg"] == per["core"]["avg"]
    held = v["held_out"]
    assert held["available"] is True and held["training"]["drive_space"] is True
    assert held["n_signals"] == per["core"]["n"]                       # the gate's population: core signals
    # The synthetic Rec.2020 verify has only ~21 CORE signals, almost all on training: the held-out
    # bucket is too small to gate here — reported with the reason (the n >= 8 HDR gating path is
    # pinned by test_hdr_gate_judges_per_signal_core_not_oog_and_gates_held_out).
    n_ho = held["held_out"]["n"]
    assert gate["held_out_gate"]["gated"] is (n_ho >= 8) and ("held_out_avg" in gate["checks"]) is (n_ho >= 8)
    if n_ho < 8:
        assert gate["held_out_gate"]["reason"] == f"held-out n {n_ho} < 8"
    # no fresh draws on HDR (follow-up): none drawn, none planned, nothing excluded from the preset
    assert "held_out_draws" not in v and "verify_held_out_draws" not in calib.calib
    assert "verify_held_out_draws" not in calib.calib["patch_plan"]
    assert v["preset_set"]["draw_reads_excluded"] == 0 and v["preset_set"]["avg_de2000"] == v["avg_de2000"]
    # the HDR build wrote the tagged probe ledger the classification read
    rows = [json.loads(line) for line in (calib.ctx.root / "measurements" / vh.PROBES_FILE)
            .read_text(encoding="utf-8").splitlines()]
    attempt = calib.calib["stages"]["build-install-3dlut"]["data"]["probe_attempt"]
    assert rows and {r["attempt"] for r in rows} == {attempt}
    q = next(r.question for r in adj.requests if r.key == "verify:accept")
    assert "per-signal over" in q and "held-out avg" in q
    assert ("reported, not gated" in q) is (n_ho < 8)
