"""Build a full-resolution per-channel 1D ``.cube`` EOTF correction for the HDR MHC base.

This replaces the coarse 32-point ``mhc.set_base_grayscale`` IPC table (capped at
``kMaxMhcGrayscalePoints``) — far too sparse for a PQ EOTF — with the path ColourSpace
and DisplayCal use: a per-channel 1D LUT that DesktopLUT imports over ``mhc.set_base_lut``
(``sourceIs1DCube`` -> ``GenerateMHC2Profile`` -> the 4096-entry MHC2 LUT).

Grounding (no handwaving — matched to DesktopLUT's own C++):

* **Pipeline** (``mhc_icc.cpp`` ``ComputeMHC2Matrix``):
  ``wire -> DeGamma -> RGBtoXYZ -> [MHC2 matrix] -> XYZtoRGB -> ReGamma -> LUT -> display``.
  The per-channel 1D LUT is the *final* per-channel signal->signal remap, applied AFTER
  the matrix. So the matrix owns absolute primaries + white (one native->D65 rotation) and
  the cube owns per-channel **tone** — including the per-level grayscale tracking drift a
  single matrix rotation cannot fix. This is what maximises grayscale quality.

* **Math** ported from ``mhc.cpp`` (``PqEOTF`` / ``PqOETF`` / ``InvertTRC``). ST 2084 constants
  are identical to ``engine.patches``.

* **The cube is derived from the GRAY RAMP, not pure-channel ramps** (HW evidence,
  2026-06-20). An earlier build inverted each channel's OWN pure-channel ramp normalised to its
  own max. On a local-dimming mini-LED that is WRONG: the panel is strongly non-additive — a
  gray patch reads only 70-84% of the sum of the pure R/G/B luminances in the shadows (the
  backlight drives differently with one channel lit vs all three), and the deficit is
  channel-dependent. So a pure-ramp cube mis-corrects each channel by a different amount in the
  shadows and the neutral axis drifts green/yellow (measured: dy +0.099 at sig 0.09, grayscale
  dE_ITP regressed 4.9 -> 11.4). The fix mirrors the proven SDR gray-balance solver
  (``refine.propose_correction_grayscale``): derive each channel's transfer from the gray ramp
  itself via the primaries matrix, where non-additivity is already baked in.

  Per gray level ``s`` (measured neutral XYZ ``M``), with ``P`` = the linear-RGB->XYZ matrix
  built from the measured primaries + native white at the peak:
      ``share_c(s) = (P^-1 · M(s))_c``                # channel c's linear share IN GRAY
      ``frac(s)    = min(PqEOTF(s)*10000, peak) / peak``  # PQ target as a fraction of peak
      ``cube_c[j]  = invert(share_c, frac(j/(N-1)))``  # signal that hits the native-white-proportional target
  Target = equal linear-RGB shares (= native white by construction of ``P``), so inverting each
  channel's *measured* (drifting) share neutralises per-level drift toward NATIVE white; the
  MHC matrix then rotates native->D65 once, uniformly, and lands neutral at every level. At peak
  ``frac=1`` -> ``cube_c=1`` (full), so the cube does NOT fight the matrix's white move at the top.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

from .colormath import invert3x3, matvec, rgb_to_xyz_matrix, xy_to_XYZ
from .mhc import Ti3Sample

# ST 2084 lives in dlc._pq (the one shared stdlib copy — the Phase 1 audit consolidated
# the four verbatim ports of mhc.cpp PqEOTF/PqOETF). Re-exported here because the cube
# math and its tests speak `mhc_cube.pq_eotf`/`pq_oetf`.
from ._pq import CONTAINER_NITS as _PQ_CONTAINER_NITS
from ._pq import eotf_norm as pq_eotf
from ._pq import oetf_norm as pq_oetf

__all__ = [
    "pq_eotf",
    "pq_oetf",
    "invert_trc",
    "invert_monotone",
    "peak_chroma_luminance",
    "adaptive_dark_floor",
    "noise_trust",
    "dark_trust_weights",
    "HDR_REFERENCE_WHITE_BAND",
    "mhc2_matrix",
    "build_hdr_cube",
    "build_sdr_cube",
    "refine_hdr_cube",
    "refine_sdr_cube",
    "refine_sdr_grayscale",
    "write_1d_cube",
    "read_1d_cube",
]

_D65 = (0.3127, 0.3290)


# BT.2408 diffuse / graphics ("HDR reference") white sits at 203 nits; SDR reference ~100. This
# band is the panel's STABLE, well-lit-but-pre-overdrive neutral — the trustworthy chromaticity to
# judge dark drift against on HDR, where the BRIGHTEST patch is the panel in overdrive/ABL/thermal
# limit (a moving, untrustworthy reference), not a target.
HDR_REFERENCE_WHITE_BAND = (100.0, 203.0)


def _median(vals: Sequence[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def adaptive_dark_floor(neutral_reads: Sequence[tuple[float, float, float]], *,
                        reference_band: tuple[float, float] | None = None,
                        chroma_tolerance: float = 0.008,
                        bounds: tuple[float, float] = (0.1, 5.0),
                        default_floor_nits: float = 0.3
                        ) -> tuple[float, dict]:
    """Derive — from MEASURED dark behaviour, not a hardcoded cap — the luminance below which a
    per-level grayscale correction chases noise and should blend to identity.

    Below some luminance the meter's chroma reading wanders (sensor noise) and/or the panel's dark
    output is unstable; either way a per-channel white-balance correction there is untrustworthy.
    Find that luminance from the data: take a TRUSTWORTHY reference chromaticity, then walk the dark
    reads and flag any whose chromaticity strays from it by more than ``chroma_tolerance``. The floor
    is the brightest such strayed dark read — smooth to identity below it. Noise or instability, the
    measured drift sets the floor either way.

    The reference depends on the mode:
      * ``reference_band=(lo,hi)`` (**HDR**): the median chromaticity of reads in the stable diffuse-
        white band (default :data:`HDR_REFERENCE_WHITE_BAND`, 100-203 nits). On HDR the BRIGHTEST
        patch is the panel pushed into overdrive/ABL/thermal limit — a shifting, untrustworthy
        reference — so we anchor on the pre-overdrive diffuse white instead. Falls back to the
        brightest read at/below the band (then the dimmest) when nothing lands in-band.
      * ``None`` (**SDR**): the brightest read — on SDR the peak IS the calibration target white and
        is stable, so it's the right anchor.

    ``neutral_reads``: ``[(nits, x, y), ...]`` (any order; nits>0). Returns ``(floor_nits, info)``
    clamped to ``bounds``; a clean dark region returns ``bounds[0]``; too few reads → ``default_floor_nits``.
    """
    reads = [(float(n), float(x), float(y)) for (n, x, y) in neutral_reads
             if n is not None and n > 0.0 and x is not None and y is not None]
    if len(reads) < 3:
        return default_floor_nits, {"reason": "too_few_reads", "n_reads": len(reads)}
    reads.sort(key=lambda r: r[0])
    nits = [r[0] for r in reads]
    if reference_band is not None:
        lo_b, hi_b = reference_band
        band = [r for r in reads if lo_b <= r[0] <= hi_b]
        if band:
            ref_x, ref_y = _median([r[1] for r in band]), _median([r[2] for r in band])
            ref_nits, ref_source = _median([r[0] for r in band]), "diffuse_white_band"
        else:
            below = [r for r in reads if r[0] <= hi_b]   # avoid the overdriven peak when out-of-band
            ref_nits, ref_x, ref_y = (below[-1] if below else reads[0])
            ref_source = "nearest_below_band" if below else "dimmest_above_band"
    else:
        ref_nits, ref_x, ref_y = reads[-1]               # SDR: the brightest read = the target white
        ref_source = "brightest"
    # Floor candidates = the dark reads (below the reference); the bounds clamp keeps a mid-tone
    # chroma error from inflating the floor even if it slips through.
    cutoff = min(nits[len(nits) // 2], ref_nits)
    strayed = []
    max_drift = 0.0
    for n, x, y in reads:
        drift = ((x - ref_x) ** 2 + (y - ref_y) ** 2) ** 0.5
        max_drift = max(max_drift, drift)
        if n <= cutoff and drift > chroma_tolerance:
            strayed.append(n)
    lo, hi = bounds
    if not strayed:
        floor, reason = lo, "clean_dark_region"
    else:
        floor, reason = min(max(max(strayed), lo), hi), "chroma_drift"
    return floor, {"reason": reason, "n_reads": len(reads),
                   "ref_xy": [round(ref_x, 5), round(ref_y, 5)], "ref_nits": round(ref_nits, 3),
                   "ref_source": ref_source, "max_chroma_drift": round(max_drift, 5),
                   "n_strayed": len(strayed), "chroma_tolerance": chroma_tolerance}


def mhc2_matrix(native_primaries: Mapping[str, float], native_white_xy: tuple[float, float],
                target_primaries: Mapping[str, float],
                target_white_xy: tuple[float, float] = _D65) -> list[list[float]]:
    """The RGB->RGB MHC2 tag matrix Windows applies to linear RGB: ``inv(disp)·src``.

    Mirrors ``mhc_icc.cpp`` ``ComputeMHC2Matrix`` (see [[mhc2-matrix-order-rgb-not-xyz]]):
    ``src`` = the standard-source (e.g. Rec.2020 @ D65) linear-RGB->XYZ matrix; ``disp`` = the
    measured native panel's linear-RGB->XYZ matrix (its primaries + native white). Both are
    white-normalised (Y=1), so ``M @ (1,1,1)`` = the per-channel native drive that reproduces the
    TARGET white — the post-matrix neutral signal the per-channel cube is indexed at (the
    ``matrix_rowsums`` :func:`refine_hdr_cube` needs). Identity primaries+white => identity matrix.
    """
    disp = rgb_to_xyz_matrix(
        native_primaries["rx"], native_primaries["ry"], native_primaries["gx"],
        native_primaries["gy"], native_primaries["bx"], native_primaries["by"],
        native_white_xy[0], native_white_xy[1], white_Y=1.0)
    src = rgb_to_xyz_matrix(
        target_primaries["rx"], target_primaries["ry"], target_primaries["gx"],
        target_primaries["gy"], target_primaries["bx"], target_primaries["by"],
        target_white_xy[0], target_white_xy[1], white_Y=1.0)
    disp_inv = invert3x3(disp)
    return [[sum(disp_inv[r][k] * src[k][c] for k in range(3)) for c in range(3)] for r in range(3)]


def peak_chroma_luminance(channel_peak_xyz: Sequence[Sequence[float]],
                          target_white_xy: tuple[float, float] = _D65) -> tuple[float, str]:
    """The brightest target-white (default D65) luminance the panel can render with EVERY channel
    inside full drive — ColourSpace's "Peak Chroma" point.

    A warm panel cannot cool its white at full drive (the cold channel has no headroom), so holding
    the target white from 0->1 means capping peak luminance. The binding constraint is the MEASURED
    per-channel peak luminance, NOT the chromaticity-primaries matrix: re-normalising primaries to a
    white point discards the per-channel peak ratios (e.g. this panel's blue peaks at only ~147 nits
    vs green ~1142), which is exactly what governs real headroom.

    ``channel_peak_xyz``: the three measured full-drive primary XYZ triples (R, G, B) — the columns
    of the additive display matrix ``disp``, in absolute nits. For a target-white of luminance ``Y``
    the per-channel drive shares are ``disp^-1 @ XYZ(target, Y)``; they scale linearly in ``Y``, so
    the cap is where the largest share reaches 1.0 (the channel hits full drive).

    Returns ``(cap_nits, binding_channel)`` — the ADDITIVE headroom limit and the additively-binding
    channel. NOTE this is a NOMINAL cap: it ignores non-additivity, which on a sub-additive panel
    pushes the dim (cold) channel's real drive higher and trims the achievable cap a little further
    (here ~1734 additive / green-edge vs ~1704 non-additive / blue-binding — the two are near-tied).
    The closed-loop grayscale refine measures the real panel and lands the achievable D65 peak; treat
    this as the seed, not the exact landing luminance. Native peak (all shares 1.0) bounds it above.
    """
    disp = [[channel_peak_xyz[c][row] for c in range(3)] for row in range(3)]  # columns = R,G,B peaks
    native_peak = sum(channel_peak_xyz[c][1] for c in range(3))                # additive full-white Y
    if native_peak <= 0.0:
        raise ValueError("channel peak luminance must be positive")
    disp_inv = invert3x3(disp)
    shares_per_nit = matvec(disp_inv, xy_to_XYZ(target_white_xy[0], target_white_xy[1], 1.0))
    binding = max(range(3), key=lambda c: shares_per_nit[c])
    if shares_per_nit[binding] <= 0.0:
        raise ValueError("degenerate primaries: non-positive target-white share")
    cap = 1.0 / shares_per_nit[binding]
    return min(cap, native_peak), _CHANNELS[binding]


_CHANNELS = ("r", "g", "b")


def noise_trust(error: float, noise: Optional[float], *,
                lo_snr: float = 1.0, hi_snr: float = 3.0) -> float:
    """How much to trust a measured correction of magnitude ``error`` against the per-reading
    measurement ``noise`` (same units) — i.e. how much of it to apply vs. smooth to identity.

    Returns ``w`` in [0,1]: **0** when the correction is within the noise (``error <= lo_snr·noise``
    — indistinguishable from the measurement uncertainty, so applying it just chases noise → smooth
    to identity), **1** when it clearly exceeds it (``error >= hi_snr·noise`` — a real signal worth
    correcting), a smoothstep between. ``noise`` is the **standard error of the mean** chromaticity
    (per-read σ / √reads) estimated from MULTIPLE readings, so it shrinks as reads accumulate — more
    readings → tighter → the same real error clears the gate ("more readings → trust the reading
    more"). A level the measure loop flags **unstable** (can't be pinned after many reads = genuine
    display fluctuation, not averageable) is passed ``noise = +inf`` by the caller ⇒ ``w = 0`` (never
    bake a correction to a chromaticity the panel won't hold). ``noise`` None/≤0 ⇒ trust fully (1.0)."""
    if noise is None or noise <= 0.0:
        return 1.0
    snr = error / noise
    if snr <= lo_snr:
        return 0.0
    if snr >= hi_snr:
        return 1.0
    t = (snr - lo_snr) / (hi_snr - lo_snr)
    return t * t * (3.0 - 2.0 * t)


def dark_trust_weights(levels: Sequence[tuple[float, float, float, Optional[float]]],
                       reference_white_xy: tuple[float, float], *,
                       lo_snr: float = 1.0, hi_snr: float = 3.0) -> list[tuple[float, float]]:
    """Per-level trust weights from MEASURED repeatability — the core of the dark-level logic.

    ``levels``: ``[(signal, x, y, noise), ...]`` per measured neutral level, where ``noise`` is the
    measurement uncertainty of that level's chromaticity — the **standard error of the mean** in
    ``xy`` (per-read σ / √reads), or ``+inf`` for an ``unstable`` level (caller's choice; ⇒ trust 0),
    or ``None`` for <2 reads. ``reference_white_xy`` is the chromaticity the per-level correction
    drives toward (native white for ``build_hdr_cube``'s per-level share correction; D65 for the
    closed-loop refine).

    For each level the chroma error to correct is ``|measured_xy - reference|`` and the trust is
    ``noise_trust(error, chroma_sigma)`` — so where the dark read's chromaticity is so noisy/unstable
    that the error can't be distinguished from the spread, the weight collapses toward 0 (smooth that
    level's correction to identity); where the read is stable, it stays ~1. Returns sorted
    ``[(signal, w), ...]``; levels with no σ (single read) get ``w=1`` (trust the adaptive integration)."""
    rx, ry = reference_white_xy
    out: list[tuple[float, float]] = []
    for sig, x, y, sigma in levels:
        err = ((x - rx) ** 2 + (y - ry) ** 2) ** 0.5
        out.append((float(sig), noise_trust(err, sigma, lo_snr=lo_snr, hi_snr=hi_snr)))
    out.sort(key=lambda p: p[0])
    return out


def invert_trc(trc: Sequence[float], target: float) -> float:
    """Given an evenly-sampled monotonic TRC (signal->light, ``trc[k]`` at input ``k/(n-1)``),
    return the input signal in [0, 1] that yields ``target`` light. Port of ``mhc.cpp`` InvertTRC
    (binary search + linear interpolation, clamped at both ends)."""
    n = len(trc)
    if n < 2:
        return target
    if target <= trc[0]:
        return 0.0
    if target >= trc[-1]:
        return 1.0
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if trc[mid] <= target:
            lo = mid
        else:
            hi = mid
    denom = trc[hi] - trc[lo]
    t = (target - trc[lo]) / denom if denom > 0.0 else 0.0
    return (lo + t) / (n - 1)


def invert_monotone(xs: Sequence[float], ys: Sequence[float], target: float) -> float:
    """Given a measured monotonic-nondecreasing mapping ``ys(xs)`` (``xs`` ascending), return the
    ``x`` in ``[xs[0], xs[-1]]`` where ``ys == target`` (linear interp, clamped at both ends).
    Used to invert a channel's measured signal->light(share) curve back to the driving signal."""
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1 or target <= ys[0]:
        return xs[0]
    if target >= ys[-1]:
        return xs[-1]
    for k in range(1, n):
        if ys[k] >= target:
            y0, y1, x0, x1 = ys[k - 1], ys[k], xs[k - 1], xs[k]
            denom = y1 - y0
            t = (target - y0) / denom if denom > 1e-12 else 0.0
            return x0 + (x1 - x0) * t
    return xs[-1]


def _gray_shares(samples: Sequence[Ti3Sample], primaries: Mapping[str, float],
                 white_xy: tuple[float, float], peak_luminance: float,
                 *, eps: float = 1e-4) -> tuple[list[float], dict[str, list[float]]]:
    """Per-channel LINEAR shares along the measured gray ramp, via the primaries matrix.

    ``share_c(s) = (P^-1 · M(s))_c`` where ``P`` reproduces the native white at ``peak_luminance``
    for RGB=(1,1,1). Because the gray patches are measured with all channels lit, non-additivity
    is already in the data. Returns ``(signals, {"r":[...], "g":[...], "b":[...]})`` sorted by
    signal, each channel monotone-enforced (so it inverts cleanly). The shares are ~equal where
    the panel tracks native white and diverge where it drifts."""
    P = rgb_to_xyz_matrix(
        primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
        primaries["bx"], primaries["by"], white_xy[0], white_xy[1], white_Y=peak_luminance,
    )
    Pinv = invert3x3(P)
    pts: dict[float, tuple[float, float, float]] = {}
    for s in samples:
        rgb = s.rgb
        if abs(rgb[0] - rgb[1]) < eps and abs(rgb[1] - rgb[2]) < eps:
            sig = rgb[0]
            if sig not in pts or sig == rgb[0]:  # last write wins; gray reads are unique per level
                pts[sig] = matvec(Pinv, s.xyz)
    sigs = sorted(pts)
    shares: dict[str, list[float]] = {"r": [], "g": [], "b": []}
    prev = {"r": 0.0, "g": 0.0, "b": 0.0}
    for sig in sigs:
        v = pts[sig]
        for ci, ch in enumerate(_CHANNELS):
            x = max(prev[ch], v[ci])      # enforce monotone non-decreasing for a clean inverse
            shares[ch].append(x)
            prev[ch] = x
    return sigs, shares


def build_hdr_cube(samples: Sequence[Ti3Sample], primaries: Mapping[str, float],
                   white_xy: tuple[float, float], peak_luminance: float,
                   *, lut_size: int = 1024, dark_floor_nits: float = 0.3,
                   level_trust: Optional[Sequence[tuple[float, float]]] = None
                   ) -> tuple[dict[str, list[float]], dict[str, float]]:
    """Build the per-channel HDR EOTF+WB correction cube from a raw TI3's GRAY ramp.

    ``primaries`` is the measured native gamut (``rx..by``), ``white_xy`` the measured native
    white, ``peak_luminance`` the panel peak (brightest neutral). Returns
    ``({"r":[...], "g":[...], "b":[...]}, summary)``. Each channel curve maps the PQ input signal
    to the signal that drives the channel's measured gray share to the native-white-proportional
    PQ target — neutralising per-level drift toward native white (the MHC matrix then does
    native->D65). Raises ``ValueError`` if the gray ramp is too sparse.

    ``dark_floor_nits`` (default 0.3): below this measured neutral luminance the per-channel
    correction is blended to IDENTITY. High-end colorimeters only spec chromaticity (xy ±0.002)
    above ~0.3 nit (Klein K10-A >0.33; JETI specbos ≥10; CS-2000A loosens <0.05) — below that
    a per-level WB correction just chases meter noise and over-corrects (HW 2026-06-20: native
    gray y at sig 0.05 read 0.32 in one run, 0.42 in the next). Holding identity leaves the
    panel's (well-behaved) native shadow tracking + the matrix's white move to do it. 0.3 nit
    (PQ signal ~0.117) is the colorimeter-grounded floor; 1 nit was needlessly conservative.

    ``level_trust`` (optional): ``[(signal, w), ...]`` per-level trust weights from MEASURED
    repeatability (:func:`dark_trust_weights`). When given, the per-level correction is additionally
    scaled by the trust interpolated to each grid point — so a dark level whose chromaticity is too
    noisy/unstable to trust is smoothed to identity REGARDLESS of the static nit floor. This is the
    measurement-driven dark logic; the ``dark_floor_nits`` ramp remains as a luminance guard/fallback."""
    if peak_luminance <= 0.0:
        raise ValueError("peak_luminance must be positive")
    sigs, shares = _gray_shares(samples, primaries, white_xy, peak_luminance)
    if len(sigs) < 2:
        raise ValueError("fewer than 2 neutral patches; cannot build a gray-ramp cube")
    # Peak linear share per channel (≈1.0 each by construction of P at the native white).
    peak_share = {ch: shares[ch][-1] for ch in _CHANNELS}
    # Signal below which a level's luminance is under the trustworthy floor (blend to identity).
    dark_sig = pq_oetf(min(dark_floor_nits, peak_luminance) / _PQ_CONTAINER_NITS)
    # Measurement-driven per-level trust (sig→w), interpolated to the grid; 1.0 everywhere when absent.
    trust_xs = [p[0] for p in level_trust] if level_trust else []
    trust_ws = [p[1] for p in level_trust] if level_trust else []

    curves: dict[str, list[float]] = {ch: [] for ch in _CHANNELS}
    prev = {ch: 0.0 for ch in _CHANNELS}
    for j in range(lut_size):
        pq_in = j / (lut_size - 1)
        lin_nits = min(pq_eotf(pq_in) * _PQ_CONTAINER_NITS, peak_luminance)
        frac = lin_nits / peak_luminance              # native-white-proportional target (0..1)
        # Luminance floor ramp: 0 in the noisy deep shadows, ramping to 1 by 2x the dark-floor signal.
        w = 0.0 if dark_sig <= 0 else min(1.0, max(0.0, (pq_in - dark_sig) / dark_sig))
        # Fold in the measured per-level trust: where the dark read is too noisy/unstable to trust
        # (low w_noise), smooth that level's correction to identity even above the luminance floor.
        if trust_xs:
            w *= _interp(pq_in, trust_xs, trust_ws)
        for ch in _CHANNELS:
            target_share = frac * peak_share[ch]
            corrected = invert_monotone(sigs, shares[ch], target_share)
            val = w * corrected + (1.0 - w) * pq_in   # blend to identity in the dark
            # Enforce monotone non-decreasing — the blend boundary or a noisy share can dip,
            # and the MHC2 LUT must be monotone for a well-defined inverse.
            val = max(prev[ch], min(max(val, 0.0), 1.0))
            prev[ch] = val
            curves[ch].append(val)

    summary: dict[str, float] = {
        "white_max_nits": round(peak_luminance, 4),
        "gray_points": float(len(sigs)),
        "lut_size": float(lut_size),
        "dark_floor_nits": round(dark_floor_nits, 4),
        "basis": "gray-ramp",
    }
    for ch in _CHANNELS:
        summary[f"{ch}_peak_share"] = round(peak_share[ch], 4)
    return curves, summary


def build_sdr_cube(samples: Sequence[Ti3Sample], primaries: Mapping[str, float],
                   white_xy: tuple[float, float], peak_luminance: float,
                   *, gamma: float = 2.2, lut_size: int = 1024, dark_floor_nits: float = 0.3,
                   level_trust: Optional[Sequence[tuple[float, float]]] = None
                   ) -> tuple[dict[str, list[float]], dict[str, float]]:
    """Build the per-channel **SDR** EOTF+WB correction cube from a raw TI3's GRAY ramp.

    The power-law (γ) analog of :func:`build_hdr_cube`: identical gray-share inversion, but the
    transfer is ``light = signal**gamma`` instead of PQ. ``primaries`` is the measured native
    gamut, ``white_xy`` the measured native white, ``peak_luminance`` the panel peak (= the SDR
    target white luminance). Returns ``({"r","g","b"}, summary)``; each channel curve maps the wire
    signal to the drive that pulls the channel's measured gray share to the native-white-proportional
    target — neutralising per-level drift toward native white, leaving the MHC matrix to do native→D65.

    Delivered via ``mhc.set_base_lut`` (``sourceIs1DCube`` → DesktopLUT's 1024-entry SDR MHC2 LUT)
    so the neutral correction is a DLC-owned base artifact — it locks DesktopLUT's grayscale editor
    (and its Reset button) and never squats in the user-editable ``correctionGrayscale`` fine-tune
    slot (see [[dlc-must-not-own-mhc-user-layers]]). Raises ``ValueError`` if the gray ramp is too
    sparse. ``dark_floor_nits`` / ``level_trust`` behave exactly as in :func:`build_hdr_cube`."""
    if peak_luminance <= 0.0:
        raise ValueError("peak_luminance must be positive")
    sigs, shares = _gray_shares(samples, primaries, white_xy, peak_luminance)
    if len(sigs) < 2:
        raise ValueError("fewer than 2 neutral patches; cannot build a gray-ramp cube")
    peak_share = {ch: shares[ch][-1] for ch in _CHANNELS}
    # Signal below which a level's luminance is under the trustworthy floor (blend to identity).
    # SDR transfer is pure power γ, so the floor luminance maps back through the inverse EOTF.
    floor_frac = min(dark_floor_nits, peak_luminance) / peak_luminance
    dark_sig = max(0.0, floor_frac) ** (1.0 / gamma)
    trust_xs = [p[0] for p in level_trust] if level_trust else []
    trust_ws = [p[1] for p in level_trust] if level_trust else []

    curves: dict[str, list[float]] = {ch: [] for ch in _CHANNELS}
    prev = {ch: 0.0 for ch in _CHANNELS}
    for j in range(lut_size):
        sig_in = j / (lut_size - 1)
        frac = sig_in ** gamma                        # native-white-proportional target (0..1)
        # Luminance floor ramp: 0 in the noisy deep shadows, ramping to 1 by 2x the dark-floor signal.
        w = 0.0 if dark_sig <= 0 else min(1.0, max(0.0, (sig_in - dark_sig) / dark_sig))
        if trust_xs:
            w *= _interp(sig_in, trust_xs, trust_ws)
        for ch in _CHANNELS:
            target_share = frac * peak_share[ch]
            corrected = invert_monotone(sigs, shares[ch], target_share)
            val = w * corrected + (1.0 - w) * sig_in  # blend to identity in the dark
            # Enforce monotone non-decreasing — the blend boundary or a noisy share can dip, and
            # the MHC2 LUT must be monotone for a well-defined inverse.
            val = max(prev[ch], min(max(val, 0.0), 1.0))
            prev[ch] = val
            curves[ch].append(val)

    summary: dict[str, float] = {
        "white_max_nits": round(peak_luminance, 4),
        "gray_points": float(len(sigs)),
        "lut_size": float(lut_size),
        "gamma": round(gamma, 4),
        "dark_floor_nits": round(dark_floor_nits, 4),
        "basis": "gray-ramp",
    }
    for ch in _CHANNELS:
        summary[f"{ch}_peak_share"] = round(peak_share[ch], 4)
    return curves, summary


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """Linear interpolation of ys(xs) at x (xs ascending), clamped to the end values."""
    if not xs:
        return 1.0
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for k in range(1, len(xs)):
        if xs[k] >= x:
            x0, x1, y0, y1 = xs[k - 1], xs[k], ys[k - 1], ys[k]
            t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
            return y0 + (y1 - y0) * t
    return ys[-1]


def refine_hdr_cube(current_curves: Mapping[str, Sequence[float]],
                    measured_neutral: Sequence[tuple[float, Sequence[float]]],
                    channel_peak_xyz: Sequence[Sequence[float]],
                    matrix_rowsums: Sequence[float],
                    *, peak_cap_nits: float, target_white_xy: tuple[float, float] = _D65,
                    damping: float = 0.85, dark_floor_nits: float = 1.0,
                    ratio_clamp: tuple[float, float] = (0.5, 2.0)
                    ) -> dict[str, list[float]]:
    """One closed-loop grayscale-refine step on the HDR base cube — PANEL-AGNOSTIC.

    Pulls the measured neutral axis toward the target white (default D65) by the proven share-ratio
    law (same as ``refine.propose_correction_grayscale``, but for the per-channel PQ cube). Uses
    ONLY measured inputs — no forward model, no hardcoded panel magnitudes — so it converges to
    whatever the real panel does over a few measure->refine rounds (the production HDR analog of
    the SDR refine loop).

    ``current_curves``    : the installed per-channel cube (r/g/b signal->drive, ``lut_size`` each).
    ``measured_neutral``  : ``[(wire_signal, measured_XYZ[, chroma_sigma]), ...]`` from a neutral-ramp
                            measurement with the current cube applied. The optional 3rd element is the
                            level's read-to-read chromaticity spread (``xy``) from MULTIPLE reads; when
                            present, that level's correction is scaled toward identity by its
                            measurement trust (:func:`noise_trust`) so dark/noisy levels don't bake a tint.
    ``channel_peak_xyz``  : measured native full-drive primary XYZ (R,G,B) — the linear-share basis.
    ``matrix_rowsums``    : ``v_c = (M @ (1,1,1))_c`` for the installed MHC2 matrix (:func:`mhc2_matrix`).

    **Abscissa convention (the load-bearing detail).** Windows applies this per-channel LUT AFTER
    the MHC2 matrix (``...-> matrix -> ReGamma -> LUT -> display``; see ``mhc_icc.cpp``), so for a wire
    neutral of signal ``s`` channel ``c`` is driven at the *post-matrix* index
    ``pq_oetf(rowsum_c * pq_eotf(s))`` -- NOT ``s``. The factor measured at wire ``s`` is therefore
    stored against that post-matrix abscissa, and the cube is indexed in the SAME post-matrix space
    (``cur[j]`` == the LUT value at post-matrix signal ``j/(N-1)``). Because the factor comes from a
    real measurement at the real abscissa, the loop converges to measured D65 even when the matrix is
    non-identity (the actual hardware case) -- HW-validated at 2.28 dE_ITP grey on the PA32UCXR with
    ``rowsums = M@(1,1,1) != (1,1,1)``. (``build_hdr_cube`` seeds the cube in *wire*-signal space;
    this measurement-driven refine corrects whatever that seed actually renders.)

    ``peak_cap_nits``     : the Peak-Chroma luminance cap (target white saturates here at the top).

    Returns updated per-channel curves (monotone, clamped). Measured points below ``dark_floor_nits``
    (meter noise) are dropped; the first surviving point's factor is held flat below it and the last
    point's factor flat above it (no extrapolation), keeping the refined curve monotone through both ends.
    """
    chans = ("r", "g", "b")
    N = len(current_curves["r"])
    if N < 2 or len(current_curves["g"]) != N or len(current_curves["b"]) != N:
        raise ValueError("current_curves must be three equal-length channels")
    disp = [[channel_peak_xyz[c][row] for c in range(3)] for row in range(3)]
    disp_inv = invert3x3(disp)
    wx, wy = target_white_xy

    # gather (post-matrix signal, linear-light correction factor) per channel from the measurement
    pts: dict[int, list[tuple[float, float]]] = {0: [], 1: [], 2: []}
    for entry in sorted(measured_neutral, key=lambda p: p[0]):
        sig, xyz = entry[0], entry[1]
        chroma_sigma = entry[2] if len(entry) > 2 else None   # per-level read-to-read xy spread, if measured
        lin = pq_eotf(sig)
        tY = min(lin * 10000.0, peak_cap_nits)
        if tY < dark_floor_nits:
            continue
        # Measurement-trust: if MULTIPLE reads gave a chromaticity spread, scale this level's whole
        # correction toward identity when its chroma error is within the noise (don't bake a per-channel
        # tint from noise/instability at a dark level). w=1 when no σ (trust the adaptive integration).
        total = sum(xyz) or 1.0
        mx, my = xyz[0] / total, xyz[1] / total
        w_trust = noise_trust(((mx - wx) ** 2 + (my - wy) ** 2) ** 0.5, chroma_sigma)
        ms = matvec(disp_inv, xyz)
        ts = matvec(disp_inv, xy_to_XYZ(wx, wy, tY))
        for c in range(3):
            ratio = ts[c] / ms[c] if ms[c] > 1e-9 else 1.0
            ratio = min(max(ratio, ratio_clamp[0]), ratio_clamp[1])
            damped = 1.0 + w_trust * (ratio ** damping - 1.0)   # toward identity by (1-w_trust)
            post_sig = pq_oetf(min(max(matrix_rowsums[c] * lin, 0.0), 1.0))
            pts[c].append((post_sig, damped))

    out: dict[str, list[float]] = {}
    for c, ch in enumerate(chans):
        xs = [p[0] for p in pts[c]]
        fs = [p[1] for p in pts[c]]
        cur = current_curves[ch]
        curve: list[float] = []
        prev = 0.0
        for j in range(N):
            sig = j / (N - 1)
            # Below the first measured point, HOLD that point's factor flat (not 1.0): the seed cube
            # already blends toward identity in the dark (build_hdr_cube dark_floor), and a flat hold
            # keeps the refined curve monotone through the onset — snapping to 1.0 there would make
            # the monotone clamp below mask a reduction at the first measured signal.
            factor = _interp(sig, xs, fs) if xs else 1.0
            new_lin = pq_eotf(float(cur[j])) * factor
            val = pq_oetf(new_lin)
            val = max(prev, min(max(val, 0.0), 1.0))               # monotone, clamped
            prev = val
            curve.append(val)
        out[ch] = curve
    return out


def refine_sdr_cube(current_curves: Mapping[str, Sequence[float]],
                    measured_neutral: Sequence[tuple[float, Sequence[float]]],
                    primaries: Mapping[str, float], native_white_xy: tuple[float, float],
                    peak_luminance: float, matrix_rowsums: Sequence[float],
                    *, gamma: float = 2.2, target_white_xy: tuple[float, float] = _D65,
                    damping: float = 0.85, dark_floor_nits: float = 0.5,
                    ratio_clamp: tuple[float, float] = (0.5, 2.0)
                    ) -> dict[str, list[float]]:
    """One closed-loop grayscale-refine step on the **SDR base cube** — PANEL-AGNOSTIC.

    The power-law (γ) analog of :func:`refine_hdr_cube`, and the cube-delivered replacement for
    :func:`refine_sdr_grayscale` (which emitted ``correctionGrayscale`` deviations — the user slot).
    Same damped share-ratio law and post-matrix abscissa, but the transfer is ``light = signal**γ``
    (not PQ) and the result is composed onto the installed per-channel ``.cube`` (signal→signal),
    pushed via ``mhc.set_base_lut``. Measurement-only (no forward model) so it converges to whatever
    the real panel does over a few measure→refine rounds.

    ``current_curves``    : the installed per-channel cube (r/g/b signal→drive, ``lut_size`` each).
    ``measured_neutral``  : ``[(wire_signal, measured_XYZ[, chroma_sigma]), ...]`` from a neutral-ramp
                            measurement with the current cube applied. The optional 3rd element is the
                            level's read-to-read chromaticity spread (xy SE) from MULTIPLE reads; when
                            present, that level's correction is scaled toward identity by its measurement
                            trust (:func:`noise_trust`) so dark/noisy levels don't bake a tint.
    ``primaries``/``native_white_xy``/``peak_luminance`` : the measured native gamut + white + peak —
                            the linear-share basis ``disp`` (column normalization cancels in the share
                            RATIO, the same shares math as :func:`refine_sdr_grayscale`).
    ``matrix_rowsums``    : ``v_c = (M @ (1,1,1))_c`` of the INSTALLED SDR MHC2 matrix
                            (``mhc2_matrix(primaries, native_white, sRGB, D65)``).

    **Abscissa convention.** Windows applies the per-channel LUT AFTER the matrix, so for a wire
    neutral of signal ``s`` channel ``c`` is driven at the post-matrix index ``v_c**(1/γ)·s`` (the
    pure-power form of ``oetf(v_c·eotf(s))``) — NOT ``s``. The measured factor is stored against that
    post-matrix abscissa, and the cube is indexed in the SAME post-matrix space (``cur[j]`` == the LUT
    value at post-matrix signal ``j/(N-1)``), exactly as :func:`refine_hdr_cube` does for PQ.

    Returns updated per-channel curves (monotone, clamped). Measured points below ``dark_floor_nits``
    (meter noise) are dropped; the first/last surviving factor is held flat beyond the measured range
    (no extrapolation), keeping the refined curve monotone through both ends."""
    chans = ("r", "g", "b")
    N = len(current_curves["r"])
    if N < 2 or len(current_curves["g"]) != N or len(current_curves["b"]) != N:
        raise ValueError("current_curves must be three equal-length channels")
    if peak_luminance <= 0.0:
        raise ValueError("peak_luminance must be positive")
    disp = rgb_to_xyz_matrix(
        primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
        primaries["bx"], primaries["by"], native_white_xy[0], native_white_xy[1],
        white_Y=peak_luminance)
    disp_inv = invert3x3(disp)
    wx, wy = target_white_xy

    # gather (post-matrix signal, linear-light correction factor) per channel from the measurement
    pts: dict[int, list[tuple[float, float]]] = {0: [], 1: [], 2: []}
    for entry in sorted(measured_neutral, key=lambda p: p[0]):
        sig, xyz = float(entry[0]), entry[1]
        chroma_sigma = entry[2] if len(entry) > 2 else None
        lin = sig ** gamma
        tY = peak_luminance * lin
        if tY < dark_floor_nits:
            continue
        total = sum(xyz) or 1.0
        mx, my = xyz[0] / total, xyz[1] / total
        w_trust = noise_trust(((mx - wx) ** 2 + (my - wy) ** 2) ** 0.5, chroma_sigma)
        ms = matvec(disp_inv, list(xyz))
        ts = matvec(disp_inv, xy_to_XYZ(wx, wy, tY))
        for c in range(3):
            ratio = ts[c] / ms[c] if ms[c] > 1e-9 else 1.0
            ratio = min(max(ratio, ratio_clamp[0]), ratio_clamp[1])
            damped = 1.0 + w_trust * (ratio ** damping - 1.0)   # toward identity by (1-w_trust)
            post_sig = (max(matrix_rowsums[c], 0.0) ** (1.0 / gamma)) * sig
            pts[c].append((min(max(post_sig, 0.0), 1.0), damped))

    out: dict[str, list[float]] = {}
    for c, ch in enumerate(chans):
        xs = [p[0] for p in pts[c]]
        fs = [p[1] for p in pts[c]]
        cur = current_curves[ch]
        curve: list[float] = []
        prev = 0.0
        for j in range(N):
            sig = j / (N - 1)
            factor = _interp(sig, xs, fs) if xs else 1.0   # flat-hold beyond the measured range
            new_lin = (float(cur[j]) ** gamma) * factor    # compose in linear light, like refine_hdr_cube
            val = new_lin ** (1.0 / gamma)
            val = max(prev, min(max(val, 0.0), 1.0))        # monotone, clamped
            prev = val
            curve.append(val)
        out[ch] = curve
    return out


def refine_sdr_grayscale(current_deviations: Optional[Mapping[str, Sequence[float]]],
                         measured_neutral: Sequence[tuple[float, Sequence[float]]],
                         primaries: Mapping[str, float], native_white_xy: tuple[float, float],
                         peak_luminance: float, matrix_rowsums: Sequence[float],
                         *, gamma: float = 2.2, target_white_xy: tuple[float, float] = _D65,
                         damping: float = 0.7, dark_floor_nits: float = 0.5,
                         n_points: int = 32, ratio_clamp: tuple[float, float] = (0.5, 2.0),
                         dev_clamp: tuple[float, float] = (0.25, 4.0)
                         ) -> tuple[list[float], dict[str, list[float]]]:
    """One closed-loop grayscale-refine step on the **SDR** MHC ``correctionGrayscale`` layer.

    SUPERSEDED 2026-06-24 by :func:`refine_sdr_cube` — the orchestrator now delivers the SDR refine
    as a 1D-LUT base (``set_base_lut``), NOT the user-editable ``correctionGrayscale`` slot
    ([[dlc-must-not-own-mhc-user-layers]]). Kept for the deviation-domain math + its tests; do not
    wire it into a hardware install.

    The SDR analog of :func:`refine_hdr_cube`: pulls the measured neutral axis toward the target
    white (default D65) by the same damped share-ratio law, but emits per-channel ``correctionGrayscale``
    **deviations** (the MHC fine-tune layer on top of the native-white base grayscale) instead of a
    rewritten 1D ``.cube``, and uses the **SDR power-law (γ) transfer** instead of PQ. Measurement-only
    (no forward model) so it converges to whatever the real panel does over a few measure→refine rounds.

    ``current_deviations`` : the installed ``correctionGrayscale`` deviations (``r``/``g``/``b``, each
                             ``n_points`` long on the returned grid), or ``None`` for identity (the
                             refine accumulates onto these — the stage resets to identity for idempotence).
    ``measured_neutral``   : ``[(wire_signal, measured_XYZ[, chroma_sigma]), ...]`` from a neutral ramp
                             measured with the current MHC applied. The optional 3rd element is the
                             level's read-to-read ``xy`` spread (SE of the mean) from MULTIPLE reads;
                             when present, that level's correction is smoothed toward identity by its
                             measurement trust (:func:`noise_trust`) so dark/noisy levels don't bake a tint.
    ``primaries``/``native_white_xy``/``peak_luminance`` : the measured native gamut + white + peak — the
                             linear-share basis ``disp`` (its column normalization cancels in the
                             share RATIO, so this is the same shares math as ``propose_correction_grayscale``).
    ``matrix_rowsums``     : ``v_c = (M @ (1,1,1))_c`` of the INSTALLED MHC2 matrix
                             (``mhc2_matrix(primaries, native_white, sRGB, D65)`` for SDR) — the
                             native-channel-c neutral drive that reproduces D65.

    **Abscissa convention (the load-bearing detail — the SDR derivation trap fix).** Windows applies
    the per-channel MHC2 LUT AFTER the matrix, so for a wire neutral of signal ``s`` channel ``c`` is
    driven at the *post-matrix* index ``oetf(v_c · eotf(s))`` — for pure power γ this is
    ``v_c**(1/γ) · s`` — NOT ``s``. The factor measured at wire ``s`` is therefore stored against that
    post-matrix abscissa, and all three channels are sampled on a SHARED post-matrix-signal grid. This
    is exactly what ``propose_correction_grayscale`` gets wrong (it keys deviations at the wire signal,
    mis-registering them along the tone axis whenever the matrix is non-trivial).

    Returns ``(points, {"r","g","b"})`` — the shared post-matrix-signal grid (uniform, ``n_points``)
    and the new per-channel signal-domain deviations (centred ~1.0, clamped to ``dev_clamp``), ready
    for ``controller.set_correction_grayscale`` (which raises a signal deviation to ``**γ`` for the
    linear-light gain — so the linear residual ``f_c`` enters as ``f_c**(1/γ)``).
    """
    chans = ("r", "g", "b")
    N = max(2, int(n_points))
    grid = [j / (N - 1) for j in range(N)]
    cur: dict[str, list[float]] = {}
    for ch in chans:
        col = list(current_deviations.get(ch, [])) if isinstance(current_deviations, Mapping) else []
        cur[ch] = [float(v) for v in col] if len(col) == N else [1.0] * N

    if peak_luminance <= 0.0:
        raise ValueError("peak_luminance must be positive")
    disp = rgb_to_xyz_matrix(
        primaries["rx"], primaries["ry"], primaries["gx"], primaries["gy"],
        primaries["bx"], primaries["by"], native_white_xy[0], native_white_xy[1],
        white_Y=peak_luminance)
    disp_inv = invert3x3(disp)
    wx, wy = target_white_xy
    lo_dev, hi_dev = dev_clamp

    # Gather (post-matrix signal, signal-domain multiplicative STEP) per channel from the measurement.
    pts: dict[int, list[tuple[float, float]]] = {0: [], 1: [], 2: []}
    for entry in sorted(measured_neutral, key=lambda p: p[0]):
        sig, xyz = float(entry[0]), entry[1]
        chroma_sigma = entry[2] if len(entry) > 2 else None
        lin = sig ** gamma
        target_Y = peak_luminance * lin
        if target_Y < dark_floor_nits:
            continue
        total = sum(xyz) or 1.0
        mx, my = xyz[0] / total, xyz[1] / total
        w_trust = noise_trust(((mx - wx) ** 2 + (my - wy) ** 2) ** 0.5, chroma_sigma)
        ms = matvec(disp_inv, list(xyz))
        ts = matvec(disp_inv, xy_to_XYZ(wx, wy, target_Y))
        for c in range(3):
            ratio = ts[c] / ms[c] if ms[c] > 1e-9 else 1.0
            ratio = min(max(ratio, ratio_clamp[0]), ratio_clamp[1])
            # Signal-domain step = f_c**(damping/γ) (same exponent as propose_correction_grayscale),
            # blended toward identity (1.0) by (1 - w_trust) so a noisy/unstable dark level holds.
            step = 1.0 + w_trust * (ratio ** (damping / gamma) - 1.0)
            post_sig = (max(matrix_rowsums[c], 0.0) ** (1.0 / gamma)) * sig
            pts[c].append((min(max(post_sig, 0.0), 1.0), step))

    out: dict[str, list[float]] = {}
    for c, ch in enumerate(chans):
        xs = [p[0] for p in pts[c]]
        ss = [p[1] for p in pts[c]]
        col: list[float] = []
        for j in range(N):
            # Flat-hold the first/last measured step beyond the measured range (no extrapolation);
            # _interp clamps to the end values. Compose multiplicatively onto the current deviation.
            step = _interp(grid[j], xs, ss) if xs else 1.0
            new_dev = cur[ch][j] * step
            col.append(min(max(new_dev, lo_dev), hi_dev))
        out[ch] = col
    return grid, out


def write_1d_cube(path: Path, curves: dict[str, list[float]], *, title: str = "DLC HDR MHC base") -> Path:
    """Write per-channel curves as an Iridas 1D ``.cube`` (``LUT_1D_SIZE`` + R G B triplets),
    classic-locale floats — the exact format ``mhc_read.cpp`` ``Load1DCubeLUT`` parses."""
    r, g, b = curves["r"], curves["g"], curves["b"]
    n = len(r)
    if not (len(g) == n and len(b) == n):
        raise ValueError("channel curves must be equal length")
    lines = [f'TITLE "{title}"', f"LUT_1D_SIZE {n}", "LUT_1D_INPUT_RANGE 0.0 1.0"]
    lines += [f"{r[i]:.8f} {g[i]:.8f} {b[i]:.8f}" for i in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_1d_cube(path: Path) -> dict[str, list[float]]:
    """Parse an Iridas 1D ``.cube`` (the inverse of :func:`write_1d_cube`) into per-channel
    curves ``{"r":[...], "g":[...], "b":[...]}``. Ignores TITLE/SIZE/RANGE header lines and
    comments; each data line is an ``R G B`` triplet. Used to reload the installed base cube
    before a closed-loop refine round."""
    r: list[float] = []
    g: list[float] = []
    b: list[float] = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        parts = ln.split()
        if len(parts) != 3:
            continue
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            continue   # header keyword line (TITLE/LUT_1D_SIZE/...)
        r.append(vals[0]); g.append(vals[1]); b.append(vals[2])
    if not r:
        raise ValueError(f"no RGB triplets found in {path}")
    return {"r": r, "g": g, "b": b}
