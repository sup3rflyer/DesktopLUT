"""grayscale-wb held-out judgment: light and colour judged separately, each against the grey's own
before-read and repeatability measured in the run (repeat reads, the two drift brackets, the level's own
between-presentation scatter). The replay runs the judge on the owner's real 2026-11-14 spot reads (touch-up
off at 02:47, run 025308's touch-up on at 03:13), frozen in ``tests/fixtures/gswb_node12_spots_2026-11-14.json``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from dlc.grayscale_wb import (GrayTouchupConfig, bracket_drift, commanded_light, curve_gains, judge_held_out,
                              light_ratios, slot_position)

_FIX = Path(__file__).parent / "fixtures" / "gswb_node12_spots_2026-11-14.json"
_CFG = GrayTouchupConfig()
_TY = 2.0
_GOOD = [_TY * 0.9505, _TY, _TY * 1.089]


def _at(k, dx=0.0):
    return [_GOOD[0] * k * (1 + dx), _GOOD[1] * k, _GOOD[2] * k]


def _mean(reads):
    return [sum(r[k] for r in reads) / len(reads) for k in range(3)]


def _replay():
    doc = json.loads(_FIX.read_text(encoding="utf-8"))
    cur = doc["curve"]
    n = len(cur["points"])
    pts = [(i / (n - 1)) ** 2 * cur["luminance"][i] for i in range(n)]
    gains = curve_gains(pts, cur["rgb"], "sdr")
    greys, after, after_reads, before_state = [], {}, {}, []
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
        before_state.append({"code": code, "target_Y": ty, "xyz": _mean(doc["off"][key])})
    return judge_held_out(greys, after, config=_CFG, after_reads=after_reads, before_state_reads=before_state)


def test_replay_on_the_real_spot_reads_flags_the_darkened_band():
    """Code 160 went +0.8 % -> -3.6 % in light while its scalar dE IMPROVED (2.96 -> 2.66): judged
    separately, the light regression shows; 138 and 148 regress in light too. These spot reads have no
    brackets: the digest says drift unknown rather than inventing a number."""
    out = _replay()
    rows = {g["code"]: g for g in out["greys"]}
    assert rows[160]["after"]["de2000"] < rows[160]["before"]["de2000"]      # the scalar dE hid it
    for code in (138, 148, 160):
        assert code in out["light_regressed_codes"], (code, rows[code]["light"])
        assert rows[code]["light"]["commanded_pct"] < -1.0                     # the curve commanded it
    assert 514 not in out["regressed_codes"] and 210 not in out["regressed_codes"]
    assert 148 in out["chroma_regressed_codes"]                # its colour damage is visible at 1.8 nit too
    assert out["drift"]["known"] is False and "drift unknown" in out["drift"]["note"]
    assert out["drift"]["expected_light_pct"] is None


def test_light_that_the_curve_did_not_command_is_evidence_not_a_regression():
    reads_b = [_at(1.01 + 0.0005 * j) for j in range(3)]
    reads_a = [_at(0.97 + 0.0005 * j) for j in range(3)]
    greys = [{"code": 200, "target_Y": _TY, "before_xyz": _mean(reads_b), "before_reads": reads_b, "commanded_e": 0.0},
             {"code": 201, "target_Y": _TY, "before_xyz": _mean(reads_b), "before_reads": reads_b,
              "commanded_e": math.log(0.96)}]
    out = judge_held_out(greys, {200: _mean(reads_a), 201: _mean(reads_a)}, config=_CFG,
                         after_reads={200: reads_a, 201: reads_a})
    assert out["light_regressed_codes"] == [201]
    assert [u["code"] for u in out["uncommanded_light"]] == [200]


def test_a_bracket_short_of_pairs_says_drift_unknown():
    few = [{"code": 60 + j, "target_Y": _TY, "early_xyz": _at(1.0), "late_xyz": _at(1.01)} for j in range(5)]
    b = bracket_drift(few, _CFG.white_xy)
    assert b["known"] is False and "drift unknown" in b["note"]
    full = few + [{"code": 70 + j, "target_Y": _TY, "early_xyz": _at(1.0), "late_xyz": _at(1.01)} for j in range(3)]
    assert bracket_drift(full, _CFG.white_xy)["known"] is True


def test_drift_from_the_two_brackets_is_carried_into_the_comparison():
    """The display brightened 2 % over the session and 2 % more over the verify: a held-out grey read early in
    the session and late in the verify comes back ~2 % brighter on average - drift, not the touch-up."""
    sb = [{"code": 60 + 40 * j, "target_Y": _TY, "early_xyz": _at(1.0 + 0.0002 * j), "late_xyz": _at(1.02 + 0.0002 * j)}
          for j in range(8)]
    vb = [{"code": 60 + 40 * j, "target_Y": _TY, "early_xyz": _at(1.02 + 0.0002 * j), "late_xyz": _at(1.04 + 0.0002 * j)}
          for j in range(8)]
    reads = [_at(1.0), _at(1.0005)]
    greys = [{"code": 300, "target_Y": _TY, "before_xyz": _mean(reads), "before_reads": reads, "commanded_e": 0.0}]
    after = [_at(1.02), _at(1.0205)]
    alone = judge_held_out(greys, {300: _mean(after)}, config=_CFG, after_reads={300: after})
    assert alone["drift"]["known"] is False
    out = judge_held_out(greys, {300: _mean(after)}, config=_CFG, after_reads={300: after},
                         session_bracket=sb, verify_bracket=vb)
    assert out["drift"]["known"] and out["drift"]["rests_on_reads"] == 32
    assert out["drift"]["expected_light_pct"] == pytest.approx(1.98, abs=0.1)
    assert out["n_regressed"] == 0 and not out["uncommanded_light"]
