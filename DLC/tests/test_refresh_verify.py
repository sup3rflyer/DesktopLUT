"""The REFRESH TRIM verify (``--flow refresh-response`` with the spec's ``"purpose": "verify"``) on the simulator.

The mock DesktopLUT with a model of its ``state.get`` ``refresh_trim`` (``refresh_verify.sim_trim_model``), the
mock's DisplayConfig (``refresh_mode.SimDisplayApi``) and a ``ProbeSimPanel`` whose light at a rate is what the trim
leaves of the panel's response: no display, no meter, no mode is ever switched on this machine.
"""
from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest

from dlc import calibration_profile as cp
from dlc import refresh_mode as rm
from dlc import refresh_probe as rp
from dlc import refresh_trim as rt
from dlc import refresh_verify as rv
from dlc.adjudication import AdjudicationRequired, AutoAdjudicator, Decision, MappingAdjudicator
from dlc.calibrate import Calibration
from dlc.controller import CalibrationController
from dlc.dip import DisplayInstrumentProfile
from dlc.probe_sim import ProbeSimPanel, VirtualClock
from dlc.runs import create_run, open_run

_DATE = datetime.date(2026, 11, 13)
SPEC = {"purpose": "verify", "rates": ["47.952", "50"], "reference": "60", "colours": False,
        "levels_nits": [0.5, 10.0, 100.0], "warm_min_s": 60.0, "warm_max_s": 300.0, "rewarm_min_s": 20.0}


def effect(nits: float) -> float:
    return 0.034 - 0.008 * math.log10(max(nits, 0.5) / 0.5)


def _calib(tmp_path: Path, name: str, *, residual=0.0, trim_on=True, model_kw=None, adjudicator=None, spec=None,
           controller=None, api=None, panel_kw=None):
    run_dir = tmp_path / name
    ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
        else create_run("HDR", display="synthetic", run_dir=run_dir)
    ctrl = controller or CalibrationController.mock()
    ctrl.set_hdr(0, True)
    api = api or rp.sim_desk_for_mock()
    server = ctrl.client.transport.server
    server.refresh_trim_model = rv.sim_trim_model(api, "SIM0000", **(model_kw or {}))
    if trim_on and controller is None:
        ctrl.call("layers.set", {"monitor": 0, "mode": "HDR", "refresh_trim": True})

    def hz():
        return rm.active_hz(rm.find_display(api.snapshot(), "SIM0000"))

    def gain(h, nits):
        """The panel's response at h, minus what DesktopLUT's trim takes away when the model says it is on screen."""
        x = (1000.0 / h - 1000.0 / 60.0) / (1000.0 / (48000 / 1001) - 1000.0 / 60.0)
        st = server.refresh_trim_model({"layers": {"0:HDR": dict(server.state.layers.get("0:HDR", {}))}})["0:HDR"]
        on = st.get("state") == "trim" and bool(st.get("active_stamp"))      # a trimmed variant is on screen
        return math.exp(effect(nits) * x * (residual if on else 1.0))
    panel = ProbeSimPanel(clock=VirtualClock(), refresh_gain=gain, hz_fn=hz, **{"noise_rel": 0.0002, **(panel_kw or {})})
    cal = Calibration(ctx=ctx, profile=cp.Profile.synthetic(output_dir=str(tmp_path / "results")), monitor=0,
                      mode="HDR", controller=ctrl, measure=panel, adjudicator=adjudicator or AutoAdjudicator(),
                      run_date=_DATE, bit_depth=10)
    cal.display_api = api
    cal.probe_spec = dict(SPEC, **(spec or {}))
    if cal._dip() is None:
        cal._dip_store().record(DisplayInstrumentProfile(display=cal.display.name, mode="HDR", made="2026-11-01",
                                                         native_white_nits=1000.0))
    return cal, api, panel, ctrl


def _at(api) -> tuple:
    return tuple(rm.find_display(api.snapshot(), "SIM0000")["active"]["vsync"])


def _layers(ctrl) -> dict:
    return (ctrl.state().get("layers") or {}).get("0:HDR") or {}


def _trim_sent(ctrl) -> list:
    """Every layers.set that carried refresh_trim AFTER the test's own setup call."""
    reqs = [r for r in ctrl.client.transport.requests if r.method == "layers.set" and "refresh_trim" in (r.params or {})]
    return reqs[1:]


def test_a_trim_that_holds_the_light_passes_and_nothing_is_recorded(tmp_path, capsys):
    cal, api, panel, ctrl = _calib(tmp_path, "good")
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    assert res.digest["purpose"] == "verify" and res.digest["verdict"] == "pass", res.digest
    root = cal.ctx.root
    assert (root / rv.RESULT_FILE).exists() and (root / "trim_rows.jsonl").exists()
    assert not (root / "trim_curves.json").exists() and not (root / "refresh_result.json").exists()
    out = json.loads((root / rv.RESULT_FILE).read_text(encoding="utf-8"))
    for rate in ("47.952", "50"):
        assert out["rates"][rate]["verdict"] == "pass"
        assert out["rates"][rate]["trim_checks"] == 2 and out["rates"][rate]["trim_not_applied"] == 0
        for cell in out["rates"][rate]["greys"].values():
            assert abs(cell["pct"]) < 0.3 and cell["bracketed"] == 2
    # the trim was never switched by the run; the display back at 60 Hz exactly; the 3D LUT bypass put back
    assert _trim_sent(ctrl) == [] and _layers(ctrl).get("refresh_trim") is True
    assert _at(api) == (60, 1) and _layers(ctrl).get("bypass_3dlut") is False
    # the record CLI refuses a verify run outright
    rc = rt._main(["record", "--run", str(root), "--dip", str(cal._dip_store().path)])
    assert rc == 2 and "VERIFY run" in capsys.readouterr().out
    ev = [json.loads(x) for x in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "check_in" and e["stage"] == rp.STAGE for e in ev)


def test_a_trim_that_leaves_light_fails_on_luminance(tmp_path):
    cal, api, _, ctrl = _calib(tmp_path, "resid", residual=0.3)
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "fail"
    r48 = next(x for x in res.digest["rates"] if x["rate"] == "47.952")
    assert r48["verdict"] == "fail" and any("outside +/-0.3 %" in f for f in r48["fail"])
    assert _at(api) == (60, 1) and _layers(ctrl).get("refresh_trim") is True


def test_a_trim_variant_that_never_shows_fails_the_rate(tmp_path):
    cal, api, _, ctrl = _calib(tmp_path, "stuck", model_kw={"stuck": True})
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "fail"
    for line in res.digest["rates"]:
        assert any("was not on screen" in f for f in line["fail"])
    checks = cal.calib["refresh_probe"]["trim_checks"]
    assert any(c["rate"] == "47.952" and not c["ok"] for c in checks)
    assert all(c["ok"] for c in checks if c["rate"] == "60")


@pytest.mark.parametrize("case, kw, words", [
    ("off", {"trim_on": False}, "REFRESH TRIM is off"),
    ("nodata", {"model_kw": {"no_data": True}}, "no refresh-trim data"),
    ("unrecorded", {"model_kw": {"rates": ("50",)}}, "have no recorded response"),
])
def test_refusals_before_anything_changes(tmp_path, case, kw, words):
    cal, api, panel, ctrl = _calib(tmp_path, case, **kw)
    res = cal.run("refresh-response")
    assert res.status == "aborted" and words in res.digest["message"], res.digest
    assert panel.reads == 0 and not api.calls and _trim_sent(ctrl) == []


def test_ctrl_c_mid_leg_restores_the_rate_and_never_touches_the_trim(tmp_path, monkeypatch):
    cal, api, _, ctrl = _calib(tmp_path, "ctrlc")
    real = rp._Probe.leg

    def dying_leg(self, lg):
        if lg["rate"] == "47.952":
            assert _at(api) == (48000, 1001)
            raise KeyboardInterrupt
        return real(self, lg)
    monkeypatch.setattr(rp._Probe, "leg", dying_leg)
    with pytest.raises(KeyboardInterrupt):
        cal.run("refresh-response")
    assert _at(api) == (60, 1) and api.legacy_registry(r"\\.\DISPLAY1")["hz"] == 60
    assert _layers(ctrl).get("bypass_3dlut") is False and _layers(ctrl).get("refresh_trim") is True
    assert _trim_sent(ctrl) == []
    out = json.loads((cal.ctx.root / rv.RESULT_FILE).read_text(encoding="utf-8"))
    assert out["verdict"] != "pass" and out["meta"]["status"].startswith("aborted")


def test_a_meter_that_dies_mid_run_restores_the_rate(tmp_path):
    cal, api, panel, ctrl = _calib(tmp_path, "meter", panel_kw={"fail_after_reads": 40})
    try:
        status = cal.run("refresh-response").status
    except Exception as exc:  # noqa: BLE001 - a dead meter may end the run by raising
        status = f"raised {type(exc).__name__}"
    assert status != "completed" and panel.reads >= 40
    assert _at(api) == (60, 1)
    assert _layers(ctrl).get("bypass_3dlut") is False and _layers(ctrl).get("refresh_trim") is True


def test_switches_ask_consent_and_a_pause_stays_at_the_reference(tmp_path):
    adj = MappingAdjudicator({"refresh-response:plan": Decision("approve")})
    cal, api, panel, ctrl = _calib(tmp_path, "consent", adjudicator=adj)
    with pytest.raises(AdjudicationRequired) as ei:
        cal.run("refresh-response")
    assert ei.value.request.key == "refresh-response:switch:L1:50"
    assert _at(api) == (60, 1)
    plan_q = cal.calib["refresh_probe"]
    assert plan_q["spec"]["purpose"] == "verify"
    keys = [f"refresh-response:switch:L{lg['leg']}:{lg['rate']}" for lg in plan_q["legs"] if lg["rate"] != "60"]
    decisions = {"refresh-response:plan": Decision("approve"), **{k: Decision("approve") for k in keys}}
    cal2, _, _, _ = _calib(tmp_path, "consent", api=api, controller=ctrl, adjudicator=MappingAdjudicator(decisions))
    res = cal2.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "pass", res.digest
    assert _at(api) == (60, 1) and _trim_sent(ctrl) == []


def test_the_plan_seam_says_verify(tmp_path):
    adj = MappingAdjudicator({})
    cal, _, _, _ = _calib(tmp_path, "seam", adjudicator=adj)
    with pytest.raises(AdjudicationRequired) as ei:
        cal.run("refresh-response")
    req = ei.value.request
    assert req.key == "refresh-response:plan" and "VERIFY" in req.question and "never switched" in req.question
    assert req.options == ("approve", "abort")


def test_dip_rate_lines_and_the_leg_check():
    lines = ["47.952 Hz trim (3 levels)", "48 Hz = 47.952", "50 Hz trim (3 levels)", "24 Hz (no levels)"]
    assert rv.dip_rates(lines) == {"47.952": "47.952", "48": "47.952", "50": "50"}
    st = {"state": "trim", "rate": "47.952", "applied": True, "stamp": "s48", "active_stamp": "s48",
          "dip": {"rates": lines}}
    assert rv.trim_leg_check(st, "48", "60")["ok"] is True          # 48 uses the 47.952 curve
    assert rv.trim_leg_check({**st, "applied": False}, "47.952", "60")["ok"] is False
    assert rv.trim_leg_check({"state": "reference", "applied": True}, "60", "60")["ok"] is True
    assert rv.trim_leg_check({"state": "trim", "rate": "50", "applied": True}, "60", "60")["ok"] is False


def _row(leg, rate, t, y, xy, patch="g520"):
    x, yy = xy
    Y = y
    return {"leg": leg, "pass": "A", "rate": rate, "rate_hz": rm.rate_hz(rate), "patch": patch, "kind": "grey",
            "code": [520, 520, 520], "nominal_nits": 100.0, "t_s": t, "y": Y, "sd_y": 0.01, "n_kept": 3,
            "settled": True, "xyz": [x / yy * Y, Y, (1 - x - yy) / yy * Y], "source": "mode"}


def test_the_rule_luminance_and_chromaticity():
    ok = [{"rate": r, "ok": True} for r in ("60", "47.952", "60")]
    rows = [_row(0, "60", 0, 100.0, (0.3127, 0.3290)), _row(1, "47.952", 60, 100.1, (0.3127, 0.3290)),
            _row(2, "60", 120, 100.2, (0.3128, 0.3291))]
    res = rv.analyze_verify(rows, reference="60", trim_checks=ok)
    assert res["verdict"] == "pass"
    # light 0.5 % over the bracket -> fail
    rows_l = rows[:1] + [_row(1, "47.952", 60, 100.6, (0.3127, 0.3290))] + rows[2:]
    assert rv.analyze_verify(rows_l, reference="60", trim_checks=ok)["verdict"] == "fail"
    # light inside, chromaticity outside the reference reads' own spread -> undecided (the overseer judges)
    rows_c = rows[:1] + [_row(1, "47.952", 60, 100.1, (0.3140, 0.3290))] + rows[2:]
    res_c = rv.analyze_verify(rows_c, reference="60", trim_checks=ok)
    assert res_c["verdict"] == "undecided" and "chromaticity" in res_c["rates"]["47.952"]["undecided"][0]
    # no trim read-back at the rate -> undecided; a failed one -> fail
    assert rv.analyze_verify(rows, reference="60", trim_checks=[])["verdict"] == "undecided"
    bad = [{"rate": "47.952", "ok": False, "state": "trim", "applied": False}]
    assert rv.analyze_verify(rows, reference="60", trim_checks=bad)["verdict"] == "fail"


def test_spec_problems_and_plan_cli(tmp_path):
    assert rp.RefreshProbeSpec.from_dict({"purpose": "verify", "source": "vrr"}).problems()
    assert rp.RefreshProbeSpec.from_dict({"purpose": "nonsense"}).problems()
    assert rp.expected_stack(rp.RefreshProbeSpec(purpose="verify")) == {"bypass_3dlut": True, "refresh_trim": True}
    assert "refresh_trim" not in rp.probe_stack(rp.RefreshProbeSpec(purpose="verify"))
    out = tmp_path / "v.json"
    assert rp.main(["plan", "--purpose", "verify", "--rates", "47.952,50", "--reference", "60", "--out", str(out)]) == 0
    spec = json.loads(out.read_text(encoding="utf-8"))
    assert spec["purpose"] == "verify" and spec["colours"] is False


# ----------------------------------------------------------------------------- review 2026-11-13
def test_a_rate_recorded_same_as_the_reference_wants_no_trim():
    """The reviewer's repro (prep/review/repro_5994.py): the C++ picks Reference at 59.94 = 60 (state "reference",
    rate null); the leg check must want that, not "trim". Failed before the fix (want_state "trim", ok False)."""
    lines = ["59.94 Hz = 60", "50 Hz trim (3 levels)", "48 Hz = 47.952", "47.952 Hz trim (3 levels)"]
    state = {"layers": {"0:HDR": {"refresh_trim": True}},
             "refresh_trim": {"0:HDR": {"enabled": True, "state": "trim", "rate": "50", "applied": True,
                                        "dip": {"record": "x", "rates": lines, "stale": False}}}}
    assert rv.trim_refusals(state, "0:HDR", ["59.94", "50", "47.952"], reference="60") == []
    at_5994 = {"state": "reference", "rate": None, "applied": True, "dip": {"rates": lines}}
    chk = rv.trim_leg_check(at_5994, "59.94", "60")
    assert chk["want_state"] == "reference" and chk["ok"] is True and chk["rule"] == "reference"
    # the chain is followed (a -> b -> reference) and a circle is caught (refused before anything changes)
    have = {"a": "b", "b": "60", "c": "d", "d": "c", "e": "e"}
    assert rv.resolve_rate("a", have, "60")[0] == "reference"
    assert rv.resolve_rate("c", have, "60")[0] is None and "circle" in rv.resolve_rate("c", have, "60")[1]
    assert rv.resolve_rate("e", have, "60")[0] == "e"
    circ = {"layers": {"0:HDR": {"refresh_trim": True}},
            "refresh_trim": {"0:HDR": {"enabled": True, "state": "reference", "applied": True,
                                       "dip": {"rates": ["50 Hz = 48", "48 Hz = 50"]}}}}
    assert any("circle" in r for r in rv.trim_refusals(circ, "0:HDR", ["50"], reference="60"))


def test_a_same_as_reference_rate_is_measured_and_judged_by_the_light(tmp_path):
    cal, api, _, ctrl = _calib(tmp_path, "s5994", spec={"rates": ["59.94", "50"]},
                               model_kw={"rates": ("47.952", "50"), "same_as": {"59.94": "60", "48": "47.952"}})
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "pass", res.digest
    out = json.loads((cal.ctx.root / rv.RESULT_FILE).read_text(encoding="utf-8"))
    r = out["rates"]["59.94"]
    assert r["verdict"] == "pass" and r["resolved"] == ["reference"] and r["trim_rules"] == ["reference"]
    assert all(abs(c["pct"]) < 0.3 for c in r["greys"].values())
    assert out["rates"]["50"]["trim_rules"] == ["stamp"]
    assert _at(api) == (60, 1)


def test_applied_without_the_trim_baked_fails_the_rate(tmp_path):
    """PERM_NOGRAY: the C++ reports state "trim", applied true, an empty active_stamp (refresh_trim.cpp:481-488)."""
    cal, api, _, ctrl = _calib(tmp_path, "nogray", model_kw={"no_gray": True})
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "fail"
    checks = [c for c in cal.calib["refresh_probe"]["trim_checks"] if c["rate"] != "60"]
    assert checks and all(c["applied"] is True and c["ok"] is False and c["rule"] == "stamp" for c in checks)
    assert all("does not carry it" in c["why"] for c in checks)
    assert _at(api) == (60, 1) and _layers(ctrl).get("bypass_3dlut") is False


def test_the_stamp_rules():
    lines = ["47.952 Hz trim (3 levels)"]
    base = {"state": "trim", "rate": "47.952", "applied": True, "dip": {"rates": lines}}
    ok = rv.trim_leg_check({**base, "stamp": "w", "active_stamp": "w"}, "47.952", "60")
    assert ok["ok"] and ok["rule"] == "stamp"
    assert not rv.trim_leg_check({**base, "stamp": "w", "active_stamp": ""}, "47.952", "60")["ok"]
    assert not rv.trim_leg_check({**base, "stamp": "w", "active_stamp": "other"}, "47.952", "60")["ok"]
    # a reply without the wanted stamp: a non-empty active stamp that differs from the reference's
    fb = rv.trim_leg_check({**base, "active_stamp": "x"}, "47.952", "60", ref_active_stamp="")
    assert fb["ok"] and fb["rule"] == "stamp_differs"
    assert not rv.trim_leg_check({**base, "active_stamp": "x"}, "47.952", "60", ref_active_stamp="x")["ok"]
    assert not rv.trim_leg_check({**base, "active_stamp": ""}, "47.952", "60", ref_active_stamp="")["ok"]


def test_a_stampless_reply_uses_the_fallback_rule_end_to_end(tmp_path):
    cal, _, _, _ = _calib(tmp_path, "stampless", model_kw={"stampless": True})
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "pass", res.digest
    out = json.loads((cal.ctx.root / rv.RESULT_FILE).read_text(encoding="utf-8"))
    assert out["rates"]["47.952"]["trim_rules"] == ["stamp_differs"]


def test_an_unbracketed_read_makes_the_rate_undecided_and_is_named():
    ok = [{"rate": r, "ok": True, "rule": "reference" if r == "60" else "stamp"} for r in ("60", "47.952", "60")]
    rows = [_row(0, "60", 0, 100.0, (0.3127, 0.3290)), _row(1, "47.952", 60, 100.1, (0.3127, 0.3290)),
            _row(2, "60", 120, 100.2, (0.3128, 0.3291))]
    assert rv.analyze_verify(rows, reference="60", trim_checks=ok)["verdict"] == "pass"
    late = _row(3, "47.952", 180, 100.1, (0.3127, 0.3290))          # after the last reference read: no bracket
    res = rv.analyze_verify(rows + [late], reference="60", trim_checks=ok)
    cell = res["rates"]["47.952"]["greys"]["g520"]
    assert res["verdict"] == "undecided" and cell["verdict"] == "undecided"
    assert cell["reads"] == 2 and cell["bracketed"] == 1
    assert cell["unbracketed"] == [{"leg": 3, "pass": "A", "t_s": 180, "why": "no reference read on both sides"}]
    assert any("without a bracketed ratio" in u for u in res["rates"]["47.952"]["undecided"])
    # a read with no light is named too, never dropped silently
    dark = {**_row(3, "47.952", 90, 100.1, (0.3127, 0.3290)), "y": None, "xyz": None}
    res2 = rv.analyze_verify(rows + [dark], reference="60", trim_checks=ok)
    assert res2["rates"]["47.952"]["greys"]["g520"]["unbracketed"][0]["why"] == "no reading"
    # a light miss still fails (the worse verdict wins)
    bad = rows[:1] + [_row(1, "47.952", 60, 100.6, (0.3127, 0.3290))] + rows[2:]
    assert rv.analyze_verify(bad + [late], reference="60", trim_checks=ok)["verdict"] == "fail"


def test_state_get_raising_inside_the_trim_check_ends_at_the_reference_with_the_bypass_restored(tmp_path, monkeypatch):
    cal, api, _, ctrl = _calib(tmp_path, "stateboom")
    real_check = rp._Probe.trim_check
    calls = {"n": 0}

    def booming_check(self, rate):
        if rate == "47.952":
            real_state = ctrl.state

            def boom():
                calls["n"] += 1
                raise ConnectionError("pipe gone mid read-back")
            ctrl.state = boom
            try:
                return real_check(self, rate)
            finally:
                ctrl.state = real_state
        return real_check(self, rate)
    monkeypatch.setattr(rp._Probe, "trim_check", booming_check)
    try:
        status = cal.run("refresh-response").status
    except Exception as exc:  # noqa: BLE001 - ending by raising is acceptable; the restore is what is tested
        status = f"raised {type(exc).__name__}"
    assert calls["n"] >= 1
    assert _at(api) == (60, 1) and api.legacy_registry(r"\\.\DISPLAY1")["hz"] == 60
    assert _layers(ctrl).get("bypass_3dlut") is False and _layers(ctrl).get("refresh_trim") is True
    if status == "completed":
        out = json.loads((cal.ctx.root / rv.RESULT_FILE).read_text(encoding="utf-8"))
        assert out["rates"]["47.952"]["verdict"] == "fail"
        assert any(c.get("error", "").startswith("ConnectionError") for c in cal.calib["refresh_probe"]["trim_checks"])
