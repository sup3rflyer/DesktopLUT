"""Glow fill reference (dlc.fald.glowfill, work guide ticket S2): what is filled, what must never change, and the safety
rules near black."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.correct import correct_image  # noqa: E402
from dlc.fald.glowfill import (BAND_HI, BAND_LO, CAP_MAX, CAP_MIN, DEFICIT_REL_HI, DEFICIT_REL_LO, FEATHER, GUARD_ITER_MAX,  # noqa: E402
                               NEIGHBOURS, REACH_MAX, REACH_MIN, REQ_FLOOR_FRAC, REQ_LIT_FRAC, WANT_EPS, GlowFillParams,
                               band_active, band_pixel_scale, band_scale, clamp_params, closing, deficit, envelope,
                               feather_weight, fill_image, guard, pedestal_colour, predict, req_ceiling, round_fill,
                               zone_local, zone_pedestal)
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


def test_the_request_ceiling_comes_from_the_measured_levels():
    """Probe pixrule (work guide): a 2-px column at 0.298 nit does NOT make a zone LIT, 0.4 nit does; whether a 0.3-nit
    AREA lights LEDs is unmeasured (R4). The ceiling stays a factor ~1.5 under the measured point and <= 0.2 nit."""
    p = FaldParams()
    assert (REQ_FLOOR_FRAC, REQ_LIT_FRAC) == (0.4, 0.55) and (p.drive_floor_nits, p.boost_lit_nits) == (0.5, 0.35)
    with_lut = FaldModel(replace(p, boost_lut=((0.0, 1.17), (0.35, 1.0))))
    assert req_ceiling(with_lut) == pytest.approx(0.1925) and req_ceiling(FaldModel(p)) == pytest.approx(0.2)
    assert req_ceiling(with_lut) * 1.5 < 0.298 and req_ceiling(with_lut) <= 0.2
    assert GlowFillParams().cap_nits == 0.05


def test_the_fill_request_stays_under_the_led_and_lit_thresholds(model):
    # a leaky panel (tmin x 10) and the widest cap: the want reaches 0.5 nit, so the ceiling BINDS (a rule without it
    # would request 0.5 x 1.6 = 0.8 nit on the blue channel — above the 0.5-nit drive floor)
    p = replace(model.p, tmin=3e-3, tmin_rgb=(0.8, 1.0, 1.6), boost_lut=((0.0, 1.17), (0.35, 1.0)))
    m = FaldModel(p)
    assert req_ceiling(m) == pytest.approx(min(REQ_FLOOR_FRAC * p.drive_floor_nits, REQ_LIT_FRAC * p.boost_lit_nits)) == pytest.approx(0.1925)
    assert req_ceiling(model) == pytest.approx(REQ_FLOOR_FRAC * model.p.drive_floor_nits)    # without a boost table: the drive floor alone
    img = lattice(m)
    on = fill_image(m, img, GlowFillParams(cap_nits=CAP_MAX))
    black = img.max(axis=0) <= 0.0
    g = on["glow"]
    unlimited = (g["want"] * g["trust"] * 1.6)[black].max()                       # what the blue channel would be asked without it
    assert unlimited > 2.0 * req_ceiling(m) and unlimited > p.drive_floor_nits
    assert on["req"][:, black].max() == pytest.approx(req_ceiling(m), rel=1e-9)  # ... and it stops exactly there
    assert on["req"][:, black].max() < p.boost_lit_nits and on["req"][:, black].max() < p.drive_floor_nits
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
    assert (c.strength, c.cap_nits) == (1.0, 0.05)
    d = GlowFillParams()
    assert (d.strength, d.reach, d.cap_nits, d.envelope, d.band, d.band_feather) == (1.0, 2, 0.05, "close", True, True)


# ---------------------------------------------------------------------------------------------- the count-threshold band
MEAN_LUT = ((0.0, 1.17), (0.20, 1.10), (0.35, 1.0))


def _zone_stat(m, req):
    p = m.p
    z = np.power(np.maximum(req.max(axis=0), 0.0), p.boost_mean_gamma).reshape(p.rows, m.ch, p.cols, m.cw).mean(axis=(1, 3))
    lit = (req.max(axis=0) > p.boost_lit_nits).reshape(p.rows, m.ch, p.cols, m.cw).any(axis=(1, 3))
    return z / p.boost_mean_thresh, lit


def test_no_filled_zone_is_parked_at_the_firmwares_count_threshold(model):
    m = FaldModel(replace(model.p, boost_lut=MEAN_LUT, boost_rule="mean"))
    assert band_active(m) and not band_active(model) and not band_active(FaldModel(replace(model.p, boost_lut=MEAN_LUT)))
    img = lattice(m, nits=220.0)                                                 # a fill that lands right AT the threshold
    gp = GlowFillParams(cap_nits=0.5)
    free = predict(m, img, replace(gp, band=False))
    z0, lit0 = _zone_stat(m, free["on"]["req"])
    assert int((~lit0 & (np.abs(z0 - 1.0) < 0.15)).sum()) >= 4                   # without the rule: zones within +-15 % of T
    res = predict(m, img, gp)
    b = res["on"]["glow"]["band"]
    z1, lit1 = _zone_stat(m, res["on"]["req"])
    assert int((~lit1 & (np.abs(z1 - 1.0) < 0.15)).sum()) == 0                   # with it: none
    assert b["band"].sum() >= 4 and np.all(b["k"][b["band"]] < 1.0) and np.all(b["k"] <= 1.0) and np.all(b["k"][~b["band"]] == 1.0)
    t = m.p.boost_mean_thresh
    assert np.all((b["pf"][b["band0"]] >= BAND_LO * t) & (b["pf"][b["band0"]] <= BAND_HI * t))   # the prediction's band ...
    assert np.array_equal(b["band"], b["band0"] | b["guard_added"]) and np.all(b["pf"][b["guard_added"]] > BAND_HI * t)   # + the guard's
    # scaled DOWN to the band's lower edge (C16: the feather of a neighbouring band zone takes a little more)
    assert np.all(z1[b["band"]] <= BAND_LO * 1.03) and np.all(z1[b["band0"]] >= 0.85 * BAND_LO)
    assert res["zones_sent"] <= free["zones_sent"]
    # (k is formed per round: round 0's prediction is NOT the sent frame's here — the fill sits in the B_est fade band and
    # the trust moves between the rounds — which is why round 1 forms its own)
    assert not np.array_equal(res["on"]["glow"]["band"]["pf"], res["on"]["glow"]["band"]["pc"])
    assert np.all(res["on"]["req"] <= free["on"]["req"] + 1e-15)                  # never up
    # zones counted because of their CONTENT are left alone, whatever the fill adds
    sky = lattice(m, nits=220.0, sky=0.02)
    bs = fill_image(m, sky, gp)["glow"]["band"]
    assert np.all(bs["pc"] >= m.p.boost_mean_thresh) and not bs["band"].any() and np.all(bs["k"] == 1.0)
    # no mean rule / no boost table: no band, bit for bit the rule without it
    for other in (model, FaldModel(replace(model.p, boost_lut=MEAN_LUT))):
        a, c = fill_image(other, lattice(other, nits=220.0), gp), fill_image(other, lattice(other, nits=220.0), replace(gp, band=False))
        assert np.array_equal(a["req"], c["req"]) and np.all(a["glow"]["band"]["k"] == 1.0)


def test_a_gamma_transfer_fit_is_refused(model):
    sdr = FaldModel(replace(model.p, transfer="gamma", code_bits=8))
    with pytest.raises(ValueError, match="HDR only"):
        fill_image(sdr, lattice(sdr))
    assert "glow" not in correct_image(sdr, lattice(sdr))                         # the layer without it is untouched


def test_want_is_the_interpolated_zone_deficit(model):
    img = lattice(model)
    g = fill_image(model, img, GlowFillParams(cap_nits=0.5))["glow"]
    assert np.array_equal(g["dz"], deficit(g["vz"], g["ez"]))
    v = np.array([1.0, 1.0, 1.0, 1.0, 0.0]); e = np.array([0.9, 1.04, 1.10, 1.5, 0.2])
    t = (0.10 - DEFICIT_REL_LO) / (DEFICIT_REL_HI - DEFICIT_REL_LO)
    assert np.allclose(deficit(v, e), [0.0, 0.0, 0.10 * t * t * (3 - 2 * t), 0.5, 0.2])   # shallow dips are no holes
    d_px = _bilinear_zones(model, g["dz"])
    assert np.allclose(g["want"], np.maximum(d_px - WANT_EPS, 0.0), rtol=0, atol=1e-15)


# ---------------------------------------------------------------------------------------------- C16: feather + neighbour guard
def windows(m, bg, level=200.0, size_px=240, step=6, start=3):
    """200-nit windows (size_px x size_px / 2 at full resolution) at every ``step``-th zone centre on a dim sky."""
    img = np.full((3, m.h, m.w), float(bg))
    s = size_px / m.p.scale
    for zy in range(start, m.p.rows, step):
        for zx in range(start, m.p.cols, step):
            cy, cx = (zy + 0.5) * m.ch, (zx + 0.5) * m.cw
            img[:, int(cy - s / 4): int(cy + s / 4), int(cx - s / 2): int(cx + s / 2)] = level
    return img


def _round_fields(m, glow):
    """The last round's (b_true, b_est), rebuilt from its input the way correct_image forms them."""
    cur = glow["round_input"]
    d = m.cell_drives(cur)
    b_true, b_est = m.backlights(d, d, boost=m.led_boost(cur))
    return np.maximum(b_true, 0.0), b_est


def _zone_pow_mean(m, req):
    p = m.p
    return np.power(np.maximum(req.max(axis=0), 0.0), p.boost_mean_gamma).reshape(p.rows, m.ch, p.cols, m.cw).mean(axis=(1, 3))


@pytest.fixture(scope="module")
def mean_model(model):
    return FaldModel(replace(model.p, boost_lut=MEAN_LUT, boost_rule="mean"))


def test_the_band_feather_geometry(model):
    """C16 pixel rule: w = 1 on the neighbour's rectangle, C1 down to 0 at FEATHER zones; s = min(k_z, min_n 1 - (1 - k_n)
    w_n) is exactly 1 with no band zone near, k_z inside a lone band zone, and ramps (no step) across its edges."""
    assert (FEATHER, GUARD_ITER_MAX) == (0.35, 16)
    assert NEIGHBOURS == tuple((c % 3 - 1, c // 3 - 1) for c in range(9) if c != 4)       # 3 x 3 row-major, centre skipped
    u = np.linspace(0.0, 0.999, 1000)
    w = feather_weight(-1, 0, u, np.array([0.5]))[0]                                      # the left neighbour: distance = u
    assert w[0] == 1.0 and np.all(w[u >= FEATHER] == 0.0) and np.all(np.diff(w) <= 0.0)
    t = u[u < FEATHER] / FEATHER
    assert np.allclose(w[u < FEATHER], 1.0 - t * t * (3.0 - 2.0 * t), rtol=0, atol=1e-15)   # smoothstep: C1 at both ends
    corner = feather_weight(-1, -1, np.array([0.1]), np.array([0.2]))[0, 0]               # the corner: the euclidean distance
    tc = np.hypot(0.1, 0.2) / FEATHER
    assert corner == pytest.approx(1.0 - tc * tc * (3.0 - 2.0 * tc), abs=1e-15)
    p = model.p
    k = np.ones((p.rows, p.cols))
    assert np.array_equal(band_pixel_scale(model, k), np.ones((model.h, model.w)))       # EXACTLY 1 without a band zone
    k[24, 24] = 0.4
    s = band_pixel_scale(model, k)
    ys, xs = slice(24 * model.ch, 25 * model.ch), slice(24 * model.cw, 25 * model.cw)
    assert np.all(s[ys, xs] == 0.4) and np.all(s >= 0.4) and np.all(s <= 1.0)           # the band zone keeps its own k
    zx, zy, u2, v2 = zone_local(model)
    X = zx[None, :] + u2[None, :]; Y = zy[:, None] + v2[:, None]                          # pixel centres in zone units
    dist = np.hypot(np.maximum(0.0, np.maximum(24.0 - X, X - 25.0)), np.maximum(0.0, np.maximum(24.0 - Y, Y - 25.0)))
    assert np.all(s[dist >= FEATHER] == 1.0) and np.all(s[(dist > 0) & (dist < FEATHER)] < 1.0)
    row = s[24 * model.ch + model.ch // 2]
    steps = np.abs(np.diff(row))
    edge = max(steps[25 * model.cw - 1], steps[24 * model.cw - 1])                       # across the zone's two vertical edges
    assert edge < 0.25 * steps.max() and edge < 0.02                                      # a ramp, not a step (was 0.6)


def _edge_steps(m, fill, k):
    """Per zone edge where k differs across it: (the largest step of the displayed fill across that edge, the largest step
    between adjacent pixels inside the two zones, in the same direction and rows / columns)."""
    p, ch, cw = m.p, m.ch, m.cw
    out = []
    for zy in range(p.rows):
        for zx in range(p.cols):
            if zx + 1 < p.cols and k[zy, zx] != k[zy, zx + 1]:
                d = np.abs(np.diff(fill[zy * ch:(zy + 1) * ch, zx * cw:(zx + 2) * cw], axis=1))
                out.append((d[:, cw - 1].max(), np.delete(d, cw - 1, axis=1).max()))
            if zy + 1 < p.rows and k[zy, zx] != k[zy + 1, zx]:
                d = np.abs(np.diff(fill[zy * ch:(zy + 2) * ch, zx * cw:(zx + 1) * cw], axis=0))
                out.append((d[ch - 1, :].max(), np.delete(d, ch - 1, axis=0).max()))
    return np.array(out)


def test_the_feathered_band_draws_no_zone_edge(mean_model):
    """C16's point (spec test a): at every zone edge where the final k differs across it, the displayed fill steps across
    the edge no more than it does between adjacent pixels inside the two zones. The rule before C16 (the pixel's own
    zone's k) fails that on most such edges — up to a third of the local level in one pixel."""
    m = mean_model
    img = windows(m, 0.004)
    gp = GlowFillParams(cap_nits=0.05)
    g = fill_image(m, img, gp)["glow"]
    b = g["band"]
    assert b["band0"].sum() >= 20 and b["guard_added"].sum() >= 10 and g["fill"].max() > 0.01
    steps = _edge_steps(m, g["fill"], b["k"])
    assert len(steps) >= 100 and np.all(steps[:, 0] <= steps[:, 1] * (1.0 + 1e-9) + 1e-15)
    old = fill_image(m, img, replace(gp, band_feather=False))["glow"]
    s_old = _edge_steps(m, old["fill"], old["band"]["k"])
    level = old["v_px"] + old["fill"]
    assert (s_old[:, 0] > s_old[:, 1] * 1.5).sum() > 0.5 * len(s_old)                  # the seams C16 removes
    assert float(np.max(np.abs(np.diff(old["fill"], axis=1)) / np.maximum(level[:, 1:], 1e-9))) > 0.25


def test_the_neighbour_guard_keeps_every_zone_out_of_the_margin(mean_model):
    """Spec test b, on the round the evidence comes from (its fields rebuilt): every zone counted only by the fill that is
    not banded ends >= BAND_HI T, every band zone ends <= its band0 prediction (the statistic with its own zone's k on its
    own pixels), zones below BAND_LO T stay below, zones counted by their content stay counted."""
    m = mean_model
    p = m.p
    t = p.boost_mean_thresh
    gp = GlowFillParams(cap_nits=0.05)
    g = fill_image(m, windows(m, 0.004), gp)["glow"]
    b_true, b_est = _round_fields(m, g)
    b = band_scale(m, g["req_nofill"], b_true, b_est, g["dz"], g["ez"], gp)
    assert np.array_equal(b["k"], g["band"]["k"]) and np.array_equal(b["band"], g["band"]["band"])   # the fields are the round's
    assert 2 <= b["iterations"] <= GUARD_ITER_MAX and b["guard_added"].sum() >= 10
    req = g["req_nofill"]
    final = _zone_pow_mean(m, req + round_fill(m, req, b_true, b_est, g["dz"], g["ez"], gp, k=b["k"])["add"])
    assert np.allclose(final, _zone_pow_mean(m, np.where(g["add"] > 0.0, req + g["add"], req)), rtol=1e-12)
    band0_pred = _zone_pow_mean(m, req + round_fill(m, req, b_true, b_est, g["dz"], g["ez"], replace(gp, band_feather=False), k=b["k"])["add"])
    free = ~b["lit"] & (b["pc"] < t)
    counted_by_fill = free & (b["pf"] > BAND_HI * t)
    assert (counted_by_fill & ~b["band"]).sum() >= 10
    assert np.all(final[counted_by_fill & ~b["band"]] >= BAND_HI * t)
    assert np.all(final[b["band"]] <= band0_pred[b["band"]] * (1.0 + 1e-12))
    assert np.all(final[b["band0"]] <= BAND_LO * t * (1.0 + 1e-9))
    below = free & (b["pf"] < BAND_LO * t)
    assert np.all(final[below] < BAND_LO * t)
    content = b["lit"] | (b["pc"] >= t)
    assert content.sum() > 0 and np.all(b["k"][content] == 1.0) and np.all(final[content & ~b["lit"]] >= t)
    # the rule before C16 on the same round: the pixels of the band zones' neighbours keep their full fill
    old = band_scale(m, req, b_true, b_est, g["dz"], g["ez"], replace(gp, band_feather=False))
    assert not old["guard_added"].any() and np.array_equal(old["band"], b["band0"]) and old["A"] is None


def test_the_guard_is_jacobi_and_joins_only_fill_counted_zones():
    """G5's semantics on a 1 x 5 chain: zone 0 is in the band (k0 = 0); zone 1 falls below BAND_HI T only through zone
    0, zone 2 only once zone 1 has joined. Jacobi (every zone reads the previous iteration's k, as the GPU's one thread
    group does) takes two iterations to band both and a third to see nothing join; a non-candidate never joins."""
    hi = np.float32(1.25)
    k0 = np.array([[0.0, 1.0, 1.0, 1.0, 1.0]], dtype=np.float32)
    band0 = k0 < 1.0
    cand = np.array([[False, True, True, False, True]])
    pf = np.full((1, 5), np.float32(1.5), dtype=np.float32)
    a = np.zeros((8, 1, 5), dtype=np.float32)
    left = NEIGHBOURS.index((-1, 0))
    a[left, 0, 1] = 0.5          # zone 1 loses 0.5 (1 - k_0) through its left neighbour
    a[left, 0, 2] = 0.4          # zone 2 loses 0.4 (1 - k_1)
    a[left, 0, 3] = 0.9          # zone 3 would, but is no candidate
    kj = np.full((1, 5), np.float32(0.3), dtype=np.float32)
    r = guard(k0, band0, cand, kj, pf, a, hi)
    assert r["k"].dtype == np.float32 and r["iterations"] == 3
    assert r["band"].tolist() == [[True, True, True, False, False]] and r["added"].tolist() == [[False, True, True, False, False]]
    assert r["k"].tolist() == [[0.0, np.float32(0.3), np.float32(0.3), 1.0, 1.0]]
    capped = guard(k0, band0, cand, kj, pf, a, hi, iter_max=1)                           # the cap: one iteration, zone 1 only
    assert capped["added"].tolist() == [[False, True, False, False, False]] and capped["iterations"] == 1


def test_c16_is_bit_identical_away_from_the_band(mean_model, model):
    """Spec test c: a frame whose band stays empty gives the output of the rule without it, bit for bit; with band zones
    (the same round fields and k) every pixel outside the 3 x 3 neighbourhood of every band zone is untouched by the
    feather, and inside a band zone the scale never exceeds the zone's k."""
    m = mean_model
    gp = GlowFillParams(cap_nits=0.5)
    img = lattice(m)                                                             # full-white stars: the fill lands far above T
    on = fill_image(m, img, gp)
    assert not on["glow"]["band"]["band"].any() and on["glow"]["fill"].max() > 0.02
    for other in (replace(gp, band_feather=False), replace(gp, band=False)):
        assert np.array_equal(on["req"], fill_image(m, img, other)["req"])
    g = fill_image(m, windows(m, 0.004), GlowFillParams(cap_nits=0.05))["glow"]
    b_true, b_est = _round_fields(m, g)
    k = g["band"]["k"]
    args = (m, g["req_nofill"], b_true, b_est, g["dz"], g["ez"])
    new = round_fill(*args, GlowFillParams(cap_nits=0.05), k=k)
    old = round_fill(*args, GlowFillParams(cap_nits=0.05, band_feather=False), k=k)
    near = np.zeros_like(k, dtype=bool)
    for zy, zx in zip(*np.nonzero(k < 1.0)):
        near[max(zy - 1, 0): zy + 2, max(zx - 1, 0): zx + 2] = True
    far = ~np.repeat(np.repeat(near, m.ch, axis=0), m.cw, axis=1)
    assert far.sum() > 0.2 * far.size and (~far).sum() > 0
    assert np.array_equal(new["add"][:, far], old["add"][:, far]) and np.all(new["s"][far] == 1.0)
    inside = np.repeat(np.repeat(k < 1.0, m.ch, axis=0), m.cw, axis=1)
    assert np.all(new["s"][inside] <= old["s"][inside]) and np.all(new["add"][:, inside] <= old["add"][:, inside] + 1e-15)
    # (no band table at all: k is all ones and the pixel scale is exactly 1 everywhere)
    plain = fill_image(model, windows(model, 0.004), GlowFillParams(cap_nits=0.05))["glow"]
    assert np.all(plain["band"]["k"] == 1.0) and np.all(plain["s"] == 1.0)
