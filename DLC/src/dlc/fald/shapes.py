"""Multi-rectangle frames for the FALD flows: a presenter for the persistent dogegen daemon's
``shapes`` command + the reader that pairs it with the persistent meter.

The daemon (``python -m dlc.dogegen_server``) must run in ``--stdin`` transport for these frames:
the Resolve XML transport paints only the FIRST rectangle after the background (work guide law 10),
so :func:`transport_check` is the first read of every hardware session.
"""
from __future__ import annotations

import socket
import time
from typing import Any, Callable, Optional

from ..measure_loop import MeasurePatch


def shapes_line(shapes) -> str:
    parts = [f"{int(r)} {int(g)} {int(b)} {x:.5f} {y:.5f} {cx:.5f} {cy:.5f}" for (r, g, b), (x, y, cx, cy) in shapes]
    return "shapes " + " ; ".join(parts) + "\n"


class ShapesPresenter:
    """Presenter for :func:`dlc.measure_loop.make_persistent_spotread_meter` that paints whatever
    ``pending`` holds (a shapes list) on the daemon, then dwells ``settle_seconds`` (+ the patch's bump)."""

    def __init__(self, host: str, port: int, settle_seconds: float = 2.5, timeout: float = 30.0) -> None:
        self.host, self.port, self.settle_seconds, self.timeout = host, port, settle_seconds, timeout
        self._sock: Optional[socket.socket] = None
        self.pending: Optional[list] = None

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        return self._sock

    def send_line(self, line: str) -> str:
        s = self._ensure()
        s.sendall(line.encode("ascii"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(256)
            if not chunk:
                break
            buf += chunk
        return buf.decode("ascii", "ignore").strip()

    def paint(self, shapes) -> None:
        ack = self.send_line(shapes_line(shapes))
        if not ack.startswith("ok"):
            raise RuntimeError(f"dogegen daemon refused the frame: {ack!r} (is it running with --stdin?)")

    def show(self, patch: MeasurePatch) -> None:
        if self.pending is None:
            raise RuntimeError("ShapesPresenter.show without a pending frame")
        self.paint(self.pending)
        time.sleep(self.settle_seconds + patch.settle_bump_s)

    def ping(self) -> bool:
        try:
            return self.send_line("ping\n").startswith("pong")
        except OSError:
            return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


def make_shapes_reader(presenter: ShapesPresenter, measure: Callable[[MeasurePatch], Any], bit_depth: int,
                       ) -> Callable[..., tuple[Optional[tuple], float, Optional[str]]]:
    """``read(label, shapes, field_code, settle_bump_s=0) -> (xyz | None, seconds, error)``: paint + one meter
    read; ``settle_bump_s`` is extra dwell for this frame (a dark field after bright content: zone decay)."""
    mx = float((1 << bit_depth) - 1)

    def read(label: str, shapes: list, field_code: tuple, settle_bump_s: float = 0.0) -> tuple[Optional[tuple], float, Optional[str]]:
        presenter.pending = shapes
        patch = MeasurePatch(label=label, rgb=tuple(int(c) for c in field_code),
                             signal=tuple(c / mx for c in field_code), role="measurement", bit_depth=bit_depth,
                             seq=0, settle_bump_s=float(settle_bump_s))
        t0 = time.time()
        rd = measure(patch)
        dt = time.time() - t0
        if rd.xyz is None:
            return None, dt, str(rd.error)
        return tuple(float(v) for v in rd.xyz), dt, None

    return read


def transport_check_frame(width: int, height: int, meter: tuple[int, int], max_code: int, mid_code: int) -> list:
    """[black, a far dark rect, a mid-grey 600-px square ON the meter]: the meter must read the
    third rectangle. A first-rectangle-only transport reads ≈ 0 (600 px rather than 400 so the mini-LED
    small-window crush leaves more of the request)."""
    def rect(x0, y0, w, h):
        x0 = min(max(0.0, x0), float(width)); y0 = min(max(0.0, y0), float(height))
        return (x0 / width, y0 / height, min(1.0, (width - x0) / width, w / width), min(1.0, (height - y0) / height, h / height))
    return [((0, 0, 0), (0.0, 0.0, 1.0, 1.0)),
            ((max_code // 3, max_code // 3, max_code // 3), rect(200, 200, 100, 100)),
            ((mid_code, mid_code, mid_code), rect(meter[0] - 300, meter[1] - 300, 600, 600))]
