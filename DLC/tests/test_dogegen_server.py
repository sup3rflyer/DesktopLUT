"""Tests for the persistent dogegen daemon protocol + the SocketPresenter that drives it.

These exercise the line protocol and the orchestrator-side presenter without spawning a
real dogegen window: ``dispatch`` is pure, and the presenter is round-tripped against a
tiny in-process socket server that mimics the daemon's replies.
"""

from __future__ import annotations

import socket
import threading

from dlc.dogegen_server import dispatch
from dlc.measure_loop import MeasurePatch, SocketPresenter


# ---------------------------------------------------------------------------
# spotread must be driven with a closed stdin (deterministic in background runs)
# ---------------------------------------------------------------------------

def test_run_spotread_once_closes_stdin(monkeypatch):
    """spotread is interactive; run_spotread_once must pass stdin=DEVNULL so a background
    run gets a deterministic EOF → one reading (regression: it returned 0.0 in background)."""
    import subprocess
    from pathlib import Path
    from dlc.argyll import Argyll, SpotreadRequest
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="XYZ: 1 1 1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    Argyll(Path("spotread.exe")).run_spotread_once(SpotreadRequest(port=1))
    assert captured.get("stdin") is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# protocol dispatch (pure)
# ---------------------------------------------------------------------------

def test_dispatch_patch_calls_show_and_acks():
    shown = []
    reply, keep = dispatch("1023 512 0", show=lambda r, g, b: shown.append((r, g, b)))
    assert reply == "ok" and keep is True
    assert shown == [(1023, 512, 0)]


def test_dispatch_ping_and_quit():
    assert dispatch("ping", show=lambda *a: None) == ("pong", True)
    assert dispatch("quit", show=lambda *a: None) == ("bye", False)


def test_dispatch_blank_is_noop_and_bad_command_errors():
    assert dispatch("   ", show=lambda *a: None) == ("", True)
    reply, keep = dispatch("hello there", show=lambda *a: None)
    assert reply.startswith("err") and keep is True


# ---------------------------------------------------------------------------
# SocketPresenter round-trip against a fake daemon
# ---------------------------------------------------------------------------

class _FakeDaemon:
    """Minimal in-process stand-in for dlc.dogegen_server: accepts one connection and
    answers each `r g b` line with `ok`, recording what it received."""

    def __init__(self):
        self.received: list[str] = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        conn, _ = self._srv.accept()
        with conn:
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.received.append(line.decode().strip())
                    conn.sendall(b"ok\n")

    def stop(self):
        try:
            self._srv.close()
        except Exception:
            pass


def test_socket_presenter_sends_code_values_and_waits_for_ack():
    daemon = _FakeDaemon()
    try:
        pres = SocketPresenter("127.0.0.1", daemon.port, settle_seconds=0.0)
        pres.show(MeasurePatch(label="white", rgb=(1023, 1023, 1023), signal=(1.0, 1.0, 1.0), bit_depth=10))
        pres.show(MeasurePatch(label="red", rgb=(1023, 0, 0), signal=(1.0, 0.0, 0.0), bit_depth=10))
        pres.close()
        # allow the daemon thread to drain
        import time
        for _ in range(50):
            if len(daemon.received) >= 2:
                break
            time.sleep(0.01)
        assert daemon.received == ["1023 1023 1023", "1023 0 0"]
    finally:
        daemon.stop()


def test_socket_presenter_raises_on_non_ack():
    # a server that answers with an error must surface, not silently accept
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"err nope\n")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        pres = SocketPresenter("127.0.0.1", port, settle_seconds=0.0)
        import pytest
        with pytest.raises(RuntimeError):
            pres.show(MeasurePatch(label="white", rgb=(1, 1, 1), signal=(1.0, 1.0, 1.0), bit_depth=8))
        pres.close()
    finally:
        srv.close()


def _drain_until(daemon, pred, tries=50):
    import time
    for _ in range(tries):
        if pred(daemon.received):
            return
        time.sleep(0.01)


def test_socket_presenter_shutdown_sends_quit():
    # A terminal run stops the persistent daemon with an explicit `quit`.
    daemon = _FakeDaemon()
    try:
        pres = SocketPresenter("127.0.0.1", daemon.port, settle_seconds=0.0)
        pres.show(MeasurePatch(label="white", rgb=(1, 1, 1), signal=(1.0, 1.0, 1.0), bit_depth=10))
        pres.shutdown_daemon()
        _drain_until(daemon, lambda r: "quit" in r)
        assert "quit" in daemon.received
    finally:
        daemon.stop()


def test_socket_presenter_close_does_not_quit():
    # A pause only drops our socket — the daemon (and its fullscreen window) must survive.
    daemon = _FakeDaemon()
    try:
        pres = SocketPresenter("127.0.0.1", daemon.port, settle_seconds=0.0)
        pres.show(MeasurePatch(label="white", rgb=(1, 1, 1), signal=(1.0, 1.0, 1.0), bit_depth=10))
        pres.close()
        _drain_until(daemon, lambda r: len(r) >= 1)
        assert "quit" not in daemon.received
    finally:
        daemon.stop()


def test_shutdown_daemon_is_safe_when_unreachable():
    # No daemon listening: shutdown_daemon must be a no-op, not raise.
    pres = SocketPresenter("127.0.0.1", 1, settle_seconds=0.0)  # port 1: nothing there
    pres.shutdown_daemon()  # must not raise
