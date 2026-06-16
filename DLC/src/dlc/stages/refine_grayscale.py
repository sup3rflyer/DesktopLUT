"""Stage 5 — refine-grayscale: one closed-loop MHC correction-grayscale step.

Reads a post-MHC verification ramp, computes the next per-channel
``correctionGrayscale`` deviations via the refinement control law
(``refine.propose_correction_grayscale``), pushes them, and re-bakes the MHC
profile. Emits per-point residuals, the white shift, and a delta vs. the
previous iteration. The assistant decides whether to loop again — the advice is
advisory only (a channel pinned at its deviation ceiling means the panel, not
the algorithm, is the limit; the assistant may accept a small residual tint).
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..mhc import find_stage_artifact, parse_ti3, resolve_run_path
from ..refine import DEV_MAX, DEV_MIN, Deviations, propose_correction_grayscale
from ..runs import RunContext
from ..stage import StageResult
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.normalize_mode(args.mode)
    iteration = args.iteration
    result = StageResult("refine-grayscale")

    state = _common.load_dlc_state(ctx)
    params = state.get("mhc_params")
    if not params:
        result.fail("no_params", "no MHC params in the run-record; run build-mhc + install-mhc first")
        return result
    monitor = params.get("monitor", args.monitor)

    # Source: a post-MHC grayscale verification ramp.
    if args.source_ti3:
        source = Path(args.source_ti3)
    else:
        source = find_stage_artifact(ctx, "mhc-verification", "ti3")
    if source is None or not Path(source).exists():
        result.fail(
            "no_verify_ti3",
            "post-MHC verification TI3 not found; run `measure --stage mhc-verification` or pass --source-ti3",
        )
        return result
    source = resolve_run_path(ctx, Path(source))
    result.add_artifact(source)

    samples = parse_ti3(source)
    gray = _common.gray_patches_from_ti3(samples)
    if len(gray) < 2:
        result.fail("too_few_gray", "verification ramp has fewer than 2 neutral patches")
        return result

    prim = _common.measured_primaries_from(params["measured_primaries"], _measured_white_tuple(params))
    target = _common.refinement_target(state)

    prev = state.get("correction_grayscale")
    current = Deviations.from_obj(prev["deviations"], len(gray)) if prev and prev.get("point_count") == len(gray) else Deviations.identity(len(gray))

    proposal = propose_correction_grayscale(
        measured=gray, target=target, primaries=prim, current=current, damping=args.damping
    )
    summary = proposal["summary"]
    result.action(f"computed correction-grayscale step (damping={args.damping})")

    # Push the new correction grayscale and re-bake.
    controller = _common.make_controller(args, ctx)
    try:
        controller.set_correction_grayscale(
            monitor, mode, proposal["point_count"], proposal["points"], proposal["deviations"]
        )
        result.action("pushed correction grayscale")
        applied = controller.apply_mhc(monitor, mode)
        result.action("re-baked MHC")
    except Exception as exc:  # noqa: BLE001
        result.fail("apply_error", f"refine push/apply failed: {type(exc).__name__}: {exc}")
        return result
    result.raw["apply"] = applied

    # Ceiling detection: a deviation pinned at its clamp means the panel limits us.
    clamped = [
        ch
        for ch in ("r", "g", "b")
        for v in proposal["deviations"][ch]
        if v <= DEV_MIN + 1e-6 or v >= DEV_MAX - 1e-6
    ]
    if clamped:
        result.anomaly(
            "deviation_clamped",
            f"channel(s) {sorted(set(clamped))} pinned at the correction ceiling; "
            "the panel may be at a physical limit (e.g. blue near max for a warm white)",
            "low",
        )

    # Persist new correction + history, compute deltas vs previous iteration.
    state["correction_grayscale"] = {
        "point_count": proposal["point_count"],
        "points": proposal["points"],
        "deviations": proposal["deviations"],
    }
    history = state.setdefault("refine_history", [])
    prev_summary = history[-1] if history else None
    metrics = {
        "iteration": iteration,
        "avg_de2000": summary["avg_de2000"],
        "max_de2000": summary["max_de2000"],
        "white_de2000": summary["white_de2000"],
        "white_xy": summary["white_xy"],
        "max_abs_deviation": summary["max_abs_deviation"],
    }
    history.append(metrics)
    _common.save_dlc_state(ctx, state)

    deltas = {}
    if prev_summary:
        deltas = {
            "avg_de2000": round(metrics["avg_de2000"] - prev_summary["avg_de2000"], 4),
            "white_de2000": round(metrics["white_de2000"] - prev_summary["white_de2000"], 4),
        }

    advice = _common.policy_advice(
        {
            "avg_de2000": summary["avg_de2000"],
            "p95_de2000": summary["max_de2000"],  # no p95 from the ramp; use max as a conservative stand-in
            "max_de2000": summary["max_de2000"],
            "white_de2000": summary["white_de2000"],
        },
        previous_avg=prev_summary["avg_de2000"] if prev_summary else None,
    )
    if clamped and advice["default_policy_verdict"] == "continue":
        advice["reasons"].append(
            "note: a channel is at its ceiling — pushing further may crush a channel rather than improve white"
        )

    result.preconditions = {"params_present": True, "verify_ramp_present": True}
    result.metrics = {**metrics, "white_cct": _round_cct(summary["white_xy"])}
    result.deltas = deltas
    result.raw["residuals"] = proposal["residuals"]
    result.advice = advice
    return result


def _measured_white_tuple(params: dict) -> tuple[float, float]:
    mw = params.get("measured_white", {})
    return (float(mw.get("x", 0.3127)), float(mw.get("y", 0.3290)))


def _round_cct(white_xy) -> int | None:
    cct = _common.cct_mccamy(white_xy[0], white_xy[1])
    return round(cct) if cct else None


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC refine-grayscale: one MHC correction-grayscale step")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--source-ti3", default=None, dest="source_ti3")
    parser.add_argument("--damping", type=float, default=0.7, help="control-law damping (default 0.7)")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result, iteration=args.iteration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
