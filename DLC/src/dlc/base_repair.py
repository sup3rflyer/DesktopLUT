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
* :func:`repair_balance` — that correction, SIZED and CONFIRMED by the dense audit (:func:`_sized_repair`): the
  display's response to the cube's drive balance (blurred along the code, shifted, a gain per series —
  :func:`fit_response`), the removal's amplitude fitted by least squares, the after-balance predicted at every
  audited code and refused unless flat within z x the clean greys' scatter. The reads size the correction; they
  never shape it (single reads of a sawtooth panel would put its teeth into it).
* :func:`judge_after` — after the candidate is applied, the HELD-OUT half (never used to build it) re-read:
  each code's balance change against its PREDICTED change and its after-balance against the flat target; light
  beside the span against its own pre-repair read, inside it reported as an observation (with tooth-edge flags).
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
                pred_codes: Optional[Sequence[int]] = None,
                amplitude: Sequence[float] = (1.0, 1.0)) -> tuple[dict[str, list[float]], dict[str, Any]]:
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
    ``gamma``).

    ``amplitude = (a_rg, a_bg)``: the fraction of the channel-imbalanced departure removed (``1`` = all of it;
    the reads SIZE it, :func:`repair_balance`): per wire grey the r-vs-g imbalance ``eps_r = delta_r - delta_g``
    keeps ``(1 - a_rg) eps_r``, the b-vs-g one ``(1 - a_bg) eps_b``, and the three channels are placed so their
    luminance-weighted mean stays ``delta_bar`` (the grey's light). ``(1, 1)`` is exactly the full removal."""
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

    a_rg, a_bg = float(amplitude[0]), float(amplitude[1])
    full = a_rg == 1.0 and a_bg == 1.0

    def kept(k: int, s: float) -> float:
        """Channel k's departure after the repair, for wire grey s (delta_bar when the removal is full)."""
        dbar = common(s)
        if full:
            return dbar
        d = scanout_grey_drive(curves, applied, s)
        w = [P[1][i] * max(d[i], 0.0) ** gamma for i in range(3)]
        dl = [delta(i, grey_lut_index(applied, i, s)) for i in range(3)]
        er, eb = (1.0 - a_rg) * (dl[0] - dl[1]), (1.0 - a_bg) * (dl[2] - dl[1])
        dg = dbar - (w[0] * er + w[2] * eb) / (sum(w) or 1.0)
        return (dg + er, dg, dg + eb)[k]

    out = {ch: list(curves[ch]) for ch in chans}
    changed = {}
    for k, ch in enumerate(chans):
        maxd = 0.0
        for j in range(ja[k] + 1, jb[k]):
            s = wire_for_index(applied, k, j / (n - 1))
            new = env[ch][j - ja[k]] * math.exp(kept(k, s))
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


# ---------------------------------------------------------------------------------------------
# the display's response to a drive-balance change: BLURRED along the wire code
# ---------------------------------------------------------------------------------------------
# Measured on the PA32UCXR (repair run 2026-11-14 12:16): the balance the meter sees over a narrow cube feature is
# the cube's drive balance through scan-out SMEARED along the wire code — a Gaussian of sigma ~2 codes, unit area,
# placed ~half a code higher than the scan-out twin puts it. Fitted on the audit's 20 fit codes (BEFORE the repair)
# alone: sigma 2.25, shift +0.5, gain k 1.14 (r/g) / 1.22 (b/g), residual RMS 0.22 / 0.20 % (the unblurred, unshifted
# model: k 0.71 / 0.78, 0.39 / 0.32 %); the SAME fit predicts the change the installed candidate then made to within
# 0.19 % RMS over 136-168 (dense after-read; unblurred: misses up to 1.2 % — 147 and 155-157 moved although their
# drive balance did not). The repair's old "k 0.77" was this blur seen through a sharp basis, not a gain. Whatever
# smooths and shifts (the scan-out's own LUT handling or the panel's processing), the model is measured per audit:
# sigma and the shift are chosen on grids by least squares, never assumed.
BLUR_SIGMAS = tuple(0.25 * i for i in range(17))   # 0 .. 4 wire codes
SHIFTS = tuple(0.25 * i for i in range(-4, 5))      # -1 .. +1 wire code
_GRID_STEP = 0.25


def _balance_grid(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]], lo: float, hi: float,
                  max_cv: int) -> tuple[list[float], list[list[float]]]:
    """The drive balance ``(ln d_r/d_g, ln d_b/d_g)`` through scan-out on a 1/4-code wire grid over ``[lo, hi]``."""
    n = int(round((hi - lo) / _GRID_STEP)) + 1
    xs = [lo + _GRID_STEP * i for i in range(n)]
    rg, bg = [], []
    for x in xs:
        d = scanout_grey_drive(curves, applied, min(max(x, 0.0), max_cv) / max_cv)
        rg.append(math.log(max(d[0], 1e-12) / max(d[1], 1e-12)))
        bg.append(math.log(max(d[2], 1e-12) / max(d[1], 1e-12)))
    return xs, [rg, bg]


def _blur(vals: Sequence[float], sigma: float) -> list[float]:
    """Gaussian blur (sigma in wire codes, unit area, edges held) of a 1/4-code grid."""
    if sigma <= 0:
        return list(vals)
    half = int(math.ceil(4.0 * sigma / _GRID_STEP))
    ker = [math.exp(-0.5 * (i * _GRID_STEP / sigma) ** 2) for i in range(-half, half + 1)]
    tot = sum(ker)
    n = len(vals)
    out = []
    for i in range(n):
        acc = 0.0
        for j, w in enumerate(ker):
            acc += w * vals[min(max(i + j - half, 0), n - 1)]
        out.append(acc / tot)
    return out


def _at(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = int((x - xs[0]) / _GRID_STEP)
    i = min(i, len(xs) - 2)
    t = (x - xs[i]) / (xs[i + 1] - xs[i])
    return ys[i] + (ys[i + 1] - ys[i]) * t


# Upper 90 % confidence factor on a sigma from n samples (chi-square, n-1 dof) — as dlc.grey_adequacy.
_CHI2_10 = {1: 0.0158, 2: 0.211, 3: 0.584, 4: 1.064, 5: 1.610, 6: 2.204, 7: 2.833, 8: 3.490, 9: 4.168,
            10: 4.865, 11: 5.578, 12: 6.304, 13: 7.042, 14: 7.790, 15: 8.547, 16: 9.312, 17: 10.085, 18: 10.865,
            19: 11.651, 20: 12.443}


def _upper_sigma(rms: float, n: int) -> float:
    dof = max(1, n - 1)
    q = _CHI2_10.get(dof, dof - 1.2816 * math.sqrt(2.0 * dof))
    return rms * math.sqrt(dof / q)


def _lsq3(rows: Sequence[tuple[Sequence[float], float]]) -> tuple[list[float], list[float], float, list[float]]:
    """Least squares with 3 regressors: ``(coef, standard errors, residual sigma, residuals)``."""
    A = [[sum(r[0][a] * r[0][b] for r in rows) for b in range(3)] for a in range(3)]
    y = [sum(r[0][a] * r[1] for r in rows) for a in range(3)]
    inv = invert3x3(A)
    coef = matvec(inv, y)
    res = [r[1] - sum(coef[a] * r[0][a] for a in range(3)) for r in rows]
    dof = max(1, len(rows) - 3)
    s = math.sqrt(sum(v * v for v in res) / dof)
    return coef, [s * math.sqrt(max(inv[a][a], 0.0)) for a in range(3)], s, res


def fit_response(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]],
                 bal: Mapping[int, Sequence[float]], codes: Sequence[int], *, lo: int, hi: int, gamma: float = 2.2,
                 max_cv: int = 1023, sigmas: Sequence[float] = BLUR_SIGMAS,
                 shifts: Sequence[float] = SHIFTS) -> dict[str, Any]:
    """The display's balance response to the cube, fitted on the audit's (BEFORE) balance at ``codes``:
    ``bal_s(c) = k_s * gamma * (h_sigma * B_s)(c - shift) + a_s + b_s x`` with ``B`` the drive balance through
    scan-out and ``h_sigma`` a unit-area Gaussian along the wire code. ONE sigma and ONE shift for both series (the
    joint least squares over the grids), ``k`` / ``a`` / ``b`` per series with standard errors. ``{"sigma",
    "shift", "series": [{k, k_se, a, b, rms, residual: {code: v}}, ...]}``. :func:`response_model` evaluates
    it at any code."""
    pad = 5.0 * max(sigmas) + max(abs(v) for v in shifts) + 2.0
    xs, B = _balance_grid(curves, applied, lo - pad, hi + pad, max_cv)
    best = None
    for sigma in sigmas:
        hb = [_blur(B[s], sigma) for s in range(2)]
        for shift in shifts:
            fits, rss = [], 0.0
            for s in range(2):
                rows = [([gamma * _at(xs, hb[s], c - shift), 1.0, (c - lo) / max(1.0, hi - lo)], bal[c][s])
                        for c in codes]
                coef, se, sd, res = _lsq3(rows)
                rss += sum(v * v for v in res)
                fits.append({"k": coef[0], "k_se": se[0], "a": coef[1], "b": coef[2], "rms": sd,
                             "residual": {c: r for c, r in zip(codes, res)}})
            if best is None or rss < best[0] - 1e-15:
                best = (rss, sigma, shift, fits)
    return {"sigma": best[1], "shift": best[2], "series": best[3], "lo": lo, "hi": hi, "gamma": gamma}


def response_model(curves: Mapping[str, Sequence[float]], applied: Sequence[Sequence[float]],
                   response: Mapping[str, Any], codes: Sequence[int], *, max_cv: int = 1023
                   ) -> dict[int, tuple[float, float]]:
    """The fitted response's balance at ``codes`` for the cube ``curves`` (smooth panel term included)."""
    lo, hi, g = response["lo"], response["hi"], response.get("gamma", 2.2)
    pad = 5.0 * max(BLUR_SIGMAS) + max(abs(v) for v in SHIFTS) + 2.0
    xs, B = _balance_grid(curves, applied, min(codes) - pad, max(codes) + pad, max_cv)
    hb = [_blur(B[s], response["sigma"]) for s in range(2)]
    out = {}
    for c in codes:
        out[c] = tuple(response["series"][s]["k"] * g * _at(xs, hb[s], c - response.get("shift", 0.0))
                       + response["series"][s]["a"] + response["series"][s]["b"] * (c - lo) / max(1.0, hi - lo)
                       for s in range(2))
    return out


def predicted_change(cube: Mapping[str, Sequence[float]], cand: Mapping[str, Sequence[float]],
                     applied: Sequence[Sequence[float]], response: Mapping[str, Any], codes: Sequence[int], *,
                     lo: int, hi: int, gamma: float = 2.2, max_cv: int = 1023) -> dict[int, tuple[float, float]]:
    """The balance change ``cand`` makes at each wire code, through the measured response:
    ``k_s * gamma * (h_sigma * (B_cand - B_cube))_s(c - shift)``."""
    pad = 5.0 * max(BLUR_SIGMAS) + max(abs(v) for v in SHIFTS) + 2.0
    xs, B0 = _balance_grid(cube, applied, lo - pad, hi + pad, max_cv)
    _, B1 = _balance_grid(cand, applied, lo - pad, hi + pad, max_cv)
    sigma, shift = response["sigma"], response.get("shift", 0.0)
    out = {}
    hd = [_blur([b1 - b0 for b0, b1 in zip(B0[s], B1[s])], sigma) for s in range(2)]
    for c in codes:
        out[c] = tuple(response["series"][s]["k"] * gamma * _at(xs, hd[s], c - shift) for s in range(2))
    return out


def light_bounds(cube: Mapping[str, Sequence[float]], cand: Mapping[str, Sequence[float]],
                 applied: Sequence[Sequence[float]], reads: Mapping[int, Sequence[float]], codes: Sequence[int], *,
                 primaries: Mapping[str, float], native_white_xy: Sequence[float], peak_luminance: float,
                 luminance_sigma: float, z: float = DEFAULT_Z, gamma: float = 2.2,
                 max_cv: int = 1023) -> dict[int, dict[str, Any]]:
    """EVIDENCE about the light change inside the span — neither a prediction nor a gate. The audit reads every
    code of the span, so it knows the panel's light teeth along the code (``t(c)``: ln Y around a smooth ln-ln
    quadratic). A balance-only change moves each channel's drive by ``dD_k`` codes (``drive_moves_codes``); a channel
    whose move reaches across a tooth edge can carry the grey's light with it. ``tooth_edge`` = an adjacent-code
    step in ``t`` beyond ``z sqrt 2`` x the noise lies within some channel's move; ``reach`` = the
    luminance-weighted largest ``|t(c') - t(c)|`` within the channels' moves. On the 2026-11-14 run neither a
    tooth-step model (wrong sign at 150, 152, 155) nor this reach (150: +1.30 % observed vs 1.08 %; 155: -2.64 %
    vs 0.95 %) predicted the observed light change, so the flow reports inside-span light as an OBSERVATION with
    these flags and gates on balance only."""
    P = _native_P(primaries, native_white_xy, peak_luminance)
    cs = sorted(c for c in codes if reads.get(c) and reads[c][1] > 0)
    if len(cs) < 5:
        return {}
    lx = [math.log(c) for c in cs]
    ly = [math.log(reads[c][1]) for c in cs]
    rows = [([1.0, x, x * x], y) for x, y in zip(lx, ly)]
    coef, _se, _sd, _res = _lsq3(rows)
    t = {c: y - (coef[0] + coef[1] * x + coef[2] * x * x) for c, x, y in zip(cs, lx, ly)}
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)
    edge = z * math.sqrt(2.0) * sig
    out = {}
    for c in cs:
        d0 = scanout_grey_drive(cube, applied, c / max_cv)
        d1 = scanout_grey_drive(cand, applied, c / max_cv)
        w = [P[1][k] * max(d0[k], 0.0) ** gamma for k in range(3)]
        bound, on_edge, moves = 0.0, False, []
        for k in range(3):
            dd = max_cv * (d1[k] - d0[k])
            moves.append(round(dd, 3))
            if abs(dd) < 1e-6:
                continue
            a, b = (c, c + int(math.ceil(dd))) if dd > 0 else (c + int(math.floor(dd)), c)
            reach = [x for x in range(a, b + 1) if x in t]
            step = max((abs(t[x] - t[c]) for x in reach), default=0.0)
            bound += w[k] * step
            if any(x + 1 in t and x + 1 <= b and abs(t[x + 1] - t[x]) > edge for x in range(a, b)):
                on_edge = True
        out[c] = {"reach": bound / (sum(w) or 1.0), "tooth_edge": on_edge, "drive_moves_codes": moves}
    return out


def repair_balance(curves: Mapping[str, Sequence[float]], reads: Mapping[int, Sequence[float]],
                   plan: Mapping[str, Sequence[int]], *, applied: Sequence[Sequence[float]],
                   primaries: Mapping[str, float], native_white_xy: Sequence[float], peak_luminance: float,
                   span: Sequence[int], max_cv: int = 1023, luminance_sigma: float = 0.0, z: float = DEFAULT_Z,
                   envelope_codes: float = 16.0, gamma: float = 2.2,
                   census: Optional[Sequence[Mapping[str, Any]]] = None) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """The balance-only repair over the operator's ``span``: SHAPED by the installed cube (:func:`cube_repair`),
    SIZED and CONFIRMED by the audit (:func:`_sized_repair`): the display's response to the cube's drive balance
    (blur along the code + gain) fitted on the FIT codes, the removal's amplitude fitted to make them flat, the
    after-balance predicted at every audited code, refused unless that prediction is flat within the clean greys'
    scatter. ``reads``: ``{wire code: XYZ}`` through the installed MHC; ``plan``: :func:`split_fit_held`.
    ``tolerance_pct`` / ``envelope`` are the clean-code envelope (evidence). Raises ValueError when the span is not
    valid (:func:`validate_span`)."""
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
    meas = {c: (bal[0][i] - env[0][i], bal[1][i] - env[1][i]) for i, c in enumerate(fit)}
    sized = _sized_repair(curves, reads, plan, applied=applied, primaries=primaries,
                          native_white_xy=native_white_xy, peak_luminance=peak_luminance, span=(lo, hi),
                          region=region, max_cv=max_cv, sig=sig, z=z, gamma=gamma, fit=fit, bal=bal)
    fixed, info = sized["fixed"], sized["info"]
    n = len(curves["r"])
    report = {
        "spans": [{"codes": list(region), "audit": [lo, hi], "tints": [[r["lo"], r["hi"]] for r in tints],
                   "changed": info["changed"],
                   "max_departure_pct": round(100.0 * (math.exp(max(max(abs(v[0]), abs(v[1]))
                                                                    for v in meas.values())) - 1.0), 3)}],
        "confirmation": sized["confirmation"], "response": sized["response"], "amplitude": sized["amplitude"],
        "predicted": sized["predicted"], "flat": sized["flat"],
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


def _sized_repair(curves, reads, plan, *, applied, primaries, native_white_xy, peak_luminance, span, region,
                  max_cv, sig, z, gamma, fit, bal) -> dict[str, Any]:
    """The correction SIZED by the audit (2026-11-14 hardware run: the unsized full removal over-corrected
    153-156 to the magenta side).

    1. The response (:func:`fit_response`) on the FIT codes: one blur sigma, gain ``k`` per series. EXPLAINED =
       every fit code's residual within ``z`` x the clean scatter (the residual RMS of every audited code further
       than ``2 sigma + 1`` from the correction region, at its 90 % upper bound; never below the read noise), and
       ``0.5 <= k <= 2`` for a series that carries the tint.
    2. The TARGET is what the response predicts once the cube's departure is gone (its envelopes):
       ``target = model(cube) + u`` with ``u_s(c)`` the predicted change of the cube-shaped full removal
       (:func:`cube_repair`, amplitude 1). After a removal of amplitude ``alpha`` the balance is predicted at
       ``before + alpha u``, i.e. ``target + r + (alpha - 1) u`` with ``r`` the response's residual. The AMPLITUDE
       ``alpha_s`` is the least squares over the FIT codes (``alpha = 1 + sum r u / sum u^2``), its standard error
       from that fit's residual; ``0.5 <= alpha <= 1.5`` or refused. (The reads SIZE the removal.)
    3. The candidate at ``alpha``; its predicted change at EVERY audited code of the span (fit and held-out), the
       predicted after-balance against the target. FLAT = every code within ``z`` x sqrt(clean scatter^2 +
       prediction se^2) -> ``confirmed``; otherwise refused (the fit leaves a residual above the clean scatter).
    4. Light: :func:`light_bounds` — tooth-edge flags per code, evidence only."""
    lo, hi = span
    bal_by = {c: (bal[0][i], bal[1][i]) for i, c in enumerate(fit)}
    resp = fit_response(curves, applied, bal_by, fit, lo=lo, hi=hi, gamma=gamma, max_cv=max_cv)
    sigma = resp["sigma"]
    reach = 2.0 * sigma + 1.0
    clean_codes = [c for c in fit if c < region[0] - reach or c > region[1] + reach]
    tint_codes = [c for c in fit if c not in clean_codes]
    # The clean scatter: the response's residual at EVERY audited code (fit and held-out: reading a held-out
    # code's before-read here fits nothing) beyond the correction region's reach — the scatter an untouched grey
    # shows — as an RMS at its 90 % upper bound (a dozen codes cannot claim a smaller scatter than they show;
    # a median of 10 under-read it by 2x in simulation).
    Pinv0 = invert3x3(_native_P(primaries, native_white_xy, peak_luminance))
    span_codes = [c for c in plan.get("codes") or range(lo, hi + 1) if lo <= c <= hi and reads.get(c)]
    clean_all = [c for c in span_codes if c < region[0] - reach or c > region[1] + reach]
    model_clean = response_model(curves, applied, resp, clean_all, max_cv=max_cv) if clean_all else {}
    scat = []
    for s in range(2):
        r = []
        for c in clean_all:
            sh = matvec(Pinv0, list(reads[c]))
            if all(v > 0 for v in sh):
                r.append((math.log(sh[0] / sh[1]) if s == 0 else math.log(sh[2] / sh[1])) - model_clean[c][s])
        # a structure among the "clean" codes (a feature of its own beside the tint) must not widen the yardstick
        # it is then judged by: codes beyond z x the robust (MAD) scatter are left out of the RMS — and, being
        # span codes, they then fail the flatness test themselves
        if len(r) >= 3:
            s0 = max(sig, 1.4826 * sorted(abs(v) for v in r)[len(r) // 2])
            kept = [v for v in r if abs(v) <= z * s0]
            rms = math.sqrt(sum(v * v for v in kept) / max(1, len(kept) - 1))
            scat.append(max(sig, _upper_sigma(rms, len(kept))))
        else:
            scat.append(sig)
    # the full removal, its predicted change at the fit codes
    full, _ = cube_repair(curves, applied, region, primaries=primaries, native_white_xy=native_white_xy,
                          peak_luminance=peak_luminance, max_cv=max_cv, gamma=gamma, pred_codes=fit)
    u = predicted_change(curves, full, applied, resp, fit, lo=lo, hi=hi, gamma=gamma, max_cv=max_cv)
    amp, amp_se, per = [], [], []
    for s in range(2):
        ser = resp["series"][s]
        r = ser["residual"]
        uu = sum(u[c][s] ** 2 for c in fit)
        alpha = 1.0 + sum(r[c] * u[c][s] for c in fit) / uu if uu > 0 else 1.0
        res = [r[c] + (alpha - 1.0) * u[c][s] for c in fit]
        sd = math.sqrt(sum(v * v for v in res) / max(1, len(fit) - 1))
        amp.append(alpha)
        amp_se.append(sd / math.sqrt(uu) if uu > 0 else float("inf"))
        worst = max(abs(ser["residual"][c]) for c in tint_codes) / (z * scat[s]) if tint_codes else 0.0
        informative = uu > 0 and math.sqrt(uu / max(1, len(fit))) > 2.0 * scat[s]
        per.append({"series": ("r/g", "b/g")[s], "k": round(ser["k"], 4), "k_se": round(ser["k_se"], 4),
                    "informative": informative, "worst_over_tolerance": round(worst, 3),
                    "tolerance_pct": round(100.0 * (math.exp(z * scat[s]) - 1.0), 3),
                    "clean_scatter_pct": round(100.0 * scat[s], 3), "fit_rms_pct": round(100.0 * ser["rms"], 3)})
    fixed, info = cube_repair(curves, applied, region, primaries=primaries, native_white_xy=native_white_xy,
                              peak_luminance=peak_luminance, max_cv=max_cv, gamma=gamma, pred_codes=fit,
                              amplitude=amp)
    codes = [c for c in plan.get("codes") or range(lo, hi + 1) if lo <= c <= hi and reads.get(c)]
    Pinv = invert3x3(_native_P(primaries, native_white_xy, peak_luminance))
    chg = predicted_change(curves, fixed, applied, resp, codes, lo=lo, hi=hi, gamma=gamma, max_cv=max_cv)
    u_all = predicted_change(curves, full, applied, resp, codes, lo=lo, hi=hi, gamma=gamma, max_cv=max_cv)
    model0 = response_model(curves, applied, resp, codes, max_cv=max_cv)
    lb = light_bounds(curves, fixed, applied, reads, codes, primaries=primaries, native_white_xy=native_white_xy,
                      peak_luminance=peak_luminance, luminance_sigma=sig, z=z, gamma=gamma, max_cv=max_cv)
    predicted, worst_flat = {}, 0.0
    for c in codes:
        sh = matvec(Pinv, list(reads[c]))
        if not all(v > 0 for v in sh):
            continue
        b0 = (math.log(sh[0] / sh[1]), math.log(sh[2] / sh[1]))
        row: dict[str, Any] = {}
        for s, nm in enumerate(("rg", "bg")):
            target = model0[c][s] + u_all[c][s]
            se = abs(chg[c][s]) * (amp_se[s] / amp[s] if amp[s] else 0.0)
            post = b0[s] + chg[c][s] - target
            ratio = abs(post) / (z * math.sqrt(scat[s] ** 2 + se ** 2))
            worst_flat = max(worst_flat, ratio)
            row.update({f"change_{nm}": chg[c][s], f"se_{nm}": se, f"target_{nm}": target,
                        f"before_{nm}_pct": round(100.0 * (b0[s] - target), 3),
                        f"post_{nm}_pct": round(100.0 * post, 3), f"post_{nm}_over_tol": round(ratio, 3)})
        if c in lb:
            row.update({"tooth_reach_pct": round(100.0 * lb[c]["reach"], 3), "tooth_edge": lb[c]["tooth_edge"],
                        "drive_moves_codes": lb[c]["drive_moves_codes"]})
        row["fit"] = c in bal_by
        predicted[c] = row
    explained = max(p["worst_over_tolerance"] for p in per)
    k_ok = all(0.5 <= p["k"] <= 2.0 for p in per if p["informative"]) and any(p["informative"] for p in per)
    a_ok = all(0.5 <= a <= 1.5 for a in amp)
    confirmed = explained <= 1.0 and k_ok and a_ok and worst_flat <= 1.0
    why = []
    if explained > 1.0:
        why.append(f"the response does not explain the tint (worst {explained:.2f} x tolerance)")
    if not k_ok:
        why.append("the response gain is not physical or no series carries the tint")
    if not a_ok:
        why.append(f"the amplitude the reads ask for is outside 0.5..1.5 ({[round(a, 3) for a in amp]})")
    if worst_flat > 1.0:
        why.append(f"the predicted after-balance is not flat (worst {worst_flat:.2f} x tolerance)")
    return {
        "fixed": fixed, "info": info,
        "confirmation": {"confirmed": confirmed, "k": per[0]["k"], "per_series": per,
                         "unexplained_over_tolerance": round(explained, 3),
                         "flat_worst_over_tolerance": round(worst_flat, 3), "refused_because": why},
        "response": {"sigma_codes": sigma, "shift_codes": resp["shift"], "k": [p["k"] for p in per],
                     "k_se": [p["k_se"] for p in per], "a": [s_["a"] for s_ in resp["series"]],
                     "b": [s_["b"] for s_ in resp["series"]], "lo": lo, "hi": hi, "gamma": gamma,
                     "clean_codes": clean_codes},
        "amplitude": {"alpha": [round(a, 4) for a in amp], "se": [round(v, 4) for v in amp_se]},
        "predicted": {str(c): {k: (round(v, 7) if isinstance(v, float) else v) for k, v in r.items()}
                      for c, r in predicted.items()},
        "flat": {"worst_over_tolerance": round(worst_flat, 3),
                 "tolerance_pct": [round(100.0 * z * s, 3) for s in scat]},
    }


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
    if report.get("predicted"):
        return _judge_predicted(before, after, report, held_out, Pinv=Pinv, luminance_sigma=luminance_sigma, z=z)
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


def _judge_predicted(before: Mapping[int, Sequence[float]], after: Mapping[int, Sequence[float]],
                     report: Mapping[str, Any], held_out: Sequence[int], *, Pinv, luminance_sigma: float,
                     z: float) -> dict[str, Any]:
    """The verify of a SIZED repair (:func:`_sized_repair`): every held-out code is judged against what the audit
    PREDICTED for it, not against an envelope.

    * ``inside`` = the response predicts a balance change there (>= 0.1 % in either series); ``beside`` = it does
      not. The beside codes' change between the two passes is the pass drift (median, removed everywhere) and its
      robust spread the two-read repeatability ``rep``.
    * BALANCE, per series: the measured change must match the predicted change within ``z x sqrt(rep^2 + fit^2 +
      se^2)`` (``fit`` = the response's residual RMS on the audit — the model's own error, which the 2026-11-14
      validation measured at 0.19-0.25 % RMS on the change; ``se`` = the amplitude's propagated error), AND the
      after-balance must be flat: ``|before - target + measured change| <= z x sqrt(clean^2 + rep^2)``.
    * LIGHT: beside, against the pass repeatability as before. INSIDE it is an OBSERVATION, never a miss: the flow
      cannot predict it on a panel whose light has teeth along the code (the 2026-11-14 run moved 150 by +1.3 % and
      155 by -2.6 % where no model fitted on the audit said so); each inside row says whether a channel's drive
      move crossed a tooth edge (``tooth_edge``)."""
    pred = report["predicted"]
    per = report["confirmation"]["per_series"]
    scat = [p["clean_scatter_pct"] / 100.0 for p in per]
    fitrms = [p["fit_rms_pct"] / 100.0 for p in per]
    sig = max(float(luminance_sigma), _SIGMA_FLOOR)

    def bal(xyz):
        sh = matvec(Pinv, list(xyz))
        if not all(v > 0 for v in sh):
            return None
        return math.log(sh[0] / sh[1]), math.log(sh[2] / sh[1])

    def inside(c):
        p = pred.get(str(c))
        return bool(p) and max(abs(p["change_rg"]), abs(p["change_bg"])) >= 1e-3

    dl, db = [], [[], []]
    for c in held_out:
        b0, a0 = before.get(c), after.get(c)
        if not (b0 and a0 and b0[1] > 0 and a0[1] > 0) or inside(c):
            continue
        dl.append(math.log(a0[1] / b0[1]))
        bb, ba = bal(b0), bal(a0)
        if bb and ba:
            for s in range(2):
                db[s].append(ba[s] - bb[s])

    def med(v):
        return sorted(v)[len(v) // 2] if v else 0.0

    def spread(v, m):
        return 1.4826 * sorted(abs(x - m) for x in v)[len(v) // 2] if len(v) >= 3 else 0.0

    drift = med(dl) if len(dl) >= 3 else 0.0
    tol_l = z * max(math.sqrt(2.0) * sig, spread(dl, drift))
    bdrift = [med(db[s]) if len(db[s]) >= 3 else 0.0 for s in range(2)]
    rep = [max(math.sqrt(2.0) * sig, spread(db[s], bdrift[s])) for s in range(2)]
    rows, out = [], []
    for c in held_out:
        b0, a0 = before.get(c), after.get(c)
        if a0 is None or b0 is None:
            rows.append({"code": c, "status": "unread"})
            continue
        bb, ba = bal(b0), bal(a0)
        p = pred.get(str(c))
        if bb is None or ba is None or not (a0[1] > 0 and b0[1] > 0):
            rows.append({"code": c, "status": "unusable"})
            continue
        where = "inside" if inside(c) else "beside"
        light = math.log(a0[1] / b0[1]) - drift
        row: dict[str, Any] = {"code": c, "where": where,
                               "light_change_pct": round(100.0 * (math.exp(light) - 1.0), 3)}
        missed = []
        for s, nm in enumerate(("rg", "bg")):
            m = ba[s] - bb[s] - bdrift[s]
            pc = p[f"change_{nm}"] if p else 0.0
            se = p[f"se_{nm}"] if p else 0.0
            t_chg = z * math.sqrt(rep[s] ** 2 + fitrms[s] ** 2 + se ** 2)
            row[f"change_{nm}_pct"] = round(100.0 * m, 3)
            row[f"predicted_{nm}_pct"] = round(100.0 * pc, 3)
            row[f"change_{nm}_tol_pct"] = round(100.0 * t_chg, 3)
            if abs(m - pc) > t_chg:
                missed.append(f"{nm} change")
            if p:
                post = bb[s] - p[f"target_{nm}"] + m
                t_flat = z * math.sqrt(scat[s] ** 2 + rep[s] ** 2)
                row[f"post_{nm}_pct"] = round(100.0 * post, 3)
                row[f"post_{nm}_tol_pct"] = round(100.0 * t_flat, 3)
                if abs(post) > t_flat:
                    missed.append(f"{nm} not flat")
        if where == "beside":
            if abs(light) > tol_l:
                missed.append("light")
        else:
            row["light"] = "observation (not judged: inside-span light is not predictable on this panel)"
            if p and "tooth_edge" in p:
                row["tooth_edge"] = p["tooth_edge"]
        tot = sum(a0)
        row.update({"x": round(a0[0] / tot, 5), "y": round(a0[1] / tot, 5), "missed": missed,
                    "status": "out" if missed else "ok"})
        # the keep seam's older readers look for rg_pct / bg_pct: the after-balance against the target
        row["rg_pct"], row["bg_pct"] = row.get("post_rg_pct"), row.get("post_bg_pct")
        rows.append(row)
        if missed:
            out.append(row)
    return {"held_out": rows, "out": out, "judged_against": "the audit's prediction (sized repair)",
            "inside": sum(1 for r in rows if r.get("where") == "inside"),
            "beside": sum(1 for r in rows if r.get("where") == "beside"),
            "light_drift_pct": round(100.0 * (math.exp(drift) - 1.0), 3),
            "balance_drift_pct": [round(100.0 * (math.exp(v) - 1.0), 3) for v in bdrift],
            "tooth_edge_codes": [r["code"] for r in rows if r.get("tooth_edge")],
            "tolerance_pct": {"r/g": round(100.0 * z * math.sqrt(scat[0] ** 2 + rep[0] ** 2), 3),
                              "b/g": round(100.0 * z * math.sqrt(scat[1] ** 2 + rep[1] ** 2), 3),
                              "light_beside": round(100.0 * (math.exp(tol_l) - 1.0), 3)}}
