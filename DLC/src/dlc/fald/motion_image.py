"""IMAGE-PAN scenes for the motion TPG (2026-10-08) — a real lit-background image panned frame-exactly on the panel: the
slow-pan study's stimuli (``results/fald_slowpan_2026-10-06``: ``pan_scenes.py`` / ``pan_sim.build_scene``) for the
120-fps phone-camera measurement of FALD "zone shimmer".

**The pixel rule — one rule for the TPG's HLSL, :func:`render_image_patch` and the study's renderer.** Content lives on a
GRID of ``S`` cells per screen pixel (S = 1: a full-resolution image; S = 2: the study's half-pixel canvas). Up to 4
:class:`ImageLayer` s (linear nits; grey / grey+alpha / RGB / RGBA, PREMULTIPLIED) are composited per cell bottom to top
over the surround ``bg`` (``c = c·(1 − a) + rgb``); MOVING layers are translated by an INTEGER number of cells per content
frame (:attr:`ImagePanScene.disp`); a screen pixel is the plain mean of its S × S cells (= exact area coverage of the
piecewise-constant canvas). That is exactly what the study renders: ``PanScene.window_full`` shifts its 2x canvas by
whole half pixels, composites ``stat·(1 − a) + mov`` per half-pixel cell and box-filters 2x2; ``ImageScene`` shifts the
4K frame by whole pixels and repeats its first column into the uncovered strip (= ``address="clamp"``). No bilinear /
Lanczos anywhere: a finer sub-pixel step needs a larger S. The TPG reads texels with ``Load()`` (no sampler: WARP
truncates bilinear fractions), so TPG == simulator up to the FP16 output rounding (tests/test_fald_motion_tpg.py).

Timing = :class:`dlc.fald.motion.Scene`'s: content frame k is held ``cadence[k % n]`` refreshes (2 = 23.976p at 47.952 Hz
2:2; 1 = a desktop pan at 60 Hz); ``base.pre`` / ``base.move`` / ``base.post`` place the sync patch's two flips (first
motion frame, first lead-out frame). Builders prepend ``lead_in`` / append ``lead_out`` still content frames;
TPG content frame k shows the source scene's content frame ``k − src_offset`` (clamped).

LCD safety: an image pan whose content frames are held an ODD number of refreshes can still toggle a pixel at refresh / 2
(e.g. a 2-px stripe panned 1 px per refresh) — :func:`dc_image_imbalance` checks every pixel's polarity balance and
:meth:`dlc.fald.motion_tpg.MotionTPG.load` refuses a polarity-locked scene (no override), as it refuses odd blinks.
Even holds (2:2) are balanced by construction.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .motion import PANEL_H, PANEL_W, Scene, render_full_patch

MAX_LAYERS = 4
MAX_GRID = 8
MAX_TEX = 16384
PAN_DIR_DEFAULT = Path(__file__).resolve().parents[3] / "results" / "fald_slowpan_2026-10-06"
DC_IMAGE_EVENT_REFRESHES = 8   # per play, net one-polarity drive allowed per pixel, in refreshes at its own peak ...
DC_IMAGE_BIAS = 0.20            # ... or this share of the play's refreshes (a polarity-locked full toggle = 0.5)


# ------------------------------------------------------------------------------------------------ scene description
@dataclass(frozen=True, eq=False)
class ImageLayer:
    """One image layer in GRID CELLS. ``data`` (h, w) grey, (h, w, 2) grey + alpha, (h, w, 3) RGB or (h, w, 4) RGBA —
    linear nits, premultiplied by alpha; no alpha = opaque. (``x``, ``y``) = the screen cell of ``data[0, 0]`` at zero
    displacement; ``moving`` layers follow the scene's per-frame ``disp``. ``address``: "border" = transparent outside
    the data, "clamp" = the edge cells extend over the whole screen. ``clip`` = a screen-fixed cell rect (x0, y0, x1, y1)
    outside which the layer is transparent (a viewport)."""
    data: np.ndarray
    x: int = 0
    y: int = 0
    moving: bool = True
    address: str = "border"
    clip: Optional[tuple[int, int, int, int]] = None

    def __post_init__(self):
        d = np.asarray(self.data)
        if d.ndim == 3 and d.shape[2] == 1:
            d = d[..., 0]
        if not (d.ndim == 2 or (d.ndim == 3 and d.shape[2] in (2, 3, 4))):
            raise ValueError(f"layer data must be (h, w) or (h, w, 2|3|4), got {d.shape}")
        if not (1 <= d.shape[0] <= MAX_TEX and 1 <= d.shape[1] <= MAX_TEX):
            raise ValueError(f"layer {d.shape[1]}x{d.shape[0]} cells exceeds {MAX_TEX}")
        if d.dtype not in (np.float16, np.float32):
            d = d.astype(np.float32)
        if not np.isfinite(d).all():
            raise ValueError("layer data must be finite")
        if self.address not in ("border", "clamp"):
            raise ValueError(f"address {self.address!r}: 'border' or 'clamp'")
        object.__setattr__(self, "data", d)
        object.__setattr__(self, "x", int(self.x)); object.__setattr__(self, "y", int(self.y))
        if self.clip is not None:
            object.__setattr__(self, "clip", tuple(int(v) for v in self.clip))

    @property
    def channels(self) -> int:
        return 1 if self.data.ndim == 2 else int(self.data.shape[2])

    @property
    def h(self) -> int:
        return int(self.data.shape[0])

    @property
    def w(self) -> int:
        return int(self.data.shape[1])

    def rgba(self, dtype=np.float64) -> tuple[np.ndarray, Optional[np.ndarray]]:
        """(rgb (h, w, 3), alpha (h, w) or None = opaque) as ``dtype``."""
        return _rgba(self.data, dtype)


def _rgba(d: np.ndarray, dtype) -> tuple[np.ndarray, Optional[np.ndarray]]:
    """Texels (h, w[, c]) -> (rgb (h, w, 3), alpha (h, w) or None) — the TPG's RGBA expansion."""
    d = d.astype(dtype)
    c = 1 if d.ndim == 2 else d.shape[2]
    if c == 1:
        return np.repeat((d if d.ndim == 2 else d[..., 0])[..., None], 3, axis=2), None
    if c == 2:
        return np.repeat(d[..., :1], 3, axis=2), d[..., 1]
    if c == 3:
        return d, None
    return d[..., :3], d[..., 3]


@dataclass(frozen=True, eq=False)
class ImagePanScene:
    """An image pan for the motion TPG. ``base`` = the analytic :class:`Scene` part (name, surround ``bg``, pre / move /
    post, ``cadence``, camera aids, optional analytic shapes drawn ON TOP of the image); ``layers`` bottom to top;
    ``disp`` = per content frame the (dx, dy) displacement of the moving layers in grid cells (len = ``base.frames``);
    ``grid`` = cells per screen pixel; ``src_offset`` = lead-in frames (TPG content k = source content k − src_offset)."""
    base: Scene
    layers: tuple[ImageLayer, ...]
    disp: tuple[tuple[int, int], ...]
    grid: int = 1
    src_offset: int = 0
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "layers", tuple(self.layers))
        object.__setattr__(self, "disp", tuple((int(a), int(b)) for a, b in self.disp))
        if not 1 <= len(self.layers) <= MAX_LAYERS:
            raise ValueError(f"1..{MAX_LAYERS} layers")
        if not 1 <= int(self.grid) <= MAX_GRID:
            raise ValueError(f"grid 1..{MAX_GRID}")
        if len(self.disp) != self.base.frames:
            raise ValueError(f"disp has {len(self.disp)} frames, base pre+move+post = {self.base.frames}")

    # Scene-like surface (MotionTPG / dc checks / the simulator side)
    @property
    def name(self) -> str:
        return self.base.name

    @property
    def frames(self) -> int:
        return self.base.frames

    @property
    def shapes(self) -> tuple:
        return self.base.shapes

    @property
    def cadence(self) -> tuple:
        return self.base.cadence

    def refreshes(self) -> list[int]:
        return self.base.refreshes()

    def src_frame(self, k: int) -> int:
        """The source scene's content frame shown at TPG content frame ``k``."""
        return int(k) - self.src_offset


# ------------------------------------------------------------------------------------------------ the reference renderer
def _span(lo: int, hi: int, a: int, b: int) -> tuple[int, int]:
    return max(lo, a), min(hi, b)


def _index(q0: int, n: int, size: int):
    """Texel indices q0 .. q0+n-1 clamped to [0, size): a slice when no clamping is needed (fast, a view)."""
    if q0 >= 0 and q0 + n <= size:
        return slice(q0, q0 + n)
    return np.clip(np.arange(q0, q0 + n), 0, size - 1)


def _composite_cells(scene: ImagePanScene, i: int, r0: int, r1: int, c0: int, c1: int, dtype, mono: bool) -> np.ndarray:
    """(r1 - r0, c1 - c0, 1 | 3) screen cells of content frame ``i``: the surround, then every layer bottom to top —
    the TPG's ``cellValue``. ``mono``: grey scenes only (1- / 2-channel layers, grey surround), one channel."""
    out = np.empty((r1 - r0, c1 - c0, 1 if mono else 3), dtype=dtype)
    out[:] = np.asarray(scene.base.bg[:1] if mono else scene.base.bg, dtype=dtype)
    for L in scene.layers:
        dx, dy = scene.disp[i] if L.moving else (0, 0)
        ox, oy = L.x + dx, L.y + dy                       # screen cell of texel (0, 0)
        ya, yb, xa, xb = r0, r1, c0, c1
        if L.address == "border":
            ya, yb = _span(ya, yb, oy, oy + L.h); xa, xb = _span(xa, xb, ox, ox + L.w)
        if L.clip is not None:
            ya, yb = _span(ya, yb, L.clip[1], L.clip[3]); xa, xb = _span(xa, xb, L.clip[0], L.clip[2])
        if yb <= ya or xb <= xa:
            continue
        iy, ix = _index(ya - oy, yb - ya, L.h), _index(xa - ox, xb - xa, L.w)
        if isinstance(iy, slice) or isinstance(ix, slice):
            sub = L.data[iy][:, ix] if not isinstance(iy, slice) else L.data[iy, ix]
        else:
            sub = L.data[np.ix_(iy, ix)]
        dst = out[ya - r0:yb - r0, xa - c0:xb - c0]
        ch = L.channels
        if mono:
            if ch > 2:
                raise ValueError("mono render of an RGB layer")
            v = (sub if ch == 1 else sub[..., 0]).astype(dtype)[..., None]
            a = None if ch == 1 else sub[..., 1].astype(dtype)[..., None]
        else:
            rgb, a = _rgba(sub, dtype)
            v = rgb
            a = None if a is None else a[..., None]
        if a is None:
            dst[:] = v
        else:
            dst *= (1.0 - a)
            dst += v
    return out


def _is_grey(scene: ImagePanScene) -> bool:
    bg = scene.base.bg
    return all(L.channels <= 2 for L in scene.layers) and bg[0] == bg[1] == bg[2]


def render_image_base(scene: ImagePanScene, i: int, x0: int, y0: int, nx: int, ny: int,
                      dtype=np.float64, mono: bool = False) -> np.ndarray:
    """(3, ny, nx) the IMAGE part of content frame ``i`` (surround + layers, no shapes / aids) over a full-resolution
    window — the TPG's ``imageValue`` (cells composited bottom to top, then the S × S mean). ``mono`` (grey scenes):
    (1, ny, nx)."""
    S = int(scene.grid)
    i = min(max(int(i), 0), scene.frames - 1)
    out = _composite_cells(scene, i, S * y0, S * (y0 + ny), S * x0, S * (x0 + nx), dtype, mono)
    img = out.reshape(ny, S, nx, S, out.shape[2]).mean(axis=(1, 3)) if S > 1 else out
    return np.ascontiguousarray(img.transpose(2, 0, 1))


def render_image_patch(scene: ImagePanScene, i: int, x0: int, y0: int, nx: int, ny: int) -> np.ndarray:
    """(3, ny, nx) linear nits of content frame ``i`` as the TPG draws it: the image, then the analytic shapes and the
    camera aids on top (:func:`dlc.fald.motion.render_full_patch`; the counters show ``i + 1`` here, the TPG's present
    number on screen)."""
    return render_full_patch(scene.base, i, x0, y0, nx, ny, base=render_image_base(scene, i, x0, y0, nx, ny))


# ------------------------------------------------------------------------------------------------ LCD polarity balance
def _moving_region(scene: ImagePanScene) -> Optional[tuple[int, int, int, int]]:
    """Screen px (x0, y0, x1, y1) where the image can change between frames (moving layers' reach), or None."""
    S = scene.grid
    box = None
    dxs = [d[0] for d in scene.disp]; dys = [d[1] for d in scene.disp]
    for L in scene.layers:
        if not L.moving or (min(dxs) == max(dxs) and min(dys) == max(dys)):
            continue
        if L.address == "clamp":
            b = (0, 0, S * PANEL_W, S * PANEL_H)
        else:
            b = (L.x + min(dxs), L.y + min(dys), L.x + max(dxs) + L.w, L.y + max(dys) + L.h)
        if L.clip is not None:
            b = (max(b[0], L.clip[0]), max(b[1], L.clip[1]), min(b[2], L.clip[2]), min(b[3], L.clip[3]))
        b = (max(b[0], 0), max(b[1], 0), min(b[2], S * PANEL_W), min(b[3], S * PANEL_H))
        if b[2] <= b[0] or b[3] <= b[1]:
            continue
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    if box is None:
        return None
    return box[0] // S, box[1] // S, -(-box[2] // S), -(-box[3] // S)


def dc_image_imbalance(scene: ImagePanScene, rows_per_chunk: int = 540) -> dict:
    """LCD polarity balance of the IMAGE over one play. Per pixel (brightest channel v, refresh r): D = Σ_r (−1)^r v_r —
    the net drive on one cell polarity. A content frame held an EVEN number of refreshes contributes 0, so even cadences
    (2:2) are balanced by construction; otherwise every pixel the moving layers reach is rendered.

    Allowance (per play, in refreshes at the pixel's own peak v_pk): max(DC_IMAGE_EVENT_REFRESHES, DC_IMAGE_BIAS · N).
    Moving detail biases a pixel INCOHERENTLY (each passing odd-width stroke adds ± one level): measured 2026-10-08 on
    the slow-pan study's set at hold 1 (+ 12 + 12 still frames), bias = |D| / (N · v_pk) at the worst pixel: desktop /
    city at 0.5 px per refresh 0.005, desktop_h_v1 0.042, city_h_v1 0.017, horizon_h_v1 0.017, scroll_v_v1 0.048, the
    2–4 px per refresh text pans 0.10–0.115, the anime frame at hold 1 0.096. A polarity-LOCKED toggle (a 2-px stripe at
    1 px per refresh, a 4-px stripe at 2, a 6-px one at 3) reaches 0.5 × its contrast: refused from ~40 % contrast up
    (a weaker toggle on a bright field passes — a known limit of a per-pixel rule). ``excess`` = max over pixels of
    |D| / allowance (> 1 = refused by MotionTPG.load); ``worst`` = its pixel (x, y); ``bias`` = |D| / (N · v_pk) there."""
    reps = scene.refreshes()
    sign, r = [], 0
    for n in reps:
        sign.append(0 if n % 2 == 0 else (1 if r % 2 == 0 else -1))
        r += n
    N = r
    if not any(sign):
        return {"excess": 0.0, "worst": None, "bias": 0.0, "checked": "even holds"}
    reg = _moving_region(scene)
    if reg is None:
        return {"excess": 0.0, "worst": None, "bias": 0.0, "checked": "static image"}
    x0, y0, x1, y1 = reg
    mono = _is_grey(scene)
    allow_refreshes = max(DC_IMAGE_EVENT_REFRESHES, DC_IMAGE_BIAS * N)
    best = (0.0, None, 0.0)
    for ya in range(y0, y1, rows_per_chunk):
        yb = min(ya + rows_per_chunk, y1)
        D = np.zeros((yb - ya, x1 - x0)); vpk = np.zeros_like(D)
        for k, sk in enumerate(sign):
            v = render_image_base(scene, k, x0, ya, x1 - x0, yb - ya, dtype=np.float32, mono=mono).max(axis=0)
            if sk:
                D += sk * v
            np.maximum(vpk, v, out=vpk)
        ex = np.abs(D) / np.maximum(allow_refreshes * vpk, 1e-6)
        j = int(ex.argmax())
        if ex.flat[j] > best[0]:
            best = (float(ex.flat[j]), (x0 + j % ex.shape[1], ya + j // ex.shape[1]),
                    float(abs(D.flat[j]) / max(N * vpk.flat[j], 1e-6)))
    return {"excess": best[0], "worst": best[1], "bias": best[2],
            "checked": f"pixels x {x0}-{x1} y {y0}-{y1}, {N} refreshes"}


# ------------------------------------------------------------------------------------------------ the TPG's files
def _layer_dtype(d: np.ndarray) -> str:
    if d.dtype == np.float16:
        return "f16"
    with np.errstate(over="ignore"):
        lossless = np.array_equal(d.astype(np.float16).astype(np.float32), d.astype(np.float32))
    return "f16" if lossless else "f32"


def write_tpg_files(scene: ImagePanScene, directory: Path) -> Path:
    """Write the TPG's scene file + one raw little-endian layer file per layer into ``directory``; returns the scene
    file path (``load <path>``). Layers are stored losslessly (float16 when exact, else float32)."""
    from .motion_tpg import scene_text   # (motion_tpg imports this module lazily)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = scene_text(scene.base)
    stem = base.split("\nname ", 1)[1].split("\n", 1)[0]
    lines = [base.rstrip("\n"), f"grid {int(scene.grid)}"]
    big = 1 << 30
    for k, L in enumerate(scene.layers):
        dt = _layer_dtype(L.data)
        path = (directory / f"{stem}_L{k}.{dt}").resolve()
        L.data.astype("<f2" if dt == "f16" else "<f4").tofile(path)
        cx0, cy0, cx1, cy1 = L.clip if L.clip is not None else (-big, -big, big, big)
        lines.append(f"layer {L.w} {L.h} {L.channels} {dt} {L.x} {L.y} {int(bool(L.moving))} {L.address} "
                     f"{cx0} {cy0} {cx1} {cy1} {path}")
    for a in range(0, len(scene.disp), 32):
        lines.append("disp " + " ".join(f"{dx} {dy}" for dx, dy in scene.disp[a:a + 32]))
    out = directory / f"{stem}.scene"
    out.write_text("\n".join(lines) + "\n", encoding="ascii")
    return out


# ------------------------------------------------------------------------------------------------ builders
def _aids(aids, object_y: float) -> dict:
    if aids is True:
        from .motion_scenes import camera_aids
        return camera_aids(object_y)
    return dict(aids) if aids else {}


def _assemble(name, layers, src_disp, grid, hold, src_pre, lead_in, lead_out, bg, aids, object_y, note, shapes=(),
              meta=None) -> ImagePanScene:
    n = len(src_disp)
    disp = [src_disp[0]] * int(lead_in) + list(src_disp) + [src_disp[-1]] * int(lead_out)
    base = Scene(name, tuple(float(v) for v in bg), tuple(shapes), pre=int(lead_in) + int(src_pre),
                 move=n - int(src_pre), post=int(lead_out), cadence=tuple(int(h) for h in np.atleast_1d(hold)),
                 note=note, **_aids(aids, object_y))
    return ImagePanScene(base, tuple(layers), tuple(disp), grid=int(grid), src_offset=int(lead_in),
                         meta=dict(meta or {}))


def from_array(nits: np.ndarray, name: str, *, n_move: int, velocity: Optional[tuple[float, float]] = None,
               steps: Optional[Sequence[tuple[float, float]]] = None, grid: int = 1, at: tuple[float, float] = (0, 0),
               hold=1, pre: int = 0, lead_in: int = 0, lead_out: int = 0, address: str = "border",
               clip_px: Optional[tuple[int, int, int, int]] = None, bg=(0.0, 0.0, 0.0), aids=True,
               object_y: float = 1102.5, note: str = "") -> ImagePanScene:
    """A pan of ``nits`` — (h, w) / (h, w, 3) linear nits on the ``grid`` (S cells per screen px: an (h, w) array at
    grid 2 covers w/2 × h/2 screen px). Its top-left sits at screen px ``at`` (multiples of 1/S); ``pre`` still content
    frames, then ``n_move`` frames each displaced by ``velocity`` px (constant) or by ``steps`` (a cycled per-frame px
    pattern, e.g. [(10, 0), (10, 0), (15, 0)]); every displacement must be a whole number of cells (1/S px) — the
    study's pans are; ``hold`` = refreshes per content frame (or a cadence tuple). ``address`` "clamp" repeats the edge
    pixels over the screen (pan_scenes.ImageScene's convention), "border" shows ``bg`` around it; ``clip_px`` = a
    screen viewport (x0, y0, x1, y1)."""
    S = int(grid)
    if (velocity is None) == (steps is None):
        raise ValueError("give exactly one of velocity / steps")
    pat = [tuple(velocity)] if velocity is not None else [tuple(s) for s in steps]
    cells = []
    for sx, sy in pat:
        cx, cy = sx * S, sy * S
        if abs(cx - round(cx)) > 1e-9 or abs(cy - round(cy)) > 1e-9:
            raise ValueError(f"step {(sx, sy)} px is not a whole number of 1/{S}-px cells (raise grid)")
        cells.append((int(round(cx)), int(round(cy))))
    src, cur = [], (0, 0)
    for k in range(int(pre) + int(n_move)):
        m = k - int(pre)
        if m > 0:
            st = cells[(m - 1) % len(cells)]
            cur = (cur[0] + st[0], cur[1] + st[1])
        src.append(cur)
    ax, ay = at[0] * S, at[1] * S
    if abs(ax - round(ax)) > 1e-9 or abs(ay - round(ay)) > 1e-9:
        raise ValueError("`at` must be a whole number of cells")
    clip = None if clip_px is None else tuple(int(v) * S for v in clip_px)
    layer = ImageLayer(np.asarray(nits), int(round(ax)), int(round(ay)), True, address, clip)
    return _assemble(name, (layer,), src, S, hold, pre, lead_in, lead_out, bg, aids, object_y, note)


def from_pan_scene(ps, *, lead_in: int = 0, lead_out: int = 0, hold=None, aids=True, name: Optional[str] = None,
                   bg=None) -> ImagePanScene:
    """The TPG twin of a slow-pan study scene (duck-typed: ``pan_scenes.PanScene`` — viewport pans on the 2x canvas,
    one content frame per refresh — or ``pan_scenes.ImageScene`` — the owner's 4K frame, integer px steps, ``hold``
    refreshes per content frame). TPG content frame k = the study's content frame ``k − lead_in`` (PanScene: its
    refresh index; ImageScene: ``content_of(r)``)."""
    if hasattr(ps, "mov"):                                            # PanScene (2x canvas, grid 2)
        X0, Y0, X1, Y1 = (int(v) for v in ps.meta()["view"])
        view = (2 * X0, 2 * Y0, 2 * X1, 2 * Y1)
        layers = []
        rows = np.asarray(ps.frame_rows, dtype=np.float64)
        if bg is None:
            bg = (float(rows[0]),) * 3
        if not np.all(rows == rows[0]) or float(rows[0]) != float(bg[0]):   # a per-row frame (the horizon's sky)
            layers.append(ImageLayer(np.repeat(rows.astype(np.float32), 2)[:, None], 0, 0, False, "clamp"))
        if ps.stat is not None:
            layers.append(ImageLayer(np.asarray(ps.stat, np.float32), view[0], view[1], False, "border", view))
        mov = np.asarray(ps.mov, np.float32)
        data = mov if ps.mov_a is None else np.stack([mov, np.asarray(ps.mov_a, np.float32)], axis=2)
        ox, oy = (int(round(v)) for v in ps.origin)
        layers.append(ImageLayer(data, 2 * ox, 2 * oy, True, "border", view))
        src = [tuple(int(v) for v in ps.disp_half(i)) for i in range(ps.frames)]
        h = 1 if hold is None else hold
        object_y = 0.5 * (ps.meta()["eval"][1] + ps.meta()["eval"][3])
        return _assemble(name or ps.name, layers, src, 2, h, ps.pre, lead_in, lead_out, bg, aids, object_y,
                         f"TPG twin of pan_scenes {ps.name}: {ps.note}", meta=ps.meta())
    if hasattr(ps, "dx_c"):                                           # ImageScene (full frame, grid 1)
        im = np.load(ps.path)
        if im.ndim != 3 or im.shape[2] != 3:
            raise ValueError(f"{ps.path}: want (h, w, 3) nits, got {im.shape}")
        n = ps.pre_c + ps.warm_c + ps.eval_c
        src = [(int(ps.dx_c(c)), 0) for c in range(n)]
        layer = ImageLayer(im, 0, 0, True, "clamp")
        h = ps.hold if hold is None else hold
        object_y = 0.5 * (ps.eval_rect[1] + ps.eval_rect[3])
        return _assemble(name or ps.name, (layer,), src, 1, h, ps.pre_c, lead_in, lead_out, bg or (0.0, 0.0, 0.0),
                         aids, object_y, f"TPG twin of pan_scenes ImageScene {ps.name}: {ps.note}", meta=ps.meta())
    raise TypeError(f"not a pan_scenes scene: {type(ps).__name__}")


def load_pan_scene(name: str, pan_dir: Optional[Path] = None):
    """``pan_sim.build_scene(name)`` from the study's directory (local results, not on main)."""
    d = Path(pan_dir) if pan_dir else PAN_DIR_DEFAULT
    if not (d / "pan_sim.py").exists():
        raise FileNotFoundError(f"{d / 'pan_sim.py'} not found (the slow-pan study lives in local results/)")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    import pan_sim   # noqa: E402  (its own imports put src/ and the study dir on sys.path)
    return pan_sim.build_scene(name)


def pan_scene(name: str, *, pan_dir: Optional[Path] = None, **kw) -> ImagePanScene:
    """:func:`from_pan_scene` of ``pan_sim.build_scene(name)`` (e.g. "anime_pan_2to2_r5", "desktop_h_v1",
    "desktop_h_v0.5", "city_h_v1")."""
    return from_pan_scene(load_pan_scene(name, pan_dir), **kw)


# ------------------------------------------------------------------------------------------------ CLI
def _ints(s: str) -> tuple[int, ...]:
    return tuple(int(v) for v in s.split(","))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dlc.fald.motion_image", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=("check", "write", "show"),
                    help="check: summary + LCD balance + WARP parity (no monitor); write: TPG files to --out; "
                         "show: HARDWARE — play it full-screen on --rect (the owner's call)")
    ap.add_argument("--pan", help="a slow-pan study scene (pan_sim.build_scene name)")
    ap.add_argument("--pan-dir", type=Path, default=None)
    ap.add_argument("--npy", type=Path, help="an (h, w[, 3]) linear-nits array to pan instead (with --step / --move)")
    ap.add_argument("--grid", type=int, default=1)
    ap.add_argument("--at", default="0,0", help="screen px of the array's top-left")
    ap.add_argument("--step", default=None, help="px per content frame: 'dx,dy' or a cycled pattern 'dx,dy;dx,dy;...'")
    ap.add_argument("--move", type=int, default=0, help="moving content frames (--npy)")
    ap.add_argument("--pre", type=int, default=0, help="still content frames before the motion (--npy)")
    ap.add_argument("--address", default="clamp", choices=("clamp", "border"))
    ap.add_argument("--hold", type=int, default=None, help="refreshes per content frame (default: the scene's)")
    ap.add_argument("--lead-in", type=int, default=0)
    ap.add_argument("--lead-out", type=int, default=0)
    ap.add_argument("--no-aids", action="store_true")
    ap.add_argument("--frames", default=None, help="check: content frames to WARP-render (default: 4 incl. sub-px)")
    ap.add_argument("--crop", default=None, help="check: x,y,w,h screen window (default 640x360 at the eval region)")
    ap.add_argument("--out", type=Path, default=None, help="write: directory")
    ap.add_argument("--rect", default="0,0,3840,2160", help="show: monitor rect x,y,w,h (physical px)")
    ap.add_argument("--log", type=Path, default=None, help="show: present log CSV")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--park", type=float, default=2.0)
    a = ap.parse_args(argv)

    aids = not a.no_aids
    if a.pan:
        sc = pan_scene(a.pan, pan_dir=a.pan_dir, lead_in=a.lead_in, lead_out=a.lead_out, hold=a.hold, aids=aids)
    elif a.npy:
        if not a.step:
            ap.error("--npy needs --step")
        pat = [tuple(float(v) for v in p.split(",")) for p in a.step.split(";")]
        at = tuple(float(v) for v in a.at.split(","))
        sc = from_array(np.load(a.npy), a.npy.stem, n_move=a.move, steps=pat, grid=a.grid, at=at, hold=a.hold or 1,
                        pre=a.pre, lead_in=a.lead_in, lead_out=a.lead_out, address=a.address, aids=aids)
    else:
        ap.error("give --pan or --npy")
    reps = sc.refreshes()
    dx = [d[0] for d in sc.disp]; dy = [d[1] for d in sc.disp]
    print(f"{sc.name}: {sc.frames} content frames = {sum(reps)} refreshes (cadence {sc.cadence}), grid {sc.grid}, "
          f"{len(sc.layers)} layer(s), disp x {min(dx)}..{max(dx)} y {min(dy)}..{max(dy)} cells, "
          f"motion frames {sc.base.pre}..{sc.base.pre + sc.base.move - 1}, src_offset {sc.src_offset}")
    if a.action == "write":
        if not a.out:
            ap.error("write needs --out")
        print(write_tpg_files(sc, a.out))
        return 0
    if a.action == "check":
        from .motion_tpg import EXE_DEFAULT, render_offscreen
        dc = dc_image_imbalance(sc)
        print(f"LCD polarity balance: excess {dc['excess']:.3f} (> 1 refused) worst {dc['worst']} [{dc['checked']}]")
        if not EXE_DEFAULT.exists():
            print("motion_tpg.exe not built: no WARP parity")
            return 0
        if a.frames:
            frames = [int(v) for v in a.frames.split(",")]
        else:
            odd = [k for k, d in enumerate(sc.disp) if d[0] % sc.grid or d[1] % sc.grid]
            mv = sc.base.pre
            frames = sorted({0, min(mv + 1, sc.frames - 1), (odd[len(odd) // 2] if odd else (mv + sc.frames) // 2),
                             sc.frames - 1})
        if a.crop:
            x, y, w, h = _ints(a.crop)
        else:
            ev = sc.meta.get("eval") or (1600, 900, 2240, 1260)
            x, y, w, h = int(ev[0]), int(ev[1]), 640, 360
        got = render_offscreen(sc, frames, (w, h), origin=(x, y))
        for k in frames:
            want = render_image_patch(sc, k, x, y, w, h)
            err = np.abs(got[k] - want) / np.maximum(want, 0.05)
            print(f"  frame {k} (disp {sc.disp[k]} cells): max rel err {err.max():.2e}  mean {want.mean():.2f} nit")
        return 0
    # show — HARDWARE
    from .motion_tpg import MotionTPG, infer_refreshes, presented_schedule, read_present_log
    if a.log is None:
        ap.error("show needs --log")
    with MotionTPG(rect=_ints(a.rect), log_path=a.log, park_nits=a.park) as tpg:
        print(tpg.load(sc))
        first, last = tpg.play(a.cycles)
        tpg.park()
    pres, _ = read_present_log(a.log)
    infer_refreshes(pres)
    s = presented_schedule(pres)
    print(f"presents {first}..{last}: slips {len(s['slips'])}, refresh resolved {s['resolved']:.3f}")
    return 0


if __name__ == "__main__":     # run through the package module: its classes are the ones isinstance() checks see
    from dlc.fald.motion_image import main as _main
    raise SystemExit(_main())
