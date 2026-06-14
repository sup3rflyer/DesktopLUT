"""Machine-readable event stream for agent supervision."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Event:
    level: str
    stage: str
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    time: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class EventWriter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, level: str, stage: str, event: str, **data: Any) -> None:
        record = Event(level=level, stage=stage, event=event, data=data)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), separators=(",", ":")) + "\n")


def read_events(path: Path) -> list[Event]:
    events: list[Event] = []
    if not path.exists():
        return events
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            events.append(Event(**raw))
    return events


