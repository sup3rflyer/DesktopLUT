"""The GPU-order twin's model of the device's bilinear sampler (gpuemu.sampler_truncates, Emu(subtexel_bits=8, sampler=...)).

D3D11 weighs a bilinear sample with an 8-bit sub-texel fraction; HOW the fraction is formed was measured on 2026-09-23 (a
standalone probe: R32F / R32G32F / R32G32B32A32F textures of 17 sizes, SampleLevel in pixel and compute shaders, fractions
read back exactly): a hardware GPU (RTX 5090) rounds it to nearest on every axis; the WARP software device — the one the
opt-in replays run — truncates it on x when the texture's width is a power of two and on y when both its dimensions are.
Modelled as rounding everywhere, an 8 x 6 few-zone WARP lattice replayed 106 FP16 steps off at its top zone row
(tests/test_fald_zone_slices_warp.py). The table below is that measurement."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dlc.fald.export import export_panel_params  # noqa: E402
from dlc.fald.gpuemu import SAMPLER_HW, SAMPLER_WARP, Emu, sampler_truncates  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

# (width, height) -> (x truncated, y truncated) on WARP; None = that axis has one texel (nothing to interpolate, not measured)
WARP_MEASURED = {
    (8, 1): (True, None), (1, 8): (None, True), (6, 1): (False, None), (1, 6): (None, False), (12, 1): (False, None),
    (48, 1): (False, None), (64, 1): (True, None), (96, 1): (False, None), (16, 1): (True, None), (1024, 1): (True, None),
    (8, 6): (True, False), (6, 8): (False, False), (64, 48): (True, False), (48, 64): (False, False), (16, 16): (True, True),
    (12, 8): (False, False), (8, 12): (True, False), (32, 3): (True, False), (3, 32): (False, False), (2, 6): (True, False),
    (4, 4): (True, True), (96, 64): (False, False), (384, 384): (False, False), (48, 48): (False, False),
    (128, 32): (True, True), (256, 256): (True, True),
}


def test_sampler_rule_is_the_measured_one():
    for (w, h), (mx, my) in WARP_MEASURED.items():
        tx, ty = sampler_truncates(SAMPLER_WARP, w, h)
        if mx is not None:
            assert tx == mx, (w, h)
        if my is not None:
            assert ty == my, (w, h)
        assert sampler_truncates(SAMPLER_HW, w, h) == (False, False), (w, h)     # a hardware GPU rounds everywhere
    with pytest.raises(ValueError):
        sampler_truncates("nearest", 8, 6)


@pytest.fixture(scope="module")
def few_zone_panel(tmp_path_factory):
    """8 x 6 zones at 960 x 540 (the few-zone WARP lattice): zone textures 8 x 6, fine grid 64 x 48, curve LUT 1024 x 1."""
    p = FaldParams(width=960, height=540, cols=8, rows=6, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6, tmin=1.5e-3)
    path = tmp_path_factory.mktemp("fald_sampler") / "panel.bin"
    export_panel_params(FaldModel(p), path)
    o = read_panel_file(path)
    assert (o["cols"], o["rows"], o["sub"], o["curveN"]) == (8, 6, 8, 1024)
    return o


def test_the_twins_sample_tables_follow_the_device(few_zone_panel):
    o = few_zone_panel
    exact = Emu(o, width=960, height=540)
    hw = Emu(o, width=960, height=540, subtexel_bits=8, sampler=SAMPLER_HW)
    warp = Emu(o, width=960, height=540, subtexel_bits=8, sampler=SAMPLER_WARP)
    # zone texture x (8 wide): pixel 66 sits at texel 66.5 / 120 - 0.5 = 0.0542 = 13.87 / 256 (the gap's worst pixel)
    assert exact.zx[2][66] * 256 == pytest.approx(13.8667, abs=1e-3)
    assert hw.zx[2][66] * 256 == 14 and warp.zx[2][66] * 256 == 13
    # zone texture y (6 high): rounded on both devices (125.5 / 90 - 0.5 = 0.8944 = 228.98 / 256)
    assert hw.zy[2][125] * 256 == 229 and warp.zy[2][125] * 256 == 229
    # fine grid x (64 wide): 66.5 / 15 - 0.5 = 3.9333 = 3 + 238.93 / 256; y (48 high) rounds on both
    assert hw.bx[2][66] * 256 == 239 and warp.bx[2][66] * 256 == 238 and (hw.bx[0][66], hw.bx[1][66]) == (3, 4)
    assert np.array_equal(hw.by[2], warp.by[2]) and np.array_equal(hw.zy[2], warp.zy[2])
    # every fraction: hardware = nearest, WARP = truncated on the power-of-two widths, both within half / one step of exact
    for e, a, b, trunc in ((exact.zx, hw.zx, warp.zx, True), (exact.bx, hw.bx, warp.bx, True), (exact.by, hw.by, warp.by, False)):
        assert np.array_equal(a[2], np.round(e[2] * 256) / 256)
        assert np.array_equal(b[2], (np.floor(e[2] * 256) if trunc else np.round(e[2] * 256)) / 256)


def test_the_drive_curve_lut_is_sampled_like_the_device(few_zone_panel):
    o = few_zone_panel
    hw = Emu(o, width=960, height=540, subtexel_bits=8, sampler=SAMPLER_HW)
    warp = Emu(o, width=960, height=540, subtexel_bits=8, sampler=SAMPLER_WARP)
    exact = Emu(o, width=960, height=540)
    assert not hw.curve_trunc and warp.curve_trunc                    # 1024 x 1: WARP truncates
    stat = np.geomspace(0.6, 1800.0, 4001).astype(np.float32)
    n, lmin, lmax = o["curveN"], float(o["curveLogMin"]), float(o["curveLogMax"])
    u = np.clip((np.log(np.maximum(stat, np.float32(1e-3))) - np.float32(lmin)) / np.float32(lmax - lmin), 0, 1).astype(np.float64)
    c = np.asarray(o["curve"], dtype=np.float64)
    f32 = np.float32
    # the device's texel coordinate: the HLSL's float32 uv = (u (N - 1) + 0.5) / N, the sampler's N uv - 0.5
    x_dev = (((u.astype(f32) * f32(n - 1)).astype(f32) + f32(0.5)) / f32(n)).astype(f32).astype(np.float64) * n - 0.5
    for emu, x, q in ((exact, u * (n - 1), None), (hw, x_dev, np.round), (warp, x_dev, np.floor)):
        i0 = np.floor(x).astype(int); i1 = np.minimum(i0 + 1, n - 1); fr = x - i0
        frq = fr if q is None else q(fr * 256) / 256
        want = (c[i0] * (1 - frq) + c[i1] * frq).astype(np.float32)
        assert np.array_equal(emu.drive_of(stat), want)
    assert (hw.drive_of(stat) != warp.drive_of(stat)).mean() > 0.3   # the two devices really differ on this LUT
    # float32 vs float64 coordinates: where they straddle a 1/256 edge the truncated fraction differs — the device's is x_dev
    edge = np.floor((u * (n - 1)) * 256) != np.floor(x_dev * 256)
    assert edge.any()


def test_a_device_sampler_needs_the_fraction_width(few_zone_panel):
    with pytest.raises(ValueError):
        Emu(few_zone_panel, width=960, height=540, sampler=SAMPLER_WARP)     # WARP's rule without subtexel_bits: refused
    with pytest.raises(ValueError):
        Emu(few_zone_panel, width=960, height=540, subtexel_bits=8, sampler="nearest")
