"""Balance repair of an INSTALLED SDR base 1D LUT from a dense neutral audit (the ``repair-base`` flow's core).

The PA32UCXR's installed SDR base (built 2026-09-25) carries a per-channel bump over entries ~149-157: two raw greys
eight codes apart sat on opposite phases of the panel's full-field light sawtooth, the build's piecewise-linear
inverse followed them, and the channels' copies of that steep edge land entries apart — a green-side tint at greys
150-154 (y 0.3295-0.3308 against ~0.328 elsewhere, coordinator's dense read 2026-11-14). The panel's own
chromaticity is flat across the teeth, so a channel-BALANCE correction measured densely is sound; the light teeth
are left alone (whether anything should chase them is open: they may be the backlight answering a full field).

Pure functions (stdlib). Everything is in WIRE grey codes and placed per channel exactly where scan-out reads the
LUT (DesktopLUT ``mhc_icc.cpp``): ``ComputeMhcScanoutBaseLuts`` resamples the 1D .cube to the profile's LUT
(1024 entries for SDR — the identity resample here), and ``EvalMhcScanout`` drives channel ``c`` with
``LUT_c(sRGB_OETF(sum_k applied[c][k] * sRGB_EOTF(wire_k)))``. For a grey wire ``s`` that index is
``sRGB_OETF(rowsum_c * sRGB_EOTF(s))`` (:func:`grey_lut_index`), so one wire grey reads the three channels'
curves at DIFFERENT entries whenever the as-applied matrix's rowsums differ (PA32UCXR SDR: wire 153 -> r 152.0,
g 153.5, b 152.0). A correction meant for wire ``s`` is therefore written at each channel's own index.

Steps (the flow measures, decides at seams and applies; these only compute):

* :func:`audit_codes` — where to look: the installed cube's own channel-balance departures (seen through
  scan-out, per wire grey), widened by a margin; a dense audit then reads every code there.
* :func:`repair_balance` — the audited greys' channel balance ``ln(r/g)``, ``ln(b/g)`` (linear native shares,
  ``P^-1 XYZ``) against a masked smooth envelope of the FIT half (alternate codes); a span is where a balance
  series leaves its envelope by more than ``z * sqrt(2) * max(read noise, that series' own structure)``. Inside
  a span every channel's light is scaled so the balance lands on the envelope while the grey's LIGHT stays what
  it measured (balance-only). Every LUT entry outside the spans is returned bit-identical.
* :func:`judge_after` — after the candidate is applied, the HELD-OUT half (never used to build it) re-read:
  balance against the same envelope, light against its own pre-repair read.
* :func:`scanout_grey_drive` — the scan-out twin, for tests and predictions.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from .colormath import invert3x3, matvec, rgb_to_xyz_matrix

__all__ = [
    "DEFAULT_Z", "srgb_eotf", "srgb_oetf", "grey_lut_index", "lut_at", "scanout_grey_drive",
    "balance_census", "audit_codes", "split_fit_held", "repair_balance", "judge_after",
    "cube3d_grey_deviation", "read_repeatability",
]

DEFAULT_Z = 4.0
# A balance departure the census must see in the INSTALLED cube before a region is worth auditing (through
# scan-out, per wire grey, against a +-32-code robust envelope). 0.3 % in a channel ratio is ~0.0007 in y at
# mid grey: under that a tint is below what one colorimeter read resolves at ~2 nit.
CENSUS_BALANCE_PCT = 0.3
_SIGMA_FLOOR = 1e-4


def srgb_eotf(v: float) -> float:
    v = min(max(float(v), 0.0), 1.0)
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def srgb_oetf(l: float) -> float:
    l = min(max(float(l), 0.0), 1.0)
    return l * 12.92 if l <= 0.0031308 else 1.055 * l ** (1.0 / 2.4) - 0.055


def grey_lut_index(applied: Sequence[Sequence[float]], channel: int, s: float) -> float:
    """The LUT index (0..1) scan-out reads channel ``channel`` at for wire grey ``s`` (EvalMhcScanout)."""
    rs = sum(float(v) for v in applied[channel])
    return srgb_oetf(rs * srgb_eotf(s))


def lut_at(curve: Sequence[float], v: float) -> float:
    """The hardware's linear interpolation between LUT entries (``MhcLutAt``)."""
    n = len(curve)
    x = min(max(float(v), 0.0), 1.0) * (n - 1)
    i0 = min(int(math.floor(x)), n - 1)
    i1 = min(i0 + 1, n - 1)
    return float(curve[i0]) + (float(curve[i1]) - float(curve[i0])) * (x - i0)


def scanout_grey_drive(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]],
                       s: float) -> list[float]:
    """Per-channel panel drive for wire grey ``s``: the full matrix row (not just its sum) on the linearised
    wire, the sRGB re-encode, then the LUT — the Python twin of ``EvalMhcScanout`` for an SDR profile."""
    lin = srgb_eotf(s)
    out = []
    for c, ch in enumerate(("r", "g", "b")):
        m = sum(float(applied[c][k]) * lin for k in range(3))
        out.append(lut_at(curves[ch], srgb_oetf(m)))
    return out


# ---------------------------------------------------------------------------------------------
# where to look
# ---------------------------------------------------------------------------------------------

def _robust_envelope(xs: Sequence[float], ys: Sequence[float], half: float,
                     mask: Optional[Sequence[bool]] = None) -> list[float]:
    """Local quadratic (tricube window +-``half``) through the unmasked points, at every ``xs``; where a window
    holds fewer than three unmasked points on a side, the nearest three on that side are used (the envelope
    bridges a masked region from both flanks instead of following it)."""
    n = len(xs)
    use = [not (mask[i] if mask is not None else False) for i in range(n)]
    out = []
    for x0 in xs:
        left = [k for k in range(n) if use[k] and xs[k] <= x0 and x0 - xs[k] <= half]
        right = [k for k in range(n) if use[k] and xs[k] > x0 and xs[k] - x0 <= half]
        if len(left) < 3:
            left = sorted((k for k in range(n) if use[k] and xs[k] <= x0), key=lambda k: x0 - xs[k])[:3]
        if len(right) < 3:
            right = sorted((k for k in range(n) if use[k] and xs[k] > x0), key=lambda k: xs[k] - x0)[:3]
        idx = left + right
        if len(idx) < 3:
            out.append(sum(ys[k] for k in idx) / len(idx) if idx else 0.0)
            continue
        reach = max(half, max(abs(xs[k] - x0) for k in idx)) + 1.0
        s = [[0.0] * 3 for _ in range(3)]
        b = [0.0] * 3
        for k in idx:
            w = (1.0 - (abs(xs[k] - x0) / reach) ** 3) ** 3
            u = (xs[k] - x0) / reach
            bas = (1.0, u, u * u)
            for i in range(3):
                b[i] += w * bas[i] * ys[k]
                for j in range(3):
                    s[i][j] += w * bas[i] * bas[j]
        try:
            out.append(matvec(invert3x3(s), b)[0])
        except (ValueError, ZeroDivisionError):
            out.append(sum(ys[k] for k in idx) / len(idx))
    return out


def _masked(xs: Sequence[float], series: Sequence[Sequence[float]], half: float,
            tol: Sequence[float], rounds: int = 8) -> tuple[list[list[float]], list[bool]]:
    """Envelopes of several series with the departing points masked out of their own envelope (any series
    beyond its ``tol``; dilated by one point), refitted until the mask stops changing."""
    n = len(xs)
    mask = [False] * n
    env: list[list[float]] = []
    for _ in range(rounds):
        env = [_robust_envelope(xs, ys, half, mask) for ys in series]
        bad = [any(abs(series[k][i] - env[k][i]) > tol[k] for k in range(len(series))) for i in range(n)]
        new = [bad[i] or (i > 0 and bad[i - 1]) or (i + 1 < n and bad[i + 1]) for i in range(n)]
        if new == mask:
            break
        mask = new
    return env, mask


def balance_census(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]], *,
                   max_cv: int = 1023, lo_code: int = 16, half: float = 32.0,
                   threshold_pct: float = CENSUS_BALANCE_PCT) -> list[dict[str, Any]]:
    """Runs of WIRE grey codes whose drive balance through scan-out (``ln(d_r/d_g)``, ``ln(d_b/d_g)``) leaves
    its +-``half``-code masked envelope by more than ``threshold_pct`` — where the installed cube itself is
    tinted, independent of the panel. ``[{"lo", "hi", "peak_code", "peak_pct", "series"}]``."""
    codes = list(range(int(lo_code), max_cv + 1))
    rg, bg = [], []
    for c in codes:
        d = scanout_grey_drive(curves, applied, c / max_cv)
        rg.append(math.log(max(d[0], 1e-9) / max(d[1], 1e-9)))
        bg.append(math.log(max(d[2], 1e-9) / max(d[1], 1e-9)))
    thr = math.log(1.0 + threshold_pct / 100.0)
    env, _mask = _masked([float(c) for c in codes], [rg, bg], half, [thr, thr])
    runs: list[dict[str, Any]] = []
    i = 0
    while i < len(codes):
        dev = [rg[i] - env[0][i], bg[i] - env[1][i]]
        if max(abs(v) for v in dev) > thr:
            j = i
            best = (0.0, codes[i], "")
            while j < len(codes):
                d2 = [rg[j] - env[0][j], bg[j] - env[1][j]]
                if max(abs(v) for v in d2) <= thr:
                    break
                k = 0 if abs(d2[0]) >= abs(d2[1]) else 1
                if abs(d2[k]) > abs(best[0]):
                    best = (d2[k], codes[j], ("r/g", "b/g")[k])
                j += 1
            runs.append({"lo": codes[i], "hi": codes[j - 1], "peak_code": best[1],
                         "peak_pct": round(100.0 * (math.exp(best[0]) - 1.0), 3), "series": best[2]})
            i = j
        else:
            i += 1
    return runs


def audit_codes(runs: Sequence[Mapping[str, Any]], *, margin: int = 16, max_cv: int = 1023,
                lo_code: int = 16, span: Optional[tuple[int, int]] = None) -> list[tuple[int, int]]:
    """The audit windows: each census run widened by ``margin`` codes (merged where they touch), or the
    operator's ``span``. Every code inside is read."""
    if span is not None:
        lo, hi = int(span[0]), int(span[1])
        return [(max(lo_code, min(lo, hi)), min(max_cv, max(lo, hi)))]
    out: list[tuple[int, int]] = []
    for r in sorted(runs, key=lambda r: r["lo"]):
        lo, hi = max(lo_code, int(r["lo"]) - margin), min(max_cv, int(r["hi"]) + margin)
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def split_fit_held(windows: Sequence[tuple[int, int]]) -> dict[str, list[int]]:
    """Every code of every window; alternate codes FIT (even offset from the window start) and HELD OUT."""
    codes, fit, held = [], [], []
    for lo, hi in windows:
        for k, c in enumerate(range(lo, hi + 1)):
            codes.append(c)
            (fit if k % 2 == 0 else held).append(c)
    return {"codes": codes, "fit": fit, "held_out": held}


# ---------------------------------------------------------------------------------------------
# the repair
# ---------------------------------------------------------------------------------------------

def _native_P(primaries: Mapping[str, float], white_xy: Sequence[float], peak: float):
    return rgb_to_xyz_matrix(primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
                             primaries["bx"], primaries["by"], float(white_xy[0]), float(white_xy[1]),
                             white_Y=float(peak))


def _series(reads: Mapping[int, Sequence[float]], codes: Sequence[int], Pinv
            ) -> tuple[list[int], list[tuple[float, float, float]], list[list[float]]]:
    """``(codes, (ln Y, ln r/g, ln b/g), shares)`` for the codes with a usable read."""
    ok, vals, shares = [], [], []
    for c in codes:
        xyz = reads.get(c)
        if xyz is None or not (xyz[1] > 0):
            continue
        sh = matvec(Pinv, list(xyz))
        if not all(v > 0 for v in sh):
            continue
        ok.append(c)
        vals.append((math.log(xyz[1]), math.log(sh[0] / sh[1]), math.log(sh[2] / sh[1])))
        shares.append(sh)
    return ok, vals, shares


def _tolerances(xs: Sequence[float], series: Sequence[Sequence[float]], half: float, sigma: float,
                z: float) -> tuple[list[list[float]], list[bool], list[float], list[float]]:
    """Per-series tolerance ``z * sqrt(2) * max(sigma, structure_k)``; ``structure_k`` = 1.4826 x the median
    |residual| of series ``k`` around its noise-masked envelope (level-to-level structure AND drift, measured
    in this audit). Returns ``(envelopes, mask, tol, structure)``."""
    noise_tol = [z * sigma * math.sqrt(2.0)] * len(series)
    env, _ = _masked(xs, series, half, noise_tol)
    structure = []
    for k, ys in enumerate(series):
        res = sorted(abs(ys[i] - env[k][i]) for i in range(len(xs)))
        structure.append(1.4826 * res[len(res) // 2] if res else 0.0)
    tol = [z * math.sqrt(2.0) * max(sigma, s) for s in structure]
    env, mask = _masked(xs, series, half, tol)
    return env, mask, tol, structure


def _slope_safe(new: list[float], old: Sequence[float], j0: int, j1: int) -> int:
    """Keep the rewritten entries ``j0..j1`` strictly increasing where the original was: every step keeps at
    least half the original step (forward from the untouched entry before ``j0``, backward from the one after
    ``j1``) — the old ``max(prev)`` clamp could leave a flat run (several codes rendering one light). Returns
    how many entries were limited."""
    n = len(new)
    limited = 0
    for j in range(max(j0, 1), min(j1, n - 1) + 1):
        floor = new[j - 1] + 0.5 * max(float(old[j]) - float(old[j - 1]), 0.0)
        if new[j] < floor:
            new[j] = floor
            limited += 1
    for j in range(min(j1, n - 2), max(j0, 0) - 1, -1):
        ceil = new[j + 1] - 0.5 * max(float(old[j + 1]) - float(old[j]), 0.0)
        if new[j] > ceil:
            new[j] = ceil
            limited += 1
    return limited


def repair_balance(curves: Mapping[str, Sequence[float]], reads: Mapping[int, Sequence[float]],
                   plan: Mapping[str, Sequence[int]], *, applied: Sequence[Sequence[float]],
                   primaries: Mapping[str, float], native_white_xy: Sequence[float], peak_luminance: float,
                   max_cv: int = 1023, luminance_sigma: float = 0.0, z: float = DEFAULT_Z,
                   envelope_codes: float = 16.0, gamma: float = 2.2,
                   suspect: Optional[Sequence[tuple[int, int]]] = None,
                   balance_sigma: Optional[float] = None) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """The balance-only repair. ``reads``: ``{wire code: XYZ}`` of the audit through the installed MHC (fine-tune
    curve, viewing layers off); ``plan``: :func:`split_fit_held`. The envelope (+-``envelope_codes``) is fitted
    on the FIT half only; spans are runs of fit codes whose r/g or b/g departs (light never decides a span);
    inside a span each channel's light is scaled by ``target share / measured share`` with the target keeping
    the grey's measured LIGHT and taking the envelope's balance, written at that channel's scan-out index
    (:func:`grey_lut_index`) as a drive change ``factor^(1/gamma)`` (the panel's local response ~ drive^gamma;
    the held-out verify measures what it really did). Factor exactly 1 at the bracketing fit codes, so every
    entry outside a span is bit-identical. Returns ``(curves', report)``."""
    P = _native_P(primaries, native_white_xy, peak_luminance)
    Pinv = invert3x3(P)
    fit, vals, shares = _series(reads, plan["fit"], Pinv)
    if len(fit) < 7:
        raise ValueError(f"too few audited greys to repair from ({len(fit)} fit reads)")
    xs = [float(c) for c in fit]
    bal = [[v[1] for v in vals], [v[2] for v in vals]]
    sig = max(float(balance_sigma if balance_sigma is not None else luminance_sigma), _SIGMA_FLOOR)
    if suspect:
        # The census already says where the INSTALLED CUBE is tinted (a property of the LUT, not of the panel):
        # the envelope is fitted on the fit codes OUTSIDE those runs only and bridges them, and the balance
        # structure the tolerance scales with is measured on those clean codes — the defect cannot inflate
        # its own tolerance (with a handful of reads a robust spread over all codes is the defect's own size).
        mask = [any(lo - 1 <= c <= hi + 1 for lo, hi in suspect) for c in fit]
        if sum(1 for m in mask if not m) < 6:
            raise ValueError("too few clean audited greys beside the suspect runs to fit an envelope")
        env = [_robust_envelope(xs, ys, envelope_codes, mask) for ys in bal]
        structure = []
        for k in range(2):
            res = sorted(abs(bal[k][i] - env[k][i]) for i in range(len(fit)) if not mask[i])
            structure.append(1.4826 * res[len(res) // 2])
        tol = [z * max(sig, s) for s in structure]
    else:
        env, _mask, tol, structure = _tolerances(xs, bal, envelope_codes, sig, z)
    bad = [any(abs(bal[k][i] - env[k][i]) > tol[k] for k in range(2)) for i in range(len(fit))]
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(fit):
        if bad[i]:
            j = i
            while j + 1 < len(fit) and bad[j + 1]:
                j += 1
            spans.append((max(0, i - 1), min(len(fit) - 1, j + 1)))
            i = j + 1
        else:
            i += 1
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and fit[a] - fit[merged[-1][1]] <= envelope_codes / 2.0:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    n = len(curves["r"])
    out = {ch: list(curves[ch]) for ch in ("r", "g", "b")}
    rows = []
    for a, b in merged:
        pts: dict[int, list[tuple[float, float]]] = {0: [], 1: [], 2: []}
        aimed = []
        for i in range(a, b + 1):
            c = fit[i]
            if i in (a, b) and not bad[i]:
                fac = (1.0, 1.0, 1.0)
            else:
                m = shares[i]
                er, eb = math.exp(env[0][i]), math.exp(env[1][i])
                y = reads[c][1]                                       # the grey's measured light, kept
                g = y / (P[1][0] * er + P[1][1] + P[1][2] * eb)
                tgt = (er * g, g, eb * g)
                fac = tuple(tgt[k] / m[k] for k in range(3))
                xyz_t = matvec(P, list(tgt))
                tot = sum(xyz_t)
                aimed.append({"code": c, "x": round(xyz_t[0] / tot, 5), "y": round(xyz_t[1] / tot, 5),
                              "factor": [round(f, 5) for f in fac]})
            for k in range(3):
                pts[k].append((grey_lut_index(applied, k, c / max_cv), fac[k]))
        changed = {}
        for k, ch in enumerate(("r", "g", "b")):
            xs_k = [p[0] for p in pts[k]]
            fs_k = [p[1] for p in pts[k]]
            j0 = max(0, int(math.floor(xs_k[0] * (n - 1))) + 1)
            j1 = min(n - 1, int(math.ceil(xs_k[-1] * (n - 1))) - 1)
            maxd, t = 0.0, 0
            for j in range(j0, j1 + 1):
                x = j / (n - 1)
                while t + 1 < len(xs_k) - 1 and xs_k[t + 1] < x:
                    t += 1
                x0, x1 = xs_k[t], xs_k[t + 1]
                f = fs_k[t] + (fs_k[t + 1] - fs_k[t]) * ((x - x0) / (x1 - x0) if x1 > x0 else 0.0)
                if f == 1.0:
                    continue
                new = float(curves[ch][j]) * f ** (1.0 / gamma)
                out[ch][j] = min(max(new, 0.0), 1.0)
                maxd = max(maxd, abs(out[ch][j] / float(curves[ch][j]) - 1.0) if curves[ch][j] > 0 else 0.0)
            limited = _slope_safe(out[ch], curves[ch], j0, j1)
            changed[ch] = {"entries": [j0, j1], "max_drive_change_pct": round(100.0 * maxd, 3),
                           "slope_limited": limited}
        rows.append({"codes": [fit[a], fit[b]], "changed": changed, "aimed": aimed,
                     "max_departure_pct": round(100.0 * max(
                         abs(math.exp(bal[k][i] - env[k][i]) - 1.0) for i in range(a, b + 1) for k in range(2)), 3)})
    report = {
        "spans": rows, "fit_codes": len(fit), "envelope_codes": envelope_codes, "z": z,
        "tolerance_pct": {"r/g": round(100.0 * (math.exp(tol[0]) - 1.0), 3),
                          "b/g": round(100.0 * (math.exp(tol[1]) - 1.0), 3)},
        "structure_pct": {"r/g": round(100.0 * structure[0], 3), "b/g": round(100.0 * structure[1], 3)},
        "noise_pct": round(100.0 * sig, 4),
        "envelope": {"codes": fit, "rg": [round(v, 7) for v in env[0]], "bg": [round(v, 7) for v in env[1]]},
        "unchanged_entries": sum(1 for ch in ("r", "g", "b") for j in range(n) if out[ch][j] == curves[ch][j]),
    }
    return out, report


def read_repeatability(rows: Sequence[Mapping[str, Any]]) -> float:
    """Relative luminance repeatability of a measure pass from its log rows: consecutive accepted re-reads of
    one patch (read-to-read) and the interleaved drift references (time-to-time), each a robust sigma in ln
    units (1.4826 x MAD), combined in quadrature. 0.0 with no evidence."""
    pairs: list[float] = []
    refs: list[float] = []
    last: Optional[tuple] = None
    group: list[float] = []

    def close() -> None:
        if len(group) >= 2:
            m = sum(group) / len(group)
            pairs.extend(abs(v - m) * math.sqrt(len(group) / (len(group) - 1)) for v in group)

    for d in rows:
        rgb, nits = d.get("rgb"), d.get("nits")
        if not rgb or not isinstance(nits, (int, float)) or nits <= 0 or d.get("phase") not in (None, "main"):
            continue
        if d.get("role") == "neutral_ref":
            refs.append(math.log(float(nits)))
            continue
        key = (d.get("label"), tuple(rgb))
        if d.get("read_index") in (0, None) or key != last:
            close()
            group = []
        group.append(math.log(float(nits)))
        last = key
    close()

    def mad(v: Sequence[float], centre: float) -> float:
        s = sorted(abs(x - centre) for x in v)
        return 1.4826 * s[len(s) // 2]

    read = 1.4826 * sorted(pairs)[len(pairs) // 2] if pairs else 0.0
    drift = mad(refs, sorted(refs)[len(refs) // 2]) if len(refs) >= 3 else 0.0
    return math.sqrt(read * read + drift * drift)


def cube3d_grey_deviation(path: Any) -> Optional[float]:
    """The largest ``|out - in|`` (any channel) on the grey diagonal of a 3D ``.cube`` (``LUT_3D_SIZE``, r
    fastest) — 0 for a cube that leaves greys alone. ``None`` when the file is not a readable 3D cube."""
    from pathlib import Path as _P
    try:
        text = _P(str(path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    size, rows = None, []
    for line in text.splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "LUT_3D_SIZE":
            size = int(t[1])
            continue
        if len(t) >= 3:
            try:
                rows.append((float(t[0]), float(t[1]), float(t[2])))
            except ValueError:
                continue
    if not size or len(rows) < size ** 3:
        return None
    worst = 0.0
    for k in range(size):
        r, g, b = rows[k + k * size + k * size * size]
        x = k / (size - 1)
        worst = max(worst, abs(r - x), abs(g - x), abs(b - x))
    return worst


def judge_after(before: Mapping[int, Sequence[float]], after: Mapping[int, Sequence[float]], report: Mapping[str, Any],
                held_out: Sequence[int], *, primaries: Mapping[str, float], native_white_xy: Sequence[float],
                peak_luminance: float, luminance_sigma: float = 0.0, z: float = DEFAULT_Z) -> dict[str, Any]:
    """The verify. Each HELD-OUT code (never used to build the repair) re-read through the candidate:
    its balance against the envelope the repair aimed at (interpolated between fit codes), its light against
    its own pre-repair read. ``inside`` = within a repaired span; ``beside`` = outside (the LUT there is
    bit-identical, so the code must read as before). Out of tolerance on either -> listed in ``out``. Evidence
    for the keep / revert seam, never the decision."""
    P = _native_P(primaries, native_white_xy, peak_luminance)
    Pinv = invert3x3(P)
    env = report["envelope"]
    ecodes, erg, ebg = env["codes"], env["rg"], env["bg"]
    tol_b = [math.log(1.0 + report["tolerance_pct"]["r/g"] / 100.0), math.log(1.0 + report["tolerance_pct"]["b/g"] / 100.0)]
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    spans = [tuple(s["codes"]) for s in report.get("spans") or ()]
    # light: the same grey read twice (before / after), minutes apart. BESIDE the spans the LUT is bit-identical,
    # so those codes measure the pass-to-pass light change directly: their median is the common drift (removed
    # everywhere), their robust spread the repeatability the light is judged against (measured in this run,
    # never only the consecutive-re-read sigma, which understates it).
    dl_beside = []
    for c in held_out:
        b0, a0 = before.get(c), after.get(c)
        if b0 and a0 and b0[1] > 0 and a0[1] > 0 and not any(lo <= c <= hi for lo, hi in spans):
            dl_beside.append(math.log(a0[1] / b0[1]))
    drift = sorted(dl_beside)[len(dl_beside) // 2] if len(dl_beside) >= 3 else 0.0
    spread = (1.4826 * sorted(abs(v - drift) for v in dl_beside)[len(dl_beside) // 2]
              if len(dl_beside) >= 3 else 0.0)
    tol_l = z * max(math.sqrt(2.0) * sig, spread)

    def interp(xs, ys, x):
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        for k in range(1, len(xs)):
            if xs[k] >= x:
                t = (x - xs[k - 1]) / (xs[k] - xs[k - 1])
                return ys[k - 1] + (ys[k] - ys[k - 1]) * t
        return ys[-1]

    # balance: a held-out READ scatters about the envelope by the reads' own noise + structure, which the
    # fit residuals understate (the envelope was fitted through those reads). The scale it is judged against is
    # the robust spread of the BEFORE reads of the same held-out codes beside the spans (the LUT there does not
    # change, so it is exactly the scatter an unchanged code shows), never smaller than the build's tolerance.
    pre: list[list[float]] = [[], []]
    for c in held_out:
        b0 = before.get(c)
        if b0 is None or any(lo <= c <= hi for lo, hi in spans):
            continue
        shb = matvec(Pinv, list(b0))
        if all(v > 0 for v in shb):
            pre[0].append(math.log(shb[0] / shb[1]) - interp(ecodes, erg, c))
            pre[1].append(math.log(shb[2] / shb[1]) - interp(ecodes, ebg, c))
    for k in range(2):
        if len(pre[k]) >= 3:
            spread_b = 1.4826 * sorted(abs(v) for v in pre[k])[len(pre[k]) // 2]
            tol_b[k] = max(tol_b[k], z * spread_b)
    rows, out = [], []
    for c in held_out:
        b0, a0 = before.get(c), after.get(c)
        if a0 is None or b0 is None:
            rows.append({"code": c, "status": "unread"})
            continue
        sh = matvec(Pinv, list(a0))
        if not all(v > 0 for v in sh) or not (a0[1] > 0 and b0[1] > 0):
            rows.append({"code": c, "status": "unusable"})
            continue
        where = "inside" if any(lo <= c <= hi for lo, hi in spans) else "beside"
        drg = math.log(sh[0] / sh[1]) - interp(ecodes, erg, c)
        dbg = math.log(sh[2] / sh[1]) - interp(ecodes, ebg, c)
        dl = math.log(a0[1] / b0[1]) - drift
        tot = sum(a0)
        missed = [nm for nm, v, t in (("r/g", drg, tol_b[0]), ("b/g", dbg, tol_b[1])) if abs(v) > t]
        # light: inside a span a balance move changes the channels' light a little by construction (their sum is
        # held only to first order); beside it nothing may move beyond read noise
        if abs(dl) > (tol_l if where == "beside" else max(tol_l, tol_b[0], tol_b[1])):
            missed.append("light")
        row = {"code": c, "where": where, "rg_pct": round(100.0 * (math.exp(drg) - 1.0), 3),
               "bg_pct": round(100.0 * (math.exp(dbg) - 1.0), 3), "light_change_pct": round(100.0 * (math.exp(dl) - 1.0), 3),
               "x": round(a0[0] / tot, 5), "y": round(a0[1] / tot, 5), "missed": missed,
               "status": "out" if missed else "ok"}
        rows.append(row)
        if missed:
            out.append(row)
    return {"held_out": rows, "out": out,
            "inside": sum(1 for r in rows if r.get("where") == "inside"),
            "beside": sum(1 for r in rows if r.get("where") == "beside"),
            "light_drift_pct": round(100.0 * (math.exp(drift) - 1.0), 3),
            "tolerance_pct": {"r/g": round(100.0 * (math.exp(tol_b[0]) - 1.0), 3),
                              "b/g": round(100.0 * (math.exp(tol_b[1]) - 1.0), 3),
                              "light_beside": round(100.0 * (math.exp(tol_l) - 1.0), 3)}}
