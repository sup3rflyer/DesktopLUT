"""ONE-OFF HW probe C (Claude, 2026-09-03): dense ASCENDING grey ramp = direct measurement of the
"red stripes" the owner sees on `Gray Ramp 1000nit.mp4` (PA32UCXR, applied HDR stack).

Data behind it (run 20260903_030752): the RAW (enter-neutral = un-profiled) grey ramp alternates in chroma
between ADJACENT levels (mean |dx| 0.0041, 13/22 sign flips, e.g. cv 730 x 0.3055 -> 748 0.2991 -> 784 0.3068
-> 801 0.2975 -> 819 0.2916 -> 837 0.3074), while the profiled post-MHC stage is smooth (0.0007) and its
peak-white repeats agree to 0.0003 regardless of the previous patch. The base cube inherited the saw-tooth as a
+-5 % R/G drive ripple between 100 and 1000 nits; the refine pinned only its 25 levels, so +-4 % R/G survives
between them = red-dominant stripes on a ramp (a pure-colour ramp never exercises the R/G ratio).

This probe reads N grey levels in ASCENDING order, one read each, in the APPLIED state (no DesktopLUT state
change), and reports x/y vs level, the adjacent-level |dx| statistic, and the worst stripe pairs. Run it once
before the fix (expect saw-tooth |dx| ~0.003-0.006 between refine pins) and once after (expect < 0.001).
Optional: `--levels 60 --lo 100 --hi 1000` (defaults) ; `--desc` reads descending as an order control.

Run (daemon up on 28930 fullscreen on mon 0, i1D3 on the panel):
    PYTHONPATH=src python agent_probe_grey_ramp.py
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from dlc import calibration_profile as cp
from dlc._pq import oetf_norm
from dlc.argyll import Argyll, SpotreadRequest
from dlc.calibrate import active_correction, correction_store_path
from dlc.correction_store import CorrectionStore
from dlc.measure_loop import MeasurePatch, SocketPresenter, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port

MON, BIT = 0, 10
HOST, PORT = "127.0.0.1", 28930
REF = (512, 512, 512)


def log(*a):
    print(*a, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", type=int, default=60)
    ap.add_argument("--lo", type=float, default=100.0, help="nits")
    ap.add_argument("--hi", type=float, default=1000.0, help="nits")
    ap.add_argument("--desc", action="store_true", help="read descending (order control)")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    out = Path("runs/probes") / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_grey_ramp" + (("_" + a.tag) if a.tag else ""))
    out.mkdir(parents=True, exist_ok=True)

    # levels equally spaced in PQ signal between lo and hi nits (perceptually even, like the video ramp)
    s_lo, s_hi = oetf_norm(a.lo / 10000.0), oetf_norm(a.hi / 10000.0)
    cvs = sorted({int(round((s_lo + (s_hi - s_lo) * i / (a.levels - 1)) * 1023)) for i in range(a.levels)})
    if a.desc:
        cvs = cvs[::-1]

    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)
    log(f"[setup] spotread port={port} ok={info.get('ok')} ccmx={ccmx}  levels={len(cvs)} {'desc' if a.desc else 'asc'} cv {cvs[0]}..{cvs[-1]}")

    presenter = SocketPresenter(HOST, PORT, settle_seconds=1.0)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    rows: list[dict] = []

    def read(cv):
        p = MeasurePatch(label=f"g{cv}", rgb=(cv, cv, cv), signal=(cv / 1023,) * 3, role="measurement", bit_depth=BIT, seq=0)
        rd = measure(p)
        if rd.xyz is None:
            return None
        X, Y, Z = rd.xyz; s = X + Y + Z
        row = {"cv": cv, "t": datetime.now().isoformat(timespec="milliseconds"), "Y": Y, "x": X / s, "y": Y / s}
        rows.append(row)
        return row

    try:
        read(REF[0])   # settle at the reference, discard from stats (cv 512 is inside the ramp anyway)
        rows.clear()
        for cv in cvs:
            r = read(cv)
            if r:
                log(f"   cv {cv:4d}  Y {r['Y']:8.2f}  xy ({r['x']:.4f},{r['y']:.4f})")
    except KeyboardInterrupt:
        log("[abort] Ctrl+C")
    finally:
        try:
            measure(MeasurePatch(label="park", rgb=REF, signal=(0.5,) * 3, role="measurement", bit_depth=BIT, seq=0))
        except Exception:  # noqa: BLE001
            pass
        try:
            meter.close()
        except Exception:  # noqa: BLE001
            pass
        (out / "reads.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
        log(f"[done] {len(rows)} reads -> {out}")

    by = sorted(rows, key=lambda r: r["cv"])
    if len(by) >= 4:
        dx = [by[i + 1]["x"] - by[i]["x"] for i in range(len(by) - 1)]
        dy = [by[i + 1]["y"] - by[i]["y"] for i in range(len(by) - 1)]
        flips = sum(1 for i in range(len(dx) - 1) if dx[i] * dx[i + 1] < 0)
        log(f"\n[summary] adjacent-level |dx| mean {statistics.mean(abs(d) for d in dx):.4f} max {max(abs(d) for d in dx):.4f}  "
            f"|dy| mean {statistics.mean(abs(d) for d in dy):.4f}  sign flips {flips}/{len(dx) - 1}  "
            f"(stripes ~ |dx| >= 0.003 with alternating sign; smooth ramp ~ < 0.001)")
        worst = sorted(range(len(dx)), key=lambda i: -abs(dx[i]))[:6]
        for i in worst:
            log(f"   stripe pair cv {by[i]['cv']}->{by[i + 1]['cv']} ({by[i]['Y']:.0f}->{by[i + 1]['Y']:.0f} nits): dx {dx[i]:+.4f} dy {dy[i]:+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
