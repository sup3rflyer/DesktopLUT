"""Persistent dogegen patch-display daemon (live only).

The orchestrator's default :class:`~dlc.measure_loop.DogegenPresenter` spawns a fresh
dogegen window **per CLI invocation** — so a pause/resume respawns the window, which
flashes and loses any manual borderless-fullscreen (Alt+Enter). That makes accurate
10-bit (which *requires* a fullscreen window) impossible across the pause/resume flow,
and gives the operator no stable window to fullscreen.

This daemon fixes that: it starts **one** dogegen process (a single patch window the
operator Alt+Enters to borderless-fullscreen **once**) and serves ``window`` commands
over a local TCP socket, so the orchestrator drives that *same* persistent window across
every invocation — no respawn, no flash, fullscreen preserved. The orchestrator side is
:class:`dlc.measure_loop.SocketPresenter` (wired via ``dlc-calibrate --dogegen-server``).

Line protocol (ASCII, loopback only — this drives a local display, not a network):
  ``"<r> <g> <b>\\n"`` → show a full-field patch at those code values; reply ``"ok\\n"``
  ``"ping\\n"``        → reply ``"pong\\n"`` (liveness)
  ``"quit\\n"``        → quit dogegen + stop the daemon; reply ``"bye\\n"``
Code values are in the daemon's dogegen bit depth (``mode 8`` → 0..255, ``mode 10`` →
0..1023), so it MUST be started at the same ``--bit-depth`` the calibration run uses.

Run:  ``python -m dlc.dogegen_server --bit-depth 10 --port 28930``
"""

from __future__ import annotations

import argparse
import socket
from pathlib import Path
from typing import Callable, Tuple

from .dogegen import DogegenPatchDisplay


def dispatch(cmd: str, *, show: Callable[[int, int, int], None]) -> Tuple[str, bool]:
    """Handle one protocol line. Returns ``(reply, keep_running)``; ``reply`` empty ⇒
    send nothing. ``show(r, g, b)`` paints a full-field patch. Pure of any socket/dogegen
    detail so it is unit-testable."""
    cmd = cmd.strip()
    if not cmd:
        return ("", True)
    if cmd == "ping":
        return ("pong", True)
    if cmd == "quit":
        return ("bye", False)
    parts = cmd.split()
    if len(parts) == 3 and all(p.lstrip("-").isdigit() for p in parts):
        r, g, b = (int(p) for p in parts)
        show(r, g, b)
        return ("ok", True)
    return (f"err bad command: {cmd!r}", True)


def serve(*, dogegen_path: str, mode: str, bit_depth: int, host: str, port: int,
          patch_size: int = 100) -> None:
    """Start one dogegen window and serve patch commands until ``quit`` (blocking)."""
    disp = DogegenPatchDisplay(Path(dogegen_path), mode, bit_depth=bit_depth)
    proc = disp.start()  # the D3D11 patch window appears on the primary display
    print(f"[dogegen-server] dogegen up ({disp.startup_mode}) pid {proc.pid}", flush=True)
    print("[dogegen-server] >>> click the patch window and press Alt+Enter to FULLSCREEN it "
          "on monitor 0, then leave it <<<", flush=True)

    def show(r: int, g: int, b: int) -> None:
        disp.send(proc, f"window {patch_size} {r} {g} {b}", settle_seconds=0.0)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    print(f"[dogegen-server] listening on {host}:{port}", flush=True)
    running = True
    try:
        while running:
            conn, _ = srv.accept()             # one orchestrator invocation per connection
            with conn:
                buf = b""
                done = False
                while not done:
                    data = conn.recv(4096)
                    if not data:
                        break                  # client gone; KEEP dogegen, accept the next run
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        reply, keep = dispatch(line.decode("ascii", "ignore"), show=show)
                        if reply:
                            conn.sendall((reply + "\n").encode("ascii"))
                        if not keep:
                            running = False
                            done = True
                            break
    finally:
        try:
            disp.send(proc, "quit", settle_seconds=0.1)
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        srv.close()
        print("[dogegen-server] stopped", flush=True)


def main(argv=None) -> int:  # pragma: no cover - live wiring
    ap = argparse.ArgumentParser(prog="dlc-dogegen-server",
                                 description="Persistent dogegen patch-window daemon")
    ap.add_argument("--dogegen", default="third_party/dogegen/dogegen.exe")
    ap.add_argument("--mode", default="SDR")
    ap.add_argument("--bit-depth", type=int, default=8, dest="bit_depth")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=28930)
    ap.add_argument("--patch-size", type=int, default=100, dest="patch_size")
    a = ap.parse_args(argv)
    serve(dogegen_path=a.dogegen, mode=a.mode, bit_depth=a.bit_depth,
          host=a.host, port=a.port, patch_size=a.patch_size)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
