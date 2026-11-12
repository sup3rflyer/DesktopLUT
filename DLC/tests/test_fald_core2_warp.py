"""Opt-in: the always-on core KC5 (shared/fald_core2.h) on a real D3D device: the C++ WARP case (tests/test_fald_core2.cpp
"FALD core on WARP") runs the overlay's layer with the core ON over a sequence of frames, and its dumps are replayed
against the GPU-order twin dlc.fald.core2emu (WARP's 8-bit bilinear weights).

The frames (960 x 540, a 12 x 12 lattice of 80 x 45-px zones): a sigma-15 1000-nit highlight moving 12 px / frame over
black (runs 0-5), then a CUT to sigma-30 glows on a 2-nit floor (runs 6-7), then the highlight again (run 8). KC5 is
stateless: every run is a pure function of its frame (the zone IIR and its cut test went with the test mode KC2c,
2026-11-12).

Write the inputs, run the case, replay (PowerShell):
    python tests/test_fald_core2_warp.py <dir>                          # panel.bin + f00 .. f08.rgba16f
    $env:FALD_TEST_CORE2_DIR = "<dir>"; bin\\Test\\DesktopLUT.Tests.exe -tc="FALD core on WARP*"
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
from dlc.fald.core2emu import CORE_KC5, CoreEmu, CoreState  # noqa: E402
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

_DIR = os.environ.get("FALD_TEST_CORE2_DIR", "")
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
                    reason="FALD_TEST_CORE2_DIR with the core's WARP dumps not given")
def test_warp_core2_fields_and_output_are_the_twin():
    root = Path(_DIR)
    o = read_panel_file(root / "panel.bin")
    emu = CoreEmu(o, flags=CORE_KC5, width=W, height=H, subtexel_bits=8, sampler="warp")
    st = CoreState()
    rows, cols = emu.rows, emu.cols
    worst = {}
    for run in range(N_RUNS):
        d = root / f"d{run:02d}"
        if not d.exists():
            break
        frame = np.fromfile(d / "fald_frame.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
        m = _meta(d)
        assert m["core2"] == "1" and m["core2_flags"] == "201"
        tw = emu.run(frame.astype(np.float64), fp16_out=True, core=st, refreshes=1)
        c = tw["core"]
        stat = np.fromfile(d / "fald_core2_stat.f32", dtype=np.float32).reshape(rows, cols, 4)
        assert np.allclose(stat[..., 0], c["P"], rtol=1e-5, atol=1e-6)
        assert np.allclose(stat[..., 1], c["M"], rtol=1e-4, atol=1e-6)
        assert np.allclose(stat[..., 2], c["lnG"], rtol=0, atol=2e-5)
        zone = np.fromfile(d / "fald_core2_zone.f32", dtype=np.float32).reshape(rows, cols, 4)
        assert np.allclose(zone[..., 0], c["alpha_z"], rtol=0, atol=2e-5), (run, float(np.abs(zone[..., 0] - c["alpha_z"]).max()))
        assert np.allclose(zone[..., 1], c["k"], rtol=0, atol=2e-5)
        assert np.allclose(zone[..., 2], np.log(np.maximum(c["P"], 1e-6)), rtol=0, atol=2e-5)
        alpha = np.fromfile(d / "fald_core2_alpha_state.f32", dtype=np.float32).reshape(rows, cols)
        assert np.allclose(alpha, c["alpha_raw"], rtol=0, atol=2e-5)
        S = emu.sub                              # the fine grid: node peaks (uint bits of the float) and round 1's cap A
        pk = np.fromfile(d / "fald_core3_node_peak.u32", dtype=np.uint32).view(np.float32).reshape(rows * S, cols * S)
        assert np.allclose(pk, emu.node_peaks(tw["img"]), rtol=1e-6, atol=1e-6)
        nd = np.fromfile(d / "fald_core3_node_d.f32", dtype=np.float32).reshape(rows * S, cols * S)
        near = np.minimum(nd, c["A1"]) < 1.0     # the shader's float32 scans add the step node by node: absolute where a gain
        assert near.any()                        # can meet it, relative beyond
        assert np.allclose(nd[~near], c["A1"][~near], rtol=2e-5, atol=0), (run, "far")
        dD = float(np.abs(nd[near] - c["A1"][near]).max())
        worst.setdefault("capped_nodes", []).append(int((c["A1"] < 0.05).sum()))
        assert dD < 1e-5, (run, dD)              # ln units: float32 sums
        out = np.fromfile(d / "fald_out.rgba16f", dtype=np.float16).reshape(H, W, 4)[..., :3]
        lit = frame.max(axis=-1) > 0
        ulp = np.spacing(np.abs(tw["out"][lit])).astype(np.float64)
        off = np.abs(out[lit].astype(np.float64) - tw["out"][lit].astype(np.float64)) / np.maximum(ulp, 1e-30)
        worst[run] = (float(off.max()), float((off > 1.0 + 1e-9).mean()), dD)
        assert float(off.max()) <= 1.0 + 1e-9, (run, worst[run])       # within one FP16 step everywhere (as C15's gate)
    capped = worst.pop("capped_nodes", None)
    print({r: tuple(round(x, 6) if isinstance(x, float) else x for x in v) for r, v in worst.items()})
    assert len(worst) == N_RUNS
    print("capped nodes (A < 0.05)", capped)              # the cap must bind on this sequence (the highlights reach the knee)
    assert capped and max(capped) > 0


if __name__ == "__main__":
    write_inputs(Path(sys.argv[1]))
    print("wrote", sys.argv[1])
