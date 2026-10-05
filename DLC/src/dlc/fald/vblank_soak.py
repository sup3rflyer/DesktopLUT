"""Count-integrity SOAK of FALD stage 3's vblank numbering (2026-10-05).

Question: over hours of real use (load, games, video, display sleep / wake, lock screen, DesktopLUT restarts, HDR
toggles, driver restarts), does the stage-3 publisher's count of a monitor stay consistent with the display's REAL
vblank count — every change of their relation announced by a new numbering epoch (the hook then drops a calibrated
parity: the fail-safe), never silent?

Ground truth = Windows' own per-output vblank counter (DXGI SyncRefreshCount + SyncQPCTime), which Windows reports only
to a process that presents to the output: ``tools/vblank_truth`` shows a 1 x 1 black STATIC window in the monitor's
bottom-right corner and presents it once a second (no stimulus — nothing on screen changes). The publisher's live map
comes from the stage-3 pipe method ``runtime.fald_vblank_status`` (count_anchor at qpc_anchor, period, epoch id). Both
share the QPC timebase.

Invariant (per sample): offset = count_anchor - truth_count(qpc_anchor) is an integer CONSTANT within one epoch and
one truth segment (Windows' counter can reset on a mode set: that splits the truth timeline, it is not our slip). An
offset change inside an epoch is a SILENT SLIP — the failure this soak exists to catch. The anchor's phase on the
truth grid (it should sit just after a true vblank: the wakes' latency floor) is reported per epoch.

    python -m dlc.fald.vblank_soak record --out runs/vblank_soak_<date> --monitor 0 --mode HDR [--hours 8]
    python -m dlc.fald.vblank_soak analyse runs/vblank_soak_<date>

``record`` needs the STAGE-3 build of DesktopLUT running with its calibration pipe armed and the FALD layer in LED-lag
mode 3 on the monitor (that is when the publisher runs). Notes appended to ``<out>/events.txt`` (one line, any text;
the recorder stamps nothing — write "HH:MM:SS what" by hand, or ``python -m dlc.fald.vblank_soak note <out> "text"``)
are listed with the epochs in the analysis."""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DLC = Path(__file__).resolve().parents[3]
TRUTH_EXE = DLC / "tools" / "vblank_truth" / "bin" / "vblank_truth.exe"
PHASE_WARN = 0.25          # an anchor further than this (periods) from just-after-a-vblank is reported


def qpc_now() -> int:
    v = ctypes.c_longlong()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(v))
    return int(v.value)


# ------------------------------------------------------------------------------------------------------------- record
def _monitor_point(monitor: int) -> tuple[int, int]:
    from dlc.dogegen_server import _rect_for_monitor
    x, y, w, h = _rect_for_monitor(monitor)
    return int(x + min(100, w // 2)), int(y + min(100, h // 2))


def _monitor_at(ctrl, point: tuple[int, int]) -> Optional[dict]:
    """The DesktopLUT monitor containing ``point`` NOW (a display in standby can leave the desktop: the indices shift and
    ANOTHER display can take the position — 2026-10-05 the BenQ became the primary at (0, 0) while the ProArt slept)."""
    for m in (ctrl.query_monitors() or {}).get("monitors") or []:
        r = m.get("rect") or {}
        if r and r["x"] <= point[0] < r["x"] + r["width"] and r["y"] <= point[1] < r["y"] + r["height"]:
            return m
    return None


def record(out: Path, monitor: int, mode: str, hours: float, every_s: float = 1.0, truth_ms: int = 1000,
           hardware_id: Optional[str] = None, point: Optional[tuple[int, int]] = None) -> int:
    """``hardware_id`` (EDID, e.g. AUS322A) identifies the target; default = whatever sits at the monitor's point now.
    ``point`` = a desktop point on the target (default: from the monitor index)."""
    out.mkdir(parents=True, exist_ok=True)
    if not TRUTH_EXE.exists():
        raise SystemExit(f"{TRUTH_EXE} not built (tools/vblank_truth/build.cmd)")
    px, py = point if point else _monitor_point(monitor)
    if hardware_id is None:
        from dlc.controller import CalibrationController
        m0 = _monitor_at(CalibrationController.connect(), (px, py))
        hardware_id = (m0 or {}).get("hardware_id")
    if not hardware_id:
        raise SystemExit("no target hardware id (pass --hardware-id)")
    k = 0
    while (out / f"truth_{k}.csv").exists() or (k == 0 and (out / "truth.csv").exists()):
        k += 1                                      # a resumed soak: a new truth file (the analysis reads them all)
    truth_path = out / ("truth.csv" if k == 0 else f"truth_{k}.csv")
    helper = subprocess.Popen([str(TRUTH_EXE), "--at", f"{px},{py}", "--log", str(truth_path),
                               "--interval-ms", str(truth_ms)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    ready = helper.stdout.readline().strip() if helper.stdout else ""
    if not ready.startswith("ready"):
        helper.kill()
        raise SystemExit(f"vblank_truth did not start: {ready!r}")
    meta = {"monitor": monitor, "mode": mode, "hours": hours, "every_s": every_s, "truth_ms": truth_ms, "truth_ready": ready,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"), "qpc_start": qpc_now(), "point": [px, py], "truth": truth_path.name,
            "hardware_id": hardware_id}
    (out / f"meta_{k}.json" if k else out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    (out / "events.txt").touch()
    print(f"[soak] {ready}; recording to {out} for {hours} h — Ctrl+C stops", file=sys.stderr, flush=True)
    from dlc.controller import CalibrationController
    ctrl, last_err = None, None
    end = time.monotonic() + hours * 3600.0
    n = 0
    try:
        with open(out / "status.jsonl", "a", encoding="utf-8") as fh:
            while time.monotonic() < end:
                t0 = time.monotonic()
                row = {"qpc": qpc_now(), "wall": time.strftime("%H:%M:%S")}
                try:
                    if ctrl is None:
                        ctrl = CalibrationController.connect()
                    mon = _monitor_at(ctrl, (px, py))  # the target by position AND identity (EDID hardware id)
                    if mon is None or mon.get("hardware_id") != hardware_id:
                        row["absent"] = True
                        row["at_hw"] = (mon or {}).get("hardware_id")
                    else:
                        row["index"] = int(mon["index"])
                        row.update(ctrl.call("runtime.fald_vblank_status", {"monitor": int(mon["index"]), "mode": mode}))
                    last_err = None
                except Exception as exc:  # noqa: BLE001 — DesktopLUT restarts are part of the soak
                    ctrl = None
                    row["error"] = f"{type(exc).__name__}: {exc}"[:200]
                    if row["error"] != last_err:
                        print(f"[soak] {row['wall']} pipe: {row['error']}", file=sys.stderr, flush=True)
                    last_err = row["error"]
                row["qpc_after"] = qpc_now()
                fh.write(json.dumps(row) + "\n")
                n += 1
                if n % 60 == 0:
                    fh.flush()
                if helper.poll() is not None:
                    print("[soak] vblank_truth exited — stopping", file=sys.stderr, flush=True)
                    break
                time.sleep(max(0.0, every_s - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        print("[soak] stopped by Ctrl+C", file=sys.stderr, flush=True)
    finally:
        try:
            if helper.poll() is None and helper.stdin:
                helper.stdin.write("quit\n"); helper.stdin.flush()
            helper.wait(timeout=5)
        except Exception:  # noqa: BLE001
            helper.kill()
    return 0


# ------------------------------------------------------------------------------------------------------------ analyse
@dataclass
class TruthSample:
    qpc: int            # SyncQPCTime: the vblank instant
    count: int          # SyncRefreshCount


@dataclass
class Epoch:
    epoch_id: str
    first_wall: str
    last_wall: str
    first_qpc: int
    last_qpc: int
    samples: int = 0
    offsets: dict = field(default_factory=dict)        # offset -> samples
    slips: list = field(default_factory=list)           # (wall, old offset, new offset)
    phase_min: float = 1e9
    phase_max: float = -1e9
    phase_far: int = 0
    parity_active: int = 0


def load_truth(path: Path) -> tuple[list[TruthSample], int]:
    """(samples sorted by vblank time, deduplicated) and the QPC frequency. ``path`` = one truth CSV, or a run directory
    (every truth*.csv in it). Rows that went to another output than the target (out_left / out_top, newer helpers) or
    report no present are dropped."""
    freq = 10_000_000
    rows = []
    paths = sorted(Path(path).glob("truth*.csv")) if Path(path).is_dir() else [Path(path)]
    for one in paths:
        lines, target = [], None
        with open(one, encoding="ascii") as fh:
            for ln in fh:
                if ln.startswith("#"):
                    kvs = dict(kv.split("=", 1) for kv in ln[1:].split() if "=" in kv)
                    freq = int(kvs.get("qpcfreq", freq))
                    if "output_left" in kvs:
                        target = (int(kvs["output_left"]), int(kvs["output_top"]))
                else:
                    lines.append(ln)
        for r in csv.DictReader(lines):
            if r["hr"] == "absent" or int(r["hr"], 16) != 0 or int(r["sync_qpc"]) <= 0:
                continue
            if target is not None and r.get("out_left") not in (None, "") and (int(r["out_left"]), int(r["out_top"])) != target:
                continue
            rows.append(TruthSample(int(r["sync_qpc"]), int(r["sync_refresh"])))
    rows.sort(key=lambda s: s.qpc)
    out: list[TruthSample] = []
    for s in rows:
        if out and s.qpc == out[-1].qpc:
            continue
        out.append(s)
    return out, freq


def truth_segments(samples: list[TruthSample], period: float, tol: float = 0.25) -> list[list[TruthSample]]:
    """Split where Windows' counter is not consistent with time (a reset / jump on a mode set): consecutive samples whose
    count step differs from the elapsed periods by more than ``tol`` periods start a new segment."""
    segs: list[list[TruthSample]] = []
    for s in samples:
        if segs:
            a = segs[-1][-1]
            steps = (s.qpc - a.qpc) / period
            if s.count - a.count < 0 or abs((s.count - a.count) - steps) > tol:
                segs.append([s])
                continue
            segs[-1].append(s)
        else:
            segs.append([s])
    return segs


def truth_at(seg: list[TruthSample], qpc: int, period: float, max_extrap_s: float, freq: int) -> Optional[tuple[int, float]]:
    """(the true vblank count of the last vblank at or before ``qpc``, the phase of ``qpc`` after it in periods) from the
    nearest sample of the segment, or None when the nearest is more than ``max_extrap_s`` away."""
    import bisect
    qs = [s.qpc for s in seg]
    i = bisect.bisect_left(qs, qpc)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(seg) and (best is None or abs(seg[j].qpc - qpc) < abs(best.qpc - qpc)):
            best = seg[j]
    if best is None or abs(best.qpc - qpc) > max_extrap_s * freq:
        return None
    x = (qpc - best.qpc) / period
    k = int(x // 1)
    return best.count + k, x - k


def analyse(run: Path, max_extrap_s: float = 3.0) -> dict:
    truth, freq = load_truth(run)
    rows = [json.loads(ln) for ln in open(run / "status.jsonl", encoding="utf-8") if ln.strip()]
    # while the target was absent another display can sit at its position — and the truth window on IT: drop those
    # truth samples (absent stretches from the status rows, widened by the truth interval + margin)
    pad = int(3.0 * freq)
    spans, cur = [], None
    for r in rows:
        if r.get("absent"):
            cur = [r["qpc"], r["qpc"]] if cur is None else [cur[0], r["qpc"]]
        elif cur is not None:
            spans.append(cur); cur = None
    if cur is not None:
        spans.append(cur)
    if spans:
        truth = [t for t in truth if not any(a - pad <= t.qpc <= b + pad for a, b in spans)]
    periods = [r["period_ms"] for r in rows if r.get("published") and r.get("period_ms", 0) > 0]
    if not truth or not periods:
        return {"error": "no truth samples or no published map", "truth": len(truth), "rows": len(rows)}
    periods.sort()
    period = periods[len(periods) // 2] * freq / 1000.0
    segs = truth_segments(truth, period)
    seg_of = []                     # (first qpc, last qpc, segment index)
    for i, s in enumerate(segs):
        seg_of.append((s[0].qpc, s[-1].qpc, i))
    epochs: dict[str, Epoch] = {}
    order: list[str] = []
    unpublished = errors = no_truth = absent = 0
    for r in rows:
        if "error" in r:
            errors += 1
            continue
        if r.get("absent"):
            absent += 1
            continue
        if not r.get("published"):
            unpublished += 1
            continue
        qa = int(r["qpc_anchor"])
        seg_i = next((i for a, b, i in seg_of if a - max_extrap_s * freq <= qa <= b + max_extrap_s * freq), None)
        got = truth_at(segs[seg_i], qa, period, max_extrap_s, freq) if seg_i is not None else None
        if got is None:
            no_truth += 1
            continue
        tcount, phase = got
        offset = int(r["count_anchor"]) - tcount
        key = f'{r["epoch_id"]}|seg{seg_i}'
        e = epochs.get(key)
        if e is None:
            e = epochs[key] = Epoch(r["epoch_id"], r["wall"], r["wall"], r["qpc"], r["qpc"])
            order.append(key)
        if e.offsets and offset not in e.offsets:
            prev = max(e.offsets, key=lambda o: e.offsets[o])
            e.slips.append((r["wall"], prev, offset))
        e.offsets[offset] = e.offsets.get(offset, 0) + 1
        e.samples += 1
        e.last_wall, e.last_qpc = r["wall"], r["qpc"]
        e.phase_min = min(e.phase_min, phase); e.phase_max = max(e.phase_max, phase)
        if PHASE_WARN < phase < 1.0 - 0.02:
            e.phase_far += 1
        if r.get("parity_active"):
            e.parity_active += 1
    events = [ln.rstrip("\n") for ln in open(run / "events.txt", encoding="utf-8")] if (run / "events.txt").exists() else []
    out = {
        "period_ms": period * 1000.0 / freq, "truth_samples": len(truth), "truth_segments": len(segs),
        "status_rows": len(rows), "pipe_errors": errors, "unpublished": unpublished, "no_truth": no_truth, "absent": absent,
        "epochs": [{"epoch_id": epochs[k].epoch_id, "truth_segment": k.split("|seg")[1], "first": epochs[k].first_wall,
                    "last": epochs[k].last_wall, "hours": (epochs[k].last_qpc - epochs[k].first_qpc) / freq / 3600.0,
                    "samples": epochs[k].samples, "offsets": {str(o): c for o, c in epochs[k].offsets.items()},
                    "silent_slips": epochs[k].slips, "phase_min": round(epochs[k].phase_min, 4),
                    "phase_max": round(epochs[k].phase_max, 4), "phase_far": epochs[k].phase_far,
                    "parity_active_samples": epochs[k].parity_active} for k in order],
        "events": events,
    }
    out["silent_slips"] = sum(len(e["silent_slips"]) for e in out["epochs"])
    out["verdict"] = ("PASS: no silent slip" if out["silent_slips"] == 0 else f"FAIL: {out['silent_slips']} silent slip(s)")
    return out


def _print_report(rep: dict) -> None:
    if "error" in rep:
        print(rep); return
    print(f"period {rep['period_ms']:.5f} ms | truth samples {rep['truth_samples']} in {rep['truth_segments']} segment(s) | "
          f"status rows {rep['status_rows']}: pipe errors {rep['pipe_errors']}, unpublished {rep['unpublished']}, "
          f"no truth {rep['no_truth']}, target absent {rep.get('absent', 0)}")
    print(f"{'epoch id':18s} {'seg':>3s} {'first':>8s} {'last':>8s} {'hours':>6s} {'samples':>7s}  offsets / phase / slips")
    for e in rep["epochs"]:
        print(f"{e['epoch_id']:18s} {e['truth_segment']:>3s} {e['first']:>8s} {e['last']:>8s} {e['hours']:6.2f} {e['samples']:7d}  "
              f"{e['offsets']} phase {e['phase_min']:+.3f}..{e['phase_max']:+.3f} far {e['phase_far']}"
              + (f" | SLIPS {e['silent_slips']}" if e["silent_slips"] else "")
              + (f" | parity active {e['parity_active_samples']}" if e["parity_active_samples"] else ""))
    if rep["events"]:
        print("events:"); [print("  " + ev) for ev in rep["events"]]
    print(rep["verdict"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record"); r.add_argument("--out", required=True); r.add_argument("--monitor", type=int, default=0)
    r.add_argument("--mode", default="HDR"); r.add_argument("--hours", type=float, default=8.0)
    r.add_argument("--every-s", type=float, default=1.0)
    r.add_argument("--hardware-id", default=None, help="EDID id of the target (default: the monitor at its point now)")
    r.add_argument("--point", default=None, help="x,y desktop point on the target (default: from --monitor)")
    a = sub.add_parser("analyse"); a.add_argument("run")
    nt = sub.add_parser("note"); nt.add_argument("run"); nt.add_argument("text")
    args = ap.parse_args(argv)
    if args.cmd == "record":
        pt = tuple(int(v) for v in args.point.split(",")) if args.point else None
        return record(Path(args.out), args.monitor, args.mode, args.hours, args.every_s, hardware_id=args.hardware_id,
                      point=pt)
    if args.cmd == "note":
        with open(Path(args.run) / "events.txt", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {args.text}\n")
        return 0
    rep = analyse(Path(args.run))
    (Path(args.run) / "analysis.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    _print_report(rep)
    return 0 if rep.get("silent_slips", 1) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
