"""Opt-in: replay the GPU dump of the C++ WARP case (tests/test_fald.cpp "FALD temporal modes on WARP") run with the glow
fill ON (work guide S2) against the GPU-order twin (dlc/fald/gpuemu.py): the zone fields the glow passes wrote and the
output frame. A real D3D device sees what the text pins cannot: a pass bound to the wrong texture, the fill computed from
the wrong round's field, the deficit sampled at the wrong place.

Run (PowerShell; the directories d0 .. d6 must exist; panel.bin = a `python -m dlc.fald.export`-style PQ file; an optional
frame.rgba16f = the frame, e.g. a star lattice with a hole — the default frames hold no hole to fill):
    $env:FALD_TEST_WARP_DIR = "<dir>"; $env:FALD_TEST_WARP_MODE = "0"; $env:FALD_TEST_WARP_GLOW = "1"
    bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"; python -m pytest tests/test_fald_glowfill_warp.py -n0
    (FALD_TEST_WARP_GLOW_REACH / FALD_TEST_WARP_GLOW_CAP_MNIT select reach / cap; the dump reports them.)
results/fald_inside_2026-09-18/glowfill/warp_identity_and_twin.py drives the whole thing, including the byte-identity of
the option OFF against a build of the tree before S2. Without the variable (or without glow dumps in it) the test is
skipped."""
from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import pytest

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "d0" / "fald_glow_env.f32").exists(), reason="FALD_TEST_WARP_DIR with glow-fill WARP dumps not given")
def test_warp_glow_dumps_are_the_twin():
    from dlc.fald.glowfill import GlowFillParams
    from dlc.fald.gpuemu import Emu
    from dlc.fald.panelfile import read_panel_file
    root = Path(_DIR)
    d = root / "d0"
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)
    assert int(g("glowfill")) == 1 and int(g("temporal_mode")) == 0
    m = re.search(r"^glowfill strength (\S+) reach (\S+) cap_nits (\S+) req_ceil (\S+)", t, re.M)
    gp = GlowFillParams(strength=float(m.group(1)), reach=int(m.group(2)), cap_nits=float(m.group(3)))
    rows, cols, W, H = int(g("rows")), int(g("cols")), int(g("width")), int(g("height"))
    emu = Emu(read_panel_file(root / "panel.bin"), width=W, height=H, subtexel_bits=8)   # a device's 8-bit bilinear weights
    assert float(m.group(4)) == pytest.approx(emu.glow_ceiling(), rel=1e-5) and emu.glow_ceiling() <= 0.2
    frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
    tw = emu.run(frame.astype(np.float64), fp16_out=True, glow=gp)
    vz = np.fromfile(d / "fald_glow_vz.f32", dtype=np.float32).reshape(rows, cols)
    env = np.fromfile(d / "fald_glow_env.f32", dtype=np.float32).reshape(rows, cols, 4)
    # the zone fields from the DUMPED round-1 B_true field (what the passes read), not from the twin's own
    gz = emu.glow_zones(np.fromfile(d / "fald_btrue.f32", dtype=np.float32).reshape(rows * emu.sub, cols * emu.sub), gp)
    assert np.allclose(vz, gz["vz"], rtol=2e-6, atol=1e-12) and np.array_equal(env[..., 3], vz)
    assert np.allclose(env[..., 2], gz["cz"], rtol=2e-6, atol=1e-12)
    assert np.allclose(env[..., 0], gz["ez"], rtol=1e-5, atol=1e-10) and np.allclose(env[..., 1], gz["dz"], rtol=1e-3, atol=1e-8)
    assert env[..., 1].max() > 0.0, "the frame holds no hole: nothing was filled (give the case a frame.rgba16f)"
    # the count-threshold band (mean-rule files with a boost LUT): the dumped k is round 1's = the twin's
    band = int(re.search(r"^glowfill strength .* band (\d)", t, re.M).group(1))
    assert band == int(emu.glow_band_active()) and (d / "fald_glow_k.f32").exists() == bool(band)
    if band:
        k = np.fromfile(d / "fald_glow_k.f32", dtype=np.float32).reshape(rows, cols)
        assert np.array_equal(k < 1.0, tw["glow"]["k"] < 1.0) and np.allclose(k, tw["glow"]["k"], rtol=2e-3, atol=1e-6)
    # the output against the twin, EVERY pixel — filled or not, lit or dim (the rule is fill = max(0, want - shown): a dim
    # content pixel below its want IS filled; an earlier form of this test asserted that no source pixel > 0 ever is)
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
    nits = lambda o: emu.panel_nits(o.astype(np.float64)).max(axis=0)
    filled = tw["glow"]["add"] > 0.0
    assert filled.any()
    err = np.abs(nits(out) - nits(tw["out"]))
    assert float(err[filled].max()) <= 3e-4, float(err[filled].max())
    same = (out == tw["out"]).all(axis=-1)
    # THE scale-free gate, and the one that holds in every regime: no channel of a filled pixel is more than ONE last bit
    # from the twin. A real divergence in the glow path (the fill computed from another round's fields, an interpolated
    # instead of a nearest-zone k, another reduction order) moves the fill by far more than the FP16 step and lands here.
    ulp = np.spacing(np.abs(tw["out"][filled])).astype(np.float64)       # the FP16 step AT each value
    off_by = np.abs(out[filled].astype(np.float64) - tw["out"][filled].astype(np.float64)) / np.maximum(ulp, 1e-30)
    assert float(off_by.max()) <= 1.0 + 1e-9, float(off_by.max())
    # How MANY of those channels land on the neighbouring half is not scale-free: it follows the twin's residual drive
    # difference, and the drive comes from the curve LUT, which a device samples with 8-bit sub-texel weights while the
    # emulator interpolates it in float64. High on the curve (a star lattice at panel white: zone statistic 34-40 nit,
    # drive 0.14) that is 1.5e-5 of B_true = 0.02 FP16 ulp and ~1 % of the channels differ; just above the curve's low-end
    # knee (610-nit stars: statistic 12-13 nit, drive 0.02-0.03) it is 1.8e-4 = 0.31 ulp and ~16 % do. So the bit-equality
    # FRACTIONS are asserted only where the twin reproduces the device's drives; the ulp bound above carries the rest.
    drives_exact = float(np.max(np.abs(vz - tw["glow"]["vz"]))) <= 2e-5 * float(vz.max())
    if drives_exact:
        assert float(same[filled].mean()) > 0.97 and float(same.mean()) > 0.99, (float(same[filled].mean()), float(same.mean()))
    else:
        assert float(same.mean()) > 0.9, float(same.mean())
