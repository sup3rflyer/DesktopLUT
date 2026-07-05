"""Tests for the optimal correction machine (``dlc.optimize``) — the outer loop.

Skips cleanly when numpy / scipy / colour are absent (engine extra), like
``test_engine_v2.py``. Drives the machine against a deterministic synthetic
ground-truth panel and asserts the load-bearing behaviour: it converges a
correctable panel below the target, the fold-back outer loop is no worse (and
generally better) than a single build, and an infeasible correction is surfaced
as a floor for adjudication rather than silently accepted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc.engine.model import Target, TargetSpace, de_itp
from dlc.optimize import (
    DegenerateMeasurements,
    OptimizeConfig,
    _classify,
    aggregate_training_samples,
    optimize_cube,
    sample_cube,
    seed_correction_budget,
    synthetic_probe,
)
from dlc.engine.lut_rbf import identity_cube


def _sdr_target() -> Target:
    return Target.sdr_srgb_power(gamma=2.2, white_nits=120.0)


def _cube_signals(n: int = 5) -> np.ndarray:
    ax = np.linspace(0.0, 1.0, n)
    return np.array([[r, g, b] for b in ax for g in ax for r in ax], dtype=float)


def _raw_max_de(target: Target, probe, signals: np.ndarray) -> float:
    space = TargetSpace(target)
    measured = probe(signals)
    de = de_itp(space.xyz_to_ictcp(measured) - space.ideal_ictcp(signals))
    return float(de.max())


# ---------------------------------------------------------------------------
# degenerate measurements (T2.3) — converted to a typed signal, not a crash
# ---------------------------------------------------------------------------

def test_collinear_measurements_raise_degenerate_not_linalgerror():
    target = _sdr_target()
    # A grayscale-only ramp: every signal on the r=g=b line ⇒ singular RBF interpolation
    # matrix (rank-deficient monomials). The raw failure is numpy.linalg.LinAlgError; the
    # boundary must convert it to an actionable DegenerateMeasurements, not crash the run.
    signals = np.array([[v, v, v] for v in np.linspace(0.0, 1.0, 12)], dtype=float)
    measured = TargetSpace(target).ideal_xyz(signals)
    probe = synthetic_probe(target)
    with pytest.raises(DegenerateMeasurements) as exc:
        optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                      config=OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0))
    assert "degenerate" in str(exc.value).lower()


def test_duplicate_measurements_raise_degenerate():
    target = _sdr_target()
    signals = np.tile([0.5, 0.5, 0.5], (8, 1)).astype(float)
    measured = TargetSpace(target).ideal_xyz(signals)
    with pytest.raises(DegenerateMeasurements):
        optimize_cube(target=target, probe=synthetic_probe(target), signals=signals,
                      measured_xyz=measured, config=OptimizeConfig(grid_size=9, max_outer=2))


def test_singular_later_build_keeps_best_cube_instead_of_crashing(monkeypatch):
    # The other DegenerateMeasurements path: a LATER model build going singular (fold-back
    # stacked collinear/duplicate driven points) must not crash or raise — the loop keeps
    # the best cube built so far and returns normally.
    import dlc.optimize as O
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))
    signals = _cube_signals(5)
    measured = probe(signals)

    real_model = O.DisplayErrorModel
    calls = {"n": 0}

    def flaky_model(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise np.linalg.LinAlgError("synthetic singular matrix")
        return real_model(*args, **kwargs)

    monkeypatch.setattr(O, "DisplayErrorModel", flaky_model)
    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=9, threshold=0.05, max_outer=4,
                                                 adaptive_sampling=False, neutral_band=0.0))
    assert calls["n"] >= 2                      # the second build really was attempted
    assert result.iterations == 1               # only the first build produced a snapshot
    assert result.cube.shape == (9, 9, 9, 3)    # ...and that cube is returned intact


def test_unchanged_training_set_reuses_model_instead_of_rebuilding(monkeypatch):
    # The force-full-validation path re-probes the SAME cube on the full set without folding
    # new measurements or escalating the budget — the deterministic model/cube must be reused,
    # not rebuilt (a rebuild re-pays the full k-fold CV for a bit-identical result).
    import dlc.optimize as O
    target = _sdr_target()
    # Infeasible floor so focused passes stall (no improvement, no escalation headroom).
    probe = synthetic_probe(target, gains=(1.0, 1.0, 0.88))
    signals = _cube_signals(6)
    measured = probe(signals)

    real_model = O.DisplayErrorModel
    calls = {"n": 0}

    def counting_model(*args, **kwargs):
        calls["n"] += 1
        return real_model(*args, **kwargs)

    monkeypatch.setattr(O, "DisplayErrorModel", counting_model)
    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=9, threshold=2.0, max_outer=6,
                                                 max_correction=0.25, auto_escalate=False,
                                                 adaptive_sampling=True, adaptive_min_full=64,
                                                 adaptive_initial_worst=24, adaptive_sentinels=16,
                                                 adaptive_low_light_cap=16, neutral_band=0.0))
    # At least one iteration ran on the cached model (a stalled focused pass followed by the
    # forced full validation of the same training set).
    assert calls["n"] < result.iterations
    modes = [h.sampling_mode for h in result.history]
    assert "full" in modes                     # the forced full validation really happened


# ---------------------------------------------------------------------------
# sample_cube
# ---------------------------------------------------------------------------

def test_sample_cube_identity_is_passthrough():
    cube = identity_cube(17)
    signals = _cube_signals(5)
    out = sample_cube(cube, signals)
    assert np.allclose(out, signals, atol=1e-6)


def test_aggregate_training_samples_averages_duplicates_and_sums_confidence():
    signals = np.array([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=float)
    xyz = np.array([[10.0, 2.0, 1.0], [14.0, 4.0, 1.0], [1.0, 9.0, 2.0]], dtype=float)
    sig_u, xyz_u, conf = aggregate_training_samples(signals, xyz)
    assert sig_u.shape == (2, 3)
    assert np.allclose(sig_u[0], [1.0, 0.0, 0.0])
    assert np.allclose(xyz_u[0], [12.0, 3.0, 1.0])
    assert conf.tolist() == [2.0, 1.0]


# ---------------------------------------------------------------------------
# convergence
# ---------------------------------------------------------------------------

def test_machine_drives_correctable_panel_below_threshold():
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))  # mild WB error (blue bright)
    signals = _cube_signals(5)
    measured = probe(signals)

    raw_max = _raw_max_de(target, probe, signals)
    cfg = OptimizeConfig(grid_size=17, threshold=2.0, max_outer=4,
                         neutral_band=0.0)  # raw panel, no MHC ⇒ cube must fix grey
    result = optimize_cube(target=target, probe=probe, signals=signals,
                           measured_xyz=measured, config=cfg)

    assert raw_max > cfg.threshold                 # the uncorrected panel is out of spec
    assert result.converged is True
    assert result.digest["best_max_de"] < cfg.threshold
    assert result.digest["best_max_de"] < raw_max  # the cube actually corrected it
    assert result.digest["cube_monotonic"] is True
    assert result.needs_adjudication is False


@pytest.mark.slow
def test_physical_engine_is_opt_in_and_reports_info():
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 0.99, 0.975), gammas=(1.0, 1.01, 0.99))
    signals = _cube_signals(4)
    measured = probe(signals)

    result = optimize_cube(target=target, probe=probe, signals=signals,
                           measured_xyz=measured,
                           config=OptimizeConfig(grid_size=5, max_outer=1,
                                                 engine="physical", neutral_band=0.0,
                                                 max_correction=0.25,
                                                 adaptive_sampling=False))

    assert result.digest["engine"] == "physical"
    assert result.digest["physical_info"] is not None
    assert result.digest["physical_info"]["metric"] == "de2000"
    assert result.digest["best_mean_de"] < _raw_max_de(target, probe, signals)


def test_constrained_rbf_engine_is_opt_in_and_reports_info():
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.0, 0.92))
    signals = _cube_signals(4)
    measured = probe(signals)

    result = optimize_cube(target=target, probe=probe, signals=signals,
                           measured_xyz=measured,
                           config=OptimizeConfig(grid_size=5, max_outer=1,
                                                 engine="constrained-rbf",
                                                 neutral_band=0.0,
                                                 max_correction=0.35,
                                                 adaptive_sampling=False))

    assert result.digest["engine"] == "constrained-rbf"
    assert result.digest["constrained_info"] is not None
    assert result.digest["constrained_info"]["metric"] == "de2000"
    assert result.digest["constrained_info"]["constrained_nodes"] > 0


def test_digest_breaks_out_the_neutral_axis():
    # The optimizer reports the probed neutral-diagonal (R=G=B) dE separately from the overall mean,
    # so the LLM (and _severe_optimizer_floor) can catch a cube that wrecks the MHC-owned neutral
    # axis even when best_mean_de stays modest.
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))
    signals = _cube_signals(5)
    result = optimize_cube(target=target, probe=probe, signals=signals,
                           measured_xyz=probe(signals),
                           config=OptimizeConfig(grid_size=17, threshold=2.0, max_outer=4))
    d = result.digest
    assert d["neutral_count"] >= 2                       # the 5-grid diagonal has neutral patches
    assert d["neutral_max_de"] is not None and d["neutral_mean_de"] is not None
    assert d["neutral_max_de"] >= d["neutral_mean_de"]
    assert d["neutral_max_de"] <= d["best_max_de"] + 1e-6  # a subset of all probes


# ---------------------------------------------------------------------------
# the outer fold-back loop is no worse than a single build
# ---------------------------------------------------------------------------

def test_foldback_loop_is_no_worse_than_a_single_build():
    target = _sdr_target()
    # A harder, still-feasible error so one build may not fully converge.
    probe = synthetic_probe(target, gains=(1.0, 1.02, 1.045), gammas=(1.0, 1.0, 1.03))
    signals = _cube_signals(5)
    measured = probe(signals)

    one = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                        config=OptimizeConfig(grid_size=17, max_outer=1))
    many = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                         config=OptimizeConfig(grid_size=17, max_outer=6))

    # Folding reality back (and escalating the budget) never makes the returned
    # cube worse — the result selects the best cube across iterations.
    assert many.digest["best_max_de"] <= one.digest["best_max_de"] + 1e-6
    # the model→reality gap is reported so the LLM can see model error
    assert "model_reality_gap" in many.history[0].as_dict()


# ---------------------------------------------------------------------------
# noise-robust termination (T2.4)
# ---------------------------------------------------------------------------

def test_noisy_run_stops_early_instead_of_running_to_cap():
    target = _sdr_target()
    # An infeasible floor (blue 12% dim) + 1% measurement noise: the worst-case error can't
    # keep improving, so the aggregate noise-aware rule must stop well before the iteration
    # cap. The old per-point "still improving" guard oscillated under noise (some point always
    # wiggled by floor_tol) and ran every iteration, folding noisy pairs into the model.
    probe = synthetic_probe(target, gains=(1.0, 1.0, 0.88), noise=0.01, seed=3)
    signals = _cube_signals(5)
    measured = probe(signals)
    cap = 12
    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=9, threshold=2.0, max_outer=cap, floor_tol=0.3,
                                                 neutral_band=0.0))  # raw panel, no MHC ⇒ cube must fix grey
    assert result.iterations < cap            # terminated by the stall rule, not the cap
    assert result.needs_adjudication is True   # the real floor is still surfaced


def test_noiseless_correctable_panel_still_converges():
    # The termination change must not regress the clean (noiseless) convergence path.
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))
    signals = _cube_signals(5)
    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=probe(signals),
                           config=OptimizeConfig(grid_size=17, threshold=2.0, max_outer=6,
                                                 neutral_band=0.0))  # raw panel, no MHC ⇒ cube must fix grey
    assert result.converged is True
    assert result.digest["best_max_de"] < 2.0


def test_auto_smooth_searches_below_old_floor():
    # The auto_smooth search range must include smoothing < 0.1. The old floor (0.1) pinned the
    # pick there and over-smoothed (~35% worse held-out in-gamut); the CV dE is monotone decreasing
    # below it, so on a low-noise panel the auto pick must land well under 0.1.
    from dlc.engine.model import DisplayErrorModel
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.01, 1.02))   # noiseless ⇒ less smoothing is better
    signals = _cube_signals(6)
    model = DisplayErrorModel(signals, probe(signals), target)  # smoothing=None ⇒ auto
    assert model.smoothing < 0.1


def test_build_cube_neutral_band_pins_grey_axis():
    # neutral_band must fade the cube's correction to identity on the grey diagonal (R==G==B inputs),
    # so the cube stops re-touching the neutral axis the MHC owns. Colour stays corrected.
    import numpy as np
    from dlc.optimize import build_cube
    from dlc.engine.model import DisplayErrorModel
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.05, 0.95))    # grey axis genuinely off
    signals = _cube_signals(6)
    model = DisplayErrorModel(signals, probe(signals), target, smoothing=1e-3)
    g = 17
    axis = np.linspace(0.0, 1.0, g)

    on = build_cube(model, g, signal_points=signals, max_correction=0.5, neutral_band=0.05)
    off = build_cube(model, g, signal_points=signals, max_correction=0.5, neutral_band=0.0)
    # cube is indexed [b, g, r]; the grey-diagonal node lut[i,i,i] is the output for input (a,a,a).
    for i in range(1, g - 1):
        assert np.allclose(on[i, i, i], [axis[i]] * 3, atol=1e-9)   # neutral_band ⇒ exact identity
    # without it, the cube DID move the grey axis (that is the regression we are preventing)...
    assert max(float(np.abs(off[i, i, i] - axis[i]).max()) for i in range(1, g - 1)) > 1e-3
    # ...while a fully-saturated colour node (sat=1, well outside the band) is bit-for-bit unchanged
    j = g - 2
    assert np.allclose(on[0, 0, j], off[0, 0, j], atol=1e-9)


def test_build_cube_preserves_black_and_blends_near_black_to_identity():
    # build_cube's dark-end invariants: the black node is EXACT [0,0,0] (never corrected),
    # and nodes whose ideal luminance sits below near_black_nits are blended toward
    # identity (the probe is unreliable there, so the model must not drive them).
    from dlc.optimize import build_cube
    from dlc.engine.model import DisplayErrorModel
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.05, 0.9))  # big error everywhere
    signals = _cube_signals(6)
    model = DisplayErrorModel(signals, probe(signals), target, smoothing=1e-3)
    g = 17
    lut = build_cube(model, g, signal_points=signals, max_correction=0.5,
                     neutral_band=0.0, near_black_nits=0.1)
    assert np.array_equal(lut[0, 0, 0], [0.0, 0.0, 0.0])
    # the first off-black grey node (signal 1/16 ≈ 0.36 nit ideal at 120-nit γ2.2 is above
    # the 0.1-nit knee, so use a coloured deep-shadow node under the knee instead:
    # signal (1/16, 0, 0) has ideal luminance ≈ 0.05 nit < 0.1 ⇒ mostly identity.
    axis = np.linspace(0.0, 1.0, g)
    node = lut[0, 0, 1]  # input (1/16, 0, 0)
    assert abs(node[0] - axis[1]) < 0.02 and abs(node[1]) < 0.02 and abs(node[2]) < 0.02


def test_adaptive_sampling_starts_focused_then_forces_full_validation():
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))
    signals = _cube_signals(7)  # large enough to avoid the always-full small-set path
    cfg = OptimizeConfig(
        grid_size=9,
        threshold=0.2,  # keep the loop moving to the full-validation milestone
        max_outer=3,
        adaptive_min_full=64,
        adaptive_initial_worst=24,
        adaptive_sentinels=24,
        adaptive_low_light_cap=24,
    )
    result = optimize_cube(target=target, probe=probe, signals=signals,
                           measured_xyz=probe(signals), config=cfg)

    assert result.history[0].sampling_mode == "focused"
    assert result.history[0].probed_patches < result.history[0].probe_total
    assert result.history[-1].sampling_mode == "full"
    assert result.digest["full_validation"] is True
    assert result.digest["best_probed_patches"] == result.digest["probe_total"]


# ---------------------------------------------------------------------------
# floor detection / escalation
# ---------------------------------------------------------------------------

def test_natural_zero_channels_on_saturated_patches_are_not_physical_floors():
    # A saturated blue-ish patch naturally has R/G at zero. That alone must not
    # mark it as clipped; otherwise low/saturated colours get prematurely written
    # off as panel limits instead of model residuals worth refining.
    masks = _classify(
        verify=np.array([[0.0, 0.0, 0.75]], dtype=float),
        driven=np.array([[0.0, 0.0, 0.70]], dtype=float),
        de=np.array([5.0]),
        threshold=2.0,
        budget=0.20,
        clamp_frac=0.85,
        boundary_eps=0.002,
        low_light_signal=0.08,
    )
    assert masks["signal_clipped"][0] == np.bool_(False)
    assert masks["residual"][0] == np.bool_(True)


def test_low_light_boundary_points_are_tracked_not_discarded():
    masks = _classify(
        verify=np.array([[0.031, 0.031, 0.0]], dtype=float),
        driven=np.array([[0.0, 0.0, 0.0]], dtype=float),
        de=np.array([8.0]),
        threshold=2.0,
        budget=0.20,
        clamp_frac=0.85,
        boundary_eps=0.002,
        low_light_signal=0.08,
    )
    assert masks["near_black"][0] == np.bool_(True)
    assert masks["low_clipped"][0] == np.bool_(True)
    assert masks["signal_clipped"][0] == np.bool_(True)


def test_full_scale_ceiling_is_still_a_physical_floor():
    masks = _classify(
        verify=np.array([[0.0, 0.0, 1.0]], dtype=float),
        driven=np.array([[0.0, 0.0, 1.0]], dtype=float),
        de=np.array([8.0]),
        threshold=2.0,
        budget=0.20,
        clamp_frac=0.85,
        boundary_eps=0.002,
        low_light_signal=0.08,
    )
    assert masks["high_clipped"][0] == np.bool_(True)
    assert masks["signal_clipped"][0] == np.bool_(True)


def test_infeasible_correction_surfaces_only_real_floors():
    target = _sdr_target()
    # Blue reads 12% too DIM: correcting it needs to push blue past full scale at
    # bright signals, which clips — a genuine physical floor near white.
    probe = synthetic_probe(target, gains=(1.0, 1.0, 0.88))
    signals = _cube_signals(5)
    measured = probe(signals)

    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=17, threshold=2.0, max_outer=6))

    assert result.converged is False
    assert result.needs_adjudication is True
    # the seed + escalation resolve the clamp-limited (false) floors — what remains
    # is the real, signal-clipped floor only.
    assert result.digest["physical_floor"] >= 1
    assert "near_black_floor" in result.digest
    assert "low_side_clipped" in result.digest and "high_side_clipped" in result.digest
    assert result.digest["budget_limited"] == 0
    assert len(result.floor_points) >= 1
    assert result.question is not None and "physical floor" in result.question
    # the worst floor point is a bright, blue-bearing stimulus
    assert max(result.floor_points[0][0]) > 0.5


def test_clamp_limited_points_are_not_labelled_a_panel_floor():
    """The hardening's core guarantee: a too-small budget is reported as
    budget-limited (raise the budget), NOT as the panel's physical floor."""
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.0, 0.88))
    signals = _cube_signals(5)
    measured = probe(signals)

    # Pin a deliberately tiny budget and disable escalation (the old behaviour).
    pinned = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=17, max_outer=2,
                                                 max_correction=0.05, auto_escalate=False))
    # Most stuck points are clamp-limited, and the machine SAYS so (vs the panel).
    assert pinned.digest["budget_limited"] > 0
    assert pinned.question is not None and "budget" in pinned.question

    # The hardened run (seed + escalate) resolves those — strictly fewer above
    # threshold, none left budget-limited.
    hardened = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                             config=OptimizeConfig(grid_size=17, max_outer=6))
    assert hardened.digest["above_threshold"] < pinned.digest["above_threshold"]
    assert hardened.digest["budget_limited"] == 0
    assert hardened.max_correction > 0.05  # the budget was raised to fit the panel


def test_seed_budget_scales_with_measured_residual():
    target = _sdr_target()
    space = TargetSpace(target)
    signals = _cube_signals(5)
    mild = synthetic_probe(target, gains=(1.0, 1.004, 1.008))
    big = synthetic_probe(target, gains=(1.0, 1.0, 0.85))
    b_mild = seed_correction_budget(space, signals, mild(signals))
    b_big = seed_correction_budget(space, signals, big(signals))
    assert b_big > b_mild
    assert 0.01 <= b_mild <= 0.05
    assert b_big > 0.06


# ---------------------------------------------------------------------------
# gamut-aware (#C3) correction: clamped targets are corrected TO, not chased
# ---------------------------------------------------------------------------

def _sub_gamut_panel():
    """A panel whose native primaries sit well inside the sRGB target — every
    saturated target clips, so the reachable clamp is live everywhere it matters."""
    import colour
    srgb = colour.RGB_COLOURSPACES["sRGB"]
    w = srgb.whitepoint
    prim = {k: (np.asarray(p) * 0.72 + np.asarray(w) * 0.28).tolist()
            for k, p in zip("RGB", srgb.primaries)}
    native = colour.RGB_Colourspace("sub-gamut panel",
                                    np.array([prim[c] for c in "RGB"]),
                                    np.asarray(w), whitepoint_name="native")

    def probe(signals: np.ndarray) -> np.ndarray:
        s = np.clip(np.asarray(signals, dtype=float).reshape(-1, 3), 0.0, 1.0)
        return np.maximum(colour.RGB_to_XYZ(s ** 2.2, native) * 120.0, 0.0)

    return prim, probe


def test_gamut_clamped_targets_are_corrected_not_worsened():
    # Regression pin (Phase 5, F5-1): the error model must train its delta against the
    # UNCLAMPED ideal even when reachable_primaries clamps the targets. With the delta
    # trained against the clamped ideal, build_cube's inversion (raw xyz_to_signal) was
    # inconsistent with it and the "correction" DESATURATED reachable boundary colours —
    # a ~7 dE_ITP patch came back at ~29 post-cube. The machine must drive the panel TO
    # the clamped target, never away from it.
    target = _sdr_target()
    prim, probe = _sub_gamut_panel()
    signals = _cube_signals(5)
    measured = probe(signals)

    space = TargetSpace(target, reachable_primaries=prim)
    raw_de = de_itp(space.xyz_to_ictcp(measured) - space.ideal_ictcp(signals))

    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=17, threshold=2.0, max_outer=4,
                                                 max_correction_cap=0.5),
                           reachable_primaries=prim)

    # The machine corrects the clamped-target error field, hard: the raw panel is tens of
    # dE off its reachable targets, the corrected one lands in the low single digits.
    assert result.digest["best_mean_de"] < 0.25 * float(raw_de.mean())
    assert result.digest["best_max_de"] < 0.5 * float(raw_de.max())

    # And the boundary patch the bug used to WORSEN specifically improves.
    idx = int(np.argmax(np.all(np.isclose(signals, [1.0, 0.0, 0.25]), axis=1)))
    driven = sample_cube(result.cube, signals[idx:idx + 1])
    post = de_itp(space.xyz_to_ictcp(np.maximum(probe(driven), 0.0))
                  - space.ideal_ictcp(signals[idx:idx + 1]))
    assert float(post[0]) < float(raw_de[idx])


def test_unreachable_targets_without_clamp_surface_as_floors():
    # The honesty contract on the same sub-gamut panel WITHOUT reachable_primaries:
    # the machine chases the unreachable pure targets into the signal rails and must
    # report physical floors (never budget limits once escalation is done) — the
    # adversarial "pure gamut floor" case from the Phase 5 brief.
    target = _sdr_target()
    prim, probe = _sub_gamut_panel()
    signals = _cube_signals(5)
    measured = probe(signals)

    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured,
                           config=OptimizeConfig(grid_size=17, threshold=2.0, max_outer=6,
                                                 max_correction_cap=0.5))
    assert result.converged is False
    assert result.needs_adjudication is True
    assert result.digest["physical_floor"] > 0
    assert result.digest["budget_limited"] == 0   # a gamut floor is never sold as a budget cap
    assert result.question is not None and "physical floor" in result.question


# ---------------------------------------------------------------------------
# cube output
# ---------------------------------------------------------------------------

def test_cube_indexing_convention_is_r_fastest_everywhere(tmp_path: Path):
    # ONE cross-pin for the `.cube` ordering convention (Phase 5): the standard IRIDAS
    # order is R-fastest, and four independent code paths each hard-code it —
    # lut_rbf.write_cube / identity_cube, lut_integrity.parse_cube (flat index
    # b·s²+g·s+r), simulation.write_identity_cube, and optimize.sample_cube (cube
    # indexed [b,g,r], sampled at signals[:, [2,1,0]]). An R↔B transposition in any
    # one of them silently swaps red and blue; this test fails instead.
    from dlc.engine.lut_rbf import identity_cube, write_cube
    from dlc.lut_integrity import parse_cube
    from dlc.simulation import write_identity_cube

    size = 5
    # An ASYMMETRIC cube: identity with the red output warped (r²) — any axis
    # transposition breaks equality, unlike a pure identity.
    cube = identity_cube(size)
    cube[..., 0] = cube[..., 0] ** 2

    path = tmp_path / "warped.cube"
    write_cube(cube, str(path))
    parsed = parse_cube(path)
    assert parsed.size == size and not parsed.parse_errors
    ax = np.linspace(0.0, 1.0, size)
    for b in (0, 2, 4):
        for g in (0, 1, 3):
            for r in (0, 3, 4):
                flat = parsed.values[(b * size * size) + (g * size) + r]
                assert np.allclose(flat, [ax[r] ** 2, ax[g], ax[b]], atol=1e-6), \
                    f"write_cube/parse_cube disagree at (r={r}, g={g}, b={b})"

    # simulation.write_identity_cube must agree with lut_rbf.identity_cube node-for-node.
    id_path = tmp_path / "id.cube"
    write_identity_cube(id_path, size=size)
    parsed_id = parse_cube(id_path)
    ident = identity_cube(size)
    for i, val in enumerate(parsed_id.values):
        b, rem = divmod(i, size * size)
        g, r = divmod(rem, size)
        assert np.allclose(val, ident[b, g, r], atol=1e-7)

    # sample_cube must read the warped cube back with the same convention: a pure red
    # input returns (r², 0, 0) — an R↔B swap would return (0, 0, r²) instead.
    out = sample_cube(cube, np.array([[1.0, 0.0, 0.0], [0.5, 0.0, 0.0], [0.0, 0.0, 0.5]]))
    assert np.allclose(out[0], [1.0, 0.0, 0.0], atol=1e-6)
    assert np.allclose(out[1], [0.25, 0.0, 0.0], atol=1e-6)
    assert np.allclose(out[2], [0.0, 0.0, 0.5], atol=1e-6)


def test_result_writes_a_parseable_cube(tmp_path: Path):
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.01, 1.02))
    signals = _cube_signals(4)
    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=probe(signals),
                           config=OptimizeConfig(grid_size=9, max_outer=2))
    out = tmp_path / "final.cube"
    result.write(str(out), title="test")
    text = out.read_text()
    assert "LUT_3D_SIZE 9" in text
    assert text.count("\n") > 9 ** 3  # header + one line per node


def test_synthetic_probe_is_deterministic():
    target = _sdr_target()
    p1 = synthetic_probe(target, gains=(1.0, 1.01, 1.02), noise=0.01, seed=5)
    p2 = synthetic_probe(target, gains=(1.0, 1.01, 1.02), noise=0.01, seed=5)
    s = _cube_signals(4)
    assert np.allclose(p1(s), p2(s))


# ---------------------------------------------------------------------------
# report_scorer: re-label the surfaced numbers WITHOUT touching the optimization
# ---------------------------------------------------------------------------

def test_report_scorer_relabels_surfaced_numbers_without_changing_optimization():
    # A report_scorer re-scores ONLY the surfaced numbers (digest/curve) into the run's report
    # metric and labels them; the cube still CONVERGES in dE_ITP. A constant scorer makes the
    # *_de_report fields take its value while the optimize-metric (dE_ITP) fields + the returned
    # cube stay byte-identical to a no-scorer run — proof the optimization is untouched.
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))
    signals = _cube_signals(5)
    measured = probe(signals)
    cfg = OptimizeConfig(grid_size=17, threshold=2.0, max_outer=4, neutral_band=0.0)

    base = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured, config=cfg)

    def const_scorer(sig, xyz):
        return np.full(len(sig), 0.5)

    rep = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured, config=cfg,
                        report_scorer=const_scorer, report_metric="CIEDE2000")

    # Optimization untouched: identical convergence, identical dE_ITP decision fields, identical cube.
    assert rep.converged == base.converged
    assert rep.digest["best_max_de"] == base.digest["best_max_de"]    # dE_ITP carrier (decision field)
    assert rep.digest["best_mean_de"] == base.digest["best_mean_de"]
    assert np.allclose(rep.cube, base.cube)
    # Surfaced numbers carry the scorer's value + the report-metric label.
    assert rep.digest["metric"] == "CIEDE2000"
    assert rep.digest["optimize_metric"] == "dE_ITP"
    assert rep.digest["best_max_de_report"] == pytest.approx(0.5)
    assert rep.digest["best_mean_de_report"] == pytest.approx(0.5)
    assert rep.history[0].metric == "CIEDE2000"
    assert rep.history[0].measured_max_de == pytest.approx(0.5)
    # Back-compat: no scorer ⇒ report fields equal the dE_ITP fields and label dE_ITP (HDR path).
    assert base.digest["metric"] == "dE_ITP"
    assert base.digest["optimize_metric"] == "dE_ITP"
    assert base.digest["best_max_de_report"] == base.digest["best_max_de"]
    assert base.history[0].metric == "dE_ITP"
