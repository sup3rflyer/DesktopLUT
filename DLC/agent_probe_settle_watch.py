"""ONE-OFF (Claude, 2026-09-03): thermal SETTLE WATCH before a run. Reads the 92-nit drift reference every ~15 s
until the chromaticity is flat, so a run's raw stage is not measured on a panel still relaxing from heavy load
(gate run 173621: the balance moved 2-3 % in 5 min after the probe holds; the 1-min preheat cannot see a
10-min relaxation). Stops when the last WINDOW minutes have |dx| and |dy| < TOL and at least MIN minutes elapsed;
hard stop at MAX minutes. No DesktopLUT state change; parks at ref512.
Run: PYTHONPATH=src python agent_probe_settle_watch.py [--min 6] [--max 15] [--window 4] [--tol 0.0006] [--identity]
--identity: watch in the run's NEUTRAL state (calibration.enter -> identity MHC2 profile with the DIP primaries + D65,
exactly what stage_enter_neutral does since 80ca860), restoring the applied stack on exit. The applied-state
reference under-reads the drift ~3x (the stack's matrix rescales the channels), so watch where the raw stage measures.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from dlc import calibration_profile as cp
from dlc.argyll import Argyll, SpotreadRequest
from dlc.calibrate import active_correction, correction_store_path
from dlc.controller import CalibrationController
from dlc.correction_store import CorrectionStore
from dlc.measure_loop import MeasurePatch, SocketPresenter, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port

from agent_probe_common import audit_state

MON, MODE, BIT = 0, "HDR", 10
HOST, PORT = "127.0.0.1", 28930
REF = (512, 512, 512)


def log(*a):
    print(*a, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=6.0); ap.add_argument("--max", type=float, default=15.0)
    ap.add_argument("--window", type=float, default=4.0); ap.add_argument("--tol", type=float, default=0.0006)
    ap.add_argument("--period", type=float, default=15.0)
    ap.add_argument("--identity", action="store_true")
    a = ap.parse_args()
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)
    ctrl = CalibrationController.connect()
    audit_state(ctrl, MON, MODE, log)
    entered = False
    if a.identity:
        import json as _json
        from dlc.profiles import default_dummy_icc
        if ctrl.calibration_status().get("active"):
            log("[ABORT] DesktopLUT already in calibration mode"); return 1
        dip = _json.loads(Path("dip_store.json").read_text(encoding="utf-8"))["displays"][f"{profile.display_for(MON).name}:HDR"]
        npr = dip["native_primaries"]
        P = {"rx": npr["R"][0], "ry": npr["R"][1], "gx": npr["G"][0], "gy": npr["G"][1], "bx": npr["B"][0], "by": npr["B"][1]}
        ctrl.enter_neutral(MON, MODE, str(default_dummy_icc(MODE).path), reason="settle watch (identity)"); entered = True
        ctrl.set_primaries(MON, MODE, P); ctrl.set_white(MON, MODE, 0.3127, 0.3290); ctrl.apply_mhc(MON, MODE)
        time.sleep(4.0)
        audit_state(ctrl, MON, MODE, log)
    presenter = SocketPresenter(HOST, PORT, settle_seconds=1.0)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    rows = []
    t0 = time.monotonic()
    verdict = "timeout"
    try:
        while True:
            rd = measure(MeasurePatch(label="ref512", rgb=REF, signal=(0.5,) * 3, role="neutral_ref", bit_depth=BIT, seq=0))
            t = (time.monotonic() - t0) / 60.0
            if rd.xyz is not None:
                X, Y, Z = rd.xyz; s = X + Y + Z
                rows.append((t, Y, X / s, Y / s))
                log(f"   {t:5.1f} min  Y {Y:7.2f}  x {X/s:.4f}  y {Y/s:.4f}")
            win = [r for r in rows if r[0] >= t - a.window]
            if t >= a.min and len(win) >= 6:
                dx = max(r[2] for r in win) - min(r[2] for r in win)
                dy = max(r[3] for r in win) - min(r[3] for r in win)
                # linear slope over the window (x per window-minutes)
                xs = [r[0] for r in win]; ys = [r[2] for r in win]
                mx, my = statistics.mean(xs), statistics.mean(ys)
                slope = sum((u - mx) * (v - my) for u, v in zip(xs, ys)) / max(sum((u - mx) ** 2 for u in xs), 1e-9) * a.window
                if dx < a.tol and dy < a.tol and abs(slope) < a.tol:
                    verdict = f"settled at {t:.1f} min (window {a.window:.0f} min: dx {dx:.4f} dy {dy:.4f} slope {slope:+.4f}/win)"
                    break
            if t >= a.max:
                break
            time.sleep(max(0.0, a.period - 2.5))
    except KeyboardInterrupt:
        verdict = "aborted"
    finally:
        try:
            meter.close()
        except Exception:  # noqa: BLE001
            pass
        if entered:
            for attempt in range(3):
                try:
                    log(f"[restore] exit_calibration(restore_snapshot=True) attempt {attempt + 1}")
                    ctrl.exit_calibration(restore_snapshot=True); break
                except Exception as exc:  # noqa: BLE001
                    log(f"[restore] FAILED attempt {attempt + 1}: {exc}")
                    time.sleep(2.0)
            time.sleep(3.0)
            audit_state(ctrl, MON, MODE, log)
    if rows:
        log(f"[settle] start x {rows[0][2]:.4f} -> end {rows[-1][2]:.4f} (dx {rows[-1][2]-rows[0][2]:+.4f}) over {rows[-1][0]:.1f} min; Y {rows[0][1]:.2f} -> {rows[-1][1]:.2f}")
    log(f"[settle] {verdict}")
    return 0 if verdict.startswith("settled") else 2


if __name__ == "__main__":
    sys.exit(main())
