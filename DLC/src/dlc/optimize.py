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
3. **Disambiguates** every above-threshold point into ``signal_clipped`` (a
   correction actually pushed a channel into a 0/1 rail — a physical limit),
   ``budget_limited`` (clamp binding but signal interior — raise the budget), or
   ``residual`` (interior, clamp slack, still off — model/measurement floor).
   Near-black points are counted separately so the most sensitive region stays
   visible rather than being dismissed as uninteresting floor noise.

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
from typing import Any, Callable, Literal, Optional

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from .engine.lut_constrained import build_constrained_rbf_cube
from .engine.lut_rbf import build_cube, cube_diagnostics, identity_cube, predicted_accuracy, write_cube
from .engine.model import DisplayErrorModel, Target, TargetSpace, de_itp
from .engine.physical import StructuredForwardModel, build_physical_cube

__all__ = [
    "ProbeFn",
    "OptimizeConfig",
    "IterationResult",
    "OptimizeResult",
    "DegenerateMeasurements",
    "SDR_CORRECTION_CAP",
    "aggregate_training_samples",
    "sample_cube",
    "seed_correction_budget",
    "optimize_cube",
    "synthetic_probe",
]

# Mode-aware ceiling for the data-derived correction budget (the orchestrator picks per mode;
# :class:`OptimizeConfig` defaults to the HDR value). HDR: the cube is a small POST-MHC residual
# (the 1D MHC base owns the neutral EOTF + per-level WB, the matrix owns native→D65) so 0.25 is
# right. SDR: the MHC does gamut+white ONLY, so the cube owns ALL the colour — the whole
# native→target gamut compression — and a 0.25 ceiling artificially starves the (gamut-aware,
# data-derived) seed budget on a wide-gamut panel. Offline held-out CV on the PA32UCXR's post-MHC
# reads (HANDOFF item H) shows the saturated-corner benefit plateaus by ~0.5 (corner-mean CIEDE2000
# 3.16→2.90, neutral axis unchanged, 0.5≡1.0), so 0.5 captures the win without over-driving. NB the
# cap is a MODEST lever: the worst corner (pure blue) is a panel/MHC gamut floor the cube cannot fix
# at any cap. See dlc.calibrate.Calibration._cube_optimize_config.
SDR_CORRECTION_CAP = 0.5


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
# Optional re-scorer for the LLM/user-facing numbers ONLY: (signal_rgb, measured_xyz) -> per-patch
# dE in the REPORT metric. The cube always CONVERGES in dE_ITP (the perceptually-uniform space the
# RBF inverts); this just relabels the surfaced digest/curve so an SDR run never shows dE_ITP as if it
# were CIEDE2000. ``None`` ⇒ the report metric IS dE_ITP (HDR / back-compat: numbers unchanged).
ReportScorer = Callable[[np.ndarray, np.ndarray], np.ndarray]


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
    low_light_signal: float = 0.08  # permanent near-black accounting; do not discard these
    adaptive_sampling: bool = True  # focus early outer probes, then force full validation
    adaptive_min_full: int = 256    # small sets are cheaper/safer to read in full
    adaptive_full_after: int = 3    # focused, wider, then full validation
    adaptive_initial_worst: int = 96
    adaptive_widen_factor: float = 2.0
    adaptive_neighbors: int = 2
    adaptive_low_light_cap: int = 128
    adaptive_sentinels: int = 96
    smoothing: Optional[float] = None   # None ⇒ per-iteration k-fold CV
    confidence_weighted_rbf: bool = True  # duplicate/skeleton samples lower local RBF smoothing
    n_inner_iterations: int = 3
    fade_width: float = 0.05
    near_black_nits: float = 0.1
    # Neutral-axis preservation: fade the cube's correction to identity as the input nears the grey
    # diagonal (R==G==B). The DWM 3D LUT feeds the MHC ICC, and the MHC owns the grey/white axis
    # (1+1+1), so the cube must own colour only in that stacked path; this stops it re-touching neutral (the white
    # 0.99→4.56 HW regression). ``0`` ⇒ off — correct only for a raw panel with no MHC foundation
    # (e.g. the synthetic-correction unit tests), where the cube legitimately must fix grey too.
    neutral_band: float = 0.05
    # Candidate engine selector. "rbf" is the shipping path. "constrained-rbf" and "physical"
    # are labelled experiments: held-out CV rejected the constrained shell and the additive
    # structured model as post-MHC/post-ICC cube replacements. Keep them opt-in for probes only.
    engine: Literal["rbf", "constrained-rbf", "physical"] = "rbf"
    constrained_metric: Literal["auto", "de2000", "de_itp"] = "auto"
    constrained_hue_tolerance_degrees: float = 4.0
    constrained_purity_slack: float = 1.03
    constrained_shell_saturation: float = 0.68
    constrained_shell_pressure: float = 0.20
    constrained_screen_error_threshold: float = 1.0
    constrained_off_channel_guard_pressure: float = 0.75
    constrained_gamut_blend_strength: float = 0.65
    constrained_maxiter: int = 40
    constrained_n_jobs: int = 1
    constrained_chunk_size: int = 128
    physical_metric: Literal["auto", "de2000", "de_itp"] = "auto"
    physical_uncertainty_radius: float = 0.18
    physical_hue_tolerance_degrees: float = 4.0
    physical_purity_slack: float = 1.03
    # Sequential/MHC-stacked default is False: MHC remains standalone-complete and owns exact
    # neutral. A later co-optimized shaper+cube path can opt in after neutral-dense validation.
    allow_neutral_refinement: bool = False


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
    near_black: int                # above-threshold low-signal points (tracked, never ignored)
    residual: int                  # interior, clamp slack, still off (→ model floor)
    worst: list[tuple[list[float], float]]   # [(signal, dE), …] top-k by dE
    smoothing: float
    cube_monotonic: bool
    large_reversals: int
    worst_lattice_jump: float
    train_points: int
    probed_patches: int
    probe_total: int
    sampling_mode: str
    # The metric the measured/predicted dE above are expressed in (display label). The loop always
    # CONVERGES in dE_ITP; this is "CIEDE2000" for an SDR run whose surfaced numbers were re-scored,
    # else "dE_ITP". Defaulted so any external constructor / older caller stays valid.
    metric: str = "dE_ITP"

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "max_correction": round(self.max_correction, 4),
            "metric": self.metric,
            "measured_mean_de": round(self.measured_mean_de, 4),
            "measured_p95_de": round(self.measured_p95_de, 4),
            "measured_max_de": round(self.measured_max_de, 4),
            "predicted_max_de": round(self.predicted_max_de, 4),
            "model_reality_gap": round(self.measured_max_de - self.predicted_max_de, 4),
            "above_threshold": self.above_threshold,
            "budget_limited": self.budget_limited,
            "signal_clipped": self.signal_clipped,
            "near_black": self.near_black,
            "residual": self.residual,
            "smoothing": round(self.smoothing, 4),
            "cube_monotonic": self.cube_monotonic,
            "large_reversals": self.large_reversals,
            "worst_lattice_jump": round(self.worst_lattice_jump, 4),
            "train_points": self.train_points,
            "worst": [[[round(c, 4) for c in s], round(d, 3)] for s, d in self.worst],
            "probed_patches": self.probed_patches,
            "probe_total": self.probe_total,
            "sample_fraction": round(self.probed_patches / self.probe_total, 4)
                               if self.probe_total else 0.0,
            "sampling_mode": self.sampling_mode,
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


def aggregate_training_samples(signals: np.ndarray, measured_xyz: np.ndarray,
                               sample_confidence: Optional[np.ndarray] = None,
                               *, decimals: int = 6) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Average duplicate measurement rows and return per-point confidence.

    Repeated start/end skeleton sweeps should not become contradictory near-duplicate
    RBF knots. They become one high-SNR training point: XYZ is averaged, and the summed
    repeat count becomes ``sample_confidence`` so :class:`DisplayErrorModel` lowers local
    smoothing there while the ordinary interior remains smoothed.
    """
    sig = np.asarray(signals, dtype=float).reshape(-1, 3)
    xyz = np.asarray(measured_xyz, dtype=float).reshape(-1, 3)
    if sig.shape != xyz.shape:
        raise ValueError("signals and measured_xyz must have matching (N, 3) shapes")
    if sample_confidence is None:
        conf = np.ones(len(sig), dtype=float)
    else:
        conf = np.asarray(sample_confidence, dtype=float).reshape(-1)
        if conf.shape[0] != len(sig):
            raise ValueError("sample_confidence must be length N")
        if not np.all(np.isfinite(conf)) or np.any(conf <= 0.0):
            raise ValueError("sample_confidence must contain finite positive values")

    groups: dict[tuple[float, float, float], list[Any]] = {}
    order: list[tuple[float, float, float]] = []
    rounded = np.round(sig, decimals=decimals)
    for i, key_arr in enumerate(rounded):
        key = tuple(float(v) for v in key_arr)
        if key not in groups:
            groups[key] = [np.zeros(3, dtype=float), np.zeros(3, dtype=float), 0.0]
            order.append(key)
        groups[key][0] += sig[i] * conf[i]
        groups[key][1] += xyz[i] * conf[i]
        groups[key][2] += float(conf[i])

    out_sig: list[np.ndarray] = []
    out_xyz: list[np.ndarray] = []
    out_conf: list[float] = []
    for key in order:
        sum_sig, sum_xyz, sum_conf = groups[key]
        out_sig.append(sum_sig / sum_conf)
        out_xyz.append(sum_xyz / sum_conf)
        out_conf.append(float(sum_conf))
    return np.vstack(out_sig), np.vstack(out_xyz), np.asarray(out_conf, dtype=float)


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
              threshold: float, budget: float, *, clamp_frac: float,
              boundary_eps: float, low_light_signal: float):
    """Bucket above-threshold points.

    Boundary classification is directional. A naturally saturated patch such as
    blue ``[0, 0, 0.75]`` should not be called a physical floor just because its
    unused red/green channels are already zero. It is clipped only when the cube
    actually pushes a nonzero channel down to zero, pushes a channel up to one, or
    the requested signal is already at full scale and remains above threshold.
    """
    above = de > threshold
    corr_mag = np.max(np.abs(driven - verify), axis=1)
    low_clipped = np.any((driven <= boundary_eps) & (verify > boundary_eps), axis=1)
    high_clipped = np.any((driven >= 1.0 - boundary_eps)
                          & ((verify >= 1.0 - boundary_eps)
                             | (driven > verify + boundary_eps)), axis=1)
    at_boundary = low_clipped | high_clipped
    clamp_active = corr_mag >= clamp_frac * budget
    near_black = above & (np.max(verify, axis=1) <= low_light_signal)
    signal_clipped = above & at_boundary
    budget_limited = above & ~at_boundary & clamp_active
    residual = above & ~at_boundary & ~clamp_active
    return {"above": above, "signal_clipped": signal_clipped,
            "budget_limited": budget_limited, "residual": residual,
            "near_black": near_black, "low_clipped": above & low_clipped,
            "high_clipped": above & high_clipped}


def _top_unique_by_score(indices: np.ndarray, scores: np.ndarray, cap: int) -> np.ndarray:
    """Keep at most ``cap`` unique indices, preferring higher-score entries."""
    unique = np.unique(indices.astype(int))
    if cap <= 0 or unique.size <= cap:
        return unique
    order = np.argsort(scores[unique])[::-1][:cap]
    return unique[order]


def _adaptive_probe_indices(verify: np.ndarray, scores: np.ndarray, *, iteration: int,
                            cfg: OptimizeConfig, force_full: bool) -> tuple[np.ndarray, str]:
    """Choose which verification signals to probe this outer iteration.

    The first large-set iterations are active-learning passes: worst known errors,
    their local neighbours, a permanent near-black spine, and global sentinels.
    A full pass is still forced at milestones so the final cube is judged against
    the whole verification set.
    """
    total = len(verify)
    if (not cfg.adaptive_sampling or total <= cfg.adaptive_min_full or force_full
            or iteration >= cfg.adaptive_full_after):
        return np.arange(total, dtype=int), "full"

    widen = cfg.adaptive_widen_factor ** max(0, iteration - 1)
    worst_n = min(total, max(1, int(round(cfg.adaptive_initial_worst * widen))))
    worst = np.argsort(scores)[::-1][:worst_n]

    picks: list[np.ndarray] = [worst]
    if cfg.adaptive_neighbors > 0 and worst.size:
        # Signal-space local neighbourhoods catch the "fix one patch, bend nearby
        # patches" effect without paying for the whole cube immediately.
        dist = np.linalg.norm(verify[:, None, :] - verify[worst][None, :, :], axis=2)
        near = np.argsort(dist, axis=0)[: cfg.adaptive_neighbors + 1, :].reshape(-1)
        picks.append(near)

    low = np.where(np.max(verify, axis=1) <= cfg.low_light_signal)[0]
    if low.size:
        picks.append(_top_unique_by_score(low, scores, cfg.adaptive_low_light_cap))

    if cfg.adaptive_sentinels > 0:
        # Deterministic canaries across the existing thermally-ordered sequence.
        sent = np.linspace(0, total - 1, min(total, cfg.adaptive_sentinels), dtype=int)
        picks.append(sent)
        neutral = np.where(np.max(verify, axis=1) - np.min(verify, axis=1) <= 0.015)[0]
        if neutral.size:
            picks.append(_top_unique_by_score(neutral, scores, max(8, cfg.adaptive_sentinels // 4)))

    selected = np.unique(np.concatenate(picks).astype(int))
    selected.sort()  # preserve the run's thermal ordering as much as possible
    return selected, "focused" if iteration == 1 else "widened"


def optimize_cube(
    *,
    target: Target,
    probe: ProbeFn,
    signals: np.ndarray,
    measured_xyz: np.ndarray,
    verify_signals: Optional[np.ndarray] = None,
    config: Optional[OptimizeConfig] = None,
    on_iteration: Optional[Callable[[IterationResult], None]] = None,
    reachable_primaries: Any = None,
    report_scorer: Optional[ReportScorer] = None,
    report_metric: str = "dE_ITP",
) -> OptimizeResult:
    """Run the correction machine.

    ``signals`` / ``measured_xyz`` are the initial **raw** profiling measurements
    (item 2's output: panel response with no LUT). ``probe`` re-measures the panel
    at arbitrary driven signals (the fidelity-ladder seam). Returns the best cube
    plus a per-iteration history and an LLM-facing digest that separates real floors
    from a too-small correction budget.

    The loop always **converges in dE_ITP** (the perceptually-uniform space the RBF
    inverts) — convergence, classification, the correction budget, and the returned-cube
    ranking are unchanged regardless of ``report_scorer``. ``report_scorer`` only re-scores
    the **surfaced** numbers (the per-iteration curve, the digest, the seam question) into
    the run's report metric so an SDR build never feeds the LLM/user dE_ITP as if it were
    CIEDE2000; ``report_metric`` labels them. ``None`` ⇒ report metric IS dE_ITP (HDR /
    back-compat: every surfaced number is byte-identical to before).
    """

    cfg = config or OptimizeConfig()

    def _report_de(signal_rgb: np.ndarray, measured_xyz: np.ndarray,
                   fallback_itp: np.ndarray) -> np.ndarray:
        """Per-patch dE in the REPORT metric (display only). ``None`` scorer ⇒ the dE_ITP
        fallback (the optimize metric). A misbehaving scorer must never break a multi-hour
        build, so any failure / shape-or-finiteness mismatch falls back to dE_ITP."""
        if report_scorer is None:
            return fallback_itp
        try:
            out = np.asarray(report_scorer(np.asarray(signal_rgb, dtype=float).reshape(-1, 3),
                                           np.asarray(measured_xyz, dtype=float).reshape(-1, 3)),
                             dtype=float).reshape(-1)
        except Exception:  # noqa: BLE001 - display metric must never break the build
            return fallback_itp
        if out.shape[0] != fallback_itp.shape[0] or not np.all(np.isfinite(out)):
            return fallback_itp
        return out
    # reachable_primaries (the panel's measured native gamut) clamps the ideal target onto what the
    # panel can physically render, so build + verify score a gamut clip as a clip, not chase it (#C3).
    space = TargetSpace(target, reachable_primaries=reachable_primaries)

    raw_train_count = int(np.asarray(signals).reshape(-1, 3).shape[0])
    train_signals = np.asarray(signals, dtype=float).reshape(-1, 3)
    train_xyz = np.maximum(np.asarray(measured_xyz, dtype=float).reshape(-1, 3), 0.0)
    train_signals, train_xyz, train_confidence = aggregate_training_samples(train_signals, train_xyz)
    if len(train_signals) < 4:
        raise DegenerateMeasurements(
            f"the {raw_train_count} profiling measurement(s) collapsed to "
            f"{len(train_signals)} unique signal point(s) after averaging repeated reads; "
            "an RBF correction model needs at least 4 non-coplanar signal points. "
            "Re-measure with more signal variation (a fuller volumetric/ramp set), then retry.")
    verify = (np.asarray(verify_signals, dtype=float).reshape(-1, 3)
              if verify_signals is not None else train_signals.copy())
    target_ictcp = space.ideal_ictcp(verify)
    if len(verify) == len(train_signals):
        score_hint = de_itp(space.xyz_to_ictcp(train_xyz) - target_ictcp)
    else:
        # A caller-supplied verification lattice may not have pre-LUT measurements.
        # Fall back to a shape-based first pass (sentinels/near-black/neighbours);
        # subsequent probes update these hints with measured dE.
        score_hint = np.zeros(len(verify), dtype=float)

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
    force_full_probe = False
    # Model/cube build cache. The build is deterministic in (training set, budget) — auto_smooth
    # uses a fixed CV seed — so when an iteration neither folded new measurements nor escalated
    # the budget (the focused-pass → force-full-validation path), rebuilding would recompute the
    # IDENTICAL model and cube while re-paying the full k-fold CV (~10-40 s CPU at real fold-back
    # sizes). Reuse instead; behaviour is bit-identical.
    train_version = 0
    built_key: Optional[tuple[int, float]] = None
    model: Any = None
    cube: Any = None
    physical_info = None
    constrained_info = None

    def _build_model_and_cube() -> tuple[Any, np.ndarray, Any, Any]:
        if cfg.engine == "physical":
            # Physical candidate: clamp the target to the measured native gamut in ALL modes
            # and solve nodes directly against signal->XYZ.
            model = StructuredForwardModel(
                train_signals, train_xyz, target, reachable_primaries=reachable_primaries)
            cube, physical_info = build_physical_cube(
                model, cfg.grid_size, signal_points=train_signals,
                metric=cfg.physical_metric, max_correction=budget,
                fade_width=cfg.fade_width,
                uncertainty_radius=cfg.physical_uncertainty_radius,
                hue_tolerance_degrees=cfg.physical_hue_tolerance_degrees,
                purity_slack=cfg.physical_purity_slack,
                neutral_band=cfg.neutral_band,
                allow_neutral_refinement=cfg.allow_neutral_refinement,
            )
            return model, cube, physical_info, None
        if cfg.engine == "constrained-rbf":
            model = DisplayErrorModel(train_signals, train_xyz, target, smoothing=cfg.smoothing,
                                      reachable_primaries=reachable_primaries,
                                      sample_confidence=(train_confidence
                                                         if cfg.confidence_weighted_rbf else None))
            cube, constrained_info = build_constrained_rbf_cube(
                model, cfg.grid_size, signal_points=train_signals,
                reachable_primaries=reachable_primaries,
                metric=cfg.constrained_metric, max_correction=budget,
                fade_width=cfg.fade_width,
                n_iterations=cfg.n_inner_iterations,
                near_black_nits=cfg.near_black_nits,
                neutral_band=cfg.neutral_band,
                hue_tolerance_degrees=cfg.constrained_hue_tolerance_degrees,
                purity_slack=cfg.constrained_purity_slack,
                shell_saturation=cfg.constrained_shell_saturation,
                shell_pressure=cfg.constrained_shell_pressure,
                screen_error_threshold=cfg.constrained_screen_error_threshold,
                off_channel_guard_pressure=cfg.constrained_off_channel_guard_pressure,
                gamut_blend_strength=cfg.constrained_gamut_blend_strength,
                maxiter=cfg.constrained_maxiter,
                n_jobs=cfg.constrained_n_jobs,
                chunk_size=cfg.constrained_chunk_size,
            )
            return model, cube, None, constrained_info
        model = DisplayErrorModel(train_signals, train_xyz, target, smoothing=cfg.smoothing,
                                  reachable_primaries=reachable_primaries,
                                  sample_confidence=(train_confidence
                                                     if cfg.confidence_weighted_rbf else None))
        cube = build_cube(
            model, cfg.grid_size, signal_points=train_signals,
            fade_width=cfg.fade_width, max_correction=budget,
            n_iterations=cfg.n_inner_iterations, near_black_nits=cfg.near_black_nits,
            neutral_band=cfg.neutral_band,
        )
        return model, cube, None, None

    for it in range(1, cfg.max_outer + 1):
        if built_key != (train_version, budget):
            # (An unchanged key means neither the training set nor the budget moved — the
            # force-full-validation path — and the previous model/cube are exact; skip.)
            try:
                model, cube, physical_info, constrained_info = _build_model_and_cube()
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
            built_key = (train_version, budget)
        probe_idx, sampling_mode = _adaptive_probe_indices(
            verify, score_hint, iteration=it, cfg=cfg, force_full=force_full_probe)
        force_full_probe = False
        verify_probe = verify[probe_idx]
        target_probe = target_ictcp[probe_idx]
        full_probe = len(probe_idx) == len(verify)

        driven = sample_cube(cube, verify_probe)
        measured = np.maximum(np.asarray(probe(driven), dtype=float).reshape(-1, 3), 0.0)
        # Optimization score (dE_ITP) — drives score_hint, _classify, convergence, budget, and the
        # returned-cube ranking. NEVER swapped for the report metric (philosophy: optimize in the
        # uniform/invertible ICtCp space; report per-mode).
        de = de_itp(space.xyz_to_ictcp(measured) - target_probe)
        score_hint[probe_idx] = de
        # Display score (report metric) — the numbers the LLM/user actually see this iteration.
        de_report = _report_de(verify_probe, measured, de)

        masks = _classify(verify_probe, driven, de, cfg.threshold, budget,
                          clamp_frac=cfg.clamp_active_frac, boundary_eps=cfg.boundary_eps,
                          low_light_signal=cfg.low_light_signal)
        diag = cube_diagnostics(cube)
        if cfg.engine == "physical":
            driven_full = sample_cube(cube, verify)
            produced_full = model.forward(driven_full)
            predicted_max = float(de_itp(space.xyz_to_ictcp(produced_full) - target_ictcp).max())
            smoothing_value = 0.0
        else:
            produced_full = None
            predicted_max = float(predicted_accuracy(model, cube, verify)["max"])
            smoothing_value = float(model.smoothing)
        # Predicted accuracy in the report metric (model-vs-reality gap stays single-metric so it
        # remains meaningful). No-op when report_scorer is None (predicted_report == predicted_max).
        if report_scorer is None:
            predicted_report = predicted_max
        else:
            if produced_full is None:
                produced_full = model.forward(sample_cube(cube, verify))
            pred_itp = de_itp(space.xyz_to_ictcp(produced_full) - target_ictcp)
            predicted_report = float(_report_de(verify, produced_full, pred_itp).max())
        order = np.argsort(de_report)[::-1][: cfg.top_k]
        result = IterationResult(
            iteration=it, max_correction=budget,
            measured_mean_de=float(de_report.mean()), measured_p95_de=float(np.percentile(de_report, 95)),
            measured_max_de=float(de_report.max()),
            predicted_max_de=predicted_report, metric=report_metric,
            above_threshold=int(masks["above"].sum()),
            budget_limited=int(masks["budget_limited"].sum()),
            signal_clipped=int(masks["signal_clipped"].sum()),
            near_black=int(masks["near_black"].sum()),
            residual=int(masks["residual"].sum()),
            worst=[(verify_probe[i].tolist(), float(de_report[i])) for i in order],
            smoothing=smoothing_value, cube_monotonic=diag.monotonic,
            large_reversals=diag.large_reversal_count,
            worst_lattice_jump=diag.worst_lattice_jump,
            train_points=int(len(train_signals)), probed_patches=int(len(probe_idx)),
            probe_total=int(len(verify)), sampling_mode=sampling_mode,
        )
        history.append(result)
        snapshots.append({"cube": cube, "driven": driven, "measured": measured, "de": de,
                          "de_report": de_report,
                          "budget": budget, "monotonic": diag.monotonic,
                          "diagnostics": diag.as_dict(),
                          "verify": verify_probe, "full": full_probe,
                          "sampling_mode": sampling_mode,
                          "physical_info": physical_info.as_dict() if physical_info else None,
                          "constrained_info": constrained_info.as_dict() if constrained_info else None})
        if on_iteration is not None:
            on_iteration(result)

        cur_max = float(de.max())
        if full_probe and cur_max < cfg.threshold:
            converged = True
            break

        # Escalating the budget is a real lever (clamp-limited points with signal headroom),
        # not noise — keep doing it ahead of any stall test.
        escalating = (cfg.auto_escalate and masks["budget_limited"].any()
                      and budget < cfg.max_correction_cap)
        if escalating:
            budget = min(cfg.max_correction_cap, budget * cfg.escalate_factor)
        elif not full_probe and (cur_max < cfg.threshold
                                 or (best_seen_max is not None
                                     and cur_max > best_seen_max - cfg.floor_tol)):
            # A focused pass either looks clean or is no longer improving. Do not declare
            # victory or failure from a slice; force a full validation of the current model.
            force_full_probe = True
            if cur_max >= cfg.threshold:
                continue
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
        train_confidence = np.concatenate([train_confidence, np.ones(len(driven), dtype=float)])
        train_signals, train_xyz, train_confidence = aggregate_training_samples(
            train_signals, train_xyz, train_confidence)
        train_version += 1

    # Pick the cube to return: prefer monotonic, then lowest worst-case dE — using
    # the cached measurements (no extra probing).
    # §0 note (Phase 5 audit): worst-case-first selection was evaluated for core-vs-corner
    # trading on synthetic corner-floor/noisy panels — the snapshots' PRACTICAL-CORE spread
    # (neutral+near-neutral band) measured ≤0.3 dE_ITP, because the neutral fade pins the
    # diagonal, all snapshots share one progressively-refined model, and full-validation
    # snapshots are preferred. Sub-JND mean-dE differences between tied snapshots are the
    # residual exposure; a practically-weighted rank (if ever wanted) must reuse Phase 6's
    # core/frontier zone definitions, not invent its own.
    def _rank(s: dict[str, Any]) -> tuple[int, float]:
        return (0 if s["monotonic"] else 1, float(s["de"].max()))
    full_snapshots = [s for s in snapshots if s["full"]]
    best = min(full_snapshots or snapshots, key=_rank)
    best_cube, best_de, best_driven, best_budget = (best["cube"], best["de"],
                                                    best["driven"], best["budget"])
    best_verify = best["verify"]
    best_de_report = best["de_report"]   # display metric (== best_de when report_scorer is None)
    final = history[-1]

    masks_best = _classify(best_verify, best_driven, best_de, cfg.threshold, best_budget,
                           clamp_frac=cfg.clamp_active_frac, boundary_eps=cfg.boundary_eps,
                           low_light_signal=cfg.low_light_signal)
    # Real floors = physically clipped + model residual (more budget won't help).
    real_floor = masks_best["signal_clipped"] | masks_best["residual"]
    budget_limited = masks_best["budget_limited"]
    # Floor membership comes from the dE_ITP classification (masks_best); the dE VALUE shown is the
    # report metric so the seam question/floor list never quote dE_ITP during an SDR run.
    floor_points = [(best_verify[i].tolist(), float(best_de_report[i])) for i in np.where(real_floor)[0]]
    floor_points.sort(key=lambda p: p[1], reverse=True)

    needs_adjudication = bool(real_floor.any() or budget_limited.any())
    question = None
    if needs_adjudication:
        parts: list[str] = []
        if real_floor.any():
            worst_sig, worst_de = floor_points[0]
            near = int((real_floor & masks_best["near_black"]).sum())
            near_clause = f"; {near} in the near-black region" if near else ""
            boundary_bits: list[str] = []
            if masks_best["low_clipped"].any():
                boundary_bits.append("low-side rail")
            if masks_best["high_clipped"].any():
                boundary_bits.append("high-side rail")
            boundary_clause = f", {', '.join(boundary_bits)}" if boundary_bits else ""
            parts.append(
                f"{int(real_floor.sum())} patch(es) at the panel's physical floor/limit{near_clause} "
                f"(worst {report_metric} {worst_de:.1f} at signal {[round(c, 3) for c in worst_sig]}, "
                f"{'boundary: ' if boundary_clause else ''}{boundary_clause.lstrip(', ') or 'interior residual'}) "
                "— accept as the panel limit (the verify gate still judges the result), "
                "or abort and investigate?"
            )
        if budget_limited.any():
            parts.append(
                f"{int(budget_limited.sum())} patch(es) still need a correction beyond the "
                f"budget cap ({cfg.max_correction_cap:g}); raise the cap (the 3D LUT is doing "
                f"more than a post-MHC residual) or run MHC first."
            )
        question = " ".join(parts)

    # Neutral-axis breakout. The neutral diagonal (R=G=B) is the MHC foundation's domain; the 3D LUT
    # is meant to refine OFF-gray volume, not re-own neutral. The cube can nonetheless pull neutral
    # off D65, and a neutral wreck with a still-modest OVERALL mean is invisible in best_mean_de — so
    # surface the probed-neutral dE separately for the LLM (and _severe_optimizer_floor) to judge.
    neutral_mask = (np.max(best_verify, axis=1) - np.min(best_verify, axis=1)) <= 0.015
    neutral_de = best_de[neutral_mask]                  # dE_ITP
    neutral_de_report = best_de_report[neutral_mask]    # report metric (display)

    digest = {
        "converged": converged,
        "iterations": len(history),
        "engine": cfg.engine,
        "grid_size": cfg.grid_size,
        "threshold": cfg.threshold,
        "max_correction": round(best_budget, 4),
        # The cube CONVERGES in dE_ITP; the *_de fields below are that optimize metric (consumed by
        # the convergence/floor logic). The *_de_report fields are the SAME patches re-scored in the
        # run's report metric (``metric``) for the LLM/user — equal to *_de when report_scorer is None.
        "metric": report_metric,
        "optimize_metric": "dE_ITP",
        "best_max_de": round(float(best_de.max()), 4),
        "best_mean_de": round(float(best_de.mean()), 4),
        "best_p95_de": round(float(np.percentile(best_de, 95)), 4),
        "best_max_de_report": round(float(best_de_report.max()), 4),
        "best_mean_de_report": round(float(best_de_report.mean()), 4),
        "best_p95_de_report": round(float(np.percentile(best_de_report, 95)), 4),
        "neutral_count": int(neutral_mask.sum()),
        "neutral_max_de": round(float(neutral_de.max()), 4) if neutral_de.size else None,
        "neutral_mean_de": round(float(neutral_de.mean()), 4) if neutral_de.size else None,
        "neutral_max_de_report": round(float(neutral_de_report.max()), 4) if neutral_de_report.size else None,
        "neutral_mean_de_report": round(float(neutral_de_report.mean()), 4) if neutral_de_report.size else None,
        "above_threshold": int(masks_best["above"].sum()),
        "physical_floor": int(real_floor.sum()),
        "near_black_floor": int((real_floor & masks_best["near_black"]).sum()),
        "near_black_above_threshold": int(masks_best["near_black"].sum()),
        "low_side_clipped": int(masks_best["low_clipped"].sum()),
        "high_side_clipped": int(masks_best["high_clipped"].sum()),
        "budget_limited": int(budget_limited.sum()),
        "cube_monotonic": bool(best["monotonic"]),
        "large_reversals": int((best.get("diagnostics") or {}).get("large_reversal_count", 0)),
        "worst_lattice_jump": (best.get("diagnostics") or {}).get("worst_lattice_jump"),
        "cube_diagnostics": best.get("diagnostics"),
        "confidence_weighted_rbf": bool(cfg.confidence_weighted_rbf),
        "training_raw_points": raw_train_count,
        "training_unique_points": int(len(train_confidence)),
        "training_max_confidence": round(float(train_confidence.max()), 3) if len(train_confidence) else 0.0,
        "sampling_mode": best["sampling_mode"],
        "full_validation": bool(best["full"]),
        "physical_info": best.get("physical_info"),
        "constrained_info": best.get("constrained_info"),
        "best_probed_patches": int(len(best_de)),
        "probe_total": int(len(verify)),
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
