"""Local repair of an INSTALLED base 1D LUT from a dense neutral audit (the core of the repair flow).

An installed base cube built before ``dlc.grey_features`` existed can carry narrow features the raw ramp
never resolved (PA32UCXR SDR, 2026-09-25 build: a +1.6 % drive bump in entries 149-157, unequal per
channel, which reads +2.2 % and green-side at grey 153 through the full stack). The repair measures the
neutral axis DENSELY through the installed MHC (matrix + base cube; fine-tune curve, viewing layers and
3D LUT off), finds where the response leaves its own smooth envelope by more than the read noise, and
rewrites ONLY those spans of the cube — every other entry stays bit-identical, so the calibration
everywhere else (and what the 3D LUT was built on there) is untouched.

Pure functions (stdlib). The flow around them (measure, seam, ``mhc.set_base_lut`` with rollback,
held-out verify) is specified in the 2026-11-14 base-LUT report; :func:`audit_plan` / :func:`repair`
/ :func:`judge_after` are its three mechanical steps.

* **Envelope** — a robust (bisquare) local quadratic over ±``envelope_codes`` around each audited code,
  fitted on the FIT half of the audit (alternate codes); ``envelope_codes`` defaults to the raw ramp's
  own spacing, so anything narrower than what the original build could have resolved is a defect and
  anything broader is the calibration's (refine's) business, left alone.
* **Target** — the envelope, in the build's three series (luminance ``ln(Y/eotf)``, balance
  ``ln(r/g)``, ``ln(b/g)``): a local repair never moves the response toward a new global target, only
  removes the narrow departure from the response's own neighbourhood.
* **Spans** — maximal runs of fit codes whose residual exceeds ``z × σ`` in any series, widened by one
  fit code each side (where the factor returns to exactly 1).
* **Correction** — per channel, the linear-light ratio target-share / measured-share at each code in a
  span, placed at the channel's POST-MATRIX LUT index (``index_scale``) and composed in linear light,
  exactly like the closed-loop refine — but with factor 1 outside the spans.
* **Verify** — the other half of the audit codes are HELD OUT of the fit; after the cube is applied
  they are read again and :func:`judge_after` compares them with the same envelope.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Optional, Sequence

from .colormath import invert3x3, matvec, rgb_to_xyz_matrix
from .grey_features import DEFAULT_Z, _SIGMA_FLOOR, grey_series

__all__ = ["audit_plan", "smooth_envelope", "repair", "judge_after"]


def audit_plan(lo_code: int, hi_code: int, *, step: int = 2) -> dict[str, list[int]]:
    """Audit codes ``lo..hi`` every ``step`` codes, split alternately into FIT and HELD-OUT halves."""
    codes = list(range(int(lo_code), int(hi_code) + 1, max(1, int(step))))
    return {"codes": codes, "fit": codes[0::2], "held_out": codes[1::2]}


def _wls_quadratic(xs: Sequence[float], ys: Sequence[float], ws: Sequence[float], x0: float) -> Optional[float]:
    s = [[0.0] * 3 for _ in range(3)]
    b = [0.0] * 3
    h = max((abs(x - x0) for x in xs), default=1.0) or 1.0
    for x, y, w in zip(xs, ys, ws):
        if w <= 0:
            continue
        u = (x - x0) / h
        bas = (1.0, u, u * u)
        for i in range(3):
            b[i] += w * bas[i] * y
            for j in range(3):
                s[i][j] += w * bas[i] * bas[j]
    try:
        inv = invert3x3(s)
    except (ValueError, ZeroDivisionError):
        return None
    return matvec(inv, b)[0]


def smooth_envelope(codes: Sequence[int], values: Sequence[float], at: Sequence[int], *,
                    half_width: float, mask: Optional[Sequence[bool]] = None) -> list[float]:
    """Local quadratic (tricube window ±``half_width`` codes) of ``values(codes)`` evaluated at ``at``,
    fitted ONLY on the unmasked codes. Where the window holds fewer than three unmasked codes on a side
    (a masked departure wider than the window), the nearest three unmasked codes on each side are used:
    the envelope then BRIDGES the masked region from both flanks instead of following it."""
    use = [not (mask[k] if mask is not None else False) for k in range(len(codes))]
    out = []
    for c0 in at:
        left = [k for k in range(len(codes)) if use[k] and codes[k] <= c0 and c0 - codes[k] <= half_width]
        right = [k for k in range(len(codes)) if use[k] and codes[k] > c0 and codes[k] - c0 <= half_width]
        if len(left) < 3:
            left = sorted((k for k in range(len(codes)) if use[k] and codes[k] <= c0),
                          key=lambda k: c0 - codes[k])[:3]
        if len(right) < 3:
            right = sorted((k for k in range(len(codes)) if use[k] and codes[k] > c0),
                           key=lambda k: codes[k] - c0)[:3]
        idx = left + right
        if len(idx) < 3:
            idx = sorted((k for k in range(len(codes)) if use[k]), key=lambda k: abs(codes[k] - c0))[:3]
        xs = [float(codes[k]) for k in idx]
        ys = [values[k] for k in idx]
        reach = max(half_width, max((abs(x - c0) for x in xs), default=half_width)) + 1.0
        ws = [(1.0 - (abs(x - c0) / reach) ** 3) ** 3 for x in xs]
        est = _wls_quadratic(xs, ys, ws, float(c0))
        out.append(est if est is not None else (sum(ys) / len(ys) if ys else 0.0))
    return out


def _masked_envelope(fit: Sequence[int], vals: Sequence[Sequence[float]], *, half_width: float,
                     tol: Sequence[float], rounds: int = 6) -> tuple[list[list[float]], list[bool]]:
    """The three series' envelopes with the departing codes MASKED out of their own envelope: fit,
    flag ``|residual| > tol[k]`` in any series, dilate by one code each side, refit without them —
    until the mask stops changing (a departure can no longer pull the envelope toward itself)."""
    mask = [False] * len(fit)
    env: list[list[float]] = []
    for _ in range(rounds):
        env = [smooth_envelope(fit, [v[k] for v in vals], fit, half_width=half_width, mask=mask)
               for k in range(3)]
        bad = [any(abs(vals[i][k] - env[k][i]) > tol[k] for k in range(3)) for i in range(len(fit))]
        new = [bad[i] or (i > 0 and bad[i - 1]) or (i + 1 < len(fit) and bad[i + 1]) for i in range(len(fit))]
        if new == mask:
            break
        mask = new
    return env, mask


def _tolerances(fit: Sequence[int], vals: Sequence[Sequence[float]], *, half_width: float, sigma: float,
                z: float) -> tuple[list[list[float]], list[bool], list[float], list[float]]:
    """Per-series tolerances that respect the display's OWN fine structure: ``z·√2·max(σ, structure_k)``,
    ``structure_k`` = the robust spread (1.4826 × median |residual|) of series ``k`` around its masked
    envelope. A display whose full-field light is a sawtooth in the code (PA32UCXR SDR, 2026-11-14:
    teeth of 2-4 % every 4-12 codes, chromaticity flat) then has a large LIGHT structure and a small
    BALANCE one: the repair leaves the teeth alone (chasing them from single reads is wrong a few codes
    away, and they may be the backlight answering a full field, not the code) and fixes a balance
    departure. Returns ``(env, mask, tol, structure)``."""
    noise_tol = [z * sigma * math.sqrt(2.0)] * 3
    env, mask = _masked_envelope(fit, vals, half_width=half_width, tol=noise_tol)
    structure = []
    for k in range(3):
        res = sorted(abs(vals[i][k] - env[k][i]) for i in range(len(fit)))
        structure.append(1.4826 * res[len(res) // 2] if res else 0.0)
    tol = [z * math.sqrt(2.0) * max(sigma, structure[k]) for k in range(3)]
    env, mask = _masked_envelope(fit, vals, half_width=half_width, tol=tol)
    return env, mask, tol, structure


def _series_table(reads: Mapping[int, Sequence[float]], codes: Sequence[int], *, max_cv: int,
                  eotf: Callable[[float], float], Pinv) -> tuple[list[int], list[tuple[float, float, float]]]:
    ok, vals = [], []
    for c in codes:
        xyz = reads.get(c)
        if xyz is None:
            continue
        v = grey_series(xyz, c / max_cv, eotf=eotf, Pinv=Pinv)
        if v is not None:
            ok.append(c)
            vals.append(v)
    return ok, vals


def repair(curves: Mapping[str, Sequence[float]], reads: Mapping[int, Sequence[float]],
           plan: Mapping[str, Sequence[int]], *, primaries: Mapping[str, float],
           native_white_xy: tuple[float, float], peak_luminance: float, max_cv: int,
           eotf: Callable[[float], float], oetf: Callable[[float], float],
           index_scale: Callable[[int, float], float], luminance_sigma: float,
           envelope_codes: float = 66.0, z: float = DEFAULT_Z) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Rewrite only the spans of ``curves`` where the audited response (``reads``: ``{code: XYZ}`` through
    the installed MHC) leaves its own smooth envelope. ``oetf`` is the inverse of ``eotf`` (the cube's
    signal domain: γ power for SDR, PQ for HDR); ``index_scale(c, s)`` the post-matrix LUT index of
    channel ``c`` for wire grey ``s``. Returns ``(new_curves, report)``; ``new_curves`` is ``curves``
    unchanged (bit-identical lists) outside the reported spans."""
    P = rgb_to_xyz_matrix(primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
                          primaries["bx"], primaries["by"], native_white_xy[0], native_white_xy[1],
                          white_Y=peak_luminance)
    Pinv = invert3x3(P)
    fit, vals = _series_table(reads, plan["fit"], max_cv=max_cv, eotf=eotf, Pinv=Pinv)
    if len(fit) < 5:
        raise ValueError("too few audited greys to repair from")
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    # balance noise: one channel's own light noise (the build's conservative stand-in)
    env, _mask, tol, structure = _tolerances(fit, vals, half_width=envelope_codes, sigma=sig, z=z)
    bad_k = [[abs(vals[i][k] - env[k][i]) > tol[k] for k in range(3)] for i in range(len(fit))]
    bad = [any(b) for b in bad_k]
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(fit):
        if bad[i]:
            j = i
            while j + 1 < len(fit) and bad[j + 1]:
                j += 1
            spans.append((max(0, i - 1), min(len(fit) - 1, j + 1)))   # widened to the good brackets
            i = j + 1
        else:
            i += 1
    # Two departures closer than half the envelope are one defect whose middle dipped under the
    # tolerance (a bump's tail): repair them as one span, the codes between pulled onto the envelope too.
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and fit[a] - fit[merged[-1][1]] <= 0.5 * envelope_codes:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    n = len(curves["r"])
    out = {ch: list(curves[ch]) for ch in ("r", "g", "b")}
    span_rows = []
    for a, b in merged:
        # per channel: (post-matrix index, linear-light factor) at each fit code of the span; the
        # bracketing codes carry factor exactly 1 (the response is on its envelope there)
        pts: dict[int, list[tuple[float, float]]] = {0: [], 1: [], 2: []}
        # only the series that depart somewhere in this span move; the others keep the measured value
        # (a balance-only span keeps its light exactly — the minimal, channel-neutralising repair)
        moved = [any(bad_k[i][k] for i in range(a, b + 1)) for k in range(3)]
        for i in range(a, b + 1):
            c = fit[i]
            s = c / max_cv
            meas = matvec(Pinv, list(reads[c]))
            if i in (a, b) and not bad[i]:
                fac = (1.0, 1.0, 1.0)
            else:
                tv = [env[k][i] if moved[k] else vals[i][k] for k in range(3)]
                y_t = eotf(s) * math.exp(tv[0])
                er, eb = math.exp(tv[1]), math.exp(tv[2])
                g_t = y_t / (P[1][0] * er + P[1][1] + P[1][2] * eb)
                tgt = (er * g_t, g_t, eb * g_t)
                fac = tuple(tgt[k] / meas[k] if meas[k] > 0 else 1.0 for k in range(3))
            for k in range(3):
                pts[k].append((index_scale(k, s), fac[k]))
        changed = {}
        for k, ch in enumerate(("r", "g", "b")):
            xs = [p[0] for p in pts[k]]
            fs = [p[1] for p in pts[k]]
            j0 = max(0, int(math.ceil(xs[0] * (n - 1))))
            j1 = min(n - 1, int(math.floor(xs[-1] * (n - 1))))
            maxd = 0.0
            for j in range(j0, j1 + 1):
                x = j / (n - 1)
                t = 0
                while t + 1 < len(xs) - 1 and xs[t + 1] < x:
                    t += 1
                x0, x1, f0, f1 = xs[t], xs[t + 1], fs[t], fs[t + 1]
                f = f0 + (f1 - f0) * ((x - x0) / (x1 - x0) if x1 > x0 else 0.0)
                if f == 1.0:
                    continue
                new = oetf(min(max(eotf(float(curves[ch][j])) * f, 0.0), 1.0))
                maxd = max(maxd, abs(new / curves[ch][j] - 1.0) if curves[ch][j] > 0 else 0.0)
                out[ch][j] = new
            # monotone through the span (never touching the entries outside it)
            for j in range(max(j0, 1), j1 + 1):
                if out[ch][j] < out[ch][j - 1]:
                    out[ch][j] = out[ch][j - 1]
            changed[ch] = {"entries": [j0, j1], "max_change_pct": round(100.0 * maxd, 3)}
        span_rows.append({"codes": [fit[a], fit[b]], "changed": changed,
                          "series_moved": [nm for nm, m in zip(("luminance", "balance_rg", "balance_bg"), moved) if m],
                          "max_residual_pct": round(100.0 * max(
                              abs(math.exp(vals[i][k] - env[k][i]) - 1.0)
                              for i in range(a, b + 1) for k in range(3)), 3)})
    report = {"spans": span_rows, "fit_codes": len(fit),
              "tolerance_pct": [round(100.0 * (math.exp(t) - 1.0), 3) for t in tol],
              "structure_pct": [round(100.0 * s, 3) for s in structure],
              "envelope_codes": envelope_codes, "z": z,
              "unchanged_entries": sum(1 for ch in ("r", "g", "b") for j in range(n)
                                       if out[ch][j] == curves[ch][j])}
    return out, report


def judge_after(reads_after: Mapping[int, Sequence[float]], plan: Mapping[str, Sequence[int]], *,
                primaries: Mapping[str, float], native_white_xy: tuple[float, float],
                peak_luminance: float, max_cv: int, eotf: Callable[[float], float],
                luminance_sigma: float, envelope_codes: float = 66.0, z: float = DEFAULT_Z) -> dict[str, Any]:
    """After the repaired cube is applied: every audited code read again (fit AND held-out), each judged
    against the envelope of the FIT half — the held-out half is the verdict the repair never saw.
    Evidence for the seam (keep / roll back), never the decision."""
    P = rgb_to_xyz_matrix(primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
                          primaries["bx"], primaries["by"], native_white_xy[0], native_white_xy[1],
                          white_Y=peak_luminance)
    Pinv = invert3x3(P)
    fit, vals = _series_table(reads_after, plan["fit"], max_cv=max_cv, eotf=eotf, Pinv=Pinv)
    held, hvals = _series_table(reads_after, plan["held_out"], max_cv=max_cv, eotf=eotf, Pinv=Pinv)
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    _env_fit, mask, tol, _structure = _tolerances(fit, vals, half_width=envelope_codes, sigma=sig, z=z)
    env = [smooth_envelope(fit, [v[k] for v in vals], held, half_width=envelope_codes, mask=mask)
           for k in range(3)]
    rows, out = [], []
    for i, c in enumerate(held):
        res = [hvals[i][k] - env[k][i] for k in range(3)]
        row = {"code": c, "luminance_pct": round(100.0 * (math.exp(res[0]) - 1.0), 3),
               "rg_pct": round(100.0 * (math.exp(res[1]) - 1.0), 3),
               "bg_pct": round(100.0 * (math.exp(res[2]) - 1.0), 3)}
        rows.append(row)
        if any(abs(r) > tol[k] for k, r in enumerate(res)):
            out.append(row)
    return {"held_out": rows, "out": out, "tolerance_pct": [round(100.0 * (math.exp(t) - 1.0), 3) for t in tol],
            "max_abs_pct": round(max((max(abs(r["luminance_pct"]), abs(r["rg_pct"]), abs(r["bg_pct"]))
                                       for r in rows), default=0.0), 3)}
