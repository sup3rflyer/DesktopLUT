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
