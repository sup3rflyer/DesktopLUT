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

from ..metrics import score_samples, summarize_metrics
from ..mhc import find_stage_artifact, parse_ti3, resolve_run_path
from ..runs import RunContext
from ..stage import StageResult
from . import _common


def _score_ti3(ctx: RunContext, ti3: Path, stage: str, gamma: float) -> dict[str, Any] | None:
    if not ti3.exists():
        return None
    samples = parse_ti3(ti3)
    patch_metrics, lum = score_samples(samples, gamma=gamma)
    summary = summarize_metrics(
        phase=stage,
        iteration=0,
        source=ti3,
        patch_metrics=patch_metrics,
        target_luminance=lum,
        metrics_path=ti3,
        patches_path=ti3,
    )
    return {
        "avg_de2000": round(summary.avg_de2000, 4),
        "p95_de2000": round(summary.p95_de2000, 4),
        "max_de2000": round(summary.max_de2000, 4),
        "white_de2000": round(summary.white_de2000, 4),
        "grayscale_avg_de2000": round(summary.grayscale_avg_de2000, 4),
        "target_luminance": round(summary.target_luminance, 4),
    }


def build(args, ctx: RunContext) -> StageResult:
    result = StageResult("report")
    dl = _common.load_dlc_state(ctx)

    raw_ti3 = find_stage_artifact(ctx, "raw-mhc", "ti3")
    before = _score_ti3(ctx, resolve_run_path(ctx, Path(raw_ti3)), "raw-mhc", args.gamma) if raw_ti3 else None

    after = None
    for stage in ("3dlut-verification", "mhc-verification", "verification"):
        ti3 = find_stage_artifact(ctx, stage, "ti3")
        if ti3:
            after = _score_ti3(ctx, resolve_run_path(ctx, Path(ti3)), stage, args.gamma)
            if after:
                after["stage"] = stage
                break
    if after is None and dl.get("score_history"):
        after = dict(dl["score_history"][-1])

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
        "target_white": params.get("white"),
        "target_luminance": params.get("target_luminance"),
        "refine_iterations": len(dl.get("refine_history", [])),
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
            f"final avg dE {after.get('avg_de2000')}, white dE {after.get('white_de2000')}"
            + (f" (CCT ~{_common.cct_mccamy(*params['measured_white'].values()):.0f}K native)" if params.get("measured_white") else "")
        )
    return result


def _render_html(p: dict[str, Any]) -> str:
    def row(label: str, key: str) -> str:
        b = (p["before"] or {}).get(key)
        a = (p["after"] or {}).get(key)
        return f"<tr><td>{label}</td><td>{b if b is not None else '—'}</td><td>{a if a is not None else '—'}</td></tr>"

    return (
        "<!doctype html><meta charset='utf-8'><title>DLC report</title>"
        "<style>body{font-family:system-ui;margin:2rem;color:#222}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.4rem .8rem;text-align:left}"
        "th{background:#f4f4f4}</style>"
        f"<h1>DesktopLUT Calibrator — {p.get('mode')} monitor {p.get('monitor')}</h1>"
        f"<p>Run: <code>{p.get('run_dir')}</code> · refine iterations: {p.get('refine_iterations')}</p>"
        "<table><tr><th>Metric</th><th>Before (raw)</th><th>After (final)</th></tr>"
        + row("Average dE2000", "avg_de2000")
        + row("Max dE2000", "max_de2000")
        + row("White dE2000", "white_de2000")
        + row("Grayscale avg dE2000", "grayscale_avg_de2000")
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
