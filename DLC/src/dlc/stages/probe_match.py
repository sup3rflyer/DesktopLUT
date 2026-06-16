"""Stage 1 (optional) — probe-match: build a colorimeter correction (.ccmx/.ccss).

Skipped silently in ordinary runs (plan §4); only invoked when the user
explicitly wants a fresh meter correction, which needs a spectrometer. Wraps the
Argyll ccxxmake engine. Under ``--simulate`` it writes a synthetic correction so
the chain can be rehearsed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..mhc import resolve_run_path
from ..probe_match import execute_probe_match_plan, write_probe_match_plan
from ..runs import RunContext
from ..stage import StageResult
from ..tools import discover_tools
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("probe-match")
    tools = discover_tools()

    if not args.simulate and not tools.ccxxmake.ok:
        result.fail("ccxxmake_missing", "ccxxmake not available; contained Argyll tools required for probe-match")
        return result

    plan = write_probe_match_plan(
        ctx=ctx,
        tools=tools,
        kind=args.kind,
        iteration=args.iteration,
        display_tech=args.display_tech,
        display_index=args.display_index,
        high_res=args.high_res,
    )
    result.action(f"planned {args.kind.upper()} probe match ({plan.measurement_mode})")
    result.add_artifact(Path(plan.artifacts["plan"]))
    if plan.required_human_actions:
        result.note("requires placement acknowledgements: " + ", ".join(plan.required_human_actions))

    execution = execute_probe_match_plan(
        ctx=ctx,
        plan_path=Path(plan.artifacts["plan"]),
        dry_run=False,
        simulate=args.simulate,
        # No physical meter placement to acknowledge when simulating.
        force=args.force or args.simulate,
    )
    if not execution.ok:
        result.fail("probe_match_failed", execution.error or "ccxxmake did not complete")
        result.raw["error"] = execution.error
        return result
    result.action("built correction (simulated)" if args.simulate else "ran ccxxmake")

    correction = resolve_run_path(ctx, Path(execution.correction))
    if correction.exists():
        result.add_artifact(correction)
    # Record so subsequent measurements can pick it up via Argyll -X.
    state = _common.load_dlc_state(ctx)
    state["probe_match_correction"] = str(correction)
    _common.save_dlc_state(ctx, state)

    result.preconditions = {"ccxxmake_available": True}
    result.metrics = {"kind": args.kind, "correction": str(correction), "measurement_mode": plan.measurement_mode}
    result.advice = {
        "default_policy_verdict": "use_correction",
        "reasons": ["meter correction built; pass it to subsequent measurements via --correction"],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC probe-match (optional): build a meter correction")
    parser.add_argument("--kind", default="ccmx", choices=("ccmx", "ccss"))
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--display-tech", default="u", dest="display_tech")
    parser.add_argument("--display-index", type=int, default=1, dest="display_index")
    parser.add_argument("--high-res", action="store_true", dest="high_res")
    parser.add_argument(
        "--force", action="store_true", help="proceed without human placement acknowledgement (hardware only)"
    )
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result, iteration=args.iteration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
