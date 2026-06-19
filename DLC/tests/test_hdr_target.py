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
# choose_peak_nits — the 200-step ladder, ceiling-bounded
# ---------------------------------------------------------------------------

def test_peak_defaults_conservatively_to_1600():
    peak, prov = ht.choose_peak_nits(native_white_nits=1840.0)
    assert peak == 1600.0
    assert prov["source"] == "default"
    # The brief read would *allow* 1800, but we don't bake that without a warm capture.
    assert prov["would_allow"] == 1800.0


def test_peak_default_clamps_to_a_low_ceiling():
    # A panel that only reaches 1100 nits brief can't sustain 1600 → clamp to 1000.
    peak, prov = ht.choose_peak_nits(native_white_nits=1100.0)
    assert peak == 1000.0
    assert prov["source"] == "default"


def test_peak_from_sustained_capture_picks_highest_rung():
    peak, prov = ht.choose_peak_nits(sustained_peak_nits=1810.0)
    assert peak == 1800.0
    assert prov["source"] == "sustained"


def test_peak_sustained_below_top_rung():
    peak, _ = ht.choose_peak_nits(sustained_peak_nits=1550.0)
    assert peak == 1400.0


def test_peak_pinned_is_honored_then_ceiling_clamped():
    assert ht.choose_peak_nits(pinned_peak_nits=1000.0, native_white_nits=1840.0)[0] == 1000.0
    # A pin above the measured ceiling is clamped down.
    peak, prov = ht.choose_peak_nits(pinned_peak_nits=2000.0, native_white_nits=1840.0)
    assert peak == 1840.0 and "clamped" in prov["note"]


def test_peak_never_exceeds_a_sub_ladder_ceiling():
    # A panel below the 1000-nit ladder floor must NOT be handed an unreachable 1000-nit
    # target (the old min(ladder) fallback) — it targets its own measured ceiling.
    peak, prov = ht.choose_peak_nits(native_white_nits=800.0)
    assert peak == 800.0 and peak <= 800.0
    peak2, prov2 = ht.choose_peak_nits(sustained_peak_nits=900.0)
    assert peak2 == 900.0 and prov2["below_ladder"] is True
    # End to end: the resolved target peak is reachable, not above the panel.
    assert ht.resolve_hdr_target(white_xy=D65, native_white_nits=800.0).peak_nits == 800.0


def test_peak_ignores_non_positive_pin_and_ceiling():
    # A 0/negative peak (e.g. an unfilled YAML field) is ignored, not treated as a ceiling.
    assert ht.choose_peak_nits(pinned_peak_nits=0.0, native_white_nits=1840.0)[0] == 1600.0
    assert ht.choose_peak_nits(native_white_nits=-5.0)[0] == 1600.0


# ---------------------------------------------------------------------------
# resolve_hdr_target — the assembled target + the knee
# ---------------------------------------------------------------------------

def test_1600_peak_on_an_1840_panel_has_no_rolloff():
    # gain ~1.064, native 1840 ⇒ knee ~1729 > 1600 ⇒ whole range boostable.
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0, eotf_undershoot=-0.06)
    assert tgt.peak_nits == 1600.0
    assert not tgt.has_rolloff
    assert tgt.knee_start_nits == tgt.peak_nits


def test_1800_peak_with_undershoot_rolls_off_below_peak():
    # Pin 1800: gain ~1.064 ⇒ knee = 1840/1.064 ≈ 1729 < 1800 ⇒ roll-off in [1729, 1800].
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0,
                                pinned_peak_nits=1800.0, eotf_undershoot=-0.06)
    assert tgt.peak_nits == 1800.0
    assert tgt.has_rolloff
    assert math.isclose(tgt.knee_start_nits, 1840.0 / (1.0 / 0.94), rel_tol=1e-6)


def test_no_undershoot_means_knee_at_peak():
    tgt = ht.resolve_hdr_target(white_xy=D65, native_white_nits=1840.0)
    assert tgt.undershoot_gain == 1.0
    assert tgt.knee_start_nits == tgt.peak_nits


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
    dip = DisplayInstrumentProfile(display="Synthetic mini-LED", mode="HDR",
                                   native_white_nits=1840.0, eotf_undershoot=-0.06)
    tgt = ht.resolve_from_dip(dip, white_xy=D65)
    assert tgt.peak_nits == 1600.0
    assert math.isclose(tgt.undershoot_gain, 1.0 / 0.94, rel_tol=1e-9)


def test_resolve_from_dip_none_is_a_cold_start():
    # No characterization yet: only the fixed white + the default peak are known.
    tgt = ht.resolve_from_dip(None, white_xy=D65)
    assert tgt.peak_nits == ht.DEFAULT_TARGET_PEAK_NITS
    assert tgt.undershoot_gain == 1.0


def test_resolve_from_dip_uses_sustained_field_when_present():
    # The warm-capture field isn't on the DIP yet; getattr must pick it up once it is.
    dip = DisplayInstrumentProfile(display="d", mode="HDR", native_white_nits=1840.0)
    dip.sustained_peak_nits = 1810.0  # the warm-capture field isn't declared yet
    tgt = ht.resolve_from_dip(dip, white_xy=D65)
    assert tgt.peak_nits == 1800.0
