"""The practical view of a measurement stage — what the DLC tab's charts show (stdlib only, like the fold).

Built for a person who acts on it, not for a benchmark (owner, 2026-10-15: "practical info over benchmark e-peen
info"; three-model review the same day). Everything is computed from the folded CIE points / grey rows of one stage:

* ``bands`` — error by LUMINANCE BAND, weighted by where content lives. With per-patch content weights (a
  content-sampled verify set) the band width is the weight share and every number is weighted; otherwise the
  width is the film-pixel share from the 2026-10-09 content survey (``results/practical_score_2026-10-09``:
  HDR live action 51.9 / 25.7 / 17.9 / 2.7 / 1.8 % at < 1 / 1–10 / 10–100 / 100–203 / > 203 nit; SDR live
  action ≈ 33 / 28 / 38 / 1 % at < 1 / 1–10 / 10–100 / > 100 nit) and patches inside a band count equally.
  Per band: mean / p95 ΔE, the signed mean lightness error, and the RMS lightness / chroma / hue error (what KIND
  of error: their squares split the band's mean squared ΔE).
* ``offset_L`` — the shared lightness offset above 1 nit: one number (often inside the meter's own accuracy),
  not a curve shape.
* ``hues`` — 12 sectors of the INTENDED colour's hue (sRGB-named): signed mean chroma / hue error, mean ΔE.
* ``worst`` — the misses that matter: ranked by content weight × ΔE (or band share × ΔE), described in words
  ("0.3 nit · red 75 %"), with the dominant error component; anchors / clamped / limits listed apart.
* ``limits`` — panel limits read off the greys: the brightest neutral (tint ≤ 1), the peak reached vs the target,
  the black floor, the white at 203 nit (HDR) or the top grey (SDR).
"""

from __future__ import annotations

import colorsys
import math
from typing import Any, Optional, Sequence

INF = float("inf")
HDR_BANDS = (("<1", 0.0, 1.0), ("1-10", 1.0, 10.0), ("10-100", 10.0, 100.0), ("100-203", 100.0, 203.0),
             (">203", 203.0, INF))
SDR_BANDS = (("<1", 0.0, 1.0), ("1-10", 1.0, 10.0), ("10-100", 10.0, 100.0), (">100", 100.0, INF))
SURVEY_SHARE = {"hdr": (0.519, 0.257, 0.179, 0.027, 0.018), "sdr": (0.33, 0.28, 0.38, 0.01)}
NEUTRAL_TINT = 1.0          # a grey whose tint is under this (metric units) reads neutral
HUE_NAMES = ("red", "orange", "yellow", "lime", "green", "spring", "cyan", "azure", "blue", "violet",
             "magenta", "rose")


def _f(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _r(v: Optional[float], nd: int = 3) -> Optional[float]:
    return None if v is None else round(v, nd)


def _wmean(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw > 0 else None


def _wrms(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    m = _wmean([(v * v, w) for v, w in pairs])
    return math.sqrt(m) if m is not None else None


def _wpct(pairs: Sequence[tuple[float, float]], q: float) -> Optional[float]:
    if not pairs:
        return None
    s = sorted(pairs)
    total = sum(w for _, w in s)
    if total <= 0:
        return None
    acc = 0.0
    for v, w in s:
        acc += w
        if acc >= q * total:
            return v
    return s[-1][0]


def band_of(nits: Optional[float], bands: Sequence[tuple]) -> Optional[int]:
    if nits is None:
        return None
    for i, (_, lo, hi) in enumerate(bands):
        if lo <= nits < hi:
            return i
    return len(bands) - 1


def signal_hue(sig: Optional[Sequence[float]]) -> tuple[Optional[int], float]:
    """(12-sector index of the intended colour's hue, its saturation 0..1); (None, 0) for a grey."""
    try:
        r, g, b = (max(0.0, min(1.0, float(c))) for c in sig[:3])  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return None, 0.0
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.08:
        return None, s
    return int(((h * 360.0 + 15.0) % 360.0) // 30.0), s


def describe(p: dict, hdr: bool) -> str:
    """'0.3 nit · red 75 %' / '412 nit · grey' — what the patch IS, in words a person can place."""
    ty = _f(p.get("tY"))
    nits = "—" if ty is None else (f"{ty:.0f}" if ty >= 100 else f"{ty:.1f}" if ty >= 1 else
                                   f"{ty:.2f}" if ty >= 0.1 else f"{ty:.3f}")
    sector, sat = signal_hue(p.get("sig"))
    what = "grey" if sector is None else f"{HUE_NAMES[sector]} {round(sat * 100 / 5) * 5:.0f} %"
    return f"{nits} nit · {what}"


def _weight(p: dict, band: int, counts: Sequence[int], shares: Sequence[float], weighted: bool) -> float:
    if weighted:
        return _f(p.get("w")) or 0.0
    return shares[band] / counts[band] if counts[band] else 0.0


def summarize(points: Sequence[dict], grays: Sequence[dict], *, hdr: bool,
              target_peak: Optional[float] = None, black_floor: Optional[float] = None) -> dict:
    """The practical view of one stage's FRESH points (carried ones belong to an earlier stage)."""
    bands = HDR_BANDS if hdr else SDR_BANDS
    pts = [p for p in points if not p.get("carried") and _f(p.get("de")) is not None and _f(p.get("tY")) is not None]
    weighted = any(_f(p.get("w")) is not None for p in pts)
    if weighted:
        pts = [p for p in pts if _f(p.get("w")) is not None]
    idx = [band_of(_f(p["tY"]), bands) for p in pts]
    counts = [sum(1 for i in idx if i == b) for b in range(len(bands))]
    prior = SURVEY_SHARE["hdr" if hdr else "sdr"]
    total_w = sum(_f(p.get("w")) or 0.0 for p in pts) if weighted else 1.0
    band_rows = []
    for b, (label, lo, hi) in enumerate(bands):
        mine = [(p, _weight(p, b, counts, prior, weighted)) for p, i in zip(pts, idx) if i == b]
        share = (sum(w for _, w in mine) / total_w if total_w > 0 else 0.0) if weighted else prior[b]
        de = [(_f(p["de"]), w) for p, w in mine]
        band_rows.append({
            "label": label, "lo": lo, "hi": None if hi == INF else hi, "share": _r(share, 4), "n": len(mine),
            "mean": _r(_wmean(de)), "p95": _r(_wpct(de, 0.95)),
            "L": _r(_wmean([(_f(p.get("L")) or 0.0, w) for p, w in mine])),
            "Lr": _r(_wrms([(_f(p.get("L")) or 0.0, w) for p, w in mine])),
            "C": _r(_wrms([(_f(p.get("C")) or 0.0, w) for p, w in mine])),
            "H": _r(_wrms([(_f(p.get("H")) or 0.0, w) for p, w in mine])),
        })
    covered = [r for r in band_rows if r["n"] and r["mean"] is not None]
    cov_share = sum(r["share"] or 0.0 for r in covered)
    score = (sum((r["share"] or 0.0) * r["mean"] for r in covered) / cov_share) if cov_share > 0 else None
    bright = [(_f(p.get("L")) or 0.0, _weight(p, i, counts, prior, weighted))
              for p, i in zip(pts, idx) if (_f(p["tY"]) or 0.0) >= 1.0 and _f(p.get("L")) is not None]

    # hue sectors (intended colour)
    hues = []
    for s in range(12):
        mine = []
        for p, i in zip(pts, idx):
            sector, _ = signal_hue(p.get("sig"))
            if sector == s:
                mine.append((p, _weight(p, i, counts, prior, weighted)))
        if not mine:
            hues.append({"sector": s, "name": HUE_NAMES[s], "n": 0})
            continue
        hues.append({"sector": s, "name": HUE_NAMES[s], "n": len(mine),
                     "w": _r(sum(w for _, w in mine) / (total_w if weighted and total_w > 0 else 1.0), 4),
                     "de": _r(_wmean([(_f(p["de"]), w) for p, w in mine])),
                     "C": _r(_wmean([(_f(p.get("C")) or 0.0, w) for p, w in mine])),
                     "H": _r(_wmean([(_f(p.get("H")) or 0.0, w) for p, w in mine]))})

    # the misses that matter
    rows = []
    for p, i in zip(pts, idx):
        e = _f(p["de"])
        w = _weight(p, i, counts, prior, weighted)
        comps = {k: abs(_f(p.get(k)) or 0.0) for k in ("L", "C", "H")}
        rows.append({"desc": describe(p, hdr), "de": _r(e), "impact": _r(e * w, 5), "w": _r(w, 5),
                     "comp": max(comps, key=comps.get) if any(comps.values()) else None,
                     "zone": p.get("zone") or "core", "dY": _r(_f(p.get("dY")), 1),
                     "sc": p.get("lsc") or p.get("sc"), "mc": p.get("lmc") or p.get("mc"), "label": p.get("label")})
    main = sorted((r for r in rows if r["zone"] == "core"), key=lambda r: r["impact"] or 0.0, reverse=True)[:12]
    apart = sorted((r for r in rows if r["zone"] != "core"), key=lambda r: r["de"] or 0.0, reverse=True)[:6]

    # panel limits from the greys
    fresh = [g for g in grays if not g.get("carried") and _f(g.get("Y")) is not None]
    neutral = [g for g in fresh if _f(g.get("tw")) is not None and _f(g.get("tm")) is not None
               and math.hypot(g["tw"], g["tm"]) <= NEUTRAL_TINT and (_f(g.get("Y")) or 0) >= 1.0]
    top = max(fresh, key=lambda g: g["Y"], default=None)
    ref = 203.0 if hdr else None
    white = None
    if fresh:
        if hdr:
            cand = [g for g in fresh if _f(g.get("tY")) is not None]
            white = min(cand, key=lambda g: abs(math.log(max(g["tY"], 1e-6) / ref)), default=None)
        else:
            white = top
    limits = {
        "peak": _r(_f(top["Y"]), 1) if top else None,
        "target_peak": _r(_f(target_peak), 1),
        "neutral_peak": _r(max((_f(g["Y"]) for g in neutral), default=None), 1),
        "black": _r(_f(black_floor) if _f(black_floor) is not None else
                    (min((_f(g["Y"]) for g in fresh if (_f(g["Y"]) or 0) > 0), default=None)), 5),
        "white": None if not white else {
            "Y": _r(_f(white.get("Y")), 1), "tY": _r(_f(white.get("tY")), 1), "de": _r(_f(white.get("de"))),
            "cct": _r(_f(white.get("cct")), 0), "duv": _r(_f(white.get("duv")), 4),
            "tw": _r(_f(white.get("tw")), 2), "tm": _r(_f(white.get("tm")), 2), "dY": _r(_f(white.get("dY")), 2)},
    }
    return {"weighting": "content" if weighted else "survey", "bands": band_rows, "score": _r(score),
            "coverage": _r(cov_share, 4), "offset_L": _r(_wmean(bright)), "n": len(pts),
            "hues": hues, "worst": main, "worst_apart": apart, "limits": limits}
