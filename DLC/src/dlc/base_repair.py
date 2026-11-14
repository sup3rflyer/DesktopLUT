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

* :func:`balance_census` / :func:`audit_codes` — where the installed cube itself is tinted (its channel-balance
  departure through scan-out, per wire grey). It only PROPOSES windows; the operator names the span
  (``--repair-span``), and :func:`validate_span` refuses a span whose edges cut a tint.
* :func:`cube_repair` — the correction is SHAPED BY THE CUBE: over the tint (+1 code) each channel's curve is
  replaced by a straight line in ``ln(out/in)`` between its untouched edge entries, times a common factor per
  wire grey that keeps that grey's light (``delta_bar``, the luminance-weighted mean of the channels' departures)
  — the channel-imbalanced part of the departure is removed per entry, in index space, placed for each channel
  where scan-out reads it. Every entry outside is bit-identical.
* :func:`repair_balance` — that correction, CONFIRMED by the dense audit: the measured balance of every audited
  grey must be the cube's drive balance through scan-out times the panel's response (``k``) plus a smooth
  panel term, within z x the clean greys' residual spread; otherwise ``confirmed: False`` (the flow refuses).
  The reads never shape the correction (single reads of a sawtooth panel would put its teeth into it).
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
    "balance_census", "audit_codes", "split_fit_held", "validate_span", "cube_repair", "wire_for_index",
    "repair_balance", "judge_after",
    "cube3d_grey_deviation", "read_repeatability",
]

DEFAULT_Z = 4.0
# A balance departure the census must see in the INSTALLED cube before a region is worth auditing (through
# scan-out, per wire grey, against a +-32-code robust envelope). 0.3 % in a channel ratio is ~0.0007 in y at
# mid grey: under that a tint is below what one colorimeter read resolves at ~2 nit.
CENSUS_BALANCE_PCT = 0.3
_SIGMA_FLOOR = 1e-4
# Codes kept on each side of a census tint inside the correction region: the bridge meets the untouched curve a
# few codes from the tint (the census threshold marks where the departure exceeds 0.3 %, not where it starts).
REGION_MARGIN = 1


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


def wire_for_index(applied: Sequence[Sequence[float]], channel: int, v: float) -> float:
    """The wire grey whose channel ``channel`` scan-out reads LUT index ``v`` (the inverse of
    :func:`grey_lut_index`)."""
    rs = sum(float(x) for x in applied[channel])
    return srgb_oetf(srgb_eotf(v) / rs) if rs > 0 else v


def validate_span(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]],
                  span: Sequence[int], *, max_cv: int = 1023, clean_margin: int = 6,
                  census: Optional[Sequence[Mapping[str, Any]]] = None) -> dict[str, Any]:
    """Is ``span`` (wire codes, inclusive) a window the repair may work in? Every tint the installed cube shows
    (:func:`balance_census`) that touches the span must lie strictly inside it with at least ``clean_margin`` clean
    codes on both sides — the edges must be clean, or the repaired curve would meet the untouched one with a step
    (``152:175`` cut the 149-155 bump at its peak). ``{"ok", "problem", "tints", "proposals"}``; ``proposals`` are
    the census's own windows (tint +-16 codes), for the plan digest only."""
    lo, hi = int(span[0]), int(span[1])
    runs = list(census if census is not None else balance_census(curves, applied, max_cv=max_cv))
    inside = [r for r in runs if r["hi"] >= lo and r["lo"] <= hi]
    proposals = audit_codes(runs, max_cv=max_cv)
    out = {"ok": True, "problem": None, "tints": inside, "proposals": proposals, "span": [lo, hi]}
    if not inside:
        out.update(ok=False, problem=f"the installed cube shows no tint inside {lo}:{hi} (its tinted greys: "
                                     f"{[(r['lo'], r['hi']) for r in runs] or 'none'})")
        return out
    for r in inside:
        if r["lo"] - lo < clean_margin or hi - r["hi"] < clean_margin:
            out.update(ok=False, problem=(
                f"the tint at {r['lo']}-{r['hi']} (peak {r['peak_pct']} % {r['series']} at {r['peak_code']}) is "
                f"not inside {lo}:{hi} with {clean_margin} clean codes on both sides — an edge would cut it and "
                f"leave a step; use e.g. {max(0, r['lo'] - 16)}:{min(max_cv, r['hi'] + 16)}"))
            return out
    return out


def _bridge_envelope(curve: Sequence[float], ja: int, jb: int) -> list[float]:
    """The replacement for ``curve`` between entries ``ja`` and ``jb`` (inclusive): a straight line in
    ``ln(out/in)`` between the two edge entries, which it meets EXACTLY (no step where repaired and untouched
    entries meet). Straight, not a fitted envelope: the DLC base cube is itself piecewise linear between its build
    and refine levels (its ratio curve is a chain of straight segments), so a fitted quadratic would differ from
    the curve's own clean neighbourhood and that difference would be written in as a correction (it was: up to
    0.2 % r/g either side of the 2026-09-25 bump). The region is a few codes around the tint, so the line is local."""
    n = len(curve)

    def r(j):
        x = j / (n - 1)
        return math.log(max(float(curve[j]), 1e-12) / max(x, 1e-12))

    ra, rb = r(ja), r(jb)
    out = []
    for j in range(ja, jb + 1):
        t = (j - ja) / (jb - ja) if jb > ja else 0.0
        out.append(math.exp(ra + (rb - ra) * t) * (j / (n - 1)))
    return out


def cube_repair(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]], span: Sequence[int], *,
                primaries: Mapping[str, float], native_white_xy: Sequence[float], peak_luminance: float,
                max_cv: int = 1023, gamma: float = 2.2,
                pred_codes: Optional[Sequence[int]] = None) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """The correction, SHAPED BY THE CUBE: inside ``span``, each channel's curve departs from its own smooth
    envelope (:func:`_bridge_envelope`, in the cube's INDEX space) by ``delta_c(j)``. For a wire grey ``s`` the
    channels read entries ``j_c(s)`` (:func:`grey_lut_index`); the repair keeps that grey's light — its common
    departure ``delta_bar(s) = sum_c w_c delta_c(j_c(s)) / sum_c w_c`` (``w_c`` = the channel's share of the grey's
    luminance) — and removes the channel-imbalanced rest. Written per entry: ``new_c[j] = env_c[j] *
    exp(delta_bar(s_c(j)))`` with ``s_c(j)`` the wire grey that reads entry ``j`` of channel ``c``. To first order
    in the departure the grey's light is unchanged whatever the panel's gamma (the weights carry it, the gamma
    cancels); its balance is the envelopes' balance. Every entry outside the span's per-channel index range is
    returned bit-identical. Returns ``(curves', info)`` with the predicted per-wire balance departure the reads must
    confirm (``pred``: ``{code: (ln r/g, ln b/g)}`` of the installed cube against its envelopes, scaled by
    ``gamma``)."""
    lo, hi = int(span[0]), int(span[1])
    n = len(curves["r"])
    P = _native_P(primaries, native_white_xy, peak_luminance)
    chans = ("r", "g", "b")
    ja = [max(1, int(math.floor(grey_lut_index(applied, k, lo / max_cv) * (n - 1)))) for k in range(3)]
    jb = [min(n - 2, int(math.ceil(grey_lut_index(applied, k, hi / max_cv) * (n - 1)))) for k in range(3)]
    env = {ch: _bridge_envelope(curves[ch], ja[k], jb[k]) for k, ch in enumerate(chans)}

    def delta(k: int, v: float) -> float:
        """ln(curve / envelope) of channel k at LUT index v (0..1), linear between entries; 0 outside."""
        x = v * (n - 1)
        if x <= ja[k] or x >= jb[k]:
            return 0.0
        i0 = int(math.floor(x))
        t = x - i0
        ch = chans[k]
        d0 = math.log(float(curves[ch][i0]) / env[ch][i0 - ja[k]])
        d1 = math.log(float(curves[ch][i0 + 1]) / env[ch][i0 + 1 - ja[k]])
        return d0 + (d1 - d0) * t

    def common(s: float) -> float:
        d = scanout_grey_drive(curves, applied, s)
        w = [P[1][k] * max(d[k], 0.0) ** gamma for k in range(3)]
        idx = [grey_lut_index(applied, k, s) for k in range(3)]
        tot = sum(w) or 1.0
        return sum(w[k] * delta(k, idx[k]) for k in range(3)) / tot

    out = {ch: list(curves[ch]) for ch in chans}
    changed = {}
    for k, ch in enumerate(chans):
        maxd = 0.0
        for j in range(ja[k] + 1, jb[k]):
            s = wire_for_index(applied, k, j / (n - 1))
            new = env[ch][j - ja[k]] * math.exp(common(s))
            out[ch][j] = min(max(new, 0.0), 1.0)
            maxd = max(maxd, abs(out[ch][j] / float(curves[ch][j]) - 1.0))
        limited = _slope_safe(out[ch], curves[ch], ja[k] + 1, jb[k] - 1)
        changed[ch] = {"entries": [ja[k] + 1, jb[k] - 1], "max_drive_change_pct": round(100.0 * maxd, 3),
                       "slope_limited": limited}
    pred = {}
    for c in (pred_codes if pred_codes is not None else range(lo, hi + 1)):
        s = c / max_cv
        idx = [grey_lut_index(applied, k, s) for k in range(3)]
        dl = [delta(k, idx[k]) for k in range(3)]
        pred[c] = (gamma * (dl[0] - dl[1]), gamma * (dl[2] - dl[1]))
    return out, {"changed": changed, "pred": pred, "entries": {"lo": ja, "hi": jb}}


def repair_balance(curves: Mapping[str, Sequence[float]], reads: Mapping[int, Sequence[float]],
                   plan: Mapping[str, Sequence[int]], *, applied: Sequence[Sequence[float]],
                   primaries: Mapping[str, float], native_white_xy: Sequence[float], peak_luminance: float,
                   span: Sequence[int], max_cv: int = 1023, luminance_sigma: float = 0.0, z: float = DEFAULT_Z,
                   envelope_codes: float = 16.0, gamma: float = 2.2,
                   census: Optional[Sequence[Mapping[str, Any]]] = None) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """The balance-only repair over the operator's ``span``: SHAPED by the installed cube (:func:`cube_repair`),
    CONFIRMED by the audit. ``reads``: ``{wire code: XYZ}`` through the installed MHC; ``plan``:
    :func:`split_fit_held`. The measured balance departure of the FIT codes (``ln r/g``, ``ln b/g`` of the linear
    native shares against an envelope fitted on the clean fit codes beside the cube's tint) is compared with the
    cube's predicted departure at the same codes: ``k`` = the least-squares scale (the panel's response to a drive
    imbalance relative to gamma 2.2 — it does not scale the correction: the departure is removed in the drive,
    where the cube made it), and the reads must be explained (``max |measured - k*pred| <= tolerance`` and
    ``0.5 <= k <= 2``) or the repair is ``confirmed: False`` (the flow refuses it). Tolerance =
    ``z * max(noise, clean-code structure)``. Raises ValueError when the span is not valid
    (:func:`validate_span`)."""
    val = validate_span(curves, applied, span, max_cv=max_cv, census=census)
    if not val["ok"]:
        raise ValueError(val["problem"])
    tints = val["tints"]
    lo, hi = int(span[0]), int(span[1])
    P = _native_P(primaries, native_white_xy, peak_luminance)
    Pinv = invert3x3(P)
    fit, vals, _shares = _series(reads, [c for c in plan["fit"] if lo <= c <= hi], Pinv)
    mask = [any(r["lo"] - REGION_MARGIN <= c <= r["hi"] + REGION_MARGIN for r in tints) for c in fit]
    if sum(1 for m in mask if not m) < 6 or not any(mask):
        raise ValueError(f"the audit has too few clean fit greys beside the tint ({sum(1 for m in mask if not m)}) "
                         "or none inside it")
    xs = [float(c) for c in fit]
    bal = [[v[1] for v in vals], [v[2] for v in vals]]
    env = [_robust_envelope(xs, ys, envelope_codes, mask) for ys in bal]
    structure = []
    for k in range(2):
        res = sorted(abs(bal[k][i] - env[k][i]) for i in range(len(fit)) if not mask[i])
        structure.append(1.4826 * res[len(res) // 2])
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    tol = [z * max(sig, s) for s in structure]
    # The correction region: the cube's tint(s) plus REGION_MARGIN codes each side — only there does the cube
    # depart from its own envelope; the rest of the audited span is the clean reference the reads confirm against
    # and stays bit-identical.
    region = (max(lo, min(r["lo"] for r in tints) - REGION_MARGIN),
              min(hi, max(r["hi"] for r in tints) + REGION_MARGIN))
    fixed, info = cube_repair(curves, applied, region, primaries=primaries, native_white_xy=native_white_xy,
                              peak_luminance=peak_luminance, max_cv=max_cv, gamma=gamma, pred_codes=fit)
    meas = {c: (bal[0][i] - env[0][i], bal[1][i] - env[1][i]) for i, c in enumerate(fit)}
    # CONFIRMATION: does the installed cube explain what the meter saw? The panel's own chromaticity is flat, so
    # the measured balance of every audited grey should be the cube's drive balance through scan-out
    # (ln d_r/d_g, ln d_b/d_g), times the panel's response k*gamma, plus a smooth panel term (a + b*code):
    # fitted per series by least squares over ALL fit codes — the cube's own clean neighbourhood included, so no
    # envelope stands between the two. The fit's residuals on the clean codes are the yardstick (z x their robust
    # spread, never below the noise); every code must be explained within it and k must be physical.
    conf = []
    for k in range(2):
        rows = []
        for i, c in enumerate(fit):
            d = scanout_grey_drive(curves, applied, c / max_cv)
            bc = math.log(d[0] / d[1]) if k == 0 else math.log(d[2] / d[1])
            rows.append((gamma * bc, 1.0, (c - lo) / max(1.0, hi - lo), bal[k][i], mask[i]))
        A = [[sum(r[a] * r[b] for r in rows) for b in range(3)] for a in range(3)]
        y = [sum(r[a] * r[3] for r in rows) for a in range(3)]
        try:
            coef = matvec(invert3x3(A), y)
        except (ValueError, ZeroDivisionError):
            coef = [0.0, 0.0, 0.0]
        res = [r[3] - (coef[0] * r[0] + coef[1] * r[1] + coef[2] * r[2]) for r in rows]
        clean = sorted(abs(v) for v, r in zip(res, rows) if not r[4])
        ytol = z * max(sig, 1.4826 * clean[len(clean) // 2] if clean else 0.0)
        # how much tint the cube puts in this series over the region (only a series with a real departure
        # carries information about k)
        spread = max(r[0] for r in rows) - min(r[0] for r in rows)
        conf.append({"k": coef[0], "worst": max(abs(v) for v in res) / ytol if ytol > 0 else 0.0,
                     "tol": ytol, "informative": spread > 2.0 * ytol})
    kscale = conf[0]["k"]
    unexplained = max(c_["worst"] for c_ in conf)
    predicted_max = max(max(abs(v[0]), abs(v[1])) for v in info["pred"].values())
    measured_max = max(max(abs(meas[c][0]), abs(meas[c][1])) for c in fit)
    confirmed = unexplained <= 1.0 and all(0.5 <= c_["k"] <= 2.0 for c_ in conf if c_["informative"]) \
        and any(c_["informative"] for c_ in conf)
    aimed = []
    for c in fit:
        if any(r["lo"] <= c <= r["hi"] for r in tints):
            i = fit.index(c)
            y = reads[c][1]
            er, eb = math.exp(env[0][i]), math.exp(env[1][i])
            g = y / (P[1][0] * er + P[1][1] + P[1][2] * eb)
            xyz_t = matvec(P, [er * g, g, eb * g])
            tot = sum(xyz_t)
            aimed.append({"code": c, "x": round(xyz_t[0] / tot, 5), "y": round(xyz_t[1] / tot, 5)})
    n = len(curves["r"])
    report = {
        "spans": [{"codes": list(region), "audit": [lo, hi], "tints": [[r["lo"], r["hi"]] for r in tints],
                   "changed": info["changed"],
                   "aimed": aimed, "max_departure_pct": round(100.0 * (math.exp(measured_max) - 1.0), 3)}],
        "confirmation": {"confirmed": confirmed, "k": round(kscale, 4),
                         "per_series": [{"series": nm, "k": round(c_["k"], 4), "informative": c_["informative"],
                                         "worst_over_tolerance": round(c_["worst"], 3),
                                         "tolerance_pct": round(100.0 * (math.exp(c_["tol"]) - 1.0), 3)}
                                        for nm, c_ in zip(("r/g", "b/g"), conf)],
                         "unexplained_over_tolerance": round(unexplained, 3),
                         "predicted_max_pct": round(100.0 * (math.exp(predicted_max) - 1.0), 3),
                         "measured_max_pct": round(100.0 * (math.exp(measured_max) - 1.0), 3)},
        "fit_codes": len(fit), "z": z, "envelope_codes": envelope_codes,
        "tolerance_pct": {"r/g": round(100.0 * (math.exp(tol[0]) - 1.0), 3),
                          "b/g": round(100.0 * (math.exp(tol[1]) - 1.0), 3)},
        "structure_pct": {"r/g": round(100.0 * structure[0], 3), "b/g": round(100.0 * structure[1], 3)},
        "noise_pct": round(100.0 * sig, 4),
        "envelope": {"codes": fit, "rg": [round(v, 7) for v in env[0]], "bg": [round(v, 7) for v in env[1]]},
        "unchanged_entries": sum(1 for ch in ("r", "g", "b") for j in range(n) if fixed[ch][j] == curves[ch][j]),
        "proposals": val["proposals"],
    }
    return fixed, report



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
    # The pass-to-pass balance drift (a channel warming between the audit and the verify): the codes BESIDE the
    # correction read the same LUT entries in both passes, so the median change of their balance is the drift —
    # removed everywhere, as the light drift is.
    bdrift = [0.0, 0.0]
    chg: list[list[float]] = [[], []]
    for c in held_out:
        b0, a0 = before.get(c), after.get(c)
        if b0 is None or a0 is None or any(lo <= c <= hi for lo, hi in spans):
            continue
        sb_, sa_ = matvec(Pinv, list(b0)), matvec(Pinv, list(a0))
        if all(v > 0 for v in sb_) and all(v > 0 for v in sa_):
            chg[0].append(math.log(sa_[0] / sa_[1]) - math.log(sb_[0] / sb_[1]))
            chg[1].append(math.log(sa_[2] / sa_[1]) - math.log(sb_[2] / sb_[1]))
    for k in range(2):
        if len(chg[k]) >= 3:
            bdrift[k] = sorted(chg[k])[len(chg[k]) // 2]
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
        drg = math.log(sh[0] / sh[1]) - bdrift[0] - interp(ecodes, erg, c)
        dbg = math.log(sh[2] / sh[1]) - bdrift[1] - interp(ecodes, ebg, c)
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
            "balance_drift_pct": [round(100.0 * (math.exp(v) - 1.0), 3) for v in bdrift],
            "tolerance_pct": {"r/g": round(100.0 * (math.exp(tol_b[0]) - 1.0), 3),
                              "b/g": round(100.0 * (math.exp(tol_b[1]) - 1.0), 3),
                              "light_beside": round(100.0 * (math.exp(tol_l) - 1.0), 3)}}
