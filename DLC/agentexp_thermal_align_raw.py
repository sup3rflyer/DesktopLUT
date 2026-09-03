"""THERMAL ALIGNMENT of a raw characterization to one reference state (Claude, 2026-09-03; owner directive:
"use the thermal data to find the middle ground to align patches on, at the least for neutrals").

The measure loop reads a fixed neutral reference (warm-up patch, e.g. 512/512/532) every N patches and at
every preheat. Its per-channel LINEAR balance over time is the panel's thermal trajectory. Every raw read taken
at time t is corrected per channel by the reference balance ratio balance(t_align)/balance(t), so the whole raw
set describes the panel at ONE state (default: the reference state at the END of the stage - the state the
closed-loop refine measures in minutes later). First-order model: a backlight balance drift is a per-channel
luminance gain (measured PA32UCXR 2026-09-03: +0.00015 x/min in the identity state under the run's load).

Usage:
  PYTHONPATH=src python agentexp_thermal_align_raw.py --run <run dir> [--align end|start|mid] [--apply]
Dry run prints the reference track, the drift, and the correction magnitudes. --apply rewrites
measurements/raw.ti3 (backup raw.ti3.orig, once) and writes measurements/raw_thermal_align.json.
Replay check: rebuild the base cube from the original and the aligned ti3 (agentexp harness) and compare the
grey-share ripple; the live check is the refine's round-1 grey avg.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
from datetime import datetime
from pathlib import Path

from dlc.colormath import invert3x3, matvec, rgb_to_xyz_matrix


def load_reads(ndjson: Path):
    rows = []
    for line in ndjson.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("xyz") and e.get("rgb") and e.get("role") in ("measurement", "neutral_ref", "warmup"):
            e["ts"] = datetime.fromisoformat(e["t"]).timestamp()
            rows.append(e)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--align", choices=("end", "start", "mid"), default="end")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-nits", type=float, default=1.0, help="do not correct reads below this luminance (noise floor)")
    a = ap.parse_args()
    run = Path(a.run)
    reads = load_reads(run / "measurements" / "raw.ndjson")
    params = json.loads((run / "generated" / "mhc_params_hdr.json").read_text(encoding="utf-8")) if (run / "generated" / "mhc_params_hdr.json").exists() else None
    if params:
        p = params["primaries"]; w = (params["measured_white"]["x"], params["measured_white"]["y"])
    else:
        # DIP native primaries as the linear basis (the matrix only needs to be a consistent linear RGB basis)
        prof = json.loads(Path("dip_store.json").read_text(encoding="utf-8"))["displays"]
        key = next(k for k in prof if k.endswith(":HDR") and "PA32UCXR" in k)
        npr = prof[key]["native_primaries"]; wxy = prof[key]["native_white_xy"]
        p = {"rx": npr["R"][0], "ry": npr["R"][1], "gx": npr["G"][0], "gy": npr["G"][1], "bx": npr["B"][0], "by": npr["B"][1]}
        w = (wxy[0], wxy[1])
    P = rgb_to_xyz_matrix(p["rx"], p["ry"], p["gx"], p["gy"], p["bx"], p["by"], w[0], w[1], white_Y=1.0)
    Pinv = invert3x3(P)

    refs = [r for r in reads if r["role"] in ("neutral_ref", "warmup")]
    if len(refs) < 3:
        print("not enough reference reads"); return 2
    ref_rgb = refs[0]["rgb"]
    refs = [r for r in refs if r["rgb"] == ref_rgb]
    track = []
    for r in refs:
        lin = matvec(Pinv, r["xyz"])
        s = sum(lin)
        track.append((r["ts"], [v / s for v in lin], r["xyz"], r["t"][11:19]))
    track.sort()
    t0, t1 = track[0][0], track[-1][0]
    print(f"[track] {len(track)} reference reads ({ref_rgb}) over {(t1 - t0) / 60:.1f} min")
    for ts, bal, xyz, tt in track[:: max(1, len(track) // 8)]:
        s = sum(xyz); print(f"   {tt}  x {xyz[0] / s:.4f} y {xyz[1] / s:.4f}   R/G {bal[0] / bal[1]:.4f}  B/G {bal[2] / bal[1]:.4f}")
    if a.align == "end":
        target = track[-1][1]
    elif a.align == "start":
        target = track[0][1]
    else:
        target = [statistics.mean(b[i] for _, b, _, _ in track) for i in range(3)]

    def balance_at(ts):
        if ts <= track[0][0]:
            return track[0][1]
        if ts >= track[-1][0]:
            return track[-1][1]
        for k in range(1, len(track)):
            if track[k][0] >= ts:
                (ta, ba, _, _), (tb, bb, _, _) = track[k - 1], track[k]
                f = (ts - ta) / max(tb - ta, 1e-9)
                return [ba[i] + (bb[i] - ba[i]) * f for i in range(3)]
        return track[-1][1]

    # correction per measurement read
    corr = {}
    mags = []
    for r in reads:
        if r["role"] != "measurement" or r["xyz"][1] < a.min_nits:
            continue
        b = balance_at(r["ts"])
        f = [target[i] / b[i] for i in range(3)]
        lin = matvec(Pinv, r["xyz"])
        lin2 = [lin[i] * f[i] for i in range(3)]
        xyz2 = matvec(P, lin2)
        key = (tuple(r["rgb"]), tuple(round(v, 6) for v in r["xyz"]))
        corr[key] = xyz2
        s1 = sum(r["xyz"]); s2 = sum(xyz2)
        if s1 > 0 and s2 > 0:
            mags.append(abs(xyz2[0] / s2 - r["xyz"][0] / s1))
    print(f"[corr] align={a.align}  target R/G {target[0] / target[1]:.4f} B/G {target[2] / target[1]:.4f}  reads corrected {len(corr)}  |dx| mean {statistics.mean(mags):.4f} max {max(mags):.4f}")

    ti3 = run / "measurements" / "raw.ti3"
    text = ti3.read_text(encoding="utf-8", errors="replace")
    # locate data block + columns
    m = re.search(r"BEGIN_DATA_FORMAT\s+(.*?)\s+END_DATA_FORMAT.*?BEGIN_DATA[ \t]*\r?\n(.*?)END_DATA", text, re.S)
    if not m:
        print("ti3 parse failed"); return 2
    fields = m.group(1).split()
    ir, ig, ib = fields.index("RGB_R"), fields.index("RGB_G"), fields.index("RGB_B")
    ix, iy, iz = fields.index("XYZ_X"), fields.index("XYZ_Y"), fields.index("XYZ_Z")
    lines = m.group(2).splitlines()
    out, hit, miss = [], 0, 0
    for ln in lines:
        parts = ln.split()
        if len(parts) < len(fields):
            out.append(ln); continue
        rgb = tuple(int(round(float(parts[i]) / 100.0 * 1023)) for i in (ir, ig, ib))
        xyz = tuple(float(parts[i]) for i in (ix, iy, iz))
        # match by rgb and closest XYZ (the ti3 holds the accepted read; ndjson holds all reads)
        cands = [(k, v) for k, v in corr.items() if k[0] == rgb]
        if not cands:
            miss += 1; out.append(ln); continue
        k, v = min(cands, key=lambda kv: sum((kv[0][1][i] - xyz[i]) ** 2 for i in range(3)))
        if sum((k[1][i] - xyz[i]) ** 2 for i in range(3)) > (0.02 * max(xyz[1], 0.01)) ** 2 + 1e-6:
            miss += 1; out.append(ln); continue
        parts[ix], parts[iy], parts[iz] = f"{v[0]:.6f}", f"{v[1]:.6f}", f"{v[2]:.6f}"
        out.append(" ".join(parts)); hit += 1
    print(f"[ti3] rows matched {hit}, untouched {miss} (dark/unmatched)")
    if a.apply:
        bak = ti3.with_suffix(".ti3.orig")
        if not bak.exists():
            shutil.copy2(ti3, bak)
        new = text[: m.start(2)] + "\n".join(out) + "\n" + text[m.end(2):]
        ti3.write_text(new, encoding="utf-8")
        note = {"align": a.align, "target_balance": target, "reference_rgb": ref_rgb, "track_n": len(track),
                "track_minutes": (t1 - t0) / 60, "rows_corrected": hit, "dx_mean": statistics.mean(mags), "dx_max": max(mags),
                "applied": datetime.now().isoformat(timespec="seconds"), "backup": str(bak)}
        (run / "measurements" / "raw_thermal_align.json").write_text(json.dumps(note, indent=1), encoding="utf-8")
        print(f"[apply] raw.ti3 rewritten (backup {bak.name}); note -> raw_thermal_align.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
