"""Starfield balancing in GPU order (work guide S1): the emulator's star passes + Balance (dlc/fald/gpuemu.py — the HLSL
of src/fald_shader.h line for line, FULL resolution) against the reference ``starfield.balance_image`` (the model's
scale-5 raster). Every pattern is raster-aligned (features >= 5 px): the centre pixel of each 5 x 5 block then has
exactly the raster pixel's bilinear zone coordinates, so the two must agree there to float32 rounding. Also: the
option off is the previous emulator bit for bit, and the HLSL / C++ text carries the reference's constants."""
from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald import gpuemu  # noqa: E402
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.gpuemu import Emu, clamp_star  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402
from dlc.fald.starfield import StarfieldParams, _bilinear_zones, balance_image  # noqa: E402

_SRC = Path(__file__).resolve().parents[2] / "src"
_SHADER = _SRC.parent / "shared" / "fald_shader.h"   # shared by the overlay and the DWM hook since e7f542f
_SHARED = _SRC.parent / "shared"                      # fald_panel.h: FALD_CB_BYTES (e7f542f)
_HOOK = _SRC.parent / "dwm_hook" / "hook_fald.cpp"    # the DWM hook's passes, pass for pass the overlay's (src/fald.cpp)
DIM, WHITE = 100.0, 1846.0


def SP(**kw):
    """The maths of rounds <= 5 — geometric-mean target (target_sigma 0), full pull (even 1), no absolute floor
    (keep_nits 0) — which every test written before the round-6 addenda pins; the tests of the DEFAULTS (round 7:
    even 0.8, target_sigma 0, keep_nits 100) and of the round-6 combination R6 say so."""
    return StarfieldParams(**{"even": 1.0, "target_sigma": 0.0, "keep_nits": 0.0, **kw})


R6 = {"target_sigma": 1.0, "even": 0.6}   # the round-6 defaults (spread-aware target, gentler pull): a tunable since round 7



def _small_params(**kw):
    # 960x540 at scale 5 -> 192x108 raster px; 12x12 zones of 80x45 px (16x9 raster px) — test_fald_boost_gpu's fixture
    return FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, **kw)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    p = _small_params()
    m = FaldModel(p)
    path = tmp_path_factory.mktemp("star") / "panel.bin"
    export_panel_params(m, path)
    return m, Emu(read_panel_file(path), width=p.width, height=p.height)


def _scrgb(img_nits, scale=5):
    """Model raster (3, h, w) as-if-white nits -> full-resolution scRGB (H, W, 3); grey stays grey (PQ: nits / 80)."""
    return np.ascontiguousarray(np.repeat(np.repeat(img_nits, scale, axis=1), scale, axis=2).transpose(1, 2, 0) / 80.0)


def _star(img, m, col, row, nits, dx=None, dy=None, size=1):
    """A size x size raster-px star (5 x 5 full-resolution px each) in zone (col, row); default = the zone's middle."""
    x = col * m.cw + (m.cw // 2 if dx is None else dx)
    y = row * m.ch + (m.ch // 2 if dy is None else dy)
    img[:, y:y + size, x:x + size] = nits


def _field(m, outliers=((5, 5),), levels=None, sky=0.0, zones=range(2, 10)):
    img = np.full((3, m.h, m.w), float(sky))
    for r in zones:
        for c in zones:
            lvl = WHITE if (c, r) in outliers else (DIM if levels is None else levels[(c + 3 * r) % len(levels)])
            _star(img, m, c, r, lvl)
    return img


def _both(rig, img, sp, native=False):
    """native: the raster is in the PANEL's primaries (saturated colours) -> through the emulator's own nits -> scRGB."""
    m, emu = rig
    ref = balance_image(m, img, sp)
    frame = emu.nits_to_scrgb(np.repeat(np.repeat(img, 5, axis=1), 5, axis=2)) if native else _scrgb(img)
    out = emu.run(frame, fp16_out=False, star=sp)
    return ref, out


def _assert_parity(rig, img, sp, rel=2e-5, native=False):
    m, emu = rig
    ref, out = _both(rig, img, sp, native)
    st, plan = out["star"]["stat"], out["star"]["plan"]
    assert np.allclose(st["peak"], ref["peak"], rtol=1e-6) and np.allclose(st["sparse"], ref["sparse"], atol=1e-4)
    assert np.allclose(st["b"], ref["b"], rtol=1e-6, atol=0) and np.allclose(st["solid"], ref["solid"], atol=2e-3)   # solid: LUT vs analytic drive
    # a_eff = (sum - b n) / (peak - b) in float32: the un-gated sum of a lit sky is large and the difference small
    assert np.allclose(st["a_eff"], ref["a_eff"], rtol=2e-4, atol=0.05)
    assert np.allclose(plan["w"], ref["w"], atol=1e-5) and np.allclose(plan["w_field"], ref["w_field"], atol=1e-5)
    assert np.allclose(plan["target"], ref["target"], rtol=1e-5)
    assert np.allclose(plan["near"], ref["near"], atol=2e-3) and np.array_equal(plan["spk"], ref["spk"])   # the tapered protection field, the flag
    assert np.array_equal(plan["flank"], ref["flank"]) and np.allclose(plan["wt"], ref["wt"], atol=1e-5)  # flank zones: no target weight
    lit = ref["peak"] > 0                                               # the brightest pixel's position, tie rule included
    assert np.array_equal(np.stack(np.divmod(st["arg"], 5 * m.cw))[::-1][:, lit], np.stack(ref["arg"])[:, lit])
    centre = (slice(None), slice(2, None, 5), slice(2, None, 5))       # the raster pixel's own bilinear coordinates
    bal = out["img"][centre]                                            # "img" of a star run = the BALANCED frame
    assert np.allclose(bal, ref["img"], rtol=rel, atol=2e-3 if native else 1e-9)   # (native: the matrix round trip's float32 dust)
    assert np.allclose(out["star"]["scale"][centre[1:]], ref["scale"], rtol=rel)
    # untouched content is BIT-identical in both (scale exactly 1, the source value itself)
    same_ref = ref["scale"] == 1.0
    assert np.array_equal(out["star"]["scale"][centre[1:]] == 1.0, same_ref)
    assert np.array_equal(out["img"][:, out["star"]["scale"] == 1.0], out["star"]["src"][:, out["star"]["scale"] == 1.0])
    # one scale for the whole 5 x 5 block's three channels (hue-preserving), and the rest of the layer saw the balanced frame
    assert np.array_equal(out["drive0"], emu.stat_drive(out["img"])[0])
    return ref, out


# ------------------------------------------------------------------------------------------------ the sampler
def test_zone_sampler_equals_the_reference_bilinear_between_zone_centres(rig):
    """Hardware bilinear + clamp on a one-texel-per-zone texture at FineUV == starfield._bilinear_zones, including the
    hold outside the outermost zone centres (first / last half zone of the frame)."""
    m, emu = rig
    z = np.random.default_rng(3).uniform(-3.0, 7.0, (m.p.rows, m.p.cols))
    full = emu.sample_zone(z)
    assert np.allclose(full[2::5, 2::5], _bilinear_zones(m, z), rtol=0, atol=1e-12)
    held = dict(rtol=0, atol=1e-12)                                                   # z0 (1 - f) + z0 f: one ulp at most
    assert np.allclose(full[:, :40], np.repeat(full[:, :1], 40, axis=1), **held)      # left of the first centre: held
    assert np.allclose(full[:22], np.repeat(full[:1], 22, axis=0), **held)             # above the first centre: held
    assert np.allclose(full[:, -40:], np.repeat(full[:, -1:], 40, axis=1), **held)
    assert full[10, 20] == pytest.approx(z[0, 0], abs=1e-12) and full[-1, -1] == pytest.approx(z[-1, -1], abs=1e-12)
    # an interior pixel written out: texel coordinate (px + 0.5) / cell - 0.5, weights = its fractional part
    px, py = 200, 157
    tx, ty = (px + 0.5) / 80 - 0.5, (py + 0.5) / 45 - 0.5
    c0, r0 = int(np.floor(tx)), int(np.floor(ty))
    fx, fy = tx - c0, ty - r0
    want = (z[r0, c0] * (1 - fx) + z[r0, c0 + 1] * fx) * (1 - fy) + (z[r0 + 1, c0] * (1 - fx) + z[r0 + 1, c0 + 1] * fx) * fy
    assert full[py, px] == pytest.approx(want, abs=1e-12)


def test_zone_sampler_follows_a_lattice_origin(tmp_path):
    """originX / originY (a lattice that does not start at the frame corner): the zone centres move with it."""
    p = _small_params()
    export_panel_params(FaldModel(p), tmp_path / "p.bin")
    o = dict(read_panel_file(tmp_path / "p.bin"))
    o["originX"], o["originY"] = 16, 7
    emu = Emu(o, width=p.width + 16, height=p.height + 7)
    z = np.arange(144, dtype=float).reshape(12, 12)
    full = emu.sample_zone(z)
    base = Emu(read_panel_file(tmp_path / "p.bin"), width=p.width, height=p.height).sample_zone(z)
    assert np.array_equal(full[7:, 16:], base)


# ------------------------------------------------------------------------------------------------ parity with the reference
def test_field_with_outliers_matches_the_reference(rig):
    m, _ = rig
    img = _field(m, outliers=((5, 5), (8, 3)))
    ref, out = _assert_parity(rig, img, SP())
    assert ref["img"].max() < 1.4 * DIM and out["img"].max() < 1.4 * DIM          # the outliers came down onto the field
    assert out["star"]["src"].max() == pytest.approx(WHITE)
    # the statistic the layer computes is the BALANCED frame's: the outlier zones' round-0 drive fell to their neighbours'
    plain = rig[1].run(_scrgb(img), fp16_out=False)
    assert plain["star"] is None and plain["drive0"][5, 5] > 1.5 * out["drive0"][5, 5]
    assert out["drive0"][5, 5] == pytest.approx(out["drive0"][5, 6], rel=0.15)   # target = the geometric mean incl. the outliers


def test_partial_pull_target_gain_and_cap_match_the_reference(rig):
    m, _ = rig
    img = _field(m, outliers=((4, 6),))
    _assert_parity(rig, img, SP(even=0.5))
    _assert_parity(rig, img, SP(target_gain=0.5, even_reach=3))
    ref, out = _assert_parity(rig, img, SP(cap_nits=50.0))
    assert out["img"].max() == pytest.approx(50.0, rel=1e-5)
    _assert_parity(rig, img, SP(strength=0.4, peak_hi=500.0))


def test_lift_matches_the_reference_and_spares_the_sky(rig):
    m, _ = rig
    img = _field(m, outliers=((5, 5),), levels=(40.0, 100.0, 250.0), sky=0.3)
    ref, out = _assert_parity(rig, img, SP(lift=1.0))
    src = out["star"]["src"]
    sky = np.isclose(src[0], 0.3, rtol=1e-6)
    assert np.array_equal(out["img"][:, sky], src[:, sky])                            # the sky is not a speck: untouched
    dim = np.isclose(src[0], 40.0, rtol=1e-6)
    assert dim.sum() == 24 * 25 and out["img"][0][dim].min() > 41.0                  # the dim specks were lifted
    _assert_parity(rig, img, SP(lift=0.5, even=0.7))


def test_a_star_next_to_a_solid_window_is_protected(rig):
    m, _ = rig
    img = _field(m, outliers=((9, 9),), zones=range(6, 12))
    img[:, 1 * m.ch:4 * m.ch, 1 * m.cw:4 * m.cw] = 1000.0                             # solid window, zones 1..3
    _star(img, m, 5, 2, WHITE)                                                        # 2 zones right of it: protected
    img[:, :, 6 * m.cw:] = np.where(img[:, :, 6 * m.cw:] > 0, img[:, :, 6 * m.cw:], 0.0)
    ref, out = _assert_parity(rig, img, SP(reach=2))
    w = out["star"]["plan"]["w"]
    assert w[1:4, 1:4].max() == 0.0 and w[2, 5] == 0.0 and w[9, 9] == pytest.approx(1.0)
    near = (slice(None), slice(2 * 45, 3 * 45), slice(5 * 80, 6 * 80))
    assert np.array_equal(out["img"][near], out["star"]["src"][near])                 # the protected star: bit-identical
    win = (slice(None), slice(45, 4 * 45), slice(80, 4 * 80))
    assert np.array_equal(out["img"][win], out["star"]["src"][win])                   # solid content: bit-identical
    assert out["img"][0, 9 * 45:10 * 45, 9 * 80:10 * 80].max() < 0.2 * WHITE         # the far outlier is evened


def test_a_speck_beside_an_empty_zone_keeps_its_treatment(rig):
    """An EMPTY zone carries its content neighbours' mean weight and the local target: an outlier sitting at the border
    to an empty zone is pulled exactly as the reference pulls it (without the carry its weight would halve there)."""
    m, _ = rig
    img = np.zeros((3, m.h, m.w))
    for r in range(2, 10):
        for c in range(2, 10):
            if (c + r) % 2 == 0:                                                       # checkerboard: every other zone empty
                _star(img, m, c, r, DIM)
    _star(img, m, 6, 4, WHITE, dx=m.cw - 1)                                           # at the right border, zone (7, 4) is empty
    ref, out = _assert_parity(rig, img, SP())
    plan = out["star"]["plan"]
    assert not plan["spk"][4, 7] and plan["w_field"][4, 7] == pytest.approx(1.0)      # the carried weight
    assert plan["ln_pk"][4, 7] == plan["ln_t"][4, 7]                                  # an empty zone's "peak" = the target
    assert out["img"][0, 4 * 45:5 * 45, 6 * 80:7 * 80].max() < 1.5 * DIM             # fully pulled despite the empty side


def test_hue_is_preserved_by_one_scale_per_pixel(rig):
    m, emu = rig
    img = _field(m, outliers=())
    img[:, 5 * m.ch + m.ch // 2, 5 * m.cw + m.cw // 2] = (1500.0, 700.0, 300.0)      # an orange outlier
    full = np.repeat(np.repeat(img, 5, axis=1), 5, axis=2)
    out = emu.run(emu.nits_to_scrgb(full), fp16_out=False, star=SP())
    ys, xs = np.nonzero(out["star"]["src"][0] > 500.0)
    ratio = out["img"][:, ys, xs] / out["star"]["src"][:, ys, xs]
    assert ys.size == 25 and ratio.max() < 0.2 and np.allclose(ratio, ratio[0:1], rtol=1e-12)


# ------------------------------------------------------------------------------------------------ the DEFAULTS (round 7) and the round-6 combination R6
def _population(m, seed, kind, sky=0.0, scale=1.0):
    """Random 5-px specks (fixed seed), one zone in two: 'heavy' = log-normal peaks (median 2 nits, sigma_ln 1.2) + three
    bright stars (102 / 175 / 200 nits); 'lights' = log-uniform 200 .. 1800 nits in saturated colours, some zones with 2-3."""
    rng = np.random.default_rng(seed)
    img = np.full((3, m.h, m.w), float(sky))
    colours = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.55, 0.0]])
    stars = {}
    for r in range(1, m.p.rows - 1):
        for c in range(1, m.p.cols - 1):
            if rng.random() > 0.5:
                continue
            for _ in range(1 if kind == "heavy" or rng.random() > 0.33 else int(rng.integers(2, 4))):
                y, x = r * m.ch + int(rng.integers(1, m.ch - 1)), c * m.cw + int(rng.integers(1, m.cw - 1))
                if kind == "heavy":
                    stars[(y, x)] = max(float(np.exp(np.log(2.0) + 1.2 * rng.standard_normal())) * scale, float(sky)) * np.ones(3)
                else:
                    stars[(y, x)] = float(np.exp(rng.uniform(np.log(200.0), np.log(1800.0)))) * colours[int(rng.integers(0, 4))]
    if kind == "heavy":
        for (c, r), lvl in (((3, 3), 102.0), ((6, 5), 175.0), ((8, 8), 200.0)):
            stars = {k: v for k, v in stars.items() if not (r * m.ch <= k[0] < (r + 1) * m.ch and c * m.cw <= k[1] < (c + 1) * m.cw)}
            stars[(r * m.ch + m.ch // 2, c * m.cw + m.cw // 2)] = lvl * scale * np.ones(3)
    for (y, x), rgb in stars.items():
        img[:, y, x] = rgb
    return img, stars


@pytest.mark.parametrize("sky", [0.0, 0.3])
def test_heavy_tailed_field_with_the_defaults_matches_the_reference(rig, sky):
    """DEFAULTS (geometric mean, even 0.8, keep 100) and the round-6 combination R6 (mean + 1 std, even 0.6): only the
    stars above 100 nits move, to level^(1 - even) x 100^even. R6 without the floor exercises the spread-aware target: the
    spread is summed ABOUT the mean in float32 (second sweep) and agrees with the float64 reference to 1e-5."""
    m, _ = rig
    img, stars = _population(m, 5, "heavy", sky)
    for sp, e in ((StarfieldParams(), 0.8), (StarfieldParams(**R6), 0.6)):
        ref, out = _assert_parity(rig, img, sp)
        moved = out["star"]["scale"] != 1.0
        assert moved.sum() == 3 * 25 and np.all(out["star"]["src"][0][moved] > 100.0)
        got = sorted(float(v) for v in np.unique(out["img"][0][moved]))
        assert got == pytest.approx([v ** (1 - e) * 100.0 ** e for v in (102.0, 175.0, 200.0)], rel=1e-4)
    # without the floor the spread-aware target itself is exercised (mean + 1 std of a log-normal field, float32 vs float64)
    ref, out = _assert_parity(rig, img, StarfieldParams(keep_nits=0.0, **R6))
    spk = ref["spk"]
    assert np.all(ref["target"][spk] > np.exp(ref["ln_mean"][spk]) * 1.5) and ref["ln_std"][spk].min() > 0.5
    assert np.allclose(out["star"]["plan"]["target"][spk], np.exp(ref["ln_mean"][spk] + ref["ln_std"][spk]), rtol=1e-5)
    _assert_parity(rig, img, StarfieldParams(keep_nits=0.0, target_sigma=2.5, even=0.3))


def test_a_field_below_keep_nits_is_bit_identical_in_the_emulator(rig):
    m, emu = rig
    img, _ = _population(m, 5, "heavy", 0.0, scale=90.0 / 200.0)
    assert img.max() == 90.0
    ref, out = _assert_parity(rig, img, StarfieldParams())
    assert np.all(out["star"]["scale"] == 1.0) and np.array_equal(out["img"], out["star"]["src"])
    plain = emu.run(_scrgb(img), fp16_out=False)
    assert np.array_equal(out["drive0"], plain["drive0"])                              # ... and so is the rest of the layer
    assert not np.all(emu.run(_scrgb(img), fp16_out=False, star=StarfieldParams(keep_nits=0.0))["star"]["scale"] == 1.0)


def test_christmas_lights_with_the_defaults_match_the_reference(rig):
    """Saturated specks 200 .. 1800 nits on code-0 black, several per zone in places: parity, hue (one scale for the three
    channels of a pixel), black stays code 0, no speck below 200 nits or above its source."""
    m, _ = rig
    img, stars = _population(m, 9, "lights")
    ref, out = _assert_parity(rig, img, StarfieldParams(), native=True)
    src, got, scale = out["star"]["src"], out["img"], out["star"]["scale"]
    lit = src.max(axis=0) > 0.0
    assert np.array_equal(got[:, ~lit], src[:, ~lit]) and np.all(got[:, ~lit] == 0.0)
    assert np.allclose(got[:, lit], src[:, lit] * scale[lit], rtol=1e-6) and np.all(scale <= 1.0)
    assert (scale < 1.0).any() and got.max(axis=0)[lit].min() >= 200.0 * (1 - 1e-6)
    _assert_parity(rig, img, StarfieldParams(**R6), native=True)                                                       # ... the round-6 combination
    _assert_parity(rig, img, StarfieldParams(keep_nits=0.0, target_sigma=0.0, even=1.0, even_reach=3), native=True)   # ... and the old maths


def test_a_lone_outlier_with_the_defaults_matches_the_reference(rig):
    """The white outlier of a 100-nit field: old maths -> the tapered geometric mean; DEFAULTS -> 80 % of the way (log;
    R6: 60 %), starting from the level the panel SHOWS (a 10 000-nit request balances like a panel-white one)."""
    m, emu = rig
    img = _field(m)
    z = (slice(5 * 45, 6 * 45), slice(5 * 80, 6 * 80))
    for sp in (StarfieldParams(), StarfieldParams(**R6)):
        e = sp.even
        ref, out = _assert_parity(rig, img, sp)
        landed = float(out["img"][0][z].max())
        t = float(ref["target"][5, 5])
        assert landed == pytest.approx(WHITE ** (1 - e) * t ** e, rel=2e-3) and 100.0 < t < 200.0
        over = img.copy(); over[img == WHITE] = 10000.0
        out2 = emu.run(_scrgb(over), fp16_out=False, star=sp)
        white = float(emu.white)
        assert float(out2["img"][0][z].max()) == pytest.approx(white ** (1 - e) * float(out2["star"]["plan"]["target"][5, 5]) ** e, rel=2e-3)


@pytest.mark.slow
def test_pa32ucxr_frame_star_lattice_with_outliers_matches_the_reference(tmp_path):
    """The hardware-gate pattern at full size (3840 x 2160, 48 x 48 zones, even_reach 8 well inside the lattice): a
    lattice of 5-px 100-nit stars with a few white outliers and a solid window, cap-only and with lift."""
    p = FaldParams()
    m = FaldModel(p)
    export_panel_params(m, tmp_path / "pa.bin")
    big = (m, Emu(read_panel_file(tmp_path / "pa.bin"), width=p.width, height=p.height))
    img = _field(m, outliers=((14, 14), (30, 22), (40, 9)), levels=(60.0, 100.0, 160.0), zones=range(6, 44))
    img[:, 20 * m.ch:26 * m.ch, 20 * m.cw:26 * m.cw] = 900.0
    ref, out = _assert_parity(big, img, SP())
    assert out["star"]["plan"]["w"][20:26, 20:26].max() == 0.0 and out["star"]["plan"]["w"][9, 40] == pytest.approx(1.0)
    assert out["img"][0, 9 * 45:10 * 45, 40 * 80:41 * 80].max() < 0.2 * WHITE
    _assert_parity(big, img, SP(lift=1.0, target_gain=0.8))
    lit = np.where(img > 0.0, img, 1.0)                                               # the same frame on a 1-nit sky
    ref, out = _assert_parity(big, lit, SP())
    assert out["star"]["plan"]["w"][9, 40] == pytest.approx(1.0, abs=1e-4) and out["star"]["stat"]["a_eff"][9, 40] == pytest.approx(25.0, abs=0.05)
    sky_px = out["star"]["src"][0] < 2.0
    assert np.array_equal(out["img"][:, sky_px], out["star"]["src"][:, sky_px])


# ------------------------------------------------------------------------------------------------ lit skies
@pytest.mark.parametrize("sky", [0.3, 1.0, 5.0, 20.0])
def test_lit_sky_field_matches_the_reference_and_the_sky_is_bit_identical(rig, sky):
    """Background-relative star-likeness at FULL resolution: min / un-gated sum / count of every zone pixel."""
    m, _ = rig
    img = _field(m, outliers=((5, 5),), sky=sky)
    ref, out = _assert_parity(rig, img, SP())
    st = out["star"]["stat"]
    assert st["a_eff"][5, 5] == pytest.approx(25.0, abs=0.05) and st["sparse"][5, 5] == pytest.approx(1.0, abs=1e-4)
    assert out["star"]["plan"]["w"][5, 5] == pytest.approx(1.0, abs=1e-4)
    assert float(st["b"][5, 5]) == pytest.approx(sky, rel=1e-6) and st["sparse"][0, 0] == 0.0       # a star-free sky zone
    src = out["star"]["src"]
    sky_px = src[0] < 0.5 * DIM
    assert np.array_equal(out["img"][:, sky_px], src[:, sky_px])                                     # the sky: BIT-identical
    assert DIM < out["img"].max() < 1.2 * DIM                                                        # the outlier landed on the field


def test_dim_lattice_gradient_and_flat_zones_match_the_reference(rig):
    m, _ = rig
    img = _field(m, outliers=(), levels=(20.0,), sky=1.0)                                            # a 20-nit lattice on 1 nit
    img[:, 0:m.ch, 0:m.cw] = np.linspace(1.0, 1.2, m.cw)[None, None, :]                              # a smooth gradient zone
    img[:, 5 * m.ch:6 * m.ch, 5 * m.cw:6 * m.cw] = np.linspace(1.0, 3.0, m.cw)[None, None, :]        # a gradient under a star
    _star(img, m, 5, 5, 100.0)
    ref, out = _assert_parity(rig, img, SP())
    st = out["star"]["stat"]
    assert st["sparse"][3, 3] > 0.9 and st["sparse"][0, 0] == 0.0 and st["sparse"][11, 11] == 0.0   # dim star / gradient / flat
    assert st["a_eff"][11, 11] == 0.0                                                                # flat: the quotient is never formed
    assert st["sparse"][5, 5] == pytest.approx(ref["sparse"][5, 5], abs=1e-4) and 0.85 < st["sparse"][5, 5] < 0.95


def test_a_window_on_a_lit_sky_protects_and_the_pull_stops_at_the_background(rig):
    m, _ = rig
    img = _field(m, outliers=((9, 9),), zones=range(6, 12), sky=1.0)
    img[:, 1 * m.ch:4 * m.ch, 1 * m.cw:4 * m.cw] = 1000.0
    _star(img, m, 5, 2, WHITE)                                                                        # 2 zones right of the window
    ref, out = _assert_parity(rig, img, SP(reach=2))
    assert out["star"]["plan"]["w"][2, 5] == 0.0 and out["star"]["stat"]["sparse"][2, 5] == pytest.approx(1.0, abs=1e-4)
    assert out["star"]["plan"]["w"][9, 9] == pytest.approx(1.0, abs=1e-4)
    img = _field(m, outliers=((5, 5),), sky=5.0)
    ref, out = _assert_parity(rig, img, SP(cap_nits=2.0))                               # a target below the sky
    # round 6 (monotone pull): a target below the sky stops the stars at the bottom of their speck band, b + 0.25 (peak - b)
    # — 28.75 nits for the 100-nit ones, ~25 % of its own peak for the outlier (rounds 3-5: flattened into the 5-nit sky)
    assert out["img"].min() > 5.0 * (1 - 1e-5) and out["img"].max() < 0.26 * WHITE                   # no hole
    dim_px = np.isclose(out["star"]["src"][0], DIM, rtol=1e-6)
    assert np.median(out["img"][0][dim_px]) == pytest.approx(5.0 + 0.25 * (DIM - 5.0), rel=0.02)
    sky_px = out["star"]["src"][0] < 6.0
    assert np.array_equal(out["img"][:, sky_px], out["star"]["src"][:, sky_px])


# ------------------------------------------------------------------------------------------------ speck zones / speck pixels
def _checker(m, sky, dx):
    """100-nit stars in every other zone of a 8 x 8 block, a white outlier at horizontal raster offset dx in zone (6, 6)."""
    img = np.full((3, m.h, m.w), float(sky))
    for r in range(2, 10):
        for c in range(2, 10):
            if (c + r) % 2 == 0:
                _star(img, m, c, r, DIM)
    _star(img, m, 6, 6, WHITE, dx=dx)
    return img


@pytest.mark.parametrize("sky", [0.0, 1.0, 5.0])
def test_star_free_lit_sky_zones_carry_the_weight_in_the_emulator_too(rig, sky):
    m, _ = rig
    img = _checker(m, sky, m.cw - 1)                                                  # the outlier at the border to a star-free zone
    ref, out = _assert_parity(rig, img, SP())
    plan = out["star"]["plan"]
    assert not plan["spk"][6, 7] and plan["spk"][6, 6] and plan["w_field"][6, 7] == pytest.approx(1.0, abs=1e-4)
    assert plan["ln_pk"][6, 7] == plan["ln_t"][6, 7] and plan["ln_g"][6, 7] == 0.0   # no speck: no peak of its own, no lift
    assert np.array_equal(out["star"]["stat"]["spk"], ref["spk"])
    zone = (0, slice(6 * 45, 7 * 45), slice(6 * 80, 7 * 80))
    # fully pulled on every sky — 113 nits (rounds 3-5: 106): the TAPERED window of round 6 weighs the outlier's own zone 1
    # and this small field (32 stars) around it less
    assert out["img"][zone].max() == pytest.approx(113.3, rel=0.01)


@pytest.mark.parametrize("sky", [1.0, 5.0])
def test_sky_is_bit_identical_under_lift_and_under_a_target_below_it(rig, sky):
    m, _ = rig
    img = _field(m, outliers=((5, 5),), levels=(60.0, 100.0, 160.0), sky=sky)
    for sp in (SP(lift=1.0), SP(cap_nits=0.5 * sky), SP(target_gain=0.05),   # 0.05 = the C++ / emulator minimum (the reference alone takes 0.01)
               SP(lift=1.0, cap_nits=0.5 * sky)):
        ref, out = _assert_parity(rig, img, sp)
        src = out["star"]["src"]
        sky_px = src[0] < 0.5 * 60.0
        assert np.array_equal(out["img"][:, sky_px], src[:, sky_px]), sp
        assert out["img"].min() > sky * (1 - 1e-5)                                    # nothing ends below the sky
    lifted = _assert_parity(rig, img, SP(lift=1.0))[1]
    dim = np.isclose(lifted["star"]["src"][0], 60.0, rtol=1e-6)
    assert lifted["img"][0][dim].min() > 61.0                                         # ... while the dim specks do rise


# ------------------------------------------------------------------------------------------------ round 4: grain, soft stars, bright skies
def _grainy_checker(m, sky, grain, dx, seed=11):
    base = float(sky) * (1.0 + np.random.default_rng(seed).uniform(-grain, grain, (m.h, m.w)))
    img = np.repeat(base[None], 3, axis=0)
    stars = np.zeros((m.h, m.w), dtype=bool)
    for r in range(2, 10):
        for c in range(2, 10):
            if (c + r) % 2 == 0:
                _star(img, m, c, r, DIM); stars[r * m.ch + m.ch // 2, c * m.cw + m.cw // 2] = True
    _star(img, m, 6, 6, WHITE, dx=dx); stars[6 * m.ch + m.ch // 2, 6 * m.cw + dx] = True
    return img, stars


@pytest.mark.parametrize("grain", [0.03, 0.10])
@pytest.mark.parametrize("sky", [1.0, 5.0])
def test_grainy_sky_matches_the_reference_and_only_star_pixels_change(rig, sky, grain):
    """D at full resolution: a grainy star-free zone is no speck zone (a_eff ~ half the zone), carries the weight, and
    every non-star pixel stays bit-identical - with and without lift."""
    m, _ = rig
    img, stars = _grainy_checker(m, sky, grain, m.cw - 1)
    full_stars = np.repeat(np.repeat(stars, 5, axis=0), 5, axis=1)
    for sp in (SP(), SP(lift=1.0)):
        ref, out = _assert_parity(rig, img, sp)
        plan, st = out["star"]["plan"], out["star"]["stat"]
        assert np.array_equal(st["spk"], ref["spk"]) and not plan["spk"][6, 7] and st["a_eff"][6, 7] > 1000.0
        assert plan["w_field"][6, 7] > 0.97
        src = out["star"]["src"]
        assert np.array_equal(out["img"][:, ~full_stars], src[:, ~full_stars])
    zone = (0, slice(6 * 45, 7 * 45), slice(6 * 80, 7 * 80))
    assert out["img"][zone].max() == pytest.approx(113.4, rel=0.01)                   # the border outlier is fully pulled (round 6 taper: 113)


@pytest.mark.parametrize("sky", [0.0, 1.0, 5.0])
def test_soft_star_profile_is_monotone_in_the_emulator(rig, sky):
    """E at full resolution: an 1800-nit 5-px core, a 50 % cross, 20 % diagonals, a 5 % outer cross."""
    m, _ = rig
    img = _field(m, outliers=(), sky=sky)
    y, x = 5 * m.ch + m.ch // 2, 5 * m.cw + m.cw // 2
    groups = [[(0, 0)], [(0, 1), (0, -1), (1, 0), (-1, 0)], [(1, 1), (1, -1), (-1, 1), (-1, -1)], [(0, 2), (0, -2), (2, 0), (-2, 0)]]
    for frac, g in zip((1.0, 0.5, 0.2, 0.05), groups):
        for dy, dx in g:
            img[:, y + dy, x + dx] = max(1800.0 * frac, sky)
    for sp in (SP(), SP(area_lo=100.0, area_hi=400.0)):
        ref, out = _assert_parity(rig, img, sp)
        blk = lambda yy, xx: out["img"][0, yy * 5:yy * 5 + 5, xx * 5:xx * 5 + 5]
        hi = [max(blk(y + dy, x + dx).max() for dy, dx in g) for g in groups]
        lo = [min(blk(y + dy, x + dx).min() for dy, dx in g) for g in groups]
        # (3e-3: fully pulled pixels land on their OWN interpolated target; under the tapered window of round 6 it varies by
        # ~1.5e-3 across the 25-px-wide star — a tilt of the target field, not a ring)
        assert all(lo[i] >= hi[i + 1] * (1 - 3e-3) for i in range(3)), (sky, hi, lo)
        assert hi[0] < 0.5 * 1800.0 and out["img"].min() >= sky * (1 - 1e-5)


@pytest.mark.parametrize("sky,dim", [(20.0, 60.0), (50.0, 100.0)])
def test_bright_sky_lift_and_cap_match_the_reference_and_spare_the_sky(rig, sky, dim):
    """F at full resolution: the background-relative speck band keeps a sky at 33 / 50 % of the star level out of the
    lift; a cap below the sky flattens the stars into it and leaves the sky bit-identical."""
    m, _ = rig
    img = _field(m, outliers=((5, 5),), levels=(dim,), sky=sky)
    for sp in (SP(lift=1.0), SP(cap_nits=0.6 * sky), SP(lift=1.0, cap_nits=0.6 * sky)):
        ref, out = _assert_parity(rig, img, sp)
        src = out["star"]["src"]
        sky_px = src[0] < 0.5 * (sky + dim)
        assert np.array_equal(out["img"][:, sky_px], src[:, sky_px]), sp
    lifted = _assert_parity(rig, img, SP(lift=1.0))[1]
    specks = np.isclose(lifted["star"]["src"][0], dim, rtol=1e-6)
    assert lifted["img"][0][specks].min() > dim * 1.01


# ------------------------------------------------------------------------------------------------ round 5: own-zone gate, tapered protection
def test_non_star_shapes_are_bit_identical_and_match_the_reference(rig):
    """The own-zone gate at full resolution: a shape in a zone without a speck is never touched, whatever weight the zone
    carries; the 10 x 10-px (star-like) one is treated like the half star it is."""
    m, _ = rig
    for side, nits, touched in ((8, 150.0, False), (3, 150.0, False), (3, 600.0, False), (2, 150.0, True)):
        img = _checker(m, 1.0, m.cw // 2)
        _star(img, m, 6, 6, DIM)                                                      # no outlier: a plain checkerboard
        y0, x0 = 6 * m.ch + (m.ch - side) // 2, 7 * m.cw + (m.cw - side) // 2         # zone (7, 6): star-free
        img[:, y0:y0 + side, x0:x0 + side] = nits
        ref, out = _assert_parity(rig, img, SP())
        blk = (slice(None), slice(5 * y0, 5 * (y0 + side)), slice(5 * x0, 5 * (x0 + side)))
        same = np.array_equal(out["img"][blk], out["star"]["src"][blk])
        assert same != touched and bool(out["star"]["plan"]["spk"][6, 7]) == touched, (side, nits)


def _step_frames(m, sky, grain, mirror=False):
    """A solid 1000-nit window (zones 1..3 x 3..7 or mirrored) + a block of 100-nit stars far from it (full resolution)."""
    H, W = m.p.height, m.p.width
    f = np.full((H, W), float(sky))
    if grain:
        f = f * (1.0 + np.random.default_rng(17).uniform(-grain, grain, f.shape))
    cols = range(1, 4) if not mirror else range(8, 11)
    f[3 * 45:8 * 45, cols[0] * 80:(cols[-1] + 1) * 80] = 1000.0
    for r in range(1, 11):
        for c in (range(8, 11) if not mirror else range(1, 4)):
            f[r * 45 + 20:r * 45 + 25, c * 80 + 38:c * 80 + 43] = DIM
    return f


def _balanced_peak(emu, f, x0, y0, sp):
    g = f.copy()
    g[y0:y0 + 5, x0:x0 + 5] = 1800.0
    src = emu.panel_nits(np.ascontiguousarray(np.repeat(g[:, :, None], 3, axis=2) / 80.0))
    cs = clamp_star(sp)
    plan = emu.star_plan(emu.star_stat(src, cs), cs)
    out, _ = emu.balance(src, plan, cs)
    return float(out[0, y0:y0 + 5, x0:x0 + 5].max())


@pytest.mark.parametrize("sky,grain", [(0.0, 0.0), (1.0, 0.0), (1.0, 0.05)])
def test_a_star_stepping_away_from_a_window_in_8_px_steps(rig, sky, grain):
    """(a) + (c): monotone, continuous, mirror-symmetric. PINNED: the series reads 1800 up to 134 px from the window edge, then
    1277 / 497 / 190 / 118 / 116 ... 114 — the largest single 8-px step changes the balanced peak of an 1800-nit star next
    to a 1000-nit window by x0.38 (w ramps 0 -> 1 within ~32 px between the d = 2 and d = 3 zone centres; round 4 jumped
    788 -> 107 = x0.14 in one 16-px step). Round 6: strictly monotone — the step where the 5-px flat star straddles a zone
    border no longer reads + 9 % (equal peaks: the two zones SHARE one vote in the target average, mirror-symmetric). The
    ~25 % target is NOT met for such an outlier — see the reference test's docstring for the arithmetic."""
    m, emu = rig
    sp = SP()
    f = _step_frames(m, sky, grain)
    xs = list(range(4 * 80 + 6, 8 * 80 - 40, 8))                                       # from 6 px off the window edge outward
    peaks = np.array([_balanced_peak(emu, f, x, 5 * 45 + 8, sp) for x in xs])
    assert np.all(np.diff(peaks) <= 1e-3 * peaks[:-1])                                 # monotone, the whole series (the target
    # itself drifts by < 1e-4 per step as the star's zone moves through the tapered window)
    assert peaks[0] == pytest.approx(1800.0, rel=1e-4) and 100.0 < peaks[-1] < 120.0
    ratio = (peaks[1:] / peaks[:-1]).min()
    assert 0.32 < ratio < 0.40, ratio                                                  # the pinned largest step: x0.38 (defaults, even 0.8: x0.46)
    fm = _step_frames(m, sky, grain, mirror=True)
    if not grain:                                                                       # (grain is not mirror-symmetric)
        mirrored = np.array([_balanced_peak(emu, fm, m.p.width - x - 5, 5 * 45 + 8, sp) for x in xs])
        assert np.allclose(mirrored, peaks, rtol=1e-6)


@pytest.mark.parametrize("sky,grain,checker", [(0.0, 0.0, False), (1.0, 0.05, False), (1.0, 0.05, True)])
def test_a_star_crossing_a_zone_border_in_8_px_steps_stays_smooth(rig, sky, grain, checker):
    """(d): a white star stepped from zone (4, 6) into zone (5, 6): the balanced peak stays at the local target; the one
    step where the 5-px star straddles the border (both zones hold it) reads a few percent higher."""
    m, emu = rig
    f = np.full((m.p.height, m.p.width), float(sky))
    if grain:
        f = f * (1.0 + np.random.default_rng(3).uniform(-grain, grain, f.shape))
    for r in range(1, 11):
        for c in range(1, 11):
            if not (checker and (c + r) % 2):
                f[r * 45 + 20:r * 45 + 25, c * 80 + 38:c * 80 + 43] = DIM
    peaks = np.array([_balanced_peak(emu, f, x, 6 * 45 + 8, SP()) for x in range(4 * 80 + 38, 5 * 80 + 39, 8)])
    # round 6: 104.5 .. 104.8 (lattice) / 108.9 .. 109.7 (checkerboard), largest step 0.2 % — the straddle step (+ 3 .. 6 % in
    # round 5) is gone: the flat star's two zones have EQUAL peaks and share one vote in the target average
    assert 100.0 < peaks.min() and peaks.max() < 111.0 and np.abs(peaks[1:] / peaks[:-1] - 1.0).max() < 0.01


# ------------------------------------------------------------------------------------------------ round 6: review fixes
def _gauss_frame(m, stars, sky=0.0, sigma=1.5):
    """Full-resolution Gaussian stars (NOT raster-aligned: emulator only), as-if-white nits (3, H, W)."""
    yy, xx = np.mgrid[0:m.p.height, 0:m.p.width]
    s = np.full((m.p.height, m.p.width), float(sky))
    for cx, cy, pk in stars:
        s = np.maximum(s, pk * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2)))
    return np.stack([s, s, s])


@pytest.mark.parametrize("sky", [0.0, 1.0])
def test_a_bright_star_crossing_a_far_zone_border_does_not_swing_the_target(rig, sky):
    """Review finding 1 (its pop.py): three 100-nit stars, a 200-nit star, and a 1500-nit star 8-9 zones away stepped in
    1-px steps across the zone border at x = 800. Rounds <= 5: its flank in the neighbour zone counted as an independent
    43 .. 1200-nit star with full weight and the hard window let the core pop in — the 200-nit star was shown at 97.2 /
    165.7 / 189.3 / 197.8 / 197.4 nits for the core at x = 803 / 801 / 800 / 799 / 797. Round 6 (flank zones carry no
    target weight; tapered window): 124.1 for x >= 800, 134.8 for x <= 799 — ONE step of + 8.6 % where the core's zone
    changes its chebyshev distance from 9 (weight 0) to 8 (weight 1 / 9). PINNED at < 10 %; the taper is per ZONE distance,
    so in a field this sparse (four other stars) a rim step of 1 / (E + 1) remains."""
    m, emu = rig
    sp = clamp_star(SP())
    base = [(1 * 80 + 40, 5 * 45 + 22, 200.0), (2 * 80 + 40, 3 * 45 + 22, 100.0), (3 * 80 + 40, 7 * 45 + 22, 100.0),
            (1 * 80 + 40, 8 * 45 + 22, 100.0)]
    shown, flanks = [], []
    for bx in range(806, 793, -1):
        img = _gauss_frame(m, base + [(bx, 5 * 45 + 22, 1500.0)], sky)
        st = emu.star_stat(img, sp)
        plan = emu.star_plan(st, sp)
        out, _ = emu.balance(img, plan, sp)
        shown.append(float(out[0, 5 * 45 + 22, 1 * 80 + 40]))
        flanks.append((bool(plan["flank"][5, 9]), bool(plan["flank"][5, 10])))
    shown = np.array(shown)
    steps = np.abs(shown[1:] / shown[:-1] - 1.0)
    assert steps.max() < 0.10 and (steps > 1e-3).sum() == 1, (shown, steps)             # one rim step, pinned: + 8.6 %
    assert shown[0] == pytest.approx(124.1, rel=0.01) and shown[-1] == pytest.approx(134.8, rel=0.01)
    assert not any(a and b for a, b in flanks)                                           # never both zones
    near = [f for bx, f in zip(range(806, 793, -1), flanks) if abs(bx - 799.5) <= 3]     # the spill is above the sky here:
    assert all(a != b for a, b in near) and near[0] == (True, False) and near[-1] == (False, True)   # a flank on either side


@pytest.mark.parametrize("combo", ["defaults", "r6"])
def test_the_same_crossing_with_the_defaults_steps_once(rig, combo):
    """The pop scene with the DEFAULTS (round 7: geometric mean, even 0.8, keep 100): the 200-nit star's zone target steps
    124.0 -> 134.7 as in the old maths, the star itself 136.5 -> 145.9 (+ 6.9 %). With the round-6 combination R6 (mean +
    1 std, even 0.6) the 1500-nit star entering the rim of the window at weight 1 / 9 moves the SPREAD more than the mean
    in a field this sparse: target 170.9 -> 232.9 (+ 36 %), the star 182.2 -> 200.0 (+ 9.8 %: above the new target it is
    left alone). Either way ONE step, at the zone crossing only. PINNED; a continuous (pixel-distance) taper would remove
    it (not implemented)."""
    m, emu = rig
    sp = clamp_star(StarfieldParams() if combo == "defaults" else StarfieldParams(**R6))
    want = {"defaults": (136.5, 145.9, 124.0, 134.7, 0.08), "r6": (182.2, 200.0, 170.9, 232.9, 0.11)}[combo]
    base = [(1 * 80 + 40, 5 * 45 + 22, 200.0), (2 * 80 + 40, 3 * 45 + 22, 100.0), (3 * 80 + 40, 7 * 45 + 22, 100.0),
            (1 * 80 + 40, 8 * 45 + 22, 100.0)]
    shown, target = [], []
    for bx in range(806, 793, -1):
        img = _gauss_frame(m, base + [(bx, 5 * 45 + 22, 1500.0)])
        plan = emu.star_plan(emu.star_stat(img, sp), sp)
        out, _ = emu.balance(img, plan, sp)
        shown.append(float(out[0, 5 * 45 + 22, 1 * 80 + 40])); target.append(float(plan["target"][5, 1]))
    shown, target = np.array(shown), np.array(target)
    steps = np.abs(shown[1:] / shown[:-1] - 1.0)
    assert (steps > 1e-3).sum() == 1 and steps.max() < want[4], (shown, steps)
    assert shown[0] == pytest.approx(want[0], rel=0.01) and shown[-1] == pytest.approx(want[1], rel=0.01)
    assert target[0] == pytest.approx(want[2], rel=0.01) and target[-1] == pytest.approx(want[3], rel=0.01)


@pytest.mark.parametrize("sky", [0.0, 1.0])
def test_flank_flag_and_brightest_pixel_position_match_the_reference(rig, sky):
    m, _ = rig
    img = _field(m, outliers=(), sky=sky, zones=range(2, 5))
    y = 6 * m.ch + m.ch // 2
    img[:, y, 9 * m.cw] = 1500.0                                                        # core at the left edge of zone (9, 6)
    img[:, y, 9 * m.cw - 1] = 300.0                                                     # its flank at the right edge of zone (8, 6)
    ref, out = _assert_parity(rig, img, SP())
    flank = out["star"]["plan"]["flank"]
    assert flank[6, 8] and flank.sum() == 1
    ly, lx = np.divmod(out["star"]["stat"]["arg"], 80)
    assert (lx[6, 8], lx[6, 9]) == (79, 0)
    flat = img.copy(); flat[:, y, 9 * m.cw - 1] = 1500.0                                # EQUAL peaks: the two zones share one vote
    ref, out = _assert_parity(rig, flat, SP())
    plan = out["star"]["plan"]
    assert not plan["flank"].any() and plan["wt"][6, 8] == pytest.approx(0.5) and plan["wt"][6, 9] == pytest.approx(0.5)


def test_a_star_at_a_zone_corner_has_three_flanks(rig):
    """A Gaussian star one pixel inside the corner of zone (6, 9): its spill into the left, the upper and the diagonal
    zone are all flanks (emulator only: not raster-aligned)."""
    m, emu = rig
    sp = clamp_star(SP())
    img = _gauss_frame(m, [(6 * 80 + 1, 9 * 45 + 1, 1500.0), (2 * 80 + 40, 3 * 45 + 22, 100.0)], 1.0)
    st = emu.star_stat(img, sp)
    plan = emu.star_plan(st, sp)
    assert plan["flank"][9, 5] and plan["flank"][8, 6] and plan["flank"][8, 5] and plan["flank"].sum() == 3
    assert not plan["flank"][9, 6] and plan["wt"][9, 6] == pytest.approx(1.0) and plan["wt"][8, 5] == 0.0
    ly, lx = np.divmod(st["arg"], 80)
    assert (lx[8, 5], ly[8, 5]) == (79, 44) and (lx[9, 6], ly[9, 6]) == (1, 1)


@pytest.mark.parametrize("sky,cap", [(10.0, 15.0), (10.0, 12.0), (5.0, 6.0), (10.0, 8.0)])
def test_target_close_to_the_sky_matches_the_reference_and_stays_monotone(rig, sky, cap):
    """The reviewer's mono.py cases at image level (target / background between 0.8 and 1.5): a soft star's profile stays
    monotone, the sky bit-identical, emulator = reference."""
    m, _ = rig
    img = _field(m, outliers=(), sky=sky)
    y, x = 5 * m.ch + m.ch // 2, 5 * m.cw + m.cw // 2
    groups = [[(0, 0)], [(0, 1), (0, -1), (1, 0), (-1, 0)], [(1, 1), (1, -1), (-1, 1), (-1, -1)], [(0, 2), (0, -2), (2, 0), (-2, 0)]]
    for frac, g in zip((1.0, 0.5, 0.2, 0.08), groups):
        for dy, dx in g:
            img[:, y + dy, x + dx] = max(1000.0 * frac, sky)
    ref, out = _assert_parity(rig, img, SP(cap_nits=cap, area_lo=100.0, area_hi=400.0))
    # no "donut": every pixel above ITS pull threshold T' lands exactly on it (w = even = 1), every other one is untouched —
    # out is a non-decreasing function of the level at every position. (T' itself tilts across the star here — up to
    # ~10 % over 15 px — because the speck-band floor b + 0.25 (pk - b) follows the INTERPOLATED zone peak; with the target
    # well above the sky, T' = the target and the tilt is ~1e-3.)
    assert ref["w"][5, 5] == pytest.approx(1.0, abs=2e-4)      # (the lit sky's own dim drive in the protection field)
    tp = ref["pull_threshold_pixel"]
    m_in = np.max(img, axis=0)
    zone = (slice(5 * m.ch, 6 * m.ch), slice(5 * m.cw, 6 * m.cw))
    above = m_in[zone] > tp[zone]
    got = np.max(ref["img"], axis=0)[zone]
    assert above.sum() >= 5 and np.allclose(got[above], np.maximum(tp[zone][above], sky), rtol=1e-3)
    assert np.array_equal(got[~above], m_in[zone][~above])
    full = np.repeat(np.repeat(np.where(above, tp[zone], m_in[zone]), 5, axis=0), 5, axis=1)   # the emulator agrees at block centres
    assert np.allclose(out["img"][0, 5 * 45:6 * 45, 5 * 80:6 * 80][2::5, 2::5], full[2::5, 2::5], rtol=1e-3)
    src = out["star"]["src"]
    sky_px = np.isclose(src[0], sky, rtol=1e-6)
    assert np.array_equal(out["img"][:, sky_px], src[:, sky_px]) and out["img"].min() >= sky * (1 - 1e-5)


# ------------------------------------------------------------------------------------------------ off = the previous layer
def test_option_off_and_inert_settings_are_bit_identical(rig):
    m, emu = rig
    img = _field(m, outliers=((5, 5),), sky=0.3)
    img[:, 1 * m.ch:3 * m.ch, 1 * m.cw:4 * m.cw] = 600.0
    frame = _scrgb(img)
    off = emu.run(frame)
    assert off["star"] is None
    keys = ("img", "drive0", "drive1", "bT", "bE", "gain", "req", "out", "out_nits", "px_gain")
    for sp in (SP(strength=0.0), SP(area_lo=0.0, area_hi=0.0)):   # no zone qualifies
        inert = emu.run(frame, star=sp)
        assert inert["star"] is not None and np.all(inert["star"]["scale"] == 1.0)
        for k in keys:
            assert np.array_equal(off[k], inert[k]), k
    solid = np.full((3, m.h, m.w), 80.0)                                               # no star-like content at all
    a, b = emu.run(_scrgb(solid)), emu.run(_scrgb(solid), star=SP(lift=1.0))
    assert all(np.array_equal(a[k], b[k]) for k in keys)
    on = emu.run(frame, star=SP())
    assert not np.array_equal(on["out"], off["out"])                                   # ... and ON does change this frame


def test_clamp_star_is_the_cpp_clamp():
    sp = clamp_star(SP(even=3.0, lift=-1.0, target_gain=0.0, even_reach=99, cap_nits=1e9, strength=7.0,
                                    area_lo=500.0, area_hi=100.0, peak_hi=-5.0, reach=40, nb_lo=0.8, nb_hi=0.2))
    assert (sp.even, sp.lift, sp.target_gain, sp.even_reach, sp.cap_nits, sp.strength) == (1.0, 0.0, 0.05, 12, 10000.0, 1.0)
    assert (sp.area_lo, sp.area_hi, sp.peak_hi, sp.reach, sp.nb_lo, sp.nb_hi) == (500.0, 500.0, 0.0, 4, 0.8, 0.8)
    assert clamp_star(StarfieldParams()) == StarfieldParams()                          # the defaults are inside every range


# ------------------------------------------------------------------------------------------------ HLSL / C++ mirrors
@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_hlsl_star_passes_mirror_the_reference():
    src = _SHADER.read_text(encoding="utf-8")
    part = lambda name: re.search(name + r' = R"\((.*?)\)";', src, re.S).group(1)
    common, stat, pixel = part("g_faldCommonSource"), part("g_faldStatSource"), part("g_faldPixelSource")
    sstat, sweight, splan = part("g_faldStarStatSource"), part("g_faldStarWeightSource"), part("g_faldStarPlanSource")
    for reg in ("starPlanTex  : register(t15)", "starStatTex  : register(t16)", "starWTex     : register(t17)",
                "starPlan2Tex : register(t18)", "starBgTex    : register(t19)"):
        assert reg in common
    # Balance: bilinear plan at FineUV, early-out = untouched pixel, one scale for the three channels
    bal = re.search(r"float3 Balance\(float3 img, int2 px\) \{(.*?)\n\}", common, re.S).group(1)
    assert "float2 uv = FineUV(float2(px));" in bal and "starPlanTex.SampleLevel(linearClamp, uv, 0)" in bal
    assert "float3 Balance(float3 img, int2 px)" in common
    # the pull stops at the interpolated zone background and never ends above the pixel itself
    assert "outM = max(outM, min(bPx, safe));" in bal
    assert "if (!(m > 0.0f)) return img;" in bal and "if (!(wPx > 0.0f)) return img;" in bal and "if (!acts) return img;" in bal
    # the protection is interpolated on its own and applied per pixel; the own-zone gate is a NEAREST load of the flag
    assert "float2 p2 = starPlan2Tex.SampleLevel(linearClamp, uv, 0).xy;" in bal
    assert "float wPx = plan.x * (1.0f - StarSmooth(starNbLo, starNbHi, p2.y));" in bal
    # the zone index in INTEGER math (as view 9), the cheap early-outs (black pixel, own-zone gate) BEFORE the two taps
    assert "uint2 zone = uint2((uint)(px.x - (int)originX) / cellW, (uint)(px.y - (int)originY) / cellH);" in bal
    assert "if (!(starPlan2Tex.Load(int3((int2)zone, 0)).z > 0.5f)) return img;" in bal
    assert bal.index("if (!(m > 0.0f)) return img;") < bal.index("starPlan2Tex.Load(") < bal.index("starPlanTex.SampleLevel(")
    # the MONOTONE pull: threshold T' = lerp(ln max(t, b + SPECK_LO span), ln t, g); no gate inside the exponent
    assert "float g = StarSmooth(FALD_STAR_GATE_LO, FALD_STAR_GATE_HI, tPx / bPx);" in bal
    assert "float tFloor = bPx + FALD_STAR_SPECK_LO * max(spanPx, 0.0f);" in bal
    assert "float lnTp = lerp(log(max(tPx, tFloor)), plan.y, g);" in bal and "if (m > exp(lnTp)) {" in bal
    assert "float lnShown = log(min(safe, white));" in bal and "outM = exp(lnShown + wPx * starEven * (lnTp - lnShown));" in bal
    assert "acts = outM < safe * (1.0f - FALD_STAR_PULL_EPS);" in bal
    assert "} else if (m <= tPx) {" in bal and "min(safe * gPx, max(tPx, safe))" in bal and "acts = gPx > 1.0f;" in bal
    assert "} else return img;" in bal
    assert "float bPx = exp(p2.x);" in bal and "float spanPx = exp(plan.w) - bPx;" in bal
    assert "(spanPx > 0.0f) ? StarSmooth(FALD_STAR_SPECK_LO, FALD_STAR_SPECK_HI, (safe - bPx) / max(spanPx, 1e-12f)) : 0.0f" in bal
    assert "return img * (outM / safe);" in bal
    assert "max(hi - lo, 1e-12f)" in common                                            # StarSmooth = starfield._smoothstep
    # the balanced frame is what every pass reads: before Correct in the statistic pass, before the fields in the pixel pass
    assert stat.index("if (starOn != 0u) img = Balance(img, int2((int)px, (int)py));") < stat.index("img = Correct(img, bT, bE, g);")
    assert pixel.index("debugMode == 4) return src;") < pixel.index("if (starOn != 0u) img = Balance(") < pixel.index("Correct(img, bT, bE, gain)")
    assert "if (debugMode == 9)" in pixel and "if (starOn == 0u) return src;" in pixel
    assert stat.count("starOn") == 1 and "starOn" not in part("g_faldConvSource")
    # S0: the statistic pass's own peak / lit sum on the SOURCE frame (no Balance, no Correct)
    assert "if (s > driveFloor) { m = max(m, s); sum += s; }" in sstat and "Balance(" not in sstat and "Correct(" not in sstat
    # ... and the un-gated background statistic next to it: every in-frame pixel, no drive floor
    assert "mn = min(mn, s); sumAll += s; count++;" in sstat
    assert sstat.index("if (px >= frameW || py >= frameH) continue;") < sstat.index("if (StarBetter(mAll, kAll, s, k)) { mAll = s; kAll = k; }")
    # the brightest pixel's position: value, then nearest the zone border, then row-major first — a total order
    assert "if (vb != va) return vb > va;" in sstat and "if (eb != ea) return eb > ea;" in sstat and "return kb < ka;" in sstat
    assert "max(abs(2 * lx - ((int)cellW - 1)) * (int)cellH, abs(2 * ly - ((int)cellH - 1)) * (int)cellW)" in sstat
    assert "if (StarBetter(gMaxAll[tid.x], gArg[tid.x], gMaxAll[tid.x + stride], gArg[tid.x + stride])) {" in sstat
    assert "bool speck = span > max(FALD_STAR_FLAT_ABS, FALD_STAR_FLAT_REL * peakAll);" in sstat
    assert "float aEff = speck ? (sumAll0 - b * cnt) / max(span, 1e-12f) : 0.0f;" in sstat
    assert "(has && speck) ? 1.0f - StarSmooth(starAreaLo, starAreaHi, aEff) : 0.0f" in sstat
    assert "starBgOut[uint2(cx, cy)] = float4(log(max(b, 1e-12f)), (gCount[0] != 0u) ? (float)gArg[0] : 0.0f, total, aEff);" in sstat
    # the speck-zone flag (lit peak above its own background) travels S0 -> S1 -> S2; carry, lift and peak field key on it
    assert "starStatOut[uint2(cx, cy)] = float4(peak, (has && speck && aEff < starAreaHi) ? 1.0f : 0.0f, sparse, solid);" in sstat
    # S1: flank zones carry no target weight (wt), keep their w (plan2.w)
    assert "starWOut[id.xy] = float4(wt, wt * log(max(st.r, 1e-12f)), flank ? 1.0f : 0.0f, st.g);" in sweight
    assert "float wt = flank ? 0.0f : w / (1.0f + partners);" in sweight                # equal-peak partners share one vote
    assert "(lx >= (int)cellW - FALD_STAR_FLANK_PX && nx < FALD_STAR_FLANK_NEAR_PX)" in sweight
    assert "(lx < FALD_STAR_FLANK_PX && nx >= (int)cellW - FALD_STAR_FLANK_NEAR_PX)" in sweight
    assert "(ly >= (int)cellH - FALD_STAR_FLANK_PX && ny < FALD_STAR_FLANK_NEAR_PX)" in sweight
    assert "if (!(np_ >= st.r && np_ > 0.0f)) continue;" in sweight
    assert "if (okx && oky) { if (np_ > st.r) flank = true; else partners += 1.0f; }" in sweight
    assert f"static const int FALD_STAR_FLANK_PX = {gpuemu.STAR_FLANK_PX};" in common
    assert f"static const int FALD_STAR_FLANK_NEAR_PX = {gpuemu.STAR_FLANK_NEAR_PX};" in common
    from dlc.fald import starfield as _sf
    assert (_sf.FLANK_PX, _sf.FLANK_NEAR_PX) == (gpuemu.STAR_FLANK_PX, gpuemu.STAR_FLANK_NEAR_PX) == (2, 12)
    assert "bool spk = sw.a > 0.5f;" in splan and "wsum += t.r * k; wl += t.g * k;" in splan
    assert "float k = (float)(E + 1 - max(abs(dx), abs(dy))) / (float)(E + 1);" in splan           # the tapered target window
    # the carry is on w0 = sparse * strength (the protection is NOT baked into the interpolated weight)
    assert "s3 += starStatTex.Load(int3(x, y, 0)).b; n3 += starWTex.Load(int3(x, y, 0)).a;" in splan
    assert "float carry = s3 * starStrength / max(n3, 1.0f) * (1.0f - StarSmooth(starNbLo, starNbHi, st.a));" in splan
    assert "StarSmooth(starPeakHi, 2.0f * starPeakHi, peak)" in sstat
    assert "(1.0f - sparse) * DriveOf(min(peak, total / area0))" in sstat
    # S1: the TAPERED protection field over chebyshev distance <= reach + 1, k = clamp((reach + 1 - d) / 2, 0, 1)
    assert "int R = (int)starReach + 1;" in sweight
    assert "float k = clamp((float)(R - max(abs(dx), abs(dy))) * 0.5f, 0.0f, 1.0f);" in sweight
    assert "near = max(near, starStatTex.Load(int3(x, y, 0)).a * k);" in sweight
    assert "float w = st.b * starStrength * (1.0f - StarSmooth(starNbLo, starNbHi, near));" in sweight
    assert "starPlan2Out[id.xy] = float4(starBgTex.Load(int3(id.xy, 0)).x, near, st.g, w);" in sweight
    assert "float target = peak;" in splan and "if (wsum > 0.0f) {" in splan and "float mean = wl / wsum;" in splan
    # the spread: summed ABOUT the mean in a second sweep (float32: sum2 / wsum - mean^2 loses a uniform field's exact 0)
    assert "float dl = t2.g / t2.r - mean;" in splan and "var += t2.r * k2 * dl * dl;" in splan and "var = max(var / wsum, 0.0f);" in splan
    assert "target = exp(mean + starTargetSigma * sqrt(var)) * starTargetGain;" in splan
    assert splan.index("target = max(target, starKeepNits);") < splan.index("if (starCapNits > 0.0f) target = min(target, starCapNits);")
    assert "target = min(target, white);" in splan and "max(wsum" not in splan                      # <= white, no quotient floor
    assert "float wField = spk ? st.b * starStrength : carry;" in splan and "(spk && peak < target) ? starLift * (lnT - lp) : 0.0f" in splan
    assert "starPlanOut[id.xy] = float4(wField, lnT, lnG, spk ? lp : lnT);" in splan
    # C++ order: the plan of the source frame first, then the layer
    c = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    assert c.index("if (r->starOn) RunStar(r);") < c.index("RunStat(r, 0);")
    assert "r->starOn ? r->starPlanSRV : nullptr" in c and "r->starOn ? r->starPlan2SRV : nullptr" in c and "FALD_SRV_SLOTS = 25" in c   # t20-t24: the glow fill (S2)
    h = (_SRC / "fald.h").read_text(encoding="utf-8")
    assert "FALD_CB_BYTES = 336" in (_SHARED / "fald_panel.h").read_text(encoding="utf-8")        # 84 words since S2 (glow fill)
    assert f"FALD_STAR_EVEN_REACH_MAX = {gpuemu.STAR_EVEN_REACH_MAX}" in h and f"FALD_STAR_REACH_MAX = {gpuemu.STAR_REACH_MAX}" in h
    # ... and the DWM hook (acb94f5): the same order, the same t15 / t18 bindings, the same reach limits
    hk = _HOOK.read_text(encoding="utf-8")
    hrun = hk[hk.index("bool FaldRun("):]
    assert hrun.index("if (m->starOn) RunStar(m);") < hrun.index("RunStat(m, 0);")
    assert "m->starOn ? m->starPlanSRV : nullptr" in hk and "m->starOn ? m->starPlan2SRV : nullptr" in hk
    assert f"if (t.starReach > {gpuemu.STAR_REACH_MAX}u) t.starReach = {gpuemu.STAR_REACH_MAX}u;" in hk
    assert f"if (t.starEvenReach > {gpuemu.STAR_EVEN_REACH_MAX}u) t.starEvenReach = {gpuemu.STAR_EVEN_REACH_MAX}u;" in hk


def test_every_reference_parameter_reaches_the_emulator():
    assert {f.name for f in fields(StarfieldParams)} == {"even", "lift", "target_gain", "target_sigma", "keep_nits", "even_reach", "cap_nits", "area_lo",
                                                         "area_hi", "peak_hi", "reach", "nb_lo", "nb_hi", "strength"}
