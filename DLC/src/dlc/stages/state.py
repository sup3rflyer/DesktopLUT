"""Utility — state: what's done (run-record) + the live DesktopLUT state.

The arbitrator reads this on (re)entry so a compacted or resumed conversation
knows where the calibration stands without a heavyweight handoff layer.
"""

from __future__ import annotations

import sys

from ..runs import RunContext
from ..stage import StageResult
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("state")
    dl = _common.load_dlc_state(ctx)

    controller = _common.make_controller(args, ctx)
    pipe_alive, live, pipe_error = _common.ping_controller(controller)
    if not pipe_alive:
        result.anomaly("pipe_unreachable", f"controller not reachable: {pipe_error}", "medium")
        result.action("read run-record (controller offline)")
    else:
        result.action("read run-record + live DesktopLUT state")

    emitted = dl.get("stages_emitted", [])
    score_history = dl.get("score_history", [])
    refine_history = dl.get("refine_history", [])
    params = dl.get("mhc_params")

    result.metrics = {
        "run_dir": str(ctx.root),
        "monitor": dl.get("monitor"),
        "mode": dl.get("mode"),
        "stages_emitted": [{"stage": e["stage"], "status": e["status"]} for e in emitted],
        "mhc_params_built": params is not None,
        "refine_iterations": len(refine_history),
        "last_refine": refine_history[-1] if refine_history else None,
        "scores": score_history,
        "correction_grayscale_points": (dl.get("correction_grayscale") or {}).get("point_count"),
    }
    if pipe_alive and isinstance(live, dict):
        result.raw["live"] = {
            "running": live.get("running"),
            "calibration_mode": live.get("calibration_mode"),
            "mhc": live.get("mhc"),
            "runtime": live.get("runtime"),
        }
    result.note("advisory only: this tool reports state; it makes no decision")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC state: run-record + live DesktopLUT state")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
