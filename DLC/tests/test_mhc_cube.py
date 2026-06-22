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
    rowsums = [sum(M[r]) for r in range(3)]
    assert rowsums[2] > rowsums[0]   # blue driven harder than red to cool the white
