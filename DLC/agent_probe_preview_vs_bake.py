"""ONE-OFF HW probe E (Claude, 2026-09-03): A/B of DesktopLUT's LIVE-PREVIEW shader realization of a grayscale
correction against the SAME correction BAKED into the MHC ICM, across luminance and chromaticity.

Owner: "I'm not convinced the software shader/live preview behaves 100% like a baked MHC profile." The 08-15
finding was preview 0.4-0.75x physics and unstable, bake faithful; this measures it systematically.

Per correction set T (32-point HDR grayscale table, PQ-linear points t_i, per-channel PQ-signal gains):
   baseline  -> grayscale_live_begin + grayscale_set_live(T) -> measure [preview]
             -> grayscale_commit (bake into the ICM)         -> measure [bake]
             -> set_correction_grayscale(identity) + apply_mhc (clear the slot) -> ref check
Correction sets: T1 red +6% global; T2 blue -6% global; T3 red ramp +8% at t=0 -> 0 at t=1; T4 luminance -5%.
Patches (2 reads each, ref512 every 6): greys ~5/30/100/300/800/1500 nits; R/G/B at cv 620 and 837; mid-sat
(837,620,620) (620,837,620) (620,620,837) (760,700,600) (500,600,700). Baseline measured at start and end.
Report: per patch preview vs bake (dY %, dx, dy), each vs baseline, and the realization ratio
(preview delta / bake delta) per patch and per set. Faithful preview => ratio ~1.0 within meter noise.
Audits state first (tonemap/DG/WB/GS must be off); restores GS slot to identity (CorrGSEnabled stays true with an
identity table - the codebase's accepted neutral; untick in the GUI afterwards if desired).
Run: PYTHONPATH=src python agent_probe_preview_vs_bake.py [T1 T2 ...]
"""
from __future__ import annotations

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
from dlc.controller import CalibrationController
from dlc.correction_store import CorrectionStore
from dlc.measure_loop import MeasurePatch, SocketPresenter, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port

from agent_probe_common import audit_state, require

MON, MODE, BIT = 0, "HDR", 10
HOST, PORT = "127.0.0.1", 28930
OUT = Path("runs/probes") / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_preview_vs_bake")
N = 32
POINTS = [i / (N - 1) for i in range(N)]
REF = ("ref512", (512, 512, 512))


def cv_for(nits):
    return int(round(oetf_norm(nits / 10000.0) * 1023))


PATCHES = [(f"g{n}", (cv_for(n),) * 3) for n in (5, 30, 100, 300, 800, 1500)] + [
    ("r620", (620, 0, 0)), ("grn620", (0, 620, 0)), ("b620", (0, 0, 620)),
    ("r837", (837, 0, 0)), ("grn837", (0, 837, 0)), ("b837", (0, 0, 837)),
    ("pink", (837, 620, 620)), ("mint", (620, 837, 620)), ("lav", (620, 620, 837)),
    ("skin", (760, 700, 600)), ("cool", (500, 600, 700)),
]


def table(r=None, g=None, b=None):
    ident = [1.0] * N
    return {"r": r or ident, "g": g or ident, "b": b or ident}


SETS = {
    "T1": ("red +6% global", table(r=[1.06] * N)),
    "T2": ("blue -6% global", table(b=[0.94] * N)),
    "T3": ("red ramp +8%@dark -> 0@top", table(r=[1.0 + 0.08 * (1.0 - t) for t in POINTS])),
    "T4": ("luminance -5%", table(r=[0.95] * N, g=[0.95] * N, b=[0.95] * N)),
}


def log(*a):
    print(*a, flush=True)


def xy(X):
    s = X[0] + X[1] + X[2]
    return (X[0] / s, X[1] / s) if s > 1e-9 else (0.0, 0.0)


def main() -> int:
    want = [a for a in sys.argv[1:] if a in SETS] or list(SETS)
    OUT.mkdir(parents=True, exist_ok=True)
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    ccmx = active_correction(profile, store, profile.display_for(MON).name)
    log(f"[setup] spotread port={port} ok={info.get('ok')} ccmx={ccmx}  sets={want}")
    ctrl = CalibrationController.connect()
    a = audit_state(ctrl, MON, MODE, log)
    if not require(a, log=log):
        log("[ABORT] fix the state above first"); return 1

    presenter = SocketPresenter(HOST, PORT, settle_seconds=1.7)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    rows: list[dict] = []

    def read(arm, label, cv, n=2):
        sig = tuple(c / 1023 for c in cv)
        out = []
        for _ in range(n):
            rd = measure(MeasurePatch(label=label, rgb=cv, signal=sig, role="measurement", bit_depth=BIT, seq=0))
            if rd.xyz is None:
                log(f"   {arm:14s} {label:7s} NO READ"); continue
            X, Y, Z = rd.xyz; x, y = xy(rd.xyz)
            r = {"arm": arm, "label": label, "cv": list(cv), "t": datetime.now().isoformat(timespec="milliseconds"),
                 "Y": Y, "x": x, "y": y, "xyz": [X, Y, Z]}
            rows.append(r); out.append(r)
        if out:
            log(f"   {arm:14s} {label:7s} Y {statistics.mean(r['Y'] for r in out):9.3f}  xy ({statistics.mean(r['x'] for r in out):.4f},{statistics.mean(r['y'] for r in out):.4f})")
        return out

    def sweep(arm):
        read(arm, *REF, n=2)
        for i, (label, cv) in enumerate(PATCHES):
            if i and i % 6 == 0:
                read(arm, *REF, n=1)
            read(arm, label, cv)
        read(arm, *REF, n=2)

    def clear_slot():
        ctrl.set_correction_grayscale(MON, MODE, N, POINTS, table())
        ctrl.apply_mhc(MON, MODE)
        time.sleep(4.0)

    live = False
    try:
        sweep("baseline")
        for name in want:
            desc, tbl = SETS[name]
            log(f"\n[{name}] {desc}")
            ctrl.grayscale_live_begin(MON, MODE); live = True
            ctrl.grayscale_set_live(MON, MODE, N, POINTS, tbl)
            time.sleep(2.0)
            sweep(f"{name}-preview")
            ctrl.grayscale_commit(MON, MODE); live = False
            time.sleep(4.0)
            audit_state(ctrl, MON, MODE, log)
            sweep(f"{name}-bake")
            clear_slot()
            read(f"{name}-cleared", *REF, n=2)
        sweep("baseline2")
    except KeyboardInterrupt:
        log("[abort] Ctrl+C")
    finally:
        try:
            if live:
                ctrl.grayscale_cancel(MON, MODE)
            clear_slot()
        except Exception as exc:  # noqa: BLE001
            log(f"[restore] FAILED: {exc} - owner: check the GUI grayscale slot")
        try:
            read("park", *REF, n=1)
        except Exception:  # noqa: BLE001
            pass
        try:
            meter.close()
        except Exception:  # noqa: BLE001
            pass
        (OUT / "reads.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
        log(f"[done] {len(rows)} reads -> {OUT / 'reads.json'}")

    # ---- analysis ------------------------------------------------------------------------
    def mean_xyz(arm, label):
        rs = [r for r in rows if r["arm"] == arm and r["label"] == label]
        if not rs:
            return None
        return [statistics.mean(r["xyz"][i] for r in rs) for i in range(3)]

    def delta(a, b):
        if not a or not b or b[1] <= 0:
            return None
        ax, ay = xy(a); bx, by = xy(b)
        return {"dY%": 100 * (a[1] - b[1]) / b[1], "dx": ax - bx, "dy": ay - by}

    log("\n[analysis] per set: patch | preview vs baseline | bake vs baseline | preview vs bake | realization ratio (preview dY / bake dY, preview dx / bake dx)")
    base = {l: mean_xyz("baseline", l) for l, _ in PATCHES}
    base2 = {l: mean_xyz("baseline2", l) for l, _ in PATCHES}
    drift = [delta(base2[l], base[l]) for l, _ in PATCHES if base2[l] and base[l]]
    if drift:
        log(f"[drift] baseline2 vs baseline over the whole probe: mean dY {statistics.mean(d['dY%'] for d in drift):+.2f}%  mean dx {statistics.mean(d['dx'] for d in drift):+.4f}  max |dx| {max(abs(d['dx']) for d in drift):.4f}")
    for name in want:
        log(f"\n== {name}: {SETS[name][0]}")
        ratios_y, ratios_x = [], []
        for l, _ in PATCHES:
            p = mean_xyz(f"{name}-preview", l); k = mean_xyz(f"{name}-bake", l); b = base[l]
            dp, dk, dpk = delta(p, b), delta(k, b), delta(p, k)
            if not (dp and dk and dpk):
                continue
            ry = dp["dY%"] / dk["dY%"] if abs(dk["dY%"]) > 0.3 else float("nan")
            rx = dp["dx"] / dk["dx"] if abs(dk["dx"]) > 0.0008 else float("nan")
            if ry == ry: ratios_y.append(ry)
            if rx == rx: ratios_x.append(rx)
            log(f"  {l:7s} prev dY {dp['dY%']:+6.2f}% dx {dp['dx']:+.4f} | bake dY {dk['dY%']:+6.2f}% dx {dk['dx']:+.4f} | prev-bake dY {dpk['dY%']:+6.2f}% dx {dpk['dx']:+.4f} dy {dpk['dy']:+.4f} | ratio Y {ry:5.2f} x {rx:5.2f}")
        if ratios_y or ratios_x:
            log(f"  realization ratio: Y median {statistics.median(ratios_y) if ratios_y else float('nan'):.2f} (n={len(ratios_y)})  x median {statistics.median(ratios_x) if ratios_x else float('nan'):.2f} (n={len(ratios_x)})   [1.00 = preview == bake]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
