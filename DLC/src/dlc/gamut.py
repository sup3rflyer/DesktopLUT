"""Chromaticity-gamut geometry (stdlib only).

Answers one question for the preflight gamut-reachability tell: does a display's
MEASURED native gamut (the DIP's ``native_primaries``) cover a target colour space?
A target primary that falls OUTSIDE the native RGB triangle is physically
unreachable — the calibration will CLIP there no matter what — so the preflight
surfaces it up front (coverage %, which primaries, by how much) and informs the
gamut-map-vs-clip choice, instead of it surfacing patch-by-patch in the cube
residuals.

Dependency-free (no numpy / colour): plain 2-D CIE-1931 xy geometry, so it lives
on the spine like the rest of the core. The standard primaries are fixed constants
(so the engine's ``colour`` library is not needed just to read a gamut triangle).
"""

from __future__ import annotations

import math
from typing import Optional

__all__ = ["STANDARD_PRIMARIES", "target_primaries", "point_in_triangle",
           "polygon_area", "clip_convex", "gamut_coverage", "reachable_fraction"]

Pt = tuple[float, float]

# CIE 1931 xy of the RGB primaries — fixed standard constants.
STANDARD_PRIMARIES: dict[str, dict[str, Pt]] = {
    "Rec.709":    {"R": (0.640, 0.330), "G": (0.300, 0.600), "B": (0.150, 0.060)},
    "Rec.2020":   {"R": (0.708, 0.292), "G": (0.170, 0.797), "B": (0.131, 0.046)},
    "Display P3": {"R": (0.680, 0.320), "G": (0.265, 0.690), "B": (0.150, 0.060)},
}

# colour-lib / profile / common spellings → the canonical keys above.
_ALIASES = {
    "rec.709": "Rec.709", "rec709": "Rec.709", "bt.709": "Rec.709",
    "itu-r bt.709": "Rec.709", "srgb": "Rec.709", "709": "Rec.709",
    "rec.2020": "Rec.2020", "rec2020": "Rec.2020", "bt.2020": "Rec.2020",
    "itu-r bt.2020": "Rec.2020", "2020": "Rec.2020",
    "display p3": "Display P3", "displayp3": "Display P3", "p3-d65": "Display P3",
    "dci-p3": "Display P3", "p3": "Display P3", "display-p3": "Display P3",
}


def target_primaries(colorspace: Optional[str]) -> Optional[dict[str, Pt]]:
    """The R/G/B xy of a named target colour space, or ``None`` if unknown (the tell then
    no-ops rather than guessing)."""
    if not colorspace:
        return None
    key = _ALIASES.get(colorspace.strip().lower())
    return STANDARD_PRIMARIES.get(key) if key else None


def _cross(o: Pt, a: Pt, b: Pt) -> float:
    """Z of (a-o) × (b-o) — >0 ⇒ b is left of o→a (CCW)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def point_in_triangle(p: Pt, a: Pt, b: Pt, c: Pt, *, eps: float = 1e-4) -> bool:
    """True if ``p`` is inside triangle ``abc`` (winding-agnostic; a point on the boundary
    within ``eps`` counts as inside — a target primary essentially ON the native edge is
    'reachable', not a false unreachable from measurement noise)."""
    d1, d2, d3 = _cross(a, b, p), _cross(b, c, p), _cross(c, a, p)
    has_neg = (d1 < -eps) or (d2 < -eps) or (d3 < -eps)
    has_pos = (d1 > eps) or (d2 > eps) or (d3 > eps)
    return not (has_neg and has_pos)


def polygon_area(poly: list[Pt]) -> float:
    """Unsigned shoelace area of a simple polygon (0 for < 3 vertices)."""
    n = len(poly)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _ccw(poly: list[Pt]) -> list[Pt]:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return poly if s >= 0 else list(reversed(poly))


def _intersect(p1: Pt, p2: Pt, a: Pt, b: Pt) -> Pt:
    """Intersection of line ``p1→p2`` with line ``a→b`` (parallel ⇒ ``p2``)."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = a
    x4, y4 = b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-15:
        return p2
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def clip_convex(subject: list[Pt], clip: list[Pt]) -> list[Pt]:
    """Sutherland–Hodgman: clip the ``subject`` polygon by the CONVEX ``clip`` polygon (a
    triangle here), returning the intersection polygon (possibly empty). Used for the true
    overlap area, so the coverage ratio is exact, not an all-or-nothing per-primary guess."""
    out = list(subject)
    cl = _ccw(list(clip))
    for i in range(len(cl)):
        a = cl[i]
        b = cl[(i + 1) % len(cl)]
        inp = out
        out = []
        if not inp:
            break
        for j in range(len(inp)):
            cur = inp[j]
            prev = inp[j - 1]
            cur_in = _cross(a, b, cur) >= -1e-12
            prev_in = _cross(a, b, prev) >= -1e-12
            if cur_in:
                if not prev_in:
                    out.append(_intersect(prev, cur, a, b))
                out.append(cur)
            elif prev_in:
                out.append(_intersect(prev, cur, a, b))
    return out


def _dist_point_segment(p: Pt, a: Pt, b: Pt) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _dist_point_to_triangle(p: Pt, tri: list[Pt]) -> float:
    """xy distance from ``p`` to the nearest edge of ``tri`` (how far OUTSIDE the gamut a
    given unreachable primary is — the magnitude of the shortfall)."""
    return min(_dist_point_segment(p, tri[i], tri[(i + 1) % 3]) for i in range(3))


def reachable_fraction(white: Pt, primary: Pt, native_tri: list[Pt]) -> float:
    """Fraction along the line ``white → primary`` that stays inside the panel's native gamut
    triangle. ``1.0`` when the target ``primary`` is itself reachable (inside the triangle);
    otherwise the ``[0,1]`` fraction at which the segment crosses the native boundary — i.e.
    how far toward a target primary the panel can actually go before it clips. Used to CAP the
    saturation of generated colour patches so the most-saturated patch lands ON the panel's
    gamut edge instead of at an unreachable target primary (wasted measurement).

    Geometry is in CIE-1931 xy. White is assumed inside the native triangle (the calibration
    target white is, by construction); a degenerate triangle or a white-on-the-boundary edge
    case returns ``1.0`` (no cap) rather than over-clamping."""
    if point_in_triangle(primary, native_tri[0], native_tri[1], native_tri[2]):
        return 1.0
    # white→primary exits the (convex) triangle across exactly one edge; find the nearest crossing.
    best: Optional[float] = None
    wp2 = (primary[0] - white[0]) ** 2 + (primary[1] - white[1]) ** 2
    if wp2 <= 1e-18:
        return 1.0
    for i in range(3):
        a, b = native_tri[i], native_tri[(i + 1) % 3]
        x = _intersect(white, primary, a, b)
        # parameter t along white→primary, and s along the edge a→b — both must be in [0,1]
        t = ((x[0] - white[0]) * (primary[0] - white[0]) + (x[1] - white[1]) * (primary[1] - white[1])) / wp2
        ex, ey = b[0] - a[0], b[1] - a[1]
        es = ex * ex + ey * ey
        s = (((x[0] - a[0]) * ex + (x[1] - a[1]) * ey) / es) if es > 1e-18 else -1.0
        if -1e-9 <= t <= 1.0 + 1e-9 and -1e-6 <= s <= 1.0 + 1e-6:
            t = max(0.0, min(1.0, t))
            if best is None or t < best:
                best = t
    return best if best is not None else 1.0


def gamut_coverage(native: dict[str, Pt], target: dict[str, Pt]) -> dict:
    """How well a measured native gamut covers a target. Returns ``coverage_ratio`` (exact
    overlap area / target area, in [0,1]), ``reachable`` (per target primary inside the
    native triangle?), ``shortfall`` (xy distance outside, per unreachable primary), and the
    triangle areas. ``covered`` ⇒ every target primary is reachable.

    A DEGENERATE native triangle (collinear / coincident primaries — a corrupt or botched
    characterization, never a real panel) is reported honestly: ``degenerate=True``,
    nothing covered, coverage 0. Without the guard the sign-agnostic geometry above would
    score a point-gamut as covering everything (all cross products ≈ 0 ⇒ every membership
    test passes) — the tell must never report a broken measurement as a perfect panel."""
    nt = [native["R"], native["G"], native["B"]]
    tt = [target["R"], target["G"], target["B"]]
    tgt_area = polygon_area(tt)
    native_area = polygon_area(nt)
    if native_area <= 1e-6:   # same guard as calibrate._reachable_primaries (#C3)
        return {"covered": False, "coverage_ratio": 0.0,
                "reachable": {ch: False for ch in ("R", "G", "B")},
                "shortfall": {ch: round(_dist_point_to_triangle(target[ch], nt), 5)
                              for ch in ("R", "G", "B")},
                "native_area": native_area, "target_area": tgt_area, "degenerate": True}
    reachable = {ch: point_in_triangle(target[ch], *nt) for ch in ("R", "G", "B")}
    inter_area = polygon_area(clip_convex(tt, nt))
    cov = max(0.0, min(1.0, inter_area / tgt_area)) if tgt_area > 0 else 0.0
    shortfall = {ch: round(_dist_point_to_triangle(target[ch], nt), 5)
                 for ch in ("R", "G", "B") if not reachable[ch]}
    return {"covered": all(reachable.values()), "coverage_ratio": cov,
            "reachable": reachable, "shortfall": shortfall,
            "native_area": native_area, "target_area": tgt_area, "degenerate": False}
