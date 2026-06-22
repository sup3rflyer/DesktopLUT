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
