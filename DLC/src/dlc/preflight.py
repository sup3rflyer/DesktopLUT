"""Preflight helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import EventWriter
from .paths import THIRD_PARTY_DIR
from .profiles import default_dummy_icc
from .runs import RunContext
from .tools import ToolSet
from .vendor import vendor_manifest_status


@dataclass(frozen=True)
class PreflightFinding:
    severity: str
    message: str


def evaluate_tools(tools: ToolSet) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for name, tool in tools.__dict__.items():
        if tool.ok and tool.contained:
            findings.append(PreflightFinding("OK", f"{name}: {tool.path}"))
        elif tool.ok:
            findings.append(PreflightFinding("WARN", f"{name}: {tool.path} ({tool.note})"))
        else:
            findings.append(PreflightFinding("FAIL", f"{name}: {tool.note}"))
    return findings


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _third_party_root(path: Path) -> Path | None:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for candidate in [resolved, *resolved.parents]:
        if candidate.name.lower() == "third_party":
            return candidate
    return None


def contained_path_evidence(tools: ToolSet, *, vendor_status: dict[str, Any] | None = None) -> dict[str, Any]:
    vendor_root = vendor_status.get("third_party_dir") if isinstance(vendor_status, dict) else None
    roots = []
    records = tools.as_evidence()
    for record in records.values():
        if record.get("ok") is True and record.get("contained") is True and isinstance(record.get("path"), str):
            root = _third_party_root(Path(str(record["path"])))
            if root is not None:
                roots.append(root)
    unique_roots = sorted({str(root.resolve()).lower(): root for root in roots}.values(), key=str)
    root = Path(str(vendor_root)) if isinstance(vendor_root, str) else (unique_roots[0] if len(unique_roots) == 1 else THIRD_PARTY_DIR)

    issues = []
    if len(unique_roots) > 1:
        issues.append({"name": "multiple", "path": None, "reason": "contained tools resolve under multiple third_party roots"})
    for name, record in records.items():
        if record.get("ok") is not True or record.get("contained") is not True:
            continue
        path_value = record.get("path")
        if not isinstance(path_value, str):
            issues.append({"name": name, "path": None, "reason": "contained tool path is missing"})
            continue
        path = Path(path_value)
        if _third_party_root(path) is None:
            issues.append({"name": name, "path": path_value, "reason": "contained tool path is not under a third_party directory"})
        elif not _is_relative_to(path, root):
            issues.append({"name": name, "path": path_value, "reason": "contained tool path is outside the expected third_party root"})
    return {
        "contained_path_root": str(root),
        "contained_paths_ready": not issues,
        "contained_path_issues": issues,
    }


def build_tool_preflight_payload(
    tools: ToolSet,
    *,
    artifact: Path,
    vendor_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dummy_sdr = default_dummy_icc("SDR")
    dummy_hdr = default_dummy_icc("HDR")
    vendor_manifest = vendor_status if vendor_status is not None else vendor_manifest_status()
    path_evidence = contained_path_evidence(tools, vendor_status=vendor_manifest)
    return {
        "artifact": str(artifact),
        "contained_ready": not tools.missing_contained(),
        **path_evidence,
        "required_ready": not tools.missing_required(),
        "missing_required": tools.missing_required(),
        "missing_contained": tools.missing_contained(),
        "vendor_manifest_ready": vendor_manifest.get("ok") is True,
        "vendor_manifest": vendor_manifest,
        "profiles": {
            "dummy_icc_sdr": dummy_sdr.as_dict(),
            "dummy_icc_hdr": dummy_hdr.as_dict(),
        },
        "tools": tools.as_evidence(),
    }


def write_tool_preflight(
    tools: ToolSet,
    output: Path,
    *,
    vendor_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_tool_preflight_payload(tools, artifact=output, vendor_status=vendor_status)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def record_tool_preflight_stage(ctx: RunContext, payload: dict[str, Any]) -> None:
    artifact = str(payload.get("artifact", ctx.root / "preflight" / "tool_preflight.json"))
    required_ready = payload.get("required_ready") is True
    contained_ready = payload.get("contained_ready") is True
    contained_paths_ready = payload.get("contained_paths_ready") is True
    ctx.manifest.stages.append(
        {
            "stage": "tool_preflight",
            "status": "passed" if required_ready and contained_ready and contained_paths_ready else "blocked",
            "artifact": artifact,
            "required_ready": required_ready,
            "contained_ready": contained_ready,
            "contained_paths_ready": contained_paths_ready,
            "contained_path_root": payload.get("contained_path_root"),
            "contained_path_issues": payload.get("contained_path_issues", []),
            "vendor_manifest_ready": payload.get("vendor_manifest_ready") is True,
            "missing_required": payload.get("missing_required", []),
            "missing_contained": payload.get("missing_contained", []),
        }
    )
    ctx.save()
    ctx.log(f"Tool preflight written: {artifact}")
    EventWriter(ctx.events_path).write(
        "INFO" if required_ready else "WARNING",
        "preflight",
        "tool_preflight_written",
        artifact=artifact,
        required_ready=required_ready,
        contained_ready=contained_ready,
        contained_paths_ready=contained_paths_ready,
    )

