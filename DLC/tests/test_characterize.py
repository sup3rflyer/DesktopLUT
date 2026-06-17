"""Tests for the display+instrument characterization pass (``dlc.characterize``).

Drives :func:`~dlc.characterize.run_characterization` against a deterministic
:class:`~dlc.measure_loop.SyntheticPanel` with an INJECTED clock — no display, no meter,
no real time. Numpy-free (the module + its inputs are stdlib only), so it runs in the
spine suite without the engine extras.

Asserts the load-bearing behaviour: the three axes produce a usable DIP (noise vs
luminance, native white/black/primaries, settle + read-overhead timing, warm-up/cold
channel), a noisier panel yields a larger measured σ, and an unsettleable panel is
FLAGGED for adjudication rather than silently capped.
"""

from __future__ import annotations

import math

from dlc.characterize import CharacterizeConfig, _noise_floor_nits, run_characterization
from dlc.dip import NoiseBand
from dlc.engine.patches import Transfer
from dlc.measure_loop import SyntheticPanel


def _transfer() -> Transfer:
    return Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)


def _perfect_panel() -> SyntheticPanel:
    # Fully warm, blue gain 1.0 → identical reads of a held stimulus (σ=0), settles at once.
    return SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0)


class _FakeClock:
    """A deterministic monotonic clock advancing a fixed dt per call (no real sleeping)."""

    def __init__(self, dt: float = 0.1) -> None:
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        v = self.t
        self.t += self.dt
        return v


def _fast_cfg(**kw) -> CharacterizeConfig:
    # warmup_max_minutes=0 SKIPS the (slow) thermal phase by default; the thermal-regime tests
    # opt back in explicitly. Keeps the noise/settle/native tests fast.
    base = dict(noise_levels=(1.0, 0.2), noise_reads=5, settle_discard=1, black_reads=2,
                primary_reads=2, settle_levels={"bright": 1.0, "dark": 0.05},
                warmup_observe_reads=10, warmup_max_minutes=0, creep_reads=3)
    base.update(kw)
    return CharacterizeConfig(**base)


# ---------------------------------------------------------------------------
# instrument axis — noise model
# ---------------------------------------------------------------------------

def test_produces_one_noise_band_per_level_ascending():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock())
    bands = res.dip.noise_model
    assert len(bands) == 2
    # ascending nits, each with the requested sample size
    assert bands[0].nits < bands[1].nits
    assert all(b.reads == 5 for b in bands)
    # a perfect (held) panel has zero read-to-read spread
    assert all(b.sigma_de == 0.0 for b in bands)
    # the produced DIP drives the read policy: zero σ ⇒ a single read suffices
    assert res.dip.reads_for_tolerance(bands[-1].nits, 0.2) == 1


def test_noisier_panel_has_larger_sigma_and_is_flagged():
    clean = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                                 config=_fast_cfg(), clock=_FakeClock())
    noisy_panel = SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0,
                                 noise=0.4, seed=11)
    noisy = run_characterization(measure=noisy_panel, transfer=_transfer(),
                                 config=_fast_cfg(), clock=_FakeClock())
    white_clean = clean.dip.noise_model[-1].sigma_de
    white_noisy = noisy.dip.noise_model[-1].sigma_de
    assert white_noisy > white_clean
    # a panel this jittery cannot settle / reads abnormally → FLAGGED, never silently accepted
    assert noisy.needs_adjudication is True
    assert noisy.flags
    assert noisy.question and "recharacterize" in noisy.question


def test_read_overhead_is_the_measured_per_read_time():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock(dt=0.1))
    # each read spans exactly one dt (two consecutive clock reads) → median == dt
    assert res.dip.read_overhead_s is not None
    assert math.isclose(res.dip.read_overhead_s, 0.1, rel_tol=0, abs_tol=1e-9)


def test_noise_floor_is_black_when_meter_is_clean():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock())
    # every band within tolerance ⇒ nothing above black is noise-dominated → floor == native black
    assert res.dip.noise_floor_nits == res.dip.native_black_nits


# ---------------------------------------------------------------------------
# noise-floor helper — the σ=tolerance crossing across luminance (all branches)
# ---------------------------------------------------------------------------

def test_noise_floor_all_clean_clamped_to_black_or_dimmest():
    bands = [NoiseBand(nits=1.0, sigma_de=0.1, reads=10), NoiseBand(nits=120.0, sigma_de=0.05, reads=10)]
    # black below the dimmest band → floor is black (nothing above black is noise-dominated)
    assert _noise_floor_nits(bands, 0.2, black_nits=0.02) == 0.02
    # a RAISED black floor can't report a floor above a band we proved clean → clamped to dimmest
    assert _noise_floor_nits(bands, 0.2, black_nits=5.0) == 1.0


def test_noise_floor_all_noisy_is_brightest_band():
    bands = [NoiseBand(nits=1.0, sigma_de=0.9, reads=10), NoiseBand(nits=120.0, sigma_de=0.4, reads=10)]
    # meter noisy even at white → a single read is untrustworthy everywhere tested
    assert _noise_floor_nits(bands, 0.2, black_nits=0.02) == 120.0


def test_noise_floor_interpolates_the_crossing():
    # dim band noisy (σ 0.4 @ 1 nit), bright band clean (σ 0.1 @ 11 nits); tolerance 0.2 crosses
    # between them. Linear in σ: f = (0.2-0.4)/(0.1-0.4) = 0.6667 → nits = 1 + 0.6667*(11-1) ≈ 7.667
    bands = [NoiseBand(nits=1.0, sigma_de=0.4, reads=10), NoiseBand(nits=11.0, sigma_de=0.1, reads=10)]
    floor = _noise_floor_nits(bands, 0.2, black_nits=0.02)
    assert abs(floor - 7.6667) < 1e-3
    assert bands[0].nits < floor < bands[1].nits   # genuinely between the two tested points


# ---------------------------------------------------------------------------
# display axis — native levels + settle
# ---------------------------------------------------------------------------

def test_native_white_black_primaries_measured():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock())
    dip = res.dip
    # native white ≈ D65 at the panel's 120-nit white
    assert dip.native_white_nits is not None and abs(dip.native_white_nits - 120.0) < 1.0
    assert abs(dip.native_white_xy[0] - 0.3127) < 0.005 and abs(dip.native_white_xy[1] - 0.3290) < 0.005
    # native black ≈ 0 on the synthetic panel
    assert dip.native_black_nits == 0.0
    # native primaries ≈ sRGB primaries (the synthetic panel is sRGB/Rec.709)
    assert abs(dip.native_primaries["R"][0] - 0.64) < 0.01
    assert abs(dip.native_primaries["G"][1] - 0.60) < 0.01
    assert abs(dip.native_primaries["B"][0] - 0.15) < 0.01


def test_settle_is_dwell_based_not_read_time_inflated():
    # A panel that settles within one integration reports a SMALL dwell — NOT the old read-count
    # artifact (which reported ~28 s = 3-4 slow dark reads). The earliest read already matches the
    # final value, so the reported settle is sub-second regardless of how slow the reads are.
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock(dt=0.1))
    dip = res.dip
    assert dip.settle_by_level is not None and set(dip.settle_by_level) == {"bright", "dark"}
    assert all(0.0 <= v < 1.0 for v in dip.settle_by_level.values())   # sub-second, not integration-inflated
    assert dip.settle_seconds == max(dip.settle_by_level.values())


def test_settle_dwell_detects_slow_droop():
    # A panel with ABL-style droop (target reads high then decays over a few reads) must report a
    # settle PAST the first read — the dwell method catches the real slow stabilization.
    from dlc.measure_loop import Reading
    base = _perfect_panel()
    seen: dict = {}

    def droop(patch):
        r = base(patch)
        if patch.label.startswith("settle_") and not patch.label.endswith("_from") and r.xyz:
            i = seen.get(patch.label, 0)
            seen[patch.label] = i + 1
            d = max(0.0, 0.20 * (1.0 - i / 4.0))   # +20% fading to 0 by the 4th read
            x, y, z = r.xyz
            return Reading(xyz=(x * (1 + d), y * (1 + d), z * (1 + d)), yxy=r.yxy, ok=True)
        return r

    res = run_characterization(measure=droop, transfer=_transfer(),
                               config=_fast_cfg(settle_levels={"bright": 1.0}, settle_required=2),
                               clock=_FakeClock(dt=0.1))
    assert res.dip.settle_by_level["bright"] >= 0.5   # the droop pushed settle well past the instant case


# ---------------------------------------------------------------------------
# drift axis — warm-up + cold channel + flag-don't-cap
# ---------------------------------------------------------------------------

def test_warmup_records_reads_to_settle_and_cold_channel():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock())
    assert res.dip.warmup_reads_to_settle is not None and res.dip.warmup_reads_to_settle >= 1
    assert res.dip.cold_channel in ("R", "G", "B")
    assert res.needs_adjudication is False   # a clean panel raises no flags


def test_unsettleable_warmup_is_flagged_not_capped():
    # A panel whose every read jitters never reaches consecutive agreement: the warm-up hits
    # its OBSERVATION bound and is FLAGGED for the LLM — never silently truncated to "warm".
    jittery = SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0,
                             noise=0.5, seed=3)
    res = run_characterization(measure=jittery, transfer=_transfer(),
                               config=_fast_cfg(warmup_observe_reads=6), clock=_FakeClock())
    assert res.needs_adjudication is True
    assert any("warm-up" in f for f in res.flags)
    # not settled ⇒ reads-to-settle is left unknown (None), not a fabricated cap value
    assert res.dip.warmup_reads_to_settle is None


def test_cold_channel_seed_is_respected_when_passed():
    # The profile-known cold channel biases warm-up when supplied (discovered otherwise).
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock(), cold_channel="B")
    assert res.dip.cold_channel == "B"


def test_cold_channel_is_discovered_from_the_data():
    # A panel with a genuinely dim channel must be IDENTIFIED as that channel (not hardcoded,
    # not seeded). The synthetic models a cold blue; at a cold start its blue reads low.
    cold_blue = SyntheticPanel(transfer=_transfer(), start_temp=0.0, cold_blue_gain=0.85,
                               warm_tau=0.02)
    res = run_characterization(measure=cold_blue, transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock(), cold_channel=None)
    assert res.dip.cold_channel == "B"   # discovered from the measurement, not assumed


def test_unsettleable_settle_is_flagged_not_capped():
    # A jittery panel never reaches consecutive agreement after a transition: the display-axis
    # settle hits its OBSERVATION bound and is FLAGGED — the step-response twin of warm-up.
    jittery = SyntheticPanel(transfer=_transfer(), start_temp=1.0, cold_blue_gain=1.0,
                             noise=0.5, seed=4)
    res = run_characterization(measure=jittery, transfer=_transfer(),
                               config=_fast_cfg(warmup_observe_reads=4, settle_observe_reads=5),
                               clock=_FakeClock())
    assert res.needs_adjudication is True
    assert any("settle did not stabilize" in f for f in res.flags)
    # an unstable level contributes NO fabricated settle value
    assert "bright" not in (res.dip.settle_by_level or {})


def test_creep_rate_is_zero_on_a_warm_panel_and_caps_the_interval():
    # A fully-warm panel does not creep: with an advancing clock the rate is a measured 0.0
    # (not None — the clock advanced), and the neutral interval saturates at its ceiling.
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(creep_reads=4), clock=_FakeClock())
    assert res.dip.creep_rate_de_per_min == 0.0
    assert res.dip.recommended_neutral_interval == 32


def test_creep_rate_is_measured_on_a_warming_panel():
    # A still-warming panel drifts during the creep window → a positive measured rate and a
    # finite, derived neutral interval (grounded in the measured creep, not a guess).
    warming = SyntheticPanel(transfer=_transfer(), start_temp=0.0, cold_blue_gain=0.6,
                             warm_tau=0.15)
    res = run_characterization(measure=warming, transfer=_transfer(),
                               config=_fast_cfg(creep_reads=6), clock=_FakeClock())
    assert res.dip.creep_rate_de_per_min is not None and res.dip.creep_rate_de_per_min > 0
    ni = res.dip.recommended_neutral_interval
    assert ni is not None and 4 <= ni <= 32


def test_failed_reads_at_a_level_are_flagged_not_fabricated():
    # A measure fn that returns no usable read at a level must FLAG it, not invent a band.
    from dlc.measure_loop import Reading

    panel = _perfect_panel()

    def flaky_measure(patch):
        # the dimmest noise level (signal ≈ 0.2) reads nothing usable
        if patch.label.startswith("noise_0.2"):
            return Reading(xyz=None, ok=False, error="dropout")
        return panel(patch)

    res = run_characterization(measure=flaky_measure, transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock())
    assert any("no usable read" in f for f in res.flags)
    # only the usable level produced a band (the dead one was skipped, not zero-filled)
    assert len(res.dip.noise_model) == 1
    assert res.dip.noise_model[0].nits > 1.0


def test_display_name_is_stamped_through():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(), clock=_FakeClock(), display="My Panel")
    assert res.dip.display == "My Panel"


# ---------------------------------------------------------------------------
# thermal regime — classified from the data (convergent / fluctuating / warming)
# ---------------------------------------------------------------------------

def _thermal_cfg(**kw) -> CharacterizeConfig:
    # warmup_max_minutes>0 RUNS the (now closed-loop) thermal phase; small block/read counts keep
    # the synthetic run snappy. The warm-in signal is injected on the neutral-reference reads.
    base = dict(noise_levels=(1.0,), noise_reads=2, black_reads=1, primary_reads=1,
                settle_levels={"bright": 1.0}, creep_reads=2, warmup_max_minutes=0.05,
                thermal_load_reads_per_block=4, thermal_ref_reads=4,
                thermal_window_blocks=5, thermal_max_blocks=40)
    base.update(kw)
    return CharacterizeConfig(**base)


def _bal_xyz(blue: float, lum: float = 30.0):
    """An XYZ at luminance ~``lum`` whose normalized channel balance is (1, 1, ``blue``) — the
    handle the thermal controller's warm-in sensor reads. Lets a test drive a chosen blue-balance
    trajectory through normalized_channels without touching luminance."""
    from dlc.measure_loop import _SRGB_TO_XYZ_D65 as M
    x = M[0][0] + M[0][1] + M[0][2] * blue
    y = M[1][0] + M[1][1] + M[1][2] * blue
    z = M[2][0] + M[2][1] + M[2][2] * blue
    s = lum / y if y > 0 else 1.0
    return (x * s, y * s, z * s)


def test_thermal_regime_convergent_on_warm_panel():
    # A warm, stable panel reads in-band immediately → convergent, with a recorded warm-up time.
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_thermal_cfg(warmup_max_minutes=2.0), clock=_FakeClock())
    assert res.dip.thermal_regime == "convergent"
    assert res.dip.warmup_minutes is not None
    assert not any("DYNAMIC" in f or "warming" in f for f in res.flags)


def test_thermal_regime_fluctuating_detected():
    # A panel whose neutral balance WANDERS (high gross, ~0 net) never settles → FLUCTUATING
    # (HDR-like): the DIP marks it, recommends frequent re-referencing + the maintain-load strategy.
    from dlc.measure_loop import Reading
    n = {"i": 0}

    def osc(patch):
        if patch.role == "neutral_ref":
            i = n["i"]; n["i"] += 1
            swing = 0.05 if (i // 4) % 2 == 0 else -0.05  # ALTERNATES per block (4 ref reads/block) →
            noise = 0.0015 * (((i * 7) % 3) - 1)          # reverses within the window: high gross, ~0 net
            return Reading(xyz=_bal_xyz(0.90 + swing + noise))
        return Reading(xyz=_bal_xyz(0.90))              # load reads: neutral, no heating in the synthetic

    res = run_characterization(measure=osc, transfer=_transfer(), config=_thermal_cfg(), clock=_FakeClock())
    assert res.dip.thermal_regime == "fluctuating"
    assert res.dip.recommended_neutral_interval == 4      # no steady state → aggressive drift checks
    assert res.dip.warmup_minutes is None                 # no warm-up target exists
    assert res.dip.fluctuation_envelope and res.dip.fluctuation_envelope > 0.0
    assert res.needs_adjudication is True
    assert any("DYNAMIC" in f or "maintain a consistent load" in f for f in res.flags)


def test_thermal_regime_warming_when_monotonic_and_unsettled():
    # A neutral balance still climbing in ONE direction at the bound is 'warming' (net≈gross).
    from dlc.measure_loop import Reading
    n = {"i": 0}

    def warming(patch):
        if patch.role == "neutral_ref":
            i = n["i"]; n["i"] += 1
            noise = 0.0010 * (((i * 5) % 3) - 1)
            return Reading(xyz=_bal_xyz(0.80 + 0.004 * (i // 4) + noise))   # monotonic per-block climb
        return Reading(xyz=_bal_xyz(0.80))

    res = run_characterization(measure=warming, transfer=_transfer(), config=_thermal_cfg(), clock=_FakeClock())
    assert res.dip.thermal_regime == "warming"
    assert any("still warming" in f for f in res.flags)


def test_thermal_phase_skipped_when_disabled():
    res = run_characterization(measure=_perfect_panel(), transfer=_transfer(),
                               config=_fast_cfg(warmup_max_minutes=0), clock=_FakeClock())
    assert res.dip.thermal_regime is None
    assert res.dip.warmup_minutes is None
