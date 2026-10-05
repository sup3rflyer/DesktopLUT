"""dlc.fald.parity_bind: the epoch parity convention, the offset from status + truth samples, Windows' counter
continuity and the calibration record's refusals (review 2026-10-05: a bare P_dxgi could be re-bound after a standby /
power cycle that re-rolled the panel)."""
import pytest

from dlc.fald.parity_bind import check_record, counter_continuous, epoch_parity, offsets_from
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


def test_offsets_from_samples_nearest_vblank_and_phase():
    base = 7_000_000_000
    truth = [TruthSample(int(base + i * P), 400_000 + i) for i in range(0, 600, 15)]
    samples = [{"published": True, "count_anchor": 100 + i, "qpc_anchor": int(base + i * P) + 300} for i in range(10, 580, 30)]
    samples.append({"published": True, "count_anchor": 100 + 581, "qpc_anchor": int(base + 581 * P) - 120})   # a hair early
    samples.append({"published": False})
    offs = offsets_from(samples, truth, P, FREQ)
    assert len(offs) == 20 and {o for o, _ in offs} == {100 - 400_000}
    assert min(ph for _, ph in offs) < 0.0 < max(ph for _, ph in offs) < 0.01


def test_counter_continuity():
    assert counter_continuous(1000, 0, 1000 + 216_000, int(216_000 * P), P)                 # an hour, running
    assert not counter_continuous(1000, 0, 1000 + 100_000, int(216_000 * P), P)             # paused (standby)
    assert not counter_continuous(500_000, 0, 1_000, int(216_000 * P), P)                   # reset (modeset)
    assert counter_continuous(1000, 0, 1000 + 216_001, int(216_000 * P), P)                 # 1 count over an hour: < 5 ppm + .25
    assert not counter_continuous(1000, 0, 1000 + 3, int(1.5 * P), P)                       # off by 1.5 in seconds


def test_record_refusals():
    rec = {"p_dxgi": 1, "hardware_id": "AUS322A", "mode": "HDR", "boot_wall": 1000.0, "written_wall": 50_000.0}
    mon = {"hardware_id": "AUS322A", "hdr_active": True}
    assert check_record(rec, mon, "HDR", 1010.0, 50_000.0 + 600, 0.5) is None
    assert "another panel" in check_record(rec, {**mon, "hardware_id": "BNQ802E"}, "HDR", 1010.0, 50_600.0, 0.5)
    assert "live HDR" in check_record(rec, {**mon, "hdr_active": False}, "HDR", 1010.0, 50_600.0, 0.5)
    assert "measured in" in check_record(rec, mon, "SDR", 1010.0, 50_600.0, 0.5)
    assert "another boot" in check_record(rec, mon, "HDR", 9000.0, 50_600.0, 0.5)
    assert "old" in check_record(rec, mon, "HDR", 1010.0, 50_000.0 + 2 * 3600, 0.5)
    assert check_record(rec, mon, "HDR", 1010.0, 50_000.0 + 2 * 3600, 3.0) is None           # the owner raised the limit
    assert "no P_dxgi" in check_record({**rec, "p_dxgi": None}, mon, "HDR", 1010.0, 50_600.0, 0.5)
