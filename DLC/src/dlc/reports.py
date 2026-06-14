"""Calibration report rendering."""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import ArtifactRecord, scan_artifacts
from .events import Event, read_events
from .runs import RunContext


@dataclass(frozen=True)
class ReportSection:
    title: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibrationReport:
    title: str
    run_name: str
    verdict: str
    sections: list[ReportSection]
    artifacts: list[dict[str, Any]] = field(default_factory=list)


def write_report_json(report: CalibrationReport, path: Path) -> None:
    path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")


def initial_report(run_name: str) -> CalibrationReport:
    return CalibrationReport(
        title="DesktopLUT Calibrator Report",
        run_name=run_name,
        verdict="incomplete",
        sections=[
            ReportSection(
                title="Summary",
                summary="Calibration run has not completed yet.",
            ),
            ReportSection(
                title="Iteration History",
                summary="MHC and 3D LUT iteration metrics will appear here.",
            ),
            ReportSection(
                title="System State",
                summary="DesktopLUT and Windows color state snapshots will appear here.",
            ),
        ],
    )


def report_verdict(ctx: RunContext) -> str:
    if ctx.manifest.status == "finalized":
        return "finalized"
    if ctx.manifest.status == "audited":
        return "audited"
    if any(entry.get("stage") == "apply_mhc_baseline" and entry.get("status") == "applied" for entry in ctx.manifest.stages):
        return "partial"
    if ctx.manifest.stages:
        return "incomplete"
    return "created"


def write_report_html(ctx: RunContext, path: Path | None = None) -> Path:
    artifacts = scan_artifacts(ctx.root)
    events = read_events(ctx.events_path)
    path = path or ctx.root / "reports" / "calibration_report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report_html(ctx, artifacts, events), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "final_report",
            "status": "written",
            "artifact": str(path),
        }
    )
    ctx.save()
    ctx.log(f"Report written: {path}")
    return path


def render_report_html(ctx: RunContext, artifacts: list[ArtifactRecord], events: list[Event]) -> str:
    manifest = ctx.manifest
    verdict = report_verdict(ctx)
    calibration_mode = manifest.desktoplut.get("calibration_mode", {})
    final_desktoplut_state = latest_desktoplut_state_capture(manifest.desktoplut)
    final_windows_state = latest_windows_color_state_capture(manifest.desktoplut)
    last_apply = manifest.desktoplut.get("last_mhc_apply", {})
    last_mhc_smoke = manifest.desktoplut.get("last_mhc_smoke", {})
    candidate = latest_stage(manifest.stages, "build_mhc_baseline")
    applied = latest_stage(manifest.stages, "apply_mhc_baseline")
    metrics = latest_metrics(manifest.stages)
    patch_metrics = load_patch_metrics(metrics)
    decision = latest_decision(manifest.stages)
    lut_integrity = latest_lut_integrity(manifest.stages)
    metric_history = metric_history_entries(manifest.stages)
    decision_history = decision_history_entries(manifest.stages)
    integrity_history = lut_integrity_history_entries(manifest.stages)
    probe_match = probe_match_summary(ctx)
    pipeline = pipeline_evidence_summary(ctx)
    provenance = automation_provenance_summary(ctx)
    loop_status = loop_status_summary(ctx)
    quality_policy = quality_policy_summary(ctx)
    adaptive_drift = adaptive_drift_summary(ctx)
    completion = completion_proof_summary(ctx)
    executive_summary = executive_summary_items(
        ctx=ctx,
        verdict=verdict,
        artifacts=artifacts,
        metrics=metrics,
        decision=decision,
        lut_integrity=lut_integrity,
        final_desktoplut_state=final_desktoplut_state,
        final_windows_state=final_windows_state,
        probe_match=probe_match,
        pipeline=pipeline,
        loop_status=loop_status,
    )

    body = f"""
    <section class="hero">
      <div>
        <p class="eyebrow">DesktopLUT Calibrator</p>
        <h1>{escape(manifest.display or "Display")} {escape(manifest.mode)} Report</h1>
        <p class="subtle">Run {escape(manifest.name)} created {escape(manifest.created)}</p>
      </div>
      <div class="verdict {escape(verdict)}">{escape(verdict.upper())}</div>
    </section>

    <section>
      <h2>Executive Summary</h2>
      <p class="summary-text">{escape(executive_summary_text(verdict, metrics, decision))}</p>
      <div class="grid executive">
        {summary_card("Outcome", executive_summary["Outcome"])}
        {summary_card("Target", executive_summary["Target"])}
        {summary_card("Calibration Evidence", executive_summary["Calibration Evidence"])}
        {summary_card("System Evidence", executive_summary["System Evidence"])}
      </div>
    </section>

    <section class="grid">
      {summary_card("Run", {"Mode": manifest.mode, "Display": manifest.display or "", "Status": manifest.status})}
      {summary_card("Evidence", {"Events": str(len(events)), "Artifacts": str(len(artifacts)), "Stages": str(len(manifest.stages))})}
      {summary_card("Calibration Mode", calibration_mode_summary(calibration_mode))}
      {summary_card("Probe Match", probe_match)}
      {summary_card("Adaptive Drift", adaptive_drift)}
      {summary_card("Toolchain Evidence", pipeline)}
      {summary_card("Automation Provenance", provenance)}
      {summary_card("Completion Proof", completion)}
      {summary_card("Quality Policy", quality_policy)}
      {summary_card("DesktopLUT Final State", desktoplut_state_summary(final_desktoplut_state))}
      {summary_card("Windows Color State", windows_color_state_summary(final_windows_state))}
      {summary_card("MHC", mhc_summary(candidate, applied, last_apply, last_mhc_smoke))}
      {summary_card("Latest Metrics", metrics_summary(metrics))}
      {summary_card("LUT Integrity", lut_integrity_summary(lut_integrity))}
      {summary_card("Latest Decision", decision_summary(decision))}
      {summary_card("Loop Status", loop_status)}
    </section>

    <section>
      <h2>Metric Charts</h2>
      {metric_charts(metrics, patch_metrics)}
    </section>

    <section>
      <h2>Iteration History</h2>
      {iteration_history_section(metric_history, decision_history, integrity_history)}
    </section>

    <section>
      <h2>Stage Timeline</h2>
      {stage_table(manifest.stages)}
    </section>

    <section>
      <h2>Human Actions</h2>
      {human_action_table(manifest.human_actions)}
    </section>

    <section>
      <h2>Recent Events</h2>
      {event_table(events[-12:])}
    </section>

    <section>
      <h2>Artifact Index</h2>
      {artifact_table(artifacts)}
    </section>
    """
    return html_document("DesktopLUT Calibrator Report", body)


def html_document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #182026;
      --muted: #65717a;
      --line: #d7dde2;
      --panel: #f8fafb;
      --accent: #1d6f8f;
      --good: #166b3a;
      --warn: #9a5b00;
      --bad: #a33131;
    }}
    body {{
      margin: 0;
      color: var(--ink);
      background: #ffffff;
      font: 14px/1.5 "Segoe UI", Arial, sans-serif;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 24px 48px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 24px;
      padding-bottom: 20px;
      border-bottom: 2px solid var(--ink);
    }}
    h1 {{
      margin: 0;
      font-size: 34px;
      line-height: 1.1;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 30px 0 10px;
      font-size: 18px;
      letter-spacing: 0;
    }}
    .eyebrow {{
      margin: 0 0 6px;
      color: var(--accent);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 12px;
    }}
    .subtle {{
      color: var(--muted);
      margin: 8px 0 0;
    }}
    .verdict {{
      border: 1px solid var(--line);
      padding: 10px 14px;
      font-weight: 700;
      min-width: 120px;
      text-align: center;
      border-radius: 4px;
      background: var(--panel);
    }}
    .verdict.partial {{ color: var(--warn); border-color: #ddb86a; }}
    .verdict.audited, .verdict.finalized {{ color: var(--good); border-color: #8abf9b; }}
    .verdict.incomplete, .verdict.created {{ color: var(--muted); }}
    .verdict.failed {{ color: var(--bad); border-color: #d99; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .summary-text {{
      max-width: 850px;
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 15px;
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 14px;
      background: var(--panel);
    }}
    .card h3 {{
      margin: 0 0 8px;
      font-size: 14px;
    }}
    dl {{
      margin: 0;
      display: grid;
      grid-template-columns: minmax(90px, auto) 1fr;
      gap: 4px 10px;
    }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      border: 1px solid var(--line);
      font-size: 13px;
    }}
    th, td {{
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #eef2f5;
      font-weight: 700;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    code {{
      font-family: Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: var(--muted);
      border: 1px solid var(--line);
      padding: 10px;
      background: var(--panel);
      border-radius: 4px;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 14px;
    }}
    .chart {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      background: #fff;
    }}
    .chart h3 {{
      margin: 0 0 8px;
      font-size: 14px;
    }}
    .chart svg {{
      width: 100%;
      height: auto;
      display: block;
      overflow: visible;
    }}
    .axis {{ stroke: #8b969e; stroke-width: 1; }}
    .grid-line {{ stroke: #e2e7eb; stroke-width: 1; }}
    .bar {{ fill: #1d6f8f; }}
    .line {{ fill: none; stroke: #1d6f8f; stroke-width: 2; }}
    .line.secondary {{ stroke: #a85f00; }}
    .dot {{ fill: #1d6f8f; }}
    .dot.secondary {{ fill: #a85f00; }}
    .limit {{ stroke: #9a5b00; stroke-width: 1.5; stroke-dasharray: 5 4; }}
    .chart text {{ fill: var(--muted); font-size: 11px; }}
    .legend {{
      display: flex;
      gap: 14px;
      color: var(--muted);
      font-size: 12px;
      margin-top: 6px;
    }}
    .swatch {{
      display: inline-block;
      width: 18px;
      height: 3px;
      vertical-align: middle;
      margin-right: 5px;
      background: #1d6f8f;
    }}
    .swatch.secondary {{ background: #a85f00; }}
  </style>
</head>
<body>
  <main>
    {body}
  </main>
</body>
</html>
"""


def summary_card(title: str, items: dict[str, Any]) -> str:
    rows = "".join(f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>" for key, value in items.items())
    return f'<div class="card"><h3>{escape(title)}</h3><dl>{rows}</dl></div>'


def executive_summary_text(verdict: str, metrics: dict[str, Any] | None, decision: dict[str, Any] | None) -> str:
    if not metrics:
        return "This report is an early run record. Verification metrics are not available yet, so no calibration quality verdict can be inferred."
    decision_text = str(decision.get("decision", "pending")) if isinstance(decision, dict) else "pending"
    phase = str(metrics.get("phase", "verification"))
    avg = format_float(metrics.get("avg_de2000"))
    p95 = format_float(metrics.get("p95_de2000"))
    max_de = format_float(metrics.get("max_de2000"))
    return (
        f"Report verdict is {verdict}. Latest {phase} verification scored avg dE00 {avg}, "
        f"p95 dE00 {p95}, and max dE00 {max_de}; loop decision is {decision_text}."
    )


def executive_summary_items(
    *,
    ctx: RunContext,
    verdict: str,
    artifacts: list[ArtifactRecord],
    metrics: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    lut_integrity: dict[str, Any] | None,
    final_desktoplut_state: dict[str, Any] | None,
    final_windows_state: dict[str, Any] | None,
    probe_match: dict[str, str],
    pipeline: dict[str, str],
    loop_status: dict[str, str],
) -> dict[str, dict[str, str]]:
    final_audit = latest_stage(ctx.manifest.stages, "final_audit")
    finalization = latest_stage(ctx.manifest.stages, "finalization")
    return {
        "Outcome": {
            "Verdict": verdict,
            "Manifest": ctx.manifest.status,
            "Final Audit": str(final_audit.get("status", "missing") if final_audit else "missing"),
            "Finalized": str(finalization.get("status", "missing") if finalization else "missing"),
        },
        "Target": {
            "Mode": ctx.manifest.mode,
            "Display": ctx.manifest.display or "",
            "Metric": str(metrics.get("metric", "CIEDE2000") if metrics else "pending"),
            "Target Luminance": format_float(metrics.get("target_luminance") if metrics else None),
        },
        "Calibration Evidence": {
            "Latest Phase": str(metrics.get("phase", "pending") if metrics else "pending"),
            "Iteration": str(metrics.get("iteration", "pending") if metrics else "pending"),
            "Avg dE00": format_float(metrics.get("avg_de2000") if metrics else None),
            "P95 dE00": format_float(metrics.get("p95_de2000") if metrics else None),
            "Max dE00": format_float(metrics.get("max_de2000") if metrics else None),
            "Decision": str(decision.get("decision", "pending") if decision else "pending"),
            "LUT Integrity": str(lut_integrity.get("ok", "pending") if lut_integrity else "pending"),
            "Probe Match": probe_match.get("Status", "Not requested"),
            "Primary Toolchain": pipeline.get("Primary", "pending"),
            "Loops": loop_status.get("Overall", "pending"),
        },
        "System Evidence": {
            "DesktopLUT State": "recorded" if final_desktoplut_state else "missing",
            "Windows State": "recorded" if final_windows_state else "missing",
            "ColourSpace Required": pipeline.get("ColourSpace Required", "pending"),
            "Artifacts": str(len(artifacts)),
            "Run Folder": str(ctx.root),
        },
    }


def calibration_mode_summary(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict) or not payload:
        return {"Recorded": "No"}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"Recorded": "Yes", "OK": str(payload.get("ok", False))}
    state = result.get("state") if "state" in result else result
    if not isinstance(state, dict):
        return {"Recorded": "Yes", "OK": str(payload.get("ok", False))}
    return {
        "Active": str(state.get("active", False)),
        "Dummy ICC": str(state.get("dummy_icc_path", "")),
        "Reset": str(state.get("corrections_reset", "")),
    }


def probe_match_summary(ctx: RunContext) -> dict[str, str]:
    request = ctx.manifest.desktoplut.get("probe_match_request")
    requested = isinstance(request, dict) and request.get("enabled") is True
    correction_value = ctx.manifest.desktoplut.get("probe_match_correction")
    correction = resolve_report_path(ctx, correction_value) if isinstance(correction_value, str) else None
    correction_exists = correction is not None and correction.exists()
    raw_plan = latest_stage_path(ctx, "raw-mhc", "plan")
    raw_plan_uses_correction = False
    if raw_plan and raw_plan.exists() and isinstance(correction_value, str):
        try:
            payload = json.loads(raw_plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
        raw_plan_uses_correction = isinstance(artifacts, dict) and artifacts.get("correction") == correction_value
    if not requested:
        return {
            "Requested": "No",
            "Status": "Not requested",
            "Correction": str(correction or ""),
            "Raw Profile Uses Correction": str(raw_plan_uses_correction),
        }
    status = "Completed and used" if correction_exists and raw_plan_uses_correction else "Requested but incomplete"
    return {
        "Requested": "Yes",
        "Kind": str(request.get("kind", "")) if isinstance(request, dict) else "",
        "Display Tech": str(request.get("display_tech", "")) if isinstance(request, dict) else "",
        "Status": status,
        "Correction": str(correction or ""),
        "Raw Profile Uses Correction": str(raw_plan_uses_correction),
    }


def adaptive_drift_summary(ctx: RunContext) -> dict[str, str]:
    config = ctx.manifest.desktoplut.get("adaptive_drift")
    enabled = isinstance(config, dict) and config.get("enabled") is True
    if not enabled:
        return {"Enabled": "No"}
    stages = config.get("stages") if isinstance(config, dict) else None
    targets = ", ".join(str(stage) for stage in stages) if isinstance(stages, list) and stages else "mhc-verification, post-mhc, 3dlut-verification"
    plans = [
        entry
        for entry in ctx.manifest.stages
        if entry.get("stage") == "adaptive_drift" and entry.get("status") == "planned"
    ]
    sequences = [
        entry
        for entry in ctx.manifest.stages
        if entry.get("stage") == "patch_sequence" and entry.get("kind") == "drift"
    ]
    latest_plan = plans[-1].get("plan") if plans else ""
    latest_sequence = sequences[-1].get("sequence") if sequences else ""
    return {
        "Enabled": "Yes",
        "Targets": targets,
        "Plans": str(len(plans)),
        "Patch Sequences": str(len(sequences)),
        "Latest Plan": str(latest_plan),
        "Latest Sequence": str(latest_sequence),
    }


def pipeline_evidence_summary(ctx: RunContext) -> dict[str, str]:
    payload = ctx.manifest.desktoplut.get("pipeline_evidence")
    if not isinstance(payload, dict):
        path = latest_stage_path(ctx, "pipeline_evidence", "artifact")
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            payload = loaded if isinstance(loaded, dict) else {}
    if not isinstance(payload, dict) or not payload:
        return {"Recorded": "No", "Primary": "pending", "ColourSpace Required": "pending"}
    refs = payload.get("colourspace_stage_references")
    missing_fingerprints = payload.get("missing_tool_fingerprints")
    return {
        "Recorded": "Yes",
        "OK": str(payload.get("ok", "")),
        "Primary": str(payload.get("primary_pipeline", "")),
        "Contained Tools": str(payload.get("contained_tools_ready", "")),
        "Contained Paths": str(payload.get("contained_paths_ready", "")),
        "Tool Evidence Source": str(payload.get("tool_evidence_source", "")),
        "Tool Preflight": str(payload.get("tool_preflight_artifact", "")),
        "Tool Fingerprints Missing": str(len(missing_fingerprints) if isinstance(missing_fingerprints, list) else "unknown"),
        "ColourSpace Required": str(payload.get("colourspace_required", "")),
        "ColourSpace Stage References": str(len(refs) if isinstance(refs, list) else ""),
        "Artifact": str(payload.get("artifact", "")),
    }


def completion_proof_summary(ctx: RunContext) -> dict[str, str]:
    final_audit = latest_stage(ctx.manifest.stages, "final_audit")
    finalization = latest_stage(ctx.manifest.stages, "finalization")
    audit_payload = read_json_object(latest_stage_path(ctx, "final_audit", "artifact"))
    finalization_payload = read_json_object(latest_stage_path(ctx, "finalization", "artifact"))
    unattended_payload = read_json_object(ctx.root / "reports" / "unattended.json")
    completion = unattended_payload.get("completion_evidence") if isinstance(unattended_payload, dict) else None
    completion = completion if isinstance(completion, dict) else {}
    audit_checks = audit_payload.get("checks") if isinstance(audit_payload, dict) else []
    simulation_boundary = ""
    if isinstance(audit_checks, list):
        for check in audit_checks:
            if isinstance(check, dict) and check.get("name") == "simulation_boundary":
                simulation_boundary = f"{check.get('ok')}: {check.get('detail')}"
                break
    current_failures = finalization_payload.get("current_audit_failures") if isinstance(finalization_payload, dict) else None
    return {
        "Final Audit Stage": str(final_audit.get("status", "missing") if final_audit else "missing"),
        "Final Audit JSON OK": str(audit_payload.get("ok", "") if isinstance(audit_payload, dict) else ""),
        "Finalization Stage": str(finalization.get("status", "missing") if finalization else "missing"),
        "Finalization JSON OK": str(finalization_payload.get("ok", "") if isinstance(finalization_payload, dict) else ""),
        "Current Audit Revalidated": str(finalization_payload.get("current_audit_ok", "") if isinstance(finalization_payload, dict) else ""),
        "Current Audit Failures": str(len(current_failures) if isinstance(current_failures, list) else ""),
        "Completion Evidence OK": str(completion.get("ok", "")),
        "Completion Links Audit": str(completion.get("finalization_links_audit", "")),
        "Completion Report Exists": str(completion.get("final_report_exists", "")),
        "Simulation Boundary": simulation_boundary,
        "Finalization Artifact": str(latest_stage_path(ctx, "finalization", "artifact") or ""),
        "Unattended Artifact": str(ctx.root / "reports" / "unattended.json" if (ctx.root / "reports" / "unattended.json").exists() else ""),
    }


def automation_provenance_summary(ctx: RunContext) -> dict[str, str]:
    raw_plan = latest_stage_path(ctx, "raw-mhc", "plan")
    raw_result = latest_stage_path(ctx, "raw-mhc", "execution_result")
    post_plan = latest_stage_path(ctx, "post-mhc", "plan")
    post_result = latest_stage_path(ctx, "post-mhc", "execution_result")
    mhc_candidate = latest_stage_path(ctx, "build_mhc_baseline", "candidate")
    applied_mhc = latest_stage(ctx.manifest.stages, "apply_mhc_baseline")
    lut_plan = latest_stage_path(ctx, "build_3dlut", "plan")
    lut_result = latest_stage_path(ctx, "build_3dlut", "execution_result")
    lut_cube = latest_stage_path(ctx, "apply_3dlut", "cube") or latest_stage_path(ctx, "build_3dlut", "cube")
    preflight = latest_stage(ctx.manifest.stages, "tool_preflight")
    preflight_artifact = latest_stage_path(ctx, "tool_preflight", "artifact") or ctx.root / "preflight" / "tool_preflight.json"
    preflight_payload = read_json_object(preflight_artifact)
    contained_ready = (
        preflight_payload.get("contained_paths_ready")
        if preflight_payload
        else preflight.get("contained_paths_ready")
        if preflight
        else ""
    )
    correction = ctx.manifest.desktoplut.get("probe_match_correction")
    raw_plan_payload = read_json_object(raw_plan)
    raw_dispread_uses_correction = raw_profile_dispread_uses_correction(raw_plan_payload, correction)
    return {
        "Raw MHC Profile Lineage": profile_lineage_label(ctx, "raw-mhc", raw_plan, raw_result, ("ti1", "ti3", "icc")),
        "Post MHC Profile Lineage": profile_lineage_label(ctx, "post-mhc", post_plan, post_result, ("ti1", "ti3", "icc")),
        "MHC Candidate Lineage": mhc_candidate_label(applied_mhc, mhc_candidate),
        "3D LUT Build Lineage": lut_build_label(ctx, lut_cube, lut_plan, lut_result),
        "Probe Match Raw -X": str(raw_dispread_uses_correction),
        "Contained Tool Paths": str(contained_ready),
        "Tool Preflight Artifact": str(preflight_artifact if preflight_artifact.exists() else ""),
    }


def profile_lineage_label(
    ctx: RunContext,
    stage: str,
    plan_path: Path | None,
    result_path: Path | None,
    artifact_keys: tuple[str, ...],
) -> str:
    entry = latest_stage(ctx.manifest.stages, stage)
    if entry is None:
        return "missing"
    status = str(entry.get("status", "recorded"))
    plan_ok = plan_path is not None and plan_path.exists()
    result_ok = result_path is not None and result_path.exists()
    artifacts = entry.get("artifacts")
    existing = 0
    if isinstance(artifacts, dict):
        for key in artifact_keys:
            path = resolve_report_path(ctx, artifacts.get(key)) if isinstance(artifacts.get(key), str) else None
            if path and path.exists():
                existing += 1
    return f"{status}; plan={plan_ok}; execution={result_ok}; artifacts={existing}/{len(artifact_keys)}"


def mhc_candidate_label(applied_stage: dict[str, Any] | None, candidate_path: Path | None) -> str:
    applied = applied_stage is not None and applied_stage.get("status") == "applied"
    candidate_ok = candidate_path is not None and candidate_path.exists()
    return f"applied={applied}; candidate={candidate_ok}"


def lut_build_label(ctx: RunContext, cube_path: Path | None, plan_path: Path | None, result_path: Path | None) -> str:
    build = latest_stage(ctx.manifest.stages, "build_3dlut")
    applied = latest_stage(ctx.manifest.stages, "apply_3dlut")
    status = str(build.get("status", "missing") if build else "missing")
    applied_ok = applied is not None and applied.get("status") == "applied"
    cube_ok = cube_path is not None and cube_path.exists()
    plan_ok = plan_path is not None and plan_path.exists()
    result_ok = result_path is not None and result_path.exists()
    return f"{status}; applied={applied_ok}; cube={cube_ok}; plan={plan_ok}; execution={result_ok}"


def raw_profile_dispread_uses_correction(plan: dict[str, Any] | None, correction: Any) -> bool:
    if not isinstance(plan, dict) or not isinstance(correction, str):
        return False
    command_argv = plan.get("command_argv")
    dispread = command_argv[1] if isinstance(command_argv, list) and len(command_argv) > 1 else None
    return isinstance(dispread, list) and "-X" in [str(item) for item in dispread] and correction in [str(item) for item in dispread]


def quality_policy_summary(ctx: RunContext) -> dict[str, str]:
    payload = ctx.manifest.desktoplut.get("quality_policy")
    if not isinstance(payload, dict) or not payload:
        return {"Recorded": "No", "MHC": "defaults", "3D LUT": "defaults"}
    default = payload.get("default") if isinstance(payload.get("default"), dict) else {}
    mhc = payload.get("mhc") if isinstance(payload.get("mhc"), dict) else {}
    lut3d = payload.get("3dlut") if isinstance(payload.get("3dlut"), dict) else {}
    return {
        "Recorded": "Yes",
        "Default": policy_summary(default),
        "MHC": policy_summary(mhc or default),
        "3D LUT": policy_summary(lut3d or default),
    }


def policy_summary(policy: dict[str, Any]) -> str:
    if not policy:
        return "defaults"
    parts = []
    for label, key in [("avg", "avg_de2000"), ("p95", "p95_de2000"), ("max", "max_de2000"), ("white", "white_de2000"), ("iters", "max_iterations")]:
        if key in policy:
            parts.append(f"{label}={policy[key]}")
    return ", ".join(parts) if parts else "custom"


def loop_status_summary(ctx: RunContext) -> dict[str, str]:
    payload = ctx.manifest.desktoplut.get("loop_status")
    if not isinstance(payload, dict):
        path = ctx.root / "reports" / "loop_status.json"
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            payload = loaded if isinstance(loaded, dict) else {}
    if not isinstance(payload, dict) or not payload:
        return {"Recorded": "No", "Overall": "pending"}
    phases = payload.get("phases")
    mhc = phases.get("mhc") if isinstance(phases, dict) else {}
    lut3d = phases.get("3dlut") if isinstance(phases, dict) else {}
    return {
        "Recorded": "Yes",
        "Overall": "stopped" if payload.get("ok") is True else "active",
        "MHC": str(mhc.get("status", "")) if isinstance(mhc, dict) else "",
        "MHC Iteration": str(mhc.get("latest_iteration", "")) if isinstance(mhc, dict) else "",
        "MHC Reason": str(mhc.get("reason", "")) if isinstance(mhc, dict) else "",
        "3D LUT": str(lut3d.get("status", "")) if isinstance(lut3d, dict) else "",
        "3D LUT Iteration": str(lut3d.get("latest_iteration", "")) if isinstance(lut3d, dict) else "",
        "3D LUT Reason": str(lut3d.get("reason", "")) if isinstance(lut3d, dict) else "",
    }


def latest_desktoplut_state_capture(desktoplut: dict[str, Any]) -> dict[str, Any] | None:
    captures = desktoplut.get("state_captures")
    if not isinstance(captures, dict):
        return None
    final = captures.get("final")
    return final if isinstance(final, dict) else None


def desktoplut_state_summary(capture: dict[str, Any] | None) -> dict[str, str]:
    if not capture:
        return {"Recorded": "No"}
    response = capture.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return {"Recorded": "Yes", "OK": str(capture.get("ok", "")), "Artifact": str(capture.get("artifact", ""))}
    mhc = result.get("mhc")
    runtime = result.get("runtime")
    calibration_mode = result.get("calibration_mode")
    return {
        "Recorded": "Yes",
        "OK": str(capture.get("ok", "")),
        "Running": str(result.get("running", "")),
        "MHC Entries": str(len(mhc) if isinstance(mhc, dict) else ""),
        "Runtime Entries": str(len(runtime) if isinstance(runtime, dict) else ""),
        "Calibration Active": str(bool(calibration_mode)),
        "Artifact": str(capture.get("artifact", "")),
    }


def latest_windows_color_state_capture(desktoplut: dict[str, Any]) -> dict[str, Any] | None:
    captures = desktoplut.get("windows_state_captures")
    if not isinstance(captures, dict):
        return None
    final = captures.get("final")
    return final if isinstance(final, dict) else None


def windows_color_state_summary(capture: dict[str, Any] | None) -> dict[str, str]:
    if not capture:
        return {"Recorded": "No"}
    profiles = capture.get("profiles")
    gamma_ramp = capture.get("gamma_ramp")
    profiles_result = profiles.get("result") if isinstance(profiles, dict) else None
    gamma_result = gamma_ramp.get("result") if isinstance(gamma_ramp, dict) else None
    active_profile = ""
    profile_count = ""
    if isinstance(profiles_result, dict):
        active_profile = str(profiles_result.get("active_profile", ""))
        raw_profiles = profiles_result.get("profiles")
        profile_count = str(len(raw_profiles) if isinstance(raw_profiles, list) else "")
    return {
        "Recorded": "Yes",
        "OK": str(capture.get("ok", "")),
        "Profiles OK": str(profiles.get("ok", "") if isinstance(profiles, dict) else ""),
        "Gamma OK": str(gamma_ramp.get("ok", "") if isinstance(gamma_ramp, dict) else ""),
        "Active ICC": active_profile,
        "Profiles": profile_count,
        "Gamma Loaded": str(gamma_result.get("gamma_ramp_loaded", "") if isinstance(gamma_result, dict) else ""),
        "Artifact": str(capture.get("artifact", "")),
    }


def mhc_summary(candidate: dict[str, Any] | None, applied: dict[str, Any] | None, last_apply: Any, last_smoke: Any) -> dict[str, str]:
    return {
        "Candidate": "Yes" if candidate else "No",
        "Applied": "Yes" if applied and applied.get("status") == "applied" else "No",
        "Last Apply OK": str(last_apply.get("ok", "")) if isinstance(last_apply, dict) else "",
        "Mock Smoke OK": str(last_smoke.get("ok", "")) if isinstance(last_smoke, dict) else "",
    }


def latest_metrics(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stage in reversed(stages):
        metrics_path = stage.get("metrics")
        if isinstance(metrics_path, str) and Path(metrics_path).exists():
            try:
                return json.loads(Path(metrics_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"Path": metrics_path, "Error": "could not read metrics"}
    return None


def load_patch_metrics(metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not metrics:
        return []
    patches_path = metrics.get("patches_path")
    if not isinstance(patches_path, str):
        return []
    path = Path(patches_path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def metric_charts(metrics: dict[str, Any] | None, patch_metrics: list[dict[str, Any]]) -> str:
    if not metrics or not patch_metrics:
        return '<div class="empty">Patch metrics are not available yet.</div>'
    return (
        '<div class="chart-grid">'
        + chart_card("Grayscale dE00", grayscale_de_chart(patch_metrics, limit=float(metrics.get("p95_de2000", 3.0) or 3.0)))
        + chart_card("RGB Balance", rgb_balance_chart(patch_metrics))
        + chart_card("Gamma / EOTF", gamma_eotf_chart(patch_metrics))
        + chart_card("CIE xy Gamut", gamut_xy_chart(patch_metrics))
        + chart_card("dE00 Histogram", de_histogram_chart(patch_metrics))
        + "</div>"
    )


def chart_card(title: str, svg: str) -> str:
    return f'<div class="chart"><h3>{escape(title)}</h3>{svg}</div>'


def iteration_history_section(
    metrics: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    integrity: list[dict[str, Any]],
) -> str:
    if not metrics and not decisions and not integrity:
        return '<div class="empty">No calibration loop iterations have been scored yet.</div>'
    return (
        '<div class="chart-grid">'
        + chart_card("MHC Iterations", iteration_trend_chart(metrics, "mhc"))
        + chart_card("3D LUT Iterations", iteration_trend_chart(metrics, "3dlut"))
        + "</div>"
        + iteration_history_table(metrics, decisions, integrity)
    )


def metric_history_entries(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    seen: set[tuple[str, int]] = set()
    for stage in stages:
        if not str(stage.get("stage", "")).endswith("_metrics"):
            continue
        payload = load_stage_json(stage, "metrics")
        if not payload:
            continue
        phase = str(payload.get("phase", "") or str(stage.get("stage", "")).removesuffix("_metrics"))
        iteration = int(payload.get("iteration", stage.get("iteration", 0)) or 0)
        key = (phase, iteration)
        if key in seen:
            continue
        seen.add(key)
        entries.append(payload | {"phase": phase, "iteration": iteration})
    return sorted(entries, key=lambda item: (str(item.get("phase", "")), int(item.get("iteration", 0) or 0)))


def decision_history_entries(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    seen: set[tuple[str, int]] = set()
    for stage in stages:
        if not str(stage.get("stage", "")).endswith("_decision"):
            continue
        payload = load_stage_json(stage, "decision")
        if not payload:
            payload = {
                "phase": str(stage.get("stage", "")).removesuffix("_decision"),
                "iteration": stage.get("iteration", 0),
                "decision": stage.get("status", ""),
                "reason": stage.get("reason", ""),
            }
        phase = str(payload.get("phase", "") or str(stage.get("stage", "")).removesuffix("_decision"))
        iteration = int(payload.get("iteration", stage.get("iteration", 0)) or 0)
        key = (phase, iteration)
        if key in seen:
            continue
        seen.add(key)
        entries.append(payload | {"phase": phase, "iteration": iteration})
    return sorted(entries, key=lambda item: (str(item.get("phase", "")), int(item.get("iteration", 0) or 0)))


def lut_integrity_history_entries(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    seen: set[int] = set()
    for stage in stages:
        if stage.get("stage") != "3dlut_lut_integrity":
            continue
        payload = load_stage_json(stage, "integrity")
        if not payload:
            continue
        iteration = int(payload.get("iteration", stage.get("iteration", 0)) or 0)
        if iteration in seen:
            continue
        seen.add(iteration)
        entries.append(payload | {"phase": "3dlut", "iteration": iteration})
    return sorted(entries, key=lambda item: int(item.get("iteration", 0) or 0))


def load_stage_json(stage: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = stage.get(key)
    if not isinstance(value, str):
        return None
    path = Path(value)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def iteration_history_table(
    metrics: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    integrity: list[dict[str, Any]],
) -> str:
    decision_by_key = {(str(item.get("phase", "")), int(item.get("iteration", 0) or 0)): item for item in decisions}
    integrity_by_iter = {int(item.get("iteration", 0) or 0): item for item in integrity}
    metric_keys = {(str(item.get("phase", "")), int(item.get("iteration", 0) or 0)) for item in metrics}
    decision_keys = set(decision_by_key)
    keys = sorted(metric_keys | decision_keys, key=lambda item: (item[0], item[1]))
    if not keys:
        return '<div class="empty">No scored iterations are available.</div>'

    metrics_by_key = {(str(item.get("phase", "")), int(item.get("iteration", 0) or 0)): item for item in metrics}
    rows = []
    for phase, iteration in keys:
        metric = metrics_by_key.get((phase, iteration), {})
        decision = decision_by_key.get((phase, iteration), {})
        integrity_row = integrity_by_iter.get(iteration, {}) if phase == "3dlut" else {}
        rows.append(
            "<tr>"
            f"<td>{escape(phase)}</td>"
            f"<td>{iteration}</td>"
            f"<td>{format_float(metric.get('avg_de2000'))}</td>"
            f"<td>{format_float(metric.get('p95_de2000'))}</td>"
            f"<td>{format_float(metric.get('max_de2000'))}</td>"
            f"<td>{format_float(metric.get('white_de2000'))}</td>"
            f"<td>{escape(str(decision.get('decision', '')))}</td>"
            f"<td>{escape(str(decision.get('reason', '')))}</td>"
            f"<td>{escape(str(integrity_row.get('ok', '')))}</td>"
            f"<td>{format_float(integrity_row.get('max_neighbor_delta'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Phase</th><th>Iter</th><th>Avg dE00</th><th>P95 dE00</th>"
        "<th>Max dE00</th><th>White dE00</th><th>Decision</th><th>Reason</th>"
        "<th>LUT OK</th><th>Max LUT Step</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def iteration_trend_chart(metrics: list[dict[str, Any]], phase: str) -> str:
    points = [
        (
            int(item.get("iteration", 0) or 0),
            float(item.get("avg_de2000", 0.0) or 0.0),
            float(item.get("p95_de2000", 0.0) or 0.0),
        )
        for item in metrics
        if item.get("phase") == phase and isinstance(item.get("avg_de2000"), (int, float))
    ]
    if not points:
        return f'<div class="empty">No {escape(phase)} iteration metrics yet.</div>'
    points.sort(key=lambda item: item[0])
    width, height = 520, 220
    left, right, top, bottom = 42, 14, 16, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    iterations = [iteration for iteration, _, _ in points]
    min_iter, max_iter = min(iterations), max(iterations)
    span = max(1, max_iter - min_iter)
    max_de = max(max(avg, p95) for _, avg, p95 in points)
    max_de = max(max_de, 1.0)

    def x_for(iteration: int) -> float:
        return left + ((iteration - min_iter) / span) * plot_w

    def y_for(value: float) -> float:
        return top + plot_h - ((value / max_de) * plot_h)

    avg_points = " ".join(f"{x_for(iteration):.1f},{y_for(avg):.1f}" for iteration, avg, _ in points)
    p95_points = " ".join(f"{x_for(iteration):.1f},{y_for(p95):.1f}" for iteration, _, p95 in points)
    avg_dots = "".join(f'<circle class="dot" cx="{x_for(iteration):.1f}" cy="{y_for(avg):.1f}" r="3" />' for iteration, avg, _ in points)
    p95_dots = "".join(
        f'<circle class="dot secondary" cx="{x_for(iteration):.1f}" cy="{y_for(p95):.1f}" r="3" />'
        for iteration, _, p95 in points
    )
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(phase)} iteration dE00 trend">
      <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" />
      <line class="axis" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{left}" y1="{y_for(max_de / 2):.1f}" x2="{left + plot_w}" y2="{y_for(max_de / 2):.1f}" />
      <polyline class="line" points="{avg_points}" />
      <polyline class="line secondary" points="{p95_points}" />
      {avg_dots}
      {p95_dots}
      <text x="4" y="{y_for(max_de):.1f}">{max_de:.1f}</text>
      <text x="{left}" y="{height - 8}">iter {min_iter}</text>
      <text x="{left + plot_w - 42}" y="{height - 8}">iter {max_iter}</text>
    </svg>
    <div class="legend"><span><span class="swatch"></span>Avg dE00</span><span><span class="swatch secondary"></span>P95 dE00</span></div>
    """


def grayscale_de_chart(patch_metrics: list[dict[str, Any]], *, limit: float) -> str:
    points = []
    for metric in patch_metrics:
        if not metric.get("grayscale"):
            continue
        rgb = metric.get("rgb")
        de = metric.get("de2000")
        if not isinstance(rgb, list) or not rgb or not isinstance(de, (int, float)):
            continue
        points.append((float(rgb[0]), float(de)))
    if not points:
        return '<div class="empty">No grayscale patch metrics.</div>'
    points.sort(key=lambda item: item[0])
    width, height = 520, 220
    left, right, top, bottom = 42, 14, 16, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_de = max(max(de for _, de in points), limit, 1.0)

    def x_for(level: float) -> float:
        return left + (level * plot_w)

    def y_for(de: float) -> float:
        return top + plot_h - ((de / max_de) * plot_h)

    polyline = " ".join(f"{x_for(level):.1f},{y_for(de):.1f}" for level, de in points)
    dots = "".join(f'<circle class="dot" cx="{x_for(level):.1f}" cy="{y_for(de):.1f}" r="3" />' for level, de in points)
    limit_y = y_for(limit)
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="Grayscale dE00 chart">
      <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" />
      <line class="axis" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{left}" y1="{y_for(max_de / 2):.1f}" x2="{left + plot_w}" y2="{y_for(max_de / 2):.1f}" />
      <line class="limit" x1="{left}" y1="{limit_y:.1f}" x2="{left + plot_w}" y2="{limit_y:.1f}" />
      <polyline class="line" points="{polyline}" />
      {dots}
      <text x="4" y="{y_for(max_de):.1f}">{max_de:.1f}</text>
      <text x="4" y="{limit_y:.1f}">limit {limit:.1f}</text>
      <text x="{left}" y="{height - 8}">black</text>
      <text x="{left + plot_w - 28}" y="{height - 8}">white</text>
    </svg>
    """


XYZ_TO_SRGB_D65 = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)


def xyz_to_linear_srgb(xyz: list[float] | tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
    return tuple(max(0.0, row[0] * x + row[1] * y + row[2] * z) for row in XYZ_TO_SRGB_D65)  # type: ignore[return-value]


def xyz_to_xy(xyz: list[float] | tuple[float, float, float]) -> tuple[float, float] | None:
    x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
    total = x + y + z
    if total <= 0:
        return None
    return (x / total, y / total)


def grayscale_patch_points(patch_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for metric in patch_metrics:
        if not metric.get("grayscale"):
            continue
        rgb = metric.get("rgb")
        measured = metric.get("measured_xyz")
        target = metric.get("target_xyz")
        if not isinstance(rgb, list) or not rgb or not isinstance(measured, list) or not isinstance(target, list):
            continue
        if len(measured) < 3 or len(target) < 3:
            continue
        points.append({"level": float(rgb[0]), "measured_xyz": measured, "target_xyz": target})
    return sorted(points, key=lambda item: item["level"])


def rgb_balance_chart(patch_metrics: list[dict[str, Any]]) -> str:
    gray = [point for point in grayscale_patch_points(patch_metrics) if point["level"] > 0.0]
    if not gray:
        return '<div class="empty">No grayscale RGB balance data.</div>'
    balance_points = []
    for point in gray:
        linear = xyz_to_linear_srgb(point["measured_xyz"])
        mean = sum(linear) / 3
        if mean <= 0:
            continue
        balance_points.append((point["level"], linear[0] / mean * 100, linear[1] / mean * 100, linear[2] / mean * 100))
    if not balance_points:
        return '<div class="empty">No usable grayscale RGB balance data.</div>'

    width, height = 520, 220
    left, right, top, bottom = 42, 14, 16, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [value for _, r, g, b in balance_points for value in (r, g, b)]
    low = min(80.0, min(values) - 2)
    high = max(120.0, max(values) + 2)
    span = max(1.0, high - low)

    def x_for(level: float) -> float:
        return left + level * plot_w

    def y_for(value: float) -> float:
        return top + plot_h - ((value - low) / span) * plot_h

    def line(channel: int) -> str:
        return " ".join(f"{x_for(point[0]):.1f},{y_for(point[channel]):.1f}" for point in balance_points)

    neutral_y = y_for(100.0)
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="RGB balance chart">
      <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" />
      <line class="axis" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{left}" y1="{neutral_y:.1f}" x2="{left + plot_w}" y2="{neutral_y:.1f}" />
      <polyline fill="none" stroke="#b3261e" stroke-width="2" points="{line(1)}" />
      <polyline fill="none" stroke="#1f7a3a" stroke-width="2" points="{line(2)}" />
      <polyline fill="none" stroke="#275db3" stroke-width="2" points="{line(3)}" />
      <text x="4" y="{y_for(high):.1f}">{high:.0f}%</text>
      <text x="4" y="{neutral_y:.1f}">100%</text>
      <text x="{left}" y="{height - 8}">black</text>
      <text x="{left + plot_w - 28}" y="{height - 8}">white</text>
    </svg>
    <div class="legend"><span><span class="swatch" style="background:#b3261e"></span>R</span><span><span class="swatch" style="background:#1f7a3a"></span>G</span><span><span class="swatch" style="background:#275db3"></span>B</span></div>
    """


def gamma_eotf_chart(patch_metrics: list[dict[str, Any]]) -> str:
    gray = grayscale_patch_points(patch_metrics)
    if not gray:
        return '<div class="empty">No grayscale EOTF data.</div>'
    measured_white = max(float(point["measured_xyz"][1]) for point in gray) or 1.0
    target_white = max(float(point["target_xyz"][1]) for point in gray) or 1.0
    points = [
        (
            point["level"],
            max(0.0, min(1.25, float(point["measured_xyz"][1]) / measured_white)),
            max(0.0, min(1.25, float(point["target_xyz"][1]) / target_white)),
        )
        for point in gray
    ]
    width, height = 520, 220
    left, right, top, bottom = 42, 14, 16, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_y = max(1.0, max(max(measured, target) for _, measured, target in points))

    def x_for(level: float) -> float:
        return left + level * plot_w

    def y_for(value: float) -> float:
        return top + plot_h - (value / max_y) * plot_h

    measured_line = " ".join(f"{x_for(level):.1f},{y_for(measured):.1f}" for level, measured, _ in points)
    target_line = " ".join(f"{x_for(level):.1f},{y_for(target):.1f}" for level, _, target in points)
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="Gamma EOTF chart">
      <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" />
      <line class="axis" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{left}" y1="{y_for(0.5):.1f}" x2="{left + plot_w}" y2="{y_for(0.5):.1f}" />
      <polyline class="line" points="{measured_line}" />
      <polyline class="line secondary" points="{target_line}" />
      <text x="4" y="{y_for(max_y):.1f}">{max_y:.2f}</text>
      <text x="4" y="{y_for(0.5):.1f}">0.50</text>
      <text x="{left}" y="{height - 8}">black</text>
      <text x="{left + plot_w - 28}" y="{height - 8}">white</text>
    </svg>
    <div class="legend"><span><span class="swatch"></span>Measured Y</span><span><span class="swatch secondary"></span>Target Y</span></div>
    """


def gamut_xy_chart(patch_metrics: list[dict[str, Any]]) -> str:
    points = []
    target_points = []
    for metric in patch_metrics:
        rgb = metric.get("rgb")
        measured = metric.get("measured_xyz")
        target = metric.get("target_xyz")
        if not isinstance(rgb, list) or len(rgb) < 3 or not isinstance(measured, list) or not isinstance(target, list):
            continue
        if len(measured) < 3 or len(target) < 3:
            continue
        if max(float(channel) for channel in rgb) < 0.95:
            continue
        measured_xy = xyz_to_xy(measured)
        target_xy = xyz_to_xy(target)
        if measured_xy and target_xy:
            points.append((tuple(float(channel) for channel in rgb[:3]), measured_xy))
            target_points.append((tuple(float(channel) for channel in rgb[:3]), target_xy))
    if not points:
        return '<div class="empty">No gamut patch metrics.</div>'

    width, height = 520, 260
    left, right, top, bottom = 42, 18, 18, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    x_min, x_max = 0.0, 0.75
    y_min, y_max = 0.0, 0.85

    def x_for(x: float) -> float:
        return left + ((x - x_min) / (x_max - x_min)) * plot_w

    def y_for(y: float) -> float:
        return top + plot_h - ((y - y_min) / (y_max - y_min)) * plot_h

    def label_for(rgb: tuple[float, float, float]) -> str:
        names = []
        if rgb[0] >= 0.95:
            names.append("R")
        if rgb[1] >= 0.95:
            names.append("G")
        if rgb[2] >= 0.95:
            names.append("B")
        return "".join(names) or "P"

    primary_targets = []
    for rgb, xy in target_points:
        if rgb in {(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)}:
            primary_targets.append((rgb, xy))
    order = {(1.0, 0.0, 0.0): 0, (0.0, 1.0, 0.0): 1, (0.0, 0.0, 1.0): 2}
    primary_targets.sort(key=lambda item: order.get(item[0], 99))
    polygon = ""
    if len(primary_targets) == 3:
        polygon_points = " ".join(f"{x_for(xy[0]):.1f},{y_for(xy[1]):.1f}" for _, xy in primary_targets)
        polygon = f'<polygon points="{polygon_points}" fill="rgba(29,111,143,0.08)" stroke="#1d6f8f" stroke-width="1.5" />'

    measured_dots = []
    target_dots = []
    for rgb, xy in points:
        label = label_for(rgb)
        measured_dots.append(
            f'<circle cx="{x_for(xy[0]):.1f}" cy="{y_for(xy[1]):.1f}" r="3.4" fill="#a85f00"><title>{escape(label)} measured x={xy[0]:.4f} y={xy[1]:.4f}</title></circle>'
        )
    for rgb, xy in target_points:
        label = label_for(rgb)
        target_dots.append(
            f'<circle cx="{x_for(xy[0]):.1f}" cy="{y_for(xy[1]):.1f}" r="2.5" fill="#1d6f8f"><title>{escape(label)} target x={xy[0]:.4f} y={xy[1]:.4f}</title></circle>'
        )

    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="CIE xy gamut chart">
      <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" />
      <line class="axis" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{x_for(0.25):.1f}" y1="{top}" x2="{x_for(0.25):.1f}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{x_for(0.50):.1f}" y1="{top}" x2="{x_for(0.50):.1f}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{left}" y1="{y_for(0.30):.1f}" x2="{left + plot_w}" y2="{y_for(0.30):.1f}" />
      <line class="grid-line" x1="{left}" y1="{y_for(0.60):.1f}" x2="{left + plot_w}" y2="{y_for(0.60):.1f}" />
      {polygon}
      {''.join(target_dots)}
      {''.join(measured_dots)}
      <text x="{left}" y="{height - 8}">x 0.00</text>
      <text x="{left + plot_w - 38}" y="{height - 8}">0.75</text>
      <text x="4" y="{top + 4}">y 0.85</text>
    </svg>
    <div class="legend"><span><span class="swatch"></span>Target</span><span><span class="swatch secondary"></span>Measured</span></div>
    """


def de_histogram_chart(patch_metrics: list[dict[str, Any]]) -> str:
    values = [float(metric["de2000"]) for metric in patch_metrics if isinstance(metric.get("de2000"), (int, float))]
    if not values:
        return '<div class="empty">No dE00 patch metrics.</div>'
    width, height = 520, 220
    left, right, top, bottom = 42, 14, 16, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_value = max(max(values), 1.0)
    bins = 8
    counts = [0 for _ in range(bins)]
    for value in values:
        index = min(bins - 1, int((value / max_value) * bins))
        counts[index] += 1
    max_count = max(counts) or 1
    bar_gap = 4
    bar_w = (plot_w - (bar_gap * (bins - 1))) / bins
    bars = []
    for index, count in enumerate(counts):
        bar_h = (count / max_count) * plot_h
        x = left + index * (bar_w + bar_gap)
        y = top + plot_h - bar_h
        bars.append(f'<rect class="bar" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" />')
    return f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="dE00 histogram">
      <line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" />
      <line class="axis" x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" />
      <line class="grid-line" x1="{left}" y1="{top + plot_h / 2:.1f}" x2="{left + plot_w}" y2="{top + plot_h / 2:.1f}" />
      {''.join(bars)}
      <text x="4" y="{top + 4}">{max_count}</text>
      <text x="{left}" y="{height - 8}">0</text>
      <text x="{left + plot_w - 54}" y="{height - 8}">max {max_value:.1f}</text>
    </svg>
    """


def latest_decision(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stage in reversed(stages):
        decision_path = stage.get("decision")
        if isinstance(decision_path, str) and Path(decision_path).exists():
            try:
                return json.loads(Path(decision_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"Path": decision_path, "Error": "could not read decision"}
    return None


def latest_lut_integrity(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stage in reversed(stages):
        integrity_path = stage.get("integrity")
        if isinstance(integrity_path, str) and Path(integrity_path).exists():
            try:
                return json.loads(Path(integrity_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"Path": integrity_path, "Error": "could not read LUT integrity"}
    return None


def metrics_summary(metrics: dict[str, Any] | None) -> dict[str, str]:
    if not metrics:
        return {"Recorded": "No"}
    return {
        "Phase": str(metrics.get("phase", "")),
        "Iteration": str(metrics.get("iteration", "")),
        "Avg dE00": format_float(metrics.get("avg_de2000")),
        "P95 dE00": format_float(metrics.get("p95_de2000")),
        "Max dE00": format_float(metrics.get("max_de2000")),
        "White dE00": format_float(metrics.get("white_de2000")),
    }


def lut_integrity_summary(summary: dict[str, Any] | None) -> dict[str, str]:
    if not summary:
        return {"Recorded": "No"}
    return {
        "OK": str(summary.get("ok", "")),
        "Size": str(summary.get("size", "")),
        "Entries": f"{summary.get('actual_entries', '')}/{summary.get('expected_entries', '')}",
        "Bounds": str(summary.get("out_of_bounds_count", "")),
        "Monotonic": str(summary.get("monotonicity_violations", "")),
        "Max Step": format_float(summary.get("max_neighbor_delta")),
    }


def decision_summary(decision: dict[str, Any] | None) -> dict[str, str]:
    if not decision:
        return {"Recorded": "No"}
    return {
        "Phase": str(decision.get("phase", "")),
        "Iteration": str(decision.get("iteration", "")),
        "Decision": str(decision.get("decision", "")),
        "Reason": str(decision.get("reason", "")),
    }


def format_float(value: Any) -> str:
    return f"{value:.3f}" if isinstance(value, (float, int)) else str(value or "")


def read_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def latest_stage(stages: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for stage in reversed(stages):
        if stage.get("stage") == name:
            return stage
    return None


def resolve_report_path(ctx: RunContext, value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    candidate = ctx.root / path
    return candidate if candidate.exists() else path


def latest_stage_path(ctx: RunContext, stage_name: str, key: str) -> Path | None:
    for stage in reversed(ctx.manifest.stages):
        if stage.get("stage") != stage_name:
            continue
        value = stage.get(key)
        if isinstance(value, str):
            return resolve_report_path(ctx, value)
        artifacts = stage.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get(key), str):
            return resolve_report_path(ctx, str(artifacts[key]))
    return None


def stage_table(stages: list[dict[str, Any]]) -> str:
    if not stages:
        return '<div class="empty">No stages recorded yet.</div>'
    rows = []
    for index, stage in enumerate(stages, start=1):
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{escape(str(stage.get('stage', '')))}</td>"
            f"<td>{escape(str(stage.get('iteration', '')))}</td>"
            f"<td>{escape(str(stage.get('status', '')))}</td>"
            f"<td><code>{escape(stage_reference(stage))}</code></td>"
            "</tr>"
        )
    return "<table><thead><tr><th>#</th><th>Stage</th><th>Iter</th><th>Status</th><th>Reference</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def stage_reference(stage: dict[str, Any]) -> str:
    for key in ["candidate", "plan", "artifact", "execution_result", "decision", "integrity"]:
        value = stage.get(key)
        if value:
            return str(value)
    artifacts = stage.get("artifacts")
    if isinstance(artifacts, dict):
        return ", ".join(str(value) for value in artifacts.values())
    return ""


def human_action_table(actions: dict[str, Any]) -> str:
    if not actions:
        return '<div class="empty">No human actions recorded.</div>'
    rows = []
    for name, action in actions.items():
        details = action.get("details", {}) if isinstance(action, dict) else {}
        rows.append(
            "<tr>"
            f"<td>{escape(name)}</td>"
            f"<td>{escape(str(action.get('status', '') if isinstance(action, dict) else ''))}</td>"
            f"<td>{escape(str(action.get('time', '') if isinstance(action, dict) else ''))}</td>"
            f"<td><code>{escape(json.dumps(details, sort_keys=True))}</code></td>"
            "</tr>"
        )
    return "<table><thead><tr><th>Action</th><th>Status</th><th>Time</th><th>Details</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def event_table(events: list[Event]) -> str:
    if not events:
        return '<div class="empty">No events recorded.</div>'
    rows = []
    for event in events:
        rows.append(
            "<tr>"
            f"<td>{escape(event.time)}</td>"
            f"<td>{escape(event.level)}</td>"
            f"<td>{escape(event.stage)}</td>"
            f"<td>{escape(event.event)}</td>"
            f"<td><code>{escape(json.dumps(event.data, sort_keys=True))}</code></td>"
            "</tr>"
        )
    return "<table><thead><tr><th>Time</th><th>Level</th><th>Stage</th><th>Event</th><th>Data</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def artifact_table(artifacts: list[ArtifactRecord]) -> str:
    if not artifacts:
        return '<div class="empty">No artifacts recorded.</div>'
    rows = []
    for artifact in artifacts:
        rows.append(
            "<tr>"
            f"<td><code>{escape(artifact.path)}</code></td>"
            f"<td>{escape(artifact.role)}</td>"
            f"<td>{artifact.size_bytes}</td>"
            f"<td><code>{escape(artifact.sha256)}</code></td>"
            "</tr>"
        )
    return "<table><thead><tr><th>Path</th><th>Role</th><th>Bytes</th><th>SHA-256</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def escape(value: str) -> str:
    return html.escape(value, quote=True)

