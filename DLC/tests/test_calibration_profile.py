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


# ---------------------------------------------------------------------------
# resolve_white — the white-point resolver (HANDOFF item 7)
# ---------------------------------------------------------------------------

def _fake_white_fn(spd_file, *, strength, observer, anchor):
    """A deterministic SPD→white stand-in (no numpy/colour) for the resolver tests —
    returns a fixed cooler "CRT-like" white and echoes the dials."""
    return {"xy": (0.3080, 0.3250), "cct": 6800.0, "duv": 0.003,
            "observer": observer, "anchor": anchor}


def test_resolve_white_numeric_by_default():
    p = cp.Profile.synthetic()
    r = p.resolve_white(0, "srgb_g22")
    assert r.provenance == "numeric"
    assert r.xy == cp.D65_XY
    assert r.observer == "2015_2" and r.anchor == "reference"


def test_resolve_white_spd_path_uses_injected_fn(tmp_path):
    spd = tmp_path / "white.sp"
    spd.write_text("stub", encoding="utf-8")
    p = cp.Profile.synthetic(white_method="spd_crt_like", white_strength=0.5, white_spd=str(spd))
    r = p.resolve_white(0, "srgb_g22", white_fn=_fake_white_fn)
    assert r.provenance == "spd_crt_like"
    assert r.xy == (0.3080, 0.3250)
    assert r.cct == 6800.0 and r.strength == 0.5
    assert r.spd_file == str(spd)


def test_resolve_white_strength_zero_is_numeric(tmp_path):
    spd = tmp_path / "white.sp"
    spd.write_text("stub", encoding="utf-8")
    # spd_crt_like method but strength 0 ⇒ numeric D65 (no SPD load, no fn call).
    p = cp.Profile.synthetic(white_method="spd_crt_like", white_strength=0.0, white_spd=str(spd))
    r = p.resolve_white(0, "srgb_g22", white_fn=_fake_white_fn)
    assert r.provenance == "numeric"
    assert r.xy == cp.D65_XY


def test_resolve_white_falls_back_when_no_spd():
    # spd_crt_like + strength>0 but no SPD on hand ⇒ graceful numeric fallback, not a crash.
    p = cp.Profile.synthetic(white_method="spd_crt_like", white_strength=0.7, white_spd=None)
    r = p.resolve_white(0, "srgb_g22", white_fn=_fake_white_fn)
    assert r.provenance == "numeric"
    assert r.xy == cp.D65_XY
    assert "no white SPD" in r.note


def test_resolve_white_falls_back_when_spd_missing(tmp_path):
    p = cp.Profile.synthetic(white_method="spd_crt_like", white_strength=0.7,
                             white_spd=str(tmp_path / "absent.sp"))
    r = p.resolve_white(0, "srgb_g22", white_fn=_fake_white_fn)
    assert r.provenance == "numeric"
    assert "not found" in r.note


def test_resolve_white_override_beats_spd(tmp_path):
    spd = tmp_path / "white.sp"
    spd.write_text("stub", encoding="utf-8")
    spec = cp.TargetSpec(name="t", white_xy_override=(0.310, 0.331),
                         white=cp.WhiteSpec(method="spd_crt_like", correction_strength=1.0))
    p = cp.Profile(meter=cp.MeterConfig(),
                   displays=(cp.DisplayConfig(name="d", desktoplut_monitor=0, argyll_display=1,
                                              sdr_target="t", white_spd=str(spd)),),
                   targets={"t": spec})
    r = p.resolve_white(0, "t", white_fn=_fake_white_fn)
    assert r.provenance == "override"
    assert r.xy == (0.310, 0.331)


def test_white_point_resolution_round_trips():
    r = cp.WhitePointResolution(xy=(0.308, 0.325), provenance="spd_crt_like", method="spd_crt_like",
                                strength=0.5, observer="2015_2", anchor="reference",
                                spd_file="w.sp", cct=6800.0, duv=0.003, note="x")
    assert cp.WhitePointResolution.from_dict(r.as_dict()).xy == r.xy
    assert cp.WhitePointResolution.from_dict(r.as_dict()).provenance == "spd_crt_like"


# ---------------------------------------------------------------------------
# staleness with a store-supplied made date (made_override)
# ---------------------------------------------------------------------------

def test_staleness_made_override_supersedes_profile():
    # Profile says the correction is fresh, but the store records an older real date.
    p = cp.Profile.synthetic(correction_made="2026-06-01")
    v = p.correction_staleness(today=datetime.date(2026, 6, 16), made_override="2025-01-01")
    assert v.made == "2025-01-01"
    assert v.stale is True


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
    white_spd: white.sp
    probe_match: {display_tech: s, colorimeter_display_type: n, spectro_port: 2, display_name: PA32UCXR}
    quirks: {temperamental_channel: blue, settle_delta_de: 0.3}
targets:
  srgb_g22_120:
    colorspace: Rec.709
    transfer: {type: power, gamma: 2.2}
    white: {intent: D65, method: spd_crt_like, correction_strength: 0.5, observer: "1964_10", anchor: legacy}
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
    assert d.white_spd == "white.sp"
    assert d.probe_match.display_tech == "s"
    assert d.probe_match.colorimeter_display_type == "n"
    assert d.probe_match.spectro_port == 2
    assert d.probe_match.display_name == "PA32UCXR"
    t = p.target("srgb_g22_120")
    assert t.transfer_type == "power" and t.gamma == 2.2
    assert t.white.method == "spd_crt_like"
    assert t.white.correction_strength == 0.5
    assert t.white.observer == "1964_10" and t.white.anchor == "legacy"


def test_observer_normalization_repairs_unquoted_yaml():
    # YAML 1.1 parses an unquoted `observer: 2015_2` as the int 20152 (underscore = digit
    # separator); the loader must repair it to the canonical id, not pass a bogus observer to
    # the white solver (M7). A genuinely unknown observer fails loudly.
    assert cp._normalize_observer("2015_2") == "2015_2"
    assert cp._normalize_observer(20152) == "2015_2"
    assert cp._normalize_observer(196410) == "1964_10"
    assert cp._normalize_observer(19312) == "1931_2"
    with pytest.raises(ValueError):
        cp._normalize_observer("nonsense")


def test_unquoted_observer_in_yaml_is_repaired(tmp_path: Path):
    # The live landmine: a profile edit that drops the quotes around the observer id.
    yaml = _YAML.replace('observer: "1964_10"', "observer: 1964_10")
    path = tmp_path / "calibration_profile.yaml"
    path.write_text(yaml, encoding="utf-8")
    p = cp.load_profile(path)
    assert p.target("srgb_g22_120").white.observer == "1964_10"


def test_profile_patches_block_loads(tmp_path: Path):
    # Durable per-profile run-size preference: a `patches:` block is carried verbatim as a dict
    # (the orchestrator/CLI builds PatchSizes from it). Absent block ⇒ empty dict.
    yaml = _YAML + "patches:\n  raw_ramp_steps: 33\n  volumetric_mode: cube\n  cube_size: 13\n"
    path = tmp_path / "calibration_profile.yaml"
    path.write_text(yaml, encoding="utf-8")
    p = cp.load_profile(path)
    assert p.patches == {"raw_ramp_steps": 33, "volumetric_mode": "cube", "cube_size": 13}

    plain = tmp_path / "plain.yaml"
    plain.write_text(_YAML, encoding="utf-8")
    assert cp.load_profile(plain).patches == {}
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
