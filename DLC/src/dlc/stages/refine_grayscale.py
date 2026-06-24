"""Stage 5 — refine-grayscale: one closed-loop MHC base-cube refine step.

Reads a post-MHC verification ramp, scores the measured grey residual (via the
refinement control law ``refine.propose_correction_grayscale``, for the digest +
advice), then composes the correction onto the DLC-owned per-channel base 1D-LUT
**cube** (``mhc_cube.refine_sdr_cube``) and reinstalls it over ``set_base_lut``.
As of 2026-06-24 this drives a DLC-owned cube, NOT the user-editable
``correctionGrayscale`` slot (a user "Reset Grayscale" wiped it; a loaded cube
locks that editor) — see [[dlc-must-not-own-mhc-user-layers]]. Emits per-point
residuals, the white shift, and a delta vs. the previous iteration. The assistant
decides whether to loop again. (Sim-wiring stage used by the mock rehearsal; the
hardware path is the orchestrator's ``stage_refine_mhc_grayscale``.)
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..mhc import SRGB_PRIMARIES, find_stage_artifact, parse_ti3, resolve_run_path
from ..mhc_cube import mhc2_matrix, read_1d_cube, refine_sdr_cube, write_1d_cube
from ..refine import DEV_MAX, DEV_MIN, Deviations, propose_correction_grayscale
from ..runs import RunContext
from ..stage import StageResult
from . import _common


def build(args, ctx: RunContext) -> StageResult:
    mode = _common.run_mode(args, ctx)
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

    # Refine the DLC-owned base 1D-LUT cube (NOT the user-editable correctionGrayscale slot —
    # [[dlc-must-not-own-mhc-user-layers]]) and re-bake. propose_correction_grayscale above is kept
    # ONLY to score the measured residual (summary/residuals/advice); the correction itself is composed
    # onto the per-channel cube via refine_sdr_cube and installed over set_base_lut.
    base_lut = params.get("base_lut") or {}
    cube_path = base_lut.get("cube_path")
    if not (cube_path and Path(cube_path).exists()):
        result.fail("no_base_cube", "no SDR base 1D-LUT cube to refine (run build-mhc + install-mhc first)")
        return result
    gamma = float(getattr(target, "gamma", 2.2))
    native_white = _measured_white_tuple(params)
    peak = float(params.get("target_luminance") or 0.0)
    matrix = mhc2_matrix(params["primaries"], native_white, SRGB_PRIMARIES, (0.3127, 0.3290))
    rowsums = [sum(matrix[r]) for r in range(3)]
    measured_neutral = [(g.level, tuple(g.xyz)) for g in gray]
    controller = _common.make_controller(args, ctx)
    try:
        new_curves = refine_sdr_cube(read_1d_cube(Path(cube_path)), measured_neutral,
                                     params["primaries"], native_white, peak, rowsums,
                                     gamma=gamma, target_white_xy=(target.white_x, target.white_y))
        new_path = ctx.root / "generated" / f"mhc_base_{mode.lower()}.refine{iteration}.cube"
        write_1d_cube(new_path, new_curves, title=f"DLC {mode} MHC refine r{iteration} (mon {monitor})")
        result.add_artifact(new_path)
        controller.set_base_lut(monitor, mode, str(new_path.resolve()), 0.0)
        result.action("pushed refined base 1D-LUT cube (set_base_lut)")
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

    # Persist the refined base cube + history, compute deltas vs previous iteration. The cube owns the
    # neutral correction now; correctionGrayscale is left identity (the deprecated user slot).
    base_lut["cube_path"] = str(new_path)
    params["base_lut"] = base_lut                  # params is state["mhc_params"]; keep the deliverable in sync
    nid = proposal["point_count"]
    state["correction_grayscale"] = {
        "point_count": nid,
        "points": proposal["points"],
        "deviations": {ch: [1.0] * nid for ch in ("r", "g", "b")},
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
