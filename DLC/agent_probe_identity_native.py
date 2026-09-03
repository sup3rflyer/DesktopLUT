"""ONE-OFF HW probe D (Claude, 2026-09-03): (1) does the identity profile's DECLARED colorant set matter, and
(2) the first TRUE-NATIVE sweep of the PA32UCXR (through an identity MHC2 profile), compared to last night's raw.

Probe A proved Windows keeps the last associated MHC2 transform, so 'neutral' must be an associated identity
profile. Its ICM still declares colorants (set_primaries P). If the compositor uses them in HDR, an identity profile
with Rec.2020 colorants and one with the panel's native colorants render DIFFERENTLY; if not, they are identical.
This decides which P enter-neutral must use.

States (3 reads each: ref512, g785, g837, g1023, r837, g837green, b837):
  applied     : today's stack (baseline + restoration check)
  I_native    : set_primaries(DIP native primaries) + set_white(D65) + apply  (matrix I)
  I_2020      : set_primaries(Rec.2020) + set_white(D65) + apply             (matrix I)
  I_native2   : repeat of I_native (thermal control)
  sweep       : in I_native2: 21 greys (PQ-even 0.5..1823 nits) + R/G/B at 4 levels, 1 read each
  applied2    : restored, re-read
Writes runs/probes/<ts>_identity_native/. Restores the stack in finally (3 attempts). Audits state first.
Run: PYTHONPATH=src python agent_probe_identity_native.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dlc import calibration_profile as cp
from dlc._pq import oetf_norm
from dlc.argyll import Argyll, SpotreadRequest
from dlc.calibrate import active_correction, correction_store_path
from dlc.controller import CalibrationController
from dlc.correction_store import CorrectionStore
from dlc.measure_loop import MeasurePatch, SocketPresenter, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port
from dlc.profiles import default_dummy_icc

from agent_probe_common import audit_state, require

MON, MODE, BIT = 0, "HDR", 10
HOST, PORT = "127.0.0.1", 28930
OUT = Path("runs/probes") / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_identity_native")
D65 = (0.3127, 0.3290)
P_2020 = {"rx": 0.708, "ry": 0.292, "gx": 0.170, "gy": 0.797, "bx": 0.131, "by": 0.046}
REF = ("ref512", (512, 512, 512))
PATCHES = [("g785", (785, 785, 785)), ("g837", (837, 837, 837)), ("g1023", (1023, 1023, 1023)),
           ("r837", (837, 0, 0)), ("grn837", (0, 837, 0)), ("b837", (0, 0, 837))]
READS = 3
SETTLE_S = 1.7


def log(*a):
    print(*a, flush=True)


def xy(X):
    s = X[0] + X[1] + X[2]
    return (X[0] / s, X[1] / s) if s > 1e-9 else (0.0, 0.0)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    disp = profile.display_for(MON)
    ccmx = active_correction(profile, store, disp.name)
    log(f"[setup] spotread port={port} ok={info.get('ok')} ccmx={ccmx}")
    # DIP native primaries (06-19 characterize) as the "native" colorant set
    dip = json.loads(Path("dip_store.json").read_text(encoding="utf-8"))["displays"][f"{disp.name}:HDR"]
    npr = dip["native_primaries"]
    P_native = {"rx": npr["R"][0], "ry": npr["R"][1], "gx": npr["G"][0], "gy": npr["G"][1], "bx": npr["B"][0], "by": npr["B"][1]}
    log(f"[setup] P_native(DIP)={P_native}")

    ctrl = CalibrationController.connect()
    a = audit_state(ctrl, MON, MODE, log)
    if not require(a, log=log):
        log("[ABORT] fix the state above first"); return 1

    presenter = SocketPresenter(HOST, PORT, settle_seconds=SETTLE_S)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    results: list[dict] = []

    def read(state, label, cv, n=READS):
        sig = tuple(c / 1023 for c in cv)
        rows = []
        for _ in range(n):
            rd = measure(MeasurePatch(label=label, rgb=cv, signal=sig, role="measurement", bit_depth=BIT, seq=0))
            t = datetime.now().isoformat(timespec="milliseconds")
            if rd.xyz is None:
                log(f"   {state:10s} {label:7s} NO READ"); continue
            X, Y, Z = rd.xyz; x, y = xy(rd.xyz)
            rows.append({"state": state, "label": label, "cv": list(cv), "t": t, "Y": Y, "x": x, "y": y, "xyz": [X, Y, Z]})
            log(f"   {state:10s} {label:7s} cv={cv}  Y={Y:8.2f}  xy=({x:.4f},{y:.4f})  {t[11:23]}")
        results.extend(rows)
        return rows

    def block(state):
        audit_state(ctrl, MON, MODE, log)
        read("prime-" + state, "g837", (837, 837, 837), n=1)
        read(state, *REF, n=2)
        for label, cv in PATCHES:
            read(state, label, cv)
        read(state, *REF, n=1)

    def identity(P):
        ctrl.set_primaries(MON, MODE, P)
        ctrl.set_white(MON, MODE, *D65)
        ctrl.apply_mhc(MON, MODE)
        time.sleep(4.0)

    entered = False
    try:
        block("applied")
        ctrl.enter_neutral(MON, MODE, str(default_dummy_icc(MODE).path), reason="probe D identity/native")
        entered = True
        time.sleep(2.0)
        identity(P_native); block("I_native")
        identity(P_2020); block("I_2020")
        identity(P_native); block("I_native2")
        # ---- native sweep through the identity profile --------------------------------
        log("[sweep] greys + primaries through I_native")
        s_lo, s_hi = oetf_norm(0.5 / 10000), oetf_norm(1823.0 / 10000)
        greys = sorted({int(round((s_lo + (s_hi - s_lo) * i / 20) * 1023)) for i in range(21)})
        for cv in greys:
            read("sweep", f"g{cv}", (cv, cv, cv), n=1)
        for lvl in (240, 420, 620, 837):
            read("sweep", f"r{lvl}", (lvl, 0, 0), n=1)
            read("sweep", f"grn{lvl}", (0, lvl, 0), n=1)
            read("sweep", f"b{lvl}", (0, 0, lvl), n=1)
        read("sweep", *REF, n=2)
    finally:
        try:
            if entered:
                for attempt in range(3):
                    try:
                        log(f"[restore] exit_calibration(restore_snapshot=True) attempt {attempt + 1}")
                        ctrl.exit_calibration(restore_snapshot=True); break
                    except Exception as exc:  # noqa: BLE001
                        log(f"[restore] FAILED attempt {attempt + 1}: {exc}")
                        if attempt == 2:
                            log("[restore] *** GAVE UP - display may be left on the identity profile. Owner: check state()/GUI. ***")
                        else:
                            time.sleep(2.0)
                time.sleep(4.0)
                block("applied2")
        finally:
            try:
                read("park", "park512", (512, 512, 512), n=1)
            except Exception:  # noqa: BLE001
                pass
            try:
                meter.close()
            except Exception:  # noqa: BLE001
                pass
            (OUT / "reads.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
            log(f"[done] {len(results)} reads -> {OUT / 'reads.json'}")

    def med(state, label, k):
        v = sorted(r[k] for r in results if r["state"] == state and r["label"] == label)
        return v[len(v) // 2] if v else float("nan")
    states = ["applied", "I_native", "I_2020", "I_native2", "applied2"]
    log("\n[summary] per state: ref x/Y, then each patch x RELATIVE to the block's ref (drift-corrected) / nits")
    for st in states:
        rx, ry = med(st, "ref512", "x"), med(st, "ref512", "Y")
        cells = " ".join(f"{l}:{med(st, l, 'x') - rx:+.4f}/{med(st, l, 'Y'):.0f}" for l, _ in PATCHES)
        log(f"  {st:10s} ref {rx:.4f}/{ry:5.1f} | {cells}")
    log("[verdict] I_native vs I_2020 on ref/g837/primaries: equal within 0.001 x and 1% Y => colorants irrelevant in HDR;"
        " different => the compositor uses the ICC colorants and enter-neutral must declare the deployed set.")
    log("[sweep] cv  Y  x  y   (native through I_native; compare with runs/.../measurements/raw.ndjson)")
    for r in results:
        if r["state"] == "sweep":
            log(f"   {r['label']:8s} Y {r['Y']:9.3f}  x {r['x']:.4f} y {r['y']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
