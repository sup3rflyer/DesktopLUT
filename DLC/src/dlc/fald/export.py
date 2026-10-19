"""Export a fitted FALD model as the binary panel-parameter file the DesktopLUT shader loads.

The shader does NO kernel math of its own: the drive curve (as a log-spaced 1-D LUT) and the two
sub-cell kernel tables are tabulated here with the very same code the Python reference uses
(:meth:`FaldModel._kernels`, :meth:`FaldModel.drive_of`), so the GPU port can only differ from the
reference by interpolation and float precision.

File layout (little-endian, all float32 unless noted; header is 32 uint32/float32 words):
  word  0  magic 0x464C4431 ('FLD1')
  word  1  cols            word  2  rows           word  3  sub (samples per cell per axis)
  word  4  cell_w_px       word  5  cell_h_px      word  6  origin_x_px   word  7  origin_y_px
  word  8  reach_true_c    word  9  reach_true_r   (kernel half-extent in cells; table (2r+1) wide)
  word 10  reach_est_c     word 11  reach_est_r
  word 12  curve_n         word 13  f: white_nits  word 14  f: tmin       word 15  f: area0_px2
  word 16  f: w_r          word 17  f: w_g         word 18  f: w_b        (channel shares of white)
  word 19  f: gain_min     word 20  f: gain_max    word 21  f: drive_floor_nits
  word 22  f: curve_log_min  word 23  f: curve_log_max   (natural log of nits at LUT ends)
  word 24  f: est_phase_px   word 25  f: est_phase_py   (informational; already baked into K_est)
  word 26  f: fade_lo       word 27  f: fade_hi      (correction fades to identity for B_est below; 0,0 = loader defaults)
  word 28  f: gain_smooth_cells (Gaussian sigma of the gain low-pass, cells; 0 = off)
  word 29  f: lum_fade_lo   word 30  f: lum_fade_hi   (pixel-luminance fade, as-if-white nits; 0,0 = loader defaults)
  word 31  the lattice EXTENT width | height << 16 (px, 16 bits each) of a FRACTIONAL zone pitch, else 0 (reserved, every
           file before 2026-10-19). Fractional pitch (docs/fald-fractional-pitch.md): words 4/5 are then 0 and the
           lattice is cols x rows zones over width x height px — a rational pitch; zone c spans pixels
           [round(c * width / cols), round((c + 1) * width / cols)) (ties to the lower edge), a pixel belongs to the zone
           its centre lies in. Written only when the frame does not divide evenly (an integer pitch keeps words 4/5 and a
           zero word 31, byte for byte); an exe from before 2026-10-19 refuses such a file (cell word 0) — deliberately:
           it would run the zones on a drifting whole-pixel lattice. Any FLD version can carry it.
FLD2 (magic 0x464C4432, written when the fit carries a pedestal colour, tmin_rgb): the same 32 words, then
8 more (header = 40 words = 160 bytes):
  word 32  f: m_r  word 33  f: m_g  word 34  f: m_b   (pedestal colour multipliers on tmin; Σ w_c·m_c = 1 keeps
                                                       the luminance fit; the LOADER stores them, the GUI
                                                       "per-channel pedestal" toggle decides whether the
                                                       shader uses them — off = white pedestal, as FLD1)
  word 35  ped_mode the fit was validated with: 0 = white, 1 = channel (informational; the GUI setting rules)
  word 36  f: ped_chroma_gain   (0 = default 1: the colour part of the pedestal term at model strength)
  word 37  f: chroma_lum_fade_lo  word 38  f: chroma_lum_fade_hi   (pixel-luminance fade of the COLOUR part only;
           both 0 with word 36 > 0 = NO fade; word 36 == 0 = follow lum_fade words 29/30)
  word 39  reserved (0)
FLD3 (magic 0x464C4433, written when the fit's ``transfer`` is "gamma" — an SDR desktop profiled under Windows
ACM): the 40 FLD2 words (words 32-39 all ZERO when the fit has no pedestal colour: the loader then keeps the
FLD1 white pedestal), then 8 more (header = 48 words = 192 bytes):
  word 40  transfer: 0 = PQ codes (HDR), 1 = gamma codes (SDR under ACM; the panel's own power-law EOTF)
  word 41  f: sdr_gamma  (the exponent, transfer 1 only; the C++ loader refuses values outside 1..4)
  words 42-47 reserved (0)
A PQ fit stays FLD1/FLD2 byte for byte. An exe from before 2026-09-14 refuses the FLD3 magic — deliberately:
it would decode an SDR fit's codes as PQ. The C++ side refuses a transfer/mode mismatch (a gamma file on an
HDR monitor or the reverse) both at runtime.set_fald_params and when the GPU tables are built.
FLD4 (magic 0x464C4434, written when the fit carries a black-frame LED boost LUT, ``FaldParams.boost_lut`` — work
guide C12): the 48 FLD3 words (words 32-39 all ZERO without a pedestal colour; word 40 = the transfer, 0 for a PQ
fit, word 41 = sdr_gamma or 0), then 56 more (header = 104 words = 416 bytes):
  word 48  boost step count n, 1..24 (0 = no boost: the loader ignores the block)
  word 49  f: boost_lit_nits   word 50  f: boost_lit_frac   (a zone counts as NON-BLACK when more than lit_frac of
  word 51  f: boost_dim_nits   word 52  f: boost_dim_frac    its pixels exceed lit_nits — 0 = any pixel — OR more than
           dim_frac of them exceed dim_nits; as-if-white nits of the pixel's brightest channel)
  word 53  zone rule kind: 0 = LIT-or-DIM as above (every file before 2026-09-20: the word was reserved, zero),
           1 = LIT-or-MEAN (work guide C12b, ``FaldParams.boost_rule`` "mean"): LIT as above OR the zone mean of
           (brightest channel, as-if-white nits) ^ mean_gamma >= mean_thresh; words 51/52 are then unused
  word 54  f: boost_mean_gamma   word 55  f: boost_mean_thresh   (kind 1 only, else ZERO; the loader refuses a kind
           > 1 and, for kind 1, a gamma outside (0, 4] or a threshold that is not a finite number > 0)
  words 56-103  24 x (f: zone_fraction_lo, f: boost), the first n in use, the rest ZERO — a step function over the
           non-black zone fraction of the frame the panel receives: the last step with lo <= fraction applies, below
           the first step the boost is 1 (:meth:`FaldModel.boost_of_fraction`). The loader refuses a count > 24, lo
           outside 0..1 or not strictly ascending, a boost outside 0.25..4 (0.5..2 before 2026-10-19: a step BELOW 1 =
           a panel that suppresses small highlights; old exes refuse such a file) and implausible activation words.
The shader multiplies B_true (never B_est) by the looked-up boost, the normalisation fields stay boost-free. A fit
WITHOUT a boost LUT stays FLD1/FLD2/FLD3 byte for byte; an exe from before 2026-09-18 refuses the FLD4 magic —
deliberately: it would run the kernels of a boost-aware fit without the boost term.
FLD5 (magic 0x464C4435, written when the fit's statistic is the P10 CONTEXT statistic, ``FaldParams.stat_kind``
"ctxpow" — work guide P10, results/fald_p10_2026-10-05/RESULT.md): the FLD4 layout (104 words; the boost block's count
word 48 may be 0 = no boost) with the zone-statistic words in the FLD3 block's reserved slots:
  word 42  statistic kind: 0 = area min(peak, Σ/A0) (every older file: the words were reserved, zero), 1 = ctxpow
  word 43  f: stat_ctx_m0 (nit, > 0)   word 44  f: stat_ctx_g_lit (0..1)   word 45  f: stat_ctx_floor (0..1)
  word 46  f: stat_ctx_eps (nit, 1e-4 <= eps <= 10)   word 47  reserved (0; refused otherwise); word 42 must be 1
  (kind 1: m = exp(mean over the zone's pixels of ln max(s, eps)), w = m / (m0 + m), gamma = g_lit + (1 - g_lit)(1 - w),
   stat = peak · min(1, Σ_lit / (A0 · peak))^gamma, drive = max(drive_of(stat), floor · (1 - w) · drive_of(peak)))
An exe from before the P10 shader refuses the FLD5 magic — deliberately: it would run the area statistic under a fit
whose kernels and corrections assume the context statistic.
The C++ reader is LoadFaldPanelParams (src/fald.cpp; tests/test_fald.cpp); words 26-30 are optional —
zero means 'loader default' so older files stay loadable; an FLD1 file loads with m = (1, 1, 1).
Keep the two in step when adding a word (the work guide forbids a header word without a C++ test).
  then: curve[curve_n]                                  drive vs ln(nits), linear in ln(nits)
  then: k_true[sub][sub][2*reach_true_r+1][2*reach_true_c+1]   index order (oy, ox, j, i)
  then: k_est [sub][sub][2*reach_est_r+1][2*reach_est_c+1]
Kernel index i (columns) / j (rows) = sample cell minus source cell, as in the reference
(fftconvolve 'same' with the kernel centred: out[c] = Σ_i d[c-i]·k[i]).
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

import numpy as np

from .model import FaldModel, FaldParams

MAGIC = 0x464C4431
MAGIC2 = 0x464C4432          # 'FLD2': 40-word header (pedestal colour multipliers + validated mode)
MAGIC3 = 0x464C4433          # 'FLD3': 48-word header (+ signal transfer words 40/41; every "gamma" fit)
MAGIC4 = 0x464C4434          # 'FLD4': 104-word header (+ the black-frame LED boost block; every fit with a boost_lut)
MAGIC5 = 0x464C4435          # 'FLD5': the FLD4 layout + the P10 context-statistic words 42-46 (stat_kind "ctxpow")
STAT_KIND_CODES = {"area": 0, "ctxpow": 1}   # FLD5 word 42 (FaldParams.stat_kind)
BOOST_RULE_CODES = {"dim": 0, "mean": 1}   # FLD4 word 53 (FaldParams.boost_rule); src/fald.h FALD_BOOST_RULE_*
BOOST_MAX_STEPS = 24         # src/fald.h FALD_BOOST_MAX_STEPS
# one step's LED factor (shared/fald_panel.h FALD_BOOST_STEP_MIN / MAX, 2026-10-19; 0.5..2 before): < 1 = small-highlight
# suppression (27-inch 1152-zone class, zoo research 2026-10-19 §3); the range = the layer's default gain clamp
BOOST_STEP_MIN, BOOST_STEP_MAX = 0.25, 4.0
TRANSFER_CODES = {"pq": 0, "gamma": 1}
PED_MODE_CODES = {"white": 0, "channel": 1}
CURVE_N = 1024
CURVE_LOG_MIN, CURVE_LOG_MAX = float(np.log(1e-2)), float(np.log(10000.0))
LATTICE_MAX_PX = 65535       # shared/fald_panel.h FALD_LATTICE_MAX_PX: the extent word packs 16 bits per axis
LATTICE_MAX_ZONES = 512      # the C++ loader's cols / rows limit


def kernel_tables(model: FaldModel):
    """(k_true, k_est) as arrays (sub, sub, 2r+1, 2c+1) with the reference's own kernel code."""
    p = model.p
    kt = model._kernels("mix", p.tail_mm, p.core_mm, p.tail_frac, pnorm=p.kernel_pnorm)
    phase = (p.est_phase_px * p.px_mm, p.est_phase_py * p.px_mm)
    if p.est_kind == "mix":
        ke = model._kernels("mix", p.est_tail_mm, p.est_core_mm, p.est_tail_frac, phase, p.est_aniso, p.est_support_cells)
    else:
        ke = model._kernels(p.est_kind, p.est_scale_mm, phase_mm=phase, aniso=p.est_aniso, support_cells=p.est_support_cells)
    return np.array(kt, dtype=np.float32), np.array(ke, dtype=np.float32)


def drive_curve_lut(model: FaldModel, n: int = CURVE_N) -> np.ndarray:
    ln = np.linspace(CURVE_LOG_MIN, CURVE_LOG_MAX, n)
    return model.drive_of(np.exp(ln)).astype(np.float32)


def stat_ctx_words(p: FaldParams) -> bytes:
    """FLD5 words 42-47 for the P10 context statistic (kind 1 + m0, g_lit, floor, eps + one reserved zero). Raises
    ValueError for what the C++ loader would refuse — judged on the float32 values the file stores."""
    m0, g, fl, eps = (float(np.float32(x)) for x in (p.stat_ctx_m0, p.stat_ctx_g_lit, p.stat_ctx_floor, p.stat_ctx_eps))
    if not (m0 > 0.0 and np.isfinite(m0)) or not (0.0 <= g <= 1.0) or not (0.0 <= fl <= 1.0) or not (1e-4 <= eps <= 10.0):
        raise ValueError(f"context statistic (m0 {m0!r}, g_lit {g!r}, floor {fl!r}, eps {eps!r}) outside the loader's gate "
                         "(m0 finite > 0, g_lit and floor in 0..1, eps in [1e-4, 10])")
    return struct.pack("<I4fI", STAT_KIND_CODES["ctxpow"], m0, g, fl, eps, 0)


def boost_block(p: FaldParams) -> bytes:
    """FLD4 words 48-103 for ``p.boost_lut`` (56 words). Raises ValueError for what the C++ loader would refuse —
    judged on the float32 values the file stores."""
    lut = [(float(np.float32(lo)), float(np.float32(b))) for lo, b in p.boost_lut]
    if not 1 <= len(lut) <= BOOST_MAX_STEPS:
        raise ValueError(f"boost_lut has {len(lut)} steps; the panel file holds 1..{BOOST_MAX_STEPS}")
    for i, (lo, b) in enumerate(lut):
        if not (0.0 <= lo <= 1.0) or (i > 0 and not lo > lut[i - 1][0]) or not (BOOST_STEP_MIN <= b <= BOOST_STEP_MAX):
            raise ValueError(f"boost_lut step {i} ({lo!r}, {b!r}) outside the loader's gate (fractions 0..1 strictly "
                             f"ascending, boosts {BOOST_STEP_MIN:g}..{BOOST_STEP_MAX:g})")
    act = [float(np.float32(x)) for x in (p.boost_lit_nits, p.boost_lit_frac, p.boost_dim_nits, p.boost_dim_frac)]
    if not (0.0 <= act[0] <= 10000.0 and 0.0 <= act[2] <= 10000.0 and 0.0 <= act[1] < 1.0 and 0.0 <= act[3] < 1.0):
        raise ValueError(f"boost activation rule {act!r} outside the loader's gate (nits 0..10000, fractions 0..<1)")
    if p.boost_rule not in BOOST_RULE_CODES:
        raise ValueError(f"boost_rule must be one of {tuple(BOOST_RULE_CODES)}, got {p.boost_rule!r}")
    if p.boost_rule == "mean":
        g, t = float(np.float32(p.boost_mean_gamma)), float(np.float32(p.boost_mean_thresh))
        if not (0.0 < g <= 4.0) or not (t > 0.0 and np.isfinite(t)):
            raise ValueError(f"boost mean rule (gamma {g!r}, thresh {t!r}) outside the loader's gate (gamma in (0, 4], "
                             "thresh a finite number > 0)")
        rule = struct.pack("<I2f", BOOST_RULE_CODES["mean"], g, t)
    else:
        rule = struct.pack("<3I", 0, 0, 0)               # legacy LIT-or-DIM: the file stays byte for byte what C12 wrote
    flat = [x for step in lut for x in step] + [0.0] * (2 * (BOOST_MAX_STEPS - len(lut)))
    buf = struct.pack("<I", len(lut)) + struct.pack("<4f", *act) + rule + struct.pack(f"<{2 * BOOST_MAX_STEPS}f", *flat)
    assert len(buf) == 56 * 4
    return buf


def header_lattice_words(width: int, height: int, cols: int, rows: int) -> tuple[int, int, int]:
    """(word 4, word 5, word 31) of a panel file whose lattice is ``cols`` x ``rows`` zones over a ``width`` x ``height``
    frame from the origin (0, 0 — words 6/7): whole-pixel cells ``(cell_w, cell_h, 0)`` when the frame divides evenly
    (every file before 2026-10-19, byte for byte), else the lattice EXTENT ``(0, 0, width | height << 16)`` — a rational
    pitch (docs/fald-fractional-pitch.md). Raises ValueError for a lattice the C++ loader would refuse (more than 512
    zones per axis, a lattice over 65535 px, fewer pixels than zones)."""
    w, h, c, r = int(width), int(height), int(cols), int(rows)
    if not (1 <= c <= LATTICE_MAX_ZONES and 1 <= r <= LATTICE_MAX_ZONES):
        raise ValueError(f"{c} x {r} zones: the loader takes 1..{LATTICE_MAX_ZONES} per axis")
    if not (1 <= w <= LATTICE_MAX_PX and 1 <= h <= LATTICE_MAX_PX):
        raise ValueError(f"a {w} x {h}-px lattice: the panel file holds 1..{LATTICE_MAX_PX} px per axis")
    if w < c or h < r:
        raise ValueError(f"{c} x {r} zones on {w} x {h} px: fewer pixels than zones (a zone of 0 px)")
    if w % c == 0 and h % r == 0:
        return w // c, h // r, 0
    return 0, 0, w | (h << 16)


def zone_edge(index: int, count: int, extent: int) -> int:
    """C++ ``FaldZoneEdge`` / HLSL ``ZoneEdgeX``: the first pixel of zone ``index`` (0..count) of ``count`` zones over
    ``extent`` px — index * extent / count rounded to the nearest pixel edge, ties to the lower one."""
    return (2 * int(index) * int(extent) + int(count) - 1) // (2 * int(count))


def zone_of_pixel(local: int, count: int, extent: int) -> int:
    """C++ ``FaldZoneOfPixel`` / HLSL ``ZoneOfPixel``: the zone whose span holds pixel ``local`` (its centre's zone)."""
    return ((2 * int(local) + 1) * int(count)) // (2 * int(extent))


def lattice_fit(width: int, height: int, cols: int, rows: int) -> dict:
    """What the exported panel file's lattice will be on a ``width`` x ``height`` frame with a ``cols`` x ``rows`` zone
    grid — the SAME arithmetic :func:`export_panel_params` runs with ``frame=(width, height)``
    (:func:`header_lattice_words`), so a profiling run can know before it measures anything whether its file will load.

    Since 2026-10-19 (fractional zone pitch, docs/fald-fractional-pitch.md) the lattice IS the frame: whole-pixel cells
    when the frame divides evenly (the PA32UCXR's 3840x2160 / 48x48 → 80 x 45 px), else the lattice extent of a rational
    pitch (2560x1440 / 48x24 → 53.33 x 60 px, zones of 53 / 54 px). ``fits`` = the C++ loader accepts it (``refused`` =
    why not); a lattice that fits is always ``exact`` — it covers the frame, nothing is uncovered, no zone boundary
    drifts (``uncovered_px`` / ``max_drift_*`` stay in the record as zeros). ``fractional`` = the file carries the extent
    word (needs a DesktopLUT build from 2026-10-19 on). The model itself still renders on the
    :func:`dlc.fald.profile.choose_scale` canvas (``model_canvas_px``, its aspect off the frame's by
    ``model_aspect_error``): a modelling approximation the panel file does not carry."""
    from .profile import choose_scale
    W, H, c, r = int(width), int(height), int(cols), int(rows)
    try:
        w4, w5, w31 = header_lattice_words(W, H, c, r)
        refused = None
    except ValueError as e:
        w4 = w5 = w31 = 0
        refused = str(e)
    fits = refused is None
    pitch = (W / c, H / r) if c > 0 and r > 0 else (0.0, 0.0)
    rec = {"frame_px": [W, H], "zones": [c, r], "pitch_px": [round(pitch[0], 4), round(pitch[1], 4)],
           "cell_px": [round(pitch[0], 4), round(pitch[1], 4)], "fits": fits, "exact": fits, "refused": refused,
           "fractional": fits and w31 != 0, "header_cell_px": [w4, w5], "extent_word": w31,
           "lattice_px": [W, H], "uncovered_px": [0, 0], "max_drift_px": [0.0, 0.0], "max_drift_cells": [0.0, 0.0]}
    if fits:
        rec["zone_px"] = [[W // c, -(-W // c)], [H // r, -(-H // r)]]            # every zone is floor or ceil of the pitch
        _, cw, ch = choose_scale(W, H, c, r)
        rec["model_canvas_px"] = [int(cw), int(ch)]
        rec["model_aspect_error"] = round((cw / ch) / (W / H) - 1.0, 5)
    return rec


def export_panel_params(model: FaldModel, path: Path, gain_clip=(0.25, 4.0), frame: Optional[tuple[int, int]] = None) -> dict:
    """Write the panel file for ``model``. ``frame`` = the monitor's resolution (width, height) the file is for; default =
    the model canvas (``p.width``, ``p.height``) — the frame itself whenever :func:`dlc.fald.profile.choose_scale` could
    render it exactly. The lattice is ``frame`` (:func:`header_lattice_words`: whole-pixel cells, or the extent of a
    fractional pitch). A ``frame`` that differs from the canvas converts the two pixel-unit header words to frame pixels:
    ``stat_area0_px2`` (canvas px² → frame px²: the panel counts frame pixels) and the informational phase words 24/25;
    every other word / table is in cell units, nits or fractions and carries over. With ``frame`` = the canvas the bytes
    are what they were before 2026-10-19."""
    p = model.p
    fw, fh = (int(p.width), int(p.height)) if frame is None else (int(frame[0]), int(frame[1]))
    cells_w, cells_h, extent_word = header_lattice_words(fw, fh, p.cols, p.rows)     # validated before anything is written
    if (fw, fh) == (int(p.width), int(p.height)):
        area0, phase_px, phase_py = p.stat_area0_px2, p.est_phase_px, p.est_phase_py
    else:
        sx, sy = fw / float(p.width), fh / float(p.height)                  # frame px per canvas px
        area0, phase_px, phase_py = p.stat_area0_px2 * sx * sy, p.est_phase_px * sx, p.est_phase_py * sy
    kt, ke = kernel_tables(model)
    curve = drive_curve_lut(model)
    rt_r, rt_c = (kt.shape[2] - 1) // 2, (kt.shape[3] - 1) // 2
    re_r, re_c = (ke.shape[2] - 1) // 2, (ke.shape[3] - 1) // 2
    if p.transfer not in TRANSFER_CODES:
        raise ValueError(f"transfer must be 'pq' or 'gamma', got {p.transfer!r}")
    v2 = p.tmin_rgb is not None
    v3 = p.transfer == "gamma"                       # an SDR/ACM fit always carries its transfer (FLD3)
    v5 = p.stat_kind == "ctxpow"                     # the P10 context statistic: FLD5 (the FLD4 layout + words 42-46)
    v4 = len(p.boost_lut) > 0 or v5                  # a fit with a black-frame LED boost LUT: FLD4 (the long header + the block)
    if v3 and not (1.0 <= float(p.sdr_gamma) <= 4.0):
        raise ValueError(f"sdr_gamma {p.sdr_gamma!r} outside the loader's 1..4 gate")
    stat_words = stat_ctx_words(p) if v5 else struct.pack("<6I", *([0] * 6))   # validated before anything is written
    boost = (boost_block(p) if p.boost_lut else struct.pack("<56I", *([0] * 56))) if v4 else b""
    header = [MAGIC5 if v5 else (MAGIC4 if v4 else (MAGIC3 if v3 else (MAGIC2 if v2 else MAGIC))), p.cols, p.rows, p.sub,
              cells_w, cells_h, 0, 0, rt_c, rt_r, re_c, re_r, len(curve)]
    floats = [p.white_nits, p.tmin, area0, *p.chan_weights, gain_clip[0], gain_clip[1],
              p.drive_floor_nits, CURVE_LOG_MIN, CURVE_LOG_MAX, phase_px, phase_py]
    buf = (struct.pack("<13I", *header) + struct.pack("<13f", *floats) + struct.pack("<2f", p.fade_lo, p.fade_hi)
           + struct.pack("<f", p.gain_smooth_cells) + struct.pack("<2f", p.lum_fade_lo, p.lum_fade_hi)
           + struct.pack("<I", extent_word))
    assert len(buf) == 32 * 4
    if v2:
        custom = p.ped_chroma_gain != 1.0 or p.ped_chroma_lum_fade is not None
        clo, chi = p.chroma_lum_fade() if p.ped_chroma_lum_fade is not None else (0.0, 0.0)
        buf += (struct.pack("<3f", *(float(x) for x in p.tmin_rgb)) + struct.pack("<I", PED_MODE_CODES[p.ped_mode])
                + struct.pack("<3f", float(p.ped_chroma_gain) if custom else 0.0, float(clo), float(chi)) + struct.pack("<I", 0))
        assert len(buf) == 40 * 4
    elif v3 or v4:
        buf += struct.pack("<8I", *([0] * 8))          # no pedestal colour: words 32-39 zero (white pedestal, as FLD1)
        assert len(buf) == 40 * 4
    if v3 or v4:
        buf += (struct.pack("<I", TRANSFER_CODES[p.transfer]) + struct.pack("<f", float(p.sdr_gamma) if v3 else 0.0)
                + stat_words)
        assert len(buf) == 48 * 4
    if v4:
        buf += boost
        assert len(buf) == 104 * 4
    buf += curve.tobytes() + np.ascontiguousarray(kt).tobytes() + np.ascontiguousarray(ke).tobytes()
    Path(path).write_bytes(buf)
    fmt = "FLD5" if v5 else ("FLD4" if v4 else ("FLD3" if v3 else ("FLD2" if v2 else "FLD1")))
    return {"path": str(path), "bytes": len(buf), "k_true_shape": kt.shape, "k_est_shape": ke.shape,
            "curve_n": len(curve), "header_ints": header, "header_floats": floats,
            "format": fmt, "header_bytes": {"FLD1": 128, "FLD2": 160, "FLD3": 192, "FLD4": 416, "FLD5": 416}[fmt],
            "transfer": p.transfer, "sdr_gamma": float(p.sdr_gamma) if v3 else None,
            # the black-frame LED boost (FaldParams.boost_lut) travels in the FLD4 block (work guide C12): a fit with a
            # LUT always exports it (or the export raises), so the shader runs the same boost-aware model as Python
            "boost_lut_steps": len(p.boost_lut), "boost_in_file": bool(p.boost_lut),
            "boost_rule": p.boost_rule if p.boost_lut else None, "stat_kind": "ctxpow" if v5 else "area",
            # the zone lattice (docs/fald-fractional-pitch.md): the frame the file is for, how words 4/5 / 31 carry it,
            # the model canvas it was fitted on and the A0 written (converted to frame px² when the two differ)
            "lattice": {"frame_px": [fw, fh], "canvas_px": [int(p.width), int(p.height)],
                        "header_cell_px": [cells_w, cells_h], "extent_word": extent_word,
                        "fractional": extent_word != 0, "area0_px2": float(np.float32(area0))}}


def main(argv=None):
    import argparse, json
    from .correct import load_fitted_params
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("fit_json", type=Path)
    ap.add_argument("out_bin", type=Path)
    ap.add_argument("--gain-min", type=float, default=0.25)
    ap.add_argument("--gain-max", type=float, default=4.0)
    a = ap.parse_args(argv)
    model = FaldModel(load_fitted_params(a.fit_json))
    info = export_panel_params(model, a.out_bin, (a.gain_min, a.gain_max))
    print(json.dumps({k: (list(v) if isinstance(v, tuple) else v) for k, v in info.items()}, indent=1, default=str))


if __name__ == "__main__":
    main()
