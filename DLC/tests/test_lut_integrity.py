"""Structural cube integrity (dlc.lut_integrity) — the check-cube gate's arms.

fable Phase 7a: the roadmap flagged `monotonicity_violations_allowed=0` as possibly
false-failing realistic cubes (a Phase 6 aside). Measured across six synthetic-panel
configurations (perfect / blue-deficient / noisy / cold-drifting, 17³ and 33³ grids,
through the real optimizer → writer → parser), realistic cubes carry ZERO violations
at the 1e-8 epsilon; only a pathological panel (3 % read noise + severe unwarmed
drift — a run the preheat/score guards flag anyway) produced reversals, and those
were shallow (median ~0.12× grid pitch) and mid-range, not near-black. The zero
default therefore stands; this file pins the empirical basis so a regression in the
optimizer's smoothness (or a parser/indexing bug) trips a test instead of a hunch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc import calibration_profile as cp
from dlc.engine.patches import Transfer
from dlc.lut_integrity import (default_neighbor_delta_allowed, parse_cube,
                               summarize_lut_integrity)
from dlc.measure_loop import MeasurePatch, SyntheticPanel
from dlc.optimize import OptimizeConfig, optimize_cube


def _optimizer_cube(tmp_path: Path, *, noise: float = 0.0) -> Path:
    """A realistic small cube built through the actual correction machine."""
    transfer = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=0.85,
                           noise=noise)
    sigs = set()
    for i in range(13):
        v = i / 12
        sigs.update({(v, v, v), (v, 0, 0), (0, v, 0), (0, 0, v)})
    for r in range(3):
        for g in range(3):
            for b in range(3):
                sigs.add((r / 2, g / 2, b / 2))
    signals = np.array(sorted(sigs), dtype=float)
    max_cv = transfer.max_cv

    def read(sig):
        rgb = tuple(int(round(c * max_cv)) for c in sig)
        return panel(MeasurePatch(label="x", rgb=rgb, signal=tuple(sig),
                                  role="measurement", bit_depth=transfer.bit_depth)).xyz

    measured = np.array([read(s) for s in signals], dtype=float)
    target = cp.Profile.synthetic().engine_target("srgb_g22", white_xy=(0.3127, 0.3290))
    result = optimize_cube(
        target=target, signals=signals, measured_xyz=measured,
        probe=lambda arr: np.array([read(s) for s in np.asarray(arr, float).reshape(-1, 3)]),
        config=OptimizeConfig(grid_size=9, max_outer=2, threshold=1.0))
    out = tmp_path / "final.cube"
    result.write(str(out), title="integrity pin")
    return out


def test_realistic_optimizer_cube_passes_defaults(tmp_path: Path):
    # A cube built from a plausibly imperfect panel must pass the structural gate at
    # DEFAULTS — zero monotonicity allowance included. If this starts failing, either
    # the optimizer lost its smoothness guarantees or the parser/indexing broke;
    # both deserve a loud stop, not a loosened gate.
    cube = parse_cube(_optimizer_cube(tmp_path))
    s = summarize_lut_integrity(cube=cube, phase="t", iteration=1,
                                integrity_path=tmp_path / "i.json")
    assert s.ok, s.notes
    assert s.monotonicity_violations == 0
    assert s.max_neighbor_delta <= s.max_neighbor_delta_allowed


def test_noisy_but_settled_cube_still_passes_defaults(tmp_path: Path):
    cube = parse_cube(_optimizer_cube(tmp_path, noise=0.02))
    s = summarize_lut_integrity(cube=cube, phase="t", iteration=1,
                                integrity_path=tmp_path / "i.json")
    assert s.ok, s.notes
    assert s.monotonicity_violations == 0


def test_deep_reversal_fails_the_gate(tmp_path: Path):
    # A structural tear — one node yanked a full grid step backwards — must fail.
    src = _optimizer_cube(tmp_path)
    lines = src.read_text(encoding="utf-8").splitlines()
    data_start = next(i for i, ln in enumerate(lines)
                      if ln and not ln.startswith(("#", "TITLE", "LUT_", "DOMAIN_")))
    mid = data_start + (len(lines) - data_start) // 2
    r, g, b = (float(x) for x in lines[mid].split())
    lines[mid] = f"{max(0.0, r - 0.3):.6f} {g:.6f} {b:.6f}"
    torn = tmp_path / "torn.cube"
    torn.write_text("\n".join(lines) + "\n", encoding="utf-8")
    s = summarize_lut_integrity(cube=parse_cube(torn), phase="t", iteration=1,
                                integrity_path=tmp_path / "i.json")
    assert not s.ok
    assert s.monotonicity_violations > 0


def test_neighbor_allowance_scales_with_grid_pitch():
    # Phase 6's grid-pitch-derived ceiling: identity pitch + the largest capped correction
    # swing (0.5), clamped to 1.0 — a full-range neighbour jump is never legitimate.
    assert default_neighbor_delta_allowed(33) == pytest.approx(2.0 / 32 + 0.5)
    assert default_neighbor_delta_allowed(17) == pytest.approx(2.0 / 16 + 0.5)
    assert default_neighbor_delta_allowed(1) == 1.0
