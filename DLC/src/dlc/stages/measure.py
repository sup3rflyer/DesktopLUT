"""Stages 3/6/8 — measure: profile the panel with Argyll (targen/dispread/colprof).

Wraps the real measurement engine (``profile_plan``). Under ``--simulate`` it
writes a synthetic TI3 instead of touching a meter. It then reads the TI3 back
and surfaces sanity metrics (patch count, black/white luminance, grayscale
monotonicity) plus anomaly flags. It does NOT judge quality — that is ``score``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..mhc import classify_samples, parse_ti3, resolve_run_path
from ..profile_plan import (
    STAGE_PRESETS,
    execute_profile_measurement_plan,
    write_profile_measurement_plan,
)
from ..runs import RunContext
from ..stage import StageResult
from ..tools import discover_tools
from . import _common


def _ti3_sanity(ti3: Path) -> tuple[dict, list[tuple[str, str, str]]]:
    """Return (metrics, anomalies) describing the measured TI3."""
    samples = parse_ti3(ti3)
    anomalies: list[tuple[str, str, str]] = []
    grey = classify_samples(samples)["grey"]
    grey_sorted = sorted(grey, key=lambda s: s.rgb[0])
    luminances = [s.xyz[1] for s in grey_sorted]
    black_y = luminances[0] if luminances else None
    white_y = luminances[-1] if luminances else None

    non_monotonic = 0
    for prev, nxt in zip(luminances, luminances[1:]):
        if nxt + 1e-6 < prev:
            non_monotonic += 1
    if non_monotonic:
        anomalies.append(
            ("grayscale_non_monotonic", f"{non_monotonic} grayscale step(s) decrease in luminance", "medium")
        )
    if white_y is not None and white_y <= 0:
        anomalies.append(("no_white", "brightest grayscale patch has zero luminance", "high"))
    if black_y is not None and white_y and black_y > 0.02 * white_y:
        anomalies.append(
            ("high_black_level", f"black level {black_y:.4f} is >2% of white {white_y:.4f}", "low")
        )
    metrics = {
        "patch_count": len(samples),
        "grayscale_count": len(grey_sorted),
        "black_luminance": black_y,
        "white_luminance": white_y,
        "grayscale_non_monotonic": non_monotonic,
    }
    return metrics, anomalies


def build(args, ctx: RunContext) -> StageResult:
    stage = args.stage
    iteration = args.iteration
    result = StageResult(f"measure:{stage}")
    if stage not in STAGE_PRESETS:
        result.fail("unknown_stage", f"unknown measure stage {stage!r}; valid: {sorted(STAGE_PRESETS)}")
        return result

    tools = discover_tools()
    correction = Path(args.correction) if args.correction else None
    plan = write_profile_measurement_plan(
        ctx=ctx,
        tools=tools,
        stage=stage,
        iteration=iteration,
        port=args.port,
        display_index=args.display_index,
        patch_window=args.patch_window,
        patch_count=args.patch_count,
        correction=correction,
        high_res=args.high_res,
        observer=args.observer,
    )
    result.action(f"planned {stage} measurement ({len(plan.commands)} Argyll commands)")
    result.add_artifact(Path(plan.artifacts["plan"]))

    execution = execute_profile_measurement_plan(
        ctx=ctx,
        plan_path=Path(plan.artifacts["plan"]),
        dry_run=False,
        simulate=args.simulate,
    )
    result.action("executed measurement (simulated)" if args.simulate else "executed measurement on hardware")
    result.raw["execution_ok"] = execution.ok
    if not execution.ok:
        result.fail("measurement_failed", f"{stage} measurement did not complete; see {execution.log_dir}")
        result.raw["log_dir"] = execution.log_dir
        return result

    ti3 = resolve_run_path(ctx, Path(plan.artifacts["ti3"]))
    if not ti3.exists():
        result.fail("ti3_missing", f"measurement reported success but TI3 is missing: {ti3}")
        return result
    result.add_artifact(ti3)
    result.add_artifact(resolve_run_path(ctx, Path(plan.artifacts["icc"])))

    sanity, anomalies = _ti3_sanity(ti3)
    for code, detail, severity in anomalies:
        result.anomaly(code, detail, severity)

    result.preconditions = {"plan_written": True, "neutral_assumed": True}
    result.metrics = {"stage": stage, "iteration": iteration, "ti3": str(ti3), **sanity}
    if args.simulate:
        result.note("simulated: synthetic sRGB-D65 TI3; meter drift/variance not modeled")
    result.advice = {
        "default_policy_verdict": "trust" if not anomalies else "inspect",
        "reasons": ["measurement looks sane" if not anomalies else "measurement has anomalies to weigh"],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC measure: profile the panel with Argyll")
    parser.add_argument("--stage", default="raw-mhc", help=f"measure stage: {', '.join(sorted(STAGE_PRESETS))}")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--port", type=int, default=1, help="meter instrument port (dispread -c)")
    parser.add_argument("--display-index", type=int, default=1, dest="display_index")
    parser.add_argument(
        "--patch-window",
        default="0.5,0.5,50,50",
        dest="patch_window",
        help="Argyll dispread -P ho,vo,ss[,vs] (default fills the screen; Argyll clamps to display bounds)",
    )
    parser.add_argument("--patch-count", type=int, default=None, dest="patch_count")
    parser.add_argument("--correction", default=None, help="colorimeter correction (.ccmx/.ccss) for Argyll -X")
    parser.add_argument("--high-res", action="store_true", dest="high_res")
    parser.add_argument("--observer", default=None)
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result, iteration=args.iteration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
