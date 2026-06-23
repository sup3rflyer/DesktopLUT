"""Tests for the dependency-free chromaticity-gamut geometry (``dlc.gamut``)."""

from __future__ import annotations

import pytest

from dlc import gamut


# --------------------------------------------------------------------------- lookup
def test_target_primaries_aliases():
    for name in ("Rec.709", "sRGB", "ITU-R BT.709", "bt.709", "709"):
        p = gamut.target_primaries(name)
        assert p is not None and p["R"] == (0.640, 0.330)
    for name in ("Rec.2020", "ITU-R BT.2020", "rec2020"):
        assert gamut.target_primaries(name)["G"] == (0.170, 0.797)
    for name in ("Display P3", "DCI-P3", "p3-d65"):
        assert gamut.target_primaries(name)["R"] == (0.680, 0.320)
    assert gamut.target_primaries("nonsense") is None
    assert gamut.target_primaries(None) is None


# --------------------------------------------------------------------------- geometry
def test_point_in_triangle_and_boundary():
    a, b, c = (0.0, 0.0), (1.0, 0.0), (0.0, 1.0)
    assert gamut.point_in_triangle((0.25, 0.25), a, b, c)
    assert not gamut.point_in_triangle((1.0, 1.0), a, b, c)
    # a vertex / edge point counts as inside (within eps) — winding-agnostic
    assert gamut.point_in_triangle((0.0, 0.0), a, b, c)
    assert gamut.point_in_triangle((0.5, 0.5), a, b, c)        # on the hypotenuse


def test_polygon_area_and_clip():
    unit = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert gamut.polygon_area(unit) == pytest.approx(1.0)
    # clip the unit square by a half-size square ⇒ area 0.25
    half = [(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)]
    assert gamut.polygon_area(gamut.clip_convex(unit, half)) == pytest.approx(0.25)
    assert gamut.polygon_area([(0.0, 0.0), (1.0, 1.0)]) == 0.0   # degenerate


# --------------------------------------------------------------------------- coverage
def test_wide_native_fully_covers_smaller_target():
    # A P3 (wide-gamut) panel covers a Rec.709 target: every 709 primary is inside P3.
    native = gamut.STANDARD_PRIMARIES["Display P3"]
    target = gamut.STANDARD_PRIMARIES["Rec.709"]
    cov = gamut.gamut_coverage(native, target)
    assert cov["covered"] is True
    assert cov["coverage_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert cov["shortfall"] == {}


def test_narrow_native_undercovers_wider_target():
    # A Rec.709 panel cannot cover a Rec.2020 target: all three 2020 primaries fall outside.
    native = gamut.STANDARD_PRIMARIES["Rec.709"]
    target = gamut.STANDARD_PRIMARIES["Rec.2020"]
    cov = gamut.gamut_coverage(native, target)
    assert cov["covered"] is False
    assert 0.5 < cov["coverage_ratio"] < 0.85          # 709 area / 2020 area
    assert set(cov["shortfall"]) == {"R", "G", "B"}     # each unreachable, with a distance
    assert all(d > 0 for d in cov["shortfall"].values())


def test_reachable_fraction_caps_an_unreachable_primary():
    # Panel ~P3 gamut; target Rec.2020 blue sits OUTSIDE it → the white→blue line exits the
    # native triangle partway, so the reachable fraction is < 1. A reachable target → 1.0.
    nat = [(0.6927, 0.3028), (0.1825, 0.7502), (0.1521, 0.0646)]  # measured PA32UCXR
    white = (0.3127, 0.329)
    f_out = gamut.reachable_fraction(white, (0.131, 0.046), nat)   # Rec.2020 blue (outside)
    assert 0.0 < f_out < 1.0
    f_in = gamut.reachable_fraction(white, (0.18, 0.40), nat)      # a point well inside
    assert f_in == pytest.approx(1.0)


def test_identical_gamut_is_full_coverage():
    p = gamut.STANDARD_PRIMARIES["Rec.709"]
    cov = gamut.gamut_coverage(p, p)
    assert cov["covered"] is True
    assert cov["coverage_ratio"] == pytest.approx(1.0, abs=1e-3)


def test_partial_coverage_some_primaries_reachable():
    # Native covers R/B (= 709) but a GREEN that is LESS saturated than the target's green ⇒
    # the target green falls outside ⇒ partial coverage, only G unreachable.
    native = {"R": (0.640, 0.330), "G": (0.300, 0.600), "B": (0.150, 0.060)}
    target = {"R": (0.620, 0.340), "G": (0.270, 0.680), "B": (0.150, 0.060)}
    cov = gamut.gamut_coverage(native, target)
    assert cov["reachable"]["R"] is True and cov["reachable"]["B"] is True
    assert cov["reachable"]["G"] is False
    assert set(cov["shortfall"]) == {"G"}
    assert 0.8 < cov["coverage_ratio"] < 1.0
