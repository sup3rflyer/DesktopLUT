"""Temporal drive state of the FALD layer (dlc/fald/temporal.py) — the Python reference of the shader's per-cell
LED-law filter (src/fald_shader.h pass 1b, fald.cpp RunTemporal), its GPU-order emulation (dlc/fald/gpuemu.py) and
the step-response fit for the owner's phone video. Work guide H5 / item 4a (2026-09-17)."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from dlc.fald import temporal as T
from dlc.fald.correct import correct_image
from dlc.fald.model import FaldModel, FaldParams

_SHADER = Path(__file__).resolve().parents[2] / "shared" / "fald_shader.h"   # since e7f542f
DT = 1000.0 / 60.0


# ------------------------------------------------------------------------------------------ helpers
def test_alpha_and_settle_pin_the_cpp_numbers():
    """tests/test_fald.cpp pins the same values against FaldTemporalAlpha / FaldSettleFrames."""
    assert T.alpha_from_tau(0.0, 16.667) == 1.0 and T.alpha_from_tau(-5.0, 16.667) == 1.0 and T.alpha_from_tau(100.0, 0.0) == 1.0
    assert abs(T.alpha_from_tau(100.0, 16.667) - 0.153521) < 1e-5
    assert abs(T.alpha_from_tau(16.667, 16.667) - (1.0 - np.exp(-1.0))) < 1e-9
    assert T.settle_frames(0.0, 0.0, 16.667) == 0
    assert T.settle_frames(100.0, 50.0, 16.667) == 30
    assert T.settle_frames(50.0, 120.0, DT) == 36            # an exact multiple stays exact (no float creep to 37)
    assert T.settle_frames(100.0, 0.0, 0.0) == 0
    assert T.settle_frames(1.0, 0.0, 16.667) == 1
    assert T.settle_frames(0.0, 0.0, 16.667, delay_frames=2) == 2 and T.settle_frames(100.0, 50.0, 16.667, 1) == 31
    assert T.alpha_from_tau(float("nan"), 16.667) == 1.0          # NaN = instant, as the C++


def test_mode_names_and_validation():
    assert T.MODE_NAMES == {0: "off", 1: "both", 2: "true_only"} and T.MODE_CODES["true_only"] == 2
    with pytest.raises(ValueError):
        T.DriveState(3)


# ------------------------------------------------------------------------------------------ DriveState
def test_drive_state_semantics():
    rng = np.random.default_rng(1)
    d0 = rng.uniform(0, 1, (4, 5)); d1 = rng.uniform(0, 1, (4, 5))
    # off: never stores, always passes through
    off = T.DriveState(T.MODE_OFF, 100, 100, DT)
    assert np.array_equal(off.peek(d0), d0) and np.array_equal(off.commit(d0), d0) and off.state is None
    assert off.settle_frames() == 0
    # both: the first frame initialises from its own drives; then s' = s + a (d - s) with a per edge
    st = T.DriveState(T.MODE_BOTH, 100, 20, DT)
    assert np.array_equal(st.peek(d0), d0)                     # no state yet: pass-through (tempInit)
    st.commit(d0)
    assert np.array_equal(st.state, d0)
    exp = np.where(d1 > d0, st.alpha_rise, st.alpha_fall)
    want = d0 + exp * (d1 - d0)
    assert np.allclose(st.peek(d1), want)
    assert np.allclose(st.peek(d1), want)                      # peek does not advance
    ft, fe = st.fields(d1)
    assert np.allclose(ft, want) and np.allclose(fe, want)     # both fields from the state
    st.commit(d1)
    assert np.allclose(st.state, want) and st.frames == 2
    assert st.settle_frames() == T.settle_frames(100, 20, DT) == 30
    # true_only: the estimate kernel keeps the instantaneous drive
    st2 = T.DriveState(T.MODE_TRUE_ONLY, 100, 20, DT); st2.commit(d0)
    ft, fe = st2.fields(d1)
    assert np.allclose(ft, want) and np.array_equal(fe, d1)
    # tau 0 on both edges = instant = the stateless layer
    st3 = T.DriveState(T.MODE_BOTH, 0, 0, DT); st3.commit(d0)
    assert np.array_equal(st3.peek(d1), d1)
    # rise instant, fall slow: switching on is immediate, switching off lingers
    st4 = T.DriveState(T.MODE_BOTH, 0, 200, DT); st4.commit(np.zeros((2, 2)))
    assert np.array_equal(st4.peek(np.ones((2, 2))), np.ones((2, 2)))
    st4.commit(np.ones((2, 2)))
    lingering = st4.peek(np.zeros((2, 2)))
    assert np.all(lingering > 0.9) and np.allclose(lingering, 1.0 - st4.alpha_fall)
    st4.reset()
    assert st4.state is None and st4.frames == 0
    # pipeline delay: the filter is fed the committed instantaneous map n frames ago (pan_sim's delay(n) law); until the
    # ring holds n maps the current one is used; delay + tau combine (delay first, then the first-order response)
    maps = [rng.uniform(0, 1, (3, 3)) for _ in range(5)]
    dl = T.DriveState(T.MODE_BOTH, 0, 0, DT, delay_frames=2)
    assert np.array_equal(dl.peek(maps[0]), maps[0]); dl.commit(maps[0])
    assert np.array_equal(dl.peek(maps[1]), maps[1]); dl.commit(maps[1])          # ring has 2 maps only after this commit
    assert np.array_equal(dl.peek(maps[2]), maps[0]); dl.commit(maps[2])
    assert np.array_equal(dl.peek(maps[3]), maps[1]); dl.commit(maps[3])
    ft, fe = dl.fields(maps[4]); assert np.array_equal(ft, maps[2]) and np.array_equal(fe, maps[2])
    assert dl.settle_frames() == 2
    dl2 = T.DriveState(T.MODE_BOTH, 100, 100, DT, delay_frames=1); dl2.commit(maps[0]); dl2.commit(maps[1])
    want = dl2.state + np.where(maps[1] > dl2.state, dl2.alpha_rise, dl2.alpha_fall) * (maps[1] - dl2.state)
    assert np.allclose(dl2.peek(maps[2]), want)                                    # fed maps[1], not maps[2]
    with pytest.raises(ValueError):
        T.DriveState(T.MODE_BOTH, 0, 0, DT, delay_frames=4)
    # the GPU-order ring mirrors it (float32)
    from dlc.fald.gpuemu import GpuDriveState
    g = GpuDriveState(T.MODE_BOTH, 100, 100, DT, delay_frames=2); s = T.DriveState(T.MODE_BOTH, 100, 100, DT, delay_frames=2)
    for mp in maps:
        gt, _ = g.pair(mp); st_, _ = s.fields(mp)
        assert np.allclose(gt, st_, atol=1e-6)
        g.commit(mp); s.commit(mp)
        assert np.allclose(g.state, s.state, atol=1e-6) and len(g.ring) == len(s.hist)


# ------------------------------------------------------------------------------------------ the inverse with a state
def _small_model():
    # 960x540 frame at scale 5 -> 192x108 reduced px; 12x12 cells of 80x45 px (the ProArt pitch). Default gain
    # low-pass (0.35 cells): a panel file cannot carry 0 (the loader reads word 28 == 0 as "use the default").
    return FaldModel(FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75,
                                est_phase_px=-20.6, tmin=1.5e-3))


def _frames(m, bright=True):
    p = m.p
    img = np.full((3, m.h, m.w), 5.0)
    if bright:
        # a 2x2-cell white block, cell aligned (cells 5-6 x 5-6)
        y0, y1 = 5 * m.ch, 7 * m.ch; x0, x1 = 5 * m.cw, 7 * m.cw
        img[:, y0:y1, x0:x1] = p.white_nits
    return img


def test_sequence_first_frame_equals_stateless_and_converges():
    m = _small_model()
    img = _frames(m)
    ref = correct_image(m, img)
    st = T.DriveState(T.MODE_BOTH, tau_rise_ms=2 * DT, tau_fall_ms=2 * DT, dt_ms=DT)
    seq = T.correct_sequence(m, [img] * 3, st)
    assert np.array_equal(seq[0]["req"], ref["req"])            # no state yet: the stateless result, bit for bit
    assert np.array_equal(seq[0]["drives_state"], ref["drives"])
    # A static frame: the state holds the committed (round-1, corrected-frame) drives, so every frame adds one more
    # round of the inverse and mode 1 settles close to the self-consistent drives (stat of the corrected frame == the
    # drives it was corrected with; exact only in the limit a -> 0 because round 0 is always fed the raw frame's
    # drives). On THIS unfitted 12x12 test model that moves the output 11.7 -> 1.4 nits from the 8-iteration result;
    # on the fitted PA32UCXR model the stateless and settled outputs agree to <= 0.1 % (design review 2026-09-17), so
    # no accuracy claim follows from this - it is a convergence check. Mode 2 has a rest BIAS instead (temporal.py).
    seq = T.correct_sequence(m, [img] * 10, T.DriveState(T.MODE_BOTH, tau_rise_ms=2 * DT, tau_fall_ms=2 * DT, dt_ms=DT))
    resid = [float(np.abs(r["drives"] - r["drives_state"]).max()) for r in seq]      # |stat(corrected) - state|
    assert resid[0] == 0.0 and all(r2 < r1 for r1, r2 in zip(resid[1:], resid[2:])) and resid[-1] < 1e-4
    fixed = correct_image(m, img, iters=8)
    gap_stateless = float(np.abs(ref["req"] - fixed["req"]).max())
    gap_settled = float(np.abs(seq[-1]["req"] - fixed["req"]).max())
    assert gap_settled < 0.25 * gap_stateless
    # a step: dark -> bright. The state crossfades from the dark drives toward the bright ones; after 5 tau the
    # corrected frame is the stateless bright result to < 1e-3 of the correction (e^-5 of the step)
    dark = _frames(m, bright=False)
    st = T.DriveState(T.MODE_BOTH, tau_rise_ms=2 * DT, tau_fall_ms=2 * DT, dt_ms=DT)
    seq = T.correct_sequence(m, [dark] + [img] * 12, st)
    a = st.alpha_rise
    d_dark, d_bright = seq[0]["drives"], ref["drives"]
    # frame 1 (first bright frame): both rounds saw state_dark + a (d - state_dark); the committed state is that of the
    # last round's instantaneous drives d1 -> strictly between the dark map and d1 where d1 rose (d1 itself is not the
    # stateless bright map: the lagging state changes the corrected frame it is measured on)
    s1, d1 = seq[1]["drives_state"], seq[1]["drives"]
    rose = d1 > d_dark + 1e-6
    assert np.all(s1[rose] > d_dark[rose]) and np.all(s1[rose] < d1[rose])
    assert np.allclose(s1[rose], d_dark[rose] + a * (d1[rose] - d_dark[rose]))
    block = np.zeros_like(d_dark, dtype=bool); block[5:7, 5:7] = True
    assert np.all(s1[block] < 0.5) and np.all(d_bright[block] > 0.8)          # the block's own cells: a third of the way
    # the sequence approaches the many-iteration result of the inverse (not the stateless two-round output, see above):
    # the gap to the 8-iteration result shrinks monotonically and ends far below the correction it applies (the pure
    # inverse itself has a period-2 limit cycle of 3e-3 drive in the halo cells on this fixture; the state damps it)
    fixed = correct_image(m, img, iters=8)
    gap = [float(np.abs(r["req"] - fixed["req"]).max()) for r in seq[1:]]
    assert gap[0] > 1.0 and all(g2 <= g1 + 1e-9 for g1, g2 in zip(gap, gap[1:]))   # monotone approach
    assert gap[-1] < 0.02 * float(np.abs(fixed["req"] - img).max())


def test_true_only_mode_keeps_the_estimate_instantaneous():
    m = _small_model()
    img = _frames(m); dark = _frames(m, bright=False)
    st = T.DriveState(T.MODE_TRUE_ONLY, tau_rise_ms=4 * DT, tau_fall_ms=4 * DT, dt_ms=DT)
    seq = T.correct_sequence(m, [dark, img], st)
    # B_est from the instantaneous drives, B_true from the lagging state -> gain = B_est/B_true ABOVE the stateless one
    # inside the block (the LEDs are modelled as still rising, the LCD opening already computed for full drive)
    ref = correct_image(m, img)
    y0, y1 = 5 * m.ch, 7 * m.ch; x0, x1 = 5 * m.cw, 7 * m.cw
    assert np.mean(seq[1]["gain"][y0:y1, x0:x1]) > np.mean(ref["gain"][y0:y1, x0:x1]) + 0.05
    # the same sequence in mode both is self-consistent: the gain inside the block stays close to the stateless one
    st2 = T.DriveState(T.MODE_BOTH, tau_rise_ms=4 * DT, tau_fall_ms=4 * DT, dt_ms=DT)
    seq2 = T.correct_sequence(m, [dark, img], st2)
    assert abs(np.mean(seq2[1]["gain"][y0:y1, x0:x1]) - np.mean(ref["gain"][y0:y1, x0:x1])) < 0.05


def test_model_backlights_accepts_a_separate_estimate_drive_map():
    m = _small_model()
    d = np.zeros((12, 12)); d[5:7, 5:7] = 1.0
    d2 = np.zeros((12, 12)); d2[5:7, 5:7] = 0.5
    bt, be = m.backlights(d)
    bt2, be2 = m.backlights(d, d2)
    assert np.array_equal(bt, bt2) and not np.allclose(be, be2) and np.allclose(be2, 0.5 * be)


# ------------------------------------------------------------------------------------------ GPU-order emulation
def test_gpu_emulation_of_the_temporal_pass_matches_the_reference(tmp_path):
    """The numpy emulator runs the passes in the shader's order on a panel file written by the exporter; its float32
    temporal step must match DriveState (float64) and, on cell-aligned content, the whole sequence's drive maps."""
    from dlc.fald.export import export_panel_params
    from dlc.fald.gpuemu import Emu, GpuDriveState
    from dlc.fald.panelfile import read_panel_file
    m = _small_model()
    export_panel_params(m, tmp_path / "small.bin")
    o = read_panel_file(tmp_path / "small.bin")
    assert o["cols"] == 12 and o["cellW"] == 80 and o["cellH"] == 45 and o["transfer"] == 0
    emu = Emu(o, width=960, height=540)
    # frames in scRGB (PQ transfer: as-if-white nits = rec2020 * 80; grey/white map to themselves through the matrix)
    def scrgb(img_nits):
        return np.ascontiguousarray(np.repeat(np.repeat(img_nits, 5, axis=1), 5, axis=2).transpose(1, 2, 0) / 80.0)
    dark, bright = _frames(m, False), _frames(m, True)
    frames_gpu = [scrgb(dark), scrgb(bright), scrgb(bright), scrgb(dark)]
    frames_ref = [dark, bright, bright, dark]
    g = GpuDriveState(T.MODE_BOTH, 3 * DT, 1.5 * DT, DT)
    s = T.DriveState(T.MODE_BOTH, 3 * DT, 1.5 * DT, DT)
    assert abs(float(g.alpha_rise) - s.alpha_rise) < 1e-6 and abs(float(g.alpha_fall) - s.alpha_fall) < 1e-6
    ref = T.correct_sequence(m, frames_ref, s)
    s_on_emu = T.DriveState(T.MODE_BOTH, 3 * DT, 1.5 * DT, DT)                # the reference formula on the GPU's own drives
    for k, (f_gpu, r) in enumerate(zip(frames_gpu, ref)):
        out = emu.run(f_gpu, fp16_out=False, temporal=g)
        s_on_emu.commit(out["drive1"])
        # the temporal step itself is exact (float32 vs float64 of the same formula, same order)
        assert np.allclose(g.state, s_on_emu.state, atol=1e-6)
        assert np.allclose(out["drive_true"], out["drive_est"])              # mode both: one map for both kernels
        # the two pipelines (GPU order at full resolution vs the model at 1/5) agree to a known, PRE-EXISTING
        # discretisation gap (fidelity review 2026-09-17, review_fidelity/g_attrib.py): the GPU samples the fields at
        # full-resolution pixels and interpolates the GAIN from the 8/cell fine grid, the model computes the gain per
        # reduced pixel from interpolated fields; the drive-curve LUT and the blur contribute < 1e-8 (the blur even
        # hides part of the gap). Frame 0 (no state yet) is exact on the drives; bright frames differ ~1.5 % inside
        # the block. The temporal step itself is checked exactly above.
        assert np.allclose(out["drive1"], r["drives"], atol=1e-6 if k == 0 else 0.02)
        assert np.allclose(g.state, r["drives_state"], atol=1e-6 if k == 0 else 0.01)
    # the pure temporal arithmetic (float32 vs float64) on arbitrary maps
    rng = np.random.default_rng(3)
    g2 = GpuDriveState(T.MODE_TRUE_ONLY, 120.0, 30.0, DT); s2 = T.DriveState(T.MODE_TRUE_ONLY, 120.0, 30.0, DT)
    for _ in range(6):
        d = rng.uniform(0, 1, (12, 12))
        gt, ge = g2.pair(d); st_, se = s2.fields(d)
        assert np.allclose(gt, st_, atol=1e-6) and np.array_equal(ge, d.astype(np.float32)) and np.array_equal(se, d)
        g2.commit(d); s2.commit(d)
        assert np.allclose(g2.state, s2.state, atol=1e-6)
    # mode off through the emulator = the stateless run, bit for bit
    g3 = GpuDriveState(T.MODE_OFF, 100.0, 100.0, DT)
    a = emu.run(frames_gpu[1], fp16_out=False)
    b = emu.run(frames_gpu[1], fp16_out=False, temporal=g3)
    assert np.array_equal(a["out"], b["out"]) and g3.state is None


def test_panel_file_reader_refuses_what_the_cpp_loader_refuses(tmp_path):
    """panelfile.read_panel_file mirrors LoadFaldPanelParams' FLD2 refusals (fidelity review 2026-09-17)."""
    import struct
    from dlc.fald.export import export_panel_params
    from dlc.fald.panelfile import read_panel_file
    m = _small_model()
    export_panel_params(m, tmp_path / "base.bin")
    base = bytearray((tmp_path / "base.bin").read_bytes())
    assert struct.unpack_from("<I", base, 0)[0] == 0x464C4431                  # FLD1: 32-word header

    def fld2(words: dict) -> bytes:
        # promote to FLD2: insert 8 words (32-39) after the 32-word header
        hdr = bytearray(base[:128]); hdr[0:4] = struct.pack("<I", 0x464C4432)
        extra = [0] * 8
        for i, (fmt, v) in words.items():
            extra[i - 32] = v if fmt == "I" else struct.unpack("<I", struct.pack("<f", v))[0]
        return bytes(hdr) + struct.pack("<8I", *extra) + bytes(base[128:])
    w = struct.unpack_from("<3f", base, 16 * 4)
    ok = {32: ("f", 1.0), 33: ("f", 1.0), 34: ("f", 1.0), 35: ("I", 1)}
    (tmp_path / "ok.bin").write_bytes(fld2(ok))
    assert read_panel_file(tmp_path / "ok.bin")["hasPedColour"] is True
    bad = {"implausible pedestal chroma gain": {**ok, 36: ("f", 200.0)},
           "implausible pedestal chroma fade words": {**ok, 36: ("f", 1.0), 37: ("f", 5.0), 38: ("f", 0.0)},
           "implausible pedestal colour words": {**ok, 32: ("f", 9.0)}}
    bad["implausible pedestal colour words (mode)"] = {**ok, 35: ("I", 7)}
    for name, words in bad.items():
        (tmp_path / "bad.bin").write_bytes(fld2(words))
        with pytest.raises(ValueError, match=name.split(" (")[0]):
            read_panel_file(tmp_path / "bad.bin")


@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_hlsl_temporal_pass_mirrors_the_reference():
    src = _SHADER.read_text(encoding="utf-8")
    body = re.search(r'g_faldTemporalSource = R"\((.*?)\)";', src, re.S).group(1)
    assert "if (tempInit != 0u) { driveFiltOut[id.xy] = d; return; }" in body           # no state: copy (DriveState.peek)
    assert "float a = (d > s) ? tempAlphaRise : tempAlphaFall;" in body                  # per-edge blend
    assert "driveFiltOut[id.xy] = s + a * (d - s);" in body
    assert "stateTex.Load" in body and "driveTex.Load" in body
    conv = re.search(r'g_faldConvSource = R"\((.*?)\)";', src, re.S).group(1)
    assert "ConvRow(driveTex, kTrue," in conv and "ConvRow(driveEstTex, kEst," in conv    # true / est kernels on their own maps
    assert "ConvRow(driveTex, kEst" not in conv and "ConvRow(driveEstTex, kTrue" not in conv
    assert "register(t10)" in src and "register(t11)" in src


# ------------------------------------------------------------------------------------------ measuring the LED law
def test_step_response_fit_recovers_tau_and_flags_an_instant_panel():
    rng = np.random.default_rng(7)
    t = np.arange(-0.05, 0.6, 1.0 / 240.0)                      # a 240-fps phone clip, step at t = 0
    y = T.first_order_step(t, 0.08, 10.0, 12.0) + rng.normal(0, 0.02, t.size)
    fit = T.fit_step_response(t, y)
    assert abs(fit["tau"] - 0.08) < 0.016 and abs(fit["t0"]) < 0.01
    assert abs(fit["y0"] - 10.0) < 0.05 and abs(fit["y1"] - 12.0) < 0.05
    assert fit["instant_rms"] > 3 * fit["rms"]                 # a lagging panel beats the instant fit clearly
    y_inst = np.where(t >= 0, 12.0, 10.0) + rng.normal(0, 0.02, t.size)
    fit2 = T.fit_step_response(t, y_inst)
    assert fit2["tau"] < 0.005 and fit2["instant_rms"] < 1.2 * fit2["rms"] + 1e-9
    with pytest.raises(ValueError):
        T.fit_step_response(t[:3], y[:3])
