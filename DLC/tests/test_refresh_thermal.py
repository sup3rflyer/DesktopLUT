"""REFRESH TRIM verify, the light judgement (2026-11-14 redesign): light classes by what a viewer can see, the panel's
thermal state against the record's, the run-level summary, and the optional closed-loop preheat.

Replays today's real data (``_refresh_fixture``: the PA32UCXR's two morning verify runs, the 2026-10-23 record, the
afternoon warm-up cycles and the one-code ladder): a cool panel's leftover is a positive below-visibility one and the
panel reads "cooler than the record"; a hot panel's light passes; a +3 % leftover (no trim) fails wherever it is
bracketed. Nothing here opens the pipe, a meter or a display: rows in, verdicts out, and the simulator for the flow."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import _refresh_fixture as rf
from dlc import refresh_probe as rp
from dlc import refresh_trim as rt
from dlc import refresh_verify as rv
from dlc.colormath import xy_to_XYZ
from dlc.dip import DipStore, RefreshResponse

LEVELS_COMPARED = {"g307", "g450", "g520", "g592", "g692", "g769"}       # the as-viewed greys with the record's light


@pytest.fixture(scope="module")
def fx() -> dict:
    return rf.load()


@pytest.fixture(scope="module")
def record(fx) -> tuple[dict, str]:
    return rf.record_th(fx)


def _verify(fx, record, run: str, *, rows=None, knowledge=rf.KNOWLEDGE_F0006, anomalies=True) -> dict:
    r = fx["runs"][run]
    th, src = record
    return rv.analyze_verify(rows if rows is not None else r["rows"], reference="60", trim_checks=r["trim_checks"],
                             anomalies=r["anomalies"] if anomalies else (), link_bpc=r["link_bpc"], record_th=th,
                             record_source=src, knowledge=knowledge)


# ----------------------------------------------------------------------------- visibility (pinned)
@pytest.mark.parametrize("nits,pct,dx,dy,ref", [
    (0.5, 3.0, 0.0, 0.0, 0.9151), (2.0, 3.0, 0.0, 0.0, 1.2691), (10.0, 0.5, 0.0, 0.0, 0.2832),
    (100.0, 0.3, 0.0, 0.0, 0.2165), (100.0, 0.9, 0.0, 0.0, 0.6476), (100.0, 3.0, 0.0, 0.0, 2.1381),
    (1000.0, 3.0, 0.0, 0.0, 2.3202), (100.0, 0.5, 0.0003, 0.0004, 0.3994), (5.0, 0.8, 0.0, 0.0, 0.4058)])
def test_the_change_in_dE_ITP_matches_the_colour_package(nits, pct, dx, dy, ref):
    """dE_ITP of a grey's change (light and colour together) at an absolute light; reference values from the colour
    package (ITU-R BT.2100-2 PQ ICtCp, BT.2124 delta_E_ITP), computed offline."""
    w = (0.3127, 0.3290)
    a = xy_to_XYZ(w[0], w[1], nits)
    b = xy_to_XYZ(w[0] + dx, w[1] + dy, nits * (1 + pct / 100))
    assert rv.de_itp(b, a) == pytest.approx(ref, abs=2e-4)
    assert rv.change_itp(w, nits, 1 + pct / 100, dx, dy)["de_itp"] == pytest.approx(ref, abs=2e-4)


# ----------------------------------------------------------------------------- the real runs
def test_the_warming_morning_run_is_a_below_visibility_leftover_on_a_cooler_panel(fx, record):
    """08:07 (the panel warming under the run): +0.1..+0.75 % left over at 47.952 / 50 Hz, under one JND everywhere;
    the reference greys read +0.0022 in x over the record's at the six comparable levels: cooler than the record,
    and the display's knowledge (F-0006) expects a positive leftover of up to about 0.9 %."""
    res = _verify(fx, record, "0807")
    th = res["thermal"]
    assert th["state"] == "cooler than the record" and th["n_levels"] == 6
    assert {lv["patch"] for lv in th["levels"] if lv["comparable"]} == LEVELS_COMPARED
    assert th["median_dx"] == pytest.approx(0.0022, abs=0.0002) and th["distance_xy"] > rv.THERMAL_TOL_XY
    for rate in ("47.952", "50"):
        cells = res["rates"][rate]["greys"]
        lights = {p: c for p, c in cells.items() if c.get("pct") is not None}
        assert all(c["verdict"] in ("pass", "pass_below_visibility") for c in lights.values())
        assert all(c["visibility_itp"] < 1.0 for c in lights.values())
        assert any(not c["lum_ok"] and c["verdict"] == "pass_below_visibility" for c in lights.values())
        # g120 renders at 0.19 nit as viewed (target 0.5): every read implausible -> named undecided, never dropped
        assert cells["g120"]["verdict"] == "undecided"
    assert res["per_rate"]["47.952"]["mean_pct"] == pytest.approx(0.37, abs=0.03)
    assert res["per_rate"]["47.952"]["largest_itp"] < 0.5
    s = res["summary"]
    assert "panel cooler than when the response was recorded" in s
    assert "a positive leftover of up to about 0.9 % is expected" in s and "F-0006" in s
    assert "drift inside the run" in s and "below visibility" in s
    # the run itself stays undecided: its nine implausible g120 reads are anomalies to judge
    assert res["verdict"] == "undecided" and res["recommendation"]["visible"] == []


def test_the_hot_morning_run_passes_on_light_as_warm_as_the_record(fx, record):
    """08:46 (hot): every grey's light within the 0.3 % gate; the panel reads as the record (within 0.0001 in xy).
    The few below-visibility cells are the chromaticity test's (statistically real, invisible shifts), not light."""
    res = _verify(fx, record, "0846")
    assert res["thermal"]["state"] == "as the record" and res["thermal"]["distance_xy"] < 0.0005
    for rate in ("47.952", "50"):
        for p, c in res["rates"][rate]["greys"].items():
            if c.get("pct") is None:
                continue
            assert c["lum_ok"] is True, (rate, p, c["pct"])
            assert c["verdict"] == "pass" or (c["verdict"] == "pass_below_visibility" and c["chroma_ok"] is False)
    assert "panel as warm as when the response was recorded" in res["summary"]
    assert "a positive leftover" not in res["summary"]


def test_a_leftover_of_three_percent_fails_at_every_bracketed_level(fx, record):
    """The trim not applied (synthetic: every rate read set to +3 % of its bracketed reference, the colour as read):
    1.2-2.3 JND at 1.5-1000 nit = fail. (At 0.5 nit 3 % would be 0.92 JND - below visibility; that grey has no
    bracketed read as viewed on this display.)"""
    res = _verify(fx, record, "0846", rows=rf.with_leftover(fx["runs"]["0846"]["rows"], 0.03))
    assert res["verdict"] == "fail"
    for rate in ("47.952", "50"):
        cells = {p: c for p, c in res["rates"][rate]["greys"].items() if c.get("pct") is not None}
        assert len(cells) == 7 and all(c["verdict"] == "fail" and c["visibility_itp"] >= 1.0 for c in cells.values())
    assert len(res["recommendation"]["visible"]) == 14 and "Verdict: fail (visible:" in res["summary"]


def test_the_ladder_trim_off_fails_and_trim_on_stays_below_visibility(fx, record):
    """Codes 515-525 on a cool panel: with the trim OFF (= not applied) the real +2.9..+3.1 % fails at every code
    (2.1-2.2 JND); with it ON the leftover scatters -0.01..+0.81 % code to code, all under one JND (the
    chromaticity test needs more than two reference legs, so those cells stay undecided on colour, said)."""
    th, src = record
    off = rv.analyze_verify(rf.ladder_rows(fx, "c48off"), reference="60", trim_checks=rf.OK_CHECKS, record_th=th,
                            record_source=src, knowledge=rf.KNOWLEDGE_F0006)
    cells = off["rates"]["47.952"]["greys"]
    assert len(cells) == 11 and all(c["verdict"] == "fail" and c["visibility_itp"] > 2.0 for c in cells.values())
    on = rv.analyze_verify(rf.ladder_rows(fx, "b48on"), reference="60", trim_checks=rf.OK_CHECKS, record_th=th,
                           record_source=src, knowledge=rf.KNOWLEDGE_F0006)
    cells = on["rates"]["47.952"]["greys"]
    pcts = [c["pct"] for c in cells.values()]
    assert min(pcts) == pytest.approx(-0.01, abs=0.03) and max(pcts) == pytest.approx(0.81, abs=0.03)
    assert all(c["visibility_itp"] < 1.0 for c in cells.values())
    assert all(c["verdict"] == "undecided" and c["chroma_ok"] is None for c in cells.values())
    assert on["thermal"]["state"] == off["thermal"]["state"] == "cooler than the record"


@pytest.mark.parametrize("k,state", [(0, "hotter than the record"), (3, "as the record"), (6, "as the record")])
def test_the_warm_up_cycles_thermometer(fx, record, k, state):
    """Straight after 12 min of a 500-nit full field the 100-nit grey reads x 0.3067 (-0.0026 against the record):
    hotter. Cooling, it passes the record's state (cycle 3) and keeps going (cycle 6: +0.0016, inside the session
    tolerance). Only g520 is comparable (5 / 29 nit match no record level)."""
    th, src = record
    own = rt.reference_thermometer(rf.warm_cycle_rows(fx, k, context=False), "60")
    t = rv.thermal_state(own, th, knowledge=rf.KNOWLEDGE_F0006, record_source=src)
    assert t["state"] == state and t["n_levels"] == 1
    assert [lv["patch"] for lv in t["levels"] if lv["comparable"]] == ["g520"]


@pytest.mark.parametrize("k,light_class", [(0, "pass"), (6, "pass_below_visibility")])
def test_the_warm_up_cycles_light(fx, k, light_class):
    """Hot (cycle 0): g520 -0.12 %, inside the gate; 23 min later +0.31 %: just outside, 0.23 JND."""
    res = rv.analyze_verify(rf.warm_cycle_rows(fx, k), reference="60", trim_checks=rf.OK_CHECKS)
    c = res["rates"]["47.952"]["greys"]["g520"]
    assert c["verdict"] == light_class and c["visibility_itp"] < 0.3


def test_without_knowledge_a_clear_difference_is_unknown_with_the_numbers(fx, record):
    res = _verify(fx, record, "0807", knowledge=None)
    assert res["thermal"]["state"] == "unknown" and "does not say which way is warmer" in res["thermal"]["why"]
    assert res["thermal"]["median_dx"] == pytest.approx(0.0022, abs=0.0002)
    assert "expected" not in res["summary"] and "thermal state unknown" in res["summary"]


def test_no_record_thermometer_is_unknown_never_guessed(fx):
    r = fx["runs"]["0807"]
    res = rv.analyze_verify(r["rows"], reference="60", trim_checks=r["trim_checks"], record_th=None,
                            record_source="the record stores no thermometer and its run folder was not found",
                            knowledge=rf.KNOWLEDGE_F0006)
    assert res["thermal"]["state"] == "unknown" and "no record thermometer" in res["thermal"]["why"]
    assert res["thermal"]["verify"]["levels"] == 7          # g120: every read implausible; the verify's own thermometer is still reported


# ----------------------------------------------------------------------------- the record and its new field
def test_an_old_record_without_the_thermometer_still_loads_and_is_written_as_before(fx, tmp_path):
    d = dict(fx["record"]["response"])
    assert "thermometer" not in d
    resp = RefreshResponse.from_dict(d)
    assert resp.thermometer is None and "thermometer" not in resp.as_dict()
    # a verify then derives it from the record's run folder when it still exists, else says it has none
    th, why = rv.record_thermometer(resp, [tmp_path])
    assert th is None and "stores no thermometer" in why
    run = tmp_path / "probes" / resp.run_id
    run.mkdir(parents=True)
    (run / "trim_rows.jsonl").write_text("\n".join(json.dumps(r) for r in fx["record"]["rows"]), encoding="utf-8")
    th, why = rv.record_thermometer(resp, [tmp_path])
    assert th is not None and why.startswith("derived from") and len(th["levels"]) == 8
    # and once stored, the stored one is used (round trip through the DIP's own dict)
    resp.thermometer = th
    back = RefreshResponse.from_dict(json.loads(json.dumps(resp.as_dict())))
    th2, why2 = rv.record_thermometer(back, [])
    assert why2 == "stored with the record" and th2["levels"][0]["patch"] == th["levels"][0]["patch"]


def test_the_record_flow_stores_its_thermometer(tmp_path):
    """A refresh-response run (trim OFF, the record flow) puts its reference greys' thermometer into the DIP section
    it records; the rest of the section is unchanged."""
    from test_refresh_probe import _calib as probe_calib
    cal, _, _ = probe_calib(tmp_path, "rec")
    assert cal.run("refresh-response").status == "completed"
    resp, problems, trim = rt.response_for_run_dir(cal.ctx.root)
    assert problems == ["simulated run"]
    th = resp.thermometer
    assert th and [lv["patch"] for lv in th["levels"]] == ["g120", "g307", "g520"]
    assert all(lv["n"] >= 4 and abs(lv["x"] - 0.3127) < 0.01 for lv in th["levels"])
    rt.record_response(cal._dip_store().path, trim["display"]["name"], "HDR", resp)
    rec = DipStore.load(cal._dip_store().path).get(f"{cal.display.name}:HDR")
    assert rec.refresh_response.thermometer["levels"][1]["patch"] == "g307"


def test_a_verify_reads_the_records_thermometer_end_to_end(tmp_path):
    """Record (sim), then verify against it: the simulated panel is in one thermal state, so 'as the record', and
    the digest carries the summary line, the thermal state and the recommendation."""
    from test_refresh_probe import _calib as probe_calib
    from test_refresh_verify import _calib as verify_calib
    cal, _, _ = probe_calib(tmp_path, "rec")
    cal.run("refresh-response")
    resp, _, trim = rt.response_for_run_dir(cal.ctx.root)
    rt.record_response(cal._dip_store().path, trim["display"]["name"], "HDR", resp)
    vcal, _, _, _ = verify_calib(tmp_path, "ver")
    assert vcal._dip_store().path == cal._dip_store().path
    res = vcal.run("refresh-response")
    d = res.digest
    assert res.status == "completed" and d["verdict"] == "pass", d
    assert d["thermal_state"] == "as the record" and d["thermal"]["record_source"] == "stored with the record"
    assert d["thermal"]["stacks"]["record"].startswith("the calibration alone")
    assert "47.952 Hz: leftover mean" in d["summary"] and "Verdict: pass" in d["summary"]
    assert d["recommendation"]["verdict"] == "pass" and d["preheat"] is None


def test_the_knowledge_hook_reads_the_thermal_offset_and_writes_nothing(tmp_path):
    from dlc import knowledge as kn
    ev = tmp_path / "ev.txt"
    ev.write_text("warm.log\n", encoding="utf-8")
    store = kn.KnowledgeStore(tmp_path / "dip_knowledge")
    hdr = kn.unit_from_header({"identity": {"hardware_id": "TST1234", "edid_id": "TST1234-12345"},
                               "aliases": ["Test Panel 32"], "model": "Test Panel 32", "panel": {"tech": "mini-LED"}})
    entry = {"kind": "panel", "title": "The refresh response is larger on a cool panel",
             "statement": "Leftover about 0 hot and +0.5..+0.9 % cool; the grey's x is the thermometer (hot = lower x).",
             "scope": {"mode": ["HDR"], "unknown": ["SDR"]}, "status": "measured",
             "observations": [{"date": "2026-11-14", "verdict": "establishes", "independent": True,
                               "source": {"kind": "meter", "path": str(ev)},
                               "numbers": {"x_100nit_cool": 0.3127, "x_100nit_hot": 0.3067, "leftover_pct": 0.55}}],
             "signature": {"type": "thermal_offset", "sign": {"rate_ratio_when_cooler": "+"},
                           "max_abs": {"rate_ratio_pct": 0.9, "dx_thermometer": 0.006}, "tau_min": [15, 30]}}
    store.apply_ops(hdr["unit_id"], [{"op": "create_entry", "by": "llm:test", "why": "t", "entry": entry}],
                    create=hdr, today="2026-11-14")
    before = {p.name: p.stat().st_mtime_ns for p in store.root.iterdir()}
    k = rv.thermal_knowledge(store.root, [None, "Test Panel 32"], mode="HDR", today="2026-11-14")
    assert {p.name: p.stat().st_mtime_ns for p in store.root.iterdir()} == before
    assert k["axis"] == "x" and k["cooler_sign"] == 1 and k["leftover_sign_when_cooler"] == "+"
    assert k["leftover_max_pct"] == 0.9 and "x_100nit_cool" in k["cooler_from"]
    assert rv.thermal_knowledge(store.root, ["SDR panel nobody knows"]) is None
    assert rv.thermal_knowledge(store.root, ["Test Panel 32"], mode="SDR") is None
    assert rv.thermal_knowledge(tmp_path / "nowhere", ["Test Panel 32"]) is None


def test_nothing_changes_for_a_run_with_the_trim_off(tmp_path):
    """The record flow (trim OFF) judges nothing and gains no verify fields (its digest and files as before, the
    thermometer only inside the DIP section it records); a verify with the trim off is still refused before anything
    changes."""
    from test_refresh_probe import _calib as probe_calib
    from test_refresh_verify import _calib as verify_calib
    cal, _, _ = probe_calib(tmp_path, "off")
    res = cal.run("refresh-response")
    assert res.status == "completed"
    assert not {"thermal_state", "thermal", "summary", "per_rate", "preheat"} & set(res.digest)
    assert not (cal.ctx.root / rv.RESULT_FILE).exists()
    assert "thermal" not in json.loads((cal.ctx.root / "refresh_result.json").read_text(encoding="utf-8"))
    vcal, _, panel, _ = verify_calib(tmp_path, "voff", trim_on=False)
    vres = vcal.run("refresh-response")
    assert vres.status == "aborted" and "REFRESH TRIM is off" in vres.digest["message"] and panel.reads == 0


# ----------------------------------------------------------------------------- the preheat option
def test_preheat_is_offered_not_forced(tmp_path):
    from test_refresh_verify import _calib as verify_calib
    assert rp.RefreshProbeSpec().preheat == "none"
    assert rp.RefreshProbeSpec.from_dict({"preheat": "hot"}).problems()
    assert "Preheat: none (default)" in rp.preheat_words(rp.RefreshProbeSpec())
    assert "closed_loop" in rp.preheat_words(rp.RefreshProbeSpec(preheat="closed_loop"))
    cal, _, _, _ = verify_calib(tmp_path, "pre", spec={"preheat": "closed_loop"})
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    pre = res.digest["preheat"]
    assert pre["policy"] == "closed_loop" and pre["blocks"] >= 1 and pre["content_reads"] >= 1
    plan = next(json.loads(x) for x in (cal.ctx.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if '"refresh-response:plan"' in x)
    assert "Preheat: closed_loop" in json.dumps(plan)
