"""Starfield balancing reference (dlc.fald.starfield, work guide ticket S1): which zones qualify, how their peaks are
evened, and what must never change."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.starfield import StarfieldParams, balance_image, pixel_weight, predict, zone_plan  # noqa: E402

FULL = (0.0, 0.0, 1.0, 1.0)
BLACK = ((0, 0, 0), FULL)
WHITE = (1023, 1023, 1023)
DIMSTAR = (520, 520, 520)          # PQ10 520 = 100 nits


def rect(x0, y0, w, h):
    return (x0 / 3840, y0 / 2160, w / 3840, h / 2160)


def star(col, row, s=5, code=WHITE):
    return (code, rect(col * 80 + 40 - s // 2, row * 45 + 22 - s // 2, s, s))


@pytest.fixture(scope="module")
def model():
    return FaldModel(FaldParams())


def field(bright=((14, 14),)):
    """A 10 x 10 field of 100-nit stars, one per zone, with a few white outliers."""
    return [BLACK] + [star(c, r, code=WHITE if (c, r) in bright else DIMSTAR) for c in range(10, 20) for r in range(10, 20)]


def lum(img):
    return np.max(img, axis=0)


def test_outliers_are_pulled_onto_the_field_and_the_rest_is_untouched(model):
    img = model.render(field())
    out = balance_image(model, img, StarfieldParams())
    assert out["w"][10:20, 10:20].min() == pytest.approx(1.0)
    assert out["peak"][14, 14] > 1800.0 and 100.0 < out["new_peak"][14, 14] < 120.0   # geometric mean: 48 dim + 1 white
    assert out["img"].max() < 130.0
    dim = (lum(img) > 0.0) & (lum(img) < 200.0)
    assert np.array_equal(out["img"][:, dim], img[:, dim])                            # cap-only: the dim stars unchanged
    res = predict(model, img, StarfieldParams())
    assert res["drive_spread_on"] < 0.2 * res["drive_spread_off"]                     # the zone drives are evened
    assert res["veil_std_on"] < res["veil_std_off"]


def test_a_uniform_field_is_left_alone(model):
    img = model.render(field(bright=()))
    assert np.allclose(balance_image(model, img, StarfieldParams())["img"], img, rtol=1e-12, atol=0.0)


def test_even_lift_and_strength_are_strengths(model):
    img = model.render(field())
    half = balance_image(model, img, StarfieldParams(even=0.5))
    assert 130.0 < half["img"].max() < 1800.0
    lifted = balance_image(model, img, StarfieldParams(lift=1.0))
    sel = (lum(img) > 0.0) & (lum(img) < 200.0)
    assert lum(lifted["img"])[sel].min() >= 100.0 - 1e-9 and lum(lifted["img"])[sel].max() > 100.5
    off = balance_image(model, img, StarfieldParams(strength=0.0))
    assert np.allclose(off["img"], img, rtol=1e-12, atol=0.0)


def test_absolute_ceiling_and_target_gain(model):
    img = model.render(field())
    assert balance_image(model, img, StarfieldParams(cap_nits=50.0))["img"].max() == pytest.approx(50.0, rel=1e-6)
    calm = balance_image(model, img, StarfieldParams(target_gain=0.5))
    assert 45.0 < calm["img"].max() < 65.0


def test_solid_content_is_never_touched_and_protects_its_neighbourhood(model):
    window = (WHITE, rect(2000, 900, 400, 400))                    # cols 25-29, rows 20-28
    near = [star(23, 24), star(22, 24, code=DIMSTAR)]              # specks 2-3 zones left of the window
    far = [star(c, 40, code=WHITE if c == 8 else DIMSTAR) for c in range(4, 13)]
    img = model.render([BLACK, window] + near + far)
    out = balance_image(model, img, StarfieldParams(reach=2))
    assert out["w"][20:29, 25:30].max() == 0.0                     # the window's own zones
    assert out["w"][24, 23] == 0.0 and out["w"][40, 8] == pytest.approx(1.0)
    m_in, m_out = lum(img), lum(out["img"])
    ys, xs = np.nonzero(m_in > 0)
    win = (xs * 5 >= 2000) & (xs * 5 < 2400) & (ys * 5 >= 900) & (ys * 5 < 1300)
    assert np.array_equal(m_out[ys[win], xs[win]], m_in[ys[win], xs[win]])
    near_px = (xs * 5 >= 23 * 80) & (xs * 5 < 24 * 80) & (ys * 5 >= 24 * 45) & (ys * 5 < 25 * 45)
    assert np.allclose(m_out[ys[near_px], xs[near_px]], m_in[ys[near_px], xs[near_px]], rtol=1e-12)   # protected speck
    far_px = (xs * 5 >= 8 * 80) & (xs * 5 < 9 * 80) & (ys * 5 >= 40 * 45)
    assert m_out[ys[far_px], xs[far_px]].max() < 0.2 * m_in[ys[far_px], xs[far_px]].max()             # far outlier evened
    assert out["scale"].min() >= 0.0


def test_a_large_highlight_is_not_a_star(model):
    img = model.render([BLACK, (WHITE, rect(800, 450, 40, 40))] + [star(c, 12, code=DIMSTAR) for c in range(6, 9)])
    out = balance_image(model, img, StarfieldParams())
    assert out["sparse"][10, 10] == 0.0
    ys, xs = np.nonzero(lum(img) > 1000.0)
    assert np.array_equal(out["img"][:, ys, xs], img[:, ys, xs])


def test_peak_limit_keeps_real_highlights(model):
    z = zone_plan(model, model.render(field()), StarfieldParams(peak_hi=500.0))
    assert z["w"][14, 14] == 0.0 and z["w"][12, 12] == pytest.approx(1.0)


def test_hue_is_preserved(model):
    orange = (1000, 800, 600)
    shapes = [BLACK] + [star(c, r, code=orange if (c, r) == (14, 14) else DIMSTAR) for c in range(10, 20) for r in range(10, 20)]
    img = model.render(shapes)
    out = balance_image(model, img, StarfieldParams())
    ys, xs = np.nonzero(lum(img) > 500.0)
    ratio = out["img"][:, ys, xs] / img[:, ys, xs]
    assert np.allclose(ratio, ratio[0:1], rtol=1e-12) and ratio.max() < 1.0            # one scale for the three channels


def test_pixel_weight_is_bilinear_between_zone_centres(model):
    w = np.zeros((model.p.rows, model.p.cols)); w[10, 10] = 1.0
    px = pixel_weight(model, w)
    cy, cx = 10 * model.ch + model.ch // 2, 10 * model.cw + model.cw // 2
    assert px[cy, cx] > 0.9 and px.max() <= 1.0 and px.min() >= 0.0
    assert px[cy, cx + model.cw] < 0.1 < px[cy, cx + model.cw // 2]
    assert pixel_weight(model, np.ones_like(w)) == pytest.approx(1.0)
