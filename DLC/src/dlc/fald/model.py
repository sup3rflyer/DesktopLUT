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

from dlc._pq import eotf_norm, oetf_norm

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
    # statistic kind: "winmax" (max of in-cell sliding-window means, the original) or
    # "area_switch" (the single-cell coverage law fitted 2026-09-10 on all coverage data — harness
    # hyp_e2, 5 % RMS — re-expressed so a uniform field drives exactly the measured curve; see
    # FaldModel._drives_area_switch). Content in the cell's bottom-left quadrant (and the neighbours
    # left/below, the SUPPRESSION box sup_box in px from the cell's top-left) pulls the LED down by
    # up to sup_strength; a fully lit cell in a lit field then sits at stat_cap·(1−sup_strength) = 0.76
    # of the LED maximum and an unsuppressed sliver can reach 1/0.76 = 1.32× the field level (HW 1.30).
    stat_kind: str = "winmax"
    stat_window: bool = False             # area_switch ceiling = winmax window mean instead of the peak px
    stat_area0_px2: float = 1199.0        # area law: stat = mean(lit px) × min(1, lit area / A0)   (fit 2026-09-11)
    stat_cap: float = 1.114               # LED headroom above the measured curve (harness "cap")
    sup_strength: float = 0.319
    sup_box: tuple[float, float, float, float] = (-40.0, 40.0, 20.0, 67.5)   # x0, x1, y0, y1 (px)
    sup_window_px: float = 10.0
    # coarse sample lattice + peak limiter (HW 2026-09-11, doc §20): the firmware samples the requested
    # CODE at pixels (lattice_px·k + phase); a cell whose lattice pixel carries near-peak code is held at
    # the normal (full-white) drive, otherwise the LED may use lattice_boost headroom (PA32UCXR: 0.30,
    # i.e. an isolated white half-cell drives 1.30× the whole cell). lattice_boost = 0 disables it.
    lattice_boost: float = 0.0
    lattice_px: float = 48.0
    lattice_phase_px: tuple[float, float] = (0.0, 0.0)
    lattice_ramp: Sequence[tuple[float, float]] = field(default_factory=lambda: [   # (10-bit code, weight)
        (800.0, 0.0), (850.0, 0.23), (900.0, 0.62), (950.0, 0.79), (1000.0, 0.95), (1023.0, 1.0)])
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
    est_aniso: float = 1.0                # vertical/horizontal scale of the ESTIMATE kernel (1 = isotropic
                                          # in mm; 45/80 = isotropic in cells → shorter vertical reach)
    est_support_cells: int = 0            # >0: the estimate only sums cells within ±N cells in each axis
                                          # (a box support in CELL units: 4 cells = 320 px wide, 180 px tall)
    # panel
    white_nits: float = 1040.0            # white at full drive, full transmittance (as measured)
    chan_weights: tuple[float, float, float] = (0.305, 0.596, 0.099)   # R,G,B share of white
    tmin: float = 3.0e-4                  # closed-LCD transmittance (pedestal = Lmax·B·tmin)
    # meter
    aperture_px: float = 80.0
    drive_dim: float = 0.0                # relative drive at the dimmest curve point (fitted; a zero
                                          # meter read only bounds it, see review 2026-09-10 #6)
    drive_gamma: float = 0.5              # power-law continuation below the first curve point
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
        d = np.interp(lx, np.log(xs), ys, left=ys[0], right=ys[-1])
        # below the first curve point: power-law continuation (model-free analysis 2026-09-10:
        # the field's own drive scales ≈ L^0.5 down to 2 nits, it does not collapse)
        below = s_nits < xs[0]
        d = np.where(below, ys[0] * (np.maximum(s_nits, 1e-3) / xs[0]) ** self.p.drive_gamma, d)
        d = np.maximum(d, self.p.drive_min_gain * s_nits / self.p.white_nits)   # LCD can't exceed 100 %
        d = np.where(s_nits < self.p.drive_floor_nits, 0.0, np.minimum(d, 1.0))
        return d

    def cell_drives(self, img: np.ndarray) -> np.ndarray:
        """Per-cell statistic = max over sliding windows (footprint ``blur_px``) that lie INSIDE
        the cell of the window mean — features smaller than the footprint count partially, and
        nothing leaks across a cell boundary (HW: a window whose edge sits exactly on a boundary
        does not drive the next cell at all)."""
        p = self.p
        s_req = np.max(img, axis=0)                           # brightest channel, requested nits (unclipped)
        s = np.minimum(s_req, p.white_nits)
        if p.stat_kind == "area_switch":
            d = self._drives_area_switch(s)
        elif p.stat_kind == "winmax":
            d = self.drive_of(self._winmax_stat(s))
        else:
            raise ValueError(f"unknown stat_kind {p.stat_kind!r}")
        if p.lattice_boost > 0:
            d = d * self.lattice_factor(s_req)
        return d

    # ------------------------------------------------------------------ coarse lattice / peak limiter
    def _ramp(self, code: np.ndarray) -> np.ndarray:
        xs = np.array([c for c, _ in self.p.lattice_ramp]); ys = np.array([w for _, w in self.p.lattice_ramp])
        return np.interp(code, xs, ys, left=ys[0], right=ys[-1])

    def lattice_factor(self, s_req: np.ndarray) -> np.ndarray:
        """Per-cell factor (1 + b·(1 − w_cell)) / (1 + b·(1 − w_ref)): w_cell = the detector weight of the
        brightest requested code at the lattice pixels whose coarse block overlaps the cell, w_ref = the
        weight of the cell's own peak code. A uniform
        field has w_cell = w_ref → factor 1 (the measured drive curve already contains the limiter's state);
        a near-peak highlight that misses every lattice pixel of its cell drives 1 + b times more (HW 1.30)."""
        p = self.p
        code = 1023.0 * np.vectorize(oetf_norm)(np.clip(s_req / 10000.0, 0.0, 1.0))
        # lattice pixels (full-res) → reduced-res samples → per-cell max weight
        kx = np.arange(int(np.ceil(-p.lattice_phase_px[0] / p.lattice_px)), int(p.width // p.lattice_px) + 1)
        ky = np.arange(int(np.ceil(-p.lattice_phase_px[1] / p.lattice_px)), int(p.height // p.lattice_px) + 1)
        lx = p.lattice_px * kx + p.lattice_phase_px[0]; ly = p.lattice_px * ky + p.lattice_phase_px[1]
        lx = lx[(lx >= 0) & (lx < p.width)]; ly = ly[(ly >= 0) & (ly < p.height)]
        ix = np.floor(lx / p.scale).astype(int); iy = np.floor(ly / p.scale).astype(int)
        samp = self._ramp(code[np.ix_(iy, ix)])                              # (len(ly), len(lx))
        # each sample governs every LED cell its 48×48 BLOCK overlaps (the sample pixel itself may lie
        # just outside the cell: row 31's sample sits 3 px above it — dark on black → boosted, as HW
        # measured; lit on a field → no boost, so uniform fields stay band-free). Cross-cell coupling
        # (white at a neighbour cell's pixel limiting this LED) is the model's prediction, untested.
        L = p.lattice_px
        w_cell = np.zeros((p.rows, p.cols))
        for ex in (0.0, L - 1e-6):
            for ey in (0.0, L - 1e-6):
                cx = np.clip(np.floor((lx + ex) / p.cell_w).astype(int), 0, p.cols - 1)
                cy = np.clip(np.floor((ly + ey) / p.cell_h).astype(int), 0, p.rows - 1)
                np.maximum.at(w_cell, (np.repeat(cy, len(cx)), np.tile(cx, len(cy))), samp.ravel())
        blocks = code.reshape(p.rows, self.ch, p.cols, self.cw)
        w_ref = self._ramp(blocks.max(axis=(1, 3)))
        b = p.lattice_boost
        return (1.0 + b * (1.0 - w_cell)) / (1.0 + b * (1.0 - w_ref))

    def _winmax_stat(self, s: np.ndarray) -> np.ndarray:
        """Max over sliding windows (footprint ``blur_px``, fractional) inside each cell of the window
        mean of ``s`` (nits) — the winmax statistic before the drive curve."""
        p = self.p
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
        return stat(k0) if f < 1e-6 else (1.0 - f) * stat(k0) + f * stat(k0 + 1)

    def _drives_area_switch(self, s: np.ndarray) -> np.ndarray:
        """The single-cell coverage law (harness ``stat_fit/fald_stat_fit.py`` hyp_e2, fitted
        2026-09-10 on sliver / posmatrix / mirror / camera / LDA-ramp data to 5 % RMS), written so
        that a uniform field of level L drives exactly ``drive_of(L)`` — the measured curve:

          stat  = min(brightest lit px, Σ lit nits·px² / stat_area0_px2)               (area law)
          h     = max ``sup_window_px`` sliding-window mean of the LIT MASK over windows fully inside
                  the suppression box ``sup_box`` (px from the cell's top-left);  hs = drive_of(white·h)
          d_led = min(1, stat_cap · drive_of(stat) · (1 − sup_strength · hs))        (LED current ≤ 1)
          drive = d_led / D0,   D0 = stat_cap · (1 − sup_strength)                   (lit field = 1)

        On single-level content the stat is level × min(1, area/A0): hyp_e2 up to the area constant
        (cap·curve(W·A/A0) ≈ curve(W·A/964) over the sliver range; refit through this model on all
        five coverage datasets 2026-09-11: A0 1199 px², cap 1.114, s 0.319, RMS 0.053 vs harness 0.050).
        A fully lit cell inside a lit field runs at D0 = cap·(1−s) = 0.76 of the LED maximum; an
        unsuppressed sliver in the right/top of the cell reaches 1/D0 = 1.32× (HW 1.30).
        ASSUMPTION (untested below white): the suppression depends on what is lit in the box, not on
        its level — the only choice under which every uniform field stays on the measured curve."""
        p = self.p
        blocks = s.reshape(p.rows, self.ch, p.cols, self.cw).transpose(0, 2, 1, 3)   # (R, C, ch, cw)
        lit = blocks > p.drive_floor_nits
        n_lit = lit.sum(axis=(2, 3))
        if p.stat_window:      # hybrid: the ceiling is the winmax window mean (= winmax on any lit field)
            peak = self._winmax_stat(s)
        else:
            peak = (blocks * lit).max(axis=(2, 3))                                # brightest lit px (nits)
        tot_nits_px2 = (blocks * lit).sum(axis=(2, 3)) * float(p.scale ** 2)       # Σ lit nits · px²
        # min(peak, Σ/A0): on single-level content = level × min(1, area/A0); on a uniform field = the
        # level; MONOTONE in the image (a mean-of-lit form is not: grey around a highlight diluted it —
        # review 2026-09-11 #1, a sliver in a 10-nit field fell to 0.68 instead of 1.0)
        stat = np.minimum(peak, tot_nits_px2 / p.stat_area0_px2)
        demand = p.stat_cap * self.drive_of(stat)
        # suppression: sliding-window mean of the lit mask; per cell the max over windows that lie
        # fully inside the box [x0,x1)×[y0,y1) px relative to the cell's top-left corner
        mask = (s > p.drive_floor_nits).astype(np.float64)
        k = max(1, int(round(p.sup_window_px / p.scale)))
        blur = self._box_blur(mask, k)                        # window [i−k//2, i−k//2+k) at pixel i
        x0, x1, y0, y1 = (v / p.scale for v in p.sup_box)
        ox0, ox1 = int(np.floor(x0)) + k // 2, int(np.ceil(x1)) - k + k // 2 + 1
        oy0, oy1 = int(np.floor(y0)) + k // 2, int(np.ceil(y1)) - k + k // 2 + 1
        padw = max(0, -ox0, ox1 - self.cw) + 1; padh = max(0, -oy0, oy1 - self.ch) + 1
        bp = np.pad(blur, ((padh, padh), (padw, padw)), mode="constant")
        h = np.zeros((p.rows, p.cols))
        for r in range(p.rows):
            ys = slice(padh + r * self.ch + oy0, padh + r * self.ch + oy1)
            for c in range(p.cols):
                xs = slice(padw + c * self.cw + ox0, padw + c * self.cw + ox1)
                h[r, c] = bp[ys, xs].max() if ox1 > ox0 and oy1 > oy0 else 0.0
        hs = self.drive_of(np.minimum(h, 1.0) * p.white_nits)
        d_led = np.minimum(1.0, demand * (1.0 - p.sup_strength * hs))
        d0 = p.stat_cap * (1.0 - p.sup_strength)
        return np.where(n_lit == 0, 0.0, np.maximum(d_led / d0, 0.0))

    # ------------------------------------------------------------------ spread
    def _kernels(self, kind: str, scale_mm: float, core_mm: float = 0.0, tail_frac: float = 0.0,
                 phase_mm: tuple[float, float] = (0.0, 0.0), aniso: float = 1.0, support_cells: int = 0):
        """Per-sub-offset kernels. ``kind``: "exp" (1/e = scale_mm), "gauss" (sigma = scale_mm),
        "mix" ((1−tail_frac)·exp(−d/core_mm) + tail_frac·exp(−d/scale_mm)). ``phase_mm`` shifts
        the SAMPLE point: the field is evaluated at (p + phase) and attributed to p."""
        key = (kind, round(scale_mm, 4), round(core_mm, 4), round(tail_frac, 5),
               round(phase_mm[0], 4), round(phase_mm[1], 4), round(aniso, 5), int(support_cells))
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
                d = np.sqrt(dx * dx + (dy / max(aniso, 1e-3)) ** 2)   # aniso < 1: shorter vertical reach
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
                if support_cells > 0:
                    k = k * ((np.abs(ii[None, :]) <= support_cells) & (np.abs(jj[:, None]) <= support_cells))
                row.append(k)
            kern.append(row)
        # normalise: a fully driven infinite field must give B = 1 at any sample point
        norm = np.mean([[k.sum() for k in row] for row in kern])
        kern = [[k / norm for k in row] for row in kern]
        self._kern_cache[key] = kern
        return kern

    def backlight(self, drives: np.ndarray, kind: str, scale_mm: float,
                  core_mm: float = 0.0, tail_frac: float = 0.0,
                  phase_px: tuple[float, float] = (0.0, 0.0), aniso: float = 1.0,
                  support_cells: int = 0) -> np.ndarray:
        """B on the reduced-res pixel grid (h, w), from cell drives (rows, cols)."""
        p = self.p
        kern = self._kernels(kind, scale_mm, core_mm, tail_frac,
                             (phase_px[0] * p.px_mm, phase_px[1] * p.px_mm), aniso, support_cells)
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
        return self.forward_img(self.render(shapes))

    def backlights(self, drives: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(B_true, B_est) on the reduced-res pixel grid for a cell-drive map."""
        p = self.p
        b_true = self.backlight(drives, "mix", p.tail_mm, p.core_mm, p.tail_frac)
        phase = (p.est_phase_px, p.est_phase_py)
        if p.est_kind == "mix":
            b_est = self.backlight(drives, "mix", p.est_tail_mm, p.est_core_mm, p.est_tail_frac, phase,
                                   p.est_aniso, p.est_support_cells)
        else:
            b_est = self.backlight(drives, p.est_kind, p.est_scale_mm, phase_px=phase, aniso=p.est_aniso,
                                   support_cells=p.est_support_cells)
        return b_true, b_est

    def forward_img(self, img: np.ndarray) -> dict:
        """Forward model on a rendered request image ``img`` (3, h, w) of as-if-white nits."""
        p = self.p
        drives = self.cell_drives(img)
        b_true, b_est = self.backlights(drives)
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
        return self.meter_img(self.render(shapes), meter_px, aperture_px)

    def aperture_mask(self, meter_px: tuple[float, float], aperture_px: Optional[float] = None) -> np.ndarray:
        r = (aperture_px if aperture_px is not None else self.p.aperture_px) / self.p.scale
        mx, my = meter_px[0] / self.p.scale, meter_px[1] / self.p.scale
        yy, xx = np.mgrid[0:self.h, 0:self.w]
        return ((xx + 0.5 - mx) ** 2 + (yy + 0.5 - my) ** 2) <= r * r

    def meter_img(self, img: np.ndarray, meter_px: tuple[float, float],
                  aperture_px: Optional[float] = None) -> np.ndarray:
        out = self.forward_img(img)
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
