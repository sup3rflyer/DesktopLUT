"""Coloured (per-channel) pedestal, 2026-09-13: FaldParams.tmin_rgb + ped_mode ("white" | "channel").

"white" must be byte-for-byte the pre-2026-09-13 layer (the GUI toggle OFF); "channel" subtracts the
measured pedestal colour per channel and floors each channel on its own.
"""
import struct
import numpy as np
import pytest
pytest.importorskip("scipy")
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.correct import correct_image, reference_pedestal, reference_pedestal_rgb, pedestal_adjust, pedestal_multipliers
from dlc.fald.export import export_panel_params, MAGIC, MAGIC2

METER = (1988.0, 1120.0)
FULL = (0.0, 0.0, 1.0, 1.0)
WHITE = (1023, 1023, 1023)


def rect(x0, y0, w, h):
    return (x0 / 3840, y0 / 2160, w / 3840, h / 2160)


# native black-field leak colour (doc §35): bluer than white, normalised so sum(w * m) = 1
M_RGB = (0.756, 1.057, 1.366)
BASE = dict(est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, drive_dim=0.108, tmin=1.5e-3)


def _norm(m, w=(0.305, 0.596, 0.099)):
    m = np.asarray(m, float); return tuple(m / float(np.dot(w, m)))


def test_tmin_vec_modes():
    p = FaldParams(**BASE)
    assert np.allclose(p.tmin_vec(), p.tmin)                                      # no colour -> white
    p = FaldParams(**BASE, tmin_rgb=_norm(M_RGB))
    assert np.allclose(p.tmin_vec(), p.tmin), "white mode ignores tmin_rgb"
    p = FaldParams(**BASE, tmin_rgb=_norm(M_RGB), ped_mode="channel")
    tv = p.tmin_vec()
    assert tv[2] > tv[1] > tv[0] and abs(float(np.dot(p.chan_weights, tv)) - p.tmin) < 1e-12
    with pytest.raises(ValueError):
        FaldParams(**BASE, ped_mode="hue").tmin_vec()


def test_forward_luminance_is_unchanged_by_the_pedestal_colour():
    # sum(w * m) = 1: the Y the meter sees is the same, only its colour moves toward blue
    shapes = [((0, 0, 0), FULL), (WHITE, rect(1988 + 110, 1120 - 100, 200, 200))]
    mw = FaldModel(FaldParams(**BASE))
    mc = FaldModel(FaldParams(**BASE, tmin_rgb=_norm(M_RGB), ped_mode="channel"))
    yw, yc = mw.meter(shapes, METER), mc.meter(shapes, METER)
    assert yw.sum() > 0.01, "the pattern must leak onto the meter"
    assert abs(yc.sum() / yw.sum() - 1.0) < 1e-9
    assert yc[2] > yw[2] and yc[0] < yw[0]


def test_reference_pedestal_rgb_is_the_luminance_times_the_multipliers():
    m = FaldModel(FaldParams(**BASE, tmin_rgb=_norm(M_RGB), ped_mode="channel"))
    img = m.render([((307, 307, 307), FULL)])
    ref = reference_pedestal(m, img); rgb = reference_pedestal_rgb(m, img)
    assert rgb.shape == (3,) + ref.shape
    assert np.allclose(rgb, ref[None] * pedestal_multipliers(m)[:, None, None])
    assert np.allclose((rgb * np.array(m.p.chan_weights)[:, None, None]).sum(0), ref)


def test_pedestal_adjust_white_equals_the_2026_09_12_rule():
    rng = np.random.default_rng(1)
    img = rng.uniform(0, 3, (3, 5, 7)); delta = np.repeat(rng.uniform(-2, 1, (1, 5, 7)), 3, axis=0)
    adj, floored = pedestal_adjust(delta, img, "white")
    darkest = img.min(axis=0, keepdims=True)
    floored_amt = np.maximum(-delta - darkest, 0.0)
    expect = np.where(delta < 0.0, delta + floored_amt, delta)
    assert np.allclose(adj, expect) and np.array_equal(floored, (floored_amt > 0)[0])


def test_pedestal_adjust_channel_floors_each_channel_on_its_own():
    img = np.array([[[1.0]], [[0.5]], [[0.0]]])                # dim orange-ish pixel, blue already at zero
    delta = np.array([[[-0.3]], [[-0.4]], [[-0.6]]])           # blue-heavy excess to subtract
    adj, floored = pedestal_adjust(delta, img, "channel")
    assert np.allclose(adj[:, 0, 0], [-0.3, -0.4, 0.0]) and floored[0, 0]
    adj_w, floored_w = pedestal_adjust(delta, img, "white")   # the vector rule cannot subtract anything here
    assert np.allclose(adj_w, 0.0) and floored_w[0, 0]
    with pytest.raises(ValueError):
        pedestal_adjust(delta, img, "hue")


def test_channel_mode_subtracts_more_blue_next_to_a_highlight():
    bg = ((307, 307, 307), FULL)                                # ≈ 10-nit grey with a 200-px white one cell away
    shapes = [bg, (WHITE, rect(1988 + 110, 1120 - 100, 200, 200))]
    pw = FaldParams(**BASE, gain_smooth_cells=0.0)
    pc = FaldParams(**BASE, gain_smooth_cells=0.0, tmin_rgb=_norm(M_RGB), ped_mode="channel")
    mw, mc = FaldModel(pw), FaldModel(pc)
    img = mw.render(shapes); mask = mw.aperture_mask(METER)
    rw = correct_image(mw, img)["req"][:, mask].mean(axis=1)
    rc = correct_image(mc, img)["req"][:, mask].mean(axis=1)
    assert np.allclose(rw[0], rw[1]) and np.allclose(rw[1], rw[2]), "white mode: grey stays grey"
    assert rc[2] < rw[2] and rc[0] > rw[0], "channel mode: more blue removed, less red"
    # the model's own view: the corrected frame comes out on target in luminance in both modes, and grey
    # (its own-field pedestal colour) in channel mode — the panel's blue leak is what got removed
    w = np.array(pw.chan_weights)[:, None, None]
    base = mw.render([bg])
    target = (base * w)[:, mask].sum(0).mean() + reference_pedestal(mw, base)[mask].mean()
    for m, req in ((mw, correct_image(mw, img)["req"]), (mc, correct_image(mc, img)["req"])):
        assert abs(m.meter_img(req, METER).sum() / target - 1.0) < 0.005
    own = mc.meter([bg], METER); got = mc.meter_img(correct_image(mc, img)["req"], METER)
    assert np.allclose(got / got.sum(), own / own.sum(), atol=2e-3), "channel mode restores the uniform field's colour"


def test_channel_mode_with_a_white_pedestal_still_removes_the_ring():
    p = FaldParams(**BASE, gain_smooth_cells=0.0, ped_mode="channel")   # no colour: channel == white on grey
    m = FaldModel(p)
    bg = ((307, 307, 307), FULL)
    shapes = [bg, (WHITE, rect(1950 - 110 - 200, 1110 - 100, 200, 200))]
    base = m.render([bg]); img = m.render(shapes)
    w = np.array(p.chan_weights)[:, None, None]; mask = m.aperture_mask(METER)
    target = (base * w)[:, mask].sum(0).mean() + reference_pedestal(m, base)[mask].mean()
    after = m.meter_img(correct_image(m, img)["req"], METER).sum() / target
    assert abs(after - 1.0) < 0.005


def test_export_writes_fld1_without_a_colour_and_fld2_with_one(tmp_path):
    kw = dict(est_phase_px=-18.0, est_phase_py=-24.5, est_aniso=0.85, est_support_cells=5, kernel_pnorm=1.75,
              core_mm=10.8, tail_mm=32.4, tail_frac=0.45)
    info1 = export_panel_params(FaldModel(FaldParams(**kw)), tmp_path / "p1.bin")
    b1 = (tmp_path / "p1.bin").read_bytes()
    assert info1["format"] == "FLD1" and struct.unpack("<I", b1[:4])[0] == MAGIC and info1["header_bytes"] == 128
    m = _norm(M_RGB)
    info2 = export_panel_params(FaldModel(FaldParams(**kw, tmin_rgb=m, ped_mode="channel")), tmp_path / "p2.bin")
    b2 = (tmp_path / "p2.bin").read_bytes()
    assert info2["format"] == "FLD2" and struct.unpack("<I", b2[:4])[0] == MAGIC2
    assert len(b2) == len(b1) + 32 and b2[4:128] == b1[4:128]                 # same 32 words, 8 more
    assert struct.unpack("<3f", b2[128:140]) == pytest.approx(m, abs=1e-6)
    assert struct.unpack("<I", b2[140:144])[0] == 1 and b2[144:160] == b"\0" * 16
    assert b2[160:] == b1[128:]                                               # tables untouched
    # white mode with a colour still ships the colour (the GUI toggle decides) and records mode 0
    export_panel_params(FaldModel(FaldParams(**kw, tmin_rgb=m)), tmp_path / "p3.bin")
    b3 = (tmp_path / "p3.bin").read_bytes()
    assert struct.unpack("<I", b3[:4])[0] == MAGIC2 and struct.unpack("<I", b3[140:144])[0] == 0


def test_colour_part_split_defaults_to_plain_channel_mode_and_unfades_on_request():
    bg = ((code_of(0.5),) * 3, FULL)                            # 0.5-nit grey: inside the lum fade (weight ~0)
    shapes = [bg, (WHITE, rect(1988 + 110, 1120 - 100, 200, 200))]
    base = dict(BASE, gain_smooth_cells=0.0, tmin_rgb=_norm(M_RGB), ped_mode="channel")
    m_plain = FaldModel(FaldParams(**base))
    m_same = FaldModel(FaldParams(**base, ped_chroma_gain=1.0, ped_chroma_lum_fade=None))
    m_free = FaldModel(FaldParams(**base, ped_chroma_gain=3.0, ped_chroma_lum_fade=(0.0, 0.0)))
    img = m_plain.render(shapes); mask = m_plain.aperture_mask(METER)
    r_plain = correct_image(m_plain, img)["req"]; r_same = correct_image(m_same, img)["req"]; r_free = correct_image(m_free, img)["req"]
    assert np.allclose(r_plain, r_same), "gain 1 + fade None is the plain channel mode"
    d_plain = (r_plain - img)[:, mask].mean(axis=1); d_free = (r_free - img)[:, mask].mean(axis=1)
    assert np.abs(d_plain).max() < 0.02, "at 0.5 nits the faded correction does ~nothing"
    assert d_free[2] < -0.02 and d_free[0] > 0.0, "un-faded colour part: blue down, red up on the dark grey"
    w = np.array(m_free.p.chan_weights)
    # luminance-neutral up to the per-channel floor (blue cannot go below 0 on a dark pixel) and the gain
    assert abs(float(w @ (d_free - d_plain))) < 0.1 * np.abs(d_free - d_plain).max(), "the colour part is ~luminance-neutral"


def test_export_colour_part_words(tmp_path):
    kw = dict(est_phase_px=-18.0, est_phase_py=-24.5, est_aniso=0.85, est_support_cells=5, kernel_pnorm=1.75,
              core_mm=10.8, tail_mm=32.4, tail_frac=0.45, tmin_rgb=_norm(M_RGB), ped_mode="channel")
    export_panel_params(FaldModel(FaldParams(**kw)), tmp_path / "d.bin")
    assert struct.unpack("<3f", (tmp_path / "d.bin").read_bytes()[144:156]) == (0.0, 0.0, 0.0)       # defaults
    export_panel_params(FaldModel(FaldParams(**kw, ped_chroma_gain=3.0, ped_chroma_lum_fade=(0.0, 0.0))), tmp_path / "n.bin")
    assert struct.unpack("<3f", (tmp_path / "n.bin").read_bytes()[144:156]) == pytest.approx((3.0, 0.0, 0.0))
    export_panel_params(FaldModel(FaldParams(**kw, ped_chroma_lum_fade=(0.2, 1.0))), tmp_path / "f.bin")
    assert struct.unpack("<3f", (tmp_path / "f.bin").read_bytes()[144:156]) == pytest.approx((1.0, 0.2, 1.0))


def code_of(nits):
    from dlc._pq import oetf_norm
    return int(round(oetf_norm(nits / 10000.0) * 1023))
