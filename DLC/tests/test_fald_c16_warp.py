"""Opt-in: the count-threshold band's feather + neighbour guard (work guide C16) on a real D3D device — the C++ WARP case
(tests/test_fald.cpp "FALD temporal modes on WARP") run with the glow fill ON, a mean-rule panel file with a boost LUT and
200-nit windows on a 0.008-nit sky (band zones in both rounds, the guard iterating), its dumps replayed against the
GPU-order twin (gpuemu.Emu): G4's per-zone record (fald_glow_band.f32: Pc, Pf, LIT flag, k0) and neighbour bound
(fald_glow_bandA.f32: A_0..A_7), G5's final k (fald_glow_k.f32) and the output frame.

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_c16_warp.py <dir>                           # panel.bin + frame.rgba16f + d0..d6
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; $env:FALD_TEST_WARP_GLOW = "1"
    bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"
    python -m pytest tests/test_fald_c16_warp.py -n0
(test_fald_glowfill_warp.py replays the same dumps' zone fields.) Without the variable / the band dumps the test is skipped."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.glowfill import GlowFillParams, NEIGHBOURS  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
W, H = 960, 540
LUT = ((0.0, 1.17), (0.30, 1.10), (0.60, 1.0))                        # the S2 WARP evidence's panel (mean zone rule)
SKY, LEVEL = 0.008, 200.0                                             # as-if-white nits
WINDOW_ZONES, WINDOW_PX = (2, 6, 10), (40, 20)                        # a 3 x 3 grid of 40 x 20-px windows, zone centres


def _params() -> FaldParams:
    return FaldParams(width=W, height=H, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, tmin=1.5e-3,
                      boost_lut=LUT, boost_rule="mean")


def frame_half() -> np.ndarray:
    cw, ch = W // 12, H // 12
    nits = np.full((H, W), SKY)
    for zy in WINDOW_ZONES:
        for zx in WINDOW_ZONES:
            cy, cx = int((zy + 0.5) * ch), int((zx + 0.5) * cw)
            nits[cy - WINDOW_PX[1] // 2: cy + WINDOW_PX[1] // 2, cx - WINDOW_PX[0] // 2: cx + WINDOW_PX[0] // 2] = LEVEL
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = (nits / 80.0).astype(np.float16)[:, :, None]      # grey: as-if-white nits = scRGB x 80 (PQ transfer)
    return out


def write_inputs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(_params()), root / "panel.bin")
    frame_half().tofile(root / "frame.rgba16f")
    for i in range(7):
        (root / f"d{i}").mkdir(exist_ok=True)


def _ulp_off(a: np.ndarray, b: np.ndarray) -> float:
    """max |a - b| in FP16 steps at b (a, b: float16 arrays)."""
    ulp = np.spacing(np.abs(b)).astype(np.float64)
    return float((np.abs(a.astype(np.float64) - b.astype(np.float64)) / np.maximum(ulp, 1e-30)).max())


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "d0" / "fald_glow_bandA.f32").exists(),
                    reason="FALD_TEST_WARP_DIR with C16 band WARP dumps not given")
def test_warp_band_record_bound_guard_and_output_are_the_twin():
    from test_fald_glowfill_warp import _starfield_params
    root = Path(_DIR)
    d = root / "d0"
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)   # noqa: E731
    assert int(g("glowfill")) == 1 and int(g("temporal_mode")) == 0 and int(g("starfield")) == 1
    m = re.search(r"^glowfill strength (\S+) reach (\S+) cap_nits (\S+) req_ceil (\S+) band (\d)", t, re.M)
    assert int(m.group(5)) == 1, "the band did not run (a mean-rule panel file with a boost LUT is needed)"
    gp = GlowFillParams(strength=float(m.group(1)), reach=int(m.group(2)), cap_nits=float(m.group(3)))
    rows, cols = int(g("rows")), int(g("cols"))
    emu = Emu(read_panel_file(root / "panel.bin"), width=int(g("width")), height=int(g("height")), subtexel_bits=8)
    assert emu.glow_band_active()
    frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(emu.H, emu.W, 4)[..., :3]
    tw = emu.run(frame.astype(np.float64), fp16_out=True, star=_starfield_params(t), glow=gp)
    b = tw["glow"]["band"]                                                # round 1's (what the dump holds)
    rec = np.fromfile(d / "fald_glow_band.f32", dtype=np.float32).reshape(rows, cols, 4)
    a = np.fromfile(d / "fald_glow_bandA.f32", dtype=np.float32).reshape(rows, cols, 8)
    k = np.fromfile(d / "fald_glow_k.f32", dtype=np.float32).reshape(rows, cols)
    # G4: the record — the LIT flags and the band's zones exactly, Pc / Pf / k0 to float32 sum order and the fields'
    # device sampling; the bound A_d (8 directions, NEIGHBOURS order; exactly 0 toward a missing neighbour)
    assert np.array_equal(rec[..., 2] > 0.5, b["lit"])
    assert np.allclose(rec[..., 0], b["pc"], rtol=2e-4, atol=1e-9) and np.allclose(rec[..., 1], b["pf"], rtol=2e-4, atol=1e-9)
    assert np.array_equal(rec[..., 3] < 1.0, b["k0"] < 1.0) and np.allclose(rec[..., 3], b["k0"], rtol=2e-3, atol=1e-6)
    tA = np.moveaxis(b["A"], 0, -1)
    assert np.allclose(a, tA, rtol=2e-3, atol=1e-6 * float(tA.max())), float(np.abs(a - tA).max())
    zy, zx = np.mgrid[0:rows, 0:cols]
    for dd, (i, j) in enumerate(NEIGHBOURS):
        outside = (zx + i < 0) | (zx + i >= cols) | (zy + j < 0) | (zy + j >= rows)
        assert not a[..., dd][outside].any(), (i, j)
    # G5: the guard's final k — the same zones banded (band0 + the guard's), the same k
    assert np.array_equal(k < 1.0, b["k"] < 1.0) and np.allclose(k, b["k"], rtol=2e-3, atol=1e-6)
    assert b["band0"].sum() >= 4 and b["guard_added"].sum() >= 1 and b["iterations"] >= 2, "the scene no longer exercises the guard"
    print(f"band: {int(b['band0'].sum())} zones by the prediction + {int(b['guard_added'].sum())} by the guard "
          f"({b['iterations']} Jacobi iterations); A max {float(a.max()):.4g}, max |A dev - twin| {float(np.abs(a - tA).max()):.3g}; "
          f"max |k dev - twin| {float(np.abs(k - b['k']).max()):.3g}")
    # the output, EVERY pixel: within one FP16 step of the twin; the filled pixels near a band zone (the feather) too
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(emu.H, emu.W, 4)[..., :3]
    filled = tw["glow"]["add"] > 0.0
    feathered = filled & (emu.band_scale_px(b["k"]) < 1.0)
    assert feathered.sum() > 1000
    off_all, off_feather = _ulp_off(out, tw["out"]), _ulp_off(out[feathered], tw["out"][feathered])
    same = (out == tw["out"]).all(axis=-1)
    print(f"output: max {off_all:.2f} FP16 steps off (feathered filled pixels {off_feather:.2f}); bit-equal "
          f"{100 * float(same.mean()):.2f} % of all, {100 * float(same[feathered].mean()):.2f} % of the {int(feathered.sum())} feathered")
    assert off_all <= 1.0 + 1e-9 and off_feather <= 1.0 + 1e-9
    # bit-equality FRACTIONS only where the fill decides the value: an unfilled sky pixel is the scRGB -> BT.2020 -> scRGB
    # round trip of an exact FP16 grey, which the float32 matrix products of device and twin put on either side of the
    # FP16 truncation edge (a coin flip unrelated to the fill; the one-step bound above holds there too)
    assert float(same[feathered].mean()) > 0.97 and float(same[filled].mean()) > 0.97
    # ... and the device does NOT run the band before C16 (the pixel's own zone's k, no guard): against that twin the
    # feathered pixels move by far more than a step
    from dataclasses import replace
    old = emu.run(frame.astype(np.float64), fp16_out=True, star=_starfield_params(t), glow=replace(gp, band_feather=False))["out"]
    off_old = _ulp_off(out[feathered], old[feathered])
    print(f"against the pre-C16 twin: max {off_old:.0f} FP16 steps off on the feathered pixels")
    assert off_old > 50.0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_inputs(Path(sys.argv[1]))
    print(f"wrote panel.bin + frame.rgba16f + d0..d6 in {sys.argv[1]}")
