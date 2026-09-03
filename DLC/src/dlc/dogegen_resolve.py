"""Drive dogegen via its "Resolve" TPG protocol (dogegen is the TCP client; we are the server).

dogegen's stdin/console ``window`` path stalls the D3D present pipeline under a long, continuous
full-field HDR session — a deterministic display freeze at ~23 min (DWM hook inactive; ~12 min
with the hook forcing composition), reproduced identically across dogegen builds, with no GDI/USER
handle leak and no GPU TDR. ColourSpace / DisplayCAL / Calman drive dogegen for *hours* without
freezing by using this **Resolve** protocol instead of the manual ``window`` path, so DLC uses it
for long HDR runs.

Protocol (read straight from dogegen ``main.cpp`` ``StartResolve``):
  * dogegen, launched with ``resolve_hdr <ip>:<port>`` (or ``resolve_sdr``), CONNECTS to us as a
    TCP **client** — so we must ``bind``/``listen`` BEFORE launching it (it connects once, with no
    retry).
  * Per patch we send a **4-byte big-endian int32 length** prefix, then that many bytes of XML.
    A length ``<= 0`` tells dogegen to close the connection. dogegen sends **nothing back**.
  * The XML is substring-parsed: ``<color red green blue bits/>`` (RGB as integer code values in
    ``0..2**bits-1``; ``bits`` must be 8 or 10) and ``<geometry x y cx cy/>`` in normalized [0,1].
    **Full-field == x=0 y=0 cx=1 cy=1** (``main.cpp`` line ~770), which overrides any window-size
    setting. Attribute order matters: ``getAttr`` takes the first ``name="`` match, so the
    standalone ``x``/``y`` must precede ``cx``/``cy`` (otherwise ``x`` matches inside ``cx``).

The window still needs borderless-fullscreen for HDR (to avoid mini-LED local-dimming
contamination of a windowed patch; HDR composites in FP16, so bit depth is already preserved) — that is handled
exactly as on the stdin path, via :func:`dlc.dogegen_window.place_dogegen` on ``proc.pid`` once
the first pattern has switched dogegen into the right bit depth.
"""

from __future__ import annotations

import socket
import struct
import subprocess
from pathlib import Path
from typing import Optional


def resolve_patch_xml(r: int, g: int, b: int, *, bits: int = 10,
                      geometry: Optional[tuple[float, float, float, float]] = None) -> bytes:
    """Resolve calibration XML for one patch (ASCII bytes, unframed).

    ``r``/``g``/``b`` are integer code values in ``0..2**bits-1``. ``geometry`` is
    ``(x, y, cx, cy)`` in normalized [0,1] — the patch window dogegen draws (the rest of
    the render target stays black). ``None`` = full-field (x=0 y=0 cx=1 cy=1), the
    mini-LED default; a WINDOWED patch is what an OLED needs (ABL dims a large bright
    field mid-read). Attribute order is deliberate — see the module docstring."""
    if geometry is None:
        xml = (
            "<calibration>"
            f'<color red="{int(r)}" green="{int(g)}" blue="{int(b)}" bits="{int(bits)}"/>'
            '<geometry x="0" y="0" cx="1" cy="1"/>'
            "</calibration>"
        )
        return xml.encode("ascii")
    # Windowed patch — TWO rectangles, painter's order (HW-verified 2026-09-02 on the
    # LG C6): dogegen draws rectangles sequentially, later on top, and does NOT clear
    # the frame to black on its own (a lone windowed rect leaves garbage — a green
    # field on the YCbCr link — behind it). So paint a full-field black background
    # first, then the patch window.
    x, y, cx, cy = (float(v) for v in geometry)
    xml = (
        "<calibration><shapes>"
        f'<rectangle><color red="0" green="0" blue="0" bits="{int(bits)}"/>'
        '<geometry x="0" y="0" cx="1" cy="1"/></rectangle>'
        f'<rectangle><color red="{int(r)}" green="{int(g)}" blue="{int(b)}" bits="{int(bits)}"/>'
        f'<geometry x="{x:g}" y="{y:g}" cx="{cx:g}" cy="{cy:g}"/></rectangle>'
        "</shapes></calibration>"
    )
    return xml.encode("ascii")


def frame(payload: bytes) -> bytes:
    """Prefix ``payload`` with dogegen's 4-byte big-endian (network-order) int32 length."""
    return struct.pack("!i", len(payload)) + payload


class ResolveDogegen:
    """Own one dogegen process driven over the Resolve TPG socket.

    Lifecycle: :meth:`start` (listen → launch → accept), :meth:`show` per patch, :meth:`close`.
    ``proc.pid`` is exposed after :meth:`start` so the caller can borderless-fullscreen the render
    window with :func:`dlc.dogegen_window.place_dogegen`."""

    def __init__(self, executable, *, is_hdr: bool = True, bits: int = 10,
                 host: str = "127.0.0.1", port: int = 20002,
                 connect_timeout: float = 15.0, io_timeout: float = 10.0,
                 geometry: Optional[tuple[float, float, float, float]] = None) -> None:
        self.executable = Path(executable)
        self.is_hdr = is_hdr
        self.bits = bits
        self.geometry = geometry          # (x, y, cx, cy) normalized; None = full-field
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout
        self.proc: Optional[subprocess.Popen] = None
        self._srv: Optional[socket.socket] = None
        self._conn: Optional[socket.socket] = None

    @property
    def startup_command(self) -> str:
        """The single argv command string handed to dogegen (one InputReader command line)."""
        return f"resolve_{'hdr' if self.is_hdr else 'sdr'} {self.host}:{self.port}"

    def start(self) -> subprocess.Popen:
        """Listen, launch dogegen pointed at us, and accept its connection. Returns the process."""
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(1)
        self._srv.settimeout(self.connect_timeout)
        # dogegen connects once with no retry, so we are already listening above.
        self.proc = subprocess.Popen(
            [str(self.executable), self.startup_command],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        try:
            self._conn, _ = self._srv.accept()
        except socket.timeout as exc:  # dogegen never connected (bad launch / port taken)
            self.close()
            raise RuntimeError(f"dogegen did not connect on {self.host}:{self.port} "
                               f"within {self.connect_timeout:g}s") from exc
        self._conn.settimeout(self.io_timeout)
        return self.proc

    def show(self, r: int, g: int, b: int) -> None:
        """Display a patch at code values ``(r, g, b)`` in the configured geometry
        (full-field unless a window was given)."""
        if self._conn is None:
            raise RuntimeError("ResolveDogegen is not started")
        self._conn.sendall(frame(resolve_patch_xml(r, g, b, bits=self.bits,
                                                   geometry=self.geometry)))

    def close(self) -> None:
        """Tell dogegen to close (length 0), drop the sockets, and stop the process. Idempotent."""
        if self._conn is not None:
            try:
                self._conn.sendall(struct.pack("!i", 0))  # dataLen <= 0 ⇒ dogegen closes
            except OSError:
                pass
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 - escalate to kill
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None
