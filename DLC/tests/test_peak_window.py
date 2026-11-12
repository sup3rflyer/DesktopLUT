"""Peak by window (the ABL graph): the plan, the per-hold analysis, the flow on the probe simulator, the DIP section.

Every flow test drives ``python -m dlc.calibrate --flow peak-window`` in-process: the mock DesktopLUT, the
:class:`dlc.probe_sim.ProbeSimPanel` meter on a virtual clock (minutes of holds and cool-downs run instantly), no
display, no meter, no daemon.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import math
from pathlib import Path

import pytest

from dlc import calibration_profile as cp
from dlc import peak_window as pw
from dlc.adjudication import AdjudicationRequired, AutoAdjudicator, Decision, MappingAdjudicator
from dlc.calibrate import Calibration
from dlc.controller import CalibrationController
from dlc.dip import DipStore, DisplayInstrumentProfile, PeakByWindow
from dlc.events import read_events
from dlc.measure_loop import MeasurePatch, window_shapes
from dlc.probe_sim import Limiter, ThermalSag, VirtualClock, mini_led_like, oled_like
from dlc.runs import create_run, open_run

_DATE = datetime.date(2026, 11, 12)


def _profile(tmp_path: Path, tech: str) -> cp.Profile:
    prof = cp.Profile.synthetic(output_dir=str(tmp_path / "results"))
    d = prof.displays[0]
    return dataclasses.replace(prof, displays=(dataclasses.replace(d, panel=dataclasses.replace(d.panel, tech=tech)),))


def _calib(tmp_path: Path, name: str, panel, *, tech: str = "WOLED", adjudicator=None, spec=None, dip=True,
           controller=None) -> Calibration:
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run("HDR", display="synthetic", run_dir=run_dir)
    ctrl = controller or CalibrationController.mock()
    ctrl.set_hdr(0, True)
    cal = Calibration(ctx=ctx, profile=_profile(tmp_path, tech), monitor=0, mode="HDR", controller=ctrl,
                      measure=panel, adjudicator=adjudicator or AutoAdjudicator(), run_date=_DATE, bit_depth=10)
    if spec is not None:
        cal.probe_spec = spec
    if dip and cal._dip() is None:
        cal._dip_store().record(DisplayInstrumentProfile(display=cal.display.name, mode="HDR", made="2026-11-01"))
    return cal


def _recorded(cal) -> PeakByWindow:
    rec = DipStore.load(cal._dip_store().path).get(f"{cal.display.name}:HDR")
    assert rec is not None and rec.peak_by_window is not None
    return rec.peak_by_window


# ----------------------------------------------------------------------------- pure pieces
def test_window_shapes_are_a_centred_screen_aspect_rectangle_on_black():
    s = window_shapes((1023, 1023, 1023), 4.0)
    assert s[0] == ((0, 0, 0), (0.0, 0.0, 1.0, 1.0))
    (rgb, (x, y, cx, cy)) = s[1]
    assert rgb == (1023, 1023, 1023) and cx == pytest.approx(0.2) and cy == pytest.approx(0.2)
    assert x == pytest.approx(0.4) and y == pytest.approx(0.4)
    assert window_shapes((5, 5, 5), 100.0) == [((5, 5, 5), (0.0, 0.0, 1.0, 1.0))]
    with pytest.raises(ValueError):
        window_shapes((1, 1, 1), 0.0)


def test_measure_patch_serializes_as_before_unless_windowed():
    p = MeasurePatch("a", (1, 2, 3), (0.1, 0.2, 0.3))
    assert "window_area_pct" not in p.as_dict() and "repaint" not in p.as_dict()
    q = MeasurePatch("a", (1, 2, 3), (0.1, 0.2, 0.3), window_area_pct=5.0, repaint=False)
    assert q.as_dict()["window_area_pct"] == 5.0 and q.as_dict()["repaint"] is False


def test_plan_runs_up_then_mirrored_down_with_one_turn():
    spec = pw.PeakWindowSpec(areas=(1.0, 10.0, 100.0))
    assert pw.plan_sequence(spec) == [("up", 1.0), ("up", 10.0), ("up", 100.0), ("down", 10.0), ("down", 1.0)]
    no_ff = dataclasses.replace(spec, include_full_field=False)
    assert pw.plan_sequence(no_ff) == [("up", 1.0), ("up", 10.0), ("down", 1.0)]


def test_emissive_defaults_and_the_wear_cap_refusal():
    em = pw.PeakWindowSpec.for_display(True)
    lcd = pw.PeakWindowSpec.for_display(False)
    assert em.hold_cap_s < lcd.hold_cap_s and em.total_peak_cap_s < lcd.total_peak_cap_s
    assert em.full_field_hold_max_s <= em.hold_cap_s
    assert not em.problems() and not lcd.problems()
    tight = pw.PeakWindowSpec.for_display(True, total_peak_cap_s=60.0)
    assert any("exceeds the total cap" in p for p in tight.problems())
    assert any("hard single-hold cap" in p for p in
               pw.PeakWindowSpec.for_display(True, hold_max_s=45.0).problems())
    with pytest.raises(ValueError):
        pw.PeakWindowSpec.for_display(False, bogus=1)
    est = pw.estimate(em)
    assert est["peak_white_s"]["worst"] <= em.total_peak_cap_s and est["full_field"] is True


def test_fit_decay_recovers_a_limiter_time_constant():
    ts = [0.3 + 0.4 * i for i in range(30)]
    ys = [400.0 + 600.0 * math.exp(-t / 1.7) for t in ts]
    fit = pw.fit_decay(ts, ys)
    assert fit["resolved"] and fit["tau_s"] == pytest.approx(1.7, rel=0.08)
    flat = pw.fit_decay(ts, [500.0] * 30)
    assert flat["tau_s"] is None


def test_tail_settled_needs_a_flat_tail():
    ts = [0.5 * i for i in range(12)]
    assert pw.tail_settled(ts, [100.0] * 12, span_s=2.0, rel=0.005, abs_nits=0.05) is not None
    assert pw.tail_settled(ts, [100.0 - i for i in range(12)], span_s=2.0, rel=0.005, abs_nits=0.05) is None


# ----------------------------------------------------------------------------- the flow
_OLED_YES = {"peak-window:consent": Decision("approve"), "peak-window:full-field": Decision("include"),
             "peak-window:record": Decision("record")}


def test_oled_like_panel_shows_a_steep_curve_and_is_recorded(tmp_path):
    panel = oled_like(clock=VirtualClock())
    cal = _calib(tmp_path, "oled", panel, adjudicator=MappingAdjudicator(dict(_OLED_YES)))
    res = cal.run("peak-window")
    assert res.status == "completed", res.digest
    s = _recorded(cal)
    assert s.valid and s.agreement["ok"]
    c = {e["area_pct"]: e for e in s.curve}
    assert c[1.0]["sustained_nits"] == pytest.approx(1000.0, rel=0.02)
    assert c[100.0]["sustained_nits"] < 0.35 * c[1.0]["sustained_nits"]     # the power limiter
    assert c[50.0]["early_nits"] > c[50.0]["sustained_nits"] * 1.05           # the limiter acts over the hold
    assert c[50.0]["tau_s"] is not None
    assert s.run_window_area_pct == pytest.approx(10.0)
    assert s.full_field_sustained_nits == c[100.0]["sustained_nits"]
    assert s.conditions["stack"] == "native" and s.conditions["emissive"] is True
    # the emissive wear caps held
    assert s.wear["longest_hold_s"] <= pw.PeakWindowSpec.for_display(True).hold_cap_s
    assert s.wear["peak_white_s"] <= pw.PeakWindowSpec.for_display(True).total_peak_cap_s
    # nothing applied, display restored, panel left on black
    assert cal.controller.calibration_status().get("active") is False
    assert panel.frames[-1]["rgb"] == [0, 0, 0]
    # every hold was a windowed frame, never the presenter's own window
    assert all(f["area"] is not None for f in panel.frames)
    # the result file + the live chart
    out = json.loads((cal.ctx.root / "results" / "peak_window.json").read_text(encoding="utf-8"))
    assert out["valid"] is True and out["schema"] == 1
    charts = [e for e in read_events(cal.ctx.events_path) if e.event == "probe_chart"]
    assert charts and charts[-1].data["chart"] == "peak_by_window" and charts[-1].data["complete"] is True
    assert charts[-1].data["x"]["scale"] == "log"
    assert any(e.event == "check_in" and e.stage == pw.STAGE for e in read_events(cal.ctx.events_path))


def test_dashboard_serves_the_probe_chart(tmp_path):
    from dlc.dashboard.state import DashboardState
    cal = _calib(tmp_path, "dash", oled_like(clock=VirtualClock()), adjudicator=MappingAdjudicator(dict(_OLED_YES)))
    cal.run("peak-window")
    st = DashboardState()
    for ev in read_events(cal.ctx.events_path):
        st.ingest(ev)
    probes = st.charts()["probes"]
    assert probes["peak_by_window"]["valid"] is True
    assert [p[0] for p in probes["peak_by_window"]["curve"]] == list(pw.DEFAULT_AREAS)


def test_mini_led_like_panel_is_flat_and_stack_installed_is_recorded(tmp_path):
    panel = mini_led_like(clock=VirtualClock(), thermal=ThermalSag(sag=0.01, tau_s=3000.0))
    cal = _calib(tmp_path, "mini", panel, tech="mini-LED IPS", spec={"stack": "installed"})
    res = cal.run("peak-window")
    assert res.status == "completed", res.digest
    s = _recorded(cal)
    assert s.valid
    c = {e["area_pct"]: e for e in s.curve}
    assert c[100.0]["sustained_nits"] > 0.6 * s.plateau_nits                 # flat-ish: no OLED-class limiter
    assert s.conditions["stack"] == "installed" and s.conditions["emissive"] is False
    assert "enter-neutral" not in cal.calib["stages"]
    assert s.conditions["stack_view"] is not None


def test_drift_fails_the_up_down_agreement_and_is_not_averaged(tmp_path):
    panel = mini_led_like(clock=VirtualClock(), thermal=None, drift_per_hour=-0.6)
    cal = _calib(tmp_path, "drift", panel, tech="mini-LED IPS",
                 adjudicator=MappingAdjudicator({"peak-window:consent": Decision("approve"),
                                                 "peak-window:record": Decision("record")}))
    res = cal.run("peak-window")
    assert res.status == "completed"
    s = _recorded(cal)
    assert not s.valid and not s.agreement["ok"]
    assert s.agreement["systematic"] is True and s.agreement["mean_down_minus_up_rel"] < 0
    failed = set(s.agreement["areas_failed"])
    assert failed
    for e in s.curve:
        if e["area_pct"] in failed:
            assert e["sustained_nits"] is None and e["up"]["sustained_nits"] and e["down"]["sustained_nits"]
    assert any("drifted over the run" in n for n in s.notes)


def test_the_record_seam_recommends_discard_for_an_invalid_curve(tmp_path):
    panel = mini_led_like(clock=VirtualClock(), thermal=None, drift_per_hour=-0.6)
    cal = _calib(tmp_path, "rec", panel, tech="mini-LED IPS",
                 adjudicator=MappingAdjudicator({"peak-window:consent": Decision("approve")}))
    with pytest.raises(AdjudicationRequired) as ei:
        cal.run("peak-window")
    req = ei.value.request
    assert req.key == "peak-window:record" and req.recommendation == "discard" and req.digest["gate_failed"]
    # paused AFTER the display was restored; the resume records without measuring again
    assert cal.controller.calibration_status().get("active") is False
    n = panel.reads
    cal2 = _calib(tmp_path, "rec", panel, tech="mini-LED IPS",
                  adjudicator=MappingAdjudicator({"peak-window:consent": Decision("approve"),
                                                  "peak-window:record": Decision("discard")}))
    res = cal2.run("peak-window")
    assert res.status == "completed" and res.digest["recorded"] is None
    assert panel.reads == n
    assert DipStore.load(cal2._dip_store().path).get(f"{cal2.display.name}:HDR").peak_by_window is None


def test_consent_comes_before_anything_is_shown(tmp_path):
    panel = oled_like(clock=VirtualClock())
    cal = _calib(tmp_path, "consent", panel, adjudicator=MappingAdjudicator({}))
    with pytest.raises(AdjudicationRequired) as ei:
        cal.run("peak-window")
    req = ei.value.request
    assert req.key == "peak-window:consent"
    d = req.digest["estimate"]
    assert d["peak_white_s"]["cap"] == 300.0 and d["longest_hold_s"]["cap"] == 30.0
    assert "Time at peak white" in req.question and "longest single hold" in req.question
    assert panel.frames == [] and panel.reads == 0
    assert cal.controller.calibration_status().get("active") is False


def test_emissive_full_field_needs_its_own_consent(tmp_path):
    panel = oled_like(clock=VirtualClock())
    cal = _calib(tmp_path, "ff", panel, adjudicator=MappingAdjudicator({
        "peak-window:consent": Decision("approve"), "peak-window:full-field": Decision("skip"),
        "peak-window:record": Decision("record")}))
    assert cal.run("peak-window").status == "completed"
    assert all((f["area"] or 0) < 100.0 or f["rgb"] == [0, 0, 0] for f in panel.frames)
    s = _recorded(cal)
    assert max(e["area_pct"] for e in s.curve) == 75.0 and s.full_field_sustained_nits is None


def test_refusals_before_anything_is_shown(tmp_path):
    panel = oled_like(clock=VirtualClock())
    cal = _calib(tmp_path, "nodip", panel, dip=False)
    res = cal.run("peak-window")
    assert res.status == "aborted" and "characterize it first" in res.digest["message"]
    cal2 = _calib(tmp_path, "cap", panel, spec={"total_peak_cap_s": 60})
    res2 = cal2.run("peak-window")
    assert res2.status == "aborted" and "exceeds the total cap" in res2.digest["message"]
    ctrl = CalibrationController.mock()
    cal3 = _calib(tmp_path, "sdr", panel, controller=ctrl)
    ctrl.set_hdr(0, False)
    res3 = cal3.run("peak-window")
    assert res3.status == "aborted" and "not in HDR" in res3.digest["message"]
    assert panel.frames == []


def test_cancel_mid_hold_restores_the_display(tmp_path):
    panel = oled_like(clock=VirtualClock())
    holder = {}

    def measure(patch):
        r = panel(patch)
        if panel.reads == 40:
            (holder["cal"].ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")
        return r
    measure.probe_clock = panel.probe_clock
    cal = _calib(tmp_path, "cancel", measure)
    holder["cal"] = cal
    res = cal.run("peak-window")
    assert res.status == "aborted" and res.digest.get("cancelled") is True
    assert cal.controller.calibration_status().get("active") is False
    assert cal.calib["peak_window"]["display_restored"] is True
    assert DipStore.load(cal._dip_store().path).get(f"{cal.display.name}:HDR").peak_by_window is None


def test_meter_loss_stops_and_restores(tmp_path):
    panel = oled_like(clock=VirtualClock(), fail_after_reads=25)
    cal = _calib(tmp_path, "meter", panel)
    res = cal.run("peak-window")
    assert res.status == "aborted"
    stage = cal.calib["stages"]["peak-window"]
    assert stage["digest"].get("meter_down") is True
    assert cal.controller.calibration_status().get("active") is False


def test_a_hold_that_never_settles_stops_at_its_max_and_is_flagged(tmp_path):
    slow = oled_like(clock=VirtualClock(), limiter=Limiter(knee_pct=5.0, exponent=0.6, tau_s=400.0))
    cal = _calib(tmp_path, "slow", slow, spec={"areas": [1, 25, 50]})
    assert cal.run("peak-window").status == "completed"
    s = DipStore.load(cal._dip_store().path).get(f"{cal.display.name}:HDR").peak_by_window
    big = [st for st in s.steps if st["area_pct"] == 50.0]
    assert big and not big[0]["settled"] and big[0]["hold_s"] <= 15.0 + 1.0
    assert any("before settling" in n for n in s.notes)
    assert any(e.event == "anomaly" and e.data.get("kind") == "hold_unsettled"
               for e in read_events(cal.ctx.events_path))


def test_dip_round_trip_and_an_unreadable_section_is_dropped(tmp_path):
    cal = _calib(tmp_path, "rt", oled_like(clock=VirtualClock()))
    cal.run("peak-window")
    path = cal._dip_store().path
    s = _recorded(cal)
    again = PeakByWindow.from_dict(json.loads(json.dumps(s.as_dict())))
    assert again.as_dict() == s.as_dict()
    raw = json.loads(path.read_text(encoding="utf-8"))
    key = f"{cal.display.name}:HDR"
    raw["displays"][key]["peak_by_window"]["curve"][0]["area_pct"] = 250
    path.write_text(json.dumps(raw), encoding="utf-8")
    rec = DipStore.load(path).get(key)
    assert rec is not None and rec.peak_by_window is None
    assert any("peak_by_window dropped" in n for n in rec.notes)
    raw["displays"][key]["peak_by_window"] = {"schema": 2, "measured": "2026-11-12", "curve": [{"area_pct": 1}]}
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert DipStore.load(path).get(key).peak_by_window is None


def test_characterize_keeps_the_probe_sections(tmp_path):
    from dlc.characterize import CharacterizeConfig
    from dlc.measure_loop import SyntheticPanel
    from dlc.engine.patches import Transfer
    cal = _calib(tmp_path, "keep", oled_like(clock=VirtualClock()))
    cal.run("peak-window")
    assert _recorded(cal) is not None
    ctx = create_run("HDR", display="synthetic", run_dir=tmp_path / "char")
    char = Calibration(ctx=ctx, profile=cal.profile, monitor=0, mode="HDR", controller=CalibrationController.mock(),
                       measure=SyntheticPanel(transfer=Transfer.pq(bit_depth=10), native_white_nits=1000.0, start_temp=1.0,
                                              cold_blue_gain=1.0),
                       adjudicator=AutoAdjudicator(), run_date=_DATE, bit_depth=10,
                       characterize_config=CharacterizeConfig(noise_levels=(1.0, 0.2), noise_reads=4, black_reads=2,
                                                              primary_reads=1, settle_levels={"bright": 1.0},
                                                              warmup_observe_reads=8, creep_reads=2,
                                                              warmup_max_minutes=0))
    res = char.run("characterize")
    assert res.status == "completed"
    assert any("no refresh response measured" in g for g in res.digest["gaps"])
    assert not any("peak-by-window" in g for g in res.digest["gaps"])         # it has one now
    assert DipStore.load(char._dip_store().path).get(f"{cal.display.name}:HDR").peak_by_window is not None


def test_plan_cli_prints_and_writes_a_spec(tmp_path, capsys):
    out = tmp_path / "spec.json"
    assert pw.main(["plan", "--emissive", "--areas", "1,10,100", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert '"sequence"' in printed
    spec = json.loads(out.read_text(encoding="utf-8"))
    assert spec["probe"] == "peak-window" and spec["areas"] == [1.0, 10.0, 100.0] and spec["emissive"] is True
    assert pw.main(["plan", "--emissive", "--spec", str(out)]) == 0


def test_an_oled_black_below_the_meter_range_is_not_meter_loss(tmp_path):
    from dlc.measure_loop import Reading
    panel = oled_like(clock=VirtualClock())

    def measure(patch):
        r = panel(patch)
        if tuple(patch.rgb) == (0, 0, 0):        # spotread's under-range warning on a true black
            return Reading(xyz=None, ok=False, error="Warning: the reading is under range", raw={})
        return r
    measure.probe_clock = panel.probe_clock
    cal = _calib(tmp_path, "black", measure, adjudicator=MappingAdjudicator(dict(_OLED_YES)))
    res = cal.run("peak-window")
    assert res.status == "completed", res.digest
    s = _recorded(cal)
    assert s.valid and all(st["cooldown_settled"] and st["black_nits"] is None for st in s.steps)
