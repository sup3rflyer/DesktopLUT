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
           controller=None, api=None, panel_kw=None, cube=False, layers_on=None):
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
    if cube and controller is None:
        from dlc.simulation import write_identity_cube
        ctrl.set_3dlut(0, "HDR", str(write_identity_cube(tmp_path / f"{name}_daily.cube", size=17)))
    if layers_on and controller is None:
        ctrl.set_layers(0, "HDR", **{n: True for n in layers_on})

    def hz():
        return rm.active_hz(rm.find_display(api.snapshot(), "SIM0000"))

    # the panel's response at the rate, what the trim takes away when the model says a trimmed variant is on screen,
    # and the link rounding when the 3D LUT is bypassed (no DesktopLUT pass dithers then)
    gain = rv.sim_refresh_gain(server, residual=residual, effect=effect)
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


# reference x / y wobble leg to leg (drift + read noise) used by the pure rule tests: ~1e-4, a linear drift on top
_WOB = (0.0, 1.2e-4, -0.9e-4, 1.0e-4, -1.1e-4)


def _bracketed(rate_xy=((0.3127, 0.3290), (0.3127, 0.3290)), rate_y=(100.1, 100.1), *, drift=3e-4):
    """Five reference legs (0, 2, 4, 6, 8) and two rate legs (1 and 7, one per pass) of one grey."""
    rows = []
    for i, leg in enumerate((0, 2, 4, 6, 8)):
        w = _WOB[i]
        rows.append(_row(leg, "60", leg * 60, 100.0 + 0.05 * i, (0.3127 + drift * i / 4 + w, 0.3290 - w)))
    for (leg, xy, y) in zip((1, 7), rate_xy, rate_y):
        base = drift * (leg / 2) / 4                  # the linear drift at that time
        rows.append(_row(leg, "47.952", leg * 60, y, (xy[0] + base, xy[1])))
    return rows


def test_the_rule_luminance_and_chromaticity():
    ok = [{"rate": r, "ok": True, "rule": "stamp"} for r in ("60", "47.952")]
    rows = _bracketed()
    res = rv.analyze_verify(rows, reference="60", trim_checks=ok)
    assert res["verdict"] == "pass", res["rates"]["47.952"]
    cell = res["rates"]["47.952"]["greys"]["g520"]
    assert cell["chroma"]["nu"] == 3 and cell["chroma"]["ok"] is True
    assert cell["reference_repeatability"]["rms_x"] > 0
    # light 0.5 % over the bracket -> fail
    assert rv.analyze_verify(_bracketed(rate_y=(100.6, 100.6)), reference="60", trim_checks=ok)["verdict"] == "fail"
    # a chromaticity shift of 0.002 (>> the legs' ~1e-4 repeatability, and well inside the 0.0003 linear drift the old
    # max-min spread counted) -> fail, with the statistics in the reason
    shifted = _bracketed(rate_xy=((0.3147, 0.3290), (0.3147, 0.3290)))
    res_c = rv.analyze_verify(shifted, reference="60", trim_checks=ok)
    assert res_c["verdict"] == "fail" and "Student t" in res_c["rates"]["47.952"]["fail"][0]
    # a shift inside the repeatability (0.0001) passes
    small = _bracketed(rate_xy=((0.3128, 0.3290), (0.3128, 0.3290)))
    assert rv.analyze_verify(small, reference="60", trim_checks=ok)["verdict"] == "pass"
    # the old max-min rule would have failed a shift the size of the linear drift; this one does not
    drift_only = _bracketed(rate_xy=((0.31275, 0.3290), (0.31275, 0.3290)))
    assert rv.analyze_verify(drift_only, reference="60", trim_checks=ok)["verdict"] == "pass"
    # no trim read-back at the rate -> undecided; a failed one -> fail
    assert rv.analyze_verify(rows, reference="60", trim_checks=ok[:1])["verdict"] == "undecided"
    bad = [{"rate": "47.952", "ok": False, "state": "trim", "applied": False}]
    assert rv.analyze_verify(rows, reference="60", trim_checks=bad)["verdict"] == "fail"
    # two reference legs only: no repeatability estimate -> undecided, said so
    few = [r for r in rows if r["leg"] in (0, 1, 2)]
    res_f = rv.analyze_verify(few, reference="60", trim_checks=ok)
    assert res_f["verdict"] == "undecided" and "too few reference legs" in res_f["rates"]["47.952"]["undecided"][0]


def test_a_real_but_invisible_chromaticity_shift_is_a_practical_pass_said_beside_the_statistics():
    """Very repeatable reference legs (xy wobble 1e-5) make the statistical tolerance collapse: a 0.0006 shift in x
    at 100 nit is 'real' (p << the Bonferroni share) yet 0.39 dE_ITP - below one just noticeable difference
    (BT.2124). It passes on the practical floor, and the rate's notes say so with both numbers; 0.002 (1.3 dE_ITP)
    still fails."""
    ok = [{"rate": r, "ok": True, "rule": "stamp"} for r in ("60", "47.952")]

    def rows(dx):
        out = []
        for i, leg in enumerate((0, 2, 4, 6, 8)):
            w = (0.0, 1e-5, -1e-5, 1e-5, -1e-5)[i]
            out.append(_row(leg, "60", leg * 60, 100.0, (0.3127 + w, 0.3290 - w)))
        for leg in (1, 7):
            out.append(_row(leg, "47.952", leg * 60, 100.0, (0.3127 + dx, 0.3290)))
        return out

    res = rv.analyze_verify(rows(0.0006), reference="60", trim_checks=ok)
    cell = res["rates"]["47.952"]["greys"]["g520"]
    assert cell["chroma"]["ok"] is False                                      # statistically real ...
    assert cell["chroma"]["practical"]["below"] is True                       # ... below one JND
    assert cell["chroma"]["practical"]["de_itp"] == pytest.approx(0.39, abs=0.05)
    assert res["verdict"] == "pass" and cell["chroma_practical_pass"] is True
    note = res["rates"]["47.952"]["practical_notes"][0]
    assert "statistically real" in note and "BT.2124" in note
    big = rv.analyze_verify(rows(0.002), reference="60", trim_checks=ok)
    assert big["verdict"] == "fail" and big["rates"]["47.952"]["greys"]["g520"]["chroma"]["practical"]["below"] is False


def test_student_t_and_its_critical_values():
    for nu, t, p in ((1, 1.0, 0.5), (2, 4.303, 0.05), (3, 3.182, 0.05), (4, 2.776, 0.05), (10, 2.228, 0.05),
                     (3, 5.841, 0.01)):
        assert rv.t_two_sided_p(t, nu) == pytest.approx(p, abs=2e-4)
        assert rv.t_critical(p, nu) == pytest.approx(t, rel=2e-3)
    assert rv.t_two_sided_p(0.0, 3) == pytest.approx(1.0)


def test_link_code_size():
    # 10-bit PQ: ~2.3 % of light per code at 0.5 nit, ~0.9 % at 1000 nit (the live run's numbers)
    assert rv.link_code_pct(0.5, 10) == pytest.approx(2.3, abs=0.1)
    assert rv.link_code_pct(1000.0, 10) == pytest.approx(0.90, abs=0.03)
    assert rv.link_code_pct(100.0, 12) < rv.link_code_pct(100.0, 10) / 3.5
    assert rv.link_code_pct(None, 10) is None and rv.link_code_pct(100.0, None) is None


def test_mhc_only_names_a_residual_below_one_link_code():
    ok = [{"rate": r, "ok": True, "rule": "stamp"} for r in ("60", "47.952")]
    rows = _bracketed(rate_y=(100.6, 100.6))              # +0.5 %: outside 0.3 %, inside one 10-bit code (~0.98 %)
    res = rv.analyze_verify(rows, reference="60", trim_checks=ok, stack="mhc_only", link_bpc=10)
    cell = res["rates"]["47.952"]["greys"]["g520"]
    assert res["verdict"] == "undecided" and cell["residual_below_one_link_code"] is True
    assert "not resolvable without dither" in res["rates"]["47.952"]["undecided"][0]
    big = _bracketed(rate_y=(101.5, 101.5))                # +1.5 %: more than one code -> fail even without dither
    assert rv.analyze_verify(big, reference="60", trim_checks=ok, stack="mhc_only", link_bpc=10)["verdict"] == "fail"
    # as viewed the same +0.5 % is a plain fail (0.3 % stays), the code size still reported
    res_v = rv.analyze_verify(rows, reference="60", trim_checks=ok, stack="as_viewed", link_bpc=10)
    assert res_v["verdict"] == "fail" and res_v["rates"]["47.952"]["greys"]["g520"]["link_code_pct"] > 0.9


def test_spec_problems_and_plan_cli(tmp_path):
    assert rp.RefreshProbeSpec.from_dict({"purpose": "verify", "source": "vrr"}).problems()
    assert rp.RefreshProbeSpec.from_dict({"purpose": "nonsense"}).problems()
    assert rp.RefreshProbeSpec.from_dict({"purpose": "verify", "verify_stack": "x"}).problems()
    assert rp.RefreshProbeSpec.from_dict({"purpose": "verify", "verify_layers_off": ["nope"]}).problems()
    assert rp.RefreshProbeSpec.from_dict({"purpose": "verify", "verify_stack": "mhc_only",
                                          "verify_layers_off": ["fald"]}).problems()
    mhc = rp.RefreshProbeSpec(purpose="verify", verify_stack="mhc_only")
    assert rp.expected_stack(mhc) == {"bypass_3dlut": True, "refresh_trim": True}
    assert rp.probe_stack(mhc) == {"bypass_3dlut": True}
    viewed = rp.RefreshProbeSpec(purpose="verify")
    assert viewed.as_viewed and rp.probe_stack(viewed) == {}
    assert rp.expected_stack(viewed)["refresh_trim"] is True
    assert "refresh_trim" not in rp.probe_stack(mhc)
    out2 = tmp_path / "m.json"
    assert rp.main(["plan", "--purpose", "verify", "--verify-stack", "mhc_only", "--rates", "47.952,50",
                    "--out", str(out2)]) == 0
    assert json.loads(out2.read_text(encoding="utf-8"))["verify_stack"] == "mhc_only"
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
    ok = [{"rate": r, "ok": True, "rule": "reference" if r == "60" else "stamp"} for r in ("60", "47.952")]
    rows = _bracketed()
    assert rv.analyze_verify(rows, reference="60", trim_checks=ok)["verdict"] == "pass"
    late = _row(9, "47.952", 540, 100.1, (0.3127, 0.3290))          # after the last reference read: no bracket
    res = rv.analyze_verify(rows + [late], reference="60", trim_checks=ok)
    cell = res["rates"]["47.952"]["greys"]["g520"]
    assert res["verdict"] == "undecided" and cell["verdict"] == "undecided"
    assert cell["reads"] == 3 and cell["bracketed"] == 2
    assert cell["unbracketed"] == [{"leg": 9, "pass": "A", "t_s": 540, "why": "no reference read on both sides"}]
    assert any("without a bracketed ratio" in u for u in res["rates"]["47.952"]["undecided"])
    # a read with no light is named too, never dropped silently
    dark = {**_row(3, "47.952", 180, 100.1, (0.3127, 0.3290)), "y": None, "xyz": None}
    res2 = rv.analyze_verify(rows + [dark], reference="60", trim_checks=ok)
    assert res2["rates"]["47.952"]["greys"]["g520"]["unbracketed"][0]["why"] == "no reading"
    # a light miss still fails (the worse verdict wins)
    bad = _bracketed(rate_y=(100.6, 100.6))
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


# ----------------------------------------------------------------------------- 2026-11-14: as viewed / mhc_only
def _set_calls(ctrl) -> list:
    return [dict(r.params or {}) for r in ctrl.client.transport.requests if r.method == "layers.set"]


def test_as_viewed_measures_the_owners_stack_untouched_and_passes_a_good_trim(tmp_path):
    adj = MappingAdjudicator({})
    cal0, _, _, _ = _calib(tmp_path, "viewed_seam", adjudicator=adj, cube=True, layers_on=("fald", "tonemap"))
    with pytest.raises(AdjudicationRequired) as ei:
        cal0.run("refresh-response")
    q, d = ei.value.request.question, ei.value.request.digest
    assert "AS VIEWED" in q and "IN the path" in q and "fald" in q and "tonemap" in q and "verify_layers_off" in q
    assert d["verify_stack"] == "as_viewed" and set(d["per_layer"]) == {"tonemap", "fald"}
    assert d["link"]["link_bpc"] == 10
    cal, api, _, ctrl = _calib(tmp_path, "viewed", cube=True, layers_on=("fald", "tonemap"))
    before = len(_set_calls(ctrl))
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "pass", res.digest
    assert res.digest["verify_stack"] == "as_viewed"
    assert _set_calls(ctrl)[before:] == []                      # the run sent NO layers.set at all
    lay = _layers(ctrl)
    assert lay["fald"] is True and lay["tonemap"] is True and lay["bypass_3dlut"] is False
    ms = cal.calib["refresh_probe"]["measured_stack"]
    assert ms["verify_stack"] == "as_viewed" and ms["layers"]["fald"] is True and ms["cube_path"]
    assert sorted((ms["viewing_layers"] or {}).get("kept") or {}) == sorted(rp.VIEWING_LAYERS)
    out = json.loads((cal.ctx.root / rv.RESULT_FILE).read_text(encoding="utf-8"))
    assert out["stack"] == "as_viewed" and out["link_bpc"] == 10
    assert all(c["link_code_pct"] > 0.8 for r in out["rates"].values() for c in r["greys"].values())
    assert _at(api) == (60, 1)


def test_as_viewed_can_take_a_layer_out_and_puts_it_back(tmp_path):
    cal, api, _, ctrl = _calib(tmp_path, "viewed_off", cube=True, layers_on=("fald", "tonemap"),
                               spec={"verify_layers_off": ["fald"]})
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    assert cal.calib["refresh_probe"]["measured_stack"]["layers"]["fald"] is False
    assert cal.calib["refresh_probe"]["measured_stack"]["layers"]["tonemap"] is True
    assert _layers(ctrl)["fald"] is True                       # restored at the end


def test_mhc_only_rounds_to_the_link_and_says_not_resolvable(tmp_path):
    """The sim rounds the MHC output to the 10-bit link when the 3D LUT is bypassed (no dither): a perfect trim then
    misses 0.3 % at some greys by less than one link code -> undecided, worded; as viewed the same trim passes."""
    cal, api, _, ctrl = _calib(tmp_path, "mhc", cube=True, layers_on=("fald",), spec={"verify_stack": "mhc_only"})
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    assert res.digest["verdict"] == "undecided", res.digest
    reasons = [u for r in res.digest["rates"] for u in r["undecided"]]
    assert any("not resolvable without dither" in u for u in reasons)
    assert all("not resolvable" in u or "too few" in u for u in reasons), reasons
    out = json.loads((cal.ctx.root / rv.RESULT_FILE).read_text(encoding="utf-8"))
    below = [c for r in out["rates"].values() for c in r["greys"].values() if c.get("residual_below_one_link_code")]
    assert below and all(abs(c["pct"]) < c["link_code_pct"] for c in below)
    ms = cal.calib["refresh_probe"]["measured_stack"]
    assert ms["layers"]["bypass_3dlut"] is True and ms["layers"]["fald"] is False
    assert _layers(ctrl)["bypass_3dlut"] is False and _layers(ctrl)["fald"] is True and _at(api) == (60, 1)


def test_mhc_only_still_fails_a_trim_that_misses_by_more_than_a_code(tmp_path):
    cal, _, _, _ = _calib(tmp_path, "mhc_bad", cube=True, residual=1.0, spec={"verify_stack": "mhc_only"})
    res = cal.run("refresh-response")
    assert res.status == "completed" and res.digest["verdict"] == "fail", res.digest
