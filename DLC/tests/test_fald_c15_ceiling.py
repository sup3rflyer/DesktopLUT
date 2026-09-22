"""Work guide C15 (2026-09-22): the soft knee's ceiling reads the LOW-PASSED B_est.

The shipped HDR estimate kernel ("knots", fit of 2026-09-11) drops e^-3 within half a zone, so B_est peaks sharply at every
LED sample point (zone centre - phase). With a per-pixel ceiling the knee let a small bright shape on black brighten only
near those points once it bound (>= ~500 nits): a lattice of lobes the owner saw and photographed (passthrough clean). The
low-passed ceiling (same filter as the gain) removes the lobes and leaves shapes the knee never reaches untouched."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.correct import correct_image  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402

# the shipped HDR fit (results/fald_native_2026-09-11/fald_fit_result_area.json), on a 12 x 12 lattice of 80 x 45-px zones
SHIPPED = dict(
    stat_area0_px2=1150.0, drive_floor_nits=0.5, drive_min_gain=1.1, drive_dim=0.0862611416278182, drive_gamma=0.5,
    drive_curve=((10.0, 0.0), (30.0, 0.131), (100.0, 0.18), (300.0, 0.362), (600.0, 0.544), (1000.0, 0.745), (1842.0, 1.0)),
    core_mm=10.769424619800093, tail_mm=32.42251614036379, tail_frac=0.44737663249587, kernel_pnorm=1.750157591007142,
    est_kind="knots", est_knot_cells=(0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0),
    est_knot_logw=(0.0, -3.0427351426456704, -3.7493199934938803, -3.7973443005453213, -4.450900468806899, -5.393243815142592,
                   -7.293607570699559, -7.356774387689927),
    est_phase_px=-16.119420283992007, est_phase_py=-24.4376470061224, est_aniso=0.8235168419364751, est_support_cells=5,
    white_nits=1842.0, tmin=0.0010169605635425812, flat_norm=True, fade_lo=0.004, fade_hi=0.03, gain_smooth_cells=0.35,
    lum_fade_lo=0.5, lum_fade_hi=5.0, width=960, height=540, cols=12, rows=12)


@pytest.fixture(scope="module")
def model():
    return FaldModel(FaldParams(**SHIPPED))


def _circle(m, nits, diam_px):
    yy, xx = np.mgrid[0:m.h, 0:m.w]
    cx, cy, r = m.w / 2 + 3.4, m.h / 2 + 2.2, diam_px / 2 / 5.0          # off-lattice centre; raster = 1/5 of the frame
    img = np.zeros((3, m.h, m.w))
    img[:, np.hypot(xx - cx, yy - cy) <= r] = nits
    return img, (cx, cy, r)


def _spike_contrast(m, ratio, geo):
    """Mean applied scale AT the estimate's LED sample points (p + phase = a zone centre) over the mean half a zone away,
    inside 0.7 r of the circle, % (0 = no lattice)."""
    cx, cy, r = geo
    fx, fy = m.p.est_phase_px / 5.0, m.p.est_phase_py / 5.0
    at, mid = [], []
    for zy in range(m.p.rows):
        for zx in range(m.p.cols):
            px, py = (zx + 0.5) * m.cw - fx, (zy + 0.5) * m.ch - fy
            for qx, qy, lst in ((px, py, at), (px + m.cw / 2, py, mid), (px, py + m.ch / 2, mid), (px + m.cw / 2, py + m.ch / 2, mid)):
                ix, iy = int(round(qx)), int(round(qy))
                if 0 <= iy < m.h and 0 <= ix < m.w and np.hypot(ix - cx, iy - cy) <= 0.7 * r:
                    lst.append(ratio[iy, ix])
    assert at and mid
    return 100.0 * (np.mean(at) / np.mean(mid) - 1.0)


@pytest.mark.parametrize("nits,diam", [(500, 160), (500, 240), (1000, 240), (1000, 400)])
def test_no_led_lattice_inside_small_bright_shapes(model, nits, diam):
    # before C15 (per-pixel ceiling): +24.2 / +3.4 / +4.4 / +2.0 %
    img, geo = _circle(model, nits, diam)
    ratio = correct_image(model, img)["req"][1] / nits
    assert abs(_spike_contrast(model, ratio, geo)) < 1.5


def _emu_spike(m, emu, nits, diam, c15):
    """The same contrast on the GPU twin at full resolution (the frame is 5x the model raster)."""
    img, (cx, cy, r) = _circle(m, nits, diam)
    frame = np.repeat(np.repeat(img, 5, axis=1), 5, axis=2).transpose(1, 2, 0) / 80.0
    emu.c15 = c15
    res = emu.run(frame, fp16_out=False)
    ratio = res["out_nits"][1] / np.maximum(res["img"][1], 1e-9)
    # sample the full-resolution ratio at the raster points' centre pixels
    return _spike_contrast(m, ratio[2::5, 2::5], (cx, cy, r)), res["out_nits"]


def test_gpu_twin_c15_switch_removes_the_lobes_and_nothing_else(model, tmp_path):
    from dlc.fald.export import export_panel_params
    from dlc.fald.gpuemu import Emu
    from dlc.fald.panelfile import read_panel_file
    export_panel_params(model, tmp_path / "panel.bin")
    emu = Emu(read_panel_file(tmp_path / "panel.bin"), width=model.p.width, height=model.p.height)
    before, _ = _emu_spike(model, emu, 500, 160, c15=False)
    after, _ = _emu_spike(model, emu, 500, 160, c15=True)
    assert before > 10.0 and abs(after) < 1.5, (before, after)
    # 100 nits: the knee never binds, so the ceiling is never read — bit for bit the pre-C15 output
    _, out_old = _emu_spike(model, emu, 100, 240, c15=False)
    _, out_new = _emu_spike(model, emu, 100, 240, c15=True)
    assert np.array_equal(out_old, out_new)
