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
    mw = vd["mhc_white"]
    assert {k: mw[k] for k in ("stage", "state", "evidence_flags")} == {"stage": STAGE, "state": "viewing",
                                                                          "evidence_flags": []}
    assert mw["line"] == ts["mhc_white"] and mw["offset_from_target"]["near_target"] is True
    # the refine's reads were taken AT the target (model), not at the band's edge — and it says so
    off = ts["offset_from_target"]
    assert abs(off["mean_offset_load"]) <= off["tolerance_load"] == ts["target"]["tolerance"]["load"]
    assert ts["mhc_white"].startswith("refined in the viewing band at its target")
    (acc,) = adj.asked("verify:accept")
    assert f"MHC white: {ts['mhc_white']}" in acc.question
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


# --- review findings 2026-10-09 (each test failed before its fix) ----------------------------------------
class _PauseAt(_Scripted):
    """_Scripted that PAUSES (AdjudicationRequired) at one seam key — a live run stopping there."""

    def __init__(self, key: str, **script: list[str]) -> None:
        super().__init__(**script)
        self.key = key

    def adjudicate(self, request):
        from dlc.adjudication import AdjudicationRequired

        if request.key == self.key:
            raise AdjudicationRequired(request)
        return super().adjudicate(request)


def test_refine_remeasure_spends_the_given_start(tmp_path: Path):
    """Finding 1: the miss question promises the re-run starts from the run history, but the re-asked seam reused
    the SAME --viewing-start-nits (kind given, 40 nit) — a cool given start lets the re-run assume in-band on a
    panel the first pass just heated. A remeasure now spends it."""
    adj = _Scripted(**{f"{STAGE}:thermal-miss": ["remeasure", "accept"]})
    calib, _ = _hdr(tmp_path, "vr_spent", adjudicator=adj, thermal_state="viewing", viewing_load_nits=3.0,
                    viewing_hold_budget_min=0.0, viewing_start_nits=40.0)
    assert calib.run("mhc-only").status == "completed"
    asks = adj.asked(f"{STAGE}:thermal-state")
    assert len(asks) == 2 and asks[0].digest["start"]["kind"] == "given"
    second = asks[1].digest["start"]
    assert second["kind"] == "run-history" and "SPENT" in second["source"]
    assert calib.calib["viewing_start_spent"]["by"] == STAGE


def test_verify_escalation_remeasure_spends_the_given_start(tmp_path: Path):
    """Finding 1, the verify's escalation remeasure (its viewing miss): the re-asked thermal-state seam must not
    re-read the given start either."""
    from test_viewing_thermal import _hdr_file, _vo
    from dlc.viewing_thermal import RECORDED_VERIFY_LOAD

    pf = _hdr_file(tmp_path)
    panel = TimedThermalPanel(start_load=RECORDED_VERIFY_LOAD, cold_blue_gain=1.0)
    adj = _Scripted(**{"measure:verify:thermal-state": ["measure-now", "precondition"],
                       "measure:verify:escalation": ["remeasure"]})
    calib = _vo(tmp_path, "vo_spent", panel=panel, verify_patches_file=pf, thermal_state="viewing",
                adjudicator=adj, viewing_start_nits=40.0)
    assert calib.run("verify-only").status == "completed"
    asks = adj.asked("measure:verify:thermal-state")
    assert len(asks) == 2 and asks[0].digest["start"]["kind"] == "given"
    assert asks[1].digest["start"]["kind"] == "run-history"
    assert calib.calib["viewing_start_spent"]["by"] == "measure:verify"
    # a NEW value re-arms it (while the knobs may still change: before the verify is memoised)
    from dlc.calibrate import resolve_thermal_knobs
    rec = {"stages": {"measure:raw": {}}, "viewing_start_nits": 40.0, "viewing_start_used_by": "measure:verify",
           "viewing_start_spent": dict(calib.calib["viewing_start_spent"])}
    resolve_thermal_knobs(rec, thermal_state=None, viewing_load_nits=None, viewing_start_nits=25.0)
    assert "viewing_start_spent" not in rec and "viewing_start_used_by" not in rec
    assert rec["viewing_start_nits"] == 25.0


def test_hold_reads_a_pass_at_the_target_not_at_the_band_edge():
    """Finding 2: the soak converges at the band's 0.9 x half-width edge and the old hold only kept the state in
    band (release 0.75), so a cool refine-like set was read at the edge (~+0.4 x the target). Now the hold settles
    to the target before the first read and keeps it there (true rig state, same law + clock as the model)."""
    from dlc.viewing_thermal import CONVERGE_MARGIN

    tolerance = 0.2                                         # x half-width: viewing_thermal.HOLD_AIM_FRAC

    target = LAW.load(13.5)
    half = 0.5 * target
    patches = [(PQ.nits_to_cv(n),) * 3 for n in (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0) * 4]   # cool grey ramp
    assert set_band(patches, PQ, LAW)["load"] < target
    edge = target + CONVERGE_MARGIN * half                  # where the soak hands over (from a hot start)
    panel = TimedThermalPanel(start_load=edge)
    spec = ViewingPrecondition(target_load=target, halfwidth=half, start_load=edge, start_source="test",
                               target_source="test", deadline_s=600.0, law=LAW, hold=True, hold_budget_s=3600.0)
    res = run_measure_loop(patches=patches, transfer=PQ, measure=panel, config=MeasureLoopConfig(viewing=spec))
    at_reads = [tr[1] for tr in panel.trace if tr[2] == "measurement"]
    mean_off = sum(at_reads) / len(at_reads) - target
    assert abs(mean_off) <= tolerance * half                 # the rig's state at the reads: AT the target
    ts = res.digest["thermal_state"]
    assert ts["hold"]["settle"]["needed"] is True and ts["hold"]["settle"]["reached"] is True
    at = ts["measure"]["at_reads"]                          # the model's account agrees with the rig
    assert at["mean_offset_load"] == pytest.approx(mean_off, abs=2e-4) and at["reads"] == len(at_reads)


def test_refine_in_band_but_off_target_is_labelled_honestly(tmp_path: Path):
    """Finding 2: with no dwell budget the refine reads where the soak left it (the band's edge): in band, but
    OFF the target — never "refined in the viewing band" without qualification; the miss seam asks."""
    adj = _Scripted(**{f"{STAGE}:thermal-miss": ["accept"]})
    calib, _ = _hdr(tmp_path, "vr_off", adjudicator=adj, thermal_state="viewing", viewing_hold_budget_min=0.0)
    assert calib.run("mhc-only").status == "completed"
    ts = calib.calib["stages"][STAGE]["digest"]["thermal_state"]
    assert ts["state"] == "viewing-band-off-target" and ts["evidence_flags"] == ["viewing_off_target"]
    assert ts["achieved"]["all_rounds_in_band"] is True and ts["achieved"]["all_rounds_on_target"] is False
    off = ts["offset_from_target"]
    assert off["near_target"] is False and off["mean_offset_load"] > off["tolerance_load"]
    assert "OFF its target" in ts["mhc_white"]
    (miss,) = adj.asked(f"{STAGE}:thermal-miss")
    assert "OFF its target" in miss.question
    assert "OFF its target" in adj.asked("verify:accept")[0].question


def test_thermal_state_locks_once_the_refine_is_memoised(tmp_path: Path):
    """Finding 3: a resume could switch --thermal-state viewing -> verify after the viewing refine (the knob only
    locked at measure:verify); the verify then claimed nothing about the MHC white. Now refused, with the reason."""
    from dlc.adjudication import AdjudicationRequired

    c1, _ = _hdr(tmp_path, "vr_lock", adjudicator=_PauseAt("measure:verify:thermal-state"), thermal_state="viewing")
    with pytest.raises(AdjudicationRequired):
        c1.run("mhc-only")
    assert c1.calib["stages"][STAGE]["status"] == "done" and "measure:verify" not in c1.calib["stages"]
    c2, _ = _hdr(tmp_path, "vr_lock", adjudicator=_Scripted(), thermal_state="verify")
    result = c2.run("mhc-only")
    assert result.status == "aborted" and result.digest["aborted_at"] == "resume-args"
    assert "locked once the MHC refine is memoised" in result.digest["message"]
    assert c2.calib["thermal_state"] == "viewing" and "measure:verify" not in c2.calib["stages"]


def test_verify_always_carries_the_mhc_white_line(tmp_path: Path):
    """Finding 3: whenever a refine recorded a thermal state, the verify digest / verify:accept / the report carry
    the MHC white's state — even for a verify measured in the 'verify' state (a pre-lock run record)."""
    from dlc.adjudication import AdjudicationRequired

    c1, _ = _hdr(tmp_path, "vr_line", adjudicator=_PauseAt("measure:verify:thermal-state"), thermal_state="viewing")
    with pytest.raises(AdjudicationRequired):
        c1.run("mhc-only")
    c1.calib.pop("thermal_state")                 # a run record from before the lock: the verify runs "verify"
    c1._save()
    adj = _Scripted()
    c2, _ = _hdr(tmp_path, "vr_line", adjudicator=adj)
    assert c2.run("mhc-only").status == "completed"
    vd = c2.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["state"] == "verify" and vd["mhc_white"]["stage"] == STAGE and vd["mhc_white"]["state"] == "viewing"
    assert vd["needs_adjudication"] is True and "refined in the 'viewing' state" in vd["note"]
    q = adj.asked("verify:accept")[0].question
    assert "MHC white: refined in the viewing band" in q and "Note: the verify was measured" in q
    report = json.loads(next((tmp_path / "vr_line_results").rglob("report.json")).read_text("utf-8"))
    rows = {r["stage"]: r for r in report["thermal_state"]["stages"]}
    assert rows["measure:verify"]["policy"] == "own band (thermal-state verify)"
    assert "practical" not in report["thermal_state"]["owner_policy"]


def test_knob_change_clears_an_aborted_refines_decision(tmp_path: Path):
    """Finding 4: abort at the refine seam, resume with a new start -> the recorded 'abort' replayed (the knob rule
    only cleared refines NOT in stages). Now any not-done refine's thermal decisions are dropped."""
    c1, _ = _hdr(tmp_path, "vr_ab", adjudicator=_Scripted(**{f"{STAGE}:thermal-state": ["abort"]}),
                 thermal_state="viewing")
    assert c1.run("mhc-only").status == "aborted"
    assert c1.calib["stages"][STAGE]["status"] == "aborted"
    adj = _Scripted()
    c2, _ = _hdr(tmp_path, "vr_ab", adjudicator=adj, thermal_state="viewing", viewing_start_nits=30.0)
    assert f"{STAGE}:thermal-state" not in c2.calib["decisions"]
    assert c2.run("mhc-only").status == "completed"
    (req,) = adj.asked(f"{STAGE}:thermal-state")
    assert req.digest["start"]["kind"] == "given"


def test_dwell_budget_counts_real_read_time_and_is_never_overrun():
    """Finding 5: the budget check assumed each dwell read takes the MODEL's read time; a real read 3x longer ran
    the budget over. Now the next read must fit a conservative bound (max(model x margin, longest real read))."""
    from dlc.viewing_thermal import ViewingGate, ViewingHold

    class _P:
        def __init__(self, rgb):
            self.rgb = tuple(rgb)

    target = LAW.load(3.0)
    now = [0.0]
    gate = ViewingGate(_spec(target, hold=True), PQ, clock=lambda: now[0])
    hold = ViewingHold(gate, (PQ.nits_to_cv(1.0),) * 3)
    hold.budget_s = 10.0 * hold.read_s
    hot = _P((PQ.nits_to_cv(60.0),) * 3)
    gate.begin("measure")
    gate.observe(hot)
    hold.begin_block()
    for _ in range(45):
        now[0] += 1.0
        gate.observe(hot)

    def slow_read():                               # a REAL dwell read: 3x the model's read time
        now[0] += 3.0 * hold.read_s
        gate.observe(_P(hold.dwell_rgb))

    rec = hold.dwell(slow_read, "time")
    assert rec is not None and rec["reads"] >= 2 and rec["stopped"] == "budget"
    assert hold.used_s <= hold.budget_s + 1e-9
    assert "budget_overrun_s" not in hold.summary()


def test_round_record_keeps_read_capped(tmp_path: Path):
    """Finding 5: the hold's read_capped flag was dropped from the refine's round record."""
    from types import SimpleNamespace

    calib, _ = _hdr(tmp_path, "vr_cap", thermal_state="viewing")
    target = LAW.load(13.5)
    st = {"target": target, "half": 0.5 * target, "used_s": 0.0, "rounds": [], "carried": None, "t_end": None,
          "left_load": None, "clock": lambda: 0.0, "law": LAW}
    res = SimpleNamespace(digest={"thermal_state": {
        "precondition": {"reached": True}, "achieved": {"in_band_throughout": True},
        "final": {"modelled_load": target}, "measure": {"observed_load": target},
        "hold": {"blocks": 2, "dwells": 1, "dwell_reads": 9, "dwell_min": 0.5, "dwell_s": 31.0,
                 "budget_min": 5.0, "budget_exhausted": False, "exhausted_at_block": None, "early_blocks": 0,
                 "read_capped": True}}})
    calib._refine_round_record(st, 1, _spec(target, hold=True), res)
    assert st["rounds"][0]["hold"]["read_capped"] is True
    assert st["used_s"] == pytest.approx(31.0)


def test_two_round_refine_carries_state_budget_and_history(tmp_path: Path):
    """Test gap: >= 2 refine rounds — round 2 starts from round 1's modelled end state (not the seam's start), its
    hold gets the budget round 1 left, both rounds read at the target, and the history the verify starts from is
    the refine's last round."""
    panel = TimedThermalPanel(start_load=0.096, cold_blue_gain=1.0, eotf_undershoot=-0.05)
    adj = _Scripted()
    calib, _ = _hdr(tmp_path, "vr_two", adjudicator=adj, thermal_state="viewing", panel=panel)
    assert calib.run("mhc-only").status == "completed"
    ts = calib.calib["stages"][STAGE]["digest"]["thermal_state"]
    rounds = ts["rounds"]
    assert len(rounds) >= 2 and len(adj.asked(f"{STAGE}:thermal-state")) == 1     # one seam, every round held
    r1, r2 = rounds[0], rounds[1]
    assert r1["start"]["load"] == ts["start"]["load"]
    assert r2["start"]["source"].startswith("carried from refine round 1")
    assert r2["start"]["load"] == pytest.approx(r1["measure"]["modelled_end"], abs=2e-5)  # sim: no wall-time gap
    assert r1["hold"]["settle"]["needed"] is True                  # round 1 settles from the soak's edge
    assert r2["hold"]["settle"].get("dwell_s", 0.0) < r1["hold"]["settle"]["dwell_s"] / 10
    budget_s = ts["hold_budget"]["budget_min"] * 60.0
    assert r2["hold"]["budget_min"] == pytest.approx((budget_s - r1["hold"]["dwell_s"]) / 60.0, abs=0.011)
    assert ts["hold_total"]["dwell_min"] == pytest.approx(sum(r["hold"]["dwell_s"] for r in rounds) / 60.0,
                                                          abs=0.011)
    assert all(r["offset_from_target"]["near_target"] for r in rounds) and ts["state"] == "viewing"
    assert ts["offset_from_target"]["reads"] == sum(r["offset_from_target"]["reads"] for r in rounds)
    hist = [h for h in calib.calib["thermal_history"] if h["stage"] == STAGE]
    last = rounds[-1]
    assert hist and hist[-1]["load"] == pytest.approx(
        max(last["measure"]["observed_load"], last["measure"]["modelled_end"]), abs=1e-5)
    assert calib.calib["stages"]["verify"]["digest"]["thermal_state"]["start"]["kind"] == "run-history"
    assert calib._last_refine["thermal_state"]["round"] == len(rounds)
