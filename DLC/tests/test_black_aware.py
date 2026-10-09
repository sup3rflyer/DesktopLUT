"""Tests for the black-aware HDR score (:mod:`dlc.black_aware` and the ``black_aware`` parts of the
content-weighted block of :func:`dlc.metrics.practical_summary`). EVIDENCE ONLY, never a gate.

Pinned here (no hardware, synthetic fixtures; the owner's data only in the opt-in test):

* the BT.2390 EETF black-level lift (toe) against reference points computed independently with
  ``colour``'s ST 2084, plus its defining properties;
* the floor sourcing order (explicit, then a raw stage's native near-black floor: the run's own, then an
  identity-matched recorded run's, labelled by what it is to the scored run; then the recorded display black,
  else unavailable), the pedestal colour's sourcing, and the refusals, in the resolver, on the orchestrator and
  on the offline rescore;
* the raw-stage native floor fit on synthetic ramps (the intercept over the lowest lit greys, the DIP meter
  floor, the window rule) and its refusal rules;
* the additive-pedestal score: every patch against the nearest point of [target, target + F w]. A reached black
  and the pedestal itself are not charged, a lift beyond it or in another colour is, a crush keeps its raw
  error, and there is no cutoff. The superseded BT.2390 band stays a recorded variant. The practical zones /
  gate view stay unchanged;
* the content-weighted block carries both numbers on one basis each, the headline is black-aware only when the
  pedestal applies, and the seam lead / report / dashboard label it;
* opt-in: the recorded D1 run with the owner's ``content_hist_hdr_live.npz``, by default at the native floor of
  its installed stack's raw stage, and at a stated 0.006-nit floor.

Each review finding of 2026-10-09 has a test that failed on the superseded rule: #1
``test_pedestal_band_scoring`` (lifted to the toe), #2 ``test_the_score_is_continuous_across_the_old_cutoff``,
#3 ``test_the_pedestal_is_charged_in_any_other_colour``, #4 ``test_every_number_beside_a_black_aware_headline_is_on
_its_basis`` + ``test_the_dashboard_labels_the_black_aware_headline``, #5
``test_native_floor_fit_separates_the_floor_from_native_tone_error``, #6
``test_a_recorded_raw_floor_must_match_the_scored_runs_identity`` +
``test_a_full_run_never_borrows_the_previous_stack_under_the_installed_label``. The second round (2026-10-09):
true black reachable below the floor (``test_true_black_is_reachable_below_the_floor_and_a_crush_above_it_is_
charged``) and one meter floor, the read evidence's (``test_the_meter_floor_is_the_read_evidences_and_a_non_
positive_dip_value_is_not_a_floor`` + ``test_signals_below_the_meter_floor_are_flagged_reported_and_still_scored``).
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
from dlc.metrics import (METER_FLOOR_NITS_FALLBACK, ContentWeights, MeterFloor, ReadEvidence, practical_gate_view,
                         practical_summary, resolve_meter_floor, score_samples_hdr, signal_key)
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
    # the pedestal score needs no peak (only the BT.2390 variants do): available, the peak recorded as missing
    no_peak = r(explicit=0.004, peak_nits=None)
    assert no_peak.available and no_peak.peak_nits is None


def test_pedestal_colour_sourcing():
    r = ba.resolve_black_floor
    fit = ba.RawFloorFit(0.003, "raw run A (own)", None, {"n": 5}, white_xy=(0.3248, 0.3284),
                         white_source="raw run A's native white")
    dip = ((0.3169, 0.3284), "DIP native_white_xy")
    # the used raw fit's own native white first
    got = r(raw=[fit], pedestal=[dip], peak_nits=1000)
    assert got.pedestal == ((0.3248, 0.3284), "raw run A's native white")
    assert got.as_dict()["pedestal_xy"] == [0.3248, 0.3284]
    # an explicit floor: the first valid candidate (an invalid one is skipped)
    got = r(explicit=0.004, pedestal=[((0.0, 0.0), "bad"), dip], peak_nits=1000)
    assert got.pedestal == dip
    # nothing: D65, stated as ASSUMED
    got = r(explicit=0.004, peak_nits=1000)
    assert got.pedestal == (ba.D65_XY, ba.PEDESTAL_ASSUMED) and "ASSUMED" in got.as_dict()["pedestal_source"]


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
                                   noise_floor_nits=0.05, made="2026-06-19", native_white_xy=[0.315, 0.33])
    explicit = _hdr_calib(tmp_path, "explicit", score_black_floor_nits=0.004)
    explicit._hdr_target, explicit._dip = (lambda: peak), (lambda: dip)
    got = explicit._score_black_floor()
    assert (got.nits, got.peak_nits) == (0.004, 1500.0) and "--score-black-floor-nits" in got.source
    assert got.pedestal == ((0.315, 0.33), "DIP native_white_xy, made 2026-06-19")   # the DIP's native white
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
_CODES = (0, 3, 10, 18, 23, 36, 42, 53, 65, 71, 89, 94, 200, 400, 1023)
_IN_005 = (10, 18, 23, 36, 42)                 # the lit codes with a PQ target <= 0.05 nit (code 3 is LEDs-off)


def _pq_nits(signal: float) -> float:
    return _pq.eotf_norm(signal) * _pq.CONTAINER_NITS


def _ramp(offset: float, *, codes=_CODES, off_codes=(3,), noise=None, gain: float = 1.0,
          tone_above: float = 0.0, tone_from: float = 0.05):
    """A native 10-bit PQ grey ramp: measured = PQ target x ``gain`` + ``offset`` (+ ``noise`` per code), plus a
    native TONE error ``tone_above`` x target on targets above ``tone_from`` nit. The codes in ``off_codes``
    read 0 (LEDs off), code 0 reads 0; two colour patches ride along (ignored)."""
    rows = []
    for i, c in enumerate(codes):
        s = c / 1023.0
        t = _pq_nits(s)
        y = (0.0 if (c == 0 or c in off_codes)
             else t * gain + offset + (noise[i] if noise else 0.0) + (tone_above * t if t > tone_from else 0.0))
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


_PA = {"display": "Asus ProArt PA32UCXR", "hardware_id": "AUS322A", "ccmx": "third_party\\x\\PA_HDR.ccmx"}


def _preflight(display=None, hardware_id=None, ccmx=None):
    dg: dict = {}
    if display:
        dg["display"] = display
    if hardware_id:
        dg["monitor_map"] = {"hardware_id": hardware_id}
    if ccmx:
        dg["correction"] = {"has_correction": True, "file": ccmx}
    return {"status": "done", "digest": dg}


def _raw_run(parent: Path, name: str, rows=None, *, mode="HDR", status="done", ti3=True, flow=None,
             white_xyz=None, ident=None, extra_calib=None) -> Path:
    """A recorded run folder with a ``measure:raw`` stage (``rows=None``: no raw stage at all) and, with
    ``ident`` ({display, hardware_id, ccmx}), a preflight digest."""
    root = parent / name
    stages = {}
    if ident is not None:
        stages["preflight"] = _preflight(**ident)
    if rows is not None:
        p = root / "measurements" / "raw.ti3"
        if ti3:
            _write_ti3(p, rows)
        stages["measure:raw"] = {"stage": "measure:raw", "status": status,
                                 "data": {"ti3": str(p), **({"white_xyz": white_xyz} if white_xyz else {})}}
    root.mkdir(parents=True, exist_ok=True)
    calib = {"stages": stages, **({"flow": flow} if flow else {}), **(extra_calib or {})}
    (root / "dlc_state.json").write_text(json.dumps({"mode": mode, "calib": calib}), encoding="utf-8")
    return root


def test_native_floor_fit_is_the_intercept_over_the_lowest_lit_greys():
    noise = [0.0, 0.0, 0.0002, -0.0001, 0.0001, -0.0001, 0.0001, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    rows = _ramp(0.004, noise=noise)
    rows.append(rows[2])                                               # a repeat read of code 10: averaged
    nits, why, st = ba.fit_native_floor(_samples(rows), meter_floor=MeterFloor(0.0001, "DIP (test)", True))
    assert why is None and nits == pytest.approx(0.004, abs=3e-4)
    used = [r["signal_pct"] for r in st["greys"]]
    # 0 < PQ target <= 0.05 nit and a lit read: code 0 and the LEDs-off code 3 are out, codes 53+ too
    assert used == [round(100 * c / 1023, 4) for c in _IN_005]
    assert (st["n"], st["window_nits"], st["window_widened"]) == (5, 0.05, False)
    assert st["greys"][0]["reads"] == 2 and st["n_unlit"] == 1 and st["n_below_meter_floor"] == 0
    assert st["F_nits"] == pytest.approx(nits, abs=1e-6) and st["g"] == pytest.approx(1.0, abs=0.02)
    assert st["se_F_nits"] < st["F_nits"] and st["residual_rms_nits"] < 3e-4 and st["rule"] == ba.RAW_FIT_RULE
    assert (st["meter_floor_nits"], st["meter_floor_source"], st["meter_floor_measured"]) == (0.0001, "DIP (test)",
                                                                                               True)
    assert all("fitted_nits" in r and "residual_nits" in r for r in st["greys"])


def test_native_floor_fit_separates_the_floor_from_native_tone_error():
    """Review #5: the bottom greys sit ~F over the target while the 0.03-0.26-nit greys carry a native TONE error;
    the old median offset over a 0.3-nit window read that tone error as floor."""
    # a gain error everywhere: the intercept is still the additive floor, the gain goes to g
    nits, why, st = ba.fit_native_floor(_samples(_ramp(0.003, gain=1.10)), meter_floor=0.0)
    assert why is None and nits == pytest.approx(0.003, abs=1e-6) and st["g"] == pytest.approx(1.10, abs=1e-4)
    assert st["residual_max_abs_nits"] < 1e-5
    # a tone error only above the window (the D1 shape): the bottom-grey floor, not the median of the offsets
    rows = _ramp(0.003, tone_above=0.04)
    nits, why, st = ba.fit_native_floor(_samples(rows), meter_floor=0.0)
    assert why is None and nits == pytest.approx(0.003, abs=1e-6) and st["window_nits"] == 0.05
    offs = [y - _pq_nits(rgb[0]) for rgb, (_x, y, _z) in rows[:len(_CODES)]
            if 0 < _pq_nits(rgb[0]) <= 0.3 and y > 0]
    assert float(np.median(offs)) > 0.0035                             # what the old median rule would have said


def test_the_meter_floor_is_the_read_evidences_and_a_non_positive_dip_value_is_not_a_floor():
    """Fix 2 (2026-10-09): the raw fit took the DIP's noise_floor_nits 0 as a 0-nit meter floor, while the read
    evidence (the practical score) took the same 0 as NOT MEASURED and used its 0.05-nit fallback. One resolver
    now: the DIP's value when > 0, else the documented fallback, stated as such."""
    for v in (None, 0.0, -0.01, float("nan"), "x"):
        mf = resolve_meter_floor(v)
        assert (mf.nits, mf.measured) == (METER_FLOOR_NITS_FALLBACK, False) and mf.source.startswith("fallback")
    assert "0.0 is not a measured floor" in resolve_meter_floor(0.0).source
    assert ReadEvidence().noise_floor_source == resolve_meter_floor(None).source       # the default IS the rule
    mf = resolve_meter_floor(0.002, where="DIP noise_floor_nits (test)")
    assert (mf.nits, mf.measured) == (0.002, True) and mf.source.startswith("DIP noise_floor_nits (test)")
    # the fit: a DIP 0 is the fallback. It is no measurement of this meter, so it FLAGS the greys below it and
    # drops none (lit = a read above 0)
    rows = _ramp(0.0005, off_codes=())
    _, _, at0 = ba.fit_native_floor(_samples(rows), meter_floor=0.0)
    assert (at0["meter_floor_nits"], at0["meter_floor_measured"]) == (METER_FLOOR_NITS_FALLBACK, False)
    assert "0.0 is not a measured floor" in at0["meter_floor_source"] and at0["n_unlit"] == 0
    assert at0["n_below_meter_floor"] == at0["n"] == sum(g["below_meter_floor"] for g in at0["greys"]) > 0
    _, _, none = ba.fit_native_floor(_samples(rows))
    assert none["n"] == at0["n"] and none["meter_floor_nits"] == METER_FLOOR_NITS_FALLBACK
    # a MEASURED meter floor excludes: the lit 0.0008-nit grey (code 3) is out at a measured 0.001
    _, _, meas = ba.fit_native_floor(_samples(rows), meter_floor=0.001)
    assert meas["meter_floor_measured"] and meas["n_unlit"] == 1 and meas["n"] == at0["n"] - 1
    # a resolved MeterFloor passes through unchanged (one value everywhere)
    _, _, same = ba.fit_native_floor(_samples(rows), meter_floor=mf)
    assert (same["meter_floor_nits"], same["meter_floor_source"]) == (mf.nits, mf.source)
    # too few lit greys in 0.05 nit: widened to the next window, and the stats say so
    nits, why, st = ba.fit_native_floor(_samples(_ramp(0.004, off_codes=(3, 10, 18, 23))), meter_floor=0.0)
    assert why is None and st["window_nits"] == 0.1 and st["window_widened"] and st["n"] == 3
    assert nits == pytest.approx(0.004, abs=1e-6)


def test_native_floor_fit_refusals():
    # fewer than 3 lit greys even in the widest window
    nits, why, st = ba.fit_native_floor(
        _samples(_ramp(0.004, off_codes=(3, 10, 18, 23, 36, 42, 53, 65, 71))), meter_floor=0.0)
    assert nits is None and "2 lit native grey" in why and st["n"] == 0 and st["window_nits"] is None
    # no lift: the native greys sit on or below the PQ target
    nits, why, _ = ba.fit_native_floor(_samples(_ramp(-0.002)), meter_floor=0.0)
    assert nits is None and why.startswith("incoherent") and "not a lift" in why
    # unresolved: the intercept's standard error is not below it
    scatter = [0.0, 0.0, 0.004, -0.003, 0.005, -0.004, 0.003, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    nits, why, st = ba.fit_native_floor(_samples(_ramp(0.001, noise=scatter)), meter_floor=0.0)
    assert nits is None and "standard error" in why and st["se_F_nits"] >= st["F_nits"] > 0


def test_raw_floor_from_a_run_and_its_refusals(tmp_path: Path):
    good = ba.raw_floor_from_run(_raw_run(tmp_path, "20260923_120740_186046_hdr_x", _ramp(0.005),
                                          white_xyz=[95.0, 100.0, 105.0]), role="r", meter_floor=0.0)
    assert good.available and good.nits == pytest.approx(0.005, abs=1e-5)
    assert good.source.startswith("raw run 20260923_120740 (r;")
    assert good.stats["run"] == "20260923_120740_186046_hdr_x" and good.stats["n"] == 5
    assert good.white_xy == pytest.approx((0.95 / 3, 1 / 3)) and "native white" in good.white_source
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


def test_a_recorded_raw_floor_must_match_the_scored_runs_identity(tmp_path: Path):
    """Review #6: a raw floor from another run counts only for the same display / EDID / mode / correction."""
    expect = {"display": _PA["display"], "hardware_id": _PA["hardware_id"], "mode": "HDR",
              "correction": ba.run_identity({"mode": "HDR", "calib": {"stages": {
                  "preflight": _preflight(ccmx=_PA["ccmx"])}}})["correction"]}
    assert expect["correction"] == "pa_hdr.ccmx"                    # the file's name, path- and case-normalised
    same = _raw_run(tmp_path, "same", _ramp(0.004), ident={**_PA, "ccmx": "C:/moved/PA_HDR.ccmx"})
    got = ba.raw_floor_from_run(same, role="r", expect=expect)
    assert got.available and got.stats["identity_unverified"] == []
    for name, ident, needle in (("other_panel", {**_PA, "hardware_id": "BNQ7F5A"}, "EDID hardware id 'BNQ7F5A'"),
                                ("other_ccmx", {**_PA, "ccmx": "BenQ.ccmx"}, "colorimeter correction 'benq.ccmx'"),
                                ("other_name", {**_PA, "display": "BenQ PD2700U"}, "display 'BenQ PD2700U'")):
        got = ba.raw_floor_from_run(_raw_run(tmp_path, name, _ramp(0.004), ident=ident), role="r", expect=expect)
        assert not got.available and "identity mismatch" in got.reason and needle in got.reason, got.reason
    # an identity field unknown on the raw run does not refuse, but is listed
    got = ba.raw_floor_from_run(_raw_run(tmp_path, "bare", _ramp(0.004)), role="r", expect=expect)
    assert got.available and set(got.stats["identity_unverified"]) == {"display", "hardware_id", "correction"}


def test_floor_sourcing_order_with_raw_fits():
    r = ba.resolve_black_floor
    fit = ba.RawFloorFit(0.0052, "raw run A (own)", None, {"n": 7})
    refused = ba.RawFloorFit(None, "raw run B (stack)", "raw run B: 2 lit native grey(s)")
    # explicit beats a raw fit
    got = r(explicit=0.004, raw=[fit], recorded=0.0, peak_nits=1000)
    assert (got.nits, got.fit, got.skipped) == (0.004, None, ())
    # the first AVAILABLE raw fit wins over the recorded display black; refusals before it are listed
    got = r(raw=[refused, fit], recorded=0.0, recorded_source="DIP", peak_nits=1000)
    assert (got.nits, got.source, got.fit) == (0.0052, "raw run A (own)", {"n": 7})
    assert got.skipped == ("raw run B: 2 lit native grey(s)",)
    d = got.as_dict()
    assert d["floor_fit"] == {"n": 7} and d["floor_sources_skipped"] == list(got.skipped)
    # all raw fits refused: the recorded display black, with the refusals listed
    got = r(raw=[refused], recorded=0.0, recorded_source="DIP", peak_nits=1000)
    assert (got.nits, got.source, got.fit) == (0.0, "DIP", None) and len(got.skipped) == 1
    # nothing usable: unavailable, and the reason names the raw refusals
    got = r(raw=[refused], peak_nits=1000)
    assert not got.available and "no recorded display black" in got.source and "2 lit" in got.source
    assert "floor_fit" not in got.as_dict()


def _orchestrator(tmp_path: Path, name: str, *, flow: str, dip=None, **kw):
    from dlc.dip import DisplayInstrumentProfile

    c = _hdr_calib(tmp_path, name, **kw)
    d = dip or DisplayInstrumentProfile(display="synthetic", mode="HDR", native_black_nits=0.0,
                                        noise_floor_nits=0.0, made="2026-06-19")
    c._hdr_target, c._dip = (lambda: SimpleNamespace(peak_nits=1500.0)), (lambda: d)
    c.calib["flow"] = flow
    return c


def _register(tmp_path: Path, calib, run_id: str):
    from dlc import stack_registry

    reg = stack_registry.StackRegistry.load(tmp_path / stack_registry.REGISTRY_FILE)
    reg.record(stack_registry.StackRecord(display=calib.display.name, mode="HDR", monitor=0, run_id=run_id,
                                          applied_at="2026-09-24"))


def test_the_orchestrator_prefers_raw_stages_in_order(tmp_path: Path):
    # 1. this run's own raw stage (its white is the pedestal colour)
    own = _orchestrator(tmp_path, "20261009_100000_000001_own", flow="full")
    raw_ti3 = _write_ti3(own.ctx.root / "measurements" / "raw.ti3", _ramp(0.006))
    own.calib["stages"]["measure:raw"] = {"stage": "measure:raw", "status": "done",
                                          "data": {"ti3": str(raw_ti3), "white_xyz": [95.0, 100.0, 105.0]}}
    got = own._score_black_floor()
    assert got.nits == pytest.approx(0.006, abs=1e-5) and "this run's raw stage" in got.source
    assert got.fit["n"] == 5 and got.as_dict()["floor_fit"]["run"] == own.ctx.root.name
    # the DIP's noise_floor_nits 0 is NOT a floor: the read evidence's fallback, the same value on both
    assert got.fit["meter_floor_nits"] == METER_FLOOR_NITS_FALLBACK and not got.fit["meter_floor_measured"]
    assert "DIP noise_floor_nits (made 2026-06-19) 0.0 is not a measured floor" in got.fit["meter_floor_source"]
    own._transfer, own._spec = (lambda: SimpleNamespace(max_cv=1023)), (lambda: SimpleNamespace(is_hdr=True))
    ev = own._verify_read_evidence(str(own.ctx.root / "measurements" / "verify.ti3"), [])
    assert (ev.noise_floor_nits, ev.noise_floor_source) == (got.fit["meter_floor_nits"],
                                                            got.fit["meter_floor_source"])
    assert got.pedestal[0] == pytest.approx((0.95 / 3, 1 / 3)) and "native white" in got.pedestal[1]
    # the explicit option still wins
    own.calib["score_black_floor_nits"] = 0.004
    assert own._score_black_floor().nits == 0.004
    # 2. a verify-only run keeps the installed stack: its training run (registry) -- its MHC run measured raw
    vo = _orchestrator(tmp_path, "vo", flow="verify-only")
    assert vo._score_black_floor().nits == 0.0                            # no registry yet: the DIP's 0
    _register(tmp_path, vo, "20260924_132412_000000_mhc")
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


def test_a_full_run_never_borrows_the_previous_stack_under_the_installed_label(tmp_path: Path):
    """Review #6: a full / mhc-only run built its OWN MHC from its own raw. If that raw is refused, the registry
    still names the PREVIOUS stack (the registry records this run only at apply): that run may be used only as
    exactly that, never as "the installed MHC's applying run" / "the installed stack's training run"."""
    full = _orchestrator(tmp_path, "20261009_110000_000002_full", flow="full")
    raw_ti3 = _write_ti3(full.ctx.root / "measurements" / "raw.ti3", _ramp(-0.002))   # own raw: no lift
    full.calib["stages"]["measure:raw"] = {"stage": "measure:raw", "status": "done", "data": {"ti3": str(raw_ti3)}}
    _register(tmp_path, full, "20260920_090000_000000_prev")
    _raw_run(tmp_path, "20260920_090000_000000_prev", _ramp(0.0045))
    got = full._score_black_floor()
    assert got.nits == pytest.approx(0.0045, abs=1e-5)
    assert "a previously applied stack's run, NOT this run's stack (this full run built its own MHC)" in got.source
    assert "installed" not in got.source and "this run's raw stage" in got.skipped[0]
    # the previous run is of another panel: refused with the reason, then the DIP's recorded black
    full.calib["stages"]["preflight"] = _preflight(hardware_id="AUS322A")
    _raw_run(tmp_path, "20260920_090000_000000_prev", _ramp(0.0045), ident={"hardware_id": "BNQ7F5A"})
    got = full._score_black_floor()
    assert got.nits == 0.0 and "DIP native_black_nits" in got.source
    assert any("identity mismatch" in s and "BNQ7F5A" in s for s in got.skipped)
    # the offline rescore labels a stack record of a full run the same way
    _raw_run(tmp_path, "rec_full", _ramp(-0.002), flow="full", extra_calib={"installed_stack": {"run_id": "prev2"}})
    _raw_run(tmp_path, "prev2", _ramp(0.004))
    fits = cs.recorded_raw_floor_fits(tmp_path / "rec_full",
                                      json.loads((tmp_path / "rec_full" / "dlc_state.json").read_text()))
    assert fits[-1].available and "NOT this run's stack" in fits[-1].source


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
    # a recorded run of another panel is refused offline too
    _raw_run(tmp_path, "mhc_other", _ramp(0.0055), ident={**_PA, "hardware_id": "BNQ7F5A"})
    scored = {"mode": "HDR", "calib": {"stages": {"preflight": _preflight(**_PA)},
                                       "installed_stack": {"run_id": "mhc_other"}}}
    fits = cs.recorded_raw_floor_fits(vo, scored)
    assert not fits[0].available and "identity mismatch" in fits[0].reason


# ---------------------------------------------------------------------------------------------
# the pedestal score
# ---------------------------------------------------------------------------------------------
_FLOOR = 0.005
_PEAK = 1000.0


def _grey(nits: float) -> tuple[float, float, float]:
    s = _pq.oetf_norm(nits / _pq.CONTAINER_NITS)
    return (s, s, s)


def _ideal(rgb) -> np.ndarray:
    from dlc.engine.model import Target, TargetSpace

    return TargetSpace(Target.hdr_rec2020_pq(white_xy=_D65)).ideal_xyz(np.asarray([rgb], float))[0]


def _xyz(xy, y) -> tuple[float, float, float]:
    return (xy[0] / xy[1] * y, y, (1 - xy[0] - xy[1]) / xy[1] * y)


def _metrics(rows, *, peak=_PEAK):
    metrics, _ = score_samples_hdr([Ti3Sample(rgb, xyz) for _n, rgb, xyz in rows], white_xy=_D65, peak_nits=peak)
    return {n: m for (n, _r, _x), m in zip(rows, metrics)}, metrics


def _set(*, black_on_pedestal: bool = False):
    """(name, rgb, measured XYZ): the floor's situations, one unique signal each, scored by the production HDR
    scorer. The pedestal is white at D65 (the floor's default colour) and _FLOOR nit. ``black_on_pedestal`` adds a
    second read of the black signal showing exactly the pedestal."""
    ped = np.array(_xyz(_D65, _FLOOR))
    rows = [("black", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))]                          # reaches true black
    if black_on_pedestal:
        rows.append(("black_on_pedestal", (0.0, 0.0, 0.0), tuple(ped)))            # shows exactly the pedestal
    g1 = _grey(0.002)
    rows.append(("floor_raised", g1, tuple(_ideal(g1) + ped)))                     # target + pedestal
    g3 = _grey(0.003)
    toe = float(ba.bt2390_black_lift([0.003], min_nits=_FLOOR, source_white_nits=_PEAK)[0])
    rows.append(("lifted_to_toe", g3, tuple(_ideal(g3) * toe / 0.003)))            # a defect lifting to the toe
    g2 = _grey(0.02)
    rows.append(("crushed", g2, (0.0, 0.0, 0.0)))                                 # below its own target
    g4 = _grey(1.0)
    rows.append(("mid", g4, tuple(_ideal(g4) * 1.1)))
    red = (0.45, 0.2, 0.2)
    rows.append(("colour", red, tuple(_ideal(red) * np.array([1.05, 1.0, 0.97]))))
    return _metrics(rows)


def _content():
    rng = np.random.default_rng(5)
    n = 2500
    itp = np.column_stack([rng.uniform(0.0, 0.3, n), rng.normal(0.0, 0.01, n), rng.normal(0.0, 0.01, n)])
    w = 1.0 / (1.0 + 20.0 * itp[:, 0])                                           # dark-heavy, like the survey
    return cs.ContentDistribution(name="fixture", variant="json", itp=itp, w=w, source="mem", fingerprint="x")


def _floor(nits=_FLOOR, source="explicit option (test)", peak=_PEAK) -> ba.BlackFloor:
    return ba.resolve_black_floor(explicit=nits, explicit_source=source, peak_nits=peak)


def test_pedestal_band_scoring():
    from dlc.engine.model import TargetSpace, de_itp

    named, metrics = _set(black_on_pedestal=True)
    per = ba.black_aware_patch_scores(metrics, _floor(), white_xy=_D65)
    lim = dict(zip(named, per["floor_limited"]))
    assert lim == {"black": True, "black_on_pedestal": True, "floor_raised": True, "lifted_to_toe": True,
                   "crushed": True, "mid": False, "colour": False}      # descriptive: PQ target Y < 10 x 0.005 nit
    e = dict(zip(named, per["e_black_aware"]))
    band = dict(zip(named, per["e_bt2390_band"]))
    raw = {k: m.de2000 for k, m in named.items()}
    assert e["black"] == 0.0 and raw["black_on_pedestal"] > 5.0 and e["black_on_pedestal"] < 1e-3
    assert raw["floor_raised"] > 2.0 and e["floor_raised"] < 1e-3            # the pedestal itself: allowed
    assert e["crushed"] == raw["crushed"]                                        # a crush keeps its raw error
    # review #1: a defect lifting near black to the BT.2390 toe (several x the floor) is CHARGED; the
    # superseded band forgave it
    assert band["lifted_to_toe"] < 1e-6 and e["lifted_to_toe"] > 3.0
    # never above raw, and never further from it than the pedestal segment's own length in ITP (the triangle
    # inequality): at 1 nit a 10 %-high grey gains at most what 0.005 nit of white is worth there
    tgt = np.array([m.target_xyz for m in metrics])
    ped = np.array(_xyz(_D65, _FLOOR))
    seg = np.max([de_itp(TargetSpace.xyz_to_ictcp(tgt + t * ped) - TargetSpace.xyz_to_ictcp(tgt))
                  for t in np.linspace(0, 1, 65)], axis=0)
    rawv = np.array([m.de2000 for m in metrics])
    assert np.all(per["e_black_aware"] <= rawv + 1e-12) and np.all(rawv - per["e_black_aware"] <= seg + 1e-9)
    assert 0.0 < raw["mid"] - e["mid"] < 0.25 and 0.0 <= raw["colour"] - e["colour"] < 0.05
    assert np.all((per["pedestal_y"] >= 0) & (per["pedestal_y"] <= _FLOOR + 1e-12))
    assert per["d0_vs_raw_max"] < 1e-9                                           # d(target) IS the raw error


def test_the_pedestal_is_charged_in_any_other_colour():
    """Review #3: Rec.2020 blue [0, 0, 0.10] lifted along its OWN chroma sat inside the superseded Y-at-fixed-chroma
    band and was forgiven ~18 dE_ITP. The pedestal adds only white light, so the lifted blue keeps its error."""
    blue = (0.0, 0.0, 0.10)
    tgt = _ideal(blue)
    floor = 0.00532
    toe = float(ba.bt2390_black_lift([tgt[1]], min_nits=floor, source_white_nits=1729.26)[0])
    named, metrics = _metrics([("blue_lifted", blue, tuple(tgt * toe / tgt[1])),
                               ("blue_plus_pedestal", blue, tuple(tgt + np.array(_xyz(_D65, floor))))], peak=1729.26)
    per = ba.black_aware_patch_scores(metrics, _floor(floor, peak=1729.26), white_xy=_D65)
    raw = [m.de2000 for m in metrics]
    assert raw[0] > 10.0 and per["e_bt2390_band"][0] < 1e-6                     # the superseded band: forgiven
    assert per["e_black_aware"][0] > 0.9 * raw[0]                                # the pedestal model: charged
    assert raw[1] > 1.0 and per["e_black_aware"][1] < 1e-3                       # blue + the white pedestal: allowed


def test_the_score_is_continuous_across_the_old_cutoff():
    """Review #2: at a 0.00532-nit floor a 0.0530-nit grey reading 0.085 scored 0.00 (inside the 10x cutoff) while
    a 0.0534-nit grey reading the same scored 7.69 (outside). No cutoff now: both are charged alike."""
    floor = 0.00532
    rows = [(f"g{t}", _grey(t), _xyz(_D65, 0.085)) for t in (0.0530, 0.0534)]
    named, metrics = _metrics(rows, peak=1729.26)
    per = ba.black_aware_patch_scores(metrics, _floor(floor, peak=1729.26), white_xy=_D65)
    e = per["e_black_aware"]
    assert list(per["floor_limited"]) == [True, False]                          # straddles the descriptive class
    assert e[0] > 5.0 and e[1] > 5.0 and abs(e[0] - e[1]) < 0.2


def test_true_black_is_reachable_below_the_floor_and_a_crush_above_it_is_charged():
    """Fix 1 (2026-10-09): the panel's near-black output is {0 (LEDs off)} U [F, ...), so a target below the floor
    cannot be shown lit at its level and reaching true black costs nothing. The segment alone charged it as a
    crush (D1: the LEDs-off grey, target 0.00026 nit, read 0: 2.11 dE_ITP). At or above F the target is reachable,
    so a read of 0 there stays a crush."""
    from dlc.engine.model import TargetSpace, de_itp

    blue = (0.0, 0.0, 0.02)
    rows = [("below_reads_black", _grey(0.002), (0.0, 0.0, 0.0)),              # target < F: reached black
            ("below_reads_between", _grey(0.004), _xyz(_D65, 0.001)),          # target < F: min(segment, black)
            ("below_on_pedestal", _grey(0.003), tuple(_ideal(_grey(0.003)) + np.array(_xyz(_D65, _FLOOR)))),
            ("above_reads_black", _grey(0.008), (0.0, 0.0, 0.0)),              # target >= F: a crush
            ("blue_below_reads_black", blue, (0.0, 0.0, 0.0)),                 # any colour below the floor
            ("black", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))]
    named, metrics = _metrics(rows)
    per = ba.black_aware_patch_scores(metrics, _floor(), white_xy=_D65)
    ty = dict(zip(named, per["target_y"]))
    assert ty["blue_below_reads_black"] < _FLOOR <= ty["above_reads_black"]
    reach = dict(zip(named, per["black_reachable"]))
    assert reach == {"below_reads_black": True, "below_reads_between": True, "below_on_pedestal": True,
                     "above_reads_black": False, "blue_below_reads_black": True, "black": True}
    e = dict(zip(named, per["e_black_aware"]))
    vb = dict(zip(named, per["scored_vs_black"]))
    raw = {k: m.de2000 for k, m in named.items()}
    # reaching black below the floor costs nothing (the segment alone kept the raw crush error)
    assert raw["below_reads_black"] > 1.0 and e["below_reads_black"] == 0.0 and vb["below_reads_black"]
    assert raw["blue_below_reads_black"] > 1.0 and e["blue_below_reads_black"] == 0.0
    # at or above the floor the target is reachable: a read of 0 stays a crush, raw bit for bit
    assert e["above_reads_black"] == raw["above_reads_black"] > 1.0 and not vb["above_reads_black"]
    # between black and the target: the nearer of the segment and black, black computed independently
    meas = np.array(named["below_reads_between"].measured_xyz)[None, :]
    d_black = float(de_itp(TargetSpace.xyz_to_ictcp(meas) - TargetSpace.xyz_to_ictcp(np.zeros((1, 3))))[0])
    assert e["below_reads_between"] == pytest.approx(min(d_black, raw["below_reads_between"]), abs=1e-9)
    # the pedestal itself is still the nearest point there (not black), and true black on a black target is 0
    assert e["below_on_pedestal"] < 1e-3 and not vb["below_on_pedestal"] and e["black"] == 0.0 and not vb["black"]
    ped = dict(zip(named, per["pedestal_y"]))
    assert ped["below_reads_black"] == 0.0 and ped["below_on_pedestal"] == pytest.approx(_FLOOR, rel=0.05)
    assert np.all(per["e_black_aware"] <= np.array([m.de2000 for m in metrics]) + 1e-12)
    # the block counts them
    out = practical_summary(metrics, is_hdr=True, content=[_content()], white_xy=_D65, black_floor=_floor())
    block = out["content_weighted"]["black_aware"]
    assert block["n_black_reachable_signals"] == 5 and block["n_signals_scored_vs_black"] == sum(vb.values())
    assert "{0 (LEDs off)} U [F" in block["black_reachable_rule"] and "true black" in block["scoring_rule"]
    rows_vb = {tuple(r["rgb"]) for r in block["per_signal"] if r["scored_vs_black"]}
    assert tuple(round(c, 4) for c in named["below_reads_black"].rgb) in rows_vb


def test_signals_below_the_meter_floor_are_flagged_reported_and_still_scored():
    """Fix 2 (2026-10-09): a signal whose target AND read are below the meter floor (the read evidence's) is
    flagged below_meter_floor and reported (count + content-weight share), never dropped from the score."""
    named, metrics = _set()
    weights = ContentWeights({signal_key(m.rgb): 1.0 for m in metrics}, label="file")
    ev = ReadEvidence(noise_floor_nits=0.01, noise_floor_source="DIP noise_floor_nits (test)")
    out = practical_summary(metrics, is_hdr=True, content=[_content()], content_weights=weights, white_xy=_D65,
                            black_floor=_floor(), read_evidence=ev)
    cw = out["content_weighted"]
    bmf = cw["black_aware"]["below_meter_floor"]
    # black (0 / 0) and floor_raised (0.002 / ~0.007): below 0.01; lifted_to_toe reads above it, crushed's
    # target is above it
    flagged = {tuple(r["rgb"]) for r in cw["black_aware"]["per_signal"] if r["below_meter_floor"]}
    assert flagged == {tuple(round(c, 4) for c in named[k].rgb) for k in ("black", "floor_raised")}
    assert (bmf["meter_floor_nits"], bmf["meter_floor_source"]) == (0.01, "DIP noise_floor_nits (test)")
    assert (bmf["n_signals"], bmf["n_reads"]) == (2, 2) and "same_as_floor_fit" not in bmf     # explicit floor
    share = cw["classes"]["fixture"]["black_aware"]["evidence"]["below_meter_floor"]
    assert bmf["content_share_by_class"]["fixture"] == {"content_share_pct": share["content_share_pct"],
                                                        "black_aware_score_share_pct": share["score_share_pct"]}
    assert share["n_signals"] == 2 and share["content_share_pct"] > 0
    assert bmf["patch_weights"]["weight_share_pct"] == round(100 * 2 / 6, 2)
    # still scored: the flagged signals are in the score, with their black-aware E
    per = ba.black_aware_patch_scores(metrics, _floor(), white_xy=_D65)          # one read per signal here
    assert cw["patch_weights"]["black_aware"]["score"] == round(float(np.mean(per["e_black_aware"])), 3)
    assert all("E_black_aware" in r for r in cw["black_aware"]["per_signal"] if r["below_meter_floor"])
    # the report states it
    from dlc.calibrate import _render_practical_html

    html = _render_practical_html(out, "dE_ITP")
    assert "Near-black evidence" in html and "2 below the 0.01-nit meter floor" in html


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
    assert ba_res["vs_bt2390_band"]["score"] is not None and "superseded" in ba_res["vs_bt2390_band"]["label"]
    head = cw["headline"]
    assert head["black_aware"] is True and head["score"] == ba_res["score"] and head["score_raw"] == res["score"]
    assert "BLACK-AWARE" in head["label"] and "0.005 nit" in head["label"] and "explicit option (test)" in head["label"]
    assert head["black_floor_nits"] == _FLOOR and head["n_floor_limited_signals"] == 4
    assert head["black_pedestal_source"] == ba.PEDESTAL_ASSUMED
    block = cw["black_aware"]
    assert block["applied"] and block["floor_limited"]["raw"]["n"] == 4 and block["n_signals"] == 6
    assert block["floor_limited"]["black_aware"]["avg"] < block["floor_limited"]["raw"]["avg"]
    assert set(block["floor_limited"]) == {"raw", "black_aware", "vs_bt2390_band", "vs_toe_point"}
    cont = block["continuity"]                         # the 10 %-high 1-nit grey: the largest change above 1 nit
    assert cont["target_ge_1_nit"]["at"]["rgb"] == [round(c, 4) for c in named["mid"].rgb]
    assert 0.0 < cont["target_ge_1_nit"]["max_abs_change_dEITP"] < 0.25 and cont["greys_target_ge_1_nit"]["n"] == 1
    assert [r["target_Y"] for r in block["per_signal"]] == sorted(r["target_Y"] for r in block["per_signal"])
    assert res["evidence"]["floor_limited"]["n_signals"] == 4                    # the raw score's share on them
    zones = {r["zone"] for r in ba_res["top_contributors"]}
    assert zones <= {"floor_limited", "core", "limits", "clamped"}
    pw = cw["patch_weights"]
    assert pw["score"] == plain["content_weighted"]["patch_weights"]["score"]
    assert pw["black_aware"]["score"] < pw["score"] and pw["weight_share"]["floor_limited"] == round(4 / 6, 4)
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
    assert "BLACK-AWARE" in html and f"(raw {res['score']})" in html and "Floor-limited signals (descriptive" in html
    assert "superseded BT.2390-band variant" in html and "pedestal colour" in html


def test_every_number_beside_a_black_aware_headline_is_on_its_basis(monkeypatch):
    """Review #4: the patch-weights headline paired the black-aware score with the RAW basis' bias-corrected
    variant and weak share; the seam lead did not say which basis they were on."""
    from dlc import metrics as metrics_mod
    from dlc.calibrate import _content_lead_text
    from dlc.metrics import ReadEvidence

    named, metrics = _set()
    weights = ContentWeights({signal_key(m.rgb): 1.0 for m in metrics}, label="file")
    monkeypatch.setattr(metrics_mod, "_signal_noise",
                        lambda groups, evidence, **_k: ([{"se": 0.8, "basis": "test"} for _ in groups],
                                                        ["test"] * len(groups)))
    # single reads on the dark signals, three on the bright ones: the weak share depends on the basis
    reads = {signal_key(m.rgb): (1 if m.target_xyz[1] < 0.05 else 3) for m in metrics}
    out = practical_summary(metrics, is_hdr=True, content_weights=weights, white_xy=_D65, black_floor=_floor(),
                            read_evidence=ReadEvidence(reads=reads))
    cw = out["content_weighted"]
    pw, pb, head = cw["patch_weights"], cw["patch_weights"]["black_aware"], cw["headline"]
    assert head["black_aware"] is True and head["score"] == pb["score"] and head["score_raw"] == pw["score"]
    assert head["score_bias_corrected"] == pb["score_bias_corrected"] != pw["score_bias_corrected"]
    assert head["weak_evidence_score_share_pct"] == round(100 * pb["score_share"]["weak"], 1)
    assert pb["score_share"]["weak"] != pw["score_share"]["weak"]
    assert head["noise_limited_weight_share_pct"] == round(100 * pb["weight_share"]["noise_limited"], 1)
    assert pb["weight_share"]["noise_limited"] != pw["weight_share"]["noise_limited"]
    lead = _content_lead_text({"content_weighted": head})
    assert "noise bias-corrected variant (black-aware basis)" in lead and "% of it (black-aware basis)" in lead


def test_the_dashboard_labels_the_black_aware_headline():
    """Review #4: the live dashboard showed the black-aware headline as plain 'content-weighted', with no floor and
    no raw number."""
    assets = Path(__file__).resolve().parents[1] / "src" / "dlc" / "dashboard" / "assets"
    js = (assets / "dashboard.js").read_text(encoding="utf-8")
    page = (assets / "index.html").read_text(encoding="utf-8")
    for el in ("de-cw-raw", "de-cw-floor", "de-cw-raw-row", "de-cw-floor-row"):
        assert f'id="{el}"' in page and f'"{el}"' in js
    for ref in ("cw.black_aware", "cw.score_raw", "cw.black_floor_nits", "cw.black_floor_source", "BLACK-AWARE"):
        assert ref in js


@pytest.mark.parametrize("floor,is_hdr,why", [
    (ba.BlackFloor(None, "unavailable: no recorded display black and no explicit floor option", _PEAK), True,
     "unavailable"),
    (ba.BlackFloor(0.0, "DIP native_black_nits", _PEAK), True, "pedestal is empty"),
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


def test_without_a_peak_the_score_applies_and_the_variants_are_skipped():
    _named, metrics = _set()
    out = practical_summary(metrics, is_hdr=True, content=[_content()], white_xy=_D65,
                            black_floor=_floor(peak=None))
    cw = out["content_weighted"]
    assert cw["headline"]["black_aware"] is True and "vs_bt2390_band" not in cw["classes"]["fixture"]["black_aware"]
    assert "source white" in cw["black_aware"]["variants_unavailable"]
    assert set(cw["black_aware"]["floor_limited"]) == {"raw", "black_aware"}


def test_without_a_floor_the_block_is_unchanged():
    _named, metrics = _set()
    out = practical_summary(metrics, is_hdr=True, content=[_content()], white_xy=_D65)
    cw = out["content_weighted"]
    assert "black_aware" not in cw and "black_aware" not in cw["headline"]
    assert "floor_limited" not in cw["classes"]["fixture"]["evidence"]


@pytest.mark.slow
def test_recorded_d1_run_black_aware_floor_sources():
    """Opt-in: the owner's recorded D1 HDR run + HDR live-action histogram (local, gitignored).

    * By default the floor is the native near-black floor of the installed stack's training run's raw stage (D1
      is a verify-only run; its stack is run 20260924_132412, a full run, same display / EDID / correction): the
      intercept over its 5 lit greys <= 0.05 nit (0.98 % .. 4.11 %), F 0.00235 nit, g 1.120 (the old median rule
      read 0.00532: the 0.03-0.26-nit greys' tone error). The pedestal colour is the raw run's native white.
    * The meter floor is the read evidence's: the DIP's noise_floor_nits is 0 (NOT measured), so the 0.05-nit
      fallback, the same value in the read evidence, the raw fit (its 5 greys flagged below it, none dropped) and
      the block. 5 verify signals sit below it, target and read (17.6 % of the content), still scored.
    * True black is reachable below the floor (3 signals with a PQ target < F): the LEDs-off grey (signal 0.29 %,
      target 0.00026 nit, read 0) costs 0 instead of 2.11, the only signal that changes. The headline is
      black-aware 1.335 against raw 1.68 (1.427 before black was reachable); the superseded BT.2390-band variant
      1.459, the literal toe point 2.322.
    * Continuity: greys >= 1 nit move < 0.0002 dE_ITP. A saturated colour keeps a near-black channel, so the white
      pedestal still moves its chroma above 1 nit (a 1.7-nit Rec.2020 green: 0.167), < 0.03 above 10 nit.
    * A stated floor moves the score (the superseded band barely depended on it): 0.006 nit -> 1.196.
    * Without any raw source (the explicit option aside), the DIP's full-field black (0.0 on this local-dimming
      panel) lifts nothing, so the headline stays raw."""
    hist = _STUDY / "content_hist_hdr_live.npz"
    run = _RUNS / _D1
    if not (hist.is_file() and (run / "reports" / "verification_iter00_patch_metrics.json").is_file()):
        pytest.skip("local study data / recorded run absent (set DLC_PRACTICAL_STUDY / DLC_PRACTICAL_STUDY_RUNS)")
    default = cs.rescore_run(run, [str(hist)], reach=20.0)["content_weighted"]
    head, block = default["headline"], default["black_aware"]
    assert head["black_aware"] is True and head["score_raw"] == 1.68 and head["score"] == 1.335
    assert block["floor_nits"] == 0.00235 and block["floor_source"].startswith(
        "raw run 20260924_132412 (the installed stack's training run;")
    assert "display floor 0.00235 nit from raw run 20260924_132412" in head["label"]
    fit = block["floor_fit"]
    assert fit["run"] == "20260924_132412_307436_hdr_asus_proart_pa32ucxr" and fit["n"] == 5
    assert (fit["F_nits"], fit["g"], fit["window_nits"]) == (0.00235, 1.12044, 0.05)
    assert fit["signal_range_pct"] == [0.9775, 4.1056] and fit["se_F_nits"] < fit["F_nits"]
    assert fit["identity_unverified"] == [] and "dip_store.json" in fit["meter_floor_source"]
    # one meter floor: the read evidence's fallback (the DIP's 0 is not a measurement), in all three places
    assert (fit["meter_floor_nits"], fit["meter_floor_measured"], fit["n_below_meter_floor"]) == (0.05, False, 5)
    assert "0.0 is not a measured floor" in fit["meter_floor_source"]
    assert (default["evidence"]["noise_floor_nits"], default["evidence"]["noise_floor_source"]) == (
        fit["meter_floor_nits"], fit["meter_floor_source"])
    bmf = block["below_meter_floor"]
    assert (bmf["meter_floor_nits"], bmf["meter_floor_source"]) == (fit["meter_floor_nits"], fit["meter_floor_source"])
    assert bmf["same_as_floor_fit"] is True and (bmf["n_signals"], bmf["n_reads"]) == (5, 5)
    assert bmf["content_share_by_class"]["hdr_live"]["content_share_pct"] == 17.6
    # true black reachable below the floor: only the LEDs-off grey changes
    assert (block["n_black_reachable_signals"], block["n_signals_scored_vs_black"]) == (3, 1)
    off = [r for r in block["per_signal"] if r["scored_vs_black"]]
    assert len(off) == 1 and off[0]["rgb"] == [0.0029] * 3 and off[0]["measured_Y"] == 0.0
    assert off[0]["E_raw"] == pytest.approx(2.1109, abs=1e-4) and off[0]["E_black_aware"] == 0.0
    assert block["pedestal_source"].startswith("raw run 20260924_132412's native white")
    cls = default["classes"]["hdr_live"]["black_aware"]
    assert cls["vs_bt2390_band"]["score"] == 1.459 and cls["vs_toe_point"]["score"] == 2.322
    cont = block["continuity"]
    assert cont["greys_target_ge_1_nit"]["max_abs_change_dEITP"] < 0.01
    assert cont["target_ge_1_nit"]["max_abs_change_dEITP"] == pytest.approx(0.167, abs=0.002)
    assert cont["target_ge_10_nit"]["max_abs_change_dEITP"] < 0.03
    stated = cs.rescore_run(run, [str(hist)], reach=20.0, black_floor_nits=0.006)["content_weighted"]
    assert stated["headline"]["score"] == 1.196 and stated["headline"]["black_floor_source"] == (
        "explicit option (--black-floor-nits)")
    import unittest.mock as um

    with um.patch.object(cs, "recorded_raw_floor_fits", lambda *_a, **_k: []):
        no_raw = cs.rescore_run(run, [str(hist)], reach=20.0)["content_weighted"]
    assert no_raw["headline"]["score"] == 1.68 and no_raw["headline"]["black_aware"] is False
    assert no_raw["black_aware"]["floor_nits"] == 0.0 and "panel_limits" in no_raw["black_aware"]["floor_source"]
