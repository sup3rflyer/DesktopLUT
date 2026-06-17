"""ArgyllCMS wrappers."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence


@dataclass(frozen=True)
class Instrument:
    port: int
    description: str


@dataclass(frozen=True)
class SpotreadRequest:
    port: int
    output_sp: Path | None = None
    logfile: Path | None = None
    emissive: bool = True
    yxy: bool = True
    skip_calibration: bool = True
    high_res: bool = False
    display_type: str | None = None
    ccmx_or_ccss: Path | None = None
    average: int | None = None        # -Y a/aa/aaa : native multi-sample averaging (None = off)
    non_adaptive: bool = False        # -Y A : fixed (non-adaptive) integration time


class Argyll:
    """Small wrapper around ArgyllCMS command line tools."""

    def __init__(self, spotread: Path) -> None:
        self.spotread = spotread

    def enumerate_instruments(self) -> list[Instrument]:
        """Return instruments reported by `spotread -?`.

        Argyll prints the attached USB/serial instruments in its usage text.
        The exact wording varies between versions and instruments, so this
        parser intentionally accepts a few common shapes.
        """

        result = subprocess.run(
            [str(self.spotread), "-?"],
            text=True,
            capture_output=True,
            check=False,
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        return parse_spotread_instruments(text)

    def spotread_command(self, request: SpotreadRequest) -> list[str]:
        cmd = [str(self.spotread), "-c", str(request.port)]
        if request.emissive:
            cmd.append("-e")
        if request.yxy:
            cmd.append("-x")
        if request.high_res:
            cmd.append("-H")
        if request.display_type:
            cmd.extend(["-y", request.display_type])
        if request.ccmx_or_ccss:
            cmd.extend(["-X", str(request.ccmx_or_ccss)])
        if request.average:
            cmd.extend(["-Y", "a" * request.average])
        if request.non_adaptive:
            cmd.extend(["-Y", "A"])
        if request.skip_calibration:
            cmd.append("-N")

        if request.output_sp:
            cmd.extend(["-O", str(request.output_sp)])
            if request.logfile:
                cmd.append(str(request.logfile))
        else:
            cmd.append("-O")
            if request.logfile:
                cmd.append(str(request.logfile))
        return cmd

    def run_spotread_once(self, request: SpotreadRequest, timeout_seconds: int = 180) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.spotread_command(request),
            text=True,
            capture_output=True,
            # spotread is interactive ("hit any key to take a reading"); with no stdin it
            # inherits the parent's, which in a BACKGROUND run never EOFs → it blocks/returns
            # no reading → callers see 0.0. A closed stdin gives a deterministic EOF → exactly
            # one reading → exit, identical in foreground and background.
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )

    def interactive_command(self, request: SpotreadRequest) -> list[str]:
        """The spotread command for a PERSISTENT interactive session — the same
        instrument/correction flags as :meth:`spotread_command` but **without**
        ``-O`` (which does one measure and exits) and **without** ``-N``. The
        process stays alive: it auto-calibrates ONCE at startup and then takes one
        reading per trigger, so the calibrate/USB-open tax is paid once for the
        whole pass instead of once per patch (the whole point of the persistent
        meter). No ``-O`` / logfile here — the live result line is parsed off the
        stream, not a spectral file."""

        cmd = [str(self.spotread), "-c", str(request.port)]
        if request.emissive:
            cmd.append("-e")
        if request.yxy:
            cmd.append("-x")
        if request.high_res:
            cmd.append("-H")
        if request.display_type:
            cmd.extend(["-y", request.display_type])
        if request.ccmx_or_ccss:
            cmd.extend(["-X", str(request.ccmx_or_ccss)])
        if request.average:
            cmd.extend(["-Y", "a" * request.average])
        if request.non_adaptive:
            cmd.extend(["-Y", "A"])
        return cmd

    def open_persistent(self, request: SpotreadRequest, **kwargs) -> "PersistentSpotread":
        """Build a :class:`PersistentSpotread` that spawns ``spotread`` in
        interactive mode (one long-lived process). ``kwargs`` are forwarded to
        :class:`PersistentSpotread` (``trigger``, ``read_timeout``, …). The caller
        owns the lifecycle — :meth:`PersistentSpotread.close` it when the pass ends."""

        command = self.interactive_command(request)

        def factory() -> "SpotreadProcess":
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge: Argyll mixes prompts across stdout/stderr
                bufsize=0,                 # unbuffered → the reader thread sees prompts immediately
            )
            return _PipeSpotreadProcess(proc)

        return PersistentSpotread(factory, **kwargs)


def parse_spotread_instruments(text: str) -> list[Instrument]:
    instruments: list[Instrument] = []
    seen: set[int] = set()

    patterns = [
        re.compile(r"^\s*(\d+)\s*=\s*(.+?)\s*$"),
        re.compile(r"^\s*port\s+(\d+)\s*[:=-]\s*(.+?)\s*$", re.IGNORECASE),
        re.compile(r"^\s*(\d+)\)\s*(.+?)\s*$"),
    ]

    for line in text.splitlines():
        lowered = line.lower()
        if not any(token in lowered for token in ["i1", "color", "munki", "display", "spectro", "x-rite", "xrite"]):
            continue
        for pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            port = int(match.group(1))
            if port in seen:
                break
            desc = match.group(2).strip()
            instruments.append(Instrument(port=port, description=desc))
            seen.add(port)
            break

    return instruments


FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def parse_yxy(text: str) -> tuple[float, float, float] | None:
    match = re.search(rf"Yxy:\s*({FLOAT_RE})\s+({FLOAT_RE})\s+({FLOAT_RE})", text)
    if not match:
        return None
    return tuple(float(part) for part in match.groups())  # type: ignore[return-value]


def parse_xyz(text: str) -> tuple[float, float, float] | None:
    match = re.search(rf"XYZ:\s*({FLOAT_RE})[,\s]+({FLOAT_RE})[,\s]+({FLOAT_RE})", text)
    if not match:
        return None
    return tuple(float(part) for part in match.groups())  # type: ignore[return-value]


def command_for_log(command: Sequence[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


# ---------------------------------------------------------------------------
# Persistent (interactive) spotread driver
# ---------------------------------------------------------------------------
#
# The one-shot path (`run_spotread_once`, `-O`) re-spawns spotread, re-opens the
# USB instrument and re-runs the initial calibration for EVERY read — on this
# i1 DisplayPro that fixed tax is ~1.5–2 s/read (measured: the per-read floor is
# ~1.95 s while a bright-patch *integration* is sub-second). The persistent driver
# keeps ONE interactive spotread alive across the whole pass: calibrate once, then
# one reading per trigger. Each read drops to ~integration time, which also makes
# the repeatability gate and adaptive settle cheap.
#
# Transport is isolated behind `SpotreadProcess` on purpose: the one genuine
# hardware unknown is whether Windows Argyll triggers a reading from a *raw pipe*
# stdin (the simple `_PipeSpotreadProcess`) or needs a pseudo-console (ConPTY).
# DisplayCAL proves piped-stdin driving works; if box-validation shows otherwise,
# only a new SpotreadProcess implementation is needed — the state machine below
# (start → trigger → parse → loop → quit) is transport-agnostic and unit-tested.


@dataclass(frozen=True)
class SpotreadResult:
    """One reading off the live interactive stream. ``raw`` is the parsed text
    chunk (the ``Result is …`` line region) for the audit trail."""

    xyz: Optional[tuple[float, float, float]]
    yxy: Optional[tuple[float, float, float]]
    ok: bool
    error: Optional[str] = None
    raw: str = ""


# spotread's own under-range / unreliable warning vocabulary. A line containing one
# of these (printed BEFORE the result line it qualifies) demotes the next reading to
# ok=False — this is how a black/no-light/cap-on read is rejected WITHOUT a blanket
# luminance floor (a genuine black patch legitimately reads near-zero and must pass).
_SPOTREAD_WARN_TOKENS = (
    "unreliable", "too dark", "too bright", "too low", "saturat",
    "under range", "underrange", "out of range", "not in range", "obscured", "clip",
)


class SpotreadProcess(Protocol):
    """The transport seam for a live spotread process. ``read_some`` blocks and
    returns the next bytes (``b""`` at EOF); ``write`` feeds stdin; ``poll``
    returns the exit code or ``None`` while running; ``terminate``/``kill`` stop it."""

    def write(self, data: bytes) -> None: ...
    def read_some(self) -> bytes: ...
    def poll(self) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class _PipeSpotreadProcess:
    """Default :class:`SpotreadProcess` over a raw :class:`subprocess.Popen` pipe."""

    def __init__(self, proc: "subprocess.Popen[bytes]") -> None:
        self._p = proc

    def write(self, data: bytes) -> None:
        assert self._p.stdin is not None
        self._p.stdin.write(data)
        self._p.stdin.flush()

    def read_some(self) -> bytes:
        assert self._p.stdout is not None
        return self._p.stdout.read(1)  # blocking; b"" at EOF (unbuffered → 1 byte at a time is fine)

    def poll(self) -> Optional[int]:
        return self._p.poll()

    def terminate(self) -> None:
        try:
            self._p.terminate()
        except Exception:
            pass

    def kill(self) -> None:
        try:
            self._p.kill()
        except Exception:
            pass


class PersistentSpotread:
    """Drive one long-lived interactive ``spotread`` process.

    A background reader thread consumes the merged stdout/stderr; a line pump
    classifies each COMPLETE line as a reading (anchored on ``Result is … XYZ: …`` so
    ``Reference is now XYZ:`` / ``Making result XYZ:`` can never be mistaken for one),
    a spotread under-range/unreliable warning (demotes the next reading), or a
    calibration line. The trailing partial line (the no-newline ``take a reading:``
    prompt) stays buffered for prompt detection.

    Self-correcting against the i1 DisplayPro's real behaviour:
    - **Drain-before-trigger:** :meth:`measure` discards any readings already queued
      (a stray instrument-switch press, a startup-calibration reading, a prior
      desync) BEFORE sending its trigger, then consumes exactly one fresh reading —
      so producer/consumer can never drift off-by-one (a reading is never returned
      for the wrong patch).
    - **Validity gate:** a reading is ``ok`` only if XYZ parsed AND (when present) the
      XYZ and Yxy luminances agree (catches a corrupted/garbled line); a spotread
      warning demotes it. Low/zero luminance is NOT rejected — black patches are real.
    - **Bounded everywhere:** every wait has a deadline; a dead process / EOF yields a
      failed :class:`SpotreadResult`, never a hang; the buffer holds only the current
      partial line (extracted readings move to a small queue), so no unbounded growth.

    The one piece this CANNOT settle in software is whether Windows Argyll triggers a
    reading from a raw pipe at all (vs a ConPTY) — that is the at-the-box validation;
    if it needs a console, only a new :class:`SpotreadProcess` impl changes.
    """

    def __init__(
        self,
        factory: Callable[[], SpotreadProcess],
        *,
        trigger: bytes = b"\n",
        quit_command: bytes = b"q\n",
        start_timeout: float = 60.0,
        read_timeout: float = 120.0,
        poll_interval: float = 0.02,
        quiesce_seconds: float = 0.75,
    ) -> None:
        self._factory = factory
        self._trigger = trigger
        self._quit_command = quit_command
        self._start_timeout = start_timeout
        self._read_timeout = read_timeout
        self._poll_interval = poll_interval
        self._quiesce_seconds = quiesce_seconds

        self._proc: Optional[SpotreadProcess] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # All of the following are guarded by _lock:
        self._buf = bytearray()                     # UNPROCESSED bytes (trailing partial line)
        self._results: list[SpotreadResult] = []    # extracted readings, not yet consumed
        self._pending_warning: Optional[str] = None
        self._saw_cal = False
        self._total_bytes = 0                        # monotonic byte counter (quiescence)
        self._eof = False
        self._started = False
        # diagnostics (read after a run; surfaced in the digest later)
        self.stale_discarded = 0
        self.extra_readings = 0

    # spotread's interactive vocabulary (tolerant of version wording drift)
    _READY_RE = re.compile(r"take a reading", re.IGNORECASE)
    _CAL_RE = re.compile(r"calibrat", re.IGNORECASE)

    # -- reader thread + line pump ----------------------------------------

    def _reader_loop(self) -> None:
        with self._lock:
            proc = self._proc
        assert proc is not None
        while True:
            try:
                chunk = proc.read_some()
            except Exception:
                break
            if not chunk:
                break
            with self._lock:
                self._buf.extend(chunk)
                self._total_bytes += len(chunk)
                self._pump_locked()
        with self._lock:
            self._eof = True

    def _pump_locked(self) -> None:
        """Extract every complete (newline-terminated) line, classifying each.
        The trailing partial line stays in ``_buf``. Caller holds ``_lock``."""
        while True:
            idx = self._buf.find(b"\n")
            if idx == -1:
                return
            line = bytes(self._buf[: idx]).decode("ascii", "ignore")
            del self._buf[: idx + 1]
            self._classify_locked(line)

    def _classify_locked(self, line: str) -> None:
        low = line.lower()
        if "result is" in low and "xyz:" in low:
            xyz = parse_xyz(line)
            yxy = parse_yxy(line)
            ok, err = self._validate(xyz, yxy)
            warn = self._pending_warning
            self._pending_warning = None
            if ok and warn:
                ok, err = False, warn
            self._results.append(SpotreadResult(xyz=xyz, yxy=yxy, ok=ok, error=err, raw=line.strip()))
        elif any(tok in low for tok in _SPOTREAD_WARN_TOKENS):
            self._pending_warning = line.strip()
        elif self._CAL_RE.search(low):
            self._saw_cal = True
        # else: banner / chatter / reference / preset lines — ignored

    @staticmethod
    def _validate(xyz, yxy) -> tuple[bool, Optional[str]]:
        # spotread ALWAYS prints "Result is XYZ: …" first, so a line we classified as a
        # reading but whose XYZ won't parse is malformed (truncated / corrupted) — reject.
        if xyz is None:
            return False, "result line had no parseable XYZ"
        # Argyll prints the SAME Y in both groups ("Yxy: Y x y"); a mismatch means a
        # corrupted/garbled line (a dropped byte, interleaved stderr). This does NOT
        # reject low/zero luminance — a black patch reads near-zero and is legitimate;
        # only spotread's own under-range WARNING (above) demotes those.
        if yxy is not None:
            y1, y2 = xyz[1], yxy[0]
            if abs(y1 - y2) > 0.05 + 0.01 * max(abs(y1), abs(y2)):
                return False, f"XYZ/Yxy luminance mismatch ({y1:.4f} vs {y2:.4f})"
        return True, None

    # -- small lock-guarded views -----------------------------------------

    def _tail(self) -> str:
        with self._lock:
            return bytes(self._buf).decode("ascii", "ignore")

    def _bytes_seen(self) -> int:
        with self._lock:
            return self._total_bytes

    def _saw_cal_recently(self) -> bool:
        with self._lock:
            if self._saw_cal:
                return True
            return bool(self._CAL_RE.search(bytes(self._buf).decode("ascii", "ignore")))

    def _dead(self) -> bool:
        with self._lock:
            if self._eof:
                return True
            proc = self._proc
        return proc is not None and proc.poll() is not None

    def _wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Poll ``predicate()`` until true, the process dies/EOFs, or ``timeout``."""
        end = time.monotonic() + timeout
        while True:
            if predicate():
                return True
            if self._dead():
                return bool(predicate())
            if time.monotonic() >= end:
                return False
            time.sleep(self._poll_interval)

    def _wait_quiescent(self, settle: float, timeout: float) -> None:
        """Return once no new bytes have arrived for ``settle`` seconds."""
        end = time.monotonic() + timeout
        last = self._bytes_seen()
        last_change = time.monotonic()
        while time.monotonic() < end:
            if self._dead():
                return
            cur = self._bytes_seen()
            now = time.monotonic()
            if cur != last:
                last, last_change = cur, now
            elif now - last_change >= settle:
                return
            time.sleep(self._poll_interval)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        proc = self._factory()
        with self._lock:
            self._proc = proc
        self._reader = threading.Thread(target=self._reader_loop, name="spotread-reader", daemon=True)
        self._reader.start()
        # Wait for the real measurement prompt (its wording contains "take a reading"
        # on the i1 DisplayPro: "Hit ESC or Q to exit, any other key to take a reading:").
        ready = self._wait_for(lambda: bool(self._READY_RE.search(self._tail())), self._start_timeout)
        if not ready and self._saw_cal_recently():
            # A calibration step appears to be waiting; nudge once, then re-wait.
            self._send(self._trigger)
            ready = self._wait_for(lambda: bool(self._READY_RE.search(self._tail())), self._start_timeout)
        if not ready:
            # Wording-agnostic fallback. NB: the i1d3 startup-calibration handshake
            # (auto vs keypress) is an at-the-box unknown — validate it live.
            self._wait_quiescent(self._quiesce_seconds, self._start_timeout)
        # Discard anything produced during startup/calibration (including a reading the
        # nudge may have triggered) so the FIRST measure() begins from a clean slate.
        with self._lock:
            self._results.clear()
            self._pending_warning = None
        self._started = True

    def _send(self, data: bytes) -> None:
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        try:
            proc.write(data)
        except Exception:
            pass

    def measure(self, *, timeout: Optional[float] = None) -> SpotreadResult:
        """Trigger one reading and return it. Never raises for a measurement failure
        — a timeout / dead process / garbled or under-range read comes back as
        ``ok=False`` so the calling loop can decide (re-read, escalate)."""
        if not self._started:
            self.start()
        # Drain readings queued BEFORE this trigger — they predate this patch.
        with self._lock:
            stale = len(self._results)
            if stale:
                self._results.clear()
                self.stale_discarded += stale
        if self._dead():
            return SpotreadResult(None, None, ok=False, error="spotread process is not running")
        self._send(self._trigger)
        res = self._wait_result(timeout or self._read_timeout)
        if res is None:
            err = "spotread exited before a reading" if self._dead() else "timed out waiting for a reading"
            return SpotreadResult(None, None, ok=False, error=err, raw=self._tail()[:500])
        return res

    def _wait_result(self, timeout: float) -> Optional[SpotreadResult]:
        end = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._results:
                    extra = len(self._results) - 1
                    if extra > 0:
                        self.extra_readings += extra
                    res = self._results[-1]   # newest; any earlier are stale echoes
                    self._results.clear()
                    return res
            if self._dead():
                with self._lock:
                    if self._results:
                        res = self._results[-1]
                        self._results.clear()
                        return res
                return None
            if time.monotonic() >= end:
                return None
            time.sleep(self._poll_interval)

    def close(self) -> None:
        """Ask spotread to quit, escalate terminate→kill if it won't, and join the
        reader. Best-effort and idempotent."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        self._send(self._quit_command)
        if not self._await_exit(proc, 1.0):
            proc.terminate()
            if not self._await_exit(proc, 1.0):
                proc.kill()           # escalate: terminate didn't take (zombie / USB-blocked)
                self._await_exit(proc, 1.0)
        if self._reader is not None:
            self._reader.join(timeout=2.0)   # kill → read_some EOFs → reader exits
        with self._lock:
            self._proc = None
            self._reader = None
            self._started = False

    def _await_exit(self, proc: SpotreadProcess, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if proc.poll() is not None:
                return True
            time.sleep(self._poll_interval)
        return proc.poll() is not None

    def __enter__(self) -> "PersistentSpotread":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

