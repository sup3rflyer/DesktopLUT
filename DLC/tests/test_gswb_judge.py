"""grayscale-wb held-out judgment (review of 3374b91, items 2, 5 and 6): light and chromaticity are judged
separately, each against the grey's own before-read and its own repeatability; the null uses measured
pairs only; session drift is bracketed. The replay runs the judge on the owner's real 2026-11-14 spot
reads (touch-up off at 02:47, run 025308's touch-up on at 03:13), frozen in
``tests/fixtures/gswb_node12_spots_2026-11-14.json``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from dlc.grayscale_wb import (GrayTouchupConfig, commanded_light, curve_gains, judge_held_out, light_ratios,
                              slot_position)

_FIX = Path(__file__).parent / "fixtures" / "gswb_node12_spots_2026-11-14.json"
_CFG = GrayTouchupConfig()


def _mean(reads):
    return [sum(r[k] for r in reads) / len(reads) for k in range(3)]


def _replay():
    doc = json.loads(_FIX.read_text(encoding="utf-8"))
    cur = doc["curve"]
    n = len(cur["points"])
    pts = [(i / (n - 1)) ** 2 * cur["luminance"][i] for i in range(n)]
    gains = curve_gains(pts, cur["rgb"], "sdr")
    greys, after, after_reads = [], {}, {}
    for key in sorted(doc["off"], key=int):
        code = int(key)
        if key not in doc["on"] or code >= 1023:
            continue
        ty = 120.0 * (code / 1023) ** 2.2
        u = slot_position(code, 1023, n, "sdr")
        cmd = commanded_light(light_ratios(gains, None, {ch: u for ch in "rgb"}, light_exponent=2.2, kind="sdr"))
        greys.append({"code": code, "target_Y": ty, "before_xyz": _mean(doc["off"][key]),
                      "before_reads": doc["off"][key], "commanded_e": cmd})
        after[code] = _mean(doc["on"][key])
        after_reads[code] = doc["on"][key]
    return judge_held_out(greys, after, [], config=_CFG, after_reads=after_reads)


def test_replay_on_the_real_spot_reads_flags_the_darkened_band():
    """Code 160 went +0.8 % -> -3.6 % in light while its scalar dE IMPROVED (2.96 -> 2.66): judged
    separately, the light regression shows; 138 and 148 regress in light too."""
    out = _replay()
    rows = {g["code"]: g for g in out["greys"]}
    assert rows[160]["after"]["de2000"] < rows[160]["before"]["de2000"]      # the scalar dE hid it
    for code in (138, 148, 160):
        assert code in out["light_regressed_codes"], (code, rows[code]["light"])
        assert rows[code]["light"]["commanded_pct"] < -1.0                     # the curve commanded it
    assert {138, 148, 160} <= set(out["regressed_codes"])
    assert 514 not in out["regressed_codes"] and 210 not in out["regressed_codes"]   # the greys it fixed


def test_light_that_the_curve_did_not_command_is_evidence_not_a_regression():
    """A grey whose light moved by a tooth while the curve commanded ~nothing there (a drive change across a
    sawtooth edge) is listed as uncommanded light, not counted as the curve's regression."""
    ty = 2.0
    good = [ty * 0.9505, ty, ty * 1.089]

    def at(k):
        return [v * k for v in good]

    reads_b = [at(1.01 + 0.0005 * j) for j in range(3)]
    reads_a = [at(0.97 + 0.0005 * j) for j in range(3)]
    greys = [{"code": 200, "target_Y": ty, "before_xyz": _mean(reads_b), "before_reads": reads_b, "commanded_e": 0.0},
             {"code": 201, "target_Y": ty, "before_xyz": _mean(reads_b), "before_reads": reads_b,
              "commanded_e": math.log(0.96)}]
    out = judge_held_out(greys, {200: _mean(reads_a), 201: _mean(reads_a)}, [], config=_CFG,
                         after_reads={200: reads_a, 201: reads_a})
    assert out["light_regressed_codes"] == [201]
    assert [u["code"] for u in out["uncommanded_light"]] == [200]


def test_moved_nodes_never_enter_the_null_and_too_few_pairs_say_so():
    ty = 2.0
    good = [ty * 0.9505, ty, ty * 1.089]
    pairs = [{"code": 100 + i, "target_Y": ty, "final_xyz": good, "verify_xyz": good} for i in range(5)]
    greys = [{"code": 50, "target_Y": ty, "before_xyz": good, "before_reads": [good, good]}]
    out = judge_held_out(greys, {50: good}, pairs, config=_CFG)
    assert out["null"]["used"] is False and "not in sigma" in out["null"]["note"]


def test_bracket_drift_is_carried_into_the_comparison():
    """Every bracket grey read 2 % brighter late in the session than early: a held-out grey 2 % brighter
    after than before is drift, not the touch-up."""
    ty = 2.0
    good = [ty * 0.9505, ty, ty * 1.089]

    def at(k):
        return [v * k for v in good]

    bracket = [{"code": 60 + 40 * j, "target_Y": ty, "early_xyz": at(1.0 + 0.0002 * j), "late_xyz": at(1.02 + 0.0002 * j)}
               for j in range(8)]
    reads = [at(1.0), at(1.0005)]
    greys = [{"code": 300, "target_Y": ty, "before_xyz": _mean(reads), "before_reads": reads, "commanded_e": 0.0}]
    late = [at(1.02), at(1.0205)]
    without = judge_held_out(greys, {300: _mean(late)}, [], config=_CFG, after_reads={300: late})
    assert without["uncommanded_light"]                                   # 2 % with no bracket: unexplained
    out = judge_held_out(greys, {300: _mean(late)}, [], config=_CFG, after_reads={300: late}, bracket=bracket)
    assert out["bracket"]["used"] and out["bracket"]["drift_light_pct"] == pytest.approx(1.98, abs=0.05)
    assert out["n_regressed"] == 0 and not out["uncommanded_light"]
