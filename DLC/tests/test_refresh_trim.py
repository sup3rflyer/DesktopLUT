"""dlc.refresh_trim: bracketed ratios recover a planted effect under drift, the predictability verdicts tell a
frame-period law from a step, and the exported trim evaluates with the hold / extend / interpolation rules."""
from __future__ import annotations

import json
import math
import random

import pytest

from dlc import refresh_trim as rt

GREYS = [(120, 0.5), (193, 2.0), (307, 10.0), (451, 50.0), (520, 100.0), (594, 200.0), (693, 500.0), (770, 1000.0)]
H = {0.5: 0.0437, 2.0: 0.0418, 10.0: 0.0391, 50.0: 0.0332, 100.0: 0.0295, 200.0: 0.0243, 500.0: 0.0176, 1000.0: 0.0117}
HZ = {"60": 60.0, "47.952": 48000 / 1001, "48": 48.0, "50": 50.0, "59.94": 60000 / 1001}
PASS_A = ["60", "47.952", "60", "48", "60", "50", "60", "59.94", "60"]
PASS_B = ["59.94", "60", "50", "60", "48", "60", "47.952", "60"]


def truth_ln(L: float, hz: float, model: str) -> float:
    h = rt._interp_log(sorted(H.items()), L)
    t, t60, t47 = 1000 / hz, 1000 / 60, 1000 / (48000 / 1001)
    if model == "period":
        return h * (t - t60) / (t47 - t60)
    return h if hz < 59 else 0.0                                # step: full below 59 Hz, none above


def synth(model: str = "period", drift_per_h: float = 0.006, noise: float = 2e-4, seed: int = 3, colours=True):
    rng = random.Random(seed)
    rows, t, seq = [], 0.0, 0
    for li, rate in enumerate(PASS_A + PASS_B):
        t += 40.0
        pats = [(f"g{c}", "grey", L, (c, c, c)) for c, L in GREYS]
        if colours and li < len(PASS_A):
            pats += [("R520", "colour", 100.0, (520, 0, 0)), ("G520", "colour", 100.0, (0, 520, 0))]
        for name, kind, L, code in pats:
            t += 4.0
            drift = 1.0 + drift_per_h * t / 3600.0 + 0.001 * math.sin(t / 400.0)
            share = 1.0 if kind == "grey" else 0.3
            y = L * share * math.exp(truth_ln(L, HZ[rate], model)) * drift * (1 + rng.gauss(0, noise))
            xyz = [0.9505 * y, y, 1.089 * y]
            rows.append({"seq": seq, "leg": li, "pass": "A" if li < len(PASS_A) else "B", "rate": rate,
                         "rate_hz": HZ[rate], "patch": name, "kind": kind, "code": list(code),
                         "nominal_nits": L, "t_s": t, "y": y, "xyz": xyz, "sd_y": y * noise * 2, "n_kept": 4,
                         "settled": True})
            seq += 1
    return rows


def test_bracket_recovers_the_planted_ratios_under_drift():
    res = rt.analyze(synth())
    for rate in ("47.952", "48", "50", "59.94"):
        for c, L in GREYS:
            v = res["combined"][rate][f"g{c}"]
            assert abs(v["ln_ratio"] - truth_ln(L, HZ[rate], "period")) < 6e-4, (rate, c, v["ln_ratio"])
            assert v["n"] == 2 and v["u"] > 0


def test_unbracketed_rows_are_listed_not_used():
    rows = [r for r in synth() if not (r["leg"] == 16)]          # drop the last 60-Hz leg
    br = rt.bracket_ratios(rows)
    assert any(r.get("unbracketed") and r["leg"] == 15 for r in br)
    comb = rt.combine(br)
    assert comb["47.952"]["g520"]["n"] == 1                       # only pass A's 47.952 leg is bracketed


def test_period_law_is_recognised():
    pr = rt.analyze(synth("period"))["predictability"]
    lin = pr["linearity"]
    assert lin["compatible_period"] and abs(lin["p_50_pooled"] - lin["predicted_period"]) < 0.02
    assert pr["control_59_94"]["ok"] and pr["eq_48_47_952"]["ok"]
    assert lin["verdict"].startswith("linear")


def test_step_law_is_not_called_linear():
    pr = rt.analyze(synth("step"))["predictability"]
    lin = pr["linearity"]
    assert not lin["compatible_period"] and not lin["compatible_frequency"]
    assert lin["compatible_step_full"] and lin["verdict"] == "nonlinear"


def test_chromaticity_and_channel_checks_pass_on_a_neutral_effect():
    res = rt.analyze(synth())
    assert all(v["ok"] for v in res["chromaticity"].values())
    assert res["channels"]["47.952"]["ok"]


def _entry(nits, ln_gain):
    return {"knots": [{"y60_nits": y, "ln_gain": v} for y, v in zip(nits, ln_gain)]}


def test_gain_fades_to_one_above_the_top_level():
    """The C++ bake's twin (DesktopLUT tests/test_refresh_trim.cpp, the same cases and numbers)."""
    e = _entry([1.0, 10.0, 100.0], [-0.03, -0.02, -0.01])
    assert rt.gain_at(e, 100.0, 1000.0) == pytest.approx(math.exp(-0.01), rel=1e-12)
    # from (2, -0.01, slope 0.01 per decade) to (3, 0, flat): the Hermite midpoint -0.01/2 + 1 * 0.01/8
    assert rt.gain_at(e, math.sqrt(1e5), 1000.0) == pytest.approx(math.exp(-0.00375), rel=1e-12)
    assert rt.gain_at(e, 1000.0, 1000.0) == 1.0 and rt.gain_at(e, 5000.0, 1000.0) == 1.0
    assert rt.gain_at(e, math.sqrt(1e5)) == pytest.approx(math.exp(-0.005), rel=1e-12)     # no peak: the top slope
    assert rt.gain_at(e, 1e5) == pytest.approx(1.0, rel=1e-12)                              # never past 1
    assert rt.gain_at(e, 1e4, 50.0) == rt.gain_at(e, 50.0, 50.0)                            # a peak below the top: held
    away = _entry([10.0, 100.0], [-0.01, -0.02])                 # heading away from 1: not followed
    assert rt.gain_at(away, math.sqrt(1e5), 1000.0) == pytest.approx(math.exp(-0.01), rel=1e-12)
    assert rt.gain_at(away, 1e4) == pytest.approx(math.exp(-0.02), rel=1e-12)
    one = _entry([100.0], [math.log(0.97)])
    assert rt.gain_at(one, 100.0, 1000.0) == pytest.approx(0.97, rel=1e-12)
    assert rt.gain_at(one, math.sqrt(1e5), 1000.0) == pytest.approx(math.sqrt(0.97), rel=1e-12)
    bright = _entry([100.0, 1000.0], [0.02, 0.03])               # brightening, the peak just above: stretched
    end = 1000.0 * 10 ** (6.0 * 0.03 / math.log(10.0))
    assert rt.gain_at(bright, 1050.0, 1050.0) > 1.0 and rt.gain_at(bright, end * 1.001, 1050.0) == 1.0
    for ent, peak in ((e, 1000.0), (away, 1000.0), (one, 1000.0), (_entry([100.0, 1000.0], [-0.11, -0.01]), 1e4),
                      (bright, 1050.0), (bright, 5000.0)):
        top = ent["knots"][-1]["y60_nits"]
        v1 = ent["knots"][-1]["ln_gain"]
        prev, prev_y = v1, top
        for i in range(1, 2001):
            y = top * (peak * 1.5 / top) ** (i / 2000)
            v = math.log(rt.gain_at(ent, y, peak))
            assert abs(v) <= abs(prev) + 1e-15 and (v == 0.0 or (v > 0) == (v1 > 0))   # only toward 1
            assert (math.log(y) + v) - (math.log(prev_y) + prev) >= 0.5 * math.log(y / prev_y) - 1e-12   # light rises
            prev, prev_y = v, y


def test_trim_export_and_evaluation():
    res = rt.analyze(synth(), peak_nits=1800.0)
    trim = res["trim"]
    assert trim["schema"] == rt.SCHEMA and set(trim["rates"]) == {"47.952", "48", "50", "59.94"}
    e = trim["rates"]["47.952"]
    k = {kk["patch"]: kk for kk in e["knots"]}
    assert k["g520"]["gain"] < 1.0 and k["g520"]["dpq_code_10bit"] < 0
    # hold below the lowest knot, interpolate between, fade out above to gain 1, flat at the peak
    assert rt.gain_at(e, 0.05) == pytest.approx(k["g120"]["gain"], rel=1e-12)
    g_mid = rt.gain_at(e, 70.0)
    assert k["g520"]["gain"] <= g_mid <= k["g451"]["gain"] or k["g451"]["gain"] <= g_mid <= k["g520"]["gain"]
    assert k["g770"]["gain"] < rt.gain_at(e, 1500.0) <= 1.0
    assert k["g770"]["gain"] < rt.gain_at(e, 1500.0, 1800.0) < 1.0
    assert rt.gain_at(e, 1800.0, 1800.0) == 1.0 and rt.gain_at(e, 5000.0, 1800.0) == 1.0
    assert rt.trim_for_hz(trim, 60.0, 100.0) == 1.0
    assert rt.trim_for_hz(trim, 48000 / 1001, k["g520"]["y60_nits"]) == pytest.approx(k["g520"]["gain"], rel=1e-9)
    # an unmeasured rate inside the span: the period model; outside: None (no trim)
    g49 = rt.trim_for_hz(trim, 49.0, 100.0)
    assert g49 is not None and k["g520"]["gain"] < g49 < 1.0
    assert rt.trim_for_hz(trim, 23.976, 100.0) is None and rt.trim_for_hz(trim, 120.0, 100.0) is None


def test_nonlinear_verdict_disables_interpolation():
    trim = rt.analyze(synth("step"))["trim"]
    assert trim["model"]["usable_for_unmeasured_rates"] is False
    assert rt.trim_for_hz(trim, 49.0, 100.0) is None
    assert rt.trim_for_hz(trim, 50.0, 100.0) is not None          # measured rates keep their own knots


def test_chi2_sf_matches_known_values():
    assert rt.chi2_sf(3.84146, 1) == pytest.approx(0.05, abs=1e-4)
    assert rt.chi2_sf(11.0705, 5) == pytest.approx(0.05, abs=1e-4)
    assert rt.chi2_sf(0.0, 3) == 1.0


def test_birge_inflation_when_repeats_disagree():
    br = [{"rate": "48", "patch": "g520", "kind": "grey", "code": [520] * 3, "nominal_nits": 100.0, "y60": 100.0,
           "rate_hz": 48.0, "ln_ratio": v, "u": 0.001, "dx": 0.0, "dy": 0.0, "settled": True} for v in (0.030, 0.036)]
    c = rt.combine(br)["48"]["g520"]
    assert c["birge"] > 1 and c["u"] > c["u_propagated"]
    assert c["ln_ratio"] == pytest.approx(0.033)


# ----------------------------------------------------------------------------- the DIP section (what DesktopLUT reads)
META = {"display": {"name": "Test Panel", "hardware_id": "TST1234", "monitor": 0},
        "measured_through": {"mhc_profile": "DesktopLUT_Mon0_HDR_P4_1.icm", "active_perm": 4,
                             "layers": {"tonemap": False, "fald": False, "desktop_gamma": False, "bypass_3dlut": True}},
        "simulated": False}
TESTS = {"P2_48_eq_47_952": {"ok": True}, "P3_59_94_control": {"ok": True},
         "P5_luminance_only": {"per_rate": {"47.952": {"max_dxy_grey": 2e-4, "max_dxy_colour": 3e-4}}},
         "P6_channel_independent": {"per_rate": {"47.952": True, "50": True}}}


def _trim():
    return rt.analyze(synth(), peak_nits=1800.0, meta=META)["trim"]


def test_dip_response_carries_ratios_rationals_and_the_frozen_verdicts():
    trim = _trim()
    resp = rt.response_from_run(trim, TESTS, run_id="r1", measured="2026-10-23", identity={"hardware_id": "TST1234"},
                                conditions={"mode": "HDR"}, hz_measured={"47.952": 47.952048})
    by = {r.key: r for r in resp.rates}
    assert set(by) == {"47.952", "48", "50", "59.94"}
    assert (by["47.952"].num, by["47.952"].den) == (48000, 1001) and (by["59.94"].num, by["59.94"].den) == (60000, 1001)
    # ratios, not gains (the DIP holds the measured response; DesktopLUT derives g = 1 / ratio)
    k = {kk["patch"]: kk for kk in trim["rates"]["47.952"]["knots"]}
    lv = {round(x.nits, 6): x for x in by["47.952"].levels}
    assert lv[round(k["g520"]["y60_nits"], 6)].ratio == pytest.approx(k["g520"]["ratio"], rel=1e-12)
    assert [x.nits for x in by["47.952"].levels] == sorted(x.nits for x in by["47.952"].levels)
    assert by["48"].same_as == "47.952" and by["59.94"].same_as == "60" and by["50"].same_as is None
    assert by["47.952"].max_dxy == 3e-4 and by["47.952"].channel_independent is True
    assert by["47.952"].hz_measured == 47.952048 and by["50"].hz_measured is None
    assert resp.peak_nits == 1800.0 and resp.reference == {"key": "60", "num": 60, "den": 1}
    # a failed equality test: no same_as (each rate keeps its own levels)
    resp2 = rt.response_from_run(trim, {"P2_48_eq_47_952": {"ok": False}}, run_id="r1", measured="2026-10-23",
                                 identity={}, conditions={})
    assert all(r.same_as is None for r in resp2.rates)


def test_run_problems_refuse_a_response_not_measured_through_the_calibration_alone():
    trim = _trim()
    ok = {"meta": {"status": "ok", "anomalies": 0, "simulated": False}}
    assert rt.run_problems({"status": "ok", "anomalies": []}, trim, ok) == []
    for layer in ("desktop_gamma", "tonemap", "fald", "refresh_trim"):
        t = json.loads(json.dumps(trim))
        t["measured_through"]["layers"][layer] = True
        assert any(layer in p for p in rt.run_problems({}, t, ok)), layer
    t = json.loads(json.dumps(trim))
    t["simulated"] = True
    assert "simulated run" in rt.run_problems({}, t, ok)
    assert rt.run_problems({}, trim, {"meta": {"status": "aborted"}})
    t = json.loads(json.dumps(trim))
    t["display"].pop("hardware_id")
    assert any("hardware id" in p for p in rt.run_problems({}, t, ok))


def test_record_puts_the_section_on_the_existing_mode_record_only(tmp_path):
    from dlc.dip import DipStore, DisplayInstrumentProfile
    path = tmp_path / "dip_store.json"
    store = DipStore(path)
    store.record(DisplayInstrumentProfile(display="Test Panel", mode="HDR", made="2026-06-19", cold_channel="B"))
    store.record(DisplayInstrumentProfile(display="Test Panel", mode="SDR", made="2026-06-18"))
    resp = rt.response_from_run(_trim(), TESTS, run_id="r1", measured="2026-10-23",
                                identity={"hardware_id": "TST1234"}, conditions={"mode": "HDR"})
    rt.record_response(path, "Test Panel", "HDR", resp, today="2026-10-23")
    again = DipStore.load(path)
    hdr, sdr = again.get("Test Panel:HDR"), again.get("Test Panel:SDR")
    assert hdr.refresh_response is not None and hdr.refresh_response.run_id == "r1"
    assert hdr.cold_channel == "B" and hdr.made == "2026-06-19" and hdr.updated == "2026-10-23"   # priors kept
    assert sdr.refresh_response is None
    with pytest.raises(LookupError):
        rt.record_response(path, "Other Panel", "HDR", resp)


def test_record_cli_is_a_dry_run_unless_written(tmp_path):
    from dlc.dip import DipStore, DisplayInstrumentProfile
    run = tmp_path / "run"
    run.mkdir()
    (run / "trim_curves.json").write_text(json.dumps(_trim()), encoding="utf-8")
    (run / "refresh_result.json").write_text(json.dumps({"meta": {"run": "r9", "status": "ok", "anomalies": 0},
                                                         "tests": TESTS}), encoding="utf-8")
    (run / "evidence.json").write_text(json.dumps({"started": "2026-10-23T18:03:29", "status": "ok",
                                                   "audit_stack": {"monitor_identity": {"device_path": "p",
                                                                                        "edid_id": "TST1234-1"}}}),
                                       encoding="utf-8")
    dip = tmp_path / "dip_store.json"
    DipStore(dip).record(DisplayInstrumentProfile(display="Test Panel", mode="HDR", made="2026-06-19"))
    assert rt._main(["record", "--run", str(run), "--dip", str(dip)]) == 0
    assert DipStore.load(dip).get("Test Panel:HDR").refresh_response is None
    assert rt._main(["record", "--run", str(run), "--dip", str(dip), "--write"]) == 0
    rr = DipStore.load(dip).get("Test Panel:HDR").refresh_response
    assert rr.run_id == "r9" and rr.measured == "2026-10-23"
    assert rr.identity == {"hardware_id": "TST1234", "device_path": "p", "edid_id": "TST1234-1"}
