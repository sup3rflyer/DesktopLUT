#!/usr/bin/env python
"""DesktopLUT logo generator.

Renders the DesktopLUT mark -- the RGB colour cube as a glass cube seen from its
white corner, framed in black -- analytically: every pixel's colour is computed
from the cube geometry (nothing is traced from the old bitmap). Output is
supersampled and stored with straight (non-premultiplied) alpha.

    python tools/logo/render_logo.py            # regenerate everything in assets/logo/
    python tools/logo/render_logo.py --metrics  # also print per-face OKLab lightness

Only Pillow and numpy are required. Output is deterministic.
"""
from __future__ import annotations

import argparse
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
ELEVATION_DEG = 35.2643897   # view elevation of the cube. 35.264 deg = true isometric:
                             # the neutral (black-white) axis points at the viewer, the
                             # outline is a regular hexagon and black hides exactly
                             # behind white. (The old PNG was ~26 deg.)
ALT_ELEVATION_DEG = 26.4     # shown as an alternative in preview.png only (the old PNG's view)
FRAME_WIDTH = 0.046          # frame stroke width, fraction of the logo height
                             # (outer hexagon and the three front edges alike)

# Glass ----------------------------------------------------------------------
GLASS_ALPHA = 0.82           # face opacity at the outer corners (1.0 at the front vertex)
GLASS_ALPHA_GAMMA = 1.7      # how quickly opacity falls from the front vertex outwards
EDGE_CLEAR = 0.10            # extra transparency in a band hugging the outer frame
EDGE_CLEAR_WIDTH = 1.4       # width of that band, in frame widths
RIM_STRENGTH = 0.30          # thin light line where the faces meet the three front edges
RIM_WIDTH = 0.18             # rim width, in frame widths

# Highlight ------------------------------------------------------------------
HIGHLIGHT_STRENGTH = 1.0     # white at the front vertex (0..1)
HIGHLIGHT_RADIUS = 0.34      # radius where the highlight is 50 %, fraction of the edge length
HIGHLIGHT_SOFTNESS = 1.7     # falloff exponent (2 = Gaussian; lower = longer, softer tail)
INVERTED_HIGHLIGHT_SCALE = 0.75  # the inverted variant is a literal colour inversion, so the
                                 # white highlight becomes a dark core; drawing it at 75 %
                                 # radius keeps that core an accent (1.0 = strictly literal)

# Back edges (the three hidden edges seen through the glass) -----------------
BACK_EDGE_STRENGTH = 0.22    # darkening / opacity added along the hidden edges
BACK_EDGE_WIDTH = 0.60       # line width, in frame widths (soft profile, seen through glass)

# Colour balance -------------------------------------------------------------
# Each face runs from its two secondaries (along the front edges) to its primary
# (the far corner); the white comes only from the shared highlight above, so the
# highlight is identical on every face. Knobs shape how the hue is distributed;
# corners always stay the exact primaries/secondaries:
#   radial:  how quickly the primary takes over going outwards (1 = plain cube)
#   upper / lower: per secondary seam, >1 widens the secondary's wedge, <1
#            widens the primary's. upper = the seam on the face's first front
#            edge (cyan on top and left, yellow on right), lower = the second
#            (yellow on top, magenta on both side faces)
# OKLab lightness: G .866 < C .905 < Y .968 on top and B .452 is by far the
# darkest, so the top face favours green (its darkest colour), the blue face
# gives its cyan seam the most room, the red face keeps its yellow seam narrow.
# Magenta is shaped identically on both side faces, so the bottom vertex stays
# mirror-symmetric. See README for the measured numbers.
FACE_SHAPING = {
    "top": dict(radial=3.0, upper=0.5, lower=0.5),     # cyan seam / yellow seam
    "left": dict(radial=1.3, upper=2.4, lower=1.1),    # cyan seam / magenta seam
    "right": dict(radial=1.3, upper=0.8, lower=1.1),   # yellow seam / magenta seam
}

# Rendering ------------------------------------------------------------------
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512, 1024]
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 256]

# Per-size hinting. At every size frame widths are whole pixels and the vertical
# strokes sit on pixel boundaries; at small sizes the detail that turns to mush
# is dropped and the faces simplify toward their primaries.
#   fw:    vertical stroke width in px (default: FRAME_WIDTH * size, rounded)
#   diag:  slanted stroke width relative to fw (optical compensation: a 1 px
#          slanted stroke anti-aliases to grey)
#   back / rim / clear: multipliers for back edges, front rim, edge clearing
#   hl:    highlight radius multiplier
#   prim:  multiplier on the seam knobs (<1 = the primary owns more of each face)
#   glass: face opacity at the outer corners (overrides GLASS_ALPHA)
SMALL = {
    16: dict(fw=1, diag=1.50, back=0.0, rim=0.0, clear=0.0, hl=0.80, prim=0.50, glass=0.92),
    20: dict(fw=1, diag=1.45, back=0.0, rim=0.0, clear=0.0, hl=0.82, prim=0.55, glass=0.92),
    24: dict(fw=1, diag=1.40, back=0.0, rim=0.0, clear=0.0, hl=0.85, prim=0.60, glass=0.90),
    32: dict(fw=2, diag=1.15, back=0.0, rim=0.0, clear=0.3, hl=0.90, prim=0.75, glass=0.88),
    40: dict(fw=2, diag=1.05, back=0.4, rim=0.4, clear=0.5, hl=0.95, prim=0.85, glass=0.85),
    48: dict(fw=2, diag=1.00, back=0.5, rim=0.5, clear=0.6, hl=1.00, prim=0.90),
    64: dict(fw=3, diag=1.00, back=0.7, rim=0.7, clear=0.8, hl=1.00, prim=0.95),
}

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
OUT_DIR = os.path.join(REPO, "assets", "logo")
ORIGINAL_PNG = os.path.join(REPO, "DesktopLUT-logo.png")
ORIGINAL_INV_PNG = os.path.join(REPO, "DesktopLUT-logo-inv.png")
ORIGINAL_ICO = os.path.join(REPO, "DesktopLUT.ico")

# Cube corner colours (sRGB-encoded, the RGB cube itself)
WHITE = np.array([1.0, 1.0, 1.0])
RED, GREEN, BLUE = np.eye(3)
CYAN, MAGENTA, YELLOW = 1 - RED, 1 - GREEN, 1 - BLUE


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
        self.unit = float(np.mean([np.hypot(*(p - self.W)) for p in (self.C, self.Y, self.M)]))
        self.faces = {
            "top": self._face(self.C, self.G, self.Y, CYAN, GREEN, YELLOW),
            "left": self._face(self.C, self.B, self.M, CYAN, BLUE, MAGENTA),
            "right": self._face(self.Y, self.R, self.M, YELLOW, RED, MAGENTA),
        }

    def width(self, p0, p1):
        """Stroke width of the edge p0-p1 (vertical or slanted)."""
        return self.fw if abs(p1[0] - p0[0]) < 1e-9 else self.fd

    def _face(self, A, P, Bc, colA, colP, colB):
        """Face W-A-P-Bc (A, Bc secondaries along the front edges, P the primary).

        The visible face is the rhombus inset by half a stroke from each cube edge.
        """
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
        return dict(Wi=Wi, Ai=Ai, Pi=Pi, Bi=Bi, inv=np.linalg.inv(basis),
                    front=(lWA, lBW), outer=(lAP, lPB), P=P,
                    colA=colA, colP=colP, colB=colB)


# =============================================================================
# Colour model
# =============================================================================
def radial_shape(x, k):
    """0->0, 1->1; k>1 rises faster (the primary takes over sooner)."""
    return 1.0 - np.power(1.0 - np.clip(x, 0.0, 1.0), k)


def angular_shape(r, q):
    """Weight of the primary for a ratio r in [0,1] (0 = secondary edge, 1 =
    diagonal). Flat at r=1, so the two halves of a face meet smoothly on the
    diagonal (no ridge at the primary)."""
    return 1.0 - np.square(1.0 - np.power(np.clip(r, 0.0, 1.0), q))


def primary_weight(a, b, shaping):
    """Weight of the face's primary at face coordinates (a, b), and which half
    (upper = toward secondary A) the point lies in."""
    fa = radial_shape(a, shaping["radial"])
    fb = radial_shape(b, shaping["radial"])
    upper = fa >= fb
    r = np.where(upper, fb / np.maximum(fa, 1e-12), fa / np.maximum(fb, 1e-12))
    t = np.where(upper, angular_shape(r, shaping["upper"]), angular_shape(r, shaping["lower"]))
    return t, upper


def face_hue(a, b, face, shaping):
    """Fully saturated face colour at face coordinates (a, b): the secondary along
    each front edge, the primary on the diagonal and in the far corner. This is
    the RGB cube's own hue layout (a fan about the white corner) with the
    distribution reshaped; every colour lies on the face's cube plane."""
    t, upper = primary_weight(a, b, shaping)
    sec = np.where(upper[..., None], face["colA"], face["colB"])
    return sec + (face["colP"] - sec) * t[..., None]


def edge_hue(s, face, shaping):
    """Colour along the back edges (A -> P -> Bc as s goes 0 -> 0.5 -> 1)."""
    if s <= 0.5:
        t = angular_shape(radial_shape(2 * s, shaping["radial"]), shaping["upper"])
        return face["colA"] + (face["colP"] - face["colA"]) * t
    t = angular_shape(radial_shape(2 * (1 - s), shaping["radial"]), shaping["lower"])
    return face["colB"] + (face["colP"] - face["colB"]) * t


def highlight(d_unit, radius_scale=1.0):
    r = HIGHLIGHT_RADIUS * radius_scale
    return HIGHLIGHT_STRENGTH * np.exp2(-np.power(d_unit / r, HIGHLIGHT_SOFTNESS))


# =============================================================================
# Rasteriser
# =============================================================================
def render(size, inverted=False, hinted=True, ss=None, return_coverage=False, elevation=None):
    """Render the mark at size x size. Returns straight-alpha RGBA uint8 array
    (and optionally per-face coverage maps)."""
    spec = SMALL.get(size, {}) if hinted else {}
    fw = spec.get("fw")
    fd = None if fw is None else fw * spec.get("diag", 1.0)
    geo = Geometry(size, fw=fw, fd=fd, hinted=hinted, elevation=elevation)
    back_k = BACK_EDGE_STRENGTH * spec.get("back", 1.0)
    rim_k = RIM_STRENGTH * spec.get("rim", 1.0)
    clear_k = EDGE_CLEAR * spec.get("clear", 1.0)
    hl_scale = spec.get("hl", 1.0) * (INVERTED_HIGHLIGHT_SCALE if inverted else 1.0)
    glass_min = spec.get("glass", GLASS_ALPHA)
    prim = spec.get("prim", 1.0)
    shaping = {k: dict(v, upper=v["upper"] * prim, lower=v["lower"] * prim) for k, v in FACE_SHAPING.items()}
    if ss is None:
        ss = 16 if size <= 64 else 8 if size <= 256 else 4
    frame_rgb = np.array([1.0, 1.0, 1.0]) if inverted else np.zeros(3)

    out = np.zeros((size, size, 4))
    cov = {k: np.zeros((size, size)) for k in geo.faces} if return_coverage else None
    off = (np.arange(ss) + 0.5) / ss
    band = max(1, int(2 ** 22 // (size * ss * ss)))      # output rows per band
    back_lines = [(geo.K, geo.G), (geo.K, geo.R), (geo.K, geo.B)]
    back_sigma = max(0.5, BACK_EDGE_WIDTH * geo.fw * 0.5)
    rim_w = max(0.35, RIM_WIDTH * geo.fw)
    clear_w = EDGE_CLEAR_WIDTH * geo.fw

    for y0 in range(0, size, band):
        y1 = min(size, y0 + band)
        ys = (np.arange(y0, y1)[:, None] + off[None, :]).reshape(-1)
        xs = (np.arange(size)[:, None] + off[None, :]).reshape(-1)
        X, Y = np.meshgrid(xs, ys)
        inside = np.ones_like(X, bool)
        for l in geo.outer_lines:
            inside &= l.dist(X, Y) < 0
        rgb = np.zeros(X.shape + (3,))
        alpha = inside.astype(float)
        rgb[inside] = frame_rgb
        dW = np.hypot(X - geo.W[0], Y - geo.W[1]) / geo.unit
        H = highlight(dW, hl_scale)
        glass = 1.0 - (1.0 - glass_min) * np.power(np.clip(dW, 0, 1), GLASS_ALPHA_GAMMA)

        for name, f in geo.faces.items():
            px, py = X - f["Wi"][0], Y - f["Wi"][1]
            a = f["inv"][0, 0] * px + f["inv"][0, 1] * py
            b = f["inv"][1, 0] * px + f["inv"][1, 1] * py
            m = (a >= 0) & (a <= 1) & (b >= 0) & (b <= 1)
            if not m.any():
                continue
            am, bm, xm, ym = a[m], b[m], X[m], Y[m]
            c = face_hue(am, bm, f, shaping[name])
            al = glass[m].copy()
            # glass: a touch clearer right against the outer frame
            if clear_k > 0:
                eo = np.minimum(-f["outer"][0].dist(xm, ym), -f["outer"][1].dist(xm, ym))
                al *= 1.0 - clear_k * np.exp(-np.square(eo / clear_w))
            # hidden back edges, seen through the glass (darker, more opaque)
            if back_k > 0:
                ell = np.zeros_like(xm)
                for p0, p1 in back_lines:
                    ell = np.maximum(ell, np.exp(-0.5 * np.square(seg_dist(xm, ym, p0, p1) / back_sigma)))
                ell *= back_k
                c = c * (1.0 - ell[:, None])
                al = al + (1.0 - al) * ell
            # white highlight at the front vertex (front surface, covers the back edges)
            h = H[m][:, None]
            c = c + (1.0 - c) * h
            # light rim along the front edges
            if rim_k > 0:
                ef = np.minimum(-f["front"][0].dist(xm, ym), -f["front"][1].dist(xm, ym))
                rim = rim_k * np.exp(-np.square(ef / rim_w))
                c = c + (1.0 - c) * rim[:, None]
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


def seg_dist(x, y, p0, p1):
    d = p1 - p0
    L2 = float(d @ d)
    if L2 == 0:
        return np.hypot(x - p0[0], y - p0[1])
    t = np.clip(((x - p0[0]) * d[0] + (y - p0[1]) * d[1]) / L2, 0, 1)
    return np.hypot(x - (p0[0] + t * d[0]), y - (p0[1] + t * d[1]))


# =============================================================================
# OKLab metrics
# =============================================================================
def srgb_to_linear(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, np.power((c + 0.055) / 1.055, 2.4))


def linear_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def oklab_L(rgb):
    lin = srgb_to_linear(rgb)
    lms = lin @ np.array([[0.4122214708, 0.2119034982, 0.0883024619],
                          [0.5363325363, 0.6806995451, 0.2817188376],
                          [0.0514459929, 0.1073969566, 0.6299787005]])
    lms = np.cbrt(lms)
    return lms @ np.array([0.2104542553, 0.7936177850, -0.0040720468])


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
            bits = 0
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
# SVG (vector approximation of the master)
# =============================================================================
def write_svg(path, size=256):
    geo = Geometry(size, hinted=False)
    f6 = lambda v: f"{v:.3f}".rstrip("0").rstrip(".")
    pt = lambda p: f"{f6(p[0])},{f6(p[1])}"
    hexd = lambda c: "#%02x%02x%02x" % tuple(int(round(x * 255)) for x in c)
    defs, faces, backs, seams = [], [], [], []
    for name, f in geo.faces.items():
        stops = []
        for i in range(33):
            s = i / 32
            col = edge_hue(s, f, FACE_SHAPING[name])
            stops.append(f'<stop offset="{f6(s)}" stop-color="{hexd(col)}"/>')
        A, B = f["Ai"], f["Bi"]
        defs.append(f'<linearGradient id="hue-{name}" gradientUnits="userSpaceOnUse" '
                    f'x1="{f6(A[0])}" y1="{f6(A[1])}" x2="{f6(B[0])}" y2="{f6(B[1])}">'
                    + "".join(stops) + "</linearGradient>")
        poly = " ".join(pt(p) for p in (f["Wi"], f["Ai"], f["Pi"], f["Bi"]))
        faces.append((name, poly))
        # Secondary seams along the two front edges (the hue fan's wedges), as
        # perpendicular fades sampled from the raster model at 65 % along the edge.
        for side, E, col in (("a", f["Ai"], f["colA"]), ("b", f["Bi"], f["colB"])):
            other = f["Bi"] if side == "a" else f["Ai"]
            e = (E - f["Wi"]) / np.hypot(*(E - f["Wi"]))
            nrm = np.array([-e[1], e[0]])
            if nrm @ (other - f["Wi"]) < 0:
                nrm = -nrm
            Q = f["Wi"] + 0.65 * (E - f["Wi"])
            # fade runs to the face diagonal, where the seam weight is exactly 0
            dia = f["Pi"] - f["Wi"]
            span = float(np.linalg.solve(np.array([nrm, -dia]).T, f["Wi"] - Q)[0])
            sstops = []
            for i in range(17):
                d = span * i / 16
                p = Q + d * nrm
                ab = f["inv"] @ (p - f["Wi"])
                t, _ = primary_weight(np.array(ab[0]), np.array(ab[1]), FACE_SHAPING[name])
                w = float(1.0 - t)
                sstops.append(f'<stop offset="{f6(i / 16)}" stop-color="{hexd(col)}" '
                              f'stop-opacity="{f6(w)}"/>')
            P2 = Q + span * nrm
            sid = f"seam-{name}-{side}"
            defs.append(f'<linearGradient id="{sid}" gradientUnits="userSpaceOnUse" '
                        f'x1="{f6(Q[0])}" y1="{f6(Q[1])}" x2="{f6(P2[0])}" y2="{f6(P2[1])}">'
                        + "".join(sstops) + "</linearGradient>")
            seams.append((sid, poly))
    # highlight + glass opacity as radial gradients about the white corner
    hl, ga = [], []
    for i in range(25):
        d = i / 24 * 1.2
        hl.append(f'<stop offset="{f6(i / 24)}" stop-color="#fff" stop-opacity="{f6(float(highlight(np.array(d))))}"/>')
        g = 1.0 - (1.0 - GLASS_ALPHA) * min(1.0, d) ** GLASS_ALPHA_GAMMA
        ga.append(f'<stop offset="{f6(i / 24)}" stop-color="#fff" stop-opacity="{f6(g)}"/>')
    r = geo.unit * 1.2
    W = geo.W
    defs.append(f'<radialGradient id="hl" gradientUnits="userSpaceOnUse" cx="{f6(W[0])}" cy="{f6(W[1])}" r="{f6(r)}">' + "".join(hl) + "</radialGradient>")
    defs.append(f'<radialGradient id="glassg" gradientUnits="userSpaceOnUse" cx="{f6(W[0])}" cy="{f6(W[1])}" r="{f6(r)}">' + "".join(ga) + "</radialGradient>")
    defs.append(f'<mask id="glass" maskUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}">'
                f'<rect width="{size}" height="{size}" fill="url(#glassg)"/></mask>')
    clip = "".join(f'<polygon points="{p}"/>' for _, p in faces)
    defs.append(f'<clipPath id="faces">{clip}</clipPath>')
    sd = BACK_EDGE_WIDTH * geo.fw * 0.35
    defs.append(f'<filter id="soft" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="{f6(sd)}"/></filter>')
    for p1 in (geo.G, geo.R, geo.B):
        backs.append(f'<line x1="{f6(geo.K[0])}" y1="{f6(geo.K[1])}" x2="{f6(p1[0])}" y2="{f6(p1[1])}"/>')
    frame = "M" + " L".join(pt(p) for p in geo.outer) + " Z"
    for _, f in geo.faces.items():
        frame += " M" + " L".join(pt(p) for p in (f["Wi"], f["Ai"], f["Pi"], f["Bi"])) + " Z"
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">',
        "<title>DesktopLUT</title>",
        "<defs>" + "".join(defs) + "</defs>",
        '<g mask="url(#glass)">',
        "".join(f'<polygon points="{p}" fill="url(#hue-{n})"/>' for n, p in faces),
        "".join(f'<polygon points="{p}" fill="url(#{sid})"/>' for sid, p in seams),
        f'<g clip-path="url(#faces)" filter="url(#soft)" stroke="#000" stroke-opacity="{f6(BACK_EDGE_STRENGTH)}" '
        f'stroke-width="{f6(BACK_EDGE_WIDTH * geo.fw)}" stroke-linecap="round">' + "".join(backs) + "</g>",
        f'<rect width="{size}" height="{size}" fill="url(#hl)" clip-path="url(#faces)"/>',
        "</g>",
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


def make_preview(path, master, master_inv, small, small_inv, stats_lines, alt=None):
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
           ("old inverted", orig_inv), ("new inverted", master_inv),
           (alt[0] if alt else "", alt[1] if alt else None)]
    big = [(t, im) for t, im in big if im is not None]
    gap, pad, lab = 16, 20, 20
    small_sizes = [16, 20, 24, 32, 40, 48, 64]
    W = pad * 2 + len(big) * 256 + (len(big) - 1) * gap
    band_h = lab + lab + 256 + gap + lab + 128 + pad
    head_h = 34 + 19 * len(stats_lines) + 12
    H = head_h + len(GROUNDS) * band_h
    sheet = Image.new("RGB", (W, H), (30, 30, 32))
    dr = ImageDraw.Draw(sheet)
    fT, fS, fM = _font(19), _font(13), _font(14)
    dr.text((pad, 8), "DesktopLUT mark: old (repo root DesktopLUT-logo*.png, DesktopLUT.ico) vs new (assets/logo)",
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


def compute_stats(master_cov, master):
    lines, raw = [], {}
    new_masks = new_face_masks(master_cov)
    raw["new"] = face_stats(master, new_masks)
    raw["new_dark"] = face_stats(master, new_masks, GROUNDS[0][1])
    if os.path.exists(ORIGINAL_PNG):
        orig = np.array(Image.open(ORIGINAL_PNG).convert("RGBA"))
        om = original_face_masks(orig)
        raw["old"] = face_stats(orig, om)
        raw["old_dark"] = face_stats(orig, om, GROUNDS[0][1])
        lines.append(fmt_stats("Face mean OKLab L, old  (straight colour)", raw["old"]))
    lines.append(fmt_stats("Face mean OKLab L, new  (straight colour)", raw["new"]))
    if "old_dark" in raw:
        lines.append(fmt_stats("Face mean OKLab L, old  (over #0b0b0c)   ", raw["old_dark"]))
    lines.append(fmt_stats("Face mean OKLab L, new  (over #0b0b0c)   ", raw["new_dark"]))
    return lines, raw


def primary_share():
    """Fraction of each face (master shaping) where the primary outweighs the
    secondary -- the check that every face still reads as its primary."""
    n = 256
    a, b = np.meshgrid((np.arange(n) + 0.5) / n, (np.arange(n) + 0.5) / n)
    return {k: float((primary_weight(a, b, v)[0] >= 0.5).mean()) for k, v in FACE_SHAPING.items()}


def extra_metrics(cov, master_inv):
    sh = primary_share()
    lines = [f"Primary share, new: top {sh['top']:.2f}   left {sh['left']:.2f}   right {sh['right']:.2f}",
             fmt_stats("Inverted, new (straight colour)", face_stats(master_inv, new_face_masks(cov)))]
    if os.path.exists(ORIGINAL_INV_PNG):
        oi = np.array(Image.open(ORIGINAL_INV_PNG).convert("RGBA"))
        lines.append(fmt_stats("Inverted, old (straight colour)", face_stats(oi, original_face_masks(oi))))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics", action="store_true", help="print per-face OKLab lightness")
    ap.add_argument("--out", default=OUT_DIR, help="output directory (default assets/logo)")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    imgs, imgs_inv = {}, {}
    for s in SIZES:
        imgs[s] = render(s)
        imgs_inv[s] = render(s, inverted=True)
        Image.fromarray(imgs[s], "RGBA").save(os.path.join(args.out, f"logo_{s}.png"), optimize=True)
        Image.fromarray(imgs_inv[s], "RGBA").save(os.path.join(args.out, f"logo_inv_{s}.png"), optimize=True)
    write_ico(os.path.join(args.out, "DesktopLUT_new.ico"), [imgs[s] for s in ICO_SIZES])
    write_ico(os.path.join(args.out, "DesktopLUT-active_new.ico"), [imgs_inv[s] for s in ICO_SIZES])
    write_svg(os.path.join(args.out, "logo.svg"))

    _, cov, _ = render(256, return_coverage=True)
    stats_lines, raw = compute_stats(cov, imgs[256])
    stats_lines += extra_metrics(cov, imgs_inv[256])
    alt = (f"alt: old proportions ({ALT_ELEVATION_DEG:g} deg)", render(256, elevation=ALT_ELEVATION_DEG))
    make_preview(os.path.join(args.out, "preview.png"), imgs[256], imgs_inv[256], imgs, imgs_inv, stats_lines, alt)
    if args.metrics:
        print("\n".join(stats_lines))
    print(f"wrote {len(SIZES) * 2} PNGs, 2 ICOs, logo.svg, preview.png -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
