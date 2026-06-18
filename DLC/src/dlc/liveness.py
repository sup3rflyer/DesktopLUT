"""Run liveness — the heartbeat + self-acting stall guard (v2-design §12).

A run must never silently grind: the 53-minute build-probe wedge happened because
nothing watched for *lack of progress*. Two complementary watchers share one
``time.monotonic()`` "last progress" clock:

* **Checkpoint guard** (:meth:`Liveness.check`) — the producers call it at every safe
  point (each read, each optimizer iteration). If there has been no PROGRESS for
  ``stall_after_s`` it emits a ``stall`` and raises :class:`RunStalled`, which the
  orchestrator turns into a clean abort + rollback. Thread-free and fully testable;
  it catches the common "loop is alive but not advancing" stall.
* **Watchdog thread** (:meth:`Liveness.start` / :meth:`Liveness.stop`, optional) — the
  backstop for the case a checkpoint can NEVER be reached because the main thread is
  blocked in a syscall (a wedged dogegen ``show()``, a ConPTY read that ignores its
  own timeout). It emits periodic heartbeats (so the dashboard shows liveness even
  *during* a wedge) and, past a deliberately looser threshold, force-kills the wedged
  meter/presenter via ``on_stall`` so the blocked main thread returns and then hits
  its own checkpoint.

The key distinction: **progress** (an accepted/good read, a completed iteration) resets
the clock; **activity** (any read attempt, even a failure) does NOT — so a failed-read
storm still trips the guard. All timing is monotonic so a wall-clock step can't skew it.
Dependency-free (stdlib + the spine only)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .events import RunLog


class RunStalled(Exception):
    """No progress for longer than the stall threshold. Carries the stage and the
    elapsed since-progress for the abort digest / the dashboard."""

    def __init__(self, stage: str, since_progress_s: float, threshold_s: float) -> None:
        self.stage = stage
        self.since_progress_s = since_progress_s
        self.threshold_s = threshold_s
        super().__init__(
            f"no measurement progress for {since_progress_s:.0f}s "
            f"(stall threshold {threshold_s:.0f}s) during {stage}")


class RunCancelled(Exception):
    """A cooperative cancel was requested (the LLM/operator wrote ``control.json``).
    Raised at the next checkpoint so the run aborts cleanly + rolls back — the
    actionable half of mid-run gating (the LLM can *stop* a run it's watching, not
    just watch it). Distinct from :class:`RunStalled` (a stall is involuntary)."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"run cancelled at a checkpoint during {stage}")


class Liveness:
    """Shared progress clock + the two stall watchers. One instance per run; the
    orchestrator owns it, the measure loop / optimizer call into it."""

    def __init__(
        self,
        runlog: RunLog,
        *,
        stall_after_s: float = 600.0,
        heartbeat_every_s: float = 15.0,
        watchdog_factor: float = 2.0,
        on_stall: Optional[Callable[[], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runlog = runlog
        self.stall_after_s = max(1.0, float(stall_after_s))
        self.heartbeat_every_s = max(1.0, float(heartbeat_every_s))
        self.watchdog_factor = max(1.0, float(watchdog_factor))
        self.on_stall = on_stall
        # Polled by the watchdog thread (NOT per-read — no syscall storm): a True result
        # latches a cancel that the next check() turns into a clean RunCancelled abort.
        self.cancel_check = cancel_check
        self._clock = clock

        self._lock = threading.Lock()
        now = clock()
        self._last_progress = now
        self._start = now
        self._stage = "run"
        self._tripped = False          # the watchdog fired; the next check() must abort
        self._stall_emitted = False    # emit the stall event exactly once
        self._cancel_requested = False # control.json asked to cancel; next check() aborts
        self._cancel_emitted = False   # emit the cancel note exactly once
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- producer-side calls ----------------------------------------------
    def set_stall_after(self, seconds: float) -> None:
        """Tune the threshold once the panel/meter timing is known (DIP-derived)."""
        with self._lock:
            self.stall_after_s = max(1.0, float(seconds))

    def progress(self, stage: str) -> None:
        """A real step happened (a good read / a completed iteration / a soak block).
        Resets the stall clock. Does NOT emit — the producers own the visible
        ``progress`` counter; this is purely the stall clock."""
        with self._lock:
            self._last_progress = self._clock()
            self._stage = stage

    def activity(self, stage: str) -> None:
        """A read was *attempted* (even if it failed). Updates the phase shown in a
        heartbeat/stall but deliberately does NOT reset the clock — so a storm of
        failed reads (the failed (0,0,0) case) still trips the guard."""
        with self._lock:
            self._stage = stage

    def check(self, stage: str) -> None:
        """Checkpoint guard (main thread). Honours a cooperative cancel first, then
        emits a ``stall`` (once) and raises :class:`RunStalled` if progress has stalled
        — or if the watchdog already tripped while the main thread was wedged. Cheap;
        call it liberally."""
        with self._lock:
            self._stage = stage
            cancelled = self._cancel_requested
            emit_cancel = cancelled and not self._cancel_emitted
            if emit_cancel:
                self._cancel_emitted = True
            since = self._clock() - self._last_progress
            thr = self.stall_after_s
            tripped = self._tripped
            should_raise = tripped or since > thr
            emit = (not cancelled) and should_raise and not self._stall_emitted
            if emit:
                self._stall_emitted = True
        if emit_cancel:
            self.runlog.note(stage, "run cancelled (control.json) — aborting at checkpoint",
                             level="WARN", cancelled=True)
        if cancelled:
            raise RunCancelled(stage)
        if emit:
            self.runlog.stall(stage, since_progress_s=round(since, 1),
                              threshold_s=round(thr, 1), via="checkpoint")
        if should_raise:
            raise RunStalled(stage, since, thr)

    def request_cancel(self) -> None:
        """Latch a cancel (so the next :meth:`check` aborts). Used directly by tests
        and as the watchdog's action when ``cancel_check`` fires."""
        with self._lock:
            self._cancel_requested = True

    # -- watchdog thread --------------------------------------------------
    def start(self) -> None:
        """Start the backstop thread (idempotent). No-op-safe: if it never trips it
        just emits heartbeats. Daemon, so it never blocks process exit."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="liveness-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    def __enter__(self) -> "Liveness":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def _watch(self) -> None:
        tick = max(0.05, min(self.heartbeat_every_s, 5.0))
        last_beat = self._clock()
        while not self._stop.wait(tick):
            now = self._clock()
            # Cooperative cancel: poll the control channel off the main thread (so it works
            # even while the main thread is wedged in a syscall) and latch it — the next
            # main-thread check() turns it into a clean RunCancelled abort.
            if self.cancel_check is not None:
                try:
                    if self.cancel_check():
                        with self._lock:
                            self._cancel_requested = True
                except Exception:  # noqa: BLE001 - a bad control read must never kill the watcher
                    pass
            with self._lock:
                since = now - self._last_progress
                stage = self._stage
                stall_after = self.stall_after_s
                wd_threshold = self.watchdog_factor * self.stall_after_s
                started = self._start
            if now - last_beat >= self.heartbeat_every_s:
                last_beat = now
                # Heartbeat is the liveness signal even when the MAIN thread is wedged in a
                # syscall (the checkpoint can't fire) — the one beat that proves the wedge.
                # since_progress_s + stall_after_s let the dashboard warn (amber) as progress
                # age approaches the guard's threshold, before the stall itself fires.
                self.runlog.heartbeat(stage, since_progress_s=round(since, 1),
                                      stall_after_s=round(stall_after, 1),
                                      elapsed_s=round(now - started, 1))
            if since > wd_threshold:
                with self._lock:
                    first = not self._stall_emitted
                    self._stall_emitted = True
                    self._tripped = True       # the next main-thread check() will abort
                if first:
                    self.runlog.stall(stage, since_progress_s=round(since, 1),
                                      threshold_s=round(wd_threshold, 1), via="watchdog")
                    if self.on_stall is not None:
                        try:
                            # Force-kill the wedged meter/presenter so the blocked main
                            # thread returns and reaches its checkpoint (which then aborts).
                            self.on_stall()
                        except Exception:
                            pass
