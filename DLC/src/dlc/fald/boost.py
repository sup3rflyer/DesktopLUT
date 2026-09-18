"""Black-frame LED boost table → the model's step LUT (work guide "HW 2026-09-18" items 2 / 2a, ticket P9).

The firmware multiplies every LED drive by a staircase function of the number of NON-BLACK zones of the frame
(:attr:`dlc.fald.model.FaldParams.boost_lut`). The probe measures it as (N, boost) points — the sensor at the centre
of a bright window, the zone count walked with dim far content, the reading divided by the same window on a fully
non-black frame. This module turns those points into the step function the model evaluates: consecutive points whose
boost agrees within ``merge_tol`` are one step; a step's lower edge is the midpoint between the last point of the
previous step and its own first point (the true edge lies somewhere between the two — the resolution of the walk).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

MERGE_TOL = 0.005          # boosts closer than this are the same firmware step (read repeatability ~0.1 %, drift ~0.3 %, level spread ~0.5 %)
UNITY_TOL = 0.003          # a step this close to 1 IS 1 (reference drift); keeps flat fields exactly boost-free


def build_boost_lut(points: Iterable[Sequence[float]], zones_total: int, merge_tol: float = MERGE_TOL,
                    ) -> tuple[tuple[float, float], ...]:
    """((zone_fraction_lo, boost), ...) ascending from measured (N, boost) points. The first step starts at 0;
    each step carries the MEAN boost of its points. Points with the same N are averaged first."""
    by_n: dict[int, list[float]] = {}
    for n, b in points:
        by_n.setdefault(int(n), []).append(float(b))
    pts = sorted((n, sum(v) / len(v)) for n, v in by_n.items())
    if not pts:
        return ()
    steps: list[dict] = []
    for n, b in pts:
        if steps and abs(b - steps[-1]["sum"] / steps[-1]["k"]) <= merge_tol:
            steps[-1]["sum"] += b
            steps[-1]["k"] += 1
            steps[-1]["n_hi"] = n
        else:
            steps.append({"n_lo": n, "n_hi": n, "sum": b, "k": 1})
    out = []
    for i, s in enumerate(steps):
        lo = 0.0 if i == 0 else 0.5 * (steps[i - 1]["n_hi"] + s["n_lo"]) / float(zones_total)
        b = s["sum"] / s["k"]
        out.append((lo, 1.0 if abs(b - 1.0) <= UNITY_TOL else b))
    return tuple(out)


def load_boost_table(path: Path | str, *, mode: str | None = None, zones_total: int | None = None,
                     merge_tol: float = MERGE_TOL) -> dict:
    """The FaldParams keywords of a ``boost_table.json`` — ``boost_lut`` plus the zone-activation statistic the table's
    zone counts were made with (``boost_stat_gamma`` / ``boost_thr``, when the table names them). ``mode`` ("HDR" /
    "SDR") and ``zones_total`` of the run are CHECKED: the law was measured per panel AND per mode (SDR is unmeasured),
    and a fraction LUT of another lattice is meaningless. Raises ValueError on a mismatch."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if mode is not None and d.get("mode") and str(d["mode"]).upper() != str(mode).upper():
        raise ValueError(f"boost table {path} was measured in {d['mode']}, the run is {mode}")
    if zones_total is not None and int(d.get("zones_total", zones_total)) != int(zones_total):
        raise ValueError(f"boost table {path} is for {d.get('zones_total')} zones, the run has {zones_total}")
    kw: dict = {"boost_lut": load_boost_lut(path, merge_tol)}
    for k in ("boost_stat_gamma", "boost_thr"):
        if d.get(k) is not None:
            kw[k] = float(d[k])
    return kw


def load_boost_lut(path: Path | str, merge_tol: float = MERGE_TOL) -> tuple[tuple[float, float], ...]:
    """The LUT of a ``boost_table.json`` (``rows`` = [{"N", "boost"}, ...], ``zones_total``); rows flagged
    ``"exclude": true`` (a zone count the probe could not know) are skipped."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    pts = [(r["N"], r["boost"]) for r in d["rows"] if not r.get("exclude")]
    return build_boost_lut(pts, int(d["zones_total"]), merge_tol)
