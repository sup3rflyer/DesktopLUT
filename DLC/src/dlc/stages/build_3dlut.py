"""Stage 7a — build-3dlut: run Argyll collink -3c to emit the runtime cube.

Source = a Rec.709 (SDR) reference ICC; display = the post-MHC measured ICC. The
result is an IRIDAS .cube DesktopLUT can load at runtime. Under ``--simulate`` no
collink runs — an identity cube is synthesised so the chain can be rehearsed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..lut3d import (
    default_source_icc,
    execute_3dlut_build_plan,
    latest_post_mhc_icc,
    write_3dlut_build_plan,
)
from ..mhc import resolve_run_path
from ..runs import RunContext
from ..simulation import write_placeholder_icc
from ..stage import StageResult
from ..tools import discover_tools
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.run_mode(args, ctx)
    iteration = args.iteration
    result = StageResult("build-3dlut")
    tools = discover_tools()

    # Source ICC (Rec.709 target by default). Synthesise a placeholder when
    # simulating without a contained Argyll reference profile.
    source_icc = Path(args.source_icc) if args.source_icc else resolve_run_path(ctx, default_source_icc(mode))
    if not source_icc.exists():
        if args.simulate:
            source_icc = write_placeholder_icc(
                ctx.root / "generated" / f"source_ref_{mode.lower()}.icc", description="simulated Rec.709 source"
            )
            result.note("simulated: synthesised a placeholder source reference ICC")
        else:
            result.fail("no_source_icc", f"source reference ICC not found: {source_icc}")
            return result

    display_icc = Path(args.display_icc) if args.display_icc else latest_post_mhc_icc(ctx)
    if display_icc is None or not Path(display_icc).exists():
        result.fail(
            "no_display_icc",
            "post-MHC display ICC not found; run `measure --stage post-mhc` or pass --display-icc",
        )
        return result

    plan = write_3dlut_build_plan(
        ctx=ctx,
        tools=tools,
        iteration=iteration,
        source_icc=source_icc,
        display_icc=Path(display_icc),
        grid_size=args.grid_size,
        quality=args.quality,
    )
    result.action(f"planned collink -3c build (grid {args.grid_size}, quality {args.quality})")
    result.raw["command"] = plan.command

    execution = execute_3dlut_build_plan(
        ctx=ctx, plan_path=Path(plan.artifacts["plan"]), dry_run=False, simulate=args.simulate
    )
    result.action("built cube (simulated)" if args.simulate else "ran collink")
    result.raw["build_ok"] = execution.ok
    if not execution.ok:
        result.fail("collink_failed", f"collink did not complete: {execution.error or 'see logs'}")
        result.raw["log"] = {"stdout": execution.stdout, "stderr": execution.stderr}
        return result

    cube = resolve_run_path(ctx, Path(execution.cube_path))
    if not cube.exists():
        result.fail("cube_missing", f"build reported success but cube is missing: {cube}")
        return result
    result.add_artifact(cube)

    result.preconditions = {"source_icc_present": True, "display_icc_present": True}
    result.metrics = {
        "mode": mode,
        "iteration": iteration,
        "cube": str(cube),
        "grid_size": args.grid_size,
        "quality": args.quality,
        "source_icc": str(source_icc),
        "display_icc": str(display_icc),
    }
    result.advice = {
        "default_policy_verdict": "check_cube",
        "reasons": ["cube built; verify structural integrity before installing"],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC build-3dlut: collink -3c -> runtime cube")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--source-icc", default=None, dest="source_icc", help="Rec.709 (SDR) target ICC")
    parser.add_argument("--display-icc", default=None, dest="display_icc", help="post-MHC measured ICC")
    parser.add_argument("--grid-size", type=int, default=33, dest="grid_size")
    parser.add_argument("--quality", default="h", help="collink quality (l/m/h/u)")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result, iteration=args.iteration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
