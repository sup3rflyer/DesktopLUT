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
from typing import Optional, Tuple

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
