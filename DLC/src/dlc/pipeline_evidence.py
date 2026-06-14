"""Evidence that a run used the scriptable DLC/Argyll pipeline."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import EventWriter
from .runs import RunContext
from .tools import REQUIRED_TOOL_NAMES, ToolSet


@dataclass(frozen=True)
class PipelineEvidence:
    ok: bool
    run: str
    generated_at: str
    primary_pipeline: str
    instrument_layer: str
    profile_layer: str
    lut_layer: str
    colourspace_required: bool
    colourspace_policy: str
    contained_tools_ready: bool
    contained_paths_ready: bool
    contained_path_root: str | None
    contained_path_issues: list[dict[str, Any]]
    missing_required_tools: list[str]
    missing_contained_tools: list[str]
    missing_tool_fingerprints: list[str]
    colourspace_stage_references: list[str]
    tools: dict[str, dict[str, Any]]
    tool_evidence_source: str
    tool_preflight_artifact: str | None
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tool_dict(tools: ToolSet) -> dict[str, dict[str, Any]]:
    return tools.as_evidence()


def tool_evidence_from_tools(tools: ToolSet) -> dict[str, dict[str, Any]]:
    return _tool_dict(tools)


def _manifest_tool_evidence(ctx: RunContext) -> dict[str, dict[str, Any]] | None:
    payload = ctx.manifest.desktoplut.get("tool_evidence")
    if not isinstance(payload, dict):
        return None
    records: dict[str, dict[str, Any]] = {}
    for name, raw in payload.items():
        if isinstance(name, str) and isinstance(raw, dict):
            records[name] = dict(raw)
    return records or None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _latest_tool_preflight_path(ctx: RunContext) -> Path:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") == "tool_preflight" and isinstance(entry.get("artifact"), str):
            path = Path(str(entry["artifact"]))
            return path if path.is_absolute() else ctx.root / path
    return ctx.root / "preflight" / "tool_preflight.json"


def _tool_preflight_evidence(ctx: RunContext) -> tuple[dict[str, dict[str, Any]] | None, Path | None, dict[str, Any] | None]:
    path = _latest_tool_preflight_path(ctx)
    payload = _read_json(path)
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, dict):
        return None, path if path.exists() else None, payload
    records: dict[str, dict[str, Any]] = {}
    for name, raw in tools.items():
        if isinstance(name, str) and isinstance(raw, dict):
            records[name] = dict(raw)
    return records or None, path, payload


def _missing_required(tool_records: dict[str, dict[str, Any]]) -> list[str]:
    return [name for name in REQUIRED_TOOL_NAMES if not bool(tool_records.get(name, {}).get("ok"))]


def _missing_contained(tool_records: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name, record in tool_records.items()
        if not bool(record.get("ok")) or not bool(record.get("contained"))
    ]


def _missing_tool_fingerprints(tool_records: dict[str, dict[str, Any]]) -> list[str]:
    return [
        name
        for name, record in tool_records.items()
        if bool(record.get("ok")) and (not isinstance(record.get("sha256"), str) or len(str(record.get("sha256"))) != 64)
    ]


def _mentions_colourspace(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return "colourspace" in lowered or "colorspace" in lowered
    if isinstance(value, dict):
        return any(_mentions_colourspace(item) for item in value.values())
    if isinstance(value, list):
        return any(_mentions_colourspace(item) for item in value)
    return False


def _colourspace_stage_references(ctx: RunContext) -> list[str]:
    references = []
    for entry in ctx.manifest.stages:
        if _mentions_colourspace(entry):
            stage = str(entry.get("stage", "unknown"))
            status = str(entry.get("status", ""))
            references.append(f"{stage}:{status}" if status else stage)
    return references


def build_pipeline_evidence(
    *,
    ctx: RunContext,
    tools: ToolSet,
    output: Path | None = None,
) -> PipelineEvidence:
    preflight_records, preflight_path, preflight_payload = _tool_preflight_evidence(ctx)
    manifest_records = _manifest_tool_evidence(ctx)
    if preflight_records is not None:
        tool_records = preflight_records
        tool_evidence_source = "tool_preflight"
    elif manifest_records is not None:
        tool_records = manifest_records
        tool_evidence_source = "manifest"
    else:
        tool_records = _tool_dict(tools)
        tool_evidence_source = "current_discovery"
    missing_required = _missing_required(tool_records)
    missing_contained = _missing_contained(tool_records)
    missing_fingerprints = _missing_tool_fingerprints(tool_records)
    if isinstance(preflight_payload, dict):
        contained_paths_ready = preflight_payload.get("contained_paths_ready") is True
        contained_path_root = preflight_payload.get("contained_path_root") if isinstance(preflight_payload.get("contained_path_root"), str) else None
        contained_path_issues = preflight_payload.get("contained_path_issues")
        contained_path_issues = contained_path_issues if isinstance(contained_path_issues, list) else []
    else:
        contained_paths_ready = not missing_contained
        contained_path_root = None
        contained_path_issues = []
    colourspace_refs = _colourspace_stage_references(ctx)
    target = output or ctx.root / "reports" / "pipeline_evidence.json"
    ok = not missing_required and not missing_contained and contained_paths_ready and not missing_fingerprints and not colourspace_refs
    return PipelineEvidence(
        ok=ok,
        run=str(ctx.root),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        primary_pipeline="DesktopLUT Calibrator + ArgyllCMS command-line tooling",
        instrument_layer="ArgyllCMS spotread/ccxxmake/dispread",
        profile_layer="ArgyllCMS targen/dispread/colprof",
        lut_layer="ArgyllCMS collink -3c plus DesktopLUT runtime application",
        colourspace_required=False,
        colourspace_policy="ColourSpace is legacy comparison/adapter-only and is not required for the primary automated path.",
        contained_tools_ready=not missing_contained,
        contained_paths_ready=contained_paths_ready,
        contained_path_root=contained_path_root,
        contained_path_issues=contained_path_issues,
        missing_required_tools=missing_required,
        missing_contained_tools=missing_contained,
        missing_tool_fingerprints=missing_fingerprints,
        colourspace_stage_references=colourspace_refs,
        tools=tool_records,
        tool_evidence_source=tool_evidence_source,
        tool_preflight_artifact=str(preflight_path) if preflight_path else None,
        artifact=str(target),
    )


def write_pipeline_evidence(
    *,
    ctx: RunContext,
    tools: ToolSet,
    output: Path | None = None,
) -> PipelineEvidence:
    result = build_pipeline_evidence(ctx=ctx, tools=tools, output=output)
    target = Path(result.artifact)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.desktoplut["pipeline_evidence"] = result.as_dict()
    ctx.manifest.stages.append(
        {
            "stage": "pipeline_evidence",
            "status": "passed" if result.ok else "failed",
            "artifact": str(target),
            "contained_tools_ready": result.contained_tools_ready,
            "contained_paths_ready": result.contained_paths_ready,
            "contained_path_issues": result.contained_path_issues,
            "tool_evidence_source": result.tool_evidence_source,
            "tool_preflight_artifact": result.tool_preflight_artifact,
            "colourspace_required": result.colourspace_required,
            "colourspace_stage_references": len(result.colourspace_stage_references),
        }
    )
    ctx.save()
    ctx.log(f"Pipeline evidence {'passed' if result.ok else 'failed'}: {target}")
    EventWriter(ctx.events_path).write(
        "INFO" if result.ok else "WARNING",
        "pipeline_evidence",
        "pipeline_evidence_written",
        ok=result.ok,
        artifact=str(target),
        contained_tools_ready=result.contained_tools_ready,
        contained_paths_ready=result.contained_paths_ready,
        colourspace_stage_references=len(result.colourspace_stage_references),
    )
    return result

