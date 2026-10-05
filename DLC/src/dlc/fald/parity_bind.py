"""Bind the panel's measured tick parity to the CURRENT stage-3 numbering epoch — without a meter (2026-10-05).

The meter probe (``agent_motion_parity_probe.py``, a DC-balanced blink-2 block under the meter, layer OFF) measures the
panel's tick parity against WINDOWS' vblank count: the panel ticks at refreshes R with (R + P_dxgi) even, and writes a
CALIBRATION RECORD (``parity_calibration.json``): P_dxgi, the panel's EDID id + device name + mode, one (R0, qpc0) sample
of Windows' counter, the boot time and when it was measured. The hook numbers refreshes with the stage-3 publisher's
count n (origin new every epoch): n = R + offset, constant within an epoch. With the hook's convention (the clock with
parity p ticks at n with (n + p) even — shared/fald_temporal.h FaldPanelClockTicks):

    p = (P_dxgi + offset) mod 2.

``derive`` measures offset for the live epoch: Windows' count from ``tools/vblank_truth`` (a 1 x 1 static window, a few
seconds; only rows on the panel's EDID) against the publisher's map (``runtime.fald_vblank_status``), and REFUSES unless
(review 2026-10-05):
  * the monitor at the point is the record's panel (EDID id) and its live HDR state is the record's mode;
  * the same boot (QPC and Windows' counter restart at boot);
  * Windows' counter ran CONTINUOUSLY since the record (R_now - R0 = elapsed / period within 0.25 + 5 ppm: a standby
    pauses it, a modeset resets it);
  * the record is younger than --max-age-h (default 0.5 h: a power cycle that keeps the timing running, an OSD change,
    re-syncs the panel invisibly — continuity is necessary, not sufficient; anything older is an owner decision);
  * one epoch, published throughout, one offset, every anchor phase in [-0.05, 0.3] of its vblank.
``--apply`` then sets ``runtime.fald_temporal parity=p parity_epoch=<the epoch's hex id>`` (never implicitly bound) and
asserts ``parity_active`` with the epoch unchanged, else resets the parity to -1. At the next epoch change the hook drops
it by itself (the fail-safe); re-binding with the same record is fine while every check above passes.

Caveat: the hook's n assumes L = +1 — the end-to-end check (spec doc, part B) confirms it once.

    python -m dlc.fald.parity_bind derive --calibration RUN/fald/parity_calibration.json [--apply]"""
from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from .vblank_soak import TRUTH_EXE, load_truth, qpc_now, truth_at

PHASE_LO, PHASE_HI = -0.05, 0.30


def boot_wall() -> float:
    """Wall-clock time of this boot (seconds since the epoch), from the uptime."""
    up = ctypes.windll.kernel32.GetTickCount64
    up.restype = ctypes.c_ulonglong
    return time.time() - up() / 1000.0


def epoch_parity(p_dxgi: int, offset: int) -> int:
    """The hook's parity setting for an epoch whose count n = R + offset (R = Windows' vblank count), given the panel
    ticks at R with (R + p_dxgi) even."""
    if p_dxgi not in (0, 1):
        raise ValueError("p_dxgi must be 0 or 1")
    return (p_dxgi + offset) % 2


def offsets_from(samples: list[dict], truth, period_qpc: float, freq: int) -> list[tuple[int, float]]:
    """(count_anchor - Windows' count of the vblank nearest qpc_anchor, the anchor's phase from it) for every published
    status sample that has a truth sample within 3 s."""
    out = []
    for r in samples:
        if not r.get("published"):
            continue
        got = truth_at(truth, int(r["qpc_anchor"]), period_qpc, 3.0, freq)
        if got is not None:
            out.append((int(r["count_anchor"]) - got[0], got[1]))
    return out


def counter_continuous(r0: int, qpc0: int, r_now: int, qpc_now_: int, period_qpc: float, tol: float = 0.25,
                       ppm: float = 5.0) -> bool:
    """Windows' counter ran without a pause / reset between the two samples."""
    steps = (qpc_now_ - qpc0) / period_qpc
    return r_now >= r0 and abs((r_now - r0) - steps) <= tol + ppm * 1e-6 * abs(steps)


def check_record(rec: dict, mon: dict, mode: str, now_boot: float, now_wall: float, max_age_h: float) -> Optional[str]:
    """Why the record must not be bound to this monitor now (None = it may)."""
    if rec.get("p_dxgi") not in (0, 1):
        return "the record has no P_dxgi"
    if mon.get("hardware_id") != rec.get("hardware_id"):
        return f"another panel ({mon.get('hardware_id')} vs the record's {rec.get('hardware_id')})"
    if rec.get("mode") != mode:
        return f"the record was measured in {rec.get('mode')}, not {mode}"
    if bool(mon.get("hdr_active")) != (mode == "HDR"):
        return f"the monitor's live HDR state ({mon.get('hdr_active')}) is not mode {mode}"
    if abs(float(rec.get("boot_wall", 0.0)) - now_boot) > 120.0:
        return "another boot since the record (QPC and Windows' counter restarted)"
    age_h = (now_wall - float(rec.get("written_wall", 0.0))) / 3600.0
    if age_h > max_age_h:
        return f"the record is {age_h:.2f} h old (> {max_age_h} h): re-measure, or the owner raises --max-age-h"
    return None


def derive(record_path: Path, point: tuple[int, int], seconds: float, apply: bool, max_age_h: float) -> dict:
    from dlc.controller import CalibrationController
    from .vblank_soak import _monitor_at
    rec = json.loads(Path(record_path).read_text(encoding="utf-8"))
    mode = rec.get("mode", "HDR")
    ctrl = CalibrationController.connect()
    mon = _monitor_at(ctrl, point)
    if mon is None:
        raise SystemExit(f"refused: no monitor at {point}")
    why = check_record(rec, mon, mode, boot_wall(), time.time(), max_age_h)
    if why:
        raise SystemExit(f"refused: {why}")
    idx = int(mon["index"])
    tmp = Path(tempfile.mkdtemp(prefix="parity_bind_"))
    helper = subprocess.Popen([str(TRUTH_EXE), "--at", f"{point[0]},{point[1]}", "--log", str(tmp / "truth.csv"),
                               "--interval-ms", "250"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    ready = helper.stdout.readline().strip() if helper.stdout else ""
    if not ready.startswith("ready"):
        helper.kill()
        raise SystemExit(f"vblank_truth did not start: {ready!r}")
    samples = []
    try:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            samples.append(ctrl.call("runtime.fald_vblank_status", {"monitor": idx, "mode": mode}))
            time.sleep(0.25)
    finally:
        try:
            helper.stdin.write("quit\n"); helper.stdin.flush(); helper.wait(timeout=5)
        except Exception:  # noqa: BLE001
            helper.kill()
    epochs = {s.get("epoch_id") for s in samples}
    if not samples or not all(s.get("published") for s in samples) or len(epochs) != 1:
        raise SystemExit(f"refused: the map was not published throughout one epoch (epochs {sorted(map(str, epochs))})")
    epoch = epochs.pop()
    truth, freq = load_truth(tmp / "truth.csv", target_hw=rec["hardware_id"])
    if len(truth) < 4:
        raise SystemExit("refused: too few truth samples on the panel")
    period = statistics.median(s["period_ms"] for s in samples) * freq / 1000.0
    # Windows' counter ran continuously since the record (a standby pauses it, a modeset resets it)
    last = truth[-1]
    if not counter_continuous(int(rec["r0"]), int(rec["qpc0"]), last.count, last.qpc, period):
        raise SystemExit("refused: Windows' vblank counter did not run continuously since the record (standby / modeset?)")
    offs = offsets_from(samples, truth, period, freq)
    if len(offs) < max(3, len(samples) // 2) or len({o for o, _ in offs}) != 1:
        raise SystemExit(f"refused: offsets not unique / too few ({sorted({o for o, _ in offs})}, {len(offs)} of {len(samples)})")
    if not all(PHASE_LO <= ph <= PHASE_HI for _, ph in offs):
        raise SystemExit(f"refused: an anchor phase outside [{PHASE_LO}, {PHASE_HI}] ({min(p for _, p in offs):.3f}.."
                         f"{max(p for _, p in offs):.3f})")
    offset = offs[0][0]
    p = epoch_parity(int(rec["p_dxgi"]), offset)
    res = {"monitor": idx, "mode": mode, "epoch_id": epoch, "offset": offset, "p_dxgi": rec["p_dxgi"], "parity": p,
           "samples": len(samples), "record": str(record_path), "applied": False}
    if apply:
        r = ctrl.call("runtime.fald_temporal", {"monitor": idx, "mode": mode, "parity": p, "parity_epoch": epoch})
        after = ctrl.call("runtime.fald_vblank_status", {"monitor": idx, "mode": mode})
        res.update(applied=True, parity_active=bool(r.get("parity_active")), live_epoch_after=after.get("epoch_id"))
        if not r.get("parity_active") or after.get("epoch_id") != epoch:
            # the epoch moved under us (or the map went away): drop the binding — never leave a parity on a guess
            ctrl.call("runtime.fald_temporal", {"monitor": idx, "mode": mode, "parity": -1, "parity_epoch": "none"})
            res["applied"] = False
            res["refused"] = "the epoch changed or the parity is not active after setting it: parity reset to -1"
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive")
    d.add_argument("--calibration", required=True, help="the probe's parity_calibration.json")
    d.add_argument("--monitor-point", default="100,100")
    d.add_argument("--seconds", type=float, default=5.0)
    d.add_argument("--max-age-h", type=float, default=0.5)
    d.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    pt = tuple(int(v) for v in a.monitor_point.split(","))
    res = derive(Path(a.calibration), pt, a.seconds, a.apply, a.max_age_h)
    print(json.dumps(res, indent=1))
    return 0 if not res.get("refused") else 1


if __name__ == "__main__":
    sys.exit(main())
