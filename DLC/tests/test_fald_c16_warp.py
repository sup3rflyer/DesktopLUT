"""Opt-in: the count-threshold band's feather + neighbour guard (work guide C16) on a real D3D device — the C++ WARP case
(tests/test_fald.cpp "FALD temporal modes on WARP") run with the glow fill ON and a mean-rule panel file with a boost LUT,
its dumps replayed against the GPU-order twin (gpuemu.Emu): G4's per-zone record (fald_glow_band.f32: Pc, Pf, LIT flag,
k0) and neighbour bound (fald_glow_bandA.f32: A_0..A_7), G5's final k (fald_glow_k.f32) and report (fald_glow_guard.f32:
iterations, converged, worst-case pass, its zones), and the output frame. Three scenes (``scene.txt`` names it):
  windows  960 x 540, 12 x 12 zones: 200-nit windows on a 0.008-nit sky — band zones in both rounds, the guard iterating;
  stripes  3840 x 2160, 48 x 48 zones (the in-repo default fit): 200-nit stripes 2 zones tall every 5 zone rows from zone
           column 4 on a 0.002-nit sky, cap 0.02 nit — a chain of joins longer than FALD_GLOW_GUARD_ITER_MAX: the guard
           ends on its cap and runs the worst-case pass;
  colour   as windows, with a coloured pedestal (m = 0.66, 0.95, 2.37) under a reddish sky — the brightest channel changes
           as the fill grows, so the bound takes its chord at those kinks;
  slices   as windows on a 12 x 9 lattice: zones of 80 x 60 = 4800 px > FALD_ZONE_SLICE_PX, so G4 sweeps each zone in two
           slices and its combine variant folds the A partials (GlowBandPart, u3).

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_c16_warp.py <dir> [windows|stripes|colour]  # panel.bin + frame.rgba16f + scene.txt + d0..d6
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; $env:FALD_TEST_WARP_GLOW = "1"
    $env:FALD_TEST_WARP_GLOW_CAP_MNIT = "20"                           # stripes only (the others run the default 100)
    bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"
    python -m pytest tests/test_fald_c16_warp.py -n0
(test_fald_glowfill_warp.py replays the same dumps' zone fields.) Without the variable / the band dumps the test is skipped."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.glowfill import GUARD_ITER_MAX, GlowFillParams, NEIGHBOURS  # noqa: E402
from dlc.fald.gpuemu import BT2020_TO_BT709, Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
LUT = ((0.0, 1.17), (0.30, 1.10), (0.60, 1.0))                        # the S2 WARP evidence's panel (mean zone rule)
MEAN_LUT = ((0.0, 1.17), (0.20, 1.10), (0.35, 1.0))                   # test_fald_glowfill.py's
PED_COLOUR = (0.66, 0.95, 2.37)                                       # a strongly blue pedestal (luminance-neutral)
SCENES = ("windows", "stripes", "colour", "slices")


def _params(scene: str) -> FaldParams:
    if scene == "stripes":
        return replace(FaldParams(), boost_lut=MEAN_LUT, boost_rule="mean")          # 3840 x 2160, 48 x 48 zones
    kw = {"tmin_rgb": PED_COLOUR} if scene == "colour" else {}
    rows = 9 if scene == "slices" else 12
    return FaldParams(width=960, height=540, cols=12, rows=rows, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, tmin=1.5e-3,
                      boost_lut=LUT, boost_rule="mean", **kw)


def frame_half(scene: str = "windows") -> np.ndarray:
    """(H, W, 4) half: the scene's frame (scRGB; the sky's channels are given as BT.2020 as-if-white nits)."""
    p = _params(scene)
    W, H, cw, ch = p.width, p.height, p.width // p.cols, p.height // p.rows
    if scene == "stripes":
        nits = np.full((H, W, 3), 0.002)
        for zy in range(0, p.rows, 5):
            nits[zy * ch: min(zy + 2, p.rows) * ch, 4 * cw:] = 200.0
    else:
        nits = np.empty((H, W, 3))
        nits[:] = np.array((0.008, 0.004, 0.002) if scene == "colour" else (0.008, 0.008, 0.008))
        for zy in ((1, 4, 7) if scene == "slices" else (2, 6, 10)):
            for zx in (2, 6, 10):
                cy, cx = int((zy + 0.5) * ch), int((zx + 0.5) * cw)
                nits[cy - 10: cy + 10, cx - 20: cx + 20] = 200.0
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = ((nits / 80.0) @ BT2020_TO_BT709.T).astype(np.float16)   # a grey stays grey
    return out


def write_inputs(root: Path, scene: str = "windows") -> None:
    assert scene in SCENES, scene
    root.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(_params(scene)), root / "panel.bin")
    frame_half(scene).tofile(root / "frame.rgba16f")
    (root / "scene.txt").write_text(scene)
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
    scene = (root / "scene.txt").read_text().strip() if (root / "scene.txt").exists() else "windows"
    d = root / "d0"
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)   # noqa: E731
    assert int(g("glowfill")) == 1 and int(g("temporal_mode")) == 0 and int(g("starfield")) == 1
    m = re.search(r"^glowfill strength (\S+) reach (\S+) cap_nits (\S+) req_ceil (\S+) band (\d)", t, re.M)
    assert int(m.group(5)) == 1, "the band did not run (a mean-rule panel file with a boost LUT is needed)"
    gp = GlowFillParams(strength=float(m.group(1)), reach=int(m.group(2)), cap_nits=float(m.group(3)))
    rows, cols = int(g("rows")), int(g("cols"))
    emu = Emu(read_panel_file(root / "panel.bin"), width=int(g("width")), height=int(g("height")), subtexel_bits=8, sampler="warp")   # WARP's 8-bit bilinear weights
    assert emu.glow_band_active()
    frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(emu.H, emu.W, 4)[..., :3]
    sp = _starfield_params(t)
    tw = emu.run(frame.astype(np.float64), fp16_out=True, star=sp, glow=gp)
    b = tw["glow"]["band"]                                                # round 1's (what the dump holds)
    rec = np.fromfile(d / "fald_glow_band.f32", dtype=np.float32).reshape(rows, cols, 4)
    a = np.fromfile(d / "fald_glow_bandA.f32", dtype=np.float32).reshape(rows, cols, 8)
    k = np.fromfile(d / "fald_glow_k.f32", dtype=np.float32).reshape(rows, cols)
    assert (d / "fald_glow_guard.f32").exists(), "no fald_glow_guard.f32: dumps of a build before the guard's report"
    rep = np.fromfile(d / "fald_glow_guard.f32", dtype=np.float32)
    # G4: the record — the LIT flags and the band's zones exactly, Pc / Pf / k0 to float32 sum order and the fields'
    # device sampling; the bound A_d (8 directions, NEIGHBOURS order; exactly 0 toward a missing neighbour)
    assert np.array_equal(rec[..., 2] > 0.5, b["lit"])
    assert np.allclose(rec[..., 0], b["pc"], rtol=2e-4, atol=1e-9) and np.allclose(rec[..., 1], b["pf"], rtol=2e-4, atol=1e-9)
    assert np.array_equal(rec[..., 3] < 1.0, b["k0"] < 1.0) and np.allclose(rec[..., 3], b["k0"], rtol=2e-3, atol=1e-6)
    tA = np.moveaxis(b["A"], 0, -1)
    # (a zone with a tiny A carries the float32 cancellation of its few kink chords: ~1e-7 absolute = 2e-5 of the largest)
    assert np.allclose(a, tA, rtol=2e-3, atol=2e-5 * float(tA.max())), float(np.abs(a - tA).max())
    zy, zx = np.mgrid[0:rows, 0:cols]
    for dd, (i, j) in enumerate(NEIGHBOURS):
        outside = (zx + i < 0) | (zx + i >= cols) | (zy + j < 0) | (zy + j >= rows)
        assert not a[..., dd][outside].any(), (i, j)
    # G5: the guard's final k — the same zones banded (band0 + the guard's), the same k — and its report
    assert np.array_equal(k < 1.0, b["k"] < 1.0) and np.allclose(k, b["k"], rtol=2e-3, atol=1e-6)
    assert rep.tolist() == [b["iterations"], float(b["converged"]), float(b["worst_case"]), float(b["worst_case_added"].sum())], rep
    line = re.search(r"^glowfill_guard iterations (\d+) converged (\d) worst_case (\d) worst_case_zones (\d+)", t, re.M)
    assert [int(x) for x in line.groups()] == [int(x) for x in rep]                  # the dump text reports the same
    print(f"[{scene}] band: {int(b['band0'].sum())} zones by the prediction + {int(b['guard_added'].sum())} by the guard "
          f"({b['iterations']} Jacobi iterations, converged {b['converged']}, worst-case pass +{int(b['worst_case_added'].sum())}); "
          f"A max {float(a.max()):.4g}, max |A dev - twin| {float(np.abs(a - tA).max()):.3g}; max |k dev - twin| {float(np.abs(k - b['k']).max()):.3g}")
    if scene == "windows":
        assert b["band0"].sum() >= 4 and b["guard_added"].sum() >= 1 and b["iterations"] >= 2 and b["converged"]
    elif scene == "slices":                                              # the partials path: two slices per zone
        from dlc.fald.gpuemu import ZONE_SLICE_PX
        assert emu.cw * emu.ch > ZONE_SLICE_PX and b["band0"].sum() >= 4 and float(a.max()) > 0.0
    elif scene == "stripes":                                             # the device ended on the cap: the worst-case pass ran
        assert rep[0] == GUARD_ITER_MAX and rep[1] == 0.0 and rep[2] == 1.0 and rep[3] > 0
    else:                                                                # the kinks: A above the plain chord's on the device
        saved = Emu.ped_m32                                              # (a white m: q = the plain chord (F(1) - F(0)) / (1 - s0))
        try:
            Emu.ped_m32 = lambda self: np.ones(3, dtype=np.float32)
            a_chord = np.moveaxis(emu.run(frame.astype(np.float64), fp16_out=True, star=sp, glow=gp)["glow"]["band"]["A"], 0, -1)
        finally:
            Emu.ped_m32 = saved
        up = a > a_chord * 1.01 + 1e-12
        print(f"[colour] A above the plain chord's on {int(up.any(axis=-1).sum())} of {rows * cols} zones, up to x{float(np.max(a / np.maximum(a_chord, 1e-30))):.2f}")
        assert up.any(axis=-1).sum() > rows * cols // 4 and b["guard_added"].sum() >= 1
    # the output, EVERY pixel: within one FP16 step of the twin; the filled pixels near a band zone (the feather) too
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(emu.H, emu.W, 4)[..., :3]
    filled = tw["glow"]["add"] > 0.0
    feathered = filled & (emu.band_scale_px(b["k"]) < 1.0)
    assert feathered.sum() > 1000
    off_all, off_feather = _ulp_off(out, tw["out"]), _ulp_off(out[feathered], tw["out"][feathered])
    same = (out == tw["out"]).all(axis=-1)
    print(f"[{scene}] output: max {off_all:.2f} FP16 steps off (feathered filled pixels {off_feather:.2f}); bit-equal "
          f"{100 * float(same[filled].mean()):.2f} % of the filled, {100 * float(same[feathered].mean()):.2f} % of the {int(feathered.sum())} feathered")
    assert off_all <= 1.0 + 1e-9 and off_feather <= 1.0 + 1e-9
    # bit-equality FRACTIONS only where the fill decides the value: an unfilled sky pixel is the scRGB -> BT.2020 -> scRGB
    # round trip of an exact FP16 grey, which the float32 matrix products of device and twin put on either side of the
    # FP16 truncation edge (a coin flip unrelated to the fill; the one-step bound above holds there too)
    # (a coloured fill goes through the BT.2020 -> scRGB matrix with three different channels: more such coin flips)
    frac = 0.95 if scene == "colour" else 0.97
    assert float(same[feathered].mean()) > frac and float(same[filled].mean()) > frac
    # ... and the device does NOT run the band before C16 (the pixel's own zone's k, no guard): against that twin the
    # feathered pixels move by far more than a step
    old = emu.run(frame.astype(np.float64), fp16_out=True, star=sp, glow=replace(gp, band_feather=False))["out"]
    off_old = _ulp_off(out[feathered], old[feathered])
    print(f"[{scene}] against the pre-C16 twin: max {off_old:.0f} FP16 steps off on the feathered pixels")
    assert off_old > 50.0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] not in SCENES):
        sys.exit(__doc__)
    write_inputs(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) == 3 else "windows")
    print(f"wrote panel.bin + frame.rgba16f + scene.txt + d0..d6 in {sys.argv[1]}")
