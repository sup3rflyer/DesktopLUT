"""Golden-vector cross-pins for every colour-math copy in DLC (Phase 1 audit).

DLC deliberately carries dependency-free (stdlib) ports of colour math in the spine
and dashboard, alongside the colour-science versions the engine uses. This module is
the drift alarm: every hand-rolled copy is pinned against published reference vectors
(Sharma 2005 CIEDE2000, ST 2084 rationals, BT.2124, Robertson 1968) and against the
colour-science implementation, and the copies are pinned against EACH OTHER — so a
future edit to any one copy fails a test here instead of silently making a chart
disagree with a score.

Layout:
  stdlib-only pins   — run everywhere the spine runs (no numpy/colour needed).
  engine cross-pins  — `pytest.importorskip` the engine stack; skip cleanly without it.
"""

from __future__ import annotations

import math

import pytest

from dlc import _pq
from dlc import mhc_cube
from dlc.colormath import invert3x3, rgb_to_xyz_matrix
from dlc.dashboard import colorimetry as dcol
from dlc.dashboard import state as dstate
from dlc.engine import patches as epatches
from dlc.metrics import (
    SRGB_TO_XYZ_D65, _finite_nonneg_xyz, delta_e2000, npm_for_white, percentile,
    xyz_to_lab,
)

D65 = (0.3127, 0.3290)


# ---------------------------------------------------------------------------
# PQ / ST 2084 — one shared stdlib transfer (dlc._pq), consumed by mhc_cube,
# engine.patches, dashboard.colorimetry, dashboard.state; engine uses colour's.
# ---------------------------------------------------------------------------

def _pq_signal_sweep() -> list[float]:
    return [i / 400.0 for i in range(401)] + [1e-12, 1e-8, 7.3e-7, 1e-6, 0.508, 0.8162]


def test_pq_consumers_are_the_shared_copy_not_ports():
    # Aliases, not re-implementations: identity, so drift is structurally impossible.
    assert mhc_cube.pq_eotf is _pq.eotf_norm
    assert mhc_cube.pq_oetf is _pq.oetf_norm
    assert dcol._pq_eotf_norm is _pq.eotf_norm
    assert dcol._pq_oetf_norm is _pq.oetf_norm


def test_pq_eotf_oetf_roundtrip_and_edges():
    for s in _pq_signal_sweep():
        assert math.isclose(_pq.oetf_norm(_pq.eotf_norm(s)), s, abs_tol=1e-9) or s < 7.4e-7
    # Below the PQ toe (~7.3e-7 signal) the EOTF is exactly 0 — black stays black.
    assert _pq.eotf_norm(0.0) == 0.0
    assert _pq.eotf_norm(-0.5) == 0.0
    # ST 2084's inverse-EOTF at Y=0 is c1^m2 ≈ 7.31e-7, NOT 0 — the standard's own
    # formula (colour.eotf_inverse_ST2084(0) returns the same); eotf() maps the whole
    # [0, c1^m2] toe back to exactly 0, so black still round-trips to black.
    toe = 0.8359375 ** 78.84375
    assert _pq.oetf_norm(0.0) == pytest.approx(toe, rel=1e-12)
    assert _pq.oetf_norm(-1.0) == _pq.oetf_norm(0.0)
    assert _pq.eotf_norm(_pq.oetf_norm(0.0)) == 0.0
    # Full signal is the 10000-nit container top.
    assert math.isclose(_pq.eotf_norm(1.0), 1.0, rel_tol=1e-12)
    assert math.isclose(_pq.oetf_norm(1.0), 1.0, rel_tol=1e-12)


def test_pq_known_anchors():
    # Published ST 2084 anchors: 100 nits → ~0.508 signal; 1800 nits → ~0.8162.
    assert math.isclose(_pq.oetf_norm(100.0 / 10000.0), 0.5081, abs_tol=5e-4)
    assert math.isclose(_pq.oetf_norm(1800.0 / 10000.0), 0.81623, abs_tol=5e-4)
    assert math.isclose(_pq.eotf_norm(0.5) * 10000.0, 92.246, rel_tol=1e-4)


def test_state_pq_eotf_is_shared_transfer_plus_dashboard_clamps():
    for s in (0.0, 0.1, 0.5, 0.75, 1.0):
        assert dstate._pq_eotf(s, None) == _pq.CONTAINER_NITS * _pq.eotf_norm(s)
    # peak clamp + out-of-range signal clamp are the wrapper's own (dashboard) semantics
    assert dstate._pq_eotf(1.0, 800.0) == 800.0
    assert dstate._pq_eotf(1.7, None) == dstate._pq_eotf(1.0, None)
    assert dstate._pq_eotf(-0.2, None) == 0.0


def test_patches_code_value_helpers_roundtrip_both_bit_depths():
    for bd in (8, 10):
        max_cv = (1 << bd) - 1
        for cv in range(0, max_cv + 1, 7):
            nits = epatches.pq_to_luminance(cv, bd)
            assert epatches.luminance_to_pq(nits, bd) == cv
        assert epatches.pq_to_luminance(0, bd) == 0.0
        assert epatches.luminance_to_pq(0.0, bd) == 0
        assert epatches.luminance_to_pq(1e9, bd) == max_cv  # over-container clamps to top code


def test_pq_matches_colour_st2084():
    pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    for s in _pq_signal_sweep():
        ref_nits = float(colour.models.eotf_ST2084(s))
        assert abs(_pq.eotf_norm(s) * 10000.0 - ref_nits) < 1e-6, s
    for nits in (0.0, 0.001, 0.05, 0.3, 1.0, 100.0, 203.0, 1000.0, 1800.0, 10000.0):
        ref_sig = float(colour.models.eotf_inverse_ST2084(nits))
        assert abs(_pq.oetf_norm(nits / 10000.0) - ref_sig) < 1e-12, nits


# ---------------------------------------------------------------------------
# CIEDE2000 — Sharma, Wu & Dalal (2005) Table 1: the 34 published test pairs.
# Two stdlib copies (metrics, dashboard.colorimetry) — both must hit the table
# and each other.
# ---------------------------------------------------------------------------

# (L1, a1, b1, L2, a2, b2, expected ΔE00) — values as published (4 decimals).
SHARMA_2005 = [
    (50.0000, 2.6772, -79.7751, 50.0000, 0.0000, -82.7485, 2.0425),
    (50.0000, 3.1571, -77.2803, 50.0000, 0.0000, -82.7485, 2.8615),
    (50.0000, 2.8361, -74.0200, 50.0000, 0.0000, -82.7485, 3.4412),
    (50.0000, -1.3802, -84.2814, 50.0000, 0.0000, -82.7485, 1.0000),
    (50.0000, -1.1848, -84.8006, 50.0000, 0.0000, -82.7485, 1.0000),
    (50.0000, -0.9009, -85.5211, 50.0000, 0.0000, -82.7485, 1.0000),
    (50.0000, 0.0000, 0.0000, 50.0000, -1.0000, 2.0000, 2.3669),
    (50.0000, -1.0000, 2.0000, 50.0000, 0.0000, 0.0000, 2.3669),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0009, 7.1792),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0010, 7.1792),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0011, 7.2195),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0012, 7.2195),
    (50.0000, -0.0010, 2.4900, 50.0000, 0.0009, -2.4900, 4.8045),
    (50.0000, -0.0010, 2.4900, 50.0000, 0.0010, -2.4900, 4.8045),
    (50.0000, -0.0010, 2.4900, 50.0000, 0.0011, -2.4900, 4.7461),
    (50.0000, 2.5000, 0.0000, 50.0000, 0.0000, -2.5000, 4.3065),
    (50.0000, 2.5000, 0.0000, 73.0000, 25.0000, -18.0000, 27.1492),
    (50.0000, 2.5000, 0.0000, 61.0000, -5.0000, 29.0000, 22.8977),
    (50.0000, 2.5000, 0.0000, 56.0000, -27.0000, -3.0000, 31.9030),
    (50.0000, 2.5000, 0.0000, 58.0000, 24.0000, 15.0000, 19.4535),
    (50.0000, 2.5000, 0.0000, 50.0000, 3.1736, 0.5854, 1.0000),
    (50.0000, 2.5000, 0.0000, 50.0000, 3.2972, 0.0000, 1.0000),
    (50.0000, 2.5000, 0.0000, 50.0000, 1.8634, 0.5757, 1.0000),
    (50.0000, 2.5000, 0.0000, 50.0000, 3.2592, 0.3350, 1.0000),
    (60.2574, -34.0099, 36.2677, 60.4626, -34.1751, 39.4387, 1.2644),
    (63.0109, -31.0961, -5.8663, 62.8187, -29.7946, -4.0864, 1.2630),
    (61.2901, 3.7196, -5.3901, 61.4292, 2.2480, -4.9620, 1.8731),
    (35.0831, -44.1164, 3.7933, 35.0232, -40.0716, 1.5901, 1.8645),
    (22.7233, 20.0904, -46.6940, 23.0331, 14.9730, -42.5619, 2.0373),
    (36.4612, 47.8580, 18.3852, 36.2715, 50.5065, 21.2231, 1.4146),
    (90.8027, -2.0831, 1.4410, 91.1528, -1.6435, 0.0447, 1.4441),
    (90.9257, -0.5406, -0.9208, 88.6381, -0.8985, -0.7239, 1.5381),
    (6.7747, -0.2908, -2.4247, 5.8714, -0.0985, -2.2286, 0.6377),
    (2.0776, 0.0795, -1.1350, 0.9033, -0.0636, -0.5514, 0.9082),
]


@pytest.mark.parametrize("impl", [delta_e2000, dcol.delta_e2000],
                         ids=["metrics", "dashboard"])
def test_ciede2000_hits_all_sharma_2005_vectors(impl):
    for L1, a1, b1, L2, a2, b2, expected in SHARMA_2005:
        got = impl((L1, a1, b1), (L2, a2, b2))
        # The published table is rounded to 4 decimals; 1e-4 is exact agreement.
        assert abs(got - expected) <= 1e-4, ((L1, a1, b1), (L2, a2, b2), got, expected)


def test_ciede2000_copies_are_bit_identical():
    # Deterministic pseudo-random Lab pairs (no numpy in the stdlib tier).
    import random
    rng = random.Random(20260705)
    for _ in range(2000):
        lab1 = (rng.uniform(0, 100), rng.uniform(-90, 90), rng.uniform(-90, 90))
        lab2 = (rng.uniform(0, 100), rng.uniform(-90, 90), rng.uniform(-90, 90))
        assert delta_e2000(lab1, lab2) == dcol.delta_e2000(lab1, lab2)


# ---------------------------------------------------------------------------
# CIE Lab f-curve — stdlib copies (metrics, dashboard) vs each other and colour.
# ---------------------------------------------------------------------------

WHITE_D65_100 = (95.047, 100.0, 108.883)


def test_lab_copies_bit_identical_including_negative_clamp():
    cases = [
        (50.0, 52.0, 49.0), (0.05, 0.06, 0.04), (95.0, 100.0, 108.0),
        (120.0, 100.0, 80.0),
        (-0.5, 0.3, -0.1),       # noisy dark read: negatives clamp to 0 (legit black)
        (0.0, 0.0, 0.0),
    ]
    for xyz in cases:
        assert xyz_to_lab(xyz, WHITE_D65_100) == dcol.xyz_to_lab(xyz, WHITE_D65_100)
    # The clamp maps a negative channel to the same Lab as a zero channel.
    assert xyz_to_lab((-1.0, 0.3, -2.0), WHITE_D65_100) == \
        xyz_to_lab((0.0, 0.3, 0.0), WHITE_D65_100)
    # Degenerate white channel → that ratio is defined to 0, not a ZeroDivisionError.
    assert all(math.isfinite(v) for v in xyz_to_lab((10.0, 10.0, 10.0), (95.0, 100.0, 0.0)))


def test_lab_matches_colour_science():
    np = pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    import dlc.engine.model  # noqa: F401  (sets colour's 'reference' domain-range scale)
    rng = np.random.default_rng(20260705)
    for _ in range(500):
        xyz = tuple(float(v) for v in rng.uniform(0.0, 120.0, 3))
        ours = np.asarray(xyz_to_lab(xyz, WHITE_D65_100))
        ref = colour.XYZ_to_Lab(np.asarray(xyz) / WHITE_D65_100[1],
                                illuminant=colour.XYZ_to_xy(np.asarray(WHITE_D65_100)))
        assert np.max(np.abs(ours - ref)) < 1e-10


def test_experimental_builders_lab_agrees_with_metrics():
    np = pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.engine import lut_constrained, physical
    from dlc.engine.lut_constrained import _lab as lab_lc
    from dlc.engine.model import Target, TargetSpace
    # One copy, not two (Phase 1 consolidation): physical re-imports the shared helpers.
    assert physical._metric_error is lut_constrained._metric_error
    assert physical._hue_chroma is lut_constrained._hue_chroma
    space = TargetSpace(Target.sdr_srgb_power(white_nits=100.0, white_xy=D65))
    white = tuple(float(c) for c in space.ideal_xyz(np.ones((1, 3)))[0])
    rng = np.random.default_rng(7)
    for _ in range(200):
        xyz = rng.uniform(0.0, 110.0, 3)
        ours = np.asarray(xyz_to_lab(tuple(float(v) for v in xyz), white))
        exp = lab_lc(space, xyz)
        assert np.max(np.abs(ours - exp)) < 1e-9


# ---------------------------------------------------------------------------
# dE_ITP (BT.2124) — 720 scale, Ct/2 applied EXACTLY once, engine vs dashboard.
# ---------------------------------------------------------------------------

def test_de_itp_unit_vectors_pin_scale_and_halving():
    np = pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.engine.model import de_itp
    # BT.2124: dE = 720·sqrt(ΔI² + (ΔCt/2)² + ΔCp²). Unit deltas make the halving
    # unmistakable: I → 720, Ct → 360 (halved ONCE), Cp → 720.
    assert float(de_itp(np.array([1.0, 0.0, 0.0]))) == pytest.approx(720.0)
    assert float(de_itp(np.array([0.0, 1.0, 0.0]))) == pytest.approx(360.0)
    assert float(de_itp(np.array([0.0, 0.0, 1.0]))) == pytest.approx(720.0)


def test_de_itp_matches_colour_reference_over_many_pairs():
    np = pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    from dlc.engine.model import de_itp
    rng = np.random.default_rng(11)
    xyz_a = rng.uniform(0.05, 900.0, (200, 3))
    xyz_b = xyz_a * rng.uniform(0.7, 1.3, (200, 3))
    ia, ib = colour.XYZ_to_ICtCp(xyz_a), colour.XYZ_to_ICtCp(xyz_b)
    ours = de_itp(ia - ib)
    ref = colour.delta_E(ia, ib, method="ITP")
    assert np.max(np.abs(ours - ref) / np.maximum(ref, 1e-12)) < 1e-9


def test_dashboard_itp_matches_engine_score_hdr_tightly():
    np = pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.engine.model import score_hdr
    sigs = [(0.5, 0.5, 0.5), (0.25, 0.25, 0.25), (0.75, 0.1, 0.1),
            (0.1, 0.1, 0.75), (0.62, 0.62, 0.62), (0.05, 0.05, 0.05)]
    ideal = score_hdr(sigs, [(1.0, 1.0, 1.0)] * len(sigs), white_xy=D65)["ideal_xyz"]
    meas = ideal * np.array([1.04, 0.97, 1.02])
    eng = score_hdr(sigs, meas, white_xy=D65)["de_itp"]
    for i, sig in enumerate(sigs):
        s = float(meas[i].sum())
        got = dcol.patch_delta_e(list(sig), float(meas[i][0]) / s, float(meas[i][1]) / s,
                                 float(meas[i][1]), is_hdr=True, white_xy=D65)
        # The stdlib ICtCp is the same math to float precision — hold it to 1e-6 JND
        # (measured agreement ~1e-11; the old 0.5-JND tolerance hid a lot of headroom).
        assert got == pytest.approx(float(eng[i]), abs=1e-6)


# ---------------------------------------------------------------------------
# ICtCp matrices — pin colour-science internals AND the dashboard's stdlib copies
# to the published Dolby/BT.2100 rationals, so a colour upgrade or a local edit
# that rotates the transform fails here.
# ---------------------------------------------------------------------------

DOLBY_RGB_TO_LMS = [[1688, 2146, 262], [683, 2951, 462], [99, 309, 3688]]
DOLBY_LMS_P_TO_ICTCP = [[2048, 2048, 0], [6610, -13613, 7003], [17933, -17390, -543]]


def test_dashboard_ictcp_matrices_are_the_published_rationals():
    for i in range(3):
        for j in range(3):
            assert dcol._M_BT2020_RGB_TO_LMS[i][j] == DOLBY_RGB_TO_LMS[i][j] / 4096
            assert dcol._M_LMS_P_TO_ICTCP[i][j] == DOLBY_LMS_P_TO_ICTCP[i][j] / 4096
    assert dcol._DE_ITP_SCALE == 720.0


def test_colour_science_ictcp_matrices_match_published_rationals():
    # engine.model builds its LMS cone from colour's INTERNAL name
    # MATRIX_ICTCP_RGB_TO_LMS — pin the values so a library upgrade that moves or
    # regenerates them cannot silently rotate the cone projection.
    np = pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    assert np.array_equal(colour.models.rgb.ictcp.MATRIX_ICTCP_RGB_TO_LMS,
                          np.asarray(DOLBY_RGB_TO_LMS) / 4096.0)
    assert np.array_equal(colour.models.rgb.ictcp.MATRIX_ICTCP_LMS_P_TO_ICTCP,
                          np.asarray(DOLBY_LMS_P_TO_ICTCP) / 4096.0)


def test_engine_cone_matrix_composition_and_dashboard_npm_agree_with_colour():
    np = pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    from dlc.engine.model import _ICTCP_XYZ_TO_LMS
    expect = (np.asarray(DOLBY_RGB_TO_LMS) / 4096.0) @ \
        colour.RGB_COLOURSPACES["ITU-R BT.2020"].matrix_XYZ_to_RGB
    assert np.allclose(_ICTCP_XYZ_TO_LMS, expect, atol=0, rtol=0)
    # dashboard's derived XYZ→BT.2020 inverse-NPM vs colour's own
    assert np.max(np.abs(np.asarray(dcol._XYZ_TO_BT2020)
                         - colour.RGB_COLOURSPACES["ITU-R BT.2020"].matrix_XYZ_to_RGB)) < 1e-12


def test_cone_projection_clamps_crafted_negative_lms_and_is_idempotent():
    np = pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.engine.model import _ICTCP_LMS_TO_XYZ, _ICTCP_XYZ_TO_LMS, _project_to_ictcp_cone
    # Craft XYZ rows whose cone responses are exactly the LMS we choose.
    lms = np.array([[-0.01, 0.4, 0.2],     # non-physical: negative L
                    [0.3, -1e-6, 0.1],     # barely negative M
                    [0.25, 0.5, 0.75]])    # physical: all non-negative
    xyz = lms @ _ICTCP_LMS_TO_XYZ.T
    projected = _project_to_ictcp_cone(xyz)
    lms_out = projected @ _ICTCP_XYZ_TO_LMS.T
    assert np.allclose(lms_out, np.maximum(lms, 0.0), atol=1e-9)
    # physical row untouched bit-for-bit; projection idempotent
    assert np.array_equal(projected[2], xyz[2])
    assert np.allclose(_project_to_ictcp_cone(projected), projected, atol=1e-12)


# ---------------------------------------------------------------------------
# sRGB / Rec.2020 NPM literals — one canonical copy + documented tolerance to
# the freshly-derived matrices.
# ---------------------------------------------------------------------------

def test_srgb_literal_is_the_single_canonical_object():
    from dlc import measure_loop, simulation
    assert simulation.SRGB_TO_XYZ_D65 is SRGB_TO_XYZ_D65
    assert measure_loop._SRGB_TO_XYZ_D65 is SRGB_TO_XYZ_D65


def test_npm_for_white_at_d65_matches_literal_to_documented_tolerance():
    # The literal is the classic (Lindbloom/IEC) sRGB matrix computed from rounded
    # D65; deriving from primaries + (0.3127, 0.3290) reproduces it to ~2.3e-4 —
    # that's the documented "~2e-4" in npm_for_white's docstring.
    npm = npm_for_white(D65)
    worst = max(abs(npm[i][j] - SRGB_TO_XYZ_D65[i][j]) for i in range(3) for j in range(3))
    assert worst < 3e-4
    # And RGB(1,1,1) maps exactly to the white at Y=1 (the construction's invariant).
    white_col = [sum(npm[i][j] for j in range(3)) for i in range(3)]
    assert white_col[1] == pytest.approx(1.0, abs=1e-12)
    x = white_col[0] / sum(white_col)
    y = white_col[1] / sum(white_col)
    assert x == pytest.approx(D65[0], abs=1e-9)
    assert y == pytest.approx(D65[1], abs=1e-9)


def test_npm_literals_match_colour_science():
    np = pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    from dlc.measure_loop import _REC2020_TO_XYZ_D65
    assert np.max(np.abs(np.asarray(SRGB_TO_XYZ_D65)
                         - colour.RGB_COLOURSPACES["sRGB"].matrix_RGB_to_XYZ)) < 3e-4
    assert np.max(np.abs(np.asarray(_REC2020_TO_XYZ_D65)
                         - colour.RGB_COLOURSPACES["ITU-R BT.2020"].matrix_RGB_to_XYZ)) < 5e-7


# ---------------------------------------------------------------------------
# Robertson (1968) CCT/Duv — table transcription + solver accuracy vs colour.
# ---------------------------------------------------------------------------

def test_robertson_table_transcription_is_exact():
    pytest.importorskip("colour")
    from colour.temperature.robertson1968 import DATA_ISOTEMPERATURE_LINES_ROBERTSON1968
    ref = [tuple(float(v) for v in row) for row in DATA_ISOTEMPERATURE_LINES_ROBERTSON1968]
    assert len(dcol._ROBERTSON) == len(ref) == 31
    for ours, theirs in zip(dcol._ROBERTSON, ref):
        assert ours == theirs


def test_cct_duv_matches_colour_robertson_solver_across_grid():
    pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from colour.temperature import CCT_to_uv_Robertson1968
    for cct in (2000, 2856, 4000, 5003, 6504, 9000, 15000, 25000):
        for duv in (-0.01, -0.005, 0.0, 0.005, 0.01):
            u, v = (float(c) for c in CCT_to_uv_Robertson1968([cct, duv]))
            x, y = dcol.uv60_to_xy(u, v)
            got = dcol.cct_duv(x, y)
            assert got is not None, (cct, duv)
            got_cct, got_duv = got
            assert abs(got_cct - cct) / cct < 2e-4, (cct, duv, got)
            assert abs(got_duv - duv) < 5e-6, (cct, duv, got)


# ---------------------------------------------------------------------------
# colormath.invert3x3 + metrics.percentile — fuzz against numpy.
# ---------------------------------------------------------------------------

def test_invert3x3_fuzz_against_numpy():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(20260705)
    for _ in range(2000):
        m = rng.uniform(-5.0, 5.0, (3, 3))
        ours = np.asarray(invert3x3(m.tolist()))
        ref = np.linalg.inv(m)
        assert np.max(np.abs(ours - ref)) / max(1.0, float(np.max(np.abs(ref)))) < 1e-9


def test_invert3x3_singular_raises_and_det_guard_is_absolute():
    with pytest.raises(ValueError):
        invert3x3([[1, 2, 3], [2, 4, 6], [0, 1, 1]])
    # KNOWN LIMITATION (documented, accepted): the det guard is ABSOLUTE (1e-12), so a
    # tiny-scaled but perfectly conditioned matrix is rejected. Every in-repo caller
    # inverts NPM-scale matrices (entries O(1), det O(0.01)) so this is unreachable in
    # production; this pin exists so a future caller with tiny-scale input finds out here.
    with pytest.raises(ValueError):
        invert3x3([[1e-5, 0, 0], [0, 1e-5, 0], [0, 0, 1e-5]])
    # NPM-scale inputs are far from the guard: this must never start raising.
    npm = rgb_to_xyz_matrix(0.64, 0.33, 0.30, 0.60, 0.15, 0.06, *D65)
    inv = invert3x3(npm)
    assert all(math.isfinite(v) for row in inv for v in row)


def test_percentile_matches_numpy_linear_interpolation():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(3)
    for n in (1, 2, 3, 10, 97):
        vals = [float(v) for v in rng.uniform(0.0, 10.0, n)]
        for p in (0, 5, 50, 95, 99, 100):
            assert percentile(vals, p) == pytest.approx(float(np.percentile(vals, p)), abs=1e-12)


# ---------------------------------------------------------------------------
# Parity P-row: both metric stacks sanitize non-finite/negative reads identically
# (metrics._finite_nonneg_xyz for SDR; score_hdr's nan_to_num+clip for HDR).
# ---------------------------------------------------------------------------

SANITIZE_CASES = [
    (float("nan"), 1.0, 2.0),
    (float("inf"), -3.0, 0.5),
    (-1.0, float("-inf"), 2.0),
    (float("nan"), float("inf"), float("-inf")),
    (1.0, 2.0, 3.0),
    (0.0, -0.0, -1e-9),
]


def test_sdr_sanitizer_semantics():
    for case in SANITIZE_CASES:
        got = _finite_nonneg_xyz(case)
        for raw, out in zip(case, got):
            if not math.isfinite(raw):
                assert out == 0.0
            else:
                assert out == max(raw, 0.0)


def test_hdr_sanitizer_matches_sdr_sanitizer_exactly():
    np = pytest.importorskip("numpy")
    for case in SANITIZE_CASES:
        spine = np.asarray(_finite_nonneg_xyz(case))
        engine = np.maximum(np.nan_to_num(np.asarray(case, dtype=float),
                                          nan=0.0, posinf=0.0, neginf=0.0), 0.0)
        assert np.array_equal(spine, engine), case


def test_patch_metric_artifacts_stay_strict_json_with_nan_reads():
    # A NaN/inf meter read scores a finite dE (sanitized) but the RAW measured_xyz is
    # kept as evidence. json.dumps would emit bare `NaN` tokens — Python re-parses them,
    # a browser's JSON.parse throws. The artifact writer must map non-finite → null.
    import json
    from dlc.metrics import PatchMetric, _strict_json_patch_rows
    rows = _strict_json_patch_rows([
        PatchMetric(rgb=(1.0, 1.0, 1.0), measured_xyz=(float("nan"), float("inf"), 5.0),
                    target_xyz=(95.0, 100.0, 108.0), de2000=42.0, grayscale=True),
        PatchMetric(rgb=(0.5, 0.5, 0.5), measured_xyz=(20.0, 21.0, 22.0),
                    target_xyz=(19.0, 20.0, 21.0), de2000=1.0, grayscale=True),
    ])
    text = json.dumps(rows, allow_nan=False)      # strict mode must not raise
    parsed = json.loads(text)
    assert parsed[0]["measured_xyz"] == [None, None, 5.0]
    assert parsed[1]["measured_xyz"] == [20.0, 21.0, 22.0]   # finite reads untouched
    assert parsed[0]["de2000"] == 42.0
