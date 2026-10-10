"""The binary panel-parameter file for the DesktopLUT FALD shader round-trips the reference tables."""
import struct
import numpy as np
from dlc.fald.model import FaldModel, FaldParams
from dlc.fald.export import export_panel_params, kernel_tables, drive_curve_lut, MAGIC, CURVE_LOG_MIN, CURVE_LOG_MAX


def _model():
    return FaldModel(FaldParams(est_phase_px=-18.0, est_phase_py=-24.5, est_aniso=0.85, est_support_cells=5,
                                kernel_pnorm=1.75, core_mm=10.8, tail_mm=32.4, tail_frac=0.45))


def test_export_roundtrip(tmp_path):
    m = _model()
    info = export_panel_params(m, tmp_path / "panel.bin", (0.25, 4.0))
    b = (tmp_path / "panel.bin").read_bytes()
    assert len(b) == info["bytes"]
    ints = struct.unpack("<13I", b[:52]); floats = struct.unpack("<13f", b[52:104])
    assert ints[0] == MAGIC and ints[1:4] == (m.p.cols, m.p.rows, m.p.sub)
    assert ints[4:6] == (80, 45)
    assert abs(floats[0] - m.p.white_nits) < 1e-3 and abs(floats[2] - m.p.stat_area0_px2) < 1e-3
    kt, ke = kernel_tables(m)
    rt_c, rt_r, re_c, re_r, n = ints[8], ints[9], ints[10], ints[11], ints[12]
    assert kt.shape == (m.p.sub, m.p.sub, 2 * rt_r + 1, 2 * rt_c + 1)
    assert ke.shape == (m.p.sub, m.p.sub, 2 * re_r + 1, 2 * re_c + 1)
    off = 128
    curve = np.frombuffer(b[off:off + 4 * n], np.float32); off += 4 * n
    assert np.allclose(curve, drive_curve_lut(m))
    kt2 = np.frombuffer(b[off:off + 4 * kt.size], np.float32).reshape(kt.shape); off += 4 * kt.size
    ke2 = np.frombuffer(b[off:], np.float32).reshape(ke.shape)
    assert np.array_equal(kt2, kt) and np.array_equal(ke2, ke)
    # kernels are the model's own: unit mean sum over sub-offsets (a full field gives B = 1)
    assert abs(kt.sum(axis=(2, 3)).mean() - 1.0) < 1e-5
    assert abs(ke.sum(axis=(2, 3)).mean() - 1.0) < 1e-5


def test_curve_lut_matches_drive_of():
    m = _model()
    curve = drive_curve_lut(m)
    ln = np.linspace(CURVE_LOG_MIN, CURVE_LOG_MAX, len(curve))
    for nits in (0.3, 2.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 1842.0):
        lut = float(np.interp(np.log(nits), ln, curve))
        ref = float(m.drive_of(np.array([nits]))[0])
        assert abs(lut - ref) < 2e-3, (nits, lut, ref)


def test_lattice_fit_matches_the_export_arithmetic():
    """The preflight's pitch check uses the export's own arithmetic (choose_scale canvas -> header cell px)."""
    from dlc.fald.export import lattice_fit
    pa = lattice_fit(3840, 2160, 48, 48)
    assert pa["exact"] and pa["fits"] and pa["cell_px"] == [80, 45] and pa["uncovered_px"] == [0, 0]
    qhd = lattice_fit(2560, 1440, 48, 24)                     # 53.3-px pitch -> 55-px cells: the loader refuses it
    assert not qhd["fits"] and qhd["lattice_px"] == [2640, 1440]
    uw = lattice_fit(3440, 1440, 48, 24)                      # 71.7-px pitch -> 70-px cells: fits, 80 px uncovered
    assert uw["fits"] and not uw["exact"] and uw["lattice_px"] == [3360, 1440] and uw["uncovered_px"] == [80, 0]
    assert uw["max_drift_px"] == [80.0, 0.0] and abs(uw["max_drift_cells"][0] - 80 / (3440 / 48)) < 1e-3
    assert uw["max_drift_cells"][0] > 1.0                      # the far-edge zones land more than a cell off
    odd = lattice_fit(2560, 1440, 40, 25)                     # an integer 64-px pitch still rescales (rows 57.6 px)
    assert not odd["fits"] and odd["cell_px"] == [65, 60]


def test_export_header_cells_are_the_lattice_fit_cells(tmp_path):
    """The file's header words 4/5 are what lattice_fit predicted for the geometry the fit was built on."""
    from dlc.fald import profile as P
    from dlc.fald.export import export_panel_params, lattice_fit
    from dlc.fald.model import FaldModel
    g = P.PanelGeometry.from_diagonal(3440, 1440, 48, 24, 34.0, meter=(1720, 720), white_nits=1000.0)
    info = export_panel_params(FaldModel(g.base_params()), tmp_path / "uw.bin")
    assert info["header_ints"][4:6] == lattice_fit(3440, 1440, 48, 24)["cell_px"]
