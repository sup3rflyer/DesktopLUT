"""--thermal-state viewing: the viewing-load precondition + state witness (dlc.viewing_thermal).

Synthetic thermal rig: :class:`TimedThermalPanel` is a :class:`SyntheticPanel` whose temperature relaxes
PER SECOND toward the load law of the patch shown, on a simulated clock advanced by the read-time model —
the physics the :class:`~dlc.viewing_thermal.ViewingGate` models. No display, no meter, no wall time.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasureLoopConfig, MeasurePatch, SyntheticPanel, run_measure_loop
from dlc.thermal import ThermalConfig, ThermalController
from dlc.viewing_thermal import (PRECONDITION_CAP_MIN, RECORDED_VERIFY_LOAD, VIEWING_LOAD_MAX_NITS, LoadLaw,
                                 ThermalStateModel, ViewingGate, ViewingPrecondition, _min_xyz, patch_load,
                                 predict_hold, read_seconds, set_band, stand_in_nits, start_state)

PQ = Transfer.pq(10)
LAW = LoadLaw()


class TimedThermalPanel(SyntheticPanel):
    """SyntheticPanel with a time-based load-thermal state (the model's own physics) + a ``sim_clock``."""

    def __init__(self, *, law: LoadLaw = LAW, start_load: float = 0.0, transfer: Transfer = PQ, **kw) -> None:
        kw.setdefault("cold_blue_gain", 0.9)
        kw.setdefault("native_white_nits", 1840.0)
        super().__init__(transfer=transfer, load_thermal=False, warm_tau=0.0, start_temp=start_load, **kw)
        self.law = law
        self.t = 0.0
        self.trace: list[tuple[float, float, str, tuple[int, int, int]]] = []

    def sim_clock(self) -> float:
        return self.t

    def __call__(self, patch: MeasurePatch):
        dt = read_seconds(_min_xyz(patch.rgb, self.transfer))
        self.temp = self.law.relax(self.temp, patch_load(patch.rgb, self.transfer, self.law), dt)
        self.t += dt
        reading = super().__call__(patch)          # temp untouched (load_thermal off, warm_tau 0)
        self.trace.append((self.t, self.temp, patch.role, tuple(patch.rgb)))
        return reading


def _grey(nits: float) -> tuple[int, int, int]:
    cv = PQ.nits_to_cv(nits)
    return (cv, cv, cv)


def _balanced_set() -> list[tuple[int, int, int]]:
    """A small content-like set in a balanced triangle order (every block the same level mix)."""
    levels = [1.0, 3.0, 9.0, 30.0, 110.0, 300.0]       # model band ~0.031 = the HDR content band
    block = [_grey(n) for n in levels]
    out: list[tuple[int, int, int]] = []
    for i in range(10):
        out += list(reversed(block)) if i % 2 == 0 else block
    return out


def _spec(target: float, start: float, *, soak: bool = True) -> ViewingPrecondition:
    half = 0.5 * target
    minutes = LAW.minutes_to_band(start, target, target, half) or 0.0
    return ViewingPrecondition(target_load=target, halfwidth=half, start_load=start, start_source="test",
                               target_source="test", deadline_s=minutes * 90.0 + 300.0, soak=soak, law=LAW)


# --- the model -------------------------------------------------------------------------------------

def test_load_law_and_time_to_band_match_the_study():
    assert LAW.load(13.5) == pytest.approx(0.0322, abs=5e-4)          # HDR content band (study meta.band)
    t = 0.0322
    # from the recorded-verify band the study quotes (0.158): ~2tau = the ">= 50 min" settle
    assert LAW.minutes_to_band(0.158, t, t, 0.5 * t) == pytest.approx(51.4, abs=0.5)
    assert LAW.minutes_to_band(LAW.load(40.0), t, t, 0.5 * t) == pytest.approx(20.2, abs=0.5)
    assert LAW.minutes_to_band(0.04, t, t, 0.5 * t) == 0.0            # already in band
    assert LAW.minutes_to_band(0.2, 0.1, t, 0.5 * t) is None          # a hold load outside the band never lands
    m = ThermalStateModel(LAW, 0.158)
    m.observe(t, 0.0)
    m.observe(t, 1500.0)                                              # one tau at the target load
    assert m.temp == pytest.approx(t + (0.158 - t) * math.exp(-1.0), rel=1e-9)


def test_stand_in_block_load_hits_the_target():
    ref = _grey(PQ.cv_to_nits(round(0.5 * PQ.max_cv)))
    nits, block = stand_in_nits(0.0322, ref_rgb=ref, transfer=PQ, law=LAW, n_load=12, n_ref=3)
    assert 3.0 < nits < 13.5 and block == pytest.approx(0.0322, abs=1e-4)
    # a target below what the reference reads alone deliver: black stand-in, the reachable block load reported
    nits0, block0 = stand_in_nits(1e-4, ref_rgb=ref, transfer=PQ, law=LAW, n_load=12, n_ref=3)
    assert nits0 == 0.0 and block0 > 1e-4


def test_start_state_resolution_order():
    desk = LAW.load(40.0)
    assert start_state(history=[], own_band_load=0.15, law=LAW)[0] == 0.15                 # a hotter set: its band
    # unknown start → ASSUME HOT: the recorded verify band, never a desktop / a content file's own (cool) band
    # (review 2026-10-09: the old max(own band, desktop) = 0.068 let the model claim "in band" on a hot panel)
    t_unknown, src_unknown = start_state(history=[], own_band_load=0.03, law=LAW)
    assert t_unknown == RECORDED_VERIFY_LOAD > desk and src_unknown.startswith("assumed HOT")
    hist = [{"stage": "measure:post-mhc", "load": 0.12, "ended_epoch": 1000.0}]
    t0, src = start_state(history=hist, own_band_load=0.03, law=LAW, now_epoch=1000.0 + 1500.0)
    assert t0 == pytest.approx(desk + (0.12 - desk) * math.exp(-1.0)) and "run history" in src
    # ...but a history that does NOT cover stages since (refine rounds / cube-build probe reads) is not trusted:
    cool = [{"stage": "measure:post-mhc", "load": 0.03, "ended_epoch": 1000.0}]
    t1, src1 = start_state(history=cool, own_band_load=0.03, law=LAW, now_epoch=1000.0,
                           unmodelled=["build-3dlut"])
    assert t1 == RECORDED_VERIFY_LOAD and "build-3dlut" in src1 and src1.startswith("assumed HOT")
    assert start_state(history=cool, own_band_load=0.03, law=LAW, now_epoch=1000.0)[0] == pytest.approx(0.03)
    assert start_state(history=hist, own_band_load=0.03, law=LAW, start_nits=13.5)[0] == pytest.approx(LAW.load(13.5))


# --- the controller's gate: refuses convergence while the MODELLED state is out of band ------------

def _controller(panel, gate=None):
    measure = panel
    if gate is not None:
        def measure(p, _inner=panel):                    # the caller's measure path feeds the gate
            r = _inner(p)
            gate.observe(p)
            return r
    return ThermalController(measure=measure, transfer=PQ, content=[_grey(8.0)], ref_nits=92.0,
                             balance_noise=None, config=ThermalConfig(k_start=1.0), clock=panel.sim_clock,
                             state_gate=gate)


def test_gate_refuses_convergence_while_the_modelled_state_is_out_of_band():
    # An inert panel (thermally flat): the slope-vs-noise gate alone converges in a few blocks.
    base = _controller(TimedThermalPanel(start_load=0.03, cold_blue_gain=1.0)).run()
    assert base.converged and base.blocks <= 8 and "state_gate" not in base.digest

    # Same panel, gate modelling a HOT start (0.158) with a deadline far short of the ~51 min it needs:
    panel = TimedThermalPanel(start_load=0.03, cold_blue_gain=1.0)
    spec = ViewingPrecondition(target_load=0.0322, halfwidth=0.0161, start_load=0.158, start_source="test",
                               target_source="test", deadline_s=600.0, law=LAW)
    gate = ViewingGate(spec, PQ, panel.sim_clock)
    gate.begin("precondition")
    res = _controller(panel, gate).run()
    assert not res.converged and res.needs_adjudication
    assert not gate.in_band() and res.digest["state_gate"]["in_band"] is False
    assert any("viewing precondition NOT reached" in f for f in res.flags)

    # ...and with the modelled state already in band the gate is transparent (same block count).
    panel2 = TimedThermalPanel(start_load=0.03, cold_blue_gain=1.0)
    gate2 = ViewingGate(_spec(0.0322, 0.03), PQ, panel2.sim_clock)
    gate2.begin("precondition")
    res2 = _controller(panel2, gate2).run()
    assert res2.converged and res2.blocks == base.blocks and res2.digest["state_gate"]["in_band"] is True


# --- the measure loop: viewing reaches AND holds the band ------------------------------------------

def test_viewing_precondition_reaches_and_the_balanced_set_holds_the_band():
    patches = _balanced_set()
    target = set_band(patches, PQ, LAW)["load"]          # a content-like set: its own band is the target
    panel = TimedThermalPanel(start_load=0.158)          # HOT: a recorded-verify state
    spec = _spec(target, 0.158)
    res = run_measure_loop(patches=patches, transfer=PQ, measure=panel, config=MeasureLoopConfig(viewing=spec))
    ts = res.digest["thermal_state"]
    pre = ts["precondition"]
    assert pre["reached"] is True and pre["converged"] is True
    need = LAW.minutes_to_band(0.158, pre["stand_in"]["predicted_block_load"], target, 0.9 * spec.halfwidth)
    assert pre["elapsed_min"] >= 0.9 * need                       # the model is the time floor (~50 min)
    # no bright filler before the main pass: nothing above the neutral reference / warm-up patch (~100 nit)
    warm_cv = round((MeasureLoopConfig().warmup_signal + MeasureLoopConfig().warmup_bias_signal) * PQ.max_cv)
    first_main = next(i for i, tr in enumerate(panel.trace) if tr[2] == "measurement")
    assert max(max(tr[3]) for tr in panel.trace[:first_main]) <= warm_cv
    # the TRUE panel state is in band at the start of the main pass and stays there through it
    lo, hi = target - spec.halfwidth, target + spec.halfwidth
    main = [tr[1] for tr in panel.trace[first_main:]]
    assert lo <= main[0] <= hi and all(lo <= t <= hi for t in main)
    m = ts["measure"]
    assert lo <= m["modelled_range"][0] and m["modelled_range"][1] <= hi
    assert m["observed_load"] == pytest.approx(target, rel=0.35)
    assert not res.needs_adjudication and "viewing_precondition_unmet" not in res.digest["anomaly_reasons"]
    # the model agrees with the rig's physics (same law, same clock)
    assert ts["final"]["modelled_load"] == pytest.approx(panel.temp, abs=2e-3)


def test_default_path_matches_the_recorded_main_fingerprint(tmp_path: Path):
    """verify (default, no flag) == main before this branch: the SAME reads in the same order and the same
    digests (except the new ``thermal_state`` key) in a forced own-band soak and the verify-only flow. The
    golden was recorded by running ``tests/_thermal_default_fingerprint.py`` against main fe890a9's source."""
    from _thermal_default_fingerprint import fingerprint

    golden = json.loads((Path(__file__).parent / "data" / "thermal_default_path_main.json").read_text("utf-8"))
    golden.pop("_recorded_from")
    got = json.loads(json.dumps(fingerprint(tmp_path), sort_keys=True))   # same JSON normalisation as the golden
    for scenario in ("measure_loop", "verify_only"):
        g, h = golden[scenario], got[scenario]
        assert h["reads"] == g["reads"], f"{scenario}: the default path commands different reads than main"
        assert h == g, f"{scenario}: the default path's digests differ from main"
    assert "thermal_state" not in got["verify_only"]["calib_keys"]       # no run-record key without the flag


def test_band_exit_during_the_measured_pass_is_flagged_never_silently_viewing():
    """A pass that LEAVES the band (a bright set pulling the panel out) is an evidence flag for the LLM, and
    the stage says the viewing state was requested but not held (review 2026-10-09: end load 0.053 > 0.048
    raised no flag and the digest still said "viewing")."""
    target = 0.0322
    bright = [_grey(n) for n in (300.0, 600.0, 1000.0, 90.0)] * 60          # ~240 bright reads
    panel = TimedThermalPanel(start_load=target, cold_blue_gain=1.0)
    res = run_measure_loop(patches=bright, transfer=PQ, measure=panel,
                           config=MeasureLoopConfig(viewing=_spec(target, target)))
    assert res.needs_adjudication and "viewing_band_left" in res.digest["anomaly_reasons"]
    ts = res.digest["thermal_state"]
    assert ts["precondition"]["reached"] is True                           # it STARTED in the viewing state...
    assert ts["achieved"]["in_band_at_start"] is True
    assert ts["achieved"]["in_band_throughout"] is False and ts["achieved"]["in_band_at_end"] is False
    assert ts["measure"]["modelled_end"] > target + 0.5 * target           # ...and left it (model)
    assert ts["state"] == "outside-viewing-band" and ts["requested"] == "viewing" and "model" in ts["basis"]
    assert "LEFT the band" in (res.question or "") and "remeasure" in res.question


def test_soak_progress_ticks_carry_the_modelled_state(tmp_path: Path):
    """The precondition's per-block model state reaches the spine's progress ticks (dashboard evidence);
    a default-state soak's ticks are unchanged."""
    import dlc.measure_loop as ml
    from dlc.events import RunLog, read_events

    patches = _balanced_set()
    target = set_band(patches, PQ, LAW)["load"]
    ticks = {}
    for name, cfg in (("viewing", MeasureLoopConfig(viewing=_spec(target, 0.07))),
                      ("default", MeasureLoopConfig(preheat="always"))):
        epath = tmp_path / f"{name}.jsonl"
        panel = TimedThermalPanel(start_load=0.07, cold_blue_gain=1.0)
        loop = ml._Loop(patches=patches, transfer=PQ, measure=panel, config=cfg, ndjson=ml._NdjsonWriter(None),
                        events=None, runlog=RunLog(epath, phase="measure:verify"))
        loop.preheat()
        ticks[name] = [e.data for e in read_events(epath) if e.event == "progress" and "block" in (e.data or {})]
    assert ticks["viewing"] and all("model_load" in t and "model_in_band" in t for t in ticks["viewing"])
    assert ticks["viewing"][0]["model_in_band"] is False and ticks["viewing"][-1]["model_in_band"] is True
    assert ticks["default"] and not any("model_load" in t for t in ticks["default"])


def test_measure_now_tracks_the_state_and_flags_it():
    patches = _balanced_set()
    target = set_band(patches, PQ, LAW)["load"]
    panel = TimedThermalPanel(start_load=0.158)
    res = run_measure_loop(patches=patches, transfer=PQ, measure=panel,
                           config=MeasureLoopConfig(viewing=_spec(target, 0.158, soak=False)))
    ts = res.digest["thermal_state"]
    assert ts["precondition"]["skipped"] == "measure-now"
    assert ts["measure"]["modelled_start"] == pytest.approx(0.158, abs=0.01)   # measured HOT, and says so
    assert res.needs_adjudication and "viewing_precondition_unmet" in res.digest["anomaly_reasons"]
    assert "VIEWING thermal state was requested" in (res.question or "")


def test_predict_hold_separates_a_bright_set_from_a_balanced_one():
    target = 0.0322
    bright = [_grey(n) for n in (200.0, 600.0, 90.0, 1000.0, 300.0)] * 400      # ~20 min of bright reads
    hb = predict_hold(bright, PQ, LAW, start_load=target, target_load=target, halfwidth=0.5 * target)
    assert hb["in_band_fraction"] < 0.5 and hb["end_load"] > 2 * target
    bal = _balanced_set()
    t2 = set_band(bal, PQ, LAW)["load"]
    hc = predict_hold(bal, PQ, LAW, start_load=t2, target_load=t2, halfwidth=0.5 * t2)
    assert hc["in_band_fraction"] == 1.0


# --- the orchestrator: option, seam, recorded state -------------------------------------------------

def _vo(tmp_path: Path, name: str, **kw):
    from test_verify_only_flow import _cube, _make, _seed_stack
    from dlc.controller import CalibrationController

    ctrl = CalibrationController.mock()
    _seed_stack(ctrl, mode="HDR", cube=_cube(tmp_path / f"{name}.cube"))
    return _make(tmp_path, name, mode="HDR", controller=ctrl, bit_depth=10, **kw)


def _hdr_file(tmp_path: Path) -> Path:
    from dlc.stages.simulate import write_synthetic_patches_file

    return write_synthetic_patches_file(tmp_path / "patches_hdr.json", mode="HDR", bit_depth=10)


class _Scripted:
    """Answers each seam key from a script (the i-th ask gets the i-th answer, the last repeats), every other
    seam by its recommendation; records every request so a test can ASSERT a seam fired and what it carried."""

    def __init__(self, **script: list[str]) -> None:
        self.script = script
        self.requests: list = []

    def adjudicate(self, request):
        from dlc.adjudication import Decision

        self.requests.append(request)
        answers = self.script.get(request.key)
        if answers:
            n = sum(1 for r in self.requests if r.key == request.key) - 1
            return Decision(answers[min(n, len(answers) - 1)], note="scripted")
        return Decision(request.recommendation, note="auto")

    def asked(self, key: str) -> list:
        return [r for r in self.requests if r.key == key]


def test_viewing_option_validation(tmp_path: Path):
    pf = _hdr_file(tmp_path)
    with pytest.raises(ValueError, match="thermal_state"):
        _vo(tmp_path, "bad_state", verify_patches_file=pf, thermal_state="lukewarm")
    with pytest.raises(ValueError, match="designed order"):
        _vo(tmp_path, "bad_order", verify_patches_file=pf, thermal_state="viewing", verify_patches_order="thermal")
    with pytest.raises(ValueError, match="preheat"):
        _vo(tmp_path, "bad_preheat", verify_patches_file=pf, thermal_state="viewing", preheat="never")


def test_default_run_records_verify_state_and_plan_order_unchanged(tmp_path: Path):
    from dlc import verify_only

    pf = _hdr_file(tmp_path)
    runs = {}
    for name, extra in (("vo_default", {}), ("vo_explicit", {"thermal_state": "verify"})):
        calib = _vo(tmp_path, name, verify_patches_file=pf, **extra)
        assert calib.run("verify-only").status == "completed"
        runs[name] = calib
    a, b = runs["vo_default"], runs["vo_explicit"]
    # an explicit `verify` IS the default: persisted as unset, exactly like omitting the flag
    assert "thermal_state" not in a.calib and "thermal_state" not in b.calib
    assert a.calib["patch_plan"] == b.calib["patch_plan"]
    order = [verify_only._patches_from_ndjson(c.ctx.root / "measurements" / "verify.ndjson") for c in (a, b)]
    assert order[0] == order[1] and [list(p) for p in order[0]] == json.loads(pf.read_text("utf-8"))["codes"]
    for c in (a, b):
        assert c.calib["stages"]["measure:verify"]["digest"]["thermal_state"]["state"] == "verify"
        assert c.calib["stages"]["verify"]["digest"]["thermal_state"] == {"state": "verify"}
        assert "thermal_history" not in c.calib
        assert not any(k.endswith(":thermal-state") for k in c.calib.get("decisions") or {})


def test_viewing_verify_seam_precondition_and_recorded_state(tmp_path: Path):
    from dlc import verify_only

    pf = _hdr_file(tmp_path)
    codes = json.loads(pf.read_text("utf-8"))["codes"]
    panel = TimedThermalPanel(start_load=LAW.load(40.0), cold_blue_gain=1.0)   # an ordinary desktop
    adj = _Scripted()
    calib = _vo(tmp_path, "vo_viewing", panel=panel, verify_patches_file=pf, thermal_state="viewing", adjudicator=adj)
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    assert calib.calib["thermal_state"] == "viewing"
    # the seam FIRES (asserted, not `if`): target band, start assumption, predicted cost, cap; recommendation
    # is one of the options; nothing auto-accepted it
    dec = calib.calib["decisions"]["measure:verify:thermal-state"]
    assert dec["choice"] == "precondition"
    reqs = adj.asked("measure:verify:thermal-state")
    assert len(reqs) == 1
    req = reqs[0]
    assert req.options == ("precondition", "measure-now", "abort") and req.recommendation in req.options
    assert "precondition" in req.question and "capped" in req.question and "--viewing-start-nits" in req.question
    assert req.digest["start"]["kind"] == "assumed-hot" and req.digest["start"]["load"] == RECORDED_VERIFY_LOAD
    assert req.digest["precondition_budget"]["capped"] is False and "model" in req.digest["basis"]
    # recorded state: the measure stage + the verify digest say which state the numbers represent
    ts = calib.calib["stages"]["measure:verify"]["digest"]["thermal_state"]
    assert ts["state"] == "viewing" and ts["decision"] == "precondition"
    assert ts["predicted"]["precondition_minutes"] > 0 and ts["target"]["band"][0] < ts["target"]["load"]
    assert ts["precondition"]["reached"] is True
    lo, hi = ts["target"]["band"]
    assert lo <= ts["measure"]["modelled_range"][0] and ts["measure"]["modelled_range"][1] <= hi
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["state"] == "viewing" and vd["precondition_reached"] is True and vd["decision"] == "precondition"
    assert vd["requested"] == "viewing" and vd["evidence_flags"] == [] and vd["needs_adjudication"] is False
    assert vd["achieved"]["in_band_at_start"] and vd["achieved"]["in_band_throughout"] and vd["achieved"]["in_band_at_end"]
    assert "model" in vd["basis"] and vd["start"]["source"].startswith("assumed HOT")
    # the file order is kept; the history records what the panel was left at (+ the stages memoised by then)
    measured = verify_only._patches_from_ndjson(calib.ctx.root / "measurements" / "verify.ndjson")
    assert [list(p) for p in measured] == codes
    assert calib.calib["thermal_history"][-1]["stage"] == "measure:verify"
    assert "measure:verify" in calib.calib["thermal_history"][-1]["stages_done"]


def _first_main_read_state(panel: TimedThermalPanel) -> float:
    """The TRUE (rig) thermal state when the main pass's first measurement read completes."""
    return next(tr[1] for tr in panel.trace if tr[2] == "measurement")


def test_unknown_start_assumes_hot_so_a_hot_panel_is_really_in_band(tmp_path: Path):
    """Finding 1: with no history / no given start the precondition used max(own band, desktop) = 0.068 for a
    content file, so on a panel straight off a DLC verify (the recorded 0.096 band) the model claimed
    "reached" while the TRUE state at the main pass was ~0.058, outside the 0.016-0.048 band. Assuming hot
    (the recorded verify band) makes the soak long enough for the hot case."""
    pf = _hdr_file(tmp_path)
    panel = TimedThermalPanel(start_load=RECORDED_VERIFY_LOAD, cold_blue_gain=1.0)
    calib = _vo(tmp_path, "vo_hot", panel=panel, verify_patches_file=pf, thermal_state="viewing", adjudicator=_Scripted())
    assert calib.run("verify-only").status == "completed"
    ts = calib.calib["stages"]["measure:verify"]["digest"]["thermal_state"]
    lo, hi = ts["target"]["band"]
    assert lo <= _first_main_read_state(panel) <= hi        # the rig's TRUE state, not just the model's claim
    assert ts["start"]["kind"] == "assumed-hot" and ts["precondition"]["reached"] is True


def test_history_start_is_untrusted_after_unmodelled_stages(tmp_path: Path):
    """Finding 1 (history): the full flow's last history entry is post-MHC, but the cube build's probe reads
    (and, in mhc-only, the MHC refine rounds) drove the display after it, uncounted. Any stage memoised after
    the entry makes the history-based start untrusted → assumed hot, naming the stages."""
    calib = _vo(tmp_path, "vo_hist", verify_patches_file=_hdr_file(tmp_path), thermal_state="viewing")
    calib.calib["stages"] = {"measure:raw": {}, "measure:post-mhc": {}}
    calib._note_thermal_history("measure:post-mhc", 0.03, basis="test")
    assert calib._unmodelled_since_history("measure:verify") == []
    calib.calib["stages"]["build-install-3dlut"] = {}
    assert calib._unmodelled_since_history("measure:verify") == ["build-install-3dlut"]
    from dlc.calibrate import CalibrationAborted

    plan_patches = [_grey(n) for n in (1.0, 9.0, 30.0)]
    adj = _Scripted(**{"measure:verify:thermal-state": ["abort"]})
    calib.adjudicator, calib.target_name = adj, "rec2020_pq"
    with pytest.raises(CalibrationAborted):
        calib._thermal_state_plan("measure:verify", "verify", plan_patches)
    start = adj.asked("measure:verify:thermal-state")[0].digest["start"]
    assert start["kind"] == "assumed-hot" and start["load"] == RECORDED_VERIFY_LOAD
    assert start["unmodelled_stages"] == ["build-install-3dlut"] and "build-install-3dlut" in start["source"]


def test_start_is_correctable_on_resume_at_the_thermal_state_seam(tmp_path: Path):
    """Finding 1 (seam): a resume answering the thermal-state seam may correct the start with
    --viewing-start-nits (refused before: earlier stages were memoised → resume-args). The change is recorded
    and a memoised thermal-state decision is DROPPED (its numbers changed) so the seam re-asks. Once
    measure:verify is memoised the knobs lock (resume-args)."""
    from dlc.adjudication import AdjudicationRequired, Decision, MappingAdjudicator

    pf = _hdr_file(tmp_path)
    key = "measure:verify:thermal-state"
    plan = {"resolve-target:plan": Decision("approve")}

    def run(decisions, **kw):
        panel = TimedThermalPanel(start_load=LAW.load(40.0), cold_blue_gain=1.0)
        calib = _vo(tmp_path, "vo_resume", panel=panel, verify_patches_file=pf, thermal_state="viewing",
                    adjudicator=MappingAdjudicator(decisions), **kw)
        return calib, calib.run("verify-only")

    with pytest.raises(AdjudicationRequired) as first:
        run(dict(plan))
    assert first.value.request.key == key
    first_start = first.value.request.digest["start"]
    # a recorded decision from the first ask (e.g. the process died after deciding) — main() seeds the
    # adjudicator from the run record, as here:
    state_path = tmp_path / "vo_resume" / "dlc_state.json"
    state = json.loads(state_path.read_text("utf-8"))
    state["calib"]["decisions"][key] = {"choice": "precondition", "note": "first ask"}
    state_path.write_text(json.dumps(state), "utf-8")
    with pytest.raises(AdjudicationRequired) as again:       # knob changed → the stale decision is dropped
        run({**plan, key: Decision("precondition", "first ask")}, viewing_start_nits=40.0)
    assert again.value.request.key == key
    assert first_start["kind"] == "assumed-hot" and first_start["load"] == RECORDED_VERIFY_LOAD
    assert again.value.request.digest["start"] == {
        "load": round(LAW.load(40.0), 5), "nits_equiv": 40.0, "source": "given: 40 nit-equivalent", "kind": "given"}
    calib, result = run({**plan, key: Decision("precondition", "start corrected")}, viewing_start_nits=40.0)
    assert result.status == "completed", result.digest
    assert calib.calib["viewing_start_nits"] == 40.0
    changes = calib.calib["thermal_state_changes"]
    assert changes[0]["field"] == "viewing_start_nits" and changes[0]["from"] is None and changes[0]["to"] == 40.0
    ts = calib.calib["stages"]["measure:verify"]["digest"]["thermal_state"]
    assert ts["start"]["kind"] == "given" and ts["decision"] == "precondition"
    # measure:verify memoised: a different start is now refused, the same one is not a conflict
    _, locked = run({**plan, key: Decision("precondition")}, viewing_start_nits=20.0)
    assert locked.status == "aborted" and locked.digest["aborted_at"] == "resume-args"
    assert locked.digest["conflicts"] == [{"field": "viewing_start_nits", "requested": 20.0, "persisted": 40.0}]
    _, same = run({**plan, key: Decision("precondition")}, viewing_start_nits=40.0)
    assert same.status == "completed"


def test_measure_now_verify_is_never_labelled_viewing(tmp_path: Path):
    """Finding 2: a skipped precondition (measure-now) used to leave the verify digest saying `state: viewing`.
    Now: requested vs achieved, an evidence flag, the model basis and the start source."""
    pf = _hdr_file(tmp_path)
    panel = TimedThermalPanel(start_load=RECORDED_VERIFY_LOAD, cold_blue_gain=1.0)
    adj = _Scripted(**{"measure:verify:thermal-state": ["measure-now"], "measure:verify:escalation": ["accept"]})
    calib = _vo(tmp_path, "vo_now", panel=panel, verify_patches_file=pf, thermal_state="viewing", adjudicator=adj)
    assert calib.run("verify-only").status == "completed"
    esc = adj.asked("measure:verify:escalation")
    assert len(esc) == 1 and {"viewing_precondition_unmet", "viewing_band_left"} <= set(esc[0].digest["anomaly_reasons"])
    ts = calib.calib["stages"]["measure:verify"]["digest"]["thermal_state"]
    assert ts["state"] == "outside-viewing-band" and ts["requested"] == "viewing"
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["state"] == "outside-viewing-band" and vd["requested"] == "viewing"
    assert vd["precondition_skipped"] == "measure-now" and vd["precondition_reached"] is None
    assert {"viewing_precondition_skipped", "viewing_band_left"} <= set(vd["evidence_flags"])
    assert vd["needs_adjudication"] is True and vd["achieved"]["in_band_at_start"] is False
    assert "model" in vd["basis"] and vd["start"]["source"] and vd["escalation_decision"]["choice"] == "accept"


def test_unmet_precondition_offers_remeasure_and_the_thermal_state_seam_reasks(tmp_path: Path):
    """Finding 3: the unmet question said "accept as such, or retry" but the seam offered accept/suppress/abort
    and the memoised thermal-state choice would have replayed. Now `remeasure` is offered; choosing it drops
    the memoised thermal-state decision, so the seam re-asks with the start from THIS run's history."""
    pf = _hdr_file(tmp_path)
    panel = TimedThermalPanel(start_load=RECORDED_VERIFY_LOAD, cold_blue_gain=1.0)
    adj = _Scripted(**{"measure:verify:thermal-state": ["measure-now", "precondition"],
                       "measure:verify:escalation": ["remeasure"]})
    calib = _vo(tmp_path, "vo_re", panel=panel, verify_patches_file=pf, thermal_state="viewing", adjudicator=adj)
    result = calib.run("verify-only")
    esc = adj.asked("measure:verify:escalation")
    assert esc and "remeasure" in esc[0].options and "remeasure" in esc[0].question
    assert result.status == "completed", result.digest
    assert len(esc) == 1
    asks = adj.asked("measure:verify:thermal-state")
    assert len(asks) == 2                                          # re-asked, not replayed
    assert asks[0].digest["start"]["kind"] == "assumed-hot"
    assert asks[1].digest["start"]["kind"] == "run-history"        # nothing unmodelled since the first pass
    assert calib.calib["decisions"]["measure:verify:thermal-state"]["choice"] == "precondition"
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["state"] == "viewing" and vd["evidence_flags"] == [] and vd["precondition_reached"] is True
    lo, hi = vd["target"]["band"]
    assert lo <= panel.temp <= hi + 0.005


def test_explicit_verify_on_resume_is_not_a_conflict(tmp_path: Path):
    """Finding 4: a run started WITHOUT the flag aborted at resume-args when resumed with an explicit
    `--thermal-state verify`. Explicit verify == absent; a real change after the verify is measured is refused."""
    pf = _hdr_file(tmp_path)
    assert _vo(tmp_path, "vo_r", verify_patches_file=pf).run("verify-only").status == "completed"
    calib = _vo(tmp_path, "vo_r", verify_patches_file=pf, thermal_state="verify")
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    assert "thermal_state" not in calib.calib and "thermal_state_changes" not in calib.calib
    viewing = _vo(tmp_path, "vo_r", verify_patches_file=pf, thermal_state="viewing").run("verify-only")
    assert viewing.status == "aborted" and viewing.digest["aborted_at"] == "resume-args"
    assert viewing.digest["conflicts"] == [{"field": "thermal_state", "requested": "viewing", "persisted": "verify"}]


@pytest.mark.parametrize("bad", [0.0, -1.0, 1000.0, float("nan"), VIEWING_LOAD_MAX_NITS + 0.5])
def test_viewing_load_target_is_bounded(tmp_path: Path, bad: float):
    """Finding 6: a 0 target can never converge; 1000 (a typo) would soak a ~full-field bright static for up to
    the cap. Refused: (0, the recorded verify band's nit-equivalent]."""
    with pytest.raises(ValueError, match="viewing_load_nits"):
        _vo(tmp_path, "vo_bad", verify_patches_file=_hdr_file(tmp_path), thermal_state="viewing",
            viewing_load_nits=bad)


def test_precondition_is_capped_and_the_seam_says_so_then_abort_measures_nothing(tmp_path: Path):
    """Finding 6 (cap) + the seam's abort: a start the model needs > the cap for is capped at
    PRECONDITION_CAP_MIN (4 tau), stated in the question; abort stops the run before any measurement read."""
    pf = _hdr_file(tmp_path)
    panel = TimedThermalPanel(start_load=RECORDED_VERIFY_LOAD, cold_blue_gain=1.0)
    adj = _Scripted(**{"measure:verify:thermal-state": ["abort"]})
    assert 65.0 <= VIEWING_LOAD_MAX_NITS < 66.0
    calib = _vo(tmp_path, "vo_cap", panel=panel, verify_patches_file=pf, thermal_state="viewing",
                viewing_load_nits=VIEWING_LOAD_MAX_NITS / 10.0, viewing_start_nits=1850.0, adjudicator=adj)
    result = calib.run("verify-only")
    assert result.status == "aborted"
    (req,) = adj.asked("measure:verify:thermal-state")
    budget = req.digest["precondition_budget"]
    assert budget == {"deadline_min": PRECONDITION_CAP_MIN, "cap_min": PRECONDITION_CAP_MIN, "capped": True}
    assert req.digest["predicted"]["precondition_minutes"] * 1.5 + 5 > PRECONDITION_CAP_MIN
    assert f"capped at {PRECONDITION_CAP_MIN:g}" in req.question.replace(".0 min", " min")
    assert calib.calib["stages"]["measure:verify"]["status"] == "aborted"
    assert "verify" not in calib.calib["stages"]
    assert calib.calib["decisions"]["measure:verify:thermal-state"]["choice"] == "abort"
    assert not any(tr[2] == "measurement" for tr in panel.trace) and not panel.trace


def test_measure_checkin_carries_the_modelled_thermal_state(tmp_path: Path):
    """Check-ins are evidence for the LLM: a viewing-state stage's packets carry the modelled state vs
    the band and the load actually shown in this phase; a default-state packet is unchanged."""
    import dlc.measure_loop as ml
    from dlc.checkin import CheckinWindow
    from dlc.events import RunLog, read_events

    patches = _balanced_set()
    target = set_band(patches, PQ, LAW)["load"]
    got = {}
    for name, viewing in (("viewing", _spec(target, 0.158)), ("default", None)):
        epath = tmp_path / f"{name}.jsonl"
        panel = TimedThermalPanel(start_load=0.158)
        loop = ml._Loop(patches=patches, transfer=PQ, measure=panel,
                        config=MeasureLoopConfig(viewing=viewing), ndjson=ml._NdjsonWriter(None), events=None,
                        runlog=RunLog(epath, phase="measure:verify"), checkin_interval_s=300.0,
                        checkin_window=CheckinWindow())
        if loop.viewing_gate is not None:
            loop.viewing_gate.begin("measure")
        for p in patches[:5]:
            loop.measure(MeasurePatch(label="x", rgb=p, signal=tuple(c / PQ.max_cv for c in p), role="measurement",
                                      bit_depth=10))
        loop._emit_measure_checkin(5, len(patches), 5 / len(patches), trigger="progress")
        got[name] = [e for e in read_events(epath) if e.event == "check_in"][-1].data
    ts = got["viewing"]["thermal_state"]
    assert ts["in_band"] is False and ts["band"][0] < target < ts["band"][1]
    assert 0.1 < ts["modelled_load"] <= 0.158 and ts["phase"] == "measure" and ts["observed_load"] > 0
    assert "thermal_state" not in got["default"]
