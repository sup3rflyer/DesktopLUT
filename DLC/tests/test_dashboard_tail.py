"""The dashboard's robust spine tailer: incremental, partial-line- and switch-safe."""

from __future__ import annotations

import json
from pathlib import Path

from dlc.events import EventWriter
from dlc.dashboard.tail import EventTail, read_active_pointer


def _events_file(tmp_path: Path, name: str = "events.jsonl") -> Path:
    return tmp_path / name


def test_incremental_tail(tmp_path):
    path = _events_file(tmp_path)
    w = EventWriter(path)
    w.write("INFO", "run", "run_header", target="srgb")
    w.write("INFO", "measure", "stage_start")

    tail = EventTail(path=path)
    events, switched = tail.poll()
    assert not switched
    assert [e.event for e in events] == ["run_header", "stage_start"]

    # Nothing new → empty.
    assert tail.poll() == ([], False)

    w.write("INFO", "measure", "patch_read", seq=1)
    events, _ = tail.poll()
    assert [e.event for e in events] == ["patch_read"]


def test_partial_line_is_held_until_complete(tmp_path):
    path = _events_file(tmp_path)
    # A complete line followed by a half-written one (mid-append).
    with path.open("wb") as fh:
        fh.write(b'{"event":"a","level":"INFO","stage":"s"}\n')
        fh.write(b'{"event":"b","level":"INFO"')  # no newline yet
    tail = EventTail(path=path)
    events, _ = tail.poll()
    assert [e.event for e in events] == ["a"]   # the torn line is held back

    # Finish the partial line; the next poll completes it.
    with path.open("ab") as fh:
        fh.write(b',"stage":"s"}\n')
    events, _ = tail.poll()
    assert [e.event for e in events] == ["b"]


def test_truncation_restarts_from_top(tmp_path):
    path = _events_file(tmp_path)
    w = EventWriter(path)
    w.write("INFO", "s", "old")
    tail = EventTail(path=path)
    tail.poll()
    # Replace the file with shorter content (size < offset → restart).
    path.write_text(json.dumps({"event": "fresh", "level": "INFO", "stage": "s"}) + "\n",
                    encoding="utf-8")
    events, _ = tail.poll()
    assert [e.event for e in events] == ["fresh"]


def test_missing_file_is_patient(tmp_path):
    tail = EventTail(path=tmp_path / "not_yet.jsonl")
    assert tail.poll() == ([], False)   # no crash before the run creates it


def test_follow_active_pointer_and_switch_runs(tmp_path):
    runs = tmp_path
    run_a = runs / "a"; run_a.mkdir()
    run_b = runs / "b"; run_b.mkdir()
    EventWriter(run_a / "events.jsonl").write("INFO", "s", "a-header")
    EventWriter(run_b / "events.jsonl").write("INFO", "s", "b-header")

    (runs / "active.json").write_text(
        json.dumps({"run": str(run_a), "events": str(run_a / "events.jsonl")}), encoding="utf-8")
    assert read_active_pointer(runs) == run_a / "events.jsonl"

    tail = EventTail(runs_dir=runs)
    events, switched = tail.poll()
    assert switched is True                      # first resolve adopts run A
    assert [e.event for e in events] == ["a-header"]

    # Repoint to run B → the tail switches and reads B from the top.
    (runs / "active.json").write_text(
        json.dumps({"run": str(run_b), "events": str(run_b / "events.jsonl")}), encoding="utf-8")
    events, switched = tail.poll()
    assert switched is True
    assert [e.event for e in events] == ["b-header"]


def test_follow_keeps_current_when_pointer_vanishes(tmp_path):
    runs = tmp_path
    run_a = runs / "a"; run_a.mkdir()
    EventWriter(run_a / "events.jsonl").write("INFO", "s", "a")
    (runs / "active.json").write_text(
        json.dumps({"events": str(run_a / "events.jsonl")}), encoding="utf-8")
    tail = EventTail(runs_dir=runs)
    tail.poll()
    (runs / "active.json").unlink()              # pointer gone mid-run
    events, switched = tail.poll()
    assert switched is False                      # don't blank the dashboard
    assert tail.current == run_a / "events.jsonl"
