"""grayscale-wb hardware acceptance, 2026-11-14 11:46 (PA32UCXR, SDR 10-bit): the five judgement defects the
run showed, replayed offline on its own files (frozen in ``tests/fixtures/gswb_hw_run_2026-11-14_1146.json``:
the committed curve, the solver's per-node curve, the light-trend record, the held-out before-reads and the
session bracket, the nodes' round-1 reads, and the verify reads in measurement order).

The run: light-trend ``trend``, node-outlier ``smooth`` at node 12 (code 153); the size seam fired with the
solver's 6.7 % luminance; the verify flagged code 69 (colour 0.91 -> 1.46 dE at 0.3 nit, +0.01 over the 0.6
tolerance) and called code 153 "did not do what was predicted" on its LIGHT (3.86 -> 1.08 % vs 4.19 %)
although only its balance changed; the null had one pair; the per-node read noise read 0.001 %."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from dlc.grayscale_wb import (GrayTouchupConfig, carry_through, commanded_light, committed_size, judge_held_out,
                              light_ratios, light_trend, slot_position)

_FIX = Path(__file__).parent / "fixtures" / "gswb_hw_run_2026-11-14_1146.json"


@pytest.fixture(scope="module")
def run():
    return json.loads(_FIX.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- 1. the size seam
def test_the_size_seam_measures_the_curve_that_will_be_committed(run):
    """The solver's raw curve was 'large' (6.7 % luminance); the curve committed after light-trend + smooth
    carries <= 0.44 % light: the size seam must not call it large."""
    assert run["solver_raw"]["large_correction"] is True
    assert run["solver_raw"]["max_abs_luminance"] == pytest.approx(0.0669, abs=0.001)
    size = committed_size(run["committed"])
    assert size["max_abs_luminance"] <= 0.0045 and size["large_luminance_correction"] is False
    assert size["large_correction"] is False and size["capped"] is False
    assert "committed" in size["what"]
    raw = committed_size(run["per_node_payload"])        # the same function on the solver's curve: large
    assert raw["large_correction"] is True and raw["max_abs_luminance"] == pytest.approx(0.0669, abs=0.001)


# ---------------------------------------------------------------------------- 5. the per-node read noise
def test_the_within_presentation_scatter_is_named_and_decides_nothing(run):
    """The 0.001 % 'read noise' was the scatter of the reads of ONE presentation (a counting colorimeter's
    back-to-back reads agree to ~1e-5). It is reported under that name; the material decision rests on the
    between-presentation scatter (the session bracket) or, unknown, treats every light move as material."""
    nodes = [{"index": r["index"], "code": r["code"], "need": math.log(1 + r["need_pct"] / 100.0),
              "own": math.log(1 + r["own_pct"] / 100.0), "sigma": 1e-5} for r in run["light_trend"]["nodes"]]
    a = light_trend(nodes, between_sigma=None)
    b = light_trend([{**n, "sigma": 0.01} for n in nodes], between_sigma=None)
    assert a["material"] == b["material"] == a["changed"]                 # within-presentation SE decides nothing
    assert "sigma_pct" not in a["nodes"][0] and a["nodes"][0]["within_presentation_se_pct"] == pytest.approx(0.001)
    c = light_trend(nodes, between_sigma=0.0005)
    assert set(c["material"]) <= set(c["changed"])
    assert all(r["between_presentation_sigma_pct"] == 0.05 for r in c["nodes"])


# ---------------------------------------------------------------------------- 2/3/4. the verify judgement
def _judge(run, *, verify_bracket=True, cmd_add=None):
    cfg = GrayTouchupConfig(white_xy=tuple(run["white_xy"]))
    first, late = {}, {}
    for r in run["verify_reads"]:
        if r["rgb"][0] == r["rgb"][1] == r["rgb"][2]:
            (late if r["code"] in first else first)[r["code"]] = r["xyz"]
    com = run["committed"]
    n = com["point_count"]
    gains = {ch: com["deviations"][ch] for ch in "rgb"}
    greys = []
    for g in run["held_out"]["greys"]:
        u = slot_position(g["code"], 1023, n, "sdr")
        greys.append({**g, "commanded_e": commanded_light(light_ratios(gains, None, {ch: u for ch in "rgb"},
                                                                       light_exponent=2.2, kind="sdr"))
                      + (cmd_add or {}).get(g["code"], 0.0)})
    nodes = {nd["index"]: nd for nd in run["nodes"]}
    k = 12
    fr = nodes[k]["first_read"]
    u = slot_position(nodes[k]["code"], 1023, n, "sdr")
    ratios = light_ratios(gains, None, {ch: u for ch in "rgb"}, light_exponent=2.2, kind="sdr")
    greys.append({"code": nodes[k]["code"], "kind": "smoothed_node", "target_Y": fr["target_Y"], "before_xyz": fr["xyz"],
                  "before_reads": fr.get("reads") or (), "predicted_xyz": carry_through(fr["xyz"], ratios, cfg.white_xy)})
    before = [{"code": g["code"], "target_Y": g["target_Y"], "xyz": g["before_xyz"]} for g in run["held_out"]["greys"]]
    before += [{"code": nd["code"], "target_Y": nd["first_read"]["target_Y"], "xyz": nd["first_read"]["xyz"]}
               for nd in run["nodes"] if nd.get("first_read")]
    tgt = {g["code"]: g["target_Y"] for g in run["held_out"]["greys"]}
    vb = [{"code": c, "target_Y": tgt[c], "early_xyz": first[c], "late_xyz": late[c]} for c in late if c in tgt]
    if verify_bracket == "as_session":   # the verify drifting exactly as the session did (the run had no bracket)
        vb = run["held_out"]["bracket"]
    return judge_held_out(greys, first, config=cfg, session_bracket=run["held_out"]["bracket"],
                          verify_bracket=vb if verify_bracket else None, before_state_reads=before,
                          chroma_tol=run["target_de"])


def _shift_colour(a, r, white):
    """The verify read ``a`` moved radially away from the target white by ``r`` in xy, light unchanged."""
    s = sum(a)
    xa, ya = a[0] / s, a[1] / s
    dx, dy = xa - white[0], ya - white[1]
    n = math.hypot(dx, dy) or 1.0
    x2, y2 = xa + r * dx / n, ya + r * dy / n
    return [x2 * a[1] / y2, a[1], (1 - x2 - y2) * a[1] / y2]


def _with_read(run, code, fn):
    r2 = json.loads(json.dumps(run))
    for v in r2["verify_reads"]:
        if v["code"] == code:
            v["xyz"] = fn(v["xyz"])
            break
    return r2


def _class(out, code):
    if code in out["regressed_codes"]:
        return "regressed"
    if code in [b["code"] for b in out["regressed_below_visibility"]]:
        return "below_visibility"
    return "kept"


def test_the_real_run_ends_in_apply_under_the_rulings(run):
    """Ruling A-D on the real verify: the fit's own metric against the run's target (0.6 dE), the level's measured
    noise, the run's single (session) bracket widening the drift. Nothing is worse than before beyond the target;
    code 69 (0.91 -> 1.46 dE, +0.55) stays inside its widened threshold; code 153 did what was predicted."""
    out = _judge(run, verify_bracket=False)
    assert out["chroma_tol_de"] == run["target_de"] == 0.6
    assert out["drift"]["state"] == "widened" and out["drift"]["known"] is False
    assert out["regressed_codes"] == [] and out["regressed_below_visibility"] == []
    g69 = next(g for g in out["greys"] if g["code"] == 69)
    assert g69["chroma"]["change_de"] == pytest.approx(0.55, abs=0.02)
    assert g69["chroma"]["threshold_de"] > g69["chroma"]["change_de"]
    assert g69["chroma"]["visibility_itp"] == pytest.approx(0.079, abs=0.01)
    assert all(p["as_predicted"] for p in out["predicted"])


def test_with_both_brackets_code_69_is_worse_but_below_visibility(run):
    """Had the verify drifted as the session did (both brackets known), code 69's +0.61 dE beyond the 0.6 target is
    a real worsening - 0.08 dE_ITP at 0.3 nit: listed under 'below visibility', apply recommended, 69 named."""
    out = _judge(run, verify_bracket="as_session")
    assert out["drift"]["state"] == "known"
    assert out["regressed_codes"] == []
    below = {b["code"]: b for b in out["regressed_below_visibility"]}
    assert set(below) == {69} and below[69]["visibility_itp"] < 0.1


def test_the_level_noise_is_the_full_colour_difference_of_nearby_re_reads(run):
    """The colour noise at a level is the colour difference between the two reads of the bracket pairs nearest in
    light (tangential moves included), and it can bind: at the bright end it lifts the threshold over the target."""
    out = _judge(run, verify_bracket=False)
    g69 = next(g for g in out["greys"] if g["code"] == 69)
    assert {x["code"] for x in g69["chroma"]["level_noise_from"]} >= {56, 108}
    bright = next(g for g in out["greys"] if g["code"] == 491)
    assert bright["chroma"]["level_noise_de"] > g69["chroma"]["level_noise_de"]
    assert any(g["chroma"]["threshold_de"] > out["chroma_tol_de"] for g in out["greys"]
               if g.get("judged") and "chroma" in g and g["kind"] == "between")


# The reviewer's injection table (review/cmp_judge.py) under ruling B: one held-out grey of the real verify damaged,
# everything else as measured; the classes expected per cell. The session bracket alone widens the drift (ruling D);
# 362's +0.004 is caught once both brackets are known (the next test).
_COLOUR_CELLS = [
    (133, 0.001, "kept"), (133, 0.002, "below_visibility"), (133, 0.004, "regressed"),
    (362, 0.001, "kept"), (362, 0.002, "kept"), (362, 0.004, "kept"),
    (932, 0.001, "kept"), (932, 0.002, "kept"), (932, 0.004, "regressed"),
    (69, 0.001, "below_visibility"), (69, 0.002, "below_visibility"), (69, 0.004, "regressed"),
]


@pytest.mark.parametrize("code,r,expected", _COLOUR_CELLS)
def test_injected_colour_damage_is_classed_as_ruled(run, code, r, expected):
    out = _judge(_with_read(run, code, lambda a: _shift_colour(a, r, tuple(run["white_xy"]))), verify_bracket=False)
    assert _class(out, code) == expected


@pytest.mark.parametrize("code,r", [(362, 0.004), (133, 0.002), (69, 0.002)])
def test_old_power_is_back_with_both_brackets(run, code, r):
    """0.3, 1.3 and 12 nit: the old judge caught these; with a known drift the new one does (below visibility)."""
    out = _judge(_with_read(run, code, lambda a: _shift_colour(a, r, tuple(run["white_xy"]))),
                 verify_bracket="as_session")
    assert _class(out, code) in ("below_visibility", "regressed")


@pytest.mark.parametrize("code", [133, 362, 932, 69])
@pytest.mark.parametrize("p", [0.01, 0.02, 0.05, 0.08])
@pytest.mark.parametrize("commanded", [True, False])
def test_injected_light_damage_is_classed_as_ruled(run, code, p, commanded):
    g = next(x for x in run["held_out"]["greys"] if x["code"] == code)
    a0 = next(v["xyz"] for v in run["verify_reads"] if v["code"] == code)
    k = 1.0 + (-1.0 if math.log(a0[1] / g["target_Y"]) < 0 else 1.0) * p
    out = _judge(_with_read(run, code, lambda a: [v * k for v in a]), verify_bracket=False,
                 cmd_add={code: math.log(k)} if commanded else None)
    caught = code in out["regressed_codes"]
    flagged_uncommanded = code in [u["code"] for u in out["uncommanded_light"]]
    big = p >= (0.05 if code == 362 else 0.02)          # 362 starts -1.07 % off: +1 / +2 % move it toward / just past
    assert caught == (commanded and big)
    assert flagged_uncommanded == ((not commanded) and big)


@pytest.mark.parametrize("p,caught", [(0.0, False), (0.05, True), (-0.05, True), (0.10, True)])
def test_a_smoothed_nodes_light_is_bounded_by_one_display_step(run, p, caught):
    """Node 12 (code 153): its balance was smoothed, the drive rose, its light fell 3.3 % below the prediction (one
    step of this stepped panel: the detrended before-state light around it spans 5.9 %). Injected +5 % / -5 % /
    +10 % on the real read go beyond what one step in that direction allows and are caught."""
    out = _judge(_with_read(run, 153, lambda a: [v * (1 + p) for v in a]), verify_bracket=False)
    pr = next(x for x in out["predicted"] if x["code"] == 153)
    assert pr["drive_change"] == "up" and pr["step_upper_pct"] == pytest.approx(5.86, abs=0.05)
    assert pr["chroma_ok"] is True
    assert (not pr["as_predicted"]) == caught


def test_unknown_drift_is_never_an_apply(run):
    """No bracket at all: every verdict says drift unknown and the caller does not recommend apply on it."""
    r2 = json.loads(json.dumps(run))
    r2["held_out"]["bracket"] = []
    out = _judge(r2, verify_bracket=False)
    assert out["drift"]["state"] == "unknown" and "apply must not be recommended" in out["drift"]["note"]
    assert all(g.get("drift") == "unknown" for g in out["greys"] if g.get("judged"))


@pytest.mark.parametrize("nits,dx,dy,ref", [
    (0.3, 0.0020, 0.0, 0.4843), (0.3, 0.0, 0.0020, 0.3331), (10.0, 0.0020, 0.0, 1.0273), (10.0, 0.0020, 0.0020, 0.8931),
    (100.0, 0.0006, 0.0, 0.3924), (100.0, 0.0020, 0.0, 1.3076), (1000.0, 0.0040, 0.0, 2.8397),
    (1000.0, 0.0, 0.0020, 0.9777)])
def test_chroma_itp_at_absolute_light_matches_the_reference(nits, dx, dy, ref):
    """dE_ITP of an xy shift at an ABSOLUTE luminance, against the colour package (ITU-R BT.2100 PQ ICtCp, BT.2124
    dE_ITP) values in review/itp_ref.py - a relative-Y ITP would get every level wrong."""
    from dlc.colormath import xy_to_XYZ
    from dlc.grayscale_wb import chroma_itp
    w = (0.3127, 0.3290)
    assert chroma_itp(xy_to_XYZ(w[0] + dx, w[1] + dy, nits), xy_to_XYZ(w[0], w[1], nits)) == pytest.approx(ref, abs=2e-4)
