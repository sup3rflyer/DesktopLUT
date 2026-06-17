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

from dlc.argyll import (
    PersistentSpotread,
    SpotreadResult,
    _PipeSpotreadProcess,
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
    assert r.error is None and r.raw == ""
