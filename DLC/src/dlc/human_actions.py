"""Human action acknowledgements for safe unattended stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .events import EventWriter
from .runs import RunContext


@dataclass(frozen=True)
class HumanActionRecord:
    action: str
    status: str
    time: str
    details: dict[str, Any]


def acknowledge_human_action(ctx: RunContext, action: str, **details: Any) -> HumanActionRecord:
    record = HumanActionRecord(
        action=action,
        status="acknowledged",
        time=datetime.now().isoformat(timespec="seconds"),
        details=details,
    )
    ctx.manifest.human_actions[action] = asdict(record)
    ctx.save()
    ctx.log(f"Human action acknowledged: {action}")
    EventWriter(ctx.events_path).write("INFO", "human_action", action, **asdict(record))
    return record


def has_human_action(ctx: RunContext, action: str) -> bool:
    record = ctx.manifest.human_actions.get(action)
    return isinstance(record, dict) and record.get("status") == "acknowledged"

