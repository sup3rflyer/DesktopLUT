"""The optimal correction machine — the outer hardware loop (v2-design-notes §7).

The differentiator. Two nested loops drive a display to its physical floor:

* **Inner (software, already built):** :func:`dlc.engine.lut_rbf.build_cube` fits a
  smoothed model of the display's error field and, per LUT node, *predicts the
  error → steers toward the input that cancels it → re-predicts → iterates*. Runs
  entirely against the measured model — no hardware.
* **Outer (hardware, this module):** build a cube from the model → **apply it and
  re-measure reality** → wherever reality disagrees with the model (model error) or
  a point isn't at its floor, **fold those real measurements back into the model
  and rebuild.** Alternate until no point can reach a lower dE.

The fold-back is the magic: the points the cube actually drives the panel to
(``cube(signal)``) are re-measured, and those ``(driven_signal, measured_xyz)``
pairs are added to the model's training set — so the next model is accurate
*exactly where the cube operates*, its inverse is more self-consistent, and the
residual shrinks. The loop converges as the model becomes true at its operating
points.

**The LLM adjudicates convergence** (the reason an LLM is in the loop at all): a
point that won't drop below the target is surfaced as a digest — "floor reached /
panel limit (accept) / worth another nudge" — never silently accepted or abandoned.

**Fidelity ladder (which ``probe`` you pass):** the probe is a single seam,
``measure(signals) -> measured_xyz``. Pass a software ground-truth model for a
preview/test (tier 1), the DWM-hook shader re-measure for the 3D LUT (tier 2 — and
for the 3D LUT the shader *is* production, so this is ground truth, fast), or the
installed-file re-measure for final verification (tier 3). The machine's math is
identical for all three — it samples the cube itself and measures the driven
signal, so it needs only that one capability.

Numpy/scipy/colour live in :mod:`dlc.engine`; importing this module pulls them
(it is the engine-tier orchestrator). The dependency-free spine never imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from .engine.lut_rbf import build_cube, cube_diagnostics, identity_cube, predicted_accuracy, write_cube
from .engine.model import DisplayErrorModel, Target, TargetSpace, de_itp

__all__ = [
    "ProbeFn",
    "OptimizeConfig",
    "IterationResult",
    "OptimizeResult",
    "sample_cube",
    "optimize_cube",
    "synthetic_probe",
]

# A probe drives the panel at ``signals`` (N, 3) in [0, 1] and returns the
# measured absolute XYZ (N, 3). The cube is sampled by the machine, so the probe
# is "raw" — it never knows about the LUT.
ProbeFn = Callable[[np.ndarray], np.ndarray]


@dataclass(frozen=True)
class OptimizeConfig:
    """Outer-loop tunables. The judgment-bearing ones (``threshold``,
    ``max_outer``, ``floor_tol``) are **LLM-deferred** — the orchestrator sets them
    per run / per the target (SDR: every patch < dE 2; HDR: LLM-negotiated)."""

    grid_size: int = 33
    threshold: float = 2.0          # per-patch dE_ITP convergence target (SDR: 2)
    max_outer: int = 4              # outer measure→fold→rebuild iterations
    floor_tol: float = 0.2          # dE improvement below this ⇒ a point is "stuck"
    top_k: int = 8                  # worst points to surface in the digest
    smoothing: Optional[float] = None   # None ⇒ per-iteration k-fold CV
    # build_cube knobs (defaults assume a post-MHC residual — small corrections):
    max_correction: float = 0.05
    n_inner_iterations: int = 3
    fade_width: float = 0.05
    near_black_nits: float = 0.1


@dataclass
class IterationResult:
    iteration: int
    measured_mean_de: float
    measured_p95_de: float
    measured_max_de: float
    predicted_max_de: float        # the model's own estimate (model vs reality gap)
    above_threshold: int
    worst: list[tuple[list[float], float]]   # [(signal, dE), …] top-k by dE
    smoothing: float
    cube_monotonic: bool
    train_points: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "measured_mean_de": round(self.measured_mean_de, 4),
            "measured_p95_de": round(self.measured_p95_de, 4),
            "measured_max_de": round(self.measured_max_de, 4),
            "predicted_max_de": round(self.predicted_max_de, 4),
            "model_reality_gap": round(self.measured_max_de - self.predicted_max_de, 4),
            "above_threshold": self.above_threshold,
            "smoothing": round(self.smoothing, 4),
            "cube_monotonic": self.cube_monotonic,
            "train_points": self.train_points,
            "worst": [[[round(c, 4) for c in s], round(d, 3)] for s, d in self.worst],
        }


@dataclass
class OptimizeResult:
    converged: bool
    iterations: int
    cube: np.ndarray
    grid_size: int
    final: IterationResult
    history: list[IterationResult]
    floor_points: list[tuple[list[float], float]]
    needs_adjudication: bool
    question: Optional[str]
    digest: dict[str, Any] = field(default_factory=dict)

    def write(self, path: str, *, title: Optional[str] = None) -> str:
        return write_cube(self.cube, path, title=title)


def sample_cube(cube: np.ndarray, signals: np.ndarray) -> np.ndarray:
    """Trilinearly sample the LUT (indexed ``[b, g, r]``) at input ``signals``
    (N, 3) → the corrected output signals the panel would be driven to."""
    grid_size = cube.shape[0]
    axis = np.linspace(0.0, 1.0, grid_size)
    interp = RegularGridInterpolator((axis, axis, axis), cube, method="linear",
                                     bounds_error=False, fill_value=None)
    signals = np.asarray(signals, dtype=float)
    out = interp(signals[:, [2, 1, 0]])  # cube indexed [B, G, R]
    return np.clip(out, 0.0, 1.0)


def optimize_cube(
    *,
    target: Target,
    probe: ProbeFn,
    signals: np.ndarray,
    measured_xyz: np.ndarray,
    verify_signals: Optional[np.ndarray] = None,
    config: Optional[OptimizeConfig] = None,
    on_iteration: Optional[Callable[[IterationResult], None]] = None,
) -> OptimizeResult:
    """Run the correction machine.

    ``signals`` / ``measured_xyz`` are the initial **raw** profiling measurements
    (item 2's output: panel response with no LUT). ``probe`` re-measures the panel
    at arbitrary driven signals (the fidelity-ladder seam). Returns the best cube
    plus a per-iteration history and an LLM-facing digest.

    Each outer iteration: fit a :class:`DisplayErrorModel` on all measurements so
    far → :func:`build_cube` (inner loop) → sample the cube at ``verify_signals`` →
    ``probe`` the driven points → score per-patch dE_ITP vs target → if not
    converged, **fold the (driven, measured) pairs back** into the training set and
    repeat. Floor = points that stay above ``threshold`` without improving.
    """

    cfg = config or OptimizeConfig()
    space = TargetSpace(target)

    train_signals = np.asarray(signals, dtype=float).reshape(-1, 3)
    train_xyz = np.asarray(measured_xyz, dtype=float).reshape(-1, 3)
    verify = (np.asarray(verify_signals, dtype=float).reshape(-1, 3)
              if verify_signals is not None else train_signals.copy())
    target_ictcp = space.ideal_ictcp(verify)

    history: list[IterationResult] = []
    best_cube = identity_cube(cfg.grid_size)
    best_max = float("inf")
    prev_de: Optional[np.ndarray] = None
    converged = False
    floor_mask = np.zeros(len(verify), dtype=bool)

    for it in range(1, cfg.max_outer + 1):
        model = DisplayErrorModel(train_signals, train_xyz, target, smoothing=cfg.smoothing)
        cube = build_cube(
            model, cfg.grid_size, signal_points=train_signals,
            fade_width=cfg.fade_width, max_correction=cfg.max_correction,
            n_iterations=cfg.n_inner_iterations, near_black_nits=cfg.near_black_nits,
        )

        driven = sample_cube(cube, verify)
        measured = np.maximum(np.asarray(probe(driven), dtype=float).reshape(-1, 3), 0.0)
        de = de_itp(space.xyz_to_ictcp(measured) - target_ictcp)

        pred = predicted_accuracy(model, cube, verify)
        diag = cube_diagnostics(cube)
        order = np.argsort(de)[::-1][: cfg.top_k]
        result = IterationResult(
            iteration=it,
            measured_mean_de=float(de.mean()),
            measured_p95_de=float(np.percentile(de, 95)),
            measured_max_de=float(de.max()),
            predicted_max_de=float(pred["max"]),
            above_threshold=int(np.sum(de > cfg.threshold)),
            worst=[(verify[i].tolist(), float(de[i])) for i in order],
            smoothing=float(model.smoothing),
            cube_monotonic=diag.monotonic,
            train_points=int(len(train_signals)),
        )
        history.append(result)
        if on_iteration is not None:
            on_iteration(result)

        if result.measured_max_de < best_max:
            best_max = result.measured_max_de
            best_cube = cube

        if de.max() < cfg.threshold:
            converged = True
            break

        # Floor detection: above-threshold points that stopped improving.
        if prev_de is not None:
            stuck = (de > cfg.threshold) & ((prev_de - de) < cfg.floor_tol)
            floor_mask = stuck
            if np.all((de <= cfg.threshold) | stuck):
                break  # everything is either good or at its floor — re-measure won't help
        prev_de = de

        # Fold reality back: the driven points + their true response are new
        # ground truth about the panel where the cube actually operates.
        train_signals = np.vstack([train_signals, driven])
        train_xyz = np.vstack([train_xyz, measured])

    final = history[-1]
    # Best-cube error at verify (the cube we actually return):
    best_driven = sample_cube(best_cube, verify)
    best_de = de_itp(space.xyz_to_ictcp(np.maximum(np.asarray(probe(best_driven), float).reshape(-1, 3), 0.0)) - target_ictcp)
    floor_points = [(verify[i].tolist(), float(best_de[i]))
                    for i in np.where(best_de > cfg.threshold)[0]]
    floor_points.sort(key=lambda p: p[1], reverse=True)

    needs_adjudication = not converged and bool(floor_points)
    question = None
    if needs_adjudication:
        worst_sig, worst_de = floor_points[0]
        question = (
            f"{len(floor_points)} patch(es) stay above dE {cfg.threshold:g} after "
            f"{len(history)} outer iteration(s) (worst dE {worst_de:.1f} at signal "
            f"{[round(c, 3) for c in worst_sig]}) — accept as the panel's physical floor, "
            f"or raise the iteration budget / loosen the target?"
        )

    digest = {
        "converged": converged,
        "iterations": len(history),
        "grid_size": cfg.grid_size,
        "threshold": cfg.threshold,
        "best_max_de": round(float(best_de.max()), 4),
        "best_mean_de": round(float(best_de.mean()), 4),
        "best_p95_de": round(float(np.percentile(best_de, 95)), 4),
        "above_threshold": int(np.sum(best_de > cfg.threshold)),
        "floor_points": len(floor_points),
        "cube_monotonic": cube_diagnostics(best_cube).monotonic,
        "needs_adjudication": needs_adjudication,
        "history": [h.as_dict() for h in history],
    }

    return OptimizeResult(
        converged=converged,
        iterations=len(history),
        cube=best_cube,
        grid_size=cfg.grid_size,
        final=final,
        history=history,
        floor_points=floor_points[: cfg.top_k],
        needs_adjudication=needs_adjudication,
        question=question,
        digest=digest,
    )


# ---------------------------------------------------------------------------
# Synthetic ground-truth probe (no hardware) — tier-1 stand-in for previews and
# tests; mirrors the engine's synthetic-panel style.
# ---------------------------------------------------------------------------

def synthetic_probe(
    target: Target,
    *,
    gains: tuple[float, float, float] = (1.0, 1.008, 1.018),
    gammas: tuple[float, float, float] = (1.0, 1.0, 1.0),
    cross: float = 0.0,
    noise: float = 0.0,
    seed: int = 11,
) -> ProbeFn:
    """A deterministic ground-truth panel: it applies a per-channel
    gain+gamma (and optional cross-channel leak) to the driven signal, then emits
    the *ideal* XYZ of that distorted signal. The correction machine should invert
    it. Gains ≥ 1 keep every correction feasible (no full-white ceiling), so the
    loop converges cleanly; raise a gain above what the LUT can pull down to test
    floor detection. Pure/deterministic (optional seeded gaussian noise)."""

    space = TargetSpace(target)
    g = np.asarray(gains, dtype=float)
    gam = np.asarray(gammas, dtype=float)
    rng = np.random.RandomState(seed)

    def probe(signals: np.ndarray) -> np.ndarray:
        s = np.clip(np.asarray(signals, dtype=float).reshape(-1, 3), 0.0, 1.0)
        s_eff = g * (s ** gam)
        if cross:
            leak = cross * (s.sum(axis=1, keepdims=True) - s) / 2.0
            s_eff = s_eff + leak
        s_eff = np.clip(s_eff, 0.0, 1.0)
        xyz = space.ideal_xyz(s_eff)
        if noise:
            xyz = xyz * (1.0 + noise * rng.standard_normal(xyz.shape))
        return np.maximum(xyz, 0.0)

    return probe
