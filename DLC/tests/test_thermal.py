"""Tests for the closed-loop thermal controller (dlc.thermal)."""

from __future__ import annotations

import pytest

from dlc.drift import normalized_channels
from dlc.engine.patches import Transfer
from dlc.measure_loop import MeasurePatch, Reading, SyntheticPanel, _SRGB_TO_XYZ_D65
from dlc.thermal import ThermalConfig, ThermalController, net_over_gross


# --------------------------------------------------------------------------- helpers
class _Clock:
    """Deterministic monotonic clock; advances a fixed step on each read it is told about."""

    def __init__(self, step: float = 0.85) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.t

    def tick(self) -> None:
        self.t += self.step


def _grey_content(transfer: Transfer, nits=(10, 30, 60, 100, 120)):
    return [(transfer.nits_to_cv(n),) * 3 for n in nits]


def _xyz_for_linear_rgb(r: float, g: float, b: float):
    """Pick an XYZ whose normalized channel balance is (r,g,b) — via the linear sRGB→XYZ
    matrix, so :func:`normalized_channels` round-trips it back to the chosen balance."""
    M = _SRGB_TO_XYZ_D65
    return (M[0][0] * r + M[0][1] * g + M[0][2] * b,
            M[1][0] * r + M[1][1] * g + M[1][2] * b,
            M[2][0] * r + M[2][1] * g + M[2][2] * b)


# --------------------------------------------------------------------------- net/gross
def test_net_over_gross_directional_vs_oscillating():
    # A monotonic ramp is fully directional: net == gross, ratio == 1.
    net, gross, ratio = net_over_gross([0.0, 0.1, 0.2, 0.3])
    assert net == pytest.approx(0.3)
    assert gross == pytest.approx(0.3)
    assert ratio == pytest.approx(1.0)
    # A symmetric oscillation returns to start: net ~0, gross large, ratio ~0.
    net, gross, ratio = net_over_gross([0.0, 0.2, 0.0, 0.2, 0.0])
    assert net == pytest.approx(0.0, abs=1e-9)
    assert gross == pytest.approx(0.8)
    assert ratio == pytest.approx(0.0, abs=1e-9)


def test_net_over_gross_degenerate():
    assert net_over_gross([0.5]) == (0.0, 0.0, None)
    assert net_over_gross([0.5, 0.5])[2] is None  # no motion ⇒ ratio None


# --------------------------------------------------------------------------- controller
def _controller(panel, transfer, **cfg_over):
    clock = _Clock()

    def measure(patch: MeasurePatch) -> Reading:
        clock.tick()
        return panel(patch)

    cfg = ThermalConfig(**{**dict(load_reads_per_block=8, window_blocks=5,
                                  max_blocks=240, drift_floor=0.003), **cfg_over})
    return ThermalController(measure=measure, transfer=transfer,
                             content=_grey_content(transfer), ref_nits=60.0,
                             balance_noise=0.0008, config=cfg, clock=clock)


def test_warming_panel_converges_with_warmin_observed():
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    panel = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    res = _controller(panel, transfer).run()
    assert res.converged, res.flags
    assert res.final_k < 1.1           # k glided back to operating load
    assert res.active_channel == "B"   # blue is the cold/warm-in channel
    assert res.warmin_magnitude > 0.0  # the warm-in was actually observed, not skipped
    assert res.regime in ("convergent", "fluctuating")
    assert res.fluctuation_envelope >= 0.0


def test_self_activating_no_warmin_needs_no_preheat():
    """A panel with no thermal CHROMA drift (cold_blue_gain=1.0 ⇒ the neutral balance never
    moves with temperature) reads in-band immediately, so the loop never soaks and converges
    far sooner than a panel that genuinely warms in — the preheat self-deactivates."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    inert = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=1.0,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    warming = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                             load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    inert_res = _controller(inert, transfer).run()
    warm_res = _controller(warming, transfer).run()
    assert inert_res.converged
    # No warm-in to chase ⇒ converges in far fewer content reads (no soak) than the warming panel.
    assert inert_res.content_reads < warm_res.content_reads
    assert inert_res.warmin_magnitude <= warm_res.warmin_magnitude + 1e-6


def test_wandering_panel_classified_fluctuating():
    """A panel whose neutral balance wanders (large gross, ~0 net) is never directionally
    settling ⇒ the regime is fluctuating, not convergent."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    state = {"n": 0}

    def wander(patch: MeasurePatch) -> Reading:
        if patch.role == "neutral_ref":
            n = state["n"]; state["n"] += 1
            tri = abs((n % 6) - 3) - 1.5          # triangle wave in [-1.5, 1.5]
            blue = 0.90 + 0.03 * tri              # wanders ±0.045 around 0.90, mean-reverting
            return Reading(xyz=_xyz_for_linear_rgb(1.0, 1.0, blue))
        return Reading(xyz=_xyz_for_linear_rgb(1.0, 1.0, 0.90))

    res = _controller(wander, transfer, max_blocks=40).run()
    assert res.regime == "fluctuating"
    assert res.fluctuation_envelope > res.drift_threshold


def test_threshold_self_calibrates_when_balance_noise_absent():
    """With no balance_noise supplied, the controller estimates its drift threshold from the
    within-block reference-read scatter (read noise) and still converges with warm-in observed."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    panel = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0, noise=0.004, seed=11)
    clock = _Clock()

    def measure(patch: MeasurePatch) -> Reading:
        clock.tick(); return panel(patch)

    ctrl = ThermalController(measure=measure, transfer=transfer, content=_grey_content(transfer),
                             ref_nits=60.0, balance_noise=None,   # <- self-calibrate
                             config=ThermalConfig(load_reads_per_block=8, ref_reads=5, max_blocks=240),
                             clock=clock)
    res = ctrl.run()
    assert res.converged, res.flags
    assert res.drift_threshold >= 0.003          # at least the floor
    assert res.active_channel == "B"
    assert res.warmin_magnitude > 0.0


def test_thermal_block_records_are_emitted():
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    panel = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                           load_thermal=True, thermal_rate=0.05, start_temp=0.6)
    rows = []
    clock = _Clock()

    def measure(patch: MeasurePatch) -> Reading:
        clock.tick(); return panel(patch)

    ctrl = ThermalController(measure=measure, transfer=transfer, content=_grey_content(transfer),
                             ref_nits=60.0, balance_noise=0.0008,
                             config=ThermalConfig(load_reads_per_block=6, max_blocks=60),
                             clock=clock, emit=rows.append)
    ctrl.run()
    assert rows and all(r["phase"] == "thermal" for r in rows)
    assert all({"k", "net", "gross", "threshold", "state"} <= set(r) for r in rows)


def test_load_thermal_synthetic_heats_with_load():
    """Sanity: the opt-in SyntheticPanel load model warms faster under brighter load and
    cools under black — the property the controller relies on."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    p = SyntheticPanel(transfer=transfer, load_thermal=True, thermal_rate=0.1, start_temp=0.0)
    white = MeasurePatch(label="w", rgb=(1023,) * 3, signal=(1.0, 1.0, 1.0))
    for _ in range(20):
        p(white)
    hot = p.temp
    assert hot > 0.5                         # bright load warmed it
    black = MeasurePatch(label="k", rgb=(0, 0, 0), signal=(0.0, 0.0, 0.0))
    for _ in range(20):
        p(black)
    assert p.temp < hot                      # black load cooled it back down
