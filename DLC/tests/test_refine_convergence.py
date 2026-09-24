"""Physics-grounded refine convergence (dlc.refine_convergence) — the replacement for the fixed
``target_de`` stop that accepted round 1 of the 2026-09-24 PA32UCXR run at 1.26 < 2.0 and left a
6σ uniform cool cast. These pin the judgment on synthetic greys with a simple, exact ΔE."""

from __future__ import annotations

import math

import pytest

from dlc.colormath import xy_to_XYZ
from dlc.refine_convergence import (
    MATERIAL_GAIN_JND,
    GreyLevel,
    PanelFloor,
    analyse_round,
    channel_quantization,
    panel_floor_from_thermal,
)

D65 = (0.3127, 0.3290)
# A wide-gamut native panel's full-drive primaries (absolute XYZ at ~1800 nits white).
_P = {"r": (0.690, 0.303), "g": (0.181, 0.751), "b": (0.151, 0.050)}


def _peaks(white_nits: float = 1800.0):
    from dlc.colormath import rgb_to_xyz_matrix
    m = rgb_to_xyz_matrix(_P["r"][0], _P["r"][1], _P["g"][0], _P["g"][1], _P["b"][0], _P["b"][1],
                          D65[0], D65[1], white_Y=white_nits)
    return [[m[row][c] for row in range(3)] for c in range(3)]


def _de(m, t) -> float:
    """A simple exact metric for the tests: 1000·|Δxy| + 100·|ΔY/Y| (chroma + luminance)."""
    sm, st = sum(m), sum(t)
    dx = m[0] / sm - t[0] / st
    dy = m[1] / sm - t[1] / st
    return 1000.0 * math.hypot(dx, dy) + 100.0 * abs(m[1] / t[1] - 1.0)


def _levels(cast=(0.0, 0.0), *, scatter=0.0, lum=0.0, se=5e-5, quant=0.0, n=16):
    out = []
    for i in range(n):
        nits = 1.0 * (1000.0 ** (i / (n - 1)))           # 1 → 1000 nits
        wob = scatter * (1 if i % 2 else -1)
        m = xy_to_XYZ(D65[0] + cast[0] + wob, D65[1] + cast[1] - wob, nits * (1.0 + lum))
        out.append(GreyLevel(signal=i / n, measured_xyz=tuple(m),
                             target_xyz=tuple(xy_to_XYZ(D65[0], D65[1], nits)),
                             meter_se_xy=se, quant_xy=quant))
    return out


def test_a_uniform_cast_well_above_the_floor_continues_and_is_named():
    # The 09-24 shape: every level ~0.0011 x cool, meter SE 5e-5, stable panel.
    a = analyse_round(_levels(cast=(-0.0011, 0.0001), scatter=0.0002), de_fn=_de,
                      floor=PanelFloor(drift_xy=0.00015))
    assert a["decision"] == "continue"
    assert a["cast_real"] is True and a["cast_sigma"] > 3
    assert a["cast_xy"][0] == pytest.approx(-0.0011, abs=1e-5)
    assert a["predicted_gain"] >= MATERIAL_GAIN_JND


def test_errors_within_the_physical_floor_converge_in_round_one():
    # Errors no bigger than meter ⊕ drift ⊕ quantization: nothing removable → converged.
    a = analyse_round(_levels(cast=(0.00005, 0.0), scatter=0.00005), de_fn=_de,
                      floor=PanelFloor(drift_xy=0.0001), previous=None)
    assert a["decision"] == "converged"
    assert a["cast_real"] is False
    assert a["raw_gain"] < MATERIAL_GAIN_JND


def test_measured_efficacy_discounts_the_prediction_and_flags_a_hidden_floor():
    # Round 1 predicted ~2 of gain; the step realized almost none of it (the panel won't move).
    first = analyse_round(_levels(cast=(-0.002, 0.0)), de_fn=_de, floor=PanelFloor())
    assert first["decision"] == "continue"
    second = analyse_round(_levels(cast=(-0.00195, 0.0)), de_fn=_de, floor=PanelFloor(),
                           previous=first)
    assert second["efficacy"] < 0.1
    assert second["raw_gain"] >= MATERIAL_GAIN_JND          # material error still there...
    assert second["decision"] == "floored"                  # ...but the refine can't remove it


def test_an_effective_step_keeps_going_until_the_rest_is_immaterial():
    first = analyse_round(_levels(cast=(-0.002, 0.0)), de_fn=_de, floor=PanelFloor())
    second = analyse_round(_levels(cast=(-0.0003, 0.0)), de_fn=_de, floor=PanelFloor(),
                           previous=first)
    assert second["efficacy"] > 0.8
    assert second["decision"] == "converged"                 # 0.3·1000·... < a quarter JND


def test_output_quantization_is_a_floor_the_prediction_respects():
    # The same 0.0006 xy scatter is removable on a fine pipeline but NOT when a code step of the
    # output is that coarse — the prediction can never promise to beat the quantization.
    fine = analyse_round(_levels(scatter=0.0006, quant=0.0), de_fn=_de, floor=PanelFloor())
    coarse = analyse_round(_levels(scatter=0.0006, quant=0.0009), de_fn=_de, floor=PanelFloor())
    assert fine["raw_gain"] > coarse["raw_gain"]
    assert coarse["decision"] == "converged"


def test_luminance_gain_error_is_removable_and_significant():
    a = analyse_round(_levels(lum=0.02), de_fn=_de, floor=PanelFloor(drift_rel=0.001))
    assert a["lum_gain_real"] is True and a["lum_gain"] == pytest.approx(0.02, abs=1e-4)
    assert a["decision"] == "continue"


def test_an_unstable_level_keeps_its_error_and_stays_out_of_the_cast():
    lv = _levels(cast=(0.0, 0.0), n=8)
    bad = lv[3]
    lv[3] = GreyLevel(bad.signal, tuple(xy_to_XYZ(D65[0] + 0.01, D65[1], bad.target_xyz[1])),
                      bad.target_xyz, meter_se_xy=math.inf)
    a = analyse_round(lv, de_fn=_de, floor=PanelFloor())
    assert a["unstable_levels"] == 1
    assert a["cast_real"] is False                            # the wild level can't fake a cast
    assert a["raw_gain"] == pytest.approx(0.0, abs=1e-6)      # and nothing is promised for it


def test_no_evidence_is_unjudged_not_converged():
    # An empty band, or one where every level is unstable, is not a deterministic "done" — the
    # orchestrator hands 'unjudged' to the LLM.
    a = analyse_round([], de_fn=_de, floor=PanelFloor())
    assert a["decision"] == "unjudged" and a["band_avg"] is None and "no measurable" in a["reason"]
    lv = [GreyLevel(g.signal, g.measured_xyz, g.target_xyz, meter_se_xy=math.inf)
          for g in _levels(cast=(-0.002, 0.0), n=6)]
    b = analyse_round(lv, de_fn=_de, floor=PanelFloor())
    assert b["decision"] == "unjudged" and "unstable" in b["reason"]


def test_a_healthy_damped_step_is_converged_not_floored():
    # Review finding: efficacy < 1 is normal (damping 0.85, noise), so a low PREDICTION alone is
    # not a floor. The last step realized a material 0.65 of its predicted 1.0 (efficacy 0.65), so
    # the remaining 0.30 of raw removable error × 0.65 < a quarter JND → 'converged', not floored.
    second = analyse_round(_levels(cast=(-0.00035, 0.0)), de_fn=_de, floor=PanelFloor(),
                           previous={"band_avg": 1.0, "raw_gain": 1.0})
    assert second["raw_gain"] >= MATERIAL_GAIN_JND > second["predicted_gain"]
    assert second["realized_gain"] >= MATERIAL_GAIN_JND
    assert second["decision"] == "converged"


def test_quantization_floor_scales_with_bit_depth_and_matches_10bit_pq():
    from dlc._pq import eotf_norm, oetf_norm
    peaks = _peaks()
    target = xy_to_XYZ(D65[0], D65[1], 100.0)

    def rel(bits):
        v = oetf_norm(100.0 / 10000.0)
        return eotf_norm(v + 1.0 / (2 ** bits - 1)) / eotf_norm(v) - 1.0

    q10 = channel_quantization(target, peaks, rel(10))
    q8 = channel_quantization(target, peaks, rel(8))
    # one 10-bit PQ code at 100 nits ≈ 1 % of light; σ = step/√12 per channel
    assert 0.0015 < q10[1] < 0.0035
    assert 0.0003 < q10[0] < 0.0010                           # ~0.0006 xy — the PA's observed scatter
    assert q8[0] == pytest.approx(4.0 * q10[0], rel=0.1)      # 4× coarser steps at 8-bit


def test_panel_floor_reads_the_settled_wander_of_the_latest_track():
    ta = {"measure:raw": {"evidence": {"track": {"span_x": 0.0004, "tail_span_x": 0.00015,
                                                 "luminance_nits": [90.0, 90.09]}}},
          "measure:post-mhc": {"evidence": {"available": False}}}
    f = panel_floor_from_thermal(ta)
    # luminance endpoints include the warm-in → scaled to the settled fraction (tail ÷ span)
    assert f.drift_xy == pytest.approx(0.00015)
    assert f.drift_rel == pytest.approx(0.001 * 0.00015 / 0.0004)
    assert f.source == "thermal_align:measure:raw"
    assert panel_floor_from_thermal(None) == PanelFloor()
