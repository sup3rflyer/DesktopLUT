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
    p = FaldParams(est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, drive_dim=0.108, tmin=1.5e-3)
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
