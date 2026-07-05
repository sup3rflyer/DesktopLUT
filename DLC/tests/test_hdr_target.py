"""Unit tests for the HDR target consumer (dlc.hdr_target) — the deterministic choice
of peak / undershoot gain / knee / fixed white from the measured DIP, per
docs/hdr-target-design.md. Pure functions; no numpy, no hardware."""

from __future__ import annotations

import math

from dlc import hdr_target as ht
from dlc.dip import DisplayInstrumentProfile

D65 = (0.3127, 0.3290)


# ---------------------------------------------------------------------------
# undershoot_gain — first-order boost toward PQ, clamped both ends
# ---------------------------------------------------------------------------

def test_undershoot_gain_none_and_zero_are_unity():
    assert ht.undershoot_gain(None) == 1.0
    assert ht.undershoot_gain(0.0) == 1.0


def test_undershoot_gain_lifts_a_negative_undershoot():
    # −6% undershoot ⇒ ~1.064× boost (1 / (1 − 0.06)).
    assert math.isclose(ht.undershoot_gain(-0.06), 1.0 / 0.94, rel_tol=1e-9)


def test_undershoot_gain_overshoot_needs_no_boost():
    # A panel that overshoots PQ (u > 0) is pulled down per-node by the cube, not here.
    assert ht.undershoot_gain(0.05) == 1.0


def test_undershoot_gain_is_clamped_for_an_implausible_dip():
    # A −80% undershoot would ask for a 5× boost — rejected to the max rather than
    # driving the panel into clipping.
    assert ht.undershoot_gain(-0.8) == ht.MAX_UNDERSHOOT_GAIN
    assert ht.undershoot_gain(-1.0) == ht.MAX_UNDERSHOOT_GAIN  # denom <= 0 guard


# ---------------------------------------------------------------------------
# choose_peak_nits — calibrate to the MAX-SUSTAINED peak (owner 2026-06-24)
# ---------------------------------------------------------------------------

def test_peak_from_sustained_capture_is_used_directly():
    # The calibration peak is the max-sustained value VERBATIM — NOT rounded to a viewing
    # ladder rung (the ladder moved to DesktopLUT's tonemap).
    peak, prov = ht.choose_peak_nits(sustained_peak_nits=1810.0, native_white_nits=1840.0)
    assert peak == 1810.0
    assert prov["source"] == "sustained"
    assert prov["sustained_unknown"] is False and prov["grounded"] is True


def test_peak_sustained_is_clamped_to_the_native_ceiling():
    # A warm capture above the measured native ceiling is a bad read — clamp to the ceiling.
    peak, prov = ht.choose_peak_nits(sustained_peak_nits=1900.0, native_white_nits=1840.0)
    assert peak == 1840.0 and "clamped" in prov["note"]


def test_peak_falls_back_to_native_ceiling_and_flags_sustained_unknown():
    # No warm capture yet → calibrate to the measured raw ceiling, but FLAG it (the brief
    # flash may not hold under sustained load). This is the new behaviour vs the old cons. 1600.
    peak, prov = ht.choose_peak_nits(native_white_nits=1840.0)
    assert peak == 1840.0
    assert prov["source"] == "native_ceiling"
    assert prov["sustained_unknown"] is True and prov["grounded"] is True


def test_peak_native_fallback_at_a_low_ceiling():
    # A panel that only reaches 1100 nits → calibrate to 1100 (flagged), not a round rung.
    peak, prov = ht.choose_peak_nits(native_white_nits=1100.0)
    assert peak == 1100.0 and prov["sustained_unknown"] is True


def test_peak_pinned_override_is_honored_then_ceiling_clamped():
    # The explicit calibration-peak override (NOT the profile's viewing peak) still works.
    peak, prov = ht.choose_peak_nits(pinned_peak_nits=1000.0, native_white_nits=1840.0)
    assert peak == 1000.0 and prov["source"] == "pinned" and prov["grounded"] is True
    # A pin above the measured ceiling is clamped down.
    peak, prov = ht.choose_peak_nits(pinned_peak_nits=2000.0, native_white_nits=1840.0)
    assert peak == 1840.0 and "clamped" in prov["note"]


def test_peak_cold_start_placeholder_is_flagged_ungrounded():
    # Nothing measured (no DIP) → the placeholder, explicitly flagged as resting on no measurement.
    peak, prov = ht.choose_peak_nits()
    assert peak == ht.DEFAULT_TARGET_PEAK_NITS
    assert prov["source"] == "cold_start_placeholder"
    assert prov["grounded"] is False and prov["sustained_unknown"] is True


def test_peak_ignores_non_positive_pin_and_ceiling():
    # A 0/negative peak (e.g. an unfilled YAML field) is ignored, not treated as a ceiling/pin.
    # pin=0 ignored, native present → calibrate to native (flagged), not the placeholder.
    assert ht.choose_peak_nits(pinned_peak_nits=0.0, native_white_nits=1840.0)[0] == 1840.0
    # native invalid AND nothing else → cold-start placeholder.
    peak, prov = ht.choose_peak_nits(native_white_nits=-5.0)
    assert peak == ht.DEFAULT_TARGET_PEAK_NITS and prov["grounded"] is False


# ---------------------------------------------------------------------------
# resolve_hdr_target — the assembled target + the knee
# ---------------------------------------------------------------------------

def test_sustained_peak_with_undershoot_has_no_rolloff_below_it():
    # Max-sustained 1700 on an 1840 panel, gain ~1.064 ⇒ knee = 1840/1.064 ≈ 1729 > 1700 ⇒
    # the whole [0, 1700] range is boostable (the roll-off begins above the sustained peak).
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0,
                                sustained_peak_nits=1700.0, eotf_undershoot=-0.06)
    assert tgt.peak_nits == 1700.0
    assert not tgt.has_rolloff
    assert tgt.knee_start_nits == tgt.peak_nits


def test_native_ceiling_peak_with_undershoot_rolls_off_at_the_top():
    # No warm capture → calibrate to the native ceiling 1840; gain ~1.064 ⇒ knee = 1840/1.064 ≈
    # 1729 < 1840 ⇒ a short roll-off at the very top (you can't boost a panel at max drive).
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0, eotf_undershoot=-0.06)
    assert tgt.peak_nits == 1840.0
    assert tgt.has_rolloff
    assert math.isclose(tgt.knee_start_nits, 1840.0 / (1.0 / 0.94), rel_tol=1e-6)


def test_no_undershoot_means_knee_at_peak():
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0)
    assert tgt.peak_nits == 1840.0          # native-ceiling fallback (sustained unknown)
    assert tgt.undershoot_gain == 1.0
    assert tgt.knee_start_nits == tgt.peak_nits


def test_resolve_target_peak_is_reachable_on_a_sub_ladder_panel():
    # End to end: a dim panel resolves to its own measured ceiling, never above it.
    assert ht.resolve_hdr_target(white_xy=D65, native_white_nits=800.0).peak_nits == 800.0


def test_white_is_carried_verbatim_and_serializes():
    tgt = ht.resolve_hdr_target(white_xy=(0.308, 0.325), native_white_nits=1840.0)
    assert tgt.white_xy == (0.308, 0.325)
    d = tgt.as_dict()
    assert d["white_xy"] == [0.308, 0.325]
    assert d["container_nits"] == ht.PQ_CONTAINER_NITS
    assert "peak" in d["provenance"] and "white" in d["provenance"]


# ---------------------------------------------------------------------------
# resolve_from_dip — defensive read of a (possibly partial / absent) DIP
# ---------------------------------------------------------------------------

def test_resolve_from_dip_reads_measured_fields():
    # No warm capture → native-ceiling fallback (flagged), plus the measured undershoot gain.
    dip = DisplayInstrumentProfile(display="Synthetic mini-LED", mode="HDR",
                                   native_white_nits=1840.0, eotf_undershoot=-0.06)
    tgt = ht.resolve_from_dip(dip, white_xy=D65)
    assert tgt.peak_nits == 1840.0
    assert tgt.provenance["peak"]["sustained_unknown"] is True
    assert math.isclose(tgt.undershoot_gain, 1.0 / 0.94, rel_tol=1e-9)


def test_resolve_from_dip_none_is_a_cold_start():
    # No characterization yet: only the fixed white + the placeholder peak are known.
    tgt = ht.resolve_from_dip(None, white_xy=D65)
    assert tgt.peak_nits == ht.DEFAULT_TARGET_PEAK_NITS
    assert tgt.provenance["peak"]["grounded"] is False
    assert tgt.undershoot_gain == 1.0


def test_resolve_from_dip_uses_sustained_field_when_present():
    # The warm-capture sustained_peak_nits is the calibration peak (used directly, clamped to native).
    dip = DisplayInstrumentProfile(display="d", mode="HDR", native_white_nits=1840.0,
                                   sustained_peak_nits=1700.0)
    tgt = ht.resolve_from_dip(dip, white_xy=D65)
    assert tgt.peak_nits == 1700.0
    assert tgt.provenance["peak"]["source"] == "sustained"


# ---------------------------------------------------------------------------
# Fable audit Phase 2 — defensive-input + provenance-honesty pins
# ---------------------------------------------------------------------------

def test_negative_native_ceiling_never_produces_a_negative_knee():
    # A corrupt DIP with a negative native_white_nits must be normalized away for the
    # KNEE exactly as choose_peak_nits normalizes it for the peak — before the fix the
    # raw value leaked into `native / gain` and yielded a negative knee_start_nits.
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=-5.0,
                                eotf_undershoot=-0.06)
    assert tgt.knee_start_nits == tgt.peak_nits          # no ceiling ⇒ knee at peak
    assert not tgt.has_rolloff
    assert tgt.knee_start_nits > 0


def test_clamped_gain_is_flagged_in_provenance():
    # MAX_UNDERSHOOT_GAIN's contract is "clamp AND flag": an implausible measured
    # undershoot must be visible as a suspect characterization, not quoted as a
    # plausible 1.5x boost.
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1800.0,
                                eotf_undershoot=-0.45)
    u = tgt.provenance["undershoot"]
    assert math.isclose(tgt.undershoot_gain, ht.MAX_UNDERSHOOT_GAIN)
    assert u["clamped"] is True
    assert "CLAMPED" in u["note"]
    # ... and a non-positive denominator (undershoot <= -1) is also a clamp
    tgt2 = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1800.0,
                                 eotf_undershoot=-1.2)
    assert tgt2.provenance["undershoot"]["clamped"] is True


def test_ordinary_gain_is_not_flagged_as_clamped():
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0,
                                eotf_undershoot=-0.06)
    u = tgt.provenance["undershoot"]
    assert u["clamped"] is False
    assert "CLAMPED" not in u["note"]
    tgt_none = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0)
    assert tgt_none.provenance["undershoot"]["clamped"] is False
