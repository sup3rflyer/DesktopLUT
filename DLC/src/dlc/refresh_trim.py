"""Per-refresh-rate grey trim: the time-bracketed ratio analysis, and the trim curves a later MHC bake reads.

The panel's light at refresh R, relative to 60 Hz, for each full-field patch. The drift between readings is as large
as the effect (docs/refresh-trim-design.md section 1), so every non-60 reading is set against the 60-Hz value
INTERPOLATED IN TIME between the 60-Hz legs on either side of it, patch by patch.

Input: one row per measured patch, the probe's ``trim_rows.jsonl``:
    {leg, pass, rate ("60" | "47.952" | ...), rate_hz (the switch read-back), patch, kind ("grey" | "colour"),
     code [r, g, b], nominal_nits, t_s (the kept reads' mid time), y, xyz, sd_y, n_kept, settled}

Output:
  * ``bracket_ratios``: per non-60 row, the ratio with its uncertainty: read noise plus the drift-interpolation
    error, the latter estimated from how well the 60-Hz series predicts its own interior points (leave-one-out);
    plus the chromaticity deltas.
  * ``combine``: per (rate, patch), the weighted mean over repeats, inflated by the Birge ratio when the repeats
    disagree beyond their stated uncertainty.
  * ``predictability``: ln(ratio) against frame period (and against frequency) per level; the 59.94 control, the
    48 / 47.952 equality, the 50-Hz linearity point and the one-shape (separability) test, judged by the frozen
    thresholds (PREREG_REFRESH.md).
  * ``trim_curves``: per measured rate, knots of the gain g_R = 1 / ratio against the 60-Hz light, with the hold /
    extend rules, plus the frame-period model for unmeasured rates when linearity holds. ``gain_at`` and
    ``trim_for_hz`` evaluate it, so a bake needs nothing else.

Pure: no I/O apart from ``load_rows``, and nothing imported beyond the standard library and ``dlc._pq``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ._pq import oetf_norm

SCHEMA = "dlc.refresh_trim/1"
REF_RATE = "60"
REF_HZ = 60.0
# The frozen decision thresholds (PREREG_REFRESH.md section 5): k sigma, plus an absolute floor for "equal" tests
# (0.1 % = the drift-interpolation class of a 6-min bracket; read noise is ~0.01 %).
K_SIGMA = 2.0
FLOOR_LN = 0.001
XY_TOL = 0.001                    # greys at >= 2 nit
XY_TOL_DARK = 0.002               # the 0.5-nit grey (i1D3 chromaticity scatter there)
XY_TOL_COLOUR = 0.002
# a primary/secondary vs grey 520 at the same rate: 0.5 % (~0.3 dE_ITP at 100 nit). The MHC matrix sets each
# colour's drive, which can differ from grey 520's; at the effect's slope (~0.014 ln per decade) a factor-2 drive
# difference is ~0.004 already.
CHANNEL_FLOOR_LN = 0.005
SEPARABLE_P_MIN = 0.01            # chi-square p-value of one curve shape across levels


def _rate_hz_nominal(rate: str) -> float:
    return {"60": 60.0, "59.94": 60000 / 1001, "50": 50.0, "48": 48.0, "47.952": 48000 / 1001}.get(rate, float(rate))


def load_rows(path: Path | str) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _xy(xyz: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    s = sum(xyz) if xyz else 0.0
    return (xyz[0] / s, xyz[1] / s) if s > 0 else (None, None)


def _rel_se(r: dict[str, Any]) -> float:
    """Relative standard error of one patch's mean (sd / sqrt(n) / y); 2e-4 when the row has no spread."""
    y, sd, n = r.get("y"), r.get("sd_y"), int(r.get("n_kept") or 0)
    if not y or y <= 0:
        return float("inf")
    if sd is None or n < 2:
        return 2e-4
    return float(sd) / math.sqrt(n) / float(y)


def _usable(r: dict[str, Any]) -> bool:
    return bool(r.get("y")) and float(r["y"]) > 0 and r.get("xyz") is not None


def series(rows: Iterable[dict[str, Any]], patch: str, rate: str = REF_RATE) -> list[dict[str, Any]]:
    return sorted((r for r in rows if r.get("patch") == patch and r.get("rate") == rate and _usable(r)),
                  key=lambda r: float(r["t_s"]))


def loo_residuals(s60: Sequence[dict[str, Any]]) -> list[float]:
    """For each interior 60-Hz point: its relative miss when predicted by linear interpolation between its two
    neighbours (each neighbour two bracket-gaps apart, so this overstates the one-gap error; see u_interp)."""
    out = []
    for k in range(1, len(s60) - 1):
        a, m, b = s60[k - 1], s60[k], s60[k + 1]
        ta, tm, tb = float(a["t_s"]), float(m["t_s"]), float(b["t_s"])
        if tb <= ta:
            continue
        w = (tm - ta) / (tb - ta)
        pred = float(a["y"]) + (float(b["y"]) - float(a["y"])) * w
        out.append(float(m["y"]) / pred - 1.0)
    return out


def u_interp(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The drift-interpolation uncertainty per patch (relative): RMS of the leave-one-out residuals / sqrt(2).
    A random-walk drift's interpolation error grows as the square root of the span, and the LOO span is twice a
    bracket's. A patch with fewer than 4 residuals uses the pooled RMS of its kind."""
    patches = sorted({r["patch"] for r in rows})
    own: dict[str, list[float]] = {p: loo_residuals(series(rows, p)) for p in patches}
    kind = {r["patch"]: r.get("kind", "grey") for r in rows}
    pooled: dict[str, list[float]] = {}
    for p, res in own.items():
        pooled.setdefault(kind[p], []).extend(res)
    out = {}
    for p in patches:
        res = own[p]
        src = "own"
        if len(res) < 4:
            res, src = pooled.get(kind[p], []), f"pooled_{kind[p]}"
        rms = math.sqrt(sum(v * v for v in res) / len(res)) if res else 0.002
        out[p] = {"u": rms / math.sqrt(2.0), "rms_loo": rms, "n": len(res), "source": src if res else "default_0.2%"}
    return out


def bracket_ratios(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each non-60 row against the 60-Hz value of the SAME patch, interpolated linearly in time between the nearest
    60-Hz rows before and after it. A row without a 60-Hz reading on both sides is not used (listed, unbracketed)."""
    ui = u_interp(rows)
    out = []
    for r in rows:
        if r.get("rate") == REF_RATE or not _usable(r):
            continue
        s60 = series(rows, r["patch"])
        t = float(r["t_s"])
        before = [a for a in s60 if float(a["t_s"]) < t]
        after = [b for b in s60 if float(b["t_s"]) > t]
        rec: dict[str, Any] = {k: r.get(k) for k in ("leg", "pass", "rate", "rate_hz", "patch", "kind", "code",
                                                       "nominal_nits", "t_s", "y", "settled")}
        if not before or not after:
            rec["unbracketed"] = True
            out.append(rec)
            continue
        a, b = before[-1], after[0]
        ta, tb = float(a["t_s"]), float(b["t_s"])
        w = (t - ta) / (tb - ta)
        y60 = float(a["y"]) + (float(b["y"]) - float(a["y"])) * w
        xa, ya = _xy(a["xyz"])
        xb, yb = _xy(b["xyz"])
        xr, yr = _xy(r["xyz"])
        u_noise = math.sqrt(_rel_se(r) ** 2 + ((1 - w) * _rel_se(a)) ** 2 + (w * _rel_se(b)) ** 2)
        u_dr = ui[r["patch"]]["u"]
        ratio = float(r["y"]) / y60
        rec.update({"y60": y60, "ratio": ratio, "ln_ratio": math.log(ratio), "u_noise": u_noise, "u_interp": u_dr,
                    "u": math.sqrt(u_noise ** 2 + u_dr ** 2), "w": w, "legs_60": [a.get("leg"), b.get("leg")],
                    "drift_60_rel": float(b["y"]) / float(a["y"]) - 1.0,
                    "dx": (xr - (xa + (xb - xa) * w)) if None not in (xr, xa, xb) else None,
                    "dy": (yr - (ya + (yb - ya) * w)) if None not in (yr, ya, yb) else None})
        out.append(rec)
    return out


def combine(ratios: Sequence[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """``{rate: {patch: {...}}}``: the inverse-variance mean of ln(ratio) over the repeats (passes), its
    uncertainty inflated by the Birge ratio sqrt(chi2/dof) when > 1, the repeat spread, and the mean chromaticity
    deltas. The 60-Hz light (y60) is the mean of the bracket values."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in ratios:
        if r.get("unbracketed") or r.get("ln_ratio") is None:
            continue
        groups.setdefault((r["rate"], r["patch"]), []).append(r)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for (rate, patch), rs in sorted(groups.items()):
        wts = [1.0 / (x["u"] ** 2) for x in rs]
        m = sum(wi * x["ln_ratio"] for wi, x in zip(wts, rs)) / sum(wts)
        u_m = 1.0 / math.sqrt(sum(wts))
        chi2 = sum(wi * (x["ln_ratio"] - m) ** 2 for wi, x in zip(wts, rs))
        dof = len(rs) - 1
        birge = math.sqrt(chi2 / dof) if dof > 0 else None
        dxs = [x["dx"] for x in rs if x.get("dx") is not None]
        dys = [x["dy"] for x in rs if x.get("dy") is not None]
        out.setdefault(rate, {})[patch] = {
            "rate": rate, "patch": patch, "kind": rs[0].get("kind"), "code": rs[0].get("code"),
            "nominal_nits": rs[0].get("nominal_nits"), "y60": sum(x["y60"] for x in rs) / len(rs),
            "rate_hz": sum(float(x.get("rate_hz") or _rate_hz_nominal(rate)) for x in rs) / len(rs),
            "ln_ratio": m, "ratio": math.exp(m), "u": u_m * max(1.0, birge or 1.0), "u_propagated": u_m,
            "n": len(rs), "birge": birge, "repeats": [x["ln_ratio"] for x in rs],
            "repeat_spread": (max(x["ln_ratio"] for x in rs) - min(x["ln_ratio"] for x in rs)) if len(rs) > 1 else None,
            "dx": sum(dxs) / len(dxs) if dxs else None, "dy": sum(dys) / len(dys) if dys else None,
            "all_settled": all(bool(x.get("settled")) for x in rs)}
    return out


# ----------------------------------------------------------------------------- chi-square survival (no scipy)
def _gammq(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x) (Numerical Recipes gser / gcf)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-14:
                break
        return max(0.0, 1.0 - s * math.exp(-x + a * math.log(x) - gln))
    b, c, d = x + 1.0 - a, 1.0 / 1e-300, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi2_sf(chi2: float, dof: int) -> float:
    return _gammq(dof / 2.0, chi2 / 2.0) if dof > 0 else 1.0


# ----------------------------------------------------------------------------- predictability
def _dT(hz: float) -> float:
    return 1000.0 / hz - 1000.0 / REF_HZ


def _df(hz: float) -> float:
    return REF_HZ - hz


def _wls_origin(xs: Sequence[float], ys: Sequence[float], us: Sequence[float]) -> dict[str, float]:
    """y = s x by weighted least squares through the origin: s, u_s, chi2, dof."""
    sw = sum((x / u) ** 2 for x, u in zip(xs, us))
    s = sum(x * y / u ** 2 for x, y, u in zip(xs, ys, us)) / sw
    chi2 = sum(((y - s * x) / u) ** 2 for x, y, u in zip(xs, ys, us))
    return {"s": s, "u_s": 1.0 / math.sqrt(sw), "chi2": chi2, "dof": len(xs) - 1, "p": chi2_sf(chi2, len(xs) - 1)}


def _ratio_of(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, float]:
    """p = ln r_a / ln r_b with first-order uncertainty."""
    p = a["ln_ratio"] / b["ln_ratio"]
    u = abs(p) * math.sqrt((a["u"] / a["ln_ratio"]) ** 2 + (b["u"] / b["ln_ratio"]) ** 2)
    return p, u


def _pooled(vals: Sequence[tuple[float, float]]) -> dict[str, float]:
    w = [1.0 / u ** 2 for _, u in vals]
    m = sum(wi * v for wi, (v, _) in zip(w, vals)) / sum(w)
    chi2 = sum(wi * (v - m) ** 2 for wi, (v, _) in zip(w, vals))
    return {"p": m, "u": 1.0 / math.sqrt(sum(w)), "chi2": chi2, "dof": len(vals) - 1,
            "p_value": chi2_sf(chi2, len(vals) - 1)}


def predictability(comb: dict[str, dict[str, dict[str, Any]]], *, ref_low: str = "47.952") -> dict[str, Any]:
    """The four questions of the design (section 3), on the GREY levels, against the frozen thresholds."""
    y60_of: dict[str, float] = {}
    for pats in comb.values():
        for p, v in pats.items():
            if v.get("kind") == "grey":
                y60_of.setdefault(p, v["y60"])
    greys = sorted(y60_of, key=y60_of.get)
    rates = [r for r in ("47.952", "48", "50", "59.94") if r in comb]
    per_level = []
    for p in greys:
        pts = {r: comb[r][p] for r in rates if p in comb[r]}
        row: dict[str, Any] = {"patch": p, "y60": next(iter(pts.values()))["y60"] if pts else None,
                               "ln_ratio": {r: v["ln_ratio"] for r, v in pts.items()},
                               "u": {r: v["u"] for r, v in pts.items()}}
        if len(pts) >= 2:
            hz = {r: v["rate_hz"] for r, v in pts.items()}
            xs_t = [_dT(hz[r]) for r in pts]
            xs_f = [_df(hz[r]) for r in pts]
            ys = [v["ln_ratio"] for v in pts.values()]
            us = [v["u"] for v in pts.values()]
            row["fit_period"] = _wls_origin(xs_t, ys, us)
            row["fit_frequency"] = _wls_origin(xs_f, ys, us)
        if "59.94" in pts:
            v = pts["59.94"]
            row["control_59_94"] = {"ln_ratio": v["ln_ratio"], "u": v["u"],
                                    "ok": abs(v["ln_ratio"]) <= max(K_SIGMA * v["u"], FLOOR_LN)}
        if "48" in pts and ref_low in pts:
            d = pts["48"]["ln_ratio"] - pts[ref_low]["ln_ratio"]
            ud = math.hypot(pts["48"]["u"], pts[ref_low]["u"])
            row["eq_48"] = {"diff": d, "u": ud, "ok": abs(d) <= max(K_SIGMA * ud, FLOOR_LN)}
        if "50" in pts and ref_low in pts and abs(pts[ref_low]["ln_ratio"]) >= 5 * pts[ref_low]["u"]:
            p50, u50 = _ratio_of(pts["50"], pts[ref_low])
            row["p_50"] = {"p": p50, "u": u50}
        per_level.append(row)
    out: dict[str, Any] = {"levels": per_level, "rates": rates}
    hz_of = {r: (sum(v["rate_hz"] for v in comb[r].values()) / len(comb[r])) for r in rates}
    out["rate_hz"] = hz_of
    if "50" in hz_of and ref_low in hz_of:
        pT = _dT(hz_of["50"]) / _dT(hz_of[ref_low])
        pF = _df(hz_of["50"]) / _df(hz_of[ref_low])
        vals = [(lv["p_50"]["p"], lv["p_50"]["u"]) for lv in per_level if "p_50" in lv]
        if vals:
            pool = _pooled(vals)
            uu = pool["u"] * max(1.0, math.sqrt(pool["chi2"] / pool["dof"]) if pool["dof"] > 0 else 1.0)
            comp = lambda target: abs(pool["p"] - target) <= K_SIGMA * uu
            out["linearity"] = {"p_50_pooled": pool["p"], "u": uu, "u_propagated": pool["u"], "chi2": pool["chi2"],
                                "dof": pool["dof"], "predicted_period": pT, "predicted_frequency": pF,
                                "compatible_period": comp(pT), "compatible_frequency": comp(pF),
                                "compatible_step_full": comp(1.0), "compatible_step_none": comp(0.0),
                                "separable": pool["p_value"] >= SEPARABLE_P_MIN, "separable_p_value": pool["p_value"]}
            lin = out["linearity"]
            if lin["compatible_period"] and lin["compatible_frequency"]:
                v = "linear (period and frequency indistinguishable at this precision)"
            elif lin["compatible_period"]:
                v = "linear in frame period"
            elif lin["compatible_frequency"]:
                v = "linear in frequency"
            else:
                v = "nonlinear"
            lin["verdict"] = v
    ctl = [lv["control_59_94"] for lv in per_level if "control_59_94" in lv]
    out["control_59_94"] = {"ok": all(c["ok"] for c in ctl), "levels": len(ctl),
                            "max_abs_ln": max((abs(c["ln_ratio"]) for c in ctl), default=None)} if ctl else None
    eq = [lv["eq_48"] for lv in per_level if "eq_48" in lv]
    out["eq_48_47_952"] = {"ok": all(e["ok"] for e in eq), "levels": len(eq),
                           "max_abs_diff": max((abs(e["diff"]) for e in eq), default=None)} if eq else None
    return out


def chromaticity(comb: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Per rate, the largest |dx|, |dy| on greys and on colours, against the frozen tolerances."""
    out = {}
    for rate, pats in comb.items():
        g, c, fails = [], [], []
        for p, v in pats.items():
            if v.get("dx") is None:
                continue
            tol = XY_TOL_COLOUR if v["kind"] != "grey" else (XY_TOL_DARK if (v["y60"] or 0) < 1.0 else XY_TOL)
            m = max(abs(v["dx"]), abs(v["dy"]))
            (g if v["kind"] == "grey" else c).append(m)
            if m > tol:
                fails.append({"patch": p, "dx": v["dx"], "dy": v["dy"], "tol": tol})
        out[rate] = {"max_dxy_grey": max(g, default=None), "max_dxy_colour": max(c, default=None),
                     "ok": not fails, "fails": fails}
    return out


def channel_test(comb: dict[str, dict[str, dict[str, Any]]], grey_patch: str = "g520") -> dict[str, Any]:
    """Each primary / secondary at code 520 against grey 520 at the same rate: one trim for R, G and B needs the
    effect to follow the drive level, not the channel."""
    out = {}
    for rate, pats in comb.items():
        g = pats.get(grey_patch)
        if not g:
            continue
        rows = []
        for p, v in pats.items():
            if v.get("kind") != "colour":
                continue
            d = v["ln_ratio"] - g["ln_ratio"]
            ud = math.hypot(v["u"], g["u"])
            rows.append({"patch": p, "ln_ratio": v["ln_ratio"], "diff_vs_grey": d, "u": ud,
                         "ok": abs(d) <= max(K_SIGMA * ud, CHANNEL_FLOOR_LN)})
        if rows:
            out[rate] = {"ok": all(r["ok"] for r in rows), "rows": rows}
    return out


# ----------------------------------------------------------------------------- trim curves (the bake's input)
def dpq_code(y60: float, ratio: float, bits: int = 10) -> float:
    """The PQ-code shift that takes the 60-Hz light y60 down by the ratio (the trim in code terms; negative)."""
    mx = (1 << bits) - 1
    return mx * (oetf_norm(y60 / ratio / 10000.0) - oetf_norm(y60 / 10000.0))


def trim_curves(comb: dict[str, dict[str, dict[str, Any]]], pred: Optional[dict[str, Any]] = None, *,
                peak_nits: Optional[float] = None, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The bake's input. Per measured rate, the grey knots sorted by y60: ratio, gain = 1 / ratio, ln_gain, u and
    the equivalent PQ-code shift. With a linearity verdict in ``pred``, also the per-level frame-period
    (or frequency) slope over all rates, so an unmeasured rate between the measured ones can be interpolated."""
    out: dict[str, Any] = {
        "schema": SCHEMA, "reference": {"rate": REF_RATE, "hz": REF_HZ}, "mode": "HDR", **(meta or {}),
        "domain": "y60_nits = the 60-Hz light of a full-field grey at that drive. The MHC is calibrated, so it is "
                  "the PQ target light of the 1D LUT input to <1 % (the trim's slope makes that a <2e-4 error).",
        "apply": "multiply the TARGET light by g right ahead of the base calibration curve (after every other "
                 "neutral-axis stage, desktop gamma included), on R, G and B alike, matrix untouched: "
                 "lut_R(x) = base(PQ^-1(T(x) * g_R(T(x)))) with T(x) the target light of x; then at rate R the "
                 "panel's extra light x ratio_R cancels it. Keyed by light, so one curve serves every permutation",
        "extrapolation": {"below": "hold the lowest knot's gain",
                          "above": "continue the top segment's slope in log10(Y) to the panel peak; the gain never "
                                   "crosses 1 (ln g keeps the top knot's sign, clamped at 0)"},
        "peak_nits": peak_nits, "rates": {}}
    for rate, pats in comb.items():
        knots = sorted((v for v in pats.values() if v.get("kind") == "grey"), key=lambda v: v["y60"])
        if not knots:
            continue
        out["rates"][rate] = {
            "hz": knots[0]["rate_hz"], "frame_period_ms": 1000.0 / knots[0]["rate_hz"],
            "knots": [{"patch": v["patch"], "code": v["code"], "nominal_nits": v["nominal_nits"],
                       "y60_nits": v["y60"], "ratio": v["ratio"], "ln_ratio": v["ln_ratio"], "u_ln": v["u"],
                       "gain": 1.0 / v["ratio"], "ln_gain": -v["ln_ratio"],
                       "dpq_code_10bit": dpq_code(v["y60"], v["ratio"]), "n_repeats": v["n"]} for v in knots]}
    if pred and pred.get("linearity"):
        lin = pred["linearity"]
        axis = ("period" if (lin["compatible_period"] or not lin["compatible_frequency"]) else "frequency")
        usable = lin["compatible_period"] or lin["compatible_frequency"]
        slopes = []
        for lv in pred["levels"]:
            f = lv.get("fit_period" if axis == "period" else "fit_frequency")
            if f and lv.get("y60"):
                slopes.append({"patch": lv["patch"], "y60_nits": lv["y60"], "s": f["s"], "u_s": f["u_s"],
                               "chi2": f["chi2"], "dof": f["dof"]})
        out["model"] = {"usable_for_unmeasured_rates": bool(usable), "verdict": lin.get("verdict"),
                        "axis": axis, "x": "1000/hz - 1000/60 (ms)" if axis == "period" else "60 - hz (Hz)",
                        "ln_ratio": "s(y60) * x; ln_gain = -s * x",
                        "valid_hz": [min(pred["rate_hz"].values()), REF_HZ],
                        "slopes": sorted(slopes, key=lambda s: s["y60_nits"])}
    return out


def _interp_log(knots: Sequence[tuple[float, float]], y: float) -> float:
    """Piecewise linear in log10(y) over (y, value) knots; held flat below the lowest knot; above the top one, the
    top segment's slope continued, clamped so the value never crosses 0 (keeps the top knot's sign)."""
    ks = sorted(knots)
    ly = math.log10(max(y, 1e-6))
    lx = [math.log10(k[0]) for k in ks]
    vs = [k[1] for k in ks]
    if ly <= lx[0] or len(ks) == 1:
        return vs[0]
    for i in range(1, len(ks)):
        if ly <= lx[i]:
            w = (ly - lx[i - 1]) / (lx[i] - lx[i - 1])
            return vs[i - 1] + (vs[i] - vs[i - 1]) * w
    slope = (vs[-1] - vs[-2]) / (lx[-1] - lx[-2])
    v = vs[-1] + slope * (ly - lx[-1])
    return max(v, 0.0) if vs[-1] >= 0 else min(v, 0.0)


def gain_at(rate_entry: dict[str, Any], y_nits: float, peak_nits: Optional[float] = None) -> float:
    """The trim gain of one measured rate at the 60-Hz light ``y_nits`` (hold below, extend above to the peak)."""
    y = min(y_nits, peak_nits) if peak_nits else y_nits
    return math.exp(_interp_log([(k["y60_nits"], k["ln_gain"]) for k in rate_entry["knots"]], y))


def trim_for_hz(trim: dict[str, Any], hz: float, y_nits: float, *, tol_hz: float = 0.01) -> Optional[float]:
    """The gain at any refresh. A measured rate (within ``tol_hz``) uses its own knots. Another rate inside the
    measured span uses the model when the verdict allows it. Otherwise None: no trim, the caller keeps the 60-Hz
    profile."""
    if abs(hz - REF_HZ) <= tol_hz:
        return 1.0
    for entry in trim.get("rates", {}).values():
        if abs(entry["hz"] - hz) <= tol_hz:
            return gain_at(entry, y_nits, trim.get("peak_nits"))
    m = trim.get("model") or {}
    if not m.get("usable_for_unmeasured_rates"):
        return None
    lo, hi = m["valid_hz"]
    if not (lo - tol_hz <= hz <= hi + tol_hz):
        return None
    x = (1000.0 / hz - 1000.0 / REF_HZ) if m["axis"] == "period" else (REF_HZ - hz)
    y = min(y_nits, trim["peak_nits"]) if trim.get("peak_nits") else y_nits
    s = _interp_log([(k["y60_nits"], k["s"]) for k in m["slopes"]], y)
    return math.exp(-s * x)


def analyze(rows: Sequence[dict[str, Any]], *, peak_nits: Optional[float] = None,
            meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    br = bracket_ratios(rows)
    comb = combine(br)
    pred = predictability(comb)
    return {"ratios": br, "combined": comb, "predictability": pred, "chromaticity": chromaticity(comb),
            "channels": channel_test(comb), "u_interp": u_interp(rows),
            "trim": trim_curves(comb, pred, peak_nits=peak_nits, meta=meta)}


# ----------------------------------------------------------------------------- the DIP's refresh response
# DesktopLUT applies the per-rate trim from the display's DIP (dlc.dip RefreshResponse): DLC records the MEASURED
# response there (ratios, not gains) and DesktopLUT derives g = 1 / ratio itself at bake time. ``record`` below is the
# step that puts a finished refresh-trim run into the DIP.
RATIONALS: dict[str, tuple[int, int]] = {"60": (60, 1), "59.94": (60000, 1001), "50": (50, 1), "48": (48, 1),
                                         "47.952": (48000, 1001)}
# The frozen equality tests of the analysis (refresh_result.json "tests", PREREG_REFRESH.md): rate -> (the test, the
# rate whose response it proved this one equal to). The reference rate's key = no effect.
SAME_AS_TESTS: dict[str, tuple[str, str]] = {"59.94": ("P3_59_94_control", REF_RATE),
                                             "48": ("P2_48_eq_47_952", "47.952")}


def response_from_run(trim: dict[str, Any], tests: dict[str, Any], *, run_id: str, measured: str,
                      identity: dict[str, Any], conditions: dict[str, Any],
                      hz_measured: Optional[dict[str, float]] = None) -> Any:
    """The DIP section (``dlc.dip.RefreshResponse``) for one analysed run: per measured rate its exact rational,
    the grey levels (the 60-Hz light, the ratio, its uncertainty, the repeats), and the frozen tests' verdicts
    (``same_as`` where an equality / control test passed; luminance-only and channel-independence per rate)."""
    from .dip import RefreshLevel, RefreshRate, RefreshResponse   # stdlib-only; kept out of the analysis' imports
    p5 = (tests.get("P5_luminance_only") or {}).get("per_rate") or {}
    p6 = (tests.get("P6_channel_independent") or {}).get("per_rate") or {}
    rates = []
    for key, entry in sorted((trim.get("rates") or {}).items(), key=lambda kv: -float(kv[1]["hz"])):
        if key not in RATIONALS:
            raise ValueError(f"rate {key!r} is not in the catalogue {sorted(RATIONALS)}")
        num, den = RATIONALS[key]
        levels = [RefreshLevel(nits=float(k["y60_nits"]), ratio=float(k["ratio"]), u_ln=float(k["u_ln"]),
                               n=int(k.get("n_repeats") or 0), code=int((k.get("code") or [0])[0]))
                  for k in entry["knots"]]
        same = None
        if key in SAME_AS_TESTS and (tests.get(SAME_AS_TESTS[key][0]) or {}).get("ok") is True:
            same = SAME_AS_TESTS[key][1]
        chroma = p5.get(key) or {}
        dxy = [v for v in (chroma.get("max_dxy_grey"), chroma.get("max_dxy_colour")) if v is not None]
        rates.append(RefreshRate(key=key, num=num, den=den, levels=sorted(levels, key=lambda lv: lv.nits),
                                 hz_measured=(hz_measured or {}).get(key), same_as=same,
                                 max_dxy=max(dxy) if dxy else None,
                                 channel_independent=(bool(p6[key]) if key in p6 else None)))
    model = trim.get("model") or {}
    num, den = RATIONALS[REF_RATE]
    return RefreshResponse(measured=measured, run_id=run_id, rates=rates,
                           reference={"key": REF_RATE, "num": num, "den": den}, identity=dict(identity),
                           conditions=dict(conditions), peak_nits=trim.get("peak_nits"),
                           interpolable=bool(model.get("usable_for_unmeasured_rates", False)),
                           verdict=model.get("verdict"))


def run_problems(evidence: dict[str, Any], trim: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Why a run must not go into the DIP (empty = it may). A response is only the panel's when the run was real,
    clean, and measured through the calibration alone: desktop gamma, tone map, FALD and the refresh trim off, the
    3D LUT bypassed. (A response measured WITH DesktopLUT's trim on would be the trim's residual, and recording it
    would undo the trim on the next bake.)"""
    out = []
    meta = result.get("meta") or {}
    if trim.get("simulated") or meta.get("simulated"):
        out.append("simulated run")
    if meta.get("status") != "ok" or evidence.get("status") not in (None, "ok"):
        out.append(f"run status {meta.get('status')!r} / {evidence.get('status')!r}")
    if int(meta.get("anomalies") or 0) or (evidence.get("anomalies") or []):
        out.append("anomalies recorded")
    if str(trim.get("mode") or "").upper() != "HDR":
        out.append(f"mode {trim.get('mode')!r}: only HDR responses are recorded")
    layers = ((trim.get("measured_through") or {}).get("layers")) or {}
    for k in ("desktop_gamma", "tonemap", "fald", "refresh_trim"):
        if layers.get(k):
            out.append(f"measured with {k} on")
    if layers.get("bypass_3dlut") is False:
        out.append("measured through the 3D LUT")
    if not (trim.get("display") or {}).get("hardware_id"):
        out.append("no hardware id recorded for the display (DesktopLUT matches the panel by it)")
    if not trim.get("rates"):
        out.append("no measured rates")
    return out


def _monitor_identity(evidence: dict[str, Any]) -> dict[str, Any]:
    for key in ("audit_stack", "audit_before"):
        mi = (evidence.get(key) or {}).get("monitor_identity")
        if mi:
            return mi
    return {}


def _hz_measured(evidence: dict[str, Any]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for s in evidence.get("switches") or []:
        if s.get("ok") and s.get("rate") and s.get("timing_hz"):
            acc.setdefault(str(s["rate"]), []).append(float(s["timing_hz"]))
    return {k: sum(v) / len(v) for k, v in acc.items()}


def response_for_run_dir(run_dir: Path | str) -> tuple[Any, list[str], dict[str, Any]]:
    """``(response, problems, trim)`` for a refresh-trim run folder (trim_curves.json, refresh_result.json,
    evidence.json)."""
    run = Path(run_dir)
    trim = json.loads((run / "trim_curves.json").read_text(encoding="utf-8"))
    result = json.loads((run / "refresh_result.json").read_text(encoding="utf-8"))
    evidence = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
    mi = _monitor_identity(evidence)
    disp = trim.get("display") or {}
    identity = {"hardware_id": disp.get("hardware_id"), "device_path": mi.get("device_path"),
                "edid_id": mi.get("edid_id")}
    through = trim.get("measured_through") or {}
    meter = evidence.get("meter") or {}
    instruments = (meter.get("resolve") or {}).get("instruments") or [{}]
    conditions = {"mode": trim.get("mode"), "through": "the installed MHC profile",
                  "mhc_profile": through.get("mhc_profile"), "active_perm": through.get("active_perm"),
                  "mhc_source": through.get("source_file"), "layers": through.get("layers"),
                  "cube": through.get("cube"), "patch": "full field, time-bracketed against the reference rate",
                  "instrument": instruments[0].get("description"),
                  "correction": (meter.get("correction") or {}).get("file")}
    started = str(evidence.get("started") or "")
    run_id = str((result.get("meta") or {}).get("run") or run.name)
    measured = started[:10] if len(started) >= 10 else f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}"
    resp = response_from_run(trim, result.get("tests") or {}, run_id=run_id, measured=measured, identity=identity,
                             conditions=conditions, hz_measured=_hz_measured(evidence))
    return resp, run_problems(evidence, trim, result), trim


def record_response(dip_path: Path | str, display: str, mode: str, response: Any, *, today: Optional[str] = None) -> Any:
    """Put ``response`` into the display's DIP record for ``mode`` (``display:mode``, else a bare record) and save.
    The record must exist: a refresh response belongs to a characterized display."""
    from datetime import date

    from .dip import DipStore
    store = DipStore.load(dip_path)
    if store.corrupt:
        raise ValueError(f"{dip_path}: the DIP store is unreadable; not overwriting it")
    rec = store.get(f"{display}:{mode}") or store.get(display)
    if rec is None:
        raise LookupError(f"no DIP record for {display!r} ({mode}) in {dip_path}: characterize the display first")
    rec.refresh_response = response
    rec.updated = today or date.today().isoformat()
    store.record(rec)
    return rec


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from .paths import PROJECT_DIR
    ap = argparse.ArgumentParser(prog="python -m dlc.refresh_trim",
                                 description="Record a refresh-trim run's measured response in the display's DIP "
                                             "(DesktopLUT applies the trim from there).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("record", help="the run's response -> the DIP (dry run unless --write)")
    rp.add_argument("--run", required=True, help="the refresh-trim run folder (trim_curves.json, refresh_result.json)")
    rp.add_argument("--dip", default=str(PROJECT_DIR / "dip_store.json"), help="the DIP store (default: DLC's)")
    rp.add_argument("--write", action="store_true", help="write it (default: show what would be written)")
    args = ap.parse_args(argv)
    resp, problems, trim = response_for_run_dir(args.run)
    display = (trim.get("display") or {}).get("name")
    print(json.dumps({"display": display, "mode": trim.get("mode"), "dip": args.dip, "problems": problems,
                      "refresh_response": resp.as_dict()}, indent=1))
    if problems:
        print("NOT recorded: " + "; ".join(problems))
        return 2
    if not args.write:
        print("dry run: --write records it")
        return 0
    record_response(args.dip, display, str(trim.get("mode")), resp)
    print(f"recorded in {args.dip} ({display}:{trim.get('mode')}). DesktopLUT re-reads the DIP on its next refresh "
          f"switch, the REFRESH TRIM row's RELOAD, or a layers.set refresh_trim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
