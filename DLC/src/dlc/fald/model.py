"""FALD forward model (numpy). Pattern (list of PQ-coded rectangles) → per-channel luminance
field at reduced resolution → meter reading (aperture average).

Pipeline (all stages parameterised by :class:`FaldParams`, nothing panel-specific in code):

1. **LDA input** ``s(p)``: requested linear nits of the brightest channel (PQ-decoded), clipped to
   ``white_nits``. The dimming algorithm was found to key on the max channel, not luminance.
2. **Cell drive** ``d_c = drive_curve(max over the cell of box-blur(s, blur_px))`` — a saturating
   max over a blurred image (features smaller than the footprint count partially).
3. **Backlight** ``B_true = Σ_c d_c K_true(|p − c|)`` with ``K_true`` an isotropic exponential in
   millimetres, normalised so a fully driven full field gives ``B = 1``. The monitor's own
   estimate ``B_est`` uses ``K_est`` (a different, fittable kernel) — the mismatch is the
   compensation error (bright/dark rings).
4. **LCD**: the monitor requests transmittance ``T = min(1, target / (Lmax · B_est))`` and the
   panel emits ``Lmax · B_true · T`` plus the leak pedestal ``Lmax · B_true · Tmin``.
5. **Meter**: mean over a disc of radius ``aperture_px`` at the meter spot.

Resolution: the frame is rendered at ``1/scale`` (default 1/5 → 768×432, integer cells for the
80×45 px grid); the backlight is evaluated on a ``sub``×``sub`` per-cell grid via FFT
convolution and bilinearly upsampled (it is smooth at pixel scale).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from scipy.signal import fftconvolve

from dlc._pq import eotf_norm

Shape = tuple[tuple[int, int, int], tuple[float, float, float, float]]


@dataclass
class FaldParams:
    # geometry (px, full resolution)
    width: int = 3840
    height: int = 3840 * 9 // 16
    cols: int = 48
    rows: int = 48
    px_mm: float = 0.1845
    # LDA
    blur_px: float = 32.0                 # statistic footprint (box)
    drive_curve: Sequence[tuple[float, float]] = field(default_factory=lambda: [
        (10.0, 0.0), (30.0, 0.17), (100.0, 0.24), (300.0, 0.47), (600.0, 0.73), (1000.0, 1.0)])
    drive_floor_nits: float = 0.5         # below this a cell is off (black)
    drive_min_gain: float = 1.1           # a cell always drives ≥ gain·L/white (LCD can't exceed 100 %)
    # spread: real kernel = (1−tail_frac)·exp(−d/core_mm) + tail_frac·exp(−d/tail_mm), isotropic in mm
    core_mm: float = 6.0
    tail_mm: float = 24.0
    tail_frac: float = 0.3
    est_kind: str = "exp"                 # monitor's assumed kernel: "exp" | "gauss" | "mix"
    est_scale_mm: float = 24.0            # its 1/e length (exp) or sigma (gauss)
    est_core_mm: float = 6.0              # "mix": own core / tail / fraction
    est_tail_mm: float = 24.0
    est_tail_frac: float = 0.3
    est_phase_px: float = 0.0             # the monitor samples its estimate at (p + phase): a grid-phase
                                          # error in the compensation map (model-free analysis 2026-09-10
                                          # found ≈ −40 px, i.e. half a cell to the left)
    est_phase_py: float = 0.0
    # panel
    white_nits: float = 1040.0            # white at full drive, full transmittance (as measured)
    chan_weights: tuple[float, float, float] = (0.305, 0.596, 0.099)   # R,G,B share of white
    tmin: float = 3.0e-4                  # closed-LCD transmittance (pedestal = Lmax·B·tmin)
    # meter
    aperture_px: float = 80.0
    drive_dim: float = 0.0                # relative drive at the dimmest curve point (fitted; a zero
                                          # meter read only bounds it, see review 2026-09-10 #6)
    # rendering
    scale: int = 5
    sub: int = 8                          # per-cell backlight samples per axis (4 under-resolved the core)

    @property
    def cell_w(self) -> float:
        return self.width / self.cols

    @property
    def cell_h(self) -> float:
        return self.height / self.rows


def _eotf_nits(code: np.ndarray, bits: int = 10) -> np.ndarray:
    v = np.vectorize(eotf_norm)(np.clip(code / ((1 << bits) - 1), 0.0, 1.0))
    return v * 10000.0


class FaldModel:
    def __init__(self, p: FaldParams):
        self.p = p
        self.w = p.width // p.scale
        self.h = p.height // p.scale
        self.cw = self.w / p.cols
        self.ch = self.h / p.rows
        assert abs(self.cw - round(self.cw)) < 1e-9 and abs(self.ch - round(self.ch)) < 1e-9, \
            "choose a scale giving integer reduced-res cells"
        self.cw, self.ch = int(round(self.cw)), int(round(self.ch))
        self._kern_cache: dict = {}

    # ------------------------------------------------------------------ pattern → targets
    def render(self, shapes: Sequence[Shape]) -> np.ndarray:
        """Per-channel requested linear nits, shape (3, h, w). Shapes paint in order."""
        img = np.zeros((3, self.h, self.w), dtype=np.float64)
        for (r, g, b), (x, y, cx, cy) in shapes:
            x0 = int(round(x * self.w)); y0 = int(round(y * self.h))
            x1 = int(round((x + cx) * self.w)); y1 = int(round((y + cy) * self.h))
            x1 = max(x1, x0 + 1); y1 = max(y1, y0 + 1)
            vals = _eotf_nits(np.array([r, g, b], dtype=np.float64))
            img[:, y0:y1, x0:x1] = vals[:, None, None]
        return img

    # ------------------------------------------------------------------ LDA
    def _box_blur(self, a: np.ndarray, k: int) -> np.ndarray:
        if k <= 1:
            return a
        pad = k // 2
        ap = np.pad(a, pad, mode="edge")
        c = np.cumsum(np.cumsum(ap, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        h, w = a.shape
        out = (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / float(k * k)
        return out

    def drive_of(self, s_nits: np.ndarray) -> np.ndarray:
        xs = np.array([n for n, _ in self.p.drive_curve]); ys = np.array([d for _, d in self.p.drive_curve], dtype=float)
        ys[0] = max(ys[0], self.p.drive_dim)                  # dim end is a fitted parameter
        lx = np.log(np.maximum(s_nits, 1e-3))
        d = np.interp(lx, np.log(xs), ys, left=0.0, right=ys[-1])
        d = np.maximum(d, self.p.drive_min_gain * s_nits / self.p.white_nits)   # LCD can't exceed 100 %
        d = np.where(s_nits < self.p.drive_floor_nits, 0.0, np.minimum(d, 1.0))
        return d

    def cell_drives(self, img: np.ndarray) -> np.ndarray:
        """Per-cell statistic = max over sliding windows (footprint ``blur_px``) that lie INSIDE
        the cell of the window mean — features smaller than the footprint count partially, and
        nothing leaks across a cell boundary (HW: a window whose edge sits exactly on a boundary
        does not drive the next cell at all)."""
        p = self.p
        s = np.max(img, axis=0)                               # brightest channel, requested nits
        s = np.minimum(s, p.white_nits)
        cells = s.reshape(p.rows, self.ch, p.cols, self.cw).transpose(0, 2, 1, 3)   # (R, C, ch, cw)
        # integral image per cell with a zero border
        S = np.zeros((p.rows, p.cols, self.ch + 1, self.cw + 1))
        S[:, :, 1:, 1:] = np.cumsum(np.cumsum(cells, axis=2), axis=3)

        def stat(k: int) -> np.ndarray:
            kh = int(np.clip(k, 1, self.ch)); kw = int(np.clip(k, 1, self.cw))
            best = np.full((p.rows, p.cols), -np.inf)
            for i in range(self.ch - kh + 1):
                for j in range(self.cw - kw + 1):
                    m = (S[:, :, i + kh, j + kw] - S[:, :, i, j + kw] - S[:, :, i + kh, j] + S[:, :, i, j]) / float(kh * kw)
                    best = np.maximum(best, m)
            return best

        # fractional footprint: blend the two integer window sizes so blur_px has a gradient
        kf = p.blur_px / p.scale
        k0 = int(np.floor(kf)); f = kf - k0
        best = stat(k0) if f < 1e-6 else (1.0 - f) * stat(k0) + f * stat(k0 + 1)
        return self.drive_of(best)

    # ------------------------------------------------------------------ spread
    def _kernels(self, kind: str, scale_mm: float, core_mm: float = 0.0, tail_frac: float = 0.0,
                 phase_mm: tuple[float, float] = (0.0, 0.0)):
        """Per-sub-offset kernels. ``kind``: "exp" (1/e = scale_mm), "gauss" (sigma = scale_mm),
        "mix" ((1−tail_frac)·exp(−d/core_mm) + tail_frac·exp(−d/scale_mm)). ``phase_mm`` shifts
        the SAMPLE point: the field is evaluated at (p + phase) and attributed to p."""
        key = (kind, round(scale_mm, 4), round(core_mm, 4), round(tail_frac, 5),
               round(phase_mm[0], 4), round(phase_mm[1], 4))
        if key in self._kern_cache:
            return self._kern_cache[key]
        p = self.p
        sub = p.sub
        cwmm, chmm = p.cell_w * p.px_mm, p.cell_h * p.px_mm
        reach_c = int(np.ceil(7 * scale_mm / cwmm)) + 1
        reach_r = int(np.ceil(7 * scale_mm / chmm)) + 1
        ii = np.arange(-reach_c, reach_c + 1)
        jj = np.arange(-reach_r, reach_r + 1)
        kern = []
        for oy in range(sub):
            row = []
            for ox in range(sub):
                # fftconvolve: out[k] = Σ_i d[k−i]·kern[i]  ⇒  kern index i is the SAMPLE cell minus
                # the SOURCE cell. Sample point sits (ox+0.5)/sub of a cell from its cell's left/top
                # edge, the source at its cell centre: |source − sample| = |−i + 0.5 − (ox+0.5)/sub|.
                # (Review 2026-09-10 caught the mirrored sign — B rose away from a lit cell.)
                # dx as coded = sample − source (only its square is used); a sample shifted by
                # +phase adds phase to it.
                dx = (ii[None, :] + (ox + 0.5) / sub - 0.5) * cwmm + phase_mm[0]
                dy = (jj[:, None] + (oy + 0.5) / sub - 0.5) * chmm + phase_mm[1]
                d = np.sqrt(dx * dx + dy * dy)
                if kind == "exp":
                    k = np.exp(-d / scale_mm)
                elif kind == "gauss":
                    k = np.exp(-0.5 * (d / scale_mm) ** 2)
                elif kind == "mix":
                    core = np.exp(-d / max(core_mm, 1e-3)); tail = np.exp(-d / scale_mm)
                    # each component normalised to unit sum before mixing so tail_frac is an ENERGY share
                    k = (1.0 - tail_frac) * core / core.sum() + tail_frac * tail / tail.sum()
                else:
                    raise ValueError(kind)
                row.append(k)
            kern.append(row)
        # normalise: a fully driven infinite field must give B = 1 at any sample point
        norm = np.mean([[k.sum() for k in row] for row in kern])
        kern = [[k / norm for k in row] for row in kern]
        self._kern_cache[key] = kern
        return kern

    def backlight(self, drives: np.ndarray, kind: str, scale_mm: float,
                  core_mm: float = 0.0, tail_frac: float = 0.0,
                  phase_px: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
        """B on the reduced-res pixel grid (h, w), from cell drives (rows, cols)."""
        p = self.p
        kern = self._kernels(kind, scale_mm, core_mm, tail_frac,
                             (phase_px[0] * p.px_mm, phase_px[1] * p.px_mm))
        sub = p.sub
        fine = np.zeros((p.rows * sub, p.cols * sub))
        for oy in range(sub):
            for ox in range(sub):
                fine[oy::sub, ox::sub] = fftconvolve(drives, kern[oy][ox], mode="same")
        # bilinear upsample fine (sub per cell) → pixels
        ys = (np.arange(self.h) + 0.5) / self.ch * sub - 0.5
        xs = (np.arange(self.w) + 0.5) / self.cw * sub - 0.5
        return _bilinear(fine, ys, xs)

    # ------------------------------------------------------------------ full forward
    def forward(self, shapes: Sequence[Shape]) -> dict:
        p = self.p
        img = self.render(shapes)
        drives = self.cell_drives(img)
        b_true = self.backlight(drives, "mix", p.tail_mm, p.core_mm, p.tail_frac)
        phase = (p.est_phase_px, p.est_phase_py)
        if p.est_kind == "mix":
            b_est = self.backlight(drives, "mix", p.est_tail_mm, p.est_core_mm, p.est_tail_frac, phase)
        else:
            b_est = self.backlight(drives, p.est_kind, p.est_scale_mm, phase_px=phase)
        lmax = p.white_nits * np.array(p.chan_weights)[:, None, None]
        # per-channel target luminance: a code's PQ decode is its "as-if-white" nits, the channel
        # contributes its share of white → target_ch = w_ch · EOTF(code_ch)
        target = img * np.array(p.chan_weights)[:, None, None]
        # monitor's request: T = target / (Lmax · B_est), clamped to [0, 1]
        t_req = target / np.maximum(lmax * np.maximum(b_est, 1e-6)[None], 1e-9)
        t = np.clip(t_req, 0.0, 1.0)
        y = lmax * b_true[None] * t + lmax * b_true[None] * p.tmin
        return {"img": img, "drives": drives, "b_true": b_true, "b_est": b_est, "t": t, "y": y}

    def meter(self, shapes: Sequence[Shape], meter_px: tuple[float, float],
              aperture_px: Optional[float] = None) -> np.ndarray:
        """Per-channel luminance the meter reads: mean of y over the aperture disc. Returns (3,)."""
        out = self.forward(shapes)
        r = (aperture_px if aperture_px is not None else self.p.aperture_px) / self.p.scale
        mx, my = meter_px[0] / self.p.scale, meter_px[1] / self.p.scale
        yy, xx = np.mgrid[0:self.h, 0:self.w]
        mask = ((xx + 0.5 - mx) ** 2 + (yy + 0.5 - my) ** 2) <= r * r
        return out["y"][:, mask].mean(axis=1)

    def meter_y(self, shapes, meter_px, aperture_px=None) -> float:
        return float(self.meter(shapes, meter_px, aperture_px).sum())


def _bilinear(a: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    h, w = a.shape
    y0 = np.clip(np.floor(ys).astype(int), 0, h - 1); y1 = np.clip(y0 + 1, 0, h - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, w - 1); x1 = np.clip(x0 + 1, 0, w - 1)
    fy = np.clip(ys - y0, 0, 1)[:, None]; fx = np.clip(xs - x0, 0, 1)[None, :]
    return (a[y0][:, x0] * (1 - fy) * (1 - fx) + a[y0][:, x1] * (1 - fy) * fx
            + a[y1][:, x0] * fy * (1 - fx) + a[y1][:, x1] * fy * fx)
