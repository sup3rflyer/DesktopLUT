"""Gain-aware inner step of build_cube (``inner_step``, 2026-09-28 — HANDOFF §0 A9).

The legacy per-node update ``s ← ideal⁻¹(T − δ(s))`` assumes the panel answers a drive change at gain 1. Behind
the Windows SDR MHC2 (the matrix is applied in sRGB-piecewise linear light, the ReGamma'd result feeds a γ2.2
base LUT) a small off-channel drive answers at gain ≈ 2 through the sRGB toe, so the node error goes
``e ← (1 − g)·e ≈ −e``: a period-2 cycle, and the shipped odd iterate mirrors the MHC's dim over-saturation
into desaturation (PA32UCXR run 133655). These pin the contract on a parameter-free simulation of that chain
(the "A3" chain of results/_replays/2026-09-28_SDR_desat): the PA's measured native primaries/white, the
production ``mhc2_matrix``, sRGB DeGamma → matrix → sRGB ReGamma → γ2.2 base → additive panel.
"""
from __future__ import annotations

import numpy as np
import pytest

import colour

from dlc.colormath import rgb_to_xyz_matrix
from dlc.engine.lut_rbf import GAIN_AWARE_MAX_GAIN, INNER_STEPS, _chord_gain, build_cube
from dlc.engine.model import DisplayErrorModel, Target, TargetSpace, de_itp
from dlc.mhc_cube import mhc2_matrix
from dlc.optimize import (OptimizeConfig, optimize_cube, resolve_inner_step, sample_cube,
                          synthetic_probe)

D65 = (0.3127, 0.3290)
# PA32UCXR run 20260925_133655 mhc_params_sdr: measured native primaries + native white.
PRIM = {"rx": 0.696042, "ry": 0.303958, "gx": 0.181786, "gy": 0.750021, "bx": 0.151213, "by": 0.064799}
NATIVE_WHITE = (0.313393, 0.326755)
SRGB = {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}
A = np.array(mhc2_matrix(PRIM, NATIVE_WHITE, SRGB, D65))          # as-applied RGB→RGB (non-diagonal)
DISP = np.array(rgb_to_xyz_matrix(PRIM["rx"], PRIM["ry"], PRIM["gx"], PRIM["gy"], PRIM["bx"], PRIM["by"],
                                  *NATIVE_WHITE, white_Y=1.0))
ROWSUM = A @ np.ones(3)
NITS = 120.0
SDR = Target.sdr_srgb_power(gamma=2.2, white_nits=NITS, white_xy=D65)
SPACE = TargetSpace(SDR)
N = 17
BUDGET = 0.19


def _srgb_decode(s):
    return np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)


def _srgb_encode(y):
    y = np.clip(y, 0.0, None)
    return np.where(y <= 0.0031308, 12.92 * y, 1.055 * y ** (1 / 2.4) - 0.055)


def mhc2_sdr_panel(signals):
    """Wire → sRGB DeGamma → MHC2 matrix → sRGB ReGamma → γ2.2 base LUT (greys exact) → additive native
    panel."""
    s = np.clip(np.atleast_2d(np.asarray(signals, float)), 0.0, 1.0)
    y = _srgb_decode(s) @ A.T
    light = ROWSUM * np.clip(_srgb_encode(y / ROWSUM), 0.0, None) ** 2.2
    return NITS * light @ DISP.T


def _lab(xyz):
    return colour.XYZ_to_Lab(np.maximum(xyz, 0.0) / NITS, illuminant=np.array(D65))


def de2000(inputs, drives):
    return np.asarray(colour.delta_E(_lab(mhc2_sdr_panel(drives)), _lab(SPACE.ideal_xyz(np.atleast_2d(inputs))),
                                     method="CIE 2000"), float)


def chroma_error(inputs, drives):
    lm, lt = _lab(mhc2_sdr_panel(drives)), _lab(SPACE.ideal_xyz(np.atleast_2d(inputs)))
    return np.hypot(lm[:, 1], lm[:, 2]) - np.hypot(lt[:, 1], lt[:, 2])


def _training():
    """A production-like set (lattice + primary/secondary/grey ramps) plus the reads the fold-back gathers in a
    real run: small off-channel drives beside dim primaries (where the cube operates) — they teach the model
    the sRGB-toe gain, exactly as the iteration-1 probe reads did on run 133655."""
    lv = np.linspace(0.0, 1.0, 7)
    lattice = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    hues = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0, 1, 1], [1, 0, 1], [1, 1, 0], [1, 1, 1]], float)
    ramps = np.concatenate([np.outer(np.linspace(0.05, 1.0, 16), h) for h in hues])
    toe = []
    for h in range(3):
        others = [k for k in range(3) if k != h]
        for level in (0.1124, 0.2, 0.25, 0.3333, 0.5):
            for a in (0.0, 0.03, 0.06, 0.09):
                for b in (0.0, 0.03, 0.06, 0.09):
                    s = np.zeros(3)
                    s[h], s[others[0]], s[others[1]] = level, a, b
                    toe.append(s)
    return np.unique(np.vstack([lattice, ramps, np.array(toe)]).round(6), axis=0)


@pytest.fixture(scope="module")
def mhc2_model():
    s = _training()
    return DisplayErrorModel(s, mhc2_sdr_panel(s), SDR), s


def _build(model, signals, n, step, **kw):
    return build_cube(model, N, signal_points=signals, max_correction=BUDGET, n_iterations=n, inner_step=step, **kw)


DIM_RG = np.array([[0.25, 0, 0], [0.2, 0, 0], [0.3333, 0, 0], [0, 0.1124, 0], [0, 0.25, 0]])


def test_the_mhc2_chain_has_gain_two_along_off_channel_corrections():
    # The premise, on the physics alone (no model): red 0.25 + a small green/blue drive responds ~2x the ideal.
    x = np.array([[0.25, 0.0, 0.0]])
    d = np.array([[0.2544, 0.0637, 0.0278]])              # run 133655's shipped drive for red 0.25
    ict = TargetSpace.xyz_to_ictcp
    d_ideal = SPACE.ideal_ictcp(d) - SPACE.ideal_ictcp(x)
    d_panel = ict(mhc2_sdr_panel(d)) - ict(mhc2_sdr_panel(x))
    g, ok = _chord_gain(d_ideal, d_panel)
    assert ok[0] and 1.7 < g[0] < 2.6


def test_fixed_point_cycles_and_ships_the_mirrored_overshoot(mhc2_model):
    model, s = mhc2_model
    probe = DIM_RG[[0, 3]]                                  # red 0.25, green 0.1124
    before = chroma_error(probe, probe)
    assert np.all(before > 1.0)                             # the MHC-only panel over-saturates dim R/G
    even = chroma_error(probe, sample_cube(_build(model, s, 2, "fixed_point"), probe))
    odd = chroma_error(probe, sample_cube(_build(model, s, 3, "fixed_point"), probe))
    assert np.all(even > 0.3) and np.all(odd < -0.3)       # period 2: the sign flips with the iteration parity
    shipped = de2000(probe, sample_cube(_build(model, s, 3, "fixed_point"), probe))
    assert np.all(shipped > 0.4)                            # n = 3 ships the mirrored (desaturated) iterate


def test_gain_aware_converges_on_the_mhc2_chain(mhc2_model):
    model, s = mhc2_model
    step, n = resolve_inner_step(OptimizeConfig(), SDR)
    assert (step, n) == ("gain_aware", 4)
    cube = _build(model, s, n, step)
    de = de2000(DIM_RG, sample_cube(cube, DIM_RG))
    legacy = de2000(DIM_RG, sample_cube(_build(model, s, 3, "fixed_point"), DIM_RG))
    assert np.all(de < 0.2), de
    assert de.mean() < 0.5 * legacy.mean()
    # more iterations do not re-open a cycle
    later = de2000(DIM_RG, sample_cube(_build(model, s, 8, step), DIM_RG))
    assert np.all(later < 0.2)


def test_gain_one_panel_is_the_legacy_result():
    # A panel whose response to any drive change IS the ideal's (a constant ICtCp offset): g ≡ 1, no damping.
    lv = np.linspace(0.0, 1.0, 7)
    s = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    offset = np.array([0.002, -0.001, 0.0015])
    model = DisplayErrorModel(s, SPACE.ictcp_to_xyz(SPACE.ideal_ictcp(s) + offset), SDR)
    for n in (3, 4):
        legacy = build_cube(model, N, signal_points=s, max_correction=0.25, n_iterations=n)
        gain = build_cube(model, N, signal_points=s, max_correction=0.25, n_iterations=n, inner_step="gain_aware")
        assert np.abs(gain - legacy).max() < 1e-4


def test_near_unit_gain_panel_differs_only_where_the_fixed_point_had_not_converged():
    # Per-channel gain errors ≤ 1.8 %: both steps reach the same colours (output within 1/20 JND), and the
    # gain-aware one is never worse by the model's own prediction.
    lv = np.linspace(0.0, 1.0, 7)
    s = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    probe = synthetic_probe(SDR, gains=(1.0, 1.008, 1.018))
    model = DisplayErrorModel(s, probe(s), SDR)
    legacy = build_cube(model, N, signal_points=s, max_correction=0.25, n_iterations=3).reshape(-1, 3)
    gain = build_cube(model, N, signal_points=s, max_correction=0.25, n_iterations=4,
                      inner_step="gain_aware").reshape(-1, 3)
    grid = np.stack(np.meshgrid(*[np.linspace(0, 1, N)] * 3, indexing="ij"), -1).reshape(-1, 3)[:, ::-1]
    tgt = SPACE.ideal_ictcp(grid)
    out_l, out_g = model.forward_ictcp(legacy), model.forward_ictcp(gain)
    assert de_itp(out_g - out_l).max() < 0.05
    assert np.all(de_itp(out_g - tgt) <= de_itp(out_l - tgt) + 5e-3)


def test_first_step_damps_only_where_the_model_gain_exceeds_one(mhc2_model):
    # The step contract, node by node: g ≤ 1 (or an unmeasurable step) keeps the legacy full step bit-for-bit;
    # g > 1 goes the fraction α = <r, dI>/<dF, dI> (dE_ITP inner product) of the way from the node to the
    # fixed-point candidate, α ∈ [1/4, 1] — 1/g for an untruncated candidate, never further than legacy.
    model, s = mhc2_model
    kw = dict(neutral_band=0.0, near_black_nits=0.0)
    legacy = _build(model, s, 1, "fixed_point", **kw).reshape(-1, 3)
    gain = _build(model, s, 1, "gain_aware", **kw).reshape(-1, 3)
    grid = np.stack(np.meshgrid(*[np.linspace(0, 1, N)] * 3, indexing="ij"), -1).reshape(-1, 3)[:, ::-1]
    grid[0] = 0.0
    w2 = np.array([1.0, 0.25, 1.0])
    f0, f1 = model.forward_ictcp(grid), model.forward_ictcp(legacy)
    d_ideal = SPACE.ideal_ictcp(legacy) - SPACE.ideal_ictcp(grid)
    g, ok = _chord_gain(d_ideal, f1 - f0)
    resid = SPACE.ideal_ictcp(grid) - f0
    with np.errstate(divide="ignore", invalid="ignore"):
        galerkin = np.sum(w2 * resid * d_ideal, 1) / np.sum(w2 * (f1 - f0) * d_ideal, 1)
        alpha = np.maximum(1.0 / g, galerkin)
    alpha = np.clip(np.where(ok & (g > 1.0), alpha, 1.0), 1.0 / GAIN_AWARE_MAX_GAIN, 1.0)
    damped = ok & (g > 1.0) & (alpha < 1.0)
    damped[0] = False                                        # black is pinned afterwards in both
    assert damped.sum() > 100 and (~damped).sum() > 100
    assert np.allclose(gain[~damped], legacy[~damped], atol=1e-12)
    expect = grid[damped] + (legacy[damped] - grid[damped]) * alpha[damped, None]
    assert np.allclose(gain[damped], expect, atol=1e-9)
    # untruncated candidates (the step reaches the ideal it aimed at, dI = r): α is exactly 1/g ...
    untrunc = damped & np.all(np.abs(resid - d_ideal) <= 1e-6 * np.abs(resid).max(1, keepdims=True) + 1e-12, axis=1)
    assert untrunc.sum() > 50
    assert np.allclose(alpha[untrunc], 1.0 / np.minimum(g[untrunc], GAIN_AWARE_MAX_GAIN), rtol=1e-3)
    # ... and a truncated one never takes less than 1/g
    assert np.all(alpha[damped] >= 1.0 / np.minimum(g[damped], GAIN_AWARE_MAX_GAIN) - 1e-12)
    assert np.all(np.abs(gain - grid) <= np.abs(legacy - grid) + 1e-12)


def test_a_node_cut_short_by_the_budget_reaches_it_instead_of_creeping(mhc2_model):
    # A candidate truncated by the correction budget: the residual is larger than the step, so the secant root
    # lies at/beyond the candidate — the node reaches the bound like the legacy step does, instead of closing
    # only (1 − 1/g) of the remaining gap per iteration.
    model, s = mhc2_model
    kw = dict(neutral_band=0.0, near_black_nits=0.0)
    tight = 0.02                                             # well below the ~0.065 the dim-red nodes need
    legacy = build_cube(model, N, signal_points=s, max_correction=tight, n_iterations=4, **kw)
    gain = build_cube(model, N, signal_points=s, max_correction=tight, n_iterations=4, inner_step="gain_aware",
                      **kw)
    node = (0, 0, 4)                                         # red 0.25
    grid_value = np.array([0.25, 0.0, 0.0])
    reach_legacy = np.abs(legacy[node] - grid_value).max()
    reach_gain = np.abs(gain[node] - grid_value).max()
    assert reach_legacy > 0.8 * tight
    assert reach_gain > 0.9 * reach_legacy


def test_out_of_gamut_target_nodes_keep_the_legacy_step():
    # With a reachable clamp (the HDR path's gamut awareness) the nodes whose target the clamp MOVED walk a
    # gamut corner, not a descent: gain-aware leaves them on the legacy step (the rough blue-corner lattice).
    small = {"R": [0.62, 0.33], "G": [0.31, 0.58], "B": [0.155, 0.075]}      # inside sRGB
    lv = np.linspace(0.0, 1.0, 6)
    s = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    probe = synthetic_probe(SDR, gains=(1.0, 1.01, 1.02), cross=0.02)
    model = DisplayErrorModel(s, probe(s), SDR, smoothing=1e-3, reachable_primaries=small)
    kw = dict(max_correction=0.5, n_iterations=4, neutral_band=0.0, near_black_nits=0.0)
    legacy = build_cube(model, 9, signal_points=s, **kw).reshape(-1, 3)
    gain = build_cube(model, 9, signal_points=s, inner_step="gain_aware", **kw).reshape(-1, 3)
    grid = np.stack(np.meshgrid(*[np.linspace(0, 1, 9)] * 3, indexing="ij"), -1).reshape(-1, 3)[:, ::-1]
    moved = np.any(np.abs(model.space.ideal_ictcp(grid) - SPACE.ideal_ictcp(grid)) > 1e-9, axis=1)
    moved[0] = False
    assert moved.sum() > 20
    assert np.allclose(gain[moved], legacy[moved], atol=1e-12)
    # ...and the guard has work to do: the model's gain along those nodes' first step exceeds 1 somewhere.
    first = build_cube(model, 9, signal_points=s, **{**kw, "n_iterations": 1}).reshape(-1, 3)
    d_ideal = SPACE.ideal_ictcp(first[moved]) - SPACE.ideal_ictcp(grid[moved])
    g, ok = _chord_gain(d_ideal, model.forward_ictcp(first[moved]) - model.forward_ictcp(grid[moved]))
    assert np.any(ok & (g > 1.0))


def test_inner_step_is_validated_and_mode_resolved():
    lv = np.linspace(0.0, 1.0, 4)
    s = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    model = DisplayErrorModel(s, SPACE.ideal_xyz(s), SDR, smoothing=1e-3)
    with pytest.raises(ValueError):
        build_cube(model, 5, signal_points=s, inner_step="newton")
    assert set(INNER_STEPS) == {"fixed_point", "gain_aware"}
    hdr = Target.hdr_rec2020_pq(white_xy=D65)
    assert resolve_inner_step(OptimizeConfig(), SDR) == ("gain_aware", 4)
    assert resolve_inner_step(OptimizeConfig(), hdr) == ("fixed_point", 3)
    assert resolve_inner_step(OptimizeConfig(inner_step="fixed_point"), SDR) == ("fixed_point", 3)
    assert resolve_inner_step(OptimizeConfig(inner_step="gain_aware", n_inner_iterations_gain_aware=5), hdr) \
        == ("gain_aware", 5)


def test_optimize_cube_reports_the_inner_step():
    lv = np.linspace(0.0, 1.0, 5)
    s = np.stack(np.meshgrid(lv, lv, lv, indexing="ij"), -1).reshape(-1, 3)
    probe = synthetic_probe(SDR, gains=(1.0, 1.01, 1.02))
    res = optimize_cube(target=SDR, probe=probe, signals=s, measured_xyz=probe(s),
                        config=OptimizeConfig(grid_size=9, max_outer=1, adaptive_sampling=False))
    assert res.digest["inner_step"] == "gain_aware" and res.digest["inner_iterations"] == 4
    res = optimize_cube(target=SDR, probe=probe, signals=s, measured_xyz=probe(s),
                        config=OptimizeConfig(grid_size=9, max_outer=1, adaptive_sampling=False,
                                              inner_step="fixed_point"))
    assert res.digest["inner_step"] == "fixed_point" and res.digest["inner_iterations"] == 3
