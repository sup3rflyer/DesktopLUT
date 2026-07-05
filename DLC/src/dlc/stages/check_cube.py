"""Stage 7b — check-cube: structural integrity of a generated 3D LUT.

Bounds, entry count, monotonicity, and neighbour-delta checks. Reports the
integrity summary and a list of suspect points; it does not decide acceptance.
A failing cube is a strong signal to rebuild (e.g. coarser grid) — but the
assistant owns that call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..lut3d import latest_3dlut_cube
from ..lut_integrity import parse_cube, summarize_lut_integrity
from ..mhc import resolve_run_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("check-cube")

    cube_path = Path(args.cube) if args.cube else latest_3dlut_cube(ctx)
    if cube_path is None or not Path(cube_path).exists():
        result.fail("no_cube", "no 3D LUT cube found; run build-3dlut or pass --cube")
        return result
    cube_path = resolve_run_path(ctx, Path(cube_path))
    result.add_artifact(cube_path)

    integrity_path = ctx.root / "reports" / f"3dlut_iter{args.iteration:02d}_lut_integrity.json"
    integrity_path.parent.mkdir(parents=True, exist_ok=True)
    cube = parse_cube(cube_path)
    summary = summarize_lut_integrity(
        cube=cube,
        phase="3dlut",
        iteration=args.iteration,
        integrity_path=integrity_path,
        max_neighbor_delta_allowed=args.max_neighbor_delta,
        monotonicity_violations_allowed=args.max_monotonicity_violations,
    )
    integrity_path.write_text(json.dumps(summary.as_dict(), indent=2), encoding="utf-8")
    result.add_artifact(integrity_path)
    result.action("parsed cube and checked bounds/monotonicity/neighbour deltas")

    for note in summary.notes:
        if "within configured integrity thresholds" not in note:
            result.anomaly("integrity", note, "high" if not summary.ok else "low")

    result.preconditions = {"cube_present": True}
    result.metrics = {
        "cube": str(cube_path),
        "ok": summary.ok,
        "size": summary.size,
        "expected_entries": summary.expected_entries,
        "actual_entries": summary.actual_entries,
        "out_of_bounds_count": summary.out_of_bounds_count,
        "monotonicity_violations": summary.monotonicity_violations,
        "max_neighbor_delta": round(summary.max_neighbor_delta, 6),
        "avg_neighbor_delta": round(summary.avg_neighbor_delta, 6),
    }
    result.advice = {
        "default_policy_verdict": "install" if summary.ok else "rebuild",
        "reasons": ["cube structurally sound" if summary.ok else "cube failed integrity (see anomalies)"],
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC check-cube: 3D LUT structural integrity")
    parser.add_argument("--cube", default=None, help="cube to check (default: latest built)")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument(
        "--max-neighbor-delta", type=float, default=None, dest="max_neighbor_delta",
        help="structural ceiling for a neighbour step (default: derived from the grid pitch "
             "— see lut_integrity.default_neighbor_delta_allowed)")
    parser.add_argument(
        "--max-monotonicity-violations", type=int, default=0, dest="max_monotonicity_violations"
    )
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result, iteration=args.iteration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
