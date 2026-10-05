"""dlc.fald.parity_bind: the epoch parity from the panel's parity vs Windows' count and the epoch's count offset, and
the offset from status + truth samples (synthetic)."""
import pytest

from dlc.fald.parity_bind import epoch_parity, offsets_from
from dlc.fald.vblank_soak import TruthSample

P = 166_666.5
FREQ = 10_000_000


def test_epoch_parity_follows_the_hook_convention():
    # the panel ticks at Windows' counts R with (R + p_dxgi) even; the hook's n = R + offset ticks with (n + p) even
    for p_dxgi in (0, 1):
        for offset in (-205347, -291317, 0, 1, 7):
            p = epoch_parity(p_dxgi, offset)
            for R in range(100, 110):
                n = R + offset
                assert ((R + p_dxgi) % 2 == 0) == ((n + p) % 2 == 0)
    with pytest.raises(ValueError):
        epoch_parity(-1, 3)


def test_offsets_from_samples():
    base = 7_000_000_000
    truth = [TruthSample(int(base + i * P), 400_000 + i) for i in range(0, 600, 15)]
    samples = [{"published": True, "count_anchor": 100 + i, "qpc_anchor": int(base + i * P) + 300} for i in range(10, 580, 30)]
    samples.append({"published": False})
    offs = offsets_from(samples, truth, P, FREQ)
    assert len(offs) == 19 and set(offs) == {100 - 400_000}
