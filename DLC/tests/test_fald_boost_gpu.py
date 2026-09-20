"""Black-frame LED boost in the panel file and the shader (work guide C12): the FLD4 block round-trips, the GPU's
lookup by zone COUNT equals ``FaldModel.boost_of_fraction``, and the GPU-order emulator (dlc/fald/gpuemu.py — the HLSL
passes line for line) applies the same two-round boost as ``correct_image``. The emulator counts full-resolution
pixels, the model scale-5 raster pixels: every pattern here is raster-aligned (>= 5-px features) so both see one frame."""
from __future__ import annotations

import re
import struct
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.correct import correct_image, shader_model  # noqa: E402
from dlc.fald.export import BOOST_MAX_STEPS, MAGIC, MAGIC4, export_panel_params  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.panelfile import boost_of_count, boost_zone_threshold, cb, read_panel_file  # noqa: E402

_SRC = Path(__file__).resolve().parents[2] / "src"
_SHADER = _SRC / "fald_shader.h"

# The measured PA32UCXR staircase (results/fald_inside_2026-09-18/boost_table.json; results/ is local-only, so the
# table is repeated here): N <= 37 -> 1.178, <= 145 -> 1.167, ..., the dead band N 235..255 -> 1.000, 256 -> 1.071 ...
PA_LUT = ((0.0, 1.178392501694869), (38 / 2304, 1.1666131220943428), (145.5 / 2304, 1.1458135945400123),
          (166.5 / 2304, 1.1140031435996065), (192 / 2304, 1.1027556327481955), (213.5 / 2304, 1.0962562949617367),
          (234.5 / 2304, 1.0), (255.5 / 2304, 1.071209981042591), (276 / 2304, 1.063124639797543),
          (312 / 2304, 1.0573641116023964), (330 / 2304, 1.048791679523251), (414 / 2304, 1.0270432868217054),
          (486 / 2304, 1.0166088595336076), (654 / 2304, 1.0080826437723391), (801 / 2304, 1.0))
# the same shape for the 12 x 12 test lattice (144 zones): N <= 7 -> 1.178, 8..28 -> 1.167, 29..43 -> 1.10,
# a dead band 44..57 -> 1.0, 58..86 -> 1.07, from 87 -> 1.0
SMALL_LUT = ((0.0, 1.178), (8 / 144, 1.167), (29 / 144, 1.10), (44 / 144, 1.0), (58 / 144, 1.07), (87 / 144, 1.0))


def _small_params(**kw):
    # 960x540 at scale 5 -> 192x108 reduced px; 12x12 cells of 80x45 px (tests/test_fald_temporal.py's fixture)
    return FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, **kw)


def _scrgb(img_nits, scale=5):
    """Model raster (3, h, w) of as-if-white nits -> full-resolution scRGB frame (H, W, 3). Grey stays grey through
    the BT.709 -> BT.2020 matrix (rows sum to 1), so PQ as-if-white nits = scRGB x 80."""
    return np.ascontiguousarray(np.repeat(np.repeat(img_nits, scale, axis=1), scale, axis=2).transpose(1, 2, 0) / 80.0)


def _block(m, c0, r0, nc, nr, nits, bg=0.0):
    """A lattice-aligned block of nc x nr zones at zone (c0, r0) on a uniform background, on the model raster."""
    img = np.full((3, m.h, m.w), float(bg))
    img[:, r0 * m.ch:(r0 + nr) * m.ch, c0 * m.cw:(c0 + nc) * m.cw] = float(nits)
    return img


def _pair(tmp_path, params_blind, lut, **act):
    """(boost-blind model, its emulator, boost-aware model, its emulator) from two exports of the same fit."""
    mb = FaldModel(params_blind)
    ma = FaldModel(replace(params_blind, boost_lut=lut, **act))
    ib = export_panel_params(mb, tmp_path / "blind.bin")
    ia = export_panel_params(ma, tmp_path / "aware.bin")
    assert ib["boost_in_file"] is False and ib["format"] == "FLD1" and ia["boost_in_file"] is True and ia["format"] == "FLD4"
    ob, oa = read_panel_file(tmp_path / "blind.bin"), read_panel_file(tmp_path / "aware.bin")
    w, h = params_blind.width, params_blind.height
    return mb, Emu(ob, width=w, height=h), ma, Emu(oa, width=w, height=h)


# ------------------------------------------------------------------------------------------------ the file
def test_fld4_round_trip_and_a_lutless_fit_is_unchanged(tmp_path):
    p0 = _small_params()
    p1 = replace(p0, boost_lut=PA_LUT, boost_lit_nits=0.4, boost_lit_frac=0.001, boost_dim_nits=0.02, boost_dim_frac=0.25)
    i0 = export_panel_params(FaldModel(p0), tmp_path / "a.bin")
    i1 = export_panel_params(FaldModel(p1), tmp_path / "b.bin")
    a, b = (tmp_path / "a.bin").read_bytes(), (tmp_path / "b.bin").read_bytes()
    assert i0["format"] == "FLD1" and i0["header_bytes"] == 128 and i0["boost_in_file"] is False and i0["boost_lut_steps"] == 0
    assert i1["format"] == "FLD4" and i1["header_bytes"] == 416 and i1["boost_in_file"] is True and i1["boost_lut_steps"] == 15
    assert struct.unpack_from("<I", a, 0)[0] == MAGIC and struct.unpack_from("<I", b, 0)[0] == MAGIC4
    # the boost changes nothing but the magic and the header tail: words 1-31 and every table byte are the FLD1 file's
    assert a[4:128] == b[4:128] and a[128:] == b[416:] and len(b) == len(a) + 288
    assert b[128:192] == bytes(64)                                  # no pedestal colour, transfer PQ, sdr_gamma 0
    assert struct.unpack_from("<I", b, 48 * 4)[0] == 15 and b[53 * 4:56 * 4] == bytes(12)
    assert b[(56 + 2 * 15) * 4:416] == bytes((24 - 15) * 8)         # unused steps are zero
    o = read_panel_file(tmp_path / "b.bin")
    assert o["magic"] == "FLD4" and o["hasBoost"] and o["boostN"] == 15 and o["hasTransfer"] and o["transfer"] == 0
    assert not o["hasPedColour"] and o["header_bytes"] == 416
    assert [(float(lo), float(v)) for lo, v in o["boostLut"]] == [(float(np.float32(lo)), float(np.float32(v))) for lo, v in PA_LUT]
    assert (float(o["boostLitNits"]), float(o["boostLitFrac"]), float(o["boostDimNits"]), float(o["boostDimFrac"])) == \
        tuple(float(np.float32(x)) for x in (0.4, 0.001, 0.02, 0.25))
    o0 = read_panel_file(tmp_path / "a.bin")
    assert o0["hasBoost"] is False and o0["boostN"] == 0 and o0["boostLut"] == [] and float(o0["boostLitNits"]) == float(np.float32(0.35))
    assert np.array_equal(o0["kTrue"], o["kTrue"]) and np.array_equal(o0["curve"], o["curve"])
    c = cb(o)
    assert c["boostNCB"] == 15 and cb(o0)["boostNCB"] == 0
    assert [int(n) for n, _ in c["boostLutCounts"]] == [0, 3, 10, 11, 12, 14, 15, 16, 18, 20, 21, 26, 31, 41, 51]   # 144 zones
    # every optional block at once: pedestal colour + gamma transfer + boost
    p2 = replace(p1, transfer="gamma", sdr_gamma=2.27, tmin_rgb=(0.9, 1.0, 1.3), ped_mode="channel")
    i2 = export_panel_params(FaldModel(p2), tmp_path / "c.bin")
    o2 = read_panel_file(tmp_path / "c.bin")
    assert i2["format"] == "FLD4" and o2["hasPedColour"] and o2["transfer"] == 1 and float(o2["sdrGamma"]) == float(np.float32(2.27))
    assert o2["boostN"] == 15


def test_export_and_reader_refuse_what_the_cpp_loader_refuses(tmp_path):
    p = _small_params()
    bad_luts = {"steps": tuple((i / 40, 1.1) for i in range(BOOST_MAX_STEPS + 1)),
                "gate": ((0.0, 1.17), (0.2, 1.1), (0.1, 1.0)),                    # descending
                "gate ": ((0.0, 1.17), (0.2, 1.1), (0.2, 1.0)),                   # not strictly ascending
                "gate  ": ((0.0, 1.17), (1.5, 1.0)), "gate   ": ((0.0, 2.5),), "gate    ": ((0.0, 0.4),)}
    for needle, lut in bad_luts.items():
        with pytest.raises(ValueError, match=needle.strip()):
            export_panel_params(FaldModel(replace(p, boost_lut=lut)), tmp_path / "never.bin")
    with pytest.raises(ValueError, match="activation"):
        export_panel_params(FaldModel(replace(p, boost_lut=SMALL_LUT, boost_dim_frac=1.0)), tmp_path / "never.bin")
    assert not (tmp_path / "never.bin").exists()                     # refused before anything is written
    export_panel_params(FaldModel(replace(p, boost_lut=SMALL_LUT)), tmp_path / "ok.bin")
    base = bytearray((tmp_path / "ok.bin").read_bytes())

    def patched(words: dict) -> Path:
        buf = bytearray(base)
        for i, (fmt, v) in words.items():
            struct.pack_into("<" + fmt, buf, i * 4, v)
        (tmp_path / "bad.bin").write_bytes(bytes(buf))
        return tmp_path / "bad.bin"
    cases = [("implausible boost step count", {48: ("I", 25)}),
             ("implausible boost LUT words", {58: ("f", 0.9), 60: ("f", 0.5)}),   # step 1 above step 2
             ("implausible boost LUT words", {58: ("f", 1.5)}),
             ("implausible boost LUT words", {57: ("f", 0.4)}),
             ("implausible boost LUT words", {57: ("f", 2.5)}),
             ("implausible boost LUT words", {57: ("f", float("nan"))}),
             ("implausible boost LUT words", {57: ("I", 0)}),                      # a missing boost is not 1.0
             ("implausible boost activation words", {49: ("f", -1.0)}),
             ("implausible boost activation words", {50: ("f", 1.0)}),
             ("implausible boost activation words", {52: ("f", float("nan"))}),
             ("unknown transfer", {40: ("I", 2)})]
    for needle, words in cases:
        with pytest.raises(ValueError, match=needle):
            read_panel_file(patched(words))
    o = read_panel_file(patched({48: ("I", 0), 49: ("f", -5.0)}))   # step count 0: the block is ignored, defaults stay
    assert o["hasBoost"] is False and o["boostN"] == 0 and float(o["boostLitNits"]) == float(np.float32(0.35))
    (tmp_path / "short.bin").write_bytes(bytes(base[:400]))
    with pytest.raises(ValueError, match="too short"):
        read_panel_file(tmp_path / "short.bin")
    (tmp_path / "size.bin").write_bytes(bytes(base[:-4]))
    with pytest.raises(ValueError, match="size mismatch"):
        read_panel_file(tmp_path / "size.bin")


def test_lookup_by_zone_count_equals_the_models_fraction_lookup(tmp_path):
    """HLSL pass 1a / C++ FaldBoostOfCount look the step up by the integer zone count; the model by the fraction."""
    for params, lut in ((FaldParams(), PA_LUT), (_small_params(), SMALL_LUT), (FaldParams(), ((0.25, 1.1), (0.5, 1.0)))):
        m = FaldModel(replace(params, boost_lut=lut))
        export_panel_params(m, tmp_path / "lut.bin")
        o = read_panel_file(tmp_path / "lut.bin")
        zones = m.p.rows * m.p.cols
        for n in range(zones + 1):
            assert float(boost_of_count(o, n)) == float(np.float32(m.boost_of_fraction(n / zones))), n
    # the numbers tests/test_fald.cpp pins for FaldBoostZoneThreshold / FaldBoostOfCount
    th = lambda lo, z=2304: boost_zone_threshold(np.float32(lo), z)
    assert (th(0.0), th(38 / 2304), th(145.5 / 2304), th(234.5 / 2304), th(255.5 / 2304), th(801 / 2304), th(1.0)) == \
        (0, 38, 146, 235, 256, 801, 2304)
    assert all(th(n / 2304) == n for n in range(2305)) and th(0.5, 512 * 512) == 131072
    export_panel_params(FaldModel(replace(FaldParams(), boost_lut=PA_LUT)), tmp_path / "pa.bin")
    o = read_panel_file(tmp_path / "pa.bin")
    got = {n: round(float(boost_of_count(o, n)), 4) for n in (0, 37, 38, 126, 145, 146, 234, 235, 255, 256, 800, 801, 2304)}
    assert got == {0: 1.1784, 37: 1.1784, 38: 1.1666, 126: 1.1666, 145: 1.1666, 146: 1.1458, 234: 1.0963, 235: 1.0, 255: 1.0,
                   256: 1.0712, 800: 1.0081, 801: 1.0, 2304: 1.0}
    assert float(boost_of_count(read_panel_file(tmp_path / "lut.bin"), 575)) == 1.0          # below the first step of a LUT that starts above 0


def test_shader_model_is_the_fit_itself_now_that_the_file_carries_the_boost(tmp_path):
    from dlc.fald import correct
    m = FaldModel(replace(_small_params(), boost_lut=SMALL_LUT))
    assert correct.BOOST_IN_PANEL_FILE is True and shader_model(m) is m
    assert export_panel_params(m, tmp_path / "m.bin")["boost_in_file"] is True


# ------------------------------------------------------------------------------------------------ the zone count
def test_emulator_zone_flags_follow_the_lit_or_dim_rule_at_full_resolution(tmp_path):
    _, emu_blind, m, emu = _pair(tmp_path, FaldParams(), PA_LUT)
    assert emu.boostN == 15
    H, W = 2160, 3840

    def count(paint):
        img = np.zeros((3, H, W), dtype=np.float32); paint(img)
        b, n, act = emu.frame_boost(img)
        assert act.shape == (48, 48) and int(act.sum()) == n and float(b) == float(boost_of_count(emu.o, n))
        return n

    def fill(v):
        return lambda img: img.__setitem__(slice(None), v)

    def col(x0, w, v):
        return lambda img: img.__setitem__((slice(None), slice(None), slice(x0, x0 + w)), v)
    assert count(fill(0.0)) == 0 and count(fill(0.005)) == 0 and count(fill(0.02)) == 2304     # PQ10 code 16 black, 32 not
    assert count(col(2400, 1, 10.0)) == 48                          # r9d: a 1-px 10-nit column lights its 48 zones
    assert count(col(2400, 1, 0.3)) == 0 and count(col(2400, 2, 0.4)) == 48      # pixrule: LIT between 0.3 and 0.4 nits
    assert count(col(640, 14, 0.2)) == 0 and count(col(640, 15, 0.2)) == 0       # r9d: 0.2 nits only from 16 px of 80
    assert count(col(640, 16, 0.2)) == 48
    assert count(lambda img: img.__setitem__((0, slice(45, 90), slice(80, 160)), 5.0)) == 1   # one channel is enough (max)
    # the model's raster count agrees on raster-aligned content
    img5 = _block(m, 20, 18, 9, 14, 923.0)
    assert round(m.active_zone_fraction(img5) * 2304) == 126
    assert emu.frame_boost(np.repeat(np.repeat(img5, 5, axis=1), 5, axis=2))[1] == 126
    # the dead band's edges: 16 x 15 zones = 240 -> 1.0; a lattice-aligned 16 x 16 window = 256 is OUTSIDE it (x 1.071)
    for nr, want in ((15, 1.0), (16, 1.0712)):
        b, n, _ = emu.frame_boost(np.repeat(np.repeat(_block(m, 16, 16, 16, nr, 923.0), 5, axis=1), 5, axis=2))
        assert n == 16 * nr and round(float(b), 4) == want
    # a file without a LUT: no flags, no boost pass
    assert emu_blind.boostN == 0 and emu_blind.frame_boost(np.zeros((3, H, W))) == (None, -1, None)


# ------------------------------------------------------------------------------------------------ the two-round boost
def _interior(m, c0, r0, nc, nr, scale):
    """Slice of the block's inner part (one zone in from every edge); scale 1 = the model raster, 5 = full resolution."""
    k = scale
    return (slice((r0 + 1) * m.ch * k, (r0 + nr - 1) * m.ch * k), slice((c0 + 1) * m.cw * k, (c0 + nc - 1) * m.cw * k))


def _check_three_regimes(tmp_path, params, lut, window, dead, level, boost_window):
    mb, eb, ma, ea = _pair(tmp_path, params, lut)
    zones = params.rows * params.cols
    # (i) every zone non-black: boost 1 in both rounds, and the LUT file's output is the LUT-less file's, bit for bit
    img = _block(mb, *window, level, bg=5.0)
    a, b = ea.run(_scrgb(img), fp16_out=False), eb.run(_scrgb(img), fp16_out=False)
    assert (a["zones0"], a["zones1"], a["boost0"], a["boost1"]) == (zones, zones, 1.0, 1.0)
    assert (b["zones0"], b["boost0"], b["boost1"]) == (-1, 1.0, 1.0) and b["active1"] is None
    assert np.array_equal(a["out"], b["out"]) and np.array_equal(a["bT"], b["bT"]) and np.array_equal(a["drive1"], b["drive1"])
    ref = correct_image(ma, img)
    assert ref["boost"] == 1.0 and np.array_equal(ref["req"], correct_image(mb, img)["req"])
    assert np.allclose(a["drive1"], ref["drives"], atol=0.02)       # the emulator's known discretisation gap
    # (ii) the window on black: both pipelines take the boost in both rounds; the corrected interior drops by ~1/boost
    img = _block(mb, *window, level)
    n_win = window[2] * window[3]
    a, b = ea.run(_scrgb(img), fp16_out=False), eb.run(_scrgb(img), fp16_out=False)
    ref, ref_blind = correct_image(ma, img), correct_image(mb, img)
    assert a["zones0"] == a["zones1"] == n_win == round(ma.active_zone_fraction(img) * zones)
    assert a["boost0"] == a["boost1"] == float(np.float32(boost_window)) and ref["boost"] == boost_window
    assert np.array_equal(a["active0"], a["active1"]) and int(a["active1"].sum()) == n_win
    s_py, s_gpu = _interior(mb, *window, 1), _interior(mb, *window, 5)
    gpu_aware, gpu_blind = float(a["req"][0][s_gpu].mean()), float(b["req"][0][s_gpu].mean())
    py_aware, py_blind = float(ref["req"][0][s_py].mean()), float(ref_blind["req"][0][s_py].mean())
    assert gpu_aware / gpu_blind == pytest.approx(1.0 / boost_window, rel=0.015)
    assert py_aware / py_blind == pytest.approx(1.0 / boost_window, rel=0.015)
    assert gpu_aware == pytest.approx(py_aware, rel=0.015) and gpu_blind == pytest.approx(py_blind, rel=0.015)
    # (iii) a dead-band frame: boost exactly 1 -> the boost-aware layer IS the boost-blind one
    img = _block(mb, *dead, level)
    a, b = ea.run(_scrgb(img), fp16_out=False), eb.run(_scrgb(img), fp16_out=False)
    assert a["zones0"] == a["zones1"] == dead[2] * dead[3] and a["boost0"] == a["boost1"] == 1.0
    assert ma.led_boost(img) == 1.0 and np.array_equal(a["out"], b["out"])
    return gpu_aware, gpu_blind


def test_emulator_two_round_boost_matches_correct_image_small_lattice(tmp_path):
    # 144 zones: a 3 x 4-zone window = 12 -> x1.167; a 6 x 8-zone block = 48 sits in the 44..57 dead band
    _check_three_regimes(tmp_path, _small_params(), SMALL_LUT, window=(4, 4, 3, 4), dead=(3, 2, 6, 8), level=300.0,
                         boost_window=1.167)


@pytest.mark.slow
def test_emulator_two_round_boost_matches_correct_image_pa32ucxr_frame(tmp_path):
    """The HW 2026-09-18 case at full size: a ~600-px 923-nit window on black = 9 x 14 zones = 126 of 2304 -> x1.167
    (720 x 630 px here: lattice-aligned, the zones the 600-px window at the meter spot touches); 16 x 15 zones = 240 is
    a dead-band frame. ~8 s per emulator run at 3840 x 2160, hence slow."""
    # (FaldParams() defaults, not the shipped fit — results/ is local-only: the absolute interior level is not the
    # +15-18 % of the HW read; the 1/boost ratio and GPU = Python are what this pins)
    _check_three_regimes(tmp_path, FaldParams(), PA_LUT, window=(20, 18, 9, 14), dead=(16, 16, 16, 15),
                         level=923.0, boost_window=1.1666131220943428)


# ------------------------------------------------------------------------------------------------ HLSL / C++ mirrors
@pytest.mark.skipif(not _SHADER.exists(), reason="DesktopLUT C++ tree not next to DLC")
def test_hlsl_boost_passes_mirror_the_reference():
    src = _SHADER.read_text(encoding="utf-8")
    part = lambda name: re.search(name + r' = R"\((.*?)\)";', src, re.S).group(1)
    common, stat, boost, conv, pixel = (part(n) for n in ("g_faldCommonSource", "g_faldStatSource", "g_faldBoostSource",
                                                          "g_faldConvSource", "g_faldPixelSource"))
    assert "float lumFadeLo; float lumFadeHi; uint boostN; uint starOn;" in common                # CB word 34 (35: S1)
    assert "float boostLitNits; float boostLitFrac; float boostDimNits; float boostDimFrac;" in common   # CB words 48-51
    assert common.index("uint tempMode; uint tempInit;") < common.index("float boostLitNits;")
    for reg in ("boostLut    : register(t12)", "activeTex   : register(t13)", "boostTex    : register(t14)"):
        assert reg in common
    # stat pass: counts on the frame the panel receives (after Correct in round 1), raw max channel, strict '>'
    assert stat.index("img = Correct(img, bT, bE, g);") < stat.index("if (mc > boostLitNits) lit++;")
    assert "float mc = max(img.r, max(img.g, img.b));" in stat and "if (mc > boostDimNits) dim++;" in stat
    # the second criterion is the file's zone rule since C12b (tests/test_fald_zone_rule.py): rule 0 = DIM, as before
    assert "bool second = (boostRule == 1u) ? (gPow[0] / (float)n >= boostMeanThresh) : (dimF > boostDimFrac);" in stat
    assert "activeOut[uint2(cx, cy)] = (litF > boostLitFrac || second) ? 1.0f : 0.0f;" in stat
    assert "if (px >= frameW || py >= frameH) continue;" in stat
    assert stat.count("if (boostN != 0u)") == 2                     # no LUT: neither counted nor written
    # boost pass: count -> last step whose first count is <= N, 1 below the first
    assert "if (activeTex.Load(int3(x, y, 0)) > 0.5f) count++;" in boost
    assert "if ((float)count < boostLut[2 * i]) break;" in boost and "b = boostLut[2 * i + 1];" in boost
    assert "float b = 1.0f;" in boost and "boostOut[uint2(0, 0)] = b;" in boost
    # conv pass: B_true only
    assert "if (boostN != 0u) accT *= boostTex.Load(int3(0, 0, 0));" in conv and "accE *=" not in conv
    assert conv.index("accT *= boostTex") < conv.index("bTrueOut[uint2(fx, fy)] = accT;")
    assert "if (debugMode == 8)" in pixel
    # C++: the CB size, the file constants, the boost-free flat-lattice pass
    h = (_SRC / "fald.h").read_text(encoding="utf-8")
    c = (_SRC / "fald.cpp").read_text(encoding="utf-8")
    assert "FALD_CB_BYTES = 304" in h and f"FALD_BOOST_MAX_STEPS = {BOOST_MAX_STEPS}" in h   # 76 words since C12b (zone rule)
    assert "0x464C4434u" in c and "magic == FALD_MAGIC4 ? 416" in c
    assert "FillCB(r, 0, 0, false);" in c and "RunConv(r, r->driveSRV, r->driveSRV, nullptr);" in c
    assert "std::ceil((double)lo * z - (1e-3 + 1e-6 * z))" in c     # panelfile.boost_zone_threshold's twin
    assert c.index("RunStat(r, 0);") < c.index("RunBoost(r, 0);") < c.index("RunConv(r, trueDrive, estDrive, r->boostSRV[0]);")
    assert c.index("RunStat(r, 1);") < c.index("RunBoost(r, 1);") < c.index("RunConv(r, trueDrive, estDrive, r->boostSRV[1]);")
