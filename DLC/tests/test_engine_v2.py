"""Tests for the v2 colour engine (``dlc.engine.*``): patch generation, the RBF
display model + LUT builder, the SDR matrix+curve LUT, and the SPD-derived white
point.

Skips cleanly when numpy / scipy / colour are absent so the dependency-free spine
suite still runs on a box without the engine extra installed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc.engine import patches as P
from dlc.engine import lut_rbf as L
from dlc.engine import lut_sdr as S
from dlc.engine import whitepoint as W
from dlc.engine.model import DisplayErrorModel, Target, TargetSpace, de_itp

# Hardware-validated white-point regression against real panel captures that live
# in a LOCAL lab dir (not in this repo). Point the DLC_COLORCAL env var at that dir
# to run these two tests; otherwise they skip. Keeps machine paths out of the repo.
_COLORCAL = os.environ.get("DLC_COLORCAL")
COLORCAL = Path(_COLORCAL) if _COLORCAL else None
# Discover captures by generic pattern (instrument/mode), not a hardcoded name.
WHITE_SP = next(iter(sorted(COLORCAL.glob("spectral_SDR_*/white.sp"), reverse=True)),
                None) if COLORCAL else None
CR250_CSV = next(iter(COLORCAL.glob("*CR-250*.csv")), None) if COLORCAL else None


# ---------------------------------------------------------------------------
# Synthetic display helpers (realistic in-gamut panel error: per-channel
# gain + gamma, i.e. grey-tracking + mild gamma — the post-MHC residual).
# ---------------------------------------------------------------------------

def _synth_measure(space: TargetSpace, signal, *, gains=(1.0, 0.992, 0.985),
                   gammas=(1.0, 1.010, 1.018), noise=0.004, seed=3):
    s = np.clip(np.asarray(signal, float), 0, 1)
    s_eff = np.clip(np.array(gains) * s ** np.array(gammas), 0, 1)
    measured = space.ideal_xyz(s_eff)
    if noise:
        rng = np.random.RandomState(seed)
        measured = measured * (1 + noise * rng.standard_normal(measured.shape))
    return np.maximum(measured, 0)


# ===========================================================================
# patches
# ===========================================================================

def test_transfer_roundtrip():
    for tf in (P.Transfer.pq(), P.Transfer.power(2.2, 120.0)):
        for nits in (0.5, 5.0, 50.0, tf.cv_to_nits(tf.max_cv) * 0.9):
            cv = tf.nits_to_cv(nits)
            assert abs(tf.cv_to_nits(cv) - nits) < max(0.05 * nits, 0.05)


def test_floor_is_positive_and_dark():
    pq, pw = P.Transfer.pq(), P.Transfer.power(2.2, 120.0)
    assert 0 < pq.floor_cv() < pq.max_cv // 2
    assert 0 < pw.floor_cv() < pw.max_cv // 2


def test_ramp_anchors_and_dedup():
    tf = P.Transfer.power(2.2, 120.0)
    ramp = P.ramp_patches(tf, steps=21, order="thermal")
    assert (0, 0, 0) in ramp and (tf.max_cv,) * 3 in ramp
    assert len(ramp) == len(set(ramp))  # deduped


def test_cube_count_and_dedup():
    tf = P.Transfer.pq()
    cube = P.cube_patches(tf, size=9)
    assert len(cube) == 9 ** 3
    assert len(cube) == len(set(cube))


def test_tube_denser_than_cube_with_neutral_core():
    tf = P.Transfer.pq()
    tube = P.tube_patches(tf, cube_size=9, tube_size=33, tube_radius=2, spines=True)
    assert len(tube) > 9 ** 3
    # neutral axis present at full resolution
    greys = [p for p in tube if p[0] == p[1] == p[2]]
    assert len(greys) >= 33


def test_thermal_balances_windows_far_better_than_luminance():
    tf = P.Transfer.pq()

    def lum(p):
        return sum(w * tf.cv_to_nits(c) for w, c in zip((0.2126, 0.7152, 0.0722), p))

    def worst_window(order, win=30):
        s = P.cube_patches(tf, size=9, order=order)
        lu = [lum(p) for p in s]
        gm = sum(lu) / len(lu)
        return max(abs(sum(lu[i:i + win]) / win - gm) for i in range(len(lu) - win))

    assert worst_window("thermal") < worst_window("luminance") / 3


def test_gamut_respects_floor_and_has_neutral_axis():
    tf = P.Transfer.pq()
    floor = tf.floor_cv()
    pts = P.gamut_patches(tf, lum_steps=17, hues=12)
    # only black may sit below the floor on the neutral axis
    for r, g, b in pts:
        if r == g == b and r != 0:
            assert r >= floor - 1
    greys = sorted({p[0] for p in pts if p[0] == p[1] == p[2]})
    assert greys[0] == 0 and len(greys) >= 5


def test_to_signal_range():
    tf = P.Transfer.power(2.2, 120.0)
    sig = P.to_signal(P.cube_patches(tf, size=5), tf)
    arr = np.array(sig)
    assert arr.min() == 0.0 and abs(arr.max() - 1.0) < 1e-9


# ===========================================================================
# model + lut_rbf
# ===========================================================================

def test_model_fits_synthetic_panel():
    target, tf = Target.hdr_rec2020_pq(), P.Transfer.pq()
    space = TargetSpace(target)
    sig = np.array(P.to_signal(P.cube_patches(tf, size=9), tf))
    model = DisplayErrorModel(sig, _synth_measure(space, sig), target)
    es = model.error_summary()
    assert es.mean > 1.0  # synthetic error is visible
    assert float(model.residuals().mean()) < es.mean  # fit explains most of it
    # forward() reproduces the measured panel within the fit residual
    fwd = de_itp(space.xyz_to_ictcp(model.forward(sig))
                 - space.xyz_to_ictcp(_synth_measure(space, sig)))
    assert float(fwd.mean()) < 2 * es.mean


def test_build_cube_reduces_error_and_is_mostly_monotonic():
    target, tf = Target.hdr_rec2020_pq(), P.Transfer.pq()
    space = TargetSpace(target)
    train = np.array(P.to_signal(
        P.tube_patches(tf, cube_size=9, tube_size=33, tube_radius=2, spines=True), tf))
    model = DisplayErrorModel(train, _synth_measure(space, train), target)

    before = L.predicted_accuracy(model, L.identity_cube(33), train, max_data_signal=1.0)
    cube = L.build_cube(model, 33, train, n_iterations=4)
    after = L.predicted_accuracy(model, cube, train, max_data_signal=1.0)
    diag = L.cube_diagnostics(cube)

    assert after["mean"] < before["mean"] / 4          # correction cuts mean >4x
    assert diag.non_monotonic < 0.01 * diag.total_steps  # near-monotonic
    # any non-monotonic steps are confined to deep shadow (near-black blend)
    axis = np.linspace(0, 1, 33)
    bi, gi, ri = np.where(np.diff(cube[:, :, :, 0], axis=2) < -1e-9)
    if ri.size:
        assert max(axis[r] for r in ri) < 0.35


def test_write_cube_roundtrip(tmp_path):
    cube = L.identity_cube(17)
    path = tmp_path / "id.cube"
    L.write_cube(cube, str(path), title="t")
    lines = path.read_text().splitlines()
    assert lines[0].startswith("TITLE") and "LUT_3D_SIZE 17" in lines[1]
    data = [ln for ln in lines if ln[:1].isdigit()]
    assert len(data) == 17 ** 3
    # identity: first data row is 0 0 0, last is 1 1 1
    assert data[0].split()[0] == "0.000000"
    assert all(abs(float(v) - 1.0) < 1e-6 for v in data[-1].split())


def test_hull_distance_degenerate_is_safe():
    # collinear points cannot form a 3D hull → all-zero distances, no crash
    pts = np.array([[t, t, t] for t in np.linspace(0, 1, 10)])
    grid = np.random.RandomState(0).random((50, 3))
    d = L.compute_hull_distance(grid, pts)
    assert d.shape == (50,) and np.all(d == 0)


# ===========================================================================
# lut_sdr
# ===========================================================================

def _synth_ramps(native_cs_name, *, white_nits=120.0, gammas=(2.2, 2.2, 2.2), levels=21):
    import colour
    cs = colour.RGB_COLOURSPACES[native_cs_name]
    peaks = {ch: colour.RGB_to_XYZ(np.array(oh, float), cs) * white_nits
             for ch, oh in [("red", (1, 0, 0)), ("green", (0, 1, 0)), ("blue", (0, 0, 1))]}
    lv = np.linspace(0, 1, levels)
    return {ch: [(float(l), (peaks[ch] * (l ** gm)).tolist()) for l in lv]
            for ch, gm in zip(("red", "green", "blue"), gammas)}


def test_sdr_identity_when_native_equals_target():
    ramps = _synth_ramps("sRGB")
    lut, _ = S.build_sdr_cube(ramps, primaries_name="sRGB", white_xy=(0.3127, 0.3290),
                              white_nits=120.0, gamma=2.2, grid_size=17)
    axis = np.linspace(0, 1, 17)
    B, G, R = np.meshgrid(axis, axis, axis, indexing="ij")
    ident = np.stack([R, G, B], axis=-1)
    assert float(np.max(np.abs(lut - ident))) < 0.02


def test_sdr_wide_gamut_maps_inward_and_is_consistent():
    ramps = _synth_ramps("ITU-R BT.2020", gammas=(2.3, 2.15, 2.45))
    lut, info = S.build_sdr_cube(ramps, primaries_name="sRGB", white_xy=(0.3127, 0.3290),
                                 white_nits=120.0, gamma=2.2, grid_size=33)
    # the native red primary is wider than sRGB red
    assert info.primary_xy["red"][0] > 0.66
    # to hit sRGB red on a wide panel, the cube must add G/B drive (desaturate)
    red_corner = lut[0, 0, -1]
    assert red_corner[1] > 0.05 and red_corner[2] > 0.05
    val = S.model_validation(lut, ramps, primaries_name="sRGB", white_xy=(0.3127, 0.3290),
                             white_nits=120.0, gamma=2.2)
    assert val["xyz_euclidean_mean"] < 0.5


def test_sdr_missing_channel_raises():
    with pytest.raises(ValueError):
        S.build_sdr_cube({"red": [(1.0, [1, 1, 1])], "green": [(1.0, [1, 1, 1])]})


def test_drive_for_y_clamps_out_of_domain():
    # inv_y extrapolates (Pchip blows up far out of the measured domain); drive_for_y must
    # clamp at the SOURCE so an out-of-range request can't yield a garbage / black-for-white
    # drive when a caller forgets to clip (M4).
    ramps = _synth_ramps("sRGB")
    model = S.make_channel_model(ramps["green"])
    assert model.drive_for_y(model.peak_y * 1000.0) == pytest.approx(1.0)   # over peak -> 1
    assert model.drive_for_y(-1e6) == 0.0                                   # below black -> 0
    # the raw extrapolating interpolator is wildly UNbounded (huge negative over peak, huge
    # positive below black) — proving the guard does real work, not just echoing a clip.
    assert not (0.0 <= float(model.inv_y(model.peak_y * 1000.0)) <= 1.0)
    assert not (0.0 <= float(model.inv_y(-1e6)) <= 1.0)


# ===========================================================================
# whitepoint
# ===========================================================================

def test_reference_d65_under_1931_is_textbook():
    x, y = W.reference_white_xy("1931_2")
    assert abs(x - 0.3127) < 0.001 and abs(y - 0.3290) < 0.001


def test_reference_d65_modern_observer_shifts_green():
    # CIE 2015 2° places D65 slightly higher in x and y than 1931 (known shift).
    x31, y31 = W.reference_white_xy("1931_2")
    x15, y15 = W.reference_white_xy("2015_2")
    assert x15 > x31 and y15 > y31


def test_corrected_white_differs_from_measured_for_narrowband():
    # A synthetic narrowband-ish white: three Gaussian primaries.
    import colour
    wl = np.arange(380, 731, 5.0)

    def gauss(c, w, a):
        return a * np.exp(-0.5 * ((wl - c) / w) ** 2)

    vals = gauss(450, 12, 1.0) + gauss(530, 12, 1.0) + gauss(620, 14, 1.2)
    spd = colour.SpectralDistribution(dict(zip(wl, vals)), name="synthQD")
    measured = W.spd_to_xy(spd, "1931_2")
    corrected = W.corrected_white_xy(spd, "2015_2", anchor="reference")
    # narrowband → a non-trivial metameric shift
    assert (abs(measured[0] - corrected[0]) + abs(measured[1] - corrected[1])) > 0.002


def test_white_from_spd_file_strength_dial(tmp_path):
    # The path-based helper the profile resolver calls: write a synthetic narrowband
    # white as a CGATS .sp, then strength 0 = numeric D65, strength 1 = corrected.
    wl = np.arange(380, 731, 5.0)

    def gauss(c, w, a):
        return a * np.exp(-0.5 * ((wl - c) / w) ** 2)

    vals = gauss(450, 12, 1.0) + gauss(530, 12, 1.0) + gauss(620, 14, 1.2)
    sp = tmp_path / "white.sp"
    sp.write_text(
        'CGATS.17\nSPECTRAL_BANDS "%d"\nSPECTRAL_START_NM "380"\nSPECTRAL_END_NM "730"\n'
        "BEGIN_DATA\n" % len(vals) + " ".join("%.6f" % v for v in vals) + "\nEND_DATA\n",
        encoding="utf-8")

    at0 = W.white_from_spd_file(sp, strength=0.0)
    assert at0["xy"] == pytest.approx(W.LEGACY_D65_XY, abs=1e-9)
    at1 = W.white_from_spd_file(sp, strength=1.0)
    assert (abs(at1["xy"][0] - at0["xy"][0]) + abs(at1["xy"][1] - at0["xy"][1])) > 0.002
    assert at1["cct"] > 0 and "duv" in at1 and at1["observer"] == "2015_2"


@pytest.mark.skipif(WHITE_SP is None, reason="set DLC_COLORCAL to the local lab dir")
def test_real_white_sp_integrates_to_plausible_white():
    # End-to-end SPD pipeline check on a real capture: a calibrated display white
    # must integrate to a sane near-D65 chromaticity under CIE 1931. (The tight
    # cross-check against the meter's own reading is done locally, not in-repo.)
    spd = W.load_sp(WHITE_SP)
    x, y = W.spd_to_xy(spd, "1931_2")
    assert 0.29 < x < 0.33 and 0.31 < y < 0.35


@pytest.mark.skipif(WHITE_SP is None or CR250_CSV is None,
                    reason="set DLC_COLORCAL to the local lab dir")
def test_cross_instrument_metameric_offset_agrees():
    # Two independent instruments must agree on Δ (it's a property of the QD
    # primaries' spectral shape, not the instrument or the session's white).
    own = W.load_sp(WHITE_SP)
    cr = W.load_cr250(CR250_CSV)
    assert {"red", "green", "blue", "white"} <= set(cr)
    d_own = W.observer_offset(own, "2015_2")
    d_cr = W.observer_offset(cr["white"], "2015_2")
    assert abs(d_own[0] - d_cr[0]) < 0.002 and abs(d_own[1] - d_cr[1]) < 0.002
