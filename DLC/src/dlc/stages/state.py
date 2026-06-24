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
    monitor = dl.get("monitor", getattr(args, "monitor", None))
    mode = _common.normalize_mode(dl.get("mode", getattr(args, "mode", "SDR")))
    runtime_key = f"{monitor}:{mode}" if monitor is not None else None
    runtime_layer = {}
    overlay_path_enabled = None
    runtime_3dlut_path = None
    runtime_3dlut_loaded = None
    if pipe_alive and isinstance(live, dict):
        runtime_layer = (live.get("runtime") or {}).get(runtime_key, {}) if runtime_key else {}
        runtime_3dlut_path = runtime_layer.get("cube_path") if isinstance(runtime_layer, dict) else None
        runtime_3dlut_loaded = bool(runtime_3dlut_path)
        # The wire field `corrections_enabled` is the OVERLAY-draw flag, NOT "is a correction live"
        # (../docs/NAMING.md §4). In DWM-hook mode the overlay is idle, so it reads FALSE even with
        # a 3D-LUT cube live through the hook. We surface it as `overlay_path_enabled` and judge an
        # actual live correction from `runtime_3dlut_loaded` (cube_path) instead.
        overlay_path_enabled = live.get("corrections_enabled")

    result.metrics = {
        "run_dir": str(ctx.root),
        "monitor": monitor,
        "mode": mode,
        "stages_emitted": [{"stage": e["stage"], "status": e["status"]} for e in emitted],
        "mhc_params_built": params is not None,
        "refine_iterations": len(refine_history),
        "last_refine": refine_history[-1] if refine_history else None,
        "scores": score_history,
        "correction_grayscale_points": (dl.get("correction_grayscale") or {}).get("point_count"),
        "overlay_path_enabled": overlay_path_enabled,
        "runtime_3dlut_loaded": runtime_3dlut_loaded,
        "runtime_3dlut_path": runtime_3dlut_path,
    }
    if pipe_alive and isinstance(live, dict):
        result.raw["live"] = {
            "running": live.get("running"),
            "overlay_path_enabled": live.get("corrections_enabled"),
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
