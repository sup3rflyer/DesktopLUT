#!/usr/bin/env python
"""DesktopLUT logo generator.

Renders the DesktopLUT mark -- a hazy cloud of mixed red, green and blue light
inside a black-framed glass box (the RGB colour cube) -- from geometry alone
(nothing is traced from the old bitmap): a flat additive RGB mix, wrapped onto
a sphere, blurred into a cloud. Output is supersampled and stored with
straight (non-premultiplied) alpha.

    python tools/logo/render_logo.py            # regenerate assets/logo/ and the repo-root icons
    python tools/logo/render_logo.py --metrics  # also print the balance numbers

Only Pillow and numpy are required. Output is deterministic.
"""
from __future__ import annotations

import argparse
import base64
import functools
import io
import math
import os
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
# Parameters
# =============================================================================
# Geometry -------------------------------------------------------------------
ELEVATION_DEG = 26.5         # view elevation of the cube: the old PNG's view (its upper
                             # outline slopes at 0.446 = sin e). 35.264 would be true
                             # isometric, which shows more of the top.
FRAME_WIDTH = 0.046          # frame stroke width, fraction of the logo height
                             # (outer hexagon and the three front edges alike)

# Cloud ----------------------------------------------------------------------
# Built in three steps:
#  1. A flat RGB mix: three hard-edged circles of light, green up, blue and red
#     at -/+ MIX_ANGLE, added channel by channel. Each pair overlaps in a lens
#     -- cyan, yellow, magenta -- pointing at the box's C, Y and M corners (an
#     upside-down triquetra); all three overlap in a white middle. The
#     secondaries touch each other only through that middle. The cloud is the
#     circles' union, not a disc: three lobes that stop short of the box's
#     corners and seams.
#  2. That mix wrapped onto the front of a sphere inside the box and seen
#     straight on, so the middle bulges toward the viewer and the rim
#     compresses. SPHERE_BULGE blends the wrap with the flat mix (1 = a full
#     equidistant wrap: a point at angle a from the sphere's front pole shows
#     the mix at radius a / 90 deg), so it reads as a roundish cloud, not a ball.
#  3. The result blurred (Gaussian, premultiplied) into haze. The blur does the
#     mixing: every boundary becomes a plain additive blend, and the middle,
#     where all six colours meet, a light grey-white. That haze is the only
#     softness in the mark; the box corners outside it stay empty.
#
# Flat mix, in units of the sphere radius (the wrap covers radius 1). Each
# circle's centre is MIX_OFFSET from the middle.
MIX_ANGLE = 120.0
MIX_OFFSET = {"G": 0.40, "B": 0.40, "R": 0.40}
MIX_RADIUS = {"G": 0.57, "B": 0.57, "R": 0.57}
# Sphere and haze, in units of R (cube centre to top vertex; the faces' inner
# corners sit at 0.93-1.04 R, the middles of the outer edges at about 0.83 R).
SPHERE_RADIUS = 0.94         # radius of the sphere the mix is wrapped on
SPHERE_BULGE = 0.6           # 0 = flat, 1 = full sphere
SPHERE_RAISE = 0.08          # cloud centre above the cube centre, so the white-grey
                             # middle spills a little onto the green (top) face
CLOUD_BLUR = 0.18            # Gaussian sigma of the haze
CLOUD_DENSITY = 2.5          # haze opacity: alpha' = 1 - (1 - alpha)^density
CLOUD_VALUE = 0.5            # 0 = blends are the plain average, 1 = lifted to full value
TOP_THIN = 0.35              # the haze thins by this much at the green face's far corner,
TOP_THIN_RADIUS = 0.45       # fading out over this distance (fraction of R), so the back
                             # edge to that corner shows through

# Back edges (the three hidden edges of the box) -----------------------------
# Crisp black lines behind the cloud: sharp wherever the cloud does not reach,
# veiled only by the cloud's own haze in front of them.
BACK_EDGE_WIDTH = 0.82       # line width, in frame widths

# Rendering ------------------------------------------------------------------
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512, 1024]
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]

# Per-size hinting. At every size frame widths are whole pixels and the vertical
# strokes sit on pixel boundaries; at small sizes the detail that turns to mush
# is dropped.
#   fw:    vertical stroke width in px (default: FRAME_WIDTH * size, rounded)
#   diag:  slanted stroke width relative to fw (optical compensation: a 1 px
#          slanted stroke anti-aliases to grey)
#   back:  back edges on (1) or off (0)
SMALL = {
    16: dict(fw=1, diag=1.50, back=0),
    20: dict(fw=1, diag=1.45, back=0),
    24: dict(fw=1, diag=1.40, back=0),
    32: dict(fw=2, diag=1.15, back=0),
    40: dict(fw=2, diag=1.05),
    48: dict(fw=2, diag=1.00),
    64: dict(fw=3, diag=1.00),
}

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
OUT_DIR = os.path.join(REPO, "assets", "logo")
ICO_DIR = REPO                                   # DesktopLUT.ico / DesktopLUT-active.ico (resource.rc)
LEGACY_DIR = os.path.join(OUT_DIR, "legacy")     # the original artwork, for the preview comparison
ORIGINAL_PNG = os.path.join(LEGACY_DIR, "DesktopLUT-logo.png")
ORIGINAL_INV_PNG = os.path.join(LEGACY_DIR, "DesktopLUT-logo-inv.png")
ORIGINAL_ICO = os.path.join(LEGACY_DIR, "DesktopLUT.ico")

# Cube corner colours (sRGB-encoded, the RGB cube itself)
RED, GREEN, BLUE = np.eye(3)
CYAN, MAGENTA, YELLOW = 1 - RED, 1 - GREEN, 1 - BLUE
COLOURS = {"R": RED, "G": GREEN, "B": BLUE, "C": CYAN, "M": MAGENTA, "Y": YELLOW}
CHANNEL = {"R": 0, "G": 1, "B": 2}
ORDER = ("G", "Y", "R", "M", "B", "C")


# =============================================================================
# Geometry
# =============================================================================
class Line:
    """n . p = c with n the outward unit normal (inside: n . p < c)."""

    def __init__(self, a, b, inside_point):
        a, b = np.asarray(a, float), np.asarray(b, float)
        d = b - a
        n = np.array([d[1], -d[0]]) / np.hypot(*d)
        if n @ np.asarray(inside_point, float) > n @ a:
            n = -n
        self.n, self.c = n, float(n @ a)

    def offset(self, delta):
        """Move the line inwards by delta (px)."""
        out = Line.__new__(Line)
        out.n, out.c = self.n, self.c - delta
        return out

    def dist(self, x, y):
        """Signed distance, negative inside."""
        return x * self.n[0] + y * self.n[1] - self.c


def intersect(l1, l2):
    m = np.array([l1.n, l2.n])
    return np.linalg.solve(m, np.array([l1.c, l2.c]))


class Geometry:
    """Pixel-space geometry of the mark for one canvas size.

    fw = width of the vertical strokes, fd = width of the slanted strokes (equal
    on large sizes; small sizes thicken the diagonals slightly so a 1 px slanted
    stroke does not anti-alias to grey)."""

    def __init__(self, size, fw=None, hinted=True, fd=None, elevation=None):
        n = size
        e = math.radians(ELEVATION_DEG if elevation is None else elevation)
        # Cube edge of screen length L seen at elevation e: half width hw = L/sqrt2,
        # top cap = hw*sin(e), vertical edge = L*cos(e); total height n fixes L.
        hw_ideal = n / (2 * math.sin(e) + math.sqrt(2) * math.cos(e))
        if fw is None:
            fw = FRAME_WIDTH * n
            if hinted:
                # whole pixels; even on large sizes so the mark stays exactly centred
                fw = 2.0 * round(fw / 2) if n >= 96 else max(1.0, round(fw))
        fd = float(fw if fd is None else fd)
        cx = n / 2.0
        hw = hw_ideal
        if hinted:
            # Centre stroke edges on pixel boundaries: for an odd stroke on an even
            # canvas the mark shifts left by half a pixel (it stays mirror-symmetric
            # about its own axis).
            if (round(fw) % 2 == 1) == (n % 2 == 0):
                cx = n / 2.0 - 0.5
            hw = cx - round(cx - hw_ideal)        # outer verticals on pixel edges
        cap = hw * math.sin(e)
        self.size, self.fw, self.fd, self.cx = n, float(fw), fd, cx
        top, bot = 0.0, float(n)
        T = (cx, top)
        UR = (cx + hw, top + cap)
        LR = (cx + hw, bot - cap)
        Bm = (cx, bot)
        LL = (cx - hw, bot - cap)
        UL = (cx - hw, top + cap)
        self.outer = [T, UR, LR, Bm, LL, UL]
        centre = (cx, n / 2.0)
        self.outer_lines = [Line(self.outer[i], self.outer[(i + 1) % 6], centre) for i in range(6)]
        # Cube edges run along the stroke centrelines.
        cl = [l.offset(self.width(self.outer[i], self.outer[(i + 1) % 6]) / 2)
              for i, l in enumerate(self.outer_lines)]
        V = [intersect(cl[i - 1], cl[i]) for i in range(6)]   # V[i] = vertex i of outer
        self.G, self.Y, self.R, self.M, self.B, self.C = V
        self.W = self.C + self.Y - self.G                     # white corner (front)
        self.K = self.C + self.Y + self.M - 2 * self.W        # black corner (hidden)
        self.centre = (self.W + self.K) / 2                   # cube centre
        self.radius = float(np.hypot(*(self.G - self.centre)))  # R: centre to top vertex
        self.faces = {
            "top": self._face(self.C, self.G, self.Y),
            "left": self._face(self.C, self.B, self.M),
            "right": self._face(self.Y, self.R, self.M),
        }

    def width(self, p0, p1):
        """Stroke width of the edge p0-p1 (vertical or slanted)."""
        return self.fw if abs(p1[0] - p0[0]) < 1e-9 else self.fd

    def _face(self, A, P, Bc):
        """Face W-A-P-Bc. The visible face is the rhombus inset by half a stroke
        from each cube edge."""
        W = self.W
        mid = (W + P) / 2
        lWA = Line(W, A, mid).offset(self.width(W, A) / 2)
        lAP = Line(A, P, mid).offset(self.width(A, P) / 2)
        lPB = Line(P, Bc, mid).offset(self.width(P, Bc) / 2)
        lBW = Line(Bc, W, mid).offset(self.width(Bc, W) / 2)
        Wi = intersect(lBW, lWA)
        Ai = intersect(lWA, lAP)
        Pi = intersect(lAP, lPB)
        Bi = intersect(lPB, lBW)
        basis = np.array([Ai - Wi, Bi - Wi]).T
        return dict(Wi=Wi, Ai=Ai, Pi=Pi, Bi=Bi, inv=np.linalg.inv(basis))

    def face_mask(self, f, x, y):
        px, py = x - f["Wi"][0], y - f["Wi"][1]
        a = f["inv"][0, 0] * px + f["inv"][0, 1] * py
        b = f["inv"][1, 0] * px + f["inv"][1, 1] * py
        return (a >= 0) & (a <= 1) & (b >= 0) & (b <= 1)


# =============================================================================
# Colour science
# =============================================================================
def srgb_to_linear(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, np.power((c + 0.055) / 1.055, 2.4))


def linear_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


_OKLAB_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                      [0.2119034982, 0.6806995451, 0.1073969566],
                      [0.0883024619, 0.2817188376, 0.6299787005]])
_OKLAB_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                      [1.9779984951, -2.4285922050, 0.4505937099],
                      [0.0259040371, 0.7827717662, -0.8086757660]])


def oklab(rgb):
    """sRGB-encoded (..., 3) -> OKLab (..., 3)."""
    lms = np.cbrt(srgb_to_linear(rgb) @ _OKLAB_M1.T)
    return lms @ _OKLAB_M2.T


def oklab_L(rgb):
    return oklab(rgb)[..., 0]


_SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                         [0.2126729, 0.7151522, 0.0721750],
                         [0.0193339, 0.1191920, 0.9503041]])


def lightness_hk(rgb):
    """CIELAB L** (Fairchild & Pirrotta 1991): L* plus the Helmholtz-Kohlrausch
    term, the lightness a saturated colour appears to have. D65, sRGB."""
    xyz = srgb_to_linear(rgb) @ _SRGB_TO_XYZ.T / (_SRGB_TO_XYZ @ np.ones(3))
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    L = 116 * f[..., 1] - 16
    a, b = 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])
    C, h = np.hypot(a, b), np.degrees(np.arctan2(b, a))
    return L + (2.5 - 0.025 * L) * (0.116 * np.abs(np.sin(np.radians((h - 90) / 2))) + 0.085) * C


def nearest_colour(rgb):
    """Index into ORDER of the named colour each pixel's OKLab hue is closest to."""
    lab = oklab(rgb)
    h = np.degrees(np.arctan2(lab[..., 2], lab[..., 1]))
    ref = [math.degrees(math.atan2(*oklab(COLOURS[c])[[2, 1]])) for c in ORDER]
    d = np.stack([np.abs((h - r + 180.0) % 360.0 - 180.0) for r in ref], axis=-1)
    return d.argmin(axis=-1)


# =============================================================================
# Cloud
# =============================================================================
def flat_mix(u, v):
    """Step 1: the flat additive RGB mix at coordinates (u, v), v up, in units of
    the sphere radius. Returns (rgb, inside the cloud = the circles' union)."""
    rgb = np.zeros(u.shape + (3,))
    for c, ch in CHANNEL.items():
        ang = math.radians({"G": 0.0, "B": -MIX_ANGLE, "R": MIX_ANGLE}[c])
        cu, cv = MIX_OFFSET[c] * math.sin(ang), MIX_OFFSET[c] * math.cos(ang)
        rgb[..., ch] = (np.hypot(u - cu, v - cv) <= MIX_RADIUS[c]).astype(float)
    return rgb, rgb.max(axis=-1) > 0


@functools.lru_cache(maxsize=None)
def _wrap_table(bulge):
    """Screen radius rho for mix radius t (both in sphere radii): a blend of the
    flat mix (rho = t) and the equidistant wrap (rho = sin(t * 90 deg))."""
    t = np.linspace(0.0, 1.0, 4097)
    rho = (1.0 - bulge) * t + bulge * np.sin(t * math.pi / 2)
    return rho, t


def sphere_layer(geo, ss):
    """Step 2: the flat mix wrapped onto the sphere, premultiplied RGBA at the
    canvas resolution (box-filtered from ss x ss samples per pixel)."""
    n = geo.size
    out = np.zeros((n, n, 4))
    off = (np.arange(ss) + 0.5) / ss
    cx, cy = geo.centre[0], geo.centre[1] - SPHERE_RAISE * geo.radius
    rs = SPHERE_RADIUS * geo.radius
    band = max(1, int(2 ** 21 // (n * ss * ss)))
    xs = (np.arange(n)[:, None] + off[None, :]).reshape(-1)
    for y0 in range(0, n, band):
        y1 = min(n, y0 + band)
        ys = (np.arange(y0, y1)[:, None] + off[None, :]).reshape(-1)
        X, Y = np.meshgrid(xs, ys)
        dx, dy = (X - cx) / rs, (Y - cy) / rs
        rho = np.hypot(dx, dy)
        on = rho < 1.0
        rt, tt = _wrap_table(SPHERE_BULGE)
        t = np.where(on, np.interp(rho, rt, tt), 0.0)
        k = np.where(rho > 0, t / np.maximum(rho, 1e-12), 0.0)
        rgb, inside = flat_mix(dx * k, -dy * k)
        a = (on & inside).astype(float)
        pm = np.concatenate([rgb * a[..., None], a[..., None]], axis=-1)
        out[y0:y1] = pm.reshape(y1 - y0, ss, n, ss, 4).mean(axis=(1, 3))
    return out


def gaussian_blur(img, sigma):
    """Gaussian blur of an (h, w, c) array via FFT, zero-padded."""
    if sigma <= 0:
        return img
    pad = int(math.ceil(4 * sigma))
    h, w = img.shape[:2]
    H, W = h + 2 * pad, w + 2 * pad
    big = np.zeros((H, W, img.shape[2]))
    big[pad:pad + h, pad:pad + w] = img
    fy = np.fft.fftfreq(H)[:, None]
    fx = np.fft.rfftfreq(W)[None, :]
    g = np.exp(-2.0 * (math.pi * sigma) ** 2 * (fy ** 2 + fx ** 2))
    out = np.fft.irfft2(np.fft.rfft2(big, axes=(0, 1)) * g[..., None], s=(H, W), axes=(0, 1))
    return out[pad:pad + h, pad:pad + w]


def cloud(geo, ss):
    """Steps 2 and 3: the hazy cloud as (straight colour, alpha) per pixel.

    The blur averages the colours where regions meet, so the middle, where all
    six meet, mixes to a light grey-white. CLOUD_VALUE lifts blends partway back
    toward full value (0 = the plain average, 1 = strongest channel at 1) so
    they do not turn muddy; CLOUD_DENSITY makes the haze less transparent."""
    pm = gaussian_blur(sphere_layer(geo, ss), CLOUD_BLUR * geo.radius)
    a = np.clip(pm[..., 3], 0.0, 1.0)
    rgb = np.clip(pm[..., :3], 0.0, None) / np.maximum(a, 1e-12)[..., None]
    rgb = np.clip(rgb, 0.0, 1.0)
    m = rgb.max(axis=-1)
    colour = np.where((m > 1e-9)[..., None], rgb / np.maximum(m, 1e-12)[..., None], 0.0)
    value = m + CLOUD_VALUE * (1.0 - m)
    colour = colour * value[..., None]
    a = 1.0 - np.power(1.0 - a, CLOUD_DENSITY)
    if TOP_THIN > 0:
        yy, xx = np.mgrid[0:geo.size, 0:geo.size] + 0.5
        u = np.clip(1.0 - np.hypot(xx - geo.G[0], yy - geo.G[1]) / (TOP_THIN_RADIUS * geo.radius), 0.0, 1.0)
        a = a * (1.0 - TOP_THIN * u * u * (3.0 - 2.0 * u))
    a = np.where(a < 1e-6, 0.0, a)
    return colour, a


def seg_dist(x, y, p0, p1):
    d = p1 - p0
    L2 = float(d @ d)
    if L2 == 0:
        return np.hypot(x - p0[0], y - p0[1])
    t = np.clip(((x - p0[0]) * d[0] + (y - p0[1]) * d[1]) / L2, 0, 1)
    return np.hypot(x - (p0[0] + t * d[0]), y - (p0[1] + t * d[1]))


# =============================================================================
# Rasteriser
# =============================================================================
def render(size, inverted=False, hinted=True, ss=None, return_coverage=False, elevation=None,
           frame=True):
    """Render the mark at size x size. Returns a straight-alpha RGBA uint8 array
    (and optionally per-face coverage maps). frame=False leaves out the frame
    (the cloud layer of the SVG)."""
    spec = SMALL.get(size, {}) if hinted else {}
    fw = spec.get("fw")
    fd = None if fw is None else fw * spec.get("diag", 1.0)
    geo = Geometry(size, fw=fw, fd=fd, hinted=hinted, elevation=elevation)
    back_on = spec.get("back", 1) > 0
    if ss is None:
        ss = 16 if size <= 64 else 8 if size <= 256 else 4
    frame_rgb = np.array([1.0, 1.0, 1.0]) if inverted else np.zeros(3)
    cl_rgb, cl_a = cloud(geo, min(ss, 4))

    out = np.zeros((size, size, 4))
    cov = {k: np.zeros((size, size)) for k in geo.faces} if return_coverage else None
    off = (np.arange(ss) + 0.5) / ss
    band = max(1, int(2 ** 22 // (size * ss * ss)))      # output rows per band
    back_lines = [(geo.K, geo.G), (geo.K, geo.R), (geo.K, geo.B)]
    back_hw = max(0.5, BACK_EDGE_WIDTH * geo.fw) / 2

    for y0 in range(0, size, band):
        y1 = min(size, y0 + band)
        ys = (np.arange(y0, y1)[:, None] + off[None, :]).reshape(-1)
        xs = (np.arange(size)[:, None] + off[None, :]).reshape(-1)
        X, Y = np.meshgrid(xs, ys)
        PX = np.minimum(X.astype(int), size - 1)
        PY = np.minimum(Y.astype(int), size - 1)
        rgb = np.zeros(X.shape + (3,))
        alpha = np.zeros(X.shape)
        if frame:
            inside = np.ones_like(X, bool)
            for l in geo.outer_lines:
                inside &= l.dist(X, Y) < 0
            rgb[inside] = frame_rgb
            alpha = inside.astype(float)

        for name, f in geo.faces.items():
            m = geo.face_mask(f, X, Y)
            if not m.any():
                continue
            xm, ym = X[m], Y[m]
            col, ca = cl_rgb[PY[m], PX[m]], cl_a[PY[m], PX[m]]
            # back to front: the box's back edges (crisp black), then the cloud
            line = np.zeros_like(xm)
            if back_on:
                dist = np.min([seg_dist(xm, ym, p0, p1) for p0, p1 in back_lines], axis=0)
                line = (dist < back_hw).astype(float)
            al = ca + (1.0 - ca) * line
            c = col * (ca / np.maximum(al, 1e-12))[:, None]
            if inverted:
                c = 1.0 - c
            rgb[m] = c
            alpha[m] = al
            if cov is not None:
                cm = m.astype(float).reshape(y1 - y0, ss, size, ss).mean(axis=(1, 3))
                cov[name][y0:y1] = cm

        # box-filter premultiplied colour, then un-premultiply
        pm = np.concatenate([rgb * alpha[..., None], alpha[..., None]], axis=-1)
        pm = pm.reshape(y1 - y0, ss, size, ss, 4).mean(axis=(1, 3))
        out[y0:y1] = pm

    a = out[..., 3:4]
    with np.errstate(divide="ignore", invalid="ignore"):
        straight = np.where(a > 0, out[..., :3] / np.maximum(a, 1e-12), 0.0)
    img = np.concatenate([straight, a], axis=-1)
    img = np.clip(np.round(img * 255.0), 0, 255).astype(np.uint8)
    img[img[..., 3] == 0, :3] = 0
    return (img, cov, geo) if return_coverage else img


# =============================================================================
# Metrics
# =============================================================================
def lightness_gray(img):
    """Grey with the same OKLab L as each pixel (alpha kept)."""
    L = oklab_L(img[..., :3] / 255.0)
    v = linear_to_srgb(np.power(np.clip(L, 0, 1), 3))
    g = np.clip(np.round(v * 255), 0, 255).astype(np.uint8)
    return np.dstack([g, g, g, img[..., 3]])


def composite(img, ground):
    a = img[..., 3:4] / 255.0
    return img[..., :3] / 255.0 * a + np.asarray(ground, float) / 255.0 * (1 - a)


def face_stats(img, masks, ground=None):
    rgb = img[..., :3] / 255.0 if ground is None else composite(img, ground)
    L = oklab_L(rgb)
    return {k: float(L[m].mean()) for k, m in masks.items()}


def new_face_masks(cov):
    return {k: v >= 0.999 for k, v in cov.items()}


def original_face_masks(img):
    """Face masks for the old 256 px PNG (measured: front vertex at (127.5, 105.8),
    upper front edges at slope 0.444, frame ~11.5 px)."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w] + 0.5
    wx, wy, s = 127.5, 105.8, 0.444
    al = img[..., 3]
    lum = img[..., :3].astype(int).sum(-1)
    face = (al > 0) & ~((al > 200) & (lum < 90))
    # stay clear of frame anti-aliasing
    from PIL import ImageFilter
    fm = Image.fromarray((face * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(5))
    face = np.array(fm) > 0
    ul = wy - (wx - xx) * s          # upper-left front edge line y at x
    ur = wy - (xx - wx) * s
    top = face & (yy < np.where(xx < wx, ul, ur))
    left = face & ~top & (xx < wx)
    right = face & ~top & (xx > wx)
    return {"top": top, "left": left, "right": right}


def colour_areas(img, masks):
    """Share of each named colour on the faces: face pixels by the named colour
    their OKLab hue is nearest, weighted by opacity x saturation (1 - min/max)."""
    face = np.zeros(img.shape[:2], bool)
    for v in masks.values():
        face |= v
    rgb = img[..., :3] / 255.0
    near = nearest_colour(rgb)
    sat = 1.0 - rgb.min(-1) / np.maximum(rgb.max(-1), 1e-6)
    w = img[..., 3] / 255.0 * sat * face
    return {c: float(w[near == i].sum() / w.sum()) for i, c in enumerate(ORDER)}


def colour_weights():
    return {c: float(lightness_hk(v)) for c, v in COLOURS.items()}


# =============================================================================
# ICO writer (BMP entries below 256 px, PNG-compressed 256 px entry)
# =============================================================================
def write_ico(path, images):
    images = sorted(images, key=lambda im: im.shape[0])
    blobs = []
    for im in images:
        n = im.shape[0]
        if n >= 256:
            buf = io.BytesIO()
            Image.fromarray(im, "RGBA").save(buf, "PNG", optimize=True)
            blobs.append(buf.getvalue())
            continue
        hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        bgra = im[::-1, :, [2, 1, 0, 3]].tobytes()
        row_bytes = ((n + 31) // 32) * 4
        mask = bytearray()
        for row in im[::-1]:
            line = bytearray(row_bytes)
            for x in range(n):
                if row[x, 3] == 0:
                    line[x // 8] |= 0x80 >> (x % 8)
            mask += line
        blobs.append(hdr + bgra + bytes(mask))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for im, blob in zip(images, blobs):
        n = im.shape[0]
        d = 0 if n >= 256 else n
        entries += struct.pack("<BBBBHHII", d, d, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as fh:
        fh.write(header + entries + b"".join(blobs))


# =============================================================================
# SVG: vector frame over the cloud layer
# =============================================================================
def write_svg(path, size=256, cloud_px=512):
    """The frame is vector; the clouds (soft by nature, and mixed in a way SVG
    compositing cannot express) are an embedded PNG at cloud_px."""
    geo = Geometry(size, hinted=False)
    clouds = render(cloud_px, hinted=False, frame=False)
    buf = io.BytesIO()
    Image.fromarray(clouds, "RGBA").save(buf, "PNG", optimize=True)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    f6 = lambda v: f"{v:.3f}".rstrip("0").rstrip(".")
    pt = lambda p: f"{f6(p[0])},{f6(p[1])}"
    frame = "M" + " L".join(pt(p) for p in geo.outer) + " Z"
    for f in geo.faces.values():
        frame += " M" + " L".join(pt(p) for p in (f["Wi"], f["Ai"], f["Pi"], f["Bi"])) + " Z"
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {size} {size}" width="{size}" height="{size}">',
        "<title>DesktopLUT</title>",
        f'<image width="{size}" height="{size}" href="data:image/png;base64,{data}" '
        f'xlink:href="data:image/png;base64,{data}"/>',
        f'<path d="{frame}" fill="#000" fill-rule="evenodd"/>',
        "</svg>",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(body) + "\n")


# =============================================================================
# Preview sheet
# =============================================================================
GROUNDS = [("#0b0b0c", (11, 11, 12)), ("#808080", (128, 128, 128)), ("#f2f2f2", (242, 242, 242))]


def _font(px):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _paste(sheet, img, xy, ground, zoom=1):
    im = Image.fromarray(img, "RGBA")
    if zoom != 1:
        im = im.resize((im.width * zoom, im.height * zoom), Image.NEAREST)
    bg = Image.new("RGBA", im.size, ground + (255,))
    bg.alpha_composite(im)
    sheet.paste(bg.convert("RGB"), xy)


def make_preview(path, master, master_inv, small, small_inv, stats_lines):
    orig = orig_inv = None
    if os.path.exists(ORIGINAL_PNG):
        orig = np.array(Image.open(ORIGINAL_PNG).convert("RGBA"))
    if os.path.exists(ORIGINAL_INV_PNG):
        orig_inv = np.array(Image.open(ORIGINAL_INV_PNG).convert("RGBA"))
    orig_small = {}
    if os.path.exists(ORIGINAL_ICO):
        ico = Image.open(ORIGINAL_ICO)
        for s in (16, 32, 48, 64):
            if (s, s) in ico.info.get("sizes", set()):
                ico.size = (s, s)
                orig_small[s] = np.array(ico.copy().convert("RGBA"))

    big = [("old", orig), ("new", master),
           ("old, OKLab L", None if orig is None else lightness_gray(orig)),
           ("new, OKLab L", lightness_gray(master)),
           ("old inverted", orig_inv), ("new inverted", master_inv)]
    big = [(t, im) for t, im in big if im is not None]
    gap, pad, lab = 16, 20, 20
    small_sizes = [16, 20, 24, 32, 40, 48, 64]
    W = max(pad * 2 + len(big) * 256 + (len(big) - 1) * gap, 1560)
    band_h = lab + lab + 256 + gap + lab + 128 + pad
    head_h = 34 + 19 * len(stats_lines) + 12
    H = head_h + len(GROUNDS) * band_h
    sheet = Image.new("RGB", (W, H), (30, 30, 32))
    dr = ImageDraw.Draw(sheet)
    fT, fS, fM = _font(19), _font(13), _font(14)
    dr.text((pad, 8), "DesktopLUT mark: old (assets/logo/legacy) vs new (assets/logo, repo-root icons)",
            fill=(235, 235, 235), font=fT)
    for i, line in enumerate(stats_lines):
        dr.text((pad, 36 + 19 * i), line, fill=(205, 205, 205), font=fM)

    def label(x, y, w, text, ink):
        tw = dr.textlength(text, font=fS)
        dr.text((x + (w - tw) / 2, y), text, fill=ink, font=fS)

    for gi, (gname, g) in enumerate(GROUNDS):
        y0 = head_h + gi * band_h
        sheet.paste(g, (0, y0, W, y0 + band_h))
        ink = (225, 225, 225) if sum(g) < 400 else (30, 30, 30)
        dr.text((pad, y0 + 4), f"ground {gname}", fill=ink, font=_font(14))
        x, yt = pad, y0 + lab + 2
        for title, im in big:
            label(x, yt, 256, title, ink)
            _paste(sheet, im, (x, yt + lab), g)
            x += 256 + gap
        ys = yt + lab + 256 + gap
        groups = [("new 16-64 @1x", [(small[s], 1) for s in small_sizes]),
                  ("new inverted @1x", [(small_inv[s], 1) for s in small_sizes])]
        if orig_small:
            groups.append(("old .ico @1x", [(im, 1) for im in orig_small.values()]))
        groups.append(("new 16 / 24 / 32 @4x", [(small[s], 4) for s in (16, 24, 32)]))
        groups.append(("new inverted 16 / 32 @4x", [(small_inv[s], 4) for s in (16, 32)]))
        if orig_small:
            groups.append(("old 16 / 32 @4x", [(orig_small[s], 4) for s in (16, 32) if s in orig_small]))
        x = pad
        for title, items in groups:
            gw = sum(im.shape[0] * z for im, z in items) + 6 * (len(items) - 1)
            label(x, ys, max(gw, 1), title, ink)
            xx = x
            for im, z in items:
                sz = im.shape[0] * z
                _paste(sheet, im, (xx, ys + lab + (128 - sz if z == 1 else 0)), g, zoom=z)
                xx += sz + 6
            x += max(gw, dr.textlength(title, font=fS)) + 26
    sheet.save(path, optimize=True)


# =============================================================================
# Main
# =============================================================================
def fmt_stats(label, st):
    vals = [st["top"], st["left"], st["right"]]
    return (f"{label}: top {st['top']:.3f}   left {st['left']:.3f}   right {st['right']:.3f}"
            f"   spread {max(vals) - min(vals):.3f}")


def fmt_colours(label, d, fmt="{:.3f}"):
    return f"{label}: " + "   ".join(f"{c} {fmt.format(d[c])}" for c in ORDER)


def compute_stats(master, cov):
    w = colour_weights()
    masks = new_face_masks(cov)
    area = colour_areas(master, masks)
    bal = {c: area[c] * w[c] for c in COLOURS}
    mb = sum(bal.values()) / 6
    lines = [
        fmt_colours("Area share, new (nearest OKLab hue, opacity x saturation)", area),
        fmt_colours("L** (lightness with Helmholtz-Kohlrausch, vs black)      ", w, "{:.1f}"),
        fmt_colours("Area x L**, new, relative to the mean                    ", {c: bal[c] / mb for c in COLOURS}, "{:.2f}"),
    ]
    if os.path.exists(ORIGINAL_PNG):
        orig = np.array(Image.open(ORIGINAL_PNG).convert("RGBA"))
        om = original_face_masks(orig)
        oa = colour_areas(orig, om)
        ob = {c: oa[c] * w[c] for c in COLOURS}
        omb = sum(ob.values()) / 6
        lines[1:1] = [fmt_colours("Area share, old                                          ", oa)]
        lines.append(fmt_colours("Area x L**, old, relative to the mean                    ", {c: ob[c] / omb for c in COLOURS}, "{:.2f}"))
        lines.append(fmt_stats("Face mean OKLab L, old  (over #0b0b0c)   ", face_stats(orig, om, GROUNDS[0][1])))
    lines.append(fmt_stats("Face mean OKLab L, new  (over #0b0b0c)   ", face_stats(master, masks, GROUNDS[0][1])))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics", action="store_true", help="print the balance numbers")
    ap.add_argument("--out", default=OUT_DIR, help="output directory (default assets/logo)")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    imgs, imgs_inv = {}, {}
    for s in SIZES:
        imgs[s] = render(s)
        imgs_inv[s] = render(s, inverted=True)
        Image.fromarray(imgs[s], "RGBA").save(os.path.join(args.out, f"logo_{s}.png"), optimize=True)
        Image.fromarray(imgs_inv[s], "RGBA").save(os.path.join(args.out, f"logo_inv_{s}.png"), optimize=True)
    # the app icons live at the repo root (resource.rc); a custom --out gets its own copies
    ico_dir = ICO_DIR if os.path.abspath(args.out) == os.path.abspath(OUT_DIR) else args.out
    write_ico(os.path.join(ico_dir, "DesktopLUT.ico"), [imgs[s] for s in ICO_SIZES])
    write_ico(os.path.join(ico_dir, "DesktopLUT-active.ico"), [imgs_inv[s] for s in ICO_SIZES])
    write_svg(os.path.join(args.out, "logo.svg"))

    _, cov, _ = render(256, return_coverage=True)
    stats_lines = compute_stats(imgs[256], cov)
    make_preview(os.path.join(args.out, "preview.png"), imgs[256], imgs_inv[256], imgs, imgs_inv, stats_lines)
    if args.metrics:
        print("\n".join(stats_lines))
    print(f"wrote {len(SIZES) * 2} PNGs, 2 ICOs, logo.svg, preview.png -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
