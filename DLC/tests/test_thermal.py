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


def test_tau_patches_reported_for_warming_panel_not_inert():
    """The controller estimates a thermal time constant (in content-read units) from the warm-in:
    a panel that genuinely warms in gets a positive tau; an inert panel (no chroma drift) gets
    None (no warm-in to fit) so the patch-ordering rotation keeps its default."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    warming = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                             load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    inert = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=1.0,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    warm_res = _controller(warming, transfer).run()
    inert_res = _controller(inert, transfer).run()
    assert warm_res.tau_patches is not None and warm_res.tau_patches >= 1
    assert warm_res.digest["tau_patches"] == warm_res.tau_patches
    assert inert_res.tau_patches is None


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
        # ~60-nit luminance so it matches the commanded reference (sanity guard stays quiet);
        # only the BLUE BALANCE wanders.
        if patch.role == "neutral_ref":
            n = state["n"]; state["n"] += 1
            tri = abs((n % 6) - 3) - 1.5          # triangle wave in [-1.5, 1.5]
            blue = 0.90 + 0.03 * tri              # wanders ±0.045 around 0.90, mean-reverting
            return Reading(xyz=tuple(c * 60.0 for c in _xyz_for_linear_rgb(1.0, 1.0, blue)))
        return Reading(xyz=tuple(c * 60.0 for c in _xyz_for_linear_rgb(1.0, 1.0, 0.90)))

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


def test_compromised_display_is_flagged_not_converged():
    """A frozen / wrong display (reference reads wildly off the commanded luminance) is caught as
    COMPROMISED and FLAGGED — never silently accepted as 'convergent' just because the frozen reads
    are identical. (This is the live-HW dogegen render-freeze failure mode.)"""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    base = _xyz_for_linear_rgb(1.0, 1.0, 1.0)   # Y ~ 1.0

    def frozen(patch: MeasurePatch) -> Reading:
        if patch.role == "neutral_ref":
            return Reading(xyz=tuple(c * 720.0 for c in base))   # ~720 nits vs the 60 commanded = 12x
        return Reading(xyz=tuple(c * 30.0 for c in base))

    res = _controller(frozen, transfer).run()
    assert res.compromised is True
    assert res.regime == "compromised"
    assert res.needs_adjudication is True
    assert not res.converged
    assert any("COMPROMISED" in f for f in res.flags)


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


def test_landing_requires_operating_window_after_soak():
    """A panel that SOAKED must not be declared converged until it has LANDED — a full window of
    operating-load (k≈1) blocks, the overshoot flushed out of the evaluation window — so cooling-
    from-overshoot can't masquerade as a flat 'converged' point during the ramp-down."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    panel = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    rows = []
    clock = _Clock()

    def measure(patch: MeasurePatch) -> Reading:
        clock.tick(); return panel(patch)

    cfg = ThermalConfig(load_reads_per_block=8, window_blocks=5, max_blocks=240, drift_floor=0.003)
    res = ThermalController(measure=measure, transfer=transfer, content=_grey_content(transfer),
                           ref_nits=60.0, balance_noise=0.0008, config=cfg, clock=clock,
                           emit=rows.append).run()
    assert res.converged, res.flags
    conv = [r for r in rows if str(r["state"]).startswith("CONVERGED")]
    assert conv, "expected a CONVERGED block record"
    assert conv[0]["did_soak"] is True                  # this cold panel genuinely warmed in ⇒ soaked
    assert conv[0]["op_streak"] >= cfg.window_blocks     # convergence judged only after a full landing


def test_warm_balance_recorded_on_convergence():
    """A converging panel reports its converged OPERATING-load channel balance (a validated 'warm'
    fingerprint for a later calibration run); an early-aborted (compromised) run does not."""
    from dlc.drift import CHANNELS
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    panel = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    res = _controller(panel, transfer).run()
    assert res.converged
    assert res.warm_balance is not None
    assert set(res.warm_balance) == set(CHANNELS)
    assert all(0.0 <= v <= 1.0 for v in res.warm_balance.values())


def test_warm_baseline_is_shadow_only_in_phase1():
    """A supplied warm_baseline is SHADOW-LOGGED (its distance recorded) but NEVER short-circuits the
    controller in Phase 1: a genuinely cold panel still soaks and runs its full warm-in."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0)
    panel = SyntheticPanel(transfer=transfer, white_nits=120.0, cold_blue_gain=0.85,
                           load_thermal=True, thermal_rate=0.06, start_temp=0.0)
    clock = _Clock()

    def measure(patch: MeasurePatch) -> Reading:
        clock.tick(); return panel(patch)

    cfg = ThermalConfig(load_reads_per_block=8, window_blocks=5, max_blocks=240, drift_floor=0.003)
    res = ThermalController(measure=measure, transfer=transfer, content=_grey_content(transfer),
                           ref_nits=60.0, balance_noise=0.0008, config=cfg, clock=clock,
                           warm_baseline={"R": 0.33, "G": 0.33, "B": 0.34}).run()
    assert res.baseline_distance is not None and res.baseline_distance >= 0.0   # shadow recorded
    assert res.blocks > 1 and res.content_reads > cfg.load_reads_per_block       # NOT a 1-block bypass


def test_protection_limited_flagged_when_reference_dims_under_soak():
    """ABL / power / thermal throttle: while soaking (k>1 load drives brighter), the fixed reference
    DIMS below its operating baseline — active limiting, not injected heat. The flatness that follows
    must NOT read as convergence; flag it with a PROTECTION reason instead."""
    transfer = Transfer.power(gamma=2.2, peak_nits=1000.0)   # headroom so the soak drives above operating
    st = {"last_load_nits": 0.0, "n": 0}

    def measure(patch: MeasurePatch) -> Reading:
        if patch.role == "neutral_ref":
            st["n"] += 1
            warm = min(1.0, 0.85 + 0.01 * st["n"])                  # a directional warm-in ⇒ the loop soaks
            dim = 0.6 if st["last_load_nits"] > 150.0 else 1.0      # ABL: dim the ref under heavy soak load
            return Reading(xyz=tuple(c * 60.0 * dim for c in _xyz_for_linear_rgb(1.0, 1.0, warm)))
        st["last_load_nits"] = transfer.cv_to_nits(max(patch.rgb))  # the (scaled) load drive in nits
        return Reading(xyz=tuple(c * 30.0 for c in _xyz_for_linear_rgb(1.0, 1.0, 1.0)))

    res = _controller(measure, transfer, max_blocks=30).run()
    assert res.protection_limited is True
    assert res.reason == "protection_limited" or any("limiting" in f for f in res.flags)


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
