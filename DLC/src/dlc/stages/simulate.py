"""End-to-end ``--simulate`` driver: rehearse the whole v1 SDR loop on the mock.

Runs every stage tool in order against the in-process DesktopLUT simulator and a
synthetic panel, proving the chain wires together from preflight to report
("Ding") without any hardware. This is the harness's smoke test and the
assistant's dry-run target before live bring-up.

Note: the synthetic TI3 models a *perfect* sRGB/D65 panel, so this exercises
*wiring*, not convergence. Convergence of the refinement control law is proven
separately on a tinted synthetic panel in ``tests/test_spine.py``.

``--flow verify-only`` rehearses the orchestrator's ``verify-only`` flow instead
(:func:`run_verify_only_rehearsal`): a small ``full`` run installs a stack on the mock
(and is the recorded source run), then verify-only measures it three ways — the
installed stack as-is, the source's exact verify set (``--verify-patches-from``,
deltas vs its recorded verify), and a candidate cube over it (``--verify-cube``,
restored at ``verify:candidate``). The in-process rubber-stamp adjudicator stands in
for the LLM — sim only, never a hardware run.
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
    "max_neighbor_delta": None,   # None ⇒ the grid-pitch-derived structural default
    "max_monotonicity_violations": 0,
    "luminance": None,
    "target_white_xy": None,
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


def run_verify_only_rehearsal(root: Path | None = None, *, mode: str = "SDR",
                              verbose: bool = False) -> dict[str, Any]:
    """Rehearse ``--flow verify-only`` end to end on the mock (orchestrator-level, not the v1
    stage chain). Returns a summary; ``reached_report`` is True iff every leg completed, the
    installed stack was left exactly as found, and the candidate leg restored the prior cube."""
    import shutil
    from datetime import datetime

    from .. import calibration_profile as cp
    from ..calibrate import AutoAdjudicator, Calibration
    from ..controller import CalibrationController, normalize_mode
    from ..engine.patches import Transfer
    from ..measure_loop import SyntheticPanel
    from ..optimize import OptimizeConfig
    from ..paths import runs_dir
    from ..patch_sets import PatchSizes

    mode = normalize_mode(mode)
    root = (Path(root) if root is not None
            else runs_dir() / f"_rehearsal_verify_{datetime.now():%Y%m%d_%H%M%S}").resolve()
    if root.exists() and any(root.iterdir()):
        # The mock controller lives in THIS process: a replayed (memoised) stack leg would install
        # nothing on a fresh mock, so a re-used root cannot rehearse anything.
        return {"root": str(root), "mode": mode, "flow": "verify-only", "reached_report": False,
                "legs": [], "error": "rehearsal root is not empty — pass a fresh --run directory"}
    root.mkdir(parents=True, exist_ok=True)
    hdr = mode == "HDR"
    transfer = Transfer.pq(bit_depth=10) if hdr else Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    panel_kw: dict[str, Any] = {"native_white_nits": 1840.0} if hdr else {}
    profile = cp.Profile.synthetic(output_dir=str(root / "results"))
    controller = CalibrationController.mock()
    # Small sets: this proves the WIRING (a perfect panel reads its target), not convergence.
    sizes = PatchSizes(raw_ramp_steps=9, cube_size=3, tube_size=5, tube_radius=1, neutral_steps=9)
    opt = OptimizeConfig(grid_size=9, max_outer=3, threshold=2.0)

    def leg(name: str, flow: str, **kw: Any) -> tuple[Any, Calibration]:
        run_dir = root / name
        ctx = open_run(run_dir) if (run_dir / "manifest.json").exists() \
            else create_run(mode, display="synthetic", run_dir=run_dir)
        panel = SyntheticPanel(transfer=transfer, start_temp=1.0, cold_blue_gain=1.0, **panel_kw)
        calib = Calibration(ctx=ctx, profile=profile, monitor=0, mode=mode, controller=controller,
                            measure=panel, adjudicator=AutoAdjudicator(), bit_depth=10,
                            patch_sizes=sizes, optimize_config=opt, **kw)
        result = calib.run(flow)
        if verbose:
            print(f"[{name}] {flow}: status={result.status} stages={result.stages}")
        return result, calib

    def live() -> dict[str, Any]:
        st = controller.state() or {}
        key = f"0:{mode}"
        return {"cube": ((st.get("runtime") or {}).get(key) or {}).get("cube_path"),
                "mhc": (st.get("mhc") or {}).get(key)}

    def verify_view(calib: Calibration) -> dict[str, Any]:
        d = ((calib.calib.get("stages") or {}).get("verify") or {}).get("digest") or {}
        vs = d.get("vs_source") or {}
        view: dict[str, Any] = {"metric": d.get("metric"), "patch_count": d.get("patch_count"),
                                "gate": (d.get("gate") or {}).get("scored"),
                                "within_quality": d.get("within_quality"),
                                "measured_stack": (d.get("verify_only") or {}).get("measured_stack")}
        if vs.get("headline"):
            view["vs_source"] = {
                "like_for_like": (vs.get("comparability") or {}).get("like_for_like"),
                "headline": vs.get("headline"),
                "buckets": {k: (v.get("avg") or {}) for k, v in (vs.get("buckets") or {}).items()}}
        return view

    legs: list[dict[str, Any]] = []
    stack, stack_calib = leg("stack_full", "full")
    legs.append({"leg": "stack_full", "flow": "full", "status": stack.status})
    ok = stack.status == "completed"
    if ok:
        before = live()
        res, calib = leg("verify_installed", "verify-only")
        unchanged = live() == before
        legs.append({"leg": "verify_installed", "status": res.status, "stack_unchanged": unchanged,
                     "results_dir": res.results_dir, "verify": verify_view(calib)})
        ok = ok and res.status == "completed" and unchanged

        res, calib = leg("verify_patches_from", "verify-only", verify_patches_from=stack_calib.ctx.root)
        unchanged = live() == before
        legs.append({"leg": "verify_patches_from", "status": res.status, "stack_unchanged": unchanged,
                     "results_dir": res.results_dir, "verify": verify_view(calib)})
        ok = ok and res.status == "completed" and unchanged

        # A "candidate": the stack's own deliverable cube under another name (wiring, not a new LUT).
        cand = root / "candidate.cube"
        shutil.copy2(before["cube"], cand)
        res, calib = leg("verify_candidate", "verify-only", verify_cube=cand,
                         verify_patches_from=stack_calib.ctx.root)
        restored = live() == before
        rec = calib.calib.get("verify_candidate") or {}
        legs.append({"leg": "verify_candidate", "status": res.status, "prior_restored": restored,
                     "candidate": {k: rec.get(k) for k in ("cube", "prior_cube", "installed", "restored", "kept")},
                     "decision": ((calib.calib.get("decisions") or {}).get("verify:candidate") or {}).get("choice"),
                     "results_dir": res.results_dir, "verify": verify_view(calib)})
        ok = ok and res.status == "completed" and restored and bool(rec.get("restored"))
    return {"root": str(root), "mode": mode, "flow": "verify-only", "reached_report": ok, "legs": legs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DLC end-to-end --simulate rehearsal")
    parser.add_argument("--run", type=Path, default=None, help="run directory (default: a fresh runs/ folder)")
    parser.add_argument("--max-refine", type=int, default=3, dest="max_refine")
    parser.add_argument("--flow", choices=("verify-only",), default=None,
                        help="rehearse an orchestrator flow instead of the v1 stage chain: verify-only = "
                             "install a stack (small full run), then verify it as-is / with the source's "
                             "exact set / with a candidate cube (restored). --run is the rehearsal root.")
    parser.add_argument("--mode", choices=("SDR", "HDR"), default="SDR",
                        help="the run mode for --flow rehearsals (default SDR)")
    args = parser.parse_args(argv)
    if args.flow == "verify-only":
        import json

        vsummary = run_verify_only_rehearsal(args.run, mode=args.mode, verbose=True)
        print(json.dumps(vsummary, indent=2, default=str))
        verdict = "Ding" if vsummary["reached_report"] else "NOT clean"
        print(f"\n{verdict} — verify-only rehearsal: {vsummary['root']}")
        return 0 if vsummary["reached_report"] else 1
    summary = run_simulation(args.run, max_refine=args.max_refine, verbose=True)
    if summary["reached_report"]:
        print(f"\nDing — calibration loop reached report. Run: {summary['run_dir']}")
    else:
        print(f"\nLoop did NOT finish cleanly; failed/blocked: {summary['failed_stages']}")
    return 0 if summary["reached_report"] else 1


if __name__ == "__main__":
    sys.exit(main())
