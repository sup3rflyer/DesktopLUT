"""Opt-in: the always-on core round 2 — PROTOTYPE (shared/fald_core2.h) on a real D3D device: the C++ WARP case
(tests/test_fald_core2.cpp "FALD core round 2 on WARP") runs the overlay's layer with the core ON over a sequence of
frames, and its dumps are replayed against the GPU-order twin dlc.fald.core2emu (WARP's 8-bit bilinear weights).

The frames (960 x 540, a 12 x 12 lattice of 80 x 45-px zones): a sigma-15 1000-nit highlight moving 12 px / frame over
black (runs 0-5: the zone IIR steps, no cut), then a CUT to sigma-30 glows on a 2-nit floor (runs 6-7: the cut resets the
IIR), then the highlight again (run 8: a cut back). Every run advances the IIR by k = 1 refresh (the case's 1-s period).

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_core2_warp.py <dir>                          # panel.bin + f00 .. f08.rgba16f
    $env:FALD_TEST_CORE2_DIR = "<dir>"; bin\\Test\\DesktopLUT.Tests.exe -tc="*core round 2*"
    $env:FALD_TEST_CORE2_DIR = "<dir>"; python -m pytest tests/test_fald_core2_warp.py -n0
Without the variable / the dumps the test is skipped."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dlc.fald.core2emu import CORE_KC2C, CORE_KC3, CORE_KC5, CoreEmu, CoreState, iir_factor  # noqa: E402
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_CORE2_DIR", "")
_MODE = {"2": 2, "4": 4}.get(os.environ.get("FALD_TEST_CORE2_MODE", "1"), 1)   # 1 = KC2c (round 2), 2 = KC3 (round 3), 4 = KC5 (round 5)
W, H = 960, 540
N_RUNS = 9


def _params() -> FaldParams:
    return FaldParams(width=W, height=H, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, tmin=1.5e-3)


def frame_nits(run: int) -> np.ndarray:
    yy, xx = np.mgrid[0:H, 0:W] + 0.5
    if run in (6, 7):                                                  # the cut: glows on a 2-nit floor
        n = np.full((H, W), 2.0)
        for cx, cy, pk in ((260.0, 200.0, 800.0), (650.0, 330.0, 1200.0)):
            n = n + (pk - 2.0) * np.exp(-0.5 * ((xx - cx) ** 2 + (yy - cy) ** 2) / 30.0 ** 2)
        return n
    cx = 300.0 + 12.0 * (run if run < 6 else 6)
    return 1000.0 * np.exp(-0.5 * ((xx - cx) ** 2 + (yy - 270.0) ** 2) / 15.0 ** 2)


def frame_half(run: int) -> np.ndarray:
    out = np.ones((H, W, 4), dtype=np.float16)
    out[:, :, :3] = (np.minimum(frame_nits(run), 1600.0) / 80.0).astype(np.float16)[:, :, None]   # grey, PQ transfer
    return out


def write_inputs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    export_panel_params(FaldModel(_params()), root / "panel.bin")
    for r in range(N_RUNS):
        frame_half(r).tofile(root / f"f{r:02d}.rgba16f")


def _meta(d: Path) -> dict:
    t = (d / "fald_dump.txt").read_text()
    return {k: v for k, v in re.findall(r"^(core2\w*) (\S+)", t, re.M)}


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "d00" / "fald_core2_zone.f32").exists(),
                    reason="FALD_TEST_CORE2_DIR with core round 2 WARP dumps not given")
def test_warp_core2_fields_and_output_are_the_twin():
    root = Path(_DIR)
    o = read_panel_file(root / "panel.bin")
    emu = CoreEmu(o, flags={1: CORE_KC2C, 2: CORE_KC3, 4: CORE_KC5}[_MODE], width=W, height=H, subtexel_bits=8, sampler="warp")
    st = CoreState()
    rows, cols = emu.rows, emu.cols
    worst = {}
    for run in range(N_RUNS):
        d = root / f"d{run:02d}"
        if not d.exists():
            break
        frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
        m = _meta(d)
        assert m["core2"] == "1" and m["core2_flags"] == {1: "3", 2: "11", 4: "201"}[_MODE]
        assert abs(float(m["core2_a"]) - iir_factor(1)) < 1e-6
        host_reset = m["core2_reset"] == "1"     # the host's reset (no valid state; a resume after a > 250-ms gap — WARP is
        if run == 0 or _MODE >= 2:               # slow enough for that on some machines): the twin follows it; rounds 3 / 5
            assert host_reset                    # (no zone IIR) reset on every run
        if host_reset:
            st.invalidate()
        tw = emu.run(frame.astype(np.float64), fp16_out=True, core=st, refreshes=1)
        c = tw["core"]
        if not host_reset and _MODE == 1:        # the IN-SHADER cut test: the two cuts fire, nothing else does
            assert c["cut"] == (run in (6, 8)), (run, c["cut"])
        stat = np.fromfile(d / "fald_core2_stat.f32", dtype=np.float32).reshape(rows, cols, 4)
        assert np.allclose(stat[..., 0], c["P"], rtol=1e-5, atol=1e-6)
        assert np.allclose(stat[..., 1], c["M"], rtol=1e-4, atol=1e-6)
        assert np.allclose(stat[..., 2], c["lnG"], rtol=0, atol=2e-5)
        zone = np.fromfile(d / "fald_core2_zone.f32", dtype=np.float32).reshape(rows, cols, 4)
        assert np.allclose(zone[..., 0], c["alpha_z"], rtol=0, atol=2e-5), (run, float(np.abs(zone[..., 0] - c["alpha_z"]).max()))
        assert np.allclose(zone[..., 1], c["k"], rtol=0, atol=2e-5)
        assert np.allclose(zone[..., 2], np.log(np.maximum(c["P"], 1e-6)), rtol=0, atol=2e-5)
        if _MODE >= 2:                           # the fine grid: node peaks (uint bits of the float) and round 1's D / A
            S = emu.sub
            pk = np.fromfile(d / "fald_core3_node_peak.u32", dtype=np.uint32).view(np.float32).reshape(rows * S, cols * S)
            assert np.allclose(pk, emu.node_peaks(tw["img"]), rtol=1e-6, atol=1e-6)
            nd = np.fromfile(d / "fald_core3_node_d.f32", dtype=np.float32).reshape(rows * S, cols * S)
            if _MODE == 4:                       # round 5: the cap A (the shader's float32 scans add the step node by node:
                near = np.minimum(nd, c["D1"]) < 1.0     # absolute where a gain can meet it, relative beyond)
                assert near.any()
                assert np.allclose(nd[~near], c["D1"][~near], rtol=2e-5, atol=0), (run, "far")
                dD = float(np.abs(nd[near] - c["D1"][near]).max())
                worst.setdefault("capped_nodes", []).append(int((c["D1"] < 0.05).sum()))
            else:
                dD = float(np.abs(nd - c["D1"]).max())
        else:
            field = np.fromfile(d / "fald_core2_field.f32", dtype=np.float32).reshape(rows, cols, 2)
            dD = float(np.abs(field[..., 1] - c["D1"]).max())
        assert dD < 1e-5, (run, dD)              # ln units: float32 sums + the IIR across runs
        out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
        lit = frame.max(axis=-1) > 0
        ulp = np.spacing(np.abs(tw["out"][lit])).astype(np.float64)
        off = np.abs(out[lit].astype(np.float64) - tw["out"][lit].astype(np.float64)) / np.maximum(ulp, 1e-30)
        worst[run] = (float(off.max()), float((off > 1.0 + 1e-9).mean()), dD, bool(c["reset"]), bool(c["cut"]))
        assert float(off.max()) <= 1.0 + 1e-9, (run, worst[run])       # within one FP16 step everywhere (as C15's gate)
    capped = worst.pop("capped_nodes", None)
    print({r: tuple(round(x, 6) if isinstance(x, float) else x for x in v) for r, v in worst.items()})
    assert len(worst) == N_RUNS
    if _MODE == 4:                               # the cap must bind on this sequence (the highlights reach the knee)
        print("capped nodes (A < 0.05)", capped)
        assert capped and max(capped) > 0


if __name__ == "__main__":
    write_inputs(Path(sys.argv[1]))
    print("wrote", sys.argv[1])
