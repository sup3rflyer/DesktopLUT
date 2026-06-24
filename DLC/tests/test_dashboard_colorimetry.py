"""The dashboard's dependency-free CCT/Duv readout (Robertson)."""

from __future__ import annotations

import math

from dlc.dashboard.colorimetry import (
    cct_duv, neutral_metrics, planckian_locus_xy, uv60_to_xy, xy_to_uv60,
)


def test_d65_lands_on_textbook_cct_and_duv():
    cct, duv = cct_duv(0.31271, 0.32902)
    assert abs(cct - 6504) < 30          # textbook D65 ≈ 6504 K
    assert abs(duv - 0.0032) < 0.0008    # D65 sits just above the locus


def test_d50_lands_near_5000k():
    cct, duv = cct_duv(0.34567, 0.35850)
    assert abs(cct - 5003) < 40
    assert abs(duv) < 0.005


def test_warm_white_reads_lower_temperature():
    warm, _ = cct_duv(0.4369, 0.4041)     # ~3000 K Planckian
    cool, _ = cct_duv(0.31271, 0.32902)   # D65
    assert warm < cool
    assert abs(warm - 3000) < 60


def test_green_tint_gives_positive_duv():
    # A point pushed up in y off the locus reads green (Duv > 0).
    _, duv = cct_duv(0.30, 0.36)
    assert duv > 0.01


def test_black_and_degenerate_return_none():
    assert cct_duv(0.0, 0.0) is None
    assert xy_to_uv60(0.0, 0.0) is not None  # (0,0) denom is 3, not degenerate
    # A chromaticity off the table's locus span returns None rather than a bogus CCT.
    assert cct_duv(0.7, 0.05) is None


def test_far_off_locus_returns_none_not_bogus_cct():
    # A point that brackets the locus but sits implausibly far from it (|Duv| huge) must
    # return None — a coloured patch mislabeled neutral should read as a dash, not a
    # confident junk temperature (it used to report ~39000 K / Duv 0.069).
    assert cct_duv(0.18, 0.30) is None
    # A real near-neutral white (small Duv) still resolves.
    assert cct_duv(0.3127, 0.329) is not None


def test_uv60_matches_definition():
    uv = xy_to_uv60(0.31271, 0.32902)
    assert uv is not None
    u, v = uv
    denom = -2 * 0.31271 + 12 * 0.32902 + 3
    assert math.isclose(u, 4 * 0.31271 / denom, rel_tol=1e-9)
    assert math.isclose(v, 6 * 0.32902 / denom, rel_tol=1e-9)


def test_uv60_xy_roundtrip():
    # xy → uv60 → xy must return the original chromaticity (the locus needs the inverse).
    for x, y in [(0.3127, 0.329), (0.45, 0.40), (0.20, 0.10)]:
        u, v = xy_to_uv60(x, y)
        rx, ry = uv60_to_xy(u, v)
        assert abs(rx - x) < 1e-6 and abs(ry - y) < 1e-6


def test_planckian_locus_is_a_sane_warm_to_cool_curve():
    locus = planckian_locus_xy()
    assert len(locus) == 31
    # all points are valid chromaticities in the locus region
    for x, y in locus:
        assert 0.2 < x < 0.7 and 0.2 < y < 0.45
    # ordered warm→cool: the first point (low temp) is redder (higher x) than the last
    assert locus[0][0] > locus[-1][0]


def test_neutral_metrics_shape_is_stable():
    ok = neutral_metrics(0.31271, 0.32902)
    assert set(ok) == {"cct", "duv"}
    assert ok["cct"] is not None
    bad = neutral_metrics(0.0, 0.0)
    assert bad == {"cct": None, "duv": None}


# ---------------------------------------------------------------------------
# Per-patch ΔE must MATCH the spine's scorer (the dashboard monitor is only useful
# if its numbers agree with the authoritative metrics_scored). Cross-validate the
# dependency-free dashboard ΔE against dlc.metrics (SDR) and the engine (HDR).
# ---------------------------------------------------------------------------

import pytest

from dlc.dashboard.colorimetry import patch_delta_e

_D65 = (0.3127, 0.3290)
_SIGS = [(1.0, 1.0, 1.0), (0.5, 0.5, 0.5), (0.25, 0.25, 0.25),
         (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
         (0.75, 0.3, 0.3), (0.6, 0.6, 0.2), (0.2, 0.5, 0.8)]


def _xy(xyz):
    s = sum(xyz)
    return (xyz[0] / s, xyz[1] / s) if s > 0 else (0.0, 0.0)


def test_patch_delta_e_sdr_matches_metrics_ciede2000():
    # Build a tinted-measured sample set, score it with the spine's SDR scorer, and confirm the
    # dashboard's per-patch CIEDE2000 reproduces each value (identical formula → near bit-exact).
    from dlc.mhc import Ti3Sample
    from dlc.metrics import score_samples, target_xyz_for_rgb, npm_for_white
    L, gamma = 120.0, 2.2
    npm = npm_for_white(_D65)
    samples = []
    for i, sig in enumerate(_SIGS):
        ideal = target_xyz_for_rgb(sig, L, gamma, npm)
        gain = 1.0 + 0.06 * math.sin(i)                 # a per-patch tint so dE is nonzero + varied
        meas = (ideal[0] * gain, ideal[1] * (1.0 / gain), ideal[2] * (1.0 + 0.03 * math.cos(i)))
        samples.append(Ti3Sample(rgb=sig, xyz=meas))
    metrics, _lum = score_samples(samples, luminance=L, gamma=gamma, white_xy=_D65)
    for m in metrics:
        x, y = _xy(m.measured_xyz)
        got = patch_delta_e(m.rgb, x, y, m.measured_xyz[1], is_hdr=False,
                            white_xy=_D65, luminance=L, gamma=gamma)
        assert got == pytest.approx(m.de2000, abs=1e-4), (m.rgb, got, m.de2000)


def test_patch_delta_e_hdr_matches_engine_de_itp():
    pytest.importorskip("numpy")
    pytest.importorskip("colour")
    from dlc.mhc import Ti3Sample
    from dlc.metrics import score_samples_hdr
    from dlc.engine.model import score_hdr
    sigs = [s for s in _SIGS]
    ideal = score_hdr(sigs, [(1.0, 1.0, 1.0)] * len(sigs), white_xy=_D65)["ideal_xyz"]
    samples = []
    for i, sig in enumerate(sigs):
        ix, iy, iz = (float(c) for c in ideal[i])
        gain = 1.0 + 0.05 * math.sin(i + 1)
        meas = (ix * gain, iy * (1.0 / gain), iz * (1.0 + 0.04 * math.cos(i)))
        samples.append(Ti3Sample(rgb=sig, xyz=meas))
    metrics, _ = score_samples_hdr(samples, white_xy=_D65, peak_nits=1000.0)
    for m in metrics:
        x, y = _xy(m.measured_xyz)
        got = patch_delta_e(m.rgb, x, y, m.measured_xyz[1], is_hdr=True, white_xy=_D65)
        # dependency-free ICtCp vs colour.XYZ_to_ICtCp: agree to well under 1 JND.
        assert got == pytest.approx(m.de2000, abs=0.5), (m.rgb, got, m.de2000)


def test_jzazbz_matches_colour_library():
    # The hand-rolled, dependency-free Jzazbz must reproduce colour.XYZ_to_Jzazbz (the project's
    # source of truth) — same role as the ICtCp cross-check, so ΔEz is trustworthy.
    np = pytest.importorskip("numpy")
    colour = pytest.importorskip("colour")
    from dlc.dashboard.colorimetry import _xyz_to_jzazbz
    for XYZ in ([95.047, 100.0, 108.883], [950.47, 1000.0, 1088.83],
                [200.0, 150.0, 80.0], [10.0, 12.0, 30.0], [0.0, 0.0, 0.0]):
        mine = np.array(_xyz_to_jzazbz(XYZ))
        theirs = colour.XYZ_to_Jzazbz(np.array(XYZ))
        assert np.allclose(mine, theirs, atol=1e-9), (XYZ, mine, theirs)


def test_patch_deltas_structure_and_scoring_metric():
    # HDR offers the scoring dE_ITP plus the dE2000 + Jzazbz lenses; SDR offers dE2000 (scoring)
    # + Jzazbz. The scoring scalar must equal the legacy patch_delta_e (back-compat shim).
    from dlc.dashboard.colorimetry import patch_deltas, patch_delta_e
    hdr = patch_deltas([0.0, 0.0, 0.6], 0.18, 0.10, 120.0, is_hdr=True, white_xy=_D65)
    assert hdr["scoring"] == "itp"
    assert set(hdr["metrics"]) == {"itp", "de2000", "jzazbz"}
    assert hdr["metrics"]["itp"]["de"] == pytest.approx(
        patch_delta_e([0.0, 0.0, 0.6], 0.18, 0.10, 120.0, is_hdr=True, white_xy=_D65))
    # the target's xyY rides along so the dashboard can show measured-vs-target without recomputing
    assert set(hdr["target"]) == {"x", "y", "Y"} and hdr["target"]["Y"] > 0
    sdr = patch_deltas([0.5, 0.5, 0.55], 0.30, 0.31, 60.0, is_hdr=False, white_xy=_D65, luminance=120.0)
    assert sdr["scoring"] == "de2000"
    assert set(sdr["metrics"]) == {"de2000", "jzazbz"}
    # a true mid-grey target sits ON the white point and at (0.5^gamma)·luminance
    grey = patch_deltas([0.5, 0.5, 0.5], 0.3127, 0.329, 30.0, is_hdr=False, white_xy=_D65, luminance=120.0)
    assert abs(grey["target"]["x"] - _D65[0]) < 1e-4 and abs(grey["target"]["y"] - _D65[1]) < 1e-4
    assert grey["target"]["Y"] == pytest.approx(120.0 * 0.5 ** 2.2, rel=0.02)
    assert sdr["metrics"]["de2000"]["de"] == pytest.approx(
        patch_delta_e([0.5, 0.5, 0.55], 0.30, 0.31, 60.0, is_hdr=False, white_xy=_D65, luminance=120.0))


def test_jzazbz_is_jnd_scaled_not_raw():
    # ΔEz is normalised (×_JZ_SCALE) to the 1≈JND scale so it shares the dashboard's severity bands
    # and the user's learned intuition — a clearly-visible error must read in the same order as
    # dE_ITP, NOT on the raw ~1e-3 Jzazbz scale.
    from dlc.dashboard.colorimetry import patch_deltas, _JZ_SCALE
    assert _JZ_SCALE == 660.0
    d = patch_deltas([0.0, 0.0, 0.9], 0.15, 0.06, 60.0, is_hdr=True, white_xy=_D65)
    jz, itp = d["metrics"]["jzazbz"]["de"], d["metrics"]["itp"]["de"]
    assert jz > 1.0                        # not the sub-0.05 raw scale
    assert 0.2 < jz / itp < 5.0            # same ballpark as ITP (both JND-anchored)


def test_euclidean_metric_components_reconstruct_scalar_exactly():
    # ITP and Jzazbz are Euclidean: the radial/tangential chroma/hue split is an orthonormal
    # change of basis, so L²+C²+H² == ΔE exactly even for a saturated patch. This is the whole
    # point of the decomposition — "where is the error" must add back up to "how big is the error".
    from dlc.dashboard.colorimetry import patch_deltas
    d = patch_deltas([0.1, 0.2, 0.7], 0.17, 0.12, 90.0, white_xy=_D65, is_hdr=True)
    for name in ("itp", "jzazbz"):
        m = d["metrics"][name]
        quad = math.sqrt(m["L"] ** 2 + m["C"] ** 2 + m["H"] ** 2)
        assert quad == pytest.approx(m["de"], rel=1e-9), (name, quad, m["de"])


def test_de2000_components_reconstruct_scalar_near_neutral():
    # CIEDE2000's ΔL*/ΔC*/ΔH* terms only sum in quadrature when the RT rotation cross-term is
    # negligible — i.e. at low chroma. Near neutral the three terms must reconstruct the scalar.
    from dlc.dashboard.colorimetry import patch_deltas
    d = patch_deltas([0.5, 0.5, 0.52], 0.305, 0.318, 60.0, white_xy=_D65, is_hdr=False, luminance=120.0)
    m = d["metrics"]["de2000"]
    quad = math.sqrt(m["L"] ** 2 + m["C"] ** 2 + m["H"] ** 2)
    assert quad == pytest.approx(m["de"], rel=0.02), (quad, m["de"])


def test_rgb_balance_neutral_is_zero_and_tints_read_correctly():
    from dlc.dashboard.colorimetry import rgb_balance
    # A gray sitting exactly on the target white reads 0/0/0 at any luminance (pure white balance).
    for Y in (5.0, 80.0, 600.0):
        r, g, b = rgb_balance(_D65[0], _D65[1], Y, is_hdr=False, white_xy=_D65)
        assert abs(r) < 1e-6 and abs(g) < 1e-6 and abs(b) < 1e-6
    # A gray pushed toward blue (lower x, slightly) lifts the blue channel above the others.
    rb, gb, bb = rgb_balance(0.300, 0.318, 80.0, is_hdr=False, white_xy=_D65)
    assert bb > 0 and bb > rb
    # Degenerate / near-black → None (balance is noise there), never a bogus number.
    assert rgb_balance(0.31, 0.0, 80.0, is_hdr=False, white_xy=_D65) is None
    assert rgb_balance(0.31, 0.33, 0.0, is_hdr=False, white_xy=_D65) is None


def test_patch_delta_e_rejects_degenerate_and_nonfinite():
    # A degenerate (y<=0) or NaN/inf read must return None — never a plausible finite ΔE that
    # would silently poison the live rolling average (the engine sanitizes the same case).
    assert patch_delta_e([1, 1, 1], 0.31, 0.0, 100.0, is_hdr=False, white_xy=_D65, luminance=120) is None
    assert patch_delta_e([1, 1, 1], float("nan"), 0.33, 100.0, is_hdr=True, white_xy=_D65) is None
    assert patch_delta_e([1, 1, 1], 0.31, 0.33, float("inf"), is_hdr=True, white_xy=_D65) is None
    assert patch_delta_e([1, 1, 1], 0.31, -0.1, 100.0, is_hdr=True, white_xy=_D65) is None
    # a valid read still scores
    assert patch_delta_e([1, 1, 1], 0.3127, 0.329, 120.0, is_hdr=False, white_xy=_D65, luminance=120) is not None
