"""Iteration scoring and decision records.

DLC v1 treats this module as an **advisor, not a gate** (rebuild plan §1.4): the
threshold computation here (`MetricThresholds`, `IterationMetrics`,
`decide_iteration`) is surfaced to the arbitrating assistant as advisory
`default_policy_verdict` + reasons. The assistant is always free to override it.
The pure threshold functions intentionally do not import the run-record/loop-status
machinery, so they stay importable from the stage tools after the autopilot modules
are deleted. `write_quality_policy` / `metric_thresholds_for_run` still take a
RunContext but never gate; the old `write_decision_record` (loop-status coupled) was
removed with the autopilot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .events import EventWriter
from .runs import RunContext


@dataclass(frozen=True)
class MetricThresholds:
    """Advisory SDR acceptance targets + iteration-control knobs.

    Provenance of the SDR dE numbers (CIEDE2000; fable audit Phase 6, P3): 1.0 ΔE2000 ≈
    one JND, so avg 1.5 ≈ "typical patch at/below ~1.5 JND" — the conventional pro-
    calibration acceptance band — with p95 3.0 / max 5.0 allowing the tail a couple of
    JND at isolated patches and white held tighter (2.0) because a white cast is the
    most visible single error. The recorded hardware baseline (PA32UCXR SDR verify:
    0.41 avg ΔE2000) passes these with ~3.5× margin — the gate is deliberately looser
    than the panel's demonstrated ability so it flags regressions, not noise. Tunable
    per profile/phase via ``quality_policy`` (``metric_thresholds_from_policy``);
    advisory only — the assistant owns acceptance (plan §1.4).

    ``max_lut_neighbor_delta=1.0`` is a last-resort ceiling only (a full-range jump);
    the effective structural gate is the grid-pitch-derived default inside
    ``lut_integrity.summarize_lut_integrity`` (its ``ok`` verdict), which
    ``decide_iteration`` already consumes via ``integrity['ok']``."""
    avg_de2000: float = 1.5
    p95_de2000: float = 3.0
    max_de2000: float = 5.0
    white_de2000: float = 2.0
    min_improvement: float = 0.1
    max_iterations: int = 5
    max_lut_neighbor_delta: float = 1.0
    max_lut_monotonicity_violations: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def acceptance_targets(self) -> dict[str, float]:
        """Just the dE **acceptance** targets — the subset the 'within quality?' gate compares
        against, and the only fields the verify digest/report should advertise as quality
        targets. Excludes the iteration-control knobs (``min_improvement``, ``max_iterations``,
        the LUT-integrity tolerances) that share this dataclass but are not acceptance criteria."""
        return {k: getattr(self, k) for k in ACCEPTANCE_FIELDS}


# The acceptance-target subset of MetricThresholds (vs the iteration-control knobs): what the
# verify gate checks and the only fields the verify digest should present as "quality targets".
ACCEPTANCE_FIELDS = ("avg_de2000", "p95_de2000", "max_de2000", "white_de2000")


THRESHOLD_FIELDS = {
    "avg_de2000",
    "p95_de2000",
    "max_de2000",
    "white_de2000",
    "min_improvement",
    "max_iterations",
    "max_lut_neighbor_delta",
    "max_lut_monotonicity_violations",
}


@dataclass(frozen=True)
class IterationMetrics:
    iteration: int
    avg_de2000: float | None = None
    p95_de2000: float | None = None
    max_de2000: float | None = None
    white_de2000: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    phase: str
    iteration: int
    decision: str
    reason: str
    next_params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _threshold_updates(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    updates = {key: raw[key] for key in THRESHOLD_FIELDS if raw.get(key) is not None}
    if "max_iterations" in updates:
        updates["max_iterations"] = int(updates["max_iterations"])
    if "max_lut_monotonicity_violations" in updates:
        updates["max_lut_monotonicity_violations"] = int(updates["max_lut_monotonicity_violations"])
    for key in [
        "avg_de2000",
        "p95_de2000",
        "max_de2000",
        "white_de2000",
        "min_improvement",
        "max_lut_neighbor_delta",
    ]:
        if key in updates:
            updates[key] = float(updates[key])
    return updates


def metric_thresholds_from_policy(
    policy: Any,
    phase: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> MetricThresholds:
    values = asdict(MetricThresholds())
    if isinstance(policy, dict):
        values.update(_threshold_updates(policy))
        values.update(_threshold_updates(policy.get("default")))
        values.update(_threshold_updates(policy.get(phase)))
    values.update(_threshold_updates(overrides))
    return MetricThresholds(**values)


def metric_thresholds_for_run(
    ctx: RunContext | None,
    phase: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> MetricThresholds:
    policy = ctx.manifest.desktoplut.get("quality_policy") if ctx else None
    return metric_thresholds_from_policy(policy, phase, overrides=overrides)


# Advisory HDR verify acceptance in **dE_ITP** (BT.2124; 1.0 ≈ one JND), looser than the
# SDR CIEDE2000 gate and **LLM-negotiated after the first refinement round**
# (v2-design-notes §7: "HDR — looser and LLM-negotiated"). The MetricThresholds fields
# keep their ``*_de2000`` names as the generic ΔE carrier; the run's ``metric`` label
# ("dE_ITP") names the units. Tunable per profile via a ``quality_policy['hdr']`` block
# (read from BOTH the profile YAML `quality: {hdr: ...}` — live verify — and the run
# manifest's quality_policy — stage CLI).
#
# Provenance (fable audit Phase 6, P3): the 2× factor over SDR (avg 3.0 vs 1.5, max 10
# vs 5) is grounded in the recorded hardware baseline, not derived from first
# principles — the PA32UCXR HDR run verified at 3.26 dE_ITP grayscale average with the
# residual dominated by patches AT the panel's gamut/luminance floor (reachability, not
# correctable error), so a 1.5-JND-style average gate would flag every honest HDR run on
# sub-BT.2020 hardware. With gamut-aware scoring (P1, this phase) clamping targets onto
# the measured gamut, the floor component shrinks and these defaults become genuinely
# negotiable downward — re-derivation against post-audit hardware scores is queued as
# HW-1; until then the numbers stay the recorded-baseline-compatible defaults. The §0
# practical split (metrics.practical_summary) is the intended negotiation evidence:
# judge `core` against SDR-like expectations, `clamped` as reachability.
HDR_VERIFY_THRESHOLD_DEFAULTS: dict[str, float] = {
    "avg_de2000": 3.0,
    "p95_de2000": 6.0,
    "max_de2000": 10.0,
    "white_de2000": 4.0,
}


def hdr_metric_thresholds(
    policy: Any = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> MetricThresholds:
    """The advisory HDR verify thresholds (dE_ITP) — :data:`HDR_VERIFY_THRESHOLD_DEFAULTS`
    overlaid by a profile ``quality_policy['hdr']`` block, then explicit ``overrides``
    (e.g. the LLM's negotiated target). Advisory, never a gate — the assistant decides."""
    values = asdict(MetricThresholds())
    values.update(HDR_VERIFY_THRESHOLD_DEFAULTS)
    if isinstance(policy, dict):
        values.update(_threshold_updates(policy.get("hdr")))
    values.update(_threshold_updates(overrides))
    return MetricThresholds(**values)


def quality_policy_coverage(policy: Any) -> dict[str, Any]:
    default = policy.get("default") if isinstance(policy, dict) and isinstance(policy.get("default"), dict) else None
    mhc = policy.get("mhc") if isinstance(policy, dict) and isinstance(policy.get("mhc"), dict) else None
    lut3d = policy.get("3dlut") if isinstance(policy, dict) and isinstance(policy.get("3dlut"), dict) else None
    mhc_covered = default is not None or mhc is not None
    lut3d_covered = default is not None or lut3d is not None
    ok = isinstance(policy, dict) and bool(policy) and mhc_covered and lut3d_covered
    missing = []
    if not isinstance(policy, dict) or not policy:
        missing.append("quality policy")
    if not mhc_covered:
        missing.append("MHC thresholds")
    if not lut3d_covered:
        missing.append("3D LUT thresholds")
    return {
        "ok": ok,
        "recorded": isinstance(policy, dict) and bool(policy),
        "mhc_covered": mhc_covered,
        "3dlut_covered": lut3d_covered,
        "missing": missing,
        "policy": policy if isinstance(policy, dict) else None,
    }


def write_quality_policy(
    *,
    ctx: RunContext,
    phase: str,
    thresholds: MetricThresholds,
) -> dict[str, Any]:
    policy = ctx.manifest.desktoplut.get("quality_policy")
    if not isinstance(policy, dict):
        policy = {}
    policy[phase] = asdict(thresholds)
    ctx.manifest.desktoplut["quality_policy"] = policy
    ctx.save()
    EventWriter(ctx.events_path).write(
        "INFO",
        "quality_policy",
        "quality_policy_written",
        phase=phase,
        thresholds=asdict(thresholds),
    )
    return policy


def _mhc_next_params(metrics: IterationMetrics) -> dict[str, Any]:
    return {
        "strategy": "rebuild_mhc_from_latest_verification",
        "source_stage": "mhc-verification",
        "source_iteration": metrics.iteration,
        "next_iteration": metrics.iteration + 1,
        "lut_size": 4096,
        "gamma": 2.2,
    }


def _3dlut_next_params(metrics: IterationMetrics, thresholds: MetricThresholds, integrity: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "strategy": "reprofile_rebuild_apply_verify",
        "next_iteration": metrics.iteration + 1,
        "quality": "u",
        "intent": "r",
        "eotf": "b",
    }
    if integrity and not bool(integrity.get("ok")):
        params.update(
            {
                "grid_size": 17,
                "post_mhc_patch_count": 729,
                "reason": "cube integrity failed; rebuild with a coarser grid before trusting measured dE",
            }
        )
        return params

    max_de = metrics.max_de2000 if metrics.max_de2000 is not None else thresholds.max_de2000
    p95_de = metrics.p95_de2000 if metrics.p95_de2000 is not None else thresholds.p95_de2000
    if max_de > thresholds.max_de2000 * 1.5:
        params.update({"grid_size": 45, "post_mhc_patch_count": 1458, "reason": "large localized error; increase sampling density"})
    elif p95_de > thresholds.p95_de2000:
        params.update({"grid_size": 33, "post_mhc_patch_count": 1024, "reason": "broad volume error; add profiling samples"})
    else:
        params.update({"grid_size": 33, "post_mhc_patch_count": 729, "reason": "threshold miss is modest; repeat with ultra quality"})
    return params


def decide_iteration(
    phase: str,
    metrics: IterationMetrics,
    thresholds: MetricThresholds,
    previous: IterationMetrics | None = None,
) -> Decision:
    if metrics.iteration >= thresholds.max_iterations:
        return Decision(
            phase=phase,
            iteration=metrics.iteration,
            decision="stop",
            reason=f"maximum iteration count {thresholds.max_iterations} reached",
        )

    missing = [
        name
        for name in ["avg_de2000", "p95_de2000", "max_de2000", "white_de2000"]
        if getattr(metrics, name) is None
    ]
    if missing:
        return Decision(
            phase=phase,
            iteration=metrics.iteration,
            decision="continue",
            reason="missing required metrics: " + ", ".join(missing),
        )

    if phase == "3dlut":
        integrity = metrics.extra.get("lut_integrity")
        if not isinstance(integrity, dict):
            return Decision(
                phase=phase,
                iteration=metrics.iteration,
                decision="continue",
                reason="missing required 3D LUT integrity summary",
            )
        integrity_ok = bool(integrity.get("ok"))
        max_neighbor_delta = float(integrity.get("max_neighbor_delta", 0.0))
        monotonicity_violations = int(integrity.get("monotonicity_violations", 0))
        if (
            not integrity_ok
            or max_neighbor_delta > thresholds.max_lut_neighbor_delta
            or monotonicity_violations > thresholds.max_lut_monotonicity_violations
        ):
            return Decision(
                phase=phase,
                iteration=metrics.iteration,
                decision="continue",
                reason=(
                    "3D LUT integrity thresholds are not satisfied: "
                    f"ok={integrity_ok}, max_neighbor_delta={max_neighbor_delta:.6f}, "
                    f"monotonicity_violations={monotonicity_violations}"
                ),
                next_params=_3dlut_next_params(metrics, thresholds, integrity),
            )

    # Explicit coercion, not `assert` (stripped under -O): the missing-metrics check above
    # guarantees these are numbers by this point; float() keeps that guarantee enforced at
    # runtime in every interpreter mode (a None here would raise TypeError loudly).
    avg = float(metrics.avg_de2000)
    p95 = float(metrics.p95_de2000)
    max_de = float(metrics.max_de2000)
    white = float(metrics.white_de2000)

    passing = (
        avg <= thresholds.avg_de2000
        and p95 <= thresholds.p95_de2000
        and max_de <= thresholds.max_de2000
        and white <= thresholds.white_de2000
    )
    if passing:
        return Decision(
            phase=phase,
            iteration=metrics.iteration,
            decision="stop",
            reason="all threshold metrics are satisfied",
        )

    if previous and previous.avg_de2000 is not None:
        improvement = previous.avg_de2000 - metrics.avg_de2000
        if improvement < thresholds.min_improvement:
            return Decision(
                phase=phase,
                iteration=metrics.iteration,
                decision="stop",
                reason=f"average dE improvement {improvement:.3f} is below minimum {thresholds.min_improvement:.3f}",
            )

    return Decision(
        phase=phase,
        iteration=metrics.iteration,
        decision="continue",
        reason="thresholds are not yet satisfied",
        next_params=_mhc_next_params(metrics) if phase == "mhc" else _3dlut_next_params(metrics, thresholds),
    )
