"""End-to-end ``--simulate`` driver: rehearse the whole v1 SDR loop on the mock.

Runs every stage tool in order against the in-process DesktopLUT simulator and a
synthetic panel, proving the chain wires together from preflight to report
("Ding") without any hardware. This is the harness's smoke test and the
assistant's dry-run target before live bring-up.

Note: the synthetic TI3 models a *perfect* sRGB/D65 panel, so this exercises
*wiring*, not convergence. Convergence of the refinement control law is proven
separately on a tinted synthetic panel in ``tests/test_spine.py``.
"""

from __future__ import annotations

import argparse
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

from ..runs import RunContext, create_run, open_run
from ..stage import StageResult
from . import (
    _common,
    build_3dlut,
    build_mhc,
    check_cube,
    enter_neutral,
    install_3dlut,
    install_mhc,
    measure,
    preflight,
    refine_grayscale,
    report,
    score,
    state,
)

_DEFAULTS: dict[str, Any] = {
    "monitor": 0,
    "mode": "SDR",
    "simulate": True,
    "pipe": _common.DEFAULT_PIPE_NAME,
    "run": None,
    "stage": None,
    "iteration": 1,
    "port": 1,
    "display_index": 1,
    "patch_window": "0.5,0.5,50,50",
    "patch_count": None,
    "correction": None,
    "high_res": False,
    "observer": None,
    "source_ti3": None,
    "gamma": 2.2,
    "damping": 0.7,
    "source_icc": None,
    "display_icc": None,
    "grid_size": 17,
    "quality": "h",
    "cube": None,
    "max_neighbor_delta": 1.0,
    "max_monotonicity_violations": 0,
    "luminance": None,
}


def run_simulation(run_dir: Path | None = None, *, max_refine: int = 3, verbose: bool = False) -> dict[str, Any]:
    """Drive the full loop in-process. Returns a summary dict.

    ``reached_report`` is True iff the chain completed to a non-failed report —
    the "Ding" condition.
    """
    if run_dir is not None and (run_dir / "manifest.json").exists():
        ctx = open_run(run_dir)
    else:
        ctx = create_run("SDR", display="simulation", run_dir=run_dir)
    base = {**_DEFAULTS, "run": ctx.root}

    def ns(**over: Any) -> Namespace:
        return Namespace(**{**base, **over})

    results: list[StageResult] = []

    def step(name: str, result: StageResult) -> StageResult:
        _common.record_stage(ctx, result)
        results.append(result)
        if verbose:
            print(f"[{name}] status={result.status} verdict={result.advice.get('default_policy_verdict')}")
        return result

    step("preflight", preflight.build(ns(), ctx))
    step("enter-neutral", enter_neutral.build(ns(), ctx))
    step("measure:raw-mhc", measure.build(ns(stage="raw-mhc", iteration=1), ctx))
    step("build-mhc", build_mhc.build(ns(), ctx))
    step("install-mhc", install_mhc.build(ns(), ctx))

    refine_iters = 0
    for it in range(1, max_refine + 1):
        step("measure:mhc-verification", measure.build(ns(stage="mhc-verification", iteration=it), ctx))
        r = step("refine-grayscale", refine_grayscale.build(ns(iteration=it), ctx))
        refine_iters = it
        if r.status == "failed" or r.advice.get("default_policy_verdict") == "stop":
            break

    step("measure:post-mhc", measure.build(ns(stage="post-mhc", iteration=1), ctx))
    step("build-3dlut", build_3dlut.build(ns(iteration=1), ctx))
    step("check-cube", check_cube.build(ns(iteration=1), ctx))
    step("install-3dlut", install_3dlut.build(ns(), ctx))
    step("measure:3dlut-verification", measure.build(ns(stage="3dlut-verification", iteration=1), ctx))
    step("score", score.build(ns(stage="3dlut-verification", iteration=1), ctx))
    step("state", state.build(ns(), ctx))
    rep = step("report", report.build(ns(), ctx))

    failed = [r.stage for r in results if r.status in ("failed", "blocked")]
    return {
        "run_dir": str(ctx.root),
        "reached_report": rep.status == "ran" and not failed,
        "refine_iterations": refine_iters,
        "failed_stages": failed,
        "stages": [{"stage": r.stage, "status": r.status} for r in results],
        "report_metrics": rep.metrics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DLC end-to-end --simulate rehearsal")
    parser.add_argument("--run", type=Path, default=None, help="run directory (default: a fresh runs/ folder)")
    parser.add_argument("--max-refine", type=int, default=3, dest="max_refine")
    args = parser.parse_args(argv)
    summary = run_simulation(args.run, max_refine=args.max_refine, verbose=True)
    if summary["reached_report"]:
        print(f"\nDing — calibration loop reached report. Run: {summary['run_dir']}")
    else:
        print(f"\nLoop did NOT finish cleanly; failed/blocked: {summary['failed_stages']}")
    return 0 if summary["reached_report"] else 1


if __name__ == "__main__":
    sys.exit(main())
