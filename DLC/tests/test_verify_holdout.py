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


def test_draws_respect_every_exclusion_and_spread_value_and_saturation():
    rng = np.random.default_rng(3)
    excl = rng.integers(0, 1024, size=(800, 3))
    existing = [tuple(int(c) for c in row) for row in rng.integers(256, 1024, size=(100, 3))]
    identity = lambda cube, s: s   # noqa: E731 - the drive IS the signal
    rec = _draw(7, exclude_codes=excl, existing=existing, cube=np.zeros((33, 33, 33, 3)), sample=identity)
    draws = np.asarray(rec["signals"])
    assert rec["n_drawn"] == 24 and rec["drive_space_checked"] is True
    assert np.all(vh.min_chebyshev(draws, excl) >= vh.DRAW_MIN_CODES)
    assert not set(map(tuple, draws.tolist())) & set(existing)
    assert np.all(vh.lattice_distance_codes(draws / 1023.0, 33, 1023) > vh.LATTICE_CODES)
    assert np.all(draws.max(axis=1) >= 256) and np.all(draws.max(axis=1) <= 1023)
    for i in range(len(draws)):   # the draws keep their distance from each other too
        others = np.delete(draws, i, axis=0)
        assert vh.min_chebyshev(draws[i:i + 1], others)[0] >= vh.DRAW_MIN_CODES
    sat = (draws.max(axis=1) - draws.min(axis=1)) / draws.max(axis=1)
    assert sat.min() < 0.5 < sat.max()                      # half-saturated AND saturated colours
    assert draws.max(axis=1).min() < 600                    # dim colours too, not just the shell
    # every draw classifies held-out against that same training set (drive space included)
    rows = vh.classify_signals(draws / 1023.0, max_cv=1023, probe_drives=excl,
                               cube=np.zeros((33, 33, 33, 3)), sample=identity)
    assert all(r["class"] == "held_out" and r["strict_held_out"] for r in rows)


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
    # the build probe ledger exists and is what the classification read
    assert (calib.ctx.root / "measurements" / vh.PROBES_FILE).is_file()
    # the seam leads with the per-signal numbers and quotes held-out next to them
    q = next(r.question for r in adj.requests if r.key == "verify:accept")
    assert q.index("per-signal over") < q.index("held-out avg") < q.index("read-weighted overall avg")
    plan = calib.calib["patch_plan"]
    assert plan["verify_held_out_draws"] == 24
    assert calib.calib["stages"]["measure:verify"]["digest"]["patch_count"] == plan["stages"]["verify"]


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


def test_default_draw_knob_keeps_an_approved_plan_fingerprint(tmp_path: Path):
    import hashlib

    from test_calibrate import _make
    calib = _make(tmp_path, "fp_draws")
    calib.stage_resolve_target()
    rec = calib._patch_plan_record("full")
    assert rec["verify_held_out_draws"] == 24 and "verify_held_out_draws" not in rec["patch_sizes"]
    # the identity a pre-V1 build hashed: the same record without the draws in its counts
    legacy = {k: v for k, v in rec.items() if k not in ("fingerprint", "verify_held_out_draws")}
    legacy["stages"] = {**legacy["stages"], "verify": legacy["stages"]["verify"] - 24}
    legacy["total_patches"] -= 24
    payload = json.dumps(legacy, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    assert rec["fingerprint"] == hashlib.sha256(payload).hexdigest()[:16]
    calib.patch_sizes = replace(calib.patch_sizes, verify_held_out_draws=12)
    rec2 = calib._patch_plan_record("full")
    assert rec2["patch_sizes"]["verify_held_out_draws"] == 12 and rec2["fingerprint"] != rec["fingerprint"]
