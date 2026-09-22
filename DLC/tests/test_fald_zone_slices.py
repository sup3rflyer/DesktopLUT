"""The GPU-order twin of the zone sweeps' summation (gpuemu.Emu.zone_pow_sum) after work guide C14: zones of more than
ZONE_SLICE_PX pixels are summed per slice (one 256-thread group each), then the slices are folded by one more group.
Checks: a one-slice zone keeps the order from before C14 exactly; a many-slice zone matches an independent loop over the
same order bit for bit and the float64 sum to float32 precision."""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dlc.fald.gpuemu import ZONE_SLICE_PX, Emu  # noqa: E402

GAMMA = np.float32(0.62)


def _emu(rows, cols, ch, cw):
    ns = SimpleNamespace(rows=rows, cols=cols, ch=ch, cw=cw, meanGamma=GAMMA, _group_sum=Emu._group_sum)
    ns._pow32 = lambda v: Emu._pow32(ns, v)                              # zone_pow_sum = zone_sweep_sum(_pow32(...)) (C16)
    ns.zone_sweep_sum = lambda vals: Emu.zone_sweep_sum(ns, vals)
    return ns


def _blocks(rows, cols, ch, cw, seed):
    rng = np.random.default_rng(seed)
    v = rng.exponential(0.05, size=(rows, ch, cols, cw)).astype(np.float32)
    v[rng.random(v.shape) < 0.2] = 0.0                                    # pixels with mc = 0 add nothing
    return v


def _pow(v):
    pos = v > 0
    return np.where(pos, np.exp(GAMMA * np.log(np.where(pos, v, np.float32(1)), dtype=np.float32), dtype=np.float32),
                    np.float32(0)).astype(np.float32)


def _group(vals):                                                        # one group, written out as the HLSL reads
    part = [np.float32(0)] * 256
    for i, x in enumerate(vals):
        part[i % 256] = np.float32(part[i % 256] + x)
    stride = 128
    while stride:
        for t in range(stride):
            part[t] = np.float32(part[t] + part[t + stride])
        stride //= 2
    return part[0]


def test_one_slice_zone_keeps_the_order_from_before():
    rows, cols, ch, cw = 3, 4, 45, 80                                    # 3600 px: one slice
    assert ch * cw <= ZONE_SLICE_PX
    b = _blocks(rows, cols, ch, cw, 1)
    got = Emu.zone_pow_sum(_emu(rows, cols, ch, cw), b)
    pw = _pow(b).transpose(0, 2, 1, 3).reshape(rows, cols, -1)
    part = np.zeros((rows, cols, 256), dtype=np.float32)                 # the pre-C14 emulation, verbatim
    for j in range(0, ch * cw, 256):
        seg = pw[:, :, j: j + 256]
        part[:, :, : seg.shape[2]] += seg
    stride = 128
    while stride > 0:
        part[:, :, :stride] += part[:, :, stride: 2 * stride]
        stride >>= 1
    assert np.array_equal(got, part[:, :, 0])


@pytest.mark.parametrize("ch, cw", [(100, 64), (127, 128), (540, 480)])   # 2, 4 and 64 slices, last slice partial
def test_many_slice_zone_follows_the_slice_order(ch, cw):
    rows, cols = 2, 2
    b = _blocks(rows, cols, ch, cw, 2)
    got = Emu.zone_pow_sum(_emu(rows, cols, ch, cw), b)
    pw = _pow(b).transpose(0, 2, 1, 3).reshape(rows, cols, -1)
    for r in range(rows):
        for c in range(cols):
            z = pw[r, c]
            slices = [_group(z[s: s + ZONE_SLICE_PX]) for s in range(0, z.size, ZONE_SLICE_PX)]
            assert len(slices) > 1
            assert got[r, c] == _group(slices)
            exact = float(np.sum(z.astype(np.float64)))
            assert abs(float(got[r, c]) - exact) <= 1e-5 * exact
