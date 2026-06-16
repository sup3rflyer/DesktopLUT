"""StageResult: the rich, LLM-readable object every harness tool emits.

The whole point of DLC is that an LLM assistant arbitrates calibration quality.
Tools are *hands*, not brains: they measure, build, install, and then report what
happened in a structured shape the assistant reads at the start and end of every
stage. A tool NEVER decides "good enough" — it surfaces evidence (`metrics`,
`deltas`, `anomalies`) and at most an advisory verdict (`advice`) the assistant is
free to override.

See docs/v1-rebuild-plan.md section 1 for the contract.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Status = Literal["ran", "failed", "blocked"]
Severity = Literal["low", "medium", "high"]


@dataclass
class Anomaly:
    code: str
    detail: str
    severity: Severity = "medium"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass
class Artifact:
    path: str
    sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass
class StageResult:
    """One stage's eyes-on-glass report for the arbitrating assistant."""

    stage: str
    status: Status = "ran"
    preconditions: dict[str, Any] = field(default_factory=dict)
    actions_taken: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    deltas: dict[str, Any] = field(default_factory=dict)
    anomalies: list[Anomaly] = field(default_factory=list)
    advice: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    # Free-form, human/LLM-oriented one-liners the tool wants to surface.
    notes: list[str] = field(default_factory=list)

    # -- builders ----------------------------------------------------------
    def action(self, message: str) -> "StageResult":
        self.actions_taken.append(message)
        return self

    def note(self, message: str) -> "StageResult":
        self.notes.append(message)
        return self

    def anomaly(self, code: str, detail: str, severity: Severity = "medium") -> "StageResult":
        self.anomalies.append(Anomaly(code, detail, severity))
        return self

    def add_artifact(self, path: str | Path, *, hash_it: bool = True) -> "StageResult":
        p = Path(path)
        digest: str | None = None
        if hash_it and p.is_file():
            digest = sha256_file(p)
        self.artifacts.append(Artifact(str(p), digest))
        return self

    def block(self, code: str, detail: str) -> "StageResult":
        """Mark the stage blocked by a hard invariant (a precondition gate)."""
        self.status = "blocked"
        return self.anomaly(code, detail, "high")

    def fail(self, code: str, detail: str) -> "StageResult":
        self.status = "failed"
        return self.anomaly(code, detail, "high")

    # -- serialization -----------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "preconditions": self.preconditions,
            "actions_taken": self.actions_taken,
            "raw": self.raw,
            "metrics": self.metrics,
            "deltas": self.deltas,
            "anomalies": [a.as_dict() for a in self.anomalies],
            "advice": self.advice,
            "artifacts": [a.as_dict() for a in self.artifacts],
            "notes": self.notes,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent)

    def emit(self) -> str:
        """Print the JSON to stdout for the assistant-invoked CLI and return it."""
        text = self.to_json()
        print(text)
        return text

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
