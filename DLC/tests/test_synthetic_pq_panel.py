"""The HDR (PQ/Rec.2020) mode of the deterministic SyntheticPanel — a perfect panel
reads the engine's PQ ideal, undershoot dims luminance by (1+u), and the native peak
clips. The dE_ITP cross-check is engine-tier (numpy/colour, importorskip); the luminance
ratio / clip checks are pure stdlib."""

from __future__ import annotations

import math

import pytest

from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasurePatch, SyntheticPanel

D65 = (0.3127, 0.3290)


def _patch(signal, *, bit_depth=10, max_cv=1023):
    rgb = tuple(round(c * max_cv) for c in signal)
    return MeasurePatch(label="p", rgb=rgb, signal=tuple(signal), bit_depth=bit_depth)


def _perfect_hdr_panel(transfer, **kw):
    # warm + no cold-blue droop ⇒ the panel hits the PQ/Rec.2020 ideal (clean wiring).
    return SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0, **kw)


def test_pq_panel_perfect_reads_match_the_engine_ideal():
    np = pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.engine.model import score_hdr

    transfer = Transfer.pq(bit_depth=10)
    panel = _perfect_hdr_panel(transfer, native_white_nits=1840.0)  # undershoot 0 ⇒ perfect
    signals = [(0.5, 0.5, 0.5), (0.6, 0.5, 0.4), (0.4, 0.4, 0.4), (0.58, 0.2, 0.2)]
    reads = [panel(_patch(s)).xyz for s in signals]

    res = score_hdr(signals, reads, white_xy=D65)
    # Panel (hand-rolled ST.2084 + canonical Rec.2020 NPM) vs engine (colour) — sub-JND.
    assert float(np.max(res["de_itp"])) < 1.0


def test_pq_panel_undershoot_dims_luminance_by_one_plus_u():
    transfer = Transfer.pq()
    perfect = _perfect_hdr_panel(transfer)
    under = _perfect_hdr_panel(transfer, eotf_undershoot=-0.06)
    patch = _patch((0.5, 0.5, 0.5))

    y_perfect = perfect(patch).xyz[1]
    y_under = under(patch).xyz[1]
    assert math.isclose(y_under / y_perfect, 0.94, rel_tol=1e-9)


def test_pq_panel_clips_at_the_native_peak():
    transfer = Transfer.pq()
    panel = _perfect_hdr_panel(transfer, native_white_nits=1000.0)
    # Full white asks for the 10000-nit PQ container; the panel clips to its 1000-nit peak.
    y = panel(_patch((1.0, 1.0, 1.0))).xyz[1]
    assert math.isclose(y, 1000.0, rel_tol=1e-3)


def test_power_transfer_panel_is_unchanged_sdr():
    # A power transfer keeps the original sRGB/γ behaviour: white reads white_nits.
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0, white_nits=120.0)
    y = panel(_patch((1.0, 1.0, 1.0))).xyz[1]
    assert math.isclose(y, 120.0, rel_tol=1e-3)
