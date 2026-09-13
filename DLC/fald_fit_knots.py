"""Stage-B refit of the panel ESTIMATE with a FREE-FORM radial profile (``est_kind="knots"``) — offline, no hardware.

Why (doc §26/§31a/§33): the shipped single-scale exponential estimate cannot fit step-edge rings and gradients
together — the ramp-inclusive exp refit (sim/fit_ramps.py) improved the ramps but degraded every held-out set — and
the 120-px dark ring next to a bright bar is modelled 3–8 pp too deep at 1–20 nits (the over-correction the layer
applies). A monotone free-form log-weight profile over 8 knots (0…5 cells) lets the near field and the mid field
have different slopes.

In-sample (Stage B): the grey-10 ring ratios of every sweep (as fald_fit main: 1950/1990 both sides, up/down, the
fine and mid-field sweeps) + IMAGE items: the §29 horizontal/vertical ramps (ramp_probe_173727.json, OFF read
relative to the flat baseline; weight ×1) and the §33 dark-halo bars at gaps 120/240/480 for greys 2/5/20 nits
(OFF/flat of the same grey; 0.5/1 nit excluded — the model is out of domain below ~1 nit; gaps 20/60 excluded —
inside the meter's real acceptance). Held out = exactly fald_fit main's set: rings at 5/20/40 nits at both positions
incl. up/down, the comp table, orange, the diagonal rings.

Fitted: est_knot_logw (7 monotone decrements, logw_0 pinned — the normalisation removes a constant), est_phase_px/py,
est_aniso, drive_dim. Fixed: the true kernel, drive curve, statistic, support. Stage 1 fits the shape items only
(fast), Stage 2 adds the image items, variant S6 repeats Stage 2 with est_support_cells = 6.

GATE (the point of the exercise): a candidate is accepted only if (i) no held-out group degrades by more than
0.2 pp mean|err| vs the shipped params and (ii) the near field improves: the bar-120 model-vs-panel gap at 5 and 20
nits shrinks by at least half, ramps h/hsteep mean|err| ≤ 1.0 pp, the fine-sweep rings not worse.

Residual evaluation is parallel (multiprocessing; each worker builds the datasets itself and keeps a small LRU of
models, capped at 8 workers running at below-normal priority); the Jacobian columns are evaluated concurrently.

Usage (DLC root):  PYTHONPATH="src;." python fald_fit_knots.py [--stage all|check|1|2|s6] [--params fit.json]
                   [--nfev 60] [--jobs 8] [--diff-step 0.05] [--out results/.../stat_fit] [--no-scale1]
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.setdefault("FALD_DATA", str(ROOT / "results" / "fald_native_2026-09-11")))
for _p in (str(ROOT), str(ROOT / "src"), str(DATA / "sim")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                                   # noqa: E402
from scipy.optimize import least_squares             # noqa: E402

import fald_fit as F                                 # noqa: E402
from dlc.fald.model import FaldModel, FaldParams, exp_knot_logw, knot_logw_from_decrements, knot_decrements_of  # noqa: E402
from dlc.fald.correct import load_fitted_params      # noqa: E402

SHIPPED = DATA / "fald_fit_result_area.json"
RAMPS_JSON = DATA / "sim" / "ramp" / "ramp_probe_173727.json"
HALO_JSON = DATA / "dark_halo" / "dark_halo_result.json"
METER_IMG = (1988, 1120)                             # the self-registered spot of the ramp + halo runs
RING_GROUPS = ("rings", "rings@1990", "rings@1990up", "rings@1950down", "rings@1950up",
               "rings@1990fine", "rings@1990finedown", "rings@1990fineup",
               "rings@1990mid", "rings@1990middown", "rings@1990midup")
FINE_GROUPS = ("rings@1990fine", "rings@1990finedown", "rings@1990fineup")
HELD_RING_GROUPS = ("rings", "rings@1990", "rings@1990up", "rings@1950down", "rings@1950up")
HALO_GREYS = (2.0, 5.0, 20.0)
HALO_GAPS = (120, 240, 480)
N_KNOT_FREE = 7                                      # 8 knots, logw_0 pinned at 0
MAX_JOBS = 8                                         # pool cap (owner directive 2026-09-13: the fit must not hog the machine)


# ------------------------------------------------------------------ image items (Evaluator extension)
_orig_predict = F.Evaluator.predict


def _predict(self, item):
    if "img" in item:
        meter = tuple(item.get("meter", METER_IMG))
        k = ("img", item["name"], meter)
        if k not in self.cache:
            y = float(self.model.meter_img(item["img"], meter).sum())
            kb = ("base", round(float(item["base_level"]), 6), meter)
            if kb not in self.cache:
                self.cache[kb] = float(self.model.meter_img(np.full_like(item["img"], item["base_level"]), meter).sum())
            self.cache[k] = y / max(self.cache[kb], 1e-9)
        return self.cache[k]
    return _orig_predict(self, item)


F.Evaluator.predict = _predict


def reduce5(img):                                    # (3, 2160, 3840) -> (3, 432, 768) block mean
    return img.reshape(3, 432, 5, 768, 5).mean(axis=(2, 4))


def ramp_items():
    import fald_ramp_frames as R
    run = json.load(open(RAMPS_JSON))
    items = []
    for r in run["results"]:
        if r["kind"] not in ("h", "hsteep", "v") or r.get("rel_off") is None:
            continue
        lo, hi, span, vert = {"h": (5.0, 200.0, 640, False), "hsteep": (5.0, 400.0, 320, False), "v": (5.0, 200.0, 360, True)}[r["kind"]]
        img = reduce5(R.ramp_frame(METER_IMG[0], METER_IMG[1], lo, hi, span, r["shift"], vert))
        items.append({"group": f"ramp@{r['kind']}", "name": f"{r['kind']}_s{r['shift']:02d}", "img": img,
                      "base_level": float(r["expected_nits"]), "y": 1.0 + float(r["rel_off"]), "base": "img", "w": 1.0,
                      "meter": METER_IMG})
    return items


def halo_items():
    import fald_dark_halo_probe as DH               # frame() only; nothing here touches hardware
    res = json.load(open(HALO_JSON))
    mx, my = res["meter"]
    flat = {r["grey"]: r for r in res["results"] if r["kind"] == "flat"}
    items = []
    for r in res["results"]:
        if r["kind"] != "bar" or r["grey"] not in HALO_GREYS or r["gap"] not in HALO_GAPS:
            continue
        f = flat[r["grey"]]
        y = r["xyz"]["off"][1] / f["xyz"]["off"][1]
        img = reduce5(DH.frame(mx, my, r["grey"], "bar", r["gap"]))
        items.append({"group": f"halo@{r['gap']}", "name": r["name"], "img": img, "base_level": float(r["grey"]),
                      "y": float(y), "base": "img", "w": 1.0, "meter": (mx, my),
                      "model_scale1_off": r["model_off"] / f["model_off"]})       # §33 numbers (scale 1) for reference
    return items


def build_all():
    """All items with harness weights (1/sqrt(n_group)); returns (items, index sets)."""
    ds = F.build_datasets()
    ramps = ramp_items(); halo = halo_items()
    items = ds + ramps + halo
    counts = {}
    for d in items:
        counts[d["group"]] = counts.get(d["group"], 0) + 1
    for d in items:
        d["w"] = 1.0 / np.sqrt(counts[d["group"]])
    sets = {
        "shapes": [i for i, d in enumerate(items) if d["group"] in RING_GROUPS and d["name"].startswith("L10:")],
        "images": [i for i, d in enumerate(items) if "img" in d],
        "held": [i for i, d in enumerate(items) if (d["group"] in RING_GROUPS and not d["name"].startswith("L10:"))
                 or d["group"] in ("comp", "orange", "rings@diag")],
    }
    sets["stage1"] = sets["shapes"]
    sets["stage2"] = sets["shapes"] + sets["images"]
    sets["all"] = sorted(set(sets["stage2"] + sets["held"]))
    return items, sets


# ------------------------------------------------------------------ params <-> dict / vector
def params_to_dict(p: FaldParams) -> dict:
    return {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in p.__dict__.items()}


def params_from_dict(d: dict) -> FaldParams:
    kw = dict(d)
    kw["drive_curve"] = [tuple(x) for x in kw["drive_curve"]]
    for k in ("chan_weights", "est_knot_cells", "est_knot_logw"):
        if k in kw:
            kw[k] = tuple(kw[k])
    return FaldParams(**{k: v for k, v in kw.items() if k in FaldParams.__dataclass_fields__})


def knots_start(base: FaldParams) -> FaldParams:
    """The shipped exp profile expressed as knots (identical estimate)."""
    cell_mm = base.cell_w * base.px_mm
    return replace(base, est_kind="knots", est_knot_logw=exp_knot_logw(base.est_scale_mm, base.est_knot_cells, cell_mm))


def x_of(p: FaldParams) -> np.ndarray:
    z = knot_decrements_of(p.est_knot_logw)
    return np.concatenate([z, [p.est_phase_px, p.est_phase_py, np.log(p.est_aniso), np.log(max(p.drive_dim, 1e-3))]])


def p_of(x: np.ndarray, base: FaldParams) -> FaldParams:
    return replace(base, est_kind="knots", est_knot_logw=knot_logw_from_decrements(x[:N_KNOT_FREE]),
                   est_phase_px=float(x[N_KNOT_FREE]), est_phase_py=float(x[N_KNOT_FREE + 1]),
                   est_aniso=float(np.exp(x[N_KNOT_FREE + 2])), drive_dim=float(np.exp(x[N_KNOT_FREE + 3])))


X_LO = np.concatenate([np.full(N_KNOT_FREE, -8.0), [-60.0, -60.0, np.log(0.3), np.log(1e-3)]])
X_HI = np.concatenate([np.full(N_KNOT_FREE, 3.0), [40.0, 40.0, np.log(3.0), np.log(0.6)]])


# ------------------------------------------------------------------ parallel prediction
_W = {}                                              # worker state


def _lower_priority():
    """Fit workers must not hammer the owner's machine: below-normal priority (psutil if present, else Win32)."""
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
        return
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL
        except Exception:
            pass


def _worker_init():
    _lower_priority()
    items, _ = build_all()
    _W["items"] = items
    _W["names"] = [d["name"] for d in items]
    _W["lru"] = []                                   # [(key, Evaluator)]


def _worker_task(args):
    pdict, ids = args
    key = json.dumps(pdict, sort_keys=True, default=float)
    ev = next((e for k, e in _W["lru"] if k == key), None)
    if ev is None:
        ev = F.Evaluator(params_from_dict(pdict))
        _W["lru"].append((key, ev))
        del _W["lru"][:-3]
    items = _W["items"]
    return [(i, ev.predict(items[i])) for i in ids]


class Predictor:
    """Predictions for many parameter sets × item ids across a process pool (or inline with jobs = 0)."""

    def __init__(self, items, jobs):
        self.items = items
        self.jobs = int(jobs)
        self.pool = None
        if self.jobs > 0:
            self.jobs = min(self.jobs, MAX_JOBS)
            self.pool = mp.Pool(self.jobs, initializer=_worker_init)
            # the workers must have built the SAME item list
            names = self.pool.apply(_names_task)
            assert names == [d["name"] for d in items], "worker/main item lists differ"
        else:
            _W["items"] = items; _W["lru"] = []

    def close(self):
        if self.pool is not None:
            self.pool.close(); self.pool.join(); self.pool = None

    def predict(self, plist: list[FaldParams], ids) -> np.ndarray:
        ids = list(ids)
        out = np.full((len(plist), len(ids)), np.nan)
        pos = {i: j for j, i in enumerate(ids)}
        pdicts = [params_to_dict(p) for p in plist]
        if self.pool is None:
            for a, pd in enumerate(pdicts):
                for i, v in _worker_task((pd, ids)):
                    out[a, pos[i]] = v
            return out
        nchunk = max(1, self.jobs // max(1, len(plist)))
        nchunk = min(nchunk, len(ids))
        chunks = [list(c) for c in np.array_split(np.array(ids), nchunk)]
        tasks = [(a, (pd, c)) for a, pd in enumerate(pdicts) for c in chunks if len(c)]
        results = self.pool.map(_worker_task, [t[1] for t in tasks], chunksize=1)
        for (a, _), res in zip(tasks, results):
            for i, v in res:
                out[a, pos[i]] = v
        return out


def _names_task():
    return _W["names"]


# ------------------------------------------------------------------ residuals, summaries, gate
def resid(items, ids, preds) -> np.ndarray:
    return np.array([items[i]["w"] * (np.log(max(pv, 1e-6)) - np.log(max(items[i]["y"], 1e-6))) for i, pv in zip(ids, preds)])


def summarise(items, ids, preds) -> dict:
    """Per-group mean|err| / max|err| in pp (ratio items) plus the pooled numbers the gate uses. Ring groups are
    split into "<group>|L10" (in-sample) and "<group>" (held-out levels)."""
    by = {}
    errs = {}
    for i, pv in zip(ids, preds):
        it = items[i]
        e = (pv / it["y"] - 1.0) * 100.0 if it["base"] is None else (pv - it["y"]) * 100.0
        # ring groups mix the in-sample grey-10 rows with the held-out levels: report them apart
        g = it["group"] + ("|L10" if it["group"] in RING_GROUPS and it["name"].startswith("L10:") else "")
        by.setdefault(g, []).append(e)
        errs[it["name"]] = (it["y"], pv, e)
    s = {"groups": {g: (float(np.mean(np.abs(v))), float(np.max(np.abs(v))), len(v)) for g, v in by.items()},
         "items": errs}

    def pooled(pred):
        v = [e for i, pv in zip(ids, preds) for e in [(pv - items[i]["y"]) * 100.0] if pred(items[i])]
        return (float(np.mean(np.abs(v))), float(np.max(np.abs(v))), len(v)) if v else (np.nan, np.nan, 0)

    s["rings_all_held"] = pooled(lambda it: it["group"] in HELD_RING_GROUPS and not it["name"].startswith("L10:"))
    s["fine_L10"] = pooled(lambda it: it["group"] in FINE_GROUPS and it["name"].startswith("L10:"))
    s["ramps_h_hsteep"] = pooled(lambda it: it["group"] in ("ramp@h", "ramp@hsteep"))
    s["bar120"] = {}
    for g in HALO_GREYS:
        nm = f"g{g:g}_bar120"
        if nm in errs:
            y, pv, e = errs[nm]
            s["bar120"][g] = {"panel": (y - 1) * 100, "model": (pv - 1) * 100, "gap": abs(pv - y) * 100}
    return s


def print_summary(title, s, before=None):
    print(f"\n== {title}")
    order = list(s["groups"].keys())
    for g in order:
        m, mx, n = s["groups"][g]
        b = f"  (before {before['groups'][g][0]:5.2f} / {before['groups'][g][1]:5.2f})" if before and g in before["groups"] else ""
        print(f"  [{g:<20}] n={n:3d}  mean|err| {m:6.2f}  max|err| {mx:6.2f}{b}")
    for k in ("rings_all_held", "fine_L10", "ramps_h_hsteep"):
        m, mx, n = s[k]
        b = f"  (before {before[k][0]:5.2f} / {before[k][1]:5.2f})" if before else ""
        print(f"  <{k:<20}> n={n:3d}  mean|err| {m:6.2f}  max|err| {mx:6.2f}{b}")
    for g, v in s["bar120"].items():
        b = f"  (before model {before['bar120'][g]['model']:+6.1f}, gap {before['bar120'][g]['gap']:4.1f})" if before else ""
        print(f"  bar120 @ {g:>4g} nit: panel {v['panel']:+6.1f} %  model {v['model']:+6.1f} %  gap {v['gap']:4.1f} pp{b}")


def print_items(items, ids, preds, groups):
    for i, pv in zip(ids, preds):
        it = items[i]
        if it["group"] in groups:
            e = (pv - it["y"]) * 100.0
            print(f"     {it['name']:<22} meas={it['y']:8.4f}  pred={pv:8.4f}  err={e:+7.2f}")


GATE_TOL_HELD = 0.2


def gate(before, after) -> list[tuple[str, str, bool]]:
    rows = []
    for g in ("rings", "rings@1990", "rings@1990up", "rings@1950down", "rings@1950up", "rings@diag", "comp", "orange"):
        if g in before["groups"]:
            b, a = before["groups"][g][0], after["groups"][g][0]
            rows.append((f"held {g}", f"{b:.2f} -> {a:.2f} (limit {b + GATE_TOL_HELD:.2f})", a <= b + GATE_TOL_HELD + 1e-9))
    b, a = before["rings_all_held"][0], after["rings_all_held"][0]
    rows.append(("held rings-all", f"{b:.2f} -> {a:.2f} (limit {b + GATE_TOL_HELD:.2f})", a <= b + GATE_TOL_HELD + 1e-9))
    for g in (5.0, 20.0):
        bg, ag = before["bar120"][g]["gap"], after["bar120"][g]["gap"]
        rows.append((f"bar120 gap @{g:g} nit halves", f"{bg:.1f} -> {ag:.1f} (limit {bg / 2:.1f})", ag <= bg / 2 + 1e-9))
    a = after["ramps_h_hsteep"][0]
    rows.append(("ramps h/hsteep <= 1.0 pp", f"{before['ramps_h_hsteep'][0]:.2f} -> {a:.2f}", a <= 1.0))
    b, a = before["fine_L10"][0], after["fine_L10"][0]
    rows.append(("fine-sweep rings not worse", f"{b:.2f} -> {a:.2f}", a <= b + 0.05))
    return rows


def print_gate(tag, rows):
    ok = all(r[2] for r in rows)
    print(f"\n== GATE [{tag}]: {'PASS' if ok else 'FAIL'}")
    for name, txt, p in rows:
        print(f"   {'ok  ' if p else 'FAIL'} {name:<32} {txt}")
    return ok


# ------------------------------------------------------------------ the fit
def fit(pred: Predictor, items, ids, p0: FaldParams, tag, nfev, diff_step, log, xtol=1e-3, ftol=1e-3, x_scale=1.0):
    x0 = np.clip(x_of(p0), X_LO + 1e-6, X_HI - 1e-6)
    t0 = time.time()
    n_eval = [0]
    cache = {}

    def kw_line(p):
        lw = np.array(p.est_knot_logw)
        return (f"knots(w)={np.array2string(np.exp(lw), precision=3, separator=',', max_line_width=200)} "
                f"phase=({p.est_phase_px:+.1f},{p.est_phase_py:+.1f}) aniso={p.est_aniso:.3f} dim={p.drive_dim:.4f}")

    def fun(x):
        k = x.tobytes()
        if k not in cache:
            p = p_of(x, p0)
            r = resid(items, ids, pred.predict([p], ids)[0])
            cache[k] = r
            n_eval[0] += 1
            log(f"   [{tag}] eval {n_eval[0]:3d}  rms={np.sqrt(np.mean(r * r)):.5f}  {kw_line(p)}  ({time.time() - t0:.0f}s)")
        return cache[k]

    def jac(x):
        h = diff_step * np.maximum(1.0, np.abs(x))
        xs = []
        for i in range(len(x)):
            xi = x.copy(); xi[i] += h[i]
            if xi[i] > X_HI[i]:
                h[i] = -h[i]; xi[i] = x[i] + h[i]
            xs.append(xi)
        plist = [p_of(x, p0)] + [p_of(xi, p0) for xi in xs]
        preds = pred.predict(plist, ids)
        r0 = resid(items, ids, preds[0])
        cache[x.tobytes()] = r0
        J = np.empty((len(r0), len(x)))
        for i in range(len(x)):
            J[:, i] = (resid(items, ids, preds[i + 1]) - r0) / h[i]
        log(f"   [{tag}] jac  ({len(x)} cols, {time.time() - t0:.0f}s)")
        return J

    res = least_squares(fun, x0, jac=jac, bounds=(X_LO, X_HI), max_nfev=nfev, xtol=xtol, ftol=ftol, x_scale=x_scale)
    p = p_of(res.x, p0)
    log(f"   [{tag}] done: status {res.status} ({res.message}), nfev {res.nfev}, njev {res.njev}, "
        f"rms {np.sqrt(np.mean(res.fun ** 2)):.5f}, {time.time() - t0:.0f}s")
    return p, float(np.sqrt(np.mean(res.fun ** 2)))


def profile_table(p: FaldParams, base: FaldParams) -> str:
    cell_mm = p.cell_w * p.px_mm
    ex = np.array(exp_knot_logw(base.est_scale_mm, p.est_knot_cells, cell_mm))
    lw = np.array(p.est_knot_logw) if len(p.est_knot_logw) else ex
    lines = ["   r[cells]  r[mm]   w_knots   w_exp(shipped)   ratio"]
    for c, r, a, b in zip(p.est_knot_cells, np.array(p.est_knot_cells) * cell_mm, np.exp(lw - lw[0]), np.exp(ex - ex[0])):
        lines.append(f"   {c:7.2f}  {r:6.2f}   {a:8.4f}   {b:8.4f}      {a / b:6.3f}")
    slope = (lw[-1] - lw[-2]) / ((p.est_knot_cells[-1] - p.est_knot_cells[-2]) * cell_mm)
    lines.append(f"   continuation 1/e length beyond the last knot: {(-1 / slope if slope < 0 else float('inf')):.2f} mm "
                 f"(exp: {base.est_scale_mm:.2f} mm)")
    return "\n".join(lines)


def halo_scale1(p: FaldParams) -> dict:
    """§33-comparable numbers: the halo bars at FULL resolution (scale 1), OFF/flat at the meter, per grey/gap."""
    import fald_dark_halo_probe as DH
    res = json.load(open(HALO_JSON))
    mx, my = res["meter"]
    m = FaldModel(replace(p, scale=1))
    mask = m.aperture_mask((mx, my))
    out = {}
    for g in HALO_GREYS:
        flat = float(m.forward_img(np.full((3, 2160, 3840), g))["y"].sum(axis=0)[mask].mean())
        for gap in HALO_GAPS:
            y = float(m.forward_img(DH.frame(mx, my, g, "bar", gap))["y"].sum(axis=0)[mask].mean())
            out[(g, gap)] = (y / flat - 1.0) * 100.0
    return out


def save_candidate(path: Path, p: FaldParams, extra: dict):
    out = {"params": params_to_dict(p), "stage_b_knots": extra}
    path.write_text(json.dumps(out, indent=1, default=float))


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default="all", choices=("all", "check", "1", "2", "s6"))
    ap.add_argument("--params", type=Path, default=None, help="start (or check) params json; default = shipped")
    ap.add_argument("--nfev", type=int, default=60)
    ap.add_argument("--jobs", type=int, default=min(MAX_JOBS, os.cpu_count() or 1), help=f"worker processes (capped at {MAX_JOBS}, below-normal priority)")
    ap.add_argument("--diff-step", type=float, default=0.05)
    ap.add_argument("--xtol", type=float, default=1e-3, help="least_squares xtol (1e-3 stops early: |x| ~ 30 with the phase in px)")
    ap.add_argument("--ftol", type=float, default=1e-3)
    ap.add_argument("--x-scale", default="1", help="least_squares x_scale: a number or 'jac'")
    ap.add_argument("--ramp-weight", type=float, default=1.0, help="extra weight on the ramp image items (trade-off study; the fit proper uses 1)")
    ap.add_argument("--out", type=Path, default=DATA / "stat_fit")
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-scale1", action="store_true", help="skip the full-resolution halo evaluation of the candidates")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)

    def log(msg=""):
        print(msg, flush=True)

    t_start = time.time()
    items, sets = build_all()
    if a.ramp_weight != 1.0:
        for d in items:
            if d["group"].startswith("ramp@"):
                d["w"] *= a.ramp_weight
        log(f"ramp items weighted x{a.ramp_weight:g}")
    log(f"items: {len(items)} total; in-sample shapes {len(sets['shapes'])}, images {len(sets['images'])}, held-out {len(sets['held'])}")
    groups = {}
    for d in items:
        groups[d["group"]] = groups.get(d["group"], 0) + 1
    log("groups: " + ", ".join(f"{g}={n}" for g, n in sorted(groups.items())))
    pred = Predictor(items, a.jobs)
    log(f"predictor: {a.jobs} workers ready ({time.time() - t_start:.0f}s)")

    shipped = load_fitted_params(SHIPPED)
    start = load_fitted_params(a.params) if a.params else shipped
    eval_ids = sets["all"]

    def evaluate(p, title, before=None, items_of=()):
        pv = pred.predict([p], eval_ids)[0]
        s = summarise(items, eval_ids, pv)
        print_summary(title, s, before)
        if items_of:
            print_items(items, eval_ids, pv, items_of)
        r_in = resid(items, sets["stage2"], pred.predict([p], sets["stage2"])[0])
        r_sh = resid(items, sets["shapes"], pred.predict([p], sets["shapes"])[0])
        s["rms_stage2"] = float(np.sqrt(np.mean(r_in ** 2))); s["rms_shapes"] = float(np.sqrt(np.mean(r_sh ** 2)))
        log(f"  weighted rms: shapes {s['rms_shapes']:.5f}  shapes+images {s['rms_stage2']:.5f}")
        return s

    # BEFORE: shipped params on everything
    before = evaluate(shipped, "BEFORE (shipped fald_fit_result_area.json)", items_of=("ramp@h", "ramp@hsteep", "ramp@v", "halo@120", "halo@240", "halo@480"))
    # pipeline sanity: the rejected ramp refit's params must reproduce §31a's AFTER numbers
    ramps_fit = DATA / "fald_fit_result_ramps.json"
    if ramps_fit.exists():
        evaluate(load_fitted_params(ramps_fit), "SANITY: fit_ramps.py AFTER params (§31a: ramps h 0.65 / hsteep 0.50 / v 0.50; "
                 "held rings 2.31 max 14.7, rings@1990 1.47, 1950up 1.65, diag 0.60, comp 3.63, orange 8.5)", before)
    if a.params:
        evaluate(start, f"START params ({a.params})", before)
    if a.stage == "check":
        pred.close(); return 0

    candidates = {}
    p_start = knots_start(start) if start.est_kind != "knots" else start
    evaluate(p_start, "knots start (must equal the start params)", before)

    def run_stage(tag, p0, ids):
        log(f"\n===== STAGE {tag}: {len(ids)} items, from {kw_short(p0)} =====")
        p, rms = fit(pred, items, ids, p0, tag, a.nfev, a.diff_step, log, a.xtol, a.ftol,
                     "jac" if a.x_scale == "jac" else float(a.x_scale))
        s = evaluate(p, f"AFTER stage {tag}", before, items_of=("ramp@h", "ramp@hsteep", "ramp@v", "halo@120", "halo@240", "halo@480"))
        log("\n  fitted estimate profile vs the shipped exponential:\n" + profile_table(p, shipped))
        rows = gate(before, s)
        ok = print_gate(tag, rows)
        extra = {"rms": rms, "kind": "knots", "stage": tag, "est_knot_cells": list(p.est_knot_cells), "est_knot_logw": list(p.est_knot_logw),
                 "est_phase_px": p.est_phase_px, "est_phase_py": p.est_phase_py, "est_aniso": p.est_aniso, "drive_dim": p.drive_dim,
                 "est_support_cells": p.est_support_cells, "gate_pass": ok, "gate": [(n, t, bool(v)) for n, t, v in rows],
                 "summary_groups": s["groups"], "rings_all_held": s["rings_all_held"], "fine_L10": s["fine_L10"],
                 "ramps_h_hsteep": s["ramps_h_hsteep"], "bar120": {str(k): v for k, v in s["bar120"].items()}}
        fn = a.out / f"knots_stage{tag}{('_' + a.tag) if a.tag else ''}.json"
        save_candidate(fn, p, extra)
        log(f"  saved {fn}")
        candidates[tag] = (p, s, ok, extra)
        return p

    def kw_short(p):
        return f"kind={p.est_kind} support={p.est_support_cells} phase=({p.est_phase_px:+.1f},{p.est_phase_py:+.1f}) aniso={p.est_aniso:.3f} dim={p.drive_dim:.4f}"

    p1 = p2 = None
    if a.stage in ("all", "1"):
        p1 = run_stage("1", p_start, sets["stage1"])
    if a.stage in ("all", "2"):
        p2 = run_stage("2", p1 if p1 is not None else p_start, sets["stage2"])
    if a.stage in ("all", "s6"):
        p0 = p2 if p2 is not None else (p1 if p1 is not None else p_start)
        run_stage("s6", replace(p0, est_support_cells=6), sets["stage2"])

    # verdict
    log("\n===== VERDICT =====")
    passing = {k: v for k, v in candidates.items() if v[2]}
    for k, (p, s, ok, extra) in candidates.items():
        log(f"  stage {k}: gate {'PASS' if ok else 'FAIL'}; rms shapes+images {s['rms_stage2']:.5f}; held rings-all {s['rings_all_held'][0]:.2f} "
            f"(shipped {before['rings_all_held'][0]:.2f}); ramps h/hsteep {s['ramps_h_hsteep'][0]:.2f}; "
            f"bar120 gap @5 {s['bar120'][5.0]['gap']:.1f} @20 {s['bar120'][20.0]['gap']:.1f} (shipped {before['bar120'][5.0]['gap']:.1f}/{before['bar120'][20.0]['gap']:.1f})")
    if not a.no_scale1:
        log("\n  full-resolution (scale 1) halo bars, OFF/flat − 1 in %, model vs panel (§33 table):")
        panel = {(it["base_level"], int(it["group"].split("@")[1])): (it["y"] - 1) * 100 for it in items if it["group"].startswith("halo@")}
        rows = {"shipped": halo_scale1(shipped)}
        for k, (p, s, ok, extra) in candidates.items():
            rows[f"stage {k}"] = halo_scale1(p)
        hdr = "   grey  gap   panel  " + "  ".join(f"{k:>9}" for k in rows)
        log(hdr)
        for g in HALO_GREYS:
            for gap in HALO_GAPS:
                log(f"   {g:4g}  {gap:3d}  {panel[(g, gap)]:+6.1f}  " + "  ".join(f"{rows[k][(g, gap)]:+9.1f}" for k in rows))
    if passing:
        best = min(passing.items(), key=lambda kv: kv[1][1]["rms_stage2"])
        k, (p, s, ok, extra) = best
        fn = DATA / "fald_fit_result_knots.json"
        save_candidate(fn, p, extra)
        log(f"\n  ACCEPTED: stage {k} -> {fn}")
    else:
        log("\n  NO candidate passed the gate.")
    pred.close()
    log(f"total {time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
