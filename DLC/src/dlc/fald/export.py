"""Export a fitted FALD model as the binary panel-parameter file the DesktopLUT shader loads.

The shader does NO kernel math of its own: the drive curve (as a log-spaced 1-D LUT) and the two
sub-cell kernel tables are tabulated here with the very same code the Python reference uses
(:meth:`FaldModel._kernels`, :meth:`FaldModel.drive_of`), so the GPU port can only differ from the
reference by interpolation and float precision.

File layout (little-endian, all float32 unless noted; header is 32 uint32/float32 words):
  word  0  magic 0x464C4431 ('FLD1')
  word  1  cols            word  2  rows           word  3  sub (samples per cell per axis)
  word  4  cell_w_px       word  5  cell_h_px      word  6  origin_x_px   word  7  origin_y_px
  word  8  reach_true_c    word  9  reach_true_r   (kernel half-extent in cells; table (2r+1) wide)
  word 10  reach_est_c     word 11  reach_est_r
  word 12  curve_n         word 13  f: white_nits  word 14  f: tmin       word 15  f: area0_px2
  word 16  f: w_r          word 17  f: w_g         word 18  f: w_b        (channel shares of white)
  word 19  f: gain_min     word 20  f: gain_max    word 21  f: drive_floor_nits
  word 22  f: curve_log_min  word 23  f: curve_log_max   (natural log of nits at LUT ends)
  word 24  f: est_phase_px   word 25  f: est_phase_py   (informational; already baked into K_est)
  word 26  f: fade_lo       word 27  f: fade_hi      (correction fades to identity for B_est below; 0,0 = loader defaults)
  word 28  f: gain_smooth_cells (Gaussian sigma of the gain low-pass, cells; 0 = off)
  word 29  f: lum_fade_lo   word 30  f: lum_fade_hi   (pixel-luminance fade, as-if-white nits; 0,0 = loader defaults)
  word 31  reserved (0)
FLD2 (magic 0x464C4432, written when the fit carries a pedestal colour, tmin_rgb): the same 32 words, then
8 more (header = 40 words = 160 bytes):
  word 32  f: m_r  word 33  f: m_g  word 34  f: m_b   (pedestal colour multipliers on tmin; Σ w_c·m_c = 1 keeps
                                                       the luminance fit; the LOADER stores them, the GUI
                                                       "per-channel pedestal" toggle decides whether the
                                                       shader uses them — off = white pedestal, as FLD1)
  word 35  ped_mode the fit was validated with: 0 = white, 1 = channel (informational; the GUI setting rules)
  word 36  f: ped_chroma_gain   (0 = default 1: the colour part of the pedestal term at model strength)
  word 37  f: chroma_lum_fade_lo  word 38  f: chroma_lum_fade_hi   (pixel-luminance fade of the COLOUR part only;
           both 0 with word 36 > 0 = NO fade; word 36 == 0 = follow lum_fade words 29/30)
  word 39  reserved (0)
The C++ reader is LoadFaldPanelParams (src/fald.cpp; tests/test_fald.cpp); words 26-30 are optional —
zero means 'loader default' so older files stay loadable; an FLD1 file loads with m = (1, 1, 1).
Keep the two in step when adding a word.
  then: curve[curve_n]                                  drive vs ln(nits), linear in ln(nits)
  then: k_true[sub][sub][2*reach_true_r+1][2*reach_true_c+1]   index order (oy, ox, j, i)
  then: k_est [sub][sub][2*reach_est_r+1][2*reach_est_c+1]
Kernel index i (columns) / j (rows) = sample cell minus source cell, as in the reference
(fftconvolve 'same' with the kernel centred: out[c] = Σ_i d[c-i]·k[i]).
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from .model import FaldModel, FaldParams

MAGIC = 0x464C4431
MAGIC2 = 0x464C4432          # 'FLD2': 40-word header (pedestal colour multipliers + validated mode)
PED_MODE_CODES = {"white": 0, "channel": 1}
CURVE_N = 1024
CURVE_LOG_MIN, CURVE_LOG_MAX = float(np.log(1e-2)), float(np.log(10000.0))


def kernel_tables(model: FaldModel):
    """(k_true, k_est) as arrays (sub, sub, 2r+1, 2c+1) with the reference's own kernel code."""
    p = model.p
    kt = model._kernels("mix", p.tail_mm, p.core_mm, p.tail_frac, pnorm=p.kernel_pnorm)
    phase = (p.est_phase_px * p.px_mm, p.est_phase_py * p.px_mm)
    if p.est_kind == "mix":
        ke = model._kernels("mix", p.est_tail_mm, p.est_core_mm, p.est_tail_frac, phase, p.est_aniso, p.est_support_cells)
    else:
        ke = model._kernels(p.est_kind, p.est_scale_mm, phase_mm=phase, aniso=p.est_aniso, support_cells=p.est_support_cells)
    return np.array(kt, dtype=np.float32), np.array(ke, dtype=np.float32)


def drive_curve_lut(model: FaldModel, n: int = CURVE_N) -> np.ndarray:
    ln = np.linspace(CURVE_LOG_MIN, CURVE_LOG_MAX, n)
    return model.drive_of(np.exp(ln)).astype(np.float32)


def export_panel_params(model: FaldModel, path: Path, gain_clip=(0.25, 4.0)) -> dict:
    p = model.p
    kt, ke = kernel_tables(model)
    curve = drive_curve_lut(model)
    rt_r, rt_c = (kt.shape[2] - 1) // 2, (kt.shape[3] - 1) // 2
    re_r, re_c = (ke.shape[2] - 1) // 2, (ke.shape[3] - 1) // 2
    v2 = p.tmin_rgb is not None
    header = [MAGIC2 if v2 else MAGIC, p.cols, p.rows, p.sub, int(round(p.cell_w)), int(round(p.cell_h)), 0, 0,
              rt_c, rt_r, re_c, re_r, len(curve)]
    floats = [p.white_nits, p.tmin, p.stat_area0_px2, *p.chan_weights, gain_clip[0], gain_clip[1],
              p.drive_floor_nits, CURVE_LOG_MIN, CURVE_LOG_MAX, p.est_phase_px, p.est_phase_py]
    buf = (struct.pack("<13I", *header) + struct.pack("<13f", *floats) + struct.pack("<2f", p.fade_lo, p.fade_hi)
           + struct.pack("<f", p.gain_smooth_cells) + struct.pack("<2f", p.lum_fade_lo, p.lum_fade_hi) + struct.pack("<I", 0))
    assert len(buf) == 32 * 4
    if v2:
        custom = p.ped_chroma_gain != 1.0 or p.ped_chroma_lum_fade is not None
        clo, chi = p.chroma_lum_fade() if p.ped_chroma_lum_fade is not None else (0.0, 0.0)
        buf += (struct.pack("<3f", *(float(x) for x in p.tmin_rgb)) + struct.pack("<I", PED_MODE_CODES[p.ped_mode])
                + struct.pack("<3f", float(p.ped_chroma_gain) if custom else 0.0, float(clo), float(chi)) + struct.pack("<I", 0))
        assert len(buf) == 40 * 4
    buf += curve.tobytes() + np.ascontiguousarray(kt).tobytes() + np.ascontiguousarray(ke).tobytes()
    Path(path).write_bytes(buf)
    return {"path": str(path), "bytes": len(buf), "k_true_shape": kt.shape, "k_est_shape": ke.shape,
            "curve_n": len(curve), "header_ints": header, "header_floats": floats,
            "format": "FLD2" if v2 else "FLD1", "header_bytes": 160 if v2 else 128}


def main(argv=None):
    import argparse, json
    from .correct import load_fitted_params
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fit_json", type=Path)
    ap.add_argument("out_bin", type=Path)
    ap.add_argument("--gain-min", type=float, default=0.25)
    ap.add_argument("--gain-max", type=float, default=4.0)
    a = ap.parse_args(argv)
    model = FaldModel(load_fitted_params(a.fit_json))
    info = export_panel_params(model, a.out_bin, (a.gain_min, a.gain_max))
    print(json.dumps({k: (list(v) if isinstance(v, tuple) else v) for k, v in info.items()}, indent=1, default=str))


if __name__ == "__main__":
    main()
