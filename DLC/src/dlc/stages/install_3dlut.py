"""Stage 7c — install-3dlut: load the runtime cube into DesktopLUT.

Routes ``runtime.set_3dlut`` through the controller and confirms via
``state.get`` that the runtime layer now points at the cube.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..lut3d import latest_3dlut_cube
from ..mhc import resolve_run_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.normalize_mode(args.mode)
    result = StageResult("install-3dlut")

    cube_path = Path(args.cube) if args.cube else latest_3dlut_cube(ctx)
    if cube_path is None or not Path(cube_path).exists():
        result.fail("no_cube", "no 3D LUT cube found; run build-3dlut/check-cube or pass --cube")
        return result
    cube_path = resolve_run_path(ctx, Path(cube_path))
    result.add_artifact(cube_path)

    state = _common.load_dlc_state(ctx)
    monitor = state.get("monitor", args.monitor)
    controller = _common.make_controller(args, ctx)
    try:
        controller.set_3dlut(monitor, mode, str(cube_path))
        result.action("set runtime 3D LUT")
        live = controller.state()
    except Exception as exc:  # noqa: BLE001
        result.fail("apply_error", f"runtime.set_3dlut failed: {type(exc).__name__}: {exc}")
        return result

    key = f"{monitor}:{mode}"
    runtime = live.get("runtime", {}).get(key, {}) if isinstance(live, dict) else {}
    installed = str(runtime.get("cube_path", "")).endswith(Path(cube_path).name)
    if not installed:
        result.anomaly("not_confirmed", f"state.get did not show the cube at runtime[{key}]", "high")
    result.raw["runtime"] = runtime

    result.preconditions = {"cube_present": True}
    result.metrics = {"monitor": monitor, "mode": mode, "cube": str(cube_path), "installed": installed}
    result.advice = {
        "default_policy_verdict": "verify_3dlut" if installed else "investigate",
        "reasons": ["cube installed at runtime; measure to verify" if installed else "install not confirmed"],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC install-3dlut: load runtime cube")
    parser.add_argument("--cube", default=None, help="cube to install (default: latest built)")
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
