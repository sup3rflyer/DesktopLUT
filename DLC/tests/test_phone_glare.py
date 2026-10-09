"""Offline tests for dlc.phone.glare: specks on a dim field convolved with a known a * d^-p kernel must give back a
and p within a few %; subtracting veil + planar floor must restore the scene, and the per-block veil fractions must
say where the veil dominates."""

from __future__ import annotations

import math

import numpy as np
import pytest

from dlc.phone import glare as gl

TRUE = gl.GlareKernel(0.051, 2.93, r_core=3.0)          # the 10-08 law, near field from 3 px


def _specks(n=240, field=0.05):
    scene = np.full((n, n), field)
    cents = []
    for i, y in enumerate((40, 120, 200)):
        for j, x in enumerate((40, 120, 200)):
            s, L = (3, 5, 7)[j], (300.0, 600.0, 1000.0)[i]
            y0, x0 = y - s // 2, x - s // 2
            scene[y0:y0 + s, x0:x0 + s] = L
            cents.append((x0 + (s - 1) / 2.0, y0 + (s - 1) / 2.0))
    return scene, np.asarray(cents)


@pytest.fixture(scope="module")
def specks():
    scene, cents = _specks()
    rng = np.random.default_rng(2)
    meas = scene + TRUE.veil(scene) + rng.normal(0.0, 5e-4, scene.shape)
    return scene, cents, meas


def test_fit_recovers_kernel(specks):
    scene, cents, meas = specks
    f = gl.fit_glare_kernel(meas, cents, flux_radius=8.0, r_fit=(12.0, 34.0), n_rings=7, r_core=3.0)
    assert f.kernel.a == pytest.approx(0.051, rel=0.03)
    assert f.kernel.p == pytest.approx(2.93, rel=0.02)
    assert f.flags == [] and f.n_specks == 9 and f.ring_rms < 2e-3
    assert np.isfinite(f.p_se) and f.p_se < 0.05 and np.isfinite(f.a_se_rel)
    for s in f.specks:
        assert s["field"] == pytest.approx(0.05, abs=0.003)          # the dim field, not its veil
        assert s["used"] and s["flags"] == [] and len(s["ring"]) == 7
    flux_true = sorted(float(v) for v in [(s * s) * (L - 0.05) for L in (300.0, 600.0, 1000.0) for s in (3, 5, 7)])
    assert np.allclose(sorted(s["flux"] for s in f.specks), flux_true, rtol=0.02)
    sm = f.summary()
    assert sm["fraction_beyond_r_core"] == pytest.approx(f.kernel.fraction(), rel=1e-9)


def test_fit_with_clipped_halo_frame_and_unclipped_flux_frame(specks):
    """The 10-08 recipe: halos from a long exposure whose specks clip, flux from a short one in the same units."""
    scene, cents, meas = specks
    clipped = np.minimum(meas, 40.0)
    f = gl.fit_glare_kernel(clipped, cents, flux_img=meas, valid=clipped < 40.0, flux_radius=8.0,
                            r_fit=(12.0, 34.0), n_rings=7, r_core=3.0, iterations=2)
    assert f.kernel.a == pytest.approx(0.051, rel=0.03) and f.kernel.p == pytest.approx(2.93, rel=0.02)
    # without the flux frame the clipped cores understate the source -> visibly wrong (and the residuals say so)
    bad = gl.fit_glare_kernel(clipped, cents, flux_radius=8.0, r_fit=(12.0, 34.0), n_rings=7, r_core=3.0,
                              iterations=1)
    assert abs(bad.kernel.a / 0.051 - 1) > 0.1 or abs(bad.kernel.p / 2.93 - 1) > 0.05


def test_subtract_veil_restores_scene_and_reports_blocks():
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]
    rng = np.random.default_rng(4)
    scene = np.zeros((n, n))
    disc = np.hypot(xx - 80, yy - 90) < 30
    scene[disc] = 500.0
    scene[170:230, 150:230] = 5.0 + 10.0 * rng.random((60, 80))      # dim content far from the disc
    xn, yn = (xx - (n - 1) / 2) / ((n - 1) / 2), (yy - (n - 1) / 2) / ((n - 1) / 2)
    floor = 0.1 + 0.02 * xn - 0.01 * yn
    k8 = gl.GlareKernel(0.051, 2.93, r_core=8.0)
    meas = scene + k8.veil(scene) + floor
    black = (np.hypot(xx - 80, yy - 90) > 70) & ~((yy > 150) & (xx > 130))
    r = gl.subtract_veil(meas, k8, floor_mask=black, iterations=4, block=32)
    assert r.flags == [] and r.converged < 1e-3
    assert np.allclose(r.floor_coef, [0.1, 0.02, -0.01], atol=1e-3)
    dim = (scene > 0) & (scene < 20)
    assert np.max(np.abs(r.corrected - scene)[dim]) < 2e-3            # the dim content is back
    assert np.max(np.abs(r.corrected - scene)[disc]) / 500.0 < 1e-4
    raw = (meas - scene)[dim].mean()
    assert raw > 0.3                                                  # ... from a ~0.5 veil + floor
    # blocks: next to the disc the veil is a big share, in the far dim patch a small one
    assert r.block_veil_fraction.shape == (8, 8)
    near, far = r.block_veil_fraction[3, 4], r.block_veil_fraction[6, 6]
    assert near > 0.5 and far < 0.10
    rob = r.robust(0.10)
    assert rob[6, 6] and not rob[3, 4] and not rob[0, 0]
    s = r.summary()
    assert s["n_blocks"] == 64 and 0 < s["robust_10pct_share"] < 1
    # a fixed known source is used as given (one pass); a known floor too
    r2 = gl.subtract_veil(meas, k8, source=scene, floor=floor)
    assert np.allclose(r2.corrected, scene, atol=1e-9) and r2.iterations == 1
    # clipped pixels in the default source are counted
    v = meas < 400.0
    assert any(f.startswith("invalid_pixels_in_source") for f in gl.subtract_veil(meas, k8, valid=v).flags)


def test_kernel_fraction_grid_and_dict():
    k = gl.GlareKernel(0.051, 2.93, r_core=8.0)
    G = k.grid(400, 400)
    yy, xx = np.mgrid[-400:401, -400:401]
    num = G[np.hypot(xx, yy) <= 400].sum()
    assert num == pytest.approx(k.fraction(8.0, 400.0), rel=0.05)
    assert G[400, 400] == 0.0 and k.k(5.0) == 0.0 and k.k(10.0) == pytest.approx(0.051 * 10 ** -2.93)
    assert k.fraction(1.0) == pytest.approx(2 * math.pi * 0.051 / 0.93, rel=1e-9)     # ~35 %: why r_core
    assert 0.04 < k.fraction() < 0.06
    k2 = gl.GlareKernel.from_dict(k.to_dict())
    assert (k2.a, k2.p, k2.r_core) == (k.a, k.p, k.r_core)
    legacy = gl.GlareKernel.from_dict({"A": 0.051, "p": 2.93, "p_se": 0.02, "ring_rms_nit": 0.0075})
    assert legacy.a == 0.051 and legacy.meta["legacy_dmin"] == 1.0
    with pytest.raises(ValueError):
        gl.GlareKernel(0.05, 1.9)
    img = np.zeros((50, 60))
    img[25, 30] = 1.0
    v = k.veil(img)
    assert abs(v[25, 30]) < 1e-12 and v[25, 40] == pytest.approx(k.k(10.0)) and abs(v[25, 35]) < 1e-12
    assert abs(k.veil(img, max_radius=12.0)[25, 45]) < 1e-12
