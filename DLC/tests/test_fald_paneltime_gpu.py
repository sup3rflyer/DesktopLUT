"""Temporal mode 3 "panel clock" (work guide C13): the GPU-order twin (dlc/fald/gpuemu.py GpuPanelDriveState — pass 1c
``g_faldPanelClockSource`` + the CPU-side blend factors of ``FaldPanelClockFactors``) against the reference
(dlc/fald/paneltime.py PanelDriveState), and the pins that hold the C++ / HLSL side to both."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from dlc.fald.gpuemu import GpuPanelDriveState, clock_factors32
from dlc.fald.paneltime import (CLOSURE_DEFAULT, CLOSURE_MAX, CLOSURE_MIN, MAX_REFRESHES, MODE_PANEL, SETTLE_MAX, PanelDriveState,
                                PanelTimeLaw, blend_factors, settle_refreshes)

_SRC = Path(__file__).resolve().parents[2] / "src"
_SHARED = _SRC.parent / "shared"                      # fald_temporal.{h,cpp}: the LED-lag constants + per-run bookkeeping
_HOOK = _SRC.parent / "dwm_hook" / "hook_fald.cpp"    # the DWM hook's passes, pass for pass the overlay's (src/fald.cpp)


def _pair(parity):
    return GpuPanelDriveState(0.72, -1 if parity is None else parity), PanelDriveState(PanelTimeLaw(closure=0.72), parity)


@pytest.mark.parametrize("parity", [None, 0, 1])
def test_twin_equals_the_reference_with_k_refreshes_per_frame(parity):
    """The reference holds a frame for h refreshes (commit(d, refreshes=h)); the twin — like the shader — learns the
    same number as k = the refreshes ELAPSED when the next frame arrives."""
    g, s = _pair(parity)
    rng = np.random.default_rng(7)
    holds = [1, 1, 2, 3, 0, 1, 5, 2, 0, 0, 1, 1, 4, 70, 3, 200, 2]          # 0 = replaced inside its refresh; 70 / 200 = long pauses
    k = 1
    for i, h in enumerate(holds):
        d = rng.uniform(0, 1, (12, 12)) if i % 4 else np.round(rng.uniform(0, 1, (12, 12)))   # steps and noise
        g.advance(k)
        gt, ge = g.pair(d); st, se = s.fields(d)
        assert gt.dtype == np.float32 and ge.dtype == np.float32
        assert np.allclose(gt, st, atol=2e-6) and np.allclose(ge, se, atol=2e-6), i
        g.commit(d); s.commit(d, refreshes=h)
        k = h
    assert g.s is not None and g.n == sum(holds[:-1])                # the refresh index: n += k; the long pauses were no reset


def test_first_frame_and_reset_are_the_stateless_layer():
    g = GpuPanelDriveState()
    assert g.mode == MODE_PANEL == 3 and g.closure == CLOSURE_DEFAULT == 0.72 and g.parity == -1
    d0, d1 = np.full((4, 4), 0.25), np.full((4, 4), 0.75)
    g.advance(1)
    for d in (d0, d1):                                               # no state yet: every round sees its OWN drives
        t, e = g.pair(d)
        assert np.array_equal(t, d.astype(np.float32)) and np.array_equal(e, d.astype(np.float32))
    g.commit(d1)                                                     # the panel is taken as settled on the first frame
    assert np.array_equal(g.s[0], g.s[1]) and np.array_equal(g.s[0], d1.astype(np.float32)) and g.n == 0
    for _ in range(5):                                               # static content: the state never moves, bit for bit
        g.advance(1)
        t, e = g.pair(d1)
        assert np.array_equal(t, d1.astype(np.float32)) and np.array_equal(e, d1.astype(np.float32))
        g.commit(d1)
    g.commit(d0)                                                     # a change, then a long static pause: NOT a reset —
    g.advance(MAX_REFRESHES + 1)                                     # the clocks have settled on it (blends exactly 1)
    assert g.s is not None and g.n == 5 + MAX_REFRESHES + 1 and [float(v) for v in g.factors[:4]] == [1.0] * 4
    t, e = g.pair(d1)
    assert np.allclose(t, d0, atol=1e-7) and np.array_equal(t, e)
    g.reset()                                                        # the C++ reset rules (layer idle, setting / period change)
    assert g.s is None and np.array_equal(g.pair(d0)[0], d0.astype(np.float32))


def test_k_0_holds_the_maps_replaces_the_target_and_reseeds_inside_the_seeding_refresh():
    g = GpuPanelDriveState(0.72, -1)
    a, b, c = np.full((3, 3), 0.2), np.full((3, 3), 1.0), np.full((3, 3), 0.6)
    g.advance(0); g.commit(a)                                        # seed
    g.advance(0)                                                     # a second frame inside the seeding refresh: re-seed
    assert g.s is None and np.array_equal(g.pair(b)[0], b.astype(np.float32))
    g.commit(b)
    assert np.array_equal(g.s[0], b.astype(np.float32)) and g.n == 0
    g.advance(1); t1, e1 = g.pair(c); g.commit(c)                    # refresh 1: target c from here on ...
    s_before = [v.copy() for v in g.s]
    g.advance(0)                                                     # ... but a frame arrives inside refresh 1 again
    t2, e2 = g.pair(a)
    assert t2 is t1 and e2 is e1 and g.n == 1                        # the maps are held, nothing advances
    assert all(np.array_equal(x, y) for x, y in zip(g.s, s_before))
    g.commit(a)                                                      # the later frame is the one the panel shows
    g.advance(2)
    assert np.all(g.true < 1.0) and np.all(g.true <= b.astype(np.float32))      # moving from b toward a (0.2), not c (0.6)
    ref = GpuPanelDriveState(0.72, -1)                               # = the sequence without the replaced frame
    ref.advance(0); ref.commit(b); ref.advance(1); ref.commit(a); ref.advance(2)
    assert np.array_equal(g.true, ref.true) and np.array_equal(g.est, ref.est)


def test_both_rounds_read_the_same_maps_and_the_state_ignores_this_frames_drives():
    g = GpuPanelDriveState(0.72, -1)
    a, b = np.full((3, 3), 0.2), np.full((3, 3), 1.0)
    g.advance(1); g.commit(a)
    g.advance(1); g.commit(b)                                        # the step is sent
    g.advance(1)
    r0 = g.pair(np.zeros((3, 3))); r1 = g.pair(np.ones((3, 3)))      # round 0 / round 1 drives differ: the maps do not
    assert r0[0] is r1[0] and r0[1] is r1[1]
    # unknown parity, one refresh after the step: one clock ticked (0.72 of the gap), the other not; the compensation
    # state is still the old one: the flash frame the correction has to pre-empt. The mean transient (0.288) is clamped to
    # ROBUST_CAP x 0.2; the plain mean (-2) takes it whole
    from dlc.fald.paneltime import PARITY_MEAN, ROBUST_CAP
    assert np.allclose(r0[0], 0.2 + ROBUST_CAP * 0.2, atol=1e-6) and np.allclose(r0[1], 0.2, atol=1e-6)
    m = GpuPanelDriveState(0.72, PARITY_MEAN)
    m.advance(1); m.commit(a); m.advance(1); m.commit(b); m.advance(1)
    assert np.allclose(m.pair(a)[0], 0.2 + 0.5 * 0.72 * 0.8, atol=1e-6) and np.allclose(m.pair(a)[1], 0.2, atol=1e-6)


def test_factors_are_float32_twins_of_the_reference_blend_factors():
    for n_a in range(5):
        for k in range(1, 6):
            for parity in (-1, 0, 1):
                f = clock_factors32(n_a, k, 0.72, parity)
                assert all(isinstance(v, np.float32) for v in f)
                for p in (0, 1):
                    a_s, a_p = blend_factors(n_a, k, 0.72, p)
                    assert abs(float(f[2 * p]) - a_s) < 1e-6 and abs(float(f[2 * p + 1]) - a_p) < 1e-6
                assert (float(f[4]), float(f[5])) == {-1: (0.5, 0.5), 0: (1.0, 0.0), 1: (0.0, 1.0)}[parity]
    # parity -1 does not care where the index started (spec: "invariant to the origin of n")
    a, b = clock_factors32(4, 3, 0.72, -1), clock_factors32(5, 3, 0.72, -1)
    assert sorted(map(float, a[0:4:2])) == sorted(map(float, b[0:4:2])) and sorted(map(float, a[1:4:2])) == sorted(map(float, b[1:4:2]))
    # the numbers tests/test_fald.cpp pins against FaldPanelClockFactors (n_a 0, k 1 / 2 / 5, closure 0.72)
    assert [round(float(v), 6) for v in clock_factors32(0, 1, 0.72, -1)[:4]] == [0.0, 0.0, 0.72, 0.0]
    assert [round(float(v), 6) for v in clock_factors32(0, 2, 0.72, -1)[:4]] == [0.72, 0.0, 0.72, 0.72]
    assert [round(float(v), 6) for v in clock_factors32(0, 5, 0.72, -1)[:4]] == [0.9216, 0.9216, 0.978048, 0.9216]
    assert float(clock_factors32(0, 2, 9.0, 0)[0]) == 1.0 and abs(float(clock_factors32(0, 2, 0.0, 0)[0]) - 0.05) < 1e-7   # clamped


def test_emulator_sequence_with_the_panel_clock(tmp_path):
    """Through Emu.run (the shader's pass order): frame 0 = the stateless run bit for bit; a step then shows the
    lag; held long enough the state settles on the drives again."""
    from dlc.fald.export import export_panel_params
    from dlc.fald.gpuemu import Emu
    from dlc.fald.model import FaldModel, FaldParams
    from dlc.fald.panelfile import read_panel_file
    m = FaldModel(FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75,
                             est_phase_px=-20.6, tmin=1.5e-3))
    export_panel_params(m, tmp_path / "small.bin")
    emu = Emu(read_panel_file(tmp_path / "small.bin"), width=960, height=540)

    def scrgb(bright):
        img = np.full((3, m.h, m.w), 5.0)
        if bright:
            img[:, 5 * m.ch:7 * m.ch, 5 * m.cw:7 * m.cw] = m.p.white_nits
        return np.ascontiguousarray(np.repeat(np.repeat(img, 5, axis=1), 5, axis=2).transpose(1, 2, 0) / 80.0)
    dark, bright = scrgb(False), scrgb(True)
    g = GpuPanelDriveState(0.72, -1)
    first = emu.run(dark, fp16_out=False, temporal=g)
    assert np.array_equal(first["out"], emu.run(dark, fp16_out=False)["out"])
    step = emu.run(bright, fp16_out=False, temporal=g, refreshes=1)            # the fields are still the dark frame's
    assert np.array_equal(step["drive_true"], first["drive1"]) and np.array_equal(step["drive_est"], first["drive1"])
    lag = emu.run(bright, fp16_out=False, temporal=g, refreshes=1)             # one refresh later: one clock ticked, estimate not
    # the step: the ticked clock's transient is the whole step against the old state, clamped (parity -1) to ROBUST_CAP x
    # the estimate map; the plain mean (-2) keeps half of the step
    from dlc.fald.paneltime import PARITY_MEAN, ROBUST_CAP
    assert np.array_equal(lag["drive_est"], first["drive1"])
    assert np.allclose(lag["drive_true"][5, 5], lag["drive_est"][5, 5] * np.float32(1 + ROBUST_CAP), rtol=1e-6)
    gm = GpuPanelDriveState(0.72, PARITY_MEAN)
    emu.run(dark, fp16_out=False, temporal=gm); emu.run(bright, fp16_out=False, temporal=gm, refreshes=1)
    lag_m = emu.run(bright, fp16_out=False, temporal=gm, refreshes=1)
    assert float(lag_m["drive_true"][5, 5]) > float(lag_m["drive_est"][5, 5]) and np.array_equal(lag_m["drive_est"], first["drive1"])
    for _ in range(settle_refreshes(0.72) + 4):
        out = emu.run(bright, fp16_out=False, temporal=g, refreshes=1)
    # at rest both maps ARE the frame's own round-1 drives: fields -> (d, d). The OUTPUT converges to the inverse's fixed
    # point (one round per frame): <= 0.4 % from the two-round stateless output on the fitted PA32UCXR model (main session
    # check 2026-09-20); on this unfitted toy model the two differ far more, so it is not compared here.
    assert np.allclose(out["drive_true"], out["drive1"], atol=5e-3) and np.allclose(out["drive_est"], out["drive1"], atol=5e-3)
    assert np.allclose(out["drive_true"], out["drive_est"], atol=5e-3)


# ------------------------------------------------------------------------------------------ the C++ / HLSL side
@pytest.mark.skipif(not (_SRC.parent / "shared" / "fald_shader.h").exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_cpp_and_hlsl_carry_the_same_law():
    # the constants and the per-run bookkeeping (plan, map choice, settle hold) live in shared/fald_temporal.{h,cpp} since
    # 6a4023e — ONE implementation for the overlay (src/fald.cpp FaldRunPasses) and the DWM hook (FaldRun); each path
    # keeps only its D3D side (the passes, the copies, which texture a FaldDriveMap is)
    h = (_SHARED / "fald_temporal.h").read_text(encoding="utf-8")
    tc = (_SHARED / "fald_temporal.cpp").read_text(encoding="utf-8")
    cpp = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    sh = (_SRC.parent / "shared" / "fald_shader.h").read_text(encoding="utf-8")
    num = lambda name: float(re.search(name + r" = ([-\d.]+)f?u?;", h).group(1))
    assert num("FALD_TEMPORAL_PANEL") == MODE_PANEL and num("FALD_CLOCK_MAX_REFRESHES") == MAX_REFRESHES
    assert num("FALD_CLOCK_CLOSURE_DEFAULT") == CLOSURE_DEFAULT and num("FALD_CLOCK_CLOSURE_MIN") == CLOSURE_MIN
    assert num("FALD_CLOCK_CLOSURE_MAX") == CLOSURE_MAX and num("FALD_CLOCK_SETTLE_MAX") == SETTLE_MAX
    # the C++ order the opt-in WARP replay (test_fald_paneltime_warp.py) checks on a device: the clock pass ONCE, before
    # round 0, only for k >= 1; the next target = the drive texture AFTER round 1
    # (FaldRunPasses executes the pure FaldPanelClockPlan literally — tests/test_fald.cpp holds the plan's rules: on k = 0
    # no pass, the maps still bound, the target still replaced; the settle hold paid per elapsed refresh)
    begin = re.search(r"FaldTemporalRun FaldTemporalBeginRun\(.*?\n\}", tc, re.S).group(0)
    end = re.search(r"void FaldTemporalEndRun\(.*?\n\}", tc, re.S).group(0)
    assert "run.clock = FaldPanelClockPlan(seeded, r->clkElapsed);" in begin
    # bindMaps: the real-spread kernel reads the clock's B_true map (driveFiltTex), the estimate kernel its B_est map
    assert "if (run.clock.bindMaps) { run.trueMap = FALD_MAP_FILTERED; run.estMap = FALD_MAP_CLOCK_EST; }" in begin
    assert "FaldSettleAccount(r, newContent || run.resumed, FaldPanelClockSettleFrames(r->clkClosure));" in end
    assert "settleLeft--" not in end
    run = cpp[cpp.index("void FaldRunPasses("):]
    assert "const FaldTemporalRun trun = FaldTemporalBeginRun(r, ts, clockOk," in run and "const FaldClockPlan plan = trun.clock;" in run
    assert run.count("RunPanelClock(r, partial);") == 1 and run.index("if (plan.runPass) RunPanelClock(r, partial);") < run.index("RunStat(r, 0);")
    # (2026-10-22: the panel clock runs ONCE per run and both rounds read its two maps: the B_true map in the round-1
    # filtered slot (the dump's fald_drive_filt.f32, as on main) and the est map. The evening of 2026-10-22: a branch's clock
    # pass wrote a round-0 slot while round 1's convolution read round 1's, a map nothing writes in mode 3: the owner's
    # over-brightened / shadowed areas in panel-clock mode. Pinned here, both hosts. The round-0 filtered slot itself went
    # with LED lag modes 1 / 2, 2026-11-12.)
    assert "return m == FALD_MAP_FILTERED ? r->driveFiltSRV" in run and "driveFilt0" not in cpp
    assert ": (m == FALD_MAP_CLOCK_EST ? r->clkEstSRV : (round ? r->driveSRV : r->drive0SRV));" in run
    assert "RunConv(r, mapSrv(trun.trueMap, 0), mapSrv(trun.estMap, 0), r->boostSRV[0], 0, partial);" in run
    assert "RunConv(r, mapSrv(trun.trueMap, 1), mapSrv(trun.estMap, 1), r->boostSRV[1], 1, partial);" in run and run.count("clkEstSRV") == 1
    clock = cpp[cpp.index("static void RunPanelClock(FaldResources* r, bool partial)"):cpp.index("// trueDrive / estDrive:")]
    assert "{ r->driveFiltUAV, r->clkEstUAV, r->clkStateUAV[0], r->clkStateUAV[1], nullptr, r->changeFlagsUAV }" in clock
    # a run whose kernel map routing differs from the previous run's (a seeding run reads the drives, the clock run after
    # it the clock's maps) takes the full path: the GPU compares each map with its own previous value only
    assert "if (partial && (trun.trueMap != r->lastTrueMap || trun.estMap != r->lastEstMap)) partial = false;" in run
    assert "r->lastTrueMap = trun.trueMap; r->lastEstMap = trun.estMap;" in run
    assert run.index("RunStat(r, 1);") < run.index("if (plan.commitPrev) g_context->CopyResource(r->clkPrevTex, r->driveTex);")
    assert run.count("CopyResource(r->clkPrevTex") == 1 and run.count("plan.seedStates") == 1
    assert "FaldTemporalEndRun(r, trun, newContent);" in run and "settleLeft--" not in run
    # ... and the DWM hook executes the same plan from the same shared code
    hook = _HOOK.read_text(encoding="utf-8")
    hrun = hook[hook.index("bool FaldRun("):]
    assert "const FaldTemporalRun trun = FaldTemporalBeginRun(m, m->tempSettings, clockOk," in hrun
    assert hrun.count("RunPanelClock(m, partial);") == 1 and hrun.index("if (trun.clock.runPass) RunPanelClock(m, partial);") < hrun.index("RunStat(m, 0);")
    assert "return k == FALD_MAP_FILTERED ? m->driveFiltSRV" in hrun and "driveFilt0" not in hook
    assert ": (k == FALD_MAP_CLOCK_EST ? m->clkEstSRV : (round ? m->driveSRV : m->drive0SRV));" in hrun
    assert "RunConv(m, mapSrv(trun.trueMap, 0), mapSrv(trun.estMap, 0), m->boostSRV[0], 0, partial);" in hrun
    assert "RunConv(m, mapSrv(trun.trueMap, 1), mapSrv(trun.estMap, 1), m->boostSRV[1], 1, partial);" in hrun and hrun.count("clkEstSRV") == 1
    hclock = hook[hook.index("static void RunPanelClock(FaldMonitor* m, bool partial)"):hook.index("// Starfield balancing, mirror of src/fald.cpp RunStar")]
    assert "{ m->driveFiltUAV, m->clkEstUAV, m->clkStateUAV[0], m->clkStateUAV[1], nullptr, m->changeFlagsUAV }" in hclock
    assert "if (partial && (trun.trueMap != m->lastTrueMap || trun.estMap != m->lastEstMap)) partial = false;" in hrun
    assert "m->lastTrueMap = trun.trueMap; m->lastEstMap = trun.estMap;" in hrun
    assert hrun.index("RunStat(m, 1);") < hrun.index("if (trun.clock.commitPrev) g_ctx->CopyResource(m->clkPrevTex, m->driveTex);")
    assert hrun.count("CopyResource(m->clkPrevTex") == 1 and hrun.count("clock.seedStates") == 1
    assert "FaldTemporalEndRun(m, trun, newContent);" in hrun and "settleLeft--" not in hrun
    from dlc.fald.paneltime import LOCK_GAIN
    assert num("FALD_CLOCK_LOCK_GAIN") == LOCK_GAIN
    # the pass: one blend per clock toward the previous frame's drives, the weighted maps, the states advanced in place
    body = re.search(r'g_faldPanelClockSource = R"\((.*?)\)";', sh, re.S).group(1)
    for line in ("float g0 = d - s0, g1 = d - s1;", "float t0 = s0 + clkTrue0 * g0, t1 = s1 + clkTrue1 * g1;",
                 "float e0 = s0 + clkEst0 * g0, e1 = s1 + clkEst1 * g1;", "float oEst = clkW0 * e0 + clkW1 * e1;",
                 "clkState0[id.xy] = t0; clkState1[id.xy] = t1;"):
        assert line in body, line
    # unknown parity (both weights > 0): the clamped mean transient (gpuemu.clock_robust_true32); known parity: the plain
    # weighted mean. (The plain-mean VARIANT for the A/B's parity -2 — FALD_CLOCK_MEAN — went 2026-11-12: the robust
    # combination won the owner's 2026-10-24 blind A/B; dlc.fald.paneltime keeps PARITY_MEAN as the research model.)
    assert "FALD_CLOCK_MEAN" not in body
    assert "float tr = clkW0 * (t0 - e0) + clkW1 * (t1 - e1), lim = FALD_CLOCK_ROBUST_CAP * oEst;" in body
    assert "float oTrue = (clkW0 > 0.0f && clkW1 > 0.0f) ? oEst + min(max(tr, -lim), lim) : clkW0 * t0 + clkW1 * t1;" in body
    from dlc.fald.paneltime import ROBUST_CAP
    hl = lambda name: float(re.search(r"static const float " + name + r" = ([\d.]+)f;", body).group(1))
    assert hl("FALD_CLOCK_ROBUST_CAP") == ROBUST_CAP and num("FALD_CLOCK_ROBUST_CAP") == ROBUST_CAP
    assert "FALD_CLOCK_PARITY_MEAN" not in sh and "g_faldClockMeanDefine" not in sh
    # both hosts dispatch the one pass (no plain-mean variant, no first-order pass 1b)
    assert "g_context->CSSetShader(g_faldPanelClockCS, nullptr, 0);" in cpp and "PanelClockMean" not in cpp
    assert "g_ctx->CSSetShader(g_clockCS, nullptr, 0);" in hook and "g_clockMeanCS" not in hook
    assert "g_faldTemporalSource" not in sh
    # FillCB puts the six words where the HLSL declares them (test_fald_transfer holds the offsets against the compiler)
    for src, v in ((cpp, "r"), (hook, "m")):
        assert f"f[66] = {v}->clkW[0]; f[67] = {v}->clkW[1];" in src
        assert f"f[68] = {v}->clkFactor[0]; f[69] = {v}->clkFactor[1]; f[70] = {v}->clkFactor[2]; f[71] = {v}->clkFactor[3];" in src


# ------------------------------------------------------------------------------------------ the clock at rest
# (shared/fald_temporal.h above FALD_CLOCK_REST_ALLOWED; HLSL pass 1c-0 g_faldPanelClockWatchSource + pass 1c's early
# return; C++ FaldClockRestBegin / FaldTemporalEndRun). The watch reads the clocks' states BEFORE this run's advance and
# the target the run aims at (d_prev); these tests set the states directly where the rule, not the arithmetic, is held.
_A, _B = np.float32(0.25), np.float32(0.5)


def _seeded(target=_A, shape=(1, 1), rest=True):
    g = GpuPanelDriveState(0.72, -1, rest=rest)
    g.commit(np.full(shape, target, np.float32))                        # the seeding run (new content)
    return g


def _clock_run(g, target, states, content=False, k=1, shape=(1, 1)):
    """One run: the previous run's commit (the target this run's clock aims at), the states as given, the advance."""
    g.commit(np.full(shape, target, np.float32), new_content=content)
    g.s = [np.full(shape, states[0], np.float32), np.full(shape, states[1], np.float32)]
    g.advance(k)
    return g.at_rest


def test_rest_one_return_is_not_a_pair_and_a_b_a_b_on_still_frames_is():
    from dlc.fald.gpuemu import REST_ALLOWED, REST_RESET, REST_STILL
    mid = np.float32(0.375)
    g = _seeded()
    assert not _clock_run(g, _A, (mid, mid))                             # the first clock run after the seed: RESET
    assert g.rest_flags == REST_RESET and g.run_seq == 1                 # (the seed's commit was content: not STILL)
    assert not _clock_run(g, _B, (mid, mid))                             # A -> B: (B, A, 0)
    assert g.rest_flags == REST_ALLOWED | REST_STILL
    assert not _clock_run(g, _A, (mid, mid))                             # back once: (A, B, 1) — not a pair, states != A
    assert [float(v) for v in g.levels[0, 0]] == [0.25, 0.5, 1.0, 0.0]
    assert _clock_run(g, _B, (mid, mid))                                 # back twice: (B, A, 2) — a pair, states inside
    assert [float(v) for v in g.levels[0, 0]] == [0.5, 0.25, 2.0, 0.0] and g.rest_runs == 1
    assert int(g.words[1]) == 4 and int(g.words[2]) == REST_ALLOWED | REST_STILL and int(g.words[0]) == 3
    assert _clock_run(g, _A, (_A, _B))                                   # the ends are inside (ends included)
    assert not _clock_run(g, _B, (_A, np.nextafter(_B, np.float32(1))))   # one ulp outside: not confined
    assert int(g.words[0]) == g.run_seq
    # a third value starts over: (C, B, 0) — no pair, so confined only on states equal to C bit for bit
    c = np.float32(0.3)
    assert _clock_run(g, c, (c, c))
    assert [float(v) for v in g.levels[0, 0]] == [float(c), 0.5, 0.0, 0.0]
    assert not _clock_run(g, c, (c, mid))                                # (unchanged target) mid is not C


def test_rest_a_change_committed_by_a_content_frame_starts_over():
    mid = np.float32(0.375)
    g = _seeded()
    _clock_run(g, _A, (mid, mid))
    _clock_run(g, _B, (mid, mid))
    _clock_run(g, _A, (mid, mid), content=True)                          # the content moved it: (A, A, 0), no pair
    assert [float(v) for v in g.levels[0, 0]] == [0.25, 0.25, 0.0, 0.0]
    assert not _clock_run(g, _B, (mid, mid))                             # (B, A, 0)
    assert not _clock_run(g, _A, (mid, mid))                             # (A, B, 1)
    assert _clock_run(g, _B, (mid, mid))                                 # (B, A, 2): a pair again
    # content committed by a k = 0 run (no clock run of its own) counts for the next watch too (C++ EndRun runs every run)
    g2 = _seeded()
    _clock_run(g2, _A, (mid, mid)); _clock_run(g2, _B, (mid, mid)); _clock_run(g2, _A, (mid, mid))
    g2.commit(np.full((1, 1), _A, np.float32), new_content=True)        # a content run's commit, then a k = 0 run's
    g2.advance(0)
    assert not _clock_run(g2, _B, (mid, mid))                            # STILL is off: (B, B, 0) instead of the pair
    assert [float(v) for v in g2.levels[0, 0]] == [0.5, 0.5, 0.0, 0.0]


def test_rest_first_clock_run_after_a_seed_never_rests():
    from dlc.fald.gpuemu import REST_RESET
    g = _seeded()
    assert not _clock_run(g, _A, (_A, _A))                               # confined (equal bit for bit), but RESET
    assert g.rest_flags & REST_RESET and int(g.words[0]) != g.run_seq    # every zone confined; the flag forbids the rest
    assert _clock_run(g, _A, (_A, _A))                                   # the next one rests
    g.reset()                                                            # the C++ reset rules (idle, a setting / period change)
    assert not g.at_rest
    g.commit(np.full((1, 1), _A, np.float32))                            # re-seed
    assert not _clock_run(g, _A, (_A, _A)) and g.rest_flags & REST_RESET
    assert _clock_run(g, _A, (_A, _A))
    # a re-seed inside the seeding refresh (k = 0 at n = 0) starts the levels over too
    g2 = _seeded()
    g2.advance(0)
    g2.commit(np.full((1, 1), _A, np.float32))
    assert not _clock_run(g2, _A, (_A, _A)) and g2.rest_flags & REST_RESET
    # the run number lives with the resources: it keeps counting across a reset (the C++ clkRunSeq)
    assert g.run_seq == 4 and g2.run_seq == 1


def test_rest_nothing_moves_and_the_maps_are_the_previous_runs_bit_for_bit():
    shape = (2, 3)
    rng = np.random.default_rng(3)
    lo, hi = np.float32(0.2), np.float32(0.6)
    g = _seeded(lo, shape)
    g.advance(1)                                                         # RESET run with the arithmetic (states == target)
    seq = [hi, lo, hi]                                                   # the loop's own toggle, still frames
    for t in seq:
        g.commit(np.full(shape, t, np.float32), new_content=False)
        g.s = [rng.uniform(0.2, 0.6, shape).astype(np.float32) for _ in range(2)]   # anywhere inside the pair's interval
        before = ([v.copy() for v in g.s], g.true.copy(), g.est.copy(), g.n)
        g.advance(1)
    assert g.at_rest and g.rest_runs == 1
    assert all(np.array_equal(a.view(np.uint32), b.view(np.uint32)) for a, b in zip(g.s, before[0]))
    assert np.array_equal(g.true.view(np.uint32), before[1].view(np.uint32))
    assert np.array_equal(g.est.view(np.uint32), before[2].view(np.uint32))
    assert g.n == before[3] + 1                                          # the refresh index still advances (CPU bookkeeping)
    t1, e1 = g.pair(np.zeros(shape))
    assert t1 is g.true and e1 is g.est
    # at rest again on the next still run with the same toggle: still nothing moves; a long pause does not end a rest
    g.commit(np.full(shape, lo, np.float32), new_content=False)
    s_keep = [v.copy() for v in g.s]
    g.advance(MAX_REFRESHES + 5)
    assert g.at_rest and all(np.array_equal(a, b) for a, b in zip(g.s, s_keep))


def test_rest_any_moved_target_releases_at_once_from_the_rested_states():
    import copy
    shape = (2, 2)
    lo, hi = np.float32(0.2), np.float32(0.6)
    g = _seeded(lo, shape)
    g.advance(1)
    mid = np.full(shape, np.float32(0.4))
    for t in (hi, lo, hi):
        g.commit(np.full(shape, t, np.float32), new_content=False)
        g.s = [mid.copy(), mid.copy()]
        g.advance(1)
    assert g.at_rest
    rested = [v.copy() for v in g.s]
    moved = np.full(shape, hi, np.float32); moved[1, 0] = np.float32(0.9)    # one zone's target leaves the pair
    for content in (False, True):
        h = copy.deepcopy(g)
        h.commit(moved, new_content=content)
        ref = copy.deepcopy(h)
        ref.rest_enabled = False                                         # the unheld arithmetic from the same states
        h.advance(1); ref.advance(1)
        assert not h.at_rest and int(h.words[0]) == h.run_seq
        for a, b in [(h.true, ref.true), (h.est, ref.est), (h.s[0], ref.s[0]), (h.s[1], ref.s[1])]:
            assert np.array_equal(a.view(np.uint32), b.view(np.uint32))
        assert not np.array_equal(h.s[0], rested[0]) or not np.array_equal(h.s[1], rested[1])


def _advance_without_the_rule(g, k):
    """GpuPanelDriveState.advance as it was before the clock at rest (2026-11-12), for the bit-for-bit comparison."""
    from dlc.fald.gpuemu import clock_robust_true32
    if g.s is None:
        return
    if k == 0:
        if g.n == 0:
            g.reset()
        return
    aT0, aE0, aT1, aE1, w0, w1 = g.factors = clock_factors32(g.n, k, g.closure, g.parity)
    d, (s0, s1) = g.d_prev, g.s
    g0 = (d - s0).astype(np.float32); g1 = (d - s1).astype(np.float32)
    t0 = (s0 + aT0 * g0).astype(np.float32); t1 = (s1 + aT1 * g1).astype(np.float32)
    e0 = (s0 + aE0 * g0).astype(np.float32); e1 = (s1 + aE1 * g1).astype(np.float32)
    g.est = (w0 * e0 + w1 * e1).astype(np.float32)
    g.true = clock_robust_true32(t0, e0, t1, e1, w0, w1, g.est) if g.parity == -1 else (w0 * t0 + w1 * t1).astype(np.float32)
    g.s = [t0, t1]
    g.n += k


@pytest.mark.parametrize("parity", [-1, 0, 1])
def test_rest_off_is_todays_arithmetic_bit_for_bit(parity):
    rng = np.random.default_rng(11)
    shape = (4, 5)
    a, b = rng.uniform(0.05, 0.9, shape).astype(np.float32), rng.uniform(0.05, 0.9, shape).astype(np.float32)
    # a frame, a long still stretch whose target toggles between two stairs (the loop), a k = 0 and a k = 2 run, a change
    targets = [(a, True, 0)] + [(a if i % 2 else b, False, 1) for i in range(30)] + [(b, False, 0), (b, False, 2)]
    targets += [(rng.uniform(0.05, 0.9, shape).astype(np.float32), True, 1)] + [(a if i % 2 else b, False, 1) for i in range(12)]
    on, off = GpuPanelDriveState(0.72, parity, rest=True), GpuPanelDriveState(0.72, parity, rest=False)
    old = GpuPanelDriveState(0.72, parity)
    for d, content, k in targets:
        on.advance(k); off.advance(k); _advance_without_the_rule(old, k)
        for x, y in ((off.true, old.true), (off.est, old.est)):
            assert (x is None and y is None) or np.array_equal(x.view(np.uint32), y.view(np.uint32))
        if old.s is not None:
            assert all(np.array_equal(x.view(np.uint32), y.view(np.uint32)) for x, y in zip(off.s, old.s))
        assert off.n == old.n and not off.at_rest
        on.commit(d, new_content=content); off.commit(d, new_content=content); old.commit(d)
    assert off.rest_runs == 0 and off.run_seq == on.run_seq
    assert on.rest_runs > 0                                              # the same sequence with the rule rests


@pytest.mark.skipif(not (_SRC.parent / "shared" / "fald_shader.h").exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_cpp_and_hlsl_carry_the_same_rest_rule():
    """Text pins: the watch pass's decisive lines, pass 1c's early return and the C++ flag bookkeeping the twin mirrors
    (GpuPanelDriveState._rest_begin / _watch / commit). An edit to either side breaks one of these."""
    from dlc.fald.gpuemu import REST_ALLOWED, REST_RESET, REST_STILL
    h = (_SHARED / "fald_temporal.h").read_text(encoding="utf-8")
    tc = (_SHARED / "fald_temporal.cpp").read_text(encoding="utf-8")
    sh = (_SHARED / "fald_shader.h").read_text(encoding="utf-8")
    flag = lambda name: int(re.search(r"constexpr uint32_t " + name + r" = (\d+)u;", h).group(1))
    assert (flag("FALD_CLOCK_REST_ALLOWED"), flag("FALD_CLOCK_REST_RESET"), flag("FALD_CLOCK_REST_STILL")) == (REST_ALLOWED, REST_RESET, REST_STILL)
    watch = re.search(r'g_faldPanelClockWatchSource = R"\((.*?)\)";', sh, re.S).group(1)
    for line in ("if (id.x == 0u && id.y == 0u) { clkRest[1] = clkRunSeq; clkRest[2] = clkRestFlags; }",
                 "float d = driveTex.Load(int3(id.xy, 0));",
                 "if ((clkRestFlags & 2u) != 0u) lv = float4(d, d, 0.0f, 0.0f);",
                 "else if (d != lv.x) {",
                 "if ((clkRestFlags & 4u) != 0u) lv = float4(d, lv.x, (d == lv.y) ? min(lv.z + 1.0f, 2.0f) : 0.0f, 0.0f);",
                 "else lv = float4(d, d, 0.0f, 0.0f);",
                 "clkLevels[zi] = lv;",
                 "float lo = lv.x, hi = lv.x;",
                 "if (lv.z >= 2.0f) { lo = min(lv.x, lv.y); hi = max(lv.x, lv.y); }",
                 "float s0 = clkState0[id.xy], s1 = clkState1[id.xy];",
                 "bool confined = s0 >= lo && s0 <= hi && s1 >= lo && s1 <= hi;",
                 "if (!confined) clkRest[0] = clkRunSeq;"):
        assert line in watch, line
    clock = re.search(r'g_faldPanelClockSource = R"\((.*?)\)";', sh, re.S).group(1)
    assert "[branch] if ((clkRestFlags & 1u) != 0u) {" in clock and "[branch] if (clkRest[0] != clkRunSeq) return;   // at rest: nothing moves" in clock
    assert clock.index("if (clkRest[0] != clkRunSeq) return;") < clock.index("float d = driveTex.Load")
    begin = re.search(r"void FaldClockRestBegin\(FaldTemporalState\* r\) \{.*?\n\}", tc, re.S).group(0)
    for line in ("if (++r->clkRunSeq == 0u) { r->clkRunSeq = 1u; r->clkLevelsValid = false;",
                 "if (!r->clkLevelsValid) f |= FALD_CLOCK_REST_RESET;",
                 "else if (g_faldClockRestOverride != 0) f |= FALD_CLOCK_REST_ALLOWED;",
                 "if (!r->clkTargetFromContent) f |= FALD_CLOCK_REST_STILL;",
                 "r->clkRestFlags = f;", "r->clkLevelsValid = true;", "r->clkTargetFromContent = false;"):
        assert line in begin, line
    note = re.search(r"bool FaldClockRestNote\(.*?\n\}", tc, re.S).group(0)
    assert "const bool rest = (flags & FALD_CLOCK_REST_ALLOWED) != 0u && lastOutSeq != seq;" in note
    end = re.search(r"void FaldTemporalEndRun\(.*?\n\}", tc, re.S).group(0)
    assert "if (run.clock.commitPrev && newContent) r->clkTargetFromContent = true;" in end
    step = re.search(r"bool FaldPanelClockStep\(.*?\n\}", tc, re.S).group(0)
    assert step.count("r->clkLevelsValid = false;") == 2                 # the seeding run and the re-seed inside its refresh
    idle = re.search(r"void FaldTemporalIdle\(.*?\n\}", tc, re.S).group(0)
    assert "r->clkLevelsValid = false;" in idle
    # both hosts: the watch pass before the clock pass, Begin before both, ALLOWED cleared without the watch
    cpp = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    hook = _HOOK.read_text(encoding="utf-8")
    for src, v, cs in ((cpp, "r", "g_faldPanelClockWatchCS"), (hook, "m", "g_clockWatchCS")):
        body = src[src.index("static void RunPanelClock("):]
        body = body[:body.index("\n}\n")]
        assert "FaldClockRestBegin(" + v + ");" in body and f"if (!watch) {v}->clkRestFlags &= ~FALD_CLOCK_REST_ALLOWED;" in body
        assert body.index("FaldClockRestBegin(") < body.index(cs + ", nullptr, 0);")
        # the dumps the WARP replay reads (fald_clock_rest.u32 / fald_clock_levels.f32 + the three text keys)
        assert 'L"fald_clock_rest.u32"' in src and 'L"fald_clock_levels.f32"' in src
        assert "clock_at_rest" in src and "clock_run_seq" in src and "clock_rest_flags" in src
        assert "(restWords[2] & FALD_CLOCK_REST_ALLOWED) != 0u && restWords[0] != restWords[1]" in src
