"""Probe F — is the runtime 3D LUT actually reaching the measured patch?

Reads a few in-gamut colour patches + one grey through the dogegen daemon with the installed
HDR runtime cube ON, then with the cube CLEARED (runtime.clear_3dlut), then re-installs the
same cube and reads again. Identical reads in all three legs = the DWM hook is not applying the
cube to the fullscreen patch window (the 2026-09-03 21:11 run's verify == its training data).
Restores the cube in `finally`. Usage: python agent_probe_cube_ab.py [--restart-daemon-hint]
"""
from __future__ import annotations

import sys
import time
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
LUT_SLOT = int(sys.argv[sys.argv.index("--lut-slot") + 1]) if "--lut-slot" in sys.argv else MON   # hook routing crossed (25H2 order-match): slot 1 renders on the ProArt
HOST, PORT = "127.0.0.1", 28930
PATCHES = [
    ("grey512", (512, 512, 512)),
    ("green-ish", (276, 418, 276)),      # 0.27/0.409/0.27 — verify's worst-worsened
    ("cyan-ish", (415, 557, 557)),
    ("orange", (700, 557, 418)),
    ("blue-ish", (300, 300, 700)),
]


def log(*a):
    print(*a, flush=True)


def xy(xyz):
    s = sum(xyz)
    return (xyz[0] / s, xyz[1] / s) if s > 0 else (0.0, 0.0)


def main() -> int:
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, _info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)
    ctrl = CalibrationController.connect()
    audit = audit_state(ctrl, MON, MODE, log)
    key = f"{LUT_SLOT}:{MODE}"
    cube = ((audit["state"].get("runtime") or {}).get(key) or {}).get("cube_path")
    if not cube:
        log("[ABORT] no runtime cube installed for", key)
        return 1
    log("[probe] cube:", cube)
    presenter = SocketPresenter(HOST, PORT, settle_seconds=1.0)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)

    def read_all(tag):
        out = {}
        for label, rgb in PATCHES:
            rd = measure(MeasurePatch(label=label, rgb=rgb, signal=tuple(v / 1023 for v in rgb),
                                      role="measurement", bit_depth=BIT, seq=0))
            x, y = xy(rd.xyz)
            out[label] = (rd.xyz[1], x, y)
            log(f"  [{tag}] {label:9s} Y {rd.xyz[1]:8.2f}  x {x:.4f} y {y:.4f}")
        return out

    try:
        log("[leg 1] cube ON (as installed)")
        on1 = read_all("on1")
        ctrl.clear_3dlut(LUT_SLOT, MODE)
        time.sleep(3.0)
        log("[leg 2] cube CLEARED")
        off = read_all("off")
        ctrl.set_3dlut(LUT_SLOT, MODE, cube)
        time.sleep(3.0)
        log("[leg 3] cube RE-INSTALLED")
        on2 = read_all("on2")
        log("[delta] on1 vs off (|dx|, dY%):")
        moved = 0
        for label, _ in PATCHES:
            a, b = on1[label], off[label]
            dx = abs(a[1] - b[1]); dy = 100 * (a[0] / b[0] - 1) if b[0] else 0.0
            log(f"   {label:9s} |dx| {dx:.4f}  dY {dy:+.1f}%")
            if dx > 0.002 or abs(dy) > 2.0:
                moved += 1
        log("[verdict]", "CUBE IS APPLIED" if moved >= 2 else "CUBE HAS NO EFFECT ON THE MEASURED PATCH",
            f"({moved}/{len(PATCHES)} patches moved by > 0.002 x or 2 % Y)")
    finally:
        try:
            st = ctrl.state()
            if ((st.get("runtime") or {}).get(key) or {}).get("cube_path") != cube:
                ctrl.set_3dlut(LUT_SLOT, MODE, cube)
                log("[restore] cube re-installed")
        except Exception as exc:  # noqa: BLE001
            log("[restore] FAILED:", exc)
        try:
            presenter.present(MeasurePatch(label="park", rgb=(0, 0, 0), signal=(0.0,) * 3,
                                           role="warmup", bit_depth=BIT, seq=0))
        except Exception:  # noqa: BLE001
            pass
        try:
            meter.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
