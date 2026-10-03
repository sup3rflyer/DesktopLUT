"""Tests for ``dlc.sdr_in_hdr`` — SDR content on an HDR display: Windows' sRGB composition at the SDR
white level, Desktop Gamma's 80-nit bake, the grey tone-model fit, and the SDR-white-level lookup."""

from __future__ import annotations

import pytest

from dlc import sdr_in_hdr as sih


def test_desktop_gamma_forecast():
    for c in (3, 24, 128, 216, 255):
        s = c / 255
        assert sih.srgb_oetf(sih.srgb_eotf(s)) == pytest.approx(s, abs=1e-12)
        # Desktop Gamma referenced to an 80-nit SDR white is exactly pure 2.2
        assert sih.grey_forecast_rel(s, 80.0, True) == pytest.approx(s ** 2.2, rel=1e-9)
        assert sih.grey_forecast_rel(s, 116.0, False) == pytest.approx(sih.srgb_eotf(s), rel=1e-12)
    # at a 116-nit white the 80-nit bake leaves part of the sRGB shadow lift: code 24 -> 0.7413 nit (2.2: 0.6405)
    assert 116.0 * sih.grey_forecast_rel(24 / 255, 116.0, True) == pytest.approx(0.7413, abs=2e-4)
    assert sih.grey_forecast_rel(230 / 255, 116.0, True) == pytest.approx(sih.srgb_eotf(230 / 255))   # > 80 nit


def _greys(model, white=116.0):
    return [(c / 255, white * model(c / 255)) for c in (3, 8, 16, 24, 32, 48, 64, 96, 128, 160, 192, 224, 255)]


def test_grey_model_fit_names_the_model_the_greys_follow():
    for name, model in (("g22", lambda s: s ** 2.2), ("srgb", sih.srgb_eotf),
                        ("dg80_forecast", lambda s: sih.grey_forecast_rel(s, 116.0, True))):
        fit = sih.grey_model_fit(_greys(model), white_y=116.0, declared_white=116.0)
        assert fit["closest"] == name and fit["models"][name]["rms_ln"] < 1e-9
    fit = sih.grey_model_fit(_greys(lambda s: s ** 2.2), white_y=116.0, declared_white=None)
    assert "dg80_forecast" not in fit["models"]                      # no declared white: no forecast
    assert all(r["Y"] >= sih.FIT_FLOOR_NITS and r["code_signal"] < 1 for r in fit["per_grey"])
    assert sih.grey_model_fit([], white_y=116.0, declared_white=116.0)["note"] == "no usable greys"


def test_sdr_white_matches_the_monitor_rect():
    levels = [{"gdi": "A", "position": [0, 0], "size": [3840, 2160], "nits": 116.0},
              {"gdi": "B", "position": [-3840, -212], "size": [3840, 2160], "nits": 80.0}]
    assert sih.sdr_white_for_rect(levels, {"x": -3840, "y": -212, "width": 3840, "height": 2160})["gdi"] == "B"
    assert sih.sdr_white_for_rect(levels, {"x": 5, "y": 0, "width": 3840, "height": 2160}) is None
    assert sih.sdr_white_for_rect(levels, None) is None


def test_probe_sdr_white_reports_no_match(monkeypatch):
    monkeypatch.setattr(sih, "windows_sdr_white_levels", lambda: [])
    rec = sih.probe_sdr_white({"x": 0, "y": 0, "width": 10, "height": 10})
    assert rec["nits"] is None and rec["reason"] == "DisplayConfig unavailable"
    monkeypatch.setattr(sih, "windows_sdr_white_levels",
                        lambda: [{"gdi": "A", "position": [0, 0], "size": [10, 10], "nits": 116.0}])
    rec = sih.probe_sdr_white({"x": 0, "y": 0, "width": 10, "height": 10})
    assert rec["nits"] == 116.0 and rec["source"] == "displayconfig_sdr_white_level"
