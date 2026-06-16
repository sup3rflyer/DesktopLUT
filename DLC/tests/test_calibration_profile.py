"""Tests for the calibration profile (``dlc.calibration_profile``).

The profile is the skill ⊥ user-data boundary. Most of it is pure stdlib (lookups,
staleness, YAML loading); only the engine ``Target``/``Transfer`` builders need the
numpy/colour engine, so those are guarded with ``importorskip``.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from dlc import calibration_profile as cp


# ---------------------------------------------------------------------------
# synthetic profile + lookups
# ---------------------------------------------------------------------------

def test_synthetic_profile_lookups():
    p = cp.Profile.synthetic()
    d = p.display_for(0)
    assert d.primary is True
    assert d.argyll_display == 1
    assert d.temperamental_channel == "B"
    assert d.settle_delta_de == 0.3
    assert p.primary_display().name == d.name
    assert p.target_for(0, "SDR").name == "srgb_g22"
    assert p.target_for(0, "SDR").luminance_nits == 120.0


def test_display_for_unknown_monitor_raises():
    p = cp.Profile.synthetic(monitor=0)
    with pytest.raises(KeyError):
        p.display_for(5)


def test_target_white_is_numeric_d65_by_default():
    p = cp.Profile.synthetic()
    assert p.target("srgb_g22").white_xy() == cp.D65_XY


def test_white_xy_override_takes_precedence():
    spec = cp.TargetSpec(name="t", white_xy_override=(0.308, 0.325))
    assert spec.white_xy() == (0.308, 0.325)


def test_hdr_target_uses_peak_luminance():
    p = cp.Profile.synthetic()
    hdr = p.target("rec2020_pq")
    assert hdr.is_hdr is True
    assert hdr.luminance_nits == 1600.0


# ---------------------------------------------------------------------------
# SPD-correction staleness (tell, don't ask)
# ---------------------------------------------------------------------------

def test_staleness_fresh_is_not_stale():
    p = cp.Profile.synthetic(correction_made="2026-05-01")
    v = p.correction_staleness(today=datetime.date(2026, 6, 16))
    assert v.has_correction is True
    assert v.stale is False
    assert v.age_days == 46


def test_staleness_old_is_stale_and_refreshable():
    p = cp.Profile.synthetic(correction_made="2025-01-01")
    v = p.correction_staleness(today=datetime.date(2026, 6, 16))
    assert v.stale is True
    assert v.refreshable is True
    assert "old" in v.message


def test_staleness_no_correction():
    p = cp.Profile(
        meter=cp.MeterConfig(correction=cp.CorrectionInfo(file=None)),
        displays=(cp.DisplayConfig(name="d", desktoplut_monitor=0, argyll_display=1, sdr_target="t"),),
        targets={"t": cp.TargetSpec(name="t")},
    )
    v = p.correction_staleness(today=datetime.date(2026, 6, 16))
    assert v.has_correction is False
    assert v.stale is False  # nothing to be stale; it's a "no correction" tell, not a gate


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

_YAML = """
meter:
  model: Test Meter
  argyll_port: 2
  correction:
    file: corr.ccmx
    made: 2026-01-15
    max_age_days: 90
    spectrometer_available: true
displays:
  - name: Panel One
    desktoplut_monitor: 0
    argyll_display: 1
    primary: true
    panel: {tech: mini-LED, bit_depth: 10}
    sdr_target: srgb_g22_120
    hdr_target: null
    quirks: {temperamental_channel: blue, settle_delta_de: 0.3}
targets:
  srgb_g22_120:
    colorspace: Rec.709
    transfer: {type: power, gamma: 2.2}
    white: {intent: D65, method: spd_crt_like, correction_strength: 0.0}
    white_luminance_nits: 120
quality:
  avg_de2000: 1.2
  max_de2000: 4.0
paths:
  output: results
"""


def test_load_profile_round_trips(tmp_path: Path):
    path = tmp_path / "calibration_profile.yaml"
    path.write_text(_YAML, encoding="utf-8")
    p = cp.load_profile(path)
    assert p.meter.argyll_port == 2
    assert p.meter.correction.max_age_days == 90
    d = p.display_for(0)
    assert d.name == "Panel One"
    assert d.temperamental_channel == "B"
    assert d.sdr_target == "srgb_g22_120"
    t = p.target("srgb_g22_120")
    assert t.transfer_type == "power" and t.gamma == 2.2
    assert t.white.method == "spd_crt_like"
    assert p.quality.avg_de2000 == 1.2
    assert p.source_path == str(path)


def test_load_profile_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        cp.load_profile(tmp_path / "nope.yaml")


def test_load_profile_no_displays_is_invalid(tmp_path: Path):
    path = tmp_path / "p.yaml"
    path.write_text("targets:\n  t: {colorspace: Rec.709}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cp.load_profile(path)


# ---------------------------------------------------------------------------
# engine builders (numpy/colour) — guarded
# ---------------------------------------------------------------------------

def test_engine_target_and_transfer():
    pytest.importorskip("numpy")
    pytest.importorskip("colour")
    p = cp.Profile.synthetic(sdr_nits=120.0)
    target = p.engine_target("srgb_g22")
    assert target.transfer == "power"
    assert target.gamma == 2.2
    assert target.peak_nits == 120.0
    assert target.white_xy == cp.D65_XY
    transfer = p.transfer_for("srgb_g22", bit_depth=8)
    assert transfer.kind == "power"
    assert transfer.bit_depth == 8

    hdr = p.engine_target("rec2020_pq")
    assert hdr.transfer == "pq"
