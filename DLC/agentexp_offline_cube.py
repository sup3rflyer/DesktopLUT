"""Offline (probe-free) HDR cube regeneration from an existing post-MHC dataset.

Why (2026-09-03 23:xx): run 211134's build probed the panel through a DEAD DWM hook (the cube
never reached the patch), so its live-probe iterations were blind and the cube ran away
(467 reversals). The training data itself (post_mhc.ti3, thermally aligned) is a clean
MHC-only measurement. This rebuilds the cube with the production optimizer, but the probe is
the RBF forward model fitted on that data (the tier-1 software path, exactly what the sim
tests use) — no panel reads. The hardware verify (once the hook is alive) is the judge.

Usage: PYTHONPATH=src python agentexp_offline_cube.py --run runs/<dir> [--out generated/final_hdr_offline.cube]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from dlc.engine.model import DisplayErrorModel, Target
from dlc.metrics import sanitize_reachable_primaries
from dlc.mhc import parse_ti3
from dlc.optimize import OptimizeConfig, optimize_cube

D65 = (0.3127, 0.3290)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--ti3", default="measurements/post_mhc.ti3")
    ap.add_argument("--out", default="generated/final_hdr_offline.cube")
    ap.add_argument("--display-key", default="Asus ProArt PA32UCXR:HDR")
    ap.add_argument("--max-outer", type=int, default=2)
    a = ap.parse_args()
    run = Path(a.run)
    samples = parse_ti3(run / a.ti3)
    signals = np.array([s.rgb for s in samples], dtype=float)
    measured = np.array([s.xyz for s in samples], dtype=float)
    print(f"[data] {len(samples)} samples from {a.ti3}")
    dip = json.loads(Path("dip_store.json").read_text(encoding="utf-8"))["displays"][a.display_key]
    npr = dip["native_primaries"]
    reachable = sanitize_reachable_primaries({ch: [float(npr[ch][0]), float(npr[ch][1])] for ch in ("R", "G", "B")})
    print("[gamut] reachable primaries:", reachable)
    target = Target.hdr_rec2020_pq(white_xy=D65)
    cfg = OptimizeConfig(max_outer=a.max_outer)
    # Tier-1 probe: the panel as the RBF model predicts it. Fitted ONCE on the raw data with the
    # same smoothing the optimizer uses, so "measured" == the model's own forward — the loop
    # converges to the model inverse (its seed) instead of chasing a dead hook.
    model = DisplayErrorModel(signals, np.maximum(measured, 0.0), target, smoothing=cfg.smoothing,
                              reachable_primaries=reachable)

    def probe(sig: np.ndarray) -> np.ndarray:
        return np.asarray(model.forward(np.clip(np.asarray(sig, dtype=float).reshape(-1, 3), 0.0, 1.0)), dtype=float)

    def on_iter(it):
        d = getattr(it, "digest", None) or {}
        print(f"[iter] {getattr(it, 'iteration', '?')}: " + json.dumps(
            {k: d.get(k) for k in ("measured_mean_de", "measured_p95_de", "measured_max_de", "above_threshold",
                                   "budget_limited", "large_reversals", "cube_monotonic") if k in d}), flush=True)

    t0 = time.time()
    res = optimize_cube(target=target, probe=probe, signals=signals, measured_xyz=measured, config=cfg,
                        on_iteration=on_iter, reachable_primaries=reachable)
    out = run / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    res.write(str(out), title="DLC HDR 3D LUT (offline regen, model probe)")
    dg = dict(res.digest)
    dg.pop("floor_offenders", None)
    print(f"[done] {time.time() - t0:.0f}s -> {out}")
    print("[digest]", json.dumps(dg)[:1200])
    # identity deviation summary
    t = out.read_text().splitlines()
    n = int([l for l in t if l.startswith("LUT_3D_SIZE")][0].split()[1])
    rows = np.array([[float(v) for v in l.split()] for l in t if l and (l[0].isdigit() or l[0] == "0")])
    g = np.linspace(0, 1, n)
    idx = np.array([[r, gg, b] for b in g for gg in g for r in g])
    dev = rows - idx
    grey = [i * n * n + i * n + i for i in range(n)]
    print(f"[cube] size {n}  max|dev| {np.abs(dev).max():.4f}  mean|dev| {np.abs(dev).mean():.4f}  grey-axis max|dev| {np.abs(dev[grey]).max():.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
