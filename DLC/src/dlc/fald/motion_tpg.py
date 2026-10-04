"""Client for the motion TPG (``tools/motion_tpg``) — frame-exact moving stimuli for the FALD temporal measurements.

The TPG renders a :class:`dlc.fald.motion.Scene` analytically with the simulator's coverage rules (one scene definition
for the frozen prediction and the filmed stimulus), presents one content frame per ``cadence`` refreshes from a
flip-model FP16 scRGB swapchain (composed by the DWM, so the FALD layer in the DWM hook sees it), and logs every Present
with DXGI's frame statistics: :func:`presented_schedule` turns that log into the PANEL REFRESH each content frame
reached the screen on — slips included, and the refresh index whose parity the panel's dimming tick is locked to
(stage 3 needs it; ``fald-model-current.md`` §6.1 item 5).

Display hygiene (FALD probe rules): the TPG parks on a dim uniform grey (``park_nits``, default 2) whenever it is not
playing; start and end parked. It covers its rect TOP-MOST — on the PA32UCXR (monitor 0, the owner's MAIN display) that
is the owner's working screen: a hardware session is the owner's call, not a background step.

Typical use::

    with MotionTPG(rect=(0, 0, 3840, 2160), log_path=run / "presents.csv") as tpg:
        tpg.load(scene)
        tpg.play(cycles=6)
        tpg.park()
    sched = presented_schedule(read_present_log(run / "presents.csv"))
"""
from __future__ import annotations

import csv
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .motion import Scene

EXE_DEFAULT = Path(__file__).resolve().parents[3] / "tools" / "motion_tpg" / "bin" / "motion_tpg.exe"


def scene_text(scene: Scene) -> str:
    """The TPG's scene file for ``scene`` (keys: name, bg, pre, move, post, cadence, rect / disc, sync, code, digits)."""
    f = lambda v: f"{float(v):.6f}"
    note = " ".join(str(scene.note).split()).encode("ascii", "replace").decode("ascii")   # one line, ASCII
    name = "".join(c if (c.isascii() and (c.isalnum() or c in "_-.")) else "_" for c in scene.name) or "scene"
    lines = [f"# {note}" if note else "# motion scene",
             f"name {name}", "bg " + " ".join(f(v) for v in scene.bg),
             f"pre {int(scene.pre)}", f"move {int(scene.move)}", f"post {int(scene.post)}",
             "cadence " + " ".join(str(int(c)) for c in scene.cadence)]
    for s in scene.shapes:
        blink = f" blink {int(s.blink)} {int(s.blink_phase)}" if s.blink > 0 else ""
        if s.kind == "rect":
            lines.append("rect " + " ".join(f(v) for v in (s.x, s.y, s.w, s.h, s.vx, s.vy, *s.nits)) + blink)
        elif s.kind == "disc":
            lines.append("disc " + " ".join(f(v) for v in (s.x, s.y, s.r, s.vx, s.vy, *s.nits)) + blink)
        else:
            raise ValueError(f"shape kind {s.kind!r} is not supported by the TPG yet")
    if scene.sync:
        x0, y0, w, h, lo, hi = scene.sync
        lines.append("sync " + " ".join(f(v) for v in (x0, y0, w, h, lo, hi)))
    if scene.code:
        x0, y0, cell, bits, lo, hi = scene.code
        lines.append(f"code {f(x0)} {f(y0)} {f(cell)} {int(bits)} {f(lo)} {f(hi)}")
    if scene.digits:
        x0, y0, h, n, lo, hi = scene.digits
        lines.append(f"digits {f(x0)} {f(y0)} {f(h)} {int(n)} {f(lo)} {f(hi)}")
    return "\n".join(lines) + "\n"


class TPGError(RuntimeError):
    pass


class MotionTPG:
    """One TPG process. ``rect`` = (x, y, w, h) in physical desktop pixels (the target monitor's bounds for a real run;
    a smaller window shows the scene in miniature — ``scene_width`` scene px across it — for pacing self-tests).
    ``repeat_presents``: re-present every refresh (the layer re-runs every refresh) instead of one Present(k) per content
    frame."""

    def __init__(self, rect: Sequence[int], log_path: Path, hdr: bool = True, exe: Optional[Path] = None,
                 park_nits: float = 2.0, repeat_presents: bool = False, scene_width: float = 3840.0,
                 sdr_white: float = 120.0, sdr_gamma: float = 2.2, allow_sdr_desktop: bool = False):
        self.rect = tuple(int(v) for v in rect)
        self.hdr, self.allow_sdr_desktop = bool(hdr), bool(allow_sdr_desktop)
        self.log_path = Path(log_path)
        self.exe = Path(exe) if exe else EXE_DEFAULT
        self.args = ["--rect", ",".join(str(v) for v in self.rect), "--log", str(self.log_path),
                     "--park", f"{park_nits:.6f}", "--scene-width", f"{scene_width:.3f}"]
        self.args += ["--hdr"] if hdr else ["--sdr", "--sdr-white", f"{sdr_white:.3f}", "--sdr-gamma", f"{sdr_gamma:.4f}"]
        if repeat_presents:
            self.args.append("--repeat-presents")
        self.proc: Optional[subprocess.Popen] = None
        self.ready: dict = {}
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._tmp = tempfile.TemporaryDirectory(prefix="motion_tpg_")

    # ------------------------------------------------------------------ process
    def start(self, timeout: float = 15.0) -> dict:
        if not self.exe.exists():
            raise TPGError(f"{self.exe} not built (run tools/motion_tpg/build.cmd)")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([str(self.exe), *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._reader, daemon=True).start()
        line = self._expect(("ready", "fatal"), timeout)
        if line.startswith("fatal"):
            raise TPGError(line)
        self.ready = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
        if self.hdr and self.ready.get("hdr") != "1" and not self.allow_sdr_desktop:
            self.close()
            raise TPGError(f"HDR stimulus requested but the output is not in HDR ({line}); scRGB above 80 nit would clip")
        return self.ready

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\r\n"))
        self._lines.put("eof")

    def _expect(self, prefixes: tuple, timeout: float) -> str:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TPGError(f"timeout waiting for {prefixes}")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                continue
            if line == "eof":
                raise TPGError("the TPG exited")
            if line.startswith(prefixes) or line.startswith(("err", "fatal")):
                return line

    def send(self, cmd: str, expect: tuple = ("ok",), timeout: float = 10.0) -> str:
        if self.proc is None or self.proc.stdin is None:
            raise TPGError("not started")
        self.proc.stdin.write(cmd + "\n"); self.proc.stdin.flush()
        line = self._expect(expect, timeout)
        if line.startswith(("err", "fatal")):
            raise TPGError(f"{cmd!r}: {line}")
        return line

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.send("quit", timeout=5.0)
            except TPGError:
                pass
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self._tmp.cleanup()

    def __enter__(self) -> "MotionTPG":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ commands
    def load(self, scene: Scene) -> str:
        path = Path(self._tmp.name) / f"{scene.name}.scene"
        path.write_text(scene_text(scene), encoding="ascii")
        return self.send(f"load {path}")

    def play(self, cycles: int = 1, timeout: Optional[float] = None, refresh_hz: Optional[float] = None,
             lock: bool = False) -> tuple[int, int]:
        """Play the loaded scene ``cycles`` times and wait until its last present was SUBMITTED; returns the (first, last)
        present numbers of the play (the log's ``present`` column — when each reached the screen is in the log). The
        finished play holds its last frame until :meth:`park`. ``lock``: blinking shapes follow the TARGET REFRESH count
        (the TPG's own vblank index + its learned offset to DXGI's PresentRefreshCount) — a late frame then mis-shows for
        one refresh instead of shifting a toggle's phase for the rest of the play."""
        self.send(f"play {int(cycles)}" + (" lock" if lock else ""), expect=("ok play",))
        if timeout is None:
            hz = refresh_hz or float(self.ready.get("refresh", 60.0) or 60.0)
            timeout = 10.0 + 4.0 * cycles * 600 / max(hz, 1.0)
        line = self._expect(("done play",), timeout)
        if line.startswith(("err", "fatal")):
            raise TPGError(line)
        a, b = line.split("presents=")[1].split("..")
        return int(a), int(b)

    def park(self, nits: Optional[float] = None) -> str:
        return self.send("park" if nits is None else f"park {float(nits):.6f}", expect=("ok park",))

    def status(self) -> str:
        return self.send("status", expect=("ok status",))


# ---------------------------------------------------------------------- the present log
@dataclass
class Present:
    present: int
    qpc: int                 # QPC ticks at the Present call
    vblank_qpc: int          # QPC ticks when the vblank wait before it returned (present phase = qpc - vblank_qpc)
    play: int
    cycle: int
    content: int
    sub: int
    interval: int            # vblanks the TPG waited before its next present (the frame's requested hold)
    dxgi_count: int          # DXGI's PresentCount of this present (GetLastPresentCount right after it)
    refresh: Optional[int]   # the vblank index it reached the screen on (DXGI PresentRefreshCount), None = unknown
    inferred: bool = False   # refresh filled in by :func:`infer_refreshes` (unambiguous gap), not reported by DXGI
    vblank_n: Optional[int] = None    # the TPG's own vblank index at submit (arbitrary origin)
    r_target: Optional[int] = None    # the DXGI refresh the TPG aimed this frame at (-1 / None = offset not learned yet)


def read_present_log(path: Path) -> tuple[list[Present], dict]:
    """Parse the TPG's ``--log`` CSV and its ``<log>.stats.csv`` (frame statistics polled at every vblank); returns
    (presents, header) with ``refresh`` = the PresentRefreshCount DXGI reported for that present (None if never
    sampled — see :func:`infer_refreshes`)."""
    path = Path(path)
    header, lines = {}, []
    with open(path, newline="", encoding="ascii") as fh:
        for ln in fh:
            if ln.startswith("#"):
                header.update(kv.split("=", 1) for kv in ln[1:].split() if "=" in kv)
            else:
                lines.append(ln)
    rows = list(csv.DictReader(lines))
    seen: dict[int, int] = {}
    for r in rows:
        if int(r["st_ok"]) and int(r["st_present_count"]):
            seen.setdefault(int(r["st_present_count"]), int(r["st_present_refresh"]))
    stats = path.with_name(path.name + ".stats.csv")
    if stats.exists():
        with open(stats, newline="", encoding="ascii") as fh:
            for r in csv.DictReader(fh):
                seen.setdefault(int(r["st_present_count"]), int(r["st_present_refresh"]))
    opt = lambda r, k: (int(r[k]) if r.get(k) not in (None, "") and int(r[k]) >= 0 else None)
    out = [Present(int(r["present"]), int(r["qpc"]), int(r.get("vblank_qpc") or 0), int(r["play"]), int(r["cycle"]),
                   int(r["content"]), int(r["sub"]), int(r["interval"]), int(r["last_present_count"]),
                   seen.get(int(r["last_present_count"])), False, opt(r, "vblank_n"), opt(r, "r_target"))
           for r in rows]
    return out, header


def infer_refreshes(presents: Sequence[Present]) -> int:
    """Fill unknown refreshes where the gap is UNAMBIGUOUS: between two reported presents whose refresh difference equals
    the sum of the requested intervals in between (no slip inside the gap), every present in it lands at the cumulative
    interval. Gaps that do not add up stay None (a slip happened somewhere inside; which frame slipped is unknown).
    Returns the number filled."""
    known = [i for i, p in enumerate(presents) if p.refresh is not None]
    filled = 0
    for a, b in zip(known, known[1:]):
        if b - a < 2:
            continue
        span = sum(presents[j].interval for j in range(a, b))
        if presents[b].refresh - presents[a].refresh != span:
            continue
        r = presents[a].refresh
        for j in range(a + 1, b):
            r += presents[j - 1].interval
            presents[j].refresh, presents[j].inferred = r, True
            filled += 1
    return filled


def presented_schedule(presents: Sequence[Present], play: Optional[int] = None) -> dict:
    """For one play (default: the last), the refresh each present reached the screen on and how long it stayed:
    rows ``(present, cycle, content, refresh, held, inferred)`` — ``held`` = refreshes until the NEXT present appeared
    (None if either is unknown); ``slips`` = presents whose ``held`` differs from the requested ``interval`` (a missed or
    doubled refresh: the stimulus geometry is still exact, only its timing moved); ``resolved`` / ``reported`` = share
    of presents with a known refresh after / before inference. Call :func:`infer_refreshes` first to fill gaps. A
    content frame's tick parity is ``refresh % 2`` of its present (relative to the vblank counter's origin — the
    one-bit calibration maps it to the panel's early / late class)."""
    plays = sorted({p.play for p in presents if p.play > 0})
    if not plays:
        return {"presents": [], "slips": [], "resolved": 0.0, "reported": 0.0}
    pid = plays[-1] if play is None else play
    idx = [i for i, p in enumerate(presents) if p.play == pid]
    held = []
    for i in idx:
        r1 = presents[i].refresh
        r2 = presents[i + 1].refresh if i + 1 < len(presents) else None
        held.append(None if (r1 is None or r2 is None) else r2 - r1)
    seq = [presents[i] for i in idx]
    slips = [(p.present, p.content, p.interval, h) for p, h in zip(seq, held) if h is not None and h != p.interval]
    return {"play": pid,
            "presents": [(p.present, p.cycle, p.content, p.refresh, h, p.inferred) for p, h in zip(seq, held)],
            "slips": slips, "resolved": sum(p.refresh is not None for p in seq) / len(seq),
            "reported": sum(p.refresh is not None and not p.inferred for p in seq) / len(seq)}
