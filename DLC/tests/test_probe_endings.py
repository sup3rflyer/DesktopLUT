"""Every ending of the two probe flows leaves the display as it started, CONFIRMED by read-back — and a run that
died without its own restore is recovered by the command an overseer already knows (``--abort``), with an honest
status (restored only when the read-back says so; otherwise the command to finish it), a second ``--abort`` harmless.

Endings, for both flows: cancel (control.json), pause timeout, meter loss, a stage error, a kill (no in-process
restore) then ``--abort``; plus the CLI rollback guard's restore. On the simulators only.
"""
from __future__ import annotations

import importlib
import json

import pytest

from dlc import calibrate as C
from dlc import peak_window as pw
from dlc import probe_kit
from dlc import refresh_mode as rm
from dlc import refresh_probe as rp
from dlc.adjudication import Decision, MappingAdjudicator
from dlc.calibrate import StageError
from dlc.controller import CalibrationController
from dlc.liveness import RunCancelled
from dlc.probe_sim import VirtualClock, oled_like

tpw = importlib.import_module("test_peak_window")
trp = importlib.import_module("test_refresh_probe")

USER_LAYERS = {"tonemap": True, "desktop_gamma": True}


def _ctrl() -> CalibrationController:
    c = CalibrationController.mock()
    c.set_hdr(0, True)
    c.set_layers(0, "HDR", **USER_LAYERS)     # the user's viewing layers: the run switches them off, must restore
    return c


def _layers(ctrl) -> dict:
    return dict(((ctrl.state() or {}).get("layers") or {}).get("0:HDR") or {})


def _user_layers_back(ctrl) -> bool:
    lay = _layers(ctrl)
    return all(lay.get(k) is v for k, v in USER_LAYERS.items())


# ----------------------------------------------------------------------------- peak-window
def _pw(tmp_path, name, **kw):
    yes = MappingAdjudicator(dict(tpw._OLED_YES))
    panel = kw.pop("panel", None) or oled_like(clock=VirtualClock())
    cal = tpw._calib(tmp_path, name, panel, adjudicator=yes, controller=_ctrl(), **kw)
    return cal, panel


def _pw_back(cal) -> bool:
    conf = probe_kit.confirm_native_restored(cal.controller, cal.calib, monitor=0, mode="HDR")
    return bool(conf["confirmed"]) and _user_layers_back(cal.controller)


def test_peak_window_completed_is_confirmed(tmp_path):
    cal, _ = _pw(tmp_path, "ok")
    res = cal.run("peak-window")
    assert res.status == "completed"
    assert res.digest["display_confirmed"]["confirmed"] is True
    assert _pw_back(cal)


def _fail_on_nth(monkeypatch, cls, name, n, exc):
    real = getattr(cls, name)
    calls = {"n": 0}

    def f(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == n:
            raise exc
        return real(self, *a, **k)
    monkeypatch.setattr(cls, name, f)


@pytest.mark.parametrize("ending", ["cancel", "pause_timeout", "meter_loss", "stage_error"])
def test_peak_window_endings_restore_the_display(tmp_path, monkeypatch, ending):
    if ending == "meter_loss":
        cal, panel = _pw(tmp_path, ending, panel=oled_like(clock=VirtualClock(), fail_after_reads=30))
    else:
        cal, panel = _pw(tmp_path, ending)
    if ending == "cancel":
        real = pw._Run.hold

        def hold(self, area, label):
            if len(self.steps) == 3:
                (cal.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")
            return real(self, area, label)
        monkeypatch.setattr(pw._Run, "hold", hold)
    elif ending == "pause_timeout":
        real_check = cal.liveness.check
        n = {"k": 0}

        def check(stage):
            n["k"] += 1
            if n["k"] == 60:
                raise RunCancelled(stage, "pause timeout")
            return real_check(stage)
        cal.liveness.check = check
    elif ending == "stage_error":
        _fail_on_nth(monkeypatch, pw._Run, "hold", 4, StageError(pw.STAGE, "the presenter refused a frame"))
    res = cal.run("peak-window")
    assert res.status == "aborted", (ending, res.digest)
    assert _pw_back(cal), ending


def test_peak_window_killed_then_abort_twice(tmp_path, monkeypatch):
    cal, _ = _pw(tmp_path, "kill")
    _fail_on_nth(monkeypatch, pw._Run, "hold", 3, KeyboardInterrupt())
    monkeypatch.setattr(cal, "_restore_user_setup", lambda **k: True)       # the process dies: no restore runs
    monkeypatch.setattr(cal, "_restore_viewing_layers", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        cal.run("peak-window")
    assert cal.controller.calibration_status().get("active") is True       # left in calibration mode
    assert not _user_layers_back(cal.controller)
    code, payload = C._abort_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                                     flow="peak-window")
    assert code == 0 and payload["status"] == "reverted", payload
    assert payload["probe_restore"]["outcome"] == "confirmed" and payload["display_may_differ"] is False
    assert _pw_back(cal)
    code2, again = C._abort_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                                    flow="peak-window")
    assert code2 == 0 and again["status"] == "reverted" and _pw_back(cal)


def test_peak_window_rollback_guard_confirms(tmp_path, monkeypatch):
    cal, _ = _pw(tmp_path, "guard")
    _fail_on_nth(monkeypatch, pw._Run, "hold", 2, KeyboardInterrupt())
    monkeypatch.setattr(cal, "_restore_user_setup", lambda **k: True)
    monkeypatch.setattr(cal, "_restore_viewing_layers", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        cal.run("peak-window")
    out = C._rollback_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                              entered_calibration=C._run_entered_calibration(cal.calib, flow="peak-window"))
    assert out["status"] == "rolled_back", out
    assert _pw_back(cal)


# ----------------------------------------------------------------------------- refresh-response
def _rr(tmp_path, name, **kw):
    cal, api, panel = trp._calib(tmp_path, name, controller=_ctrl(), **kw)
    return cal, api, panel


def _rr_back(cal, api) -> bool:
    at = tuple(rm.find_display(api.snapshot(), "SIM0000")["active"]["vsync"])
    reg = api.legacy_registry(r"\\.\DISPLAY1")["hz"]
    return at == (60, 1) and reg == 60 and _layers(cal.controller).get("bypass_3dlut") is False \
        and _user_layers_back(cal.controller)


@pytest.mark.parametrize("ending", ["cancel", "pause_timeout", "meter_loss", "stage_error"])
def test_refresh_endings_restore_the_display(tmp_path, monkeypatch, ending):
    from dlc.probe_sim import ProbeSimPanel, period_refresh_gain
    kw = {}
    if ending == "meter_loss":
        api = rp.sim_desk_for_mock()
        kw["api"] = api
        kw["panel"] = ProbeSimPanel(clock=VirtualClock(), refresh_gain=period_refresh_gain(trp.effect),
                                    hz_fn=lambda: rm.active_hz(rm.find_display(api.snapshot(), "SIM0000")),
                                    fail_after_reads=400)
    cal, api, panel = _rr(tmp_path, ending, **kw)
    real_leg = rp._Probe.leg

    def leg(self, lg):
        if lg["rate"] == "47.952":
            if ending == "cancel":
                (cal.ctx.root / "control.json").write_text(json.dumps({"action": "cancel"}), encoding="utf-8")
            elif ending == "stage_error":
                raise StageError(rp.STAGE, "DesktopLUT refused a patch")
            elif ending == "pause_timeout":
                raise RunCancelled(rp.STAGE, "pause timeout")
        return real_leg(self, lg)
    monkeypatch.setattr(rp._Probe, "leg", leg)
    res = cal.run("refresh-response")
    assert res.status == "aborted", (ending, res.digest)
    assert _rr_back(cal, api), ending
    assert cal.calib["refresh_probe"]["restore"]["outcome"] == "restored"


def test_refresh_killed_then_abort_twice(tmp_path, monkeypatch):
    cal, api, _ = _rr(tmp_path, "kill")
    monkeypatch.setattr(rp, "_restore_all", lambda *a, **k: {"skipped": "the process died"})
    monkeypatch.setattr(cal, "_restore_viewing_layers", lambda: None)
    real_leg = rp._Probe.leg

    def leg(self, lg):
        if lg["rate"] == "47.952":
            raise KeyboardInterrupt
        return real_leg(self, lg)
    monkeypatch.setattr(rp._Probe, "leg", leg)
    with pytest.raises(KeyboardInterrupt):
        cal.run("refresh-response")
    assert not _rr_back(cal, api)                                   # at 47.952, the 3D LUT bypassed, layers off
    code, payload = C._abort_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                                     flow="refresh-response", display_api=api)
    assert code == 0 and payload["status"] == "reverted", payload
    pr = payload["probe_restore"]
    assert pr["outcome"] == "confirmed" and pr["display"]["outcome"] == "restored"
    assert pr["display"]["mode"]["final"]["vsync"] == [60, 1]
    assert _rr_back(cal, api)
    code2, again = C._abort_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                                    flow="refresh-response", display_api=api)
    assert code2 == 0 and again["status"] == "reverted" and _rr_back(cal, api)
    assert again["probe_restore"]["display"]["mode"]["needed"] is False
    assert len(cal.calib["refresh_probe"]["abort_restore_history"]) == 1


def test_refresh_abort_that_cannot_restore_says_so(tmp_path, monkeypatch):
    cal, api, _ = _rr(tmp_path, "honest")
    monkeypatch.setattr(rp, "_restore_all", lambda *a, **k: {"skipped": "the process died"})
    real_leg = rp._Probe.leg

    def leg(self, lg):
        if lg["rate"] == "47.952":
            raise KeyboardInterrupt
        return real_leg(self, lg)
    monkeypatch.setattr(rp._Probe, "leg", leg)
    with pytest.raises(KeyboardInterrupt):
        cal.run("refresh-response")
    api.fail_rc = {60: -1}                                          # the driver now refuses 60 Hz
    api.registry[r"\\.\DISPLAY1"] = (48000, 1001)                   # and the saved mode is not the original
    code, payload = C._abort_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                                     flow="refresh-response", display_api=api)
    assert payload["status"] != "reverted" and payload["display_may_differ"] is True
    d = payload["probe_restore"]["display"]
    assert d["display_may_be_at_probe_rate"] is True
    assert "--abort" in payload["hint"]


def test_refresh_rollback_guard_restores(tmp_path, monkeypatch):
    cal, api, _ = _rr(tmp_path, "guard")
    monkeypatch.setattr(rp, "_restore_all", lambda *a, **k: {"skipped": "the process died"})
    real_leg = rp._Probe.leg

    def leg(self, lg):
        if lg["rate"] == "47.952":
            raise KeyboardInterrupt
        return real_leg(self, lg)
    monkeypatch.setattr(rp._Probe, "leg", leg)
    with pytest.raises(KeyboardInterrupt):
        cal.run("refresh-response")
    out = C._rollback_restore(cal.controller, cal.calib, monitor=0, mode="HDR", run_root=cal.ctx.root,
                              entered_calibration=C._run_entered_calibration(cal.calib, flow="refresh-response"),
                              display_api=api)
    assert out["status"] == "rolled_back", out
    assert _rr_back(cal, api)


# ----------------------------------------------------------------------------- the recorded refresh rate
def test_the_run_records_the_display_refresh_and_the_probe_states_it(tmp_path):
    from dlc import stack_registry
    cal, api, _ = _rr(tmp_path, "rec")
    reg_path = stack_registry.registry_path(cal.profile, cal.ctx.root)
    reg = stack_registry.StackRegistry.load(reg_path)
    rec = stack_registry.StackRecord(display=cal.display.name, mode="HDR", monitor=0, run_id="20261101_cal",
                                     applied_at="2026-11-01T10:00:00",
                                     refresh={"rational": [60, 1], "hz": 60.0, "rate": "60", "run_id": "20261101_cal"})
    reg.record(rec)
    adj = MappingAdjudicator({})
    cal.adjudicator = adj
    with pytest.raises(C.AdjudicationRequired) as ei:
        cal.run("refresh-response")
    req = ei.value.request
    assert req.key == "refresh-response:plan"
    assert req.digest["reference_basis"] == "recorded" and "recorded by run 20261101_cal" in req.question
    assert cal.calib["display_refresh"]["rational"] == [60, 1] and cal.calib["display_refresh"]["rate"] == "60"
    # a spec that names another reference: the plan asks
    cal2, _, _ = _rr(tmp_path, "rec2", spec={"reference": "59.94"})
    cal2.adjudicator = MappingAdjudicator({})
    res = cal2.run("refresh-response")         # the display runs at 60, not 59.94: refused before anything changes
    assert res.status == "aborted" and "not at the reference" in res.digest["message"]
    assert cal2.calib["refresh_probe"]["reference_basis"] == "recorded_disagrees"


def test_registry_and_dip_refresh_fields_round_trip_and_old_records_load():
    from dlc import stack_registry
    from dlc.dip import DisplayInstrumentProfile
    r = stack_registry.StackRecord(display="d", mode="HDR", monitor=0, run_id="x", applied_at="t")
    assert "refresh" not in r.as_dict()
    r.refresh = {"rational": [48000, 1001], "hz": 47.952048, "rate": "47.952"}
    assert stack_registry.StackRecord.from_dict(r.as_dict()).refresh == r.refresh
    d = DisplayInstrumentProfile(display="d", mode="HDR")
    assert "measured_refresh" not in d.as_dict()
    d.measured_refresh = {"rational": [60, 1], "hz": 60.0}
    assert DisplayInstrumentProfile.from_dict(d.as_dict()).measured_refresh == d.measured_refresh
