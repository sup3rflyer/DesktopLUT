"""Unit tests for the adaptive-planning evidence + validator + investigator tools
(#47/#49). The LLM is the adjudicator; these test the *guards* around it — the bounds
validator (which must never let a bad decision break the run or touch the foundation),
the investigator analysis functions, and the conservative autonomous fallback."""

from __future__ import annotations

import json
from pathlib import Path

from dlc import patch_evidence as pe
from dlc.dip import DisplayInstrumentProfile, NoiseBand
from dlc.engine.patches import Transfer
from dlc.mhc import Ti3Sample

_BASE = {"tube_size": 33, "cube_size": 9, "tube_radius": 2,
         "low_light_steps": 9, "low_light_cube_size": 5}


# ---------------------------------------------------------------------------
# validate_decision — the guard: clamps/drops, never breaks, never touches foundation
# ---------------------------------------------------------------------------

def test_validate_maps_tiers_to_knobs():
    knobs, norm = pe.validate_decision(
        {"shadow_treatment": "extra", "volumetric_density": "denser",
         "reason": "r", "confidence": "high"}, _BASE)
    assert knobs["low_light_steps"] == 9 + 6
    assert knobs["tube_size"] == 33 + 8 and knobs["cube_size"] == 9 + 2
    assert norm["adjustments"] == []
    assert norm["confidence"] == "high"


def test_validate_rejects_foundation_knobs():
    # The ICC/raw foundation is NOT adaptable — an override of a raw_* knob is dropped.
    knobs, norm = pe.validate_decision(
        {"volumetric_density": "custom",
         "patch_size_overrides": {"raw_ramp_steps": 99, "tube_size": 41}}, _BASE)
    assert "raw_ramp_steps" not in knobs
    assert knobs["tube_size"] == 41
    assert any("raw_ramp_steps" in a for a in norm["adjustments"])


def test_validate_clamps_out_of_bounds_overrides():
    knobs, norm = pe.validate_decision(
        {"volumetric_density": "custom",
         "patch_size_overrides": {"cube_size": 500, "low_light_signal": 9.0}}, _BASE)
    assert knobs["cube_size"] == 33                 # KNOB_BOUNDS cube_size hi
    assert knobs["low_light_signal"] == 0.40        # clamped to hi
    assert any("clamped" in a for a in norm["adjustments"])


def test_validate_garbage_tiers_fall_back_to_standard():
    knobs, norm = pe.validate_decision(
        {"shadow_treatment": "ultra", "volumetric_density": "mega"}, _BASE)
    assert norm["shadow_treatment"] == "standard" and norm["volumetric_density"] == "standard"
    assert knobs == {}
    assert len(norm["adjustments"]) == 2


def test_validate_categorical_knob_must_be_in_set():
    knobs, norm = pe.validate_decision(
        {"volumetric_density": "custom",
         "patch_size_overrides": {"volumetric_mode": "tube", "grid_type": "nonsense"}}, _BASE)
    assert knobs["volumetric_mode"] == "tube"
    assert "grid_type" not in knobs
    assert any("grid_type" in a for a in norm["adjustments"])


def test_validate_empty_decision_is_a_noop():
    knobs, norm = pe.validate_decision({}, _BASE)
    assert knobs == {}
    assert norm["shadow_treatment"] == "standard" and norm["volumetric_density"] == "standard"


def test_sparser_clamps_at_floors():
    assert pe.volumetric_knobs("sparser", {"tube_size": 9, "cube_size": 5, "tube_radius": 1}) == \
        {"tube_size": 9, "cube_size": 5}


# ---------------------------------------------------------------------------
# conservative_fallback — the autonomous guard (low confidence, never custom/sparser)
# ---------------------------------------------------------------------------

def test_fallback_clean_panel_is_standard():
    ev = {"dip": {"contrast": 900, "near_black_sigma_de": 0.1}, "gamut_overcoverage": 1.02,
          "raw_tone": {"available": True, "bumpiness": 0.005}, "mhc_residual": {"white_de_vs_d65": 0.4}}
    fb = pe.conservative_fallback(ev)
    assert fb["shadow_treatment"] == "standard" and fb["volumetric_density"] == "standard"
    assert fb["confidence"] == "low" and fb["source"] == "fallback"


def test_fallback_raised_black_and_wide_gamut():
    ev = {"dip": {"contrast": 80}, "gamut_overcoverage": 1.5,
          "raw_tone": {"available": False}, "mhc_residual": {}}
    fb = pe.conservative_fallback(ev)
    assert fb["shadow_treatment"] == "heavy"          # <100:1 → heavy
    assert fb["volumetric_density"] == "denser"
    assert fb["patch_size_overrides"] == {}           # never proposes custom


# ---------------------------------------------------------------------------
# Investigator analysis functions
# ---------------------------------------------------------------------------

def test_summarize_dip_none_and_present():
    assert pe.summarize_dip(None) == {"present": False}
    dip = DisplayInstrumentProfile(
        display="d", mode="SDR", native_white_nits=120.0, native_black_nits=0.5,
        noise_model=[NoiseBand(nits=120, sigma_de=0.05), NoiseBand(nits=0.2, sigma_de=0.4)])
    s = pe.summarize_dip(dip)
    assert s["present"] and s["contrast"] == 240
    assert s["noise_bands"][0]["nits"] == 0.2        # sorted ascending
    assert s["near_black_sigma_de"] == 0.4


def _gray_ramp(gamma: float, n: int = 16, glitch_at: int = -1) -> list:
    samples = []
    for i in range(1, n + 1):
        v = i / n
        y = v ** gamma
        if i == glitch_at:
            y = 1e-5                                   # a flaky below-floor read
        samples.append(Ti3Sample(rgb=(v, v, v), xyz=(0.95 * y, y, 1.08 * y)))
    return samples


def test_analyze_raw_ti3_clean_power_law_is_flat():
    out = pe.analyze_raw_ti3(_gray_ramp(2.4))
    assert out["available"] and out["bumpiness"] < 0.01
    assert abs(out["fit_gamma"] - 2.4) < 0.05         # gamma-agnostic (any power fits flat)


def test_analyze_raw_ti3_is_robust_to_a_glitch_read():
    out = pe.analyze_raw_ti3(_gray_ramp(2.2, glitch_at=7))
    assert out["available"] and out["bumpiness"] < 0.01
    assert out["rejected_outliers"] >= 1


def test_analyze_raw_ti3_too_short():
    assert pe.analyze_raw_ti3(_gray_ramp(2.2, n=5))["available"] is False
    assert pe.analyze_raw_ti3(None)["available"] is False


def test_gamut_overcoverage_wide_and_none():
    from dlc import gamut
    tgt = gamut.target_primaries("Rec.709")
    dip = DisplayInstrumentProfile(display="d", mode="SDR",
        native_primaries={"R": [0.68, 0.32], "G": [0.265, 0.69], "B": [0.15, 0.06]})
    assert pe.gamut_overcoverage(dip, tgt) > 1.3
    assert pe.gamut_overcoverage(None, tgt) is None
    assert pe.gamut_overcoverage(dip, None) is None


def test_estimate_and_compare_patch_plans():
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    base = pe.estimate_patch_plan({"tube_size": 5, "cube_size": 3}, t, flow="full", read_overhead_s=2.0)
    denser = pe.estimate_patch_plan({"tube_size": 13, "cube_size": 5}, t, flow="full", read_overhead_s=2.0)
    assert denser["total_patches"] > base["total_patches"]
    assert "est_minutes" in base
    delta = pe.compare_patch_plans(base, denser)
    assert delta["total_delta"] == denser["total_patches"] - base["total_patches"]
    assert delta["seconds_delta"] is not None


def test_list_prior_runs(tmp_path: Path):
    for i, avg in enumerate([0.5, 0.9]):
        d = tmp_path / f"run{i}"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({
            "mode": "SDR",
            "desktoplut": {"display": "pa32", "flow": "full",
                           "calib": {"stages": {"verify": {"digest": {
                               "avg_de2000": avg, "within_quality": avg < 0.8}}}}}}), encoding="utf-8")
    runs = pe.list_prior_runs(tmp_path, "pa32")
    assert len(runs) == 2
    assert all(r["flow"] == "full" for r in runs)
    assert any(r["verify"]["avg_de2000"] == 0.5 for r in runs)
    assert pe.list_prior_runs(tmp_path, "other-display") == []


def test_gather_evidence_packet_shape():
    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    dip = DisplayInstrumentProfile(display="d", mode="SDR", native_white_nits=120.0,
                                   native_black_nits=1.0)   # 120:1 → raised → a flag
    ev = pe.gather_evidence(
        dip=dip, target_primaries=None, target_colorspace="Rec.709", raw_ti3=None,
        mhc_digest={"white_de_vs_d65": 0.3}, patch_sizes=_BASE, transfer=t, flow="full",
        prior_runs=[], cache_state={})
    assert "conservative_fallback" in ev and "worth_investigating" in ev
    assert any("raised black" in f for f in ev["worth_investigating"])
    assert ev["transfer"]["gamma"] == 2.2              # serialized for the CLI
    assert ev["current_plan"]["total_patches"] > 0
