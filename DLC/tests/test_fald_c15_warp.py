"""Opt-in: the soft knee's low-passed ceiling (work guide C15) on a real D3D device — the C++ WARP case (tests/test_fald.cpp
"FALD temporal modes on WARP") fed with small bright circles on black and the SHIPPED HDR estimate kernel (knots: a sharp
peak at every LED sample point), its dumps replayed against the GPU-order twin (gpuemu.Emu, c15 on).

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_c15_warp.py <dir>                           # panel.bin + frame.rgba16f + d0..d6
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"
    python -m pytest tests/test_fald_c15_warp.py -n0
Without the variable / the dumps the test is skipped."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
W, H = 960, 540
# (centre x, centre y, diameter px, nits): the knee binds in both (>= 500 nits, small), off-lattice centres
CIRCLES = ((243.0, 272.0, 160.0, 500.0), (717.0, 268.0, 240.0, 1000.0))


def _params() -> FaldParams:
    from test_fald_c15_ceiling import SHIPPED
    return FaldParams(**SHIPPED)


def frame_half() -> np.ndarray:
    yy, xx = np.mgrid[0:H, 0:W]
    nits = np.zeros((H, W))
    for cx, cy, d, v in CIRCLES:
        nits[np.hypot(xx + 0.5 - cx, yy + 0.5 - cy) <= d / 2] = v
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = (nits / 80.0).astype(np.float16)[:, :, None]      # grey: as-if-white nits = scRGB x 80 (PQ transfer)
    return out


def write_inputs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(_params()), root / "panel.bin")
    frame_half().tofile(root / "frame.rgba16f")
    for i in range(7):
        (root / f"d{i}").mkdir(exist_ok=True)


def _spike(emu: Emu, ratio: np.ndarray, cx: float, cy: float, r: float) -> float:
    """Mean applied scale at the estimate's LED sample points (p + phase = a zone centre) over the mean half a zone away,
    inside 0.7 r, % (0 = no lattice)."""
    o = emu.o
    fx, fy = float(o["estPhasePx"]), float(o["estPhasePy"])
    at, mid = [], []
    for zy in range(emu.rows):
        for zx in range(emu.cols):
            px, py = (zx + 0.5) * emu.cw - fx, (zy + 0.5) * emu.ch - fy
            for qx, qy, lst in ((px, py, at), (px + emu.cw / 2, py, mid), (px, py + emu.ch / 2, mid),
                                (px + emu.cw / 2, py + emu.ch / 2, mid)):
                ix, iy = int(qx), int(qy)
                if 0 <= iy < H and 0 <= ix < W and np.hypot(ix + 0.5 - cx, iy + 0.5 - cy) <= 0.7 * r:
                    lst.append(ratio[iy, ix])
    assert at and mid
    return 100.0 * (np.mean(at) / np.mean(mid) - 1.0)


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "d0" / "fald_gain_fine.rg32f").exists(),
                    reason="FALD_TEST_WARP_DIR with C15 WARP dumps not given")
def test_warp_knee_ceiling_is_the_twin_and_draws_no_lattice():
    root = Path(_DIR)
    d = root / "d0"
    o = read_panel_file(root / "panel.bin")
    emu = Emu(o, width=W, height=H, subtexel_bits=8, sampler="warp")  # WARP's 8-bit bilinear weights
    frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
    tw = emu.run(frame.astype(np.float64), fp16_out=True)
    S = emu.sub
    # the gain pass's two channels, low-passed together: the device's (gain, ceiling B_est) against the twin's
    gf = np.fromfile(d / "fald_gain_fine.rg32f", dtype=np.float32).reshape(emu.rows * S, emu.cols * S, 2)
    assert np.allclose(gf[..., 0], tw["gain"], rtol=0, atol=2e-5)
    assert np.allclose(gf[..., 1], tw["ceil_est"], rtol=0, atol=2e-5)
    # ... and the ceiling channel IS low-passed: smoother than the raw estimate it came from
    raw_e = (np.fromfile(d / "fald_best.f32", dtype=np.float32).reshape(emu.rows * S, emu.cols * S)
             / np.maximum(np.fromfile(d / "fald_flat_best.f32", dtype=np.float32).reshape(emu.rows * S, emu.cols * S), 1e-6))
    lap = lambda a: float(np.abs(np.diff(a, 2, axis=0)).mean() + np.abs(np.diff(a, 2, axis=1)).mean())   # noqa: E731
    assert lap(gf[..., 1]) < 0.5 * lap(raw_e)
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
    lit = frame.max(axis=-1) > 0
    ulp = np.spacing(np.abs(tw["out"][lit])).astype(np.float64)
    off = np.abs(out[lit].astype(np.float64) - tw["out"][lit].astype(np.float64)) / np.maximum(ulp, 1e-30)
    assert float(off.max()) <= 1.0 + 1e-9, float(off.max())
    # the device does NOT run the pre-C15 rule: against the per-pixel-ceiling twin the knee-bound pixels move by far more
    old = Emu(o, width=W, height=H, subtexel_bits=8, sampler="warp", c15=False).run(frame.astype(np.float64), fp16_out=True)["out"]
    nits = lambda a: emu.panel_nits(a.astype(np.float64))[1]           # noqa: E731
    assert float(np.abs(nits(out) - nits(old))[lit].max()) > 20.0
    # no lobes in what the device put out
    ratio = nits(out) / np.maximum(nits(frame), 1e-9)
    old_ratio = nits(old) / np.maximum(nits(frame), 1e-9)
    for cx, cy, dia, _ in CIRCLES:
        s_new, s_old = _spike(emu, ratio, cx, cy, dia / 2), _spike(emu, old_ratio, cx, cy, dia / 2)
        print(f"circle {dia:.0f} px: LED-point contrast device {s_new:+.2f} %  (pre-C15 twin {s_old:+.2f} %)")
        assert abs(s_new) < 1.5


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_inputs(Path(sys.argv[1]))
    print(f"wrote panel.bin + frame.rgba16f + d0..d6 in {sys.argv[1]}")
