"""Tests for the #C3 native-gamut target clamp (constant-luminance, hue-preserving chroma clip).

A real panel's native gamut is narrower than Rec.2020, so a saturated ideal target is physically
unreachable; without a clamp the optimizer/verify chase it and score a gamut clip as panel error.
The clamp projects the target onto the reachable gamut, preserving ICtCp intensity (I) and hue and
pulling chroma in to the boundary. Gated on `reachable_primaries` ⇒ a no-op when absent (back-compat)
and when the target is already inside the panel gamut (e.g. sRGB on any real wide-gamut panel).
"""
from __future__ import annotations

import math

import colour
import numpy as np

from dlc.engine.model import (
    Target, TargetSpace, score_hdr, _native_colourspace,
)
from dlc.metrics import score_samples
from dlc.mhc import Ti3Sample

D65 = (0.3127, 0.3290)
# A NARROW native panel gamut (well inside Rec.2020) — saturated Rec.2020 targets fall outside it.
NARROW = {"R": [0.66, 0.33], "G": [0.25, 0.66], "B": [0.15, 0.07]}
# A wide gamut that comfortably contains the near-neutral region — the in-gamut targets used in the
# no-op test sit well inside it, so the clamp must leave them untouched. (It does NOT fully enclose
# saturated Rec.2020, which is expected: on a real HDR run the saturated corners DO get clamped.)
WIDE = {"R": [0.74, 0.27], "G": [0.12, 0.83], "B": [0.11, 0.03]}


def _native_rgb(xyz_abs, primaries, scale: float = 10000.0):
    return colour.XYZ_to_RGB(np.asarray(xyz_abs, float) / scale, _native_colourspace(primaries, D65))


def test_clamp_is_noop_when_target_inside_native_gamut():
    tgt = Target.hdr_rec2020_pq(white_xy=D65)
    plain = TargetSpace(tgt)
    clamped = TargetSpace(tgt, reachable_primaries=WIDE)
    # Neutral + low-saturation signals are inside the panel gamut ⇒ identical ideal, byte for byte.
    sig = np.array([[0.5, 0.5, 0.5], [0.4, 0.42, 0.45], [0.3, 0.3, 0.3]])
    assert np.allclose(plain.ideal_xyz(sig), clamped.ideal_xyz(sig), rtol=1e-9, atol=1e-9)


def test_clamp_is_noop_without_reachable_primaries():
    # No reachable_primaries (the default / no DIP) ⇒ the prior unclamped behaviour, even for a
    # wildly out-of-gamut signal.
    tgt = Target.hdr_rec2020_pq(white_xy=D65)
    sig = np.array([[0.6, 0.0, 0.0]])
    assert np.allclose(TargetSpace(tgt).ideal_xyz(sig),
                       TargetSpace(tgt, reachable_primaries=None).ideal_xyz(sig))


def test_clamp_projects_out_of_gamut_target_to_reachable_boundary():
    tgt = Target.hdr_rec2020_pq(white_xy=D65)
    sig = np.array([[0.6, 0.0, 0.0]])   # saturated red — outside the narrow panel gamut
    ip = TargetSpace(tgt).ideal_xyz(sig)
    ic = TargetSpace(tgt, reachable_primaries=NARROW).ideal_xyz(sig)
    # The clamp engaged (the unclamped target was unreachable) and the result IS reachable.
    assert not np.allclose(ip, ic)
    assert _native_rgb(ip, NARROW).min() < -1e-4            # plain target outside the narrow gamut
    rgb_clamped = _native_rgb(ic, NARROW)
    assert rgb_clamped.min() >= -2e-3 and rgb_clamped.max() <= 1.0 + 2e-3
    # Constant-luminance + hue preserved, chroma pulled in.
    icp = colour.XYZ_to_ICtCp(ip)[0]
    icc = colour.XYZ_to_ICtCp(ic)[0]
    assert abs(icc[0] - icp[0]) < 5e-3                                    # intensity (I) preserved
    assert (icc[1] ** 2 + icc[2] ** 2) < (icp[1] ** 2 + icp[2] ** 2)      # chroma reduced
    assert abs(math.atan2(icc[2], icc[1]) - math.atan2(icp[2], icp[1])) < 2e-2   # hue preserved


def test_clamp_makes_a_gamut_clip_score_as_reachable():
    # Feed the panel's BEST (the clamped ideal) as the measurement. WITH the clamp the target IS that
    # reachable point → ~0 dE_ITP; WITHOUT it the same measurement vs the unreachable full-Rec.2020
    # target scores a large dE — the clip masquerading as panel error the optimizer would chase.
    tgt = Target.hdr_rec2020_pq(white_xy=D65)
    sig = [[0.5, 0.0, 0.0]]
    reachable_xyz = TargetSpace(tgt, reachable_primaries=NARROW).ideal_xyz(np.array(sig)).tolist()
    clamped = score_hdr(sig, reachable_xyz, white_xy=D65, reachable_primaries=NARROW)["de_itp"]
    plain = score_hdr(sig, reachable_xyz, white_xy=D65)["de_itp"]
    assert clamped[0] < 1.0
    assert plain[0] > 10.0


def test_sdr_verify_can_score_against_clamped_reachable_target():
    tgt = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0, white_xy=D65)
    sig = (0.0, 0.0, 1.0)   # saturated sRGB blue, outside this deliberately narrow native gamut
    reachable_xyz = TargetSpace(tgt, reachable_primaries=NARROW).ideal_xyz(np.array([sig]))[0]
    sample = Ti3Sample(rgb=sig, xyz=tuple(float(c) for c in reachable_xyz))

    clamped, _ = score_samples([sample], gamma=2.2, white_xy=D65, luminance=120.0,
                               reachable_primaries=NARROW)
    plain, _ = score_samples([sample], gamma=2.2, white_xy=D65, luminance=120.0)

    assert clamped[0].de2000 < 0.05
    assert plain[0].de2000 > 1.0


def test_xyz_to_ictcp_guards_nonphysical_inputs_without_touching_physical():
    """colour.XYZ_to_ICtCp returns finite-but-ASTRONOMICAL values on non-physical XYZ (a positive
    channel with collapsed luminance -> negative LMS -> PQ blow-up), which detonates dE_ITP and
    silently corrupts cube snapshot selection (the value is finite, so NaN/inf guards miss it).
    xyz_to_ictcp projects onto the physically-realizable (LMS>=0) cone first: bit-identical for every
    physical colour (incl. legitimate wide-gamut OOG), bounded for non-physical extrapolations."""
    from dlc.engine.model import _project_to_ictcp_cone, de_itp

    sp = TargetSpace(Target.hdr_rec2020_pq(white_xy=D65))

    physical = np.array([
        [0.00095, 0.001, 0.001089],                          # sub-nit neutral
        [95.047, 100.0, 108.883],                            # D65 white @ 100 cd/m^2
        colour.RGB_to_XYZ([1.0, 0.0, 0.0], "sRGB") * 120.0,  # full red
        colour.RGB_to_XYZ([0.0, 0.0, 1.0], "sRGB") * 100.0,  # sRGB blue (OOG on a narrow panel)
    ], dtype=float)
    # No-op for physical inputs: projection unchanged, ICtCp bit-identical to raw colour.
    assert np.array_equal(_project_to_ictcp_cone(physical), physical)
    assert np.allclose(sp.xyz_to_ictcp(physical), colour.XYZ_to_ICtCp(physical), atol=0, rtol=0)

    nonphysical = np.array([[0.04, 0.0, 0.0], [0.0, 0.0, 0.04], [1.0, 0.0, 2.0]], dtype=float)
    assert np.max(np.abs(colour.XYZ_to_ICtCp(nonphysical))) > 1e5   # the latent detonation
    guarded = sp.xyz_to_ictcp(nonphysical)
    assert np.all(np.isfinite(guarded)) and np.max(np.abs(guarded)) < 10.0
    # dE_ITP against a bounded target stays bounded -> cannot poison snapshot selection.
    target = sp.ideal_ictcp(np.array([[0.0, 0.0, 0.5]]))
    de = de_itp(guarded - target)
    assert np.all(np.isfinite(de)) and de.max() < 1e4
