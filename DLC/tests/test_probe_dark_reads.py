"""probe_hw_common: a patch that should be lit and reads at / below the meter floor (2026-11-14: a sleeping display
recorded eight mid-greys as Y = 0.0 with status ok) is an anomaly, three in a row stop the probe; legitimately dark
patches are never judged. Simulated session only (virtual clock, synthetic panel, the file-backed mock pipe)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import probe_hw_common as hc  # noqa: E402


class SleepyPanel(hc.SimPanel):
    """Reads exactly 0 while ``asleep`` (the display off), else the synthetic panel."""

    asleep: bool = True

    def read(self, shapes, meter_norm, since_paint_s):
        if self.asleep:
            return (0.0, 0.0, 0.0)
        return super().read(shapes, meter_norm, since_paint_s)


def _session(tmp_path: Path, mode: str = "HDR", bit_depth: int = 10):
    args = SimpleNamespace(monitor=0, mode=mode, simulate=True, tag="", run=str(tmp_path / f"run_{mode}"),
                           bit_depth=bit_depth, present_dwell_s=0.25, settle_max_s=60.0, settle_max_reads=200,
                           settle_rel_tol=hc.SETTLE_REL_TOL, settle_abs_tol=hc.SETTLE_ABS_TOL, settle_min_span_s=None,
                           idle_between="dim", dogegen_server=None, profile=None)
    s = hc.ProbeSession(args, "dark_test")
    g = hc.Geometry(3840, 2160, (1920, 1080))
    panel = SleepyPanel("fald", mode, bit_depth, g)
    s.open_meter(g, panel=panel)
    return s, panel


def _patch(code, name):
    return hc.Patch(name, "t", hc.full(hc.grey(code)), hc.grey(code))


def test_lit_patches_reading_zero_raise_display_dark_after_three(tmp_path):
    s, panel = _session(tmp_path)
    mid = hc.pq_code(100.0, 10)
    s.measure_patch(_patch(mid, "a"))
    s.measure_patch(_patch(mid, "b"))
    assert [a["kind"] for a in s.anomalies].count("dark_read") == 2
    with pytest.raises(hc.DisplayDark) as ei:
        s.measure_patch(_patch(mid, "c"))
    assert "asleep" in str(ei.value)
    assert isinstance(ei.value, hc.MeterDown)                   # every runner that stops on a dead meter stops here


def test_dark_patches_are_never_judged_and_a_lit_read_resets_the_streak(tmp_path):
    s, panel = _session(tmp_path)
    for code in (0, 8, hc.pq_code(0.001, 10)):                 # black / near-black PQ: expected light ~0
        s.measure_patch(_patch(code, f"k{code}"))
    assert not [a for a in s.anomalies if a["kind"] == "dark_read"]
    mid = hc.pq_code(100.0, 10)
    s.measure_patch(_patch(mid, "a"))
    s.measure_patch(_patch(mid, "b"))
    panel.asleep = False                                        # the display wakes
    s.measure_patch(_patch(mid, "c"))
    assert s.dark_streak == 0
    panel.asleep = True
    s.measure_patch(_patch(mid, "d"))                           # one dark read: anomaly, no stop
    assert s.dark_streak == 1


def test_sdr_codes_0_to_20_read_about_zero_without_an_anomaly(tmp_path):
    s, _ = _session(tmp_path, mode="SDR", bit_depth=10)
    for code in (0, 10, 20):
        s.measure_patch(_patch(code, f"s{code}"))
    assert not [a for a in s.anomalies if a["kind"] == "dark_read"]
    with pytest.raises(hc.DisplayDark):
        for code in (514, 642, 770):                            # mid greys at 120 nit white: ~26 / 42 / 62 nit
            s.measure_patch(_patch(code, f"m{code}"))


def test_expected_light_uses_the_luminance_weights():
    s = SimpleNamespace(mode="HDR", args=SimpleNamespace(bit_depth=10))
    blue = hc.ProbeSession.expected_y(s, (0, 0, hc.pq_code(100.0, 10)))
    grey = hc.ProbeSession.expected_y(s, hc.grey(hc.pq_code(100.0, 10)))
    assert grey == pytest.approx(100.0, rel=0.01) and blue == pytest.approx(100.0 * hc.LUMA_2020[2], rel=0.01)
