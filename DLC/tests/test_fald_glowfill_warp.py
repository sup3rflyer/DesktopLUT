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
    emu = Emu(read_panel_file(root / "panel.bin"), width=W, height=H)
    assert float(m.group(4)) == pytest.approx(emu.glow_ceiling(), rel=1e-5)
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
    # the output: the filled pixels agree with the twin to the FP16 step, lit pixels are not touched by the fill
    out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
    nits = lambda o: emu.panel_nits(o.astype(np.float64)).max(axis=0)
    filled = tw["glow"]["add"] > 0.0
    assert filled.any()
    err = np.abs(nits(out) - nits(tw["out"]))[filled]
    assert float(err.max()) <= 2e-3 + 2e-3 * float(nits(tw["out"])[filled].max()), float(err.max())
    # (bit equality is not expected on filled pixels: the twin forms the fill in float64, the GPU in float32 — 20-40 % of
    # them land on the same half; the nits bound above is one FP16 step at these levels)
    lit = frame.max(axis=-1) > 0
    off = emu.run(frame.astype(np.float64), fp16_out=True)
    assert np.array_equal(tw["out"][lit], off["out"][lit]) and not (filled & lit).any()
