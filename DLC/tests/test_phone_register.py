"""Offline tests for dlc.phone.register: a textured "encoded frame" warped into a synthetic camera image by a known
homography (+ camera gain, vignetting, optical blur and noise) must be recovered to well under half a pixel; a
low-texture segment must fall back to its seed and say so; a series fills it by time interpolation."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from dlc.phone import register as rg
from dlc.phone.analysis import apply_h

SW, SH = 480, 270
CAM = (240, 420)


def _screen(seed=5):
    from scipy.ndimage import gaussian_filter
    rng = np.random.default_rng(seed)
    s = gaussian_filter(rng.normal(size=(SH, SW)), 3.0)
    s = 0.05 + (s - s.min()) / np.ptp(s)
    s[60:100, 200:260] += 0.8
    return s


def _H(dx=30.0, dy=15.0, rot_deg=1.5, scale=0.8):
    th = np.deg2rad(rot_deg)
    return np.array([[scale * np.cos(th), -scale * np.sin(th), dx], [scale * np.sin(th), scale * np.cos(th), dy],
                     [2e-5, -1e-5, 1.0]])


def _camera(screen, H, seed=7, gain=0.6):
    """What the phone sees: gain, radial vignetting, lens blur, shot-ish + read noise; black off the screen."""
    from scipy.ndimage import gaussian_filter
    rng = np.random.default_rng(seed)
    img = rg.render_screen(screen, H, CAM)
    yy, xx = np.mgrid[0:CAM[0], 0:CAM[1]]
    vig = 1.0 - 0.175 * (((xx - 210) / 210.0) ** 2 + ((yy - 120) / 120.0) ** 2)
    img = gaussian_filter(gain * np.nan_to_num(img, nan=0.002) * vig, 0.8)
    img = img * (1 + 0.01 * rng.normal(size=CAM)) + 0.002 * rng.normal(size=CAM)
    return np.maximum(img, 1e-4)


def _map_err(Ha, Hb):
    g = np.mgrid[20:SW - 20:20, 20:SH - 20:20].reshape(2, -1).T
    return np.linalg.norm(apply_h(Ha, g) - apply_h(Hb, g), axis=1)


def _seed(H, dx=6.0, dy=-4.0, rot_deg=0.3):
    th = np.deg2rad(rot_deg)
    R = np.array([[np.cos(th), -np.sin(th), 0.0], [np.sin(th), np.cos(th), 0.0], [0.0, 0.0, 1.0]])
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]]) @ H @ R


KW = dict(half=16, grid=(8, 5))


def test_register_segment_recovers_known_homography():
    scr, H = _screen(), _H()
    seed = _seed(H)
    assert np.sqrt(np.mean(_map_err(seed, H) ** 2)) > 5.0          # the seed is visibly off
    r = rg.register_segment(_camera(scr, H), scr, seed, t=12.5, name="e400", **KW)
    err = _map_err(r.H, H)
    assert np.sqrt(np.mean(err ** 2)) < 0.5 and err.max() < 1.0
    assert r.model == "h" and r.good and r.flags == []
    assert r.n_inliers >= 20 and r.rms_px < 0.5 and r.ncc_median > 0.9
    assert len(r.iterations) == 3 and r.iterations[0]["search"] == 16 and r.iterations[-1]["search"] == 6
    sh = apply_h(H, [r.ref_point])[0] - apply_h(r.seed_H, [r.ref_point])[0]
    assert np.allclose(r.shift_vs_seed_px, sh, atol=0.5)               # the drift since the seed is reported
    d = r.to_dict(points=True)
    r2 = rg.SegmentRegistration.from_dict(d)
    assert np.allclose(r2.H, r.H) and r2.good and r2.t == 12.5 and len(r2.src) == len(r.src)
    # a FiducialFit-like seed object works too, as does a camera crop (origin)
    crop = _camera(scr, H)[20:220, 30:400]

    class Fid:
        pass
    f = Fid()
    f.H = seed
    rc = rg.register_segment(crop, scr, f, origin=(30, 20), **KW)
    assert rc.good and np.sqrt(np.mean(_map_err(rc.H, H) ** 2)) < 0.5


def test_low_texture_segment_keeps_seed_and_is_flagged():
    flat = np.tile(np.linspace(0.2, 1.0, SW), (SH, 1))                # a 1-D ramp: no 2-D texture anywhere
    H = _H()
    seed = _seed(H, 2.0, 1.0, 0.0)
    r = rg.register_segment(_camera(flat, H), flat, seed, **KW)
    assert r.model == "seed" and not r.good
    assert "low_texture" in r.flags and "seed_only" in r.flags
    assert np.allclose(r.H, seed / seed[2, 2])
    assert r.n_inliers == 0 and np.isnan(r.rms_px)


def test_series_interpolates_low_texture_segment_in_time():
    scr = _screen()
    flat = np.tile(np.linspace(0.2, 1.0, SW), (SH, 1))
    Ha, Hb = _H(30.0, 15.0), _H(34.0, 12.0)                              # 5 px of drift over 2 s
    Hmid = 0.5 * (Ha + Hb)
    segs = [{"t": 2.0, "cam": _camera(scr, Hb, seed=3), "name": "late"},
            {"t": 0.0, "cam": _camera(scr, Ha, seed=1), "name": "early"},
            {"t": 1.0, "cam": _camera(flat, Hmid, seed=2), "screen": flat, "name": "flat"}]
    out = rg.register_series(segs, scr, _seed(Ha), **KW)
    assert [s.name for s in out] == ["early", "flat", "late"]
    early, mid, late = out
    assert early.good and late.good and not mid.good
    assert np.sqrt(np.mean(_map_err(late.H, Hb) ** 2)) < 0.5             # seeded by the previous good segment
    assert mid.fill.startswith("interp early / late") and "filled" in mid.flags
    assert np.sqrt(np.mean(_map_err(mid.H, Hmid) ** 2)) < 0.5
    assert mid.own_H is not None and mid.own_vs_final_px is not None
    # a jump between the neighbours -> no blind interpolation: the neighbour consistent with the segment's own H
    far = [dataclasses.replace(early), dataclasses.replace(mid, H=mid.own_H, fill=None),
           dataclasses.replace(late, H=rg._translation(60.0, 0.0) @ late.H)]
    filled = rg.interpolate_segments(far, max_jump_px=20.0)[1]
    assert filled.fill.startswith("nearest-consistent")


def test_global_search_recovers_a_jump():
    scr, H = _screen(), _H()
    seed = _seed(H, 40.0, -30.0, 0.0)                                   # beyond the +-16 px patch search
    cam = _camera(scr, H)
    r = rg.register_segment(cam, scr, seed, global_search=(64, 64), **KW)
    assert r.global_shift is not None and r.global_shift[2] > 0.3
    assert r.good and np.sqrt(np.mean(_map_err(r.H, H) ** 2)) < 0.5
    assert "jump_vs_seed" in r.flags                                    # the jump itself is reported


def test_ncc_match_subpixel_and_robust_fit_models():
    from scipy.ndimage import gaussian_filter, shift
    rng = np.random.default_rng(0)
    img = gaussian_filter(rng.normal(size=(80, 80)), 2.0)
    moved = shift(img, (1.3, -2.6), order=3)
    dx, dy, pk, sec = rg.ncc_match(moved[10:70, 10:70], img[20:60, 20:60])
    assert dx == pytest.approx(-2.6, abs=0.1) and dy == pytest.approx(1.3, abs=0.1) and pk > 0.95 and sec < pk
    src = rng.uniform(0, 400, (5, 2))
    T = rg._translation(3.0, -2.0)
    fit = rg.robust_fit(src, apply_h(T, src), np.eye(3), min_h=12)
    assert fit["model"] == "s" and np.allclose(fit["H"], T, atol=1e-6)
    fit = rg.robust_fit(src[:2], apply_h(T, src[:2]), np.eye(3))
    assert fit["model"] == "t" and np.allclose(fit["H"], T, atol=1e-9)
    assert rg.robust_fit(np.zeros((0, 2)), np.zeros((0, 2)), T)["model"] == "seed"
