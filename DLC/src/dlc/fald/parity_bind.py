"""Bind the panel's measured tick parity to the CURRENT stage-3 numbering epoch — without a meter (2026-10-05).

The meter probe (``agent_motion_parity_probe.py``, a DC-balanced blink-2 block under the meter, layer OFF) measures the
panel's tick parity against WINDOWS' vblank count: the panel ticks at refreshes R with (R + P_dxgi) even. That is a
property of the DXGI timeline — it survives DesktopLUT restarts (the kernel counter runs on; the soak saw one truth
segment across an app swap) and changes only with a display event (a modeset, a power cycle, a driver restart — which
events exactly is the panel re-roll map). The hook, though, numbers refreshes with the stage-3 publisher's count n,
whose origin is new every epoch: n = R + offset, offset constant within an epoch. With the hook's convention (the clock
with parity p ticks at n with (n + p) even — shared/fald_temporal.h FaldPanelClockTicks), the epoch's parity is

    p = (P_dxgi + offset) mod 2.

``derive`` measures offset for the live epoch: Windows' count from ``tools/vblank_truth`` (a 1 x 1 static window on the
monitor, a few seconds) against the publisher's map from ``runtime.fald_vblank_status`` — the same epoch at start and
end, published throughout, the offset identical in every sample, else it refuses. ``--apply`` then sets
``runtime.fald_temporal parity=p parity_epoch=<the epoch's hex id>`` (never implicitly bound) and asserts
``parity_active``; at the next epoch change the hook drops it by itself (the fail-safe), and ``derive --apply`` binds it
again — no meter, as long as P_dxgi still holds.

Caveats (the spec doc's calibration protocol): the hook's n assumes L = +1 (a frame composed in refresh j shows at j + 1)
— the end-to-end layer check must confirm it once; P_dxgi must be re-measured after any display event.

    python -m dlc.fald.parity_bind derive --p-dxgi 1 [--monitor-point 100,100] [--hardware-id AUS322A] [--apply]"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from .vblank_soak import TRUTH_EXE, load_truth, truth_at


def epoch_parity(p_dxgi: int, offset: int) -> int:
    """The hook's parity setting for an epoch whose count n = R + offset (R = Windows' vblank count), given the panel
    ticks at R with (R + p_dxgi) even."""
    if p_dxgi not in (0, 1):
        raise ValueError("p_dxgi must be 0 or 1")
    return (p_dxgi + offset) % 2


def offsets_from(samples: list[dict], truth, period_qpc: float, freq: int) -> list[int]:
    """count_anchor - Windows' count at qpc_anchor for every published status sample that has a truth sample within 3 s."""
    out = []
    for r in samples:
        if not r.get("published"):
            continue
        got = truth_at(truth, int(r["qpc_anchor"]), period_qpc, 3.0, freq)
        if got is not None:
            out.append(int(r["count_anchor"]) - got[0])
    return out


def derive(p_dxgi: int, point: tuple[int, int], hardware_id: Optional[str], mode: str, seconds: float, apply: bool) -> dict:
    from dlc.controller import CalibrationController
    from .vblank_soak import _monitor_at
    ctrl = CalibrationController.connect()
    mon = _monitor_at(ctrl, point)
    if mon is None or (hardware_id and mon.get("hardware_id") != hardware_id):
        raise SystemExit(f"target monitor not at {point} ({(mon or {}).get('hardware_id')})")
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
    truth, freq = load_truth(tmp)
    period = samples[0]["period_ms"] * freq / 1000.0
    offs = offsets_from(samples, truth, period, freq)
    if len(offs) < max(3, len(samples) // 2) or len(set(offs)) != 1:
        raise SystemExit(f"refused: offsets not unique / too few ({sorted(set(offs))}, {len(offs)} of {len(samples)})")
    offset = offs[0]
    p = epoch_parity(p_dxgi, offset)
    res = {"monitor": idx, "mode": mode, "epoch_id": epoch, "offset": offset, "p_dxgi": p_dxgi, "parity": p,
           "samples": len(samples), "applied": False}
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
    d.add_argument("--p-dxgi", type=int, required=True, help="the panel's tick parity vs Windows' count (meter probe)")
    d.add_argument("--monitor-point", default="100,100")
    d.add_argument("--hardware-id", default="AUS322A")
    d.add_argument("--mode", default="HDR")
    d.add_argument("--seconds", type=float, default=5.0)
    d.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    pt = tuple(int(v) for v in a.monitor_point.split(","))
    res = derive(a.p_dxgi, pt, a.hardware_id, a.mode, a.seconds, a.apply)
    print(json.dumps(res, indent=1))
    return 0 if not res.get("refused") else 1


if __name__ == "__main__":
    sys.exit(main())
