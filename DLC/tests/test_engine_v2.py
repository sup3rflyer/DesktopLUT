"""Tests for the v2 colour engine (``dlc.engine.*``): patch generation, the RBF
display model + LUT builder, the SDR matrix+curve LUT, and the SPD-derived white
point.

Skips cleanly when numpy / scipy / colour are absent so the dependency-free spine
suite still runs on a box without the engine extra installed.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("colour")

from dlc.engine import patches as P
from dlc.engine import lut_rbf as L
from dlc.engine import lut_sdr_reference as S
from dlc.engine import whitepoint as W
from dlc.engine.lut_constrained import build_constrained_rbf_cube, gamut_clip_pressure, gamut_pressure
from dlc.engine.model import DisplayErrorModel, Target, TargetSpace, de_itp
from dlc.engine.physical import StructuredForwardModel, build_physical_cube

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
# Gamut-aware patch generation — cap the colour-ramp saturation to the panel's
# reachable gamut so patches land where the panel can render (not at an
# unreachable target primary). The cap is colorspace-exact (inverts through PQ).
# ---------------------------------------------------------------------------

def test_signal_saturation_caps_are_colorspace_exact_not_xy_geometry():
    from dlc.engine.model import signal_saturation_caps
    from dlc import gamut
    nat = {"R": (0.6927, 0.3028), "G": (0.1825, 0.7502), "B": (0.1521, 0.0646)}  # PA32UCXR
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=(0.3127, 0.329)))
    caps = signal_saturation_caps(space, nat)
    # All three Rec.2020 primaries AND the C/M/Y secondaries are unreachable on this panel → all capped.
    assert set(caps) == {"R", "G", "B", "C", "M", "Y"}
    assert all(0.2 < caps[c] < 0.6 for c in "RGBCMY"), caps
    # The xy-line fraction would say ~0.88; PQ makes the real signal-sat cap far lower.
    xy_frac_blue = gamut.reachable_fraction((0.3127, 0.329), (0.131, 0.046),
                                            [nat["R"], nat["G"], nat["B"]])
    assert caps["B"] < 0.6 * xy_frac_blue          # exact cap is much tighter than the xy fraction
    # A panel that already covers the target → no cap (1.0 each).
    wide = {"R": (0.708, 0.292), "G": (0.170, 0.797), "B": (0.131, 0.046)}
    assert signal_saturation_caps(space, wide) == {c: 1.0 for c in "RGBCMY"}


def test_gamut_capped_ramp_lands_in_gamut_with_one_clip_marker():
    from dlc.engine.model import signal_saturation_caps
    from dlc.gamut import point_in_triangle
    nat = {"R": (0.6927, 0.3028), "G": (0.1825, 0.7502), "B": (0.1521, 0.0646)}
    nt = [nat["R"], nat["G"], nat["B"]]
    space = TargetSpace(Target.hdr_rec2020_pq(white_xy=(0.3127, 0.329)))
    caps = signal_saturation_caps(space, nat)
    tr = P.Transfer.pq(bit_depth=10)

    def txy(rgb):
        sig = [c / tr.max_cv for c in rgb]
        xyz = space.ideal_xyz(np.array([sig], float))[0]
        s = float(sum(xyz))
        return (float(xyz[0]) / s, float(xyz[1]) / s)

    def blue_out(hue_caps):
        ps = P.ramp_patches(tr, steps=13, saturations=(1.0, 0.5),
                            include_secondaries=False, hue_sat_caps=hue_caps, order="luminance")
        blues = [p for p in ps if p[0] == p[1] and p[2] > p[0]]
        return sum(1 for p in blues if not point_in_triangle(txy(p), *nt)), len(blues)

    out_nocap, _ = blue_out(None)
    out_capped, n_capped = blue_out(caps)
    assert out_nocap >= 10                          # the un-capped ramp wastes many blue patches
    assert out_capped <= 2                          # capped: only the clip marker(s) sit out of gamut
    assert n_capped >= 10                           # still a full ramp, just redistributed in-range


# ---------------------------------------------------------------------------
# Gamut-aware VOLUMETRIC build set: project the bulk stimuli onto the reachable
# gamut (no-op for in-gamut), bracket the boundary with target-gamut anchors,
# always carry the neutral tube + dark refinements, and scale with gamut volume.
# ---------------------------------------------------------------------------

_NARROW = {"R": (0.6927, 0.3028), "G": (0.1825, 0.7502), "B": (0.1521, 0.0646)}   # PA32UCXR (~P3-ish)
_WIDE = {"R": (0.708, 0.292), "G": (0.170, 0.797), "B": (0.131, 0.046)}           # covers Rec.2020


def test_reachable_signal_is_noop_in_gamut_and_moves_oog():
    tr = P.Transfer.pq(bit_depth=10)
    m = float(tr.max_cv)
    target = Target.hdr_rec2020_pq(white_xy=(0.3127, 0.329))
    space_none = TargetSpace(target)
    space_reach = TargetSpace(target, reachable_primaries=_NARROW)
    # Grey + near-neutral stimuli are inside ANY gamut → round-trip to their own code value.
    for cv in [(512, 512, 512), (700, 680, 690), (300, 310, 305), (900, 880, 872)]:
        s = np.array([[c / m for c in cv]], float)
        for sp in (space_none, space_reach):
            out = sp.reachable_signal(s)[0]
            assert all(abs(round(o * m) - c) <= 1 for o, c in zip(out, cv)), (cv, out * m)
    # Full-saturation blue is OOG for the narrow panel → reachable_signal pulls chroma in (off-channels
    # rise); with no reachable_primaries it is left untouched (the exact prior behaviour).
    blue = np.array([[0.0, 0.0, 1.0]], float)
    assert space_none.reachable_signal(blue)[0] == pytest.approx([0.0, 0.0, 1.0], abs=1e-4)
    moved = space_reach.reachable_signal(blue)[0]
    assert moved[0] + moved[1] > 0.02               # chroma pulled inward toward the reachable boundary


def test_target_anchor_patches_bracket_the_reachable_boundary():
    from dlc.engine.model import signal_saturation_caps
    from dlc.gamut import point_in_triangle
    nt = [_NARROW["R"], _NARROW["G"], _NARROW["B"]]
    tr = P.Transfer.pq(bit_depth=10)
    target = Target.hdr_rec2020_pq(white_xy=(0.3127, 0.329))
    space_raw = TargetSpace(target)                 # NO clip — caps need the raw space to detect OOG
    levels = [200, 512, 870]
    caps_by_level = {V: signal_saturation_caps(space_raw, _NARROW, level=V / tr.max_cv) for V in levels}
    anchors = P.target_anchor_patches(tr, levels=levels, caps_by_level=caps_by_level, order="luminance")

    def raw_xy(rgb):
        xyz = space_raw.ideal_xyz(np.array([[c / tr.max_cv for c in rgb]], float))[0]
        s = float(sum(xyz))
        return (float(xyz[0]) / s, float(xyz[1]) / s)

    inside = [p for p in anchors if min(p) > 0]                 # just-inside anchor (off-channel lit)
    markers = [p for p in anchors if min(p) == 0]              # OOG clip marker (off-channel == 0)
    assert inside and markers                                   # the boundary is bracketed both sides
    for p in inside:
        assert point_in_triangle(raw_xy(p), *nt), p             # just-inside anchors are reachable
    for p in markers:
        assert not point_in_triangle(raw_xy(p), *nt), p         # clip markers sit just outside


def test_gamut_aware_volumetric_drops_oog_keeps_interior_and_scales_with_gamut():
    # _volumetric_bulk/_project_and_thin are patch_sets internals (moved there in fable
    # Phase 7b); the public names stay importable from dlc.calibrate via the re-export shim.
    from dlc.patch_sets import PatchSizes, build_volumetric_set, _volumetric_bulk, _project_and_thin
    from dlc.gamut import point_in_triangle
    nt = [_NARROW["R"], _NARROW["G"], _NARROW["B"]]
    tr = P.Transfer.pq(bit_depth=10)
    m = float(tr.max_cv)
    target = Target.hdr_rec2020_pq(white_xy=(0.3127, 0.329))
    space_raw = TargetSpace(target)
    ps = PatchSizes(volumetric_mode="gamut", gamut_lum_steps=6, gamut_hues=8, low_light_steps=5)

    def in_gamut(p):
        if p[0] == p[1] == p[2]:
            return True                                                       # grey is always reachable
        xyz = space_raw.ideal_xyz(np.array([[c / m for c in p]], float))[0]
        s = float(sum(xyz))
        return s <= 0 or point_in_triangle((xyz[0] / s, xyz[1] / s), *nt)

    def bright_oog_frac(pset):
        # Chromaticity is only meaningful above the shadow band; sub-nit chroma is noise-dominated and
        # deliberately left sparse + un-capped (the foundation's dark set), so OOG is measured on the
        # bright chroma the projection actually operates on.
        chroma = [p for p in pset if not (p[0] == p[1] == p[2]) and max(p) / m >= 0.25]
        return sum(1 for p in chroma if not in_gamut(p)) / max(1, len(chroma))

    # 1. Projecting the bulk onto the reachable gamut eliminates the vast majority of bright OOG reads.
    bulk = _volumetric_bulk(ps, tr, warm_tau=None, max_cv=None)
    proj = _project_and_thin(bulk, target=target, reachable_primaries=_NARROW, transfer=tr, max_cv=None)
    assert bright_oog_frac(bulk) > 0.3
    assert bright_oog_frac(proj) < 0.10

    ga = build_volumetric_set(ps, tr, target=target, reachable_primaries=_NARROW)

    def sat(p):
        mx = max(p)
        return 0.0 if mx == 0 else (mx - min(p)) / mx

    # 2. Content bias preserved: the grey axis never moves (projection is a no-op on neutral), and the
    #    near-neutral interior (where ~99 % of content lives) is not thinned away — it grows, because
    #    the always-on neutral tube + dark foundation is added on top of the preserved bulk interior.
    assert {p for p in bulk if p[0] == p[1] == p[2]} <= set(ga)
    assert sum(1 for p in ga if sat(p) < 0.2) >= sum(1 for p in bulk if sat(p) < 0.2)
    # 3. Density scales with reachable gamut volume + no micro-clusters: a uniform cube over a NARROW
    #    panel collapses many OOG corners onto a thin boundary, so min-separation thinning sheds the
    #    near-duplicates ("50 patches on 0.001 of blue"); over a WIDE panel projection is a no-op so
    #    every distinct sample is kept. Fixed spacing ⇒ surviving count tracks reachable volume.
    cube = _volumetric_bulk(PatchSizes(volumetric_mode="cube", cube_size=9, low_light_cube_size=0),
                            tr, warm_tau=None, max_cv=None)
    cube_narrow = _project_and_thin(cube, target=target, reachable_primaries=_NARROW, transfer=tr, max_cv=None)
    cube_wide = _project_and_thin(cube, target=target, reachable_primaries=_WIDE, transfer=tr, max_cv=None)
    assert len(cube_wide) == len(cube)             # wide panel ⊇ target ⇒ projection is a no-op
    assert len(cube_narrow) < 0.7 * len(cube_wide)  # narrow panel ⇒ OOG corners collapse + thin away
    # 4. The colorimetric foundation is present: target-gamut anchors leave OOG clip markers that
    #    bracket the boundary (the intentionally-OOG brackets that are exempt from projection).
    assert any(not in_gamut(p) for p in ga)


def test_verify_is_one_fixed_preset_shape_for_both_modes():
    # Owner directive B: one "cover all bases" verify preset for SDR and HDR. The reachable cap is the
    # only SDR/HDR difference and is a structural no-op when the panel covers the target (caps all 1.0).
    from dlc.calibrate import PatchSizes, build_verify_set
    tr = P.Transfer.pq(bit_depth=10)
    ps = PatchSizes()
    no_cap = build_verify_set(ps, tr, hue_sat_caps=None)                       # SDR (no cap)
    noop_cap = build_verify_set(ps, tr, hue_sat_caps={c: 1.0 for c in "RGBCMY"})  # panel ⊇ target
    assert no_cap == noop_cap

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


def test_shadow_levels_add_low_light_density():
    tf = P.Transfer.power(2.2, 120.0, bit_depth=8)
    shadow = P.shadow_levels(9, tf, max_signal=0.20, bias=2.0)
    uniform = P.uniform_levels(9, tf.max_cv)

    assert shadow[0] == 0
    assert shadow[-1] <= round(tf.max_cv * 0.20)
    assert shadow[1] < uniform[1]                 # packed toward black
    assert len(set(shadow) - set(uniform)) >= 6   # genuinely additive, not a no-op


def test_ramp_anchors_and_dedup():
    tf = P.Transfer.power(2.2, 120.0)
    ramp = P.ramp_patches(tf, steps=21, order="thermal")
    assert (0, 0, 0) in ramp and (tf.max_cv,) * 3 in ramp
    assert len(ramp) == len(set(ramp))  # deduped


def test_headroom_levels_extends_above_cap_to_full_drive():
    lv = P.headroom_levels(713, 1023, steps=6)
    assert lv and lv[-1] == 1023           # reaches full drive
    assert all(v > 713 for v in lv)        # strictly above the cap (713 already present)
    assert lv == sorted(lv) and len(lv) == len(set(lv))
    assert P.headroom_levels(1023, 1023) == []   # nothing above full drive
    assert P.headroom_levels(800, 700) == []     # to<=from


def test_ramp_extend_to_cv_adds_grey_headroom_only():
    # The RAW full-drive headroom extension (LG C6 WRGB fix): GREY levels go above the peak-code
    # cap to full drive so the neutral roll-off is measured; COLOUR ramps stay bounded at the cap
    # (full-drive primaries would storm the white-referenced plausibility envelope).
    tr = P.Transfer.pq(bit_depth=10)
    cap = tr.nits_to_cv(1600.0)                        # HDR peak cap < full scale (1023)
    base = P.ramp_patches(tr, steps=13, saturations=(1.0,), max_cv=cap, order="none")
    ext = P.ramp_patches(tr, steps=13, saturations=(1.0,), max_cv=cap,
                         extend_to_cv=tr.max_cv, order="none")
    base_greys = {p[0] for p in base if p[0] == p[1] == p[2]}
    ext_greys = {p[0] for p in ext if p[0] == p[1] == p[2]}
    # New grey levels appear above the cap, up to full drive.
    assert ext_greys > base_greys
    assert max(ext_greys) == tr.max_cv and max(base_greys) <= cap
    assert all(g <= cap for g in base_greys)
    # Colour patches are unchanged — none introduced above the cap.
    base_colours = {p for p in base if not (p[0] == p[1] == p[2])}
    ext_colours = {p for p in ext if not (p[0] == p[1] == p[2])}
    assert base_colours == ext_colours
    assert all(max(p) <= cap for p in ext_colours)


def test_saturation_sweep_preserves_repeated_skeleton_reads():
    tf = P.Transfer.power(2.2, 120.0, bit_depth=8)
    sweep = P.saturation_sweep_patches(tf, repeats=3, order="none")
    assert len(sweep) == 4 * 7 * 3
    counts = Counter(sweep)
    assert set(counts.values()) == {3}
    assert (64, 64, 64) in counts
    assert (255, 0, 0) in counts and (0, 255, 255) in counts


def _is_secondary(p):
    """A C/M/Y patch: two channels equal-and-high, the third strictly lower (and not grey)."""
    hi = max(p)
    return hi > 0 and list(p).count(hi) == 2 and min(p) < hi and not (p[0] == p[1] == p[2])


def test_ramp_primaries_only_drops_secondaries():
    # The MHC foundation ramp uses grey + R/G/B only (a matrix+1D can't fit C/M/Y). Secondaries
    # appear with include_secondaries=True (default, back-compat) and are absent when False.
    tf = P.Transfer.power(2.2, 120.0)
    full = P.ramp_patches(tf, steps=9, include_secondaries=True)
    prim = P.ramp_patches(tf, steps=9, include_secondaries=False)
    assert any(_is_secondary(p) for p in full)
    assert not any(_is_secondary(p) for p in prim)
    # primaries-only still has the grey ramp + R/G/B endpoints
    assert (tf.max_cv, 0, 0) in prim and (0, tf.max_cv, 0) in prim and (0, 0, tf.max_cv) in prim
    assert len(prim) < len(full)


def test_near_neutral_tube_is_offaxis_and_near_neutral():
    # The ICC foundation tube samples R≠G≠B *close* to the grey axis (off-axis non-additivity /
    # white-balance data) along the six hue directions — never on the diagonal, never wildly saturated.
    tf = P.Transfer.pq()
    levels = [128, 512, 800]
    tube = P.near_neutral_tube_patches(tf, levels=levels, offsets=(0.06, 0.15), max_cv=835)
    assert tube and len(tube) == len(set(tube))                      # deduped
    assert all(not (p[0] == p[1] == p[2]) for p in tube)            # all off-axis
    # near-neutral: chroma stays a modest fraction of the level (≤ ~2*max_offset), never a primary
    for p in tube:
        lvl = max(p)
        assert (max(p) - min(p)) <= 0.4 * lvl + 2                    # bounded chroma
    # all six hue directions appear at a mid level (R,G,B,C,M,Y around grey)
    mid = [p for p in tube if 380 <= max(p) <= 640]
    assert len({tuple(int(c > min(p)) for c in p) for p in mid}) >= 5


def _is_tube_patch(p):
    """A near-neutral tube sample: all three channels lit (min>0), off the grey axis, with chroma
    only a modest fraction of the level. Distinguishes it from pure-channel ramps (a zero channel)."""
    return min(p) > 0 and not (p[0] == p[1] == p[2]) and (max(p) - min(p)) <= 0.4 * max(p) + 2


def test_foundation_tube_optin_and_backcompat():
    # icc_tube_levels=0 (default) ⇒ the foundation is the old grey+RGB ramp (back-compat, no tube);
    # turning it on adds near-neutral off-axis patches without dropping any ramp patch.
    from dlc.calibrate import PatchSizes, build_ramp_set
    tf = P.Transfer.pq()
    base = build_ramp_set(PatchSizes(), tf, max_cv=835)
    withtube = build_ramp_set(PatchSizes(icc_tube_levels=10, icc_tube_offsets=(0.06, 0.15)),
                              tf, max_cv=835)
    assert not any(_is_tube_patch(p) for p in base)                  # default: no tube
    assert len(withtube) > len(base)
    assert len(withtube) == len(set(withtube))                       # deduped union
    assert set(base).issubset(set(withtube))                         # no ramp patch dropped
    assert any(_is_tube_patch(p) for p in withtube)                  # near-neutral tube present


def test_cube_count_and_dedup():
    tf = P.Transfer.pq()
    cube = P.cube_patches(tf, size=9)
    assert len(cube) == 9 ** 3
    assert len(cube) == len(set(cube))


def test_cube_low_light_mini_cube_is_additive():
    tf = P.Transfer.power(2.2, 120.0, bit_depth=8)
    base = P.cube_patches(tf, size=5, order="none")
    dense = P.cube_patches(tf, size=5, low_light_size=5, order="none")
    dark_cap = round(tf.max_cv * 0.20)

    assert len(dense) > len(base)
    assert len(dense) == len(set(dense))
    assert sum(1 for p in dense if max(p) <= dark_cap) > sum(1 for p in base if max(p) <= dark_cap)


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


def test_patch_energy_is_peak_channel_luminance():
    tf = P.Transfer.pq()
    assert P.patch_energy((0, 0, 0), tf) == 0.0
    assert P.patch_energy((tf.max_cv,) * 3, tf) == pytest.approx(10000.0, rel=1e-6)
    # peak channel drives the proxy, not luminance: a saturated-red patch's energy is its red CV.
    red = (tf.max_cv, 0, 0)
    assert P.patch_energy(red, tf) == pytest.approx(tf.cv_to_nits(tf.max_cv))


def test_mean_patch_energy_matches_build_set_mean():
    # The soak-into-calibration coupling: a preheat fed the build set parks at this mean.
    tf = P.Transfer.pq()
    cube = P.cube_patches(tf, size=9)
    by_hand = sum(P.patch_energy(p, tf) for p in cube) / len(cube)
    assert P.mean_patch_energy(cube, tf) == pytest.approx(by_hand)
    assert P.mean_patch_energy([], tf) == 0.0


def test_warm_tau_threads_and_defaults():
    # None coalesces to the built-in default; a different measured tau changes the warm-start
    # rotation; the result is always a valid permutation of the input.
    tf = P.Transfer.pq()
    cube = P.cube_patches(tf, size=9, order="none")
    assert P.sort_patches(cube, "thermal", tf, warm_tau=None) == P.sort_patches(cube, "thermal", tf)
    fast = P.sort_patches(cube, "thermal", tf, warm_tau=1)
    slow = P.sort_patches(cube, "thermal", tf, warm_tau=200)
    assert fast != slow
    assert sorted(fast) == sorted(slow) == sorted(cube)
    # the builders thread it through to the same effect
    assert P.cube_patches(tf, size=9, warm_tau=1, order="thermal") == fast


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


def test_gamut_low_light_levels_are_additive_when_requested():
    tf = P.Transfer.power(2.2, 120.0, bit_depth=8)
    base = P.gamut_patches(tf, lum_steps=9, hues=6, order="none")
    dense = P.gamut_patches(tf, lum_steps=9, hues=6, low_light_steps=9, order="none")
    dark_cap = round(tf.max_cv * 0.20)

    assert len(dense) > len(base)
    assert len(dense) == len(set(dense))
    assert sum(1 for p in dense if max(p) <= dark_cap) > sum(1 for p in base if max(p) <= dark_cap)


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


def test_model_confidence_weights_trust_repeated_skeleton_anchor():
    target = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0)
    space = TargetSpace(target)
    axis = np.linspace(0.0, 1.0, 5)
    sig = np.array([[r, g, b] for b in axis for g in axis for r in axis], dtype=float)
    measured = _synth_measure(space, sig, gains=(1.0, 0.99, 0.98), noise=0.0)
    red_idx = int(np.where(np.all(np.isclose(sig, [1.0, 0.0, 0.0]), axis=1))[0][0])
    measured[red_idx] = space.ideal_xyz(np.array([[0.82, 0.08, 0.04]], dtype=float))[0]

    plain = DisplayErrorModel(sig, measured, target, smoothing=2.0)
    conf = np.ones(len(sig))
    conf[red_idx] = 1000.0
    weighted = DisplayErrorModel(sig, measured, target, smoothing=2.0, sample_confidence=conf)

    anchor = sig[[red_idx]]
    target_ictcp = space.xyz_to_ictcp(measured[[red_idx]])
    plain_err = de_itp(space.xyz_to_ictcp(plain.forward(anchor)) - target_ictcp)[0]
    weighted_err = de_itp(space.xyz_to_ictcp(weighted.forward(anchor)) - target_ictcp)[0]
    assert weighted_err < plain_err / 4


@pytest.mark.slow
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


def test_cube_diagnostics_report_large_reversals_and_lattice_jumps():
    cube = L.identity_cube(5)
    cube[0, 0, 2, 0] = cube[0, 0, 1, 0] - 0.02
    diag = L.cube_diagnostics(cube)
    assert diag.non_monotonic >= 1
    assert diag.large_reversal_count >= 1
    assert diag.large_reversal_threshold == pytest.approx(0.008)
    assert diag.worst_lattice_jump > 0.0
    assert diag.as_dict()["large_reversal_count"] == diag.large_reversal_count


def test_structured_forward_model_fits_physical_xyz_panel():
    target = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0)
    space = TargetSpace(target)
    sig = np.array([[r, g, b]
                    for b in np.linspace(0, 1, 5)
                    for g in np.linspace(0, 1, 5)
                    for r in np.linspace(0, 1, 5)], dtype=float)
    measured = _synth_measure(space, sig, gains=(1.0, 0.982, 0.955),
                              gammas=(1.0, 1.015, 0.985), noise=0.0)
    raw = de_itp(space.xyz_to_ictcp(measured) - space.ideal_ictcp(sig))
    model = StructuredForwardModel(sig, measured, target)
    fit = model.residual_de_itp()
    assert float(fit.mean()) < float(raw.mean()) / 3
    assert float(np.percentile(fit, 95)) < float(np.percentile(raw, 95))


@pytest.mark.slow
def test_physical_cube_reduces_model_error_and_pins_neutral():
    from dlc.optimize import sample_cube

    target = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0)
    space = TargetSpace(target)
    sig = np.array([[r, g, b]
                    for b in np.linspace(0, 1, 5)
                    for g in np.linspace(0, 1, 5)
                    for r in np.linspace(0, 1, 5)], dtype=float)
    measured = _synth_measure(space, sig, gains=(1.0, 0.985, 0.965),
                              gammas=(1.0, 1.01, 0.99), noise=0.0)
    model = StructuredForwardModel(sig, measured, target)
    cube, info = build_physical_cube(model, 5, sig, metric="de2000", max_correction=0.25,
                                     neutral_band=0.05, maxiter=35)

    before = de_itp(space.xyz_to_ictcp(model.forward(sig)) - space.ideal_ictcp(sig))
    driven = sample_cube(cube, sig)
    after = de_itp(space.xyz_to_ictcp(model.forward(driven)) - space.ideal_ictcp(sig))
    assert float(after.mean()) < float(before.mean())
    assert info.solved_nodes == 5 ** 3 - 5

    axis = np.linspace(0, 1, 5)
    for i, a in enumerate(axis):
        assert np.allclose(cube[i, i, i], [a, a, a], atol=1e-9)


@pytest.mark.slow
def test_constrained_rbf_caps_off_channel_lift_at_saturated_blue():
    target = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0)
    space = TargetSpace(target)
    narrow = {"R": [0.66, 0.33], "G": [0.25, 0.66], "B": [0.15, 0.07]}
    sig = np.array([[r, g, b]
                    for b in np.linspace(0, 1, 5)
                    for g in np.linspace(0, 1, 5)
                    for r in np.linspace(0, 1, 5)], dtype=float)
    measured = _synth_measure(space, sig, gains=(1.0, 1.0, 0.90), noise=0.0)
    model = DisplayErrorModel(sig, measured, target, smoothing=1e-3,
                              reachable_primaries=narrow)

    cube, info = build_constrained_rbf_cube(
        model, 5, sig, max_correction=0.35, n_iterations=2, neutral_band=0.0,
        reachable_primaries=narrow, off_channel_lift=0.002, maxiter=30,
    )

    blue = cube[-1, 0, 0]  # cube indexed [B,G,R] for input signal [0,0,1]
    assert info.constrained_nodes > 0
    assert blue[0] <= 0.0021
    assert blue[1] <= 0.0021


def test_gamut_pressure_fires_at_reachable_boundary_even_when_measured():
    target = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0, white_xy=(0.3127, 0.329))
    narrow = {"R": [0.66, 0.33], "G": [0.25, 0.66], "B": [0.15, 0.07]}
    space = TargetSpace(target, reachable_primaries=narrow)
    pressure = gamut_pressure(space, np.array([[0.5, 0.5, 0.5], [0.0, 0.0, 1.0]]), narrow)
    assert pressure[0] < 0.25
    assert pressure[1] > 0.9


def test_gamut_clip_pressure_stays_low_for_reachable_saturated_red():
    target = Target.sdr_srgb_power(gamma=2.2, white_nits=120.0, white_xy=(0.3127, 0.329))
    wide = {"R": [0.70, 0.30], "G": [0.20, 0.72], "B": [0.14, 0.05]}
    space = TargetSpace(target, reachable_primaries=wide)
    pressure = gamut_clip_pressure(space, np.array([[1.0, 0.0, 0.0]]), wide)
    assert pressure[0] < 0.25


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
# lut_sdr_reference (production-unreachable reference port — Phase 5 disposition)
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


@pytest.mark.slow
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


# ---------------------------------------------------------------------------
# Fable audit Phase 2 — input-side invariants (transfers, colour floor, white default)
# ---------------------------------------------------------------------------

def test_transfer_power_is_pure_power_never_piecewise_srgb():
    # Owner hard requirement: SDR is a pure power law, NEVER the piecewise sRGB EOTF.
    # Pin the exact formula and pin that it DIFFERS from piecewise sRGB at mid-signal
    # (so a future "helpful" swap to colour-science's sRGB cctf fails loudly).
    t = P.Transfer.power(gamma=2.2, peak_nits=120.0, bit_depth=10)
    for cv in (64, 256, 512, 767, 1023):
        s = cv / t.max_cv
        assert t.cv_to_nits(cv) == pytest.approx(120.0 * s ** 2.2, rel=1e-12)
    s = 0.5
    piecewise = ((s + 0.055) / 1.055) ** 2.4          # sRGB EOTF at 0.5
    assert abs(0.5 ** 2.2 - piecewise) > 1e-3          # the two curves genuinely differ here
    assert t.cv_to_nits(round(s * t.max_cv)) != pytest.approx(120.0 * piecewise, rel=1e-3)


def test_ramp_color_floor_is_full_scale_and_never_overlaps_grey_toe():
    # color_min_signal is a fraction of the FULL-scale signal (absolute under PQ:
    # 0.25 ~ 1 nit for any target peak), deliberately not of the HDR max_cv peak cap;
    # and the floored colour never dips into the low_light grey toe band.
    tr = P.Transfer.pq(bit_depth=10)
    cap = tr.nits_to_cv(1600.0)                        # HDR peak cap < full scale
    floor_signal, toe_signal = 0.25, 0.20
    ramp = P.ramp_patches(tr, steps=13, saturations=(1.0,), max_cv=cap,
                          low_light_steps=9, low_light_signal=toe_signal,
                          color_min_signal=floor_signal, order="none")
    colour_patches = [p for p in ramp if not (p[0] == p[1] == p[2])]
    greys = [p[0] for p in ramp if p[0] == p[1] == p[2]]
    floor_cv = round(floor_signal * tr.max_cv)         # full-scale domain (the pin)
    assert min(max(p) for p in colour_patches) >= floor_cv
    # the grey toe still covers the dark EOTF below the colour floor
    toe_top = round(toe_signal * cap)                  # cap-relative shadow band
    assert any(0 < g <= toe_top for g in greys)
    assert floor_cv > toe_top                          # no-overlap invariant


def test_white_from_spd_file_default_is_numeric_d65(tmp_path):
    # The default strength must match target_white's (0 = numeric D65) — a silent
    # full-strength perceptual correction is never a default.
    wl = np.arange(380, 731, 5.0)

    def gauss(c, w, a):
        return a * np.exp(-0.5 * ((wl - c) / w) ** 2)

    vals = gauss(450, 12, 1.0) + gauss(530, 12, 1.0) + gauss(620, 14, 1.2)
    sp = tmp_path / "white.sp"
    sp.write_text(
        'CGATS.17\nSPECTRAL_BANDS "%d"\nSPECTRAL_START_NM "380"\nSPECTRAL_END_NM "730"\n'
        "BEGIN_DATA\n" % len(vals) + " ".join("%.6f" % v for v in vals) + "\nEND_DATA\n",
        encoding="utf-8")
    res = W.white_from_spd_file(sp)                    # no strength argument
    assert res["xy"] == pytest.approx(W.LEGACY_D65_XY, abs=1e-9)
    assert res["strength"] == 0.0
