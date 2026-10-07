"""Moving content against the PA32UCXR's measured time law — the offline MOTION simulator (2026-10-04).

Every FALD temporal measurement so far was a block toggling in place (``fald-model-current.md`` §6); no moving object
has been modelled under the measured law or filmed since the owner's 2026-09-15 pan video. This module runs moving
scenes through the panel's forward model (:mod:`dlc.fald.paneltime`) with and without the layer
(:func:`dlc.fald.correct.correct_image` = the shader) and scores what a viewer would see on the STATIC background
beside the path — the "visible zone transitions" — and on the moving object itself.

**One scene definition for the simulator AND the motion TPG.** A :class:`Scene` is plain data in FULL-RESOLUTION panel
pixels (3840 × 2160, pixel j covers [j, j+1)) and refreshes (one content frame per panel refresh unless ``cadence``
says otherwise), with an exact coverage rule per shape (:func:`coverage_rect` box-filter area, :func:`coverage_disc`
SDF clamp) blended in LINEAR nits. The TPG renders the same formulas, so a frozen prediction and the filmed stimulus
are the same frames (camera-aid counters aside, see the end).

**Why the full-resolution peak matters.** The model works on a 1/``scale`` raster (768 × 432 at scale 5). A 1-px
column entering a zone averages to 1/5 of its level there, but the shader's statistic runs at full resolution and the
panel's border law is a LEVEL law (one pixel column inside a zone → ~full drive, §4.2). :class:`MotionModel` therefore
reads each zone's peak from the full-resolution render (per raster pixel, the RGB of its brightest full-resolution
pixel) and carries it through the correction as a COMPANION image (``correct_image(..., peak=)``): the same fields, the
same per-pixel rule at the peak's own level — the soft knee acts per full-resolution pixel, so the peak of a corrected
edge pixel is not the raster pixel scaled by its mean correction (review 2026-10-04: that shortcut overstated edge-zone
drives by up to 0.25).

**Two panel-truth statistics bracket the unknown** (P10 refit owed; the measured regimes disagree with any single
form): ``"area"`` = the shipped ``min(peak, Σ/A0)`` at full resolution (what the layer itself assumes: the panel then
agrees with the layer's model and only the time law differs), ``"level"`` = the brightest lit pixel (one column →
full drive at its level; the upper bracket of border snapping). ``"ctx"`` = the P10 context statistic (FaldParams
``stat_ctx_*``; area on black, ~level on a lit background), fed the full-resolution peak AND log-mean companions. The
LAYER runs the fit's own statistic (``simulate_scene(layer_stat=None)``: "ctx" for a ``stat_kind`` "ctxpow" fit = the FLD5
shader, else "area" = the shader today).

Not modelled: LCD transition time (as :mod:`dlc.fald.paneltime`), starfield balancing (S1: live in the owner's HDR INI;
it acts on specks, which the default scenes avoid — a scene with specks must say so); the boost table's LIT rule reads the full-resolution peak (:meth:`MotionModel.active_zones`).
The camera aids' counters show the TPG's PRESENT number; here they show content index + 1 — the aid pixels differ in a
real play (they are excluded from every score).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

from .correct import reference_pedestal
from .model import FaldModel, FaldParams
from .paneltime import PanelClock, PanelDriveState, PanelTimeLaw

PANEL_W, PANEL_H = 3840, 2160


# ------------------------------------------------------------------------------------------------ scene description
@dataclass(frozen=True)
class MovingShape:
    """One shape in full-resolution panel pixels. ``kind`` "rect" (``w`` × ``h``) or "disc" (radius ``r``); centre
    (``x``, ``y``) at the first frame; moves (``vx``, ``vy``) px per CONTENT frame while the scene's motion runs;
    ``nits`` = linear level per channel (R, G, B as-if-white nits; grey = three equal values). ``blink`` > 0: the shape
    is shown for ``blink`` content frames, hidden for ``blink``, … — visible at content frame i when
    ``((i + blink_phase) // blink) % 2 == 0`` (a toggle at refresh / (2·blink) with cadence 1: blink 2 = 15 Hz at 60 Hz)."""
    kind: str
    x: float
    y: float
    nits: tuple[float, float, float]
    w: float = 0.0
    h: float = 0.0
    r: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    blink: int = 0
    blink_phase: int = 0
    angle: float = 0.0       # "bar": a ``w`` (thickness) × ``h`` (length) box rotated by ``angle`` degrees (simulator only so far)

    def visible(self, i: int) -> bool:
        return self.blink <= 0 or ((int(i) + self.blink_phase) // self.blink) % 2 == 0

    def centre(self, t: float) -> tuple[float, float]:
        return self.x + self.vx * t, self.y + self.vy * t

    def bbox(self, t: float) -> tuple[float, float, float, float]:
        cx, cy = self.centre(t)
        if self.kind == "rect":
            hw, hh = self.w / 2.0, self.h / 2.0
        elif self.kind == "bar":
            c, sn = abs(math.cos(math.radians(self.angle))), abs(math.sin(math.radians(self.angle)))
            hw = 0.5 * (self.h * c + self.w * sn) + 0.5
            hh = 0.5 * (self.h * sn + self.w * c) + 0.5
        else:
            hw = hh = self.r + 0.5
        return cx - hw, cy - hh, cx + hw, cy + hh

    def sdf(self, t: float, xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
        """Signed distance (px) from the shape's edge at points (xx, yy): < 0 inside. rect / bar = the exact box SDF (in the
        bar's own frame), disc = |p − c| − r."""
        cx, cy = self.centre(t)
        dx, dy = xx - cx, yy - cy
        if self.kind == "disc":
            return np.hypot(dx, dy) - self.r
        if self.kind == "bar":
            a = math.radians(self.angle)
            dx, dy = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
            hw, hh = self.h / 2.0, self.w / 2.0            # along the length, across the thickness
        else:
            hw, hh = self.w / 2.0, self.h / 2.0
        qx, qy = np.abs(dx) - hw, np.abs(dy) - hh
        return np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0)


@dataclass(frozen=True)
class Scene:
    """A moving-content stimulus. Content frame i (0 … ``pre + move + post − 1``) has motion time
    ``clip(i − pre, 0, move)``: ``pre`` static frames (the panel settles on the start), ``move`` frames of motion, ``post``
    static frames at the end position. ``cadence``: panel refreshes per content frame, cycled (default (1,) = a new
    frame every refresh; (3, 2) = 24 p in 60 Hz). ``bg`` = background level (R, G, B nits).

    Camera aids (the TPG shows them; rendered here too so a prediction sees the same frames — keep them ≥ 6 zones from the
    path): ``sync`` = (x0, y0, w, h, lo, hi) a grey patch at ``lo`` nits that flips at the first motion frame and back at
    the first post frame (put it on the object's rows: the panel scans out top to bottom); ``code`` = (x0, y0, cell, bits,
    lo, hi) a row of ``bits + 2`` square cells — reference lo, reference hi, then the Gray code of the TPG's present number,
    bit 0 first (here: of content frame index + 1); ``digits`` = (x0, y0, h, n, lo, hi) the same number READABLE: n
    7-segment digits (mod 10^n) of height h on a plate at ``lo``, lit segments at ``hi`` (it changes many pixels per frame
    — keep it dim and far from the path)."""
    name: str
    bg: tuple[float, float, float]
    shapes: tuple[MovingShape, ...]
    pre: int = 20
    move: int = 60
    post: int = 24
    cadence: tuple[int, ...] = (1,)
    note: str = ""
    sync: tuple = ()
    code: tuple = ()
    digits: tuple = ()

    @property
    def frames(self) -> int:
        return self.pre + self.move + self.post

    def motion_time(self, i: int) -> float:
        return float(min(max(i - self.pre, 0), self.move))

    def refreshes(self) -> list[int]:
        """Panel refreshes each content frame stays up."""
        return [int(self.cadence[i % len(self.cadence)]) for i in range(self.frames)]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["frames"] = self.frames
        return d

    @staticmethod
    def from_dict(d: dict) -> "Scene":
        shapes = tuple(MovingShape(**{k: (tuple(v) if k == "nits" else v) for k, v in s.items()}) for s in d["shapes"])
        return Scene(name=d["name"], bg=tuple(d["bg"]), shapes=shapes, pre=int(d["pre"]), move=int(d["move"]),
                     post=int(d["post"]), cadence=tuple(int(c) for c in d.get("cadence", (1,))), note=d.get("note", ""),
                     sync=tuple(d.get("sync", ())), code=tuple(d.get("code", ())), digits=tuple(d.get("digits", ())))

    def aid_rects(self, i: int) -> list:
        """The camera aids at content frame ``i`` as ``(x0, y0, w, h, nits)`` grey rects (empty without aids)."""
        out = []
        if self.sync:
            x0, y0, w, h, lo, hi = self.sync
            ev = (1 if i >= self.pre else 0) + (1 if i >= self.pre + self.move else 0)
            out.append((x0, y0, w, h, hi if ev % 2 else lo))
        if self.code:
            x0, y0, cell, bits, lo, hi = self.code
            g = (i + 1) ^ ((i + 1) >> 1)
            vals = [lo, hi] + [hi if (g >> k) & 1 else lo for k in range(int(bits))]
            out.extend((x0 + j * cell, y0, cell, cell, v) for j, v in enumerate(vals))
        if self.digits:
            out.extend(digit_rects(*self.digits, value=i + 1))
        return out


_SEG = (0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F)   # 7-segment bits a..g per digit


def digit_rects(x0: float, y0: float, h: float, n: int, lo: float, hi: float, value: int) -> list:
    """The readable counter as grey rects ``(x, y, w, h, nits)``: a plate at ``lo`` (pad = one stroke), then the lit
    segments of ``value`` mod 10^n at ``hi``. Digit width 0.6 h, stroke h / 8, gap 0.25 h — the TPG's ``fillCB``."""
    w, t, gap, n = 0.6 * h, h / 8.0, 0.25 * h, int(n)
    out = [(x0 - t, y0 - t, n * w + (n - 1) * gap + 2 * t, h + 2 * t, lo)]
    v = int(value)
    digs = []
    for d in range(n - 1, -1, -1):
        digs.append((d, _SEG[v % 10])); v //= 10
    hv = 0.5 * h - 1.5 * t
    for d, seg in digs:
        x, y = x0 + d * (w + gap), y0
        segs = ((x + t, y, w - 2 * t, t), (x + w - t, y + t, t, hv), (x + w - t, y + 0.5 * h + 0.5 * t, t, hv),
                (x + t, y + h - t, w - 2 * t, t), (x, y + 0.5 * h + 0.5 * t, t, hv), (x, y + t, t, hv),
                (x + t, y + 0.5 * h - 0.5 * t, w - 2 * t, t))
        out.extend((*r, hi) for k, r in enumerate(segs) if seg & (1 << k))
    return out


def grey(n: float) -> tuple[float, float, float]:
    return (float(n), float(n), float(n))


# ------------------------------------------------------------------------------------------------ exact coverage
def _cov_1d(lo: float, hi: float, j0: int, n: int) -> np.ndarray:
    """Overlap of [lo, hi) with pixels j0 … j0+n−1 (pixel j = [j, j+1)) — the exact box-filter coverage."""
    j = np.arange(j0, j0 + n, dtype=np.float64)
    return np.clip(np.minimum(j + 1.0, hi) - np.maximum(j, lo), 0.0, 1.0)


def coverage_rect(s: MovingShape, t: float, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    """(ny, nx) exact area coverage of a rect over the pixel window starting at (x0, y0). The TPG's pixel shader:
    cov = clamp(min(px+1, x1) − max(px, x0), 0, 1) · clamp(min(py+1, y1) − max(py, y0), 0, 1)."""
    a, b, c, d = s.bbox(t)
    return _cov_1d(b, d, y0, ny)[:, None] * _cov_1d(a, c, x0, nx)[None, :]


def coverage_disc(s: MovingShape, t: float, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    """(ny, nx) coverage of a disc: clamp(r − |pixel centre − centre| + 0.5, 0, 1) (SDF anti-aliasing; area-exact to
    ~1e-3 of the rim for r ≥ 2 px). The TPG's pixel shader uses the same expression."""
    cx, cy = s.centre(t)
    yy = np.arange(y0, y0 + ny, dtype=np.float64)[:, None] + 0.5
    xx = np.arange(x0, x0 + nx, dtype=np.float64)[None, :] + 0.5
    return np.clip(s.r - np.hypot(xx - cx, yy - cy) + 0.5, 0.0, 1.0)


def coverage_bar(s: MovingShape, t: float, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    """(ny, nx) coverage of a rotated bar: clamp(0.5 − SDF at the pixel centre, 0, 1) (the disc's anti-aliasing rule)."""
    yy = np.arange(y0, y0 + ny, dtype=np.float64)[:, None] + 0.5
    xx = np.arange(x0, x0 + nx, dtype=np.float64)[None, :] + 0.5
    return np.clip(0.5 - s.sdf(t, xx, yy), 0.0, 1.0)


def coverage(s: MovingShape, t: float, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    if s.kind == "rect":
        return coverage_rect(s, t, x0, y0, nx, ny)
    if s.kind == "disc":
        return coverage_disc(s, t, x0, y0, nx, ny)
    if s.kind == "bar":
        return coverage_bar(s, t, x0, y0, nx, ny)
    raise ValueError(f"unknown shape kind {s.kind!r}")


def render_full_patch(scene: Scene, i: int, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    """(3, ny, nx) linear nits of content frame ``i`` over a full-resolution window: the background, then every shape
    painted in order with its coverage (``out = out·(1 − cov) + nits·cov``), then the camera aids — the TPG's order and
    formulas exactly (``tests/test_fald_motion_tpg.py`` compares the two pixel for pixel)."""
    t = scene.motion_time(i)
    out = np.empty((3, ny, nx), dtype=np.float64)
    out[:] = np.asarray(scene.bg, dtype=np.float64)[:, None, None]
    for s in scene.shapes:
        if not s.visible(i):
            continue
        cov = coverage(s, t, x0, y0, nx, ny)
        out = out * (1.0 - cov)[None] + np.asarray(s.nits, dtype=np.float64)[:, None, None] * cov[None]
    for ax, ay, aw, ah, v in scene.aid_rects(i):
        cov = _cov_1d(ay, ay + ah, y0, ny)[:, None] * _cov_1d(ax, ax + aw, x0, nx)[None, :]
        out = out * (1.0 - cov)[None] + v * cov[None]
    return out


def _render_window(scene: Scene, i: int, scale: int, img: np.ndarray, peak: np.ndarray,
                   x0: float, y0: float, x1: float, y1: float, logm: Optional[np.ndarray] = None,
                   log_eps: float = 0.05, white: float = 1842.0) -> None:
    """Render the full-resolution window covering [x0, x1) × [y0, y1) (snapped out to whole raster pixels, one pixel of
    margin) with :func:`render_full_patch` and write its block mean into ``img`` and its block PEAK (the RGB of the
    full-resolution pixel with the largest channel) into ``peak``. Exact wherever it is written: the patch carries the
    background, every shape and every aid in the TPG's order."""
    hr, wr = img.shape[1:]
    rx0 = max(int(math.floor(x0)) // scale - 1, 0); ry0 = max(int(math.floor(y0)) // scale - 1, 0)
    rx1 = min(int(math.ceil(x1)) // scale + 2, wr); ry1 = min(int(math.ceil(y1)) // scale + 2, hr)
    if rx1 <= rx0 or ry1 <= ry0:
        return
    nyr, nxr = ry1 - ry0, rx1 - rx0
    patch = render_full_patch(scene, i, rx0 * scale, ry0 * scale, nxr * scale, nyr * scale)
    blocks = patch.reshape(3, nyr, scale, nxr, scale).transpose(0, 1, 3, 2, 4).reshape(3, nyr, nxr, scale * scale)
    img[:, ry0:ry1, rx0:rx1] = blocks.mean(axis=3)
    k = blocks.max(axis=0).argmax(axis=2)                                   # brightest full-res pixel of each block
    peak[:, ry0:ry1, rx0:rx1] = np.take_along_axis(blocks, k[None, :, :, None], axis=3)[..., 0]
    if logm is not None:                                                    # mean of ln max(s, eps), s capped at white
        logm[ry0:ry1, rx0:rx1] = np.log(np.maximum(np.minimum(blocks.max(axis=0), white), log_eps)).mean(axis=2)


def render_reduced(scene: Scene, i: int, scale: int, w: int = PANEL_W, h: int = PANEL_H,
                   log_eps: Optional[float] = None, white: float = 1842.0):
    """Content frame ``i`` on the model raster: ``(img, peak)`` — img (3, h/scale, w/scale) = the exact block MEAN of the
    full-resolution render (light is conserved); peak (3, h/scale, w/scale) = per block, the RGB of its brightest
    full-resolution pixel (what a full-resolution statistic sees; ``peak.max(0)`` is the block max of the brightest
    channel). Only the shapes' bounding box and the aids' bounding box are rendered at full resolution (each window
    exactly, shapes and aids included); elsewhere the frame is the background. ``log_eps`` given: ``(img, peak, logm)``
    with logm (h/scale, w/scale) = per block the mean of ln max(s, log_eps), s = the brightest channel capped at
    ``white`` — the P10 context statistic's geometric-mean companion (``FaldModel.set_peak(peak, logm)``)."""
    hr, wr = h // scale, w // scale
    bg = np.asarray(scene.bg, dtype=np.float64)
    img = np.empty((3, hr, wr)); img[:] = bg[:, None, None]
    peak = img.copy()
    logm = None
    if log_eps is not None:
        logm = np.full((hr, wr), math.log(max(min(float(bg.max()), white), log_eps)))
    kw = {} if logm is None else {"logm": logm, "log_eps": float(log_eps), "white": float(white)}
    aids = scene.aid_rects(i)
    if aids:
        _render_window(scene, i, scale, img, peak, min(a[0] for a in aids), min(a[1] for a in aids),
                       max(a[0] + a[2] for a in aids), max(a[1] + a[3] for a in aids), **kw)
    t = scene.motion_time(i)
    boxes = [s.bbox(t) for s in scene.shapes if s.visible(i)]
    if boxes:
        _render_window(scene, i, scale, img, peak, min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes), **kw)
    return (img, peak) if logm is None else (img, peak, logm)


# ------------------------------------------------------------------------------------------------ the model
class MotionModel(FaldModel):
    """:class:`FaldModel` whose zone statistic reads the FULL-RESOLUTION peak of the image being evaluated.

    Before every call that forms drives (:meth:`cell_drives`, :meth:`led_boost`), :meth:`set_peak` the PEAK image of the
    image about to be evaluated — per raster pixel the RGB of its brightest full-resolution pixel: the content's
    (:func:`render_reduced`) or a corrected request's (:func:`dlc.fald.correct.correct_image` carries a ``peak``
    companion through the same per-pixel rule — the soft knee acts per full-resolution pixel, so the peak is NOT the
    raster pixel scaled by its mean correction). ``None`` = the raster itself (the plain model). ``stat`` = "area" (the
    shipped ``min(peak, Σ/A0)``, the shader's), "level" (the brightest lit pixel — the border law's upper bracket) or
    "power:<g>" = ``peak · min(1, Σ / (A0 · peak))^g`` (g 1 = area, 0 = level; the 2026-10-04 border meter probe — a
    1000-nit 40x270 bar stepped into a zone — fits g ≈ 0.45: the first pixel column already gives 2/3 of the zone's
    effect, `results/fald_motion_2026-10-04/border_analysis.json`); the area sum always comes from the raster."""

    def __init__(self, p: FaldParams, stat: str = "area"):
        super().__init__(p)
        self.gamma: Optional[float] = None
        if stat.startswith("power:"):
            self.gamma = float(stat.split(":", 1)[1])
            if not 0.0 <= self.gamma <= 1.0:
                raise ValueError(f"power statistic exponent must be in [0, 1], got {self.gamma}")
        elif stat not in ("area", "level", "ctx"):
            raise ValueError(f"stat must be 'area', 'level', 'ctx' or 'power:<g>', got {stat!r}")
        self.stat = stat

    def cell_drives(self, img: np.ndarray) -> np.ndarray:
        """``stat`` "ctx" = the P10 context statistic (``FaldParams.stat_ctx_*``, :meth:`FaldModel._ctx_drives`) with the
        full-resolution peak, whatever ``p.stat_kind`` says; every other ``stat`` goes through :meth:`_area_stat`."""
        if self.stat == "ctx":
            return self._ctx_drives(np.minimum(np.max(img, axis=0), self.p.white_nits))
        if self.p.stat_kind == "ctxpow":                # a ctx fit evaluated under another panel statistic (brackets)
            return self.drive_of(self._area_stat(np.minimum(np.max(img, axis=0), self.p.white_nits)))
        return super().cell_drives(img)

    def _area_stat(self, s: np.ndarray) -> np.ndarray:
        p = self.p
        pk = self._peak_px(s)
        lit_pk = pk > p.drive_floor_nits
        peak = (pk * lit_pk).reshape(p.rows, self.ch, p.cols, self.cw).max(axis=(1, 3))
        if self.stat == "level":
            return peak
        lit = s > p.drive_floor_nits
        tot = (s * lit).reshape(p.rows, self.ch, p.cols, self.cw).sum(axis=(1, 3)) * float(p.scale ** 2)
        if self.gamma is not None:
            return peak * np.minimum(1.0, tot / np.maximum(p.stat_area0_px2 * peak, 1e-9)) ** self.gamma
        return np.minimum(peak, tot / p.stat_area0_px2)

    def active_zones(self, img: np.ndarray) -> np.ndarray:
        """The boost's zone rule with the LIT test on the full-resolution peak (any pixel above ``boost_lit_nits`` —
        ``boost_lit_frac`` is 0 in the shipped files; a non-zero fraction keeps the raster test)."""
        zones = super().active_zones(img)
        if self._peak is None or self.p.boost_lit_frac > 0.0:
            return zones
        lit = (self._peak > self.p.boost_lit_nits).reshape(self.p.rows, self.ch, self.p.cols, self.cw).any(axis=(1, 3))
        return zones | lit


# ------------------------------------------------------------------------------------------------ running a scene
def object_mask(model: FaldModel, scene: Scene, i: int, inset_px: float = 6.0) -> np.ndarray:
    """Reduced pixels well inside any shape at content frame ``i`` (the object's own luminance). The inset shrinks to a
    quarter of a small shape; a shape with no reduced pixel centre inside falls back to the pixel at its centre."""
    sc = model.p.scale
    yy = (np.arange(model.h)[:, None] + 0.5) * sc
    xx = (np.arange(model.w)[None, :] + 0.5) * sc
    m = np.zeros((model.h, model.w), bool)
    t = scene.motion_time(i)
    for s in scene.shapes:
        cx, cy = s.centre(t)
        if s.kind == "rect":
            a, b, c, d = s.bbox(t)
            ins = min(inset_px, 0.25 * min(s.w, s.h))
            mi = (xx >= a + ins) & (xx <= c - ins) & (yy >= b + ins) & (yy <= d - ins)
        elif s.kind == "bar":
            mi = s.sdf(t, xx, yy) <= -min(inset_px, 0.25 * min(s.w, s.h))
        else:
            mi = np.hypot(xx - cx, yy - cy) <= s.r - min(inset_px, 0.25 * s.r)
        if not mi.any():
            mi = np.zeros_like(m)
            mi[min(max(int(cy // sc), 0), model.h - 1), min(max(int(cx // sc), 0), model.w - 1)] = True
        m |= mi
    return m


def static_mask(model: FaldModel, scene: Scene, margin_px: float = 10.0, near_zones: float = 4.0,
                border_zones: int = 2) -> np.ndarray:
    """Reduced pixels whose CONTENT never changes during the scene (farther than ``margin_px`` from every shape at every
    frame and from the camera aids) and that lie within ``near_zones`` zone widths of the path (where the layer and the
    LED transitions act); ``border_zones`` at the panel edge are excluded (the model's gains are not trusted there)."""
    p = model.p
    sc = p.scale
    yy = (np.arange(model.h)[:, None] + 0.5) * sc
    xx = (np.arange(model.w)[None, :] + 0.5) * sc
    touched = np.zeros((model.h, model.w), bool)
    near = np.zeros((model.h, model.w), bool)
    reach = near_zones * p.cell_w
    pad = margin_px + 0.5 * sc * math.sqrt(2.0)
    for i in range(scene.frames):
        t = scene.motion_time(i)
        for s in scene.shapes:
            if s.kind == "bar":                                   # the true diagonal, not its bounding box
                dist = np.maximum(s.sdf(t, xx, yy), 0.0)
            else:
                a, b, c, d = s.bbox(t)
                dx = np.maximum(np.maximum(a - xx, xx - c), 0.0); dy = np.maximum(np.maximum(b - yy, yy - d), 0.0)
                dist = np.hypot(dx, dy)
            touched |= dist <= pad
            near |= dist <= reach
    for x0, y0, aw, ah, _ in scene.aid_rects(0):   # aids change on purpose: never score them as static background
        dx = np.maximum(np.maximum(x0 - xx, xx - (x0 + aw)), 0.0); dy = np.maximum(np.maximum(y0 - yy, yy - (y0 + ah)), 0.0)
        touched |= np.hypot(dx, dy) <= pad
    bx, by = border_zones * model.cw, border_zones * model.ch
    inside = np.zeros_like(near); inside[by:model.h - by, bx:model.w - bx] = True
    return near & ~touched & inside


@dataclass
class PanelRun:
    """What one panel hypothesis showed over a scene, per REFRESH: ``ys`` (n, m) float32 luminance at the static pixels
    (``mask`` order), ``row`` (n, w) float32 luminance along the kymograph row, ``obj`` (n,) mean luminance over the
    object's interior, ``obj_spread`` (n,) the interior's spatial non-uniformity per refresh = (p95 − p5) / p50 of Y over
    the interior (edges excluded by the object mask's inset) — a zone grid printed INSIDE a flat bright shape raises it;
    ``snaps`` {refresh: (h, w) float32 Y} for the refreshes asked for."""
    truth: str
    parity: int
    ys: np.ndarray
    row: np.ndarray
    obj: np.ndarray
    obj_spread: np.ndarray = None
    snaps: dict = None


@dataclass
class SceneRun:
    scene: Scene
    content_index: list          # per refresh: the content frame on the panel
    mask: np.ndarray             # static pixels
    target_static: np.ndarray    # (m,) uniform-field luminance at the static pixels (their content never changes)
    kymo_row: int
    runs: list                   # PanelRun per (truth, parity)


def simulate_scene(p: FaldParams, scene: Scene, state=None, panels: Sequence[tuple[str, int]] = (("area", 0), ("area", 1)),
                   law: PanelTimeLaw = PanelTimeLaw(), rerender_on_repeat: bool = True, iters: int = 2,
                   kymo_row: Optional[int] = None, mask: Optional[np.ndarray] = None,
                   control: Optional[Callable] = None, layer_stat: Optional[str] = None,
                   snapshot_refreshes: Sequence[int] = ()) -> SceneRun:
    """Stream ``scene`` refresh by refresh through the layer (``state``: None = layer OFF; a
    :class:`dlc.fald.temporal.DriveState` — ``DriveState(MODE_OFF)`` = the static layer — or a
    :class:`dlc.fald.paneltime.PanelDriveState`) and through every panel hypothesis ``(truth statistic, tick parity)``.
    The layer's output never depends on the panel (it is open loop), so one layer pass feeds all of them.

    ``rerender_on_repeat``: the layer re-runs on every refresh of a held content frame (the shader's settle hold, k = 1
    each — overlay mode; in hook mode the 1×1 kicker that drives it is NOT HW-proven); False = the request of the
    frame's first refresh stays up and the state commits all its refreshes at once.

    ``control``: an optional content-side controller ``control(layer_model, img, peak, req, req_peak) -> (req, req_peak)``
    applied to every refresh's request after the layer (candidate motion algorithms; ``peak`` / ``req_peak`` = the
    full-resolution peak companions of the content and of the request); it may keep its own state. ``layer_stat``: the
    statistic the LAYER assumes; None = the fit's own (``p.stat_kind`` "ctxpow" -> "ctx", the FLD5 shader; else "area",
    the shader today); "level" = a candidate that believes the border law. A "ctx" layer or panel gets the
    full-resolution log companion as well as the peak."""
    from .correct import correct_image
    if layer_stat is None:
        layer_stat = "ctx" if p.stat_kind == "ctxpow" else "area"
    layer = MotionModel(p, layer_stat)
    want_log = layer_stat == "ctx" or any(t == "ctx" for t, _ in panels)
    pms = [(t, par, MotionModel(p, t), PanelClock(law, par)) for t, par in panels]
    w = np.array(p.chan_weights)[:, None, None]
    lmax = p.white_nits * w
    tv = p.tmin_vec()[:, None, None]
    m = static_mask(layer, scene) if mask is None else mask
    if kymo_row is None:
        top = min(s.bbox(0)[1] for s in scene.shapes)
        kymo_row = int(min(max((top - 25.0) // p.scale, 0), layer.h - 1))
    refr = scene.refreshes()
    n = sum(refr)
    ys = {k: np.empty((n, int(m.sum())), np.float32) for k in panels}
    rows = {k: np.empty((n, layer.w), np.float32) for k in panels}
    objs = {k: np.empty(n) for k in panels}
    spreads = {k: np.full(n, np.nan) for k in panels}
    snaps = {k: {} for k in panels}
    want_snap = set(int(v) for v in snapshot_refreshes)
    cidx, target_static, k_out = [], None, 0
    for i, kk in enumerate(refr):
        if want_log:
            img, pk_img, lg_img = render_reduced(scene, i, p.scale, p.width, p.height, log_eps=p.stat_ctx_eps,
                                                 white=p.white_nits)
        else:
            (img, pk_img), lg_img = render_reduced(scene, i, p.scale, p.width, p.height), None
        if target_static is None:
            tgt = (img * w).sum(axis=0) + reference_pedestal(layer, img)
            target_static = tgt[m].astype(np.float32)
        om = object_mask(layer, scene, i)
        req, req_pk = img, pk_img
        for r in range(kk):
            if state is not None and (r == 0 or rerender_on_repeat):
                res = correct_image(layer, img, iters=iters, drive_filter=state.fields, peak=pk_img, logm=lg_img)
                layer.set_peak(None)
                req, req_pk = res["req"], res["req_peak"]
                if isinstance(state, PanelDriveState):
                    state.commit(res["drives"], refreshes=1 if rerender_on_repeat else kk)
                else:
                    state.commit(res["drives"])
            sent, sent_pk = (req, req_pk) if control is None else control(layer, img, pk_img, req, req_pk)
            lg_sent = None if lg_img is None else layer.logm_follow(lg_img, img, sent)
            for truth, par, pm, clock in pms:
                pm.set_peak(sent_pk, lg_sent)
                d = pm.cell_drives(sent)
                boost = pm.led_boost(sent)
                pm.set_peak(None)
                s_true, s_est = clock.step(d)
                b_true, b_est = pm.backlights(s_true, s_est, boost=boost)   # LEDs from s_true, the panel's estimate from s_est
                t = np.clip(sent * w / np.maximum(lmax * np.maximum(b_est, 1e-6)[None], 1e-9), 0.0, 1.0)
                y = (lmax * b_true[None] * (t + tv)).sum(axis=0)
                key = (truth, par)
                ys[key][k_out] = y[m]
                rows[key][k_out] = y[kymo_row]
                objs[key][k_out] = float(y[om].mean())
                if om.sum() >= 4:
                    q5, q50, q95 = np.percentile(y[om], (5, 50, 95))
                    spreads[key][k_out] = float((q95 - q5) / max(q50, 1e-9))
                if k_out in want_snap:
                    snaps[key][k_out] = y.astype(np.float32)
            cidx.append(i)
            k_out += 1
    runs = [PanelRun(t, par, ys[(t, par)], rows[(t, par)], objs[(t, par)], spreads[(t, par)], snaps[(t, par)])
            for t, par in panels]
    return SceneRun(scene, cidx, m, target_static, kymo_row, runs)


# ------------------------------------------------------------------------------------------------ scoring
def score(run: PanelRun, sr: SceneRun, fuse: int = 3, floor_nits: float = 0.05) -> dict:
    """Numbers for one panel hypothesis. Static background, from the first refresh of motion to the end:
    ``step_*`` = refresh-to-refresh |Δ ln Y| (% after exp−1) — p99 over pixels × refreshes and the max; ``fused_*`` =
    the same on Y averaged over ``fuse`` refreshes (≈ 50 ms, the flash the eye integrates); ``swing_p95`` = per-pixel
    (max − min) / min over the window, p95 over pixels; ``err_*`` = |ln(Y / target)| over the static pixels and the
    window (accuracy vs the uniform-field look, static halo included). Object: interior mean Y per refresh relative to
    its SETTLED value on the last static refresh before the motion (a narrow object never reaches its uniform-field
    target — one zone column of LEDs — so the reference is its own rest level): ``obj_min`` / ``obj_max`` over the
    motion and the post frames, ``obj_pulse`` = the largest refresh-to-refresh change (%) of that ratio while the motion
    runs (the object brightening / dimming as it crosses zone columns). Also ``trace_step`` = per refresh, the p99
    over the static pixels of |Δ ln Y|, and ``trace_obj`` = the object ratio per refresh (for plots).

    ``floor_nits``: every relative metric reads max(Y, floor) and max(target, floor) — on a BLACK background (target ≈ 0)
    a ratio of two near-zero levels is not a visible quantity. The absolute companions, which a black scene must be read
    by: ``step_abs_max`` = the largest refresh-to-refresh |ΔY| (nits) on the static pixels, ``excess_abs_max`` = the
    largest Y − target (nits) over the window."""
    sc = sr.scene
    if sc.pre < 1 or sc.move + sc.post < 1:
        raise ValueError(f"{sc.name}: scoring needs pre >= 1 (the rest reference) and move + post >= 1")
    ci = sr.content_index
    n0 = next(n for n, i in enumerate(ci) if i >= sc.pre)
    n_end = next((n for n, i in enumerate(ci) if i >= sc.pre + sc.move), len(ci))
    y = run.ys.astype(np.float64)
    ly = np.log(np.maximum(y, floor_nits))
    d = np.abs(np.diff(ly, axis=0))[n0 - 1:]
    c = np.cumsum(np.vstack([np.zeros((1, y.shape[1])), y]), axis=0)
    yf = (c[fuse:] - c[:-fuse]) / fuse                                  # running mean over `fuse` refreshes
    df = np.abs(np.diff(np.log(np.maximum(yf, floor_nits)), axis=0))[max(n0 - fuse, 0):]
    win = y[n0:]
    tgt = sr.target_static.astype(np.float64)[None]
    err = np.abs(np.log(np.maximum(win, floor_nits) / np.maximum(tgt, floor_nits)))
    wf = np.maximum(win, floor_nits)
    ob = run.obj[n0:] / run.obj[n0 - 1]
    mov = np.concatenate([[1.0], ob[:max(n_end - n0, 1)]])            # from the rest level into the motion
    pc = lambda v: 100.0 * (math.exp(float(v)) - 1.0)
    return {
        "step_p99": pc(np.percentile(d, 99)), "step_max": pc(d.max()),
        "fused_p99": pc(np.percentile(df, 99)), "fused_max": pc(df.max()),
        "swing_p95": 100.0 * float(np.percentile(wf.max(axis=0) / wf.min(axis=0) - 1.0, 95)),
        "step_abs_max": float(np.abs(np.diff(y, axis=0))[n0 - 1:].max()), "excess_abs_max": float((win - tgt).max()),
        "err_mean": pc(err.mean()), "err_p95": pc(np.percentile(err, 95)),
        "obj_min": float(ob.min()), "obj_max": float(ob.max()),
        "obj_spread_rest": (100.0 * float(run.obj_spread[n0 - 1])) if run.obj_spread is not None else float("nan"),
        "obj_spread_max": (100.0 * float(np.nanmax(run.obj_spread[n0:]))) if run.obj_spread is not None else float("nan"),
        "obj_spread_step": (100.0 * float(np.nanmax(np.abs(np.diff(run.obj_spread[n0 - 1:])))))
                           if run.obj_spread is not None else float("nan"),
        "obj_pulse": pc(np.abs(np.diff(np.log(np.maximum(mov, 1e-9)))).max()) if mov.size > 1 else float("nan"),
        "static_px": int(sr.mask.sum()),
        "trace_step": [pc(v) for v in np.percentile(np.abs(np.diff(ly, axis=0)), 99, axis=1)],
        "trace_obj": [float(v) for v in ob],
    }
