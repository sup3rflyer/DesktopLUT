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
def _judge(run, *, verify_bracket=True):
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
                                                                       light_exponent=2.2, kind="sdr"))})
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
    return judge_held_out(greys, first, config=cfg, session_bracket=run["held_out"]["bracket"],
                          verify_bracket=vb if verify_bracket else None, before_state_reads=before)


def test_a_dark_grey_is_judged_against_its_own_levels_scatter(run):
    """Code 69 (0.3 nit): colour 0.91 -> 1.46 dE in the touch-up's luminance-relative metric, flagged by 0.01 over
    the 0.6 tolerance. That metric weighs a 0.3-nit grey like a 100-nit one. Judged in BT.2124 dE_ITP at its
    own light, with the noise of re-presenting greys at that level (the bracket pairs nearest in light), the
    change is a fraction of one just noticeable difference: not a regression."""
    out = _judge(run)
    g69 = next(g for g in out["greys"] if g["code"] == 69)
    assert g69["chroma"]["after_de"] - g69["chroma"]["before_de"] == pytest.approx(0.55, abs=0.05)
    assert g69["chroma"]["change_itp"] < 0.2
    assert {x["code"] for x in g69["chroma"]["level_scatter_from"]} & {56, 108}   # its own level's re-reads
    assert 69 not in out["regressed_codes"]
    dark, bright = g69["chroma"]["level_scatter_itp"], next(
        g for g in out["greys"] if g["code"] == 811)["chroma"]["level_scatter_itp"]
    assert dark != bright                                          # the noise is the level's own


def test_a_smoothed_nodes_light_is_an_observation_where_light_is_stepped(run):
    """Node 12 (code 153): only its balance changed; its colour landed as predicted (3.79 vs 3.71 dE), its light
    went 3.86 -> 1.08 % against a predicted 4.19 % - on a display whose grey light is stepped in the code (the
    before-state greys' own light roughness says so) that light is reported, not judged."""
    out = _judge(run)
    p = next(p for p in out["predicted"] if p["code"] == 153)
    assert p["chroma_ok"] is True and p["light_judged"] is False and p["light_ok"] is None
    assert "NOT judged" in p["light_observation"] and p["as_predicted"] is True
    assert p["light_roughness_pct"] > 1.0


def test_the_drift_rests_on_two_brackets_and_says_unknown_without_one(run):
    """The hardware run had no verify bracket (it predates it): the replay says drift unknown, rather than the
    old one-pair null. Every grey still reads better than before or within its noise: the recommendation the
    flow would now make is apply."""
    out = _judge(run, verify_bracket=False)
    assert out["drift"]["known"] is False and "drift unknown" in out["drift"]["note"]
    assert out["drift"]["session_bracket"]["known"] is True and out["drift"]["session_bracket"]["n"] == 8
    assert out["n_regressed"] == 0, out["worst"]
    assert all(p["as_predicted"] for p in out["predicted"])
