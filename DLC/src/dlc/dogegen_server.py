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
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from .dogegen import DogegenPatchDisplay
from .dogegen_resolve import ResolveDogegen
from .dogegen_window import Rect, place_dogegen, resolve_monitor_rect


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
          patch_size: int = 100, monitor_rect: Optional[Rect] = None,
          auto_fullscreen: bool = True, resolve: bool = True,
          resolve_port: int = 20002) -> None:
    """Start one dogegen window and serve patch commands until ``quit`` (blocking).

    The window is borderless-fullscreened automatically onto ``monitor_rect`` (the persistent
    daemon exists for the fullscreen HDR path — fullscreen avoids mini-LED local-dimming
    contamination of a windowed patch). Composited HDR does NOT lose bit depth: the DWM composites
    HDR (and ACM SDR) in FP16, preserving 10-bit, so a forced-composition DWM hook is not a
    bit-depth gate here — only a legacy (non-ACM) 8-bit SDR desktop would need the compositor
    bypass to keep 10-bit. When ``monitor_rect`` is ``None`` the window lands on dogegen's own
    monitor (the Windows primary);
    pass the DLC target monitor's rect to hit a non-primary panel. If auto-placement fails, fall
    back to the manual Alt+Enter prompt.

    **Resolve is the DEFAULT transport** (``resolve=True``): the daemon drives dogegen over its
    **Resolve TPG protocol** (:mod:`dlc.dogegen_resolve`). ``resolve=False`` (``--stdin``) reverts
    to the legacy stdin ``window`` path, which deterministically stalls dogegen's present pipeline
    under a long continuous full-field HDR session (a display freeze at ~23 min, ~12 min with the
    DWM hook forcing composition) — reproduced across dogegen builds, no handle leak, no GPU TDR;
    keep it only for debugging. The Resolve path (what ColourSpace/DisplayCAL/Calman drive for
    hours) does not freeze; HW-validated 28 min clean. The daemon exists for the fullscreen
    10-bit/HDR path, which is exactly where the freeze bites — hence Resolve as the default. The
    orchestrator-facing line protocol on ``host:port`` is identical either way; only how the daemon
    talks to dogegen changes."""
    if resolve:
        rdg = ResolveDogegen(Path(dogegen_path), is_hdr=(mode.upper() == "HDR"),
                             bits=bit_depth, port=resolve_port)
        proc = rdg.start()                 # we listen, launch dogegen, accept its connection
        print(f"[dogegen-server] dogegen up via Resolve ({rdg.startup_command}) pid {proc.pid}",
              flush=True)
        mid = ((1 << bit_depth) - 1) // 2
        rdg.show(mid, mid, mid)            # first pattern switches dogegen into the target depth/HDR
        time.sleep(1.0)                    # let the mode switch + swapchain settle before fullscreen

        def show(r: int, g: int, b: int) -> None:
            rdg.show(r, g, b)

        def teardown() -> None:
            rdg.close()
    else:
        disp = DogegenPatchDisplay(Path(dogegen_path), mode, bit_depth=bit_depth)
        proc = disp.start()  # the D3D11 patch window appears on the primary display
        print(f"[dogegen-server] dogegen up ({disp.startup_mode}) pid {proc.pid}", flush=True)

        def show(r: int, g: int, b: int) -> None:
            disp.send(proc, f"window {patch_size} {r} {g} {b}", settle_seconds=0.0)

        def teardown() -> None:
            try:
                disp.send(proc, "quit", settle_seconds=0.1)
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass

    placed = place_dogegen(proc.pid, rect=monitor_rect, fullscreen=True) if auto_fullscreen \
        else {"ok": False, "reason": "auto-fullscreen disabled"}
    if placed.get("ok"):
        print(f"[dogegen-server] auto-fullscreened the patch window at {placed['rect']} "
              f"(hwnd {placed['hwnd']})", flush=True)
    else:
        print(f"[dogegen-server] auto-fullscreen unavailable ({placed.get('reason')})", flush=True)
        print("[dogegen-server] >>> click the patch window and press Alt+Enter to FULLSCREEN it "
              "on monitor 0, then leave it <<<", flush=True)

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
                    try:
                        data = conn.recv(4096)
                    except (ConnectionResetError, ConnectionAbortedError, OSError):
                        break                  # client died / abruptly closed (e.g. killed mid-run);
                                               # treat like a disconnect — KEEP dogegen, accept the next run
                    if not data:
                        break                  # client gone (graceful); KEEP dogegen, accept the next run
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            reply, keep = dispatch(line.decode("ascii", "ignore"), show=show)
                        except OSError as exc:
                            # The dogegen child died (broken stdin pipe) — report it cleanly to the
                            # client instead of crashing the daemon, so the caller gets a clear error
                            # (not an empty ack) and the daemon stays up to accept a restart.
                            reply, keep = (f"err dogegen unavailable: {exc}", True)
                        if reply:
                            conn.sendall((reply + "\n").encode("ascii"))
                        if not keep:
                            running = False
                            done = True
                            break
    finally:
        teardown()
        srv.close()
        print("[dogegen-server] stopped", flush=True)


def _parse_rect_arg(s: str) -> Optional[Rect]:
    """Parse a ``--monitor-rect`` value ``"x,y,w,h"`` into a rect tuple (or ``None``)."""
    try:
        parts = [int(p) for p in s.replace(" ", "").split(",")]
    except ValueError:
        return None
    return (parts[0], parts[1], parts[2], parts[3]) if len(parts) == 4 else None


def _rect_for_monitor(index: int) -> Optional[Rect]:
    """Best-effort resolve the DLC target monitor's bounds via the DesktopLUT pipe. Returns
    ``None`` (and the caller falls back to the window's current monitor) if the pipe is down."""
    try:
        from .controller import CalibrationController
        monitors = (CalibrationController.connect().query_monitors() or {}).get("monitors")
        return resolve_monitor_rect(monitors, index)
    except Exception:  # noqa: BLE001 - advisory; never block the daemon on a pipe hiccup
        return None


def main(argv=None) -> int:  # pragma: no cover - live wiring
    ap = argparse.ArgumentParser(prog="dlc-dogegen-server",
                                 description="Persistent dogegen patch-window daemon")
    ap.add_argument("--dogegen", default="third_party/dogegen/dogegen.exe")
    ap.add_argument("--mode", default="SDR")
    ap.add_argument("--bit-depth", type=int, default=8, dest="bit_depth")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=28930)
    ap.add_argument("--patch-size", type=int, default=100, dest="patch_size")
    ap.add_argument("--monitor", type=int, default=None,
                    help="DLC target monitor index; auto-fullscreen lands the window on this "
                         "panel (rect resolved over the DesktopLUT pipe). Use for a non-primary target.")
    ap.add_argument("--monitor-rect", default=None, dest="monitor_rect",
                    help='Explicit target bounds "x,y,w,h" (overrides --monitor; no pipe needed).')
    ap.add_argument("--no-auto-fullscreen", action="store_false", dest="auto_fullscreen",
                    help="Disable auto-fullscreen and prompt for a manual Alt+Enter instead.")
    ap.add_argument("--stdin", action="store_false", dest="resolve",
                    help="Drive dogegen over the legacy stdin 'window' path instead of the Resolve "
                         "TPG protocol (Resolve is the DEFAULT). The stdin path freezes dogegen's "
                         "present pipeline on long HDR sessions (~12-23 min) — use only for debugging.")
    ap.add_argument("--resolve-port", type=int, default=20002, dest="resolve_port",
                    help="TCP port the daemon listens on for dogegen's Resolve connection (default 20002).")
    a = ap.parse_args(argv)
    rect: Optional[Rect] = None
    if a.monitor_rect:
        rect = _parse_rect_arg(a.monitor_rect)
    elif a.monitor is not None:
        rect = _rect_for_monitor(a.monitor)
    serve(dogegen_path=a.dogegen, mode=a.mode, bit_depth=a.bit_depth,
          host=a.host, port=a.port, patch_size=a.patch_size,
          monitor_rect=rect, auto_fullscreen=a.auto_fullscreen,
          resolve=a.resolve, resolve_port=a.resolve_port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
