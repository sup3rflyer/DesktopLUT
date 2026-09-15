"""FALD panel profiling — the standardised, meter-only characterisation pass (2026-09-14).

This module is the panel-agnostic core of the user-facing ``dlc.stages.fald_profile`` flow: it
turns a panel's geometry (spec-sheet zone count, resolution, physical size) into the fixed pattern
plans one meter spot needs, assembles the reads into the fit datasets, fits the
:class:`~dlc.fald.model.FaldParams` in the two stages the PA32UCXR work validated (probe §22–§27,
work guide §1), reports the held-out accuracy the LLM judges, and provides a synthetic panel so the
whole chain runs under ``--simulate``.

What the pass measures (one meter spot, ≈ 35–45 min on an i1D3; the research probe took ≈ 3 h):

======  ==========================================================  ================================
phase   patterns                                                    yields
======  ==========================================================  ================================
register  black; whole cells at ±2/±3 columns, full rows at ±5/±6   sensor position (self-registration)
grid      grey field, window edge stepped 5 px across the nearest   origin phase of the spec grid
          cell boundary right of / below the meter
drive     white + R/G/B full fields; flat-field sweep; a small       white_nits, chan_weights, the SDR
          window in a black cell at 7 levels (leak ∝ drive);         gamma, drive_curve, A0, tmin,
          size ramp + slivers; hole; peak windows on the meter        peak-size law
leak      code-0 field: window at 5 gaps × R/L/U/D, 4 diagonals      K_true (Stage A)
rings     grey-10 rings at 8 gaps × R/L, 6 × U/D, fine 90–200 px     K_est: scale, phase, aniso (Stage B)
          + grey-5 / grey-20 rings (held out)
heldout   diagonal rings, superposition, R/G/B/orange fields         predictions FROZEN first, then read
======  ==========================================================  ================================

The camera pass of the research programme is not needed: every shipped parameter came from the
meter; the LED sits at the geometric cell centre (probe §26, assumed here) and the fit's held-out
left/right/up/down residual is the gate that would expose an offset.

Nothing here decides "good enough". The stage tool surfaces the numbers; the LLM judges
(design law: every non-deterministic verdict is a seam).
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Optional, Sequence

import numpy as np

from .model import FaldModel, FaldParams, Shape, exp_knot_logw, knot_decrements_of, knot_logw_from_decrements

FULL = (0.0, 0.0, 1.0, 1.0)

# i1 Display Pro body seen from the panel: 37 × 65 mm (199 × 353 px at 0.1845 mm/px, the owner's
# to-scale reference). Bright content must clear the BODY, and the work guide's law 9 keeps highlights
# ≥ 120 px from the sensor on the ProArt (the fitted 57-px aperture is a ring-fit radius, not acceptance).
DEFAULT_BODY_MM = (37.0, 65.0)
MIN_HIGHLIGHT_GAP_MM = 22.0            # 120 px on the ProArt

# Grey levels of the ring sweeps as FRACTIONS of full-field white, so the same plan works on a
# 1800-nit HDR panel (5/10/20 nits ≈ the PA32UCXR levels) and a 120-nit SDR desktop (0.33/0.65/1.3 nits
# would be below the i1D3's useful floor — the SDR plan uses nits floors below).
RING_LEVELS_NITS = (5.0, 10.0, 20.0)
RING_MAIN_NITS = 10.0

# The near-field sizes were designed on the PA32UCXR (0.1845 mm/px, 80-px cells, 120-px keep-out). On another panel
# they follow the panel, not the pixel count (hardening 2026-09-15: a 27" 4K has a 142-px keep-out and a 32" 6K a
# 187-px one — the fixed 120/240/480 names broke the extended verify and mislabelled the held-out bars):
#   near gaps  = m · max(keep-out, ceil(1.5 cells)) for m in (1, 2, 4)       ProArt 120 / 240 / 480
#   fine sweep = 1/8-cell steps across ONE cell from the first near gap     ProArt 120, 130 … 200
#   bar / ramp = the ProArt pixel sizes expressed in mm                     ProArt 40x600 px, 320 px of 8-px strips
PROART_PX_MM = 0.1845
NEAR_GAP_CELLS = 1.5
NEAR_GAP_MULTIPLES = (1, 2, 4)
FINE_STEPS_PER_CELL = 8
BAR_MM = (40 * PROART_PX_MM, 600 * PROART_PX_MM)
RAMP_MM = 320 * PROART_PX_MM
RAMP_STRIP_MM = 8 * PROART_PX_MM


# ---------------------------------------------------------------------------- geometry
@dataclass
class PanelGeometry:
    """Everything the plans need to know about the panel + meter. ``px_mm`` comes from the spec
    diagonal (``from_diagonal``); the zone count from the spec sheet; the meter spot from the
    placement (refined by the ``register`` phase)."""

    width: int
    height: int
    cols: int
    rows: int
    px_mm: float
    meter: tuple[int, int]
    transfer: str = "pq"                 # "pq" (HDR) | "gamma" (SDR)
    bit_depth: int = 10                  # the dogegen daemon's code depth
    white_nits: float = 1000.0           # full-field white (measured by `drive`; SDR: the desktop white)
    sdr_gamma: float = 2.2               # SDR only; measured by `drive`
    body_mm: tuple[float, float] = DEFAULT_BODY_MM

    @classmethod
    def from_diagonal(cls, width: int, height: int, cols: int, rows: int, diagonal_in: float, **kw) -> "PanelGeometry":
        diag_px = math.hypot(width, height)
        return cls(width=width, height=height, cols=cols, rows=rows,
                   px_mm=diagonal_in * 25.4 / diag_px, **kw)

    @property
    def cell_w(self) -> float:
        return self.width / self.cols

    @property
    def cell_h(self) -> float:
        return self.height / self.rows

    @property
    def max_code(self) -> int:
        return (1 << self.bit_depth) - 1

    @property
    def body_px(self) -> tuple[float, float]:
        return (self.body_mm[0] / self.px_mm, self.body_mm[1] / self.px_mm)

    @property
    def min_gap_h(self) -> int:
        """Nearest allowed window edge horizontally (px from the sensor): past the body + the
        highlight keep-out."""
        return int(math.ceil(max(self.body_px[0] / 2 + 10, MIN_HIGHLIGHT_GAP_MM / self.px_mm)))

    @property
    def min_gap_v(self) -> int:
        return int(math.ceil(max(self.body_px[1] / 2 + 10, MIN_HIGHLIGHT_GAP_MM / self.px_mm)))

    @property
    def meter_cell(self) -> tuple[int, int]:
        return (int(self.meter[0] // self.cell_w), int(self.meter[1] // self.cell_h))

    # codes ↔ nits
    def _params_stub(self) -> FaldParams:
        return FaldParams(transfer=self.transfer, code_bits=self.bit_depth, white_nits=self.white_nits,
                          sdr_gamma=self.sdr_gamma)

    def code(self, nits: float) -> int:
        return self._params_stub().nits_to_code(nits)

    def nits(self, code: float) -> float:
        return float(self._params_stub().code_to_nits(np.array([code]))[0])

    def grey(self, nits: float) -> tuple[int, int, int]:
        c = self.code(nits)
        return (c, c, c)

    @property
    def white(self) -> tuple[int, int, int]:
        return (self.max_code,) * 3

    # rects (px → normalised)
    def rect(self, x0: float, y0: float, w: float, h: float) -> tuple[float, float, float, float]:
        """Clipped to the panel; a rect entirely outside comes back with zero width/height (the plans drop
        such patterns — the daemon rejects geometry outside [0, 1])."""
        x0c, y0c = min(max(0.0, x0), float(self.width)), min(max(0.0, y0), float(self.height))
        x1, y1 = min(float(self.width), x0 + w), min(float(self.height), y0 + h)
        return (x0c / self.width, y0c / self.height, max(0.0, x1 - x0c) / self.width, max(0.0, y1 - y0c) / self.height)

    def canvas_meter(self, params: Optional[FaldParams] = None) -> tuple[float, float]:
        """The sensor in the MODEL's canvas pixels. :func:`choose_scale` may rescale the canvas to make
        every cell an integer number of reduced pixels; patterns are normalised (they follow), the meter
        is given in panel pixels and must be scaled the same way (review 2026-09-14 #1)."""
        _, w, h = choose_scale(self.width, self.height, self.cols, self.rows)
        if params is not None:
            w, h = params.width, params.height
        return (self.meter[0] * w / self.width, self.meter[1] * h / self.height)

    def window(self, gap: float, size: float, side: str, meter: Optional[tuple[float, float]] = None):
        """Square window whose NEAR edge is ``gap`` px from the meter centre on ``side``
        (R/L/D/U), centred on the meter's other axis."""
        mx, my = meter or self.meter
        if side == "R":
            return self.rect(mx + gap, my - size / 2, size, size)
        if side == "L":
            return self.rect(mx - gap - size, my - size / 2, size, size)
        if side == "D":
            return self.rect(mx - size / 2, my + gap, size, size)
        if side == "U":
            return self.rect(mx - size / 2, my - gap - size, size, size)
        raise ValueError(side)

    def diag_window(self, gap: float, size: float, tag: str, meter=None):
        mx, my = meter or self.meter
        sx = +1 if tag[1] == "R" else -1
        sy = +1 if tag[0] == "D" else -1
        x0 = mx + gap if sx > 0 else mx - gap - size
        y0 = my + gap if sy > 0 else my - gap - size
        return self.rect(x0, y0, size, size)

    def cell_rect(self, col: int, row: int, inset: tuple[float, float] = (0.0, 0.0)):
        return self.rect(col * self.cell_w + inset[0], row * self.cell_h + inset[1],
                         self.cell_w - 2 * inset[0], self.cell_h - 2 * inset[1])

    def mm_px(self, mm: float) -> int:
        """A physical length in whole panel pixels (≥ 1)."""
        return max(1, int(round(mm / self.px_mm)))

    def base_params(self, **over: Any) -> FaldParams:
        """A :class:`FaldParams` carrying this geometry (and a scale giving integer reduced cells)."""
        scale, w, h = choose_scale(self.width, self.height, self.cols, self.rows)
        # a rescaled canvas keeps the panel's physical size: px_mm shrinks/grows with it
        kw = dict(width=w, height=h, cols=self.cols, rows=self.rows, px_mm=self.px_mm * self.width / w, scale=scale,
                  transfer=self.transfer, code_bits=self.bit_depth, white_nits=self.white_nits,
                  sdr_gamma=self.sdr_gamma, est_support_cells=5, aperture_px=60.0)
        kw.update(over)
        return FaldParams(**kw)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def choose_scale(width: int, height: int, cols: int, rows: int) -> tuple[int, int, int]:
    """(scale, model_width, model_height): the reduced-resolution factor the model renders at, such that
    every cell is an integer number of reduced pixels. Exact for 3840×2160 / 48×48 (scale 5 → 16×9 px
    cells); when no factor in 3..8 divides evenly, the canvas is rescaled to the nearest integer-cell
    size (patterns are normalised, so the geometry error is ≤ half a reduced pixel per cell)."""
    for s in (5, 4, 6, 3, 8, 7, 2):
        if (width // s) * s == width and (height // s) * s == height and (width // s) % cols == 0 and (height // s) % rows == 0:
            return s, width, height
    s = 5
    cw = max(4, int(round(width / (s * cols))))
    ch = max(2, int(round(height / (s * rows))))
    return s, cw * cols * s, ch * rows * s


# ---------------------------------------------------------------------------- patterns
@dataclass
class Pattern:
    """One frame to present + read. ``kind`` "abs" = absolute nits into the fit; "ratio" = divided by
    the mean of the ``ref`` reads of the same group (the ring metric); "aux" = evidence only (not fitted)."""

    name: str
    group: str
    shapes: list
    field: tuple[int, int, int]                   # what the meter nominally sits on (settle bump / log)
    kind: str = "abs"
    ref: Optional[str] = None
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "group": self.group, "shapes": [[list(c), list(g)] for c, g in self.shapes],
                "field": list(self.field), "kind": self.kind, "ref": self.ref, "note": self.note, "meta": self.meta}


def _bg(code3):
    return (tuple(code3), FULL)


def near_gaps(g: "PanelGeometry") -> list[int]:
    """The three near-field window gaps (px, strictly increasing) the held-out rings, the verify rings, the low-grey
    rings and the thin bars use: ``m · max(min_gap_h, ceil(1.5 · cell_w))`` for m = 1, 2, 4. ProArt 120/240/480,
    27" 4K 142/284/568, 32" 6K 187/374/748, 27" 1440p 96/192/384. Patterns carry the index as ``meta.gap_rank``."""
    base = max(g.min_gap_h, int(math.ceil(NEAR_GAP_CELLS * g.cell_w - 1e-9)))
    out: list[int] = []
    for m in NEAR_GAP_MULTIPLES:
        v = int(m * base)
        out.append(v if not out or v > out[-1] else out[-1] + 1)
    return out


def fine_gaps(g: "PanelGeometry") -> list[int]:
    """The fine near-field ring sweep: 1/8-cell steps across one cell starting at the first near gap (the sample phase
    of the estimate repeats per cell). ProArt: 120, 130 … 200 — the set the 2026-09-14 runs measured."""
    n0 = near_gaps(g)[0]
    out: list[int] = []
    for k in range(FINE_STEPS_PER_CELL + 1):
        v = int(round(n0 + k * g.cell_w / FINE_STEPS_PER_CELL))
        if not out or v > out[-1]:
            out.append(v)
    return out


def _drop_offpanel(pats: list) -> list:
    """Patterns whose bright rectangle was clipped to nothing (meter near a border, small panel) are
    dropped whole: the daemon draws nothing where the model would render a 1-px sliver."""
    keep = []
    for p in pats:
        if any(g[2] <= 0.0 or g[3] <= 0.0 for _, g in p.shapes):
            continue
        keep.append(p)
    return keep


def plan_dropped(g: "PanelGeometry") -> dict[str, int]:
    """How many patterns each plan loses to the panel edge at this meter position (preflight evidence)."""
    out = {}
    for name, fn in PLANS.items():
        raw = fn.__wrapped__(g) if hasattr(fn, "__wrapped__") else None
        if raw is None:
            continue
        out[name] = len(raw) - len(_drop_offpanel(raw))
    return {k: v for k, v in out.items() if v}


REG_SPAN_PX = 100          # must exceed the aperture radius (~55 px ring-fit) + the placement error so both plateaus are reached
REG_STEP_PX = 10


def plan_register(g: PanelGeometry, level_nits: float = RING_MAIN_NITS) -> list[Pattern]:
    """Sensor self-registration, kernel-free: on a grey field a bright window's LEFT edge is stepped across
    the nominal sensor x (then its TOP edge across y). The reading follows the aperture's coverage — an
    S-curve whose midpoint is the sensor centre and whose 10–90 % width is the effective aperture. Bright
    reads, ~1 min, valid in SDR and HDR (the earlier kernel-ratio method was degenerate with the firmware's
    sample phase on a grey field and needed code-0 leaks on black — unmeasurable at SDR white)."""
    fc = g.grey(level_nits)
    bg = _bg(fc)
    win = g.grey(0.5 * g.white_nits)
    size = max(600, int(7 * g.cell_w))
    mx, my = g.meter
    pats = [Pattern("REG:black", "register", [_bg((0, 0, 0))], (0, 0, 0), "aux", note="full-field black (meter floor)"),
            Pattern("REG:ref", "register", [bg], fc, "aux", note="field alone")]
    for d in range(-REG_SPAN_PX, REG_SPAN_PX + 1, REG_STEP_PX):
        pats.append(Pattern(f"REG:x{d:+d}", "register", [bg, (win, g.rect(mx + d, my - size / 2, size, size))], fc, "aux",
                            note=f"window left edge at x={mx + d}", meta={"axis": "x", "edge_px": mx + d, "d": d}))
    for d in range(-REG_SPAN_PX, REG_SPAN_PX + 1, REG_STEP_PX):
        pats.append(Pattern(f"REG:y{d:+d}", "register", [bg, (win, g.rect(mx - size / 2, my + d, size, size))], fc, "aux",
                            note=f"window top edge at y={my + d}", meta={"axis": "y", "edge_px": my + d, "d": d}))
    pats.append(Pattern("REG:ref_end", "register", [bg], fc, "aux", note="drift"))
    return pats


def plan_grid(g: PanelGeometry, level_nits: float = 5.0, step_px: int = 5, span_px: int = 30) -> list[Pattern]:
    """Origin-phase check of the spec grid: on a grey field, a window whose near edge is stepped ``step_px``
    across the first cell boundary beyond the keep-out, right of (x) and below (y) the meter. The ring
    reading steps where the edge crosses the boundary; the step position vs the spec boundary is the
    origin offset (0 on the ProArt). Grey 5 nits: the ring is deeper at low grey (ProArt grey-5 +10 %, grey-10 +7 %)."""
    fc = g.grey(level_nits)
    bg = _bg(fc)
    c, r = g.meter_cell
    size = max(200, int(2.5 * g.cell_w))
    bar = 3 * size                       # a BAR along the boundary switches ~7 cells at once: 3x the contrast of a square
    pats = [Pattern("GRID:ref", "grid", [bg], fc, "aux", note="field alone")]
    # first boundary right of the meter with edge - meter >= min_gap_h
    # first boundary whose sweep (edge − span) still clears the keep-out
    bx = next(((c + k) * g.cell_w for k in range(1, 8) if (c + k) * g.cell_w - span_px - g.meter[0] >= g.min_gap_h), (c + 3) * g.cell_w)
    by = next(((r + k) * g.cell_h for k in range(1, 14) if (r + k) * g.cell_h - span_px - g.meter[1] >= g.min_gap_v), (r + 6) * g.cell_h)
    for d in range(-span_px, span_px + 1, step_px):
        x0 = bx + d
        pats.append(Pattern(f"GRID:x{d:+d}", "grid", [bg, (g.white, g.rect(x0, g.meter[1] - bar / 2, size, bar))], fc, "ratio", "GRID:ref",
                            note=f"edge x={x0:.0f}", meta={"axis": "x", "edge_px": x0, "boundary_px": bx, "d": d}))
    for d in range(-span_px, span_px + 1, step_px):
        y0 = by + d
        pats.append(Pattern(f"GRID:y{d:+d}", "grid", [bg, (g.white, g.rect(g.meter[0] - bar / 2, y0, bar, size))], fc, "ratio", "GRID:ref",
                            note=f"edge y={y0:.0f}", meta={"axis": "y", "edge_px": y0, "boundary_px": by, "d": d}))
    pats.append(Pattern("GRID:ref_end", "grid", [bg], fc, "aux", note="drift"))
    return pats


DRIVE_FRACTIONS = (0.01, 0.03, 0.1, 0.3, 0.6, 1.0)          # of white: the drive-curve levels
FLAT_FRACTIONS = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.7, 1.0)   # of white: the EOTF sweep


def plan_drive(g: PanelGeometry) -> list[Pattern]:
    """White / R / G / B full fields (white_nits, chan_weights); flat-field EOTF sweep (the SDR gamma;
    HDR: PQ tracking evidence); a 40-px window in a black cell ≥ 2 columns right of the meter at 6 levels
    (its leak at the meter ∝ the cell's drive → drive_curve); size ramp + slivers in that cell (the area
    statistic / A0); black hole in a bright field (tmin); white windows ON the meter (peak-size law)."""
    black = _bg((0, 0, 0))
    W = g.white
    c, r = g.meter_cell
    # source cell just past the body: the first column ≥ 2 right whose LEFT edge clears the keep-out (the full-cell
    # sliver fills it) — c + 2 on the ProArt; a 27" 4K 48x24 at x=1940 needs c + 3 (c + 2 sits 140 px away, keep-out 142)
    k = next((k for k in range(2, 8) if (c + k) * g.cell_w - g.meter[0] >= g.min_gap_h), 2)
    src = (c + k, r)
    sx0 = src[0] * g.cell_w + 0.25 * g.cell_w
    sy0 = src[1] * g.cell_h + 0.1 * g.cell_h
    pats = [Pattern("DRV:white", "white", [(W, FULL)], W, "abs", note="full-field white")]
    for ch, code in (("R", (g.max_code, 0, 0)), ("G", (0, g.max_code, 0)), ("B", (0, 0, g.max_code))):
        pats.append(Pattern(f"DRV:{ch}", "primaries", [(code, FULL)], code, "aux", note=f"full-field {ch}", meta={"ch": ch}))
    for f in FLAT_FRACTIONS:
        code = g.grey(f * g.white_nits)
        pats.append(Pattern(f"DRV:flat{f:g}", "flat", [_bg(code)], code, "aux", note=f"flat {f:g}·white", meta={"fraction": f, "code": code[0]}))
    pats.append(Pattern("DRV:black", "flat", [black], (0, 0, 0), "aux", note="full-field black (floor)", meta={"fraction": 0.0, "code": 0}))
    # drive curve: a 200-px window at the keep-out gap (area-saturated → the cell drives at the window's
    # level) at fractions of white; its leak at the meter ∝ drive. Dark reads: usable when the code-0 leak
    # is above the meter floor (HDR panels); the stage falls back to the ring version otherwise.
    for f in DRIVE_FRACTIONS:
        code = g.white if f >= 1.0 else g.grey(f * g.white_nits)     # the top point is FULL drive (code max), not the planned white
        pats.append(Pattern(f"DRV:lum{f:g}", "lda_lum", [black, (code, g.window(g.min_gap_h, 200, "R"))], (0, 0, 0), "abs",
                            note=f"200px window at gap {g.min_gap_h} R at {f:g}·white", meta={"fraction": f, "size_px": 200}))
    # size ramp (area statistic): anchored top-left in the source cell, growing right/down
    for s in (10, 20, 40, 80, 160, 320):
        pats.append(Pattern(f"DRV:size{s}", "lda_size", [black, (W, g.rect(sx0, sy0, s, s))], (0, 0, 0), "abs",
                            note=f"{s}px white in cell {src}", meta={"size_px": s}))
    # slivers inside one cell: equal-area shapes must read alike (area law), area steps set A0
    cx1 = (src[0] + 1) * g.cell_w
    cyc = src[1] * g.cell_h + g.cell_h / 2
    for w, h in ((10, 40), (20, 20), (40, 20), (40, 40), (int(g.cell_w), int(g.cell_h))):
        pats.append(Pattern(f"DRV:sliver{w}x{h}", "sliver", [black, (W, g.rect(cx1 - w, cyc - h / 2, w, h))], (0, 0, 0), "abs",
                            note=f"{w}x{h} px at the cell's right edge", meta={"w": w, "h": h}))
    # hole: black square on the meter in a bright field (the pedestal seen from the other side)
    hf = g.grey(0.1 * g.white_nits)
    pats.append(Pattern("DRV:hole_black", "hole", [black], (0, 0, 0), "aux", note="black reference"))
    for s in (300, 600, 1200):
        pats.append(Pattern(f"DRV:hole{s}", "hole", [_bg(hf), ((0, 0, 0), g.rect(g.meter[0] - s / 2, g.meter[1] - s / 2, s, s))], (0, 0, 0), "abs",
                            note=f"{s}px hole in a 0.1·white field", meta={"size_px": s}))
    # peak windows on the meter
    for s in (40, 160, 320, 640):
        pats.append(Pattern(f"DRV:peak{s}", "peak", [black, (W, g.rect(g.meter[0] - s / 2, g.meter[1] - s / 2, s, s))], W, "abs",
                            note=f"{s}px white ON the meter", meta={"size_px": s}))
    pats.append(Pattern("DRV:black_end", "flat", [black], (0, 0, 0), "aux", note="drift"))
    return pats


LEAK_GAPS_H = (120, 180, 300, 480, 900)
LEAK_GAPS_V = (200, 300, 480, 900)


def plan_leak(g: PanelGeometry) -> list[Pattern]:
    """Code-0 field, a white window at increasing gaps in four directions + four diagonal corners:
    the raw leak profile (K_true, Stage A). Gaps below the keep-out are dropped."""
    black = _bg((0, 0, 0))
    W = g.white
    size = 200
    pats = [Pattern("LEAK:black", "leak0", [black], (0, 0, 0), "aux", note="reference")]
    for side, gaps, mn in (("R", LEAK_GAPS_H, g.min_gap_h), ("L", LEAK_GAPS_H, g.min_gap_h), ("D", LEAK_GAPS_V, g.min_gap_v), ("U", LEAK_GAPS_V, g.min_gap_v)):
        for gap in gaps:
            if gap < mn:
                continue
            pats.append(Pattern(f"LEAK:{side}{gap}", "leak0", [black, (W, g.window(gap, size, side))], (0, 0, 0), "abs",
                                note=f"gap {gap}px {side}", meta={"side": side, "gap": gap}))
    for tag in ("DR", "DL", "UR", "UL"):
        gap = max(240, g.min_gap_v)
        pats.append(Pattern(f"LEAK:{tag}{gap}", "leak0@diag", [black, (W, g.diag_window(gap, size, tag))], (0, 0, 0), "abs",
                            note=f"corner gap {gap}px {tag}", meta={"side": tag, "gap": gap}))
    pats.append(Pattern("LEAK:black_end", "leak0", [black], (0, 0, 0), "aux", note="drift"))
    return pats


RING_GAPS_H = (110, 140, 180, 240, 300, 360, 480, 700)
RING_GAPS_V = (200, 240, 300, 360, 480, 700)


def plan_rings(g: PanelGeometry, main_nits: float = RING_MAIN_NITS, held_nits: Sequence[float] = (5.0, 20.0),
               fine: bool = True) -> list[Pattern]:
    """Grey rings: the panel's compensation ERROR next to a white window (ratio to the field alone).
    Grey-``main_nits`` in four directions + the fine near-field sweep (:func:`fine_gaps`) = Stage B; the other levels
    are held out at the :func:`near_gaps`. Levels are in nits (the fit works in nits); the stage clamps them to the
    panel's range."""
    W = g.white
    size = 200
    pats: list[Pattern] = []

    def level_block(nits: float, gaps_h, gaps_v, fine_list, group: str, ranked: bool = False):
        fc = g.grey(nits)
        bg = _bg(fc)
        ref = f"RING{nits:g}:ref"
        pats.append(Pattern(ref, group, [bg], fc, "aux", note="field alone"))
        for side, gaps, mn in (("R", gaps_h, g.min_gap_h), ("L", gaps_h, g.min_gap_h), ("D", gaps_v, g.min_gap_v), ("U", gaps_v, g.min_gap_v)):
            for rank, gap in enumerate(gaps):
                if gap < mn:
                    continue
                meta = {"side": side, "gap": gap, "nits": nits, **({"gap_rank": rank} if ranked else {})}
                pats.append(Pattern(f"RING{nits:g}:{side}{gap}", group, [bg, (W, g.window(gap, size, side))], fc, "ratio", ref,
                                    note=f"gap {gap}px {side}", meta=meta))
        for side in ("R", "L"):
            for gap in fine_list:
                if gap < g.min_gap_h:
                    continue
                pats.append(Pattern(f"RING{nits:g}:fine{side}{gap}", group + "@fine", [bg, (W, g.window(gap, size, side))], fc, "ratio", ref,
                                    note=f"fine gap {gap}px {side}", meta={"side": side, "gap": gap, "nits": nits}))
        pats.append(Pattern(ref + "_end", group, [bg], fc, "aux", note="drift"))

    level_block(main_nits, RING_GAPS_H, RING_GAPS_V, fine_gaps(g) if fine else (), "rings")
    # area law + drive curve seen through the RING (bright reads — the SDR-safe route to A0 and the drive
    # curve): windows of growing area with a fixed near edge, and the 200-px window at fractions of white
    fc = g.grey(main_nits)
    bg = _bg(fc)
    ref = f"RING{main_nits:g}:ref"
    gap = g.min_gap_h
    for a in (10, 20, 40, 80):
        pats.append(Pattern(f"RING{main_nits:g}:area{a}", "rings@area", [bg, (W, g.rect(g.meter[0] + gap, g.meter[1] - a / 2, a, a))], fc, "ratio", ref,
                            note=f"{a}x{a} px at gap {gap} R", meta={"side": "R", "gap": gap, "area_px2": a * a, "nits": main_nits}))
    for f in (0.03, 0.1, 0.3, 0.6):
        code = g.grey(f * g.white_nits)
        pats.append(Pattern(f"RING{main_nits:g}:drive{f:g}", "rings@drive", [bg, (code, g.window(gap, size, "R"))], fc, "ratio", ref,
                            note=f"200px window at {f:g}·white, gap {gap} R", meta={"side": "R", "gap": gap, "fraction": f, "nits": main_nits}))
    for nits in held_nits:
        level_block(nits, near_gaps(g), (), (), "rings@held", ranked=True)
    return pats


def plan_heldout(g: PanelGeometry, nits: float = RING_MAIN_NITS) -> list[Pattern]:
    """Never fitted: diagonal grey rings, superposition of two windows, coloured fields with a near
    highlight (the comp table), a dim orange next to a highlight. Predictions are frozen BEFORE the
    reads (the stage writes them next to the plan)."""
    W = g.white
    size = 200
    fc = g.grey(nits)
    bg = _bg(fc)
    pats = [Pattern("HO:ref", "rings@diag", [bg], fc, "aux", note="field alone")]
    dg = max(240, g.min_gap_v)
    for tag in ("DR", "DL", "UR", "UL"):
        pats.append(Pattern(f"HO:{tag}{dg}", "rings@diag", [bg, (W, g.diag_window(dg, size, tag))], fc, "ratio", "HO:ref",
                            note=f"diagonal {tag} gap {dg}", meta={"side": tag, "gap": dg}))
    g2 = max(240, g.min_gap_v)
    pats.append(Pattern("HO:R240+L240", "superpose", [bg, (W, g.window(240, size, "R")), (W, g.window(240, size, "L"))], fc, "ratio", "HO:ref",
                        note="two windows", meta={"gap": 240}))
    pats.append(Pattern(f"HO:R240+D{g2}", "superpose", [bg, (W, g.window(240, size, "R")), (W, g.window(g2, size, "D"))], fc, "ratio", "HO:ref",
                        note="right + below", meta={"gap": 240}))
    pats.append(Pattern("HO:ref_end", "rings@diag", [bg], fc, "aux", note="drift"))
    # coloured fields at the ring level's luminance: the panel keys on the max channel, the meter reads Y
    lum = {"R": 0.2627, "G": 0.6780, "B": 0.0593}
    for ch, share in lum.items():
        peak = min(nits / share, 0.95 * g.white_nits)
        code = [0, 0, 0]
        code["RGB".index(ch)] = g.code(peak)
        code = tuple(code)
        cbg = _bg(code)
        ref = f"HO:{ch}:ref"
        pats.append(Pattern(ref, "comp", [cbg], code, "aux", note=f"{ch} field alone"))
        for side in ("R", "L"):
            gap = max(120, g.min_gap_h)
            pats.append(Pattern(f"HO:{ch}:{side}{gap}", "comp", [cbg, (W, g.window(gap, size, side))], code, "ratio", ref,
                                note=f"{ch} field, window {side} {gap}", meta={"side": side, "gap": gap, "ch": ch}))
    # orange (fire-lit skin) at half the ring level
    o = (1.0, 0.40, 0.10)
    y_o = 0.5 * nits / sum(a * b for a, b in zip(o, (0.2627, 0.6780, 0.0593)))
    ocode = tuple(g.code(v * y_o) for v in o)
    obg = _bg(ocode)
    pats.append(Pattern("HO:orange:ref", "orange", [obg], ocode, "aux", note="orange alone"))
    for gap in (max(120, g.min_gap_h), 900):
        pats.append(Pattern(f"HO:orange:R{gap}", "orange", [obg, (W, g.window(gap, size, "R"))], ocode, "ratio", "HO:orange:ref",
                            note=f"orange, window R {gap}", meta={"side": "R", "gap": gap}))
    return pats


def plan_verify(g: PanelGeometry, levels: Sequence[float] = (5.0, 20.0)) -> list[Pattern]:
    """The acceptance recipe of a panel file on THIS unit (work guide law 6 / H1): flats, and grey rings
    at three gaps left/right at two levels. Each pattern is read with the layer OFF, in identity
    (debug 4 — the overlay path without the correction, the A/B baseline) and ON."""
    W = g.white
    size = 200
    pats = []
    for nits in levels:
        fc = g.grey(nits)
        bg = _bg(fc)
        pats.append(Pattern(f"VER{nits:g}:flat", "verify", [bg], fc, "aux", note="flat"))
        for side in ("R", "L"):
            for rank, gap in enumerate(near_gaps(g)):
                if gap < g.min_gap_h:
                    continue
                pats.append(Pattern(f"VER{nits:g}:{side}{gap}", "verify", [bg, (W, g.window(gap, size, side))], fc, "ratio", f"VER{nits:g}:flat",
                                    note=f"ring {side} {gap}", meta={"side": side, "gap": gap, "nits": nits, "gap_rank": rank}))
    return pats


# The extended-verify selection by ROLE (group + meta), never by name: the names carry the panel's real gap px.
VERIFY_EXT_ROLES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("rings@low", {"nits": 1.0, "side": "L", "gap_rank": 0}),
    ("rings@low", {"nits": 1.0, "side": "R", "gap_rank": 0}),
    ("halo", {"nits": 2.0, "side": "L", "gap_rank": 0}),
    ("halo", {"nits": 5.0, "side": "L", "gap_rank": 0}),
    ("ramp", {"axis": "h", "direction": "down"}),
    ("ramp", {"axis": "v", "direction": "down"}),
)


def plan_verify_extended(g: PanelGeometry, missing: Optional[list] = None) -> list[Pattern]:
    """The standard verify set + the regimes the augment phase targets: 1-nit rings (dark-theme greys, inside the old
    fade), thin white bars at the first near gap on 2 / 5 nits (the text halo), and the steep downward ramps through
    the sensor. Same OFF / identity / ON protocol; every ratio has its own flat in the set (added before its first
    ratio). Patterns are chosen by role (:data:`VERIFY_EXT_ROLES`); a role this geometry cannot draw is appended to
    ``missing`` (when given) instead of raising."""
    pats = plan_verify(g)
    aug = plan_augment(g)
    by_name = {p.name: p for p in aug}
    added: set[str] = set()

    def add(a: Pattern):
        if a.name in added:
            return
        added.add(a.name)
        pats.append(Pattern("VX:" + a.name, "verify", a.shapes, a.field, a.kind, ("VX:" + a.ref) if a.ref else None, a.note, dict(a.meta)))

    for group, want in VERIFY_EXT_ROLES:
        hit = next((p for p in aug if p.group == group and p.kind == "ratio" and all(p.meta.get(k) == v for k, v in want.items())), None)
        ref = by_name.get(hit.ref or "") if hit is not None else None
        if hit is None or ref is None:
            if missing is not None:
                missing.append({"group": group, **want, "reason": "not drawable at this geometry / meter" if hit is None else "reference dropped"})
            continue
        add(ref)
        add(hit)
    return _drop_offpanel(pats)


LOW_GREYS = (0.5, 1.0, 2.0)          # nits: the dim end (dark-theme UI greys) — sets the drive floor + the fade
HALO_GREYS = (2.0, 5.0, 20.0)


def plan_augment(g: PanelGeometry) -> list[Pattern]:
    """The near-field regime the short pass missed (HDR 2026-09-14: the short-pass model got a thin 40x600 bar at
    120 px wrong by 7 pp) and the dim end the fade has to be chosen from:

    * low-grey rings at 0.5 / 1 / 2 nits (L/R at the first two :func:`near_gaps` fitted, D/U held out) — the drive
      floor + dim drive curve;
    * thin bright bars (:data:`BAR_MM`, 40 x 600 px on the ProArt, full white) LEFT of the sensor at the three near
      gaps on 2 / 5 / 20-nit greys (fitted) + a RIGHT bar at the first near gap on 5 / 20 nits (held out) — the text /
      UI-edge halo;
    * steep ramps through the sensor (lo 2 nits -> hi 0.45 x white over :data:`RAMP_MM` of :data:`RAMP_STRIP_MM`
      strips — 320 px of 8-px strips on the ProArt), horizontal and vertical, both directions, ratio to a flat at the
      ramp's value at the sensor — the gradient regime.
    Names carry the real gap px; ``meta.gap_rank`` is the role (0/1/2 = the near gap used). Every ratio's model
    baseline is its reference field (``meta.base_code`` for the ramps, whose background is not the reference)."""
    W = g.white
    size = 200
    mx, my = g.meter
    near = near_gaps(g)
    bw, bh = g.mm_px(BAR_MM[0]), g.mm_px(BAR_MM[1])
    pats: list[Pattern] = []
    for nits in LOW_GREYS:
        fc = g.grey(nits); bg = _bg(fc); ref = f"LOW{nits:g}:ref"
        pats.append(Pattern(ref, "rings@low", [bg], fc, "aux", note="field alone", meta={"nits": nits}))
        gv = max(near[1], g.min_gap_v)
        for side, rank, gap, grp in (("L", 0, near[0], "rings@low"), ("R", 0, near[0], "rings@low"), ("L", 1, near[1], "rings@low"),
                                     ("R", 1, near[1], "rings@low"), ("D", 1, gv, "rings@lowheld"), ("U", 1, gv, "rings@lowheld")):
            if gap < (g.min_gap_h if side in "LR" else g.min_gap_v):
                continue
            pats.append(Pattern(f"LOW{nits:g}:{side}{gap}", grp, [bg, (W, g.window(gap, size, side))], fc, "ratio", ref,
                                note=f"{nits:g}-nit grey, window {side} {gap}",
                                meta={"side": side, "gap": gap, "nits": nits, "gap_rank": rank}))
        pats.append(Pattern(ref + "_end", "rings@low", [bg], fc, "aux", note="drift", meta={"nits": nits}))
    for nits in HALO_GREYS:
        fc = g.grey(nits); bg = _bg(fc); ref = f"BAR{nits:g}:ref"
        pats.append(Pattern(ref, "halo", [bg], fc, "aux", note="field alone", meta={"nits": nits}))
        for rank, gap in enumerate(near):
            pats.append(Pattern(f"BAR{nits:g}:L{gap}", "halo", [bg, (W, g.rect(mx - gap - bw, my - bh / 2, bw, bh))], fc, "ratio", ref,
                                note=f"{bw}x{bh} white bar, near edge {gap} px left",
                                meta={"side": "L", "gap": gap, "nits": nits, "gap_rank": rank}))
        if nits >= 5.0:
            gap = near[0]
            pats.append(Pattern(f"BAR{nits:g}:R{gap}", "halo@held", [bg, (W, g.rect(mx + gap, my - bh / 2, bw, bh))], fc,
                                "ratio", ref, note=f"{bw}x{bh} white bar, near edge {gap} px right",
                                meta={"side": "R", "gap": gap, "nits": nits, "gap_rank": 0}))
        pats.append(Pattern(ref + "_end", "halo", [bg], fc, "aux", note="drift", meta={"nits": nits}))
    strip = g.mm_px(RAMP_STRIP_MM)
    n = max(2, int(round(RAMP_MM / RAMP_STRIP_MM)))
    lo, hi, span = 2.0, 0.45 * g.white_nits, n * strip            # the strips tile the span exactly (no background seam)
    mid = 0.5 * (lo + hi)
    mc = g.code(mid)
    ref = "RAMP:ref"
    pats.append(Pattern(ref, "ramp", [_bg((mc, mc, mc))], (mc, mc, mc), "aux", note=f"flat at the ramp's value at the sensor ({mid:.1f} nits)",
                        meta={"nits": mid}))
    for axis in ("h", "v"):
        for direction in ("up", "down"):
            first, last = (lo, hi) if direction == "up" else (hi, lo)
            shapes = [_bg(g.grey(first))]
            if axis == "h":
                shapes.append((g.grey(last), g.rect(mx + span / 2, 0, g.width, g.height)))
            else:
                shapes.append((g.grey(last), g.rect(0, my + span / 2, g.width, g.height)))
            for i in range(n):
                v = first + (last - first) * (i + 0.5) / n
                if axis == "h":
                    shapes.append((g.grey(v), g.rect(mx - span / 2 + i * strip, 0, strip, g.height)))
                else:
                    shapes.append((g.grey(v), g.rect(0, my - span / 2 + i * strip, g.width, strip)))
            pats.append(Pattern(f"RAMP:{axis}{direction}", "ramp", shapes, (mc, mc, mc), "ratio", ref,
                                note=f"{axis} ramp {first:.0f}->{last:.0f} nits over {span} px through the sensor",
                                meta={"axis": axis, "direction": direction, "nits": mid, "base_code": mc}))
    pats.append(Pattern(ref + "_end", "ramp", [_bg((mc, mc, mc))], (mc, mc, mc), "aux", note="drift", meta={"nits": mid}))
    return pats


def _dropping(fn):
    def wrapped(g, *a, **kw):
        return _drop_offpanel(fn(g, *a, **kw))
    wrapped.__wrapped__ = fn
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


plan_register = _dropping(plan_register)
plan_grid = _dropping(plan_grid)
plan_drive = _dropping(plan_drive)
plan_leak = _dropping(plan_leak)
plan_rings = _dropping(plan_rings)
plan_heldout = _dropping(plan_heldout)
plan_verify = _dropping(plan_verify)
plan_augment = _dropping(plan_augment)

PLANS: dict[str, Callable[..., list[Pattern]]] = {
    "register": plan_register, "grid": plan_grid, "drive": plan_drive, "leak": plan_leak,
    "rings": plan_rings, "heldout": plan_heldout, "verify": plan_verify, "augment": plan_augment,
}


# ---------------------------------------------------------------------------- reads → datasets
@dataclass
class Read:
    name: str
    xyz: Optional[tuple[float, float, float]]
    t_read_s: float = 0.0
    error: Optional[str] = None

    @property
    def y(self) -> Optional[float]:
        return None if self.xyz is None else float(self.xyz[1])

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "xyz": list(self.xyz) if self.xyz else None, "t_read_s": self.t_read_s, "error": self.error}


REF_TWIN_TOL = 0.03          # a reference and its _end twin further apart than this (relative) = one of them is an outlier


def ref_means(patterns: Sequence[Pattern], reads: dict[str, Read], *, other: Optional[dict[str, Read]] = None,
              outliers: Optional[list] = None, tol: float = REF_TWIN_TOL) -> dict[str, float]:
    """Y of each reference pattern: the mean of it and its ``_end`` drift twin.

    When the two disagree by more than ``tol`` (relative) and ``other`` — the same patterns read in the interleaved
    other state (layer OFF vs identity) — has an agreeing pair, the read consistent with it is used: the other pair's
    mean times this state's typical ratio to the other state at the same field level (median over the phase's other
    patterns on that field, 1 when none). Otherwise the mean. Each such case is appended to ``outliers`` (evidence for
    the phase metrics: ``ref_outlier``)."""
    out = {}
    for p in patterns:
        if p.kind != "aux" or p.name.endswith("_end"):
            continue
        pair = [reads[n].y for n in (p.name, p.name + "_end") if n in reads and reads[n].y is not None]
        if not pair:
            continue
        val = float(np.mean(pair))
        if len(pair) == 2 and min(pair) > 0 and max(pair) / min(pair) - 1.0 > tol:
            rule, chosen = "mean", None
            opair = [] if other is None else [other[n].y for n in (p.name, p.name + "_end") if n in other and other[n].y is not None]
            if len(opair) == 2 and min(opair) > 0 and max(opair) / min(opair) - 1.0 <= tol:
                level = tuple(p.field)
                ratios = [reads[q.name].y / other[q.name].y for q in patterns
                          if tuple(q.field) == level and q.name not in (p.name, p.name + "_end")
                          and q.name in reads and q.name in other and reads[q.name].y and other[q.name].y]
                target = float(np.mean(opair)) * (float(np.median(ratios)) if ratios else 1.0)
                chosen = 0 if abs(pair[0] - target) <= abs(pair[1] - target) else 1
                val, rule = float(pair[chosen]), "consistent_with_other_state"
            if outliers is not None:
                outliers.append({"ref": p.name, "start": pair[0], "end": pair[1], "used": val, "rule": rule,
                                 "kept": (None if chosen is None else ("start" if chosen == 0 else "end"))})
        out[p.name] = val
    return out


def build_items(patterns: Sequence[Pattern], reads: dict[str, Read], meter: tuple[float, float],
                floor_nits: float = 0.012, weight: bool = True) -> list[dict[str, Any]]:
    """Fit items ``{group, name, shapes, y, base, meter, w}`` (the fald_fit.py shape): absolute reads
    above the meter floor, ring ratios to their reference. Aux patterns are skipped."""
    refs = ref_means(patterns, reads)
    items = []
    for p in patterns:
        if p.kind == "aux":
            continue
        rd = reads.get(p.name)
        if rd is None or rd.y is None:
            continue
        if p.kind == "abs":
            if rd.y < floor_nits:
                continue
            items.append({"group": p.group, "name": p.name, "shapes": p.shapes, "y": rd.y, "base": None, "meter": tuple(meter), "w": 1.0})
        else:
            yref = refs.get(p.ref or "")
            if not yref or yref < floor_nits:
                continue
            bc = p.meta.get("base_code")
            base = [((bc, bc, bc), FULL)] if bc is not None else [p.shapes[0]]
            items.append({"group": p.group, "name": p.name, "shapes": p.shapes, "y": rd.y / yref, "base": base,
                          "meter": tuple(meter), "w": 1.0, "level": p.meta.get("nits")})
    return weight_items(items) if weight else items


def weight_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """w = 1/sqrt(n_group): equalise group influence (fald_fit.py). Call once on the MERGED item list."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it["group"]] = counts.get(it["group"], 0) + 1
    for it in items:
        it["w"] = 1.0 / math.sqrt(counts[it["group"]])
    return items


def level_report(ev: "Evaluator", items) -> list[dict[str, Any]]:
    """Per (group, grey level): mean |err| in pp and how often the model gets the SIGN of the ring right where the
    measured ring is > 0.5 pp — the evidence the low-luminance fade is chosen from (below the level where the sign
    agreement breaks, the correction would push the wrong way: the 2026-09-12 dark band)."""
    rows: dict[tuple, list] = {}
    for it in items:
        if it.get("level") is None or it["base"] is None:
            continue
        pred = ev.predict(it)
        rows.setdefault((it["group"], float(it["level"])), []).append((it["y"] - 1.0, pred - 1.0))
    out = []
    for (grp, lvl), rs in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        errs = [100 * abs(pr - me) for me, pr in rs]
        signed = [(me, pr) for me, pr in rs if abs(me) > 0.005]
        agree = sum(1 for me, pr in signed if (me > 0) == (pr > 0))
        out.append({"group": grp, "nits": lvl, "n": len(rs), "mean_abs_pp": float(np.mean(errs)), "max_abs_pp": float(np.max(errs)),
                    "sign_agree": f"{agree}/{len(signed)}", "mean_meas_pp": float(np.mean([100 * me for me, _ in rs]))})
    return out


# ---------------------------------------------------------------------------- derived panel facts
def drive_curve_from_reads(g: PanelGeometry, patterns: Sequence[Pattern], reads: dict[str, Read],
                           floor_nits: float = 0.004) -> list[tuple[float, float]]:
    """The LED drive curve from the ``lda_lum`` window reads: leak at the meter ∝ the source cell's drive,
    normalised to 1 at white (the top point is the white-level window)."""
    pts = []
    for p in patterns:
        if p.group == "lda_lum" and p.name in reads and reads[p.name].y is not None:
            # nits of the window's CODE under g (the measured white), capped at white: code max is full drive
            pts.append((min(g.nits(p.shapes[1][0][0]), g.white_nits), reads[p.name].y))
    if len(pts) < 4:
        return []
    pts.sort()
    top = pts[-1][1]
    if top <= 0 or sum(1 for _, y in pts if y > 3.0 * floor_nits) < 4:
        return []                        # the code-0 leak is at the meter floor: unusable (use rings@drive)
    return [(float(n), float(min(1.0, max(0.0, y / top)))) for n, y in pts]


def power_drive_curve(white_nits: float, k: float) -> list[tuple[float, float]]:
    """A power-law drive curve d = (n/white)^k tabulated on the model's log grid (the fallback when the
    absolute drive sweep is below the meter floor; k is fitted in Stage B on the rings@drive patterns)."""
    return [(white_nits * f, f ** k) for f in (0.003, 0.01, 0.03, 0.1, 0.3, 0.6, 1.0)]


def chan_weights_from_reads(reads: dict[str, Read]) -> Optional[tuple[float, float, float]]:
    w = reads.get("DRV:white")
    if not w or not w.y:
        return None
    ys = []
    for ch in "RGB":
        r = reads.get(f"DRV:{ch}")
        if not r or r.y is None:
            return None
        ys.append(r.y / w.y)
    s = sum(ys)
    return tuple(float(v / s) for v in ys) if s > 0 else None


def flat_sweep(g: PanelGeometry, patterns: Sequence[Pattern], reads: dict[str, Read]) -> list[dict[str, Any]]:
    """(fraction, code, requested nits, measured nits) rows of the flat-field sweep."""
    rows = []
    for p in patterns:
        if p.group == "flat" and p.name in reads and reads[p.name].y is not None and "fraction" in p.meta:
            rows.append({"fraction": p.meta["fraction"], "code": p.meta["code"], "expected_nits": g.nits(p.meta["code"]),
                         "measured_nits": reads[p.name].y})
    return rows


def fit_sdr_gamma(rows: Sequence[dict[str, Any]], white_nits: float, max_code: int) -> Optional[float]:
    """Least-squares power-law exponent through the flat sweep (codes > 0, reads > 0)."""
    xs, ys = [], []
    for r in rows:
        if r["code"] > 0 and r["measured_nits"] and r["measured_nits"] > 0 and r["fraction"] < 1.0:
            xs.append(math.log(r["code"] / max_code)); ys.append(math.log(r["measured_nits"] / white_nits))
    if len(xs) < 3:
        return None
    x, y = np.array(xs), np.array(ys)
    return float(np.sum(x * y) / np.sum(x * x))


def _transition_mid(xs: np.ndarray, ys: np.ndarray) -> tuple[Optional[float], float]:
    """(position where a monotone-ish series crosses the midpoint between its first-3 and last-3 plateaus,
    plateau contrast |hi/lo - 1|). The area statistic turns a cell boundary into a ramp ~ A0/cell_h wide,
    so a 'largest jump' detector is noise-bound; the midpoint crossing is not."""
    if len(xs) < 6:
        return None, 0.0
    sm = np.convolve(ys, np.ones(3) / 3.0, mode="same")
    sm[0], sm[-1] = ys[0], ys[-1]
    lo, hi = float(np.mean(sm[:3])), float(np.mean(sm[-3:]))
    mid = 0.5 * (lo + hi)
    contrast = abs(hi / lo - 1.0) if lo > 0 else 0.0
    for i in range(len(xs) - 1):
        a, b = sm[i] - mid, sm[i + 1] - mid
        if a == 0:
            return float(xs[i]), contrast
        if a * b < 0:
            return float(xs[i] + (xs[i + 1] - xs[i]) * a / (a - b)), contrast
    return None, contrast


def grid_step(patterns: Sequence[Pattern], reads: dict[str, Read], axis: str, area0_px2: float = 1150.0,
              cell_px: Optional[float] = None) -> dict[str, Any]:
    """Where the ring reading transitions along the grid sweep vs where the spec grid says it should.
    The switching cell's lit area grows linearly as the bar's edge backs across the boundary and saturates
    at A0 (the area law), so the transition is a ramp of width A0/cell whose midpoint sits A0/(2·cell) INSIDE
    the boundary on the lit side. ``offset_px`` = measured midpoint − that expectation (A0 is the prior 1150
    before the fit: ± ~10 px of uncertainty); ``contrast`` = the plateau difference (weak = no boundary there).
    Deliberately model-free: a kernel prediction would fold the unknown sample phase into the answer."""
    rows = [(p.meta["edge_px"], reads[p.name].y, p.meta["boundary_px"], p.name) for p in patterns
            if p.group == "grid" and p.meta.get("axis") == axis and p.name in reads and reads[p.name].y is not None]
    rows.sort()
    if len(rows) < 6:
        return {"axis": axis, "ok": False, "reason": "too few reads"}
    xs = np.array([r[0] for r in rows]); ys = np.array([r[1] for r in rows])
    mid, contrast = _transition_mid(xs, ys)
    boundary = rows[0][2]
    out: dict[str, Any] = {"axis": axis, "ok": mid is not None, "boundary_px": boundary, "measured_mid_px": mid,
                           "contrast": contrast, "n": len(rows)}
    if cell_px:
        ramp = area0_px2 / cell_px
        out["expected_mid_px"] = boundary - 0.5 * ramp        # the edge moves +x/+y: the cell on the −side unlights
        out["ramp_px"] = ramp
    else:
        out["expected_mid_px"] = boundary
    if mid is not None:
        out["offset_px"] = mid - out["expected_mid_px"]
    return out


def register_sensor(g: PanelGeometry, patterns: Sequence[Pattern], reads: dict[str, Read]) -> dict[str, Any]:
    """Sensor position from the edge sweeps of :func:`plan_register`: per axis the edge position where the
    reading crosses the midpoint between the window-covered and field-only plateaus (linear interpolation
    between 5-px steps), plus the 10–90 % transition width (≈ the effective aperture diameter)."""
    out: dict[str, Any] = {"nominal_px": list(g.meter)}
    result = list(g.meter)
    for axis in ("x", "y"):
        rows = sorted((p.meta["edge_px"], reads[p.name].y) for p in patterns
                      if p.meta.get("axis") == axis and p.name in reads and reads[p.name].y is not None)
        if len(rows) < 6:
            out[axis] = {"ok": False, "reason": "too few reads"}
            continue
        xs = np.array([r[0] for r in rows]); ys = np.array([r[1] for r in rows])
        # the window extends to +x (+y) of its edge: edge left of/above the sensor → covered (high), past it → field (low)
        hi, lo = float(np.mean(ys[:2])), float(np.mean(ys[-2:]))
        if hi <= lo * 1.5:
            out[axis] = {"ok": False, "reason": f"no transition (covered {hi:.3f} vs field {lo:.3f} nits)"}
            continue

        def cross(level):
            for i in range(len(xs) - 1):
                a, b = ys[i] - level, ys[i + 1] - level
                if a >= 0 > b or a > 0 >= b:
                    return float(xs[i] + (xs[i + 1] - xs[i]) * a / (a - b))
            return None

        mid = cross(0.5 * (hi + lo))
        p10, p90 = cross(lo + 0.9 * (hi - lo)), cross(lo + 0.1 * (hi - lo))
        if mid is None:
            out[axis] = {"ok": False, "reason": "midpoint not crossed inside the sweep"}
            continue
        width = (p90 - p10) if (p10 is not None and p90 is not None) else None
        out[axis] = {"ok": True, "sensor_px": mid, "offset_px": mid - (g.meter[0] if axis == "x" else g.meter[1]),
                     "covered_nits": hi, "field_nits": lo, "width_10_90_px": width}
        result[0 if axis == "x" else 1] = int(round(mid))
    out["sensor_px"] = result
    out["ok"] = all(out[a].get("ok") for a in ("x", "y"))
    return out


# ---------------------------------------------------------------------------- fit
class Evaluator:
    """Model predictions with a per-parameter-set cache (fald_fit.py's, unchanged in spirit)."""

    def __init__(self, params: FaldParams):
        self.params = params
        self.model = FaldModel(params)
        self.cache: dict = {}

    def set(self, **kw):
        self.params = replace(self.params, **kw)
        self.model = FaldModel(self.params)
        self.cache = {}

    def y(self, shapes, meter) -> float:
        k = (tuple((tuple(c), tuple(round(v, 6) for v in g)) for c, g in shapes), tuple(meter))
        if k not in self.cache:
            self.cache[k] = self.model.meter_y(shapes, tuple(meter))
        return self.cache[k]

    def predict(self, item) -> float:
        if item["base"] is None:
            return self.y(item["shapes"], item["meter"])
        return self.y(item["shapes"], item["meter"]) / max(self.y(item["base"], item["meter"]), 1e-9)


def residuals(ev: Evaluator, items) -> np.ndarray:
    return np.array([it["w"] * (math.log(max(ev.predict(it), 1e-6)) - math.log(max(it["y"], 1e-6))) for it in items])


def group_report(ev: Evaluator, items) -> dict[str, dict[str, Any]]:
    """Per group: n, mean|err|, max|err| — % of measured for absolute items, pp (ratio·100) for ratios —
    and the per-item rows the LLM can read."""
    groups: dict[str, list] = {}
    for it in items:
        pred = ev.predict(it)
        err = (pred / it["y"] - 1.0) * 100.0 if it["base"] is None else (pred - it["y"]) * 100.0
        groups.setdefault(it["group"], []).append({"name": it["name"], "meas": it["y"], "pred": pred, "err": err})
    out = {}
    for g, rows in groups.items():
        errs = np.array([r["err"] for r in rows])
        out[g] = {"n": len(rows), "unit": "%" if any(it["group"] == g and it["base"] is None for it in items) else "pp",
                  "mean_abs": float(np.mean(np.abs(errs))), "max_abs": float(np.max(np.abs(errs))), "rows": rows}
    return out


STAGE_A_GROUPS = ("leak0", "leak0@diag", "hole", "lda_lum", "lda_size", "sliver", "peak")
STAGE_B_GROUPS = ("rings", "rings@fine", "rings@area", "rings@drive", "rings@low", "halo", "ramp")
HELD_GROUPS = ("rings@held", "rings@diag", "superpose", "comp", "orange", "rings@lowheld", "halo@held")
NEAR_FIELD_GROUPS = ("rings@fine", "halo")
DRIVE_FLOOR_CANDIDATES = (0.05, 0.15, 0.3, 0.5, 1.0)


def fit_stage_a(ev: Evaluator, items, *, quick: bool = False, log=print) -> dict[str, float]:
    """K_true (core, tail, share, p-norm), tmin, A0 and the meter aperture from the absolute reads."""
    from scipy.optimize import least_squares
    names = ["core_mm", "tail_mm", "tail_frac", "tmin", "aperture_px", "kernel_pnorm", "stat_area0_px2"]
    x0 = np.log([8.0, 28.0, 0.35, 1e-3, 60.0, 1.8, 1150.0])
    lo = np.log([1.5, 8.0, 0.02, 5e-5, 30.0, 1.0, 200.0]); hi = np.log([25.0, 90.0, 0.9, 1e-2, 140.0, 2.5, 6000.0])

    def f(x):
        v = np.exp(x)
        ev.set(core_mm=v[0], tail_mm=v[1], tail_frac=min(v[2], 0.95), tmin=v[3], aperture_px=v[4], kernel_pnorm=v[5], stat_area0_px2=v[6])
        r = residuals(ev, items)
        log(f"   A: {' '.join(f'{n}={val:.4g}' for n, val in zip(names, v))}  rms={math.sqrt(np.mean(r * r)):.4f}")
        return r

    res = least_squares(f, x0, bounds=(lo, hi), diff_step=0.05, max_nfev=6 if quick else 40, xtol=1e-3, ftol=1e-3)
    v = np.exp(res.x)
    ev.set(core_mm=v[0], tail_mm=v[1], tail_frac=min(v[2], 0.95), tmin=v[3], aperture_px=v[4], kernel_pnorm=v[5], stat_area0_px2=v[6])
    return {**dict(zip(names, (float(x) for x in v))), "rms": float(math.sqrt(np.mean(res.fun ** 2)))}


def fit_stage_b(ev: Evaluator, items, *, quick: bool = False, log=print, fit_drive_k: bool = False,
                fit_area0: bool = True) -> dict[str, float]:
    """The firmware's estimate: exponential scale, sample phase (x, y), vertical anisotropy, dim-end
    drive, plus the area constant A0 seen through the rings@area patterns and (``fit_drive_k``, when the
    absolute drive sweep was below the meter floor) the power-law drive exponent through rings@drive.
    A coarse phase grid first (the surface has local minima), then a continuous refine."""
    from scipy.optimize import least_squares
    best = None
    white = ev.params.white_nits
    for ph in ([-30.0, 0.0] if quick else [-40.0, -20.0, 0.0, 20.0]):
        def f(x, ph=ph):
            ev.set(est_kind="exp", est_scale_mm=float(np.exp(x[0])), drive_dim=float(np.exp(x[1])), est_phase_px=ph)
            r = residuals(ev, items)
            log(f"   B[ph={ph:+.0f}]: scale={np.exp(x[0]):.4g} dim={np.exp(x[1]):.4g}  rms={math.sqrt(np.mean(r * r)):.4f}")
            return r
        res = least_squares(f, np.log([15.0, 0.1]), bounds=(np.log([3.0, 1e-3]), np.log([120.0, 0.6])),
                            diff_step=0.05, max_nfev=4 if quick else 15, xtol=1e-3, ftol=1e-3)
        rms = math.sqrt(np.mean(res.fun ** 2))
        if best is None or rms < best[0]:
            best = (rms, ph, res.x)
    _, ph, xb = best

    a0_fixed = ev.params.stat_area0_px2
    # the refine's parameter vector: a frozen coordinate must be LEFT OUT, not pinned by bounds (a ±1e-6 box
    # stalled the trust region on the HDR HW refit: phase_py / aniso never moved from their start values)
    active = ["est_scale_mm", "drive_dim", "est_phase_px", "est_phase_py", "est_aniso"] + (["stat_area0_px2"] if fit_area0 else [])         + (["drive_k"] if fit_drive_k else [])

    def unpack(x):
        v = dict(zip(active, x))
        kw = {"est_scale_mm": float(np.exp(v["est_scale_mm"])), "drive_dim": float(np.exp(v["drive_dim"])),
              "est_phase_px": float(v["est_phase_px"]), "est_phase_py": float(v["est_phase_py"]),
              "est_aniso": float(np.exp(v["est_aniso"])),
              "stat_area0_px2": float(np.exp(v["stat_area0_px2"])) if fit_area0 else a0_fixed}
        if fit_drive_k:
            kw["drive_curve"] = power_drive_curve(white, float(np.exp(v["drive_k"])))
        return kw

    def gfun(x):
        kw = unpack(x)
        ev.set(est_kind="exp", **kw)
        r = residuals(ev, items)
        log(f"   B[refine]: scale={kw['est_scale_mm']:.4g} dim={kw['drive_dim']:.4g} phase=({kw['est_phase_px']:+.1f},{kw['est_phase_py']:+.1f}) "
            f"aniso={kw['est_aniso']:.3f} A0={kw['stat_area0_px2']:.0f}"
            + (f" k={np.exp(dict(zip(active, x))['drive_k']):.3f}" if fit_drive_k else "") + f"  rms={math.sqrt(np.mean(r * r)):.4f}")
        return r

    spec = {"est_scale_mm": (xb[0], np.log(3.0), np.log(120.0), 0.05), "drive_dim": (xb[1], np.log(1e-3), np.log(0.6), 0.05),
            "est_phase_px": (ph, -100.0, 100.0, 0.1), "est_phase_py": (0.0, -60.0, 60.0, 0.2), "est_aniso": (0.0, np.log(0.25), np.log(2.0), 0.05),
            "stat_area0_px2": (math.log(ev.params.stat_area0_px2), np.log(200.0), np.log(6000.0), 0.1),
            "drive_k": (math.log(0.55), math.log(0.2), math.log(1.5), 0.05)}
    x0 = [spec[n][0] for n in active]; lo = [spec[n][1] for n in active]; hi = [spec[n][2] for n in active]; steps = [spec[n][3] for n in active]
    res = least_squares(gfun, np.array(x0), bounds=(lo, hi), diff_step=steps, max_nfev=5 if quick else 40, xtol=1e-3, ftol=1e-3)
    kw = unpack(res.x)
    ev.set(est_kind="exp", **kw)
    out = {k: v for k, v in kw.items() if k != "drive_curve"}
    if fit_drive_k:
        out["drive_k"] = float(np.exp(dict(zip(active, res.x))["drive_k"]))
    out["rms"] = float(math.sqrt(np.mean(res.fun ** 2)))
    return out


def fit_knots(ev: Evaluator, items, *, quick: bool = False, log=print) -> dict[str, Any]:
    """Free-form (monotone) radial estimate profile over the model's knots, started from the fitted
    exponential — the near-field refinement that closed the ProArt's 120-px ring (probe §34/§34a).
    The caller gates it on the held-out report (never ship it blind)."""
    from scipy.optimize import least_squares
    p = ev.params
    cell_mm = p.cell_w * p.px_mm
    logw0 = exp_knot_logw(p.est_scale_mm, p.est_knot_cells, cell_mm)
    z0 = knot_decrements_of(logw0)

    def f(z):
        ev.set(est_kind="knots", est_knot_logw=knot_logw_from_decrements(z))
        r = residuals(ev, items)
        log(f"   K: rms={math.sqrt(np.mean(r * r)):.4f}")
        return r

    res = least_squares(f, z0, diff_step=0.1, max_nfev=4 if quick else 30, xtol=1e-3, ftol=1e-3)
    logw = knot_logw_from_decrements(res.x)
    ev.set(est_kind="knots", est_knot_logw=logw)
    return {"est_knot_logw": list(logw), "rms": float(math.sqrt(np.mean(res.fun ** 2)))}


def run_fit(base: FaldParams, items: list[dict[str, Any]], *, quick: bool = False, knots: str = "auto",
            fit_drive_k: bool = False, log=print) -> dict[str, Any]:
    """The whole fit: Stage A on the absolute groups, Stage B on the grey-main rings, held-out report;
    ``knots`` = "never" | "auto" (fit, keep only if no held-out group degrades > 0.3 pp and the fine
    sweep improves) | "always". Returns params + reports; nothing here accepts anything."""
    t0 = time.time()
    ev = Evaluator(base)
    by = lambda *gs: [d for d in items if d["group"] in gs]
    a_items = by(*STAGE_A_GROUPS)
    b_items = by(*STAGE_B_GROUPS)
    held = by(*HELD_GROUPS)
    out: dict[str, Any] = {"n_items": len(items), "groups": sorted({d["group"] for d in items})}
    if a_items:
        out["stage_a"] = fit_stage_a(ev, a_items, quick=quick, log=log)
        out["stage_a_report"] = group_report(ev, a_items)
    else:
        out["stage_a"] = None
    # A0: Stage A owns it when the absolute sliver / size-ramp reads were above the meter floor (the HW-validated
    # route); the four rings@area patterns are a weak substitute used only when Stage A had none (SDR white).
    # (HDR 2026-09-14: Stage A 1401 / research 1150 / rings@area 518 — the ring value was wrong and cost an
    # 8.9-pp staircase outlier; review finding #6.)
    has_area_abs = any(d["group"] in ("sliver", "lda_size") for d in a_items)
    out["area0_source"] = "stage_a" if has_area_abs else "rings@area"
    if b_items:
        out["stage_b"] = fit_stage_b(ev, b_items, quick=quick, log=log, fit_drive_k=fit_drive_k, fit_area0=not has_area_abs)
        out["stage_b_report"] = group_report(ev, b_items)
    else:
        out["stage_b"] = None
    # drive floor (cells off below it): a threshold, so a small grid on the dim items instead of the optimiser
    dim_items = [d for d in by("rings@low", "halo") if d.get("level") is not None and d["level"] <= 2.0]
    out["drive_floor"] = None
    if dim_items and b_items:
        table = []
        for fl in DRIVE_FLOOR_CANDIDATES:
            ev.set(drive_floor_nits=fl)
            r = residuals(ev, dim_items)
            table.append({"floor_nits": fl, "rms": float(math.sqrt(np.mean(r * r)))})
            log(f"   floor {fl:g} nits: dim-item rms {table[-1]['rms']:.4f}")
        # a floor BELOW the dimmest measured grey is unidentifiable (identical rms): take the HIGHEST floor within 1 %
        # of the best — never assume LEDs light below the data (HDR evidence: LEDs off < 0.5 nit)
        rmin = min(t["rms"] for t in table)
        best = max(t["floor_nits"] for t in table if t["rms"] <= rmin * 1.01 + 1e-12)
        ev.set(drive_floor_nits=best)
        out["drive_floor"] = {"table": table, "chosen_nits": best, "n_dim_items": len(dim_items)}
        out["stage_b_report"] = group_report(ev, b_items)
    exp_params = ev.params
    held_exp = group_report(ev, held) if held else {}
    out["heldout_exp"] = held_exp
    out["knots"] = None
    if knots != "never" and b_items:
        near = [g for g in NEAR_FIELD_GROUPS if by(g)]
        near_exp = {g: out["stage_b_report"].get(g, {}).get("mean_abs") for g in near}
        kres = fit_knots(ev, b_items, quick=quick, log=log)
        held_k = group_report(ev, held) if held else {}
        near_k = {g: group_report(ev, by(g)).get(g, {}).get("mean_abs") for g in near}
        degraded = [g for g in held_exp if held_k.get(g, {}).get("mean_abs", 0) > held_exp[g]["mean_abs"] + 0.3]
        near_worse = [g for g in near if near_k[g] is not None and near_exp[g] is not None and near_k[g] > near_exp[g] + 0.2]
        gain = sum((near_exp[g] - near_k[g]) for g in near if near_k[g] is not None and near_exp[g] is not None)
        improved = gain > 0.2 and not near_worse
        keep = knots == "always" or (not degraded and improved)
        out["knots"] = {**kres, "heldout": held_k, "near_field_exp_pp": near_exp, "near_field_knots_pp": near_k,
                        "fine_exp_pp": near_exp.get("rings@fine"), "fine_knots_pp": near_k.get("rings@fine"),
                        "degraded_groups": degraded, "near_field_worse": near_worse, "kept": keep}
        if not keep:
            ev = Evaluator(exp_params)
    out["by_level"] = level_report(ev, b_items + held)
    out["heldout"] = group_report(ev, held) if held else {}
    out["params"] = params_dict(ev.params)
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


def params_dict(p: FaldParams) -> dict[str, Any]:
    d = {}
    for k, v in p.__dict__.items():
        d[k] = [list(x) if isinstance(x, (list, tuple)) else x for x in v] if isinstance(v, (list, tuple)) else v
    return d


def params_from_dict(d: dict[str, Any]) -> FaldParams:
    kw = dict(d)
    if "drive_curve" in kw:
        kw["drive_curve"] = [tuple(x) for x in kw["drive_curve"]]
    for k in ("chan_weights", "tmin_rgb", "est_knot_cells", "est_knot_logw", "ped_chroma_lum_fade"):
        if kw.get(k) is not None:
            kw[k] = tuple(kw[k])
    return FaldParams(**{k: v for k, v in kw.items() if k in FaldParams.__dataclass_fields__})


def predictions(params: FaldParams, patterns: Sequence[Pattern], meter) -> dict[str, float]:
    """Frozen predictions for a plan (absolute nits for abs patterns, ratios for ratio patterns)."""
    ev = Evaluator(params)
    out = {}
    for p in patterns:
        if p.kind == "abs":
            out[p.name] = ev.y(p.shapes, meter)
        elif p.kind == "ratio":
            out[p.name] = ev.y(p.shapes, meter) / max(ev.y([p.shapes[0]], meter), 1e-9)
    return out


def compare_predictions(patterns: Sequence[Pattern], reads: dict[str, Read], pred: dict[str, float]) -> dict[str, Any]:
    """Held-out scorecard: per group mean/max |err| against the frozen predictions."""
    refs = ref_means(patterns, reads)
    rows: dict[str, list] = {}
    for p in patterns:
        if p.kind == "aux" or p.name not in reads or reads[p.name].y is None or p.name not in pred:
            continue
        y = reads[p.name].y
        if p.kind == "ratio":
            yref = refs.get(p.ref or "")
            if not yref:
                continue
            meas = y / yref
            err = (pred[p.name] - meas) * 100.0
        else:
            meas = y
            err = (pred[p.name] / meas - 1.0) * 100.0 if meas > 0 else float("nan")
        rows.setdefault(p.group, []).append({"name": p.name, "meas": meas, "pred": pred[p.name], "err": err})
    out = {}
    for g, rs in rows.items():
        errs = np.array([r["err"] for r in rs if math.isfinite(r["err"])])
        out[g] = {"n": len(rs), "mean_abs": float(np.mean(np.abs(errs))) if len(errs) else None,
                  "max_abs": float(np.max(np.abs(errs))) if len(errs) else None, "rows": rs}
    return out


# ---------------------------------------------------------------------------- synthetic panel (simulate)
class SyntheticFaldPanel:
    """A mini-LED panel for ``--simulate``: the forward model with HIDDEN parameters + read noise. Reads
    return XYZ (D65-ish chromaticity) so the stage's plumbing is exercised end to end."""

    def __init__(self, params: FaldParams, *, noise_frac: float = 0.004, floor_nits: float = 0.003, seed: int = 7):
        self.params = params
        self.model = FaldModel(params)
        self.rng = np.random.default_rng(seed)
        self.noise_frac = noise_frac
        self.floor = floor_nits

    @classmethod
    def hidden(cls, g: PanelGeometry, **over) -> "SyntheticFaldPanel":
        """Plausible-but-different-from-the-fit-start truth (so a recovered fit means something)."""
        kw = dict(core_mm=9.0, tail_mm=30.0, tail_frac=0.4, kernel_pnorm=1.8, tmin=8e-4, stat_area0_px2=1000.0,
                  aperture_px=55.0, est_kind="exp", est_scale_mm=16.0, est_phase_px=-22.0, est_phase_py=-10.0,
                  est_aniso=0.9, drive_dim=0.12, chan_weights=(0.30, 0.60, 0.10))
        kw.update(over)
        return cls(g.base_params(**kw))

    def read(self, shapes, meter) -> tuple[float, float, float]:
        y3 = self.model.meter(shapes, tuple(meter))               # per-channel luminance
        y = float(y3.sum())
        y = y * (1.0 + self.rng.normal(0.0, self.noise_frac)) + self.rng.normal(0.0, self.floor)
        y = max(y, 0.0)
        # a mildly coloured pedestal: the leak is bluer than the white (probe §22)
        blue = float(y3[2] / max(y, 1e-9))
        x = y * (0.95 - 0.05 * blue)
        z = y * (1.09 + 0.15 * blue)
        return (x, y, z)
