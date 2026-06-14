"""Artifact indexing for calibration runs."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    role: str
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


ROLE_BY_SUFFIX = {
    ".json": "json",
    ".jsonl": "events",
    ".log": "log",
    ".txt": "text",
    ".ti1": "argyll_target",
    ".ti3": "argyll_measurement",
    ".icc": "icc_profile",
    ".icm": "icc_profile",
    ".ccmx": "colorimeter_correction_matrix",
    ".ccss": "colorimeter_correction_spectral_sample",
    ".cube": "lut_cube",
    ".sp": "spectrum",
    ".csv": "table",
    ".html": "html_report",
}


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_role(path: Path) -> str:
    if path.name == "manifest.json":
        return "manifest"
    if path.name == "events.jsonl":
        return "events"
    if path.name.endswith("_adaptive_drift_plan.json"):
        return "adaptive_drift_plan"
    if path.name.endswith("_patch_sequence.json"):
        return "patch_sequence"
    if path.name.endswith("_ccmx_plan.json") or path.name.endswith("_ccss_plan.json"):
        return "probe_match_plan"
    if path.name.endswith("_decision.json"):
        return "decision_record"
    if path.name.startswith("supervise_") and path.name.endswith(".json"):
        return "supervision_record"
    if path.name == "final_audit.json":
        return "final_audit"
    if path.name == "finalization.json":
        return "finalization_record"
    if path.name == "readiness.json":
        return "readiness_audit"
    if path.name == "tool_preflight.json":
        return "tool_preflight"
    if path.name == "monitor.json":
        return "run_health"
    if path.name == "unattended.json":
        return "unattended_record"
    if path.name == "self_test.json":
        return "self_test_record"
    if path.name == "live_setup.json":
        return "live_setup"
    if path.name == "pipeline_evidence.json":
        return "pipeline_evidence"
    if path.name == "loop_status.json":
        return "loop_status"
    if path.name == "loop_rehearsal.json":
        return "loop_rehearsal"
    if path.name == "agent_handoff.json":
        return "agent_handoff"
    if path.name == "dashboard.html":
        return "status_dashboard"
    if path.name == "readout.html":
        return "operator_readout"
    if path.name.startswith("desktoplut_state_") and path.name.endswith(".json"):
        return "desktoplut_state"
    if path.name.startswith("windows_color_state_") and path.name.endswith(".json"):
        return "windows_color_state"
    if path.name.startswith("windows_local_audit_") and path.name.endswith(".json"):
        return "windows_local_audit"
    if path.name.startswith("desktoplut_contract_") and path.name.endswith(".json"):
        return "desktoplut_contract_check"
    if path.name.endswith("_lut_integrity.json"):
        return "lut_integrity"
    if path.name.endswith("_metrics.json") or path.name.endswith("_patch_metrics.json"):
        return "metrics"
    return ROLE_BY_SUFFIX.get(path.suffix.lower(), "artifact")


def scan_artifacts(run_dir: Path) -> list[ArtifactRecord]:
    records: list[ArtifactRecord] = []
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        if path.name == ".gitkeep":
            continue
        relative = path.relative_to(run_dir)
        records.append(
            ArtifactRecord(
                path=str(relative),
                role=artifact_role(path),
                size_bytes=path.stat().st_size,
                sha256=hash_file(path),
            )
        )
    return records

