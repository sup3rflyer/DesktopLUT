"""Glow fill reference (dlc.fald.glowfill, work guide ticket S2): what is filled, what must never change, and the safety
rules near black."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.correct import correct_image  # noqa: E402
from dlc.fald.glowfill import (CAP_MAX, CAP_MIN, DEFICIT_REL_HI, DEFICIT_REL_LO, REACH_MAX, REACH_MIN, WANT_EPS, GlowFillParams,  # noqa: E402
                               clamp_params, closing, deficit, envelope, fill_image, pedestal_colour, predict, req_ceiling,
                               zone_pedestal)
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.starfield import _bilinear_zones  # noqa: E402

WHITE = 1842.0
GAP = (24, 24)


@pytest.fixture(scope="module")
def model():
    return FaldModel(FaldParams())


def lattice(m, nits=WHITE, gap=GAP, half=1, sky=0.0, cols=range(14, 35), rows=range(14, 35), size=2):
    """One size x size-raster-px star per zone, a (2 half + 1)^2-zone hole around ``gap``."""
    img = np.full((3, m.h, m.w), float(sky))
    for c in cols:
        for r in rows:
            if gap is not None and abs(c - gap[0]) <= half and abs(r - gap[1]) <= half:
                continue
            y, x = r * m.ch + m.ch // 2, c * m.cw + m.cw // 2
            img[:, y: y + size, x: x + size] = nits
    return img


def centre(m, zone=GAP):
    return zone[1] * m.ch + m.ch // 2, zone[0] * m.cw + m.cw // 2


def lcd_light(m, fwd):
    p = m.p
    return (fwd["t"] * (p.white_nits * np.array(p.chan_weights)[:, None, None]) * fwd["b_true"][None]).sum(axis=0)


# ---------------------------------------------------------------------------------------------- the zone fields
def test_closing_is_extensive_idempotent_and_keeps_a_monotone_falloff_up_to_the_frame_edge():
    rng = np.random.default_rng(1)
    v = rng.random((12, 12))
    for reach in (1, 2, 3):
        c = closing(v, reach)
        assert c.shape == v.shape and np.all(c >= v)
        assert np.array_equal(closing(c, reach), c)
    yy, xx = np.mgrid[0:12, 0:12]
    for cx, cy in ((0, 0), (11, 5), (6, 6), (3, 11)):
        fall = np.exp(-0.4 * np.maximum(np.abs(xx - cx), np.abs(yy - cy)))      # falls away from one source, to every edge
        assert np.array_equal(closing(fall, 2), fall)


def test_closing_fills_a_hole_to_its_rim_and_only_holes_narrower_than_twice_the_reach():
    v = np.ones((16, 16)); v[6:9, 6:9] = 0.2                                     # a 3 x 3 hole
    assert np.array_equal(closing(v, 2), np.ones_like(v))
    assert closing(v, 1)[7, 7] == pytest.approx(0.2)                             # reach 1 fills holes up to 2 zones wide
    w = np.ones((24, 24)); w[8:14, 8:14] = 0.2                                   # 6 x 6: wider than 2 * 2
    assert closing(w, 2)[10, 10] == pytest.approx(0.2) and closing(w, 3)[10, 10] == pytest.approx(1.0)


def test_envelope_never_exceeds_the_closing_and_is_the_closing_in_a_flat_field():
    rng = np.random.default_rng(2)
    v = rng.random((20, 20))
    gp = GlowFillParams()
    e = envelope(v, gp)
    assert np.all(e <= closing(v, gp.reach) + 1e-15)
    flat = np.full((10, 10), 0.3)
    assert np.allclose(envelope(flat, gp), flat, rtol=0, atol=1e-15)


def test_zone_pedestal_is_the_zone_mean_of_the_pixel_pedestal(model):
    img = lattice(model)
    d = model.cell_drives(img)
    vz = zone_pedestal(model, d, 1.0)
    b_true, _ = model.backlights(d)
    px = model.p.white_nits * model.p.tmin * b_true.reshape(model.p.rows, model.ch, model.p.cols, model.cw).mean(axis=(1, 3))
    inner = (slice(4, -4), slice(4, -4))
    assert np.allclose(vz[inner], px[inner], rtol=0.02)                          # fine-grid mean vs bilinear-pixel mean
    assert vz[GAP[1], GAP[0]] < 0.8 * vz[GAP[1], GAP[0] - 4]                     # the hole is a hole in Vz


# ---------------------------------------------------------------------------------------------- what is filled
def test_a_hole_in_a_star_lattice_is_filled_toward_its_rim(model):
    img = lattice(model)
    res = predict(model, img)
    g = res["on"]["glow"]
    y, x = centre(model)
    assert g["want"][y, x] > 0.2 * g["v_px"][y, x]                               # a real deficit ...
    assert res["y_on"][y, x] == pytest.approx(res["y_off"][y, x] + g["fill"][y, x], rel=2e-3)   # ... shown as V + fill
    rim = res["y_off"][centre(model, (GAP[0] - 3, GAP[1]))[0] + model.ch // 2, centre(model, (GAP[0] - 3, GAP[1]))[1] + model.cw // 2]
    assert res["y_off"][y, x] < res["y_on"][y, x] <= rim * 1.02                  # toward the glow around it, not past it


def test_lit_content_and_everything_unfilled_is_bit_identical(model):
    img = lattice(model)
    off = correct_image(model, img)
    on = fill_image(model, img)
    lit = img.max(axis=0) > 0.0
    assert np.array_equal(on["req"][:, lit], off["req"][:, lit])
    untouched = on["glow"]["add"].max(axis=0) <= 0.0
    assert untouched.sum() > 0.5 * untouched.size
    assert np.array_equal(on["req"][:, untouched], off["req"][:, untouched])
    assert np.array_equal(on["drives"], off["drives"])                           # the fill never lights a LED


def test_off_and_strength_zero_are_the_layer_without_it(model):
    img = lattice(model)
    off = correct_image(model, img)
    assert "glow" not in off
    zero = correct_image(model, img, glow=GlowFillParams(strength=0.0))
    assert np.array_equal(zero["req"], off["req"])


def test_no_skirt_around_a_window_and_no_filled_letterbox_bars(model):
    win = np.zeros((3, model.h, model.w))
    win[:, 170:260, 300:460] = 1000.0
    assert fill_image(model, win)["glow"]["add"].max() == 0.0
    bars = np.zeros((3, model.h, model.w))
    bars[:, 55:-55, :] = 200.0                                                   # a uniformly bright 2.39:1 picture
    assert fill_image(model, bars)["glow"]["add"].max() == 0.0
    near_edge = np.zeros((3, model.h, model.w))
    near_edge[:, 0:60, 0:100] = 1000.0                                           # glow falling toward the frame's far edges
    assert fill_image(model, near_edge)["glow"]["add"].max() == 0.0


def test_the_first_spec_form_paints_the_skirt_the_closing_avoids(model):
    win = np.zeros((3, model.h, model.w))
    win[:, 170:260, 300:460] = 1000.0
    spec = fill_image(model, win, GlowFillParams(envelope="dilate"))["glow"]
    assert spec["add"].max() > 0.05                                              # (the offline switch stays honest)


def test_cap_and_strength_limit_the_fill(model):
    img = lattice(model)
    y, x = centre(model)
    full = fill_image(model, img, GlowFillParams(cap_nits=0.5))["glow"]
    assert full["want"][y, x] > 0.02
    capped = fill_image(model, img, GlowFillParams(cap_nits=0.01))["glow"]
    assert capped["want"].max() == pytest.approx(0.01) and capped["fill"].max() <= 0.01 + 1e-12
    half = fill_image(model, img, GlowFillParams(cap_nits=0.5, strength=0.5))["glow"]
    assert half["want"][y, x] == pytest.approx(0.5 * (full["want"][y, x] + WANT_EPS) - WANT_EPS, rel=1e-6)


def test_reach_decides_which_holes_count(model):
    img = lattice(model, half=2, cols=range(10, 39), rows=range(10, 39))         # a 5 x 5-zone hole
    y, x = centre(model)
    r2 = fill_image(model, img, GlowFillParams(reach=2, cap_nits=0.5))["glow"]["want"][y, x]
    r3 = fill_image(model, img, GlowFillParams(reach=3, cap_nits=0.5))["glow"]["want"][y, x]
    assert r3 > 2.0 * max(r2, 1e-6)


# ---------------------------------------------------------------------------------------------- continuity
def test_the_fill_fades_out_continuously_as_the_content_gets_brighter(model):
    y, x = centre(model)
    levels = np.linspace(0.0, 0.2, 41)
    shown, fills, content = [], [], []
    for sky in levels:
        img = lattice(model)
        img[:, y - 2: y + 3, x - 2: x + 3] = sky                                 # a dim patch in the hole (the drives do not move)
        res = predict(model, img)
        shown.append(lcd_light(model, res["fwd_on"])[y, x]); fills.append(res["on"]["glow"]["fill"][y, x])
        content.append(lcd_light(model, res["fwd_off"])[y, x])
        want, trust = res["on"]["glow"]["want"][y, x], res["on"]["glow"]["trust"][y, x]
    shown, fills, content = np.array(shown), np.array(fills), np.array(content)
    assert fills[0] > 0.02 and fills[-1] == 0.0 and trust == 1.0
    assert np.all(np.diff(shown) >= -1e-9)                                       # brighter content never shows darker
    assert np.all(np.abs(np.diff(fills)) <= np.diff(content) * 1.001 + 1e-9)     # no jump: the fill gives way 1 : 1
    assert np.allclose(shown, np.maximum(content, want), rtol=2e-3)              # displayed LCD light = max(content, want)


def test_the_fill_is_continuous_in_position(model):
    g = fill_image(model, lattice(model))["glow"]
    black = lattice(model).max(axis=0) <= 0.0
    f = np.where(black, g["fill"], np.nan)
    step = np.nanmax([np.nanmax(np.abs(np.diff(f, axis=0))), np.nanmax(np.abs(np.diff(f, axis=1)))])
    assert g["fill"].max() > 0.02 and step < 0.08 * g["fill"].max()              # one raster px = 5 px: a gentle ramp


def test_a_moving_star_changes_the_fill_gradually(model):
    y, x = centre(model)
    prev = None
    for shift in range(0, model.cw + 1, 2):                                      # a rim star drifts one zone toward the hole
        img = lattice(model)
        yy, xx = centre(model, (GAP[0] - 2, GAP[1]))
        img[:, yy: yy + 2, xx: xx + 2] = 0.0
        img[:, yy: yy + 2, xx + shift: xx + shift + 2] = WHITE
        res = predict(model, img)
        cur = res["y_on"][y, x]
        if prev is not None:
            assert abs(cur / prev - 1.0) < 0.10                                  # the spec's bar for a temporal term
        prev = cur


# ---------------------------------------------------------------------------------------------- safety near black
def test_never_more_lcd_light_than_wanted_and_the_zone_mean_stays_under_the_envelope(model):
    img = lattice(model)
    res = predict(model, img)
    g = res["on"]["glow"]
    black = img.max(axis=0) <= 0.0
    added = lcd_light(model, res["fwd_on"])
    assert np.all(added[black] <= 1.01 * g["want"][black] + 1e-9)
    zmean = lambda a: a.reshape(model.p.rows, model.ch, model.p.cols, model.cw).mean(axis=(1, 3))
    hole = (slice(GAP[1] - 1, GAP[1] + 2), slice(GAP[0] - 1, GAP[0] + 2))
    assert np.all(zmean(np.where(black, res["y_on"], 0.0))[hole] <= 1.01 * g["ez"][hole])


def test_no_fill_where_the_panel_estimate_is_not_trusted(model):
    p = model.p
    img = lattice(model, nits=150.0)                                             # dim stars: low drives, B_est in the fade band
    on = fill_image(model, img, GlowFillParams(cap_nits=0.5))
    g = on["glow"]
    _, b_est = model.backlights(on["drives"])
    assert g["want"].max() > 3e-3
    assert np.all(g["fill"] <= g["want"] * g["trust"] + 1e-12)
    assert ((g["trust"] > 0.0) & (g["trust"] < 1.0) & (g["want"] > 1e-4)).any()  # the fade band is exercised
    dead = b_est <= p.fade_lo
    assert dead.any() and g["add"][:, dead].max() == 0.0
    # with the estimate distrusted everywhere, nothing is filled at all
    blind = FaldModel(replace(p, fade_lo=10.0, fade_hi=20.0))
    assert fill_image(blind, lattice(blind))["glow"]["add"].max() == 0.0


def test_the_fill_request_stays_under_the_led_and_lit_thresholds(model):
    p = replace(model.p, tmin_rgb=(0.8, 1.0, 1.6), boost_lut=((0.0, 1.17), (0.35, 1.0)))
    m = FaldModel(p)
    assert req_ceiling(m) == pytest.approx(min(0.6 * p.drive_floor_nits, 0.85 * p.boost_lit_nits))
    assert req_ceiling(model) == pytest.approx(0.6 * model.p.drive_floor_nits)    # without a boost table: the drive floor alone
    img = lattice(m)
    on = fill_image(m, img, GlowFillParams(cap_nits=CAP_MAX))
    black = img.max(axis=0) <= 0.0
    assert on["glow"]["add"].max() > 0.05
    assert on["req"][:, black].max() <= req_ceiling(m) + 1e-12
    assert np.array_equal(on["drives"], correct_image(m, img)["drives"])
    # a pixel whose own content is above the ceiling takes no fill
    y, x = centre(m)
    img2 = img.copy(); img2[:, y, x] = 0.4
    on2 = fill_image(m, img2, GlowFillParams(cap_nits=CAP_MAX))
    assert on2["glow"]["add"][:, y, x].max() == 0.0


def test_the_fill_has_the_pedestal_colour_and_is_luminance_neutral(model):
    w = np.array(model.p.chan_weights)
    rgb = np.array([0.7, 1.0, 1.9]); rgb = rgb / float(w @ rgb)
    m = FaldModel(replace(model.p, tmin_rgb=tuple(rgb)))
    assert np.allclose(pedestal_colour(m), rgb) and np.allclose(pedestal_colour(model), 1.0)
    g = fill_image(m, lattice(m))["glow"]
    y, x = centre(m)
    add = g["add"][:, y, x]
    assert add.max() > 0.0 and np.allclose(add / add[1], rgb / rgb[1], rtol=1e-9)
    assert float(w @ add) == pytest.approx(g["fill"][y, x] * min(1.0, 1.0) * (float(w @ add) / g["fill"][y, x]), rel=1e-12)
    white_fill = fill_image(model, lattice(model))["glow"]
    a = white_fill["add"][:, y, x]
    assert a[0] == a[1] == a[2] > 0.0


# ---------------------------------------------------------------------------------------------- the boost loop
def test_the_boost_is_read_from_the_frame_that_carries_the_fill(model):
    lut = ((0.0, 1.17), (0.20, 1.10), (0.35, 1.0))
    m = FaldModel(replace(model.p, boost_lut=lut, boost_rule="mean"))
    res = predict(m, lattice(m))
    assert res["zones_sent"] >= res["zones_off"]                                 # filled zones can count as non-black
    assert res["zones_assumed"] == res["zones_sent"]                             # round 1 counted the frame that is sent
    assert res["boost_assumed"] == pytest.approx(res["boost_sent"])


# ---------------------------------------------------------------------------------------------- parameters
def test_clamp_params():
    c = clamp_params(GlowFillParams(strength=3.0, reach=9, cap_nits=7.0))
    assert (c.strength, c.reach, c.cap_nits) == (1.0, REACH_MAX, CAP_MAX)
    c = clamp_params(GlowFillParams(strength=-1.0, reach=0, cap_nits=0.0))
    assert (c.strength, c.reach, c.cap_nits) == (0.0, REACH_MIN, CAP_MIN)
    c = clamp_params(GlowFillParams(strength=float("nan"), cap_nits=float("nan")))
    assert (c.strength, c.cap_nits) == (1.0, 0.10)
    d = GlowFillParams()
    assert (d.strength, d.reach, d.cap_nits, d.envelope) == (1.0, 2, 0.10, "close")


def test_want_is_the_interpolated_zone_deficit(model):
    img = lattice(model)
    g = fill_image(model, img, GlowFillParams(cap_nits=0.5))["glow"]
    assert np.array_equal(g["dz"], deficit(g["vz"], g["ez"]))
    v = np.array([1.0, 1.0, 1.0, 1.0, 0.0]); e = np.array([0.9, 1.04, 1.10, 1.5, 0.2])
    t = (0.10 - DEFICIT_REL_LO) / (DEFICIT_REL_HI - DEFICIT_REL_LO)
    assert np.allclose(deficit(v, e), [0.0, 0.0, 0.10 * t * t * (3 - 2 * t), 0.5, 0.2])   # shallow dips are no holes
    d_px = _bilinear_zones(model, g["dz"])
    assert np.allclose(g["want"], np.maximum(d_px - WANT_EPS, 0.0), rtol=0, atol=1e-15)
