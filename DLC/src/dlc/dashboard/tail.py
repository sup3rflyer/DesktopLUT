"""Follow the spine — a robust, restart-safe tail of ``events.jsonl``.

This is the dashboard's only reader. It handles the live-file realities the naive
"read the whole file" path can't: the file not existing yet (the dash started before the
run), a half-written final line during an append, and a *run switch* — when
``runs/active.json`` repoints to a new run's spine, the tail resets and signals the hub
to start a fresh state. It never raises on a transient I/O hiccup; a missed poll just
catches up on the next one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..events import Event, event_from_dict


def read_active_pointer(runs_dir: Path) -> Optional[Path]:
    """Resolve the active run's spine from ``runs/active.json`` (producer-written).

    Returns the ``events.jsonl`` path, or ``None`` if there's no pointer yet / it's
    unreadable — the caller keeps showing whatever it had.
    """
    pointer = runs_dir / "active.json"
    try:
        raw = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    events = raw.get("events")
    if events:
        return Path(events)
    run = raw.get("run")
    return Path(run) / "events.jsonl" if run else None


class EventTail:
    """Incrementally yield new :class:`Event`s appended to the spine.

    Two modes: a **fixed** ``path`` (``--run`` was given) or **follow** mode (given a
    ``runs_dir``, it tracks ``active.json`` so the dash moves to the next run on its own).
    Call :meth:`poll` repeatedly; it returns the events appended since the last call and
    a ``switched`` flag set when the underlying file changed (so the hub resets state).
    """

    def __init__(self, *, path: Optional[Path] = None, runs_dir: Optional[Path] = None) -> None:
        if (path is None) == (runs_dir is None):
            raise ValueError("EventTail needs exactly one of path= or runs_dir=")
        self._fixed = Path(path) if path is not None else None
        self._runs_dir = Path(runs_dir) if runs_dir is not None else None
        self._current: Optional[Path] = self._fixed
        self._offset = 0
        self._buf = b""

    @property
    def current(self) -> Optional[Path]:
        return self._current

    def _resolve_target(self) -> Optional[Path]:
        if self._fixed is not None:
            return self._fixed
        target = read_active_pointer(self._runs_dir)  # type: ignore[arg-type]
        # Keep the current target if the pointer momentarily vanishes/garbles — don't
        # blank the dashboard on a transient read of active.json mid-rewrite.
        return target if target is not None else self._current

    def poll(self) -> tuple[list[Event], bool]:
        target = self._resolve_target()
        switched = False
        if target != self._current:
            self._current = target
            self._offset = 0
            self._buf = b""
            switched = True
        return self._read_new(), switched

    def _read_new(self) -> list[Event]:
        path = self._current
        if path is None:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []  # not created yet, or a transient stat failure
        if size < self._offset:
            # Truncated or replaced under us — restart from the top.
            self._offset = 0
            self._buf = b""
        if size == self._offset:
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []

        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")  # trailing piece is a partial line
        events: list[Event] = []
        for raw_line in lines:
            text = raw_line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue  # torn line — skip, the next poll re-reads cleanly
            events.append(event_from_dict(obj))
        return events
