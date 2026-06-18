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
