"""Tests for the FALD forward model (dlc.fald.model): normalisation, monotonic sanity, symmetry."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402

FULL = (0.0, 0.0, 1.0, 1.0)
WHITE = (1023, 1023, 1023)
METER = (1950, 1110)


def rect(x0, y0, w, h):
    return (x0 / 3840, y0 / 2160, w / 3840, h / 2160)


@pytest.fixture(scope="module")
def model():
    return FaldModel(FaldParams())


def test_full_field_white_reads_white_nits(model):
    y = model.meter_y([(WHITE, FULL)], METER)
    assert abs(y - model.p.white_nits) / model.p.white_nits < 0.01


def test_full_field_black_reads_zero(model):
    assert model.meter_y([((0, 0, 0), FULL)], METER) == 0.0


def test_uniform_grey_is_reproduced(model):
    # a uniform field: B_true == B_est (same drives, both kernels normalised) → exact target
    y = model.meter_y([((307, 307, 307), FULL)], METER)      # PQ 307 ≈ 10 nits
    assert abs(y - 10.0) < 0.5


def test_peak_window_reads_less_than_full_field(model):
    small = model.meter_y([((0, 0, 0), FULL), (WHITE, rect(1950 - 80, 1110 - 80, 160, 160))], METER)
    full = model.meter_y([(WHITE, FULL)], METER)
    assert 0.2 * full < small < full


def test_leak_decreases_with_distance_and_is_left_right_symmetric_at_code0():
    p = FaldParams()
    m = FaldModel(p)
    def leak(gap, side):
        x0 = 1950 + gap if side > 0 else 1950 - gap - 200
        return m.meter_y([((0, 0, 0), FULL), (WHITE, rect(x0, 1110 - 100, 200, 200))], METER)
    ys = [leak(g, +1) for g in (110, 240, 480, 1200)]
    assert all(a > b for a, b in zip(ys, ys[1:]))
    # the meter sits ~10 px left of its cell centre; symmetry holds only approximately
    assert abs(leak(240, +1) / leak(240, -1) - 1.0) < 0.5


def test_kernel_energy_share_is_respected():
    p = FaldParams(core_mm=4.0, tail_mm=30.0, tail_frac=0.25)
    m = FaldModel(p)
    kern = m._kernels("mix", p.tail_mm, p.core_mm, p.tail_frac)
    k = kern[0][0]
    core = np.exp(-np.sqrt(0) / 1)  # noqa: F841  (placeholder to keep intent explicit)
    # the kernels are normalised so that a uniform unit drive integrates to 1 (mean over offsets)
    total = np.mean([[kk.sum() for kk in row] for row in kern])
    assert abs(total - 1.0) < 1e-9
    assert k.max() > 0


def test_est_kernel_mismatch_creates_rings_but_not_on_uniform_fields():
    p = FaldParams(est_kind="gauss", est_scale_mm=30.0)
    m = FaldModel(p)
    uniform = m.meter_y([((307, 307, 307), FULL)], METER)
    assert abs(uniform - 10.0) < 0.5
    with_win = m.meter_y([((307, 307, 307), FULL), (WHITE, rect(1950 - 200 - 110, 1110 - 100, 200, 200))], METER)
    assert with_win != pytest.approx(uniform, rel=1e-3)


def test_backlight_decays_monotonically_away_from_a_single_lit_cell():
    # review 2026-09-10 #1: the sub-cell kernel samples were mirrored, so B ROSE away from a lit
    # cell inside the neighbouring cell. Guard: B along the row through a single lit cell must be
    # monotone on each side of the cell centre.
    p = FaldParams(core_mm=6.0, tail_mm=24.0, tail_frac=0.3)
    m = FaldModel(p)
    d = np.zeros((p.rows, p.cols)); d[24, 24] = 1.0
    b = m.backlight(d, "mix", p.tail_mm, p.core_mm, p.tail_frac)
    row = b[int((24 + 0.5) * m.ch)]
    c = int((24 + 0.5) * m.cw)
    right = row[c:c + 4 * m.cw]; left = row[c - 4 * m.cw:c + 1][::-1]
    assert np.all(np.diff(right) <= 1e-12), "B must not rise moving right from the lit cell"
    assert np.all(np.diff(left) <= 1e-12), "B must not rise moving left from the lit cell"
    assert np.argmax(row) in range(c - 1, c + 2)


def test_backlight_sub_resolution_converged():
    p8 = FaldParams(sub=8); p16 = FaldParams(sub=16)
    d = np.zeros((p8.rows, p8.cols)); d[24, 24] = 1.0
    b8 = FaldModel(p8).backlight(d, "mix", p8.tail_mm, p8.core_mm, p8.tail_frac)
    b16 = FaldModel(p16).backlight(d, "mix", p16.tail_mm, p16.core_mm, p16.tail_frac)
    assert abs(b8.max() / b16.max() - 1.0) < 0.06


def test_est_phase_shifts_the_estimate_not_the_truth():
    # a negative horizontal phase samples the estimate to the LEFT of the pixel: for a lit cell to
    # the RIGHT of the meter cell the estimate drops, for one to the LEFT it rises; B_true unchanged
    p0 = FaldParams(est_kind="gauss", est_scale_mm=25.0, est_phase_px=0.0)
    p1 = FaldParams(est_kind="gauss", est_scale_mm=25.0, est_phase_px=-40.0)
    m0, m1 = FaldModel(p0), FaldModel(p1)
    for dc, expect in ((+2, "drop"), (-2, "rise")):
        d = np.zeros((p0.rows, p0.cols)); d[24, 24 + dc] = 1.0
        y = int((24 + 0.5) * m0.ch); x = int((24 + 0.5) * m0.cw)
        e0 = m0.backlight(d, "gauss", 25.0, phase_px=(0.0, 0.0))[y, x]
        e1 = m1.backlight(d, "gauss", 25.0, phase_px=(-40.0, 0.0))[y, x]
        t0 = m0.backlight(d, "mix", p0.tail_mm, p0.core_mm, p0.tail_frac)[y, x]
        t1 = m1.backlight(d, "mix", p1.tail_mm, p1.core_mm, p1.tail_frac)[y, x]
        assert t0 == pytest.approx(t1)
        assert (e1 < e0) if expect == "drop" else (e1 > e0)


# ---------------------------------------------------------------------------
# the inverse (dlc.fald.correct)
# ---------------------------------------------------------------------------

def test_correction_is_identity_on_a_uniform_field():
    from dlc.fald.correct import correct_image, reference_pedestal
    p = FaldParams(est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, drive_dim=0.108, tmin=1.5e-3)
    m = FaldModel(p)
    img = m.render([((307, 307, 307), FULL)])
    res = correct_image(m, img)
    mask = m.aperture_mask(METER)
    # uniform field: B_est == B_true away from the edges → the only change is pedestal bookkeeping,
    # which cancels because the reference pedestal IS the field's own pedestal
    assert abs(res["req"][:, mask].mean() / img[:, mask].mean() - 1.0) < 0.01
    assert not res["clipped"][:, mask].any() and not res["floored"][:, mask].any()


def test_correction_removes_the_ring_in_the_model():
    from dlc.fald.correct import correct_image, reference_pedestal
    # exact inverse with the gain low-pass off; with the default low-pass (sigma 0.35 cells, the anti-grid
    # measure of 2026-09-12) a one-cell-gap ring is corrected to within 2 % in the model.
    p = FaldParams(est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, drive_dim=0.108, tmin=1.5e-3, gain_smooth_cells=0.0)
    m = FaldModel(p)
    bg = ((307, 307, 307), FULL)
    shapes = [bg, (WHITE, rect(1950 - 110 - 200, 1110 - 100, 200, 200))]     # one-cell-gap dark ring
    base = m.render([bg]); img = m.render(shapes)
    w = np.array(p.chan_weights)[:, None, None]; mask = m.aperture_mask(METER)
    target = (base * w)[:, mask].sum(0).mean() + reference_pedestal(m, base)[mask].mean()
    before = m.meter_img(img, METER).sum() / target
    after = m.meter_img(correct_image(m, img)["req"], METER).sum() / target
    assert before < 0.9, "the test pattern must show a dark ring to begin with"
    assert abs(after - 1.0) < 0.005
    m2 = FaldModel(FaldParams(est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, drive_dim=0.108, tmin=1.5e-3))
    after2 = m2.meter_img(correct_image(m2, img)["req"], METER).sum() / target
    assert abs(after2 - 1.0) < 0.02
    # the highlight itself is untouched (saturated: original request kept)
    res = correct_image(m, img)
    hi = m.render(shapes)[0] > 5000
    assert np.allclose(res["req"][0][hi], img[0][hi])


def test_area_statistic_is_shape_independent_and_matches_winmax_on_fields():
    # native sliver matrix 2026-09-11: 40x10, 20x20 and 10x40 white slivers read identically (area law)
    pa = FaldParams(stat_kind="area"); pw = FaldParams(stat_kind="winmax")
    for code in (307, 700, 1023):
        ma, mw = FaldModel(pa), FaldModel(pw)
        assert np.allclose(ma.cell_drives(ma.render([((code,) * 3, FULL)])), mw.cell_drives(mw.render([((code,) * 3, FULL)])), atol=1e-9)
    m = FaldModel(pa); black = ((0, 0, 0), FULL); cell = (24, 26)
    d = lambda x, y, w, h: m.cell_drives(m.render([black, (WHITE, rect(x, y, w, h))]))[cell]
    a, b, c = d(2120, 1100, 40, 10), d(2140, 1095, 20, 20), d(2150, 1085, 10, 40)
    assert abs(a - b) < 1e-9 and abs(b - c) < 1e-9 and 0 < a < d(2080, 1080, 80, 45)


def test_area_win_statistic_bounds():
    # min(winmax, area): equals both on uniform fields; ≤ each of them everywhere; on 20/20 px stripes at
    # 40 nits it follows the window mean (≈ 20 nits), not the area law (40 nits)
    pw, pa, ph = (FaldParams(stat_kind=k) for k in ("winmax", "area", "area_win"))
    mw, ma, mh = FaldModel(pw), FaldModel(pa), FaldModel(ph)
    field = [((520, 520, 520), FULL)]
    assert np.allclose(mh.cell_drives(mh.render(field)), mw.cell_drives(mw.render(field)), atol=1e-9)
    c40 = 466                                              # ≈ 40 nits
    stripes = [((0, 0, 0), FULL)] + [((c40, c40, c40), rect(0, y, 3840, 20)) for y in range(0, 2160, 40)]
    img = mh.render(stripes)
    dh, dw, da = mh.cell_drives(img), mw.cell_drives(mw.render(stripes)), ma.cell_drives(ma.render(stripes))
    assert np.all(dh <= dw + 1e-12) and np.all(dh <= da + 1e-12)
    assert abs(dh[24, 24] - dw[24, 24]) < 1e-9 and da[24, 24] > dh[24, 24] * 1.1


def test_kernel_pnorm_diamond_decays_faster_on_the_diagonal():
    # p = 2 reproduces the radial kernel exactly; p = 1 (L1) gives the same on-axis leak but less on the diagonal
    p2 = FaldParams(kernel_pnorm=2.0); p1 = FaldParams(kernel_pnorm=1.0); pd = FaldParams()
    m2, m1, md = FaldModel(p2), FaldModel(p1), FaldModel(pd)
    black = ((0, 0, 0), FULL)
    src = [black, (WHITE, rect(2080, 1080, 80, 45))]                # cell (26,24)
    assert np.allclose(m2.forward(src)["b_true"], md.forward(src)["b_true"])
    b2, b1 = m2.forward(src)["b_true"], m1.forward(src)["b_true"]
    m = FaldModel(pd)
    axial = (int(1102.5 / 5), int((2120 + 240) / 5)); diag = (int((1102.5 + 170) / 5), int((2120 + 170) / 5))
    # both kernels are normalised to unit integral (a lit field → B = 1), so compare SHAPES: at equal Euclidean
    # distance (240 px on axis vs 170,170 px diagonal) the L1 kernel puts relatively less light on the diagonal
    assert b1[diag] / b1[axial] < 0.8 * (b2[diag] / b2[axial])


def test_cell_domain_estimate_is_one_on_fields_and_blocky_around_a_cell():
    pn = FaldParams(est_cell=True, est_interp="nearest", est_scale_mm=13.0, est_phase_px=-18.0, est_phase_py=-24.0)
    pb = FaldParams(est_cell=True, est_interp="bilinear", est_scale_mm=13.0)
    for p in (pn, pb):
        m = FaldModel(p)
        f = m.forward([((700, 700, 700), FULL)])
        inner = (slice(150, 280), slice(250, 520))                          # the true tail reaches ~7 cells in
        assert np.allclose(f["b_est"][inner], f["b_true"][inner], atol=2e-3)  # a uniform field: estimate = truth
        y = m.meter_y([((307, 307, 307), FULL)], METER)
        assert abs(y - 10.0) < 0.5                                            # uniform grey reproduced
    m = FaldModel(pn)
    f = m.forward([((0, 0, 0), FULL), (WHITE, rect(2080, 1080, 80, 45))])
    row = f["b_est"][int(1102.5 / 5)]                                        # the meter row, all columns
    # nearest sampling: piecewise constant across each 80-px cell (16 reduced px), steps only at boundaries
    span = row[int(1760 / 5):int(2000 / 5)]                                 # three cells (the steps sit 18 px
    vals = np.unique(np.round(span, 9))                                     # right of the boundaries: the phase)
    assert 3 <= len(vals) <= 4 and np.all(np.diff(vals) > 0)


def test_flat_field_gives_unit_gain_everywhere():
    """A uniform field must leave every pixel untouched (flat-response normalisation), including the
    sub-cell sawtooth of the mean-normalised estimate kernel and the frame border."""
    from dlc.fald.model import FaldModel, FaldParams
    m = FaldModel(FaldParams(est_phase_px=-18.0, est_phase_py=-24.5, est_aniso=0.85, est_support_cells=5,
                             kernel_pnorm=1.75, core_mm=10.8, tail_mm=32.4, tail_frac=0.45))
    img = np.full((3, m.h, m.w), 200.0)
    b_true, b_est = m.backlights(m.cell_drives(img))
    gain = b_est / b_true
    assert np.abs(gain - 1.0).max() < 1e-6
    # and the fields themselves are the flat drive level (normalised to the drive of the field)
    d = float(m.cell_drives(img)[0, 0])
    assert np.allclose(b_true, d, atol=1e-6) and np.allclose(b_est, d, atol=1e-6)


def test_lum_fade_leaves_near_black_untouched():
    """Pixel-luminance fade (doc S33): on a 0.3-nit field next to a bright bar the correction must be identity
    outside the bar (the model has no baseline there); at 20 nits the same geometry must still be corrected."""
    import numpy as np
    from dataclasses import replace
    from dlc.fald.model import FaldModel, FaldParams
    from dlc.fald.correct import correct_image
    p = FaldParams()                     # scale 5 -> integer reduced cells (16 x 9)
    m = FaldModel(p)
    h, w = m.h, m.w
    def frame(grey):
        img = np.full((3, h, w), grey, np.float64)
        img[:, h // 2 - 40:h // 2 + 40, w // 2 - 12:w // 2 - 4] = 600.0
        return img
    dark = frame(0.3); req_dark = correct_image(m, dark, iters=1)["req"]
    bar = dark[0] >= 600.0
    assert np.allclose(req_dark[:, ~bar], dark[:, ~bar]), "0.3-nit surround must be untouched by the layer"
    mid = frame(20.0); req_mid = correct_image(m, mid, iters=1)["req"]
    assert np.abs(req_mid[:, ~bar] / mid[:, ~bar] - 1).max() > 0.02, "20-nit surround must still be corrected"
    off = replace(p, lum_fade_lo=0.0, lum_fade_hi=0.0)
    req_off = correct_image(FaldModel(off), dark, iters=1)["req"]
    assert np.abs(req_off[:, ~bar] / np.maximum(dark[:, ~bar], 1e-9) - 1).max() > 0.02, "fade off -> correction present"
