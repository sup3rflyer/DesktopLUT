"""Small, dependency-free color primitives used by the refinement control law.

Kept standalone (no imports from the rest of the package) so it is trivially
testable and reusable. dE2000 lives in metrics.py; this module is matrices,
chromaticity, and the RGB<->XYZ primaries transform.
"""

from __future__ import annotations

from typing import Sequence

Vec3 = tuple[float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]


def matvec(m: Sequence[Sequence[float]], v: Sequence[float]) -> Vec3:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def invert3x3(m: Sequence[Sequence[float]]) -> list[list[float]]:
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("singular matrix")
    inv_det = 1.0 / det
    return [
        [(e * i - f * h) * inv_det, (c * h - b * i) * inv_det, (b * f - c * e) * inv_det],
        [(f * g - d * i) * inv_det, (a * i - c * g) * inv_det, (c * d - a * f) * inv_det],
        [(d * h - e * g) * inv_det, (b * g - a * h) * inv_det, (a * e - b * d) * inv_det],
    ]


def xy_to_XYZ(x: float, y: float, Y: float = 1.0) -> Vec3:
    """Chromaticity (x, y) + luminance Y -> XYZ."""
    if y <= 0.0:
        return (0.0, 0.0, 0.0)
    return (x / y * Y, Y, (1.0 - x - y) / y * Y)


def XYZ_to_xy(X: float, Y: float, Z: float) -> tuple[float, float]:
    s = X + Y + Z
    if s <= 0.0:
        return (0.0, 0.0)
    return (X / s, Y / s)


def rgb_to_xyz_matrix(
    rx: float, ry: float,
    gx: float, gy: float,
    bx: float, by: float,
    wx: float, wy: float,
    white_Y: float = 1.0,
) -> list[list[float]]:
    """Build the linear-RGB -> XYZ matrix for a display with the given primaries
    and white point, scaled so RGB=(1,1,1) reproduces the white at luminance
    white_Y. This is the standard SMPTE/derivation used by ICC and color tools.
    """
    # Unscaled primary XYZ (each column is a primary at Y=1 relative shape).
    Xr, Yr, Zr = xy_to_XYZ(rx, ry, 1.0)
    Xg, Yg, Zg = xy_to_XYZ(gx, gy, 1.0)
    Xb, Yb, Zb = xy_to_XYZ(bx, by, 1.0)
    m = [
        [Xr, Xg, Xb],
        [Yr, Yg, Yb],
        [Zr, Zg, Zb],
    ]
    w = xy_to_XYZ(wx, wy, white_Y)
    s = matvec(invert3x3(m), w)  # per-channel scale so columns sum to white
    return [
        [m[0][0] * s[0], m[0][1] * s[1], m[0][2] * s[2]],
        [m[1][0] * s[0], m[1][1] * s[1], m[1][2] * s[2]],
        [m[2][0] * s[0], m[2][1] * s[1], m[2][2] * s[2]],
    ]


def clamp(value: float, low: float, high: float) -> float:
    return high if value > high else low if value < low else value
