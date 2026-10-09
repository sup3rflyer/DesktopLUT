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
from dlc.viewing_thermal import (LoadLaw, ThermalStateModel, ViewingGate, ViewingPrecondition, _min_xyz,
                                 patch_load, predict_hold, read_seconds, set_band, stand_in_nits, start_state)

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
    assert start_state(history=[], own_band_load=0.15, law=LAW)[0] == 0.15                 # conservative: hot
    assert start_state(history=[], own_band_load=0.03, law=LAW)[0] == pytest.approx(desk)  # desktop floor
    hist = [{"stage": "measure:post-mhc", "load": 0.12, "ended_epoch": 1000.0}]
    t0, src = start_state(history=hist, own_band_load=0.03, law=LAW, now_epoch=1000.0 + 1500.0)
    assert t0 == pytest.approx(desk + (0.12 - desk) * math.exp(-1.0)) and "run history" in src
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


def test_default_state_soak_parks_at_the_sets_own_band_unchanged():
    # verify (default): no viewing config — the own-band soak, no thermal_state digest, identical reads.
    patches = _balanced_set()
    runs = []
    for cfg in (MeasureLoopConfig(preheat="always"), MeasureLoopConfig(preheat="always", viewing=None)):
        panel = SyntheticPanel(transfer=PQ, load_thermal=True, start_temp=0.1)
        res = run_measure_loop(patches=patches, transfer=PQ, measure=panel, config=cfg)
        runs.append((res, panel.reads))
    (a, na), (b, nb) = runs
    assert na == nb and a.digest["preheat"] == b.digest["preheat"]
    assert "thermal_state" not in a.digest and "thermal_state" not in (a.digest["preheat"] or {})


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
    assert "thermal_state" not in a.calib and b.calib["thermal_state"] == "verify"
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
    calib = _vo(tmp_path, "vo_viewing", panel=panel, verify_patches_file=pf, thermal_state="viewing")
    result = calib.run("verify-only")
    assert result.status == "completed", result.digest
    assert calib.calib["thermal_state"] == "viewing"
    # the seam: target band, start assumption, predicted cost; the recommendation is one of the options
    dec = calib.calib["decisions"]["measure:verify:thermal-state"]
    assert dec["choice"] == "precondition"
    from dlc.events import read_events
    reqs = [e.data for e in read_events(calib.ctx.events_path)
            if (e.data or {}).get("key") == "measure:verify:thermal-state" and (e.data or {}).get("options")]
    if reqs:
        req = reqs[-1]
        assert req["recommendation"] in req["options"] and "precondition" in req["question"]
    # recorded state: the measure stage + the verify digest say which state the numbers represent
    ts = calib.calib["stages"]["measure:verify"]["digest"]["thermal_state"]
    assert ts["state"] == "viewing" and ts["decision"] == "precondition"
    assert ts["predicted"]["precondition_minutes"] > 0 and ts["target"]["band"][0] < ts["target"]["load"]
    assert ts["precondition"]["reached"] is True
    lo, hi = ts["target"]["band"]
    assert lo <= ts["measure"]["modelled_range"][0] and ts["measure"]["modelled_range"][1] <= hi
    vd = calib.calib["stages"]["verify"]["digest"]["thermal_state"]
    assert vd["state"] == "viewing" and vd["precondition_reached"] is True and vd["decision"] == "precondition"
    # the file order is kept; the history records what the panel was left at
    measured = verify_only._patches_from_ndjson(calib.ctx.root / "measurements" / "verify.ndjson")
    assert [list(p) for p in measured] == codes
    assert calib.calib["thermal_history"][-1]["stage"] == "measure:verify"


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
