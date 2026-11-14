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


# ----------------------------------------------------------------------------- visibility (pinned in test_visibility)
@pytest.mark.parametrize("nits,pct,dx,dy", [(0.5, 3.0, 0.0, 0.0), (100.0, 0.9, 0.0, 0.0), (100.0, 0.5, 0.0003, 0.0004)])
def test_the_change_uses_dlc_visibility(nits, pct, dx, dy):
    """The verify's change of a grey is dlc.visibility.de_itp of the reference grey and the grey at the rate (the
    values are pinned against the colour package in tests/test_visibility.py)."""
    from dlc.visibility import de_itp
    w = (0.3127, 0.3290)
    a = xy_to_XYZ(w[0], w[1], nits)
    b = xy_to_XYZ(w[0] + dx, w[1] + dy, nits * (1 + pct / 100))
    assert rv.change_itp(w, nits, 1 + pct / 100, dx, dy)["de_itp"] == de_itp(b, a) == rv.de_itp(b, a)


# ----------------------------------------------------------------------------- the real runs
def test_the_warming_morning_run_is_a_below_visibility_leftover_on_a_cooler_panel(fx, record):
    """08:07 (the panel warming under the run): +0.1..+0.75 % left over at 47.952 / 50 Hz, under one JND everywhere.
    The reference greys read +0.0022 in x over the record's at the six comparable levels: beyond the 0.002 tolerance,
    in the cooler direction (F-0006), but inside the rig's 0.0025 session-to-session offset (R-0001) - a re-placed
    meter does that by itself, so 'cooler' is NOT established and no leftover expectation is stated."""
    res = _verify(fx, record, "0807")
    th = res["thermal"]
    assert th["state"] == "cooler than the record" + rv.NOT_ESTABLISHED and th["n_levels"] == 6
    assert th["established"] is False and th["session_offset_dxy"] == 0.0025 and "R-0001" in th["session_offset_from"]
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
    assert "panel possibly cooler than when the response was recorded" in s and "not established" in s
    assert "is expected" not in s                          # the expectation only for an established state
    assert "drift inside the run" in s and "below visibility" in s
    # established once beyond the session offset: the same run against a 0.002 offset (no session entry)
    kn = {k: v for k, v in rf.KNOWLEDGE_F0006.items() if not k.startswith("session_offset")}
    res2 = _verify(fx, record, "0807", knowledge=kn)
    assert res2["thermal"]["state"] == "cooler than the record" and res2["thermal"]["established"] is True
    assert "a positive leftover of up to about 0.9 % is expected" in res2["summary"] and "F-0006" in res2["summary"]
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


def _knowledge_store(tmp_path: Path, *, structured_sign: bool = False, with_session: bool = True):
    """A knowledge unit shaped like the PA32UCXR's: a refresh-response entry, a thermal_offset entry WITHOUT a
    direction listed first (like F-0005), the refresh-linked thermal_offset with one (like F-0006), and the rig's
    session_offset (like R-0001)."""
    from dlc import knowledge as kn
    ev = tmp_path / "ev.txt"
    ev.write_text("warm.log\n", encoding="utf-8")
    store = kn.KnowledgeStore(tmp_path / "dip_knowledge")
    hdr = kn.unit_from_header({"identity": {"hardware_id": "TST1234", "edid_id": "TST1234-12345"},
                               "aliases": ["Test Panel 32"], "model": "Test Panel 32", "panel": {"tech": "mini-LED"}})
    obs = lambda nums: [{"date": "2026-11-14", "verdict": "establishes", "independent": True,  # noqa: E731
                         "source": {"kind": "meter", "path": str(ev)}, "numbers": nums}]
    refresh = {"kind": "panel", "id": "F-0001", "title": "The panel is brighter at lower refresh rates",
               "statement": "About +1 to +3.4 % at 47.952 Hz against 60 Hz.", "status": "measured",
               "scope": {"mode": ["HDR"], "unknown": ["SDR"]}, "observations": obs({"ratio_pct_520": 2.64}),
               "signature": {"type": "none"},
               "links": [{"rel": "see_field", "field": "Test Panel 32:HDR.refresh_response"}]}
    load = {"kind": "panel", "id": "F-0002", "title": "Warm-in under load", "status": "measured",
            "statement": "The white drifts with the backlight load.", "scope": {"mode": ["HDR"], "unknown": ["SDR"]},
            "observations": obs({"tau_min": 20}),
            "signature": {"type": "thermal_offset", "sign": {"light_when_cooler": "-"}, "max_abs": {"light_pct": 1.0}}}
    sign = {"rate_ratio_when_cooler": "+", **({"dx_thermometer_when_cooler": "+"} if structured_sign else {})}
    thermal = {"kind": "panel", "id": "F-0003", "title": "The refresh response is larger on a cool panel",
               "statement": "Leftover about 0 hot, +0.5..+0.9 % cool; the grey's x is the thermometer (hot = lower x).",
               "scope": {"mode": ["HDR"], "unknown": ["SDR"]}, "status": "measured",
               "observations": obs({"x_100nit_cool": 0.3127, "x_100nit_hot": 0.3067, "leftover_pct": 0.55}),
               "signature": {"type": "thermal_offset", "sign": sign,
                             "max_abs": {"rate_ratio_pct": 0.9, "dx_thermometer": 0.006}, "tau_min": [15, 30]},
               "links": [{"rel": "refines", "id": "F-0001"}, {"rel": "refines", "id": "F-0002"}]}
    session = {"kind": "rig", "id": "R-0001", "title": "A white reading moves between sessions",
               "statement": "Re-placing the meter moves a white reading by up to 0.0025 in xy.", "status": "measured",
               "scope": {"unknown": ["a month apart"]}, "observations": obs({"dxy": [0.0002, 0.0021]}),
               "signature": {"type": "session_offset", "max_abs_dxy": 0.0025, "max_spread_dxy": 0.0008}}
    ents = [refresh, load, thermal] + ([session] if with_session else [])
    store.apply_ops(hdr["unit_id"], [{"op": "create_entry", "by": "llm:test", "why": "t", "entry": e} for e in ents],
                    create=hdr, today="2026-11-14")
    return store


@pytest.mark.parametrize("structured", [False, True])
def test_the_knowledge_hook_reads_the_thermal_offset_and_writes_nothing(tmp_path, structured):
    """A thermal_offset entry without a direction (listed first) is skipped, never returned; the refresh-linked one
    with a direction is: from sign.dx_thermometer_when_cooler '+' when the entry carries it, else from its paired
    x_..._cool / x_..._hot numbers. The session offset comes from the session_offset entry. Nothing is written."""
    store = _knowledge_store(tmp_path, structured_sign=structured)
    before = {p.name: p.stat().st_mtime_ns for p in store.root.iterdir()}
    k = rv.thermal_knowledge(store.root, [None, "Test Panel 32"], mode="HDR", today="2026-11-14")
    assert {p.name: p.stat().st_mtime_ns for p in store.root.iterdir()} == before
    assert k["entry"] == "F-0003" and k["skipped_without_direction"] == ["F-0002"] and k["about_refresh_response"]
    assert k["axis"] == "x" and k["cooler_sign"] == 1 and k["leftover_sign_when_cooler"] == "+"
    assert k["leftover_max_pct"] == 0.9
    assert ("dx_thermometer_when_cooler" in k["cooler_from"]) if structured else ("x_100nit_cool" in k["cooler_from"])
    assert k["session_offset_dxy"] == 0.0025 and k["session_offset_entry"] == "R-0001"
    assert rv.thermal_knowledge(store.root, ["SDR panel nobody knows"]) is None
    sdr = rv.thermal_knowledge(store.root, ["Test Panel 32"], mode="SDR")
    assert sdr["entry"] is None and sdr["session_offset_dxy"] == 0.0025       # the rig's bound is mode-free
    assert rv.thermal_knowledge(tmp_path / "nowhere", ["Test Panel 32"]) is None
    for p in store.root.glob("*.json"):                                       # a broken file is no knowledge
        p.write_text("{broken", encoding="utf-8")
    assert rv.thermal_knowledge(store.root, ["TST1234-12345"], mode="HDR") is None


def test_without_a_session_entry_the_tolerance_is_the_session_bound(tmp_path, fx, record):
    store = _knowledge_store(tmp_path, with_session=False)
    k = rv.thermal_knowledge(store.root, ["Test Panel 32"], mode="HDR")
    assert "session_offset_dxy" not in k
    res = _verify(fx, record, "0807", knowledge=k)
    th = res["thermal"]
    assert th["state"] == "cooler than the record" and th["session_offset_dxy"] == rv.THERMAL_TOL_XY
    assert "no session_offset entry" in th["session_offset_from"]


def test_a_pure_session_offset_on_the_hot_run_is_not_an_established_state(fx, record):
    """The reviewer's case: the HOT run's reference greys shifted by +0.0021 in x (a re-placed meter) read beyond the
    0.002 tolerance in the cooler direction - inside the rig's 0.0025 session offset: not established."""
    rows = []
    for r in fx["runs"]["0846"]["rows"]:
        r2 = dict(r)
        if r.get("xyz") and str(r.get("rate")) == "60":
            s = sum(r["xyz"])
            x, y = r["xyz"][0] / s + 0.0021, r["xyz"][1] / s
            r2["xyz"] = [x / y * r["y"], r["y"], (1 - x - y) / y * r["y"]]
        rows.append(r2)
    th = rv.thermal_state(rt.reference_thermometer(rows, "60"), record[0], knowledge=rf.KNOWLEDGE_F0006)
    assert th["state"] == "cooler than the record" + rv.NOT_ESTABLISHED and th["distance_xy"] > rv.THERMAL_TOL_XY


@pytest.mark.parametrize("run", ["0807", "0846"])
def test_the_thermal_inputs_never_change_a_verdict(fx, record, tmp_path, run):
    """Per grey and per rate, the real morning runs' verdicts are the same with the record's thermometer present /
    absent and the knowledge present / absent / broken: the thermal state is reported, never a gate."""
    r = fx["runs"][run]
    th, src = record
    broken = _knowledge_store(tmp_path)
    for p in broken.root.glob("*.json"):
        p.write_text("{broken", encoding="utf-8")
    kn_broken = rv.thermal_knowledge(broken.root, ["TST1234-12345"])
    assert kn_broken is None
    seen = []
    for rec_th in (th, None):
        for kn in (rf.KNOWLEDGE_F0006, None, kn_broken, {"axis": "x", "cooler_sign": None}):
            res = rv.analyze_verify(r["rows"], reference="60", trim_checks=r["trim_checks"], anomalies=r["anomalies"],
                                    link_bpc=r["link_bpc"], record_th=rec_th, record_source=src, knowledge=kn)
            seen.append((res["verdict"], {rate: (v["verdict"], {p: c["verdict"] for p, c in v["greys"].items()})
                                          for rate, v in res["rates"].items()}, res["recommendation"]["visible"],
                         res["recommendation"]["below_visibility"]))
    assert all(s == seen[0] for s in seen)


@pytest.mark.parametrize("run", ["0807", "0846"])
@pytest.mark.parametrize("fault", ["trim not applied", "half strength", "applied twice"])
def test_a_trim_fault_on_the_real_rows_fails(fx, record, run, fault):
    """The reviewer's enumeration: the trim not applied, at half strength (x sqrt of the recorded ratio) or applied
    twice (x 1 / ratio) on top of what the panel did, on both real runs - each fails the run, with its anomalies kept."""
    r = fx["runs"][run]
    res = _verify(fx, record, run, rows=rf.with_fault(fx, r["rows"], fault))
    assert res["verdict"] == "fail", res["summary"]
    for rate in ("47.952", "50"):
        assert res["rates"][rate]["verdict"] == "fail"
        assert any(c.get("verdict") == "fail" and c["visibility_itp"] >= 1.0
                   for c in res["rates"][rate]["greys"].values())


def test_the_committed_probe_fixtures_match_what_dlc_writes():
    """tests/fixtures/gen_dip_probes.py --check (the DIP / chart fixtures the C++ DLC readers are tested on, built by
    DLC's own probe simulators): the committed files must load as DLC loads them and carry exactly the keys DLC
    writes today. A new DIP field without a regenerated fixture fails here, not in the C++ suite."""
    import importlib.util
    import sys
    gen = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "gen_dip_probes.py"
    if not gen.exists():
        pytest.skip("the repository's C++ fixtures are not beside this DLC tree")
    spec = importlib.util.spec_from_file_location("gen_dip_probes", gen)
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        spec.loader.exec_module(mod)
        assert mod.check() == 0
    finally:
        sys.path[:] = saved


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
    # the plan's time estimate includes the chosen preheat
    est = cal.calib["refresh_probe"]["estimate"]
    assert est["preheat_min"][0] > 0 and est["preheat_min"][1] > est["preheat_min"][0]
    from dataclasses import replace
    run_spec = rp.RefreshProbeSpec.from_dict(cal.calib["refresh_probe"]["spec"])
    none = rp.estimate(cal.calib["refresh_probe"]["legs"], 3, 0, replace(run_spec, preheat="none"))
    assert none["preheat_min"] == [0.0, 0.0]
    assert est["total_min"][0] == pytest.approx(none["total_min"][0] + est["preheat_min"][0], abs=0.15)


def test_the_record_flow_with_closed_loop_preheats_with_the_trim_off_and_records_it(tmp_path, monkeypatch):
    """The record flow (purpose response) with preheat closed_loop: the probe stack (trim OFF, 3D LUT bypassed) is
    set before the preheat runs, and the recorded section's conditions say it was preheated."""
    from test_refresh_probe import _calib as probe_calib
    cal, _, _ = probe_calib(tmp_path, "recpre", spec={"preheat": "closed_loop"})
    cal.controller.call("layers.set", {"monitor": 0, "mode": "HDR", "refresh_trim": True})   # on before the run
    seen = {}
    real = rp._Probe.preheat

    def spy(self):
        seen["layers"] = dict((cal.controller.state().get("layers") or {}).get("0:HDR") or {})
        return real(self)
    monkeypatch.setattr(rp._Probe, "preheat", spy)
    res = cal.run("refresh-response")
    assert res.status == "completed", res.digest
    assert seen["layers"].get("refresh_trim") is False and seen["layers"].get("bypass_3dlut") is True
    resp, _, _ = rt.response_for_run_dir(cal.ctx.root)
    pre = resp.conditions["preheat"]
    assert pre["policy"] == "closed_loop" and "converged" in pre
    cal2, _, _ = probe_calib(tmp_path, "recnone")
    cal2.run("refresh-response")
    assert rt.response_for_run_dir(cal2.ctx.root)[0].conditions["preheat"] == {"policy": "none"}
