"""Tests for the black-aware HDR score (:mod:`dlc.black_aware` and the ``black_aware`` parts of the
content-weighted block of :func:`dlc.metrics.practical_summary`). EVIDENCE ONLY, never a gate.

Pinned here (no hardware, synthetic fixtures; the owner's data only in the opt-in test):

* the BT.2390 EETF black-level lift (toe) against reference points computed independently with
  ``colour``'s ST 2084, plus its defining properties;
* the floor sourcing order (explicit, then a raw stage's native near-black floor: the run's own, then the
  installed stack's training run's; then the recorded display black, else unavailable) and its refusals, in
  the resolver, on the orchestrator and on the offline rescore;
* the raw-stage native floor fit on synthetic ramps (an additive median offset) and its refusal rules;
* the ``floor_limited`` classification and the panel-limit band scoring: a reached black is not charged,
  a crush keeps its raw error, a lifted floor scores its chroma only, and the rest stays bit-identical.
  The practical zones / gate view stay unchanged;
* the content-weighted block carries both numbers, and the headline is black-aware only when the lift
  applies;
* opt-in: the recorded D1 run with the owner's ``content_hist_hdr_live.npz`` at a stated 0.006-nit floor, and by
  default at the native floor of its installed stack's raw stage.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import _pq
from dlc import black_aware as ba
from dlc import content_score as cs
from dlc.metrics import (ContentWeights, practical_gate_view, practical_summary, score_samples_hdr,
                         signal_key)
from dlc.mhc import Ti3Sample

_DLC = Path(__file__).resolve().parents[1]
_STUDY = Path(os.environ.get("DLC_PRACTICAL_STUDY", _DLC / "results" / "practical_score_2026-10-09"))
_RUNS = Path(os.environ.get("DLC_PRACTICAL_STUDY_RUNS", _DLC / "runs"))
_D1 = "20261002_145601_781557_hdr_asus_proart_pa32ucxr"
_D65 = (0.3127, 0.3290)

# BT.2390 §5.4.1 toe, L_B = 0, L_W = 1000 nit, computed INDEPENDENTLY of DLC's PQ with colour 0.4.6
# (eotf_inverse_ST2084 / eotf_ST2084): E1 = (PQ(L)-PQ(0))/(PQ(1000)-PQ(0)), b likewise for L_min,
# E3 = E1 + b(1-E1)^4, L' = PQ^-1(E3 (PQ(1000)-PQ(0)) + PQ(0)).  {L: (L' at L_min 0.005, L' at L_min 0.05)}
_REF_LW1000 = {
    0.0: (0.005, 0.05),
    0.001: (0.009444394308, 0.06244600247),
    0.01: (0.02722439714, 0.100802015),
    0.1: (0.146196148, 0.2834860213),
    1.0: (1.127234558, 1.428199209),
    10.0: (10.25276781, 10.79039718),
    100.0: (100.1660873, 100.5088071),
}


@pytest.mark.parametrize("col,lmin", [(0, 0.005), (1, 0.05)])
def test_bt2390_toe_matches_reference_points(col, lmin):
    xs = np.array(sorted(_REF_LW1000))
    got = ba.bt2390_black_lift(xs, min_nits=lmin, source_white_nits=1000.0)
    ref = np.array([_REF_LW1000[x][col] for x in xs])
    assert np.allclose(got, ref, rtol=2e-9, atol=0.0)


def test_bt2390_toe_properties():
    lift = ba.bt2390_black_lift
    # L'(0) = L_min exactly, L'(L_W) = L_W, at / above the source white: untouched
    assert lift([0.0], min_nits=0.006, source_white_nits=1729.26)[0] == pytest.approx(0.006, rel=1e-12)
    top = lift([1729.26, 2000.0, 1e4], min_nits=0.006, source_white_nits=1729.26)
    assert top.tolist() == [1729.26, 2000.0, 1e4]
    # L_min 0 is the identity, bit for bit (no PQ round trip)
    xs = np.geomspace(1e-4, 1500.0, 200)
    assert np.array_equal(lift(xs, min_nits=0.0, source_white_nits=1729.26), xs)
    # strictly lifted below the white and monotone (the toe never folds)
    ys = lift(xs, min_nits=0.006, source_white_nits=1729.26)
    assert np.all(ys > xs) and np.all(np.diff(ys) > 0)
    # the lift is in PQ, not linear light: far more than an additive 0.006-nit pedestal in the shadows
    assert lift([0.1], min_nits=0.006, source_white_nits=1729.26)[0] > 0.1 + 5 * 0.006
    with pytest.raises(ValueError):
        lift([0.1], min_nits=0.006, source_white_nits=0.0)
    with pytest.raises(ValueError):
        lift([0.1], min_nits=-0.001, source_white_nits=1000.0)


def test_floor_sourcing_order_and_refusal():
    r = ba.resolve_black_floor
    got = r(explicit=0.004, explicit_source="explicit", recorded=0.02, recorded_source="DIP", peak_nits=1000)
    assert (got.nits, got.source, got.peak_nits) == (0.004, "explicit", 1000.0)
    got = r(recorded=0.02, recorded_source="DIP", peak_nits=1000)
    assert (got.nits, got.source) == (0.02, "DIP") and got.available
    zero = r(recorded=0.0, peak_nits=1000)                          # a recorded 0: available, lifts nothing
    assert zero.available and zero.nits == 0.0
    none = r(peak_nits=1000)
    assert not none.available and none.source.startswith("unavailable") and "no recorded display black" in none.source
    for bad in (float("nan"), -0.01, "x", float("inf")):
        got = r(recorded=bad, peak_nits=1000)
        assert not got.available and "not a valid luminance" in got.source
    for bad in (float("nan"), -0.01, "x"):
        with pytest.raises(ValueError):                             # an operator typo never falls through
            r(explicit=bad, recorded=0.02, peak_nits=1000)
    no_peak = r(explicit=0.004, peak_nits=None)
    assert not no_peak.available and "no target peak" in no_peak.source


def _hdr_calib(tmp_path: Path, name: str, **kw):
    from dlc import calibration_profile as cp
    from dlc.adjudication import AutoAdjudicator
    from dlc.calibrate import Calibration
    from dlc.controller import CalibrationController
    from dlc.engine.patches import Transfer
    from dlc.measure_loop import SyntheticPanel
    from dlc.runs import create_run

    return Calibration(ctx=create_run("HDR", display="synthetic", run_dir=tmp_path / name),
                       profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0, mode="HDR",
                       controller=CalibrationController.mock(),
                       measure=SyntheticPanel(transfer=Transfer.pq(bit_depth=10)), adjudicator=AutoAdjudicator(),
                       bit_depth=10, **kw)


def test_the_orchestrator_sources_the_floor_in_order(tmp_path: Path):
    from dlc.dip import DisplayInstrumentProfile

    peak = SimpleNamespace(peak_nits=1500.0)
    dip = DisplayInstrumentProfile(display="synthetic", mode="HDR", native_black_nits=0.0123,
                                   noise_floor_nits=0.05, made="2026-06-19")
    explicit = _hdr_calib(tmp_path, "explicit", score_black_floor_nits=0.004)
    explicit._hdr_target, explicit._dip = (lambda: peak), (lambda: dip)
    got = explicit._score_black_floor()
    assert (got.nits, got.peak_nits) == (0.004, 1500.0) and "--score-black-floor-nits" in got.source
    assert explicit.calib["score_black_floor_nits"] == 0.004        # memoised for a resume
    recorded = _hdr_calib(tmp_path, "recorded")
    recorded._hdr_target, recorded._dip = (lambda: peak), (lambda: dip)
    got = recorded._score_black_floor()
    assert got.nits == 0.0123 and "DIP native_black_nits" in got.source and "2026-06-19" in got.source
    # the DIP's noise_floor_nits is the METER's trust floor, never the panel's black
    recorded._dip = lambda: DisplayInstrumentProfile(display="synthetic", mode="HDR", noise_floor_nits=0.05)
    assert not recorded._score_black_floor().available
    recorded._dip = lambda: None
    got = recorded._score_black_floor()
    assert not got.available and got.source.startswith("unavailable")
    with pytest.raises(ValueError):
        _hdr_calib(tmp_path, "bad", score_black_floor_nits=-1.0)


# ---------------------------------------------------------------------------------------------
# the native near-black floor from a RAW stage
# ---------------------------------------------------------------------------------------------
_D65_XZ = (0.3127 / 0.3290, (1.0 - 0.3127 - 0.3290) / 0.3290)


def _pq_nits(signal: float) -> float:
    return _pq.eotf_norm(signal) * _pq.CONTAINER_NITS


def _ramp(offset: float, *, codes=(0, 3, 10, 18, 23, 36, 42, 53, 65, 71, 89, 94, 200, 400, 1023),
          off_codes=(3,), noise=None, gain: float = 1.0):
    """A native 10-bit PQ grey ramp: measured = PQ target x ``gain`` + ``offset`` (+ ``noise`` per code), the
    codes in ``off_codes`` read 0 (LEDs off), code 0 reads 0; two colour patches ride along (ignored)."""
    rows = []
    for i, c in enumerate(codes):
        s = c / 1023.0
        y = 0.0 if (c == 0 or c in off_codes) else _pq_nits(s) * gain + offset + (noise[i] if noise else 0.0)
        rows.append(((s, s, s), (y * _D65_XZ[0], y, y * _D65_XZ[1])))
    rows.append(((0.05, 0.0, 0.0), (0.02, 0.01, 0.0)))                  # a near-black red: not a grey
    rows.append(((0.5, 0.4, 0.4), (90.0, 80.0, 70.0)))
    return rows


def _samples(rows):
    from dlc.mhc import Ti3Sample

    return [Ti3Sample(rgb, xyz) for rgb, xyz in rows]


def _write_ti3(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(" ".join(f"{v:.6f}" for v in (*(100.0 * c for c in rgb), *xyz)) for rgb, xyz in rows)
    path.write_text("CTI3\nBEGIN_DATA_FORMAT\nRGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z\nEND_DATA_FORMAT\n"
                    f"NUMBER_OF_SETS {len(rows)}\nBEGIN_DATA\n{body}\nEND_DATA\n", encoding="utf-8")
    return path


def _raw_run(parent: Path, name: str, rows=None, *, mode="HDR", status="done", ti3=True) -> Path:
    """A recorded run folder with a ``measure:raw`` stage (``rows=None``: no raw stage at all)."""
    root = parent / name
    stages = {}
    if rows is not None:
        p = root / "measurements" / "raw.ti3"
        if ti3:
            _write_ti3(p, rows)
        stages["measure:raw"] = {"stage": "measure:raw", "status": status, "data": {"ti3": str(p)}}
    root.mkdir(parents=True, exist_ok=True)
    (root / "dlc_state.json").write_text(json.dumps({"mode": mode, "calib": {"stages": stages}}), encoding="utf-8")
    return root


def test_native_floor_fit_on_a_synthetic_ramp():
    noise = [0.0, 0.0, 0.0004, -0.0003, 0.0002, -0.0001, 0.0003, -0.0002, 0.0001, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    rows = _ramp(0.004, noise=noise)
    rows.append(rows[2])                                               # a repeat read of code 10: averaged
    nits, why, st = ba.fit_native_floor(_samples(rows))
    assert why is None and nits == pytest.approx(0.004, abs=3e-4)
    used = [r["signal_pct"] for r in st["greys"]]
    # 0 < PQ target <= 0.3 nit and a lit read: code 0 and the LEDs-off code 3 are out, codes 200+ too
    assert used == [round(100 * c / 1023, 4) for c in (10, 18, 23, 36, 42, 53, 65, 71, 89, 94)]
    assert st["n"] == 10 and st["n_below_meter_floor"] == 1 and st["n_above_window"] == 3
    assert st["greys"][0]["reads"] == 2 and st["n_positive"] == 10
    assert st["signal_range_pct"] == [used[0], used[-1]] and st["target_range_nits"][1] <= ba.RAW_FIT_MAX_TARGET_NITS
    assert st["robust_spread_nits"] < st["median_offset_nits"] and st["rule"] == ba.RAW_FIT_RULE
    # an ADDITIVE offset is what is fitted: a native tone (gain) error leaks in at the window's median target
    nits_g, _, st_g = ba.fit_native_floor(_samples(_ramp(0.004, gain=1.02)))
    med_t = float(np.median([r["target_nits"] for r in st_g["greys"]]))
    assert nits_g == pytest.approx(0.004 + 0.02 * med_t, abs=2e-5)


def test_native_floor_fit_refusals():
    # fewer than 3 usable greys: all but two lit greys read 0 (LEDs off)
    nits, why, st = ba.fit_native_floor(_samples(_ramp(0.004, off_codes=(3, 10, 18, 23, 36, 42, 53, 65, 71))))
    assert nits is None and "2 usable" in why and st["n"] == 2
    # no lift: the native greys sit on or below the PQ target
    nits, why, _ = ba.fit_native_floor(_samples(_ramp(-0.002)))
    assert nits is None and why.startswith("incoherent") and "not a lift" in why
    # incoherent: offsets scatter about a small median
    scatter = [0.0, 0.0, 0.010, -0.008, 0.002, -0.001, 0.009, 0.0015, -0.006, 0.003, 0.0, 0.0, 0.0, 0.0, 0.0]
    nits, why, st = ba.fit_native_floor(_samples(_ramp(0.0, noise=scatter)))
    assert nits is None and "robust spread" in why and st["robust_spread_nits"] >= st["median_offset_nits"] > 0


def test_raw_floor_from_a_run_and_its_refusals(tmp_path: Path):
    good = ba.raw_floor_from_run(_raw_run(tmp_path, "20260923_120740_186046_hdr_x", _ramp(0.005)), role="r")
    assert good.available and good.nits == pytest.approx(0.005, abs=1e-5)
    assert good.source.startswith("raw run 20260923_120740 (r;")
    assert good.stats["run"] == "20260923_120740_186046_hdr_x" and good.stats["n"] == 10
    # the recorded ti3 path moved with the runs/ tree: the run folder's own copy is read
    moved = _raw_run(tmp_path, "moved", _ramp(0.005))
    st = json.loads((moved / "dlc_state.json").read_text())
    st["calib"]["stages"]["measure:raw"]["data"]["ti3"] = "Z:/elsewhere/raw.ti3"
    (moved / "dlc_state.json").write_text(json.dumps(st))
    assert ba.raw_floor_from_run(moved, role="r").available
    for run, needle in ((_raw_run(tmp_path, "sdr", _ramp(0.005), mode="SDR"), "SDR-mode run"),
                        (_raw_run(tmp_path, "noraw"), "no completed raw stage"),
                        (_raw_run(tmp_path, "aborted", _ramp(0.005), status="aborted"), "no completed raw stage"),
                        (_raw_run(tmp_path, "gone", _ramp(0.005), ti3=False), "not on disk"),
                        (tmp_path / "missing", "dlc_state.json unreadable")):
        got = ba.raw_floor_from_run(run, role="r")
        assert not got.available and needle in got.reason, got.reason
    bad = _write_ti3(tmp_path / "x.ti3", [])
    bad.write_text("not a ti3", encoding="utf-8")
    got = ba.raw_floor_from_ti3(bad, run_name="x", role="r")
    assert not got.available and "unreadable" in got.reason


def test_floor_sourcing_order_with_raw_fits():
    r = ba.resolve_black_floor
    fit = ba.RawFloorFit(0.0052, "raw run A (own)", None, {"n": 7})
    refused = ba.RawFloorFit(None, "raw run B (stack)", "raw run B: 2 usable native grey(s)")
    # explicit beats a raw fit
    got = r(explicit=0.004, raw=[fit], recorded=0.0, peak_nits=1000)
    assert (got.nits, got.fit, got.skipped) == (0.004, None, ())
    # the first AVAILABLE raw fit wins over the recorded display black; refusals before it are listed
    got = r(raw=[refused, fit], recorded=0.0, recorded_source="DIP", peak_nits=1000)
    assert (got.nits, got.source, got.fit) == (0.0052, "raw run A (own)", {"n": 7})
    assert got.skipped == ("raw run B: 2 usable native grey(s)",)
    d = got.as_dict()
    assert d["floor_fit"] == {"n": 7} and d["floor_sources_skipped"] == list(got.skipped)
    # all raw fits refused: the recorded display black, with the refusals listed
    got = r(raw=[refused], recorded=0.0, recorded_source="DIP", peak_nits=1000)
    assert (got.nits, got.source, got.fit) == (0.0, "DIP", None) and len(got.skipped) == 1
    # nothing usable: unavailable, and the reason names the raw refusals
    got = r(raw=[refused], peak_nits=1000)
    assert not got.available and "no recorded display black" in got.source and "2 usable" in got.source
    assert "floor_fit" not in got.as_dict()


def test_the_orchestrator_prefers_raw_stages_in_order(tmp_path: Path):
    from dlc import stack_registry
    from dlc.dip import DisplayInstrumentProfile

    peak = SimpleNamespace(peak_nits=1500.0)
    dip = DisplayInstrumentProfile(display="synthetic", mode="HDR", native_black_nits=0.0, made="2026-06-19")

    def calib(name, **kw):
        c = _hdr_calib(tmp_path, name, **kw)
        c._hdr_target, c._dip = (lambda: peak), (lambda: dip)
        return c

    # 1. this run's own raw stage
    own = calib("20261009_100000_000001_own")
    raw_ti3 = _write_ti3(own.ctx.root / "measurements" / "raw.ti3", _ramp(0.006))
    own.calib["stages"]["measure:raw"] = {"stage": "measure:raw", "status": "done", "data": {"ti3": str(raw_ti3)}}
    got = own._score_black_floor()
    assert got.nits == pytest.approx(0.006, abs=1e-5) and "this run's raw stage" in got.source
    assert got.fit["n"] == 10 and got.as_dict()["floor_fit"]["run"] == own.ctx.root.name
    # the explicit option still wins
    own.calib["score_black_floor_nits"] = 0.004
    assert own._score_black_floor().nits == 0.004
    # 2. no own raw: the installed stack's training run (registry) -- its MHC run measured raw
    vo = calib("vo")
    assert vo._score_black_floor().nits == 0.0                            # no registry yet: the DIP's 0
    reg = stack_registry.StackRegistry.load(tmp_path / stack_registry.REGISTRY_FILE)
    reg.record(stack_registry.StackRecord(display=vo.display.name, mode="HDR", monitor=0,
                                          run_id="20260924_132412_000000_mhc", applied_at="2026-09-24"))
    _raw_run(tmp_path, "20260924_132412_000000_mhc", _ramp(0.0045))
    got = vo._score_black_floor()
    assert got.nits == pytest.approx(0.0045, abs=1e-5)
    assert got.source.startswith("raw run 20260924_132412 (the installed stack's training run;")
    # 3. --verify-patches-from names a cube-only training run (no raw): fall through to the MHC's applying run
    cube_run = _raw_run(tmp_path, "cube_only")
    vo.calib["stages"]["verify-source"] = {"status": "done", "data": {"run": str(cube_run)}}
    got = vo._score_black_floor()
    assert got.nits == pytest.approx(0.0045, abs=1e-5) and "the installed MHC's applying run" in got.source
    assert any("no completed raw stage" in s for s in got.skipped)
    # every raw source refused (an incoherent ramp): the DIP's recorded black, the refusals listed
    _raw_run(tmp_path, "20260924_132412_000000_mhc", _ramp(-0.002))
    got = vo._score_black_floor()
    assert got.nits == 0.0 and "DIP native_black_nits" in got.source and len(got.skipped) == 2


def test_rescore_sources_the_raw_floor_of_the_recorded_stack(tmp_path: Path):
    mhc = _raw_run(tmp_path, "mhc_run", _ramp(0.0055))
    vo = _raw_run(tmp_path, "vo_run")
    state = {"mode": "HDR", "calib": {"stages": {}, "verify_patches_from": "Z:/old/runs/mhc_run",
                                      "installed_stack": {"run_id": "mhc_run"}}}
    fits = cs.recorded_raw_floor_fits(vo, state)
    assert len(fits) == 1 and fits[0].available and fits[0].stats["run"] == mhc.name   # found beside the run
    # the run's own raw stage comes first
    own = _raw_run(tmp_path, "own_run", _ramp(0.007))
    fits = cs.recorded_raw_floor_fits(own, {**json.loads((own / "dlc_state.json").read_text()),
                                            "calib": {**state["calib"], "stages": {"measure:raw": {"status": "done"}}}})
    assert [f.stats["run"] for f in fits] == ["own_run"] and fits[0].nits == pytest.approx(0.007, abs=1e-5)
    # a training run that is not on disk is a listed refusal, then the stack record is tried
    fits = cs.recorded_raw_floor_fits(vo, {"mode": "HDR", "calib": {"verify_patches_from": "Z:/gone/run_x",
                                                                    "installed_stack": {"run_id": "mhc_run"}}})
    assert not fits[0].available and "not on disk" in fits[0].reason and fits[1].available


# ---------------------------------------------------------------------------------------------
# classification + scoring
# ---------------------------------------------------------------------------------------------
_FLOOR = 0.005
_PEAK = 1000.0


def _grey(nits: float) -> tuple[float, float, float]:
    s = _pq.oetf_norm(nits / _pq.CONTAINER_NITS)
    return (s, s, s)


def _ideal(rgb) -> np.ndarray:
    from dlc.engine.model import Target, TargetSpace

    return TargetSpace(Target.hdr_rec2020_pq(white_xy=_D65)).ideal_xyz(np.asarray([rgb], float))[0]


def _set():
    """(name, rgb, measured XYZ): the floor's situations, scored by the production HDR scorer."""
    rows = []
    black = (0.0, 0.0, 0.0)
    rows.append(("black", black, (0.0, 0.0, 0.0)))                                 # reaches true black
    g1 = _grey(0.002)
    rows.append(("floor_raised", g1, tuple(_ideal(g1) * (0.002 + _FLOOR) / 0.002)))  # + floor, chroma kept
    g2 = _grey(0.02)
    rows.append(("crushed", g2, (0.0, 0.0, 0.0)))                                 # below its own target
    g3 = _grey(0.03)
    rows.append(("overshoot", g3, tuple(_ideal(g3) * 0.2 / 0.03)))                # beyond the BT.2390 point
    g4 = _grey(1.0)
    rows.append(("mid", g4, tuple(_ideal(g4) * 1.1)))                             # far above 10 x the floor
    red = (0.45, 0.2, 0.2)
    rows.append(("colour", red, tuple(_ideal(red) * np.array([1.05, 1.0, 0.97]))))
    metrics, _ = score_samples_hdr([Ti3Sample(rgb, xyz) for _n, rgb, xyz in rows], white_xy=_D65, peak_nits=_PEAK)
    return {n: m for (n, _r, _x), m in zip(rows, metrics)}, metrics


def _content():
    rng = np.random.default_rng(5)
    n = 2500
    itp = np.column_stack([rng.uniform(0.0, 0.3, n), rng.normal(0.0, 0.01, n), rng.normal(0.0, 0.01, n)])
    w = 1.0 / (1.0 + 20.0 * itp[:, 0])                                           # dark-heavy, like the survey
    return cs.ContentDistribution(name="fixture", variant="json", itp=itp, w=w, source="mem", fingerprint="x")


def _floor(nits=_FLOOR, source="explicit option (test)") -> ba.BlackFloor:
    return ba.resolve_black_floor(explicit=nits, explicit_source=source, peak_nits=_PEAK)


def test_floor_limited_classification_and_band_scoring():
    named, metrics = _set()
    per = ba.black_aware_patch_scores(metrics, _floor(), white_xy=_D65)
    lim = dict(zip(named, per["floor_limited"]))
    assert lim == {"black": True, "floor_raised": True, "crushed": True, "overshoot": True,
                   "mid": False, "colour": False}                   # PQ target Y < 10 x 0.005 nit
    e = dict(zip(named, per["e_black_aware"]))
    toe = dict(zip(named, per["e_toe_point"]))
    raw = {k: m.de2000 for k, m in named.items()}
    assert e["black"] == pytest.approx(0.0, abs=1e-9) and toe["black"] > 5.0      # reached black: not charged
    assert raw["floor_raised"] > 2.0 and e["floor_raised"] == pytest.approx(0.0, abs=1e-6)   # chroma only
    assert e["crushed"] == pytest.approx(raw["crushed"], rel=1e-9)                # a crush keeps its raw error
    assert 0.0 < e["overshoot"] < raw["overshoot"]                               # scored vs the BT.2390 point
    assert e["overshoot"] == pytest.approx(toe["overshoot"], rel=1e-12)
    assert e["mid"] == raw["mid"] and e["colour"] == raw["colour"]               # untouched, bit for bit
    # the black-aware target never moves below the PQ target nor above the BT.2390 one
    assert np.all(per["band_y"] >= per["target_y"]) and np.all(per["band_y"] <= per["toe_y"] + 1e-15)


def test_content_weighted_block_carries_both_numbers_and_the_gate_basis_is_unchanged():
    named, metrics = _set()
    weights = ContentWeights({signal_key(m.rgb): 1.0 for m in metrics}, label="file")
    plain = practical_summary(metrics, is_hdr=True, content=[_content()], content_weights=weights, white_xy=_D65)
    out = practical_summary(metrics, is_hdr=True, content=[_content()], content_weights=weights, white_xy=_D65,
                            black_floor=_floor())
    cw = out["content_weighted"]
    res = cw["classes"]["fixture"]
    assert res["score"] == plain["content_weighted"]["classes"]["fixture"]["score"]      # raw stays recorded
    ba_res = res["black_aware"]
    assert ba_res["score"] < res["score"] and ba_res["vs_toe_point"]["score"] > res["score"]
    head = cw["headline"]
    assert head["black_aware"] is True and head["score"] == ba_res["score"] and head["score_raw"] == res["score"]
    assert "BLACK-AWARE" in head["label"] and "0.005 nit" in head["label"] and "explicit option (test)" in head["label"]
    assert head["black_floor_nits"] == _FLOOR and head["n_floor_limited_signals"] == 4
    block = cw["black_aware"]
    assert block["applied"] and block["floor_limited"]["raw"]["n"] == 4
    assert block["floor_limited"]["black_aware"]["avg"] < block["floor_limited"]["raw"]["avg"]
    assert [r["target_Y"] for r in block["per_signal"]] == sorted(r["target_Y"] for r in block["per_signal"])
    assert res["evidence"]["floor_limited"]["n_signals"] == 4                    # the raw score's share on them
    zones = {r["zone"] for r in ba_res["top_contributors"]}
    assert zones <= {"floor_limited", "core", "limits", "clamped"}
    pw = cw["patch_weights"]
    assert pw["score"] == plain["content_weighted"]["patch_weights"]["score"]
    assert pw["score_black_aware"] < pw["score"] and pw["weight_share"]["floor_limited"] == round(4 / 6, 4)
    # the practical zones and the verify gate's view never see the black-aware class
    for k in ("core", "limits", "clamped", "tube", "bands", "per_signal"):
        assert out[k] == plain[k]
    assert practical_gate_view(out) == practical_gate_view(plain)
    json.dumps(out, allow_nan=False)                                              # strict-JSON safe
    # the seam question and the deliverable report show the raw number beside the black-aware one
    from dlc.calibrate import _content_lead_text, _render_practical_html

    lead = _content_lead_text({"content_weighted": head})
    assert lead.startswith(head["label"]) and f"[raw {res['score']}]" in lead
    html = _render_practical_html(out, "dE_ITP")
    assert "BLACK-AWARE" in html and f"(raw {res['score']})" in html and "Floor-limited signals (PQ target Y &lt;" in html


@pytest.mark.parametrize("floor,is_hdr,why", [
    (ba.BlackFloor(None, "unavailable: no recorded display black and no explicit floor option", _PEAK), True,
     "unavailable"),
    (ba.BlackFloor(0.0, "DIP native_black_nits", _PEAK), True, "identity"),
    (ba.BlackFloor(_FLOOR, "explicit", _PEAK), False, "SDR"),
])
def test_the_headline_stays_raw_when_the_lift_does_not_apply(floor, is_hdr, why):
    _named, metrics = _set()
    out = practical_summary(metrics, is_hdr=is_hdr, content=[_content()], white_xy=_D65, black_floor=floor)
    cw = out["content_weighted"]
    head = cw["headline"]
    assert head["black_aware"] is False and why in head["black_aware_reason"]
    assert head["score"] == cw["classes"]["fixture"]["score"] and "score_raw" not in head
    assert "black_aware" not in cw["classes"]["fixture"] and cw["black_aware"]["applied"] is False
    from dlc.calibrate import _render_practical_html

    assert "not applied" in _render_practical_html(out, "dE_ITP")


def test_without_a_floor_the_block_is_unchanged():
    _named, metrics = _set()
    out = practical_summary(metrics, is_hdr=True, content=[_content()], white_xy=_D65)
    cw = out["content_weighted"]
    assert "black_aware" not in cw and "black_aware" not in cw["headline"]
    assert "floor_limited" not in cw["classes"]["fixture"]["evidence"]


@pytest.mark.slow
def test_recorded_d1_run_black_aware_floor_sources():
    """Opt-in: the owner's recorded D1 HDR run + HDR live-action histogram (local, gitignored).

    * With a stated 0.006-nit floor (the owner's read of the near-black offset), the headline is black-aware
      and the raw 1.68 stays recorded.
    * By default, the floor is the native near-black floor of the installed stack's training run's raw stage
      (D1 is a verify-only run; its stack is run 20260924_132412, a full run): 0.00532 nit over 10 lit greys
      (0.98 % .. 9.19 %). The headline is black-aware at the same 1.331: inside the panel-limit band only the
      chroma error counts, so the floor-limited signals score alike at 0.0053 and 0.006.
    * Without any raw source (the explicit option aside), the DIP's full-field black (0.0 on this local-dimming
      panel) lifts nothing, so the headline stays raw."""
    hist = _STUDY / "content_hist_hdr_live.npz"
    run = _RUNS / _D1
    if not (hist.is_file() and (run / "reports" / "verification_iter00_patch_metrics.json").is_file()):
        pytest.skip("local study data / recorded run absent (set DLC_PRACTICAL_STUDY / DLC_PRACTICAL_STUDY_RUNS)")
    got = cs.rescore_run(run, [str(hist)], reach=20.0, black_floor_nits=0.006)["content_weighted"]
    head = got["headline"]
    assert head["black_aware"] is True and head["score_raw"] == 1.68
    assert head["score"] == 1.331 and head["black_floor_nits"] == 0.006
    assert head["black_floor_source"] == "explicit option (--black-floor-nits)"
    assert got["black_aware"]["n_floor_limited_signals"] == 5
    assert got["classes"]["hdr_live"]["black_aware"]["vs_toe_point"]["score"] > 1.68       # the literal toe point
    default = cs.rescore_run(run, [str(hist)], reach=20.0)["content_weighted"]
    head, block = default["headline"], default["black_aware"]
    assert head["black_aware"] is True and head["score_raw"] == 1.68 and head["score"] == 1.331
    assert block["floor_nits"] == 0.00532 and block["floor_source"].startswith(
        "raw run 20260924_132412 (the installed stack's training run;")
    assert "display floor 0.00532 nit from raw run 20260924_132412" in head["label"]
    fit = block["floor_fit"]
    assert fit["run"] == "20260924_132412_307436_hdr_asus_proart_pa32ucxr" and fit["n"] == 10
    assert fit["signal_range_pct"] == [0.9775, 9.1887] and fit["robust_spread_nits"] < fit["median_offset_nits"]
    import unittest.mock as um

    with um.patch.object(cs, "recorded_raw_floor_fits", lambda *_a, **_k: []):
        no_raw = cs.rescore_run(run, [str(hist)], reach=20.0)["content_weighted"]
    assert no_raw["headline"]["score"] == 1.68 and no_raw["headline"]["black_aware"] is False
    assert no_raw["black_aware"]["floor_nits"] == 0.0 and "panel_limits" in no_raw["black_aware"]["floor_source"]
