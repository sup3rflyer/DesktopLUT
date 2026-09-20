"""Opt-in: replay the GPU dumps of the C++ WARP case (tests/test_fald.cpp "FALD temporal modes on WARP", temporal mode 3
"panel clock", work guide C13) against the float64 REFERENCE law (dlc/fald/paneltime.py PanelClock) — not the float32
twin — and check the dumped k against the dumped run times. This is the check that sees SEQUENCING bugs on a real D3D
device: the next target taken from the wrong round, the clock pass run twice per frame, k not following the time.

Run (PowerShell; the directories d0 .. d6 must exist, panel.bin = `python -m dlc.fald.export`-style file, PQ):
    $env:FALD_TEST_WARP_DIR = "<dir>"; bin\\Test\\DesktopLUT.Tests.exe -tc="*WARP*"; python -m pytest tests/test_fald_paneltime_warp.py -n0
    ($env:FALD_TEST_WARP_PERIOD_US = "150000" makes runs share a refresh: k = 0; the default period gives one k > 64.)
Without the variable (or without mode-3 dumps in it) the test is skipped."""
from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import pytest

from dlc.fald.paneltime import MAX_REFRESHES, PanelClock, PanelTimeLaw, refresh_index

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
_RUNS = 7


def _meta(d: Path) -> dict:
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)
    return {"mode": int(g("temporal_mode")), "seed": int(g("clock_seed")), "time_ms": float(g("clock_time_ms")),
            "period_ms": float(g("clock_refresh_ms")), "n": int(g("clock_refresh_index")), "k": int(g("clock_elapsed_refreshes")),
            "closure": float(g("clock_closure")), "parity": int(g("clock_parity")), "cols": int(g("cols")), "rows": int(g("rows"))}


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "d0" / "fald_dump.txt").exists(), reason="FALD_TEST_WARP_DIR with WARP dumps not given")
def test_warp_dumps_follow_the_reference_law_and_the_run_times():
    root = Path(_DIR)
    metas = [_meta(root / f"d{i}") for i in range(_RUNS)]
    if metas[0]["mode"] != 3:
        pytest.skip("the dumps are not temporal mode 3")
    rows, cols = metas[0]["rows"], metas[0]["cols"]
    f32 = lambda p: np.fromfile(p, dtype=np.float32).reshape(rows, cols)
    weights = {-1: (0.5, 0.5), 0: (1.0, 0.0), 1: (0.0, 1.0)}[metas[0]["parity"]]
    files = {"true": "fald_drive_filt.f32", "est": "fald_clock_est.f32", "s0": "fald_clock_s0.f32", "s1": "fald_clock_s1.f32",
             "dprev": "fald_clock_dprev.f32"}
    clocks, pending, prev_n, prev_maps, ks = None, None, 0, None, []
    assert metas[0]["seed"] == 1, "run 0 must seed the clocks"
    for i, m in enumerate(metas):
        d = root / f"d{i}"
        drive = f32(d / "fald_drive.f32")                                  # the drive texture after ROUND 1
        # the index is the run's absolute time on the refresh grid, k its difference to the previous run's
        assert m["n"] == refresh_index(m["time_ms"], m["period_ms"]), (i, m)
        if m["seed"]:
            assert m["n"] == 0 and m["k"] == 0 and not (d / files["s0"]).exists(), (i, m)
            law = PanelTimeLaw(closure=m["closure"])
            clocks, pending, prev_n, prev_maps = [PanelClock(law, 0), PanelClock(law, 1)], drive.astype(np.float64), 0, None
            continue
        assert m["k"] == m["n"] - prev_n and m["n"] >= 1, (i, m, prev_n)
        maps = {name: f32(d / fn) for name, fn in files.items()}
        # the target the pass read = the PREVIOUS run's round-1 drives (bit for bit: a CopyResource)
        assert np.array_equal(maps["dprev"].astype(np.float64), pending), f"run {i}: d_prev is not the previous run's round-1 drive map"
        if m["k"] == 0:
            # a second run inside one refresh: nothing advanced, the previous run's maps were read again
            assert prev_maps is not None
            for name in ("true", "est", "s0", "s1"):
                assert np.array_equal(maps[name], prev_maps[name]), (i, name)
        else:
            for c in clocks:
                c.step(pending, refreshes=m["k"])                          # the previous frame stayed k refreshes
            assert clocks[0].n == m["n"]
            pk = [c.peek() for c in clocks]
            ref = {"s0": pk[0][0], "s1": pk[1][0], "true": weights[0] * pk[0][0] + weights[1] * pk[1][0],
                   "est": weights[0] * pk[0][1] + weights[1] * pk[1][1]}
            for name, want in ref.items():
                err = float(np.max(np.abs(maps[name].astype(np.float64) - want)))
                assert err <= 2e-6, f"run {i} (k {m['k']}): {name} is {err:.3e} from the reference law"
            if m["k"] > MAX_REFRESHES:                                     # a long pause: settled on the previous frame, no reset
                assert np.allclose(maps["true"], pending, atol=2e-6) and np.allclose(maps["est"], pending, atol=2e-6)
            ks.append(m["k"])
        pending, prev_n, prev_maps = drive.astype(np.float64), m["n"], maps
    assert ks, "no run advanced the clocks"
    print("k per run:", [m["k"] for m in metas], "seeds:", [m["seed"] for m in metas])
