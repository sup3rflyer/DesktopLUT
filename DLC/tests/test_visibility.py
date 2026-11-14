"""dlc.visibility pinned at absolute levels (0.3, 10, 100, 1000 nit): chroma_itp against values computed with the
``colour`` package (0.4.6: XYZ_to_ICtCp "ITU-R BT.2100-2 PQ" on absolute cd/m2 + delta_E_ITP), the reviewer's
method for gswb-hw-fixes (fixes-2026-11-14/gswb_hw_fixes/review/itp_ref.py), to 4 decimals. When ``colour`` is
installed the reference is also recomputed live."""

from __future__ import annotations

import math

import pytest

from dlc.colormath import xy_to_XYZ
from dlc.visibility import chroma_itp, light_itp

W = (0.3127, 0.3290)
SHIFTS = ((0.001, 0.0), (0.002, 0.0), (0.0, 0.002), (0.002, 0.002))
# colour 0.4.6 reference, dE_ITP of (W + shift) against W at the level (generated with colour, 2026-11-14)
REF = {
    0.3: (0.2422, 0.4843, 0.3331, 0.4207),
    10.0: (0.5137, 1.0273, 0.7068, 0.8931),
    100.0: (0.6539, 1.3076, 0.9, 1.1373),
    1000.0: (0.7102, 1.4203, 0.9777, 1.2357),
}


@pytest.mark.parametrize("level", sorted(REF))
def test_chroma_itp_pinned_at_absolute_levels(level):
    for (dx, dy), ref in zip(SHIFTS, REF[level]):
        a = xy_to_XYZ(W[0] + dx, W[1] + dy, level)
        b = xy_to_XYZ(W[0], W[1], level)
        assert chroma_itp(a, b) == pytest.approx(ref, abs=1e-4), (level, dx, dy)


@pytest.mark.parametrize("level", sorted(REF))
def test_chroma_itp_matches_the_colour_package_live(level):
    colour = pytest.importorskip("colour")
    import numpy as np
    for dx, dy in SHIFTS:
        a = xy_to_XYZ(W[0] + dx, W[1] + dy, level)
        b = xy_to_XYZ(W[0], W[1], level)
        ia = colour.XYZ_to_ICtCp(np.array(a, float), method="ITU-R BT.2100-2 PQ")
        ib = colour.XYZ_to_ICtCp(np.array(b, float), method="ITU-R BT.2100-2 PQ")
        assert chroma_itp(a, b) == pytest.approx(float(colour.difference.delta_E_ITP(ia, ib)), abs=1e-4)


def test_chroma_itp_is_chromaticity_only_at_the_first_stimulus_light():
    a = xy_to_XYZ(W[0] + 0.002, W[1], 10.0)
    b = xy_to_XYZ(W[0], W[1], 37.0)                       # another light: brought to a's
    assert chroma_itp(a, b) == pytest.approx(chroma_itp(a, xy_to_XYZ(W[0], W[1], 10.0)), abs=1e-9)
    assert math.isnan(chroma_itp([0.0, 0.0, 0.0], b))


def test_light_itp_is_one_at_the_documented_visibility():
    # docs/practical-thresholds.md: a grey's light change is just visible from 1.39 % at 100 nit
    assert light_itp(xy_to_XYZ(W[0], W[1], 100.0), math.log(1.0139)) == pytest.approx(1.0, rel=0.03)
    assert light_itp(xy_to_XYZ(W[0], W[1], 100.0), 0.0) == pytest.approx(0.0, abs=1e-12)
