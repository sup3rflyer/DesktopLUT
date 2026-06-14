"""Accept an audited calibration run as finalized."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import EventWriter
from .final_audit import build_final_audit
from .runs import RunContext


@dataclass(frozen=True)
class FinalizeResult:
    ok: bool
    run: str
    finalized_at: str
    artifact: str
    audit_artifact: str | None
    final_report: str | None
    reason: str
    current_audit_ok: bool
    current_audit_failures: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def latest_passed_audit(ctx: RunContext) -> str | None:
    for entry in reversed(ctx.manifest.stages):
        if entry.get("stage") == "final_audit" and entry.get("status") == "passed":
            artifact = entry.get("artifact")
            path = Path(artifact) if isinstance(artifact, str) else None
            if path is not None and not path.is_absolute():
                path = ctx.root / path
            if path is not None and _audit_json_ok(path):
                return artifact
    return None


def _audit_json_ok(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def finalize_run(ctx: RunContext, *, output: Path | None = None) -> FinalizeResult:
    audit_artifact = latest_passed_audit(ctx)
    current_audit = build_final_audit(ctx)
    current_audit_failures = [check.name for check in current_audit.checks if not check.ok]
    output = output or ctx.root / "reports" / "finalization.json"
    finalized_at = datetime.now().isoformat(timespec="seconds")
    ok = audit_artifact is not None and current_audit.ok
    if audit_artifact is None:
        reason = "passing final audit JSON is required before finalization"
    elif not current_audit.ok:
        reason = "current run state no longer passes final audit: " + ", ".join(current_audit_failures)
    else:
        reason = "passing final audit accepted and current run state revalidated"
    result = FinalizeResult(
        ok=ok,
        run=str(ctx.root),
        finalized_at=finalized_at,
        artifact=str(output),
        audit_artifact=audit_artifact,
        final_report=str(ctx.root / "reports" / "calibration_report.html") if ok else None,
        reason=reason,
        current_audit_ok=current_audit.ok,
        current_audit_failures=current_audit_failures,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    ctx.manifest.stages.append(
        {
            "stage": "finalization",
            "status": "finalized" if ok else "failed",
            "artifact": str(output),
            "audit_artifact": audit_artifact,
            "current_audit_ok": current_audit.ok,
            "current_audit_failures": current_audit_failures,
        }
    )
    if ok:
        ctx.manifest.status = "finalized"
    ctx.save()
    if ok:
        from .reports import write_report_html

        write_report_html(ctx)
    ctx.log(f"Run finalization {'accepted' if ok else 'failed'}: {output}")
    EventWriter(ctx.events_path).write(
        "INFO" if ok else "ERROR",
        "finalization",
        "run_finalized",
        ok=ok,
        artifact=str(output),
        audit_artifact=audit_artifact,
    )
    return result

