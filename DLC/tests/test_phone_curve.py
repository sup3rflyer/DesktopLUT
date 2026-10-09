"""Offline tests for dlc.phone.curve: camera-curve self-calibration from simulated multi-exposure pixel pairs (known
Log-like camera with a black pedestal, read noise and a hard clip), the verify step's compression factor (the
2026-10-08 failure mode), persistence and pair sampling. No phone, display or clip files involved."""

from __future__ import annotations

import dataclasses
import json
import math

import numpy as np
import pytest

from dlc.phone import curve as cv
from dlc.phone.analysis import ToneCurve

BLACK, CLIP = 175.0, 800.0


def _code_of(E):
    """The simulated camera: ~230 codes per decade (69 per stop) above a toe, like the 10-08 takes."""
    return 230.0 * np.log10(np.asarray(E) + 0.002) + 650.0


def _g_true(c):
    return np.log10(10.0 ** ((np.asarray(c) - 650.0) / 230.0) - 0.002)


def _shoot(E, gain, rng):
    c = _code_of(E * gain) + rng.normal(0.0, 0.7, E.shape)
    c = np.maximum(c, BLACK + rng.normal(0.0, 0.7, E.shape))
    return np.minimum(np.round(c), CLIP)


@pytest.fixture(scope="module")
def pairs():
    rng = np.random.default_rng(3)
    E = 10.0 ** rng.uniform(-2.2, 0.8, 8000)
    return [cv.PairSet(_shoot(E, 0.1, rng), _shoot(E, 1.0, rng), 10.0, name="iso10x"),
            cv.PairSet(_shoot(E, 1.0, rng), _shoot(E, 1.0 / 8.0, rng), 1.0 / 8.0, name="iso8x-reversed"),
            cv.PairSet(_shoot(E, 0.1 / 3.6, rng), _shoot(E, 0.1, rng), 4.0, free=True, name="shutter")]


@pytest.fixture(scope="module")
def fit(pairs):
    return cv.fit_camera_curve(pairs, bits=10, name="sim")


def test_fit_recovers_known_curve(fit):
    c = fit.curve
    assert fit.clip["detected"] and abs(fit.clip["code"] - CLIP) <= 1.0
    assert fit.black["detected"] and abs(fit.black["code"] - BLACK) <= 3.0
    assert fit.flags == [] and c.monotonic
    assert c.clip_code == fit.clip["code"] and c.hi <= CLIP - 5.0 and c.lo >= BLACK + 25.0
    cc = np.linspace(max(c.lo, 230.0), c.hi, 200)
    err = c.g(cc) - (_g_true(cc) - _g_true(fit.gauge_code))
    assert np.max(np.abs(err)) < 0.006                     # < 1.4 % in E over the usable range
    assert abs(float(c.g(fit.gauge_code))) < 1e-9          # the gauge
    assert c.codes_per_stop(500.0) == pytest.approx(230.0 * math.log10(2.0), rel=0.03)
    assert fit.sigma_log10 < 0.01
    shutter = next(s for s in fit.sets if s["free"])
    assert shutter["ratio_fit"] == pytest.approx(3.6, rel=0.01)             # the free ratio is a real check
    assert all(abs(s["median_resid_log10"]) < 0.002 for s in fit.sets)
    assert fit.dropped["clipped"] > 0 and fit.dropped["below_min_code"] > 0  # counted, not silent
    assert fit.resid_by_code and all(abs(r["median_log10"]) < 0.005 for r in fit.resid_by_code)
    # evaluate / inverse / outside the range
    codes = np.array([250.0, 400.0, 650.0, 780.0])
    assert np.allclose(c.inverse(c(codes)), codes, atol=1e-6)
    assert c.inverse(c(400.0)) == pytest.approx(400.0, abs=1e-6)
    assert not c.in_range(c.lo - 10.0) and np.isfinite(c(c.lo - 10.0)) and c(c.lo - 10.0) < c(c.lo)
    assert list(c.saturated([700.0, CLIP])) == [False, True]
    anch = c.anchored(650.0, 100.0)
    assert anch(650.0) == pytest.approx(100.0) and anch(500.0) / anch(650.0) == pytest.approx(c(500.0) / c(650.0))


def test_verify_detects_compressing_curve(fit, pairs):
    ok = cv.verify_curve(fit.curve, pairs)
    assert ok.within_tol and ok.compression == pytest.approx(1.0, abs=0.01) and ok.flags == []
    assert all(s["compression"] == pytest.approx(1.0, abs=0.01) for s in ok.sets if not s["free"])
    assert next(s for s in ok.sets if s["free"])["measured_stops"] == pytest.approx(math.log2(3.6), abs=0.03)
    # an "old" curve whose slope is 1/0.87 too shallow (g scales with its coefficients) - the 10-08 failure mode
    old = dataclasses.replace(fit.curve, coef=tuple(0.87 * np.asarray(fit.curve.coef)), name="old")
    bad = cv.verify_curve(old, pairs)
    assert bad.compression == pytest.approx(0.87, abs=0.01)
    assert not bad.within_tol and "compresses_ratios" in bad.flags
    assert bad.by_code and all(b["compression"] == pytest.approx(0.87, abs=0.02) for b in bad.by_code)
    iso = next(s for s in bad.sets if s["name"] == "iso10x")
    assert iso["measured_stops"] == pytest.approx(0.87 * math.log2(10.0), abs=0.05)
    assert bad.dropped["clipped"] > 0


def test_persistence_legacy_and_tone(fit, tmp_path):
    c = fit.curve
    c2 = cv.CameraCurve.from_json(c.to_json(tmp_path / "curve.json"))
    cc = np.linspace(c.lo - 20.0, c.hi + 20.0, 50)
    assert np.allclose(c2(cc), c(cc)) and c2.clip_code == c.clip_code and c2.bits == 10
    assert c2.meta["path"].endswith("curve.json")
    # the 10-08 session's camera_curve2.json shape
    legacy = {"form": "log10(E) = g(code) cubic B-spline (knots), E relative; gauge g(650)=0",
              "knots": list(c.knots), "coef": list(c.coef), "lo": c.lo, "hi": c.hi, "n_eq": 123,
              "codes_per_decade": {"400": 230.0}}
    c3 = cv.CameraCurve.from_dict(json.loads(json.dumps(legacy)))
    assert np.allclose(c3(cc), c(cc)) and c3.meta["legacy"]["n_eq"] == 123 and c3.clip_code is None
    tone = c.to_tone()
    assert isinstance(tone, ToneCurve) and tone.kind == "table" and tone.bits == 10
    assert tone.sat_code == c.clip_code
    mid = np.linspace(c.lo, c.hi, 37)
    assert np.allclose(tone(mid), c(mid), rtol=2e-3)
    assert ToneCurve.from_dict(tone.to_dict())(500.0) == pytest.approx(tone(500.0))


def test_pairs_from_frames_filters_edges_and_follows_points():
    rng = np.random.default_rng(1)
    E = np.full((80, 120), 0.05)
    E[:, 60:] = 0.5                                        # one sharp edge at x = 60
    E[20:40, 10:30] = 2.0
    fa = _code_of(E * 0.1) + rng.normal(0, 0.3, E.shape)
    fb = _code_of(E) + rng.normal(0, 0.3, E.shape)
    ps = cv.pairs_from_frames(fa, fb, 10.0, n=4000, name="same-grid")
    assert len(ps) > 2000 and ps.meta["drop_texture"] > 0 and ps.ratio == 10.0
    true_b = _code_of(10.0 ** _g_true(ps.codes_a) * 10.0)
    assert np.median(np.abs(ps.codes_b - true_b)) < 1.5    # every kept pair is the same scene point
    # frame b shifted by (+7.5, -3) px: sample the same scene points through each frame's own geometry
    fb2 = np.roll(np.roll(fb, 8, axis=1), -3, axis=0)
    pts = np.c_[rng.uniform(15, 100, 3000), rng.uniform(10, 70, 3000)]
    ps2 = cv.pairs_from_frames(fa, fb2, 10.0, points_a=pts, points_b=pts + [8.0, -3.0], name="moved")
    assert len(ps2) > 1000
    assert np.median(np.abs(ps2.codes_b - _code_of(10.0 ** _g_true(ps2.codes_a) * 10.0))) < 1.5
    mask = np.ones(E.shape, bool)
    mask[:, :60] = False
    ps3 = cv.pairs_from_frames(fa, fb, 10.0, n=4000, mask_a=mask)
    assert ps3.meta["drop_mask"] > 0 and np.all(ps3.codes_b > _code_of(0.2))


def test_rgb_codes_from_yuv_inverts_bt2020_ncl():
    rgb = np.array([[0.8, 0.3, 0.1], [0.2, 0.5, 0.9], [0.5, 0.5, 0.5]])
    r, g, b = rgb.T
    y = 0.2627 * r + 0.6780 * g + 0.0593 * b
    cb, cr = (b - y) / 1.8814, (r - y) / 1.4746
    Y = (y * 876 + 64).reshape(1, 3)
    U = (cb * 896 + 512).reshape(1, 3)
    V = (cr * 896 + 512).reshape(1, 3)
    out = cv.rgb_codes_from_yuv(Y, U, V)
    assert out.shape == (3, 1, 3)
    assert np.allclose(out[:, 0, :].T, rgb * 876 + 64, atol=1e-6)
    sub = cv.rgb_codes_from_yuv(np.full((4, 6), 500.0), np.full((2, 3), 512.0), np.full((2, 3), 512.0))
    assert sub.shape == (3, 4, 6) and np.allclose(sub, 500.0)


def test_estimators_and_refusals():
    rng = np.random.default_rng(0)
    c = np.r_[rng.uniform(300, 700, 5000), np.full(300, 798.0), np.full(400, 176.0)]
    assert cv.estimate_clip_code(c)["code"] == 798.0 and cv.estimate_clip_code(c)["detected"]
    assert cv.estimate_black_code(c)["code"] == 176.0
    smooth = rng.uniform(300, 700, 5000)
    assert not cv.estimate_clip_code(smooth)["detected"]
    a = rng.uniform(300, 600, 500)
    with pytest.raises(ValueError):
        cv.fit_camera_curve([cv.PairSet(a, a + 100, 4.0, free=True)])   # no known ratio -> no scale
    with pytest.raises(ValueError):
        cv.PairSet(a, a[:10], 4.0)
    with pytest.raises(ValueError):
        cv.PairSet(a, a, 1.0)
