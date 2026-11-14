"""grayscale-wb: the LIGHT part of the touch-up is committed only as light the nodes on both sides
share (review of 3374b91, item 1). The display this flow was built on has full-field light that is a
sawtooth in the code (teeth of 2-4 % every 4-12 codes, chromaticity flat), so a node's light need is
one phase of it. The rule must never extrapolate, must be supported on both sides, must reach zero at
the edges of the supported range, must never exceed what the supporting nodes ask for, and must not
depend on the assumed read noise.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from dlc.grayscale_wb import light_trend

_RUN = Path("H:/Projects/DesktopLUT/DLC/runs/20261114_020606_626719_sdr_asus_proart_pa32ucxr/dlc_state.json")


def _nodes(needs, sigma=0.001, start=0):
    return [{"index": start + i, "code": (start + i) * 30, "need": v, "own": v, "sigma": sigma}
            for i, v in enumerate(needs)]


def _committed(lt):
    return [r["committed_gain"] for r in lt["nodes"]]


def _check_laws(needs, lt):
    """The rule's laws on any input: bounded by every ask in its centred window, never on an end."""
    com = [math.log(g) for g in _committed(lt)]
    n = len(needs)
    for i, c in enumerate(com):
        if i < 2 or i > n - 3:
            assert c == 0.0, (i, c)                        # not supported on both sides: no light
            continue
        window = needs[i - 2:i + 3]
        if c > 0:
            assert all(v >= c - 1e-12 for v in window)   # every supporting node asks at least this
        elif c < 0:
            assert all(v <= c + 1e-12 for v in window)
    return com


def test_alternating_teeth_commit_no_light_anywhere():
    needs = [0.03 * (1 if i % 2 else -1) for i in range(20)]
    lt = light_trend(_nodes(needs))
    com = _check_laws(needs, lt)
    assert all(c == 0.0 for c in com)                       # the reviewer saw +3.05 % invented at the top


def test_a_shared_trend_is_kept_and_tapers_to_zero_at_the_edges():
    needs = [0.02] * 12
    com = _check_laws(needs, light_trend(_nodes(needs)))
    assert com[2:-2] == pytest.approx([0.02] * 8)
    assert com[:2] == [0.0, 0.0] and com[-2:] == [0.0, 0.0]


def test_single_and_paired_teeth_and_one_sided_ramps_commit_nothing():
    for needs in ([0.0] * 8 + [-0.04] + [0.0] * 11,
                  [0.0] * 8 + [-0.04, -0.04] + [0.0] * 10,
                  [0.04 - 0.01 * i if i < 4 else 0.0 for i in range(20)]):
        com = _check_laws(needs, light_trend(_nodes(needs)))
        assert all(c == 0.0 for c in com), needs


def test_the_committed_light_does_not_depend_on_the_assumed_noise():
    """Same data, the assumed read noise x1 and x3: the same committed light (bound: exactly equal -
    the rule uses the asks only, the noise only labels the evidence)."""
    needs = [0.01 * math.sin(0.9 * i) + 0.004 * i for i in range(22)]
    a = light_trend(_nodes(needs, sigma=0.001))
    b = light_trend(_nodes(needs, sigma=0.003))
    assert _committed(a) == _committed(b)
    _check_laws(needs, a)


@pytest.mark.skipif(not _RUN.exists(), reason="the 2026-11-14 hardware record is local to the owner's machine")
def test_the_hardware_record_invents_no_light_at_the_dark_end():
    """2026-11-14 run 020606: nodes 8 / 9 / 10 asked for +5.6 / -4.4 / -2.6 %; 3374b91 committed
    +1.9 / +1.4 / +0.9 % that no read asked for (and -6.2 % at node 8 with 3x the noise)."""
    s = json.loads(_RUN.read_text(encoding="utf-8"))["calib"]
    pay = s["grayscale_wb_touchup"]
    nodes = []
    for p in sorted(s["stages"]["grayscale-wb"]["digest"]["per_point"], key=lambda q: q["index"]):
        rounds = p["rounds"]
        if p["code"] >= 1023 or not rounds or any((x.get("update") or {}).get("held_dark") for x in rounds):
            continue
        last = rounds[-1]
        if last.get("update") or last["measured_Y"] <= 0 or last["target_Y"] < 0.25:
            continue
        lum = pay["luminance"][p["index"]]
        nodes.append({"index": p["index"], "code": p["code"], "own": math.log(lum),
                      "need": math.log(lum) + math.log(last["target_Y"] / last["measured_Y"]) / 2.2,
                      "sigma": (0.003 / math.sqrt(3) if last["target_Y"] >= 30 else 0.01) / 2.2})
    for mult in (1.0, 3.0):
        lt = light_trend([{**n, "sigma": n["sigma"] * mult} for n in nodes])
        rows = {r["index"]: r for r in lt["nodes"]}
        for k in (8, 9, 10):
            ask = rows[k]["need_pct"]
            com = rows[k]["committed_pct"]
            assert com == 0.0 or (com * ask > 0 and abs(com) <= abs(ask)), (mult, k, ask, com)
        assert rows[8]["committed_pct"] == 0.0 and rows[9]["committed_pct"] == 0.0   # an end: no support
