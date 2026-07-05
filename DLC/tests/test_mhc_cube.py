"""Tests for the HDR MHC 1D-cube generator (dlc.mhc_cube).

The cube is the EOTF correction DesktopLUT bakes into the 4096-entry HDR MHC2 LUT, so its
math must match DesktopLUT's own C++ (mhc.cpp PqEOTF / InvertTRC / GenerateMHC2LUT_FromTRC_HDR)
and the ST 2084 standard. These tests pin both, plus the calibration behaviour (perfect panel
-> identity; a mistracking panel -> a correcting curve)."""

from __future__ import annotations

import math

import pytest

from dlc.mhc import Ti3Sample
from dlc import mhc_cube as mc


# --- ST 2084 grounding ------------------------------------------------------

def test_pq_eotf_matches_colour_st2084():
    colour = pytest.importorskip("colour")
    for s in (0.0, 0.1, 0.25, 0.5, 0.75, 0.8162, 0.9, 1.0):
        ref_nits = float(colour.models.eotf_ST2084(s))           # nits, 0..10000
        got_nits = mc.pq_eotf(s) * 10000.0
        assert math.isclose(got_nits, ref_nits, rel_tol=1e-4, abs_tol=1e-3), s


def test_pq_eotf_oetf_roundtrip():
    for s in (0.0, 0.05, 0.2, 0.5, 0.8, 1.0):
        assert math.isclose(mc.pq_oetf(mc.pq_eotf(s)), s, abs_tol=1e-6)


def test_known_pq_anchor_1800_nits():
    # 1800 nits is the run target peak; its PQ signal is ~0.8162 (cross-checked elsewhere).
    assert math.isclose(mc.pq_oetf(1800.0 / 10000.0), 0.81623, abs_tol=1e-3)


# --- Port fidelity vs the C++ InvertTRC / FromTRC_HDR -----------------------

def _cpp_invert_trc(trc, target):
    """Re-implementation of mhc.cpp InvertTRC, independent of dlc.mhc_cube, for a golden compare."""
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
    t = (target - trc[lo]) / (trc[hi] - trc[lo]) if trc[hi] > trc[lo] else 0.0
    return (lo + t) / (n - 1)


def test_invert_trc_matches_cpp_reference():
    trc = [ (k / 63) ** 1.7 for k in range(64) ]   # a non-trivial monotone TRC
    for target in (0.0, 0.01, 0.13, 0.5, 0.77, 0.99, 1.0, 1.5, -0.2):
        assert math.isclose(mc.invert_trc(trc, target), _cpp_invert_trc(trc, target), abs_tol=1e-9)


def test_invert_monotone():
    xs = [0.0, 0.25, 0.5, 0.75, 1.0]
    ys = [0.0, 0.0625, 0.25, 0.5625, 1.0]   # y = x^2 sampled
    assert math.isclose(mc.invert_monotone(xs, ys, 0.25), 0.5, abs_tol=1e-9)
    assert mc.invert_monotone(xs, ys, -0.1) == 0.0       # clamp low
    assert mc.invert_monotone(xs, ys, 2.0) == 1.0        # clamp high
    # interpolates between samples
    assert 0.5 < mc.invert_monotone(xs, ys, 0.4) < 0.75


# --- Calibration behaviour (gray-ramp basis) --------------------------------
# The cube is derived from the GRAY ramp via the primaries matrix, so build synthetic gray
# patches with controlled per-channel linear shares using the SAME matrix the builder uses.

from dlc.colormath import rgb_to_xyz_matrix, matvec

_PRIM = {"rx": 0.68, "ry": 0.32, "gx": 0.265, "gy": 0.69, "bx": 0.15, "by": 0.06}
_WHITE = (0.3127, 0.3290)
_PEAK = 1000.0
_P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                       _PRIM["bx"], _PRIM["by"], _WHITE[0], _WHITE[1], white_Y=_PEAK)


def _gray_sample(sig, shares):
    """A gray patch (rgb=(sig,sig,sig)) whose measured XYZ decomposes to the given linear
    RGB shares (each 0..1 of peak) under the builder's primaries matrix."""
    xyz = matvec(_P, [s * _PEAK for s in shares])
    return Ti3Sample(rgb=(sig, sig, sig), xyz=(xyz[0], xyz[1], xyz[2]))


def _perfect_gray_ramp(n=40):
    # A panel whose gray tracks native white + PQ exactly: equal shares = pq_eotf(s)*10000/peak.
    samples = []
    smax = mc.pq_oetf(_PEAK / 10000.0)            # signal that yields the peak
    for i in range(n):
        s = smax * i / (n - 1)
        frac = min(mc.pq_eotf(s) * 10000.0 / _PEAK, 1.0)
        samples.append(_gray_sample(s, (frac, frac, frac)))
    return samples


def test_perfect_neutral_panel_yields_identity_cube():
    curves, summary = mc.build_hdr_cube(_perfect_gray_ramp(), _PRIM, _WHITE, _PEAK, lut_size=512)
    assert summary["basis"] == "gray-ramp"
    n = 512
    smax = mc.pq_oetf(_PEAK / 10000.0)
    for ch in ("r", "g", "b"):
        c = curves[ch]
        assert all(c[i] <= c[i + 1] + 1e-6 for i in range(n - 1))     # monotone
        assert c[0] <= 1e-3                                            # anchored at black
        # Below the peak signal the cube is ~identity (perfect tracking needs no correction).
        for pq_in in (0.1, 0.3, 0.5, 0.7):
            j = round(pq_in * (n - 1))
            assert abs(c[j] - pq_in) < 5e-3, (ch, pq_in, c[j])


def test_blue_deficient_shadows_get_boosted():
    # A panel neutral at the top but blue-deficient in the shadows (gray drifts yellow/green).
    # The cube must BOOST blue more than red there (cube_b > cube_r at a shadow level) and leave
    # the peak alone (so it doesn't fight the matrix's white rotation).
    samples = []
    smax = mc.pq_oetf(_PEAK / 10000.0)
    n = 40
    for i in range(n):
        s = smax * i / (n - 1)
        frac = min(mc.pq_eotf(s) * 10000.0 / _PEAK, 1.0)
        # blue share sags below frac in the dark, recovering to neutral by the top
        t = i / (n - 1)
        b = frac * (0.6 + 0.4 * t)                # 40% deficient at black -> neutral at peak
        samples.append(_gray_sample(s, (frac, frac, b)))
    curves, _ = mc.build_hdr_cube(samples, _PRIM, _WHITE, _PEAK, lut_size=512, dark_floor_nits=1.0)
    n = 512
    # above the dark floor (sig ~0.5, well over 1 nit) blue is still deficient -> boosted over red
    j = round(0.5 * (n - 1))
    assert curves["b"][j] > curves["r"][j] + 1e-3, (curves["b"][j], curves["r"][j])
    # deep shadow (sig ~0.05, sub-nit) is held to identity (no noise-chasing): cube ~= input
    jd = round(0.05 * (n - 1))
    assert abs(curves["b"][jd] - 0.05) < 5e-3 and abs(curves["r"][jd] - 0.05) < 5e-3
    # peak: all channels converge (no white override)
    assert abs(curves["b"][-1] - curves["r"][-1]) < 2e-3


# --- Peak-Chroma luminance cap ----------------------------------------------

def _channel_peaks(white_xy, peak, scale=(1.0, 1.0, 1.0)):
    """The 3 per-channel full-drive XYZ triples (disp columns) for a panel whose additive white is
    `white_xy` at `peak` nits, optionally scaling a channel's luminance to make it cold."""
    P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                          _PRIM["bx"], _PRIM["by"], white_xy[0], white_xy[1], white_Y=peak)
    return [[P[row][c] * scale[c] for row in range(3)] for c in range(3)]


def test_peak_chroma_cap_neutral_panel_is_uncapped():
    # A panel whose additive white already IS D65 needs no luminance sacrifice to hold D65.
    peaks = _channel_peaks((0.3127, 0.3290), 1000.0)
    cap, _binding = mc.peak_chroma_luminance(peaks)
    assert math.isclose(cap, 1000.0, rel_tol=1e-9)


def test_peak_chroma_cap_is_limited_by_the_cold_channel():
    # Halve blue's peak luminance -> warm white, blue can't reach D65's blue content at full drive,
    # so blue is the binding channel and the cap drops below the (now lower) native peak.
    peaks = _channel_peaks((0.3127, 0.3290), 1000.0, scale=(1.0, 1.0, 0.5))
    cap, binding = mc.peak_chroma_luminance(peaks)
    native_peak = sum(peaks[c][1] for c in range(3))
    assert binding == "b"
    assert cap < native_peak


def test_peak_chroma_cap_rejects_nonpositive_peak():
    with pytest.raises(ValueError):
        mc.peak_chroma_luminance([(0.0, 0.0, 0.0)] * 3)


def test_adaptive_dark_floor_clean_panel_is_low():
    # Every read sits at the same chromaticity (a clean meter/panel) → no smoothing needed → low bound.
    reads = [(nits, 0.3127, 0.3290) for nits in (0.2, 0.5, 1.0, 5.0, 20.0, 100.0, 500.0)]
    floor, info = mc.adaptive_dark_floor(reads, bounds=(0.1, 5.0))
    assert floor == 0.1
    assert info["reason"] == "clean_dark_region" and info["n_strayed"] == 0


def test_adaptive_dark_floor_follows_dark_chroma_drift():
    # The dim reads wander off the bright white (noise/instability); the floor rises to the
    # brightest strayed DIM read so the per-level correction blends to identity below it.
    reads = [
        (0.3, 0.34, 0.30),    # dim, strayed
        (0.8, 0.33, 0.31),    # dim, strayed
        (2.0, 0.315, 0.328),  # dim-ish, on-white
        (50.0, 0.3127, 0.3290),
        (300.0, 0.3127, 0.3290),
    ]
    floor, info = mc.adaptive_dark_floor(reads, chroma_tolerance=0.008, bounds=(0.1, 5.0))
    assert info["reason"] == "chroma_drift"
    assert floor == 0.8                      # brightest strayed dim read; smooth below it
    assert info["n_strayed"] == 2


def test_adaptive_dark_floor_hdr_anchors_on_diffuse_white_not_overdriven_peak():
    # HDR: the brightest patch (1000 nits) is overdriven/ABL-shifted; the diffuse-white band
    # (100-203) is stable at D65. A dark read that tracks D65 must NOT be flagged just because it
    # differs from the shifted peak — only the genuinely-wandering dark read sets the floor.
    reads = [(0.3, 0.34, 0.30),       # dark, genuinely wandering
             (0.5, 0.3127, 0.329),    # dark, on diffuse white
             (2.0, 0.3127, 0.329),    # dark, on diffuse white
             (120.0, 0.3127, 0.329),  # diffuse-white band
             (180.0, 0.3127, 0.329),  # diffuse-white band
             (1000.0, 0.300, 0.340)]  # overdriven peak (shifted)
    band_floor, info = mc.adaptive_dark_floor(reads, reference_band=mc.HDR_REFERENCE_WHITE_BAND)
    peak_floor, _ = mc.adaptive_dark_floor(reads)            # brightest-ref (SDR / old behaviour)
    assert info["ref_source"] == "diffuse_white_band"
    assert abs(band_floor - 0.3) < 1e-6                      # only the wandering read; 0.5/2.0 track D65
    assert peak_floor > band_floor                           # anchoring on the overdriven peak over-smooths


def test_adaptive_dark_floor_too_few_reads_falls_back():
    floor, info = mc.adaptive_dark_floor([(1.0, 0.31, 0.33)], default_floor_nits=0.3)
    assert floor == 0.3 and info["reason"] == "too_few_reads"


def test_adaptive_dark_floor_respects_upper_bound():
    # A dark region unstable well past the cap can't push the floor past the upper bound: the
    # brightest strayed dim-half read is 6 nits, but the bound caps the floor at 3.
    reads = [(2.0, 0.45, 0.20), (4.0, 0.20, 0.45), (6.0, 0.40, 0.40),
             (8.0, 0.3127, 0.3290), (400.0, 0.3127, 0.3290)]
    floor, _info = mc.adaptive_dark_floor(reads, bounds=(0.1, 3.0))
    assert floor == 3.0


# --- noise-derived dark trust ----------------------------------------------

def test_noise_trust_gates_on_snr():
    # correction within the noise -> 0 (don't chase noise); clearly above -> 1; monotone between.
    assert mc.noise_trust(0.001, 0.01) == 0.0          # error << noise
    assert mc.noise_trust(0.05, 0.01) == 1.0           # error >> noise (5σ)
    mid = mc.noise_trust(0.02, 0.01)                    # 2σ -> partial
    assert 0.0 < mid < 1.0
    # no measured noise (single read / perfect) -> trust fully
    assert mc.noise_trust(0.02, None) == 1.0
    assert mc.noise_trust(0.02, 0.0) == 1.0
    # more readings tighten σ (SE shrinks) -> the SAME error clears the gate
    assert mc.noise_trust(0.02, 0.02) < mc.noise_trust(0.02, 0.02 / (4 ** 0.5))


def test_dark_trust_weights_smooths_noisy_levels():
    # a clean dark level (tiny σ, real error) trusts ~1; a noisy one (σ ≈ error) collapses to ~0.
    white = (0.3127, 0.3290)
    levels = [
        (0.05, 0.34, 0.30, 0.03),   # dark + very noisy (σ≈error) -> low trust
        (0.20, 0.32, 0.33, 0.001),  # dark but STABLE, small real error -> high trust
        (0.80, 0.314, 0.330, 0.0005),
    ]
    w = dict(mc.dark_trust_weights(levels, white))
    assert w[0.05] < 0.3            # noisy dark level smoothed toward identity
    assert w[0.20] > 0.9            # stable level trusted
    assert w[0.80] > 0.9


def test_build_hdr_cube_level_trust_smooths_low_trust_levels_to_identity():
    # With a level_trust that says "don't trust the dark third", build_hdr_cube must leave that
    # region ~identity even though the gray ramp asks for a correction there.
    peak = 1000.0
    sigs = [0.1, 0.3, 0.5, 0.7, 0.9, 1.0]
    # a warm-drifting gray ramp (blue share deficient) so the cube WANTS to correct everywhere
    samples = []
    for s in sigs:
        frac = (s ** 2.0)
        # measured XYZ via _PRIM at a warm white, scaled to peak*frac (controlled drift)
        from dlc.colormath import rgb_to_xyz_matrix as _m
        P = _m(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"], _PRIM["bx"], _PRIM["by"],
               0.335, 0.345, white_Y=peak)   # warm native white
        samples.append(Ti3Sample(rgb=(s, s, s),
                                  xyz=(P[0][0] * frac + P[0][1] * frac + P[0][2] * frac,
                                       P[1][0] * frac + P[1][1] * frac + P[1][2] * frac,
                                       P[2][0] * frac + P[2][1] * frac + P[2][2] * frac)))
    prim = _PRIM
    base, _ = mc.build_hdr_cube(samples, prim, (0.335, 0.345), peak, lut_size=256)
    # now declare the bottom third untrustworthy (trust 0 below signal 0.35, 1 above 0.5)
    trust = [(0.0, 0.0), (0.35, 0.0), (0.5, 1.0), (1.0, 1.0)]
    gated, _ = mc.build_hdr_cube(samples, prim, (0.335, 0.345), peak, lut_size=256, level_trust=trust)
    N = 256
    lo_idx = int(0.2 * (N - 1))     # a signal in the untrusted dark third
    hi_idx = int(0.8 * (N - 1))     # a signal in the trusted region
    ident = lo_idx / (N - 1)
    # gated cube is ~identity in the untrusted dark third...
    assert abs(gated["r"][lo_idx] - ident) < abs(base["r"][lo_idx] - ident) or \
        abs(gated["b"][lo_idx] - ident) < 1e-3
    # ...and matches the ungated cube in the trusted region
    for ch in "rgb":
        assert abs(gated[ch][hi_idx] - base[ch][hi_idx]) < 1e-6


def test_refine_hdr_cube_trust_damps_noisy_dark_level():
    # A measured neutral with a per-level σ tag: a noisy dark point's correction is damped toward
    # identity vs the same point with no σ (full correction).
    peaks, smax, _emit, peak = _synthetic_panel((0.3127, 0.3290))
    N = 256
    cube = {ch: [j / (N - 1) for j in range(N)] for ch in "rgb"}
    # one dark measured point that's off-white (would normally be corrected hard)
    from dlc.colormath import invert3x3, matvec, xy_to_XYZ
    disp = [[peaks[c][r] for c in range(3)] for r in range(3)]
    disp_inv = invert3x3(disp)
    s = 0.25 * smax
    tY = min(mc.pq_eotf(s) * 10000.0, peak)
    ts = matvec(disp_inv, xy_to_XYZ(0.3127, 0.3290, tY))
    ms = [ts[0] * 1.3, ts[1], ts[2] * 0.7]                       # off-white (warm) dark read
    xyz = tuple(sum(disp[r][c] * ms[c] for c in range(3)) for r in range(3))
    rowsums = (1.0, 1.0, 1.0)
    trusted = mc.refine_hdr_cube(cube, [(s, xyz)], peaks, rowsums, peak_cap_nits=peak, dark_floor_nits=0.5)
    # same point, but tagged with a σ as large as its chroma error -> low trust -> damped
    tot = sum(xyz)
    err = ((xyz[0] / tot - 0.3127) ** 2 + (xyz[1] / tot - 0.3290) ** 2) ** 0.5
    noisy = mc.refine_hdr_cube(cube, [(s, xyz, err)], peaks, rowsums, peak_cap_nits=peak, dark_floor_nits=0.5)
    # the noisy-tagged refine moves the cube LESS from identity than the trusted one
    j = int(0.5 * (N - 1))
    move_trusted = sum(abs(trusted[ch][j] - cube[ch][j]) for ch in "rgb")
    move_noisy = sum(abs(noisy[ch][j] - cube[ch][j]) for ch in "rgb")
    assert move_noisy < move_trusted


def test_build_hdr_cube_requires_neutral():
    with pytest.raises(ValueError):
        mc.build_hdr_cube([Ti3Sample(rgb=(0.0, 0.0, 0.0), xyz=(0.0, 0.0, 0.0))],
                          _PRIM, _WHITE, _PEAK)


# --- closed-loop HDR grayscale refine ---------------------------------------
# A synthetic additive panel (no hardcoded magnitudes in the engine): per-channel full-drive XYZ
# = columns of the primaries matrix at a chosen white; emit = sum of per-channel light. Driving it
# neutral renders that white; the refine must bend the cube per-channel to land D65 from the panel's
# OWN measurements (identity MHC2 matrix -> post-matrix signal == input signal, so v=(1,1,1)).

def _synthetic_panel(white_xy, peak_nits=1000.0):
    P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                          _PRIM["bx"], _PRIM["by"], white_xy[0], white_xy[1], white_Y=peak_nits)
    peaks = [[P[r][c] for r in range(3)] for c in range(3)]      # peaks[c] = XYZ of channel c at full
    smax = mc.pq_oetf(peak_nits / 10000.0)
    def emit(drive):
        f = [mc.pq_eotf(max(0.0, min(d, smax))) / mc.pq_eotf(smax) for d in drive]
        return tuple(sum(P[r][c] * f[c] for c in range(3)) for r in range(3))
    return peaks, smax, emit, peak_nits


def _run_loop(white_xy, rounds=6):
    peaks, smax, emit, peak = _synthetic_panel(white_xy)
    N = 256
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}                      # identity seed
    sigs = [f * smax for f in (0.3, 0.4, 0.5, 0.6, 0.7)]
    def measure(cube):
        out = []
        for s in sigs:
            drive = [_interp_list(grid, cube[ch], s) for ch in "rgb"]
            out.append((s, emit(drive)))
        return out
    for _ in range(rounds):
        meas = measure(cube)
        cube = mc.refine_hdr_cube(cube, meas, peaks, (1.0, 1.0, 1.0),
                                  peak_cap_nits=peak, dark_floor_nits=0.5)
    return measure(cube)


def _interp_list(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for k in range(1, len(xs)):
        if xs[k] >= x:
            t = (x - xs[k - 1]) / (xs[k] - xs[k - 1])
            return ys[k - 1] + (ys[k] - ys[k - 1]) * t
    return ys[-1]


def _xy(XYZ):
    s = sum(XYZ)
    return (XYZ[0] / s, XYZ[1] / s)


def test_refine_hdr_cube_converges_warm_panel_to_d65():
    # A warm panel (excess red) driven neutral renders ~its warm white; the refine must reach D65.
    meas = _run_loop((0.340, 0.345), rounds=6)
    for _s, xyz in meas:
        x, y = _xy(xyz)
        assert abs(x - 0.3127) < 0.004 and abs(y - 0.3290) < 0.004, (x, y)


def test_refine_hdr_cube_improves_monotonically():
    peaks, smax, emit, peak = _synthetic_panel((0.340, 0.345))
    N = 256; grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}
    s = 0.5 * smax
    errs = []
    for _ in range(5):
        drive = [_interp_list(grid, cube[ch], s) for ch in "rgb"]
        x, y = _xy(emit(drive))
        errs.append(((x - 0.3127) ** 2 + (y - 0.3290) ** 2) ** 0.5)
        cube = mc.refine_hdr_cube(cube, [(s, emit(drive))], peaks, (1.0, 1.0, 1.0),
                                  peak_cap_nits=peak, dark_floor_nits=0.5)
    assert errs[-1] < errs[0] * 0.25                              # error shrinks substantially
    assert all(errs[i + 1] <= errs[i] + 1e-6 for i in range(len(errs) - 1))  # monotone


def test_refine_hdr_cube_uses_matrix_rowsums_for_abscissa():
    # The load-bearing non-identity-matrix case the convergence tests don't exercise: blue's
    # correction is keyed to the POST-matrix signal pq_oetf(rowsum_b * pq_eotf(s)), so the SAME
    # measurements under different rowsums must place blue's correction differently. A wire-keyed
    # bug (ignoring rowsums) would produce identical blue curves for both. Blue is perfect at the
    # low point and dim at the high point, so its factor is level-dependent (not a flat scale that
    # would wash the abscissa out).
    from dlc.colormath import invert3x3, matvec, xy_to_XYZ
    peaks, smax, _emit, peak = _synthetic_panel((0.3127, 0.3290))
    disp = [[peaks[c][row] for c in range(3)] for row in range(3)]
    disp_inv = invert3x3(disp)
    N = 512
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}
    s_lo, s_hi = 0.40 * smax, 0.70 * smax
    blue_scale = {s_lo: 1.0, s_hi: 0.6}                              # perfect low, 40% too dim high
    meas = []
    for s in (s_lo, s_hi):
        tY = min(mc.pq_eotf(s) * 10000.0, peak)
        ts = matvec(disp_inv, xy_to_XYZ(0.3127, 0.3290, tY))
        ms = [ts[0], ts[1], ts[2] * blue_scale[s]]
        xyz = tuple(sum(disp[r][c] * ms[c] for c in range(3)) for r in range(3))
        meas.append((s, xyz))
    base = mc.refine_hdr_cube(cube, meas, peaks, (1.0, 1.0, 1.0), peak_cap_nits=peak, dark_floor_nits=0.5)
    shifted = mc.refine_hdr_cube(cube, meas, peaks, (1.0, 1.0, 2.0), peak_cap_nits=peak, dark_floor_nits=0.5)

    # rowsum_b 1.0 vs 1.5 shifts blue's post-matrix abscissa → the blue curve must differ.
    diff_b = max(abs(base["b"][j] - shifted["b"][j]) for j in range(N))
    assert diff_b > 5e-3, diff_b
    # red/green have rowsum 1.0 in both runs → bit-identical (and uncorrected: blue-only error).
    for ch in ("r", "g"):
        assert all(abs(base[ch][j] - shifted[ch][j]) < 1e-9 for j in range(N))


def test_refine_hdr_cube_leaves_neutral_panel_alone():
    # A panel already at D65 needs ~no correction: the cube stays ~identity.
    peaks, smax, emit, peak = _synthetic_panel((0.3127, 0.3290))
    N = 128; grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}
    sigs = [f * smax for f in (0.3, 0.5, 0.7)]
    meas = [(s, emit([_interp_list(grid, cube[ch], s) for ch in "rgb"])) for s in sigs]
    new = mc.refine_hdr_cube(cube, meas, peaks, (1.0, 1.0, 1.0), peak_cap_nits=peak, dark_floor_nits=0.5)
    for ch in "rgb":
        for j in range(0, N, 8):
            assert abs(new[ch][j] - cube[ch][j]) < 5e-3, (ch, j, new[ch][j], cube[ch][j])


def test_write_1d_cube_roundtrips(tmp_path):
    curves = {c: [i / 7 for i in range(8)] for c in ("r", "g", "b")}
    path = mc.write_1d_cube(tmp_path / "base.cube", curves)
    text = path.read_text(encoding="utf-8")
    assert "LUT_1D_SIZE 8" in text
    rows = [ln for ln in text.splitlines() if ln and ln[0].isdigit()]
    assert len(rows) == 8
    first = rows[0].split()
    assert len(first) == 3 and all(float(x) == 0.0 for x in first)


def test_read_1d_cube_roundtrips_write(tmp_path):
    # read_1d_cube is the exact inverse of write_1d_cube (skips TITLE/SIZE/RANGE header lines).
    curves = {"r": [0.0, 0.3, 1.0], "g": [0.0, 0.5, 1.0], "b": [0.0, 0.7, 1.0]}
    path = mc.write_1d_cube(tmp_path / "rt.cube", curves, title="DLC test")
    got = mc.read_1d_cube(path)
    for ch in ("r", "g", "b"):
        assert got[ch] == pytest.approx(curves[ch], abs=1e-7)


def test_read_1d_cube_rejects_empty(tmp_path):
    p = tmp_path / "empty.cube"
    p.write_text('TITLE "nothing"\nLUT_1D_SIZE 0\n', encoding="utf-8")
    with pytest.raises(ValueError):
        mc.read_1d_cube(p)


# --- mhc2_matrix (the inv(disp)·src tag matrix) -----------------------------

def test_mhc2_matrix_identity_when_native_equals_target():
    # Native panel == standard source (same primaries + white) ⇒ the MHC2 matrix is identity:
    # nothing to rotate, neutral drive (M @ (1,1,1)) is exactly (1,1,1).
    prim = {"rx": 0.708, "ry": 0.292, "gx": 0.170, "gy": 0.797, "bx": 0.131, "by": 0.046}
    M = mc.mhc2_matrix(prim, _WHITE, prim, _WHITE)
    for r in range(3):
        for c in range(3):
            assert M[r][c] == pytest.approx(1.0 if r == c else 0.0, abs=1e-9)
    rowsums = [sum(M[r]) for r in range(3)]
    assert rowsums == pytest.approx([1.0, 1.0, 1.0], abs=1e-9)


def test_mhc2_matrix_warm_panel_needs_cooler_neutral_drive():
    # A warm (red-biased) native panel mapped to a D65 source must DRIVE BLUE UP and RED DOWN at
    # neutral — i.e. the post-matrix neutral drive (row-sums) has blue > red.
    target = {"rx": 0.708, "ry": 0.292, "gx": 0.170, "gy": 0.797, "bx": 0.131, "by": 0.046}
    warm_white = (0.330, 0.340)   # warmer than D65 (0.3127, 0.3290)
    M = mc.mhc2_matrix(target, warm_white, target, _WHITE)
    # Native gamut == target gamut (only the white differs) ⇒ a pure DIAGONAL white-only move
    # (no cross-channel gamut rotation). The native-target refine relies on this: its rowsums are
    # the per-channel white gains, NOT cross-channel sums — so the cube abscissa matches the install.
    assert all(abs(M[r][c]) < 1e-9 for r in range(3) for c in range(3) if r != c)
    rowsums = [sum(M[r]) for r in range(3)]
    assert rowsums[2] > rowsums[0]   # blue driven harder than red to cool the white


# --- closed-loop SDR grayscale refine (correctionGrayscale, γ2.2) -----------
# Same synthetic-additive-panel idea as the HDR refine tests, but the SDR transfer is pure power γ
# and the refine emits per-channel correctionGrayscale DEVIATIONS (signal-domain gains, raised to ^γ
# for the linear-light effect) on a shared POST-matrix-signal grid. With rowsums=(1,1,1) the post-
# matrix signal == the wire signal, so the panel sim evaluates the deviation at the wire signal.

_GAMMA = 2.2


def _synthetic_sdr_panel(white_xy, peak_nits=120.0, gamma=_GAMMA):
    P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                          _PRIM["bx"], _PRIM["by"], white_xy[0], white_xy[1], white_Y=peak_nits)
    def emit(drive):                          # drive = per-channel SIGNAL in [0,1]
        f = [max(0.0, min(d, 1.0)) ** gamma for d in drive]
        return tuple(sum(P[r][c] * f[c] for c in range(3)) for r in range(3))
    return P, emit, peak_nits


def _run_sdr_loop(white_xy, rounds=10):
    _P, emit, peak = _synthetic_sdr_panel(white_xy)
    sigs = [0.3, 0.4, 0.5, 0.6, 0.7]
    grid = None
    dev = None
    def measure(grid, dev):
        out = []
        for s in sigs:
            if grid is None:
                drive = [s, s, s]             # identity correctionGrayscale (round 0)
            else:                             # rowsums=(1,1,1) ⇒ post-matrix signal == wire signal s
                drive = [s * _interp_list(grid, dev[ch], s) for ch in "rgb"]
            out.append((s, emit(drive)))
        return out
    for _ in range(rounds):
        meas = measure(grid, dev)
        grid, dev = mc.refine_sdr_grayscale_legacy(dev, meas, _PRIM, white_xy, peak,
                                            (1.0, 1.0, 1.0), dark_floor_nits=0.5)
    return measure(grid, dev)


def test_refine_sdr_grayscale_converges_warm_panel_to_d65():
    # A warm panel (its native white) driven neutral renders ~its warm white; the closed loop must
    # bend the correctionGrayscale deviations until the measured neutral lands D65.
    meas = _run_sdr_loop((0.336, 0.345), rounds=12)
    for _s, xyz in meas:
        x, y = _xy(xyz)
        assert abs(x - 0.3127) < 0.004 and abs(y - 0.3290) < 0.004, (x, y)


def test_refine_sdr_grayscale_leaves_d65_panel_alone():
    # An already-D65 panel needs ~no correction: deviations stay ~identity (1.0).
    _P, emit, peak = _synthetic_sdr_panel((0.3127, 0.3290))
    sigs = [0.3, 0.5, 0.7]
    meas = [(s, emit([s, s, s])) for s in sigs]
    _grid, dev = mc.refine_sdr_grayscale_legacy(None, meas, _PRIM, (0.3127, 0.3290), peak,
                                         (1.0, 1.0, 1.0), dark_floor_nits=0.5)
    for ch in "rgb":
        assert all(abs(v - 1.0) < 5e-3 for v in dev[ch]), (ch, dev[ch])


def test_refine_sdr_grayscale_idempotent_current_none_equals_identity():
    # Passing current=None must equal passing an explicit identity map (the stage resets to identity
    # for re-run idempotence — the two entry points must agree bit-for-bit).
    _P, emit, peak = _synthetic_sdr_panel((0.336, 0.345))
    sigs = [0.3, 0.5, 0.7]
    meas = [(s, emit([s, s, s])) for s in sigs]
    grid_a, dev_a = mc.refine_sdr_grayscale_legacy(None, meas, _PRIM, (0.336, 0.345), peak, (1.0, 1.0, 1.0))
    ident = {ch: [1.0] * len(grid_a) for ch in "rgb"}
    grid_b, dev_b = mc.refine_sdr_grayscale_legacy(ident, meas, _PRIM, (0.336, 0.345), peak, (1.0, 1.0, 1.0))
    assert grid_a == grid_b
    for ch in "rgb":
        assert dev_a[ch] == pytest.approx(dev_b[ch], abs=1e-12)


def test_refine_sdr_grayscale_uses_matrix_rowsums_for_abscissa():
    # The load-bearing trap fix: blue's deviation is keyed to the POST-matrix signal
    # rowsum_b**(1/γ)·s, so the SAME measurements under different rowsums place blue's correction at
    # different grid positions → different blue curves. A wire-keyed bug would produce identical
    # curves. Red/green (rowsum 1.0 in both) stay bit-identical.
    from dlc.colormath import invert3x3, matvec, xy_to_XYZ
    peak = 120.0
    disp = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                             _PRIM["bx"], _PRIM["by"], 0.3127, 0.3290, white_Y=peak)
    disp_inv = invert3x3(disp)
    s_lo, s_hi = 0.3, 0.5
    blue_scale = {s_lo: 1.0, s_hi: 0.6}                  # blue perfect low, 40% dim high (level-dep)
    meas = []
    for s in (s_lo, s_hi):
        tY = peak * (s ** _GAMMA)
        ts = matvec(disp_inv, xy_to_XYZ(0.3127, 0.3290, tY))
        ms = [ts[0], ts[1], ts[2] * blue_scale[s]]
        xyz = tuple(sum(disp[r][c] * ms[c] for c in range(3)) for r in range(3))
        meas.append((s, xyz))
    _g1, base = mc.refine_sdr_grayscale_legacy(None, meas, _PRIM, (0.3127, 0.3290), peak,
                                        (1.0, 1.0, 1.0), dark_floor_nits=0.5)
    _g2, shifted = mc.refine_sdr_grayscale_legacy(None, meas, _PRIM, (0.3127, 0.3290), peak,
                                           (1.0, 1.0, 2.0), dark_floor_nits=0.5)
    n = len(base["b"])
    assert max(abs(base["b"][j] - shifted["b"][j]) for j in range(n)) > 5e-3
    for ch in ("r", "g"):
        assert all(abs(base[ch][j] - shifted[ch][j]) < 1e-9 for j in range(n))


def test_refine_sdr_grayscale_trust_damps_noisy_dark_level():
    # A dark off-white level tagged with a chroma σ as large as its error → low trust → its deviation
    # is pulled less far from identity than the same level with no σ (full correction).
    from dlc.colormath import invert3x3, matvec, xy_to_XYZ
    peak = 120.0
    disp = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                             _PRIM["bx"], _PRIM["by"], 0.3127, 0.3290, white_Y=peak)
    disp_inv = invert3x3(disp)
    s = 0.25
    tY = peak * (s ** _GAMMA)
    ts = matvec(disp_inv, xy_to_XYZ(0.3127, 0.3290, tY))
    ms = [ts[0] * 1.3, ts[1], ts[2] * 0.7]               # warm dark read
    xyz = tuple(sum(disp[r][c] * ms[c] for c in range(3)) for r in range(3))
    tot = sum(xyz)
    err = ((xyz[0] / tot - 0.3127) ** 2 + (xyz[1] / tot - 0.3290) ** 2) ** 0.5
    _g, trusted = mc.refine_sdr_grayscale_legacy(None, [(s, xyz)], _PRIM, (0.3127, 0.3290), peak,
                                          (1.0, 1.0, 1.0), dark_floor_nits=0.1)
    _g2, noisy = mc.refine_sdr_grayscale_legacy(None, [(s, xyz, err)], _PRIM, (0.3127, 0.3290), peak,
                                         (1.0, 1.0, 1.0), dark_floor_nits=0.1)
    n = len(trusted["r"])
    move_trusted = sum(abs(trusted[ch][j] - 1.0) for ch in "rgb" for j in range(n))
    move_noisy = sum(abs(noisy[ch][j] - 1.0) for ch in "rgb" for j in range(n))
    assert move_noisy < move_trusted


def test_refine_sdr_grayscale_clamps_deviations():
    # An extreme measured error cannot drive a deviation past dev_clamp.
    from dlc.colormath import invert3x3, matvec, xy_to_XYZ
    peak = 120.0
    disp = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                             _PRIM["bx"], _PRIM["by"], 0.3127, 0.3290, white_Y=peak)
    disp_inv = invert3x3(disp)
    s = 0.5
    tY = peak * (s ** _GAMMA)
    ts = matvec(disp_inv, xy_to_XYZ(0.3127, 0.3290, tY))
    ms = [ts[0] * 100.0, ts[1], ts[2] * 0.001]           # absurd error (ratio clamp + dev clamp kick in)
    xyz = tuple(sum(disp[r][c] * ms[c] for c in range(3)) for r in range(3))
    _g, dev = mc.refine_sdr_grayscale_legacy(None, [(s, xyz)], _PRIM, (0.3127, 0.3290), peak,
                                      (1.0, 1.0, 1.0), dev_clamp=(0.25, 4.0))
    for ch in "rgb":
        assert all(0.25 - 1e-9 <= v <= 4.0 + 1e-9 for v in dev[ch])


# --- SDR base 1D-LUT cube: build + closed-loop refine (set_base_lut, γ2.2) --
# The cube-delivered SDR path (replaces the correctionGrayscale path 2026-06-24): build_sdr_cube is
# the γ analog of build_hdr_cube, and refine_sdr_cube composes onto a per-channel signal→signal .cube
# (like the HDR base cube) instead of emitting deviations. Same synthetic additive γ panel.

def _perfect_sdr_gray_ramp(n=40, gamma=_GAMMA):
    # gray tracks native white + power γ exactly: equal shares = s**γ (so the cube is identity).
    return [_gray_sample(i / (n - 1), ((i / (n - 1)) ** gamma,) * 3) for i in range(n)]


def test_build_sdr_cube_perfect_panel_yields_identity():
    curves, summary = mc.build_sdr_cube(_perfect_sdr_gray_ramp(), _PRIM, _WHITE, _PEAK, lut_size=512)
    assert summary["basis"] == "gray-ramp" and summary["gamma"] == _GAMMA
    n = 512
    for ch in "rgb":
        c = curves[ch]
        assert all(c[i] <= c[i + 1] + 1e-6 for i in range(n - 1))     # monotone
        assert c[0] <= 1e-3                                            # anchored at black
        for sig in (0.1, 0.3, 0.5, 0.7):                              # ~identity (perfect tracking)
            j = round(sig * (n - 1))
            assert abs(c[j] - sig) < 5e-3, (ch, sig, c[j])


def test_build_sdr_cube_blue_deficient_shadows_get_boosted():
    # Blue-deficient in the shadows (gray drifts yellow), neutral at the top: the cube must BOOST blue
    # over red above the dark floor and leave the peak alone (no fight with the matrix's white move).
    n = 40
    samples = []
    for i in range(n):
        s = i / (n - 1)
        frac = s ** _GAMMA
        b = frac * (0.6 + 0.4 * (i / (n - 1)))            # 40% deficient at black → neutral at peak
        samples.append(_gray_sample(s, (frac, frac, b)))
    curves, _ = mc.build_sdr_cube(samples, _PRIM, _WHITE, _PEAK, lut_size=512, dark_floor_nits=1.0)
    n = 512
    j = round(0.5 * (n - 1))
    assert curves["b"][j] > curves["r"][j] + 1e-3, (curves["b"][j], curves["r"][j])
    assert abs(curves["b"][-1] - curves["r"][-1]) < 2e-3    # peak: channels converge (no white override)


def test_build_sdr_cube_requires_neutral():
    with pytest.raises(ValueError):
        mc.build_sdr_cube([Ti3Sample(rgb=(0.0, 0.0, 0.0), xyz=(0.0, 0.0, 0.0))],
                          _PRIM, _WHITE, _PEAK)


def _run_sdr_cube_loop(white_xy, rounds=14, peak=120.0):
    _P, emit, _peak = _synthetic_sdr_panel(white_xy, peak_nits=peak)
    N = 256
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}                          # identity seed
    sigs = [0.3, 0.4, 0.5, 0.6, 0.7]
    def measure(cube):
        out = []
        for s in sigs:                                               # rowsums=(1,1,1) ⇒ post-matrix sig == s
            drive = [_interp_list(grid, cube[ch], s) for ch in "rgb"]
            out.append((s, emit(drive)))
        return out
    for _ in range(rounds):
        cube = mc.refine_sdr_cube(cube, measure(cube), _PRIM, white_xy, peak,
                                  (1.0, 1.0, 1.0), dark_floor_nits=0.5)
    return measure(cube)


def test_refine_sdr_cube_converges_warm_panel_to_d65():
    # A warm panel driven neutral renders ~its warm white; the closed loop must bend the base cube
    # until the measured neutral lands D65.
    meas = _run_sdr_cube_loop((0.336, 0.345), rounds=16)
    for _s, xyz in meas:
        x, y = _xy(xyz)
        assert abs(x - 0.3127) < 0.004 and abs(y - 0.3290) < 0.004, (x, y)


def test_refine_sdr_cube_leaves_d65_panel_alone():
    _P, emit, peak = _synthetic_sdr_panel((0.3127, 0.3290))
    N = 128
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}
    sigs = [0.3, 0.5, 0.7]
    meas = [(s, emit([_interp_list(grid, cube[ch], s) for ch in "rgb"])) for s in sigs]
    new = mc.refine_sdr_cube(cube, meas, _PRIM, (0.3127, 0.3290), peak, (1.0, 1.0, 1.0), dark_floor_nits=0.5)
    for ch in "rgb":
        for j in range(0, N, 8):
            assert abs(new[ch][j] - cube[ch][j]) < 5e-3, (ch, j)


def test_refine_sdr_cube_uses_matrix_rowsums_for_abscissa():
    # The load-bearing trap: blue's factor is keyed to the POST-matrix signal rowsum_b**(1/γ)·s, so the
    # SAME measurements under different rowsums place blue's correction at different grid positions →
    # different blue curves. A wire-keyed bug would produce identical curves. Red/green stay identical.
    from dlc.colormath import invert3x3, matvec, xy_to_XYZ
    peak = 120.0
    disp = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                             _PRIM["bx"], _PRIM["by"], 0.3127, 0.3290, white_Y=peak)
    disp_inv = invert3x3(disp)
    N = 512
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}
    s_lo, s_hi = 0.3, 0.5
    blue_scale = {s_lo: 1.0, s_hi: 0.6}                              # perfect low, 40% too dim high
    meas = []
    for s in (s_lo, s_hi):
        tY = peak * (s ** _GAMMA)
        ts = matvec(disp_inv, xy_to_XYZ(0.3127, 0.3290, tY))
        ms = [ts[0], ts[1], ts[2] * blue_scale[s]]
        xyz = tuple(sum(disp[r][c] * ms[c] for c in range(3)) for r in range(3))
        meas.append((s, xyz))
    base = mc.refine_sdr_cube(cube, meas, _PRIM, (0.3127, 0.3290), peak, (1.0, 1.0, 1.0), dark_floor_nits=0.5)
    shifted = mc.refine_sdr_cube(cube, meas, _PRIM, (0.3127, 0.3290), peak, (1.0, 1.0, 2.0), dark_floor_nits=0.5)
    assert max(abs(base["b"][j] - shifted["b"][j]) for j in range(N)) > 5e-3
    for ch in ("r", "g"):
        assert all(abs(base[ch][j] - shifted[ch][j]) < 1e-9 for j in range(N))


# --- σ-aware adaptive dark floor (Phase 4: real drift is not noise) ----------

def test_adaptive_dark_floor_stable_real_drift_does_not_raise_floor():
    # A REAL, repeatable dark drift (drift >> its measured σ) is the disease the cube exists to
    # correct — it must NOT raise the floor and smooth its own correction away. The per-level
    # trust machinery (dark_trust_weights) governs it instead.
    reads = [
        (0.5, 0.3127, 0.3290, 0.0005),   # deep dark, clean, stable
        (1.0, 0.315, 0.345, 0.0005),     # REAL drift (|dxy| ~0.016), σ tiny -> correctable signal
        (3.0, 0.314, 0.342, 0.0005),     # REAL drift, stable
        (20.0, 0.3127, 0.3290, 0.0005),
        (150.0, 0.3127, 0.3290, 0.0005),
        (400.0, 0.3127, 0.3290, 0.0005),
    ]
    floor, info = mc.adaptive_dark_floor(reads, reference_band=None, bounds=(0.1, 5.0))
    assert floor == 0.1, (floor, info)                       # nothing untrustworthy -> low bound
    assert info["n_strayed"] == 0 and info["n_real_drift"] == 2


def test_adaptive_dark_floor_unstable_dark_read_still_raises_floor():
    # An `unstable` level (σ=+inf: the loop couldn't pin it) stays untrustworthy — floor rises.
    reads = [
        (0.5, 0.3127, 0.3290, 0.0005),
        (1.0, 0.315, 0.345, math.inf),   # strayed AND unstable -> raise the floor over it
        (20.0, 0.3127, 0.3290, 0.0005),
        (150.0, 0.3127, 0.3290, 0.0005),
        (400.0, 0.3127, 0.3290, 0.0005),
    ]
    floor, info = mc.adaptive_dark_floor(reads, reference_band=None, bounds=(0.1, 5.0))
    assert floor == 1.0 and info["reason"] == "chroma_drift"
    assert info["n_strayed"] == 1 and info["n_real_drift"] == 0


def test_adaptive_dark_floor_sigma_less_strays_stay_conservative():
    # Without σ (single-read run / no sidecar) a strayed dark read keeps the old conservative
    # behaviour: noise and real drift are indistinguishable, so the floor rises (3-tuples and
    # 4-tuples with noise=None behave identically).
    reads3 = [(0.3, 0.34, 0.30), (0.8, 0.33, 0.31), (2.0, 0.315, 0.328),
              (50.0, 0.3127, 0.3290), (300.0, 0.3127, 0.3290)]
    reads4 = [(n, x, y, None) for (n, x, y) in reads3]
    f3, i3 = mc.adaptive_dark_floor(reads3, chroma_tolerance=0.008, bounds=(0.1, 5.0))
    f4, i4 = mc.adaptive_dark_floor(reads4, chroma_tolerance=0.008, bounds=(0.1, 5.0))
    assert f3 == f4 == 0.8
    assert i3["n_strayed"] == i4["n_strayed"] == 2


# --- gray-share monotone enforcement on NON-monotone measured shares ---------

def test_nonmonotone_measured_shares_still_yield_monotone_invertible_cube():
    # A noisy gray ramp whose blue share DIPS mid-ramp (non-monotone measured data). The
    # monotone enforcement in _gray_shares must keep the inversion well-defined and the cube
    # monotone — no oscillation leaks through to the curves.
    samples = []
    smax = mc.pq_oetf(_PEAK / 10000.0)
    n = 24
    for i in range(n):
        s = smax * i / (n - 1)
        frac = min(mc.pq_eotf(s) * 10000.0 / _PEAK, 1.0)
        b = frac
        if 8 <= i <= 10:                     # a mid-ramp dip: measured blue share goes DOWN
            b = frac * 0.55
        samples.append(_gray_sample(s, (frac, frac, b)))
    curves, _ = mc.build_hdr_cube(samples, _PRIM, _WHITE, _PEAK, lut_size=256, dark_floor_nits=0.3)
    for ch in "rgb":
        c = curves[ch]
        assert all(c[i] <= c[i + 1] + 1e-9 for i in range(len(c) - 1)), ch   # strictly monotone out
        assert 0.0 <= min(c) and max(c) <= 1.0


# --- END-TO-END abscissa verification (Phase 4 seeded lead #1) ---------------
# Simulate the FULL Windows pipeline — wire -> DeGamma -> MHC2 matrix (linear RGB) -> ReGamma ->
# per-channel 1D LUT -> panel — with a NON-identity matrix (warm native white) and a per-level
# channel defect the matrix cannot fix. The refine, fed the production rowsums, must land every
# measured neutral above the dark floor on D65. This independently verifies the post-matrix
# abscissa convention (factor stored at oetf(rowsum·eotf(s)), cube indexed in the same space).

def _mid_sag(d, top):
    """A per-level defect: the channel's drive->light response sags up to 30% mid-range
    (clean at black and full drive) — per-level tracking error only the cube can fix."""
    t = max(0.0, min(d / top, 1.0))
    return 1.0 - 0.30 * (4.0 * t * (1.0 - t)) ** 2


def test_refine_hdr_cube_end_to_end_converges_through_nonidentity_matrix():
    peak = 1000.0
    warm = (0.336, 0.345)                                    # warm native white -> rowsums != 1
    P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                          _PRIM["bx"], _PRIM["by"], warm[0], warm[1], white_Y=peak)
    peaks = [[P[r][c] for r in range(3)] for c in range(3)]
    smax = mc.pq_oetf(peak / 10000.0)

    def emit(drive):
        f = [mc.pq_eotf(max(0.0, min(d, smax))) / mc.pq_eotf(smax) for d in drive]
        f[2] *= _mid_sag(drive[2], smax)                      # blue mid-range sag
        return tuple(sum(P[r][c] * f[c] for c in range(3)) for r in range(3))

    # Installed HDR matrix: native-target white-only move (the C++ default) — diagonal, rowsums != 1.
    M = mc.mhc2_matrix(_PRIM, warm, _PRIM, (0.3127, 0.3290))
    rowsums = [sum(M[r]) for r in range(3)]
    assert max(abs(v - 1.0) for v in rowsums) > 0.05          # genuinely non-identity
    cap, _b = mc.peak_chroma_luminance(peaks)

    N = 512
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}

    def render(cube, s):
        """wire s (neutral) -> matrix on linear RGB -> ReGamma -> 1D LUT -> panel."""
        lin = mc.pq_eotf(s)
        drive = []
        for c, ch in enumerate("rgb"):
            post = mc.pq_oetf(min(max(rowsums[c] * lin, 0.0), 1.0))
            drive.append(_interp_list(grid, cube[ch], post))
        return emit(drive)

    sigs = [f * smax for f in (0.3, 0.45, 0.6, 0.75, 0.9)]    # all above the 0.5-nit floor
    for _ in range(6):
        meas = [(s, render(cube, s)) for s in sigs]
        cube = mc.refine_hdr_cube(cube, meas, peaks, rowsums,
                                  peak_cap_nits=cap, dark_floor_nits=0.5)
    for s in sigs:
        xyz = render(cube, s)
        x, y = _xy(xyz)
        assert abs(x - 0.3127) < 0.002 and abs(y - 0.3290) < 0.002, (s, x, y)


def test_refine_sdr_cube_end_to_end_converges_through_nonidentity_matrix():
    peak = 120.0
    warm = (0.336, 0.345)
    SRGB = {"rx": 0.64, "ry": 0.33, "gx": 0.30, "gy": 0.60, "bx": 0.15, "by": 0.06}
    P = rgb_to_xyz_matrix(_PRIM["rx"], _PRIM["ry"], _PRIM["gx"], _PRIM["gy"],
                          _PRIM["bx"], _PRIM["by"], warm[0], warm[1], white_Y=peak)

    def emit(drive):
        f = [max(0.0, min(d, 1.0)) ** _GAMMA for d in drive]
        f[2] *= _mid_sag(drive[2], 1.0)
        return tuple(sum(P[r][c] * f[c] for c in range(3)) for r in range(3))

    # Installed SDR matrix: src = sRGB@D65, display = native + warm white (full 3x3).
    M = mc.mhc2_matrix(_PRIM, warm, SRGB, (0.3127, 0.3290))
    rowsums = [sum(M[r]) for r in range(3)]
    assert max(abs(v - 1.0) for v in rowsums) > 0.05

    N = 512
    grid = [j / (N - 1) for j in range(N)]
    cube = {ch: list(grid) for ch in "rgb"}

    def render(cube, s):
        lin = s ** _GAMMA
        drive = []
        for c, ch in enumerate("rgb"):
            post = min(max(rowsums[c] * lin, 0.0), 1.0) ** (1.0 / _GAMMA)
            drive.append(_interp_list(grid, cube[ch], post))
        return emit(drive)

    sigs = [0.3, 0.45, 0.6, 0.75, 0.9]
    for _ in range(10):
        meas = [(s, render(cube, s)) for s in sigs]
        cube = mc.refine_sdr_cube(cube, meas, _PRIM, warm, peak, rowsums,
                                  gamma=_GAMMA, dark_floor_nits=0.2)
    for s in sigs:
        x, y = _xy(render(cube, s))
        assert abs(x - 0.3127) < 0.002 and abs(y - 0.3290) < 0.002, (s, x, y)
