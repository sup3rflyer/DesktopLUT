"""Tests for the run liveness watcher (dlc.liveness): the checkpoint stall guard
and the watchdog backstop."""

from __future__ import annotations

import time

import pytest

from dlc.events import Ev, RunLog, read_events
from dlc.liveness import Liveness, RunStalled


class _Clock:
    """A hand-cranked monotonic clock so the checkpoint guard is deterministic."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# --------------------------------------------------------------------------
# Checkpoint guard (thread-free, deterministic)
# --------------------------------------------------------------------------
def test_check_does_not_raise_while_progressing(tmp_path):
    clk = _Clock()
    live = Liveness(RunLog(tmp_path / "e.jsonl"), stall_after_s=100.0, clock=clk)
    for _ in range(10):
        clk.advance(50.0)        # 50s < threshold since last progress
        live.check("measure")
        live.progress("measure")  # each step resets the clock
    # no stall emitted
    assert not [e for e in read_events(tmp_path / "e.jsonl") if e.event == Ev.STALL]


def test_check_raises_runstalled_after_threshold(tmp_path):
    clk = _Clock()
    live = Liveness(RunLog(tmp_path / "e.jsonl"), stall_after_s=100.0, clock=clk)
    live.progress("measure")
    clk.advance(101.0)            # past the threshold with no progress
    with pytest.raises(RunStalled) as ei:
        live.check("measure")
    assert ei.value.stage == "measure"
    assert ei.value.since_progress_s >= 100.0
    # exactly one stall event, digest tier, with the via tag
    stalls = [e for e in read_events(tmp_path / "e.jsonl") if e.event == Ev.STALL]
    assert len(stalls) == 1
    assert stalls[0].data["via"] == "checkpoint"
    assert stalls[0].effective_tier == "digest"


def test_failed_read_activity_does_not_reset_the_clock(tmp_path):
    # A failed-read storm (activity but no progress) must still trip the guard.
    clk = _Clock()
    live = Liveness(RunLog(tmp_path / "e.jsonl"), stall_after_s=100.0, clock=clk)
    live.progress("measure")
    for _ in range(5):
        clk.advance(30.0)
        live.activity("measure")   # attempts, never progress
    # 150s of only-activity elapsed → stalled
    with pytest.raises(RunStalled):
        live.check("measure")


def test_stall_emitted_once_even_across_multiple_checks(tmp_path):
    clk = _Clock()
    live = Liveness(RunLog(tmp_path / "e.jsonl"), stall_after_s=10.0, clock=clk)
    live.progress("measure")
    clk.advance(20.0)
    for _ in range(3):
        with pytest.raises(RunStalled):
            live.check("measure")
    stalls = [e for e in read_events(tmp_path / "e.jsonl") if e.event == Ev.STALL]
    assert len(stalls) == 1


# --------------------------------------------------------------------------
# Watchdog backstop (real-time, short threshold)
# --------------------------------------------------------------------------
def test_watchdog_fires_on_stall_and_kills(tmp_path):
    """The main thread is 'wedged' (never calls check); the watchdog must emit a
    stall, run the kill hook, and then the next check() aborts."""
    killed = []
    live = Liveness(
        RunLog(tmp_path / "e.jsonl"),
        stall_after_s=0.1,            # watchdog fires at 2x = 0.2s
        heartbeat_every_s=0.1,
        on_stall=lambda: killed.append(True),
    )
    live.progress("build")
    live.start()
    try:
        deadline = time.monotonic() + 3.0
        while not killed and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        live.stop()

    assert killed, "watchdog should have run the kill hook"
    events = read_events(tmp_path / "e.jsonl")
    stalls = [e for e in events if e.event == Ev.STALL]
    assert stalls and stalls[0].data["via"] == "watchdog"
    assert any(e.event == Ev.HEARTBEAT for e in events)
    # after the watchdog tripped, the main thread's checkpoint must abort
    with pytest.raises(RunStalled):
        live.check("build")


def test_watchdog_quiet_when_progressing(tmp_path):
    live = Liveness(RunLog(tmp_path / "e.jsonl"), stall_after_s=0.2, heartbeat_every_s=0.1)
    live.start()
    try:
        for _ in range(8):
            live.progress("measure")   # keep resetting; never stall
            time.sleep(0.05)
    finally:
        live.stop()
    assert not [e for e in read_events(tmp_path / "e.jsonl") if e.event == Ev.STALL]
