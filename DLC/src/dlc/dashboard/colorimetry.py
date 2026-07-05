"""Dependency-free CCT + Duv from chromaticity — the dashboard's live colour readout.

The dashboard must stay stdlib-only (it runs anywhere the spine does, without the
``[engine]`` extras), so it can't reach for ``colour-science``. This module gives a
correct, well-established **Robertson (1968)** correlated-colour-temperature + Duv
solve straight from CIE 1931 ``xy``.

This is a *monitoring* readout, not the calibration's source of truth: the
authoritative white/dE come from the scoring stage and ride the spine as
``metrics_scored``. Robertson is the standard method calibration tools use for a live
CCT/Duv display; it's accurate to a few kelvin and ~1e-3 Duv across 2000-15000 K,
which is exactly the regime a display calibration lives in.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from ..colormath import invert3x3, matvec, rgb_to_xyz_matrix

# ST 2084 comes from dlc._pq — the one shared stdlib copy (pinned against
# colour.eotf_ST2084 in the golden tests), so the dashboard's PQ can never
# disagree with the cube/patch math.
from .._pq import eotf_norm as _pq_eotf_norm
from .._pq import oetf_norm as _pq_oetf_norm

# Robertson's 31 isotemperature lines (Wyszecki & Stiles, Table 1(3.11)). Columns:
# reciprocal megakelvin (mired, i.e. 1e6 / T), CIE 1960 UCS u, v, and the slope of the
# isotemperature line at that point. The locus runs from infinite temperature (mired 0)
# down to ~1667 K (mired 600).
_ROBERTSON: tuple[tuple[float, float, float, float], ...] = (
    (0.0, 0.18006, 0.26352, -0.24341),
    (10.0, 0.18066, 0.26589, -0.25479),
    (20.0, 0.18133, 0.26846, -0.26876),
    (30.0, 0.18208, 0.27119, -0.28539),
    (40.0, 0.18293, 0.27407, -0.30470),
    (50.0, 0.18388, 0.27709, -0.32675),
    (60.0, 0.18494, 0.28021, -0.35156),
    (70.0, 0.18611, 0.28342, -0.37915),
    (80.0, 0.18740, 0.28668, -0.40955),
    (90.0, 0.18880, 0.28997, -0.44278),
    (100.0, 0.19032, 0.29326, -0.47888),
    (125.0, 0.19462, 0.30141, -0.58204),
    (150.0, 0.19962, 0.30921, -0.70471),
    (175.0, 0.20525, 0.31647, -0.84901),
    (200.0, 0.21142, 0.32312, -1.0182),
    (225.0, 0.21807, 0.32909, -1.2168),
    (250.0, 0.22511, 0.33439, -1.4512),
    (275.0, 0.23247, 0.33904, -1.7298),
    (300.0, 0.24010, 0.34308, -2.0637),
    (325.0, 0.24792, 0.34655, -2.4681),
    (350.0, 0.25591, 0.34951, -2.9641),
    (375.0, 0.26400, 0.35200, -3.5814),
    (400.0, 0.27218, 0.35407, -4.3633),
    (425.0, 0.28039, 0.35577, -5.3762),
    (450.0, 0.28863, 0.35714, -6.7262),
    (475.0, 0.29685, 0.35823, -8.5955),
    (500.0, 0.30505, 0.35907, -11.324),
    (525.0, 0.31320, 0.35968, -15.628),
    (550.0, 0.32129, 0.36011, -23.325),
    (575.0, 0.32931, 0.36038, -40.770),
    (600.0, 0.33724, 0.36051, -116.45),
)


def xy_to_uv60(x: float, y: float) -> Optional[Tuple[float, float]]:
    """CIE 1931 ``xy`` → CIE 1960 UCS ``(u, v)`` (the space Duv is defined in).

    Returns ``None`` for a degenerate chromaticity (the denominator vanishes), so a
    black / no-signal read doesn't blow up the readout.
    """
    denom = -2.0 * x + 12.0 * y + 3.0
    if abs(denom) < 1e-12:
        return None
    return (4.0 * x / denom, 6.0 * y / denom)


# A CCT is only meaningful near the Planckian locus. Robertson will "bracket" (and so
# report a temperature for) points far off the locus too, but their Duv is implausibly
# large — a real display white sits within a few ×1e-3 of the locus. Past this band we
# return None so a stray coloured patch mislabeled neutral shows a dash, not a junk CCT.
_MAX_PLAUSIBLE_DUV = 0.05


def cct_duv(x: float, y: float) -> Optional[Tuple[float, float]]:
    """Correlated colour temperature (K) and Duv from CIE 1931 ``xy`` via Robertson.

    ``Duv`` is the signed distance from the Planckian locus in CIE 1960 UCS: positive
    above the locus (toward green), negative below (toward magenta) — the same
    convention calibration tools use for "tint". Returns ``None`` when the point is a
    degenerate chromaticity, never brackets the locus, or sits implausibly far from it
    (``|Duv| > _MAX_PLAUSIBLE_DUV``) — so a non-neutral colour reads as a clean dash
    rather than a confident but meaningless temperature.
    """
    uv = xy_to_uv60(x, y)
    if uv is None:
        return None
    u, v = uv

    last_d = 0.0
    for i, (mired, ui, vi, ti) in enumerate(_ROBERTSON):
        # Signed perpendicular distance from the point to isotemperature line i.
        di = ((v - vi) - ti * (u - ui)) / math.sqrt(1.0 + ti * ti)
        if i > 0 and di <= 0.0 and last_d > 0.0:
            # The point is bracketed between lines i-1 and i; interpolate by the ratio
            # of perpendicular distances (Robertson's method).
            prev_mired, pu, pv, _ = _ROBERTSON[i - 1]
            frac = last_d / (last_d - di) if (last_d - di) != 0.0 else 0.0
            mired_interp = prev_mired + frac * (mired - prev_mired)
            if mired_interp <= 0.0:
                return None
            cct = 1.0e6 / mired_interp

            # Duv: distance to the interpolated point on the locus segment, signed by
            # which side of the locus the point sits on (above ⇒ +, toward green).
            lu = pu + frac * (ui - pu)
            lv = pv + frac * (vi - pv)
            duv = math.hypot(u - lu, v - lv)
            if duv > _MAX_PLAUSIBLE_DUV:
                return None             # too far off the locus for a CCT to mean anything
            duv = math.copysign(duv, v - lv)
            return (cct, duv)
        last_d = di
    return None


def uv60_to_xy(u: float, v: float) -> Tuple[float, float]:
    """CIE 1960 UCS ``(u, v)`` → CIE 1931 ``xy`` (inverse of :func:`xy_to_uv60`)."""
    denom = 2.0 * u - 8.0 * v + 4.0
    if abs(denom) < 1e-12:
        return (0.0, 0.0)
    return (3.0 * u / denom, 2.0 * v / denom)


def planckian_locus_xy() -> list[Tuple[float, float]]:
    """The Planckian locus as CIE 1931 ``xy`` points (from the Robertson table, warm→cool),
    for the dashboard's CIE chart. Stdlib-only, so the chart needs no colour-science."""
    return [uv60_to_xy(u, v) for (_mired, u, v, _t) in reversed(_ROBERTSON)]


# ---------------------------------------------------------------------------
# Per-patch ΔE — dependency-free, CROSS-VALIDATED against dlc.metrics (SDR CIEDE2000)
# and dlc.engine.model.score_hdr (HDR dE_ITP) in tests. The authoritative dE still rides
# the spine as metrics_scored at verify; this is the LIVE per-patch monitor the event log,
# the Live Patch tile, and the rolling-ΔE header read, so it must MATCH the engine's numbers.
# ---------------------------------------------------------------------------

_SRGB_PRIMARIES = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))
_REC2020_PRIMARIES = ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
_D65_XY = (0.3127, 0.3290)

# BT.2100/2124 ICtCp matrices (Dolby 2016) — the exact constants colour.XYZ_to_ICtCp uses.
_M_BT2020_RGB_TO_LMS = [[1688 / 4096, 2146 / 4096, 262 / 4096],
                        [683 / 4096, 2951 / 4096, 462 / 4096],
                        [99 / 4096, 309 / 4096, 3688 / 4096]]
_M_LMS_P_TO_ICTCP = [[2048 / 4096, 2048 / 4096, 0.0],
                     [6610 / 4096, -13613 / 4096, 7003 / 4096],
                     [17933 / 4096, -17390 / 4096, -543 / 4096]]
# XYZ (absolute cd/m²) → BT.2020 linear RGB (D65); BT.2020 white IS D65 so no chromatic adapt.
_XYZ_TO_BT2020 = invert3x3(rgb_to_xyz_matrix(
    _REC2020_PRIMARIES[0][0], _REC2020_PRIMARIES[0][1], _REC2020_PRIMARIES[1][0],
    _REC2020_PRIMARIES[1][1], _REC2020_PRIMARIES[2][0], _REC2020_PRIMARIES[2][1],
    _D65_XY[0], _D65_XY[1], white_Y=1.0))
_DE_ITP_SCALE = 720.0


def measured_xyz(x: float, y: float, big_y: float) -> tuple[float, float, float]:
    """Absolute XYZ from chromaticity ``xy`` + luminance ``Y`` (the dashboard has xy + Y per read)."""
    if y <= 0:
        return (0.0, 0.0, 0.0)
    return (big_y * x / y, big_y, big_y * (1.0 - x - y) / y)


def xyz_to_lab(xyz: Sequence[float], white: Sequence[float]) -> tuple[float, float, float]:
    """CIE XYZ → L*a*b* against ``white`` (port of dlc.metrics.xyz_to_lab; clamps negatives)."""
    eps, kappa = 216 / 24389, 24389 / 27

    def f(v: float) -> float:
        return v ** (1 / 3) if v > eps else (kappa * v + 16) / 116
    xr = max(0.0, xyz[0] / white[0]) if white[0] else 0.0
    yr = max(0.0, xyz[1] / white[1]) if white[1] else 0.0
    zr = max(0.0, xyz[2] / white[2]) if white[2] else 0.0
    fx, fy, fz = f(xr), f(yr), f(zr)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _hue_angle(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    ang = math.degrees(math.atan2(b, a))
    return ang + 360 if ang < 0 else ang


def delta_e2000(lab1: Sequence[float], lab2: Sequence[float]) -> float:
    """CIEDE2000 (port of dlc.metrics.delta_e2000 — identical formula, kept dependency-free)."""
    return _de2000_full(lab1, lab2)[0]


def _de2000_full(lab1: Sequence[float], lab2: Sequence[float]) -> tuple[float, dict]:
    """CIEDE2000 scalar **and** its weighted lightness/chroma/hue terms (ΔL'/SL, ΔC'/SC,
    ΔH'/SH). Pass ``lab1`` = ideal, ``lab2`` = measured, so a positive component means the
    measured patch exceeds the target on that axis. The three terms' quadrature equals the
    scalar up to the small RT rotation cross-term (negligible except at high chroma)."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt((c_bar ** 7) / ((c_bar ** 7) + (25 ** 7)))) if c_bar else 0.0
    ap1, ap2 = (1 + g) * a1, (1 + g) * a2
    cp1, cp2 = math.hypot(ap1, b1), math.hypot(ap2, b2)
    hp1, hp2 = _hue_angle(ap1, b1), _hue_angle(ap2, b2)
    delta_lp = l2 - l1
    delta_cp = cp2 - cp1
    if cp1 * cp2 == 0:
        delta_hp = 0.0
    else:
        diff = hp2 - hp1
        diff = diff - 360 if diff > 180 else (diff + 360 if diff < -180 else diff)
        delta_hp = diff
    delta_hp_term = 2 * math.sqrt(cp1 * cp2) * math.sin(math.radians(delta_hp / 2))
    l_bar = (l1 + l2) / 2
    cp_bar = (cp1 + cp2) / 2
    if cp1 * cp2 == 0:
        hp_bar = hp1 + hp2
    else:
        diff = abs(hp1 - hp2)
        if diff <= 180:
            hp_bar = (hp1 + hp2) / 2
        elif hp1 + hp2 < 360:
            hp_bar = (hp1 + hp2 + 360) / 2
        else:
            hp_bar = (hp1 + hp2 - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hp_bar - 30)) + 0.24 * math.cos(math.radians(2 * hp_bar))
         + 0.32 * math.cos(math.radians(3 * hp_bar + 6)) - 0.20 * math.cos(math.radians(4 * hp_bar - 63)))
    delta_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    rc = 2 * math.sqrt((cp_bar ** 7) / ((cp_bar ** 7) + (25 ** 7))) if cp_bar else 0.0
    sl = 1 + ((0.015 * ((l_bar - 50) ** 2)) / math.sqrt(20 + ((l_bar - 50) ** 2)))
    sc = 1 + 0.045 * cp_bar
    sh = 1 + 0.015 * cp_bar * t
    rt = -math.sin(math.radians(2 * delta_theta)) * rc
    term_l, term_c, term_h = delta_lp / sl, delta_cp / sc, delta_hp_term / sh
    value = term_l ** 2 + term_c ** 2 + term_h ** 2 + rt * term_c * term_h
    return (math.sqrt(max(0.0, value)), {"L": term_l, "C": term_c, "H": term_h})


def _xyz_to_ictcp(xyz_abs: Sequence[float]) -> tuple[float, float, float]:
    """Absolute XYZ (cd/m²) → ICtCp (BT.2124, Dolby 2016) — matches colour.XYZ_to_ICtCp."""
    # NB: do NOT clamp the BT.2020 RGB to >=0 — colour.XYZ_to_ICtCp lets out-of-gamut (negative)
    # RGB flow through to LMS (the M1 mix often keeps LMS positive); only the PQ stage clamps
    # negative light. Clamping RGB here diverges from the engine on saturated/out-of-gamut patches.
    rgb = matvec(_XYZ_TO_BT2020, xyz_abs)
    lms = matvec(_M_BT2020_RGB_TO_LMS, rgb)
    lms_p = [_pq_oetf_norm(max(0.0, c) / 10000.0) for c in lms]
    return tuple(matvec(_M_LMS_P_TO_ICTCP, lms_p))  # type: ignore[return-value]


def _lch_components(l_ideal: float, plane_ideal: tuple[float, float],
                    l_meas: float, plane_meas: tuple[float, float],
                    scale: float = 1.0) -> dict:
    """Split a metric's error into signed **lightness / chroma / hue** contributions whose
    quadrature equals the metric's Euclidean ΔE (the chroma/hue split is the radial/tangential
    decomposition of the plane-error vector about the *target* hue direction — an orthonormal
    basis, so L²+C²+H² is exact). +L brighter than target, +C more chromatic, +H a CCW hue
    rotation. When the target is achromatic the hue direction is undefined → all plane error
    is reported as chroma."""
    dl = (l_meas - l_ideal) * scale
    pxi, pyi = plane_ideal
    dvx, dvy = (plane_meas[0] - pxi), (plane_meas[1] - pyi)
    r = math.hypot(pxi, pyi)
    if r < 1e-9:
        return {"L": dl, "C": math.hypot(dvx, dvy) * scale, "H": 0.0}
    ux, uy = pxi / r, pyi / r
    return {"L": dl, "C": (dvx * ux + dvy * uy) * scale, "H": (-dvx * uy + dvy * ux) * scale}


def _itp_metric(meas_xyz: Sequence[float], ideal_xyz: Sequence[float]) -> dict:
    im = list(_xyz_to_ictcp(meas_xyz));  im[1] *= 0.5      # Ct -> T (BT.2124)
    ii = list(_xyz_to_ictcp(ideal_xyz)); ii[1] *= 0.5
    de = _DE_ITP_SCALE * math.sqrt(sum((a - b) ** 2 for a, b in zip(im, ii)))
    return {"de": de, **_lch_components(ii[0], (ii[1], ii[2]), im[0], (im[1], im[2]),
                                        scale=_DE_ITP_SCALE)}


def _de2000_metric(meas_xyz: Sequence[float], ideal_xyz: Sequence[float],
                   white_xyz: Sequence[float]) -> dict:
    de, comp = _de2000_full(xyz_to_lab(ideal_xyz, white_xyz), xyz_to_lab(meas_xyz, white_xyz))
    return {"de": de, **comp}


def _white_xyz(white_xy: Sequence[float], lum: float) -> tuple[float, float, float]:
    wx, wy = white_xy[0], white_xy[1]
    return (wx / wy * lum, lum, (1.0 - wx - wy) / wy * lum)


def _target_xyy(ideal_xyz: Sequence[float]) -> Optional[dict]:
    """The patch's ideal target as ``{x, y, Y}`` (CIE 1931 chromaticity + luminance/nits) — so the
    dashboard can put measured vs target side by side, the way calibration tools present a read.
    ``None`` if the target is degenerate (sum ≤ 0, e.g. a pure-black patch)."""
    s = ideal_xyz[0] + ideal_xyz[1] + ideal_xyz[2]
    if s <= 0:
        return None
    return {"x": ideal_xyz[0] / s, "y": ideal_xyz[1] / s, "Y": ideal_xyz[1]}


def _npm(primaries: tuple, white_xy: Sequence[float]) -> list[list[float]]:
    (rx, ry), (gx, gy), (bx, by) = primaries
    return rgb_to_xyz_matrix(rx, ry, gx, gy, bx, by, white_xy[0], white_xy[1], white_Y=1.0)


def patch_deltas(signal: Sequence[float], x: float, y: float, big_y: float, *,
                 is_hdr: bool, white_xy: Sequence[float] = _D65_XY,
                 luminance: Optional[float] = None, gamma: float = 2.2
                 ) -> Optional[dict]:
    """The per-patch ΔE for one measured patch vs its ideal target, with its signed
    lightness/chroma/hue split. ``signal`` is the patch's normalised RGB; ``(x,y,big_y)`` its
    measured chromaticity + luminance (cd/m²).

    Returns ``{"scoring": <key>, "metrics": {<key>: {"de", "L", "C", "H"}}}`` — or ``None`` for a
    degenerate (``y<=0``) / non-finite read that must NOT yield a plausible finite ΔE (it would
    silently poison the live rolling average; the engine sanitizes the same failure mode).

    ONE metric per mode, matching the spine's scorer exactly: **dE_ITP for HDR (PQ/Rec.2020),
    CIEDE2000 for SDR**. There is no alternate "viewing lens" — the dashboard shows the metric the
    run is actually optimised against, so a number on screen is never a different scale than the
    score. dE_ITP and CIEDE2000 here stay bit-identical to ``dlc.metrics`` / ``engine.score_hdr``."""
    if not signal or len(signal) < 3 or big_y is None or x is None or y is None:
        return None
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(big_y)):
        return None
    if y <= 0.0 or big_y <= 0.0 or not all(math.isfinite(s) for s in signal[:3]):
        return None
    meas = measured_xyz(x, y, big_y)
    if is_hdr:
        npm = _npm(_REC2020_PRIMARIES, white_xy)
        nits = [_pq_eotf_norm(max(0.0, min(1.0, s))) * 10000.0 for s in signal[:3]]
        ideal = matvec(npm, nits)                                  # absolute XYZ (RGB_to_XYZ·10000)
        return {"scoring": "itp", "target": _target_xyy(ideal),
                "metrics": {"itp": _itp_metric(meas, ideal)}}
    if luminance is None or luminance <= 0:
        return None
    npm = _npm(_SRGB_PRIMARIES, white_xy)
    linear = [max(0.0, min(1.0, s)) ** gamma for s in signal[:3]]
    ideal = [luminance * v for v in matvec(npm, linear)]
    return {"scoring": "de2000", "target": _target_xyy(ideal),
            "metrics": {"de2000": _de2000_metric(meas, ideal, _white_xyz(white_xy, luminance))}}


def patch_delta_e(signal: Sequence[float], x: float, y: float, big_y: float, *,
                  is_hdr: bool, white_xy: Sequence[float] = _D65_XY,
                  luminance: Optional[float] = None, gamma: float = 2.2
                  ) -> Optional[float]:
    """The single scoring-metric ΔE for a patch (dE_ITP for HDR, CIEDE2000 for SDR) — the spine's
    authoritative scalar. Thin wrapper over :func:`patch_deltas` for callers that want one number."""
    d = patch_deltas(signal, x, y, big_y, is_hdr=is_hdr, white_xy=white_xy,
                     luminance=luminance, gamma=gamma)
    return None if d is None else d["metrics"][d["scoring"]]["de"]


def linear_rgb(x: Optional[float], y: Optional[float], big_y: Optional[float], *,
               is_hdr: bool, white_xy: Sequence[float] = _D65_XY) -> Optional[tuple]:
    """A measured gray's **linear R/G/B contributions** in the target colourspace (``inv(NPM)·XYZ``)
    — the basis for both white balance and per-channel thermal-drift tracking. ``None`` for a
    degenerate/non-finite read."""
    if x is None or y is None or big_y is None or y <= 0 or big_y <= 0:
        return None
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(big_y)):
        return None
    lin = matvec(invert3x3(_npm(_REC2020_PRIMARIES if is_hdr else _SRGB_PRIMARIES, white_xy)),
                 measured_xyz(x, y, big_y))
    return (lin[0], lin[1], lin[2])


def rgb_balance(x: Optional[float], y: Optional[float], big_y: Optional[float], *,
                is_hdr: bool, white_xy: Sequence[float] = _D65_XY) -> Optional[tuple]:
    """Per-channel **R/G/B deviation from neutral (%)** for a measured gray, against the target
    white — the classic grayscale RGB-balance reading. Linear RGB normalised to the three-channel
    mean, so a perfectly neutral gray reads ``(0, 0, 0)`` at ANY luminance (this is pure white
    balance, decoupled from the luminance error the EOTF chart owns). ``None`` for a degenerate
    read or a non-positive mean (e.g. near-black, where balance is noise)."""
    lin = linear_rgb(x, y, big_y, is_hdr=is_hdr, white_xy=white_xy)
    if lin is None:
        return None
    mean = (lin[0] + lin[1] + lin[2]) / 3.0
    if mean <= 0:
        return None
    return ((lin[0] / mean - 1.0) * 100.0, (lin[1] / mean - 1.0) * 100.0,
            (lin[2] / mean - 1.0) * 100.0)


def neutral_metrics(x: float, y: float) -> dict:
    """A compact CCT/Duv payload for one chromaticity, shaped for the wire.

    Always returns the keys (``cct``/``duv`` ``None`` when undefined) so the browser
    can bind to a stable shape and just render a dash for the missing case.
    """
    solved = cct_duv(x, y)
    if solved is None:
        return {"cct": None, "duv": None}
    cct, duv = solved
    return {"cct": round(cct, 1), "duv": round(duv, 5)}
