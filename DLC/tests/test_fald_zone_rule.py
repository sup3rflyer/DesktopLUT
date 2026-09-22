"""Work guide C12b — the black-frame LED boost's ZONE RULE: which zones the firmware counts as non-black.

``FaldParams.boost_rule`` "dim" = LIT-or-DIM (C12, legacy; every fit / table / FLD4 file without the key), "mean" =
LIT-or-MEAN: LIT (any pixel above boost_lit_nits) OR the zone mean of (brightest channel, as-if-white nits)^gamma >=
thresh (gamma 0.62, T 0.0693 — the 2026-09-20 refit over all 64 meter + camera observations,
results/fald_inside_2026-09-18/zone_rule_refit/). The rule travels: boost_table.json (zone_rule / mean_gamma /
mean_thresh) -> FaldParams -> FLD4 words 53-55 -> C++ loader -> CB words 72-74 -> the statistic shader; the GPU-order twin
is gpuemu.Emu.stat_active. Legacy everything (no key / word 53 = 0) must behave and export exactly as before.
"""
from __future__ import annotations

import json
import re
import struct
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dlc.fald.boost import load_boost_table  # noqa: E402
from dlc.fald.correct import load_fitted_params  # noqa: E402
from dlc.fald.export import BOOST_RULE_CODES, MAGIC4, export_panel_params  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import BOOST_RULE_DIM, BOOST_RULE_MEAN, cb, read_panel_file  # noqa: E402
from dlc.fald.profile import params_dict, params_from_dict  # noqa: E402

_DLC = Path(__file__).resolve().parents[1]
_SRC = _DLC.parent / "src"
_SHADER = _SRC.parent / "shared" / "fald_shader.h"   # shared by the overlay and the DWM hook since e7f542f
_SHARED = _SRC.parent / "shared"                      # fald_panel.{h,cpp}: CB size, boost-rule constants, the loader (e7f542f)
_HOOK = _SRC.parent / "dwm_hook" / "hook_fald.cpp"    # the DWM hook's FillCB: CB-word for CB-word the overlay's
_INSIDE = _DLC / "results" / "fald_inside_2026-09-18"
GAMMA, THRESH = 0.62, 0.0693
ZW, ZH = 80, 45
SMALL_LUT = ((0.0, 1.178), (8 / 144, 1.167), (29 / 144, 1.10), (44 / 144, 1.0), (58 / 144, 1.07), (87 / 144, 1.0))

# The 64 observations of the refit (zone_rule_refit/observations.py build(), 2026-09-20): (id, rects (x0, y0, w, h, PQ10
# code) in ONE 80 x 45-px zone, background code, counted?, confidence). "ambiguous" rows are listed, never scored.
OBS = [
    ("full_c1", [], 1, False, "solid"),
    ("full_c2", [], 2, False, "solid"),
    ("full_c4", [], 4, False, "solid"),
    ("full_c8", [], 8, False, "solid"),
    ("full_c16", [], 16, False, "solid"),
    ("full_c32", [], 32, True, "solid"),
    ("full_c64", [], 64, True, "solid"),
    ("full_c85", [], 85, True, "solid"),
    ("hline2_c85", [(0, 22, 80, 2, 85)], 0, False, "solid"),
    ("vline2_c85", [(39, 0, 2, 45, 85)], 0, False, "solid"),
    ("band_h11_c85", [(0, 0, 80, 11, 85)], 0, True, "solid"),
    ("band_h20_c85", [(0, 0, 80, 20, 85)], 0, True, "solid"),
    ("band_h18_c85", [(0, 0, 80, 18, 85)], 0, True, "solid"),
    ("blk607_col47_c85", [(0, 0, 47, 45, 85)], 0, True, "solid"),
    ("blk607_row22_c85", [(0, 0, 80, 22, 85)], 0, True, "solid"),
    ("blk650_col10_c85", [(0, 0, 10, 45, 85)], 0, False, "solid"),
    ("blk550_row10_c85", [(0, 0, 80, 10, 85)], 0, True, "medium"),
    ("blk500_row5_c85", [(0, 0, 80, 5, 85)], 0, False, "ambiguous"),
    ("frame160_col3_c307", [(0, 0, 3, 45, 307)], 0, True, "solid"),
    ("window_col3_c683", [(0, 0, 3, 45, 683)], 0, True, "medium"),
    ("col1_c307", [(0, 0, 1, 45, 307)], 0, True, "solid"),
    ("col2_c307", [(0, 0, 2, 45, 307)], 0, True, "solid"),
    ("col3_c307", [(0, 0, 3, 45, 307)], 0, True, "solid"),
    ("col4_c307", [(0, 0, 4, 45, 307)], 0, True, "solid"),
    ("col6_c307", [(0, 0, 6, 45, 307)], 0, True, "solid"),
    ("col2_c153", [(0, 0, 2, 45, 153)], 0, True, "solid"),
    ("col4_c153", [(0, 0, 4, 45, 153)], 0, True, "solid"),
    ("col6_c153", [(0, 0, 6, 45, 153)], 0, True, "solid"),
    ("col8_c153", [(0, 0, 8, 45, 153)], 0, True, "solid"),
    ("col10_c153", [(0, 0, 10, 45, 153)], 0, True, "solid"),
    ("col14_c153", [(0, 0, 14, 45, 153)], 0, True, "solid"),
    ("col12_c85", [(0, 0, 12, 45, 85)], 0, False, "solid"),
    ("col14_c85", [(0, 0, 14, 45, 85)], 0, False, "solid"),
    ("col16_c85", [(0, 0, 16, 45, 85)], 0, True, "solid"),
    ("col18_c85", [(0, 0, 18, 45, 85)], 0, True, "solid"),
    ("col20_c85", [(0, 0, 20, 45, 85)], 0, True, "solid"),
    ("col24_c85", [(0, 0, 24, 45, 85)], 0, True, "solid"),
    ("col1_c520", [(0, 0, 1, 45, 520)], 0, True, "solid"),
    ("col2_c520", [(0, 0, 2, 45, 520)], 0, True, "solid"),
    ("col1_c760", [(0, 0, 1, 45, 760)], 0, True, "solid"),
    ("dot1_c307", [(40, 22, 1, 1, 307)], 0, True, "solid"),
    ("dot2_c307", [(39, 21, 2, 2, 307)], 0, True, "solid"),
    ("dot4_c307", [(38, 20, 4, 4, 307)], 0, True, "solid"),
    ("dot8_c307", [(36, 18, 8, 8, 307)], 0, True, "solid"),
    ("dot1_c520", [(40, 22, 1, 1, 520)], 0, True, "solid"),
    ("dot2_c520", [(39, 21, 2, 2, 520)], 0, True, "solid"),
    ("dot4_c520", [(38, 20, 4, 4, 520)], 0, True, "solid"),
    ("dot8_c520", [(36, 18, 8, 8, 520)], 0, True, "solid"),
    ("dot1_c760", [(40, 22, 1, 1, 760)], 0, True, "solid"),
    ("dot2_c760", [(39, 21, 2, 2, 760)], 0, True, "solid"),
    ("dot4_c760", [(38, 20, 4, 4, 760)], 0, True, "solid"),
    ("dot8_c760", [(36, 18, 8, 8, 760)], 0, True, "solid"),
    ("col2_c99", [(0, 0, 2, 45, 99)], 0, False, "solid"),
    ("col2_c111", [(0, 0, 2, 45, 111)], 0, True, "solid"),
    ("col2_c120", [(0, 0, 2, 45, 120)], 0, True, "solid"),
    ("col2_c136", [(0, 0, 2, 45, 136)], 0, True, "solid"),
    ("act8x8_c193", [(36, 18, 8, 8, 193)], 0, True, "solid"),
    ("seed40x23_c29", [(0, 0, 40, 23, 29)], 0, False, "solid"),
    ("seed40x23_c37", [(0, 0, 40, 23, 37)], 0, False, "solid"),
    ("seed40x23_c64", [(0, 0, 40, 23, 64)], 0, False, "solid"),
    ("seed40x23_c99", [(0, 0, 40, 23, 99)], 0, True, "solid"),
    ("seed57x32_c37", [(0, 0, 57, 32, 37)], 0, False, "solid"),
    ("seed80x45_c37", [(0, 0, 80, 45, 37)], 0, True, "solid"),
    ("seed80x45_c64", [(0, 0, 80, 45, 64)], 0, True, "solid"),
]


def _small_params(**kw):
    return FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, **kw)


def _emu(tmp_path, params, name="p.bin"):
    info = export_panel_params(FaldModel(params), tmp_path / name)
    return Emu(read_panel_file(tmp_path / name), width=params.width, height=params.height), info


def _legacy_active(model, img):
    """FaldModel.active_zone_fraction as it was before C12b (commit 0e85180), verbatim."""
    p = model.p
    blocks = np.max(img, axis=0).reshape(p.rows, model.ch, p.cols, model.cw)
    lit = (blocks > p.boost_lit_nits).mean(axis=(1, 3)) > p.boost_lit_frac
    dim = (blocks > p.boost_dim_nits).mean(axis=(1, 3)) > p.boost_dim_frac
    return lit | dim


def _random_frame(m, rng):
    """Raster frame with one random probe per zone around both rules' thresholds: black / a full dim field / a rect of
    random size and level (on black or on a dim background) / a bright dot in one channel."""
    img = np.zeros((3, m.h, m.w))
    for r in range(m.p.rows):
        for c in range(m.p.cols):
            z = img[:, r * m.ch:(r + 1) * m.ch, c * m.cw:(c + 1) * m.cw]
            kind = rng.integers(0, 5)
            if kind == 1:
                z[:] = 10 ** rng.uniform(-2.6, -0.6)
            elif kind in (2, 4):
                if kind == 4:
                    z[:] = 10 ** rng.uniform(-3.0, -1.6)
                w, h = rng.integers(1, m.cw + 1), rng.integers(1, m.ch + 1)
                x0, y0 = rng.integers(0, m.cw - w + 1), rng.integers(0, m.ch - h + 1)
                z[:, y0:y0 + h, x0:x0 + w] = 10 ** rng.uniform(-2.0, 0.0)
            elif kind == 3:
                z[rng.integers(0, 3), rng.integers(0, m.ch), rng.integers(0, m.cw)] = 10 ** rng.uniform(-0.8, 3.0)
    return img


# ------------------------------------------------------------------------------------------------ the parameters travel
def test_default_rule_is_legacy_and_the_rule_travels_with_the_boost_fields(tmp_path):
    p = FaldParams()
    assert (p.boost_rule, p.boost_mean_gamma, p.boost_mean_thresh) == ("dim", GAMMA, THRESH)
    q = replace(p, boost_lut=SMALL_LUT, boost_rule="mean", boost_mean_gamma=0.55, boost_mean_thresh=0.08)
    back = params_from_dict(json.loads(json.dumps(params_dict(q))))
    assert (back.boost_rule, back.boost_mean_gamma, back.boost_mean_thresh, back.boost_lut) == ("mean", 0.55, 0.08, SMALL_LUT)
    d = params_dict(q)
    (tmp_path / "fit.json").write_text(json.dumps({"params": d}))
    assert load_fitted_params(tmp_path / "fit.json").boost_rule == "mean"
    for k in ("boost_rule", "boost_mean_gamma", "boost_mean_thresh"):
        d.pop(k)                                                     # a fit from before C12b
    (tmp_path / "old.json").write_text(json.dumps({"params": d}))
    old = load_fitted_params(tmp_path / "old.json")
    assert (old.boost_rule, old.boost_mean_gamma, old.boost_mean_thresh) == ("dim", GAMMA, THRESH) and old.boost_lut == SMALL_LUT
    assert params_from_dict(d).boost_rule == "dim"
    # the boost table: zone_rule / mean_gamma / mean_thresh ride next to boost_lit_* / boost_dim_*
    rows = [{"N": 10, "boost": 1.17}, {"N": 1000, "boost": 1.0}]
    table = {"mode": "HDR", "zones_total": 2304, "boost_lit_nits": 0.35, "boost_dim_frac": 0.19, "rows": rows}
    (tmp_path / "legacy.json").write_text(json.dumps(table))
    kw = load_boost_table(tmp_path / "legacy.json", mode="HDR", zones_total=2304)
    assert not {"boost_rule", "boost_mean_gamma", "boost_mean_thresh"} & set(kw)          # FaldParams' default ("dim") stays
    assert replace(FaldParams(), **kw).boost_rule == "dim"
    (tmp_path / "mean.json").write_text(json.dumps({**table, "zone_rule": "mean", "mean_gamma": 0.6, "mean_thresh": 0.07}))
    kw = load_boost_table(tmp_path / "mean.json", mode="HDR", zones_total=2304)
    assert (kw["boost_rule"], kw["boost_mean_gamma"], kw["boost_mean_thresh"], kw["boost_lit_nits"]) == ("mean", 0.6, 0.07, 0.35)
    assert replace(FaldParams(), **kw).boost_rule == "mean"
    (tmp_path / "bad.json").write_text(json.dumps({**table, "zone_rule": "median"}))
    with pytest.raises(ValueError, match="zone_rule"):
        load_boost_table(tmp_path / "bad.json")
    with pytest.raises(ValueError, match="boost_rule"):
        FaldModel(_small_params(boost_rule="median")).active_zone_fraction(np.zeros((3, 108, 192)))


# ------------------------------------------------------------------------------------------------ the model
def test_model_legacy_rule_is_unchanged_and_the_mean_rule_separates_the_camera_seeds():
    m_dim, m_mean = FaldModel(_small_params()), FaldModel(_small_params(boost_rule="mean"))
    rng = np.random.default_rng(12)
    for _ in range(6):                                               # "dim" = the pre-C12b function, bit for bit
        img = _random_frame(m_dim, rng)
        assert np.array_equal(m_dim.active_zones(img), _legacy_active(m_dim, img))
        assert m_dim.active_zone_fraction(img) == float(_legacy_active(m_dim, img).mean())

    def one(m, paint):
        img = np.zeros((3, m.h, m.w))
        paint(img[:, 2 * m.ch:3 * m.ch, 3 * m.cw:4 * m.cw])
        act = m.active_zones(img)
        assert int(act.sum()) == int(act[2, 3])                      # nothing but the painted zone
        return bool(act[2, 3])

    def full(v):
        return lambda z: z.__setitem__(slice(None), v)

    def rect(w, h, v):
        return lambda z: z.__setitem__((slice(None), slice(0, h), slice(0, w)), v)
    for m in (m_dim, m_mean):
        assert not one(m, full(0.0054)) and one(m, full(0.0216))                 # PQ10 code 16 black, code 32 not
        assert one(m, rect(1, 1, 10.0)) and one(m, rect(1, 9, 0.403)) and not one(m, rect(1, 9, 0.298))   # LIT
        assert one(m, rect(4, 9, 0.2003)) and not one(m, rect(2, 9, 0.2003))     # 0.2-nit slivers: 20 px yes, 10 px no
    # the camera's seeds (raster-aligned stand-ins: 40 x 25 px and 55 x 30 px): the pixel-FRACTION rule counts them,
    # the firmware (and the mean rule) does not
    for w, h, v in ((8, 5, 0.017), (8, 5, 0.03), (8, 5, 0.1), (11, 6, 0.03)):
        assert one(m_dim, rect(w, h, v)) and not one(m_mean, rect(w, h, v))
    assert one(m_mean, full(0.03))
    # the statistic itself: mean of nits^gamma against T, parameters honoured
    m_lin = FaldModel(_small_params(boost_rule="mean", boost_mean_gamma=1.0, boost_mean_thresh=0.05))
    assert one(m_lin, rect(8, 9, 0.101)) and not one(m_lin, rect(8, 9, 0.099))   # half the zone: mean = v / 2
    # led_boost reads the rule: 40 seeded zones count under "dim" only
    lut = ((0.0, 1.17), (20 / 144, 1.0))
    img = np.zeros((3, m_dim.h, m_dim.w))
    for r in range(4):
        for c in range(10):
            img[:, r * 9:r * 9 + 5, c * 16:c * 16 + 8] = 0.03
    img[:, 60:69, 64:80] = 500.0
    assert FaldModel(_small_params(boost_lut=lut)).led_boost(img) == 1.0
    assert FaldModel(_small_params(boost_lut=lut, boost_rule="mean")).led_boost(img) == 1.17


# ------------------------------------------------------------------------------------------------ the GPU-order twin
@pytest.mark.parametrize("rule", ["dim", "mean"])
def test_twin_equals_the_model_on_random_raster_frames(tmp_path, rule):
    p = _small_params(boost_lut=SMALL_LUT, boost_rule=rule)
    m = FaldModel(p)
    emu, info = _emu(tmp_path, p)
    assert info["boost_rule"] == rule and emu.boostRule == BOOST_RULE_CODES[rule]
    rng = np.random.default_rng(7)
    seen = set()
    for _ in range(8):
        img = _random_frame(m, rng)
        full = np.repeat(np.repeat(img, 5, axis=1), 5, axis=2)
        want = m.active_zones(img)
        assert np.array_equal(emu.stat_active(full), want)
        b, n, act = emu.frame_boost(full)
        assert n == int(want.sum()) == round(m.active_zone_fraction(img) * 144) and float(b) == float(np.float32(m.led_boost(img)))
        seen.add(n)
    assert len(seen) > 3 and 0 < min(seen) and max(seen) < 144       # the frames exercise the rule, not a constant


def _obs_zone(i, cols):
    per_row = (cols - 2) // 2
    return 1 + 2 * (i // per_row), 1 + 2 * (i % per_row)             # (zone row, zone col): every other zone, every other row


def _obs_frame(params):
    """All 64 observations in one full-resolution frame of nits (H, W), each in its own zone."""
    nits_of = params.code_to_nits(np.arange(1024))
    codes = np.zeros((params.height, params.width), dtype=np.int64)
    for i, (_, rects, bg, _, _) in enumerate(OBS):
        zr, zc = _obs_zone(i, params.cols)
        z = codes[zr * ZH:(zr + 1) * ZH, zc * ZW:(zc + 1) * ZW]
        z[:] = bg
        for x0, y0, w, h, c in rects:
            z[y0:y0 + h, x0:x0 + w] = c
    return nits_of[codes]


def test_the_64_observations_through_the_twin_at_full_resolution(tmp_path):
    assert len(OBS) == 64 and sum(o[4] != "ambiguous" for o in OBS) == 63
    wrong = {}
    for rule in ("dim", "mean"):
        p = replace(FaldParams(), boost_lut=((0.0, 1.17), (0.5, 1.0)), boost_rule=rule)
        assert (p.cell_w, p.cell_h, p.cols, p.rows) == (ZW, ZH, 48, 48)
        emu, _ = _emu(tmp_path, p, f"{rule}.bin")
        nits = _obs_frame(p)
        act = emu.stat_active(np.repeat(nits[None], 3, axis=0))
        where = [_obs_zone(i, p.cols) for i in range(len(OBS))]
        wrong[rule] = [o[0] for o, (zr, zc) in zip(OBS, where) if o[4] != "ambiguous" and bool(act[zr, zc]) != o[3]]
        assert int(act.sum()) == sum(bool(act[zr, zc]) for zr, zc in where)       # no stray zone
        # float64 cross-check of the twin's float32 GPU-order sums: the same verdicts
        blocks = nits.reshape(48, ZH, 48, ZW)
        second = (np.power(blocks, GAMMA).mean(axis=(1, 3)) >= THRESH) if rule == "mean" else ((blocks > 0.011).mean(axis=(1, 3)) > 0.19)
        assert np.array_equal(act, (blocks > 0.35).any(axis=(1, 3)) | second)
    assert wrong["mean"] == []                                                     # every scored observation, solid and medium
    assert wrong["dim"] == ["seed40x23_c29", "seed40x23_c37", "seed40x23_c64", "seed57x32_c37"]   # the refit's finding


def test_model_raster_misses_only_the_14px_sliver_under_the_mean_rule():
    """The scale-5 raster renders the 14-px (not counted) and the 16-px (counted) 0.2-nit slivers as the same 15 px, so
    one of them is always wrong on the raster; every other observation is right. (The shader / twin are full-res.)"""
    p = replace(FaldParams(), boost_rule="mean")
    m = FaldModel(p)
    W, H = float(p.width), float(p.height)
    shapes = [((0, 0, 0), (0.0, 0.0, 1.0, 1.0))]
    for i, (_, rects, bg, _, _) in enumerate(OBS):
        zr, zc = _obs_zone(i, p.cols)
        X, Y = zc * ZW, zr * ZH
        if bg:
            shapes.append(((bg,) * 3, (X / W, Y / H, ZW / W, ZH / H)))
        shapes += [((c,) * 3, ((X + x0) / W, (Y + y0) / H, w / W, h / H)) for x0, y0, w, h, c in rects]
    act = m.active_zones(m.render(shapes))
    wrong = [o[0] for i, o in enumerate(OBS) if o[4] != "ambiguous" and bool(act[_obs_zone(i, p.cols)]) != o[3]]
    assert wrong == ["col14_c85"]


@pytest.mark.skipif(not (_INSIDE / "zone_rule_refit" / "observations.py").exists(), reason="local refit data (gitignored results/)")
def test_embedded_observations_are_the_local_refits():
    sys.path.insert(0, str(_INSIDE / "zone_rule_refit"))
    try:
        import observations as ob
        local = [(o["id"], [tuple(r) for r in o["rects"]], o["bg"], o["outcome"], o["solid"]) for o in ob.build()]
    finally:
        sys.path.pop(0)
    assert local == [(a, [tuple(r) for r in b], c, d, e) for a, b, c, d, e in OBS]


# ------------------------------------------------------------------------------------------------ the panel file
def test_fld4_words_53_55_round_trip_and_a_legacy_rule_export_is_unchanged(tmp_path):
    base = _small_params(boost_lut=SMALL_LUT)
    i_dim = export_panel_params(FaldModel(base), tmp_path / "dim.bin")
    i_mean = export_panel_params(FaldModel(replace(base, boost_rule="mean")), tmp_path / "mean.bin")
    i_odd = export_panel_params(FaldModel(replace(base, boost_rule="mean", boost_mean_gamma=0.5, boost_mean_thresh=0.0885)), tmp_path / "odd.bin")
    assert (i_dim["boost_rule"], i_mean["boost_rule"], i_odd["format"]) == ("dim", "mean", "FLD4")
    a, b = (tmp_path / "dim.bin").read_bytes(), (tmp_path / "mean.bin").read_bytes()
    assert struct.unpack_from("<I", a, 0)[0] == MAGIC4 and a[53 * 4:56 * 4] == bytes(12)     # legacy: the reserved zeros of C12
    assert struct.unpack_from("<I2f", b, 53 * 4) == (1, float(np.float32(GAMMA)), float(np.float32(THRESH)))
    assert a[:53 * 4] == b[:53 * 4] and a[56 * 4:] == b[56 * 4:] and len(a) == len(b)      # nothing else moves
    # a fit with other mean parameters but the LEGACY rule exports the legacy bytes: they do not leak into the file
    export_panel_params(FaldModel(replace(base, boost_mean_gamma=0.5, boost_mean_thresh=0.2)), tmp_path / "dim2.bin")
    assert (tmp_path / "dim2.bin").read_bytes() == a
    od, om, oo = (read_panel_file(tmp_path / n) for n in ("dim.bin", "mean.bin", "odd.bin"))
    assert (od["boostRule"], od["words53_55"]) == (BOOST_RULE_DIM, [0, 0, 0])
    assert (od["boostMeanGamma"], od["boostMeanThresh"]) == (np.float32(GAMMA), np.float32(THRESH))   # the C++ defaults, unused
    assert (om["boostRule"], om["boostMeanGamma"], om["boostMeanThresh"]) == (BOOST_RULE_MEAN, np.float32(GAMMA), np.float32(THRESH))
    assert (oo["boostRule"], oo["boostMeanGamma"], oo["boostMeanThresh"]) == (BOOST_RULE_MEAN, np.float32(0.5), np.float32(0.0885))
    assert om["boostLut"] == od["boostLut"] and om["boostDimFrac"] == od["boostDimFrac"]
    c = cb(om)
    assert (c["boostRuleCB"], c["boostMeanGammaCB"], c["boostMeanThreshCB"], c["boostNCB"]) == (1, np.float32(GAMMA), np.float32(THRESH), 6)
    assert cb(od)["boostRuleCB"] == 0
    # a fit without a LUT never writes the block, whatever its rule says
    i_none = export_panel_params(FaldModel(_small_params(boost_rule="mean")), tmp_path / "none.bin")
    assert i_none["format"] == "FLD1" and i_none["boost_rule"] is None and read_panel_file(tmp_path / "none.bin")["boostRule"] == 0


def test_export_and_reader_refuse_what_the_cpp_loader_refuses(tmp_path):
    base = _small_params(boost_lut=SMALL_LUT, boost_rule="mean")
    for kw in ({"boost_mean_gamma": 0.0}, {"boost_mean_gamma": -0.62}, {"boost_mean_gamma": 4.5}, {"boost_mean_gamma": float("nan")},
               {"boost_mean_thresh": 0.0}, {"boost_mean_thresh": -1.0}, {"boost_mean_thresh": float("inf")},
               {"boost_mean_thresh": float("nan")}, {"boost_rule": "median"}):
        with pytest.raises(ValueError, match="boost"):
            export_panel_params(FaldModel(replace(base, **kw)), tmp_path / "never.bin")
        assert not (tmp_path / "never.bin").exists()
    export_panel_params(FaldModel(replace(base, boost_mean_gamma=4.0)), tmp_path / "edge.bin")         # the gate's closed end
    assert read_panel_file(tmp_path / "edge.bin")["boostMeanGamma"] == np.float32(4.0)
    # legacy rule: out-of-gate mean parameters are irrelevant (not written, not judged)
    export_panel_params(FaldModel(replace(base, boost_rule="dim", boost_mean_gamma=0.0)), tmp_path / "legacy.bin")
    export_panel_params(FaldModel(base), tmp_path / "ok.bin")
    good = (tmp_path / "ok.bin").read_bytes()

    def patched(word, fmt, value):
        buf = bytearray(good)
        struct.pack_into(fmt, buf, 4 * word, value)
        (tmp_path / "bad.bin").write_bytes(bytes(buf))
        return tmp_path / "bad.bin"
    for word, fmt, value, needle in ((53, "<I", 2, "zone rule"), (53, "<f", 1.0, "zone rule"), (54, "<f", 0.0, "mean-rule"),
                                     (54, "<f", 4.0001, "mean-rule"), (54, "<f", float("nan"), "mean-rule"),
                                     (55, "<f", 0.0, "mean-rule"), (55, "<f", -0.07, "mean-rule"),
                                     (55, "<f", float("inf"), "mean-rule"), (55, "<f", float("nan"), "mean-rule")):
        with pytest.raises(ValueError, match=needle):
            read_panel_file(patched(word, fmt, value))
    # rule 0 ignores words 54 / 55 (the C++ loader does not read them)
    legacy = bytearray(good)
    struct.pack_into("<I2f", legacy, 53 * 4, 0, -7.0, float("nan"))
    (tmp_path / "legacy_garbage.bin").write_bytes(bytes(legacy))
    o = read_panel_file(tmp_path / "legacy_garbage.bin")
    assert (o["boostRule"], o["boostMeanGamma"], o["boostMeanThresh"]) == (0, np.float32(GAMMA), np.float32(THRESH))


@pytest.mark.skipif(not (_INSIDE / "pa32ucxr_fald_panel_boost.bin").exists(), reason="local panel file (gitignored results/)")
def test_the_shipped_legacy_panel_file_still_exports_byte_for_byte(tmp_path):
    """The HDR daily driver of 2026-09-19 (C12, LIT-or-DIM): today's exporter writes the same bytes from the same fit."""
    p = load_fitted_params(_INSIDE / "fald_fit_result_area_boostlut.json")
    assert p.boost_rule == "dim" and len(p.boost_lut) == 15
    info = export_panel_params(FaldModel(p), tmp_path / "again.bin")
    assert info["format"] == "FLD4" and info["boost_rule"] == "dim"
    assert (tmp_path / "again.bin").read_bytes() == (_INSIDE / "pa32ucxr_fald_panel_boost.bin").read_bytes()
    o = read_panel_file(_INSIDE / "pa32ucxr_fald_panel_boost.bin")
    assert (o["boostRule"], o["words53_55"]) == (0, [0, 0, 0])


# ------------------------------------------------------------------------------------------------ the C++ / HLSL side
@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_hlsl_and_cpp_carry_the_zone_rule():
    src = _SHADER.read_text(encoding="utf-8")

    def part(name):
        return re.search(name + r' = R"\((.*?)\)";', src, re.S).group(1)
    common, stat = part("g_faldCommonSource"), part("g_faldStatSource")
    fields = re.findall(r"(?:uint|float) (\w+);", re.search(r"cbuffer FaldCB : register\(b0\) \{(.*?)\n\};", common, re.S).group(1))
    assert len(fields) == 84 and fields[72:76] == ["boostRule", "boostMeanGamma", "boostMeanThresh", "glowOn"]   # 75: S2's switch
    # the sweep: the sum only under rule 1, only over mc > 0 (no pow(0), no negative, no NaN), exp(gamma * log) not pow()
    assert "if (boostRule == 1u && mc > 0.0f) powSum += exp(boostMeanGamma * log(mc));" in stat and "pow(" not in stat
    assert stat.index("if (mc > boostDimNits) dim++;") < stat.index("powSum += exp(") < stat.index("gPow[tid.x] = powSum;")
    assert "groupshared float gPow[256];" in stat and "gPow[tid.x] += gPow[tid.x + stride];" in stat
    assert "bool second = (boostRule == 1u) ? (gPow[0] / (float)n >= boostMeanThresh) : (dimF > boostDimFrac);" in stat
    assert "activeOut[uint2(cx, cy)] = (litF > boostLitFrac || second) ? 1.0f : 0.0f;" in stat
    assert stat.count("if (boostN != 0u)") == 2                     # no LUT: neither summed nor written
    for other in ("g_faldConvSource", "g_faldBoostSource", "g_faldPixelSource", "g_faldGainSource", "g_faldTemporalSource"):
        assert "boostRule" not in part(other) and "boostMean" not in part(other)
    # the constants, FaldPanelParams and the loader live in shared/fald_panel.{h,cpp} since e7f542f (one parser for the
    # overlay and the DWM hook); FillCB and the dump stay in src/fald.cpp, the hook's FillCB writes the same words
    h = (_SHARED / "fald_panel.h").read_text(encoding="utf-8")
    loader = (_SHARED / "fald_panel.cpp").read_text(encoding="utf-8")
    c = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    hook = _HOOK.read_text(encoding="utf-8")
    assert "FALD_CB_BYTES = 336" in h and "FALD_BOOST_RULE_DIM = 0;" in h and "FALD_BOOST_RULE_MEAN = 1;" in h
    assert "float boostMeanGamma = 0.62f, boostMeanThresh = 0.0693f;" in h            # = FaldParams / panelfile defaults
    assert "u[72] = p.boostRule; f[73] = p.boostMeanGamma; f[74] = p.boostMeanThresh;" in c
    assert "u[72] = p.boostRule; f[73] = p.boostMeanGamma; f[74] = p.boostMeanThresh;" in hook
    assert "FALD_BOOST_WORD_RULE = 53;" in loader and "unknown boost zone rule" in loader and "implausible boost mean-rule words" in loader
    assert "!(meanGamma > 0.0f && meanGamma <= 4.0f) || !(meanThresh > 0.0f && std::isfinite(meanThresh))" in loader
    for key in ("boost_rule ", "boost_mean_gamma ", "boost_mean_thresh "):
        assert "\\n" + key in c                                     # fald_dump.txt
    t = (_SRC.parent / "tests" / "test_fald.cpp").read_text(encoding="utf-8")
    assert "FLD4 word 53 selects the zone rule" in t and "FaldBoostZoneActive" in t   # no header word without a C++ test
