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


# ---------------------------------------------------------------------------
# sample_cube
# ---------------------------------------------------------------------------

def test_sample_cube_identity_is_passthrough():
    cube = identity_cube(17)
    signals = _cube_signals(5)
    out = sample_cube(cube, signals)
    assert np.allclose(out, signals, atol=1e-6)


# ---------------------------------------------------------------------------
# convergence
# ---------------------------------------------------------------------------

def test_machine_drives_correctable_panel_below_threshold():
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))  # mild WB error (blue bright)
    signals = _cube_signals(5)
    measured = probe(signals)

    raw_max = _raw_max_de(target, probe, signals)
    cfg = OptimizeConfig(grid_size=17, threshold=2.0, max_outer=4)
    result = optimize_cube(target=target, probe=probe, signals=signals,
                           measured_xyz=measured, config=cfg)

    assert raw_max > cfg.threshold                 # the uncorrected panel is out of spec
    assert result.converged is True
    assert result.digest["best_max_de"] < cfg.threshold
    assert result.digest["best_max_de"] < raw_max  # the cube actually corrected it
    assert result.digest["cube_monotonic"] is True
    assert result.needs_adjudication is False


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
                           config=OptimizeConfig(grid_size=9, threshold=2.0, max_outer=cap, floor_tol=0.3))
    assert result.iterations < cap            # terminated by the stall rule, not the cap
    assert result.needs_adjudication is True   # the real floor is still surfaced


def test_noiseless_correctable_panel_still_converges():
    # The termination change must not regress the clean (noiseless) convergence path.
    target = _sdr_target()
    probe = synthetic_probe(target, gains=(1.0, 1.012, 1.025))
    signals = _cube_signals(5)
    result = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=probe(signals),
                           config=OptimizeConfig(grid_size=17, threshold=2.0, max_outer=6))
    assert result.converged is True
    assert result.digest["best_max_de"] < 2.0


# ---------------------------------------------------------------------------
# floor detection / escalation
# ---------------------------------------------------------------------------

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
# cube output
# ---------------------------------------------------------------------------

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
