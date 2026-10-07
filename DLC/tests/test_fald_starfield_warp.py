"""Opt-in: starfield balancing (work guide S1) on a real D3D device — the C++ WARP case (tests/test_fald.cpp "FALD temporal
modes on WARP", FALD_TEST_WARP_STAR = 1: starfield at the C++ defaults) replayed against the GPU-order twin (gpuemu.Emu,
sampler="warp"): the five star zone fields the passes S0 / S1 / S2 dumped and the output frame. A real device sees what the
text pins cannot: a pass bound to the wrong texture, a plan sampled at the wrong place, a flag read from the wrong zone.

The scene is a speck field (3 x 3 px specks of 150..1500 nits in ~70 % of the zones of a 16 x 9 lattice, on a 0.3-nit sky),
so zones hold specks, peaks above the local target are pulled and the speck pixels are balanced. (Until 2026-10-07 this
replay lived in the glow-fill WARP test; the glow fill was removed.)

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_starfield_warp.py <dir>                     # panel.bin + frame.rgba16f + d0..d6
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; $env:FALD_TEST_WARP_STAR = "1"
    bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"
    python -m pytest tests/test_fald_starfield_warp.py -n0
Without the variable (or without this test's starfield dump in it) the test is skipped."""
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
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402
from test_fald_zone_slices_warp import _starfield_params, _steps  # noqa: E402

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
W, H, COLS, ROWS = 960, 540, 16, 9
LUT = ((0.0, 1.17), (0.30, 1.10), (0.60, 1.0))                        # a boost LUT with the mean zone rule
SKY, SPECK_LO, SPECK_HI, SPECK_PX, SEED = 0.3, 150.0, 1500.0, 3, 7    # as-if-white nits


def _params() -> FaldParams:
    return FaldParams(width=W, height=H, cols=COLS, rows=ROWS, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, boost_lut=LUT, boost_rule="mean")


def frame_half() -> np.ndarray:
    """The speck field (RGBA half, the fald_frame dump layout): log-uniform levels, one speck in ~70 % of the zones."""
    rng = np.random.default_rng(SEED)
    cw, ch = W // COLS, H // ROWS
    nits = np.full((H, W), SKY)
    for zy in range(ROWS):
        for zx in range(COLS):
            if rng.random() < 0.7:
                lvl = float(np.exp(rng.uniform(np.log(SPECK_LO), np.log(SPECK_HI))))
                y = zy * ch + int(rng.integers(4, ch - 4 - SPECK_PX)); x = zx * cw + int(rng.integers(4, cw - 4 - SPECK_PX))
                nits[y:y + SPECK_PX, x:x + SPECK_PX] = lvl
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = (nits / 80.0).astype(np.float16)[:, :, None]      # grey: as-if-white nits = scRGB x 80 (PQ transfer)
    return out


def write_inputs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(_params()), root / "panel.bin")
    frame_half().tofile(root / "frame.rgba16f")
    for i in range(7):
        (root / f"d{i}").mkdir(exist_ok=True)


def _dumped() -> bool:
    if not _DIR:
        return False
    t = Path(_DIR) / "d0" / "fald_dump.txt"
    if not (t.exists() and (Path(_DIR) / "d0" / "fald_star_plan.f32").exists()):
        return False
    lat = tuple(int(re.search(r"^" + k + r" (\d+)$", t.read_text(), re.M).group(1)) for k in ("cols", "rows"))
    return lat == (COLS, ROWS)


@pytest.mark.skipif(not _dumped(), reason="FALD_TEST_WARP_DIR with this test's 16 x 9 starfield WARP dump not given")
def test_warp_starfield_dumps_are_the_twin():
    root = Path(_DIR)
    d = root / "d0"
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)   # noqa: E731
    assert int(g("starfield")) == 1 and int(g("temporal_mode")) == 0
    rows, cols, w, h = int(g("rows")), int(g("cols")), int(g("width")), int(g("height"))
    frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(h, w, 4)[..., :3]
    assert np.array_equal(frame, frame_half()[..., :3]), "not this test's frame: write the inputs with this file's __main__"
    emu = Emu(read_panel_file(root / "panel.bin"), width=w, height=h, subtexel_bits=8, sampler="warp")   # WARP's bilinear weights
    tw = emu.run(frame.astype(np.float64), fp16_out=True, star=_starfield_params(t))
    # the device's zone fields (fald_frame = the SOURCE; every later pass read Balance of it) against the twin's — the flags
    # and the brightest pixel exactly, the fields the pixels read to float32 sum order / the curve LUT's sub-texel weights
    # (near and w carry a drive)
    z4 = lambda n: np.fromfile(d / f"fald_star_{n}.f32", dtype=np.float32).reshape(rows, cols, 4)   # noqa: E731
    stat, sw, plan, plan2, sbg = z4("stat"), z4("w"), z4("plan"), z4("plan2"), z4("bg")
    st, pl = tw["star"]["stat"], tw["star"]["plan"]
    spk = stat[..., 1] > 0.5
    assert np.array_equal(spk, st["spk"]) and np.array_equal(sw[..., 3] > 0.5, spk) and np.array_equal(plan2[..., 2] > 0.5, spk)
    assert np.array_equal(sw[..., 2] > 0.5, pl["flank"]) and np.array_equal(sbg[..., 1], st["arg"])
    assert np.allclose(stat[..., 0], st["peak"], rtol=1e-6, atol=0)
    assert np.allclose(plan[..., 0], pl["w_field"], rtol=0, atol=1e-6) and np.allclose(plan2[..., 3], pl["w"], rtol=0, atol=1e-6)
    for i, key in ((1, "ln_t"), (2, "ln_g"), (3, "ln_pk")):                         # ln domain: absolute = relative
        assert np.allclose(plan[..., i], pl[key], rtol=0, atol=1e-5), key
    assert np.allclose(plan2[..., 0], pl["ln_b"], rtol=0, atol=1e-5) and np.allclose(plan2[..., 1], pl["near"], rtol=0, atol=5e-5)
    # the scene exercises the feature: speck zones, peaks pulled toward the local target, pixels balanced
    scaled = tw["star"]["scale"] != 1.0
    pulled = plan[..., 1] < plan[..., 3] - 1e-6
    assert spk.sum() >= 50 and pulled.sum() >= 20 and scaled.sum() >= 200, (int(spk.sum()), int(pulled.sum()), int(scaled.sum()))
    # the output, EVERY pixel — balanced or not — within one FP16 step of the twin
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(h, w, 4)[..., :3]
    off = _steps(out, tw["out"])
    print(f"starfield: {int(spk.sum())} speck zones, {int(pulled.sum())} pulled, {int(scaled.sum())} pixels balanced; "
          f"output max {off.max():.2f} FP16 steps (balanced {off[scaled].max():.2f}), bit-equal {100 * float((off == 0).mean()):.2f} %")
    assert float(off.max()) <= 1.0 + 1e-9 and float(off[scaled].max()) <= 1.0 + 1e-9


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_inputs(Path(sys.argv[1]))
    print(f"wrote panel.bin + frame.rgba16f + d0..d6 in {sys.argv[1]}")
