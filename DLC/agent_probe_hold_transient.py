"""ONE-OFF HW probe B (Claude, 2026-09-03): burst-vs-held neutral transient on the PA32UCXR.

Question (docs/pa32ucxr-plan-2026-09-03.md item 3): the owner sees peak white alternate warm/cool on
an HDR grey-ramp test. The run's data shows bright neutrals stable to +-0.0002 x across 45 min of
golden-ratio load (mean 75-200 nits over 2 min) - but the run only ever reads bright neutrals as
1-3 s BURSTS. A grey-ramp test HOLDS full-field white for seconds. This probe measures the held
transient directly in the APPLIED (viewing) state, and the recovery after a rest at the run's
92-nit reference.

Legs (each read is one persistent-spotread trigger, ~1-2 s on bright fields):
  pre   : 90 s at ref512 (~92 nits), reads continuously  - baseline + meter warm
  hold1 : 180 s at g837 (peak code, ~1766 nits), reads continuously; every ~20 s ONE ref512 read is
          interleaved (the reference's chroma during the hold separates a panel-global / sensor shift
          from the white's own transient - if ref512 moves with the white, suspect the sensor or a
          panel-wide state; if only g837 moves, it is the white's local thermal behaviour)
  rest1 : 120 s at ref512, reads continuously (recovery direction + time constant)
  hold2 : 180 s at g680 (~450 nits), same interleave
  rest2 : 120 s at ref512
Outputs: per-read CSV/JSON (t, leg, label, Y, x, y) and a summary: burst = median of the first 3 s
of each hold, plateau = median of the last 60 s, dY, dx, dy, and a crude exponential time constant.

Safety: the PA32UCXR is rated for sustained full-field HDR (1600-nit class); the 06-19 characterize
held ~1818 nits for 20 reads. Abort (Ctrl+C) parks at ref512. No DesktopLUT state is mutated.
Run (daemon up on 28930 fullscreen on mon 0, i1D3 on the panel, applied stack live):
    PYTHONPATH=src python agent_probe_hold_transient.py            # all legs
    PYTHONPATH=src python agent_probe_hold_transient.py pre hold1  # windowed-daemon repeat (restart the daemon with
                                                                   #   --patch-size ~15 and re-aim the meter first)
The windowed repeat is the decisive leg for the owner's grey-ramp alternation: dogegen_server.py:64 already
states full-field exists to avoid mini-LED local-dimming contamination of a WINDOWED patch - a ramp-test app's
window drives the zones differently from the calibration's full field.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from dlc import calibration_profile as cp
from dlc.argyll import Argyll, SpotreadRequest
from dlc.calibrate import active_correction, correction_store_path
from dlc.correction_store import CorrectionStore
from dlc.measure_loop import MeasurePatch, SocketPresenter, make_persistent_spotread_meter
from dlc.measure_rgbw import resolve_spotread_instrument_port

MON, BIT = 0, 10
HOST, PORT = "127.0.0.1", 28930
OUT = Path("runs/probes") / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_hold_transient")
REF = ("ref512", (512, 512, 512))
LEGS = [
    ("pre", REF, 90.0, None),
    ("hold1", ("g837", (837, 837, 837)), 180.0, 20.0),
    ("rest1", REF, 120.0, None),
    ("hold2", ("g680", (680, 680, 680)), 180.0, 20.0),
    ("rest2", REF, 120.0, None),
]


def log(*a):
    print(*a, flush=True)


def xy(X):
    s = X[0] + X[1] + X[2]
    return (X[0] / s, X[1] / s) if s > 1e-9 else (0.0, 0.0)


def main() -> int:
    global LEGS
    if len(sys.argv) > 1:     # e.g. `agent_probe_hold_transient.py pre hold1` for the windowed-daemon repeat
        want = set(sys.argv[1:]); LEGS = [l for l in LEGS if l[0] in want]
        log(f"[legs] {[l[0] for l in LEGS]}")
    OUT.mkdir(parents=True, exist_ok=True)
    profile = cp.load_profile()
    argyll = Argyll(Path(profile.paths["argyll"]) / "spotread.exe")
    port, info = resolve_spotread_instrument_port(argyll, profile.meter.argyll_port)
    store = CorrectionStore.load(correction_store_path(profile, Path.cwd()))
    disp_name = profile.display_for(MON).name
    ccmx = active_correction(profile, store, disp_name)
    log(f"[setup] spotread port={port} ok={info.get('ok')} display={disp_name!r} ccmx={ccmx}")

    # 1.0 s presenter settle on EVERY read (= the run's jump-settle dwell): without it the first read of a
    # hold is meter/electronics rise-time, not the panel. Cadence ~2-3 s on bright fields.
    presenter = SocketPresenter(HOST, PORT, settle_seconds=1.0)
    meter = argyll.open_persistent(SpotreadRequest(port=port, ccmx_or_ccss=Path(ccmx) if ccmx else None))
    measure = make_persistent_spotread_meter(presenter=presenter, persistent=meter)
    rows: list[dict] = []
    t0 = time.monotonic()

    def read(leg, label, cv):
        sig = tuple(c / ((1 << BIT) - 1) for c in cv)
        p = MeasurePatch(label=label, rgb=cv, signal=sig, role="measurement", bit_depth=BIT, seq=0)
        rd = measure(p)
        if rd.xyz is None:
            return None
        x, y = xy(rd.xyz)
        row = {"t": round(time.monotonic() - t0, 3), "wall": datetime.now().isoformat(timespec="milliseconds"),
               "leg": leg, "label": label, "Y": rd.xyz[1], "x": x, "y": y}
        rows.append(row)
        return row

    try:
        for leg, (label, cv), dur, interleave in LEGS:
            log(f"[{leg}] {label} cv={cv} for {dur:.0f} s" + (f", ref512 every {interleave:.0f} s" if interleave else ""))
            t_leg = time.monotonic(); t_ref = t_leg; n = 0; burst_y = None
            while time.monotonic() - t_leg < dur:
                if interleave and time.monotonic() - t_ref >= interleave:
                    r = read(leg, "ref512", REF[1]); t_ref = time.monotonic()
                    if r: log(f"   {r['t']:7.1f}s  ref512  Y={r['Y']:8.2f} xy=({r['x']:.4f},{r['y']:.4f})")
                r = read(leg, label, cv); n += 1
                if r and (n <= 3 or n % 5 == 0):
                    log(f"   {r['t']:7.1f}s  {label:6s}  Y={r['Y']:8.2f} xy=({r['x']:.4f},{r['y']:.4f})")
                # circuit breaker (protection_drop_frac analogue): >15% luminance drop under load mid-hold =
                # ABL/thermal protection or a present-stall - bail to the reference instead of cooking on.
                if r and n == 3:
                    burst_y = r["Y"]
                if r and burst_y and burst_y > 50 and r["Y"] < 0.85 * burst_y:
                    log(f"   [ABORT] {leg}: Y {r['Y']:.1f} < 85% of burst {burst_y:.1f} - protection/stall? parking.")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        log("[abort] Ctrl+C")
    finally:
        try:
            read("park", *REF)
        except Exception:  # noqa: BLE001
            pass
        try:
            meter.close()
        except Exception:  # noqa: BLE001
            pass
        (OUT / "reads.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
        with open(OUT / "reads.csv", "w", encoding="utf-8") as fh:
            fh.write("t,wall,leg,label,Y,x,y\n")
            for r in rows:
                fh.write(f"{r['t']},{r['wall']},{r['leg']},{r['label']},{r['Y']:.4f},{r['x']:.5f},{r['y']:.5f}\n")
        log(f"[done] {len(rows)} reads -> {OUT}")

    # ---- summary -------------------------------------------------------------------------
    def seg(leg, label):
        return [r for r in rows if r["leg"] == leg and r["label"] == label]

    def med(rs, k):
        return statistics.median(r[k] for r in rs) if rs else float("nan")

    log("\n[summary]")
    for leg, (label, _cv), dur, _il in LEGS:
        rs = seg(leg, label)
        if not rs:
            continue
        ts0 = rs[0]["t"]
        burst = [r for r in rs if r["t"] - ts0 <= 3.0] or rs[:2]
        plateau = [r for r in rs if r["t"] >= rs[-1]["t"] - 60.0]
        bY, pY = med(burst, "Y"), med(plateau, "Y")
        bx, px, by, py = med(burst, "x"), med(plateau, "x"), med(burst, "y"), med(plateau, "y")
        # crude tau: time for Y to cover 63% of (plateau - burst); nan if the change is within noise
        tau = float("nan")
        if abs(pY - bY) > 0.5:
            target = bY + 0.632 * (pY - bY)
            for r in rs:
                if (pY > bY and r["Y"] >= target) or (pY < bY and r["Y"] <= target):
                    tau = r["t"] - ts0; break
        log(f"  {leg:6s} {label:6s} n={len(rs):3d}  burst Y {bY:8.2f} xy ({bx:.4f},{by:.4f})  "
            f"plateau Y {pY:8.2f} xy ({px:.4f},{py:.4f})  dY {100 * (pY - bY) / bY:+.2f}%  "
            f"dx {px - bx:+.4f} dy {py - by:+.4f}  tau~{tau:.0f}s")
        refs = seg(leg, "ref512") if label != "ref512" else []
        if refs:
            pre = seg("pre", "ref512")
            ref0x = med(pre[-10:], "x") if pre else float("nan"); ref0y = med(pre[-10:], "y") if pre else float("nan")
            drx = med(refs[-3:], "x") - ref0x; dry = med(refs[-3:], "y") - ref0y
            log(f"         ref512 during hold: n={len(refs)} Y {med(refs, 'Y'):7.2f} x {min(r['x'] for r in refs):.4f}..{max(r['x'] for r in refs):.4f}"
                f"  ref drift vs pre {drx:+.4f}/{dry:+.4f} -> corrected hold dx {px - bx - drx:+.4f} dy {py - by - dry:+.4f}")
            log("         [caveat] the interleaved reference shares neither optical nor sensor history with the hold;"
                " a spectro read at burst + plateau is the clean sensor control]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
