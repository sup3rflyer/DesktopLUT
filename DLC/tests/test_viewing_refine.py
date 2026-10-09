"""--thermal-state viewing, policy ``viewing-refine`` (owner decision 2026-10-09): the MHC closed-loop refine runs
in the VIEWING state — its own thermal-state seam, the viewing-load precondition, and a HOLD of its reads
(dim-neutral dwells between ~45 s blocks, bounded by an LLM-chosen budget) — while raw + the cube build stay at
their own (loaded) band and the verify is practical. Sim only: :class:`TimedThermalPanel` is the model's own
physics on a simulated clock (no display, no meter, no wall time).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasureLoopConfig, run_measure_loop
from dlc.viewing_thermal import (HOLD_BLOCK_S, HOLD_DWELL_NITS, LoadLaw, ViewingPrecondition, dwell_seconds,
                                 predict_refine_hold, set_band)
from test_viewing_thermal import TimedThermalPanel, _Scripted

PQ = Transfer.pq(10)
LAW = LoadLaw()
STAGE = "refine-mhc-cube"


def _hot_set() -> list[tuple[int, int, int]]:
    """~4.5 min of distinct 20-60 nit near-greys: ~6x a 3-nit-eq target's load, so an UNHELD pass leaves the
    band within ~3 min (model)."""
    out = []
    for i in range(300):
        cv = PQ.nits_to_cv(20.0 + 40.0 * (i % 60) / 59.0)
        out.append((cv, cv - (i // 60), cv))
    return out


def _spec(target: float, *, hold: bool, budget_s: float = 3600.0) -> ViewingPrecondition:
    return ViewingPrecondition(target_load=target, halfwidth=0.5 * target, start_load=target, start_source="test",
                               target_source="test", deadline_s=600.0, law=LAW, hold=hold, hold_budget_s=budget_s)


class _LabelledPanel(TimedThermalPanel):
    """TimedThermalPanel that also records each read's patch label (to find the dwell reads)."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.labels: list[str] = []

    def __call__(self, patch):
        self.labels.append(patch.label)
        return super().__call__(patch)


def _main_pass_states(panel: TimedThermalPanel) -> list[float]:
    first = next(i for i, tr in enumerate(panel.trace) if tr[2] == "measurement")
    return [tr[1] for tr in panel.trace[first:]]


# --- the hold (measure loop) -------------------------------------------------------------------------
def test_dwell_formula_is_the_spec_load_balance():
    # black dwell: block_s x (block_load / target - 1); block + dwell then average to the target
    assert dwell_seconds(0.064, 45.0, 0.032, 0.0) == pytest.approx(45.0)
    d = dwell_seconds(0.08, 45.0, 0.032, 0.005)
    assert (0.08 * 45.0 + 0.005 * d) / (45.0 + d) == pytest.approx(0.032)
    assert dwell_seconds(0.02, 45.0, 0.032, 0.005) == 0.0          # a cool block needs no dwell


def test_hold_keeps_a_hot_pass_in_the_viewing_band():
    patches = _hot_set()
    target = LAW.load(3.0)
    lo, hi = 0.5 * target, 1.5 * target
    assert set_band(patches, PQ, LAW)["load"] > 4 * target

    # unheld: the set's own load pulls the panel out of the band — flagged, never silently "viewing"
    p0 = TimedThermalPanel(start_load=target)
    r0 = run_measure_loop(patches=patches, transfer=PQ, measure=p0,
                          config=MeasureLoopConfig(viewing=_spec(target, hold=False)))
    assert r0.digest["thermal_state"]["achieved"]["in_band_throughout"] is False
    assert "viewing_band_left" in r0.digest["anomaly_reasons"] and "hold" not in r0.digest["thermal_state"]

    # held: dim-neutral dwells keep the TRUE (rig) state and the model in band throughout
    p1 = _LabelledPanel(start_load=target)
    r1 = run_measure_loop(patches=patches, transfer=PQ, measure=p1,
                          config=MeasureLoopConfig(viewing=_spec(target, hold=True)))
    ts = r1.digest["thermal_state"]
    assert ts["state"] == "viewing" and ts["achieved"]["in_band_throughout"] is True
    assert all(lo - 1e-4 <= t <= hi + 1e-4 for t in _main_pass_states(p1))
    hold = ts["hold"]
    assert hold["dwells"] > 0 and hold["dwell_min"] > 0 and not hold["budget_exhausted"]
    assert ts["spec"]["hold"]["block_s"] == HOLD_BLOCK_S
    # every block that needed it got its dwell; blocks stay ~45 s (<< tau)
    assert all(rec["block_s"] <= HOLD_BLOCK_S + 10 for rec in hold["dwell_log"])
    # the dwell field: ONE steady dim neutral (<= 1 nit; no full-signal static, no toggling), read + DISCARDED
    dwell = [tr for tr, label in zip(p1.trace, p1.labels) if label == "viewing_dwell"]
    assert len(dwell) == hold["dwell_reads"] > 0
    assert {tr[3] for tr in dwell} == {tuple(hold["dwell_rgb"])}
    assert PQ.cv_to_nits(hold["dwell_rgb"][0]) <= HOLD_DWELL_NITS + 1e-6
    assert len(r1.digest.get("unresolved") or []) == 0
    measured = [tr for tr in p1.trace if tr[2] == "measurement"]
    assert len({tr[3] for tr in measured}) == len(set(patches))          # dwell reads never enter the data
    # the model agrees with the rig (same law, same clock) — the dwell reads fed it
    assert ts["final"]["modelled_load"] == pytest.approx(p1.temp, abs=2e-3)


def test_hold_budget_caps_the_dwell_and_the_band_exit_is_flagged():
    patches = _hot_set()
    target = LAW.load(3.0)
    panel = TimedThermalPanel(start_load=target)
    res = run_measure_loop(patches=patches, transfer=PQ, measure=panel,
                           config=MeasureLoopConfig(viewing=_spec(target, hold=True, budget_s=120.0)))
    hold = res.digest["thermal_state"]["hold"]
    assert hold["budget_exhausted"] is True and hold["exhausted_at_block"] is not None
    assert 0 < hold["dwell_min"] <= 2.0 + 1e-9                     # the budget is never exceeded
    assert "viewing_band_left" in res.digest["anomaly_reasons"]


def test_predicted_dwell_uses_the_same_policy_as_the_loop():
    patches = _hot_set()
    target = LAW.load(3.0)
    pred = predict_refine_hold(patches, PQ, LAW, target_load=target, halfwidth=0.5 * target, start_load=target)
    assert pred["in_band_fraction"] == 1.0 and pred["dwell_min"] > 0
    capped = predict_refine_hold(patches, PQ, LAW, target_load=target, halfwidth=0.5 * target, start_load=target,
                                 budget_s=120.0)
    assert capped["budget_exhausted"] and capped["dwell_min"] <= 2.0 and capped["in_band_fraction"] < 1.0
    panel = TimedThermalPanel(start_load=target)
    res = run_measure_loop(patches=patches, transfer=PQ, measure=panel,
                           config=MeasureLoopConfig(viewing=_spec(target, hold=True)))
    # the loop adds warm-up + neutral checkpoints (hot reads) on top of the set: a bit more dwell, same order
    got = res.digest["thermal_state"]["hold"]["dwell_min"]
    assert pred["dwell_min"] * 0.8 <= got <= pred["dwell_min"] * 1.6


# --- the orchestrator: the refine seam, the per-stage record, the report -------------------------------
def _hdr(tmp_path: Path, name: str, *, adjudicator=None, start_load: float = 0.096, **kw):
    from test_calibrate import _DATE, _OPT, _SMALL, _fake_launch
    from dlc import calibration_profile as cp
    from dlc.calibrate import Calibration
    from dlc.controller import CalibrationController
    from dlc.runs import create_run, open_run

    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() else \
        create_run("HDR", display="synthetic", run_dir=run_dir)
    panel = kw.pop("panel", None) or TimedThermalPanel(start_load=start_load, cold_blue_gain=1.0)
    calib = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / f"{name}_results")),
                        monitor=0, mode="HDR", controller=CalibrationController.mock(), measure=panel,
                        adjudicator=adjudicator or _Scripted(), optimize_config=_OPT, patch_sizes=_SMALL,
                        run_date=_DATE, probe_launcher=_fake_launch, bit_depth=10, **kw)
    return calib, panel


def test_viewing_refine_seam_precondition_hold_and_stage_states(tmp_path: Path):
    adj = _Scripted()
    calib, panel = _hdr(tmp_path, "vr", adjudicator=adj, thermal_state="viewing")
    result = calib.run("mhc-only")
    assert result.status == "completed", result.digest
    # the refine's thermal-state seam FIRES (asserted, not `if`), once, before its reads, with the hold cost
    (req,) = adj.asked(f"{STAGE}:thermal-state")
    assert req.options == ("precondition", "measure-now", "abort") and req.recommendation == "precondition"
    d = req.digest
    assert d["policy"] == "viewing-refine" and d["start"]["kind"] == "assumed-hot"
    assert "build-install-mhc" in d["start"]["unmodelled_stages"]          # raw's history is not trusted
    pred = d["predicted"]
    assert pred["precondition_minutes"] > 0 and pred["hold_dwell_total_min"] >= 0
    budget = d["hold_budget"]
    assert budget["budget_min"] == budget["default_min"] == round(pred["hold_dwell_total_min"] * 1.5 + 5, 1)
    assert "--viewing-hold-budget-min" in req.question and "Dwell budget" in req.question
    refine_reads = [i for i, tr in enumerate(panel.trace) if tr[2] == "measurement"]
    assert refine_reads                                                   # (the run measured)
    # the refine's record: policy, requested vs achieved over every round, the hold totals
    ts = calib.calib["stages"][STAGE]["digest"]["thermal_state"]
    assert ts["policy"] == "viewing-refine" and ts["state"] == "viewing" and ts["decision"] == "precondition"
    assert ts["achieved"]["all_rounds_in_band"] is True and ts["evidence_flags"] == []
    lo, hi = ts["target"]["band"]
    for r in ts["rounds"]:
        assert r["precondition"]["reached"] is True and r["achieved"]["in_band_throughout"] is True
        assert lo <= r["measure"]["modelled_range"][0] and r["measure"]["modelled_range"][1] <= hi
        assert "dwell_min" in r["hold"]
    assert ts["hold_total"]["dwell_min"] <= ts["hold_total"]["budget_min"]
    assert ts["mhc_white"].startswith("refined in the viewing band")
    # the build stage: viewing requested, NOT applied (own band, owner policy)
    raw = calib.calib["stages"]["measure:raw"]["digest"]["thermal_state"]
    assert raw["requested"] == "viewing" and raw["applied"] is False and raw["policy"] == "own"
    assert "not applied" in raw["basis"] and raw["state"] == "verify"
    # the verify says which stage ran in which state + the MHC white's state
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    rows = {r["stage"]: r for r in vd["stages"]}
    assert rows["measure:raw"]["policy"] == "own" and rows["measure:raw"]["applied"] is False
    assert rows[STAGE]["policy"] == "viewing-refine" and rows[STAGE]["state"] == "viewing"
    assert rows["measure:verify"]["policy"].startswith("practical")
    assert vd["mhc_white"] == {"stage": STAGE, "state": "viewing", "evidence_flags": []}
    # the refine's history entry is trusted by the verify (nothing unmodelled after it)
    assert calib.calib["thermal_history"][-2]["stage"] == STAGE
    assert vd["start"]["kind"] == "run-history"
    # the final report carries the stage-state summary
    report = json.loads(next((tmp_path / "vr_results").rglob("report.json")).read_text("utf-8"))
    assert [r["stage"] for r in report["thermal_state"]["stages"]] == [r["stage"] for r in vd["stages"]]
    assert report["thermal_state"]["mhc_white"]["state"] == "viewing"
    html = next((tmp_path / "vr_results").rglob("report.html")).read_text("utf-8")
    assert "Thermal state (viewing requested)" in html and "viewing-refine" in html
    # the round check-in carries the round's thermal state
    assert calib._last_refine["thermal_state"]["policy"] == "viewing-refine"


def test_measure_now_refine_is_flagged_through_to_verify_accept(tmp_path: Path):
    adj = _Scripted(**{f"{STAGE}:thermal-state": ["measure-now"]})
    calib, _ = _hdr(tmp_path, "vr_now", adjudicator=adj, thermal_state="viewing")
    assert calib.run("mhc-only").status == "completed"
    ts = calib.calib["stages"][STAGE]["digest"]["thermal_state"]
    assert ts["decision"] == "measure-now" and ts["state"] == "outside-viewing-band"
    assert "viewing_precondition_skipped" in ts["evidence_flags"] and ts["needs_adjudication"] is True
    assert all(r["hold"] == {"policy": "none (measure-now: no hold)"} for r in ts["rounds"])
    assert not adj.asked(f"{STAGE}:thermal-miss")              # the LLM chose it knowingly
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert "viewing_precondition_skipped" in vd["mhc_white"]["evidence_flags"] and vd["needs_adjudication"] is True
    (acc,) = adj.asked("verify:accept")
    assert "MHC white: refined OUTSIDE the viewing band" in acc.question


def test_refine_band_miss_offers_remeasure_and_the_seam_reasks(tmp_path: Path):
    """A 3-nit-eq target with a ZERO dwell budget: the refine set's own load pulls the rounds out of the band.
    The miss seam offers remeasure; choosing it re-runs the refine and its thermal-state seam re-asks (start
    from this run's history); accept then keeps it, flagged through to verify:accept."""
    adj = _Scripted(**{f"{STAGE}:thermal-miss": ["remeasure", "accept"]})
    calib, _ = _hdr(tmp_path, "vr_re", adjudicator=adj, thermal_state="viewing", viewing_load_nits=3.0,
                    viewing_hold_budget_min=0.0)
    assert calib.run("mhc-only").status == "completed"
    miss = adj.asked(f"{STAGE}:thermal-miss")
    assert len(miss) == 2 and miss[0].options == ("accept", "remeasure", "abort")
    assert "remeasure" in miss[0].question and miss[0].digest["read_anomaly"] is True   # --supervised escalates
    asks = adj.asked(f"{STAGE}:thermal-state")
    assert len(asks) == 2                                             # re-asked, not replayed
    assert asks[0].digest["hold_budget"]["budget_min"] == 0.0
    assert asks[1].digest["start"]["kind"] == "run-history"
    assert calib.calib["decisions"][f"{STAGE}:thermal-miss"]["choice"] == "accept"
    ts = calib.calib["stages"][STAGE]["digest"]["thermal_state"]
    assert ts["state"] == "outside-viewing-band" and ts["evidence_flags"]
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["mhc_white"]["evidence_flags"] == ts["evidence_flags"]
    assert "MHC white: refined OUTSIDE" in adj.asked("verify:accept")[0].question


def test_refine_seam_abort_measures_nothing(tmp_path: Path):
    adj = _Scripted(**{f"{STAGE}:thermal-state": ["abort"]})
    calib, panel = _hdr(tmp_path, "vr_abort", adjudicator=adj, thermal_state="viewing")
    result = calib.run("mhc-only")
    assert result.status == "aborted"
    assert calib.calib["stages"][STAGE]["status"] == "aborted"
    assert not (calib.ctx.root / "measurements" / "refine_1.ti3").exists()   # no refine read was taken
    assert "measure:verify" not in calib.calib["stages"]


def test_given_start_answers_one_seam_and_a_budget_change_reasks(tmp_path: Path):
    """--viewing-start-nits answers ONE seam (the refine's here); the verify's start then comes from the run
    history. A new --viewing-hold-budget-min / start on resume drops the not-yet-run refine's seam decision."""
    calib, _ = _hdr(tmp_path, "vr_given", adjudicator=_Scripted(), thermal_state="viewing", viewing_start_nits=40.0)
    assert calib.run("mhc-only").status == "completed"
    ts = calib.calib["stages"][STAGE]["digest"]["thermal_state"]
    assert ts["start"]["kind"] == "given" and calib.calib["viewing_start_used_by"] == STAGE
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["start"]["kind"] == "run-history"

    # the run pauses AT the refine seam; its decision gets recorded but the refine has not run (e.g. a cancel
    # right after the answer); a resume with a new budget drops it, so the seam re-asks with the new budget
    from dlc.adjudication import AdjudicationRequired

    class _PauseAtRefine(_Scripted):
        def adjudicate(self, request):
            if request.key == f"{STAGE}:thermal-state":
                raise AdjudicationRequired(request)
            return super().adjudicate(request)

    c2, _ = _hdr(tmp_path, "vr_budget", adjudicator=_PauseAtRefine(), thermal_state="viewing")
    with pytest.raises(AdjudicationRequired):
        c2.run("mhc-only")
    assert "measure:raw" in c2.calib["stages"] and STAGE not in c2.calib["stages"]
    c2.calib["decisions"][f"{STAGE}:thermal-state"] = {"choice": "precondition", "note": "x"}
    c2._save()
    adj3 = _Scripted()
    c3, _ = _hdr(tmp_path, "vr_budget", adjudicator=adj3, thermal_state="viewing", viewing_hold_budget_min=12.0)
    assert f"{STAGE}:thermal-state" not in c3.calib["decisions"]           # dropped: its numbers changed
    assert any(ch["field"] == "viewing_hold_budget_min" for ch in c3.calib["thermal_state_changes"])
    assert c3.run("mhc-only").status == "completed"
    (req,) = adj3.asked(f"{STAGE}:thermal-state")
    assert req.digest["hold_budget"] == {**req.digest["hold_budget"], "budget_min": 12.0,
                                         "source": "--viewing-hold-budget-min"}


def test_hold_budget_knob_is_bounded(tmp_path: Path):
    with pytest.raises(ValueError, match="viewing_hold_budget_min"):
        _hdr(tmp_path, "vr_bad", thermal_state="viewing", viewing_hold_budget_min=-1.0)
    with pytest.raises(ValueError, match="viewing_hold_budget_min"):
        _hdr(tmp_path, "vr_bad2", thermal_state="viewing", viewing_hold_budget_min=1e6)


def test_default_refine_path_matches_main(tmp_path: Path):
    """verify (default, no flag) == main e0a559e for the MHC refine: the SAME reads in the same order, the same
    refine / measure / verify digests, decisions and run-record keys, in the HDR and SDR mhc-only sim flows.
    The golden was recorded by running ``tests/_refine_default_fingerprint.py`` against main e0a559e's source."""
    from _refine_default_fingerprint import fingerprint

    golden = json.loads((Path(__file__).parent / "data" / "refine_default_path_main.json").read_text("utf-8"))
    golden.pop("_recorded_from")
    got = json.loads(json.dumps(fingerprint(tmp_path), sort_keys=True))
    for scenario in ("hdr_mhc_only", "sdr_mhc_only"):
        g, h = golden[scenario], got[scenario]
        assert h["reads"] == g["reads"], f"{scenario}: the default refine path commands different reads than main"
        assert h == g, f"{scenario}: the default refine path's digests differ from main"
        assert "thermal_history" not in h["calib_keys"] and "viewing_start_used_by" not in h["calib_keys"]


def test_sdr_refine_runs_in_the_viewing_state_too(tmp_path: Path):
    """The SDR sibling (refine-mhc-grayscale) takes the same path: its seam, its record, the verify summary."""
    from test_calibrate import _DATE, _OPT, _SMALL, _fake_launch, _transfer
    from dlc import calibration_profile as cp
    from dlc.calibrate import Calibration
    from dlc.controller import CalibrationController
    from dlc.runs import create_run

    adj = _Scripted()
    panel = TimedThermalPanel(start_load=0.096, cold_blue_gain=1.0, transfer=_transfer())
    calib = Calibration(ctx=create_run("SDR", display="synthetic", run_dir=tmp_path / "sdr"),
                        profile=cp.Profile.synthetic(output_dir=str(tmp_path / "sdr_results")), monitor=0, mode="SDR",
                        controller=CalibrationController.mock(), measure=panel, adjudicator=adj, optimize_config=_OPT,
                        patch_sizes=_SMALL, run_date=_DATE, probe_launcher=_fake_launch, thermal_state="viewing")
    assert calib.run("mhc-only").status == "completed"
    stage = "refine-mhc-grayscale"
    (req,) = adj.asked(f"{stage}:thermal-state")
    assert req.digest["policy"] == "viewing-refine" and "SDR content" in req.digest["target"]["source"]
    ts = calib.calib["stages"][stage]["digest"]["thermal_state"]
    assert ts["policy"] == "viewing-refine" and ts["rounds"] and ts["decision"] == "precondition"
    rows = {r["stage"]: r for r in calib.calib["stages"]["verify"]["digest"]["thermal_state"]["stages"]}
    assert rows[stage]["policy"] == "viewing-refine" and rows["measure:raw"]["applied"] is False
