"""Tests for the EOTF + luminance-dependent white sweep (dlc.characterize.eotf_white_sweep).

Drives the sweep with a synthetic measure that models a uniform sub-peak undershoot, a hard
peak clip, and a level-dependent white — the PA32UCXR-like HDR behaviour — and asserts the DIP
recovers the calibratable gain (excluding the clip) and the white map.
"""

from __future__ import annotations

import pytest

from dlc.characterize import (CharacterizeConfig, _Characterizer, _NdjsonWriter,
                              run_characterization)
from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasurePatch, Reading, SyntheticPanel


def _Yxy_to_xyz(Y: float, x: float, y: float):
    if y <= 0:
        return (0.0, 0.0, 0.0)
    return (Y * x / y, Y, Y * (1.0 - x - y) / y)


class _Clock:
    def __init__(self, dt: float = 0.1) -> None:
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        v = self.t
        self.t += self.dt
        return v


def _characterizer(measure, transfer, **cfg) -> _Characterizer:
    return _Characterizer(measure=measure, transfer=transfer,
                          config=CharacterizeConfig(**cfg), ndjson=_NdjsonWriter(None),
                          events=None, clock=_Clock(), injected_clock=True, cold_channel=None)


def test_eotf_sweep_recovers_subpeak_undershoot_and_excludes_clip():
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    PANEL_PEAK = 100.0   # physical peak BELOW the brightest requested target → the top level clips

    def measure(patch: MeasurePatch) -> Reading:
        cv = patch.rgb[0]
        target = transfer.cv_to_nits(cv)
        measured = min(target * 0.94, PANEL_PEAK)        # uniform -6% gain, then a hard peak clip
        x = 0.320 - 0.020 * (cv / transfer.max_cv)       # white drifts cooler as level rises
        return Reading(xyz=_Yxy_to_xyz(measured, x, 0.330))

    res = _characterizer(measure, transfer,
                         eotf_levels=(0.2, 0.5, 0.8, 1.0), eotf_reads=2).eotf_white_sweep()

    # The sub-peak undershoot is recovered (~-6%); the clipped top level (request 120 > peak 100)
    # is EXCLUDED, so it does not drag the gain toward its -0.17 clip error.
    assert res["eotf_undershoot"] == pytest.approx(-0.06, abs=0.01)
    wvl = res["white_vs_luminance"]
    assert wvl is not None and len(wvl) == 4 and all(len(row) == 3 for row in wvl)
    assert [r[0] for r in wvl] == sorted(r[0] for r in wvl)          # ascending by nits
    # The clipped top level IS still recorded in the white map — at the PHYSICAL peak (~100), not 120.
    assert wvl[-1][0] == pytest.approx(100.0, abs=1.0)
    # White genuinely varies across the range (the reason the field exists).
    assert wvl[0][1] != wvl[-1][1]


def test_eotf_sweep_skipped_when_reads_zero():
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    res = _characterizer(lambda p: Reading(xyz=(1.0, 1.0, 1.0)), transfer,
                         eotf_reads=0).eotf_white_sweep()
    assert res["skipped"] is True
    assert res["eotf_undershoot"] is None and res["white_vs_luminance"] is None


def test_run_characterization_populates_eotf_dip_fields():
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0)
    cfg = CharacterizeConfig(noise_levels=(1.0, 0.2), noise_reads=3, black_reads=2,
                             primary_reads=2, warmup_observe_reads=10, warmup_max_minutes=0,
                             creep_reads=3, eotf_levels=(0.3, 0.6, 1.0), eotf_reads=2)
    res = run_characterization(measure=panel, transfer=transfer, config=cfg, clock=_Clock())
    assert res.dip.white_vs_luminance is not None and len(res.dip.white_vs_luminance) == 3
    assert [r[0] for r in res.dip.white_vs_luminance] == sorted(r[0] for r in res.dip.white_vs_luminance)
    assert res.dip.eotf_undershoot is not None
    # A faithful panel renders close to the requested luminance → small undershoot.
    assert abs(res.dip.eotf_undershoot) < 0.2
    # And it survives a store round-trip (the fields serialize/deserialize).
    from dlc.dip import DisplayInstrumentProfile
    rt = DisplayInstrumentProfile.from_dict(res.dip.as_dict())
    assert rt.eotf_undershoot == res.dip.eotf_undershoot
    assert rt.white_vs_luminance == res.dip.white_vs_luminance
