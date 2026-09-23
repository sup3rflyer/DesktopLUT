"""Tests for the persistent (interactive) spotread driver (``dlc.argyll``).

The hard, hardware-independent logic — process lifecycle, the trigger→parse→loop
state machine, bounded waits (timeout / EOF / dead process), buffer-position
scoping so a reading is never confused with the previous one's echo, and clean
shutdown — is exercised two ways:

1. an **in-memory** ``FakeSpotread`` implementing the ``SpotreadProcess`` seam with
   condition-variable blocking reads (deterministic, no subprocess), and
2. one **real subprocess** fake (a tiny Python script over an actual pipe) that
   proves the ``_PipeSpotreadProcess`` transport + reader thread plumbing.

The only thing NOT covered here is whether *Argyll on Windows* triggers a reading
from a raw pipe vs needs a pseudo-console — that is the box-validation step, and by
design it is the only piece (a new ``SpotreadProcess`` impl) that would change.
"""

from __future__ import annotations

import sys
import threading
import subprocess
import time
from pathlib import Path

from dlc.argyll import (
    Argyll,
    PersistentSpotread,
    SpotreadRequest,
    SpotreadResult,
    _PipeSpotreadProcess,
    _strip_ansi,
)


# ---------------------------------------------------------------------------
# In-memory fake transport
# ---------------------------------------------------------------------------

_PROMPT = b"Place instrument on spot to be measured,\nand hit any key to take a reading: "


class FakeSpotread:
    """An in-memory :class:`SpotreadProcess`. Emits a startup prompt, then one
    ``Result is … XYZ: … Yxy: …`` line per newline-terminated trigger. ``b"q"``
    quits. ``read_some`` blocks (1 byte at a time) like the real raw-pipe reader."""

    def __init__(self, *, prompt: bytes = _PROMPT, ignore_triggers: bool = False,
                 eof_after: int | None = None, responder=None, die_after: int | None = None) -> None:
        self._out = bytearray(prompt)
        self._in = bytearray()
        self._cv = threading.Condition()
        self._closed = False
        self._count = 0
        self._ignore = ignore_triggers
        self._eof_after = eof_after
        # responder(n) -> bytes to emit for the nth trigger (custom hostile payloads).
        # die_after=N -> close the stream right after emitting the Nth response (mid-line death).
        self._responder = responder
        self._die_after = die_after
        self.writes: list[bytes] = []

    # -- SpotreadProcess seam --
    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        with self._cv:
            self._in.extend(data)
        self._drain_lines()

    def read_some(self) -> bytes:
        with self._cv:
            while not self._out and not self._closed:
                self._cv.wait(timeout=1.0)
            if self._out:
                b = bytes(self._out[:1])
                del self._out[:1]
                return b
            return b""  # EOF

    def poll(self):
        with self._cv:
            return 0 if self._closed else None

    def terminate(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def kill(self) -> None:
        self.terminate()

    # -- behaviour --
    def _emit(self, data: bytes) -> None:
        with self._cv:
            self._out.extend(data)
            self._cv.notify_all()

    def _close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def _drain_lines(self) -> None:
        while True:
            with self._cv:
                idx = self._in.find(b"\n")
                if idx == -1:
                    return
                line = bytes(self._in[:idx])
                del self._in[: idx + 1]
            if line.strip().lower() == b"q":
                self._emit(b"\nSpot read stopped\n")
                self._close()
                return
            if self._ignore:
                continue
            self._count += 1
            if self._eof_after is not None and self._count > self._eof_after:
                self._close()
                return
            if self._responder is not None:
                payload = self._responder(self._count)
                if payload:
                    self._emit(payload)
                if self._die_after is not None and self._count >= self._die_after:
                    self._close()
                    return
                continue
            x = 95.0 + self._count
            self._emit(
                (" Result is XYZ: %f %f %f, Yxy: %f 0.312700 0.329000\n"
                 "and hit any key to take a reading: " % (x, 100.0, 108.0, 100.0)).encode("ascii")
            )


def _driver(fake: FakeSpotread, **kw) -> PersistentSpotread:
    kw.setdefault("start_timeout", 5.0)
    kw.setdefault("read_timeout", 5.0)
    kw.setdefault("restart_backoff_s", 0.0)   # the self-heal's real-world USB backoff, not in tests
    return PersistentSpotread(lambda: fake, **kw)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_start_then_single_measure_parses_xyz_and_yxy():
    fake = FakeSpotread()
    drv = _driver(fake)
    drv.start()
    res = drv.measure()
    assert res.ok
    assert res.xyz is not None and res.yxy is not None
    assert abs(res.xyz[0] - 96.0) < 1e-6   # 95 + first count
    assert abs(res.yxy[1] - 0.3127) < 1e-6
    drv.close()


def test_multiple_reads_do_not_confuse_consecutive_results():
    fake = FakeSpotread()
    with _driver(fake) as drv:
        first = drv.measure()
        second = drv.measure()
        third = drv.measure()
    # Each read scans only output produced after its own trigger → strictly distinct.
    assert [r.xyz[0] for r in (first, second, third)] == [96.0, 97.0, 98.0]


def test_measure_auto_starts_when_not_started_explicitly():
    fake = FakeSpotread()
    drv = _driver(fake)
    res = drv.measure()  # no explicit start()
    assert res.ok and res.xyz is not None
    drv.close()


def test_stale_warning_does_not_demote_next_reading():
    # A warn token can arrive between reads (a transient), setting _pending_warning with no
    # following result line. The trigger-time drain must drop it so it can't be attached to —
    # and falsely demote — the NEXT patch's valid reading (M1).
    fake = FakeSpotread()
    drv = _driver(fake)
    drv.start()
    with drv._lock:
        drv._pending_warning = "spurious under-range warning from a prior transient"
    res = drv.measure()
    assert res.ok is True and res.error is None   # the stale warning was dropped, not attached
    drv.close()


def test_trigger_and_quit_bytes_are_forwarded():
    fake = FakeSpotread()
    drv = _driver(fake, trigger=b"\n", quit_command=b"q\n")
    drv.start()
    drv.measure()
    assert b"\n" in fake.writes            # the reading trigger went down stdin
    drv.close()
    assert fake.writes[-1] == b"q\n"       # quit was sent on close


def test_measure_times_out_without_a_reading():
    fake = FakeSpotread(ignore_triggers=True)
    drv = _driver(fake, read_timeout=0.3)
    drv.start()
    res = drv.measure()
    assert not res.ok
    assert "timed out" in (res.error or "")
    drv.close()


def test_measure_reports_dead_process_on_eof():
    fake = FakeSpotread(eof_after=0)  # first trigger closes the stream
    drv = _driver(fake, read_timeout=2.0)
    drv.start()
    res = drv.measure()
    assert not res.ok
    assert "exited" in (res.error or "")
    # A subsequent measure short-circuits on the dead process, still no hang.
    again = drv.measure()
    assert not again.ok
    drv.close()


def test_start_falls_back_to_quiescence_when_prompt_wording_is_unknown():
    # No "take a reading" anywhere → start() must still settle via the quiescence path.
    fake = FakeSpotread(prompt=b">>> ready <<<\n")
    drv = _driver(fake, start_timeout=2.0, quiesce_seconds=0.15)
    drv.start()
    res = drv.measure()
    assert res.ok and res.xyz is not None
    drv.close()


def test_close_is_idempotent():
    fake = FakeSpotread()
    drv = _driver(fake)
    drv.start()
    drv.close()
    drv.close()  # must not raise


# ---------------------------------------------------------------------------
# Startup calibration handshake (spectros: ColorChecker Studio / ColorMunki / i1Studio)
# ---------------------------------------------------------------------------

_CAL_PROMPT = (
    b" Place instrument on its reflective white reference,\n"
    b" and set it to the calibration position,\n"
    b" then hit any key to start calibration.\n"
    b" Hit ESC or Q to abort: "
)


class CalibratingFakeSpotread:
    """A spectro that PARKS at a calibration keypress prompt and only reaches the
    "take a reading" prompt AFTER a keypress starts (and completes) calibration — the
    i1Studio-family behaviour the i1 DisplayPro doesn't exhibit. Unlike
    :class:`FakeSpotread`, no reading prompt is emitted up front, so a driver that waits
    for "take a reading" before nudging would block until its start_timeout."""

    def __init__(self, *, cal_prompt: bytes = _CAL_PROMPT, cal_delay: float = 0.0) -> None:
        self._out = bytearray(cal_prompt)
        self._in = bytearray()
        self._cv = threading.Condition()
        self._closed = False
        self._calibrated = False
        self._count = 0
        self._cal_delay = cal_delay
        self.writes: list[bytes] = []

    # -- SpotreadProcess seam --
    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        if data.strip().lower() == b"q":
            self._emit(b"\nSpot read stopped\n")
            self._close()
            return
        if not self._calibrated:
            self._calibrated = True
            # Echo that the keypress landed and calibration began, then — ASYNCHRONOUSLY,
            # so write() returns at once like the real instrument — stay SILENT for
            # cal_delay before emitting the reading prompt. That silent gap is exactly the
            # window in which a naive driver might wrongly send a second keypress.
            self._emit(b"\nCalibrating...\n")

            def _finish_cal() -> None:
                if self._cal_delay:
                    time.sleep(self._cal_delay)
                self._emit(b"Calibration complete\n"
                           b"Place instrument on spot to be measured,\n"
                           b"and hit any key to take a reading: ")

            threading.Thread(target=_finish_cal, daemon=True).start()
            return
        self._count += 1
        self._emit((" Result is XYZ: %f 100.000000 108.000000, Yxy: 100.000000 0.312700 0.329000\n"
                    "and hit any key to take a reading: " % (95.0 + self._count)).encode("ascii"))

    def read_some(self) -> bytes:
        with self._cv:
            while not self._out and not self._closed:
                self._cv.wait(timeout=1.0)
            if self._out:
                b = bytes(self._out[:1])
                del self._out[:1]
                return b
            return b""

    def poll(self):
        with self._cv:
            return 0 if self._closed else None

    def terminate(self) -> None:
        self._close()

    def kill(self) -> None:
        self._close()

    def _emit(self, data: bytes) -> None:
        with self._cv:
            self._out.extend(data)
            self._cv.notify_all()

    def _close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


def test_start_nudges_calibration_prompt_promptly_not_after_start_timeout():
    # THE REGRESSION GUARD: a spectro parked at its cal prompt must be nudged as soon as
    # the stream goes idle, not after the whole start_timeout elapses. start_timeout is
    # set large on purpose — the OLD code would block ~that long before the reading prompt.
    fake = CalibratingFakeSpotread()
    drv = _driver(fake, start_timeout=20.0, quiesce_seconds=0.2)
    t0 = time.monotonic()
    drv.start()
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"start() took {elapsed:.1f}s — it waited out start_timeout instead of nudging"
    assert fake.writes == [b"\n"], "exactly one calibration keypress should be sent before ready"
    # And the session is genuinely usable afterwards.
    res = drv.measure()
    assert res.ok and res.xyz is not None
    assert abs(res.xyz[0] - 96.0) < 1e-6
    drv.close()


def test_start_tolerates_calibration_integration_silence():
    # The keypress starts a calibration that is SILENT for a couple of seconds before the
    # reading prompt. The grace window must ride that out with a single nudge (no spurious
    # second keypress that would later manifest as a wasted/queued reading).
    fake = CalibratingFakeSpotread(cal_delay=2.0)
    drv = _driver(fake, start_timeout=20.0, quiesce_seconds=0.2)
    drv.start()
    assert fake.writes == [b"\n"], f"expected one nudge, got {fake.writes!r}"
    res = drv.measure()
    assert res.ok and abs(res.xyz[0] - 96.0) < 1e-6
    drv.close()


def _result_line(x, y, z, *, ylum=None, cx=0.3127, cy=0.3290, prompt=True):
    ylum = y if ylum is None else ylum
    line = " Result is XYZ: %f %f %f, Yxy: %f %f %f\n" % (x, y, z, ylum, cx, cy)
    if prompt:
        line += "and hit any key to take a reading: "
    return line.encode("ascii")


# ---------------------------------------------------------------------------
# Hostile / hardware-faithful behaviour (the fixes from the adversarial review)
# ---------------------------------------------------------------------------

def test_result_parse_is_anchored_on_result_is_not_bare_xyz():
    # spotread can print 'Reference is now XYZ: …' (the r key) — it contains 'XYZ:'
    # but is NOT a reading. The driver must ignore it and return the real result.
    def responder(n):
        return (b" Reference is now XYZ: 50.0 50.0 50.0 Lab: 76 0 0\n"
                + _result_line(30.0, 30.0, 31.0))
    drv = _driver(FakeSpotread(responder=responder))
    drv.start()
    res = drv.measure()
    assert res.ok
    assert abs(res.xyz[0] - 30.0) < 1e-6   # the real reading, not the 50.0 reference
    drv.close()


def test_under_range_warning_demotes_the_next_reading():
    # A spotread 'unreliable' warning precedes a (near-)zero reading: the reading
    # must come back ok=False with the warning — WITHOUT a blanket luminance floor.
    def responder(n):
        return (b"Warning - reading may be unreliable\n"
                + _result_line(0.0, 0.0, 0.0))
    drv = _driver(FakeSpotread(responder=responder))
    drv.start()
    res = drv.measure()
    assert not res.ok
    assert "unreliable" in (res.error or "")
    assert res.xyz == (0.0, 0.0, 0.0)      # data preserved for the audit, just not trusted
    drv.close()


def test_legitimate_near_black_reading_is_accepted():
    # No warning → a genuine near-black patch (low Y) is a VALID reading, not rejected.
    drv = _driver(FakeSpotread(responder=lambda n: _result_line(0.02, 0.02, 0.03)))
    drv.start()
    res = drv.measure()
    assert res.ok and res.xyz[1] < 0.1
    drv.close()


def test_malformed_xyz_line_is_rejected_not_parsed_as_garbage():
    # A truncated XYZ (2 of 3 floats) must NOT be accepted as a reading.
    drv = _driver(FakeSpotread(responder=lambda n: b" Result is XYZ: 30.0 30.0, Yxy: 30.0 0.31 0.33\n"),
                  read_timeout=0.4)
    drv.start()
    res = drv.measure()
    assert not res.ok
    drv.close()


def test_xyz_yxy_luminance_mismatch_is_rejected():
    # A corrupted line where the XYZ Y and the Yxy Y disagree → reject.
    drv = _driver(FakeSpotread(responder=lambda n: _result_line(30.0, 30.0, 31.0, ylum=80.0)),
                  read_timeout=0.4)
    drv.start()
    res = drv.measure()
    assert not res.ok
    assert "mismatch" in (res.error or "")
    drv.close()


def test_death_mid_line_does_not_parse_a_partial_reading():
    # Process emits a partial result line (no newline) then dies → must be ok=False.
    def responder(n):
        return b" Result is XYZ: 30.0 30.0 31"   # no comma/Yxy/newline
    drv = _driver(FakeSpotread(responder=responder, die_after=1), read_timeout=2.0)
    drv.start()
    res = drv.measure()
    assert not res.ok
    drv.close()


def test_stale_queued_reading_is_drained_before_a_trigger():
    # A reading that predates this trigger (stray instrument-switch press, prior
    # desync) must be discarded, not returned for this patch.
    from dlc.argyll import SpotreadResult
    drv = _driver(FakeSpotread())
    drv.start()
    with drv._lock:
        drv._results.append(SpotreadResult(xyz=(1.0, 1.0, 1.0), yxy=(1.0, 0.3, 0.3), ok=True, raw="stale"))
    res = drv.measure()
    assert res.ok
    assert abs(res.xyz[0] - 96.0) < 1e-6   # the fresh reading, not the stale (1,1,1)
    assert drv.stale_discarded == 1
    drv.close()


def test_two_readings_for_one_trigger_takes_latest_and_counts_extra():
    # If a stray reading lands alongside the triggered one, take the newest and note it.
    def responder(n):
        return _result_line(10.0, 10.0, 11.0, prompt=False) + _result_line(20.0, 20.0, 21.0)
    drv = _driver(FakeSpotread(responder=responder))
    drv.start()
    res = drv.measure()
    assert res.ok and abs(res.xyz[0] - 20.0) < 1e-6
    assert drv.extra_readings == 1
    drv.close()


# ---------------------------------------------------------------------------
# ConPTY transport: VT-decorated stream + default wiring (the box-validated fix)
# ---------------------------------------------------------------------------

def test_strip_ansi_removes_vt_decoration_keeps_reading_text():
    raw = ("\x1b[2K\x1b[0m Result is XYZ: 80.000000 85.000000 90.000000, "
           "Yxy: 85.000000 0.310000 0.330000\x1b[?25h\r")
    clean = _strip_ansi(raw)
    assert "\x1b" not in clean and "\r" not in clean
    assert "Result is XYZ: 80.000000 85.000000 90.000000" in clean


def test_conpty_style_ansi_decorated_readings_parse_cleanly():
    # A ConPTY wraps spotread's line-oriented output in VT/SGR escapes, echoes CRs,
    # and re-emits the prompt with cursor codes. The pump must strip all of it and
    # still parse XYZ/Yxy — this is what makes the pseudo-console transport usable.
    def responder(n):
        x = 80.0 + n
        return (
            "\x1b[2K\x1b[0m Result is XYZ: %f %f %f, Yxy: %f 0.312700 0.329000\x1b[0m\r\n"
            "\x1b[?25h\x1b[1mPlace instrument and hit any key to take a reading: \x1b[0m"
            % (x, 100.0, 108.0, 100.0)
        ).encode("ascii")

    prompt = b"\x1b[2J\x1b[H Spot read\r\n hit any key to take a reading: "
    fake = FakeSpotread(prompt=prompt, responder=responder)
    with _driver(fake) as drv:
        first = drv.measure()
        second = drv.measure()
    assert first.ok and second.ok
    assert first.xyz is not None and abs(first.xyz[0] - 81.0) < 1e-6
    assert second.xyz is not None and abs(second.xyz[0] - 82.0) < 1e-6
    assert abs(first.yxy[1] - 0.3127) < 1e-6


def test_open_persistent_defaults_to_conpty_with_enter_trigger():
    arg = Argyll(Path("spotread.exe"))
    req = SpotreadRequest(port=1)
    # Default transport is ConPTY (Windows-correct); the factory is lazy so nothing spawns.
    meter = arg.open_persistent(req)
    assert meter._trigger == b"\r"
    # The raw-pipe fallback keeps the newline trigger.
    pipe_meter = arg.open_persistent(req, transport="pipe")
    assert pipe_meter._trigger == b"\n"


# ---------------------------------------------------------------------------
# Command construction: interactive vs one-shot
# ---------------------------------------------------------------------------

def test_interactive_command_omits_O_and_N_but_keeps_instrument_flags():
    from pathlib import Path
    from dlc.argyll import Argyll, SpotreadRequest
    a = Argyll(Path("spotread.exe"))
    req = SpotreadRequest(port=3, ccmx_or_ccss=Path("x.ccmx"), high_res=True,
                          display_type="n", average=2)
    cmd = a.interactive_command(req)
    # -O (one-shot exit) and -N (i1d3-unsupported skip-cal) MUST be absent.
    assert "-O" not in cmd and "-N" not in cmd
    # instrument-bearing flags present and correct.
    assert cmd[cmd.index("-c") + 1] == "3"
    assert "-e" in cmd and "-x" in cmd and "-H" in cmd
    assert cmd[cmd.index("-X") + 1] == "x.ccmx"
    assert cmd[cmd.index("-y") + 1] == "n"
    assert "-Y" in cmd and "aa" in cmd      # average=2 -> -Y aa


def test_interactive_and_oneshot_share_the_same_measurement_flags():
    from pathlib import Path
    from dlc.argyll import Argyll, SpotreadRequest
    a = Argyll(Path("spotread.exe"))
    req = SpotreadRequest(port=1, ccmx_or_ccss=Path("c.ccmx"), high_res=True, display_type="n")
    interactive = a.interactive_command(req)
    oneshot = a.spotread_command(req)
    for flag in ("-e", "-x", "-H"):
        assert (flag in interactive) == (flag in oneshot) == True
    # same correction + display type fed to both
    assert interactive[interactive.index("-X") + 1] == oneshot[oneshot.index("-X") + 1] == "c.ccmx"
    assert interactive[interactive.index("-y") + 1] == oneshot[oneshot.index("-y") + 1] == "n"
    # the ONLY measurement-flag difference is one-shot's -N/-O.
    assert "-O" in oneshot and "-N" in oneshot


# ---------------------------------------------------------------------------
# Real subprocess transport (pipe + reader thread plumbing)
# ---------------------------------------------------------------------------

_FAKE_SPOTREAD = '''\
import sys
sys.stdout.write("Place instrument on spot to be measured,\\n")
sys.stdout.write("and hit any key to take a reading: ")
sys.stdout.flush()
n = 0
for line in sys.stdin:
    if line.strip().lower() == "q":
        sys.stdout.write("\\nSpot read stopped\\n"); sys.stdout.flush(); break
    n += 1
    sys.stdout.write(" Result is XYZ: %f %f %f, Yxy: %f 0.312700 0.329000\\n" % (95.0 + n, 100.0, 108.0, 100.0))
    sys.stdout.write("and hit any key to take a reading: ")
    sys.stdout.flush()
'''


def test_real_subprocess_pipe_round_trips(tmp_path):
    script = tmp_path / "fake_spotread.py"
    script.write_text(_FAKE_SPOTREAD, encoding="utf-8")

    def factory():
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
        )
        return _PipeSpotreadProcess(proc)

    drv = PersistentSpotread(factory, start_timeout=10.0, read_timeout=10.0)
    with drv:
        r1 = drv.measure()
        r2 = drv.measure()
    assert r1.ok and r2.ok
    assert abs(r1.xyz[0] - 96.0) < 1e-6
    assert abs(r2.xyz[0] - 97.0) < 1e-6


# ---------------------------------------------------------------------------
# Composer: make_persistent_spotread_meter → MeasureFn
# ---------------------------------------------------------------------------

def test_persistent_meter_composer_maps_result_and_shows_patch():
    from dlc.engine.patches import Transfer, to_signal
    from dlc.measure_loop import MeasurePatch, make_persistent_spotread_meter

    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    shown: list[MeasurePatch] = []

    class _Presenter:
        def show(self, patch):
            shown.append(patch)

        def close(self):
            pass

    fake = FakeSpotread()
    drv = _driver(fake)
    drv.start()
    meter = make_persistent_spotread_meter(presenter=_Presenter(), persistent=drv)

    cv = (511, 511, 511)
    patch = MeasurePatch(label="p0", rgb=cv, signal=to_signal([cv], t)[0])
    reading = meter(patch)

    assert shown == [patch]               # presenter was driven
    assert reading.ok and reading.xyz is not None
    assert reading.raw.get("persistent") is True
    drv.close()


def test_spotread_result_defaults():
    r = SpotreadResult(xyz=None, yxy=None, ok=False)
    assert r.error is None and r.raw == "" and r.fault is None


# ---------------------------------------------------------------------------
# Bounded self-heal (2026-09-23 HDR-run incident: a spotread that died at startup left the
# driver answering "process is not running" instantly and forever, its error text lost)
# ---------------------------------------------------------------------------

_USB_OPEN_FAILURE = (b"Setting up the instrument\n"
                     b"Instrument access failed with error 'Communications failure'\n")


def _dead_on_arrival(output: bytes = _USB_OPEN_FAILURE) -> FakeSpotread:
    """A spotread that prints its instrument-open error and exits before any prompt —
    already exited (poll() != None) while its output is still unread, like the real one."""
    fake = FakeSpotread(prompt=output)
    fake._close()
    return fake


class _SequencedFactory:
    """Hands out one fresh fake per spawn from ``builders`` (the last one repeats)."""

    def __init__(self, *builders) -> None:
        self._builders = list(builders)
        self.calls = 0
        self.spawned: list = []

    def __call__(self):
        idx = min(self.calls, len(self._builders) - 1)
        self.calls += 1
        proc = self._builders[idx]()
        self.spawned.append(proc)
        return proc


def _healing(factory, **kw) -> PersistentSpotread:
    kw.setdefault("start_timeout", 2.0)
    kw.setdefault("read_timeout", 2.0)
    kw.setdefault("quiesce_seconds", 0.1)
    kw.setdefault("restart_backoff_s", 0.0)
    return PersistentSpotread(factory, **kw)


def test_dead_at_first_measure_respawns_and_returns_a_valid_reading():
    # The incident: the process spotread spawned for the resumed stage died at startup. The
    # first measure() must capture its dying words, respawn ONE fresh process, and read.
    factory = _SequencedFactory(_dead_on_arrival, FakeSpotread)
    drv = _healing(factory)
    res = drv.measure()
    assert res.ok and res.xyz is not None
    assert abs(res.xyz[0] - 96.0) < 1e-6          # the respawned process's first reading
    assert factory.calls == 2
    assert drv.restarts == 1 and drv.restart_failures == 0 and drv.deaths == 1
    assert "Communications failure" in (drv.last_death_tail or "")
    assert drv.death_log[-1]["context"] == "startup"
    # The healed process stays healthy: no further respawns, readings continue in sequence.
    again = drv.measure()
    assert again.ok and abs(again.xyz[0] - 97.0) < 1e-6
    assert drv.restarts == 1 and factory.calls == 2
    drv.close()
    assert factory.spawned[-1].writes[-1] == b"q\n"   # close() still quits the RESPAWNED process


def test_process_dying_mid_read_is_respawned_and_the_patch_re_read():
    # Dies while we wait for the reading (after printing an error): record the tail, respawn,
    # re-trigger — the caller's patch is still presented, so the fresh read is valid for it.
    def dies_mid_read():
        return FakeSpotread(responder=lambda n: b"Instrument read failed: USB transfer error\n",
                            die_after=1)

    factory = _SequencedFactory(dies_mid_read, FakeSpotread)
    drv = _healing(factory)
    drv.start()
    res = drv.measure()
    assert res.ok and abs(res.xyz[0] - 96.0) < 1e-6
    assert drv.restarts == 1 and drv.deaths == 1
    assert drv.death_log[-1]["context"] == "mid-read"
    assert "USB transfer error" in (drv.last_death_tail or "")
    drv.close()


def test_self_heal_budget_exhausted_fails_with_the_captured_tail():
    # A truly unplugged meter must still FAIL — bounded, fast, and saying why — not spin.
    factory = _SequencedFactory(_dead_on_arrival)
    drv = _healing(factory, restart_budget=3)
    t0 = time.monotonic()
    res = drv.measure()
    assert not res.ok and res.xyz is None
    assert res.fault == "self_heal_exhausted"
    assert "self-heal exhausted" in (res.error or "")
    assert "Communications failure" in (res.error or "")     # spotread's own error, in the error
    assert "Communications failure" in res.raw                # and the full tail in raw
    assert factory.calls == 1 + 3                            # the initial spawn + 3 respawns
    assert drv.restarts == 3 and drv.restart_failures == 3 and drv.deaths == 4
    # Exhausted: later calls fail IMMEDIATELY without spawning again (no unbounded retry).
    again = drv.measure()
    assert again.fault == "self_heal_exhausted" and factory.calls == 4
    assert time.monotonic() - t0 < 10.0
    drv.close()


def test_failed_spawn_never_raises_from_measure_and_is_budgeted():
    def boom():
        raise OSError("spotread.exe not found")

    drv = _healing(boom, restart_budget=2)
    res = drv.measure()                    # must not raise
    assert not res.ok and res.fault == "self_heal_exhausted"
    assert "spawn failed" in (res.error or "") and "not found" in (res.error or "")
    assert drv.restarts == 2 and drv.deaths == 3
    # Later calls do not re-spawn outside the budget (no lazy re-spawn per call).
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        raise OSError("still missing")

    drv._factory = counting
    drv.measure()
    assert calls["n"] == 0
    drv.close()


def test_self_heal_budget_is_a_sliding_window():
    factory = _SequencedFactory(_dead_on_arrival)
    drv = _healing(factory, restart_budget=1, restart_window_s=0.3)
    assert drv.measure().fault == "self_heal_exhausted"
    assert factory.calls == 2                       # initial + the one budgeted respawn
    assert drv.measure().fault == "self_heal_exhausted"
    assert factory.calls == 2                       # window still full → no respawn
    time.sleep(0.35)
    drv.measure()
    assert factory.calls == 3                       # the window slid → one more attempt
    drv.close()


def test_owner_close_mid_read_is_never_respawned():
    # The stall watchdog force-closes a wedged meter from another thread: the in-flight
    # measure() must return (fault="closed") — NOT respawn a process over the deliberate kill.
    factory = _SequencedFactory(lambda: FakeSpotread(ignore_triggers=True))
    drv = _healing(factory, read_timeout=10.0)
    drv.start()
    out: dict = {}
    th = threading.Thread(target=lambda: out.setdefault("res", drv.measure()))
    th.start()
    time.sleep(0.3)
    drv.close()
    th.join(timeout=5.0)
    assert not th.is_alive()
    res = out["res"]
    assert not res.ok and res.fault == "closed"
    assert factory.calls == 1 and drv.restarts == 0


def test_close_is_sticky_until_an_explicit_start():
    # close() means "no spotread behind my back": a measure() after (or racing) the owner's
    # close fails with fault="closed" and spawns nothing. An explicit start() re-opens with a
    # clean stream (a stale EOF from the closed process must not make the new one look dead).
    factory = _SequencedFactory(FakeSpotread)
    drv = _healing(factory)
    assert drv.measure().ok
    drv.close()
    closed = drv.measure()
    assert not closed.ok and closed.fault == "closed" and factory.calls == 1
    drv.start()
    res = drv.measure()
    assert res.ok and abs(res.xyz[0] - 96.0) < 1e-6   # a FRESH process's first reading
    assert factory.calls == 2 and drv.restarts == 0
    drv.close()


def test_one_measure_call_never_outlives_its_respawn_budget():
    # Review finding: with a sliding window, a respawn→die cycle slower than window/budget
    # refilled the budget INSIDE one measure() call, so the call never returned. The per-call
    # cap bounds it: comes up fine, dies 0.3 s after each trigger, window far shorter.
    def dies_after_trigger():
        fake = FakeSpotread(ignore_triggers=True)
        orig_write = fake.write

        def write(data):
            orig_write(data)
            if data.strip().lower() != b"q":          # a reading trigger (b"\n"), not quit
                threading.Timer(0.3, fake._close).start()

        fake.write = write
        return fake

    factory = _SequencedFactory(dies_after_trigger)
    drv = _healing(factory, restart_budget=2, restart_window_s=0.2, read_timeout=5.0)
    t0 = time.monotonic()
    res = drv.measure()
    assert time.monotonic() - t0 < 8.0
    assert not res.ok and res.fault == "self_heal_exhausted"
    assert factory.calls == 1 + 2                   # initial + exactly the per-call budget
    drv.close()


_DIES_AT_STARTUP = '''\
import sys
sys.stdout.write("Setting up the instrument\\n"); sys.stdout.flush()
sys.stderr.write("Instrument access failed with error 'Communications failure'\\n")
sys.stderr.flush()
sys.exit(1)
'''


def test_real_subprocess_death_at_startup_is_captured_and_healed(tmp_path):
    # Real processes over the raw pipe: the first spotread prints its instrument-open error
    # (stderr, merged) and exits 1; the self-heal captures that text and the respawned
    # process reads normally.
    dying = tmp_path / "dies.py"
    dying.write_text(_DIES_AT_STARTUP, encoding="utf-8")
    healthy = tmp_path / "fake_spotread.py"
    healthy.write_text(_FAKE_SPOTREAD, encoding="utf-8")
    scripts = [dying, healthy]

    def factory():
        script = scripts.pop(0) if len(scripts) > 1 else scripts[0]
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
        )
        return _PipeSpotreadProcess(proc)

    drv = PersistentSpotread(factory, start_timeout=10.0, read_timeout=10.0,
                             quiesce_seconds=0.2, restart_backoff_s=0.0)
    try:
        res = drv.measure()
        assert res.ok and abs(res.xyz[0] - 96.0) < 1e-6
        assert drv.restarts == 1 and drv.deaths == 1
        assert "Communications failure" in (drv.last_death_tail or "")
        assert drv.last_death_exit_code == 1
    finally:
        drv.close()


def test_composer_surfaces_restart_and_meter_down_to_the_loop():
    from dlc.engine.patches import Transfer, to_signal
    from dlc.measure_loop import MeasurePatch, make_persistent_spotread_meter

    class _Presenter:
        def show(self, patch):
            pass

        def close(self):
            pass

    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    cv = (511, 511, 511)
    patch = MeasurePatch(label="p0", rgb=cv, signal=to_signal([cv], t)[0])

    healed = _healing(_SequencedFactory(_dead_on_arrival, FakeSpotread))
    reading = make_persistent_spotread_meter(presenter=_Presenter(), persistent=healed)(patch)
    assert reading.ok
    assert reading.raw["meter_restarts"] == 1
    assert "Communications failure" in reading.raw["meter_death_tail"]
    assert "meter_down" not in reading.raw
    healed.close()

    dead = _healing(_SequencedFactory(_dead_on_arrival), restart_budget=1)
    reading = make_persistent_spotread_meter(presenter=_Presenter(), persistent=dead)(patch)
    assert not reading.ok
    assert reading.raw["meter_fault"] == "self_heal_exhausted" and reading.raw["meter_down"] is True
    assert "Communications failure" in (reading.error or "")
    # attempts are reported, but no respawn is CLAIMED when none came up alive
    assert reading.raw["meter_restart_attempts"] == 1 and reading.raw["meter_restarts"] == 0
    dead.close()


def test_dead_meter_stops_the_preheat_soak_fast_with_the_error_surfaced(tmp_path: Path):
    # End-to-end incident replay (no hardware): the persistent meter can never come up, the
    # measure stage starts with the preheat soak. The OLD behaviour cycled soak patches on
    # instant failed reads until the stall watchdog rolled the run back; now the soak halts
    # on the first terminal read and the run-stopper digest carries spotread's error.
    from dlc.engine.patches import Transfer
    from dlc.events import RunLog, read_events
    from dlc.measure_loop import MeasureLoopConfig, make_persistent_spotread_meter, run_measure_loop

    class _Presenter:
        def __init__(self):
            self.shown = 0

        def show(self, patch):
            self.shown += 1

        def close(self):
            pass

    t = Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    presenter = _Presenter()
    drv = _healing(_SequencedFactory(_dead_on_arrival), restart_budget=3)
    meter = make_persistent_spotread_meter(presenter=presenter, persistent=drv)
    epath = tmp_path / "events.jsonl"
    greys = [(v, v, v) for v in (100, 300, 500, 700, 900, 1023)]
    t0 = time.monotonic()
    res = run_measure_loop(patches=greys, transfer=t, measure=meter,
                           config=MeasureLoopConfig(preheat="always"),
                           runlog=RunLog(epath, phase="measure:verify"),
                           ndjson_path=tmp_path / "m.ndjson")
    assert time.monotonic() - t0 < 10.0
    assert presenter.shown == 1                       # halted on the FIRST read, no patch cycling
    assert res.digest["meter_down"] is True and res.needs_adjudication
    assert "meter_down" in res.digest["anomaly_reasons"]
    assert res.patch_count == 0
    assert "METER DOWN" in (res.question or "")
    assert "Communications failure" in (res.question or "")
    assert res.digest["meter_down_detail"]["measure_phase"] == "preheat"
    events = read_events(epath)
    failed = [e for e in events if e.event == "meter_read_failed"]
    assert failed and "Communications failure" in failed[0].data["error"]
    assert failed[0].level == "WARN"                  # digest tier: the LLM sees it at once
    assert any(e.event == "anomaly" and e.data.get("kind") == "meter_down" for e in events)
    assert not any(e.event == "meter_restarted" for e in events)   # no respawn ever came up
    assert failed[0].data["respawn_attempts"] == 3
    drv.close()
