"""Tests for the content-weighted practical score (:mod:`dlc.content_score` + the ``content_weighted``
block of :func:`dlc.metrics.practical_summary`) — EVIDENCE ONLY, never a gate.

Pinned here (no hardware, synthetic fixtures — never the owner's data in the repo):

* the kernel score is the study's §5.1 definition — it reproduces a transcription of the reference
  ``score.py`` (``kernel_field`` + ``score``) on a small fixture;
* the study's npz layout decodes to the right ITP bin centres + labels, and the compact JSON export
  round-trips to the identical score;
* per-patch content weights carry through (Σ w·E / Σ w over unique signals, n, out-of-gamut and
  weak-evidence shares, the unmeasured weight);
* the block LEADS the practical summary when present, and is absent otherwise (unchanged shape);
* opt-in: on the recorded D1 HDR run with the owner's ``content_hist_hdr_live.npz`` it reproduces the
  study's 1.68 dE_ITP / 40.7 % gap at R 20 (skipped when the local data is absent).
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from dlc import calibration_profile as cp
from dlc import content_score as cs
from dlc.metrics import (ContentWeights, PatchMetric, ReadEvidence, practical_summary, signal_key)

_DLC = Path(__file__).resolve().parents[1]
_STUDY = Path(os.environ.get("DLC_PRACTICAL_STUDY", _DLC / "results" / "practical_score_2026-10-09"))
_RUNS = Path(os.environ.get("DLC_PRACTICAL_STUDY_RUNS", _DLC / "runs"))
_D1 = "20261002_145601_781557_hdr_asus_proart_pa32ucxr"


def _content(rng, n=3000) -> cs.ContentDistribution:
    itp = np.column_stack([rng.uniform(0.05, 0.6, n), rng.normal(0.0, 0.02, n), rng.normal(0.0, 0.02, n)])
    w = rng.uniform(0.0, 1.0, n) ** 2
    return cs.ContentDistribution(name="fixture", variant="json", itp=itp, w=w, source="mem", fingerprint="x",
                                  zone=rng.integers(0, 3, n), band=rng.integers(0, 5, n), tube=rng.integers(0, 2, n))


def _reference_score(content_itp, w, loc, err, reach):
    """A transcription of the study's score.py ``kernel_field`` + ``score`` (score_covered, gap, fallback)."""
    from scipy.spatial import cKDTree

    sigma = reach / 2.0
    ct = cKDTree(content_itp * 720.0)
    num = np.zeros(len(content_itp))
    den = np.zeros(len(content_itp))
    for lo, e in zip(loc * 720.0, err):
        ids = np.asarray(ct.query_ball_point(lo, reach), dtype=int)
        if ids.size == 0:
            continue
        d = np.linalg.norm(content_itp[ids] * 720.0 - lo, axis=1)
        k = np.exp(-0.5 * (d / sigma) ** 2)
        np.add.at(num, ids, k * e)
        np.add.at(den, ids, k)
    cov = den > 0
    est = np.where(cov, num / np.where(cov, den, 1.0), np.nan)
    _dn, jn = cKDTree(loc * 720.0).query(content_itp * 720.0)
    est_fb = np.where(cov, est, err[jn])
    return {"score": round(float(np.sum(w[cov] * est[cov]) / w[cov].sum()), 3),
            "gap": round(100 * float(w[~cov].sum() / w.sum()), 2),
            "fallback": round(float(np.sum(w * est_fb) / w.sum()), 3)}


@pytest.mark.parametrize("reach", [5.0, 10.0, 20.0])
def test_kernel_score_reproduces_the_reference_definition(reach):
    rng = np.random.default_rng(7)
    content = _content(rng)
    loc = np.column_stack([rng.uniform(0.05, 0.6, 25), rng.normal(0, 0.03, 25), rng.normal(0, 0.03, 25)])
    err = rng.uniform(0.2, 4.0, 25)
    got = cs.kernel_score(content, loc, err, reach=reach)
    ref = _reference_score(content.itp, content.w, loc, err, reach)
    assert got["score"] == ref["score"]
    assert got["coverage_gap_pct"] == ref["gap"]
    assert got["score_with_nearest_fallback"] == ref["fallback"]
    assert got["sigma_dEITP"] == reach / 2.0
    # breakdowns partition the mass; contributions sum to ~100 %
    assert abs(sum(b["mass_pct"] for b in got["zones"].values()) - 100.0) < 0.05
    assert abs(sum(b["contribution_pct"] or 0 for b in got["bands"].values()) - 100.0) < 0.5


def test_kernel_score_reports_evidence_shares_and_top_contributors():
    rng = np.random.default_rng(3)
    content = _content(rng)
    loc = np.column_stack([np.linspace(0.06, 0.58, 12), np.zeros(12), np.zeros(12)])
    err = np.array([5.0] + [0.5] * 11)
    info = [{"rgb": [i / 12] * 3, "weak": i == 0, "single_read": i == 0, "at_floor": i == 0, "low_snr": False,
             "single_read_at_floor": i == 0} for i in range(12)]
    got = cs.kernel_score(content, loc, err, reach=20.0, sig_info=info)
    weak = got["evidence"]["weak"]
    assert weak["n_signals"] == 1 and weak["score_share_pct"] > weak["content_share_pct"]
    assert got["top_contributors"][0]["rgb"] == [0.0, 0.0, 0.0] and got["top_contributors"][0]["weak"] is True
    # an error-free set reports no (noise) score shares
    flat = cs.kernel_score(content, loc, np.zeros(12), reach=20.0, sig_info=info)
    assert flat["score"] == 0.0 and "evidence" not in flat


def test_npz_layout_decodes_and_json_export_round_trips(tmp_path: Path):
    shape = (30, 400, 200, 200)
    # three bins: (lab, i, t, p) → flat index into [N_LAB, NI, NT, NP]
    bins = [(0, 10, 100, 100), (13, 200, 50, 150), (25, 399, 0, 199)]
    idx = np.array([((lab * shape[1] + i) * shape[2] + t) * shape[3] + p for lab, i, t, p in bins], np.int64)
    path = tmp_path / "content_hist_tiny.npz"
    np.savez(path, main_itp_idx=idx, main_itp_w=np.array([0.5, 0.3, 0.2]), desk_itp_idx=idx[:2],
             desk_itp_w=np.array([0.6, 0.4]), shape_itp=np.array(shape), ITP_STEP_I=0.0025, ITP_STEP_TP=0.003,
             TP_MAX=0.3, n_titles=3)
    d = cs.load_content_distribution(path)
    assert d.name == "tiny" and d.variant == "main" and d.meta["n_titles"] == 3
    assert np.allclose(d.itp[0], [(10 + 0.5) * 0.0025, -0.3 + 100.5 * 0.003, -0.3 + 100.5 * 0.003])
    assert d.zone.tolist() == [0, 1, 2] and d.band.tolist() == [0, 1, 2] and d.tube.tolist() == [0, 1, 1]
    assert cs.load_content_distribution(path, content_mode="SDR").variant == "desk"     # the desktop path
    assert cs.load_content_distribution(f"{path}#main", content_mode="SDR").variant == "main"
    with pytest.raises(ValueError):
        cs.load_content_distribution(f"{path}#nope")
    out = tmp_path / "tiny.json"
    cs.export_content_json(d, out)
    back = cs.load_content_distribution(out)
    assert back.name == "tiny" and np.allclose(back.itp, d.itp, atol=1e-5) and np.allclose(back.w, d.w)
    loc = np.array([[0.03, 0.0, 0.0], [0.5, -0.15, 0.15]])
    a = cs.kernel_score(d, loc, [1.0, 2.0], reach=20.0)
    b = cs.kernel_score(back, loc, [1.0, 2.0], reach=20.0)
    assert (a["score"], a["coverage_gap_pct"]) == (b["score"], b["coverage_gap_pct"])


def test_xyz_to_itp_is_bt2100_ictcp_with_half_ct():
    # D65 white at 100 nit: neutral → T = P = 0; I = PQ(100 nit) on every LMS channel
    w = np.array([[95.047, 100.0, 108.883]])
    itp = cs.xyz_to_itp(w)[0]
    assert abs(itp[1]) < 1e-3 and abs(itp[2]) < 1e-3
    assert abs(itp[0] - float(cs._pq_oetf(np.array([100.0]))[0])) < 2e-3


def _pm(rgb, de, *, y=50.0, clamped=False, grey=False) -> PatchMetric:
    return PatchMetric(tuple(rgb), (y * 0.95, y, y * 1.09), (y * 0.95, y, y * 1.09), de, grey, gamut_clamped=clamped)


def test_practical_summary_carries_content_weights_through():
    a, b, c, d = (0.1, 0.1, 0.1), (0.5, 0.4, 0.4), (0.9, 0.2, 0.1), (0.3, 0.3, 0.3)
    metrics = [_pm(a, 4.0, y=0.01, grey=True), _pm(a, 2.0, y=0.01, grey=True),     # one signal, two rows
               _pm(b, 1.0), _pm(c, 6.0, clamped=True), _pm(d, 9.0)]                 # d: no weight
    weights = {signal_key(a): 0.5, signal_key(b): 0.3, signal_key(c): 0.1,
               signal_key((0.7, 0.7, 0.7)): 0.1}                                     # never measured
    ev = ReadEvidence(reads={signal_key(a): 1, signal_key(b): 3, signal_key(c): 2, signal_key(d): 2})
    out = practical_summary(metrics, is_hdr=True,
                            content_weights=ContentWeights(weights, label="file", coverage_gap_pct={"reach_20": 7.0}),
                            read_evidence=ev)
    assert next(iter(out)) == "content_weighted"                       # it LEADS the practical block
    pw = out["content_weighted"]["patch_weights"]
    expect = (0.5 * 3.0 + 0.3 * 1.0 + 0.1 * 6.0) / 0.9                 # E_a = mean(4, 2) = 3
    assert pw["score"] == round(expect, 3) and pw["n"] == 3
    assert pw["weight_unmeasured_share"] == 0.1 and pw["coverage_gap_pct_as_drawn"] == 7.0
    assert pw["weight_share"]["out_of_gamut"] == round(0.1 / 0.9, 4)
    # a: one meter read below the 0.05-nit fallback floor — the near-black single read the study warned of
    assert pw["weight_share"]["single_read_at_floor"] == round(0.5 / 0.9, 4)
    assert pw["score_share"]["weak"] == round(1.5 / (0.5 * 3.0 + 0.3 + 0.6), 4)
    head = out["content_weighted"]["headline"]
    assert head["score"] == pw["score"] and head["coverage_gap_pct"] == 7.0 and "file" in head["label"]
    assert out["content_weighted"]["evidence"]["n_single_read_at_floor"] == 1
    # the zones are unchanged underneath
    assert out["core"]["n"] + out["limits"]["n"] + out["clamped"]["n"] == len(metrics)


def test_practical_summary_without_content_is_unchanged():
    metrics = [_pm((0.2, 0.2, 0.2), 1.0, grey=True), _pm((0.5, 0.3, 0.3), 2.0)]
    out = practical_summary(metrics, is_hdr=False)
    assert "content_weighted" not in out and next(iter(out)) == "gamut_aware"


def test_practical_summary_kernel_block_for_an_sdr_set():
    rng = np.random.default_rng(11)
    # SDR: signals located at their scored target (absolute nits, gamma 2.2 at 116 nit)
    metrics = []
    for v in np.linspace(0.05, 1.0, 12):
        y = 116.0 * v ** 2.2
        metrics.append(PatchMetric((v, v, v), (0.9505 * y, y, 1.089 * y), (0.9505 * y, y, 1.089 * y),
                                   float(rng.uniform(0.2, 1.5)), True))
    content = _content(rng)
    out = practical_summary(metrics, is_hdr=False, content=[content])
    cw = out["content_weighted"]
    assert cw["metric"] == "CIEDE2000" and cw["headline"]["class"] == "fixture"
    res = cw["classes"]["fixture"]
    assert res["score"] == cw["headline"]["score"] and res["coverage_gap_pct"] == cw["headline"]["coverage_gap_pct"]
    assert res["provenance"]["source"] == "mem"
    json.dumps(out, allow_nan=False)                                  # strict-JSON safe (metrics artifact)


def test_profile_content_distribution_key(tmp_path: Path):
    raw = {"HDR": ["hist/content_hist_hdr_live.npz"], "sdr": "C:/abs/content_hist_sdr_live.npz#desk"}
    parsed = cp._content_distribution(raw, tmp_path / "calibration_profile.yaml")
    assert parsed["HDR"] == (str(tmp_path.resolve() / "hist" / "content_hist_hdr_live.npz"),)
    assert parsed["SDR"][0].endswith("content_hist_sdr_live.npz#desk")
    prof = replace(cp.Profile.synthetic(), content_distribution=parsed)
    assert prof.content_distribution_for("hdr") == parsed["HDR"]
    assert replace(prof, content_distribution={"*": ("x.json",)}).content_distribution_for("SDR") == ("x.json",)
    assert cp.Profile.synthetic().content_distribution_for("HDR") == ()


@pytest.mark.slow
def test_recorded_d1_run_reproduces_the_study_score():
    """Opt-in: the owner's recorded D1 HDR run + HDR live-action histogram (local, gitignored) →
    the study's content-weighted 1.68 dE_ITP with a 40.7 % coverage gap at R 20 (rescored_runs.json)."""
    hist = _STUDY / "content_hist_hdr_live.npz"
    run = _RUNS / _D1
    rescored = _STUDY / "rescored_runs.json"
    if not (hist.is_file() and rescored.is_file() and (run / "reports" / "verification_iter00_patch_metrics.json").is_file()):
        pytest.skip("local study data / recorded run absent (set DLC_PRACTICAL_STUDY / DLC_PRACTICAL_STUDY_RUNS)")
    ref = json.loads(rescored.read_text(encoding="utf-8"))["runs"]["D1_daily_cube"]["classes"]["hdr_live"]["reach_20_detail"]
    got = cs.rescore_run(run, [str(hist)], reach=20.0)["content_weighted"]
    res = got["classes"]["hdr_live"]
    assert res["score"] == ref["score_covered"] == 1.68
    assert res["coverage_gap_pct"] == ref["coverage_gap_pct"]
    assert res["score_with_nearest_fallback"] == ref["score_with_nearest_fallback"]
    assert res["p95_covered"] == ref["p95_covered"]
    assert res["signals_with_no_content_within_reach"] == ref["patches_with_no_content_within_reach"]
    # the near-black reads that carry the number are visible as weak evidence
    assert res["evidence"]["at_floor"]["score_share_pct"] > 40.0
    assert got["headline"]["score"] == 1.68


def test_noise_aware_variant_flags_noise_limited_signals_and_corrects_in_quadrature():
    a, b, c = (0.05, 0.05, 0.05), (0.5, 0.5, 0.5), (0.6, 0.3, 0.3)
    y = 0.2      # above the meter floor (0.05 nit fallback): at/below it a spread is no noise estimate
    # a: 4 dark reads scattered far more than its scored E → noise-limited; b: tight repeats → not;
    # c: a single read → no noise evidence ("noise unknown")
    reads_a = [(y * 0.95, y, y * 1.09), (y * 1.4, y * 0.8, y * 0.7), (y * 0.6, y * 1.3, y * 1.6), (y, y, y)]
    reads_b = [(47.5, 50.0, 54.5), (47.51, 50.01, 54.49)]
    metrics = [PatchMetric(a, tuple(sum(r[i] for r in reads_a) / 4 for i in range(3)), (y * 0.95, y, y * 1.09),
                           0.5, True),
               PatchMetric(b, (47.5, 50.0, 54.5), (47.5, 50.0, 54.5), 0.8, True),
               PatchMetric(c, (30.0, 20.0, 15.0), (30.0, 20.0, 15.0), 1.2, False)]
    ev = ReadEvidence(reads={signal_key(a): 4, signal_key(b): 2, signal_key(c): 1},
                      read_xyz={signal_key(a): reads_a, signal_key(b): reads_b, signal_key(c): [(30.0, 20.0, 15.0)]})
    cw = ContentWeights({signal_key(a): 0.5, signal_key(b): 0.3, signal_key(c): 0.2}, label="file")
    out = practical_summary(metrics, is_hdr=True, content_weights=cw, read_evidence=ev)["content_weighted"]
    noise = out["noise"]
    rows = {tuple(r["rgb"]): r for r in noise["per_signal"]}
    ra, rb = rows[tuple(round(v, 4) for v in a)], rows[tuple(round(v, 4) for v in b)]
    assert ra["noise_limited"] is True and ra["E_bias_corrected"] == 0.0 and ra["noise_se"] > ra["E"]
    assert rb["noise_limited"] is False and 0.79 < rb["E_bias_corrected"] <= 0.8
    assert noise["n_with_estimate"] == 2 and noise["n_noise_limited"] == 1
    pw = out["patch_weights"]
    assert pw["score"] == round((0.5 * 0.5 + 0.3 * 0.8 + 0.2 * 1.2) / 1.0, 3)
    assert pw["score_bias_corrected"] < pw["score"]                # the labelled variant sits BESIDE the raw score
    assert pw["weight_share"]["noise_limited"] == 0.5 and pw["weight_share"]["noise_unknown"] == 0.2
    assert out["headline"]["score"] == pw["score"] and out["headline"]["score_bias_corrected"] == pw["score_bias_corrected"]
    assert "VARIANT" in noise["label"]
    json.dumps(out, allow_nan=False)


def test_kernel_variants_share_the_kernel():
    rng = np.random.default_rng(5)
    content = _content(rng)
    loc = np.column_stack([np.linspace(0.06, 0.58, 10), np.zeros(10), np.zeros(10)])
    err = np.linspace(0.5, 3.0, 10)
    res = cs.kernel_score(content, loc, err, reach=20.0, alt_err={"half": err / 2.0, "same": err})
    assert res["variants"]["same"]["score"] == res["score"]
    assert abs(res["variants"]["half"]["score"] - res["score"] / 2.0) < 2e-3


def test_read_noise_se_in_both_metrics():
    reads = [(47.5, 50.0, 54.5), (47.6, 50.1, 54.4), (47.4, 49.9, 54.6)]
    hdr = cs.read_noise_se(reads, rows=1, is_hdr=True)
    sdr = cs.read_noise_se(reads, rows=1, is_hdr=False, white_xyz=(95.047, 100.0, 108.883))
    assert hdr["n_reads"] == 3 and hdr["se"] > 0 and sdr["se"] > 0
    # two rows of the same signal: each row averages fewer reads → a larger per-row SE
    assert cs.read_noise_se(reads, rows=3, is_hdr=True)["se"] > hdr["se"]
    assert cs.read_noise_se(reads[:1], rows=1, is_hdr=True) is None
    # identical reads: floored at the meter's print quantisation, never a proof of zero noise
    assert cs.read_noise_se([reads[0], reads[0]], rows=1, is_hdr=True)["se"] > 0


# ---------------------------------------------------------------------------------------------
# read evidence = the reads the SCORED value rests on (final adopted round, loop-kept reads only)
# ---------------------------------------------------------------------------------------------
def _loop_stream(path: Path, rounds):
    """Drive the REAL measure loop: ``rounds`` = [(rgb8, phase, [xyz, ...], min_reads), ...] — each a
    measure_patch round of label p<rgb> reading the listed XYZ in turn. Returns the NDJSON path."""
    from dlc.engine.patches import Transfer, to_signal
    from dlc.measure_loop import MeasureLoopConfig, MeasurePatch, Reading, _Loop, _NdjsonWriter

    t = Transfer.power(2.2, 120.0, bit_depth=8)
    queue: list = []

    def measure(_patch):
        xyz = queue.pop(0)
        return Reading(xyz=xyz, yxy=(xyz[1], 0.31, 0.33), ok=True)

    loop = _Loop(patches=[], transfer=t, measure=measure, config=MeasureLoopConfig(dark_min_reads=1),
                 ndjson=_NdjsonWriter(path), events=None, dip=None)
    for rgb, phase, reads, min_reads in rounds:
        queue[:] = list(reads)
        patch = MeasurePatch(label="p" + "_".join(map(str, rgb)), rgb=rgb, signal=to_signal([rgb], t)[0],
                             bit_depth=8, seq=0, min_reads=min_reads)
        loop.measure_patch(patch, phase=phase, disposition=("appended" if phase == "remeasure" else None))
        assert not queue, "the loop took fewer reads than scripted"
    return path


def _strip_round_records(src: Path, dst: Path) -> Path:
    rows = [ln for ln in src.read_text(encoding="utf-8").splitlines()
            if ln.strip() and json.loads(ln).get("role") != "measurement_round"]
    dst.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return dst


def test_a_re_measured_signal_rests_on_its_final_round_only(tmp_path: Path):
    # BenQ 2026-09-26 [42,42,85]: the scored value is the single warm re-measure read; pooling the cold
    # main read made it "two-read" with an SE that was really drift → flagged noise-limited, E_corr → 0.
    rgb = (42, 42, 85)
    cold, warm = (2.00, 2.10, 4.00), (2.08, 2.20, 4.25)
    nd = _loop_stream(tmp_path / "v.ndjson", [(rgb, "main", [cold], 0), (rgb, "remeasure", [warm], 0)])
    key = signal_key([c / 255 for c in rgb])
    for path, basis in ((nd, "loop"), (_strip_round_records(nd, tmp_path / "old.ndjson"), "reconstructed")):
        final = cs.final_round_reads(path, 255)
        assert final.basis == basis and final.reads[key] == [warm] and final.counts[key] == 1
        assert cs.read_counts_from_ndjson(path, 255)[key] == 1 and cs.reads_from_ndjson(path, 255)[key] == [warm]
    final = cs.final_round_reads(nd, 255)
    m = PatchMetric(tuple(c / 255 for c in rgb), warm, tuple(c * 1.001 for c in warm), 0.031, False)
    ev = ReadEvidence(reads=final.counts, read_xyz=final.reads, loop_round_se=final.loop_se,
                      reads_basis=final.describe())
    out = practical_summary([m], is_hdr=False, content_weights=ContentWeights({key: 1.0}, label="f"),
                            read_evidence=ev)["content_weighted"]
    assert out["noise"]["n_noise_limited"] == 0 and out["noise"]["per_signal"] == []
    assert out["patch_weights"]["score_bias_corrected"] == out["patch_weights"]["score"] == 0.031
    assert out["patch_weights"]["weight_share"]["noise_unknown"] == 1.0
    assert out["evidence"]["n_single_read"] == 1 and out["evidence"]["reads_basis"].startswith("meter reads")


def test_a_glitch_the_loop_rejected_is_not_read_noise(tmp_path: Path):
    # The loop drops a +30 % glitch (its own SE ~0.01 dE2000); the noise evidence must not resurrect it.
    rgb = (128, 128, 128)
    clean = (19.0, 20.0, 21.8)
    jit = [clean, (19.01, 20.01, 21.81), (19.0 * 1.3, 26.0, 21.8 * 1.3), (18.99, 19.99, 21.79), clean, clean]
    nd = _loop_stream(tmp_path / "v.ndjson", [(rgb, "main", jit, 5)])
    key = signal_key([c / 255 for c in rgb])
    rnd = [json.loads(ln) for ln in nd.read_text(encoding="utf-8").splitlines() if "measurement_round" in ln][0]
    assert len(rnd["rejected_seqs"]) == 1 and rnd["se_de"] < 0.05
    mean = tuple(sum(r[i] for r in jit if r[1] < 25) / 5 for i in range(3))
    m = PatchMetric(tuple(c / 255 for c in rgb), mean, mean, 0.4, True)
    for path in (nd, _strip_round_records(nd, tmp_path / "old.ndjson")):        # loop records + the fallback
        final = cs.final_round_reads(path, 255)
        assert final.counts[key] == 5 and all(r[1] < 25 for r in final.reads[key])
        for is_hdr in (False, True):
            ev = ReadEvidence(reads=final.counts, read_xyz=final.reads, loop_round_se=final.loop_se)
            out = practical_summary([m], is_hdr=is_hdr, content_weights=ContentWeights({key: 1.0}, label="f"),
                                    read_evidence=ev)["content_weighted"]
            (row,) = out["noise"]["per_signal"]
            assert row["noise_se"] < 0.05 and row["noise_limited"] is False, (path.name, is_hdr, row)
    # SDR prefers the loop's own SE (its round record) over re-deriving it
    final = cs.final_round_reads(nd, 255)
    out = practical_summary([m], is_hdr=False, content_weights=ContentWeights({key: 1.0}, label="f"),
                            read_evidence=ReadEvidence(reads=final.counts, read_xyz=final.reads,
                                                       loop_round_se=final.loop_se))["content_weighted"]
    assert out["noise"]["per_signal"][0]["basis"].startswith("measure-loop round SE")


def test_reads_at_the_meter_floor_are_noise_unknown_not_low_noise():
    # D1 [3,3,3]: two agreeing early-stop reads of 0,0,0 gave SE 0.0175 → "0 % noise-limited". At or below
    # the meter floor a read spread is no noise estimate: noise_unknown, E kept.
    dark, mid = (0.003, 0.003, 0.003), (0.5, 0.5, 0.5)
    d_xyz, m_xyz = (0.0, 0.0, 0.0), (47.5, 50.0, 54.5)
    metrics = [PatchMetric(dark, d_xyz, (0.0019, 0.002, 0.0022), 0.9, True),
               PatchMetric(mid, m_xyz, m_xyz, 0.8, True)]
    ev = ReadEvidence(reads={signal_key(dark): 2, signal_key(mid): 2},
                      read_xyz={signal_key(dark): [d_xyz, d_xyz], signal_key(mid): [m_xyz, (47.51, 50.01, 54.49)]})
    out = practical_summary(metrics, is_hdr=True, content_weights=ContentWeights(
        {signal_key(dark): 0.5, signal_key(mid): 0.5}, label="f"), read_evidence=ev)["content_weighted"]
    noise = out["noise"]
    assert [r["rgb"] for r in noise["per_signal"]] == [[0.5, 0.5, 0.5]]
    assert noise["n_unknown_at_floor"] == 1 and "meter floor" in noise["unknown_rule"]
    assert out["patch_weights"]["weight_share"]["noise_unknown"] == 0.5


def test_evidence_failures_are_recorded_never_raised(monkeypatch):
    rng = np.random.default_rng(3)
    a, b = (0.2, 0.2, 0.2), (0.6, 0.3, 0.3)
    metrics = [PatchMetric(a, (20.0, 21.0, 23.0), (20.0, 21.0, 23.0), 0.5, True),
               PatchMetric(b, (30.0, 20.0, 15.0), (30.0, 20.0, 15.0), 1.2, False)]
    cw = ContentWeights({signal_key(a): 0.5, signal_key(b): 0.5}, label="f")

    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(cs, "nominal_signal_xyz", boom)
    import dlc.metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "_signal_noise", boom)
    out = practical_summary(metrics, is_hdr=True, content_weights=cw, content=[_content(rng)])
    block = out["content_weighted"]
    assert {e["part"] for e in block["errors"]} == {"noise", "nominal_location"}
    assert block["patch_weights"]["score"] == round((0.5 * 0.5 + 0.5 * 1.2), 3)       # the rest still reports
    assert "core" in out and json.dumps(out, allow_nan=False)
    monkeypatch.setattr(metrics_mod, "content_weighted_summary", boom)
    out = practical_summary(metrics, is_hdr=True, content_weights=cw)
    assert "synthetic failure" in out["content_weighted"]["error"] and "core" in out
