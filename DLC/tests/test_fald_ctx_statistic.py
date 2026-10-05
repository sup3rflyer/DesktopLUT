"""P10 context zone statistic (FaldParams.stat_kind "ctxpow", work guide P10, results/fald_p10_2026-10-05/RESULT.md): the
model's formula, the FLD5 panel file (writer + the Python twin of the C++ loader, refusals included), the GPU-order
emulator's stat pass, and that an area fit's files stay byte for byte what they were. Every pattern here is raster-aligned
(>= 5-px features) so the scale-5 model and the full-resolution emulator see one frame."""
from __future__ import annotations

import struct
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("scipy")
from dlc.fald.export import MAGIC4, MAGIC5, export_panel_params  # noqa: E402
from dlc.fald.gpuemu import Emu  # noqa: E402
from dlc.fald.model import FaldModel, FaldParams  # noqa: E402
from dlc.fald.motion import MotionModel  # noqa: E402
from dlc.fald.panelfile import read_panel_file  # noqa: E402

LUT = ((0.0, 1.178), (8 / 144, 1.167), (29 / 144, 1.10), (44 / 144, 1.0))


def _small_params(**kw):
    # 960x540 at scale 5 -> 192x108 reduced px; 12x12 cells of 80x45 px (the boost / temporal tests' fixture)
    return FaldParams(width=960, height=540, cols=12, rows=12, est_kind="exp", est_scale_mm=13.75, est_phase_px=-20.6,
                      tmin=1.5e-3, **kw)


def _raster(m, bg=0.0):
    return np.full((3, m.h, m.w), float(bg))


def _put(img, m, x0, y0, w, h, nits):
    """A rect in FULL-RES px (multiples of the scale) on the model raster."""
    sc = m.p.scale
    img[:, y0 // sc:(y0 + h) // sc, x0 // sc:(x0 + w) // sc] = float(nits)
    return img


def _full(img, scale=5):
    return np.repeat(np.repeat(img, scale, axis=1), scale, axis=2)


# ------------------------------------------------------------------------------------------------ the formula
def test_ctx_is_near_area_on_black_and_unlocks_the_peak_on_a_lit_field():
    pa, pc = _small_params(), _small_params(stat_kind="ctxpow")
    ma, mc = FaldModel(pa), FaldModel(pc)
    # a zone-filling window and a 20x20 window on BLACK: the context statistic stays within ~10 % of the area law's drive
    img = _put(_put(_raster(ma), ma, 80, 45, 80, 45, 1000.0), ma, 400, 225, 20, 20, 1000.0)
    da, dc = ma.cell_drives(img), mc.cell_drives(img)
    assert dc[1, 1] == pytest.approx(da[1, 1], rel=1e-9)                   # a full zone: frac = 1 in both
    assert da[5, 5] < dc[5, 5] < 1.15 * da[5, 5]                           # 400 px² on black: ~area (gamma ~0.93)
    # the same 20x20 window on a 20-nit field: the background unlocks the peak — far above the area law
    img2 = _put(_raster(ma, 20.0), ma, 400, 225, 20, 20, 1000.0)
    da2, dc2 = ma.cell_drives(img2), mc.cell_drives(img2)
    assert dc2[5, 5] > 1.4 * da2[5, 5]                                     # 0.70 vs 0.44 on this fixture
    assert dc2[5, 5] <= ma.drive_of(np.array(1000.0)) + 1e-12               # never above the level law
    # a uniform field: every statistic gives the level itself
    img3 = _raster(ma, 300.0)
    assert np.allclose(mc.cell_drives(img3), ma.cell_drives(img3), rtol=1e-12)


def test_ctx_floor_acts_on_black_only():
    p = _small_params(stat_kind="ctxpow")
    m = FaldModel(p)
    speck = _put(_raster(m), m, 400, 225, 5, 5, 1000.0)                   # one 5x5 speck on black
    d = m.cell_drives(speck)[5, 5]
    w = float(m.ctx_weight(np.minimum(speck.max(axis=0), p.white_nits))[5, 5])
    assert d >= p.stat_ctx_floor * (1.0 - w) * float(m.drive_of(np.array(1000.0))) - 1e-12
    no_floor = FaldModel(replace(p, stat_ctx_floor=0.0)).cell_drives(speck)[5, 5]
    assert d > no_floor                                                     # the floor lifts a tiny feature on black


def test_motion_model_ctx_on_raster_exact_content_needs_no_companion():
    p = _small_params(stat_kind="ctxpow")
    img = _put(_put(_raster(FaldModel(p), 5.0), FaldModel(p), 400, 225, 40, 45, 1000.0), FaldModel(p), 80, 90, 10, 10, 600.0)
    mm = MotionModel(p, "ctx")
    assert np.allclose(mm.cell_drives(img), FaldModel(p).cell_drives(img), rtol=1e-12)
    # and MotionModel's other statistics stay what they were for a ctx fit (the area bracket = the plain area law)
    assert np.allclose(MotionModel(p, "area").cell_drives(img), FaldModel(_small_params()).cell_drives(img), rtol=1e-12)


# ------------------------------------------------------------------------------------------------ the panel file
def test_fld5_round_trip_and_area_files_unchanged(tmp_path):
    p_area, p_lut = _small_params(), _small_params(boost_lut=LUT)
    p_ctx = _small_params(stat_kind="ctxpow")
    p_ctx_lut = _small_params(stat_kind="ctxpow", boost_lut=LUT, stat_ctx_m0=2.5, stat_ctx_g_lit=0.2,
                              stat_ctx_floor=0.1, stat_ctx_eps=0.04)
    infos = {k: export_panel_params(FaldModel(pp), tmp_path / f"{k}.bin")
             for k, pp in (("area", p_area), ("lut", p_lut), ("ctx", p_ctx), ("ctxlut", p_ctx_lut))}
    assert infos["area"]["format"] == "FLD1" and infos["lut"]["format"] == "FLD4"
    assert infos["ctx"]["format"] == "FLD5" and infos["ctxlut"]["format"] == "FLD5"
    assert infos["ctx"]["boost_in_file"] is False and infos["ctxlut"]["boost_in_file"] is True
    lut_b, ctxlut_b = (tmp_path / "lut.bin").read_bytes(), (tmp_path / "ctxlut.bin").read_bytes()
    assert struct.unpack_from("<I", lut_b, 0)[0] == MAGIC4 and struct.unpack_from("<I", ctxlut_b, 0)[0] == MAGIC5
    assert struct.unpack_from("<6I", lut_b, 42 * 4) == (0,) * 6                      # FLD4: words 42-47 stay reserved zero
    kind, m0, g, fl, eps, res = struct.unpack_from("<I4fI", ctxlut_b, 42 * 4)
    assert (kind, res) == (1, 0) and (m0, g, fl, eps) == pytest.approx((2.5, 0.2, 0.1, 0.04), rel=1e-6)
    o = read_panel_file(tmp_path / "ctxlut.bin")
    assert o["magic"] == "FLD5" and o["statKind"] == 1 and o["hasBoost"] and o["boostN"] == len(LUT)
    assert (float(o["ctxM0"]), float(o["ctxGLit"]), float(o["ctxFloor"]), float(o["ctxEps"])) == pytest.approx((2.5, 0.2, 0.1, 0.04), rel=1e-6)
    o2 = read_panel_file(tmp_path / "ctx.bin")
    assert o2["statKind"] == 1 and not o2["hasBoost"]                                # a ctx fit without a LUT: count 0
    assert read_panel_file(tmp_path / "lut.bin")["statKind"] == 0
    # the statistic changes nothing but the magic and words 42-47: the tables are the same bytes
    ref =export_panel_params(FaldModel(replace(p_ctx_lut, stat_kind="area")), tmp_path / "ref.bin")
    rb = (tmp_path / "ref.bin").read_bytes()
    assert ref["format"] == "FLD4" and rb[4:42 * 4] == ctxlut_b[4:42 * 4] and rb[48 * 4:] == ctxlut_b[48 * 4:]


@pytest.mark.parametrize("word,value,reason", [
    (42, 2, "unknown zone statistic"),
    (42, 0, "unknown zone statistic"),                      # FLD5 exists only for the context statistic
    (43, 0.0, "implausible context-statistic words"),       # m0 must be > 0
    (43, float("nan"), "implausible context-statistic words"),
    (44, 1.5, "implausible context-statistic words"),       # g_lit in 0..1
    (45, -0.1, "implausible context-statistic words"),      # floor in 0..1
    (46, 0.0, "implausible context-statistic words"),       # eps in [1e-4, 10]
    (46, 1.4e-45, "implausible context-statistic words"),   # a denormal flushes to 0 on the GPU (ln 0)
    (47, 1, "implausible context-statistic words"),         # reserved
])
def test_fld5_loader_refusals(tmp_path, word, value, reason):
    export_panel_params(FaldModel(_small_params(stat_kind="ctxpow")), tmp_path / "c.bin")
    b = bytearray((tmp_path / "c.bin").read_bytes())
    struct.pack_into("<I" if word in (42, 47) else "<f", b, word * 4, value)
    (tmp_path / "bad.bin").write_bytes(bytes(b))
    with pytest.raises(ValueError, match=reason):
        read_panel_file(tmp_path / "bad.bin")


def test_export_refuses_parameters_the_loader_would(tmp_path):
    for bad in (dict(stat_ctx_m0=0.0), dict(stat_ctx_g_lit=1.2), dict(stat_ctx_floor=-0.01), dict(stat_ctx_eps=11.0),
                dict(stat_ctx_eps=5e-5)):
        with pytest.raises(ValueError, match="context statistic"):
            export_panel_params(FaldModel(_small_params(stat_kind="ctxpow", **bad)), tmp_path / "x.bin")


# ------------------------------------------------------------------------------------------------ the GPU-order emulator
def test_emulator_stat_pass_equals_the_model(tmp_path):
    p = _small_params(stat_kind="ctxpow", boost_lut=LUT)
    m = FaldModel(p)
    export_panel_params(m, tmp_path / "c.bin")
    emu = Emu(read_panel_file(tmp_path / "c.bin"), width=p.width, height=p.height)
    for bg in (0.0, 2.0, 20.0, 150.0):
        img = _raster(m, bg)
        _put(img, m, 400, 225, 40, 45, 1000.0)       # a bar edge, a speck, a sliver, a zone-filling block
        _put(img, m, 85, 50, 5, 5, 1500.0)
        _put(img, m, 640, 90, 10, 40, 600.0)
        _put(img, m, 800, 360, 80, 45, 300.0)
        dm = m.cell_drives(img)
        de, _ = emu.stat_drive(_full(img))
        assert np.allclose(de, dm, atol=2e-3), (bg, np.max(np.abs(de - dm)))     # the curve LUT vs the analytic drive (area: same gap)
    # an area file: the emulator's stat pass is the area law (kind 0)
    pa = replace(p, stat_kind="area")
    export_panel_params(FaldModel(pa), tmp_path / "a.bin")
    ea = Emu(read_panel_file(tmp_path / "a.bin"), width=p.width, height=p.height)
    img = _put(_raster(m, 20.0), m, 400, 225, 20, 20, 1000.0)
    assert np.allclose(ea.stat_drive(_full(img))[0], FaldModel(pa).cell_drives(img), atol=2e-3)


# ------------------------------------------------------------------------------------------------ review of aa0e14b
def _scene(bg, shapes):
    from dlc.fald.motion import MovingShape, Scene
    return Scene("t", (bg,) * 3, tuple(MovingShape("rect", x + w / 2, y + h / 2, (lv,) * 3, w=w, h=h) for x, y, w, h, lv in shapes),
                 pre=0, move=0, post=1)


def test_emulator_floor_binds_on_a_dim_speck_on_black(tmp_path):
    p = _small_params(stat_kind="ctxpow")
    m = FaldModel(p)
    export_panel_params(m, tmp_path / "c.bin")
    emu = Emu(read_panel_file(tmp_path / "c.bin"), width=p.width, height=p.height)
    img = _put(_raster(m), m, 400, 225, 5, 5, 300.0)                      # a 5x5 300-nit speck on black
    s = np.minimum(img.max(axis=0), p.white_nits)
    w = float(m.ctx_weight(s)[5, 5])
    floor_d = p.stat_ctx_floor * (1.0 - w) * float(m.drive_of(np.array(300.0)))
    no_floor = float(FaldModel(replace(p, stat_ctx_floor=0.0)).cell_drives(img)[5, 5])
    assert floor_d > no_floor + 0.02                                       # the floor binds, by far more than the LUT gap
    assert m.cell_drives(img)[5, 5] == pytest.approx(floor_d, rel=1e-9)
    assert emu.stat_drive(_full(img))[0][5, 5] == pytest.approx(floor_d, abs=2e-3)


def test_sub_raster_content_needs_the_companions(tmp_path):
    from dlc.fald.motion import render_full_patch, render_reduced
    p = _small_params(stat_kind="ctxpow")
    export_panel_params(FaldModel(p), tmp_path / "c.bin")
    emu = Emu(read_panel_file(tmp_path / "c.bin"), width=p.width, height=p.height)
    # a 1-px 1000-nit line on 20 nit; 1-px 200-nit dots on black every 3 px (sub-raster texture: Jensen); a 3x3 speck on black
    dots = [(560 + 3 * i, 270 + 3 * j, 1, 1, 200.0) for i in range(20) for j in range(10)]
    # (scene, the raster alone is badly wrong): line -53 %, 3x3 speck -45 % without the companions; the dots stay within
    # ~4 % (the black-only floor dominates them) — a companion-accuracy case only
    for sc, raster_wrong in ((_scene(20.0, [(401, 230, 1, 30, 1000.0)]), True), (_scene(0.0, dots), False),
                             (_scene(0.0, [(401, 231, 3, 3, 1000.0)]), True)):
        full = render_full_patch(sc, 0, 0, 0, p.width, p.height)
        de, _ = emu.stat_drive(full)
        img, pk, lg = render_reduced(sc, 0, p.scale, p.width, p.height, log_eps=p.stat_ctx_eps, white=p.white_nits)
        mm = MotionModel(p, "ctx")
        mm.set_peak(pk, lg)
        dm = mm.cell_drives(img)
        mm.set_peak(None)
        assert np.allclose(dm, de, atol=2e-3), np.max(np.abs(dm - de))
        with pytest.warns(RuntimeWarning, match="peak companion"):
            raw = FaldModel(p).cell_drives(img)                            # the raster alone: warned
        if raster_wrong:
            assert np.max(np.abs(raw - de)) > 0.3 * np.max(de)


def test_correct_image_follows_the_log_companion():
    from dlc.fald.correct import correct_image
    from dlc.fald.motion import render_reduced
    p = _small_params(stat_kind="ctxpow")
    sc = _scene(0.0, [(560 + 3 * i, 270 + 3 * j, 1, 1, 200.0) for i in range(20) for j in range(10)])
    img, pk, lg = render_reduced(sc, 0, p.scale, p.width, p.height, log_eps=p.stat_ctx_eps, white=p.white_nits)
    m = MotionModel(p, "ctx")
    a = correct_image(m, img, peak=pk, logm=lg)
    m.set_peak(None)
    b = correct_image(m, img, peak=pk)                                      # no log companion: the raster's own logs
    m.set_peak(None)
    assert not np.allclose(a["drives"], b["drives"], atol=1e-3)            # the companion is used ...
    lg2 = m.logm_follow(lg, img, img)
    assert np.array_equal(lg2, np.maximum(lg, np.log(p.stat_ctx_eps)))     # ... and is the content's own on round 1


def test_starfield_reference_follows_a_ctx_fit(tmp_path):
    from dlc.fald.starfield import StarfieldParams, zone_plan
    p = _small_params(stat_kind="ctxpow")
    m = FaldModel(p)
    export_panel_params(m, tmp_path / "c.bin")
    emu = Emu(read_panel_file(tmp_path / "c.bin"), width=p.width, height=p.height)
    img = _put(_put(_raster(m, 3.0), m, 400, 225, 20, 20, 1000.0), m, 80, 90, 5, 5, 600.0)
    sp = StarfieldParams()
    ref, gpu = zone_plan(m, img, sp)["solid"], emu.star_stat(_full(img), sp)["solid"]
    assert np.allclose(ref, gpu, atol=2e-3), np.max(np.abs(ref - gpu))
    area_solid = zone_plan(FaldModel(replace(p, stat_kind="area")), img, sp)["solid"]
    assert np.max(np.abs(area_solid - gpu)) > 0.05                          # the area law would not have matched


def test_simulate_scene_layer_runs_the_fits_statistic():
    from dlc.fald.motion import simulate_scene
    from dlc.fald.temporal import DriveState
    sc = _scene(5.0, [(400, 225, 5, 5, 1000.0)])                          # a speck: area and ctx disagree on its zone
    for kind, explicit in (("ctxpow", "ctx"), ("area", "area")):
        p = _small_params(stat_kind=kind)
        a = simulate_scene(p, sc, state=DriveState(), panels=(("ctx", 0),))
        b = simulate_scene(p, sc, state=DriveState(), panels=(("ctx", 0),), layer_stat=explicit)
        assert np.array_equal(a.runs[0].ys, b.runs[0].ys)
    p = _small_params(stat_kind="ctxpow")
    c = simulate_scene(p, sc, state=DriveState(), panels=(("ctx", 0),), layer_stat="area")
    d = simulate_scene(p, sc, state=DriveState(), panels=(("ctx", 0),))
    assert not np.allclose(c.runs[0].ys, d.runs[0].ys, rtol=1e-4)          # the default is NOT the area layer for a ctx fit
