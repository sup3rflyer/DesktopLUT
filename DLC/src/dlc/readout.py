"""Minimal live readout over the measurement stream (v2-design-notes §12, §15.1).

The adaptive measurement loop (:mod:`dlc.measure_loop`) emits a durable, append-only
``measurements.ndjson``. This module is a **separate consumer** of that stream — the
*mission-control* view in the three-consumer model: it is for the **human** to follow
along (brightness + patch progress + drift), running in its own terminal while the
loop runs in another. (The LLM never tails the stream — it reads the loop's digest at
the boundary; the core reacts per-patch in real time. This renderer is neither of
those.)

Deliberately minimal and **dependency-free** — pure stdlib, no numpy, no `dlc.*`
imports — so it stays light and can tail a live file with zero coupling to the loop.
The richer Calman/ColourSpace-style CIE-triangle / gamma-tracking visualization is a
later renderer on this **same stream** (`render_html` is the seam for it).

Drift is reported **generically**: the temperamental channel is read from each
record's ``drift.coldest`` (a per-display fact carried in the data), never assumed.

Usage::

    python -m dlc.readout results/<run>/measurements.ndjson            # render a finished run
    python -m dlc.readout results/<run>/measurements.ndjson --follow   # tail a live run
    python -m dlc.readout <stream> --follow --html readout.html        # + a refreshing HTML page
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = [
    "NdjsonTailer",
    "iter_records",
    "ReadoutState",
    "render_console_line",
    "render_summary",
    "render_html",
    "main",
]


# ---------------------------------------------------------------------------
# Tailing the append-only stream
# ---------------------------------------------------------------------------

class NdjsonTailer:
    """Incrementally yields complete JSON records appended to a file.

    Byte-offset based (binary reads, so Windows CRLF translation can't desync the
    cursor); only consumes through the last newline so a half-written final line
    is never parsed; resets if the file shrinks (a new run truncated it)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._pos = 0

    def poll(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._pos:
            self._pos = 0  # truncated / rewritten → start over
        records: list[dict[str, Any]] = []
        with self.path.open("rb") as handle:
            handle.seek(self._pos)
            buf = handle.read()
        cut = buf.rfind(b"\n")
        if cut == -1:
            return records
        chunk = buf[: cut + 1]
        self._pos += len(chunk)
        for raw in chunk.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue
        return records


def iter_records(
    path: Path | str,
    *,
    follow: bool = False,
    poll_interval: float = 0.25,
    idle_timeout: Optional[float] = None,
) -> Iterator[dict[str, Any]]:
    """Yield records from an ndjson stream.

    ``follow=False`` reads what's there and stops. ``follow=True`` keeps polling
    for appended records until interrupted, or — if ``idle_timeout`` is set — until
    that many seconds pass with no new record after at least one has been seen."""

    tailer = NdjsonTailer(path)
    if not follow:
        yield from tailer.poll()
        return
    last_activity = time.monotonic()
    seen_any = False
    while True:
        new = tailer.poll()
        if new:
            seen_any = True
            last_activity = time.monotonic()
            yield from new
        elif idle_timeout is not None and seen_any and (time.monotonic() - last_activity) >= idle_timeout:
            return
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Accumulated state (computed independently from the stream — cross-checks the
# loop's own digest; the two consumers must agree)
# ---------------------------------------------------------------------------

@dataclass
class ReadoutState:
    phase: Optional[str] = None
    warm: bool = False
    warmup_reads: int = 0
    cold_channel: Optional[str] = None
    measured: set[str] = field(default_factory=set)
    total_reads: int = 0
    white_nits: Optional[float] = None
    drift_episodes: int = 0
    immediate: int = 0
    appended: int = 0
    last_drift: Optional[dict[str, Any]] = None

    @property
    def patches(self) -> int:
        return len(self.measured)

    def update(self, rec: dict[str, Any]) -> None:
        # The warm-up completion marker is a control record (the loop's honest final settle
        # verdict), not a probe read — track `warm` from it and do NOT count it as a read, so
        # total_reads stays equal to the loop's seq_counter. This is the single source of the
        # human view's warm state; the main pass starting does NOT imply the panel settled
        # (the loop proceeds past the warm-up cap with warm=False when it escalates).
        if rec.get("role") == "warmup_complete":
            self.warm = bool(rec.get("settled"))
            return

        self.total_reads += 1
        self.phase = rec.get("phase", self.phase)

        xyz = rec.get("xyz")
        if xyz:
            y = xyz[1]
            if self.white_nits is None or y > self.white_nits:
                self.white_nits = y

        role = rec.get("role")
        if role == "warmup":
            self.warmup_reads += 1
            settle = rec.get("settle") or {}
            if settle.get("warm"):
                self.warm = True
        elif role == "measurement":
            label = rec.get("label")
            if label:
                self.measured.add(label)
            # warm is tracked from the warm-up verdict (warmup_complete marker), NOT assumed
            # from the main pass starting — the loop can begin the main pass with warm=False
            # after exhausting the warm-up cap (the escalation case the cross-check exists for).
        elif role == "neutral_ref":
            drift = rec.get("drift") or {}
            self.last_drift = drift
            if drift.get("coldest"):
                self.cold_channel = drift["coldest"]
            if drift.get("repeat"):
                self.drift_episodes += 1

        disposition = rec.get("disposition")
        if disposition == "immediate":
            self.immediate += 1
        elif disposition == "appended":
            self.appended += 1


# ---------------------------------------------------------------------------
# Console rendering (ASCII-only — safe in the Windows console)
# ---------------------------------------------------------------------------

def _xy(rec: dict[str, Any]) -> Optional[tuple[float, float]]:
    yxy = rec.get("yxy")
    if yxy:
        return yxy[1], yxy[2]
    xyz = rec.get("xyz")
    if xyz and sum(xyz) > 0:
        total = sum(xyz)
        return xyz[0] / total, xyz[1] / total
    return None


def render_console_line(rec: dict[str, Any], state: ReadoutState) -> str:
    """One status line per stream record — brightness + progress + drift made
    prominent. Call after :meth:`ReadoutState.update` so totals are current."""

    rgb = rec.get("rgb") or [0, 0, 0]
    rgbs = f"rgb({rgb[0]:>3},{rgb[1]:>3},{rgb[2]:>3})"
    xyz = rec.get("xyz")
    ys = f"Y={xyz[1]:7.3f}" if xyz else "Y=   --  "
    xy = _xy(rec)
    xys = f"xy({xy[0]:.4f},{xy[1]:.4f})" if xy else "xy(  --  ,  --  )"
    role = rec.get("role")

    if role == "warmup":
        settle = rec.get("settle") or {}
        flag = "WARM" if settle.get("warm") else f"settle {settle.get('consecutive', 0)}"
        return f"[warm ] {'':6s} {rgbs} {ys} {xys}  {flag}"

    if role == "neutral_ref":
        drift = rec.get("drift") or {}
        if not drift:
            return f"[drift] {'check':6s} {rgbs} {ys}  (read failed)"
        verdict = "DRIFT -> re-measure queued" if drift.get("repeat") else "stable"
        return (
            f"[drift] {'check':6s} {rgbs} d={drift.get('max_delta', 0.0):.4f} "
            f"cold={drift.get('coldest', '?')} -> {verdict}"
        )

    # measurement / remeasure
    label = rec.get("label", "")
    read_index = rec.get("read_index", 0)
    disposition = rec.get("disposition")
    if read_index and read_index > 0:
        tag = "[conf ]"
    elif disposition == "appended" or rec.get("phase") == "remeasure":
        tag = "[redo ]"
    else:
        tag = "[meas ]"
    white = f"white {state.white_nits:.0f}nt" if state.white_nits else ""
    return f"{tag} {label:6s} {rgbs} {ys} {xys}  {state.patches} done | {white}"


def render_summary(state: ReadoutState) -> str:
    lines = [
        "--- readout summary " + "-" * 28,
        f"  phase             : {state.phase}",
        f"  warm              : {'yes' if state.warm else 'NO (cold / unsettled)'}",
        f"  warm-up reads     : {state.warmup_reads}",
        f"  patches measured  : {state.patches}",
        f"  brightness (white): {state.white_nits:.1f} nits" if state.white_nits is not None else "  brightness (white): --",
        f"  drift episodes    : {state.drift_episodes}"
        + (f"  (last d={state.last_drift.get('max_delta', 0):.4f} cold={state.last_drift.get('coldest', '?')})"
           if state.last_drift else ""),
        f"  re-measures       : immediate {state.immediate}, appended {state.appended}",
        f"  total reads       : {state.total_reads}",
        "-" * 48,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML snapshot (the seam for the future CIE-triangle / gamma-tracking view)
# ---------------------------------------------------------------------------

def render_html(records: list[dict[str, Any]], *, title: str = "DLC measurement readout",
                live: bool = False, refresh_seconds: int = 2) -> str:
    """Render the whole stream as a self-contained HTML page: a summary header +
    a table of measurement reads + the drift checks. No external assets. With
    ``live=True`` adds a meta-refresh so a browser re-loads a file the CLI rewrites
    on each poll (a server-free poor-man's live view)."""

    state = ReadoutState()
    for rec in records:
        state.update(rec)

    def td(value: Any) -> str:
        return f"<td>{value}</td>"

    rows: list[str] = []
    for rec in records:
        role = rec.get("role")
        if role not in ("measurement", "neutral_ref"):
            continue
        if rec.get("read_index", 0):
            continue  # show first reads + drift checks only (skip confirm noise)
        rgb = rec.get("rgb") or []
        xyz = rec.get("xyz")
        xy = _xy(rec)
        if role == "neutral_ref":
            drift = rec.get("drift") or {}
            cls = "drift" if drift.get("repeat") else "ok"
            verdict = "DRIFT" if drift.get("repeat") else "stable"
            label = "drift check"
            extra = f"d={drift.get('max_delta', 0):.4f} cold={drift.get('coldest', '?')} {verdict}"
        else:
            cls = "redo" if rec.get("phase") == "remeasure" else ""
            label = rec.get("label", "")
            extra = "re-measured" if rec.get("phase") == "remeasure" else ""
        rows.append(
            "<tr class='%s'>" % cls
            + td(label)
            + td(",".join(str(c) for c in rgb))
            + td(f"{xyz[1]:.2f}" if xyz else "--")
            + td(f"{xy[0]:.4f}, {xy[1]:.4f}" if xy else "--")
            + td(extra)
            + "</tr>"
        )

    meta = f"<meta http-equiv='refresh' content='{refresh_seconds}'>" if live else ""
    brightness = f"{state.white_nits:.1f} nits" if state.white_nits is not None else "--"
    drift_extra = ""
    if state.last_drift:
        drift_extra = (f" (last d={state.last_drift.get('max_delta', 0):.4f} "
                       f"cold={state.last_drift.get('coldest', '?')})")
    return f"""<!doctype html>
<html><head><meta charset='utf-8'>{meta}<title>{title}</title>
<style>
 body{{font:14px system-ui,Segoe UI,Arial;margin:1.5rem;background:#111;color:#ddd}}
 h1{{font-size:1.1rem}} .sum{{margin:.5rem 0 1rem;line-height:1.7}}
 .k{{color:#888;display:inline-block;min-width:11rem}}
 table{{border-collapse:collapse;width:100%}} th,td{{padding:.25rem .6rem;text-align:left;border-bottom:1px solid #2a2a2a}}
 th{{color:#888;font-weight:600}} tr.drift td{{color:#f6a}} tr.redo td{{color:#fc6}} tr.ok td{{color:#6c9}}
 .warmno{{color:#f6a}} .warmyes{{color:#6c9}}
</style></head><body>
<h1>{title}</h1>
<div class='sum'>
 <div><span class='k'>phase</span> {state.phase or '--'}</div>
 <div><span class='k'>warm</span> <span class='{ 'warmyes' if state.warm else 'warmno' }'>{ 'yes' if state.warm else 'NO (cold / unsettled)' }</span> &nbsp; ({state.warmup_reads} warm-up reads)</div>
 <div><span class='k'>patches measured</span> {state.patches}</div>
 <div><span class='k'>brightness (white)</span> {brightness}</div>
 <div><span class='k'>drift episodes</span> {state.drift_episodes}{drift_extra}</div>
 <div><span class='k'>re-measures</span> immediate {state.immediate}, appended {state.appended}</div>
 <div><span class='k'>total reads</span> {state.total_reads}</div>
</div>
<table><thead><tr><th>patch</th><th>RGB</th><th>Y (nits)</th><th>x, y</th><th>note</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
</body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dlc-readout",
        description="Live mission-control readout over a measurement ndjson stream.",
    )
    parser.add_argument("stream", help="path to measurements.ndjson")
    parser.add_argument("-f", "--follow", action="store_true", help="tail a live run")
    parser.add_argument("--html", default=None, help="also write/refresh an HTML page at this path")
    parser.add_argument("--poll", type=float, default=0.25, help="poll interval seconds (follow)")
    parser.add_argument("--idle-timeout", type=float, default=None,
                        help="in follow mode, stop after this many idle seconds")
    args = parser.parse_args(argv)

    path = Path(args.stream)
    state = ReadoutState()
    seen: list[dict[str, Any]] = []
    html_path = Path(args.html) if args.html else None

    def refresh_html() -> None:
        if html_path is not None:
            html_path.write_text(render_html(seen, live=args.follow), encoding="utf-8")

    if not args.follow and not path.exists():
        print(f"stream not found: {path}", file=sys.stderr)
        return 2

    try:
        for rec in iter_records(path, follow=args.follow, poll_interval=args.poll,
                                idle_timeout=args.idle_timeout):
            state.update(rec)
            seen.append(rec)
            print(render_console_line(rec, state))
            if html_path is not None and args.follow:
                refresh_html()
    except KeyboardInterrupt:
        print()  # clean line after ^C

    print(render_summary(state))
    refresh_html()
    if html_path is not None:
        print(f"wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
