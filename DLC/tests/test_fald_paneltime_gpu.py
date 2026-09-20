"""Temporal mode 3 "panel clock" (work guide C13): the GPU-order twin (dlc/fald/gpuemu.py GpuPanelDriveState — pass 1c
``g_faldPanelClockSource`` + the CPU-side blend factors of ``FaldPanelClockFactors``) against the reference
(dlc/fald/paneltime.py PanelDriveState), and the pins that hold the C++ / HLSL side to both."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from dlc.fald.gpuemu import GpuPanelDriveState, clock_factors32
from dlc.fald.paneltime import (CLOSURE_DEFAULT, CLOSURE_MAX, CLOSURE_MIN, MAX_REFRESHES, MODE_PANEL, PanelDriveState,
                                PanelTimeLaw, blend_factors, settle_refreshes)

_SRC = Path(__file__).resolve().parents[2] / "src"


def _pair(parity):
    return GpuPanelDriveState(0.72, -1 if parity is None else parity), PanelDriveState(PanelTimeLaw(closure=0.72), parity)


@pytest.mark.parametrize("parity", [None, 0, 1])
def test_twin_equals_the_reference_with_k_refreshes_per_frame(parity):
    """The reference holds a frame for h refreshes (commit(d, refreshes=h)); the twin — like the shader — learns the
    same number as k = the refreshes ELAPSED when the next frame arrives."""
    g, s = _pair(parity)
    rng = np.random.default_rng(7)
    holds = [1, 1, 2, 3, 1, 5, 2, 1, 1, 4, 3, 2]
    k = 1
    for i, h in enumerate(holds):
        d = rng.uniform(0, 1, (12, 12)) if i % 4 else np.round(rng.uniform(0, 1, (12, 12)))   # steps and noise
        g.advance(k)
        gt, ge = g.pair(d); st, se = s.fields(d)
        assert gt.dtype == np.float32 and ge.dtype == np.float32
        assert np.allclose(gt, st, atol=2e-6) and np.allclose(ge, se, atol=2e-6), i
        g.commit(d); s.commit(d, refreshes=h)
        k = h
    assert g.n == sum(holds[:-1])                                    # the running refresh index: n += k


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
    g.advance(MAX_REFRESHES + 1)                                     # a gap the state cannot bridge = a reset
    assert g.s is None and np.array_equal(g.pair(d0)[0], d0.astype(np.float32))
    g.commit(d0); g.advance(MAX_REFRESHES)                           # 64 itself is bridged
    assert g.s is not None and g.n == MAX_REFRESHES


def test_both_rounds_read_the_same_maps_and_the_state_ignores_this_frames_drives():
    g = GpuPanelDriveState(0.72, -1)
    a, b = np.full((3, 3), 0.2), np.full((3, 3), 1.0)
    g.advance(1); g.commit(a)
    g.advance(1); g.commit(b)                                        # the step is sent
    g.advance(1)
    r0 = g.pair(np.zeros((3, 3))); r1 = g.pair(np.ones((3, 3)))      # round 0 / round 1 drives differ: the maps do not
    assert r0[0] is r1[0] and r0[1] is r1[1]
    # unknown parity, one refresh after the step: one clock ticked (0.72 of the gap), the other not -> mean 0.36;
    # the compensation state is still the old one: the flash frame the correction has to pre-empt
    assert np.allclose(r0[0], 0.2 + 0.5 * 0.72 * 0.8, atol=1e-6) and np.allclose(r0[1], 0.2, atol=1e-6)


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
    lag = emu.run(bright, fp16_out=False, temporal=g, refreshes=1)             # one refresh later: LEDs half way, estimate not
    assert float(lag["drive_true"][5, 5]) > float(lag["drive_est"][5, 5]) and np.array_equal(lag["drive_est"], first["drive1"])
    for _ in range(settle_refreshes(0.72) + 4):
        out = emu.run(bright, fp16_out=False, temporal=g, refreshes=1)
    # at rest both maps ARE the frame's own round-1 drives: fields -> (d, d). (The OUTPUT is then the inverse iterated one
    # round per frame, like mode 1 at rest — temporal.py's docstring: <= 0.1 % from the two-round stateless output on the
    # fitted PA32UCXR model, far more on this unfitted toy model — so it is not compared here.)
    assert np.allclose(out["drive_true"], out["drive1"], atol=5e-3) and np.allclose(out["drive_est"], out["drive1"], atol=5e-3)
    assert np.allclose(out["drive_true"], out["drive_est"], atol=5e-3)


# ------------------------------------------------------------------------------------------ the C++ / HLSL side
@pytest.mark.skipif(not (_SRC / "fald_shader.h").exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_cpp_and_hlsl_carry_the_same_law():
    h = (_SRC / "fald.h").read_text(encoding="utf-8")
    cpp = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    sh = (_SRC / "fald_shader.h").read_text(encoding="utf-8")
    num = lambda name: float(re.search(name + r" = ([-\d.]+)f?u?;", h).group(1))
    assert num("FALD_TEMPORAL_PANEL") == MODE_PANEL and num("FALD_CLOCK_MAX_REFRESHES") == MAX_REFRESHES
    assert num("FALD_CLOCK_CLOSURE_DEFAULT") == CLOSURE_DEFAULT and num("FALD_CLOCK_CLOSURE_MIN") == CLOSURE_MIN
    assert num("FALD_CLOCK_CLOSURE_MAX") == CLOSURE_MAX
    # the pass: one blend per clock toward the previous frame's drives, the weighted maps, the states advanced in place
    body = re.search(r'g_faldPanelClockSource = R"\((.*?)\)";', sh, re.S).group(1)
    for line in ("float g0 = d - s0, g1 = d - s1;", "float t0 = s0 + clkTrue0 * g0, t1 = s1 + clkTrue1 * g1;",
                 "clkTrueOut[id.xy] = clkW0 * t0 + clkW1 * t1;",
                 "clkEstOut[id.xy] = clkW0 * (s0 + clkEst0 * g0) + clkW1 * (s1 + clkEst1 * g1);",
                 "clkState0[id.xy] = t0; clkState1[id.xy] = t1;"):
        assert line in body, line
    # the first-order pass is untouched by the new mode
    old = re.search(r'g_faldTemporalSource = R"\((.*?)\)";', sh, re.S).group(1)
    assert "clk" not in old and "float a = (d > s) ? tempAlphaRise : tempAlphaFall;" in old
    # FillCB puts the six words where the HLSL declares them (test_fald_transfer holds the offsets against the compiler)
    assert "f[66] = r->clkW[0]; f[67] = r->clkW[1];" in cpp
    assert "f[68] = r->clkFactor[0]; f[69] = r->clkFactor[1]; f[70] = r->clkFactor[2]; f[71] = r->clkFactor[3];" in cpp
