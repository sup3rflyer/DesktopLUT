"""Stage 9 — report: a slim before/after summary for the human.

Compares the raw panel (scored from the raw-mhc TI3) against the final
verification score and writes report.json + report.html. The assistant confirms
acceptance; this tool only summarises.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..metrics import (
    practical_summary,
    reachable_primaries_from_mhc_params,
    score_samples,
    score_samples_hdr,
    summarize_metrics,
)
from ..mhc import find_stage_artifact, parse_ti3, resolve_run_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common


def _score_ti3(ctx: RunContext, ti3: Path, stage: str, gamma: float,
               white_xy: tuple[float, float], *, is_hdr: bool = False,
               peak_nits: float = 1000.0, reachable: dict | None = None) -> dict[str, Any] | None:
    if not ti3.exists():
        return None
    samples = parse_ti3(ti3)
    # Mode-gate the metric like calibrate.py (dE_ITP HDR-only / CIEDE2000 SDR-only). before/after must
    # use the SAME metric for the improvement delta to mean anything — both flow from the run's mode.
    # The HDR gamut clamp (`reachable`, P1) is likewise applied to BOTH sides, so the improvement
    # delta compares like with like (an unreachable corner is a reachability floor on either side).
    if is_hdr:
        patch_metrics, lum = score_samples_hdr(samples, white_xy=white_xy, peak_nits=peak_nits,
                                               reachable_primaries=reachable)
        metric_name = "dE_ITP"
    else:
        patch_metrics, lum = score_samples(samples, gamma=gamma, white_xy=white_xy)
        metric_name = "CIEDE2000"
    # This report only extracts the dE numbers below — it writes no metrics/patch JSON, so
    # metrics_path/patches_path stay None rather than masquerading as the TI3 path.
    summary = summarize_metrics(
        phase=stage,
        iteration=0,
        source=ti3,
        patch_metrics=patch_metrics,
        target_luminance=lum,
        metric=metric_name,
    )
    return {
        "metric": metric_name,
        "avg_de2000": round(summary.avg_de2000, 4),
        "p95_de2000": round(summary.p95_de2000, 4),
        "max_de2000": round(summary.max_de2000, 4),
        "white_de2000": round(summary.white_de2000, 4),
        "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 4),
        "target_luminance": round(summary.target_luminance, 4),
        "practical": practical_summary(patch_metrics, is_hdr=is_hdr,
                                       gamut_aware=reachable is not None),
    }


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("report")
    dl = _common.load_dlc_state(ctx)
    try:
        target_white_xy, target_white_source = _common.target_white_from_state(dl)
    except ValueError as exc:
        result.fail("invalid_target_white", str(exc))
        return result

    is_hdr = _common.run_mode(args, ctx) == "HDR"
    peak_nits = float((dl.get("mhc_params") or {}).get("target_luminance") or 1000.0)
    metric_name = "dE_ITP" if is_hdr else "CIEDE2000"
    # Gamut-aware like the live verify + the score stage (P1): HDR targets clamp onto the
    # panel's measured native gamut from the run record; None for SDR (never clamps).
    reachable = reachable_primaries_from_mhc_params(dl.get("mhc_params")) if is_hdr else None

    raw_ti3 = find_stage_artifact(ctx, "raw-mhc", "ti3")
    before = _score_ti3(ctx, resolve_run_path(ctx, Path(raw_ti3)), "raw-mhc",
                        args.gamma, target_white_xy, is_hdr=is_hdr, peak_nits=peak_nits,
                        reachable=reachable) if raw_ti3 else None

    after = None
    for stage in ("3dlut-verification", "mhc-verification", "verification"):
        ti3 = find_stage_artifact(ctx, stage, "ti3")
        if ti3:
            after = _score_ti3(ctx, resolve_run_path(ctx, Path(ti3)), stage, args.gamma,
                               target_white_xy, is_hdr=is_hdr, peak_nits=peak_nits,
                               reachable=reachable)
            if after:
                after["stage"] = stage
                break
    if after is None and dl.get("score_history"):
        # Fall back to the last recorded score ONLY if it was scored in this run's metric —
        # a cross-metric before/after "improvement" (CIEDE2000 minus dE_ITP) is meaningless
        # and worse than an honest "no final score".
        last = dict(dl["score_history"][-1])
        if last.get("metric") == metric_name:
            after = last

    params = dl.get("mhc_params", {})
    improvement = None
    if before and after:
        improvement = {
            "avg_de2000": round(before["avg_de2000"] - after["avg_de2000"], 4),
            "white_de2000": round(before["white_de2000"] - after["white_de2000"], 4),
            "max_de2000": round(before["max_de2000"] - after["max_de2000"], 4),
        }

    payload = {
        "run_dir": str(ctx.root),
        "monitor": dl.get("monitor"),
        "mode": dl.get("mode"),
        "measured_primaries": params.get("primaries"),
        "measured_white": params.get("measured_white"),
        "target_white": {"x": round(target_white_xy[0], 6), "y": round(target_white_xy[1], 6)},
        "target_white_source": target_white_source,
        "target_luminance": params.get("target_luminance"),
        "refine_iterations": len(dl.get("refine_history", [])),
        "metric": metric_name,
        "before": before,
        "after": after,
        "improvement": improvement,
    }
    report_json = ctx.root / "reports" / "report.json"
    report_html = ctx.root / "reports" / "report.html"
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report_html.write_text(_render_html(payload), encoding="utf-8")
    result.add_artifact(report_json)
    result.add_artifact(report_html)
    result.action("wrote before/after report (json + html)")

    if before is None:
        result.anomaly("no_baseline", "no raw-mhc TI3 to compute a before score", "low")
    if after is None:
        result.anomaly("no_final", "no verification TI3 to compute a final score", "medium")

    result.metrics = {"before": before, "after": after, "improvement": improvement}
    result.advice = {
        "default_policy_verdict": "finalize",
        "reasons": ["report written; confirm acceptance with the user and report the trade-offs"],
    }
    if after:
        result.note(
            f"final avg {metric_name} {after.get('avg_de2000')}, white {metric_name} {after.get('white_de2000')}"
            + (f" (CCT ~{_common.cct_mccamy(*params['measured_white'].values()):.0f}K native)" if params.get("measured_white") else "")
        )
    return result


def _render_html(p: dict[str, Any]) -> str:
    # Label the rows with the run's actual metric (dE_ITP for HDR, dE2000 for SDR) so an HDR
    # report never prints dE_ITP numbers under a "dE2000" header.
    de = "dE_ITP" if p.get("metric") == "dE_ITP" else "dE2000"

    def row(label: str, key: str) -> str:
        b = (p["before"] or {}).get(key)
        a = (p["after"] or {}).get(key)
        return f"<tr><td>{label}</td><td>{b if b is not None else '—'}</td><td>{a if a is not None else '—'}</td></tr>"

    def zone_row(label: str, zone: str, stat: str) -> str:
        # The §0 practical split (core = the verdict; clamped = at the panel's gamut floor).
        def get(side: str):
            return (((p[side] or {}).get("practical") or {}).get(zone) or {}).get(stat)
        b, a = get("before"), get("after")
        if b is None and a is None:
            return ""
        return f"<tr><td>{label}</td><td>{b if b is not None else '—'}</td><td>{a if a is not None else '—'}</td></tr>"

    return (
        "<!doctype html><meta charset='utf-8'><title>DLC report</title>"
        "<style>body{font-family:system-ui;margin:2rem;color:#222}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.4rem .8rem;text-align:left}"
        "th{background:#f4f4f4}</style>"
        f"<h1>DesktopLUT Calibrator — {p.get('mode')} monitor {p.get('monitor')}</h1>"
        f"<p>Run: <code>{p.get('run_dir')}</code> · refine iterations: {p.get('refine_iterations')}</p>"
        f"<table><tr><th>Metric ({de})</th><th>Before (raw)</th><th>After (final)</th></tr>"
        + row(f"Average {de}", "avg_de2000")
        + row(f"Max {de}", "max_de2000")
        + row(f"White {de}", "white_de2000")
        + row(f"Grayscale avg {de}", "grayscale_avg_de2000")
        + zone_row(f"Practical core avg {de} (Rec.709 ≤ ref-white)", "core", "avg")
        + zone_row(f"Practical core max {de}", "core", "max")
        + zone_row(f"At gamut floor avg {de} (unreachable targets)", "clamped", "avg")
        + "</table>"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _common.base_parser("DLC report: slim before/after summary")
    parser.add_argument("--gamma", type=float, default=2.2)
    args = parser.parse_args(argv)
    ctx = _common.resolve_run(args, create=False)
    args.run = ctx.root
    result = build(args, ctx)
    _common.emit_and_record(ctx, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
