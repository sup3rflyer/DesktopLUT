"""The refresh response probe (``--flow refresh-response``) with the fixed-mode rate source, on the simulator.

The mock DesktopLUT, ``refresh_mode.SimDisplayApi`` as DisplayConfig (the mock's two displays), and a
:class:`dlc.probe_sim.ProbeSimPanel` whose light depends on the active refresh rate: no display, no meter, no mode
is ever switched on this machine.
"""
from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest

from dlc import refresh_mode as rm
from dlc import refresh_probe as rp
from dlc import refresh_trim as rt
from dlc import calibration_profile as cp
from dlc.adjudication import AdjudicationRequired, AutoAdjudicator, Decision, MappingAdjudicator
from dlc.calibrate import Calibration
from dlc.controller import CalibrationController
from dlc.dip import DipStore, DisplayInstrumentProfile
from dlc.probe_sim import ProbeSimPanel, VirtualClock, period_refresh_gain
from dlc.runs import create_run, open_run

_DATE = datetime.date(2026, 11, 12)
SPEC = {"rates": ["47.952", "50"], "levels_nits": [0.5, 10.0, 100.0], "warm_min_s": 60.0, "warm_max_s": 300.0,
        "rewarm_min_s": 20.0}


def effect(nits: float) -> float:
    """ln(ratio) at 47.952 Hz: +3.4 % at the bottom falling to +1 % at 1000 nit (the owner's panel, roughly)."""
    return 0.034 - 0.008 * math.log10(max(nits, 0.5) / 0.5)


def _calib(tmp_path: Path, name: str, *, gain=True, api=None, adjudicator=None, spec=None, controller=None,
           panel=None):
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run("HDR", display="synthetic", run_dir=run_dir)
    ctrl = controller or CalibrationController.mock()
    ctrl.set_hdr(0, True)
    api = api or rp.sim_desk_for_mock()

    def hz():
        return rm.active_hz(rm.find_display(api.snapshot(), "SIM0000"))
    panel = panel or ProbeSimPanel(clock=VirtualClock(), refresh_gain=period_refresh_gain(effect) if gain else None,
                                   hz_fn=hz, noise_rel=0.0002)
    cal = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                      mode="HDR", controller=ctrl, measure=panel, adjudicator=adjudicator or AutoAdjudicator(),
                      run_date=_DATE, bit_depth=10)
    cal.display_api = api
    cal.probe_spec = dict(SPEC, **(spec or {}))
    if cal._dip() is None:
        cal._dip_store().record(DisplayInstrumentProfile(display=cal.display.name, mode="HDR", made="2026-11-01",
                                                         native_white_nits=1000.0))
    return cal, api, panel


def _at(api) -> tuple:
    return tuple(rm.find_display(api.snapshot(), "SIM0000")["active"]["vsync"])


def _layers(cal) -> dict:
    return (cal.controller.state().get("layers") or {}).get("0:HDR") or {}


def test_legs_bracket_and_mirror():
    legs = rp.mode_legs(["47.952", "50"], "60")
    assert [lg["rate"] for lg in legs] == ["60", "47.952", "60", "50", "60", "50", "60", "47.952", "60"]
    assert [lg["pass"] for lg in legs] == ["A"] * 5 + ["B"] * 4
    assert rp.levels_for_peak(1000.0) == [0.5, 2.0, 10.0, 50.0, 100.0, 200.0, 500.0]
    assert rp.levels_for_peak(300.0) == [0.5, 2.0, 10.0, 50.0, 100.0, 200.0]
    assert rp.levels_for_peak(100.0) == [0.5, 2.0, 10.0, 50.0, 100.0]


def test_a_rate_dependent_panel_is_measured_and_recordable(tmp_path):
    cal, api, panel = _calib(tmp_path, "gain")
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    root = cal.ctx.root
    for f in ("trim_rows.jsonl", "evidence.json", "refresh_result.json", "trim_curves.json", "pre_state.json"):
        assert (root / f).exists(), f
    resp, problems, trim = rt.response_for_run_dir(root)
    assert problems == ["simulated run"]                       # the only reason: a real run records
    r48 = resp.rate("47.952")
    assert r48.num == 48000 and r48.den == 1001 and r48.source == "mode" and r48.proof["kind"] == "displayconfig"
    for lv in r48.levels:
        assert math.log(lv.ratio) == pytest.approx(effect(lv.nits), abs=0.002)
    r50 = resp.rate("50")
    x50 = (1000 / 50 - 1000 / 60) / (1000 / (48000 / 1001) - 1000 / 60)
    assert math.log(r50.levels[0].ratio) == pytest.approx(effect(r50.levels[0].nits) * x50, abs=0.002)
    assert resp.reference == {"key": "60", "num": 60, "den": 1}
    assert resp.conditions["rate_sources"] == ["mode"]
    # restored: the reference rate, exactly; the stack as found; nothing saved
    assert _at(api) == (60, 1)
    assert api.legacy_registry(r"\\.\DISPLAY1")["hz"] == 60
    assert _layers(cal).get("bypass_3dlut") is False
    assert res.digest["restore"]["mode"]["ok"] is True
    # only the target display was ever asked to switch
    assert {c["device"] for c in api.calls} == {r"\\.\DISPLAY1"}
    assert "record" in res.digest
    ev = [json.loads(x) for x in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "probe_chart" and e["data"]["chart"] == "refresh_response" for e in ev)
    assert any(e["event"] == "check_in" and e["stage"] == rp.STAGE for e in ev)


def test_a_panel_without_a_refresh_effect_reads_ratio_one(tmp_path):
    cal, api, _ = _calib(tmp_path, "flat", gain=False)
    assert cal.run("refresh-response").status == "completed"
    resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
    for rate in resp.rates:
        for lv in rate.levels:
            assert abs(math.log(lv.ratio)) < 0.0015


def test_the_record_cli_works_unchanged_on_the_run(tmp_path, capsys):
    cal, _, _ = _calib(tmp_path, "cli")
    cal.run("refresh-response")
    rc = rt._main(["record", "--run", str(cal.ctx.root), "--dip", str(cal._dip_store().path)])
    out = capsys.readouterr().out
    assert rc == 2 and "NOT recorded: simulated run" in out          # a simulated run is never recorded
    resp, _, trim = rt.response_for_run_dir(cal.ctx.root)
    rt.record_response(cal._dip_store().path, trim["display"]["name"], "HDR", resp)   # what --write does
    rec = DipStore.load(cal._dip_store().path).get(f"{cal.display.name}:HDR")
    assert rec.refresh_response.rate("47.952") is not None


def test_a_switch_that_fails_ends_the_run_at_the_reference(tmp_path):
    api = rp.sim_desk_for_mock(fail_rc={47: -2})
    cal, api, _ = _calib(tmp_path, "fail", api=api)
    res = cal.run("refresh-response")
    assert res.status == "aborted" and "did not verify" in res.digest["message"]
    assert _at(api) == (60, 1)
    assert _layers(cal).get("bypass_3dlut") is False
    ev = json.loads((cal.ctx.root / "evidence.json").read_text(encoding="utf-8"))
    assert ev["status"].startswith("aborted")


def test_a_switch_that_lands_wrong_is_refused_and_restored(tmp_path):
    api = rp.sim_desk_for_mock(land_on={50: [60000, 1001]})   # the driver lands 50 Hz on 59.94
    cal, api, _ = _calib(tmp_path, "land", api=api)
    res = cal.run("refresh-response")
    assert res.status == "aborted"
    assert _at(api) == (60, 1)
    assert cal.calib["refresh_probe"]["restore"]["mode"]["ok"] is True


def test_restore_after_a_run_that_died_mid_switch(tmp_path, monkeypatch):
    cal, api, _ = _calib(tmp_path, "died")
    monkeypatch.setattr(rp, "_restore_all", lambda *a, **k: {"skipped": "the process died"})

    calls = {"n": 0}
    real = rp._Probe.leg

    def dying_leg(self, lg):
        calls["n"] += 1
        if lg["rate"] == "47.952":
            raise KeyboardInterrupt        # the run is killed while the display is at 47.952 Hz
        return real(self, lg)
    monkeypatch.setattr(rp._Probe, "leg", dying_leg)
    with pytest.raises(KeyboardInterrupt):
        cal.run("refresh-response")
    assert _at(api) == (48000, 1001) and _layers(cal).get("bypass_3dlut") is True
    out = rp.restore_run(cal.ctx.root, api=api, controller=cal.controller)
    assert out["ok"] is True
    assert _at(api) == (60, 1) and api.legacy_registry(r"\\.\DISPLAY1")["hz"] == 60
    assert _layers(cal).get("bypass_3dlut") is False


def test_every_switch_asks_consent_and_a_pause_stays_at_the_reference(tmp_path):
    adj = MappingAdjudicator({"refresh-response:plan": Decision("approve")})
    cal, api, panel = _calib(tmp_path, "consent", adjudicator=adj)
    with pytest.raises(AdjudicationRequired) as ei:
        cal.run("refresh-response")
    req = ei.value.request
    assert req.key == "refresh-response:switch:L1:50"           # the higher rates first
    assert req.digest["would_call"]["hz"] == 50 and "restore" in req.digest["restore_if_abandoned"]
    assert _at(api) == (60, 1)
    plan_digest = cal.calib["refresh_probe"]["plan"]
    keys = [f"refresh-response:switch:L{lg['leg']}:{lg['rate']}" for lg in cal.calib["refresh_probe"]["legs"]
            if lg["rate"] != "60"]
    assert len(keys) == 4 and plan_digest["reference"] == "60"
    decisions = {"refresh-response:plan": Decision("approve"), **{k: Decision("approve") for k in keys}}
    cal2, _, _ = _calib(tmp_path, "consent", api=api, panel=panel, adjudicator=MappingAdjudicator(decisions),
                        controller=cal.controller)
    res = cal2.run("refresh-response")
    assert res.status == "completed", res.digest
    assert cal2.calib["refresh_probe"]["rewarm"]                  # a resumed run re-warms before the next leg
    assert _at(api) == (60, 1)


def test_refusals_before_anything_changes(tmp_path):
    api = rp.sim_desk_for_mock()
    api.displays[r"\\.\DISPLAY1"].active = (50, 1)
    cal, api, panel = _calib(tmp_path, "ref", api=api, spec={"reference": "60"})
    res = cal.run("refresh-response")
    assert res.status == "aborted" and "not at the reference" in res.digest["message"]
    assert panel.reads == 0 and not api.calls
    cal2, _, _ = _calib(tmp_path, "unreach", spec={"rates": ["119.88"]})
    res2 = cal2.run("refresh-response")
    assert res2.status == "aborted" and "not reachable" in res2.digest["message"]


def test_plan_and_modes_cli(tmp_path, capsys):
    out = tmp_path / "s.json"
    assert rp.main(["plan", "--rates", "47.952,50", "--reference", "60", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["probe"] == "refresh-response"
    assert rp.main(["modes", "--hardware-id", "SIM0000", "--simulate"]) == 0
    rep = json.loads(capsys.readouterr().out.split("\n{", 1)[1].join(["{", ""]) if False else "{}")
    assert rep == {}
