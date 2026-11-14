"""The base build's inversion between raw greys runs in the TRANSFER domain above the shadows (mhc_cube
``transfer_domain_gaps`` / ``invert_monotone_domain``), in linear light below.

DOMAINS: the simulated panels are defined in the wire code (light per channel = transfer(code) x a smooth tilt
[+ black]); the build inverts each channel's native share; the error is judged THROUGH the panel as light
delivered / light wanted at every code between the raw greys (the build's target: SDR (code/max)^2.2 of the
delivered white; HDR the PQ light up to the cube peak). No noise: this is the interpolant's own error.
"""

from __future__ import annotations

import json as _json
import math
from pathlib import Path as _Path

import pytest

from dlc import mhc_cube as mc
from dlc.colormath import matvec, rgb_to_xyz_matrix
from dlc.engine.patches import Transfer
from dlc.mhc import Ti3Sample
from dlc.patch_sets import PatchSizes, build_ramp_set

PRIM = {"rx": 0.6960, "ry": 0.3040, "gx": 0.1818, "gy": 0.7500, "bx": 0.1512, "by": 0.0648}
WHITE = (0.3134, 0.3268)


def _P(peak):
    return rgb_to_xyz_matrix(PRIM["rx"], PRIM["ry"], PRIM["gx"], PRIM["gy"], PRIM["bx"], PRIM["by"], *WHITE,
                             white_Y=peak)


def _srgb_eotf(v):
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _sdr_panel(kind="power", contrast=None):
    black = 0.0 if contrast is None else 1.0 / contrast

    def share(ch, d):
        d = min(max(d, 0.0), 1.0)
        k = 1.0 + ((1.02, 1.0, 0.99)[ch] - 1.0) * (1.0 - d)
        base = _srgb_eotf(d) if kind == "srgb" else d ** 2.2
        return black + (1.0 - black) * base * k
    return share


def _through(P, share, curves, code, max_cv=1023):
    s = code / max_cv
    n = len(curves["r"])
    x = s * (n - 1)
    j = min(int(x), n - 2)
    drive = [curves[ch][j] + (curves[ch][j + 1] - curves[ch][j]) * (x - j) for ch in "rgb"]
    return matvec(P, [share(c, d) for c, d in enumerate(drive)])[1]


def _sdr_errors(share, *, lo=125, transfer_domain=True, monkeypatch=None):
    peak = 121.0
    P = _P(peak)
    t = Transfer.power(gamma=2.2, peak_nits=peak, bit_depth=10)
    raw_codes = sorted({p[0] for p in build_ramp_set(PatchSizes(), t) if p[0] == p[1] == p[2]})
    raw = [Ti3Sample(rgb=(c / 1023,) * 3, xyz=tuple(matvec(P, [share(ch, c / 1023) for ch in range(3)])))
           for c in raw_codes]
    if not transfer_domain:
        monkeypatch.setattr(mc, "TRANSFER_DOMAIN_MIN_LIGHT", math.inf)
    curves, _ = mc.build_sdr_cube(raw, PRIM, WHITE, peak, gamma=2.2, dark_floor_nits=0.1)
    if not transfer_domain:
        monkeypatch.undo()
    wy = _through(P, share, curves, 1023)
    return [(c, _through(P, share, curves, c) / (wy * (c / 1023) ** 2.2) - 1.0)
            for c in range(lo, 1023) if c not in raw_codes], raw_codes, curves


def test_domain_inverter_is_the_old_one_with_every_gap_linear_and_agrees_at_the_greys():
    xs = [0.0, 0.1, 0.3, 0.6, 1.0]
    ys = [0.0, 0.006, 0.07, 0.32, 1.0]
    enc = lambda v: max(v, 0.0) ** (1 / 2.2)   # noqa: E731
    for tgt in (0.0, 0.003, 0.006, 0.05, 0.2, 0.5, 1.0, 1.2):
        assert mc.invert_monotone_domain(xs, ys, tgt, enc, [False] * 5) == mc.invert_monotone(xs, ys, tgt)
    for x, y in zip(xs, ys):
        assert abs(mc.invert_monotone_domain(xs, ys, y, enc, [False] + [True] * 4) - x) < 1e-12


def test_transfer_domain_gaps_start_above_the_shadows():
    # gap k is decided by its LOWER grey: at least 1 % of white above the black AND more than twice the black
    y = [0.12, 0.5, 1.0, 1.3, 5.0, 50.0, 120.0]
    g = mc.transfer_domain_gaps(y, 120.0, 0.12)
    assert g == [False, False, False, False, False, True, True]     # 1.3 - 0.12 = 1.18 < 1.2
    assert mc.transfer_domain_gaps(y, 120.0, 0.0)[4] is True        # no black: 1.3 >= 1.2
    assert mc.transfer_domain_gaps([1.0, 2.0, 3.0], 100.0, 0.9)[2] is False   # 2.0 - 0.9 = 1.1 < 2 x 0.9


def test_sdr_power_law_mid_gap_under_drive_is_gone(monkeypatch):
    # linear light: every gap's mid-point under-driven (mean -0.15 %, worst ~-1.1 % above 1 % of white);
    # transfer domain: mean within 0.02 %, rms < 0.1 %
    for contrast in (None, 1000, 300):
        share = _sdr_panel(contrast=contrast)
        old, _, _ = _sdr_errors(share, transfer_domain=False, monkeypatch=monkeypatch)
        new, _, _ = _sdr_errors(share)
        mean_old = sum(e for _, e in old) / len(old)
        mean_new = sum(e for _, e in new) / len(new)
        rms_old = math.sqrt(sum(e * e for _, e in old) / len(old))
        rms_new = math.sqrt(sum(e * e for _, e in new) / len(new))
        assert mean_old < -0.001 and abs(mean_new) < 0.0003, (contrast, mean_old, mean_new)
        assert rms_new < 0.35 * rms_old, (contrast, rms_old, rms_new)
        assert min(e for _, e in new) > min(e for _, e in old)


def test_sdr_shadow_gaps_are_built_as_before(monkeypatch):
    # below 1 % of white the inversion is linear light, so the curve there is the old curve (a linear-toe
    # sRGB-piecewise panel: the case where the transfer domain would be worse)
    share = _sdr_panel(kind="srgb", contrast=1000)
    _, raw_codes, new = _sdr_errors(share)
    _, _, old = _sdr_errors(share, transfer_domain=False, monkeypatch=monkeypatch)
    grey_y = [matvec(_P(121.0), [share(ch, c / 1023) for ch in range(3)])[1] for c in raw_codes]
    gaps = mc.transfer_domain_gaps(grey_y, 121.0, grey_y[0])
    k = gaps.index(True)
    assert 0 < k < len(raw_codes)
    # every entry whose target light is below the lower grey of the first transfer-domain gap is inverted in
    # linear light: bit-identical to before (2 % margin for the channel tilt)
    j_end = int(1023 * (0.98 * (grey_y[k - 1] - grey_y[0]) / 121.0) ** (1 / 2.2))
    assert j_end > 50
    for ch in "rgb":
        assert new[ch][:j_end] == old[ch][:j_end], ch
    assert new != old


def test_hdr_pq_panel_mid_gap_is_closer(monkeypatch):
    peak = 1000.0
    P = _P(peak)
    t = Transfer.pq(bit_depth=10)

    def share(ch, d):
        d = min(max(d, 0.0), 1.0)
        k = 1.0 + ((1.02, 1.0, 0.99)[ch] - 1.0) * (1.0 - d)
        return min(mc.pq_eotf(d) * mc._PQ_CONTAINER_NITS / peak, 1.0) * k

    cap = t.nits_to_cv(peak)
    raw_codes = sorted({p[0] for p in build_ramp_set(PatchSizes(), t, max_cv=cap) if p[0] == p[1] == p[2]})
    raw = [Ti3Sample(rgb=(c / 1023,) * 3, xyz=tuple(matvec(P, [share(ch, c / 1023) for ch in range(3)])))
           for c in raw_codes]

    def errs(curves):
        out = []
        for c in range(300, cap):
            if c in raw_codes:
                continue
            want = mc.pq_eotf(c / 1023) * mc._PQ_CONTAINER_NITS
            out.append(_through(P, share, curves, c) / want - 1.0)
        return out
    new, _ = mc.build_hdr_cube(raw, PRIM, WHITE, peak, dark_floor_nits=0.3)
    monkeypatch.setattr(mc, "TRANSFER_DOMAIN_MIN_LIGHT", math.inf)
    old, _ = mc.build_hdr_cube(raw, PRIM, WHITE, peak, dark_floor_nits=0.3)
    e_new, e_old = errs(new), errs(old)
    rms = lambda v: math.sqrt(sum(x * x for x in v) / len(v))   # noqa: E731
    assert sum(e_old) / len(e_old) < -0.001
    assert abs(sum(e_new) / len(e_new)) < 0.25 * abs(sum(e_old) / len(e_old))
    assert rms(e_new) < 0.5 * rms(e_old)


# ---------------------------------------------------------------------------
# REAL-RUN cubes: the raw grey ramps of two past SDR runs (tests/data/raw_greys_*.json: BenQ PD2700U 2026-10-01,
# PA32UCXR 2026-09-25), built with the transfer-domain inversion and with it off (every gap linear light, as
# before). Pinned: nothing moves down, the shadows below the first transfer-domain gap are bit-identical, the
# curves stay monotone, the largest move, and entries of the new cube.
# ---------------------------------------------------------------------------

_REAL = {
    "benq_20261001": {"first_moved": 142, "worst": (0.8011086466816743, "r", 186),
                      "pins": {"r300": 0.2880034439099171, "g600": 0.5785811872150262, "b900": 0.8768073933964149,
                               "g162": 0.15118160227543792}},
    "pa32_20260925": {"first_moved": 150, "worst": (1.0650298969239977, "r", 174),
                      "pins": {"r170": 0.1743062904039243, "g300": 0.2974209250821215, "b600": 0.5903495618496033,
                               "r900": 0.8712180084972455}},
}


def _real_build(label, monkeypatch=None, off=False):
    doc = _json.loads((_Path(__file__).parent / "data" / f"raw_greys_{label}.json").read_text(encoding="utf-8"))
    samples = [Ti3Sample(rgb=(s,) * 3, xyz=tuple(x)) for s, x in doc["greys"]]
    if off:
        monkeypatch.setattr(mc, "TRANSFER_DOMAIN_MIN_LIGHT", math.inf)
    curves, _ = mc.build_sdr_cube(samples, doc["primaries"], tuple(doc["white_xy"]), doc["peak"], gamma=2.2)
    if off:
        monkeypatch.undo()
    return curves


@pytest.mark.parametrize("label", sorted(_REAL))
def test_real_run_cube_moves_only_up_mid_gap_and_keeps_the_shadows(label, monkeypatch):
    exp = _REAL[label]
    new = _real_build(label)
    old = _real_build(label, monkeypatch, off=True)
    moves = [((b - a) * 1023, ch, j) for ch in "rgb" for j, (a, b) in enumerate(zip(old[ch], new[ch]))]
    assert all(m >= -1e-9 for m, _ch, _j in moves)                                   # never down
    first = min(j for m, _ch, j in moves if abs(m) > 1e-9)
    assert first == exp["first_moved"]
    for ch in "rgb":
        assert new[ch][:first] == old[ch][:first]                                     # shadows bit-identical
        assert all(new[ch][j + 1] >= new[ch][j] for j in range(len(new[ch]) - 1))    # monotone
    worst = max(moves)
    assert worst[1:] == exp["worst"][1:] and worst[0] == pytest.approx(exp["worst"][0], abs=1e-9)
    for key, v in exp["pins"].items():
        assert new[key[0]][int(key[1:])] == pytest.approx(v, abs=1e-12)
