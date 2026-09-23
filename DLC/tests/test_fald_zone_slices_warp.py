"""Opt-in: a FEW-ZONE lattice on a real D3D device — 8 x 6 zones at 960 x 540, 120 x 90 = 10 800 px per zone = three slices
of FALD_ZONE_SLICE_PX (work guide C14: every zone sweep writes slice partials and folds them in its combine pass) — with
starfield + glow fill + the count-threshold band on (a mean-rule panel file with a boost LUT), the C++ WARP case
(tests/test_fald.cpp "FALD temporal modes on WARP") replayed against the GPU-order twin (gpuemu.Emu). A second lattice,
4 x 4 zones (240 x 135 px = eight slices; zone textures 4 x 4, fine grid 32 x 32), makes WARP truncate on y as well.

It was built for a gap found in the C16 work (2026-09-22): device and twin up to 106 FP16 steps apart at the top zone row.
Neither the shader nor the twin's passes were wrong: the twin modelled the device's bilinear sampler as a hardware GPU does
it (the 8-bit sub-texel fraction rounded to nearest), and WARP TRUNCATES that fraction on x when the texture's width is a
power of two (and on y when both dimensions are; gpuemu.sampler_truncates) — this lattice's zone textures are 8 wide, its
fine grid 64 wide, the drive-curve LUT 1024. A glow deficit stepping 0 -> 0.18 nit between a lit corner zone and its dark
neighbour, sampled at a fraction of 0.054 (13.9 / 256: WARP 13, the rounding model 14), put the interpolated fill 3.6 % off.
With WARP's rule (Emu(sampler="warp")) every pixel is within one FP16 step and the drives and band scales are equal.

Write the inputs, run the case, replay (PowerShell; one directory per lattice, "8x6" by default):
    python tests/test_fald_zone_slices_warp.py <dir> [8x6|4x4]         # panel.bin + frame.rgba16f + d0..d6
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; $env:FALD_TEST_WARP_GLOW = "1"
    bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"
    python -m pytest tests/test_fald_zone_slices_warp.py -n0
Without the variable / a few-zone glow dump in it the test is skipped."""
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
from dlc.fald.glowfill import GlowFillParams  # noqa: E402
from dlc.fald.gpuemu import SAMPLER_WARP, ZONE_SLICE_PX, Emu, sampler_truncates  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
W, H = 960, 540
LATTICES = {"8x6": (8, 6), "4x4": (4, 4)}                             # (cols, rows); zones of 3 / 8 slices
LUT = ((0.0, 1.17), (0.30, 1.10), (0.60, 1.0))                        # the S2 WARP evidence's panel (mean zone rule)
SKY, LEVEL, WINDOW_PX = 0.002, 100.0, (40, 20)                        # as-if-white nits; windows centred in every third zone


def _params(cols: int, rows: int) -> FaldParams:
    return FaldParams(width=W, height=H, cols=cols, rows=rows, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, boost_lut=LUT, boost_rule="mean")


def frame_half(cols: int, rows: int) -> np.ndarray:
    cw, ch = W // cols, H // rows
    nits = np.full((H, W), SKY)
    for zy in range(rows):
        for zx in range(cols):
            if (zx + zy) % 3 == 0:                                    # lit zones at the frame corners / edges: steep deficits
                cy, cx = int((zy + 0.5) * ch), int((zx + 0.5) * cw)
                nits[cy - WINDOW_PX[1] // 2: cy + WINDOW_PX[1] // 2, cx - WINDOW_PX[0] // 2: cx + WINDOW_PX[0] // 2] = LEVEL
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = (nits / 80.0).astype(np.float16)[:, :, None]      # grey: as-if-white nits = scRGB x 80 (PQ transfer)
    return out


def write_inputs(root: Path, lattice: str = "8x6") -> None:
    root.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(_params(*LATTICES[lattice])), root / "panel.bin")
    frame_half(*LATTICES[lattice]).tofile(root / "frame.rgba16f")
    for i in range(7):
        (root / f"d{i}").mkdir(exist_ok=True)


def _steps(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """|a - b| per pixel in FP16 steps at b, the worst channel (a, b: float16 (H, W, 3))."""
    ulp = np.spacing(np.abs(b)).astype(np.float64)
    return (np.abs(a.astype(np.float64) - b.astype(np.float64)) / np.maximum(ulp, 1e-30)).max(axis=-1)


def _dumped():
    if not _DIR:
        return False
    t = Path(_DIR) / "d0" / "fald_dump.txt"
    if not (t.exists() and (Path(_DIR) / "d0" / "fald_glow_k.f32").exists()):
        return False
    lat = tuple(int(re.search(r"^" + k + r" (\d+)$", t.read_text(), re.M).group(1)) for k in ("cols", "rows"))
    return lat in LATTICES.values()


@pytest.mark.skipif(not _dumped(), reason="FALD_TEST_WARP_DIR with a few-zone (8 x 6 / 4 x 4) glow-band WARP dump not given")
def test_warp_few_zone_lattice_is_the_twin_with_warps_sampler():
    from test_fald_glowfill_warp import _starfield_params
    root = Path(_DIR)
    d = root / "d0"
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)   # noqa: E731
    rows, cols, w, h, sub = int(g("rows")), int(g("cols")), int(g("width")), int(g("height")), int(g("sub"))
    assert (cols, rows) in LATTICES.values() and (w, h) == (W, H)
    cw, ch = w // cols, h // rows
    assert (cw * ch + ZONE_SLICE_PX - 1) // ZONE_SLICE_PX >= 3, "the lattice no longer sweeps its zones in slices"
    assert int(g("glowfill")) == 1 and int(g("starfield")) == 1 and int(g("temporal_mode")) == 0
    m = re.search(r"^glowfill strength (\S+) reach (\S+) cap_nits (\S+) req_ceil (\S+) band (\d)", t, re.M)
    assert int(m.group(5)) == 1, "the band did not run (a mean-rule panel file with a boost LUT is needed)"
    gp = GlowFillParams(strength=float(m.group(1)), reach=int(m.group(2)), cap_nits=float(m.group(3)))
    o = read_panel_file(root / "panel.bin")
    frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(h, w, 4)[..., :3]
    assert np.array_equal(frame, frame_half(cols, rows)[..., :3]), "not this test's frame: write the inputs with this file's __main__"
    kw = dict(fp16_out=True, star=_starfield_params(t), glow=gp)
    warp = Emu(o, width=w, height=h, subtexel_bits=8, sampler="warp")
    tw = warp.run(frame.astype(np.float64), **kw)
    # the zone sweeps (statistic both rounds, S0, G4 — three / eight slices per zone, folded by the combine passes): the
    # round-1 drives equal (this scene's zone sums are exact in float32: a ramp would leave ~1e-4 from the sum order), the
    # boost counts / boosts equal, the band's k to float32 rounding
    drv = np.fromfile(d / "fald_drive.f32", dtype=np.float32).reshape(rows, cols)
    assert np.allclose(drv, tw["drive1"], rtol=1e-6, atol=0), float(np.abs(drv - tw["drive1"]).max())
    assert (int(g("active_zones_r0")), int(g("active_zones_r1"))) == (tw["zones0"], tw["zones1"])
    assert float(g("boost_r1")) == pytest.approx(tw["boost1"], rel=1e-6)
    k = np.fromfile(d / "fald_glow_k.f32", dtype=np.float32).reshape(rows, cols)
    kt = tw["glow"]["k"]
    assert np.array_equal(k < 1.0, kt < 1.0)
    if (cols, rows) == LATTICES["8x6"]:                                # (4 x 4 zones of 32 400 px never band at this scene)
        assert (k < 1.0).sum() >= 2, "the scene no longer puts zones in the band"
    assert np.allclose(k, kt, rtol=0, atol=1e-5), float(np.abs(k - kt).max())
    st = np.fromfile(d / "fald_star_stat.f32", dtype=np.float32).reshape(rows, cols, 4)
    bg = np.fromfile(d / "fald_star_bg.f32", dtype=np.float32).reshape(rows, cols, 4)
    s = tw["star"]["stat"]
    assert np.allclose(st[..., 0], s["peak"], rtol=1e-6) and np.array_equal(st[..., 1] > 0.5, s["spk"])
    assert np.array_equal(bg[..., 1].astype(int), s["arg"]) and np.allclose(bg[..., 2], s["total"], rtol=1e-5)
    # the output, EVERY pixel: within one FP16 step of the twin
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(h, w, 4)[..., :3]
    off = _steps(out, tw["out"])
    filled = tw["glow"]["add"] > 0.0
    print(f"WARP sampler model: max {off.max():.2f} FP16 steps, bit-equal {100 * float((off == 0).mean()):.2f} % of the frame, "
          f"{100 * float((off[filled] == 0).mean()):.2f} % of the {int(filled.sum())} filled px")
    assert float(off.max()) <= 1.0 + 1e-9
    # ... and the case exercises what it was built for: with the hardware (rounding) sampler model the top zone row is far off
    hw = Emu(o, width=w, height=h, subtexel_bits=8, sampler="hw").run(frame.astype(np.float64), **kw)["out"]
    top = float(_steps(out[:ch], hw[:ch]).max())
    print(f"hardware (rounding) sampler model: top zone row {top:.0f} FP16 steps off")
    assert top > 50.0
    if sampler_truncates(SAMPLER_WARP, cols, rows)[1]:
        # 4 x 4: WARP truncates on y too — a twin that truncates x only (y rounded like a hardware GPU) is off as well
        xonly = Emu(o, width=w, height=h, subtexel_bits=8, sampler="warp")
        hwy = Emu(o, width=w, height=h, subtexel_bits=8, sampler="hw")
        xonly.by, xonly.zy = hwy.by, hwy.zy
        yoff = float(_steps(out, xonly.run(frame.astype(np.float64), **kw)["out"]).max())
        print(f"x-only truncation (y rounded): {yoff:.0f} FP16 steps off")
        assert yoff > 10.0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] not in LATTICES):
        sys.exit(__doc__)
    write_inputs(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) == 3 else "8x6")
    print(f"wrote panel.bin + frame.rgba16f + d0..d6 in {sys.argv[1]}")
