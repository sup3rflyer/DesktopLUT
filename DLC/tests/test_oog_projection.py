"""Out-of-gamut projection solve (build_cube ``oog_solve``) + the lattice diagnostics (engine/cube_quality.py).

The 2026-09-24 PA32UCXR finding: the direct per-node inversion toward clamped out-of-gamut targets diverged at the
Rec.2020 blue corner into a rough lattice (a node one cell off the blue axis output 0.27 PQ of red) that amplified
input noise into a static speckle. The projection solve re-solves every node whose target the reachable clamp moved
AT its gamut projection; everything else is bit-identical. These pin the contract on a synthetic sub-gamut panel
that decodes Rec.2020 colorimetrically and clips at its native gamut (the physics the run's post-MHC data showed).
"""
from __future__ import annotations

import numpy as np
import pytest

from dlc.colormath import rgb_to_xyz_matrix
from dlc.engine import cube_quality as CQ
from dlc.engine.lut_rbf import _near_black_signal, build_cube
from dlc.engine.model import DisplayErrorModel, Target, TargetSpace
from dlc.optimize import OptimizeConfig, _classify, optimize_cube, sample_cube

D65 = (0.3127, 0.3290)
PA32 = {"R": [0.6928, 0.3027], "G": [0.1829, 0.7498], "B": [0.1521, 0.0648]}
TARGET = Target.hdr_rec2020_pq(white_xy=D65)
RAW = TargetSpace(TARGET)
NAT = np.array(rgb_to_xyz_matrix(PA32["R"][0], PA32["R"][1], PA32["G"][0], PA32["G"][1],
                                 PA32["B"][0], PA32["B"][1], *D65))
INV = np.linalg.inv(NAT)
CAP_NITS = 1000.0
TOP = 0.75                       # ~ PQ(1000 nits): the calibrated top / patch cap
N = 17


def panel(signals, gain=(1.02, 0.99, 1.0)):
    """Colorimetric decode, native-gamut clip, luminance roof, a small per-channel gain error to correct."""
    xyz = RAW.ideal_xyz(np.asarray(signals, float))
    nat = np.clip((xyz / 1e4) @ INV.T, 0.0, CAP_NITS / 1e4) * np.asarray(gain)
    return (nat @ NAT.T) * 1e4


def native_rgb_panel(signals):
    """The OTHER physics: the drive is native RGB (PQ-tracking native primaries), no colorimetric decode."""
    from dlc._pq import eotf_norm
    lin = np.vectorize(eotf_norm)(np.asarray(signals, float))
    return (np.minimum(lin, CAP_NITS / 1e4) @ NAT.T) * 1e4


def training(seed=0, n=500):
    rng = np.random.default_rng(seed)
    grey = np.repeat(np.linspace(0.05, TOP, 12)[:, None], 3, axis=1)
    axes = np.concatenate([np.outer(np.linspace(0.1, TOP, 8), e) for e in np.eye(3)])   # pure primaries
    vol = rng.uniform(0.0, TOP, size=(n, 3))
    s = np.vstack([grey, axes, vol])
    return s, panel(s)


@pytest.fixture(scope="module")
def model():
    s, x = training()
    return DisplayErrorModel(s, x, TARGET, reachable_primaries=PA32), s


def build(m, s, mode, **kw):
    args = dict(max_correction=0.25, n_iterations=3, near_black_nits=0.1, neutral_band=0.05,
                hold_above=TOP, best_iterate=True, best_iterate_margin=2.0)
    args.update(kw)
    return build_cube(m, N, s, oog_solve=mode, **args)


def moved_nodes(m):
    """The guard set: lattice nodes whose target the reachable clamp moved, above the near-black knee."""
    axis = np.linspace(0, 1, N)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    g = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)
    moved = (np.any(np.abs(RAW.ideal_ictcp(g) - m.space.ideal_ictcp(g)) > 1e-9, axis=1)
             & (g.max(axis=1) >= _near_black_signal(m, 0.1)))
    return moved.reshape(N, N, N)


def test_projection_leaves_every_unmoved_node_bit_identical(model):
    m, s = model
    direct, proj = build(m, s, "direct"), build(m, s, "projection")
    changed = np.any(direct != proj, axis=-1)
    moved = moved_nodes(m)
    held = np.zeros_like(moved)                       # held nodes solve their top projection: may move too
    lvl = int(np.ceil(TOP * (N - 1) - 1e-9))
    held[lvl + 1:, :, :] = held[:, lvl + 1:, :] = held[:, :, lvl + 1:] = True
    assert changed.any()                               # the solve does something on this panel
    assert not np.any(changed & ~moved & ~held)        # ...and only where the clamp moved the target


def test_projection_has_no_off_channel_spike_beside_a_primary_axis(model):
    m, s = model
    proj = build(m, s, "projection")
    ideal = CQ.ideal_cube(m.space, N, TOP)
    lvl = int(np.floor(TOP * (N - 1)))
    for k in range(4, lvl + 1):                        # blue axis (0,0,k) vs its red / green neighbours
        for nb in ((0, 1), (1, 0)):                    # [g, r] offsets
            d = np.abs(proj[k, nb[0], nb[1]] - proj[k, 0, 0])
            d_ideal = np.abs(ideal[k, nb[0], nb[1]] - ideal[k, 0, 0])
            assert np.all(d <= d_ideal + 0.06), (k, nb, d, d_ideal)


def test_projection_is_stable_under_small_measurement_noise(model):
    m, s = model
    s0, x0 = training()
    rng = np.random.default_rng(5)
    m2 = DisplayErrorModel(s0, x0 * (1 + 1e-3 * rng.standard_normal(x0.shape)), TARGET, reachable_primaries=PA32)
    a, b = build(m, s, "projection"), build(m2, s0, "projection")
    moved = moved_nodes(m)
    assert np.percentile(np.abs(a - b).max(-1)[moved], 99) <= 0.1


def test_projection_works_under_the_chroma_clip_policy_too():
    s, x = training()
    tgt = Target.hdr_rec2020_pq(white_xy=D65, oog_mapping="chroma-clip")
    m = DisplayErrorModel(s, x, tgt, reachable_primaries=PA32)
    cube = build(m, s, "projection")
    assert np.all(np.isfinite(cube)) and cube.min() >= 0.0 and cube.max() <= 1.0


def test_unknown_oog_solve_is_rejected_before_any_work(model):
    m, s = model
    with pytest.raises(ValueError, match="oog_solve"):
        build_cube(m, N, s, oog_solve="lm")


def test_classify_measures_correction_from_the_reference():
    # An out-of-gamut blue driven to its gamut projection moved every channel far from the request, but it is
    # being MAPPED, not corrected: against the reachable reference it is neither budget-limited nor a rail clip.
    verify = np.array([[0.0, 0.0, 0.7]])
    reach = TargetSpace(TARGET, reachable_primaries=PA32).reachable_signal(verify)
    de = np.array([5.0])
    legacy = _classify(verify, reach, de, 2.0, 0.1, clamp_frac=0.85, boundary_eps=2e-3, low_light_signal=0.08)
    proj = _classify(verify, reach, de, 2.0, 0.1, clamp_frac=0.85, boundary_eps=2e-3, low_light_signal=0.08,
                     reference=reach)
    assert bool(legacy["budget_limited"][0]) is True
    assert bool(proj["budget_limited"][0]) is False and bool(proj["signal_clipped"][0]) is False


def test_cube_quality_of_the_ideal_cube_is_clean_and_a_spike_is_flagged(model):
    m, s = model
    ideal = CQ.ideal_cube(m.space, N, TOP)
    q = CQ.cube_quality(m, ideal, m.space, PA32, D65, hold_above=TOP, top=TOP)
    assert q["noise_gain"]["flag"] is False and abs(q["noise_gain"]["oog"]["p99"]) < 1e-6
    assert q["ramp_excess"]["flag"] is False and q["excess_reversals"]["excess"] == 0
    assert q["oog_drive_share"]["gt_5pct"] == 0.0
    spiked = ideal.copy()
    lvl = int(np.floor(TOP * (N - 1)))
    spiked[6:lvl + 1, 0, 1, 0] += 0.25                 # red added one cell off the blue axis (the 09-24 shape)
    q2 = CQ.cube_quality(m, np.clip(spiked, 0, 1), m.space, PA32, D65, hold_above=TOP, top=TOP)
    assert q2["noise_gain"]["flag"] is True
    assert q2["noise_gain"]["oog"]["max"] > 5.0
    assert q2["oog_drive_share"]["gt_1pct"] > 0.0


def test_premise_check_tells_colorimetric_from_native_rgb_panels():
    rng = np.random.default_rng(2)
    s = rng.uniform(0.0, TOP, size=(600, 3))
    ok = CQ.premise_check(s, panel(s, gain=(1, 1, 1)), TARGET, PA32, D65, CAP_NITS)
    bad = CQ.premise_check(s, native_rgb_panel(s), TARGET, PA32, D65, CAP_NITS)
    assert ok["passed"] is True and ok["n"] >= 20
    assert bad["passed"] is False
    few = CQ.premise_check(s[:5], panel(s[:5]), TARGET, PA32, D65, CAP_NITS)
    assert few["passed"] is None


def test_optimize_reports_the_solve_and_the_lattice_evidence():
    s, x = training(n=200)

    def probe(driven):
        return panel(driven)

    for mode in ("direct", "projection"):
        cfg = OptimizeConfig(grid_size=9, max_outer=1, oog_solve=mode, top_hold_signal=TOP,
                             adaptive_sampling=False)
        res = optimize_cube(target=TARGET, probe=probe, signals=s, measured_xyz=x, config=cfg,
                            reachable_primaries=PA32)
        assert res.digest["oog_solve"] == mode
        cq = res.digest["cube_quality"]
        assert set(cq) == {"noise_gain", "ramp_excess", "excess_reversals", "oog_drive_share"}
        assert np.all(np.isfinite(sample_cube(res.cube, s)))
