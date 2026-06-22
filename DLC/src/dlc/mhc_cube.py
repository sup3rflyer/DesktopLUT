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
from typing import Mapping, Sequence

from .colormath import invert3x3, matvec, rgb_to_xyz_matrix, xy_to_XYZ
from .mhc import Ti3Sample

__all__ = [
    "pq_eotf",
    "pq_oetf",
    "invert_trc",
    "invert_monotone",
    "peak_chroma_luminance",
    "build_hdr_cube",
    "refine_hdr_cube",
    "write_1d_cube",
]

_D65 = (0.3127, 0.3290)


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

# ST 2084 (SMPTE) constants — verbatim from mhc.cpp / engine.patches (2610/16384, ...).
_PQ_M1 = 0.1593017578125
_PQ_M2 = 78.84375
_PQ_C1 = 0.8359375
_PQ_C2 = 18.8515625
_PQ_C3 = 18.6875

_PQ_CONTAINER_NITS = 10000.0
_CHANNELS = ("r", "g", "b")


def pq_eotf(pq: float) -> float:
    """PQ signal (0..1) -> linear light (0..1 normalised to 10000 nits). Port of ``mhc.cpp`` PqEOTF."""
    vm = max(pq, 1e-10) ** (1.0 / _PQ_M2)
    t = max(vm - _PQ_C1, 0.0) / max(_PQ_C2 - _PQ_C3 * vm, 1e-10)
    return t ** (1.0 / _PQ_M1)


def pq_oetf(y: float) -> float:
    """Linear light (0..1 over 10000 nits) -> PQ signal (0..1). Port of ``mhc.cpp`` PqOETF."""
    ym = max(y, 0.0) ** _PQ_M1
    return ((_PQ_C1 + _PQ_C2 * ym) / (1.0 + _PQ_C3 * ym)) ** _PQ_M2


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
                   *, lut_size: int = 1024, dark_floor_nits: float = 0.3
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
    (PQ signal ~0.117) is the colorimeter-grounded floor; 1 nit was needlessly conservative."""
    if peak_luminance <= 0.0:
        raise ValueError("peak_luminance must be positive")
    sigs, shares = _gray_shares(samples, primaries, white_xy, peak_luminance)
    if len(sigs) < 2:
        raise ValueError("fewer than 2 neutral patches; cannot build a gray-ramp cube")
    # Peak linear share per channel (≈1.0 each by construction of P at the native white).
    peak_share = {ch: shares[ch][-1] for ch in _CHANNELS}
    # Signal below which a level's luminance is under the trustworthy floor (blend to identity).
    dark_sig = pq_oetf(min(dark_floor_nits, peak_luminance) / _PQ_CONTAINER_NITS)

    curves: dict[str, list[float]] = {ch: [] for ch in _CHANNELS}
    prev = {ch: 0.0 for ch in _CHANNELS}
    for j in range(lut_size):
        pq_in = j / (lut_size - 1)
        lin_nits = min(pq_eotf(pq_in) * _PQ_CONTAINER_NITS, peak_luminance)
        frac = lin_nits / peak_luminance              # native-white-proportional target (0..1)
        # Trust weight: 0 in the noisy deep shadows, ramping to 1 by 2x the dark-floor signal.
        w = 0.0 if dark_sig <= 0 else min(1.0, max(0.0, (pq_in - dark_sig) / dark_sig))
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
    ``measured_neutral``  : ``[(wire_signal, measured_XYZ), ...]`` from a neutral-ramp measurement
                            with the current cube applied.
    ``channel_peak_xyz``  : measured native full-drive primary XYZ (R,G,B) — the linear-share basis.
    ``matrix_rowsums``    : ``v_c = M @ (1,1,1)`` for the installed MHC2 matrix, to index the cube at
                            the POST-matrix signal Windows actually applies it to.
    ``peak_cap_nits``     : the Peak-Chroma luminance cap (target white saturates here at the top).

    Returns updated per-channel curves (monotone, clamped). The correction is identity below
    ``dark_floor_nits`` (meter noise) and held flat past the measured range (no extrapolation).
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
    for sig, xyz in sorted(measured_neutral, key=lambda p: p[0]):
        lin = pq_eotf(sig)
        tY = min(lin * 10000.0, peak_cap_nits)
        if tY < dark_floor_nits:
            continue
        ms = matvec(disp_inv, xyz)
        ts = matvec(disp_inv, xy_to_XYZ(wx, wy, tY))
        for c in range(3):
            ratio = ts[c] / ms[c] if ms[c] > 1e-9 else 1.0
            ratio = min(max(ratio, ratio_clamp[0]), ratio_clamp[1])
            post_sig = pq_oetf(min(max(matrix_rowsums[c] * lin, 0.0), 1.0))
            pts[c].append((post_sig, ratio ** damping))

    out: dict[str, list[float]] = {}
    for c, ch in enumerate(chans):
        xs = [p[0] for p in pts[c]]
        fs = [p[1] for p in pts[c]]
        cur = current_curves[ch]
        curve: list[float] = []
        prev = 0.0
        for j in range(N):
            sig = j / (N - 1)
            factor = _interp(sig, xs, fs) if xs else 1.0          # 1.0 left of first point (dark)
            new_lin = pq_eotf(float(cur[j])) * factor
            val = pq_oetf(new_lin)
            val = max(prev, min(max(val, 0.0), 1.0))               # monotone, clamped
            prev = val
            curve.append(val)
        out[ch] = curve
    return out


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
