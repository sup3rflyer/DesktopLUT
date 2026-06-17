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
residual shrinks.

**Correction budget — derived and self-disambiguating (not a hand-tuned guess).**
The inner builder soft-clamps each node's correction to ``max_correction`` (signal
space) to protect gamut edges and reject model noise. A fixed default is dangerous:
too tight and the machine reports *clamp-limited* points as if they were the panel's
physical floor (a false floor — "accept this, the panel can't do better" when really
a bigger budget fixes it). So this loop:

1. **Seeds** ``max_correction`` from the *measured* residual (a high percentile of
   the per-channel signal correction the panel actually needs × a safety factor),
   not a constant.
2. **Auto-escalates** the budget when stuck points are *clamp-limited with signal
   headroom*, up to a cap, before ever calling anything a floor.
3. **Disambiguates** every above-threshold point into ``signal_clipped`` (driven
   channel already at 0/1 — a real physical floor), ``budget_limited`` (clamp
   binding but signal interior — raise the budget), or ``residual`` (interior, clamp
   slack, still off — model/measurement floor). Only the real floors reach the LLM's
   adjudication question; a tuning limit never masquerades as a panel limit.

**The LLM adjudicates** the real floors (the reason an LLM is in the loop): "floor
reached / panel limit (accept) / worth another nudge" — never silently accepted.

**Fidelity ladder (which ``probe`` you pass):** the probe is a single seam,
``measure(signals) -> measured_xyz``. Pass a software ground-truth model for a
preview/test (tier 1), the DWM-hook shader re-measure for the 3D LUT (tier 2 — and
for the 3D LUT the shader *is* production, so this is ground truth, fast), or the
installed-file re-measure for final verification (tier 3). The machine samples the
cube itself and measures the driven signal, so it needs only that one capability.

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
    "DegenerateMeasurements",
    "sample_cube",
    "seed_correction_budget",
    "optimize_cube",
    "synthetic_probe",
]


class DegenerateMeasurements(Exception):
    """The measurement set cannot support an RBF correction model — duplicate or collinear
    signals make the interpolation matrix singular (``numpy.linalg.LinAlgError`` from the RBF
    solve). Raised at the optimize boundary so the orchestrator surfaces a clear
    'measurements degenerate — re-measure' outcome instead of crashing the whole run."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

# A probe drives the panel at ``signals`` (N, 3) in [0, 1] and returns the
# measured absolute XYZ (N, 3). The cube is sampled by the machine, so the probe
# is "raw" — it never knows about the LUT.
ProbeFn = Callable[[np.ndarray], np.ndarray]


@dataclass(frozen=True)
class OptimizeConfig:
    """Outer-loop tunables. The judgment-bearing ones (``threshold``,
    ``max_outer``, ``floor_tol``) are **LLM-deferred** — the orchestrator sets them
    per run / per target (SDR: every patch < dE 2; HDR: LLM-negotiated).

    ``max_correction`` defaults to ``None`` ⇒ **derive it from the measured
    residual** (:func:`seed_correction_budget`). Set an explicit value only to pin
    the budget (e.g. to honour a known small post-MHC residual). ``auto_escalate``
    raises the budget toward ``max_correction_cap`` while points are clamp-limited
    with signal headroom, so a too-small budget self-corrects instead of producing
    a false floor.
    """

    grid_size: int = 33
    threshold: float = 2.0          # per-patch dE_ITP convergence target (SDR: 2)
    max_outer: int = 6              # outer measure→(escalate)→fold→rebuild iterations
    floor_tol: float = 0.2          # dE improvement below this ⇒ a stuck point is "not improving"

    # Correction budget (signal-space soft-clamp on the inner builder) ------
    max_correction: Optional[float] = None   # None ⇒ seed from the measured residual
    auto_escalate: bool = True
    max_correction_cap: float = 0.25         # ceiling for the seed and escalation
    correction_floor: float = 0.01           # smallest sane budget
    correction_safety: float = 1.5           # seed = percentile(needed) × safety
    seed_percentile: float = 98.0
    escalate_factor: float = 1.6             # budget × this when clamp-limited persists
    clamp_active_frac: float = 0.85          # |correction| ≥ frac×budget ⇒ clamp binding
    boundary_eps: float = 2e-3               # driven channel within eps of 0/1 ⇒ clipped

    top_k: int = 8                  # worst points to surface in the digest
    smoothing: Optional[float] = None   # None ⇒ per-iteration k-fold CV
    n_inner_iterations: int = 3
    fade_width: float = 0.05
    near_black_nits: float = 0.1


@dataclass
class IterationResult:
    iteration: int
    max_correction: float          # the correction budget used this iteration
    measured_mean_de: float
    measured_p95_de: float
    measured_max_de: float
    predicted_max_de: float        # the model's own estimate (model vs reality gap)
    above_threshold: int
    budget_limited: int            # clamp binding + signal headroom (→ raise budget)
    signal_clipped: int            # driven channel at 0/1 (→ physical floor)
    residual: int                  # interior, clamp slack, still off (→ model floor)
    worst: list[tuple[list[float], float]]   # [(signal, dE), …] top-k by dE
    smoothing: float
    cube_monotonic: bool
    train_points: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "max_correction": round(self.max_correction, 4),
            "measured_mean_de": round(self.measured_mean_de, 4),
            "measured_p95_de": round(self.measured_p95_de, 4),
            "measured_max_de": round(self.measured_max_de, 4),
            "predicted_max_de": round(self.predicted_max_de, 4),
            "model_reality_gap": round(self.measured_max_de - self.predicted_max_de, 4),
            "above_threshold": self.above_threshold,
            "budget_limited": self.budget_limited,
            "signal_clipped": self.signal_clipped,
            "residual": self.residual,
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
    max_correction: float          # final budget that produced the returned cube
    final: IterationResult
    history: list[IterationResult]
    floor_points: list[tuple[list[float], float]]   # real (non-budget) floors, worst first
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


def seed_correction_budget(space: TargetSpace, signals: np.ndarray, measured_xyz: np.ndarray,
                           *, safety: float = 1.5, percentile: float = 98.0,
                           floor: float = 0.01, cap: float = 0.25) -> float:
    """Size the correction budget from the *measured* residual, in signal space.

    The signal that would ideally produce ``measured_xyz`` is the panel's "apparent
    signal"; its gap from the driven signal is roughly the correction the panel
    demands. A high percentile of that (× safety), clamped to ``[floor, cap]``,
    sizes the budget to the panel instead of guessing a constant.
    """
    signals = np.asarray(signals, dtype=float).reshape(-1, 3)
    apparent = space.xyz_to_signal(np.maximum(np.asarray(measured_xyz, float).reshape(-1, 3), 0.0))
    needed = np.max(np.abs(apparent - signals), axis=1)
    p = float(np.percentile(needed, percentile)) if needed.size else floor
    return float(np.clip(p * safety, floor, cap))


def _classify(verify: np.ndarray, driven: np.ndarray, de: np.ndarray,
              threshold: float, budget: float, *, clamp_frac: float, boundary_eps: float):
    """Bucket above-threshold points: signal_clipped (boundary), budget_limited
    (clamp binding, interior), residual (interior, clamp slack)."""
    above = de > threshold
    corr_mag = np.max(np.abs(driven - verify), axis=1)
    at_boundary = np.any((driven <= boundary_eps) | (driven >= 1.0 - boundary_eps), axis=1)
    clamp_active = corr_mag >= clamp_frac * budget
    signal_clipped = above & at_boundary
    budget_limited = above & ~at_boundary & clamp_active
    residual = above & ~at_boundary & ~clamp_active
    return {"above": above, "signal_clipped": signal_clipped,
            "budget_limited": budget_limited, "residual": residual}


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
    plus a per-iteration history and an LLM-facing digest that separates real floors
    from a too-small correction budget.
    """

    cfg = config or OptimizeConfig()
    space = TargetSpace(target)

    train_signals = np.asarray(signals, dtype=float).reshape(-1, 3)
    train_xyz = np.maximum(np.asarray(measured_xyz, dtype=float).reshape(-1, 3), 0.0)
    verify = (np.asarray(verify_signals, dtype=float).reshape(-1, 3)
              if verify_signals is not None else train_signals.copy())
    target_ictcp = space.ideal_ictcp(verify)

    # Budget: seed from the measured residual unless pinned.
    budget = (cfg.max_correction if cfg.max_correction is not None
              else seed_correction_budget(space, train_signals, train_xyz,
                                          safety=cfg.correction_safety,
                                          percentile=cfg.seed_percentile,
                                          floor=cfg.correction_floor,
                                          cap=cfg.max_correction_cap))

    history: list[IterationResult] = []
    snapshots: list[dict[str, Any]] = []   # cached per-iter measurements (no extra probing)
    best_seen_max: Optional[float] = None  # best worst-case dE so far (noise-robust stop)
    converged = False

    for it in range(1, cfg.max_outer + 1):
        try:
            model = DisplayErrorModel(train_signals, train_xyz, target, smoothing=cfg.smoothing)
            cube = build_cube(
                model, cfg.grid_size, signal_points=train_signals,
                fade_width=cfg.fade_width, max_correction=budget,
                n_iterations=cfg.n_inner_iterations, near_black_nits=cfg.near_black_nits,
            )
        except np.linalg.LinAlgError as exc:
            if snapshots:
                # A later iteration went singular — typically the fold-back stacked duplicate
                # or collinear driven points onto the training set. Keep the best cube built so
                # far rather than crashing; the loop's job is done.
                break
            # First build failed: there is no usable model at all. Convert the raw numpy error
            # into a typed, actionable signal for the orchestrator (re-measure with more variation).
            raise DegenerateMeasurements(
                f"the {len(train_signals)} profiling measurement(s) cannot build an RBF "
                f"correction model (singular interpolation matrix: {exc}). The patch set is "
                f"degenerate — duplicate or collinear signals. Re-measure with more signal "
                f"variation (a fuller volumetric/ramp set), then retry.") from exc
        driven = sample_cube(cube, verify)
        measured = np.maximum(np.asarray(probe(driven), dtype=float).reshape(-1, 3), 0.0)
        de = de_itp(space.xyz_to_ictcp(measured) - target_ictcp)

        masks = _classify(verify, driven, de, cfg.threshold, budget,
                          clamp_frac=cfg.clamp_active_frac, boundary_eps=cfg.boundary_eps)
        diag = cube_diagnostics(cube)
        order = np.argsort(de)[::-1][: cfg.top_k]
        result = IterationResult(
            iteration=it, max_correction=budget,
            measured_mean_de=float(de.mean()), measured_p95_de=float(np.percentile(de, 95)),
            measured_max_de=float(de.max()),
            predicted_max_de=float(predicted_accuracy(model, cube, verify)["max"]),
            above_threshold=int(masks["above"].sum()),
            budget_limited=int(masks["budget_limited"].sum()),
            signal_clipped=int(masks["signal_clipped"].sum()),
            residual=int(masks["residual"].sum()),
            worst=[(verify[i].tolist(), float(de[i])) for i in order],
            smoothing=float(model.smoothing), cube_monotonic=diag.monotonic,
            train_points=int(len(train_signals)),
        )
        history.append(result)
        snapshots.append({"cube": cube, "driven": driven, "measured": measured, "de": de,
                          "budget": budget, "monotonic": diag.monotonic})
        if on_iteration is not None:
            on_iteration(result)

        cur_max = float(de.max())
        if cur_max < cfg.threshold:
            converged = True
            break

        # Escalating the budget is a real lever (clamp-limited points with signal headroom),
        # not noise — keep doing it ahead of any stall test.
        escalating = (cfg.auto_escalate and masks["budget_limited"].any()
                      and budget < cfg.max_correction_cap)
        if escalating:
            budget = min(cfg.max_correction_cap, budget * cfg.escalate_factor)
        elif best_seen_max is not None and cur_max > best_seen_max - cfg.floor_tol:
            # No budget headroom AND this iteration did not improve the best worst-case error
            # by at least floor_tol (the measurement-noise band). Folding its driven/measured
            # pairs would just inject noise into the next model — stop and keep the best cube.
            # (The old per-point "still improving" guard oscillated under noise: some point
            # always wiggled by floor_tol, so the loop ran to the cap and folded noisy pairs.)
            break

        best_seen_max = cur_max if best_seen_max is None else min(best_seen_max, cur_max)
        # Fold reality back: the driven points + their true response are new ground truth where
        # the cube actually operates. Only reached when escalating or genuinely improving — a
        # non-improving (likely noisy) iteration breaks above WITHOUT polluting the model.
        train_signals = np.vstack([train_signals, driven])
        train_xyz = np.vstack([train_xyz, measured])

    # Pick the cube to return: prefer monotonic, then lowest worst-case dE — using
    # the cached measurements (no extra probing).
    def _rank(s: dict[str, Any]) -> tuple[int, float]:
        return (0 if s["monotonic"] else 1, float(s["de"].max()))
    best = min(snapshots, key=_rank)
    best_cube, best_de, best_driven, best_budget = (best["cube"], best["de"],
                                                    best["driven"], best["budget"])
    final = history[-1]

    masks_best = _classify(verify, best_driven, best_de, cfg.threshold, best_budget,
                           clamp_frac=cfg.clamp_active_frac, boundary_eps=cfg.boundary_eps)
    # Real floors = physically clipped + model residual (more budget won't help).
    real_floor = masks_best["signal_clipped"] | masks_best["residual"]
    budget_limited = masks_best["budget_limited"]
    floor_points = [(verify[i].tolist(), float(best_de[i])) for i in np.where(real_floor)[0]]
    floor_points.sort(key=lambda p: p[1], reverse=True)

    needs_adjudication = bool(real_floor.any() or budget_limited.any())
    question = None
    if needs_adjudication:
        parts: list[str] = []
        if real_floor.any():
            worst_sig, worst_de = floor_points[0]
            parts.append(
                f"{int(real_floor.sum())} patch(es) at the panel's physical floor "
                f"(worst dE {worst_de:.1f} at signal {[round(c, 3) for c in worst_sig]}, "
                f"channel at full scale) — accept as the panel limit, or loosen the target?"
            )
        if budget_limited.any():
            parts.append(
                f"{int(budget_limited.sum())} patch(es) still need a correction beyond the "
                f"budget cap ({cfg.max_correction_cap:g}); raise the cap (the 3D LUT is doing "
                f"more than a post-MHC residual) or run MHC first."
            )
        question = " ".join(parts)

    digest = {
        "converged": converged,
        "iterations": len(history),
        "grid_size": cfg.grid_size,
        "threshold": cfg.threshold,
        "max_correction": round(best_budget, 4),
        "best_max_de": round(float(best_de.max()), 4),
        "best_mean_de": round(float(best_de.mean()), 4),
        "best_p95_de": round(float(np.percentile(best_de, 95)), 4),
        "above_threshold": int(masks_best["above"].sum()),
        "physical_floor": int(real_floor.sum()),
        "budget_limited": int(budget_limited.sum()),
        "cube_monotonic": bool(best["monotonic"]),
        "needs_adjudication": needs_adjudication,
        "history": [h.as_dict() for h in history],
    }

    return OptimizeResult(
        converged=converged, iterations=len(history), cube=best_cube, grid_size=cfg.grid_size,
        max_correction=best_budget, final=final, history=history,
        floor_points=floor_points[: cfg.top_k], needs_adjudication=needs_adjudication,
        question=question, digest=digest,
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
    loop converges cleanly; a gain < 1 needs the cube to push past full scale at
    bright signals, which clips — a genuine physical floor (to test floor
    detection). Pure/deterministic (optional seeded gaussian noise)."""

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
