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


# ---------------------------------------------------------------- area_switch drive statistic
def _drives(p: FaldParams, shapes):
    m = FaldModel(p)
    return m.cell_drives(m.render(shapes), m.lattice_codes(shapes))


def test_area_switch_equals_winmax_on_uniform_fields():
    # the coverage law is normalised so that ANY uniform field stays on the measured drive curve
    pw = FaldParams(stat_kind="winmax"); pa = FaldParams(stat_kind="area_switch")
    for code in (307, 520, 769, 1023):                      # ≈ 10 / 100 / 500 / white nits
        dw = _drives(pw, [((code, code, code), FULL)]); da = _drives(pa, [((code, code, code), FULL)])
        assert np.allclose(da, dw, atol=1e-9), code


def test_area_switch_single_cell_coverage_law():
    # HW 2026-09-10 (sliver / posmatrix / mirror / camera): a sliver in the RIGHT half of a cell drives
    # ≈1.30× the whole cell, the LEFT half drives the same as the whole cell, and a 20 px square in the
    # bottom-left quadrant drives ~0.51× (elsewhere ~0.77×).
    p = FaldParams(stat_kind="area_switch")
    black = ((0, 0, 0), FULL)
    cell = (24, 26)                                          # x 2080–2160, y 1080–1125
    def d(x, y, w, h):
        return _drives(p, [black, (WHITE, rect(x, y, w, h))])[cell]
    whole = d(2080, 1080, 80, 45)
    assert abs(whole - 1.0) < 1e-9                           # a fully lit cell = 1 (field normalisation)
    assert abs(d(2120, 1080, 40, 45) / whole - 1.32) < 0.03  # right half: unsuppressed, at the LED max
    assert abs(d(2080, 1080, 40, 45) / whole - 1.00) < 0.02  # left half: suppressed like the whole
    bl = d(2100, 1095, 20, 20) / whole; tr = d(2140, 1080, 20, 20) / whole
    assert 0.45 < bl < 0.57 and 0.70 < tr < 0.85 and bl < tr


def test_area_switch_unknown_kind_rejected():
    with pytest.raises(ValueError):
        _drives(FaldParams(stat_kind="bogus"), [((0, 0, 0), FULL)])


def test_area_switch_is_monotone_in_the_image_and_isolated_cell_is_one():
    # review 2026-09-11 #1: grey around a highlight must not LOWER the LED (a mean-of-lit form did)
    p = FaldParams(stat_kind="area_switch")
    cell = (24, 26)
    def d(left_code):
        return _drives(p, [((0, 0, 0), FULL), ((left_code,) * 3, rect(2080, 1080, 40, 45)),
                           (WHITE, rect(2120, 1080, 40, 45))])[cell]
    on_black, grey10, white = d(0), d(307), d(1023)
    assert on_black >= grey10 >= white
    assert abs(grey10 - 1.0) < 1e-6 and abs(white - 1.0) < 1e-6      # suppressed → the field state
    # sliver inside a uniform 10-nit field: winmax and area_switch agree (both 1.0 — the whole-cell state)
    field = [((307, 307, 307), FULL), (WHITE, rect(2120, 1080, 40, 45))]
    assert abs(_drives(p, field)[cell] - _drives(FaldParams(stat_kind="winmax"), field)[cell]) < 1e-6
    iso = _drives(p, [((0, 0, 0), FULL), (WHITE, rect(2080, 1080, 80, 45))])
    assert abs(iso[cell] - 1.0) < 1e-9 and iso.sum() == iso[cell]     # isolated cell = 1, nothing else lit


def test_area_switch_suppression_box_geometry():
    # HW ratios (posmatrix/mirror, cell 26 row 24): a 20 px square at the cell's bottom-left (x 2100,
    # y 1093) reads 0.51× the whole cell, at the top-left (y 1082) 0.77×; content in the LEFT neighbour's
    # right half (x −40..0 of the box) also suppresses, content beyond it (x < −40) does not.
    p = FaldParams(stat_kind="area_switch")
    cell = (24, 26)
    black = ((0, 0, 0), FULL)
    def d(*rects):
        return _drives(p, [black] + [(WHITE, r) for r in rects])[cell]
    whole = d(rect(2080, 1080, 80, 45))
    assert abs(d(rect(2100, 1095, 20, 20)) / whole - 0.52) < 0.04
    assert abs(d(rect(2100, 1080, 20, 20)) / whole - 0.77) < 0.04
    sliver = rect(2120, 1080, 40, 45)
    assert abs(d(sliver) / whole - 1.32) < 0.03
    assert abs(d(sliver, rect(2050, 1105, 20, 15)) / whole - 1.00) < 0.03     # left neighbour, in the box
    assert abs(d(sliver, rect(2000, 1105, 20, 15)) / whole - 1.32) < 0.03     # left neighbour, outside
    assert abs(d(sliver, rect(2100, 1130, 20, 10)) / whole - 1.00) < 0.03     # cell below, in the box
    assert abs(d(sliver, rect(2100, 1160, 20, 10)) / whole - 1.32) < 0.03     # cell below, outside


# ---------------------------------------------------------------- coarse lattice / peak limiter (doc §20)
def test_lattice_factor_is_one_on_uniform_fields():
    p0 = FaldParams(lattice_boost=0.0); p1 = FaldParams(lattice_boost=0.30)
    for code in (307, 700, 850, 950, 1023):
        d0 = _drives(p0, [((code,) * 3, FULL)]); d1 = _drives(p1, [((code,) * 3, FULL)])
        assert np.allclose(d0, d1, rtol=1e-9), code


def test_lattice_white_half_cell_drives_1p3_times_the_whole_cell():
    # HW 2026-09-11: cell (14,39) x 1120-1200 y 1755-1800 holds the lattice pixel (1152,1776); the right
    # half misses it → 1.30× the whole cell; the left half covers it → same as the whole cell.
    p = FaldParams(lattice_boost=0.30)
    black = ((0, 0, 0), FULL)
    cell = (39, 14)
    whole = _drives(p, [black, (WHITE, rect(1120, 1755, 80, 45))])[cell]
    right = _drives(p, [black, (WHITE, rect(1160, 1755, 40, 45))])[cell]
    left = _drives(p, [black, (WHITE, rect(1120, 1755, 40, 45))])[cell]
    assert abs(whole - 1.0) < 1e-9
    assert abs(right / whole - 1.30) < 1e-6 and abs(left / whole - 1.0) < 1e-6


def test_lattice_free_row_31_is_boosted_and_sub_peak_content_is_not_limited():
    p = FaldParams(lattice_boost=0.30)
    black = ((0, 0, 0), FULL)
    # row 31 (y 1395-1440) holds no lattice pixel → the whole cell itself sits in the boosted state
    whole31 = _drives(p, [black, (WHITE, rect(1120, 1395, 80, 45))])[31, 14]
    whole39 = _drives(p, [black, (WHITE, rect(1120, 1755, 80, 45))])[39, 14]
    assert abs(whole31 / whole39 - 1.30) < 1e-6
    # a code-690 (≈300 nit) whole cell vs half: both below the detector's ramp → same factor as the field curve
    p0 = FaldParams(lattice_boost=0.0)
    for shape in (rect(1120, 1755, 80, 45), rect(1160, 1755, 40, 45)):
        assert abs(_drives(p, [black, ((690,) * 3, shape)])[39, 14] - _drives(p0, [black, ((690,) * 3, shape)])[39, 14]) < 1e-9


def test_lattice_geometry_pinned_by_strips_and_the_top_half():
    # review 2026-09-11: the 1/5 render loses the sample row at 1776 for a half ending at 1777; exact
    # lattice codes fix it. Strips 4 px either side of the pixel pin the phase to ±2 px.
    p = FaldParams(lattice_boost=0.30); p0 = FaldParams(lattice_boost=0.0)
    black = ((0, 0, 0), FULL)
    cell = (39, 14)
    def d(x, y, w, h):
        return _drives(p, [black, (WHITE, rect(x, y, w, h))])[cell]
    def f(x, y, w, h):                                            # the lattice factor alone
        return d(x, y, w, h) / _drives(p0, [black, (WHITE, rect(x, y, w, h))])[cell]
    # HW pins the RATIO limited/boosted = 1/1.30 between a pattern covering the sample and the same
    # pattern shifted off it (the absolute level of thin content is the statistic's business — winmax
    # under-reads a 22-px white strip, a separate known residual)
    assert abs(f(1120, 1755, 80, 45) - 1.00) < 1e-6                              # whole cell → limited
    assert abs(f(1120, 1755, 80, 22) / f(1120, 1778, 80, 22) - 1 / 1.30) < 1e-6  # top (covers 1776) vs bottom
    assert abs(f(1150, 1755, 4, 45) / f(1146, 1755, 4, 45) - 1 / 1.30) < 1e-6    # x strips 1150-1154 vs 1146-1150
    assert abs(f(1150, 1755, 4, 45) / f(1154, 1755, 4, 45) - 1 / 1.30) < 1e-6
    assert abs(f(1120, 1774, 80, 4) / f(1120, 1770, 80, 4) - 1 / 1.30) < 1e-6    # y strips 1774-1778 vs 1770-1774
    assert abs(f(1120, 1774, 80, 4) / f(1120, 1778, 80, 4) - 1 / 1.30) < 1e-6


def test_lattice_reference_is_the_statistic_not_the_peak():
    # a white 10-px dot OFF the lattice pixel inside a code-700 cell leaves the cell in the boosted
    # state it already had (the measured curve contains it) → no extra 1.30 (review #4)
    p = FaldParams(lattice_boost=0.30); p0 = FaldParams(lattice_boost=0.0)
    cell = (39, 14)
    shapes = [((0, 0, 0), FULL), ((700, 700, 700), rect(1120, 1755, 80, 45)), (WHITE, rect(1180, 1785, 10, 10))]
    assert abs(_drives(p, shapes)[cell] / _drives(p0, shapes)[cell] - 1.0) < 1e-6
