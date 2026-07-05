"""Stages 5/8 — score: a verification TI3 against the target.

The numbers-only end gate: avg/p95/max/white/grayscale dE plus a delta vs. the
previous score for the same stage and an advisory stop/continue verdict. The
assistant decides acceptance.

Mode-gated exactly like the live ``calibrate.py`` path (owner directive: dE_ITP for
HDR only, CIEDE2000 for SDR only). An HDR run scores ``dE_ITP`` (BT.2124) against the
PQ/Rec.2020 target — CIEDE2000's Lab is meaningless at HDR absolute luminance, so the
old unconditional-CIEDE2000 path produced ~30+ dE garbage on HDR data. Also GAMUT-AWARE
exactly like the live verify (P1): the HDR target is clamped onto the panel's measured
native gamut from the run record, and every summary carries the §0 practical split
(core / limits / at-the-gamut-floor) alongside the raw numbers.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..metrics import (
    practical_summary,
    reachable_primaries_from_mhc_params,
    score_samples,
    score_samples_hdr,
    write_metrics,
)
from ..mhc import parse_ti3, resolve_run_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common
from ..decisions import hdr_metric_thresholds, metric_thresholds_for_run


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult(f"score:{args.stage}")

    source = Path(args.source_ti3) if args.source_ti3 else None
    if source is None:
        from ..mhc import find_stage_artifact

        source = find_stage_artifact(ctx, args.stage, "ti3")
    if source is None or not Path(source).exists():
        result.fail("no_ti3", f"no TI3 to score; run `measure --stage {args.stage}` or pass --source-ti3")
        return result
    source = resolve_run_path(ctx, Path(source))
    result.add_artifact(source)

    dl_state = _common.load_dlc_state(ctx)
    try:
        explicit_white = _common.parse_target_white_xy(getattr(args, "target_white_xy", None))
        if explicit_white is not None:
            target_white_xy, target_white_source = explicit_white, "explicit"
        else:
            target_white_xy, target_white_source = _common.target_white_from_state(dl_state)
    except ValueError as exc:
        result.fail("invalid_target_white", str(exc))
        return result

    samples = parse_ti3(source)
    # Mode-gate the metric exactly like calibrate.py (dE_ITP HDR-only / CIEDE2000 SDR-only). The run's
    # FIXED mode comes from the manifest (run_mode), not the SDR-defaulting --mode flag, so a flagless
    # resume of an HDR run still scores in dE_ITP.
    is_hdr = _common.run_mode(args, ctx) == "HDR"
    if is_hdr:
        # peak_nits is a reported number only (PQ is absolute) — the dE_ITP math needs just the white.
        peak = (args.luminance
                or (dl_state.get("mhc_params") or {}).get("target_luminance") or 1000.0)
        # Gamut-aware exactly like the live verify (P1): clamp the target onto the panel's
        # MEASURED native gamut from the run record (the same mhc_params.primaries the live
        # path prefers), so a stage-CLI HDR score never counts an unreachable BT.2020 corner
        # as calibration error while the live run doesn't. No build yet ⇒ no clamp — surfaced
        # as gamut_aware=false below, never silently.
        reachable = reachable_primaries_from_mhc_params(dl_state.get("mhc_params"))
        patch_metrics, target_luminance = score_samples_hdr(
            samples, white_xy=target_white_xy, peak_nits=float(peak),
            reachable_primaries=reachable,
        )
        metric_name = "dE_ITP"
        thresholds = hdr_metric_thresholds(
            ctx.manifest.desktoplut.get("quality_policy") if ctx else None
        )
    else:
        reachable = None   # production SDR never clamps (CV-gated worse; see score_samples)
        patch_metrics, target_luminance = score_samples(
            samples, luminance=args.luminance, gamma=args.gamma, white_xy=target_white_xy
        )
        metric_name = "CIEDE2000"
        thresholds = metric_thresholds_for_run(ctx, args.stage)
    gamut_aware = reachable is not None
    practical = practical_summary(patch_metrics, is_hdr=is_hdr, gamut_aware=gamut_aware)
    # One producer shape (P4): write_metrics owns the artifacts (metrics + per-patch rows —
    # the file the dashboard's /api/patch_metrics globs for) and the canonical
    # metrics_scored event, so a stage-CLI run fills the same dashboard ΔE panel a live
    # run does (previously: no event, blank cells; a declared patches_path never written).
    summary = write_metrics(
        ctx=ctx,
        phase=f"score_{args.stage}",
        iteration=args.iteration,
        source=source,
        patch_metrics=patch_metrics,
        target_luminance=target_luminance,
        metric=metric_name,
        practical=practical,
        label=args.stage,
    )
    result.add_artifact(Path(summary.metrics_path))
    result.add_artifact(Path(summary.patches_path))
    result.action(
        f"scored {summary.patch_count} patches ({metric_name}) vs "
        + ("PQ/Rec.2020" if is_hdr else f"gamma {args.gamma}")
        + f" / white {target_white_xy[0]:.6f},{target_white_xy[1]:.6f} ({target_white_source})"
        + (f" / gamut-aware (native primaries from the run record)" if gamut_aware else "")
    )

    # Worst offenders, for the assistant to inspect. (`de2000` is the generic ΔE carrier field — it
    # holds dE_ITP for an HDR run; the `metric` label names the units.) `gamut_clamped` marks a
    # patch scored against the panel's gamut boundary — a reachability floor, not a cube miss.
    worst = sorted(patch_metrics, key=lambda m: m.de2000, reverse=True)[:5]
    result.raw["worst_patches"] = [
        {"rgb": [round(c, 4) for c in m.rgb], "de2000": round(m.de2000, 3),
         "grayscale": m.grayscale, "gamut_clamped": m.gamut_clamped}
        for m in worst
    ]
    if summary.max_de2000 > thresholds.max_de2000:
        result.anomaly(
            "large_max_de",
            f"max {metric_name} {summary.max_de2000:.2f} exceeds {thresholds.max_de2000:.2f}",
            "medium",
        )

    metrics = {
        "stage": args.stage,
        "iteration": args.iteration,
        "metric": metric_name,
        "avg_de2000": round(summary.avg_de2000, 4),
        "p95_de2000": round(summary.p95_de2000, 4),
        "p99_de2000": round(summary.p99_de2000, 4),
        "max_de2000": round(summary.max_de2000, 4),
        "white_de2000": round(summary.white_de2000, 4),
        "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 4),
        "grayscale_max_de2000": round(summary.grayscale_max_de2000, 4),
        "colour_avg_de2000": (round(summary.colour_avg_de2000, 4)
                              if summary.colour_avg_de2000 is not None else None),
        "target_luminance": round(summary.target_luminance, 4),
        "target_white_xy": [round(target_white_xy[0], 6), round(target_white_xy[1], 6)],
        "target_white_source": target_white_source,
        "gamut_aware": gamut_aware,
        "practical": practical,
    }

    # Delta vs previous score for the same stage + history for the report.
    score_history = dl_state.setdefault("score_history", [])
    prev = next((s for s in reversed(score_history) if s.get("stage") == args.stage), None)
    score_history.append(metrics)
    _common.save_dlc_state(ctx, dl_state)
    deltas = {}
    if prev:
        deltas = {
            "avg_de2000": round(metrics["avg_de2000"] - prev["avg_de2000"], 4),
            "max_de2000": round(metrics["max_de2000"] - prev["max_de2000"], 4),
            "white_de2000": round(metrics["white_de2000"] - prev["white_de2000"], 4),
        }

    result.preconditions = {"ti3_present": True}
    result.metrics = metrics
    result.deltas = deltas
    result.advice = _common.policy_advice(
        metrics,
        previous_avg=prev["avg_de2000"] if prev else None,
        thresholds=thresholds,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC score: verification TI3 (CIEDE2000 SDR / dE_ITP HDR)")
    parser.add_argument("--stage", default="3dlut-verification", help="measure stage whose TI3 to score")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--source-ti3", default=None, dest="source_ti3")
    parser.add_argument("--gamma", type=float, default=2.2)
    parser.add_argument("--luminance", type=float, default=None, help="target white luminance (default: inferred)")
    _common.add_target_white_args(parser)
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result, iteration=args.iteration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
