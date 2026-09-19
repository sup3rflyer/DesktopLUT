"""Starfield balancing reference (dlc.fald.starfield, work guide ticket S1): which zones qualify, how their peaks are
evened, and what must never change."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.starfield import StarfieldParams, balance_image, pixel_weight, predict, zone_plan  # noqa: E402

FULL = (0.0, 0.0, 1.0, 1.0)


def SP(**kw):
    """The maths of rounds <= 5 — geometric-mean target (target_sigma 0), full pull (even 1), no absolute floor
    (keep_nits 0) — which every test written before the round-6 addenda pins; the tests of the DEFAULTS (round 7:
    even 0.8, target_sigma 0, keep_nits 100) and of the round-6 combination R6 say so."""
    return StarfieldParams(**{"even": 1.0, "target_sigma": 0.0, "keep_nits": 0.0, **kw})


R6 = {"target_sigma": 1.0, "even": 0.6}   # the round-6 defaults (spread-aware target, gentler pull): a tunable since round 7

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
    out = balance_image(model, img, SP())
    assert out["w"][10:20, 10:20].min() == pytest.approx(1.0)
    assert out["peak"][14, 14] > 1800.0 and 100.0 < out["new_peak"][14, 14] < 120.0   # geometric mean: 48 dim + 1 white
    assert out["img"].max() < 130.0
    dim = (lum(img) > 0.0) & (lum(img) < 200.0)
    assert np.array_equal(out["img"][:, dim], img[:, dim])                            # cap-only: the dim stars unchanged
    res = predict(model, img, SP())
    assert res["drive_spread_on"] < 0.2 * res["drive_spread_off"]                     # the zone drives are evened
    assert res["veil_std_on"] < res["veil_std_off"]


def test_a_uniform_field_is_left_alone(model):
    img = model.render(field(bright=()))
    assert np.allclose(balance_image(model, img, SP())["img"], img, rtol=1e-12, atol=0.0)


def test_even_lift_and_strength_are_strengths(model):
    img = model.render(field())
    half = balance_image(model, img, SP(even=0.5))
    assert 130.0 < half["img"].max() < 1800.0
    lifted = balance_image(model, img, SP(lift=1.0))
    sel = (lum(img) > 0.0) & (lum(img) < 200.0)
    assert lum(lifted["img"])[sel].min() >= 100.0 - 1e-9 and lum(lifted["img"])[sel].max() > 100.5
    off = balance_image(model, img, SP(strength=0.0))
    assert np.allclose(off["img"], img, rtol=1e-12, atol=0.0)


def test_absolute_ceiling_and_target_gain(model):
    img = model.render(field())
    assert balance_image(model, img, SP(cap_nits=50.0))["img"].max() == pytest.approx(50.0, rel=1e-6)
    calm = balance_image(model, img, SP(target_gain=0.5))
    assert 45.0 < calm["img"].max() < 65.0


def test_solid_content_is_never_touched_and_protects_its_neighbourhood(model):
    window = (WHITE, rect(2000, 900, 400, 400))                    # cols 25-29, rows 20-28
    near = [star(23, 24), star(22, 24, code=DIMSTAR)]              # specks 2-3 zones left of the window
    far = [star(c, 40, code=WHITE if c == 8 else DIMSTAR) for c in range(4, 13)]
    img = model.render([BLACK, window] + near + far)
    out = balance_image(model, img, SP(reach=2))
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
    out = balance_image(model, img, SP())
    assert out["sparse"][10, 10] == 0.0
    ys, xs = np.nonzero(lum(img) > 1000.0)
    assert np.array_equal(out["img"][:, ys, xs], img[:, ys, xs])


def test_peak_limit_keeps_real_highlights(model):
    z = zone_plan(model, model.render(field()), SP(peak_hi=500.0))
    assert z["w"][14, 14] == 0.0 and z["w"][12, 12] == pytest.approx(1.0)


def test_hue_is_preserved(model):
    orange = (1000, 800, 600)
    shapes = [BLACK] + [star(c, r, code=orange if (c, r) == (14, 14) else DIMSTAR) for c in range(10, 20) for r in range(10, 20)]
    img = model.render(shapes)
    out = balance_image(model, img, SP())
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


# ------------------------------------------------------------------------------------------------ lit skies (2026-09-19)
# Background-relative star-likeness: real content never sits on code 0, and a sky above the 0.5-nit drive floor used to
# count into the effective area (S / m). The patterns below are built on the model raster directly (one raster pixel =
# a 5 x 5-px star) so the sky can be any level in nits.
def sky_field(model, sky, outlier=(14, 14), dim=100.0, cols=range(10, 20), rows=range(10, 20)):
    img = np.full((3, model.h, model.w), float(sky))
    for r in rows:
        for c in cols:
            img[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = model.p.white_nits if (c, r) == outlier else dim
    return img


@pytest.mark.parametrize("sky", [0.0, 0.3, 1.0, 5.0, 20.0])
def test_the_outlier_is_evened_on_any_dark_sky_and_the_sky_is_untouched(model, sky):
    """The same 10 x 10 field of 100-nit 5-px stars with one white outlier on skies of 0 .. 20 nits: a_eff above the
    background is the star's own 25 px² whatever the sky (the old S / m read 61 px² on 1 nit, 204 px² for a 20-nit
    star), so the zone is fully star-like. At 20 nits it STILL works: a star-free sky zone counts as "solid" at its dim
    drive (0.083 with these parameters, 0.11 with the PA32UCXR fit) and that is below nb_lo 0.15, so the sky does not
    protect the stars sitting on it (see the bright-sky test for where that ends)."""
    img = sky_field(model, sky)
    out = balance_image(model, img, SP())
    assert out["a_eff"][14, 14] == pytest.approx(25.0, rel=1e-6) and out["sparse"][14, 14] == 1.0
    assert out["w"][10:20, 10:20].min() == pytest.approx(1.0) and out["w"][14, 14] == pytest.approx(1.0)
    assert 100.0 < out["new_peak"][14, 14] < 120.0                     # the local geometric mean: 99 dim + 1 white
    assert 100.0 < out["img"].max() < 120.0
    untouched = lum(img) < 200.0                                       # the sky and the dim stars
    assert np.array_equal(out["img"][:, untouched], img[:, untouched])  # BIT-identical
    assert out["b"][14, 14] == sky and out["solid"][5, 5] < SP().nb_lo
    assert out["w"][5, 5] == 0.0 and out["sparse"][5, 5] == 0.0        # a star-free sky zone is not star-like


def test_a_bright_sky_ends_the_balancing_smoothly(model):
    """Where it stops, and why: the zone's drive comes from the floored statistic, and a star-free zone of a BRIGHT sky is
    solid content like any other. Its drive enters nb_lo .. nb_hi (0.15 .. 0.30) at ~50 nits and passes nb_hi at ~200
    nits with these parameters: there the backlight is set by the sky and evening a speck buys nothing."""
    w_edge = {}
    for sky in (50.0, 100.0, 400.0):
        out = balance_image(model, sky_field(model, sky, outlier=(10, 10)), SP())
        w_edge[sky] = float(out["w"][10, 10])                          # a field-corner zone: sky zones within reach
        assert out["sparse"][10, 10] == 1.0                            # still star-like: it is the neighbourhood that decides
    assert w_edge[50.0] > 0.99 and 0.8 < w_edge[100.0] < 0.95 and w_edge[400.0] == 0.0
    img = sky_field(model, 400.0, outlier=(10, 10))
    assert np.array_equal(balance_image(model, img, SP())["img"][:, 10 * model.ch:11 * model.ch, 10 * model.cw:11 * model.cw],
                          img[:, 10 * model.ch:11 * model.ch, 10 * model.cw:11 * model.cw])


def test_a_dim_star_lattice_on_a_lit_sky_is_star_like(model):
    z = zone_plan(model, sky_field(model, 1.0, outlier=None, dim=20.0), SP())
    assert z["a_eff"][12, 12] == pytest.approx(25.0, rel=1e-6) and z["sparse"][12, 12] > 0.9      # S / m would be 204 px²
    assert z["w"][12, 12] == pytest.approx(1.0)


def test_flat_and_gradient_zones_are_not_star_like_and_the_min_background_over_counts(model):
    img = np.full((3, model.h, model.w), 1.0)
    cw, ch = model.cw, model.ch
    img[:, 5 * ch:6 * ch, 5 * cw:6 * cw] = np.linspace(1.0, 1.2, cw)[None, None, :]             # a smooth gradient zone
    img[:, 8 * ch:9 * ch, 8 * cw:9 * cw] = np.linspace(1.0, 3.0, cw)[None, None, :]             # a steeper one ...
    img[:, 8 * ch + ch // 2, 8 * cw + cw // 2] = 100.0                                          # ... under a 100-nit star
    img[:, 8 * ch:9 * ch, 12 * cw:13 * cw] = np.linspace(1.0, 3.0, cw)[None, None, :]           # ... and without one
    z = zone_plan(model, img, SP())
    assert z["sparse"][2, 2] == 0.0 and z["a_eff"][2, 2] == 0.0                                  # flat: never divided
    assert z["sparse"][5, 5] == 0.0 and z["a_eff"][5, 5] == pytest.approx(1800.0, rel=1e-6)      # half the zone "above" its min
    assert z["sparse"][8, 12] == 0.0
    # KNOWN LIMITATION: b is the zone's MINIMUM, so a sky that varies inside the zone over-counts the area by
    # (mean sky - min sky) * n / (peak - b) = (2 - 1) * 3600 / 99 = 36.4 px²: 25 -> 61 px², sparse 1.0 -> 0.92.
    # A 1 -> 6-nit ramp under the same star would read 25 + 91 px² (sparse 0.53); a brighter star shrinks the term.
    assert z["a_eff"][8, 8] == pytest.approx(25.0 + 36.4, abs=0.5) and z["sparse"][8, 8] == pytest.approx(0.918, abs=0.005)


def test_a_solid_window_on_a_lit_sky_still_protects_its_neighbourhood(model):
    cw, ch = model.cw, model.ch
    img = sky_field(model, 1.0, outlier=(8, 40), cols=range(4, 13), rows=range(40, 41))           # a far row with an outlier
    img[:, 900 // 5:1300 // 5, 2000 // 5:2400 // 5] = model.p.white_nits                          # a 400-px window: cols 25-29, rows 20-28
    for c, lvl in ((23, model.p.white_nits), (22, 100.0)):                                        # specks 2-3 zones left of it
        img[:, 24 * ch + ch // 2, c * cw + cw // 2] = lvl
    out = balance_image(model, img, SP(reach=2))
    assert out["w"][20:29, 25:30].max() == 0.0 and out["solid"][22, 27] == pytest.approx(1.0)    # the window's own zones
    assert out["sparse"][24, 23] == 1.0 and out["w"][24, 23] == 0.0                               # star-like but protected
    assert out["w"][40, 8] == pytest.approx(1.0)
    near = (slice(None), slice(24 * ch, 25 * ch), slice(23 * cw, 24 * cw))
    assert np.allclose(out["img"][near], img[near], rtol=1e-12)                                   # the protected speck stays
    assert out["img"][0, 40 * ch:41 * ch, 8 * cw:9 * cw].max() < 0.2 * model.p.white_nits         # the far outlier is evened
    keep = (lum(img) == 1.0) | (lum(img) == model.p.white_nits) & (np.arange(model.h)[:, None] < 30 * ch)
    assert np.array_equal(out["img"][:, keep], img[:, keep])                                      # sky + window BIT-identical


def test_the_pull_stops_at_the_background_and_never_brightens(model):
    img = sky_field(model, 5.0)
    out = balance_image(model, img, SP(cap_nits=2.0))                                # a target BELOW the sky
    stars = lum(img) > 5.0
    # Round 6 (monotone pull): with the target BELOW the background the pull threshold is the bottom of the zone's speck
    # band, b + 0.25 (peak - b) — the 100-nit stars land at 28.75 nits (rounds 3-5: flattened INTO the 5-nit sky), the
    # white outlier at ~25 % of its own peak. No hole, the sky bit-identical.
    got = lum(out["img"])[stars]
    assert np.median(got) == pytest.approx(5.0 + 0.25 * 95.0, rel=0.02) and got.min() > 5.0 and got.max() < 0.26 * model.p.white_nits
    assert lum(out["img"]).min() == 5.0 and np.all(got < lum(img)[stars])
    assert np.array_equal(out["img"][:, ~stars], img[:, ~stars])
    # a neighbour's brighter background interpolated in must not lift a pulled pixel above itself (reach 0: unprotected)
    img = sky_field(model, 1.0, outlier=(10, 14))
    img[:, 14 * model.ch:15 * model.ch, 9 * model.cw:10 * model.cw] = 900.0                       # a solid zone beside the outlier
    out = balance_image(model, img, SP(reach=0))
    assert out["scale"].max() <= 1.0 and out["scale"].min() > 0.0


# ------------------------------------------------------------------------------------------------ speck zones / speck pixels
# Round 3 (2026-09-19): the carry, the lift and the peak field key on the SPECK-ZONE flag (a lit peak above the zone's
# own background) instead of `has`, and the pull — like the lift — acts on speck PIXELS only.
def checkerboard(model, sky, dx):
    """100-nit stars in every other zone (the rest star-free), one white outlier at horizontal offset dx (raster px)."""
    img = np.full((3, model.h, model.w), float(sky))
    for r in range(10, 30):
        for c in range(10, 30):
            if (c + r) % 2 == 0:
                img[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = 100.0
    y, x = 20 * model.ch + model.ch // 2, 20 * model.cw + dx
    img[:, y, x] = model.p.white_nits
    return img, y, x


@pytest.mark.parametrize("where", ["centre", "border"])
def test_a_star_free_zone_of_a_lit_sky_carries_the_weight_like_an_empty_zone_of_a_black_one(model, where):
    """Before: a sky zone above the drive floor was `has` with w = 0 and diluted an off-centre star's pixel weight (outlier
    at the zone border on a 1-nit sky: 396 nits, w_px 0.53, vs 102 nits on black). Now it carries its speck neighbours' mean."""
    dx = model.cw // 2 if where == "centre" else model.cw - 1
    peaks = {}
    for sky in (0.0, 0.3, 1.0, 5.0):
        img, y, x = checkerboard(model, sky, dx)
        out = balance_image(model, img, SP())
        assert not out["spk"][20, 21] and out["spk"][20, 20]                       # the star-free neighbour / the outlier's zone
        assert out["w_field"][20, 21] == pytest.approx(1.0) and out["w_pixel"][y, x] == pytest.approx(1.0)
        peaks[sky] = float(out["img"][0, y, x])
        assert np.array_equal(out["img"][:, lum(img) == sky], img[:, lum(img) == sky])   # the sky: BIT-identical
    assert all(100.0 < v < 106.0 for v in peaks.values())
    assert all(abs(peaks[s] / peaks[0.0] - 1.0) < 0.03 for s in peaks)             # lit skies = the black sky within 3 %


def test_the_carry_ramps_out_with_the_zones_own_drive(model):
    """The carried weight x (1 - smoothstep(nb_lo, nb_hi, own solid drive)): full on a dark sky, 0.90 on a 100-nit sky
    (drive 0.18), 0 on a 400-nit one and in a solid window — `near` protects the speck zones, this the zones without one."""
    got = {}
    for sky in (1.0, 100.0, 400.0):
        img, _, _ = checkerboard(model, sky, model.cw // 2)
        img[:, 20 * model.ch + model.ch // 2, 20 * model.cw + model.cw // 2] = 1000.0 if sky >= 100.0 else model.p.white_nits
        for r in range(10, 30):
            for c in range(10, 30):
                if (c + r) % 2 == 0 and (c, r) != (20, 20):
                    img[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = max(100.0, 2.0 * sky)
        out = balance_image(model, img, SP(reach=0))                  # reach 0: look at the carry alone
        got[sky] = float(out["w_field"][20, 21])
        assert not out["spk"][20, 21]
    assert got[1.0] == pytest.approx(1.0) and 0.85 < got[100.0] < 0.95 and got[400.0] == 0.0


@pytest.mark.parametrize("sky", [1.0, 5.0])
def test_a_target_below_the_sky_leaves_the_sky_bit_identical(model, sky):
    img = sky_field(model, sky)
    stars = lum(img) > sky
    for sp in (SP(cap_nits=0.5 * sky), SP(target_gain=0.01)):
        out = balance_image(model, img, sp)
        assert np.array_equal(out["img"][:, ~stars], img[:, ~stars])               # every sky pixel, bit for bit
        got = lum(out["img"])[stars]               # round 6: the stars stop at the bottom of their speck band (not at the sky)
        assert np.median(got) == pytest.approx(sky + 0.25 * (100.0 - sky), rel=0.15)   # (target_gain 0.01 on 1 nit: g > 0, lower)
        assert got.min() > sky and got.max() < 0.26 * model.p.white_nits and lum(out["img"]).min() == sky


@pytest.mark.parametrize("sky", [0.3, 1.0, 5.0])
def test_lift_never_touches_the_sky(model, sky):
    """Before: the transition band toward a star-free `has` zone lifted the sky (1 nit: 1732 raster px up to x1.84)."""
    img = sky_field(model, sky)
    out = balance_image(model, img, SP(lift=1.0))
    sky_px = lum(img) == sky
    assert np.array_equal(out["img"][:, sky_px], img[:, sky_px])
    dim = lum(img) == 100.0
    assert lum(out["img"])[dim].min() > 100.5 and lum(out["img"]).max() < 106.0    # the specks rise onto the local mean


def test_a_sky_close_to_the_star_level_with_a_cap_below_it(model):
    """Sky 50 / stars 100 / cap 30. Round 4: the speck band is BACKGROUND-RELATIVE, so the sky ((m - b) / (pk - b) = 0) is
    no speck pixel, and with the target below the background the pull gate closes to the speck pixels: the sky is not
    even considered (round 3: it counted as speck at m / pk = 0.5 and only the background floor held it). The stars are
    above the pull threshold T' = 50 + 0.25 (100 - 50) = 62.5 (round 6, the monotone pull: with the target below the
    background the threshold is the bottom of the speck band; rounds 3-5 flattened them INTO the sky at 50) — a cap below
    the sky cannot be met; no hole, nothing brightened, the profile stays monotone. With LIFT the sky is untouched
    too (round 3 lifted it by up to 3.6 % here, up to x1.67 for 60-nit stars under a 100-nit target)."""
    img = sky_field(model, 50.0)
    sky_px = lum(img) == 50.0
    out = balance_image(model, img, SP(cap_nits=30.0))
    assert out["w"][14, 14] == pytest.approx(1.0) and out["target"][14, 14] == 30.0
    assert np.array_equal(out["img"][:, sky_px], img[:, sky_px])
    got = lum(out["img"])[~sky_px]                                                 # round 6: 100 nits -> 62.5 = the bottom of
    assert np.median(got) == pytest.approx(62.5, rel=0.03) and got.min() > 50.0    # the speck band (rounds 3-5: -> 50, the sky)
    plain = balance_image(model, img, SP())
    assert np.array_equal(plain["img"][:, sky_px], img[:, sky_px]) and 100.0 < lum(plain["img"]).max() < 106.0
    lifted = balance_image(model, img, SP(lift=1.0))
    assert np.array_equal(lifted["img"][:, sky_px], img[:, sky_px])               # round 3: up to x1.036 (band relative to 0)
    assert out["is_speck"][sky_px].max() == 0.0 and out["gate"][sky_px].max() == 0.0


# ------------------------------------------------------------------------------------------------ round 4: grain, soft stars, bright skies
def grainy_sky(model, sky, grain, seed=11):
    """A sky with multiplicative uniform grain (+- grain), the same on the three channels."""
    base = float(sky) * (1.0 + np.random.default_rng(seed).uniform(-grain, grain, (model.h, model.w)))
    return np.repeat(base[None], 3, axis=0)


def grainy_checkerboard(model, sky, grain, dx):
    img = grainy_sky(model, sky, grain)
    star_px = np.zeros((model.h, model.w), dtype=bool)
    for r in range(10, 30):
        for c in range(10, 30):
            if (c + r) % 2 == 0:
                y, x = r * model.ch + model.ch // 2, c * model.cw + model.cw // 2
                img[:, y, x] = 100.0; star_px[y, x] = True
    y, x = 20 * model.ch + model.ch // 2, 20 * model.cw + dx
    img[:, y, x] = model.p.white_nits; star_px[y, x] = True
    return img, star_px, y, x


@pytest.mark.parametrize("grain", [0.03, 0.10])
@pytest.mark.parametrize("sky", [1.0, 5.0])
def test_a_grainy_sky_behaves_like_a_flat_one(model, sky, grain):
    """D: the 2 % FLAT rule alone called every grainy sky zone a speck zone (w = 0, no carry: outlier at a zone border
    396 nits, w_px 0.53; lift 1 raised 12 828 sky raster px by up to x1.83). A speck zone now also needs a STAR-SIZED area
    above its background (a_eff < area_hi); grain reads about half the zone (~1700 px2)."""
    for where, dx in (("centre", model.cw // 2), ("border", model.cw - 1)):
        f_img, y, x = checkerboard(model, sky, dx)
        flat = float(balance_image(model, f_img, SP())["img"][0, y, x])
        img, star_px, y, x = grainy_checkerboard(model, sky, grain, dx)
        out = balance_image(model, img, SP())
        assert not out["spk"][20, 21] and out["a_eff"][20, 21] > 1000.0 and out["w_field"][20, 21] > 0.97
        got = float(out["img"][0, y, x])
        assert abs(got / flat - 1.0) < 0.04, (where, got, flat)                        # the flat-sky number within a few %
        assert np.array_equal(out["img"][:, ~star_px], img[:, ~star_px])               # every non-star pixel: BIT-identical
        lifted = balance_image(model, img, SP(lift=1.0))
        assert np.array_equal(lifted["img"][:, ~star_px], img[:, ~star_px])            # ... under lift too
        assert lum(lifted["img"])[star_px].min() >= 100.0                              # while the specks rise


def test_a_non_star_shape_inside_a_star_field_is_never_touched(model):
    """The own-zone gate (round 5): a pixel is acted on only when the zone it lies in is a SPECK zone. A zone without a
    speck still CARRIES its neighbours' weight (for the border pixels of the neighbouring speck zones) and shows the
    local target as its peak — without the gate a NON-star shape brighter than the target in such a zone was pulled like
    a speck. Checkerboard field of 100-nit stars on a 1-nit sky, the shape centred in a star-free zone, mean over the
    shape (round 3 -> round 4 -> now):
      40 x 40 px, 150 nits (drive 0.25, between nb_lo and nb_hi):  -3.5 % ->  -5.7 % -> 0, bit-identical
      15 x 15 px, 150 nits (drive 0.13 < nb_lo, a_eff 225 px2):    -4.8 % -> -33.3 % -> 0, bit-identical
      15 x 15 px, 600 nits (drive 0.21):                          -12.9 % -> -54.5 % -> 0, bit-identical
      10 x 10 px, 150 nits (a_eff 100 px2: a star-like speck zone): -19.7 % throughout (sparse 0.5 — it IS half a star)
      40 x 40 px, 300 nits (drive 0.36 >= nb_hi):                     0 %  throughout (solid: carries 0 and protects)"""
    def pulled(side, nits):
        img, _, _ = checkerboard(model, 1.0, model.cw // 2)
        img[:, 20 * model.ch + model.ch // 2, 20 * model.cw + model.cw // 2] = 100.0   # no outlier: a plain field
        sl = (slice(None), slice(20 * model.ch + (model.ch - side) // 2, 20 * model.ch + (model.ch - side) // 2 + side),
              slice(21 * model.cw + (model.cw - side) // 2, 21 * model.cw + (model.cw - side) // 2 + side))
        img[sl] = nits
        out = balance_image(model, img, SP())
        return out, img, sl
    for side, nits in ((8, 150.0), (3, 150.0), (3, 600.0), (8, 300.0)):
        out, img, sl = pulled(side, nits)
        assert not out["spk"][20, 21] and np.array_equal(out["img"][sl], img[sl]), (side, nits)   # exactly 0 %
        zone = (slice(None), slice(20 * model.ch, 21 * model.ch), slice(21 * model.cw, 22 * model.cw))
        assert np.array_equal(out["img"][zone], img[zone])                         # the whole star-free zone
    out, img, sl = pulled(3, 150.0)
    assert out["w_field"][20, 21] == pytest.approx(1.0)                            # ... although it carries the full weight
    out, img, sl = pulled(2, 150.0)
    assert out["spk"][20, 21] and float(out["img"][sl].mean() / 150.0 - 1.0) == pytest.approx(-0.197, abs=0.01)
    for grain in (0.0, 0.05):                                                       # the case the carry exists for
        img, star_px, y, x = grainy_checkerboard(model, 1.0, grain, model.cw - 1)
        # 105.3 (rounds 3-5: 102.0): in the TAPERED target window the outlier's own zone weighs 1, the field around it less
        assert balance_image(model, img, SP())["img"][0, y, x] == pytest.approx(105.3, rel=0.01)


def soft_star(model, sky, zone=(14, 14)):
    """An 1800-nit core with a 50 % cross, 20 % diagonals and a 5 % outer cross (a_eff 100 px2 on black), in a 10 x 10
    field of 100-nit stars. Returns the image and the four pixel groups from the core outward."""
    img = np.full((3, model.h, model.w), float(sky))
    for r in range(10, 20):
        for c in range(10, 20):
            img[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = 100.0
    y, x = zone[1] * model.ch + model.ch // 2, zone[0] * model.cw + model.cw // 2
    groups = [[(0, 0)], [(0, 1), (0, -1), (1, 0), (-1, 0)], [(1, 1), (1, -1), (-1, 1), (-1, -1)], [(0, 2), (0, -2), (2, 0), (-2, 0)]]
    for frac, g in zip((1.0, 0.5, 0.2, 0.05), groups):
        for dy, dx in g:
            img[:, y + dy, x + dx] = max(1800.0 * frac, sky)
    return img, [[(y + dy, x + dx) for dy, dx in g] for g in groups]


@pytest.mark.parametrize("sky", [0.0, 1.0, 5.0])
def test_a_soft_star_comes_down_as_a_whole(model, sky):
    """E: with the pull on speck pixels only (round 3) the 20 % ring stayed at 360 nits around a core pulled to ~100: a
    bright ring around a dark core in EVERY normal setting. The gate max(is_speck, smoothstep(1, 2, target / background))
    pulls the whole profile while the target is well above the background."""
    img, groups = soft_star(model, sky)
    for sp in (SP(), SP(area_lo=100.0, area_hi=400.0)):      # w = 0.5 / fully star-like
        out = balance_image(model, img, sp)
        hi = [max(out["img"][0, y, x] for y, x in g) for g in groups]
        lo = [min(out["img"][0, y, x] for y, x in g) for g in groups]
        # MONOTONE non-increasing outward (1e-3: fully pulled pixels land on their OWN interpolated target, which the
        # tapered window makes vary by ~5e-5 across the star; rounds 3-5 had a flat target inside the field)
        assert all(lo[i] >= hi[i + 1] * (1 - 1e-3) for i in range(3)), (sky, hi, lo)
        assert lo[3] >= sky and lum(out["img"]).min() >= sky * (1 - 1e-12)             # nothing below the sky
        assert hi[0] < 0.5 * 1800.0                                                     # ... and the core did come down
        sky_px = lum(img) == sky
        assert np.array_equal(out["img"][:, sky_px], img[:, sky_px])
    full = balance_image(model, img, SP(area_lo=100.0, area_hi=400.0))
    y, x = groups[0][0]
    assert full["w"][14, 14] == pytest.approx(1.0) and 100.0 < full["img"][0, y, x] < 125.0
    assert full["img"][0, y + 1, x + 1] == pytest.approx(full["img"][0, y, x], rel=0.02)   # the 20 % ring lands with the core


def test_a_target_below_a_bright_sky_still_leaves_the_sky_bit_identical(model):
    """The round-3 finding-C cases on a 50-nit sky (1 and 5 nits are parametrized above). Round 6: the 100-nit stars stop
    at the bottom of their speck band, 50 + 0.25 x 50 = 62.5 nits (rounds 3-5: at the 50-nit sky)."""
    img = sky_field(model, 50.0)
    sky_px = lum(img) == 50.0
    for sp in (SP(cap_nits=25.0), SP(target_gain=0.01), SP(cap_nits=25.0, lift=1.0)):
        out = balance_image(model, img, sp)
        assert np.array_equal(out["img"][:, sky_px], img[:, sky_px])
        got = lum(out["img"])[~sky_px]
        assert np.median(got) == pytest.approx(62.5, rel=0.06) and got.min() > 50.0 and got.max() < 0.26 * model.p.white_nits


@pytest.mark.parametrize("sky,dim", [(20.0, 60.0), (50.0, 100.0)])
def test_lift_on_a_sky_close_to_the_star_level_leaves_the_sky_alone(model, sky, dim):
    """F: the speck band is relative to the BACKGROUND: (m - b) / (peak - b). A 20-nit sky under 60-nit stars sits at 33 %
    of the peak but at 0 % of the way from the background - no speck pixel (round 3 lifted such a sky by up to x1.67)."""
    img = sky_field(model, sky, dim=dim)
    out = balance_image(model, img, SP(lift=1.0))
    sky_px = lum(img) == sky
    assert np.array_equal(out["img"][:, sky_px], img[:, sky_px])                        # 0 sky pixels changed
    specks = lum(img) == dim
    assert out["w"][14, 14] == pytest.approx(1.0)
    assert lum(out["img"])[specks].min() > dim * 1.01 and lum(out["img"])[specks].max() < out["target"][10:20, 10:20].max() * 1.001


# ------------------------------------------------------------------------------------------------ round 5: tapered protection
def window_scene(model, sky, grain=0.0, mirror=False):
    """A solid 1000-nit window (zones 20..24 x 18..22) and a column block of 100-nit stars well away from it."""
    img = grainy_sky(model, sky, grain) if grain else np.full((3, model.h, model.w), float(sky))
    img[:, 18 * model.ch:23 * model.ch, 20 * model.cw:25 * model.cw] = 1000.0
    for r in range(14, 27):
        for c in (list(range(30, 36)) if not mirror else list(range(9, 15))):
            img[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = 100.0
    return img


def step_away(model, sky, grain=0.0, mirror=False, params=None):
    """A white 5-px star stepped away from the window's edge in raster (5-px) steps along row 20: distances in px from
    the window edge, the balanced peak and the pixel weight at each step."""
    base = window_scene(model, sky, grain, mirror)
    y = 20 * model.ch + 2
    dist, peak, w = [], [], []
    for k in range(4, 4 * model.cw, 1):                                             # 20 px .. 4 zones from the edge
        x = 25 * model.cw + k if not mirror else 20 * model.cw - 1 - k
        img = base.copy()
        img[:, y, x] = model.p.white_nits
        out = balance_image(model, img, params or SP())
        dist.append(5 * k); peak.append(float(out["img"][0, y, x])); w.append(float(out["w_pixel"][y, x]))
    return np.array(dist), np.array(peak), np.array(w)


@pytest.mark.parametrize("sky,grain", [(0.0, 0.0), (1.0, 0.0), (1.0, 0.05)])
def test_a_star_leaving_solid_content_gains_weight_continuously(model, sky, grain):
    """Round 4: `near` was a hard box max baked into the zone weight — a star crossing the reach boundary jumped from
    788 to 107 nits within one 16-px step (w_px 0.30 -> 1.00). Now the protection field is TAPERED (k = 1, 1, 0.5, 0 at
    d = 0..3 for reach 2) and interpolated PER PIXEL, so the weight rises continuously with the distance.
    PINNED NUMBER: next to a 1000-nit window (solid drive 0.75 -> near 0.375 at d = 2) near_px crosses nb_hi -> nb_lo
    within ~30 px (135 .. 165 px from the window edge), so the weight ramp 0 -> 1 spans ~6 raster steps and the largest
    5-px step changes the peak of a WHITE star (18x above the field) by x0.51 — monotone and continuous, but NOT within
    a ~25 %-per-8-px target: that needs w to change <= 0.08 per 8 px, i.e. a ramp of >= 100 px for such an outlier (the
    ramp width is (nb_hi - nb_lo) / (0.5 solid) zone pitches: the taper acts on the DRIVE, not on the protection)."""
    dist, peak, w = step_away(model, sky, grain)
    assert np.all(np.diff(peak) <= 1e-9 * peak[:-1]) and np.all(np.diff(w) >= -1e-12)        # monotone
    assert peak[0] == pytest.approx(model.p.white_nits) and w[0] == 0.0                       # protected next to the window
    assert 100.0 < peak[-1] < 112.0 and w[-1] == pytest.approx(1.0)                           # free 4 zones away
    ratio = peak[1:] / peak[:-1]
    assert 0.45 < ratio.min() < 0.60, ratio.min()                                             # the pinned largest step (x0.50)
    ramp = dist[(w > 0.01) & (w < 0.99)]
    assert 20 <= ramp.max() - ramp.min() <= 40 and 120 < ramp.min() and ramp.max() < 200    # between the d = 2 and d = 3 zone centres


def test_protection_is_mirror_symmetric_and_a_mid_drive_object_protects_partially(model):
    """(c) Approaching from the other side gives the same series (the taper and the bilinear fields are symmetric)."""
    d1, p1, w1 = step_away(model, 1.0)
    d2, p2, w2 = step_away(model, 1.0, mirror=True)
    assert np.allclose(p1, p2, rtol=1e-9) and np.allclose(w1, w2, atol=1e-12)
    # a speck at chebyshev distance 2 from solid content: k = 0.5. Next to a FULL-drive window (solid 1.0 -> near 0.5 >=
    # nb_hi) it is still fully protected; next to a mid-drive object (300 nits: drive 0.36 -> near 0.18) it is now only
    # PARTLY protected (w 0.90) where the hard box max of round 4 gave 0.
    for nits, want in ((1000.0, 0.0), (300.0, 0.90)):
        img = np.full((3, model.h, model.w), 1.0)
        img[:, 18 * model.ch:23 * model.ch, 20 * model.cw:25 * model.cw] = nits
        for c in (26, 27, 28):                                                     # d = 2, 3, 4 from the object
            img[:, 20 * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = 100.0
        z = zone_plan(model, img, SP(reach=2))
        assert z["near"][20, 25] == pytest.approx(z["solid"][20, 24]) and z["near"][20, 26] == pytest.approx(0.5 * z["solid"][20, 24])
        assert z["near"][20, 27] < 0.01 and z["w"][20, 27] == pytest.approx(1.0)      # (< 0.01: the 1-nit sky zones' own dim drive)
        assert z["w"][20, 26] == pytest.approx(want, abs=0.02), (nits, z["w"][20, 26])
    # reach 0: only the zone itself, at half strength (docstring item 6)
    z0 = zone_plan(model, img, SP(reach=0))
    assert z0["near"][20, 22] == pytest.approx(0.5 * z0["solid"][20, 22]) and z0["near"][20, 25] < 0.01


@pytest.mark.parametrize("sky", [0.0, 1.0])
def test_a_star_crossing_a_zone_border_moves_smoothly(model, sky):
    """(d) A white star stepped from one zone's centre to the next (both zones keep their own 100-nit star) in raster
    steps: the balanced peak stays within a few percent of the local target at every step."""
    base = np.full((3, model.h, model.w), float(sky))
    for r in range(10, 20):
        for c in range(10, 20):
            base[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = 100.0
    peaks = []
    for k in range(model.cw + 1):
        img = base.copy()
        y, x = 14 * model.ch + 1, 14 * model.cw + model.cw // 2 + k
        img[:, y, x] = model.p.white_nits
        peaks.append(float(balance_image(model, img, SP())["img"][0, y, x]))
    peaks = np.array(peaks)
    assert peaks.min() > 100.0 and peaks.max() < 108.0 and np.abs(peaks[1:] / peaks[:-1] - 1.0).max() < 0.03


# ------------------------------------------------------------------------------------------------ round 6: review fixes
def test_the_pull_is_monotone_in_the_pixel_level():
    """Review finding 2 (the "donut"): with the gate INSIDE the exponent (round 4-5) out(m) rose and fell again when
    target / background < 2 — b 10, t 15, pk 100: out peaked at 24.5 (m = 40) and came back to 15 at the peak. Now
    out(m) = m up to the pull threshold T' and m^(1 - a) T'^a above it: non-decreasing for every (b, t, pk, w, even)."""
    from dlc.fald.starfield import pixel_rule
    cases = [(10.0, 15.0, 100.0), (10.0, 12.0, 100.0), (5.0, 6.0, 200.0), (20.0, 30.0, 100.0), (1.0, 1.5, 100.0), (10.0, 8.0, 100.0),
             (1.0, 100.0, 1800.0), (1e-12, 100.0, 1800.0), (5.0, 10.0, 400.0), (5.0, 9.99, 400.0)]      # the last four: g = 1 / ~1
    for b, t, pk in cases:
        m = np.linspace(b, pk, 4001)
        for w in (1.0, 0.6):
            for even in (1.0, 0.5):
                r = pixel_rule(m, w, t, b, pk, 0.0, even)
                out = r["out"]
                assert np.all(np.diff(out) >= -1e-9 * out[:-1]), (b, t, pk, w, even)
                assert np.all(out <= m * (1 + 1e-12)) and np.all(out >= b * (1 - 1e-12))
                tp = float(r["pull_threshold"][0])
                assert t <= tp <= max(t, b + 0.25 * (pk - b)) * (1 + 1e-12)
                assert np.array_equal(out[m <= tp], m[m <= tp])                        # untouched up to the threshold
    full = pixel_rule(np.array([1800.0, 900.0, 360.0, 150.0, 90.0]), 1.0, 100.0, 1.0, 1800.0, 0.0, 1.0)      # g = 1: T' = t
    assert np.allclose(full["out"], [100.0, 100.0, 100.0, 100.0, 90.0]) and float(full["pull_threshold"][0]) == pytest.approx(100.0)
    low = pixel_rule(np.array([10.0, 20.0, 32.5, 40.0, 100.0]), 1.0, 8.0, 10.0, 100.0, 0.0, 1.0)             # target below the sky
    assert float(low["pull_threshold"][0]) == pytest.approx(32.5) and np.allclose(low["out"], [10.0, 20.0, 32.5, 32.5, 32.5])
    assert not low["acts"][:3].any() and low["acts"][3:].all()


def test_lift_is_monotone_too():
    from dlc.fald.starfield import pixel_rule
    m = np.linspace(1.0, 60.0, 2001)
    out = pixel_rule(m, 1.0, 100.0, 1.0, 60.0, np.log(100.0 / 60.0), 1.0)["out"]
    assert np.all(np.diff(out) >= -1e-9 * out[:-1]) and out[-1] == pytest.approx(100.0) and out[0] == 1.0


def flank_scene(model, sky=0.0):
    """Three 100-nit stars, a 200-nit star, and far away a bright feature STRADDLING a zone border: its 1500-nit core at
    the left edge of zone (30, 20) and its 300-nit flank at the right edge of zone (29, 20) (raster-aligned)."""
    img = np.full((3, model.h, model.w), float(sky))
    for (c, r), lvl in {(21, 20): 200.0, (22, 17): 100.0, (23, 23): 100.0, (21, 24): 100.0}.items():
        img[:, r * model.ch + model.ch // 2, c * model.cw + model.cw // 2] = lvl
    y = 20 * model.ch + model.ch // 2
    img[:, y, 30 * model.cw] = 1500.0
    img[:, y, 30 * model.cw - 1] = 300.0
    return img


@pytest.mark.parametrize("sky", [0.0, 1.0])
def test_the_flank_of_a_bright_star_is_no_independent_dim_star(model, sky):
    """Review finding 1: the spill of a bright star into the neighbour zone counted as a dim star with full weight. A
    flank zone keeps its weight for its own pixels but carries none in the target average."""
    z = zone_plan(model, flank_scene(model, sky), SP())
    assert z["flank"][20, 29] and not z["flank"][20, 30] and z["flank"].sum() == 1
    assert z["w"][20, 29] == pytest.approx(1.0) and z["wt"][20, 29] == 0.0 and z["wt"][20, 30] == pytest.approx(1.0)
    lx, ly = z["arg"]
    assert (lx[20, 29], lx[20, 30]) == (79, 0)                                     # the brightest pixels hug the shared edge
    # the 200-nit star 8 / 9 zones away sees the 1500-nit CORE at the rim of its tapered window and not the 300-nit flank
    no_flank = flank_scene(model, sky); no_flank[:, 20 * model.ch + model.ch // 2, 30 * model.cw - 1] = sky
    assert zone_plan(model, no_flank, SP())["target"][20, 21] == pytest.approx(z["target"][20, 21], rel=1e-12)
    # a flat-topped feature straddling the border (EQUAL peaks) counts once: its two zones SHARE the vote (symmetric)
    flat = flank_scene(model, sky); flat[:, 20 * model.ch + model.ch // 2, 30 * model.cw - 1] = 1500.0
    zf = zone_plan(model, flat, SP())
    assert not zf["flank"].any() and zf["wt"][20, 29] == pytest.approx(0.5) and zf["wt"][20, 30] == pytest.approx(0.5)
    assert zf["w"][20, 29] == pytest.approx(1.0) and zf["target"][20, 21] == pytest.approx(133.2, rel=0.01)   # (127.1 above: here zone 29 holds half a vote at the window's rim)
    # ... and two DIFFERENT stars in neighbouring zones, away from the shared edge, are both counted
    apart = flank_scene(model, sky); apart[:, 20 * model.ch + model.ch // 2, 30 * model.cw - 1] = sky
    apart[:, 20 * model.ch + model.ch // 2, 29 * model.cw + model.cw // 2] = 300.0
    assert not zone_plan(model, apart, SP())["flank"].any()


def test_the_target_window_is_tapered():
    """(E + 1 - d) / (E + 1): a star at the rim of the window weighs 1 / (E + 1), one zone further out nothing — entering
    or leaving the window moves the target by a rim step, not by a full vote."""
    from dlc.fald.starfield import box_tapered_sum
    a = np.zeros((30, 30)); a[15, 15] = 1.0
    s = box_tapered_sum(a, 8)
    assert s[15, 15] == 1.0 and s[15, 16] == pytest.approx(8 / 9) and s[15, 23] == pytest.approx(1 / 9) and s[15, 24] == 0.0
    assert s[7, 7] == pytest.approx(1 / 9) and s[6, 15] == 0.0 and box_tapered_sum(a, 0)[15, 15] == 1.0


def test_target_never_exceeds_white_and_needs_no_floor(model):
    img = sky_field(model, 0.0, dim=1500.0)
    z = zone_plan(model, img, SP(target_gain=2.0))
    assert z["target"].max() == model.p.white_nits                                # min(target, white)
    w_tiny = zone_plan(model, sky_field(model, 0.0), SP(strength=1e-9))
    assert 100.0 < w_tiny["target"][14, 14] < 120.0                               # a tiny weight sum is still a weight sum


# ------------------------------------------------------------------------------------------------ rounds 6 / 7: the DEFAULTS and the R6 combination
# Owner's first eye test (Gravity frame 3045: 696 speck zones, peaks median 2 nits, p99 74, max 175): equalising a heavy-
# tailed star field to its geometric mean deleted the stars that matter, and on such a field there was no haze to fix in
# the first place. Round 6 answered with target = max(exp(mean + 1 std of ln peak), keep_nits 100), even 0.6; round 7
# (the owner's real clips) keeps only the FLOOR as a default: target_sigma 0, even 0.8, keep_nits 100 — the spread term
# (R6) is a tunable. The tests above pin the OLD maths through SP(); these pin the defaults AND the R6 combination.
def star_population(model, seed, kind, sky=0.0, scale=1.0):
    """A sparse field of 5-px specks at random zone-interior positions (fixed seed) + the list of (y, x, level).
    kind 'heavy': log-normal peaks (median 2 nits, sigma_ln 1.2) + six bright stars 50 .. 200 nits in ~35 % of the zones;
    kind 'lights': log-uniform 200 .. 1800 nits in saturated colours, ~25 % of the zones, a third of them with 2-3 specks."""
    rng = np.random.default_rng(seed)
    img = np.full((3, model.h, model.w), float(sky))
    stars = []
    colours = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.55, 0.0]])       # red, green, blue, amber
    for r in range(4, model.p.rows - 4):
        for c in range(4, model.p.cols - 4):
            if rng.random() > (0.35 if kind == "heavy" else 0.25):
                continue
            n = 1 if kind == "heavy" or rng.random() > 0.33 else int(rng.integers(2, 4))
            for _ in range(n):
                y = r * model.ch + int(rng.integers(1, model.ch - 1)); x = c * model.cw + int(rng.integers(1, model.cw - 1))
                if lum(img)[y, x] != sky:
                    continue                                                          # taken: one speck per pixel
                if kind == "heavy":
                    lvl = max(float(np.exp(np.log(2.0) + 1.2 * rng.standard_normal())) * scale, float(sky))
                    img[:, y, x] = lvl
                else:
                    lvl = float(np.exp(rng.uniform(np.log(200.0), np.log(1800.0))))
                    img[:, y, x] = lvl * colours[int(rng.integers(0, 4))]
                stars.append((y, x, lvl))
    if kind == "heavy":
        for k, lvl in enumerate((50.0, 75.0, 102.0, 130.0, 175.0, 200.0)):
            r, c = 8 + 5 * k, 10 + 4 * k
            y, x = r * model.ch + model.ch // 2, c * model.cw + model.cw // 2
            img[:, r * model.ch:(r + 1) * model.ch, c * model.cw:(c + 1) * model.cw] = sky        # the zone's only star
            stars = [s for s in stars if not (r * model.ch <= s[0] < (r + 1) * model.ch and c * model.cw <= s[1] < (c + 1) * model.cw)]
            img[:, y, x] = lvl * scale
            stars.append((y, x, lvl * scale))
    return img, stars


def levels(out_img, stars):
    return np.array([lum(out_img)[y, x] for y, x, _ in stars])


def order_kept(stars, shown, model, zones=3, ratio=1.2):
    """Every pair of specks within `zones` zones whose INPUT levels differ by more than `ratio` keeps its order."""
    pos = np.array([(y / model.ch, x / model.cw) for y, x, _ in stars]); lv = np.array([s[2] for s in stars])
    near = (np.abs(pos[:, None, :] - pos[None, :, :]).max(axis=2) <= zones) & (lv[:, None] > ratio * lv[None, :])
    i, j = np.nonzero(near)
    return bool(np.all(shown[i] >= shown[j] * (1 - 1e-9))), int(i.size)


@pytest.mark.parametrize("sky", [0.0, 0.3])
def test_a_heavy_tailed_field_is_compressed_not_flattened(model, sky):
    """Addendum 1, the R6 combination pinned EXPLICITLY (keep_nits 0 to isolate the spread-aware target; no longer the
    default since round 7): with k 1 / even 0.6 the brightest stars stay the
    brightest, none of the top 5 % ends below 4 x the field median (16 .. 200 nits -> 9.6 .. 39), the order is kept and
    the predicted zone drives still even out (their std over the speck zones falls 0.00020 -> 0.00004; the RELATIVE spread
    std / mean rises, because nearly every zone of such a field already sits below the drive floor — 21 of 488 lit: the
    hardware finding "no haze to fix here"). With k 0 / even 1 (the old maths) the field is flattened: the top 5 % end at
    2.1 .. 3.3 nits, the 200-nit star at 3.1."""
    img, stars = star_population(model, 7, "heavy", sky)
    lv = np.array([s[2] for s in stars])
    med = float(np.median(lv))
    new = predict(model, img, StarfieldParams(keep_nits=0.0, **R6))
    old = predict(model, img, SP())
    s_new, s_old = levels(new["balanced"]["img"], stars), levels(old["balanced"]["img"], stars)
    top = lv >= np.percentile(lv, 95)
    assert top.sum() >= 10 and s_new[top].min() > 4.0 * med                              # the stars that matter survive
    assert list(np.argsort(s_new)[-4:]) == list(np.argsort(lv)[-4:])                     # the brightest stay the brightest, in order
    rank = lambda v: np.argsort(np.argsort(v))                                           # (globally the order is kept up to the
    assert np.corrcoef(rank(lv), rank(s_new))[0, 1] > 0.999                              # neighbourhoods' different targets: 75 <-> 51)
    kept, pairs = order_kept(stars, s_new, model)
    assert kept and pairs > 500
    spk = new["balanced"]["spk"]
    assert new["drives_on"][spk].std() < 0.5 * new["drives_off"][spk].std()              # ... and the zone drives still even out
    i200 = int(np.argmax(lv))
    assert lv[i200] == 200.0 and s_new[i200] == pytest.approx(39.0, rel=0.03) and s_old[i200] == pytest.approx(3.1, rel=0.15)
    assert s_old[top].max() < 2.0 * med                                                  # the old maths: everything near the median
    assert np.all(s_new <= lv * (1 + 1e-12)) and np.array_equal(s_new[lv < med], lv[lv < med])   # nothing lifted, the body untouched


def test_a_uniform_field_is_still_left_alone_with_the_new_defaults(model):
    """std = 0 -> target = the mean, nothing above it (keep_nits 0 so the floor is not what protects it)."""
    img = model.render(field(bright=()))
    for sp in (StarfieldParams(keep_nits=0.0), StarfieldParams(keep_nits=0.0, **R6), StarfieldParams(keep_nits=0.0, target_sigma=4.0),
               StarfieldParams()):
        out = balance_image(model, img, sp)
        assert np.allclose(out["img"], img, rtol=1e-12, atol=0.0)
    z = zone_plan(model, img, StarfieldParams(keep_nits=0.0, **R6))
    assert z["ln_std"][14, 14] < 1e-6 and z["target"][14, 14] == pytest.approx(z["peak"][14, 14], rel=1e-6)


def test_a_lone_outlier_with_the_new_defaults(model):
    """The 10 x 10 field of 100-nit stars with ONE white (1842-nit) outlier. Where the outlier lands:
      old maths (k 0, even 1, keep 0):            105 nits  (the tapered geometric mean)
      k 1, even 1, keep 0:                         ~150 nits  (mean + 1 std: one outlier in 100 makes std ~0.35)
      round 6 (k 1, even 0.6, keep 100):           ~410 nits  (pulled 60 % of the way in the log domain)
      DEFAULTS (k 0, even 0.8, keep 100):          ~186 nits  (= 1842^0.2 x 105^0.8)
      defaults, keep 0:                            the same (the target is above 100 nits anyway)."""
    img = model.render(field())
    got = {}
    for name, sp in (("old", SP()), ("k1_even1", SP(target_sigma=1.0)), ("r6", StarfieldParams(**R6)), ("defaults", StarfieldParams()),
                     ("defaults_keep0", StarfieldParams(keep_nits=0.0))):
        out = balance_image(model, img, sp)
        got[name] = float(lum(out["img"]).max())
        dim = (lum(img) > 0.0) & (lum(img) < 200.0)
        assert np.array_equal(out["img"][:, dim], img[:, dim])                           # the 100-nit stars: untouched
    assert got["old"] == pytest.approx(105.0, rel=0.03) and got["k1_even1"] == pytest.approx(150.0, rel=0.08)
    assert got["r6"] == pytest.approx(410.7, rel=0.02)
    assert got["defaults"] == pytest.approx(186.2, rel=0.02) and got["defaults_keep0"] == pytest.approx(got["defaults"], rel=1e-9)


def test_level_sweep_of_one_star_in_a_100_nit_field_with_the_defaults(model):
    """One star of the 10 x 10 field of 100-nit stars swept 120 .. 1800 nits, DEFAULTS: shown = level^0.2 x target^0.8
    with the target = the tapered geometric mean (it rises 100.6 -> 107 with the star itself): 120 -> 104, 150 -> 109,
    200 -> 117, 300 -> 127, 500 -> 142, 1000 -> 166, 1800 -> 188 — monotone, the order is kept, nothing else moves."""
    base = model.render(field(bright=()))
    ys, xs = np.nonzero(lum(base) > 0.0)
    y, x = int(ys[ys.size // 2]), int(xs[xs.size // 2])
    want = {120.0: 104.2, 150.0: 109.4, 200.0: 116.5, 300.0: 127.3, 500.0: 142.3, 1000.0: 165.5, 1800.0: 188.2}
    shown = []
    for lvl, pinned in want.items():
        img = base.copy(); img[:, y, x] = lvl
        out = balance_image(model, img, StarfieldParams())
        got = float(lum(out["img"])[y, x])
        assert got == pytest.approx(pinned, rel=0.01) and got == pytest.approx(lvl ** 0.2 * out["target_pixel"][y, x] ** 0.8, rel=1e-3)
        rest = np.ones(lum(img).shape, dtype=bool); rest[y, x] = False
        assert np.array_equal(out["img"][:, rest], img[:, rest])
        shown.append(got)
    assert np.all(np.diff(shown) > 0.0) and shown[0] > 100.0


def test_a_field_below_keep_nits_is_bit_identical(model):
    """Addendum 2 (1): the heavy-tailed field scaled so that its maximum is 90 nits — nothing reaches the 100-nit floor of
    the target: not a single pixel changes (on hardware a 100-nit speck in EVERY zone makes 0.02-0.03 nits of haze)."""
    img, stars = star_population(model, 7, "heavy", 0.0, scale=90.0 / 200.0)
    assert lum(img).max() == 90.0
    out = balance_image(model, img, StarfieldParams())
    assert np.array_equal(out["img"], img) and np.all(out["scale"] == 1.0)
    assert not np.array_equal(balance_image(model, img, StarfieldParams(keep_nits=0.0))["img"], img)   # the floor is what spares it


def test_a_gravity_like_field_only_loses_a_little_of_its_few_bright_stars(model):
    """Addendum 2 (2): median 2 nits, a few stars above 100 — only those are touched, and the floor IS their target.
    DEFAULTS (even 0.8): 102 -> 100.4, 130 -> 105.4, 175 -> 111.8 (= 175^0.2 x 100^0.8), 200 -> 114.9; the round-6
    combination (even 0.6): 100.8 / 111.1 / 125.1 / 132.0. The geometric mean WITHOUT the floor would flatten the field
    (204 stars touched, 200 -> 7 nits): keep_nits is what protects it."""
    img, stars = star_population(model, 7, "heavy", 0.0)
    lv = np.array([s[2] for s in stars])
    for sp, want in ((StarfieldParams(), (100.4, 105.4, 111.8, 114.9)), (StarfieldParams(**R6), (100.8, 111.1, 125.1, 132.0))):
        out = balance_image(model, img, sp)
        shown = levels(out["img"], stars)
        touched = shown != lv
        assert np.array_equal(touched, lv > 100.0) and touched.sum() == 4                # 102, 130, 175, 200
        assert np.array_equal(out["img"][:, lum(img) <= 100.0], img[:, lum(img) <= 100.0])
        at = lambda nits: float(shown[np.argmin(np.abs(lv - nits))])
        assert [at(v) for v in (102.0, 130.0, 175.0, 200.0)] == pytest.approx(list(want), rel=0.01)
        assert out["target"][out["spk"]].max() == 100.0                                   # the floor IS the target everywhere
        assert order_kept(stars, shown, model)[0]
    bare = levels(balance_image(model, img, StarfieldParams(keep_nits=0.0))["img"], stars)
    assert int((bare != lv).sum()) > 150 and float(bare[np.argmax(lv)]) < 10.0           # without the floor: flattened


def test_christmas_lights_with_the_defaults_keep_the_haze_win(model):
    """Round 7 DEFAULTS (k 0, even 0.8, keep 100) on the Christmas-lights field (632 saturated specks 200 .. 1795 nits on
    code-0 black): target = the tapered geometric mean ~690 nits, the 268 specks above it are pulled 80 % of the way —
    out 200 .. 965; mean drive of the speck zones 0.0765 -> 0.0619 (-19 %), predicted veil -19 %, veil std -17 % (the
    round-6 combination: 74 touched, veil -1 %; the old maths even 1: out <= 881, veil -24 %). Hue exact. ORDER: even 0.8
    nearly equalises, so neighbourhoods with different targets can swap near-equal specks — 4 of 3625 neighbouring pairs
    (input ratio > 1.2) end inverted, by at most 2.4 %; none with an input ratio > 2 (old maths: 208 pairs, up to 15 %)."""
    img, stars = star_population(model, 11, "lights", 0.0)
    lv = np.array([s[2] for s in stars])
    res = predict(model, img, StarfieldParams())
    out = res["balanced"]
    shown = levels(out["img"], stars)
    lit = lum(img) > 0.0
    scale = out["scale"][lit]
    for ch in range(3):                                                                   # hue: every lit channel x the SAME scale
        on = img[ch][lit] > 0.0
        assert np.allclose((out["img"][ch][lit] / np.where(on, img[ch][lit], 1.0))[on], scale[on], rtol=1e-12)
    assert np.array_equal(out["img"][:, ~lit], img[:, ~lit]) and np.all(out["img"][img == 0.0] == 0.0)
    assert np.all(shown <= lv * (1 + 1e-12)) and shown.min() >= 200.0 * (1 - 1e-9)
    assert shown.max() == pytest.approx(965.0, rel=0.03) and 240 <= int((shown < lv * (1 - 1e-9)).sum()) <= 300
    assert float(np.median(out["target"][out["spk"]])) == pytest.approx(693.0, rel=0.05)
    i = int(np.argmax(lv))
    assert shown[i] == pytest.approx(lv[i] ** 0.2 * out["target_pixel"][stars[i][0], stars[i][1]] ** 0.8, rel=0.02)
    spk = out["spk"]
    assert res["drives_on"][spk].mean() < 0.85 * res["drives_off"][spk].mean()            # the haze proxy: -19 %
    assert res["veil_on"] < 0.85 * res["veil_off"] and res["veil_std_on"] < 0.9 * res["veil_std_off"]
    pos = np.array([(y / model.ch, x / model.cw) for y, x, _ in stars])
    close = np.abs(pos[:, None, :] - pos[None, :, :]).max(axis=2) <= 3
    a, b = np.nonzero(close & (lv[:, None] > 1.2 * lv[None, :]))
    inv = shown[a] < shown[b] * (1 - 1e-9)
    assert int(inv.sum()) <= 8 and (shown[b] / shown[a])[inv].max(initial=1.0) < 1.03     # pinned: 4 pairs, <= 2.4 %
    a2, b2 = np.nonzero(close & (lv[:, None] > 2.0 * lv[None, :]))
    assert np.all(shown[a2] >= shown[b2] * (1 - 1e-9))


def test_christmas_lights_with_the_round_6_combination_keep_hue_and_order(model):
    """Addendum 2 (3) with the R6 combination pinned EXPLICITLY (k 1 / even 0.6; the default until round 7): tiny saturated
    specks at 200 .. 1800 nits on code-0 black, some zones holding several. Hue exactly
    preserved (one scale per pixel), order kept (0 of 3625 neighbouring pairs inverted; the old maths inverted 208), the
    brightest land at peak^0.4 x target^0.6. NUMBERS: 632 specks 200 .. 1795 nits (median 595); target = mean + 1 std of
    ln peak = ~1290 nits, so only the 74 specks above it are touched — out 200 .. 1583; predicted zone-drive spread
    0.767 -> 0.764 (the old maths, k 0 / even 1: 268 touched, out 200 .. 881, drive std 0.059 -> 0.049, veil -25 %). With
    k = 1 the option does LITTLE on this content — why round 7 made the spread term a tunable."""
    img, stars = star_population(model, 11, "lights", 0.0)
    lv = np.array([s[2] for s in stars])
    res = predict(model, img, StarfieldParams(**R6))
    out = res["balanced"]
    shown = levels(out["img"], stars)
    lit = lum(img) > 0.0
    ratio = out["img"][:, lit] / np.where(img[:, lit] > 0.0, img[:, lit], 1.0)
    scale = out["scale"][lit]
    for ch in range(3):                                                                   # hue: every lit channel x the SAME scale
        on = img[ch][lit] > 0.0
        assert np.allclose(ratio[ch][on], scale[on], rtol=1e-12)
    assert np.array_equal(out["img"][:, ~lit], img[:, ~lit]) and np.all(out["img"][img == 0.0] == 0.0)
    kept, pairs = order_kept(stars, shown, model)
    assert kept and pairs > 300
    assert shown.max() < 0.9 * lv.max() and shown.min() >= 200.0 * (1 - 1e-9) and np.all(shown <= lv * (1 + 1e-12))
    assert 60 <= int((shown < lv * (1 - 1e-9)).sum()) <= 90
    t_med = float(np.median(out["target"][out["spk"]]))
    assert 1150.0 < t_med < 1450.0                                                        # mean + 1 std of ln(200 .. 1800)
    i = int(np.argmax(lv))
    assert shown[i] == pytest.approx(lv[i] ** 0.4 * out["target_pixel"][stars[i][0], stars[i][1]] ** 0.6, rel=0.02)
    assert res["drive_spread_on"] < res["drive_spread_off"]
    old = predict(model, img, SP())                                                       # the old maths on the same frame
    assert old["veil_on"] < 0.8 * old["veil_off"] and levels(old["balanced"]["img"], stars).max() < 1000.0
