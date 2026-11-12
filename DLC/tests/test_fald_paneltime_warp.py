"""Opt-in: replay the GPU dumps of the C++ WARP case (tests/test_fald.cpp "FALD temporal modes on WARP", temporal mode 3
"panel clock", work guide C13) against the float64 REFERENCE law (dlc/fald/paneltime.py PanelClock) — not the float32
twin —, the backlight fields the kernels CONSUMED against the dumped maps, and the dumped k against the dumped run times
(RefreshGrid, the twin of the C++ phase-locked refresh grid). This is the check that sees SEQUENCING bugs on a real D3D
device: the next target taken from the wrong round or not replaced on k = 0, the clock pass run twice per frame, the
maps not bound on a k = 0 run, k not following the time. The C++ case sends a DIFFERENT frame every run for that reason.

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

from dlc.fald.paneltime import MAX_REFRESHES, PanelClock, PanelTimeLaw, RefreshGrid, robust_transient

_DIR = os.environ.get("FALD_TEST_WARP_DIR", "")
_RUNS = 7


def _meta(d: Path) -> dict:
    t = (d / "fald_dump.txt").read_text()
    g = lambda key: re.search(r"^" + key + r" (\S+)", t, re.M).group(1)
    return {"mode": int(g("temporal_mode")), "seed": int(g("clock_seed")), "time_ms": float(g("clock_time_ms")),
            "grid_ms": float(g("clock_grid_ms")), "period_ms": float(g("clock_refresh_ms")), "n": int(g("clock_refresh_index")),
            "k": int(g("clock_elapsed_refreshes")), "closure": float(g("clock_closure")), "parity": int(g("clock_parity")),
            "cols": int(g("cols")), "rows": int(g("rows")), "sub": int(g("sub")), "width": int(g("width")), "height": int(g("height")),
            "boost_r1": float(g("boost_r1"))}


@pytest.mark.skipif(not _DIR or not (Path(_DIR) / "d0" / "fald_dump.txt").exists(), reason="FALD_TEST_WARP_DIR with WARP dumps not given")
def test_warp_dumps_follow_the_reference_law_and_the_run_times():
    from dlc.fald.gpuemu import Emu
    from dlc.fald.panelfile import read_panel_file
    root = Path(_DIR)
    metas = [_meta(root / f"d{i}") for i in range(_RUNS)]
    if metas[0]["mode"] != 3:
        pytest.skip("the dumps are not temporal mode 3")
    rows, cols, sub = metas[0]["rows"], metas[0]["cols"], metas[0]["sub"]
    f32 = lambda p: np.fromfile(p, dtype=np.float32).reshape(rows, cols)
    fine = lambda p: np.fromfile(p, dtype=np.float32).reshape(rows * sub, cols * sub)
    panel = read_panel_file(root / "panel.bin")
    emu = Emu(panel, width=metas[0]["width"], height=metas[0]["height"])
    weights = {-1: (0.5, 0.5), -2: (0.5, 0.5), 0: (1.0, 0.0), 1: (0.0, 1.0)}[metas[0]["parity"]]
    robust = metas[0]["parity"] == -1           # unknown: the robust combination (2026-10-23); -2 = the plain mean
    files = {"true": "fald_drive_filt.f32", "est": "fald_clock_est.f32", "s0": "fald_clock_s0.f32", "s1": "fald_clock_s1.f32",
             "dprev": "fald_clock_dprev.f32"}
    grid = RefreshGrid(metas[0]["period_ms"])
    clocks, pending, prev_maps, ks, drives_seen = None, None, None, [], []
    assert metas[0]["seed"] == 1, "run 0 must seed the clocks"
    for i, m in enumerate(metas):
        d = root / f"d{i}"
        drive = f32(d / "fald_drive.f32")                                  # the drive texture after ROUND 1
        drives_seen.append(drive)
        # k = this run's time on the phase-locked refresh grid, replayed from the dumped run times alone
        seeded, k = grid.step(m["time_ms"])
        assert (int(seeded), k, grid.n) == (m["seed"], m["k"], m["n"]), (i, m, seeded, k, grid.n)
        assert abs(grid.grid_ms - m["grid_ms"]) < 1e-3, (i, grid.grid_ms, m["grid_ms"])
        # the fields the kernels CONSUMED this run (round 1): conv(true map) x boost, conv(est map) — on a seeding run the
        # maps are the frame's own round-1 drives
        if m["seed"]:
            want_true = want_est = drive
        else:
            want_true, want_est = f32(d / files["true"]), f32(d / files["est"])
        b_t, b_e = emu.fields(want_true, want_est, None if not panel.get("hasBoost") else np.float32(m["boost_r1"]))
        for name, want, got in (("B_true", b_t, fine(d / "fald_btrue.f32")), ("B_est", b_e, fine(d / "fald_best.f32"))):
            err = float(np.max(np.abs(got.astype(np.float64) - want.astype(np.float64)))) / max(float(np.max(np.abs(want))), 1e-9)
            assert err <= 1e-5, f"run {i} (k {m['k']}, seed {m['seed']}): the consumed {name} is not the field of the dumped map ({err:.2e})"
        if m["seed"]:
            assert m["n"] == 0 and m["k"] == 0 and not (d / files["s0"]).exists(), (i, m)
            law = PanelTimeLaw(closure=m["closure"])
            clocks, pending, prev_maps = [PanelClock(law, 0), PanelClock(law, 1)], drive.astype(np.float64), None
            continue
        maps = {name: f32(d / fn) for name, fn in files.items()}
        # the target the pass read = the PREVIOUS run's round-1 drives (bit for bit: a CopyResource), k = 0 runs included
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
            est = weights[0] * pk[0][1] + weights[1] * pk[1][1]
            if robust:
                true = est + robust_transient(pk)
            else:
                true = weights[0] * pk[0][0] + weights[1] * pk[1][0]
            ref = {"s0": pk[0][0], "s1": pk[1][0], "true": true, "est": est}
            for name, want in ref.items():
                err = float(np.max(np.abs(maps[name].astype(np.float64) - want)))
                assert err <= 2e-6, f"run {i} (k {m['k']}): {name} is {err:.3e} from the reference law"
            if m["k"] > MAX_REFRESHES:                                     # a long pause: settled on the previous frame, no reset
                assert np.allclose(maps["true"], pending, atol=2e-6) and np.allclose(maps["est"], pending, atol=2e-6)
            ks.append(m["k"])
        pending, prev_maps = drive.astype(np.float64), maps
    assert ks, "no run advanced the clocks"
    # the check only bites when consecutive frames differ (the C++ case moves its block every run)
    assert all(not np.array_equal(a, b) for a, b in zip(drives_seen[1:], drives_seen[2:])), "consecutive runs carried identical drives"
    print("k per run:", [m["k"] for m in metas], "seeds:", [m["seed"] for m in metas])


# ---------------------------------------------------------------------------------------------------------------------
# The clock AT REST on WARP against the float32 twin (dlc/fald/gpuemu.py GpuPanelDriveState), bit for bit.
# C++ tests/test_fald.cpp "FALD panel clock at rest on WARP" writes <dir>/on (the rule as shipped) and <dir>/off
# (g_faldClockRestOverride = 0): one fald_dump per run (rNNN) and schedule.txt ("run k newContent"). The twin is fed the
# dumped round-1 drives (fald_drive.f32 — the C++ commit's source) with the schedule's k and newContent, and must equal
# every dumped clock texture (fald_drive_filt / fald_clock_est / _s0 / _s1 / _dprev), the watch's words and per-zone
# levels, and the dump's clock_at_rest / clock_run_seq / clock_rest_flags, run for run: through coming to rest, at rest
# and the release.
#     $env:FALD_TEST_WARP_REST_DIR = "<dir>"; bin\\Test\\DesktopLUT.Tests.exe -tc="*clock at rest on WARP*"
#     python -m pytest tests/test_fald_paneltime_warp.py -n0
_REST_DIR = os.environ.get("FALD_TEST_WARP_REST_DIR", "")


def _rest_meta(d: Path) -> dict:
    t = (d / "fald_dump.txt").read_text()

    def g(key, conv=int, default=None):
        m = re.search(r"^" + key + r" (\S+)", t, re.M)
        return conv(m.group(1)) if m else default
    return {"seed": g("clock_seed"), "k": g("clock_elapsed_refreshes"), "n": g("clock_refresh_index"),
            "closure": g("clock_closure", float), "parity": g("clock_parity"), "cols": g("cols"), "rows": g("rows"),
            "temporal_mode": g("temporal_mode"), "run_seq": g("clock_run_seq"), "flags": g("clock_rest_flags"),
            "at_rest": g("clock_at_rest")}


def _bits(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float32).view(np.uint32)


@pytest.mark.skipif(not _REST_DIR or not (Path(_REST_DIR) / "on" / "schedule.txt").exists(),
                    reason="FALD_TEST_WARP_REST_DIR with the at-rest WARP dumps not given")
@pytest.mark.parametrize("variant", ["on", "off"])
def test_warp_rest_dumps_equal_the_twin(variant):
    from dlc.fald.gpuemu import GpuPanelDriveState
    root = Path(_REST_DIR) / variant
    sched = [tuple(int(v) for v in line.split()) for line in (root / "schedule.txt").read_text().splitlines() if line.strip()]
    assert [s[0] for s in sched] == list(range(len(sched)))
    m0 = _rest_meta(root / "r000")
    assert m0["temporal_mode"] == 3 and m0["seed"] == 1
    rows, cols = m0["rows"], m0["cols"]
    zone = lambda p: np.fromfile(p, dtype=np.float32).reshape(rows, cols)
    g = GpuPanelDriveState(m0["closure"], m0["parity"], rest=(variant == "on"))
    verdicts = []                                   # per run: 1 / 0 = the clock run of this run rested / did not, -1 = none
    for i, k, content in sched:
        d = root / f"r{i:03d}"
        m = _rest_meta(d)
        clock_run = g.s is not None and k >= 1      # (before the advance: a valid state and k >= 1 = a clock run)
        g.advance(k)
        assert int(g.s is None) == m["seed"], f"run {i}: the twin's seed verdict differs from the dump's"
        assert m["k"] == (0 if m["seed"] else k) and (m["seed"] or m["n"] == g.n), (i, m, g.n)
        drive = zone(d / "fald_drive.f32")
        if not m["seed"]:
            for name, want in (("fald_drive_filt.f32", g.true), ("fald_clock_est.f32", g.est), ("fald_clock_s0.f32", g.s[0]),
                               ("fald_clock_s1.f32", g.s[1]), ("fald_clock_dprev.f32", g.d_prev)):
                got = zone(d / name)
                bad = int(np.count_nonzero(_bits(got) != _bits(want)))
                assert bad == 0, (f"{variant} run {i} (k {k}, content {content}, twin at rest {g.at_rest}): {name} differs from the"
                                  f" twin in {bad} zones, max {float(np.max(np.abs(got.astype(np.float64) - want))):.3e}")
            words = np.fromfile(d / "fald_clock_rest.u32", dtype=np.uint32)
            assert words.tolist() == g.words.tolist(), (variant, i, words.tolist(), g.words.tolist())
            levels = np.fromfile(d / "fald_clock_levels.f32", dtype=np.float32).reshape(rows, cols, 4)
            assert np.array_equal(_bits(levels), _bits(g.levels)), (variant, i, "the watch's per-zone levels")
            assert (m["run_seq"], m["flags"], m["at_rest"]) == (g.run_seq, g.rest_flags, int(g.at_rest)), (variant, i, m)
        verdicts.append(int(g.at_rest) if clock_run else -1)
        g.commit(drive, new_content=bool(content))
    rest_runs = [i for i, v in enumerate(verdicts) if v == 1]
    print(f"{variant}: {len(rest_runs)} clock runs at rest of {sum(v >= 0 for v in verdicts)}; first at run "
          f"{rest_runs[0] if rest_runs else None}; verdicts {''.join('.' if v < 0 else str(v) for v in verdicts)}")
    if variant == "off":
        assert not rest_runs and g.rest_runs == 0
        return
    # the schedule (tests/test_fald.cpp): content at runs 0 (the seed), 41 (the same pixels), 52 (a block raised), 98 (back)
    contents = [i for i, _, c in sched if c]
    assert len(contents) >= 3
    first_edit = contents[1]
    assert rest_runs and rest_runs[0] < first_edit, "the real layer did not come to rest on the first still stretch"
    assert all(v == 1 for v in verdicts[rest_runs[0]:first_edit]), "a still desktop, and the clock left its rest"
    for c in contents[2:]:                          # drives moved: the next clock run aims at them and is released
        nxt = next(j for j in range(c + 1, len(sched)) if verdicts[j] >= 0)
        assert verdicts[nxt] == 0, f"the clock run after the content change at run {c} (run {nxt}) was at rest"
        assert any(v == 1 for v in verdicts[nxt:]), f"the clock never came to rest again after run {c}"
